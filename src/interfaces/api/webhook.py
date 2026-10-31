"""
AutoCare V2 - WhatsApp Webhook Router
Ponto de entrada assíncrono e linear para eventos e mensagens de WhatsApp.
Pipeline AI-First:
[Webhook WhatsApp] -> [Carrega Contexto Curto + Veículos/Histórico] -> [ConversationService/LLM] -> [Persistência Direta] -> [Disparo Outbound]
"""

import asyncio
import logging
import re
from datetime import datetime, date, timedelta, time
from typing import Dict, Any, Optional, Tuple, List
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.config.clock import now_brt_naive
from src.infrastructure.database.session import get_async_session, async_session_factory
from src.infrastructure.database.repositories import (
    UserRepository,
    VehicleRepository,
    MaintenanceRepository,
    ReminderRepository,
    TrafficFineRepository,
    soft_delete_last_user_record,
)
from src.application.services.conversation import (
    ConversationService,
    UNDO_SUCCESS_REPLY,
    UNDO_NOTHING_REPLY,
)
from src.domain.entities import Vehicle, MaintenanceRecord, Reminder, TrafficFine
from src.domain.enums import VehicleType, FuelType, UsageProfile

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

logger = logging.getLogger("autocare.webhook")
router = APIRouter(tags=["Webhook"])
conversation_service = ConversationService()


