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

    async def get_by_proprietario_id(self, proprietario_id: int) -> List[Veiculo]:
        """Retorna todos os veículos pertencentes a um proprietário."""
        stmt = select(Veiculo).where(Veiculo.proprietario_id == proprietario_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_by_model_fuzzy(
        self, query: str, proprietario_id: Optional[int] = None, threshold: float = 0.7
    ) -> List[Veiculo]:
        """
        Busca veículos por modelo ou marca utilizando correspondência aproximada (fuzzy matching).
        Permite tolerar erros de digitação como 'sandeiro' -> 'Sandero'.
        """
        import difflib

        stmt = select(Veiculo)
        if proprietario_id is not None:
            stmt = stmt.where(Veiculo.proprietario_id == proprietario_id)

        result = await self.session.execute(stmt)
        all_veiculos = list(result.scalars().all())

        query_clean = query.strip().lower()
        query_words = query_clean.split()
        matched: List[tuple[Veiculo, float]] = []

        for v in all_veiculos:
            model_lower = (v.modelo or "").lower()
            make_lower = (v.marca or "").lower()
            plate_lower = (v.placa or "").lower().replace("-", "")

            # Match exato ou substring direta
            if query_clean in model_lower or query_clean in make_lower or query_clean == plate_lower:
                matched.append((v, 1.0))
                continue

            # Similaridade fuzzy por difflib
            highest_score = 0.0
            targets = [model_lower, make_lower] + model_lower.split() + make_lower.split()
            for word in query_words:
                if len(word) < 3:
                    continue
                for target in targets:
                    if len(target) < 3:
                        continue
                    ratio = difflib.SequenceMatcher(None, word, target).ratio()
                    if ratio > highest_score:
                        highest_score = ratio

            if highest_score >= threshold:
                matched.append((v, highest_score))

        # Ordena pelos de maior pontuação
        matched.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in matched]
