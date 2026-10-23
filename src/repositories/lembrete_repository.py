"""
AutoCare V2 - Repositório: Lembrete (SQLAlchemy 2.0 Async)
"""

from typing import List, Optional
from datetime import datetime
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.lembrete import Lembrete
from src.schemas.lembrete import LembreteCreate, LembreteUpdate


class LembreteRepository:
    """Repositório assíncrono para operações de acesso a dados da entidade Lembrete."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, data: LembreteCreate) -> Lembrete:
        """Cria e persiste um novo lembrete."""
        lembrete = Lembrete(
            veiculo_id=data.veiculo_id,
            descricao=data.descricao,
            data_hora_agendada=data.data_hora_agendada,
            odometro_alvo=data.odometro_alvo,
            status=data.status,
        )
        self.session.add(lembrete)
        await self.session.commit()
        await self.session.refresh(lembrete)
        return lembrete

    async def get_by_id(self, lembrete_id: int) -> Optional[Lembrete]:
        """Busca lembrete pelo ID primário."""
        stmt = select(Lembrete).where(Lembrete.id == lembrete_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_veiculo_id(self, veiculo_id: int) -> List[Lembrete]:
        """Lista todos os lembretes de um veículo."""
        stmt = select(Lembrete).where(Lembrete.veiculo_id == veiculo_id).order_by(Lembrete.id.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_due_scheduled_reminders(self, current_dt: Optional[datetime] = None) -> List[Lembrete]:
        """
        Retorna lembretes agendados para horários específicos que já venceram
        (data_hora_agendada <= now e status in ('pendente', 'agendado')).
        """
        now = current_dt or datetime.now()
        stmt = select(Lembrete).where(
            and_(
                Lembrete.data_hora_agendada.is_not(None),
                Lembrete.data_hora_agendada <= now,
                Lembrete.status.in_(["pendente", "agendado"]),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_general_pending_for_vehicle(
        self, veiculo_id: int, current_dt: Optional[datetime] = None
    ) -> List[Lembrete]:
        """
        Retorna lembretes elegíveis para o Boletim Diário geral da garagem.
        REGRA CRÍTICA: Se data_hora_agendada for explícita e futura (data_hora_agendada > now),
        ele DEVE SER IGNORADO para evitar disparo duplo / spam fora de hora.
        """
        now = current_dt or datetime.now()
        stmt = select(Lembrete).where(
            and_(
                Lembrete.veiculo_id == veiculo_id,
                Lembrete.status.in_(["pendente", "agendado"]),
                or_(
                    Lembrete.data_hora_agendada.is_(None),
                    Lembrete.data_hora_agendada <= now,
                ),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(self, lembrete_id: int, new_status: str) -> Optional[Lembrete]:
        """Atualiza o status do lembrete (ex: concluido, disparado)."""
        lembrete = await self.get_by_id(lembrete_id)
        if lembrete:
            lembrete.status = new_status
            await self.session.commit()
            await self.session.refresh(lembrete)
        return lembrete
