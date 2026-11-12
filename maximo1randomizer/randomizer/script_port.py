"""Script_Section porting support for cross-world enemy ports.

STATE: Task 2 of ``.kiro/specs/ported-enemy-behavior-repair/`` only. This module
contains EXACTLY ONE public function, the decode-free, read-only
``script_section_bounds``. There is no opcode map here, no stub map, no operand
classification and no remap -- those are Tasks 3-5, and inventing any of them
early would fabricate semantics the prerequisite spec
``.kiro/specs/bef-script-vm-disassembler/`` has not yet recovered.

WHY THIS MODULE MAY EXIST WHILE THE PREREQUISITE IS BLOCKED (Req 1.2, as
narrowed in the plan): it imports no ``befscript``, decodes nothing, rewrites
nothing, and is reached from NO porting path until Task 7. The guarantee Req 1.2
protects is byte-identical emitted ``.PRS``/``.PRT``/``.BEF`` for a fixed seed,
which a bounds reader that nothing calls cannot violate.

BEF_Blob layout consumed here (see ``randomizer/bef.py``): magic ``0xDEADC0DE``
plus a ``0x30`` header; payload length at ``0x0C``; section offsets at ``0x1C``,
``0x20``, ``0x24``, ``0x28``, ``0x2C``, ``0x30``, ``0x34`` and ``0x38``. The
Script_Section is ``blob[u32@0x38 : u32@0x0C]``.
"""
from __future__ import annotations

import struct

#: Offset of the blob's payload-length word, which is also the Script_Section's
#: exclusive end.
SCRIPT_SECTION_END_OFFSET = 0x0C
#: Offset of the eighth section offset, which is the Script_Section's start.
SCRIPT_SECTION_START_OFFSET = 0x38

_MIN_BLOB_LEN = SCRIPT_SECTION_START_OFFSET + 4


def script_section_bounds(blob) -> tuple[int | None, int | None]:
    """Return ``(u32@0x38, u32@0x0C)`` -- the Script_Section span of ``blob``.

    Read-only and decode-free: the Opcode_Map plays no part, so this is the one
    part of this module that may exist while the prerequisite is blocked.

    NEVER RAISES, in keeping with ``mesh_port`` / ``anim_port`` / ``texture_port``:
    a malformed blob is a refusal, not an exception. A refusal is reported as
    ``(None, None)``, which is Refusal Model condition 4 -- "the Script_Section
    span is not inside the blob" -- and it is returned when

    * the blob is too short to carry both words;
    * ``u32@0x38 > u32@0x0C`` (the span runs backwards);
    * either bound is past the end of the blob.

    A span with ``start == end`` is NOT a refusal: it is a legitimately empty
    Script_Section, and reporting it as such is what lets a caller mark a
    byte-identity comparison over it VACUOUS rather than passing.
    """
    data = bytes(blob)
    if len(data) < _MIN_BLOB_LEN:
        return None, None
    end = struct.unpack_from("<I", data, SCRIPT_SECTION_END_OFFSET)[0]
    start = struct.unpack_from("<I", data, SCRIPT_SECTION_START_OFFSET)[0]
    if start > end:
        return None, None
    if start > len(data) or end > len(data):
        return None, None
    return start, end
