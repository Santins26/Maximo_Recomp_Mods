"""BEF Script VM instruction set and script API index map (Phase 3).

Everything in this module is DATA recovered from `game_files/MAXDEMOR.ELF`
(the debug "demo" build, the only build with an intact `.symtab`) using
`file_offset = 0x80 + (vaddr - 0x100000)`. Nothing here reads a file at import
time and nothing here writes anything, ever.

The disassembler itself (task 7) consumes this module; it is deliberately not
implemented here yet.

How the instruction set was measured
------------------------------------
`CVmExecutionEngine::ExecuteByteCodes` (`0x001098D0`, 124 bytes) fetches ONE
opcode byte with `lbu`, scales it by 12 into the in-object dispatch table at
`this+0x70` (256 entries), advances the instruction pointer at `this+0xD0C` by
**exactly 1**, and calls the handler through `__ptmf_scall` (`0x001D9E80`).

Because the interpreter only ever advances the IP by one, **each handler reads
its own operand bytes from `this+0xD2C` (bytecode base) + `this+0xD0C` (IP) and
advances the IP itself.** So every opcode's operand layout is a direct
measurement: disassemble the handler, take the widths of its loads off the
`base + ip` pointer in program order, and take the constant it adds to
`this+0xD0C` after each fetch. `length` below is `1 + sum(operand widths)`.

All 158 real handlers were measured this way, and every handler address resolves
to a `<mnemonic>__18CVmExecutionEngineFv` symbol, so the mnemonics are the
engine's own names, not invented ones. Every IP store was checked to lie outside
any loop in its handler, so **every instruction in this encoding is fixed
length** — there is no data-dependent operand block.

What is measured and what is inference
--------------------------------------
Measured: the opcode byte, the mnemonic (from `.symtab`), the handler address,
the operand COUNT, each operand's WIDTH and ORDER, and the total length.
Inference: the operand NAMES and the split between `u8`/`u32`/`i32`/`rel8`/
`rel32`/`apiidx` where the load itself only proves a width. Where a handler
used a signed load (`lb`) the operand is recorded as `rel8`, except `by_ir_inc`
whose 1-byte delta is sign-extended but is not a branch displacement; it is
listed in `SIGN_EXTENDED_U8`.

The letter-prefix reading, tested against the measurement
---------------------------------------------------------
`r` = register (a slot in the register file at `this+0xC74`, indexed by a
1-byte operand scaled by 4), `c` = inline constant, `l` = local/frame index,
`m` = member/field offset, leading `i`/`f` = integer/float operation type, and
`by_` = the narrow form, which replaces EVERY 4-byte operand of its wide twin
with a 1-byte one. Confirmed by measurement for the push/pop/load families
(`c_push` 4 bytes vs `by_c_push` 1; `l_push` 4 vs `by_l_push` 1; `irr_add`
three 1-byte register operands vs `ir_neg` one). Exceptions, also measured:
the `if*`/`jump` families carry a leading branch displacement that no letter
names; `ir_inc` carries an unnamed immediate delta; and the array/field
families carry one register operand more than their letters suggest. See
`research/BEF_SCRIPT_VM.md` (task 13.1) for the full write-up.

Encoding revisions
------------------
`OPCODES_PROTOTYPE` and `OPCODES_RETAIL` are built from ONE shared base plus
per-revision override tables, so no opcode value is written down twice. The
override tables are currently EMPTY: the demo VM implements a single instruction
set that contains BOTH the narrow (`0x5E` `by_r_ifeqz`, 3 bytes) and wide
(`0x64` `r_ifeqz`, 6 bytes) branch forms, so the observed prototype/retail
difference is which forms the script compiler emitted, not a different length
for the same byte. Whether a prototype section is executable by the retail VM
unmodified is task 10, and is NOT claimed here.

Revision selection is always an explicit argument. Nothing in this module
sniffs bytes.
"""
from __future__ import annotations

from collections import namedtuple

# ---------------------------------------------------------------------------
# VM facts (measured, Phase 2)
# ---------------------------------------------------------------------------

#: `file_offset = 0x80 + (vaddr - ELF_LOAD_VADDR)` for all three builds.
ELF_FILE_BASE = 0x80
ELF_LOAD_VADDR = 0x100000

#: `CVmExecutionEngine::ExecuteByteCodes`, the dispatch loop.
DISPATCH_VADDR = 0x001098D0
DISPATCH_SIZE = 124
#: `CVmExecutionEngine::InitInstructionSet`, which builds the dispatch table.
INIT_INSTRUCTION_SET_VADDR = 0x00109AE0
INIT_INSTRUCTION_SET_SIZE = 5844

#: In-object dispatch table: 256 entries of 12 bytes at `this+0x70`.
DISPATCH_TABLE_OFFSET = 0x70
DISPATCH_TABLE_ENTRIES = 256
DISPATCH_TABLE_STRIDE = 12
#: Handler installed in every unused slot.
INVALID_INSTRUCTION_HANDLER = 0x0010B1C0
#: `__ptmf_scall`, through which the dispatch loop reaches each handler.
PTMF_SCALL_VADDR = 0x001D9E80

#: VM state fields, all relative to the `CVmExecutionEngine` object.
IP_OFFSET = 0xD0C          # instruction pointer, a BYTE INDEX into the section
BYTECODE_BASE_OFFSET = 0xD2C   # pointer to the script section's first byte
REGISTER_FILE_OFFSET = 0xC74   # register slots, index * 4

#: The dispatch loop's IP advance for the opcode byte itself.
OPCODE_BYTE_LENGTH = 1

REVISION_PROTOTYPE = "prototype"
REVISION_RETAIL = "retail"
REVISIONS = frozenset({REVISION_PROTOTYPE, REVISION_RETAIL})

CONFIDENCE_LEVELS = frozenset({"verified", "inferred", "unknown"})

#: Operand kinds and their encoded widths in bytes.
KIND_WIDTHS = {
    "u8": 1,
    "u16": 2,
    "u32": 4,
    "i32": 4,
    "rel8": 1,
    "rel32": 4,
    "apiidx": 4,
}
OPERAND_KINDS = frozenset(KIND_WIDTHS)

#: Operands that name a script API slot. `callapi` encodes it as a 4-byte
#: `apiidx`; `by_callapi` narrows it to one byte, which the kind vocabulary
#: spells `u8`, so the annotation path (task 7.3) keys on the operand NAME
#: as well as on the `apiidx` kind.
API_INDEX_OPERAND_NAME = "api_index"

#: `by_ir_inc`'s 1-byte delta is loaded with `lb`, i.e. sign-extended, but it
#: is not a branch displacement so it is not spelled `rel8`.
SIGN_EXTENDED_U8 = frozenset({0x47})

OpcodeEntry = namedtuple(
    "OpcodeEntry",
    ("mnemonic", "length", "operands", "handler", "confidence", "revisions"),
)

_BOTH = frozenset({REVISION_PROTOTYPE, REVISION_RETAIL})

# ---------------------------------------------------------------------------
# The shared base map. (opcode, mnemonic, handler vaddr, operands)
# `length` is computed as 1 + sum of operand widths, so it can never disagree
# with the declared operands.
# ---------------------------------------------------------------------------

_REG = ("reg", "u8")
_DST = ("dst_reg", "u8")
_SRC = ("src_reg", "u8")
_LHS = ("lhs_reg", "u8")
_RHS = ("rhs_reg", "u8")
_COUNT = ("count", "u8")
_REL8 = ("target", "rel8")
_REL32 = ("target", "rel32")

