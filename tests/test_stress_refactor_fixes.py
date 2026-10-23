"""
AutoCare V2 - Testes de Estresse e Refatoração (Stress Test Fixes)
Valida a resolução dos 3 problemas críticos de UX e Extração de Entidades:
1. "Visão de Túnel": Extração e processamento de múltiplas ações/veículos na mesma mensagem (List[ActionDraft])
2. Validação Rígida: Classificação semântica (NLP) nas confirmações ("foi um erro", "pode meter marcha", "isso aí")
3. Conflito de Entidades: Placa vs Nome do Carro ("Compass placa RIO2A24" onde RIO2A24 é Sandero no banco)
"""

import pytest
from src.ai.schemas import ActionDraft, GeminiAnalysisResponse
from src.application.services.conversation import ConversationService
from src.domain.entities import Vehicle, MaintenanceRecord, ConversationContext
from src.domain.enums import ConversationState, IntentType


# ============================================================================
# PROBLEMA 1: VISÃO DE TÚNEL (MÚLTIPLAS AÇÕES / MÚLTIPLOS VEÍCULOS)
# ============================================================================

@pytest.mark.asyncio
async def test_problema_1_multiplas_acoes_multiplos_veiculos_mesma_mensagem():
    """
    Cenário: 'Fiz revisão no Civic e atualiza o km do Sandero para 45000 km'
    Valida que o bot não ignora o segundo carro, gerando rascunho em lote com
    confirmação agrupada e persistência de ambas as ações.
    """
    service = ConversationService()
    user_id = "5521999990002"
    vehicles = [
        Vehicle(id=1, user_id=user_id, make="Honda", model="Civic", plate="CIV1C00", current_km=40000),
        Vehicle(id=2, user_id=user_id, make="Renault", model="Sandero", plate="RIO2A24", current_km=42000),
    ]

    # Turno 1: Usuário envia duas ações distintas para dois carros
    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Fiz revisão no Civic e atualiza o km do Sandero para 45000 km",
        vehicles=vehicles
    )

    assert res1["action_type"] == "awaiting_confirmation"
    draft = res1.get("draft", {})
    assert draft.get("_is_multi_action") is True
    actions = draft.get("actions", [])
    assert len(actions) == 2, f"Esperava 2 ações extraídas, obteve {len(actions)}"

    # Verifica se os dois veículos foram identificados
    refs = [a.get("target_vehicle_ref") for a in actions]
    assert "Civic" in refs
    assert "Sandero" in refs

    # Mensagem de pré-confirmação deve listar ambos
    assert "Civic" in res1["reply"]
    assert "Sandero" in res1["reply"]
    assert "45.000 km" in res1["reply"]

    # Turno 2: Confirmação semântica coloquial ("pode meter marcha")
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="pode meter marcha",
        vehicles=vehicles
    )

    assert res2["action_type"] == "action_confirmed"
    assert "Todas as ações foram confirmadas e salvas com sucesso!" in res2["reply"]
    assert "Civic" in res2["reply"]
    assert "Sandero" in res2["reply"]
    assert res2["committed_data"].get("_is_multi_action") is True


# ============================================================================
# PROBLEMA 2: VALIDAÇÃO RÍGIDA (NLP NAS CONFIRMAÇÕES / CANCELAMENTOS)
# ============================================================================

@pytest.mark.asyncio
async def test_problema_2_retrocesso_odometro_nlp_cancelamento_foi_um_erro():
    """
    Cenário: O sistema detecta retrocesso de km (30000 km quando o carro está com 45000 km).
    O usuário responde 'foi um erro'. O bot deve entender semanticamente como cancelamento
    e descartar a operação sem repetir rigidamente a pergunta.
    """
    service = ConversationService()
    user_id = "5521999990003"
    vehicles = [
        Vehicle(id=1, user_id=user_id, make="Honda", model="Civic", current_km=45000)
    ]

    # Turno 1: Odômetro inferior ao atual
    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="odômetro está com 30000 km",
        vehicles=vehicles
    )

    assert res1["action_type"] == "awaiting_confirmation"
    assert "30.000 km" in res1["reply"]
    assert "menor que a registrada anteriormente (45.000 km)" in res1["reply"]
    assert "Foi um erro de digitação?" in res1["reply"]

    # Turno 2: Usuário responde 'foi um erro' (não digitou 'Não' estrito)
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="foi um erro",
        vehicles=vehicles
    )

    assert res2["action_type"] == "action_cancelled"
    assert "cancelei a operação" in res2["reply"]
    assert service.get_context(user_id).state == ConversationState.NORMAL
    assert service.get_context(user_id).draft_payload is None


