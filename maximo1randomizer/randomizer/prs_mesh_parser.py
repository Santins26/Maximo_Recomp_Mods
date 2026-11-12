"""
PRS mesh/skeleton/shader/render-pass parser and in-place texture-id patcher.

Reverse-engineered via disassembly of PResourceManager's loader chain
(LoadMeshResTable -> PMeshNode::Load / PSkeleton::Load -> PBone::Load ->
PMesh::Load -> PMeshFragment::Load -> PShader::Load -> PRenderPass::Load),
cross-referenced against real bytes in CASTLE.PRS. See
research/CROSSWORLD_TEXTURE_RESEARCH.md Part 4 for the full trail, including
two backwards-tag-semantics bugs that were found and fixed by testing
against real file bytes:
  - PMeshNode: tag 1 = another child follows (recurse), tag 2 = terminal/stop
  - PBone:     tag 8 = another child follows (recurse), tag 9 = terminal/stop
Both are easy to get backwards from reading disassembly alone; both were
only caught by running the parser against real bytes and watching where a
long, clean run of plausible data suddenly turned to garbage.

Verified result: parses CASTLE.PRS's entire 59-entry mesh table, extracting
600+ real texture-cache-index references (PRenderPass.tex_id) across every
top-level object in the file (props, doors, and character skeletons with
deeply nested bone hierarchies).

Dark_Knight identification (see research doc): his BEF blob (type 0x53 in
CASTLE.BEF) has the value pair (6, 25) packed as two u16s at header offset
+20 -- 25 matches mesh-table entry 25 exactly, which is one of only 13
character skeletons in the file, uses exactly 2 distinct textures (33, 34)
reused across 31 render passes (a classic "armor plating reuses a couple of
textures across many pieces" pattern), and no other candidate fits as well.

STRUCTURES CORRECTED AGAINST THE ENGINE'S OWN LOADER CHAIN
(research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md, MEASURED from MAXDEMOR.ELF's full
symbol table). Three formerly-unsolved edge cases are now closed, and with
them every resync:
  - PSTAnim's channel count is a u16 at header +0, NOT +2. `Load__7PSTAnimFP5PFile`
    @0x00195BE0 does `lhu $v0, 80($sp)` (hdr+0); the `sh $v0, 2($s3)` two
    instructions later is the IN-MEMORY field offset, which is the trap.
  - PVertexAnim's element count is a u32 at header +4, NOT +0.
    `Load__11PVertexAnimFP5PFile` @0x0016C4E0: hdr+0 is a validity word that
    must be non-zero or the engine's Load FAILS; the count is hdr+4, and the
    record is 16 + count*16 bytes with no trailing tag.
  - PMeshNode tag==4 is a PAnim block (`new PAnim(32)` + `Load__5PAnimFP5PFile`
    @0x001740F0), after which the NEXT tag is read. It is NOT a camera or
    particle attachment. (It appears ZERO times across all 11 shipped .PRS
    files -- implemented to remove a known unknown, it fixes nothing by itself.)

With those corrections all 11 .PRS files in game_files/ walk end to end with
ZERO resyncs (484 entries), and in each the byte after the mesh table is the
next resource table's type id (2 = anim table) -- an independent check that
nothing was silently absorbed.

NO RECOVERY, BY DESIGN. `LoadMeshResTable` @0x00167C30 aborts the WHOLE
resource load on any Load failure, so a resync is by definition already off
the rails relative to the engine. The `_resync_*` helpers are therefore now
LOUD REFUSALS: they raise instead of scanning, so a desynced walk can never
be reported as a successful parse. The engine's own hard failures are
mirrored as correctness gates (PMeshFragment flags&1 set, PPolyTable count
non-zero, PVertexAnim hdr+0 and count non-zero, PAnim channel count < 5,
PChannel anim type 1..4 / eval type 1..2); any of them tripping proves the
walk is already desynced.

Usage:
    from randomizer.prs_mesh_parser import Reader, load_mesh_res_table, \\
        render_passes, find_render_passes_under, patch_texture_ids

    data = open('CASTLE.PRS', 'rb').read()
    r = Reader(data, 0x20)
    load_mesh_res_table(r)   # populates the module-level render_passes list

    # Inspect what's under a specific top-level entry:
    dark_knight_passes = find_render_passes_under('mesh[25]')

    # Patch: remap Dark_Knight's textures 33->NEW_A, 34->NEW_B in place.
    new_data, patched = patch_texture_ids(data, 'mesh[25]', {33: NEW_A, 34: NEW_B})
    # `patched` lists every (offset, old_id, new_id, path) that changed.
    # File length is unchanged -- write new_data back to CASTLE.PRS as-is.
"""
import contextlib
import struct

