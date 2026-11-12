"""The seeded, budget-bounded selector (Tasks 5-7 of `randomized-enemy-roster`).

THREE THINGS LIVE HERE, IN ORDER OF DEPENDENCE

  1. THE BUDGET (Task 5). `pool_budget(pool_size=... / raised=...)` states the
     byte bound as an
     OCCUPANCY of the RESOURCE pool rather than as a byte literal, so raising
     the pool constant moves the budget with no second edit (Requirement 6.4)
     and NOT applying the ELF patch shrinks the budget to the vanilla pool
     instead of quietly assuming a patch the build never made (Requirement 6.5).

     The 93.0% figure is EMPIRICAL, not chosen, and it has a HISTORY. 90.4% was
     the previous loading ceiling, inferred from earlier builds; 95.1% was the
     previous crash floor. The 90.4%..95.1% band was then WALKED DELIBERATELY,
     one ISO build at a time, at the user's request: 95% CRASHED at level load,
     94% CRASHED at level load, 93.5% CRASHED at level load, 93% LOADED and
     played. So the bracket is now 93.0 loading / 93.5 crashing, and the
     unexplored gap is the sliver 93.0%..93.5% -- too small to be worth another
     console build, so THIS LEVER IS FINISHED and the RESOURCE pool SIZE is the
     remaining one. Pool exhaustion is invisible -- `MemoryPool::Allocate` falls
     through and returns `sentinel_block + 16`, so the caller corrupts allocator
     metadata and the game dies at level load with no log line, no build failure
     and no test failure; a green test run says nothing about it. That is the
     whole reason this module exists.

     THE CEILING IS PER-BUILD (`max_pct=`), defaulting to `DEFAULT_MAX_PCT`,
     which is 90.0 -- DELIBERATELY BELOW the observed-loading 93.0, at the
     user's request, for testing. Lowering is always safe: it can only ship a
     smaller lineup. Anything up to (but not including) 93.5 is still selectable
     per build via `--pool-occupancy`. 93.5% and above is REFUSED, not clamped,
     because it is observed to crash -- the refusal floor moved DOWN from 95.1
     to 94.0 to 93.5, which makes the guard STRICTER, not looser, at every step.
     See `LOADED_MAX_PCT` (the measurement), `DEFAULT_MAX_PCT` (what a build
     draws to by default) and `resolve_max_pct`.

  2. THE ORDER (Task 6). `plan_draw` is PURE: no I/O, no porting, no game files.
     It produces an ORDERED candidate list, never a set. The ordering is what
     makes refusal backfill deterministic (Requirement 5.4): a refusal simply
     ADVANCES THE WALK, so the outcome depends only on the seed and the game
     files and never on which routes happened to refuse. There is no separate
     backfill algorithm to keep in step with this one.

     The order is a PLAIN SEEDED SHUFFLE and is deliberately NOT sorted by cost
     in either direction. Cheap-first systematically starves expensive enemies
     like `0x49` Bomb_Skeleton; expensive-first produces two-enemy lineups.
     Fitting the budget is admission's job, not ordering's (Requirement 4.4).

  3. ADMISSION (Task 7). `admit` walks that order and admits a group only while
     the LIVE session size plus the route's predicted cost still fits. THE
     STATIC COST TABLE PROPOSES; THE MEASURED SESSION SIZE DECIDES. This is the
     feature's core safety property: a route whose real cost exceeds its
     predicted cost still cannot overshoot the pool, because the NEXT admission
     reads the true size (Requirement 6.1). It also makes Requirement 3.4's
     warning about non-additive costs stop mattering -- no sum is ever trusted.

     Requirement 4.5's predict-verify-rebuild path is retained as an ASSERTION
     THAT SHOULD NEVER FIRE, and when it fires we REFUSE TO SHIP
     (`PoolBudgetExceeded`) rather than rebuild silently: if the live bound was
     broken, the invariant was broken somewhere else and shipping is the wrong
     response.

WHAT THIS MODULE IS NOT
    It does not port anything, does not read a game file, does not touch
    `classify_placement`, `WorldPortSession`, `verify_session` or the permission
    model. It PRODUCES an allowlist subset; `allowlist_lookup` stays the single
    permission oracle. `active=False` is excluded ABSOLUTELY, for every seed,
    because a route that crashed the console has to stay held back
    (Requirement 10.3).
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from . import elf_patch as ep
from . import roster as roster_mod
from . import roster_costs

# ---------------------------------------------------------------------------
# Task 5 -- the budget and the seeding primitives
# ---------------------------------------------------------------------------

#: The highest occupancy OBSERVED TO LOAD. A MEASUREMENT, and no longer the
#: default a build draws to -- see `DEFAULT_MAX_PCT`, which sits deliberately
#: below this figure. This constant records what the console actually did.
#: Measured, not picked, and arrived at by walking a band build by build:
#:
#:   * 90.4% was the PREVIOUS loading ceiling, inferred from earlier builds,
#:     which paired with a previous crash floor of 95.1%;
#:   * the user then asked to WALK the 90.4%..95.1% band one ISO build at a time,
#:     which is why the ceiling is a per-build option (`--pool-occupancy`,
#:     `patch_iso(pool_occupancy=...)`, and a GUI control) rather than a constant
#:     somebody edits;
#:   * 95 CRASHED at level load. 94 CRASHED at level load. 93.5 CRASHED at level
#:     load. 93 LOADED and played.
#:
#: So the bracket is now 93.0 loading / 93.5 crashing and THE UNEXPLORED BAND IS
#: THE SLIVER 93.0%..93.5%. Nothing here claims that gap is safe or unsafe -- it
#: is simply unmeasured, and it is now so small that another console build is not
#: worth its cost: THIS LEVER IS FINISHED. The remaining lever is the RESOURCE
#: POOL SIZE, not its occupancy. 93.0 is an OBSERVATION recorded as evidence, not
#: a guarantee, and it stays a measured default rather than a confirmed one.
#:
#: WHAT THIS CEILING BOUGHT WHEN IT WAS THE DEFAULT (it no longer is -- the
#: default is now `DEFAULT_MAX_PCT` = 90.0): at the `raised` pool the budget
#: moved from
#: 6,635,388 B (0.904 * 7,340,032) to 6,826,229 B (0.93 * 7,340,032, truncated),
#: i.e. +190,841 B -- roughly ONE extra arrival per world, since enemy routes
#: cost 225 KB to 840 KB. LOWERING the ceiling is always safe, because it can
#: only ship a SMALLER lineup and can never overflow the pool.
#:
#: THE POOL default moved AWAY AND BACK since then, and this percentage did NOT
#: move for either step -- the two levers are independent and this one is
#: finished. `elf_patch.RESOURCE_POOL_LOADED_MAX` (0x800000) briefly replaced
#: `RESOURCE_POOL_RAISED` (0x700000) as what a raised build patches, on the
#: strength of one build that loaded; a later 0x800000 build HUNG with infinite
#: loading, so 0x800000 is UNRELIABLE and the default is 0x700000 again. At THIS
#: percentage the budget is therefore 0.93 * 7,340,032 = 6,826,229 B, not the
#: 7,801,405 B that 0x800000 would have given.
#:
#: HOW TO READ THE RESULT OF AN EXPLORATION BUILD:
#:   * it LOADS and plays at a higher figure -> that is EVIDENCE, and it is worth
#:     RECORDING (the figure, the world, the resident total) because it moves the
#:     known-loading ceiling up off a measurement rather than a guess;
#:   * it HANGS on the loading screen or DIES at level load -> that is the
#:     EXPECTED failure of this band and it means DROPPING BACK to the last
#:     figure that loaded. There is no other signal: exhaustion is SILENT.
#:     `MemoryPool::Allocate` falls through and returns `sentinel_block + 16`, so
#:     the caller corrupts allocator metadata. No log line, no build failure, no
#:     test failure -- a green test run says nothing about this.
LOADED_MAX_PCT = 93.0

#: The lowest occupancy OBSERVED TO CRASH, and a HARD REFUSAL, in the same spirit
#: as `elf_patch.RESOURCE_POOL_MAX`: builds at 93.5, at 94 and at 95 all died at
#: level load, so a build is never allowed to ask for 93.5% or more. The floor
#: moved DOWN 95.1 -> 94.0 -> 93.5 on that evidence, which makes this guard
#: STRICTER at every step, never looser. Values between `LOADED_MAX_PCT` and here
#: (93.0..93.5) are the remaining unexplored sliver and are allowed; anything at
#: or below
#: `LOADED_MAX_PCT` is always allowed, because LOWERING the ceiling can only ship
#: a smaller lineup and can never overflow the pool.
#:
#: REFUSED, NOT CLAMPED: there is no engine-side check that would catch an
#: over-occupied pool, so a silent clamp would hide the one decision the user
#: needs to make. Same shape as `elf_patch.RESOURCE_POOL_MAX`.
CRASHED_MIN_PCT = 93.5

#: The occupancy a build draws to when the caller chooses NOTHING. This is a
#: DEFAULT, not a measurement: `LOADED_MAX_PCT` (93.0) is the measurement, and
#: this figure sits DELIBERATELY BELOW it, set at the user's request for testing.
#:
#: WHY THAT IS SAFE: lowering the ceiling can only ship a SMALLER lineup and can
#: never overflow the pool, so a default under the measured loading ceiling needs
#: no console evidence of its own. Nothing here is promoted to CONFIRMED, and the
#: measurement is untouched: 93.0 is still the highest occupancy OBSERVED TO LOAD
#: (95 CRASHED, 94 CRASHED, 93.5 CRASHED, 93 LOADED and played) and 93.5 is still
#: the refusal floor. Anything up to but NOT INCLUDING `CRASHED_MIN_PCT` -- which
#: includes 93.0 itself -- remains selectable PER BUILD via `--pool-occupancy`;
#: this constant only moves what a build gets when it asks for nothing.
#:
#: THE BUDGET ARITHMETIC, at the default `elf_patch.RESOURCE_POOL_RAISED` pool
#: (0x700000 = 7,340,032 B):
#:   0.90 * 7,340,032 = 6,606,028 B   (this default)
#:   0.93 * 7,340,032 = 6,826,229 B   (at the measured `LOADED_MAX_PCT`)
#: i.e. **-220,201 B** -- roughly ONE FEWER arrival in a world where the BUDGET
#: rather than `LINEUP_MAX` is the thing that binds, since enemy routes cost
#: 225 KB to 840 KB. Where the cap binds instead, this changes nothing.
#:
#: THIS PERCENTAGE DID NOT MOVE when the pool default came back from 0x800000 to
#: 0x700000. The budget did: it was int(8,388,608 * 0.90) = 7,549,747 B and is now
#: int(7,340,032 * 0.90) = 6,606,028 B, i.e. **-943,719 B** -- roughly ONE TO TWO
#: FEWER arrivals per world. That cost is accepted deliberately, because 0x800000
#: loaded once and HUNG once and 0x700000 has never hung.
#:
#: NOTE 90.0 is NOT the historical 90.4: 90.4 was the PREVIOUS inferred loading
#: ceiling, a different number with a different meaning, and the two must not be
#: merged.
DEFAULT_MAX_PCT = 90.0

#: DEMOTED TO A REPORTING FLOOR (`unified-randomization` task 3, Req 10.4).
#: `unified_draw.unified_admit` reads this ONLY after both admission phases have
#: finished, to emit a `shortfalls` row; no code path there consults it to decide
#: an admission, which is what makes it compatible with "if it has space put it,
#: if not don't". The constant is KEPT because it is still the floor that gets
#: REPORTED, and because `AdmissionGate.finish` below -- still the BUILD's path
#: until task 4 -- reads it in exactly the same after-the-fact way it always did.
#:
#: Requirement 4.10, as it stood. Natives cost ZERO budget, so an unconstrained
#: draw could come up all-native and produce a world indistinguishable from
#: vanilla -- the exact opposite of the feature's purpose. Hence a floor on
#: FOREIGN routes.
MIN_FOREIGN = 3

#: Requirement 4.9. Total enemy TYPES in the lineup (foreign + native).
#:
#: RAISED 7 -> 10, FROM MEASUREMENTS AND NOT BY FEEL. The 7-route cap was
#: MEASURED BINDING: on the seed 132570 build grave filled ALL 7 foreign slots
#: and still had 38,331 B of budget UNSPENT, so grave was CAPPED, not starved --
#: the cap, not the pool, was the thing stopping it.
#:
#: THE ARITHMETIC AT THE DEFAULT POOL (`RESOURCE_POOL_RAISED`, 0x700000, the only
#: raised size that has loaded and played consistently; 0x800000 loaded once and
#: hung once, so it is not the default):
#:   budget                 6,826,229 B  (0.93 * 7,340,032, truncated)
#:   grave resident vanilla 3,380,358 B  (2,563,126 .PRS + 817,232 .PRT)
#:   room for arrivals     ~3,445,871 B  (~3.45 MB)
#:   enemy route cost         225,551 B (Axe_Guard) .. 841,214 B (Plant_Monster)
#: So a MEDIAN-cost lineup of 10 fits inside ~3.45 MB only when the routes are
#: cheap, an EXPENSIVE one does
#: not -- in which case the live budget guard in `admit` stops it partway, which
#: is the CORRECT behaviour: the cap is an upper bound on ambition and the budget
#: is the thing that actually decides.
#:
#: WHAT DOES NOT BIND, checked so it is not rediscovered as a surprise: the one
#: texture-side limit found in the executable is the 175-slot CLUT table
#: (`slti $v0,$v1,175` at vaddr 0x001A6178). Grave went 34 -> 53 CLUTs with 7
#: routes, i.e. ~2.7 CLUTs per route, so 10 routes lands near 61 and the 175 cap
#: stays far away.
#:
#: THE TRADEOFF, STATED RATHER THAN DISCOVERED: `plan_draw` sets
#: `lineup_target = rng.choice([LINEUP_MIN, LINEUP_MAX])` and natives only
#: BACKFILL up to `lineup_target`, so a world that admits 10 foreign routes gets
#: ZERO natives and is ENTIRELY NON-NATIVE. That is a legitimate outcome of the
#: feature as asked for, not a defect, but it is the visible consequence of
#: raising this number and it is recorded here on purpose. `MIN_FOREIGN` is
#: unchanged at 3.
#: FATES DIVERGE IN `unified-randomization` TASK 3, and both constants are KEPT
#: PRESENT rather than deleted because tests and prose reference them by name:
#:
#:   * `LINEUP_MIN` is DEMOTED to a REPORTING floor (Req 10.3), exactly like
#:     `MIN_FOREIGN` above: `unified_draw` reads it only after admission, to emit
#:     a shortfall row, and never to decide an admission.
#:   * `LINEUP_MAX` is RETIRED FROM ADMISSION (Req 10.1). The unified pass has NO
#:     count cap at all -- `unified_draw.unified_admit` never reads this value --
#:     because the bound that matters is the LIVE byte total, and a count budget
#:     is a second bound that can only be wrong. Retiring it cannot overrun the
#:     pool: there is no count to get wrong, only bytes that are measured as they
#:     are spent.
#:
#: IT IS NOT RETIRED FROM `AdmissionGate` IN THIS TASK, deliberately. That gate is
#: still what the BUILD walks until task 4 lands, and its `max_routes` default IS
#: this constant, so removing it here would change today's build output (grave is
#: MEASURED filling all ten foreign slots) in a task whose new pass nothing calls
#: yet. The cap is gone from the NEW pass, which is what Req 10.1 governs; the
#: legacy gate loses it when the legacy gate stops driving builds.
LINEUP_MIN, LINEUP_MAX = 6, 10

#: RETIRED (`unified-randomization` task 3). The separate enemy ARRIVAL draw no
#: longer exists as its own draw: `unified_draw.unified_admit` runs one pass on
#: `unified_draw.UNIFIED_SALT`, deriving its streams from
#: `(UNIFIED_SALT, seed, target_world, phase)`. The constant is KEPT PRESENT
#: rather than deleted because tests and prose reference it by name, and because
#: `plan_draw` / `selection_rng` below are still what the BUILD uses until task 4
#: wires the unified pass in -- deleting it would break today's build for no gain.
#: Nothing NEW should draw from it.
#:
#: Requirement 4.6, as it stood: distinct from every other salt in the project
#: (`0x5A5A5A5A` player spawn, `0x3C7B91A5` exits, `0xD150D250` drops,
#: `0x57A47125` starting loadout), so the existing per-record retyping RNG
#: stream is PROVABLY unperturbed by this feature: nothing here draws from the
#: streams those salts create.
SELECTION_SALT = 0x5A170572


def _stable_hash(text: str) -> int:
    """A hash that is the SAME IN EVERY PROCESS.

    Python's built-in `hash()` of a `str` is randomised per process by PYTHONHASH
    SEED, so using it here would mean the same seed drew a different lineup on
    every run -- Requirement 4.1 silently broken, and broken in a way no test
    inside one process could catch. So: blake2b of the lower-cased name.
    """
    digest = hashlib.blake2b(str(text).lower().encode("utf-8"),
                             digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _pool_in_effect(raised: bool, pool_size: Optional[int]) -> int:
    """The pool the build ACTUALLY patched, in bytes.

    TWO WAYS IN, ON PURPOSE.

      * `pool_size` is the exact size the caller patched (or `RESOURCE_POOL_
        VANILLA` when it patched nothing). It WINS whenever it is supplied,
        because it is the only input that cannot be wrong: it is the number the
        build wrote into the executable.
      * `raised` is the older two-valued form, kept because several callers --
        and the pinned budget test -- only know "did we raise it or not", and
        because there is more than one raised pool (the default
        `RESOURCE_POOL_RAISED`, the UNRELIABLE `RESOURCE_POOL_LOADED_MAX`, and
        the now-unselectable `RESOURCE_POOL_HIGH`), a boolean CANNOT express
        which. Under `raised=True` the budget is THE DEFAULT pool's occupancy,
        which is `RESOURCE_POOL_RAISED` (0x700000).

        THAT DEFAULT MOVED TWICE. It was 0x700000, then 0x800000 after one
        0x800000 build loaded and played, and it is 0x700000 again because a
        LATER 0x800000 build HUNG with infinite loading. Both console results are
        real, so 0x800000 is UNRELIABLE rather than good-or-bad, and the boundary
        is not a clean step. Coming back moves the two-valued budget at
        `DEFAULT_MAX_PCT` (90.0) from int(8,388,608 * 0.90) = 7,549,747 B to
        int(7,340,032 * 0.90) = 6,606,028 B -- **-943,719 B**, roughly ONE TO TWO
        FEWER arrivals per world. Paid deliberately: the boolean means "the build
        raised the pool", and what a raised build patches is the size that has
        never hung.

    The boolean is therefore a lossy view of `pool_size`, not a second concept:
    when both arrive, the size is the truth and the boolean is ignored. A build
    that patched a pool BELOW the raised preset gets that pool's occupancy, not
    the preset's -- the budget must never assume a patch that was not made
    (Requirement 6.5), in either direction.
    """
    if pool_size is not None:
        return int(pool_size)
    return ep.RESOURCE_POOL_RAISED if raised else ep.RESOURCE_POOL_VANILLA


class OccupancyRefused(ValueError):
    """A requested occupancy ceiling at or above the measured crash floor."""


def resolve_max_pct(max_pct=None) -> float:
    """The occupancy ceiling this build draws to, VALIDATED.

    `None`, blank, `measured`, `default` and `vanilla` all mean `DEFAULT_MAX_PCT`
    (90.0), so a caller that threads the option without the user setting it gets
    exactly today's build. That default is DELIBERATELY BELOW the measured
    `LOADED_MAX_PCT` (93.0), which stays a measurement and can still be asked for
    explicitly. Anything at or above
    `CRASHED_MIN_PCT` is REFUSED rather than clamped -- the same shape as
    `elf_patch.patch_resource_pool` refusing above `RESOURCE_POOL_MAX`, and for
    the same reason: there is no engine-side check that would catch it, so a
    silent clamp would hide the one decision the user needs to make.
    """
    if max_pct is None:
        return DEFAULT_MAX_PCT
    if isinstance(max_pct, str):
        label = max_pct.strip().lower()
        if label in ("", "measured", "default", "vanilla"):
            return DEFAULT_MAX_PCT
        try:
            max_pct = float(label)
        except ValueError:
            raise OccupancyRefused(
                f"pool occupancy must be a percentage or 'measured' "
                f"(= the default {DEFAULT_MAX_PCT}%, which is below the "
                f"measured loading ceiling of {LOADED_MAX_PCT}%), "
                f"got {max_pct!r}")
    try:
        pct = float(max_pct)
    except (TypeError, ValueError):
        raise OccupancyRefused(
            f"pool occupancy must be a percentage or 'measured' "
            f"(= the default {DEFAULT_MAX_PCT}%, which is below the "
            f"measured loading ceiling of {LOADED_MAX_PCT}%), got {max_pct!r}")
    if not pct > 0:
        raise OccupancyRefused(
            f"pool occupancy must be greater than 0 (got {pct})")
    if pct >= CRASHED_MIN_PCT:
        raise OccupancyRefused(
            f"pool occupancy {pct}% is REFUSED: {CRASHED_MIN_PCT}% is the "
            f"MEASURED CRASH FLOOR -- builds at 93.5%, at 94% and at 95% all "
            f"crashed at level load, and pool exhaustion is SILENT "
            f"(`MemoryPool::Allocate` falls through and returns "
            f"`sentinel_block + 16`, the caller corrupts allocator metadata "
            f"and the game dies at level load with no log line, no build "
            f"failure and no test failure). The measured LOADING ceiling is "
            f"{LOADED_MAX_PCT}% (the DEFAULT this build draws to is a lower "
            f"{DEFAULT_MAX_PCT}%); {LOADED_MAX_PCT}%..{CRASHED_MIN_PCT}% is the "
            f"only UNEXPLORED sliver left, and it is too small to be worth "
            f"another build. At or above "
            f"{CRASHED_MIN_PCT}% is not unexplored -- it is measured to crash, "
            f"so it is refused rather than clamped.")
    return pct


def pool_budget(*, raised: bool = False, pool_size: Optional[int] = None,
                max_pct=None) -> int:
    """The byte budget, as an OCCUPANCY of whichever pool is actually in effect.

    `DEFAULT_MAX_PCT` is the single source of the DEFAULT fraction (Requirement
    6.4), and it sits below the measured `LOADED_MAX_PCT`;
    `pool_size`/`raised` only decide WHAT it is a fraction OF -- see
    `_pool_in_effect` for why both exist and which wins.

    `max_pct` is the per-build ceiling and WINS whenever it is supplied, exactly
    as `pool_size` does: it is the figure the user chose for THIS build, and
    `DEFAULT_MAX_PCT` is only what a build that chose nothing draws to.
    """
    return int(_pool_in_effect(raised, pool_size)
               * resolve_max_pct(max_pct) / 100.0)


def occupancy_pct(resident_bytes: int, *, raised: bool = False,
                  pool_size: Optional[int] = None, max_pct=None) -> float:
    """`resident_bytes` as a percentage of the pool in effect, for logging.

    `max_pct` is accepted so every budget-shaped call in the project takes the
    same keyword and a caller can thread the plan's figure through uniformly. It
    is VALIDATED here (so a refused figure is refused everywhere) but it does not
    move the answer: this is the ratio of what is RESIDENT to the pool, not to
    the ceiling. The ceiling is what that ratio is judged against, and the
    caller does the judging.
    """
    resolve_max_pct(max_pct)
    return 100.0 * int(resident_bytes) / _pool_in_effect(raised, pool_size)


def selection_rng(seed, target_world: str) -> random.Random:
    """The per-target-world derived sub-seed.

    Per-WORLD rather than per-run, which answers the requirements' one open
    question in a way that survives either resolution: the draw is reproducible
    per world today, and when the other four worlds open up each gets an
    independent draw from the same run seed with no change to the scheme.
    """
    return random.Random((int(seed or 0) ^ SELECTION_SALT
                          ^ _stable_hash(target_world)) & ((1 << 128) - 1))


# ---------------------------------------------------------------------------
# Task 6 -- the ordered draw plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One foreign route the seed is willing to attempt, with its prediction."""
    type_id: int
    name: str
    source_world: str
    target_world: str
    predicted_bytes: int          # .PRS + .PRT, the quantity the pool bounds
    companions: tuple = ()
    refuses: bool = False         # measured to refuse as a GROUP

    @property
    def key(self) -> tuple:
        return (self.type_id, self.source_world, self.target_world)


