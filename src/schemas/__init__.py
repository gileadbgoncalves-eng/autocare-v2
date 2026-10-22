"""
AutoCare V2 - Schemas Pydantic v2
"""

from src.schemas.proprietario import (
    ProprietarioBase,
    ProprietarioCreate,
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

__all__ = [
    "ProprietarioBase",
    "ProprietarioCreate",
    "ProprietarioResponse",
    "VeiculoBase",
    "VeiculoCreate",
    "VeiculoResponse",
    "PLACA_REGEX",
    "ManutencaoBase",
    "ManutencaoCreate",
    "ManutencaoResponse",
]
