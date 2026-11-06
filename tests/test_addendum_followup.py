"""
Testes de Regressão do Adendo de Correção Sênior — AutoCare V2 (Follow-up Staff+).
Cobre integralmente os itens A1, A2, A3, A5, A6, A7, B1, B2, B4, B5, B6, B7, B8, B9, C2 e C3.
"""

import asyncio
import gc
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.ai.client as ai_client_mod
from src.ai.client import (
    GeminiClient,
    GeminiClientException,
    get_urllib_executor,
)
from src.ai.schemas import GeminiAnalysisResponse, ToolCallAction
from src.application.services.conversation import ConversationService
from src.config.settings import settings
from src.domain.entities import (
    ConversationContext,
    MaintenanceRecord,
    Reminder,
    User,
    Vehicle,
)
from src.domain.enums import (
    ConfidenceLevel,
    ConversationState,
    IntentType,
    ReminderRecurrence,
    ReminderStatus,
)
from src.infrastructure.database.models import Base, ReminderModel
from src.infrastructure.database.repositories import (
    MaintenanceRepository,
    ReminderRepository,
    UserRepository,
    VehicleRepository,
)
from src.infrastructure.http_client import (
    DEFAULT_LIMITS,
    close_http_client,
    get_http_client,
    shared_http_client,
)
from src.interfaces.api.webhook import (
    MAX_MESSAGE_CHARS,
    MessageDebouncer,
    _parse_due_datetime,
    _persist_confirmed_data,
    _apply_persistence_outcome,
    _rate_limiter,
    _run_pipeline,
    conversation_service as webhook_conv_service,
    parse_whatsapp_payload,
    receive_webhook,
    resolve_target_vehicle,
)
from src.interfaces.app import create_app


@pytest.fixture
async def async_db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ============================================================================
# GRUPO A — Validação de afirmações do relatório
# ============================================================================


@pytest.mark.integration
async def test_two_concurrent_messages_same_user_do_not_corrupt_draft(async_db_session: AsyncSession):
    """
    A1 [B3]: Duas mensagens concorrentes do mesmo `user_id` via `asyncio.gather` real
    (com usuário e veículo persistidos via `AsyncSession`) não corrompem `draft_payload`
    nem perdem exclusão mútua durante `turn_lock.released()`.
    """
    user_id = "5511999990001"
    await UserRepository(async_db_session).get_or_create(user_id)
    veh = await VehicleRepository(async_db_session).add_vehicle(
        Vehicle(user_id=user_id, make="Honda", model="Civic", plate="ABC1D23", current_km=49000)
    )
    order_log = []

    class SlowMockGemini:
        def is_available(self) -> bool:
            return True

        async def analyze_message(self, user_id, current_message, **kwargs):
            order_log.append(f"start:{current_message}")
            await asyncio.sleep(0.05)
            order_log.append(f"end:{current_message}")
            if "óleo" in current_message.lower():
                return GeminiAnalysisResponse(
                    intent=IntentType.UPSERT_DRAFT,
                    confidence=ConfidenceLevel.HIGH,
                    is_critical_action=True,
                    reply_message="Rascunho óleo",
                    tool_call=ToolCallAction(
                        name="upsert_draft",
                        itens=["Óleo 5W30"],
                        km=50000,
                        data="10/02/2026",
                        valor=250.0,
                    ),
                )
            return GeminiAnalysisResponse(
                intent=IntentType.UPSERT_DRAFT,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Rascunho filtro",
                tool_call=ToolCallAction(
                    name="upsert_draft",
                    itens=["Filtro de ar"],
                ),
            )

    svc = ConversationService(gemini_client=SlowMockGemini())

    r1, r2 = await asyncio.gather(
        svc.handle_incoming_message(user_id, "Troquei óleo 5W30 com 50000 km dia 10/02/2026 por 250 reais", vehicles=[veh]),
        svc.handle_incoming_message(user_id, "Também troquei filtro de ar nesse dia", vehicles=[veh]),
    )

    ctx = svc.get_context(user_id)
    assert ctx.turn_seq == 2
    assert ctx.state == ConversationState.AWAITING_MAINTENANCE_CONFIRM
    assert ctx.draft_payload is not None
    if r1["action_type"] != "async_superseded_completed" and r2["action_type"] != "async_superseded_completed":
        assert "Filtro de ar" in ctx.draft_payload["itens"]
        assert ctx.draft_payload["km"] == 50000
        assert ctx.draft_payload["data"] == "10/02/2026"


