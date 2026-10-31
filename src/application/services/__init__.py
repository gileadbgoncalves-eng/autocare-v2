"""
AutoCare V2 Application Services Package

Imports PREGUIÇOSOS (PEP 562): importar `src.application.services.parsers` (módulo neutro,
usado por `src.ai.offline_fallback`) NÃO deve arrastar `conversation` -> `src.ai.client`,
senão o ciclo conversation <-> client reaparece no nível do pacote (B15).
"""
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .conversation import ConversationService
    from .notification_service import (
        NotificationService,
        NotificationScheduler,
        notification_service,
        notification_scheduler,
    )

_LAZY = {
    "ConversationService": ".conversation",
    "NotificationService": ".notification_service",
    "NotificationScheduler": ".notification_service",
    "notification_service": ".notification_service",
    "notification_scheduler": ".notification_service",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib
        module = importlib.import_module(_LAZY[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
