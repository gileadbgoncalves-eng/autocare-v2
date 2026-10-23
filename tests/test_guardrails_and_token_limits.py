"""
Autoquer - Testes de Guardrails de Escopo e Limites de Tokens

Comprovam a blindagem de custo da assinatura de R$ 9,90/mês:
1. Perguntas fora do universo automotivo (ex: receita de bolo) recebem a recusa padrão
   e NÃO geram nenhuma chamada paga ao Gemini.
2. O System Instruction carrega a Regra de Bloqueio Absoluto.
3. O modelo é da família lite/flash e `maxOutputTokens` fica sempre entre 250 e 400.
4. Somente as últimas 5 interações do histórico são enviadas à API.
"""

import sys

import pytest

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.ai import client as client_module
from src.ai.client import GeminiClient
from src.ai.guardrails import (
    MAX_HISTORY_TURNS,
    OUT_OF_SCOPE_REPLY,
    is_out_of_scope,
    prune_history,
)
from src.ai.prompts import SYSTEM_INSTRUCTION, build_analysis_prompt
from src.ai.schemas import GeminiAnalysisResponse
from src.application.services.conversation import ConversationService
from src.domain.entities import ConversationTurn
from src.domain.enums import ConfidenceLevel, IntentType

EXPECTED_REFUSAL = (
    "Desculpe, sou o assistente exclusivo do Autoquer e só consigo ajudar com dúvidas e registros "
    "sobre o seu veículo. Como posso ajudar com seu carro hoje?"
)


def _client_with_key() -> GeminiClient:
    return GeminiClient(api_key="k" * 24)


def _forbid_api_calls(monkeypatch) -> list:
    """Faz qualquer tentativa de chamada à API falhar o teste (comprova custo zero)."""
    calls = []

    async def _boom(self, model, prompt, schema):
        calls.append(prompt)
        raise AssertionError("A API do Gemini NÃO deveria ter sido chamada para conteúdo fora do escopo")

    monkeypatch.setattr(GeminiClient, "_call_gemini_with_retry", _boom)
    return calls


# ---------------------------------------------------------------------------
# 1. Blindagem de escopo
# ---------------------------------------------------------------------------
def test_refusal_constant_is_the_exact_mandated_sentence():
    assert OUT_OF_SCOPE_REPLY == EXPECTED_REFUSAL


def test_system_instruction_contains_absolute_block_rule():
    assert "Você é o Autoquer, um assistente virtual focado EXCLUSIVAMENTE na gestão e manutenção" in SYSTEM_INSTRUCTION
    assert "REGRA DE BLOQUEIO ABSOLUTO" in SYSTEM_INSTRUCTION
    assert "NUNCA responda a perguntas fora do universo automotivo" in SYSTEM_INSTRUCTION
    assert EXPECTED_REFUSAL in SYSTEM_INSTRUCTION
    assert "direto, conciso e evite respostas prolixas" in SYSTEM_INSTRUCTION


async def test_cake_recipe_is_blocked_with_zero_api_calls(monkeypatch):
    """Cenário do requisito: usuário pergunta uma receita de bolo ao bot."""
    calls = _forbid_api_calls(monkeypatch)
    service = ConversationService(gemini_client=_client_with_key())

    result = await service.handle_incoming_message(
        user_id="5521900000001",
        message_text="Me passa uma receita de bolo de cenoura com cobertura de chocolate?",
    )

    assert result["reply"] == EXPECTED_REFUSAL
    assert calls == []  # nenhum token gasto


@pytest.mark.parametrize(
    "message",
    [
        "Conta uma piada pra mim",
        "me ajuda a programar um script em python",
        "Explica a fórmula da física quântica",
        "Faz meu dever de casa de matemática",
        "qual a previsão do tempo amanhã?",
        "escreva um poema sobre o mar",
    ],
)
async def test_off_topic_messages_are_refused_without_api(monkeypatch, message):
    calls = _forbid_api_calls(monkeypatch)
    analysis = await _client_with_key().analyze_message(user_id="5521900000002", current_message=message)

    assert analysis.reply_message == EXPECTED_REFUSAL
    assert analysis.intent == IntentType.UNKNOWN
    assert analysis.is_critical_action is False
    assert calls == []


