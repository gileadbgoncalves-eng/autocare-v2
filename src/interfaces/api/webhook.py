"""
AutoCare V2 - WhatsApp Webhook Router
Ponto de entrada assíncrono para eventos e mensagens de provedores de WhatsApp
(Evolution API, Z-API ou payloads diretos de teste).
"""

import logging
from typing import Dict, Any, Optional, Tuple
from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.session import get_async_session
from src.infrastructure.database.repositories import (
    UserRepository,
    VehicleRepository,
    MaintenanceRepository,
)
from src.application.services.conversation import ConversationService
from src.domain.entities import Vehicle, MaintenanceRecord
from src.domain.enums import VehicleType, FuelType, UsageProfile

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

    # 1. Payload de teste direto
    if "user_id" in payload and "message" in payload:
        user_id = "".join(filter(str.isdigit, str(payload["user_id"])))
        return user_id, str(payload["message"]).strip()

    # 2. Formato Evolution API
    data = payload.get("data", {})
    key = data.get("key", {})
    # Ignora mensagens enviadas pelo próprio bot
    if key.get("fromMe", False):
        return None, None

    remote_jid = key.get("remoteJid", "")
    # Ignora mensagens de grupos (@g.us)
    if "@g.us" in remote_jid:
        return None, None

    if "@s.whatsapp.net" in remote_jid:
        user_id = "".join(filter(str.isdigit, remote_jid.split("@")[0]))
        msg_obj = data.get("message", {})
        text = (
            msg_obj.get("conversation")
            or msg_obj.get("extendedTextMessage", {}).get("text")
            or msg_obj.get("buttonsResponseMessage", {}).get("selectedButtonId")
            or msg_obj.get("listResponseMessage", {}).get("singleSelectReply", {}).get("selectedRowId")
        )
        if text:
            return user_id, text.strip()

    # 3. Formato Z-API
    phone = payload.get("phone") or payload.get("senderPhone")
    if phone:
        if payload.get("isGroup", False):
            return None, None
        user_id = "".join(filter(str.isdigit, str(phone)))
        text_obj = payload.get("text", {})
        text = text_obj.get("message") if isinstance(text_obj, dict) else payload.get("message")
        if text:
            return user_id, str(text).strip()

    return None, None


@router.post("/webhook", status_code=status.HTTP_200_OK)
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

    # 1. Garante que o usuário existe no banco
    await user_repo.get_or_create(user_id=user_id)

    # 2. Carrega veículos cadastrados na garagem
    vehicles = await vehicle_repo.get_vehicles_by_user_id(user_id=user_id)

    # 3. Processa mensagem através da máquina de estados e Gemini
    result = await conversation_service.handle_incoming_message(
        user_id=user_id,
        message_text=message_text,
        vehicles=vehicles,
    )

    action_type = result.get("action_type")
    committed_data = result.get("committed_data")

    # 4. Se o usuário confirmou uma ação crítica pendente ou acionou o reset do sistema
    if action_type in ("action_confirmed", "system_reset") and committed_data:
        try:
            # Caso A: Confirmação de Cadastro de Veículo
            if "make" in committed_data and "model" in committed_data and "components" not in committed_data and not committed_data.get("_reset_system"):
                new_vehicle = Vehicle(
                    user_id=user_id,
                    make=committed_data.get("make", "Desconhecido"),
                    model=committed_data.get("model", "Veículo"),
                    year=committed_data.get("year"),
                    plate=committed_data.get("plate"),
                    engine=committed_data.get("engine"),
                    current_km=committed_data.get("current_km"),
                    vehicle_type=VehicleType(committed_data.get("vehicle_type", "carro")) if committed_data.get("vehicle_type") in [e.value for e in VehicleType] else VehicleType.CARRO,
                    fuel_type=FuelType(committed_data.get("fuel_type", "flex")) if committed_data.get("fuel_type") in [e.value for e in FuelType] else FuelType.FLEX,
                    usage_profile=UsageProfile(committed_data.get("usage_profile", "convencional")) if committed_data.get("usage_profile") in [e.value for e in UsageProfile] else UsageProfile.CONVENCIONAL,
                )
                saved_vehicle = await vehicle_repo.add_vehicle(new_vehicle)
                logger.info(f"Veículo [{saved_vehicle.id}] cadastrado para o condutor [{user_id}]")

            # Caso B: Confirmação de Registro de Manutenção
            elif ("components" in committed_data or "labor_cost" in committed_data or "total_cost" in committed_data) and not committed_data.get("_reset_system"):
                # Seleciona veículo ativo ou o único veículo da garagem
                target_vehicle_id = None
                if vehicles:
                    target_vehicle_id = vehicles[0].id

                if target_vehicle_id:
                    new_maintenance = MaintenanceRecord(
                        vehicle_id=target_vehicle_id,
                        user_id=user_id,
                        components=committed_data.get("components") or [committed_data.get("description", "Manutenção Geral")],
                        description=committed_data.get("description", "Manutenção realizada"),
                        km=committed_data.get("km"),
                        total_cost=float(committed_data.get("total_cost", 0.0) or 0.0),
                        parts_cost=float(committed_data.get("parts_cost", 0.0) or 0.0),
                        labor_cost=float(committed_data.get("labor_cost", 0.0) or 0.0),
                        workshop_name=committed_data.get("workshop_name"),
                        parts_brand=committed_data.get("parts_brand"),
                    )
                    saved_maint = await maintenance_repo.add_record(new_maintenance)
                    logger.info(f"Manutenção [{saved_maint.id}] salva para o veículo [{target_vehicle_id}]")

            # Caso C: Confirmação ou Comando Direto de Reset de Garagem / Sistema
            elif (
                action_type == "system_reset"
                or committed_data.get("_reset_system")
                or committed_data.get("_reset_garage")
                or "reset" in str(committed_data).lower()
            ):
                reset_stats = await user_repo.reset_user_data(user_id=user_id)
                logger.info(f"Garagem e Sistema do usuário [{user_id}] completamente zerados: {reset_stats}")

        except Exception as err:
            logger.error(f"Erro ao persistir ação confirmada no banco: {err}", exc_info=True)

    return {
        "status": "success",
        "user_id": user_id,
        "reply": result.get("reply"),
        "action_type": action_type,
        "state": result.get("state"),
    }
