"""
AutoCare V2 - Testes de Integração da API de Veículos (Pytest + HTTPX Async)
Valida rigorosamente:
1. Rejeição com HTTP 422 Unprocessable Entity para placas inválidas (ex: 'ABC-12X4').
2. Aceitação com HTTP 201 Created e sanitização para placas no padrão antigo (ex: 'ABC-1234' -> 'ABC1234').
3. Aceitação com HTTP 201 Created para placas no padrão Mercosul (ex: 'bra2e19' -> 'BRA2E19').
4. Listagem com HTTP 200 OK.
"""

import sys
import pytest
from httpx import AsyncClient, ASGITransport

# Configuração de UTF-8 para o terminal Windows
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
from src.schemas.proprietario import ProprietarioCreate


@pytest.fixture(autouse=True)
async def setup_database():
    """Garante que as tabelas do banco de dados estejam criadas antes dos testes."""
    await init_db()


@pytest.fixture
async def proprietario_teste():
    """Cria um proprietário padrão no banco para associação nos testes de veículos."""
    async with async_session_factory() as session:
        repo = ProprietarioRepository(session)
        email = "condutor.teste@autocare.com.br"
        existente = await repo.get_by_email(email)
        if existente:
            return existente
        novo = await repo.create(
            ProprietarioCreate(
                nome="Condutor de Testes AutoCare",
                email=email,
                telefone="5511999998888",
            )
        )
        return novo


@pytest.mark.asyncio
async def test_criar_veiculo_placa_invalida_retorna_422(proprietario_teste):
    """
    Deve rejeitar placas que desrespeitam os padrões Antigo e Mercosul com status 422.
    Exemplo: 'ABC-12X4' (letra na posição numérica final).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload_invalido = {
            "placa": "ABC-12X4",
            "marca": "Chevrolet",
            "modelo": "Onix",
            "ano": 2021,
            "quilometragem": 35000,
            "proprietario_id": proprietario_teste.id,
        }

        response = await client.post("/veiculos", json=payload_invalido)

        assert response.status_code == 422, f"Esperado 422, recebido {response.status_code}: {response.text}"
        data = response.json()
        assert "detail" in data
        # Confirma que a validação falhou especificamente no campo placa
        erros_campos = [err.get("loc")[-1] for err in data["detail"] if "loc" in err]
        assert "placa" in erros_campos


import random

@pytest.mark.asyncio
async def test_criar_veiculo_placa_valida_antiga_retorna_201(proprietario_teste):
    """
    Deve aceitar placas válidas no formato antigo ('ABC-1234'),
    sanitizando hífen e salvando como 'ABC1234' com status 201 Created.
    """
    placa_num = random.randint(1000, 9999)
    placa_antiga = f"OLD-{placa_num}"
    placa_esperada = f"OLD{placa_num}"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload_valido_antigo = {
            "placa": placa_antiga,
            "marca": "Honda",
            "modelo": "Civic Touring",
            "ano": 2019,
            "quilometragem": 48000,
            "proprietario_id": proprietario_teste.id,
        }

        response = await client.post("/veiculos", json=payload_valido_antigo)

        assert response.status_code == 201, f"Esperado 201, recebido {response.status_code}: {response.text}"
        data = response.json()
        assert data["placa"] == placa_esperada
        assert data["marca"] == "Honda"
        assert data["modelo"] == "Civic Touring"
        assert data["proprietario_id"] == proprietario_teste.id
        assert "id" in data


@pytest.mark.asyncio
async def test_criar_veiculo_placa_valida_mercosul_retorna_201(proprietario_teste):
    """
    Deve aceitar placas válidas no padrão Mercosul em minúsculas ('bra2e19'),
    sanitizando e normalizando para 'BRA2E19' com status 201 Created.
    """
    import random
    import string
    prefix = "".join(random.choices(string.ascii_lowercase, k=3))
    dig1 = random.randint(1, 9)
    dig2 = random.randint(10, 99)
    placa_mercosul_input = f"{prefix}{dig1}e{dig2}"
    placa_mercosul_esperada = f"{prefix.upper()}{dig1}E{dig2}"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload_mercosul = {
            "placa": placa_mercosul_input,
            "marca": "Toyota",
            "modelo": "Corolla Altis Hybrid",
            "ano": 2023,
            "quilometragem": 18500,
            "proprietario_id": proprietario_teste.id,
        }

        response = await client.post("/veiculos", json=payload_mercosul)

        assert response.status_code == 201, f"Esperado 201, recebido {response.status_code}: {response.text}"
        data = response.json()
        assert data["placa"] == placa_mercosul_esperada
        assert data["modelo"] == "Corolla Altis Hybrid"


@pytest.mark.asyncio
async def test_listar_veiculos_retorna_200():
    """Deve listar os veículos cadastrados com status 200 OK."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/veiculos")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
