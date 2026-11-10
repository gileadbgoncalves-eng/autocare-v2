"""
AutoCare V2 - AI Schemas (Gemini Structured Outputs)
Contratos estritos tipados em Pydantic v2 utilizados no response_schema do Google Gemini.
Garante que a resposta da IA venha sempre formatada em JSON previsível e 100% validada.
"""

from typing import Optional, List, Dict, Any, Literal, Union
from pydantic import BaseModel, Field, field_validator, model_validator

from src.domain.enums import (
    IntentType,
    ConfidenceLevel,
    VehicleType,
    FuelType,
    ReminderRecurrence,
)


class VehicleDraft(BaseModel):
    """Entidades extraídas para rascunho de cadastro de veículo."""
    make: Optional[str] = Field(default=None, description="Montadora (ex: Fiat, Honda, Toyota, Volkswagen)")
    brand: Optional[str] = Field(default=None, description="Alias para make / montadora (ex: Fiat, Honda, Toyota, Volkswagen)")
    model: Optional[str] = Field(default=None, description="Modelo (ex: Palio, CG 160 Titan, Corolla, Nivus)")
    version: Optional[str] = Field(default=None, description="Versão de acabamento ou pacote (ex: Highline, XEI, Comfortline, Premier)")
    year: Optional[int] = Field(default=None, description="Ano de fabricação / modelo (ex: 2018, 2022)")
    plate: Optional[str] = Field(default=None, description="Placa do veículo (ex: ABC1D23)")
    odometer_km: Optional[int] = Field(default=None, ge=0, description="Quilometragem inicial informada")
    odometer: Optional[int] = Field(default=None, ge=0, description="Alias para odometer_km (quilometragem inicial informada)")
    engine: Optional[str] = Field(default=None, description="Motorização informada pelo usuário (ex: 1.0, 1.0 Turbo, 2.0, 160cc)")
    transmission: Optional[str] = Field(default=None, description="Câmbio / transmissão informada pelo usuário (ex: Automático, Manual, CVT)")
    steering: Optional[str] = Field(default=None, description="Tipo de direção informada pelo usuário (ex: Elétrica, Hidráulica, Mecânica)")
    cambio: Optional[str] = Field(default=None, description="Alias para transmission (ex: Automático, Manual)")
    direcao: Optional[str] = Field(default=None, description="Alias para steering (ex: Elétrica, Hidráulica)")
    motor: Optional[str] = Field(default=None, description="Alias para engine (ex: 1.0 Turbo, 2.0)")
    vehicle_type: Optional[VehicleType] = Field(default=None, description="carro, moto ou caminhonete")
    fuel_type: Optional[FuelType] = Field(default=None, description="flex, gasolina, etanol, diesel, etc.")

    @model_validator(mode="after")
    def sync_technical_specs(self) -> "VehicleDraft":
        """Garante sincronização bidirecional entre aliases e campos canônicos."""
        if not self.make and self.brand:
            self.make = self.brand
        if not self.brand and self.make:
            self.brand = self.make
        if self.odometer_km is None and self.odometer is not None:
            self.odometer_km = self.odometer
        if self.odometer is None and self.odometer_km is not None:
            self.odometer = self.odometer_km
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
    data: Optional[str] = Field(default=None, description="Alias para date_str (DD/MM/AAAA ou ISO)")
    date: Optional[str] = Field(default=None, description="Alias para date_str")
    service_date: Optional[str] = Field(default=None, description="Alias para date_str")
    data_manutencao: Optional[str] = Field(default=None, description="Alias para date_str")
    total_cost: Optional[float] = Field(default=None, ge=0.0, description="Valor financeiro total gasto")
    parts_cost: Optional[float] = Field(default=None, ge=0.0, description="Custo específico de peças")
    labor_cost: Optional[float] = Field(default=None, ge=0.0, description="Custo de mão de obra")
    workshop_name: Optional[str] = Field(default=None, description="Nome da oficina mecânica")
    parts_brand: Optional[str] = Field(default=None, description="Marca das peças instaladas")
    warranty_months: Optional[int] = Field(default=None, ge=0, description="Meses de garantia")

    @model_validator(mode="before")
    @classmethod
    def _normalize_maintenance_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            date_candidate = (
                data.get("date_str")
                or data.get("data")
                or data.get("date")
                or data.get("service_date")
                or data.get("data_manutencao")
            )
            if date_candidate:
                str_val = str(date_candidate)
                data["date_str"] = data["data"] = data["date"] = data["service_date"] = data["data_manutencao"] = str_val
            km_candidate = (
                data.get("odometer_km")
                if data.get("odometer_km") is not None
                else (data.get("km") if data.get("km") is not None else data.get("odometer"))
            )
            if km_candidate is not None:
                data["odometer_km"] = km_candidate
            if not data.get("components") and data.get("itens"):
                data["components"] = data.get("itens")
            val_candidate = (
                data.get("total_cost")
                if data.get("total_cost") is not None
                else (data.get("valor") if data.get("valor") is not None else data.get("cost"))
            )
            if val_candidate is not None:
                data["total_cost"] = val_candidate
            if not data.get("service_summary") and data.get("description"):
                data["service_summary"] = data.get("description")
            if not data.get("workshop_name") and data.get("oficina"):
                data["workshop_name"] = data.get("oficina")
        return data

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

        # 0. Busca por padrão ISO: YYYY-MM-DD ou YYYY/MM/DD
        iso_match = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", s)
        if iso_match:
            year = int(iso_match.group(1))
            month = int(iso_match.group(2))
            day = int(iso_match.group(3))
            if 1 <= day <= 31 and 1 <= month <= 12:
                return f"{day:02d}/{month:02d}/{year}"

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
    data: Optional[str] = Field(default=None, description="Alias para date_str")
    date: Optional[str] = Field(default=None, description="Alias para date_str")
    service_date: Optional[str] = Field(default=None, description="Alias para date_str")
    data_manutencao: Optional[str] = Field(default=None, description="Alias para date_str")
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


