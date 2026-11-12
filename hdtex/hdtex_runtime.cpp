// HD texture replacement at run time; see hdtex_runtime.h and HD_TEXTURES.md.
#include "hdtex_runtime.h"
#include "hdtex_format.h"

#include "gs_interface.hpp"
#include "device.hpp"

#define XXH_INLINE_ALL
#include "xxhash.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace hdtex {
namespace {

// GS pixel storage formats used here.
enum : uint32_t { PSMCT32 = 0x00, PSMCT24 = 0x01, PSMCT16 = 0x02, PSMCT16S = 0x0A, PSMT8 = 0x13, PSMT4 = 0x14 };

constexpr uint32_t kVramBlocks = 4 * 1024 * 1024 / 256;

// Block numbers inside a page (GS manual), [row][column].
constexpr uint8_t kBlocks32[4][8] = {{0, 1, 4, 5, 16, 17, 20, 21}, {2, 3, 6, 7, 18, 19, 22, 23},
                                     {8, 9, 12, 13, 24, 25, 28, 29}, {10, 11, 14, 15, 26, 27, 30, 31}};
constexpr uint8_t kBlocks16[8][4] = {{0, 2, 8, 10}, {1, 3, 9, 11}, {4, 6, 12, 14}, {5, 7, 13, 15},
                                     {16, 18, 24, 26}, {17, 19, 25, 27}, {20, 22, 28, 30}, {21, 23, 29, 31}};
// Word offset of a PSMCT32 pixel inside its 8x8 block.
constexpr uint8_t kColumn32[8][8] = {{0, 1, 4, 5, 8, 9, 12, 13}, {2, 3, 6, 7, 10, 11, 14, 15},
                                     {16, 17, 20, 21, 24, 25, 28, 29}, {18, 19, 22, 23, 26, 27, 30, 31},
                                     {32, 33, 36, 37, 40, 41, 44, 45}, {34, 35, 38, 39, 42, 43, 46, 47},
                                     {48, 49, 52, 53, 56, 57, 60, 61}, {50, 51, 54, 55, 58, 59, 62, 63}};

struct Layout { uint32_t pageW, pageH, blockW, blockH; bool wide; }; // wide: 8 block columns per page (32/8-bit)

bool layoutOf(uint32_t psm, Layout &l)
{
    switch (psm) {
    case PSMCT32: case PSMCT24: l = {64, 32, 8, 8, true}; return true;
    case PSMCT16: case PSMCT16S: l = {64, 64, 16, 8, false}; return true;
    case PSMT8: l = {128, 64, 16, 16, true}; return true;
    case PSMT4: l = {128, 128, 32, 16, false}; return true;
    default: return false;
    }
}

// 256-byte block number in VRAM of block (bx, by) of a texture at tbp (blocks) with buffer width tbw (64 px units).
uint32_t blockAddress(const Layout &l, uint32_t tbp, uint32_t tbw, uint32_t bx, uint32_t by)
{
    const uint32_t pagesPerRow = std::max(1u, tbw * 64 / l.pageW);
    const uint32_t x = bx * l.blockW, y = by * l.blockH;
    const uint32_t page = (y / l.pageH) * pagesPerRow + x / l.pageW;
    const uint32_t col = (x % l.pageW) / l.blockW, row = (y % l.pageH) / l.blockH;
    const uint32_t block = l.wide ? kBlocks32[row][col] : kBlocks16[row][col];
    return (tbp + page * 32 + block) % kVramBlocks;
}

// PCSX2 TEX0Hash for textures made of whole blocks: XXH3-64 over the 256-byte blocks in raster order.
bool textureHash(const uint8_t *vram, uint32_t psm, uint32_t tbp, uint32_t tbw, uint32_t tw, uint32_t th, uint64_t &out)
{
    Layout l;
    if (!layoutOf(psm, l) || tw < l.blockW || th < l.blockH) return false;
    XXH3_state_t st;
    XXH3_64bits_reset(&st);
    for (uint32_t by = 0; by < th / l.blockH; by++)
        for (uint32_t bx = 0; bx < tw / l.blockW; bx++)
            XXH3_64bits_update(&st, vram + size_t(blockAddress(l, tbp, tbw, bx, by)) * 256, 256);
    out = XXH3_64bits_digest(&st);
    return true;
}

uint32_t readCt32(const uint8_t *vram, uint32_t cbp, uint32_t x, uint32_t y)
{
    static const Layout l = {64, 32, 8, 8, true};
    const uint32_t block = blockAddress(l, cbp, 1, x / 8, y / 8);
    uint32_t v;
    std::memcpy(&v, vram + size_t(block) * 256 + kColumn32[y % 8][x % 8] * 4, 4);
    return v;
}

// PCSX2 CLUTHash: XXH3-64 of the palette in index order. CT32 palettes in CSM1 layout only (T8: 16x16, T4: 8x2).
bool clutHash(const uint8_t *vram, uint32_t psm, uint32_t cpsm, uint32_t csm, uint32_t cbp, uint64_t &out)
{
    if (cpsm != PSMCT32 || csm != 0) return false;
    uint32_t pal[256];
    uint32_t n = 0;
    if (psm == PSMT8) {
        n = 256;
        for (uint32_t i = 0; i < 256; i++) {
            const uint32_t j = (i & ~0x18u) | ((i & 0x08u) << 1) | ((i & 0x10u) >> 1);
            pal[i] = readCt32(vram, cbp, j % 16, j / 16);
        }
    } else if (psm == PSMT4) {
        n = 16;
        for (uint32_t i = 0; i < 16; i++) pal[i] = readCt32(vram, cbp, i % 8, i / 8);
    } else {
        return false;
    }
    out = XXH3_64bits(pal, n * 4);
    return true;
}

// Alpha of the original texture in VRAM (CT32, or T8 / T4 with a CT32 palette; n = 0 for other formats).
struct AlphaStats { uint32_t min = 255, max = 0; uint64_t sum = 0, n = 0; };
AlphaStats originalAlpha(const uint8_t *vram, uint32_t psm, uint32_t cpsm, uint32_t tbp, uint32_t tbw, uint32_t cbp,
                         uint32_t tw, uint32_t th)
{
    AlphaStats s;
    auto add = [&](uint32_t a) { s.min = std::min(s.min, a); s.max = std::max(s.max, a); s.sum += a; s.n++; };
    const uint32_t w = 1u << std::min(tw, 10u), h = 1u << std::min(th, 10u);
    Layout l;
    if (psm == PSMCT32) {
        static const Layout l32 = {64, 32, 8, 8, true};
        for (uint32_t y = 0; y < h; y++)
            for (uint32_t x = 0; x < w; x++)
                add(vram[size_t(blockAddress(l32, tbp, tbw, x / 8, y / 8)) * 256 + kColumn32[y % 8][x % 8] * 4 + 3]);
    } else if ((psm == PSMT8 || psm == PSMT4) && cpsm == PSMCT32 && layoutOf(psm, l) && w >= l.blockW && h >= l.blockH) {
        uint32_t pal[256];
        for (uint32_t i = 0; i < (psm == PSMT8 ? 256u : 16u); i++) {
            const uint32_t j = psm == PSMT8 ? ((i & ~0x18u) | ((i & 0x08u) << 1) | ((i & 0x10u) >> 1)) : i;
            pal[i] = readCt32(vram, cbp, psm == PSMT8 ? j % 16 : j % 8, psm == PSMT8 ? j / 16 : j / 8);
        }
        // Texel order does not matter for the statistics: every byte (T8) or nibble (T4) of the blocks.
        for (uint32_t by = 0; by < h / l.blockH; by++)
            for (uint32_t bx = 0; bx < w / l.blockW; bx++) {
                const uint8_t *b = vram + size_t(blockAddress(l, tbp, tbw, bx, by)) * 256;
                for (uint32_t i = 0; i < 256; i++) {
                    if (psm == PSMT8) add(pal[b[i]] >> 24);
                    else { add(pal[b[i] & 15] >> 24); add(pal[b[i] >> 4] >> 24); }
                }
            }
    }
    return s;
}

struct Pack
{
    std::string path;
    std::FILE *file = nullptr; // read by the loader thread only
    std::vector<Entry> entries;
};

struct Key
{
    uint64_t tex, clut;
    uint32_t bits;
    bool operator==(const Key &o) const { return tex == o.tex && clut == o.clut && bits == o.bits; }
};
struct KeyHash
{
    size_t operator()(const Key &k) const { return size_t(k.tex ^ (k.clut * 0x9E3779B97F4A7C15ull) ^ (uint64_t(k.bits) << 17)); }
};

std::string hexName(const Key &k)
{
    char buf[64];
    if (k.clut) std::snprintf(buf, sizeof(buf), "%llx-%llx-%08x", (unsigned long long)k.tex, (unsigned long long)k.clut, k.bits);
    else std::snprintf(buf, sizeof(buf), "%llx-%08x", (unsigned long long)k.tex, k.bits);
    return buf;
}

bool envOn(const char *name)
{
    const char *v = std::getenv(name);
    return v && std::atoi(v) != 0;
}

} // namespace

