"""Validate agent-proposed edits to operations.py.

Used by /douzonebot:troubleshoot 단계 4 when the agent extends the bounded
operations surface. Enforces a small allow-list so the agent can compose
existing primitives but cannot reach into raw CDP or run arbitrary code.

Usage:
    python -m src.validate_extension <proposed_file> [--baseline <baseline_file>]

Exits 0 if OK to apply, 1 with reasons on stderr otherwise.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import List, Set

# Modules the agent may import inside operations.py.
ALLOWED_IMPORTS_STDLIB = {
    "os", "sys", "logging", "typing", "dataclasses", "pathlib",
    "re", "json", "asyncio", "datetime", "enum", "functools",
    "itertools", "collections", "math",
}
ALLOWED_IMPORTS_LOCAL = {
    "src.automation", "src.models", "src.operations",
    ".automation", ".models", ".operations",
    "automation", "models", "operations",
}

# Outright-forbidden calls — escape hatches that bypass the bounded surface.
FORBIDDEN_AUTO_CALLS = {
    "page.evaluate", "page.evaluate_handle",
    "cdp.send", "cdp.session",
}
FORBIDDEN_FREE_CALLS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "os.system", "os.popen",
}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}

# Allowed Playwright Locator methods (chained off auto.page.locator(...)).
LOCATOR_METHODS = {
    "first", "last", "nth", "fill", "click", "is_visible", "is_enabled",
    "wait_for", "count", "text_content", "input_value", "press",
    "set_input_files", "check", "uncheck", "hover", "all", "locator",
}


def _resolve_attr_chain(node: ast.Attribute) -> List[str]:
    """auto.page.locator -> ['auto', 'page', 'locator']. [] if not Name-rooted."""
    parts: List[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return list(reversed(parts))
    return []


def extract_auto_methods(tree: ast.AST) -> Set[str]:
    """Collect every auto.<x> and auto.page.<x> method invoked in the tree."""
    methods: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _resolve_attr_chain(node)
        if not chain or chain[0] != "auto":
            continue
        if len(chain) == 2:
            methods.add(chain[1])
        elif len(chain) >= 3 and chain[1] == "page":
            methods.add(f"page.{chain[2]}")
    return methods


def check_imports(tree: ast.AST) -> List[str]:
    errors: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in ALLOWED_IMPORTS_STDLIB:
                    continue
                if alias.name in ALLOWED_IMPORTS_LOCAL:
                    continue
                errors.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0:
                key = "." + mod if mod else "."
                if key not in ALLOWED_IMPORTS_LOCAL:
                    errors.append(f"forbidden relative import: from {key}")
                continue
            top = mod.split(".")[0]
            if top in ALLOWED_IMPORTS_STDLIB or mod in ALLOWED_IMPORTS_LOCAL:
                continue
            errors.append(f"forbidden import: from {mod}")
    return errors


def check_forbidden_calls(tree: ast.AST) -> List[str]:
    errors: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Builtin name calls: eval(), exec(), ...
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_BUILTINS:
            errors.append(f"forbidden builtin call: {node.func.id}")
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        chain = _resolve_attr_chain(node.func)
        if not chain:
            continue
        # auto.page.evaluate / auto.cdp.send
        if chain[0] == "auto" and len(chain) >= 3:
            tail = ".".join(chain[1:])
            if tail in FORBIDDEN_AUTO_CALLS:
                errors.append(f"forbidden CDP escape: auto.{tail}")
        # subprocess.run, os.system, ...
        if len(chain) == 2 and ".".join(chain) in FORBIDDEN_FREE_CALLS:
            errors.append(f"forbidden call: {'.'.join(chain)}")
    return errors


def check_async_signatures(tree: ast.AST) -> List[str]:
    """Public functions must be async + take `auto` first (except `connect`)."""
    errors: List[str] = []
    if not isinstance(tree, ast.Module):
        return errors
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            errors.append(f"public sync function not allowed: {node.name}")
        elif isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
            if node.name == "connect":
                continue
            args = node.args.args
            if not args or args[0].arg != "auto":
                errors.append(f"async function {node.name} must take `auto` as first arg")
            if not (ast.get_docstring(node) or "").strip():
                errors.append(f"async function {node.name} missing docstring")
    return errors


def check_auto_methods_against_baseline(
    proposed: Set[str], baseline: Set[str]
) -> List[str]:
    new = proposed - baseline
    unknown: Set[str] = set()
    for m in new:
        if m.startswith("page."):
            sub = m.split(".", 1)[1]
            if sub in LOCATOR_METHODS:
                continue
        unknown.add(m)
    if not unknown:
        return []
    return [f"unknown auto.* method (not used in baseline): {m}" for m in sorted(unknown)]


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("proposed", help="Path to proposed operations.py")
    ap.add_argument(
        "--baseline",
        help="Path to current operations.py (defaults to a sibling 'operations.py')",
        default=None,
    )
    args = ap.parse_args(argv)

    proposed_path = Path(args.proposed)
    if args.baseline:
        baseline_path = Path(args.baseline)
    else:
        baseline_path = proposed_path.parent / "operations.py"

    if not proposed_path.exists():
        print(f"REJECT: proposed file not found: {proposed_path}", file=sys.stderr)
        return 1
    if not baseline_path.exists():
        print(f"REJECT: baseline file not found: {baseline_path}", file=sys.stderr)
        return 1

    try:
        proposed_tree = ast.parse(proposed_path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        print(f"REJECT: syntax error in proposed file: {e}", file=sys.stderr)
        return 1
    try:
        baseline_tree = ast.parse(baseline_path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        print(f"REJECT: baseline failed to parse: {e}", file=sys.stderr)
        return 1

    errors: List[str] = []
    errors += check_imports(proposed_tree)
    errors += check_forbidden_calls(proposed_tree)
    errors += check_async_signatures(proposed_tree)
    errors += check_auto_methods_against_baseline(
        extract_auto_methods(proposed_tree),
        extract_auto_methods(baseline_tree),
    )

    if errors:
        print("REJECT:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
