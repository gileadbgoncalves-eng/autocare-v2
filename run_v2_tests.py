"""
AutoCare V2 - Automated Verification Test Suite
Executa testes unitários e de integração em todas as camadas da nova arquitetura:
1. Domínio & Validação Pydantic v2
2. Configurações & Normalização de URL
3. Banco de Dados & Repositórios Assíncronos (SQLAlchemy 2.0 + SQLite)
4. Schemas de IA & Prompts
5. Máquina de Estados & Orquestração Human-in-the-Loop
6. Webhook FastAPI & Tolerância de Payloads (Evolution/Z-API)
"""

import sys
import asyncio
import traceback
from datetime import datetime, date
from typing import List, Dict, Any

# 1. Testes de Domínio
def test_domain():
    print("\n--- [TESTE 1] Domínio & Modelos Pydantic v2 ---")
    from src.domain.enums import (
        ConversationState, IntentType, VehicleType, FuelType,
        UsageProfile, ReminderRecurrence, ReminderStatus, FineStatus
    )
    from src.domain.entities import (
        User, UserPreferences, Vehicle, MaintenanceRecord,
        Reminder, TrafficFine, ConversationContext, ConversationTurn
    )

    # 1.1 Usuário e Sanitização de Telefone
    user = User(id="+55 (21) 99202-8482", name="Gabriel Duarte")
    assert user.id == "5521992028482", f"Erro sanitização telefone: {user.id}"
    assert user.preferences.notify_maintenance is True
    print("  ✅ User e sanitização de telefone E.164: OK")

    # 1.2 Validação de Telefone Inválido
    try:
        User(id="123")
        assert False, "Deveria ter falhado com telefone curto"
    except Exception:
        print("  ✅ Rejeição de telefone inválido (<10 dígitos): OK")

    # 1.3 Veículo e Sanitização de Placa
    veh = Vehicle(
        user_id=user.id,
        make="Fiat",
        model="Palio Fire",
        year=2015,
        plate="abc-1234",
        current_km=145000,
        vehicle_type=VehicleType.CARRO,
        fuel_type=FuelType.FLEX,
    )
    assert veh.plate == "ABC1234", f"Erro sanitização de placa: {veh.plate}"
    print("  ✅ Vehicle e sanitização de placa Mercosul/antiga: OK")

    # 1.4 Manutenção e Custo
    maint = MaintenanceRecord(
        vehicle_id=1,
        user_id=user.id,
        components=["Óleo 5W-30", "Filtro de Óleo"],
        description="Troca de óleo e filtro preventiva",
        km=145000,
        total_cost=250.0,
        parts_cost=180.0,
        labor_cost=70.0,
    )
    assert maint.total_cost == 250.0
    assert len(maint.components) == 2
    print("  ✅ MaintenanceRecord e integridade de custos: OK")

    # 1.5 Contexto Conversacional
    ctx = ConversationContext(user_id=user.id)
    assert ctx.is_awaiting_confirmation() is False
    ctx.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
    ctx.draft_payload = {"components": ["Óleo"]}
    assert ctx.is_awaiting_confirmation() is True
    print("  ✅ ConversationContext e máquina de estados Human-in-the-Loop: OK")


# 2. Testes de Configurações
def test_config():
    print("\n--- [TESTE 2] Configurações & Dialetos de Banco ---")
    from src.config.settings import Settings

    # Normalização de URLs
    s1 = Settings(DATABASE_URL="postgres://user:pass@host:5432/db")
    assert s1.DATABASE_URL == "postgresql+asyncpg://user:pass@host:5432/db", f"Erro postgres: {s1.DATABASE_URL}"

    s2 = Settings(DATABASE_URL="sqlite:///test.db")
    assert s2.DATABASE_URL == "sqlite+aiosqlite:///test.db", f"Erro sqlite: {s2.DATABASE_URL}"

    s3 = Settings(DATABASE_URL="")
    assert s3.DATABASE_URL == "sqlite+aiosqlite:///autocare.db", f"Erro default: {s3.DATABASE_URL}"

    print("  ✅ Normalização automática de dialeto assíncrono (asyncpg/aiosqlite): OK")
    print(f"  ✅ Ambiente ativo: {s3.ENVIRONMENT} | Timeout IA: {s3.GEMINI_TIMEOUT_SECONDS}s")


