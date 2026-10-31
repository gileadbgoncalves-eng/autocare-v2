"""
AutoCare V2 - httpx.AsyncClient compartilhado (A9)

Antes: cada `send_whatsapp_reply` / `_call_gemini_with_retry` abria e fechava um
`httpx.AsyncClient` (novo pool TCP + handshake TLS por mensagem). Agora há um singleton
por event loop, com keep-alive, criado sob demanda e fechado no `lifespan` do FastAPI.

Por que "por event loop": um AsyncClient fica atrelado ao loop em que foi usado. Em
produção há um loop só; nos testes (pytest-asyncio) cada teste tem seu loop. Se o loop
mudou/fechou, descartamos a referência e criamos outro (o antigo morreu junto com o loop).
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Tuple, Union

from src.config.settings import settings

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    HAS_HTTPX = False

# Timeouts explícitos por operação (A7)
TIMEOUT_WHATSAPP_OUTBOUND = httpx.Timeout(5.0, connect=3.0) if HAS_HTTPX else 5.0
TIMEOUT_GEMINI_API = httpx.Timeout(30.0, connect=5.0) if HAS_HTTPX else 30.0
TIMEOUT_HEALTHCHECK = httpx.Timeout(2.0, connect=1.0) if HAS_HTTPX else 2.0

# Limites explícitos do pool de conexões TCP/TLS (A7)
DEFAULT_LIMITS = (
    httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0)
    if HAS_HTTPX
    else None
)

_state: Optional[Tuple[asyncio.AbstractEventLoop, "httpx.AsyncClient"]] = None


def get_http_client(timeout: Union[float, "httpx.Timeout"] = 5.0) -> "httpx.AsyncClient":
    """Retorna o AsyncClient compartilhado do loop corrente. `timeout` é aplicado por requisição."""
    global _state
    if not HAS_HTTPX:  # pragma: no cover
        raise RuntimeError("httpx não está instalado")
    loop = asyncio.get_running_loop()
    if _state is not None:
        st_loop, st_client = _state
        if st_loop is loop and not loop.is_closed():
            return st_client
    resolved_timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(float(timeout), connect=min(float(timeout), 5.0))
    client = httpx.AsyncClient(
        timeout=resolved_timeout,
        limits=DEFAULT_LIMITS,
    )
    client._configured_limits = DEFAULT_LIMITS  # Exposto para inspeção determinística em testes (A7)
    _state = (loop, client)
    return client


@asynccontextmanager
async def shared_http_client(timeout: Union[float, "httpx.Timeout"] = 5.0):
    """
    `async with shared_http_client(timeout) as c:` - usa o client compartilhado SEM fechá-lo ao sair.
    Se a feature flag `AUTOCARE_FEATURE_SHARED_HTTP_CLIENT` estiver desligada (C3), abre e fecha
    um cliente efêmero por chamada (fallback seguro sem redeploy).
    """
    if not getattr(settings, "AUTOCARE_FEATURE_SHARED_HTTP_CLIENT", True):
        resolved_timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(float(timeout))
        async with httpx.AsyncClient(timeout=resolved_timeout, limits=DEFAULT_LIMITS) as ephemeral_client:
            yield ephemeral_client
        return
    yield get_http_client(timeout)


async def close_http_client() -> None:
    """Fecha o client compartilhado (chamado no shutdown do lifespan)."""
    global _state
    if _state is None:
        return
    st_loop, st_client = _state
    _state = None
    try:
        if not st_client.is_closed:
            await st_client.aclose()
    except Exception:  # loop antigo já fechado: nada a fazer
        pass

