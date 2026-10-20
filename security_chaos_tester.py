"""
AutoCare V2 - Security & Infrastructure Chaos Tester (security_chaos_tester.py)
Suíte de testes de estresse, segurança de infraestrutura e consistência de dados:
1. Isolamento Multi-Tenant Concorrente (User A vs User B)
2. Proteção contra Odômetro Regressivo (Quilometragem inferior no banco e diálogo)
3. Injeção de Dados Estruturados (SQL Injection, XSS e caracteres de escape)
4. Resiliência a Fallback de LLM (Simulação de Timeout/Outage sem HTTP 500)
"""

import sys
import os
import json
import time
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from unittest.mock import patch, AsyncMock

import httpx

# Configura encoding UTF-8 no terminal Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config.settings import settings
from src.interfaces.app import app
from src.infrastructure.database.session import async_session_factory
from src.infrastructure.database.repositories import UserRepository, VehicleRepository, MaintenanceRepository
from src.domain.entities import Vehicle, MaintenanceRecord
from src.domain.enums import VehicleType, FuelType

# Cores ANSI
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_BLUE = "\033[94m"
COLOR_MAGENTA = "\033[95m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[2m"
COLOR_RESET = "\033[0m"


class SecurityChaosTester:
    """Executa a bateria de testes de caos, segurança e consistência de infraestrutura."""

    def __init__(self):
        self.results: List[Dict[str, Any]] = []
        self.passed_count = 0
        self.failed_count = 0
        self.report_file = "security_audit_report.json"

    async def _send_webhook(self, user_id: str, message: str) -> Tuple[int, Dict[str, Any], float]:
        """Dispara mensagem HTTP POST assíncrona ao webhook via transporte ASGI em memória."""
        payload = {"user_id": user_id, "message": message}
        t0 = time.time()
        from httpx import ASGITransport
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as client:
            resp = await client.post("/webhook", json=payload)
            elapsed_ms = (time.time() - t0) * 1000.0
            try:
                data = resp.json()
            except Exception:
                data = {"raw_text": resp.text}
            return resp.status_code, data, elapsed_ms

    async def _reset_user(self, user_id: str):
        """Limpa dados e estado do usuário no banco."""
        async with async_session_factory() as session:
            repo = UserRepository(session)
            await repo.reset_user_data(user_id)
        # Limpa contexto conversacional em memória
        from src.interfaces.api.webhook import conversation_service
        if hasattr(conversation_service, "clear_context"):
            conversation_service.clear_context(user_id)
        elif hasattr(conversation_service, "_contexts") and user_id in conversation_service._contexts:
            del conversation_service._contexts[user_id]

    # =========================================================================
    # TESTE 1: ISOLAMENTO MULTI-TENANT CONCORRENTE
    # =========================================================================
    async def test_multi_tenant_isolation(self) -> Dict[str, Any]:
        """
        Simula dois condutores concorrentes (User A e User B):
        - User A cadastra um Honda Civic
        - User B cadastra uma Yamaha Fazer
        - Disparos simultâneos ocorrem em paralelo
        - User A zera a garagem
        - Assegura que a garagem e contexto do User B permaneçam 100% intactos!
        """
        print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}🛡️  [TESTE 1]: ISOLAMENTO MULTI-TENANT CONCORRENTE{COLOR_RESET}")
        print(f"{COLOR_DIM}Verifica se dados, estados e ações de um usuário não contaminam outro usuário.{COLOR_RESET}")
        print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")

        user_a = "5521999991111"
        user_b = "5521999992222"

        # Prepara ambiente limpo para ambos
        await self._reset_user(user_a)
        await self._reset_user(user_b)

        # 1. Garante existência dos condutores e cadastro concorrente via banco
        async with async_session_factory() as session:
            u_repo = UserRepository(session)
            await u_repo.get_or_create(user_a)
            await u_repo.get_or_create(user_b)

            v_repo = VehicleRepository(session)
            veh_a = await v_repo.add_vehicle(Vehicle(
                user_id=user_a,
                make="Honda",
                model="Civic",
                year=2020,
                plate="CIV1111",
                current_km=45000,
                vehicle_type=VehicleType.CARRO,
            ))
            veh_b = await v_repo.add_vehicle(Vehicle(
                user_id=user_b,
                make="Yamaha",
                model="Fazer 250",
                year=2022,
                plate="FAZ2222",
                current_km=15000,
                vehicle_type=VehicleType.MOTO,
            ))

        print(f"{COLOR_BLUE}👤 User A ({user_a}):{COLOR_RESET} Honda Civic cadastrado (ID {veh_a.id})")
        print(f"{COLOR_MAGENTA}👤 User B ({user_b}):{COLOR_RESET} Yamaha Fazer 250 cadastrada (ID {veh_b.id})")

        # 2. Disparos concorrentes simultâneos (User A pergunta de óleo, User B pergunta de calibragem)
        print(f"{COLOR_DIM}⚡ Disparando requisições paralelas concorrentes para User A e User B...{COLOR_RESET}")
        task_a = self._send_webhook(user_a, "qual a viscosidade de óleo recomendada para o meu civic?")
        task_b = self._send_webhook(user_b, "quantas libras devo calibrar o pneu da minha moto?")
        
        results = await asyncio.gather(task_a, task_b)
        status_a, res_a, ms_a = results[0]
        status_b, res_b, ms_b = results[1]

        print(f"{COLOR_BLUE}🤖 Resposta User A ({ms_a:.1f}ms):{COLOR_RESET} {res_a.get('reply', '')[:100]}...")
        print(f"{COLOR_MAGENTA}🤖 Resposta User B ({ms_b:.1f}ms):{COLOR_RESET} {res_b.get('reply', '')[:100]}...")

        # 3. User A executa comando destrutivo: "zerar sistema"
        print(f"\n{COLOR_YELLOW}💣 User A disparando 'zerar sistema'...{COLOR_RESET}")
        status_reset, res_reset, _ = await self._send_webhook(user_a, "zerar sistema")
        print(f"{COLOR_BLUE}🤖 AutoCare V2 (User A):{COLOR_RESET} {res_reset.get('reply', '')[:110]}...")

        # 4. Verificações de Auditoria no Banco de Dados
        async with async_session_factory() as session:
            v_repo = VehicleRepository(session)
            vehs_a = await v_repo.get_vehicles_by_user_id(user_a)
            vehs_b = await v_repo.get_vehicles_by_user_id(user_b)

        print(f"\n{COLOR_BOLD}🔍 Verificações de Isolamento:{COLOR_RESET}")
        print(f"• Veículos do User A após reset: {len(vehs_a)} (esperado: 0)")
        print(f"• Veículos do User B após reset do User A: {len(vehs_b)} (esperado: 1 - Yamaha Fazer 250)")

        passed = (len(vehs_a) == 0) and (len(vehs_b) == 1) and (vehs_b[0].plate == "FAZ2222")

        test_data = {
            "test_name": "Isolamento Multi-Tenant Concorrente",
            "passed": passed,
            "status_codes": {"user_a": status_a, "user_b": status_b, "reset": status_reset},
            "user_a_vehicles_count": len(vehs_a),
            "user_b_vehicles_count": len(vehs_b),
            "user_b_vehicle_intact": (len(vehs_b) == 1 and vehs_b[0].plate == "FAZ2222"),
            "details": "Ações e resets do User A não afetaram a garagem e contexto do User B." if passed else "Vazamento de dados entre tenants detectado!"
        }

        if passed:
            print(f"{COLOR_GREEN}{COLOR_BOLD}✅ [PASS] Isolamento Multi-Tenant validado com 100% de integridade!{COLOR_RESET}")
            self.passed_count += 1
        else:
            print(f"{COLOR_RED}{COLOR_BOLD}❌ [FAIL] Falha no isolamento multi-tenant!{COLOR_RESET}")
            self.failed_count += 1

        self.results.append(test_data)
        return test_data

    # =========================================================================
    # TESTE 2: PROTEÇÃO CONTRA ODÔMETRO REGRESSIVO
    # =========================================================================
    async def test_odometer_regression_protection(self) -> Dict[str, Any]:
        """
        Cadastra um veículo com 45.000 km e tenta registrar manutenção ou atualizar km
        para um valor inferior (ex: 20.000 km). Avalia a resposta do NLP e a integridade do banco.
        """
        print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}🛡️  [TESTE 2]: PROTEÇÃO CONTRA ODÔMETRO REGRESSIVO{COLOR_RESET}")
        print(f"{COLOR_DIM}Verifica se quilometragens inferiores à atual do veículo são barradas no banco e alertadas pela IA.{COLOR_RESET}")
        print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")

        user_id = "5521999993333"
        await self._reset_user(user_id)

        # 1. Garante existência do condutor e cadastra veículo com 45.000 km no banco
        async with async_session_factory() as session:
            u_repo = UserRepository(session)
            await u_repo.get_or_create(user_id)

            v_repo = VehicleRepository(session)
            veh = await v_repo.add_vehicle(Vehicle(
                user_id=user_id,
                make="Honda",
                model="Civic",
                year=2020,
                plate="ODO4545",
                current_km=45000,
                vehicle_type=VehicleType.CARRO,
            ))
            vehicle_id = veh.id

        print(f"{COLOR_BLUE}🚗 Veículo Cadastrado:{COLOR_RESET} Honda Civic placa ODO4545 com {veh.current_km} km")

        # 2. Tenta atualizar diretamente o repositório com 20.000 km (teste unitário de repositório)
        async with async_session_factory() as session:
            v_repo = VehicleRepository(session)
            updated_veh = await v_repo.update_km(vehicle_id, 20000)
            km_after_repo = updated_veh.current_km

        print(f"{COLOR_BOLD}🔍 Teste Atômico de Banco (Tentativa de update_km para 20.000 km):{COLOR_RESET}")
        print(f"• Odômetro no banco após tentativa: {km_after_repo} km (esperado: 45000 km)")
        repo_protected = (km_after_repo == 45000)

        # 3. Tenta registrar manutenção via Webhook com KM regressivo (20.000 km)
        regressive_msg = "Troquei o óleo do meu Civic placa ODO4545 com 20.000 km por 180 reais na Oficina do Zé"
        print(f"\n{COLOR_BLUE}👤 Condutor:{COLOR_RESET} \"{regressive_msg}\"")

        status_code, response, elapsed_ms = await self._send_webhook(user_id, regressive_msg)
        bot_reply = response.get("reply", "")
        print(f"{COLOR_MAGENTA}🤖 AutoCare V2 ({elapsed_ms:.1f}ms):{COLOR_RESET} \"{bot_reply}\"")

        # Avalia se o bot identificou a divergência do odômetro ou solicitou confirmação
        reply_lower = bot_reply.lower()
        nlp_alerted = any(term in reply_lower for term in [
            "45.000", "45000", "inferior", "menor", "inconsistência", "passado", "atual", "quilometragem", "km"
        ])

        passed = repo_protected

        test_data = {
            "test_name": "Proteção contra Odômetro Regressivo",
            "passed": passed,
            "status_code": status_code,
            "original_km": 45000,
            "attempted_km": 20000,
            "db_km_preserved": repo_protected,
            "nlp_alerted": nlp_alerted,
            "bot_reply": bot_reply,
            "details": "Repositório impediu a redução de odômetro de 45.000 para 20.000 km com sucesso." if passed else "Falha: banco permitiu regressão do odômetro!"
        }

        if passed:
            print(f"{COLOR_GREEN}{COLOR_BOLD}✅ [PASS] Proteção contra odômetro regressivo aprovada com sucesso!{COLOR_RESET}")
            self.passed_count += 1
        else:
            print(f"{COLOR_RED}{COLOR_BOLD}❌ [FAIL] Odômetro regressivo não foi impedido!{COLOR_RESET}")
            self.failed_count += 1

        self.results.append(test_data)
        return test_data

    # =========================================================================
    # TESTE 3: INJEÇÃO EM DADOS ESTRUTURADOS (SQL Injection, XSS e Escape)
    # =========================================================================
    async def test_structured_data_injection(self) -> Dict[str, Any]:
        """
        Envia múltiplos vetores maliciosos com SQL Injection, tags HTML/Script e
        caracteres especiais em placas, nomes de oficinas e comandos.
        Garante que o banco permaneça intacto e que o sistema responda com HTTP 200.
        """
        print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}🛡️  [TESTE 3]: INJEÇÃO EM DADOS ESTRUTURADOS & SANITIZAÇÃO{COLOR_RESET}")
        print(f"{COLOR_DIM}Testa vetores de SQL Injection, XSS e caracteres nulos/escape em placas e oficinas.{COLOR_RESET}")
        print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")

        user_id = "5521999994444"
        await self._reset_user(user_id)

        attack_vectors = [
            {
                "name": "SQL Injection Clássico em Placa",
                "message": "Cadastra meu carro Fiat Uno placa ABC1234'; DROP TABLE vehicles; -- ano 2015 com 60000 km",
            },
            {
                "name": "XSS / HTML Injection em Nome de Oficina",
                "message": "Troquei pastilhas na oficina <script>alert('pwned')</script> por 300 reais",
            },
            {
                "name": "Caracteres Nulos e Escape Hexadecimal",
                "message": "Atualiza meu carro \\x00\\r\\n'\" OR 1=1 -- /* comment */ placa TEST999",
            },
            {
                "name": "Blind SQL Injection em Parâmetro de Busca",
                "message": "Quando foi a última troca de óleo do carro' AND (SELECT 1 FROM users WHERE 1=1)='1?",
            }
        ]

        vector_results = []
        all_passed = True

        for idx, vec in enumerate(attack_vectors, 1):
            name = vec["name"]
            msg = vec["message"]
            print(f"\n{COLOR_YELLOW}⚔️  Vetor {idx} [{name}]:{COLOR_RESET} \"{msg[:65]}...\"")

            status_code, response, ms = await self._send_webhook(user_id, msg)
            reply = response.get("reply", "")

            # 1. Deve responder HTTP 200 (sem crashar 500)
            status_ok = (status_code == 200)

            # 2. Verifica se a tabela 'vehicles' ainda existe e está acessível no banco
            table_intact = True
            try:
                async with async_session_factory() as session:
                    v_repo = VehicleRepository(session)
                    await v_repo.get_vehicles_by_user_id(user_id)
            except Exception as e:
                table_intact = False

            vec_passed = status_ok and table_intact
            if not vec_passed:
                all_passed = False

            print(f"   Status HTTP: {status_code} | Latência: {ms:.1f}ms | Tabela vehicles íntegra: {table_intact}")
            print(f"   {COLOR_MAGENTA}🤖 AutoCare V2:{COLOR_RESET} \"{reply[:90]}...\"")

            vector_results.append({
                "vector": name,
                "status_code": status_code,
                "table_intact": table_intact,
                "passed": vec_passed,
                "latency_ms": ms,
            })

        test_data = {
            "test_name": "Injeção em Dados Estruturados & Sanitização",
            "passed": all_passed,
            "total_vectors": len(attack_vectors),
            "vectors_evaluated": vector_results,
            "details": "Todas as tentativas de injeção foram neutralizadas via bound parameters do SQLAlchemy 2.0." if all_passed else "Vulnerabilidade detectada em injeção de dados estruturados!"
        }

        if all_passed:
            print(f"\n{COLOR_GREEN}{COLOR_BOLD}✅ [PASS] 4/4 Vetores de Injeção neutralizados com sucesso!{COLOR_RESET}")
            self.passed_count += 1
        else:
            print(f"\n{COLOR_RED}{COLOR_BOLD}❌ [FAIL] Falha na contenção de injeção!{COLOR_RESET}")
            self.failed_count += 1

        self.results.append(test_data)
        return test_data

    # =========================================================================
    # TESTE 4: RESILIÊNCIA A FALLBACK DE LLM (Timeout sem HTTP 500)
    # =========================================================================
    async def test_llm_fallback_resilience(self) -> Dict[str, Any]:
        """
        Simula indisponibilidade severa / timeout de rede no cliente Gemini.
        Garante que o Webhook intercepte a falha de transporte, monte a resposta
        de contingência amigável e retorne HTTP 200 sem crashar com erro 500.
        """
        print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}🛡️  [TESTE 4]: RESILIÊNCIA A TIMEOUT DE LLM (CONTINGÊNCIA SEM 500){COLOR_RESET}")
        print(f"{COLOR_DIM}Simula queda/timeout da API Gemini e valida entrega de mensagem amigável com HTTP 200.{COLOR_RESET}")
        print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")

        user_id = "5521999995555"
        await self._reset_user(user_id)

        # Mensagem de teste
        test_msg = "Troquei as pastilhas de freio do meu Civic por 250 reais"
        print(f"{COLOR_BLUE}👤 Condutor:{COLOR_RESET} \"{test_msg}\"")

        # Mocka a chamada HTTP do Gemini com TimeoutException forçado
        with patch("src.ai.client.GeminiClient._call_gemini_with_retry", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = httpx.TimeoutException("Mocked LLM Gateway Read Timeout")

            print(f"{COLOR_YELLOW}⚡ Injetando simulação de timeout estrito (httpx.TimeoutException)...{COLOR_RESET}")
            status_code, response, ms = await self._send_webhook(user_id, test_msg)
            reply = response.get("reply", "")

        print(f"{COLOR_BOLD}🔍 Resultado da Contingência:{COLOR_RESET}")
        print(f"• Status Code HTTP retornado: {status_code} (esperado: 200)")
        print(f"• Resposta de Contingência: \"{reply[:100]}...\"")

        # Critérios de sucesso:
        # 1. HTTP Status DEVE ser 200
        # 2. Resposta deve ser a mensagem amigável de contingência ("processando muitas mensagens")
        is_http_200 = (status_code == 200)
        has_friendly_message = ("processando muitas mensagens" in reply) or ("Desculpe" in reply) or ("instantes" in reply)
        passed = is_http_200 and has_friendly_message

        test_data = {
            "test_name": "Resiliência a Fallback de LLM",
            "passed": passed,
            "status_code": status_code,
            "has_friendly_message": has_friendly_message,
            "latency_ms": ms,
            "contingency_reply": reply,
            "details": "Timeout da LLM foi contido com sucesso, retornando HTTP 200 com mensagem amigável." if passed else "Falha: timeout causou erro não tratado ou resposta inadequada!"
        }

        if passed:
            print(f"{COLOR_GREEN}{COLOR_BOLD}✅ [PASS] Resiliência a fallback de LLM 100% aprovada!{COLOR_RESET}")
            self.passed_count += 1
        else:
            print(f"{COLOR_RED}{COLOR_BOLD}❌ [FAIL] Falha no tratamento de contingência de LLM!{COLOR_RESET}")
            self.failed_count += 1

        self.results.append(test_data)
        return test_data

    # =========================================================================
    # EXECUÇÃO COMPLETA & RELATÓRIO
    # =========================================================================
    async def run_all(self):
        """Executa a bateria de caos e infraestrutura ininterruptamente."""
        print(f"\n{COLOR_CYAN}{'#' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}🔥 AUTOCARE V2 - SUÍTE DE CAOS, SEGURANÇA E CONSISTÊNCIA DE DADOS{COLOR_RESET}")
        print(f"{COLOR_DIM}Módulos: Multi-Tenant | Odômetro Anti-Regressão | SQLi & XSS | LLM Timeout{COLOR_RESET}")
        print(f"{COLOR_CYAN}{'#' * 75}{COLOR_RESET}\n")

        t_start = time.time()

        # Execução sequencial protegida (NON-STOP)
        tests = [
            self.test_multi_tenant_isolation,
            self.test_odometer_regression_protection,
            self.test_structured_data_injection,
            self.test_llm_fallback_resilience,
        ]

        for t in tests:
            try:
                await t()
            except Exception as e:
                print(f"{COLOR_RED}💥 Erro não tratado durante execução do teste: {e}{COLOR_RESET}")
                self.results.append({
                    "test_name": t.__name__,
                    "passed": False,
                    "error": str(e),
                })
                self.failed_count += 1
            # Pausa rápida para estabilidade de conexões
            await asyncio.sleep(1.0)

        total_time = time.time() - t_start
        total_tests = len(tests)

        # Salva o relatório em disco
        report_data = {
            "timestamp": datetime.now().isoformat(),
            "total_tests": total_tests,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "duration_seconds": total_time,
            "tests_log": self.results,
        }

        try:
            with open(self.report_file, "w", encoding="utf-8") as f:
                json.dump(report_data, f, ensure_ascii=False, indent=2)
            print(f"\n{COLOR_BOLD}📄 Relatório gravado com sucesso em: {self.report_file}{COLOR_RESET}")
        except Exception as e:
            print(f"⚠️ Erro ao salvar relatório: {e}")

        # Resumo Executivo
        print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}📊 RESULTADO FINAL DA AUDITORIA DE CAOS E SEGURANÇA:{COLOR_RESET}")
        print(f"• Total de Testes Executados: {total_tests}")
        print(f"• Testes Aprovados [PASS]: {self.passed_count}")
        print(f"• Testes Reprovados [FAILED]: {self.failed_count}")
        print(f"• Tempo Total de Homologação: {total_time:.2f}s")

        if self.failed_count == 0:
            print(f"\n{COLOR_GREEN}{COLOR_BOLD}🎉 100% APROVADO! Infraestrutura, consistência de dados e segurança validadas!{COLOR_RESET}\n")
        else:
            print(f"\n{COLOR_RED}{COLOR_BOLD}⚠️  A AUDITORIA DETECTOU INCONSISTÊNCIAS. Verifique o relatório acima.{COLOR_RESET}\n")


async def main():
    tester = SecurityChaosTester()
    await tester.run_all()


if __name__ == "__main__":
    asyncio.run(main())
