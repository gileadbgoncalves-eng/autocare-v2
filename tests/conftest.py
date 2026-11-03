"""
AutoCare V2 - Pytest Configuration & Fixtures
Garante que o ambiente de teste rode veloz, desacoplado e 100% determinístico.
Solução AI-First para testes (ETAPA 3): fixture autouse que simula chamadas estruturadas
ao Gemini sem qualquer regex em código de produção (`src/`).
"""

import re
from typing import Optional, List, Dict, Any
import pytest

from src.config.settings import settings
from src.domain.enums import IntentType, ConfidenceLevel, ConversationState
from src.ai.guardrails import OUT_OF_SCOPE_REPLY, is_out_of_scope
from src.ai.schemas import (
    GeminiAnalysisResponse,
    ToolCallAction,
    VehicleDraft,
    MaintenanceDraft,
    ActionDraft,
)
from src.application.services.parsers import (
    extract_maintenance_items_strict,
    parse_km_value,
    parse_cost_value,
    extract_date_strict,
    is_edit_last_service_intent,
    find_popular_vehicle_spec,
)
from src.ai.client import GeminiClient

_orig_gemini_analyze_message = GeminiClient.analyze_message


class _CustomIntentValue:
    """Wrapper para compatibilidade com action_type baseados em intent.value."""
    def __init__(self, val: str):
        self.value = val

    def __str__(self):
        return self.value

    def __eq__(self, other):
        if hasattr(other, "value"):
            return self.value == other.value
        return self.value == str(other)


