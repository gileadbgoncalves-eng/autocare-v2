"""
AutoCare V2 - Repositórios de Acesso a Dados
"""

from src.repositories.proprietario_repository import ProprietarioRepository
from src.repositories.veiculo_repository import VeiculoRepository
from src.repositories.manutencao_repository import ManutencaoRepository

__all__ = [
    "ProprietarioRepository",
    "VeiculoRepository",
    "ManutencaoRepository",
]