_BASE_SPEC = (
    # -- stack push / pop ---------------------------------------------------
    (0x01, "r_push", 0x00112E80, (_REG,)),
    (0x02, "r_pushset", 0x00112EE0, (("first_reg", "u8"), ("last_reg", "u8"))),
    (0x03, "r_pushall", 0x00112FC0, ()),
    (0x04, "by_c_push", 0x00113020, (("value", "u8"),)),
    (0x05, "c_push", 0x00113070, (("value", "i32"),)),
    (0x06, "by_l_push", 0x001130F0, (("local", "u8"),)),
    (0x07, "l_push", 0x00113160, (("local", "u32"),)),
    (0x08, "by_m_push", 0x001131F0, (("member", "u8"),)),
    (0x09, "m_push", 0x00113250, (("member", "u32"),)),
    (0x0A, "r_pop", 0x001132E0, (_REG,)),
    (0x0B, "r_popset", 0x00113350, (("first_reg", "u8"), ("last_reg", "u8"))),
    (0x0C, "r_popall", 0x00113430, ()),
    (0x0D, "by_l_pop", 0x00113480, (("local", "u8"),)),
    (0x0E, "l_pop", 0x001134F0, (("local", "u32"),)),
    (0x0F, "by_m_pop", 0x00113590, (("member", "u8"),)),
    (0x10, "m_pop", 0x00113600, (("member", "u32"),)),
    (0x11, "l_pushset", 0x00113690, (("first_local", "u32"), ("last_local", "u32"))),
    (0x12, "m_pushset", 0x001138A0, (("first_member", "u32"), ("last_member", "u32"))),
    (0x13, "l_popset", 0x00113AB0, (("first_local", "u32"), ("last_local", "u32"))),
    (0x14, "m_popset", 0x00113CB0, (("first_member", "u32"), ("last_member", "u32"))),
    (0x15, "popapiparam", 0x0010E6F0, ()),
    (0x16, "popsetapiparam", 0x0010E770, (("count", "u32"),)),
    (0x17, "by_popsetapiparam", 0x0010E810, (_COUNT,)),
    (0x18, "by_l_pushset", 0x001137C0, (("first_local", "u8"), ("last_local", "u8"))),
    (0x19, "by_m_pushset", 0x001139D0, (("first_member", "u8"), ("last_member", "u8"))),
    (0x1A, "by_l_popset", 0x00113BD0, (("first_local", "u8"), ("last_local", "u8"))),
    (0x1B, "by_m_popset", 0x00113DE0, (("first_member", "u8"), ("last_member", "u8"))),
    # -- loads --------------------------------------------------------------
    (0x1E, "r_load", 0x0010ED70, (_DST, _SRC)),
    (0x1F, "r_loadset", 0x0010EE30, (
        ("first_dst_reg", "u8"), ("last_dst_reg", "u8"),
        ("first_src_reg", "u8"), ("last_src_reg", "u8"))),
    (0x20, "by_rc_load", 0x0010F000, (_DST, ("value", "u8"))),
    (0x21, "rc_load", 0x0010F090, (_DST, ("value", "i32"))),
    (0x22, "by_rl_load", 0x0010F140, (_DST, ("local", "u8"))),
    (0x23, "rl_load", 0x0010F1E0, (_DST, ("local", "u32"))),
    (0x24, "by_rm_load", 0x0010F2B0, (_DST, ("member", "u8"))),
    (0x25, "rm_load", 0x0010F3B0, (_DST, ("member", "u32"))),
    (0x26, "by_lr_load", 0x0010F4D0, (("local", "u8"), _SRC)),
    (0x27, "lr_load", 0x0010F580, (("local", "u32"), _SRC)),
    (0x28, "by_mr_load", 0x0010F650, (("member", "u8"), _SRC)),
    (0x29, "mr_load", 0x0010F750, (("member", "u32"), _SRC)),
    (0x2A, "by_lc_load", 0x0010F870, (("local", "u8"), ("value", "u8"))),
    (0x2B, "lc_load", 0x0010F8D0, (("local", "u32"), ("value", "i32"))),
    (0x2C, "by_mc_load", 0x0010F970, (("member", "u8"), ("value", "u8"))),
    (0x2D, "mc_load", 0x0010FA40, (("member", "u32"), ("value", "i32"))),
    (0x2E, "r_loadthis", 0x0010FB50, (_DST,)),
    (0x3C, "r_swap", 0x0010FBF0, (("reg_a", "u8"), ("reg_b", "u8"))),
    (0x3D, "r_swapset", 0x0010FCC0, (
        ("first_reg_a", "u8"), ("last_reg_a", "u8"),
        ("first_reg_b", "u8"), ("last_reg_b", "u8"))),
    # -- arithmetic ---------------------------------------------------------
    (0x41, "irr_add", 0x00111560, (_DST, _LHS, _RHS)),
    (0x42, "irr_sub", 0x001115E0, (_DST, _LHS, _RHS)),
    (0x43, "irr_mul", 0x00111660, (_DST, _LHS, _RHS)),
    (0x44, "irr_div", 0x001116E0, (_DST, _LHS, _RHS)),
    (0x45, "irr_rem", 0x00111770, (_DST, _LHS, _RHS)),
    (0x46, "ir_neg", 0x00111800, (_REG,)),
    (0x47, "by_ir_inc", 0x00111840, (_REG, ("delta", "u8"))),
    (0x48, "ir_inc", 0x00111890, (_REG, ("delta", "i32"))),
    (0x49, "frr_add", 0x00111910, (_DST, _LHS, _RHS)),
    (0x4A, "frr_sub", 0x00111990, (_DST, _LHS, _RHS)),
    (0x4B, "frr_mul", 0x00111A10, (_DST, _LHS, _RHS)),
    (0x4C, "frr_div", 0x00111A90, (_DST, _LHS, _RHS)),
    (0x4D, "fr_neg", 0x00111B10, (_REG,)),
    (0x5A, "irr_and", 0x0010FE90, (_DST, _LHS, _RHS)),
    (0x5B, "irr_or", 0x0010FF10, (_DST, _LHS, _RHS)),
    (0x5C, "irr_xor", 0x0010FF90, (_DST, _LHS, _RHS)),
    (0x5D, "ir_not", 0x00110010, (_REG,)),
    # -- branches -----------------------------------------------------------
    (0x5E, "by_r_ifeqz", 0x00110C50, (_REL8, _REG)),
    (0x5F, "by_r_ifnez", 0x00110CB0, (_REL8, _REG)),
    (0x60, "by_r_ifltz", 0x00110D10, (_REL8, _REG)),
    (0x61, "by_r_iflez", 0x00110D70, (_REL8, _REG)),
    (0x62, "by_r_ifgtz", 0x00110DD0, (_REL8, _REG)),
    (0x63, "by_r_ifgez", 0x00110E30, (_REL8, _REG)),
    (0x64, "r_ifeqz", 0x00110050, (_REL32, _REG)),
    (0x65, "r_ifnez", 0x001100E0, (_REL32, _REG)),
    (0x66, "r_ifltz", 0x00110170, (_REL32, _REG)),
    (0x67, "r_iflez", 0x00110200, (_REL32, _REG)),
    (0x68, "r_ifgtz", 0x00110290, (_REL32, _REG)),
    (0x69, "r_ifgez", 0x00110320, (_REL32, _REG)),
    (0x6A, "irr_ifcmpeq", 0x001103B0, (_REL32, _LHS, _RHS)),
    (0x6B, "irr_ifcmpne", 0x00110460, (_REL32, _LHS, _RHS)),
    (0x6C, "irr_ifcmplt", 0x00110510, (_REL32, _LHS, _RHS)),
    (0x6D, "irr_ifcmplte", 0x001105C0, (_REL32, _LHS, _RHS)),
    (0x6E, "irr_ifcmpgt", 0x00110670, (_REL32, _LHS, _RHS)),
    (0x6F, "irr_ifcmpgte", 0x00110720, (_REL32, _LHS, _RHS)),
    (0x70, "frr_ifcmpeq", 0x001107D0, (_REL32, _LHS, _RHS)),
    (0x71, "frr_ifcmpne", 0x00110880, (_REL32, _LHS, _RHS)),
    (0x72, "frr_ifcmplt", 0x00110930, (_REL32, _LHS, _RHS)),
    (0x73, "frr_ifcmplte", 0x001109E0, (_REL32, _LHS, _RHS)),
    (0x74, "frr_ifcmpgt", 0x00110A90, (_REL32, _LHS, _RHS)),
    (0x75, "frr_ifcmpgte", 0x00110B40, (_REL32, _LHS, _RHS)),
    (0x76, "jump", 0x00110BF0, (_REL32,)),
    (0x77, "by_irr_ifcmpeq", 0x00110E90, (_REL8, _LHS, _RHS)),
    (0x78, "by_irr_ifcmpne", 0x00110F10, (_REL8, _LHS, _RHS)),
    (0x79, "by_irr_ifcmplt", 0x00110F90, (_REL8, _LHS, _RHS)),
    (0x7A, "by_irr_ifcmplte", 0x00111020, (_REL8, _LHS, _RHS)),
    (0x7B, "by_irr_ifcmpgt", 0x001110B0, (_REL8, _LHS, _RHS)),
    (0x7C, "by_irr_ifcmpgte", 0x00111140, (_REL8, _LHS, _RHS)),
    (0x7D, "by_frr_ifcmpeq", 0x001111D0, (_REL8, _LHS, _RHS)),
    (0x7E, "by_frr_ifcmpne", 0x00111260, (_REL8, _LHS, _RHS)),
    (0x7F, "by_frr_ifcmplt", 0x001112F0, (_REL8, _LHS, _RHS)),
    (0x80, "by_frr_ifcmplte", 0x00111380, (_REL8, _LHS, _RHS)),
    (0x81, "by_frr_ifcmpgt", 0x00111410, (_REL8, _LHS, _RHS)),
    (0xE6, "by_frr_ifcmpgte", 0x001114A0, (_REL8, _LHS, _RHS)),
    (0xE7, "by_jump", 0x00111530, (_REL8,)),
    # -- fields -------------------------------------------------------------
    (0x82, "mr_putfield", 0x00111B50, (("member", "u32"), ("obj_reg", "u8"), _SRC)),
    (0x83, "rm_getfield", 0x00111CB0, (_DST, ("member", "u32"), ("obj_reg", "u8"))),
    (0x84, "rm_fieldarraylength", 0x00111E10,
     (_DST, ("member", "u32"), ("obj_reg", "u8"))),
    (0x85, "rm_fieldarraygetval", 0x00111F90,
     (_DST, ("member", "u32"), ("obj_reg", "u8"), ("index", "i32"))),
    (0x86, "mr_fieldarraysetval", 0x00112160,
     (("member", "u32"), ("obj_reg", "u8"), ("index", "i32"), _SRC)),
    # -- calls --------------------------------------------------------------
    (0x87, "calllocal", 0x0010D9A0, (("target", "u32"),)),
    (0x88, "by_calllocal", 0x0010DAF0, (("target", "u8"),)),
    (0x89, "callextern", 0x0010DC20, (("target", "u32"), ("arg_count", "u8"))),
    (0x8A, "by_callextern", 0x0010DE70, (("target", "u8"), ("arg_count", "u8"))),
    (0x8B, "callapi", 0x0010E200, ((API_INDEX_OPERAND_NAME, "apiidx"),)),
    (0x8C, "by_callapi", 0x0010E350, ((API_INDEX_OPERAND_NAME, "u8"),)),
    (0x8D, "r_apiparam", 0x0010E480, (_REG,)),
    (0x8E, "c_apiparam", 0x0010E500, (("value", "i32"),)),
    (0x8F, "m_apiparam", 0x0010E5A0, (("member", "u32"),)),
    (0x90, "l_apiparam", 0x0010E640, (("local", "u32"),)),
    (0x91, "returnvoid", 0x0010E0A0, ()),
    (0x92, "alloclocal", 0x0010D630, (("size", "u32"), _REG)),
    (0x93, "freelocal", 0x0010D720, ()),
    (0x94, "exit", 0x0010D790, (("code", "u8"),)),
    (0x95, "pushpc", 0x0010D8A0, (("target", "u32"),)),
    # -- arrays -------------------------------------------------------------
    (0x97, "r_deletearray", 0x001147B0, (_REG,)),
    (0x98, "rl_arraylength", 0x001148A0, (_DST, ("local", "u32"))),
    (0x99, "rm_arraylength", 0x001149A0, (_DST, ("member", "u32"))),
    (0x9A, "rl_arraygetval", 0x00114AF0, (_DST, ("local", "u32"), ("index_reg", "u8"))),
    (0x9B, "rm_arraygetval", 0x00114C20, (_DST, ("member", "u32"), ("index_reg", "u8"))),
    (0x9C, "lr_arraysetval", 0x00114DA0, (("local", "u32"), ("index_reg", "u8"), _SRC)),
    (0x9D, "mr_arraysetval", 0x00114ED0, (("member", "u32"), ("index_reg", "u8"), _SRC)),
    (0xDC, "m_newarray", 0x00113EC0, (("member", "u32"), ("count_reg", "u8"), ("type", "u8"))),
    (0xDD, "l_newarray", 0x00114360, (("local", "u32"), ("count_reg", "u8"), ("type", "u8"))),
    (0xDE, "rr_arraylength", 0x00115050, (_DST, ("array_reg", "u8"))),
    (0xDF, "rr_arraygetval", 0x00115140, (_DST, ("array_reg", "u8"), ("index_reg", "u8"))),
    (0xE0, "rr_arraysetval", 0x00115260, (("array_reg", "u8"), ("index_reg", "u8"), _SRC)),
    # -- SIMD ---------------------------------------------------------------
    (0xA0, "by_rl_simdload", 0x00112330, (_DST, ("local", "u8"), _COUNT)),
    (0xA1, "rl_simdload", 0x001123C0, (_DST, ("local", "u32"), _COUNT)),
    (0xA2, "by_lr_simdload", 0x00112480, (("local", "u8"), _SRC, _COUNT)),
    (0xA3, "lr_simdload", 0x00112510, (("local", "u32"), _SRC, _COUNT)),
    (0xA4, "by_rm_simdload", 0x001125D0, (_DST, ("member", "u8"), _COUNT)),
    (0xA5, "rm_simdload", 0x00112650, (_DST, ("member", "u32"), _COUNT)),
    (0xA6, "by_mr_simdload", 0x00112700, (("member", "u8"), _SRC, _COUNT)),
    (0xA7, "mr_simdload", 0x00112780, (("member", "u32"), _SRC, _COUNT)),
    (0xAF, "irr_simdadd", 0x00112830, (_DST, _LHS, _RHS, _COUNT)),
    (0xB0, "irr_simdsub", 0x001128E0, (_DST, _LHS, _RHS, _COUNT)),
    (0xB1, "irr_simdmul", 0x00112990, (_DST, _LHS, _RHS, _COUNT)),
    (0xB2, "irr_simddiv", 0x00112A40, (_DST, _LHS, _RHS, _COUNT)),
    (0xB3, "ir_simdneg", 0x00112B00, (_REG, _COUNT)),
    (0xB4, "frr_simdadd", 0x00112B60, (_DST, _LHS, _RHS, _COUNT)),
    (0xB5, "frr_simdsub", 0x00112C10, (_DST, _LHS, _RHS, _COUNT)),
    (0xB6, "frr_simdmul", 0x00112CC0, (_DST, _LHS, _RHS, _COUNT)),
    (0xB7, "frr_simddiv", 0x00112D70, (_DST, _LHS, _RHS, _COUNT)),
    (0xB8, "fr_simdneg", 0x00112E20, (_REG, _COUNT)),
    # -- conversions and misc ----------------------------------------------
    (0xC8, "rr_inttochar", 0x0010E890, (_DST, _SRC)),
    (0xC9, "rr_inttofloat", 0x0010E950, (_DST, _SRC)),
    (0xCA, "rr_floattoint", 0x0010EA20, (_DST, _SRC)),
    (0xCB, "rr_floattochar", 0x0010EAF0, (_DST, _SRC)),
    (0xCC, "checkdyncast", 0x0010EBC0, (_REG, ("class_id", "u32"))),
    (0xFE, "nop", 0x0010B230, ()),
    (0xFF, "breakpoint", 0x0010B240, ()),
)


