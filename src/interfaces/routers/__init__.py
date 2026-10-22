"""
AutoCare V2 - Roteadores da API HTTP
"""

from src.interfaces.routers.veiculos import router as veiculos_router
from src.interfaces.routers.proprietarios import router as proprietarios_router
from src.interfaces.routers.manutencoes import router as manutencoes_router

__all__ = [
    "veiculos_router",
    "proprietarios_router",
    "manutencoes_router",
]
