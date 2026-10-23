"""
AutoCare V2 - Repositório: Proprietário (SQLAlchemy 2.0 Async)
"""

from typing import List, Optional
from datetime import time
from sqlalchemy import select, and_
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
            horario_notificacao_padrao=data.horario_notificacao_padrao,
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

    async def get_by_telefone(self, telefone: str) -> Optional[Proprietario]:
        clean = "".join(filter(str.isdigit, telefone))
        stmt = select(Proprietario).where(Proprietario.telefone.like(f"%{clean}%"))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_horario_notificacao(self, target_time: time) -> List[Proprietario]:
        """Busca proprietários cujo horário padrão cadastrado coincide com a hora/minuto fornecidos."""
        stmt = select(Proprietario)
        result = await self.session.execute(stmt)
        all_props = list(result.scalars().all())
        # Filtra por hora e minuto (tolerância do minuto atual do cron)
        return [
            p for p in all_props
            if p.horario_notificacao_padrao.hour == target_time.hour
            and p.horario_notificacao_padrao.minute == target_time.minute
        ]

    async def update_horario_notificacao(self, proprietario_id: int, novo_horario: time) -> Optional[Proprietario]:
        """Atualiza a preferência de horário do Check-up Diário do proprietário."""
        prop = await self.get_by_id(proprietario_id)
        if prop:
            prop.horario_notificacao_padrao = novo_horario
            await self.session.commit()
            await self.session.refresh(prop)
        return prop

    async def list_all(self, skip: int = 0, limit: int = 100) -> List[Proprietario]:
        stmt = select(Proprietario).offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

