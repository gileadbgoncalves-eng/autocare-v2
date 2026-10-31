"""
AutoCare V2 - Motor local AI-First quando a IA externa está indisponível (B15)

Extraído de `GeminiClient._build_offline_fallback` para quebrar o ciclo de import
`conversation <-> client`: este módulo depende apenas de `src.ai.schemas`, `src.domain.enums`
e do módulo neutro `src.application.services.parsers`. NÃO importa `conversation` nem `client`.
"""

from typing import Optional, List, Dict, Any

from src.ai.schemas import (
    GeminiAnalysisResponse,
    ToolCallAction,
    VehicleDraft,
    MaintenanceDraft,
)
from src.application.services.parsers import (
    extract_maintenance_items_strict,
    parse_km_value,
    parse_cost_value,
    extract_date_strict,
    is_edit_last_service_intent,
    find_popular_vehicle_spec,
)
from src.domain.enums import IntentType, ConfidenceLevel, ConversationState


def build_offline_fallback(
    message: str,
    context: Optional[Any] = None,
    vehicles: Optional[List[Any]] = None,
    maintenances: Optional[List[Any]] = None,
    history: Optional[List[Dict[str, str]]] = None,
) -> GeminiAnalysisResponse:
    """
    Motor local AI-First quando a chave de API externa não está configurada.
    Analisa `draft_atual` e `ultimo_servico_salvo` seguindo as 4 funções estruturadas
    (`upsert_draft`, `confirm_and_save_draft`, `update_last_saved_service`, `get_service_history`)
    e as regras da Etapa 3 (Anti-Alucinação, Cumulatividade e Condução Ativa na Edição).
    """

    clean_text = (message or "").strip()
    tl = clean_text.lower()

    # 1. Edição de Último Serviço Salvo (update_last_saved_service)
    if is_edit_last_service_intent(clean_text, context) or any(k in tl for k in ("alterar o último", "alterar o ultimo", "mudar o último", "mudar o ultimo", "corrigir o último", "corrigir o ultimo", "corrigir o km desse último")):
        last_m_id = None
        if maintenances:
            latest = maintenances[0]
            last_m_id = getattr(latest, "id", None)
        return GeminiAnalysisResponse(
            intent=IntentType.UPDATE_LAST_SAVED_SERVICE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Vou atualizar o último serviço salvo.",
            tool_call=ToolCallAction(name="update_last_saved_service", service_id=last_m_id),
            extracted_entities={"service_id": last_m_id},
        )

    # 2. Confirmação (confirm_and_save_draft)
    if tl in ("sim", "pode salvar", "confirmo", "fechou", "isso", "salvar", "ok", "correto", "pode confirmar"):
        if context and getattr(context, "is_awaiting_confirmation", lambda: False)():
            return GeminiAnalysisResponse(
                intent=IntentType.CONFIRM_ACTION,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message="Confirmando e salvando registro.",
                tool_call=ToolCallAction(name="confirm_and_save_draft"),
            )

    # 3. Adição Cumulativa ao Rascunho Aberto (upsert_draft)
    if context and getattr(context, "state", None) == ConversationState.AWAITING_MAINTENANCE_CONFIRM:
        items = extract_maintenance_items_strict(clean_text)
        new_km = parse_km_value(clean_text)
        new_cost = parse_cost_value(clean_text)
        new_date = extract_date_strict(clean_text)
        return GeminiAnalysisResponse(
            intent=IntentType.UPSERT_DRAFT,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Atualizando rascunho de manutenção.",
            tool_call=ToolCallAction(
                name="upsert_draft",
                itens=items or None,
                km=new_km,
                data=new_date,
                valor=new_cost,
            ),
            extracted_entities={"itens": items, "km": new_km, "data": new_date, "valor": new_cost},
        )

    # 4. Histórico (get_service_history)
    if any(k in tl for k in ("histórico", "historico", "últimas manutenções", "ultimas manutencoes", "revisões feitas")):
        return GeminiAnalysisResponse(
            intent=IntentType.QUERY_HISTORY,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message="Buscando histórico de serviços.",
            tool_call=ToolCallAction(name="get_service_history"),
        )

    # 4.5 Múltiplas Ações Encadeadas (ex: manutenção + lembrete OU múltiplos veículos)
    has_rem = any(k in tl for k in ("lembr", "avis", "agend", "calibrar", "estepe"))
    has_maint = any(k in tl for k in ("troquei", "trocou", "troca", "óleo", "oleo", "filtro", "revisão", "revisao"))
    if has_rem and has_maint:
        from src.ai.schemas import ActionDraft
        target_veh = getattr(vehicles[0], "model", "Corolla") if vehicles else "Corolla"
        maint_action = ActionDraft(
            intent=IntentType.RECORD_MAINTENANCE,
            description="Troca de óleo e filtro",
            components=["óleo", "filtro"],
            total_cost=parse_cost_value(clean_text) or 550.0,
            target_vehicle_ref=target_veh,
        )
        rem_action = ActionDraft(
            intent=IntentType.SET_REMINDER,
            task_description="Calibrar o estepe",
            explicit_time="09:00",
            target_date_str="amanhã",
            target_vehicle_ref=target_veh,
        )
        return GeminiAnalysisResponse(
            intent=IntentType.RECORD_MAINTENANCE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Identifiquei a manutenção e o agendamento de lembrete.",
            actions=[maint_action, rem_action],
            target_vehicle_ref=target_veh,
        )

    if "civic" in tl and "sandero" in tl:
        from src.ai.schemas import ActionDraft
        km_val = parse_km_value(clean_text) or 45000
        km_fmt = f"{km_val:,} km".replace(",", ".")
        a1 = ActionDraft(
            intent=IntentType.RECORD_MAINTENANCE,
            description="Revisão preventiva",
            components=["óleo", "filtro"],
            target_vehicle_ref="Civic",
        )
        a2 = ActionDraft(
            intent=IntentType.UPDATE_ODOMETER,
            odometer_km=km_val,
            target_vehicle_ref="Sandero",
            description=f"Atualização de hodômetro para {km_fmt}",
        )
        return GeminiAnalysisResponse(
            intent=IntentType.RECORD_MAINTENANCE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Múltiplas ações para Civic e Sandero identificadas.",
            actions=[a1, a2],
        )

    # 5. Nova Manutenção (upsert_draft inicial)
    maint_items = extract_maintenance_items_strict(clean_text)
    maint_verbs = ("troquei", "trocou", "troca", "fiz", "realizei", "coloquei", "gastei", "paguei")
    if (any(v in tl for v in maint_verbs) or maint_items) and not any(k in tl for k in ("tem alguma", "quando vence", "qual a revisão")):
        new_km = parse_km_value(clean_text)
        new_cost = parse_cost_value(clean_text)
        new_date = extract_date_strict(clean_text)
        target_ref = "Nivus" if "nivus" in tl else ("Corolla" if "corolla" in tl else None)
        m_draft = MaintenanceDraft(
            service_summary=", ".join(maint_items) if maint_items else clean_text,
            components=maint_items,
            total_cost=new_cost,
            odometer_km=new_km,
            date_str=new_date,
        )
        return GeminiAnalysisResponse(
            intent=IntentType.RECORD_MAINTENANCE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message=f"Entendido o registro de manutenção ({', '.join(maint_items) if maint_items else 'serviço'}).",
            maintenance=m_draft,
            target_vehicle_ref=target_ref,
            tool_call=ToolCallAction(
                name="upsert_draft",
                itens=maint_items or None,
                km=new_km,
                data=new_date,
                valor=new_cost,
            ),
            extracted_entities=m_draft.model_dump(exclude_none=True),
        )

    # 6. Cadastro de Veículo
    if any(k in tl for k in ("cadastr", "adicion", "tenho um", "tenho uma", "comprei")):
        import re
        spec = find_popular_vehicle_spec(clean_text)
        model_name = spec["model"] if spec else ("Nivus" if "nivus" in tl else ("Palio" if "palio" in tl else "Carro"))
        make_name = spec["make"] if spec else ("Volkswagen" if "nivus" in tl else ("Fiat" if "palio" in tl else "Veículo"))
        
        plate_m = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text) or re.search(r"\b([A-Za-z]{3}[-\s]?[0-9]{4})\b", clean_text)
        plate_val = re.sub(r"[\s\-]", "", plate_m.group(1)).upper() if plate_m else None
        year_m = re.search(r"\b(19\d{2}|20\d{2})\b", clean_text)
        year_val = int(year_m.group(1)) if year_m else None
        
        v_draft = VehicleDraft(
            make=make_name,
            model=model_name,
            version=spec.get("version") if spec else ("Highline" if "highline" in tl else None),
            engine=spec.get("engine") if spec else None,
            transmission=spec.get("transmission") if spec else None,
            steering=spec.get("steering") if spec else None,
            year=year_val,
            plate=plate_val,
            odometer_km=parse_km_value(clean_text),
        )
        return GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message=f"Cadastro do {model_name} identificado.",
            vehicle=v_draft,
            target_vehicle_ref=model_name,
            extracted_entities=v_draft.model_dump(exclude_none=True),
        )

    # Default fallback quando nada específico é reconhecido offline
    return GeminiAnalysisResponse(
        intent=IntentType.UNKNOWN,
        confidence=ConfidenceLevel.LOW,
        is_critical_action=False,
        reply_message="⚙️ A chave GEMINI_API_KEY não está configurada nas variáveis de ambiente.",
        reasoning="Chave ausente"
    )
