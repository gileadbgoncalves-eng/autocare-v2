"""
AutoCare V2 - Testes de Integração: Proprietários e Manutenções (Pytest + HTTPX Async)
Valida:
1. Criação e listagem de Proprietários (201 Created / 200 OK / 400 em duplicidade).
2. Criação e listagem de Manutenções (201 Created / 200 OK / 404 em veículo inexistente / atualização de odômetro).
"""

import sys
import pytest
from httpx import AsyncClient, ASGITransport

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.interfaces.app import app
from src.infrastructure.database.session import init_db
from src.database import async_session_factory
from src.repositories.proprietario_repository import ProprietarioRepository
from src.repositories.veiculo_repository import VeiculoRepository
from src.schemas.proprietario import ProprietarioCreate
from src.schemas.veiculo import VeiculoCreate


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


import uuid
import random

@pytest.mark.asyncio
async def test_criar_e_listar_proprietario():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = f"mariana_{uuid.uuid4().hex[:8]}@testemail.com"
        payload = {
            "nome": "Mariana Oliveira",
            "email": email,
            "telefone": "5521998877665",
        }

        # Criação
        response = await client.post("/proprietarios", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["nome"] == "Mariana Oliveira"
        assert data["email"] == email
        assert "id" in data
        prop_id = data["id"]

        # Busca por ID
        get_resp = await client.get(f"/proprietarios/{prop_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == prop_id

        # Listagem
        list_resp = await client.get("/proprietarios")
        assert list_resp.status_code == 200
        assert isinstance(list_resp.json(), list)


@pytest.mark.asyncio
async def test_criar_proprietario_duplicado_retorna_400():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = f"duplicado_{uuid.uuid4().hex[:8]}@testemail.com"
        payload = {
            "nome": "Carlos Duplicado",
            "email": email,
            "telefone": "5521911112222",
        }
        resp1 = await client.post("/proprietarios", json=payload)
        assert resp1.status_code == 201

        # Segunda tentativa com o mesmo e-mail deve falhar com 400
        resp2 = await client.post("/proprietarios", json=payload)
        assert resp2.status_code == 400
        assert "Já existe um proprietário" in resp2.json()["detail"]


@pytest.mark.asyncio
async def test_criar_e_listar_manutencao():
    # 1. Cria Proprietário e Veículo primeiro via Repositório para ter base consistente
    unique_email = f"dono_{uuid.uuid4().hex[:8]}@testemail.com"
    dig1 = random.randint(1, 9)
    dig2 = random.randint(10, 99)
    unique_plate = f"MAN{dig1}A{dig2}"

    async with async_session_factory() as session:
        prop_repo = ProprietarioRepository(session)
        veic_repo = VeiculoRepository(session)

        prop = await prop_repo.create(
            ProprietarioCreate(
                nome="Dono Manutencao",
                email=unique_email,
                telefone="5521977778888",
            )
        )

        veiculo = await veic_repo.create(
            VeiculoCreate(
                placa=unique_plate,
                marca="Volkswagen",
                modelo="Polo TSI",
                ano=2022,
                quilometragem=20000,
                proprietario_id=prop.id,
            )
        )
        veiculo_id = veiculo.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 2. Registra Manutenção
        payload_manut = {
            "veiculo_id": veiculo_id,
            "descricao": "Troca de óleo sintético 5W40 e filtro",
            "custo": 350.50,
            "odometro_registro": 25000,
        }

        resp = await client.post("/manutencoes", json=payload_manut)
        assert resp.status_code == 201
        data = resp.json()
        assert data["custo"] == 350.50
        assert data["odometro_registro"] == 25000
        assert data["veiculo_id"] == veiculo_id

        # 3. Verifica se odômetro do veículo subiu para 25.000
        v_resp = await client.get(f"/veiculos/{veiculo_id}")
        assert v_resp.status_code == 200
        assert v_resp.json()["quilometragem"] == 25000

        # 4. Listagem por veículo
        hist_resp = await client.get(f"/manutencoes/veiculo/{veiculo_id}")
        assert hist_resp.status_code == 200
        items = hist_resp.json()
        assert len(items) >= 1
        assert items[0]["veiculo_id"] == veiculo_id


@pytest.mark.asyncio
async def test_criar_manutencao_veiculo_inexistente_retorna_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "veiculo_id": 9999999,
            "descricao": "Revisão geral",
            "custo": 500.0,
            "odometro_registro": 50000,
        }
        resp = await client.post("/manutencoes", json=payload)
        assert resp.status_code == 404
        assert "não encontrado" in resp.json()["detail"]