@dataclass(frozen=True)
class Exclusion:
    """A roster enemy the draw could not even attempt, and WHY (Req 9.2)."""
    type_id: int
    name: str
    reason: str

    def log_line(self) -> str:
        return (f"[cross-world]   SKIP    0x{self.type_id:02X} "
                f"{self.name:<20} {self.reason}")


@dataclass(frozen=True)
class DrawPlan:
    """The seed-fixed plan. Ordered, pure, and free of any game-file contact."""
    seed: Optional[int]
    target_world: str
    budget: int
    raised_pool: bool
    order: tuple                  # tuple[Candidate], THE walk order
    native_order: tuple           # tuple[int], natives in seeded order
    exclusions: tuple             # tuple[Exclusion]
    lineup_target: int            # LINEUP_MIN or LINEUP_MAX, from the same RNG
    #: The pool the build actually patched, when the caller knew it. Carried so
    #: the plan, its log line and the gate's budget can never disagree about
    #: which pool the budget is an occupancy of. `None` = only `raised_pool` was
    #: known, which is the pre-`RESOURCE_POOL_HIGH` behaviour.
    pool_size: Optional[int] = None
    #: The occupancy ceiling this plan's `budget` is, when the caller chose one.
    #: Carried for the SAME reason `pool_size` is: the plan, its log line and the
    #: gate's budget must never disagree about which figure is in effect.
    #: `None` = `DEFAULT_MAX_PCT`, the default a build draws to, which is BELOW
    #: the measured `LOADED_MAX_PCT`.
    max_pct: Optional[float] = None

    @property
    def pool_in_effect(self) -> int:
        """The pool this plan's `budget` is an occupancy OF."""
        return _pool_in_effect(self.raised_pool, self.pool_size)

    @property
    def max_pct_in_effect(self) -> float:
        """The occupancy ceiling this plan's `budget` actually IS."""
        return resolve_max_pct(self.max_pct)

    @property
    def order_ids(self) -> tuple:
        return tuple(c.type_id for c in self.order)

    def log_lines(self) -> list:
        order = " ".join(f"0x{c.type_id:02X}" for c in self.order)
        # The percentage ACTUALLY IN EFFECT, never the module constant. Formatting
        # the constant here was the same defect as formatting the default pool:
        # a build that moved the ceiling would have logged the default while drawing to
        # something else, which is exactly the disagreement this band cannot
        # afford -- the log is the only record an exploration build leaves.
        return [
            f"[cross-world] {self.target_world.upper()} draw "
            f"(seed {self.seed}, budget {self.budget:,} B = "
            f"{self.max_pct_in_effect}% "
            f"of {self.pool_in_effect:,}"
            f"{'' if self.raised_pool else ' -- VANILLA pool, raise not applied'}):",
            f"[cross-world]   order: {order}",
        ] + [x.log_line() for x in self.exclusions]


