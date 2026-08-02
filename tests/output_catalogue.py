"""Extract every user-visible output site in `backend/` as ordered text.

Batch 7 (#26) rewrites the *call* at 67 output sites while requiring the
*bytes* those sites emit to stay identical. That is a silent-failure shape:
a dropped site, a mangled f-string, or a message that quietly moved between
the CLI-report population and the diagnostic population all still import,
still pass type checks, and still run a live post — they just stop telling
the maintainer what happened, or tell them something different.

So the pin is built the way Batch 6 built its Playwright goldens: run the
extractor against the *unmodified* code, commit the result under
`tests/golden/`, and require the refactor to reproduce it byte for byte.

The catalogue deliberately records `module :: function :: argument source`
and **not** the callee. `print(f"[queue] x")` and `_log.warning(f"[queue] x")`
produce the same entry, so the golden is invariant across the migration and
any difference in it is a real change to what the user sees. Which callee
each site uses is the thing Batch 7 changes, so it is asserted separately —
see `test_logging_adoption.py`.

Line numbers are excluded for the same reason: they shift under any edit and
would turn the pin into noise.
"""

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"

#: Logger methods that emit to the user. `debug` is included so a site
#: demoted below the console threshold still appears in the catalogue
#: rather than silently leaving it.
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical"}
)

#: The module-level logger name this project uses. Matching on the name
#: rather than on any attribute call keeps unrelated `.error(...)` calls
#: (on responses, parsers, or platform objects) out of the catalogue.
LOGGER_NAME = "_log"


def _is_output_call(node: ast.AST) -> bool:
    """True for `print(...)` and for `_log.<level>(...)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    return (
        isinstance(func, ast.Attribute)
        and func.attr in LOG_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id == LOGGER_NAME
    )


def _enclosing_scopes(tree: ast.AST) -> dict[int, str]:
    """Map each node id to its dotted enclosing function/class path."""
    scopes: dict[int, str] = {}

    def walk(node: ast.AST, path: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                child_path = f"{path}.{child.name}" if path else child.name
            else:
                child_path = path
            scopes[id(child)] = child_path
            walk(child, child_path)

    scopes[id(tree)] = ""
    walk(tree, "")
    return scopes


def _argument_source(src: str, node: ast.Call) -> str:
    """Return the call's argument source, flattened to a single line.

    Only the *source line breaks* are flattened, by stripping each physical
    line and joining on one space. Whitespace inside a literal is preserved
    exactly, because several of these messages are aligned report rows
    (`f"    TikTok:    {tt}"`) where the indentation is the user-visible
    output. A naive `" ".join(segment.split())` collapses those too and
    would let an alignment change pass the pin unnoticed.
    """
    if not node.args:
        return "<empty>"
    parts = []
    for arg in node.args:
        segment = ast.get_source_segment(src, arg) or ""
        parts.append(" ".join(line.strip() for line in segment.splitlines()))
    return ", ".join(parts)


def catalogue() -> list[str]:
    """Return every output site in `backend/`, ordered and deterministic."""
    entries: list[str] = []
    for path in sorted(BACKEND.glob("*.py")):
        src = path.read_text()
        tree = ast.parse(src)
        scopes = _enclosing_scopes(tree)
        sites = [
            (node.lineno, scopes.get(id(node), ""), _argument_source(src, node))
            for node in ast.walk(tree)
            if _is_output_call(node)
        ]
        for _, scope, args in sorted(sites):
            entries.append(f"{path.name} :: {scope or '<module>'} :: {args}")
    return entries


def catalogue_text() -> str:
    """The catalogue as the golden file stores it."""
    return "\n".join(catalogue()) + "\n"


def print_scopes() -> set[tuple[str, str]]:
    """Return `(module, function)` for every site still using `print`.

    Batch 7 split these sites into two populations: CLI presentation, which
    stays `print`, and diagnostics, which became log records. Nothing in the
    code marks which is which, so the boundary is pinned by this set.
    """
    scopes: set[tuple[str, str]] = set()
    for path in sorted(BACKEND.glob("*.py")):
        src = path.read_text()
        tree = ast.parse(src)
        enclosing = _enclosing_scopes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                scopes.add((path.name, enclosing.get(id(node), "") or "<module>"))
    return scopes


def callees() -> list[tuple[str, int, str]]:
    """Return `(module, lineno, callee)` for every output site.

    This is the half of the migration that *does* change, so it is reported
    separately from the invariant catalogue.
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted(BACKEND.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not _is_output_call(node):
                continue
            func = node.func
            name = (
                "print"
                if isinstance(func, ast.Name)
                else f"{LOGGER_NAME}.{func.attr}"
            )
            found.append((path.name, node.lineno, name))
    return sorted(found)


if __name__ == "__main__":  # pragma: no cover - regeneration helper
    print(catalogue_text(), end="")