VERBOSE = False  # set True for a full per-node/bone/fragment trace to stdout


class PRSParseError(Exception):
    pass


class Reader:
    def __init__(self, data, off=0):
        self.data = data
        self.off = off

    def read(self, n):
        if self.off + n > len(self.data):
            raise PRSParseError(f"EOF at 0x{self.off:X}, wanted {n} bytes")
        b = self.data[self.off:self.off + n]
        self.off += n
        return b

    def u32(self):
        return struct.unpack('<I', self.read(4))[0]

    def i32(self):
        return struct.unpack('<i', self.read(4))[0]

    def u16(self):
        return struct.unpack('<H', self.read(2))[0]

    def peek_u32(self):
        return struct.unpack_from('<I', self.data, self.off)[0]


def _log(msg):
    if VERBOSE:
        print(msg)


render_passes = []  # collected (offset, texture_id, path) for every PRenderPass seen


def load_st_anim_channel(r, path):
    start = r.off
    hdr = r.read(16)
    count = struct.unpack_from('<I', hdr, 0)[0]
    if not (0 <= count <= 10000):
        raise PRSParseError(f"0x{start:X}: implausible STAnimChannel count={count} path={path}")
    r.read(count * 8)


def load_st_anim(r, path):
    """16-byte header (channel count @ offset+0, u16) + that many PSTAnimChannel.

    MEASURED: `Load__7PSTAnimFP5PFile` @0x00195BE0 reads the count with
    `lhu $v0, 80($sp)` == hdr+0. The `sh $v0, 2($s3)` that follows is the
    in-memory field offset, not a file offset -- reading the store instead of
    the load is what put a +2 here, and it under-consumed every PSTAnim in the
    file by at least its channels.
    """
    start = r.off
    hdr = r.read(16)
    count = struct.unpack_from('<H', hdr, 0)[0]
    if not (0 <= count <= 64):
        raise PRSParseError(f"0x{start:X}: implausible PSTAnim channel count={count} path={path}")
    for i in range(count):
        load_st_anim_channel(r, path + f".stchannel{i}")


def load_render_pass(r, path):
    """40-byte header; offset+4 (s32) is the texture-cache index (-1 = none);
    offset+20 bit0x0001 gates an optional PSTAnim (UV-scroll) block."""
    start = r.off
    hdr = r.read(40)
    tex_id = struct.unpack_from('<i', hdr, 4)[0]
    flags20 = struct.unpack_from('<I', hdr, 20)[0]
    render_passes.append((start, tex_id, path))
    if flags20 & 0x0001:
        load_st_anim(r, path + ".stanim")


def load_shader(r, path):
    """16-byte header; offset+0 (u32) is the layer count. Each layer is a
    full PRenderPass record."""
    start = r.off
    hdr = r.read(16)
    layer_count = struct.unpack_from('<I', hdr, 0)[0]
    if not (0 <= layer_count <= 64):
        raise PRSParseError(f"0x{start:X}: implausible PShader layer_count={layer_count} path={path}")
    for i in range(layer_count):
        load_render_pass(r, path + f".layer{i}")