struct Runtime::Impl
{
    std::vector<Pack> packs;
    struct Item { uint32_t pack, entry; };
    std::vector<Item> items; // id - 1
    std::unordered_map<Key, uint32_t, KeyHash> byKey; // -> id
    std::unordered_map<Key, uint64_t, KeyHash> alphaMode; // -> GSInterface::Replacement* bits
    ParallelGS::GSInterface *gs = nullptr;
    Vulkan::Device *device = nullptr;

    enum State : uint8_t { Idle, Queued, Loaded, Ready, Failed };
    struct Slot
    {
        State state = Idle;
        std::vector<uint8_t> bytes; // Loaded: the mips, until the image is made
        Vulkan::ImageHandle image;
    };
    std::vector<Slot> slots; // per id - 1; state / bytes guarded by mutex while Queued or Loaded

    std::mutex mutex;
    std::condition_variable cv;
    std::deque<uint32_t> queue;
    bool quit = false;
    std::thread loader;

    // Statistics (GS thread).
    uint64_t looked = 0, matched = 0, made = 0, madeBytes = 0, unsupported = 0;
    uint64_t lastLogged = ~0ull;
    int64_t budget = 0; // bytes of new images this frame
    const bool logMisses = envOn("MAXIMO_HDTEX_LOG_MISSES");
    // Diagnostics: MAXIMO_HDTEX_LOG_HITS=1 logs each replaced texture once (name, tbp); MAXIMO_HDTEX_OFF_FROM=<frame>
    // stops drawing replacements from that guest frame (A/B comparison of consecutive frames).
    const bool logHits = envOn("MAXIMO_HDTEX_LOG_HITS");
    const long long offFrom = [] { const char *v = std::getenv("MAXIMO_HDTEX_OFF_FROM"); return v ? std::atoll(v) : -1ll; }();
    std::atomic<bool> off{false};
    std::unordered_map<Key, bool, KeyHash> hitLogged;
    std::unordered_map<Key, bool, KeyHash> missLogged;

