"""TEMPORARY — deliberately failing test for #72.

Exists only to make CI red so we can confirm the `main` ruleset actually
blocks a merge now that the repository is public. Rulesets are stored but not
enforced on a private Free repo, so this could not be verified before the
visibility flip.

This file and its branch are deleted as soon as the check is recorded. If you
are reading this on `main`, something went wrong — delete it.
"""


def test_deliberate_failure_to_make_ci_red():
    assert False, "intentional failure for the #72 ruleset enforcement probe"
