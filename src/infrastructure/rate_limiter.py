"""
AutoCare V2 - Rate limiter simples por usuário (S4)

Janela deslizante em memória (deque de timestamps por chave). Suficiente para o deploy
single-worker documentado (A1/A2); com múltiplos workers o limite vira "por worker" e deve
migrar para Redis (INCR + EXPIRE) junto com o estado conversacional.
"""

import time
from collections import deque
from typing import Callable, Deque, Dict


class SlidingWindowRateLimiter:
    def __init__(self, max_events: int = 20, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._clock = clock
        self._events: Dict[str, Deque[float]] = {}

    def allow(self, key: str, max_events: int = None) -> bool:
        """Registra o evento e devolve False se a chave excedeu o limite na janela."""
        limit = self.max_events if max_events is None else max_events
        if limit is None or limit <= 0:  # 0/negativo desativa o limite
            return True
        now = self._clock()
        q = self._events.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        self._events.clear()
