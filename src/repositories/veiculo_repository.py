"""
AutoCare V2 - Repositório: Veículo (SQLAlchemy 2.0 Async)
Utiliza a sintaxe moderna select() e await session.execute().
"""

from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.veiculo import Veiculo
from src.schemas.veiculo import VeiculoCreate


class VeiculoRepository:
    """Repositório assíncrono para operações de acesso a dados da entidade Veiculo."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, data: VeiculoCreate) -> Veiculo:
        """Cria e persiste um novo veículo no banco de dados."""
        veiculo = Veiculo(
            placa=data.placa,
            marca=data.marca,
            modelo=data.modelo,
            ano=data.ano,
            quilometragem=data.quilometragem,
            proprietario_id=data.proprietario_id,
        )
        self.session.add(veiculo)
        await self.session.commit()
        await self.session.refresh(veiculo)
        return veiculo

    async def list_all(self, skip: int = 0, limit: int = 100) -> List[Veiculo]:
        """Lista veículos de forma paginada usando select()."""
        stmt = select(Veiculo).offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, veiculo_id: int) -> Optional[Veiculo]:
        """Busca veículo pelo identificador primário."""
        stmt = select(Veiculo).where(Veiculo.id == veiculo_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_placa(self, placa: str) -> Optional[Veiculo]:
        """Busca veículo pela placa única sanitizada."""
        stmt = select(Veiculo).where(Veiculo.placa == placa)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
