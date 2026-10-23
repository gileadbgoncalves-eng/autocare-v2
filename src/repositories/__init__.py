"""
AutoCare V2 - Repositórios de Acesso a Dados
"""

from src.repositories.proprietario_repository import ProprietarioRepository
from src.repositories.veiculo_repository import VeiculoRepository
from src.repositories.manutencao_repository import ManutencaoRepository
from src.repositories.lembrete_repository import LembreteRepository

__all__ = [
    "ProprietarioRepository",
    "VeiculoRepository",
    "ManutencaoRepository",
    "LembreteRepository",
]

