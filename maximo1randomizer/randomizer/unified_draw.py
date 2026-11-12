"""The ONE seeded admission pass (task 3 of `unified-randomization`).

WHAT THIS MODULE IS
    One salt, one pass, two phases, one live byte counter, one record per target
    world. It replaces the *decision* previously split across
    `roster_selection.plan_draw`/`admit` (enemy arrivals) and
    `structure_draw.plan_structure_draw`/`run_structure_pass` (structures).

WHAT IT DELIBERATELY IS NOT, AND WHAT IT DOES NOT TOUCH
    It adds no porter, no placer and no permission oracle. `WorldPortSession`,
    `verify_session`, `items.randomize_universal`, the asset trust gates and the
    `catalog` + `roster_costs` oracle are untouched. Nothing here promotes a
    route to CONFIRMED. The candidate set arrives already narrowed by the
    projections in `spawn_config`, which can only ever REMOVE routes from the
    set the oracle permits.

    IT IS WIRED IN AS OF TASK 4. `cross_world.run_unified_cross_world` is the
    one entry point that drives this pass over the REAL gated port path, and
    `cli` reports the record. `roster_selection.plan_draw` / `AdmissionGate` /
    `admit` and `structure_draw.run_structure_pass` are no longer reached by a
    build; they are left in place deliberately, so removing them stays a separate
    decision rather than a side effect of this task.

THE TWO PHASES, AND WHY THEY ARE NOT A FLAT DRAW
    Phase 1 is ENEMIES and its ceiling is
    `structure_draw.enemy_budget_ceiling(Shared_Budget, structures_enabled=...)`,
    i.e. `Shared_Budget - STRUCTURE_RESERVE_BYTES` when structures are on. Phase
    2 is STRUCTURES and its ceiling is the FULL `Shared_Budget`, so the 300,000 B
    reserve is spendable ONLY there. That is the whole of "enemies have
    priority" and the whole of "structures are not starved"; within a phase the
    order is purely seeded.

U3 AND U8 ARE THE PAIR MOST EASILY CONFUSED, SO THEY ARE KEPT APART HERE
    * U3 -- a route does not fit the applicable ceiling. That is the budget
      WORKING. It is a QUIET `skipped_budget` row carrying the predicted cost and
      the headroom that was left. No warning, no shortfall, no assertion, never
      fatal, and the walk CONTINUES so a cheaper later route can still use the
      headroom. Nothing is ported, so the target world is byte-identical to what
      it would have been had the route never been attempted.
    * U8 -- the committed total ACTUALLY EXCEEDS `Shared_Budget` once admission
      is over. That is the accounting BROKEN, it is impossible unless an
      invariant failed elsewhere, and it raises
      `roster_selection.PoolBudgetExceeded` (refuse to ship, never rebuild
      silently). It is a raised exception and a DIFFERENT type from data;
      `skipped_budget` rows can never carry it and it can never appear as a row.

    Mechanically: U3 only ever appends to `UnifiedResult.skipped_budget`, and U8
    only ever raises. There is no code path where one produces the other's
    output.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from typing import Callable, Optional

from . import roster_selection as sel
from . import spawn_config as sc
from . import structure_draw
from .items import ENEMY_DESTINATIONS

# --- THE ONE SALT ----------------------------------------------------------
#
# DISTINCT FROM EVERY OTHER SALT IN THE PROJECT, asserted by inspection against
# the complete list of them:
#
#   0x5A170572  roster_selection.SELECTION_SALT            (RETIRED, task 3)
#   0x57A0C701  structure_draw.STRUCTURE_SELECTION_SALT    (RETIRED, task 3)
#   0x5A5A5A5A  player spawn
#   0x3C7B91A5  exits
#   0xD150D250  drops
#   0x57A47125  starting loadout
#   0x11F1ED01  THIS ONE
#
# `_SALTS_IN_USE` below carries the same six values as data and the module
# asserts the distinctness at import, so a future salt cannot collide silently:
# a comment alone would not have caught it.
UNIFIED_SALT = 0x11F1ED01

#: Every other salt in the project, as DATA, so the distinctness claim in
#: `UNIFIED_SALT`'s comment is checked rather than merely asserted in prose.
_SALTS_IN_USE = (
    0x5A170572,   # roster_selection.SELECTION_SALT, retired
    0x57A0C701,   # structure_draw.STRUCTURE_SELECTION_SALT, retired
    0x5A5A5A5A,   # player spawn
    0x3C7B91A5,   # exits
    0xD150D250,   # drops
    0x57A47125,   # starting loadout
)
assert UNIFIED_SALT not in _SALTS_IN_USE, (
    "UNIFIED_SALT must be distinct from every other salt in the project")

#: The three stream PHASES. `native` is not drawn here -- the retyping draw is
#: `items.randomize_universal`'s, and task 4 feeds it the native projection
#: AFTER this pass finishes -- but it is named here because it is part of the
#: derivation and because the `phase` component is exactly what makes the
#: retyping stream NOT a continuation of the admission stream (Property 5).
PHASE_NATIVE = "native"
PHASE_ENEMY = "enemy"
PHASE_STRUCTURE = "structure"
PHASES = (PHASE_NATIVE, PHASE_ENEMY, PHASE_STRUCTURE)

#: `limit_kind` values on an `excluded_rule` row. The three kinds of limit the
#: requirements refuse to blur, carried in the DATA so a log cannot blur them.
LIMIT_PROTECTION = "protection"     # U5
LIMIT_BUDGET = "budget"             # U3's kind, when a caller wants it named
LIMIT_UNMEASURED = "unmeasured"     # U2, no `roster_costs` row
LIMIT_DISALLOWED = "disallowed"     # U1, no `catalog` allowlist row
LIMIT_KINDS = (LIMIT_PROTECTION, LIMIT_BUDGET, LIMIT_UNMEASURED,
               LIMIT_DISALLOWED)

#: The table names an exclusion row quotes, so the report can say WHICH source
#: refused a route by name (Req 4.4).
TABLE_ALLOWLIST = "catalog allowlist"
TABLE_COSTS = "roster_costs measured rows"

LOG_PREFIX = structure_draw.LOG_PREFIX

# The reserve and the enemy ceiling keep their CURRENT names and their ONE
# definition in `structure_draw`; re-aliased here only so a reader of this
# module can see what the two ceilings are without chasing the import.
STRUCTURE_RESERVE_BYTES = structure_draw.STRUCTURE_RESERVE_BYTES
enemy_budget_ceiling = structure_draw.enemy_budget_ceiling


def shared_budget(*, raised: bool = True, pool_size: Optional[int] = None,
                  max_pct=None) -> int:
    """`Shared_Budget`: DERIVED from the pool constant in effect, never a literal.

    It is `roster_selection.pool_budget` and nothing else, so changing the pool
    (or the occupancy ceiling) moves this with NO second edit -- there is exactly
    one budget concept in the project and this is a name for it, not a copy.

    At today's defaults (`elf_patch.RESOURCE_POOL_RAISED` = 0x700000 at
    `DEFAULT_MAX_PCT` = 90.0) that is int(0x700000 * 0.90) = 6,606,028 B.
    """
    return sel.pool_budget(raised=raised, pool_size=pool_size,
                           max_pct=max_pct)


def unified_stream(seed, target_world: str, phase: str) -> random.Random:
    """The per-world, per-phase stream: f(UNIFIED_SALT, seed, world, phase).

    `roster_selection._stable_hash` (blake2b) is used for BOTH the world and the
    phase, never Python's `hash()`, whose string hashing is randomised per
    process by PYTHONHASHSEED -- using it would mean the same seed drew a
    different result on every run, and no test inside one process could catch it.

    The `phase` component is why the retyping stream is not a continuation of
    the admission stream: retyping depends on admission's RESULT and never on how
    many draws admission consumed (Property 5).
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")
    mixed = (int(seed or 0) ^ UNIFIED_SALT
             ^ sel._stable_hash(target_world)
             ^ sel._stable_hash(f"phase:{phase}"))
    return random.Random(mixed & ((1 << 128) - 1))