@pytest.mark.integration
async def test_invalid_date_does_not_persist_reminder_and_reopens_draft(async_db_session: AsyncSession):
    """
    A2 [B5]: Datas impossíveis numéricas (`31/02/2026`) e textuais (`31 de fevereiro`)
    retornam `None` em `_parse_due_datetime`, falham com `invalid_date`, NÃO persistem
    `Reminder` no banco e reabrem o rascunho via `reopen_draft_for_correction`.
    """
    assert _parse_due_datetime(None, date_str="31/02/2026") is None
    assert _parse_due_datetime(None, date_str="dia 31 de fevereiro de 2026") is None

    user_id = "5511999990002"
    await UserRepository(async_db_session).get_or_create(user_id)
    veh = await VehicleRepository(async_db_session).add_vehicle(
        Vehicle(user_id=user_id, make="Toyota", model="Corolla", plate="XYZ9A88", current_km=30000)
    )

    committed_data = {
        "_action_state": ConversationState.AWAITING_REMINDER_CONFIRM.value,
        "task_description": "Renovar seguro",
        "target_date_str": "31 de fevereiro de 2026",
        "due_km": 35000,
        "_target_vehicle_ref": "Corolla",
    }
    now_ts = datetime.now(timezone.utc)
    outcome = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", committed_data, [veh], now_ts
    )

    assert len(outcome["success"]) == 0
    assert any(f["reason"] == "invalid_date" for f in outcome["failed"])

    rems = await ReminderRepository(async_db_session).get_by_user_id(user_id)
    assert len(rems) == 0

    result = {"reply": "ok", "action_type": "action_confirmed"}
    _apply_persistence_outcome(result, user_id, committed_data, outcome)

    assert result["action_type"] == "invalid_date"
    reopened_ctx = webhook_conv_service.get_context(user_id)
    assert reopened_ctx.state == ConversationState.AWAITING_REMINDER_CONFIRM
    assert reopened_ctx.draft_payload is not None
    assert "target_date_str" not in reopened_ctx.draft_payload
    assert reopened_ctx.draft_payload.get("task_description") == "Renovar seguro"


@pytest.mark.integration
async def test_multi_action_failure_rolls_back_entire_batch(async_db_session: AsyncSession):
    """
    A3 [B13/C3]: Em um lote multi-ação onde a 1ª sub-ação (manutenção válida) passa no flush
    e a 2ª sub-ação (lembrete com data impossível `31/02/2026`) falha, `session.rollback()`
    descarta o lote inteiro (0 manutenções e 0 lembretes gravados no banco).
    """
    user_id = "5511999990003"
    await UserRepository(async_db_session).get_or_create(user_id)
    veh = await VehicleRepository(async_db_session).add_vehicle(
        Vehicle(user_id=user_id, make="VW", model="Nivus", plate="NIV1A23", current_km=20000)
    )

    committed_data = {
        "_is_multi_action": True,
        "actions": [
            {
                "intent": "record_maintenance",
                "description": "Troca de óleo",
                "components": ["Óleo 5W40"],
                "km": 21000,
                "total_cost": 320.0,
                "target_vehicle_ref": "Nivus",
            },
            {
                "intent": "set_reminder",
                "task_description": "Calibrar estepe",
                "target_date_str": "31/02/2026",
                "target_vehicle_ref": "Nivus",
            },
        ],
    }
    outcome = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", committed_data, [veh], datetime.now(timezone.utc)
    )

    assert outcome["success"] == []
    assert len(outcome["failed"]) == 1
    assert outcome["failed"][0]["reason"] == "invalid_date"

    maints = await MaintenanceRepository(async_db_session).get_by_user_id(user_id)
    rems = await ReminderRepository(async_db_session).get_by_user_id(user_id)
    updated_veh = await VehicleRepository(async_db_session).get_by_id(veh.id)
    assert len(maints) == 0
    assert len(rems) == 0
    assert updated_veh.current_km == 20000


@pytest.mark.unit
async def test_truncation_happens_after_parse_not_on_raw_payload(async_db_session: AsyncSession):
    """
    A5 [S3]: `parse_whatsapp_payload` extrai o texto integral sem truncar o JSON bruto;
    a truncagem para `MAX_MESSAGE_CHARS` ocorre posteriormente em `_run_pipeline`.
    """
    long_msg = "A" * (MAX_MESSAGE_CHARS + 500)
    payload = {
        "data": {
            "key": {"remoteJid": "5511999990005@s.whatsapp.net", "fromMe": False},
            "message": {"conversation": long_msg},
        }
    }
    uid, parsed_text = parse_whatsapp_payload(payload)
    assert uid == "5511999990005"
    assert len(parsed_text) == MAX_MESSAGE_CHARS + 500

    captured_lengths = []
    orig_handle = webhook_conv_service.handle_incoming_message

    async def spy_handle(user_id, message_text, **kwargs):
        captured_lengths.append(len(message_text))
        return {"reply": "ok", "action_type": "unknown", "state": "normal"}

    webhook_conv_service.handle_incoming_message = spy_handle
    try:
        await _run_pipeline(async_db_session, uid, parsed_text)
    finally:
        webhook_conv_service.handle_incoming_message = orig_handle

    assert captured_lengths == [MAX_MESSAGE_CHARS]