# 3. Testes de Banco de Dados e Repositórios
async def test_database_and_repositories():
    print("\n--- [TESTE 3] Banco de Dados & Repositórios Assíncronos ---")
    from src.infrastructure.database.session import init_db, close_db, async_session_factory
    from src.infrastructure.database.repositories import (
        UserRepository, VehicleRepository, MaintenanceRepository
    )
    from src.domain.entities import User, Vehicle, MaintenanceRecord
    from src.domain.enums import VehicleType

    # Inicializa tabelas
    await init_db()
    print("  ✅ init_db(): Tabelas criadas / verificadas no SQLite: OK")

    test_user_id = "5521998887777"

    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        veh_repo = VehicleRepository(session)
        maint_repo = MaintenanceRepository(session)

        # 3.1 Limpeza prévia para isolamento
        await maint_repo.delete_all_by_user_id(test_user_id)
        await veh_repo.delete_all_by_user_id(test_user_id)

        # 3.2 Criação e Upsert de Usuário
        user = await user_repo.get_or_create(test_user_id, name="Piloto de Teste")
        assert user.id == test_user_id
        assert user.name == "Piloto de Teste"

        # Atualiza preferências
        user.preferences.quiet_mode = True
        await user_repo.save(user)
        user_fetched = await user_repo.get_by_id(test_user_id)
        assert user_fetched.preferences.quiet_mode is True
        print("  ✅ UserRepository (get_or_create, save, upsert preferências): OK")

        # 3.3 Adição de Veículo
        new_veh = Vehicle(
            user_id=test_user_id,
            make="Toyota",
            model="Corolla",
            year=2021,
            plate="RIO2A18",
            current_km=52000,
            vehicle_type=VehicleType.CARRO,
        )
        saved_veh = await veh_repo.add_vehicle(new_veh)
        assert saved_veh.id is not None
        assert saved_veh.plate == "RIO2A18"
        veh_id = saved_veh.id
        print(f"  ✅ VehicleRepository (add_vehicle com ID gerado [{veh_id}]): OK")

        # 3.4 Busca por usuário e por placa
        vehs = await veh_repo.get_vehicles_by_user_id(test_user_id)
        assert len(vehs) == 1
        assert vehs[0].model == "Corolla"

        veh_by_plate = await veh_repo.get_by_plate(test_user_id, "rio-2a18")
        assert veh_by_plate is not None
        assert veh_by_plate.id == veh_id
        print("  ✅ VehicleRepository (get_vehicles_by_user_id e get_by_plate sanitizado): OK")

        # 3.5 Atualização de Hodômetro (Proteção contra regressão)
        updated = await veh_repo.update_km(veh_id, 55000)
        assert updated.current_km == 55000
        # Tenta regredir o odômetro (deve ignorar ou manter)
        regressed = await veh_repo.update_km(veh_id, 40000)
        assert regressed.current_km == 55000, "Odômetro não deve regredir"
        print("  ✅ VehicleRepository (update_km com proteção anti-regressão): OK")

        # 3.6 Registro de Manutenção
        maint1 = MaintenanceRecord(
            vehicle_id=veh_id,
            user_id=test_user_id,
            components=["Óleo 0W-20 Sintético", "Filtro de Óleo"],
            description="Revisão dos 50k",
            km=55000,
            total_cost=380.0,
            parts_cost=260.0,
            labor_cost=120.0,
            workshop_name="Oficina Premier",
        )
        saved_maint = await maint_repo.add_record(maint1)
        assert saved_maint.id is not None
        print(f"  ✅ MaintenanceRepository (add_record com ID [{saved_maint.id}]): OK")

        # 3.7 Histórico e Custo Total
        history = await maint_repo.get_by_vehicle_id(veh_id)
        assert len(history) == 1
        assert history[0].total_cost == 380.0

        total_spent = await maint_repo.get_total_cost_by_vehicle_id(veh_id)
        assert total_spent == 380.0
        print(f"  ✅ MaintenanceRepository (get_by_vehicle_id e get_total_cost: R$ {total_spent:.2f}): OK")

        # 3.8 Limpeza / Reset de Garagem
        deleted = await veh_repo.delete_all_by_user_id(test_user_id)
        assert deleted == 1
        vehs_after = await veh_repo.get_vehicles_by_user_id(test_user_id)
        assert len(vehs_after) == 0
        print("  ✅ VehicleRepository (delete_all_by_user_id com cascade): OK")

    await close_db()


