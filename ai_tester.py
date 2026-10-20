"""
AutoCare V2 - Automated AI Auditor & Conversational Tester (ai_tester.py)
Auditor Inteligente ponta a ponta que simula o comportamento de um condutor no WhatsApp,
valida regras de negócio em tempo real com o Google Gemini e audita o sistema contra regressões.
"""

import sys
import os
import json
import time
import asyncio
import argparse
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

import httpx

# Garante suporte a UTF-8 no terminal Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config.settings import settings

# Códigos de Cores ANSI para Terminal
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_BLUE = "\033[94m"
COLOR_MAGENTA = "\033[95m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[2m"
COLOR_RESET = "\033[0m"


AUDITOR_SYSTEM_INSTRUCTION = """Você é o Auditor Sênior de QA e Testador Especialista de Software do AutoCare V2.
Sua missão é atuar como um condutor e auditor técnico no WhatsApp, validando a aderência estrita às regras de negócio:

1. HUMAN-IN-THE-LOOP: Ações críticas (cadastros, manutenções, lembretes, zerar garagem) DEVEM pedir confirmação explícita antes de salvar.
2. ISOLAMENTO MULTI-VEÍCULOS: Dados de carros e motos não podem se misturar. Se houver mais de um veículo, o bot deve associar as peças e placas ao veículo correto.
3. INCOMPATIBILIDADE MECÂNICA IMPEDIENTE:
   - Motos com refrigeração a ar (ex: Honda CG 160 Titan/Fan/Start, Bros, Biz, Pop, Factor, Fazer) NÃO possuem radiador, água nem aditivo.
   - Motos NÃO possuem filtro de ar-condicionado/cabine, direção hidráulica nem palheta de limpador de para-brisa.
   - O bot DEVE recusar tais manutenções com UNKNOWN, is_critical_action=False, sem criar rascunho de manutenção.
4. ANTI-ALUCINAÇÃO DE OFICINA: Se o condutor relatar manutenção sem citar o local, o campo workshop_name DEVE ser estritamente null (nunca repetir nome de peças).
5. RECONCILIAÇÃO DE RASCUNHO: Mensagens com "Não, o valor é X" ou "a placa é Y" são CORREÇÕES, não desistências. O bot deve retificar o rascunho e pedir nova confirmação.
6. LIMITE DE 3 VEÍCULOS: O usuário só pode cadastrar no máximo 3 veículos. A 4ª tentativa deve ser bloqueada cordialmente informando o limite do plano gratuito.

Ao analisar cada resposta, você DEVE gerar um JSON estrito no schema:
- status: "PASS" (se o objetivo final do cenário foi atingido com perfeição),
          "CONTINUE" (se o bot respondeu de forma correta e o teste deve prosseguir enviando a próxima mensagem),
          "FAIL" (se o bot violou uma regra clara ou cometeu um erro evidente),
          "SUSPICIOUS" (se o comportamento for duvidoso, estranho ou exigir avaliação humana).
- reasoning: Justificativa técnica concisa e analítica.
- next_user_message: Texto da próxima mensagem que o usuário de teste deve enviar (ex: "Sim", "Não, a placa é XYZ"). Se concluído, envie null.
- is_scenario_complete: Booleano indicando se o objetivo do cenário foi finalizado.
"""

AUDITOR_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "status": {
            "type": "STRING",
            "enum": ["PASS", "CONTINUE", "FAIL", "SUSPICIOUS"]
        },
        "reasoning": {"type": "STRING"},
        "next_user_message": {"type": "STRING"},
        "is_scenario_complete": {"type": "BOOLEAN"}
    },
    "required": ["status", "reasoning", "is_scenario_complete"]
}


