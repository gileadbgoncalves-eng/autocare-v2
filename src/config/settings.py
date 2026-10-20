"""
AutoCare V2 - Configurações Centrais do Sistema
Gerenciamento tipado de variáveis de ambiente usando pydantic-settings com validação e defaults seguros.
"""

import os
from typing import Optional
from pydantic import Field, field_validator

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    # Fallback seguro caso pydantic-settings não esteja instalado no ambiente imediato
    from pydantic import BaseModel as BaseSettings
    SettingsConfigDict = None


class Settings(BaseSettings):
    """Configurações centralizadas de infraestrutura, chaves de API e banco de dados."""

    if SettingsConfigDict:
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )

    # 1. Ambiente & Execução
    ENVIRONMENT: str = Field(default="development", description="development, staging ou production")
    PORT: int = Field(default=8080, description="Porta HTTP do servidor Webhook")
    LOG_LEVEL: str = Field(default="info", description="debug, info, warning, error")

    # 2. Google Gemini AI (Cérebro NLP)
    GEMINI_API_KEY: str = Field(default="", description="Chave de API do Google AI Studio / Gemini")
    GEMINI_PRIMARY_MODEL: str = Field(default="gemini-flash-lite-latest", description="Modelo veloz e inteligente para análise de intenções")
    GEMINI_FALLBACK_MODEL: str = Field(default="gemini-3.5-flash-lite", description="Modelo alternativo para contingência de timeout")
    GEMINI_TIMEOUT_SECONDS: float = Field(default=5.0, description="Timeout estrito para requisições de IA")
    GEMINI_MAX_RETRIES: int = Field(default=3, description="Tentativas com backoff exponencial em 429/5xx")

    # 3. Banco de Dados Relacional (SQLAlchemy 2.0 Async)
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///autocare.db",
        description="URL de conexão assíncrona SQLAlchemy"
    )

    # 4. Provedores de WhatsApp
    PROVIDER: str = Field(default="evolution", description="evolution, zapi ou twilio")
    EVOLUTION_BASE_URL: str = Field(default="https://evolution-api-production-6ecb.up.railway.app")
    EVOLUTION_API_KEY: str = Field(default="8b5aafc3215bb38743847011ea655f2fae031f0b003edbbd32d45b4f32097e9f")
    EVOLUTION_INSTANCE: str = Field(default="autocare")
    EVOLUTION_TYPING_DELAY_MS: int = Field(default=200, description="Simulação de digitação em milissegundos")

    ZAPI_INSTANCE_ID: str = Field(default="")
    ZAPI_TOKEN: str = Field(default="")
    ZAPI_CLIENT_TOKEN: str = Field(default="")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, v: Optional[str]) -> str:
        """Normaliza a URL do banco para drivers assíncronos (asyncpg / aiosqlite)."""
        if not v:
            return "sqlite+aiosqlite:///autocare.db"
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql+psycopg2://"):
            return v.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        if v.startswith("sqlite:///") and "+aiosqlite" not in v:
            return v.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        return v

    @classmethod
    def load_from_env(cls) -> "Settings":
        """Método de inicialização explícita lendo do os.environ."""
        kwargs = {}
        for field_name in cls.model_fields.keys():
            val = os.getenv(field_name)
            if val is not None:
                kwargs[field_name] = val
        return cls(**kwargs)


# Instância singleton padrão da aplicação
try:
    settings = Settings()
except Exception:
    settings = Settings.load_from_env()
