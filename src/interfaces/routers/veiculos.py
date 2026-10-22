"""
AutoCare V2 - Router: Veículos (FastAPI)
Endpoints CRUD assíncronos com injeção de dependência e tratamento padronizado de erros.
"""

import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.repositories.veiculo_repository import VeiculoRepository
from src.repositories.proprietario_repository import ProprietarioRepository
from src.schemas.veiculo import VeiculoCreate, VeiculoResponse

logger = logging.getLogger("autocare.routers.veiculos")
router = APIRouter(prefix="/veiculos", tags=["Veículos"])


@router.post(
    "",
    response_model=VeiculoResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cadastrar um novo veículo",
    description="Valida a placa (antiga ou Mercosul), verifica integridade do proprietário e persiste o veículo.",
)
async def criar_veiculo(
    dados: VeiculoCreate,
    session: AsyncSession = Depends(get_db),
) -> VeiculoResponse:
    """Cria um novo veículo associado a um proprietário existente."""
    veiculo_repo = VeiculoRepository(session)
    proprietario_repo = ProprietarioRepository(session)

    # 1. Verifica se a placa já está cadastrada
    veiculo_existente = await veiculo_repo.get_by_placa(dados.placa)
    if veiculo_existente:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Já existe um veículo cadastrado com a placa '{dados.placa}'.",
        )

    # 2. Verifica se o proprietário existe
    proprietario = await proprietario_repo.get_by_id(dados.proprietario_id)
    if not proprietario:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Proprietário com id {dados.proprietario_id} não encontrado.",
        )

    try:
        novo_veiculo = await veiculo_repo.create(dados)
        logger.info(f"Veículo criado com sucesso: ID={novo_veiculo.id}, Placa={novo_veiculo.placa}")
        return VeiculoResponse.model_validate(novo_veiculo)
    except Exception as exc:
        logger.error(f"Erro inesperado ao salvar veículo: {exc}", exc_info=False)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro interno ao registrar veículo no banco de dados.",
        )


@router.get(
    "",
    response_model=List[VeiculoResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar veículos cadastrados",
    description="Retorna a lista paginada de todos os veículos cadastrados no sistema.",
)
async def listar_veiculos(
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(get_db),
) -> List[VeiculoResponse]:
    """Lista todos os veículos cadastrados de forma paginada."""
    veiculo_repo = VeiculoRepository(session)
    veiculos = await veiculo_repo.list_all(skip=skip, limit=limit)
    return [VeiculoResponse.model_validate(v) for v in veiculos]


@router.get(
    "/{veiculo_id}",
    response_model=VeiculoResponse,
    status_code=status.HTTP_200_OK,
    summary="Buscar veículo por ID",
)
async def obter_veiculo(
    veiculo_id: int,
    session: AsyncSession = Depends(get_db),
) -> VeiculoResponse:
    """Busca um veículo pelo identificador único."""
    veiculo_repo = VeiculoRepository(session)
    veiculo = await veiculo_repo.get_by_id(veiculo_id)
    if not veiculo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Veículo com id {veiculo_id} não encontrado.",
        )
    return VeiculoResponse.model_validate(veiculo)
