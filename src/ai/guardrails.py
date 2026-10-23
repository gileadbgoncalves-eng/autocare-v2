"""
Autoquer - Guardrails de custo e escopo do LLM.

Camadas de proteção (da mais barata para a mais cara):
1. `is_out_of_scope`: filtro determinístico, SEM chamada de API, que barra assuntos
   claramente fora do universo automotivo (receitas, piadas, programação, estudos...).
2. `prune_history`: limita o histórico enviado ao Gemini às últimas N interações.
3. Regra de bloqueio absoluto no System Instruction (cobre o que o filtro não pega).
"""

import re
import unicodedata
from typing import Any, Dict, Iterable, List

# Resposta única e literal para qualquer pedido fora do escopo automotivo.
OUT_OF_SCOPE_REPLY = (
    "Desculpe, sou o assistente exclusivo do Autoquer e só consigo ajudar com dúvidas e "
    "registros sobre o seu veículo. Como posso ajudar com seu carro hoje?"
)

# Quantidade máxima de turnos (mensagens) do histórico enviados à API.
MAX_HISTORY_TURNS = 5
# Teto de caracteres por turno do histórico (evita uma mensagem longa estourar o contexto).
MAX_HISTORY_CHARS_PER_TURN = 400


def _normalize(text: str) -> str:
    base = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in base if not unicodedata.combining(c))


def _turn_to_dict(turn: Any) -> Dict[str, str]:
    if isinstance(turn, dict):
        return {"role": str(turn.get("role", "")), "content": str(turn.get("content", ""))}
    return {"role": str(getattr(turn, "role", "")), "content": str(getattr(turn, "content", ""))}


def prune_history(
    turns: Iterable[Any] | None,
    max_turns: int = MAX_HISTORY_TURNS,
    current_message: str | None = None,
    max_chars: int = MAX_HISTORY_CHARS_PER_TURN,
) -> List[Dict[str, str]]:
    """
    Fatia o histórico para enviar à API somente as últimas `max_turns` interações.

    - Aceita dicts ({"role","content"}) ou objetos com `.role`/`.content` (ConversationTurn).
    - Se o último turno é a própria mensagem atual do usuário (já registrada no contexto),
      ele é descartado: ela é enviada separadamente e duplicá-la desperdiça tokens.
    - Cada turno é truncado a `max_chars` caracteres.
    """
    if not turns or max_turns <= 0:
        return []
    items = [_turn_to_dict(t) for t in turns]
    if (
        current_message is not None
        and items
        and items[-1]["role"] == "user"
        and items[-1]["content"].strip() == current_message.strip()
    ):
        items = items[:-1]
    pruned = items[-max_turns:]
    for item in pruned:
        if len(item["content"]) > max_chars:
            item["content"] = item["content"][:max_chars].rstrip() + "…"
    return pruned


# Termos que indicam claramente o universo automotivo: na dúvida, deixa o LLM decidir.
_AUTOMOTIVE_RE = re.compile(
    r"\b(carro|carros|moto|motos|veiculo|veiculos|automovel|automotivo|oleo|pneu|pneus|freio|freios|"
    r"motor|km|quilometragem|odometro|hodometro|placa|revisao|manutencao|oficina|mecanico|mecanica|"
    r"bateria|embreagem|suspensao|correia|vela|velas|filtro|alinhamento|balanceamento|abasteci|"
    r"abastecimento|gasolina|etanol|diesel|combustivel|multa|multas|infracao|cnh|ipva|licenciamento|"
    r"seguro|garagem|lembrete|lembra|civic|corolla|sandero|compass|gol|onix|hb20|strada|nivus|"
    r"fiat|honda|toyota|chevrolet|volkswagen|hyundai|jeep|renault|ford|nissan|yamaha|fazer|titan)\b"
)

# Assuntos claramente fora do escopo (receitas, humor, programação, estudos, etc.).
_OFF_TOPIC_RE = re.compile(
    r"(\breceitas?\b|\bbolo\b|\bingredientes?\b|\bculinaria\b|\bpiadas?\b|\bhumor\b|\bpoema\b|\bpoesia\b|"
    r"\bletra de musica\b|\bmusicas?\b|\bhoroscopo\b|\bsigno\b|\bprogramacao\b|\bprogramar\b|\bpython\b|"
    r"\bjavascript\b|\bjava\b|\bsql\b|\bhtml\b|\bcodigo fonte\b|\bscript\b|\bredacao\b|\bdever de casa\b|"
    r"\bequacao\b|\bfisica\b|\bquantica\b|\bquimica\b|\bmatematica\b|\bhistoria do\b|\bgeografia\b|"
    r"\bcapital d[aeo]\b|\bprevisao do tempo\b|\bfilmes?\b|\bseries?\b|\bnovela\b|\bfutebol\b|"
    r"\bbitcoin\b|\bcriptomoedas?\b|\bdieta\b|\bremedio\b|\btraduz\w*\b|\bresum[ae]\s+(?:o|esse|este)\s+texto\b|"
    r"\bconte uma\b|\bme conta uma\b|\bescreva (?:um|uma)\b|\bcrie (?:um|uma) (?:historia|texto|poema)\b)"
)


def is_out_of_scope(text: str) -> bool:
    """
    True quando a mensagem é claramente fora do universo automotivo e não contém nenhum
    termo automotivo. Determinístico e gratuito: roda antes de qualquer chamada de API.
    """
    norm = _normalize(text)
    if not norm.strip():
        return False
    if _AUTOMOTIVE_RE.search(norm):
        return False
    return bool(_OFF_TOPIC_RE.search(norm))
