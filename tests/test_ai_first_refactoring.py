"""
Suíte de Testes E2E e Unitários para o PLANO DE REFATORAÇÃO COMPLETO - AUTOCARE
(Arquitetura AI-First, Fluxo e Latência - Etapas 1, 2, 3 e 4).
"""
import pytest
from datetime import datetime
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from src.interfaces.app import create_app
from src.infrastructure.database.session import init_db, async_session_factory
from src.infrastructure.database.models import MaintenanceRecordModel
from src.application.services.conversation import ConversationService, ConversationState
from src.domain.enums import ConfidenceLevel
from src.ai.guardrails import MAX_HISTORY_TURNS
from src.ai.prompts import (
    SYSTEM_INSTRUCTION,
    build_analysis_prompt,
    build_draft_atual_dict,
    build_ultimo_servico_salvo_dict,
)
from src.ai.schemas import GeminiAnalysisResponse, ToolCallAction
import src.interfaces.api.webhook as webhook_module

app = create_app()


@pytest.fixture(autouse=True)
def _disable_live_gemini_network_in_tests(monkeypatch):
    """Garante execução rápida e determinística sem depender de quota externa do Gemini."""
    monkeypatch.setattr(webhook_module.conversation_service.gemini, "client", None, raising=False)
    monkeypatch.setattr(webhook_module.conversation_service.gemini, "api_key", None, raising=False)


@pytest.mark.asyncio
async def test_etapa1_invisible_state_injection_in_prompt():
    """
    ETAPA 1: Verifica que o estado invisível (`draft_atual` e `ultimo_servico_salvo`)
    é serializado e injetado no prompt enviado ao LLM a cada mensagem.
    """
    service = ConversationService()
    ctx = service.get_context("5511999990001")
    ctx.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
    ctx.draft_payload = {
        "itens": ["Troca de óleo", "Filtro de óleo"],
        "km": 52000,
        "data": "15/08/2026",
        "oficina": "Auto Center Brasil",
        "valor": 350.0,
        "_target_vehicle_ref": "Nivus",
    }

    class DummyMaint:
        id = "srv-999-abc"
        date = datetime(2026, 8, 10, 12, 0, 0)
        km = 50000
        components = ["Pastilha de freio", "Fluido de freio"]
        description = "Pastilha de freio, Fluido de freio"
        workshop_name = "OficinaFreios"
        total_cost = 480.0
        status = "active"

    draft_dict = build_draft_atual_dict(ctx)
    assert draft_dict is not None
    assert draft_dict["itens"] == ["Troca de óleo", "Filtro de óleo"]
    assert draft_dict["km"] == 52000
    assert draft_dict["data"] == "15/08/2026"

    last_srv_dict = build_ultimo_servico_salvo_dict([DummyMaint()])
    assert last_srv_dict is not None
    assert last_srv_dict["service_id"] == "srv-999-abc"
    assert last_srv_dict["data"] == "10/08/2026"
    assert last_srv_dict["km"] == 50000
    assert "Pastilha de freio" in last_srv_dict["itens"]

    prompt = build_analysis_prompt(
        current_message="ah, também troquei o filtro de combustível",
        history=[{"role": "user", "content": "Troquei o óleo com 52000 km"}],
        context=ctx,
        vehicles=[],
        maintenances=[DummyMaint()],
    )
    assert '"draft_atual":' in prompt
    assert "Troca de óleo" in prompt
    assert '"ultimo_servico_salvo":' in prompt
    assert "srv-999-abc" in prompt


@pytest.mark.asyncio
async def test_etapa2_and_3_regra1_anti_hallucination_vague_service():
    """
    ETAPA 3 (Regra 1 — Proibição de Inferência Cega / Anti-Alucinação):
    Frases vagas de início ('informa um serviço que fiz anteriormente', 'fiz uma manutenção')
    NÃO devem criar rascunho nem inventar 'vistoria'/'revisão', km ou data.
    Devem responder: 'Beleza! Me conta qual serviço você fez, e se lembrar, a data e a quilometragem.'
    """
    service = ConversationService()
    service.gemini.client = None
    service.gemini.api_key = None
    user_id = "5511999990002"

    for phrase in [
        "informa um serviço que fiz anteriormente",
        "fiz uma manutenção",
        "quero registrar um serviço que fiz",
    ]:
        res = await service.handle_incoming_message(
            user_id=user_id,
            message_text=phrase,
            vehicles=[],
            maintenances=[],
        )
        assert res["state"] == ConversationState.NORMAL.value
        assert service.get_context(user_id).draft_payload is None
        assert "Beleza! Me conta qual serviço você fez, e se lembrar, a data e a quilometragem." in res["reply"]
        assert "vistoria" not in res["reply"].lower()


