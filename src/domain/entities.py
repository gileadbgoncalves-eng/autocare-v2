"""
AutoCare V2 - Domain Entities
Entidades puras de domínio modeladas com Pydantic v2.
Garante imutabilidade, validação de regras de integridade e serialização rápida.
"""

import datetime as dt
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator

from .enums import (
    ConversationState,
    VehicleType,
    FuelType,
    UsageProfile,
    ReminderRecurrence,
    ReminderStatus,
    FineStatus,
)


class UserPreferences(BaseModel):
    """Preferências granulares de notificação do motorista."""
    notify_financial: bool = Field(default=True, description="Notificações de IPVA, seguro e licenciamento")
    notify_maintenance: bool = Field(default=True, description="Alertas preventivos do manual da montadora")
    notify_minor_checks: bool = Field(default=True, description="Lembretes de calibragem de pneus e fluidos")
    quiet_mode: bool = Field(default=False, description="Modo silencioso global (suspende disparos ativos)")
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class User(BaseModel):
    """Identidade do usuário motorista (vinculado ao número WhatsApp em formato E.164)."""
    id: str = Field(..., description="Telefone sanitizado no formato internacional, ex: 5521992028482")
    name: Optional[str] = Field(default=None, description="Nome ou apelido preferido do condutor")
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

    @field_validator("id")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        clean = "".join(filter(str.isdigit, v))
        if len(clean) < 10 or len(clean) > 15:
            raise ValueError(f"Telefone inválido: {v}. Deve conter entre 10 e 15 dígitos numéricos.")
        return clean


class Vehicle(BaseModel):
    """Entidade do Veículo (Carro, Moto ou Caminhonete) cadastrado na garagem."""
    id: Optional[int] = Field(default=None, description="Identificador único no banco de dados")
    user_id: str = Field(..., description="ID do proprietário do veículo")
    make: str = Field(..., description="Montadora / Fabricante (ex: Fiat, Honda, Toyota)")
    model: str = Field(..., description="Modelo do veículo (ex: Palio, CG 160 Titan, Corolla)")
    year: Optional[int] = Field(default=None, description="Ano de fabricação / modelo")
    plate: Optional[str] = Field(default=None, description="Placa no formato Mercosul ou antigo")
    engine: Optional[str] = Field(default=None, description="Motorização (ex: 1.0 Fire, 160cc, 2.0 Dynamic)")
    version: Optional[str] = Field(default=None, description="Versão de acabamento (ex: Attractive, EX, XEi)")
    transmission: Optional[str] = Field(default=None, description="Tipo de transmissão (Manual, Automático, CVT)")
    steering: Optional[str] = Field(default=None, description="Tipo de direção (Hidráulica, Elétrica, Mecânica)")
    vehicle_type: VehicleType = Field(default=VehicleType.CARRO)
    fuel_type: FuelType = Field(default=FuelType.FLEX)
    usage_profile: UsageProfile = Field(default=UsageProfile.CONVENCIONAL)
    current_km: Optional[int] = Field(default=None, ge=0, description="Quilometragem atual no odômetro")
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

    @field_validator("plate")
    @classmethod
    def sanitize_plate(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        return v.upper().replace("-", "").strip()


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
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

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
    due_km: Optional[int] = Field(default=None, ge=0, description="Disparo por hodômetro atingido")
    recurrence: ReminderRecurrence = Field(default=ReminderRecurrence.ONCE)
    status: ReminderStatus = Field(default=ReminderStatus.PENDING)
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


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
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class ConversationTurn(BaseModel):
    """Representação de um turno de diálogo atômico."""
    role: str = Field(..., description="'user' para mensagem do condutor, 'assistant' para resposta do bot")
    content: str = Field(..., description="Texto integral da mensagem enviada ou recebida")
    timestamp: dt.datetime = Field(default_factory=dt.datetime.utcnow)


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
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

    def is_awaiting_confirmation(self) -> bool:
        """Verifica se o usuário está em um estado que aguarda resposta afirmativa/negativa explícita."""
        return self.state != ConversationState.NORMAL and self.draft_payload is not None