def _cost_lookup(costs) -> Callable:
    """Accept a callable, a mapping keyed by route key, or a RouteCost sequence.

    Convenience only. Whatever the shape, a MISSING ROW STAYS MISSING: this
    never invents, scales or defaults a cost, because a guessed cost is exactly
    how a build silently overflows the pool (Requirement 3.6).
    """
    if costs is None:
        return roster_costs.cost_for
    if callable(costs):
        return costs
    if isinstance(costs, Mapping):
        table = dict(costs)
    else:
        table = {row.key: row for row in costs}
    return lambda t, s, w: table.get((int(t), s, w))


def plan_draw(seed, target_world: str, roster: Mapping, costs, *,
              allowlist: Mapping, raised_pool: bool,
              pool_size: Optional[int] = None,
              max_pct=None,
              allow_non_walkers: bool = False) -> DrawPlan:
    """The seed-fixed ORDERED candidate list. Pure: no I/O, no porting.

    Every eligible foreign enemy either becomes a `Candidate` or an `Exclusion`
    carrying its reason -- nothing falls out of the plan unaccounted for. The
    three exclusion rules, in the order they are applied:

      1. NO ALLOWLIST ENTRY for any of its source worlds. This is how
         `active=False` excludes ABSOLUTELY, for every seed (Requirement 10.3),
         and it is checked FIRST so a held-back route can never be admitted by
         some later rule.
      2. COMPANION CLOSURE NOT DERIVABLE (Requirement 2.3) -- porting an enemy
         whose extra objects may be missing is the thing the existing porter
         already refuses to do.
      3. NO MEASURED COST ROW (Requirement 3.6). `roster_costs` records why for
         the routes it drove and could not measure, and that reason is quoted.
    """
    rng = selection_rng(seed, target_world)
    cost_for = _cost_lookup(costs)

    foreign = roster_mod.eligible_foreign(roster, target_world,
                                          allow_non_walkers=allow_non_walkers)
    candidates: list = []
    exclusions: list = []
    for type_id in foreign:
        entry = roster[type_id]
        sources = sorted(entry.worlds - {target_world})

        allowed = [w for w in sources
                   if (type_id, w, target_world) in allowlist]
        if not allowed:
            exclusions.append(Exclusion(
                type_id, entry.name,
                f"no allowlist entry for any source world {sources} -> "
                f"{target_world}: either never allowlisted or held back with "
                f"active=False, and either way excluded for EVERY seed"))
            continue

        if entry.companion_reason is not None:
            exclusions.append(Exclusion(
                type_id, entry.name,
                f"companion closure could not be derived: "
                f"{entry.companion_reason}"))
            continue

        priced = [w for w in allowed if cost_for(type_id, w, target_world)]
        if not priced:
            why = " / ".join(
                roster_costs.unmeasurable_reason(type_id, w, target_world)
                or "no measured cost row" for w in allowed)
            exclusions.append(Exclusion(
                type_id, entry.name,
                f"no measured cost row for {allowed} -> {target_world}: {why}"))
            continue

        # Several priced sources is rare; when it happens the seed picks, from
        # the same stream, so the choice is reproducible.
        source = priced[0] if len(priced) == 1 else rng.choice(priced)
        row = cost_for(type_id, source, target_world)
        candidates.append(Candidate(
            type_id=type_id, name=entry.name, source_world=source,
            target_world=target_world, predicted_bytes=row.resident_bytes,
            companions=tuple(row.companions),
            refuses=bool(getattr(row, "refuses", False))))

    # A PLAIN shuffle. Not cost-sorted in either direction -- see the module
    # docstring for why both directions are defects.
    rng.shuffle(candidates)

    natives = list(roster_mod.eligible_native(roster, target_world))
    rng.shuffle(natives)

    return DrawPlan(
        seed=seed, target_world=target_world,
        budget=pool_budget(raised=raised_pool, pool_size=pool_size,
                           max_pct=max_pct),
        raised_pool=bool(raised_pool),
        order=tuple(candidates), native_order=tuple(natives),
        exclusions=tuple(exclusions),
        lineup_target=rng.choice([LINEUP_MIN, LINEUP_MAX]),
        pool_size=None if pool_size is None else int(pool_size),
        max_pct=None if max_pct is None else resolve_max_pct(max_pct))


