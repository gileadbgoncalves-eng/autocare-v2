# 🚗 AutoCare V2 — Plataforma Inteligente de Gestão Automotiva

O **AutoCare V2** é um assistente conversacional inteligente de manutenção preventiva, livro de bordo digital e monitoramento veicular projetado para operar com alta disponibilidade, baixa latência e segurança de dados via WhatsApp.

A versão **V2** foi integralmente reconstruída do zero, adotando **Clean Architecture**, chamadas assíncronas de ponta a ponta, validação estrita com **Pydantic v2**, persistência relacional com **SQLAlchemy 2.0 Async** e inteligência artificial generativa com **Google Gemini** em modo de saída estruturada (*Structured Output*).

---

## 🏛️ Arquitetura do Sistema (Clean Architecture)

```
autocare-v2/
├── src/
│   ├── config/              # Configurações centralizadas (pydantic-settings)
│   │   └── settings.py      # Gestão de .env e normalizador de dialeto DB
│   │
│   ├── domain/              # Núcleo de Domínio e Regras de Negócio Puras
│   │   ├── entities.py      # Modelos Pydantic v2 (User, Vehicle, Maintenance, Reminder)
│   │   └── enums.py         # Enums canônicos (IntentType, ActionState, Provider)
│   │
│   ├── ai/                  # Cérebro Semântico e Conexão de IA
│   │   ├── client.py        # Cliente assíncrono Gemini com Structured Outputs e timeout
│   │   └── prompts.py       # Prompt mestre e persona de especialista automotivo
│   │
│   ├── application/         # Casos de Uso e Orquestração
│   │   └── services/
│   │       └── conversation.py # Máquina de estados, Fast-Path 14ms e Human-in-the-Loop
│   │
│   ├── infrastructure/      # Adaptadores e Persistência de Dados
│   │   └── database/
│   │       ├── session.py   # Engine assíncrono e PRAGMA foreign_keys = ON
│   │       ├── models.py    # Modelos relacionais ORM mapeados
│   │       └── repositories.py # Padrão Repository (User, Vehicle, Maintenance)
│   │
│   └── interfaces/          # Portas de Entrada e Comunicação Externa
│       ├── api/
│       │   └── webhook.py   # Roteador multi-provedor (Evolution API, Z-API, JSON)
│       └── app.py           # Aplicação FastAPI com lifespan e middlewares
│
├── run_v2_tests.py          # Bateria completa de testes automatizados ponta a ponta
├── test_bot.py              # Cliente interativo de terminal para simulação em tempo real
├── setup_project.py         # Script autônomo gerador de todo o ecossistema V2
├── requirements.txt         # Dependências de produção atualizadas
├── .env.example             # Modelo documentado de variáveis de ambiente
└── PONTO_DE_PARADA_V2.md    # Documento de auditoria técnica do checkpoint
```

---

## ✨ Principais Diferenciais e Funcionalidades

1. **Human-in-the-Loop Obrigatório:**
   - Ações que alteram dados patrimoniais ou financeiros (cadastrar veículo, registrar gastos, criar lembretes ou zerar garagem) geram um rascunho visual formatado e aguardam a confirmação explícita do condutor antes de persistir no banco.

2. **Fast-Path Conversacional de 14ms:**
   - Mensagens de cortesia e encerramento (*"ok"*, *"valeu"*, *"show"*, *"obrigado"*) são interceptadas por um analisador léxico determinístico ultrarrápido, respondendo instantaneamente em ~14ms com zero consumo de tokens e sem dependência de rede externa.

3. **Structured Outputs com Google Gemini:**
   - Integração com o modelo `gemini-3.8-flash` (e fallback `gemini-flash-latest`) forçando respostas estritamente validadas via JSON Schema, eliminando 100% de alucinações e formatações quebradas.
   - Higienização e inlining automático de esquemas Pydantic v2 para compatibilidade com o padrão REST da Google AI.

4. **Persistência Assíncrona e Multi-Dialeto:**
   - Suporta SQLite (`sqlite+aiosqlite`) para desenvolvimento e testes locais com chaves estrangeiras ativas (`PRAGMA foreign_keys = ON`).
   - Suporta PostgreSQL gerenciado (`postgresql+asyncpg`) para produção sem alteração de código.

5. **Webhook Multi-Provedor de WhatsApp:**
   - Normalizador unificado capaz de processar payloads da **Evolution API**, **Z-API** ou payloads JSON genéricos de forma transparente.

---

## 🚀 Como Executar o Projeto

### 1. Instalação das Dependências
Recomenda-se a criação de um ambiente virtual Python:
```bash
python -m venv venv
venv\Scripts\activate   # No Windows
# source venv/bin/activate # No Linux/macOS

pip install -r requirements.txt
```

### 2. Configuração de Variáveis de Ambiente
Copie o modelo de ambiente e insira sua chave do Google Gemini:
```bash
cp .env.example .env
```
Edite o arquivo `.env`:
```ini
ENVIRONMENT=development
PORT=8080
LOG_LEVEL=info
DATABASE_URL=sqlite+aiosqlite:///autocare.db
GEMINI_API_KEY=sua_chave_do_google_gemini_aqui
```

### 3. Execução dos Testes Automatizados
Valide a integridade de todas as camadas do sistema com a suíte integrada:
```bash
python run_v2_tests.py
```
*(Executa testes de domínio, banco de dados, cliente de IA, máquina de estados e rotas HTTP em ~1.2s).*

### 4. Simulação Interativa no Terminal
Converse diretamente com o bot no terminal com banco de dados real persistido:
```bash
python test_bot.py
```

### 5. Iniciar o Servidor HTTP / Webhook
Suba o servidor FastAPI para receber mensagens reais de WhatsApp:
```bash
uvicorn src.interfaces.app:app --host 0.0.0.0 --port 8080 --reload
```
Acesse a documentação Swagger interativa em: `http://localhost:8080/docs`

---

## 🔒 Segurança e Boas Práticas
* Arquitetura desacoplada sem referências circulares ou estado global compartilhado.
* Injeção de dependências via sessões assíncronas do SQLAlchemy.
* Tratamento de exceções e timeout inteligente de 5s para chamadas de IA com respostas amigáveis de contingência.

---

## 📄 Licença
Projeto proprietário AutoCare. Todos os direitos reservados.