# --- the record ------------------------------------------------------------

@dataclass(frozen=True)
class UnifiedResult:
    """ONE record per target world. Replaces three result objects.

    `skipped_budget` rows are `(candidate, predicted_cost, headroom)` -- U3, and
    quiet. `refused_gate` rows are `(candidate, reason)`. `excluded_rule` rows
    are `(candidate_or_route, limit_kind, table_name)`.
    """
    world: str
    committed_enemies: tuple = ()
    committed_structures: tuple = ()
    skipped_budget: tuple = ()
    refused_gate: tuple = ()
    excluded_rule: tuple = ()
    bytes_committed: int = 0
    bytes_remaining: int = 0
    realised_enemy_share: float = 0.0
    shortfalls: tuple = ()
    #: Carried so a caller never has to re-derive what the two ceilings were.
    budget: int = 0
    enemy_ceiling: int = 0
    baseline_bytes: int = 0
    log: tuple = ()

    @property
    def committed(self) -> tuple:
        return tuple(self.committed_enemies) + tuple(self.committed_structures)

    @property
    def committed_routes(self) -> tuple:
        """`(type_id, source_world, target_world)` for every committed route --
        the shape `SpawnConfig.native_weights(world, committed_routes=...)` takes."""
        return tuple((c.type_id, c.source_world, c.target_world)
                     for c in self.committed)

    def budget_line(self) -> str:
        return (f"{LOG_PREFIX}   unified [{self.world}]: "
                f"{len(self.committed_enemies)} enemy + "
                f"{len(self.committed_structures)} structure committed, "
                f"{len(self.skipped_budget)} skipped (budget), "
                f"{len(self.refused_gate)} refused (gate), "
                f"{len(self.excluded_rule)} excluded (rule); "
                f"SPENT {self.bytes_committed:,} B, REMAINING "
                f"{self.bytes_remaining:,} B of {self.budget:,} B "
                f"(enemy phase ceiling {self.enemy_ceiling:,} B, structure "
                f"reserve {STRUCTURE_RESERVE_BYTES:,} B); realised enemy share "
                f"{self.realised_enemy_share:.3f}")