    ~Impl()
    {
        {
            std::lock_guard<std::mutex> lk(mutex);
            quit = true;
        }
        cv.notify_all();
        if (loader.joinable()) loader.join();
        for (Pack &p : packs)
            if (p.file) std::fclose(p.file);
        std::fprintf(stderr, "[hdtex] session: %llu of %llu textures looked at had a replacement; %llu loaded (%.1f MB)\n",
                     (unsigned long long)matched, (unsigned long long)looked, (unsigned long long)made, madeBytes / 1048576.0);
    }

    bool openPack(const std::string &where)
    {
        std::string path = where;
        if (path.size() < 5 || path.compare(path.size() - 5, 5, ".mhtx") != 0)
            path += (path.empty() || path.back() == '\\' || path.back() == '/') ? "pack.mhtx" : "\\pack.mhtx";
        Pack p;
        p.path = path;
        p.file = std::fopen(path.c_str(), "rb");
        Header h{};
        if (!p.file || std::fread(&h, sizeof(h), 1, p.file) != 1 || h.magic != kMagic || h.version != kVersion) {
            if (h.magic == kMagic && h.version < kVersion)
                std::fprintf(stderr, "[hdtex] %s was made by an older version; reinstall (convert) the pack again\n",
                             path.c_str());
            else
                std::fprintf(stderr, "[hdtex] cannot use %s\n", path.c_str());
            if (p.file) std::fclose(p.file);
            return false;
        }
        p.entries.resize(h.count);
        if (_fseeki64(p.file, int64_t(h.indexOffset), SEEK_SET) != 0 ||
            (h.count && std::fread(p.entries.data(), sizeof(Entry), h.count, p.file) != h.count)) {
            std::fprintf(stderr, "[hdtex] %s: index unreadable\n", path.c_str());
            std::fclose(p.file);
            return false;
        }
        const uint32_t packIndex = uint32_t(packs.size());
        uint32_t added = 0;
        for (uint32_t i = 0; i < h.count; i++) {
            const Entry &e = p.entries[i];
            if ((e.format != FormatRGBA8 && e.format != FormatBC7) || !e.width || !e.height || !e.levels) continue;
            if (byKey.emplace(Key{e.tex, e.clut, e.bits}, uint32_t(items.size() + 1)).second) {
                items.push_back({packIndex, i});
                added++;
            }
        }
        std::fprintf(stderr, "[hdtex] pack %s: %u textures (%u not overridden by a pack above it)\n", path.c_str(), h.count,
                     added);
        packs.push_back(std::move(p));
        return true;
    }