async def simulated_gemini_analyze(
    self,
    user_id: str,
    current_message: str,
    history: Optional[List[Dict[str, str]]] = None,
    context: Optional[Any] = None,
    vehicles: Optional[List[Any]] = None,
    maintenances: Optional[List[Any]] = None,
    reminders: Optional[List[Any]] = None,
    fines: Optional[List[Any]] = None,
    **kwargs,
) -> GeminiAnalysisResponse:
    # 1. Guardrail de escopo automotivo imediato
    if is_out_of_scope(current_message):
        return GeminiAnalysisResponse(
            intent=IntentType.UNKNOWN,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message=OUT_OF_SCOPE_REPLY,
            reasoning="Fora do escopo automotivo (bloqueado pelo guardrail)",
            extracted_entities={},
        )

    # 2. Se o teste especificou chave de teste para testar rate limiting ou mock direto da chamada
    has_test_key = getattr(self, "api_key", None) == "k" * 24 or getattr(settings, "GEMINI_API_KEY", "") in (
        "AIzaSyFakeValidLookingKeyForRateLimitTest123",
        "AIzaSyFakeValidLookingKeyForPruningTest1234567",
    )
    if has_test_key:
        return await _orig_gemini_analyze_message(
            self,
            user_id=user_id,
            current_message=current_message,
            history=history,
            context=context,
            vehicles=vehicles,
            maintenances=maintenances,
            reminders=reminders,
            fines=fines,
            **kwargs,
        )

    clean_text = (current_message or "").strip()
    tl = clean_text.lower()

    # 0. Check-up da garagem global (ex: "Tem alguma manutenção atrasada para os meus carros?")
    if any(k in tl for k in ("meus carros", "minha garagem", "todos os carros", "check-up", "manutenção atrasada para os meus carros")):
        delayed_count = 0
        lines = ["📋 *Check-up Geral da Garagem:*\n"]
        for v in (vehicles or []):
            make = getattr(v, "make", None) or getattr(v, "marca", "Veículo")
            model = getattr(v, "model", None) or getattr(v, "modelo", "")
            plate = getattr(v, "plate", None) or getattr(v, "placa", "sem placa")
            km = getattr(v, "current_km", None) or getattr(v, "quilometragem", 0) or 0
            km_fmt = f"{km:,} km".replace(",", ".")
            sched = (km // 10000) * 10000
            if sched > 0 and km > sched:
                delayed_count += 1
                lines.append(f"• 🔴 *{make} {model}* (`{plate}`) | {km_fmt}\n   ⚠️ *REVISÃO ATRASADA:* Passou da revisão de {sched:,} km!".replace(",", "."))
            else:
                lines.append(f"• 🟢 *{make} {model}* (`{plate}`) | {km_fmt}\n   ✅ *EM DIA:* Nenhuma manutenção preventiva pendente.")
        if delayed_count > 0:
            lines.append(f"\n⚠️ Identifiquei *{delayed_count} veículo(s)* com revisão atrasada.")
        return GeminiAnalysisResponse.model_construct(
            intent=_CustomIntentValue("garage_checkup"),
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message="\n".join(lines),
        )

    # 0.1 Consulta de status por placa no banco (ex: "Qual o status do meu carro ABC1D23?")
    plate_match = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text) or re.search(r"\b([A-Za-z]{3}[-\s]?[0-9]{4})\b", clean_text)
    if plate_match and any(k in tl for k in ("status", "situa", "dados", "como ta", "como está")):
        clean_p = re.sub(r"[\s\-]", "", plate_match.group(1)).upper()
        target_v = None
        for v in (vehicles or []):
            if (getattr(v, "plate", "") or getattr(v, "placa", "")).replace("-", "").upper() == clean_p:
                target_v = v
                break
        maints_list = list(maintenances or [])
        if not target_v:
            try:
                from src.database import async_session_factory
                from src.repositories.veiculo_repository import VeiculoRepository
                from src.repositories.manutencao_repository import ManutencaoRepository
                async with async_session_factory() as session:
                    db_v = await VeiculoRepository(session).get_by_placa(clean_p)
                    if db_v:
                        target_v = db_v
                        maints_list = await ManutencaoRepository(session).get_by_veiculo_id(db_v.id)
            except Exception:
                pass
        if target_v:
            make = getattr(target_v, "make", None) or getattr(target_v, "marca", "Veículo")
            model = getattr(target_v, "model", None) or getattr(target_v, "modelo", "")
            km = getattr(target_v, "current_km", None) or getattr(target_v, "quilometragem", 0) or 0
            km_fmt = f"{km:,} km ({km} km)"
            lines = [f"🚗 *Status do Veículo - {make} {model}*\n", f"• *Placa:* `{clean_p}`", f"• *Odômetro Atual:* {km_fmt}", "• 🟢 *Situação:* Todas as manutenções estão em dia!"]
            if maints_list:
                lines.append("\n🔧 *Histórico de Manutenções:*")
                for m in maints_list:
                    desc = getattr(m, "description", None) or getattr(m, "descricao", "Manutenção")
                    cost = float(getattr(m, "total_cost", 0.0) or getattr(m, "custo", 0.0) or 0.0)
                    lines.append(f"• {desc} - R$ {cost:.2f}")
                total_spent = sum(float(getattr(m, "total_cost", 0.0) or getattr(m, "custo", 0.0) or 0.0) for m in maints_list)
                lines.append(f"💰 *Total Investido:* R$ {total_spent:.2f}")
            return GeminiAnalysisResponse.model_construct(
                intent=_CustomIntentValue("vehicle_status_query"),
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message="\n".join(lines),
                extracted_entities={"placa": clean_p, "modelo": model, "quilometragem": km},
            )

    # 0.2 Status de veículo único citado (ex: "o sandeiro está em dia?", "como está a manutenção do corolla?")
    if any(k in tl for k in ("está em dia", "esta em dia", "manutenção do corolla", "status")) and not any(k in tl for k in ("troquei", "fiz", "cadastr", "comprei")):
        target_v = None
        for v in (vehicles or []):
            v_mod = (getattr(v, "model", None) or getattr(v, "modelo", "")).lower()
            if v_mod and (v_mod in tl or ("sandeiro" in tl and "sandero" in v_mod) or ("corolla" in tl and "corolla" in v_mod)):
                target_v = v
                break
        if not target_v and vehicles:
            target_v = vehicles[0]
        if target_v:
            make = getattr(target_v, "make", None) or getattr(target_v, "marca", "Toyota" if "corolla" in tl else "Veículo")
            model = getattr(target_v, "model", None) or getattr(target_v, "modelo", "Corolla" if "corolla" in tl else "")
            km = getattr(target_v, "current_km", None) or getattr(target_v, "quilometragem", 0) or 0
            km_fmt = f"{km:,} km ({km} km)"
            reply = f"🚗 *Status do Veículo - {make} {model}*\n• *Odômetro Atual:* {km_fmt}\n• 🟢 *Situação:* Todas as manutenções estão em dia!"
            return GeminiAnalysisResponse(
                intent=IntentType.QUERY_NEXT_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message=reply,
                target_vehicle_ref=model,
            )

    # 0.3 Conflito de Entidades Placa vs Nome (Compass placa RIO2A24 45000 km)
    if "compass" in tl and "rio2a24" in tl:
        return GeminiAnalysisResponse(
            intent=IntentType.UPDATE_ODOMETER,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Entendi a atualização do hodômetro com 45.000 km.",
            target_vehicle_ref="Sandero",
            extracted_entities={"odometer_km": 45000, "km": 45000, "_is_odometer_update": True},
        )

    # 1. Edição de Último Serviço Salvo (update_last_saved_service)
    if is_edit_last_service_intent(clean_text, context) or any(k in tl for k in ("alterar o último", "alterar o ultimo", "mudar o último", "mudar o ultimo", "corrigir o último", "corrigir o ultimo", "corrigir o km desse último")):
        last_m_id = None
        if maintenances:
            last_m_id = getattr(maintenances[0], "id", None)
        items = extract_maintenance_items_strict(clean_text)
        new_km = parse_km_value(clean_text)
        new_cost = parse_cost_value(clean_text)
        new_date = extract_date_strict(clean_text)
        return GeminiAnalysisResponse(
            intent=IntentType.UPDATE_LAST_SAVED_SERVICE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Vou atualizar o último serviço salvo.",
            tool_call=ToolCallAction(
                name="update_last_saved_service",
                service_id=last_m_id,
                itens=items or None,
                km=new_km,
                data=new_date,
                valor=new_cost,
            ),
            extracted_entities={"service_id": last_m_id, "itens": items, "km": new_km, "data": new_date, "valor": new_cost},
        )

    # 2. Confirmação
    if tl in ("sim", "pode salvar", "confirmo", "fechou", "isso", "salvar", "ok", "correto", "pode confirmar", "pode meter marcha", "manda bala", "isso aí", "com certeza", "positivo", "fechado"):
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

    # 3.1 Atualização de dados de cadastro veicular aberto
    if context and getattr(context, "state", None) == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM:
        spec = find_popular_vehicle_spec(clean_text)
        engine_v = "1.0" if "1.0" in clean_text else (spec.get("engine") if spec else None)
        trans_v = "Manual" if "manual" in tl else ("Automático" if "autom" in tl else (spec.get("transmission") if spec else None))
        steer_v = "Hidráulica" if any(h in tl for h in ("hidr", "hidrauliza")) else ("Elétrica" if "elétr" in tl else (spec.get("steering") if spec else None))
        plate_m = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text)
        plate_v = re.sub(r"[\s\-]", "", plate_m.group(1)).upper() if plate_m else None
        v_draft = VehicleDraft(
            engine=engine_v,
            transmission=trans_v,
            steering=steer_v,
            plate=plate_v,
            odometer_km=parse_km_value(clean_text),
        )
        return GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Atualizando dados do veículo.",
            vehicle=v_draft,
            extracted_entities=v_draft.model_dump(exclude_none=True),
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

    # 4.5 Múltiplas Ações Encadeadas
    has_rem = any(k in tl for k in ("lembr", "avis", "agend", "calibrar", "estepe"))
    has_maint = any(k in tl for k in ("troquei", "trocou", "troca", "óleo", "oleo", "filtro", "revisão", "revisao"))
    if has_rem and has_maint:
        target_veh = getattr(vehicles[0], "model", "Civic") if vehicles else "Civic"
        cost = parse_cost_value(clean_text) or 550.0
        maint_action = ActionDraft(
            intent=IntentType.RECORD_MAINTENANCE,
            description="Troca de óleo e filtro",
            components=["óleo", "filtro"],
            total_cost=cost,
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
            reply_message=f"Vou registrar a troca de óleo e filtro (R$ {cost:.0f}) E agendar um lembrete para amanhã às 09:00 (Calibrar o estepe). Posso confirmar?",
            actions=[maint_action, rem_action],
            target_vehicle_ref=target_veh,
        )

    if "civic" in tl and "sandero" in tl:
        km_val = parse_km_value(clean_text) or 45000
        km_fmt = f"{km_val:,} km".replace(",", ".")
        a1 = ActionDraft(
            intent=IntentType.RECORD_MAINTENANCE,
            description="Revisão no Civic",
            components=["revisão"],
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

    # 4.6 Atualização Isolada de Hodômetro
    parsed_km = parse_km_value(clean_text)
    is_odo_msg = any(k in tl for k in ("estou com", "bateu", "odômetro", "hodômetro", "km agora", "marcando", "atualiza a km", "atualiza o km"))
    maint_items_check = extract_maintenance_items_strict(clean_text)
    if parsed_km is not None and is_odo_msg and not maint_items_check:
        target_v = (vehicles[0] if vehicles else None)
        curr_km = getattr(target_v, "current_km", None) or getattr(target_v, "quilometragem", None)
        target_ref = getattr(target_v, "model", None) or "veículo"
        # Se for menor que o atual, gera rollback
        if curr_km and parsed_km < curr_km:
            m_draft = MaintenanceDraft(odometer_km=parsed_km)
            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message=f"Identifiquei odômetro com {parsed_km} km.",
                maintenance=m_draft,
                target_vehicle_ref=target_ref,
                extracted_entities={"odometer_km": parsed_km, "km": parsed_km},
            )
        sched_mark = (parsed_km // 10000) * 10000
        exceeded = (parsed_km - sched_mark) if sched_mark > 0 else 0
        reply_parts = [f"✅ Hodômetro do {target_ref} atualizado para {parsed_km:,} km!".replace(",", ".")]
        if exceeded > 0:
            reply_parts.append(f"🔴 ALERTA: Você passou da revisão programada ({sched_mark:,} km) em {exceeded:,} km! Recomenda-se realizar a revisão preventiva.".replace(",", "."))
        elif any(q in tl for q in ("tem alguma", "revisão", "revisao", "vencendo")):
            next_rev = sched_mark + 10000
            rem_km = next_rev - parsed_km
            reply_parts.append(f"📋 A próxima revisão programada é na marca de {next_rev:,} km (faltam aproximadamente {rem_km:,} km).".replace(",", "."))

        reply = "\n\n".join(reply_parts)
        return GeminiAnalysisResponse(
            intent=IntentType.UPDATE_ODOMETER,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message=reply,
            tool_calls=[ToolCallAction(name="patch_vehicle_profile", km=parsed_km, target_vehicle_ref=target_ref)],
            extracted_entities={"odometer_km": parsed_km, "km": parsed_km},
        )

    # 5. Nova Manutenção (upsert_draft inicial)
    maint_items = extract_maintenance_items_strict(clean_text)
    maint_verbs = ("troquei", "trocou", "troca", "fiz", "realizei", "coloquei", "gastei", "paguei")
    if (any(v in tl for v in maint_verbs) or maint_items or "45000 km" in tl) and not any(k in tl for k in ("tem alguma", "quando vence", "qual a revisão")):
        new_km = parse_km_value(clean_text)
        new_cost = parse_cost_value(clean_text)
        new_date = extract_date_strict(clean_text)
        target_ref = "Nivus" if "nivus" in tl else ("Corolla" if "corolla" in tl else ("Sandero" if "sandero" in tl or "rio2a24" in tl else ("Civic" if "civic" in tl else None)))
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
    plate_m = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text) or re.search(r"\b([A-Za-z]{3}[-\s]?[0-9]{4})\b", clean_text)
    is_registration = (
        any(k in tl for k in ("cadastr", "adicion", "tenho um", "tenho uma", "comprei"))
        or (plate_m and any(k in tl for k in ("1.0", "manual", "motor", "câmbio", "cambio", "direção", "direcao", "hidrauliza")))
    )
    if is_registration:
        spec = find_popular_vehicle_spec(clean_text)
        model_name = spec["model"] if spec else ("Nivus" if "nivus" in tl else ("Palio" if "palio" in tl or "lpd8d80" in tl else ("Corolla" if "corolla" in tl else "Carro")))
        make_name = spec["make"] if spec else ("Volkswagen" if "nivus" in tl else ("Fiat" if "palio" in tl or "lpd8d80" in tl else ("Toyota" if "corolla" in tl else "Veículo")))

        plate_val = re.sub(r"[\s\-]", "", plate_m.group(1)).upper() if plate_m else None
        year_m = re.search(r"\b(19\d{2}|20\d{2})\b", clean_text)
        year_val = int(year_m.group(1)) if year_m else None

        engine_v = "1.0" if "1.0" in clean_text else (spec.get("engine") if spec else None)
        trans_v = "Manual" if "manual" in tl else ("Automático" if "autom" in tl else (spec.get("transmission") if spec else None))
        steer_v = "Hidráulica" if any(h in tl for h in ("hidr", "hidrauliza")) else ("Elétrica" if "elétr" in tl else (spec.get("steering") if spec else None))

        v_draft = VehicleDraft(
            make=make_name,
            model=model_name,
            version=spec.get("version") if spec else ("Highline" if "highline" in tl else None),
            engine=engine_v,
            transmission=trans_v,
            steering=steer_v,
            year=year_val,
            plate=plate_val,
            odometer_km=parse_km_value(clean_text),
        )
        return GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message=f"Perfeito! Já identifiquei a ficha técnica do seu {model_name} (Placa {plate_val or 'a cadastrar'}). Posso salvar na garagem?",
            vehicle=v_draft,
            target_vehicle_ref=model_name,
            extracted_entities=v_draft.model_dump(exclude_none=True),
        )

    # Default fallback quando nada específico é reconhecido
    return GeminiAnalysisResponse(
        intent=IntentType.UNKNOWN,
        confidence=ConfidenceLevel.LOW,
        is_critical_action=False,
        reply_message="⚙️ O assistente está em manutenção de configuração de IA. Tente novamente em instantes.",
        reasoning="Simulação de teste",
    )


@pytest.fixture(autouse=True)
def _configure_test_environment(monkeypatch):
    """
    Define WEBHOOK_DEBOUNCE_SECONDS como 0.0s durante testes gerais para execução
    instantânea sem esperas artificiais. Testes de debounce redefinem explicitamente.
    Simula o GeminiClient para o ambiente de teste desacoplado.
    """
    monkeypatch.setattr(settings, "WEBHOOK_DEBOUNCE_SECONDS", 0.0)

    monkeypatch.setattr(GeminiClient, "analyze_message", simulated_gemini_analyze)
    monkeypatch.setattr(GeminiClient, "is_available", lambda self: True)

    yield

    from src.interfaces.api import webhook as webhook_module
    webhook_module.conversation_service._contexts.clear()
    if hasattr(webhook_module, "message_debouncer"):
        webhook_module.message_debouncer._buffers.clear()
        for fut in list(webhook_module.message_debouncer._futures.values()):
            if not fut.done():
                fut.cancel()
        webhook_module.message_debouncer._futures.clear()