def _operand_bytes(operands):
    return sum(KIND_WIDTHS[kind] for _name, kind in operands)


def _build_base():
    out = {}
    for opcode, mnemonic, handler, operands in _BASE_SPEC:
        if opcode in out:  # pragma: no cover - guarded by tests
            raise ValueError(f"opcode 0x{opcode:02X} defined twice in the base map")
        out[opcode] = OpcodeEntry(
            mnemonic=mnemonic,
            length=OPCODE_BYTE_LENGTH + _operand_bytes(operands),
            operands=operands,
            handler=handler,
            confidence="verified",
            revisions=_BOTH,
        )
    return out


#: The 158 opcodes the demo VM implements, all measured, all `verified`.
OPCODES_BASE = _build_base()

#: Per-revision overrides. Both are deliberately EMPTY: no opcode was measured
#: to have a different length or layout between the two revisions, because the
#: single demo instruction set carries the narrow and wide forms as separate
#: opcode values. Any future revision-specific entry goes here and ONLY here,
#: so a value is still never written down twice.
PROTOTYPE_OVERRIDES: dict = {}
RETAIL_OVERRIDES: dict = {}


def _apply(overrides):
    merged = dict(OPCODES_BASE)
    merged.update(overrides)
    return merged


OPCODES_PROTOTYPE = _apply(PROTOTYPE_OVERRIDES)
OPCODES_RETAIL = _apply(RETAIL_OVERRIDES)

_MAPS = {
    REVISION_PROTOTYPE: OPCODES_PROTOTYPE,
    REVISION_RETAIL: OPCODES_RETAIL,
}


def opcode_map(revision):
    """Return the opcode map for `revision`. No default, nothing inferred."""
    try:
        return _MAPS[revision]
    except KeyError:
        raise ValueError(
            f"unknown encoding revision {revision!r}; expected one of {sorted(REVISIONS)}"
        ) from None


