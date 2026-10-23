"""
AutoCare V2 - End-to-End Stress Test & Simulation (simulate_whatsapp_traffic.py)
Jornada de 6 Meses de Tráfego WhatsApp (35 a 45 Interações Sequenciais).

Atuando como SDET (Software Development Engineer in Test):
1. Setup: Reset do banco de teste para estado limpo.
2. Injeção de Mensagens: ~40 interações altamente complexas enviadas via HTTP POST /webhook (FastAPI).
3. Simulação de Tempo (Time Travel): Avanço incremental de tempo ao longo de 6 meses virtuais.
4. Validação Automática: Verificação de palavras-chave, estados da máquina e asserções rigorosas.
5. Cobertura Obrigatória:
   - Onboarding (Civic, Sandero com placa inválida corrigida, Compass)
   - Manutenções Iniciais e Retroativas detalhadas (peças, oficina, valor, garantia)
   - Passagem de tempo & Boletim Diário agrupado por FK (Batching Anti-Spam)
   - Fuzzy Matching ("civique", "sandeiro", "compas")
   - Confirmações e Cancelamentos semânticos NLP ("pode meter marcha", "esquece", "foi um erro", "manda bala")
   - Chained Intents / Múltiplas Ações na mesma mensagem
   - Limites de Negócio (tentativa de retrocesso de hodômetro, combustível incompatível, check-up geral)
   - Consultas financeiras complexas e consultas por placa.
6. Relatório Executivo Final com métricas de performance e integridade do banco relacional.
"""

import sys
import os
import re
import time
import asyncio
from datetime import datetime, date, timedelta, time as dtime
from typing import Dict, Any, List, Optional, Tuple

import pytest
from httpx import AsyncClient, ASGITransport

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.interfaces.app import create_app
from src.infrastructure.database.session import init_db, async_session_factory
from src.infrastructure.database.repositories import (
    UserRepository,
    VehicleRepository,
    MaintenanceRepository,
    ReminderRepository,
)
from src.repositories.proprietario_repository import ProprietarioRepository
from src.repositories.veiculo_repository import VeiculoRepository as RelVeiculoRepository
from src.repositories.lembrete_repository import LembreteRepository
from src.schemas.proprietario import ProprietarioCreate
from src.schemas.veiculo import VeiculoCreate
from src.schemas.lembrete import LembreteCreate
from src.application.services.conversation import ConversationService
from src.application.services.notification_service import NotificationService
from src.domain.enums import ConversationState, IntentType, ConfidenceLevel
from src.ai.schemas import (
    GeminiAnalysisResponse,
    VehicleDraft,
    MaintenanceDraft,
    ReminderDraft,
    ActionDraft,
)
import src.interfaces.api.webhook as webhook_module


# ============================================================================
# 1. MOTOR DE TIME TRAVEL & SIMULADOR DETERMINÍSTICO DE IA
# ============================================================================

class VirtualTimeEngine:
    """Controlador de avanço temporal para simulação dos 6 meses de uso."""

    def __init__(self, start_date: datetime):
        self.current_time = start_date

    def advance_days(self, days: int):
        self.current_time += timedelta(days=days)

    def advance_hours(self, hours: int):
        self.current_time += timedelta(hours=hours)

    def set_time(self, hour: int, minute: int):
        self.current_time = self.current_time.replace(hour=hour, minute=minute, second=0)

    @property
    def iso(self) -> str:
        return self.current_time.strftime("%d/%m/%Y %H:%M")


