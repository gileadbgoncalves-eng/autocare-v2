"""
AutoCare V2 - Router: Manutenções (FastAPI)
Endpoints CRUD assíncronos para histórico e registro de manutenções veiculares.
"""

import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.repositories.manutencao_repository import ManutencaoRepository
from src.repositories.veiculo_repository import VeiculoRepository
from src.schemas.manutencao import ManutencaoCreate, ManutencaoResponse

logger = logging.getLogger("autocare.routers.manutencoes")
router = APIRouter(prefix="/manutencoes", tags=["Manutenções"])


@router.post(
    "",
    response_model=ManutencaoResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar nova manutenção",
    description="Registra serviço/peças, valida existência do veículo e atualiza o odômetro de forma protegida.",
)
async def criar_manutencao(
    dados: ManutencaoCreate,
    session: AsyncSession = Depends(get_db),
) -> ManutencaoResponse:
    """Registra uma nova manutenção e atualiza a quilometragem do veículo se for superior."""
    manutencao_repo = ManutencaoRepository(session)
    veiculo_repo = VeiculoRepository(session)

    # 1. Verifica se o veículo associado existe
    veiculo = await veiculo_repo.get_by_id(dados.veiculo_id)
    if not veiculo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Veículo com id {dados.veiculo_id} não encontrado.",
        )

    try:
        nova_manutencao = await manutencao_repo.create(dados)

        # 2. Atualização protegida de odômetro (Anti-regressão)
        if dados.odometro_registro > veiculo.quilometragem:
            veiculo.quilometragem = dados.odometro_registro
            await session.commit()
            logger.info(f"Odômetro do veículo {veiculo.id} atualizado para {dados.odometro_registro} km")

        logger.info(f"Manutenção {nova_manutencao.id} registrada com sucesso para o veículo {veiculo.id}")
        return ManutencaoResponse.model_validate(nova_manutencao)
    except Exception as exc:
        logger.error(f"Erro ao registrar manutenção: {exc}", exc_info=False)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro interno ao registrar manutenção no banco de dados.",
        )


@router.get(
    "",
    response_model=List[ManutencaoResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar manutenções",
    description="Retorna o histórico geral de manutenções registradas no sistema.",
)
async def listar_manutencoes(
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(get_db),
) -> List[ManutencaoResponse]:
    """Lista o histórico de manutenções com paginação."""
    repo = ManutencaoRepository(session)
    manutencoes = await repo.list_all(skip=skip, limit=limit)
    return [ManutencaoResponse.model_validate(m) for m in manutencoes]


@router.get(
    "/veiculo/{veiculo_id}",
    response_model=List[ManutencaoResponse],
    status_code=status.HTTP_200_OK,
    summary="Histórico de manutenções por veículo",
    description="Retorna todas as manutenções vinculadas a um veículo específico.",
)
async def listar_manutencoes_por_veiculo(
    veiculo_id: int,
    session: AsyncSession = Depends(get_db),
) -> List[ManutencaoResponse]:
    """Recupera o histórico completo do livro de bordo digital do veículo."""
    veiculo_repo = VeiculoRepository(session)
    manutencao_repo = ManutencaoRepository(session)

    veiculo = await veiculo_repo.get_by_id(veiculo_id)
    if not veiculo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Veículo com id {veiculo_id} não encontrado.",
        )

    manutencoes = await manutencao_repo.get_by_veiculo_id(veiculo_id)
    return [ManutencaoResponse.model_validate(m) for m in manutencoes]


@router.get(
    "/{manutencao_id}",
    response_model=ManutencaoResponse,
    status_code=status.HTTP_200_OK,
    summary="Buscar manutenção por ID",
)
async def obter_manutencao(
    manutencao_id: int,
    session: AsyncSession = Depends(get_db),
) -> ManutencaoResponse:
    """Busca uma manutenção específica pelo ID."""
    repo = ManutencaoRepository(session)
    manutencao = await repo.get_by_id(manutencao_id)
    if not manutencao:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manutenção com id {manutencao_id} não encontrada.",
        )
    return ManutencaoResponse.model_validate(manutencao)