#: The 98 dispatch slots `InitInstructionSet` fills with `invalid_instruction`
#: (`0x0010B1C0`). They are absent from the opcode maps by construction.
#: `0x00` being among them is what makes a run of trailing zero padding trap
#: rather than execute, which is the basis of the Req 3.7 padding rule.
INVALID_OPCODES = frozenset(
    {0x00, 0x1C, 0x1D, 0x96, 0x9E, 0x9F}
    | set(range(0x2F, 0x3C))
    | set(range(0x3E, 0x41))
    | set(range(0x4E, 0x5A))
    | set(range(0xA8, 0xAF))
    | set(range(0xB9, 0xC8))
    | set(range(0xCD, 0xDC))
    | set(range(0xE1, 0xE6))
    | set(range(0xE8, 0xFE))
)

#: Narrow/wide twins, `by_` form -> wide form. Every pair was measured and in
#: every case the `by_` form replaces each 4-byte operand with a 1-byte one and
#: nothing else changes. Wide opcodes with no narrow twin are simply absent.
BY_PAIRS = {
    0x04: 0x05, 0x06: 0x07, 0x08: 0x09, 0x0D: 0x0E, 0x0F: 0x10,
    0x17: 0x16, 0x18: 0x11, 0x19: 0x12, 0x1A: 0x13, 0x1B: 0x14,
    0x20: 0x21, 0x22: 0x23, 0x24: 0x25, 0x26: 0x27, 0x28: 0x29,
    0x2A: 0x2B, 0x2C: 0x2D, 0x47: 0x48,
    0x5E: 0x64, 0x5F: 0x65, 0x60: 0x66, 0x61: 0x67, 0x62: 0x68, 0x63: 0x69,
    0x77: 0x6A, 0x78: 0x6B, 0x79: 0x6C, 0x7A: 0x6D, 0x7B: 0x6E, 0x7C: 0x6F,
    0x7D: 0x70, 0x7E: 0x71, 0x7F: 0x72, 0x80: 0x73, 0x81: 0x74, 0xE6: 0x75,
    0x88: 0x87, 0x8A: 0x89, 0x8C: 0x8B, 0xE7: 0x76,
    0xA0: 0xA1, 0xA2: 0xA3, 0xA4: 0xA5, 0xA6: 0xA7,
}

#: Task 6.3. The five byte values seen in prototype scripts and in no retail
#: script, classified. `0x97` is the only one that is a real opcode, and the
#: corpus evidence alone cannot say whether its occurrences are opcodes or
#: operand bytes — that needs the in-phase decode of task 7.7.
PROTOTYPE_ONLY_BYTES = {
    0x97: (
        "ambiguous",
        "real opcode r_deletearray (handler 0x001147B0, length 2); whether the "
        "prototype-only occurrences sit at instruction boundaries is not yet "
        "settled and needs the task 7.7 corpus decode",
    ),
    0xBC: ("operand", "invalid_instruction slot, cannot be an opcode"),
    0xEB: ("operand", "invalid_instruction slot, cannot be an opcode"),
    0xF1: ("operand", "invalid_instruction slot, cannot be an opcode"),
    0xF2: ("operand", "invalid_instruction slot, cannot be an opcode"),
}

# ---------------------------------------------------------------------------
# Script API index map (Phase 1, moved here per task 6.1)
# ---------------------------------------------------------------------------

#: `CScriptApi::CallApiFn` (`0x0011A780`) bounds-checks with
#: `sltiu $v0, $s0, 254` and scales by 12 into an in-object array built at
#: runtime by `CScriptApi::InitApiFnVTable` from 16-byte pointer-to-member
#: templates at `0x001E1B08`, function pointer at +8.
API_SLOT_COUNT = 254
API_SLOT_STRIDE = 12
CALL_API_FN_VADDR = 0x0011A780
API_BOUNDS_CHECK_VADDR = 0x0011A798
PTMF_TEMPLATE_BASE = 0x001E1B08
PTMF_TEMPLATE_STRIDE = 16
PTMF_FN_OFFSET = 8
#: Inline argument block: `this+0xBF0 + 4*n`, bounded above by the completion
#: flag `CallApiFn` writes to `this+0xC7C`.
API_ARG_BLOCK_BASE = 0xBF0
API_ARG_BLOCK_END = 0xC7C

ApiSlot = namedtuple(
    "ApiSlot", ("index", "name", "handler", "arity", "arg_widths", "tail_call")
)

