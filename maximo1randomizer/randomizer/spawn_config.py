"""
Editable spawn-rate configuration for the Maximo: Ghosts to Glory randomizer.

This module turns the randomizer's hard-coded weight tables into a user-editable
config, organized by TAG (enemies / items / structures) and, for enemies, split
WORLD-by-WORLD. Each entry carries a weight (0-100) and an enabled flag; a
disabled entry (or weight 0) is removed from every spawn pool so it never
appears in the randomized game.

The config is JSON-serializable so the GUI can save/load it, and exposes helper
methods that produce the exact weight dicts the randomizer pipeline consumes:

  - universal_weights(world): weights for randomize_universal()'s item pool
        (items + structures + the enemies available in that world).
  - enemy_weights_for_world(world): per-enemy weights for the cli.py even-out
        balancing bag.
  - disabled_enemies(world): enemy type-ids to drop from a world's pool.

Defaults are derived from items.DEFAULT_ITEM_WEIGHTS, scaled so the largest
entry maps to 100. Because the randomizer only cares about RELATIVE weights,
the default config reproduces the stock spawn behavior exactly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .items import (
    DEFAULT_ITEM_WEIGHTS,
    ENEMY_DESTINATIONS,
    ENEMY_SHARE_TARGET as _ITEMS_ENEMY_SHARE_TARGET,
    ENEMY_TIER_TYPES_PER_WORLD,
    ENEMY_TIER_WEIGHTS_PER_TYPE,
    UNIVERSAL_ITEM_POOL,
    get_item_weights_for_world,
)
from .elf_patch import ABILITY_DROP_EDITABLE, ABILITY_DROP_WEIGHT_MAX
from . import roster_costs as _roster_costs
from .catalog import (
    CROSS_WORLD_STRUCTURE_ALLOWLIST,
    load_structure_allowlist,
    GRAVE_WALKING_MELEE,
    UNDER_WALKING_MELEE,
    SWAMP_WALKING_MELEE,
    ICE_WALKING_MELEE,
    CASTLE_WALKING_MELEE,
    WALKING_MELEE_WEIGHTS,
    CROSS_WORLD_ALLOWLIST,
    HELD_BACK_ON_OBSERVED_CRASH,
    is_held_back_on_observed_crash,
    load_allowlist,
)

#: Bumped from 1 when the CROSS-WORLD ARRIVALS section was added, and from 2
#: when CROSS-WORLD STRUCTURE ARRIVALS were added. Old files (any earlier
#: version, or no version at all) still load: `from_dict` starts from
#: `default()` and overlays, so a missing section means DEFAULTS, never a crash.
#: Bumped to 4 when the UNIFIED weight model (one table for native types,
#: enemy arrival routes and structure arrival routes) was added. Every earlier
#: file still loads: `from_dict` starts from `default()` and overlays, so a
#: missing `unified` section means DEFAULTS, never a crash, and every legacy
#: section (`items` / `structures` / `enemies` / `arrivals` /
#: `structure_arrivals`) is still read and still written exactly as in
#: version 3. A version-3 file therefore loads unchanged; the real table-by-
#: table migration into the unified table is a SEPARATE step (task 2).
CONFIG_VERSION = 4

# Default config file name (lives next to the exe / cwd).
DEFAULT_CONFIG_FILENAME = "spawn_config.json"

# ---------------------------------------------------------------------------
# Type classification + display names
# ---------------------------------------------------------------------------
# Enemies (walking-melee roster). These are configured per-world; only the
# subset available in a world is shown/used there.
ENEMY_TYPES: dict[int, str] = {
    0x47: "Basic Skeleton",
    0x2F: "Axe Skeleton",
    0x39: "Sword Skeleton",
    0x54: "Guard Skeleton",
    0x49: "Bomb Skeleton",
    0x3A: "Pirate Skeleton",
    0x48: "Basic Zombie",
    0x6A: "Torso Zombie",
    0x10: "Frozen Zombie",
    0x19: "Swamp Zombie",
    0x0D: "Zombie Crocodile",
    0x6B: "Ghost / Poltergeist",
    0x37: "Raven",
    0x1D: "Snowman",
    0x13: "Goat Devil",
    0x27: "Hammer Devil",
    0x5D: "Doomed Soul",
    0x08: "Plant Monster",
    0x53: "Dark Knight",
    0x58: "Crazed Prisoner",
    0x16: "Axe Guard",
}

# Items (pickups + chests + ability/skill pickups).
ITEM_TYPES: dict[int, str] = {
    0x21: "Gold Coin",
    0x46: "Bag of Gold",
    0x52: "Gem",
    0x25: "Wooden Chest",
    0x5B: "Locked Chest",
    0x65: "Skeleton Key",
    0x45: "Armor Power-Up",
    0x51: "Full Health",
    0x4E: "Extra Life",
    0x24: "Collector",
    0x20: "Ability / Skill",
}

# Structures (smashables, decorations, spawners).
STRUCTURE_TYPES: dict[int, str] = {
    0x0A: "Popup Headstone",
    0x01: "Bone Tower",
    0x02: "Bonus Grave",
    0xB0: "Smashable Torch",
    0x30: "Grave Smashable Glass",
    0xA0: "Coffin Lid",
    0xC9: "Breakable Rockwall",
    0xAE: "Underworld Lantern",
    0xD3: "Breakable Tiki Torch",
    0x6C: "Coin Container",
    0xC8: "Spirit Statue",
    0xB5: "Swamp Coffin Lid",
    0xBB: "Thorn Vine",
    0x17: "Smashable Snow Pirate",
    0xB1: "Smashable Ice Wall",
    0x7C: "Ice Plant",
    0x1B: "Popup Ice",
    0x18: "Ice Torch",
    0xAA: "Smashable Door",
    0xAD: "Spirit Torch",
    0x4A: "Globe",
    0xBC: "Smashable Coffin",
    0x50: "Standing Coffin",
    0x5F: "Monster Generator",
}

# Ability/skill DROP-table ids (per-level ELF weight tables). Single source of
# names, shared with elf_patch so no id is ever written twice.
ABILITY_DROP_TYPES = ABILITY_DROP_EDITABLE
ABILITY_DROP_DEFAULT_WEIGHT = 10.0

WORLDS = ("grave", "under", "swamp", "ice", "castle")

# Special chest outcomes (not entity types) — controlled via chest content
# tags. Mimic = a chest that's secretly an enemy; Wizard = a chest that spawns
# a wizard. The editor value is a DIRECT PERCENT CHANCE that a given wooden
# chest becomes that type (independent for mimic and wizard).
SPECIAL_NAMES = {
    "mimic": "Mimic chest",
    "wizard": "Wizard chest",
}
SPECIAL_DEFAULTS = {"mimic": 12.0, "wizard": 10.0}  # default % chance per chest

_WORLD_ENEMY_AVAIL: dict[str, set[int]] = {
    "grave": set(GRAVE_WALKING_MELEE),
    "under": set(UNDER_WALKING_MELEE),
    "swamp": set(SWAMP_WALKING_MELEE),
    "ice": set(ICE_WALKING_MELEE),
    "castle": set(CASTLE_WALKING_MELEE),
}


def world_enemy_types(world: str) -> dict[int, str]:
    """Enemy type-id -> name map available in a world (vanilla BEF roster)."""
    avail = _WORLD_ENEMY_AVAIL.get(world, set())
    return {tid: ENEMY_TYPES[tid] for tid in ENEMY_TYPES if tid in avail}


# Default weight for a single enemy class level (tier) when vanilla used an
# even split. Basic_Zombie and any other type in ENEMY_TIER_WEIGHTS_PER_TYPE
# keep their stock bias.
TIER_DEFAULT_EQUAL = 50.0


def world_enemy_tier_types(world: str) -> dict[int, tuple[str, list[int]]]:
    """Tier-eligible enemies in a world -> (name, [valid tier values])."""
    return dict(ENEMY_TIER_TYPES_PER_WORLD.get(world, {}))


def tier_label(tier_value: int) -> str:
    """User-facing label for a tier value (0-indexed internally)."""
    return f"Lv {tier_value + 1}"


# ---------------------------------------------------------------------------
# CROSS-WORLD ARRIVALS
# ---------------------------------------------------------------------------
# The unit here is a ROUTE -- (enemy_id, source_world, target_world) -- not an
# enemy, because the same enemy can arrive in several worlds from different
# sources and each crossing is measured separately.
#
# THIS SECTION NARROWS, IT NEVER GRANTS. Two sources of truth decide what may
# even be OFFERED: `catalog.CROSS_WORLD_ALLOWLIST` (which routes exist, plus
# their `active` flag and the observed-crash hold) and `roster_costs.cost_for`
# (which routes were MEASURED). A route that is inactive, unmeasured or held
# back is reported with its reason and is NOT selectable -- there is no config
# value, hand-edited or otherwise, that can turn it on. `allowlist_lookup`
# stays the single permission oracle; all this produces is a narrower candidate
# set for the seeded draw.
ARRIVAL_UNSELECTABLE_HELD_BACK = ("held back on an OBSERVED IN-GAME CRASH — "
                                  "cannot be enabled")
ARRIVAL_UNSELECTABLE_INACTIVE = ("route is not active in the allowlist — "
                                 "cannot be enabled")
ARRIVAL_UNSELECTABLE_UNMEASURED = ("no measured resident-cost row — "
                                   "cannot be enabled")
ARRIVAL_SELECTABLE = "measured route"

#: Shown next to the section. Enabling arrivals buys VARIETY, not size.
ARRIVAL_BUDGET_NOTE = (
    "Cross-world arrivals are enemies ported INTO a world from another world. "
    "Each row is a ROUTE (enemy + source world), so the same enemy can arrive "
    "from different places. Un-checking a route keeps it out of every seed; "
    "the weight only biases how often an arrival is picked once it is in.\n"
    "IMPORTANT: enabling more routes buys VARIETY, NOT SIZE. The resource-pool "
    "budget bounds the live resident total per draw, so only what FITS arrives "
    "— roughly 6-7 routes in Grave, fewer elsewhere, and Castle affords only "
    "1-2. Enabling ten routes and seeing five arrive is the budget working, "
    "not a bug. Greyed rows cannot be enabled at all (held back on an observed "
    "crash, inactive, or never measured)."
)


@dataclass(frozen=True)
class ArrivalOption:
    """One arrival ROUTE as the editor should show it.

    `selectable` is the ONLY thing the UI may act on, and it is derived here
    from the catalog and the measured cost table rather than from the config,
    so a stored value can never widen it.
    """
    enemy_id: int
    name: str
    source_world: str
    target_world: str
    resident_bytes: int | None
    selectable: bool
    reason: str

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.enemy_id, self.source_world, self.target_world)

    @property
    def route_key(self) -> str:
        return arrival_route_key(self.enemy_id, self.source_world)

    @property
    def label(self) -> str:
        return f"{self.name} (from {self.source_world.capitalize()})"

    @property
    def cost_label(self) -> str:
        if self.resident_bytes is None:
            return "not measured"
        return f"{self.resident_bytes:,} B"


def arrival_route_key(enemy_id: int, source_world: str) -> str:
    """The JSON key for one route inside a target world's table."""
    return f"0x{int(enemy_id):02X}|{source_world}"


def parse_arrival_route_key(key) -> tuple[int, str] | None:
    """Inverse of `arrival_route_key`; None when the key is unparseable."""
    if not isinstance(key, str) or "|" not in key:
        return None
    raw_id, _, source = key.partition("|")
    try:
        return int(raw_id, 16), source
    except (ValueError, TypeError):
        return None


def arrival_options(world: str) -> list[ArrivalOption]:
    """Every allowlisted arrival route INTO `world`, with its verdict.

    Unselectable routes are returned too (with the reason) so the editor can
    grey them and say why — 0x13 Goat_Devil in particular stays visible and
    unreachable rather than quietly vanishing.
    """
    active = load_allowlist()
    out: list[ArrivalOption] = []
    for entry in CROSS_WORLD_ALLOWLIST:
        if entry.target_world != world:
            continue
        row = _roster_costs.cost_for(entry.enemy_id, entry.source_world,
                                    entry.target_world)
        if (entry.key in HELD_BACK_ON_OBSERVED_CRASH
                or is_held_back_on_observed_crash(entry.enemy_id)):
            selectable, reason = False, ARRIVAL_UNSELECTABLE_HELD_BACK
        elif not entry.active or entry.key not in active:
            selectable, reason = False, ARRIVAL_UNSELECTABLE_INACTIVE
        elif row is None:
            selectable, reason = False, ARRIVAL_UNSELECTABLE_UNMEASURED
        else:
            selectable, reason = True, ARRIVAL_SELECTABLE
        out.append(ArrivalOption(
            enemy_id=entry.enemy_id,
            name=ENEMY_TYPES.get(entry.enemy_id,
                                 str(entry.name).replace("_", " ")),
            source_world=entry.source_world,
            target_world=entry.target_world,
            resident_bytes=(int(row.resident_bytes) if row is not None
                            else None),
            selectable=selectable, reason=reason))
    out.sort(key=lambda o: (o.enemy_id, o.source_world))
    return out


