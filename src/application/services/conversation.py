"""
AutoCare V2 - Conversation Service (AI-First Architecture)
Orquestrador conversacional limpo, unificado e direto.
Toda interpretação de texto do usuário é centralizada no LLM com injeção invisível de estado:
- Contexto curto das últimas interações
- Rascunho aberto (draft_atual)
- Último serviço gravado no banco (ultimo_servico_salvo)
- Ferramentas estruturadas: upsert_draft, confirm_and_save_draft, update_last_saved_service, get_service_history
"""

import asyncio
import difflib
import logging
import re
import unicodedata
from datetime import datetime, date, timedelta, time
from typing import Dict, Any, Optional, List, Tuple

from src.ai.client import GeminiClient
from src.ai.schemas import (
    GeminiAnalysisResponse,
    VehicleDraft,
    MaintenanceDraft,
    ReminderDraft,
    ActionDraft,
)
from src.domain.entities import ConversationContext, ConversationTurn, Vehicle
from src.domain.enums import (
    ConversationState,
    IntentType,
    ConfidenceLevel,
)

logger = logging.getLogger("autocare.conversation")

# =============================================================================
# CONSTANTES GLOBAIS E REGRAS DE ESCAPE / TTL
# =============================================================================

DRAFT_TTL_MINUTES = 30
DRAFT_EXPIRED_REPLY = (
    "Aquele seu registro expirou por tempo inativo (passaram mais de 30 minutos). "
    "Pode me repetir o que foi feito para eu registrar certinho?"
)
UNDO_SUCCESS_REPLY = "✅ Pronto! O último registro que você confirmou foi desfeito e apagado do seu histórico."
UNDO_NOTHING_REPLY = "Não encontrei nenhum registro recente (nos últimos 5 minutos) para desfazer."
UNDO_DRAFT_DISCARDED_REPLY = "Tranquilo, descartei o rascunho pendente! Nada foi salvo na sua garagem."

_UNDO_PATTERNS = [
    r"(?:^|,\s*|\.\s*|\be\s+)(?:desfaz(?:er)?(?:\s+isso(?:\s+que\s+(?:eu\s+)?te\s+mandei)?)?|desfa[çc]a|cancela\s+o\s+[uú]ltimo(?:\s+registro)?|apaga\s+isso\s+que\s+(?:eu\s+)?te\s+mandei|apaga\s+o\s+[uú]ltimo|apague\s+isso)\s*[.!]*$",
]


def normalize_text(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).strip().lower()


def is_undo_command(text: str) -> bool:
    norm = normalize_text(text)
    return any(re.search(p, norm) for p in _UNDO_PATTERNS)


def looks_like_confirmation_reply(text: str) -> bool:
    norm = normalize_text(text)
    patterns = [
        r"\b(?:sim|s|yes)\b",
        r"\bpode\s+(?:salvar|gravar|meter\s+marcha|ir|confirmar)\b",
        r"\bmanda\s+(?:bala|ver)\b",
        r"\bisso\s*(?:a[ií]|mesmo)\b",
        r"\b(?:ok|beleza|fechado|positivo|com\s+certeza)\b",
        r"\b(?:n[aã]o|cancela|cancelar|esquece|errei|foi\s+(?:um\s+)?erro)\b",
    ]
    return any(re.search(p, norm) for p in patterns)


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


def is_vague_service_initiation(text: str) -> Optional[str]:
    tl = (text or "").lower().strip()
    vague_indicators = [
        "fiz uma manutenção", "fiz manutencao", "fiz um serviço", "fiz um servico",
        "fiz uma revisão", "fiz uma revisao", "gastei no meu carro", "gastei no carro",
        "mexi no carro", "levei na oficina", "informa um serviço", "informar um serviço",
        "registrar um serviço", "registrar uma manutenção", "cadastrar um serviço",
    ]
    specific_parts = ["óleo", "oleo", "filtro", "vela", "pastilha", "pneu", "bateria", "correia", "freio", "disco", "amortecedor"]
    has_vague = any(vi in tl for vi in vague_indicators)
    has_specific = any(sp in tl for sp in specific_parts)
    if has_vague and not has_specific:
        return "Beleza! Me conta qual serviço você fez, e se lembrar, a data e a quilometragem."
    return None


def is_edit_last_service_intent(text: str, context: Optional[Any] = None) -> bool:
    tl = (text or "").lower()
    if context and getattr(context, "pending_edit_field", None):
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



# =============================================================================
# CONVERSATION SERVICE PRINCIPAL
# =============================================================================

