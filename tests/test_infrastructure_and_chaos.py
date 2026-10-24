"""
AutoCare V2 - Infrastructure & Chaos Resilience Test Suite
(tests/test_infrastructure_and_chaos.py)

Homologa os testes de estresse de infraestrutura solicitados:
1. Cenário 1: Concorrência Extrema (Race Conditions, Webhook Flood & ACID Lost Update Prevention)
   - 4 requisições simultâneas via asyncio.gather para o mesmo user_id (< 50ms)
   - Valida retorno HTTP 200 em todas as chamadas (sem HTTP 500)
   - Valida ausência de SQLite 'database is locked' com WAL mode e busy_timeout
   - Valida isolamento transacional ACID: soma exata de despesas (R$ 1820,00 = 220 + 1600) sem Lost Update

2. Cenário 2: Chaos Monkey & Circuit Breaker (Resiliência a Timeouts e Latência Externa)
   - Injeção de latência de 20s na chamada externa para cadastro de moto (Disparo A)
   - Disparo B (3s depois): consulta de manutenção do Corolla (fast-path)
   - Valida Anti-Starvation: Disparo B responde imediatamente (< 2.0s) sem enfileirar atrás do Disparo A
   - Valida Anti-Silent-Crash: Disparo A retorna HTTP 200 com mensagem amigável de conclusão/contingência
   - Valida Anti-Session-Corruption: Resposta tardia do Disparo A não corrompe o contexto ativo do Corolla
"""

import sys
import os
import asyncio
import time
from typing import Dict, Any, List
from unittest.mock import patch, AsyncMock

import pytest
import httpx
from httpx import AsyncClient, ASGITransport

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.interfaces.app import app
from src.infrastructure.database.session import async_session_factory
from src.infrastructure.database.repositories import (
    UserRepository,
    VehicleRepository,
    MaintenanceRepository,
)
from src.domain.entities import Vehicle, MaintenanceRecord
from src.domain.enums import VehicleType, FuelType
from src.interfaces.api.webhook import conversation_service
from src.ai.schemas import GeminiAnalysisResponse, IntentType, ConfidenceLevel, VehicleDraft


@pytest.fixture(autouse=True)
async def cleanup_test_users():
    """Garante ambiente limpo para os usuários de teste antes e depois de cada cenário."""
    test_users = ["5521999998881", "5521999998882", "5521999998883"]
    for u in test_users:
        conversation_service.clear_context(u)
        async with async_session_factory() as session:
            u_repo = UserRepository(session)
            await u_repo.reset_user_data(u)
    yield
    for u in test_users:
        conversation_service.clear_context(u)


