"""
AutoCare V2 - Testes Automatizados de Refatoração QA (4 Problemas Críticos)
Valida a resolução dos 4 problemas identificados nos testes de integração:
1. Extração de Entidades Temporais e Locais (Data absoluta DD/MM/YYYY e workshop_name estrito/None)
2. Alerta Enxuto de KM e Multi-Intenção ("sim, me lembra amanhã às 8h" sem menus)
3. Consulta Global da Garagem (Visão consolidada de todos os carros)
4. Fuzzy Matching e Troca Imediata de Contexto ("sandeiro" -> Sandero)
"""

import pytest
import pytest_asyncio
from typing import List

from src.ai.schemas import MaintenanceDraft, GeminiAnalysisResponse
from src.application.services.conversation import ConversationService
from src.domain.entities import Vehicle, MaintenanceRecord, Reminder, ConversationContext
from src.domain.enums import VehicleType, FuelType, ConversationState, IntentType, ConfidenceLevel
from src.repositories.veiculo_repository import VeiculoRepository
from src.database import async_session_factory, Base, engine


# ============================================================================
# PROBLEMA 1: EXTRAÇÃO DE ENTIDADES TEMPORAIS E LOCAIS
# ============================================================================

def test_problema_1_workshop_name_generico_vira_none():
    """Valida que termos circunstanciais/genéricos como 'época' sejam estritamente convertidos para None."""
    # Cenário reportado no QA: "44000 km na época" -> extraiu "época"
    draft1 = MaintenanceDraft(workshop_name="época")
    assert draft1.workshop_name is None, f"Esperava None, obteve: {draft1.workshop_name}"

    draft2 = MaintenanceDraft(workshop_name="na época")
    assert draft2.workshop_name is None

    draft3 = MaintenanceDraft(workshop_name="lugar")
    assert draft3.workshop_name is None

    draft4 = MaintenanceDraft(workshop_name="rua")
    assert draft4.workshop_name is None

    draft5 = MaintenanceDraft(workshop_name="tempo")
    assert draft5.workshop_name is None

    # Nomes próprios legítimos de estabelecimentos DEVEM ser preservados
    draft_valido = MaintenanceDraft(workshop_name="Oficina do Zé")
    assert draft_valido.workshop_name == "Oficina do Zé"

    draft_valido2 = MaintenanceDraft(workshop_name="Auto Center Porto Seguro")
    assert draft_valido2.workshop_name == "Auto Center Porto Seguro"


def test_problema_1_data_absoluta_priorizada_e_formatada_dd_mm_yyyy():
    """Valida que datas absolutas sobreponham expressões relativas e sejam normalizadas para DD/MM/YYYY."""
    # Cenário reportado no QA: "mês passado, no dia 15 de setembro"
    draft1 = MaintenanceDraft(date_str="mês passado, no dia 15 de setembro")
    assert draft1.date_str == "15/09/2026", f"Esperava 15/09/2026, obteve: {draft1.date_str}"

    draft2 = MaintenanceDraft(date_str="15 de setembro")
    assert draft2.date_str == "15/09/2026"

    draft3 = MaintenanceDraft(date_str="dia 05 de outubro de 2025")
    assert draft3.date_str == "05/10/2025"

    draft4 = MaintenanceDraft(date_str="15/09")
    assert draft4.date_str == "15/09/2026"

    draft5 = MaintenanceDraft(date_str="20/11/2024")
    assert draft5.date_str == "20/11/2024"


# ============================================================================
# PROBLEMA 2: UX ENGESSADA, ALERTA ENXUTO E MULTI-INTENÇÃO
# ============================================================================

@pytest.mark.asyncio
async def test_problema_2_alerta_enxuto_pre_confirmacao_km_excedido():
    """Valida a atualização silenciosa e direta de hodômetro com alerta preventivo quando o odômetro ultrapassa a revisão."""
    service = ConversationService()
    user_id = "5521999990001"
    vehicles = [
        Vehicle(id=1, user_id=user_id, make="Honda", model="Civic", current_km=40000)
    ]

    # Usuário informa odômetro com 44000 km (passou da revisão de 40000 km em 4000 km)
    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="estou com 44000 km agora",
        vehicles=vehicles
    )

    # Nova regra: Atualização de hodômetro isolada NÃO cria draft e NÃO pede "Posso confirmar?"
    assert res["action_type"] in ("odometer_updated", "update_odometer")
    assert res["state"] == ConversationState.NORMAL.value
    assert "posso confirmar" not in res["reply"].lower()
    assert "ALERTA" in res["reply"]
    assert "4.000 km" in res["reply"] or "4000 km" in res["reply"]
    assert res["committed_data"]["odometer_km"] == 44000


