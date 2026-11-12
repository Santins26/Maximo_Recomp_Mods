"""
Whole-GUI settings profiles for the Maximo: Ghosts to Glory randomizer.

Everything the user can set in the randomizer window lives in a `tk.Variable`
hanging off `RandomizerApp` (plus the two nested dicts `skill_vars` and
`world_vars`). This module snapshots those variables into JSON and applies them
back, so a whole configuration — including the ISO / folder paths and the seed —
can be saved, exported, shared, and auto-loaded on startup.

Collection is done by INTROSPECTION rather than a hand-written key list: any
option added to the GUI in future is picked up automatically with no extra work
here.

Version tolerance is the point of the design, in both directions:
  - a key in the profile that this build no longer has  -> skipped + counted
  - a var in this build the profile never mentions      -> LEFT AT ITS DEFAULT
  - a value of the wrong type                           -> coerced when that is
    unambiguous, otherwise skipped + counted; one bad key never aborts a load.

Layout on disk (mirrors spawn_config.py: next to the exe / cwd):

  randomizer_profiles/<slot>.json     one file per named slot
  randomizer_profiles/_last.json      pointer: last-used slot + auto-load flag
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import tkinter as tk

# Bumped when the on-disk shape changes in a way loaders must know about.
PROFILE_VERSION = 1

# Marker so a foreign JSON file can be rejected outright instead of being
# half-applied over the user's settings.
TOOL_MARKER = "maximo-randomizer-settings-profile"

# Profiles directory name (created lazily, next to the exe / cwd — the same
# convention spawn_config.py uses for spawn_config.json).
PROFILES_DIRNAME = "randomizer_profiles"

# Pointer file holding {"last_slot": ..., "auto_load": bool}.
POINTER_FILENAME = "_last.json"

# Nested dict-of-vars attributes handled explicitly (introspection only walks
# plain attributes).
NESTED_VAR_DICTS = ("skill_vars", "world_vars")

# The profile system's own controls are not randomizer settings, so they are
# never stored in (or restored from) a profile.
EXCLUDED_ATTRS = ("profile_slot_var", "profile_autoload_var")


class ProfileError(Exception):
    """Raised for a file that is not one of our profiles at all."""


# ---------------------------------------------------------------------------
# Paths / slots
# ---------------------------------------------------------------------------
def profiles_dir() -> Path:
    """Directory holding the named slots. Not created until a write happens."""
    return Path.cwd() / PROFILES_DIRNAME


def sanitize_slot_name(name: str) -> str:
    """Turn a user-typed slot name into a safe filename stem."""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:64] or "profile"


def slot_path(name: str) -> Path:
    return profiles_dir() / f"{sanitize_slot_name(name)}.json"


def list_slots() -> list[str]:
    """Slot names currently on disk, sorted. Never raises."""
    d = profiles_dir()
    try:
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json")
                      if p.name != POINTER_FILENAME)
    except OSError as exc:
        print(f"[profiles] could not list {d}: {exc}")
        return []


def delete_slot(name: str) -> bool:
    """Delete a slot file. Returns True when something was removed."""
    p = slot_path(name)
    try:
        if p.is_file():
            p.unlink()
            print(f"[profiles] deleted slot '{p.stem}'")
            return True
    except OSError as exc:
        print(f"[profiles] could not delete {p}: {exc}")
    return False


# ---------------------------------------------------------------------------
# Collection (introspection)
# ---------------------------------------------------------------------------
def collect_vars(app) -> dict[str, object]:
    """Every settable variable on `app`, keyed by a profile key.

    Plain attributes that are `tk.Variable` instances use their attribute name;
    the nested dicts use "<attr>.<inner key>" so they stay flat in the JSON.
    """
    out: dict[str, object] = {}
    for name, value in vars(app).items():
        if isinstance(value, tk.Variable) and name not in EXCLUDED_ATTRS:
            out[name] = value
    for dict_attr in NESTED_VAR_DICTS:
        nested = getattr(app, dict_attr, None)
        if not isinstance(nested, dict):
            continue
        for key, var in nested.items():
            if isinstance(var, tk.Variable):
                out[f"{dict_attr}.{key}"] = var
    return out


def snapshot(app) -> dict:
    """Current GUI state as a JSON-serializable profile dict."""
    settings: dict[str, object] = {}
    for key, var in collect_vars(app).items():
        try:
            settings[key] = var.get()
        except Exception as exc:  # a var mid-edit can hold a bad literal
            print(f"[profiles] skipping '{key}' while saving: {exc}")
    return {
        "tool": TOOL_MARKER,
        "version": PROFILE_VERSION,
        "settings": settings,
    }


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------
_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off", ""}


class _Skip(Exception):
    """Value cannot be applied to this variable safely."""


def _coerce(var: tk.Variable, value):
    """Coerce `value` for `var`, or raise _Skip when it is not safe."""
    if isinstance(value, (list, dict)):
        raise _Skip("container value")

    if isinstance(var, tk.BooleanVar):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE_WORDS:
                return True
            if low in _FALSE_WORDS:
                return False
        raise _Skip("not boolean-like")

    if isinstance(var, tk.IntVar):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and float(value).is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                raise _Skip("not an integer")
        raise _Skip("not an integer")

    if isinstance(var, tk.DoubleVar):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                raise _Skip("not a number")
        raise _Skip("not a number")

    # StringVar (and anything else): every scalar has an unambiguous text form.
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    raise _Skip("not a scalar")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class LoadResult:
    """One-line-summarizable outcome of applying a profile."""

    def __init__(self):
        self.applied = 0
        self.unknown = 0
        self.bad = 0
        self.defaulted = 0
        self.version = None

    @property
    def skipped(self) -> int:
        return self.unknown + self.bad

    def summary(self) -> str:
        parts = [f"{self.applied} applied"]
        if self.unknown:
            parts.append(f"{self.unknown} unknown skipped")
        if self.bad:
            parts.append(f"{self.bad} invalid skipped")
        parts.append(f"{self.defaulted} left at default")
        return ", ".join(parts)

    def __repr__(self) -> str:  # handy in tests / logs
        return f"<LoadResult {self.summary()}>"


def apply_profile(app, data: dict) -> LoadResult:
    """Apply a profile dict to `app`'s variables. Tolerant by design."""
    if not isinstance(data, dict):
        raise ProfileError("profile is not a JSON object")
    if data.get("tool") != TOOL_MARKER:
        raise ProfileError("this file was not written by the randomizer's "
                           "settings-profile system")
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise ProfileError("profile has no settings block")

    res = LoadResult()
    try:
        res.version = int(data.get("version"))
    except (TypeError, ValueError):
        res.version = None
    if res.version is not None and res.version > PROFILE_VERSION:
        print(f"[profiles] profile version {res.version} is newer than this "
              f"build's {PROFILE_VERSION}; unknown keys will be skipped")

    known = collect_vars(app)
    for key, value in settings.items():
        var = known.get(key)
        if var is None:
            res.unknown += 1
            print(f"[profiles] unknown setting '{key}' skipped")
            continue
        try:
            var.set(_coerce(var, value))
        except _Skip as exc:
            res.bad += 1
            print(f"[profiles] setting '{key}' skipped ({exc})")
        except Exception as exc:
            res.bad += 1
            print(f"[profiles] setting '{key}' skipped ({exc})")
        else:
            res.applied += 1

    # ALWAYS-ON SETTINGS ARE RE-PINNED AFTER APPLYING, because a profile written
    # before they became always-on carries the OLD value and would silently
    # switch the feature back off. The raised on-screen object budget has no GUI
    # control any more, so there would be no way to notice or undo it.
    # `randomizer.cli --object-budget vanilla` is the only remaining way off.
    # Keys are ATTRIBUTE NAMES, exactly as `collect_vars` emits them.
    _pinned = {"object_budget_var": "more"}
    for key, pinned_value in _pinned.items():
        var = known.get(key)
        if var is None:
            continue
        try:
            if var.get() != pinned_value:
                print(f"[profiles] setting '{key}' is always-on in this build; "
                      f"pinned to {pinned_value!r} (profile said "
                      f"{var.get()!r})")
                var.set(pinned_value)
        except Exception:      # noqa: BLE001 - a pin must never fail a load
            pass

    # Anything this build has that the profile never mentioned keeps whatever
    # value it already holds — that is the "older profile" case.
    res.defaulted = sum(1 for key in known if key not in settings)
    if res.defaulted:
        print(f"[profiles] {res.defaulted} setting(s) not present in the "
              f"profile were left at their current values")
    return res


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------
def save_profile(app, path: str | Path) -> Path:
    """Write a snapshot of `app` to `path` (parents created as needed)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snapshot(app), indent=2))
    print(f"[profiles] saved settings profile -> {p}")
    return p


def read_profile(path: str | Path) -> dict:
    """Read+parse a profile file. Raises ProfileError on anything unusable."""
    p = Path(path)
    try:
        raw = p.read_text()
    except OSError as exc:
        raise ProfileError(f"could not read {p.name}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ProfileError(f"{p.name} is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{p.name} is not a settings profile")
    return data


def load_profile(app, path: str | Path) -> LoadResult:
    """Read `path` and apply it. Raises ProfileError for a rejected file; a
    single bad KEY inside an otherwise valid profile never raises."""
    res = apply_profile(app, read_profile(path))
    print(f"[profiles] loaded {Path(path).name}: {res.summary()}")
    return res


def save_slot(app, name: str) -> Path:
    return save_profile(app, slot_path(name))


def load_slot(app, name: str) -> LoadResult:
    return load_profile(app, slot_path(name))


# ---------------------------------------------------------------------------
# Auto-load pointer
# ---------------------------------------------------------------------------
def pointer_path() -> Path:
    return profiles_dir() / POINTER_FILENAME


def read_pointer() -> dict:
    """{"last_slot": str|None, "auto_load": bool}. Never raises."""
    p = pointer_path()
    try:
        if p.is_file():
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                slot = data.get("last_slot")
                return {
                    "last_slot": slot if isinstance(slot, str) and slot else None,
                    "auto_load": bool(data.get("auto_load", False)),
                }
    except (OSError, ValueError) as exc:
        print(f"[profiles] ignoring unreadable {p.name}: {exc}")
    return {"last_slot": None, "auto_load": False}


def write_pointer(last_slot: str | None, auto_load: bool) -> None:
    """Record the active slot + auto-load flag. Never raises."""
    p = pointer_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "last_slot": sanitize_slot_name(last_slot) if last_slot else None,
            "auto_load": bool(auto_load),
        }, indent=2))
    except OSError as exc:
        print(f"[profiles] could not update {p.name}: {exc}")


def autoload(app) -> tuple[str | None, LoadResult | None]:
    """Apply the auto-load slot when enabled.

    Returns (slot_name, result). Never raises — a corrupt or unreadable profile
    must not stop the GUI from starting.
    """
    try:
        ptr = read_pointer()
        if not ptr["auto_load"] or not ptr["last_slot"]:
            return (ptr["last_slot"], None)
        slot = ptr["last_slot"]
        p = slot_path(slot)
        if not p.is_file():
            print(f"[profiles] auto-load slot '{slot}' is missing; "
                  f"starting with defaults")
            return (slot, None)
        return (slot, load_profile(app, p))
    except ProfileError as exc:
        print(f"[profiles] auto-load skipped: {exc}")
    except Exception as exc:
        print(f"[profiles] auto-load failed: {exc}")
    return (None, None)