@pytest.mark.asyncio
async def test_etapa2_and_3_regra2_cumulative_upsert_draft_and_confirm_e2e():
    """
    ETAPA 2 (`upsert_draft` e `confirm_and_save_draft`) & ETAPA 3 (Regra 2 — Atualização Cumulativa antes da Confirmação):
    1. Usuário inicia manutenção ('Troquei o óleo do Nivus com 45000 km no dia 10/08/2026 por 300 reais').
    2. Sistema gera rascunho pendente e pergunta 'Posso confirmar e salvar?'.
    3. Em vez de dizer apenas 'Sim', usuário diz: 'ah, também troquei o filtro de combustível'.
    4. Sistema mescla os itens ('Troca de óleo' + 'Filtro de combustível') no `draft_atual`,
       mostra o card atualizado e mantém o status pendente de confirmação.
    5. Usuário diz 'pode salvar' -> `confirm_and_save_draft` persiste ambos os itens no banco e limpa `draft_atual`.
    """
    await init_db()
    user_id = "5511988887701"
    webhook_module.conversation_service.clear_context(user_id)
    async with async_session_factory() as session:
        from src.infrastructure.database.repositories import UserRepository
        await UserRepository(session).reset_user_data(user_id)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Cadastra um veículo primeiro
        await client.post("/webhook", json={"user_id": user_id, "message": "Comprei um Nivus Highline 2024 placa RJA1B23 com 40000 km"})
        await client.post("/webhook", json={"user_id": user_id, "message": "pode salvar"})

        # 1. Inicia rascunho de manutenção
        r1 = await client.post(
            "/webhook",
            json={
                "user_id": user_id,
                "message": "Troquei o óleo do Nivus com 45000 km em 10/08/2026 por 300 reais",
            },
        )
        data1 = r1.json()
        assert data1["state"] == "awaiting_maintenance_confirm"
        assert "óleo" in data1["reply"].lower()

        # 2. Adição cumulativa antes da confirmação!
        r2 = await client.post(
            "/webhook",
            json={
                "user_id": user_id,
                "message": "ah, também troquei o filtro de combustível",
            },
        )
        data2 = r2.json()
        assert data2["state"] == "awaiting_maintenance_confirm"
        reply2_lower = data2["reply"].lower()
        assert "óleo" in reply2_lower
        assert "filtro de combustível" in reply2_lower
        assert "posso confirmar" in reply2_lower or "posso salvar" in reply2_lower

        # 3. Confirmação final (`confirm_and_save_draft`)
        r3 = await client.post(
            "/webhook",
            json={"user_id": user_id, "message": "sim, pode salvar"},
        )
        data3 = r3.json()
        assert data3["state"] == "normal"
        assert data3["action_type"] == "action_confirmed"

    # Verifica persistência no banco de dados
    async with async_session_factory() as session:
        stmt = select(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id)
        records = list((await session.execute(stmt)).scalars().all())
        assert len(records) == 1
        saved = records[0]
        comps_lower = " ".join(saved.components or []).lower()
        assert "óleo" in comps_lower
        assert "filtro de combustível" in comps_lower
        assert saved.km == 45000
        assert saved.date.strftime("%d/%m/%Y") == "10/08/2026"