@pytest.mark.asyncio
async def test_problema_2_multi_intencao_confirmacao_com_lembrete_sem_menu():
    """Valida resposta a 'sim, me lembra amanhã às 8h' confirmando draft e criando lembrete sem menus burocráticos."""
    service = ConversationService()
    user_id = "5521999990002"
    vehicles = [
        Vehicle(id=1, user_id=user_id, make="Honda", model="Civic", current_km=40000)
    ]

    # 1. Usuário envia manutenção que requer confirmação
    await service.handle_incoming_message(
        user_id=user_id,
        message_text="troquei pastilhas de freio do civic por 450 reais com 44000 km",
        vehicles=vehicles
    )
    ctx = service.get_context(user_id)
    assert ctx.is_awaiting_confirmation() is True

    # 2. Usuário responde com intenção múltipla: confirmação + agendamento
    res_multi = await service.handle_incoming_message(
        user_id=user_id,
        message_text="sim, me lembra amanhã às 8h",
        vehicles=vehicles
    )

    assert res_multi["action_type"] == "action_confirmed_with_chained_reminder"
    assert res_multi["state"] == ConversationState.NORMAL.value
    # Resposta deve conter confirmação da manutenção e confirmação do lembrete pontual
    assert "Lembrete agendado" in res_multi["reply"]
    assert "08:00" in res_multi["reply"]
    # Não deve cuspir menu de opções (1, 2, 3)
    assert "1 - " not in res_multi["reply"]
    assert "2 - " not in res_multi["reply"]

    # Valida que o payload committed contém os dois dados
    committed = res_multi["committed_data"]
    assert committed.get("odometer_km") == 44000 or committed.get("km") == 44000
    assert "chained_reminder" in committed
    assert committed["chained_reminder"]["explicit_time"] == "08:00"


# ============================================================================
# PROBLEMA 3: CONSULTA GLOBAL (VISÃO GERAL DA GARAGEM)
# ============================================================================

@pytest.mark.asyncio
async def test_problema_3_consulta_global_retorna_todos_os_carros_da_garagem():
    """Valida que pergunta sobre manutenções atrasadas para os carros retorna o status de TODOS os veículos cadastrados."""
    service = ConversationService()
    user_id = "5521999990003"
    vehicles = [
        Vehicle(id=1, user_id=user_id, make="Honda", model="Civic", plate="ABC1D23", current_km=40000),
        Vehicle(id=2, user_id=user_id, make="Renault", model="Sandero", plate="XYZ9876", current_km=44000),
    ]

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="Tem alguma manutenção atrasada para os meus carros?",
        vehicles=vehicles
    )

    assert res["action_type"] == "garage_checkup"
    reply = res["reply"]
    # Deve conter menção explícita a AMBOS os carros da garagem
    assert "Civic" in reply
    assert "Sandero" in reply
    # Sandero com 44.000 km deve acusar atraso da revisão de 40.000 em 4.000 km
    assert "44.000 km" in reply or "44000 km" in reply
    assert "ATRASADA" in reply
    # Civic com 40.000 km está em dia
    assert "EM DIA" in reply or "em dia" in reply.lower()


# ============================================================================
# PROBLEMA 4: FUZZY MATCHING E TROCA IMEDIATA DE CONTEXTO
# ============================================================================

def test_problema_4_fuzzy_matching_resolucao_sandeiro_para_sandero():
    """Valida que 'sandeiro' (erro de digitação) seja resolvido para 'Sandero' via SequenceMatcher."""
    service = ConversationService()
    vehicles = [
        Vehicle(id=1, user_id="123", make="Honda", model="Civic", plate="ABC1D23"),
        Vehicle(id=2, user_id="123", make="Renault", model="Sandero", plate="XYZ9876"),
    ]

    matched = service._resolve_vehicle_fuzzy("o sandeiro está em dia?", vehicles)
    assert matched is not None
    assert matched.model == "Sandero"
    assert matched.id == 2


@pytest.mark.asyncio
async def test_problema_4_troca_imediata_de_contexto_em_memoria():
    """Valida que citar 'sandeiro' troca o active_vehicle_id imediatamente e responde sobre o Sandero, não sobre o Civic anterior."""
    service = ConversationService()
    user_id = "5521999990004"
    vehicles = [
        Vehicle(id=1, user_id=user_id, make="Honda", model="Civic", plate="ABC1D23", current_km=40000),
        Vehicle(id=2, user_id=user_id, make="Renault", model="Sandero", plate="XYZ9876", current_km=44000),
    ]

    # Contexto estava previamente focado no Civic (id 1)
    ctx = service.get_context(user_id)
    ctx.active_vehicle_id = 1
    service.update_context(ctx)

    # Usuário pergunta: "o sandeiro está em dia?"
    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text="o sandeiro está em dia?",
        vehicles=vehicles
    )

    # 1. Verifica se o active_vehicle_id trocou imediatamente para 2 (Sandero)
    updated_ctx = service.get_context(user_id)
    assert updated_ctx.active_vehicle_id == 2, f"Esperava 2 (Sandero), obteve: {updated_ctx.active_vehicle_id}"

    # 2. A resposta deve ser sobre o Sandero, NUNCA sobre o Civic
    reply = res["reply"]
    assert "Sandero" in reply
    assert "Civic" not in reply
    # Deve reportar dados do Sandero (44.000 km)
    assert "44.000 km" in reply or "44000 km" in reply
