"""Generate inventory_agent.ipynb from the repository's Python sources: cleared, no keys, no downloads.

The notebook contains the complete application code inline (calculations, Supabase tools, agent
instructions, graph, persistence, scenario prompts and checks), so a fresh Colab runtime needs no
project files. Run this after changing any of the .py sources; tests/test_notebook.py fails otherwise.
"""
import ast
from pathlib import Path

import nbformat as nbf

ROOT = Path(__file__).resolve().parents[1]
LOCAL_MODULES = {'inventory_tools', 'inventory_agent', 'scenario_checks'}
md, code = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell


def inline_source(filename: str, title: str) -> list:
    """One cell for the module's imports, then one readable cell per top-level definition."""
    source = (ROOT / filename).read_text(encoding='utf-8')
    lines = source.splitlines()
    cells, imports, definitions = [md(title)], [], []
    previous_was_constant = False
    for node in ast.parse(source).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # the module docstring is replaced by the section title
        if isinstance(node, ast.ImportFrom) and node.module in LOCAL_MODULES:
            continue  # those definitions are already in earlier cells
        start = (node.decorator_list[0].lineno if getattr(node, 'decorator_list', None) else node.lineno) - 1
        text = '\n'.join(lines[start:node.end_lineno])
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(text)
            continue
        is_constant = isinstance(node, (ast.Assign, ast.AnnAssign))
        if is_constant and previous_was_constant:
            definitions[-1] += '\n' + text  # consecutive constants share one cell
        else:
            definitions.append(text)
        previous_was_constant = is_constant
    for text in ['\n'.join(imports), *definitions]:
        cell = code(text)
        cell.metadata['inventory_source'] = filename
        cells.append(cell)
    return cells


pins = dict(line.split('==', 1) for line in (ROOT / 'requirements.lock.txt').read_text(encoding='utf-8').splitlines()
            if '==' in line)
packages = ['supabase', 'langchain-core', 'langchain-openai', 'langgraph', 'langgraph-prebuilt', 'langgraph-checkpoint',
            'langgraph-checkpoint-postgres', 'psycopg', 'psycopg-binary', 'langsmith']
install = '%pip install -q ' + ' '.join(f'{name}=={pins[name]}' for name in packages)

