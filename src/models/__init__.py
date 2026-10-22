"""
AutoCare V2 - Módulo de Modelos ORM (SQLAlchemy 2.0)
"""

from src.database import Base
from src.models.proprietario import Proprietario
from src.models.veiculo import Veiculo
from src.models.manutencao import Manutencao

__all__ = ["Base", "Proprietario", "Veiculo", "Manutencao"]
