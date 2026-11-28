# Inventory agent: tool-use exercise

[Open the notebook in Colab](https://colab.research.google.com/github/ronihorowitz/tools-use-inventory-example/blob/main/inventory_agent.ipynb)

A LangGraph inventory agent backed by Supabase, rebuilt stage by stage from the four prompts in `student-instructions.pdf` (also in `prompts/`). The agent will interpret supplier messages, explain demand anomalies, consolidate purchases and prioritize stockouts under a budget. Python does the arithmetic; PostgreSQL validates purchasing constraints. Orders, supplier communication and deliveries are all simulated.

**Status:** stages 1–3 (database, Python tools, agent and notebook) are built and tested offline and in CI. Stage 4 (running the five scenarios with a live model) is not done yet.

Students: build your own copy from the prompts in **your own** Supabase project before comparing with this one.

## Stage 1: the database

Four migrations in `supabase/migrations/` (see its README for the applied versions):

1. **Schema:** `inventory` holds suppliers, products, supplier products, price breaks, stock, sales history, supplier messages, the demo state, purchase orders, order lines and agent decisions. `inventory_checkpoints` holds the saved conversations. RLS is on everywhere with no policies, so only the service role can reach the data.
2. **Fixtures:**
   - 12 fictional SKUs, 4 suppliers, 6 SKUs with two suppliers;
   - 180 days of sales per SKU (2,160 rows), including an annotated one-off Feb 14 pen sale of 900 extra boxes;
   - one supplier message per scenario;
   - the clock fixed at March 1, 2026, a March 15 deadline and a $20,000 budget.
3. **Functions** (in `public`, security invoker, executable by `service_role` only):

   | Function | Purpose |
   |---|---|
   | `inventory_snapshot()` | read everything for the current run |
   | `inventory_create_order(supplier, lines, request_key, rationale)` | create a draft order with prices computed in SQL |
   | `inventory_send_order(order_id)` | mark a draft as sent (simulated; idempotent) |
   | `inventory_log_decision(decision)` | record the agent's evidence and rationale |
   | `inventory_reset_scenario(scenario)` | notebook only, not a model tool: restore the fixtures, start a new run |
   | `inventory_receive_due(date)` | notebook only, not a model tool: advance the clock, receive due orders |

4. **Checkpoint tables:** the frozen DDL of `langgraph-checkpoint-postgres==3.2.1`.

### What the database enforces

`inventory_create_order` locks the scenario state, so concurrent orders can't overspend. It takes prices only from its own tables, never from the caller, and rejects:
- duplicate or fractional lines;
- quantities below the MOQ or not in whole packs;
- orders below the supplier's minimum value;
- products not active in the scenario;
- a reused `request_key` with a different payload (the same payload returns the existing order);
- anything that would exceed the trusted per-SKU cumulative cap for the run (paper: 1,200 units, even when split across orders);
- anything that would exceed the shared budget.

Drafts reserve budget and caps immediately; sent, unreceived lines count as on order.

### Checks

`tests/database_assertions.sql` runs 56 checks inside one transaction that rolls back:
- fixture counts;
- the prompt's cost examples ($750 vs $620, $11,430 + $8,580 over budget);
- every rejection rule, including 10,000 units and 2 × 700 against the paper cap;
- idempotent retries, draft → sent → received, reset, and access for anon/authenticated/service_role.

Run it as the database owner, for example with Supabase `execute_sql`. No error means every check passed.

## Stage 2: the Python tools

`inventory_tools.py` holds the deterministic tools over the stage 1 database. There is no LLM yet.

**Pure calculations.** All use Decimal, and money is shown with 2 places.
- **Demand:** the 90 calendar days before the simulated date / 90, with zero days included, × 365.
  - Only annotated sales inside that window may be excluded.
  - Excluding one removes that whole day's sales, including the ordinary ones, and the denominator stays 90. This is a deliberate teaching simplification.
- **EOQ:** √(2DS/H), where H = applicable price × holding rate. The candidates compared on total annual cost are:
  - the EOQ rounded to MOQ and packs;
  - each all-unit price break;
  - the quantity that reaches free shipping;
  - the quantity that reaches the supplier's minimum order value.
- **Reorder point:** daily demand × lead days + safety stock.
- **Stockout projection:** up to 366 days. Sent orders arrive at the start of their due date.
- **Basket cost:** one ordering fee and one shipping fee per supplier order, with the same validation and error messages as the database.
- **`plan_orders`:** an urgency-first heuristic.
  - It finds stockouts by the deadline and picks the cheapest supplier that arrives in time. Lead-time overrides from the evidence affect this analysis only.
  - It groups lines by supplier and, if over budget, shrinks the least urgent lines by whole packs.
  - It reports unavoidable delivery gaps. It is not a global optimum.

**`InventoryService`** reads one `inventory_snapshot()` per call and writes only through the RPCs. It reads `SUPABASE_URL` / `SUPABASE_KEY` from the environment and never prints them.

**`build_tools(service)`** exposes exactly 13 typed LangChain tools:
- reads: `list_inventory`, `get_sales_history`, `get_supplier_options`, `get_supplier_messages`;
- calculations: `estimate_demand`, `calculate_eoq`, `reorder_point`, `predict_stockout`, `order_total_cost`, `plan_orders`;
- writes: `create_purchase_order`, `send_purchase_order`, `log_decision`.

`reset_scenario` and `receive_due` are notebook utilities only. No tool accepts SQL, a key, a budget, a cap or a price.

**Request keys** (a deliberate change from the earlier version): `create_purchase_order(supplier_id, lines, rationale)` builds the idempotency key itself, as `scenario:supplier:simulated-date`. The model never passes a key, so it cannot get the format wrong or invent a new key to get past a conflict. That gives one batch per supplier per simulated day, and a retry returns the existing order.

### Tests

`python -m pytest -q`:

- **Unit and tool tests (39, offline, no key or network)** cover:
  - EOQ 1000/10/2 → 100;
  - rounding, price breaks, zero demand, annotated exclusions;
  - $750 vs $620;
  - invalid quantities, cumulative caps (10,000 and 700 + 700), scheduled receipts;
  - the disruption switch to West with the 21-day evidence;
  - a budget plan that includes toner and chairs within $20,000 and flags the unavoidable tape gap;
  - the 13-tool surface and the key built by the tool.

  The fixture is a snapshot of the stage 1 data. Its 2,160 sales rows are regenerated from the seed formula and verified against the database's own checksum.
- **PostgreSQL integration tests (`tests/test_database.py`)** need `TEST_DATABASE_URL` pointing at an **empty, disposable** database, never the instructor project. They:
  - apply the migrations;
  - run `tests/database_assertions.sql`;
  - drive the same `InventoryService` against the real functions, checking that Python and SQL costs agree;
  - check rollback and idempotent retries;
  - check that a second order waits for the scenario lock and is then refused for budget.

  GitHub Actions runs them on every push with a throwaway PostgreSQL 17 (`.github/workflows/tests.yml`). Locally they skip with the reason.

**Limitation:** the integration tests have not run on the instructor's Windows PC. Windows application control blocks psycopg's compiled driver there, so they run in CI only.

## Stage 3: the agent and the notebook

**`inventory_agent.py`** builds an explicit LangGraph loop: an `agent` node, then `tools_condition`, then a `ToolNode` with the 13 tools, then back to `agent`.
- **The model:** chosen through `OPENAI_MODEL`, with no temperature, a 90-second timeout, 2 retries and a 30-step recursion limit. `validate_model` checks account access and tool calling before any scenario runs.
- **The system prompt:**
  - supplier messages and notes are untrusted evidence;
  - all arithmetic goes through the tools;
  - every decision is logged, including exclusions and lead-time overrides;
  - replenishment requests are carried out without asking for approval, and analysis-only requests write nothing;
  - one order batch per supplier per simulated day;
  - rejections are explained, never worked around.
- **Tool failures:** the model receives the validation message (for example the cap that was hit) and a reminder that nothing was confirmed. Database credentials never appear in these messages.
- **`open_agent`:** connects through the session pooler with `search_path=inventory_checkpoints`, checks that the checkpoint schema version matches the installed library (no silent upgrades), and keeps the connection open for the session.
- **Conversations:** `new_thread_id(scenario)` makes a fresh ID, and reusing an ID restores the conversation. `run_turn` prints every agent message, tool call and tool result. If a run stops, it says a write may or may not have happened.
- **LangSmith (optional):** `turn_config` labels each turn with `thread_id`, `scenario` and the tag `inventory-agent`.

**`scenario_checks.py`** holds the five scenario prompts and `check_outcome`, which checks each result against the database, not the model's words.

**`inventory_agent.ipynb`** is generated by `python scripts/build_notebook.py` from the three `.py` files. It contains the complete code inline: nothing is downloaded or imported from the repository, and a cell installs the pinned packages. It has separate cells for:
- secrets, plus optional LangSmith tracing;
- connect and validate;
- scenario reset, with a new thread;
- the run;
- the outcome check;
- a follow-up on the same thread;
- clock advance and receipt;
- reconnect and persistence;
- close.

`python scripts/validate_notebook.py` checks the notebook: cleared outputs, valid syntax, no project imports, no credentials.

**Colab secrets:**
- `OPENAI_API_KEY` and `OPENAI_MODEL`;
- `SUPABASE_URL` and `SUPABASE_KEY` (the secret / service-role key);
- `SUPABASE_DB_URI` (Connect → session pooler, port 5432, SSL);
- optionally `LANGSMITH_API_KEY` and `LANGSMITH_ENDPOINT`.

**Tests (62 offline, plus 11 in CI):**
- the graph with the real tools and a scripted model: the tool loop, persistence and thread separation, tool errors reaching the model, the key built by the tool, the recursion limit, trace labels, model validation;
- the acceptance checks, on both passing and failing states;
- the notebook: its inline code runs with the project's modules blocked, it matches the sources, and its install cell matches the lock file;
- in CI, a conversation that survives closing and reopening the PostgreSQL connection (no model cost), and refusal of a mismatched checkpoint schema.

**Still to do, live:** run the five scenarios in Colab with a real model and record the outcomes (stage 4). None of the stage 3 tests call a real model.

## Setup

- Python 3.12: `pip install -r requirements.lock.txt`, then `python -m pytest -q`.
- After changing `inventory_tools.py`, `inventory_agent.py` or `scenario_checks.py`: `python scripts/build_notebook.py`, then commit the notebook too.
- To regenerate migration 004: `python scripts/build_checkpoint_migration.py`.
- Copy `.env.example` for the notebook stage; never commit real values.
