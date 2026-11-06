"""
AutoCare V2 - Orquestrador de Conversação e Máquina de Estados (Arquitetura 100% AI-First)
Atua estritamente como orquestrador mecânico de estado entre o usuário, o Google Gemini e o banco:
1. Controle de concorrência (_TurnLock), validação de TTL (30 min) e comandos operacionais (/reset_all e desfazer).
2. Chamada ao Gemini (`src/ai/client.py`) repassando a mensagem do usuário e o contexto serializado.
3. Execução mecânica no estado/banco das `tool_calls` e intenções retornadas no JSON estruturado do Gemini.
4. Devolução ao usuário da exata mensagem (`reply_message`) gerada pelo Gemini, sem templates fixos.
"""

import asyncio
import difflib
import logging
import re
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

from src.domain.enums import IntentType, ConversationState, VehicleType, FuelType
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
    """Wrapper de posse sobre `asyncio.Lock` para um único turno com cessão durante chamada ao LLM."""

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
    """Orquestrador enxuto de conversação e gerenciamento de estado (100% AI-First)."""

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
        return datetime.now(timezone.utc)

    def _is_draft_expired(self, context: ConversationContext) -> bool:
        if not context.is_awaiting_confirmation() or not context.draft_updated_at:
            return False
        now_utc = self._utcnow()
        now_utc = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
        draft_ts = context.draft_updated_at
        draft_ts = draft_ts.replace(tzinfo=timezone.utc) if draft_ts.tzinfo is None else draft_ts.astimezone(timezone.utc)
        return (now_utc - draft_ts) > timedelta(minutes=self.draft_ttl_minutes)

    def reopen_draft_for_correction(
        self,
        user_id: str,
        committed_data: Dict[str, Any],
        clear_keys: Tuple[str, ...] = ("data", "date_str", "date"),
    ) -> None:
        if not committed_data:
            return
        ctx = self.get_context(user_id)
        state_value = committed_data.get("_action_state") or ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
        try:
            ctx.state = ConversationState(state_value)
        except ValueError:
            ctx.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
        ctx.draft_payload = {k: v for k, v in committed_data.items() if k != "_action_state" and k not in clear_keys}
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
        """Localiza o veículo alvo na garagem retornando `(veiculo_unico, candidatos_ambiguos)`."""
        if not vehicles:
            return None, []
        search_text = f"{target_ref or ''} {message}".lower()

        for v in vehicles:
            v_plate = getattr(v, "plate", None) or getattr(v, "placa", None)
            if v_plate and str(v_plate).lower().replace("-", "") in search_text.replace("-", ""):
                return v, []

        if len(vehicles) == 1:
            return vehicles[0], []

        def _match_against(text_to_search: str) -> List[Vehicle]:
            found: List[Vehicle] = []
            for v in vehicles:
                v_model = str(getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
                v_make = str(getattr(v, "make", None) or getattr(v, "marca", "") or "").lower()
                tokens = [tok for tok in v_model.split() if len(tok) >= 2]
                if any(re.search(rf"\b{re.escape(tok)}\b", text_to_search) for tok in tokens) or (
                    len(v_make) >= 3 and re.search(rf"\b{re.escape(v_make)}\b", text_to_search)
                ):
                    found.append(v)
            return found

        if target_ref and target_ref.strip():
            ref_matched = _match_against(target_ref.strip().lower())
            if len(ref_matched) == 1:
                return ref_matched[0], []
            if len(ref_matched) > 1:
                for v in ref_matched:
                    if getattr(v, "year", None) and str(v.year) in search_text:
                        return v, []
                return None, ref_matched

        matched = _match_against(search_text)
        if len(matched) == 1:
            return matched[0], []
        if len(matched) > 1:
            for v in matched:
                if getattr(v, "year", None) and str(v.year) in search_text:
                    return v, []
            return None, matched

        for words_key, vtype_token in ((("moto", "motoca", "duas rodas"), "moto"), (("carro", "automóvel", "automovel"), "carro")):
            if any(w in search_text for w in words_key):
                typed = [v for v in vehicles if vtype_token in str(getattr(v, "vehicle_type", "")).lower()]
                if len(typed) == 1:
                    return typed[0], []
                if len(typed) > 1:
                    return None, typed

        words = [w for w in re.findall(r"[a-z0-9]+", search_text) if len(w) >= 4]
        fuzzy_matches: List[Vehicle] = []
        for v in vehicles:
            v_model = str(getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
            for tok in v_model.split():
                if len(tok) >= 3 and any(difflib.SequenceMatcher(None, w, tok).ratio() >= 0.80 for w in words):
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
        uid = user_id if user_id is not None else (phone or "default")
        msg = message_text if message_text is not None else (message or "")
        user_lock = self._get_user_lock(uid)
        async with _TurnLock(user_lock) as turn_lock:
            return await self._process_turn(
                turn_lock, user_id=uid, message_text=msg,
                vehicles=vehicles, maintenances=maintenances, reminders=reminders, fines=fines,
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
            return {
                "reply": "🧹 *Garagem e Sistema zerados com sucesso!* Todos os veículos, manutenções, lembretes e rascunhos foram removidos.",
                "action_type": "system_reset",
                "state": context.state.value,
                "committed_data": {"_reset_system": True, "intent": "system_reset"},
            }

        if is_undo_command(clean_text):
            was_awaiting = context.is_awaiting_confirmation()
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.draft_updated_at = None
            self.update_context(context)
            reply = UNDO_DRAFT_DISCARDED_REPLY if was_awaiting else UNDO_SUCCESS_REPLY
            self._record_assistant_turn(context, reply)
            return {
                "reply": reply,
                "action_type": "action_cancelled" if was_awaiting else "undo_last_record",
                "state": context.state.value,
                "details": {},
            }

        if self._is_draft_expired(context):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.draft_updated_at = None
            self.update_context(context)
            if re.search(r"\b(?:sim|s|yes|pode\s+salvar|confirmo|ok|não|nao|cancela)\b", clean_text, re.IGNORECASE):
                self._record_assistant_turn(context, DRAFT_EXPIRED_REPLY)
                return {"reply": DRAFT_EXPIRED_REPLY, "action_type": "draft_expired", "state": context.state.value, "details": {}}

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

        ctx_snapshot = context.model_copy(deep=True)
        history_dicts = [{"role": t.role, "content": t.content} for t in ctx_snapshot.recent_turns[:-1]]

        async with turn_lock.released():
            analysis: GeminiAnalysisResponse = await self.gemini.analyze_message(
                user_id=user_id, current_message=effective_message, history=history_dicts,
                context=ctx_snapshot, vehicles=vehicles, maintenances=maintenances, reminders=reminders, fines=fines,
            )

        live_ctx = self.get_context(user_id)
        if live_ctx.turn_seq != turn_id:
            return {"reply": analysis.reply_message, "action_type": "async_superseded_completed", "state": live_ctx.state.value, "superseded": True}

        result = self._execute_gemini_decision(analysis, live_ctx, vehicles, maintenances, effective_message, resolved_ambiguous_vehicle)
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
        tool_calls_list = list(analysis.tool_calls or ([] if not analysis.tool_call else [analysis.tool_call]))
        primary_tc: Optional[ToolCallAction] = tool_calls_list[0] if tool_calls_list else None

        if resolved_ambiguous_vehicle and getattr(resolved_ambiguous_vehicle, "id", None):
            context.active_vehicle_id = resolved_ambiguous_vehicle.id
        elif vehicles and analysis.intent != IntentType.REGISTER_VEHICLE:
            matched_v, _ = self._resolve_vehicle(analysis.target_vehicle_ref, vehicles, clean_msg)
            if matched_v and getattr(matched_v, "id", None):
                context.active_vehicle_id = matched_v.id

        if len(tool_calls_list) > 1:
            patch_tc = next((t for t in tool_calls_list if t.name == "patch_vehicle_profile"), None)
            if patch_tc is not None:
                return self._execute_patch_vehicle_profile(analysis, patch_tc, context, vehicles, clean_msg)

        if (primary_tc and primary_tc.name == "confirm_and_save_draft") or analysis.intent == IntentType.CONFIRM_ACTION:
            return self._execute_confirm_draft(analysis, context)

        if analysis.intent == IntentType.CANCEL_ACTION:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.draft_updated_at = None
            return {"reply": analysis.reply_message, "action_type": "action_cancelled", "state": context.state.value, "details": {}}

        if (primary_tc and primary_tc.name == "update_last_saved_service") or analysis.intent == IntentType.UPDATE_LAST_SAVED_SERVICE:
            return self._execute_update_last_saved_service(analysis, primary_tc, context, maintenances)

        if (primary_tc and primary_tc.name == "get_service_history") or analysis.intent == IntentType.QUERY_HISTORY:
            return {"reply": analysis.reply_message, "action_type": "query_history", "state": context.state.value, "details": analysis.extracted_entities}

        if (primary_tc and primary_tc.name == "patch_vehicle_profile") or analysis.intent == IntentType.UPDATE_ODOMETER:
            return self._execute_patch_vehicle_profile(analysis, primary_tc, context, vehicles, clean_msg)

        if (primary_tc and primary_tc.name == "upsert_draft") or analysis.is_critical_action or analysis.intent in (
            IntentType.REGISTER_VEHICLE, IntentType.RECORD_MAINTENANCE, IntentType.UPSERT_DRAFT,
            IntentType.SET_REMINDER, IntentType.REGISTER_FINE, IntentType.RESET_GARAGE,
        ):
            return self._execute_upsert_draft(analysis, primary_tc, context, vehicles, clean_msg, resolved_ambiguous_vehicle)

        if not context.is_awaiting_confirmation():
            context.state = ConversationState.NORMAL
        return {"reply": analysis.reply_message, "action_type": analysis.intent.value, "state": context.state.value, "details": analysis.extracted_entities}

    def _execute_confirm_draft(self, analysis: GeminiAnalysisResponse, context: ConversationContext) -> Dict[str, Any]:
        if not context.draft_payload:
            context.state = ConversationState.NORMAL
            context.draft_updated_at = None
            return {"reply": analysis.reply_message, "action_type": "none", "state": context.state.value}

        committed = dict(context.draft_payload)
        state = context.state

        if committed.get("_is_multi_action"):
            committed["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
            committed["intent"] = "multi_action"
        elif state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM or committed.get("_action_state") == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value:
            committed["_action_state"] = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
            committed["intent"] = IntentType.REGISTER_VEHICLE.value
            committed.setdefault("odometer_km", committed.get("km"))
            committed.setdefault("km", committed.get("odometer_km"))
        elif state == ConversationState.AWAITING_MAINTENANCE_CONFIRM or committed.get("_action_state") == ConversationState.AWAITING_MAINTENANCE_CONFIRM.value:
            committed["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
            committed["intent"] = IntentType.RECORD_MAINTENANCE.value
            itens = committed.get("itens") or committed.get("components") or []
            itens = [itens] if isinstance(itens, str) else list(itens)
            committed["itens"] = itens
            committed["components"] = list(itens)
            if not committed.get("description"):
                committed["description"] = committed.get("service_summary") or (", ".join(itens) if itens else "Manutenção preventiva")
            committed.setdefault("km", committed.get("odometer_km"))
            committed.setdefault("odometer_km", committed.get("km"))
            committed.setdefault("total_cost", committed.get("valor"))
            committed.setdefault("workshop_name", committed.get("oficina"))
            committed.setdefault("date_str", committed.get("data"))
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
        else:
            committed.setdefault("intent", IntentType.RECORD_MAINTENANCE.value)

        if analysis.reminder:
            committed["chained_reminder"] = analysis.reminder.model_dump(exclude_none=True)
        elif analysis.extracted_entities and isinstance(analysis.extracted_entities.get("chained_reminder"), dict):
            committed["chained_reminder"] = analysis.extracted_entities["chained_reminder"]

        action_type = "action_confirmed_with_chained_reminder" if committed.get("chained_reminder") else "action_confirmed"
        context.state = ConversationState.NORMAL
        context.draft_payload = None
        context.draft_updated_at = None
        return {"reply": analysis.reply_message, "action_type": action_type, "state": context.state.value, "committed_data": committed, "details": committed}

    def _execute_upsert_draft(
        self,
        analysis: GeminiAnalysisResponse,
        tc: Optional[ToolCallAction],
        context: ConversationContext,
        vehicles: List[Vehicle],
        clean_msg: str,
        resolved_ambiguous_vehicle: Optional[Vehicle] = None,
    ) -> Dict[str, Any]:
        if len(analysis.actions) >= 2:
            target_ref_top = analysis.target_vehicle_ref or (analysis.actions[0].target_vehicle_ref if analysis.actions else None)
            target_v, ambiguous_candidates = (resolved_ambiguous_vehicle, []) if resolved_ambiguous_vehicle is not None else self._resolve_vehicle(target_ref_top, vehicles, clean_msg)
            if len(vehicles) > 1 and target_v is None and ambiguous_candidates:
                context.pending_ambiguous_message = clean_msg
                return {"reply": analysis.reply_message, "action_type": "vehicle_disambiguation_required", "state": context.state.value, "details": {}}

            actions_payload: List[Dict[str, Any]] = []
            for act in analysis.actions:
                act_dict = act.model_dump(exclude_none=True)
                act_v, _ = (resolved_ambiguous_vehicle, []) if resolved_ambiguous_vehicle is not None else self._resolve_vehicle(act.target_vehicle_ref, vehicles, clean_msg)
                if act_v and getattr(act_v, "id", None):
                    act_dict["vehicle_id"] = act_v.id
                    if getattr(act_v, "plate", None) and (resolved_ambiguous_vehicle is not None or not act_dict.get("target_vehicle_ref")):
                        act_dict["target_vehicle_ref"] = act_v.plate
                actions_payload.append(act_dict)

            multi_draft: Dict[str, Any] = {
                "intent": "multi_action", "_is_multi_action": True, "actions": actions_payload,
                "_action_state": ConversationState.AWAITING_MAINTENANCE_CONFIRM.value,
            }
            if target_v and getattr(target_v, "plate", None):
                multi_draft["_target_vehicle_ref"] = target_v.plate
            elif target_ref_top:
                multi_draft["_target_vehicle_ref"] = target_ref_top

            context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
            context.draft_payload = multi_draft
            context.draft_updated_at = self._utcnow()
            return {"reply": analysis.reply_message, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload, "details": multi_draft}

        is_vehicle_draft = (
            analysis.intent == IntentType.REGISTER_VEHICLE
            or (context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM and analysis.intent not in (IntentType.RECORD_MAINTENANCE, IntentType.SET_REMINDER, IntentType.REGISTER_FINE, IntentType.RESET_GARAGE))
            or (analysis.vehicle is not None and analysis.intent != IntentType.RECORD_MAINTENANCE)
        )

        if is_vehicle_draft:
            existing = dict(context.draft_payload) if context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM and isinstance(context.draft_payload, dict) else {}
            v_obj, ext = analysis.vehicle, analysis.extracted_entities or {}
            incoming: Dict[str, Any] = {}
            make_val = ((v_obj.make or v_obj.brand) if v_obj else None) or ((tc.make or tc.brand) if tc else None) or ext.get("make") or ext.get("brand")
            model_val = (v_obj.model if v_obj else None) or (tc.model if tc else None) or ext.get("model")
            version_val = (v_obj.version if v_obj else None) or (tc.version if tc else None) or ext.get("version")
            year_val = (v_obj.year if v_obj else None) or (tc.year if tc else None) or ext.get("year")
            plate_val = (v_obj.plate if v_obj else None) or (tc.plate if tc else None) or ext.get("plate")
            odo_val = (
                (v_obj.odometer_km if v_obj and v_obj.odometer_km is not None else (v_obj.odometer if v_obj else None))
                if v_obj and (v_obj.odometer_km is not None or v_obj.odometer is not None)
                else ((tc.odometer if tc.odometer is not None else tc.km) if tc and (tc.odometer is not None or tc.km is not None) else (ext.get("odometer_km") if ext.get("odometer_km") is not None else ext.get("odometer") if ext.get("odometer") is not None else ext.get("km")))
            )
            engine_val = (v_obj.engine or v_obj.motor if v_obj else None) or (tc.engine if tc else None) or ext.get("engine") or ext.get("motor")
            trans_val = (v_obj.transmission or v_obj.cambio if v_obj else None) or (tc.transmission if tc else None) or ext.get("transmission") or ext.get("cambio")
            steering_val = (v_obj.steering or v_obj.direcao if v_obj else None) or (tc.steering if tc else None) or ext.get("steering") or ext.get("direcao")
            vtype_val = (v_obj.vehicle_type.value if v_obj and v_obj.vehicle_type else None) or (tc.vehicle_type.value if tc and tc.vehicle_type else None) or ext.get("vehicle_type")
            fuel_val = (v_obj.fuel_type.value if v_obj and v_obj.fuel_type else None) or (tc.fuel_type.value if tc and tc.fuel_type else None) or ext.get("fuel_type")

            if make_val:
                incoming["make"] = incoming["brand"] = make_val
            if model_val:
                incoming["model"] = model_val
            if version_val:
                incoming["version"] = version_val
            if year_val is not None:
                incoming["year"] = year_val
            if plate_val:
                incoming["plate"] = str(plate_val).upper().replace("-", "").strip()
            if odo_val is not None:
                incoming["odometer_km"] = incoming["odometer"] = incoming["km"] = int(odo_val)
            if engine_val:
                incoming["engine"] = incoming["motor"] = engine_val
            if trans_val:
                incoming["transmission"] = incoming["cambio"] = trans_val
            if steering_val:
                incoming["steering"] = incoming["direcao"] = steering_val
            if vtype_val:
                incoming["vehicle_type"] = vtype_val
            if fuel_val:
                incoming["fuel_type"] = fuel_val

            merged = {**existing, **incoming}
            merged.setdefault("vehicle_type", VehicleType.CARRO.value)
            merged.setdefault("fuel_type", FuelType.FLEX.value)

            if not any(merged.get(k) is not None for k in ("make", "model", "plate", "odometer_km", "engine", "transmission", "steering", "year")) and not analysis.is_critical_action:
                return {"reply": analysis.reply_message, "action_type": IntentType.REGISTER_VEHICLE.value, "state": context.state.value, "details": merged}

            merged["_action_state"] = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value
            context.state = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
            context.draft_payload = merged
            context.draft_updated_at = self._utcnow()
            return {"reply": analysis.reply_message, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload, "details": merged}

        if analysis.intent == IntentType.SET_REMINDER:
            existing = dict(context.draft_payload) if context.state == ConversationState.AWAITING_REMINDER_CONFIRM and isinstance(context.draft_payload, dict) else {}
            rem_dict = analysis.reminder.model_dump(exclude_none=True) if analysis.reminder else {}
            merged = {**existing, **(analysis.extracted_entities or {}), **rem_dict}
            if not merged.get("task_description") and merged.get("description"):
                merged["task_description"] = merged["description"]
            target_v, _ = self._resolve_vehicle(analysis.target_vehicle_ref, vehicles, clean_msg)
            if target_v and getattr(target_v, "id", None) and not merged.get("vehicle_id"):
                merged["vehicle_id"] = target_v.id
            merged["_action_state"] = ConversationState.AWAITING_REMINDER_CONFIRM.value
            context.state = ConversationState.AWAITING_REMINDER_CONFIRM
            context.draft_payload = merged
            context.draft_updated_at = self._utcnow()
            return {"reply": analysis.reply_message, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload, "details": merged}

        if analysis.intent == IntentType.REGISTER_FINE:
            existing = dict(context.draft_payload) if context.state == ConversationState.AWAITING_FINE_CONFIRM and isinstance(context.draft_payload, dict) else {}
            fine_dict = analysis.fine.model_dump(exclude_none=True) if analysis.fine else {}
            merged = {**existing, **(analysis.extracted_entities or {}), **fine_dict}
            target_v, _ = self._resolve_vehicle(analysis.target_vehicle_ref, vehicles, clean_msg)
            if target_v and getattr(target_v, "id", None) and not merged.get("vehicle_id"):
                merged["vehicle_id"] = target_v.id
            merged["_action_state"] = ConversationState.AWAITING_FINE_CONFIRM.value
            context.state = ConversationState.AWAITING_FINE_CONFIRM
            context.draft_payload = merged
            context.draft_updated_at = self._utcnow()
            return {"reply": analysis.reply_message, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload, "details": merged}

        if analysis.intent == IntentType.RESET_GARAGE:
            if not vehicles or not analysis.is_critical_action:
                return {"reply": analysis.reply_message, "action_type": IntentType.RESET_GARAGE.value, "state": context.state.value, "details": {}}
            reset_draft = {"intent": IntentType.RESET_GARAGE.value, "_reset_garage": True, "_action_state": ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value}
            context.state = ConversationState.AWAITING_GARAGE_RESET_CONFIRM
            context.draft_payload = reset_draft
            context.draft_updated_at = self._utcnow()
            return {"reply": analysis.reply_message, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload, "details": reset_draft}

        existing = dict(context.draft_payload) if context.state == ConversationState.AWAITING_MAINTENANCE_CONFIRM and isinstance(context.draft_payload, dict) else {}
        m_obj, ext = analysis.maintenance, analysis.extracted_entities or {}

        if existing.get("_is_multi_action") and isinstance(existing.get("actions"), list):
            valor_corr = (tc.valor if tc and tc.valor is not None else None) if (tc and tc.valor is not None) else (m_obj.total_cost if (m_obj and m_obj.total_cost is not None) else (ext.get("valor") if ext.get("valor") is not None else ext.get("total_cost")))
            km_corr = (tc.km if tc and tc.km is not None else None) if (tc and tc.km is not None) else (m_obj.odometer_km if (m_obj and m_obj.odometer_km is not None) else (ext.get("km") if ext.get("km") is not None else ext.get("odometer_km")))
            for act in existing["actions"]:
                if "maintenance" in str(act.get("intent", "")).lower():
                    if valor_corr is not None:
                        act["total_cost"] = act["valor"] = float(valor_corr)
                    if km_corr is not None:
                        act["odometer_km"] = act["km"] = int(km_corr)
            context.draft_payload = existing
            context.draft_updated_at = self._utcnow()
            return {"reply": analysis.reply_message, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload, "details": existing}

        if not vehicles and not existing:
            return {"reply": analysis.reply_message, "action_type": "needs_vehicle", "state": context.state.value, "details": {}}

        prev_items: List[str] = list(existing.get("itens") or existing.get("components") or [])
        new_items: List[str] = []
        if tc and tc.itens:
            new_items.extend(tc.itens)
        elif m_obj and m_obj.components:
            new_items.extend(m_obj.components)
        elif ext.get("itens") or ext.get("components"):
            raw_ext = ext.get("itens") or ext.get("components")
            new_items.extend(raw_ext if isinstance(raw_ext, list) else [str(raw_ext)])

        merged_items = list(prev_items)
        for item in new_items:
            item_clean = str(item).strip()
            if item_clean and not any(item_clean.lower() == p.lower() for p in merged_items):
                merged_items.append(item_clean)

        km_val = (tc.km if tc and tc.km is not None else None) if (tc and tc.km is not None) else (m_obj.odometer_km if (m_obj and m_obj.odometer_km is not None) else (ext.get("km") if ext.get("km") is not None else ext.get("odometer_km")))
        data_val = (tc.data if tc and tc.data else None) or (m_obj.date_str if m_obj and m_obj.date_str else None) or ext.get("data") or ext.get("date_str")
        oficina_val = (tc.oficina if tc and tc.oficina else None) or (m_obj.workshop_name if m_obj and m_obj.workshop_name else None) or ext.get("oficina") or ext.get("workshop_name")
        valor_val = (tc.valor if tc and tc.valor is not None else None) if (tc and tc.valor is not None) else (m_obj.total_cost if (m_obj and m_obj.total_cost is not None) else (ext.get("valor") if ext.get("valor") is not None else ext.get("total_cost")))
        summary_val = (m_obj.service_summary if m_obj and m_obj.service_summary else None) or ext.get("service_summary") or ext.get("description")

        if not merged_items and km_val is None and valor_val is None and not summary_val and not existing:
            return {"reply": analysis.reply_message, "action_type": analysis.intent.value, "state": context.state.value, "details": ext}

        target_ref = (tc.target_vehicle_ref if tc and tc.target_vehicle_ref else None) or analysis.target_vehicle_ref or existing.get("_target_vehicle_ref")
        target_v, ambiguous_candidates = (resolved_ambiguous_vehicle, []) if resolved_ambiguous_vehicle is not None else self._resolve_vehicle(target_ref, vehicles, clean_msg)

        if target_v and getattr(target_v, "id", None):
            existing["vehicle_id"] = target_v.id
            existing["_target_vehicle_ref"] = getattr(target_v, "plate", None) or f"{target_v.make} {target_v.model}"
            context.active_vehicle_id = target_v.id

        merged_maint = dict(existing)
        if merged_items:
            merged_maint["itens"] = merged_maint["components"] = merged_items
        if summary_val or merged_items:
            merged_maint["service_summary"] = merged_maint["description"] = summary_val or ", ".join(merged_items)
        if km_val is not None:
            merged_maint["km"] = merged_maint["odometer_km"] = int(km_val)
        if data_val:
            merged_maint["data"] = merged_maint["date_str"] = data_val
        if oficina_val:
            merged_maint["oficina"] = merged_maint["workshop_name"] = oficina_val
        if valor_val is not None:
            merged_maint["valor"] = merged_maint["total_cost"] = float(valor_val)
        if ext.get("_is_odometer_rollback"):
            merged_maint["_is_odometer_rollback"] = True

        if len(vehicles) > 1 and not merged_maint.get("vehicle_id") and ambiguous_candidates:
            context.pending_ambiguous_message = clean_msg
            merged_maint["_target_vehicle_ref"] = f"AMBIGUOUS:{target_ref or ''}"
            merged_maint["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
            context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
            context.draft_payload = merged_maint
            context.draft_updated_at = self._utcnow()
            return {"reply": analysis.reply_message, "action_type": "vehicle_disambiguation_required", "state": context.state.value, "draft": context.draft_payload, "details": merged_maint}

        merged_maint["_action_state"] = ConversationState.AWAITING_MAINTENANCE_CONFIRM.value
        context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
        context.draft_payload = merged_maint
        context.draft_updated_at = self._utcnow()
        return {"reply": analysis.reply_message, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload, "details": merged_maint}

    def _execute_patch_vehicle_profile(
        self,
        analysis: GeminiAnalysisResponse,
        tc: Optional[ToolCallAction],
        context: ConversationContext,
        vehicles: List[Vehicle],
        clean_msg: str,
    ) -> Dict[str, Any]:
        ext, v_obj = analysis.extracted_entities or {}, analysis.vehicle
        new_km = (
            (tc.km if tc and tc.km is not None else (tc.odometer if tc and tc.odometer is not None else None))
            if tc and (tc.km is not None or tc.odometer is not None)
            else (v_obj.odometer_km if (v_obj and v_obj.odometer_km is not None) else (analysis.maintenance.odometer_km if (analysis.maintenance and analysis.maintenance.odometer_km is not None) else (ext.get("km") if ext.get("km") is not None else ext.get("odometer_km"))))
        )
        target_ref = (tc.target_vehicle_ref if tc and tc.target_vehicle_ref else None) or analysis.target_vehicle_ref
        target_v, ambiguous = self._resolve_vehicle(target_ref, vehicles, clean_msg)
        if target_v is None and len(vehicles) == 1:
            target_v = vehicles[0]
        if target_v is None and ambiguous:
            return {"reply": analysis.reply_message, "action_type": "vehicle_disambiguation_required", "state": context.state.value, "details": {}}

        if target_v is not None and new_km is not None and int(new_km) >= 0:
            if analysis.intent == IntentType.UNKNOWN:
                return {"reply": analysis.reply_message, "action_type": IntentType.UNKNOWN.value, "state": context.state.value, "details": {}}
            old_km = getattr(target_v, "current_km", None) or 0
            if int(new_km) >= old_km:
                target_v.current_km = int(new_km)
            committed = {
                "intent": IntentType.UPDATE_ODOMETER.value, "_is_odometer_update": True,
                "vehicle_id": getattr(target_v, "id", None),
                "_target_vehicle_ref": target_ref or getattr(target_v, "model", None),
                "old_km": old_km, "new_km": int(new_km), "odometer_km": int(new_km), "km": int(new_km),
            }
            return {"reply": analysis.reply_message, "action_type": "odometer_updated", "state": context.state.value, "committed_data": committed, "details": committed}

        return {"reply": analysis.reply_message, "action_type": analysis.intent.value, "state": context.state.value, "details": ext}

    def _execute_update_last_saved_service(
        self,
        analysis: GeminiAnalysisResponse,
        tc: Optional[ToolCallAction],
        context: ConversationContext,
        maintenances: List[Any],
    ) -> Dict[str, Any]:
        if not maintenances:
            return {"reply": analysis.reply_message, "action_type": "none", "state": context.state.value, "details": {}}
        target_service = maintenances[0]
        service_id = (tc.service_id if tc and tc.service_id is not None else None) or getattr(target_service, "id", None)
        if service_id is not None:
            for m in maintenances:
                if str(getattr(m, "id", "")) == str(service_id):
                    target_service = m
                    break
        m_obj, ext = analysis.maintenance, analysis.extracted_entities or {}
        new_itens = (tc.itens if tc and tc.itens else None) or (m_obj.components if m_obj and m_obj.components else None) or ext.get("itens")
        new_km = (tc.km if tc and tc.km is not None else None) if (tc and tc.km is not None) else (m_obj.odometer_km if (m_obj and m_obj.odometer_km is not None) else ext.get("km"))
        new_data = (tc.data if tc and tc.data else None) or (m_obj.date_str if m_obj and m_obj.date_str else None) or ext.get("data")
        new_oficina = (tc.oficina if tc and tc.oficina else None) or (m_obj.workshop_name if m_obj and m_obj.workshop_name else None) or ext.get("oficina")
        new_valor = (tc.valor if tc and tc.valor is not None else None) if (tc and tc.valor is not None) else (m_obj.total_cost if (m_obj and m_obj.total_cost is not None) else ext.get("valor"))

        if not any(v is not None for v in (new_itens, new_km, new_data, new_oficina, new_valor)):
            context.pending_edit_service_id = service_id
            if ext.get("pending_edit_field"):
                context.pending_edit_field = ext["pending_edit_field"]
            return {"reply": analysis.reply_message, "action_type": "awaiting_edit_value", "state": context.state.value, "details": {"service_id": service_id}}

        context.pending_edit_field = None
        context.pending_edit_service_id = None
        if new_itens:
            merged_items = list(getattr(target_service, "components", None) or [])
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
                target_service.date = datetime.strptime(new_data, "%d/%m/%Y").date()
            except ValueError:
                pass

        committed = {
            "intent": IntentType.UPDATE_LAST_SAVED_SERVICE.value, "service_id": getattr(target_service, "id", service_id),
            "itens": getattr(target_service, "components", []), "components": getattr(target_service, "components", []),
            "description": getattr(target_service, "description", ""), "km": getattr(target_service, "km", None),
            "data": new_data, "oficina": getattr(target_service, "workshop_name", None), "valor": getattr(target_service, "total_cost", 0.0),
        }
        return {"reply": analysis.reply_message, "action_type": "update_last_saved_service", "state": context.state.value, "committed_data": committed, "details": committed}
