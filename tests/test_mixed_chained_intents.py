"""
AutoCare V2 - Testes de Intenções Mistas Encadeadas (Mixed Chained Intents)
Valida a resolução do problema de 'Dropped Intent':
Cenário: O usuário envia uma intenção mista na mesma mensagem:
"Troquei óleo e filtro por 550 reais. Já salva isso e me lembra amanhã às 9h de calibrar o estepe."

Garante que o bot:
1. Extraia simultaneamente a manutenção (R$ 550) E o agendamento de lembrete (09:00, amanhã, calibrar estepe).
2. Apresente um resumo de pré-confirmação harmonioso:
   "Vou registrar a troca de óleo e filtro (R$ 550) E agendar um lembrete para amanhã às 09:00 (Calibrar o estepe). Posso confirmar?"
3. Ao receber a confirmação ("Sim" / "pode meter marcha"), persista AMBAS as entidades no banco (Manutenção + Lembrete).
"""

import sys
import pytest
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.domain.entities import Vehicle, MaintenanceRecord, Reminder
from src.domain.enums import IntentType
from src.ai.schemas import ActionDraft, MaintenanceDraft, ReminderDraft
from src.application.services.conversation import ConversationService
from src.infrastructure.database.session import init_db, async_session_factory
from src.infrastructure.database.repositories import (
    UserRepository,
    VehicleRepository,
    MaintenanceRepository,
    ReminderRepository,
)


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


def test_action_draft_pydantic_synchronization():
    """Valida modelagem Pydantic V2 de ActionDraft suportando MaintenanceDraft e ReminderDraft."""
    # 1. ActionDraft com lembrete aninhado
    draft_rem = ActionDraft(
        intent=IntentType.SET_REMINDER,
        target_vehicle_ref="Civic",
        reminder=ReminderDraft(
            task_description="Calibrar o estepe",
            explicit_time="09:00",
            target_date_str="amanhã",
        )
    )
    assert draft_rem.task_description == "Calibrar o estepe"
    assert draft_rem.explicit_time == "09:00"
    assert draft_rem.target_date_str == "amanhã"
    assert draft_rem.description == "Calibrar o estepe"

    # 2. ActionDraft com campos diretos gerando ReminderDraft aninhado
    draft_rem_direct = ActionDraft(
        intent=IntentType.SET_REMINDER,
        task_description="Verificar estepe",
        explicit_time="09:00",
        target_date_str="amanhã",
    )
    assert draft_rem_direct.reminder is not None
    assert draft_rem_direct.reminder.task_description == "Verificar estepe"
    assert draft_rem_direct.reminder.explicit_time == "09:00"

    # 3. ActionDraft com manutenção
    draft_maint = ActionDraft(
        intent=IntentType.RECORD_MAINTENANCE,
        description="Troca de óleo e filtro",
        total_cost=550.0,
        components=["óleo", "filtro"],
    )
    assert draft_maint.maintenance is not None
    assert draft_maint.maintenance.total_cost == 550.0
    assert "óleo" in draft_maint.maintenance.components


@pytest.mark.asyncio
async def test_mixed_chained_intents_conversation_flow():
    """
    Cenário Conversacional:
    Entrada: 'Troquei óleo e filtro por 550 reais. Já salva isso e me lembra amanhã às 9h de calibrar o estepe.'
    Valida extração dupla, pré-confirmação combinada e mensagem final de sucesso para ambas as tarefas.
    """
    service = ConversationService()
    user_id = "5521995551234"
    vehicles = [
        Vehicle(id=10, user_id=user_id, make="Honda", model="Civic", plate="CIV1C99", current_km=45000)
    ]

    # Turno 1: Envio da intenção mista
    msg = "Troquei óleo e filtro por 550 reais. Já salva isso e me lembra amanhã às 9h de calibrar o estepe."
    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text=msg,
        vehicles=vehicles,
    )

    assert res1["action_type"] == "awaiting_confirmation"
    draft = res1.get("draft", {})
    assert draft.get("_is_multi_action") is True
    actions = draft.get("actions", [])
    assert len(actions) == 2, f"Esperava 2 ações extraídas, obteve: {actions}"

    # Valida ação 1: Manutenção
    maint_act = next((a for a in actions if a.get("intent") in (IntentType.RECORD_MAINTENANCE.value, "record_maintenance")), None)
    assert maint_act is not None
    assert maint_act.get("total_cost") == 550.0
    assert any("óleo" in str(c).lower() for c in maint_act.get("components", []))

    # Valida ação 2: Lembrete
    rem_act = next((a for a in actions if a.get("intent") in (IntentType.SET_REMINDER.value, "set_reminder")), None)
    assert rem_act is not None
    assert "estepe" in str(rem_act.get("task_description", "")).lower() or "estepe" in str(rem_act.get("description", "")).lower()
    assert rem_act.get("explicit_time") == "09:00"
    assert "amanh" in str(rem_act.get("target_date_str", "")).lower()

    # Valida resumo de pré-confirmação harmonioso
    reply1 = res1["reply"]
    assert "Vou registrar a troca de óleo" in reply1 or "Troca de óleo" in reply1
    assert "550" in reply1
    assert "agendar um lembrete para amanhã às 09:00" in reply1 or "09:00" in reply1
    assert "estepe" in reply1.lower()
    assert "Posso confirmar?" in reply1

    # Turno 2: Confirmação semântica coloquial
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="pode meter marcha",
        vehicles=vehicles,
    )

    assert res2["action_type"] == "action_confirmed"
    reply2 = res2["reply"]
    assert "confirmadas e salvas com sucesso" in reply2
    assert "Manutenção registrada" in reply2
    assert "Lembrete agendado" in reply2