class MessageDebouncer:
    """
    Gerenciador de Debounce e Consolidação de rajadas de mensagens no WhatsApp.
    Agrupa mensagens enviadas pelo mesmo usuário em uma janela curta de espera configurável
    (WEBHOOK_DEBOUNCE_SECONDS), evitando respostas duplicadas e race conditions no LLM.
    """

    def __init__(self, default_debounce_seconds: Optional[float] = None):
        self._default_debounce_seconds = default_debounce_seconds
        self._buffers: Dict[str, List[str]] = {}
        self._futures: Dict[str, asyncio.Future] = {}
        self._timer_tasks: Dict[str, asyncio.Task] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    @property
    def debounce_seconds(self) -> float:
        if self._default_debounce_seconds is not None:
            return self._default_debounce_seconds
        return getattr(settings, "WEBHOOK_DEBOUNCE_SECONDS", 2.0)

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def enqueue(
        self,
        user_id: str,
        message_text: str,
        process_fn,
    ) -> Dict[str, Any]:
        """
        Adiciona a mensagem à fila de espera do usuário.
        Se debounce_seconds <= 0, processa imediatamente.
        Caso contrário, inicia ou reseta a janela de debounce.
        """
        if self.debounce_seconds <= 0:
            return await process_fn(user_id, message_text)

        lock = self._get_lock(user_id)
        async with lock:
            if user_id not in self._buffers:
                self._buffers[user_id] = []
            self._buffers[user_id].append(message_text)

            loop = asyncio.get_running_loop()
            if user_id not in self._futures or self._futures[user_id].done():
                self._futures[user_id] = loop.create_future()

            fut = self._futures[user_id]

            old_task = self._timer_tasks.get(user_id)
            if old_task and not old_task.done():
                old_task.cancel()

            task = asyncio.create_task(
                self._timer_worker(user_id, self.debounce_seconds, process_fn, fut)
            )
            self._timer_tasks[user_id] = task

        return await fut

    async def _timer_worker(
        self,
        user_id: str,
        delay: float,
        process_fn,
        fut: asyncio.Future,
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        lock = self._get_lock(user_id)
        async with lock:
            messages = self._buffers.pop(user_id, [])
            self._timer_tasks.pop(user_id, None)
            self._futures.pop(user_id, None)

        if not messages:
            if not fut.done():
                fut.set_result({"status": "ignored", "reason": "empty_buffer"})
            return

        consolidated_message = "\n".join(messages)
        try:
            res = await process_fn(user_id, consolidated_message)
            if not fut.done():
                fut.set_result(res)
        except Exception as exc:
            logger.error(f"Erro no processamento consolidado de {user_id}: {exc}", exc_info=True)
            if not fut.done():
                fut.set_exception(exc)


message_debouncer = MessageDebouncer()


def parse_whatsapp_payload(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Normaliza payloads de múltiplos gateways (Evolution API, Z-API ou testes diretos)."""
    if not payload:
        return None, None

    # 1. Payload de teste direto ({user_id, message} ou {user_id, text})
    if "user_id" in payload and ("message" in payload or "text" in payload):
        user_id = "".join(filter(str.isdigit, str(payload["user_id"])))
        raw_msg = payload.get("message") if "message" in payload else payload.get("text")
        return user_id, str(raw_msg).strip()

    # 2. Formato Evolution API
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = data.get("key", {}) if isinstance(data, dict) else {}
    if key.get("fromMe", False) or payload.get("fromMe", False):
        return None, None

    remote_jid = str(key.get("remoteJid", "") or payload.get("remoteJid", ""))
    if "@g.us" in remote_jid or payload.get("isGroup", False):
        return None, None

    if "@s.whatsapp.net" in remote_jid:
        user_id = "".join(filter(str.isdigit, remote_jid.split("@")[0]))
        msg_obj = data.get("message", {}) if isinstance(data, dict) else {}
        text = (
            msg_obj.get("conversation")
            or msg_obj.get("extendedTextMessage", {}).get("text")
            or msg_obj.get("buttonsResponseMessage", {}).get("selectedButtonId")
            or msg_obj.get("listResponseMessage", {}).get("singleSelectReply", {}).get("selectedRowId")
        )
        if text:
            return user_id, str(text).strip()

    # 3. Formato Z-API
    if payload.get("isGroup", False) or payload.get("fromMe", False):
        return None, None
    phone = payload.get("phone") or payload.get("senderPhone") or payload.get("connectedPhone")
    if phone:
        phone_str = str(phone)
        if "@g.us" in phone_str or "g.us" in str(payload.get("chatId", "")):
            return None, None
        user_id = "".join(filter(str.isdigit, phone_str.split("@")[0]))
        text_obj = payload.get("text", {})
        text = text_obj.get("message") if isinstance(text_obj, dict) else payload.get("message")
        if text:
            return user_id, str(text).strip()

    return None, None


def resolve_target_vehicle(
    vehicles: List[Vehicle],
    ref: Optional[str] = None,
    payload_hint: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Resolve o ID do veículo alvo por placa, modelo, tipo de veículo ou fuzzy matching."""
    if not vehicles:
        return None
    hint = payload_hint or {}
    if hint.get("vehicle_id") and any(v.id == hint["vehicle_id"] for v in vehicles):
        return hint["vehicle_id"]
    if len(vehicles) == 1:
        return vehicles[0].id

    ref_clean = (str(ref or "")).strip().lower()
    plate_hint = (str(hint.get("plate") or "")).strip().upper().replace("-", "")
    type_hint = (str(hint.get("vehicle_type") or "")).strip().lower()

    # 1. Correspondência exata ou substring por placa
    for v in vehicles:
        v_plate = (getattr(v, "plate", None) or getattr(v, "placa", "") or "").upper().replace("-", "").strip()
        if v_plate:
            if plate_hint and plate_hint == v_plate:
                return v.id
            if ref_clean and (v_plate.lower() in ref_clean or ref_clean in v_plate.lower()):
                return v.id

    # 2. Correspondência específica por modelo
    for v in vehicles:
        model_str = (getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
        if model_str:
            if model_str in ref_clean:
                return v.id
            for part in model_str.split():
                if len(part) >= 3 and part in ref_clean:
                    return v.id
            if "model" in hint and str(hint["model"]).lower() in model_str:
                return v.id

    # 3. Correspondência por tipo de veículo (moto vs carro)
    is_moto = any(m in ref_clean for m in ["moto", "motoca", "motocicleta", "cg", "fazer", "titan", "biz", "twister", "scooter"]) or type_hint == "moto"
    is_carro = any(c in ref_clean for c in ["carro", "automovel", "caranga", "veiculo", "caminhonete", "suv", "sedan", "hatch"]) or type_hint in ("carro", "caminhonete")

    if is_moto:
        for v in vehicles:
            v_type_str = str(getattr(v, "vehicle_type", None) or getattr(v, "tipo", "")).lower()
            if "moto" in v_type_str:
                return v.id

    if is_carro:
        for v in vehicles:
            v_type_str = str(getattr(v, "vehicle_type", None) or getattr(v, "tipo", "")).lower()
            if "carro" in v_type_str or "caminhonete" in v_type_str:
                return v.id

    # 4. Fuzzy Matching por similaridade aproximada
    import difflib
    ref_words = ref_clean.split()
    best_id = None
    best_score = 0.0
    for v in vehicles:
        model_str = (getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
        make_str = (getattr(v, "make", None) or getattr(v, "marca", "") or "").lower()
        targets = [model_str, make_str] + model_str.split() + make_str.split()
        for rw in ref_words:
            if len(rw) < 3:
                continue
            for target in targets:
                if not target or len(target) < 3:
                    continue
                ratio = difflib.SequenceMatcher(None, rw, target).ratio()
                if ratio > best_score and ratio >= 0.70:
                    best_score = ratio
                    best_id = v.id
    if best_id is not None:
        return best_id

    return vehicles[0].id


def _parse_due_datetime(raw_dt: Any, date_str: Optional[str] = None, explicit_time: Optional[str] = None) -> Optional[datetime]:
    """Converte e normaliza data/hora de vencimento para lembretes."""
    if isinstance(raw_dt, datetime):
        return raw_dt
    if isinstance(raw_dt, str):
        try:
            return datetime.fromisoformat(raw_dt)
        except Exception:
            pass

    hour, minute = 9, 0
    if explicit_time:
        tm_match = re.search(r"(\d{1,2}):(\d{2})|(\d{1,2})h", str(explicit_time))
        if tm_match:
            if tm_match.group(1):
                hour, minute = int(tm_match.group(1)), int(tm_match.group(2))
            elif tm_match.group(3):
                hour = int(tm_match.group(3))

    now = now_brt_naive()
    day_offset = 1 if "amanh" in str(date_str or "").lower() else 0
    return datetime.combine(now.date() + timedelta(days=day_offset), time(hour, minute))


def _parse_maintenance_date(raw_date: Any, default_date: date) -> date:
    """Extrai objeto date válido para registros de manutenção, com suporte a DD/MM/YYYY, ISO YYYY-MM-DD e variações."""
    if isinstance(raw_date, datetime):
        return raw_date.date()
    if isinstance(raw_date, date):
        return raw_date
    if not raw_date or not isinstance(raw_date, str):
        return default_date

    s = raw_date.strip()

    # 1. Formato ISO: YYYY-MM-DD
    m_iso = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", s)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return date(y, m, d)
        except ValueError:
            pass

    # 2. Formato Brasileiro: DD/MM/YYYY ou DD-MM-YYYY
    m_br = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\b", s)
    if m_br:
        d, m, y = int(m_br.group(1)), int(m_br.group(2)), int(m_br.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, m, d)
        except ValueError:
            pass

    # 3. Formato textual em português: ex: 15 de fevereiro de 2026
    months_map = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
        "outubro": 10, "novembro": 11, "dezembro": 12,
        "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
        "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
    }
    m_txt = re.search(r"(?:dia\s+)?(\d{1,2})\s+de\s+([a-zA-ZçÇ]+)(?:\s+de\s+(\d{4}))?", s, re.IGNORECASE)
    if m_txt:
        d_val = int(m_txt.group(1))
        m_name = m_txt.group(2).lower()
        y_val = int(m_txt.group(3)) if m_txt.group(3) else default_date.year
        m_num = months_map.get(m_name)
        if m_num and 1 <= d_val <= 31:
            try:
                return date(y_val, m_num, d_val)
            except ValueError:
                pass

    return default_date


async def _persist_confirmed_data(
    session: AsyncSession,
    user_id: str,
    action_type: str,
    committed_data: Dict[str, Any],
    vehicles: List[Vehicle],
    now_ts: datetime,
) -> None:
    """Centraliza a gravação atômica de ações confirmadas no banco de dados."""
    user_repo = UserRepository(session)
    vehicle_repo = VehicleRepository(session)
    maint_repo = MaintenanceRepository(session)
    reminder_repo = ReminderRepository(session)
    fine_repo = TrafficFineRepository(session)

    # 1. Reset Global do Sistema / Garagem
    if (
        action_type == "system_reset"
        or committed_data.get("_reset_system")
        or committed_data.get("_reset_garage")
        or committed_data.get("_action_state") == "awaiting_garage_reset_confirm"
    ):
        await user_repo.reset_user_data(user_id=user_id)
        return

    # 2. Multi-Ação em Lote (ex: Manutenção + Lembrete na mesma mensagem)
    if committed_data.get("_is_multi_action"):
        for act in committed_data.get("actions", []):
            act_intent = act.get("intent")
            target_veh_id = resolve_target_vehicle(vehicles, act.get("target_vehicle_ref"), act) or (vehicles[0].id if vehicles else None)

            if act_intent in ("record_maintenance", "RECORD_MAINTENANCE") and target_veh_id:
                km_val = act.get("km") or act.get("odometer_km")
                desc_val = act.get("description") or "Manutenção realizada"
                comp_val = act.get("components") or [desc_val]
                new_maint = MaintenanceRecord(
                    vehicle_id=target_veh_id,
                    user_id=user_id,
                    components=comp_val if isinstance(comp_val, list) else [str(comp_val)],
                    description=desc_val,
                    km=int(km_val) if km_val is not None else None,
                    total_cost=float(act.get("total_cost", 0.0) or 0.0),
                    workshop_name=act.get("workshop_name"),
                    created_at=now_ts,
                )
                await maint_repo.add_record(new_maint)
                if km_val:
                    await vehicle_repo.update_km(target_veh_id, int(km_val))

            elif act_intent in ("update_odometer", "UPDATE_ODOMETER") and target_veh_id:
                km_val = act.get("odometer_km") or act.get("km")
                if km_val:
                    await vehicle_repo.update_km(target_veh_id, int(km_val))

            elif act_intent in ("set_reminder", "SET_REMINDER"):
                due_dt = _parse_due_datetime(
                    act.get("due_datetime") or act.get("due_date"),
                    date_str=act.get("target_date_str"),
                    explicit_time=act.get("explicit_time"),
                )
                new_rem = Reminder(
                    user_id=user_id,
                    vehicle_id=target_veh_id,
                    description=act.get("task_description") or act.get("description", "Lembrete automotivo"),
                    due_datetime=due_dt,
                    due_km=act.get("due_km") or act.get("target_km"),
                    created_at=now_ts,
                )
                await reminder_repo.add_reminder(new_rem)
        return

    # 3. Cadastro / Atualização Atômica de Veículo (PATCH)
    action_state = committed_data.get("_action_state", "")
    if (
        action_state == "awaiting_vehicle_registration_confirm"
        or ("make" in committed_data and "model" in committed_data and "components" not in committed_data)
        or ("plate" in committed_data and "engine" in committed_data and "components" not in committed_data)
    ):
        km_val = committed_data.get("current_km") or committed_data.get("odometer_km") or committed_data.get("km")

        target_veh_id = None
        if committed_data.get("vehicle_id"):
            target_veh_id = committed_data["vehicle_id"]
        elif committed_data.get("plate"):
            existing_p = await vehicle_repo.get_by_plate(user_id, committed_data["plate"])
            if existing_p:
                target_veh_id = existing_p.id

        if not target_veh_id and vehicles:
            ref_model = (committed_data.get("model") or "").strip().lower()
            if ref_model:
                for v in vehicles:
                    v_model = (getattr(v, "model", None) or "").strip().lower()
                    if v_model and (v_model in ref_model or ref_model in v_model):
                        target_veh_id = v.id
                        break
            elif len(vehicles) == 1:
                target_veh_id = vehicles[0].id

        if target_veh_id:
            await vehicle_repo.patch_vehicle_profile(
                target_veh_id,
                make=committed_data.get("make"),
                model=committed_data.get("model"),
                version=committed_data.get("version"),
                year=committed_data.get("year"),
                plate=committed_data.get("plate"),
                engine=committed_data.get("engine") or committed_data.get("motor"),
                transmission=committed_data.get("transmission") or committed_data.get("cambio"),
                steering=committed_data.get("steering") or committed_data.get("direcao"),
                current_km=int(km_val) if km_val is not None else None,
                vehicle_type=committed_data.get("vehicle_type"),
                fuel_type=committed_data.get("fuel_type"),
                usage_profile=committed_data.get("usage_profile"),
            )
            return

        new_vehicle = Vehicle(
            user_id=user_id,
            make=committed_data.get("make", "Desconhecido"),
            model=committed_data.get("model", "Veículo"),
            version=committed_data.get("version"),
            year=committed_data.get("year"),
            plate=committed_data.get("plate"),
            engine=committed_data.get("engine") or committed_data.get("motor"),
            transmission=committed_data.get("transmission") or committed_data.get("cambio"),
            steering=committed_data.get("steering") or committed_data.get("direcao"),
            current_km=int(km_val) if km_val is not None else None,
            vehicle_type=VehicleType(committed_data.get("vehicle_type", "carro")) if committed_data.get("vehicle_type") in [e.value for e in VehicleType] else VehicleType.CARRO,
            fuel_type=FuelType(committed_data.get("fuel_type", "flex")) if committed_data.get("fuel_type") in [e.value for e in FuelType] else FuelType.FLEX,
            usage_profile=UsageProfile(committed_data.get("usage_profile", "convencional")) if committed_data.get("usage_profile") in [e.value for e in UsageProfile] else UsageProfile.CONVENCIONAL,
            created_at=now_ts,
        )
        await vehicle_repo.add_vehicle(new_vehicle)
        return

    # 4. Agendamento de Lembrete
    if (
        action_state == "awaiting_reminder_confirm"
        or "task_description" in committed_data
        or ("description" in committed_data and ("due_date" in committed_data or "due_km" in committed_data or "due_datetime" in committed_data))
    ):
        target_veh_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)
        due_dt = _parse_due_datetime(
            committed_data.get("due_datetime") or committed_data.get("due_date"),
            date_str=committed_data.get("target_date_str"),
            explicit_time=committed_data.get("explicit_time"),
        )
        new_rem = Reminder(
            user_id=user_id,
            vehicle_id=target_veh_id,
            description=committed_data.get("task_description") or committed_data.get("description", "Lembrete automotivo"),
            due_datetime=due_dt,
            due_km=committed_data.get("due_km"),
            created_at=now_ts,
        )
        await reminder_repo.add_reminder(new_rem)
        return

    # 5. Multa / Infração de Trânsito
    if action_state == "awaiting_fine_confirm" or "infraction_code" in committed_data or "amount" in committed_data:
        target_veh_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data) or (vehicles[0].id if vehicles else None)
        if target_veh_id:
            amt = committed_data.get("amount", 0.0)
            new_fine = TrafficFine(
                vehicle_id=target_veh_id,
                description=committed_data.get("description", "Infração de Trânsito"),
                infraction_code=committed_data.get("infraction_code"),
                issuing_body=committed_data.get("issuing_body"),
                points=int(committed_data.get("points", 0) or 0),
                amount=float(amt or 0.0),
                discounted_amount=float(committed_data.get("discounted_amount", 0.0) or 0.0),
                created_at=now_ts,
            )
            await fine_repo.add_fine(new_fine)
        return

    # 6. Registro de Manutenção
    if (
        action_state == "awaiting_maintenance_confirm"
        or "components" in committed_data
        or "total_cost" in committed_data
        or "service_summary" in committed_data
        or "itens" in committed_data
    ):
        target_veh_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)
        if target_veh_id:
            km_val = committed_data.get("km") or committed_data.get("odometer_km")
            desc_val = committed_data.get("description") or committed_data.get("service_summary") or "Manutenção realizada"
            comp_val = committed_data.get("itens") or committed_data.get("components") or [desc_val]
            cost_val = committed_data.get("valor") if committed_data.get("valor") is not None else committed_data.get("total_cost", 0.0)
            parsed_date = _parse_maintenance_date(
                committed_data.get("data") or committed_data.get("date_str") or committed_data.get("date"),
                now_ts.date(),
            )
            new_maint = MaintenanceRecord(
                vehicle_id=target_veh_id,
                user_id=user_id,
                components=comp_val if isinstance(comp_val, list) else [str(comp_val)],
                description=desc_val,
                km=int(km_val) if km_val is not None else None,
                date=parsed_date,
                total_cost=float(cost_val or 0.0),
                parts_cost=float(committed_data.get("parts_cost", 0.0) or 0.0),
                labor_cost=float(committed_data.get("labor_cost", 0.0) or 0.0),
                workshop_name=committed_data.get("oficina") or committed_data.get("workshop_name"),
                parts_brand=committed_data.get("parts_brand"),
                created_at=now_ts,
            )
            await maint_repo.add_record(new_maint)
            if new_maint.km:
                await vehicle_repo.update_km(target_veh_id, new_maint.km)

    # 7. Atualização Isolada de Odômetro
    elif committed_data.get("_is_odometer_update") or "odometer_km" in committed_data:
        target_veh_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)
        km_val = committed_data.get("odometer_km") or committed_data.get("km")
        if target_veh_id and km_val:
            await vehicle_repo.update_km(target_veh_id, int(km_val))

    # 8. Lembrete Encadeado (Chained Reminder)
    if committed_data.get("chained_reminder"):
        c_rem = committed_data["chained_reminder"]
        target_veh_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)
        target_dt = _parse_due_datetime(None, date_str=c_rem.get("target_date_str"), explicit_time=c_rem.get("explicit_time"))
        new_rem = Reminder(
            user_id=user_id,
            vehicle_id=target_veh_id,
            description=c_rem.get("task_description", "Revisão preventiva"),
            due_datetime=target_dt,
            data_hora_agendada=target_dt,
            created_at=now_ts,
        )
        await reminder_repo.add_reminder(new_rem)

        if target_veh_id:
            try:
                from src.repositories.lembrete_repository import LembreteRepository
                from src.schemas.lembrete import LembreteCreate
                await LembreteRepository(session).create(
                    LembreteCreate(
                        veiculo_id=target_veh_id,
                        descricao=c_rem.get("task_description", "Revisão preventiva"),
                        data_hora_agendada=target_dt,
                        status="agendado" if target_dt else "pendente",
                    )
                )
            except Exception as e_lem:
                logger.warning(f"Sincronização relacional de lembretes ignorada: {e_lem}")