@pytest.mark.asyncio
async def test_etapa2_and_3_regra3_active_edit_last_saved_service_e2e():
    """
    ETAPA 2 (`update_last_saved_service`) & ETAPA 3 (Regra 3 — Condução Ativa na Edição de Registros):
    Cenário A (Condução Ativa):
      - Usuário diz: 'quero alterar o último serviço, a data estava errada'
      - Bot NÃO exibe relatório passivo de histórico; pergunta diretamente:
        'Sem problemas! A data atual está registrada como [DATA]. Qual é a data correta?'
      - Usuário responde: '18/08/2026' -> Atualiza o registro diretamente no banco!
    Cenário B (Edio Completa na mesma frase):
      - Usuário diz: 'quero adicionar filtro de combustível no último serviço e mudar a data para 20/08/2026'
      - Bot executa `update_last_saved_service` imediatamente, mescla o item e muda a data no banco!
    """
    await init_db()
    user_id = "5511988887702"
    webhook_module.conversation_service.clear_context(user_id)
    async with async_session_factory() as session:
        from src.infrastructure.database.repositories import UserRepository
        await UserRepository(session).reset_user_data(user_id)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Cadastra veículo e salva uma manutenção inicial
        await client.post("/webhook", json={"user_id": user_id, "message": "Tenho um Corolla XEI 2023 placa ABC1D23 com 30000 km"})
        await client.post("/webhook", json={"user_id": user_id, "message": "sim"})

        await client.post("/webhook", json={"user_id": user_id, "message": "Troquei o óleo do Corolla com 32000 km em 10/08/2026 por 280 reais"})
        await client.post("/webhook", json={"user_id": user_id, "message": "pode salvar"})

        # Cenário A: Usuário pede para alterar o último serviço dizendo apenas que a data estava errada
        r_edit1 = await client.post(
            "/webhook",
            json={
                "user_id": user_id,
                "message": "quero alterar o último serviço, a data estava errada",
            },
        )
        d_edit1 = r_edit1.json()
        assert d_edit1["action_type"] == "awaiting_edit_value"
        assert "Sem problemas! A data atual está registrada como 10/08/2026. Qual é a data correta?" in d_edit1["reply"]

        # Usuário responde apenas com a nova data
        r_edit2 = await client.post(
            "/webhook",
            json={"user_id": user_id, "message": "18/08/2026"},
        )
        d_edit2 = r_edit2.json()
        assert d_edit2["action_type"] == "update_last_saved_service"
        assert "18/08/2026" in d_edit2["reply"]

        # Cenário B: Usuário já fornece a correção completa na frase (adicionar item + mudar data)
        r_edit3 = await client.post(
            "/webhook",
            json={
                "user_id": user_id,
                "message": "quero adicionar filtro de combustível no último serviço e mudar a data para 20/08/2026",
            },
        )
        d_edit3 = r_edit3.json()
        assert d_edit3["action_type"] == "update_last_saved_service"
        assert "20/08/2026" in d_edit3["reply"]
        assert "filtro de combustível" in d_edit3["reply"].lower()

    # Verifica que o registro no banco foi atualizado (sem duplicar registros)
    async with async_session_factory() as session:
        stmt = select(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id)
        records = list((await session.execute(stmt)).scalars().all())
        assert len(records) == 1
        updated = records[0]
        assert updated.date.strftime("%d/%m/%Y") == "20/08/2026"
        comps_lower = " ".join(updated.components or []).lower()
        assert "óleo" in comps_lower
        assert "filtro de combustível" in comps_lower


@pytest.mark.asyncio
async def test_etapa2_get_service_history_pure_query_only():
    """
    ETAPA 2 (`get_service_history`) & ETAPA 4:
    Verifica que `get_service_history` é acionada apenas quando a intenção for puramente consultar
    o que já foi feito, e que a poda de histórico e configuração de latência estão ativas.
    """
    assert MAX_HISTORY_TURNS <= 7
    assert "upsert_draft" in SYSTEM_INSTRUCTION
    assert "confirm_and_save_draft" in SYSTEM_INSTRUCTION
    assert "update_last_saved_service" in SYSTEM_INSTRUCTION
    assert "get_service_history" in SYSTEM_INSTRUCTION

    # Testa sincronização do ToolCallAction no schema GeminiAnalysisResponse
    resp = GeminiAnalysisResponse(
        intent="unknown",
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=False,
        reply_message="Consultando histórico",
        tool_call=ToolCallAction(name="get_service_history"),
    )
    assert resp.intent.value == "query_history"


@pytest.mark.asyncio
async def test_upsert_draft_deep_merge_preserves_unmentioned_fields():
    """
    CORREÇÃO CRÍTICA 1: Deep Merge em upsert_draft
    Ao receber um item adicional durante AWAITING_MAINTENANCE_CONFIRM,
    os novos itens são adicionados (appended) e os campos km, data, oficina e valor
    já existentes no rascunho são estritamente preservados (não zerados e data não vira hoje).
    """
    service = ConversationService()
    user_id = "5511999998888"
    ctx = service.get_context(user_id)
    ctx.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
    ctx.draft_payload = {
        "itens": ["óleo do motor"],
        "components": ["óleo do motor"],
        "km": 42000,
        "odometer_km": 42000,
        "data": "15/09/2026",
        "date_str": "15/09/2026",
        "oficina": "Oficina do Zé",
        "workshop_name": "Oficina do Zé",
        "valor": 250.0,
        "total_cost": 250.0,
        "_target_vehicle_ref": "Palio",
    }

    # Simula IA retornando upsert_draft apenas com o item novo
    mock_analysis = GeminiAnalysisResponse(
        intent="record_maintenance",
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=True,
        reply_message="Atualizando rascunho",
        tool_call=ToolCallAction(name="upsert_draft", itens=["filtro de combustível"]),
    )

    class MockGemini:
        is_available = lambda self: True
        async def analyze_message(self, *args, **kwargs):
            return mock_analysis

    service.gemini = MockGemini()

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="também troquei filtro de combustível",
        vehicles=[],
        maintenances=[],
    )

    assert res["state"] == ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
    draft = ctx.draft_payload
    assert draft is not None
    # Verifica que itens foram mesclados
    assert "óleo do motor" in draft["itens"]
    assert "filtro de combustível" in draft["itens"]
    # Verifica que km foi preservado (não virou nulo nem 0)
    assert draft["km"] == 42000
    assert draft["odometer_km"] == 42000
    # Verifica que data foi preservada (não virou hoje)
    assert draft["data"] == "15/09/2026"
    assert draft["date_str"] == "15/09/2026"
    # Verifica que oficina e valor foram preservados
    assert draft["oficina"] == "Oficina do Zé"
    assert draft["valor"] == 250.0
    # Verifica que o reply_message do Gemini foi preservado intacto (sem sobrescrita determinística)
    assert res["reply"] == "Atualizando rascunho"


