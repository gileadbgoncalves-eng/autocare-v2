"""
Relógio de referência do AutoCare V2 (fuso do Brasil, UTC-3).

O servidor pode rodar em UTC; o usuário vive em UTC-3. Toda resolução de datas
relativas ("amanhã", "de manhãzinha") deve partir deste relógio.
"""
from datetime import datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3), name="BRT")


def now_brt() -> datetime:
    """Momento atual com tzinfo UTC-3."""
    return datetime.now(BRT)


def now_brt_naive() -> datetime:
    """Momento atual no horário de Brasília, sem tzinfo (formato gravado nas colunas DateTime)."""
    return now_brt().replace(tzinfo=None)


def build_temporal_context(now: datetime | None = None) -> str:
    """Linha de contexto temporal injetada no prompt do LLM antes de cada análise."""
    ref = now.astimezone(BRT) if now is not None and now.tzinfo else (now or now_brt())
    weekdays = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
                "sexta-feira", "sábado", "domingo")
    return (
        f"[CONTEXTO TEMPORAL]: Considere que AGORA é {ref.isoformat(timespec='seconds')} "
        f"({weekdays[ref.weekday()]}, {ref.strftime('%d/%m/%Y %H:%M')}, fuso de Brasília UTC-3). "
        "Ajuste suas datas relativas ('hoje', 'amanhã', 'sexta que vem', 'de manhãzinha') "
        "com base nesse momento exato."
    )
