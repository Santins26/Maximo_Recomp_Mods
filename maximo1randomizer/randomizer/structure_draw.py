"""The structure draw, budget and destination permitting (Task 5 of
`cross-world-structures`).

THE SWITCH IS THE USER'S, IT IS EXPERIMENTAL, AND IT IS OFF BY DEFAULT
    `--cross-world-structures` (CLI) / "Cross-world structures (EXPERIMENTAL)"
    (GUI) is the one switch, threaded explicitly at both the `_Args` and
    `patch_iso` sites, defaulting OFF. `cli` reads it in exactly one place and
    passes `structure_fn=None` when it is off, so `run_cross_world_placements`
    behaves exactly as it did before the parameter existed and the build is
    byte-identical to one of a tree without this feature (Requirement 7.3,
    design Property 1). `STRUCTURE_PORT_ENABLED` below survives as a MAINTAINER
    kill switch that `cli` also requires.

    THE SWITCH ITSELF MEANS YES (bugfix). `run_structure_pass(enabled_routes=...)`
    still only ever REMOVES routes from the draw, but the set `cli` hands it is
    now built by `spawn_config.structure_routes_for_world`: a user who made no
    explicit per-route selection gets EVERY SELECTABLE route as a candidate and
    the shared resource budget decides how many fit, while a user who did select
    gets exactly that subset. The old behaviour -- the flag on and the candidate
    set always empty, because `spawn_config.structure_arrivals` ships every entry
    DISABLED and an old config file has no section at all -- was a defect: no
    structure could ever be ported. The PERMISSION oracle is untouched; only the
    answer to "which routes did the user want" changed.

WHAT THIS MODULE ADDS, AND WHAT IT DELIBERATELY DOES NOT
    It adds SELECTION, BUDGET ACCOUNTING and REPORTING. It adds no porter, no
    placer, no permission oracle and no second budget:

      * porting is `cross_world.port_structure_route`, which hand-builds a
        `PlacementPlan(path=PORT)` and calls `companion_port.port_group` ->
        `session.port_enemy` unchanged (Req 1.1, 1.4);
      * the budget is `roster_selection.pool_budget` and the admission rules are
        `roster_selection.AdmissionGate`, the SAME gate and the SAME live
        `len(session.prs) + len(session.prt)` read the enemy draw uses (Req 5.1);
      * permission is `catalog.load_structure_allowlist` plus
        `roster_costs.structure_cost_for`. A route with no measured cost row is
        excluded for every seed, with no fallback and no estimate (Req 4.2);
      * placement is the ITEM pool (`randomize_universal`). This feature widens
        that pool's TYPE set for a world and never touches which RECORDS are
        eligible (Req 3.5, design Property 4).

HOW A STRUCTURE CANNOT DISPLACE A COMMITTED ENEMY (Req 5.2)
    By ORDER, still -- `STRUCTURE_RESERVE_BYTES` (below) lowers the ENEMY gate's
    CEILING before the enemy walk starts, which is not the same thing as
    displacing anything: no enemy decision is revisited, and a structure is still
    only ever considered after every enemy group for the world. `cross_world`
    calls the structure pass ONCE PER WORLD, after every enemy group for that
    world has been attempted and before `session.finish()`. By then the enemy
    arrivals are already committed into the session's buffers, so the live size
    the structure gate reads ALREADY INCLUDES them and the structure pass can
    only ever spend the remainder of the one budget. No enemy decision is revisited
    and no enemy is ever un-committed, because there is nothing left to revisit.

FILE AND MEMORY DISCIPLINE (Req 5.5)
    The pass borrows the world session that already exists, so no second target
    `.PRS` is ever resident. Each route parses ONE source `.BEF` and drops it
    inside `port_structure_route`; the source `.PRS`/`.PRT` are read transiently
    by the unmodified port path, exactly as they are for an enemy route.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import Optional

from . import catalog
from . import cross_world
from . import items
from . import roster
from . import roster_costs
from . import roster_selection as sel

# --- THE SWITCH ------------------------------------------------------------
#
# A MAINTAINER KILL SWITCH, no longer the user-facing default (Task 6). It used
# to be False and to be the ONLY thing `cli` consulted, which is what kept the
# feature unreachable while Task 5 shipped. The USER'S switch is now what
# normally decides -- `--cross-world-structures` / the GUI checkbox, default OFF
# -- and `cli` requires BOTH, so setting this False still kills the feature
# outright no matter what the user asked for. It is a kill switch, not a tuning
# knob: nothing else in this module reads it.
#
# THE DEFAULT IS STILL OFF, at the switch that faces the user: with the flag
# absent no structure is drawn, no structure blob is read, no allowlist row is
# consulted and no log line is emitted, so the build is byte-identical to one of
# a tree without this feature (Req 7.3). WITH the flag and no explicit per-route
# selection, every SELECTABLE route is a candidate and the shared resource budget
# decides how many fit; the per-route table narrows that when the user uses it.
STRUCTURE_PORT_ENABLED = True

# --- THE RESERVED SLICE ----------------------------------------------------
#
# THE ONE PLACE the reserve is defined. `cli` reads it and lowers the ENEMY
# gate's ceiling by exactly this much when cross-world structures are enabled;
# the structure pass keeps being handed the FULL `roster_selection.pool_budget`
# and keeps reading the live `len(prs) + len(prt)`. So there is still ONE budget
# value — the enemy walk simply stops short of the end of it, and the remainder
# is what the structure pass finds when its turn comes. Nothing about the
# ORDERING changes: structures are still admitted after every enemy group for a
# world, and no committed enemy is ever revisited or displaced.
#
# WHY IT EXISTS. Measured build (seed 132570, all worlds, `--cross-world
# --cross-world-structures`): the enemy pass consumed 99.4% of the shared budget,
# leaving castle 8,126 B and grave 38,331 B, while the CHEAPEST structure route
# costs 38,576 B. Both worlds therefore committed ZERO structures. Raising the
# RESOURCE pool to the `high` preset was tried instead and that build HUNG ON MAP
# LOAD (see `elf_patch.RESOURCE_POOL_HIGH`), so the room has to come out of the
# budget that already exists.
#
# WHY 300,000 B, from the measured route costs rather than a round guess. The
# per-route resident costs measured in `.kiro/specs/cross-world-structures/
# measurements.md` span 38,576 B (cheapest, `0xAA` Smashable_Door class) to
# 230,324 B (dearest, `0x17`-class 3-mesh/3-anim routes). So:
#   * anything below 38,576 B buys NOTHING at all — it is the starvation again;
#   * 300,000 B clears the dearest single route (230,324 B) with headroom, so no
#     route is unreachable purely because it is expensive; and
#   * it buys roughly 3-7 routes per world depending on the draw order that seed
#     produced (300,000 / 38,576 ~= 7 cheap ones; 300,000 / 230,324 = 1 dear one
#     plus a cheap one, with the mid-range costs landing in between).
# The cost is roughly one foreign ENEMY arrival per world, which is the accepted
# trade. The existing FOREIGN SHORTFALL report still fires on the real count, so
# when the trade does bite, the run says so.
STRUCTURE_RESERVE_BYTES = 300_000


def enemy_budget_ceiling(budget: int, *, structures_enabled: bool) -> int:
    """The ceiling the ENEMY admission gate is handed.

    `budget` is the ONE `roster_selection.pool_budget` value. With cross-world
    structures OFF the enemy ceiling is that value BYTE FOR BYTE, so a build
    without the feature is unchanged. With them ON it is lowered by
    `STRUCTURE_RESERVE_BYTES` so the structure pass has room to spend out of the
    same budget. Clamped at 0 so a pathologically small budget cannot go
    negative.
    """
    budget = int(budget)
    if not structures_enabled:
        return budget
    return max(0, budget - STRUCTURE_RESERVE_BYTES)


#: RETIRED (`unified-randomization` task 3), for the same reason
#: `roster_selection.SELECTION_SALT` is: the separate structure draw is no longer
#: a separate draw. `unified_draw.unified_admit` covers both arrival kinds from
#: one salt (`unified_draw.UNIFIED_SALT`) with a `phase` component. KEPT PRESENT
#: because tests and prose name it, and because `run_structure_pass` below is
#: still what the BUILD uses until task 4 wires the unified pass in. Nothing NEW
#: should draw from it.
#:
#: Distinct from every other salt in the project (`0x5A170572` enemy selection,
#: `0x5A5A5A5A` player spawn, `0x3C7B91A5` exits, `0xD150D250` drops,
#: `0x57A47125` starting loadout), so the structure order is drawn from its own
#: stream and the enemy draw is provably unperturbed: the structure pass never
#: draws from the `Random` the enemy plan used -- it builds its own.
STRUCTURE_SELECTION_SALT = 0x57A0C701

#: The prefix every asset message carries (Req 8.2). `ELF patch:` stays reserved
#: for executable patches and must never appear on a BEF/PRS/PRT message.
LOG_PREFIX = cross_world.LOG_PREFIX

#: The label the shared `AdmissionGate` stamps into its verdict lines so a reader
#: can tell a structure verdict from an enemy one.
GATE_LABEL = "STRUCT "


# --- data models -----------------------------------------------------------

@dataclass(frozen=True)
class StructureRoute:
    """One (source world, structure type, target world) triple, priced.

    The design names this type; it carries the same four fields plus the
    measured prediction, and converts to the `roster_selection.Candidate` the
    shared admission gate walks. Keeping one shape for both means the gate needs
    no structure-specific branch.
    """

    type_id: int
    name: str
    source_world: str
    target_world: str
    predicted_bytes: int = 0
    companions: tuple = ()

    @property
    def key(self) -> tuple:
        return (self.type_id, self.source_world, self.target_world)

    def as_candidate(self) -> sel.Candidate:
        return sel.Candidate(
            type_id=self.type_id, name=self.name,
            source_world=self.source_world, target_world=self.target_world,
            predicted_bytes=self.predicted_bytes,
            companions=tuple(self.companions))


@dataclass(frozen=True)
class StructurePlan:
    """The seed-fixed, PURE structure plan: no I/O, no porting, no game files."""

    seed: Optional[int]
    target_world: str
    budget: int
    order: tuple = ()            # tuple[StructureRoute], THE walk order
    excluded: tuple = ()         # tuple[roster_selection.Exclusion]

    @property
    def order_ids(self) -> tuple:
        return tuple(r.type_id for r in self.order)


@dataclass(frozen=True)
class StructureDrawResult:
    """Every route's verdict, plus what the pass spent (Req 5.3, 8.1)."""

    target_world: str
    budget: int = 0
    baseline_bytes: int = 0      # live resident BEFORE the structure pass
    resident_bytes: int = 0      # live resident AFTER it
    committed: tuple = ()        # tuple[StructureRoute] -- ported AND permitted
    skipped_budget: tuple = ()   # tuple[(StructureRoute, reason)] -- would not fit
    refused_gate: tuple = ()     # tuple[(StructureRoute, reason)] -- a gate said no
    excluded_rule: tuple = ()    # tuple[Exclusion] -- rule, allowlist or no cost row
    log: tuple = ()

    @property
    def spent(self) -> int:
        """Bytes the STRUCTURE pass added. Measured, never summed from rows."""
        return int(self.resident_bytes) - int(self.baseline_bytes)

    @property
    def remaining(self) -> int:
        return int(self.budget) - int(self.resident_bytes)

    @property
    def committed_types(self) -> frozenset:
        return frozenset(r.type_id for r in self.committed)

    def budget_line(self) -> str:
        return (f"{LOG_PREFIX}   structures [{self.target_world}]: "
                f"{len(self.committed)} committed, "
                f"{len(self.skipped_budget)} skipped (budget), "
                f"{len(self.refused_gate)} refused (gate), "
                f"{len(self.excluded_rule)} excluded (rule); "
                f"structure budget SPENT {self.spent:,} B, "
                f"REMAINING {self.remaining:,} B of the shared "
                f"{self.budget:,} B (enemies had already taken "
                f"{self.baseline_bytes:,} B and were never revisited)")


