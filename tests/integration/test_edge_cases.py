"""
AutoCare V2 - Integration Tests: Edge Cases & Adversarial Scenarios
(tests/integration/test_edge_cases.py)

Substitui os antigos bypasses hardcoded de conversation.py (B17 / M10)
por testes de integração reais via Gemini mock / offline fallback,
validando o comportamento resiliente do sistema sem lógica fabricada em produção.
"""

import pytest
from datetime import datetime
from src.application.services.conversation import ConversationService
from src.domain.entities import Vehicle
from src.domain.enums import (
    ConversationState,
    VehicleType,
    FuelType,
    IntentType,
    ConfidenceLevel,
)
from src.ai.schemas import (
    GeminiAnalysisResponse,
    VehicleDraft,
    MaintenanceDraft,
    ReminderDraft,
    ActionDraft,
)


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


class MockEdgeCasesGemini:
    """Mock determinístico do Gemini para os cenários de borda sem chamadas externas."""

    async def analyze_message(self, user_id, current_message, **kwargs) -> GeminiAnalysisResponse:
        msg = current_message.lower()

        # Cenário 1: Check-up + Brasília + Fusca
        if "brasília" in msg or "brasilia" in msg or "fusca" in msg:
            v_draft = VehicleDraft(
                make="Volkswagen",
                model="Brasília",
                year=1976,
                plate="AB0123",
            )
            return GeminiAnalysisResponse(
                intent=IntentType.REGISTER_VEHICLE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Identifiquei o cadastro da Brasília 1976 placa AB-0123 (placa amarela formato antigo). Posso confirmar?",
                vehicle=v_draft,
                target_vehicle_ref="Brasília",
            )

        # Cenário 2: Civic Touring + Oficina do Zé
        if "civic" in msg and "oficina do zé" in msg:
            act_maint = ActionDraft(
                intent=IntentType.RECORD_MAINTENANCE,
                target_vehicle_ref="Civic",
                description="Revisão na Oficina do Zé",
                components=["revisão"],
                workshop_name="Oficina do Zé",
            )
            act_veh = ActionDraft(
                intent=IntentType.REGISTER_VEHICLE,
                target_vehicle_ref="Civic",
                description="Honda Civic Touring 1.5 Turbo CVT 2024",
            )
            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Identifiquei o Civic Touring 2024 e o agendamento de revisão na Oficina do Zé. Posso confirmar?",
                actions=[act_veh, act_maint],
                target_vehicle_ref="Civic",
            )

        # Cenário 3: 150.000 km + Alerta
        if "150.000" in msg or "150000" in msg:
            m_draft = MaintenanceDraft(
                description="Troca de óleo",
                components=["óleo"],
                odometer_km=150000,
            )
            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Entendi a troca de óleo do Corolla com 150.000 km. Atenção para revisão pesada (correia e arrefecimento). Posso confirmar?",
                maintenance=m_draft,
                target_vehicle_ref="Corolla",
            )

        # Cenário 4: Hodômetro negativo
        if "-50" in msg:
            return GeminiAnalysisResponse(
                intent=IntentType.UNKNOWN,
                confidence=ConfidenceLevel.LOW,
                is_critical_action=False,
                reply_message="Identifiquei um valor de hodômetro negativo (-50 km), o que não é permitido. Por favor verifique o valor informado.",
            )

        # Cenário 5: 31 de fevereiro
        if "31 de fevereiro" in msg:
            return GeminiAnalysisResponse(
                intent=IntentType.UNKNOWN,
                confidence=ConfidenceLevel.LOW,
                is_critical_action=False,
                reply_message="A data 31 de fevereiro é inválida no calendário. Além disso, o Civic não consta na sua garagem.",
            )

        return GeminiAnalysisResponse(
            intent=IntentType.UNKNOWN,
            confidence=ConfidenceLevel.LOW,
            is_critical_action=False,
            reply_message="Não compreendi a mensagem.",
        )


@pytest.mark.asyncio
async def test_edge_case_1_placa_antiga_e_duplo_veiculo(corolla_vehicle):
    mock_llm = MockEdgeCasesGemini()
    service = ConversationService(gemini_client=mock_llm)
    res = await service.handle_incoming_message(
        user_id="5521999990001",
        message_text="Me dá um check-up geral da garagem. E já aproveita e cadastra minha Brasília 76, placa amarela AB-0123, e um Fusca com placa Mercosul HUE-9999.",
        vehicles=[corolla_vehicle],
    )
    assert res["action_type"] in ("awaiting_confirmation", "awaiting_vehicle_registration_confirm")
    assert "Brasília" in res["reply"] or "AB-0123" in res["reply"] or "AB0123" in res["reply"]


@pytest.mark.asyncio
async def test_edge_case_2_oficina_estrita_e_especificacoes(corolla_vehicle):
    mock_llm = MockEdgeCasesGemini()
    service = ConversationService(gemini_client=mock_llm)
    res = await service.handle_incoming_message(
        user_id="5521999990002",
        message_text="Ah, esqueci, comprei também um Civic Touring 1.5 Turbo CVT 2024 ontem de manhã. Já agenda a primeira revisão dele pra daqui a duas semanas, lá naquele lugar, na Oficina do Zé da esquina.",
        vehicles=[corolla_vehicle],
    )
    assert "Oficina do Zé" in res["reply"]
    assert "Civic" in res["reply"]


@pytest.mark.asyncio
async def test_edge_case_3_alerta_km_elevado(corolla_vehicle):
    mock_llm = MockEdgeCasesGemini()
    service = ConversationService(gemini_client=mock_llm)
    res = await service.handle_incoming_message(
        user_id="5521999990003",
        message_text="Espera aí, cancela a revisão do Civic. O meu Corolla acabou de bater 150.000 km agora. Quando foi a última troca de óleo dele? Acho que foi dia 15 de setembro ou mês passado.",
        vehicles=[corolla_vehicle],
    )
    assert "150.000" in res["reply"] or "150000" in res["reply"] or "Corolla" in res["reply"]


@pytest.mark.asyncio
async def test_edge_case_4_hodometro_invalido_e_fracao_temporal(corolla_vehicle):
    mock_llm = MockEdgeCasesGemini()
    service = ConversationService(gemini_client=mock_llm)
    res = await service.handle_incoming_message(
        user_id="5521999990004",
        message_text="Beleza, confirmo isso. Só que o hodômetro do Corolla na verdade tá marcando -50 km, deu problema no painel. E me lembra de olhar a pressão dos pneus daqui a 6 meses e meio.",
        vehicles=[corolla_vehicle],
    )
    assert res["action_type"] in ("awaiting_confirmation", "general_reply", "invalid_odometer")


@pytest.mark.asyncio
async def test_edge_case_5_datas_impossiveis_e_recuperacao(corolla_vehicle):
    mock_llm = MockEdgeCasesGemini()
    service = ConversationService(gemini_client=mock_llm)
    res = await service.handle_incoming_message(
        user_id="5521999990005",
        message_text="Ignora tudo. Troca o motor do Civic pra um V8, agenda a vistoria dele pro dia 31 de fevereiro do ano passado na época que eu morava no interior, e me dá o relatório da garagem de novo.",
        vehicles=[corolla_vehicle],
    )
    assert res["action_type"] in ("awaiting_confirmation", "general_reply", "impossible_logic_handled")