def test_automotive_messages_are_never_blocked():
    for message in (
        "Troquei o óleo do Civic por 300 reais",
        "Quanto custa uma revisão completa do carro?",
        "me lembra de calibrar o pneu amanhã às 8h",
        "qual a receita pra trocar o óleo do motor?",  # termo automotivo presente: o LLM decide
        "meu IPVA vence quando?",
        "",
    ):
        assert not is_out_of_scope(message), message


# ---------------------------------------------------------------------------
# 2. Configuração do LLM: modelo e max_output_tokens
# ---------------------------------------------------------------------------
def test_model_is_cost_efficient_tier():
    client = _client_with_key()
    for model in (client.primary_model, client.fallback_model):
        assert "pro" not in model
        assert "flash" in model


def test_payload_enforces_safe_max_output_tokens():
    client = _client_with_key()
    payload = client._build_payload("prompt", {"type": "object"})
    limit = payload["generationConfig"]["maxOutputTokens"]
    assert 250 <= limit <= 400


@pytest.mark.parametrize("configured, expected", [(10, 250), (300, 300), (4000, 400)])
def test_max_output_tokens_is_clamped_to_safe_range(monkeypatch, configured, expected):
    monkeypatch.setattr(client_module.settings, "GEMINI_MAX_OUTPUT_TOKENS", configured)
    assert _client_with_key().max_output_tokens == expected


# ---------------------------------------------------------------------------
# 3. Pruning do histórico (apenas as últimas 5 interações)
# ---------------------------------------------------------------------------
def _turns(n: int):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"mensagem-{i:02d}"}
        for i in range(n)
    ]


def test_prune_history_keeps_only_last_five_turns():
    pruned = prune_history(_turns(12))
    assert MAX_HISTORY_TURNS == 5
    assert [t["content"] for t in pruned] == [f"mensagem-{i:02d}" for i in range(7, 12)]


def test_prune_history_accepts_conversation_turn_objects_and_drops_duplicate_current_message():
    turns = [ConversationTurn(role="user" if i % 2 == 0 else "assistant", content=f"t{i}") for i in range(8)]
    turns.append(ConversationTurn(role="user", content="Troquei o óleo"))
    pruned = prune_history(turns, current_message="Troquei o óleo")
    assert [t["content"] for t in pruned] == ["t3", "t4", "t5", "t6", "t7"]


def test_prune_history_truncates_long_turns_and_handles_empty():
    pruned = prune_history([{"role": "user", "content": "x" * 5000}])
    assert len(pruned[0]["content"]) <= 401
    assert prune_history(None) == []
    assert prune_history([]) == []


def test_analysis_prompt_contains_only_last_five_history_turns():
    prompt = build_analysis_prompt(current_message="Quanto gastei no Civic?", history=_turns(12))
    for i in range(7, 12):
        assert f"mensagem-{i:02d}" in prompt
    for i in range(0, 7):
        assert f"mensagem-{i:02d}" not in prompt


async def test_api_receives_pruned_history_end_to_end(monkeypatch):
    captured = {}

    async def _fake_call(self, model, prompt, schema):
        captured["prompt"] = prompt
        return GeminiAnalysisResponse(
            intent=IntentType.UNKNOWN,
            confidence=ConfidenceLevel.LOW,
            is_critical_action=False,
            reply_message="ok",
            reasoning="teste",
        ).model_dump_json()

    monkeypatch.setattr(GeminiClient, "_call_gemini_with_retry", _fake_call)
    history = _turns(12) + [{"role": "user", "content": "Quanto gastei no Civic este ano?"}]

    await _client_with_key().analyze_message(
        user_id="5521900000003",
        current_message="Quanto gastei no Civic este ano?",
        history=history,
    )

    prompt = captured["prompt"]
    assert prompt.count("mensagem-") == MAX_HISTORY_TURNS
    assert "mensagem-00" not in prompt and "mensagem-11" in prompt
    # A mensagem atual aparece uma única vez (não é duplicada no histórico)
    assert prompt.count("Quanto gastei no Civic este ano?") == 1
