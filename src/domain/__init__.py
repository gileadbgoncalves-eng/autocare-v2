"""AutoCare V2 Domain Package"""
from .enums import (
    ConversationState,
    IntentType,
    VehicleType,
    FuelType,
    UsageProfile,
    ReminderRecurrence,
    ReminderStatus,
    FineStatus,
    ConfidenceLevel,
)
from .entities import (
    UserPreferences,
    User,
    Vehicle,
    MaintenanceRecord,
    Reminder,
    TrafficFine,
    ConversationTurn,
    ConversationContext,
)

__all__ = [
    # Enums
    "ConversationState",
    "IntentType",
    "VehicleType",
    "FuelType",
    "UsageProfile",
    "ReminderRecurrence",
    "ReminderStatus",
    "FineStatus",
    "ConfidenceLevel",
    # Entities
    "UserPreferences",
    "User",
    "Vehicle",
    "MaintenanceRecord",
    "Reminder",
    "TrafficFine",
    "ConversationTurn",
    "ConversationContext",
]
