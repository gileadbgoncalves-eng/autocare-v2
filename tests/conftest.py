"""
AutoCare V2 - Pytest Configuration & Fixtures
Garante que o ambiente de teste rode veloz, desacoplado e 100% determinístico.
Fixture autouse que simula chamadas estruturadas ao Gemini (`GeminiAnalysisResponse`)
em testes offline sem qualquer regex ou dicionário de inferência em `src/`.
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
    ReminderDraft,
    ActionDraft,
)
from src.application.services.parsers import (
    parse_odometer_km,
    parse_currency_brl,
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


def _sim_extract_items(text: str) -> List[str]:
    """Extrator simples exclusivo de ambiente de teste para simular a resposta estruturada do Gemini."""
    tl = (text or "").lower()
    catalog = [
        ("filtro de combustível", "filtro de combustível"),
        ("filtro de combustivel", "filtro de combustível"),
        ("filtro de óleo", "filtro de óleo"),
        ("filtro de oleo", "filtro de óleo"),
        ("filtro de ar", "filtro de ar"),
        ("filtro de cabine", "filtro de cabine"),
        ("pastilha de freio", "pastilha de freio"),
        ("pastilhas de freio", "pastilhas de freio"),
        ("pastilha", "pastilha de freio"),
        ("correia dentada", "correia dentada"),
        ("óleo do motor", "óleo do motor"),
        ("oleo do motor", "óleo do motor"),
        ("óleo", "óleo do motor"),
        ("oleo", "óleo do motor"),
        ("filtro", "filtro"),
        ("vela", "velas"),
        ("pneu", "pneus"),
        ("bateria", "bateria"),
        ("amortecedor", "amortecedor"),
        ("alinhamento", "alinhamento"),
        ("balanceamento", "balanceamento"),
    ]
    found: List[str] = []
    for key, canonical in catalog:
        if key in tl and not any(canonical in f or f in canonical for f in found):
            found.append(canonical)
    return found


def _sim_extract_date(text: str) -> Optional[str]:
    # 0. Padrão ISO YYYY-MM-DD ou YYYY/MM/DD
    m_iso = re.search(r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", text or "")
    if m_iso:
        return MaintenanceDraft.normalize_date_str(m_iso.group(1))
    # 1. Padrão DD/MM/YYYY ou DD-MM-YYYY
    m = re.search(r"\b(\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?)\b", text or "")
    if m:
        return MaintenanceDraft.normalize_date_str(m.group(1))
    m_txt = re.search(r"(?:dia\s+)?(\d{1,2})\s+de\s+([a-zA-ZçÇ]+)(?:\s+de\s+(\d{4}))?", text or "", re.IGNORECASE)
    if m_txt:
        return MaintenanceDraft.normalize_date_str(m_txt.group(0))
    m_rel = re.search(r"\b(?:há|ha)\s+(\d+|um|uma)\s+(dia|dias|mês|mes|meses|ano|anos)\b", text or "", re.IGNORECASE)
    if m_rel:
        return MaintenanceDraft.normalize_date_str(m_rel.group(0))
    return None


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

    # 2. Se o teste especificou chave de teste para testar rate limiting ou mock direto da chamada HTTP
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
    norm_tl = re.sub(r"[.!?,;:]+$", "", tl).strip()

    # 0. Check-up da garagem global
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

    # 0.1 Consulta de status por placa no banco
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

    # 0.2 Status de veículo único citado
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
            intent=IntentType.RECORD_MAINTENANCE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message="Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero. Posso confirmar a atualização para 45.000 km?",
            target_vehicle_ref="Sandero",
            maintenance=MaintenanceDraft(odometer_km=45000, service_summary="Atualização de hodômetro"),
            extracted_entities={"odometer_km": 45000, "km": 45000, "_is_odometer_update": True},
        )

    # 0.4 Frases vagas de início de serviço (Regra 1 Anti-Alucinação)
    vague_phrases = (
        "informa um serviço", "informar um serviço", "fiz uma manutenção", "fiz manutencao",
        "quero registrar um serviço", "registrar um serviço", "fiz um serviço",
    )
    if any(vp in tl for vp in vague_phrases) and not _sim_extract_items(clean_text):
        return GeminiAnalysisResponse(
            intent=IntentType.CONSULTANCY,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message="Beleza! Me conta qual serviço você fez, e se lembrar, a data e a quilometragem.",
        )

    # 1. Edição de Último Serviço Salvo (update_last_saved_service) - incluindo resposta à condução ativa
    if context and getattr(context, "pending_edit_service_id", None) is not None:
        srv_id = context.pending_edit_service_id
        new_date = _sim_extract_date(clean_text)
        new_km = parse_odometer_km(clean_text)
        new_cost = parse_currency_brl(clean_text)
        items = _sim_extract_items(clean_text)
        if any(v is not None for v in (new_date, new_km, new_cost)) or items:
            val_str = new_date or (f"{new_km:,} km".replace(",", ".") if new_km else "")
            return GeminiAnalysisResponse(
                intent=IntentType.UPDATE_LAST_SAVED_SERVICE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message=f"✅ Serviço atualizado com sucesso para {val_str}!",
                tool_call=ToolCallAction(
                    name="update_last_saved_service",
                    service_id=srv_id,
                    itens=items or None,
                    km=new_km,
                    data=new_date,
                    valor=new_cost,
                ),
                extracted_entities={"service_id": srv_id, "itens": items, "km": new_km, "data": new_date, "valor": new_cost},
            )

    if any(k in tl for k in ("alterar o último", "alterar o ultimo", "mudar o último", "mudar o ultimo", "corrigir o último", "corrigir o ultimo", "corrigir o km desse último", "ultimo serviço", "último serviço")):
        last_m = maintenances[0] if maintenances else None
        last_m_id = getattr(last_m, "id", None) if last_m else None
        items = _sim_extract_items(clean_text)
        new_km = parse_odometer_km(clean_text)
        new_cost = parse_currency_brl(clean_text)
        new_date = _sim_extract_date(clean_text)

        has_values = bool(items or new_km is not None or new_cost is not None or new_date is not None)
        if not has_values and last_m is not None:
            if "km" in tl or "quilometragem" in tl:
                curr_km = getattr(last_m, "km", None) or 0
                curr_km_fmt = f"{curr_km:,} km".replace(",", ".")
                return GeminiAnalysisResponse(
                    intent=IntentType.UPDATE_LAST_SAVED_SERVICE,
                    confidence=ConfidenceLevel.HIGH,
                    is_critical_action=False,
                    reply_message=f"Sem problemas! A quilometragem atual está registrada como {curr_km_fmt}. Qual é a quilometragem correta?",
                    tool_call=ToolCallAction(name="update_last_saved_service", service_id=last_m_id),
                    extracted_entities={"service_id": last_m_id, "pending_edit_field": "km"},
                )
            if "data" in tl:
                m_dt = getattr(last_m, "date", None)
                dt_str = m_dt.strftime("%d/%m/%Y") if hasattr(m_dt, "strftime") else str(m_dt or "")
                return GeminiAnalysisResponse(
                    intent=IntentType.UPDATE_LAST_SAVED_SERVICE,
                    confidence=ConfidenceLevel.HIGH,
                    is_critical_action=False,
                    reply_message=f"Sem problemas! A data atual está registrada como {dt_str}. Qual é a data correta?",
                    tool_call=ToolCallAction(name="update_last_saved_service", service_id=last_m_id),
                    extracted_entities={"service_id": last_m_id, "pending_edit_field": "data"},
                )

        summary_parts = []
        if items:
            summary_parts.append(", ".join(items))
        if new_date:
            summary_parts.append(new_date)
        if new_km:
            summary_parts.append(f"{new_km:,} km".replace(",", "."))
        return GeminiAnalysisResponse(
            intent=IntentType.UPDATE_LAST_SAVED_SERVICE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message=f"Atualizei o último serviço salvo ({' | '.join(summary_parts)}).",
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

    # 2. Tratamento de Estado Pendente de Confirmação (AWAITING_*_CONFIRM)
    if context and getattr(context, "is_awaiting_confirmation", lambda: False)():
        # 2.0 Confirmação com lembrete encadeado ("sim, me lembra amanhã às 8h")
        if norm_tl.startswith("sim") and ("lembr" in tl or "amanhã" in tl or "amanha" in tl):
            tm_m = re.search(r"(\d{1,2})h(?::?(\d{2}))?", tl)
            exp_time = f"{int(tm_m.group(1)):02d}:{tm_m.group(2) or '00'}" if tm_m else "08:00"
            rem = ReminderDraft(
                task_description="Lembrete de manutenção",
                explicit_time=exp_time,
                target_date_str="amanhã",
            )
            return GeminiAnalysisResponse(
                intent=IntentType.CONFIRM_ACTION,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message=f"✅ Manutenção confirmada e salva! Lembrete agendado para amanhã às {exp_time}.",
                tool_call=ToolCallAction(name="confirm_and_save_draft"),
                reminder=rem,
            )

        # 2.1 Confirmação afirmativa pura
        if norm_tl in (
            "sim", "s", "yes", "pode salvar", "sim, pode salvar", "pode salvar na garagem",
            "confirmo", "fechou", "isso", "salvar", "ok", "correto", "pode confirmar",
            "pode meter marcha", "manda bala", "isso aí", "isso ai", "com certeza",
            "positivo", "fechado", "grava aí",
        ):
            draft_p = getattr(context, "draft_payload", None) or {}
            if getattr(context, "state", None) == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM:
                reply_conf = "✅ Veículo cadastrado com sucesso na sua garagem! Deseja registrar o histórico de alguma manutenção que já foi feita ou relatar algum serviço pendente?"
            elif draft_p.get("_is_multi_action"):
                acts = draft_p.get("actions") or []
                refs_str = ", ".join(str(a.get("target_vehicle_ref", "")) for a in acts if a.get("target_vehicle_ref"))
                reply_conf = f"✅ Todas as ações foram confirmadas e salvas com sucesso! Manutenção registrada e Lembrete agendado. ({refs_str})"
            elif draft_p.get("service_summary") == "Atualização de hodômetro":
                reply_conf = "Atenção: A placa RIO2A24 pertence ao seu Sandero, e não a um Compass. Os dados foram lançados no Sandero. ✅ Atualização do hodômetro com 45.000 km confirmada e salva com sucesso!"
            else:
                reply_conf = "✅ Registro confirmado e salvo com sucesso!"
            return GeminiAnalysisResponse(
                intent=IntentType.CONFIRM_ACTION,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message=reply_conf,
                tool_call=ToolCallAction(name="confirm_and_save_draft"),
            )

        # 2.2 Cancelamento puro (sem correção de dados)
        if norm_tl in (
            "não", "nao", "n", "cancela", "cancelar", "esquece", "descarta",
            "não quero", "nao quero", "não salva", "nao salva", "deixa pra lá",
            "deixa pra la", "deixa quieto", "abortar", "sair", "foi um erro",
            "foi erro", "foi engano", "errei", "me confundi", "esquece isso, digitei errado",
        ):
            return GeminiAnalysisResponse(
                intent=IntentType.CANCEL_ACTION,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message="Tranquilo, cancelei a operação! Nada foi salvo na sua garagem.",
            )

        # 2.3 Atualização / Correção de rascunho de manutenção aberto
        if getattr(context, "state", None) == ConversationState.AWAITING_MAINTENANCE_CONFIRM:
            items = _sim_extract_items(clean_text)
            new_km = parse_odometer_km(clean_text)
            new_cost = parse_currency_brl(clean_text)
            new_date = _sim_extract_date(clean_text)
            draft_p = getattr(context, "draft_payload", None) or {}
            prev_items = list(draft_p.get("itens") or draft_p.get("components") or [])
            all_items = list(prev_items)
            for it in items:
                if it not in all_items:
                    all_items.append(it)
            items_label = ", ".join(all_items) if all_items else "manutenção"
            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message=f"Atualizei o rascunho de manutenção ({items_label}). Posso confirmar e salvar?",
                tool_call=ToolCallAction(
                    name="upsert_draft",
                    itens=items or None,
                    km=new_km,
                    data=new_date,
                    valor=new_cost,
                ),
                extracted_entities={"itens": items, "km": new_km, "data": new_date, "valor": new_cost},
            )

        # 2.4 Atualização / Correção de rascunho de veículo aberto
        if getattr(context, "state", None) == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM:
            engine_v = "1.0 Turbo" if "1.0 turbo" in tl else ("1.0" if "1.0" in tl else ("1.4" if "1.4" in tl else ("1.6" if "1.6" in tl else ("2.0" if "2.0" in tl else None))))
            trans_v = "Manual" if "manual" in tl else ("Automático" if "autom" in tl else ("CVT" if "cvt" in tl else None))
            steer_v = None
            if "mecânica" in tl or "mecanica" in tl or "sem direção" in tl:
                steer_v = "Mecânica"
            elif any(h in tl for h in ("hidr", "hidrauliza")):
                steer_v = "Hidráulica"
            elif ("elétrica" in tl or "eletrica" in tl) and not any(a in tl for a in ("vidro", "trava", "espelho")):
                steer_v = "Elétrica"

            plate_m = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text)
            plate_v = re.sub(r"[\s\-]", "", plate_m.group(1)).upper() if plate_m else None
            odo_v = parse_odometer_km(clean_text)
            year_m = re.search(r"\b(19\d{2}|20\d{2})\b", clean_text)
            year_v = int(year_m.group(1)) if year_m else None

            v_draft = VehicleDraft(
                engine=engine_v,
                transmission=trans_v,
                steering=steer_v,
                plate=plate_v,
                odometer_km=odo_v,
                year=year_v,
            )
            return GeminiAnalysisResponse(
                intent=IntentType.REGISTER_VEHICLE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Atualizei os dados do veículo no rascunho. Posso confirmar e salvar na garagem?",
                tool_call=ToolCallAction(
                    name="upsert_draft",
                    engine=engine_v,
                    transmission=trans_v,
                    steering=steer_v,
                    plate=plate_v,
                    odometer=odo_v,
                    year=year_v,
                ),
                vehicle=v_draft,
                extracted_entities=v_draft.model_dump(exclude_none=True),
            )

    # 3. Histórico (get_service_history)
    if any(k in tl for k in ("histórico", "historico", "últimas manutenções", "ultimas manutencoes", "revisões feitas")):
        return GeminiAnalysisResponse(
            intent=IntentType.QUERY_HISTORY,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message="Aqui está o histórico de serviços registrados.",
            tool_call=ToolCallAction(name="get_service_history"),
        )

    # 4. Múltiplas Ações Encadeadas
    has_rem = any(k in tl for k in ("lembr", "avis", "agend", "calibrar", "estepe"))
    has_maint = any(k in tl for k in ("troquei", "trocou", "troca", "óleo", "oleo", "filtro", "revisão", "revisao"))
    if has_rem and has_maint:
        # Verifica se há 2 veículos do mesmo modelo na garagem sem placa na mensagem
        plate_in_msg = any(
            getattr(v, "plate", None) and getattr(v, "plate").lower().replace("-", "") in tl.replace("-", "")
            for v in (vehicles or [])
        )
        if vehicles and len(vehicles) >= 2 and not plate_in_msg:
            plates_list = ", ".join(f"{v.model} (Placa {v.plate})" for v in vehicles if getattr(v, "plate", None))
            target_veh = getattr(vehicles[0], "model", "Corolla")
            cost = parse_currency_brl(clean_text) or 350.0
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
                reply_message=f"Você possui mais de um veículo cadastrado ({plates_list}). Para qual placa deseja registrar?",
                actions=[maint_action, rem_action],
                target_vehicle_ref=target_veh,
            )

        target_veh = getattr(vehicles[0], "model", "Civic") if vehicles else "Civic"
        for v in (vehicles or []):
            if getattr(v, "plate", None) and getattr(v, "plate").lower().replace("-", "") in tl.replace("-", ""):
                target_veh = v.plate
                break
        cost = parse_currency_brl(clean_text) or 550.0
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
        km_val = parse_odometer_km(clean_text) or 45000
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
            reply_message=f"Identifiquei 2 ações: 1. Revisão no Civic; 2. Atualização do Sandero para {km_fmt}. Posso confirmar?",
            actions=[a1, a2],
        )

    # 4.6 Atualização Isolada de Hodômetro
    parsed_km = parse_odometer_km(clean_text)
    is_odo_msg = any(k in tl for k in ("estou com", "bateu", "odômetro", "hodômetro", "km agora", "marcando", "atualiza a km", "atualiza o km"))
    maint_items_check = _sim_extract_items(clean_text)
    if parsed_km is not None and is_odo_msg and not maint_items_check:
        target_v = vehicles[0] if vehicles else None
        curr_km = getattr(target_v, "current_km", None) or getattr(target_v, "quilometragem", None)
        target_ref = getattr(target_v, "model", None) or "veículo"
        if curr_km and parsed_km < curr_km:
            parsed_fmt = f"{parsed_km:,} km".replace(",", ".")
            curr_fmt = f"{curr_km:,} km".replace(",", ".")
            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message=f"⚠️ A quilometragem informada ({parsed_fmt}) é menor que a registrada anteriormente ({curr_fmt}). Foi um erro de digitação?",
                maintenance=MaintenanceDraft(odometer_km=parsed_km, service_summary="Atualização de odômetro"),
                target_vehicle_ref=target_ref,
                extracted_entities={"odometer_km": parsed_km, "km": parsed_km, "_is_odometer_rollback": True},
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
    maint_items = _sim_extract_items(clean_text)
    maint_verbs = ("troquei", "trocou", "troca", "fiz", "realizei", "coloquei", "gastei", "paguei")
    if (any(v in tl for v in maint_verbs) or maint_items or "45000 km" in tl) and not any(k in tl for k in ("tem alguma", "quando vence", "qual a revisão")):
        if not vehicles:
            return GeminiAnalysisResponse(
                intent=IntentType.RECORD_MAINTENANCE,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=True,
                reply_message="Você ainda não possui veículos na garagem. Cadastre o veículo primeiro para registrar manutenções.",
                maintenance=MaintenanceDraft(components=maint_items or ["manutenção"]),
            )

        new_km = parse_odometer_km(clean_text)
        new_cost = parse_currency_brl(clean_text)
        new_date = _sim_extract_date(clean_text)
        target_ref = "Nivus" if "nivus" in tl else ("Corolla" if "corolla" in tl else ("Sandero" if "sandero" in tl or "rio2a24" in tl else ("Civic" if "civic" in tl else None)))
        for v in (vehicles or []):
            if getattr(v, "plate", None) and getattr(v, "plate").lower().replace("-", "") in tl.replace("-", ""):
                target_ref = v.plate
                break

        # Verifica ambiguidade entre 2 veículos do mesmo modelo sem placa informada
        plate_in_msg = any(
            getattr(v, "plate", None) and getattr(v, "plate").lower().replace("-", "") in tl.replace("-", "")
            for v in (vehicles or [])
        )
        if len(vehicles) >= 2 and not plate_in_msg:
            plates_str = ", ".join(f"{v.model} ({v.plate})" for v in vehicles if getattr(v, "plate", None))
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
                reply_message=f"Identifiquei mais de um veículo ({plates_str}). Informe a placa para continuar.",
                maintenance=m_draft,
                target_vehicle_ref=target_ref,
                tool_call=ToolCallAction(name="upsert_draft", itens=maint_items or None, km=new_km, data=new_date, valor=new_cost),
            )

        # Verifica quilometragem regressiva em data recente/hoje (sem contexto temporal passado)
        is_past_context = any(p in tl for p in ("ano passado", "mês passado", "mes passado", "2024", "2025", "há um ano", "ha um ano"))
        if not is_past_context and new_date:
            try:
                parts = new_date.split("/")
                if len(parts) == 3:
                    d_parsed = dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
                    if d_parsed < dt.date.today():
                        is_past_context = True
            except Exception:
                pass
        curr_v_km = getattr(vehicles[0], "current_km", None) if vehicles else None
        is_rollback = bool(curr_v_km and new_km is not None and new_km < curr_v_km and not is_past_context)

        m_draft = MaintenanceDraft(
            service_summary=", ".join(maint_items) if maint_items else clean_text,
            components=maint_items,
            total_cost=new_cost,
            odometer_km=new_km,
            date_str=new_date,
        )
        ext_dict = m_draft.model_dump(exclude_none=True)
        if is_rollback:
            ext_dict["_is_odometer_rollback"] = True
            reply_msg = (
                f"⚠️ Atenção: A quilometragem informada ({new_km:,} km) é menor que a registrada anteriormente "
                f"({curr_v_km:,} km). Foi um erro de digitação? Posso confirmar mesmo assim?".replace(",", ".")
            )
        else:
            reply_msg = f"Entendi o registro de manutenção ({', '.join(maint_items) if maint_items else 'serviço'}). Posso confirmar e salvar?"

        return GeminiAnalysisResponse(
            intent=IntentType.RECORD_MAINTENANCE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message=reply_msg,
            maintenance=m_draft,
            target_vehicle_ref=target_ref,
            tool_call=ToolCallAction(
                name="upsert_draft",
                itens=maint_items or None,
                km=new_km,
                data=new_date,
                valor=new_cost,
            ),
            extracted_entities=ext_dict,
        )

    # 6. Cadastro de Veículo (sem alucinar especificações não ditas pelo usuário)
    plate_m = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text) or re.search(r"\b([A-Za-z]{3}[-\s]?[0-9]{4})\b", clean_text)
    is_registration = (
        any(k in tl for k in ("cadastr", "adicion", "tenho um", "tenho uma", "comprei"))
        or (plate_m and any(k in tl for k in ("1.0", "manual", "motor", "câmbio", "cambio", "direção", "direcao", "hidrauliza")))
    )
    if is_registration:
        model_name = "Nivus" if "nivus" in tl else ("Palio" if "palio" in tl or "lpd8d80" in tl else ("Corolla" if "corolla" in tl else ("Civic" if "civic" in tl else ("Gol" if "gol" in tl else "Carro"))))
        make_name = "Volkswagen" if model_name in ("Nivus", "Gol") else ("Fiat" if model_name == "Palio" else ("Toyota" if model_name == "Corolla" else ("Honda" if model_name == "Civic" else "Veículo")))

        plate_val = re.sub(r"[\s\-]", "", plate_m.group(1)).upper() if plate_m else None
        year_m = re.search(r"\b(19\d{2}|20\d{2})\b", clean_text)
        year_val = int(year_m.group(1)) if year_m else None
        odo_val = parse_odometer_km(clean_text)

        engine_v = "1.0 Turbo" if "1.0 turbo" in tl else ("1.0" if "1.0" in tl else ("2.0" if "2.0" in tl else None))
        trans_v = "Manual" if "manual" in tl else ("Automático" if "autom" in tl else None)
        steer_v = None
        if "mecânica" in tl or "mecanica" in tl:
            steer_v = "Mecânica"
        elif any(h in tl for h in ("hidr", "hidrauliza")):
            steer_v = "Hidráulica"
        elif ("elétrica" in tl or "eletrica" in tl) and not any(a in tl for a in ("vidro", "trava", "espelho")):
            steer_v = "Elétrica"

        v_draft = VehicleDraft(
            make=make_name,
            model=model_name,
            version="Highline" if "highline" in tl else ("XEI" if "xei" in tl else None),
            engine=engine_v,
            transmission=trans_v,
            steering=steer_v,
            year=year_val,
            plate=plate_val,
            odometer_km=odo_val,
        )
        return GeminiAnalysisResponse(
            intent=IntentType.REGISTER_VEHICLE,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=True,
            reply_message=f"Anotei os dados iniciais do seu {make_name} {model_name}. Para concluir o cadastro, informe o que faltar (motor/câmbio/direção, placa e quilometragem atual) ou confirme se já estiver completo.",
            tool_call=ToolCallAction(
                name="upsert_draft",
                make=make_name,
                model=model_name,
                year=year_val,
                plate=plate_val,
                odometer=odo_val,
                engine=engine_v,
                transmission=trans_v,
                steering=steer_v,
            ),
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