def selectable_arrival_keys(world: str) -> set[tuple[int, str, str]]:
    """The route keys the editor is allowed to offer for `world`."""
    return {o.key for o in arrival_options(world) if o.selectable}


def default_arrivals() -> dict:
    """The stock arrivals table: every SELECTABLE route on, at its enemy's
    default weight.

    Kept as a module-level function (rather than inline in `SpawnConfig.
    default`) so the separate cross-world editor's "use stock" button and the
    CLI can both reach the same decision logic without touching a widget.
    """
    out: dict[str, dict] = {}
    for world in WORLDS:
        out[world] = {
            opt.route_key: {"weight": _enemy_default(opt.enemy_id),
                            "enabled": True}
            for opt in arrival_options(world) if opt.selectable
        }
    return out


def reset_arrivals(config) -> None:
    """Put `config.arrivals` back to stock, leaving everything else alone."""
    config.arrivals = default_arrivals()


def arrival_summary(config) -> dict[str, tuple[int, int]]:
    """{world: (enabled_count, selectable_count)} for a status line.

    Both numbers come from the permission oracle, so the "enabled" count can
    never exceed what the catalog and the measured cost table already allow.
    Zero enabled in a world is a clean "no cross-world arrivals here", not an
    error.
    """
    out: dict[str, tuple[int, int]] = {}
    for world in WORLDS:
        offered = len(selectable_arrival_keys(world))
        enabled = len(config.enabled_arrival_keys(world))
        out[world] = (enabled, offered)
    return out


# ---------------------------------------------------------------------------
# CROSS-WORLD STRUCTURE ARRIVALS  (`cross-world-structures` Task 6)
# ---------------------------------------------------------------------------
# The unit is the same ROUTE -- (structure type, source world, target world) --
# and the JSON key is the SAME `arrival_route_key` helper, so one parser serves
# both tables and there is no second key format to keep in step.
#
# TWO DIFFERENCES FROM THE ENEMY TABLE, BOTH DELIBERATE:
#
#   1. EVERY ENTRY DEFAULTS DISABLED (Req 6.2). Enemy arrivals default ON
#      because that is the shipped behaviour of a feature the user already
#      opted into with `--cross-world`. Cross-world structures are EXPERIMENTAL
#      and every route is only CANDIDATE, never CONFIRMED, so enabling the
#      switch alone must change nothing at all until the user ticks a route.
#   2. PERMISSION COMES FROM THE STRUCTURE TABLES. `load_structure_allowlist`
#      and `roster_costs.structure_cost_for` decide what may be OFFERED; a
#      route that is unmeasured, inactive or held back gets NO entry and
#      therefore NO editable control (Req 6.3). `from_dict` only ever overlays
#      onto existing keys, so a hand-edited file cannot invent one.
#
# This section NARROWS and never grants, exactly as the enemy one does. The
# oracle stays `catalog.load_structure_allowlist` + the measured cost table;
# all this produces is a narrower candidate set for the seeded structure draw.
STRUCTURE_ARRIVAL_UNSELECTABLE_HELD_BACK = ARRIVAL_UNSELECTABLE_HELD_BACK
STRUCTURE_ARRIVAL_UNSELECTABLE_INACTIVE = ARRIVAL_UNSELECTABLE_INACTIVE
STRUCTURE_ARRIVAL_UNSELECTABLE_UNMEASURED = ARRIVAL_UNSELECTABLE_UNMEASURED
STRUCTURE_ARRIVAL_SELECTABLE = ARRIVAL_SELECTABLE

#: Shown next to the section. Says the two things Requirement 7.4 requires the
#: help text to say, in the place a GUI user actually reads.
STRUCTURE_ARRIVAL_BUDGET_NOTE = (
    "EXPERIMENTAL. Cross-world structures are smashables, props and "
    "decorations ported INTO a world from another world — an Ice Plant in the "
    "graveyard, a Coffin Lid in the ice ship. Each row is a ROUTE (structure + "
    "source world).\n"
    "THE SWITCH ITSELF MEANS YES. If you turn cross-world structures on and "
    "never touch this list, EVERY selectable route below is a candidate and the "
    "resource budget decides how many actually fit. Tick rows only if you want "
    "to narrow that down: as soon as you touch a checkbox here your selection "
    "is what counts, exactly as ticked, and turning them all off again means "
    "nothing is ported. With the switch off, nothing here is read at all.\n"
    "PORTED STRUCTURES ARE COSMETIC. A ported structure is scenery in its new "
    "world: no protected record is ever a destination, a level's own puzzle "
    "props are never retyped, and nothing here can make a level harder to "
    "finish. What changed is the SOURCE side — see the two paragraphs below.\n"
    "ARE ALL THE STRUCTURES HERE? 23 of the 24 structure types the game "
    "declares can port. Only the Monster Generator stays home, and not on "
    "caution: it already resolves its assets from GLOBAL in every world, so "
    "there is nothing to port. Each greyed row below says its own reason.\n"
    "THE PROGRESSION AND PUZZLE PROPS ARE NOW IN, AT YOUR REQUEST, AND THEY "
    "ARE THE RISKIEST ROWS HERE. Bonus Grave, Bone Tower and Popup Headstone "
    "are script-driven (a bonus entrance, an assembling tower, a reveal that "
    "pops up on a cue), and Spirit Statue, Swamp Coffin Lid, Breakable Tiki "
    "Torch and Underworld Lantern belong to the ordered puzzle chains that a "
    "level script counts through in sequence. All 28 of their routes ported and "
    "verified cleanly, which proves the ART travels — it does NOT prove a "
    "foreign copy ever assembles, ever pops up, leads anywhere, or is ignored "
    "by the script that counts a chain. If one of those scripts counts an "
    "arrival, the recorded failure mode is a level that never finishes loading. "
    "A level's OWN chain members are still protected and never retyped. Leave "
    "these rows off if you want the safe set.\n"
    "FOUR MORE WERE HELD BACK UNTIL YOU ASKED FOR THEM, AND CARRY A "
    "KNOWN RISK OF THEIR OWN. Standing Coffin and Smashable Coffin can each spawn a Gold "
    "Key, and nobody has yet seen what a foreign coffin DROPS in a world whose "
    "progression did not plan for it. Spirit Torch and Smashable Snow Pirate "
    "both WRITE a persistent game var, and which var — and whether any world "
    "reads it — is still undecoded. All four ported and verified cleanly, so "
    "the hold was caution rather than a measured failure, and you accepted the "
    "risk of them breaking in game. They are candidates like every other row "
    "here: nobody has observed them running.\n"
    "WEIGHT is how often a ported structure gets picked when a slot is "
    "retyped, relative to the ITEMS it competes with, and \"Share of items\" is "
    "that weight as a percentage of the item pool. Enemies are not in that "
    "denominator: their share of the pool is renormalized separately, so "
    "nothing you set here can take slots away from enemies. The default is "
    "deliberately high enough to be noticed in game; lower it if a world starts "
    "to feel crowded with props.\n"
    "Structures share the ONE resource budget with cross-world enemies and are "
    "admitted AFTER them, so a structure can never displace an arriving enemy. "
    "Enabling ten routes and seeing three arrive is the budget working. Greyed "
    "rows cannot be enabled at all (never measured, inactive, or held back)."
)


@dataclass(frozen=True)
class StructureArrivalOption:
    """One STRUCTURE arrival ROUTE as the editor should show it.

    Same shape and same rules as `ArrivalOption`: `selectable` is derived here
    from the structure allowlist and the measured structure cost table, never
    from the config, so a stored value can never widen it.
    """
    type_id: int
    name: str
    source_world: str
    target_world: str
    resident_bytes: int | None
    selectable: bool
    reason: str

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.type_id, self.source_world, self.target_world)

    @property
    def route_key(self) -> str:
        return arrival_route_key(self.type_id, self.source_world)

    @property
    def label(self) -> str:
        return f"{self.name} (from {self.source_world.capitalize()})"

    @property
    def cost_label(self) -> str:
        if self.resident_bytes is None:
            return "not measured"
        return f"{self.resident_bytes:,} B"


def structure_arrival_options(world: str) -> list[StructureArrivalOption]:
    """Every allowlisted STRUCTURE route INTO `world`, with its verdict.

    Mirrors `arrival_options`: unselectable routes are returned too, with the
    reason, so the editor can grey them and say why instead of silently hiding
    a route the user might otherwise go looking for in the JSON.
    """
    active = load_structure_allowlist()
    out: list[StructureArrivalOption] = []
    for entry in CROSS_WORLD_STRUCTURE_ALLOWLIST:
        if entry.target_world != world:
            continue
        row = _roster_costs.structure_cost_for(
            entry.enemy_id, entry.source_world, entry.target_world)
        if (entry.key in HELD_BACK_ON_OBSERVED_CRASH
                or is_held_back_on_observed_crash(entry.enemy_id)):
            selectable, reason = (False,
                                 STRUCTURE_ARRIVAL_UNSELECTABLE_HELD_BACK)
        elif not entry.active or entry.key not in active:
            selectable, reason = (False,
                                 STRUCTURE_ARRIVAL_UNSELECTABLE_INACTIVE)
        elif row is None:
            selectable, reason = (False,
                                 STRUCTURE_ARRIVAL_UNSELECTABLE_UNMEASURED)
        else:
            selectable, reason = True, STRUCTURE_ARRIVAL_SELECTABLE
        out.append(StructureArrivalOption(
            type_id=entry.enemy_id,
            name=STRUCTURE_TYPES.get(entry.enemy_id,
                                     str(entry.name).replace("_", " ")),
            source_world=entry.source_world,
            target_world=entry.target_world,
            resident_bytes=(int(row.resident_bytes) if row is not None
                            else None),
            selectable=selectable, reason=reason))
    out.sort(key=lambda o: (o.type_id, o.source_world))
    return out


def selectable_structure_arrival_keys(world: str) -> set[tuple[int, str, str]]:
    """The structure route keys the editor is allowed to offer for `world`."""
    return {o.key for o in structure_arrival_options(world) if o.selectable}


#: The two answers `structure_routes_for_world` can give, so callers name the
#: case rather than re-deriving it from a set size.
STRUCTURE_ROUTES_CASE_EXPLICIT = "explicit"
STRUCTURE_ROUTES_CASE_FALLBACK = "fallback"


def structure_routes_for_world(config, world: str) -> tuple[set, str]:
    """The structure routes a run should CONSIDER for `world`, plus which case
    applied.

    THE BUGFIX. The switch itself now means "yes". Before this, the candidate
    set came only from `SpawnConfig.enabled_structure_arrival_keys`, and because
    every stock route ships `enabled=False` (and a pre-version-3 file carries no
    `structure_arrivals` section at all), that set was ALWAYS empty -- so ticking
    the switch could never port a structure. Two cases are now distinguished:

    * EXPLICIT -- the user actually made a selection (see
      `SpawnConfig.has_explicit_structure_selection`). Their subset is honoured
      EXACTLY, including the empty one: explicitly ticking nothing still ports
      nothing.
    * FALLBACK -- no explicit selection was ever saved (the stock config, an old
      config file, or `config is None` because no config was supplied at all).
      Every SELECTABLE route for the world becomes a candidate and the existing
      resource budget decides how many actually fit.

    THIS ONLY ANSWERS "WHICH ROUTES DID THE USER WANT", NEVER "WHICH ROUTES ARE
    ALLOWED". The fallback set is `selectable_structure_arrival_keys(world)` --
    the permission oracle itself -- so it can never contain a withheld, refused,
    unmeasured, inactive or held-back route, and the returned set is a subset of
    that oracle in BOTH cases (the explicit case is re-validated against it by
    `enabled_structure_arrival_keys`). Nothing here is promoted: every route
    stays CANDIDATE.
    """
    # VERSION 4: the marker is per target world (`selection_explicit`). The
    # predicate asked here is `has_explicit_selection(world)`, which falls back
    # to the old global `has_explicit_structure_selection()` -- so the answer is
    # never NARROWER than before and the FALLBACK below is untouched. A config
    # object that predates the per-world method (a hand-built stub) still works
    # through the getattr fallback.
    if config is not None:
        per_world = getattr(config, "has_explicit_selection", None)
        explicit = (per_world(world) if callable(per_world)
                    else config.has_explicit_structure_selection())
    else:
        explicit = False
    if explicit:
        return (set(config.enabled_structure_arrival_keys(world)),
                STRUCTURE_ROUTES_CASE_EXPLICIT)
    return (selectable_structure_arrival_keys(world),
            STRUCTURE_ROUTES_CASE_FALLBACK)


def default_structure_arrivals() -> dict:
    """The stock structure-arrivals table: every SELECTABLE route present and
    every one of them DISABLED (Req 6.2).

    The entry EXISTS so the editor has something to tick and so the weight is
    editable; `enabled=False` is what makes turning the feature on a no-op.
    """
    out: dict[str, dict] = {}
    for world in WORLDS:
        out[world] = {
            opt.route_key: {"weight": _structure_arrival_default(opt.type_id),
                            "enabled": False}
            for opt in structure_arrival_options(world) if opt.selectable
        }
    return out


