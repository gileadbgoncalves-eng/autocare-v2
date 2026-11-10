"""
Testes de Validação do Histórico de Manutenções Retroativas — AutoCare V2.

Cenário obrigatório:
1. Usuário cadastra veículo com km_atual = 144.000 km.
2. Registra manutenção passada de 06/07/2026 com 140.000 km -> SUCESSO, km_atual permanece 144.000 km.
3. Registra manutenção ainda mais antiga de 20/05/2026 com 135.000 km -> SUCESSO, km_atual permanece 144.000 km.
4. Tenta registrar manutenção para a data de HOJE com 130.000 km -> FALHA com 'regressive_km' ("quilometragem inferior à atual do veículo para a data de hoje").
5. Tenta registrar manutenção incoerente cronologicamente (ex: 20/05/2026 com 145.000 km quando em 06/07/2026 tinha 140.000 km) -> FALHA com 'chronological_incoherence'.
"""

from datetime import date, datetime, timezone
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.ai.schemas import GeminiAnalysisResponse, ToolCallAction, MaintenanceDraft
from src.application.services.conversation import ConversationService
from src.domain.entities import User, Vehicle
from src.domain.enums import ConfidenceLevel, ConversationState, IntentType
from src.infrastructure.database.models import Base
from src.infrastructure.database.repositories import (
    MaintenanceRepository,
    UserRepository,
    VehicleRepository,
)
from src.interfaces.api.webhook import (
    _FAIL_REASONS_PT,
    _persist_confirmed_data,
    _persist_maintenance,
    _new_outcome,
)


