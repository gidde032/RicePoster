"""Shared filesystem anchors for the test suite.

Several test modules need the project root — to read `frontend/index.html`,
`run.sh`, `.gitignore`, or to set `subprocess.run(cwd=...)`. Each formerly
redefined `PROJECT_ROOT = Path(__file__).parent.parent`, which drifted
(notably `test_gates.py`, which kept a `str` variant). They now import it
from here so there is one definition.

This is a plain module rather than a fixture because some uses are at import
time (e.g. `INDEX_HTML = PROJECT_ROOT / ...` in `test_frontend_robustness`),
which a fixture cannot serve. Mirrors the established `tests/source_probe.py`
and `tests/output_catalogue.py` helper-module convention.
"""

from pathlib import Path

# The same value every former site computed
# (`Path(__file__).parent.parent` from a file one level under the root), kept
# literal rather than `.resolve()`-d so the consolidation changes nothing.
PROJECT_ROOT = Path(__file__).parent.parent
