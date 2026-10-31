"""
AutoCare V2 - Parsers / utilitários puros de texto (B15)

Módulo NEUTRO: não importa `conversation` nem `src.ai.client`.
É a base compartilhada por `conversation.py` e `src/ai/offline_fallback.py`,
quebrando o ciclo conversation <-> client.

Contém apenas funções puras (texto -> valor) e catálogos estáticos.
"""

import re
import difflib
import unicodedata
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional, List, Tuple


def normalize_text(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).strip().lower()


# =============================================================================
# CATÁLOGO DE ESPECIFICAÇÕES VEICULARES POPULARES (ENRIQUECIMENTO AUTOMÁTICO)
# =============================================================================

POPULAR_VEHICLE_SPECS: Dict[str, Dict[str, Any]] = {
    "nivus": {
        "make": "Volkswagen", "model": "Nivus", "version": "Highline",
        "engine": "1.0 Turbo", "transmission": "Automático", "steering": "Elétrica",
        "versions": {
            "highline": {"version": "Highline", "engine": "1.0 Turbo", "transmission": "Automático", "steering": "Elétrica"},
            "comfortline": {"version": "Comfortline", "engine": "1.0 Turbo", "transmission": "Automático", "steering": "Elétrica"},
        }
    },
    "corolla": {
        "make": "Toyota", "model": "Corolla", "version": "XEI",
        "engine": "2.0 Dynamic Force", "transmission": "Automático", "steering": "Elétrica",
        "versions": {
            "xei": {"version": "XEI", "engine": "2.0 Dynamic Force", "transmission": "Automático", "steering": "Elétrica"},
            "altis": {"version": "Altis", "engine": "2.0 Dynamic Force", "transmission": "Automático", "steering": "Elétrica"},
            "gli": {"version": "GLI", "engine": "2.0 Dynamic Force", "transmission": "Automático", "steering": "Elétrica"},
        }
    },
    "civic": {
        "make": "Honda", "model": "Civic", "version": "Touring",
        "engine": "1.5 Turbo", "transmission": "CVT", "steering": "Elétrica",
        "versions": {
            "touring": {"version": "Touring", "engine": "1.5 Turbo", "transmission": "CVT", "steering": "Elétrica"},
            "exl": {"version": "EXL", "engine": "2.0", "transmission": "CVT", "steering": "Elétrica"},
            "sport": {"version": "Sport", "engine": "2.0", "transmission": "CVT", "steering": "Elétrica"},
        }
    },
    "sandero": {"make": "Renault", "model": "Sandero", "version": "Zen", "engine": "1.0", "transmission": "Manual", "steering": "Eletro-hidráulica"},
    "onix": {"make": "Chevrolet", "model": "Onix", "version": "Premier", "engine": "1.0 Turbo", "transmission": "Automático", "steering": "Elétrica"},
    "hb20": {"make": "Hyundai", "model": "HB20", "version": "Platinum", "engine": "1.0 Turbo", "transmission": "Automático", "steering": "Elétrica"},
    "compass": {"make": "Jeep", "model": "Compass", "version": "Longitude", "engine": "1.3 Turbo", "transmission": "Automático", "steering": "Elétrica"},
    "renegade": {"make": "Jeep", "model": "Renegade", "version": "Sport", "engine": "1.3 Turbo", "transmission": "Automático", "steering": "Elétrica"},
    "polo": {"make": "Volkswagen", "model": "Polo", "version": "Highline", "engine": "1.0 TSI", "transmission": "Automático", "steering": "Elétrica"},
    "gol": {"make": "Volkswagen", "model": "Gol", "version": "1.0", "engine": "1.0", "transmission": "Manual", "steering": "Hidráulica"},
    "celta": {"make": "Chevrolet", "model": "Celta", "version": "Life", "engine": "1.0", "transmission": "Manual", "steering": "Mecânica"},
    "palio": {"make": "Fiat", "model": "Palio", "version": "Fire", "engine": "1.0", "transmission": "Manual", "steering": "Hidráulica"},
    "uno": {"make": "Fiat", "model": "Uno", "version": "Mille", "engine": "1.0", "transmission": "Manual", "steering": "Mecânica"},
    "corsa": {"make": "Chevrolet", "model": "Corsa", "version": "Joy", "engine": "1.0", "transmission": "Manual", "steering": "Hidráulica"},
    "siena": {"make": "Fiat", "model": "Siena", "version": "ELX", "engine": "1.0", "transmission": "Manual", "steering": "Hidráulica"},
}