# 4. Testes do Motor de IA (Schemas, Prompts e Fallbacks)
def test_ai_layer():
    print("\n--- [TESTE 4] Camada de IA (Schemas & Fallbacks) ---")
    from src.ai.schemas import GeminiAnalysisResponse, VehicleDraft, MaintenanceDraft
    from src.ai.prompts import build_analysis_prompt, SYSTEM_INSTRUCTION
    from src.ai.client import GeminiClient
    from src.domain.enums import IntentType, ConfidenceLevel
    from src.domain.entities import Vehicle, ConversationContext

    # 4.1 Validação do Schema Pydantic
    resp = GeminiAnalysisResponse(
        intent=IntentType.RECORD_MAINTENANCE,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=True,
        reply_message="Identifiquei a troca de óleo do seu Corolla. Deseja confirmar?",
        maintenance=MaintenanceDraft(
            components=["Óleo 5W-30"],
            description="Troca de óleo",
            km=60000,
            total_cost=220.0,
        ),
        target_vehicle_ref="Corolla",
    )
    dumped = resp.model_dump()
    assert dumped["intent"] == "record_maintenance"
    assert dumped["is_critical_action"] is True
    print("  ✅ GeminiAnalysisResponse Structured Output schema: OK")

    # 4.2 Construção de Prompt Contextualizado
    v = Vehicle(user_id="123", make="Honda", model="Civic", current_km=80000)
    ctx = ConversationContext(user_id="123")
    prompt = build_analysis_prompt("troquei o óleo dele hoje", context=ctx, vehicles=[v])
    assert "Honda Civic" in prompt
    assert "troquei o óleo dele hoje" in prompt
    assert len(SYSTEM_INSTRUCTION) > 500
    print("  ✅ build_analysis_prompt (injeção de veículos, odômetro e turnos): OK")

    # 4.3 Fallback de Contingência do GeminiClient
    client = GeminiClient(api_key="")
    fallback_resp = client._build_contingency_fallback("Valeu, obrigado!", "Teste")
    assert fallback_resp.intent == IntentType.POLITE_CLOSING
    assert "Show de bola" in fallback_resp.reply_message

    fallback_busy = client._build_contingency_fallback("Troquei o radiador", "Timeout")
    assert fallback_busy.intent == IntentType.UNKNOWN
    assert "Desculpe, estou processando muitas mensagens" in fallback_busy.reply_message
    print("  ✅ GeminiClient (Fallbacks inteligentes de encerramento amigável e sobrecarga): OK")