@pytest.mark.asyncio
async def test_problema_2_nlp_confirmacoes_e_cancelamentos_variados():
    """Valida a tradução de respostas coloquiais pelo classificador semântico NLP."""
    service = ConversationService()

    # Confirmações coloquiais devem mapear para True
    assert await service.classify_confirmation("pode meter marcha") is True
    assert await service.classify_confirmation("manda bala") is True
    assert await service.classify_confirmation("isso aí") is True
    assert await service.classify_confirmation("isso ai") is True
    assert await service.classify_confirmation("com certeza") is True
    assert await service.classify_confirmation("positivo") is True
    assert await service.classify_confirmation("fechado") is True

    # Cancelamentos e erros devem mapear para False
    assert await service.classify_confirmation("foi um erro") is False
    assert await service.classify_confirmation("foi erro") is False
    assert await service.classify_confirmation("foi engano") is False
    assert await service.classify_confirmation("errei") is False
    assert await service.classify_confirmation("me confundi") is False
    assert await service.classify_confirmation("cancela") is False
    assert await service.classify_confirmation("esquece") is False
    assert await service.classify_confirmation("deixa quieto") is False


# ============================================================================
# PROBLEMA 3: CONFLITO DE ENTIDADES (PLACA VS NOME DO CARRO)
# ============================================================================

@pytest.mark.asyncio
async def test_problema_3_conflito_entidades_placa_vs_nome_carro_aviso_obrigatorio():
    """
    Cenário: Usuário diz 'Compass placa RIO2A24'. No banco, RIO2A24 pertence a um Sandero.
    O sistema DEVE emitir o aviso:
    'Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero.'
    """
    service = ConversationService()
    user_id = "5521999990004"
    vehicles = [
        Vehicle(id=1, user_id=user_id, make="Renault", model="Sandero", plate="RIO2A24", current_km=40000)
    ]

    # 1. Teste direto do método de resolução veicular
    matched, warning = service._resolve_vehicle("Compass placa RIO2A24", vehicles)
    assert matched is not None
    assert matched.model == "Sandero"
    assert warning == "Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero."

    # 2. Teste do fluxo conversacional completo com aviso emitido
    res1 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Compass placa RIO2A24 45000 km",
        vehicles=vehicles
    )

    assert res1["action_type"] == "awaiting_confirmation"
    assert "Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero." in res1["reply"]

    # 3. Na confirmação, o aviso deve acompanhar o feedback de sucesso
    res2 = await service.handle_incoming_message(
        user_id=user_id,
        message_text="isso aí",
        vehicles=vehicles
    )

    assert res2["action_type"] == "action_confirmed"
    assert "Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero." in res2["reply"]
    assert "Atualização do hodômetro com 45.000 km confirmada e salva" in res2["reply"]


@pytest.mark.asyncio
async def test_problema_3_sem_conflito_quando_modelo_e_placa_concordam():
    """Valida que nenhum aviso de divergência seja emitido se a placa e o modelo concordarem."""
    service = ConversationService()
    vehicles = [
        Vehicle(id=1, user_id="u1", make="Renault", model="Sandero", plate="RIO2A24", current_km=40000)
    ]

    matched, warning = service._resolve_vehicle("Sandero placa RIO2A24", vehicles)
    assert matched is not None
    assert matched.model == "Sandero"
    assert warning is None
