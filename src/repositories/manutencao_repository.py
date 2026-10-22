"""
AutoCare V2 - Repositório: Manutenção (SQLAlchemy 2.0 Async)
Operações assíncronas com select() para histórico de manutenções e revisões.
"""

from typing import List, Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.manutencao import Manutencao
from src.schemas.manutencao import ManutencaoCreate


class ManutencaoRepository:
    """Repositório assíncrono para operações de acesso a dados da entidade Manutencao."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, data: ManutencaoCreate) -> Manutencao:
        """Cria e persiste um novo registro de manutenção."""
        manutencao = Manutencao(
            veiculo_id=data.veiculo_id,
            descricao=data.descricao,
            custo=data.custo,
            data=data.data,
            odometro_registro=data.odometro_registro,
        )
        self.session.add(manutencao)
        await self.session.commit()
        await self.session.refresh(manutencao)
        return manutencao

    async def list_all(self, skip: int = 0, limit: int = 100) -> List[Manutencao]:
        """Lista manutenções de forma paginada ordenadas pela data decrescente."""
        stmt = select(Manutencao).order_by(Manutencao.data.desc()).offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, manutencao_id: int) -> Optional[Manutencao]:
        """Busca manutenção pelo identificador único."""
        stmt = select(Manutencao).where(Manutencao.id == manutencao_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_veiculo_id(self, veiculo_id: int) -> List[Manutencao]:
        """Recupera todo o histórico de manutenções de um veículo específico."""
        stmt = (
            select(Manutencao)
            .where(Manutencao.veiculo_id == veiculo_id)
            .order_by(Manutencao.data.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_total_cost_by_veiculo(self, veiculo_id: int) -> float:
        """Calcula a soma total dos custos de manutenção de um veículo."""
        stmt = select(func.coalesce(func.sum(Manutencao.custo), 0.0)).where(
            Manutencao.veiculo_id == veiculo_id
        )
        result = await self.session.execute(stmt)
        return float(result.scalar_one())
