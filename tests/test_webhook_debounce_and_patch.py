"""
AutoCare V2 - Testes de Debounce no Webhook, Patch Atômico e Tratamento de Typos
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
from src.application.services.conversation import ConversationService, parse_steering_type
import src.interfaces.api.webhook as webhook_module


def _generate_test_uid() -> str:
    return f"5511{uuid.uuid4().int % 100000000:08d}"


@pytest.mark.asyncio
async def test_steering_typo_and_accessories_disambiguation():
    """Valida mapeamento de typos em direção e desambiguação com acessórios elétricos."""
    # 1. Typos de Direção Hidráulica
    assert parse_steering_type("direção hidrauliza") == "Hidráulica"
    assert parse_steering_type("direção hidraulica") == "Hidráulica"
    assert parse_steering_type("direção hidro") == "Hidráulica"
    assert parse_steering_type("tem ar, direção hidrauliza, vidro elétrico na frente, cambio manual") == "Hidráulica"

    # 2. Desambiguação de vidro/trava elétrica (NÃO deve ser mapeado como Direção Elétrica)
    assert parse_steering_type("vidro elétrico na frente e trava elétrica") is None
    assert parse_steering_type("vidro eletrico e ar condicionado") is None

    # 3. Direção Mecânica
    assert parse_steering_type("direção mecanica") == "Mecânica"
    assert parse_steering_type("direção mecânica") == "Mecânica"
    assert parse_steering_type("sem direção") == "Mecânica"

    # 4. Coerência Histórica (Palio 2008 popular antigo -> Hidráulica mesmo se citado elétrico genericamente)
    assert parse_steering_type("direção elétrica", model_name="Palio", year=2008) == "Hidráulica"
    assert parse_steering_type("direção elétrica", model_name="Corolla", year=2022) == "Elétrica"


@pytest.mark.asyncio
async def test_vehicle_profile_atomic_patch_preserves_existing_fields():
    """Valida que patch_vehicle_profile atualiza atomicamente sem sobrescrever com placeholders."""
    user_id = _generate_test_uid()

    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        veh_repo = VehicleRepository(session)
        await user_repo.get_or_create(user_id=user_id, name="Piloto Patch")

        # 1. Cria veículo completo inicial
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

        # 2. Executa PATCH com novos dados válidos + placeholders genéricos que DEVEM SER IGNORADOS
        patched = await veh_repo.patch_vehicle_profile(
            vehicle_id=saved.id,
            engine="1.4",  # Atualização válida
            plate="A cadastrar",  # Placeholder -> DEVE PRESERVAR LPD8D80
            version="Padrão",  # Placeholder -> DEVE PRESERVAR Fire
            steering=None,  # Nulo -> DEVE PRESERVAR Hidráulica
            transmission="",  # Vazio -> DEVE PRESERVAR Manual
        )

        assert patched is not None
        assert patched.engine == "1.4"
        assert patched.plate == "LPD8D80"  # Preservado!
        assert patched.version == "Fire"  # Preservado!
        assert patched.steering == "Hidráulica"  # Preservado!
        assert patched.transmission == "Manual"  # Preservado!
        assert patched.current_km == 144000


@pytest.mark.asyncio
async def test_multi_turn_onboarding_preserves_plate_and_specs():
    """Valida que em múltiplos turnos de onboarding a placa e os specs não são apagados."""
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

    # Turno 2: Usuário informa motor, câmbio e direção com typo ("hidrauliza") e vidro elétrico
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="1.0 , não sei a versão mas tem ar, direção hidrauliza, vidro elétrico na frente, cambio manual",
        vehicles=[],
    )
    assert res2["action_type"] == "awaiting_confirmation"
    ctx2 = service.get_context(user_id)
    draft2 = ctx2.draft_payload

    # Blindagem: A placa anterior NÃO PODE ter sido perdida!
    assert draft2.get("plate") == "LPD8D80"
    assert draft2.get("engine") == "1.0"
    assert draft2.get("transmission") == "Manual"
    # A direção NÃO PODE ser elétrica por causa do vidro elétrico, e sim Hidráulica por "hidrauliza"
    assert draft2.get("steering") == "Hidráulica"

    # Turno 3: Usuário confirma
    res3 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Sim, pode salvar",
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

    # Configura janela de debounce curta para rapidez nos testes
    monkeypatch.setattr(settings, "WEBHOOK_DEBOUNCE_SECONDS", 0.15)

    payload1 = {
        "user_id": user_id,
        "message": "1.0 , não sei a versão mas tem ar, direção hidrauliza, vidro elétrico na frente, cambio manual",
    }
    payload2 = {
        "user_id": user_id,
        "message": "placa lpd8d80",
    }

    # Dispara a primeira mensagem
    task1 = asyncio.create_task(webhook_module.receive_webhook(payload1))
    await asyncio.sleep(0.04)  # 40ms depois chega a placa

    # Dispara a segunda mensagem dentro da janela de 150ms
    task2 = asyncio.create_task(webhook_module.receive_webhook(payload2))

    resp1, resp2 = await asyncio.gather(task1, task2)

    # Ambas as requisições HTTP retornam sucesso
    assert resp1["status"] == "success"
    assert resp2["status"] == "success"
    assert resp1["user_id"] == user_id
    assert resp2["user_id"] == user_id

    # Exatamente UMA resposta enviada ao WhatsApp do usuário!
    assert len(sent_outbound) == 1
    outbound_to, outbound_text = sent_outbound[0]
    assert outbound_to == user_id
    assert "Perfeito!" in outbound_text or "ficha" in outbound_text.lower() or "salvar" in outbound_text.lower()


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

        # Veículo inicial com 140.000 km
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

    # Envia webhook informando nova quilometragem isolada
    payload = {
        "user_id": user_id,
        "message": "hoje estou com 144.500 km agora",
    }
    resp = await webhook_module.receive_webhook(payload)
    assert resp["status"] == "success"

    # Inspeciona estado da conversa e resposta
    service = webhook_module.conversation_service
    ctx = service.get_context(user_id)
    assert ctx.state == ConversationState.NORMAL
    assert ctx.draft_payload is None
    assert ctx.is_awaiting_confirmation() is False

    last_reply = ctx.recent_turns[-1].content
    assert "posso confirmar" not in last_reply.lower()
    assert "144.500 km" in last_reply or "144500 km" in last_reply

    # Verifica persistência imediata no banco de dados
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
    # Deve conter menção a revisão programada ou preventiva
    assert "revisão" in last_reply.lower() or "revisao" in last_reply.lower()

    # Confirma que o banco de dados gravou o novo KM
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

    tc_res = await service._execute_ai_tool_calls(
        context=service.get_context(user_id),
        analysis=ai_response,
        tool_calls=ai_response.tool_calls,
        clean_text="hoje bateu 145.200 km, tem alguma revisão vencendo?",
        vehicles=[v],
    )

    assert tc_res is not None
    assert tc_res["action_type"] == "odometer_updated"
    assert tc_res["state"] == ConversationState.NORMAL.value
    assert tc_res["committed_data"]["odometer_km"] == 145200
    assert tc_res["committed_data"]["_is_odometer_update"] is True
    assert v.current_km == 145200  # Atualizou em memória!