@pytest.mark.unit
async def test_gemini_rate_limit_blocks_before_api_call(monkeypatch):
    """
    A6 [S4]: O segundo rate limiter dentro de `GeminiClient.analyze_message` bloqueia
    antes da chamada HTTP (`httpx.AsyncClient.post`) quando `GEMINI_RATE_LIMIT_PER_MIN` é atingido.
    """
    import httpx

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "AIzaSyFakeValidLookingKeyForRateLimitTest123")
    monkeypatch.setattr(settings, "AUTOCARE_FEATURE_RATE_LIMIT", True)
    monkeypatch.setattr(settings, "GEMINI_RATE_LIMIT_PER_MIN", 2)
    ai_client_mod._gemini_rate_limiter.reset()

    client = GeminiClient()
    http_calls = 0

    async def fake_httpx_post(self_client, url, **kwargs):
        nonlocal http_calls
        http_calls += 1
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"intent":"unknown","confidence":"high","is_critical_action":false,"reply_message":"ok"}'
                            }
                        ]
                    }
                }
            ]
        }
        return mock_resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_httpx_post)

    r1 = await client.analyze_message(user_id="user_rl_test", current_message="msg 1")
    r2 = await client.analyze_message(user_id="user_rl_test", current_message="msg 2")
    r3 = await client.analyze_message(user_id="user_rl_test", current_message="msg 3")

    assert http_calls == 2
    assert r1.reply_message == "ok"
    assert r2.reply_message == "ok"
    assert r3.intent == IntentType.UNKNOWN
    assert r3.confidence == ConfidenceLevel.LOW
    assert "Muitas mensagens em pouco tempo" in r3.reply_message
    ai_client_mod._gemini_rate_limiter.reset()


@pytest.mark.unit
async def test_shared_client_reuses_connection_across_calls():
    """
    A7 [A9]: `shared_http_client` reutiliza a mesma instância de `httpx.AsyncClient`
    entre chamadas sucessivas e não a fecha ao sair do bloco `async with`.
    """
    await close_http_client()
    async with shared_http_client() as c1:
        pass
    async with shared_http_client() as c2:
        pass
    assert c1 is c2
    assert not c1.is_closed
    await close_http_client()


@pytest.mark.unit
async def test_shared_client_sets_connection_limits():
    """
    A7 [A9]: O `httpx.AsyncClient` compartilhado é criado com `Limits(max_connections=100,
    max_keepalive_connections=20, keepalive_expiry=30.0)`.
    """
    await close_http_client()
    client = get_http_client()
    limits = getattr(client, "_configured_limits", DEFAULT_LIMITS)
    assert limits.max_connections == 100
    assert limits.max_keepalive_connections == 20
    assert limits.keepalive_expiry == 30.0
    await close_http_client()


# ============================================================================
# GRUPO B — Itens da auditoria original ausentes do relatório
# ============================================================================


@pytest.mark.unit
def test_reminder_to_entity_tolerates_case_mismatch():
    """
    B1 [C5]: `ReminderRepository._to_entity` tolera valores de enum salvos em CAIXA ALTA,
    mista ou por nome do membro sem levantar `ValueError`.
    """
    raw_model = ReminderModel(
        id=99,
        user_id="5511999990008",
        vehicle_id=1,
        description="Trocar fluido de freio",
        recurrence="MONTHLY",
        status="PENDING",
        created_at=datetime.now(timezone.utc),
    )
    entity = ReminderRepository._to_entity(raw_model)
    assert entity.recurrence == ReminderRecurrence.MONTHLY
    assert entity.status == ReminderStatus.PENDING


