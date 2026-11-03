"""
AutoCare V2 - Motor de Fallback Offline Neutro (AI-First Puro)

De acordo com as diretrizes de arquitetura AI-First estrita, o motor offline
não tenta emular inteligência artificial via regex. Retorna contingência neutra.
"""

from typing import Optional, List, Dict, Any

from src.ai.schemas import (
    GeminiAnalysisResponse,
    ToolCallAction,
    VehicleDraft,
    MaintenanceDraft,
)
from src.domain.enums import IntentType, ConfidenceLevel, ConversationState


def build_offline_fallback(*args: Any, **kwargs: Any) -> GeminiAnalysisResponse:
    """Retorna contingência neutra sem tentar adivinhar intenções ou dados via regex."""
    return GeminiAnalysisResponse(
        intent=IntentType.UNKNOWN,
        confidence=ConfidenceLevel.LOW,
        is_critical_action=False,
        reply_message="Tive uma oscilação momentânea na conexão com o sistema de inteligência. Pode repetir sua mensagem em instantes?",
        reasoning="Offline fallback desativado em favor da arquitetura AI-First pura",
    )
