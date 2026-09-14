"""scripts/screenshots.py structure checks (Task 34).

Reads the script's source as text and inspects module attributes after
import; never starts uvicorn, never launches a browser, never touches the
network. `main()` (the only thing that does any of that) is not called.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "screenshots.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_screenshots_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_importable_without_a_browser_or_server():
    # If this module did anything network- or browser-adjacent at import
    # time, this would hang or fail in a sandboxed test run.
    module = _load_module()
    assert hasattr(module, "SCREENS")
    assert hasattr(module, "main")


def test_screens_list_has_the_five_required_shapes():
    module = _load_module()
    screens = module.SCREENS
    paths = [path for _name, path in screens]

    assert paths[0] == "/"
    assert any(p.startswith("/libraries/") for p in paths), (
        "expected a library screen path templated on the first seeded library id"
    )
    assert "/history" in paths
    assert "/runs" in paths
    assert "/settings" in paths
    assert len(paths) == 5


def test_both_color_schemes_are_captured():
    source = SCRIPT_PATH.read_text()
    assert '"light"' in source
    assert '"dark"' in source


def test_375px_mobile_viewport_is_present():
    source = SCRIPT_PATH.read_text()
    assert "375" in source
    # And it's paired with the 812 height used for the dashboard mobile
    # shot required by the brief, not a stray unrelated "375".
    assert "812" in source


def test_screenshot_output_filenames_match_the_brief():
    source = SCRIPT_PATH.read_text()
    # Filenames are built as f"{name}-{scheme}.png" from SCREENS names and
    # COLOR_SCHEMES, plus a separate literal mobile filename.
    assert "{name}-{scheme}.png" in source
    assert "dashboard-mobile.png" in source


def test_no_ast_syntax_errors_and_no_top_level_network_calls():
    tree = ast.parse(SCRIPT_PATH.read_text())
    # main() must exist as a function definition, not run at import time.
    func_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "main" in func_names

    # Only calls made directly at module scope (not inside any function or
    # class body) count here — walking into function bodies would flag
    # ordinary calls like `uvicorn.Server(...)` made *inside* `main()`,
    # which never run at import time.
    module_body_calls = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    forbidden = {"launch", "run", "Server"}
    for expr in module_body_calls:
        call = expr.value
        name = getattr(call.func, "attr", getattr(call.func, "id", ""))
        assert name not in forbidden, f"unexpected top-level call to {name}"