nb = nbf.v4.new_notebook()
nb.cells = [
    md('''# Inventory agent: tool use with LangGraph and Supabase

An explicit LangGraph agent manages a fictional office-supply inventory: it interprets supplier messages,
explains demand anomalies, consolidates purchases and prioritizes stockouts under a budget.
**Python does the arithmetic; PostgreSQL validates every purchase.** Orders, supplier messages and deliveries
are all simulated. There is no human approval step.

**Before running:** apply the migrations in `supabase/migrations` to **your own** Supabase project, in order, and
add the Colab Secrets listed in section 2. This notebook makes real model calls, which your OpenAI account pays for.
The simulation starts on March 1, 2026; the real date does not matter.'''),
    md('## 1. Install the pinned packages\nThe complete application code is in the cells below; '
       'nothing is downloaded from the repository.'),
    code(install),
    md('## 2. Load secrets\nAdd `OPENAI_API_KEY`, `OPENAI_MODEL`, `SUPABASE_URL`, `SUPABASE_KEY` (your secret / '
       'service-role key, not the publishable key) and `SUPABASE_DB_URI` (Connect → **session pooler**, port 5432, '
       'SSL; URL-encode special characters in the password) to Colab Secrets and allow this notebook to use them. '
       'Choose a model your account can use with tool calling; no model is hardcoded.'),
    code('''import os
from google.colab import userdata

for key in ["OPENAI_API_KEY", "OPENAI_MODEL", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_DB_URI"]:
    value = userdata.get(key)
    if not value:
        raise ValueError(f"Missing Colab Secret: {key}")
    os.environ[key] = value
print("Settings loaded without displaying values.")'''),
    md('''### Optional: trace runs in LangSmith
Add a `LANGSMITH_API_KEY` Colab Secret from your own LangSmith account to send every agent turn to LangSmith
as a trace, in the project `inventory-agent` (an EU workspace also needs `LANGSMITH_ENDPOINT` =
`https://eu.api.smith.langchain.com`). LangGraph traces itself once these variables are set. Traces contain the
prompts, tool results and supplier messages; all data here is fictional. Without the secret, tracing stays off.'''),
    code('''def optional_secret(key):
    try:
        return userdata.get(key)
    except (userdata.SecretNotFoundError, userdata.NotebookAccessError):
        return None

langsmith_key = optional_secret("LANGSMITH_API_KEY")
if langsmith_key:
    os.environ["LANGSMITH_API_KEY"] = langsmith_key
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = "inventory-agent"
    endpoint = optional_secret("LANGSMITH_ENDPOINT")
    if endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = endpoint
    print("LangSmith tracing on: project inventory-agent.")
else:
    os.environ["LANGSMITH_TRACING"] = "false"
    print("LangSmith tracing off (no LANGSMITH_API_KEY secret).")'''),
    md('## 3. The full application\nRun these cells in order. The matching `.py` files in the repository are the '
       'same code, kept for local tests; this notebook does not import them.'),
    *inline_source('inventory_tools.py', '### 3.1 Calculations and database tools'),
    *inline_source('inventory_agent.py', '### 3.2 Agent instructions, graph and durable conversations'),
    *inline_source('scenario_checks.py', '### 3.3 Scenario prompts and acceptance checks'),
    md('## 4. Connect and validate\nChecks the database functions, the model (access and tool calling) and the '
       'checkpoint schema version before any scenario runs. The connection stays open for the whole session.'),
    code('''from contextlib import ExitStack
from langchain_openai import ChatOpenAI

if "SESSION" in globals():
    SESSION.close()
service = InventoryService.from_env()
print(service.list_inventory()["scenario"], "scenario is loaded.")
model = ChatOpenAI(model=os.environ["OPENAI_MODEL"], timeout=MODEL_TIMEOUT, max_retries=MODEL_RETRIES)
print("Model says:", validate_model(model, service))
SESSION = ExitStack()
graph = SESSION.enter_context(open_agent(service, model=model))
print("Database, model and checkpoint schema are ready.")'''),
    md('''## 5. Select and reset a scenario
Choose `disruption`, `anomaly`, `consolidation`, `budget` or `injection`.
**The reset deletes this demo's orders and decisions and restores the fixtures** (it does not touch other
schemas or old conversations). Every reset needs a new thread ID, which this cell creates.'''),
    code('''SCENARIO = "disruption"
assert SCENARIO in SCENARIOS
state = service.reset_scenario(SCENARIO)
THREAD_ID = new_thread_id(SCENARIO)
print({"scenario": SCENARIO, "thread_id": THREAD_ID, "simulated_date": state["as_of"]})'''),
    md('## 6. Run the agent\nEvery agent message, tool call and tool result is shown as it happens.'),
    code('run_turn(graph, THREAD_ID, SCENARIOS[SCENARIO]);'),
    md('## 7. Check the outcome\nThese checks read the database, not the model\'s words. A failed check is a result to '
       'record and discuss, not something to hide.'),
    code('print(check_outcome(service, SCENARIO))'),
    md('## 8. Follow up in the same conversation\nThe same thread ID continues the persisted conversation. Do not reset '
       'first.'),
    code('run_turn(graph, THREAD_ID, "Explain the evidence and costs behind your previous decision. '
         'Do not create any additional orders.");'),
    md('## 9. Advance the simulated clock\nAn instructor action, never a model tool: moves the clock and receives every '
       'sent order that has arrived. Running it again for the same date changes nothing.'),
    code('''print(service.receive_due("2026-03-08"))
print(service.list_inventory())'''),
    md('''## 10. Reconnect and confirm persistence
Closing and reopening the connection keeps the conversation. To test a full restart: note the thread ID,
restart the runtime, run sections 1–4, set `THREAD_ID` to the noted value, and run this cell (no reset).'''),
    code('''SESSION.close()
SESSION = ExitStack()
graph = SESSION.enter_context(open_agent(service, model=model))
messages = graph.get_state({"configurable": {"thread_id": THREAD_ID}}).values.get("messages", [])
print(f"{len(messages)} messages restored for {THREAD_ID}.")'''),
    md('## 11. Close the connection'),
    code('SESSION.close()'),
]
nb.metadata = {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
               'language_info': {'name': 'python', 'version': '3.12'}}
for i, cell in enumerate(nb.cells):
    cell.id = f'inventory-{i:02d}'
nbf.write(nb, ROOT / 'inventory_agent.ipynb')