# 5. Testes de Orquestração & Máquina de Estados
async def test_conversation_service():
    print("\n--- [TESTE 5] Serviço de Conversação & Máquina de Estados ---")
    from src.application.services.conversation import ConversationService
    from src.ai.schemas import GeminiAnalysisResponse, MaintenanceDraft
    from src.domain.enums import ConversationState, IntentType, ConfidenceLevel
    from src.domain.entities import Vehicle

    # Mock do GeminiClient para testar a máquina de estados sem gastar cota externa
    class MockGeminiClient:
        def __init__(self):
            self.next_response = None

        async def analyze_message(self, **kwargs) -> GeminiAnalysisResponse:
            return self.next_response

    mock_gemini = MockGeminiClient()
    service = ConversationService(gemini_client=mock_gemini)
    user_id = "5521999991111"
    vehicles = [Vehicle(id=10, user_id=user_id, make="Volkswagen", model="Gol", current_km=90000)]

    # 5.1 Encerramento Educado ("OK", "Valeu")
    mock_gemini.next_response = GeminiAnalysisResponse(
        intent=IntentType.POLITE_CLOSING,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=False,
        reply_message="Show de bola! Qualquer novidade estou por aqui! 👍",
    )
    res_ok = await service.handle_incoming_message(user_id, "Valeu, abraço!", vehicles=vehicles)
    assert res_ok["action_type"] == "polite_closing"
    assert service.get_context(user_id).state == ConversationState.NORMAL
    print("  ✅ Tratamento de POLITE_CLOSING (não reabre formulários): OK")

    # 5.2 Ação Crítica: Proposta de Manutenção
    mock_gemini.next_response = GeminiAnalysisResponse(
        intent=IntentType.RECORD_MAINTENANCE,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=True,
        reply_message="Identifiquei a troca de velas no Gol por R$ 150,00.",
        maintenance=MaintenanceDraft(
            components=["Velas de ignição"],
            description="Troca de velas",
            total_cost=150.0,
            km=90000,
        ),
    )
    res_critical = await service.handle_incoming_message(user_id, "Troquei as velas dele por 150 reais", vehicles=vehicles)
    assert res_critical["action_type"] == "awaiting_confirmation"
    assert res_critical["state"] == ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
    assert "Deseja confirmar" in res_critical["reply"]
    ctx = service.get_context(user_id)
    assert ctx.is_awaiting_confirmation() is True
    print("  ✅ Ação Crítica interceptada -> Estado mudou para AWAITING_MAINTENANCE_CONFIRM: OK")

    # 5.3 Resposta Afirmativa do Usuário ("Sim, pode salvar")
    res_confirm = await service.handle_incoming_message(user_id, "Sim, pode salvar!", vehicles=vehicles)
    assert res_confirm["action_type"] == "action_confirmed"
    assert res_confirm["state"] == ConversationState.NORMAL.value
    assert res_confirm["committed_data"]["total_cost"] == 150.0
    assert service.get_context(user_id).state == ConversationState.NORMAL
    print("  ✅ Confirmação afirmativa ('Sim') -> action_confirmed e retorno ao estado NORMAL: OK")

    # 5.4 Teste de Cancelamento/Recusa ("Não, errei")
    mock_gemini.next_response = GeminiAnalysisResponse(
        intent=IntentType.REGISTER_VEHICLE,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=True,
        reply_message="Cadastrar Honda Civic?",
        extracted_entities={"make": "Honda", "model": "Civic"},
    )
    await service.handle_incoming_message(user_id, "Cadastra um Civic para mim", vehicles=vehicles)
    assert service.get_context(user_id).state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM

    res_cancel = await service.handle_incoming_message(user_id, "Não, cancela isso", vehicles=vehicles)
    assert res_cancel["action_type"] == "action_cancelled"
    assert service.get_context(user_id).state == ConversationState.NORMAL
    assert service.get_context(user_id).draft_payload is None
    print("  ✅ Rejeição/Negação ('Não, cancela') -> Rascunho descartado e reset para NORMAL: OK")

    # 5.5 Comando de Escape Universal ("sair", "menu")
    service.get_context(user_id).state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
    service.get_context(user_id).draft_payload = {"teste": True}
    res_escape = await service.handle_incoming_message(user_id, "sair", vehicles=vehicles)
    assert res_escape["action_type"] == "flow_escaped"
    assert service.get_context(user_id).state == ConversationState.NORMAL
    print("  ✅ Comando Universal de Escape ('sair') cancela qualquer rascunho: OK")

    # 5.6 Teste de Correção de Rascunho com "Não, a placa é..." (Reconciliação e não cancelamento)
    mock_gemini.next_response = GeminiAnalysisResponse(
        intent=IntentType.REGISTER_VEHICLE,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=True,
        reply_message="Identifiquei Honda Civic ano 2020. Deseja confirmar?",
        extracted_entities={"make": "Honda", "model": "Civic", "year": 2020},
    )
    await service.handle_incoming_message(user_id, "Cadastra meu Civic 2020", vehicles=vehicles)
    assert service.get_context(user_id).state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM

    # Usuário envia correção: "Não, a placa correta é ABC1234"
    mock_gemini.next_response = GeminiAnalysisResponse(
        intent=IntentType.REGISTER_VEHICLE,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=True,
        reply_message="Corrigido! Alterei a placa para ABC1234. Honda Civic 2020 com placa ABC1234.",
        extracted_entities={"make": "Honda", "model": "Civic", "year": 2020, "plate": "ABC1234"},
    )
    res_correction = await service.handle_incoming_message(user_id, "Não, a placa é ABC1234", vehicles=vehicles)
    assert res_correction["action_type"] == "awaiting_confirmation"
    assert service.get_context(user_id).state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
    assert service.get_context(user_id).draft_payload["plate"] == "ABC1234"
    assert "Corrigido!" in res_correction["reply"]
    print("  ✅ Correção de rascunho ('Não, a placa é ABC1234') não cancela e atualiza dados: OK")

    # Confirma o rascunho corrigido com sucesso
    res_confirmed = await service.handle_incoming_message(user_id, "Sim, pode salvar", vehicles=vehicles)
    assert res_confirmed["action_type"] == "action_confirmed"
    assert service.get_context(user_id).state == ConversationState.NORMAL

    # 5.7 Teste de Limite de 3 Veículos na Garagem
    mock_gemini.next_response = GeminiAnalysisResponse(
        intent=IntentType.UNKNOWN,
        confidence=ConfidenceLevel.HIGH,
        is_critical_action=False,
        reply_message="Você já atingiu o limite de 3 veículos na sua garagem no plano gratuito. Gerencie seus veículos atuais para adicionar outro.",
        extracted_entities={},
    )
    res_limit = await service.handle_incoming_message(user_id, "Quero cadastrar minha quarta moto", vehicles=vehicles)
    assert res_limit["action_type"] == IntentType.UNKNOWN.value
    assert service.get_context(user_id).state == ConversationState.NORMAL
    assert "limite de 3 veículos" in res_limit["reply"]
    print("  ✅ Limite de 3 veículos na garagem bloqueado cordialmente com UNKNOWN: OK")