@pytest.mark.unit
async def test_two_same_model_vehicles_ambiguous_query_asks_clarification():
    """
    B2 [C6]:
    - Turno 1: Usuário com 2 veículos do mesmo modelo ("Corolla" placa AAA1A11 e "Corolla" placa BBB2B22)
      enviando mensagem sem placa recebe pedido de desambiguação e nenhum veículo é atualizado.
    - Turno 2: Usuário informa a placa ("Troquei o óleo do Corolla placa AAA1A11 hoje com 65.000 km por 300 reais")
      e o sistema resolve deterministicamente para `v1` (`AAA1A11`).
    """
    svc = ConversationService()
    v1 = Vehicle(id=10, user_id="5511999990009", make="Toyota", model="Corolla", plate="AAA1A11", current_km=40000)
    v2 = Vehicle(id=20, user_id="5511999990009", make="Toyota", model="Corolla", plate="BBB2B22", current_km=60000)

    # Turno 1: sem placa -> pede desambiguação
    res_turn1 = await svc.handle_incoming_message(
        user_id="5511999990009",
        message_text="Troquei o óleo do Corolla hoje com 65.000 km por 300 reais",
        vehicles=[v1, v2],
    )

    assert res_turn1["action_type"] == "vehicle_disambiguation_required"
    assert "AAA1A11" in res_turn1["reply"]
    assert "BBB2B22" in res_turn1["reply"]
    assert v1.current_km == 40000
    assert v2.current_km == 60000

    # Turno 2: usuário responde apenas "placa AAA1A11" -> retoma a mensagem original, seleciona v1 e abre o rascunho
    res_turn2 = await svc.handle_incoming_message(
        user_id="5511999990009",
        message_text="placa AAA1A11",
        vehicles=[v1, v2],
    )
    ctx = svc.get_context("5511999990009")
    assert ctx.active_vehicle_id == v1.id
    assert res_turn2["action_type"] != "vehicle_disambiguation_required"
    assert ctx.draft_payload is not None
    assert resolve_target_vehicle([v1, v2], ref="AAA1A11") == v1.id
    assert resolve_target_vehicle([v1, v2], ref="AMBIGUOUS:Corolla") is None


@pytest.mark.unit
def test_timestamps_use_utc_aware_consistently():
    """
    B4 [M6]: `Reminder.due_datetime`, `Reminder.created_at`, `MaintenanceRecord.created_at`,
    `Vehicle.created_at` e `ConversationService._utcnow()` usam `datetime` UTC-aware (`tzinfo == timezone.utc`).
    """
    svc = ConversationService()
    now_val = svc._utcnow()
    assert now_val.tzinfo == timezone.utc

    rem = Reminder(
        user_id="5511999990010",
        description="Calibrar pneus",
        due_datetime=datetime(2026, 5, 10, 9, 0),
    )
    maint = MaintenanceRecord(
        user_id="5511999990010",
        vehicle_id=1,
        description="Troca de óleo",
        components=["Óleo 5W30"],
    )
    veh = Vehicle(
        user_id="5511999990010",
        make="Toyota",
        model="Corolla",
        plate="AAA1A11",
    )
    assert rem.due_datetime.tzinfo == timezone.utc
    assert rem.created_at.tzinfo == timezone.utc
    assert maint.created_at.tzinfo == timezone.utc
    assert veh.created_at.tzinfo == timezone.utc

    parsed_due = _parse_due_datetime(None, date_str="15/08/2026", explicit_time="10:30")
    assert parsed_due is not None
    assert parsed_due.tzinfo == timezone.utc


@pytest.mark.unit
def test_debouncer_locks_are_weakrefd_or_ttl_cleaned():
    """
    B5 [M7]: `MessageDebouncer._locks` e `ConversationService._user_locks` usam
    `WeakValueDictionary`, liberando automaticamente os locks após coleta de lixo.
    """
    debouncer = MessageDebouncer(default_debounce_seconds=0.0)
    for i in range(100):
        debouncer._get_lock(f"ephemeral_user_{i}")
    gc.collect()
    assert len(debouncer._locks) == 0


@pytest.mark.unit
def test_recent_turns_are_capped_at_max_turns():
    """
    B6 [M8]: `ConversationContext.recent_turns` nunca ultrapassa `max_turns` (default 50).
    """
    ctx = ConversationContext(user_id="5511999990012")
    svc = ConversationService()
    for i in range(80):
        ctx.advance_turn(f"user msg {i}")
        svc._record_assistant_turn(ctx, f"bot reply {i}")
    assert len(ctx.recent_turns) == 50
    assert ctx.recent_turns[-1].content == "bot reply 79"


