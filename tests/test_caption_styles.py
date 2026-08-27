"""Tests for the rotatable caption-style system (prompts/*.json).

Feature added 2026-07-18: caption prompts are per-style files instead of a
hardcoded SYSTEM_PROMPT, selectable per slot from the UI.
"""

import ast
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import backend.main as main
from backend import captions

from tests.paths import PROJECT_ROOT
SEED_STYLES = {"meme-humor", "sports", "generic", "music-fanpage"}


# --- style loading -----------------------------------------------------------

@pytest.mark.smoke
def test_all_seed_styles_load():
    styles = captions.load_styles()
    assert SEED_STYLES <= set(styles)
    for s in styles.values():
        assert s.display_name
        assert s.system_prompt
        assert s.no_topic_fallback


def test_default_style_ships_in_a_clone():
    """#50 Phase B: replaced a test that pinned the old default prompt
    verbatim. That prompt is now gitignored — a shipped default naming real
    people tied this repo to the accounts it posts to. The default must be a
    seed style, so a fresh clone can generate a caption out of the box."""
    assert captions.DEFAULT_STYLE == "generic"
    assert captions.DEFAULT_STYLE in SEED_STYLES
    assert captions.DEFAULT_STYLE in captions.load_styles()


def test_tracked_prompts_are_exactly_the_seed_set():
    """#50 Phase B: personal caption styles must stay local.

    Deliberately an allowlist, not a blocklist of names. A blocklist would
    have to spell out the very names it exists to keep out of the repo, which
    discloses them to anyone reading the test. Comparing against the seed set
    leaks nothing and is stricter: *any* unexpected tracked prompt fails,
    whoever it names.

    Reads from git, not the filesystem, so the maintainer's own gitignored
    styles sit in prompts/ without failing this.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "prompts/"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert tracked, "expected the seed prompts to be tracked"

    names = {Path(rel).stem for rel in tracked}
    assert names == SEED_STYLES, (
        f"Tracked prompts {sorted(names)} differ from the seed set "
        f"{sorted(SEED_STYLES)}. A personal caption style must stay local — "
        "add it to .gitignore instead of committing it."
    )


def test_clipper_ingest_default_is_a_public_seed_style():
    """Sensitivity regression (MEDIUM): public defaults must stay content-neutral.

    The prompt-file allowlist cannot catch a private style identifier copied
    into configuration, documentation, examples, or tests. Pin the executable
    default structurally without spelling any private identifier here.
    """
    tree = ast.parse((PROJECT_ROOT / "backend/config.py").read_text())
    assignments = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "CLIPPER_INGEST_STYLE"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    call = assignments[0]
    assert isinstance(call, ast.Call) and len(call.args) == 2
    default = call.args[1]
    assert isinstance(default, ast.Constant) and default.value in SEED_STYLES
    assert default.value == "generic"


def test_local_style_identifiers_are_absent_from_public_head():
    """Sensitivity regression (MEDIUM): local style names never enter public Git.

    Discover ignored local prompt names instead of embedding a disclosure-prone
    blocklist. The pre-push full suite then checks both tracked files and commit
    messages reachable from the proposed public HEAD.
    """
    local_names = {
        path.stem.casefold()
        for path in (PROJECT_ROOT / "prompts").glob("*.json")
        if path.stem not in SEED_STYLES
    }
    if not local_names:
        return

    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT, capture_output=True, check=True,
    ).stdout.split(b"\0")
    public_text = []
    for raw_path in tracked:
        if not raw_path:
            continue
        path = PROJECT_ROOT / raw_path.decode()
        try:
            public_text.append(path.read_text(errors="ignore"))
        except (OSError, UnicodeError):
            continue
    public_text.append(subprocess.run(
        ["git", "log", "HEAD", "--format=%B"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    ).stdout)
    corpus = "\n".join(public_text).casefold()
    leaked = sorted(name for name in local_names if name in corpus)
    assert not leaked, (
        "A local caption-style identifier appears in tracked files or public "
        "HEAD history. Keep local styles ignored and use a seed style in all "
        "public defaults, examples, tests, documentation, and commit messages."
    )


def test_malformed_style_file_is_skipped(monkeypatch, tmp_path):
    good = {
        "name": "good", "display_name": "Good",
        "system_prompt": "sp", "no_topic_fallback": "fb",
    }
    (tmp_path / "good.json").write_text(json.dumps(good))
    (tmp_path / "broken.json").write_text("{not valid json")
    (tmp_path / "missing-fields.json").write_text(json.dumps({"name": "x"}))
    monkeypatch.setattr(captions, "PROMPTS_DIR", tmp_path)

    styles = captions.load_styles()
    assert set(styles) == {"good"}


# --- generation routing ------------------------------------------------------

def test_generate_uses_selected_style_prompt(capturing_anthropic):
    asyncio.run(captions.generate_caption("video", "big dunk", style="sports"))
    sent = capturing_anthropic.calls[-1]
    assert sent["system"] == captions.load_styles()["sports"].system_prompt


def test_generate_defaults_to_default_style(capturing_anthropic):
    """Regression guard: omitting style must route through DEFAULT_STYLE."""
    asyncio.run(captions.generate_caption("video", "topic"))
    sent = capturing_anthropic.calls[-1]
    assert sent["system"] == captions.load_styles()[captions.DEFAULT_STYLE].system_prompt


def test_generate_unknown_style_raises_with_available_list(capturing_anthropic):
    with pytest.raises(ValueError, match="Unknown caption style 'nope'"):
        asyncio.run(captions.generate_caption("video", "t", style="nope"))
    assert capturing_anthropic.calls == []  # no API call was attempted


def test_no_topic_uses_style_specific_fallback(capturing_anthropic):
    asyncio.run(captions.generate_caption("video", "", style="sports"))
    user_msg = capturing_anthropic.calls[-1]["messages"][0]["content"]
    assert captions.load_styles()["sports"].no_topic_fallback in user_msg


# --- API plumbing ------------------------------------------------------------

def test_accounts_endpoint_lists_styles(client, monkeypatch):
    monkeypatch.setattr(main, "POST_MODE", "mock")
    data = client.get("/api/accounts").json()
    names = {s["name"] for s in data["caption_styles"]}
    assert SEED_STYLES <= names
    assert data["default_caption_style"] == "generic"
    for s in data["caption_styles"]:
        assert s["display_name"]


def test_generate_endpoint_passes_style(client, monkeypatch):
    seen = {}

    async def fake_generate(media_type, topic, style, *args, **kwargs):
        seen.update(media_type=media_type, topic=topic, style=style)
        return "ok"

    monkeypatch.setattr(main, "generate_caption", fake_generate)
    resp = client.post(
        "/api/generate-caption",
        data={"media_type": "video", "topic": "t", "style": "sports"},
    )
    assert resp.status_code == 200
    assert seen["style"] == "sports"


def test_generate_endpoint_unknown_style_is_400(client, monkeypatch):
    async def fake_generate(media_type, topic, style, *args, **kwargs):
        raise ValueError(f"Unknown caption style '{style}'. Available: generic")

    monkeypatch.setattr(main, "generate_caption", fake_generate)
    resp = client.post(
        "/api/generate-caption",
        data={"media_type": "video", "style": "nope"},
    )
    assert resp.status_code == 400
    assert "Unknown caption style" in resp.json()["detail"]


# --- per-slot regeneration (2026-07-18 feature) ------------------------------

def test_regenerate_prompt_carries_avoid_and_feedback(capturing_anthropic):
    """Anti-repeat: the previous caption and user feedback both reach the
    model so a regeneration can actually differ."""
    asyncio.run(captions.generate_caption(
        "video", "big dunk", style="sports",
        avoid_caption="Old caption text here", feedback="shorter, punchier",
    ))
    user_msg = capturing_anthropic.calls[-1]["messages"][0]["content"]
    assert '"Old caption text here"' in user_msg
    assert "meaningfully different" in user_msg
    assert "Revision guidance from the user: shorter, punchier" in user_msg


def test_plain_generation_prompt_has_no_regen_clauses(capturing_anthropic):
    asyncio.run(captions.generate_caption("video", "big dunk", style="sports"))
    user_msg = capturing_anthropic.calls[-1]["messages"][0]["content"]
    assert "meaningfully different" not in user_msg
    assert "Revision guidance" not in user_msg


def test_generate_endpoint_passes_avoid_and_feedback(client, monkeypatch):
    seen = {}

    async def fake_generate(media_type, topic, style, avoid_caption, feedback, **kwargs):
        seen.update(avoid=avoid_caption, feedback=feedback)
        return "ok"

    monkeypatch.setattr(main, "generate_caption", fake_generate)
    client.post("/api/generate-caption", data={
        "media_type": "video",
        "style": "sports",
        "avoid_caption": "prev cap",
        "feedback": "funnier",
    })
    assert seen == {"avoid": "prev cap", "feedback": "funnier"}


def test_frontend_regenerate_wiring(frontend_src):
    html = frontend_src
    assert "regenerateCaption" in html
    assert "undoCaption" in html
    assert "avoid_caption" in html
    assert "feedback_" in html
    # global Generate fills empty captions only
    assert "s.filename && !s.caption.trim()" in html


# --- frontend wiring (source-level) ------------------------------------------

def test_frontend_has_per_slot_style_selector(frontend_src):
    html = frontend_src
    assert "Caption style" in html
    assert "styleOptions" in html
    assert "formData.append('style'" in html
    assert "default_caption_style" in html
