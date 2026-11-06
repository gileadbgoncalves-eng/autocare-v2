"""
AutoCare V2 - Testes de Debounce no Webhook, Patch Atômico e Orquestração AI-First
Validação rigorosa contra race conditions no WhatsApp, atropelo de mensagens consecutivas
e blindagem contra perda de dados na ficha técnica do veículo.
"""

import asyncio
import uuid
import pytest
from datetime import datetime

from src.config.settings import settings
from src.domain.entities import Vehicle
from src.domain.enums import VehicleType, FuelType, UsageProfile, ConversationState, IntentType, ConfidenceLevel
from src.ai.schemas import GeminiAnalysisResponse, ToolCallAction
from src.infrastructure.database.session import async_session_factory
from src.infrastructure.database.repositories import UserRepository, VehicleRepository
from src.application.services.conversation import ConversationService
import src.interfaces.api.webhook as webhook_module


def _generate_test_uid() -> str:
    return f"5511{uuid.uuid4().int % 100000000:08d}"


@pytest.mark.asyncio
async def test_vehicle_profile_atomic_patch_preserves_existing_fields():
    """Valida que patch_vehicle_profile atualiza atomicamente sem sobrescrever com placeholders."""
    user_id = _generate_test_uid()

    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        veh_repo = VehicleRepository(session)
        await user_repo.get_or_create(user_id=user_id, name="Piloto Patch")

        initial_veh = Vehicle(
            user_id=user_id,
            make="Fiat",
            model="Palio",
            version="Fire",
            year=2008,
            plate="LPD8D80",
            engine="1.0",
            transmission="Manual",
            steering="Hidráulica",
            current_km=144000,
            vehicle_type=VehicleType.CARRO,
            fuel_type=FuelType.FLEX,
            usage_profile=UsageProfile.CONVENCIONAL,
            created_at=datetime.utcnow(),
        )
        saved = await veh_repo.add_vehicle(initial_veh)
        assert saved.id is not None
        assert saved.plate == "LPD8D80"
        assert saved.version == "Fire"
        assert saved.engine == "1.0"
        assert saved.steering == "Hidráulica"

        patched = await veh_repo.patch_vehicle_profile(
            vehicle_id=saved.id,
            engine="1.4",
            plate="A cadastrar",
            version="Padrão",
            steering=None,
            transmission="",
        )

        assert patched is not None
        assert patched.engine == "1.4"
        assert patched.plate == "LPD8D80"
        assert patched.version == "Fire"
        assert patched.steering == "Hidráulica"
        assert patched.transmission == "Manual"
        assert patched.current_km == 144000


@pytest.mark.asyncio
async def test_multi_turn_onboarding_preserves_plate_and_specs():
    """Valida que em múltiplos turnos de onboarding a placa e os specs não são apagados no Deep Merge."""
    user_id = _generate_test_uid()
    service = ConversationService()

    # Turno 1: Usuário informa que comprou o Palio com placa e km
    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Comprei um Fiat Palio 2008 placa LPD8D80 com 144.000 km",
        vehicles=[],
    )
    assert res1["action_type"] in ("awaiting_confirmation", "awaiting_vehicle_registration_confirm")
    ctx = service.get_context(user_id)
    assert ctx.draft_payload is not None
    assert ctx.draft_payload.get("plate") == "LPD8D80"
    assert ctx.draft_payload.get("odometer_km") == 144000

    # Turno 2: Usuário informa motor, câmbio e direção
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="1.0 , não sei a versão mas tem ar, direção hidrauliza, vidro elétrico na frente, cambio manual",
        vehicles=[],
    )
    assert res2["action_type"] == "awaiting_confirmation"
    ctx2 = service.get_context(user_id)
    draft2 = ctx2.draft_payload

    assert draft2.get("plate") == "LPD8D80"
    assert draft2.get("engine") == "1.0"
    assert draft2.get("transmission") == "Manual"
    assert draft2.get("steering") == "Hidráulica"

    # Turno 3: Usuário confirma
    res3 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="pode salvar",
        vehicles=[],
    )
    assert res3["action_type"] == "action_confirmed"
    assert res3["committed_data"].get("plate") == "LPD8D80"
    assert res3["committed_data"].get("engine") == "1.0"
    assert res3["committed_data"].get("steering") == "Hidráulica"


@pytest.mark.asyncio
async def test_webhook_debounce_burst_messages(monkeypatch):
    """
    Simula envio em rajada no WhatsApp (ex: descrição e placa enviadas em mensagens separadas com 50ms de intervalo).
    Valida que o webhook agrupa as mensagens em um único payload e emite apenas 1 resposta outbound.
    """
    user_id = _generate_test_uid()
    sent_outbound = []

    async def fake_whatsapp_send(to: str, text: str) -> bool:
        sent_outbound.append((to, text))
        return True

    monkeypatch.setattr(webhook_module, "send_whatsapp_reply", fake_whatsapp_send)
    monkeypatch.setattr(settings, "WEBHOOK_DEBOUNCE_SECONDS", 0.15)

    payload1 = {
        "user_id": user_id,
        "message": "1.0 , não sei a versão mas tem ar, direção hidrauliza, vidro elétrico na frente, cambio manual",
    }
    payload2 = {
        "user_id": user_id,
        "message": "placa lpd8d80",
    }

    task1 = asyncio.create_task(webhook_module.receive_webhook(payload1))
    await asyncio.sleep(0.04)
    task2 = asyncio.create_task(webhook_module.receive_webhook(payload2))

    resp1, resp2 = await asyncio.gather(task1, task2)

    assert resp1["status"] == "success"
    assert resp2["status"] == "success"
    assert resp1["user_id"] == user_id
    assert resp2["user_id"] == user_id

    assert len(sent_outbound) == 1
    outbound_to, outbound_text = sent_outbound[0]
    assert outbound_to == user_id
    assert len(outbound_text) > 0


