"""
AutoCare V2 - FastAPI Application Factory
Ponto de montagem da API HTTP com suporte assíncrono, gerenciamento de ciclo de vida
(Lifespan para inicialização de banco), middleware de métricas e tratamento global de exceções.
"""

import asyncio
import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from src.ai.client import get_urllib_executor, shutdown_urllib_executor
from src.config.settings import settings
from src.infrastructure.database.session import init_db, close_db, async_session_factory
from src.infrastructure.http_client import close_http_client, TIMEOUT_HEALTHCHECK, get_http_client
from src.interfaces.api.webhook import router as webhook_router
from src.interfaces.routers import (
    veiculos_router,
    proprietarios_router,
    manutencoes_router,
)
from src.application.services.notification_service import notification_scheduler
from pydantic import BaseModel, Field

# Configuração de Logs
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("autocare.app")


class HealthCheckResponse(BaseModel):
    """Modelo canônico para o endpoint de verificação de integridade."""
    status: str = Field(..., description="Estado de saúde da aplicação")
    service: str = Field(..., description="Nome do microsserviço")
    version: str = Field(..., description="Versão semântica da aplicação")
    environment: str = Field(..., description="Ambiente de execução (development/production)")
    provider: str = Field(..., description="Gateway WhatsApp padrão configurado")


async def ping_database() -> float:
    """C3 (R3): executa SELECT 1 no banco e devolve a latência em milissegundos."""
    t0 = time.perf_counter()
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
    return round((time.perf_counter() - t0) * 1000.0, 2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerenciamento de Ciclo de Vida da Aplicação (Startup e Shutdown)."""
    logger.info("🚀 Inicializando AutoCare V2...")
    try:
        await init_db()
        logger.info("✅ Conexão e tabelas do banco de dados verificadas com sucesso.")
    except Exception as e:
        logger.error(f"❌ Falha ao inicializar banco de dados: {e}", exc_info=True)

    # B9 (S6): Inicializa ThreadPoolExecutor limitado (max_workers=8, thread_name_prefix="gemini-urllib")
    app.state.urllib_executor = get_urllib_executor()

    # Inicia o agendador de notificações em segundo plano
    notification_scheduler.start()

    yield

    logger.info("🛑 Encerrando AutoCare V2...")
    await notification_scheduler.stop()
    shutdown_urllib_executor(wait=False)
    await close_http_client()
    await close_db()
    logger.info("✅ Conexões do banco liberadas.")


def create_app() -> FastAPI:
    """Cria e configura a instância do FastAPI."""
    app = FastAPI(
        title="AutoCare V2 - Intelligent Automotive Assistant",
        description="Backend assíncrono modular com IA Generativa (Google Gemini) para gestão veicular.",
        version="2.0.0",
        lifespan=lifespan,
    )

    # 1. Middleware de CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 2. Middleware de Logs e Métricas de Tempo de Resposta
    @app.middleware("http")
    async def log_response_time_middleware(request: Request, call_next):
        start_time = time.perf_counter()
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        logger.info(f"[{request.method}] {request.url.path} -> {response.status_code} ({process_time_ms:.2f}ms)")
        return response

    # 3. Tratamento Global de Exceções Não Capturadas
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"Exceção não tratada na rota [{request.method} {request.url.path}]: {exc}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "error",
                "message": "Ocorreu um erro interno no servidor ao processar a requisição.",
                "path": request.url.path,
            },
        )

    # 4. Endpoints de Diagnóstico e Saúde (Health Check)
    @app.get("/", tags=["Health"], response_model=HealthCheckResponse, summary="Verificação de integridade do serviço")
    @app.get("/health", tags=["Health"], response_model=HealthCheckResponse, summary="Verificação de integridade do serviço")
    async def health_check():
        return HealthCheckResponse(
            status="healthy",
            service="AutoCare V2",
            version="2.0.0",
            environment=settings.ENVIRONMENT,
            provider=settings.PROVIDER,
        )

    @app.get("/healthz", tags=["Health"], summary="Healthcheck operacional detalhado com timeout de 2s (C3 / R3 / R6)")
    async def healthz():
        db_ok = True
        db_ping_ms = None
        timeout_sec = 2.0
        if isinstance(TIMEOUT_HEALTHCHECK, (int, float)):
            timeout_sec = float(TIMEOUT_HEALTHCHECK)
        elif hasattr(TIMEOUT_HEALTHCHECK, "read") and TIMEOUT_HEALTHCHECK.read is not None:
            timeout_sec = float(TIMEOUT_HEALTHCHECK.read)

        try:
            db_ping_ms = await asyncio.wait_for(ping_database(), timeout=timeout_sec)
            if db_ping_ms is not None and db_ping_ms > (timeout_sec * 1000.0):
                db_ok = False
        except Exception as exc:
            logger.warning("Healthcheck DB ping falhou ou excedeu timeout: %s", exc)
            db_ok = False

        # R6: Validação do cliente HTTP compartilhado vivo
        client_ok = False
        try:
            client = get_http_client()
            client_ok = not client.is_closed
        except Exception as exc_client:
            logger.warning("Healthcheck shared_http_client falhou: %s", exc_client)
            client_ok = False

        raw_key = (getattr(settings, "GEMINI_API_KEY", "") or "").strip()
        gemini_key_configured = bool(
            raw_key and len(raw_key) > 10 and "sua_chave" not in raw_key.lower() and "placeholder" not in raw_key.lower()
        )
        checks = {
            "shared_http_client": bool(getattr(settings, "AUTOCARE_FEATURE_SHARED_HTTP_CLIENT", True)),
            "shared_http_client_feature_flag": bool(getattr(settings, "AUTOCARE_FEATURE_SHARED_HTTP_CLIENT", True)),
            "shared_http_client_alive": client_ok,
            "rate_limiter": bool(getattr(settings, "AUTOCARE_FEATURE_RATE_LIMIT", True)),
            "db_ping_ms": db_ping_ms,
            "gemini_key_configured": gemini_key_configured,
        }
        overall_status = "ok" if (db_ok and client_ok) else "degraded"
        return {"status": overall_status, "checks": checks}

    # 5. Inclusão das Rotas de Webhook, Veículos, Proprietários e Manutenções
    app.include_router(webhook_router)
    app.include_router(veiculos_router)
    app.include_router(proprietarios_router)
    app.include_router(manutencoes_router)

    return app


# Instância principal da aplicação para servidores ASGI (uvicorn src.interfaces.app:app)
app = create_app()
