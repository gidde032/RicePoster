"""Read a function's real source, independent of the autouse tripwire.

Playwright flows are not end-to-end testable here, so a number of guarantees
are asserted at source level instead (CLAUDE.md § Testing rules). Those
assertions and `tests/conftest.py`'s live-call tripwire pull in opposite
directions: the tripwire replaces module attributes such as
`tiktok_browser.post_media` with a stub, so `inspect.getsource` on the live
attribute returns the stub's body and the assertion silently stops checking
anything real.

`test_scheduling_slice1.py` had solved this locally for
`session_manager._run_session_check`, the first function to be both
tripwired and source-asserted. Batch 6 added the two browser `post_media`
functions to the tripwire, which put every remaining source-level pin in the
same position, so the helper moved here to be shared rather than copied.

Parsing the file rather than the attribute is the point: it cannot be fooled
by a monkeypatch, which is exactly the failure mode being guarded against.
"""

import ast
import inspect


def real_source(module, func_name: str) -> str:
    """Return `func_name`'s source as written in `module`'s file.

    Raises rather than returning "" when the function is absent, so a rename
    fails the assertion loudly instead of turning it into a vacuous pass.
    """
    mod_src = inspect.getsource(module)
    tree = ast.parse(mod_src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(mod_src, node)
    raise ValueError(f"{func_name} not found in {module.__name__}")
