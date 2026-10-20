"""
AutoCare V2 - Deep Architectural, Persistence & Security Validation Suite (deep_validation_tester.py)

Suíte de homologação profunda não-interruptiva (non-stop execution):
1. Roteamento Exato para 2º Veículo (Moto vs Carro) - Validação direta SQL em maintenance_records
2. Persistência Real de Lembretes, Multas e Odômetro - Validação direta SQL em reminders, traffic_fines e vehicles
3. Teste Cego de Consulta do Banco de Dados (Memória Zep/Contexto limpo, prompt injeta dados reais)
4. Idempotência e Concorrência Double-Click (asyncio.gather com 2 'Sim' simultâneos)
5. Bloqueio de Reset Indireto (Bypass bloqueado para 'zerar garagem', exige confirmação e cancelamento com 'Não')
6. Auditoria Estrita de Logs (Verificação de não vazamento da GEMINI_API_KEY no stream httpx)
7. Parser de Payloads Reais de Provedores (Evolution API e Z-API ignoram @g.us e fromMe: True com HTTP 200)
"""

import sys
import io
import json
import time
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, date

import httpx
from httpx import ASGITransport

# Configura encoding UTF-8 no terminal Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config.settings import settings
from src.interfaces.app import app
from src.interfaces.api.webhook import conversation_service
from src.infrastructure.database.session import async_session_factory
from src.infrastructure.database.models import (
    UserModel,
    VehicleModel,
    MaintenanceRecordModel,
    ReminderModel,
    TrafficFineModel,
)
from src.infrastructure.database.repositories import (
    UserRepository,
    VehicleRepository,
    MaintenanceRepository,
    ReminderRepository,
    TrafficFineRepository,
)
from src.domain.entities import Vehicle, MaintenanceRecord, Reminder, TrafficFine
from src.domain.enums import VehicleType, FuelType, UsageProfile, FineStatus, ReminderStatus

# Cores ANSI para formatação elegante no terminal
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_BOLD = "\033[1m"
COLOR_RESET = "\033[0m"