#: `arg_widths` is one character per inline argument slot, giving the width in
#: bytes of the widest load the thunk makes from that slot; `0` means the slot
#: is inside the arity but no direct load of it was observed (the thunk copies
#: the block wholesale, or reaches it through a path the tracker does not
#: follow). `tail_call` is the first non-`CScriptApi` function the thunk calls,
#: which is what actually names the behaviour. Every thunk is a
#: `..._10CScriptApiFv` member; the names below are the demangled short names.
_API_SPEC = (
    (0, "SetLiquidSplashHeight", 0x00186380, 1, "4", "SetLiquidSplashHeight"),
    (1, "AddChild", 0x001157B0, 1, "4", "GetEngineObjectLink"),
    (2, "SetDualSide", 0x001161B0, 1, "4", "GetEngineObjectLink"),
    (3, "ResetFreeze", 0x001168A0, 1, "4", "GetEngineObjectLink"),
    (4, "EnableDynamicLight", 0x00123730, 1, "4", "GetEngineObjectLink"),
    (5, "CheckAnimating", 0x00120020, 0, "", ""),
    (6, "SaveSpawnedApparitionData", 0x0011D2B0, 3, "441", "CdsAssert"),
    (7, "SetBoundingBoxRadius", 0x00116A00, 1, "4", "GetEngineObjectLink"),
    (8, "Cos", 0x0011ED50, 1, "4", "FastMath_Cos360"),
    (9, "CreateTimer", 0x001156A0, 1, "4", "CreateTimer"),
    (10, "Distance", 0x0011F1A0, 6, "444444", "FastMath_Sqrt"),
    (11, "DistanceSquare", 0x0011F200, 6, "444444", ""),
    (12, "FullAmbientLight", 0x00123690, 2, "44", "GetEngineObjectLink"),
    (13, "Fabs", 0x0011ECC0, 1, "4", "FastMath_Fabs"),
    (14, "GetClassID", 0x00121230, 1, "4", "GetEntityID"),
    (15, "GetGroundPosition", 0x00121280, 1, "4", "GetGroundPos"),
    (16, "GetOrientationAngles", 0x00120CC0, 1, "4", "GetEngineObjectLink"),
    (17, "SetOrientationAngles", 0x00120DD0, 4, "4444", "GetEngineObjectLink"),
    (18, "GetOrientationY", 0x00121090, 1, "4", "GetEngineObjectLink"),
    (19, "SetOrientationY", 0x00121010, 2, "44", "GetEngineObjectLink"),
    (20, "GetForwardVector", 0x00120F10, 1, "4", "GetForwardVector"),
    (21, "SetForwardVector", 0x00120E60, 4, "4444", "GetEngineObjectLink"),
    (22, "GetPosition", 0x001209F0, 1, "4", "GetEngineObjectLink"),
    (23, "IsFreezing", 0x00116680, 1, "4", "GetEngineObjectLink"),
    (24, "GetState", 0x00115770, 0, "", "GetState"),
    (25, "GetVelocity", 0x00121150, 1, "4", "GetEngineObjectLink"),
    (26, "EnablePause", 0x00116630, 1, "4", "GetEngineObjectLink"),
    (27, "IsSequenceComplete", 0x001191F0, 0, "", ""),
    (28, "Kill", 0x001154B0, 1, "4", "Kill"),
    (29, "KillTimer", 0x001156E0, 1, "4", "DestroyTimer"),
    (30, "LookAt", 0x00115BB0, 4, "4444", "CdsAssert"),
    (31, "PlaySoundEffect", 0x00122B10, 2, "44", "GetEngineObjectLink"),
    (32, "RandomInt", 0x0011EE20, 0, "", "rand"),
    (33, "RandomIntRange", 0x0011EE80, 2, "44", "CdsAssert"),
    (34, "RandomFloat", 0x0011EF30, 0, "", "rand"),
    (35, "RandomFloatRange", 0x0011EF80, 2, "44", "rand"),
    (36, "RandomVector", 0x0011F010, 0, "", "rand"),
    (37, "RandomVectorRange", 0x0011F0B0, 6, "444444", "rand"),
    (38, "GetGameFps", 0x00120040, 0, "", ""),
    (39, "RemoveChild", 0x00115860, 1, "4", "GetEngineObjectLink"),
    (40, "CheckLanding", 0x0011EC30, 0, "", "GetEngineObjectLink"),
    (41, "SeqPlay", 0x00117B70, 3, "444", ""),
    (42, "SetBounce", 0x0011E210, 1, "4", "GetEngineObjectLink"),
    (43, "SetMass", 0x0011E1B0, 1, "4", "GetEngineObjectLink"),
    (44, "SetPhysicsModel", 0x0011E280, 1, "4", "GetEngineObjectLink"),
    (45, "SetPosition", 0x00120960, 4, "4444", "GetEngineObjectLink"),
    (46, "SetState", 0x00115740, 1, "4", "SetState"),
    (47, "SetVelocity", 0x0011E080, 4, "4444", "GetEngineObjectLink"),
    (48, "Sin", 0x0011ED20, 1, "4", "FastMath_Sin360"),
    (49, "Spawn", 0x001154F0, 1, "4", "Spawn"),
    (50, "Sqrt", 0x0011ECF0, 1, "4", "FastMath_Sqrt"),
    (51, "Tan", 0x0011ED80, 1, "4", "tanf"),
    (52, "VectorCrossProduct", 0x0011F2D0, 6, "444444", ""),
    (53, "VectorDotProduct", 0x0011F240, 6, "444444", ""),
    (54, "VectorLength", 0x0011F280, 3, "444", "FastMath_Sqrt"),
    (55, "VectorNormalize", 0x0011F320, 3, "444", "FastMath_Sqrt"),
    (56, "VectorReflect", 0x0011F3D0, 6, "444444", ""),
    (57, "VectorRotate", 0x0011F5B0, 0, "", "CdsAssert"),
    (58, "SetCursedHand", 0x001198F0, 1, "4", "GetEngineObjectLink"),
    (59, "FindAnimatedGroundPos", 0x001214F0, 0, "", "GetEngineObjectLink"),
    (60, "CheckPlayerFullHealth", 0x00119BB0, 0, "", ""),
    (61, "ChangeMaximo", 0x00119BF0, 1, "4", "SetMaximo"),
    (62, "PlayerSetStartPosition", 0x00119C50, 8, "44404004", "Round180"),
    (63, "AnimateLand", 0x00116A70, 3, "444", "SetWorldAnim"),
    (64, "DebugBreak", 0x0011D010, 0, "", ""),
    (65, "DisplaySubLevelName", 0x0011FD50, 1, "4", ""),
    (66, "CheckSubLevelCompleted", 0x0011F860, 1, "4", "CheckSubLevelComplete"),
    (67, "IsFacing", 0x00122680, 3, "444", "GetEngineObjectLink"),
    (68, "TriggerAnimFrame", 0x00117880, 1, "4", "GetEngineObjectLink"),
    (69, "SetBoneMesh", 0x00116460, 2, "44", "CdsAssert"),
    (70, "SetPlayerEffect", 0x0011A070, 4, "4444", "CdsAssert"),
    (71, "IsPlayerProjectileComing", 0x00119F90, 0, "", "GetEngineObjectLink"),
    (72, "IsSpawnedObject", 0x00115FF0, 0, "", "IsEditedInstance"),
    (73, "GetNextHubCount", 0x0011F680, 1, "4", "GetNextHubCount"),
    (74, "TransportHubLevel", 0x0011F6C0, 1, "4", "TransportHubLevel"),
    (75, "AnimateLandFrame", 0x00116D90, 3, "444", "CdsAssert"),
    (76, "ResetAnimateLand", 0x00116D60, 1, "4", "RecurseResetAnimByID"),
    (77, "IsPlayerOnCursedLand", 0x00119B70, 0, "", ""),
    (78, "IsPlayerDefending", 0x00119D60, 0, "", ""),
    (79, "GetAngleYToTarget", 0x00121620, 1, "4", "GetEngineObjectLink"),
    (80, "RotateTo", 0x0011E370, 3, "444", "GetEngineObjectLink"),
    (81, "SetScaledShadow", 0x00122190, 1, "4", "GetEngineObjectLink"),
    (82, "MoveTo", 0x0011E7E0, 6, "444444", "GetEngineObjectLink"),
    (83, "GetBonePosition", 0x00122270, 2, "44", "GetBonePosition"),
    (84, "FindApparitions", 0x00115A60, 4, "4444", "GetEngineObjectLink"),
    (85, "EnableCollisions", 0x00115D10, 3, "444", "CdsAssert"),
    (86, "RemoveAllChildren", 0x00115B50, 0, "", "GetEngineObjectLink"),
    (87, "GetAngleYToPoint", 0x00121C50, 4, "4404", "CdsAssert"),
    (88, "SetCollisionRadius", 0x00115F30, 1, "4", "GetEngineObjectLink"),
    (89, "SetFreeze", 0x00116720, 2, "44", "GetEngineObjectLink"),
    (90, "GetRelAngleYToTarget", 0x00121860, 1, "4", "GetEngineObjectLink"),
    (91, "GetRelAngleYToPoint", 0x00121F50, 4, "4404", "CdsAssert"),
    (92, "SaveApparitionData", 0x0011D160, 2, "41", "IsEditedInstance"),
    (93, "RestoreApparitionData", 0x0011D390, 1, "4", "IsEditedInstance"),
    (94, "EnterSubLevel", 0x0011F7F0, 1, "4", "mglEnterSubLevel"),
    (95, "IsPlayerOnAnimID", 0x00119F20, 2, "44", "OnAnim"),
    (96, "GetTime", 0x00123100, 0, "", ""),
    (97, "VectorRotateY", 0x0011F5E0, 4, "4444", "sinf"),
    (98, "CountSubLevelCompleted", 0x0011F8A0, 0, "", "CountSubLevelComplete"),
    (99, "GetDistance", 0x00120AD0, 1, "4", "GetEngineObjectLink"),
    (100, "GetCameraPosition", 0x00120250, 0, "", ""),
    (101, "IsApparitionAlive", 0x00122330, 1, "4", "IsApparitionSuspended"),
    (102, "TerminateMoveTo", 0x0011E640, 0, "", ""),
    (103, "CheckInScreen", 0x001227F0, 1, "4", "GetEngineObjectLink"),
    (104, "EnableHeadTracking", 0x00115ED0, 2, "44", "EnableHeadTracking"),
    (105, "GameVarAdd", 0x0011F6F0, 2, "44", "GameVarAdd"),
    (106, "GameVarSub", 0x0011F720, 2, "44", "GameVarSub"),
    (107, "GameVarRead", 0x0011F750, 1, "4", ""),
    (108, "GameVarSet", 0x0011F7C0, 2, "44", "GameVarSet"),
    (109, "SetRadiusHeight", 0x00115F90, 1, "4", "GetEngineObjectLink"),
    (110, "GetGroundPositionXYZ", 0x001213F0, 3, "444", "GetEngineObjectLink"),
    (111, "PlayerHit", 0x00119DA0, 2, "44", "GetEngineObjectLink"),
    (112, "EnableTrackedByPlayer", 0x00115F00, 1, "4", "EnableTrackedByPlayer"),
    (113, "GotoNextLevel", 0x0011F840, 1, "4", "mglGotoNextLevel"),
    (114, "PlayerGetAction", 0x00119E90, 0, "", ""),
    (115, "CameraSetTriggerMode", 0x00120050, 18, "444444444444444444", "SetTriggerMode"),
    (116, "GetDistanceSquared", 0x00120BD0, 1, "4", "GetEngineObjectLink"),
    (117, "CreatePointLight", 0x00123120, 8, "44444444", "GetEngineObjectLink"),
    (118, "CreateDirectionLight", 0x001231C0, 6, "444444", "GetEngineObjectLink"),
    (119, "DestroyLight", 0x00123240, 1, "4", "GetEngineObjectLink"),
    (120, "EnableLight", 0x00123280, 2, "44", "GetEngineObjectLink"),
    (121, "SetLightColour", 0x001232D0, 4, "4444", "GetEngineObjectLink"),
    (122, "SetLightPosition", 0x00123330, 4, "4444", "GetEngineObjectLink"),
    (123, "SetLightRange", 0x00123390, 2, "44", "GetEngineObjectLink"),
    (124, "SetLightIntensity", 0x001233E0, 2, "44", "GetEngineObjectLink"),
    (125, "GetLightPosition", 0x00123430, 1, "4", "GetLightPosition"),
    (126, "GetLightRange", 0x00123480, 1, "4", "GetLightRange"),
    (127, "GetLightIntensity", 0x001234C0, 1, "4", "GetLightIntensity"),
    (128, "SetLightDirection", 0x00123500, 4, "4444", "GetEngineObjectLink"),
    (129, "GetLightDirection", 0x00123560, 1, "4", "GetLightDirection"),
    (130, "AttachLightToMeshNode", 0x001235B0, 1, "4", "GetEngineObjectLink"),
    (131, "DetachLightFromMeshNode", 0x00123620, 1, "4", "GetEngineObjectLink"),
    (132, "PlayerOnObject", 0x00119EB0, 0, "", "GetEngineObjectLink"),
    (133, "PointInBoundBox", 0x00122380, 9, "444444444", ""),
    (134, "SetGravity", 0x0011E130, 2, "44", "GetEngineObjectLink"),
    (135, "DisableSkeletonNode", 0x001162A0, 2, "44", "CdsAssert"),
    (136, "SetSleepMode", 0x00115390, 1, "4", "GetEngineObjectLink"),
    (137, "GetCameraOrientation", 0x00120280, 0, "", ""),
    (138, "CameraShakeScreen", 0x00120130, 3, "444", "CameraShakeScreen"),
    (139, "GetLandType", 0x00120880, 1, "4", "GetEngineObjectLink"),
    (140, "CameraCutScene", 0x001203E0, 0, "", ""),
    (141, "CreateSnow", 0x001842C0, 0, "", "CdsAssert"),
    (142, "DestroySnow", 0x00184320, 0, "", "CdsAssert"),
    (143, "SetSnowDensity", 0x00184380, 2, "44", "CdsAssert"),
    (144, "SetSnowWindIntensity", 0x001843F0, 1, "4", "CdsAssert"),
    (145, "CreateRain", 0x00184450, 0, "", "CdsAssert"),
    (146, "DestroyRain", 0x001844B0, 0, "", "CdsAssert"),
    (147, "SetRainDensity", 0x00184510, 2, "44", "CdsAssert"),
    (148, "CreateFogVolume", 0x00184580, 12, "444444444444", "CdsAssert"),
    (149, "DestroyParticleSystem", 0x001846A0, 1, "4", "GetEngineObjectLink"),
    (150, "AttachParticleSystem", 0x00184770, 2, "44", "GetEngineObjectLink"),
    (151, "DetachParticleSystem", 0x00184840, 1, "4", "GetEngineObjectLink"),
    (152, "FadeAndKillParticleSystem", 0x00184910, 2, "44", "GetEngineObjectLink"),
    (153, "CreateFire", 0x001849F0, 11, "44444444444", "CdsAssert"),
    (154, "ArcTan2", 0x0011EDC0, 2, "44", "atan2f"),
    (155, "GetEntityLoadClass", 0x001160B0, 1, "4", "GetLoadClass"),
    (156, "GetEntityLoadClassValue", 0x001160F0, 1, "4", "GetLoadValue"),
    (157, "GetLoadClassTotal", 0x00116130, 1, "4", "GetLoadClassCurLoad"),
    (158, "GetLoadClassLimit", 0x00116170, 1, "4", "GetLoadClassLimit"),
    (159, "GetBGAnimPath", 0x001177D0, 1, "4", "FindAnimNode"),
    (160, "GetYAngleBetweenPoints", 0x00122610, 6, "404404", "atan2f"),
    (161, "TerminateRotateTo", 0x0011E420, 0, "", "GetEngineObjectLink"),
    (162, "DebugValue", 0x0011E070, 0, "", ""),
    (163, "CheckWallHit", 0x00122890, 2, "44", "GetEngineObjectLink"),
    (164, "KillAllTimers", 0x00115710, 0, "", "KillAllApparitionTimers"),
    (165, "IsSubLevelCompleted", 0x0011F8E0, 1, "4", "IsSubLevelCompleted"),
    (166, "StartParticleSystem", 0x00184B10, 1, "4", "CdsAssert"),
    (167, "StopParticleSystem", 0x00184B70, 1, "4", "CdsAssert"),
    (168, "CreateFlame", 0x00184BD0, 13, "4444444444444", "CdsAssert"),
    (169, "SetSurfaceMask", 0x0011E2F0, 1, "4", "GetEngineObjectLink"),
    (170, "GetRelAngleYToPointFromPoint", 0x00121A40, 7, "4044044", "atan2f"),
    (171, "TriggerBGAnimFrame", 0x00117940, 2, "44", "CdsAssert"),
    (172, "FlashBossMeter", 0x0011FFE0, 1, "4", "FlashBossMeter"),
    (173, "IsPlayerAttacking", 0x00119D80, 0, "", ""),
    (174, "GetAbility", 0x0011F920, 0, "", "GetAbility"),
    (175, "SetAbility", 0x0011F960, 1, "4", "SetAbility"),
    (176, "SpawnAbility", 0x0011F990, 3, "444", "CdsAssert"),
    (177, "DebugPrintFloat", 0x00187EB0, 2, "44", "fptodp"),
    (178, "DebugPrintInt", 0x00187F10, 2, "44", "sprintf"),
    (179, "DebugPrintBool", 0x00187FE0, 2, "44", "sprintf"),
    (180, "CutCamera_FixedLookAt", 0x001202B0, 0, "", ""),
    (181, "CutCamera_MoveTo", 0x001202C0, 0, "", ""),
    (182, "CutCamera_RotateTo", 0x001203C0, 0, "", ""),
    (183, "CutCamera_Zoom", 0x001203D0, 0, "", ""),
    (184, "CutScene_Start", 0x00120300, 1, "4", "CdsAssert"),
    (185, "CutScene_Stop", 0x00120360, 0, "", "CdsAssert"),
    (186, "DisplayAccessoryString", 0x0011FD10, 1, "4", "InterfaceSet"),
    (187, "GetPlayerBits", 0x00119CF0, 0, "", ""),
    (188, "CutCamera_Spline", 0x001202E0, 0, "", ""),
    (189, "CutCamera_SplinePoint", 0x001202F0, 0, "", ""),
    (190, "SetPlayerExtFunction", 0x00119D10, 2, "44", "GetEngineObjectLink"),
    (191, "CutCamera_Pan", 0x001202D0, 0, "", ""),
    (192, "GetAnimFrameCount", 0x001172C0, 0, "", "GetEngineObjectLink"),
    (193, "AnimLoopTrigger", 0x00117330, 0, "", "GetEngineObjectLink"),
    (194, "SetLightningDensity", 0x0018F550, 1, "4", "CdsAssert"),
    (195, "StopSoundEffect", 0x00122BC0, 1, "4", "StopEffect"),
    (196, "CreateSmoke", 0x00184CD0, 21, "444444444444444444444", "CdsAssert"),
    (197, "CreateDust", 0x00184E40, 16, "4444444444444444", "CdsAssert"),
    (198, "CreateDryIce", 0x00184F70, 15, "444444444444444", "CdsAssert"),
    (199, "CreateEntityZone", 0x00116220, 7, "4444444", "CreateNewZone"),
    (200, "CreateGlow", 0x001850A0, 10, "4444444444", "CdsAssert"),
    (201, "CheckPlayerPowerup", 0x0011A110, 3, "444", "GetEngineObjectLink"),
    (202, "GetEntity", 0x00116670, 0, "", ""),
    (203, "AnimateLandTo", 0x00116E10, 3, "444", "CdsAssert"),
    (204, "SetMeshEffect", 0x0018F620, 2, "44", "GetResource"),
    (205, "GetMaximoType", 0x00119F70, 0, "", ""),
    (206, "GetStabCount", 0x00119FF0, 0, "", "GetStabCount"),
    (207, "SetAnimateLand", 0x00116AB0, 6, "444444", "GetEngineObjectLink"),
    (208, "SetAnimateLandSound", 0x00116CD0, 2, "44", "FindAnimTrigger"),
    (209, "SetAnimateLandShake", 0x00116D10, 4, "4444", "FindAnimTrigger"),
    (210, "SetOnAnimateLand", 0x00116BF0, 3, "444", "AddTrigger"),
    (211, "CreateExplosion", 0x001851C0, 21, "444444444444444444444", "CdsAssert"),
    (212, "EnableAllOnPlayerHit", 0x0011A030, 1, "4", "GetEngineObjectLink"),
    (213, "Camera_SetBossPosition", 0x001203F0, 3, "444", "SetBossPosition"),
    (214, "CreateFireFlies", 0x00185340, 17, "44444444444444444", "CdsAssert"),
    (215, "SetParticleSystemTexture", 0x001854A0, 2, "44", "CdsAssert"),
    (216, "CreateSmokeTrail", 0x00185500, 22, "4444444444444444444444", "CdsAssert"),
    (217, "ResetSkeletonNode", 0x001163F0, 0, "", "GetEngineObjectLink"),
    (218, "CameraShakeScreenFromTable", 0x001201A0, 1, "4", "CameraShakeScreen"),
    (219, "DebugPrintUInt", 0x00187FA0, 2, "44", "sprintf"),
    (220, "CreateSwirl", 0x00185670, 21, "444444444444444444444", "CdsAssert"),
    (221, "GetLevelID", 0x00120010, 0, "", ""),
    (222, "SetParticleSystemBlendMode", 0x001857E0, 2, "44", "CdsAssert"),
    (223, "SetShadow", 0x00122200, 2, "44", "GetEngineObjectLink"),
    (224, "AttachParticleSystemToLandMeshNode", 0x00185860, 2, "44", "CdsAssert"),
    (225, "SetParticleSystemAutoStart", 0x00185900, 4, "4444", "CdsAssert"),
    (226, "SetEventHandlerReturnValue", 0x00116600, 1, "4", "SetEventHandlerReturnValue"),
    (227, "CreateGroundDust", 0x00185990, 4, "4444", "GetEngineObjectLink"),
    (228, "DebugPrintConsoleInt1", 0x00187F50, 2, "44", "printf"),
    (229, "SetCurrentRenderPass", 0x0018F5B0, 1, "4", "GetEngineObjectLink"),
    (230, "CameraSetBossRadius", 0x00120450, 5, "44444", "SetBossRadius"),
    (231, "SetScale", 0x00116940, 2, "44", "SetScale"),
    (232, "CameraSetBossFlag", 0x00120500, 1, "4", "SetBossFlag"),
    (233, "CameraClearBossFlag", 0x00120530, 1, "4", "ClearBossFlag"),
    (234, "SetParticleSystemPosition", 0x00185D40, 7, "4444444", "CdsAssert"),
    (235, "CreateFireSpray", 0x00185DE0, 21, "444444444444444444444", "CdsAssert"),
    (236, "GetLiquid", 0x00185F60, 0, "", "GetEngineObjectLink"),
    (237, "SetLiquidTexture", 0x00186000, 3, "444", "CdsAssert"),
    (238, "SetLiquidColor", 0x001860D0, 6, "444444", "CdsAssert"),
    (239, "GetGameRate", 0x00122870, 0, "", ""),
    (240, "CameraSetTriggerModeHeight", 0x00120560, 1, "4", ""),
    (241, "CreateElectricity", 0x001861D0, 25, "4444444444444444444444444", "CdsAssert"),
    (242, "SetAmbientLightColor", 0x001237A0, 3, "444", ""),
    (243, "UseStaticLights", 0x001237D0, 1, "4", ""),
    (244, "LineOfSight", 0x001229A0, 6, "444444", "sceVu0CopyVector"),
    (245, "EnableWorldDynamicLights", 0x001237F0, 1, "4", ""),
    (246, "PointInRotatedBoundBox", 0x00122470, 10, "4444444444", "sinf"),
    (247, "CutScenePlaying", 0x00120580, 0, "", "CdsAssert"),
    (248, "IsLandAnimate", 0x00117A10, 1, "4", "CdsAssert"),
    (249, "KeepCamOutOfSphere", 0x001205E0, 4, "4444", "GetWorldPos"),
    (250, "CameraSetTriggerFlags", 0x001206D0, 1, "4", "SetFlags"),
    (251, "CutScene_SetupKingCamera", 0x00120700, 9, "444444444", "CdsAssert"),
    (252, "Camera_SetSourceAndTarget", 0x001207C0, 6, "444444", "SetCameraPosAndTarget"),
    (253, "MakePlayerInvisible", 0x0011A400, 0, "", "SetInvisible"),
)

