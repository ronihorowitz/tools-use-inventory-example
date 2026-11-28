# Project instructions

## Database

Whenever a database is needed for this repository, use the existing Supabase project **agents-course**.

- Project ID: `nstddqhiwmouttudbcvp`
- Region: `eu-central-1` (Frankfurt)
- Dashboard: https://supabase.com/dashboard/project/nstddqhiwmouttudbcvp

Do not create or use another database project unless the user explicitly requests it.

The instructor implementation uses this project. The student exercise explicitly asks students to use their own Supabase projects; use placeholders in student-facing configuration and never distribute instructor credentials.

## Development

- Apply migrations with Supabase `apply_migration` and keep every applied file in `supabase/migrations`, synchronized with the remote migration history (see `supabase/migrations/README.md`).
- Never edit an applied migration; add a new one.
- Keep dependencies pinned in `requirements.lock.txt`. Run `python -m pytest -q`.
- Integration tests require an explicitly configured, empty `TEST_DATABASE_URL`; never point them at the instructor project. GitHub Actions provides one.
- `tests/database_assertions.sql` runs inside one transaction that rolls back; it must leave the database unchanged.
- Keep notebook outputs cleared. Include the full application code inline in the notebook; never download project files or import repository modules at notebook runtime. Regenerate it with `python scripts/build_notebook.py` when the Python sources change, and validate with `python scripts/validate_notebook.py`.
- No baseline comparison, approval interrupts, real supplier messaging or multi-agent architecture.
- The model will never get arbitrary SQL. Reset and clock advancement stay out of the model's tool list.

## Build stages

The repository is rebuilt stage by stage from the prompts in `prompts/` (the same text as `student-instructions.pdf`). Stage 1 (`prompts/01-database.txt`) is the database; stage 2 (`prompts/02-tools.txt`) is `inventory_tools.py`. `create_purchase_order` builds the request key itself; the model never passes one. Stage 3 (`prompts/03-agent.txt`) is `inventory_agent.py`, `scenario_checks.py` and the generated `inventory_agent.ipynb`.
