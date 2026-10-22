"""
AutoCare V2 - Testes de Integração do Bot de IA com Banco Relacional Real
Valida os Casos de Uso Críticos:
1. Consulta de status via linguagem natural com Regex de placa (Antiga e Mercosul) retornando dados reais do banco.
2. Extração e integridade de dados reais de veículos e manutenções.
"""

import sys
import pytest

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.infrastructure.database.session import init_db
from src.database import async_session_factory
from src.repositories.proprietario_repository import ProprietarioRepository
from src.repositories.veiculo_repository import VeiculoRepository
from src.repositories.manutencao_repository import ManutencaoRepository
from src.schemas.proprietario import ProprietarioCreate
from src.schemas.veiculo import VeiculoCreate
from src.schemas.manutencao import ManutencaoCreate
from src.application.services.conversation import ConversationService


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


@pytest.mark.asyncio
async def test_bot_consulta_status_veiculo_com_placa_mercosul():
    """
    Caso de Uso Crítico:
    Quando o usuário perguntar 'Qual o status do meu carro ABC1D23?',
    o bot deve sanitizar a placa via Regex, buscar no banco real e responder com dados precisos.
    """
    async with async_session_factory() as session:
        prop_repo = ProprietarioRepository(session)
        v_repo = VeiculoRepository(session)
        m_repo = ManutencaoRepository(session)

        # 1. Garante Proprietário
        prop = await prop_repo.get_by_email("joao.pulse@teste.com")
        if not prop:
            prop = await prop_repo.create(
                ProprietarioCreate(
                    nome="João Pulse",
                    email="joao.pulse@teste.com",
                    telefone="5521988881234",
                )
            )

        # 2. Garante Veículo com placa Mercosul
        veiculo = await v_repo.get_by_placa("ABC1D23")
        if not veiculo:
            veiculo = await v_repo.create(
                VeiculoCreate(
                    placa="ABC1D23",
                    marca="Fiat",
                    modelo="Pulse Audace",
                    ano=2023,
                    quilometragem=32000,
                    proprietario_id=prop.id,
                )
            )

        # 3. Garante Manutenção registrada
        maints = await m_repo.get_by_veiculo_id(veiculo.id)
        if not maints:
            await m_repo.create(
                ManutencaoCreate(
                    veiculo_id=veiculo.id,
                    descricao="Troca de pastilhas dianteiras e fluido DOT4",
                    custo=420.0,
                    odometro_registro=32000,
                )
            )

    # 4. Executa a consulta através do serviço conversacional do Bot
    service = ConversationService()
    user_id = "5521988881234"
    pergunta = "Qual o status do meu carro ABC1D23?"

    resultado = await service.handle_incoming_message(user_id=user_id, message_text=pergunta)

    # 5. Asserções de conformidade
    assert resultado["action_type"] == "vehicle_status_query"
    assert "ABC1D23" in resultado["reply"]
    assert "Fiat Pulse Audace" in resultado["reply"]
    assert "32,000" in resultado["reply"] or "32000" in resultado["reply"]
    assert "Troca de pastilhas dianteiras" in resultado["reply"]
    assert "420.00" in resultado["reply"]
    assert resultado["details"]["placa"] == "ABC1D23"


@pytest.mark.asyncio
async def test_bot_consulta_status_veiculo_com_placa_antiga_hifen():
    """
    Valida consulta de status com placa antiga contendo hífen: 'KZZ-9876'
    """
    async with async_session_factory() as session:
        prop_repo = ProprietarioRepository(session)
        v_repo = VeiculoRepository(session)

        prop = await prop_repo.get_by_email("ana.yaris@teste.com")
        if not prop:
            prop = await prop_repo.create(
                ProprietarioCreate(
                    nome="Ana Yaris",
                    email="ana.yaris@teste.com",
                    telefone="5521977771122",
                )
            )

        veiculo = await v_repo.get_by_placa("YAR1928")
        if not veiculo:
            veiculo = await v_repo.create(
                VeiculoCreate(
                    placa="YAR-1928",
                    marca="Toyota",
                    modelo="Yaris Sedan",
                    ano=2021,
                    quilometragem=45000,
                    proprietario_id=prop.id,
                )
            )

    service = ConversationService()
    user_id = "5521977771122"
    pergunta = "Como está a situação do meu carro YAR-1928?"

    resultado = await service.handle_incoming_message(user_id=user_id, message_text=pergunta)

    assert resultado["action_type"] == "vehicle_status_query"
    assert "YAR1928" in resultado["reply"]
    assert "Toyota Yaris Sedan" in resultado["reply"]
    assert "45,000" in resultado["reply"] or "45000" in resultado["reply"]