class ToolCallAction(BaseModel):
    """
    Estrutura de Function Calling / Ação Estruturada decidida pela IA (Arquitetura AI-First).
    Suporta as operações centrais de cadastro de veículo, manutenção e histórico:
    - upsert_draft(make/brand, model, version, year, engine, transmission, steering, plate, odometer/km, itens, data, oficina, valor)
    - confirm_and_save_draft()
    - update_last_saved_service(service_id, itens, km, data, oficina, valor)
    - get_service_history()
    - patch_vehicle_profile(target_vehicle_ref, km/odometer, plate, engine, transmission, steering)
    """
    name: Literal[
        "upsert_draft",
        "confirm_and_save_draft",
        "update_last_saved_service",
        "get_service_history",
        "patch_vehicle_profile",
    ] = Field(..., description="Nome exato da função a ser executada pelo backend")
    target_vehicle_ref: Optional[str] = Field(
        default=None,
        description="Referência ao veículo alvo se aplicável (ex: 'Palio', 'Civic', placa)"
    )
    service_id: Optional[Union[int, str]] = Field(
        default=None,
        description="ID do serviço salvo no banco (usado em update_last_saved_service a partir de ultimo_servico_salvo)"
    )
    # Campos para rascunho / patch de veículo
    make: Optional[str] = Field(default=None, description="Montadora do veículo (ex: Fiat, Volkswagen, Honda)")
    brand: Optional[str] = Field(default=None, description="Alias para montadora do veículo (ex: Fiat, Volkswagen)")
    model: Optional[str] = Field(default=None, description="Modelo do veículo (ex: Palio, Gol, Nivus, Corolla)")
    version: Optional[str] = Field(default=None, description="Versão do veículo (ex: Highline, XEI, ELX)")
    year: Optional[int] = Field(default=None, description="Ano do veículo (ex: 2008, 2022)")
    plate: Optional[str] = Field(default=None, description="Placa do veículo (ex: ABC1D23)")
    odometer: Optional[int] = Field(default=None, ge=0, description="Quilometragem atual do veículo")
    engine: Optional[str] = Field(default=None, description="Motorização informada pelo usuário (ex: 1.0, 1.0 Turbo, 2.0)")
    transmission: Optional[str] = Field(default=None, description="Câmbio informado pelo usuário (ex: Manual, Automático, CVT)")
    steering: Optional[str] = Field(default=None, description="Direção informada pelo usuário (ex: Hidráulica, Elétrica, Mecânica)")
    vehicle_type: Optional[VehicleType] = Field(default=None, description="Tipo do veículo: carro, moto ou caminhonete")
    fuel_type: Optional[FuelType] = Field(default=None, description="Combustível do veículo")
    # Campos para rascunho de manutenção
    itens: Optional[List[str]] = Field(
        default=None,
        description="Lista completa de itens/peças/serviços (mesclada cumulativamente em upsert_draft ou atualizada em update_last_saved_service)"
    )
    km: Optional[int] = Field(
        default=None,
        ge=0,
        description="Quilometragem do veículo no momento do serviço ou para atualização de hodômetro"
    )
    data: Optional[str] = Field(
        default=None,
        description="Data do serviço no formato DD/MM/AAAA (apenas se informada explicitamente ou mantida do estado)"
    )
    date: Optional[str] = Field(default=None, description="Alias para data")
    date_str: Optional[str] = Field(default=None, description="Alias para data")
    service_date: Optional[str] = Field(default=None, description="Alias para data")
    data_manutencao: Optional[str] = Field(default=None, description="Alias para data")
    oficina: Optional[str] = Field(
        default=None,
        description="Nome da oficina ou mecânico onde o serviço foi realizado"
    )
    valor: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Valor financeiro total pago pelo serviço em reais (R$)"
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_tool_call_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Normalização de aliases para data
            date_candidate = (
                data.get("data")
                or data.get("date")
                or data.get("date_str")
                or data.get("service_date")
                or data.get("data_manutencao")
            )
            if date_candidate:
                str_date = str(date_candidate)
                data["data"] = data["date"] = data["date_str"] = data["service_date"] = data["data_manutencao"] = str_date
            # Normalização de aliases para km
            km_candidate = (
                data.get("km")
                if data.get("km") is not None
                else (data.get("odometer") if data.get("odometer") is not None else data.get("odometer_km"))
            )
            if km_candidate is not None:
                data["km"] = km_candidate
            # Normalização de aliases para itens
            if not data.get("itens") and data.get("components"):
                data["itens"] = data.get("components")
            # Normalização de aliases para valor
            val_candidate = (
                data.get("valor")
                if data.get("valor") is not None
                else (data.get("total_cost") if data.get("total_cost") is not None else data.get("cost"))
            )
            if val_candidate is not None:
                data["valor"] = val_candidate
            # Normalização de aliases para oficina
            if not data.get("oficina") and data.get("workshop_name"):
                data["oficina"] = data.get("workshop_name")
        return data

    @field_validator("oficina", mode="before")
    @classmethod
    def validate_tool_oficina(cls, v: Optional[str]) -> Optional[str]:
        return MaintenanceDraft.validate_workshop_name(v)

    @field_validator("data", mode="before")
    @classmethod
    def normalize_tool_data(cls, v: Optional[str]) -> Optional[str]:
        return MaintenanceDraft.normalize_date_str(v)


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

    # Function Calling / Ação Estruturada (Arquitetura AI-First)
    tool_call: Optional[ToolCallAction] = Field(
        default=None,
        description="Ação estruturada / Function Calling primária decidida pela IA: upsert_draft, confirm_and_save_draft, update_last_saved_service, get_service_history ou patch_vehicle_profile"
    )
    tool_calls: List[ToolCallAction] = Field(
        default_factory=list,
        description="Lista de ações estruturadas / Function Calling decididas pela IA (para execução múltipla em lote / intenções compostas)"
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

    @model_validator(mode="after")
    def sync_tool_call_and_intent(self) -> "GeminiAnalysisResponse":
        """Sincroniza tool_call e tool_calls (Function Calling AI-First) com intent e modelos auxiliares."""
        if self.tool_calls and not self.tool_call:
            self.tool_call = self.tool_calls[0]
        elif self.tool_call and not self.tool_calls:
            self.tool_calls = [self.tool_call]

        if self.tool_call:
            tc = self.tool_call
            if tc.name == "upsert_draft":
                has_vehicle_fields = any(
                    v is not None
                    for v in (
                        tc.make,
                        tc.brand,
                        tc.model,
                        tc.version,
                        tc.year,
                        tc.plate,
                        tc.odometer,
                        tc.engine,
                        tc.transmission,
                        tc.steering,
                        tc.vehicle_type,
                        tc.fuel_type,
                    )
                )
                has_maint_fields = any(
                    v is not None
                    for v in (tc.itens, tc.data, tc.oficina, tc.valor)
                )

                if self.intent == IntentType.REGISTER_VEHICLE or self.vehicle is not None or (has_vehicle_fields and not has_maint_fields):
                    self.intent = IntentType.REGISTER_VEHICLE
                    self.is_critical_action = True
                    odo = tc.odometer if tc.odometer is not None else tc.km
                    if not self.vehicle and (has_vehicle_fields or odo is not None):
                        self.vehicle = VehicleDraft(
                            make=tc.make or tc.brand,
                            brand=tc.brand or tc.make,
                            model=tc.model,
                            version=tc.version,
                            year=tc.year,
                            plate=tc.plate,
                            odometer_km=odo,
                            odometer=odo,
                            engine=tc.engine,
                            transmission=tc.transmission,
                            steering=tc.steering,
                            vehicle_type=tc.vehicle_type,
                            fuel_type=tc.fuel_type,
                        )
                    elif self.vehicle and (has_vehicle_fields or odo is not None):
                        if tc.make or tc.brand:
                            self.vehicle.make = self.vehicle.make or tc.make or tc.brand
                            self.vehicle.brand = self.vehicle.brand or tc.brand or tc.make
                        if tc.model:
                            self.vehicle.model = self.vehicle.model or tc.model
                        if tc.version:
                            self.vehicle.version = self.vehicle.version or tc.version
                        if tc.year is not None:
                            self.vehicle.year = self.vehicle.year or tc.year
                        if tc.plate:
                            self.vehicle.plate = self.vehicle.plate or tc.plate
                        if odo is not None and self.vehicle.odometer_km is None:
                            self.vehicle.odometer_km = odo
                            self.vehicle.odometer = odo
                        if tc.engine:
                            self.vehicle.engine = self.vehicle.engine or tc.engine
                        if tc.transmission:
                            self.vehicle.transmission = self.vehicle.transmission or tc.transmission
                        if tc.steering:
                            self.vehicle.steering = self.vehicle.steering or tc.steering
                        if tc.vehicle_type:
                            self.vehicle.vehicle_type = self.vehicle.vehicle_type or tc.vehicle_type
                        if tc.fuel_type:
                            self.vehicle.fuel_type = self.vehicle.fuel_type or tc.fuel_type
                else:
                    if self.intent in (IntentType.UNKNOWN, IntentType.UPSERT_DRAFT):
                        self.intent = IntentType.RECORD_MAINTENANCE
                    self.is_critical_action = True
                    if not self.maintenance and (tc.itens or tc.km is not None or tc.data or tc.oficina or tc.valor is not None):
                        self.maintenance = MaintenanceDraft(
                            components=list(tc.itens or []),
                            service_summary=", ".join(tc.itens) if tc.itens else None,
                            odometer_km=tc.km,
                            date_str=tc.data,
                            workshop_name=tc.oficina,
                            total_cost=tc.valor,
                        )
            elif tc.name == "confirm_and_save_draft":
                self.intent = IntentType.CONFIRM_ACTION
                self.is_critical_action = False
            elif tc.name == "update_last_saved_service":
                self.intent = IntentType.UPDATE_LAST_SAVED_SERVICE
                self.is_critical_action = False
            elif tc.name == "get_service_history":
                self.intent = IntentType.QUERY_HISTORY
                self.is_critical_action = False
            elif tc.name == "patch_vehicle_profile":
                if self.intent in (IntentType.UNKNOWN, IntentType.RECORD_MAINTENANCE):
                    self.intent = IntentType.UPDATE_ODOMETER
                self.is_critical_action = False
        return self


# Alias para retrocompatibilidade
GeminiNLPResult = GeminiAnalysisResponse
