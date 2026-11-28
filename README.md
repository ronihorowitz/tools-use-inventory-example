# Inventory agent: tool-use exercise

A LangGraph inventory agent backed by Supabase, rebuilt stage by stage from the four prompts in `student-instructions.pdf` (also in `prompts/`). The agent will interpret supplier messages, explain demand anomalies, consolidate purchases and prioritize stockouts under a budget. Python does the arithmetic; PostgreSQL validates purchasing constraints. Orders, supplier communication and deliveries are all simulated.

**Status:** stage 1 (database) and stage 2 (Python tools) are done. Stages 3–4 (agent and notebook, scenarios) are not built yet.

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

`tests/database_assertions.sql` runs 54 checks inside one transaction that rolls back:
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

## Setup

- Python 3.12: `pip install -r requirements.lock.txt`, then `python -m pytest -q`.
- To regenerate migration 004: `python scripts/build_checkpoint_migration.py`.
- Copy `.env.example` for the notebook stage; never commit real values.