@pytest.mark.asyncio
async def test_edit_last_saved_service_anti_hallucination_and_routing():
    """
    CORREÇÃO CRÍTICA 2: Roteamento de Edição e Anti-Alucinação
    Ao receber 'quero corrigir o km desse último serviço', o sistema DEVE acionar a condução
    ativa de update_last_saved_service e NUNCA alucinar upsert_draft com 'Revisão geral periódica'.
    """
    service = ConversationService()
    user_id = "5511999997777"
    ctx = service.get_context(user_id)
    ctx.state = ConversationState.NORMAL
    ctx.draft_payload = None

    class SavedMaint:
        id = 205
        date = datetime(2026, 8, 15, 10, 0, 0)
        km = 140000
        odometro_registro = 140000
        components = ["troca de óleo"]
        description = "troca de óleo"
        total_cost = 200.0
        status = "active"
        workshop_name = "AutoFix"

    # Turno 1: Usuário pede para corrigir o km do último serviço
    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="quero corrigir o km desse último serviço",
        vehicles=[],
        maintenances=[SavedMaint()],
    )

    # Verifica que NÃO criou rascunho de manutenção com alucinação
    assert ctx.draft_payload is None
    assert "Revisão geral periódica" not in res1["reply"]
    assert "revisão geral" not in res1["reply"].lower()
    # Verifica que perguntou ativamente pelo novo km consultando o último serviço
    assert res1["action_type"] == "awaiting_edit_value"
    assert "Sem problemas! A quilometragem atual está registrada como 140.000 km. Qual é a quilometragem correta?" in res1["reply"]
    assert ctx.pending_edit_field == "km"
    assert ctx.pending_edit_service_id == 205

    # Turno 2: Usuário informa a nova quilometragem
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="145.000 km",
        vehicles=[],
        maintenances=[SavedMaint()],
    )
    assert res2["action_type"] == "update_last_saved_service"
    assert res2["committed_data"]["service_id"] == 205
    assert res2["committed_data"]["km"] == 145000
    assert "145.000 km" in res2["reply"]
    assert ctx.pending_edit_field is None


def test_prompts_anti_hallucination_and_onboarding_rules():
    """Valida que o SYSTEM_INSTRUCTION contém as regras mestras de anti-alucinação e onboarding."""
    # 1. Anti-Alucinação de Especificações
    assert "NUNCA presuma ou preencha automaticamente especificações opcionais" in SYSTEM_INSTRUCTION
    assert "Palio 2008" in SYSTEM_INSTRUCTION
    # 2. Obrigatoriedade do Odômetro no Onboarding
    assert "Durante o cadastro de um novo veículo, é OBRIGATÓRIO solicitar a quilometragem" in SYSTEM_INSTRUCTION
    # 3. Gatilho de Manutenções Pendentes
    assert "Deseja registrar o histórico de alguma manutenção que já foi feita ou relatar algum serviço pendente?" in SYSTEM_INSTRUCTION
    # 4. Correlação Cronológica
    assert "É PERMITIDO registrar uma manutenção com quilometragem inferior à atual" in SYSTEM_INSTRUCTION


@pytest.mark.asyncio
async def test_vehicle_registration_confirmation_triggers_maintenance_engagement():
    """Valida que o pós-cadastro engaja o motorista perguntando sobre manutenções passadas/pendentes."""
    service = ConversationService()
    user_id = "5511999990099"
    ctx = service.get_context(user_id)
    ctx.state = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
    ctx.draft_payload = {
        "make": "Volkswagen",
        "model": "Nivus",
        "plate": "RJA1B23",
        "engine": "1.0 Turbo",
        "odometer_km": 15000,
    }

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="sim, pode salvar",
        vehicles=[],
    )

    assert res["action_type"] == "action_confirmed"
    assert "Veículo cadastrado com sucesso na sua garagem!" in res["reply"]
    assert "Deseja registrar o histórico de alguma manutenção que já foi feita ou relatar algum serviço pendente?" in res["reply"]