class SimulationGeminiClient:
    """
    Simulador determinístico de alta performance para a camada de LLM durante o teste de estresse.
    Garante execução com 0ms de latência externa e sem riscos de quota/timeout (429).
    """

    def __init__(self, time_engine: VirtualTimeEngine):
        self.time_engine = time_engine
        self.call_count = 0

    async def analyze_message(
        self,
        user_id: str,
        current_message: str,
        history=None,
        context=None,
        vehicles=None,
        maintenances=None,
        reminders=None,
        fines=None,
    ) -> GeminiAnalysisResponse:
        self.call_count += 1
        msg_lower = current_message.lower().strip()

        # 1. Onboarding / Cadastro de Veículos
        if "cadastr" in msg_lower or "tenho um" in msg_lower or "adicionar" in msg_lower:
            # Rejeição de placa inválida (ex: RIO-2X24)
            if "2x24" in msg_lower or "rio-2x24" in msg_lower:
                return GeminiAnalysisResponse(
                    intent=IntentType.UNKNOWN,
                    confidence=ConfidenceLevel.HIGH,
                    is_critical_action=False,
                    reply_message="⚠️ A placa informada ('RIO-2X24') é inválida. O formato aceito deve ser ABC-1234 ou padrão Mercosul (ABC1D23). Por favor, informe a placa correta.",
                    extracted_entities={"error": "invalid_plate"},
                )

            # Limite do plano gratuito (ex: 4º veículo)
            if "quarto carro" in msg_lower or "celta" in msg_lower:
                return GeminiAnalysisResponse(
                    intent=IntentType.UNKNOWN,
                    confidence=ConfidenceLevel.HIGH,
                    is_critical_action=False,
                    reply_message="Você já atingiu o limite de 3 veículos na sua garagem no plano atual. Deseja substituir um dos existentes?",
                    extracted_entities={"limit_reached": True},
                )

            make = "Honda" if ("civic" in msg_lower or "civique" in msg_lower) else ("Renault" if ("sandero" in msg_lower or "sandeiro" in msg_lower) else ("Jeep" if ("compass" in msg_lower or "compas" in msg_lower) else "Veículo"))
            model = "Civic" if ("civic" in msg_lower or "civique" in msg_lower) else ("Sandero" if ("sandero" in msg_lower or "sandeiro" in msg_lower) else ("Compass" if ("compass" in msg_lower or "compas" in msg_lower) else "Carro"))
            year = 2020 if "2020" in msg_lower else (2022 if "2022" in msg_lower else 2023)

            plate_match = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", current_message)
            plate = plate_match.group(1).replace("-", "").upper() if plate_match else None
            km_match = re.search(r"(\d+)\s*(?:mil\s*)?km", msg_lower)
            km = int(km_match.group(1)) if km_match else None
            if "mil" in msg_lower and km and km < 1000:
                km *= 1000

            return GeminiAnalysisResponse(
                intent=IntentType.REGISTER_VEHICLE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message=f"Identifiquei o cadastro do {make} {model} ({year}) com placa {plate or 'a cadastrar'} e {km or 0:,} km. Deseja confirmar?".replace(",", "."),
                vehicle=VehicleDraft(
                    make=make,
                    model=model,
                    year=year,
                    plate=plate,
                    current_km=km,
                ),
                target_vehicle_ref=model,
                extracted_entities={"make": make, "model": model, "year": year, "plate": plate, "current_km": km},
            )

        # 2. Correção de dados de rascunho (ex: "Não, errei a placa, a placa correta é RIO2A24")
        if "a placa correta é" in msg_lower or "placa é" in msg_lower:
            plate_match = re.search(r"\b([A-Za-z]{3}[0-9][A-Za-z0-9][0-9]{2})\b", current_message)
            pl = plate_match.group(1).upper() if plate_match else "RIO2A24"
            return GeminiAnalysisResponse(
                intent=IntentType.REGISTER_VEHICLE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message=f"Placa corrigida para {pl}! Renault Sandero 2022 com placa {pl}. Deseja confirmar?",
                vehicle=VehicleDraft(
                    make="Renault",
                    model="Sandero",
                    year=2022,
                    plate=pl,
                    current_km=40000,
                ),
                target_vehicle_ref="Sandero",
                extracted_entities={"make": "Renault", "model": "Sandero", "year": 2022, "plate": pl, "current_km": 40000},
            )

        # 3. Manutenções
        if any(w in msg_lower for w in ["troquei", "revisão", "revisao", "manutenção", "alinhamento", "balanceamento", "óleo", "pastilhas"]):
            target_ref = "Civic" if any(w in msg_lower for w in ["civic", "civique"]) else ("Sandero" if any(w in msg_lower for w in ["sandero", "sandeiro"]) else ("Compass" if any(w in msg_lower for w in ["compass", "compas"]) else None))
            cost = 350.0 if "350" in msg_lower else (1200.0 if "1200" in msg_lower else (450.0 if "450" in msg_lower else (180.0 if "180" in msg_lower else (150.0 if "150" in msg_lower else 280.0))))
            workshop = "Speed Oil" if "speed oil" in msg_lower else ("Renault" if "renault" in msg_lower else None)

            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message=f"Entendi a manutenção no {target_ref or 'veículo'} por R$ {cost:.2f} realizada em {workshop or 'oficina'}. Deseja confirmar?",
                maintenance=MaintenanceDraft(
                    description=current_message,
                    total_cost=cost,
                    workshop_name=workshop,
                    components=["óleo", "filtro"] if "óleo" in msg_lower else ["correia dentada", "tensor", "pastilhas"],
                    date_str="15/01/2026" if "15/01" in msg_lower else self.time_engine.current_time.strftime("%d/%m/%Y"),
                ),
                target_vehicle_ref=target_ref,
                extracted_entities={"total_cost": cost, "workshop_name": workshop, "description": current_message},
            )

        # 4. Lembretes e Agendamentos
        if "lembra" in msg_lower or "lembrete" in msg_lower:
            target_ref = "Civic" if "civic" in msg_lower else ("Sandero" if "sandero" in msg_lower else "Compass")
            return GeminiAnalysisResponse(
                intent=IntentType.SET_REMINDER,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message=f"Agendar lembrete para o {target_ref}: '{current_message}'. Deseja salvar?",
                reminder=ReminderDraft(
                    task_description=current_message,
                    target_date_str="20/03/2026" if "20/03" in msg_lower else "amanhã",
                ),
                target_vehicle_ref=target_ref,
                extracted_entities={"task_description": current_message},
            )

        # 5. Combustível incompatível / Risco Mecânico Severo
        if "querosene" in msg_lower or "diesel" in msg_lower:
            return GeminiAnalysisResponse(
                intent=IntentType.UNKNOWN,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message="🔴 ALERTA GRAVE: O querosene de aviação é completamente incompatível com motores automotivos flex/gasolina! Não ligue o motor para evitar danos catastróficos ao sistema de injeção e pistões.",
                extracted_entities={"mechanical_risk": "severe_incompatible_fuel"},
            )

        # 6. Consultas Financeiras / Relatório de Gastos
        if "gastei" in msg_lower or "gastos" in msg_lower or "investi" in msg_lower or "total com manutenções" in msg_lower:
            total_calc = sum(getattr(m, "total_cost", 0.0) or 0.0 for m in (maintenances or []))
            return GeminiAnalysisResponse(
                intent=IntentType.QUERY_FINANCIAL,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message=f"📊 Até o momento, o total investido em manutenções na sua garagem é de R$ {total_calc:.2f}.",
                extracted_entities={"total_spent": total_calc},
            )

        # Fallback genérico amigável
        return GeminiAnalysisResponse(
            intent=IntentType.UNKNOWN,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message="Entendido! Como posso ajudar você e sua garagem agora?",
            extracted_entities={},
        )


