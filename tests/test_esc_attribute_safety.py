"""Issue #41: ``esc()`` does not escape quotes but is used in HTML attribute
contexts.

``esc()`` escapes via ``textContent -> innerHTML``, which the HTML spec defines
as escaping ``& < >`` only — **not** ``"`` or ``'``. That is correct for text
content, but it was also used inside two attribute shapes where an unescaped
quote can break out of the attribute and inject markup or rewrite an
``onclick`` handler:

* ``<option value="${...}">`` — a double-quoted attribute, and
* ``onclick="cancelQueueBatch('${...}')"`` — a single-quoted JS string inside a
  double-quoted attribute (the same shape ``deleteQueueMedia`` uses).

The fix adds a separate ``escAttr()`` helper that also escapes both quotes, and
documents ``esc()`` as text-content-only.

These are **source-level assertions**, in the established style of
``test_frontend_robustness.py`` and ``test_ui_polish.py``. The frontend is one
vanilla HTML/JS file with no build step, and the project forbids real-browser
E2E tests, so there is no DOM available to run ``esc`` against. Instead we pin
the two properties that make the attribute safe — that ``escAttr`` performs the
two quote replacements and ``esc`` does not — at the source. If a future change
drops either replacement, or routes an attribute site back through ``esc``,
these fail.

This is not a live vulnerability today (batch IDs are ``uuid4().hex`` and style
names are maintainer-authored); the safety lives in other files, which is
exactly why a regression test is warranted.
"""

from tests.test_frontend_robustness import _function_body, _script

# A payload carrying every character that could close an attribute or open a
# tag. The exact bytes don't matter to a source-level test, but writing it down
# keeps the threat model the tests guard against legible at a glance.
PAYLOAD = "a'\"<b onclick=evil()>"


# --- the split: esc is text-only, escAttr adds the quotes -------------------


def test_esc_is_text_content_only_and_does_not_escape_quotes():
    """The contract: ``esc()`` escapes ``& < >`` (via textContent -> innerHTML)
    but must NOT escape quotes — it is for text content, not attributes. If this
    ever starts escaping quotes, the split with ``escAttr`` is wrong and the
    contract comment is stale; reconcile them."""
    body = _function_body("esc")
    # The browser round-trip esc() relies on is the only escaping it does...
    assert "textContent" in body and "innerHTML" in body
    # ...so a quote replacement inside esc() would mean it has grown a second
    # job and the contract comment no longer describes it.
    assert ".replace(" not in body, "esc() must not hand-escape quotes (use escAttr)"


def test_esc_contract_comment_states_attribute_safety():
    """Acceptance criterion: the next reader must know whether ``esc()`` is safe
    for attributes. Pin the contract comment so a cleanup that strips it (and
    re-hides the gap) fails here."""
    body = _function_body("esc")
    assert "attribute" in body.lower(), (
        "esc() must state in a comment whether it is safe inside an attribute"
    )
    assert "escAttr" in body, "esc() must point at escAttr() for attribute sites"


def test_esc_attr_escapes_both_quote_characters():
    """The fix: ``escAttr`` must neutralise both ``"`` and ``'`` so a value
    cannot break out of an attribute delimited by either quote style. This is
    the core acceptance criterion — both replacements must be present, by name,
    so dropping either is caught."""
    body = _function_body("escAttr")
    assert ".replace(/\"/g, '&quot;')" in body, (
        "escAttr must escape the double quote (the option-value breakout vector)"
    )
    assert ".replace(/'/g, '&#39;')" in body, (
        "escAttr must escape the single quote (the onclick JS-string vector)"
    )
    # It must build on esc()'s & < > handling rather than re-deriving it, so a
    # future change to the tag-char escaping flows through both helpers.
    assert "esc(" in body


# --- the wiring: every attribute site routes through escAttr ----------------


def test_option_value_uses_esc_attr():
    """Attribute context 1: ``<option value="...">`` in styleOptions(). A style
    name carrying a quote must not break out of the value attribute."""
    body = _function_body("styleOptions")
    assert 'value="${escAttr(st.name)}"' in body, (
        "the option value attribute must use escAttr, not esc"
    )
    # The display name is text content *between* the tags, so esc stays correct
    # there — pin it so a blanket s/esc/escAttr/ migration is caught.
    assert "${esc(st.display_name)}" in body


def test_onclick_js_strings_use_esc_attr():
    """Attribute context 2: single-quoted JS strings inside double-quoted
    onclick attributes. A quote in the value would otherwise close the JS string
    and let the rest of the value run as code. Both onclick sites must use it."""
    script = _script()
    assert "cancelQueueBatch('${escAttr(b.id)}')" in script, (
        "cancelQueueBatch onclick must wrap its argument in escAttr"
    )
    # The deleteQueueMedia onclick is the same shape and the same latent gap;
    # it is not named in the issue text but is fixed alongside for completeness.
    assert "deleteQueueMedia('${escAttr(s.batch_id)}'" in script
    assert "${escAttr(s.reason || '')}" in script
    # And neither onclick site should still reach for the text-only helper.
    assert "cancelQueueBatch('${esc(" not in script
    assert "deleteQueueMedia('${esc(" not in script
