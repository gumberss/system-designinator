import ast
from pathlib import Path


def test_domain_has_no_infrastructure_imports() -> None:
    root = Path(__file__).parents[1] / "src" / "mapinator"
    domain = ast.parse((root / "domain.py").read_text())
    prohibited = {"fastapi", "sqlalchemy", "redis", "httpx", "mapinator.postgres", "mapinator.api"}
    imports = [
        node.module for node in ast.walk(domain) if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert not prohibited.intersection(imports)


def test_production_does_not_use_test_double() -> None:
    root = Path(__file__).parents[1] / "src" / "mapinator"
    for path in root.glob("*.py"):
        assert "tests.memory" not in path.read_text()