API_SLOTS = tuple(ApiSlot(*row) for row in _API_SPEC)
API_BY_INDEX = {slot.index: slot for slot in API_SLOTS}

#: Mangled-name suffix every slot's target carries.
CSCRIPTAPI_MANGLED_SUFFIX = "__10CScriptApiFv"

#: Anchors (Req 1.5).
GAMEVARSUB_INDEX = 106
SETABILITY_INDEX = 175

#: Slots whose real work happens behind an indirect `jalr`, so the recovered
#: arity describes only the visible prologue. Carried forward from task 2.2.
API_INDIRECT_SLOTS = frozenset({58, 68, 69, 135, 171, 192, 193, 217, 224, 227})

#: Slots whose arity the tracker reports but which are NOT trusted:
#: 62 `PlayerSetStartPosition` reports 8 with gaps and is probably 4
#: (`w f f f`); 170 `GetRelAngleYToPointFromPoint` reports 7 with gaps for a
#: 524-byte function. Both need a hand decode.
API_ARITY_UNCERTAIN = frozenset({62, 170})


def api_slot(index):
    """Return the `ApiSlot` for `index`, or `None` if out of range/unmapped."""
    return API_BY_INDEX.get(index)


__all__ = [
    "OpcodeEntry", "OPCODES_BASE", "OPCODES_PROTOTYPE", "OPCODES_RETAIL",
    "PROTOTYPE_OVERRIDES", "RETAIL_OVERRIDES", "opcode_map",
    "KIND_WIDTHS", "OPERAND_KINDS", "CONFIDENCE_LEVELS", "REVISIONS",
    "REVISION_PROTOTYPE", "REVISION_RETAIL", "INVALID_OPCODES", "BY_PAIRS",
    "PROTOTYPE_ONLY_BYTES", "SIGN_EXTENDED_U8", "API_INDEX_OPERAND_NAME",
    "ApiSlot", "API_SLOTS", "API_BY_INDEX", "api_slot", "API_SLOT_COUNT",
    "API_SLOT_STRIDE", "API_INDIRECT_SLOTS", "API_ARITY_UNCERTAIN",
    "GAMEVARSUB_INDEX", "SETABILITY_INDEX", "CSCRIPTAPI_MANGLED_SUFFIX",
]


