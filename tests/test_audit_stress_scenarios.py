"""
Test Suite: Auditoria de Cenários Adversariais, Chained Intents e Casos de Borda (V2.9.0)
Homologa os 5 cenários críticos de estresse solicitados:
1. Cadastro Caótico e Consulta Global (Check-up + Placa Amarela AB-0123 + Placa Mercosul Inválida HUE-9999)
2. Sobrecarga de Especificações e Resolução Temporal (Civic Touring + Data Ontem + Revisão 2 Semanas + Filtro de Oficina)
3. Quebra de Contexto e Gatilho de Alerta (Cancela Civic + Corolla 150.000 km + Alerta de Revisão Pesada + Dúvida Data Óleo)
4. Validação de Borda e Edge Cases (Bloqueio de Hodômetro Negativo -50 km + Lembrete 6 meses e meio)
5. Teste de Colapso Absurdo (Ignora tudo + 31 de Fevereiro + Ano Passado + Relatório da Garagem)
"""

import pytest
from src.application.services.conversation import ConversationService
from src.domain.enums import ConversationState
from src.domain.entities import Vehicle
from src.domain.enums import VehicleType, FuelType


@pytest.fixture
def corolla_vehicle():
    return Vehicle(
        id=1,
        user_id="5521999990001",
        make="Toyota",
        model="Corolla",
        year=2021,
        plate="BRA2E19",
        current_km=140000,
        vehicle_type=VehicleType.CARRO,
        fuel_type=FuelType.FLEX,
    )


@pytest.mark.asyncio
async def test_scenario_1_cadastro_caotico_e_consulta_global(corolla_vehicle):
    """
    Cenário 1:
    - Chained Intents: Realiza o check-up da garagem AND inicia os cadastros simultaneamente.
    - Validação de Regras:
      * Placa amarela (AB-0123): Identifica formato antigo de 2 letras.
      * Placa Mercosul inválida (HUE-9999): Rejeita aceitação silenciosa como Mercosul e aponta que é cinza.
    """
    service = ConversationService()
    user_id = "5521999990001"
    msg = "Me dá um check-up geral da garagem. E já aproveita e cadastra minha Brasília 76, placa amarela AB-0123, e um Fusca com placa Mercosul HUE-9999."

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text=msg,
        vehicles=[corolla_vehicle],
    )

    assert res["action_type"] == "awaiting_confirmation"
    assert res["state"] == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
    reply = res["reply"]

    # 1. Check-up da garagem presente
    assert "Check-up Geral da Garagem" in reply
    assert "Toyota Corolla" in reply

    # 2. Identificação da Brasília e placa amarela
    assert "Brasília" in reply
    assert "AB0123" in reply
    assert "placa amarela" in reply.lower() or "2 letras" in reply

    # 3. Identificação do Fusca e erro de placa Mercosul
    assert "Fusca" in reply
    assert "HUE9999" in reply
    assert "não é padrão mercosul" in reply.lower() or "formato antigo cinza" in reply.lower()


@pytest.mark.asyncio
async def test_scenario_2_sobrecarga_especificacoes_e_resolucao_temporal(corolla_vehicle):
    """
    Cenário 2:
    - Entity Extraction: Captura Honda Civic, Touring, 1.5 Turbo, CVT, 2024.
    - Lógica Temporal: "ontem de manhã" e "daqui a duas semanas" normalizadas em DD/MM/YYYY.
    - Extração de Local: Filtra ruído ("lá naquele lugar", "da esquina") -> estritamente "Oficina do Zé".
    """
    service = ConversationService()
    user_id = "5521999990001"
    msg = "Ah, esqueci, comprei também um Civic Touring 1.5 Turbo CVT 2024 ontem de manhã. Já agenda a primeira revisão dele pra daqui a duas semanas, lá naquele lugar, na Oficina do Zé da esquina."

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text=msg,
        vehicles=[corolla_vehicle],
    )

    assert res["action_type"] == "awaiting_confirmation"
    assert res["state"] == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
    reply = res["reply"]

    # Valida especificações
    assert "Civic" in reply
    assert "Touring" in reply
    assert "1.5 Turbo" in reply
    assert "CVT" in reply

    # Valida filtro estrito de oficina (sem ruídos)
    assert "Oficina do Zé" in reply
    assert "lá naquele lugar" not in reply
    assert "da esquina" not in reply

    # Valida integridade do draft
    draft = res["draft"]
    assert draft["workshop_name"] == "Oficina do Zé"
    assert "/" in draft["purchase_date"]
    assert "/" in draft["scheduled_review_date"]


