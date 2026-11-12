"""Minimal MIPS R5900 (PS2 EE) disassembler used for the cross-world texture
research (see ../CROSSWORLD_TEXTURE_RESEARCH.md).

Covers standard MIPS I/II opcodes (arithmetic, loads/stores, branches, jumps).
Decodes `lq`/`sq` plus the one EE "MMI" (multimedia instruction) form the
Metrowerks codegen uses pervasively: `paddub rd, rs, $zero`, which is its
register move, rendered as `move rd, rs`. Every other MMI word is still
printed as an opaque ".mmi 0xXXXXXXXX" placeholder -- full MMI coverage is
out of scope.

Usage:
    from mips_disasm import disasm_range
    code_bytes = ...  # raw function bytes, length multiple of 4
    for addr, word, text in disasm_range(code_bytes, base_addr):
        print(f'{addr:08X}: {word:08X}  {text}')
"""
import struct

REGS = ['zero','at','v0','v1','a0','a1','a2','a3',
        't0','t1','t2','t3','t4','t5','t6','t7',
        's0','s1','s2','s3','s4','s5','s6','s7',
        't8','t9','k0','k1','gp','sp','fp','ra']

def r(n): return '$'+REGS[n]

SPECIAL = {
    0x00:'sll', 0x02:'srl', 0x03:'sra', 0x04:'sllv', 0x06:'srlv', 0x07:'srav',
    0x08:'jr', 0x09:'jalr', 0x0c:'syscall', 0x0d:'break',
    0x10:'mfhi', 0x11:'mthi', 0x12:'mflo', 0x13:'mtlo',
    0x18:'mult', 0x19:'multu', 0x1a:'div', 0x1b:'divu',
    0x20:'add', 0x21:'addu', 0x22:'sub', 0x23:'subu',
    0x24:'and', 0x25:'or', 0x26:'xor', 0x27:'nor',
    0x2a:'slt', 0x2b:'sltu',
    0x2c:'dadd', 0x2d:'daddu',
    0x14:'dsllv', 0x16:'dsrlv',
    0x3c:'dsll32', 0x3e:'dsrl32', 0x3f:'dsra32',
}
REGIMM = {0x00:'bltz',0x01:'bgez',0x10:'bltzal',0x11:'bgezal'}
OPCODES = {
    0x02:'j', 0x03:'jal', 0x04:'beq', 0x05:'bne', 0x06:'blez', 0x07:'bgtz',
    0x08:'addi', 0x09:'addiu', 0x0a:'slti', 0x0b:'sltiu', 0x0c:'andi', 0x0d:'ori', 0x0e:'xori',
    0x0f:'lui',
    0x14:'beql', 0x15:'bnel', 0x16:'blezl', 0x17:'bgtzl',
    0x20:'lb', 0x21:'lh', 0x23:'lw', 0x24:'lbu', 0x25:'lhu', 0x27:'lwu',
    0x28:'sb', 0x29:'sh', 0x2b:'sw',
    0x2f:'cache', 0x37:'ld', 0x3f:'sd',
    0x31:'lwc1', 0x39:'swc1',
    0x1e:'lq', 0x1f:'sq',
}

# Narrow MMI (SPECIAL2, op 0x1c) coverage. Full MMI decoding is out of scope;
# only the sub-ops the Metrowerks codegen actually leans on are decoded.
# SPECIAL2 funct 0x28 selects the MMI1 class, whose sub-op field is bits 10..6.
MMI1_SUBOP_PADDUB = 0x18

def sext16(v):
    return v-0x10000 if v & 0x8000 else v

