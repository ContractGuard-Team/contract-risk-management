"""Keep the deliberately disconnected container components independently importable."""
import ast
from pathlib import Path
import unittest


class ComponentBoundaryTests(unittest.TestCase):
    def test_components_do_not_import_disconnected_neighbors(self):
        root = Path(__file__).resolve().parents[2]
        boundaries = {
            "workers": {"integrations", "input_processing", "agents", "orchestration"},
            "input_processing": {"workers", "agents", "orchestration"},
            "orchestration": {"input_processing", "workers"},
        }
        for folder, forbidden in boundaries.items():
            for path in (root / folder).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [item.name for item in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        modules = [node.module]
                    for module in modules:
                        with self.subTest(path=str(path.relative_to(root)), module=module):
                            self.assertNotIn(module.split(".")[0], forbidden)
