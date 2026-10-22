"""
AutoCare V2 - Repositório: Proprietário (SQLAlchemy 2.0 Async)
"""

from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.proprietario import Proprietario
from src.schemas.proprietario import ProprietarioCreate


class ProprietarioRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, data: ProprietarioCreate) -> Proprietario:
        proprietario = Proprietario(
            nome=data.nome,
            email=data.email,
            telefone=data.telefone,
        )
        self.session.add(proprietario)
        await self.session.commit()
        await self.session.refresh(proprietario)
        return proprietario

    async def get_by_id(self, proprietario_id: int) -> Optional[Proprietario]:
        stmt = select(Proprietario).where(Proprietario.id == proprietario_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[Proprietario]:
        stmt = select(Proprietario).where(Proprietario.email == email)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, skip: int = 0, limit: int = 100) -> List[Proprietario]:
        stmt = select(Proprietario).offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
