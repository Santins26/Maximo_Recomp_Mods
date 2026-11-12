"""OFFLINE route-cost measurement harness (Task 3 of `randomized-enemy-roster`).

WHAT THIS IS
    A RESEARCH TOOL. Nothing in `randomizer/` imports it, it is not part of any
    build, and it writes NOTHING: every row it produces goes to stdout for a
    human to paste into `randomizer/roster_costs.py`. It reads game files and
    holds buffers in memory only.

WHAT IT MEASURES
    For ONE route `(enemy_id, source_world, target_world)` it opens a FRESH
    `WorldPortSession`, drives the UNMODIFIED gated path for that single group
    through `cross_world.run_cross_world_placements`, and reports the `.PRS`,
    `.PRT` and `.BEF` buffer deltas against the session's own pristine copies.
    That is the same method the exhaustive GRAVE-route measurement recorded in
    `catalog.py`'s comments used, so the method is not new.

    NO GATE IS RELAXED, ANYWHERE. `COMPANION_PORT_ENABLED` stays on, so a row is
    the whole GROUP's cost. A route the gated path refuses is reported as a
    REFUSAL with its reason, and its deltas are then zero because a refusal is
    all-or-nothing and mutates no buffer.

THE ONE THING THIS PROCESS SUBSTITUTES, STATED PLAINLY
    `companion_port.route_companions` reads `catalog.CROSS_WORLD_ALLOWLIST`
    directly, and only 11 routes are described there. Measuring a route the
    catalog does not yet describe therefore needs a companion set from
    somewhere, so the child process REBINDS `catalog.CROSS_WORLD_ALLOWLIST` to a
    SYNTHETIC one-route tuple for the duration of the measurement (the
    `allowlist=` parameter of `run_cross_world_placements` exists for exactly
    this, and the rebind additionally reaches `route_companions`). Two rules
    make that honest:

      * for a route the catalog DOES describe, the synthetic entry carries the
        catalog's OWN hand-measured `companions` literal -- the ground truth --
        so a known route is measured with exactly the data it ships with;
      * for any other route the closure comes from `roster.derive_companions`,
        the Task 1 oracle that calibrated 11/11 against those literals.

    Measuring a cost is NOT activating a route: no `active` flag is read or
    written, and the catalog file is never edited.

MEMORY / FILE DISCIPLINE (Requirements 11.1-11.3, 11.5)
    ONE ROUTE PER PROCESS. The driver spawns a child per route and parses no
    binary itself, so no process measures two routes and no second target `.PRS`
    is ever resident. A child touches the minimum the unmodified port path
    requires: the source `.BEF` (for the blob and the Spawn-edge oracle), the
    target `.PRS`/`.PRT`/`.BEF` held by the session, and the source `.PRS` /
    `.PRT` the porter reads transiently. That six-file floor belongs to the port
    path itself; the harness adds nothing to it, and batching across processes is
    what keeps each measurement's footprint at one route.

USAGE
    Driver (measures every route, prints the table and pasteable rows):
        python -m research.tools.measure_route_costs
    One route, this process (the child mode the driver uses):
        python -m research.tools.measure_route_costs --route 0x0D --source swamp
    Enemy-alone cost, companions excluded, for a route that refuses AS A GROUP:
        python -m research.tools.measure_route_costs --route 0x53 --source castle --enemy-only
    Validation pass (the seven already-measured routes only):
        python -m research.tools.measure_route_costs --known-only
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GAME_FILES = REPO_ROOT / "game_files"

#: The DEFAULT target, kept so every existing invocation of this tool measures
#: exactly what it measured before. It is no longer the ONLY target: `--target`
#: selects any world in `ELIGIBLE_FOREIGN`, and the driver measures whichever one
#: it is pointed at. Nothing else in this file assumes grave any more.
TARGET_WORLD = "grave"

#: Eligible foreign routes PER TARGET WORLD, from
#: `roster.eligible_foreign(build_roster(...), target)` (Task 2). Held as
#: literals here so the driver process parses no `.BEF` of its own.
#:
#: grave is the CONTROL and is unchanged. ice / swamp were stage 1, MEASURED in
#: research/MULTI_WORLD_TARGET_BUDGET_MEASUREMENT.md §2 -- 13 and 10 routes.
#: under (10) and castle (8) are STAGE 2 and are measured the same way: one
#: route per process, into that world's OWN pristine archives.
ELIGIBLE_FOREIGN: dict = {
    "grave": (0x08, 0x0D, 0x10, 0x13, 0x16, 0x19, 0x1D, 0x27,
              0x3A, 0x49, 0x53, 0x58, 0x5D),
    "ice":   (0x08, 0x0D, 0x13, 0x16, 0x19, 0x27, 0x2F, 0x37, 0x48, 0x53,
              0x58, 0x5D, 0x6A),
    "swamp": (0x10, 0x13, 0x16, 0x1D, 0x27, 0x3A, 0x49, 0x53, 0x58, 0x5D),
    "under": (0x08, 0x0D, 0x10, 0x16, 0x19, 0x1D, 0x37, 0x3A, 0x53, 0x58),
    "castle": (0x0D, 0x10, 0x13, 0x19, 0x1D, 0x27, 0x3A, 0x5D),
}

#: Backwards-compatible alias. The grave list used to be the only one.
ELIGIBLE_FOREIGN_GRAVE = ELIGIBLE_FOREIGN["grave"]

#: MEASURED vanilla `.PRS`/`.PRT` sizes per target, asserted in every child so a
#: mis-read target world is LOUD rather than silently producing a wrong delta.
#: (research/MULTI_WORLD_TARGET_BUDGET_MEASUREMENT.md §1.)
VANILLA_SIZES: dict = {
    "grave": (2_563_126, 817_232),
    "castle": (4_199_541, 1_633_184),
    "swamp": (3_442_632, 1_255_072),
    "ice": (3_082_301, 1_154_816),
    "under": (3_641_513, 1_352_448),
}

#: The routes whose cost is ALREADY KNOWN, keyed by TARGET then enemy. The
#: harness is validated against these BEFORE any unknown route is trusted; a
#: disagreement is a HARNESS BUG (or a real change) and is reported, never
#: papered over. Only grave has known figures -- ice and swamp are what this
#: stage measures for the first time, so they carry no expectations at all.
KNOWN_PRS_PRT_BY_TARGET: dict = {"grave": {}, "ice": {}, "swamp": {},
                                 "under": {}, "castle": {}}
KNOWN_PRS_PRT = KNOWN_PRS_PRT_BY_TARGET["grave"]
KNOWN_PRS_PRT.update({
    0x0D: (234_996, 66_576),
    0x19: (377_851, 110_656),
    0x1D: (359_462, 189_536),
    0x49: (532_307, 225_472),
    0x58: (411_500, 84_000),
    0x3A: (458_624, 117_808),
    # 0x53 STOPPED BEING AN EXPECTED REFUSAL. This entry read `None` -- "measured
    # despite REFUSING as a group" -- which was true until the prs_mesh_parser
    # field-offset correction made CASTLE.PRS mesh[54] addressable. Dark_Knight's
    # group has committed since, and `roster_costs` has carried the group figures
    # with `refuses=False` since then; this literal was simply left behind, so
    # every driver run reported a spurious disagreement. Updated to the measured
    # group cost, which is the same number `roster_costs` records.
    0x53: (616_642, 166_960),
})

#: Vanilla GRAVE, kept as named constants because the grave-only era's callers
#: and notes refer to them. The per-target table above is what the child asserts.
VANILLA_PRS, VANILLA_PRT = VANILLA_SIZES["grave"]


# --- source-world choice ---------------------------------------------------

def source_candidates(type_id: int, target_world: str = TARGET_WORLD) -> list:
    """Worlds that could source this enemy, best candidate first.

    A source world must own the enemy's MESH, which is `catalog.native_worlds`
    -- BEF presence alone is not enough to port from. The catalog's own choice
    of source for a route it already describes comes first, so a known route is
    re-measured on exactly the route that was measured before.
    """
    from randomizer import catalog

    owned = [w for w in sorted(catalog.native_worlds(type_id))
             if w != target_world]
    preferred = [e.source_world for e in catalog.CROSS_WORLD_ALLOWLIST
                 if e.enemy_id == type_id and e.target_world == target_world
                 and e.source_world in owned]
    return list(dict.fromkeys(preferred + owned))


# --- the measurement (child process) ---------------------------------------

def measure_route(type_id: int, source_world: str, target_world: str,
                  game_files_dir: Path, *, enemy_only: bool = False,
                  verbose: bool = False) -> dict:
    """Measure ONE route in THIS process. Returns a row dict; never raises."""
    from randomizer import catalog
    from randomizer import cross_world as cw
    from randomizer import roster
    from randomizer.bef import BefFile

    logs: list[str] = []
    emit = (lambda m: print(f"    | {m}")) if verbose else logs.append

    row = {
        "type_id": type_id,
        "name": catalog.name_for_type(type_id),
        "source_world": source_world,
        "target_world": target_world,
        "prs_bytes": 0,
        "prt_bytes": 0,
        "bef_bytes": 0,
        "companions": [],
        "status": "ERROR",
        "reason": "",
        "enemy_only": bool(enemy_only),
        "verified": None,
    }

    source_path = Path(game_files_dir) / f"{source_world.upper()}.BEF"
    if not source_path.is_file():
        row["reason"] = f"{source_path.name} is missing"
        return row

    source_bef = BefFile.parse(source_path)
    if not source_bef.has_type(type_id):
        row["status"] = "NO_BLOB"
        row["reason"] = (f"{source_path.name} declares no type "
                         f"0x{type_id:02X}")
        return row
    blob = source_bef.get_entry(type_id).data

    # --- the companion set: catalog literal if it exists, else derived ------
    catalog_entry = next(
        (e for e in catalog.CROSS_WORLD_ALLOWLIST
         if e.enemy_id == type_id and e.source_world == source_world
         and e.target_world == target_world), None)
    if catalog_entry is not None and catalog_entry.companions is not None:
        companions = tuple(catalog_entry.companions)
        companion_src = "catalog hand-measured literal"
    else:
        closure, reason = roster.derive_companions(type_id, source_world,
                                                   source_bef)
        if reason is not None:
            row["status"] = "NO_CLOSURE"
            row["reason"] = reason
            return row
        companions = tuple(sorted(closure))
        companion_src = "derived by roster.derive_companions"
    row["companions"] = [int(c) for c in companions]

    entry = catalog.AllowEntry(
        type_id, catalog.name_for_type(type_id), source_world, target_world,
        catalog.CANDIDATE,
        f"SYNTHETIC measurement-only entry (research/tools/"
        f"measure_route_costs.py); companions {companion_src}",
        True, companions)

    saved = catalog.CROSS_WORLD_ALLOWLIST
    try:
        # The one substitution, documented in the module docstring: this is what
        # lets `companion_port.route_companions` see a route the catalog does not
        # yet describe. No `active` flag and no measured literal is edited.
        catalog.CROSS_WORLD_ALLOWLIST = (entry,)
        allowlist = {entry.key: entry}

        if enemy_only:
            # The COMPANIONS-OFF path, byte-for-byte what a run with
            # `COMPANION_PORT_ENABLED = False` does: `classify_placement` then
            # `session.port_enemy`, both unmodified. Used only to answer "what
            # does it cost to ATTEMPT this route" for a group that refuses.
            session = cw.WorldPortSession(target_world, game_files_dir, log=emit)
            plan = cw.classify_placement(type_id, target_world,
                                         cross_world=True, allowlist=allowlist)
            if plan.path != cw.PORT:
                row["status"] = "NOT_PORT"
                row["reason"] = f"classified {plan.path}: {plan.reason}"
                return row
            session.port_enemy(plan, blob)
            session.finish()
            plans = [plan]
        else:
            sessions: dict = {}
            cw.run_cross_world_placements(
                [(type_id, target_world, blob)], cross_world=True,
                game_files_dir=game_files_dir, log=emit, allowlist=allowlist,
                sessions_out=sessions)
            session = sessions.get(target_world)
            if session is None:
                row["status"] = "NO_SESSION"
                row["reason"] = ("the gated path opened no session for this "
                                 "route; see the log")
                return row
            plans = None
    finally:
        catalog.CROSS_WORLD_ALLOWLIST = saved

    expect = VANILLA_SIZES.get(target_world)
    if expect is not None:
        assert len(session.pristine_prs) == expect[0], len(session.pristine_prs)
        assert len(session.pristine_prt) == expect[1], len(session.pristine_prt)

    row["prs_bytes"] = len(session.prs) - len(session.pristine_prs)
    row["prt_bytes"] = len(session.prt) - len(session.pristine_prt)
    row["bef_bytes"] = len(session.bef) - len(session.pristine_bef)

    results = list(session.results)
    refused = [r for r in results if r.refused]
    committed = [r for r in results if r.ok]
    if committed and not refused:
        row["status"] = "COMMIT"
        report = cw.verify_session(session, plans)
        row["verified"] = bool(report.ok)
        if not report.ok:
            row["reason"] = "; ".join(report.failures)
        row["companion_injected"] = int(
            sum(r.companion_injected for r in committed))
    else:
        row["status"] = "REFUSE"
        reasons = [r.plan.reason for r in refused if r.plan.reason]
        row["reason"] = " | ".join(reasons) or "refused with no reason recorded"

    row["log_tail"] = logs[-6:]
    del session
    return row


# --- driver ----------------------------------------------------------------

def _child(type_id: int, source_world: str, *, enemy_only: bool,
           target_world: str = TARGET_WORLD) -> dict | None:
    """Measure one route in a FRESH process and return its row."""
    cmd = [sys.executable, "-m", "research.tools.measure_route_costs",
           "--route", hex(type_id), "--source", source_world,
           "--target", target_world, "--emit-json"]
    if enemy_only:
        cmd.append("--enemy-only")
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                          text=True)
    for line in proc.stdout.splitlines():
        if line.startswith("ROW "):
            return json.loads(line[4:])
    print(f"    !! child failed for 0x{type_id:02X} <- {source_world} "
          f"(exit {proc.returncode})")
    for line in (proc.stdout.strip().splitlines()[-5:]
                 + proc.stderr.strip().splitlines()[-8:]):
        print(f"       {line}")
    return None


def _fmt(row: dict) -> str:
    comps = " ".join(f"0x{c:02X}" for c in row["companions"]) or "-"
    return (f"  0x{row['type_id']:02X} {row['name']:<18} "
            f"{row['source_world']:<7} {row['status']:<9} "
            f"{row['prs_bytes']:>9,} .PRS {row['prt_bytes']:>9,} .PRT "
            f"{row['bef_bytes']:>7,} .BEF  companions {comps}")


def _pasteable(row: dict) -> str:
    comps = ", ".join(f"0x{c:02X}" for c in row["companions"])
    note = row["reason"].replace('"', "'")[:400] or "measured clean"
    return (f"    RouteCost(0x{row['type_id']:02X}, "
            f"{row['source_world']!r}, {row['target_world']!r}, "
            f"{row['prs_bytes']}, {row['prt_bytes']}, {row['bef_bytes']},\n"
            f"              ({comps}{',' if len(row['companions']) == 1 else ''}), "
            f"\"{row['status']}: {note}\"),")


def run_driver(type_ids, *, known_only: bool = False,
               target_world: str = TARGET_WORLD) -> int:
    """Spawn one child per route, in order, and report everything."""
    from randomizer import catalog          # names / sources only; no binaries

    known = KNOWN_PRS_PRT_BY_TARGET.get(target_world, {})
    if known_only:
        type_ids = [t for t in type_ids if t in known]

    vanilla = VANILLA_SIZES.get(target_world, (0, 0))
    print("=" * 78)
    print("ROUTE COST MEASUREMENT -- one route per process, nothing written")
    print(f"target world: {target_world.upper()}  "
          f"(vanilla {vanilla[0]:,} .PRS + {vanilla[1]:,} .PRT)")
    print(f"routes: {' '.join(f'0x{t:02X}' for t in type_ids)}")
    print("=" * 78)

    rows: list[dict] = []
    for type_id in type_ids:
        sources = source_candidates(type_id, target_world)
        if not sources:
            print(f"  0x{type_id:02X} {catalog.name_for_type(type_id)}: no "
                  f"source world owns its mesh; NO ROW")
            continue
        chosen = None
        for source_world in sources:
            print(f"  measuring 0x{type_id:02X} "
                  f"{catalog.name_for_type(type_id)} <- {source_world} ...",
                  flush=True)
            row = _child(type_id, source_world, enemy_only=False,
                         target_world=target_world)
            if row is None:
                continue
            print(_fmt(row))
            if row["status"] == "COMMIT":
                chosen = row
                break
            chosen = chosen or row
        if chosen is None:
            continue
        if chosen["status"] == "REFUSE":
            # A refusal mutates nothing, so the group's deltas are zero. What it
            # COSTS TO ATTEMPT is the enemy-alone figure, measured on the
            # companions-off path and labelled as such -- never merged into the
            # group figure.
            print(f"    0x{type_id:02X} refuses as a group; measuring the "
                  f"enemy-alone attempt cost", flush=True)
            alone = _child(chosen["type_id"], chosen["source_world"],
                           enemy_only=True, target_world=target_world)
            if alone is not None:
                print(_fmt(alone))
                chosen["enemy_only_prs"] = alone["prs_bytes"]
                chosen["enemy_only_prt"] = alone["prt_bytes"]
                chosen["enemy_only_bef"] = alone["bef_bytes"]
                chosen["enemy_only_status"] = alone["status"]
        rows.append(chosen)

    print()
    print("=" * 78)
    print("MEASURED TABLE")
    print("=" * 78)
    for row in rows:
        print(_fmt(row))
        if "enemy_only_prs" in row:
            print(f"        enemy-alone attempt cost: "
                  f"{row['enemy_only_prs']:,} .PRS / "
                  f"{row['enemy_only_prt']:,} .PRT / "
                  f"{row['enemy_only_bef']:,} .BEF "
                  f"({row['enemy_only_status']})")
        if row["reason"]:
            print(f"        {row['reason'][:300]}")

    print()
    print("=" * 78)
    print("VALIDATION against the already-known figures")
    print("=" * 78)
    bad = 0
    for row in rows:
        known = KNOWN_PRS_PRT_BY_TARGET.get(target_world, {}).get(
            row["type_id"], "unknown")
        if known == "unknown":
            print(f"  0x{row['type_id']:02X} NEW      "
                  f"{row['prs_bytes']:>9,} / {row['prt_bytes']:>9,}")
            continue
        if known is None:
            print(f"  0x{row['type_id']:02X} EXPECTED-REFUSAL -> "
                  f"{row['status']}")
            if row["status"] != "REFUSE":
                bad += 1
                print("       !! MISMATCH: this route is expected to REFUSE")
            continue
        got = (row["prs_bytes"], row["prt_bytes"])
        ok = got == known
        bad += 0 if ok else 1
        print(f"  0x{row['type_id']:02X} {'MATCH' if ok else 'MISMATCH'}  "
              f"got {got[0]:,} / {got[1]:,}  "
              f"expected {known[0]:,} / {known[1]:,}")
    print(f"  {bad} disagreement(s) with the known figures")
    if bad:
        print("  A disagreement is a HARNESS BUG or a real change. Report it; "
              "do NOT overwrite the known-good numbers.")

    print()
    print("=" * 78)
    print("PASTEABLE ROUTE_COSTS ROWS")
    print("=" * 78)
    for row in rows:
        print(_pasteable(row))
    return 1 if bad else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", help="single route: enemy type id, e.g. 0x0D")
    parser.add_argument("--source", help="source world for --route")
    parser.add_argument("--target", default=TARGET_WORLD)
    parser.add_argument("--enemy-only", action="store_true",
                        help="companions-off path (attempt cost of a group "
                             "that refuses)")
    parser.add_argument("--emit-json", action="store_true",
                        help="print the machine-readable ROW line")
    parser.add_argument("--known-only", action="store_true",
                        help="measure only the seven already-known routes")
    parser.add_argument("--game-files", default=str(GAME_FILES))
    args = parser.parse_args(argv)

    if args.route:
        type_id = int(args.route, 0)
        sources = [args.source] if args.source else source_candidates(type_id,
                                                                     args.target)
        if not sources:
            print(f"no source world owns 0x{type_id:02X}'s mesh")
            return 2
        row = measure_route(type_id, sources[0], args.target,
                            Path(args.game_files),
                            enemy_only=args.enemy_only,
                            verbose=not args.emit_json)
        if args.emit_json:
            print("ROW " + json.dumps(row))
        else:
            print(_fmt(row))
            if row["reason"]:
                print(f"    {row['reason']}")
            print(_pasteable(row))
        return 0

    routes = ELIGIBLE_FOREIGN.get(args.target)
    if routes is None:
        print(f"no eligible-foreign route list for target {args.target!r}; "
              f"known targets: {', '.join(sorted(ELIGIBLE_FOREIGN))}")
        return 2
    return run_driver(routes, known_only=args.known_only,
                      target_world=args.target)


if __name__ == "__main__":
    raise SystemExit(main())
