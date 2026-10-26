"""
AutoCare V2 - AI Schemas (Gemini Structured Outputs)
Contratos estritos tipados em Pydantic v2 utilizados no response_schema do Google Gemini.
Garante que a resposta da IA venha sempre formatada em JSON previsível e 100% validada.
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator

from src.domain.enums import (
    IntentType,
    ConfidenceLevel,
    VehicleType,
    FuelType,
    ReminderRecurrence,
)


class VehicleDraft(BaseModel):
    """Entidades extraídas para rascunho de cadastro de veículo com suporte a autopreenchimento de ficha técnica."""
    make: Optional[str] = Field(default=None, description="Montadora (ex: Fiat, Honda, Toyota, Volkswagen)")
    model: Optional[str] = Field(default=None, description="Modelo (ex: Palio, CG 160 Titan, Corolla, Nivus)")
    version: Optional[str] = Field(default=None, description="Versão de acabamento ou pacote (ex: Highline, XEI, Comfortline, Premier)")
    year: Optional[int] = Field(default=None, description="Ano de fabricação / modelo (ex: 2018, 2022)")
    plate: Optional[str] = Field(default=None, description="Placa do veículo (ex: ABC1D23)")
    odometer_km: Optional[int] = Field(default=None, ge=0, description="Quilometragem inicial informada")
    engine: Optional[str] = Field(default=None, description="Motorização oficial inferida ou informada (ex: 1.0 Turbo, 2.0 Dynamic Force, 160cc)")
    transmission: Optional[str] = Field(default=None, description="Câmbio / transmissão oficial inferida ou informada (ex: Automático, Manual, CVT)")
    steering: Optional[str] = Field(default=None, description="Tipo de direção oficial inferida ou informada (ex: Elétrica, Hidráulica, Mecânica)")
    cambio: Optional[str] = Field(default=None, description="Alias para transmission (ex: Automático, Manual)")
    direcao: Optional[str] = Field(default=None, description="Alias para steering (ex: Elétrica, Hidráulica)")
    motor: Optional[str] = Field(default=None, description="Alias para engine (ex: 1.0 Turbo, 2.0)")
    vehicle_type: Optional[VehicleType] = Field(default=None, description="carro, moto ou caminhonete")
    fuel_type: Optional[FuelType] = Field(default=None, description="flex, gasolina, etanol, diesel, etc.")

    @model_validator(mode="after")
    def sync_technical_specs(self) -> "VehicleDraft":
        """Garante sincronização bidirecional entre aliases em português e campos canônicos."""
        if not self.engine and self.motor:
            self.engine = self.motor
        if not self.motor and self.engine:
            self.motor = self.engine
        if not self.transmission and self.cambio:
            self.transmission = self.cambio
        if not self.cambio and self.transmission:
            self.cambio = self.transmission
        if not self.steering and self.direcao:
            self.steering = self.direcao
        if not self.direcao and self.steering:
            self.direcao = self.steering
        return self


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

    @field_validator("workshop_name", mode="before")
    @classmethod
    def validate_workshop_name(cls, v: Optional[str]) -> Optional[str]:
        """Garante que workshop_name seja apenas nome próprio de estabelecimento ou None."""
        if not v:
            return None
        v_clean = str(v).strip()
        if len(v_clean) < 3:
            return None
        v_lower = v_clean.lower()
        blacklist = {
            "época", "epoca", "momento", "tempo", "lugar", "rua", "oficina",
            "concessionária", "concessionaria", "mecânico", "mecanico", "casa",
            "garagem", "loja", "ontem", "hoje", "mês passado", "mes passado",
            "ano passado", "semana passada", "auto center", "centro automotivo",
            "período", "periodo", "na época", "na epoca", "naquele dia", "naquele momento",
            "ali", "lá", "la", "aqui", "posto", "posto de gasolina", "posto de combustível",
            "concessionária autorizada", "oficina mecânica", "na rua", "na rua mesmo", "rua mesmo"
        }
        if (
            v_lower in blacklist
            or v_lower.startswith("na época")
            or v_lower.startswith("na epoca")
            or v_lower.startswith("na rua")
            or "na rua" in v_lower
            or "rua mesmo" in v_lower
        ):
            return None
        return v_clean

    @field_validator("date_str", mode="before")
    @classmethod
    def normalize_date_str(cls, v: Optional[str]) -> Optional[str]:
        """
        Prioriza datas absolutas sobre referências relativas e normaliza para DD/MM/YYYY.
        Ex: 'mês passado, no dia 15 de setembro' -> '15/09/2026'
        """
        if not v:
            return None
        import re
        from datetime import datetime

        s = str(v).strip()
        current_year = datetime.now().year

        months_map = {
            "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
            "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
            "outubro": 10, "novembro": 11, "dezembro": 12,
            "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
            "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
        }

        # 1. Busca por padrão textual: (dia )?15 de setembro (de 2026)?
        textual_match = re.search(
            r"(?:dia\s+)?(\d{1,2})\s+de\s+([a-zA-ZçÇ]+)(?:\s+de\s+(\d{4}))?",
            s,
            re.IGNORECASE,
        )
        if textual_match:
            day = int(textual_match.group(1))
            month_name = textual_match.group(2).lower()
            year = int(textual_match.group(3)) if textual_match.group(3) else current_year
            month = months_map.get(month_name)
            if month and 1 <= day <= 31:
                return f"{day:02d}/{month:02d}/{year}"

        # 2. Busca por padrão numérico DD/MM/YYYY ou DD/MM
        numeric_match = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", s)
        if numeric_match:
            day = int(numeric_match.group(1))
            month = int(numeric_match.group(2))
            year_grp = numeric_match.group(3)
            if year_grp:
                year = int(year_grp)
                if year < 100:
                    year += 2000
            else:
                year = current_year
            if 1 <= day <= 31 and 1 <= month <= 12:
                return f"{day:02d}/{month:02d}/{year}"

        # Se for apenas termo relativo simples sem data absoluta associada
        return s


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


class ActionDraft(BaseModel):
    """
    Entidade representativa de uma ação estruturada individual (suporte a múltiplas ações/veículos e intenções mistas).
    """
    intent: IntentType = Field(..., description="Intenção individual identificada (ex: RECORD_MAINTENANCE, SET_REMINDER, UPDATE_ODOMETER)")
    target_vehicle_ref: Optional[str] = Field(default=None, description="Veículo alvo (ex: 'Civic', 'Sandero', placa)")
    is_critical_action: bool = Field(default=True, description="Se exige confirmação antes de persistir")
    description: Optional[str] = Field(default=None, description="Descrição da ação, peças ou lembrete")
    odometer_km: Optional[int] = Field(default=None, ge=0, description="Hodômetro informado se aplicável")
    total_cost: Optional[float] = Field(default=None, ge=0.0, description="Valor financeiro total se aplicável")
    components: List[str] = Field(default_factory=list, description="Peças ou serviços revisados")
    workshop_name: Optional[str] = Field(default=None, description="Oficina mecânica")
    date_str: Optional[str] = Field(default=None, description="Data da realização")
    task_description: Optional[str] = Field(default=None, description="Descrição da tarefa para agendamento de lembrete")
    explicit_time: Optional[str] = Field(default=None, description="Horário explícito HH:MM (ex: '09:00', '14:30')")
    target_date_str: Optional[str] = Field(default=None, description="Data alvo (ex: 'amanhã', 'hoje')")
    due_datetime: Optional[str] = Field(default=None, description="Data/hora do lembrete")
    vehicle: Optional[VehicleDraft] = Field(default=None)
    maintenance: Optional[MaintenanceDraft] = Field(default=None)
    reminder: Optional[ReminderDraft] = Field(default=None)
    fine: Optional[FineDraft] = Field(default=None)
    extracted_entities: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("workshop_name", mode="before")
    @classmethod
    def validate_action_workshop_name(cls, v: Optional[str]) -> Optional[str]:
        return MaintenanceDraft.validate_workshop_name(v)

    @model_validator(mode="after")
    def sync_nested_drafts(self) -> "ActionDraft":
        """Sincroniza atributos diretos com rascunhos aninhados (MaintenanceDraft e ReminderDraft)."""
        if self.reminder:
            if not self.task_description:
                self.task_description = self.reminder.task_description
            if not self.explicit_time:
                self.explicit_time = self.reminder.explicit_time
            if not self.target_date_str:
                self.target_date_str = self.reminder.target_date_str
            if not self.description:
                self.description = self.reminder.task_description
        elif self.intent == IntentType.SET_REMINDER and (self.task_description or self.description):
            self.reminder = ReminderDraft(
                task_description=self.task_description or self.description or "Lembrete automotivo",
                explicit_time=self.explicit_time,
                target_date_str=self.target_date_str,
            )

        if self.maintenance:
            if not self.total_cost and self.maintenance.total_cost:
                self.total_cost = self.maintenance.total_cost
            if not self.components and self.maintenance.components:
                self.components = self.maintenance.components
            if not self.description and self.maintenance.service_summary:
                self.description = self.maintenance.service_summary
        elif self.intent == IntentType.RECORD_MAINTENANCE and (self.components or self.total_cost or self.description):
            self.maintenance = MaintenanceDraft(
                service_summary=self.description,
                components=self.components,
                total_cost=self.total_cost,
                odometer_km=self.odometer_km,
                workshop_name=self.workshop_name,
                date_str=self.date_str,
            )
        return self


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
    actions: List[ActionDraft] = Field(
        default_factory=list,
        description="Lista de ações estruturadas quando a mensagem contiver múltiplas intenções ou múltiplos veículos"
    )


# Alias para retrocompatibilidade
GeminiNLPResult = GeminiAnalysisResponse