# --- selection -------------------------------------------------------------

def structure_selection_rng(seed, target_world: str) -> random.Random:
    """The per-target-world structure sub-seed, on its OWN salt.

    A fresh `Random` on a distinct salt, so drawing a structure order cannot
    advance any stream the enemy draw or the per-record retyping reads.
    """
    return random.Random((int(seed or 0) ^ STRUCTURE_SELECTION_SALT
                          ^ sel._stable_hash(target_world)) & ((1 << 128) - 1))


def _never_touch_reason(type_id: int) -> Optional[str]:
    """Requirement 3.2, checked against the table that actually enforces it.

    `HAZARD_NEVER_TOUCH` is a NAMED CONCEPT in `items` rather than a symbol: the
    authored hazards it describes (`0x40` Flame_Jet_Dragon_Head, `0xCC`
    Firing_Cannon_Hazard, `0xD1` Swinging_Spike) are enforced by their ABSENCE
    from `items.UNIVERSAL_ITEM_POOL`, and `items.py` says so at both sites. So
    the honest check is membership of that pool: a type the item pass will not
    touch as a source or a destination must not become a structure route either.
    This is belt-and-braces -- `spawn_config.STRUCTURE_TYPES` is a subset of
    `UNIVERSAL_ITEM_POOL` today, and a test pins that -- but a rule that is only
    true by coincidence is a rule waiting to break.
    """
    if int(type_id) not in set(items.UNIVERSAL_ITEM_POOL):
        return (f"0x{int(type_id):02X} is not in items.UNIVERSAL_ITEM_POOL, "
                f"which is how the HAZARD_NEVER_TOUCH rule is enforced: an "
                f"authored hazard is never a source and never a destination "
                f"(Req 3.2)")
    return None