@pytest.mark.asyncio
async def test_adversarial_chronological_correlation_and_relative_dates_sqlite():
    """
    Teste Adversarial Real (SQLite + _persist_confirmed_data):
    1. Manutenção retroativa no passado ('mês passado', 'há 1 ano', '10/01/2025') com KM menor que o atual (42.000 < 60.000)
       DEVE ser permitida, gravando a data retroativa real no banco e preservando current_km=60.000 no veículo.
    2. Manutenção na data de hoje ('hoje' ou sem data passada) com KM regressivo (42.000 < 60.000)
       DEVE ser bloqueada ('regressive_km') com rollback transacional no SQLite.
    """
    from datetime import timedelta, timezone
    from sqlalchemy import select
    from src.domain.entities import Vehicle
    from src.infrastructure.database.models import MaintenanceRecordModel
    from src.infrastructure.database.repositories import VehicleRepository, UserRepository
    from src.infrastructure.database.session import init_db, async_session_factory
    from src.interfaces.api.webhook import _persist_confirmed_data, _parse_maintenance_date

    await init_db()
    user_id = "5511999998801"
    now_ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    # 1. Valida parser de datas relativas misturadas com datas absolutas e datas impossíveis
    assert _parse_maintenance_date("mês passado", now_ts.date()) == now_ts.date() - timedelta(days=30)
    assert _parse_maintenance_date("há 1 ano", now_ts.date()) == now_ts.date() - timedelta(days=365)
    assert _parse_maintenance_date("há 3 meses", now_ts.date()) == now_ts.date() - timedelta(days=90)
    assert _parse_maintenance_date("semana passada", now_ts.date()) == now_ts.date() - timedelta(days=7)
    assert _parse_maintenance_date("10/01/2025", now_ts.date()).isoformat() == "2025-01-10"
    assert _parse_maintenance_date("31/02/2026", now_ts.date()) is None
    assert _parse_maintenance_date("31 de abril de 2026", now_ts.date()) is None

    async with async_session_factory() as async_db_session:
        await UserRepository(async_db_session).reset_user_data(user_id)
        await UserRepository(async_db_session).get_or_create(user_id=user_id)
        v_repo = VehicleRepository(async_db_session)
        veh = await v_repo.add_vehicle(
            Vehicle(user_id=user_id, make="Fiat", model="Toro", plate="ABC1D23", current_km=60000),
            commit=True,
        )

        # 2. Manutenção na data de hoje com KM regressivo (42.000 < 60.000) -> BLOQUEADA
        bad_today_payload = {
            "_action_state": "awaiting_maintenance_confirm",
            "vehicle_id": veh.id,
            "itens": ["pastilhas de freio"],
            "km": 42000,
            "data": "hoje",
            "valor": 380.0,
        }
        outcome_blocked = await _persist_confirmed_data(
            async_db_session, user_id, "action_confirmed", bad_today_payload, [veh], now_ts
        )
        assert outcome_blocked["success"] == []
        assert len(outcome_blocked["failed"]) == 1
        assert outcome_blocked["failed"][0]["reason"] == "regressive_km"

        rows_after_block = (
            await async_db_session.execute(select(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id))
        ).scalars().all()
        assert len(rows_after_block) == 0

        # 3. Manutenção retroativa ('há 1 ano') com KM menor que o atual (42.000 < 60.000) -> PERMITIDA
        retro_payload = {
            "_action_state": "awaiting_maintenance_confirm",
            "vehicle_id": veh.id,
            "itens": ["correia dentada", "tensor"],
            "km": 42000,
            "data": "há 1 ano",
            "oficina": "Oficina Central",
            "valor": 950.0,
        }
        outcome_ok = await _persist_confirmed_data(
            async_db_session, user_id, "action_confirmed", retro_payload, [veh], now_ts
        )
        assert len(outcome_ok["success"]) == 1
        assert outcome_ok["failed"] == []

        rows_after_ok = (
            await async_db_session.execute(select(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id))
        ).scalars().all()
        assert len(rows_after_ok) == 1
        assert rows_after_ok[0].km == 42000
        assert rows_after_ok[0].date.date() if hasattr(rows_after_ok[0].date, "date") else rows_after_ok[0].date == now_ts.date() - timedelta(days=365)

        # Odômetro atual do veículo permanece 60.000 km (não regrediu para 42.000 km)
        updated_veh = await v_repo.get_by_id(veh.id)
        assert updated_veh.current_km == 60000


