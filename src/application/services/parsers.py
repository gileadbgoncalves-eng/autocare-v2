"""
AutoCare V2 - Utilitários de Formatação e Validação Escalar
Funções auxiliares puras sem heurísticas de NLP nem dicionários de inferência automotiva.
Toda extração semântica e decisão conversacional pertence 100% ao Google Gemini (AI-First).
"""

import re
import unicodedata
from typing import Optional

# Placas brasileiras (Mercosul ABC1D23 ou cinza ABC-1234 / ABC1234)
_RE_PLATE = re.compile(r"\b([A-Za-z]{3})[-\s]?(\d[A-Za-z0-9]\d{2})\b")

# Comandos operacionais de reversão/desfazer a última gravação (Soft-Delete Undo)
_UNDO_PATTERNS = [
    r"(?:^|,\s*|\.\s*|\be\s+)(?:desfaz(?:er)?(?:\s+isso(?:\s+que\s+(?:eu\s+)?te\s+mandei)?)?|desfa[çc]a|cancela\s+o\s+[uú]ltimo(?:\s+registro)?|apaga\s+isso(?:\s+que\s+(?:eu\s+)?te\s+mandei)?|apaga\s+o\s+[uú]ltimo|apague\s+isso)\s*[.!]*$",
]


def normalize_text(text: str) -> str:
    """Normaliza espaços e caixa para comparações operacionais."""
    if not text:
        return ""
    cleaned = unicodedata.normalize("NFKC", text.strip().lower())
    return re.sub(r"\s+", " ", cleaned)


def parse_odometer_km(text: str) -> Optional[int]:
    """
    Converte representações numéricas de quilometragem em inteiro absoluto.
    Suporta '85mil km', '85.000 km', '120k km', '51000 km', ou número puro quando isolado.
    """
    if not text:
        return None

    # 1. Prioridade: número explicitamente acompanhado de sufixo km/quilômetros ou mil/k
    m = re.search(
        r"(?<!R\$\s)(?<!R\$)(?<!/)\b(\d{1,3}(?:[.\s]\d{3})+|\d+(?:[.,]\d+)?)\s*(?:(mil|k)\s*(?:km|quil[oô]metros?)?|(mil|k)?\s*(?:km|quil[oô]metros?))\b",
        text,
        re.IGNORECASE,
    )
    if m:
        raw_num = m.group(1).strip()
        suffix = (m.group(2) or m.group(3) or "").lower()
        if suffix in ("mil", "k"):
            cleaned = raw_num.replace(".", "").replace(",", ".")
            try:
                val = int(float(cleaned) * 1000)
                return val if 0 <= val <= 2_000_000 else None
            except ValueError:
                return None
        if re.match(r"^\d{1,3}(?:[.\s]\d{3})+$", raw_num):
            cleaned = re.sub(r"[.\s]", "", raw_num)
            try:
                val = int(cleaned)
                return val if 0 <= val <= 2_000_000 else None
            except ValueError:
                return None
        cleaned = re.sub(r"\D", "", raw_num)
        if cleaned:
            val = int(cleaned)
            return val if 0 <= val <= 2_000_000 else None

    # 2. Fallback para número puro (ex: "45000" ou "145.000") quando não é data nem motor 1.0/1.4/2.0 nem ano
    for m_bare in re.finditer(r"(?<!R\$\s)(?<!R\$)(?<!/)\b(\d{1,3}(?:\.\d{3})+|\d{4,7})\b", text):
        raw_num = m_bare.group(1)
        cleaned = re.sub(r"\D", "", raw_num)
        if not cleaned:
            continue
        val = int(cleaned)
        if 1970 <= val <= 2030:
            continue
        if 0 <= val <= 2_000_000:
            return val

    return None


def parse_currency_brl(text: str) -> Optional[float]:
    """Extrai valores monetários em reais (ex: 'R$ 350,00', '350 reais')."""
    if not text:
        return None
    m = re.search(
        r"(?:R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)|"
        r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:reais|conto|pila|mangos)\b)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    if not raw:
        return None
    if "," in raw and "." in raw:
        normalized = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        normalized = raw.replace(",", ".")
    else:
        normalized = raw
    try:
        return round(float(normalized), 2)
    except ValueError:
        return None


def parse_plate_br(text: str) -> Optional[str]:
    """Extrai e normaliza placa brasileira em formato alfanumérico maiúsculo de 7 caracteres."""
    if not text:
        return None
    m = _RE_PLATE.search(text)
    if not m:
        return None
    return f"{m.group(1).upper()}{m.group(2).upper()}"


def is_undo_command(text: str) -> bool:
    """Identifica se o motorista pediu para desfazer/reverter o último registro salvo."""
    norm = normalize_text(text)
    if not norm:
        return False
    return any(re.search(p, norm) for p in _UNDO_PATTERNS)
