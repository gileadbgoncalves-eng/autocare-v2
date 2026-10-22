"""
AutoCare V2 - Configuração do Banco de Dados Assíncrono (SQLAlchemy 2.0 Async)
Configurado para asyncpg (PostgreSQL) e compatível com aiosqlite (testes/local).
"""

import os
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase

# Carrega variáveis de ambiente ou utiliza settings
try:
    from src.config.settings import settings
    DEFAULT_DB_URL = settings.DATABASE_URL
except Exception:
    DEFAULT_DB_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://autocare_user:autocare_pass@localhost:5432/autocare_db",
    )


def get_async_database_url(url: str) -> str:
    """Normaliza o prefixo do driver para garantir compatibilidade assíncrona."""
    if not url:
        return "postgresql+asyncpg://autocare_user:autocare_pass@localhost:5432/autocare_db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite:///") and "+aiosqlite" not in url:
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


DATABASE_ASYNC_URL = get_async_database_url(DEFAULT_DB_URL)

# Base Declarativa do SQLAlchemy 2.0
class Base(DeclarativeBase):
    pass


# Inicialização do Engine Assíncrono
is_sqlite = DATABASE_ASYNC_URL.startswith("sqlite")
engine_kwargs = {"echo": False, "future": True}

if not is_sqlite:
    engine_kwargs.update({
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    })

engine: AsyncEngine = create_async_engine(DATABASE_ASYNC_URL, **engine_kwargs)

# Sessionmaker assíncrono padrão
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependência FastAPI que injeta uma sessão transacional assíncrona.
    Garante rollback em exceções e fechamento automático.
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
