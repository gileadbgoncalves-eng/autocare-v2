"""
AutoCare V2 - Pytest Configuration & Fixtures
Garante que o ambiente de teste rode veloz e desacoplado.
"""

import pytest
from src.config.settings import settings


@pytest.fixture(autouse=True)
def _configure_test_environment(monkeypatch):
    """
    Define WEBHOOK_DEBOUNCE_SECONDS como 0.0s durante testes gerais para execução
    instantânea sem esperas artificiais. Testes de debounce redefinem explicitamente.
    """
    monkeypatch.setattr(settings, "WEBHOOK_DEBOUNCE_SECONDS", 0.0)