async def send_whatsapp_reply(user_id: str, message: str) -> bool:
    """Dispara mensagem outbound via WhatsApp Gateway configurado com fallback silencioso."""
    if not message or not HAS_HTTPX:
        return False
    try:
        if settings.PROVIDER == "evolution" and settings.EVOLUTION_BASE_URL and settings.EVOLUTION_API_KEY:
            url = f"{settings.EVOLUTION_BASE_URL.rstrip('/')}/message/sendText/{settings.EVOLUTION_INSTANCE}"
            headers = {"apikey": settings.EVOLUTION_API_KEY, "Content-Type": "application/json"}
            body = {"number": user_id, "text": message, "delay": settings.EVOLUTION_TYPING_DELAY_MS}
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=body, headers=headers)
            return True
        elif settings.PROVIDER == "zapi" and settings.ZAPI_INSTANCE_ID and settings.ZAPI_TOKEN:
            url = f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE_ID}/token/{settings.ZAPI_TOKEN}/send-text"
            headers = {"Content-Type": "application/json"}
            if settings.ZAPI_CLIENT_TOKEN:
                headers["Client-Token"] = settings.ZAPI_CLIENT_TOKEN
            body = {"phone": user_id, "message": message}
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=body, headers=headers)
            return True
    except Exception as e:
        logger.warning(f"Gateway WhatsApp outbound indisponível: {e}")
    return False