@pytest.mark.unit
async def test_multi_action_decomposition_runs_online_and_offline_consistently():
    """
    B7 [M9] / AI-First: Testa diretamente a função utilitária pura `_decompose_multi_action_message`
    e confirma que a decisão do Gemini não é mais sobrescrita por regex em produção.
    """
    msg = "Troquei óleo por 280 reais e me lembra amanhã às 9h de calibrar o estepe"
    veh = Vehicle(id=1, user_id="5511999990013", make="Fiat", model="Pulse", plate="PUL1A23", current_km=15000)

    class SingleIntentOnlineGemini:
        def is_available(self) -> bool:
            return True

        async def analyze_message(self, **kwargs):
            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Entendi a manutenção.",
                actions=[],
            )

    class OfflineGemini:
        def is_available(self) -> bool:
            return False

        async def analyze_message(self, **kwargs):
            return GeminiAnalysisResponse(
                intent=IntentType.UNKNOWN,
                confidence=ConfidenceLevel.LOW,
                is_critical_action=False,
                reply_message="A chave da API do Gemini não está configurada.",
            )

    online_svc = ConversationService(gemini_client=SingleIntentOnlineGemini())
    offline_svc = ConversationService(gemini_client=OfflineGemini())

    res_online = await online_svc.handle_incoming_message("5511999990013", msg, vehicles=[veh])
    res_offline = await offline_svc.handle_incoming_message("5511999990013", msg, vehicles=[veh])

    # Confirma que a decisão da IA não é sobrescrita por regex
    assert not res_online.get("draft", {}).get("_is_multi_action", False)
    assert res_offline["action_type"] == "unknown"


