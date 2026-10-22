"""
AutoCare V2 - Modelo ORM: Proprietário
"""

from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

if TYPE_CHECKING:
    from src.models.veiculo import Veiculo


class Proprietario(Base):
    """Entidade relacional do Proprietário de veículos."""
    __tablename__ = "proprietarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    telefone: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    # Relacionamento 1:N com Veículo (carregamento assíncrono otimizado com selectin)
    veiculos: Mapped[List["Veiculo"]] = relationship(
        "Veiculo",
        back_populates="proprietario",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Proprietario(id={self.id}, nome='{self.nome}', email='{self.email}')>"
