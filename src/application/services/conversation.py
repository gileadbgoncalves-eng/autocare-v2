"""
AutoCare V2 - Orquestrador de Conversação e Máquina de Estados (Arquitetura 100% AI-First)
Atua estritamente como um orquestrador enxuto de estado entre o usuário, o Google Gemini e o banco de dados:
1. Controle de concorrência, limite de taxa e carregamento de contexto (garagem + rascunho ativo com TTL + últimos 5 turnos).
2. Chamada ao Gemini (`src/ai/client.py`) repassando a mensagem do usuário e o contexto serializado.
3. Execução mecânica no banco/estado das `tool_calls` e intenções retornadas no JSON estruturado do Gemini.
4. Devolução ao usuário da exata mensagem (`reply_message`) gerada pelo Gemini, sem templates ou interceptadores determinísticos.
"""

import asyncio
import difflib
import logging
import re
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

from src.domain.enums import (
    IntentType,
    ConversationState,
    VehicleType,
    FuelType,
)
from src.domain.entities import Vehicle, ConversationContext, ConversationTurn
from src.ai.client import GeminiClient
from src.ai.schemas import GeminiAnalysisResponse, ToolCallAction
from src.application.services.parsers import is_undo_command

logger = logging.getLogger("autocare.conversation")

DRAFT_TTL_MINUTES = 30
DRAFT_EXPIRED_REPLY = (
    "Aquele seu registro expirou por tempo inativo (passaram mais de 30 minutos). "
    "Pode me repetir o que foi feito para eu registrar certinho?"
)
UNDO_SUCCESS_REPLY = "✅ Pronto! O último registro que você confirmou foi desfeito e apagado do seu histórico."
UNDO_NOTHING_REPLY = "Não encontrei nenhum registro recente (nos últimos 5 minutos) para desfazer."
UNDO_DRAFT_DISCARDED_REPLY = "Tranquilo, descartei o rascunho pendente! Nada foi salvo na sua garagem."


class _TurnLock:
    """
    Wrapper de posse sobre `asyncio.Lock` para um único turno.
    - `async with _TurnLock(lock)`: adquire na entrada, libera na saída.
    - `released()`: cede o lock durante a chamada assíncrona ao LLM e o readquire depois.
    """

    def __init__(self, lock: asyncio.Lock):
        self._lock = lock
        self._held = False

    async def __aenter__(self) -> "_TurnLock":
        await self._lock.acquire()
        self._held = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._held:
            self._held = False
            self._lock.release()

    def released(self):
        @asynccontextmanager
        async def _cm():
            if self._held:
                self._held = False
                self._lock.release()
            try:
                yield
            finally:
                await self._lock.acquire()
                self._held = True

        return _cm()


