# ==============================================================================
# AutoCare V2 - Production Dockerfile
# Base: Python 3.12-slim com execução assíncrona FastAPI / Uvicorn
# ==============================================================================

FROM python:3.12-slim as base

# Configurações de ambiente
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Instala dependências de sistema mínimas para compilação/healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copia e instala dependências Python primeiro (aproveitamento de cache)
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copia o código-fonte da aplicação
COPY src/ ./src
COPY run_v2_tests.py .

# Cria usuário não-root por segurança (Clean Architecture & DevSecOps)
RUN useradd -m -u 1001 appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

# Healthcheck embutido do container
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Comando padrão para inicialização do servidor Uvicorn
CMD ["uvicorn", "src.interfaces.app:app", "--host", "0.0.0.0", "--port", "8080"]