async def _process_webhook_pipeline(
    session: AsyncSession,
    user_id: str,
    message_text: str,
) -> Dict[str, Any]:
    """Executa a lógica completa de processamento conversacional, persistência e resposta."""
    logger.info(f"Processando mensagem consolidada de [{user_id}]: {message_text}")
    await UserRepository(session).get_or_create(user_id=user_id)

    vehicles = await VehicleRepository(session).get_vehicles_by_user_id(user_id=user_id)
    maintenances = await MaintenanceRepository(session).get_by_user_id(user_id=user_id)
    reminders = await ReminderRepository(session).get_by_user_id(user_id=user_id)
    fines = await TrafficFineRepository(session).get_by_user_id(user_id=user_id)

    result = await conversation_service.handle_incoming_message(
        user_id=user_id,
        message_text=message_text,
        vehicles=vehicles,
        maintenances=maintenances,
        reminders=reminders,
        fines=fines,
    )

    action_type = result.get("action_type")
    committed_data = result.get("committed_data")
    now_ts = conversation_service._utcnow()

    # 1. Persistência de Ação Confirmada ou Reset
    if action_type in ("action_confirmed", "system_reset", "action_confirmed_with_chained_reminder") and committed_data:
        try:
            await _persist_confirmed_data(session, user_id, action_type, committed_data, vehicles, now_ts)
        except Exception as err:
            logger.error(f"Erro ao persistir ação confirmada: {err}", exc_info=True)

    # 2. Edição Direta do Último Serviço Salvo (`update_last_saved_service`)
    elif action_type == "update_last_saved_service" and committed_data:
        try:
            srv_id = committed_data.get("service_id")
            if srv_id:
                await MaintenanceRepository(session).update_maintenance_record(
                    service_id=str(srv_id),
                    components=committed_data.get("itens") or committed_data.get("components"),
                    km=committed_data.get("km"),
                    date_val=committed_data.get("data"),
                    workshop_name=committed_data.get("oficina") or committed_data.get("workshop_name"),
                    total_cost=committed_data.get("valor") if committed_data.get("valor") is not None else committed_data.get("total_cost"),
                    description=committed_data.get("description"),
                )
        except Exception as err:
            logger.error(f"Erro ao atualizar último serviço: {err}", exc_info=True)

    # 3. Atualização Direta de Odômetro
    elif action_type in ("update_odometer", "odometer_updated") or (committed_data and committed_data.get("_is_odometer_update")):
        try:
            details = result.get("details") or committed_data or {}
            target_veh_id = (
                resolve_target_vehicle(vehicles, details.get("_target_vehicle_ref") or details.get("target_vehicle_ref"), details)
                or details.get("vehicle_id")
                or (vehicles[0].id if vehicles else None)
            )
            new_km = details.get("odometer_km") or details.get("km")
            if target_veh_id and new_km:
                await VehicleRepository(session).update_km(target_veh_id, int(new_km))
        except Exception as err:
            logger.error(f"Erro ao atualizar odômetro: {err}", exc_info=True)

    # 4. Escape Global (Soft delete do último registro nos últimos 5 minutos)
    elif action_type == "undo_last_record":
        try:
            cancelled = await soft_delete_last_user_record(session, user_id, now=now_ts)
            total = len(cancelled["maintenance_ids"]) + len(cancelled["reminder_ids"])
            result["reply"] = UNDO_SUCCESS_REPLY if total else UNDO_NOTHING_REPLY
        except Exception as err:
            logger.error(f"Erro ao desfazer último registro: {err}", exc_info=True)
            result["reply"] = "Não consegui apagar o último registro agora. Pode tentar de novo em instantes?"
        conversation_service.replace_last_assistant_turn(user_id, result["reply"])

    # 5. Disparo outbound transparente
    reply_text = result.get("reply")
    if reply_text:
        await send_whatsapp_reply(user_id, reply_text)

    return {
        "status": "success",
        "user_id": user_id,
        "reply": reply_text,
        "action_type": action_type,
        "state": result.get("state"),
    }


