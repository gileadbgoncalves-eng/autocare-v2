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