class ConversationService:
    """
    Orquestrador enxuto de conversação e gerenciamento de estado (100% AI-First).
    Toda extração de entidades, inferência automotiva, confirmação, cancelamento,
    correção de rascunho e geração de resposta pertencem exclusivamente ao Google Gemini.
    """

    def __init__(self, gemini_client: Optional[GeminiClient] = None, draft_ttl_minutes: Optional[int] = None):
        self.gemini = gemini_client or GeminiClient()
        self.draft_ttl_minutes = draft_ttl_minutes or DRAFT_TTL_MINUTES
        self._contexts: Dict[str, ConversationContext] = {}
        self._user_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        return lock

    def get_context(self, user_id: str) -> ConversationContext:
        if user_id not in self._contexts:
            self._contexts[user_id] = ConversationContext(user_id=user_id)
        return self._contexts[user_id]

    def get_or_create_context(self, user_id: str) -> ConversationContext:
        return self.get_context(user_id)

    def update_context(self, context: ConversationContext) -> None:
        self._contexts[context.user_id] = context

    def _utcnow(self) -> datetime:
        """Carimbo de tempo canônico UTC-aware."""
        return datetime.now(timezone.utc)

    def _is_draft_expired(self, context: ConversationContext) -> bool:
        if not context.is_awaiting_confirmation() or not context.draft_updated_at:
            return False
        now_utc = self._utcnow()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)
        draft_ts = context.draft_updated_at
        if draft_ts.tzinfo is None:
            draft_ts = draft_ts.replace(tzinfo=timezone.utc)
        else:
            draft_ts = draft_ts.astimezone(timezone.utc)
        return (now_utc - draft_ts) > timedelta(minutes=self.draft_ttl_minutes)

    def reopen_draft_for_correction(
        self,
        user_id: str,
        committed_data: Dict[str, Any],
        clear_keys: Tuple[str, ...] = ("data", "date_str", "date"),
    ) -> None:
        """Reabre o rascunho cuja persistência falhou por dado inválido, removendo apenas as chaves problemáticas."""
        if not committed_data:
            return
        ctx = self.get_context(user_id)
        state_value = committed_data.get("_action_state") or ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
        try:
            ctx.state = ConversationState(state_value)
        except ValueError:
            ctx.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
        ctx.draft_payload = {
            k: v for k, v in committed_data.items() if k != "_action_state" and k not in clear_keys
        }
        ctx.draft_updated_at = self._utcnow()
        self.update_context(ctx)

    def replace_last_assistant_turn(self, user_id: str, new_reply: str) -> None:
        ctx = self.get_context(user_id)
        if ctx.recent_turns and ctx.recent_turns[-1].role == "assistant":
            ctx.recent_turns[-1].content = new_reply
            self.update_context(ctx)

    def clear_context(self, user_id: str) -> None:
        self._contexts.pop(user_id, None)

    def _record_assistant_turn(self, context: ConversationContext, reply: str) -> None:
        context.recent_turns.append(ConversationTurn(role="assistant", content=reply))
        max_t = getattr(context, "max_turns", 50)
        if len(context.recent_turns) > max_t:
            context.recent_turns = context.recent_turns[-max_t:]

    def _resolve_vehicle(
        self,
        target_ref: Optional[str],
        vehicles: List[Vehicle],
        message: str = "",
    ) -> Tuple[Optional[Vehicle], List[Vehicle]]:
        """
        Localiza o veículo alvo na garagem do usuário.
        Retorna `(veiculo_unico, lista_de_candidatos_ambiguos)`.
        """
        if not vehicles:
            return None, []

        search_text = f"{target_ref or ''} {message}".lower()

        # 1. Busca exata por placa
        for v in vehicles:
            v_plate = getattr(v, "plate", None) or getattr(v, "placa", None)
            if v_plate and str(v_plate).lower().replace("-", "") in search_text.replace("-", ""):
                return v, []

        if len(vehicles) == 1:
            return vehicles[0], []

        # Se target_ref foi informado explicitamente, tenta casar primeiro apenas contra target_ref
        if target_ref and target_ref.strip():
            ref_lower = target_ref.strip().lower()
            ref_matched: List[Vehicle] = []
            for v in vehicles:
                v_model = str(getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
                v_make = str(getattr(v, "make", None) or getattr(v, "marca", "") or "").lower()
                model_tokens = [tok for tok in v_model.split() if len(tok) >= 2]
                if any(re.search(rf"\b{re.escape(tok)}\b", ref_lower) for tok in model_tokens) or (
                    len(v_make) >= 3 and re.search(rf"\b{re.escape(v_make)}\b", ref_lower)
                ):
                    ref_matched.append(v)
            if len(ref_matched) == 1:
                return ref_matched[0], []
            if len(ref_matched) > 1:
                for v in ref_matched:
                    v_year = getattr(v, "year", None)
                    if v_year and str(v_year) in search_text:
                        return v, []
                return None, ref_matched

        # 2. Busca por modelo ou marca no texto completo
        matched: List[Vehicle] = []
        for v in vehicles:
            v_model = str(getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
            v_make = str(getattr(v, "make", None) or getattr(v, "marca", "") or "").lower()
            model_tokens = [tok for tok in v_model.split() if len(tok) >= 2]
            if any(re.search(rf"\b{re.escape(tok)}\b", search_text) for tok in model_tokens) or (
                len(v_make) >= 3 and re.search(rf"\b{re.escape(v_make)}\b", search_text)
            ):
                matched.append(v)

        if len(matched) == 1:
            return matched[0], []
        if len(matched) > 1:
            for v in matched:
                v_year = getattr(v, "year", None)
                if v_year and str(v_year) in search_text:
                    return v, []
            return None, matched

        # 3. Busca por tipo (moto vs carro)
        if any(w in search_text for w in ("moto", "motoca", "duas rodas")):
            motos = [
                v for v in vehicles
                if "moto" in str(getattr(v, "vehicle_type", "")).lower()
            ]
            if len(motos) == 1:
                return motos[0], []
            if len(motos) > 1:
                return None, motos

        if any(w in search_text for w in ("carro", "automóvel", "automovel")):
            carros = [
                v for v in vehicles
                if "carro" in str(getattr(v, "vehicle_type", "")).lower()
            ]
            if len(carros) == 1:
                return carros[0], []
            if len(carros) > 1:
                return None, carros

        # 4. Busca aproximada (Fuzzy Matching >= 0.80 para typos como 'sandeiro' -> 'sandero')
        words = [w for w in re.findall(r"[a-z0-9]+", search_text) if len(w) >= 4]
        fuzzy_matches: List[Vehicle] = []
        for v in vehicles:
            v_model = str(getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
            for tok in v_model.split():
                if len(tok) < 3:
                    continue
                if any(difflib.SequenceMatcher(None, w, tok).ratio() >= 0.80 for w in words):
                    fuzzy_matches.append(v)
                    break
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], []
        if len(fuzzy_matches) > 1:
            return None, fuzzy_matches

        return None, vehicles

    async def handle_incoming_message(
        self,
        user_id: Optional[str] = None,
        message_text: Optional[str] = None,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
        *,
        phone: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ponto de entrada único para processamento de mensagens recebidas."""
        uid = user_id if user_id is not None else (phone or "default")
        msg = message_text if message_text is not None else (message or "")
        return await self._handle_incoming_message_locked(
            user_id=uid,
            message_text=msg,
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
        user_lock = self._get_user_lock(user_id)
        async with _TurnLock(user_lock) as turn_lock:
            return await self._process_turn(
                turn_lock,
                user_id=user_id,
                message_text=message_text,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
            )

    async def _process_turn(
        self,
        turn_lock: _TurnLock,
        user_id: str,
        message_text: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        vehicles = vehicles if vehicles is not None else []
        maintenances = maintenances if maintenances is not None else []
        reminders = reminders if reminders is not None else []
        fines = fines if fines is not None else []

        clean_text = (message_text or "").strip()
        context = self.get_context(user_id)
        turn_id = context.advance_turn(clean_text)

        # 0. Comando operacional de Hard Reset (/reset_all ou "zerar sistema")
        if re.match(r"^(?:/reset_all|zerar\s+(?:o\s+)?sistema|hard\s+reset)$", clean_text, re.IGNORECASE):
            vehicles.clear()
            maintenances.clear()
            reminders.clear()
            fines.clear()
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.draft_updated_at = None
            context.recent_turns.clear()
            self.update_context(context)
            reply = "🧹 *Garagem e Sistema zerados com sucesso!* Todos os veículos, manutenções, lembretes e rascunhos foram removidos."
            return {
                "reply": reply,
                "action_type": "system_reset",
                "state": context.state.value,
                "committed_data": {"_reset_system": True, "intent": "system_reset"},
            }

        # 0.1. Escape Pós-Commit ("desfazer" / soft delete)
        if is_undo_command(clean_text):
            if context.is_awaiting_confirmation():
                context.state = ConversationState.NORMAL
                context.draft_payload = None
                context.draft_updated_at = None
                self.update_context(context)
                reply = UNDO_DRAFT_DISCARDED_REPLY
                self._record_assistant_turn(context, reply)
                return {
                    "reply": reply,
                    "action_type": "action_cancelled",
                    "state": context.state.value,
                    "details": {},
                }
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.draft_updated_at = None
            self.update_context(context)
            reply = UNDO_SUCCESS_REPLY
            self._record_assistant_turn(context, reply)
            return {
                "reply": reply,
                "action_type": "undo_last_record",
                "state": context.state.value,
                "details": {},
            }

        # 1. Verificação de expiração de rascunho por TTL (30 minutos)
        if self._is_draft_expired(context):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.draft_updated_at = None
            self.update_context(context)
            if re.search(r"\b(?:sim|s|yes|pode\s+salvar|confirmo|ok|não|nao|cancela)\b", clean_text, re.IGNORECASE):
                reply = DRAFT_EXPIRED_REPLY
                self._record_assistant_turn(context, reply)
                return {
                    "reply": reply,
                    "action_type": "draft_expired",
                    "state": context.state.value,
                    "details": {},
                }

        # 1.1 Resolução de desambiguação pendente (quando havia 2 veículos do mesmo modelo e o usuário informa a placa)
        effective_message = clean_text
        resolved_ambiguous_vehicle: Optional[Vehicle] = None
        if context.pending_ambiguous_message and vehicles:
            for v in vehicles:
                v_plate = (getattr(v, "plate", None) or "").upper().replace("-", "").strip()
                if v_plate and v_plate in clean_text.upper().replace("-", ""):
                    resolved_ambiguous_vehicle = v
                    break
            if resolved_ambiguous_vehicle is not None:
                context.active_vehicle_id = resolved_ambiguous_vehicle.id
                effective_message = f"{context.pending_ambiguous_message} placa {resolved_ambiguous_vehicle.plate}"
                context.pending_ambiguous_message = None

        # 2. Chamada ao Google Gemini (Arquitetura 100% AI-First)
        ctx_snapshot = context.model_copy(deep=True)
        history_dicts = [
            {"role": t.role, "content": t.content}
            for t in ctx_snapshot.recent_turns[:-1]
        ]

        async with turn_lock.released():
            analysis: GeminiAnalysisResponse = await self.gemini.analyze_message(
                user_id=user_id,
                current_message=effective_message,
                history=history_dicts,
                context=ctx_snapshot,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
            )

        # Reconciliação de concorrência pós-await: se outro turno atropelou este usuário enquanto o LLM rodava
        live_ctx = self.get_context(user_id)
        if live_ctx.turn_seq != turn_id:
            return {
                "reply": analysis.reply_message,
                "action_type": "async_superseded_completed",
                "state": live_ctx.state.value,
                "superseded": True,
            }

        # 3. Execução Mecânica das Tool Calls e Intenções Retornadas pelo Gemini
        result = self._execute_gemini_decision(
            analysis=analysis,
            context=live_ctx,
            vehicles=vehicles,
            maintenances=maintenances,
            clean_msg=effective_message,
            resolved_ambiguous_vehicle=resolved_ambiguous_vehicle,
        )

        self._record_assistant_turn(live_ctx, result["reply"])
        self.update_context(live_ctx)
        return result

    def _execute_gemini_decision(
        self,
        analysis: GeminiAnalysisResponse,
        context: ConversationContext,
        vehicles: List[Vehicle],
        maintenances: List[Any],
        clean_msg: str,
        resolved_ambiguous_vehicle: Optional[Vehicle] = None,
    ) -> Dict[str, Any]:
        """
        Executa mecanicamente no estado e banco de dados a decisão estruturada do Gemini.
        Nunca sobrescreve `analysis.reply_message` com templates fixos.
        """
        tool_calls_list = list(analysis.tool_calls or ([] if not analysis.tool_call else [analysis.tool_call]))
        primary_tc: Optional[ToolCallAction] = tool_calls_list[0] if tool_calls_list else None

        # Atualiza veículo ativo em foco na sessão caso identificado
        if resolved_ambiguous_vehicle and getattr(resolved_ambiguous_vehicle, "id", None):
            context.active_vehicle_id = resolved_ambiguous_vehicle.id
        elif vehicles and analysis.intent != IntentType.REGISTER_VEHICLE:
            matched_v, _ = self._resolve_vehicle(analysis.target_vehicle_ref, vehicles, clean_msg)
            if matched_v and getattr(matched_v, "id", None):
                context.active_vehicle_id = matched_v.id

        # A. Execução em lote (Multi-Tool Call: ex. patch_vehicle_profile + get_service_history)
        if len(tool_calls_list) > 1:
            patch_tc = next((t for t in tool_calls_list if t.name == "patch_vehicle_profile"), None)
            if patch_tc is not None:
                return self._execute_patch_vehicle_profile(
                    analysis=analysis,
                    tc=patch_tc,
                    context=context,
                    vehicles=vehicles,
                    clean_msg=clean_msg,
                )

        # B. Confirmação de rascunho (confirm_and_save_draft ou CONFIRM_ACTION)
        if (primary_tc and primary_tc.name == "confirm_and_save_draft") or analysis.intent == IntentType.CONFIRM_ACTION:
            return self._execute_confirm_draft(analysis=analysis, context=context)

        # C. Cancelamento de rascunho (CANCEL_ACTION)
        if analysis.intent == IntentType.CANCEL_ACTION:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.draft_updated_at = None
            return {
                "reply": analysis.reply_message,
                "action_type": "action_cancelled",
                "state": context.state.value,
                "details": {},
            }

        # D. Edição do último serviço salvo (update_last_saved_service / UPDATE_LAST_SAVED_SERVICE)
        if (primary_tc and primary_tc.name == "update_last_saved_service") or analysis.intent == IntentType.UPDATE_LAST_SAVED_SERVICE:
            return self._execute_update_last_saved_service(
                analysis=analysis,
                tc=primary_tc,
                context=context,
                maintenances=maintenances,
            )

        # E. Consulta de histórico de serviços (get_service_history / QUERY_HISTORY)
        if (primary_tc and primary_tc.name == "get_service_history") or analysis.intent == IntentType.QUERY_HISTORY:
            return {
                "reply": analysis.reply_message,
                "action_type": "query_history",
                "state": context.state.value,
                "details": analysis.extracted_entities,
            }

        # F. Atualização direta de odômetro / ficha do veículo salvo (patch_vehicle_profile / UPDATE_ODOMETER)
        if (primary_tc and primary_tc.name == "patch_vehicle_profile") or analysis.intent == IntentType.UPDATE_ODOMETER:
            return self._execute_patch_vehicle_profile(
                analysis=analysis,
                tc=primary_tc,
                context=context,
                vehicles=vehicles,
                clean_msg=clean_msg,
            )

        # G. Criação ou atualização (Deep Merge) de Rascunho (upsert_draft ou Ações Críticas)
        if (
            (primary_tc and primary_tc.name == "upsert_draft")
            or analysis.is_critical_action
            or analysis.intent in (
                IntentType.REGISTER_VEHICLE,
                IntentType.RECORD_MAINTENANCE,
                IntentType.UPSERT_DRAFT,
                IntentType.SET_REMINDER,
                IntentType.REGISTER_FINE,
                IntentType.RESET_GARAGE,
            )
        ):
            return self._execute_upsert_draft(
                analysis=analysis,
                tc=primary_tc,
                context=context,
                vehicles=vehicles,
                clean_msg=clean_msg,
                resolved_ambiguous_vehicle=resolved_ambiguous_vehicle,
            )

        # H. Respostas conversacionais diretas (CONSULTANCY, GREETING_MENU, POLITE_CLOSING, UNKNOWN)
        if not context.is_awaiting_confirmation():
            context.state = ConversationState.NORMAL
        return {
            "reply": analysis.reply_message,
            "action_type": analysis.intent.value,
            "state": context.state.value,
            "details": analysis.extracted_entities,
        }

    def _execute_confirm_draft(
        self,
        analysis: GeminiAnalysisResponse,
        context: ConversationContext,
    ) -> Dict[str, Any]:
        """Persiste o rascunho ativo em `committed_data` e limpa o estado pendente."""
        if not context.draft_payload:
            context.state = ConversationState.NORMAL
            context.draft_updated_at = None
            return {
                "reply": analysis.reply_message,
                "action_type": "none",
                "state": context.state.value,
            }

        committed = dict(context.draft_payload)
        state = context.state

        if committed.get("_is_multi_action"):
            committed["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
            committed["intent"] = "multi_action"
        elif state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM or committed.get("_action_state") == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value:
            committed["_action_state"] = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
            committed["intent"] = IntentType.REGISTER_VEHICLE.value
            if committed.get("odometer_km") is None and committed.get("km") is not None:
                committed["odometer_km"] = committed["km"]
            if committed.get("km") is None and committed.get("odometer_km") is not None:
                committed["km"] = committed["odometer_km"]
        elif state == ConversationState.AWAITING_MAINTENANCE_CONFIRM or committed.get("_action_state") == ConversationState.AWAITING_MAINTENANCE_CONFIRM.value:
            committed["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
            committed["intent"] = IntentType.RECORD_MAINTENANCE.value
            itens = committed.get("itens") or committed.get("components") or []
            if isinstance(itens, str):
                itens = [itens]
            committed["itens"] = list(itens)
            committed["components"] = list(itens)
            if not committed.get("description"):
                committed["description"] = committed.get("service_summary") or (", ".join(itens) if itens else "Manutenção preventiva")
            if committed.get("km") is None and committed.get("odometer_km") is not None:
                committed["km"] = committed["odometer_km"]
            if committed.get("odometer_km") is None and committed.get("km") is not None:
                committed["odometer_km"] = committed["km"]
            if committed.get("total_cost") is None and committed.get("valor") is not None:
                committed["total_cost"] = committed["valor"]
            if committed.get("workshop_name") is None and committed.get("oficina") is not None:
                committed["workshop_name"] = committed["oficina"]
            if committed.get("date_str") is None and committed.get("data") is not None:
                committed["date_str"] = committed["data"]
        elif state == ConversationState.AWAITING_REMINDER_CONFIRM or committed.get("_action_state") == ConversationState.AWAITING_REMINDER_CONFIRM.value:
            committed["_action_state"] = ConversationState.AWAITING_REMINDER_CONFIRM.value
            committed["intent"] = IntentType.SET_REMINDER.value
        elif state == ConversationState.AWAITING_FINE_CONFIRM or committed.get("_action_state") == ConversationState.AWAITING_FINE_CONFIRM.value:
            committed["_action_state"] = ConversationState.AWAITING_FINE_CONFIRM.value
            committed["intent"] = IntentType.REGISTER_FINE.value
        elif state == ConversationState.AWAITING_GARAGE_RESET_CONFIRM or committed.get("_action_state") == ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value:
            committed["_action_state"] = ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value
            committed["_reset_garage"] = True
            committed["intent"] = IntentType.RESET_GARAGE.value
        elif "intent" not in committed:
            committed["intent"] = IntentType.RECORD_MAINTENANCE.value

        # Se a IA também retornou um lembrete encadeado junto com a confirmação
        if analysis.reminder:
            committed["chained_reminder"] = analysis.reminder.model_dump(exclude_none=True)
        elif analysis.extracted_entities and isinstance(analysis.extracted_entities.get("chained_reminder"), dict):
            committed["chained_reminder"] = analysis.extracted_entities["chained_reminder"]

        action_type = (
            "action_confirmed_with_chained_reminder"
            if committed.get("chained_reminder")
            else "action_confirmed"
        )

        context.state = ConversationState.NORMAL
        context.draft_payload = None
        context.draft_updated_at = None

        return {
            "reply": analysis.reply_message,
            "action_type": action_type,
            "state": context.state.value,
            "committed_data": committed,
            "details": committed,
        }

    def _execute_upsert_draft(
        self,
        analysis: GeminiAnalysisResponse,
        tc: Optional[ToolCallAction],
        context: ConversationContext,
        vehicles: List[Vehicle],
        clean_msg: str,
        resolved_ambiguous_vehicle: Optional[Vehicle] = None,
    ) -> Dict[str, Any]:
        """
        Realiza o Deep Merge limpo dos dados extraídos pelo Gemini no `context.draft_payload`
        e mantém a resposta natural gerada pelo Gemini (`analysis.reply_message`).
        """
        # 1. Caso de múltiplas ações na mesma mensagem (List[ActionDraft] >= 2)
        if len(analysis.actions) >= 2:
            # Verifica se há ambiguidade entre múltiplos veículos do mesmo modelo
            target_ref_top = analysis.target_vehicle_ref or (analysis.actions[0].target_vehicle_ref if analysis.actions else None)
            target_v, ambiguous_candidates = (
                (resolved_ambiguous_vehicle, [])
                if resolved_ambiguous_vehicle is not None
                else self._resolve_vehicle(target_ref_top, vehicles, clean_msg)
            )
            if len(vehicles) > 1 and target_v is None and ambiguous_candidates:
                context.pending_ambiguous_message = clean_msg
                return {
                    "reply": analysis.reply_message,
                    "action_type": "vehicle_disambiguation_required",
                    "state": context.state.value,
                    "details": {},
                }

            actions_payload: List[Dict[str, Any]] = []
            for act in analysis.actions:
                act_dict = act.model_dump(exclude_none=True)
                act_v, _ = (
                    (resolved_ambiguous_vehicle, [])
                    if resolved_ambiguous_vehicle is not None
                    else self._resolve_vehicle(act.target_vehicle_ref, vehicles, clean_msg)
                )
                if act_v and getattr(act_v, "id", None):
                    act_dict["vehicle_id"] = act_v.id
                    if getattr(act_v, "plate", None) and (resolved_ambiguous_vehicle is not None or not act_dict.get("target_vehicle_ref")):
                        act_dict["target_vehicle_ref"] = act_v.plate
                actions_payload.append(act_dict)

            multi_draft: Dict[str, Any] = {
                "intent": "multi_action",
                "_is_multi_action": True,
                "actions": actions_payload,
                "_action_state": ConversationState.AWAITING_MAINTENANCE_CONFIRM.value,
            }
            if target_v and getattr(target_v, "plate", None):
                multi_draft["_target_vehicle_ref"] = target_v.plate
            elif target_ref_top:
                multi_draft["_target_vehicle_ref"] = target_ref_top

            context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
            context.draft_payload = multi_draft
            context.draft_updated_at = self._utcnow()
            return {
                "reply": analysis.reply_message,
                "action_type": "awaiting_confirmation",
                "state": context.state.value,
                "draft": context.draft_payload,
                "details": multi_draft,
            }

        # Determina se o rascunho é de VEÍCULO
        is_vehicle_draft = (
            analysis.intent == IntentType.REGISTER_VEHICLE
            or (
                context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
                and analysis.intent not in (
                    IntentType.RECORD_MAINTENANCE,
                    IntentType.SET_REMINDER,
                    IntentType.REGISTER_FINE,
                    IntentType.RESET_GARAGE,
                )
            )
            or (analysis.vehicle is not None and analysis.intent != IntentType.RECORD_MAINTENANCE)
        )

        # 2. Rascunho de Cadastro / Atualização de Veículo (REGISTER_VEHICLE)
        if is_vehicle_draft:
            existing = (
                dict(context.draft_payload)
                if context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM and isinstance(context.draft_payload, dict)
                else {}
            )

            v_obj = analysis.vehicle
            ext = analysis.extracted_entities or {}

            incoming: Dict[str, Any] = {}
            make_val = (
                (v_obj.make or v_obj.brand) if v_obj else None
            ) or ((tc.make or tc.brand) if tc else None) or ext.get("make") or ext.get("brand")
            model_val = (v_obj.model if v_obj else None) or (tc.model if tc else None) or ext.get("model")
            version_val = (v_obj.version if v_obj else None) or (tc.version if tc else None) or ext.get("version")
            year_val = (v_obj.year if v_obj else None) or (tc.year if tc else None) or ext.get("year")
            plate_val = (v_obj.plate if v_obj else None) or (tc.plate if tc else None) or ext.get("plate")
            odo_val = (
                (v_obj.odometer_km if v_obj and v_obj.odometer_km is not None else (v_obj.odometer if v_obj else None))
                if v_obj and (v_obj.odometer_km is not None or v_obj.odometer is not None)
                else (
                    (tc.odometer if tc.odometer is not None else tc.km)
                    if tc and (tc.odometer is not None or tc.km is not None)
                    else (ext.get("odometer_km") if ext.get("odometer_km") is not None else ext.get("odometer") if ext.get("odometer") is not None else ext.get("km"))
                )
            )
            engine_val = (v_obj.engine or v_obj.motor if v_obj else None) or (tc.engine if tc else None) or ext.get("engine") or ext.get("motor")
            trans_val = (v_obj.transmission or v_obj.cambio if v_obj else None) or (tc.transmission if tc else None) or ext.get("transmission") or ext.get("cambio")
            steering_val = (v_obj.steering or v_obj.direcao if v_obj else None) or (tc.steering if tc else None) or ext.get("steering") or ext.get("direcao")
            vtype_val = (v_obj.vehicle_type.value if v_obj and v_obj.vehicle_type else None) or (tc.vehicle_type.value if tc and tc.vehicle_type else None) or ext.get("vehicle_type")
            fuel_val = (v_obj.fuel_type.value if v_obj and v_obj.fuel_type else None) or (tc.fuel_type.value if tc and tc.fuel_type else None) or ext.get("fuel_type")

            if make_val:
                incoming["make"] = make_val
                incoming["brand"] = make_val
            if model_val:
                incoming["model"] = model_val
            if version_val:
                incoming["version"] = version_val
            if year_val is not None:
                incoming["year"] = year_val
            if plate_val:
                incoming["plate"] = str(plate_val).upper().replace("-", "").strip()
            if odo_val is not None:
                incoming["odometer_km"] = int(odo_val)
                incoming["odometer"] = int(odo_val)
                incoming["km"] = int(odo_val)
            if engine_val:
                incoming["engine"] = engine_val
                incoming["motor"] = engine_val
            if trans_val:
                incoming["transmission"] = trans_val
                incoming["cambio"] = trans_val
            if steering_val:
                incoming["steering"] = steering_val
                incoming["direcao"] = steering_val
            if vtype_val:
                incoming["vehicle_type"] = vtype_val
            if fuel_val:
                incoming["fuel_type"] = fuel_val

            merged = {**existing, **incoming}
            if not merged.get("vehicle_type"):
                merged["vehicle_type"] = VehicleType.CARRO.value
            if not merged.get("fuel_type"):
                merged["fuel_type"] = FuelType.FLEX.value

            has_meaningful_vehicle_data = any(
                merged.get(k) is not None
                for k in ("make", "model", "plate", "odometer_km", "engine", "transmission", "steering", "year")
            )
            if not has_meaningful_vehicle_data and not analysis.is_critical_action:
                return {
                    "reply": analysis.reply_message,
                    "action_type": IntentType.REGISTER_VEHICLE.value,
                    "state": context.state.value,
                    "details": merged,
                }

            merged["_action_state"] = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
            context.state = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
            context.draft_payload = merged
            context.draft_updated_at = self._utcnow()
            return {
                "reply": analysis.reply_message,
                "action_type": "awaiting_confirmation",
                "state": context.state.value,
                "draft": context.draft_payload,
                "details": merged,
            }

        # 3. Rascunho de Lembrete (SET_REMINDER)
        if analysis.intent == IntentType.SET_REMINDER:
            existing = (
                dict(context.draft_payload)
                if context.state == ConversationState.AWAITING_REMINDER_CONFIRM and isinstance(context.draft_payload, dict)
                else {}
            )
            rem_dict = analysis.reminder.model_dump(exclude_none=True) if analysis.reminder else {}
            ext = analysis.extracted_entities or {}
            merged = {**existing, **ext, **rem_dict}
            if not merged.get("task_description") and merged.get("description"):
                merged["task_description"] = merged["description"]
            target_v, _ = self._resolve_vehicle(analysis.target_vehicle_ref, vehicles, clean_msg)
            if target_v and getattr(target_v, "id", None) and not merged.get("vehicle_id"):
                merged["vehicle_id"] = target_v.id
            merged["_action_state"] = ConversationState.AWAITING_REMINDER_CONFIRM.value
            context.state = ConversationState.AWAITING_REMINDER_CONFIRM
            context.draft_payload = merged
            context.draft_updated_at = self._utcnow()
            return {
                "reply": analysis.reply_message,
                "action_type": "awaiting_confirmation",
                "state": context.state.value,
                "draft": context.draft_payload,
                "details": merged,
            }

        # 4. Rascunho de Multa (REGISTER_FINE)
        if analysis.intent == IntentType.REGISTER_FINE:
            existing = (
                dict(context.draft_payload)
                if context.state == ConversationState.AWAITING_FINE_CONFIRM and isinstance(context.draft_payload, dict)
                else {}
            )
            fine_dict = analysis.fine.model_dump(exclude_none=True) if analysis.fine else {}
            ext = analysis.extracted_entities or {}
            merged = {**existing, **ext, **fine_dict}
            target_v, _ = self._resolve_vehicle(analysis.target_vehicle_ref, vehicles, clean_msg)
            if target_v and getattr(target_v, "id", None) and not merged.get("vehicle_id"):
                merged["vehicle_id"] = target_v.id
            merged["_action_state"] = ConversationState.AWAITING_FINE_CONFIRM.value
            context.state = ConversationState.AWAITING_FINE_CONFIRM
            context.draft_payload = merged
            context.draft_updated_at = self._utcnow()
            return {
                "reply": analysis.reply_message,
                "action_type": "awaiting_confirmation",
                "state": context.state.value,
                "draft": context.draft_payload,
                "details": merged,
            }

        # 5. Rascunho de Reset de Garagem (RESET_GARAGE)
        if analysis.intent == IntentType.RESET_GARAGE:
            if not vehicles or not analysis.is_critical_action:
                return {
                    "reply": analysis.reply_message,
                    "action_type": IntentType.RESET_GARAGE.value,
                    "state": context.state.value,
                    "details": {},
                }
            reset_draft = {
                "intent": IntentType.RESET_GARAGE.value,
                "_reset_garage": True,
                "_action_state": ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value,
            }
            context.state = ConversationState.AWAITING_GARAGE_RESET_CONFIRM
            context.draft_payload = reset_draft
            context.draft_updated_at = self._utcnow()
            return {
                "reply": analysis.reply_message,
                "action_type": "awaiting_confirmation",
                "state": context.state.value,
                "draft": context.draft_payload,
                "details": reset_draft,
            }

        # 6. Rascunho de Manutenção (RECORD_MAINTENANCE / upsert_draft)
        existing = (
            dict(context.draft_payload)
            if context.state == ConversationState.AWAITING_MAINTENANCE_CONFIRM and isinstance(context.draft_payload, dict)
            else {}
        )

        # Se o rascunho atual é um lote multi-ação e o usuário enviou correção (ex: valor/km), atualiza a sub-ação de manutenção
        if existing.get("_is_multi_action") and isinstance(existing.get("actions"), list):
            m_obj = analysis.maintenance
            ext = analysis.extracted_entities or {}
            valor_corr = (
                (tc.valor if tc and tc.valor is not None else None)
                if (tc and tc.valor is not None)
                else (
                    m_obj.total_cost
                    if (m_obj and m_obj.total_cost is not None)
                    else (ext.get("valor") if ext.get("valor") is not None else ext.get("total_cost"))
                )
            )
            km_corr = (
                (tc.km if tc and tc.km is not None else None)
                if (tc and tc.km is not None)
                else (
                    m_obj.odometer_km
                    if (m_obj and m_obj.odometer_km is not None)
                    else (ext.get("km") if ext.get("km") is not None else ext.get("odometer_km"))
                )
            )
            for act in existing["actions"]:
                act_int = str(act.get("intent", "")).lower()
                if "maintenance" in act_int:
                    if valor_corr is not None:
                        act["total_cost"] = float(valor_corr)
                        act["valor"] = float(valor_corr)
                    if km_corr is not None:
                        act["odometer_km"] = int(km_corr)
                        act["km"] = int(km_corr)
            context.draft_payload = existing
            context.draft_updated_at = self._utcnow()
            return {
                "reply": analysis.reply_message,
                "action_type": "awaiting_confirmation",
                "state": context.state.value,
                "draft": context.draft_payload,
                "details": existing,
            }

        # Se não há nenhum veículo cadastrado na garagem e não havia rascunho prévio em andamento,
        # rejeita criação de rascunho órfão de manutenção (B12)
        if not vehicles and not existing:
            return {
                "reply": analysis.reply_message,
                "action_type": "needs_vehicle",
                "state": context.state.value,
                "details": {},
            }

        m_obj = analysis.maintenance
        ext = analysis.extracted_entities or {}

        prev_items: List[str] = list(existing.get("itens") or existing.get("components") or [])
        new_items: List[str] = []
        if tc and tc.itens:
            new_items.extend(tc.itens)
        elif m_obj and m_obj.components:
            new_items.extend(m_obj.components)
        elif ext.get("itens") or ext.get("components"):
            raw_ext_items = ext.get("itens") or ext.get("components")
            if isinstance(raw_ext_items, list):
                new_items.extend(raw_ext_items)
            elif isinstance(raw_ext_items, str):
                new_items.append(raw_ext_items)

        merged_items = list(prev_items)
        for item in new_items:
            item_clean = str(item).strip()
            if item_clean and not any(item_clean.lower() == p.lower() for p in merged_items):
                merged_items.append(item_clean)

        km_val = (
            (tc.km if tc and tc.km is not None else None)
            if (tc and tc.km is not None)
            else (
                m_obj.odometer_km
                if (m_obj and m_obj.odometer_km is not None)
                else (ext.get("km") if ext.get("km") is not None else ext.get("odometer_km"))
            )
        )
        data_val = (
            (tc.data if tc and tc.data else None)
            or (m_obj.date_str if m_obj and m_obj.date_str else None)
            or ext.get("data")
            or ext.get("date_str")
        )
        oficina_val = (
            (tc.oficina if tc and tc.oficina else None)
            or (m_obj.workshop_name if m_obj and m_obj.workshop_name else None)
            or ext.get("oficina")
            or ext.get("workshop_name")
        )
        valor_val = (
            (tc.valor if tc and tc.valor is not None else None)
            if (tc and tc.valor is not None)
            else (
                m_obj.total_cost
                if (m_obj and m_obj.total_cost is not None)
                else (ext.get("valor") if ext.get("valor") is not None else ext.get("total_cost"))
            )
        )
        summary_val = (
            (m_obj.service_summary if m_obj and m_obj.service_summary else None)
            or ext.get("service_summary")
            or ext.get("description")
        )

        if not merged_items and km_val is None and valor_val is None and not summary_val and not existing:
            return {
                "reply": analysis.reply_message,
                "action_type": analysis.intent.value,
                "state": context.state.value,
                "details": ext,
            }

        target_ref = (tc.target_vehicle_ref if tc and tc.target_vehicle_ref else None) or analysis.target_vehicle_ref or existing.get("_target_vehicle_ref")
        target_v, ambiguous_candidates = (
            (resolved_ambiguous_vehicle, [])
            if resolved_ambiguous_vehicle is not None
            else self._resolve_vehicle(target_ref, vehicles, clean_msg)
        )

        if target_v and getattr(target_v, "id", None):
            existing["vehicle_id"] = target_v.id
            existing["_target_vehicle_ref"] = getattr(target_v, "plate", None) or f"{target_v.make} {target_v.model}"
            context.active_vehicle_id = target_v.id

        merged_maint = dict(existing)
        if merged_items:
            merged_maint["itens"] = merged_items
            merged_maint["components"] = merged_items
        if summary_val or merged_items:
            merged_maint["service_summary"] = summary_val or ", ".join(merged_items)
            merged_maint["description"] = merged_maint["service_summary"]
        if km_val is not None:
            merged_maint["km"] = int(km_val)
            merged_maint["odometer_km"] = int(km_val)
        if data_val:
            merged_maint["data"] = data_val
            merged_maint["date_str"] = data_val
        if oficina_val:
            merged_maint["oficina"] = oficina_val
            merged_maint["workshop_name"] = oficina_val
        if valor_val is not None:
            merged_maint["valor"] = float(valor_val)
            merged_maint["total_cost"] = float(valor_val)
        if ext.get("_is_odometer_rollback"):
            merged_maint["_is_odometer_rollback"] = True

        if len(vehicles) > 1 and not merged_maint.get("vehicle_id") and ambiguous_candidates:
            context.pending_ambiguous_message = clean_msg
            merged_maint["_target_vehicle_ref"] = f"AMBIGUOUS:{target_ref or ''}"
            merged_maint["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
            context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
            context.draft_payload = merged_maint
            context.draft_updated_at = self._utcnow()
            return {
                "reply": analysis.reply_message,
                "action_type": "vehicle_disambiguation_required",
                "state": context.state.value,
                "draft": context.draft_payload,
                "details": merged_maint,
            }

        merged_maint["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
        context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
        context.draft_payload = merged_maint
        context.draft_updated_at = self._utcnow()
        return {
            "reply": analysis.reply_message,
            "action_type": "awaiting_confirmation",
            "state": context.state.value,
            "draft": context.draft_payload,
            "details": merged_maint,
        }

    def _execute_patch_vehicle_profile(
        self,
        analysis: GeminiAnalysisResponse,
        tc: Optional[ToolCallAction],
        context: ConversationContext,
        vehicles: List[Vehicle],
        clean_msg: str,
    ) -> Dict[str, Any]:
        """Executa atualização direta de odômetro ou perfil de veículo salvo sem abrir rascunho de manutenção."""
        ext = analysis.extracted_entities or {}
        v_obj = analysis.vehicle

        new_km = (
            (tc.km if tc and tc.km is not None else (tc.odometer if tc and tc.odometer is not None else None))
            if tc and (tc.km is not None or tc.odometer is not None)
            else (
                v_obj.odometer_km
                if (v_obj and v_obj.odometer_km is not None)
                else (
                    analysis.maintenance.odometer_km
                    if (analysis.maintenance and analysis.maintenance.odometer_km is not None)
                    else (ext.get("km") if ext.get("km") is not None else ext.get("odometer_km"))
                )
            )
        )

        target_ref = (tc.target_vehicle_ref if tc and tc.target_vehicle_ref else None) or analysis.target_vehicle_ref
        target_v, ambiguous = self._resolve_vehicle(target_ref, vehicles, clean_msg)

        if target_v is None and len(vehicles) == 1:
            target_v = vehicles[0]

        if target_v is None and ambiguous:
            return {
                "reply": analysis.reply_message,
                "action_type": "vehicle_disambiguation_required",
                "state": context.state.value,
                "details": {},
            }

        if target_v is not None and new_km is not None and int(new_km) >= 0:
            if analysis.intent == IntentType.UNKNOWN:
                return {
                    "reply": analysis.reply_message,
                    "action_type": IntentType.UNKNOWN.value,
                    "state": context.state.value,
                    "details": {},
                }
            old_km = getattr(target_v, "current_km", None) or 0
            target_v.current_km = int(new_km)
            committed = {
                "intent": IntentType.UPDATE_ODOMETER.value,
                "_is_odometer_update": True,
                "vehicle_id": getattr(target_v, "id", None),
                "_target_vehicle_ref": target_ref or getattr(target_v, "model", None),
                "old_km": old_km,
                "new_km": int(new_km),
                "odometer_km": int(new_km),
                "km": int(new_km),
            }
            return {
                "reply": analysis.reply_message,
                "action_type": "odometer_updated",
                "state": context.state.value,
                "committed_data": committed,
                "details": committed,
            }

        return {
            "reply": analysis.reply_message,
            "action_type": analysis.intent.value,
            "state": context.state.value,
            "details": ext,
        }

    def _execute_update_last_saved_service(
        self,
        analysis: GeminiAnalysisResponse,
        tc: Optional[ToolCallAction],
        context: ConversationContext,
        maintenances: List[Any],
    ) -> Dict[str, Any]:
        """Executa a edição direta do último registro de manutenção salvo no banco."""
        if not maintenances:
            return {
                "reply": analysis.reply_message,
                "action_type": "none",
                "state": context.state.value,
                "details": {},
            }

        target_service = maintenances[0]
        service_id = (tc.service_id if tc and tc.service_id is not None else None) or getattr(target_service, "id", None)
        if service_id is not None:
            for m in maintenances:
                if str(getattr(m, "id", "")) == str(service_id):
                    target_service = m
                    break

        m_obj = analysis.maintenance
        ext = analysis.extracted_entities or {}

        new_itens = (tc.itens if tc and tc.itens else None) or (m_obj.components if m_obj and m_obj.components else None) or ext.get("itens")
        new_km = (
            (tc.km if tc and tc.km is not None else None)
            if (tc and tc.km is not None)
            else (m_obj.odometer_km if (m_obj and m_obj.odometer_km is not None) else ext.get("km"))
        )
        new_data = (tc.data if tc and tc.data else None) or (m_obj.date_str if m_obj and m_obj.date_str else None) or ext.get("data")
        new_oficina = (tc.oficina if tc and tc.oficina else None) or (m_obj.workshop_name if m_obj and m_obj.workshop_name else None) or ext.get("oficina")
        new_valor = (
            (tc.valor if tc and tc.valor is not None else None)
            if (tc and tc.valor is not None)
            else (m_obj.total_cost if (m_obj and m_obj.total_cost is not None) else ext.get("valor"))
        )

        has_updates = any(v is not None for v in (new_itens, new_km, new_data, new_oficina, new_valor))
        if not has_updates:
            context.pending_edit_service_id = service_id
            if ext.get("pending_edit_field"):
                context.pending_edit_field = ext["pending_edit_field"]
            return {
                "reply": analysis.reply_message,
                "action_type": "awaiting_edit_value",
                "state": context.state.value,
                "details": {"service_id": service_id},
            }

        context.pending_edit_field = None
        context.pending_edit_service_id = None

        existing_items = list(getattr(target_service, "components", None) or [])
        if new_itens:
            merged_items = list(existing_items)
            for item in new_itens:
                item_clean = str(item).strip()
                if item_clean and not any(item_clean.lower() == p.lower() for p in merged_items):
                    merged_items.append(item_clean)
            target_service.components = merged_items
            target_service.description = ", ".join(merged_items)

        if new_km is not None:
            target_service.km = int(new_km)
        if new_oficina is not None:
            target_service.workshop_name = new_oficina
        if new_valor is not None:
            target_service.total_cost = float(new_valor)
        if new_data:
            try:
                parsed_dt = datetime.strptime(new_data, "%d/%m/%Y").date()
                target_service.date = parsed_dt
            except ValueError:
                pass

        committed = {
            "intent": IntentType.UPDATE_LAST_SAVED_SERVICE.value,
            "service_id": getattr(target_service, "id", service_id),
            "itens": getattr(target_service, "components", []),
            "components": getattr(target_service, "components", []),
            "description": getattr(target_service, "description", ""),
            "km": getattr(target_service, "km", None),
            "data": new_data,
            "oficina": getattr(target_service, "workshop_name", None),
            "valor": getattr(target_service, "total_cost", 0.0),
        }
        return {
            "reply": analysis.reply_message,
            "action_type": "update_last_saved_service",
            "state": context.state.value,
            "committed_data": committed,
            "details": committed,
        }
