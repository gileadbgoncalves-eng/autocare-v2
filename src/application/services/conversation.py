"""
AutoCare V2 - Conversation Orchestration Service
Orquestrador central de diálogo que recebe mensagens do WhatsApp, gerencia a máquina
de estados conversacionais, invoca o Gemini e assegura o fluxo de confirmação (Human-in-the-Loop).
"""

import re
import unicodedata
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

    def clear_context(self, user_id: str):
        """Limpa o contexto em memória do usuário."""
        if user_id in self._contexts:
            del self._contexts[user_id]

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

        # 1.5 Comando Direto de Zerar/Resetar Sistema (Zero Latência para Testes)
        norm_text = "".join(c for c in unicodedata.normalize("NFKD", text_lower) if not unicodedata.combining(c)).strip()
        reset_triggers = {
            "zerar", "reset", "resetar", "/reset", "/zerar", "/limpar", "/resetar", "/reset_all",
            "zerar sistema", "zerar o sistema", "zerar todo o sistema", "zerar todo sistema",
            "resetar sistema", "resetar o sistema", "resetar todo o sistema",
            "limpar sistema", "limpar o sistema", "limpar tudo", "zerar tudo", "resetar tudo",
            "apagar tudo", "excluir tudo", "zerar garagem", "limpar garagem", "resetar garagem",
            "apagar garagem", "reiniciar sistema", "reiniciar cadastro", "comecar do zero", "iniciar do zero"
        }
        if norm_text in reset_triggers:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            context.recent_turns.clear()
            self.update_context(context)
            reply = (
                "🧹 *Garagem e Sistema zerados com sucesso!*\n\n"
                "Todos os seus veículos, histórico de manutenção, gastos e lembretes foram removidos.\n"
                "O seu perfil voltou ao ponto zero, pronto para novos testes!\n\n"
                "🚀 *O que você gostaria de fazer agora?*\n"
                "• Cadastrar um novo veículo (ex: *'Cadastrar meu Honda Civic'* ou *'Minha moto Fazer 250'*)\n"
                "• Mandar uma dúvida técnica ou consulta mecânica"
            )
            self._record_assistant_turn(context, reply)
            return {
                "reply": reply,
                "action_type": "system_reset",
                "state": context.state.value,
                "committed_data": {"_reset_system": True, "_reset_garage": True, "user_id": user_id},
                "details": {"reset_all": True}
            }

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
        Garante que apenas respostas curtas diretas de afirmação/negação sejam tratadas
        via Fast-Path, enviando comandos complexos, correções e novas ações para o Gemini.
        """
        words = clean_text.strip().split()
        is_affirmative = bool(re.search(r"\b(sim|confirmo|confirma|pode\s+salvar|salvar|salva|isso|correto|pode\s+registrar|com\s+certeza|positivo|fecho|fechado)\b", text_lower))
        is_negative = bool(re.search(r"\b(n[ãa]o|cancela|cancelar|n[ãa]o\s+salva|abortar|errado|errei|esquece|parar)\b", text_lower))

        # Detecta se o usuário está corrigindo dados do rascunho (ex: "Não, a placa é...", "Não, o ano é 2023")
        is_correction = bool(re.search(
            r"(\b(placa|ano|km|quilometragem|valor|custo|pre[çc]o|reais|oficina|pe[çc]a|marca|modelo|troc|troquei|troca|era|foi|seria|[ée])\b|"
            r"\b[a-zA-Z]{3}\d[a-zA-Z0-9]\d{2}\b|\b(19\d\d|20\d\d)\b|\b\d+\s*(km|reais|r\$))",
            text_lower
        ))

        # A) Confirmação Positiva Direta: Apenas mensagens curtas (<= 4 palavras) e sem entidades complexas novas
        is_short_affirmative = is_affirmative and not is_negative and not is_correction and len(words) <= 4

        if is_short_affirmative:
            executed_action = context.state.value
            draft_data = context.draft_payload or {}
            if executed_action == ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value:
                draft_data["_reset_system"] = True
                draft_data["_reset_garage"] = True
            
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

        # B) Recusa / Cancelamento Puro Curto
        if is_negative and not is_correction and len(words) <= 4:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": "Tranquilo, cancelei a operação! Nada foi alterado no seu veículo.",
                "action_type": "action_cancelled",
                "state": context.state.value,
                "details": {}
            }

        # C) Mensagens complexas, correções de dados, novas instruções ou dúvidas: consulta o Gemini
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
            executed_action = context.state.value
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            success_messages = {
                ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM.value: "🚗 *Veículo cadastrado com sucesso na sua garagem!*",
                ConversationState.AWAITING_MAINTENANCE_CONFIRM.value: "✅ *Manutenção salva com sucesso no histórico oficial do seu veículo!*",
                ConversationState.AWAITING_REMINDER_CONFIRM.value: "⏰ *Lembrete agendado com sucesso! Vou te alertar pontualmente no WhatsApp.*",
                ConversationState.AWAITING_FINE_CONFIRM.value: "📋 *Multa registrada no painel de controle do veículo!*",
                ConversationState.AWAITING_GARAGE_RESET_CONFIRM.value: "🗑️ *Garagem zerada com sucesso! Todos os registros foram limpos.*",
            }
            msg = success_messages.get(executed_action, "✅ *Perfeito! Informações salvas com sucesso no seu histórico oficial!*")
            return {
                "reply": f"{msg}\n\nSe precisar de algo mais, é só me chamar!",
                "action_type": "action_confirmed",
                "state": context.state.value,
                "committed_data": draft_data,
            }

        # Se for nova ação crítica OU correção de dados do rascunho (is_critical_action = True):
        # Substitui/atualiza o rascunho e solicita confirmação para o novo dado
        if analysis.is_critical_action:
            return self._initiate_confirmation_flow(context, analysis)

        # Se for UNKNOWN (ex: incompatibilidade mecânica, serviço recusado, prompt injection ou fora de escopo):
        # Retorna a resposta explicativa do Gemini e limpa o rascunho pendente incompatível
        if analysis.intent == IntentType.UNKNOWN:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": analysis.reply_message,
                "action_type": "unknown",
                "state": context.state.value,
                "details": analysis.extracted_entities
            }

        # Se for POLITE_CLOSING ou GREETING:
        if analysis.intent in (IntentType.POLITE_CLOSING, IntentType.GREETING_MENU):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {
                "reply": analysis.reply_message,
                "action_type": analysis.intent.value,
                "state": context.state.value,
                "details": {}
            }

        # D) Caso permaneça em dúvida genuína sobre o rascunho: reapresenta a pergunta de confirmação
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