# ---------------------------------------------------------------------------
# Phase 4 — the disassembler (tasks 7.1, 7.3, 7.5, 7.6)
#
# Read-only. Nothing below writes a file, and `disassemble_bef_type` reuses
# `randomizer.bef.BefFile` for BEF parsing rather than reimplementing the blob
# layout.
#
# The decode is a plain linear walk from offset 0: fetch one opcode byte, look
# it up in the map for the EXPLICITLY supplied revision, consume exactly the
# operand bytes that entry declares. That is correct because the interpreter
# advances its IP by exactly 1 for the opcode and each handler consumes its own
# operands with a constant IP delta measured outside every loop in its body, so
# the encoding is fixed length throughout.
# ---------------------------------------------------------------------------

#: Pseudo-mnemonics the decoder emits for things that are not instructions.
PAD_MNEMONIC = ".pad"
DATUM_MNEMONIC = ".byte"

#: Reasons recorded in a `DecodeFailure`.
FAILURE_UNKNOWN_OPCODE = "unknown_opcode"
FAILURE_TRUNCATED = "truncated"

#: An `Instruction`'s `operands` is a tuple of these.
Operand = namedtuple("Operand", ("name", "kind", "value"))

Instruction = namedtuple(
    "Instruction", ("offset", "opcode", "raw", "mnemonic", "operands", "annotation")
)

DecodeFailure = namedtuple("DecodeFailure", ("offset", "opcode", "reason"))

Disassembly = namedtuple(
    "Disassembly",
    ("revision", "length", "instructions", "failures", "consumed",
     "coverage", "clean"),
)

_SIGNED_KINDS = frozenset({"i32", "rel8", "rel32"})


def _decode_operand_value(raw, kind):
    signed = kind in _SIGNED_KINDS
    return int.from_bytes(raw, "little", signed=signed)


def _encode_operand_value(value, kind):
    width = KIND_WIDTHS[kind]
    signed = kind in _SIGNED_KINDS
    return int(value).to_bytes(width, "little", signed=signed)


def annotate_api_operand(index):
    """Return a terse annotation string for a script API slot index.

    Never raises. An index outside 0-253 is reported out of range; an in-range
    index with no entry is reported unmapped. The recovered map is total over
    0-253, so `unmapped` is currently unreachable in practice — it is
    implemented anyway so a future partial map cannot crash a listing.
    """
    if not isinstance(index, int) or index < 0 or index >= API_SLOT_COUNT:
        return f"api[{index}] out of range (valid 0-{API_SLOT_COUNT - 1})"
    slot = api_slot(index)
    if slot is None:
        return f"api[{index}] unmapped"
    text = f"CScriptApi::{slot.name} argc={slot.arity}"
    notes = []
    if index in API_ARITY_UNCERTAIN:
        notes.append("argc uncertain")
    if index in API_INDIRECT_SLOTS:
        notes.append("indirect target")
    if slot.tail_call:
        notes.append(f"-> {slot.tail_call}")
    if notes:
        text += " (" + "; ".join(notes) + ")"
    return text


def _annotation_for(entry, operands):
    """Annotate an instruction that names a script API slot, else `None`.

    Keys on the operand NAME as well as the `apiidx` kind: `callapi` (0x8B)
    encodes the slot as a 4-byte `apiidx`, but `by_callapi` (0x8C) narrows it
    to one byte spelled `u8`, so kind alone would miss every `by_callapi`.
    """
    for operand in operands:
        if operand.kind == "apiidx" or operand.name == API_INDEX_OPERAND_NAME:
            return annotate_api_operand(operand.value)
    return None


