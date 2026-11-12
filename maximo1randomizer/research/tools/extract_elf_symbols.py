"""Extract the ELF32 symbol table + section headers from MAXDEMOR.ELF (the
debug build of the game, found in game_files/). This is a DEBUG build (6MB,
vs 1.17MB for the retail SLUS_200.17) and has a full, un-stripped .symtab /
.strtab plus a large Metrowerks-format .debug/.line section with real struct
field names.

Usage:
    python extract_elf_symbols.py path/to/MAXDEMOR.ELF symbols_out.txt

Output format (tab-separated): name, address (hex), size, st_info, st_shndx

Regenerating this is much faster than re-deriving it: this script is what
produced research/symbols.txt.
"""
import struct
import sys


def va_to_file_offset(va, main_addr=0x100000, main_file_off=0x80):
    """Convert a virtual address in the 'main' section to a file offset.
    Only valid for addresses within the 'main' section range -- check
    the section table (see dump_sections below) if working with a
    different ELF build where these numbers might differ."""
    return main_file_off + (va - main_addr)


def dump_sections(data):
    e_shoff, e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<IHHH', data, 0x20)[0:1] + (0,0,0)
    # (kept simple/explicit rather than fully general -- re-read header fields directly)
    e_type, e_machine, e_version = struct.unpack_from('<HHI', data, 16)
    e_entry, e_phoff, e_shoff = struct.unpack_from('<III', data, 24)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<HHH', data, 46)
    sections = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        fields = struct.unpack_from('<IIIIIIIIII', data, off)
        sections.append(dict(zip(
            ['name','type','flags','addr','offset','size','link','info','addralign','entsize'],
            fields)))
    shstr = sections[e_shstrndx]
    shstrtab = data[shstr['offset']:shstr['offset']+shstr['size']]
    def getname(o):
        end = shstrtab.find(b'\x00', o)
        return shstrtab[o:end].decode('latin1')
    for s in sections:
        s['name_str'] = getname(s['name'])
    return sections


def extract_symbols(data):
    sections = dump_sections(data)
    symtab = next(s for s in sections if s['name_str'] == '.symtab')
    strtab = next(s for s in sections if s['name_str'] == '.strtab')
    strtab_data = data[strtab['offset']:strtab['offset']+strtab['size']]

    def getname(off):
        end = strtab_data.find(b'\x00', off)
        return strtab_data[off:end].decode('latin1', errors='replace')

    n_syms = symtab['size'] // 16
    syms = []
    for i in range(n_syms):
        off = symtab['offset'] + i*16
        st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from('<IIIBBH', data, off)
        syms.append((getname(st_name), st_value, st_size, st_info, st_shndx))
    return syms, sections


if __name__ == '__main__':
    elf_path = sys.argv[1] if len(sys.argv) > 1 else 'MAXDEMOR.ELF'
    out_path = sys.argv[2] if len(sys.argv) > 2 else 'symbols.txt'
    data = open(elf_path, 'rb').read()
    syms, sections = extract_symbols(data)
    with open(out_path, 'w') as f:
        for name, val, size, info, shndx in syms:
            f.write(f'{name}\t0x{val:08X}\t{size}\t{info}\t{shndx}\n')
    print(f'wrote {len(syms)} symbols to {out_path}')
    print('sections:')
    for s in sections:
        print(f"  {s['name_str']:12s} type={s['type']:2d} off=0x{s['offset']:X} size=0x{s['size']:X} addr=0x{s['addr']:X}")
