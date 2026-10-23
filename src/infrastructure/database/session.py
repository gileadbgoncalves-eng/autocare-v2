"""
AutoCare V2 - Database Session Configuration (SQLAlchemy 2.0 Async)
Configuração do motor assíncrono e gerenciador de sessões com suporte
nativo a PostgreSQL (asyncpg) e SQLite (aiosqlite).
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
from src.config.settings import settings


def get_async_database_url(url: str) -> str:
    """
    Normaliza a URL do banco para utilizar drivers assíncronos compatíveis
    com SQLAlchemy 2.0 (asyncpg para PostgreSQL e aiosqlite para SQLite).
    """
    if not url:
        return "sqlite+aiosqlite:///autocare.db"

    # Conversão de URLs comuns do PostgreSQL para asyncpg
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)

    # Conversão de URLs SQLite síncronas para aiosqlite
    if url.startswith("sqlite:///") and "+aiosqlite" not in url:
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

    return url


# Definição da Classe Base Declarativa do SQLAlchemy 2.0
class Base(DeclarativeBase):
    pass


# Inicialização do Engine Assíncrono com pool resiliente
DATABASE_ASYNC_URL = get_async_database_url(settings.DATABASE_URL)

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

async_engine: AsyncEngine = create_async_engine(DATABASE_ASYNC_URL, **engine_kwargs)

if is_sqlite:
    from sqlalchemy import event

    @event.listens_for(async_engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

# Gerador de Sessões Assíncronas
async_session_factory = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection / Context manager para obtenção de sessões do banco de dados."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Cria tabelas no banco de dados se não existirem (ideal para bootstrap e migrações iniciais)."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_soft_delete_columns)
        try:
            from src.models import Base as NewBase
            await conn.run_sync(NewBase.metadata.create_all)
        except Exception:
            pass


def _ensure_soft_delete_columns(sync_conn) -> None:
    """Migração aditiva: adiciona `maintenance_records.status` em bancos pré-existentes."""
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "maintenance_records" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("maintenance_records")}
    if "status" not in columns:
        sync_conn.execute(
            text("ALTER TABLE maintenance_records ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'")
        )



async def close_db() -> None:
    """Encerra graciosamente o pool de conexões do banco de dados."""
    await async_engine.dispose()
