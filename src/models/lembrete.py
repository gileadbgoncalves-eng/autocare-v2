"""
AutoCare V2 - Modelo ORM: Lembrete
"""

from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

if TYPE_CHECKING:
    from src.models.veiculo import Veiculo


class Lembrete(Base):
    """Entidade relacional de Lembretes de manutenção e checagens preventivas."""
    __tablename__ = "lembretes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    veiculo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("veiculos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    descricao: Mapped[str] = mapped_column(String(255), nullable=False)
    data_hora_agendada: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    odometro_alvo: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pendente", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    # Relacionamento N:1 com Veículo
    veiculo: Mapped["Veiculo"] = relationship(
        "Veiculo",
        back_populates="lembretes",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Lembrete(id={self.id}, veiculo_id={self.veiculo_id}, status='{self.status}')>"