def plan_structure_draw(seed, target_world: str, *, budget: int = 0,
                        allowlist=None, costs=None,
                        enabled_routes=None) -> StructurePlan:
    """The seed-fixed ORDERED structure route list. PURE: no I/O, no porting.

    Every declared structure type either becomes a `StructureRoute` or an
    `Exclusion` carrying its reason, so nothing falls out unaccounted for
    (Req 8.1). The rules, in the order they are applied:

      1. RULE EXCLUSION (refusal id S9). `roster.structure_exclusion_reason`
         owns this: `0x5F` Monster_Generator's measured GLOBAL-assets exclusion,
         the four Ordered_Puzzle_Props and the three progression grave
         structures. Checked FIRST so a protected type can never be admitted by
         a later rule. It no longer covers `0x17` / `0xAD` / `0x50` / `0xBC`:
         their hold was POLICY on measured-clean routes and the user lifted it
         (see `roster.STRUCTURE_HOLD_LIFTED`), so they now reach rules 3-5 like
         any other source.
      2. HAZARD_NEVER_TOUCH (S9), see `_never_touch_reason`.
      3. NO ALLOWLIST ROW into this target (S7). A missing row is stronger than
         `active=False` and excludes for every seed.
      4. NO MEASURED COST ROW (S7), quoting `structure_absence_reason` when the
         absence was recorded -- today that is exactly the two
         measured-and-refused `0x6C` routes, which stay refused because a
         refusal is evidence, not policy.
      5. NOT WANTED (S9). `enabled_routes` is the candidate set
         `spawn_config.structure_routes_for_world(config, target_world)` returns,
         and it can only ever REMOVE keys -- a route absent from it is excluded
         even though it is allowlisted and priced. `None` means "no narrowing"
         and is for tests and for measurement runs; `cli` always passes a real
         set. With no explicit user selection that set is every SELECTABLE route
         for the world, so the switch alone is enough to port structures; with a
         saved selection it is exactly that subset, empty included.

    `allowlist` and `costs` are injectable so tests can drive the walk without
    touching the shipped tables; both default to the shipped ones.
    """
    if allowlist is None:
        allowlist = catalog.load_structure_allowlist()
    cost_for = costs if callable(costs) else roster_costs.structure_cost_for
    rng = structure_selection_rng(seed, target_world)

    names = roster.structure_candidates()
    routes: list = []
    excluded: list = []
    for type_id, name in sorted(names.items()):
        why = roster.structure_exclusion_reason(type_id)
        if why is None:
            why = _never_touch_reason(type_id)
        if why is not None:
            excluded.append(sel.Exclusion(type_id, name, f"S9 {why}"))
            continue

        sources = sorted({key[1] for key in allowlist
                          if key[0] == type_id and key[2] == target_world})
        if not sources:
            recorded = roster_costs.structure_absence_reason(
                type_id, "", target_world)
            excluded.append(sel.Exclusion(
                type_id, name,
                f"S7 no structure allowlist row into {target_world}: either "
                f"never measured, measured-and-refused, or withheld -- and "
                f"either way excluded for EVERY seed"
                + (f" ({recorded})" if recorded else "")))
            continue

        priced = [w for w in sources if cost_for(type_id, w, target_world)]
        if not priced:
            recorded = " / ".join(
                roster_costs.structure_absence_reason(type_id, w, target_world)
                or "no measured cost row" for w in sources)
            excluded.append(sel.Exclusion(
                type_id, name,
                f"S7 no measured cost row for {sources} -> {target_world}: "
                f"{recorded}"))
            continue

        source = priced[0] if len(priced) == 1 else rng.choice(priced)
        if enabled_routes is not None and (type_id, source, target_world) \
                not in enabled_routes:
            excluded.append(sel.Exclusion(
                type_id, name,
                f"S9 {source} -> {target_world} is not enabled in the spawn "
                f"config, and a structure arrival defaults to DISABLED"))
            continue
        row = cost_for(type_id, source, target_world)
        routes.append(StructureRoute(
            type_id=type_id, name=name, source_world=source,
            target_world=target_world, predicted_bytes=row.resident_bytes,
            companions=tuple(row.companions)))

    # A PLAIN seeded shuffle, for the same reason the enemy order is one: cost
    # ordering in either direction is a defect (cheap-first starves the expensive
    # routes, expensive-first ships two of them). Fitting the budget is
    # admission's job.
    rng.shuffle(routes)
    # `budget` is carried, never computed here: it is the caller's ONE shared
    # `roster_selection.pool_budget` value, and this function computing a second
    # one would be the second budget concept the design forbids.
    return StructurePlan(seed=seed, target_world=target_world,
                         budget=int(budget), order=tuple(routes),
                         excluded=tuple(excluded))