@pytest.mark.asyncio
async def test_scenario_3_quebra_de_contexto_e_gatilho_de_alerta(corolla_vehicle):
    """
    Cenário 3:
    - Context Switching: Cancela revisão do Civic e muda foco para Corolla (não grava como manutenção!).
    - Business Logic: 150.000 km dispara alerta de revisão pesada (correia, fluidos, velas, amortecedores).
    - Ambiguidade: Não aceita datas conflitantes e solicita confirmação da data exata.
    """
    service = ConversationService()
    user_id = "5521999990001"
    msg = "Espera aí, cancela a revisão do Civic. O meu Corolla acabou de bater 150.000 km agora. Quando foi a última troca de óleo dele? Acho que foi dia 15 de setembro ou mês passado."

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text=msg,
        vehicles=[corolla_vehicle],
    )

    reply = res["reply"]

    # 1. Cancelamento do Civic
    assert "Cancelei o agendamento da revisão do Civic" in reply

    # 2. Contexto do Corolla e 150.000 km com Alerta Pesado
    assert "Corolla" in reply
    assert "150.000 km" in reply
    assert "ALERTA DE REVISÃO PESADA" in reply
    assert "correia" in reply.lower()
    assert "arrefecimento" in reply.lower()

    # 3. Dúvida sobre data conflitante
    assert "15 de setembro ou mês passado" in reply
    assert "data exata" in reply.lower()


@pytest.mark.asyncio
async def test_scenario_4_validacao_de_borda_hodometro_negativo_e_fracao_temporal(corolla_vehicle):
    """
    Cenário 4:
    - Proteção de Dados: Bloqueia hodômetro negativo (-50 km).
    - Resolução Complexa: Calcula 6 meses e meio (~198 dias) para data futura exata.
    """
    service = ConversationService()
    user_id = "5521999990001"
    msg = "Beleza, confirmo isso. Só que o hodômetro do Corolla na verdade tá marcando -50 km, deu problema no painel. E me lembra de olhar a pressão dos pneus daqui a 6 meses e meio."

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text=msg,
        vehicles=[corolla_vehicle],
    )

    assert res["action_type"] == "invalid_odometer"
    reply = res["reply"]

    # Bloqueio de hodômetro negativo
    assert "quilometragem negativa" in reply.lower()
    assert "-50 km" in reply

    # Resolução de 6 meses e meio
    assert "daqui a 6 meses e meio" in reply
    assert "Olhar a pressão dos pneus" in reply
    assert "/" in reply


@pytest.mark.asyncio
async def test_scenario_5_colapso_absurdo_recuperacao_e_relatorio(corolla_vehicle):
    """
    Cenário 5:
    - Lógica Temporal Absurda: Bloqueia "31 de fevereiro" e agendamento para o "ano passado".
    - Veículo Inexistente: Rejeita troca de motor do Civic.
    - Recuperação Graciosa: Não crasha e entrega o relatório geral da garagem para o Corolla.
    """
    service = ConversationService()
    user_id = "5521999990001"
    msg = "Ignora tudo. Troca o motor do Civic pra um V8, agenda a vistoria dele pro dia 31 de fevereiro do ano passado na época que eu morava no interior, e me dá o relatório da garagem de novo."

    res = await service.handle_incoming_message(
        user_id=user_id,
        message_text=msg,
        vehicles=[corolla_vehicle],
    )

    assert res["action_type"] == "impossible_logic_handled"
    reply = res["reply"]

    # Inconsistências apontadas
    assert "31 de fevereiro" in reply
    assert "ano passado" in reply
    assert "não consta na sua garagem" in reply.lower()

    # Relatório entregue com sucesso
    assert "Toyota Corolla" in reply
    assert "140.000 km" in reply or "150.000 km" in reply
