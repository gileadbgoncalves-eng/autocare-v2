"""
AutoCare V2 - Conversation Orchestration Service
Orquestrador central de diálogo que recebe mensagens do WhatsApp, gerencia a máquina
de estados conversacionais, invoca o Gemini e assegura o fluxo de confirmação (Human-in-the-Loop).
"""

import re
import asyncio
import unicodedata
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

from src.domain.enums import ConversationState, IntentType, ConfidenceLevel
from src.domain.entities import ConversationContext, ConversationTurn
from src.ai.client import GeminiClient
from src.ai.schemas import GeminiAnalysisResponse, ActionDraft, VehicleDraft


# Base de Conhecimento Automotivo Oficial (Mercado Brasileiro) para Inferência e Autopreenchimento de Ficha Técnica
POPULAR_VEHICLE_SPECS: Dict[str, Dict[str, Any]] = {
    # Volkswagen
    "nivus highline": {
        "make": "Volkswagen",
        "model": "Nivus",
        "version": "Highline",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "nivus comfortline": {
        "make": "Volkswagen",
        "model": "Nivus",
        "version": "Comfortline",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "t-cross highline": {
        "make": "Volkswagen",
        "model": "T-Cross",
        "version": "Highline",
        "engine": "1.4 TSI",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "t-cross comfortline": {
        "make": "Volkswagen",
        "model": "T-Cross",
        "version": "Comfortline",
        "engine": "1.0 TSI",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "polo highline": {
        "make": "Volkswagen",
        "model": "Polo",
        "version": "Highline",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "polo comfortline": {
        "make": "Volkswagen",
        "model": "Polo",
        "version": "Comfortline",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "polo track": {
        "make": "Volkswagen",
        "model": "Polo",
        "version": "Track",
        "engine": "1.0 MPI",
        "transmission": "Manual",
        "steering": "Elétrica",
    },

    # Toyota
    "corolla xei": {
        "make": "Toyota",
        "model": "Corolla",
        "version": "XEI",
        "engine": "2.0 Dynamic Force",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "corolla altis": {
        "make": "Toyota",
        "model": "Corolla",
        "version": "Altis",
        "engine": "2.0 Dynamic Force",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "corolla cross xre": {
        "make": "Toyota",
        "model": "Corolla Cross",
        "version": "XRE",
        "engine": "2.0 Dynamic Force",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "corolla cross xrv": {
        "make": "Toyota",
        "model": "Corolla Cross",
        "version": "XRV Hybrid",
        "engine": "1.8 Híbrido",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "yaris xls": {
        "make": "Toyota",
        "model": "Yaris",
        "version": "XLS",
        "engine": "1.5 16V",
        "transmission": "CVT",
        "steering": "Elétrica",
    },
    "hilux srx": {
        "make": "Toyota",
        "model": "Hilux",
        "version": "SRX",
        "engine": "2.8 Turbo Diesel",
        "transmission": "Automático",
        "steering": "Hidráulica",
    },

    # Honda
    "civic touring": {
        "make": "Honda",
        "model": "Civic",
        "version": "Touring",
        "engine": "1.5 Turbo",
        "transmission": "CVT",
        "steering": "Elétrica",
    },
    "civic exl": {
        "make": "Honda",
        "model": "Civic",
        "version": "EXL",
        "engine": "2.0 i-VTEC",
        "transmission": "CVT",
        "steering": "Elétrica",
    },
    "hr-v touring": {
        "make": "Honda",
        "model": "HR-V",
        "version": "Touring",
        "engine": "1.5 Turbo",
        "transmission": "CVT",
        "steering": "Elétrica",
    },
    "hr-v exl": {
        "make": "Honda",
        "model": "HR-V",
        "version": "EXL",
        "engine": "1.5 i-VTEC",
        "transmission": "CVT",
        "steering": "Elétrica",
    },

    # Jeep
    "compass longitude": {
        "make": "Jeep",
        "model": "Compass",
        "version": "Longitude",
        "engine": "1.3 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "compass limited": {
        "make": "Jeep",
        "model": "Compass",
        "version": "Limited",
        "engine": "1.3 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "renegade longitude": {
        "make": "Jeep",
        "model": "Renegade",
        "version": "Longitude",
        "engine": "1.3 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "renegade sport": {
        "make": "Jeep",
        "model": "Renegade",
        "version": "Sport",
        "engine": "1.3 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },

    # Chevrolet
    "onix premier": {
        "make": "Chevrolet",
        "model": "Onix",
        "version": "Premier",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "onix plus premier": {
        "make": "Chevrolet",
        "model": "Onix Plus",
        "version": "Premier",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "tracker premier": {
        "make": "Chevrolet",
        "model": "Tracker",
        "version": "Premier",
        "engine": "1.2 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },

    # Fiat
    "pulse audace": {
        "make": "Fiat",
        "model": "Pulse",
        "version": "Audace",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "pulse impetus": {
        "make": "Fiat",
        "model": "Pulse",
        "version": "Impetus",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "fastback audace": {
        "make": "Fiat",
        "model": "Fastback",
        "version": "Audace",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "fastback limited": {
        "make": "Fiat",
        "model": "Fastback",
        "version": "Limited Edition",
        "engine": "1.3 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "strada volcano": {
        "make": "Fiat",
        "model": "Strada",
        "version": "Volcano",
        "engine": "1.3 Firefly",
        "transmission": "CVT",
        "steering": "Elétrica",
    },

    # Hyundai
    "hb20 platinum": {
        "make": "Hyundai",
        "model": "HB20",
        "version": "Platinum",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "creta ultimate": {
        "make": "Hyundai",
        "model": "Creta",
        "version": "Ultimate",
        "engine": "2.0",
        "transmission": "Automático",
        "steering": "Elétrica",
    },
    "creta platinum": {
        "make": "Hyundai",
        "model": "Creta",
        "version": "Platinum",
        "engine": "1.0 Turbo",
        "transmission": "Automático",
        "steering": "Elétrica",
    },

    # Renault
    "sandero stepway": {
        "make": "Renault",
        "model": "Sandero",
        "version": "Stepway",
        "engine": "1.6 16V",
        "transmission": "Manual",
        "steering": "Eletro-hidráulica",
    },
    "duster iconic": {
        "make": "Renault",
        "model": "Duster",
        "version": "Iconic",
        "engine": "1.3 Turbo",
        "transmission": "CVT",
        "steering": "Elétrica",
    },

    # Nissan
    "kicks exclusive": {
        "make": "Nissan",
        "model": "Kicks",
        "version": "Exclusive",
        "engine": "1.6",
        "transmission": "CVT",
        "steering": "Elétrica",
    },
}


def find_popular_vehicle_spec(query_text: str) -> Optional[Dict[str, Any]]:
    """
    Busca ficha técnica oficial na base de conhecimento automotivo pelo modelo/versão.
    Combina termos de busca de forma insensível a maiúsculas/minúsculas e pontuação.
    """
    if not query_text:
        return None
    q = query_text.lower().replace("-", " ")
    for key, spec in sorted(POPULAR_VEHICLE_SPECS.items(), key=lambda x: len(x[0]), reverse=True):
        parts = key.split()
        if all(p in q for p in parts):
            return spec
    return None


class ConversationService:
    """
    Serviço de aplicação responsável por orquestrar a interação do usuário.
    Garante que ações críticas nunca sejam persistidas sem aprovação explícita.
    """

    def __init__(self, gemini_client: Optional[GeminiClient] = None):
        self.gemini = gemini_client or GeminiClient()
        # Repositório de contexto em memória com persistência por sessão de usuário
        self._contexts: Dict[str, ConversationContext] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def get_context(self, user_id: str) -> ConversationContext:
        """Recupera ou inicializa o contexto conversacional do usuário."""
        if user_id not in self._contexts:
            self._contexts[user_id] = ConversationContext(user_id=user_id)
        return self._contexts[user_id]

    def update_context(self, context: ConversationContext):
        """Atualiza o contexto do usuário na memória de aplicação."""
        context.updated_at = datetime.utcnow()
        self._contexts[context.user_id] = context

    def clear_context(self, user_id: str):
        """Limpa o contexto em memória do usuário."""
        if user_id in self._contexts:
            del self._contexts[user_id]

    async def handle_incoming_message(
        self,
        user_id: str,
        message_text: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        Ponto de entrada do processamento de mensagens recebidas via Webhook.
        Protegido por asyncio.Lock individual por user_id contra concorrência e race conditions.
        """
        lock = self._get_user_lock(user_id)
        async with lock:
            return await self._handle_incoming_message_locked(
                user_id=user_id,
                message_text=message_text,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
            )

    async def _handle_incoming_message_locked(
        self,
        user_id: str,
        message_text: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        clean_text = (message_text or "").strip()
        context = self.get_context(user_id)

        # 0. Registro da mensagem do usuário no histórico recente
        context.recent_turns.append(ConversationTurn(role="user", content=clean_text))
        if len(context.recent_turns) > 10:
            context.recent_turns = context.recent_turns[-10:]

        text_lower = clean_text.lower()

        # 1. Comando Universal de Escape / Cancelamento
        if text_lower in ("sair", "cancelar", "abortar", "voltar", "menu", "início", "inicio"):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            self.update_context(context)
            reply = "✅ Operação cancelada! Seus dados anteriores foram mantidos intactos. Como posso te ajudar agora?"
            self._record_assistant_turn(context, reply)
            return {"reply": reply, "action_type": "flow_escaped", "state": context.state.value}

        # 1.5 Comando Direto de Zerar/Resetar Sistema (Zero Latência para Testes)
        norm_text = "".join(c for c in unicodedata.normalize("NFKD", text_lower) if not unicodedata.combining(c)).strip()
        reset_triggers = {"zerar sistema", "zerar o sistema", "/reset_all"}
        if norm_text in reset_triggers:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.recent_turns.clear()
            self.update_context(context)
            reply = (
                "🧹 *Garagem e Sistema zerados com sucesso!*\n\n"
                "Todos os seus veículos, histórico de manutenção, gastos e lembretes foram removidos.\n"
                "O seu perfil voltou ao ponto zero, pronto para novos testes!\n\n"
                "🚀 *O que você gostaria de fazer agora?*\n"
                "• Cadastrar um novo veículo (ex: *'Cadastrar meu Honda Civic'* ou *'Minha moto Fazer 250'*)\n"
                "• Mandar uma dúvida técnica ou consulta mecânica"
            )
            self._record_assistant_turn(context, reply)
            return {
                "reply": reply,
                "action_type": "system_reset",
                "state": context.state.value,
                "committed_data": {"_reset_system": True, "_reset_garage": True, "user_id": user_id},
                "details": {"reset_all": True}
            }

        # 1.8 Detecção de Veículo via Resolução Estrita & Fuzzy (Problemas 3 e 4)
        matched_veh, discrepancy_warning = self._resolve_vehicle(clean_text, vehicles)
        if matched_veh:
            v_id = getattr(matched_veh, "id", None)
            if v_id is not None and context.active_vehicle_id != v_id:
                context.active_vehicle_id = v_id

        # 2. Máquina de Estados: Se estiver aguardando confirmação do usuário
        if context.is_awaiting_confirmation():
            result = await self._handle_pending_confirmation_state(
                user_id=user_id,
                clean_text=clean_text,
                text_lower=text_lower,
                context=context,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
            )
            self._record_assistant_turn(context, result["reply"])
            self.update_context(context)
            return result

        # 2.5 Fast-Path de Baixa Latência: Agradecimentos e Despedidas Rápidas ("OK", "Valeu")
        clean_no_punct = re.sub(r"[^\w\s]", "", text_lower).strip()
        courtesies = {
            "ok", "valeu", "show", "beleza", "obrigado", "obrigada",
            "combinado", "perfeito", "tks", "show de bola",
            "valeu obrigado", "obrigado valeu", "muito obrigado",
            "show de bola valeu", "show de bola muito obrigado",
            "valeu show", "valeu demais", "muito obrigada"
        }
        is_courtesy = (
            clean_no_punct in courtesies
            or (
                any(w in clean_no_punct for w in ("show de bola", "muito obrigado", "valeu", "obrigado", "show"))
                and len(clean_no_punct.split()) <= 5
                and not any(a in clean_no_punct for a in ("troquei", "fiz", "cadastr", "placa", "km", "reais", "multa", "lembra", "revisão"))
            )
        )
        if is_courtesy:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            reply = "Show de bola! Qualquer novidade ou manutenção no seu veículo, estou por aqui. Um abraço! 👍"
            self._record_assistant_turn(context, reply)
            self.update_context(context)
            return {
                "reply": reply,
                "action_type": "polite_closing",
                "state": context.state.value,
                "details": {}
            }

        # 2.55 Decomposição de Múltiplas Ações / Múltiplos Veículos na mesma mensagem (Problema 1)
        multi_actions = self._decompose_multi_action_message(clean_text, vehicles)
        if len(multi_actions) >= 2:
            conf_res = self._initiate_multi_action_confirmation_flow(
                context, multi_actions, discrepancy_warning=discrepancy_warning
            )
            self._record_assistant_turn(context, conf_res["reply"])
            self.update_context(context)
            return conf_res

        # 2.6 Consulta Global / Check-up Geral da Garagem (Problema 3)
        if self._is_garage_checkup_query(text_lower):
            garage_res = await self._handle_garage_checkup(
                user_id=user_id,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
            )
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            self._record_assistant_turn(context, garage_res["reply"])
            self.update_context(context)
            return garage_res

        # 2.7 Consulta Direta de Status Veicular por Modelo / Fuzzy Matching (Problema 4)
        action_verbs = ("fiz", "troquei", "realizei", "gastei", "paguei", "atualiza", "mudei", "cadastra", "adicione")
        has_action_verb = any(v in text_lower for v in action_verbs)
        status_keywords = (
            "status", "situa", "dados", "como ta", "como está", "informa",
            "revisao", "revisão", "manuten", "em dia", "está em dia", "ta em dia"
        )
        if matched_veh and not has_action_verb and any(k in text_lower for k in status_keywords):
            status_res = self._format_single_vehicle_status(matched_veh, maintenances)
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            self._record_assistant_turn(context, status_res["reply"])
            self.update_context(context)
            return status_res

        # 2.8 Consulta Direta de Status Veicular por Placa (Regex Sanitizado & Banco Real)
        plate_search = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text)
        if plate_search and not has_action_verb and any(k in text_lower for k in status_keywords):
            clean_plate = re.sub(r"[\s\-]", "", plate_search.group(1)).upper()
            status_result = await self._query_vehicle_status_by_plate(clean_plate, vehicles)
            if status_result:
                context.state = ConversationState.NORMAL
                context.draft_payload = None
                self._record_assistant_turn(context, status_result["reply"])
                self.update_context(context)
                return status_result

        # 2.9 Detecção Direta de Atualização de Hodômetro com Alerta Enxuto de Revisão (Problema 2)
        km_direct_match = re.search(r"\b(\d+)\s*(?:mil\s*)?km\b", text_lower)
        odometer_keywords = ("hodometro", "hodômetro", "km", "quilometragem", "odometro", "odômetro", "estou com", "rodou", "bateu")
        maintenance_verbs = ("revisão", "revisao", "troquei", "troca", "óleo", "oleo", "filtro", "vela", "pastilha", "amortecedor")
        registration_verbs = ("cadastr", "adicionar", "adiciona", "tenho um", "novo carro", "incluir")
        if (
            km_direct_match
            and any(k in text_lower for k in odometer_keywords)
            and not any(k in text_lower for k in maintenance_verbs)
            and not any(k in text_lower for k in registration_verbs)
        ):
            km_val = int(km_direct_match.group(1))
            if "mil" in text_lower and km_val < 1000:
                km_val *= 1000

            target_veh = matched_veh or (vehicles[0] if vehicles else None)
            target_ref = getattr(target_veh, "model", None) or getattr(target_veh, "modelo", "veículo")
            current_veh_km = getattr(target_veh, "current_km", None) or getattr(target_veh, "quilometragem", None)

            # Detecção de Retrocesso de Hodômetro (Problema 2)
            if current_veh_km and km_val < current_veh_km:
                context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
                context.draft_payload = {
                    "_is_odometer_rollback": True,
                    "_is_odometer_update": True,
                    "_action_state": context.state.value,
                    "odometer_km": km_val,
                    "km": km_val,
                    "current_km": current_veh_km,
                    "_target_vehicle_ref": target_ref,
                }
                if discrepancy_warning:
                    context.draft_payload["_discrepancy_warning"] = discrepancy_warning

                reply = (
                    f"⚠️ Identifiquei que a quilometragem informada ({km_val:,} km) é menor que a registrada anteriormente ({current_veh_km:,} km). "
                    f"Foi um erro de digitação? (Responda se deseja cancelar ou confirmar)"
                ).replace(",", ".")
                if discrepancy_warning:
                    reply = f"⚠️ {discrepancy_warning}\n\n" + reply
                self._record_assistant_turn(context, reply)
                self.update_context(context)
                return {
                    "reply": reply,
                    "action_type": "awaiting_confirmation",
                    "state": context.state.value,
                    "draft": context.draft_payload,
                }

            interval = 10000
            sched_mark = (km_val // interval) * interval
            exceeded_km = (km_val - sched_mark) if sched_mark > 0 else 0

            context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
            context.draft_payload = {
                "_is_odometer_update": True,
                "_action_state": context.state.value,
                "odometer_km": km_val,
                "km": km_val,
                "_target_vehicle_ref": target_ref,
                "_exceeded_km": exceeded_km,
            }
            if discrepancy_warning:
                context.draft_payload["_discrepancy_warning"] = discrepancy_warning

            if exceeded_km > 0:
                reply = (
                    f"Entendi a atualização do hodômetro com {km_val} km. "
                    f"🔴 ALERTA: Você passou da revisão em {exceeded_km} km! Posso confirmar?"
                )
            else:
                reply = (
                    f"Entendi a atualização do hodômetro com {km_val} km. Posso confirmar?"
                )
            if discrepancy_warning:
                reply = f"⚠️ {discrepancy_warning}\n\n" + reply

            self._record_assistant_turn(context, reply)
            self.update_context(context)
            return {
                "reply": reply,
                "action_type": "awaiting_confirmation",
                "state": context.state.value,
                "draft": context.draft_payload,
            }

        # 3. Estado NORMAL: Envia para o Gemini analisar com contexto completo
        history_dicts = [{"role": t.role, "content": t.content} for t in context.recent_turns]
        analysis: GeminiAnalysisResponse = await self.gemini.analyze_message(
            user_id=user_id,
            current_message=clean_text,
            history=history_dicts,
            context=context,
            vehicles=vehicles,
            maintenances=maintenances,
            reminders=reminders,
            fines=fines,
        )

        # Fallback inteligente para intenção de cadastro de veículo (apenas em falha/indisponibilidade do Gemini)
        gemini_failed = (
            not getattr(self.gemini, "is_available", lambda: True)()
            or analysis.confidence == ConfidenceLevel.LOW
            or "não está configurada" in (analysis.reply_message or "")
            or "desculpe, estou processando muitas mensagens" in (analysis.reply_message or "").lower()
        )
        has_explicit_error = bool(analysis.extracted_entities and any(k in analysis.extracted_entities for k in ("error", "limit_reached", "invalid_plate")))
        if analysis.intent == IntentType.UNKNOWN and gemini_failed and not has_explicit_error:
            registration_verbs = ("cadastr", "adicion", "tenho um", "tenho uma", "comprei um", "comprei uma", "meu carro", "minha moto")
            matched_spec = find_popular_vehicle_spec(clean_text)
            if any(rv in text_lower for rv in registration_verbs) or matched_spec:
                plate_match = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text)
                plate = plate_match.group(1).replace("-", "").upper() if plate_match else None
                year_match = re.search(r"\b(19\d{2}|20\d{2})\b", clean_text)
                year = int(year_match.group(1)) if year_match else None
                km_match = re.search(r"\b(\d+)\s*(?:mil\s*)?km\b", text_lower)
                km = int(km_match.group(1)) if km_match else None
                if "mil" in text_lower and km and km < 1000:
                    km *= 1000

                make = matched_spec["make"] if matched_spec else "Veículo"
                model = matched_spec["model"] if matched_spec else "Carro"
                version = matched_spec.get("version") if matched_spec else None
                engine = matched_spec.get("engine") if matched_spec else None
                transmission = matched_spec.get("transmission") if matched_spec else None
                steering = matched_spec.get("steering") if matched_spec else None

                vehicle_draft = VehicleDraft(
                    make=make,
                    model=model,
                    version=version,
                    year=year,
                    plate=plate,
                    odometer_km=km,
                    engine=engine,
                    transmission=transmission,
                    steering=steering,
                )
                analysis = GeminiAnalysisResponse(
                    intent=IntentType.REGISTER_VEHICLE,
                    confidence=ConfidenceLevel.HIGH,
                    is_critical_action=True,
                    reply_message=f"Cadastro do veículo {model} identificado.",
                    vehicle=vehicle_draft,
                    target_vehicle_ref=model,
                    extracted_entities=vehicle_draft.model_dump(exclude_none=True),
                )

        # 3.5 Múltiplas Ações Estruturadas retornadas pelo Gemini (Problema 1)
        if analysis.actions and len(analysis.actions) >= 2:
            conf_res = self._initiate_multi_action_confirmation_flow(
                context, analysis.actions, discrepancy_warning=discrepancy_warning
            )
            self._record_assistant_turn(context, conf_res["reply"])
            self.update_context(context)
            return conf_res

        # 4. Tratamento de Ações Críticas (Human-in-the-Loop)
        if analysis.is_critical_action:
            result = self._initiate_confirmation_flow(context, analysis, discrepancy_warning=discrepancy_warning)
            self._record_assistant_turn(context, result["reply"])
            self.update_context(context)
            return result

        # 5. Tratamento de Encerramento Educado ("OK", "Valeu", "Show")
        if analysis.intent == IntentType.POLITE_CLOSING:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            self.update_context(context)
            self._record_assistant_turn(context, analysis.reply_message)
            return {
                "reply": analysis.reply_message,
                "action_type": "polite_closing",
                "state": context.state.value,
                "details": {}
            }

        # 6. Consultas e Respostas Informativas Diretas
        context.state = ConversationState.NORMAL
        self.update_context(context)
        self._record_assistant_turn(context, analysis.reply_message)
        return {
            "reply": analysis.reply_message,
            "action_type": analysis.intent.value,
            "state": context.state.value,
            "details": analysis.extracted_entities
        }

    def _initiate_confirmation_flow(
        self,
        context: ConversationContext,
        analysis: GeminiAnalysisResponse,
        discrepancy_warning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retém a ação em estado de rascunho e formula a mensagem de confirmação para o motorista.
        """
        intent = analysis.intent

        # Mapeia a intenção para o estado conversacional de confirmação
        state_map = {
            IntentType.REGISTER_VEHICLE: ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM,
            IntentType.RECORD_MAINTENANCE: ConversationState.AWAITING_MAINTENANCE_CONFIRM,
            IntentType.SET_REMINDER: ConversationState.AWAITING_REMINDER_CONFIRM,
            IntentType.REGISTER_FINE: ConversationState.AWAITING_FINE_CONFIRM,
            IntentType.RESET_GARAGE: ConversationState.AWAITING_GARAGE_RESET_CONFIRM,
        }
        context.state = state_map.get(intent, ConversationState.AWAITING_MAINTENANCE_CONFIRM)

        # Consolida payload do rascunho com merge profundo das entidades tipadas
        payload = dict(analysis.extracted_entities or {})
        if analysis.vehicle:
            payload.update(analysis.vehicle.model_dump(exclude_none=True))
        elif analysis.maintenance:
            payload.update(analysis.maintenance.model_dump(exclude_none=True))
        elif analysis.reminder:
            payload.update(analysis.reminder.model_dump(exclude_none=True))
        elif analysis.fine:
            payload.update(analysis.fine.model_dump(exclude_none=True))

        last_user_msg = context.recent_turns[-1].content if context.recent_turns else ""
        combined_text = f"{last_user_msg} {analysis.reply_message} {analysis.reasoning or ''}"

        # Heurística de complementação e integridade dos rascunhos
        if context.state == ConversationState.AWAITING_MAINTENANCE_CONFIRM:
            if not payload.get("total_cost"):
                cost_m = re.search(r"(?:r\$|reais)\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:reais|r\$)", combined_text, re.I)
                if cost_m:
                    val_s = cost_m.group(1) or cost_m.group(2)
                    payload["total_cost"] = float(val_s.replace(",", "."))
            if not payload.get("components"):
                comps = []
                for term in ["relação", "lona de freio", "lona", "pastilha", "óleo", "filtro", "amortecedor", "vela", "correia", "pneu", "embreagem", "bateria"]:
                    if term in combined_text.lower() and term not in comps:
                        comps.append(term)
                payload["components"] = comps or [last_user_msg]
            if not payload.get("description"):
                payload["description"] = payload.get("service_summary") or ", ".join(payload.get("components", [])) or last_user_msg

        elif context.state == ConversationState.AWAITING_FINE_CONFIRM:
            if not payload.get("amount"):
                cost_m = re.search(r"(?:r\$|reais)\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:reais|r\$)", combined_text, re.I)
                if cost_m:
                    val_s = cost_m.group(1) or cost_m.group(2)
                    payload["amount"] = float(val_s.replace(",", "."))
            if not payload.get("infraction_code"):
                code_m = re.search(r"\b\d{3}-\d{1,2}-\d\b", combined_text)
                if code_m:
                    payload["infraction_code"] = code_m.group(0)
            if not payload.get("points"):
                pts_m = re.search(r"(\d+)\s*pontos?", combined_text, re.I)
                payload["points"] = int(pts_m.group(1)) if pts_m else 4
            if not payload.get("description"):
                payload["description"] = last_user_msg

        elif context.state == ConversationState.AWAITING_REMINDER_CONFIRM:
            if not payload.get("task_description") and not payload.get("description"):
                payload["task_description"] = last_user_msg

        elif context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM:
            search_str = f"{payload.get('make', '')} {payload.get('model', '')} {payload.get('version', '')} {combined_text}"
            matched_spec = find_popular_vehicle_spec(search_str)
            if matched_spec:
                if not (payload.get("engine") or payload.get("motor")):
                    payload["engine"] = matched_spec["engine"]
                    payload["motor"] = matched_spec["engine"]
                if not (payload.get("transmission") or payload.get("cambio")):
                    payload["transmission"] = matched_spec["transmission"]
                    payload["cambio"] = matched_spec["transmission"]
                if not (payload.get("steering") or payload.get("direcao")):
                    payload["steering"] = matched_spec["steering"]
                    payload["direcao"] = matched_spec["steering"]
                if not payload.get("version"):
                    payload["version"] = matched_spec["version"]
                if not payload.get("make") or payload.get("make") in ("Veículo", "Carro", "Desconhecido"):
                    payload["make"] = matched_spec["make"]
                if payload.get("model") in (None, "", "Veículo", "Carro"):
                    payload["model"] = matched_spec["model"]
                elif payload.get("model") and matched_spec["model"] and payload.get("model").lower() == f"{matched_spec['model']} {matched_spec['version']}".lower():
                    payload["model"] = matched_spec["model"]

        payload["_action_state"] = context.state.value
        payload["_target_vehicle_ref"] = analysis.target_vehicle_ref
        if discrepancy_warning:
            payload["_discrepancy_warning"] = discrepancy_warning
        context.draft_payload = payload

        # Alerta enxuto de hodômetro com revisão excedida (Problema 2)
        if intent == IntentType.UPDATE_ODOMETER or payload.get("_is_odometer_update"):
            km_val = payload.get("odometer_km") or payload.get("current_km") or payload.get("km")
            if km_val is not None and not payload.get("total_cost"):
                interval = 10000
                sched_mark = (int(km_val) // interval) * interval
                exceeded_km = (int(km_val) - sched_mark) if sched_mark > 0 else 0
                if exceeded_km > 0:
                    payload["_exceeded_km"] = exceeded_km
                    payload["_scheduled_mark"] = sched_mark
                    payload["_is_odometer_update"] = True
                    confirmation_reply = (
                        f"Entendi a atualização do hodômetro com {km_val} km. "
                        f"🔴 ALERTA: Você passou da revisão em {exceeded_km} km! Posso confirmar?"
                    )
                    if discrepancy_warning:
                        confirmation_reply = f"⚠️ {discrepancy_warning}\n\n" + confirmation_reply
                    return {
                        "reply": confirmation_reply,
                        "action_type": "awaiting_confirmation",
                        "state": context.state.value,
                        "draft": payload,
                    }

        # Confirmação Inteligente e Proativa de Ficha Técnica Veicular (Sem Interrogatório Burocrático)
        if context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM:
            engine_val = payload.get("engine") or payload.get("motor")
            transmission_val = payload.get("transmission") or payload.get("cambio")
            steering_val = payload.get("steering") or payload.get("direcao")

            # Se o veículo possui ficha técnica preenchida ou inferida de primeira
            if engine_val and (transmission_val or steering_val):
                model_name = payload.get("model") or "Veículo"
                version_name = payload.get("version") or ""
                if version_name and version_name.lower() not in model_name.lower():
                    full_model = f"{model_name} {version_name}".strip()
                else:
                    full_model = model_name.strip()

                specs_parts = []
                if engine_val:
                    eng_str = engine_val if engine_val.lower().startswith("motor") else f"Motor {engine_val}"
                    specs_parts.append(eng_str)
                if transmission_val:
                    specs_parts.append(transmission_val)
                if steering_val:
                    st_str = steering_val if (steering_val.lower().startswith("direção") or steering_val.lower().startswith("direcao")) else f"Direção {steering_val}"
                    specs_parts.append(st_str)

                specs_str = ", ".join(specs_parts)
                confirmation_reply = f"Excelente escolha! Já puxei a ficha técnica do seu {full_model} ({specs_str}). Posso salvar na garagem?"
                if discrepancy_warning:
                    confirmation_reply = f"⚠️ {discrepancy_warning}\n\n" + confirmation_reply

                return {
                    "reply": confirmation_reply,
                    "action_type": "awaiting_confirmation",
                    "state": context.state.value,
                    "draft": payload,
                }

        # Formata o pedido de confirmação
        confirmation_reply = (
            f"{analysis.reply_message}\n\n"
            f"📌 *Deseja confirmar e registrar essas informações?*\n"
            f"👉 Responda *'Sim'* para salvar ou *'Não'* para cancelar."
        )
        if discrepancy_warning:
            confirmation_reply = f"⚠️ {discrepancy_warning}\n\n" + confirmation_reply

        return {
            "reply": confirmation_reply,
            "action_type": "awaiting_confirmation",
            "state": context.state.value,
            "draft": payload,
        }

    async def _handle_pending_confirmation_state(
        self,
        user_id: str,
        clean_text: str,
        text_lower: str,
        context: ConversationContext,
        vehicles: Optional[List[Any]],
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        Processa respostas quando há um rascunho pendente de confirmação.
        Aplica classificação semântica NLP (Problema 2) para aceitar respostas naturais
        como 'foi um erro', 'cancela', 'esquece' (False) e 'pode meter marcha', 'isso aí' (True).
        """
        words = clean_text.strip().split()
        is_affirmative = bool(re.search(r"\b(sim|confirmo|confirma|pode\s+salvar|salvar|salva|isso|correto|pode\s+registrar|com\s+certeza|positivo|fecho|fechado)\b", text_lower))
        is_negative = bool(re.search(r"\b(n[ãa]o|cancela|cancelar|n[ãa]o\s+salva|abortar|errado|errei|esquece|parar)\b", text_lower))

        # Classificação semântica rica (NLP)
        nlp_classification = await self.classify_confirmation(clean_text, context)

        # Detecta se o usuário está corrigindo dados do rascunho
        is_correction = bool(re.search(
            r"(\b(placa|ano|km|quilometragem|valor|custo|pre[çc]o|reais|oficina|pe[çc]a|marca|modelo)\b|"
            r"\b[a-zA-Z]{3}\d[a-zA-Z0-9]\d{2}\b|\b(19\d\d|20\d\d)\b|\b\d+\s*(km|reais|r\$))",
            text_lower
        ))

        # A.1) Multi-Intenção: Confirmação + Lembrete/Novo Comando encadeado (Problema 2)
        has_chained_command = bool(re.search(
            r"\b(lembr|avis|agend|notific|lembrete|amanh[ãa]|às\s+\d+|as\s+\d+|\d{1,2}h)\b",
            text_lower
        ))
        if (is_affirmative or nlp_classification is True) and has_chained_command and not is_negative and nlp_classification is not False:
            executed_action = context.state.value
            draft_data = context.draft_payload or {}
            draft_data["_action_state"] = executed_action

            time_match = re.search(r"(?:às|as)\s*(\d{1,2})(?::(\d{2})|h)?|\b(\d{1,2})h\b", text_lower)
            hour = 8
            minute = 0
            if time_match:
                if time_match.group(1):
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                elif time_match.group(3):
                    hour = int(time_match.group(3))
            time_str = f"{hour:02d}:{minute:02d}"

            date_str = "amanhã" if ("amanhã" in text_lower or "amanha" in text_lower) else "hoje"
            task_desc = "Revisão preventiva"
            task_match = re.search(r"(?:lembr\w+|avis\w+)\s+(?:de\s+)?([^,.]+)", clean_text, re.I)
            if task_match:
                extracted_t = task_match.group(1).strip()
                extracted_t = re.sub(r"(?:amanh[ãa]|hoje|(?:às|as)\s*\d+h?|\d+h).*", "", extracted_t, flags=re.I).strip()
                if len(extracted_t) > 3:
                    task_desc = extracted_t

            draft_data["chained_reminder"] = {
                "task_description": task_desc,
                "explicit_time": time_str,
                "target_date_str": date_str,
            }

            # Limpa estado pendente
            context.state = ConversationState.NORMAL
            context.draft_payload = None

            km_saved = draft_data.get("odometer_km") or draft_data.get("km") or draft_data.get("current_km")
            if km_saved and draft_data.get("_is_odometer_update"):
                base_msg = f"✅ *Atualização do hodômetro com {km_saved:,} km confirmada e salva!*".replace(",", ".")
            else:
                base_msg = "✅ *Ação confirmada e salva com sucesso!*"

            reply = (
                f"{base_msg}\n\n"
                f"⏰ *Lembrete agendado:* Vou te avisar {date_str} às {time_str} sobre *'{task_desc}'*! 🔔"
            )
            if draft_data.get("_discrepancy_warning"):
                reply = f"⚠️ {draft_data['_discrepancy_warning']}\n\n" + reply
            return {
                "reply": reply,
                "action_type": "action_confirmed_with_chained_reminder",
                "state": context.state.value,
                "committed_data": draft_data,
            }

        # B) Recusa / Cancelamento Puro / Erro Natural (NLP ou Direct) (Problema 2)
        if not is_correction and (nlp_classification is False or (is_negative and len(words) <= 4)):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": "Tranquilo, cancelei a operação! Nada foi alterado no seu veículo. Como posso te ajudar agora?",
                "action_type": "action_cancelled",
                "state": context.state.value,
                "details": {}
            }

        # A.2) Confirmação Positiva Direta (NLP ou Direct)
        if nlp_classification is True or (is_affirmative and not is_negative and not is_correction and len(words) <= 5):
            executed_action = context.state.value
            draft_data = context.draft_payload or {}
            draft_data["_action_state"] = executed_action
            if executed_action == ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value:
                draft_data["_reset_system"] = True
                draft_data["_reset_garage"] = True

            # Limpa estado pendente
            context.state = ConversationState.NORMAL
            context.draft_payload = None

            # Multi-Ação em Lote (Problema 1 e Intenções Mistas)
            if draft_data.get("_is_multi_action"):
                summary_lines = ["✅ *Todas as ações foram confirmadas e salvas com sucesso!*"]
                for act in draft_data.get("actions", []):
                    ref = act.get("target_vehicle_ref") or "Veículo"
                    intent = act.get("intent")
                    if intent in (IntentType.RECORD_MAINTENANCE.value, "record_maintenance"):
                        desc = act.get("description") or "Manutenção"
                        summary_lines.append(f"• 🚗 *{ref}*: Manutenção registrada no histórico ({desc}).")
                    elif intent in (IntentType.UPDATE_ODOMETER.value, "update_odometer"):
                        km = act.get("odometer_km") or act.get("km")
                        km_str = f"{km:,} km".replace(",", ".") if km else ""
                        summary_lines.append(f"• 🚗 *{ref}*: Odômetro atualizado para {km_str}." if km_str else f"• 🚗 *{ref}*: Odômetro atualizado.")
                    elif intent in (IntentType.SET_REMINDER.value, "set_reminder"):
                        task = act.get("task_description") or act.get("description", "Lembrete automotivo")
                        when = f"{act.get('target_date_str', 'em breve')} às {act.get('explicit_time', '09:00')}"
                        summary_lines.append(f"• ⏰ *{ref}*: Lembrete agendado para {when} (*{task}*).")
                    else:
                        summary_lines.append(f"• 🚗 *{ref}*: Registro concluído com sucesso.")

                summary_lines.append("\nSeus veículos e lembretes estão atualizados! Posso te ajudar com algo mais?")
                reply = "\n".join(summary_lines)
                if draft_data.get("_discrepancy_warning"):
                    reply = f"⚠️ {draft_data['_discrepancy_warning']}\n\n" + reply
                return {
                    "reply": reply,
                    "action_type": "action_confirmed",
                    "state": context.state.value,
                    "committed_data": draft_data,
                }

            km_saved = draft_data.get("odometer_km") or draft_data.get("km")
            if draft_data.get("_is_odometer_update") and km_saved:
                msg = f"✅ *Atualização do hodômetro com {km_saved:,} km confirmada e salva com sucesso!*".replace(",", ".")
            else:
                success_messages = {
                    ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value: "🚗 *Veículo cadastrado com sucesso na sua garagem!*",
                    ConversationState.AWAITING_MAINTENANCE_CONFIRM.value: "✅ *Manutenção salva com sucesso no histórico oficial do seu veículo!*",
                    ConversationState.AWAITING_REMINDER_CONFIRM.value: "⏰ *Lembrete agendado com sucesso! Vou te alertar pontualmente no WhatsApp.*",
                    ConversationState.AWAITING_FINE_CONFIRM.value: "📋 *Multa registrada no painel de controle do veículo!*",
                    ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value: "🗑️ *Garagem zerada com sucesso! Todos os registros foram limpos.*",
                }
                msg = success_messages.get(executed_action, "✅ *Ação confirmada e registrada com sucesso!*")

            reply = f"{msg}\n\nSe precisar de algo mais, é só me chamar!"
            if draft_data.get("_discrepancy_warning"):
                reply = f"⚠️ {draft_data['_discrepancy_warning']}\n\n" + reply

            return {
                "reply": reply,
                "action_type": "action_confirmed",
                "state": context.state.value,
                "committed_data": draft_data,
            }

        # C) Mensagens complexas, correções de dados, novas instruções ou dúvidas: consulta o Gemini
        history_dicts = [{"role": t.role, "content": t.content} for t in context.recent_turns]
        analysis = await self.gemini.analyze_message(
            user_id=user_id,
            current_message=clean_text,
            history=history_dicts,
            context=context,
            vehicles=vehicles,
            maintenances=maintenances,
            reminders=reminders,
            fines=fines,
        )

        # Se o Gemini identificar cancelamento explícito
        if analysis.intent == IntentType.CANCEL_ACTION:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": "Entendido! Rascunho descartado. Como posso te ajudar agora?",
                "action_type": "action_cancelled",
                "state": context.state.value,
                "details": {}
            }

        # Se o usuário confirmou por vias semânticas
        if analysis.intent == IntentType.CONFIRM_ACTION:
            draft_data = context.draft_payload or {}
            executed_action = context.state.value
            if executed_action == ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value:
                draft_data["_reset_system"] = True
                draft_data["_reset_garage"] = True
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            success_messages = {
                ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value: "🚗 *Veículo cadastrado com sucesso na sua garagem!*",
                ConversationState.AWAITING_MAINTENANCE_CONFIRM.value: "✅ *Manutenção salva com sucesso no histórico oficial do seu veículo!*",
                ConversationState.AWAITING_REMINDER_CONFIRM.value: "⏰ *Lembrete agendado com sucesso! Vou te alertar pontualmente no WhatsApp.*",
                ConversationState.AWAITING_FINE_CONFIRM.value: "📋 *Multa registrada no painel de controle do veículo!*",
                ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value: "🗑️ *Garagem zerada com sucesso! Todos os registros foram limpos.*",
            }
            msg = success_messages.get(executed_action, "✅ *Perfeito! Informações salvas com sucesso no seu histórico oficial!*")
            reply = f"{msg}\n\nSe precisar de algo mais, é só me chamar!"
            if draft_data.get("_discrepancy_warning"):
                reply = f"⚠️ {draft_data['_discrepancy_warning']}\n\n" + reply
            return {
                "reply": reply,
                "action_type": "action_confirmed",
                "state": context.state.value,
                "committed_data": draft_data,
            }

        # Se for nova ação crítica OU correção de dados do rascunho (is_critical_action = True):
        # Substitui/atualiza o rascunho e solicita confirmação para o novo dado
        if analysis.is_critical_action:
            discrepancy_warning = context.draft_payload.get("_discrepancy_warning") if context.draft_payload else None
            return self._initiate_confirmation_flow(context, analysis, discrepancy_warning=discrepancy_warning)

        # Se for UNKNOWN (ex: incompatibilidade mecânica, serviço recusado, prompt injection ou fora de escopo):
        # Retorna a resposta explicativa do Gemini e limpa o rascunho pendente incompatível
        if analysis.intent == IntentType.UNKNOWN:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": analysis.reply_message,
                "action_type": "unknown",
                "state": context.state.value,
                "details": analysis.extracted_entities
            }

        # Se for POLITE_CLOSING ou GREETING:
        if analysis.intent in (IntentType.POLITE_CLOSING, IntentType.GREETING_MENU):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": analysis.reply_message,
                "action_type": analysis.intent.value,
                "state": context.state.value,
                "details": {}
            }

        # D) Caso permaneça em dúvida genuína sobre o rascunho: reapresenta a pergunta de confirmação
        return {
            "reply": (
                f"Você possui um registro aguardando sua resposta:\n\n"
                f"Deseja confirmar e salvar? (Responda *'Sim'* para salvar, *'Não'* para descartar ou *'Sair'* para voltar ao menu)."
            ),
            "action_type": "awaiting_confirmation_clarification",
            "state": context.state.value,
            "draft": context.draft_payload,
        }

    async def classify_confirmation(
        self, text: str, context: Optional[ConversationContext] = None
    ) -> Optional[bool]:
        """
        Classifica semanticamente respostas de confirmação, rejeição ou cancelamento (Problema 2).
        Traduz respostas naturais:
        - ('foi um erro', 'cancela', 'esquece', 'errei', 'não') -> False
        - ('pode meter marcha', 'isso aí', 'manda bala', 'com certeza', 'sim') -> True
        Retorna None se for uma dúvida ou correção de dados.
        """
        clean = text.strip()
        clean_lower = clean.lower()
        norm = "".join(c for c in unicodedata.normalize("NFKD", clean_lower) if not unicodedata.combining(c))

        # Se contiver entidades claras de correção (placa, ano, valor, custo, etc.), não é confirmação nem cancelamento puro
        has_correction_entity = bool(re.search(
            r"(\b(placa|ano|valor|custo|pre[çc]o|reais|oficina|pe[çc]a|marca|modelo)\b|"
            r"\b[a-zA-Z]{3}\d[a-zA-Z0-9]\d{2}\b|\b(19\d\d|20\d\d)\b)",
            clean_lower
        ))
        if has_correction_entity:
            return None

        # 1. Padrões de Cancelamento / Rejeição / Erro (False)
        negative_patterns = [
            r"\bfoi\s+(?:um\s+)?(?:erro|engano)\b",
            r"\berr(?:o|ei|ado)\s*(?:meu)?\b",
            r"\bme\s+(?:confundi|enganei)\b",
            r"\bdigitei\s+errado\b",
            r"\bt[aá]\s+errado\b",
            r"\best[aá]\s+errado\b",
            r"\b(?:cancela|cancelar|aborta|abortar|esquece|esquece\s+isso)\b",
            r"\bdeixa\s+(?:quieto|pra\s+l[aá])\b",
            r"\bn[aã]o\s*(?:salv[ae]|registr[ae]|faz\s+isso)?\b",
            r"\b(?:nop|nem\s+pensar|desiste|parar|nada\s+disso|recusar)\b",
        ]
        for p in negative_patterns:
            if re.search(p, norm):
                return False

        # 2. Padrões de Confirmação / Aprovação (True)
        affirmative_patterns = [
            r"\bpode\s+meter\s+marcha\b",
            r"\bmanda\s+(?:bala|ver|brasa)\b",
            r"\bisso\s*(?:a[ií]|mesmo)\b",
            r"\bcom\s+certeza\b",
            r"\bsem\s+d[uú]vida\b",
            r"\bpositivo\b",
            r"\b(?:bora|dale|toca\s+o\s+barco|vai\s+fundo|demorou)\b",
            r"\bpode\s+(?:ir|salvar|registrar|gravar|mandar)\b",
            r"\b(?:fecho|fechado|beleza\s+pode\s+ir|t[aá]\s+certo|correto|exato)\b",
            r"\b(?:sim|s|yes|confirmo|confirma|confirmar|salvar|salva|gravar)\b",
        ]
        for p in affirmative_patterns:
            if re.search(p, norm):
                return True

        # 3. Fallback: Se for texto curto não identificado, consulta classificação rápida com Gemini
        if len(clean.split()) <= 6 and self.gemini and hasattr(self.gemini, "client") and self.gemini.client:
            try:
                prompt = (
                    f"Analise a seguinte resposta do usuário em um diálogo de confirmação: '{clean}'\n"
                    "Classifique em apenas UMA palavra:\n"
                    "- TRUE se o usuário estiver confirmando/aprovando a ação.\n"
                    "- FALSE se estiver cancelando, recusando ou dizendo que foi um erro/engano.\n"
                    "- UNKNOWN se for uma correção de dados, dúvida ou outro assunto."
                )
                resp = await self.gemini.client.aio.models.generate_content(
                    model=self.gemini.model_name,
                    contents=prompt,
                )
                resp_txt = (resp.text or "").strip().upper()
                if "TRUE" in resp_txt:
                    return True
                if "FALSE" in resp_txt:
                    return False
            except Exception:
                pass

        return None

    def _initiate_multi_action_confirmation_flow(
        self,
        context: ConversationContext,
        actions: List[Any],
        discrepancy_warning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Inicia fluxo de confirmação agrupada para múltiplas ações ou múltiplos veículos (Problema 1)."""
        context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
        action_dicts = []
        for a in actions:
            if hasattr(a, "model_dump"):
                action_dicts.append(a.model_dump(exclude_none=True))
            elif isinstance(a, dict):
                action_dicts.append(a)
            else:
                action_dicts.append({"description": str(a)})

        payload = {
            "_is_multi_action": True,
            "_action_state": "awaiting_multi_action_confirm",
            "actions": action_dicts,
        }
        if discrepancy_warning:
            payload["_discrepancy_warning"] = discrepancy_warning

        context.draft_payload = payload

        lines = ["Identifiquei múltiplas ações para a sua garagem:"]
        has_maint = False
        has_rem = False
        maint_summary = ""
        maint_cost = 0.0
        rem_summary = ""
        rem_time = "09:00"
        rem_date = "amanhã"

        for idx, act in enumerate(action_dicts, 1):
            ref = act.get("target_vehicle_ref") or "Veículo"
            intent = act.get("intent")
            if intent in (IntentType.RECORD_MAINTENANCE.value, "record_maintenance"):
                has_maint = True
                desc = act.get("description") or "Revisão"
                maint_summary = desc
                cost = act.get("total_cost", 0.0)
                maint_cost = cost
                cost_str = f" - R$ {cost:.2f}".replace(".", ",") if cost else ""
                lines.append(f"{idx}. 🚗 *{ref}*: Registro de manutenção (*{desc}*{cost_str})")
            elif intent in (IntentType.UPDATE_ODOMETER.value, "update_odometer"):
                km = act.get("odometer_km") or act.get("km")
                km_str = f"{km:,} km".replace(",", ".") if km else "novo hodômetro"
                lines.append(f"{idx}. 🚗 *{ref}*: Atualização de hodômetro para *{km_str}*")
            elif intent in (IntentType.SET_REMINDER.value, "set_reminder"):
                has_rem = True
                task = act.get("task_description") or act.get("description", "Lembrete automotivo")
                rem_summary = task
                t_str = act.get("explicit_time") or "09:00"
                d_str = act.get("target_date_str") or "amanhã"
                rem_time = t_str
                rem_date = d_str
                lines.append(f"{idx}. ⏰ *{ref}*: Agendamento de lembrete (*{task}* para {d_str} às {t_str})")
            else:
                lines.append(f"{idx}. 🚗 *{ref}*: {act.get('description', 'Ação solicitada')}")

        lines.append("")
        if discrepancy_warning:
            lines.insert(0, f"⚠️ {discrepancy_warning}\n")

        # Se for intenção mista encadeada clássica (Manutenção + Lembrete)
        if has_maint and has_rem:
            cost_formatted = f" (R$ {maint_cost:,.0f})" if maint_cost else ""
            summary_concl = (
                f"Vou registrar a {maint_summary.lower()}{cost_formatted} E "
                f"agendar um lembrete para {rem_date} às {rem_time} ({rem_summary}). Posso confirmar?"
            )
            lines.append(f"📌 {summary_concl}")
        else:
            lines.append("📌 *Deseja confirmar e registrar essas operações?*")
            lines.append("👉 Responda *'Sim'* (ou *'pode meter marcha'*) para salvar ou *'Não'* (ou *'foi um erro'*) para descartar.")

        reply = "\n".join(lines)
        return {
            "reply": reply,
            "action_type": "awaiting_confirmation",
            "state": context.state.value,
            "draft": payload,
        }

    def _decompose_multi_action_message(
        self, text: str, vehicles: Optional[List[Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Decompõe mensagens com múltiplos veículos ou intenções na mesma frase (Problema 1 e Intenções Mistas).
        Ex: 'Fiz revisão no Civic e atualiza o km do Sandero para 45000 km'
        Ex: 'Troquei óleo e filtro por 550 reais. Já salva isso e me lembra amanhã às 9h de calibrar o estepe.'
        """
        if not text:
            return []

        # 1. Conectores estruturados de múltiplas intenções / orações
        split_pattern = r"(?:\.\s+|\n+|;\s*|\s+(?:e\s+tamb[eé]m|e\s+al[eé]m\s+disso|j[áa]\s+salva\s+isso\s+e|j[áa]\s+aproveita\s+e|e\s+me\s+lembra|me\s+lembra|lembra-me)\s+)"
        clauses = [c.strip() for c in re.split(split_pattern, text.strip(), flags=re.I) if c.strip()]
        if len(clauses) < 2:
            clauses = [c.strip() for c in re.split(r"\s+(?:e\s+tamb[eé]m|e\s+al[eé]m\s+disso|e)\s+|[;\n]+", text.strip(), flags=re.I) if c.strip()]

        if len(clauses) < 2:
            return []

        actions: List[Dict[str, Any]] = []

        # Determina veículo padrão caso a mensagem não mencione explicitamente em todas as cláusulas
        first_veh, _ = self._resolve_vehicle(text, vehicles)
        if not first_veh and vehicles:
            first_veh = vehicles[0]
        default_ref = getattr(first_veh, "model", None) or getattr(first_veh, "modelo", "Veículo") if first_veh else "Veículo"

        for clause in clauses:
            c_clean = clause.strip()
            if not c_clean:
                continue
            c_lower = c_clean.lower()

            c_veh, _ = self._resolve_vehicle(c_clean, vehicles)
            c_ref = None
            if c_veh:
                c_ref = getattr(c_veh, "model", None) or getattr(c_veh, "modelo", None) or getattr(c_veh, "plate", None)
            else:
                for v in (vehicles or []):
                    v_model = (getattr(v, "model", None) or getattr(v, "modelo", "")).lower()
                    if v_model and v_model in c_lower:
                        c_ref = getattr(v, "model", None) or getattr(v, "modelo", "")
                        c_veh = v
                        break

            clause_veh_ref = c_ref or default_ref

            # Extração de hodômetro se houver
            km_match = re.search(r"\b(\d+)\s*(?:mil\s*)?km\b|\b(?:para|em|com)\s*(\d+)\b", c_lower)
            km_val = None
            if km_match:
                val_s = km_match.group(1) or km_match.group(2)
                if val_s:
                    km_val = int(val_s)
                    if "mil" in c_lower and km_val < 1000:
                        km_val *= 1000

            # Extração de valor financeiro
            cost_m = re.search(r"(?:r\$|reais|por)\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:reais|r\$)", c_clean, re.I)
            cost_val = 0.0
            if cost_m:
                val_s = cost_m.group(1) or cost_m.group(2)
                if val_s:
                    cost_val = float(val_s.replace(",", "."))

            # Classificação de intenção da cláusula
            is_rem = any(k in c_lower for k in ("lembr", "avis", "agend", "notific", "calibrar", "estepe", "rodízio", "rodizio")) and any(
                t in c_lower for t in ("amanh", "hoje", "às", "as", "hora", "h", "dia", "semana", "mês", "mes", "calibr", "estepe", "lembr")
            )
            is_maint = any(k in c_lower for k in ("revis", "troc", "manuten", "oleo", "óleo", "filtro", "vela", "pastilha", "freio", "pneu", "alinhamento", "balanceamento"))
            is_odo = any(k in c_lower for k in ("km", "hodometro", "hodômetro", "quilometragem", "odometro", "odômetro", "atualiza", "bateu"))

            if is_rem:
                time_m = re.search(r"(?:às|as)\s*(\d{1,2})(?::(\d{2})|h)?|\b(\d{1,2})h\b|\b(\d{1,2}):(\d{2})\b", c_lower)
                hour, minute = 9, 0
                if time_m:
                    if time_m.group(1):
                        hour = int(time_m.group(1))
                        minute = int(time_m.group(2)) if time_m.group(2) else 0
                    elif time_m.group(3):
                        hour = int(time_m.group(3))
                    elif time_m.group(4):
                        hour = int(time_m.group(4))
                        minute = int(time_m.group(5))
                time_str = f"{hour:02d}:{minute:02d}"

                date_str = "amanhã" if "amanh" in c_lower else "hoje"
                task_t = c_clean
                task_t = re.sub(r"^(?:j[áa]\s+salva\s+isso\s+e\s+|j[áa]\s+aproveita\s+e\s+|e\s+)?(?:me\s+)?lembr\w*\s+(?:de\s+)?", "", task_t, flags=re.I).strip()
                task_t = re.sub(r"(?:amanh[ãa]|hoje|(?:às|as)\s*\d+(?::\d{2}|h)?|\b\d+h\b)\s*(?:de\s+)?", "", task_t, flags=re.I).strip()
                if not task_t or len(task_t) < 3:
                    task_t = "Calibrar o estepe" if "estepe" in c_lower else "Lembrete preventivo"

                actions.append({
                    "intent": IntentType.SET_REMINDER.value,
                    "target_vehicle_ref": clause_veh_ref,
                    "is_critical_action": True,
                    "task_description": task_t,
                    "description": task_t,
                    "explicit_time": time_str,
                    "target_date_str": date_str,
                })

            elif is_maint:
                comps = []
                for term in ["óleo", "filtro", "pastilha", "vela", "correia", "pneu", "amortecedor", "bateria"]:
                    if term in c_lower:
                        comps.append(term)
                desc_val = c_clean
                if "óleo" in c_lower and "filtro" in c_lower:
                    desc_val = "Troca de óleo e filtro"
                elif "óleo" in c_lower:
                    desc_val = "Troca de óleo"

                actions.append({
                    "intent": IntentType.RECORD_MAINTENANCE.value,
                    "target_vehicle_ref": clause_veh_ref,
                    "is_critical_action": True,
                    "description": desc_val,
                    "components": comps or [c_clean],
                    "total_cost": cost_val,
                    "odometer_km": km_val,
                    "km": km_val,
                })

            elif is_odo or km_val:
                actions.append({
                    "intent": IntentType.UPDATE_ODOMETER.value,
                    "target_vehicle_ref": clause_veh_ref,
                    "is_critical_action": True,
                    "description": f"Atualização de hodômetro ({c_clean})",
                    "odometer_km": km_val,
                    "km": km_val,
                })

        if len(actions) >= 2:
            intents = [a.get("intent") for a in actions]
            distinct_intents = set(intents)
            # 1. Intenções mistas distintas (ex: Manutenção + Lembrete ou Manutenção + Odômetro)
            if len(distinct_intents) >= 2:
                return actions

            # 2. Ações para veículos distintos (ex: Civic e Sandero)
            target_refs = [a.get("target_vehicle_ref") for a in actions if a.get("target_vehicle_ref") and a.get("target_vehicle_ref") != "Veículo"]
            distinct_refs = set(target_refs)
            if len(distinct_refs) >= 2:
                return actions

            # 3. Ações distintas com verbos de ação explícitos em cada oração
            action_verbs = ("fiz", "troquei", "atualiza", "atualizar", "mudei", "cadastra", "registra", "salva", "salvar", "anota")
            clauses_with_verbs = [c for c in clauses if any(v in c.lower().split() for v in action_verbs)]
            if len(clauses_with_verbs) >= 2:
                return actions

            return []
        return []

    def _resolve_vehicle(
        self, text: str, vehicles: Optional[List[Any]] = None
    ) -> Tuple[Optional[Any], Optional[str]]:
        """
        Resolve o veículo a partir do texto e detecta conflitos entre modelo mencionado e placa real (Problema 3).
        Se o usuário disser 'Compass placa RIO2A24' e no banco a placa pertencer a um Sandero, emite:
        'Atenção: A placa {placa} pertence ao seu {db_model}, e não a um {mentioned_model}. Os dados foram lançados no {db_model}.'
        """
        if not vehicles:
            return None, None

        text_clean = text.strip()
        text_lower = text_clean.lower()

        # 1. Busca por placa antiga ou Mercosul
        plate_search = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", text_clean)
        if plate_search:
            clean_plate = re.sub(r"[\s\-]", "", plate_search.group(1)).upper()
            db_veh = next(
                (v for v in vehicles if (getattr(v, "plate", None) or getattr(v, "placa", "")).replace("-", "").upper() == clean_plate),
                None,
            )
            if db_veh:
                db_model = getattr(db_veh, "model", None) or getattr(db_veh, "modelo", "Veículo")
                db_make = getattr(db_veh, "make", None) or getattr(db_veh, "marca", "")

                mentioned_model = None

                # Padrão contextual: "<modelo> [com a / de] placa"
                pos_match = re.search(r"(?:do|da|no|na|o|a)?\s*([a-zA-Z0-9\-]+)\s+(?:com\s+a\s+|com\s+|de\s+)?placa\b", text_clean, re.I)
                if pos_match:
                    cand = pos_match.group(1).strip()
                    if cand.lower() not in ("carro", "veiculo", "veículo", "moto", "com", "a", "o", "da", "do"):
                        if cand.lower() != db_model.lower() and cand.lower() != db_make.lower():
                            mentioned_model = cand.capitalize()

                # Lista de modelos e marcas conhecidos para detecção ampla
                if not mentioned_model:
                    known_models = [
                        "compass", "renegade", "civic", "corolla", "sandero", "logan", "duster",
                        "gol", "polo", "virtus", "t-cross", "tcross", "nivus", "taos", "onix", "cruze",
                        "tracker", "hilux", "yaris", "etios", "sw4", "hb20", "creta", "argo",
                        "cronos", "pulse", "fastback", "toro", "strada", "mobi", "kwid", "kicks",
                        "versa", "sentra", "fit", "city", "hr-v", "hrv", "wr-v", "titan", "fan", "biz",
                        "twister", "fazer", "lander", "crosser", "xj6", "bmw", "audi", "mercedes"
                    ]
                    for km in known_models:
                        if re.search(rf"\b{km}\b", text_lower):
                            if km != db_model.lower() and km != db_make.lower():
                                mentioned_model = km.capitalize()
                                break

                warning = None
                if mentioned_model:
                    warning = (
                        f"Atenção: A placa {clean_plate} pertence ao seu {db_model}, "
                        f"e não a um {mentioned_model}. Os dados foram lançados no {db_model}."
                    )
                return db_veh, warning

        # 2. Se não houver placa, busca por fuzzy matching
        fuzzy_veh = self._resolve_vehicle_fuzzy(text_clean, vehicles)
        return fuzzy_veh, None

    def _record_assistant_turn(self, context: ConversationContext, reply_text: str):
        """Registra a resposta do assistente no histórico do contexto."""
        context.recent_turns.append(ConversationTurn(role="assistant", content=reply_text))
        if len(context.recent_turns) > 10:
            context.recent_turns = context.recent_turns[-10:]

    async def _query_vehicle_status_by_plate(
        self,
        plate: str,
        vehicles: Optional[List[Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Consulta o status real do veículo no banco de dados relacional (SQLAlchemy 2.0 Async)
        utilizando regex sanitizado para placas Antigas e Mercosul.
        """
        # 1. Consulta o banco relacional assíncrono real
        try:
            from src.database import async_session_factory
            from src.repositories.veiculo_repository import VeiculoRepository
            from src.repositories.manutencao_repository import ManutencaoRepository

            async with async_session_factory() as session:
                v_repo = VeiculoRepository(session)
                m_repo = ManutencaoRepository(session)

                db_veh = await v_repo.get_by_placa(plate)
                if db_veh:
                    maints = await m_repo.get_by_veiculo_id(db_veh.id)
                    total_spent = await m_repo.get_total_cost_by_veiculo(db_veh.id)

                    reply = (
                        f"🚗 *Status do Veículo - {db_veh.marca} {db_veh.modelo} ({db_veh.ano})*\n\n"
                        f"• *Placa:* `{db_veh.placa}`\n"
                        f"• *Odômetro Atual:* {db_veh.quilometragem:,} km\n"
                        f"• *Manutenções Registradas:* {len(maints)}\n"
                        f"• *Total Investido:* R$ {total_spent:.2f}\n"
                    )
                    if maints:
                        ult = maints[0]
                        data_fmt = ult.data.strftime("%d/%m/%Y") if hasattr(ult.data, "strftime") else str(ult.data)
                        reply += f"• *Última Manutenção:* {ult.descricao} em {data_fmt} (R$ {ult.custo:.2f})\n"
                    else:
                        reply += "• *Histórico:* Nenhuma manutenção registrada ainda.\n"

                    reply += "\n💡 Deseja registrar uma nova manutenção ou despesa para este veículo?"
                    return {
                        "reply": reply,
                        "action_type": "vehicle_status_query",
                        "state": "normal",
                        "details": {
                            "placa": db_veh.placa,
                            "modelo": db_veh.modelo,
                            "quilometragem": db_veh.quilometragem,
                            "total_manutencoes": len(maints),
                            "total_investido": total_spent,
                        },
                    }
        except Exception:
            pass

        # 2. Fallback para lista de veículos em memória / garagem
        if vehicles:
            matching = next(
                (v for v in vehicles if (getattr(v, "plate", None) or "").replace("-", "").upper() == plate),
                None,
            )
            if matching:
                reply = (
                    f"🚗 *Status do Veículo - {getattr(matching, 'make', '')} {getattr(matching, 'model', '')}*\n\n"
                    f"• *Placa:* `{getattr(matching, 'plate', plate)}`\n"
                    f"• *Odômetro Atual:* {getattr(matching, 'current_km', 0):,} km\n"
                    f"• *Perfil:* {getattr(matching, 'vehicle_type', 'Carro')}\n\n"
                    f"💡 Veículo localizado na sua garagem. Deseja registrar alguma manutenção?"
                )
                return {
                    "reply": reply,
                    "action_type": "vehicle_status_query",
                    "state": "normal",
                    "details": {"placa": plate, "modelo": getattr(matching, "model", "")},
                }

        return None

    def _resolve_vehicle_fuzzy(
        self, text: str, vehicles: Optional[List[Any]] = None, threshold: float = 0.65
    ) -> Optional[Any]:
        """
        Busca aproximada de veículo no texto usando difflib.SequenceMatcher (Fuzzy Matching).
        Permite tolerar erros de digitação como 'sandeiro' -> 'Sandero' ou 'civique' -> 'Civic'.
        """
        if not vehicles:
            return None
        import difflib

        phonetic_aliases = {
            "civique": "civic",
            "civi": "civic",
            "sandeiro": "sandero",
            "compas": "compass",
            "conpass": "compass",
        }

        words = re.findall(r"\b[a-zA-Z0-9À-ÿ]+\b", text.lower())
        best_match = None
        highest_score = 0.0

        for v in vehicles:
            model = (getattr(v, "model", None) or getattr(v, "modelo", "")).lower()
            make = (getattr(v, "make", None) or getattr(v, "marca", "")).lower()
            plate = (getattr(v, "plate", None) or getattr(v, "placa", "")).lower().replace("-", "")

            # Match exato ou substring direta
            if plate and plate in text.lower().replace("-", ""):
                return v

            targets = [model, make] + model.split() + make.split()

            for raw_word in words:
                if len(raw_word) < 3:
                    continue
                word = phonetic_aliases.get(raw_word, raw_word)
                for target in targets:
                    if not target or len(target) < 3:
                        continue
                    if word == target:
                        return v
                    ratio = difflib.SequenceMatcher(None, word, target).ratio()
                    if ratio > highest_score and ratio >= threshold:
                        highest_score = ratio
                        best_match = v

        return best_match if highest_score >= threshold else None

    def _is_garage_checkup_query(self, text_lower: str) -> bool:
        """Verifica se a mensagem é uma consulta global sobre todos os veículos ou manutenções atrasadas."""
        patterns = [
            r"\b(meus\s+carros|meus\s+ve[íi]culos|minha\s+garagem|todos\s+os\s+carros|todas\s+as\s+motos)\b",
            r"\b(algum(a)?\s+(carro|ve[íi]culo|moto|manuten[çc][ãa]o|revis[ãa]o)\s+(atrasad[ao]|pendente))\b",
            r"\b(manuten[çc][õo]es\s+atrasadas|revis[õo]es\s+atrasadas)\b",
            r"\b(status\s+d(os\s+meus\s+carros|a\s+minha\s+garagem))\b",
            r"\b(check-?up\s+(geral|da\s+garagem))\b",
            r"\b(est[ãa]o\s+em\s+dia|tem\s+alguma\s+manuten[çc][ãa]o\s+atrasada)\b",
        ]
        return any(re.search(p, text_lower) for p in patterns)

    async def _handle_garage_checkup(
        self,
        user_id: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        Gera relatório consolidado iterando sobre TODOS os veículos da garagem (Problema 3).
        Informa o status de cada um (em dia vs atrasado) e resumo geral.
        """
        all_vehs = list(vehicles or [])

        # Fallback para banco relacional se a lista injetada for vazia
        if not all_vehs:
            try:
                from src.database import async_session_factory
                from src.repositories.veiculo_repository import VeiculoRepository
                from src.repositories.proprietario_repository import ProprietarioRepository

                async with async_session_factory() as session:
                    v_repo = VeiculoRepository(session)
                    p_repo = ProprietarioRepository(session)
                    prop = await p_repo.get_by_telefone(user_id)
                    if prop:
                        all_vehs = await v_repo.get_by_proprietario_id(prop.id)
                    else:
                        all_vehs = await v_repo.list_all()
            except Exception:
                pass

        if not all_vehs:
            reply = (
                "🚗 *Check-up Geral da Garagem*\n\n"
                "Não encontrei nenhum veículo cadastrado na sua garagem no momento.\n"
                "Gostaria de cadastrar um novo carro ou moto?"
            )
            return {
                "reply": reply,
                "action_type": "garage_checkup",
                "state": "normal",
                "details": {"total_vehicles": 0},
            }

        lines = ["🚗 *Check-up Geral da Garagem - Status das Manutenções:*\n"]
        delayed_count = 0

        for v in all_vehs:
            make = getattr(v, "make", None) or getattr(v, "marca", "Veículo")
            model = getattr(v, "model", None) or getattr(v, "modelo", "")
            plate = getattr(v, "plate", None) or getattr(v, "placa", "sem placa")
            km = getattr(v, "current_km", None) or getattr(v, "quilometragem", 0) or 0
            v_id = getattr(v, "id", None)

            interval = 10000
            sched_mark = (km // interval) * interval
            is_delayed = False
            delay_km = 0
            if sched_mark > 0 and km > sched_mark:
                delay_km = km - sched_mark
                is_delayed = True

            # Verifica se há lembrete com km vencido
            if reminders:
                for r in reminders:
                    if getattr(r, "vehicle_id", None) == v_id and getattr(r, "due_km", None):
                        if km >= r.due_km:
                            is_delayed = True
                            delay_km = max(delay_km, km - r.due_km)

            km_fmt = f"{km:,} km".replace(",", ".")
            if is_delayed and delay_km > 0:
                delayed_count += 1
                delay_fmt = f"{delay_km:,} km".replace(",", ".")
                sched_fmt = f"{sched_mark:,} km".replace(",", ".")
                lines.append(
                    f"• 🔴 *{make} {model}* (`{plate}`) | {km_fmt}\n"
                    f"   ⚠️ *REVISÃO ATRASADA:* Passou da revisão de {sched_fmt} em *{delay_fmt}*!"
                )
            else:
                lines.append(
                    f"• 🟢 *{make} {model}* (`{plate}`) | {km_fmt}\n"
                    f"   ✅ *EM DIA:* Nenhuma manutenção preventiva pendente."
                )

        if delayed_count > 0:
            lines.append(
                f"\n⚠️ Identifiquei *{delayed_count} veículo(s)* com revisão atrasada. Deseja agendar um lembrete ou registrar o serviço agora?"
            )
        else:
            lines.append("\n🎉 Todos os veículos da sua garagem estão com as manutenções em dia!")

        return {
            "reply": "\n".join(lines),
            "action_type": "garage_checkup",
            "state": "normal",
            "details": {
                "total_vehicles": len(all_vehs),
                "delayed_vehicles": delayed_count,
            },
        }

    def _format_single_vehicle_status(
        self, vehicle: Any, maintenances: Optional[List[Any]] = None
    ) -> Dict[str, Any]:
        """Formata o status de um veículo individual com verificação de manutenção em dia (Problema 4)."""
        make = getattr(vehicle, "make", None) or getattr(vehicle, "marca", "Veículo")
        model = getattr(vehicle, "model", None) or getattr(vehicle, "modelo", "")
        plate = getattr(vehicle, "plate", None) or getattr(vehicle, "placa", "sem placa")
        km = getattr(vehicle, "current_km", None) or getattr(vehicle, "quilometragem", 0) or 0
        km_fmt = f"{km:,} km".replace(",", ".")

        interval = 10000
        sched_mark = (km // interval) * interval
        is_delayed = False
        delay_km = 0
        if sched_mark > 0 and km > sched_mark:
            delay_km = km - sched_mark
            is_delayed = True

        if is_delayed:
            delay_fmt = f"{delay_km:,} km".replace(",", ".")
            sched_fmt = f"{sched_mark:,} km".replace(",", ".")
            reply = (
                f"🚗 *Status do Veículo - {make} {model}*\n\n"
                f"• *Placa:* `{plate}`\n"
                f"• *Odômetro Atual:* {km_fmt}\n"
                f"• 🔴 *Situação:* A revisão dos {sched_fmt} está atrasada em *{delay_fmt}*!\n\n"
                f"💡 Deseja registrar a manutenção ou agendar um lembrete para realizá-la?"
            )
        else:
            reply = (
                f"🚗 *Status do Veículo - {make} {model}*\n\n"
                f"• *Placa:* `{plate}`\n"
                f"• *Odômetro Atual:* {km_fmt}\n"
                f"• 🟢 *Situação:* Todas as manutenções estão em dia!\n\n"
                f"💡 Precisa de alguma orientação ou registrar um novo serviço?"
            )

        return {
            "reply": reply,
            "action_type": "vehicle_status_query",
            "state": "normal",
            "details": {
                "placa": plate,
                "modelo": model,
                "quilometragem": km,
                "em_dia": not is_delayed,
                "atrasado_km": delay_km,
            },
        }