# --- the pass --------------------------------------------------------------

def run_structure_pass(session, *, seed, budget: int, game_files_dir,
                       log=None, allowlist=None, costs=None,
                       enabled_routes=None,
                       port_fn=None) -> StructureDrawResult:
    """Draw, admit and port structures into an ALREADY-PORTED world session.

    Called by `cross_world.run_cross_world_placements` through its `structure_fn`
    hook, once per world, AFTER every enemy group for that world and BEFORE
    `session.finish()`. `budget` is the SAME `roster_selection.pool_budget` value
    the enemy gate used; the enemy spend is already inside the live size this
    reads, which is the whole of Requirement 5.2.

    `port_fn(route)` is injectable for tests and defaults to the real, gated
    `cross_world.port_structure_route`. It returns `(ok, reason)`.
    """
    target_world = session.target_world
    emit = log if log is not None else cross_world._default_log

    def live() -> int:
        return len(session.prs) + len(session.prt)

    baseline = live()
    plan = plan_structure_draw(seed, target_world, budget=int(budget),
                               allowlist=allowlist, costs=costs,
                               enabled_routes=enabled_routes)

    # The shared gate, with the three enemy-lineup rules that have no structure
    # meaning turned off and nothing else changed. See AdmissionGate's docstring.
    gate_plan = sel.DrawPlan(
        seed=seed, target_world=target_world, budget=int(budget),
        raised_pool=True, order=tuple(r.as_candidate() for r in plan.order),
        native_order=(), exclusions=plan.excluded, lineup_target=0)
    gate = sel.AdmissionGate(gate_plan, budget=int(budget), max_routes=None,
                             draw_natives=False, label=GATE_LABEL)

    if port_fn is None:
        def port_fn(route):
            return cross_world.port_structure_route(
                session, type_id=route.type_id, name=route.name,
                source_world=route.source_world, companions=route.companions,
                game_files_dir=game_files_dir, emit=emit)

    committed: list = []
    skipped: list = []
    refused: list = []
    for route in plan.order:
        cand = route.as_candidate()
        ok, reason = gate.consider(cand, live)
        if not ok:
            # BUDGET, with the PREDICTED cost stated and NOTHING ported: a route
            # that does not fit is never partially ported (Req 5.4). The walk
            # CONTINUES, so a cheaper later structure can still use the headroom.
            skipped.append((route, reason))
            continue
        ported, why = port_fn(route)
        if ported:
            gate.record_commit(cand)
            committed.append(route)
        else:
            # A gate said no. `port_group` restored the session before recording
            # the refusal, so this route mutated nothing (Req 1.5) and the next
            # route walks against exactly the size it would have had.
            gate.record_refusal(cand, why)
            refused.append((route, why or "refused by the gated port path"))

    result_gate = gate.finish(live)

    # THE REPORT IS RETURNED, NOT PRINTED, exactly as the enemy gate's is: the
    # lines already carry the `[cross-world]` prefix (Req 8.2), and the caller
    # owns indentation. Emitting them through `emit` here would prefix them
    # twice, because `emit` is the SESSION log, which adds the prefix itself --
    # that log is for the porter's own per-step messages.
    lines = list(result_gate.log)
    # EXCLUDE, not SKIP. The gate's own word for "did not fit" is SKIP, so a rule
    # exclusion must not borrow it: Requirement 8.1 wants the four verdicts --
    # committed, skipped (budget), refused (gate), excluded (rule) -- to stay
    # distinguishable in the log. `Exclusion.log_line()` says SKIP because that is
    # the enemy draw's wording, so the line is built here instead.
    lines.extend(f"{LOG_PREFIX}   {GATE_LABEL}EXCLUDE 0x{excl.type_id:02X} "
                 f"{excl.name:<20} {excl.reason}"
                 for excl in plan.excluded)
    # Req 8.4: every committed route is CANDIDATE and stays CANDIDATE until the
    # user observes it, so the run says out loud what is still owed. Nothing here
    # promotes anything; only an in-game observation can.
    lines.extend(
        f"{LOG_PREFIX}   {GATE_LABEL}CANDIDATE 0x{route.type_id:02X} "
        f"{route.name} {route.source_world}->{route.target_world}: OUTSTANDING "
        f"IN-GAME OBSERVATIONS -- does it render, does it break and animate, "
        f"does breaking it drop what it should, does it fit the geometry, and "
        f"does the level still complete"
        for route in committed)
    result = StructureDrawResult(
        target_world=target_world, budget=int(budget),
        baseline_bytes=baseline, resident_bytes=result_gate.resident_bytes,
        committed=tuple(committed), skipped_budget=tuple(skipped),
        refused_gate=tuple(refused), excluded_rule=tuple(plan.excluded),
        log=tuple(lines))
    # The budget line needs the finished result to state spent/remaining, so it
    # is appended once the result exists (Req 5.3).
    return replace(result, log=result.log + (result.budget_line(),))