@pytest.mark.asyncio
async def test_adversarial_plate_validation_and_corrective_negation_deep_merge():
    """
    Teste Adversarial Real:
    1. Negativas corretivas ('Não, o ano é 2008 e direção hidráulica') via upsert_draft atualizam o rascunho
       sem cancelar a operação e sem tocar nos campos já preenchidos, mantendo reply_message idêntico ao da IA.
    2. Validação estrita de placas no banco: aceita padrão antigo (ABC-1234) e Mercosul (BRA2E19),
       mas rejeita formatos inválidos ('123-ABCD', 'INVALID99') com rollback transacional.
    """
    from datetime import timedelta, timezone
    from src.domain.enums import IntentType
    from src.infrastructure.database.repositories import VehicleRepository, UserRepository
    from src.infrastructure.database.session import init_db, async_session_factory
    from src.interfaces.api.webhook import _persist_confirmed_data

    await init_db()
    service = ConversationService()
    user_id = "5511999998802"
    ctx = service.get_context(user_id)
    ctx.state = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
    ctx.draft_payload = {
        "make": "Fiat",
        "model": "Hatch",
        "year": 2010,
        "plate": "ABC1234",
        "odometer_km": 98500,
        "engine": "1.4",
        "transmission": "Manual",
    }

    arbitrary_ai_reply = "Entendido! Corrigi o ano para 2008 e a direção para Hidráulica [ID-9941-X]. Confirma?"
    corrective_analysis = GeminiAnalysisResponse(
        intent=IntentType.REGISTER_VEHICLE,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=True,
        reply_message=arbitrary_ai_reply,
        tool_call=ToolCallAction(name="upsert_draft", year=2008, steering="Hidráulica"),
    )

    class SingleTurnGemini:
        async def analyze_message(self, *args, **kwargs):
            return corrective_analysis

    service.gemini = SingleTurnGemini()
    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Não, o ano é 2008 e é direção hidráulica",
        vehicles=[],
    )

    # Não cancelou; atualizou via deep merge e manteve o texto exato da IA
    assert res["action_type"] == "awaiting_confirmation"
    assert res["state"] == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
    assert res["reply"] == arbitrary_ai_reply
    assert ctx.draft_payload["year"] == 2008
    assert ctx.draft_payload["steering"] == "Hidráulica"
    assert ctx.draft_payload["plate"] == "ABC1234"
    assert ctx.draft_payload["odometer_km"] == 98500
    assert ctx.draft_payload["engine"] == "1.4"

    async with async_session_factory() as async_db_session:
        await UserRepository(async_db_session).reset_user_data(user_id)
        await UserRepository(async_db_session).get_or_create(user_id=user_id)
        # Persistência com placa inválida deve falhar com 'invalid_plate' e fazer rollback
        now_ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        invalid_plate_data = {
            **ctx.draft_payload,
            "_action_state": ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value,
            "plate": "123-ABCD",
        }
        out_invalid = await _persist_confirmed_data(
            async_db_session, user_id, "action_confirmed", invalid_plate_data, [], now_ts
        )
        assert out_invalid["success"] == []
        assert out_invalid["failed"][0]["reason"] == "invalid_plate"
        assert len(await VehicleRepository(async_db_session).get_vehicles_by_user_id(user_id)) == 0

        # Persistência com placa Mercosul válida ('BRA2E19') deve gravar no SQLite
        valid_mercosul_data = {
            **ctx.draft_payload,
            "_action_state": ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value,
            "plate": "BRA-2E19",
        }
        out_valid = await _persist_confirmed_data(
            async_db_session, user_id, "action_confirmed", valid_mercosul_data, [], now_ts
        )
        assert len(out_valid["success"]) == 1
        saved_vehs = await VehicleRepository(async_db_session).get_vehicles_by_user_id(user_id)
        assert len(saved_vehs) == 1
        assert saved_vehs[0].plate == "BRA2E19"
        assert saved_vehs[0].year == 2008
        assert saved_vehs[0].steering == "Hidráulica"
        assert saved_vehs[0].current_km == 98500


