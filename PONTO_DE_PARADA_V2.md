# 📍 PONTO DE PARADA TOTAL - AUTOQUER / AUTOCARE V2 (GUARDRAILS, TTL, ESCAPE GLOBAL & MULTI-ACTION)

**Data do Checkpoint:** 23/10/2026 - 16:35  
**Versão:** Autoquer / AutoCare V2.8.0 (Financial Guardrails R$ 9,90/mês, Draft TTL 30min, BRT UTC-3 Clock, Global Undo Soft-Delete, Mixed Chained Intents & E2E Traffic Simulation)  
**Status do Projeto:** 100% Funcional, 66/66 Testes Pytest Aprovados e 40/40 Interações de Simulação E2E Aprovadas (100% Pass Rate).  

---

## 📊 Matriz Consolidada de Homologação dos Testes

| Suíte de Testes | Arquivo / Comando | Abrangência / Escopo | Resultado |
| :--- | :--- | :--- | :---: |
| **Guardrails de Escopo e Tokens** | `tests/test_guardrails_and_token_limits.py` | Bloqueio de assuntos fora de escopo (receitas, programação, piadas), limite 250-400 tokens, pruning de 5 turnos | **20/20 PASS (100%) ✅** |
| **TTL, Fuso UTC-3 e Escape Global** | `tests/test_ttl_and_global_escape.py` | Expiração de rascunho (30 min), injeção temporal BRT no prompt, soft delete (5 min) via webhook | **13/13 PASS (100%) ✅** |
| **Intenções Mistas Encadeadas** | `tests/test_mixed_chained_intents.py` | Sincronização Pydantic ActionDraft, fluxo de conversação e persistência dupla (Manutenção + Lembrete) | **3/3 PASS (100%) ✅** |
| **Auto-Enriquecimento no Onboarding** | `tests/test_auto_enrichment_onboarding.py` | Inferência técnica (motor, câmbio, direção) e confirmação proativa sem questionários | **5/5 PASS (100%) ✅** |
| **Refatoração dos Testes de Estresse** | `tests/test_stress_refactor_fixes.py` | Múltiplas ações, classificador semântico NLP e conflito Placa vs Modelo | **5/5 PASS (100%) ✅** |
| **Correções Críticas de QA** | `tests/test_qa_refactor_fixes.py` | Extração temporal/local, alertas enxutos, check-up global de garagem e fuzzy matching | **7/7 PASS (100%) ✅** |
| **Motor de Notificações & Scheduler** | `tests/test_notification_batch_scheduler.py` | Batching de alertas, isolamento FK, bypass de agendamento e horário personalizado | **3/3 PASS (100%) ✅** |
| **APIs REST de Negócio** | `tests/test_proprietarios_manutencoes_api.py` e `tests/test_veiculos_api.py` | Endpoints FastAPI, validações Pydantic, placas Mercosul/antigas e regras de integridade | **8/8 PASS (100%) ✅** |
| **Integração Bot-Banco** | `tests/test_bot_integration.py` | Resposta do assistente conectada ao banco SQLite/Postgres com sanitização de placas | **2/2 PASS (100%) ✅** |
| **Suíte Global Automatizada (Pytest)** | `pytest tests/ -v -k "not simulate_whatsapp_traffic"` | Cobertura total regressiva e preventiva de todas as camadas do sistema | **66/66 PASS (100%) ✅** |
| **Simulação E2E de Tráfego WhatsApp** | `python tests/simulate_whatsapp_traffic.py` | Jornada de 6 meses de uso simulado (40 turnos sequenciais, 3 veículos, time travel) | **40/40 PASS (100%) ✅** |

---

## 🛡️ Principais Funcionalidades e Blindagens Implementadas (V2.8.0)

### 1. Blindagem Financeira e Guardrails de Custo (Autoquer - R$ 9,90/mês)
- **Filtro Pré-LLM Custo Zero (`src/ai/guardrails.py`)**:
  - Interceptor determinístico que detecta perguntas fora do universo automotivo (receitas de bolo, humor, programação, física quântica, redações escolares).
  - Responde imediatamente sem disparar nenhuma chamada para a API do Gemini, poupando 100% dos tokens de requisições off-topic.
  - Resposta padrão obrigatória:
    > *"Desculpe, sou o assistente exclusivo do Autoquer e só consigo ajudar com dúvidas e registros sobre o seu veículo. Como posso ajudar com seu carro hoje?"*