# ---------------------------------------------------------------------------
# Task 7 -- incremental admission control
# ---------------------------------------------------------------------------

class PoolBudgetExceeded(RuntimeError):
    """Requirement 4.5, kept as an assertion that should NEVER fire.

    If the live session total ends up over budget, the incremental bound was
    violated somewhere else, and a silent rebuild would ship a build nobody
    understands. So this REFUSES TO SHIP instead.
    """


@dataclass
class AdmissionResult:
    admitted: tuple = ()          # tuple[Candidate], in walk order
    refused: tuple = ()           # tuple[(Candidate, reason)] -- ported, said no
    rejected: tuple = ()          # tuple[(Candidate, reason)] -- never attempted
    natives: tuple = ()           # tuple[int]
    resident_bytes: int = 0
    budget: int = 0
    shortfalls: tuple = ()        # explicit, reportable (Reqs 4.9, 4.10)
    log: tuple = ()

    @property
    def lineup(self) -> tuple:
        """The enemy TYPES the world can spawn: committed foreign + natives."""
        return tuple(c.type_id for c in self.admitted) + tuple(self.natives)

    @property
    def foreign_count(self) -> int:
        return len(self.admitted)

    @property
    def drawn_keys(self) -> tuple:
        return tuple(c.key for c in self.admitted)


