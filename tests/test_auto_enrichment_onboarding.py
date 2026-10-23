"""
AutoCare V2 - Testes de Enriquecimento Automático e Autopreenchimento de Ficha Técnica
Valida os requisitos de UX inteligente no Onboarding:
1. Sincronização e validação dos aliases em português (motor, cambio, direcao) no VehicleDraft.
2. Inferência de ficha técnica oficial para modelos do mercado brasileiro (ex: Nivus Highline, Corolla XEI).
3. Resposta de aprovação proativa sem interrogatório burocrático:
   "Excelente escolha! Já puxei a ficha técnica do seu Nivus Highline (Motor 1.0 Turbo, Automático, Direção Elétrica). Posso salvar na garagem?"
4. Persistência completa no banco de dados (SQLite/SQLAlchemy) com os 4 campos técnicos (engine, version, transmission, steering).
"""

import sys
import pytest

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.ai.schemas import VehicleDraft
from src.application.services.conversation import ConversationService, find_popular_vehicle_spec
from src.domain.enums import ConversationState
from src.domain.entities import Vehicle
from src.infrastructure.database.session import init_db, async_session_factory
from src.infrastructure.database.repositories import VehicleRepository, UserRepository


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


def test_vehicle_draft_schema_and_aliases_sync():
    """Valida que o schema Pydantic V2 sincroniza aliases em português bidirecionalmente."""
    # Instanciação usando aliases em português
    draft_pt = VehicleDraft(
        make="Volkswagen",
        model="Nivus",
        version="Highline",
        motor="1.0 Turbo",
        cambio="Automático",
        direcao="Elétrica",
    )
    assert draft_pt.engine == "1.0 Turbo"
    assert draft_pt.transmission == "Automático"
    assert draft_pt.steering == "Elétrica"
    assert draft_pt.motor == "1.0 Turbo"
    assert draft_pt.cambio == "Automático"
    assert draft_pt.direcao == "Elétrica"

    # Instanciação usando campos canônicos em inglês
    draft_en = VehicleDraft(
        make="Toyota",
        model="Corolla",
        version="XEI",
        engine="2.0 Dynamic Force",
        transmission="Automático",
        steering="Elétrica",
    )
    assert draft_en.motor == "2.0 Dynamic Force"
    assert draft_en.cambio == "Automático"
    assert draft_en.direcao == "Elétrica"


def test_find_popular_vehicle_spec_knowledge_base():
    """Valida que a base de conhecimento automotivo identifica modelos e versões com precisão."""
    nivus_spec = find_popular_vehicle_spec("Tenho um Nivus Highline 2022")
    assert nivus_spec is not None
    assert nivus_spec["model"] == "Nivus"
    assert nivus_spec["version"] == "Highline"
    assert nivus_spec["engine"] == "1.0 Turbo"
    assert nivus_spec["transmission"] == "Automático"
    assert nivus_spec["steering"] == "Elétrica"

    corolla_spec = find_popular_vehicle_spec("Comprei um Corolla XEI")
    assert corolla_spec is not None
    assert corolla_spec["model"] == "Corolla"
    assert corolla_spec["version"] == "XEI"
    assert corolla_spec["engine"] == "2.0 Dynamic Force"
    assert corolla_spec["transmission"] == "Automático"
    assert corolla_spec["steering"] == "Elétrica"


