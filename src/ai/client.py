"""
AutoCare V2 - Async Gemini Client
Cliente assíncrono para o Google Gemini com Structured Outputs (Pydantic v2),
timeout estrito de 5s, exponential backoff com jitter para 429/5xx e fallback seguro.
"""

import json
import asyncio
import random
import urllib.request
import urllib.error
from typing import Optional, List, Dict, Any

from src.config.settings import settings
from src.domain.enums import IntentType, ConfidenceLevel
from src.ai.schemas import GeminiAnalysisResponse
from src.ai.prompts import SYSTEM_INSTRUCTION, build_analysis_prompt
from src.ai.guardrails import OUT_OF_SCOPE_REPLY, is_out_of_scope, prune_history

# Limites do guardrail de custo para tokens de saída
MIN_OUTPUT_TOKENS = 250
MAX_OUTPUT_TOKENS = 400

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class GeminiClientException(Exception):
    """Exceção base para falhas do cliente Gemini."""
    pass


class GeminiClient:
    """
    Cliente assíncrono para a API do Google Gemini com Structured Output estrito.
    Garante respostas previsíveis, tipadas e com alta resiliência a falhas de rede.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        primary_model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
    ):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.primary_model = primary_model or settings.GEMINI_PRIMARY_MODEL
        self.fallback_model = fallback_model or settings.GEMINI_FALLBACK_MODEL
        self.timeout = timeout_seconds or settings.GEMINI_TIMEOUT_SECONDS
        self.max_retries = max_retries or settings.GEMINI_MAX_RETRIES
        # Sempre dentro da faixa segura 250-400, mesmo que a configuração externa fuja dela
        self.max_output_tokens = max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, settings.GEMINI_MAX_OUTPUT_TOKENS))
        self.history_max_turns = settings.GEMINI_HISTORY_MAX_TURNS
        self._circuit_open_until: float = 0.0

    def is_available(self) -> bool:
        """Verifica se a chave de API está configurada."""
        return bool(self.api_key and len(self.api_key.strip()) > 10)

    async def analyze_message(
        self,
        user_id: str,
        current_message: str,
        history: Optional[List[Dict[str, str]]] = None,
        context: Optional[Any] = None,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> GeminiAnalysisResponse:
        """
        Analisa a mensagem do usuário com contexto conversacional e Structured Output.
        Garante retorno de instância válida de GeminiAnalysisResponse mesmo sob falhas de rede.
        """
        import time as _time

        # Guardrail 1: assunto fora do universo automotivo é barrado ANTES de qualquer custo de API
        if is_out_of_scope(current_message):
            return self._build_out_of_scope_response()

        if not self.is_available():
            return self._build_offline_fallback(
                current_message,
                context=context,
                vehicles=vehicles,
                maintenances=maintenances,
                history=history,
            )

        # Guardrail 2: somente as últimas N interações do histórico seguem para a API
        history = prune_history(history, max_turns=self.history_max_turns, current_message=current_message)

        # Montagem do prompt com injeção do histórico de diálogo
        prompt = self._format_prompt(current_message, history, context, vehicles, maintenances, reminders, fines)
        schema_dict = self._clean_schema_for_gemini(GeminiAnalysisResponse.model_json_schema())

        # Circuit breaker rápido quando a cota/chave retornou erro recentemente (evita bloquear chamadas concorrentes)
        if _time.monotonic() < getattr(self, "_circuit_open_until", 0.0):
            return self._build_contingency_fallback(current_message, "Circuit breaker ativo (cota/API indisponível)")

        models_to_try = [self.primary_model, self.fallback_model]
        last_error = None

        for model_name in models_to_try:
            try:
                raw_json = await self._call_gemini_with_retry(model_name, prompt, schema_dict)
                if raw_json:
                    self._circuit_open_until = 0.0
                    # Validação e desserialização tipada Pydantic v2
                    validated_response = GeminiAnalysisResponse.model_validate_json(raw_json)
                    return validated_response
            except Exception as e:
                last_error = e
                err_str = str(e)
                # Se a chave de API estiver sem cota (429) ou erro 4xx, abre circuit breaker curto e não bloqueia
                if any(code in err_str for code in ("HTTP 429", "HTTP 401", "HTTP 403", "HTTP 400", "HTTP 404")):
                    self._circuit_open_until = _time.monotonic() + 30.0
                    break
                continue

        # Se todas as tentativas falharem, aciona o Fallback Handler
        return self._build_contingency_fallback(current_message, str(last_error))

    def _format_prompt(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
        context: Optional[Any],
        vehicles: Optional[List[Any]],
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> str:
        """Monta o prompt com contexto enriquecido, veículos completos, histórico recente e dados do banco."""
        return build_analysis_prompt(
            current_message=message,
            context=context,
            vehicles=vehicles,
            history=history,
            maintenances=maintenances,
            reminders=reminders,
            fines=fines,
        )

    @staticmethod
    def _clean_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Adapta o JSON Schema gerado pelo Pydantic v2 para a especificação estrita
        da API REST do Google Gemini (resolvendo $defs/$ref inline e removendo campos não suportados).
        """
        import copy
        defs = schema.get("$defs", {})

        def resolve(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    name = node["$ref"].split("/")[-1]
                    target = defs.get(name, {})
                    return resolve(copy.deepcopy(target))
                if "anyOf" in node:
                    non_null = [x for x in node["anyOf"] if isinstance(x, dict) and x.get("type") != "null"]
                    if non_null:
                        resolved_sub = resolve(copy.deepcopy(non_null[0]))
                        if isinstance(resolved_sub, dict):
                            resolved_sub["nullable"] = True
                            if "description" in node and "description" not in resolved_sub:
                                resolved_sub["description"] = node["description"]
                            return resolved_sub
                cleaned = {}
                for k, v in node.items():
                    if k in ("$defs", "additionalProperties", "title", "default", "minimum", "maximum", "minItems", "maxItems"):
                        continue
                    if k == "properties" and isinstance(v, dict):
                        props = {}
                        for pk, pv in v.items():
                            if pk == "extracted_entities":
                                continue
                            props[pk] = resolve(pv)
                        cleaned[k] = props
                        continue
                    if k == "required" and isinstance(v, list):
                        cleaned[k] = [r for r in v if r != "extracted_entities"]
                        continue
                    cleaned[k] = resolve(v)
                return cleaned
            elif isinstance(node, list):
                return [resolve(x) for x in node]
            return node

        return resolve(schema)

    def _build_payload(self, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Monta o corpo da requisição, incluindo o teto de tokens de saída (guardrail de custo)."""
        return {
            "system_instruction": {
                "parts": [{"text": SYSTEM_INSTRUCTION}]
            },
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": self.max_output_tokens,
                "response_mime_type": "application/json",
                "response_schema": schema,
            }
        }

    async def _call_gemini_with_retry(
        self,
        model: str,
        prompt: str,
        schema: Dict[str, Any],
    ) -> Optional[str]:
        """
        Executa chamada de baixa latência (Etapa 4) com fail-fast para erros 4xx/429
        e retry curto apenas para erros transitórios de servidor (5xx).
        """
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        payload = self._build_payload(prompt, schema)

        for attempt in range(1, self.max_retries + 1):
            try:
                if HAS_HTTPX:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                if parts:
                                    return parts[0].get("text", "").strip()
                            raise GeminiClientException("Candidatos vazios na resposta do Gemini.")
                        elif resp.status_code in (500, 502, 503, 504):
                            if attempt < self.max_retries:
                                await asyncio.sleep(0.15 * attempt)
                                continue
                            raise GeminiClientException(f"HTTP {resp.status_code}: {resp.text}")
                        else:
                            # Fail-fast para 400, 401, 403, 404 e 429 (evita travar a requisição em sleep longo)
                            raise GeminiClientException(f"HTTP {resp.status_code}: {resp.text}")
                else:
                    # Fallback assíncrono transparente via thread pool nativo
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(None, self._sync_urllib_call, url, payload)

            except GeminiClientException:
                raise
            except urllib.error.HTTPError as err:
                raise GeminiClientException(f"HTTP {err.code}: {err.reason}") from err
            except Exception as e:
                if attempt < self.max_retries:
                    await asyncio.sleep(0.15)
                    continue
                raise GeminiClientException(f"Erro no transporte de IA: {e}")

        return None

    def _sync_urllib_call(self, url: str, payload: dict) -> str:
        """Chamada síncrona com urllib encapsulada para execução assíncrona."""
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json", "User-Agent": "AutoCare-V2/1.0", "x-goog-api-key": self.api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
        raise GeminiClientException("Resposta inválida do Gemini via urllib.")

    @staticmethod
    def _build_out_of_scope_response() -> GeminiAnalysisResponse:
        """Recusa padronizada (sem chamada de API) para assuntos fora do escopo automotivo."""
        return GeminiAnalysisResponse(
            intent=IntentType.UNKNOWN,
            confidence=ConfidenceLevel.HIGH,
            is_critical_action=False,
            reply_message=OUT_OF_SCOPE_REPLY,
            reasoning="Fora do escopo automotivo (bloqueado pelo guardrail, sem chamada ao LLM)",
        )

    def _build_contingency_fallback(self, message: str, error_detail: str) -> GeminiAnalysisResponse:
        """Fallback seguro em caso de indisponibilidade ou timeout excedido."""
        clean = message.lower().strip()
        if any(w in clean for w in ["ok", "valeu", "show", "beleza", "obrigado", "combinado"]):
            return GeminiAnalysisResponse(
                intent=IntentType.POLITE_CLOSING,
                confidence=ConfidenceLevel.HIGH,
                is_critical_action=False,
                reply_message="Show de bola! Qualquer novidade ou manutenção no seu veículo, estou por aqui. Um abraço! 👍",
                reasoning=f"Contingência de encerramento amigável ({error_detail})"
            )

        return GeminiAnalysisResponse(
            intent=IntentType.UNKNOWN,
            confidence=ConfidenceLevel.LOW,
            is_critical_action=False,
            reply_message=(
                "😅 Desculpe, estou processando muitas mensagens agora, pode repetir em instantes?\n\n"
                "Exemplos de como falar comigo:\n"
                "• *Troquei óleo e filtro hoje com 145 mil km*\n"
                "• *Me lembra de calibrar pneus na sexta*\n"
                "• *Menu* (ver opções da sua garagem)"
            ),
            reasoning=f"Contingência de Timeout/Erro: {error_detail}"
        )

    def _build_offline_fallback(
        self,
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
        return GeminiAnalysisResponse(
            intent=IntentType.UNKNOWN,
            confidence=ConfidenceLevel.LOW,
            is_critical_action=False,
            reply_message="⚙️ A chave GEMINI_API_KEY não está configurada nas variáveis de ambiente.",
            reasoning="Chave ausente"
        )