    void loaderLoop()
    {
        for (;;) {
            uint32_t id;
            {
                std::unique_lock<std::mutex> lk(mutex);
                cv.wait(lk, [&] { return quit || !queue.empty(); });
                if (quit) return;
                id = queue.front();
                queue.pop_front();
            }
            const Item &it = items[id - 1];
            Pack &p = packs[it.pack];
            const Entry &e = p.entries[it.entry];
            std::vector<uint8_t> bytes(size_t(e.size));
            const bool ok = _fseeki64(p.file, int64_t(e.offset), SEEK_SET) == 0 &&
                            std::fread(bytes.data(), 1, bytes.size(), p.file) == bytes.size();
            std::lock_guard<std::mutex> lk(mutex);
            Slot &s = slots[id - 1];
            if (ok) { s.bytes = std::move(bytes); s.state = Loaded; }
            else s.state = Failed;
        }
    }

    uint64_t classify(const ParallelGS::GSInterface::TextureReplacementQuery &q)
    {
        const uint64_t t = q.tex0;
        const uint32_t tbp = uint32_t(t & 0x3fff), tbw = uint32_t((t >> 14) & 0x3f), psm = uint32_t((t >> 20) & 0x3f);
        const uint32_t tw = uint32_t((t >> 26) & 0xf), th = uint32_t((t >> 30) & 0xf);
        const uint32_t cbp = uint32_t((t >> 37) & 0x3fff), cpsm = uint32_t((t >> 51) & 0xf), csm = uint32_t((t >> 55) & 1);
        looked++;
        // Diagnostics: MAXIMO_HDTEX_DUMP_TBP=<hex>: write the first 8 CT32 versions of that texture (RGBA, unswizzled).
        static const long dumpTbp = [] { const char *v = std::getenv("MAXIMO_HDTEX_DUMP_TBP"); return v ? std::strtol(v, nullptr, 16) : -1l; }();
        static int dumped = 0;
        if (long(tbp) == dumpTbp && psm == PSMCT32 && dumped < 8) {
            static const Layout l = {64, 32, 8, 8, true};
            const uint32_t w = 1u << std::min(tw, 10u), h = 1u << std::min(th, 10u);
            std::vector<uint32_t> img(size_t(w) * h);
            for (uint32_t y = 0; y < h; y++)
                for (uint32_t x = 0; x < w; x++) {
                    const uint32_t block = blockAddress(l, tbp, tbw, x / 8, y / 8);
                    std::memcpy(&img[size_t(y) * w + x], q.vram + size_t(block) * 256 + kColumn32[y % 8][x % 8] * 4, 4);
                }
            char name[64];
            std::snprintf(name, sizeof(name), "texdump_%lx_%d_%ux%u.rgba", dumpTbp, dumped++, w, h);
            if (FILE *f = std::fopen(name, "wb")) { std::fwrite(img.data(), 4, img.size(), f); std::fclose(f); }
        }
        // T8 with a CT32 palette: the palette in index order (256 words), then the texture's blocks in raster order.
        static int dumped8 = 0;
        if (long(tbp) == dumpTbp && psm == PSMT8 && cpsm == PSMCT32 && dumped8 < 8) {
            static const Layout l8 = {128, 64, 16, 16, true};
            const uint32_t w = 1u << std::min(tw, 10u), h = 1u << std::min(th, 10u);
            char name[64];
            std::snprintf(name, sizeof(name), "texdump_%lx_%d_t8_%ux%u.bin", dumpTbp, dumped8++, w, h);
            if (FILE *f = std::fopen(name, "wb")) {
                for (uint32_t i = 0; i < 256; i++) {
                    const uint32_t j = (i & ~0x18u) | ((i & 0x08u) << 1) | ((i & 0x10u) >> 1);
                    const uint32_t c = readCt32(q.vram, cbp, j % 16, j / 16);
                    std::fwrite(&c, 4, 1, f);
                }
                for (uint32_t by = 0; by < h / 16; by++)
                    for (uint32_t bx = 0; bx < w / 16; bx++)
                        std::fwrite(q.vram + size_t(blockAddress(l8, tbp, tbw, bx, by)) * 256, 256, 1, f);
                std::fclose(f);
            }
        }
        Key k{};
        if (!textureHash(q.vram, psm, tbp, tbw, 1u << std::min(tw, 10u), 1u << std::min(th, 10u), k.tex)) {
            unsupported++;
            return 0;
        }
        const bool palette = psm == PSMT8 || psm == PSMT4;
        if (palette && !clutHash(q.vram, psm, cpsm, csm, cbp, k.clut)) {
            unsupported++;
            return 0;
        }
        k.bits = psm | (tw << 6) | (th << 10);
        // TEXA (TA0, AEM, TA1) changes how 24/16-bit colours expand, so it is part of the name for those formats only.
        const bool texa = psm == PSMCT24 || psm == PSMCT16 || psm == PSMCT16S || (palette && cpsm != PSMCT32);
        if (texa)
            k.bits |= uint32_t(q.texa & 0xff) << 15 | uint32_t((q.texa >> 15) & 1) << 23 | uint32_t((q.texa >> 32) & 0xff) << 24;
        // PCSX2 names include TCC (bit 14). paraLLEl-GS caches one image for both TCC values, so a texture is
        // classified by whichever draw makes it first: the lookup tries that draw's TCC, then the other one.
        k.bits |= uint32_t((t >> 34) & 1) << 14;
        // Extraction for new art: MAXIMO_HDTEX_DUMP_HASH=<tex hash>: for each palette of that T8 texture, write
        // <pack name>.t8.bin (the palette in index order, then the texture's blocks in raster order).
        static const uint64_t dumpHash = [] { const char *v = std::getenv("MAXIMO_HDTEX_DUMP_HASH"); return v ? std::strtoull(v, nullptr, 16) : 0ull; }();
        static std::vector<uint64_t> dumpedCluts;
        if (k.tex == dumpHash && psm == PSMT8 && cpsm == PSMCT32 &&
            std::find(dumpedCluts.begin(), dumpedCluts.end(), k.clut) == dumpedCluts.end()) {
            dumpedCluts.push_back(k.clut);
            static const Layout l8 = {128, 64, 16, 16, true};
            const uint32_t w = 1u << std::min(tw, 10u), h = 1u << std::min(th, 10u);
            if (FILE *f = std::fopen((hexName(k) + ".t8.bin").c_str(), "wb")) {
                for (uint32_t i = 0; i < 256; i++) {
                    const uint32_t j = (i & ~0x18u) | ((i & 0x08u) << 1) | ((i & 0x10u) >> 1);
                    const uint32_t c = readCt32(q.vram, cbp, j % 16, j / 16);
                    std::fwrite(&c, 4, 1, f);
                }
                for (uint32_t by = 0; by < h / 16; by++)
                    for (uint32_t bx = 0; bx < w / 16; bx++)
                        std::fwrite(q.vram + size_t(blockAddress(l8, tbp, tbw, bx, by)) * 256, 256, 1, f);
                std::fclose(f);
            }
        }
        // Textures to keep as the game drew them, whatever the packs hold. Compared on the texture hash, so any palette matches.
        static const std::vector<uint64_t> keep = [] {
            std::vector<uint64_t> list;
            const char *v = std::getenv("MAXIMO_HDTEX_KEEP");
            std::string text = v ? v : "";
            for (size_t i = 0; i < text.size();) {
                size_t e = text.find(',', i);
                if (e == std::string::npos) e = text.size();
                if (e > i) list.push_back(std::strtoull(text.substr(i, e - i).c_str(), nullptr, 16));
                i = e + 1;
            }
            return list;
        }();
        static const long traceTbp = [] { const char *v = std::getenv("MAXIMO_HDTEX_TRACE_TBP"); return v ? std::strtol(v, nullptr, 16) : -1L; }();
        if (long(tbp) == traceTbp)
            std::fprintf(stderr, "[hdtex] trace: %s (tbp=%x cbp=%x)\n", hexName(k).c_str(), tbp, cbp);
        if (std::find(keep.begin(), keep.end(), k.tex) != keep.end())
            return 0;
        auto it = byKey.find(k);
        if (it == byKey.end()) {
            Key other = k;
            other.bits ^= 1u << 14;
            it = byKey.find(other);
            if (it != byKey.end()) k = other;
        }
        if (it == byKey.end()) {
            if (logMisses && missLogged.size() < 4096 && missLogged.emplace(k, true).second)
                std::fprintf(stderr, "[hdtex] no replacement: %s (tbp=%x tbw=%u %ux%u)\n", hexName(k).c_str(), tbp, tbw,
                             1u << tw, 1u << th);
            return 0;
        }
        matched++;
        // How the replacement's alpha is used, decided once per texture (same name = same original data).
        auto mode = alphaMode.find(k);
        if (mode == alphaMode.end()) {
            const Item &item = items[it->second - 1];
            const Entry &e = packs[item.pack].entries[item.entry];
            AlphaStats orig = originalAlpha(q.vram, psm, cpsm, tbp, tbw, cbp, tw, th);
            uint64_t bits = 0;
            const char *why = "raw";
            if (e.flags & EntryFlatAlpha) {
                // One alpha everywhere (PNG without alpha, or an upscaler that dropped it): keep the original's shape.
                bits = ParallelGS::GSInterface::ReplacementOriginalAlpha;
                why = "original";
            } else if (orig.n && orig.max <= 0x80 && e.alphaMax > 0x88) {
                // The game's texture never goes above 0x80 (GS opaque) but the image uses 0-255: the image was saved
                // with ordinary PNG alpha (255 = opaque). Scale it to the GS's range, or it blends about twice as dense.
                bits = ParallelGS::GSInterface::ReplacementScaleAlpha;
                why = "scaled 128/255";
            }
            mode = alphaMode.emplace(k, bits).first;
            if (logHits)
                std::fprintf(stderr, "[hdtex] replaced: %s (tbp=%x tbw=%u %ux%u cbp=%x) original alpha %u/%.1f/%u, image %u-%u: %s\n",
                             hexName(k).c_str(), tbp, tbw, 1u << tw, 1u << th, cbp, orig.n ? orig.min : 0,
                             orig.n ? double(orig.sum) / double(orig.n) : 0.0, orig.max, e.alphaMin, e.alphaMax, why);
        }
        return it->second | mode->second;
    }

