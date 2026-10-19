"""
AutoCare V2 - AI Schemas (Gemini Structured Outputs)
Contratos estritos tipados em Pydantic v2 utilizados no response_schema do Google Gemini.
Garante que a resposta da IA venha sempre formatada em JSON previsível e 100% validada.
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from src.domain.enums import (
    IntentType,
    ConfidenceLevel,
    VehicleType,
    FuelType,
    ReminderRecurrence,
)


class VehicleDraft(BaseModel):
    """Entidades extraídas para rascunho de cadastro de veículo."""
    make: Optional[str] = Field(default=None, description="Montadora (ex: Fiat, Honda, Toyota)")
    model: Optional[str] = Field(default=None, description="Modelo (ex: Palio, CG 160 Titan, Corolla)")
    year: Optional[int] = Field(default=None, description="Ano de fabricação / modelo (ex: 2018)")
    plate: Optional[str] = Field(default=None, description="Placa do veículo (ex: ABC1D23)")
    odometer_km: Optional[int] = Field(default=None, ge=0, description="Quilometragem inicial informada")
    engine: Optional[str] = Field(default=None, description="Motorização (ex: 1.0, 160cc, 2.0)")
    vehicle_type: Optional[VehicleType] = Field(default=None, description="carro, moto ou caminhonete")
    fuel_type: Optional[FuelType] = Field(default=None, description="flex, gasolina, etanol, diesel, etc.")


class MaintenanceDraft(BaseModel):
    """Entidades extraídas para rascunho de serviço ou manutenção."""
    components: List[str] = Field(
        default_factory=list,
        description="Peças ou itens revisados (ex: ['óleo do motor 5w30', 'filtro de óleo'])"
    )
    service_summary: Optional[str] = Field(default=None, description="Resumo do serviço executado")
    odometer_km: Optional[int] = Field(default=None, ge=0, description="Hodômetro no momento da revisão")
    date_str: Optional[str] = Field(default=None, description="Data informada (ex: 'hoje', 'ontem', '15/10/2026')")
    total_cost: Optional[float] = Field(default=None, ge=0.0, description="Valor financeiro total gasto")
    parts_cost: Optional[float] = Field(default=None, ge=0.0, description="Custo específico de peças")
    labor_cost: Optional[float] = Field(default=None, ge=0.0, description="Custo de mão de obra")
    workshop_name: Optional[str] = Field(default=None, description="Nome da oficina mecânica")
    parts_brand: Optional[str] = Field(default=None, description="Marca das peças instaladas")
    warranty_months: Optional[int] = Field(default=None, ge=0, description="Meses de garantia")


class ReminderDraft(BaseModel):
    """Entidades extraídas para rascunho de agendamento de lembrete."""
    task_description: str = Field(..., description="Descrição da verificação a ser lembrada")
    relative_minutes: Optional[int] = Field(default=None, ge=1, description="Minutos no futuro (ex: 15)")
    relative_hours: Optional[int] = Field(default=None, ge=1, description="Horas no futuro (ex: 2)")
    explicit_time: Optional[str] = Field(default=None, description="Horário explícito HH:MM (ex: '14:30')")
    target_date_str: Optional[str] = Field(default=None, description="Data alvo (ex: 'hoje', 'amanhã', '25/11/2026')")
    target_km: Optional[int] = Field(default=None, ge=0, description="Hodômetro alvo para o alerta (ex: 50000)")
    recurrence: ReminderRecurrence = Field(default=ReminderRecurrence.ONCE, description="once, daily, weekly, monthly")
    is_cancel: bool = Field(default=False, description="True se o usuário solicitou cancelamento")


class FineDraft(BaseModel):
    """Entidades extraídas para rascunho de autuação de trânsito."""
    description: str = Field(..., description="Motivo / Enquadramento da infração")
    infraction_code: Optional[str] = Field(default=None, description="Código de infração CTB")
    amount: Optional[float] = Field(default=None, ge=0.0, description="Valor em reais")
    points: Optional[int] = Field(default=None, ge=0, description="Pontos na CNH")
    due_date_str: Optional[str] = Field(default=None, description="Data de vencimento")


class GeminiAnalysisResponse(BaseModel):
    """
    Contrato Raiz de Structured Output do Google Gemini.
    Toda chamada de NLP retorna rigorosamente uma instância tipada desta classe.
    """
    intent: IntentType = Field(..., description="Intenção semântica identificada")
    confidence: ConfidenceLevel = Field(..., description="Nível de certeza atribuído pela IA")
    is_critical_action: bool = Field(
        ...,
        description="True se a ação é crítica e exige confirmação prévia do usuário antes de persistir"
    )
    target_vehicle_ref: Optional[str] = Field(
        default=None,
        description="Referência ao veículo alvo mencionado na mensagem (ex: 'Palio', 'moto', 'carro')"
    )
    extracted_entities: Dict[str, Any] = Field(
        default_factory=dict,
        description="Dicionário com entidades estruturadas extraídas para rascunho de ações"
    )
    reply_message: str = Field(
        ...,
        description="Mensagem natural em português brasileiro gerada pela IA para o usuário"
    )
    reasoning: Optional[str] = Field(
        default=None,
        description="Justificativa técnica resumida para fins de observabilidade e auditoria"
    )

    # Modelos auxiliares opcionais tipados para facilitar serialização e acesso
    vehicle: Optional[VehicleDraft] = Field(default=None, description="Detalhes se intent for REGISTER_VEHICLE")
    maintenance: Optional[MaintenanceDraft] = Field(default=None, description="Detalhes se intent for RECORD_MAINTENANCE")
    reminder: Optional[ReminderDraft] = Field(default=None, description="Detalhes se intent for SET_REMINDER")
    fine: Optional[FineDraft] = Field(default=None, description="Detalhes se intent for REGISTER_FINE")


# Alias para retrocompatibilidade
GeminiNLPResult = GeminiAnalysisResponse