def reset_structure_arrivals(config) -> None:
    """Put `config.structure_arrivals` back to stock (all off), leaving
    everything else alone.

    STOCK also means "no explicit selection", so the marker is cleared too: a
    reset restores the shipped default exactly, which with the switch on now
    means "every selectable route is a candidate, budget decides".
    """
    config.structure_arrivals = default_structure_arrivals()
    config.structure_selection_explicit = False


def structure_arrival_summary(config) -> dict[str, tuple[int, int]]:
    """{world: (enabled_count, selectable_count)} for a status line."""
    out: dict[str, tuple[int, int]] = {}
    for world in WORLDS:
        offered = len(selectable_structure_arrival_keys(world))
        enabled = len(config.enabled_structure_arrival_keys(world))
        out[world] = (enabled, offered)
    return out


# ---------------------------------------------------------------------------
# Default weight derivation
# ---------------------------------------------------------------------------
def _scaled_default(tid: int, scale: float, fallback: float = 5.0) -> float:
    base = DEFAULT_ITEM_WEIGHTS.get(tid)
    if base is None:
        return fallback
    return round(base * scale, 1)


# Weight assigned to a "normal" enemy (one with WALKING_MELEE_WEIGHTS 1.0).
# Enemy defaults are derived from WALKING_MELEE_WEIGHTS — the table the
# randomizer normally uses to balance enemy frequency — so the editor's
# "Default" restores the exact stock enemy distribution (Raven/Ghost/Torso/
# Guard reduced, everything else full).
ENEMY_DEFAULT_BASE = 100.0


def _enemy_default(tid: int) -> float:
    return round(WALKING_MELEE_WEIGHTS.get(tid, 1.0) * ENEMY_DEFAULT_BASE, 1)


#: RAW (pre-scaling) pool weight a ported structure gets by default, on the same
#: scale as `items.DEFAULT_ITEM_WEIGHTS`.
#:
#: WHY 25 AND NOT THE GENERIC STRUCTURES VALUE. Measured on this tree:
#:   * `DEFAULT_ITEM_WEIGHTS` totals 1,360, of which 282 is enemies and 1,078 is
#:     the ITEM half (items + structures). `randomize_universal` renormalizes the
#:     enemy block to `ENEMY_SHARE_TARGET` (0.20) whatever else changes, so a
#:     structure weight competes only inside that 1,078 and can NEVER take share
#:     from enemies.
#:   * a native smashable in that table is 3-10 (Globe 3, Ice Plant 5, most 8,
#:     Coin Container 10). At 5 that is 5/1078 = 0.46% of the item half, 0.37% of
#:     all retyped slots — which is why a ported structure was reported as
#:     appearing "too little": it was competing at one four-hundredth of a share.
#:   * 25 is not a round guess: it is the weight this repo ALREADY chose for the
#:     same problem. `0x24` Collector and `0x02` Bonus_Grave sit at 25 with the
#:     comment "bumped, was effectively invisible" — a fun-but-minor spawn is
#:     visible at 25 and invisible below it. It is also still well under
#:     Gem (42), Locked_Chest (52), Bag_Of_Gold (62), Wooden_Chest (170) and
#:     GoldCoin (471).
#:
#: WHAT IT BUYS. Per ARRIVING type: 25/(~1030 + 25N) of the item half, so with a
#: single arrival ~1.9% of the item half and ~1.5% of ALL retyped slots — four
#: times the old 0.37%. With the out-of-the-box fallback (every selectable route
#: a candidate, typically ~11 types reaching one world) the arrivals together
#: take 25*11/(1030+275) = ~21% of the item half, ~17% of all retyped slots.
#:
#: THE TRADEOFF, stated. Raising this trades ITEM frequency for structure
#: variety and nothing else: enemies are renormalized independently, and items
#: keep ~79% of their own half at the 11-route worst case (GoldCoin alone stays
#: the single most likely roll at ~36% of that half). Going to 50 would put the
#: same worst case at ~35% of the item half, which starts to read as a takeover
#: rather than as variety, so this is deliberately parked at the "visible"
#: threshold the item table itself established rather than at the highest number
#: that still looks defensible.
STRUCTURE_ARRIVAL_DEFAULT_RAW_WEIGHT = 25.0


def _structure_arrival_default(tid: int) -> float:
    """The default WEIGHT of a structure arrival route.

    Scaled by the same `_default_scale()` every other section uses, so the
    number stored in the config is on the config's own 0-100 scale and stays
    comparable with the `items` and `structures` entries it competes against.

    It is DELIBERATELY NOT the generic `structures` value any more. That value
    (`_scaled_default`, 0.6-2.1 on this scale) is the weight a structure has when
    the world already owns it and the level designers already placed dozens of
    them; a PORTED structure has no designer placements at all, so the pool
    weight is the only thing that puts it on screen. See
    `STRUCTURE_ARRIVAL_DEFAULT_RAW_WEIGHT` for the derivation.

    `tid` is retained because callers pass one and because a future per-type
    default belongs here rather than at the call sites.
    """
    del tid  # one default for every route; kept in the signature on purpose
    return _clamp(round(STRUCTURE_ARRIVAL_DEFAULT_RAW_WEIGHT
                        * _default_scale(), 1))


def _default_scale() -> float:
    mx = max(DEFAULT_ITEM_WEIGHTS.values()) if DEFAULT_ITEM_WEIGHTS else 100.0
    return 100.0 / mx if mx else 1.0


# ---------------------------------------------------------------------------
# THE UNIFIED WEIGHT MODEL  (`unified-randomization` Task 1)
# ---------------------------------------------------------------------------
# ONE table, ONE scale, covering all three categories the randomizer used to
# weight separately: native spawn types, cross-world ENEMY arrival routes and
# cross-world STRUCTURE arrival routes.
#
# NOTHING CONSUMES THIS YET. Task 1 adds the model, the version-4 section and
# the three projections ALONGSIDE the legacy tables; `universal_weights`,
# `enemy_weights_for_world`, `arrivals` and `structure_arrivals` are untouched
# and still drive every build, so observable build behaviour is UNCHANGED.
# Wiring the projections into the build is task 3/4's job.
#
# THE SCALE is `items.DEFAULT_ITEM_WEIGHTS`'s own scale (that table totals
# 1,360: 282 enemy, 1,078 item half), so the stock numbers appear in the
# unified table AS THEMSELVES and a migrated v3 native weight needs no
# conversion. Route defaults are expressed on that same scale below.
#
# THE KEYSPACE is one flat string space per TARGET WORLD:
#     native type          "native:0x1A"
#     enemy arrival route  "enemy:0x53-ice"        (source world = ice)
#     structure arrival    "structure:0x7C-ice"    (source world = ice)
# The route forms REUSE `arrival_route_key` -- there is one route key format in
# this module, not two. The only difference is the separator: the unified id
# spells it `-` (the design's spelling) while the legacy JSON key spells it
# `|`, and `unified_route_key` / `parse_unified_key` convert between the two in
# ONE place. World names never contain `-` or `|`, so the transform is total
# and the round trip is exact.
UNIFIED_KIND_NATIVE = "native"
UNIFIED_KIND_ENEMY = "enemy"
UNIFIED_KIND_STRUCTURE = "structure"
UNIFIED_KINDS = (UNIFIED_KIND_NATIVE, UNIFIED_KIND_ENEMY,
                 UNIFIED_KIND_STRUCTURE)
UNIFIED_ROUTE_KINDS = (UNIFIED_KIND_ENEMY, UNIFIED_KIND_STRUCTURE)

#: The unified id's route separator, and the legacy JSON key's. Kept adjacent
#: so the one-place transform is obvious.
UNIFIED_ROUTE_SEP = "-"
_LEGACY_ROUTE_SEP = "|"

#: The DEFAULT for the `enemy_share_target` CONFIG FIELD. `items`'s constant is
#: demoted to exactly this: the default value of a config field, no longer the
#: number the selection code reads. (`items.randomize_universal` still reads its
#: own module constant -- that consumer is NOT modified by this task.)
#
#: 0.0 MEANS OFF: `native_weights` uses the typed weights unchanged, because
#: `_apply_enemy_share` only rescales for a target strictly inside (0, 1). It is
#: 0.0 rather than None so every `float(...)` reader (save, the GUI, the report)
#: keeps working. It used to be 0.20, which silently forced enemies to 20% of the
#: pool and made the editor's percentages meaningless. See `items.ENEMY_SHARE_TARGET`.
UNIFIED_ENEMY_SHARE_DEFAULT = float(_ITEMS_ENEMY_SHARE_TARGET or 0.0)

#: The unified-scale weight of a "normal" enemy: the value every full-share
#: enemy carries in `DEFAULT_ITEM_WEIGHTS`. Used to put `_enemy_default`'s
#: WALKING_MELEE bias onto the unified scale without changing its ratios.
UNIFIED_ENEMY_BASE_WEIGHT = 15.0


def unified_native_key(type_id: int) -> str:
    """The unified id of a NATIVE spawn type: `native:0x1A`."""
    return f"{UNIFIED_KIND_NATIVE}:0x{int(type_id):02X}"


def unified_route_key(kind: str, type_id: int, source_world: str) -> str:
    """The unified id of an arrival ROUTE: `enemy:0x53-ice`.

    Built FROM `arrival_route_key`, so the legacy table and the unified table
    share one route key format; only the separator differs (see
    `UNIFIED_ROUTE_SEP`).
    """
    if kind not in UNIFIED_ROUTE_KINDS:
        raise ValueError(f"not a route kind: {kind!r}")
    legacy = arrival_route_key(type_id, source_world)
    return f"{kind}:{legacy.replace(_LEGACY_ROUTE_SEP, UNIFIED_ROUTE_SEP)}"


def parse_unified_key(key) -> tuple[str, int, str | None] | None:
    """The exact inverse of `unified_native_key` / `unified_route_key`.

    Returns `(kind, type_id, source_world)` with `source_world` None for a
    native, or None when the key is unparseable. Round-tripping is TOTAL: for
    every kind/type/source this module can spell, parsing the spelling returns
    the inputs, and re-spelling a parse returns the same string.
    """
    if not isinstance(key, str) or ":" not in key:
        return None
    kind, _, rest = key.partition(":")
    if kind == UNIFIED_KIND_NATIVE:
        try:
            return (kind, int(rest, 16), None)
        except (ValueError, TypeError):
            return None
    if kind not in UNIFIED_ROUTE_KINDS:
        return None
    # Hand the remainder back to the ONE route-key parser.
    parsed = parse_arrival_route_key(
        rest.replace(UNIFIED_ROUTE_SEP, _LEGACY_ROUTE_SEP))
    if parsed is None or not parsed[1]:
        return None
    return (kind, parsed[0], parsed[1])


@dataclass(frozen=True)
class Candidate:
    """Anything the unified pass may pick.

    `cost_bytes` is the ONLY structural difference between the categories: 0 for
    a native (a type the world already owns costs no resident bytes), and the
    MEASURED resident bytes for a route. It is never a guess: an unmeasured
    route is not offered at all, because the measured cost table is half the
    permission oracle.
    """
    key: str
    kind: str
    type_id: int
    name: str
    source_world: str | None
    target_world: str
    cost_bytes: int


def _type_name(type_id: int) -> str:
    tid = int(type_id)
    return (ENEMY_TYPES.get(tid) or ITEM_TYPES.get(tid)
            or STRUCTURE_TYPES.get(tid) or f"0x{tid:02X}")


def native_pool_types(world: str) -> tuple[int, ...]:
    """The NATIVE types that may be a destination in `world`, sorted.

    Membership is exactly today's: a type in `items.UNIVERSAL_ITEM_POOL`, and
    for enemies additionally present in the world's vanilla roster. The hazards
    `0x40` / `0xCC` / `0xD1` are absent because they are absent from the pool
    tuple -- enforced BY ABSENCE, with no set to keep in step (Req 7.2).
    """
    avail = _WORLD_ENEMY_AVAIL.get(world, set())
    out = []
    for tid in UNIVERSAL_ITEM_POOL:
        if tid in ENEMY_DESTINATIONS and tid not in avail:
            continue
        out.append(int(tid))
    return tuple(sorted(set(out)))


def _unified_native_default(world: str, type_id: int) -> float:
    """A native's stock unified weight: its own `DEFAULT_ITEM_WEIGHTS` number
    (with any per-world override), i.e. itself, on the 1,360 scale."""
    table = get_item_weights_for_world(world)
    return float(table.get(int(type_id), 0))


def _unified_enemy_route_default(type_id: int) -> float:
    """`_enemy_default(type_id)` expressed on the unified 1,360 scale.

    Same decision, same ratios: `_enemy_default` is WALKING_MELEE_WEIGHTS times
    a base, so dividing by that base and multiplying by the unified "normal
    enemy" weight lands a full-share enemy on 15 -- exactly its stock
    `DEFAULT_ITEM_WEIGHTS` entry -- and a reduced enemy proportionally below it.
    """
    return round(_enemy_default(type_id) / ENEMY_DEFAULT_BASE
                 * UNIFIED_ENEMY_BASE_WEIGHT, 1)