    Vulkan::ImageHandle lookup(uint64_t id)
    {
        id &= ~(ParallelGS::GSInterface::ReplacementOriginalAlpha | ParallelGS::GSInterface::ReplacementScaleAlpha);
        if (!id || id > slots.size() || off.load(std::memory_order_relaxed)) return {};
        Slot &s = slots[id - 1];
        std::unique_lock<std::mutex> lk(mutex);
        if (s.state == Ready) return s.image;
        if (s.state == Idle) {
            s.state = Queued;
            queue.push_back(uint32_t(id));
            cv.notify_one();
            return {};
        }
        if (s.state != Loaded || budget <= 0) return {};
        std::vector<uint8_t> bytes = std::move(s.bytes);
        lk.unlock();
        const Item &it = items[id - 1];
        const Entry &e = packs[it.pack].entries[it.entry];
        Vulkan::ImageCreateInfo info = Vulkan::ImageCreateInfo::immutable_2d_image(
            e.width, e.height, e.format == FormatBC7 ? VK_FORMAT_BC7_UNORM_BLOCK : VK_FORMAT_R8G8B8A8_UNORM);
        info.levels = e.levels;
        std::vector<Vulkan::ImageInitialData> init(e.levels);
        uint64_t at = 0;
        bool fits = true;
        for (uint32_t l = 0; l < e.levels; l++) {
            const uint32_t w = std::max(1u, uint32_t(e.width) >> l), h = std::max(1u, uint32_t(e.height) >> l);
            init[l] = {bytes.data() + at, 0, 0};
            at += levelBytes(e.format, w, h);
            if (at > bytes.size()) fits = false;
        }
        Vulkan::ImageHandle image = fits ? device->create_image(info, init.data()) : Vulkan::ImageHandle{};
        lk.lock();
        if (!image) {
            s.state = Failed;
            std::fprintf(stderr, "[hdtex] cannot create %s (%ux%u)\n", hexName({e.tex, e.clut, e.bits}).c_str(), e.width, e.height);
            return {};
        }
        s.image = image;
        s.state = Ready;
        budget -= int64_t(bytes.size());
        made++;
        madeBytes += bytes.size();
        return image;
    }
};