# --- ordering --------------------------------------------------------------

def _weighted_order(rng: random.Random, rows) -> list:
    """A seeded WEIGHT-RESPECTING order over `rows` = [(candidate, weight)].

    `rows` MUST arrive sorted on the Candidate id; every caller below sorts, so
    no result depends on interpreter hashing (Property 6).

    The order is drawn without replacement, higher weight earlier ON AVERAGE,
    using one exponential key per row (`-ln(u)/w`). That is deliberately NOT a
    cost sort in either direction -- cheap-first starves expensive routes and
    expensive-first ships two of them; fitting the budget is admission's job.
    One `rng.random()` per row means the number of draws depends only on the
    number of rows, not on any admission outcome.
    """
    keyed = []
    for idx, (cand, weight) in enumerate(rows):
        u = rng.random()
        w = float(weight)
        if w <= 0:
            # Should not happen -- the projections drop weight-0 rows (U6) --
            # but a zero here must sort LAST rather than divide by zero.
            key = math.inf
        else:
            key = -math.log(u if u > 0.0 else 5e-324) / w
        keyed.append((key, idx, cand))
    keyed.sort(key=lambda row: (row[0], row[1]))
    return [cand for _key, _idx, cand in keyed]


def _projection(weights, world: str, phase: str) -> dict:
    """The phase's `{route: weight}` map, from a `SpawnConfig` or a plain dict.

    A `SpawnConfig` is asked for its PROJECTION, which is the normal path. A
    plain mapping is accepted so a test can drive the walk without a config; it
    may be `{"enemy": {...}, "structure": {...}}` or already be the one phase's
    map. Either way this only ever NARROWS: the candidate table below is the
    oracle, and a route absent from it is excluded, never admitted.
    """
    if weights is None:
        return {}
    getter = getattr(weights, f"{phase}_route_weights", None)
    if callable(getter):
        return dict(getter(world))
    if isinstance(weights, dict):
        if phase in weights and isinstance(weights[phase], dict):
            return dict(weights[phase])
        if any(isinstance(k, tuple) for k in weights):
            return dict(weights)
    return {}


