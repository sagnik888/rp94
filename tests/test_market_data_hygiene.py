import ast
from pathlib import Path


def test_market_data_has_no_silent_exception_pass_handlers():
    source = Path(__file__).resolve().parents[1] / "data" / "market_data.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    silent_handlers = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and len(node.body) == 1:
            if isinstance(node.body[0], ast.Pass):
                silent_handlers.append(
                    (node.lineno, ast.unparse(node.type) if node.type else "bare")
                )

    assert silent_handlers == []
