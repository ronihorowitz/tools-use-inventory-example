"""The notebook carries the whole application inline and stays in sync with the .py sources."""
import builtins
import runpy

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
import nbformat

from conftest import ROOT, FakeClient, snapshot_data
from scripts.validate_notebook import LOCAL_MODULES, validate

NOTEBOOK = ROOT / 'inventory_agent.ipynb'


def test_notebook_structure_is_valid():
    assert validate(NOTEBOOK) > 0


def test_generated_notebook_is_current(monkeypatch):
    captured = []
    monkeypatch.setattr(nbformat, 'write', lambda notebook, path: captured.append(notebook))
    runpy.run_path(str(ROOT / 'scripts' / 'build_notebook.py'))
    assert captured and captured[0] == nbformat.read(NOTEBOOK, as_version=4), \
        'Run python scripts/build_notebook.py after changing the .py sources'


def test_install_cell_uses_the_locked_versions():
    pins = dict(l.split('==', 1) for l in (ROOT / 'requirements.lock.txt').read_text(encoding='utf-8').splitlines() if '==' in l)
    cells = [c.source for c in nbformat.read(NOTEBOOK, as_version=4).cells if c.source.startswith('%pip install')]
    assert len(cells) == 1
    for requirement in cells[0].split()[3:]:
        name, version = requirement.split('==')
        assert pins[name] == version, name


def test_inline_code_runs_without_project_files(monkeypatch):
    """Execute only the embedded source cells, with imports of the repository's modules blocked."""
    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name.split('.')[0] in LOCAL_MODULES:
            raise ImportError('Project modules are not available in a fresh Colab runtime')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', isolated_import)
    namespace, sources = {'__name__': 'notebook'}, set()
    for cell in nbformat.read(NOTEBOOK, as_version=4).cells:
        if cell.cell_type == 'code' and (filename := cell.metadata.get('inventory_source')):
            sources.add(filename)
            exec(compile(cell.source, f'<notebook:{filename}>', 'exec'), namespace)
    assert sources == {f'{m}.py' for m in LOCAL_MODULES}

    service = namespace['InventoryService'](FakeClient(snapshot_data('disruption')))
    assert len(namespace['build_tools'](service)) == 13
    assert namespace['eoq'](1000, 10, 2) == 100

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages):
            if messages[-1].type == 'human':
                return AIMessage(content='', tool_calls=[{'name': 'list_inventory', 'args': {}, 'id': 'inline'}])
            return AIMessage(content='Read with the inline tools.')

    graph = namespace['create_graph'](service, InMemorySaver(), Model())
    state = namespace['run_turn'](graph, namespace['new_thread_id']('disruption'), 'Read.', emit=lambda _: None)
    assert [m.type for m in state['messages']] == ['human', 'ai', 'tool', 'ai']
    assert set(namespace['SCENARIOS']) == {'disruption', 'anomaly', 'consolidation', 'budget', 'injection'}
    assert hasattr(namespace['open_agent'], '__wrapped__')  # still a context manager


def test_notebook_has_the_required_cells():
    text = '\n'.join(c.source for c in nbformat.read(NOTEBOOK, as_version=4).cells)
    for needed in ('OPENAI_API_KEY', 'OPENAI_MODEL', 'SUPABASE_URL', 'SUPABASE_KEY', 'SUPABASE_DB_URI',
                   'validate_model(model, service)', 'service.reset_scenario(SCENARIO)', 'new_thread_id(SCENARIO)',
                   'check_outcome(service, SCENARIO)', 'service.receive_due(', 'SESSION.close()'):
        assert needed in text, needed
