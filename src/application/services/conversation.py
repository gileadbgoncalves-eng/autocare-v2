"""
AutoCare V2 - Conversation Orchestration Service
Orquestrador central de diálogo que recebe mensagens do WhatsApp, gerencia a máquina
de estados conversacionais, invoca o Gemini e assegura o fluxo de confirmação (Human-in-the-Loop).
"""

import re
from typing import Dict, Any, Optional, List
from datetime import datetime

from src.domain.enums import ConversationState, IntentType
from src.domain.entities import ConversationContext, ConversationTurn
from src.ai.client import GeminiClient
from src.ai.schemas import GeminiAnalysisResponse


class ConversationService:
    """
    Serviço de aplicação responsável por orquestrar a interação do usuário.
    Garante que ações críticas nunca sejam persistidas sem aprovação explícita.
    """

    def __init__(self, gemini_client: Optional[GeminiClient] = None):
        self.gemini = gemini_client or GeminiClient()
        # Repositório de contexto em memória com persistência por sessão de usuário
        self._contexts: Dict[str, ConversationContext] = {}

    def get_context(self, user_id: str) -> ConversationContext:
        """Recupera ou inicializa o contexto conversacional do usuário."""
        if user_id not in self._contexts:
            self._contexts[user_id] = ConversationContext(user_id=user_id)
        return self._contexts[user_id]

    def update_context(self, context: ConversationContext):
        """Atualiza o contexto do usuário na memória de aplicação."""
        context.updated_at = datetime.utcnow()
        self._contexts[context.user_id] = context

    async def handle_incoming_message(
        self,
        user_id: str,
        message_text: str,
        vehicles: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        Ponto de entrada do processamento de mensagens recebidas via Webhook.
        """
        clean_text = (message_text or "").strip()
        context = self.get_context(user_id)

        # 0. Registro da mensagem do usuário no histórico recente
        context.recent_turns.append(ConversationTurn(role="user", content=clean_text))
        if len(context.recent_turns) > 10:
            context.recent_turns = context.recent_turns[-10:]

        text_lower = clean_text.lower()

        # 1. Comando Universal de Escape / Cancelamento
        if text_lower in ("sair", "cancelar", "abortar", "voltar", "menu", "início", "inicio"):
            if context.is_awaiting_confirmation():
                context.state = ConversationState.NORMAL
                context.draft_payload = None
                self.update_context(context)
                reply = "✅ Operação cancelada! Seus dados anteriores foram mantidos intactos. Como posso te ajudar agora?"
                self._record_assistant_turn(context, reply)
                return {"reply": reply, "action_type": "flow_escaped", "state": context.state.value}

        # 2. Máquina de Estados: Se estiver aguardando confirmação do usuário
        if context.is_awaiting_confirmation():
            result = await self._handle_pending_confirmation_state(user_id, clean_text, text_lower, context, vehicles)
            self._record_assistant_turn(context, result["reply"])
            self.update_context(context)
            return result

        # 2.5 Fast-Path de Baixa Latência: Agradecimentos e Despedidas Rápidas ("OK", "Valeu")
        clean_no_punct = re.sub(r"[^\w\s]", "", text_lower).strip()
        courtesies = {"ok", "valeu", "show", "beleza", "obrigado", "obrigada", "combinado", "perfeito", "tks", "show de bola", "valeu obrigado", "obrigado valeu"}
        if clean_no_punct in courtesies:
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

        # 3. Estado NORMAL: Envia para o Gemini analisar com contexto completo
        history_dicts = [{"role": t.role, "content": t.content} for t in context.recent_turns]
        analysis: GeminiAnalysisResponse = await self.gemini.analyze_message(
            user_id=user_id,
            current_message=clean_text,
            history=history_dicts,
            context=context,
            vehicles=vehicles,
        )

        # 4. Tratamento de Ações Críticas (Human-in-the-Loop)
        if analysis.is_critical_action:
            result = self._initiate_confirmation_flow(context, analysis)
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

        # Consolida payload do rascunho
        payload = analysis.extracted_entities or {}
        if analysis.vehicle and not payload:
            payload = analysis.vehicle.model_dump(exclude_none=True)
        elif analysis.maintenance and not payload:
            payload = analysis.maintenance.model_dump(exclude_none=True)
        elif analysis.reminder and not payload:
            payload = analysis.reminder.model_dump(exclude_none=True)
        elif analysis.fine and not payload:
            payload = analysis.fine.model_dump(exclude_none=True)

        payload["_target_vehicle_ref"] = analysis.target_vehicle_ref
        context.draft_payload = payload

        # Formata o pedido de confirmação
        confirmation_reply = (
            f"{analysis.reply_message}\n\n"
            f"📌 *Deseja confirmar e registrar essas informações?*\n"
            f"👉 Responda *'Sim'* para salvar ou *'Não'* para cancelar."
        )

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
    ) -> Dict[str, Any]:
        """
        Processa respostas quando há um rascunho pendente de confirmação.
        """
        is_affirmative = bool(re.search(r"\b(sim|confirmo|confirma|pode\s+salvar|salvar|salva|isso|correto|pode\s+registrar|com\s+certeza|positivo)\b", text_lower))
        is_negative = bool(re.search(r"\b(n[ãa]o|cancela|cancelar|n[ãa]o\s+salva|abortar|errado|esquece|parar)\b", text_lower))

        # A) Confirmação Positiva: Executa persistência definitiva
        if is_affirmative and not is_negative:
            executed_action = context.state.value
            draft_data = context.draft_payload or {}
            
            # Limpa estado pendente
            context.state = ConversationState.NORMAL
            context.draft_payload = None

            # Monta confirmação de sucesso de acordo com o tipo
            success_messages = {
                ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value: "🚗 *Veículo cadastrado com sucesso na sua garagem!*",
                ConversationState.AWAITING_MAINTENANCE_CONFIRM.value: "✅ *Manutenção salva com sucesso no histórico oficial do seu veículo!*",
                ConversationState.AWAITING_REMINDER_CONFIRM.value: "⏰ *Lembrete agendado com sucesso! Vou te alertar pontualmente no WhatsApp.*",
                ConversationState.AWAITING_FINE_CONFIRM.value: "📋 *Multa registrada no painel de controle do veículo!*",
                ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value: "🗑️ *Garagem zerada com sucesso! Todos os registros foram limpos.*",
            }
            msg = success_messages.get(executed_action, "✅ *Ação confirmada e registrada com sucesso!*")
            return {
                "reply": f"{msg}\n\nSe precisar de algo mais, é só me chamar!",
                "action_type": "action_confirmed",
                "state": context.state.value,
                "committed_data": draft_data,
            }

        # B) Recusa / Cancelamento: Descarta o rascunho
        if is_negative:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": "Tranquilo, cancelei a operação! Nada foi alterado no seu veículo.",
                "action_type": "action_cancelled",
                "state": context.state.value,
                "details": {}
            }

        # C) Se enviou correção ou nova dúvida durante a confirmação
        history_dicts = [{"role": t.role, "content": t.content} for t in context.recent_turns]
        analysis = await self.gemini.analyze_message(
            user_id=user_id,
            current_message=clean_text,
            history=history_dicts,
            context=context,
            vehicles=vehicles,
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
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": "✅ *Perfeito! Informações salvas com sucesso no seu histórico oficial!*",
                "action_type": "action_confirmed",
                "state": context.state.value,
                "committed_data": draft_data,
            }

        # D) Caso permaneça em dúvida: reapresenta a pergunta de confirmação de forma objetiva
        return {
            "reply": (
                f"Você possui um registro aguardando sua resposta:\n\n"
                f"Deseja confirmar e salvar? (Responda *'Sim'* para salvar, *'Não'* para descartar ou *'Sair'* para voltar ao menu)."
            ),
            "action_type": "awaiting_confirmation_clarification",
            "state": context.state.value,
            "draft": context.draft_payload,
        }

    def _record_assistant_turn(self, context: ConversationContext, reply_text: str):
        """Registra a resposta do assistente no histórico do contexto."""
        context.recent_turns.append(ConversationTurn(role="assistant", content=reply_text))
        if len(context.recent_turns) > 10:
            context.recent_turns = context.recent_turns[-10:]