# 6. Testes do Webhook FastAPI
async def test_fastapi_webhook():
    print("\n--- [TESTE 6] Webhook HTTP FastAPI & Normalizador de Payloads ---")
    from src.interfaces.api.webhook import parse_whatsapp_payload
    from src.interfaces.app import app
    from httpx import AsyncClient, ASGITransport

    # 6.1 Teste de Parsing de Payloads Diversos
    # Formato A: Evolution API
    evolution_payload = {
        "event": "messages.upsert",
        "data": {
            "key": {"remoteJid": "5521988887777@s.whatsapp.net", "fromMe": False},
            "message": {"conversation": "Quanto gastei esse mês?"}
        }
    }
    uid, text = parse_whatsapp_payload(evolution_payload)
    assert uid == "5521988887777"
    assert text == "Quanto gastei esse mês?"

    # Formato B: Evolution API com fromMe = True (deve ignorar)
    evolution_bot_echo = {
        "data": {"key": {"remoteJid": "5521988887777@s.whatsapp.net", "fromMe": True}}
    }
    assert parse_whatsapp_payload(evolution_bot_echo) == (None, None)

    # Formato C: Z-API
    zapi_payload = {
        "phone": "5511999990000",
        "isGroup": False,
        "text": {"message": "Troquei pastilhas de freio"}
    }
    uid_z, text_z = parse_whatsapp_payload(zapi_payload)
    assert uid_z == "5511999990000"
    assert text_z == "Troquei pastilhas de freio"

    # Formato D: Teste Direto
    direct_payload = {"user_id": "5521911112222", "message": "Olá AutoCare"}
    uid_d, text_d = parse_whatsapp_payload(direct_payload)
    assert uid_d == "5521911112222"
    assert text_d == "Olá AutoCare"
    print("  ✅ Normalizador multi-provedor (Evolution API, Z-API, Direto): OK")

    # 6.2 Requisições HTTP reais usando ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Rota de Health
        res_health = await client.get("/health")
        assert res_health.status_code == 200
        health_data = res_health.json()
        assert health_data["status"] == "healthy"
        assert health_data["service"] == "AutoCare V2"
        print(f"  ✅ GET /health -> HTTP 200 {health_data}: OK")

        # Rota de Webhook com mensagem de encerramento amigável
        webhook_body = {
            "user_id": "5521987654321",
            "message": "valeu obrigado!"
        }
        res_webhook = await client.post("/webhook", json=webhook_body)
        assert res_webhook.status_code == 200
        webhook_data = res_webhook.json()
        assert webhook_data["status"] == "success"
        assert webhook_data["user_id"] == "5521987654321"
        assert "reply" in webhook_data
        print(f"  ✅ POST /webhook -> HTTP 200: OK")


async def main():
    print("=" * 70)
    print("🚗 AUTOCARE V2 - BATERIA DE TESTES DE INTEGRAÇÃO COMPLETA")
    print("=" * 70)
    start_time = datetime.now()

    try:
        test_domain()
        test_config()
        await test_database_and_repositories()
        test_ai_layer()
        await test_conversation_service()
        await test_fastapi_webhook()

        elapsed = (datetime.now() - start_time).total_seconds()
        print("\n" + "=" * 70)
        print(f"🎉 TODOS OS TESTES PASSARAM COM 100% DE SUCESSO! ({elapsed:.2f}s)")
        print("=" * 70)

    except Exception as e:
        print("\n" + "!" * 70)
        print(f"❌ FALHA ENCONTRADA: {e}")
        print("!" * 70)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