def _unified_structure_route_default(type_id: int) -> float:
    """`_structure_arrival_default(type_id)` on the unified scale.

    That is `STRUCTURE_ARRIVAL_DEFAULT_RAW_WEIGHT` by construction: the legacy
    helper is the same number scaled DOWN onto the config's 0-100 scale, and its
    own docstring records 25 as being on `DEFAULT_ITEM_WEIGHTS`'s scale.
    """
    del type_id  # one default for every route, as in the legacy helper
    return float(STRUCTURE_ARRIVAL_DEFAULT_RAW_WEIGHT)


def native_candidates(world: str) -> list[Candidate]:
    """Every native Candidate for `world`, sorted by unified id."""
    out = [
        Candidate(key=unified_native_key(tid), kind=UNIFIED_KIND_NATIVE,
                  type_id=tid, name=_type_name(tid), source_world=None,
                  target_world=world, cost_bytes=0)
        for tid in native_pool_types(world)
    ]
    out.sort(key=lambda c: c.key)
    return out


def enemy_route_candidates(world: str) -> list[Candidate]:
    """Every SELECTABLE enemy arrival Candidate into `world`, sorted by id.

    Derived from `arrival_options`, so the permission oracle (the catalog
    allowlist plus the measured cost row) decides membership. This NARROWS.
    """
    out = [
        Candidate(key=unified_route_key(UNIFIED_KIND_ENEMY, opt.enemy_id,
                                        opt.source_world),
                  kind=UNIFIED_KIND_ENEMY, type_id=opt.enemy_id,
                  name=opt.name, source_world=opt.source_world,
                  target_world=world, cost_bytes=int(opt.resident_bytes or 0))
        for opt in arrival_options(world) if opt.selectable
    ]
    out.sort(key=lambda c: c.key)
    return out


def structure_route_candidates(world: str) -> list[Candidate]:
    """Every SELECTABLE structure arrival Candidate into `world`, sorted by id."""
    out = [
        Candidate(key=unified_route_key(UNIFIED_KIND_STRUCTURE, opt.type_id,
                                        opt.source_world),
                  kind=UNIFIED_KIND_STRUCTURE, type_id=opt.type_id,
                  name=opt.name, source_world=opt.source_world,
                  target_world=world, cost_bytes=int(opt.resident_bytes or 0))
        for opt in structure_arrival_options(world) if opt.selectable
    ]
    out.sort(key=lambda c: c.key)
    return out


def unified_candidates(world: str) -> list[Candidate]:
    """Every Candidate for `world` -- all three kinds, ONE list, sorted by the
    unified id, so no caller's result can depend on interpreter hashing
    (Req 6.4)."""
    out = (native_candidates(world) + enemy_route_candidates(world)
           + structure_route_candidates(world))
    out.sort(key=lambda c: c.key)
    return out


def unified_candidate_map(world: str) -> dict[str, Candidate]:
    """`{unified id: Candidate}` for `world`, insertion-ordered by sorted id."""
    return {c.key: c for c in unified_candidates(world)}


def default_unified_weights() -> dict:
    """The stock unified table: `{world: {unified id: {"weight", "enabled"}}}`.

    THE DEFAULT ENABLED STATE IS DELIBERATELY NOT UNIFORM, and this is the one
    decision the design left open. Routes are NOT all defaulted off: the stock
    unified table reproduces TODAY'S behaviour exactly, per category, because
    Req 1.5 says adopting the unified system must not by itself change what a
    build produces. Defaulting every route off would silently change the game
    the moment the unified table started driving the build.

      * natives          -> ENABLED at their own `DEFAULT_ITEM_WEIGHTS` weight,
                            which is what the stock pool already is.
      * ENEMY routes     -> ENABLED at `_enemy_default` on the unified scale,
                            which is what `default_arrivals()` does today
                            (`enabled=True`).
      * STRUCTURE routes -> ENABLED at `_structure_arrival_default` on the
                            unified scale. THIS WAS CHANGED: they shipped
                            `enabled=False` (matching the old
                            `default_structure_arrivals()`, Req 6.2 of the
                            earlier feature), and now ship `enabled=True`,
                            because the GUI no longer has a cross-world
                            structures switch — the feature is always on, so
                            every selectable route is meant to be a candidate
                            out of the box and the byte budget alone decides how
                            many fit.

                            THE "SWITCH ALONE MEANS YES" FALLBACK STILL EXISTS
                            AND MUST NOT BE DELETED. With every stock row now
                            enabled it is REDUNDANT FOR THE STOCK CONFIG rather
                            than load bearing, but it is still the behaviour for
                            a config whose rows were all unticked without the
                            selection being marked explicit — which is precisely
                            what `apply_harder_mode_preset` relies on marking.
                            See `SpawnConfig.structure_route_weights`, which
                            routes through `structure_routes_for_world` rather
                            than re-deriving the candidate set from `enabled`.

    Only SELECTABLE routes get an entry, so -- exactly as in the legacy tables --
    a hand-edited file has no key to switch a held-back, inactive or unmeasured
    route on, and `from_dict` never creates one.
    """
    out: dict[str, dict] = {}
    for world in WORLDS:
        tbl: dict[str, dict] = {}
        for cand in native_candidates(world):
            tbl[cand.key] = {
                "weight": _unified_native_default(world, cand.type_id),
                "enabled": True,
            }
        for cand in enemy_route_candidates(world):
            tbl[cand.key] = {
                "weight": _unified_enemy_route_default(cand.type_id),
                "enabled": True,
            }
        for cand in structure_route_candidates(world):
            tbl[cand.key] = {
                "weight": _unified_structure_route_default(cand.type_id),
                # ENABLED by default now — see the docstring. Cross-world
                # structures no longer have a GUI switch to opt in with.
                "enabled": True,
            }
        out[world] = {k: tbl[k] for k in sorted(tbl)}
    return out


def default_selection_explicit() -> dict[str, bool]:
    """`{world: False}` -- nobody has chosen anything in a fresh config."""
    return {world: False for world in WORLDS}


def reset_unified(config) -> None:
    """Put the unified section back to stock, leaving everything else alone."""
    config.unified_weights = default_unified_weights()
    config.enemy_share_target = UNIFIED_ENEMY_SHARE_DEFAULT
    config.selection_explicit = default_selection_explicit()


# ---------------------------------------------------------------------------
# MIGRATION FROM VERSION <= 3  (`unified-randomization` Task 2)
# ---------------------------------------------------------------------------
# Three legacy tables become ONE. The mapping, verbatim from the design:
#
#     universal_weights[world][type]   -> native:<type>
#     arrivals[world][route]           -> enemy:<route>
#     structure_arrivals[world][route] -> structure:<route>
#
# A v3 FILE has no `universal_weights` key -- that projection is COMPUTED from
# the three sections a v3 file actually carries (`items`, `structures` and the
# per-world `enemies`), so those are the sections read here, and each maps to a
# `native:` id.
#
# ALL-OR-NOTHING (Req 8.3). Either a complete v4 config comes out or
# `MigrationError` (U9) is raised naming the field that could not be mapped.
# There is no half-loaded state and no path on which a user's arrival selections
# silently revert.
#
# THE ONE ASYMMETRY (Req 8.4). `enabled` is copied VERBATIM: a route the user
# turned OFF in v3 is off in v4 and NOTHING here turns it back on. A key the
# user never saw takes the v4 default instead.
#
# U9 vs U10, drawn precisely:
#   * U9  (FATAL) -- the field EXISTS and is structurally unmappable: a section
#                    that is not an object, an entry that is not an object, a
#                    weight that is not a number or is negative, or a key that
#                    does not parse as a type id / route at all. Nothing can be
#                    said about what the user meant, so we refuse rather than
#                    drop it silently.
#   * U10 (not fatal) -- the field parses FINE but names no known v4 Candidate
#                    (a route since deactivated, an unmeasured route, a world
#                    that no longer exists). Reported as DROPPED and NOT
#                    resurrected, exactly as `from_dict`'s overlays already
#                    treat an unknown key.
#
# THIS MODULE PRINTS NOTHING -- it has no `print` and no logger, by convention
# (its only I/O is `save`/`load`). The report is returned as DATA for the caller
# to log (task 4's job).
#
# READ, NOT REWRITTEN IN PLACE (Req 8.2). Migration is a pure dict -> dict
# transform: the input dict is never mutated and the v3 FILE is never touched.
# Only `save()` writes, and it writes v4.
MIGRATION_ERROR_ID = "U9"
MIGRATION_DROPPED_ID = "U10"

#: WHICH DIRECTION THE WEIGHT SCALE IS CONVERTED, AND WHY.
#:
#: v3 stores every weight on the CONFIG's own 0-100 scale: the item/structure
#: sections are `DEFAULT_ITEM_WEIGHTS` values multiplied by `_default_scale()`
#: (= 100/471, so GoldCoin's 471 is stored as 100.0), and the enemy sections are
#: `WALKING_MELEE_WEIGHTS` multiplied by `ENEMY_DEFAULT_BASE` (= 100, so a
#: full-share enemy is stored as 100.0).
#:
#: Task 1 put the unified table on `DEFAULT_ITEM_WEIGHTS`' own 1,360 scale
#: (282 enemy / 1,078 item), so stock numbers migrate as THEMSELVES. Migration
#: therefore converts UPWARD, v3 -> v4, un-doing each legacy scaling:
#:
#:   item / structure / structure-route weight:  v4 = v3 / _default_scale()
#:                                                  = v3 * 471/100   (x4.71)
#:   enemy / enemy-route weight:                 v4 = v3 / 100 * 15   (x0.15)
#:
#: Checked against the stock numbers in both directions: a stock GoldCoin goes
#: 100.0 -> 471.0 (its `DEFAULT_ITEM_WEIGHTS` entry), a stock full-share enemy
#: goes 100.0 -> 15.0 (its entry), a stock reduced enemy at 0.5 share goes
#: 50.0 -> 7.5, and a stock structure route goes 5.3 -> ~25.0
#: (`STRUCTURE_ARRIVAL_DEFAULT_RAW_WEIGHT`). Getting either factor backwards
#: would change every spawn rate in the game and would be invisible in a diff,
#: which is why both are derived from the constants rather than written out as
#: numbers.
def _migration_item_factor() -> float:
    """v3 config 0-100 scale -> unified 1,360 scale, for items/structures."""
    scale = _default_scale()
    return (1.0 / scale) if scale else 1.0


def _migration_enemy_factor() -> float:
    """v3 enemy 0-100 scale -> unified 1,360 scale, for enemies and routes."""
    if not ENEMY_DEFAULT_BASE:
        return 1.0
    return UNIFIED_ENEMY_BASE_WEIGHT / ENEMY_DEFAULT_BASE


class MigrationError(ValueError):
    """U9: a version <= 3 config could not be FULLY migrated.

    Raised rather than half-loading (Req 8.3). `field` names the exact v3 field
    that could not be mapped, so the message points at the file, not at the
    migrator.
    """

    def __init__(self, field: str, detail: str):
        self.field = str(field)
        self.detail = str(detail)
        super().__init__(f"{MIGRATION_ERROR_ID}: cannot migrate field "
                         f"{self.field}: {self.detail}")


