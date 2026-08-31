"""Local account discovery and versioned UI state.

The state file lives below the gitignored sessions root.  It contains only
account-selection metadata; session credentials and queue contents never pass
through this module.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


SCHEMA_VERSION = 1
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ROSTER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


class AccountStateError(ValueError):
    """Local state is invalid and must not be guessed around."""


@dataclass(frozen=True)
class DiscoveredAccount:
    account_id: str
    display_name: str
    instagram: bool
    tiktok: bool


@dataclass
class AccountState:
    schema_version: int = SCHEMA_VERSION
    active_account_ids: list[str] = field(default_factory=list)
    rosters: dict[str, list[str]] = field(default_factory=dict)
    caption_defaults: dict[str, str] = field(default_factory=dict)
    device_profiles: dict[str, int] = field(default_factory=dict)


def validate_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id):
        raise AccountStateError(
            f"Invalid account id {account_id!r}; use letters, digits, '_' or '-' only."
        )
    return account_id


def _has_entries(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def discover_accounts(
    instagram_root: Path,
    tiktok_root: Path,
    compatibility_ids: list[str],
    display_names: dict[str, str] | None = None,
) -> list[DiscoveredAccount]:
    """Discover the platform-first layout plus supported legacy TikTok files."""
    display_names = display_names or {}
    ids: set[str] = set(compatibility_ids)
    ig_ids: set[str] = set()
    tt_ids: set[str] = set()

    if instagram_root.is_dir():
        for path in instagram_root.iterdir():
            if path.is_dir() and not path.is_symlink() and ACCOUNT_ID_RE.fullmatch(path.name):
                ids.add(path.name)
                if _has_entries(path):
                    ig_ids.add(path.name)

    if tiktok_root.is_dir():
        for path in tiktok_root.iterdir():
            if path.is_dir() and not path.is_symlink() and ACCOUNT_ID_RE.fullmatch(path.name):
                ids.add(path.name)
                if _has_entries(path) or (path / "cookies.json").is_file():
                    tt_ids.add(path.name)
            elif path.is_file() and not path.is_symlink() and path.name.endswith("_cookies.json"):
                account_id = path.name.removesuffix("_cookies.json")
                if ACCOUNT_ID_RE.fullmatch(account_id) and path.stat().st_size > 10:
                    ids.add(account_id)
                    tt_ids.add(account_id)

    ordered = list(dict.fromkeys(compatibility_ids))
    ordered.extend(sorted(ids - set(ordered), key=str.casefold))
    return [
        DiscoveredAccount(
            account_id=account_id,
            display_name=display_names.get(account_id, account_id),
            instagram=account_id in ig_ids,
            tiktok=account_id in tt_ids,
        )
        for account_id in ordered
    ]


class AccountStateStore:
    def __init__(
        self,
        path: Path,
        known_ids: list[str],
        style_ids: set[str],
        capacity: int,
        instagram_ids: set[str] | None = None,
        compatibility_ids: list[str] | None = None,
    ):
        self.path = path
        self.known_ids = list(dict.fromkeys(known_ids))
        self.style_ids = set(style_ids)
        self.capacity = capacity
        # `None` preserves the original all-Instagram assumption for focused
        # callers and older tests. Runtime discovery passes the explicit set,
        # so TikTok-only accounts never consume an Instagram fingerprint.
        self.instagram_ids = (
            set(self.known_ids) if instagram_ids is None else set(instagram_ids)
        )
        self.compatibility_ids = list(dict.fromkeys(
            self.known_ids if compatibility_ids is None else compatibility_ids
        ))
        if any(account_id not in self.known_ids for account_id in self.compatibility_ids):
            raise AccountStateError("Compatibility defaults reference an unknown account.")

    def default_state(self) -> AccountState:
        # Discovery must never silently activate a newly found session folder.
        # Missing state reproduces only the configured compatibility roster;
        # discovered accounts wait for an explicit Accounts selection.
        active = list(self.compatibility_ids)
        profiles = {
            account_id: compatibility_index
            for compatibility_index, account_id in enumerate(self.compatibility_ids)
            if account_id in self.instagram_ids
        }
        return AccountState(active_account_ids=active, device_profiles=profiles)

    def load(self) -> AccountState:
        if not self.path.exists():
            return self.default_state()
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict) or "schema_version" not in raw:
                raise AccountStateError("Account state is missing its schema version.")
            state = AccountState(**raw)
            self.validate(state)
            return state
        except Exception as exc:
            if isinstance(exc, AccountStateError):
                raise
            raise AccountStateError(f"Account state is corrupt: {exc}") from exc

    def validate(self, state: AccountState) -> None:
        if state.schema_version != SCHEMA_VERSION:
            raise AccountStateError(
                f"Unsupported account-state schema {state.schema_version}; expected {SCHEMA_VERSION}."
            )
        known = set(self.known_ids)
        self._validate_ids("active accounts", state.active_account_ids, known)
        active_instagram = [
            account_id
            for account_id in state.active_account_ids
            if account_id in self.instagram_ids
        ]
        if len(active_instagram) > self.capacity:
            raise AccountStateError(
                f"Active roster has {len(active_instagram)} Instagram accounts but only "
                f"{self.capacity} distinct Instagram device profiles exist."
            )
        for name, ids in state.rosters.items():
            if not isinstance(name, str) or not ROSTER_NAME_RE.fullmatch(name):
                raise AccountStateError(f"Invalid roster name {name!r}.")
            self._validate_ids(f"roster {name!r}", ids, known)
            instagram_count = sum(account_id in self.instagram_ids for account_id in ids)
            if instagram_count > self.capacity:
                raise AccountStateError(
                    f"Roster {name!r} exceeds Instagram device-profile capacity."
                )
        for account_id, style in state.caption_defaults.items():
            if account_id not in known:
                raise AccountStateError(f"Caption default references unknown account {account_id!r}.")
            if style not in self.style_ids:
                raise AccountStateError(f"Unknown caption style selected for account {account_id!r}.")
        used: dict[int, str] = {}
        for account_id, profile in state.device_profiles.items():
            if account_id not in known:
                raise AccountStateError(f"Device assignment references unknown account {account_id!r}.")
            if not isinstance(profile, int) or isinstance(profile, bool) or not 0 <= profile < self.capacity:
                raise AccountStateError(f"Invalid device profile for account {account_id!r}.")
            if profile in used:
                raise AccountStateError(
                    f"Accounts {used[profile]!r} and {account_id!r} share device profile {profile}."
                )
            used[profile] = account_id

    @staticmethod
    def _validate_ids(label: str, ids: list[str], known: set[str]) -> None:
        if not isinstance(ids, list):
            raise AccountStateError(f"{label} must be an ordered list.")
        if len(ids) != len(set(ids)):
            raise AccountStateError(f"{label} contains a duplicate account id.")
        if len(ids) != len({account_id.casefold() for account_id in ids}):
            raise AccountStateError(f"{label} contains case-colliding account ids.")
        unknown = [account_id for account_id in ids if account_id not in known]
        if unknown:
            raise AccountStateError(f"{label} references unknown account(s): {', '.join(unknown)}.")
        for account_id in ids:
            validate_account_id(account_id)

    def reconcile_device_profiles(self, state: AccountState) -> AccountState:
        """Drop assignments created by the old all-platform allocation rule."""
        state.device_profiles = {
            account_id: profile
            for account_id, profile in state.device_profiles.items()
            if account_id in self.instagram_ids
        }
        return state

    def assign_active_profiles(self, state: AccountState) -> AccountState:
        """Assign unused profiles without moving any existing account."""
        self.reconcile_device_profiles(state)
        used = set(state.device_profiles.values())
        available = [i for i in range(self.capacity) if i not in used]
        for account_id in state.active_account_ids:
            if account_id not in self.instagram_ids:
                continue
            if account_id not in state.device_profiles:
                if not available:
                    raise AccountStateError(
                        f"No distinct Instagram device profile is available for {account_id!r}."
                    )
                state.device_profiles[account_id] = available.pop(0)
        self.validate(state)
        return state

    def save(self, state: AccountState) -> AccountState:
        self.reconcile_device_profiles(state)
        self.validate(state)
        self.assign_active_profiles(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        try:
            with open(tmp, "w") as handle:
                json.dump(asdict(state), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        return state
