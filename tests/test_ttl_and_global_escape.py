"""
AutoCare V2 - Testes de Regressão: TTL de Rascunho, Fuso UTC-3 e Escape Global (Undo)

Cobre as 3 diretrizes da Fase 1 de correção da máquina de estados:
1. TTL de 30 min no rascunho aguardando confirmação (fim do "Sim" atrasado).
2. Injeção do momento exato em UTC-3 no prompt do LLM (e relógio BRT na persistência).
3. Interceptor de "desfaz / cancela o último / apaga isso que te mandei" com soft delete
   (status='cancelled') dos registros dos últimos 5 minutos.
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.ai.prompts import build_analysis_prompt
from src.application.services.conversation import (
    ConversationService,
    DRAFT_EXPIRED_REPLY,
    UNDO_SUCCESS_REPLY,
    UNDO_NOTHING_REPLY,
    UNDO_DRAFT_DISCARDED_REPLY,
    is_undo_command,
    looks_like_confirmation_reply,
)
from src.config.clock import BRT, build_temporal_context, now_brt_naive
from src.domain.entities import MaintenanceRecord, Reminder, Vehicle
from src.domain.enums import ConversationState
from src.infrastructure.database.repositories import (
    MaintenanceRepository,
    ReminderRepository,
    UserRepository,
    VehicleRepository,
    soft_delete_last_user_record,
)
from src.infrastructure.database.session import (
    _ensure_soft_delete_columns,
    async_session_factory,
    init_db,
)
from src.interfaces.api import webhook as webhook_module

MIXED_MESSAGE = "Troquei óleo e filtro por 550 reais. Já salva isso e me lembra amanhã às 9h de calibrar o estepe."


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


def _uid() -> str:
    # O identificador do usuário é um telefone sanitizado (somente dígitos)
    return "55219" + str(uuid.uuid4().int)[:8]



async def _seed_user_with_vehicle(session, user_id: str) -> Vehicle:
    await UserRepository(session).get_or_create(user_id=user_id, name="Teste Undo")
    return await VehicleRepository(session).add_vehicle(
        Vehicle(user_id=user_id, make="Toyota", model="Corolla", plate="COR2023", current_km=30000)
    )


# ---------------------------------------------------------------------------
# DIRETRIZ 1: TTL de rascunho
# ---------------------------------------------------------------------------
async def test_draft_expired_after_ttl_rejects_late_yes():
    user_id = _uid()
    veh = Vehicle(id=1, user_id=user_id, make="Toyota", model="Corolla", plate="COR2023", current_km=30000)
    service = ConversationService()

    res1 = await service.handle_incoming_message(user_id=user_id, message_text=MIXED_MESSAGE, vehicles=[veh])
    assert res1["action_type"] == "awaiting_confirmation"
    assert service.get_context(user_id).draft_payload is not None

    # Dois dias depois, o usuário responde "Sim"
    future = datetime.utcnow() + timedelta(days=2)
    service._utcnow = lambda: future
    res2 = await service.handle_incoming_message(user_id=user_id, message_text="Sim", vehicles=[veh])

    assert res2["action_type"] == "draft_expired"
    assert res2["reply"] == DRAFT_EXPIRED_REPLY
    assert "committed_data" not in res2  # nada pode ser persistido
    ctx = service.get_context(user_id)
    assert ctx.state == ConversationState.NORMAL
    assert ctx.draft_payload is None


async def test_draft_within_ttl_still_confirms():
    user_id = _uid()
    veh = Vehicle(id=1, user_id=user_id, make="Toyota", model="Corolla", plate="COR2023", current_km=30000)
    service = ConversationService()

    await service.handle_incoming_message(user_id=user_id, message_text=MIXED_MESSAGE, vehicles=[veh])
    soon = datetime.utcnow() + timedelta(minutes=10)
    service._utcnow = lambda: soon
    res = await service.handle_incoming_message(user_id=user_id, message_text="sim, pode salvar", vehicles=[veh])

    assert res["action_type"] == "action_confirmed"
    assert res["committed_data"].get("_is_multi_action") is True


async def test_draft_ttl_is_configurable_and_boundary():
    user_id = _uid()
    veh = Vehicle(id=1, user_id=user_id, make="Toyota", model="Corolla", plate="COR2023", current_km=30000)
    service = ConversationService(draft_ttl_minutes=30)
    await service.handle_incoming_message(user_id=user_id, message_text=MIXED_MESSAGE, vehicles=[veh])
    ctx = service.get_context(user_id)

    base = ctx.draft_updated_at
    assert base is not None
    service._utcnow = lambda: base + timedelta(minutes=29, seconds=59)
    assert service._is_draft_expired(ctx) is False
    service._utcnow = lambda: base + timedelta(minutes=30, seconds=1)
    assert service._is_draft_expired(ctx) is True


def test_confirmation_reply_detector():
    for text_ in ("Sim", "sim, pode salvar", "pode meter marcha", "ok", "não", "cancela", "isso aí"):
        assert looks_like_confirmation_reply(text_), text_
    for text_ in (
        "Troquei o óleo do Civic por 300 reais ontem na oficina do Zé",
        "qual o status do meu carro?",
        "",
    ):
        assert not looks_like_confirmation_reply(text_), text_


# ---------------------------------------------------------------------------
# DIRETRIZ 2: Contexto temporal em UTC-3
# ---------------------------------------------------------------------------
def test_temporal_context_uses_brazil_timezone():
    # 05:00 UTC == 02:00 em Brasília (o caso "amanhã de manhãzinha" às 2h da madrugada)
    utc_moment = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)
    ctx = build_temporal_context(utc_moment)
    assert "AGORA é 2026-10-23T02:00:00-03:00" in ctx
    assert "23/10/2026 02:00" in ctx
    assert "sexta-feira" in ctx

    # 01:00 UTC == 22:00 do dia ANTERIOR em Brasília
    previous_day = build_temporal_context(datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc))
    assert "2026-10-23T22:00:00-03:00" in previous_day


def test_analysis_prompt_always_injects_temporal_context():
    prompt = build_analysis_prompt(current_message="me lembra amanhã de manhãzinha de calibrar o pneu")
    assert "[CONTEXTO TEMPORAL]" in prompt
    assert "AGORA é" in prompt
    assert "-03:00" in prompt
    # O contexto temporal precede qualquer outra seção do prompt
    assert prompt.index("[CONTEXTO TEMPORAL]") < prompt.index("[VEÍCULOS CADASTRADOS")


def test_now_brt_naive_is_utc_minus_3():
    expected = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    assert abs((now_brt_naive() - expected).total_seconds()) < 5
    assert BRT.utcoffset(None) == timedelta(hours=-3)


# ---------------------------------------------------------------------------
# DIRETRIZ 3: Escape global (undo) com soft delete
# ---------------------------------------------------------------------------
def test_undo_command_detection():
    for phrase in (
        "desfaz", "Desfaz!", "desfazer isso", "cancela o último", "Cancela o ultimo registro",
        "apaga isso que te mandei", "apaga isso que eu te mandei", "apaga o último", "apague isso",
    ):
        assert is_undo_command(phrase), phrase
    for phrase in (
        "cancela o lembrete de amanhã",
        "troquei o óleo e desfaz o resto",
        "apaga a placa antiga do Civic",
        "sim",
        "cancelar",  # escape de fluxo já tratado em outro interceptor
        "",
    ):
        assert not is_undo_command(phrase), phrase


async def test_undo_with_pending_draft_discards_draft_only():
    user_id = _uid()
    veh = Vehicle(id=1, user_id=user_id, make="Toyota", model="Corolla", plate="COR2023", current_km=30000)
    service = ConversationService()
    await service.handle_incoming_message(user_id=user_id, message_text=MIXED_MESSAGE, vehicles=[veh])

    res = await service.handle_incoming_message(user_id=user_id, message_text="desfaz", vehicles=[veh])
    assert res["action_type"] == "action_cancelled"
    assert res["reply"] == UNDO_DRAFT_DISCARDED_REPLY
    assert service.get_context(user_id).draft_payload is None


async def test_soft_delete_last_record_within_5_minutes_and_batch():
    user_id = _uid()
    async with async_session_factory() as session:
        veh = await _seed_user_with_vehicle(session, user_id)
        maint_repo = MaintenanceRepository(session)
        rem_repo = ReminderRepository(session)

        old = await maint_repo.add_record(MaintenanceRecord(
            vehicle_id=veh.id, user_id=user_id, components=["Pneus"], description="Pneus antigos",
            total_cost=1000.0, created_at=datetime.utcnow() - timedelta(hours=1),
        ))
        recent_m = await maint_repo.add_record(MaintenanceRecord(
            vehicle_id=veh.id, user_id=user_id, components=["Óleo", "Filtro"], description="Troca de óleo e filtro",
            total_cost=550.0,
        ))
        recent_r = await rem_repo.add_reminder(Reminder(
            user_id=user_id, vehicle_id=veh.id, description="Calibrar o estepe",
            due_datetime=datetime.utcnow() + timedelta(days=1),
        ))

        cancelled = await soft_delete_last_user_record(session, user_id)

        # Lote da intenção mista cancelado junto; registro antigo (fora da janela) preservado
        assert cancelled["maintenance_ids"] == [recent_m.id]
        assert cancelled["reminder_ids"] == [recent_r.id]
        active = await maint_repo.get_by_user_id(user_id)
        assert [m.id for m in active] == [old.id]
        assert await rem_repo.get_by_user_id(user_id) == []  # lembrete cancelado some das consultas

        # Soft delete: a linha continua no banco com status 'cancelled'
        row = await maint_repo.get_by_id(recent_m.id)
        assert row is not None and row.status == "cancelled"
        rem_row = await rem_repo.get_by_id(recent_r.id)
        assert rem_row is not None and rem_row.status.value == "cancelled"

        # Segunda chamada: nada novo na janela de 5 minutos
        again = await soft_delete_last_user_record(session, user_id)
        assert again == {"maintenance_ids": [], "reminder_ids": []}


async def test_soft_delete_ignores_records_older_than_window():
    user_id = _uid()
    async with async_session_factory() as session:
        veh = await _seed_user_with_vehicle(session, user_id)
        maint_repo = MaintenanceRepository(session)
        await maint_repo.add_record(MaintenanceRecord(
            vehicle_id=veh.id, user_id=user_id, components=["Óleo"], description="Troca de óleo",
            created_at=datetime.utcnow() - timedelta(minutes=6),
        ))
        result = await soft_delete_last_user_record(session, user_id)
        assert result == {"maintenance_ids": [], "reminder_ids": []}
        assert len(await maint_repo.get_by_user_id(user_id)) == 1


async def test_webhook_undo_end_to_end(monkeypatch):
    user_id = _uid()
    sent = []

    async def fake_send(to, text_):
        sent.append((to, text_))

    monkeypatch.setattr(webhook_module, "send_whatsapp_reply", fake_send)

    async with async_session_factory() as session:
        veh = await _seed_user_with_vehicle(session, user_id)
        maint_repo = MaintenanceRepository(session)
        saved = await maint_repo.add_record(MaintenanceRecord(
            vehicle_id=veh.id, user_id=user_id, components=["Óleo"], description="Troca de óleo", total_cost=200.0,
        ))

        resp = await webhook_module.receive_webhook(
            {"user_id": user_id, "message": "apaga isso que te mandei"}, session=session
        )
        assert resp["action_type"] == "undo_last_record"
        assert resp["reply"] == UNDO_SUCCESS_REPLY
        assert sent[-1] == (user_id, UNDO_SUCCESS_REPLY)
        assert (await maint_repo.get_by_id(saved.id)).status == "cancelled"
        assert await maint_repo.get_by_user_id(user_id) == []

        # Sem nada recente para desfazer, a resposta é honesta (não finge ter apagado)
        resp2 = await webhook_module.receive_webhook(
            {"user_id": user_id, "message": "desfaz"}, session=session
        )
        assert resp2["action_type"] == "undo_last_record"
        assert resp2["reply"] == UNDO_NOTHING_REPLY


def test_migration_adds_status_column_to_legacy_table():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE maintenance_records (id INTEGER PRIMARY KEY, description TEXT)"))
        conn.execute(text("INSERT INTO maintenance_records (description) VALUES ('legado')"))
        _ensure_soft_delete_columns(conn)
        _ensure_soft_delete_columns(conn)  # idempotente
        assert "status" in {c["name"] for c in inspect(conn).get_columns("maintenance_records")}
        assert conn.execute(text("SELECT status FROM maintenance_records")).scalar() == "active"