@dataclass(frozen=True)
class MigrationReport:
    """What migration did, as DATA for the caller to log.

    `mapped`    -- (v3 field path, v4 Candidate id) for every field carried over.
    `dropped`   -- (v3 field path, reason) for every U10: parsed fine, names no
                   known v4 Candidate. NOT resurrected.
    `defaulted` -- every v4 field no v3 field fed, which therefore took the v4
                   default.
    """
    version_from: int
    mapped: tuple[tuple[str, str], ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()
    defaulted: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {"mapped": len(self.mapped), "dropped": len(self.dropped),
                "defaulted": len(self.defaulted)}

    def summary(self) -> str:
        c = self.counts
        return (f"spawn_config: migrated version {self.version_from} -> "
                f"{CONFIG_VERSION}: {c['mapped']} field(s) mapped, "
                f"{c['dropped']} dropped, {c['defaulted']} defaulted")

    def lines(self) -> tuple[str, ...]:
        """One ready-to-log line per field. The CALLER logs these."""
        out = [self.summary()]
        out += [f"  mapped:    {src} -> {dst}" for src, dst in self.mapped]
        out += [f"  dropped:   {src} ({MIGRATION_DROPPED_ID}: {why})"
                for src, why in self.dropped]
        out += [f"  defaulted: {name}" for name in self.defaulted]
        return tuple(out)


_MIGRATION_NO_CANDIDATE = "matches no known v4 Candidate"
_MIGRATION_NO_WORLD = "names no known target world"


def _migration_version(old) -> int:
    try:
        return int((old or {}).get("version") or 0)
    except (TypeError, ValueError):
        return 0


def needs_migration(d) -> bool:
    """True when `d` is a version <= 3 file that must be migrated.

    A file that already carries a `unified` section is left alone whatever its
    version says: its unified table is authoritative and re-deriving one from
    the legacy sections would overwrite it.
    """
    if not isinstance(d, dict):
        return False
    return _migration_version(d) < CONFIG_VERSION and "unified" not in d


def _migration_type_id(field: str, raw_key) -> int:
    try:
        return int(raw_key, 16) if isinstance(raw_key, str) else int(raw_key)
    except (TypeError, ValueError):
        raise MigrationError(field, f"key {raw_key!r} is not a type id")


def _migration_entry(field: str, value, factor: float):
    """`(weight | None, enabled | None)` from one v3 entry, or raise U9."""
    if not isinstance(value, dict):
        raise MigrationError(field, f"entry is {type(value).__name__}, "
                                    "expected an object")
    weight = None
    if "weight" in value:
        try:
            raw = float(value["weight"])
        except (TypeError, ValueError):
            raise MigrationError(field, f"weight {value['weight']!r} is not "
                                        "a number")
        if raw < 0:
            raise MigrationError(field, f"weight {raw} is negative")
        weight = round(raw * factor, 1)
    enabled = bool(value["enabled"]) if "enabled" in value else None
    return weight, enabled


def _migration_section(old, name: str):
    """A v3 section as a dict, or None when absent. U9 when it is the wrong
    shape -- a section that is not an object cannot be mapped at all."""
    section = old.get(name)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise MigrationError(name, f"section is {type(section).__name__}, "
                                   "expected an object")
    return section


def migrate_v3_with_report(old: dict) -> tuple[dict, MigrationReport]:
    """`migrate_v3` plus the report. See `migrate_v3` for the mapping."""
    if not isinstance(old, dict):
        raise MigrationError("<root>", f"config is {type(old).__name__}, "
                                       "expected an object")
    version = _migration_version(old)
    # Start from the STOCK unified table, so a key the v3 file never carried
    # takes the v4 default -- the other half of the Req 8.4 asymmetry.
    unified: dict[str, dict] = {
        world: {key: dict(entry) for key, entry in tbl.items()}
        for world, tbl in default_unified_weights().items()
    }
    mapped: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    defaulted: list[str] = []
    touched: set[tuple[str, str]] = set()

    def _apply(world: str, key: str, weight, enabled) -> None:
        if weight is not None:
            unified[world][key]["weight"] = _clamp_unified(weight)
        if enabled is not None:
            # VERBATIM. False stays False; nothing below re-enables a key.
            unified[world][key]["enabled"] = bool(enabled)
        touched.add((world, key))

    # --- items / structures -> native:<type>, on EVERY world that offers the
    # --- type (a v3 items/structures entry is global; the unified table is
    # --- per target world).
    item_factor = _migration_item_factor()
    for name in ("items", "structures"):
        section = _migration_section(old, name)
        if section is None:
            defaulted.append(f"{name} (section absent)")
            continue
        for raw_key in sorted(section, key=str):
            field = f"{name}[{raw_key!r}]"
            tid = _migration_type_id(field, raw_key)
            key = unified_native_key(tid)
            weight, enabled = _migration_entry(field, section[raw_key],
                                               item_factor)
            hits = [w for w in WORLDS if key in unified[w]]
            if not hits:
                dropped.append((field, _MIGRATION_NO_CANDIDATE))
                continue
            for world in hits:
                _apply(world, key, weight, enabled)
            mapped.append((field, key))

    # --- enemies[world] -> native:<type>, per world. Done AFTER the global
    # --- sections so a per-world entry wins for the same id (the id sets are
    # --- disjoint today, so this is precedence stated rather than exercised).
    enemy_factor = _migration_enemy_factor()
    section = _migration_section(old, "enemies")
    if section is None:
        defaulted.append("enemies (section absent)")
    else:
        for world in sorted(section, key=str):
            tbl = section[world]
            field_w = f"enemies[{world!r}]"
            if not isinstance(tbl, dict):
                raise MigrationError(field_w, f"world table is "
                                              f"{type(tbl).__name__}, "
                                              "expected an object")
            if world not in unified:
                dropped.append((field_w, _MIGRATION_NO_WORLD))
                continue
            for raw_key in sorted(tbl, key=str):
                field = f"enemies[{world!r}][{raw_key!r}]"
                tid = _migration_type_id(field, raw_key)
                key = unified_native_key(tid)
                weight, enabled = _migration_entry(field, tbl[raw_key],
                                                   enemy_factor)
                if key not in unified[world]:
                    dropped.append((field, _MIGRATION_NO_CANDIDATE))
                    continue
                _apply(world, key, weight, enabled)
                mapped.append((field, key))

    # --- arrivals -> enemy:<route>, structure_arrivals -> structure:<route>.
    # --- Both use `parse_arrival_route_key`, so there is ONE route key format
    # --- in this module and migration invents no second one.
    for name, kind, factor in (
            ("arrivals", UNIFIED_KIND_ENEMY, enemy_factor),
            ("structure_arrivals", UNIFIED_KIND_STRUCTURE, item_factor)):
        section = _migration_section(old, name)
        if section is None:
            defaulted.append(f"{name} (section absent)")
            continue
        for world in sorted(section, key=str):
            tbl = section[world]
            field_w = f"{name}[{world!r}]"
            if not isinstance(tbl, dict):
                raise MigrationError(field_w, f"world table is "
                                              f"{type(tbl).__name__}, "
                                              "expected an object")
            if world not in unified:
                dropped.append((field_w, _MIGRATION_NO_WORLD))
                continue
            for raw_key in sorted(tbl, key=str):
                field = f"{name}[{world!r}][{raw_key!r}]"
                parsed = parse_arrival_route_key(raw_key)
                if parsed is None or not parsed[1]:
                    raise MigrationError(field, f"key {raw_key!r} is not a "
                                                "route key (0xNN|source)")
                key = unified_route_key(kind, parsed[0], parsed[1])
                weight, enabled = _migration_entry(field, tbl[raw_key], factor)
                if key not in unified[world]:
                    dropped.append((field, _MIGRATION_NO_CANDIDATE))
                    continue
                _apply(world, key, weight, enabled)
                mapped.append((field, key))

    # --- the "user chose" marker. v3 carries ONE global boolean; v4 carries one
    # --- flag per target world. Absent means FALSE for every world, which is
    # --- what keeps `structure_routes_for_world`'s "switch alone means yes"
    # --- FALLBACK applying after migration. Marking it True here would make a
    # --- v3 file's all-off stock structure table read as a deliberate "port
    # --- nothing" and silently turn every structure route off for existing
    # --- users -- the exact opposite of what the file means.
    if "structure_selection_explicit" in old:
        flag = bool(old.get("structure_selection_explicit"))
        explicit = {world: flag for world in WORLDS}
        mapped.append(("structure_selection_explicit",
                       "unified.selection_explicit"))
    else:
        explicit = default_selection_explicit()
        defaulted.append("unified.selection_explicit (no v3 marker)")

    # --- enemy_share_target has no v3 field at all: it was the module constant
    # --- `items.ENEMY_SHARE_TARGET`, now the DEFAULT of a config field.
    defaulted.append("unified.enemy_share_target (no v3 field)")

    # --- The two FEATURE SWITCHES have no v3 field either, and DEFAULTING them
    # --- to True is the only honest answer: a v3 file was written by a build in
    # --- which both features were reachable, so turning one off here would be
    # --- inventing a decision the user never made.
    defaulted.append("unified.cross_world_enemies_enabled (no v3 field)")
    defaulted.append("unified.cross_world_structures_enabled (no v3 field)")

    for world in WORLDS:
        for key in sorted(unified[world]):
            if (world, key) not in touched:
                defaulted.append(f"unified.weights[{world!r}][{key!r}]")

    # A COMPLETE v4 config or nothing: every legacy section is carried through
    # untouched (the legacy paths still read them), the version is stamped 4 and
    # the unified section is synthesised. `old` itself is never mutated.
    out = dict(old)
    out["version"] = CONFIG_VERSION
    out["unified"] = {
        "enemy_share_target": UNIFIED_ENEMY_SHARE_DEFAULT,
        "cross_world_enemies_enabled": True,
        "cross_world_structures_enabled": True,
        "weights": {world: {key: dict(unified[world][key])
                            for key in sorted(unified[world])}
                    for world in WORLDS},
        "selection_explicit": dict(explicit),
    }
    report = MigrationReport(version_from=version, mapped=tuple(mapped),
                             dropped=tuple(dropped),
                             defaulted=tuple(defaulted))
    return out, report


def migrate_v3(old: dict) -> dict:
    """Map three tables into one. Reports every field mapped.

    universal_weights[world][type]      -> native:<type>
    arrivals[world][route]              -> enemy:<route>
    structure_arrivals[world][route]    -> structure:<route>

    `enabled` is copied VERBATIM. A route disabled in v3 is disabled in v4.
    A key present in v3 but absent from the v4 default is reported as DROPPED
    and not silently resurrected; a key absent from v3 takes the v4 default.

    Returns a complete version-4 config dict suitable for `from_dict`, or raises
    `MigrationError` (U9) naming the field that could not be mapped. The report
    is available from `migrate_v3_with_report`, and from
    `SpawnConfig.migration_report` after a load.
    """
    return migrate_v3_with_report(old)[0]


class SpawnConfig:
    """Editable spawn-rate table.

    Internal storage:
      items:       {tid: {"weight": float, "enabled": bool}}
      structures:  {tid: {"weight": float, "enabled": bool}}
      enemies:     {world: {tid: {"weight": float, "enabled": bool}}}
      enemy_tiers: {world: {tid: {tier_int: float weight}}}  (class levels)
      specials:    {key: {"weight": float, "enabled": bool}}  (mimic/wizard)
      ability_drops: {ability_id: {"weight": float, "enabled": bool}}
                     (enabled=False -> leave that id's vanilla weight alone)
      arrivals:    {target_world: {"0xNN|source": {"weight", "enabled"}}}
                   (cross-world arrival ROUTES; selectable routes only)
      structure_arrivals:
                   {target_world: {"0xNN|source": {"weight", "enabled"}}}
                   (cross-world STRUCTURE routes; selectable routes only, and
                    every entry defaults enabled=False)
    """

    def __init__(self, items: dict, structures: dict, enemies: dict,
                 enemy_tiers: dict | None = None, specials: dict | None = None,
                 ability_drops: dict | None = None,
                 arrivals: dict | None = None,
                 structure_arrivals: dict | None = None,
                 unified_weights: dict | None = None,
                 enemy_share_target: float | None = None,
                 cross_world_enemies_enabled: bool | None = None,
                 cross_world_structures_enabled: bool | None = None):
        self.items = items
        self.structures = structures
        self.enemies = enemies
        self.enemy_tiers = enemy_tiers if enemy_tiers is not None else {}
        self.specials = specials if specials is not None else {}
        self.ability_drops = (ability_drops if ability_drops is not None
                              else {})
        self.arrivals = arrivals if arrivals is not None else {}
        # NEW OPTIONAL KEYWORD, LAST, DEFAULTING None. Every existing caller
        # (the GUI editors, the CLI loader and the tests that build a config by
        # hand) keeps working unchanged and simply gets an empty table, which
        # `enabled_structure_arrival_keys` reads as "nothing enabled" -- the same
        # answer as the all-off default.
        self.structure_arrivals = (structure_arrivals
                                   if structure_arrivals is not None else {})
        # DID THE USER ACTUALLY CHOOSE? Distinguishes "no explicit selection
        # was ever saved" (the stock table, an old file with no section, a
        # hand-built config) from "the user visited the structure-arrivals
        # editor and deliberately ticked nothing". Only the SECOND may mean
        # "port nothing" while the switch is on; the first falls back to every
        # selectable route (see `structure_routes_for_world`).
        #
        # The signal is deliberately the simplest honest one: this flag is set
        # by `mark_structure_selection_explicit()` (the editor calls it on every
        # tick AND every untick), it survives a save/load round trip through the
        # `structure_selection_explicit` key, and it is treated as implied
        # whenever the table already carries at least one enabled entry -- a
        # config that names an enabled route has plainly been chosen, however it
        # was produced.
        #
        # GENERALISED IN VERSION 4 (`unified-randomization` task 1). The marker
        # is now PER TARGET WORLD and covers the whole unified tab rather than
        # the structure category alone, so "the user chose nothing" stays
        # distinguishable from "the user never chose" for every category.
        # `structure_selection_explicit` survives as a property over this dict,
        # with exactly its old meaning (any world marked), so every existing
        # caller -- the editors, `reset_structure_arrivals`, the save/load round
        # trip and `structure_routes_for_world` -- keeps working unchanged and
        # the structure FALLBACK is preserved intact.
        self.selection_explicit = default_selection_explicit()
        # THE UNIFIED TABLE. Added ALONGSIDE the legacy tables, which still
        # drive every build for now (task 1 changes no build output).
        self.unified_weights = (unified_weights if unified_weights is not None
                                else {})
        self.enemy_share_target = (UNIFIED_ENEMY_SHARE_DEFAULT
                                   if enemy_share_target is None
                                   else float(enemy_share_target))
        # THE TWO FEATURE SWITCHES, both DEFAULT TRUE.
        #
        # They live in the CONFIG rather than in GUI state for three reasons:
        # the spawn-rate editor edits the config, the config is what persists
        # across sessions, and the switch governs the very routes the section
        # it sits in lists.
        #
        # THEY CAN ONLY NARROW. `cli.py` ANDs each with the CLI / `_Args`
        # switch (`--cross-world` / `--cross-world-structures`), so either
        # source can turn a feature OFF and neither can turn ON what the other
        # refused. Nothing here touches the PER-ROUTE `enabled` flags, so a
        # user who turns a feature off and on again gets their exact route
        # selection back, and nothing here widens the permission oracle.
        self.cross_world_enemies_enabled = (
            True if cross_world_enemies_enabled is None
            else bool(cross_world_enemies_enabled))
        self.cross_world_structures_enabled = (
            True if cross_world_structures_enabled is None
            else bool(cross_world_structures_enabled))
        # THE MIGRATION REPORT, or None when nothing was migrated. Set by
        # `from_dict`; carried as DATA for the caller to log, because this module
        # does no I/O of its own beyond `save`/`load`. Never serialised.
        self.migration_report: MigrationReport | None = None

    # ---- the "user chose" marker, per target world ------------------------
    @property
    def structure_selection_explicit(self) -> bool:
        """Back-compat view of `selection_explicit`: True when ANY world is
        marked. Same value the single boolean used to carry."""
        return any(bool(v) for v in self.selection_explicit.values())

    @structure_selection_explicit.setter
    def structure_selection_explicit(self, value) -> None:
        flag = bool(value)
        self.selection_explicit = {world: flag for world in WORLDS}

    def mark_selection_explicit(self, world: str | None = None) -> None:
        """Record that the user made a choice, for `world` or for all worlds.

        Called on every tick AND every untick, exactly as the structure-only
        marker was, which is what keeps "I visited the editor and turned
        everything off" distinguishable from "I never chose".
        """
        if world is None:
            for w in WORLDS:
                self.selection_explicit[w] = True
        elif world in self.selection_explicit or world in WORLDS:
            self.selection_explicit[world] = True

    def has_explicit_selection(self, world: str) -> bool:
        """True when this config carries a selection the user MADE for `world`.

        PER-WORLD MEANS PER-WORLD. This used to fall back to the legacy GLOBAL
        predicate `has_explicit_structure_selection()`, whose marker half is
        `any(self.selection_explicit.values())` -- so marking ONE world explicit
        marked ALL FIVE, and touching a grave structure row silently switched
        castle's structures to "the user chose exactly these" too. A legacy
        file's global marker is already migrated into every per-world flag by
        `from_dict`, so dropping the global read here loses nothing and stops one
        world's edit leaking into the other four.

        The second, self-evident half is kept but narrowed to THIS world: a world
        that already names an enabled route has plainly been chosen for.
        """
        if bool(self.selection_explicit.get(world, False)):
            return True
        return bool(self.enabled_structure_arrival_keys(world))

    # ---- construction ----------------------------------------------------
    @classmethod
    def default(cls) -> "SpawnConfig":
        scale = _default_scale()
        items = {
            tid: {"weight": _scaled_default(tid, scale), "enabled": True}
            for tid in ITEM_TYPES
        }
        structures = {
            tid: {"weight": _scaled_default(tid, scale), "enabled": True}
            for tid in STRUCTURE_TYPES
        }
        enemies = {}
        for world in WORLDS:
            avail = _WORLD_ENEMY_AVAIL[world]
            enemies[world] = {
                tid: {"weight": _enemy_default(tid), "enabled": True}
                for tid in ENEMY_TYPES if tid in avail
            }
        enemy_tiers = {}
        for world in WORLDS:
            tbl = {}
            for tid, (name, tiers) in ENEMY_TIER_TYPES_PER_WORLD.get(world, {}).items():
                bias = ENEMY_TIER_WEIGHTS_PER_TYPE.get(tid, {})
                tbl[tid] = {
                    t: float(bias.get(t, TIER_DEFAULT_EQUAL)) for t in tiers
                }
            enemy_tiers[world] = tbl
        specials = {
            key: {"weight": SPECIAL_DEFAULTS[key], "enabled": True}
            for key in SPECIAL_NAMES
        }
        ability_drops = {
            aid: {"weight": ABILITY_DROP_DEFAULT_WEIGHT, "enabled": False}
            for aid in ABILITY_DROP_TYPES
        }
        # Only SELECTABLE routes get an entry. Because `from_dict` overlays
        # onto these defaults and never creates a key, a saved file cannot hand
        # a held-back / inactive / unmeasured route an entry to switch on.
        arrivals = default_arrivals()
        # Same rule, one stricter default: only SELECTABLE structure routes get
        # an entry, and every entry is DISABLED (Req 6.2).
        structure_arrivals = default_structure_arrivals()
        # THE UNIFIED TABLE, alongside the three legacy ones. See
        # `default_unified_weights` for why route defaults are NOT uniform.
        unified_weights = default_unified_weights()
        return cls(items, structures, enemies, enemy_tiers, specials,
                   ability_drops, arrivals, structure_arrivals,
                   unified_weights, UNIFIED_ENEMY_SHARE_DEFAULT,
                   # BOTH FEATURES ON by default: the stock config must
                   # reproduce today's behaviour exactly, and these switches
                   # exist only so the user can turn one OFF.
                   cross_world_enemies_enabled=True,
                   cross_world_structures_enabled=True)

    # ---- JSON ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": CONFIG_VERSION,
            "items": {f"0x{t:02X}": v for t, v in self.items.items()},
            "structures": {f"0x{t:02X}": v for t, v in self.structures.items()},
            "enemies": {
                world: {f"0x{t:02X}": v for t, v in tbl.items()}
                for world, tbl in self.enemies.items()
            },
            "enemy_tiers": {
                world: {
                    f"0x{t:02X}": {str(tier): w for tier, w in tiers.items()}
                    for t, tiers in tbl.items()
                }
                for world, tbl in self.enemy_tiers.items()
            },
            "specials": dict(self.specials),
            "ability_drops": {
                f"0x{t:02X}": v for t, v in self.ability_drops.items()
            },
            "arrivals": {world: dict(tbl)
                         for world, tbl in self.arrivals.items()},
            "structure_arrivals": {
                world: dict(tbl)
                for world, tbl in self.structure_arrivals.items()},
            # The "the user chose" marker. Written from the same predicate the
            # draw reads, so a saved all-off selection the user actually made
            # comes back as all-off instead of falling back to everything.
            "structure_selection_explicit":
                bool(self.has_explicit_structure_selection()),
            # THE UNIFIED SECTION (version 4). Written ALONGSIDE the legacy
            # sections above, which stay in the file byte-for-byte as before, so
            # this task's files remain readable by the legacy paths and no build
            # changes. Keys are emitted in SORTED unified-id order (Req 6.4).
            "unified": {
                "enemy_share_target": float(self.enemy_share_target),
                # THE TWO FEATURE SWITCHES. `CONFIG_VERSION` is deliberately
                # NOT bumped for them: `from_dict` overlays onto `default()`,
                # so an existing version-4 file that predates these keys loads
                # with both True -- the shipped behaviour -- rather than
                # needing a migration.
                "cross_world_enemies_enabled":
                    bool(self.cross_world_enemies_enabled),
                "cross_world_structures_enabled":
                    bool(self.cross_world_structures_enabled),
                "weights": {
                    world: {k: dict(tbl[k]) for k in sorted(tbl)}
                    for world, tbl in self.unified_weights.items()
                },
                # Written from the SAME predicate the draw reads
                # (`has_explicit_selection`), not from the raw per-world dict,
                # for the same reason the legacy `structure_selection_explicit`
                # key above is: an enabled route IS a selection however it was
                # produced, so the implied-explicit case must survive the save.
                # Writing the raw dict instead breaks the Req 8.6 round trip --
                # the legacy key would come back True while these came back
                # False, and the two markers would disagree in the file.
                "selection_explicit": {
                    world: bool(self.has_explicit_selection(world))
                    for world in WORLDS
                },
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpawnConfig":
        # Start from defaults so missing/added types are handled gracefully,
        # then overlay whatever the saved file specifies.
        cfg = cls.default()
        # VERSION <= 3 MIGRATION (task 2). This runs BEFORE the overlays and
        # feeds THEM, rather than bypassing them: `migrate_v3_with_report`
        # returns a complete v4 dict whose legacy sections are untouched and
        # whose `unified` section is synthesised from them, and every value then
        # goes through exactly the same clamp-and-drop overlay below. So Req 8.5
        # is preserved unchanged -- a v4 file with no `unified` section is not
        # migrated and simply keeps the defaults -- and an unknown Candidate id
        # still cannot be created. All-or-nothing: `MigrationError` (U9)
        # propagates, so a partially mapped config is never returned.
        report = None
        if needs_migration(d):
            d, report = migrate_v3_with_report(d)

        def _overlay(target: dict, saved: dict):
            for k, v in (saved or {}).items():
                try:
                    tid = int(k, 16) if isinstance(k, str) else int(k)
                except (ValueError, TypeError):
                    continue
                if tid not in target:
                    continue
                if isinstance(v, dict):
                    if "weight" in v:
                        target[tid]["weight"] = _clamp(v["weight"])
                    if "enabled" in v:
                        target[tid]["enabled"] = bool(v["enabled"])

        _overlay(cfg.items, d.get("items"))
        _overlay(cfg.structures, d.get("structures"))
        for world, tbl in (d.get("enemies") or {}).items():
            if world in cfg.enemies:
                _overlay(cfg.enemies[world], tbl)
        # Enemy tiers (class levels).
        for world, tbl in (d.get("enemy_tiers") or {}).items():
            if world not in cfg.enemy_tiers:
                continue
            for k, tiers in (tbl or {}).items():
                try:
                    tid = int(k, 16) if isinstance(k, str) else int(k)
                except (ValueError, TypeError):
                    continue
                if tid not in cfg.enemy_tiers[world]:
                    continue
                for tk, w in (tiers or {}).items():
                    try:
                        tier = int(tk)
                    except (ValueError, TypeError):
                        continue
                    if tier in cfg.enemy_tiers[world][tid]:
                        cfg.enemy_tiers[world][tid][tier] = _clamp(w)
        # Specials (mimic / wizard) — string-keyed.
        for k, v in (d.get("specials") or {}).items():
            if k in cfg.specials and isinstance(v, dict):
                if "weight" in v:
                    cfg.specials[k]["weight"] = _clamp(v["weight"])
                if "enabled" in v:
                    cfg.specials[k]["enabled"] = bool(v["enabled"])
        # Ability drop-table weights (defaults first, then overlay).
        for k, v in (d.get("ability_drops") or {}).items():
            try:
                aid = int(k, 16) if isinstance(k, str) else int(k)
            except (ValueError, TypeError):
                continue
            if aid not in cfg.ability_drops or not isinstance(v, dict):
                continue
            if "weight" in v:
                cfg.ability_drops[aid]["weight"] = _clamp(
                    v["weight"], 0.0, float(ABILITY_DROP_WEIGHT_MAX))
            if "enabled" in v:
                cfg.ability_drops[aid]["enabled"] = bool(v["enabled"])
        # Cross-world arrivals. A MISSING SECTION (every pre-version-2 file)
        # simply leaves the defaults in place. Unknown worlds and unknown route
        # keys are DROPPED, never created: this overlay can only narrow.
        for world, tbl in (d.get("arrivals") or {}).items():
            if world not in cfg.arrivals:
                continue
            for k, v in (tbl or {}).items():
                if k not in cfg.arrivals[world] or not isinstance(v, dict):
                    continue
                if "weight" in v:
                    cfg.arrivals[world][k]["weight"] = _clamp(v["weight"])
                if "enabled" in v:
                    cfg.arrivals[world][k]["enabled"] = bool(v["enabled"])
        # CROSS-WORLD STRUCTURE ARRIVALS. Identical overlay discipline: a
        # missing section (every pre-version-3 file) leaves the all-off defaults
        # in place, and unknown worlds / unknown route keys are DROPPED rather
        # than created, so a hand-edited file cannot enable a route the
        # structure allowlist and the measured cost table did not offer.
        for world, tbl in (d.get("structure_arrivals") or {}).items():
            if world not in cfg.structure_arrivals:
                continue
            for k, v in (tbl or {}).items():
                if (k not in cfg.structure_arrivals[world]
                        or not isinstance(v, dict)):
                    continue
                if "weight" in v:
                    cfg.structure_arrivals[world][k]["weight"] = _clamp(
                        v["weight"])
                if "enabled" in v:
                    cfg.structure_arrivals[world][k]["enabled"] = bool(
                        v["enabled"])
        # The "the user chose" marker. ABSENT in every file written before this
        # fix, which is exactly right: such a file gets no marker and therefore
        # falls back to every selectable route when the switch is on -- unless it
        # already names an enabled route, which `has_explicit_structure_selection`
        # reads as a selection on its own.
        if bool(d.get("structure_selection_explicit", False)):
            cfg.mark_structure_selection_explicit()
        # THE UNIFIED SECTION. A MISSING SECTION -- every version <= 3 file --
        # simply leaves the stock unified defaults in place: defaults, never a
        # crash (Req 8.5). Same overlay discipline as every other section:
        # unknown worlds and unknown Candidate ids are DROPPED, never created,
        # so a hand-edited file cannot invent a Candidate the permission oracle
        # never offered. (Mapping a v3 file's legacy tables INTO this table is
        # task 2's migration; this task only keeps v3 files loading.)
        unified = d.get("unified") or {}
        if isinstance(unified, dict):
            if "enemy_share_target" in unified:
                cfg.enemy_share_target = _clamp_share(
                    unified.get("enemy_share_target"))
            # THE TWO FEATURE SWITCHES. A MISSING KEY means the default, which
            # is True -- an older version-4 file, or any file written before
            # these existed, therefore loads with both features ON and behaves
            # exactly as it did. Only an explicit `false` turns one off.
            if "cross_world_enemies_enabled" in unified:
                cfg.cross_world_enemies_enabled = bool(
                    unified.get("cross_world_enemies_enabled"))
            if "cross_world_structures_enabled" in unified:
                cfg.cross_world_structures_enabled = bool(
                    unified.get("cross_world_structures_enabled"))
            for world, tbl in (unified.get("weights") or {}).items():
                if world not in cfg.unified_weights:
                    continue
                for k, v in (tbl or {}).items():
                    if (k not in cfg.unified_weights[world]
                            or not isinstance(v, dict)):
                        continue
                    if "weight" in v:
                        cfg.unified_weights[world][k]["weight"] = \
                            _clamp_unified(v["weight"])
                    if "enabled" in v:
                        cfg.unified_weights[world][k]["enabled"] = bool(
                            v["enabled"])
            for world, flag in (unified.get("selection_explicit")
                                or {}).items():
                if world in cfg.selection_explicit and bool(flag):
                    cfg.selection_explicit[world] = True
        cfg.migration_report = report
        return cfg

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "SpawnConfig":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)

    # ---- effective-weight helpers (consumed by the randomizer) -----------
    def chest_special_chances(self) -> dict[str, float]:
        """Per-chest % chance (0-100) that a wooden chest becomes a mimic /
        wizard. A disabled special contributes 0. The chest randomizer treats
        these as independent probabilities (clamped so mimic+wizard <= 100)."""
        out: dict[str, float] = {}
        for key in SPECIAL_NAMES:
            ent = self.specials.get(key, {})
            out[key] = float(ent.get("weight", 0)) if ent.get("enabled", True) else 0.0
        return out

    def ability_drop_overrides(self) -> dict[int, int]:
        """One weight per ability id: `{ability_id: weight}`.

        Once the user has enabled ANY id, the editor becomes authoritative:
        enabled ids get their chosen weight and unchecked ids get weight 0, so
        an unchecked power-up never drops (elf_patch's zero guard still keeps a
        table from ending up entirely empty). Returns an empty dict while the
        user hasn't enabled anything, which leaves vanilla alone."""
        out: dict[int, int] = {}
        any_enabled = any(ent.get("enabled", False)
                          for ent in self.ability_drops.values())
        if not any_enabled:
            return out
        for aid, ent in self.ability_drops.items():
            if ent.get("enabled", False):
                out[int(aid)] = int(round(float(ent.get("weight", 0))))
            else:
                out[int(aid)] = 0
        return out

    # ---- cross-world arrivals -------------------------------------------
    def enabled_arrival_keys(self, world: str) -> set[tuple[int, str, str]]:
        """The route keys this config WANTS into `world`, re-validated.

        Validation is redone against `selectable_arrival_keys` every time, so a
        hand-edited JSON that names a held-back route gets nothing: the result
        is always a SUBSET of what the catalog and the measured cost table
        already permit.
        """
        allowed = selectable_arrival_keys(world)
        out: set[tuple[int, str, str]] = set()
        for k, ent in self.arrivals.get(world, {}).items():
            parsed = parse_arrival_route_key(k)
            if parsed is None:
                continue
            key = (parsed[0], parsed[1], world)
            if key not in allowed:
                continue
            if ent.get("enabled", True) and float(ent.get("weight", 0)) > 0:
                out.add(key)
        return out

    def arrival_weights_for_world(self, world: str) -> dict[int, float]:
        """{enemy_id: weight} for the enabled arrivals into `world`.

        Several enabled routes for the same enemy collapse to the HIGHEST
        weight: the draw admits at most one source per enemy anyway, so a sum
        would silently inflate an enemy that happens to have two doors in.
        """
        out: dict[int, float] = {}
        for key in self.enabled_arrival_keys(world):
            enemy_id = key[0]
            ent = self.arrivals.get(world, {}).get(
                arrival_route_key(enemy_id, key[1]), {})
            w = float(ent.get("weight", 0))
            if w > out.get(enemy_id, 0.0):
                out[enemy_id] = w
        return out

    def disabled_arrivals(self, world: str) -> set[int]:
        """Arriving enemy ids the user switched OFF for `world` entirely.

        An enemy with a selectable route into the world but no ENABLED one. It
        joins `disabled_enemies`, so even if something ported it the placer will
        not put it in a map.
        """
        enabled = {key[0] for key in self.enabled_arrival_keys(world)}
        offered = {key[0] for key in selectable_arrival_keys(world)}
        return offered - enabled

    def narrow_allowlist(self, allowlist: dict) -> dict:
        """FILTER an active allowlist down to the routes the user left enabled.

        A pure filter: keys are only ever removed. Worlds this config knows
        nothing about are left untouched, and a world with every arrival
        disabled simply contributes no routes — that is a clean "no cross-world
        arrivals here", which the caller logs.
        """
        out = {}
        for key, entry in allowlist.items():
            world = key[2]
            if world not in self.arrivals:
                out[key] = entry
                continue
            if key in self.enabled_arrival_keys(world):
                out[key] = entry
        return out

    # ---- cross-world STRUCTURE arrivals ---------------------------------
    def mark_structure_selection_explicit(self) -> None:
        """Record that the user made a structure-arrival choice IN THE EDITOR.

        Called on every tick and every UNTICK, which is what makes "I visited
        the editor and turned everything off" distinguishable from "I never
        chose". Once set it stays set and it is serialised, so an all-off
        selection the user actually made keeps meaning all-off.
        """
        self.structure_selection_explicit = True

    def has_explicit_structure_selection(self) -> bool:
        """True when this config carries a structure selection the user MADE.

        Two ways to be true, and they are the honest ones:
          1. the explicit marker is set (the editor set it, or it round-tripped
             in from the `structure_selection_explicit` key);
          2. some world already has at least one ENABLED, still-selectable route
             -- a selection is self-evident from its own content.

        False for the stock config, for a pre-version-3 file with no
        `structure_arrivals` section, and for a config built by hand without one.
        In those cases the switch alone decides (see
        `structure_routes_for_world`).
        """
        if self.structure_selection_explicit:
            return True
        return any(self.enabled_structure_arrival_keys(w) for w in WORLDS)

    def enabled_structure_arrival_keys(self, world: str) -> set[tuple[int, str, str]]:
        """The STRUCTURE route keys this config WANTS into `world`, re-validated.

        Re-derived against `selectable_structure_arrival_keys` every call, so the
        result is always a SUBSET of what the structure allowlist and the
        measured cost table already permit. A config that names nothing (the
        default, and an old file that has no section) returns the empty set,
        which the draw reads as "port no structures".
        """
        allowed = selectable_structure_arrival_keys(world)
        out: set[tuple[int, str, str]] = set()
        for k, ent in self.structure_arrivals.get(world, {}).items():
            parsed = parse_arrival_route_key(k)
            if parsed is None:
                continue
            key = (parsed[0], parsed[1], world)
            if key not in allowed:
                continue
            # `enabled` defaults FALSE here, the opposite of the enemy table: a
            # malformed entry with no flag must not become an enabled route.
            if ent.get("enabled", False) and float(ent.get("weight", 0)) > 0:
                out.add(key)
        return out

    def enabled_unified_structure_routes(self, world: str) -> set[tuple[int, str, str]]:
        """The STRUCTURE routes the UNIFIED table says the user wants into `world`.

        THIS EXISTS BECAUSE THE EDITOR AND THE DRAW READ DIFFERENT TABLES. The
        spawn-rate editor writes `unified_weights[world]["structure:<tid>-<src>"]`,
        but `structure_routes_for_world`'s EXPLICIT branch used only
        `enabled_structure_arrival_keys`, which reads the LEGACY
        `structure_arrivals` table -- and nothing mirrors one into the other. So
        ticking a structure row marked the world EXPLICIT (which is what the
        editor intends) while the tick itself landed somewhere the draw never
        looked, and the explicit branch then returned the legacy table's
        all-disabled contents. MEASURED on a real config: 0 routes in every
        world, so no structure could ever port, and touching ONE row was enough
        to cause it.

        Same discipline as every other projection: re-validated against
        `selectable_structure_arrival_keys`, so this NARROWS the oracle and can
        never invent a route it withheld.
        """
        allowed = selectable_structure_arrival_keys(world)
        out: set[tuple[int, str, str]] = set()
        for key in self.unified_weights.get(world, {}):
            parsed = parse_unified_key(key)
            if parsed is None or parsed[0] != UNIFIED_KIND_STRUCTURE:
                continue
            route = (parsed[1], parsed[2], world)
            if route not in allowed:
                continue
            # Structure rows ship DISABLED, so a missing flag means disabled --
            # the same stance the legacy table takes.
            if (self.unified_enabled(world, key, False)
                    and self.unified_weight(world, key) > 0):
                out.add(route)
        return out

    def structure_arrival_weights_for_world(self, world: str) -> dict[int, float]:
        """{type_id: weight} for the enabled structure arrivals into `world`.

        Several enabled routes for the same structure collapse to the HIGHEST
        weight, for the same reason the enemy table does it: the draw admits at
        most one source per type, so summing would silently inflate a structure
        that happens to have two doors in.
        """
        out: dict[int, float] = {}
        for key in self.enabled_structure_arrival_keys(world):
            type_id = key[0]
            ent = self.structure_arrivals.get(world, {}).get(
                arrival_route_key(type_id, key[1]), {})
            w = float(ent.get("weight", 0))
            if w > out.get(type_id, 0.0):
                out[type_id] = w
        return out

    def disabled_enemies(self, world: str) -> set[int]:
        """Enemy type-ids that must NOT spawn in this world (disabled or 0).

        Covers NATIVES (the per-world enemy table) and ARRIVALS (a cross-world
        route the user switched off).
        """
        tbl = self.enemies.get(world, {})
        out = set()
        for tid, ent in tbl.items():
            if not ent.get("enabled", True) or ent.get("weight", 0) <= 0:
                out.add(tid)
        return out | self.disabled_arrivals(world)

    def enemy_weights_for_world(self, world: str) -> dict[int, float]:
        """Per-enemy relative weights for a world (enabled types only).

        Arrivals are merged in on the same scale as the natives, which is what
        makes the arrival weight reach the even-out balancing bag. It BIASES the
        bag and nothing else: the seeded draw and the admission walk are
        untouched, so a weight can never force a specific arrival to appear.
        """
        tbl = self.enemies.get(world, {})
        out: dict[int, float] = {}
        for tid, ent in tbl.items():
            if ent.get("enabled", True) and ent.get("weight", 0) > 0:
                out[tid] = float(ent["weight"])
        for tid, w in self.arrival_weights_for_world(world).items():
            out.setdefault(tid, w)
        return out

    def enemy_tier_weights_for_world(self, world: str) -> dict[int, dict[int, float]]:
        """Per-enemy class-level (tier) weights for a world, as consumed by
        reroll_enemy_tier: {type_id: {tier: weight}}. Every tier-eligible type
        is included (even all-zero) so the randomizer respects the user's
        choice to suppress a level rather than falling back to a uniform roll."""
        out: dict[int, dict[int, float]] = {}
        for tid, tiers in self.enemy_tiers.get(world, {}).items():
            out[tid] = {int(t): float(w) for t, w in tiers.items()}
        return out

    def structure_arrival_weight_overlay(
            self, world: str, arrived_types) -> dict[int, float]:
        """{type_id: weight} to OVERLAY on the generic `structures` weights for
        the structure types that were actually PORTED into `world`.

        WHY THIS EXISTS. The editor's per-world structure tab has a Weight
        column, and until this method existed that column was INERT: the tick
        narrowed the draw, but placement frequency came from
        `universal_weights`, which only ever read the generic `self.structures`
        entry for the type (0.6-2.1 on the config scale — a third of a percent of
        the pool). The user's number reached nothing. This makes the arrival
        weight the AUTHORITY for a route that ported.

        `arrived_types` is the set of type ids the run genuinely committed into
        this world; the caller owns that fact (cli reads it off the structure
        pass's `committed_types`). Passing something else cannot widen anything:
        the result is intersected with the type ids the structure allowlist and
        the measured cost table already offer as ARRIVALS INTO THIS WORLD, so a
        type that is NATIVE to `world` — or one no route targets — is never in
        the result and keeps its generic weight untouched.

        Weight precedence, highest first:
          1. an ENABLED route's weight (`structure_arrival_weights_for_world`) —
             the user ticked the row and set a number;
          2. the stored weight of any route for that type into this world — the
             fallback case, where the switch alone made every selectable route a
             candidate and the row was never ticked, so the stored default is
             what the user implicitly accepted;
          3. `_structure_arrival_default(tid)` — no stored entry at all (an old
             config file with no `structure_arrivals` section).
        """
        wanted = {int(t) for t in (arrived_types or ())}
        if not wanted:
            return {}
        offered = {k[0] for k in selectable_structure_arrival_keys(world)}
        wanted &= offered
        ticked = self.structure_arrival_weights_for_world(world)
        table = self.structure_arrivals.get(world, {})
        stored: dict[int, float] = {}
        for k, ent in table.items():
            parsed = parse_arrival_route_key(k)
            if parsed is None:
                continue
            w = float(ent.get("weight", 0) or 0)
            if w > stored.get(parsed[0], 0.0):
                stored[parsed[0]] = w
        out: dict[int, float] = {}
        for tid in wanted:
            w = ticked.get(tid) or stored.get(tid) or 0.0
            out[tid] = w if w > 0 else _structure_arrival_default(tid)
        return out

    def universal_weights(self, world: str,
                          arrived_structure_types=None) -> dict[int, float]:
        """Weight dict for randomize_universal()'s pool in a given world:
        every enabled item + structure, plus the enemies available AND
        enabled in that world. Disabled entries are omitted (weight 0 →
        randomize_universal excludes them).

        `arrived_structure_types` is OPTIONAL and defaults to None, which is the
        whole of the compatibility story: this method is called for EVERY world,
        including worlds with no arrivals, and on builds where cross-world
        structures are off entirely or the feature does not run at all. In all of
        those cases the caller passes nothing, the overlay loop below does not
        execute, and the returned dict is byte-for-byte what it was before the
        overlay existed. Only a caller that can name the types a structure pass
        ACTUALLY COMMITTED into this world can change the result, and then only
        for those types (see `structure_arrival_weight_overlay`).

        The overlay is applied AFTER the generic `structures` merge — it is meant
        to beat it — and BEFORE the enemy merge, which is unaffected either way
        because the two type sets are disjoint and the enemy merge assigns
        unconditionally.
        """
        out: dict[int, float] = {}
        for tid, ent in self.items.items():
            if ent.get("enabled", True) and ent.get("weight", 0) > 0:
                out[tid] = float(ent["weight"])
        for tid, ent in self.structures.items():
            if ent.get("enabled", True) and ent.get("weight", 0) > 0:
                out[tid] = float(ent["weight"])
        if arrived_structure_types:
            for tid, w in self.structure_arrival_weight_overlay(
                    world, arrived_structure_types).items():
                if w > 0:
                    out[tid] = w
        for tid, w in self.enemy_weights_for_world(world).items():
            out[tid] = w
        return out

    # ---- THE THREE UNIFIED PROJECTIONS -----------------------------------
    # COMPUTED, NEVER STORED. One table in, three views out. Every loop below
    # iterates SORTED unified ids, so no result depends on interpreter hashing
    # (Req 6.4).
    #
    # NOTHING CONSUMES THESE YET (task 3/4). `universal_weights`,
    # `enemy_weights_for_world`, `arrivals` and `structure_arrivals` above are
    # untouched and still drive the build, so this task changes no build output.
    def unified_entry(self, world: str, key: str) -> dict:
        """The stored `{"weight", "enabled"}` for one Candidate id, or {}."""
        return self.unified_weights.get(world, {}).get(key, {})

    def unified_weight(self, world: str, key: str) -> float:
        ent = self.unified_entry(world, key)
        try:
            return float(ent.get("weight", 0) or 0)
        except (ValueError, TypeError):
            return 0.0

    def unified_enabled(self, world: str, key: str, default: bool) -> bool:
        ent = self.unified_entry(world, key)
        if "enabled" not in ent:
            return default
        return bool(ent.get("enabled"))

    def native_weights(self, world: str, committed_routes=()) -> dict[int, float]:
        """PROJECTION 1 -> `items.randomize_universal`'s pool for `world`.

        Every `native:*` Candidate whose type is in the world's pool and which is
        enabled with weight > 0, PLUS every `enemy:*` / `structure:*` route in
        `committed_routes` -- a ported type IS a placeable type, which is the
        whole reason the three tables became one.

        `committed_routes` is the set of routes a run genuinely committed into
        this world, as `(type_id, source_world, target_world)` tuples or
        `Candidate`s. It defaults to EMPTY, so calling this with no argument
        gives the pool with no arrivals in it. A committed route only reaches the
        result if the permission oracle still offers it, so passing junk cannot
        widen anything: this NARROWS.

        THE `enemy_share_target` RENORMALIZATION APPLIES HERE AND ONLY HERE.
        It scales the enemy block so enemies are `enemy_share_target` of the
        returned pool, which is where "how often does a slot become an enemy" is
        actually decided. Route weights are NOT rescaled: route admission is
        governed by the byte budget and the phase order, not by a share.
        """
        table = self.unified_weights.get(world, {})
        pool = set(native_pool_types(world))
        out: dict[int, float] = {}
        for key in sorted(table):
            parsed = parse_unified_key(key)
            if parsed is None or parsed[0] != UNIFIED_KIND_NATIVE:
                continue
            tid = parsed[1]
            if tid not in pool:
                continue
            if not self.unified_enabled(world, key, True):
                continue
            w = self.unified_weight(world, key)
            if w > 0:
                out[tid] = w
        # Committed routes join the pool on the SAME scale. Re-validated against
        # the oracle, and several sources for one type collapse to the HIGHEST
        # weight (the pass commits at most one source per type, so summing would
        # silently inflate a type that happens to have two doors in).
        allowed_enemy = selectable_arrival_keys(world)
        allowed_structure = selectable_structure_arrival_keys(world)
        for key, tid, weight in self._committed_route_weights(
                world, committed_routes, allowed_enemy, allowed_structure):
            del key
            if weight > out.get(tid, 0.0):
                out[tid] = weight
        return self._apply_enemy_share(out)

    def _committed_route_weights(self, world, committed_routes, allowed_enemy,
                                 allowed_structure):
        """`(unified id, type_id, weight)` for each permitted committed route,
        in sorted unified-id order."""
        rows: list[tuple[str, int, float]] = []
        for item in (committed_routes or ()):
            if isinstance(item, Candidate):
                kind, tid, source = item.kind, item.type_id, item.source_world
                if kind not in UNIFIED_ROUTE_KINDS or not source:
                    continue
            else:
                try:
                    tid, source = int(item[0]), str(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                route = (tid, source, world)
                if route in allowed_enemy:
                    kind = UNIFIED_KIND_ENEMY
                elif route in allowed_structure:
                    kind = UNIFIED_KIND_STRUCTURE
                else:
                    continue
            route = (tid, source, world)
            allowed = (allowed_enemy if kind == UNIFIED_KIND_ENEMY
                       else allowed_structure)
            if route not in allowed:
                continue
            key = unified_route_key(kind, tid, source)
            w = self.unified_weight(world, key)
            if w <= 0:
                w = (_unified_enemy_route_default(tid)
                     if kind == UNIFIED_KIND_ENEMY
                     else _unified_structure_route_default(tid))
            rows.append((key, tid, float(w)))
        rows.sort(key=lambda r: r[0])
        return rows

    def _apply_enemy_share(self, weights: dict[int, float]) -> dict[int, float]:
        """Scale the enemy block so enemies are `enemy_share_target` of the pool.

        The same algebra `items.randomize_universal` uses:
            s * E * (1 - t) = I * t  =>  s = I*t / (E*(1-t))
        A pool with no enemies, or no items, is returned untouched -- there is no
        share to hit and inventing one would mean dividing by zero.
        """
        target = self.enemy_share_target
        try:
            target = float(target)
        except (ValueError, TypeError):
            return dict(weights)
        if not (0.0 < target < 1.0):
            return dict(weights)
        enemy_total = sum(w for tid, w in weights.items()
                          if tid in ENEMY_DESTINATIONS)
        item_total = sum(w for tid, w in weights.items()
                         if tid not in ENEMY_DESTINATIONS)
        if enemy_total <= 0 or item_total <= 0:
            return dict(weights)
        scale = (item_total * target) / (enemy_total * (1.0 - target))
        out: dict[int, float] = {}
        for tid in sorted(weights):
            w = weights[tid]
            out[tid] = round(w * scale, 4) if tid in ENEMY_DESTINATIONS else w
        return out

    def enemy_route_weights(self, world: str) -> dict[tuple[int, str, str], float]:
        """PROJECTION 2 -> admission phase 1 (enemies, priority).

        `{(type_id, source_world, target_world): weight}` for every `enemy:*`
        Candidate targeting `world` that is enabled, has weight > 0 and PASSES
        THE PERMISSION ORACLE. The oracle set (`selectable_arrival_keys`) is the
        ceiling: this projection can only ever remove routes from it, never add
        one, so a hand-edited config cannot admit a held-back, inactive or
        unmeasured route.
        """
        allowed = selectable_arrival_keys(world)
        out: dict[tuple[int, str, str], float] = {}
        for key in sorted(self.unified_weights.get(world, {})):
            parsed = parse_unified_key(key)
            if parsed is None or parsed[0] != UNIFIED_KIND_ENEMY:
                continue
            route = (parsed[1], parsed[2], world)
            if route not in allowed:
                continue
            # Enemy routes default ENABLED, matching `default_arrivals()`.
            if not self.unified_enabled(world, key, True):
                continue
            w = self.unified_weight(world, key)
            if w > 0:
                out[route] = w
        return out

    def structure_route_weights(self, world: str) -> dict[tuple[int, str, str], float]:
        """PROJECTION 3 -> admission phase 2 (structures).

        Same shape and the same oracle as projection 2, plus THE FALLBACK, which
        is preserved exactly as `structure_routes_for_world` defines it:

          * EXPLICIT -- the user made a selection for this world, so their subset
            is honoured EXACTLY, including the empty one.
          * FALLBACK -- no explicit selection was ever saved, so the switch alone
            means yes: every SELECTABLE structure route is a candidate at its
            stored (or default) weight even though the stock entry is
            `enabled=False`, and the byte budget decides how many fit.

        The candidate set comes FROM `structure_routes_for_world` rather than
        being re-derived here, so generalising the explicit marker to
        `selection_explicit` cannot lose the fallback. In both cases the result
        is a subset of the oracle.
        """
        routes, case = structure_routes_for_world(self, world)
        del case  # the caller that needs to name the case can ask directly
        allowed = selectable_structure_arrival_keys(world)
        out: dict[tuple[int, str, str], float] = {}
        for route in sorted(routes):
            if route not in allowed:
                continue
            key = unified_route_key(UNIFIED_KIND_STRUCTURE, route[0], route[1])
            w = self.unified_weight(world, key)
            if w <= 0:
                w = _unified_structure_route_default(route[0])
            out[route] = float(w)
        return out


#: Ceiling for a UNIFIED weight. The unified table is on
#: `DEFAULT_ITEM_WEIGHTS`'s scale, whose largest stock entry is 471 (GoldCoin),
#: so the legacy 0-100 clamp would silently crush the stock numbers. 2,000 is
#: simply well clear of the whole stock table; weights are relative, so the
#: ceiling only has to stop a nonsense value, not encode a meaning.
UNIFIED_WEIGHT_MAX = 2000.0


def _clamp_unified(value) -> float:
    return _clamp(value, 0.0, UNIFIED_WEIGHT_MAX)


def _clamp_share(value) -> float:
    """Clamp an `enemy_share_target`. 0 (or anything <= 0) means OFF -- use the
    typed weights -- and is kept as 0.0. A value inside (0, 1) is a real forced
    share. Anything else is unreadable and falls back to the default, which is
    also OFF, so a bad value can never force a share nobody asked for."""
    try:
        v = float(value)
    except (ValueError, TypeError):
        return UNIFIED_ENEMY_SHARE_DEFAULT
    if v <= 0.0:
        return 0.0
    if not (0.0 < v < 1.0):
        return UNIFIED_ENEMY_SHARE_DEFAULT
    return round(v, 4)


def _clamp(value, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        v = float(value)
    except (ValueError, TypeError):
        return lo
    return max(lo, min(hi, round(v, 1)))


def load_spawn_config(path: str | Path | None):
    """Load a SpawnConfig from `path`. Returns None when no path is given or
    the file doesn't exist — the caller then uses stock behavior."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return SpawnConfig.load(p)
    except MigrationError:
        # ALL-OR-NOTHING (Req 8.3). A version <= 3 file that cannot be fully
        # migrated must NOT fall through to "stock behavior": that would be the
        # silent reversion of the user's arrival selections the requirement
        # forbids. U9 propagates to the caller.
        raise
    except Exception:
        return None