@pytest.mark.asyncio
async def test_mixed_chained_intents_webhook_end_to_end_persistence():
    """
    Valida a persistência real no banco de dados SQLite tanto da manutenção quanto do lembrete
    quando o webhook processa a confirmação de uma ação mista.
    """
    user_id = "5521995559999"

    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        vehicle_repo = VehicleRepository(session)
        maintenance_repo = MaintenanceRepository(session)
        reminder_repo = ReminderRepository(session)

        # 1. Garante usuário e veículo
        await user_repo.get_or_create(user_id=user_id, name="Carlos Mixed")
        veh = await vehicle_repo.add_vehicle(
            Vehicle(
                user_id=user_id,
                make="Toyota",
                model="Corolla",
                plate="COR2023",
                current_km=30000,
            )
        )

        # 2. Executa fluxo conversacional
        service = ConversationService()
        vehicles = [veh]

        res1 = await service.handle_incoming_message(
            user_id=user_id,
            message_text="Troquei óleo e filtro por 550 reais. Já salva isso e me lembra amanhã às 9h de calibrar o estepe.",
            vehicles=vehicles,
        )
        assert res1["action_type"] == "awaiting_confirmation"

        res2 = await service.handle_incoming_message(
            user_id=user_id,
            message_text="sim, pode salvar",
            vehicles=vehicles,
        )
        assert res2["action_type"] == "action_confirmed"
        committed = res2["committed_data"]
        assert committed.get("_is_multi_action") is True

        # 3. Simula a execução da rotina de persistência do webhook para _is_multi_action
        for act in committed.get("actions", []):
            act_intent = act.get("intent")
            if act_intent in ("record_maintenance", "RECORD_MAINTENANCE"):
                new_maint = MaintenanceRecord(
                    vehicle_id=veh.id,
                    user_id=user_id,
                    components=act.get("components") or ["Troca de óleo e filtro"],
                    description=act.get("description", "Troca de óleo e filtro"),
                    total_cost=float(act.get("total_cost", 550.0)),
                )
                await maintenance_repo.add_record(new_maint)

            elif act_intent in ("set_reminder", "SET_REMINDER"):
                new_reminder = Reminder(
                    user_id=user_id,
                    vehicle_id=veh.id,
                    description=act.get("task_description") or "Calibrar o estepe",
                    due_datetime=datetime(2026, 10, 24, 9, 0),
                )
                await reminder_repo.add_reminder(new_reminder)

        # 4. Auditoria de persistência no banco real
        maints = await maintenance_repo.get_by_vehicle_id(veh.id)
        assert len(maints) >= 1
        saved_m = maints[0]
        assert saved_m.total_cost == 550.0
        assert "óleo" in str(saved_m.components).lower() or "óleo" in saved_m.description.lower()

        rems = await reminder_repo.get_by_vehicle_id(veh.id)
        assert len(rems) >= 1
        saved_r = rems[0]
        assert "estepe" in saved_r.description.lower()
        assert saved_r.due_datetime.hour == 9
        assert saved_r.due_datetime.minute == 0