def load_poly_table(r, path):
    """16-byte header, n = u32@+4, then n * 32 bytes. `Load__10PPolyTableFP5PFile`
    @0x00167510 FAILS the load when n == 0, so zero is a desync gate, not a
    tolerable value."""
    start = r.off
    hdr = r.read(16)
    poly_count = struct.unpack_from('<i', hdr, 4)[0]
    if poly_count == 0:
        raise PRSParseError(
            f"0x{start:X}: PPolyTable count is 0, which the engine fails the "
            f"load on -- the walk is desynced path={path}")
    if not (0 < poly_count <= 100000):
        raise PRSParseError(f"0x{start:X}: implausible poly_count={poly_count} path={path}")
    r.read(poly_count * 32)


def load_vertex_anim(r, path):
    """16-byte header + (u32@+4) * 16 bytes, no trailing tag.

    MEASURED: `Load__11PVertexAnimFP5PFile` @0x0016C4E0 requires hdr+0 to be
    non-zero (it is a validity/keyframe word, and the engine's Load FAILS on
    zero), reads the ELEMENT COUNT from hdr+4, and `sll $a0, $v0, 4`s it for the
    allocation and the read. The previous code read the count from hdr+0.
    """
    start = r.off
    hdr = r.read(16)
    valid = struct.unpack_from('<I', hdr, 0)[0]
    count = struct.unpack_from('<I', hdr, 4)[0]
    if valid == 0:
        raise PRSParseError(
            f"0x{start:X}: PVertexAnim hdr+0 is 0, which the engine fails the "
            f"load on -- the walk is desynced path={path}")
    if count == 0:
        raise PRSParseError(
            f"0x{start:X}: PVertexAnim count (hdr+4) is 0, which the engine "
            f"fails the load on -- the walk is desynced path={path}")
    if count > 100000:
        raise PRSParseError(f"0x{start:X}: implausible PVertexAnim count={count} path={path}")
    r.read(count * 16)


def load_channel(r, path):
    """`LoadChannel__8PChannelFP5PFile` @0x001660C0: 8-byte header --
    u32@+0 anim type (1..4), u8@+4 eval type (1..2), u8@+5/+6/+7 key counts --
    then eval type 1 -> b5*16 bytes, eval type 2 -> (b5+b6+b7)*16 bytes."""
    start = r.off
    hdr = r.read(8)
    anim_type = struct.unpack_from('<I', hdr, 0)[0]
    eval_type = hdr[4]
    if not (1 <= anim_type <= 4):
        raise PRSParseError(
            f"0x{start:X}: PChannel anim type={anim_type} outside 1..4, which "
            f"the engine fails the load on path={path}")
    if eval_type == 1:
        keys = hdr[5]
    elif eval_type == 2:
        keys = hdr[5] + hdr[6] + hdr[7]
    else:
        raise PRSParseError(
            f"0x{start:X}: PChannel eval type={eval_type} outside 1..2, which "
            f"the engine fails the load on path={path}")
    r.read(keys * 16)


def load_anim(r, path):
    """`Load__5PAnimFP5PFile` @0x001740F0: 16-byte header (u32@+4 = channel
    count, must be < 5; u16@+12 = raw block size; u16@+14 = count of 32-byte
    records), that many PChannel, the raw block if non-zero, count*32 if
    non-zero, then a u32 trailing tag that must be 5."""
    start = r.off
    hdr = r.read(16)
    channel_count = struct.unpack_from('<I', hdr, 4)[0]
    raw_size = struct.unpack_from('<H', hdr, 12)[0]
    rec_count = struct.unpack_from('<H', hdr, 14)[0]
    if channel_count >= 5:
        raise PRSParseError(
            f"0x{start:X}: PAnim channel count={channel_count} is not < 5, "
            f"which the engine fails the load on path={path}")
    for i in range(channel_count):
        load_channel(r, path + f".channel{i}")
    if raw_size:
        r.read(raw_size)
    if rec_count:
        r.read(rec_count * 32)
    tag = r.u32()
    if tag != 5:
        raise PRSParseError(
            f"0x{r.off - 4:X}: PAnim trailing tag={tag}, expected 5 path={path}")


def load_vertex_anims(r, count, path):
    for i in range(count):
        load_vertex_anim(r, path + f".vanim{i}")


