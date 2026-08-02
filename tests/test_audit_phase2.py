"""Regression tests for the Phase 2 multi-lens audit (2026-07-27), Slice 2.

Slice 2 is the "clone integrity" slice: every finding here is something that
is fine on the maintainer's machine and broken for anyone who clones the repo.
That asymmetry is exactly what the test suite structurally cannot see — the
suite is deliberately hermetic (`config.py` skips load_dotenv under pytest,
conftest redirects the session-label lookup), so it happily passes on a clone
where the tooling, the browser and the quality gates are all absent.

These tests therefore assert on the *shipped setup surface* — requirements.txt,
README.md, pyproject.toml, credentials.env.example, run.sh — rather than on
runtime behaviour. Reviewers: skeptical senior engineer (findings #5, #6),
spec-drift lens and user-friction lens (findings #7, #15, #16, #17).
"""

import re
import warnings
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

# The smoke-tier wall-clock budget, asserted for real in test_gates.py. Every
# document that quotes a number must quote this one.
SMOKE_BUDGET_S = 2.0

# Docs that are deliberately gitignored (local-only maintainer notes). A clone
# does not have them, so no *setup* file may send a user to one.
GITIGNORED_DOCS = (
    "RESEARCH-platform-detection.md",
    "DESIGN-scheduling.md",
    "AUDIT-phase1-scheduling.md",
    "SPEC.md",
    "handoff.md",
)


def _read(name: str) -> str:
    return (PROJECT_ROOT / name).read_text()


# --- Finding #5: the gates cannot run on a fresh clone ----------------------

@pytest.mark.parametrize("package", ["pytest", "pytest-cov", "pre-commit"])
def test_requirements_declares_the_test_tooling(package):
    """requirements.txt must install what the gates need.

    It listed only the 9 runtime dependencies. `pip install -r
    requirements.txt` therefore produced an environment where
    `python -m pytest` raised ModuleNotFoundError and the first `git commit`
    failed in the hook. Found independently by the skeptic and the friction
    lens — the strongest convergence signal in the pass.
    """
    requirements = _read("requirements.txt")
    assert re.search(rf"^{re.escape(package)}[=<>]", requirements, re.M), (
        f"{package} is not declared in requirements.txt. The test suite and "
        f"the pre-commit hooks are unrunnable without it, so a clone cannot "
        f"commit. It being installed on the maintainer's machine is not the "
        f"same as it being installed."
    )


# --- Finding #6: the config file is not the enforcement --------------------

def test_readme_documents_pre_commit_install():
    """Installing the hooks is a required setup step, not an optional one.

    `.pre-commit-config.yaml` describes two gates; it enforces neither until
    `pre-commit install` has run. test_gates.py asserts the config file's
    *text*, which stays green on a clone where the hooks were never wired up
    — the same "documented but enforced nowhere" gap that test_gates.py was
    itself written to close, reappearing one level up.
    """
    readme = _read("README.md")
    assert "pre-commit install" in readme, (
        "README.md does not tell a new contributor to run `pre-commit "
        "install`. Without it, git commit and git push enforce nothing."
    )
    assert "--hook-type pre-push" in readme, (
        "README.md documents the commit hook but not the pre-push hook. "
        "pre-commit does not install the pre-push stage by default, so the "
        "coverage floor would still be enforced by nothing."
    )


