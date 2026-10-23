"""
AutoCare V2 - Domain Enums
Módulo de enumerações canônicas de domínio, intenções de NLP e máquina de estados conversacionais.
"""

from enum import Enum


class ConversationState(str, Enum):
    """
    Estados possíveis da máquina de conversação do usuário.
    Garante o princípio Human-in-the-Loop antes de persistir dados críticos.
    """
    NORMAL = "normal"
    AWAITING_VEHICLE_REGISTRATION_CONFIRM = "awaiting_vehicle_registration_confirm"
    AWAITING_MAINTENANCE_CONFIRM = "awaiting_maintenance_confirm"
    AWAITING_REMINDER_CONFIRM = "awaiting_reminder_confirm"
    AWAITING_FINE_CONFIRM = "awaiting_fine_confirm"
    AWAITING_GARAGE_RESET_CONFIRM = "awaiting_garage_reset_confirm"
    AWAITING_VEHICLE_SELECTION = "awaiting_vehicle_selection"


class IntentType(str, Enum):
    """
    Intenções semânticas primárias compreendidas pelo Motor de IA (Gemini).
    """
    # Ações Críticas (Requerem Rascunho & Confirmação Prévia)
    REGISTER_VEHICLE = "register_vehicle"
    RECORD_MAINTENANCE = "record_maintenance"
    SET_REMINDER = "set_reminder"
    REGISTER_FINE = "register_fine"
    RESET_GARAGE = "reset_garage"

    # Atualizações Diretas
    UPDATE_ODOMETER = "update_odometer"

    # Consultas Informativas (Somente Leitura)
    QUERY_HISTORY = "query_history"
    QUERY_NEXT_MAINTENANCE = "query_next_maintenance"
    QUERY_FINANCIAL = "query_financial"
    QUERY_DOSSIER = "query_dossier"
    QUERY_FINES = "query_fines"
    QUERY_REMINDERS = "query_reminders"
    CONSULTANCY = "consultancy"

    # Diálogo & Navegação
    CONFIRM_ACTION = "confirm_action"
    CANCEL_ACTION = "cancel_action"
    POLITE_CLOSING = "polite_closing"
    GREETING_MENU = "greeting_menu"
    UNKNOWN = "unknown"


class VehicleType(str, Enum):
    """Tipos de veículos suportados e homologados."""
    CARRO = "carro"
    MOTO = "moto"
    CAMINHONETE = "caminhonete"
    OUTRO = "outro"


class FuelType(str, Enum):
    """Matrizes energéticas e tipos de combustível."""
    FLEX = "flex"
    GASOLINA = "gasolina"
    ETANOL = "etanol"
    DIESEL = "diesel"
    ELETRICO = "eletrico"
    HIBRIDO = "hibrido"
    GNV = "gnv"


class UsageProfile(str, Enum):
    """Perfil de rodagem para calibração do plano de manutenção preventiva."""
    CONVENCIONAL = "convencional"
    SEVERO = "severo"  # Trânsito urbano intenso, poeira, anda-e-para ou trabalho diário


class ReminderRecurrence(str, Enum):
    """Frequência de recorrência para notificações programadas."""
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


class ReminderStatus(str, Enum):
    """Ciclo de vida de um lembrete do sistema."""
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RecordStatus(str, Enum):
    """Ciclo de vida de registros persistidos (soft delete via cancelamento)."""
    ACTIVE = "active"
    CANCELLED = "cancelled"


class FineStatus(str, Enum):
    """Situação de uma autuação / infração de trânsito."""
    NOTIFICADA = "notificada"
    EM_RECURSO = "em_recurso"
    PAGA = "paga"
    CANCELADA = "cancelada"


class ConfidenceLevel(str, Enum):
    """Nível de certeza atribuído pelo Gemini à interpretação."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