def _candidate_table(world: str, candidates=None) -> dict:
    """`{route: Candidate}` for `world` -- THE ORACLE, and it is never widened.

    Defaults to `spawn_config.enemy_route_candidates` +
    `structure_route_candidates`, which are built from the catalog allowlist and
    the measured cost rows, so a route with no allowlist row (U1) or no measured
    cost row (U2) simply is not in here and is reported as excluded. `candidates`
    is injectable for tests only; it cannot add permission the oracle withheld
    because it IS the stand-in for the oracle in a test that has no tables.
    """
    if candidates is None:
        candidates = (sc.enemy_route_candidates(world)
                      + sc.structure_route_candidates(world))
    table = {}
    for cand in candidates:
        table[(int(cand.type_id), cand.source_world, cand.target_world)] = cand
    return {k: table[k] for k in sorted(table)}


# --- THE ORACLE, NAMED EXACTLY (task 4, U1 vs U2, Req 4.4) -----------------
#
# Task 3 could only be CONSERVATIVE here: a route absent from the candidate
# table was reported as `disallowed` against BOTH table names, because absence
# alone cannot say which of the two rows was the missing one. Task 4 makes the
# exact source knowable by asking the oracle's OWN option rows, which carry the
# distinction directly:
#
#   * NO allowlist entry for the route at all            -> U1, catalog allowlist
#   * an allowlist entry that is INACTIVE or HELD BACK    -> U1, catalog allowlist
#     (the allowlist is what withheld it -- a cost row would change nothing)
#   * an allowlist entry whose `resident_bytes` is None   -> U2, roster_costs
#     (`arrival_options` sets that field from `roster_costs.cost_for`, so None IS
#      "no measured cost row", and it is the ONLY way that field is None)
#
# This NARROWS and can never widen: the function's only outputs are refusals.
_ORACLE_OPTIONS: dict = {}


def _oracle_options(world: str, kind: str) -> dict:
    """`{route: option}` from the oracle's own tables, memoised (they are static)."""
    ck = (world, kind)
    if ck not in _ORACLE_OPTIONS:
        try:
            opts = (sc.arrival_options(world)
                    if kind == sc.UNIFIED_KIND_ENEMY
                    else sc.structure_arrival_options(world))
        except Exception:      # noqa: BLE001 - a missing table is not a crash
            opts = []
        _ORACLE_OPTIONS[ck] = {o.key: o for o in opts}
    return _ORACLE_OPTIONS[ck]


def oracle_refusal(world: str, route, kind: str) -> tuple:
    """`(limit_kind, table_name)` naming WHICH source refused `route`.

    U1 `disallowed` / `catalog allowlist`, or U2 `unmeasured` /
    `roster_costs measured rows`. Never returns "permitted": it is only ever
    called for a route the oracle already declined.
    """
    opt = _oracle_options(world, kind).get(route)
    if opt is not None and getattr(opt, "resident_bytes", None) is None:
        return (LIMIT_UNMEASURED, TABLE_COSTS)          # U2
    return (LIMIT_DISALLOWED, TABLE_ALLOWLIST)          # U1


