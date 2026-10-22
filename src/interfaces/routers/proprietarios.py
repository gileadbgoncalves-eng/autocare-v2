"""
AutoCare V2 - Router: Proprietários (FastAPI)
Endpoints CRUD assíncronos para gerenciamento de condutores e proprietários.
"""

import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.repositories.proprietario_repository import ProprietarioRepository
from src.schemas.proprietario import ProprietarioCreate, ProprietarioResponse

logger = logging.getLogger("autocare.routers.proprietarios")
router = APIRouter(prefix="/proprietarios", tags=["Proprietários"])


@router.post(
    "",
    response_model=ProprietarioResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cadastrar um novo proprietário",
    description="Cria um condutor/proprietário com validação de unicidade de e-mail.",
)
async def criar_proprietario(
    dados: ProprietarioCreate,
    session: AsyncSession = Depends(get_db),
) -> ProprietarioResponse:
    """Cria e persiste um novo proprietário no sistema."""
    repo = ProprietarioRepository(session)

    # 1. Verifica duplicidade de e-mail
    existente = await repo.get_by_email(dados.email)
    if existente:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Já existe um proprietário cadastrado com o e-mail '{dados.email}'.",
        )

    try:
        proprietario = await repo.create(dados)
        logger.info(f"Proprietário cadastrado com sucesso: ID={proprietario.id}, Email={proprietario.email}")
        return ProprietarioResponse.model_validate(proprietario)
    except Exception as exc:
        logger.error(f"Erro ao salvar proprietário: {exc}", exc_info=False)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro interno ao registrar proprietário no banco de dados.",
        )


@router.get(
    "",
    response_model=List[ProprietarioResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar proprietários",
    description="Retorna lista paginada de proprietários cadastrados.",
)
async def listar_proprietarios(
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(get_db),
) -> List[ProprietarioResponse]:
    """Lista todos os proprietários cadastrados com paginação."""
    repo = ProprietarioRepository(session)
    proprietarios = await repo.list_all(skip=skip, limit=limit)
    return [ProprietarioResponse.model_validate(p) for p in proprietarios]


@router.get(
    "/{proprietario_id}",
    response_model=ProprietarioResponse,
    status_code=status.HTTP_200_OK,
    summary="Buscar proprietário por ID",
)
async def obter_proprietario(
    proprietario_id: int,
    session: AsyncSession = Depends(get_db),
) -> ProprietarioResponse:
    """Busca um proprietário pelo ID primário."""
    repo = ProprietarioRepository(session)
    proprietario = await repo.get_by_id(proprietario_id)
    if not proprietario:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Proprietário com id {proprietario_id} não encontrado.",
        )
    return ProprietarioResponse.model_validate(proprietario)
