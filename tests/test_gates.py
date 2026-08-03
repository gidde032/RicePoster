"""Quality-gate meta-tests.

These enforce the project's numeric contracts and their placement: smoke-tier
marker count, smoke-tier speed budget, and coverage-floor agreement across the
local pre-push hook and GitHub Actions.

The coverage-floor tests were added by audit fix B1 (finding #3). Before
that, this docstring already claimed "coverage-floor agreement" and no such
test existed — the gate was documented in CLAUDE.md and SPEC.md, listed in
this docstring, and enforced by nothing. The tests below check the
*enforcement mechanism* (the --cov flags in the pre-push hook) rather than
re-running the suite under coverage, which would double the suite's runtime
every time it ran.
"""

import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PROJECT_ROOT_PATH = Path(__file__).parent.parent
PROJECT_ROOT = str(PROJECT_ROOT_PATH)

COVERAGE_FLOOR = 43

SMOKE_TEST_COUNT = 6

# The smoke tier's execution budget, excluding interpreter startup, plugin
# loading and collection. Baseline is ~0.3s (2026-08-02), so this leaves ~4x
# headroom for a loaded machine while still catching a real regression.
SMOKE_EXECUTION_BUDGET_S = 1.5

# Wall-clock ceiling for the whole subprocess. A hang detector, not a budget:
# the tier executes in ~0.3s, so anything approaching this is an environment
# failure. Kept deliberately loose — the tight version was the flake.
SMOKE_WALL_CLOCK_CEILING_S = 10.0


def _collected_node_ids(stdout: str) -> list[str]:
    """The test node IDs pytest printed, independent of its summary wording.

    `--collect-only -q` prints one node ID per selected test and then a summary
    sentence. This used to read the summary — `collected[-1].startswith("6/")`
    against `6/785 tests collected (779 deselected) in 0.31s` — which coupled a
    quality gate to prose pytest is free to reformat in any release (#37, TS-3).

    The audit's stated reason for flagging it was wrong: it claimed the prefix
    would also match 60 tests, but `"60 tests collected"` does not start with
    `"6/"`, so the old assertion did fail as intended. The real exposure was
    only ever the reformatting one.

    Node IDs are a stable public contract — you can paste one back into pytest
    as an argument — so counting them is both stricter and less brittle than
    parsing a sentence.

    Every line containing `::` is counted. An earlier version also filtered
    `ERROR`/`E ` prefixes to skip collection-error output, which all three cold
    reviewers (2026-07-31) independently identified as unreachable: the caller
    asserts `returncode == 0` first, and a collection error makes that
    non-zero. It was also incomplete — pytest's plural `ERRORS` header and
    indented traceback frames carry neither prefix. A dead filter that would
    not have worked anyway is worse than none, because it implies a guard that
    is not there. The `returncode` assertion is the real guard; keep it.
    """
    return [line.strip() for line in stdout.splitlines() if "::" in line]