def oracle_admits(world: str, cand) -> tuple:
    """THE ORACLE CHECK IN THE ADMISSION PATH (Property 1, Req 4.2).

    `(True, None, None)` only when the route has BOTH a `catalog` allowlist row
    and a `roster_costs` measured cost row, read from the oracle's own tables at
    the moment of admission -- not from config validation, so a hand-edited
    config cannot bypass it. Otherwise `(False, limit_kind, table_name)`.

    When the oracle tables are unavailable in this process (no game data, a
    test's injected candidate set) the option map is EMPTY and this defers to the
    candidate table, which is the only oracle such a caller has. It cannot widen
    anything: `_candidate_table` is still what produced the candidate.
    """
    route = (int(cand.type_id), cand.source_world, cand.target_world)
    options = _oracle_options(world, cand.kind)
    if not options:
        return (True, None, None)
    opt = options.get(route)
    if opt is None:
        return (False, LIMIT_DISALLOWED, TABLE_ALLOWLIST)          # U1
    if getattr(opt, "resident_bytes", None) is None:
        return (False, LIMIT_UNMEASURED, TABLE_COSTS)              # U2
    if not getattr(opt, "selectable", False):
        return (False, LIMIT_DISALLOWED, TABLE_ALLOWLIST)          # U1
    return (True, None, None)


def _rows_for_phase(world, weights, phase, table):
    """`([(candidate, weight)], [excluded rows])`, both sorted on Candidate id.

    A route the projection wants but the oracle does not offer becomes an
    `excluded_rule` row naming the source that refused it, rather than vanishing.
    """
    kind = (sc.UNIFIED_KIND_ENEMY if phase == PHASE_ENEMY
            else sc.UNIFIED_KIND_STRUCTURE)
    projection = _projection(weights, world, phase)
    rows, excluded = [], []
    for route in sorted(projection):
        weight = projection[route]
        cand = table.get(route)
        if cand is None or cand.kind != kind:
            # U1 / U2, NAMED EXACTLY (task 4). `oracle_refusal` asks the oracle's
            # own option rows, which distinguish "no allowlist row / withheld by
            # the allowlist" from "no measured cost row", so the report names one
            # table rather than hedging across both.
            limit, tbl = oracle_refusal(world, route, kind)
            excluded.append((route, limit, tbl))
            continue
        if not int(cand.cost_bytes) > 0:
            # No MEASURED cost is U2, and a guessed cost is how a build silently
            # overflows the pool. Excluded, never estimated.
            excluded.append((cand, LIMIT_UNMEASURED, TABLE_COSTS))
            continue
        rows.append((cand, float(weight)))
    rows.sort(key=lambda r: r[0].key)
    excluded.sort(key=lambda r: getattr(r[0], "key", str(r[0])))
    return rows, excluded


# --- the pass --------------------------------------------------------------

