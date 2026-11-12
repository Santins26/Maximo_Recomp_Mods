"""
Read-only MIPS (PS2 Emotion Engine / R5900) reader for ``game_files/MAXDEMOR.ELF``.

Analysis only. This module NEVER writes a file and never modifies a byte of the
ELF (Requirements 2.7, 10.1): the only filesystem call it makes is an ``open``
in binary READ mode. Everything else takes ``bytes`` and returns objects, so the
tests need no ELF on disk at all (Requirement 2.2).

Address arithmetic: the demo executable is loaded at virtual address 0x100000
and its first loaded byte sits at file offset 0x80, so

    file_offset = 0x80 + (vaddr - 0x100000)

which is exactly ``vaddr_to_offset`` (Requirement 2.1). ``LoadAnimResTable`` at
vaddr 0x00167F00 therefore lives at file offset 0x67F80.

The decoder covers only the instruction subset that the code in this region
actually uses (see the design's ``elf_reader.py`` section):

    lui ori addiu addu subu sll srl and or lw lh lhu lb lbu sw
    beq bne blez bgtz slt slti j jal jr jalr nop

Anything else decodes to ``mnemonic="?"`` with the raw word retained, so an
unknown encoding never aborts a decode and never silently becomes a wrong
instruction.

NOTE ON LOGGING: this module reads only. The log prefix ``ELF patch:`` is
reserved for actual executable patches and MUST NOT be used here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "ELF_LOAD_VADDR",
    "ELF_FILE_BASE",
    "DEFAULT_ELF_RELPATH",
    "LOAD_ANIM_RES_TABLE_VADDR",
    "REG_NAMES",
    "Insn",
    "vaddr_to_offset",
    "offset_to_vaddr",
    "read_elf_bytes",
    "decode_word",
    "decode_range",
    "branch_targets",
    "indexed_loads",
    "format_insn",
]

ELF_LOAD_VADDR = 0x100000
ELF_FILE_BASE = 0x80

DEFAULT_ELF_RELPATH = "game_files/MAXDEMOR.ELF"

#: The engine's anim resource-table loader, the target of stage 1.
LOAD_ANIM_RES_TABLE_VADDR = 0x00167F00

REG_NAMES = (
    "$zero", "$at", "$v0", "$v1", "$a0", "$a1", "$a2", "$a3",
    "$t0", "$t1", "$t2", "$t3", "$t4", "$t5", "$t6", "$t7",
    "$s0", "$s1", "$s2", "$s3", "$s4", "$s5", "$s6", "$s7",
    "$t8", "$t9", "$k0", "$k1", "$gp", "$sp", "$fp", "$ra",
)

#: Mnemonics that read memory through ``base + offset``.
LOAD_MNEMONICS = frozenset({"lw", "lh", "lhu", "lb", "lbu"})

#: Mnemonics that compute an address / index into a register.
ADDRESS_ARITH_MNEMONICS = frozenset({"addu", "addiu", "sll", "srl", "or", "ori", "lui"})

_UNKNOWN = "?"


# --------------------------------------------------------------------------
# address arithmetic (Requirement 2.1)
# --------------------------------------------------------------------------

def vaddr_to_offset(vaddr: int) -> int:
    """Return the ``MAXDEMOR.ELF`` file offset of ``vaddr``.

    ``0x80 + (vaddr - 0x100000)``. Order preserving: the arithmetic is a pure
    translation, so ``a < b`` implies ``vaddr_to_offset(a) < vaddr_to_offset(b)``.
    """
    if vaddr < ELF_LOAD_VADDR:
        raise ValueError(f"vaddr 0x{vaddr:08X} is below the load address 0x{ELF_LOAD_VADDR:08X}")
    return ELF_FILE_BASE + (vaddr - ELF_LOAD_VADDR)


def offset_to_vaddr(offset: int) -> int:
    """Inverse of :func:`vaddr_to_offset`."""
    if offset < ELF_FILE_BASE:
        raise ValueError(f"offset 0x{offset:X} is below the file base 0x{ELF_FILE_BASE:X}")
    return ELF_LOAD_VADDR + (offset - ELF_FILE_BASE)


def read_elf_bytes(path: str | Path = DEFAULT_ELF_RELPATH) -> bytes:
    """Read the whole ELF READ-ONLY and return its bytes.

    The only filesystem access in this module. No write mode is ever used and
    the file is never truncated, so the ELF is left byte-identical.
    """
    return Path(path).read_bytes()


# --------------------------------------------------------------------------
# instruction model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Insn:
    """One decoded 4-byte word.

    ``mnemonic`` is ``"?"`` for anything outside the decoded subset, and ``word``
    always retains the raw little-endian value so no information is lost.

    ``rs`` / ``rt`` / ``rd`` are register NUMBERS (or ``None`` where the field is
    not part of the instruction's meaning), kept alongside ``operands`` so
    dataflow helpers such as :func:`indexed_loads` can trace registers without
    re-parsing text.
    """

    vaddr: int
    word: int
    mnemonic: str
    operands: tuple = ()
    target: int | None = None
    rs: int | None = None
    rt: int | None = None
    rd: int | None = None
    imm: int | None = None

    @property
    def is_unknown(self) -> bool:
        return self.mnemonic == _UNKNOWN

    @property
    def is_branch(self) -> bool:
        return self.mnemonic in ("beq", "bne", "blez", "bgtz")

    @property
    def is_jump(self) -> bool:
        return self.mnemonic in ("j", "jal")

    @property
    def dest_reg(self) -> int | None:
        """The register this instruction writes, or ``None``."""
        m = self.mnemonic
        if m in ("addu", "subu", "and", "or", "slt", "sll", "srl"):
            return self.rd
        if m in ("addiu", "ori", "slti") or m in LOAD_MNEMONICS:
            return self.rt
        if m == "lui":
            return self.rt
        if m == "jalr":
            return self.rd
        if m == "jal":
            return 31
        return None


def _reg(num: int) -> str:
    return REG_NAMES[num & 31]


def _signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


# opcode 0 (SPECIAL) funct -> mnemonic, for the three-register forms
_SPECIAL_R = {
    0x21: "addu",
    0x23: "subu",
    0x24: "and",
    0x25: "or",
    0x2A: "slt",
}
_SPECIAL_SHIFT = {0x00: "sll", 0x02: "srl"}

_IMM_ARITH = {
    0x09: "addiu",
    0x0A: "slti",
    0x0D: "ori",
}

_MEM = {
    0x20: "lb",
    0x21: "lh",
    0x24: "lbu",
    0x25: "lhu",
    0x23: "lw",
    0x2B: "sw",
}


def decode_word(word: int, vaddr: int) -> Insn:
    """Decode one little-endian word already read out of the ELF."""
    word &= 0xFFFFFFFF
    op = word >> 26
    rs = (word >> 21) & 31
    rt = (word >> 16) & 31
    rd = (word >> 11) & 31
    shamt = (word >> 6) & 31
    funct = word & 0x3F
    imm_raw = word & 0xFFFF

    def unknown() -> Insn:
        return Insn(vaddr=vaddr, word=word, mnemonic=_UNKNOWN, operands=(word,))

    if op == 0x00:
        if word == 0:
            return Insn(vaddr=vaddr, word=word, mnemonic="nop")
        if funct in _SPECIAL_SHIFT:
            m = _SPECIAL_SHIFT[funct]
            return Insn(
                vaddr=vaddr, word=word, mnemonic=m,
                operands=(_reg(rd), _reg(rt), shamt),
                rt=rt, rd=rd, imm=shamt,
            )
        if funct in _SPECIAL_R:
            m = _SPECIAL_R[funct]
            return Insn(
                vaddr=vaddr, word=word, mnemonic=m,
                operands=(_reg(rd), _reg(rs), _reg(rt)),
                rs=rs, rt=rt, rd=rd,
            )
        if funct == 0x08:  # jr
            return Insn(vaddr=vaddr, word=word, mnemonic="jr",
                        operands=(_reg(rs),), rs=rs)
        if funct == 0x09:  # jalr
            return Insn(vaddr=vaddr, word=word, mnemonic="jalr",
                        operands=(_reg(rd), _reg(rs)), rs=rs, rd=rd)
        return unknown()

    if op in (0x02, 0x03):  # j / jal
        tgt = (vaddr & 0xF0000000) | ((word & 0x03FFFFFF) << 2)
        m = "j" if op == 0x02 else "jal"
        return Insn(vaddr=vaddr, word=word, mnemonic=m, operands=(tgt,), target=tgt)

    if op in (0x04, 0x05):  # beq / bne
        off = _signed16(imm_raw)
        tgt = vaddr + 4 + off * 4
        m = "beq" if op == 0x04 else "bne"
        return Insn(
            vaddr=vaddr, word=word, mnemonic=m,
            operands=(_reg(rs), _reg(rt), tgt), target=tgt,
            rs=rs, rt=rt, imm=off,
        )

    if op in (0x06, 0x07):  # blez / bgtz
        off = _signed16(imm_raw)
        tgt = vaddr + 4 + off * 4
        m = "blez" if op == 0x06 else "bgtz"
        return Insn(
            vaddr=vaddr, word=word, mnemonic=m,
            operands=(_reg(rs), tgt), target=tgt, rs=rs, imm=off,
        )

    if op == 0x0F:  # lui
        return Insn(vaddr=vaddr, word=word, mnemonic="lui",
                    operands=(_reg(rt), imm_raw), rt=rt, imm=imm_raw)

    if op in _IMM_ARITH:
        m = _IMM_ARITH[op]
        value = imm_raw if m == "ori" else _signed16(imm_raw)
        return Insn(vaddr=vaddr, word=word, mnemonic=m,
                    operands=(_reg(rt), _reg(rs), value), rs=rs, rt=rt, imm=value)

    if op in _MEM:
        m = _MEM[op]
        off = _signed16(imm_raw)
        return Insn(vaddr=vaddr, word=word, mnemonic=m,
                    operands=(_reg(rt), off, _reg(rs)), rs=rs, rt=rt, imm=off)

    return unknown()


def decode_range(elf_bytes: bytes, start_vaddr: int, word_count: int) -> list[Insn]:
    """Decode ``word_count`` words starting at ``start_vaddr``.

    One :class:`Insn` per 4-byte little-endian word, in ascending address order
    with a step of exactly 4 (Requirement 2.2). ``elf_bytes`` may be the whole
    ELF (then addresses are translated with :func:`vaddr_to_offset`) or, when
    ``start_vaddr`` is 0 or the buffer is shorter than the translated offset
    requires, a bare code slice whose first byte IS ``start_vaddr`` -- which is
    what lets tests decode synthetic words with no file.

    Pure: reads nothing, writes nothing.
    """
    if word_count < 0:
        raise ValueError("word_count must not be negative")

    base = 0
    if start_vaddr >= ELF_LOAD_VADDR:
        offset = vaddr_to_offset(start_vaddr)
        if offset + word_count * 4 <= len(elf_bytes):
            base = offset
    if base == 0 and word_count * 4 > len(elf_bytes):
        raise ValueError(
            f"buffer of {len(elf_bytes)} bytes cannot supply {word_count} words"
        )

    out: list[Insn] = []
    for i in range(word_count):
        pos = base + i * 4
        word = int.from_bytes(elf_bytes[pos:pos + 4], "little")
        out.append(decode_word(word, start_vaddr + i * 4))
    return out


# --------------------------------------------------------------------------
# analysis helpers
# --------------------------------------------------------------------------

def branch_targets(insns: Iterable[Insn]) -> list[int]:
    """Every distinct branch/jump target vaddr, ascending.

    Covers ``beq/bne/blez/bgtz`` and ``j/jal``. ``jr``/``jalr`` targets are
    register-computed and therefore not knowable here; they show up instead as
    the dispatch that :func:`indexed_loads` is used to trace.
    """
    seen = {i.target for i in insns if i.target is not None}
    return sorted(seen)


def indexed_loads(insns: Sequence[Insn], seed_regs: Iterable[int] | None = None) -> list[Insn]:
    """Loads whose address is computed rather than a plain frame access.

    A load is INDEXED when its base register was produced, earlier in the same
    decoded window, by address arithmetic (``addu``/``addiu``/``sll``/``srl``/
    ``or``/``ori``/``lui``) that itself descends from a value loaded out of
    memory -- which is the shape of a type-tag driven table lookup
    (``lw tag`` -> ``sll tag,2`` -> ``addu base`` -> ``lw handler``).

    ``seed_regs`` optionally seeds the tainted set with register numbers known
    to hold the per-record TYPE TAG, so a caller that has already located the
    tag load can ask specifically for the loads indexed BY that tag
    (Requirement 2.3).

    Straight-line propagation only: no attempt is made to follow branches, so a
    result is a lead for a human reader, not a proof.
    """
    tainted: set[int] = {r & 31 for r in (seed_regs or ())}
    tainted.discard(0)
    out: list[Insn] = []

    for insn in insns:
        m = insn.mnemonic
        base_tainted = insn.rs is not None and insn.rs in tainted

        if m in LOAD_MNEMONICS:
            if base_tainted:
                out.append(insn)
            # a loaded value is itself a candidate index source
            if insn.rt:
                tainted.add(insn.rt)
            continue

        if m in ADDRESS_ARITH_MNEMONICS:
            src_tainted = (
                (insn.rs is not None and insn.rs in tainted)
                or (insn.rt is not None and insn.rt in tainted and m in ("addu", "or", "sll", "srl"))
            )
            dest = insn.dest_reg
            if dest:
                if src_tainted:
                    tainted.add(dest)
                elif m in ("lui", "ori") and insn.rs in (None, 0):
                    # a fresh constant base; not tainted, and it clears any
                    # stale taint on the destination
                    tainted.discard(dest)
            continue

        dest = insn.dest_reg
        if dest:
            tainted.discard(dest)

    return out


def format_insn(insn: Insn) -> str:
    """One-line listing form, e.g. ``0x00167F00  27BDFFD0  addiu $sp, $sp, -48``."""
    if insn.is_unknown:
        body = f"?  .word 0x{insn.word:08X}"
    elif not insn.operands:
        body = insn.mnemonic
    else:
        parts = []
        for op in insn.operands:
            if isinstance(op, int):
                parts.append(f"0x{op:08X}" if op > 0xFFFF or op < -0x8000 else str(op))
            else:
                parts.append(str(op))
        if insn.mnemonic in _MEM.values():
            body = f"{insn.mnemonic} {parts[0]}, {parts[1]}({parts[2]})"
        else:
            body = f"{insn.mnemonic} " + ", ".join(parts)
    return f"0x{insn.vaddr:08X}  {insn.word:08X}  {body}"
