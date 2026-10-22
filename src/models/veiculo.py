"""
AutoCare V2 - Modelo ORM: Veículo
"""

from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import String, Integer, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

if TYPE_CHECKING:
    from src.models.proprietario import Proprietario
    from src.models.manutencao import Manutencao


class Veiculo(Base):
    """Entidade relacional do Veículo."""
    __tablename__ = "veiculos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    placa: Mapped[str] = mapped_column(String(10), unique=True, index=True, nullable=False)
    marca: Mapped[str] = mapped_column(String(50), nullable=False)
    modelo: Mapped[str] = mapped_column(String(50), nullable=False)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    quilometragem: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    proprietario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("proprietarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    # Relacionamento N:1 com Proprietário
    proprietario: Mapped["Proprietario"] = relationship(
        "Proprietario",
        back_populates="veiculos",
        lazy="selectin",
    )

    # Relacionamento 1:N com Manutenções
    manutencoes: Mapped[List["Manutencao"]] = relationship(
        "Manutencao",
        back_populates="veiculo",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Veiculo(id={self.id}, placa='{self.placa}', modelo='{self.modelo}')>"