@pytest.fixture
async def async_db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_retroactive_maintenance_full_lifecycle(async_db_session: AsyncSession):
    """
    Validação estrita do ciclo de 4 etapas exigido na auditoria:
    - Veículo a 144.000 km
    - Manutenção 1: 06/07/2026 com 140.000 km -> Sucesso, km_atual inalterado (144.000)
    - Manutenção 2: 20/05/2026 com 135.000 km -> Sucesso, km_atual inalterado (144.000)
    - Manutenção 3: Hoje com 130.000 km -> Bloqueada com erro de quilometragem regressiva
    - Manutenção 4 (Adversarial): 20/05/2026 com 145.000 km -> Bloqueada por incoerência cronológica
    """
    user_id = "5511999998888"
    now_ts = datetime(2026, 11, 9, 14, 0, 0, tzinfo=timezone.utc)
    today_date = now_ts.date()

    user_repo = UserRepository(async_db_session)
    veh_repo = VehicleRepository(async_db_session)
    maint_repo = MaintenanceRepository(async_db_session)

    await user_repo.get_or_create(user_id)

    # 1. Cadastra veículo com km_atual = 144000
    veh = await veh_repo.add_vehicle(
        Vehicle(
            user_id=user_id,
            make="Honda",
            model="Civic",
            plate="BRA2E19",
            current_km=144000,
        )
    )
    assert veh.id is not None
    assert veh.current_km == 144000

    # 2. Registra manutenção de 06/07/2026 com 140.000 km
    payload_maint_1 = {
        "description": "Troca de óleo e filtro",
        "components": ["Óleo 0W20", "Filtro de óleo"],
        "km": 140000,
        "data": "06/07/2026",
        "total_cost": 380.0,
        "target_vehicle_ref": "BRA2E19",
        "vehicle_id": veh.id,
    }
    outcome_1 = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_maint_1, [veh], now_ts
    )

    assert len(outcome_1["success"]) == 1, f"Falha inesperada no passo 2: {outcome_1['failed']}"
    assert len(outcome_1["failed"]) == 0

    # Valida que o km_atual do veículo PERMANECE 144.000 (não regrediu para 140.000)
    veh_after_1 = await veh_repo.get_by_id(veh.id)
    assert veh_after_1.current_km == 144000

    # 3. Registra manutenção mais antiga ainda: 20/05/2026 com 135.000 km
    payload_maint_2 = {
        "description": "Pastilhas de freio",
        "components": ["Pastilhas dianteiras"],
        "km": 135000,
        "data": "20/05/2026",
        "total_cost": 250.0,
        "target_vehicle_ref": "BRA2E19",
        "vehicle_id": veh.id,
    }
    outcome_2 = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_maint_2, [veh], now_ts
    )

    assert len(outcome_2["success"]) == 1, f"Falha inesperada no passo 3: {outcome_2['failed']}"
    assert len(outcome_2["failed"]) == 0

    # Valida que o km_atual do veículo CONTINUA 144.000
    veh_after_2 = await veh_repo.get_by_id(veh.id)
    assert veh_after_2.current_km == 144000

    # Valida que ambos os registros estão no banco com as datas e KMs corretos
    maints = await maint_repo.get_by_vehicle_id(veh.id)
    assert len(maints) == 2
    dates_kms = [(m.date, m.km) for m in maints]
    assert (date(2026, 7, 6), 140000) in dates_kms
    assert (date(2026, 5, 20), 135000) in dates_kms

    # 4. Tenta registrar manutenção com data de HOJE e 130.000 km -> Deve FALHAR
    payload_maint_today_regressive = {
        "description": "Revisão geral",
        "components": ["Velas", "Cabos"],
        "km": 130000,
        "data": today_date.strftime("%d/%m/%Y"),
        "total_cost": 500.0,
        "target_vehicle_ref": "BRA2E19",
        "vehicle_id": veh.id,
    }
    outcome_3 = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_maint_today_regressive, [veh_after_2], now_ts
    )

    assert len(outcome_3["success"]) == 0
    assert len(outcome_3["failed"]) == 1
    assert outcome_3["failed"][0]["reason"] == "regressive_km"
    assert _FAIL_REASONS_PT["regressive_km"] == "quilometragem inferior à atual do veículo para a data de hoje"

    # Veículo segue com 144.000 km
    veh_after_3 = await veh_repo.get_by_id(veh.id)
    assert veh_after_3.current_km == 144000

    # 5. Teste adversarial de incoerência cronológica:
    # Tenta inserir serviço em 20/05/2026 com 145.000 km (sendo que em 06/07/2026 o carro tinha 140.000 km)
    payload_maint_incoherent = {
        "description": "Alinhamento",
        "components": ["Alinhamento e balanceamento"],
        "km": 145000,
        "data": "20/05/2026",
        "total_cost": 120.0,
        "target_vehicle_ref": "BRA2E19",
        "vehicle_id": veh.id,
    }
    outcome_4 = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_maint_incoherent, [veh_after_3], now_ts
    )

    assert len(outcome_4["success"]) == 0
    assert len(outcome_4["failed"]) == 1
    assert outcome_4["failed"][0]["reason"] == "chronological_incoherence"
    assert _FAIL_REASONS_PT["chronological_incoherence"] == "quilometragem incompatível com o histórico cronológico de manutenções"

    # 6. Teste adversarial complementar:
    # Tenta inserir serviço em 15/08/2026 com 138.000 km (data posterior a 06/07/2026 mas com km menor que 140.000 km)
    payload_maint_incoherent_future = {
        "description": "Troca de palhetas",
        "components": ["Palhetas do limpador"],
        "km": 138000,
        "data": "15/08/2026",
        "total_cost": 80.0,
        "target_vehicle_ref": "BRA2E19",
        "vehicle_id": veh.id,
    }
    outcome_5 = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_maint_incoherent_future, [veh_after_3], now_ts
    )

    assert len(outcome_5["success"]) == 0
    assert len(outcome_5["failed"]) == 1
    assert outcome_5["failed"][0]["reason"] == "chronological_incoherence"

    # Total de manutenções no banco permanece 2
    final_maints = await maint_repo.get_by_vehicle_id(veh.id)
    assert len(final_maints) == 2