@pytest.mark.asyncio
async def test_complete_vehicle_onboarding_9_fields_and_optional_specs_skip():
    """
    Valida o Playbook de Onboarding Veicular Completo (9 campos padronizados:
    brand, model, year, plate, odometer, version, engine, transmission, steering):
    - Cenário A: Condutor informa todos os 9 dados -> rascunho preenchido 100% e persistido no SQLite.
    - Cenário B: Condutor pula as especificações técnicas (version, engine, transmission, steering) ->
      veículo é salvo com sucesso no SQLite com esses campos nulos/None, sem travar o onboarding.
    """
    from datetime import timezone
    from src.ai.schemas import VehicleDraft
    from src.domain.entities import Vehicle
    from src.domain.enums import IntentType
    from src.infrastructure.database.models import VehicleModel
    from src.infrastructure.database.repositories import VehicleRepository, UserRepository
    from src.infrastructure.database.session import init_db, async_session_factory
    from src.interfaces.api.webhook import _persist_confirmed_data

    required_9_fields = ("brand", "model", "year", "plate", "odometer", "version", "engine", "transmission", "steering")
    for field_name in required_9_fields:
        assert field_name in VehicleDraft.model_fields, f"Campo {field_name} ausente em VehicleDraft"
        assert field_name in Vehicle.model_fields, f"Campo {field_name} ausente em Vehicle"
        assert hasattr(VehicleModel, field_name), f"Atributo {field_name} ausente em VehicleModel"

    # Valida que o Playbook em 5 etapas consta no SYSTEM_INSTRUCTION
    assert "Etapa 1: Dados Básicos (Marca, Modelo e Ano)" in SYSTEM_INSTRUCTION
    assert "Etapa 2: Identificação e Odômetro (Placa e KM atual)" in SYSTEM_INSTRUCTION
    assert "Etapa 3: Ficha Técnica (Versão, Motor, Câmbio e Direção)" in SYSTEM_INSTRUCTION
    assert "Etapa 4: Confirmação e Salvamento" in SYSTEM_INSTRUCTION
    assert "Etapa 5: Engajamento Imediato Pós-Salvamento" in SYSTEM_INSTRUCTION

    await init_db()
    now_ts = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)

    # =========================================================================
    # CENÁRIO A: Condutor informa todos os 9 dados -> rascunho 100% preenchido
    # =========================================================================
    service_a = ConversationService()
    user_a = "5511999998901"

    turns_a = [
        # Turno 1 (Etapa 1): Marca, Modelo e Ano
        GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Ótimo, um Fiat Palio 2008! Qual é a placa e a quilometragem atual dele?",
            tool_call=ToolCallAction(name="upsert_draft", brand="Fiat", model="Palio", year=2008),
        ),
        # Turno 2 (Etapa 2): Placa e Odômetro
        GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Para calibrar os planos de revisão: qual é a versão (ex: Attractive, Fire), o motor (ex: 1.0, 1.4), o câmbio (manual ou automático) e o tipo de direção (mecânica, hidráulica ou elétrica) dele?",
            tool_call=ToolCallAction(name="upsert_draft", plate="KXY-1234", odometer=144000),
        ),
        # Turno 3 (Etapa 3): Ficha técnica completa (versão, motor, câmbio, direção)
        GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Resumo do veículo: Fiat Palio Fire 2008, motor 1.0, câmbio Manual, direção Hidráulica, placa KXY1234 e 144.000 km. Posso salvar na garagem?",
            tool_call=ToolCallAction(
                name="upsert_draft",
                version="Fire",
                engine="1.0",
                transmission="Manual",
                steering="Hidráulica",
            ),
        ),
        # Turno 4 (Etapas 4 e 5): Confirmação e salvamento com engajamento pós-cadastro
        GeminiAnalysisResponse(
            intent=IntentType.CONFIRM_ACTION,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message="✅ Veículo cadastrado com sucesso na garagem! Deseja registrar o histórico de alguma manutenção que já foi feita ou relatar algum serviço pendente?",
            tool_call=ToolCallAction(name="confirm_and_save_draft"),
        ),
    ]
    idx_a = 0

    class StepGeminiA:
        async def analyze_message(self, *args, **kwargs):
            nonlocal idx_a
            resp = turns_a[idx_a]
            idx_a += 1
            return resp

    service_a.gemini = StepGeminiA()

    await service_a.handle_incoming_message(user_id=user_a, message_text="Quero cadastrar um Fiat Palio 2008", vehicles=[])
    await service_a.handle_incoming_message(user_id=user_a, message_text="Placa KXY-1234 e está com 144.000 km", vehicles=[])
    r3_a = await service_a.handle_incoming_message(user_id=user_a, message_text="Versão Fire, motor 1.0, manual, direção hidráulica", vehicles=[])

    draft_a = r3_a["draft"]
    assert draft_a["brand"] == "Fiat"
    assert draft_a["model"] == "Palio"
    assert draft_a["year"] == 2008
    assert draft_a["plate"] == "KXY1234"
    assert draft_a["odometer"] == 144000
    assert draft_a["version"] == "Fire"
    assert draft_a["engine"] == "1.0"
    assert draft_a["transmission"] == "Manual"
    assert draft_a["steering"] == "Hidráulica"

    # Verifica que build_draft_atual_dict expõe todos os 9 campos para a IA
    invisible_draft_a = build_draft_atual_dict(service_a.get_context(user_a))
    for k in required_9_fields:
        assert invisible_draft_a[k] is not None, f"Campo {k} deveria estar preenchido em draft_atual"

    r4_a = await service_a.handle_incoming_message(user_id=user_a, message_text="Sim, pode salvar", vehicles=[])
    assert r4_a["action_type"] == "action_confirmed"
    assert "Deseja registrar o histórico de alguma manutenção que já foi feita ou relatar algum serviço pendente?" in r4_a["reply"]

    async with async_session_factory() as session:
        await UserRepository(session).reset_user_data(user_a)
        await UserRepository(session).get_or_create(user_id=user_a)
        out_a = await _persist_confirmed_data(session, user_a, r4_a["action_type"], r4_a["committed_data"], [], now_ts)
        assert len(out_a["success"]) == 1
        vehs_a = await VehicleRepository(session).get_vehicles_by_user_id(user_a)
        assert len(vehs_a) == 1
        va = vehs_a[0]
        assert va.brand == "Fiat" and va.make == "Fiat"
        assert va.model == "Palio"
        assert va.year == 2008
        assert va.plate == "KXY1234"
        assert va.odometer == 144000 and va.current_km == 144000
        assert va.version == "Fire"
        assert va.engine == "1.0"
        assert va.transmission == "Manual"
        assert va.steering == "Hidráulica"

    # =========================================================================
    # CENÁRIO B: Condutor pula as especificações técnicas (version/engine/transmission/steering)
    # -> veículo é salvo com sucesso com esses campos nulos/None, sem travar o onboarding
    # =========================================================================
    service_b = ConversationService()
    user_b = "5511999998902"

    turns_b = [
        # Turno 1: Marca, Modelo, Ano, Placa e Odômetro
        GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Anotei seu Fiat Palio 2008 (placa ABC-1234, 95.000 km). Para calibrar os planos de revisão: qual é a versão, o motor, o câmbio e o tipo de direção dele?",
            tool_call=ToolCallAction(name="upsert_draft", brand="Fiat", model="Palio", year=2008, plate="ABC-1234", odometer=95000),
        ),
        # Turno 2: Usuário responde 'não sei, pode salvar só com isso' -> IA não insiste e salva com campos opcionais None
        GeminiAnalysisResponse(
            intent=IntentType.CONFIRM_ACTION,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message="Sem problemas! Salvei seu Fiat Palio 2008 (placa ABC1234, 95.000 km) e você pode completar a ficha técnica quando quiser. Deseja registrar o histórico de alguma manutenção que já foi feita ou relatar algum serviço pendente?",
            tool_call=ToolCallAction(name="confirm_and_save_draft"),
        ),
    ]
    idx_b = 0

    class StepGeminiB:
        async def analyze_message(self, *args, **kwargs):
            nonlocal idx_b
            resp = turns_b[idx_b]
            idx_b += 1
            return resp

    service_b.gemini = StepGeminiB()

    r1_b = await service_b.handle_incoming_message(
        user_id=user_b,
        message_text="Quero cadastrar meu Fiat Palio 2008 placa ABC-1234 com 95000 km",
        vehicles=[],
    )
    assert r1_b["action_type"] == "awaiting_confirmation"
    assert r1_b["draft"]["version"] is None
    assert r1_b["draft"]["engine"] is None
    assert r1_b["draft"]["transmission"] is None
    assert r1_b["draft"]["steering"] is None

    r2_b = await service_b.handle_incoming_message(
        user_id=user_b,
        message_text="não sei, depois vejo, pode salvar só com isso",
        vehicles=[],
    )
    assert r2_b["action_type"] == "action_confirmed"
    committed_b = r2_b["committed_data"]
    assert committed_b["brand"] == "Fiat"
    assert committed_b["model"] == "Palio"
    assert committed_b["year"] == 2008
    assert committed_b["plate"] == "ABC1234"
    assert committed_b["odometer"] == 95000
    assert committed_b["version"] is None
    assert committed_b["engine"] is None
    assert committed_b["transmission"] is None
    assert committed_b["steering"] is None

    async with async_session_factory() as session:
        await UserRepository(session).reset_user_data(user_b)
        await UserRepository(session).get_or_create(user_id=user_b)
        out_b = await _persist_confirmed_data(session, user_b, r2_b["action_type"], committed_b, [], now_ts)
        assert len(out_b["success"]) == 1
        assert out_b["failed"] == []
        vehs_b = await VehicleRepository(session).get_vehicles_by_user_id(user_b)
        assert len(vehs_b) == 1
        vb = vehs_b[0]
        assert vb.brand == "Fiat"
        assert vb.model == "Palio"
        assert vb.year == 2008
        assert vb.plate == "ABC1234"
        assert vb.odometer == 95000 and vb.current_km == 95000
        assert vb.version is None
        assert vb.engine is None
        assert vb.transmission is None
        assert vb.steering is None