KNOWN_MODEL_MAKES: Dict[str, Dict[str, str]] = {
    k: {"make": v["make"], "model": v["model"]} for k, v in POPULAR_VEHICLE_SPECS.items()
}
KNOWN_MODEL_MAKES.update({
    "fiorino": {"make": "Fiat", "model": "Fiorino"},
    "fusca": {"make": "Volkswagen", "model": "Fusca"},
    "brasília": {"make": "Volkswagen", "model": "Brasília"},
    "brasilia": {"make": "Volkswagen", "model": "Brasília"},
})


def parse_steering_type(text: str, model_name: Optional[str] = None, year: Optional[int] = None) -> Optional[str]:
    """
    Parser robusto para tipo de direção com tratamento de typos e tolerância fonética.
    - 'hidrauliza', 'hidraulica', 'hidro', 'hidra' -> 'Hidráulica'
    - 'mecanica', 'mecânica', 'sem direção', 'direção dura', 'direção manual' -> 'Mecânica'
    - 'eletro-hidráulica', 'eletrohidraulica' -> 'Eletro-hidráulica'
    - 'direção elétrica' -> 'Elétrica' (desambigua estritamente de 'vidro elétrico', 'trava elétrica')
    - Coerência histórica: modelos populares dos anos 2000 (ex: Palio 2008, Gol G4, Celta) não tinham direção elétrica de fábrica.
    """
    tl = (text or "").lower()

    # 1. Hidráulica e typos comuns ("hidrauliza", "hidraulica", "hidro", "hidra")
    if re.search(r"\b(?:dire[cç][aã]o\s+)?(hidr[aá]uliz\w*|hidr[aá]ulic\w*|hidro|hidra)\b", tl):
        return "Hidráulica"

    # 2. Mecânica / Manual na direção
    if re.search(r"\b(?:dire[cç][aã]o\s+)?(mec[aâ]nic[ao]|sem\s+dire[cç][aã]o|dire[cç][aã]o\s+dura)\b", tl) or re.search(r"dire[cç][aã]o\s+manual", tl):
        return "Mecânica"

    # 3. Eletro-hidráulica
    if "eletro-hidr" in tl or "eletrohidr" in tl:
        return "Eletro-hidráulica"

    # 4. Elétrica: SOMENTE se for explicitamente associado à direção
    # Evita falsos positivos com 'vidro elétrico', 'trava elétrica', 'retrovisor elétrico'
    if re.search(r"\bdire[cç][aã]o\s+(?:[eé]l[eé]tric\w*)\b", tl) or re.search(r"\b(?:[eé]l[eé]tric\w*)\s+na\s+dire[cç][aã]o\b", tl):
        model_clean = (model_name or "").lower()
        older_popular = ("palio", "gol", "celta", "uno", "corsa", "siena", "clio", "fiesta", "fiorino", "fusca", "brasilia")
        if any(op in model_clean for op in older_popular) and (year is None or year < 2016):
            return "Hidráulica"
        return "Elétrica"

    return None


def find_popular_vehicle_spec(text: str) -> Optional[Dict[str, Any]]:
    """Identifica marca, modelo e ficha técnica para modelos populares."""
    tl = (text or "").lower()
    for key, spec in POPULAR_VEHICLE_SPECS.items():
        if key in tl:
            base = {
                "make": spec["make"],
                "model": spec["model"],
                "version": spec.get("version"),
                "engine": spec.get("engine"),
                "transmission": spec.get("transmission"),
                "steering": spec.get("steering"),
            }
            if "versions" in spec:
                for v_key, v_info in spec["versions"].items():
                    if v_key in tl:
                        base.update(v_info)
                        break
            return base
    return None


# =============================================================================
# HELPERS ENXUTOS DE APOIO (VALIDAÇÕES E PARSERS ESSENCIAIS)
# =============================================================================