Runtime::Runtime(std::unique_ptr<Impl> impl_) : impl(std::move(impl_)) {}
Runtime::~Runtime() = default;

std::unique_ptr<Runtime> Runtime::fromEnv()
{
    const char *list = std::getenv("MAXIMO_HDTEX_PACKS");
    if (!list || !*list) return nullptr;
    auto impl = std::make_unique<Impl>();
    std::string s = list;
    for (size_t a = 0; a <= s.size();) {
        size_t b = s.find(';', a);
        if (b == std::string::npos) b = s.size();
        std::string one = s.substr(a, b - a);
        if (!one.empty()) impl->openPack(one);
        a = b + 1;
    }
    if (impl->items.empty()) {
        std::fprintf(stderr, "[hdtex] no usable texture packs; HD textures off\n");
        return nullptr;
    }
    impl->slots.resize(impl->items.size());
    std::fprintf(stderr, "[hdtex] HD textures on: %zu replacements from %zu pack(s)\n", impl->items.size(), impl->packs.size());
    return std::unique_ptr<Runtime>(new Runtime(std::move(impl)));
}

void Runtime::attach(ParallelGS::GSInterface &gs, Vulkan::Device &device, const void *initialVram)
{
    Impl *p = impl.get();
    p->gs = &gs;
    p->device = &device;
    p->budget = 64ll << 20;
    p->loader = std::thread([p] { p->loaderLoop(); });
    gs.set_texture_replacement([p](const ParallelGS::GSInterface::TextureReplacementQuery &q) { return p->classify(q); },
                               [p](uint64_t id) { return p->lookup(id); }, initialVram);
}

void Runtime::endFrame(unsigned long long guestFrame)
{
    Impl *p = impl.get();
    // New replacement images per frame: about 64 MB, so a scene change spreads its uploads over a few frames.
    p->budget = 64ll << 20;
    if (p->offFrom >= 0)
        p->off.store((long long)guestFrame >= p->offFrom, std::memory_order_relaxed);
    if (guestFrame / 250 != p->lastLogged) {
        p->lastLogged = guestFrame / 250;
        std::fprintf(stderr, "[hdtex] frame=%llu looked=%llu replaced=%llu loaded=%llu (%.1f MB) unsupported=%llu\n", guestFrame,
                     (unsigned long long)p->looked, (unsigned long long)p->matched, (unsigned long long)p->made,
                     p->madeBytes / 1048576.0, (unsigned long long)p->unsupported);
    }
}

} // namespace hdtex
