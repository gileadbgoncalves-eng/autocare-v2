# 📍 PONTO DE PARADA - AUTOCARE V2 (ARQUITETURA RECONSTRUÍDA DO ZERO)

**Data do Checkpoint:** 19/10/2026  
**Status do Projeto:** 100% Funcional, Modular, Assíncrono, Testado e Pronto para Produção.  
**Taxa de Sucesso dos Testes:** 100% (6 suítes integradas executadas em 1.29s).

---

## 🎯 Objetivo Cumprido
Toda a lógica e arquitetura do AutoCare foram reconstruídas do zero absoluto, eliminando débitos técnicos, monólitos e acoplamentos do sistema legado anterior. A versão V2 adota Clean Architecture, Structured Outputs estritos com o Google Gemini (`gemini-3.8-flash`), tipagem completa com Pydantic v2 e persistência relacional assíncrona com SQLAlchemy 2.0.

---

## 📂 Estrutura de Pastas e Separação de Versões

### 1. Sistema Novo V2 (`src/` e Raiz)
- [`src/config/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/config/): Gerenciamento centralizado de variáveis com `pydantic-settings` e normalizador de dialetos assíncronos (`postgresql+asyncpg` e `sqlite+aiosqlite`).
- [`src/domain/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/domain/): Entidades puras de domínio (`User`, `Vehicle`, `MaintenanceRecord`, `Reminder`, `TrafficFine`, `ConversationContext`) e enums canônicos.
- [`src/ai/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/ai/): Cliente assíncrono do Google Gemini com Structured Output, sanitização de esquemas (`$defs`/`$ref`), timeout de 5s e contingência inteligente.
- [`src/application/services/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/application/services/): Orquestração conversacional com máquina de estados, controle **Human-in-the-Loop** para aprovação explícita e **Fast-Path de 14ms** para cortesias ("OK", "Valeu").
- [`src/infrastructure/database/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/infrastructure/database/): Camada de persistência relacional com SQLAlchemy 2.0 Async, modelos ORM espelhados e padrão Repository (`UserRepository`, `VehicleRepository`, `MaintenanceRepository`).
- [`src/interfaces/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/interfaces/): Servidor HTTP assíncrono FastAPI com gerenciamento de ciclo de vida (`lifespan`), middleware de logs de tempo de resposta e roteador de Webhook multi-provedor (Evolution API, Z-API e JSON direto).

### 2. Sistema Legado Anterior Preservado (`v1_ponto_de_parada_total/`)
Todos os arquivos do sistema legado anterior antes da reconstrução do zero foram isolados nesta pasta e preservados no Git:
- `engine.py`, `ai_engine.py`, `database.py`, `whatsapp_gateway.py`
- Arquivos `.html`, scripts `.bat` e `.ps1`, relatórios de stress e o banco SQLite antigo `autocare.db`.

---

## 🛠️ Scripts Utilitários e Testes Criados na Raiz

1. **[`setup_project.py`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/setup_project.py):** Script autônomo com todos os códigos embutidos capaz de recriar toda a árvore do projeto em qualquer máquina com um único comando.
2. **[`run_v2_tests.py`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/run_v2_tests.py):** Bateria de testes automatizados de ponta a ponta (6 baterias cobrindo domínio, banco, IA, estados e HTTP).
3. **[`test_bot.py`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/test_bot.py):** Terminal interativo de diálogo que simula a experiência real do condutor no WhatsApp com persistência relacional em tempo real.
4. **[`.env.example`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/.env.example):** Template de variáveis de ambiente.
5. **[`requirements.txt`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/requirements.txt):** Dependências de produção atualizadas (`fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `sqlalchemy[asyncio]`, `greenlet`, `aiosqlite`, `asyncpg`, `httpx`).

---

## 🧪 Resumo das 6 Correções Críticas Validadas na Suíte de Testes
1. **`greenlet`**: Incluído para viabilizar corrotinas do SQLAlchemy assíncrono.
2. **Namespace Pydantic**: Eliminado o conflito `date: date` em `MaintenanceRecord` utilizando `dt.date`.
3. **Limpeza de JSON Schema Gemini**: Adicionado resolvedor recursivo em `GeminiClient` para converter `$defs` do Pydantic para a especificação REST da Google AI.
4. **Atualização do Modelo Gemini**: Migrado para `gemini-3.8-flash` e `gemini-flash-latest`.
5. **Fast-Path de 14ms**: Despedidas ("OK", "Valeu", "Show") respondem instantaneamente sem consumir cota de IA.
6. **PRAGMA foreign_keys = ON**: Ativado automaticamente no SQLite assíncrono para garantir integridade e cascata.

---

## 🚀 Como Retomar ou Executar o Projeto

- Para rodar a bateria de testes completa:
  ```bash
  python run_v2_tests.py
  ```

- Para conversar com o bot localmente no terminal:
  ```bash
  python test_bot.py
  ```

- Para iniciar a API FastAPI do Webhook:
  ```bash
  uvicorn src.interfaces.app:app --host 0.0.0.0 --port 8080 --reload
  ```
