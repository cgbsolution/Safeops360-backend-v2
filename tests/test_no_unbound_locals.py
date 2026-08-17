"""Static guard: no function reads a local before it is assigned.

Written after `get_audit` shipped with exactly that bug and took the audit
detail page down for every audit in the product.

`R = AuditCheckpointResponse` sat beside the query that was last to need it.
When the department-replication block (docs/cams/20) was inserted ABOVE that
line, the five reads of `R` inside it became reads of an unbound local —
because Python makes a name assigned anywhere in a function local to the
WHOLE function, not from the assignment onwards. Every call raised

    UnboundLocalError: cannot access local variable 'R'

and the Next.js detail page rendered that 500 as a bare "404 This page could
not be found", so the one thing the screen said was the one thing that was not
true: the audit existed.

**Why a static test rather than a functional one.** `get_audit` needs a live
database and this suite has none, so nothing here could ever have called it —
which is precisely how a 100%-reproducible crash reached production. A parser
needs neither a database nor a fixture, and it checks every function in `app/`
rather than the handful someone remembered to cover.

Ruff would catch this with F821, but only for names it cannot resolve at all;
a name that IS assigned later in the function is not undefined, so this
specific ordering bug passes lint. Hence the explicit check.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

# Nodes that introduce their OWN scope. A comprehension target is not a local of
# the enclosing function in Python 3, so `[x.id for x in rows]` must not be read
# as "x used before assignment" — the false positive that makes a naive version
# of this check useless.
NESTED_SCOPES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def _own_scope_names(fn: ast.AST):
    """Name nodes belonging to this function's own scope, nested scopes excluded."""
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop(0)
        if isinstance(node, NESTED_SCOPES):
            continue
        if isinstance(node, ast.Name):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _bound_names(fn) -> set[str]:
    names = {
        a.arg for a in
        list(fn.args.args) + list(fn.args.kwonlyargs) + list(fn.args.posonlyargs)
    }
    if fn.args.vararg:
        names.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        names.add(fn.args.kwarg.arg)
    for n in ast.walk(fn):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
    return names


def _offenders(fn) -> list[tuple[str, int, list[int]]]:
    skip = _bound_names(fn)
    stores: dict[str, list[int]] = {}
    loads: dict[str, list[int]] = {}
    for n in _own_scope_names(fn):
        if n.id in skip:
            continue
        if isinstance(n.ctx, ast.Store):
            stores.setdefault(n.id, []).append(n.lineno)
        elif isinstance(n.ctx, ast.Load):
            loads.setdefault(n.id, []).append(n.lineno)
    out = []
    for name, store_lines in stores.items():
        first_store = min(store_lines)
        early = sorted({ln for ln in loads.get(name, []) if ln < first_store})
        if early:
            out.append((name, first_store, early))
    return out


def _scan() -> list[str]:
    findings: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a parse failure is its own test
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for name, store, early in _offenders(node):
                    findings.append(
                        f"{path.relative_to(APP.parent)}::{node.name}() — "
                        f"{name!r} read at line(s) {early} but only assigned at "
                        f"line {store}; Python makes it local to the whole "
                        f"function, so those reads raise UnboundLocalError."
                    )
    return findings


def test_no_local_is_read_before_it_is_assigned():
    findings = _scan()
    assert not findings, (
        "Local read before assignment — this raises UnboundLocalError at runtime "
        "on every call:\n  " + "\n  ".join(findings)
    )


def test_get_audit_binds_its_model_alias_before_use():
    """The specific regression, pinned by name.

    `get_audit` is the audit detail endpoint: if it raises, no audit in the
    product can be opened. It cannot be exercised here (no database), so the
    ordering is asserted directly."""
    tree = ast.parse((APP / "services" / "audit_compliance.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_audit"
    )
    stores = [n.lineno for n in ast.walk(fn)
              if isinstance(n, ast.Name) and n.id == "R" and isinstance(n.ctx, ast.Store)]
    loads = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Name) and n.id == "R" and isinstance(n.ctx, ast.Load)]
    assert stores, "get_audit no longer binds `R` — update this test if it was renamed."
    assert min(stores) < min(loads), (
        f"`R` is read at line {min(loads)} but not assigned until {min(stores)}."
    )