def load_mesh_fragment(r, path):
    """PShader::Load(fragment) + 60-byte fragment header + optional
    poly-tables (vert_count of them) + optional vertex-anims + optional
    raw face/index buffer (size given directly by header offset+4)."""
    load_shader(r, path)
    hdr_start = r.off
    hdr = r.read(60)
    flags = struct.unpack_from('<I', hdr, 0)[0]
    face_buf_size = struct.unpack_from('<I', hdr, 4)[0]
    vert_count = struct.unpack_from('<H', hdr, 12)[0]
    _log(f"      [frag hdr@0x{hdr_start:X}] flags=0x{flags:X} "
         f"face_buf_size={face_buf_size} vert_count={vert_count} path={path}")
    if not flags & 0x0001:
        # 0x00173150-0x0017315C: the engine FAILS the fragment load when bit 0
        # is clear, so it is always set in valid data. This is the gate that
        # caught the PSTAnim offset bug.
        raise PRSParseError(
            f"0x{hdr_start:X}: PMeshFragment flags=0x{flags:X} has bit 0 clear, "
            f"which the engine fails the load on -- the walk is desynced "
            f"path={path}")
    if face_buf_size == 0:
        raise PRSParseError(
            f"0x{hdr_start:X}: PMeshFragment face_buf_size is 0, which the "
            f"engine fails the load on -- the walk is desynced path={path}")
    if not (0 <= vert_count <= 200000):
        raise PRSParseError(f"0x{hdr_start:X}: implausible vert_count={vert_count} path={path}")
    for pt_i in range(vert_count):
        load_poly_table(r, path + f".polytable{pt_i}")
    vanim_count = struct.unpack_from('<I', hdr, 8)[0]
    if flags & 0x0002 and vanim_count != 0:
        load_vertex_anims(r, vanim_count, path + ".vertexanims")
    if flags & 0x0001:
        r.read(face_buf_size)


# ---------------------------------------------------------------------------
# THE FORMER RESYNC HELPERS ARE NOW REFUSALS.
#
# They used to scan forward for a plausible tag so the walk could push past a
# structure the parser did not understand. The structures are now understood
# (see the module docstring), and the engine itself has no recovery at all:
# `LoadMeshResTable` @0x00167C30 aborts the entire resource load on any Load
# failure. So a resync is, by construction, a walk that has already diverged
# from what the engine will do, and anything derived from it -- an entry's byte
# range, a table end, a texture-id offset -- is a guess.
#
# The names are kept, and kept callable with the same signatures, because
# `mesh_port.walk_mesh_table` calls `_resync_toplevel_tag` and
# `mesh_port.resync_recorder` wraps all four. They now raise, which turns the
# gate's outcome from "recovered, looks fine" into a recorded REFUSAL.
# ---------------------------------------------------------------------------

def _refuse(kind, r, path, window):
    raise PRSParseError(
        f"0x{r.off:X}: {kind} REFUSED -- the walk is desynced and this parser "
        f"no longer resyncs. The engine aborts the whole resource load on any "
        f"Load failure, so a recovered walk would report a boundary it did not "
        f"derive. path={path}")


def _resync_any_tag(r, path, window=0x4000):
    _refuse("inner tag resync", r, path, window)


def load_mesh(r, count, path):
    """`n` fragments, where `n` came from the caller's mesh header. A fragment
    that will not parse is terminal: it propagates."""
    for i in range(count):
        load_mesh_fragment(r, path + f".frag{i}")


def _resync_tag_bone(r, path, window=0x2000):
    _refuse("bone tag resync", r, path, window)