def unified_admit(world: str, weights, seed, *, structures_enabled: bool,
                  budget: Optional[int] = None,
                  live_fn: Optional[Callable[[], int]] = None,
                  session=None, port_fn: Optional[Callable] = None,
                  candidates=None) -> UnifiedResult:
    """ONE seeded pass. Enemies first, structures guaranteed a slice.

    Phase 1 (ENEMIES, priority): walk the seeded enemy order; the ceiling is
      `enemy_budget_ceiling(Shared_Budget, structures_enabled=...)`, which is
      `Shared_Budget - STRUCTURE_RESERVE_BYTES` when structures are on.
    Phase 2 (STRUCTURES): walk the seeded structure order; the ceiling is the
      FULL `Shared_Budget`, so the reserve is spendable only here. Skipped
      entirely when `structures_enabled` is False.

    Both phases admit while the LIVE committed session size plus the route's
    MEASURED cost fits the applicable ceiling. A non-fit does NOT stop the walk,
    so a cheaper later route can still use the headroom. WHOLE ROUTES ONLY: a
    refused route ports nothing, so the target world is byte-identical to what it
    would have been had the route never been attempted.

    NO COUNT CAP. `LINEUP_MAX` is not consulted here and cannot be: retiring it
    cannot overrun the pool because the check is on LIVE bytes, not on a count.
    `LINEUP_MIN` and `MIN_FOREIGN` are read ONLY after both phases, to emit
    `shortfalls` rows -- no code path in this function consults them to decide an
    admission (Property 11).

    The extra keywords are plumbing, not policy:
      `budget`    -- override `Shared_Budget` (tests, and a caller that already
                     resolved the pool); defaults to `shared_budget()`.
      `live_fn`   -- the LIVE `len(session.prs) + len(session.prt)` read. Given a
                     `session` and no `live_fn`, that read is built from it.
                     With neither, the counter is a pure model that starts at 0
                     and advances by each committed route's measured cost.
      `port_fn`   -- performs the port, returning truthy or `(ok, reason)`. With
                     none, this is a dry pass and commits nothing but bytes.
      `candidates` -- stand-in for the oracle tables, for tests.
    """
    budget = int(shared_budget() if budget is None else budget)
    ceiling_enemy = enemy_budget_ceiling(
        budget, structures_enabled=bool(structures_enabled))

    if live_fn is None and session is not None:
        def live_fn():
            return len(session.prs) + len(session.prt)

    table = _candidate_table(world, candidates)
    enemy_rows, excluded = _rows_for_phase(world, weights, PHASE_ENEMY, table)
    structure_rows, struct_excluded = _rows_for_phase(
        world, weights, PHASE_STRUCTURE, table)
    excluded = list(excluded) + list(struct_excluded)

    baseline = int(live_fn()) if live_fn is not None else 0
    modelled = 0            # bytes this pass committed, for the no-session case

    def live() -> int:
        if live_fn is not None:
            return int(live_fn())
        return baseline + modelled

    committed_enemies: list = []
    committed_structures: list = []
    skipped: list = []
    refused: list = []
    log: list = []

    def walk(rows, ceiling, phase, sink):
        nonlocal modelled
        rng = unified_stream(seed, world, phase)
        for cand in _weighted_order(rng, rows):
            # THE ORACLE, RE-ASKED IN THE ADMISSION PATH (Property 1, Req 4.2).
            # The candidate table was already built from the oracle, so this is
            # normally redundant -- deliberately. It is the check that makes the
            # guarantee hold for a HAND-EDITED config too, because it lives here
            # and not in config validation, and it names the exact source (U1
            # `disallowed` / U2 `unmeasured`) rather than the pair.
            ok_oracle, limit, tbl = oracle_admits(world, cand)
            if not ok_oracle:
                excluded.append((cand, limit, tbl))
                log.append(f"{LOG_PREFIX}   {phase.upper()} EXCLUDE {cand.key} "
                           f"{limit}: refused by {tbl}")
                continue
            cost = int(cand.cost_bytes)
            now = live()
            headroom = ceiling - now
            if now + cost > ceiling:
                # U3. THE BUDGET WORKING: quiet, whole-route, walk continues.
                # No warning, no shortfall, no assertion, nothing ported.
                skipped.append((cand, cost, headroom))
                log.append(
                    f"{LOG_PREFIX}   {phase.upper()} SKIP    {cand.key} "
                    f"predicted {cost:,} B > headroom {headroom:,} B "
                    f"(walk continues -- a cheaper later route may still fit)")
                continue
            ok, why = True, None
            if port_fn is not None:
                outcome = port_fn(cand)
                if isinstance(outcome, tuple):
                    ok = bool(outcome[0])
                    why = outcome[1] if len(outcome) > 1 else None
                else:
                    ok = bool(outcome)
            if not ok:
                # U4. A gate said no; the porter restored the session first, so
                # this route mutated nothing and the walk simply advances.
                refused.append((cand, why or "refused by the gated port path"))
                log.append(f"{LOG_PREFIX}   {phase.upper()} REFUSE  "
                           f"{cand.key} {why or 'refused during porting'}")
                continue
            modelled += cost
            sink.append(cand)
            log.append(f"{LOG_PREFIX}   {phase.upper()} COMMIT  {cand.key} "
                       f"+{cost:,} B measured")

    walk(enemy_rows, ceiling_enemy, PHASE_ENEMY, committed_enemies)
    if structures_enabled:
        walk(structure_rows, budget, PHASE_STRUCTURE, committed_structures)

    resident = live()

    # U8, AND ONLY U8. This is the accounting BROKEN, not the budget working:
    # it fires only when the committed total ACTUALLY exceeds the budget, which
    # means the live bound was violated somewhere else. Refusing to ship beats
    # rebuilding silently, and it is an EXCEPTION rather than a row so it can
    # never be mistaken for a U3 skip.
    if resident > budget:
        raise sel.PoolBudgetExceeded(
            f"live resident total {resident:,} B exceeds Shared_Budget "
            f"{budget:,} B after unified admission into {world} of "
            f"{[c.key for c in committed_enemies + committed_structures]}. "
            f"This is U8, an ACCOUNTING BUG, and is NOT a budget refusal (U3, "
            f"which is a quiet skipped_budget row): the incremental live bound "
            f"was violated elsewhere. Refusing to ship rather than rebuilding.")

    shortfalls = _shortfalls(world, weights, committed_enemies, len(enemy_rows),
                             len(skipped), len(refused))
    share = _realised_enemy_share(
        world, weights,
        tuple((c.type_id, c.source_world, c.target_world)
              for c in committed_enemies + committed_structures))

    result = UnifiedResult(
        world=world,
        committed_enemies=tuple(committed_enemies),
        committed_structures=tuple(committed_structures),
        skipped_budget=tuple(skipped),
        refused_gate=tuple(refused),
        excluded_rule=tuple(excluded),
        bytes_committed=resident - baseline,
        bytes_remaining=budget - resident,
        realised_enemy_share=share,
        shortfalls=tuple(shortfalls),
        budget=budget, enemy_ceiling=ceiling_enemy, baseline_bytes=baseline,
        log=tuple(log))
    # The budget line needs the finished record to state spent / remaining, so
    # it is appended once the record exists.
    return replace(result, log=result.log + (result.budget_line(),))


