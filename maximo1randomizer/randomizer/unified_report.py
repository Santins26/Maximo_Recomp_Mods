"""The unified run report (unified-randomization task 4, Requirement 9).

ONE block per target world, and it is a REPORT: nothing here decides anything.
Every number it prints is read off the `UnifiedResult` the pass produced, the
config it drew from, and the oracle's own tables -- so a line can disagree with
the build only if the build disagreed with itself.

WHAT IT IS CAREFUL ABOUT, because the requirements refuse to blur them:

  * FOUR VERDICTS, one per Route_Candidate considered: COMMITTED, SKIPPED
    (budget), REFUSED (asset gate), EXCLUDED (rule). Each rule exclusion carries
    its `limit_kind` AND the table name, so limit (a) protection, limit (b)
    budget and limit (c) permission are never printed as the same thing.
  * A BUDGET SKIP IS NOT A SHORTFALL. `SKIPPED (budget)` is the budget WORKING
    (U3) and is printed as an ordinary line with its predicted cost and the
    headroom that was left. `LINEUP_MIN` / `MIN_FOREIGN` shortfalls are printed
    separately, and only as reports.
  * OCCUPANCY IS STATED AS A PERCENTAGE. Pool exhaustion is SILENT
    (`MemoryPool::Allocate` falls through to `sentinel_block + 16`; the game dies
    at level load with no log line, no build failure and no test failure), so how
    close a build ran is the one number a user cannot get any other way.
  * `[cross-world]` PREFIX ON EVERY LINE. `ELF patch:` stays reserved for edits
    made to the executable and never appears here.
"""
from __future__ import annotations

from . import spawn_config as sc
from . import structure_draw
from . import unified_draw as ud

LOG_PREFIX = ud.LOG_PREFIX

#: How many weight rows to name per category before summarising the rest. The
#: unified table is per-world and large (every native pool type plus every
#: route), and a log that prints all of it is a log nobody reads.
_WEIGHT_ROWS = 12


def _pct(part: int, whole: int) -> float:
    return (100.0 * float(part) / float(whole)) if whole else 0.0


def _weights_in_effect(config, world: str) -> list[str]:
    """The unified weights this world actually drew from, by category."""
    out: list[str] = []
    native = dict(config.native_weights(world))
    enemy = dict(config.enemy_route_weights(world))
    structure = dict(config.structure_route_weights(world))
    total = sum(native.values())
    out.append(f"{LOG_PREFIX}   unified weights in effect [{world}]: "
               f"{len(native)} native pool type(s) totalling {total:,.0f}, "
               f"{len(enemy)} enemy route(s), "
               f"{len(structure)} structure route(s) -- ONE table, ONE scale")
    for label, rows in (("enemy", enemy), ("structure", structure)):
        if not rows:
            out.append(f"{LOG_PREFIX}     {label} routes: none drawable here "
                       f"(disabled, weight 0, or not offered by the oracle)")
            continue
        named = sorted(rows.items(), key=lambda kv: (-kv[1], kv[0]))
        shown = ", ".join(
            f"0x{tid:02X} {src}->{tgt} w={w:g}"
            for (tid, src, tgt), w in named[:_WEIGHT_ROWS])
        more = ("" if len(named) <= _WEIGHT_ROWS
                else f", +{len(named) - _WEIGHT_ROWS} more")
        out.append(f"{LOG_PREFIX}     {label} routes: {shown}{more}")
    return out


def _protection_lines(world: str, result) -> list[str]:
    """The Protection_Policy, reported as IN FORCE and UNCHANGED (Req 7)."""
    out = [
        f"{LOG_PREFIX}   Protection_Policy [{world}]: IN FORCE and UNCHANGED. "
        f"PROTECTED_TAGS, PROTECTED_INSTANCE_NAMES, the end* kill groups, the "
        f"Gold_Key / Level_Column / Skeleton_Key / Monster_Generator anchors, "
        f"HAZARD_PROTECTED_AS_SOURCE and the ordered puzzle chains all still "
        f"run; there is no relaxation setting and no relaxation code path"]
    named: list[str] = []
    for cand in sc.structure_route_candidates(world):
        reason = structure_draw._never_touch_reason(cand.type_id)
        if reason:
            named.append(f"{cand.key} ({reason})")
    for row in result.excluded_rule:
        if len(row) >= 2 and row[1] == ud.LIMIT_PROTECTION:
            named.append(f"{getattr(row[0], 'key', row[0])} "
                         f"(limit_kind={ud.LIMIT_PROTECTION})")
    if named:
        for entry in named:
            out.append(f"{LOG_PREFIX}     protection EXCLUDED {entry}")
    else:
        out.append(f"{LOG_PREFIX}     protection excluded no Candidate this "
                   f"world (it still ran -- nothing offered was protected)")
    return out


def _verdict_lines(result) -> list[str]:
    """ONE verdict per Route_Candidate considered. Four kinds, never blurred."""
    out: list[str] = []
    for cand in result.committed_enemies:
        out.append(f"{LOG_PREFIX}     COMMITTED  enemy     {cand.key} "
                   f"{cand.cost_bytes:,} B measured resident")
    for cand in result.committed_structures:
        out.append(f"{LOG_PREFIX}     COMMITTED  structure {cand.key} "
                   f"{cand.cost_bytes:,} B measured resident")
    for cand, cost, headroom in result.skipped_budget:
        out.append(f"{LOG_PREFIX}     SKIPPED    budget    {cand.key} "
                   f"limit_kind={ud.LIMIT_BUDGET} "
                   f"table=Shared_Budget (not a table refusal): predicted "
                   f"{int(cost):,} B > headroom {int(headroom):,} B. An "
                   f"ORDINARY SKIP -- NOT a shortfall, not a warning, nothing "
                   f"ported, the walk continued")
    for cand, why in result.refused_gate:
        out.append(f"{LOG_PREFIX}     REFUSED    asset gate {getattr(cand, 'key', cand)} "
                   f"limit_kind=asset-trust (NOT a permission or budget "
                   f"limit): {why}")
    for row in result.excluded_rule:
        cand = row[0] if row else None
        limit = row[1] if len(row) > 1 else "?"
        table = row[2] if len(row) > 2 else "?"
        out.append(f"{LOG_PREFIX}     EXCLUDED   rule      "
                   f"{getattr(cand, 'key', cand)} limit_kind={limit} "
                   f"refused by: {table}")
    if not out:
        out.append(f"{LOG_PREFIX}     no Route_Candidate was considered for "
                   f"this world (nothing drawable, which is not an error)")
    return out