def disassemble(section, revision):
    """Linearly decode a Script_Section. NEVER raises.

    `revision` is required and is never inferred from the bytes.

    Returns a `Disassembly` with the instruction records, the list of
    `DecodeFailure`s, the byte count consumed, the Decode_Coverage as a
    fraction of the section length, and whether the decode was a Clean_Decode.

    Recovery rules:
      * unrecognised opcode -> emit a `.byte` datum, record a failure, resume
        at the FOLLOWING byte;
      * declared length overrunning the section end -> record a failure at that
        offset and STOP this section;
      * a run of zeros extending to the section end -> one `.pad`
        pseudo-instruction, no failure, still a Clean_Decode. Only a run that
        reaches the end counts: `0x00` is an invalid opcode, so a zero run in
        the MIDDLE of a section is a genuine decode failure, not padding.
    """
    table = opcode_map(revision)
    data = bytes(section)
    total = len(data)

    instructions = []
    failures = []
    consumed = 0
    offset = 0
    while offset < total:
        opcode = data[offset]
        if opcode == 0x00 and data[offset:] == b"\x00" * (total - offset):
            raw = data[offset:]
            instructions.append(Instruction(
                offset=offset, opcode=None, raw=raw, mnemonic=PAD_MNEMONIC,
                operands=(Operand("size", "u32", len(raw)),), annotation=None,
            ))
            consumed += len(raw)
            offset = total
            break

        entry = table.get(opcode)
        if entry is None:
            instructions.append(Instruction(
                offset=offset, opcode=opcode, raw=data[offset:offset + 1],
                mnemonic=DATUM_MNEMONIC, operands=(), annotation="unrecognised opcode",
            ))
            failures.append(DecodeFailure(offset, opcode, FAILURE_UNKNOWN_OPCODE))
            offset += 1
            continue

        if offset + entry.length > total:
            failures.append(DecodeFailure(offset, opcode, FAILURE_TRUNCATED))
            break

        raw = data[offset:offset + entry.length]
        cursor = offset + OPCODE_BYTE_LENGTH
        operands = []
        for name, kind in entry.operands:
            width = KIND_WIDTHS[kind]
            operands.append(
                Operand(name, kind, _decode_operand_value(data[cursor:cursor + width], kind))
            )
            cursor += width
        operands = tuple(operands)
        instructions.append(Instruction(
            offset=offset, opcode=opcode, raw=raw, mnemonic=entry.mnemonic,
            operands=operands, annotation=_annotation_for(entry, operands),
        ))
        consumed += entry.length
        offset += entry.length

    coverage = 1.0 if total == 0 else consumed / total
    clean = not failures and consumed == total
    return Disassembly(
        revision=revision, length=total, instructions=tuple(instructions),
        failures=tuple(failures), consumed=consumed, coverage=coverage, clean=clean,
    )


def _format_operand(operand):
    if operand.kind in ("rel8", "rel32"):
        sign = "+" if operand.value >= 0 else "-"
        return f"{operand.name}={sign}0x{abs(operand.value):X}"
    if operand.kind == "i32" and operand.value < 0:
        return f"{operand.name}=-0x{-operand.value:X}"
    return f"{operand.name}=0x{operand.value:X}"


def format_instruction(instruction, raw_width=8):
    raw = instruction.raw[:raw_width].hex(" ")
    if len(instruction.raw) > raw_width:
        raw += " ..."
    text = f"{instruction.offset:08X}  {raw:<{raw_width * 3 + 4}}  {instruction.mnemonic}"
    if instruction.operands:
        text += " " + ", ".join(_format_operand(o) for o in instruction.operands)
    if instruction.annotation:
        text += f"   ; {instruction.annotation}"
    return text.rstrip()


def format_listing(disassembly, header=None):
    """Render a `Disassembly` as text, one instruction per line."""
    lines = []
    if header:
        lines.append(f"; {header}")
    lines.append(
        f"; revision={disassembly.revision} length={disassembly.length} "
        f"coverage={disassembly.coverage * 100:.2f}% "
        f"clean={'yes' if disassembly.clean else 'no'}"
    )
    lines.extend(format_instruction(i) for i in disassembly.instructions)
    for failure in disassembly.failures:
        opcode = "--" if failure.opcode is None else f"{failure.opcode:02X}"
        lines.append(
            f"; decode failure at {failure.offset:08X}: {failure.reason} (0x{opcode})"
        )
    return "\n".join(lines)


def encode(instructions):
    """Re-encode decoded `Instruction` records back to bytes.

    For every Clean_Decode section this MUST reproduce the original bytes
    exactly (VP2). Re-encoding from the decoded operand VALUES rather than from
    `raw` is the point: clean coverage alone can be reached by a wrong length
    that happens to stay in phase, but a byte-exact round trip through the
    operand layout cannot.
    """
    out = bytearray()
    for instruction in instructions:
        if instruction.mnemonic == PAD_MNEMONIC:
            out += b"\x00" * int(instruction.operands[0].value)
            continue
        if instruction.mnemonic == DATUM_MNEMONIC:
            out.append(instruction.opcode)
            continue
        out.append(instruction.opcode)
        for operand in instruction.operands:
            out += _encode_operand_value(operand.value, operand.kind)
    return bytes(out)


# ---------------------------------------------------------------------------
# BEF integration
# ---------------------------------------------------------------------------

#: Script_Section span inside a BEF_Blob: `blob[u32@0x38 : u32@0x0C]`.
BLOB_PAYLOAD_LENGTH_OFFSET = 0x0C
BLOB_SCRIPT_OFFSET_OFFSET = 0x38


def script_section(blob):
    """Return the Script_Section bytes of one BEF_Blob.

    Raises `ValueError` if the span is not sane; callers that must not fail
    (the corpus walk, `disassemble_bef_type`) catch it and skip.
    """
    blob = bytes(blob)
    if len(blob) < BLOB_SCRIPT_OFFSET_OFFSET + 4:
        raise ValueError(f"blob too small for a script span: {len(blob)} bytes")
    start = int.from_bytes(
        blob[BLOB_SCRIPT_OFFSET_OFFSET:BLOB_SCRIPT_OFFSET_OFFSET + 4], "little")
    end = int.from_bytes(
        blob[BLOB_PAYLOAD_LENGTH_OFFSET:BLOB_PAYLOAD_LENGTH_OFFSET + 4], "little")
    if not (0 < start <= end <= len(blob)):
        raise ValueError(
            f"implausible script span [0x{start:X}:0x{end:X}] in a {len(blob)}-byte blob"
        )
    return blob[start:end]


def disassemble_bef_type(path, type_id, revision):
    """Disassemble one BEF type's Script_Section. Returns `None` on failure.

    Reuses `randomizer.bef.BefFile` for parsing. Reads only; writes nothing and
    modifies no BEF file or executable. A blob that fails to parse is reported
    through the returned `None` plus the message on `.skipped` — see
    `iter_bef_scripts` for the corpus form that reports and continues.
    """
    from randomizer.bef import BefFile

    try:
        bef = BefFile.parse(path)
    except Exception as exc:  # unparsable file: report and skip
        disassemble_bef_type.skipped = f"{path}: {exc}"
        return None
    entry = bef.get_entry(type_id)
    if entry is None:
        disassemble_bef_type.skipped = f"{path}: type 0x{type_id:02X} not present"
        return None
    try:
        section = script_section(entry.data)
    except ValueError as exc:
        disassemble_bef_type.skipped = f"{path} type 0x{type_id:02X}: {exc}"
        return None
    disassemble_bef_type.skipped = None
    return disassemble(section, revision)


disassemble_bef_type.skipped = None


def iter_bef_scripts(path):
    """Yield `(type_id, section_bytes_or_None, message)` for one BEF file.

    A file or blob that fails to parse yields a message and is skipped rather
    than raising, so a corpus walk continues. Read-only.
    """
    from randomizer.bef import BefFile

    try:
        bef = BefFile.parse(path)
    except Exception as exc:
        yield (None, None, f"{path}: unparsable BEF ({exc})")
        return
    for entry in bef.entries:
        try:
            yield (entry.type_id, script_section(entry.data), None)
        except ValueError as exc:
            yield (entry.type_id, None, f"{path} type 0x{entry.type_id:02X}: {exc}")


__all__ += [
    "Instruction", "Operand", "DecodeFailure", "Disassembly", "disassemble",
    "encode", "format_instruction", "format_listing", "annotate_api_operand",
    "script_section", "disassemble_bef_type", "iter_bef_scripts",
    "PAD_MNEMONIC", "DATUM_MNEMONIC", "FAILURE_UNKNOWN_OPCODE",
    "FAILURE_TRUNCATED", "BLOB_PAYLOAD_LENGTH_OFFSET",
    "BLOB_SCRIPT_OFFSET_OFFSET",
]
