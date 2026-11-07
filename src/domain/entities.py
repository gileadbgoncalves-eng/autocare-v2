"""
AutoCare V2 - Domain Entities
Entidades puras de domínio modeladas com Pydantic v2.
Garante imutabilidade, validação de regras de integridade e serialização rápida.
"""

import datetime as dt
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import (
    ConversationState,
    VehicleType,
    FuelType,
    UsageProfile,
    ReminderRecurrence,
    ReminderStatus,
    RecordStatus,
    FineStatus,
)


MAX_CONTEXT_TURNS = 50


def utc_now() -> dt.datetime:
    """B4: carimbo de tempo canônico UTC aware (`datetime.now(timezone.utc)`)."""
    return dt.datetime.now(dt.timezone.utc)


def _ensure_utc_aware(val: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if val is None or not isinstance(val, dt.datetime):
        return val
    if val.tzinfo is None:
        return val.replace(tzinfo=dt.timezone.utc)
    return val.astimezone(dt.timezone.utc)


class UserPreferences(BaseModel):
    """Preferências granulares de notificação do motorista."""
    notify_financial: bool = Field(default=True, description="Notificações de IPVA, seguro e licenciamento")
    notify_maintenance: bool = Field(default=True, description="Alertas preventivos do manual da montadora")
    notify_minor_checks: bool = Field(default=True, description="Lembretes de calibragem de pneus e fluidos")
    quiet_mode: bool = Field(default=False, description="Modo silencioso global (suspende disparos ativos)")
    horario_notificacao_padrao: dt.time = Field(default=dt.time(9, 0), description="Horário preferido para receber o Check-up Diário")
    updated_at: dt.datetime = Field(default_factory=utc_now)


class User(BaseModel):
    """Identidade do usuário motorista (vinculado ao número WhatsApp em formato E.164)."""
    id: str = Field(..., description="Telefone sanitizado no formato internacional, ex: 5521992028482")
    name: Optional[str] = Field(default=None, description="Nome ou apelido preferido do condutor")
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    created_at: dt.datetime = Field(default_factory=utc_now)

    @field_validator("id")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        clean = "".join(filter(str.isdigit, v))
        if len(clean) < 10 or len(clean) > 15:
            raise ValueError(f"Telefone inválido: {v}. Deve conter entre 10 e 15 dígitos numéricos.")
        return clean


class Vehicle(BaseModel):
    """Entidade do Veículo (Carro, Moto ou Caminhonete) cadastrado na garagem com os 9 campos padronizados."""
    id: Optional[int] = Field(default=None, description="Identificador único no banco de dados")
    user_id: str = Field(..., description="ID do proprietário do veículo")
    brand: Optional[str] = Field(default=None, description="Marca / Montadora (ex: Fiat, Honda, Toyota, Volkswagen)")
    make: Optional[str] = Field(default=None, description="Montadora / Fabricante (alias de brand)")
    model: str = Field(..., description="Modelo do veículo (ex: Palio, CG 160 Titan, Corolla)")
    year: Optional[int] = Field(default=None, description="Ano de fabricação / modelo")
    plate: Optional[str] = Field(default=None, description="Placa no formato Mercosul ou antigo")
    odometer: Optional[int] = Field(default=None, ge=0, description="Quilometragem atual no odômetro")
    current_km: Optional[int] = Field(default=None, ge=0, description="Quilometragem atual no odômetro (alias de odometer)")
    version: Optional[str] = Field(default=None, description="Versão de acabamento (ex: Fire, Attractive, XEi, Highline)")
    engine: Optional[str] = Field(default=None, description="Motorização (ex: 1.0, 1.4, 2.0, aspirado, turbo)")
    transmission: Optional[str] = Field(default=None, description="Câmbio (manual, automático, automatizado, CVT)")
    steering: Optional[str] = Field(default=None, description="Direção (mecânica, hidráulica, elétrica, eletro-hidráulica)")
    vehicle_type: VehicleType = Field(default=VehicleType.CARRO)
    fuel_type: FuelType = Field(default=FuelType.FLEX)
    usage_profile: UsageProfile = Field(default=UsageProfile.CONVENCIONAL)
    created_at: dt.datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def sync_brand_and_odometer(self) -> "Vehicle":
        if not self.make and self.brand:
            self.make = self.brand
        elif not self.brand and self.make:
            self.brand = self.make
        elif not self.make and not self.brand:
            self.make = self.brand = "Desconhecido"
        if self.current_km is None and self.odometer is not None:
            self.current_km = self.odometer
        elif self.odometer is None and self.current_km is not None:
            self.odometer = self.current_km
        return self

    @field_validator("plate")
    @classmethod
    def sanitize_plate(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        return v.upper().replace("-", "").strip()

    @property
    def plate_alias(self) -> str:
        """Placa canônica para comparação: MAIÚSCULA, sem hífen/espaço ('' quando ausente). (C2)"""
        return (self.plate or "").upper().replace("-", "").replace(" ", "").strip()

    @property
    def model_alias(self) -> str:
        """Modelo canônico para comparação: minúsculo e sem espaços nas pontas ('' quando ausente). (C2)"""
        return (self.model or "").strip().lower()


class MaintenanceRecord(BaseModel):
    """Registro oficial de intervenção preventiva ou corretiva do Porta-Luvas Digital."""
    id: Optional[int] = Field(default=None)
    vehicle_id: int = Field(..., description="Veículo ao qual a manutenção pertence")
    user_id: str = Field(..., description="Motorista que realizou o registro")
    components: List[str] = Field(..., min_length=1, description="Peças ou itens revisados (ex: Óleo 5W-30, Filtro de óleo)")
    description: str = Field(..., description="Resumo descritivo da manutenção")
    km: Optional[int] = Field(default=None, ge=0, description="Hodômetro no momento da revisão")
    date: dt.date = Field(default_factory=dt.date.today, description="Data da realização do serviço")
    total_cost: float = Field(default=0.0, ge=0.0, description="Valor total desembolsado")
    parts_cost: float = Field(default=0.0, ge=0.0, description="Custo exclusivo de peças")
    labor_cost: float = Field(default=0.0, ge=0.0, description="Custo de mão de obra / oficina")
    workshop_name: Optional[str] = Field(default=None, description="Oficina ou centro automotivo")
    parts_brand: Optional[str] = Field(default=None, description="Marca dos componentes utilizados")
    warranty_months: Optional[int] = Field(default=None, ge=0, description="Tempo de garantia em meses")
    invoice_photo_url: Optional[str] = Field(default=None, description="Foto da nota fiscal ou recibo")
    status: str = Field(default=RecordStatus.ACTIVE.value, description="active | cancelled (soft delete)")
    created_at: dt.datetime = Field(default_factory=utc_now)

    @property
    def maintenance_date(self) -> dt.date:
        return self.date


class Reminder(BaseModel):
    """Lembrete programado para disparo proativo no WhatsApp."""
    id: Optional[int] = Field(default=None)
    user_id: str = Field(..., description="Destinatário do alerta")
    vehicle_id: Optional[int] = Field(default=None, description="Veículo opcionalmente associado")
    description: str = Field(..., description="Conteúdo da tarefa / verificação")
    due_datetime: Optional[dt.datetime] = Field(default=None, description="Data e hora exatas do disparo")
    data_hora_agendada: Optional[dt.datetime] = Field(default=None, description="Data e hora do agendamento explícito")
    due_km: Optional[int] = Field(default=None, ge=0, description="Disparo por hodômetro atingido")
    recurrence: ReminderRecurrence = Field(default=ReminderRecurrence.ONCE)
    status: ReminderStatus = Field(default=ReminderStatus.PENDING)
    created_at: dt.datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "due_datetime", "data_hora_agendada", mode="after")
    @classmethod
    def ensure_utc_timestamps(cls, v: Optional[dt.datetime]) -> Optional[dt.datetime]:
        return _ensure_utc_aware(v)


class TrafficFine(BaseModel):
    """Infração de trânsito vinculada a um veículo para controle financeiro e pontos na CNH."""
    id: Optional[int] = Field(default=None)
    vehicle_id: int = Field(..., description="Veículo autuado")
    description: str = Field(..., description="Motivo / Enquadramento da infração")
    infraction_code: Optional[str] = Field(default=None, description="Código do CTB, ex: 745-5-0")
    issuing_body: Optional[str] = Field(default=None, description="Órgão autuador (DETRAN, PRF, Prefeitura)")
    points: int = Field(default=0, ge=0, description="Pontos na CNH (3, 4, 5 ou 7)")
    amount: float = Field(default=0.0, ge=0.0, description="Valor nominal da multa")
    discounted_amount: float = Field(default=0.0, ge=0.0, description="Valor com desconto SNE (ex: 20%)")
    infraction_date: Optional[dt.date] = Field(default=None, description="Data da autuação")
    due_date: Optional[dt.date] = Field(default=None, description="Data de vencimento para pagamento com desconto")
    status: FineStatus = Field(default=FineStatus.NOTIFICADA)
    created_at: dt.datetime = Field(default_factory=utc_now)


class ConversationTurn(BaseModel):
    """Representação de um turno de diálogo atômico."""
    role: str = Field(..., description="'user' para mensagem do condutor, 'assistant' para resposta do bot")
    content: str = Field(..., description="Texto integral da mensagem enviada ou recebida")
    timestamp: dt.datetime = Field(default_factory=utc_now)


class ConversationContext(BaseModel):
    """
    Contexto de conversação ativo e estado do usuário.
    Garante idempotência, controle transacional e memória recente em cada requisição.
    """
    user_id: str = Field(..., description="Identificador do usuário (telefone)")
    state: ConversationState = Field(default=ConversationState.NORMAL, description="Estado atual da máquina de estados")
    active_vehicle_id: Optional[int] = Field(default=None, description="Veículo ativo em foco na sessão")
    draft_payload: Optional[Dict[str, Any]] = Field(default=None, description="Dados temporários aguardando confirmação do usuário")
    recent_turns: List[ConversationTurn] = Field(default_factory=list, description="Últimos turnos de diálogo para injeção de contexto no Gemini")
    max_turns: int = Field(default=MAX_CONTEXT_TURNS, description="B6 (M8): limite máximo de turnos mantidos em memória")
    updated_at: dt.datetime = Field(default_factory=utc_now)
    draft_updated_at: Optional[dt.datetime] = Field(default=None, description="Última atividade do rascunho pendente (base do TTL, UTC)")
    turn_seq: int = Field(default=0, description="Contador sequencial monótono de turnos para controle de concorrência")
    pending_edit_field: Optional[str] = Field(default=None, description="Campo do último serviço aguardando novo valor na condução ativa")
    pending_edit_service_id: Optional[Any] = Field(default=None, description="ID do último serviço salvo em processo de edição")
    pending_edit_awaiting_choice: bool = Field(default=False, description="True quando o bot perguntou QUAL campo editar e aguarda a escolha (pending_edit_field fica None)")
    pending_ambiguous_message: Optional[str] = Field(default=None, description="B2 (C6): mensagem original aguardando desambiguação de placa pelo usuário")
    pending_ambiguous_vehicle_resolved_id: Optional[int] = Field(default=None, description="B2/R2: ID do veículo resolvido durante desambiguação")

    def advance_turn(self, content: str) -> int:
        self.turn_seq += 1
        self.recent_turns.append(ConversationTurn(role="user", content=content))
        if len(self.recent_turns) > self.max_turns:
            self.recent_turns = self.recent_turns[-self.max_turns:]
        return self.turn_seq

    def is_awaiting_confirmation(self) -> bool:
        """Verifica se o usuário está em um estado que aguarda resposta afirmativa/negativa explícita."""
        return self.state != ConversationState.NORMAL and self.draft_payload is not None
