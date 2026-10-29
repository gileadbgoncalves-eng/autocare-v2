# 📍 PONTO DE PARADA TOTAL - AUTOQUER / AUTOCARE V2 (MERGE ATÔMICO E ROTEAMENTO ESTRITO AI-FIRST)

**Data do Checkpoint:** 29/10/2026 - 19:05  
**Versão:** Autoquer / AutoCare V2.11.0 (Atomic Deep Merge on Upsert, Strict AI-First Tool Routing, Elimination of Local Regex Heuristics, Multi-Tool Calls & Debounce Buffer)  
**Status do Projeto:** 100% Funcional, 89/89 Testes Pytest Aprovados e 35/35 Turnos da Bateria do Caos Aprovados (100% Pass Rate).  

---

## 📊 Matriz Consolidada de Homologação dos Testes (89/89 PASS ✅)

| Suíte de Testes | Arquivo / Comando | Abrangência / Escopo | Resultado |
| :--- | :--- | :--- | :---: |
| **Refatoração AI-First & Function Calling** | `tests/test_ai_first_refactoring.py` | `upsert_draft` (Deep Merge Atômico), `confirm_and_save_draft`, `update_last_saved_service` (Anti-Alucinação), `get_service_history` | **7/7 PASS (100%) ✅** |
| **Debounce, Patch Atômico & Hodômetro** | `tests/test_webhook_debounce_and_patch.py` | Debounce anti-atropelo (2.0s), patch atômico de ficha técnica, atualização silenciosa de odômetro e Multi-Tool Call | **7/7 PASS (100%) ✅** |
| **Bateria do Caos (E2E 35 Turnos)** | `tests/test_fire_chaos_suite.py` | 35 turnos sequenciais ponta a ponta com clock virtual, sem interrupção de fluxo nem corrupção | **1/1 (35 turnos) PASS (100%) ✅** |
| **Guardrails de Escopo e Tokens** | `tests/test_guardrails_and_token_limits.py` | Bloqueio pré-LLM custo zero, teto de tokens, pruning de histórico | **20/20 PASS (100%) ✅** |
| **TTL, Fuso UTC-3 e Escape Global** | `tests/test_ttl_and_global_escape.py` | Expiração de rascunho (30 min), injeção temporal BRT, soft delete (5 min) | **13/13 PASS (100%) ✅** |
| **Intenções Mistas Encadeadas** | `tests/test_mixed_chained_intents.py` | Sincronização Pydantic ActionDraft, persistência dupla (Manutenção + Lembrete) | **3/3 PASS (100%) ✅** |
| **Auto-Enriquecimento no Onboarding** | `tests/test_auto_enrichment_onboarding.py` | Inferência técnica (motor, câmbio, direção) e onboarding proativo | **5/5 PASS (100%) ✅** |
| **Refatoração dos Testes de Estresse** | `tests/test_stress_refactor_fixes.py` | Múltiplas ações, classificador semântico NLP e conflito Placa vs Modelo | **5/5 PASS (100%) ✅** |
| **Correções Críticas de QA** | `tests/test_qa_refactor_fixes.py` | Extração temporal/local, alerta enxuto de revisão, check-up de garagem e fuzzy matching | **7/7 PASS (100%) ✅** |
| **Motor de Notificações & Scheduler** | `tests/test_notification_batch_scheduler.py` | Batching de alertas, isolamento FK, bypass de agendamento e horário personalizado | **3/3 PASS (100%) ✅** |
| **APIs REST de Negócio** | `tests/test_proprietarios_manutencoes_api.py` e `tests/test_veiculos_api.py` | Endpoints FastAPI, validações Pydantic, placas Mercosul/antigas e regras de integridade | **8/8 PASS (100%) ✅** |
| **Integração Bot-Banco** | `tests/test_bot_integration.py` | Resposta do assistente conectada ao banco SQLite/Postgres com sanitização de placas | **2/2 PASS (100%) ✅** |
| **Suíte Global Automatizada (Pytest)** | `pytest -q -k "not simulate_whatsapp_traffic"` | Cobertura total regressiva e preventiva de todas as camadas do sistema | **89/89 PASS (100%) ✅** |

---

## 🚀 Implementações da Versão V2.11.0 (Refatoração Cirúrgica: Merge Atômico & Roteamento Estrito)

### 1. Reescrita Cirúrgica de `_upsert_maintenance_draft` (Deep Merge Implacável)
- **Merge de Itens:** Mesclagem (`append` cumulativo) dos itens do `existing_draft` com os novos `itens`/`components` fornecidos pela IA, com desduplicação insensível a maiúsculas/minúsculas.
- **Preservação Blindada (KM e Valor):** Quando a nova chamada da IA não enviar valor numérico para `km`, o valor exato de `existing_draft.get("km")` é incondicionalmente preservado. Para `valor`, preserva o valor anterior ou soma custos cumulativos caso o usuário cite termos de adição (*"também"*, *"mais"*).
- **Preservação Blindada de Data:** Removidas checagens de palavras circunstanciais como `"hoje" in clean_text.lower()`. Se a IA não extrair uma nova data explícita, a data original do rascunho é **estritamente preservada**.
- **Preservação de Oficina:** A oficina original é preservada inalterada caso a nova mensagem não especifique outro estabelecimento.

### 2. Blindagem do Roteamento em `_handle_pending_confirmation_state`
- **Sub-bloco 4.1 (Edição do Último Serviço):** Curto-circuito mandatário (`return early`). Se `is_edit_service` for `True` ou se a ferramenta `update_last_saved_service` for acionada, a execução retorna o resultado de `_handle_edit_last_saved_service` imediatamente, sem risco de cair em interceptadores de criação de rascunho.
- **Sub-bloco 4.2 (Upsert de Rascunho):** Eliminadas regexes e inferências manuais locais (`is_cumulative_text`). A decisão de acionar `_upsert_maintenance_draft` é delegada estritamente às chamadas de Tool (`has_upsert_tc`) ou intenções da IA.

### 3. Remoção de Lógicas Antigas (Delete-First)
- **Eliminação de Heurísticas de Fallback:** O fallback offline não tenta mais emular rascunhos cumulativos ou mesclagens de rascunhos via regex quando a IA estiver offline, emitindo aviso limpo caso haja falha externa.
- **Prompt Mestre (`src/ai/prompts.py`):** Adicionada a **Regra 4 (Separação Estrita de Tools)**, definindo expressamente que mensagens adicionais a um rascunho aberto devem acionar exclusivamente `upsert_draft`, enquanto comandos de correção do serviço já salvo devem acionar exclusivamente `update_last_saved_service`.
- **Motor Offline no GeminiClient (`src/ai/client.py`):** `_build_offline_fallback` alinhado à arquitetura de Tools estruturadas, simulando com precisão o retorno de `ToolCallAction` para execução em ambientes de teste sem quota externa.

---

## 📂 Caminho do Arquivo Local
O projeto está localizado no diretório:
`C:\Users\WINDOWS\.gemini\antigravity\scratch\autocare-v2`