# --- reporting floors, read ONLY after admission ---------------------------

def _shortfalls(world, weights, committed_enemies, considered, skipped_count,
                refused_count) -> list:
    """`LINEUP_MIN` / `MIN_FOREIGN` as REPORTING floors (Req 10.3, 10.4).

    Read here, AFTER both phases, and nowhere else. Nothing in `unified_admit`
    consults either figure to decide an admission, which is what makes them
    compatible with "if it has space put it, if not don't" -- and it is also why
    a shortfall line can never coincide with a budget refusal DECISION
    (Property 11): the floors have no vote.
    """
    out: list = []
    foreign = len(committed_enemies)
    if foreign < sel.MIN_FOREIGN:
        out.append(
            f"FOREIGN SHORTFALL: {foreign} foreign enemy route(s) committed, "
            f"reporting floor is {sel.MIN_FOREIGN} (Req 10.4). {considered} "
            f"candidate(s) were in the enemy order, {skipped_count} did not fit "
            f"the ceiling, {refused_count} refused during porting. A REPORT, "
            f"not a decision: nothing was admitted or refused because of this.")
    natives = tuple(tid for tid in sc.native_pool_types(world)
                    if tid in ENEMY_DESTINATIONS)
    lineup = len({c.type_id for c in committed_enemies} | set(natives))
    if lineup < sel.LINEUP_MIN:
        out.append(
            f"LINEUP SHORTFALL: lineup is {lineup} enemy type(s), below the "
            f"LINEUP_MIN reporting floor {sel.LINEUP_MIN} (Req 10.3) -- "
            f"{foreign} foreign + {len(natives)} native. A REPORT, not a "
            f"decision.")
    del weights
    return out


def _realised_enemy_share(world, weights, committed_routes) -> float:
    """The share the build ACTUALLY got, computed from the native projection.

    The configured `enemy_share_target` is what was asked for; this is what the
    pool the retyping draw will see actually carries, with the routes this pass
    committed already in it. Reported side by side so the gap is visible rather
    than assumed away. 0.0 when there is no config to project from.
    """
    getter = getattr(weights, "native_weights", None)
    if not callable(getter):
        return 0.0
    try:
        pool = getter(world, committed_routes)
    except TypeError:
        pool = getter(world)
    total = sum(float(w) for w in pool.values())
    if total <= 0:
        return 0.0
    enemy = sum(float(w) for tid, w in pool.items()
                if tid in ENEMY_DESTINATIONS)
    return enemy / total
