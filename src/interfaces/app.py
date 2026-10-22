"""
AutoCare V2 - FastAPI Application Factory
Ponto de montagem da API HTTP com suporte assíncrono, gerenciamento de ciclo de vida
(Lifespan para inicialização de banco), middleware de métricas e tratamento global de exceções.
"""

import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.config.settings import settings
from src.infrastructure.database.session import init_db, close_db
from src.interfaces.api.webhook import router as webhook_router
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerenciamento de Ciclo de Vida da Aplicação (Startup e Shutdown)."""
    logger.info("🚀 Inicializando AutoCare V2...")
    try:
        await init_db()
        logger.info("✅ Conexão e tabelas do banco de dados verificadas com sucesso.")
    except Exception as e:
        logger.error(f"❌ Falha ao inicializar banco de dados: {e}", exc_info=True)

    yield

    logger.info("🛑 Encerrando AutoCare V2...")
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

    # 5. Inclusão das Rotas de Webhook
    app.include_router(webhook_router)

    return app


# Instância principal da aplicação para servidores ASGI (uvicorn src.interfaces.app:app)
app = create_app()