@pytest.mark.asyncio
async def test_retroactive_maintenance_conversation_draft_pipeline():
    """
    Testa o fluxo da IA / ConversationService:
    Garante que quando o usuário relata uma manutenção retroativa, a data é extraída
    e preservada no draft_payload e subsequentemente no committed_data ao responder 'sim'.
    """
    conv_service = ConversationService()
    user_id = "5511999997777"
    veh = Vehicle(id=101, user_id=user_id, make="Honda", model="Civic", plate="CIV9999", current_km=144000)

    # Turno 1: Envia mensagem informando manutenção retroativa
    turn1_res = await conv_service.handle_incoming_message(
        user_id=user_id,
        message_text="Fiz troca de óleo dia 20/05/2026 com 135000 km no Civic",
        vehicles=[veh],
        maintenances=[],
    )

    assert turn1_res["action_type"] == "awaiting_confirmation"
    draft = conv_service.get_context(user_id).draft_payload
    assert draft is not None
    assert draft.get("data") == "20/05/2026"
    assert draft.get("date_str") == "20/05/2026"
    assert draft.get("km") == 135000

    # Turno 2: Confirmação com 'sim'
    turn2_res = await conv_service.handle_incoming_message(
        user_id=user_id,
        message_text="sim",
        vehicles=[veh],
        maintenances=[],
    )

    assert turn2_res["action_type"] == "action_confirmed"
    committed = turn2_res.get("committed_data") or turn2_res.get("details")
    assert committed is not None
    assert committed.get("data") == "20/05/2026"
    assert committed.get("date_str") == "20/05/2026"
    assert committed.get("km") == 135000
    assert committed.get("vehicle_id") == 101


@pytest.mark.asyncio
async def test_higher_km_maintenance_updates_vehicle_current_km(async_db_session: AsyncSession):
    """
    Garante que uma manutenção com quilometragem MAIOR que a atual do veículo
    atualiza o hodômetro do veículo com sucesso.
    """
    user_id = "5511999996666"
    now_ts = datetime(2026, 11, 9, 14, 0, 0, tzinfo=timezone.utc)
    user_repo = UserRepository(async_db_session)
    veh_repo = VehicleRepository(async_db_session)
    await user_repo.get_or_create(user_id)

    veh = await veh_repo.add_vehicle(
        Vehicle(user_id=user_id, make="Toyota", model="Corolla", plate="TOY1A23", current_km=144000)
    )

    payload_higher_km = {
        "description": "Revisão dos 150k km",
        "components": ["Correia dentada", "Tensor"],
        "km": 150000,
        "data": "09/11/2026",
        "total_cost": 1200.0,
        "target_vehicle_ref": "TOY1A23",
        "vehicle_id": veh.id,
    }
    outcome = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_higher_km, [veh], now_ts
    )

    assert len(outcome["success"]) == 1
    assert len(outcome["failed"]) == 0

    updated_veh = await veh_repo.get_by_id(veh.id)
    assert updated_veh.current_km == 150000


@pytest.mark.asyncio
async def test_retroactive_date_aliases_and_formats(async_db_session: AsyncSession):
    """
    Garante que aliases variados de data (ISO '2026-04-10', 'service_date', 'data_manutencao')
    são devidamente reconhecidos e persistem com sucesso no histórico retroativo.
    """
    user_id = "5511999995555"
    now_ts = datetime(2026, 11, 9, 14, 0, 0, tzinfo=timezone.utc)
    user_repo = UserRepository(async_db_session)
    veh_repo = VehicleRepository(async_db_session)
    maint_repo = MaintenanceRepository(async_db_session)
    await user_repo.get_or_create(user_id)

    veh = await veh_repo.add_vehicle(
        Vehicle(user_id=user_id, make="Fiat", model="Pulse", plate="FIA1B34", current_km=100000)
    )

    # 1. Teste com formato ISO '2026-04-10' e chave 'service_date'
    payload_iso = {
        "description": "Troca de velas",
        "components": ["Jogo de velas"],
        "km": 80000,
        "service_date": "2026-04-10",
        "valor": 200.0,
        "vehicle_id": veh.id,
    }
    outcome_iso = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_iso, [veh], now_ts
    )
    assert len(outcome_iso["success"]) == 1

    # 2. Teste com chave 'data_manutencao' e formato '15 de janeiro de 2026'
    payload_txt = {
        "description": "Troca de óleo",
        "itens": ["Óleo sintético"],
        "km": 75000,
        "data_manutencao": "15 de janeiro de 2026",
        "total_cost": 250.0,
        "vehicle_id": veh.id,
    }
    outcome_txt = await _persist_confirmed_data(
        async_db_session, user_id, "action_confirmed", payload_txt, [veh], now_ts
    )
    assert len(outcome_txt["success"]) == 1

    # Valida que o km_atual não foi alterado em nenhum dos casos
    veh_check = await veh_repo.get_by_id(veh.id)
    assert veh_check.current_km == 100000

    # Valida que ambos os registros foram gravados no banco
    records = await maint_repo.get_by_vehicle_id(veh.id)
    assert len(records) == 2