- **Prompt Mestre Autoquer (`src/ai/prompts.py`)**:
  - Persona atualizada para **Autoquer** com a *Regra de Bloqueio Absoluto*.
  - Instrução rigorosa para respostas diretas, concisas e sem prolixidade.
- **Teto Seguro de Tokens de Saída (`maxOutputTokens`)**:
  - Configurado em 400 tokens de saída (limitado rigidamente entre 250 e 400 em `src/ai/client.py`).
  - Modelo econômico de alta velocidade e baixo custo: `gemini-flash-lite-latest`.
- **Fatiamento Inteligente do Histórico (`prune_history`)**:
  - Envia no máximo as últimas 5 interações (turnos) para a API.
  - Evita duplicação da mensagem atual no histórico e trunca turnos longos em 400 caracteres.

### 2. TTL de Rascunho e Timeout da Máquina de Estados (30 minutos)
- **Problema Resolvido**: Usuário recebia pedido de confirmação, dormia e respondia *"Sim"* dois dias depois, gravando lembrete relativo com data defasada.
- **Solução (`src/application/services/conversation.py`)**:
  - Adicionado controle de `draft_updated_at` com TTL configurável de 30 minutos.
  - Ao receber confirmação tardia, limpa o estado pendente e responde cordialmente:
    > *"Aquele seu registro expirou por tempo inativo. Pode me repetir o que você queria salvar?"*
  - Se o usuário enviar um novo comando em vez de uma resposta solta, o rascunho antigo é descartado e o novo comando é processado normalmente.

### 3. Injeção de Contexto Temporal e Relógio BRT UTC-3
- **Problema Resolvido**: Servidor rodando em UTC registrava "amanhã de manhãzinha" dito às 02h da madrugada como o dia errado.
- **Solução (`src/config/clock.py` e `src/interfaces/api/webhook.py`)**:
  - Criado módulo central de relógio em UTC-3 (`now_brt()`, `now_brt_naive()`, `build_temporal_context()`).
  - Todo prompt do Gemini é injetado com o carimbo exato:
    > `[CONTEXTO TEMPORAL]: Considere que AGORA é 2026-10-23T... (dia-da-semana, DD/MM/YYYY HH:MM, fuso de Brasília UTC-3). Ajuste suas datas relativas com base nisso.`
  - Resoluções de datas no webhook usam o relógio de Brasília, e não o relógio local do servidor.

### 4. Comando de Escape Global e Desfazer Pós-Commit (Soft Delete 5 min)
- **Problema Resolvido**: O usuário confirmava uma gravação no banco e não tinha como corrigir ou cancelar se errou.
- **Solução (`src/infrastructure/database/repositories.py` e `src/interfaces/api/webhook.py`)**:
  - Interceptor antes do LLM reconhece: *"desfaz"*, *"cancela o último"*, *"apaga isso que te mandei"*, *"apague o último registro"*.
  - Executa **Soft Delete** (`status = 'cancelled'`) no último registro inserido pelo usuário nos últimos 5 minutos.
  - Lotes de intenções mistas (manutenção + lembrete criados juntos) são cancelados em conjunto.
  - Resposta do assistente:
    > *"Pronto! O último registro foi apagado. O que quer corrigir?"*
  - Migração aditiva em `session.py` adiciona a coluna `status` na tabela `maintenance_records` preservando dados existentes.

### 5. Intenções Mistas Encadeadas (Mixed Chained Intents)
- **Problema Resolvido**: *"Troquei óleo e filtro por 550 reais. Já salva isso e me lembra amanhã às 9h de calibrar o estepe."* O bot salvava a manutenção mas esquecia o lembrete.
- **Solução**:
  - `ActionDraft` (`src/ai/schemas.py`) sincronizado com campos de manutenção e lembrete.
  - Pré-confirmação harmoniosa:
    > *"Vou registrar a troca de óleo e filtro (R$ 550) E agendar um lembrete para amanhã às 09:00 (Calibrar o estepe). Posso confirmar?"*
  - O webhook persiste ambas as entidades atomicamente no banco após a confirmação.

### 6. Auto-Enriquecimento no Onboarding de Veículos (Auto-Enrichment)
- Ao cadastrar modelos com versão e ano (ex: *"Nivus Highline 2024"* ou *"Corolla XEi"*), a base automotiva interna autopreenche:
  - Motor (ex: `1.0 Turbo` / `2.0 Flex`)
  - Câmbio (ex: `Automático` / `CVT`)
  - Direção (ex: `Elétrica`)
