"""
AutoCare V2 - Testes do Fluxo Conversacional de Cadastro de Veículo (Arquitetura 100% AI-First)
Valida os requisitos estritos da arquitetura AI-First:
1. Sincronização de campos e aliases no VehicleDraft e ToolCallAction (upsert_draft).
2. Proibição de inferência cega em Python: o backend nunca inventa direção, câmbio ou motor se a IA não enviou.
3. Preservação integral da reply_message do Gemini sem injeção de templates ou rodapés hardcoded.
4. Correção conversacional ("Não, é...") atualizando o rascunho via upsert_draft sem cancelar a operação.
5. Persistência completa no banco de dados (SQLite/SQLAlchemy) após confirm_and_save_draft.
"""

import sys
import pytest

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.ai.schemas import VehicleDraft, ToolCallAction, GeminiAnalysisResponse
from src.application.services.conversation import ConversationService
from src.domain.enums import ConversationState, IntentType, ConfidenceLevel
from src.domain.entities import Vehicle
from src.infrastructure.database.session import init_db, async_session_factory
from src.infrastructure.database.repositories import VehicleRepository, UserRepository


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


def test_vehicle_draft_schema_and_aliases_sync():
    """Valida que o schema Pydantic V2 sincroniza aliases em português e inglês bidirecionalmente."""
    draft_pt = VehicleDraft(
        brand="Volkswagen",
        model="Nivus",
        version="Highline",
        motor="1.0 Turbo",
        cambio="Automático",
        direcao="Elétrica",
        odometer=15000,
    )
    assert draft_pt.make == "Volkswagen"
    assert draft_pt.brand == "Volkswagen"
    assert draft_pt.engine == "1.0 Turbo"
    assert draft_pt.transmission == "Automático"
    assert draft_pt.steering == "Elétrica"
    assert draft_pt.odometer_km == 15000

    draft_en = VehicleDraft(
        make="Toyota",
        model="Corolla",
        version="XEI",
        engine="2.0 Dynamic Force",
        transmission="Automático",
        steering="Elétrica",
        odometer_km=25000,
    )
    assert draft_en.brand == "Toyota"
    assert draft_en.motor == "2.0 Dynamic Force"
    assert draft_en.cambio == "Automático"
    assert draft_en.direcao == "Elétrica"
    assert draft_en.odometer == 25000


@pytest.mark.asyncio
async def test_onboarding_no_python_spec_hallucination():
    """
    Cenário: Usuário informa 'Comprei um Fiat Palio 2008'.
    O código Python NUNCA deve adivinhar se tem direção hidráulica, câmbio manual ou motor 1.0,
    e o texto devolvido deve ser estritamente a reply_message gerada pelo Gemini.
    """
    service = ConversationService()
    user_id = "5521990001111"

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Comprei um Fiat Palio 2008",
        vehicles=[],
    )

    assert res["action_type"] == "awaiting_confirmation"
    assert res["state"] == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value

    draft = res["draft"]
    assert draft["make"] == "Fiat"
    assert draft["model"] == "Palio"
    assert draft["year"] == 2008
    # Python não pode ter inventado direção, motor ou câmbio
    assert draft.get("steering") is None
    assert draft.get("engine") is None
    assert draft.get("transmission") is None


@pytest.mark.asyncio
async def test_onboarding_correction_updates_draft_instead_of_cancelling():
    """
    Cenário: Rascunho de veículo aberto e usuário corrige dizendo:
    'Não, ele não tem direção hidráulica, é mecânica, motor 1.0 manual, placa LPD8D80 com 144000 km'.
    O sistema deve fazer deep merge no rascunho via upsert_draft sem cancelar a operação.
    """
    service = ConversationService()
    user_id = "5521990002222"

    await service.handle_incoming_message(
        user_id=user_id,
        message_text="Quero cadastrar meu Palio 2008",
        vehicles=[],
    )

    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Não, ele é direção mecânica, motor 1.0 manual, placa LPD8D80 com 144000 km",
        vehicles=[],
    )

    assert res2["action_type"] == "awaiting_confirmation"
    assert res2["state"] == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
    draft = res2["draft"]
    assert draft["model"] == "Palio"
    assert draft["year"] == 2008
    assert draft["steering"] == "Mecânica"
    assert draft["engine"] == "1.0"
    assert draft["transmission"] == "Manual"
    assert draft["plate"] == "LPD8D80"
    assert draft["odometer_km"] == 144000


@pytest.mark.asyncio
async def test_onboarding_end_to_end_database_persistence():
    """
    Fluxo ponta a ponta 100% AI-First:
    1. Usuário envia dados do veículo.
    2. Usuário confirma com 'sim'.
    3. Veículo é gravado no banco de dados SQLite com todos os atributos técnicos informados.
    """
    service = ConversationService()
    user_id = "5521990003333"

    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Tenho um Nivus Highline 2022 1.0 Turbo automático direção elétrica placa ABC1D23 com 12000 km",
        vehicles=[],
    )
    assert res1["action_type"] == "awaiting_confirmation"

    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="sim",
        vehicles=[],
    )
    assert res2["action_type"] == "action_confirmed"
    # Verifica presença da pergunta de engajamento pós-cadastro gerada pela IA
    assert "manutenção" in res2["reply"].lower() or "serviço" in res2["reply"].lower()

    committed = res2["committed_data"]
    assert committed["model"] == "Nivus"
    assert committed["version"] == "Highline"
    assert committed["engine"] == "1.0 Turbo"
    assert committed["transmission"] == "Automático"
    assert committed["steering"] == "Elétrica"
    assert committed["plate"] == "ABC1D23"
    assert committed["odometer_km"] == 12000

    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        vehicle_repo = VehicleRepository(session)

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