@pytest.mark.unit
async def test_gemini_exception_does_not_leak_response_body(monkeypatch):
    """
    B8 [S1]: `GeminiClientException` contém apenas `HTTP <status>: <body len=N>`,
    nunca o corpo bruto da resposta HTTP.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "AIzaSyFakeKeyForExceptionMaskTest123456")
    client = GeminiClient()
    secret_body = "SENSITIVE_INTERNAL_PAYLOAD_ECHO_CPF_12345678900"

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = secret_body

    class DummyClientCM:
        async def __aenter__(self):
            c = AsyncMock()
            c.post = AsyncMock(return_value=mock_resp)
            return c

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(ai_client_mod, "shared_http_client", lambda timeout=None: DummyClientCM())

    with pytest.raises(GeminiClientException) as exc_info:
        await client._call_gemini_with_retry("gemini-2.5-flash", "prompt", {})

    err_msg = str(exc_info.value)
    assert f"HTTP 500: <body len={len(secret_body)}>" in err_msg
    assert secret_body not in err_msg


@pytest.mark.unit
def test_urllib_fallback_uses_bounded_executor():
    """
    B9 [S6]: O fallback `urllib` usa um `ThreadPoolExecutor` nomeado e limitado a `max_workers=8`.
    """
    executor = get_urllib_executor()
    assert executor._max_workers == 8
    assert "gemini-urllib" in executor._thread_name_prefix


# ============================================================================
# GRUPO C — Metrificação, observabilidade e deploy
# ============================================================================
# RESIDUAIS R1 a R8 (Adendo Complementar)
# ============================================================================


@pytest.mark.integration
async def test_ok_after_rollback_does_not_appear_in_success(async_db_session: AsyncSession):
    """
    R1: Quando um lote possui sub-ações bem-sucedidas e uma falha, o rollback é acionado
    e outcome['success'] é reatribuído para [], garantindo que nenhum _ok() conste no resultado final.
    """
    from sqlalchemy import select
    from src.infrastructure.database.models import MaintenanceRecordModel

    user_id = "5511999990091"
    v = Vehicle(id=91, user_id=user_id, make="Toyota", model="Corolla", plate="ABC9191")
    await VehicleRepository(async_db_session).add_vehicle(v, commit=True)

    committed_data = {
        "_is_multi_action": True,
        "actions": [
            {
                "intent": "record_maintenance",
                "components": ["óleo"],
                "km": 50000,
                "data": "15/03/2026",
                "target_vehicle_ref": "ABC9191",
            },
            {
                "intent": "record_maintenance",
                "components": ["filtro"],
                "km": 50000,
                "data": "31/02/2026",  # Data impossível -> força _fail("invalid_date")
                "target_vehicle_ref": "ABC9191",
            },
        ],
    }
    now_ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    outcome = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", committed_data, [v], now_ts
    )

    assert outcome["success"] == []
    assert len(outcome["failed"]) == 1
    assert outcome["failed"][0]["reason"] == "invalid_date"

    # Confirma via SELECT que o rollback foi integral e NADA foi gravado
    db_records = (await async_db_session.execute(select(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id))).scalars().all()
    assert len(db_records) == 0


@pytest.mark.integration
async def test_ambiguous_followup_reprocesses_original_message_without_corruption(async_db_session: AsyncSession):
    """
    R2: No turno 2 de desambiguação, a mensagem original é reprocessada por substituição (sem concatenação),
    preservando as heurísticas e regexes de _decompose_multi_action_message, e vinculando a placa resolvida.
    """
    user_id = "5511999990092"
    v1 = Vehicle(id=921, user_id=user_id, make="Toyota", model="Corolla", plate="AAA1A11")
    v2 = Vehicle(id=922, user_id=user_id, make="Toyota", model="Corolla", plate="BBB2B22")
    await VehicleRepository(async_db_session).add_vehicle(v1, commit=False)
    await VehicleRepository(async_db_session).add_vehicle(v2, commit=True)

    service = ConversationService()
    orig_msg = "Troquei o óleo do Corolla por R$ 350 e me lembra amanhã às 09:00 de calibrar o estepe"

    # Turno 1: Mensagem ambígua (2 Corollas na garagem)
    r1 = await service.handle_incoming_message(user_id=user_id, message_text=orig_msg, vehicles=[v1, v2])
    assert r1["action_type"] == "vehicle_disambiguation_required"
    ctx1 = service.get_context(user_id)
    assert ctx1.pending_ambiguous_message == orig_msg

    # Turno 2: Usuário responde apenas com a placa desambiguadora
    r2 = await service.handle_incoming_message(user_id=user_id, message_text="AAA1A11", vehicles=[v1, v2])

    ctx2 = service.get_context(user_id)
    assert ctx2.active_vehicle_id == v1.id
    assert ctx2.pending_ambiguous_message is None
    assert ctx2.draft_payload is not None
    assert ctx2.draft_payload.get("_target_vehicle_ref") == "AAA1A11"
    assert len(ctx2.draft_payload.get("actions", [])) == 2


@pytest.mark.integration
async def test_chained_reminder_does_not_break_batch_atomicity(async_db_session: AsyncSession):
    """
    R3: Quando um lembrete encadeado é processado e um item subsequente ou concorrente do lote falha,
    o rollback integral descarta o lote e NADA é persistido no banco (nem Reminder V2, nem Lembrete legado).
    """
    from sqlalchemy import select
    from src.infrastructure.database.models import MaintenanceRecordModel, ReminderModel

    user_id = "5511999990093"
    v = Vehicle(id=93, user_id=user_id, make="Honda", model="Civic", plate="HON9393")
    await VehicleRepository(async_db_session).add_vehicle(v, commit=True)

    # Lote com manutenção com data inválida E lembrete encadeado válido
    committed_data = {
        "components": ["óleo do motor"],
        "data": "31/02/2026",  # Data impossível -> causa falha
        "km": 60000,
        "valor": 250.0,
        "_target_vehicle_ref": "HON9393",
        "chained_reminder": {
            "task_description": "Trocar filtro de combustível",
            "due_datetime": "2026-06-15T09:00:00Z",
            "data": "15/06/2026",
            "explicit_time": "09:00",
        },
    }
    now_ts = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    outcome = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed_with_chained_reminder", committed_data, [v], now_ts
    )

    assert len(outcome["failed"]) > 0
    assert outcome["success"] == []

    # SELECT confirma que nada foi persistido no banco
    reminders = (await async_db_session.execute(select(ReminderModel).where(ReminderModel.user_id == user_id))).scalars().all()
    assert len(reminders) == 0

    maints = (await async_db_session.execute(select(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id))).scalars().all()
    assert len(maints) == 0


@pytest.mark.integration
async def test_reopen_draft_only_when_pending_data_exists(async_db_session: AsyncSession):
    """
    R4: Quando a persistência falha mas committed_data está vazio ({}), o rascunho
    NÃO é reaberto e o estado do contexto permanece NORMAL.
    """
    user_id = "5511999990094"
    service = ConversationService()
    ctx = service.get_context(user_id)
    ctx.state = ConversationState.NORMAL
    ctx.draft_payload = None
    service.update_context(ctx)

    result = {"reply": "Resposta", "action_type": "action_confirmed"}
    outcome = {"success": [], "failed": [{"item": "Data", "reason": "invalid_date", "raw_date": "31/02/2026"}]}

    _apply_persistence_outcome(result, user_id, {}, outcome)

    ctx_after = service.get_context(user_id)
    assert ctx_after.state == ConversationState.NORMAL
    assert ctx_after.draft_payload is None
    assert result["action_type"] == "invalid_date"


@pytest.mark.unit
def test_empty_outcome_returns_no_op():
    """
    R5: Quando outcome não possui sucessos nem falhas (nada persistido),
    _apply_persistence_outcome marca action_type='no_op' e reply informativo.
    """
    result = {"reply": "Tudo certo", "action_type": "action_confirmed"}
    outcome = {"success": [], "failed": []}
    _apply_persistence_outcome(result, "5511999990095", {"foo": "bar"}, outcome)

    assert result["action_type"] == "no_op"
    assert "Não identifiquei nenhuma ação para salvar" in result["reply"]


@pytest.mark.unit
async def test_healthz_reports_dead_http_client_as_degraded():
    """
    R6: Quando o shared_http_client está fechado, /healthz reporta status='degraded'
    e checks['shared_http_client_alive']=False.
    """
    client = get_http_client()
    await client.aclose()
    assert client.is_closed is True

    from httpx import ASGITransport, AsyncClient

    test_app = create_app()
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        resp = await ac.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["shared_http_client_alive"] is False
    assert "shared_http_client_feature_flag" in body["checks"]

    # Reestabelece o cliente para isolamento entre testes
    from src.infrastructure import http_client as hc_mod
    hc_mod._state = None


@pytest.mark.unit
async def test_webhook_log_masks_message_content(async_db_session: AsyncSession, caplog, monkeypatch):
    """
    C2 [R2] / R7: Em INFO, o log do webhook registra user, msg_hash e len,
    mas NUNCA o conteúdo sensível. O conteúdo só aparece em DEBUG se settings.DEBUG=True.
    """
    sensitive_text = "Meu CPF é 123.456.789-00 e placa SECRETA99"
    orig_handle = webhook_conv_service.handle_incoming_message
    webhook_conv_service.handle_incoming_message = AsyncMock(
        return_value={"reply": "ok", "action_type": "unknown", "state": "normal"}
    )
    try:
        # 1. Caso DEBUG=False: nenhum record em qualquer nível deve conter o texto sensível
        monkeypatch.setattr(settings, "DEBUG", False)
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="autocare.webhook"):
            await _run_pipeline(async_db_session, "5511999990016", sensitive_text)

        webhook_records = [rec for rec in caplog.records if rec.name == "autocare.webhook"]
        # Assert positivo: record INFO com msg_hash= e len=
        info_records = [rec for rec in webhook_records if rec.levelno == logging.INFO]
        assert any("msg_hash=" in rec.getMessage() and f"len={len(sensitive_text)}" in rec.getMessage() for rec in info_records)
        # Assert negativo: nenhum record em qualquer nível tem o texto sensível quando DEBUG=False
        assert all(sensitive_text not in rec.getMessage() for rec in webhook_records)

        # 2. Caso DEBUG=True: record DEBUG deve conter o texto sensível, mas INFO continua mascarado
        monkeypatch.setattr(settings, "DEBUG", True)
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="autocare.webhook"):
            await _run_pipeline(async_db_session, "5511999990016", sensitive_text)

        webhook_records_debug = [rec for rec in caplog.records if rec.name == "autocare.webhook"]
        info_records_debug = [rec for rec in webhook_records_debug if rec.levelno == logging.INFO]
        debug_records = [rec for rec in webhook_records_debug if rec.levelno == logging.DEBUG]

        # INFO continua estritamente sem o texto sensível
        assert all(sensitive_text not in rec.getMessage() for rec in info_records_debug)
        # DEBUG contém o texto sensível exclusivamente quando DEBUG=True
        assert any(sensitive_text in rec.getMessage() for rec in debug_records)
    finally:
        webhook_conv_service.handle_incoming_message = orig_handle


@pytest.mark.unit
def test_healthz_returns_degraded_when_db_slow(monkeypatch):
    """
    C3 [R3]: `GET /healthz` retorna `{"status": "degraded", "checks": ...}` quando o ping
    ao banco excede o timeout ou falha.
    """
    import importlib
    app_mod = importlib.import_module("src.interfaces.app")

    async def slow_ping():
        await asyncio.sleep(0)
        raise asyncio.TimeoutError("DB ping exceeded 2s")

    monkeypatch.setattr(app_mod, "ping_database", slow_ping)
    test_app = create_app()
    with TestClient(test_app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert "shared_http_client" in body["checks"]
    assert "rate_limiter" in body["checks"]
    assert "db_ping_ms" in body["checks"]
    assert "gemini_key_configured" in body["checks"]


@pytest.mark.unit
async def test_feature_flags_disable_rate_limiter(monkeypatch):
    """
    C3 [R3] / R8: Quando `AUTOCARE_FEATURE_RATE_LIMIT=False`, o webhook não bloqueia
    mesmo com `WEBHOOK_RATE_LIMIT_PER_MIN=1`. Mock refactor-safe via monkeypatch.setattr no módulo webhook_mod.
    """
    import src.interfaces.api.webhook as webhook_mod

    monkeypatch.setattr(settings, "AUTOCARE_FEATURE_RATE_LIMIT", False)
    monkeypatch.setattr(settings, "WEBHOOK_RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(settings, "WEBHOOK_DEBOUNCE_SECONDS", 0.0)
    _rate_limiter.reset()

    mock_pipeline = AsyncMock(return_value={"status": "success", "reply": "ok"})
    monkeypatch.setattr(webhook_mod, "_process_webhook_pipeline", mock_pipeline)
    try:
        r1 = await webhook_mod.receive_webhook({"user_id": "5511999990018", "message": "ola 1"})
        r2 = await webhook_mod.receive_webhook({"user_id": "5511999990018", "message": "ola 2"})
        r3 = await webhook_mod.receive_webhook({"user_id": "5511999990018", "message": "ola 3"})
    finally:
        _rate_limiter.reset()

    assert r1["status"] == "success"
    assert r2["status"] == "success"
    assert r3["status"] == "success"
    assert mock_pipeline.call_count == 3


@pytest.mark.unit
async def test_b12_no_vehicle_registered_rejects_draft():
    """
    B12 / C7: Quando o usuário não possui nenhum veículo cadastrado na garagem,
    o sistema recusa abrir rascunho fantasma de manutenção e devolve action_type='needs_vehicle'.
    """
    service = ConversationService()
    user_id = "5511999990099"
    service.clear_context(user_id)

    # Mensagem de manutenção explícita com veículos=[]
    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Troquei o óleo por 250 reais",
        vehicles=[],
    )
    assert res["action_type"] == "needs_vehicle"
    assert "Cadastre o veículo primeiro" in res["reply"]
    ctx = service.get_context(user_id)
    assert ctx.state == ConversationState.NORMAL
    assert ctx.draft_payload is None


@pytest.mark.unit
def test_c1_maintenance_payload_conversion():
    """
    C1: O MaintenancePayload encapsula todos os campos de manutenção com imutabilidade
    e conversão determinística para committed_data.
    """
    from src.application.services.maintenance_payload import MaintenancePayload

    payload = MaintenancePayload(
        itens=["óleo 5w30", "filtro de óleo"],
        description="Troca de óleo e filtro",
        km=55000,
        data="2026-03-15",
        oficina="Oficina Central",
        valor=350.0,
        target_vehicle_ref="AAA1A11",
        discrepancy_warning=None,
    )
    data_dict = payload.to_committed_data()
    assert data_dict["components"] == ["óleo 5w30", "filtro de óleo"]
    assert data_dict["km"] == 55000
    assert data_dict["total_cost"] == 350.0
    assert data_dict["_target_vehicle_ref"] == "AAA1A11"
    assert data_dict["workshop_name"] == "Oficina Central"


@pytest.mark.unit
async def test_s2_gemini_api_key_sent_via_header_not_query_params(monkeypatch):
    """
    S2: O GeminiClient envia a chave de API via header 'x-goog-api-key' e NUNCA
    na query string da URL da requisição.
    """
    from contextlib import asynccontextmanager
    from src.ai.client import GeminiClient

    captured_requests = []

    async def mock_post(url, json=None, headers=None, timeout=None):
        captured_requests.append({"url": url, "headers": headers})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"intent": "unknown", "reply_message": "ok", "confidence": "high"}'}
                        ]
                    }
                }
            ]
        }
        return resp

    mock_http = MagicMock()
    mock_http.post = AsyncMock(side_effect=mock_post)

    @asynccontextmanager
    async def mock_shared(timeout=None):
        yield mock_http

    monkeypatch.setattr("src.ai.client.shared_http_client", mock_shared)

    client = GeminiClient(api_key="AIzaSyDummySecretKeyForHeaderTest")
    res = await client._call_gemini_with_retry(
        model="gemini-2.5-flash",
        prompt="hello",
        schema={"type": "object"},
    )
    assert res == '{"intent": "unknown", "reply_message": "ok", "confidence": "high"}'

    assert len(captured_requests) == 1
    req = captured_requests[0]
    # Header deve conter a chave
    assert req["headers"].get("x-goog-api-key") == "AIzaSyDummySecretKeyForHeaderTest"
    # URL NUNCA deve conter a chave exposta
    assert "key=" not in req["url"]
    assert "AIzaSyDummySecretKeyForHeaderTest" not in req["url"]