class AdmissionGate:
    """The admission RULES, exposed one candidate at a time.

    `admit` below is a thin loop over this object, so there is exactly ONE
    implementation of "does this group still fit" in the project. The gate exists
    separately because the build does NOT own the porting loop:
    `cross_world.run_cross_world_placements` owns the per-world
    `WorldPortSession`, and the live `len(prs) + len(prt)` only exists inside
    that loop. `cli.py` therefore hands the runner an `admit_fn` callback that
    drives this gate with the real session, which is the same walk `admit`
    performs and the same decisions, just driven from the outside.

    Order of use, per candidate, is exactly `admit`'s:
      `consider(cand, live)` -> then either `record_commit` or
      `record_refusal`, and `finish(live)` once at the end.

    THE THREE KWARGS ARE A GENERALISATION, NOT A SECOND BUDGET CONCEPT
    (`cross-world-structures` Task 5, Requirement 5.1). The structure draw shares
    THIS gate and THIS budget -- the byte bound, the live-size read, the
    walk-continues-past-a-non-fit rule and the `PoolBudgetExceeded` assertion are
    all the same code -- and differs only in three ways that are genuinely not
    about budgeting:

      * `max_routes`: `None` means "no route-count cap, the budget is the only
        bound". The enemy draw caps at `LINEUP_MAX` because a lineup is a roster
        of at most seven TYPES; structures are placed by the ITEM pool, which has
        no lineup, so inventing a cap for them would be unmeasured policy.
      * `draw_natives`: `False` skips the native backfill and the two lineup
        floors, which are enemy-lineup rules (Requirements 4.9-4.11 of
        `randomized-enemy-roster`) with no structure meaning. A structure pass
        that commits nothing is a legitimate outcome, not a shortfall.
      * `label`: inserted into the COMMIT / SKIP / REFUSE lines so a reader can
        tell an enemy verdict from a structure verdict.

    All three default to exactly today's behaviour, and with `label=""` the log
    lines are byte-for-byte the ones that shipped.
    """

    def __init__(self, plan: DrawPlan, *, budget: int,
                 max_routes: Optional[int] = LINEUP_MAX,
                 draw_natives: bool = True, label: str = ""):
        self.plan = plan
        self.budget = int(budget)
        self.max_routes = max_routes
        self.draw_natives = bool(draw_natives)
        self.label = str(label)
        self.admitted: list = []
        self.refused: list = []
        self.rejected: list = []
        self.log: list = []

    def consider(self, cand: Candidate, live) -> tuple:
        """`(ok, reason)`. `live` may be an int or a callable returning one.

        The callable form exists so the LIVE size is read only when it is
        actually needed -- after the lineup cap, exactly as `admit` did before
        this was factored out.
        """
        if self.max_routes is not None and len(self.admitted) >= self.max_routes:
            reason = (f"lineup already holds {self.max_routes} foreign "
                      f"routes ({self.max_routes} is LINEUP_MAX)")
            self.rejected.append((cand, reason))
            return False, reason
        live = int(live() if callable(live) else live)
        if live + cand.predicted_bytes > self.budget:
            reason = (f"does not fit: live {live:,} B + predicted "
                      f"{cand.predicted_bytes:,} B > budget {self.budget:,} B "
                      f"(walk continues -- a cheaper later route may still fit)")
            self.rejected.append((cand, reason))
            self.log.append(f"[cross-world]   {self.label}SKIP    "
                            f"0x{cand.type_id:02X} "
                            f"{cand.name:<20} {cand.source_world:<7} {reason}")
            return False, reason
        return True, None

    def record_commit(self, cand: Candidate) -> None:
        self.admitted.append(cand)
        self.log.append(f"[cross-world]   {self.label}COMMIT  "
                        f"0x{cand.type_id:02X} "
                        f"{cand.name:<20} {cand.source_world:<7} "
                        f"+{cand.predicted_bytes:,} B predicted")

    def record_refusal(self, cand: Candidate, why=None) -> None:
        """A refusal costs THIS ENEMY and never the build (Requirement 5.1).

        It also simply ADVANCES THE WALK -- that is the whole of the backfill
        (Requirement 5.4): the next candidate in the seed-fixed order gets the
        budget the refusal did not spend, so the lineup is not shrunk by it.
        """
        self.refused.append((cand, why or "refused during porting"))
        self.log.append(f"[cross-world]   {self.label}REFUSE  "
                        f"0x{cand.type_id:02X} "
                        f"{cand.name:<20} {cand.source_world:<7} "
                        f"{why or 'refused during porting'}")

    def finish(self, live) -> AdmissionResult:
        """Append natives, check the bound and the floors, build the result."""
        plan = self.plan
        admitted, refused = self.admitted, self.refused
        rejected, log = self.rejected, list(self.log)
        shortfalls: list = []

        # Natives come AFTER the foreign pass has taken the budget it can use,
        # so they can never displace an affordable foreigner (Requirement 4.11).
        # They cost zero bytes, so they are never budget-checked.
        natives: tuple = ()
        if self.draw_natives:
            want = max(plan.lineup_target, LINEUP_MIN) - len(admitted)
            natives = tuple(plan.native_order[:max(0, want)])
            if natives:
                log.append("[cross-world]   natives: "
                           + " ".join(f"0x{n:02X}" for n in natives))

        resident = int(live() if callable(live) else live)
        if resident > self.budget:
            # Never expected. Refusing to ship beats rebuilding silently.
            raise PoolBudgetExceeded(
                f"live resident total {resident:,} B exceeds the budget "
                f"{self.budget:,} B after admission of "
                f"{[hex(c.type_id) for c in admitted]}. The incremental bound "
                f"was violated elsewhere; refusing to ship rather than "
                f"rebuilding.")

        if not self.draw_natives:
            # NO LINEUP, SO NO LINEUP FLOORS. `MIN_FOREIGN` and `LINEUP_MIN`
            # exist because an all-native enemy lineup is indistinguishable from
            # vanilla; a structure pass that commits nothing is simply a pass the
            # remaining budget could not pay for, and the caller reports the
            # spend. The BOUND above still ran, which is the part that matters.
            return AdmissionResult(
                admitted=tuple(admitted), refused=tuple(refused),
                rejected=tuple(rejected), natives=(),
                resident_bytes=resident, budget=self.budget,
                shortfalls=(), log=tuple(log))

        if len(admitted) < MIN_FOREIGN:
            shortfalls.append(
                f"FOREIGN SHORTFALL: {len(admitted)} foreign route(s) "
                f"committed, floor is {MIN_FOREIGN} (Requirement 4.10). "
                f"{len(plan.order)} candidate(s) were in the order, "
                f"{len(rejected)} did not fit or were capped, {len(refused)} "
                f"refused during porting. A nearly-all-native lineup is a "
                f"REPORTABLE outcome, not a silent pass.")
        lineup_size = len(admitted) + len(natives)
        if lineup_size < LINEUP_MIN:
            shortfalls.append(
                f"LINEUP SHORTFALL: lineup is {lineup_size} type(s), below "
                f"LINEUP_MIN {LINEUP_MIN} (Requirement 4.9) -- "
                f"{len(admitted)} foreign + {len(natives)} native, with "
                f"{len(plan.native_order)} native(s) eligible. Shipping the "
                f"largest lineup available.")
        log.extend(f"[cross-world]   {s}" for s in shortfalls)
        log.append(
            f"[cross-world]   lineup {lineup_size} types "
            f"({len(admitted)} foreign, {len(natives)} native); resident "
            f"{resident:,} B = "
            f"{occupancy_pct(resident, raised=plan.raised_pool, pool_size=plan.pool_size, max_pct=plan.max_pct):.1f}%"
            f" of a {plan.max_pct_in_effect}% ceiling")

        return AdmissionResult(
            admitted=tuple(admitted), refused=tuple(refused),
            rejected=tuple(rejected), natives=natives, resident_bytes=resident,
            budget=self.budget, shortfalls=tuple(shortfalls), log=tuple(log))