- O bot pula o interrogatório e emite resposta de aprovação proativa com a ficha técnica completa.

### 7. Resolução de Conflitos e NLP Natural
- **Resolução Placa vs Modelo**: Emite aviso explicativo transparente se o usuário citar um modelo diferente da placa cadastrada.
- **NLP de Confirmação e Cancelamento**: Compreende coloquialismos (*"pode meter marcha"*, *"manda bala"* -> Confirmação; *"foi um erro"*, *"esquece"* -> Cancelamento gracioso).
- **Fuzzy Matching**: Corrige grafias aproximadas (*"sandeiro"*, *"covic"*) sem falhar a busca.

---

## 📂 Arquitetura de Arquivos Principais

```text
autocare-v2/
├── PONTO_DE_PARADA_V2.md                 # Este documento de referência e estado consolidado
├── src/
│   ├── ai/
│   │   ├── client.py                     # Cliente Gemini assíncrono com retry, timeout e maxOutputTokens (400)
│   │   ├── guardrails.py                 # Filtro de escopo custo-zero e poda de histórico (5 turnos)
│   │   ├── prompts.py                    # Prompt mestre Autoquer, Regra de Bloqueio Absoluto e injeção temporal
│   │   └── schemas.py                    # Schemas Pydantic V2 (GeminiAnalysisResponse, ActionDraft, etc.)
│   ├── application/
│   │   └── services/
│   │       ├── conversation.py           # Máquina de estados, TTL (30 min), interceptor de undo e NLP
│   │       └── notification_service.py   # Motor de batching diário e agendamento de alertas
│   ├── config/
│   │   ├── clock.py                      # Relógio oficial do Brasil (UTC-3 / BRT)
│   │   └── settings.py                   # Configurações Pydantic BaseSettings
│   ├── domain/
│   │   ├── entities.py                   # Entidades ricas de domínio
│   │   └── enums.py                      # Enums (ConversationState, RecordStatus, etc.)
│   ├── infrastructure/
│   │   └── database/
│   │       ├── models.py                 # Modelos SQLAlchemy 2.0 (suporte a soft-delete status='cancelled')
│   │       ├── repositories.py           # Repositórios assíncronos e soft_delete_last_user_record
│   │       └── session.py                # Conexão aiosqlite/asyncpg e migrações aditivas
│   └── interfaces/
│       ├── api/
│       │   └── webhook.py                # Endpoint WhatsApp, despacho transacional e interceptor de undo
│       └── app.py                        # Instância FastAPI e lifespan de inicialização
└── tests/
    ├── simulate_whatsapp_traffic.py      # Simulação E2E de tráfego real de 6 meses (40 interações)
    ├── test_guardrails_and_token_limits.py # 20 testes de escopo e limites de tokens
    ├── test_ttl_and_global_escape.py     # 13 testes de TTL, fuso UTC-3 e undo soft-delete
    ├── test_mixed_chained_intents.py     # 3 testes de intenções mistas encadeadas
    ├── test_auto_enrichment_onboarding.py# 5 testes de onboarding e ficha técnica
    ├── test_stress_refactor_fixes.py     # 5 testes de estresse, NLP e conflito de entidades
    ├── test_qa_refactor_fixes.py         # 7 testes de QA (datas, oficinas, fuzzy, check-up)
    ├── test_notification_batch_scheduler.py # 3 testes de notificações e batching
    ├── test_proprietarios_manutencoes_api.py # 4 testes de API CRUD
    ├── test_veiculos_api.py              # 4 testes de API de Veículos
    └── test_bot_integration.py           # 2 testes de integração do Bot com banco real
```

---

## 🚀 Como Executar Localmente e Validar

```powershell
# 1. Ativar o ambiente virtual
.\.venv\Scripts\Activate.ps1

# 2. Executar a suíte completa de testes unitários e de integração (66 testes)
python -m pytest tests/ -v -k "not simulate_whatsapp_traffic"

# 3. Executar o teste de tráfego e simulação E2E de estresse (40 turnos de conversa real)
python tests/simulate_whatsapp_traffic.py

# 4. Iniciar a API com Uvicorn para recepção de webhooks do WhatsApp
uvicorn src.interfaces.app:app --host 0.0.0.0 --port 8080 --reload
```
