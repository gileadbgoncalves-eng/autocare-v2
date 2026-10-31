"""
AutoCare V2 - Production Main Entry Point
Ponto de entrada canônico para execução em contêineres e provedores PaaS (Railway, Render, Docker).
Importa a aplicação FastAPI oficial do AutoCare V2 montada em src.interfaces.app:app.
"""

import os
import uvicorn
from src.interfaces.app import app, create_app

__all__ = ["app", "create_app"]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