@pytest.mark.asyncio
async def test_onboarding_proactive_confirmation_nivus_highline():
    """
    Cenário: Usuário informa 'Tenho um Nivus Highline 2022 placa ABC1D23 com 15000 km'.
    O sistema deve pular perguntas burocráticas e emitir a confirmação proativa exata:
    'Excelente escolha! Já puxei a ficha técnica do seu Nivus Highline (Motor 1.0 Turbo, Automático, Direção Elétrica). Posso salvar na garagem?'
    """
    service = ConversationService()
    user_id = "5521990001111"

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Tenho um Nivus Highline 2022 placa ABC1D23 com 15000 km",
        vehicles=[],
    )

    assert res["action_type"] == "awaiting_confirmation"
    assert res["state"] == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value

    reply = res["reply"]
    # Verifica a frase exata de aprovação proativa
    assert "Excelente escolha! Já puxei a ficha técnica do seu Nivus Highline" in reply
    assert "Motor 1.0 Turbo" in reply
    assert "Automático" in reply
    assert "Direção Elétrica" in reply
    assert "Posso salvar na garagem?" in reply

    # Garante que NÃO contém o interrogatório burocrático tradicional
    assert "📌 *Deseja confirmar e registrar essas informações?*" not in reply
    assert "👉 Responda *'Sim'* para salvar" not in reply

    # Valida integridade do draft payload retido na máquina de estados
    draft = res["draft"]
    assert draft["model"] == "Nivus"
    assert draft["version"] == "Highline"
    assert draft["engine"] == "1.0 Turbo"
    assert draft["transmission"] == "Automático"
    assert draft["steering"] == "Elétrica"
    assert draft["plate"] == "ABC1D23"
    assert draft["odometer_km"] == 15000


@pytest.mark.asyncio
async def test_onboarding_proactive_confirmation_corolla_xei():
    """Valida aprovação proativa para o Toyota Corolla XEI."""
    service = ConversationService()
    user_id = "5521990002222"

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Cadastra meu Corolla XEI 2023 placa BRA2E19",
        vehicles=[],
    )

    assert res["action_type"] == "awaiting_confirmation"
    assert res["state"] == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value

    reply = res["reply"]
    assert "Excelente escolha! Já puxei a ficha técnica do seu Corolla XEI" in reply
    assert "Motor 2.0 Dynamic Force" in reply
    assert "Automático" in reply
    assert "Direção Elétrica" in reply
    assert "Posso salvar na garagem?" in reply


@pytest.mark.asyncio
async def test_onboarding_end_to_end_database_persistence():
    """
    Fluxo ponta a ponta:
    1. Usuário envia 'Tenho um Nivus Highline placa ABC1D23 com 12000 km'.
    2. Bot responde com aprovação proativa e puxa a ficha técnica.
    3. Usuário confirma com 'pode salvar'.
    4. Veículo é gravado no banco de dados SQLite com todos os atributos técnicos.
    """
    service = ConversationService()
    user_id = "5521990003333"

    # Turno 1: Envio do cadastro
    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Tenho um Nivus Highline 2022 placa ABC1D23 com 12000 km",
        vehicles=[],
    )
    assert res1["action_type"] == "awaiting_confirmation"
    assert "Excelente escolha! Já puxei a ficha técnica do seu Nivus Highline" in res1["reply"]

    # Turno 2: Confirmação afirmativa
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="pode salvar na garagem",
        vehicles=[],
    )
    assert res2["action_type"] == "action_confirmed"
    assert "Veículo cadastrado com sucesso na sua garagem!" in res2["reply"]

    committed = res2["committed_data"]
    assert committed["model"] == "Nivus"
    assert committed["version"] == "Highline"
    assert committed["engine"] == "1.0 Turbo"
    assert committed["transmission"] == "Automático"
    assert committed["steering"] == "Elétrica"

    # Simula persistência no banco (espelhando a camada de webhook)
    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        vehicle_repo = VehicleRepository(session)

        # Garante condutor
        await user_repo.get_or_create(user_id=user_id, name="Piloto Nivus")

        new_vehicle = Vehicle(
            user_id=user_id,
            make=committed.get("make", "Volkswagen"),
            model=committed.get("model", "Nivus"),
            version=committed.get("version"),
            year=committed.get("year", 2022),
            plate=committed.get("plate", "ABC1D23"),
            engine=committed.get("engine"),
            transmission=committed.get("transmission"),
            steering=committed.get("steering"),
            current_km=committed.get("odometer_km", 12000),
        )
        saved = await vehicle_repo.add_vehicle(new_vehicle)

        # 5. Asserção no banco real
        persisted = await vehicle_repo.get_by_id(saved.id)
        assert persisted is not None
        assert persisted.make == "Volkswagen"
        assert persisted.model == "Nivus"
        assert persisted.version == "Highline"
        assert persisted.engine == "1.0 Turbo"
        assert persisted.transmission == "Automático"
        assert persisted.steering == "Elétrica"
        assert persisted.plate == "ABC1D23"
        assert persisted.current_km == 12000