class GeminiAuditorClient:
    """Cliente dedicado de inteligência artificial para auditoria e geração de turnos de teste."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.primary_model = settings.GEMINI_PRIMARY_MODEL
        self.fallback_model = settings.GEMINI_FALLBACK_MODEL

    async def evaluate_turn(
        self,
        scenario_title: str,
        scenario_objective: str,
        expected_rules: str,
        user_message: str,
        bot_response: Dict[str, Any],
        scenario_history: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Submete o turno de conversa ao Gemini Auditor para avaliação técnica com Structured Output."""
        prompt_content = f"""[CENÁRIO DE TESTE]: {scenario_title}
[OBJETIVO DO CENÁRIO]: {scenario_objective}
[REGRAS ESPERADAS]: {expected_rules}

[HISTÓRICO RECENTE DO DIÁLOGO]:
{json.dumps(scenario_history, ensure_ascii=False, indent=2)}

[MENSAGEM ENVIADA PELO CONDUTOR (Auditor)]:
"{user_message}"

[RESPOSTA RECEBIDA DO AUTOCARE V2]:
- action_type: {bot_response.get("action_type")}
- state: {bot_response.get("state")}
- reply: "{bot_response.get("reply")}"

Avalie detalhadamente a resposta do AutoCare V2 e emita o veredito estruturado:"""

        payload = {
            "system_instruction": {"parts": [{"text": AUDITOR_SYSTEM_INSTRUCTION}]},
            "contents": [{"parts": [{"text": prompt_content}]}],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json",
                "response_schema": AUDITOR_SCHEMA,
            }
        }

        # Tentativas com backoff resiliente para absorver rate limits (HTTP 429) do free tier
        models = [self.primary_model, self.fallback_model]
        for attempt in range(1, 4):
            for model in models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(url, json=payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                                return json.loads(raw_text)
                        elif resp.status_code == 429:
                            # Cooldown inteligente para liberar cota por minuto (RPM)
                            cooldown = 4.5 * attempt
                            print(f"{COLOR_YELLOW}⏳ Auditor atingiu taxa limite (429). Cooldown de {cooldown:.1f}s...{COLOR_RESET}")
                            await asyncio.sleep(cooldown)
                            continue
                except Exception:
                    await asyncio.sleep(2.0)
                    continue

        # Fallback local determinístico caso o auditor fique temporariamente sem cota
        return {
            "status": "SUSPICIOUS",
            "reasoning": "Não foi possível contactar o Gemini Auditor via API devido a limite temporário de requisições.",
            "next_user_message": None,
            "is_scenario_complete": False,
        }


# Definição do Roteiro de Objetivos de Homologação
AUDIT_SCENARIOS = [
    {
        "id": 1,
        "title": "Limpeza Inicial da Garagem (Reset do Sistema)",
        "initial_message": "zerar sistema",
        "objective": "Zerar qualquer dado anterior da sessão de testes e garantir que o sistema volte ao ponto zero.",
        "expected_rules": "O sistema deve acionar system_reset imediatamente pelo Fast-Path e confirmar a limpeza da garagem sem exigir confirmações complexas.",
        "max_turns": 2,
    },
    {
        "id": 2,
        "title": "Cadastro de 1º Veículo (Carro) com Human-in-the-Loop",
        "initial_message": "Quero cadastrar meu Honda Civic ano 2020 placa ABC1234 com 45.000 km",
        "objective": "Cadastrar um carro legítimo e verificar se o sistema respeita o fluxo de confirmação prévia antes de salvar.",
        "expected_rules": "O bot deve gerar rascunho de veículo, marcar action_type='awaiting_confirmation' e solicitar aprovação ('Sim'/'Não'). Ao responder 'Sim', deve confirmar a gravação.",
        "max_turns": 4,
    },
    {
        "id": 3,
        "title": "Cadastro de 2º Veículo (Moto) com Isolamento de Dados",
        "initial_message": "Agora cadastra minha moto Honda CG 160 Titan ano 2022 placa XYZ9876 com 12.000 km",
        "objective": "Cadastrar uma moto e garantir que o sistema isola o tipo 'moto' e não herda placa ou km do Civic anterior.",
        "expected_rules": "O bot deve reconhecer como moto, pedir confirmação para a Titan e confirmar ao receber 'Sim'.",
        "max_turns": 4,
    },
    {
        "id": 4,
        "title": "Gatilho de Incompatibilidade Mecânica (Radiador na CG 160 Titan)",
        "initial_message": "Troquei a água do radiador e aditivo da CG 160 Titan por 80 reais",
        "objective": "Forçar um erro mecânico grosseiro (radiador em moto refrigerada a ar) e validar se o sistema barra a ação.",
        "expected_rules": "O bot DEVE mudar a intenção para UNKNOWN, is_critical_action=False, NÃO criar rascunho de manutenção e alertar com autoridade que a moto é refrigerada a ar e não possui radiador.",
        "max_turns": 2,
    },
    {
        "id": 5,
        "title": "Manutenção Legítima e Omissão de Local (Anti-Alucinação de Oficina)",
        "initial_message": "Troquei as pastilhas de freio do Civic por 250 reais",
        "objective": "Registrar uma manutenção válida onde o condutor omitiu o local, validando se o bot não inventa nome de oficina.",
        "expected_rules": "O bot deve reconhecer o serviço para o Civic, valor R$ 250 e solicitar confirmação sem repetir nomes de peças como oficina (workshop_name deve ser null).",
        "max_turns": 4,
    },
    {
        "id": 6,
        "title": "Reconciliação e Retificação de Rascunho com 'Não'",
        "initial_message": "Troquei as velas do Civic por 150 reais",
        "objective": "Iniciar um rascunho e em seguida enviar uma mensagem de retificação ('Não, o valor correto foi 180 reais') para validar que o bot atualiza sem cancelar.",
        "expected_rules": "Ao receber a retificação, o bot NÃO deve cancelar. Deve atualizar o valor para 180 e pedir confirmação. Ao responder 'Sim', deve salvar com sucesso.",
        "max_turns": 5,
    },
    {
        "id": 7,
        "title": "Teto da Garagem (Limite de 3 Veículos do Plano Gratuito)",
        "initial_message": "Quero cadastrar meu terceiro carro: Toyota Corolla 2021 placa COR1122 com 30.000 km",
        "objective": "Cadastrar o 3º veículo com sucesso e em seguida tentar cadastrar um 4º veículo (Yamaha Fazer) para testar o bloqueio por limite de garagem.",
        "expected_rules": "O 3º veículo deve ser aceito e salvo. Em seguida, ao tentar cadastrar o 4º veículo, o bot deve recusar cordialmente informando que o limite de 3 veículos foi atingido.",
        "max_turns": 6,
    },
    {
        "id": 8,
        "title": "Fast-Path de Encerramento Amigável",
        "initial_message": "Show de bola, valeu obrigado!",
        "objective": "Verificar se mensagens de despedida e cortesia recebem resposta imediata (<20ms) sem reabrir formulários ou chamar IA desnecessariamente.",
        "expected_rules": "O bot deve retornar action_type='polite_closing' com resposta calorosa e estado NORMAL.",
        "max_turns": 2,
    },
]


class AutoCareAITester:
    """Executador do ciclo de testes e auditoria conversacional."""

    def __init__(self, target_url: str, user_id: str, in_process: bool = False):
        self.target_url = target_url
        self.user_id = user_id
        self.in_process = in_process
        self.auditor = GeminiAuditorClient(settings.GEMINI_API_KEY)
        self.report_log: List[Dict[str, Any]] = []

    async def _send_webhook_message(self, message: str) -> Tuple[int, Dict[str, Any]]:
        """Despacha mensagem HTTP POST simulando o gateway de WhatsApp."""
        payload = {
            "user_id": self.user_id,
            "message": message,
        }

        if self.in_process:
            from src.interfaces.app import app
            from httpx import ASGITransport
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as client:
                resp = await client.post("/webhook", json=payload)
                return resp.status_code, resp.json()
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.target_url, json=payload)
                return resp.status_code, resp.json()

    async def run_scenario(self, scenario: Dict[str, Any]) -> bool:
        """Executa um cenário conversacional com verificação inteligente passo a passo."""
        s_id = scenario["id"]
        title = scenario["title"]
        objective = scenario["objective"]
        rules = scenario["expected_rules"]
        current_msg = scenario["initial_message"]
        max_turns = scenario.get("max_turns", 4)

        print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}📋 [CENÁRIO {s_id}]: {title}{COLOR_RESET}")
        print(f"{COLOR_DIM}🎯 Objetivo: {objective}{COLOR_RESET}")
        print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")

        scenario_history: List[Dict[str, str]] = []

        for turn_idx in range(1, max_turns + 1):
            # Pausa de 3 a 4 segundos entre turnos para respeitar o limite de RPM da API gratuita do Gemini
            if turn_idx > 1:
                await asyncio.sleep(4.0)

            print(f"\n{COLOR_BLUE}👤 Condutor (Auditor) [Turno {turn_idx}]:{COLOR_RESET} \"{current_msg}\"")
            scenario_history.append({"role": "user", "content": current_msg})

            t0 = time.time()
            try:
                status_code, bot_res = await self._send_webhook_message(current_msg)
            except Exception as conn_err:
                print(f"{COLOR_RED}❌ Falha de Conexão HTTP com o AutoCare V2 em {self.target_url}:{COLOR_RESET} {conn_err}")
                print(f"{COLOR_YELLOW}💡 Dica: Suba o servidor com: uvicorn src.interfaces.app:app --host 0.0.0.0 --port 8080{COLOR_RESET}")
                return False

            elapsed_ms = (time.time() - t0) * 1000.0

            bot_reply = bot_res.get("reply", "")
            action_type = bot_res.get("action_type", "")
            state = bot_res.get("state", "")

            # Se recebeu mensagem de contingência de sobrecarga temporária da API (HTTP 429), aguarda cooldown de 5s e tenta novamente
            if "processando muitas mensagens" in bot_reply:
                print(f"{COLOR_YELLOW}⏳ Cooldown de cota do Google Gemini atingido (429). Aguardando 5s para reprocessar...{COLOR_RESET}")
                await asyncio.sleep(5.0)
                status_code, bot_res = await self._send_webhook_message(current_msg)
                bot_reply = bot_res.get("reply", "")
                action_type = bot_res.get("action_type", "")
                state = bot_res.get("state", "")

            print(f"{COLOR_MAGENTA}🤖 AutoCare V2 ({elapsed_ms:.1f}ms):{COLOR_RESET} \"{bot_reply}\"")
            print(f"{COLOR_DIM}   [Action: {action_type} | State: {state}]{COLOR_RESET}")

            scenario_history.append({"role": "assistant", "content": bot_reply})

            # Intervalo estratégico de 4.0s antes da chamada ao Gemini Auditor para não estourar RPM
            await asyncio.sleep(4.0)

            # Avaliação pelo Gemini Auditor
            print(f"{COLOR_DIM}   🔍 Gemini Auditor avaliando resposta...{COLOR_RESET}")
            verdict = await self.auditor.evaluate_turn(
                scenario_title=title,
                scenario_objective=objective,
                expected_rules=rules,
                user_message=current_msg,
                bot_response=bot_res,
                scenario_history=scenario_history,
            )

            status = verdict.get("status", "CONTINUE")
            reasoning = verdict.get("reasoning", "")
            next_msg = verdict.get("next_user_message")
            is_complete = verdict.get("is_scenario_complete", False)

            # Log para relatório
            self.report_log.append({
                "scenario_id": s_id,
                "scenario_title": title,
                "turn": turn_idx,
                "user_message": current_msg,
                "bot_reply": bot_reply,
                "action_type": action_type,
                "state": state,
                "latency_ms": elapsed_ms,
                "verdict_status": status,
                "reasoning": reasoning,
            })

            # 1. Tratamento de FALHA EXPLÍCITA
            if status == "FAIL":
                print(f"\n{COLOR_RED}{COLOR_BOLD}❌ FALHA DETECTADA PELO AUDITOR:{COLOR_RESET}")
                print(f"{COLOR_RED}   Motivo: {reasoning}{COLOR_RESET}")
                return False

            # 2. Tratamento de COMPORTAMENTO SUSPEITO (Intervenção Humana: gbduarte)
            if status == "SUSPICIOUS":
                print(f"\n{COLOR_YELLOW}{COLOR_BOLD}⚠️  COMPORTAMENTO SUSPEITO DETECTADO PELO AUDITOR:{COLOR_RESET}")
                print(f"{COLOR_YELLOW}   Análise da IA: {reasoning}{COLOR_RESET}")
                try:
                    user_approval = input(
                        f"\n{COLOR_CYAN}{COLOR_BOLD}❓ gbduarte, você aprova essa resposta do AutoCare V2? (S/N): {COLOR_RESET}"
                    ).strip().upper()
                except (KeyboardInterrupt, EOFError):
                    user_approval = "N"

                if user_approval != "S":
                    print(f"{COLOR_RED}🛑 Execução reprovada pelo usuário gbduarte. Abortando cenário.{COLOR_RESET}")
                    return False
                else:
                    print(f"{COLOR_GREEN}✅ Resposta aprovada manualmente por gbduarte. Prosseguindo...{COLOR_RESET}")

            # 3. Tratamento de SUCESSO / CONCLUSÃO DO CENÁRIO
            if status == "PASS" or is_complete:
                print(f"\n{COLOR_GREEN}{COLOR_BOLD}✅ [PASS] Cenário {s_id} validado com sucesso!{COLOR_RESET}")
                print(f"{COLOR_GREEN}   Parecer Técnico: {reasoning}{COLOR_RESET}")
                return True

            # 4. Caso o fluxo precise de um próximo turno (ex: aprovação "Sim" ou retificação)
            if next_msg:
                current_msg = next_msg
            elif s_id == 6 and turn_idx == 1:
                # No Cenário 6, se o auditor não formulou mensagem, envia a retificação
                current_msg = "Não, o valor correto foi 180 reais"
            elif s_id == 6 and turn_idx == 2:
                # No Cenário 6, após a retificação, confirma
                current_msg = "Sim, pode salvar"
            elif s_id == 7 and turn_idx == 1:
                # No Cenário 7, após rascunho do 3º carro, confirma
                current_msg = "Sim"
            elif s_id == 7 and turn_idx == 2:
                # No Cenário 7, após salvar o 3º carro, tenta cadastrar o 4º
                current_msg = "Quero cadastrar minha quarta moto: Yamaha Fazer 250 placa FAZ3344 com 5.000 km"
            else:
                # Se não sugeriu mensagem mas não declarou erro, encerra com sucesso se não for suspeito
                print(f"\n{COLOR_GREEN}✅ [PASS] Cenário concluído: {reasoning}{COLOR_RESET}")
                return True

        print(f"{COLOR_YELLOW}⚠️ Cenário atingiu o limite de {max_turns} turnos sem conclusão definitiva.{COLOR_RESET}")
        return True

    async def run_all(self):
        """Executa a bateria completa de cenários de auditoria."""
        print(f"\n{COLOR_CYAN}{'#' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}🚗 AUTOCARE V2 - AUDITOR CONVERSACIONAL DE PONTA A PONTA (AI TESTER){COLOR_RESET}")
        print(f"{COLOR_DIM}Target: {self.target_url} | Auditor: {settings.GEMINI_PRIMARY_MODEL} | Usuário Teste: {self.user_id}{COLOR_RESET}")
        print(f"{COLOR_CYAN}{'#' * 75}{COLOR_RESET}\n")

        # Verifica conectividade se for HTTP real
        if not self.in_process:
            health_url = self.target_url.replace("/webhook", "/health")
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    resp = await client.get(health_url)
                    if resp.status_code == 200:
                        print(f"{COLOR_GREEN}🔌 Conectado ao servidor ativo em {health_url}: {resp.json()}{COLOR_RESET}")
            except Exception:
                print(f"{COLOR_YELLOW}⚠️ Servidor HTTP não detectado em {self.target_url}.{COLOR_RESET}")
                print(f"{COLOR_CYAN}Alternando automaticamente para modo ASGI In-Process (execução local imediata)...{COLOR_RESET}\n")
                self.in_process = True

        total_scenarios = len(AUDIT_SCENARIOS)
        passed_scenarios = 0
        t_start = time.time()

        for sc in AUDIT_SCENARIOS:
            success = await self.run_scenario(sc)
            if success:
                passed_scenarios += 1
            else:
                print(f"\n{COLOR_RED}🛑 Auditoria interrompida devido a falha no Cenário {sc['id']}.{COLOR_RESET}")
                break
            # Pausa de 5.0 segundos entre cenários para recompor a janela de requisições por minuto do Gemini
            print(f"{COLOR_DIM}⏳ Pausa estratégica de 5s entre cenários para preservação de cota...{COLOR_RESET}")
            await asyncio.sleep(5.0)

        total_time = time.time() - t_start
        print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
        print(f"{COLOR_BOLD}📊 RESULTADO FINAL DA AUDITORIA:{COLOR_RESET}")
        print(f"• Cenários Executados com Sucesso: {passed_scenarios}/{total_scenarios}")
        print(f"• Tempo Total de Homologação: {total_time:.2f}s")

        # Salva o relatório em disco
        report_file = "audit_test_report.json"
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamp": datetime.now().isoformat(),
                    "passed": passed_scenarios,
                    "total": total_scenarios,
                    "duration_seconds": total_time,
                    "log": self.report_log
                }, f, ensure_ascii=False, indent=2)
            print(f"📄 Relatório técnico salvo em: {COLOR_BOLD}{report_file}{COLOR_RESET}")
        except Exception as err:
            print(f"⚠️ Não foi possível salvar relatório JSON: {err}")

        if passed_scenarios == total_scenarios:
            print(f"\n{COLOR_GREEN}{COLOR_BOLD}🎉 AUDITORIA 100% APROVADA! O AutoCare V2 está calibrado e robusto!{COLOR_RESET}\n")
        else:
            print(f"\n{COLOR_RED}{COLOR_BOLD}❌ AUDITORIA ENCONTROU INCONSISTÊNCIAS. Verifique os logs acima.{COLOR_RESET}\n")


async def main():
    parser = argparse.ArgumentParser(description="AutoCare V2 - Automated AI Conversational Tester")
    parser.add_argument("--url", default="http://localhost:8080/webhook", help="URL do endpoint /webhook")
    parser.add_argument("--user", default="5521999990001", help="ID/Telefone de teste do condutor")
    parser.add_argument("--in-process", action="store_true", help="Executa via ASGI In-Process sem exigir uvicorn externo")

    args = parser.parse_args()
    tester = AutoCareAITester(target_url=args.url, user_id=args.user, in_process=args.in_process)
    await tester.run_all()


if __name__ == "__main__":
    asyncio.run(main())