class DeepValidationTester:
    """Suíte de testes de validação arquitetural profunda e auditoria SQL."""

    def __init__(self):
        self.results: List[Dict[str, Any]] = []
        self.passed_count = 0
        self.failed_count = 0
        self.report_file = "deep_audit_report.json"

    async def _send_webhook(self, user_id: str, message: str) -> Tuple[int, Dict[str, Any], float]:
        """Dispara mensagem HTTP POST assíncrona ao webhook via transporte ASGI em memória."""
        payload = {"user_id": user_id, "message": message}
        return await self._send_raw_webhook(payload)

    async def _send_raw_webhook(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any], float]:
        """Dispara payload arbitrário para o endpoint /webhook."""
        t0 = time.time()
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as client:
            resp = await client.post("/webhook", json=payload)
            elapsed_ms = (time.time() - t0) * 1000.0
            try:
                data = resp.json()
            except Exception:
                data = {"raw_text": resp.text}
            return resp.status_code, data, elapsed_ms

    async def _reset_user(self, user_id: str):
        """Zera dados no banco e limpa contexto em memória."""
        async with async_session_factory() as session:
            repo = UserRepository(session)
            await repo.reset_user_data(user_id)
        conversation_service.clear_context(user_id)

    def _record_result(self, test_name: str, passed: bool, details: Dict[str, Any]):
        """Registra resultado de um teste sem interromper a suíte."""
        if passed:
            self.passed_count += 1
            status_str = "PASS"
            print(f"  {COLOR_GREEN}✅ [{status_str}] {test_name}{COLOR_RESET}")
        else:
            self.failed_count += 1
            status_str = "FAILED"
            print(f"  {COLOR_RED}❌ [{status_str}] {test_name}{COLOR_RESET}")
            if "error" in details:
                print(f"     {COLOR_RED}Motivo: {details['error']}{COLOR_RESET}")

        self.results.append({
            "test_name": test_name,
            "status": status_str,
            "timestamp": datetime.utcnow().isoformat(),
            "details": details,
        })

    # =========================================================================
    # CENÁRIO 1: ROTEAMENTO EXATO PARA 2º VEÍCULO (MOTO vs CARRO)
    # =========================================================================
    async def test_scenario_1_vehicle_routing(self):
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}--- [CENÁRIO 1] Roteamento Exato de Entidades (Moto vs Carro) ---{COLOR_RESET}")
        user_id = "5521990010001"
        await self._reset_user(user_id)

        try:
            # 1. Cadastra Carro e Moto na garagem do usuário
            async with async_session_factory() as session:
                u_repo = UserRepository(session)
                await u_repo.get_or_create(user_id=user_id)
                v_repo = VehicleRepository(session)

                car = await v_repo.add_vehicle(Vehicle(
                    user_id=user_id,
                    make="Honda",
                    model="Civic EXL",
                    year=2020,
                    plate="CIV1C20",
                    vehicle_type=VehicleType.CARRO,
                    current_km=50000,
                ))
                moto = await v_repo.add_vehicle(Vehicle(
                    user_id=user_id,
                    make="Honda",
                    model="CG 160 Titan",
                    year=2022,
                    plate="CG160TT",
                    vehicle_type=VehicleType.MOTO,
                    current_km=15000,
                ))
                car_id = car.id
                moto_id = moto.id

            print(f"  Garagem: Carro ID {car_id} (Civic) | Moto ID {moto_id} (CG 160)")

            # 2. Usuário relata manutenção citando explicitamente a motoca
            msg = "troquei a relação e a lona de freio da motoca por 180 reais"
            print(f"  Condutor: \"{msg}\"")
            st, data, ms = await self._send_webhook(user_id, msg)
            assert st == 200, f"HTTP {st}"
            reply = data.get("reply", "")
            print(f"  Resposta AutoCare ({ms:.0f}ms): {reply[:100]}...")

            await asyncio.sleep(4.5)

            # 3. Usuário confirma
            print(f"  Condutor: \"Sim\"")
            st2, data2, ms2 = await self._send_webhook(user_id, "Sim")
            assert st2 == 200, f"HTTP {st2}"
            print(f"  Resposta Confirmação: {data2.get('reply', '')[:80]}...")

            # 4. Auditoria SQL direta no banco
            async with async_session_factory() as session:
                m_repo = MaintenanceRepository(session)
                records = await m_repo.get_by_user_id(user_id)
                assert len(records) >= 1, "Nenhum registro de manutenção inserido no banco!"
                rec = records[0]
                assert rec.vehicle_id == moto_id, f"Falha de Roteamento! Gravado no veículo ID {rec.vehicle_id}, esperado Moto ID {moto_id}!"
                assert rec.vehicle_id != car_id, f"Gravou no Civic (Carro ID {car_id}) em vez da Moto!"
                assert rec.total_cost == 180.0, f"Custo incorreto: {rec.total_cost}"

            self._record_result(
                "Roteamento Exato para Moto vs Carro validado via SQL",
                True,
                {"moto_id": moto_id, "car_id": car_id, "record_vehicle_id": rec.vehicle_id, "total_cost": rec.total_cost}
            )

        except Exception as e:
            self._record_result("Roteamento Exato para Moto vs Carro", False, {"error": str(e)})

    # =========================================================================
    # CENÁRIO 2: PERSISTÊNCIA REAL DE LEMBRETES, MULTAS E ODÔMETRO
    # =========================================================================
    async def test_scenario_2_real_persistence(self):
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}--- [CENÁRIO 2] Persistência Real de Lembretes, Multas e Odômetro ---{COLOR_RESET}")
        user_id = "5521990020002"
        await self._reset_user(user_id)

        try:
            # 1. Setup com 1 carro Corolla
            async with async_session_factory() as session:
                u_repo = UserRepository(session)
                await u_repo.get_or_create(user_id=user_id)
                v_repo = VehicleRepository(session)
                car = await v_repo.add_vehicle(Vehicle(
                    user_id=user_id,
                    make="Toyota",
                    model="Corolla Altis",
                    year=2021,
                    plate="COR2121",
                    vehicle_type=VehicleType.CARRO,
                    current_km=40000,
                ))
                car_id = car.id

            await asyncio.sleep(4.5)

            # 2. Registra Lembrete
            msg_rem = "Me lembra de calibrar os pneus dia 25/11"
            print(f"  Condutor: \"{msg_rem}\"")
            st, data, ms = await self._send_webhook(user_id, msg_rem)
            assert st == 200
            await asyncio.sleep(4.5)

            st, data, ms = await self._send_webhook(user_id, "Sim")
            assert st == 200

            # 3. Registra Multa de Trânsito
            await asyncio.sleep(4.5)
            msg_fine = "Tomei multa 745-5-0 de 130 reais por velocidade na marginal"
            print(f"  Condutor: \"{msg_fine}\"")
            st, data, ms = await self._send_webhook(user_id, msg_fine)
            assert st == 200
            await asyncio.sleep(4.5)

            st, data, ms = await self._send_webhook(user_id, "Sim")
            assert st == 200

            # 4. Atualiza Odômetro
            await asyncio.sleep(4.5)
            msg_odo = "Meu carro está agora com 52.000 km"
            print(f"  Condutor: \"{msg_odo}\"")
            st, data, ms = await self._send_webhook(user_id, msg_odo)
            assert st == 200
            await asyncio.sleep(4.5)
            st, data, ms = await self._send_webhook(user_id, "Sim")
            assert st == 200

            # 5. Auditoria Direta SQL
            async with async_session_factory() as session:
                r_repo = ReminderRepository(session)
                f_repo = TrafficFineRepository(session)
                v_repo = VehicleRepository(session)

                reminders = await r_repo.get_by_user_id(user_id)
                fines = await f_repo.get_by_user_id(user_id)
                v_updated = await v_repo.get_by_id(car_id)

                assert len(reminders) >= 1, "Lembrete não foi persistido na tabela reminders!"
                assert len(fines) >= 1, "Multa não foi persistida na tabela traffic_fines!"
                assert v_updated.current_km == 52000, f"Odômetro do veículo não atualizado: {v_updated.current_km} (esperado 52000)"

            self._record_result(
                "Persistência SQL de Lembretes, Multas e Odômetro",
                True,
                {
                    "reminders_count": len(reminders),
                    "reminder_desc": reminders[0].description,
                    "fines_count": len(fines),
                    "fine_amount": fines[0].amount,
                    "updated_km": v_updated.current_km,
                }
            )

        except Exception as e:
            self._record_result("Persistência SQL de Lembretes, Multas e Odômetro", False, {"error": str(e)})

    # =========================================================================
    # CENÁRIO 3: TESTE CEGO DE CONSULTA DO BANCO (MEMORY WIPED)
    # =========================================================================
    async def test_scenario_3_blind_db_query(self):
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}--- [CENÁRIO 3] Teste Cego de Consulta do Banco (Memória Zep Limpa) ---{COLOR_RESET}")
        user_id = "5521990030003"
        await self._reset_user(user_id)

        try:
            # 1. Popula dados ricos diretamente no banco
            async with async_session_factory() as session:
                u_repo = UserRepository(session)
                await u_repo.get_or_create(user_id=user_id)
                v_repo = VehicleRepository(session)
                m_repo = MaintenanceRepository(session)
                f_repo = TrafficFineRepository(session)
                r_repo = ReminderRepository(session)

                golf = await v_repo.add_vehicle(Vehicle(
                    user_id=user_id,
                    make="Volkswagen",
                    model="Golf TSI",
                    year=2019,
                    plate="GLF1919",
                    vehicle_type=VehicleType.CARRO,
                    current_km=60000,
                ))

                await m_repo.add_record(MaintenanceRecord(
                    vehicle_id=golf.id,
                    user_id=user_id,
                    components=["óleo 5w40", "filtro de óleo"],
                    description="Troca de óleo sintético",
                    km=55000,
                    date=date(2026, 8, 10),
                    total_cost=350.0,
                    workshop_name="Oficina Premium",
                ))
                await m_repo.add_record(MaintenanceRecord(
                    vehicle_id=golf.id,
                    user_id=user_id,
                    components=["pastilhas de freio dianteiras"],
                    description="Troca de pastilhas Bosch",
                    km=58000,
                    date=date(2026, 9, 20),
                    total_cost=450.0,
                    workshop_name="Freios & Cia",
                ))

                await f_repo.add_fine(TrafficFine(
                    vehicle_id=golf.id,
                    description="Excesso de velocidade até 20%",
                    infraction_code="745-5-0",
                    points=4,
                    amount=130.16,
                ))

                await r_repo.add_reminder(Reminder(
                    user_id=user_id,
                    vehicle_id=golf.id,
                    description="Revisão dos 60.000 km",
                    due_km=60000,
                ))

            # 2. Limpa radicalmente todo o contexto em memória
            conversation_service.clear_context(user_id)
            print("  🧹 Memória recente de turnos completamente zerada (clear_context)")

            await asyncio.sleep(4.5)

            # 3. Consulta financeira: Deve somar R$ 800 do banco
            q_fin = "Quanto já gastei com manutenções no meu Golf?"
            print(f"  Condutor: \"{q_fin}\"")
            st, data, ms = await self._send_webhook(user_id, q_fin)
            reply_fin = data.get("reply", "")
            print(f"  Resposta AutoCare: {reply_fin}")
            assert any(x in reply_fin for x in ["800", "800,00", "800.00"]), f"Não encontrou total R$ 800 na resposta: {reply_fin}"

            await asyncio.sleep(4.5)

            # 4. Consulta de Oficina: Deve citar 'Freios & Cia' ou 'Oficina Premium'
            q_shop = "Qual o nome da oficina que fez o último serviço no carro?"
            print(f"  Condutor: \"{q_shop}\"")
            st, data, ms = await self._send_webhook(user_id, q_shop)
            reply_shop = data.get("reply", "")
            print(f"  Resposta AutoCare: {reply_shop}")
            assert any(x in reply_shop.lower() for x in ["freios & cia", "freios e cia", "oficina premium"]), f"Oficina não encontrada na resposta: {reply_shop}"

            await asyncio.sleep(4.5)

            # 5. Consulta de Multas: Deve citar 4 pontos
            q_fines = "Quantos pontos de multa eu tenho registrados?"
            print(f"  Condutor: \"{q_fines}\"")
            st, data, ms = await self._send_webhook(user_id, q_fines)
            reply_fines = data.get("reply", "")
            print(f"  Resposta AutoCare: {reply_fines}")
            assert "4" in reply_fines, f"Pontos de multa não encontrados na resposta: {reply_fines}"

            self._record_result(
                "Teste Cego de Consulta ao Banco (Gasto R$ 800, Oficina e Pontos de Multa)",
                True,
                {"reply_fin": reply_fin, "reply_shop": reply_shop, "reply_fines": reply_fines}
            )

        except Exception as e:
            self._record_result("Teste Cego de Consulta ao Banco", False, {"error": str(e)})

    # =========================================================================
    # CENÁRIO 4: IDEMPOTÊNCIA E DOUBLE-CLICK CONCORRENTE (RACE CONDITION)
    # =========================================================================
    async def test_scenario_4_concurrency_double_click(self):
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}--- [CENÁRIO 4] Idempotência & Double-Click Concorrente (Race Condition) ---{COLOR_RESET}")
        user_id = "5521990040004"
        await self._reset_user(user_id)

        try:
            # 1. Setup com 1 Compass
            async with async_session_factory() as session:
                u_repo = UserRepository(session)
                await u_repo.get_or_create(user_id=user_id)
                v_repo = VehicleRepository(session)
                jeep = await v_repo.add_vehicle(Vehicle(
                    user_id=user_id,
                    make="Jeep",
                    model="Compass Limited",
                    year=2022,
                    plate="JEP4444",
                    current_km=30000,
                ))

            await asyncio.sleep(4.5)

            # 2. Deixa rascunho de manutenção pendente
            msg_serv = "Troquei amortecedores dianteiros por 1200 reais na oficina Mopar"
            print(f"  Condutor: \"{msg_serv}\"")
            st, data, ms = await self._send_webhook(user_id, msg_serv)
            assert st == 200
            assert data.get("state") == "awaiting_maintenance_confirm"

            # 3. Dispara simultaneamente duas requisições com 'Sim' (Double Click no WhatsApp)
            print("  ⚡ Disparando duplo-clique simultâneo ('Sim') com asyncio.gather...")
            req1 = self._send_webhook(user_id, "Sim")
            req2 = self._send_webhook(user_id, "Sim")

            results = await asyncio.gather(req1, req2)
            print(f"  Respostas recebidas: HTTP {results[0][0]} e HTTP {results[1][0]}")

            # 4. Auditoria SQL: Garante que APENAS 1 registro foi gravado
            async with async_session_factory() as session:
                m_repo = MaintenanceRepository(session)
                records = await m_repo.get_by_user_id(user_id)
                print(f"  Total de manutenções no banco: {len(records)} (esperado: exatamente 1)")
                assert len(records) == 1, f"Falha de idempotência! Gravou {len(records)} registros em vez de 1!"

            self._record_result(
                "Idempotência e Proteção contra Double-Click Concorrente",
                True,
                {"maintenance_records_count": len(records), "record_id": records[0].id}
            )

        except Exception as e:
            self._record_result("Idempotência e Proteção contra Double-Click Concorrente", False, {"error": str(e)})

    # =========================================================================
    # CENÁRIO 5: BYPASS DE RESET BLOQUEADO
    # =========================================================================
    async def test_scenario_5_reset_bypass_blocked(self):
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}--- [CENÁRIO 5] Bloqueio de Reset Acidental sem Confirmação ---{COLOR_RESET}")
        user_id = "5521990050005"
        await self._reset_user(user_id)

        try:
            # 1. Cadastra 2 veículos no banco
            async with async_session_factory() as session:
                u_repo = UserRepository(session)
                await u_repo.get_or_create(user_id=user_id)
                v_repo = VehicleRepository(session)
                await v_repo.add_vehicle(Vehicle(user_id=user_id, make="Chevrolet", model="Onix", year=2020, plate="ONX2020"))
                await v_repo.add_vehicle(Vehicle(user_id=user_id, make="Ford", model="Ka", year=2019, plate="FOR2019"))

            await asyncio.sleep(4.5)

            # 2. Usuário envia frase em linguagem natural: 'zerar garagem'
            msg_reset = "zerar garagem"
            print(f"  Condutor: \"{msg_reset}\"")
            st, data, ms = await self._send_webhook(user_id, msg_reset)
            assert st == 200
            action_type = data.get("action_type")
            state = data.get("state")
            reply = data.get("reply", "")
            print(f"  Resposta AutoCare ({ms:.0f}ms): {reply}")

            # Valida que o fast-path de reset instantâneo NÃO foi acionado
            assert action_type != "system_reset", "Falha de segurança! 'zerar garagem' acionou system_reset sem confirmação!"
            assert state == "awaiting_garage_reset_confirm", f"Estado esperado awaiting_garage_reset_confirm, obtido: {state}"

            # 3. Verifica que os 2 veículos permanecem intactos no banco
            async with async_session_factory() as session:
                v_repo = VehicleRepository(session)
                veics = await v_repo.get_by_user_id(user_id)
                assert len(veics) == 2, f"Veículos apagados indevidamente antes da confirmação! Restaram: {len(veics)}"

            # 4. Usuário responde 'Não' para cancelar
            print(f"  Condutor: \"Não\"")
            st2, data2, ms2 = await self._send_webhook(user_id, "Não")
            assert st2 == 200
            print(f"  Resposta Cancelamento: {data2.get('reply', '')}")

            # 5. Verifica novamente que os 2 veículos continuam intactos no banco
            async with async_session_factory() as session:
                v_repo = VehicleRepository(session)
                veics_after = await v_repo.get_by_user_id(user_id)
                assert len(veics_after) == 2, "Veículos foram apagados após 'Não'!"

            self._record_result(
                "Bypass de Reset Bloqueado (Exige Confirmação e 'Não' Preserva Garagem)",
                True,
                {"action_type": action_type, "state": state, "vehicles_preserved": len(veics_after)}
            )

        except Exception as e:
            self._record_result("Bypass de Reset Bloqueado", False, {"error": str(e)})

    # =========================================================================
    # CENÁRIO 6: AUDITORIA DE LOGS SEM VAZAMENTO DE CREDENCIAIS
    # =========================================================================
    async def test_scenario_6_no_credential_leak_in_logs(self):
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}--- [CENÁRIO 6] Auditoria de Logs: Zero Vazamento de Credenciais ---{COLOR_RESET}")
        user_id = "5521990060006"
        await self._reset_user(user_id)

        try:
            # 1. Configura captura de stream de log de httpx e root
            log_capture = io.StringIO()
            handler = logging.StreamHandler(log_capture)
            handler.setLevel(logging.DEBUG)

            root_logger = logging.getLogger()
            httpx_logger = logging.getLogger("httpx")
            root_logger.addHandler(handler)
            httpx_logger.addHandler(handler)
            httpx_logger.setLevel(logging.INFO)

            # 2. Dispara mensagem que obriga chamada à API do Gemini
            await asyncio.sleep(4.5)
            msg = "Qual a viscosidade de óleo recomendada para um Honda Civic 2.0 2020?"
            print(f"  Condutor: \"{msg}\"")
            st, data, ms = await self._send_webhook(user_id, msg)
            assert st == 200

            # Remove handler de captura
            root_logger.removeHandler(handler)
            httpx_logger.removeHandler(handler)

            captured_text = log_capture.getvalue()

            # 3. Validações estritas de segurança de logs
            api_key = settings.GEMINI_API_KEY
            if api_key and len(api_key) > 5:
                assert api_key not in captured_text, "CRÍTICO: GEMINI_API_KEY vazou nos logs de httpx!"
            assert "key=" not in captured_text.lower() or "apikey" not in captured_text.lower(), "Query param de chave encontrado na URL dos logs!"

            print(f"  Stream de log auditado: {len(captured_text)} caracteres verificados sem ocorrência da chave.")
            self._record_result(
                "Auditoria de Logs: Zero Vazamento da API_KEY em URLs ou Headers",
                True,
                {"log_bytes_analyzed": len(captured_text), "key_found": False}
            )

        except Exception as e:
            self._record_result("Auditoria de Logs: Zero Vazamento de Credenciais", False, {"error": str(e)})

    # =========================================================================
    # CENÁRIO 7: PARSER DE PAYLOAD REAL DE GATEWAYS (Evolution API e Z-API)
    # =========================================================================
    async def test_scenario_7_gateway_payload_parsers(self):
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}--- [CENÁRIO 7] Parser de Payloads Reais de Gateways (Evolution e Z-API) ---{COLOR_RESET}")

        try:
            # 1. Evolution API - Mensagem de Grupo (@g.us) -> Deve ignorar com HTTP 200
            payload_evo_group = {
                "event": "messages.upsert",
                "instance": "autocare",
                "data": {
                    "key": {
                        "remoteJid": "120363024567891234@g.us",
                        "fromMe": False,
                        "id": "BAE5D8F0B876",
                    },
                    "message": {"conversation": "Galera, alguém sabe um mecânico bom?"}
                }
            }
            st1, d1, ms1 = await self._send_raw_webhook(payload_evo_group)
            assert st1 == 200, f"HTTP {st1}"
            assert d1.get("status") == "ignored", f"Grupo Evolution não ignorado: {d1}"
            print("  ✅ Evolution API (@g.us group): HTTP 200 status='ignored'")

            # 2. Evolution API - Mensagem própria do Bot (fromMe: True) -> Deve ignorar
            payload_evo_from_me = {
                "event": "messages.upsert",
                "instance": "autocare",
                "data": {
                    "key": {
                        "remoteJid": "5521988887777@s.whatsapp.net",
                        "fromMe": True,
                        "id": "BAE5D8F0B877",
                    },
                    "message": {"conversation": "Olá, sou o assistente AutoCare"}
                }
            }
            st2, d2, ms2 = await self._send_raw_webhook(payload_evo_from_me)
            assert st2 == 200
            assert d2.get("status") == "ignored", f"fromMe=True não ignorado: {d2}"
            print("  ✅ Evolution API (fromMe: True): HTTP 200 status='ignored'")

            # 3. Z-API - Mensagem de Grupo (isGroup: True) -> Deve ignorar
            payload_zapi_group = {
                "isGroup": True,
                "phone": "5521999991111",
                "chatId": "120363024567891234@g.us",
                "text": {"message": "Alguém vende peças?"}
            }
            st3, d3, ms3 = await self._send_raw_webhook(payload_zapi_group)
            assert st3 == 200
            assert d3.get("status") == "ignored", f"Z-API group não ignorado: {d3}"
            print("  ✅ Z-API (isGroup: True): HTTP 200 status='ignored'")

            # 4. Z-API - Mensagem própria do Bot (fromMe: True) -> Deve ignorar
            payload_zapi_from_me = {
                "isGroup": False,
                "fromMe": True,
                "phone": "5521999991111",
                "text": {"message": "Mensagem enviada pelo bot"}
            }
            st4, d4, ms4 = await self._send_raw_webhook(payload_zapi_from_me)
            assert st4 == 200
            assert d4.get("status") == "ignored", f"Z-API fromMe não ignorado: {d4}"
            print("  ✅ Z-API (fromMe: True): HTTP 200 status='ignored'")

            # 5. Z-API - Mensagem 1-on-1 legítima rápida ("valeu") -> Deve processar com HTTP 200
            payload_zapi_valid = {
                "isGroup": False,
                "fromMe": False,
                "phone": "5521999998888",
                "text": {"message": "valeu obrigado!"}
            }
            st5, d5, ms5 = await self._send_raw_webhook(payload_zapi_valid)
            assert st5 == 200
            assert d5.get("status") == "success", f"Z-API válido falhou: {d5}"
            print("  ✅ Z-API (1-on-1 legítima): HTTP 200 status='success'")

            self._record_result(
                "Parser de Payloads Reais de Gateways (Evolution e Z-API)",
                True,
                {"evolution_group": d1, "evolution_from_me": d2, "zapi_group": d3, "zapi_from_me": d4, "zapi_valid": d5}
            )

        except Exception as e:
            self._record_result("Parser de Payloads Reais de Gateways", False, {"error": str(e)})

    # =========================================================================
    # ORQUESTRADOR GERAL
    # =========================================================================
    async def run_all(self):
        t0 = time.time()
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}{'='*75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}{COLOR_CYAN}🔬 AUTOCARE V2 - SUÍTE DE VALIDAÇÃO PROFUNDA E AUDITORIA SQL{COLOR_RESET}")
        print(f"{COLOR_BOLD}{COLOR_CYAN}{'='*75}{COLOR_RESET}")

        await self.test_scenario_1_vehicle_routing()
        await self.test_scenario_2_real_persistence()
        await self.test_scenario_3_blind_db_query()
        await self.test_scenario_4_concurrency_double_click()
        await self.test_scenario_5_reset_bypass_blocked()
        await self.test_scenario_6_no_credential_leak_in_logs()
        await self.test_scenario_7_gateway_payload_parsers()

        total_time = time.time() - t0
        total_tests = self.passed_count + self.failed_count

        report_data = {
            "suite": "deep_validation_suite",
            "executed_at": datetime.utcnow().isoformat(),
            "total_time_seconds": round(total_time, 2),
            "summary": {
                "total": total_tests,
                "passed": self.passed_count,
                "failed": self.failed_count,
                "success_rate": f"{(self.passed_count / total_tests * 100):.1f}%" if total_tests else "0%",
            },
            "results": self.results,
        }

        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)

        print(f"\n{COLOR_BOLD}{COLOR_CYAN}{'='*75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}📊 RELATÓRIO CONSOLIDADO DA AUDITORIA PROFUNDA:{COLOR_RESET}")
        print(f"• Total de Cenários: {total_tests}")
        print(f"• Aprovados: {COLOR_GREEN}{self.passed_count}{COLOR_RESET}")
        print(f"• Reprovados: {COLOR_RED if self.failed_count > 0 else COLOR_GREEN}{self.failed_count}{COLOR_RESET}")
        print(f"• Tempo Total: {total_time:.2f}s")
        print(f"• Relatório JSON gravado em: {self.report_file}")
        print(f"{COLOR_BOLD}{COLOR_CYAN}{'='*75}{COLOR_RESET}\n")

        if self.failed_count > 0:
            print(f"{COLOR_YELLOW}⚠️  Atenção: Alguns testes falharam. Verifique os detalhes em {self.report_file}.{COLOR_RESET}")
        else:
            print(f"{COLOR_GREEN}🎉 100% DOS CENÁRIOS PROFUNDOS VALIDADOS COM SUCESSO!{COLOR_RESET}")


if __name__ == "__main__":
    tester = DeepValidationTester()
    asyncio.run(tester.run_all())