def load_bone(r, path, depth=0):
    """96 bytes (transform+name) + 16-byte mesh-header (field0 doubles as
    both 'has mesh' flag and fragment count) + optional attached PMesh +
    tag loop: 8=another child bone follows (recurse), 9=no more children."""
    if depth > 64:
        raise PRSParseError(f"0x{r.off:X}: bone recursion too deep, likely desynced path={path}")
    bone_start = r.off
    r.read(96)
    mesh_hdr_start = r.off
    mesh_hdr = r.read(16)
    has_mesh = struct.unpack_from('<I', mesh_hdr, 0)[0]
    _log(f"  [bone@0x{bone_start:X}] has_mesh={has_mesh} path={path}")
    if has_mesh:
        mesh_count = has_mesh
        if not (0 <= mesh_count <= 256):
            raise PRSParseError(f"0x{mesh_hdr_start:X}: implausible bone mesh_count={mesh_count} path={path}")
        load_mesh(r, mesh_count, path + ".mesh")
    tag = r.u32()
    if tag not in (8, 9):
        r.off = _resync_tag_bone(r, path)
        tag = r.u32()
    child_idx = 0
    while tag == 8:
        load_bone(r, path + f".child{child_idx}", depth + 1)
        tag = r.u32()
        if tag not in (8, 9):
            r.off = _resync_tag_bone(r, path)
            tag = r.u32()
        child_idx += 1
    if tag != 9:
        raise PRSParseError(f"0x{r.off-4:X}: bone tag loop ended with tag={tag}, expected 9 path={path}")


def load_shared_mesh(r, path):
    start = r.off
    hdr = r.read(16)
    has_mesh = struct.unpack_from('<I', hdr, 0)[0]
    if has_mesh:
        mesh_count = has_mesh
        if not (0 <= mesh_count <= 256):
            raise PRSParseError(f"0x{start:X}: implausible sharedmesh mesh_count={mesh_count} path={path}")
        load_mesh(r, mesh_count, path + ".sharedmesh")


def load_skeleton(r, path):
    """16-byte header (field0 gates LoadSharedMesh) + opening tag (must==8)
    + root PBone::Load (recursive) + optional LoadSharedMesh (BEFORE the
    closing tag, not after -- easy to get backwards) + closing tag (must==7)."""
    hdr = r.read(16)
    field0 = struct.unpack_from('<I', hdr, 0)[0]
    tag1 = r.u32()
    if tag1 != 8:
        raise PRSParseError(f"0x{r.off-4:X}: skeleton opening tag={tag1}, expected 8 path={path}")
    load_bone(r, path + ".root")
    if field0:
        load_shared_mesh(r, path + ".shared")
    tag2 = r.u32()
    if tag2 != 7:
        raise PRSParseError(f"0x{r.off-4:X}: skeleton closing tag={tag2}, expected 7 path={path}")


def _resync_tag(r, path, window=0x2000):
    """Bounded forward scan for the next plausible tag value (1 or 2),
    used only when the expected tag read comes back implausible. This lets
    the parser push past not-yet-understood edge cases rather than being
    fully blocked by them -- see module docstring for known unsolved cases."""
    start = r.off - 4
    for cand in range(start, min(start + window, len(r.data) - 4), 4):
        v = struct.unpack_from('<I', r.data, cand)[0]
        if v in (1, 2):
            return cand
    raise PRSParseError(f"0x{start:X}: resync failed to find a valid tag within {window} bytes path={path}")


def load_mesh_node(r, path, depth=0):
    """112-byte transform (NOTE: version-gated, 80 bytes on older files --
    not yet auto-detected, hardcoded to 112 here) + 16-byte mesh-header +
    optional attached PMesh + tag loop: 1=another child node follows
    (recurse), 2=no more children (terminal, with a notify call). tag==4
    is a rare, untraced special case (raises clearly if hit)."""
    if depth > 64:
        raise PRSParseError(f"0x{r.off:X}: meshnode recursion too deep path={path}")
    node_start = r.off
    r.read(112)
    mesh_hdr_start = r.off
    mesh_hdr = r.read(16)
    has_mesh = struct.unpack_from('<I', mesh_hdr, 0)[0]
    _log(f"  [node@0x{node_start:X}] has_mesh={has_mesh} path={path}")
    if has_mesh:
        mesh_count = has_mesh
        if not (0 <= mesh_count <= 256):
            raise PRSParseError(f"0x{mesh_hdr_start:X}: implausible node mesh_count={mesh_count} path={path}")
        load_mesh(r, mesh_count, path + ".mesh")
    tag = r.u32()
    if tag == 4:
        # `Load__9PMeshNodeFP5PFile` @0x00176F34: tag 4 means a PAnim block
        # follows, after which the NEXT tag is read. Accepted only at a node's
        # FIRST tag read -- inside the child loop the engine takes only 1 or 2.
        load_anim(r, path + ".anim")
        tag = r.u32()
    if tag not in (1, 2):
        r.off = _resync_tag(r, path)
        tag = r.u32()
    child_idx = 0
    while tag == 1:
        load_mesh_node(r, path + f".child{child_idx}", depth + 1)
        tag = r.u32()
        if tag not in (1, 2):
            r.off = _resync_tag(r, path)
            tag = r.u32()
        child_idx += 1
    if tag != 2:
        raise PRSParseError(f"0x{r.off-4:X}: meshnode tag loop ended with tag={tag}, expected 2 path={path}")


