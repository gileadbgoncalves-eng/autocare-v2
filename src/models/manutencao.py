"""
AutoCare V2 - Modelo ORM: Manutenção
"""

from datetime import datetime, date
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Integer, Float, Date, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

if TYPE_CHECKING:
    from src.models.veiculo import Veiculo


class Manutencao(Base):
    """Entidade relacional de Manutenções realizadas."""
    __tablename__ = "manutencoes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    veiculo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("veiculos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    descricao: Mapped[str] = mapped_column(String(255), nullable=False)
    custo: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    data: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    odometro_registro: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    # Relacionamento N:1 com Veículo
    veiculo: Mapped["Veiculo"] = relationship(
        "Veiculo",
        back_populates="manutencoes",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Manutencao(id={self.id}, veiculo_id={self.veiculo_id}, custo={self.custo})>"