def parse_km_value(text: str) -> Optional[int]:
    """Extrai valor numérico de quilometragem."""
    tl = (text or "").lower()
    m_mil = re.search(r"(\d+(?:[.,]\d+)?)\s*mil\s*(?:de\s*)?km", tl)
    if m_mil:
        val = float(m_mil.group(1).replace(",", "."))
        return int(val * 1000)
    m_km = re.search(r"(?:com|em|bateu|para|pra|odometro|hodometro|hodômetro)?\s*(\d{1,3}(?:\.\d{3})+|\d+)\s*(?:km|quil[oô]metros?)", tl)
    if m_km:
        raw = m_km.group(1).replace(".", "")
        return int(raw)
    return None


def parse_cost_value(text: str) -> Optional[float]:
    """Extrai valor financeiro em reais."""
    tl = (text or "").lower()
    m_cost = re.search(r"(?:r\$\s*|reais\s*|por\s*|valor\s*(?:de\s*)?|gastei\s*|custou\s*|morreu\s*(?:em\s*)?|paguei\s*)(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+,\d{2}|\d+)\s*(?:reais|conto)?", tl)
    if m_cost:
        raw = m_cost.group(1).replace(".", "").replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            pass
    m_simple = re.search(r"\b(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)\s*(?:reais|conto)\b", tl)
    if m_simple:
        try:
            return float(m_simple.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            pass
    return None


def validate_brazilian_plate(plate: str, context_hint: str = "") -> Dict[str, Any]:
    """Valida formato de placa de veículo (Mercosul, Amarela ou Cinza Antiga)."""
    clean = re.sub(r"[\s\-]", "", plate).upper()
    hint = context_hint.lower()
    is_mercosul = bool(re.match(r"^[A-Z]{3}[0-9][A-Z][0-9]{2}$", clean))
    is_amarela = bool(re.match(r"^[A-Z]{2}[0-9]{4}$", clean))
    warning = ""
    if "amarela" in hint or is_amarela:
        warning = "⚠️ Placa no formato antigo amarelo (2 letras e 4 números). Necessário recadastramento para Mercosul."
    elif "mercosul" in hint and not is_mercosul:
        warning = "❌ Placa informada como Mercosul, mas não é padrão mercosul (formato antigo cinza de 3 letras e 4 números)."
    return {"plate": clean, "is_mercosul": is_mercosul, "is_amarela": is_amarela, "warning": warning}


def clean_workshop_name(text: str) -> Optional[str]:
    """Filtra ruídos circunstanciais preservando nomes legítimos de estabelecimentos."""
    raw = (text or "").strip()
    if not raw or any(g in raw.lower() for g in ("época", "lugar", "rua", "tempo", "esquina", "lá naquele")):
        m = re.search(r"(oficina\s+do\s+[a-zA-ZÀ-ÿ]+|auto\s+center\s+[a-zA-ZÀ-ÿ\s]+)", raw, re.I)
        if m:
            return re.sub(r"(?:da\s+esquina|lá\s+naquele\s+lugar|na\s+época)[,\s]*", "", m.group(0), flags=re.I).strip()
        return None
    return raw


KNOWN_MAINTENANCE_ITEMS = {
    "óleo": "troca de óleo",
    "oleo": "troca de óleo",
    "filtro de óleo": "filtro de óleo",
    "filtro de oleo": "filtro de óleo",
    "filtro de combustível": "filtro de combustível",
    "filtro de combustivel": "filtro de combustível",
    "filtro de cabine": "filtro de cabine",
    "filtro": "filtro de óleo",
    "pastilha de freio": "pastilhas de freio",
    "pastilhas de freio": "pastilhas de freio",
    "pastilha": "pastilhas de freio",
    "pastilhas": "pastilhas de freio",
    "disco de freio": "discos de freio",
    "discos de freio": "discos de freio",
    "discos": "discos de freio",
    "vela": "velas de ignição",
    "velas": "velas de ignição",
    "alinhamento": "alinhamento",
    "balanceamento": "balanceamento",
    "correia dentada": "correia dentada",
    "correia de acessórios": "correia de acessórios",
    "bateria": "bateria",
    "pneu": "troca de pneus",
    "pneus": "troca de pneus",
    "amortecedor": "amortecedores",
    "amortecedores": "amortecedores",
    "fluido de freio": "fluido de freio",
    "arrefecimento": "fluido de arrefecimento",
}


def extract_maintenance_items_strict(text: str) -> List[str]:
    tl = (text or "").lower()
    found, seen = [], set()
    for kw, canonical in sorted(KNOWN_MAINTENANCE_ITEMS.items(), key=lambda x: len(x[0]), reverse=True):
        if re.search(r"\b" + re.escape(kw) + r"\b", tl):
            if canonical not in seen:
                seen.add(canonical)
                found.append(canonical)
    return found


def merge_maintenance_items(existing: List[str], new_items: List[str]) -> List[str]:
    seen, res = set(), []
    for it in (existing or []) + (new_items or []):
        clean = str(it).strip()
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            res.append(clean)
    return res


def extract_date_strict(text: str) -> Optional[str]:
    tl = (text or "").lower()
    m_abs = re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b", tl)
    if m_abs:
        d, m = int(m_abs.group(1)), int(m_abs.group(2))
        y = int(m_abs.group(3)) if m_abs.group(3) else 2026
        if y < 100:
            y += 2000
        return f"{d:02d}/{m:02d}/{y}"
    m_ext = re.search(r"\b(?:dia\s*)?(\d{1,2})\s+de\s+(janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)(?:\s+de\s+(\d{4}))?\b", tl)
    if m_ext:
        months = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
        d, m = int(m_ext.group(1)), months[m_ext.group(2)]
        y = int(m_ext.group(3)) if m_ext.group(3) else 2026
        return f"{d:02d}/{m:02d}/{y}"
    if "ontem" in tl:
        return (datetime.now() - timedelta(days=1)).strftime("%d/%m/%Y")
    if "hoje" in tl:
        return datetime.now().strftime("%d/%m/%Y")
    return None


def extract_workshop_strict(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.search(r"(?:l[aá]\s+no\s+|l[aá]\s+na\s+|na\s+|em\s+|no\s+)?\b([A-Za-zÀ-ÿ0-9\-]+(?:\s+[A-Za-zÀ-ÿ0-9\-]+)?\s+Auto\s+Center)\b", raw, re.I)
    if m:
        return clean_workshop_name(m.group(1).strip())
    m2 = re.search(r"\b(Oficina\s+do\s+[A-Za-zÀ-ÿ0-9\-]+|Auto\s+Center\s+[A-Za-zÀ-ÿ0-9\-]+|Mec[aâ]nica\s+[A-Za-zÀ-ÿ0-9\-]+|Concession[aá]ria\s+[A-Za-zÀ-ÿ0-9\-]+)\b", raw, re.I)
    if m2:
        return clean_workshop_name(m2.group(1).strip())
    return None


def is_edit_last_service_intent(text: str, context: Optional[Any] = None) -> bool:
    tl = (text or "").lower()
    if context and (getattr(context, "pending_edit_field", None) or getattr(context, "pending_edit_awaiting_choice", False)):
        return True

    # Consultas e perguntas nunca são comandos de edição
    if re.search(r"\b(qual|quando|quanto|quantos|o que|quais|ver|mostrar|listar|consulta|hist[oó]rico)\b", tl):
        return False

    patterns = [
        r"\b(alterar|mudar|corrigir|editar|atualizar|retificar|arrumar|consertar)\b.*\b(?:[uú]ltim[oa]\s+)?(?:servi[çc]o|registro|manuten[çc][ãa]o)\b",
        r"\b(?:quero|preciso|gostaria de)\s+(?:alterar|mudar|editar|corrigir|atualizar|arrumar)\b.*\b(?:[uú]ltim[oa]\s+)?(?:servi[çc]o|registro|manuten[çc][ãa]o|deste|desse)\b",
        r"\berrei\s+(?:a\s+data|o\s+valor|o\s+km|os\s+itens|a\s+oficina|o\s+preço)\b",
        r"\b(corrigir|alterar|mudar|editar)\s+(?:o\s+km|o\s+valor|a\s+data|os\s+itens|a\s+oficina)\s+(?:d[eo]ss?e\s+)?(?:[uú]ltimo\b)?",
        r"\b(adicionar|incluir)\b.*\b(?:no|ao)\s+(?:[uú]ltim[oa]\s+)?(?:servi[çc]o|registro|manuten[çc][ãa]o)\b",
    ]
    return any(re.search(p, tl) for p in patterns)