def test_pre_push_hook_if_installed_delegates_to_pre_commit():
    """A hook that exists must actually invoke pre-commit.

    Still skips on a genuinely fresh clone — a suite that went red on
    `git clone` would be worse than the gap it reports. But the original skip
    covered a second case it should not have: a *partial* install, where
    `pre-commit install` ran (so the commit hook exists) but
    `--hook-type pre-push` never did. That machine looks hooked-up and is not,
    and the skip made the test pass most loudly exactly when the gate was
    missing (tech-debt audit TS-2).

    This matters more than the audit credited: CLAUDE.md records that the
    `main` ruleset requiring the Actions check is stored but *not enforced* on
    the current private Free repository, so the local pre-push hook is the only
    thing actually enforcing the coverage floor today.
    """
    hooks_dir = PROJECT_ROOT / ".git" / "hooks"
    pre_push = hooks_dir / "pre-push"
    pre_commit = hooks_dir / "pre-commit"

    if not pre_push.exists():
        if pre_commit.exists() and "pre-commit" in pre_commit.read_text():
            pytest.fail(
                "PARTIAL HOOK INSTALL: the pre-commit hook is installed but the "
                "pre-push hook is not, so `git push` runs neither the full suite "
                "nor the coverage floor — and with the main ruleset unenforced on "
                "a private Free repo, nothing else does either. Fix: "
                "`pre-commit install --hook-type pre-push`."
            )
        # Neither hook: a fresh clone or CI, where the workflow enforces the
        # gates directly. Warn so it appears in pytest's warnings summary
        # rather than vanishing into a silent skip.
        warnings.warn(
            "no git hooks installed — local gates are not enforced. Run "
            "`pre-commit install && pre-commit install --hook-type pre-push`.",
            UserWarning,
            stacklevel=2,
        )
        pytest.skip("no hooks installed (fresh clone or CI) — see warning above")

    assert "pre-commit" in pre_push.read_text(), (
        f"{pre_push} exists but does not invoke pre-commit, so `git push` is not "
        f"running the full suite or the coverage floor."
    )


# --- Finding #7: the documented install fetches the wrong browser ----------

def test_readme_playwright_install_matches_the_launch_channel():
    """README's install command and the code's launch channel must agree.

    README said `playwright install chromium`; both posters launch
    `channel="chrome"` (real Google Chrome, needed for video codec support).
    Chromium is a different binary, so the documented setup does not produce
    the browser the code opens.
    """
    for module in ("backend/instagram_browser.py", "backend/tiktok_browser.py"):
        assert 'channel="chrome"' in _read(module), (
            f"{module} no longer launches channel='chrome' — if the launch "
            f"channel changed, README's `playwright install` line must too."
        )

    # Scope to the Installation code fence: the surrounding prose deliberately
    # names `playwright install chromium` in order to warn against it, so a
    # whole-file search reports the warning as the defect.
    readme = _read("README.md")
    assert "## Installation" in readme, "README.md lost its Installation section."
    fence = re.search(r"```bash\n(.*?)```", readme.split("## Installation", 1)[1], re.S)
    assert fence, "README.md's Installation section has no shell block."

    commands = re.findall(r"playwright install (\w+)", fence.group(1))
    assert commands, "README.md no longer documents a `playwright install` step."
    assert "chromium" not in commands, (
        "README.md tells the user to run `playwright install chromium`, but "
        "the posters launch channel='chrome'. Chromium is the wrong binary."
    )


# --- Finding #15: setup files must not cite files a clone lacks ------------

@pytest.mark.parametrize("doc", GITIGNORED_DOCS)
def test_credentials_example_does_not_cite_gitignored_docs(doc):
    """The config template must explain itself, not delegate to a missing file.

    `credentials.env.example` pointed at RESEARCH-platform-detection.md to
    justify the three anti-detection knobs. That file is gitignored, so the
    one user who most needs the reasoning — someone deciding what values to
    set — cannot read it. The rationale is now inline.
    """
    assert doc not in _read("credentials.env.example"), (
        f"credentials.env.example cites {doc}, which is gitignored and "
        f"absent from a clone. Inline the reasoning instead of linking."
    )


def test_readme_discloses_that_internal_docs_are_absent():
    """Source comments cite ~40 gitignored docs; the README must say so.

    Rewriting every citation across backend/ and tests/ would churn the
    posting path for no functional gain. Disclosing the convention once is
    the proportionate fix: a citation you cannot follow becomes expected
    rather than a missing file.
    """
    readme = _read("README.md")
    assert "## Internal documentation" in readme, (
        "README.md has no `## Internal documentation` section. Without it, "
        "the design/research docs cited throughout backend/ and tests/ read "
        "as missing files rather than as deliberate local-only notes."
    )
    section = readme.split("## Internal documentation", 1)[1]
    for doc in ("DESIGN-scheduling.md", "RESEARCH-platform-detection.md"):
        assert doc in section, (
            f"README's Internal documentation section does not name {doc}, "
            f"one of the filenames a reader will actually encounter in "
            f"source comments."
        )