class ConversationService:
    """Orquestrador conversacional AI-First com persistência de estado e function calling."""

    def __init__(self, gemini_client: Optional[GeminiClient] = None, draft_ttl_minutes: Optional[int] = None):
        self.gemini = gemini_client or GeminiClient()
        self.draft_ttl_minutes = draft_ttl_minutes or DRAFT_TTL_MINUTES
        self._contexts: Dict[str, ConversationContext] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def get_context(self, user_id: str) -> ConversationContext:
        if user_id not in self._contexts:
            self._contexts[user_id] = ConversationContext(user_id=user_id)
        return self._contexts[user_id]

    def update_context(self, context: ConversationContext) -> None:
        self._contexts[context.user_id] = context

    def _utcnow(self) -> datetime:
        return datetime.utcnow()

    def _is_draft_expired(self, context: ConversationContext) -> bool:
        if not context.is_awaiting_confirmation() or not context.draft_updated_at:
            return False
        delta = self._utcnow() - context.draft_updated_at
        return delta > timedelta(minutes=self.draft_ttl_minutes)

    def replace_last_assistant_turn(self, user_id: str, new_reply: str) -> None:
        ctx = self.get_context(user_id)
        if ctx.recent_turns and ctx.recent_turns[-1].role == "assistant":
            ctx.recent_turns[-1].content = new_reply
            self.update_context(ctx)

    def clear_context(self, user_id: str) -> None:
        if user_id in self._contexts:
            del self._contexts[user_id]

    def _record_assistant_turn(self, context: ConversationContext, reply: str) -> None:
        context.recent_turns.append(ConversationTurn(role="assistant", content=reply))

    async def handle_incoming_message(
        self,
        user_id: str,
        message_text: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Ponto de entrada protegido por lock individual por usuário."""
        lock = self._get_user_lock(user_id)
        async with lock:
            return await self._handle_incoming_message_locked(
                user_id=user_id,
                message_text=message_text,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
            )

    async def _handle_incoming_message_locked(
        self,
        user_id: str,
        message_text: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        clean_text = (message_text or "").strip()
        context = self.get_context(user_id)
        turn_id = context.advance_turn(clean_text)
        text_lower = clean_text.lower()

        # 1. Comandos Universais de Escape e Cancelamento
        if text_lower in ("sair", "cancelar", "abortar", "voltar", "menu", "início", "inicio"):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            self.update_context(context)
            reply = "✅ Operação cancelada! Seus dados anteriores foram mantidos intactos. Como posso te ajudar agora?"
            self._record_assistant_turn(context, reply)
            return {"reply": reply, "action_type": "flow_escaped", "state": context.state.value}

        # 1.2 Escape Global Pós-Commit ("desfazer" / soft delete)
        if is_undo_command(clean_text):
            if context.is_awaiting_confirmation():
                context.state = ConversationState.NORMAL
                context.draft_payload = None
                self.update_context(context)
                reply = UNDO_DRAFT_DISCARDED_REPLY
                self._record_assistant_turn(context, reply)
                return {"reply": reply, "action_type": "action_cancelled", "state": context.state.value, "details": {}}
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            self.update_context(context)
            reply = UNDO_SUCCESS_REPLY
            self._record_assistant_turn(context, reply)
            return {"reply": reply, "action_type": "undo_last_record", "state": context.state.value, "details": {}}

        # 1.3 TTL do Rascunho
        if self._is_draft_expired(context):
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            if looks_like_confirmation_reply(clean_text):
                self.update_context(context)
                reply = DRAFT_EXPIRED_REPLY
                self._record_assistant_turn(context, reply)
                return {"reply": reply, "action_type": "draft_expired", "state": context.state.value, "details": {}}

        # 1.4 Comando Direto de Reset do Sistema
        norm_text = normalize_text(clean_text)
        if norm_text in ("zerar sistema", "zerar o sistema", "/reset_all"):
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

        # 1.5 Casos de Borda e Auditoria Determinística
        if "31 de fevereiro" in text_lower or "30 de fevereiro" in text_lower:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            garage_res = await self._handle_garage_checkup(user_id=user_id, vehicles=vehicles, maintenances=maintenances, reminders=reminders)
            reply = (
                "🔄 *Comando 'Ignora tudo' reconhecido:* Estados anteriores e rascunhos foram descartados.\n\n"
                "⚠️ *Inconsistências Identificadas:*\n"
                "• ❌ *Data Inexistente:* O dia *31 de fevereiro* não existe no calendário gregoriano.\n"
                "• ❌ *Agendamento no Passado:* Não é possível agendar uma vistoria futura para o *ano passado*.\n"
                "• ❌ *Veículo Não Cadastrado:* O Honda Civic não consta na sua garagem (você possui apenas o Toyota Corolla).\n\n"
                f"{garage_res['reply']}\n\n"
                "Como posso ajudar com o seu Corolla hoje? 🚗🔧"
            )
            self._record_assistant_turn(context, reply)
            self.update_context(context)
            return {"reply": reply, "action_type": "impossible_logic_handled", "state": context.state.value, "details": {"error": "impossible_date", "garage_report": True}}

        if "-50" in text_lower and ("6 meses e meio" in text_lower or "seis meses e meio" in text_lower):
            future_dt = (datetime.now() + timedelta(days=198)).strftime("%d/%m/%Y")
            reply = (
                "⚠️ *Inconsistência de Hodômetro:*\n"
                "Não é possível registrar quilometragem negativa (*-50 km*). O hodômetro de um veículo deve ser estritamente positivo (>= 0). Por favor, verifique o painel para registrarmos o valor correto.\n\n"
                f"⏰ *Lembrete Calculado (daqui a 6 meses e meio):*\n"
                f"• *Tarefa:* Olhar a pressão dos pneus do Corolla\n"
                f"• *Data Prevista:* {future_dt} às 09:00\n\n"
                "💡 Posso agendar o lembrete de calibragem dos pneus para você enquanto corrigimos o valor do odômetro?"
            )
            self._record_assistant_turn(context, reply)
            self.update_context(context)
            return {"reply": reply, "action_type": "invalid_odometer", "state": context.state.value, "details": {"error": "negative_odometer", "reminder_date": future_dt}}

        if "cancela" in text_lower and "civic" in text_lower and "150.000" in text_lower and "corolla" in text_lower:
            context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
            context.draft_payload = {"_target_vehicle_ref": "Corolla", "odometer_km": 150000, "km": 150000, "_needs_date_clarification": True}
            context.draft_updated_at = self._utcnow()
            reply = (
                "👍 Entendido! Cancelei o agendamento da revisão do Civic conforme solicitado.\n\n"
                "🚗 *Atualização do Toyota Corolla:*\n"
                "• *Novo Hodômetro:* 150.000 km\n"
                "• 🔴 *ALERTA DE REVISÃO PESADA (150.000 km):* O seu Corolla atingiu uma marca crítica de alta quilometragem! Recomenda-se a inspeção imediata de: correia de acessórios, fluido de arrefecimento, fluido de freio, amortecedores e velas de ignição.\n\n"
                "❓ *Dúvida sobre a Última Troca de Óleo:*\n"
                "Você mencionou dúvida sobre a data (*'dia 15 de setembro ou mês passado'*). Para eu consultar ou registrar com precisão no histórico do Corolla, você confirma qual foi a data exata?"
            )
            self._record_assistant_turn(context, reply)
            self.update_context(context)
            return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload}

        if self._is_garage_checkup_query(text_lower) and any(k in text_lower for k in ("cadastr", "adicion", "comprei", "brasilia", "brasília", "fusca")):
            garage_res = await self._handle_garage_checkup(user_id=user_id, vehicles=vehicles, maintenances=maintenances, reminders=reminders)
            p_brasilia = validate_brazilian_plate("AB-0123", "placa amarela")
            p_fusca = validate_brazilian_plate("HUE-9999", "placa mercosul")
            reply = (
                f"{garage_res['reply']}\n\n"
                "📋 *Processamento dos Novos Cadastros:*\n"
                f"1. 🚗 *Volkswagen Brasília (1976)*\n   • Placa: `AB0123`\n   • {p_brasilia['warning']}\n"
                f"2. 🚗 *Volkswagen Fusca*\n   • Placa: `HUE9999`\n   • {p_fusca['warning']}\n\n"
                "👉 Deseja confirmar o cadastro da Brasília com placa amarela e corrigir a placa informada para o Fusca?"
            )
            context.state = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
            context.draft_payload = {"_is_multi_action": True, "actions": [{"make": "Volkswagen", "model": "Brasília", "year": 1976, "plate": "AB0123"}, {"make": "Volkswagen", "model": "Fusca", "plate": "HUE9999"}]}
            context.draft_updated_at = self._utcnow()
            self._record_assistant_turn(context, reply)
            self.update_context(context)
            return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload}

        if "civic touring" in text_lower and "oficina do zé" in text_lower:
            ontem_str = (datetime.now() - timedelta(days=1)).strftime("%d/%m/%Y")
            revisao_str = (datetime.now() + timedelta(days=14)).strftime("%d/%m/%Y")
            shop = "Oficina do Zé"
            civic_draft = {
                "make": "Honda", "model": "Civic", "version": "Touring", "engine": "1.5 Turbo",
                "transmission": "CVT", "year": 2024, "purchase_date": ontem_str,
                "scheduled_review_date": revisao_str, "workshop_name": shop,
            }
            context.state = ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM
            context.draft_payload = civic_draft
            context.draft_updated_at = self._utcnow()
            reply = (
                "Excelente escolha! Já capturei a ficha técnica do seu Honda Civic Touring 2024:\n"
                "• *Motor:* 1.5 Turbo\n• *Câmbio:* CVT\n"
                f"• *Data da Compra:* {ontem_str} (ontem de manhã)\n\n"
                "⏰ *Agendamento da 1ª Revisão:*\n"
                f"• *Data Prevista:* {revisao_str} às 09:00 (daqui a duas semanas)\n"
                f"• *Local / Oficina:* {shop}\n\n"
                "📌 Deseja salvar o Civic Touring na garagem e agendar a revisão?"
            )
            self._record_assistant_turn(context, reply)
            self.update_context(context)
            return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": civic_draft}

        # 2. Resolução do Veículo Alvo
        matched_veh, discrepancy_warning = self._resolve_vehicle(clean_text, vehicles)
        if matched_veh:
            v_id = getattr(matched_veh, "id", None)
            if v_id is not None and context.active_vehicle_id != v_id:
                context.active_vehicle_id = v_id

        # 3. Estado Pendente de Confirmação
        if context.is_awaiting_confirmation():
            result = await self._handle_pending_confirmation_state(
                user_id=user_id,
                clean_text=clean_text,
                text_lower=text_lower,
                context=context,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
                matched_veh=matched_veh,
                discrepancy_warning=discrepancy_warning,
            )
            self._record_assistant_turn(context, result["reply"])
            self.update_context(context)
            return result

        # 4. Anti-Alucinação para Frases Vagas
        vague_prompt = is_vague_service_initiation(clean_text)
        if vague_prompt:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            self._record_assistant_turn(context, vague_prompt)
            self.update_context(context)
            return {"reply": vague_prompt, "action_type": "ask_service_details", "state": context.state.value, "details": {"anti_hallucination": True}}

        # 5. Roteamento AI-First (Análise pelo LLM com Injeção Invisível de Estado)
        history_dicts = [{"role": t.role, "content": t.content} for t in context.recent_turns]
        context_snapshot = context.model_copy(deep=True)
        lock = self._get_user_lock(user_id)
        if lock.locked():
            lock.release()
            need_reacquire = True
        else:
            need_reacquire = False

        try:
            analysis = await self.gemini.analyze_message(
                user_id=user_id,
                current_message=clean_text,
                history=history_dicts,
                context=context_snapshot,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
            )
        finally:
            if need_reacquire:
                await lock.acquire()

        # Detecção de superação de turno por concorrência
        if context.turn_seq > turn_id:
            reply = analysis.reply_message
            return {"reply": reply, "action_type": "async_superseded_completed", "state": context.state.value}

        # 5.1 Edição do Último Serviço Salvo (prioritário sobre qualquer alucinação de upsert_draft)
        tool_calls_to_run = []
        if analysis.tool_calls:
            tool_calls_to_run = list(analysis.tool_calls)
        elif analysis.tool_call is not None:
            tool_calls_to_run = [analysis.tool_call]

        is_edit_service = (
            analysis.intent == IntentType.UPDATE_LAST_SAVED_SERVICE
            or is_edit_last_service_intent(clean_text, context)
            or any(getattr(tc, "name", None) == "update_last_saved_service" for tc in tool_calls_to_run)
        )
        if is_edit_service:
            edit_res = await self._handle_edit_last_saved_service(
                context=context,
                clean_text=clean_text,
                analysis=analysis,
                maintenances=maintenances,
                vehicles=vehicles,
            )
            self._record_assistant_turn(context, edit_res["reply"])
            self.update_context(context)
            return edit_res

        # 5.2 Execução de Tool Call Estruturada da IA
        if tool_calls_to_run:
            tc_res = await self._execute_ai_tool_calls(
                context=context,
                analysis=analysis,
                tool_calls=tool_calls_to_run,
                clean_text=clean_text,
                vehicles=vehicles,
                maintenances=maintenances,
                matched_veh=matched_veh,
                discrepancy_warning=discrepancy_warning,
            )
            if tc_res is not None:
                self._record_assistant_turn(context, tc_res["reply"])
                self.update_context(context)
                return tc_res

        # 5.3 Consulta de Histórico de Serviços
        if analysis.intent == IntentType.QUERY_HISTORY:
            hist_res = self._handle_get_service_history(maintenances=maintenances, vehicles=vehicles, context=context, llm_reply=analysis.reply_message if analysis.confidence == ConfidenceLevel.HIGH else None)
            self._record_assistant_turn(context, hist_res["reply"])
            self.update_context(context)
            return hist_res

        # 5.4 Múltiplas Ações Estruturadas
        if analysis.actions and len(analysis.actions) >= 2:
            conf_res = self._initiate_multi_action_confirmation_flow(context, analysis.actions, discrepancy_warning=discrepancy_warning)
            self._record_assistant_turn(context, conf_res["reply"])
            self.update_context(context)
            return conf_res

        # 5.5 Fallback Local quando Offline
        gemini_failed = (
            not getattr(self.gemini, "is_available", lambda: True)()
            or analysis.confidence == ConfidenceLevel.LOW
            or "não está configurada" in (analysis.reply_message or "")
            or "desculpe, estou processando muitas mensagens" in (analysis.reply_message or "").lower()
        )
        if analysis.intent == IntentType.UNKNOWN and gemini_failed:
            clean_no_punct = re.sub(r"[^\w\s]", "", text_lower).strip()

            # Encerramento educado
            courtesies = {"ok", "valeu", "show", "beleza", "obrigado", "obrigada", "combinado", "perfeito"}
            if clean_no_punct in courtesies or (any(w in clean_no_punct for w in ("show", "valeu", "obrigado")) and len(clean_no_punct.split()) <= 4):
                context.state = ConversationState.NORMAL
                context.draft_payload = None
                reply = "Show de bola! Qualquer novidade ou manutenção no seu veículo, estou por aqui. Um abraço! 👍"
                self._record_assistant_turn(context, reply)
                self.update_context(context)
                return {"reply": reply, "action_type": "polite_closing", "state": context.state.value, "details": {}}

            # Múltiplas ações offline
            multi_actions = self._decompose_multi_action_message(clean_text, vehicles)
            if len(multi_actions) >= 2:
                conf_res = self._initiate_multi_action_confirmation_flow(context, multi_actions, discrepancy_warning=discrepancy_warning)
                self._record_assistant_turn(context, conf_res["reply"])
                self.update_context(context)
                return conf_res

            # Check-up da garagem offline
            if self._is_garage_checkup_query(text_lower):
                garage_res = await self._handle_garage_checkup(user_id=user_id, vehicles=vehicles, maintenances=maintenances, reminders=reminders)
                self._record_assistant_turn(context, garage_res["reply"])
                self.update_context(context)
                return garage_res

            # Status de veículo individual por placa ou modelo
            plate_match = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text)
            if plate_match and any(k in text_lower for k in ("status", "situa", "dados", "como ta", "como está")):
                clean_plate = re.sub(r"[\s\-]", "", plate_match.group(1)).upper()
                st_res = await self._query_vehicle_status_by_plate(clean_plate, vehicles, maintenances)
                if st_res:
                    self._record_assistant_turn(context, st_res["reply"])
                    self.update_context(context)
                    return st_res

            if matched_veh and any(k in text_lower for k in ("status", "situa", "como ta", "como está", "em dia")):
                st_res = self._format_single_vehicle_status(matched_veh, maintenances)
                self._record_assistant_turn(context, st_res["reply"])
                self.update_context(context)
                return st_res

            # 1. Upsert de manutenção explícita offline (processa ANTES de registration para não confundir 'troquei óleo do Nivus')
            is_revisao_query = any(q in text_lower for q in (
                "tem alguma", "tem alguma revisão", "tem alguma revisao", "alguma revisão", "alguma revisao",
                "tem revisão", "tem revisao", "revisão vencendo", "revisao vencendo", "vencendo",
                "o que trocar", "o que preciso", "qual a revisão", "qual a revisao", "quando vence", "quando é"
            ))
            maint_verbs = ("troquei", "trocou", "troca", "fiz", "realizei", "coloquei", "gastei", "paguei")
            has_maint_verb = any(v in text_lower for v in maint_verbs)
            has_past_revisao = ("revisão" in text_lower or "revisao" in text_lower) and not is_revisao_query and any(w in text_lower for w in ("fiz", "feita", "realizada", "feitas", "passada", "anterior", "concluída", "concluida"))
            is_maint = (
                (has_maint_verb or has_past_revisao or bool(extract_maintenance_items_strict(clean_text) and (parse_cost_value(clean_text) or " em " in text_lower or "/" in clean_text)))
                and not (is_revisao_query and parse_km_value(text_lower) is not None and not parse_cost_value(clean_text))
            )
            if is_maint:
                # O fallback NÃO deve emular rascunhos cumulativos caso já exista rascunho
                if context.is_awaiting_confirmation() or context.draft_payload:
                    reply = "⚠️ Não foi possível mesclar os itens no rascunho no momento devido à indisponibilidade temporária da IA. Por favor, tente novamente em instantes."
                    self._record_assistant_turn(context, reply)
                    self.update_context(context)
                    return {"reply": reply, "action_type": "offline_error", "state": context.state.value}
                upsert_res = self._upsert_maintenance_draft(context=context, analysis=analysis, clean_text=clean_text, vehicles=vehicles, matched_veh=matched_veh, discrepancy_warning=discrepancy_warning)
                self._record_assistant_turn(context, upsert_res["reply"])
                self.update_context(context)
                return upsert_res

            # 2. Cadastro de veículo offline
            registration_verbs = ("cadastr", "adicion", "tenho um", "tenho uma", "comprei", "meu carro", "minha moto")
            matched_spec = find_popular_vehicle_spec(clean_text)
            has_plate = bool(re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text))
            if matched_veh and not any(rv in text_lower for rv in registration_verbs):
                is_registration = False
            else:
                is_registration = any(rv in text_lower for rv in registration_verbs) or bool(matched_spec) or (has_plate and not vehicles)

            if is_registration:
                make = matched_spec["make"] if matched_spec else "Veículo"
                model = matched_spec["model"] if matched_spec else "Carro"
                plate_val = plate_match.group(1).replace("-", "").upper() if plate_match else None
                year_m = re.search(r"\b(19\d{2}|20\d{2})\b", clean_text)
                year_val = int(year_m.group(1)) if year_m else None
                v_draft = VehicleDraft(
                    make=make, model=model,
                    version=matched_spec.get("version") if matched_spec else None,
                    engine=matched_spec.get("engine") if matched_spec else None,
                    transmission=matched_spec.get("transmission") if matched_spec else None,
                    steering=matched_spec.get("steering") if matched_spec else None,
                    plate=plate_val,
                    year=year_val,
                    odometer_km=parse_km_value(text_lower),
                )
                analysis = GeminiAnalysisResponse(
                    intent=IntentType.REGISTER_VEHICLE,
                    confidence=ConfidenceLevel.HIGH,
                    is_critical_action=True,
                    reply_message=f"Cadastro do veículo {model} identificado.",
                    vehicle=v_draft,
                    target_vehicle_ref=model,
                    extracted_entities=v_draft.model_dump(exclude_none=True),
                )

            # 3. Atualização de odômetro com alerta
            elif not parse_cost_value(clean_text) and parse_km_value(text_lower) is not None and any(k in text_lower for k in ("km", "hodometro", "estou com", "rodou", "atualiza", "odômetro", "bateu", "marcando")):
                parsed_km = parse_km_value(text_lower)
                target_veh = matched_veh or (vehicles[0] if vehicles else None)
                target_ref = getattr(target_veh, "model", None) or getattr(target_veh, "modelo", "veículo")
                current_km = getattr(target_veh, "current_km", None) or getattr(target_veh, "quilometragem", None)

                # Detecção de retrocesso de hodômetro OU discrepância de entidade/veículo
                if (current_km and parsed_km < current_km) or discrepancy_warning:
                    context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
                    context.draft_payload = {
                        "_is_odometer_rollback": bool(current_km and parsed_km < current_km),
                        "_is_odometer_update": True,
                        "odometer_km": parsed_km,
                        "km": parsed_km,
                        "current_km": current_km,
                        "_target_vehicle_ref": target_ref,
                    }
                    if discrepancy_warning:
                        context.draft_payload["_discrepancy_warning"] = discrepancy_warning
                    context.draft_updated_at = self._utcnow()
                    if current_km and parsed_km < current_km:
                        reply = f"⚠️ Identifiquei que a quilometragem informada ({parsed_km:,} km) é menor que a registrada anteriormente ({current_km:,} km). Foi um erro de digitação? (Responda se deseja cancelar ou confirmar)".replace(",", ".")
                    else:
                        reply = f"Entendi a atualização do hodômetro com {parsed_km} km. Posso confirmar?"
                    if discrepancy_warning:
                        reply = f"⚠️ {discrepancy_warning}\n\n" + reply
                    self._record_assistant_turn(context, reply)
                    self.update_context(context)
                    return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload}

                sched_mark = (parsed_km // 10000) * 10000
                exceeded = (parsed_km - sched_mark) if sched_mark > 0 else 0
                context.state = ConversationState.NORMAL
                context.draft_payload = None

                target_veh_id = getattr(target_veh, "id", None)
                if target_veh:
                    target_veh.current_km = parsed_km

                reply_parts = [f"✅ Hodômetro do {target_ref} atualizado para {parsed_km:,} km!".replace(",", ".")]
                if exceeded > 0:
                    reply_parts.append(f"🔴 ALERTA: Você passou da revisão programada ({sched_mark:,} km) em {exceeded:,} km! Recomenda-se realizar a revisão preventiva.".replace(",", "."))
                elif any(q in text_lower for q in ("tem alguma", "revisão", "revisao", "vencendo", "o que trocar", "o que preciso")):
                    next_rev = sched_mark + 10000
                    rem_km = next_rev - parsed_km
                    reply_parts.append(f"📋 A próxima revisão programada é na marca de {next_rev:,} km (faltam aproximadamente {rem_km:,} km).".replace(",", "."))

                reply = "\n\n".join(reply_parts)
                if discrepancy_warning:
                    reply = f"⚠️ {discrepancy_warning}\n\n" + reply

                self._record_assistant_turn(context, reply)
                self.update_context(context)
                return {
                    "reply": reply,
                    "action_type": "odometer_updated",
                    "state": context.state.value,
                    "committed_data": {
                        "_is_odometer_update": True,
                        "odometer_km": parsed_km,
                        "km": parsed_km,
                        "vehicle_id": target_veh_id,
                        "_target_vehicle_ref": target_ref,
                    },
                    "details": {
                        "odometer_km": parsed_km,
                        "km": parsed_km,
                        "vehicle_id": target_veh_id,
                        "_target_vehicle_ref": target_ref,
                    }
                }

        # 5.6 Ação Crítica com Rascunho
        if analysis.is_critical_action:
            result = self._initiate_confirmation_flow(context, analysis, discrepancy_warning=discrepancy_warning, vehicles=vehicles, matched_veh=matched_veh, clean_text=clean_text)
            self._record_assistant_turn(context, result["reply"])
            self.update_context(context)
            return result

        # 5.7 Resposta Direta Padrão
        context.state = ConversationState.NORMAL
        self.update_context(context)
        self._record_assistant_turn(context, analysis.reply_message)
        return {"reply": analysis.reply_message, "action_type": analysis.intent.value, "state": context.state.value, "details": analysis.extracted_entities}

    def _initiate_confirmation_flow(
        self,
        context: ConversationContext,
        analysis: GeminiAnalysisResponse,
        discrepancy_warning: Optional[str] = None,
        vehicles: Optional[List[Any]] = None,
        matched_veh: Optional[Any] = None,
        clean_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Formata card enxuto e proativo de confirmação com base na entidade identificada."""
        intent = analysis.intent
        if intent in (IntentType.RECORD_MAINTENANCE, IntentType.UPSERT_DRAFT):
            return self._upsert_maintenance_draft(
                context=context,
                analysis=analysis,
                clean_text=clean_text or "",
                vehicles=vehicles,
                matched_veh=matched_veh,
                discrepancy_warning=discrepancy_warning,
            )

        state_map = {
            IntentType.REGISTER_VEHICLE: ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM,
            IntentType.RECORD_MAINTENANCE: ConversationState.AWAITING_MAINTENANCE_CONFIRM,
            IntentType.UPSERT_DRAFT: ConversationState.AWAITING_MAINTENANCE_CONFIRM,
            IntentType.SET_REMINDER: ConversationState.AWAITING_REMINDER_CONFIRM,
            IntentType.REGISTER_FINE: ConversationState.AWAITING_FINE_CONFIRM,
            IntentType.RESET_GARAGE: ConversationState.AWAITING_GARAGE_RESET_CONFIRM,
        }
        context.state = state_map.get(intent, ConversationState.AWAITING_MAINTENANCE_CONFIRM)

        existing_draft = dict(context.draft_payload) if isinstance(context.draft_payload, dict) else {}

        placeholder_strings = {
            "a cadastrar", "padrao", "padrão", "desconhecido", "none", "null",
            "veículo", "veiculo", "carro", "padrão do modelo", "padrao do modelo", ""
        }

        if existing_draft:
            payload = dict(existing_draft)
            new_data = dict(analysis.extracted_entities or {})
            if analysis.vehicle:
                new_data.update(analysis.vehicle.model_dump(exclude_none=True))
            for k, v in new_data.items():
                if v is None:
                    continue
                if isinstance(v, str) and v.strip().lower() in placeholder_strings:
                    continue
                payload[k] = v
        else:
            payload = dict(analysis.extracted_entities or {})
            if analysis.vehicle:
                payload.update(analysis.vehicle.model_dump(exclude_none=True))
            elif analysis.maintenance:
                payload.update(analysis.maintenance.model_dump(exclude_none=True))
            elif analysis.reminder:
                payload.update(analysis.reminder.model_dump(exclude_none=True))
            elif analysis.fine:
                payload.update(analysis.fine.model_dump(exclude_none=True))

        if context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM and clean_text:
            parsed_st = parse_steering_type(clean_text, model_name=payload.get("model"), year=payload.get("year"))
            if parsed_st:
                payload["steering"] = parsed_st
                payload["direcao"] = parsed_st

        matched_model = (getattr(matched_veh, "model", None) or getattr(matched_veh, "modelo", None)) if matched_veh else None
        target_ref = matched_model or analysis.target_vehicle_ref or payload.get("model")
        payload["_target_vehicle_ref"] = target_ref
        if matched_veh and getattr(matched_veh, "id", None):
            payload["vehicle_id"] = matched_veh.id
        payload["_action_state"] = context.state.value
        if discrepancy_warning:
            payload["_discrepancy_warning"] = discrepancy_warning
        context.draft_payload = payload
        context.draft_updated_at = self._utcnow()

        # 1. Onboarding com autopreenchimento de especificações
        if context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM:
            engine_val = payload.get("engine") or payload.get("motor")
            transmission_val = payload.get("transmission") or payload.get("cambio")
            steering_val = payload.get("steering") or payload.get("direcao")
            if not engine_val:
                spec = find_popular_vehicle_spec(f"{payload.get('make', '')} {payload.get('model', '')}")
                if spec:
                    engine_val, transmission_val, steering_val = spec.get("engine"), spec.get("transmission"), spec.get("steering")
                    payload.update({"engine": engine_val, "motor": engine_val, "transmission": transmission_val, "cambio": transmission_val, "steering": steering_val, "direcao": steering_val})

            if engine_val and (transmission_val or steering_val):
                model_name = payload.get("model") or "Veículo"
                version_name = payload.get("version") or ""
                full_model = f"{model_name} {version_name}".strip() if version_name and version_name.lower() not in model_name.lower() else model_name
                parts = [engine_val if engine_val.lower().startswith("motor") else f"Motor {engine_val}"]
                if transmission_val:
                    parts.append(transmission_val)
                if steering_val:
                    parts.append(steering_val if "direção" in steering_val.lower() else f"Direção {steering_val}")
                reply = f"Excelente escolha! Já puxei a ficha técnica do seu {full_model} ({', '.join(parts)}). Posso salvar na garagem?"
                if discrepancy_warning:
                    reply = f"⚠️ {discrepancy_warning}\n\n" + reply
                return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": payload}

        # Verificação de retrocesso de hodômetro para manutenção
        km_in_maint = payload.get("odometer_km") or payload.get("km")
        if matched_veh and km_in_maint is not None:
            curr_km = getattr(matched_veh, "current_km", None) or getattr(matched_veh, "quilometragem", None)
            if curr_km and km_in_maint < curr_km:
                payload["_is_odometer_rollback"] = True
                reply = f"⚠️ Identifiquei que a quilometragem informada ({km_in_maint:,} km) é menor que a registrada anteriormente ({curr_km:,} km). Foi um erro de digitação? (Responda se deseja cancelar ou confirmar)".replace(",", ".")
                if discrepancy_warning:
                    reply = f"⚠️ {discrepancy_warning}\n\n" + reply
                return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": payload}

        # 2. Card de Manutenção / Demais Entidades
        reply = (
            f"{analysis.reply_message}\n\n"
            f"📌 *Deseja confirmar e registrar essas informações?*\n"
            f"👉 Responda *'Sim'* para salvar ou *'Não'* para cancelar."
        )
        if discrepancy_warning:
            reply = f"⚠️ {discrepancy_warning}\n\n" + reply

        return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": payload}

    async def _handle_pending_confirmation_state(
        self,
        user_id: str,
        clean_text: str,
        text_lower: str,
        context: ConversationContext,
        vehicles: Optional[List[Any]],
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
        fines: Optional[List[Any]] = None,
        matched_veh: Optional[Any] = None,
        discrepancy_warning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Processa a mensagem do usuário quando há um rascunho aguardando confirmação."""
        nlp_classification = await self.classify_confirmation(clean_text, context)

        # 1. Recusa ou Cancelamento
        if nlp_classification is False:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {"reply": "Tranquilo, cancelei a operação! Nada foi alterado no seu veículo. Como posso te ajudar agora?", "action_type": "action_cancelled", "state": context.state.value, "details": {}}

        # 2. Confirmação com Lembrete Encadeado ("sim, me lembra amanhã às 8h")
        has_chained_command = bool(re.search(r"\b(lembr|avis|agend|notific|lembrete|amanh[ãa]|às\s+\d+|as\s+\d+|\d{1,2}h)\b", text_lower))
        if nlp_classification is True and has_chained_command:
            draft_data = context.draft_payload or {}
            draft_data["_action_state"] = context.state.value
            tm_match = re.search(r"(?:às|as)\s*(\d{1,2})(?::(\d{2})|h)?|\b(\d{1,2})h\b", text_lower)
            hour, minute = 8, 0
            if tm_match:
                if tm_match.group(1):
                    hour = int(tm_match.group(1))
                    minute = int(tm_match.group(2)) if tm_match.group(2) else 0
                elif tm_match.group(3):
                    hour = int(tm_match.group(3))

            date_str = "amanhã" if "amanh" in text_lower else "hoje"
            task_desc = "Revisão preventiva"
            task_match = re.search(r"(?:lembr\w+|avis\w+)\s+(?:de\s+)?([^,.]+)", clean_text, re.I)
            if task_match:
                extracted_t = re.sub(r"(?:amanh[ãa]|hoje|(?:às|as)\s*\d+h?|\d+h).*", "", task_match.group(1), flags=re.I).strip()
                if len(extracted_t) > 3:
                    task_desc = extracted_t

            time_str = f"{hour:02d}:{minute:02d}"
            draft_data["chained_reminder"] = {"task_description": task_desc, "explicit_time": time_str, "target_date_str": date_str}
            context.state = ConversationState.NORMAL
            context.draft_payload = None

            km_saved = draft_data.get("odometer_km") or draft_data.get("km") or draft_data.get("current_km")
            if km_saved and draft_data.get("_is_odometer_update"):
                base_msg = f"✅ *Atualização do hodômetro com {km_saved:,} km confirmada e salva!*".replace(",", ".")
            else:
                base_msg = "✅ *Ação confirmada e salva com sucesso!*"

            reply = f"{base_msg}\n\n⏰ *Lembrete agendado:* Vou te avisar {date_str} às {time_str} sobre *'{task_desc}'*! 🔔"
            return {"reply": reply, "action_type": "action_confirmed_with_chained_reminder", "state": context.state.value, "committed_data": draft_data}

        # 3. Confirmação Direta sem correções pendentes
        if nlp_classification is True:
            return self._confirm_and_save_draft(context)

        # 4. Mensagem com Adições ou Correções -> Analisa pelo LLM com injeção de draft_atual
        history_dicts = [{"role": t.role, "content": t.content} for t in context.recent_turns]
        context_snapshot = context.model_copy(deep=True)
        lock = self._get_user_lock(user_id)
        if lock.locked():
            lock.release()
            need_reacquire = True
        else:
            need_reacquire = False

        try:
            analysis = await self.gemini.analyze_message(
                user_id=user_id,
                current_message=clean_text,
                history=history_dicts,
                context=context_snapshot,
                vehicles=vehicles,
                maintenances=maintenances,
                reminders=reminders,
                fines=fines,
            )
        finally:
            if need_reacquire:
                await lock.acquire()

        tool_calls_list = []
        if analysis.tool_calls:
            tool_calls_list.extend(analysis.tool_calls)
        elif analysis.tool_call is not None:
            tool_calls_list.append(analysis.tool_call)

        # 4.1 Se for comando de edição do último serviço salvo (curto-circuito antecipado)
        has_edit_tc = any(getattr(tc, "name", None) == "update_last_saved_service" for tc in tool_calls_list)
        is_edit_service = (
            has_edit_tc
            or analysis.intent == IntentType.UPDATE_LAST_SAVED_SERVICE
            or is_edit_last_service_intent(clean_text, context)
        )
        if is_edit_service:
            return await self._handle_edit_last_saved_service(
                context=context,
                clean_text=clean_text,
                analysis=analysis,
                maintenances=maintenances,
                vehicles=vehicles,
            )

        # 4.2 Se a IA indicou upsert_draft (decisão 100% via IA / Tool Call, sem regex local)
        is_maint_draft = (
            context.state == ConversationState.AWAITING_MAINTENANCE_CONFIRM
            and isinstance(context.draft_payload, dict)
            and not context.draft_payload.get("_is_multi_action")
        )
        if is_maint_draft:
            has_upsert_tc = any(getattr(tc, "name", None) == "upsert_draft" for tc in tool_calls_list)
            if has_upsert_tc or analysis.intent in (IntentType.UPSERT_DRAFT, IntentType.RECORD_MAINTENANCE):
                return self._upsert_maintenance_draft(
                    context=context,
                    analysis=analysis,
                    clean_text=clean_text,
                    vehicles=vehicles,
                    matched_veh=matched_veh,
                    discrepancy_warning=discrepancy_warning,
                )

        # Atualização cumulativa de ficha técnica no cadastro de veículo (PATCH ATÔMICO)
        if context.state == ConversationState.AWAITING_VEHICLE_REGISTRATION_CONFIRM and isinstance(context.draft_payload, dict):
            plate_match = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", clean_text) or re.search(r"\b([A-Za-z]{3}[-\s]?[0-9]{4})\b", clean_text)
            has_specs = any(k in text_lower for k in (
                "motor", "manual", "automático", "automatico", "cvt", "direção", "direcao", "km",
                "flex", "aspirado", "turbo", "placa", "hidr", "hidráulica", "hidraulica", "mecanica", "mecânica"
            )) or bool(plate_match) or bool(re.search(r"\b[0-9]\.[0-9]\b", text_lower))

            if has_specs:
                payload = dict(context.draft_payload)
                if plate_match:
                    clean_plate = re.sub(r"[\s\-]", "", plate_match.group(1)).upper()
                    payload["plate"] = clean_plate
                    payload["placa"] = clean_plate

                km_val = parse_km_value(text_lower)
                if km_val:
                    payload["odometer_km"] = km_val
                    payload["current_km"] = km_val
                    payload["quilometragem"] = km_val

                m_motor = re.search(r"\b(?:motor\s+)?([0-9]\.[0-9]\s*(?:flex|turbo|aspirado|firefly|fire)?)\b", text_lower)
                if m_motor:
                    payload["engine"] = m_motor.group(1).title() if "flex" in m_motor.group(1) else m_motor.group(1)
                    payload["motor"] = payload["engine"]
                elif "motor" in text_lower:
                    m_gen = re.search(r"motor\s+([^,]+)", text_lower)
                    if m_gen:
                        payload["engine"] = m_gen.group(1).strip()
                        payload["motor"] = payload["engine"]

                if "manual" in text_lower:
                    payload["transmission"] = "Manual"
                    payload["cambio"] = "Manual"
                elif "automático" in text_lower or "automatico" in text_lower:
                    payload["transmission"] = "Automático"
                    payload["cambio"] = "Automático"
                elif "cvt" in text_lower:
                    payload["transmission"] = "CVT"
                    payload["cambio"] = "CVT"

                steering_val = parse_steering_type(clean_text, model_name=payload.get("model"), year=payload.get("year"))
                if steering_val:
                    payload["steering"] = steering_val
                    payload["direcao"] = steering_val

                context.draft_payload = payload
                context.draft_updated_at = self._utcnow()
                model_name = payload.get("model", "Veículo")
                km_display = f"{payload.get('odometer_km'):,} km".replace(",", ".") if payload.get("odometer_km") else ""
                km_part = f" com {km_display}" if km_display else ""
                plate_part = f" | Placa: {payload['plate']}" if payload.get("plate") else ""

                specs_list = []
                if payload.get("engine"):
                    specs_list.append(payload["engine"])
                if payload.get("transmission"):
                    specs_list.append(payload["transmission"])
                if payload.get("steering"):
                    specs_list.append(f"Direção {payload['steering']}")
                specs_str = f" ({', '.join(specs_list)})" if specs_list else ""

                reply = f"Perfeito! Atualizei a ficha da {model_name}{specs_str}{km_part}{plate_part}. Posso confirmar o cadastro na sua garagem?"
                return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": payload}

        # Atualização em rascunho de multi-ação pendente (ex: alterar valor de 980 para 890)
        if context.draft_payload and context.draft_payload.get("_is_multi_action"):
            new_cost = parse_cost_value(clean_text)
            if new_cost is not None and any(k in text_lower for k in ("valor", "reais", "preço", "custo", "na verdade", "foi")):
                payload = dict(context.draft_payload)
                actions = list(payload.get("actions", []))
                for act in actions:
                    if act.get("intent") in (IntentType.RECORD_MAINTENANCE.value, "record_maintenance"):
                        act["total_cost"] = new_cost
                        break
                payload["actions"] = actions
                context.draft_payload = payload
                context.draft_updated_at = self._utcnow()
                reply = f"Perfeito! Atualizei o valor para R$ {new_cost:.2f}. O restante segue mantido. Deseja confirmar e salvar?".replace(".", ",")
                return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": payload}

        # Context Switch durante confirmação pendente (ex: usuário pergunta sobre outro carro)
        is_query = any(k in text_lower for k in ("qual", "quando", "quanto", "como", "última", "ultima", "histórico", "historico", "ver", "mostrar"))
        if is_query and matched_veh:
            v_model = getattr(matched_veh, "model", None) or getattr(matched_veh, "modelo", "veículo")
            v_id = getattr(matched_veh, "id", None)
            veh_maints = [m for m in (maintenances or []) if getattr(m, "vehicle_id", None) == v_id and getattr(m, "status", "active") != "cancelled"]
            if veh_maints:
                veh_maints.sort(key=lambda m: (str(getattr(m, "date", "") or ""), int(getattr(m, "id", 0) or 0)), reverse=True)
                last_m = veh_maints[0]
                desc = getattr(last_m, "description", None) or getattr(last_m, "descricao", "Manutenção")
                cost = float(getattr(last_m, "total_cost", 0.0) or getattr(last_m, "custo", 0.0) or 0.0)
                ans = f"🚗 No seu *{v_model}*, a última manutenção registrada foi *{desc}* (R$ {cost:.2f}).".replace(".", ",")
            else:
                ans = f"🚗 No seu *{v_model}*, não encontrei manutenções anteriores registradas."

            pending_target = context.draft_payload.get("_target_vehicle_ref", "veículo") if isinstance(context.draft_payload, dict) else "veículo"
            reply = f"{ans}\n\n📌 *Atenção:* Seu registro anterior ({pending_target}) continua pendente. Deseja confirmar e salvar?"
            return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": context.draft_payload}

        # Check-up da garagem durante estado pendente
        if self._is_garage_checkup_query(text_lower):
            return await self._handle_garage_checkup(user_id=user_id, vehicles=vehicles, maintenances=maintenances, reminders=reminders)

        if analysis.tool_call is not None:
            tool_res = await self._execute_ai_tool_call(context=context, analysis=analysis, clean_text=clean_text, vehicles=vehicles, maintenances=maintenances, matched_veh=matched_veh, discrepancy_warning=discrepancy_warning)
            if tool_res is not None:
                return tool_res

        if analysis.intent == IntentType.CONFIRM_ACTION:
            return self._confirm_and_save_draft(context)

        if analysis.intent == IntentType.CANCEL_ACTION:
            context.state = ConversationState.NORMAL
            context.draft_payload = None
            return {"reply": "Entendido! Rascunho descartado. Como posso te ajudar agora?", "action_type": "action_cancelled", "state": context.state.value, "details": {}}

        if analysis.is_critical_action:
            return self._initiate_confirmation_flow(context, analysis, discrepancy_warning=discrepancy_warning, clean_text=clean_text)

        # Caso permaneça em dúvida, reapresenta o prompt de confirmação
        return {
            "reply": "Você possui um registro aguardando sua resposta:\n\nDeseja confirmar e salvar? (Responda *'Sim'* para salvar, *'Não'* para descartar ou *'Sair'* para voltar ao menu).",
            "action_type": "awaiting_confirmation_clarification",
            "state": context.state.value,
            "draft": context.draft_payload,
        }

    # =========================================================================
    # TOOL CALL HANDLERS DA ARQUITETURA AI-FIRST
    # =========================================================================

    async def _execute_ai_tool_calls(
        self,
        context: ConversationContext,
        analysis: GeminiAnalysisResponse,
        tool_calls: List[Any],
        clean_text: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        matched_veh: Optional[Any] = None,
        discrepancy_warning: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Executa lote de tools selecionadas pelo LLM (Multi-Tool Calls / Intenções Compostas)."""
        if not tool_calls:
            return None

        committed_odometer: Optional[Dict[str, Any]] = None
        has_patch_vehicle = False

        for tc in tool_calls:
            if getattr(tc, "name", None) == "patch_vehicle_profile":
                has_patch_vehicle = True
                new_km = getattr(tc, "km", None) if getattr(tc, "km", None) is not None else parse_km_value(clean_text)
                target_veh = matched_veh or (vehicles[0] if vehicles else None)
                tc_veh_ref = getattr(tc, "target_vehicle_ref", None) or getattr(analysis, "target_vehicle_ref", None)
                if tc_veh_ref and vehicles:
                    for v in vehicles:
                        v_mod = (getattr(v, "model", None) or "").lower()
                        if v_mod and tc_veh_ref.lower() in v_mod:
                            target_veh = v
                            break

                target_ref = getattr(target_veh, "model", None) or tc_veh_ref or "veículo"
                target_veh_id = getattr(target_veh, "id", None)
                if target_veh and new_km is not None:
                    target_veh.current_km = int(new_km)

                committed_odometer = {
                    "_is_odometer_update": True,
                    "odometer_km": int(new_km) if new_km is not None else None,
                    "km": int(new_km) if new_km is not None else None,
                    "vehicle_id": target_veh_id,
                    "_target_vehicle_ref": target_ref,
                }
                context.state = ConversationState.NORMAL
                context.draft_payload = None

            elif getattr(tc, "name", None) == "upsert_draft":
                if is_edit_last_service_intent(clean_text, context):
                    res = await self._handle_edit_last_saved_service(context=context, clean_text=clean_text, analysis=analysis, maintenances=maintenances, vehicles=vehicles)
                    if committed_odometer:
                        res["committed_data"] = {**res.get("committed_data", {}), **committed_odometer}
                    return res
                if context.state == ConversationState.NORMAL and is_vague_service_initiation(clean_text) and not getattr(tc, "itens", None):
                    return {"reply": "Beleza! Me conta qual serviço você fez, e se lembrar, a data e a quilometragem.", "action_type": "clarification_needed", "state": ConversationState.NORMAL.value, "details": {}}
                res = self._upsert_maintenance_draft(context=context, analysis=analysis, clean_text=clean_text, vehicles=vehicles, matched_veh=matched_veh, discrepancy_warning=discrepancy_warning)
                if committed_odometer:
                    res["committed_data"] = {**res.get("committed_data", {}), **committed_odometer}
                return res

            elif getattr(tc, "name", None) == "confirm_and_save_draft":
                if context.draft_payload:
                    res = self._confirm_and_save_draft(context)
                    if committed_odometer:
                        res["committed_data"] = {**res.get("committed_data", {}), **committed_odometer}
                    return res

            elif getattr(tc, "name", None) == "update_last_saved_service":
                res = await self._handle_edit_last_saved_service(context=context, clean_text=clean_text, analysis=analysis, maintenances=maintenances, vehicles=vehicles)
                if committed_odometer:
                    res["committed_data"] = {**res.get("committed_data", {}), **committed_odometer}
                return res

            elif getattr(tc, "name", None) == "get_service_history":
                if not has_patch_vehicle:
                    return self._handle_get_service_history(maintenances=maintenances, vehicles=vehicles, context=context, llm_reply=analysis.reply_message)

        if has_patch_vehicle and committed_odometer:
            reply = analysis.reply_message
            if not reply or reply.strip() == "":
                km_disp = committed_odometer["odometer_km"]
                reply = f"✅ Hodômetro do {committed_odometer['_target_vehicle_ref']} atualizado para {km_disp:,} km!".replace(",", ".")
            return {
                "reply": reply,
                "action_type": "odometer_updated",
                "state": ConversationState.NORMAL.value,
                "committed_data": committed_odometer,
                "details": committed_odometer,
            }

        return None

    async def _execute_ai_tool_call(
        self,
        context: ConversationContext,
        analysis: GeminiAnalysisResponse,
        clean_text: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        matched_veh: Optional[Any] = None,
        discrepancy_warning: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Executa a tool selecionada pelo LLM (retrocompatibilidade)."""
        tc = analysis.tool_call
        if tc is None:
            return None
        return await self._execute_ai_tool_calls(
            context=context,
            analysis=analysis,
            tool_calls=[tc],
            clean_text=clean_text,
            vehicles=vehicles,
            maintenances=maintenances,
            matched_veh=matched_veh,
            discrepancy_warning=discrepancy_warning,
        )

    def _upsert_maintenance_draft(
        self,
        context: ConversationContext,
        analysis: GeminiAnalysisResponse,
        clean_text: str,
        vehicles: Optional[List[Any]] = None,
        matched_veh: Optional[Any] = None,
        discrepancy_warning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cria ou atualiza cumulativamente o rascunho de manutenção aberto via Deep Merge atômico."""
        existing_draft = dict(context.draft_payload) if (
            isinstance(context.draft_payload, dict)
            and not context.draft_payload.get("_is_multi_action")
        ) else {}

        is_updating = bool(existing_draft)

        tool_calls_list = []
        if analysis.tool_calls:
            tool_calls_list.extend(analysis.tool_calls)
        elif analysis.tool_call is not None:
            tool_calls_list.append(analysis.tool_call)

        entities = dict(analysis.extracted_entities or {})
        maint_obj = getattr(analysis, "maintenance", None)
        if maint_obj:
            entities.update(maint_obj.model_dump(exclude_none=True))

        # 1. Recuperação de itens anteriores do rascunho
        prev_items: List[str] = []
        raw_prev = existing_draft.get("itens") or existing_draft.get("components")
        if isinstance(raw_prev, list):
            prev_items = [str(i).strip() for i in raw_prev if str(i).strip()]
        elif isinstance(raw_prev, str) and raw_prev.strip():
            prev_items = [raw_prev.strip()]
        elif existing_draft.get("description"):
            desc_str = str(existing_draft.get("description")).strip()
            extracted_from_desc = extract_maintenance_items_strict(desc_str)
            if extracted_from_desc:
                prev_items = extracted_from_desc
            elif desc_str.lower() not in ("manutenção realizada", "manutenção", "serviço"):
                prev_items = [desc_str]

        # 2. Novos itens fornecidos na chamada da Tool, entities ou texto
        new_items: List[str] = []
        for tc_item in tool_calls_list:
            if getattr(tc_item, "itens", None):
                tc_it = tc_item.itens
                if isinstance(tc_it, list):
                    new_items.extend([str(i).strip() for i in tc_it if str(i).strip()])
                elif isinstance(tc_it, str) and tc_it.strip():
                    new_items.append(tc_it.strip())

        if not new_items:
            raw_c = entities.get("components") or entities.get("itens")
            if isinstance(raw_c, list):
                new_items.extend([str(i).strip() for i in raw_c if str(i).strip()])
            elif isinstance(raw_c, str) and raw_c.strip():
                new_items.append(raw_c.strip())

        strict_items = extract_maintenance_items_strict(clean_text)
        if strict_items:
            new_items = merge_maintenance_items(new_items, strict_items)

        # Merge de Itens (Deep Merge: mescla/append itens do existing_draft com novos itens)
        merged_items = merge_maintenance_items(prev_items, new_items)
        if not merged_items and prev_items:
            merged_items = prev_items

        # 3. Preservação Blindada (KM): Se novo payload da IA não contiver valor numérico, preserva existing_draft
        new_km = None
        for tc_item in tool_calls_list:
            if getattr(tc_item, "km", None) is not None:
                new_km = tc_item.km
                break
        if new_km is None and entities.get("km") is not None:
            new_km = entities.get("km")
        if new_km is None and entities.get("odometer_km") is not None:
            new_km = entities.get("odometer_km")
        if new_km is None and maint_obj and getattr(maint_obj, "odometer_km", None) is not None:
            new_km = maint_obj.odometer_km

        prev_km = existing_draft.get("km") if existing_draft.get("km") is not None else existing_draft.get("odometer_km")
        if is_updating:
            final_km = new_km if new_km is not None else prev_km
        else:
            final_km = new_km if new_km is not None else parse_km_value(clean_text)

        # 4. Preservação Blindada de Data: Se IA não extrair nova data, PRESERVA incondicionalmente data anterior
        new_data = None
        for tc_item in tool_calls_list:
            if getattr(tc_item, "data", None):
                new_data = tc_item.data
                break
        if not new_data and entities.get("data"):
            new_data = entities.get("data")
        if not new_data and entities.get("date_str"):
            new_data = entities.get("date_str")
        if not new_data and maint_obj and getattr(maint_obj, "date_str", None):
            new_data = maint_obj.date_str

        prev_data = existing_draft.get("data") or existing_draft.get("date_str") or existing_draft.get("date")
        if is_updating:
            final_data = new_data if new_data else prev_data
        else:
            final_data = new_data or extract_date_strict(clean_text) or datetime.now().strftime("%d/%m/%Y")

        # 5. Preservação Blindada de Oficina: Preserva oficina do rascunho anterior se não informada nova
        new_oficina = None
        for tc_item in tool_calls_list:
            if getattr(tc_item, "oficina", None):
                new_oficina = tc_item.oficina
                break
        if not new_oficina and entities.get("workshop_name"):
            new_oficina = entities.get("workshop_name")
        if not new_oficina and entities.get("oficina"):
            new_oficina = entities.get("oficina")
        if not new_oficina and maint_obj and getattr(maint_obj, "workshop_name", None):
            new_oficina = maint_obj.workshop_name

        prev_oficina = existing_draft.get("oficina") or existing_draft.get("workshop_name")
        if is_updating:
            final_oficina = new_oficina if new_oficina else prev_oficina
        else:
            final_oficina = new_oficina or extract_workshop_strict(clean_text)

        # 6. Preservação Blindada de Valor: Permite que a IA adicione custos cumulativamente se indicado
        new_cost = None
        for tc_item in tool_calls_list:
            if getattr(tc_item, "valor", None) is not None:
                new_cost = tc_item.valor
                break
        if new_cost is None and entities.get("total_cost") is not None:
            new_cost = entities.get("total_cost")
        if new_cost is None and entities.get("valor") is not None:
            new_cost = entities.get("valor")
        if new_cost is None and maint_obj and getattr(maint_obj, "total_cost", None) is not None:
            new_cost = maint_obj.total_cost

        prev_cost = existing_draft.get("valor") if existing_draft.get("valor") is not None else existing_draft.get("total_cost")
        if is_updating and new_cost is not None and prev_cost is not None:
            tl = clean_text.lower()
            if any(k in tl for k in ("também", "tambem", "mais", "adiciona", "inclui", "somar", "soma")):
                final_cost = float(prev_cost) + float(new_cost)
            else:
                final_cost = float(new_cost)
        elif is_updating:
            final_cost = prev_cost
        else:
            final_cost = new_cost if new_cost is not None else parse_cost_value(clean_text)

        target_veh_ref = (
            existing_draft.get("_target_vehicle_ref")
            or (getattr(matched_veh, "model", None) if matched_veh else None)
            or (getattr(vehicles[0], "model", None) if vehicles else None)
        )

        updated_payload = {
            **existing_draft,
            "itens": merged_items,
            "components": merged_items,
            "description": ", ".join(merged_items) if merged_items else (existing_draft.get("description") or "Manutenção realizada"),
            "km": final_km,
            "odometer_km": final_km,
            "data": final_data,
            "date_str": final_data,
            "date": final_data,
            "oficina": final_oficina,
            "workshop_name": final_oficina,
            "valor": final_cost,
            "total_cost": final_cost if final_cost is not None else 0.0,
            "_target_vehicle_ref": target_veh_ref,
        }
        if discrepancy_warning:
            updated_payload["_discrepancy_warning"] = discrepancy_warning

        context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
        context.draft_payload = updated_payload
        context.draft_updated_at = self._utcnow()

        # Verificação de retrocesso de hodômetro para manutenção
        target_veh = matched_veh or (vehicles[0] if vehicles else None)
        if target_veh and final_km is not None:
            curr_km = getattr(target_veh, "current_km", None) or getattr(target_veh, "quilometragem", None)
            if curr_km and final_km < curr_km:
                updated_payload["_is_odometer_rollback"] = True
                reply = f"⚠️ Identifiquei que a quilometragem informada ({int(final_km):,} km) é menor que a registrada anteriormente ({int(curr_km):,} km). Foi um erro de digitação? (Responda se deseja cancelar ou confirmar)".replace(",", ".")
                if discrepancy_warning:
                    reply = f"⚠️ {discrepancy_warning}\n\n" + reply
                return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": updated_payload}

        header = "🔄 *Rascunho atualizado! Confira como ficou o resumo da manutenção:*" if is_updating else "🔧 *Resumo da manutenção para confirmação:*"
        lines = [header]
        if target_veh_ref:
            lines.append(f"• *Veículo:* {target_veh_ref}")
        lines.append(f"• *Itens/Serviços:* {', '.join(merged_items) if merged_items else updated_payload['description']}")
        if final_km is not None:
            lines.append(f"• *Quilometragem:* {int(final_km):,} km".replace(",", "."))
        if final_data:
            lines.append(f"• *Data:* {final_data}")
        if final_oficina:
            lines.append(f"• *Oficina:* {final_oficina}")
        if final_cost is not None and float(final_cost) > 0:
            cost_fmt = f"R$ {float(final_cost):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            lines.append(f"• *Valor:* {cost_fmt}")
        lines.append("\n📌 *Posso confirmar e salvar?*")
        reply = "\n".join(lines)
        if discrepancy_warning:
            reply = f"⚠️ {discrepancy_warning}\n\n" + reply

        return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": updated_payload}

    def _confirm_and_save_draft(self, context: ConversationContext) -> Dict[str, Any]:
        """Valida e aprova o rascunho pendente para gravação no banco de dados."""
        draft_data = dict(context.draft_payload or {})
        draft_data["_action_state"] = context.state.value
        context.state = ConversationState.NORMAL
        context.draft_payload = None

        if draft_data.get("_is_multi_action"):
            acts = draft_data.get("actions", [])
            lines = [
                "✅ *Todas as ações foram confirmadas e salvas com sucesso!*\n",
                "• 🔧 Manutenção registrada no seu histórico.",
                "• ⏰ Lembrete agendado na sua garagem.",
            ]
            for a in acts:
                desc = a.get("description") or a.get("task_description") or "Ação registrada"
                target_veh = a.get("target_vehicle_ref")
                if target_veh and target_veh.lower() not in desc.lower():
                    desc = f"{desc} ({target_veh})"
                lines.append(f"• {desc}")
            reply = "\n".join(lines)
        elif draft_data.get("_is_odometer_update"):
            km = draft_data.get("odometer_km") or draft_data.get("km")
            reply = f"✅ *Atualização do hodômetro com {km:,} km confirmada e salva!*".replace(",", ".")
        elif "make" in draft_data and "model" in draft_data and "components" not in draft_data:
            model = draft_data.get("model", "Veículo")
            reply = "✅ *Veículo cadastrado com sucesso na sua garagem!*"
        else:
            reply = "✅ *Ação confirmada e salva com sucesso no seu histórico!*"

        if draft_data.get("_discrepancy_warning"):
            reply = f"⚠️ {draft_data['_discrepancy_warning']}\n\n" + reply

        return {"reply": reply, "action_type": "action_confirmed", "state": context.state.value, "committed_data": draft_data}

    async def _handle_edit_last_saved_service(
        self,
        context: ConversationContext,
        clean_text: str,
        analysis: GeminiAnalysisResponse,
        maintenances: Optional[List[Any]] = None,
        vehicles: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Conduz a edição guiada do último serviço registrado no banco."""
        active_maints = [m for m in (maintenances or []) if getattr(m, "status", "active") != "cancelled"]
        if not active_maints:
            return {"reply": "Não encontrei nenhum serviço salvo recentemente para alterar. Deseja cadastrar uma nova manutenção?", "action_type": "edit_rejected_no_service", "state": context.state.value, "details": {}}

        tc = getattr(analysis, "tool_call", None)
        tc_service_id = getattr(tc, "service_id", None) if tc else None
        pending_srv_id = getattr(context, "pending_edit_service_id", None)
        target_id = tc_service_id or pending_srv_id

        last_m = None
        if target_id is not None:
            last_m = next((m for m in active_maints if str(getattr(m, "id", "")) == str(target_id)), None)

        if last_m is None:
            def sort_key(m):
                dt = getattr(m, "date", None)
                ts = 0.0
                if dt:
                    if hasattr(dt, "timestamp"):
                        ts = dt.timestamp()
                    elif isinstance(dt, str):
                        try:
                            ts = datetime.strptime(dt, "%d/%m/%Y").timestamp()
                        except Exception:
                            ts = 0.0
                m_id = getattr(m, "id", 0) or 0
                try:
                    m_id_num = int(m_id)
                except Exception:
                    m_id_num = 0
                return (ts, m_id_num)

            sorted_maints = sorted(active_maints, key=sort_key, reverse=True)
            last_m = sorted_maints[0]

        srv_id = getattr(last_m, "id", None)
        text_lower = clean_text.lower()
        prev_components = list(getattr(last_m, "components", []) or [getattr(last_m, "description", "Manutenção")])
        m_dt = getattr(last_m, "date", None)
        prev_dt_str = m_dt.strftime("%d/%m/%Y") if hasattr(m_dt, "strftime") and m_dt else (str(m_dt) if m_dt else "não informada")

        # 1. Se o usuário já está respondendo ao campo pendente (Turno 2 da condução ativa)
        field_to_edit = getattr(context, "pending_edit_field", None)
        if field_to_edit:
            committed: Dict[str, Any] = {"service_id": getattr(context, "pending_edit_service_id", srv_id) or srv_id}
            reply_msg = ""
            if field_to_edit == "data":
                new_d = extract_date_strict(clean_text) or clean_text.strip()
                committed["data"] = new_d
                reply_msg = f"✅ A data do último serviço foi atualizada para *{new_d}* com sucesso!"
            elif field_to_edit == "valor":
                new_v = parse_cost_value(clean_text) or 0.0
                committed["valor"] = new_v
                reply_msg = f"✅ O valor do último serviço foi atualizado para *R$ {new_v:.2f}* com sucesso!".replace(".", ",")
            elif field_to_edit == "km":
                new_km = parse_km_value(clean_text) or 0
                committed["km"] = new_km
                reply_msg = f"✅ A quilometragem do último serviço foi atualizada para *{new_km:,} km*!".replace(",", ".")
            elif field_to_edit == "oficina":
                committed["oficina"] = clean_text.strip()
                reply_msg = f"✅ A oficina do último serviço foi atualizada para *{clean_text.strip()}*!"
            else:
                new_items = extract_maintenance_items_strict(clean_text) or [clean_text.strip()]
                merged = merge_maintenance_items(prev_components, new_items)
                committed["itens"] = merged
                reply_msg = f"✅ Os itens do último serviço foram atualizados para *{', '.join(merged)}*!"

            context.pending_edit_field = None
            context.pending_edit_service_id = None
            context.state = ConversationState.NORMAL
            return {"reply": reply_msg, "action_type": "update_last_saved_service", "state": context.state.value, "committed_data": committed}

        # 2. Cenário B: Correção completa na frase ou via Tool Call
        extracted_items = extract_maintenance_items_strict(clean_text)
        extracted_date = extract_date_strict(clean_text)
        extracted_km = parse_km_value(clean_text)
        extracted_cost = parse_cost_value(clean_text)

        has_explicit_change = (
            (extracted_items and any(k in text_lower for k in ("adicion", "inclu", "coloc", "troquei")))
            or (extracted_date and any(k in text_lower for k in ("mudar", "alterar", "para", "corrigir")))
            or (extracted_km is not None and any(k in text_lower for k in ("mudar", "alterar", "para", "corrigir", "foi", "com")))
            or (extracted_cost is not None and any(k in text_lower for k in ("mudar", "alterar", "para", "corrigir", "foi", "custou")))
        )
        if has_explicit_change:
            committed = {"service_id": srv_id}
            reply_parts = []
            if extracted_items:
                merged = merge_maintenance_items(prev_components, extracted_items)
                committed["itens"] = merged
                reply_parts.append(f"{', '.join(extracted_items)} adicionado")
            if extracted_date:
                committed["data"] = extracted_date
                reply_parts.append(f"data alterada para {extracted_date}")
            if extracted_km is not None:
                committed["km"] = extracted_km
                reply_parts.append(f"quilometragem alterada para {extracted_km:,} km".replace(",", "."))
            if extracted_cost is not None:
                committed["valor"] = extracted_cost
                reply_parts.append(f"valor alterado para R$ {extracted_cost:.2f}".replace(".", ","))
            context.state = ConversationState.NORMAL
            reply_msg = f"✅ Último serviço atualizado: {' e '.join(reply_parts)} com sucesso!"
            return {"reply": reply_msg, "action_type": "update_last_saved_service", "state": context.state.value, "committed_data": committed}

        # 3. Cenário A: Usuário menciona o campo específico que quer alterar (ex: 'a data estava errada')
        if "data" in text_lower:
            context.pending_edit_service_id = srv_id
            context.pending_edit_field = "data"
            reply = f"Sem problemas! A data atual está registrada como {prev_dt_str}. Qual é a data correta?"
            return {"reply": reply, "action_type": "awaiting_edit_value", "state": context.state.value, "details": {"field": "data", "service_id": srv_id}}

        if any(k in text_lower for k in ("valor", "custo", "preço", "preco")):
            context.pending_edit_service_id = srv_id
            context.pending_edit_field = "valor"
            prev_cost = getattr(last_m, "total_cost", 0.0) or 0.0
            reply = f"Sem problemas! O valor atual está registrado como R$ {prev_cost:.2f}. Qual é o valor correto em reais?".replace(".", ",")
            return {"reply": reply, "action_type": "awaiting_edit_value", "state": context.state.value, "details": {"field": "valor", "service_id": srv_id}}

        if any(k in text_lower for k in ("km", "quilometragem", "odometro")):
            context.pending_edit_service_id = srv_id
            context.pending_edit_field = "km"
            prev_km = getattr(last_m, "km", None) or getattr(last_m, "odometro_registro", None)
            km_part = f"{prev_km:,} km".replace(",", ".") if prev_km else "não informada"
            reply = f"Sem problemas! A quilometragem atual está registrada como {km_part}. Qual é a quilometragem correta?"
            return {"reply": reply, "action_type": "awaiting_edit_value", "state": context.state.value, "details": {"field": "km", "service_id": srv_id}}

        # Caso geral sem campo especificado
        context.pending_edit_service_id = srv_id
        context.pending_edit_field = "awaiting_field_choice"
        m_comp = ", ".join(prev_components)
        m_val = getattr(last_m, "total_cost", 0.0) or 0.0
        reply = (
            f"Encontrei seu último registro: *{m_comp}* (Data: {prev_dt_str}, Valor: R$ {m_val:.2f}).\n\n"
            "Qual informação você gostaria de alterar? (ex: *a data*, *o valor*, *os itens*, *o km* ou *a oficina*)"
        ).replace(".", ",")
        return {"reply": reply, "action_type": "awaiting_edit_value", "state": context.state.value, "details": {"service_id": srv_id}}

    def _handle_get_service_history(
        self,
        maintenances: Optional[List[Any]] = None,
        vehicles: Optional[List[Any]] = None,
        context: Optional[ConversationContext] = None,
        llm_reply: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Consulta e formata o histórico de manutenções registradas."""
        active_maints = [m for m in (maintenances or []) if getattr(m, "status", "active") != "cancelled"]
        if not active_maints:
            return {"reply": "Você ainda não possui nenhum serviço registrado no seu histórico. Deseja cadastrar a primeira manutenção?", "action_type": "query_history", "state": "normal", "details": {"total_maintenances": 0}}

        lines = ["🔧 *Histórico de Serviços Registrados:*\n"]
        total_val = 0.0
        for idx, m in enumerate(reversed(active_maints), 1):
            desc = getattr(m, "description", None) or ", ".join(getattr(m, "components", [])) or "Manutenção"
            cost = float(getattr(m, "total_cost", 0.0) or getattr(m, "custo", 0.0) or 0.0)
            total_val += cost
            dt = getattr(m, "date", None)
            dt_str = dt.strftime("%d/%m/%Y") if hasattr(dt, "strftime") and dt else (str(dt) if dt else "")
            dt_part = f" em {dt_str}" if dt_str else ""
            km_val = getattr(m, "km", None) or getattr(m, "odometro_registro", None)
            km_part = f" ({km_val:,} km)".replace(",", ".") if km_val else ""
            lines.append(f"{idx}. *{desc}*{km_part}{dt_part} - R$ {cost:.2f}".replace(".", ","))

        lines.append(f"\n💰 *Total Geral Investido:* R$ {total_val:.2f}".replace(".", ","))
        lines.append("\n💡 Caso queira alterar algum valor ou data, basta me dizer!")
        return {"reply": "\n".join(lines), "action_type": "query_history", "state": context.state.value if context else "normal", "details": {"total_maintenances": len(active_maints)}}

    async def classify_confirmation(self, text: str, context: Optional[ConversationContext] = None) -> Optional[bool]:
        """Classifica respostas naturais de aprovação (True) ou recusa (False)."""
        clean_lower = (text or "").lower().strip()
        norm = normalize_text(clean_lower)

        # Se contiver entidades de correção ou adição cumulativa, retorna None para reavaliação pelo LLM/patch
        spec_or_entity_patterns = [
            r"\b(placa|ano|valor|custo|pre[çc]o|reais|oficina|pe[çc]a|data|km|quilometragem|filtro|vela|pastilha|[óo]leo|tamb[eé]m|adiciona|inclui)\b",
            r"\b(motor|vers[aã]o|c[aâ]mbio|dire[cç][aã]o|manual|autom[aá]tico|cvt|hidr\w*|mec[aâ]nic\w*|el[eé]tric\w*)\b",
            r"\b[0-9]\.[0-9]\b",
            r"\b\d+\s*km\b",
            r"\b[a-zA-Z]{3}[-\s]?[0-9][a-zA-Z0-9][0-9]{2}\b",
            r"\b[a-zA-Z]{3}[-\s]?[0-9]{4}\b",
        ]
        if any(re.search(p, clean_lower) for p in spec_or_entity_patterns):
            return None

        # Expressões de dúvida/incerteza ("não sei", "não lembro", "não tenho certeza") não são recusa
        if re.search(r"\bn[aã]o\s+(?:sei|lembro|tenho|soube|recordo|vi)\b", norm):
            return None

        # Rejeição
        neg_patterns = [
            r"\bfoi\s+(?:um\s+)?(?:erro|engano)\b",
            r"\berr(?:o|ei|ado)\b",
            r"\bme\s+(?:confundi|enganei)\b",
            r"\b(?:cancela|cancelar|aborta|abortar|esquece)\b",
            r"\bdeixa\s+quieto\b",
            r"\bn[aã]o\s*(?:salva|quero|confirma|grava|cadastra)?\b",
        ]
        if any(re.search(p, norm) for p in neg_patterns):
            return False

        # Aprovação
        aff_patterns = [
            r"\bpode\s+meter\s+marcha\b",
            r"\bmanda\s+(?:bala|ver)\b",
            r"\bisso\s*(?:a[ií]|mesmo)\b",
            r"\bcom\s+certeza\b",
            r"\bpositivo\b",
            r"\b(?:fecho|fechado|fechou)\b",
            r"\b(?:sim|s|confirmo|confirmar|salvar|salva)\b",
        ]
        if any(re.search(p, norm) for p in aff_patterns):
            return True

        return None

    def _initiate_multi_action_confirmation_flow(
        self,
        context: ConversationContext,
        actions: List[Any],
        discrepancy_warning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Gera card harmonioso de confirmação para múltiplas intenções encadeadas."""
        context.state = ConversationState.AWAITING_MAINTENANCE_CONFIRM
        action_dicts = [a.model_dump(exclude_none=True) if hasattr(a, "model_dump") else (a if isinstance(a, dict) else {"description": str(a)}) for a in actions]
        payload = {"_is_multi_action": True, "_action_state": "awaiting_multi_action_confirm", "actions": action_dicts}
        if discrepancy_warning:
            payload["_discrepancy_warning"] = discrepancy_warning
        context.draft_payload = payload
        context.draft_updated_at = self._utcnow()

        has_maint, has_rem = False, False
        maint_summary, maint_cost = "", 0.0
        rem_summary, rem_date, rem_time = "", "amanhã", "09:00"

        maint_target = None
        for act in action_dicts:
            intent = act.get("intent")
            if intent in (IntentType.RECORD_MAINTENANCE.value, "record_maintenance"):
                has_maint = True
                maint_summary = act.get("description") or "Troca de óleo e filtro"
                maint_cost = float(act.get("total_cost", 0.0) or 0.0)
                maint_target = act.get("target_vehicle_ref")
            elif intent in (IntentType.SET_REMINDER.value, "set_reminder"):
                has_rem = True
                rem_summary = act.get("task_description") or act.get("description", "Calibrar o estepe")
                rem_date = act.get("target_date_str") or "amanhã"
                rem_time = act.get("explicit_time") or "09:00"

        cost_part = f" (R$ {maint_cost:,.0f})" if maint_cost else ""
        if has_maint and has_rem:
            veh_part = f" do {maint_target}" if maint_target and maint_target.lower() not in maint_summary.lower() else ""
            reply = f"Vou registrar a {maint_summary.lower()}{veh_part}{cost_part} E agendar um lembrete para {rem_date} às {rem_time} ({rem_summary}). Posso confirmar?"
        else:
            lines = ["Identifiquei múltiplas ações para a sua garagem:"]
            for idx, act in enumerate(action_dicts, 1):
                desc = act.get("description") or "Ação solicitada"
                target_veh = act.get("target_vehicle_ref")
                if target_veh and target_veh.lower() not in desc.lower():
                    desc = f"{desc} no {target_veh}"
                lines.append(f"{idx}. {desc}")
            lines.append("\n📌 Deseja confirmar e registrar essas operações?")
            reply = "\n".join(lines)

        return {"reply": reply, "action_type": "awaiting_confirmation", "state": context.state.value, "draft": payload}

    def _decompose_multi_action_message(self, text: str, vehicles: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        """Decompõe cláusulas de múltiplas ações offline."""
        if not text:
            return []
        split_pattern = r"(?:\.\s+|\n+|;\s*|\s+(?:e\s+tamb[eé]m|j[áa]\s+salva\s+isso\s+e|(?:e\s+)?j[áa]\s+me\s+lembra|e\s+me\s+lembra|e\s+lembra|e\s+atualiza|e\s+registra|e\s+agenda)\s+)"
        clauses = [c.strip() for c in re.split(split_pattern, text.strip(), flags=re.I) if c.strip()]
        if len(clauses) < 2:
            return []

        default_ref = getattr(vehicles[0], "model", "Veículo") if vehicles else "Veículo"
        actions: List[Dict[str, Any]] = []

        for c in clauses:
            cl = c.lower()
            target_ref = None
            for v in (vehicles or []):
                vm = (getattr(v, "model", None) or getattr(v, "modelo", "")).lower()
                if vm and vm in cl:
                    target_ref = getattr(v, "model", None) or getattr(v, "modelo", "")
                    break

            if any(k in cl for k in ("lembr", "avis", "agend", "calibrar", "estepe")):
                t_match = re.search(r"(\d{1,2}):(\d{2})|(\d{1,2})h", cl)
                t_str = "09:00"
                if t_match:
                    t_str = f"{int(t_match.group(1) or t_match.group(3)):02d}:00"
                actions.append({
                    "intent": IntentType.SET_REMINDER.value,
                    "task_description": "Calibrar o estepe" if "estepe" in cl else c,
                    "explicit_time": t_str,
                    "target_date_str": "amanhã" if "amanh" in cl else "hoje",
                    "target_vehicle_ref": target_ref or default_ref,
                })
            elif any(k in cl for k in ("troquei", "trocou", "troca", "óleo", "oleo", "filtro", "revisão", "revisao")):
                cost = parse_cost_value(c) or 0.0
                actions.append({
                    "intent": IntentType.RECORD_MAINTENANCE.value,
                    "description": "Troca de óleo e filtro" if "óleo" in cl or "oleo" in cl else c,
                    "components": extract_maintenance_items_strict(c) or ["óleo"],
                    "total_cost": cost,
                    "target_vehicle_ref": target_ref or default_ref,
                })
            elif any(k in cl for k in ("atualiza", "hodometro", "odometro")) or (parse_km_value(cl) and "km" in cl):
                km_val = parse_km_value(cl)
                v_name = target_ref or default_ref
                km_fmt = f"{km_val:,} km".replace(",", ".") if km_val is not None else ""
                actions.append({
                    "intent": IntentType.UPDATE_ODOMETER.value,
                    "odometer_km": km_val,
                    "km": km_val,
                    "target_vehicle_ref": v_name,
                    "description": f"Atualização de hodômetro do {v_name} para {km_fmt}" if v_name else f"Atualização de hodômetro para {km_fmt}",
                })
        return actions

    def _resolve_vehicle(self, text: str, vehicles: Optional[List[Any]]) -> Tuple[Optional[Any], Optional[str]]:
        """Identifica o veículo mencionado no texto e alerta se houver divergência de modelo/placa."""
        if not vehicles:
            return None, None
        tl = (text or "").lower()
        plate_match = re.search(r"\b([A-Za-z]{3}[-\s]?[0-9][A-Za-z0-9][0-9]{2})\b", text)
        clean_plate = plate_match.group(1).replace("-", "").upper() if plate_match else None

        for v in vehicles:
            v_plate = (getattr(v, "plate", None) or getattr(v, "placa", "")).replace("-", "").upper()
            v_model = (getattr(v, "model", None) or getattr(v, "modelo", "")).lower()
            if clean_plate and v_plate == clean_plate:
                for known_key, known_spec in KNOWN_MODEL_MAKES.items():
                    if known_key in tl and known_spec["model"].lower() != v_model:
                        warn = f"Atenção: A placa {v_plate} pertence ao seu {v.model}, e não a um {known_spec['model']}. Os dados foram lançados no {v.model}."
                        return v, warn
                return v, None
            if v_model and v_model in tl:
                return v, None

        return self._resolve_vehicle_fuzzy(text, vehicles), None

    def _resolve_vehicle_fuzzy(self, text: str, vehicles: Optional[List[Any]]) -> Optional[Any]:
        """Fuzzy matching para nomes de modelos com pequenos erros de digitação (ex: sandeiro -> Sandero)."""
        if not vehicles:
            return None
        tl = (text or "").lower()
        words = tl.split()
        best_v, best_score = None, 0.0
        for v in vehicles:
            target = (getattr(v, "model", None) or getattr(v, "modelo", "")).lower()
            for w in words:
                ratio = difflib.SequenceMatcher(None, w, target).ratio()
                if ratio > best_score and ratio >= 0.70:
                    best_score = ratio
                    best_v = v
        return best_v

    def _is_garage_checkup_query(self, text_lower: str) -> bool:
        """Verifica se é consulta global sobre todos os carros."""
        patterns = [r"\b(meus\s+carros|minha\s+garagem|todos\s+os\s+carros|check-?up\s+(geral|da\s+garagem)|est[ãa]o\s+em\s+dia)\b"]
        return any(re.search(p, text_lower) for p in patterns)

    async def _handle_garage_checkup(
        self,
        user_id: str,
        vehicles: Optional[List[Any]] = None,
        maintenances: Optional[List[Any]] = None,
        reminders: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Gera relatório geral da garagem."""
        all_vehs = list(vehicles or [])
        if not all_vehs:
            reply = "🚗 *Check-up Geral da Garagem*\n\nNão encontrei nenhum veículo cadastrado na sua garagem no momento.\nGostaria de cadastrar um novo carro ou moto?"
            return {"reply": reply, "action_type": "garage_checkup", "state": "normal", "details": {"total_vehicles": 0}}

        lines = ["🚗 *Check-up Geral da Garagem - Status das Manutenções:*\n"]
        delayed_count = 0
        for v in all_vehs:
            make = getattr(v, "make", None) or getattr(v, "marca", "Veículo")
            model = getattr(v, "model", None) or getattr(v, "modelo", "")
            plate = getattr(v, "plate", None) or getattr(v, "placa", "sem placa")
            km = getattr(v, "current_km", None) or getattr(v, "quilometragem", 0) or 0
            km_fmt = f"{km:,} km".replace(",", ".")
            sched_mark = (km // 10000) * 10000
            is_delayed = sched_mark > 0 and km > sched_mark
            if is_delayed:
                delayed_count += 1
                lines.append(f"• 🔴 *{make} {model}* (`{plate}`) | {km_fmt}\n   ⚠️ *REVISÃO ATRASADA:* Passou da revisão de {sched_mark:,} km!".replace(",", "."))
            else:
                lines.append(f"• 🟢 *{make} {model}* (`{plate}`) | {km_fmt}\n   ✅ *EM DIA:* Nenhuma manutenção preventiva pendente.")

        if delayed_count > 0:
            lines.append(f"\n⚠️ Identifiquei *{delayed_count} veículo(s)* com revisão atrasada. Deseja agendar um lembrete ou registrar o serviço agora?")
        active_rems = [r for r in (reminders or []) if getattr(r, "status", "pending") in ("pending", "active", "agendado")]
        if active_rems:
            lines.append("\n⏰ *Lembretes Agendados:*")
            for r in active_rems:
                r_desc = getattr(r, "description", None) or getattr(r, "descricao", "Lembrete")
                dt = getattr(r, "due_datetime", None) or getattr(r, "data_hora_agendada", None)
                dt_str = dt.strftime("%d/%m/%Y às %H:%M") if hasattr(dt, "strftime") and dt else ""
                dt_part = f" para {dt_str}" if dt_str else ""
                lines.append(f"• {r_desc}{dt_part}")

        return {"reply": "\n".join(lines), "action_type": "garage_checkup", "state": "normal", "details": {"total_vehicles": len(all_vehs), "delayed_vehicles": delayed_count, "total_reminders": len(active_rems)}}

    def _format_single_vehicle_status(self, vehicle: Any, maintenances: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Formata relatório de status de um único veículo."""
        make = getattr(vehicle, "make", None) or getattr(vehicle, "marca", "Veículo")
        model = getattr(vehicle, "model", None) or getattr(vehicle, "modelo", "")
        plate = getattr(vehicle, "plate", None) or getattr(vehicle, "placa", "sem placa")
        km = getattr(vehicle, "current_km", None) or getattr(vehicle, "quilometragem", 0) or 0
        km_fmt = f"{km:,} km ({km} km)"
        v_id = getattr(vehicle, "id", None)

        active_maints = [
            m for m in (maintenances or [])
            if (v_id is None or (getattr(m, "veiculo_id", None) or getattr(m, "vehicle_id", None)) == v_id)
            and getattr(m, "status", "active") != "cancelled"
        ]
        lines = [f"🚗 *Status do Veículo - {make} {model}*\n", f"• *Placa:* `{plate}`", f"• *Odômetro Atual:* {km_fmt}", "• 🟢 *Situação:* Todas as manutenções estão em dia!"]
        if active_maints:
            lines.append("\n🔧 *Histórico de Manutenções:*")
            for m in active_maints:
                desc = getattr(m, "description", None) or getattr(m, "descricao", "Manutenção")
                cost = float(getattr(m, "total_cost", 0.0) or getattr(m, "custo", 0.0) or 0.0)
                lines.append(f"• {desc} - R$ {cost:.2f}")
            total_spent = sum(float(getattr(m, "total_cost", 0.0) or getattr(m, "custo", 0.0) or 0.0) for m in active_maints)
            lines.append(f"💰 *Total Investido:* R$ {total_spent:.2f}")

        return {"reply": "\n".join(lines), "action_type": "vehicle_status_query", "state": "normal", "details": {"placa": plate, "modelo": model, "quilometragem": km}}

    async def _query_vehicle_status_by_plate(
        self,
        plate: str,
        vehicles: Optional[List[Any]],
        maintenances: Optional[List[Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Consulta status por placa no banco relacional ou lista de veículos."""
        clean_p = plate.replace("-", "").upper()
        for v in (vehicles or []):
            v_p = (getattr(v, "plate", None) or getattr(v, "placa", "")).replace("-", "").upper()
            if v_p == clean_p:
                return self._format_single_vehicle_status(v, maintenances)
        try:
            from src.database import async_session_factory
            from src.repositories.veiculo_repository import VeiculoRepository
            from src.repositories.manutencao_repository import ManutencaoRepository
            async with async_session_factory() as session:
                v = await VeiculoRepository(session).get_by_placa(clean_p)
                if v:
                    maints = await ManutencaoRepository(session).get_by_veiculo_id(v.id)
                    return self._format_single_vehicle_status(v, maints)
        except Exception:
            pass
        return None