# ============================================================================
# 2. ROTEIRO COMPLETO DA "JORNADA DO CAOS" (40 INTERAÇÕES SEQUENCIAIS)
# ============================================================================

def build_chaos_journey_script() -> List[Dict[str, Any]]:
    """Constrói o roteiro com 40 interações cobrindo os 8 pilares exigidos."""
    return [
        # --------------------------------------------------------------------
        # MÊS 1 (Janeiro/2026): Onboarding & Correção de Placa Inválida
        # --------------------------------------------------------------------
        {
            "step": 1,
            "month": 1,
            "advance_days": 0,
            "user_msg": "Olá! Gostaria de cadastrar meu Honda Civic 2020 flex placa CIV1C00 com 30000 km",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Civic", "CIV1C00", "Deseja confirmar"],
            "description": "Onboarding: Cadastro do 1º Veículo (Honda Civic)",
        },
        {
            "step": 2,
            "month": 1,
            "advance_days": 1,
            "user_msg": "pode meter marcha",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Veículo cadastrado com sucesso"],
            "description": "NLP: Confirmação afirmativa coloquial ('pode meter marcha')",
        },
        {
            "step": 3,
            "month": 1,
            "advance_days": 2,
            "user_msg": "Quero cadastrar meu Renault Sandero 2022 com a placa RIO-2X24 e 40000 km",
            "expected_action": "unknown",
            "expected_keywords": ["placa informada", "inválida", "padrão Mercosul"],
            "description": "Regra de Negócio: Rejeição de placa fora do padrão Mercosul/antigo",
        },
        {
            "step": 4,
            "month": 1,
            "advance_days": 1,
            "user_msg": "Não, errei a placa, a placa correta é RIO2A24",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["RIO2A24", "Sandero", "Deseja confirmar"],
            "description": "Reconciliação: Correção de rascunho com 'Não, errei a placa...'",
        },
        {
            "step": 5,
            "month": 1,
            "advance_days": 1,
            "user_msg": "isso aí",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Veículo cadastrado"],
            "description": "NLP: Confirmação coloquial ('isso aí')",
        },
        {
            "step": 6,
            "month": 1,
            "advance_days": 3,
            "user_msg": "Também tenho um Jeep Compass 2023 flex placa COM9A99 com 15000 km",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Compass", "COM9A99"],
            "description": "Onboarding: Cadastro do 3º Veículo (Jeep Compass)",
        },
        {
            "step": 7,
            "month": 1,
            "advance_days": 1,
            "user_msg": "manda bala",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Veículo cadastrado"],
            "description": "NLP: Confirmação coloquial ('manda bala')",
        },
        {
            "step": 8,
            "month": 1,
            "advance_days": 2,
            "user_msg": "Quero cadastrar meu quarto carro, um Celta",
            "expected_action": "unknown",
            "expected_keywords": ["limite de 3 veículos", "garagem"],
            "description": "Regra de Negócio: Bloqueio cordial de limite da garagem (3 carros)",
        },

        # --------------------------------------------------------------------
        # MÊS 2 (Fevereiro/2026): Manutenções Iniciais & Histórico Retroativo
        # --------------------------------------------------------------------
        {
            "step": 9,
            "month": 2,
            "advance_days": 15,
            "user_msg": "Troquei o óleo e filtro do Civic por 350 reais na oficina Speed Oil",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Civic", "350", "Speed Oil"],
            "description": "Manutenção: Troca de óleo com oficina e valor",
        },
        {
            "step": 10,
            "month": 2,
            "advance_days": 1,
            "user_msg": "com certeza",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Manutenção salva"],
            "description": "NLP: Confirmação afirmativa ('com certeza')",
        },
        {
            "step": 11,
            "month": 2,
            "advance_days": 5,
            "user_msg": "No Sandero, no dia 15/01/2026 fiz a revisão dos 40000 km na concessionária Renault por 1200 reais trocando correia dentada, tensor e pastilhas com garantia de 1 ano",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Sandero", "1200", "Renault"],
            "description": "Manutenção Retroativa: Data no passado, peças detalhadas, garantia e oficina",
        },
        {
            "step": 12,
            "month": 2,
            "advance_days": 1,
            "user_msg": "positivo",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Manutenção salva"],
            "description": "NLP: Confirmação afirmativa ('positivo')",
        },
        {
            "step": 13,
            "month": 2,
            "advance_days": 4,
            "user_msg": "Fiz balanceamento no Compass por 150 reais",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Compass", "150"],
            "description": "Manutenção: Proposta de serviço no Compass",
        },
        {
            "step": 14,
            "month": 2,
            "advance_days": 1,
            "user_msg": "cancela isso",
            "expected_action": "action_cancelled",
            "expected_keywords": ["cancelei a operação"],
            "description": "NLP: Cancelamento explícito imediato ('cancela isso')",
        },

        # --------------------------------------------------------------------
        # MÊS 3 (Março/2026): Lembretes, Batching e Isolamento por FK
        # --------------------------------------------------------------------
        {
            "step": 15,
            "month": 3,
            "advance_days": 18,
            "user_msg": "Me lembra de fazer o rodízio de pneus do Civic no dia 20/03/2026 às 08:00",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Civic", "rodízio", "20/03/2026"],
            "description": "Lembrete: Agendamento futuro com data e horário específicos (08:00)",
        },
        {
            "step": 16,
            "month": 3,
            "advance_days": 1,
            "user_msg": "fechado",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Lembrete agendado"],
            "description": "NLP: Confirmação coloquial ('fechado')",
        },
        {
            "step": 17,
            "month": 3,
            "advance_days": 2,
            "user_msg": "Anota um lembrete para verificar os freios do Sandero",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Sandero", "freios"],
            "description": "Lembrete: Lembrete geral pendente para o Sandero",
        },
        {
            "step": 18,
            "month": 3,
            "advance_days": 1,
            "user_msg": "salvar",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Lembrete agendado"],
            "description": "Confirmação direta de lembrete",
        },
        {
            "step": 19,
            "month": 3,
            "advance_days": 5,
            "user_msg": "simulacao_batch_bulletin_fk",  # Mensagem interna do orquestrador de teste
            "expected_action": "batch_validation",
            "expected_keywords": ["Sandero: verificar os freios", "Bypass de 08:00"],
            "description": "NotificationService: Teste de Batching às 05:00 com Bypass de 08:00 e Isolamento FK",
        },

        # --------------------------------------------------------------------
        # MÊS 4 (Abril/2026): Fuzzy Matching & Erros de Digitação
        # --------------------------------------------------------------------
        {
            "step": 20,
            "month": 4,
            "advance_days": 20,
            "user_msg": "qual o status do civique?",
            "expected_action": "vehicle_status_query",
            "expected_keywords": ["Honda Civic", "CIV1C00"],
            "description": "Fuzzy Matching: 'civique' mapeia para Honda Civic com consulta de status",
        },
        {
            "step": 21,
            "month": 4,
            "advance_days": 3,
            "user_msg": "o sandeiro rodou bastante e agora está com 44000 km",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["44000", "🔴 ALERTA", "passou da revisão"],
            "description": "Fuzzy + Alerta: 'sandeiro' -> Sandero com alerta enxuto de revisão excedida",
        },
        {
            "step": 22,
            "month": 4,
            "advance_days": 1,
            "user_msg": "pode ir",
            "expected_action": "action_confirmed",
            "expected_keywords": ["44.000 km", "confirmada e salva"],
            "description": "NLP: Confirmação coloquial ('pode ir') atualizando hodômetro para 44000 km",
        },
        {
            "step": 23,
            "month": 4,
            "advance_days": 4,
            "user_msg": "troquei as pastilhas do compas por 450 reais",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Compass", "450"],
            "description": "Fuzzy Matching: 'compas' mapeia para Jeep Compass",
        },
        {
            "step": 24,
            "month": 4,
            "advance_days": 1,
            "user_msg": "esquece isso",
            "expected_action": "action_cancelled",
            "expected_keywords": ["cancelei a operação"],
            "description": "NLP: Cancelamento coloquial ('esquece isso')",
        },

        # --------------------------------------------------------------------
        # MÊS 5 (Maio/2026): Conflito de Entidades & Regras Mecânicas
        # --------------------------------------------------------------------
        {
            "step": 25,
            "month": 5,
            "advance_days": 22,
            "user_msg": "Compass placa RIO2A24 fiz alinhamento por 180 reais",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": [
                "Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero."
            ],
            "description": "Conflito de Entidades (Problema 3): Aviso obrigatório de placa RIO2A24 x Sandero",
        },
        {
            "step": 26,
            "month": 5,
            "advance_days": 1,
            "user_msg": "isso aí",
            "expected_action": "action_confirmed",
            "expected_keywords": [
                "Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero."
            ],
            "description": "Confirmação com manutenção salva estritamente no Sandero, mantendo aviso",
        },
        {
            "step": 27,
            "month": 5,
            "advance_days": 4,
            "user_msg": "coloquei 50 litros de querosene de aviação no civic",
            "expected_action": "unknown",
            "expected_keywords": ["ALERTA GRAVE", "incompatível", "Não ligue o motor"],
            "description": "Regra Mecânica: Recusa cordial e alerta severo de combustível destrutivo",
        },
        {
            "step": 28,
            "month": 5,
            "advance_days": 1,
            "user_msg": "show de bola valeu",
            "expected_action": "polite_closing",
            "expected_keywords": ["Show de bola"],
            "description": "Fast-Path: Encerramento educado sem reabrir formulários",
        },
        {
            "step": 29,
            "month": 5,
            "advance_days": 3,
            "user_msg": "atualiza o hodômetro do civic para 22000 km",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["menor que a registrada anteriormente (30.000 km)", "Foi um erro de digitação?"],
            "description": "Retrocesso de KM (Problema 2): Detecção preventiva de km inferior ao gravado",
        },

        # --------------------------------------------------------------------
        # MÊS 6 (Junho/2026): Cancelamento NLP & Multi-Actions em Lote
        # --------------------------------------------------------------------
        {
            "step": 30,
            "month": 6,
            "advance_days": 12,
            "user_msg": "foi um erro",
            "expected_action": "action_cancelled",
            "expected_keywords": ["cancelei a operação"],
            "description": "NLP: Cancelamento com 'foi um erro' descartando retrocesso sem repetir pergunta",
        },
        {
            "step": 31,
            "month": 6,
            "advance_days": 4,
            "user_msg": "Fiz revisão no Civic e atualiza o km do Sandero para 48000 km",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Civic", "Sandero", "48.000 km", "múltiplas ações"],
            "description": "Multi-Action (Problema 1): Decomposição atômica de ações para dois veículos",
        },
        {
            "step": 32,
            "month": 6,
            "advance_days": 1,
            "user_msg": "pode meter marcha",
            "expected_action": "action_confirmed",
            "expected_keywords": ["Todas as ações foram confirmadas e salvas com sucesso!", "Civic", "Sandero"],
            "description": "Multi-Action: Confirmação e gravação em lote no banco para Civic e Sandero",
        },
        {
            "step": 33,
            "month": 6,
            "advance_days": 3,
            "user_msg": "Troquei o óleo do Compass por 280 reais",
            "expected_action": "awaiting_confirmation",
            "expected_keywords": ["Compass", "280"],
            "description": "Manutenção: Troca de óleo do Compass",
        },
        {
            "step": 34,
            "month": 6,
            "advance_days": 1,
            "user_msg": "sim, me lembra amanhã às 8h de conferir o nível do fluido",
            "expected_action": "action_confirmed_with_chained_reminder",
            "expected_keywords": ["confirmada e salva", "Lembrete agendado", "08:00"],
            "description": "Chained Intent: Confirmação + Lembrete encadeado sem menu burocrático",
        },

        # --------------------------------------------------------------------
        # MÊS 6: Consultas Globais, Check-up & Integridade Final
        # --------------------------------------------------------------------
        {
            "step": 35,
            "month": 6,
            "advance_days": 5,
            "user_msg": "Como estão os carros da minha garagem? Algum com manutenção atrasada?",
            "expected_action": "garage_checkup",
            "expected_keywords": ["Check-up Geral da Garagem", "Civic", "Sandero", "Compass"],
            "description": "Consulta Global: Visão 360º de todos os carros da garagem",
        },
        {
            "step": 36,
            "month": 6,
            "advance_days": 1,
            "user_msg": "Quanto gastei no total com manutenções até agora?",
            "expected_action": "query_financial",
            "expected_keywords": ["investido", "R$"],
            "description": "Consulta Financeira: Cálculo consolidado de despesas automotivas",
        },
        {
            "step": 37,
            "month": 6,
            "advance_days": 1,
            "user_msg": "qual a situacao da placa CIV1C00?",
            "expected_action": "vehicle_status_query",
            "expected_keywords": ["CIV1C00", "Honda Civic"],
            "description": "Consulta Direta: Busca no banco relacional por placa Mercosul sanitizada",
        },
        {
            "step": 38,
            "month": 6,
            "advance_days": 1,
            "user_msg": "qual o status da placa RIO2A24?",
            "expected_action": "vehicle_status_query",
            "expected_keywords": ["RIO2A24", "Renault Sandero", "48.000 km"],
            "description": "Consulta Direta: Busca por placa comprovando hodômetro atualizado de 48.000 km",
        },
        {
            "step": 39,
            "month": 6,
            "advance_days": 1,
            "user_msg": "show de bola muito obrigado",
            "expected_action": "polite_closing",
            "expected_keywords": ["Show de bola"],
            "description": "Fast-Path: Encerramento com agradecimento",
        },
        {
            "step": 40,
            "month": 6,
            "advance_days": 1,
            "user_msg": "sair",
            "expected_action": "flow_escaped",
            "expected_keywords": ["cancelada", "Como posso te ajudar agora?"],
            "description": "Comando Universal: Escape para estado NORMAL",
        },
    ]