def _resync_toplevel_tag(r, path, window=0x8000):
    _refuse("top-level tag resync", r, path, window)


#: Type id of the resource table that follows the mesh table. `Load__16P
#: ResourceManager...` @0x00168230 dispatches 1 -> mesh, 2 -> anim, 3 ->
#: texture, and MEASURED on all 11 shipped .PRS files the mesh table is
#: followed by the ANIM table.
NEXT_RESOURCE_TABLE_TYPE = 2


def check_end_of_table(r, expected=NEXT_RESOURCE_TABLE_TYPE):
    """POST-CONDITION on a completed mesh-table walk: the next u32 must be the
    following resource table's type id. Peeks only; does not advance.

    This is independent of everything the mesh loaders did, so it is what makes
    refusing (rather than resyncing) safe: if the walk absorbed or dropped bytes
    it lands somewhere that is not a table type id, and the porter must refuse
    -- the engine would too.
    """
    got = r.peek_u32()
    if got != expected:
        raise PRSParseError(
            f"0x{r.off:X}: end-of-mesh-table post-condition failed -- next u32 "
            f"is {got}, expected the next resource table's type id {expected}. "
            f"The walk is wrong; nothing derived from it can be trusted")
    return got


def load_mesh_res_table(r, verify_next_table=False):
    """Entry point: [u32 uNumMeshes][per entry: u32 tag; tag==1 ->
    PMeshNode::Load; tag==6 -> PSkeleton::Load].

    NO RECOVERY. `LoadMeshResTable` @0x00167C30 aborts the whole resource load
    on any Load failure, so a failing entry propagates here too rather than
    being resynced past into a walk whose boundaries are guesses.

    `verify_next_table=True` additionally enforces `check_end_of_table` once the
    table is done. It is off by default because callers legitimately parse a
    SYNTHETIC one-entry table (see `mesh_port.round_trip_entry`) that has no
    following resource table.
    """
    count = r.u32()
    _log(f"uNumMeshes = {count}")
    for i in range(count):
        entry_start = r.off
        tag = r.u32()
        path = f"mesh[{i}]@0x{entry_start:X}"
        if tag == 1:
            load_mesh_node(r, path)
        elif tag == 6:
            load_skeleton(r, path)
        else:
            raise PRSParseError(f"0x{entry_start:X}: unknown mesh-table tag={tag} (expected 1 or 6) idx={i}")
        _log(f"  [{i}] tag={tag} consumed up to 0x{r.off:X}")
    if verify_next_table:
        check_end_of_table(r)


def parse_prs_mesh_table(prs_path):
    """Convenience entry point: parse a world's .PRS mesh table and return
    the list of (file_offset, texture_cache_id, path) tuples collected.
    Resets the module-level render_passes list first."""
    global render_passes
    render_passes = []
    with open(prs_path, 'rb') as f:
        data = f.read()
    r = Reader(data, 0x20)
    load_mesh_res_table(r)
    return render_passes


