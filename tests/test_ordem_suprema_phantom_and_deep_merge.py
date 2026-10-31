"""
AutoCare V2 - Teste Obrigatório E2E: Ordem Suprema de Eliminação do Fantasma e Deep Merge

Valida integralmente o fluxo de 3 turnos no FastAPI (main:app):
- Turno 1: Relato inicial de manutenção antiga com data (15/02/2026), km (135.000) e 2 itens.
- Turno 2: Adição complementar de peça ("esqueci de um detalhe, também troquei a correia dentada nesse dia").
          Verifica proibição de confirmação precoce, preservação de data e km e merge dos 3 itens.
- Turno 3: Confirmação afirmativa ("sim") persistindo no banco de dados com data 15/02/2026, 135000 km e 3 itens.
"""

import uuid
import pytest
from datetime import date, datetime
from httpx import AsyncClient, ASGITransport

from main import app
from src.domain.entities import Vehicle
from src.domain.enums import ConversationState
from src.infrastructure.database.session import async_session_factory, init_db
from src.infrastructure.database.repositories import UserRepository, VehicleRepository, MaintenanceRepository
import src.interfaces.api.webhook as webhook_module


def _generate_test_uid() -> str:
    return f"5521{uuid.uuid4().int % 100000000:08d}"


@pytest.mark.asyncio
async def test_ordem_suprema_3_turnos_e2e_via_fastapi_app():
    """Simula o fluxo completo de 3 turnos chamando diretamente a rota HTTP /webhook do FastAPI."""
    await init_db()
    user_id = _generate_test_uid()

    # Cria o proprietário e o veículo na garagem
    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        veh_repo = VehicleRepository(session)
        await user_repo.get_or_create(user_id=user_id, name="Piloto Suprema")
        v = Vehicle(
            user_id=user_id,
            make="Fiat",
            model="Palio",
            year=2008,
            current_km=130000,
            plate="KXZ1234",
        )
        saved_v = await veh_repo.add_vehicle(v)
        veh_id = saved_v.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # =========================================================================
        # TURNO 1: Relato inicial de serviço antigo
        # =========================================================================
        msg_t1 = (
            "quero informar um serviço antigo\n"
            "foi no dia 15/02/2026, com 135.000 km. troquei o óleo de motor e o filtro de óleo"
        )
        resp1 = await client.post("/webhook", json={"user_id": user_id, "message": msg_t1})
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["status"] == "success"

        # Inspeciona rascunho em memória
        ctx1 = webhook_module.conversation_service.get_context(user_id)
        assert ctx1.state == ConversationState.AWAITING_MAINTENANCE_CONFIRM
        assert ctx1.draft_payload is not None
        draft1 = ctx1.draft_payload

        # Validações do Turno 1: data 15/02/2026, km 135.000 e 2 itens
        assert draft1.get("data") == "15/02/2026" or draft1.get("date_str") == "15/02/2026"
        assert draft1.get("km") == 135000 or draft1.get("odometer_km") == 135000
        items1 = draft1.get("itens") or draft1.get("components") or []
        assert any("óleo" in str(i).lower() or "oleo" in str(i).lower() for i in items1)
        assert any("filtro" in str(i).lower() for i in items1)
        assert "Posso confirmar" in data1["reply"] or "posso salvar" in data1["reply"].lower() or "confirmar" in data1["reply"].lower()

        # Garante que NENHUMA manutenção foi salva no banco ainda
        async with async_session_factory() as session:
            maints_turn1 = await MaintenanceRepository(session).get_by_user_id(user_id)
            assert len(maints_turn1) == 0

        # =========================================================================
        # TURNO 2: Adição complementar sem dizer 'Sim'
        # =========================================================================
        msg_t2 = "esqueci de um detalhe, também troquei a correia dentada nesse dia"
        resp2 = await client.post("/webhook", json={"user_id": user_id, "message": msg_t2})
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["status"] == "success"

        # Inspeciona rascunho em memória após o Turno 2
        ctx2 = webhook_module.conversation_service.get_context(user_id)
        assert ctx2.state == ConversationState.AWAITING_MAINTENANCE_CONFIRM
        assert ctx2.draft_payload is not None
        draft2 = ctx2.draft_payload

        # Validações do Turno 2 (ORDEM 2 e ORDEM 3):
        # 1. NÃO salvou prematuramente
        async with async_session_factory() as session:
            maints_turn2 = await MaintenanceRepository(session).get_by_user_id(user_id)
            assert len(maints_turn2) == 0, "Erro: Salvamento prematuro no Turno 2!"

        # 2. Preservou incondicionalmente a data 15/02/2026 e o km 135.000
        assert draft2.get("data") == "15/02/2026"
        assert draft2.get("date_str") == "15/02/2026"
        assert draft2.get("km") == 135000
        assert draft2.get("odometer_km") == 135000

        # 3. Mesclou os 3 itens juntos
        items2 = draft2.get("itens") or draft2.get("components") or []
        assert len(items2) >= 3
        items2_str = " ".join(items2).lower()
        assert "correia dentada" in items2_str
        assert "óleo" in items2_str or "oleo" in items2_str
        assert "filtro" in items2_str

        # =========================================================================
        # TURNO 3: Confirmação afirmativa pura
        # =========================================================================
        msg_t3 = "sim"
        resp3 = await client.post("/webhook", json={"user_id": user_id, "message": msg_t3})
        assert resp3.status_code == 200
        data3 = resp3.json()
        assert data3["status"] == "success"

        # Estado volta a NORMAL
        ctx3 = webhook_module.conversation_service.get_context(user_id)
        assert ctx3.state == ConversationState.NORMAL
        assert ctx3.draft_payload is None

        # Validação no Banco de Dados (ORDEM 3):
        async with async_session_factory() as session:
            maints_final = await MaintenanceRepository(session).get_by_user_id(user_id)
            assert len(maints_final) == 1, "Manutenção deve estar persistida no banco!"
            saved_m = maints_final[0]

            # Data exata convertida para date(2026, 2, 15)
            assert saved_m.date == date(2026, 2, 15), f"Data esperada 2026-02-15 mas foi salva: {saved_m.date}"
            # Hodômetro salvo 135.000 km
            assert saved_m.km == 135000, f"KM esperado 135000 mas foi salvo: {saved_m.km}"
            # Todos os 3 itens gravados
            comps_saved = [c.lower() for c in saved_m.components]
            comps_str = " ".join(comps_saved)
            assert "correia dentada" in comps_str
            assert "filtro" in comps_str
            assert "óleo" in comps_str or "oleo" in comps_str


@pytest.mark.asyncio
async def test_ausencia_total_de_strings_fantasmas():
    """Garante que as frases legadas do AutoCare V1 nunca sejam geradas pela aplicação."""
    phantom_phrases = [
        "Serviços e Checagens identificados",
        "Histórico Oficial do",
        "Posso salvar e oficializar",
        "Data: Ausente",
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Testa rotas HTTP principais
        for endpoint in ["/webhook", "/webhook/whatsapp", "/zapi", "/evolution"]:
            resp = await client.get(endpoint)
            assert resp.status_code == 200
            for phrase in phantom_phrases:
                assert phrase.lower() not in resp.text.lower()
