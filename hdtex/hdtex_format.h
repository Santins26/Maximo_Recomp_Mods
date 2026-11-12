// HD texture packs for the Maximo port (HD_TEXTURES.md).
//
// The launcher installs PCSX2-format replacement packs (<hash>[-<clut hash>]-<bits>.png, SLES-50703) by running
// `maximo_boot.exe --hdtex-convert`, which turns each pack into one file the game reads directly:
//   textures\<pack>\pack.mhtx   header, texture data (one mip chain per entry), sorted index at the end
//   textures\<pack>\pack.txt    what was converted (source, cap, counts), for the launcher
//   textures\<pack>\convert_log.txt   every skipped file and why
// The game never decodes PNGs: it looks up (texture hash, CLUT hash, bits) in the index and uploads the stored mips.
#pragma once
#include <cstdint>

namespace hdtex {

constexpr uint32_t kMagic = 0x5854484Du; // "MHTX"
// 2: PNGs without alpha get 0x80 (version 1 gave them 255). 3: Entry::flags (EntryFlatAlpha), alphaMin / alphaMax.
constexpr uint32_t kVersion = 3;
// Entry::flags: the image's alpha is one value everywhere (PNG without alpha, or an upscaler that dropped the alpha).
// The game then keeps the original texture's alpha (its shape) and takes only the colours from the pack.
constexpr uint16_t EntryFlatAlpha = 1;

enum Format : uint8_t { FormatRGBA8 = 1, FormatBC7 = 2 };

#pragma pack(push, 1)
struct Header {
    uint32_t magic;       // kMagic
    uint32_t version;     // kVersion
    uint32_t count;       // index entries
    uint32_t flags;       // 0
    uint64_t indexOffset; // file offset of count Entry records, sorted by (tex, clut, bits)
    uint64_t reserved;
};
struct Entry {
    uint64_t tex;    // PCSX2 TEX0Hash (XXH3-64 of the texture data)
    uint64_t clut;   // PCSX2 CLUTHash (XXH3-64 of the palette in index order); 0 when the name has none
    uint32_t bits;   // PCSX2 TEX0 bits: PSM 0-5, TW 6-9, TH 10-13, TCC 14, TA0 15-22, AEM 23, TA1 24-31
    uint16_t width;  // level 0 size in texels
    uint16_t height;
    uint8_t format;  // Format. Alpha as in the PCSX2 pack: 0-255, 255 = opaque (the shader maps it to the GS's 0x80)
    uint8_t levels;  // mip levels stored, level 0 first, each half the size of the previous (min 1)
    uint16_t flags;  // EntryFlatAlpha
    uint8_t alphaMin; // the image's alpha range (before compression)
    uint8_t alphaMax;
    uint16_t reserved1;
    uint64_t offset; // file offset of level 0; levels follow back to back
    uint64_t size;   // bytes of all levels
};
#pragma pack(pop)
static_assert(sizeof(Header) == 32 && sizeof(Entry) == 48, "pack layout");

inline bool keyLess(const Entry &a, const Entry &b)
{
    if (a.tex != b.tex) return a.tex < b.tex;
    if (a.clut != b.clut) return a.clut < b.clut;
    return a.bits < b.bits;
}

// Bytes of one level for a format (BC7: 4x4 blocks of 16 bytes, partial blocks padded).
inline uint64_t levelBytes(uint8_t format, uint32_t w, uint32_t h)
{
    if (format == FormatBC7) return uint64_t((w + 3) / 4) * ((h + 3) / 4) * 16;
    return uint64_t(w) * h * 4;
}

int convertMain(int argc, char **argv); // maximo_boot.exe --hdtex-convert ...

} // namespace hdtex