# --- Finding #16: one budget, quoted consistently --------------------------

def test_smoke_budget_is_consistent_across_documents():
    """Every document quoting the smoke budget must quote the enforced one.

    pyproject.toml and SPEC.md said "< 1s" while test_gates.py enforces 2.0s
    and CLAUDE.md says "< 2s". test_gates.py cross-checks only the *coverage*
    number, so the smoke budget drifted with the suite fully green.

    SPEC.md and CLAUDE.md are gitignored, so they are checked when present
    rather than required — the same pattern test_gates.py already uses.
    """
    budget = f"< {SMOKE_BUDGET_S:.0f}s"

    marker_line = [
        line for line in _read("pyproject.toml").splitlines()
        if line.strip().startswith('"smoke:')
    ]
    assert marker_line, "pyproject.toml no longer defines the smoke marker."
    assert budget in marker_line[0], (
        f"pyproject.toml's smoke marker does not quote the enforced budget "
        f"({budget}): {marker_line[0].strip()}"
    )

    for name in ("SPEC.md", "CLAUDE.md"):
        path = PROJECT_ROOT / name
        if not path.exists():
            continue
        smoke_lines = [
            line for line in path.read_text().splitlines()
            if "smoke" in line.lower() and re.search(r"<\s*\d+(\.\d+)?\s*s", line)
        ]
        for line in smoke_lines:
            assert budget in line, (
                f"{name} quotes a smoke budget that is not the enforced "
                f"{budget}: {line.strip()}"
            )


def test_spec_documents_every_tripwire_target():
    """SPEC.md's tripwire list must match what conftest actually patches.

    SPEC.md omitted `_run_session_check` and `NtfyNotifier.send`. An
    understated guard is the safer direction of error, but the list is the
    thing a contributor reads to learn which live entry points are covered —
    and the list is explicit, not automatic, so a new entry point stays
    unguarded until someone adds it.
    """
    spec = PROJECT_ROOT / "SPEC.md"
    if not spec.exists():
        pytest.skip("SPEC.md is gitignored and absent from this checkout")

    conftest = _read("tests/conftest.py")
    for target in ("_run_session_check", "NtfyNotifier.send"):
        leaf = target.split(".")[-1]
        assert leaf in conftest, (
            f"conftest no longer references {target}; if the tripwire "
            f"dropped a target, SPEC.md's budget table must change too."
        )
        assert target in spec.read_text(), (
            f"SPEC.md's no-live-calls budget does not list {target}, which "
            f"the conftest tripwire does guard."
        )


# --- Finding #17: the seed content style leaking into shipped files --------

def test_run_sh_uses_the_product_name():
    """run.sh greeted the user with a caption style naming real people.

    CLAUDE.md states the app is content-neutral and the UI name is
    RicePoster; run.sh used to echo a startup banner built from the seed
    caption style — in a tracked file, as the first thing a new user sees.

    The banner must name the product, not a content style. Asserted as an
    allowlist: spelling out the old persona to forbid it would put the name
    back in a tracked file (#50 Phase B).
    """
    run_sh = _read("run.sh")
    assert "RicePoster" in run_sh, "run.sh no longer names the product."
    for style in ("generic", "meme-humor", "sports", "music-fanpage"):
        assert style not in run_sh.lower(), (
            f"run.sh names the '{style}' caption style. Prompts are rotatable "
            "per slot; the app itself is content-neutral."
        )


# --- Finding #14: ROADMAP contradicted itself two lines apart --------------

def test_roadmap_does_not_list_stories_as_a_daily_activity():
    """ROADMAP's goal statement and its scope statement disagreed.

    Line 5 said the maintainer "spends daily effort only on stories and
    comment replies"; line 6 said stories are permanently out of scope.
    ROADMAP.md is tracked, so this contradiction was clone-visible.
    """
    roadmap = _read("ROADMAP.md")
    assert "only on stories" not in roadmap, (
        "ROADMAP.md still describes stories as a daily activity. They were "
        "permanently removed on 2026-07-18."
    )

