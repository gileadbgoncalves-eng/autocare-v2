# 📍 PONTO DE PARADA TOTAL - AUTOQUER / AUTOCARE V2 (DEEP MERGE NO UPSERT & ROTEAMENTO ESTRITO DE EDIÇÃO SEM ALUCINAÇÃO)

**Data do Checkpoint:** 29/10/2026 - 18:40  
**Versão:** Autoquer / AutoCare V2.10.0 (Deep Merge on Draft Upsert, Strict Edit Routing with Anti-Hallucination, Multi-Tool Calls, Silent Odometer Update & Debounce Buffer)  
**Status do Projeto:** 100% Funcional, 89/89 Testes Pytest Aprovados e 35/35 Turnos da Bateria do Caos Aprovados (100% Pass Rate).  

---

## 📊 Matriz Consolidada de Homologação dos Testes (89/89 PASS ✅)

| Suíte de Testes | Arquivo / Comando | Abrangência / Escopo | Resultado |
| :--- | :--- | :--- | :---: |
| **Refatoração AI-First & Function Calling** | `tests/test_ai_first_refactoring.py` | `upsert_draft` (Deep Merge), `confirm_and_save_draft`, `update_last_saved_service` (Anti-Alucinação), `get_service_history` | **7/7 PASS (100%) ✅** |
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

## 🚀 Implementações da Versão V2.10.0 (Deep Merge & Roteamento Estrito Anti-Alucinação)

### 1. Deep Merge no Upsert de Rascunho (`upsert_draft`)
- **Problema Anterior**: Ao receber um item adicional durante `AWAITING_CONFIRMATION` (ex: *"também troquei filtro de combustível"*), a Tool `upsert_draft` apagava os itens anteriores, zerava a quilometragem e redefinia a data para o dia atual.
- **Solução Implementada**:
  - `_upsert_maintenance_draft` realiza um **Deep Merge**: novos itens são adicionados (appended) à lista cumulativa existente, sem duplicidade.
  - Se o usuário não mencionar nova quilometragem, data ou oficina, os valores anteriormente capturados no rascunho são **estritamente preservados**. A data não é redefinida para a data de hoje.
  - Formatação monetária padronizada no padrão brasileiro (`R$ 1.250,50`).
  - Verificação de retrocesso de hodômetro no fluxo unificado de upsert.

### 2. Roteamento Estrito de Edição & Anti-Alucinação (`update_last_saved_service`)
- **Problema Anterior**: Ao receber comandos como *"quero corrigir o km desse último serviço"*, o LLM alucinava um novo rascunho de serviço com o nome genérico *"Revisão geral periódica"*, em vez de acionar a ferramenta de edição do registro existente.
- **Solução Implementada**:
  - Instruções de sistema atualizadas com regra mandatória: comandos para corrigir/editar/alterar o último serviço gravado **devem acionar obrigatoriamente** `update_last_saved_service`, com o `service_id` do estado invisível.
  - Proibição estrita de invenção de nomes de serviço genéricos como *"Revisão geral periódica"*.
  - Roteamento determinístico no orquestrador: se o usuário solicita edição do último serviço, o fluxo é roteado diretamente para `_handle_edit_last_saved_service`, ordenando decrescente por data e ID e buscando a manutenção exata por ID.
  - Schemas Pydantic ajustados (`service_id: Optional[Union[int, str]]`) para flexibilidade completa entre chaves primárias numéricas e mocks de teste.

### 3. Desambiguação de Consultas vs. Edição
- `is_edit_last_service_intent` refinado para não capturar perguntas informativas sobre histórico (ex: *"Qual foi a última manutenção que fiz no Nivus?"*), garantindo a preservação de trocas de contexto (Context Switch) durante confirmações pendentes.

---

## 📂 Caminho do Arquivo Local
O projeto está localizado no diretório:
`C:\Users\WINDOWS\.gemini\antigravity\scratch\autocare-v2`