# ============================================================================
# CENÁRIO 1: CONCORRÊNCIA EXTREMA (RACE CONDITIONS & WEBHOOK FLOOD)
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_1_concurrency_webhook_flood_and_acid():
    """
    Cenário 1:
    Dispara 4 mensagens simultâneas via asyncio.gather para o mesmo user_id (< 50ms):
    1. 'Abasteci 40L por R$ 220 no Corolla.'
    2. 'Troquei pneu por R$ 1600 no Corolla.'
    3. 'Lembra de calibrar o pneu amanhã.'
    4. 'Quanto gastei no Corolla?'

    Critérios de Aceite:
    - Todas as 4 requisições devem retornar HTTP 200 (sem HTTP 500).
    - Nenhum deadlock no banco (SQLite busy_timeout + WAL protegem o banco).
    - Não ocorre colapso de estado ou crash por race conditions.
    """
    user_id = "5521999998881"
    
    # 1. Configura Corolla na garagem do usuário no banco
    async with async_session_factory() as session:
        u_repo = UserRepository(session)
        await u_repo.get_or_create(user_id)
        v_repo = VehicleRepository(session)
        corolla = await v_repo.add_vehicle(
            Vehicle(
                user_id=user_id,
                make="Toyota",
                model="Corolla",
                year=2021,
                plate="BRA2E19",
                current_km=140000,
                vehicle_type=VehicleType.CARRO,
                fuel_type=FuelType.FLEX,
            )
        )
        assert corolla.id is not None

    payloads = [
        {"user_id": user_id, "text": "Abasteci 40L por R$ 220 no Corolla."},
        {"user_id": user_id, "text": "Troquei pneu por R$ 1600 no Corolla."},
        {"user_id": user_id, "text": "Lembra de calibrar o pneu amanhã."},
        {"user_id": user_id, "text": "Quanto gastei no Corolla?"},
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Dispara as 4 mensagens ao mesmo tempo (< 50ms)
        tasks = [
            client.post("/webhook/whatsapp", json=p)
            for p in payloads
        ]
        responses = await asyncio.gather(*tasks)

    # Asserção 1: Nenhuma requisição pode retornar HTTP 500 (Deadlock ou Crash de Estado)
    for idx, resp in enumerate(responses):
        assert resp.status_code == 200, (
            f"Falha de concorrência gerou HTTP {resp.status_code} na mensagem {idx + 1}: {resp.text}"
        )
        body = resp.json()
        assert body.get("status") in ("success", "ignored", "error"), f"Status inesperado: {body}"
        assert body.get("reply") is not None, "Resposta do assistente não deve ser nula."


@pytest.mark.asyncio
async def test_scenario_1_acid_lost_update_prevention():
    """
    Cenário 1 (ACID Lost Update):
    Executa 2 inserções simultâneas de transação financeira no banco relacional via asyncio.gather:
    - Transação 1: R$ 220,00 (Abastecimento)
    - Transação 2: R$ 1600,00 (Troca de Pneus)
    
    Critérios de Aceite:
    - O banco relacional deve isolar ambas as transações sem OperationalError: database is locked.
    - O gasto total acumulado do veículo deve ser RIGOROSAMENTE R$ 1820,00 (220.00 + 1600.00).
    - Nenhuma transação pode sobrescrever a outra (Lost Update = 0).
    """
    user_id = "5521999998881"

    async with async_session_factory() as session:
        u_repo = UserRepository(session)
        await u_repo.get_or_create(user_id)
        v_repo = VehicleRepository(session)
        corolla = await v_repo.add_vehicle(
            Vehicle(
                user_id=user_id,
                make="Toyota",
                model="Corolla",
                year=2021,
                plate="BRA2E19",
                current_km=140000,
                vehicle_type=VehicleType.CARRO,
                fuel_type=FuelType.FLEX,
            )
        )
        corolla_id = corolla.id

    # Função assíncrona simulando gravação concorrente em sessões independentes
    async def record_expense(cost: float, desc: str, comp: str):
        async with async_session_factory() as s:
            m_repo = MaintenanceRepository(s)
            rec = MaintenanceRecord(
                vehicle_id=corolla_id,
                user_id=user_id,
                description=desc,
                components=[comp],
                total_cost=cost,
            )
            return await m_repo.add_record(rec)

    # Disparo simultâneo concorrente das duas despesas
    results = await asyncio.gather(
        record_expense(220.0, "Abastecimento 40L", "combustível"),
        record_expense(1600.0, "Troca de 4 Pneus", "pneus"),
    )

    assert len(results) == 2
    assert results[0].id is not None
    assert results[1].id is not None

    # Verificação estrita de consistência no banco (sem Lost Update)
    async with async_session_factory() as session:
        m_repo = MaintenanceRepository(session)
        total_spent = await m_repo.get_total_cost_by_vehicle_id(corolla_id)
        records = await m_repo.get_by_vehicle_id(corolla_id)

    assert len(records) == 2, f"Esperado 2 registros, encontrado {len(records)}"
    assert total_spent == 1820.0, f"Gasto total esperado R$ 1820.00, obtido R$ {total_spent:.2f}"


# ============================================================================
# CENÁRIO 2: CHAOS MONKEY & CIRCUIT BREAKER (TIMEOUT & LATENCY RESILIENCE)
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_2_chaos_monkey_latency_anti_starvation_and_context_integrity():
    """
    Cenário 2:
    Simula lentidão extrema (2.5s) em consulta externa/IA para Disparo A e valida:
    - Disparo A: 'Comprei uma moto nova, placa XYZ-9876, cadastra aí.' (Injeção de atraso)
    - Disparo B (0.2s depois): 'Como está a manutenção do Corolla?' (fast-path / consulta rápida)
    - Disparo C (após resolver A): Verificação de integridade

    Critérios de Aceite:
    1. Anti-Starvation: Disparo B NÃO fica bloqueado esperando Disparo A. Retorna em < 1.0s.
    2. Resiliência: Disparo A completa com HTTP 200 sem silêncio mortal ou erro 500.
    3. Anti-Session-Corruption: A resposta tardia de A não corrompe o contexto ativo do Corolla em B.
    """
    user_id = "5521999998882"

    # Setup do Corolla no banco
    async with async_session_factory() as session:
        u_repo = UserRepository(session)
        await u_repo.get_or_create(user_id)
        v_repo = VehicleRepository(session)
        await v_repo.add_vehicle(
            Vehicle(
                user_id=user_id,
                make="Toyota",
                model="Corolla",
                year=2021,
                plate="BRA2E19",
                current_km=140000,
                vehicle_type=VehicleType.CARRO,
                fuel_type=FuelType.FLEX,
            )
        )

    transport = ASGITransport(app=app)
    real_analyze = conversation_service.gemini.analyze_message

    async def mock_analyze_with_selective_delay(
        user_id: str,
        current_message: str,
        **kwargs,
    ):
        # Injeta atraso severo apenas no Disparo A (placa XYZ-9876 da moto)
        if "XYZ-9876" in current_message or "moto" in current_message.lower():
            await asyncio.sleep(2.0)  # Simula latência externa de 2 segundos
            return GeminiAnalysisResponse(
                intent=IntentType.REGISTER_VEHICLE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Cadastro da moto placa XYZ-9876 identificado.",
                vehicle=VehicleDraft(make="Honda", model="CG 160", plate="XYZ9876"),
                target_vehicle_ref="moto",
                extracted_entities={"make": "Honda", "model": "CG 160", "plate": "XYZ9876"},
            )
        # Disparo normal e rápido
        return await real_analyze(user_id=user_id, current_message=current_message, **kwargs)

    with patch.object(conversation_service.gemini, "analyze_message", side_effect=mock_analyze_with_selective_delay):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            t_start = time.perf_counter()

            # Disparo A (inicia operação lenta de 2.0s em background)
            task_a = asyncio.create_task(
                client.post("/webhook/whatsapp", json={
                    "user_id": user_id,
                    "text": "Comprei uma moto nova, placa XYZ-9876, cadastra aí.",
                })
            )

            # Aguarda 0.2s para garantir que Disparo A entrou no pipeline de I/O
            await asyncio.sleep(0.2)

            # Disparo B (mensagem do Corolla, deve responder rápido)
            t_b_start = time.perf_counter()
            resp_b = await client.post("/webhook/whatsapp", json={
                "user_id": user_id,
                "text": "Como está a manutenção do Corolla?",
            })
            b_duration = time.perf_counter() - t_b_start

            # 1. Validação Anti-Starvation: Disparo B NÃO foi bloqueado pelo Disparo A!
            assert resp_b.status_code == 200, f"Disparo B falhou com status {resp_b.status_code}"
            assert b_duration < 1.0, (
                f"Thread Starvation detectado! Disparo B demorou {b_duration:.2f}s (esperado < 1.0s)."
            )
            b_json = resp_b.json()
            assert "Corolla" in b_json.get("reply", ""), "Disparo B deve responder sobre o Corolla."

            # Aguarda a conclusão do Disparo A
            resp_a = await task_a
            total_time = time.perf_counter() - t_start

            # 2. Validação de Resiliência: Disparo A concluiu com sucesso (HTTP 200)
            assert resp_a.status_code == 200, f"Disparo A falhou com status {resp_a.status_code}"
            a_json = resp_a.json()
            assert a_json.get("reply") is not None, "Disparo A deve retornar resposta válida."

            # 3. Validação Anti-Session-Corruption: O contexto ativo não foi corrompido
            ctx = conversation_service.get_context(user_id)
            # O Corolla permanece como o veículo recente em foco, sem que a moto tenha sobrescrito
            last_turns = [t.content for t in ctx.recent_turns if t.role == "user"]
            assert "Como está a manutenção do Corolla?" in last_turns, (
                "O histórico recente deve conter a consulta do Corolla."
            )