@pytest.mark.asyncio
async def test_isolated_odometer_update_is_silent_and_direct():
    """
    Valida Diretriz 1: Atualização isolada de KM NÃO cria rascunho de serviço,
    NÃO aciona AWAITING_CONFIRMATION, NÃO pergunta 'Posso confirmar?' e persiste no banco imediatamente.
    """
    user_id = _generate_test_uid()

    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        veh_repo = VehicleRepository(session)
        await user_repo.get_or_create(user_id=user_id, name="Piloto Odom")

        v = Vehicle(
            user_id=user_id,
            make="Fiat",
            model="Palio",
            year=2008,
            current_km=140000,
            plate="LPD8D80",
            created_at=datetime.utcnow(),
        )
        saved = await veh_repo.add_vehicle(v)
        veh_id = saved.id

    payload = {
        "user_id": user_id,
        "message": "hoje estou com 144.500 km agora",
    }
    resp = await webhook_module.receive_webhook(payload)
    assert resp["status"] == "success"

    service = webhook_module.conversation_service
    ctx = service.get_context(user_id)
    assert ctx.state == ConversationState.NORMAL
    assert ctx.draft_payload is None
    assert ctx.is_awaiting_confirmation() is False

    last_reply = ctx.recent_turns[-1].content
    assert "posso confirmar" not in last_reply.lower()
    assert "144.500 km" in last_reply or "144500 km" in last_reply

    async with async_session_factory() as session:
        veh_repo = VehicleRepository(session)
        updated_veh = await veh_repo.get_by_id(veh_id)
        assert updated_veh is not None
        assert updated_veh.current_km == 144500


@pytest.mark.asyncio
async def test_compound_intent_odometer_and_maintenance_query():
    """
    Valida Diretriz 2 (Intenção Composta): 'hoje bateu 145.200 km, tem alguma revisão vencendo?'
    O sistema DEVE atualizar o hodômetro imediatamente E responder sobre as revisões para o novo KM,
    sem tratar a atualização como rascunho de serviço e sem pedir confirmação.
    """
    user_id = _generate_test_uid()

    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        veh_repo = VehicleRepository(session)
        await user_repo.get_or_create(user_id=user_id, name="Piloto Compound")

        v = Vehicle(
            user_id=user_id,
            make="Fiat",
            model="Palio",
            year=2008,
            current_km=140000,
            plate="LPD8D80",
            created_at=datetime.utcnow(),
        )
        saved = await veh_repo.add_vehicle(v)
        veh_id = saved.id

    payload = {
        "user_id": user_id,
        "message": "hoje bateu 145.200 km, tem alguma revisão vencendo?",
    }
    resp = await webhook_module.receive_webhook(payload)
    assert resp["status"] == "success"

    service = webhook_module.conversation_service
    ctx = service.get_context(user_id)
    assert ctx.state == ConversationState.NORMAL
    assert ctx.draft_payload is None
    assert ctx.is_awaiting_confirmation() is False

    last_reply = ctx.recent_turns[-1].content
    assert "posso confirmar" not in last_reply.lower()
    assert "145.200 km" in last_reply or "145200 km" in last_reply
    assert "revisão" in last_reply.lower() or "revisao" in last_reply.lower()

    async with async_session_factory() as session:
        veh_repo = VehicleRepository(session)
        updated_veh = await veh_repo.get_by_id(veh_id)
        assert updated_veh.current_km == 145200


@pytest.mark.asyncio
async def test_multi_tool_call_patch_vehicle_and_get_service_history():
    """
    Valida execução em lote (Multi-Tool Call):
    Quando a IA decide [patch_vehicle_profile, get_service_history], o ConversationService
    deve executar atomicamente o patch de KM e retornar a resposta analítica sem estado de confirmação.
    """
    service = ConversationService()
    user_id = _generate_test_uid()
    v = Vehicle(id=42, user_id=user_id, make="Fiat", model="Palio", current_km=140000)

    ai_response = GeminiAnalysisResponse(
        intent=IntentType.UPDATE_ODOMETER,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=False,
        reply_message="✅ Hodômetro atualizado para 145.200 km! Você está a 4.800 km da revisão programada de 150.000 km.",
        tool_calls=[
            ToolCallAction(name="patch_vehicle_profile", km=145200, target_vehicle_ref="Palio"),
            ToolCallAction(name="get_service_history"),
        ],
    )

    tc_res = service._execute_gemini_decision(
        analysis=ai_response,
        context=service.get_context(user_id),
        vehicles=[v],
        maintenances=[],
        clean_msg="hoje bateu 145.200 km, tem alguma revisão vencendo?",
    )

    assert tc_res is not None
    assert tc_res["action_type"] == "odometer_updated"
    assert tc_res["state"] == ConversationState.NORMAL.value
    assert tc_res["committed_data"]["odometer_km"] == 145200
    assert tc_res["committed_data"]["_is_odometer_update"] is True
    assert v.current_km == 145200