@router.get("/webhook", status_code=status.HTTP_200_OK)
@router.get("/webhook/whatsapp", status_code=status.HTTP_200_OK)
@router.get("/zapi", status_code=status.HTTP_200_OK)
@router.get("/evolution", status_code=status.HTTP_200_OK)
async def webhook_health_or_challenge() -> Dict[str, str]:
    """Validação de integridade e handshake HTTP para gateways de webhook."""
    return {"status": "ok", "service": "AutoCare V2 Webhook", "provider": settings.PROVIDER}


@router.post("/webhook", status_code=status.HTTP_200_OK)
@router.post("/webhook/whatsapp", status_code=status.HTTP_200_OK)
@router.post("/zapi", status_code=status.HTTP_200_OK)
@router.post("/webhook/zapi", status_code=status.HTTP_200_OK)
@router.post("/evolution", status_code=status.HTTP_200_OK)
@router.post("/webhook/evolution", status_code=status.HTTP_200_OK)
@router.post("/messages-upsert", status_code=status.HTTP_200_OK)
@router.post("/webhook/messages-upsert", status_code=status.HTTP_200_OK)
async def receive_webhook(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """
    Pipeline linear AI-First com Debounce Buffer anti-atropelo:
    Recebe mensagem -> Bufferiza mensagens consecutivas -> Executa Pipeline no LLM -> Responde.
    """
    user_id, message_text = parse_whatsapp_payload(payload)
    if not user_id or not message_text:
        return {"status": "ignored", "reason": "non_actionable_event"}

    async def _runner(uid: str, msg: str) -> Dict[str, Any]:
        if isinstance(session, AsyncSession) and session.is_active:
            return await _process_webhook_pipeline(session, uid, msg)
        async with async_session_factory() as local_session:
            return await _process_webhook_pipeline(local_session, uid, msg)

    return await message_debouncer.enqueue(user_id, message_text, _runner)