def admit(plan: DrawPlan, session_size_fn: Callable[[], int], *, budget: int,
          commit_fn: Optional[Callable] = None) -> AdmissionResult:
    """Walk the plan's order, admitting a group only while it still FITS.

    `session_size_fn()` returns the LIVE `len(session.prs) + len(session.prt)`.
    It is read BEFORE EVERY admission decision, which is the whole point: a
    route that turned out more expensive than its row predicted is absorbed by
    the next decision instead of overshooting the pool.

    `commit_fn(candidate)` is optional. When given it performs the port and
    returns truthy on commit; a falsy return (or a `(False, reason)` pair) is a
    REFUSAL, which costs that enemy and never the build (Requirement 5.1) and
    which simply ADVANCES THE WALK -- that is the backfill (Requirement 5.4).
    With no `commit_fn` this is a dry plan-through, and `session_size_fn` is the
    caller's model of the session.

    The walk CONTINUES past a non-fit rather than stopping, so a cheaper route
    later in the order can still use the remaining headroom (Requirement 4.4).
    """
    gate = AdmissionGate(plan, budget=budget)
    for cand in plan.order:
        ok, _reason = gate.consider(cand, session_size_fn)
        if not ok:
            continue

        committed, why = True, None
        if commit_fn is not None:
            outcome = commit_fn(cand)
            if isinstance(outcome, tuple):
                committed = bool(outcome[0])
                why = outcome[1] if len(outcome) > 1 else None
            else:
                committed = bool(outcome)
        if not committed:
            gate.record_refusal(cand, why)
            continue
        gate.record_commit(cand)

    return gate.finish(session_size_fn)
