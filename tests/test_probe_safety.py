"""`tools/probe_fingerprint.py` must stay incapable of reaching a platform.

The probe launches a real Chrome, which puts it one careless edit away from
being a live path. `CLAUDE.md` § SAFETY forbids an agent from running anything
that could publish to a live account, and the conftest tripwire only guards
entry points it is told about. These are source-level assertions — the probe is
never executed here.

Added 2026-07-27 alongside the tool.
"""

import ast
from pathlib import Path

import pytest

PROBE = Path(__file__).parent.parent / "tools" / "probe_fingerprint.py"

PLATFORM_TELLS = (
    "instagram.com",
    "tiktok.com",
    "instagram_browser",
    "tiktok_browser",
    "poster_browser",
    "post_media",
    "post_all",
)


@pytest.fixture(scope="module")
def source() -> str:
    assert PROBE.exists(), f"probe tool missing at {PROBE}"
    return PROBE.read_text()


def _code_only(source: str) -> str:
    """The probe's executable code, with comments and docstrings stripped.

    Scoped deliberately: the module docstring *documents* that the probe never
    touches instagram.com or `sessions/`, so a naive substring search over the
    whole file matches the prose explaining the safety property and reports it
    as a violation. `workflow-practices.md` records this exact failure mode
    twice in the Phase 2 pass — assert on the code, not the explanation.

    Comments are absent from the AST; docstrings are removed explicitly.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_probe_never_names_a_platform_or_poster(source):
    """No platform URL and no posting module in executable code."""
    code = _code_only(source)
    for tell in PLATFORM_TELLS:
        assert tell not in code, (
            f"tools/probe_fingerprint.py references {tell!r} in code — it must "
            f"never be able to reach a platform or a posting path"
        )


def test_probe_imports_nothing_that_can_post(source):
    """Only `device_identity` may be imported from `backend`.

    Parsed rather than grepped so a rename or an `import backend.x as y`
    cannot slip past.
    """
    tree = ast.parse(source)
    backend_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend"):
            backend_imports.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("backend"):
                    backend_imports.add(alias.name)

    assert backend_imports <= {"backend.device_identity"}, (
        f"probe imports {backend_imports - {'backend.device_identity'}} from "
        f"backend; only the pure device_identity module is allowed"
    )


def test_probe_does_not_touch_the_real_sessions_dir(source):
    """It must use a throwaway profile, never the maintainer's session state."""
    code = _code_only(source)
    assert "SESSIONS_DIR" not in code
    assert "sessions" not in code, (
        "probe's code references a sessions path; the profile must be temporary"
    )
    assert "TemporaryDirectory" in code, (
        "probe must launch against a throwaway profile directory"
    )


def test_probe_launch_args_match_instagram_browser(source):
    """The copied arg list must not drift from the real launch config.

    The probe copies `IG_ARGS` rather than importing them, precisely so it
    pulls in no postable module. That trade buys a drift risk, which this
    test closes: if `instagram_browser` gains or loses a launch arg, the
    probe stops being faithful and this fails.
    """
    ig_source = (
        Path(__file__).parent.parent / "backend" / "instagram_browser.py"
    ).read_text()

    probe_args = set(
        ast.literal_eval(
            next(
                node.value
                for node in ast.parse(source).body
                if isinstance(node, ast.Assign)
                and any(
                    getattr(t, "id", None) == "IG_ARGS" for t in node.targets
                )
            )
        )
    )

    # Every arg the probe claims to reproduce must appear in the real module.
    for arg in probe_args:
        assert arg in ig_source, (
            f"probe passes {arg!r}, which instagram_browser no longer does"
        )

    # And every `--flag` string literal in the real launch args must be in the
    # probe, so a newly added arg cannot be silently missed.
    ig_flags = {
        node.value
        for node in ast.walk(ast.parse(ig_source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("--")
    }
    assert ig_flags == probe_args, (
        f"launch args drifted — instagram_browser has {ig_flags}, "
        f"probe has {probe_args}"
    )
