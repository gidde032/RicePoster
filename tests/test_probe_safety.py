"""`tools/probe_fingerprint.py` must stay incapable of reaching a platform.

The probe launches a real Chrome, which puts it one careless edit away from
being a live path. `CLAUDE.md` § SAFETY forbids an agent from running anything
that could publish to a live account, and the conftest tripwire only guards
entry points it is told about. These are source-level assertions — the probe is
never executed here.

Added 2026-07-27 alongside the tool.
"""

import ast
import asyncio
from pathlib import Path
import sys

import pytest

from tools import probe_fingerprint

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


def _surface(width, height, scale, **unstable):
    return {
        "innerSize": [width, height - 111],
        "screenSize": [width, height],
        "devicePixelRatio": scale,
        **unstable,
    }


def test_cross_slot_comparison_uses_only_repository_controlled_surfaces():
    results = {
        "A": _surface(1512, 982, 2, timezone="America/Chicago", webgl="host-a"),
        "B": _surface(1440, 900, 2, timezone="Europe/Paris", webgl="host-b"),
        # Host/runtime values differ, but C is still an identity collision with A.
        "C": _surface(1512, 982, 2, timezone="Asia/Tokyo", webgl="host-c"),
    }

    groups = probe_fingerprint.group_controlled_surfaces(results)

    assert groups == [["A", "C"], ["B"]]
    assert set(probe_fingerprint.controlled_surface(results["A"])) == {
        "innerSize", "screenSize", "devicePixelRatio",
    }


def test_cross_slot_report_names_identical_and_differing_surfaces(capsys):
    results = {
        "A": _surface(1512, 982, 2),
        "B": _surface(1440, 900, 2),
        "C": _surface(1512, 982, 2),
    }

    assert probe_fingerprint.show_slot_comparison(results) is False

    output = capsys.readouterr().out
    assert "differing controlled surfaces : 2/3" in output
    assert "IDENTICAL controlled surfaces : A, C" in output


def test_cross_slot_report_confirms_every_configured_slot_differs(capsys):
    results = {
        "A": _surface(1512, 982, 2),
        "B": _surface(1440, 900, 2),
    }

    assert probe_fingerprint.show_slot_comparison(results) is True
    assert "none — every configured slot differs" in capsys.readouterr().out


def test_probe_slots_runs_sequential_headless_disposable_probes(monkeypatch):
    calls = []

    async def fake_probe(*, headless, slot):
        calls.append((headless, slot))
        return _surface(1512 if slot == "A" else 1440, 982, 2)

    monkeypatch.setattr(probe_fingerprint, "probe", fake_probe)
    monkeypatch.setattr(probe_fingerprint, "show", lambda *_args: None)

    results = asyncio.run(probe_fingerprint.probe_slots(["A", "B"]))

    assert calls == [(True, "A"), (True, "B")]
    assert list(results) == ["A", "B"]


def test_all_slots_preflight_reports_every_unresolved_assignment(monkeypatch, capsys):
    def fake_identity_kwargs(headless, slot):
        assert headless is True
        if slot in {"A", "C"}:
            raise ValueError(f"account {slot!r} has no stable device assignment")
        return {"viewport": {"width": 1440, "height": 789}}

    monkeypatch.setattr(probe_fingerprint, "identity_kwargs", fake_identity_kwargs)

    errors = probe_fingerprint.slot_assignment_errors(["A", "B", "C"])
    probe_fingerprint.show_slot_assignment_errors(errors)

    assert list(errors) == ["A", "C"]
    output = capsys.readouterr().out
    assert "CONFIGURED SLOT PREFLIGHT FAILED" in output
    assert "A                account 'A' has no stable device assignment" in output
    assert "C                account 'C' has no stable device assignment" in output
    assert "No browser probes were launched" in output


def test_all_slots_cli_exits_nonzero_on_controlled_surface_collision(monkeypatch):
    async def fake_probe_slots(slots):
        assert slots == ["A", "B"]
        return {
            "A": _surface(1512, 982, 2),
            "B": _surface(1512, 982, 2),
        }

    monkeypatch.setattr(probe_fingerprint, "SLOT_IDS", ("A", "B"))
    monkeypatch.setattr(probe_fingerprint, "slot_assignment_errors", lambda _slots: {})
    monkeypatch.setattr(probe_fingerprint, "probe_slots", fake_probe_slots)
    monkeypatch.setattr(sys, "argv", ["probe_fingerprint.py", "--all-slots"])

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(probe_fingerprint.main())

    assert exc_info.value.code == 1


def test_all_slots_cli_rejects_visible_mode(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe_fingerprint.py", "--all-slots", "--visible"],
    )

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(probe_fingerprint.main())

    assert exc_info.value.code == 2
    assert "cannot be used with --visible" in capsys.readouterr().err


def test_all_slots_cli_is_headless_only_and_uses_configured_slots(source):
    assert '"--all-slots"' in source
    assert "slots = list(SLOT_IDS)" in source
    assert "args.all_slots" in source and "args.visible" in source
