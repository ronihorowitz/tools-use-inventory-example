"""Structural checks for inventory_agent.ipynb, without Colab, credentials or a model."""
import ast
from pathlib import Path
import re

import nbformat

ROOT = Path(__file__).resolve().parents[1]
LOCAL_MODULES = {'inventory_tools', 'inventory_agent', 'scenario_checks'}
SECRET = re.compile(r'eyJ[A-Za-z0-9_-]{20,}|sb_secret_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{20,}|postgres(ql)?://[^\s"]*:[^\s"@]{6,}@')


def validate(path=ROOT / 'inventory_agent.ipynb') -> int:
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    for index, cell in enumerate(notebook.cells):
        assert not SECRET.search(cell.source), f'Cell {index} looks like it contains a credential'
        if cell.cell_type != 'code':
            continue
        assert cell.execution_count is None and not cell.outputs, f'Cell {index}: clear notebook outputs'
        source = '\n'.join(line for line in cell.source.splitlines() if not line.startswith('%'))
        for node in ast.walk(ast.parse(source, filename=f'cell-{index}')):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in LOCAL_MODULES | {'urllib.request'}, f'Cell {index} imports {node.module}'
            elif isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & LOCAL_MODULES, f'Cell {index} imports a project file'
        assert 'raw.githubusercontent.com' not in source, f'Cell {index} downloads project files'
    return len(notebook.cells)


if __name__ == '__main__':
    print(f'Validated {validate()} notebook cells; no outputs, no project imports, no credentials.')