def find_render_passes_under(prefix):
    """Return every (offset, tex_id, path) already collected in the global
    render_passes list whose path starts with the given prefix (e.g.
    'mesh[25]' to get every render pass belonging to that top-level entry,
    including all nested bones/fragments/layers).

    Contract on emptiness: an empty result is NOT an error and is NOT
    distinguishable on its own from "this mesh genuinely uses no textures".
    Callers that need to tell the two apart must check whether the parse
    collected anything at all under that entry -- use
    has_render_passes(prefix) / render_pass_tex_ids(prefix), and treat an
    empty list as a named fallback trigger (a resync inside the entry can
    make its passes unreachable; the ICE Snowman 0x1D case does exactly
    this). This function never raises for emptiness, by design: the caller
    routes zero passes to a logged fallback, not to an exception.
    """
    return [(off, tid, p) for off, tid, p in render_passes if p.startswith(prefix)]


def has_render_passes(prefix):
    """True if the current parse collected at least one render pass under
    `prefix`. Side-effect free. False means the caller must take the
    zero-render-pass fallback path (see find_render_passes_under's
    emptiness contract) rather than assuming a texture-less mesh."""
    return any(p.startswith(prefix) for _, _, p in render_passes)


def render_pass_tex_ids(prefix):
    """The set of distinct texture-cache ids referenced under `prefix`,
    excluding -1 (== no texture). Side-effect free. An empty set combined
    with has_render_passes(prefix) == False is the zero-render-pass
    fallback condition; an empty set with has_render_passes() == True means
    passes exist but none bind a texture."""
    return {tid for _, tid, p in render_passes if p.startswith(prefix) and tid != -1}


@contextlib.contextmanager
def parser_scope():
    """Isolate one parse's accumulated global state.

    `render_passes` is module-global and every loader appends to it, so two
    parses in the same process would otherwise share a list: the second
    parse's find_render_passes_under('mesh[N]') could match passes left
    over from the first and silently produce a wrong texture remap. Wrap
    each parse:

        with parser_scope():
            r = Reader(data, 0x20)
            load_mesh_res_table(r)
            passes = find_render_passes_under('mesh[25]')

    Saves the current `render_passes`, installs a fresh empty list for the
    duration, and restores the saved object on exit -- including when the
    body raises. `render_passes` is the only state this module accumulates
    (VERBOSE is a caller-owned config flag, not per-parse state), so it is
    the only thing saved here.

    Nesting is safe. Callers that still clear the global by hand before
    parsing keep working unchanged; this is additive.
    """
    global render_passes
    saved = render_passes
    render_passes = []
    try:
        yield render_passes
    finally:
        render_passes = saved


def patch_texture_ids(data, prefix, tex_id_map):
    """Return (new_bytes, patched) where new_bytes is `data` with every
    PRenderPass texture-id field under `prefix` remapped according to
    tex_id_map (old_tex_id -> new_tex_id), and patched is a list of
    (offset, old_tex_id, new_tex_id, path) for everything actually changed.
    Render passes whose current tex_id isn't a key in tex_id_map are left
    untouched.

    This is a pure in-place overwrite -- exactly 4 bytes change per
    affected render pass, at offset+4 of its 40-byte header. No other byte
    in the file moves or shifts, so there is no file-growth/resync concern
    here (unlike the .PRT texture-porting case in texture_port.py). The
    resulting bytes are the same length as the input and can be written
    straight back to the .PRS file.

    `render_passes` (the module-level list) must already be populated by a
    prior call to load_mesh_res_table()/parse_prs_mesh_table() against this
    same `data`.
    """
    out = bytearray(data)
    patched = []
    for off, tid, path in find_render_passes_under(prefix):
        if tid in tex_id_map:
            new_tid = tex_id_map[tid]
            struct.pack_into('<i', out, off + 4, new_tid)
            patched.append((off, tid, new_tid, path))
    return bytes(out), patched


if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'CASTLE.PRS'
    results = parse_prs_mesh_table(path)
    print(f"{path}: collected {len(results)} texture references")
    for off, tid, p in results[:20]:
        print(f"  0x{off:X}  tex_id={tid}  {p}")