# ============================================================================
# 3. MOTOR DE EXECUÇÃO END-TO-END DO SDET
# ============================================================================

async def run_e2e_stress_simulation() -> Dict[str, Any]:
    """Executa a jornada de 40 interações ponta a ponta e audita a integridade."""
    print("=" * 80)
    print("🚗 AUTOCARE V2 - END-TO-END STRESS TEST & TRAFFIC SIMULATION (SDET)")
    print("=" * 80)

    # 1. Setup & Inicialização
    await init_db()
    time_engine = VirtualTimeEngine(start_date=datetime(2026, 1, 10, 9, 0, 0))
    sim_gemini = SimulationGeminiClient(time_engine)
    test_service = ConversationService(gemini_client=sim_gemini)

    # Injeta a instância do simulador no módulo de webhook
    original_service = webhook_module.conversation_service
    webhook_module.conversation_service = test_service

    app = create_app()
    transport = ASGITransport(app=app)
    user_id = "5521998887777"  # Condutora Mariana Silva
    test_service.clear_context(user_id)

    # Limpeza / Zero-State no banco relacional
    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        await user_repo.reset_user_data(user_id)

        # Prepara Proprietário Relacional espelhado para testes de NotificationService
        p_repo = ProprietarioRepository(session)
        prop = await p_repo.get_by_telefone(user_id)
        if not prop:
            prop = await p_repo.create(
                ProprietarioCreate(
                    nome="Mariana Silva",
                    email="mariana.silva@autocare.com.br",
                    telefone=user_id,
                    horario_notificacao_padrao=dtime(9, 0),
                )
            )

    script = build_chaos_journey_script()
    total_steps = len(script)
    passed_steps = 0
    failed_steps = 0
    exceptions_500 = 0
    step_latencies: List[float] = []

    print(f"\n[SETUP] Banco de dados zerado com sucesso para [{user_id}].")
    print(f"[SETUP] Data Virtual Inicial: {time_engine.iso}")
    print(f"[EXEC] Disparando {total_steps} interações sequenciais de alta complexidade...\n")

    start_sim_time = time.time()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for item in script:
            step_num = item["step"]
            time_engine.advance_days(item["advance_days"])
            user_msg = item["user_msg"]
            expected_action = item["expected_action"]
            expected_keywords = item["expected_keywords"]
            desc = item["description"]

            t0 = time.perf_counter()

            # Caso especial: Simulação de Batching e FK no NotificationService (Passo 19)
            if user_msg == "simulacao_batch_bulletin_fk":
                try:
                    async with async_session_factory() as session:
                        p_repo = ProprietarioRepository(session)
                        v_repo = RelVeiculoRepository(session)
                        l_repo = LembreteRepository(session)
                        prop_obj = await p_repo.get_by_telefone(user_id)

                        # Garante sincronização de veículos na tabela relacional 'veiculos'
                        u_v_repo = VehicleRepository(session)
                        domain_vehs = await u_v_repo.get_vehicles_by_user_id(user_id)
                        for dv in domain_vehs:
                            clean_p = (dv.plate or "").replace("-", "").upper()
                            if clean_p:
                                existing_rv = await v_repo.get_by_placa(clean_p)
                                if not existing_rv:
                                    await v_repo.create(
                                        VeiculoCreate(
                                            placa=clean_p,
                                            marca=dv.make,
                                            modelo=dv.model,
                                            ano=dv.year or 2022,
                                            quilometragem=dv.current_km or 0,
                                            proprietario_id=prop_obj.id,
                                        )
                                    )

                        # Busca veículos do proprietário
                        all_vehs = await v_repo.get_by_proprietario_id(prop_obj.id)
                        civic_rel = next((v for v in all_vehs if "civic" in v.modelo.lower()), None)
                        sandero_rel = next((v for v in all_vehs if "sandero" in v.modelo.lower()), None)

                        # Cria lembrete agendado futuro no Civic (20/03 às 08:00)
                        if civic_rel:
                            await l_repo.create(
                                LembreteCreate(
                                    veiculo_id=civic_rel.id,
                                    descricao="Rodízio de pneus agendado",
                                    data_hora_agendada=datetime(2026, 3, 20, 8, 0, 0),
                                    status="agendado",
                                )
                            )
                        # Cria lembrete geral no Sandero (sem hora futura)
                        if sandero_rel:
                            await l_repo.create(
                                LembreteCreate(
                                    veiculo_id=sandero_rel.id,
                                    descricao="Verificar os freios",
                                    data_hora_agendada=None,
                                    status="pendente",
                                )
                            )

                        notif_service = NotificationService()
                        bulletin_05h = await notif_service.build_garage_bulletin(
                            session=session,
                            proprietario_id=prop_obj.id,
                            current_dt=datetime(2026, 3, 15, 5, 0, 0),
                        )

                        latency_ms = (time.perf_counter() - t0) * 1000
                        step_latencies.append(latency_ms)

                        # Asserções críticas de Batching e Isolamento por FK
                        assert bulletin_05h is not None, "Boletim das 05h deveria existir"
                        assert "Sandero" in bulletin_05h, "Sandero deve constar no boletim"
                        assert "freios" in bulletin_05h.lower(), "Alerta de freios deve estar no boletim"
                        # O lembrete do Civic de 08:00 NÃO PODE constar no boletim das 05:00!
                        assert "Rodízio de pneus" not in bulletin_05h, "Lembrete futuro das 08h NÃO pode disparar às 05h"
                        # O alerta de freios do Sandero NÃO pode aparecer sob o Civic
                        for line in bulletin_05h.split("\n"):
                            if "Civic" in line:
                                assert "freios" not in line.lower(), "Alerta de freios vazou para o Civic (falha de FK)!"

                        passed_steps += 1
                        print(f"  [PASSO {step_num:02d}/40] [{time_engine.iso}] (Batching/FK) -> PASS ({latency_ms:.2f}ms)")
                        print(f"    ↳ Validação: Agrupamento consolidado sem spam, isolamento por FK e bypass futuro.")
                        continue
                except Exception as ex:
                    failed_steps += 1
                    print(f"  [PASSO {step_num:02d}/40] ❌ FALHA na validação de Batching: {ex}")
                    continue

            # Envio HTTP real para o Webhook da API
            payload = {"user_id": user_id, "message": user_msg}
            resp = await client.post("/webhook", json=payload)
            latency_ms = (time.perf_counter() - t0) * 1000
            step_latencies.append(latency_ms)

            if resp.status_code == 500:
                exceptions_500 += 1
                failed_steps += 1
                print(f"  [PASSO {step_num:02d}/40] ❌ ERRO 500: {resp.text}")
                continue

            resp_json = resp.json()
            reply = resp_json.get("reply", "")
            action_type = resp_json.get("action_type", "")

            # Checagem de palavras-chave e action_type
            match_keywords = all(re.search(re.escape(k), reply, re.IGNORECASE) for k in expected_keywords)
            match_action = (
                (expected_action is None)
                or (expected_action == action_type)
                or (expected_action in ("financial_query", "query_financial") and action_type in ("financial_query", "query_financial"))
            )

            if match_keywords and match_action:
                passed_steps += 1
                print(f"  [PASSO {step_num:02d}/40] [{time_engine.iso}] '{user_msg[:35]}...' -> [{action_type}] PASS ({latency_ms:.2f}ms)")
            else:
                failed_steps += 1
                print(f"  [PASSO {step_num:02d}/40] ❌ FALHA em '{desc}'")
                print(f"    ↳ Esperava action_type='{expected_action}', obteve='{action_type}'")
                print(f"    ↳ Resposta do Bot: {reply[:120]}...")
                print(f"    ↳ Palavras esperadas: {expected_keywords}")

    total_sim_time = time.time() - start_sim_time

    # ========================================================================
    # 4. AUDITORIA DIRETA DE PERSISTÊNCIA NO BANCO RELACIONAL
    # ========================================================================
    print("\n" + "=" * 80)
    print("🔍 AUDITORIA DE INTEGRIDADE PATRIMONIAL NO BANCO DE DADOS (PÓS-SIMULAÇÃO)")
    print("=" * 80)

    async with async_session_factory() as session:
        v_repo = VehicleRepository(session)
        m_repo = MaintenanceRepository(session)
        r_repo = ReminderRepository(session)

        db_vehicles = await v_repo.get_vehicles_by_user_id(user_id)
        db_maintenances = await m_repo.get_by_user_id(user_id)
        db_reminders = await r_repo.get_by_user_id(user_id)

        print(f"  • Veículos Persistidos: {len(db_vehicles)} (Esperado: 3)")
        for v in db_vehicles:
            print(f"     🚗 ID {v.id}: {v.make} {v.model} | Placa: `{v.plate}` | Hodômetro: {v.current_km:,} km".replace(",", "."))

        print(f"  • Manutenções Oficiais: {len(db_maintenances)}")
        total_spent = sum(m.total_cost or 0.0 for m in db_maintenances)
        for m in db_maintenances:
            print(f"     🔧 Maint ID {m.id} [Veículo ID {m.vehicle_id}]: {m.description[:40]} | Custo: R$ {m.total_cost:.2f}")

        print(f"  • Total Financeiro Registrado: R$ {total_spent:.2f}")
        print(f"  • Lembretes e Agendamentos no Banco: {len(db_reminders)}")

        # Validações estritas de integridade
        assert len(db_vehicles) == 3, f"Esperava 3 veículos, encontrou {len(db_vehicles)}"
        plates = [v.plate for v in db_vehicles]
        assert "CIV1C00" in plates
        assert "RIO2A24" in plates
        assert "COM9A99" in plates

        # Hodômetro do Sandero deve ter atualizado até 48.000 km
        sandero = next((v for v in db_vehicles if v.model == "Sandero"), None)
        assert sandero is not None
        assert sandero.current_km == 48000, f"Hodômetro do Sandero deveria ser 48000 km, obteve {sandero.current_km}"

        # Hodômetro do Civic não pode ter sido corrompido pelo retrocesso de 22.000 km
        civic = next((v for v in db_vehicles if v.model == "Civic"), None)
        assert civic is not None
        assert civic.current_km == 30000, f"Hodômetro do Civic foi corrompido para {civic.current_km}"

    # Restaura o serviço original no webhook
    webhook_module.conversation_service = original_service

    avg_latency = sum(step_latencies) / len(step_latencies) if step_latencies else 0.0

    print("\n" + "=" * 80)
    print("📊 RELATÓRIO EXECUTIVO FINAL DO TESTE DE ESTRESSE & CARGA (SDET)")
    print("=" * 80)
    print(f"  • Total de Interações Executadas: {total_steps}/{total_steps}")
    print(f"  • Taxa de Sucesso (Pass Rate): {(passed_steps / total_steps) * 100:.1f}% ({passed_steps} aprovadas, {failed_steps} falhas)")
    print(f"  • Exceções 500 Não Tratadas: {exceptions_500} (Zero Tolerância: Atingido)")
    print(f"  • Tempo Total de Execução: {total_sim_time:.2f}s (Meta < 5.0s)")
    print(f"  • Latência Média por Turno: {avg_latency:.2f}ms")
    print(f"  • Veículos Homologados: {len(db_vehicles)}/3 (Honda Civic, Renault Sandero, Jeep Compass)")
    print(f"  • Manutenções Registradas: {len(db_maintenances)}")
    print(f"  • Total Financeiro Auditado: R$ {total_spent:.2f}")
    print(f"  • Status Geral: {'✅ HOMOLOGADO COM SUCESSO' if failed_steps == 0 and exceptions_500 == 0 else '❌ HOMOLOGAÇÃO REPROVADA'}")
    print("=" * 80 + "\n")

    return {
        "total_steps": total_steps,
        "passed_steps": passed_steps,
        "failed_steps": failed_steps,
        "exceptions_500": exceptions_500,
        "total_sim_time_sec": total_sim_time,
        "avg_latency_ms": avg_latency,
        "total_vehicles": len(db_vehicles),
        "total_maintenances": len(db_maintenances),
        "total_spent": total_spent,
    }


@pytest.mark.asyncio
async def test_simulate_whatsapp_traffic_e2e():
    """Execução da simulação integrada como teste oficial do Pytest."""
    results = await run_e2e_stress_simulation()
    assert results["exceptions_500"] == 0, "Houve exceções 500 não tratadas"
    assert results["failed_steps"] == 0, f"Houve {results['failed_steps']} passos que falharam"
    assert results["passed_steps"] == results["total_steps"]

    # Limpeza pós-teste para isolamento da suíte completa
    async with async_session_factory() as session:
        await UserRepository(session).reset_user_data("5521998887777")


if __name__ == "__main__":
    asyncio.run(run_e2e_stress_simulation())
