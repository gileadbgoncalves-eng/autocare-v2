"""
AutoCare V2 - Schemas Pydantic v2
"""

from src.schemas.proprietario import (
    ProprietarioBase,
    ProprietarioCreate,
    ProprietarioUpdate,
    ProprietarioResponse,
)
from src.schemas.veiculo import (
    VeiculoBase,
    VeiculoCreate,
    VeiculoResponse,
    PLACA_REGEX,
)
from src.schemas.manutencao import (
    ManutencaoBase,
    ManutencaoCreate,
    ManutencaoResponse,
)
from src.schemas.lembrete import (
    LembreteBase,
    LembreteCreate,
    LembreteUpdate,
    LembreteResponse,
)

__all__ = [
    "ProprietarioBase",
    "ProprietarioCreate",
    "ProprietarioUpdate",
    "ProprietarioResponse",
    "VeiculoBase",
    "VeiculoCreate",
    "VeiculoResponse",
    "PLACA_REGEX",
    "ManutencaoBase",
    "ManutencaoCreate",
    "ManutencaoResponse",
    "LembreteBase",
    "LembreteCreate",
    "LembreteUpdate",
    "LembreteResponse",
]

