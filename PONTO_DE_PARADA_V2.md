# 📍 PONTO DE PARADA TOTAL - AUTOCARE V2 (RED TEAMING & IA HOMOLOGADA)

**Data do Checkpoint:** 19/10/2026 - 22:45  
**Status do Projeto:** 100% Funcional, Blindado contra Adversários, Assíncrono e Auditado.  
**Taxa de Sucesso dos Testes:**
- 100% na Suíte de Testes Unitários e Integrados (`run_v2_tests.py` - 6 camadas).
- 100% na Bateria de Testes Adversariais Red Teaming (`ai_tester.py` - 9/9 cenários aprovados).

---

## 🎯 Resumo das Conquistas Recentes & Blindagem de Segurança

Após testes exaustivos simulando motoristas reais no WhatsApp, implementamos defesas rígidas de negócio, mecânica e segurança contra ataques adversariais (Red Teaming):

1. **Eliminação do *Ghost Draft State* e Confirmação Falsa Positiva:**
   - O Fast-Path afirmativo (`sim`, `salva`, etc.) agora só é disparado se a mensagem for curta (<= 4 palavras) e sem novas entidades.
   - Mensagens longas contendo gírias ou dados novos (ex: *"salva aí meu cvic 2020 a plaka é ABC1234"*) são enviadas para a análise do Gemini, evitando confirmações acidentais de rascunhos antigos.

2. **Entrega Direta de Alertas Técnicos (`UNKNOWN` Passthrough):**
   - Respostas do Gemini com intenção `UNKNOWN` (como alertas de incompatibilidade mecânica ou rejeição de injeção) são entregues diretamente ao motorista e limpam qualquer rascunho incompatível pendente.

3. **Incompatibilidade Mecânica Impediente (Mecânico Confiável):**
   - Bloqueio imediato de trocas de radiador, água e aditivo em motos refrigeradas a ar (ex: CG 160 Titan, Biz, Pop, Bros, Factor, Fazer).
   - Bloqueio de correia dentada, ar-condicionado, filtro de cabine e direção hidráulica em motocicletas.

4. **Anti-Alucinação de Oficina (`workshop_name` Estritamente Null):**
   - O sistema nunca mais preenche o nome da oficina repetindo componentes revisados (ex: "troquei na oficina pastilhas e amortecedores" mantém `workshop_name: null`).

5. **Reconciliação e Retificação Caótica de Rascunho com "Não":**
   - Mensagens contendo "Não" seguido de retificações de dados (ex: *"Não, o valor correto foi 250 e troca de pastilha"*) não cancelam o rascunho; atualizam os campos e pedem confirmação.

6. **Teto de Garagem do Plano Gratuito (Máximo de 3 Veículos):**
   - A tentativa de cadastrar o 4º veículo é recusada cordialmente na `reply_message` com `UNKNOWN` e `is_critical_action=False`.

7. **Defesa Ativa contra Injeção de Prompt / Jailbreak:**
   - Tentativas de evasão como `ADMIN_OVERRIDE`, `HackerGPT` ou *"esqueça todas as regras"* são repelidas com foco exclusivo em consultoria automotiva preventiva.

8. **Estabilidade de Cota da API (Zero Erros 429 / 503):**
   - Configurado `gemini-flash-lite-latest` como modelo primário e `gemini-3.5-flash-lite` como contingência, garantindo altíssima velocidade (<2s) e zero sobrecarga.

---

## 📂 Arquitetura do Repositório

- [`src/config/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/config/): Configurações Pydantic Settings com modelos velozes e limites de rate limit.
- [`src/domain/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/domain/): Entidades de Domínio com validações de odômetro `ge=0`, custos positivos `ge=0.0` e sanitização de placas.
- [`src/ai/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/ai/): Prompt mestre calibrado com 10 seções de regras de negócio, persona mecânica e Structured Outputs.
- [`src/application/services/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/application/services/): Máquina de estados conversacional imune a vazamento de contexto órfão.
- [`src/infrastructure/database/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/infrastructure/database/): Persistência SQLAlchemy 2.0 Async, proteção contra regressão de km no odômetro e cascade delete total em reset.
- [`src/interfaces/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/src/interfaces/): API FastAPI de alta performance com normalizador de webhooks (Evolution, Z-API e Direto).
- [`v1_ponto_de_parada_total/`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/v1_ponto_de_parada_total/): Pasta isolada e preservada contendo o código legado anterior da V1.

---

## 🛠️ Ferramentas de Auditoria e Testes

1. **[`ai_tester.py`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/ai_tester.py):** Simulador adversarial com agente Red Team que gera mensagens dinâmicas imprevisíveis via IA e auditor com Structured Output.
2. **[`audit_test_report.json`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/audit_test_report.json):** Relatório oficial da homologação adversarial (9 cenários, 100% aprovados).
3. **[`run_v2_tests.py`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/run_v2_tests.py):** Bateria de 6 suítes locais com execução em 1.25s.
4. **[`test_bot.py`](file:///C:/Users/gbduarte/Downloads/autocare/autocare/test_bot.py):** Terminal interativo de testes manuais.

---

## 🚀 Como Executar

```bash
# Rodar suíte completa de integração
python run_v2_tests.py

# Rodar auditoria adversarial Red Teaming (modo ASGI direto)
python ai_tester.py --in-process

# Iniciar servidor Webhook
uvicorn src.interfaces.app:app --host 0.0.0.0 --port 8080
```
