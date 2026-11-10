"""
AutoCare V2 - WhatsApp Webhook Router
Ponto de entrada assíncrono e linear para eventos e mensagens de WhatsApp.
Pipeline AI-First:
[Webhook WhatsApp] -> [Carrega Contexto Curto + Veículos/Histórico] -> [ConversationService/LLM] -> [Persistência Direta] -> [Disparo Outbound]
"""

import asyncio
import hashlib
import logging
import re
import weakref
from datetime import datetime, date, timedelta, time, timezone
from typing import Dict, Any, Optional, Tuple, List
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.config.clock import now_brt_naive
from src.infrastructure.database.session import async_session_factory
from src.infrastructure.http_client import shared_http_client, TIMEOUT_WHATSAPP_OUTBOUND
from src.infrastructure.rate_limiter import SlidingWindowRateLimiter
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
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @property
    def debounce_seconds(self) -> float:
        if self._default_debounce_seconds is not None:
            return self._default_debounce_seconds
        return getattr(settings, "WEBHOOK_DEBOUNCE_SECONDS", 2.0)

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def enqueue(
        self,
        user_id: str,
        message_text: str,
        process_fn,
    ) -> Dict[str, Any]:
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
                self._timer_worker(user_id, self.debounce_seconds, process_fn, fut, lock)
            )
            self._timer_tasks[user_id] = task

        return await fut

    async def _timer_worker(
        self,
        user_id: str,
        delay: float,
        process_fn,
        fut: asyncio.Future,
        lock: Optional[asyncio.Lock] = None,
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        active_lock = lock or self._get_lock(user_id)
        async with active_lock:
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

MAX_MESSAGE_CHARS = 2000


def _extract_non_empty_text(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text else None


def _sanitize_message_text(raw: Any, user_id: Optional[str] = None) -> Optional[str]:
    text = _extract_non_empty_text(raw)
    if not text:
        return None
    if len(text) > MAX_MESSAGE_CHARS:
        logger.warning(f"Mensagem de [{user_id}] truncada de {len(text)} para {MAX_MESSAGE_CHARS} caracteres")
        text = text[:MAX_MESSAGE_CHARS]
    return text


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_whatsapp_payload(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Normaliza payloads de múltiplos gateways (Evolution API, Z-API ou testes diretos)."""
    if not payload:
        return None, None

    if "user_id" in payload and ("message" in payload or "text" in payload):
        user_id = "".join(filter(str.isdigit, str(payload["user_id"])))
        raw_msg = payload.get("message") if "message" in payload else payload.get("text")
        return user_id, _extract_non_empty_text(raw_msg)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = _as_dict(data.get("key")) if isinstance(data, dict) else {}
    if key.get("fromMe", False) or payload.get("fromMe", False):
        return None, None

    remote_jid = str(key.get("remoteJid", "") or payload.get("remoteJid", "") or "")
    if "@g.us" in remote_jid or payload.get("isGroup", False):
        return None, None

    if "@s.whatsapp.net" in remote_jid:
        user_id = "".join(filter(str.isdigit, remote_jid.split("@")[0]))
        msg_obj = _as_dict(data.get("message")) if isinstance(data, dict) else {}
        text = (
            msg_obj.get("conversation")
            or _as_dict(msg_obj.get("extendedTextMessage")).get("text")
            or _as_dict(msg_obj.get("buttonsResponseMessage")).get("selectedButtonId")
            or _as_dict(_as_dict(msg_obj.get("listResponseMessage")).get("singleSelectReply")).get("selectedRowId")
        )
        clean = _extract_non_empty_text(text)
        if clean:
            return user_id, clean

    if payload.get("isGroup", False) or payload.get("fromMe", False):
        return None, None
    phone = payload.get("phone") or payload.get("senderPhone") or payload.get("connectedPhone")
    if phone:
        phone_str = str(phone)
        if "@g.us" in phone_str or "g.us" in str(payload.get("chatId", "")):
            return None, None
        user_id = "".join(filter(str.isdigit, phone_str.split("@")[0]))
        text_obj = payload.get("text")
        text = text_obj.get("message") if isinstance(text_obj, dict) else payload.get("message")
        clean = _extract_non_empty_text(text)
        if clean:
            return user_id, clean

    return None, None


def resolve_target_vehicle(
    vehicles: List[Vehicle],
    ref: Optional[str] = None,
    payload_hint: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """
    Resolve o ID do veículo alvo com ordem determinística:
    (1) placa exata -> (2) make + model ou model exato -> (3) fuzzy difflib ratio >= 0.85 ->
    (4) se >= 2 veículos empatarem sem placa desambiguadora, retorna None.
    """
    if not vehicles:
        return None
    if isinstance(ref, str) and ref.startswith("AMBIGUOUS:"):
        return None
    hint = payload_hint or {}
    if hint.get("vehicle_id") and any(v.id == hint["vehicle_id"] for v in vehicles):
        return hint["vehicle_id"]
    if len(vehicles) == 1:
        return vehicles[0].id

    ref_clean = (str(ref or "")).strip().lower()
    plate_hint = (str(hint.get("plate") or "")).strip().upper().replace("-", "").replace(" ", "")
    type_hint = (str(hint.get("vehicle_type") or "")).strip().lower()
    model_hint = (str(hint.get("model") or "")).strip().lower()

    for v in vehicles:
        v_plate = v.plate_alias if isinstance(v, Vehicle) else (getattr(v, "plate", None) or getattr(v, "placa", "") or "").upper().replace("-", "").strip()
        if v_plate:
            if plate_hint and plate_hint == v_plate:
                return v.id
            if ref_clean and (v_plate.lower() in ref_clean or ref_clean == v_plate.lower()):
                return v.id

    exact_matches: List[Vehicle] = []
    query_candidates = [q for q in (ref_clean, model_hint) if q]
    for v in vehicles:
        model_str = v.model_alias if isinstance(v, Vehicle) else (getattr(v, "model", None) or getattr(v, "modelo", "") or "").strip().lower()
        make_str = (getattr(v, "make", None) or getattr(v, "marca", "") or "").strip().lower()
        full_name = f"{make_str} {model_str}".strip()
        if not model_str:
            continue
        matched = False
        for q in query_candidates:
            if q == full_name or q == model_str or model_str in q:
                matched = True
                break
            for part in model_str.split():
                if len(part) >= 3 and part in q.split():
                    matched = True
                    break
        if matched:
            exact_matches.append(v)

    if len(exact_matches) == 1:
        return exact_matches[0].id
    if len(exact_matches) >= 2:
        return None

    is_moto = any(m in ref_clean for m in ["moto", "motoca", "motocicleta", "cg", "fazer", "titan", "biz", "twister", "scooter"]) or type_hint == "moto"
    is_carro = any(c in ref_clean for c in ["carro", "automovel", "caranga", "veiculo", "caminhonete", "suv", "sedan", "hatch"]) or type_hint in ("carro", "caminhonete")

    if is_moto:
        moto_matches = [v for v in vehicles if "moto" in str(getattr(v, "vehicle_type", None) or getattr(v, "tipo", "")).lower()]
        if len(moto_matches) == 1:
            return moto_matches[0].id
        if len(moto_matches) >= 2:
            return None

    if is_carro:
        carro_matches = [
            v for v in vehicles
            if any(t in str(getattr(v, "vehicle_type", None) or getattr(v, "tipo", "")).lower() for t in ("carro", "caminhonete"))
        ]
        if len(carro_matches) == 1:
            return carro_matches[0].id
        if len(carro_matches) >= 2:
            return None

    import difflib
    ref_words = ref_clean.split()
    fuzzy_scored: List[Tuple[Vehicle, float]] = []
    for v in vehicles:
        model_str = (getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
        make_str = (getattr(v, "make", None) or getattr(v, "marca", "") or "").lower()
        targets = [model_str, make_str] + model_str.split() + make_str.split()
        best_v_score = 0.0
        for rw in ref_words:
            if len(rw) < 3:
                continue
            for target in targets:
                if not target or len(target) < 3:
                    continue
                ratio = difflib.SequenceMatcher(None, rw, target).ratio()
                if ratio > best_v_score:
                    best_v_score = ratio
        if best_v_score >= 0.85:
            fuzzy_scored.append((v, best_v_score))

    if len(fuzzy_scored) == 1:
        return fuzzy_scored[0][0].id
    if len(fuzzy_scored) >= 2:
        return None

    model_aliases = [v.model_alias if isinstance(v, Vehicle) else (getattr(v, "model", "") or "").strip().lower() for v in vehicles]
    if len(set(model_aliases)) < len(model_aliases):
        return None

    return vehicles[0].id


_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b")
_TEXTUAL_DATE_RE = re.compile(r"(?:dia\s+)?(\d{1,2})\s+de\s+([a-zA-ZçÇ]+)(?:\s+de\s+(\d{4}))?", re.IGNORECASE)

_MONTHS_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def _parse_due_datetime(raw_dt: Any, date_str: Optional[str] = None, explicit_time: Optional[str] = None) -> Optional[datetime]:
    """
    Converte e normaliza data/hora de vencimento para lembretes.
    Data inexistente no calendário devolve None para o chamador abortar a gravação e reabrir o rascunho.
    Retorna sempre datetime UTC-aware.
    """
    if isinstance(raw_dt, datetime):
        return raw_dt if raw_dt.tzinfo is not None else raw_dt.replace(tzinfo=timezone.utc)
    if isinstance(raw_dt, str):
        try:
            dt_iso = datetime.fromisoformat(raw_dt)
            return dt_iso if dt_iso.tzinfo is not None else dt_iso.replace(tzinfo=timezone.utc)
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
    today = now.date()

    for candidate in (date_str, raw_dt if isinstance(raw_dt, str) else None):
        if not candidate:
            continue
        cand_str = str(candidate).strip()

        m = _NUMERIC_DATE_RE.search(cand_str)
        if m:
            day, month, year_raw = int(m.group(1)), int(m.group(2)), m.group(3)
            year = today.year
            if year_raw:
                year = int(year_raw)
                if year < 100:
                    year += 2000
            try:
                target = date(year, month, day)
                if not year_raw and target < today:
                    target = date(year + 1, month, day)
            except ValueError:
                return None
            return datetime.combine(target, time(hour, minute), tzinfo=timezone.utc)

        m_txt = _TEXTUAL_DATE_RE.search(cand_str)
        if m_txt:
            day = int(m_txt.group(1))
            month_num = _MONTHS_PT.get(m_txt.group(2).lower())
            if not month_num:
                return None
            year = int(m_txt.group(3)) if m_txt.group(3) else today.year
            try:
                target = date(year, month_num, day)
                if not m_txt.group(3) and target < today:
                    target = date(year + 1, month_num, day)
            except ValueError:
                return None
            return datetime.combine(target, time(hour, minute), tzinfo=timezone.utc)

    day_offset = 1 if "amanh" in str(date_str or "").lower() else 0
    return datetime.combine(today + timedelta(days=day_offset), time(hour, minute), tzinfo=timezone.utc)


def _parse_maintenance_date(raw_date: Any, default_date: date) -> Optional[date]:
    """
    Extrai e valida a data de um registro de manutenção:
    - Vazio / "hoje" -> default_date; "ontem"/"anteontem" -> relativo ao default.
    - Expressões relativas passadas ("semana passada", "mês passado", "ano passado", "há X dias/semanas/meses/anos").
    - ISO, DD/MM/YYYY, DD-MM-YYYY, DD/MM e data textual em português.
    - Data explícita porém inexistente no calendário -> None (tratado como invalid_date).
    """
    if isinstance(raw_date, datetime):
        return raw_date.date()
    if isinstance(raw_date, date):
        return raw_date
    if not raw_date or not isinstance(raw_date, str) or not raw_date.strip():
        return default_date

    s = raw_date.strip()
    low = s.lower()
    if low == "hoje":
        return default_date
    if low == "ontem":
        return default_date - timedelta(days=1)
    if low == "anteontem":
        return default_date - timedelta(days=2)
    if "semana passada" in low:
        return default_date - timedelta(days=7)
    if "mês passado" in low or "mes passado" in low:
        return default_date - timedelta(days=30)
    if "ano passado" in low:
        return default_date - timedelta(days=365)

    m_rel = re.search(
        r"(?:h[aá]\s+)?(\d+|um|uma)\s*(dia|dias|semana|semanas|m[eê]s|meses|ano|anos)(?:\s+atr[aá]s)?",
        low,
    )
    if m_rel and ("há" in low or "ha " in low or "atrás" in low or "atras" in low):
        qty_raw = m_rel.group(1)
        qty = 1 if qty_raw in ("um", "uma") else int(qty_raw)
        unit = m_rel.group(2)
        if unit.startswith("dia"):
            return default_date - timedelta(days=qty)
        if unit.startswith("semana"):
            return default_date - timedelta(days=qty * 7)
        if unit.startswith("m"):
            return default_date - timedelta(days=qty * 30)
        if unit.startswith("ano"):
            return default_date - timedelta(days=qty * 365)

    m_iso = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", s)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            return None

    m_br = re.search(r"\b(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?\b", s)
    if m_br:
        d_i, m_i = int(m_br.group(1)), int(m_br.group(2))
        y_i = int(m_br.group(3)) if m_br.group(3) else default_date.year
        if y_i < 100:
            y_i += 2000
        try:
            return date(y_i, m_i, d_i)
        except ValueError:
            return None

    m_txt = re.search(r"(?:dia\s+)?(\d{1,2})\s+de\s+([a-zA-ZçÇ]+)(?:\s+de\s+(\d{4}))?", s, re.IGNORECASE)
    if m_txt:
        m_num = _MONTHS_PT.get(m_txt.group(2).lower())
        if m_num:
            y_val = int(m_txt.group(3)) if m_txt.group(3) else default_date.year
            try:
                return date(y_val, m_num, int(m_txt.group(1)))
            except ValueError:
                return None

    return default_date


PersistOutcome = Dict[str, List[Any]]


def _new_outcome() -> PersistOutcome:
    return {"success": [], "failed": []}


def _ok(outcome: PersistOutcome, label: str) -> None:
    outcome["success"].append(label)


def _fail(outcome: PersistOutcome, label: str, reason: str, **extra: Any) -> None:
    outcome["failed"].append({"item": label, "reason": reason, **extra})


def _maint_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza os aliases do rascunho de manutenção (itens/components, valor/total_cost, ...)."""
    sub = data.get("maintenance") if isinstance(data.get("maintenance"), dict) else {}
    desc_val = data.get("description") or sub.get("description") or data.get("service_summary") or sub.get("service_summary") or "Manutenção realizada"
    comp_val = data.get("itens") or data.get("components") or sub.get("components") or [desc_val]
    cost_val = data.get("valor") if data.get("valor") is not None else (data.get("total_cost") if data.get("total_cost") is not None else (sub.get("valor") if sub.get("valor") is not None else sub.get("total_cost", 0.0)))
    raw_date_val = (
        data.get("data")
        or data.get("date_str")
        or data.get("date")
        or data.get("service_date")
        or data.get("data_manutencao")
        or sub.get("data")
        or sub.get("date_str")
        or sub.get("date")
        or sub.get("service_date")
        or sub.get("data_manutencao")
    )
    return {
        "km": data.get("km") or data.get("odometer_km") or sub.get("odometer_km") or sub.get("km"),
        "description": desc_val,
        "components": comp_val if isinstance(comp_val, list) else [str(comp_val)],
        "cost": float(cost_val or 0.0),
        "raw_date": raw_date_val,
        "workshop": data.get("oficina") or data.get("workshop_name") or sub.get("workshop_name"),
    }


def check_vehicle_timeline_coherence(
    existing_maints: List[Any],
    target_veh: Optional[Any],
    event_date: date,
    event_km: int,
    today_date: date,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Posiciona o par (event_date, event_km) dentro da linha do tempo completa do veículo
    (histórico de manutenções registradas + leitura atual hoje / veiculo.km_atual).

    Retorna (is_coherent, conflict_dict):
    - Se for coerente: (True, None)
    - Se for incoerente: (False, {
        "conflict_date": date,
        "conflict_km": int,
        "direction": "anterior" | "posterior",
        "message": str
    })
    """
    past_points: List[Tuple[date, int]] = []
    for m in (existing_maints or []):
        m_date = getattr(m, "date", None)
        m_km = getattr(m, "km", None)
        if m_date is not None and m_km is not None:
            d_val = m_date.date() if isinstance(m_date, datetime) else m_date
            past_points.append((d_val, int(m_km)))

    current_veh_km = getattr(target_veh, "current_km", None) if target_veh else None

    # 1. Trava contra odômetro regressivo para a data de hoje ou futura:
    # Se a data for hoje ou futura, a quilometragem NÃO pode ser menor que o km_atual registrado do veículo.
    if current_veh_km is not None and event_date >= today_date and event_km < current_veh_km:
        c_date_str = today_date.strftime("%d/%m/%Y")
        e_date_str = event_date.strftime("%d/%m/%Y")
        c_km_str = f"{current_veh_km:,} km".replace(",", ".")
        e_km_str = f"{event_km:,} km".replace(",", ".")
        msg = (
            f"Notei que em {c_date_str} o veículo estava com {c_km_str}. "
            f"Confirma que em {e_date_str} a quilometragem era {e_km_str} ou deseja corrigir a data/km?"
        )
        return False, {
            "conflict_date": today_date,
            "conflict_km": current_veh_km,
            "direction": "anterior",
            "message": msg,
        }

    # 2. Limites da linha do tempo baseados no histórico de manutenções:
    # Busca registros imediatamente anterior e posterior a event_date
    anterior_maints = [(d, km) for d, km in past_points if d <= event_date]
    posterior_maints = [(d, km) for d, km in past_points if d >= event_date]

    if anterior_maints:
        max_ant_date = max(d for d, _ in anterior_maints)
        km_anterior = max(km for d, km in anterior_maints if d == max_ant_date)
        if event_km < km_anterior:
            c_date_str = max_ant_date.strftime("%d/%m/%Y")
            e_date_str = event_date.strftime("%d/%m/%Y")
            c_km_str = f"{km_anterior:,} km".replace(",", ".")
            e_km_str = f"{event_km:,} km".replace(",", ".")
            msg = (
                f"Notei que em {c_date_str} o veículo estava com {c_km_str}. "
                f"Confirma que em {e_date_str} a quilometragem era {e_km_str} ou deseja corrigir a data/km?"
            )
            return False, {
                "conflict_date": max_ant_date,
                "conflict_km": km_anterior,
                "direction": "anterior",
                "message": msg,
            }

    if posterior_maints:
        min_post_date = min(d for d, _ in posterior_maints)
        km_posterior = min(km for d, km in posterior_maints if d == min_post_date)
        if event_km > km_posterior:
            c_date_str = min_post_date.strftime("%d/%m/%Y")
            e_date_str = event_date.strftime("%d/%m/%Y")
            c_km_str = f"{km_posterior:,} km".replace(",", ".")
            e_km_str = f"{event_km:,} km".replace(",", ".")
            msg = (
                f"Notei que em {c_date_str} o veículo estava com {c_km_str}. "
                f"Confirma que em {e_date_str} a quilometragem era {e_km_str} ou deseja corrigir a data/km?"
            )
            return False, {
                "conflict_date": min_post_date,
                "conflict_km": km_posterior,
                "direction": "posterior",
                "message": msg,
            }

    # 3. Verificação global contra qualquer registro no futuro com KM menor ou no passado com KM maior
    for d, km in past_points:
        if d > event_date and event_km > km:
            c_date_str = d.strftime("%d/%m/%Y")
            e_date_str = event_date.strftime("%d/%m/%Y")
            c_km_str = f"{km:,} km".replace(",", ".")
            e_km_str = f"{event_km:,} km".replace(",", ".")
            msg = (
                f"Notei que em {c_date_str} o veículo estava com {c_km_str}. "
                f"Confirma que em {e_date_str} a quilometragem era {e_km_str} ou deseja corrigir a data/km?"
            )
            return False, {
                "conflict_date": d,
                "conflict_km": km,
                "direction": "posterior",
                "message": msg,
            }
        if d < event_date and event_km < km:
            c_date_str = d.strftime("%d/%m/%Y")
            e_date_str = event_date.strftime("%d/%m/%Y")
            c_km_str = f"{km:,} km".replace(",", ".")
            e_km_str = f"{event_km:,} km".replace(",", ".")
            msg = (
                f"Notei que em {c_date_str} o veículo estava com {c_km_str}. "
                f"Confirma que em {e_date_str} a quilometragem era {e_km_str} ou deseja corrigir a data/km?"
            )
            return False, {
                "conflict_date": d,
                "conflict_km": km,
                "direction": "anterior",
                "message": msg,
            }

    return True, None


async def _persist_maintenance(
    maint_repo: MaintenanceRepository,
    vehicle_repo: VehicleRepository,
    user_id: str,
    vehicle_id: Optional[int],
    data: Dict[str, Any],
    now_ts: datetime,
    outcome: PersistOutcome,
) -> None:
    f = _maint_fields(data)
    label = f"Manutenção: {', '.join(f['components'])}"
    if not vehicle_id:
        _fail(outcome, label, "vehicle_not_found")
        return
    parsed_date = _parse_maintenance_date(f["raw_date"], now_ts.date())
    if parsed_date is None:
        _fail(outcome, label, "invalid_date", raw_date=str(f["raw_date"]))
        return
    target_veh = await vehicle_repo.get_by_id(vehicle_id)
    if f["km"] is not None:
        event_km = int(f["km"])
        today_date = now_ts.date()
        is_forced = bool(
            data.get("force")
            or data.get("force_timeline")
            or data.get("confirmed_divergence")
            or data.get("_allow_regressive_km")
        )
        if not is_forced:
            existing_maints = await maint_repo.get_by_vehicle_id(vehicle_id)
            is_coherent, conflict = check_vehicle_timeline_coherence(
                existing_maints=existing_maints,
                target_veh=target_veh,
                event_date=parsed_date,
                event_km=event_km,
                today_date=today_date,
            )
            if not is_coherent and conflict:
                _fail(
                    outcome,
                    label,
                    "timeline_incoherence",
                    conflict_date=conflict["conflict_date"].strftime("%d/%m/%Y"),
                    conflict_km=conflict["conflict_km"],
                    event_date=parsed_date.strftime("%d/%m/%Y"),
                    event_km=event_km,
                    message=conflict["message"],
                    direction=conflict["direction"],
                )
                return

    new_maint = MaintenanceRecord(
        vehicle_id=vehicle_id,
        user_id=user_id,
        components=f["components"],
        description=f["description"],
        km=int(f["km"]) if f["km"] is not None else None,
        date=parsed_date,
        total_cost=f["cost"],
        parts_cost=float(data.get("parts_cost", 0.0) or 0.0),
        labor_cost=float(data.get("labor_cost", 0.0) or 0.0),
        workshop_name=f["workshop"],
        parts_brand=data.get("parts_brand"),
        created_at=now_ts,
    )
    await maint_repo.add_record(new_maint, commit=False)
    # 4. Protege km_atual: nunca regride km_atual em manutenções retroativas coerentes
    # Só atualiza km_atual se o novo registro representar a maior leitura (km > current_km)
    if new_maint.km is not None:
        if (
            target_veh is None
            or target_veh.current_km is None
            or new_maint.km > target_veh.current_km
        ):
            await vehicle_repo.update_km(vehicle_id, new_maint.km, commit=False)
    _ok(outcome, label)


async def _persist_reminder(
    reminder_repo: ReminderRepository,
    user_id: str,
    vehicle_id: Optional[int],
    description: str,
    raw_due: Any,
    date_str: Optional[str],
    explicit_time: Optional[str],
    due_km: Any,
    now_ts: datetime,
    outcome: PersistOutcome,
) -> None:
    label = f"Lembrete: {description}"
    due_dt = _parse_due_datetime(raw_due, date_str=date_str, explicit_time=explicit_time)
    if due_dt is None:
        _fail(outcome, label, "invalid_date", raw_date=str(date_str or raw_due))
        return
    await reminder_repo.add_reminder(
        Reminder(
            user_id=user_id,
            vehicle_id=vehicle_id,
            description=description,
            due_datetime=due_dt,
            data_hora_agendada=due_dt,
            due_km=int(due_km) if due_km else None,
            created_at=now_ts,
        ),
        commit=False,
    )
    _ok(outcome, label)


async def _persist_chained_reminder(
    session: AsyncSession,
    user_id: str,
    committed_data: Dict[str, Any],
    vehicles: List[Vehicle],
    now_ts: datetime,
    outcome: PersistOutcome,
) -> None:
    """Persiste lembrete encadeado junto com a ação principal."""
    c_rem = committed_data.get("chained_reminder")
    if not c_rem:
        return
    description = c_rem.get("task_description", "Revisão preventiva")
    ref = str(committed_data.get("_target_vehicle_ref") or "")
    if ref.startswith("AMBIGUOUS:"):
        _fail(outcome, f"Lembrete: {description}", "ambiguous_vehicle")
        return
    target_veh_id = resolve_target_vehicle(vehicles, ref, committed_data)
    raw_due = c_rem.get("due_datetime") or c_rem.get("due_date")
    date_str = c_rem.get("target_date_str") or c_rem.get("data") or c_rem.get("date_str")
    await _persist_reminder(
        ReminderRepository(session), user_id, target_veh_id, description,
        raw_due, date_str, c_rem.get("explicit_time"), c_rem.get("due_km"),
        now_ts, outcome,
    )


async def _persist_multi_actions(
    session: AsyncSession,
    user_id: str,
    committed_data: Dict[str, Any],
    vehicles: List[Vehicle],
    now_ts: datetime,
    outcome: PersistOutcome,
) -> None:
    maint_repo = MaintenanceRepository(session)
    vehicle_repo = VehicleRepository(session)
    reminder_repo = ReminderRepository(session)
    for act in committed_data.get("actions", []):
        raw_intent = act.get("intent")
        act_intent = (raw_intent.value if hasattr(raw_intent, "value") else str(raw_intent or "")).strip().lower()
        if act_intent.startswith("intenttype."):
            act_intent = act_intent.split(".", 1)[1]
        label = act.get("description") or act.get("task_description") or act_intent or "ação"
        try:
            ref = str(act.get("target_vehicle_ref") or "")
            if ref.startswith("AMBIGUOUS:"):
                _fail(outcome, str(label), "ambiguous_vehicle")
                continue
            target_veh_id = resolve_target_vehicle(vehicles, ref, act)
            if "record_maintenance" in act_intent or act_intent == "maintenance":
                await _persist_maintenance(maint_repo, vehicle_repo, user_id, target_veh_id, act, now_ts, outcome)
            elif "update_odometer" in act_intent or act_intent == "odometer":
                km_val = act.get("odometer_km") or act.get("km")
                if target_veh_id and km_val:
                    await vehicle_repo.update_km(target_veh_id, int(km_val), commit=False)
                    _ok(outcome, f"Hodômetro: {int(km_val):,} km".replace(",", "."))
                else:
                    _fail(outcome, str(label), "vehicle_not_found" if not target_veh_id else "missing_km")
            elif "set_reminder" in act_intent or act_intent == "reminder":
                await _persist_reminder(
                    reminder_repo, user_id, target_veh_id,
                    act.get("task_description") or act.get("description", "Lembrete automotivo"),
                    act.get("due_datetime") or act.get("due_date"),
                    act.get("target_date_str") or act.get("data") or act.get("date_str"),
                    act.get("explicit_time"), act.get("due_km") or act.get("target_km"), now_ts, outcome,
                )
            else:
                _fail(outcome, str(label), "unsupported_action")
        except Exception as err:
            logger.error(f"Falha ao persistir ação '{label}': {err}", exc_info=True)
            _fail(outcome, str(label), "persistence_error")


async def _persist_single_action(
    session: AsyncSession,
    user_id: str,
    committed_data: Dict[str, Any],
    vehicles: List[Vehicle],
    now_ts: datetime,
    outcome: PersistOutcome,
) -> None:
    vehicle_repo = VehicleRepository(session)
    maint_repo = MaintenanceRepository(session)
    reminder_repo = ReminderRepository(session)
    fine_repo = TrafficFineRepository(session)
    action_state = committed_data.get("_action_state", "")
    ref = str(committed_data.get("_target_vehicle_ref") or "")
    if ref.startswith("AMBIGUOUS:"):
        _fail(outcome, "Veículo", "ambiguous_vehicle")
        return

    # 3. Cadastro / Atualização Atômica de Veículo (PATCH)
    if (
        action_state == "awaiting_vehicle_registration_confirm"
        or ("make" in committed_data and "model" in committed_data and "components" not in committed_data)
        or ("plate" in committed_data and "engine" in committed_data and "components" not in committed_data)
    ):
        label = f"Veículo: {committed_data.get('make', '')} {committed_data.get('model', '')}".strip()
        raw_plate = committed_data.get("plate")
        if raw_plate and str(raw_plate).strip().lower() not in ("a cadastrar", "none", "null", ""):
            clean_plate = re.sub(r"[\s\-]", "", str(raw_plate)).upper()
            if not re.match(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$", clean_plate):
                _fail(outcome, label, "invalid_plate", raw_plate=str(raw_plate))
                return
            committed_data["plate"] = clean_plate

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
                matched_by_model = [
                    v for v in vehicles
                    if v.model_alias and (v.model_alias in ref_model or ref_model in v.model_alias)
                ]
                if len(matched_by_model) == 1:
                    target_veh_id = matched_by_model[0].id
                elif len(matched_by_model) >= 2:
                    _fail(outcome, f"Veículo: {committed_data.get('model')}", "ambiguous_vehicle")
                    return
            elif len(vehicles) == 1:
                target_veh_id = vehicles[0].id

        if target_veh_id:
            await vehicle_repo.patch_vehicle_profile(
                target_veh_id,
                commit=False,
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
        else:
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
            await vehicle_repo.add_vehicle(new_vehicle, commit=False)
        _ok(outcome, label)
        return

    # 4. Agendamento de Lembrete
    if (
        action_state == "awaiting_reminder_confirm"
        or "task_description" in committed_data
        or ("description" in committed_data and ("due_date" in committed_data or "due_km" in committed_data or "due_datetime" in committed_data or "target_date_str" in committed_data))
    ):
        target_veh_id = resolve_target_vehicle(vehicles, ref, committed_data)
        await _persist_reminder(
            reminder_repo, user_id, target_veh_id,
            committed_data.get("task_description") or committed_data.get("description", "Lembrete automotivo"),
            committed_data.get("due_datetime") or committed_data.get("due_date"),
            committed_data.get("target_date_str") or committed_data.get("data") or committed_data.get("date_str"),
            committed_data.get("explicit_time"),
            committed_data.get("due_km"), now_ts, outcome,
        )
        return

    # 5. Multa / Infração de Trânsito
    if action_state == "awaiting_fine_confirm" or "infraction_code" in committed_data or "amount" in committed_data:
        target_veh_id = resolve_target_vehicle(vehicles, ref, committed_data)
        label = f"Multa: {committed_data.get('description', 'Infração de Trânsito')}"
        if not target_veh_id:
            _fail(outcome, label, "vehicle_not_found")
            return
        await fine_repo.add_fine(
            TrafficFine(
                vehicle_id=target_veh_id,
                description=committed_data.get("description", "Infração de Trânsito"),
                infraction_code=committed_data.get("infraction_code"),
                issuing_body=committed_data.get("issuing_body"),
                points=int(committed_data.get("points", 0) or 0),
                amount=float(committed_data.get("amount", 0.0) or 0.0),
                discounted_amount=float(committed_data.get("discounted_amount", 0.0) or 0.0),
                created_at=now_ts,
            ),
            commit=False,
        )
        _ok(outcome, label)
        return

    # 6. Registro de Manutenção
    if (
        action_state == "awaiting_maintenance_confirm"
        or "components" in committed_data
        or "total_cost" in committed_data
        or "service_summary" in committed_data
        or "itens" in committed_data
    ):
        target_veh_id = resolve_target_vehicle(vehicles, ref, committed_data)
        await _persist_maintenance(maint_repo, vehicle_repo, user_id, target_veh_id, committed_data, now_ts, outcome)

    # 7. Atualização Isolada de Odômetro
    elif committed_data.get("_is_odometer_update") or "odometer_km" in committed_data:
        target_veh_id = resolve_target_vehicle(vehicles, ref, committed_data)
        km_val = committed_data.get("odometer_km") or committed_data.get("km")
        if target_veh_id and km_val:
            await vehicle_repo.update_km(target_veh_id, int(km_val), commit=False)
            _ok(outcome, f"Hodômetro: {int(km_val):,} km".replace(",", "."))
        else:
            _fail(outcome, "Hodômetro", "vehicle_not_found" if not target_veh_id else "missing_km")


async def _persist_confirmed_data(
    session: AsyncSession,
    user_id: str,
    action_type: str,
    committed_data: Dict[str, Any],
    vehicles: List[Vehicle],
    now_ts: datetime,
) -> PersistOutcome:
    """
    Centraliza a gravação de ações confirmadas e garante atomicidade transacional de lote:
    se qualquer sub-ação falhar (outcome["failed"] não vazio), executa session.rollback()
    e descarta o lote inteiro; caso contrário, executa um único session.commit() ao final.
    """
    outcome = _new_outcome()
    try:
        if (
            action_type == "system_reset"
            or committed_data.get("_reset_system")
            or committed_data.get("_reset_garage")
            or committed_data.get("_action_state") == "awaiting_garage_reset_confirm"
        ):
            await UserRepository(session).reset_user_data(user_id=user_id)
            _ok(outcome, "Reset de dados")
            return outcome

        if committed_data.get("_is_multi_action"):
            await _persist_multi_actions(session, user_id, committed_data, vehicles, now_ts, outcome)
        else:
            await _persist_single_action(session, user_id, committed_data, vehicles, now_ts, outcome)

        await _persist_chained_reminder(session, user_id, committed_data, vehicles, now_ts, outcome)

        if outcome["failed"]:
            await session.rollback()
            outcome["success"] = []
        else:
            await session.commit()
    except Exception as err:
        logger.error(f"Erro ao persistir ação confirmada: {err}", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            pass
        outcome["success"] = []
        _fail(outcome, "Registro", "persistence_error")
    return outcome


_FAIL_REASONS_PT = {
    "vehicle_not_found": "não encontrei o veículo",
    "ambiguous_vehicle": "há mais de um veículo desse modelo na garagem (informe a placa)",
    "invalid_date": "a data informada não existe",
    "invalid_plate": "a placa informada não segue o padrão brasileiro (ABC-1234 ou Mercosul ABC1D23)",
    "regressive_km": "quilometragem inferior à atual do veículo para a data de hoje",
    "chronological_incoherence": "quilometragem incompatível com o histórico cronológico de manutenções",
    "timeline_incoherence": "quilometragem incompatível com o histórico cronológico de manutenções",
    "missing_km": "faltou a quilometragem",
    "unsupported_action": "tipo de ação não suportado",
    "persistence_error": "erro ao gravar no banco",
}


def _apply_persistence_outcome(
    result: Dict[str, Any],
    user_id: str,
    committed_data: Dict[str, Any],
    outcome: PersistOutcome,
) -> None:
    failed, success = outcome["failed"], outcome["success"]
    if not failed and not success:
        result["action_type"] = "no_op"
        result["reply"] = "Não identifiquei nenhuma ação para salvar."
        result["persistence"] = outcome
        return

    if not failed:
        return

    invalid_date = next((f for f in failed if f["reason"] == "invalid_date"), None)
    timeline_issue = next(
        (f for f in failed if f["reason"] in ("timeline_incoherence", "chronological_incoherence")),
        None,
    )
    if invalid_date:
        raw = invalid_date.get("raw_date", "")
        result["reply"] = (
            f"⚠️ Data inválida (*{raw}*): esse dia não existe no calendário. "
            "Me diga a data correta (ex.: *15/03/2026*) e eu salvo em seguida. Seus outros dados foram mantidos."
        )
        result["action_type"] = "invalid_date"
        has_restorable_content = bool(
            committed_data and any(
                k in committed_data for k in (
                    "task_description", "components", "itens", "description",
                    "km", "valor", "total_cost", "workshop_name", "oficina",
                    "make", "model", "plate", "infraction_code", "actions"
                )
            )
        )
        if has_restorable_content:
            conversation_service.reopen_draft_for_correction(
                user_id,
                committed_data,
                clear_keys=("data", "date_str", "date", "target_date_str", "due_date", "due_datetime"),
            )
    elif timeline_issue:
        msg = timeline_issue.get("message")
        if not msg:
            c_date = timeline_issue.get("conflict_date", "")
            c_km = timeline_issue.get("conflict_km", "")
            e_date = timeline_issue.get("event_date", "")
            e_km = timeline_issue.get("event_km", "")
            if c_date and c_km and e_date and e_km:
                c_km_str = f"{int(c_km):,} km".replace(",", ".") if str(c_km).isdigit() else f"{c_km} km"
                e_km_str = f"{int(e_km):,} km".replace(",", ".") if str(e_km).isdigit() else f"{e_km} km"
                msg = f"Notei que em {c_date} o veículo estava com {c_km_str}. Confirma que em {e_date} a quilometragem era {e_km_str} ou deseja corrigir a data/km?"
            else:
                msg = "Notei uma divergência na linha do tempo da quilometragem informada. Confirma os dados ou deseja corrigir a data/km?"
        result["reply"] = f"⚠️ {msg}"
        result["action_type"] = "timeline_divergence_verification"
        committed_copy = dict(committed_data)
        committed_copy["_pending_timeline_verification"] = True
        conversation_service.reopen_draft_for_correction(
            user_id,
            committed_copy,
            clear_keys=(),
        )
    else:
        lines = ["⚠️ *Salvei apenas parte das ações:*\n" if success else "⚠️ *Não consegui salvar:*\n"]
        lines += [f"✅ {s}" for s in success]
        lines += [f"❌ {f['item']} — {_FAIL_REASONS_PT.get(f['reason'], f['reason'])}" for f in failed]
        if any(f["reason"] == "vehicle_not_found" for f in failed):
            lines.append("\n🚗 Cadastre/identifique o veículo e me envie novamente os itens que falharam.")
        result["reply"] = "\n".join(lines)
        result["action_type"] = "action_partially_confirmed" if success else "action_failed"
    result["persistence"] = outcome
    conversation_service.replace_last_assistant_turn(user_id, result["reply"])


async def send_whatsapp_reply(user_id: str, message: str) -> bool:
    """Dispara mensagem outbound via WhatsApp Gateway configurado com fallback silencioso."""
    if not message or not HAS_HTTPX:
        return False
    try:
        if settings.PROVIDER == "evolution" and settings.EVOLUTION_BASE_URL and settings.EVOLUTION_API_KEY:
            url = f"{settings.EVOLUTION_BASE_URL.rstrip('/')}/message/sendText/{settings.EVOLUTION_INSTANCE}"
            headers = {"apikey": settings.EVOLUTION_API_KEY, "Content-Type": "application/json"}
            body = {"number": user_id, "text": message, "delay": settings.EVOLUTION_TYPING_DELAY_MS}
            async with shared_http_client(TIMEOUT_WHATSAPP_OUTBOUND) as client:
                await client.post(url, json=body, headers=headers, timeout=TIMEOUT_WHATSAPP_OUTBOUND)
            return True
        elif settings.PROVIDER == "zapi" and settings.ZAPI_INSTANCE_ID and settings.ZAPI_TOKEN:
            url = f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE_ID}/token/{settings.ZAPI_TOKEN}/send-text"
            headers = {"Content-Type": "application/json"}
            if settings.ZAPI_CLIENT_TOKEN:
                headers["Client-Token"] = settings.ZAPI_CLIENT_TOKEN
            body = {"phone": user_id, "message": message}
            async with shared_http_client(TIMEOUT_WHATSAPP_OUTBOUND) as client:
                await client.post(url, json=body, headers=headers, timeout=TIMEOUT_WHATSAPP_OUTBOUND)
            return True
    except Exception as e:
        logger.warning(f"Gateway WhatsApp outbound indisponível: {e}")
    return False


async def _process_webhook_pipeline(
    user_id: str,
    message_text: str,
    session: Optional[AsyncSession] = None,
    session_factory: Any = None,
) -> Dict[str, Any]:
    sanitized_text = _sanitize_message_text(message_text, user_id) or ""
    factory = session_factory if callable(session_factory) else async_session_factory
    if session is not None:
        try:
            return await _run_pipeline(session, user_id, sanitized_text)
        except Exception as e:
            if "closed" in str(e).lower():
                async with factory() as new_session:
                    return await _run_pipeline(new_session, user_id, sanitized_text)
            raise
    async with factory() as new_session:
        return await _run_pipeline(new_session, user_id, sanitized_text)


async def _run_pipeline(session: AsyncSession, user_id: str, message_text: str) -> Dict[str, Any]:
    message_text = _sanitize_message_text(message_text, user_id) or ""

    msg_hash = hashlib.sha256(message_text.encode("utf-8", errors="ignore")).hexdigest()[:8]
    logger.info("Processing user=%s msg_hash=%s len=%d", user_id, msg_hash, len(message_text))
    if getattr(settings, "DEBUG", False):
        logger.debug("Processing user=%s text=%s", user_id, message_text)

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

    if action_type in ("action_confirmed", "system_reset", "action_confirmed_with_chained_reminder") and committed_data:
        outcome = await _persist_confirmed_data(session, user_id, action_type, committed_data, vehicles, now_ts)
        _apply_persistence_outcome(result, user_id, committed_data, outcome)
        action_type = result.get("action_type")

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

    elif action_type in ("update_odometer", "odometer_updated") or (committed_data and committed_data.get("_is_odometer_update")):
        try:
            details = result.get("details") or committed_data or {}
            target_veh_id = (
                resolve_target_vehicle(vehicles, details.get("_target_vehicle_ref") or details.get("target_vehicle_ref"), details)
                or details.get("vehicle_id")
            )
            new_km = details.get("odometer_km") or details.get("km")
            if target_veh_id and new_km:
                await VehicleRepository(session).update_km(target_veh_id, int(new_km))
        except Exception as err:
            logger.error(f"Erro ao atualizar odômetro: {err}", exc_info=True)

    elif action_type == "undo_last_record":
        try:
            cancelled = await soft_delete_last_user_record(session, user_id, now=now_ts)
            total = len(cancelled["maintenance_ids"]) + len(cancelled["reminder_ids"])
            result["reply"] = UNDO_SUCCESS_REPLY if total else UNDO_NOTHING_REPLY
        except Exception as err:
            logger.error(f"Erro ao desfazer último registro: {err}", exc_info=True)
            result["reply"] = "Não consegui apagar o último registro agora. Pode tentar de novo em instantes?"
        conversation_service.replace_last_assistant_turn(user_id, result["reply"])

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


def get_session_factory():
    """Dependência FastAPI: devolve a fábrica de sessões (sobrescrevível em testes)."""
    return async_session_factory


_rate_limiter = SlidingWindowRateLimiter(max_events=0, window_seconds=60.0)


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
    session: Any = None,
    session_factory: Any = None,
) -> Dict[str, Any]:
    """
    Pipeline linear AI-First com Debounce Buffer anti-atropelo:
    Recebe mensagem -> Rate limit -> Bufferiza -> Trunca -> Executa Pipeline no LLM -> Responde.
    """
    user_id, message_text = parse_whatsapp_payload(payload)
    if not user_id or not message_text:
        return {"status": "ignored", "reason": "non_actionable_event"}

    rate_limit_enabled = getattr(settings, "AUTOCARE_FEATURE_RATE_LIMIT", True)
    rate_limit = getattr(settings, "WEBHOOK_RATE_LIMIT_PER_MIN", 120)
    if rate_limit_enabled and rate_limit > 0 and not _rate_limiter.allow(user_id, rate_limit):
        logger.warning(f"Rate limit excedido para [{user_id}]")
        return {"status": "ignored", "reason": "rate_limited"}

    resolved_factory = session_factory if callable(session_factory) else async_session_factory

    async def _runner(uid: str, msg: str) -> Dict[str, Any]:
        return await _process_webhook_pipeline(uid, msg, session=session, session_factory=resolved_factory)

    return await message_debouncer.enqueue(user_id, message_text, _runner)
