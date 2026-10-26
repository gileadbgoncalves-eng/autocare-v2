"""
AutoCare V2 - WhatsApp Webhook Router
Ponto de entrada assíncrono para eventos e mensagens de provedores de WhatsApp
(Evolution API, Z-API ou payloads diretos de teste).
"""

import logging
import re
from datetime import datetime, date, timedelta, time
from typing import Dict, Any, Optional, Tuple, List
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.config.clock import now_brt_naive
from src.infrastructure.database.session import get_async_session
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

# Instância única do serviço de conversação
conversation_service = ConversationService()


def parse_whatsapp_payload(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalizador de payloads para suporte nativo a múltiplos gateways WhatsApp:
    - Evolution API (messages.upsert)
    - Z-API (on-message-received)
    - Payload direto / testes locais ({user_id, message})
    """
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
    # Ignora mensagens enviadas pelo próprio bot (fromMe: true)
    if key.get("fromMe", False) or payload.get("fromMe", False):
        return None, None

    remote_jid = str(key.get("remoteJid", "") or payload.get("remoteJid", ""))
    # Ignora mensagens de grupos (@g.us)
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
    """
    Resolve o veículo alvo com base em placa, modelo específico, tipo de veículo (moto vs carro)
    ou identificador antes de recorrer ao fallback padrão.
    """
    if not vehicles:
        return None
    if len(vehicles) == 1:
        return vehicles[0].id

    ref_clean = (str(ref or "")).strip().lower()
    hint = payload_hint or {}
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

    # 2. Correspondência específica por MODELO (ex: 'CG 160', 'Civic', 'Golf', 'Corolla')
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

    # 3. Correspondência por TIPO DE VEÍCULO (ex: 'moto' vs 'carro')
    is_moto_ref = any(m in ref_clean for m in ["moto", "motoca", "motocicleta", "cg", "fazer", "titan", "biz", "twister", "scooter"]) or type_hint == "moto"
    is_carro_ref = any(c in ref_clean for c in ["carro", "automovel", "caranga", "veiculo", "caminhonete", "suv", "sedan", "hatch"]) or type_hint in ("carro", "caminhonete")

    if is_moto_ref:
        for v in vehicles:
            v_type = getattr(v, "vehicle_type", None) or getattr(v, "tipo", "")
            v_type_str = v_type.value if hasattr(v_type, "value") else str(v_type).lower()
            if "moto" in v_type_str:
                return v.id

    if is_carro_ref:
        for v in vehicles:
            v_type = getattr(v, "vehicle_type", None) or getattr(v, "tipo", "")
            v_type_str = v_type.value if hasattr(v_type, "value") else str(v_type).lower()
            if "carro" in v_type_str or "caminhonete" in v_type_str:
                return v.id

    # 4. Correspondência combinada make + model
    for v in vehicles:
        make_str = (getattr(v, "make", None) or getattr(v, "marca", "") or "").lower()
        model_str = (getattr(v, "model", None) or getattr(v, "modelo", "") or "").lower()
        if f"{make_str} {model_str}" in ref_clean:
            return v.id

    # 4.5 Correspondência por similaridade aproximada (Fuzzy Matching) para modelo/marca
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

    # 5. Fallback para o primeiro veículo registrado
    return vehicles[0].id


async def send_whatsapp_reply(user_id: str, message: str) -> bool:
    """
    Dispara mensagem de volta para o condutor via WhatsApp Gateway configurado (com fallback silencioso).
    """
    if not message or not HAS_HTTPX:
        return False

    try:
        if settings.PROVIDER == "evolution" and settings.EVOLUTION_BASE_URL and settings.EVOLUTION_API_KEY:
            url = f"{settings.EVOLUTION_BASE_URL.rstrip('/')}/message/sendText/{settings.EVOLUTION_INSTANCE}"
            headers = {
                "apikey": settings.EVOLUTION_API_KEY,
                "Content-Type": "application/json",
            }
            body = {
                "number": user_id,
                "text": message,
                "delay": settings.EVOLUTION_TYPING_DELAY_MS,
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=body, headers=headers)
            return True

        elif settings.PROVIDER == "zapi" and settings.ZAPI_INSTANCE_ID and settings.ZAPI_TOKEN:
            url = f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE_ID}/token/{settings.ZAPI_TOKEN}/send-text"
            headers = {"Content-Type": "application/json"}
            if settings.ZAPI_CLIENT_TOKEN:
                headers["Client-Token"] = settings.ZAPI_CLIENT_TOKEN
            body = {
                "phone": user_id,
                "message": message,
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=body, headers=headers)
            return True

    except Exception as e:
        logger.warning(f"Gateway WhatsApp outbound ignorado ou indisponível: {e}")

    return False


@router.post("/webhook", status_code=status.HTTP_200_OK)
@router.post("/webhook/whatsapp", status_code=status.HTTP_200_OK)
async def receive_webhook(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """
    Endpoint assíncrono para recebimento de mensagens e processamento NLP via Gemini.
    """
    user_id, message_text = parse_whatsapp_payload(payload)

    if not user_id or not message_text:
        # Retorna 200 para eventos não acionáveis (ex: status de entrega, mensagens próprias)
        return {"status": "ignored", "reason": "non_actionable_event"}

    logger.info(f"Nova mensagem de [{user_id}]: {message_text}")

    user_repo = UserRepository(session)
    vehicle_repo = VehicleRepository(session)
    maintenance_repo = MaintenanceRepository(session)
    reminder_repo = ReminderRepository(session)
    fine_repo = TrafficFineRepository(session)

    # 1. Garante que o usuário existe no banco
    await user_repo.get_or_create(user_id=user_id)

    # 2. Carrega veículos cadastrados na garagem e dados persistidos
    vehicles = await vehicle_repo.get_vehicles_by_user_id(user_id=user_id)
    maintenances = await maintenance_repo.get_by_user_id(user_id=user_id)
    reminders = await reminder_repo.get_by_user_id(user_id=user_id)
    fines = await fine_repo.get_by_user_id(user_id=user_id)

    # 3. Processa mensagem através da máquina de estados e Gemini com injeção completa de dados
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

    # 4. Se o usuário confirmou uma ação crítica pendente ou acionou o reset do sistema
    if action_type in ("action_confirmed", "system_reset", "action_confirmed_with_chained_reminder") and committed_data:
        try:
            now_ts = conversation_service._utcnow()
            action_state = committed_data.get("_action_state", "")

            # Caso Multi-Ação em Lote (Problema 1)
            if committed_data.get("_is_multi_action"):
                for act in committed_data.get("actions", []):
                    act_intent = act.get("intent")
                    act_ref = act.get("target_vehicle_ref")
                    target_vehicle_id = resolve_target_vehicle(vehicles, act_ref, act)
                    if not target_vehicle_id and vehicles:
                        target_vehicle_id = vehicles[0].id

                    if act_intent in ("record_maintenance", "RECORD_MAINTENANCE") and target_vehicle_id:
                        km_val = act.get("km") or act.get("odometer_km")
                        desc_val = act.get("description") or "Manutenção realizada"
                        comp_val = act.get("components") or [desc_val]
                        cost_val = act.get("total_cost", 0.0)
                        new_maint = MaintenanceRecord(
                            vehicle_id=target_vehicle_id,
                            user_id=user_id,
                            components=comp_val if isinstance(comp_val, list) else [str(comp_val)],
                            description=desc_val,
                            km=int(km_val) if km_val is not None else None,
                            total_cost=float(cost_val or 0.0),
                            workshop_name=act.get("workshop_name"),
                            created_at=now_ts,
                        )
                        saved_maint = await maintenance_repo.add_record(new_maint)
                        logger.info(f"Multi-ação: Manutenção [{saved_maint.id}] salva para veículo [{target_vehicle_id}]")
                        if km_val:
                            await vehicle_repo.update_km(target_vehicle_id, int(km_val))

                    elif act_intent in ("update_odometer", "UPDATE_ODOMETER") and target_vehicle_id:
                        km_val = act.get("odometer_km") or act.get("km")
                        if km_val:
                            await vehicle_repo.update_km(target_vehicle_id, int(km_val))
                            logger.info(f"Multi-ação: Odômetro do veículo [{target_vehicle_id}] atualizado para {km_val} km")

                    elif act_intent in ("set_reminder", "SET_REMINDER"):
                        target_veh_id = target_vehicle_id or resolve_target_vehicle(vehicles, act.get("target_vehicle_ref"), act)
                        task_desc = act.get("task_description") or act.get("description", "Lembrete automotivo")
                        due_dt = None
                        raw_dt = act.get("due_datetime") or act.get("due_date")
                        if isinstance(raw_dt, datetime):
                            due_dt = raw_dt
                        elif isinstance(raw_dt, str):
                            try:
                                due_dt = datetime.fromisoformat(raw_dt)
                            except Exception:
                                pass

                        if not due_dt:
                            target_date = (act.get("target_date_str") or "").lower()
                            time_str = act.get("explicit_time") or ""
                            hour, minute = 9, 0
                            if time_str:
                                tm_match = re.search(r"(\d{1,2}):(\d{2})|(\d{1,2})h", time_str)
                                if tm_match:
                                    if tm_match.group(1):
                                        hour = int(tm_match.group(1))
                                        minute = int(tm_match.group(2))
                                    elif tm_match.group(3):
                                        hour = int(tm_match.group(3))
                            now = now_brt_naive()  # relógio do usuário (UTC-3), não do servidor
                            base_d = now.date()
                            if "amanh" in target_date:
                                base_d = base_d + timedelta(days=1)
                            due_dt = datetime.combine(base_d, time(hour, minute))

                        new_reminder = Reminder(
                            user_id=user_id,
                            vehicle_id=target_veh_id,
                            description=task_desc,
                            due_datetime=due_dt,
                            due_km=act.get("due_km") or act.get("target_km"),
                            created_at=now_ts,
                        )
                        saved_reminder = await reminder_repo.add_reminder(new_reminder)
                        logger.info(f"Multi-ação: Lembrete [{saved_reminder.id}] cadastrado para veículo [{target_veh_id}]")

            # Caso C: Confirmação ou Comando Direto de Reset de Garagem / Sistema
            elif (
                action_type == "system_reset"
                or committed_data.get("_reset_system") is True
                or committed_data.get("_reset_garage") is True
                or action_state == "awaiting_garage_reset_confirm"
            ):
                reset_stats = await user_repo.reset_user_data(user_id=user_id)
                logger.info(f"Garagem e Sistema do usuário [{user_id}] completamente zerados: {reset_stats}")

            # Caso A: Confirmação de Cadastro de Veículo
            elif (
                action_state == "awaiting_vehicle_registration_confirm"
                or ("make" in committed_data and "model" in committed_data and "components" not in committed_data and not committed_data.get("_reset_system"))
            ):
                km_val = committed_data.get("current_km") or committed_data.get("odometer_km")
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
                saved_vehicle = await vehicle_repo.add_vehicle(new_vehicle)
                logger.info(f"Veículo [{saved_vehicle.id}] cadastrado para o condutor [{user_id}]")

            # Caso D: Confirmação de Agendamento de Lembrete
            elif (
                action_state == "awaiting_reminder_confirm"
                or "task_description" in committed_data
                or ("description" in committed_data and ("due_date" in committed_data or "due_km" in committed_data or "due_datetime" in committed_data))
            ) and not committed_data.get("_reset_system"):
                target_vehicle_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)
                task_desc = committed_data.get("task_description") or committed_data.get("description", "Lembrete automotivo")
                due_dt = None
                raw_dt = committed_data.get("due_datetime") or committed_data.get("due_date")
                if isinstance(raw_dt, datetime):
                    due_dt = raw_dt
                elif isinstance(raw_dt, str):
                    try:
                        due_dt = datetime.fromisoformat(raw_dt)
                    except Exception:
                        pass
                new_reminder = Reminder(
                    user_id=user_id,
                    vehicle_id=target_vehicle_id,
                    description=task_desc,
                    due_datetime=due_dt,
                    due_km=committed_data.get("due_km"),
                    created_at=now_ts,
                )
                saved_reminder = await reminder_repo.add_reminder(new_reminder)
                logger.info(f"Lembrete [{saved_reminder.id}] cadastrado para o condutor [{user_id}]")

            # Caso E: Confirmação de Multa / Infração de Trânsito
            elif (
                action_state == "awaiting_fine_confirm"
                or "infraction_code" in committed_data
                or "amount" in committed_data
            ) and not committed_data.get("_reset_system"):
                target_vehicle_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data) or (vehicles[0].id if vehicles else None)
                if target_vehicle_id:
                    amt = committed_data.get("amount", 0.0)
                    new_fine = TrafficFine(
                        vehicle_id=target_vehicle_id,
                        description=committed_data.get("description", "Infração de Trânsito"),
                        infraction_code=committed_data.get("infraction_code"),
                        issuing_body=committed_data.get("issuing_body"),
                        points=int(committed_data.get("points", 0) or 0),
                        amount=float(amt or 0.0),
                        discounted_amount=float(committed_data.get("discounted_amount", 0.0) or 0.0),
                        created_at=now_ts,
                    )
                    saved_fine = await fine_repo.add_fine(new_fine)
                    logger.info(f"Multa [{saved_fine.id}] cadastrada para o veículo [{target_vehicle_id}]")

            # Caso B: Confirmação de Registro de Manutenção
            elif (
                action_state == "awaiting_maintenance_confirm"
                or "components" in committed_data
                or "labor_cost" in committed_data
                or "total_cost" in committed_data
                or "service_summary" in committed_data
            ) and not committed_data.get("_reset_system"):
                target_vehicle_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)

                if target_vehicle_id:
                    km_val = committed_data.get("km") or committed_data.get("odometer_km")
                    desc_val = committed_data.get("description") or committed_data.get("service_summary") or "Manutenção realizada"
                    comp_val = committed_data.get("components") or [desc_val]
                    cost_val = committed_data.get("total_cost", 0.0)
                    new_maintenance = MaintenanceRecord(
                        vehicle_id=target_vehicle_id,
                        user_id=user_id,
                        components=comp_val if isinstance(comp_val, list) else [str(comp_val)],
                        description=desc_val,
                        km=int(km_val) if km_val is not None else None,
                        total_cost=float(cost_val or 0.0),
                        parts_cost=float(committed_data.get("parts_cost", 0.0) or 0.0),
                        labor_cost=float(committed_data.get("labor_cost", 0.0) or 0.0),
                        workshop_name=committed_data.get("workshop_name"),
                        parts_brand=committed_data.get("parts_brand"),
                        created_at=now_ts,
                    )
                    saved_maint = await maintenance_repo.add_record(new_maintenance)
                    logger.info(f"Manutenção [{saved_maint.id}] salva para o veículo [{target_vehicle_id}]")
                    if new_maintenance.km:
                        await vehicle_repo.update_km(target_vehicle_id, new_maintenance.km)

            # Caso F: Confirmação Exclusiva de Atualização de Odômetro
            elif (
                committed_data.get("_is_odometer_update")
                or ("odometer_km" in committed_data and "components" not in committed_data and "make" not in committed_data)
            ):
                target_vehicle_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)
                km_val = committed_data.get("odometer_km") or committed_data.get("km")
                if target_vehicle_id and km_val:
                    await vehicle_repo.update_km(target_vehicle_id, int(km_val))
                    logger.info(f"Odômetro do veículo [{target_vehicle_id}] atualizado para {km_val} km")

            # Caso G: Lembrete Encadeado (Chained Reminder)
            if committed_data.get("chained_reminder"):
                c_rem = committed_data["chained_reminder"]
                target_vehicle_id = resolve_target_vehicle(vehicles, committed_data.get("_target_vehicle_ref"), committed_data)

                # Resolução de data e hora agendada explícita
                target_dt = None
                exp_time_str = c_rem.get("explicit_time")
                tgt_date_str = str(c_rem.get("target_date_str", "")).lower()
                if exp_time_str:
                    try:
                        h, m = [int(p) for p in exp_time_str.split(":")]
                        day_offset = 1 if "amanh" in tgt_date_str else 0
                        target_day = now_brt_naive().date() + timedelta(days=day_offset)
                        target_dt = datetime.combine(target_day, time(h, m))
                    except Exception:
                        target_dt = None

                new_reminder = Reminder(
                    user_id=user_id,
                    vehicle_id=target_vehicle_id,
                    description=c_rem.get("task_description", "Revisão preventiva"),
                    due_datetime=target_dt,
                    data_hora_agendada=target_dt,
                    created_at=now_ts,
                )
                await reminder_repo.add_reminder(new_reminder)

                # Espelha na tabela relacional 'lembretes' se houver veículo relacional correspondente
                if target_vehicle_id:
                    try:
                        from src.repositories.lembrete_repository import LembreteRepository
                        from src.schemas.lembrete import LembreteCreate
                        lembrete_repo = LembreteRepository(session)
                        await lembrete_repo.create(
                            LembreteCreate(
                                veiculo_id=target_vehicle_id,
                                descricao=c_rem.get("task_description", "Revisão preventiva"),
                                data_hora_agendada=target_dt,
                                status="agendado" if target_dt else "pendente",
                            )
                        )
                    except Exception as e_lem:
                        logger.warning(f"Não foi possível sincronizar na tabela relacional de lembretes: {e_lem}")

                logger.info(f"Lembrete encadeado salvo para o condutor [{user_id}] agendado para: {target_dt}")

        except Exception as err:
            logger.error(f"Erro ao persistir ação confirmada no banco: {err}", exc_info=True)

    # 5. Tratamento de Atualização Direta de Odômetro (UPDATE_ODOMETER)
    elif action_type in ("update_odometer", "odometer_updated"):
        try:
            details = result.get("details") or {}
            target_vehicle_id = resolve_target_vehicle(vehicles, details.get("_target_vehicle_ref"), details)
            new_km = details.get("odometer_km") or details.get("km")
            if target_vehicle_id and new_km:
                await vehicle_repo.update_km(target_vehicle_id, int(new_km))
                logger.info(f"Odômetro do veículo [{target_vehicle_id}] atualizado para {new_km} km")
        except Exception as err:
            logger.error(f"Erro ao atualizar odômetro: {err}", exc_info=True)

    # 5.5 Escape Global: soft delete (status='cancelled') do último registro dos últimos 5 minutos
    elif action_type == "undo_last_record":
        try:
            now_ts = conversation_service._utcnow()
            cancelled = await soft_delete_last_user_record(session, user_id, now=now_ts)
            total = len(cancelled["maintenance_ids"]) + len(cancelled["reminder_ids"])
            if total:
                logger.info(f"Desfazer [{user_id}]: cancelados {cancelled}")
                result["reply"] = UNDO_SUCCESS_REPLY
            else:
                result["reply"] = UNDO_NOTHING_REPLY
        except Exception as err:
            logger.error(f"Erro ao desfazer último registro: {err}", exc_info=True)
            result["reply"] = "Não consegui apagar o último registro agora. Pode tentar de novo em instantes?"
        conversation_service.replace_last_assistant_turn(user_id, result["reply"])

    # 6. Disparo outbound transparente via WhatsApp Gateway
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