def report_world(result, *, config, seed, structures_enabled: bool,
                 enemies_enabled: bool) -> list[str]:
    """The whole block for ONE target world, as lines. Pure: no printing."""
    world = result.world
    budget = int(result.budget)
    committed = int(result.bytes_committed)
    resident = budget - int(result.bytes_remaining)
    target = float(getattr(config, "enemy_share_target",
                           sc.UNIFIED_ENEMY_SHARE_DEFAULT) or 0.0)

    out = [
        f"{LOG_PREFIX} ===== UNIFIED PASS [{world}] =====",
        f"{LOG_PREFIX}   seed {seed}, UNIFIED_SALT 0x{ud.UNIFIED_SALT:08X}, "
        f"streams hash(UNIFIED_SALT, seed, world, phase) with phase in "
        f"{{native, enemy, structure}} (reproduce with --seed {seed})",
        f"{LOG_PREFIX}   switches: cross-world ENEMIES "
        f"{'ON' if enemies_enabled else 'OFF'}, cross-world STRUCTURES "
        f"{'ON' if structures_enabled else 'OFF'} -- INDEPENDENT switches, "
        f"either works alone",
    ]
    out += _weights_in_effect(config, world)
    out.append(
        f"{LOG_PREFIX}   enemy share: configured enemy_share_target "
        f"{target:.3f} vs REALISED {result.realised_enemy_share:.3f} "
        f"(the realised figure is the retyping pool this world will draw "
        f"from, with every route this pass committed already in it)")
    out.append(
        f"{LOG_PREFIX}   Shared_Budget {budget:,} B; COMMITTED this pass "
        f"{committed:,} B; REMAINING {int(result.bytes_remaining):,} B; "
        f"resident total {resident:,} B = {_pct(resident, budget):.1f}% "
        f"OCCUPANCY of the budget. Pool exhaustion is SILENT: there is no log "
        f"line, no build failure and no test failure, so this percentage is "
        f"the only warning a build gets")
    out.append(
        f"{LOG_PREFIX}   admission order ACTUALLY used: PHASE 1 ENEMIES "
        f"(priority) to a ceiling of {int(result.enemy_ceiling):,} B "
        f"|| PHASE BOUNDARY || PHASE 2 STRUCTURES to the full {budget:,} B, so "
        f"the {structure_draw.STRUCTURE_RESERVE_BYTES:,} B STRUCTURE SLICE is "
        f"spendable only in phase 2"
        + ("" if structures_enabled else
           " -- STRUCTURES ARE OFF, so phase 2 did not run and no reserve was "
           "carved"))
    out += list(result.log)
    out.append(f"{LOG_PREFIX}   verdicts, ONE per Route_Candidate considered "
               f"(committed / skipped-budget / refused-gate / excluded-rule):")
    out += _verdict_lines(result)
    out += _protection_lines(world, result)
    if result.shortfalls:
        for line in result.shortfalls:
            out.append(f"{LOG_PREFIX}   SHORTFALL {line}")
    else:
        out.append(f"{LOG_PREFIX}   shortfalls: none "
                   f"(LINEUP_MIN / MIN_FOREIGN both met). A budget skip is "
                   f"never counted here")
    out.append(f"{LOG_PREFIX}   every committed route stays CANDIDATE until you "
               f"observe it in game; nothing here promotes anything")
    return out


def report(results, *, config, seed, structures_enabled: bool,
           enemies_enabled: bool, emit) -> None:
    """Emit the block for every target world, in sorted world order."""
    for world in sorted(results):
        for line in report_world(results[world], config=config, seed=seed,
                                 structures_enabled=structures_enabled,
                                 enemies_enabled=enemies_enabled):
            emit(line)


def migration_lines(report_obj) -> list[str]:
    """The v3 -> v4 migration report, as lines (Req 8.2).

    `spawn_config` deliberately does not print, so the logging belongs to the
    caller. Every MAPPED, DROPPED and DEFAULTED field is already counted on the
    report object; the dropped entries are named individually because a lost
    selection the user made is the one thing a count cannot convey.
    """
    if report_obj is None:
        return []
    counts = report_obj.counts
    out = [f"{LOG_PREFIX} spawn config MIGRATED to version "
           f"{sc.CONFIG_VERSION}: {report_obj.summary()}",
           f"{LOG_PREFIX}   mapped {counts.get('mapped', 0)}, dropped "
           f"{counts.get('dropped', 0)}, defaulted "
           f"{counts.get('defaulted', 0)}. `enabled` was copied VERBATIM: a "
           f"route you turned OFF in v3 is still OFF, and the v3 file was read "
           f"rather than rewritten in place"]
    # `lines()` already opens with the summary, so it is skipped here; the
    # DROPPED entries are named individually because a selection the user made
    # and lost is the one thing a count cannot convey.
    for line in report_obj.lines()[1:]:
        out.append(f"{LOG_PREFIX}   {line}")
    return out
