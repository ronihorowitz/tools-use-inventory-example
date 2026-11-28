# Inventory agent: tool-use exercise

A LangGraph inventory agent backed by Supabase, rebuilt stage by stage from the four prompts in `student-instructions.pdf` (also in `prompts/`). The agent will interpret supplier messages, explain demand anomalies, consolidate purchases and prioritize stockouts under a budget. Python does the arithmetic; PostgreSQL validates purchasing constraints. Orders, supplier communication and deliveries are all simulated.

**Status:** stage 1 (database) is done. Stages 2–4 (Python tools, agent and notebook, scenarios) are not built yet.

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

## Setup

- Python 3.12. `pip install -r requirements.lock.txt` installs the pinned checkpoint library, needed only to regenerate migration 004: `python scripts/build_checkpoint_migration.py`.
- Copy `.env.example` for later stages; never commit real values.
