"""Source-level UI contracts for Accounts, exact targets, and lightweight Stats."""

from tests.test_frontend_robustness import _function_body, _html, _script


def test_accounts_workspace_has_accessible_roster_and_order_controls():
    html = _html()
    for element_id in ("accountsList", "rosterSelect", "rosterName", "accountStateError"):
        assert f'id="{element_id}"' in html
    script = _script()
    for function in (
        "renderAccounts", "persistAccountState", "applyActiveAccounts",
        "toggleAccount", "moveAccount", "saveRoster", "selectRoster",
        "renameRoster", "deleteRoster", "setCaptionDefault",
    ):
        assert f"function {function}" in script
    assert "Move ${escAttr(account.name)} earlier" in script
    assert "Caption default for ${escAttr(account.name)}" in script


def test_roster_switch_warns_with_affected_draft_account_names():
    body = _function_body("applyActiveAccounts")
    assert "drafts.map" in body
    assert "names.join(', ')" in body
    assert "?.name || id} [${id}]" in body
    assert "will discard the current browser draft" in body
    assert "confirm(" in body
    assert "draft.file || draft.filename" in body


def test_removed_pending_upload_cannot_resume_into_destroyed_account_card():
    body = _function_body("handleFile")
    assert body.count("state.slots[slot] !== s") >= 2
    assert "state.slots[slot] === s && s.file === file" in body


def test_payload_contains_only_active_immutable_account_ids():
    body = _function_body("buildSlotsPayload")
    assert "new Set(state.accounts.map(a => a.slot))" in body
    assert "activeIds.has(slot)" in body
    assert "slot," in body


def test_post_and_schedule_confirm_exact_names_and_platforms():
    summary = _function_body("targetSummary")
    assert "account?.name || slot.slot" in summary
    assert "Instagram" in summary and "TikTok" in summary
    assert "for exactly:" in summary
    assert "[${slot.slot}]" in summary
    assert 'class="slot-account-id"' in _function_body("renderSlots")
    assert "if (!confirmTargets('Post All')) return;" in _function_body("postAll")
    assert "if (!confirmTargets('Confirm Schedule')) return;" in _function_body("scheduleAll")
    assert "fetch('/api/post'" in _function_body("postAll")
    assert "fetch('/api/queue'" in _function_body("scheduleAll")


def test_stats_is_read_only_and_labels_media_tracking_limit():
    assert 'id="statsPanel"' in _html()
    body = _function_body("loadStats")
    assert "fetchWithTimeout('/api/stats')" in body
    assert "method:" not in body
    assert "Since tracking began" in body
    for forbidden in ("chart", "trend", "goal", "lifetime queued"):
        assert forbidden not in body.lower()


def test_accounts_and_stats_narrow_layout_stack_without_overflow():
    html = _html()
    assert ".account-row { grid-template-columns:1fr;" in html
    assert ".stats-grid { grid-template-columns:1fr;" in html
    assert "overflow-wrap:anywhere" in html


def test_untrusted_account_and_roster_text_is_escaped():
    body = _function_body("renderAccounts")
    assert "esc(account.name)" in body
    assert "escAttr(account.slot)" in body
    assert "escAttr(name)" in body and "esc(name)" in body


def test_account_controls_are_named_sized_and_restore_keyboard_focus():
    html = _html()
    body = _function_body("renderAccounts")
    assert ".account-active { min-height:44px" in html
    assert "Set ${escAttr(account.name)} [${escAttr(account.slot)}] active" in body
    assert "focusAccountId" in body and "focusControl" in body
    assert "queueMicrotask" in body and ".focus()" in body


def test_session_strip_and_roster_selection_follow_applied_account_state():
    apply_body = _function_body("applyActiveAccounts")
    select_body = _function_body("selectRoster")
    assert "renderSessionStrip();" in apply_body
    assert "const applied = await applyActiveAccounts" in select_body
    assert "state.selectedRoster = applied ? name : previous" in select_body


def test_roster_rerender_hydrates_retained_review_drafts():
    """Cold-review repair (HIGH): visible cards must match postable retained state."""
    render = _function_body("renderSlots")
    hydrate = _function_body("hydrateSlotCard")
    preview = _function_body("renderSlotPreview")
    assert "hydrateSlotCard(slot);" in render
    assert "topic.value = s.topic" in hydrate
    assert "caption.value = s.caption" in hydrate
    assert "updateThumbChip(slot)" in hydrate
    assert "renderSlotPreview(slot)" in hydrate
    assert "s.filename" in preview and "s.file" in preview


def test_confirmation_names_both_platforms_when_no_sessions_are_saved():
    """Cold-review repair (MEDIUM): empty availability still names intended platforms."""
    summary = _function_body("targetSummary")
    assert "no saved Instagram or TikTok session" in summary


def test_tiktok_only_accounts_do_not_count_against_instagram_device_capacity():
    accounts = _function_body("renderAccounts")
    apply = _function_body("applyActiveAccounts")
    assert "not required (TikTok only)" in accounts
    assert "account?.instagram_device_required" in apply
    assert "Object.keys(assignedProfiles).length + newInstagramAccounts.length" in apply
    assert "ids.length > state.deviceProfileCapacity" not in apply
