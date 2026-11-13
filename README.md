# Empório da Música: customer service agent

A prototype of a text-message customer service agent (WhatsApp style) for Empório da Música, a musical
instrument store in Campo Grande, Brazil. The assistant is called Lia.

She looks up the catalog, orders and promotions, applies the rules from the store policy manual (returns,
warranty, payment, shipping, opening hours), turns down off-topic requests and hands over to the staff when
needed.

The agent talks to customers in Brazilian Portuguese, since that is the store's audience. Everything else in
the repository is documented in English.

```
Customer: Quanto custa o Takamine GD20?                      (How much is the Takamine GD20?)
Lia:      O Takamine GD20 está em estoque. R$ 2.199,00, ou R$ 2.089,05 no PIX (5% de desconto),
          ou até 12x de R$ 183,25 sem juros. ...
```

The example conversations, including the tools called on each turn, are in [`examples/`](examples/).

## Contents

1. [How to run](#1-how-to-run)
2. [Structure](#2-structure)
3. [Technical decisions](#3-technical-decisions)
4. [Data issues](#4-data-issues)
5. [Assumptions](#5-assumptions)
6. [Evaluation](#6-evaluation)
7. [Limitations and next steps](#7-limitations-and-next-steps)
8. [Use of coding assistants](#8-use-of-coding-assistants)

## 1. How to run

You need Python 3.10+ and an LLM API key. The default is Anthropic's Claude Haiku 5.5 (key at
https://console.anthropic.com/settings/keys). If you don't have a paid key, you can use Gemini, which has a
free tier (https://aistudio.google.com/apikey), or any OpenAI-compatible API. Both options are in `.env.example`.

```bash
git clone https://github.com/joaovguimaraes/emporio-agente.git
cd emporio-agente
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # fill in ANTHROPIC_API_KEY (or set up option 2)
```

Terminal chat:

```bash
python -m emporio                         # or just: emporio
python -m emporio --debug                 # shows the tools being called
python -m emporio --sessao joao           # resumes the "joao" conversation
python -m emporio --sessao joao --nova    # clears the history and starts over
```

Inside the chat, `/nova` restarts and `/sair` exits.

Other commands:

| Command | What it does | Needs a key |
|---|---|---|
| `pytest` | 39 tests (business rules, tools, manual search, agent loop with a fake LLM) | no |
| `python scripts/relatorio_qualidade_dados.py` | lists the inconsistencies found in the CSVs and the manual | no |
| `python scripts/gerar_exemplos.py` | regenerates the 5 conversations in `examples/` | yes |
| `python scripts/avaliar_roteamento.py` | checks whether the agent picks the right tool (15 cases) | yes |

`.env` variables:

| Variable | Use |
|---|---|
| `LLM_PROVIDER` | `anthropic` (default) or `openai` (any compatible API) |
| `ANTHROPIC_API_KEY` | Anthropic key |
| `ANTHROPIC_WORKSPACE_ID` | only if the key is not tied to a workspace |
| `LLM_MODEL` | default `claude-haiku-5-5`; with Gemini, `gemini-3.5-flash` |
| `LLM_EFFORT` | Anthropic only, reasoning level (default `low`) |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_TEMPERATURE` | only for the `openai` provider |
| `EMPORIO_AGORA` | optional, pins the current date/time (see section 5) |

## 2. Structure

```
src/emporio/
  config.py     settings, provider, Campo Grande time zone, reference date
  data.py       CSV loading and data quality checks
  policies.py   PDF -> sections -> BM25 search, plus the manual corrections
  business.py   fixed rules: price, PIX, installments, opening hours, return deadlines
  tools.py      the 8 tools and the schemas sent to the model
  prompts.py    system prompt (persona and rules)
  agent.py      function calling loop (Anthropic and OpenAI-compatible backends)
  history.py    conversation history in JSON
  cli.py        terminal interface
scripts/        examples, routing evaluation, data report
tests/          offline tests
examples/       generated conversations
```

How a message flows: the CLI calls `Agent.respond()`, which sends the conversation to the model along with
the 8 tools. When the model asks for a tool, it runs in Python on the cleaned data and the result goes back to
the model, until it answers in text (at most 6 rounds).

Tools (names in Portuguese, like the rest of the domain): `buscar_produtos` (search products),
`detalhar_produto` (product details), `listar_categorias` (list categories), `consultar_pedidos` (look up
orders), `avaliar_troca_devolucao` (check return/exchange eligibility), `consultar_politicas` (search the
policy manual), `status_da_loja` (is the store open now) and `encaminhar_para_equipe` (open a ticket for the
staff).

## 3. Technical decisions

### Function calling, with separate tools for data and for policies

The core of the challenge is knowing when to look up data and when to look up policies. Function calling
makes that choice explicit: `--debug` and the examples show which tool was used for each question.

- Products, orders and promotions are served by tools that filter the CSVs with pandas. I didn't use RAG for
  this: "guitars up to R$ 1,000" is a filter, and semantic search over CSV rows would be imprecise.
- The policy manual is searched (details below).
- I ruled out a SQL agent because it would have free access to the customer table. With closed tools, the
  model simply has no way to list customers or see an order without the identity check, whatever the user
  writes.
- I didn't use LangChain/LangGraph. This is a single agent with 8 tools, and a ~150-line loop of my own
  (`agent.py`) is easier to read and test. If the flow grew (several agents, human approval in the middle),
  I would reconsider.

### Math and dates in Python

Promo price, PIX price, installments, return and warranty deadlines and opening hours are computed in
`business.py` and reach the model already formatted (`"R$ 2.089,05"`, `"até 12x de R$ 183,25"`,
`"Recebido há 35 dias (limite: 7)"`). Language models often get arithmetic and dates wrong, and the manual
itself says a wrong price can count as misleading advertising. These rules are covered by tests.

Rules implemented: 5% PIX discount that doesn't stack with promotions; installments following section 3.1
(up to 3x with a R$ 50 minimum per installment, 4 to 6x with R$ 80, 7 to 12x with R$ 100); 7-day right of
withdrawal from delivery; 30-day exchange for defects from purchase; 90-day legal warranty; national and
Mato Grosso do Sul holidays plus Campo Grande's anniversary.

### Manual search: sections + BM25

1. `pypdf`'s default extraction returns one word per line for this PDF. I used layout mode.
2. Each numbered subsection of the manual becomes one chunk (25 in total), instead of fixed N-token chunks
   that would cut rules in half.
3. Search is BM25 with accents stripped and simple prefix stemming (`devolver` and `devolução` both become
   `devol`). For 25 short chunks this works well and avoids depending on an embeddings API.
4. Questions with two subjects ("address and Saturday hours?") made one subject drop out of the top 3. I
   added a coverage step: if a query term doesn't appear in any selected chunk, the best chunk for that term
   is added too.
5. The manual contradicts itself in places. My decisions live in `policies.ERRATA` and are attached to the
   retrieved chunk, so the answer is always the same.

### Model

I use Claude Haiku 5.5 through Anthropic's official SDK. I picked it for cost (it is Anthropic's cheapest
model) and latency, 2 to 3 seconds per answer. Customer service is high volume and simple questions, and the
hard part (math and rules) is already in Python. Reasoning effort is set to `low`, and the fixed part of the
prompt is cached; date and time go in a separate block so they don't invalidate the cache.

I started with Gemini because of its free tier, thinking about whoever would run the project. Running it for
real, I hit three problems: `gemini-2.5-flash` is no longer available to new keys; Gemini 3 requires each
tool call's `thought_signature` to be sent back on the next turn, otherwise it returns a 400 error; and on
the free tier answers took minutes. I fixed the compatibility issue and kept Gemini as an option
(`LLM_PROVIDER=openai`), but Claude became the default.

### Prompt

The system prompt (`prompts.py`) has the persona and tone taken from the manual (informal but professional),
a guide on which tool to use for each kind of question, the rules that can't be broken (no prices or
deadlines without checking, no promising inactive promotions, instruments only, LGPD, no rule changes on
request) and the current date/time, so the agent knows whether the store is open.

Tool descriptions also steer the model, and some results carry instructions at the right moment (a late
order comes with an `alerta` field suggesting a ticket for the staff).

### Interface and history

I built a CLI because the focus is the agent. Since everything goes through `Agent.respond()`, wiring it to
an API or a WhatsApp webhook would take little code. History is saved as JSON per session, because on
WhatsApp customers come back later and expect continuity. The window keeps the last 40 messages and always
starts at a customer message, so a tool call is never separated from its result. Tickets for the staff go to
`runtime/tickets.jsonl`.

### Privacy (LGPD, Brazil's data protection law)

- Order data is only returned with the order number plus the e-mail or phone on file.
- The error message is the same for "order doesn't exist" and "order belongs to someone else", so nobody
  can probe for orders or registered e-mails.
- No tool returns a customer's e-mail or phone, only the first name.
- Customers 11 and 43 share an e-mail address; in that case the agent asks for the order number or phone.

## 4. Data issues

Before writing code, I went through every file looking for inconsistencies.
`scripts/relatorio_qualidade_dados.py` lists what was found.

### CSVs

| Issue | Example | What I did |
|---|---|---|
| Description mentions another brand | 139 "Yamaha drum kit" described as Pearl; 142 "Korg" described as an 88-key Roland (specs say 61); 6 products in total | Detect the mismatching brand, hide the description and use only name and specs |
| Discontinued but in stock | 136, with 13 units | Treated as unavailable |
| Active with zero stock | 96, Giannini GF-3D | Shown as sold out, with similar alternatives |
| "Coming soon" with stock and already sold | 130, in orders 3 and 14 | Not offered; existing orders can still be looked up |
| Categories with no products | woodwinds, brass, orchestral strings | The agent only mentions categories with items for sale |
| Inactive and duplicated promotions | 21 of 25 inactive; 93, 96 and 99 have two each | Only `is_active=1` counts; if two are active, the larger one |
| Order total differs from the sum of items | order 3 (3,450 vs 3,498), order 20 (1,400 vs 1,488) | Show the recorded total |
| Installments below the minimum | orders 9, 16 and 18 | Reported only, it's historical data |
| Shared e-mail | customers 11 and 43 | Ask for order number or phone |
| Stalled orders | order 8 past its delivery date; orders pending for weeks | The tool flags the delay and the agent offers to escalate |
| Empty dates | orders not yet shipped | pandas produced `NaT`, which passes as a date and broke the lookup; converted to `None` |
| Zip contents | `__MACOSX`, `.DS_Store`, PDF name in decomposed Unicode (NFD), spaces in file names | Copied to `data/` with simple names |

### Policy manual

| Issue | Decision |
|---|---|
| Two phone numbers: 3341-4444 (cover and company table) and 3321-4500 (section 7) | Use 3341-4444, which appears in both company data sources |
| E-mail with an accent in the table (`empóriodamusica`) | Use the version without the accent, from the cover |
| Table says R$ 100 minimum installment, section 3.1 has tiers | Section 3.1 wins, it's more specific |
| "Up to 3x with no minimum (except below R$ 50)" | Read as a R$ 50 minimum |
| Free shipping "above R$ 500" and a fee "below R$ 500"; exactly R$ 500 has no rule | Free shipping, the reading most favorable to the customer (Consumer Code, art. 47) |
| 4.2 sends the customer to the manufacturer after 30 days, but 8.1 gives a 90-day legal warranty | The store is still responsible within the 90 days; the agent offers to escalate |
| "Black Friday discounts on the whole catalog" | Only promotions active in the system count, as section 7.3 requires |
| "Suggest partner stores", but there is no list | The agent doesn't make up store names |

## 5. Assumptions

- Reference date: orders go from October 2025 to March 2026. In normal use the agent uses the real date. For
  the examples and the evaluation I pinned `EMPORIO_AGORA=2026-03-24T10:30` (right after the latest order) so
  deadlines make sense and results are reproducible.
- There is no delivery date in the data. For delivered orders, I use `estimated_delivery`.
- I treat every order in the system as an online purchase, so the right of withdrawal applies.
- Status: `pending` is awaiting payment, `confirmed` is paid and being prepared, `shipped` is on its way,
  `delivered` was delivered, `cancelled` was cancelled.
- Holidays: national, Mato Grosso do Sul state and August 26 (Campo Grande's anniversary). Optional holidays
  are not included.
- The agent informs and escalates. It doesn't change orders, give discounts or complete exchanges.

## 6. Evaluation

- `pytest`: 39 offline tests. They cover price, PIX and installments, catalog filters, accessory detection
  ("cordas de violão" (guitar strings) is an accessory, "violão 7 cordas" (7-string guitar) is not), identity
  checks, return deadlines, opening hours and holidays, manual search, and the agent loop with a fake model
  on both providers.
- `scripts/avaliar_roteamento.py`: 15 questions with the expected tool and the forbidden ones. With
  `claude-haiku-5-5`, 15 out of 15 correct, including the cases where the right move is to call no tool at
  all (order question without identification, off-topic question, request for a customer list, greeting).
- Example conversations (in Portuguese, with an English summary at the top of each file):

| # | Conversation | What it shows |
|---|---|---|
| 1 | [Guitars up to R$ 1,000](examples/01_catalogo_violoes_ate_1000.md) | price filter, sold-out item with alternatives, an option that only fits the budget when paying by PIX |
| 2 | [Price, Black Friday, address and hours](examples/02_preco_promocao_e_informacoes_da_loja.md) | data and policies in the same conversation, declining a promotion that doesn't exist |
| 3 | [Return and defect](examples/03_devolucao_por_arrependimento_e_defeito.md) | non-trivial case: identity check, expired withdrawal period, legal warranty and a ticket |
| 4 | [Late order](examples/04_pedido_atrasado_reclamacao.md) | tracking, detected delay, complaint logged |
| 5 | [Off-topic and privacy](examples/05_fora_do_escopo_acessorios_e_privacidade.md) | accessories, unrelated request and an attempt to get another customer's data |

I reviewed the conversations by comparing each answer with what the tools returned. The first round had four
problems, which I fixed and covered with tests:

| Problem | Cause | Fix |
|---|---|---|
| Order 7 (Shelby SU-21S Soprano, a ukulele) was called a saxophone, and the agent contradicted the customer | the order tool only returned the item name | items now include their category |
| The agent said the store sells woodwinds and orchestral strings | it used the category list from the database, which includes empty ones | the prompt only gets categories with products for sale |
| "I couldn't find the address" for a question about address and hours | search only returned opening-hours chunks | coverage step in the search |
| Ticket opened without asking, then a second one just to add the phone number | no rule for escalation | confirm first, one ticket per issue |

## 7. Limitations and next steps

Limitations:

- The final answer is still model-generated text. It could restate a value the tool got right in the wrong
  way. Today this is mitigated with pre-formatted values and a prompt rule.
- Checking identity by e-mail or phone is too weak for production.
- Product search is keyword-based. Subjective requests ("a warmer sound") depend on the model turning them
  into filters.
- Shipping to other cities isn't calculated; the manual has no price table.
- Local JSON history doesn't work with more than one instance.
- The evaluation is small: 15 routing cases and 5 conversations.

With more time:

1. Check the final answer before sending: every R$ value and order number must appear in that turn's tool
   results.
2. A bigger evaluation, around 100 conversations with an LLM judge for faithfulness, tone and scope, run on
   every prompt or model change.
3. WhatsApp integration, identifying the customer by phone number (better verification) and keeping history
   in a database.
4. Tickets in a real helpdesk, with the conversation handed over to a person.
5. Hybrid catalog search (BM25 + embeddings) if the catalog grows.
6. Send the issues in section 4 back to the store so they get fixed at the source.
7. Monitoring of cost, latency and escalation rate.

## 8. Use of coding assistants

I used Claude Code (Anthropic's coding agent, Claude Opus model) throughout the challenge, with access to the
repository and the terminal. I used it like someone I hand tasks to: I set the goal and what "done" means,
make the product and cost decisions, and check the result. Most of the code was written by it.

How the work went:

1. Before any code, I asked for a full read of the brief, the manual and the CSVs looking for traps. That
   became section 4 and shaped the architecture.
2. We went over the options (RAG, SQL agent, function calling, frameworks) and I chose closed tools with the
   math in Python, because the biggest risk here is making up a price or leaking customer data.
3. I built it in parts (data, manual, rules, tools, agent), testing each with the real data and committing.
   The Git history shows that order.
4. I started with Gemini for the free tier. When I ran it for real and saw the problems described in
   section 3, I switched to Claude and asked for the cheapest model.
5. I didn't publish the examples as they came out: I compared the conversations with the data and fixed what
   was wrong (section 6).

Assistant mistakes that came up along the way:

| Mistake | How it showed up |
|---|---|
| Empty dates became `NaT` and broke the order lookup | testing the tool with orders not yet shipped |
| A commit went in with a failing test (a `\| tail` hid `pytest`'s exit code) | checking the output; after that, commits only with all tests passing |
| Build files (`egg-info`) committed | `git status` before the next commit |
| Gemini 3's `thought_signature` was being dropped (400 error on the second turn) | testing against the real API |
| Ukulele called a saxophone, empty categories mentioned, address not found, duplicate ticket | reviewing the conversations (section 6) |

The assistant sped up the code, tests and documentation a lot. Deciding what to prioritize, which
architecture and model to use, and when the result was good enough stayed with me.