def test_smoke_marker_exact_count():
    """The 'smoke' marker must tag exactly `SMOKE_TEST_COUNT` tests.

    Exact-count prevents both removal (drops below the count) and creep (slow
    or non-happy-path tests added to what's supposed to be fast feedback).
    Update the constant deliberately when adding or removing a smoke test.

    The count is named rather than spelled out here: an earlier version wrote
    "exactly 6" in this docstring beside the constant, which is the same
    two-copies drift the constant exists to remove (cold review, 2026-07-31).
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-m", "smoke", "-q"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, f"Collection failed:\n{result.stderr}"
    node_ids = _collected_node_ids(result.stdout)
    assert len(node_ids) == SMOKE_TEST_COUNT, (
        f"Expected exactly {SMOKE_TEST_COUNT} smoke tests, collected "
        f"{len(node_ids)}:\n" + "\n".join(node_ids or [result.stdout])
    )


def test_smoke_count_survives_a_reformatted_pytest_summary():
    """TS-3 (#37): the smoke gate must not read pytest's summary sentence.

    Feeds the parser output whose node IDs are unchanged but whose summary line
    has been reworded, as a future pytest release may do. The count still comes
    out right, and the second half of this test shows the previous approach
    would not have: the reworded summary is still matched by the old line
    filter, so it was reached, and it does not carry the expected prefix.

    That combination — reached but not matching — is a *failing* gate, not a
    skipped one. A pytest release could therefore have turned this gate red
    with no change to the suite it guards.
    """
    reworded = (
        "tests/test_api.py::test_serve_ui\n"
        "tests/test_api.py::test_accounts_mock_mode\n"
        "\n"
        "collected 2 tests / 779 deselected in 0.31s\n"
    )

    assert len(_collected_node_ids(reworded)) == 2

    old_filter = [
        line for line in reworded.splitlines()
        if "collected" in line and "deselected" in line
    ]
    assert old_filter, "the old filter should still find a summary line here"
    assert not old_filter[-1].startswith("2/"), (
        "the old prefix assertion would have failed on this wording"
    )


def _junit_suite_seconds(xml_path: Path) -> float:
    """Execution time of a pytest run, from its JUnit XML report.

    Read from structured output rather than the summary sentence. Two reasons,
    and the second is the one that motivated the rewrite (#13, #44):

    1. `_collected_node_ids` above exists because a gate once parsed pytest's
       prose. Adding a *second* prose parser to the same file would reintroduce
       exactly the coupling TS-3 removed. The `time` attribute is part of the
       JUnit schema, which pytest does not reword between releases.
    2. The summary's `in 0.27s` and the subprocess wall-clock measure different
       things. The old gate timed the wall-clock of a cold subprocess, so it
       was dominated by interpreter startup, plugin loading and full-suite
       collection — measured 2026-08-02 at 0.97-2.36s across five *idle* runs
       for a tier that executes in 0.27s. Roughly 80% of the gate was
       process-spawn noise, and it breached the 2s budget on an unloaded
       machine.

    Raises rather than returning a sentinel: a report this cannot read means
    the measurement did not happen, and a speed gate that silently passes when
    it cannot measure is worse than no gate.
    """
    root = ET.parse(xml_path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None, f"No <testsuite> element in JUnit report:\n{xml_path.read_text()}"
    seconds = suite.get("time")
    assert seconds is not None, "JUnit <testsuite> carries no time attribute"
    return float(seconds)


def test_smoke_tier_runs_under_budget(tmp_path):
    """The smoke tier's *execution* must stay fast.

    Two assertions measuring two different failures:

    - `SMOKE_EXECUTION_BUDGET_S` is the real gate. It covers test execution
      only, so it catches a genuine regression in the tier and is largely
      immune to machine load. Baseline is ~0.3s, so the budget leaves roughly
      4x headroom.
    - `SMOKE_WALL_CLOCK_CEILING_S` is deliberately loose and only catches a
      hang or a pathological environment. It is not a performance budget;
      do not tighten it to make it "meaningful". Its looseness is the point —
      the tight version was the flake (#13, #44).

    The JUnit report goes to `tmp_path`, never the project tree: it records
    the machine's hostname, which must not become a committable file.
    """
    report = tmp_path / "smoke-junit.xml"
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "smoke", "-q",
         "--junit-xml", str(report)],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0, f"Smoke suite failed:\n{result.stderr}"

    assert elapsed < SMOKE_WALL_CLOCK_CEILING_S, (
        f"Smoke run took {elapsed:.1f}s wall-clock, ceiling is "
        f"{SMOKE_WALL_CLOCK_CEILING_S}s. This is a hang detector, not a speed "
        "budget — suspect the environment, not the tier."
    )

    executed = _junit_suite_seconds(report)
    assert executed < SMOKE_EXECUTION_BUDGET_S, (
        f"Smoke tier executed in {executed:.2f}s, budget is "
        f"{SMOKE_EXECUTION_BUDGET_S}s. Unlike the old wall-clock gate this "
        "excludes interpreter startup and collection, so a breach here is a "
        "real regression in the tier."
    )


def test_smoke_budget_reads_structured_timing_not_prose(tmp_path):
    """#13/#44: the speed gate must not parse pytest's summary sentence.

    Feeds a JUnit report whose `time` attribute is unambiguous, and confirms
    the parser reads it. Then shows the failure mode this replaced: the
    summary sentence a prose parser would have read reports a *different*
    number — wall-clock including startup — so the two disagree by design, and
    a prose-based gate measures the machine rather than the tier.
    """
    report = tmp_path / "report.xml"
    report.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests"><testsuite name="pytest" errors="0" '
        'failures="0" skipped="0" tests="6" time="0.322">'
        '<testcase classname="tests.test_api" name="test_serve_ui" time="0.013" />'
        "</testsuite></testsuites>"
    )
    assert _junit_suite_seconds(report) == pytest.approx(0.322)

    prose = "6 passed, 798 deselected in 2.36s\n"
    prose_number = float(re.search(r"in (\d+\.\d+)s", prose).group(1))
    assert prose_number > _junit_suite_seconds(report) * 5, (
        "the summary sentence and the execution time are not the same "
        "measurement; that gap is why the old gate flaked"
    )


def test_smoke_budget_fails_loudly_on_an_unreadable_report(tmp_path):
    """A speed gate that cannot measure must fail, not silently pass.

    The tempting shortcut is to return 0.0 when the report is missing a
    `time` attribute, which would make every future run pass regardless of
    how slow the tier became.
    """
    missing_attr = tmp_path / "no-time.xml"
    missing_attr.write_text('<testsuite name="pytest" tests="6"></testsuite>')
    with pytest.raises(AssertionError, match="no time attribute"):
        _junit_suite_seconds(missing_attr)

    wrong_shape = tmp_path / "wrong.xml"
    wrong_shape.write_text("<something-else />")
    with pytest.raises(AssertionError, match="No <testsuite> element"):
        _junit_suite_seconds(wrong_shape)


def _pre_push_entry() -> str:
    """The command string the pre-push hook actually runs."""
    config = (PROJECT_ROOT_PATH / ".pre-commit-config.yaml").read_text()
    # Split on hook boundaries ("- id:") and return the block for pytest-full.
    blocks = config.split("- id:")
    matching = [b for b in blocks if b.lstrip().startswith("pytest-full")]
    assert matching, f"No pytest-full hook found in .pre-commit-config.yaml:\n{config}"
    entry_lines = [l for l in matching[0].splitlines() if l.strip().startswith("entry:")]
    assert entry_lines, f"pytest-full hook has no entry: line:\n{matching[0]}"
    return entry_lines[0].split("entry:", 1)[1].strip()


def _ci_workflow() -> str:
    """Return the tracked clean-environment gate."""
    path = PROJECT_ROOT_PATH / ".github" / "workflows" / "ci.yml"
    assert path.exists(), "GitHub Actions CI workflow is missing"
    return path.read_text()


def test_coverage_gate_is_actually_enforced():
    """A hook must really run --cov-fail-under (audit B1 / finding #3).

    CLAUDE.md lists coverage as a quality gate and SPEC.md lists it as a
    budget, but for the whole life of the project no hook passed --cov, so the
    floor could be breached without anything failing. This test fails if the
    flags are ever dropped from either the pre-push hook or CI.
    """
    entry = _pre_push_entry()
    assert "--cov=backend" in entry, (
        f"pre-push hook no longer measures coverage: {entry!r}"
    )
    assert "--cov-fail-under" in entry, (
        f"pre-push hook measures coverage but enforces no floor: {entry!r}"
    )

    ci = _ci_workflow()
    assert "python -m pytest tests/" in ci, (
        "CI no longer runs the full test suite"
    )
    assert "--cov=backend" in ci, "CI no longer measures backend coverage"
    assert "--cov-fail-under=43" in ci, (
        "CI no longer enforces the documented coverage floor"
    )
    assert "-ra" in ci, "CI must display skip reasons instead of hiding them"


def test_ci_is_a_read_only_pull_request_gate():
    """CI must validate pull requests without write-capable token permissions."""
    ci = _ci_workflow()
    assert "pull_request:" in ci, "CI no longer runs for pull requests"
    assert "contents: read" in ci, "CI token is not explicitly read-only"
    assert "pull_request_target" not in ci, (
        "Do not execute contributor code through pull_request_target"
    )


def test_coverage_floor_matches_documentation():
    """The enforced floor and the documented floor must not drift apart.

    A gate that enforces a different number than the docs claim is how the
    phantom gate went unnoticed for so long.
    """
    entry = _pre_push_entry()
    match = re.search(r"--cov-fail-under[= ](\d+)", entry)
    assert match, f"Could not parse the floor out of: {entry!r}"
    enforced = int(match.group(1))
    assert enforced == COVERAGE_FLOOR, (
        f"pre-push hook enforces {enforced}%, this file documents "
        f"{COVERAGE_FLOOR}%. Update both, plus CLAUDE.md and SPEC.md."
    )

    # CLAUDE.md is gitignored (local-only agent doc), so it is absent from a
    # fresh clone. Check it when present rather than failing on its absence.
    claude_md_path = PROJECT_ROOT_PATH / "CLAUDE.md"
    if claude_md_path.exists():
        assert f"--cov-fail-under={COVERAGE_FLOOR}" in claude_md_path.read_text(), (
            f"CLAUDE.md § Quality gates no longer documents a {COVERAGE_FLOOR}% "
            f"coverage floor. Lowering a floor needs maintainer sign-off."
        )

    ci_match = re.search(r"--cov-fail-under[= ](\d+)", _ci_workflow())
    assert ci_match, "Could not parse the coverage floor from CI"
    assert int(ci_match.group(1)) == COVERAGE_FLOOR, (
        "CI coverage floor drifted from the pre-push and documented floor"
    )


HANDOFF_LINE_BUDGET = 200


def test_handoff_stays_within_its_budget():
    """`handoff.md` briefs the next session; it is not a history.

    It had ballooned to 403 lines three times by 2026-07-31, each time because
    a closing agent appended a full batch retrospective instead of routing the
    content to `CHANGELOG.md`, `SPEC.md`, `workflow-practices.md`,
    `subagent-brief-templates.md` or `practices/`. The file's own preamble
    carries that routing table; this test is what makes the budget real rather
    than aspirational.

    `handoff.md` is gitignored, so this warns-and-skips on a clean clone in the
    same way `test_documented_long_file_sizes_are_current` does. That makes it
    a local guard rather than a CI gate — weaker, but it fires at commit time
    on the only machine where the file exists.

    If this fails, route the excess rather than raising the number. Raising it
    is a deliberate maintainer decision, not a way to make the test pass.
    """
    handoff = PROJECT_ROOT_PATH / "handoff.md"
    if not handoff.exists():
        pytest.skip("handoff.md is gitignored and absent from this checkout")

    lines = len(handoff.read_text().splitlines())
    assert lines <= HANDOFF_LINE_BUDGET, (
        f"handoff.md is {lines} lines, over its {HANDOFF_LINE_BUDGET}-line "
        f"budget by {lines - HANDOFF_LINE_BUDGET}. Route the excess to the "
        f"destinations in the file's own preamble table; do not raise this "
        f"number to make the test pass."
    )


def test_suite_does_not_read_the_maintainers_credentials_env():
    """The suite's result must not depend on an untracked local file.

    Every knob in backend/config.py is a module-level constant evaluated at
    import, and config.py used to call load_dotenv(credentials.env)
    unconditionally. So the maintainer setting PREFLIGHT_CHECK_PLATFORMS=none
    on 2026-07-27 turned seven tests red without a line of code changing, and
    a fresh clone (no credentials.env) would exercise a third set of values
    again. Tests must see the shipped defaults and nothing else.

    Checks the observable effect — no credentials.env key reached os.environ —
    rather than config.UNDER_PYTEST, which would only restate the flag.
    """
    env_path = PROJECT_ROOT_PATH / "credentials.env"
    if not env_path.exists():
        # Fresh clone: nothing to leak. The invariant holds trivially.
        return

    leaked = []
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        # A key already exported in the maintainer's shell would be in
        # os.environ regardless of dotenv, and python-dotenv would not have
        # overwritten it. Only a *matching* value is evidence of a load.
        if os.environ.get(key) == value and value:
            leaked.append(key)

    assert not leaked, (
        f"credentials.env leaked into the test environment: {sorted(leaked)}. "
        f"backend/config.py must not call load_dotenv under pytest — see "
        f"config.UNDER_PYTEST. A gate whose result depends on a gitignored "
        f"file is not a gate."
    )