def disasm_one(word, addr):
    op = (word>>26)&0x3f
    rs = (word>>21)&0x1f
    rt = (word>>16)&0x1f
    rd = (word>>11)&0x1f
    shamt = (word>>6)&0x1f
    funct = word&0x3f
    imm = word&0xffff
    target = word & 0x3ffffff

    if word == 0:
        return 'nop'
    if op == 0:
        name = SPECIAL.get(funct)
        if name is None:
            return f'.word 0x{word:08x} (special funct=0x{funct:02x})'
        if name in ('sll','srl','sra') and rd!=0 and rt==0 and rs==0 and shamt==0:
            return 'nop'
        if name in ('sll','srl','sra'):
            return f'{name} {r(rd)}, {r(rt)}, {shamt}'
        if name in ('sllv','srlv','srav','add','addu','sub','subu','and','or','xor','nor','slt','sltu','dadd','daddu'):
            return f'{name} {r(rd)}, {r(rs)}, {r(rt)}'
        if name in ('jr',):
            return f'jr {r(rs)}'
        if name in ('jalr',):
            return f'jalr {r(rd)}, {r(rs)}'
        if name in ('mfhi','mflo'):
            return f'{name} {r(rd)}'
        if name in ('mthi','mtlo'):
            return f'{name} {r(rs)}'
        if name in ('mult','multu','div','divu'):
            return f'{name} {r(rs)}, {r(rt)}'
        if name in ('dsll32','dsrl32','dsra32'):
            return f'{name} {r(rd)}, {r(rt)}, {shamt}'
        return f'{name} (unhandled fmt)'
    if op == 0x1c:  # SPECIAL2 (mmi on EE) - narrow subset only
        # MMI1 class (funct 0x28), sub-op in the shamt field.
        if funct == 0x28 and shamt == MMI1_SUBOP_PADDUB:
            # paddub rd, rs, rt. Metrowerks emits it with one source tied to
            # $zero as its 128-bit-safe register move, so render that as `move`.
            if rt == 0:
                return f'move {r(rd)}, {r(rs)}'
            if rs == 0:
                return f'move {r(rd)}, {r(rt)}'
            return f'paddub {r(rd)}, {r(rs)}, {r(rt)}'
        return f'.mmi 0x{word:08x}'
    if op == 0x01:
        name = REGIMM.get(rt, f'regimm_0x{rt:02x}')
        off = sext16(imm)*4
        return f'{name} {r(rs)}, 0x{addr+4+off:08X}'
    name = OPCODES.get(op)
    if name is None:
        return f'.word 0x{word:08x} (op=0x{op:02x})'
    if name in ('j','jal'):
        tgt = (addr & 0xf0000000) | (target<<2)
        return f'{name} 0x{tgt:08X}'
    if name in ('beq','bne','beql','bnel'):
        off = sext16(imm)*4
        return f'{name} {r(rs)}, {r(rt)}, 0x{addr+4+off:08X}'
    if name in ('blez','bgtz','blezl','bgtzl'):
        off = sext16(imm)*4
        return f'{name} {r(rs)}, 0x{addr+4+off:08X}'
    if name in ('addi','addiu','slti','sltiu'):
        return f'{name} {r(rt)}, {r(rs)}, {sext16(imm)}'
    if name in ('andi','ori','xori'):
        return f'{name} {r(rt)}, {r(rs)}, 0x{imm:04x}'
    if name == 'lui':
        return f'lui {r(rt)}, 0x{imm:04x}'
    if name in ('lb','lh','lw','lbu','lhu','lwu','ld','lwc1','lq'):
        return f'{name} {r(rt)}, {sext16(imm)}({r(rs)})'
    if name in ('sb','sh','sw','sd','swc1','sq'):
        return f'{name} {r(rt)}, {sext16(imm)}({r(rs)})'
    if name == 'cache':
        return f'cache 0x{rt:x}, {sext16(imm)}({r(rs)})'
    return f'{name} (unhandled fmt) rs={rs} rt={rt} imm={imm}'

def disasm_range(code_bytes, base_addr):
    out = []
    for i in range(0, len(code_bytes), 4):
        word = struct.unpack_from('<I', code_bytes, i)[0]
        addr = base_addr + i
        out.append((addr, word, disasm_one(word, addr)))
    return out
