// Native boot test for Maximo: Ghosts to Glory (SLES-50703).
//
// Runs the recompiled executable from _start through main on the PS2Recomp runtime, headless, with
// the IOP-side services replaced at the EE SDK boundary by logging stubs. Nothing is rendered.
//
// usage: maximo_boot <disc folder containing SLES_507.03> [seconds=20] [log=boot_log.txt]
//
// What is replaced (addresses identified from SLES_507.03, see BOOT_FINDINGS.md):
//   SIF / IOP     sceSifInitRpc, sceSifBindRpc, sceSifCallRpc, sceSifCheckStatRpc, sceSifRebootIop,
//                 sceSifSyncIop, sceSifLoadModule. IOP services are answered by
//                 small native handlers keyed by RPC server id; anything unknown is logged as such.
//   libcdvd       sceCdInit, sceCdMmode, sceCdSearchFile, sceCdRead, sceCdSync, sceCdSyncS,
//                 sceCdDiskReady, sceCdSt*: the runtime's host-file implementation, reading the
//                 extracted disc folder. Streams read past the end of their file (zeros beyond
//                 it), as the drive does: Maximo always streams whole 100-sector banks.
//   libpad        scePadInit, scePadPortOpen, scePadRead, scePadGetState, scePadPortClose, scePadEnd:
//                 the runtime's implementation (no controller attached).
//   libmc         sceMcInit: the runtime's implementation.
//   libc          printf (to the log), __assert (logged, run stops).
// Everything else, including the game's own GS/DMA register programming, runs as recompiled code
// against the runtime's memory map; every hardware register access is logged by the MMIO hook.

#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"
#include "ps2_stubs.h"
#include "runtime/ee_scheduler.h"
#include "runtime/ps2_host_profile.h"
#include "runtime/ps2_vu1_overlap_model.h"
#include "runtime/ps2_vu1_thread.h"
#include "runtime/ps2_pacing_trace.h"
#include "runtime/ps2_exit_reason.h"
#include "runtime/ps2_display_aspect.h"
#include "runtime/gs/gs_cpu_profile.h"
#include "vu1_capture.h"
#ifdef MAXIMO_ENABLE_GPU_SHADOW
#include "gpu_shadow.h"
extern std::atomic<uint64_t> g_gpuHostSkippedGifPackets; // (gpu_shadow.cpp)
#include "runtime/gs/gs_backend.h"
#endif

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <winsock2.h> // NETPLAY (UDP)
#include <ws2tcpip.h>
#include <dbghelp.h>
#ifdef _MSC_VER
#pragma comment(lib, "ws2_32.lib")
#endif

#include <algorithm>
#include <atomic>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <random>
#include <unordered_set>
#include <set>
#include <string>
#include <unordered_map>
#include <utility>
#include <condition_variable>
#include <thread>
#include <vector>
#include "hdtex/hdtex_format.h"
#define MINIZ_NO_ZLIB_COMPATIBLE_NAMES
#include "miniz.h" // NETPLAY: the host's picture, compressed for the joining game
#ifdef MAXIMO_HAS_FFMPEG
#include <deque>
#include "runtime/ps2_ipu.h"
extern "C"
{
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
}
#endif

// The release this build is for (USA, Japan or PAL) and where each game address is in it (maximo_region.h).
#include "maximo_region.h"
using maximo_region::GA;
using maximo_region::GW;

namespace ps2_stubs
{
    void setCdStreamPastFileEnd(bool enabled); // runtime CD layer (boot-runtime branch)
    // Pad test hooks (Pad.cpp): override what scePadRead reports. Buttons are active-low.
    void setPadOverrideState(uint16_t buttons, uint8_t lx, uint8_t ly, uint8_t rx, uint8_t ry);
    void clearPadOverrideState();
}

extern const uint32_t g_ps2RecompiledFunctionTableBase;
extern const uint32_t g_ps2RecompiledFunctionTableSlotCount;
extern PS2Runtime::RecompiledFunction g_ps2RecompiledFunctionTable[];

// raylib (linked in with the runtime): PLAYER 2 reads the second gamepad itself.
extern "C" bool IsGamepadAvailable(int gamepad);
extern "C" bool IsGamepadButtonDown(int gamepad, int button);
extern "C" float GetGamepadAxisMovement(int gamepad, int axis);
extern "C" bool IsKeyDown(int key);
extern "C" int GetCurrentMonitor(void);
extern "C" int GetMonitorRefreshRate(int monitor); // (MAXIMO_FRAME_RATE=refresh)

namespace
{
    namespace fs = std::filesystem;

    // ---- logging -------------------------------------------------------------------------------

    FILE *g_log = stdout;
    std::mutex g_logMutex;
    auto g_start = std::chrono::steady_clock::now();

    void logf(const char *fmt, ...)
    {
        std::lock_guard<std::mutex> lock(g_logMutex);
        const double t = std::chrono::duration<double>(std::chrono::steady_clock::now() - g_start).count();
        std::fprintf(g_log, "[%9.4f] ", t);
        va_list ap;
        va_start(ap, fmt);
        std::vfprintf(g_log, fmt, ap);
        va_end(ap);
        std::fputc('\n', g_log);
        // Written to the disk at most four times a second instead of after every line: a flush is a system call
        // on the game thread, and the log gets dozens of lines a second (sound slots, pad probes, survival). The
        // crash handler and the exit flush the rest.
        static std::chrono::steady_clock::time_point lastFlush{};
        const auto now = std::chrono::steady_clock::now();
        if (now - lastFlush > std::chrono::milliseconds(250))
        {
            std::fflush(g_log);
            lastFlush = now;
        }
    }

    PS2Runtime *g_runtime = nullptr;
#ifdef MAXIMO_ENABLE_GPU_SHADOW
    std::unique_ptr<GpuShadow> g_gpuShadow;
    std::vector<uint64_t> g_gpuShadowFrames; // capture frames, ascending; the run stops after the last
    // MAXIMO_GS_BACKEND=gpu: paraLLEl-GS renders and supplies the window frames; the CPU GS keeps only register,
    // privileged-register and interrupt semantics (GSDiscardBackend). g_gpuFailed records a fatal GPU-mode error.
    bool g_gpuMode = false;
    std::atomic<bool> g_gpuFailed{false};

    // GPU mode: does a GIF packet hold anything the CPU GS frontend must see? Only A+D register writes can set
    // SIGNAL, FINISH, LABEL, the transfer registers or anything else the frontend answers for; PACKED geometry
    // (PRIM, RGBAQ, ST, UV, XYZ...) and REGLIST data only draw, which the GPU does. IMAGE data and anything
    // that does not parse cleanly go to the frontend as before.
    bool gifPacketNeedsCpuGs(const uint8_t *data, uint32_t size)
    {
        uint32_t pos = 0;
        while (pos + 16u <= size)
        {
            uint64_t lo, hi;
            std::memcpy(&lo, data + pos, 8);
            std::memcpy(&hi, data + pos + 8, 8);
            pos += 16u;
            const uint32_t nloop = static_cast<uint32_t>(lo & 0x7FFFu);
            const uint32_t flg = static_cast<uint32_t>((lo >> 58) & 3u);
            uint32_t nreg = static_cast<uint32_t>((lo >> 60) & 0xFu);
            if (nreg == 0u)
                nreg = 16u;
            if (flg != 0u && flg != 1u)
                return true; // IMAGE (or the IMAGE-like FLG 3)
            if (flg == 0u)
            {
                for (uint32_t r = 0; r < nreg; ++r)
                    if (((hi >> (4u * r)) & 0xFu) == 0xEu)
                        return true; // A+D
                pos += nloop * nreg * 16u;
            }
            else
                pos += ((nloop * nreg + 1u) / 2u) * 16u; // REGLIST: two registers per qword
            if (pos > size)
                return true;
        }
        return pos != size;
    }

    // Raster backend for GPU mode. Draws, CLUT loads and uploads are dropped (the GPU device receives the same
    // packets). Anything that needs the CPU's VRAM contents cannot be answered and fails the run instead.
    class GSDiscardBackend final : public GSRasterBackend
    {
    public:
        void Initialize(uint8_t *, uint32_t) override {}
        void Reset() override {}
        void Submit(const GSPrimitiveBatch &) override {}
        void LoadClut(const GSTex0Reg &, const GSTexClutReg &) override {}
        void BeginTransfer(const GSTransferCommand &command) override
        {
            if (command.direction == 1u)
                fail("local-to-host transfer");
        }
        void UploadImage(const uint8_t *, uint32_t) override {}
        void Flush() override {}
        void TextureFlush() override {}
        void Sync(GSSyncReason) override {}
        PresentationFrame Present(const GSPresentationRequest &) override { return {}; }
        bool ClearFramebuffer(const GSContext &, uint32_t) override
        {
            fail("HLE framebuffer clear");
            return false;
        }
        uint32_t ConsumeLocalToHostBytes(uint8_t *, uint32_t) override
        {
            fail("local-to-host read");
            return 0u;
        }
        uint32_t ReadVram(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t) const override
        {
            fail("CPU VRAM read");
            return 0u;
        }
        void WriteVram(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t) override { fail("CPU VRAM write"); }
        void SnapshotVram(std::vector<uint8_t> &out) const override
        {
            fail("CPU VRAM snapshot");
            out.clear();
        }
        GSTransferSnapshot GetTransferSnapshot() const override { return {}; }

    private:
        static void fail(const char *what)
        {
            if (!g_gpuFailed.exchange(true))
                std::fprintf(stderr, "[gpu] %s is not supported with MAXIMO_GS_BACKEND=gpu; stopping\n", what);
            if (g_runtime)
                g_runtime->requestStop();
        }
    };
#endif
    uint8_t *g_rdram = nullptr;
    fs::path g_disc;

    uint32_t reg(R5900Context *ctx, int r) { return GPR_U32(ctx, r); }

    void ret(R5900Context *ctx, int32_t v0)
    {
        SET_GPR_S32(ctx, 2, v0);
        ctx->pc = GPR_U32(ctx, 31);
    }

    uint8_t *mem(uint32_t addr) { return g_rdram + (addr & PS2_RAM_MASK); }

    uint32_t rd32(uint32_t addr)
    {
        uint32_t v;
        std::memcpy(&v, mem(addr), 4);
        return v;
    }

    void wr32(uint32_t addr, uint32_t v) { std::memcpy(mem(addr), &v, 4); }

    std::string cstr(uint32_t addr, size_t max = 256)
    {
        std::string s;
        if (addr == 0)
            return "(null)";
        for (size_t i = 0; i < max; ++i)
        {
            const char c = static_cast<char>(*mem(addr + static_cast<uint32_t>(i)));
            if (c == 0)
                break;
            s += c;
        }
        return s;
    }

    std::string printable(std::string s)
    {
        for (char &c : s)
            if (c == '\n' || c == '\r')
                c = '|';
            else if (static_cast<unsigned char>(c) < 0x20)
                c = '.';
        return s;
    }

    // ---- execution trace: every guest function entry passes through traceEntry ------------------

    std::vector<PS2Runtime::RecompiledFunction> g_original;
    std::vector<uint32_t> g_entryCount;
    std::array<uint32_t, 128> g_ring{};
    uint32_t g_ringPos = 0;
    uint32_t g_distinct = 0;
    uint32_t g_lastGameFn = 0; // last entered function below the SDK range
    constexpr uint32_t kSdkStart = GA(0x00190000u);

    uint32_t slotOf(uint32_t pc) { return (pc - g_ps2RecompiledFunctionTableBase) >> 2; }

    // Interrupt handlers (and what they call) are counted but kept out of the ring, so the ring
    // shows what the main thread was doing.
    bool g_inHandler = false;
    bool isHandlerEntry(uint32_t pc) { return pc == GA(0x00160960u) || pc == GA(0x00160B40u); }

    // Memory guard: a word that must not change (set from the command line); checked on every
    // function entry, so the first entry after a stray write is reported with the call ring.
    uint32_t g_guardAddr = 0, g_guardValue = 0;
    bool g_guardTripped = false;
    void dumpRing(const char *why);

    void checkGuard(uint32_t pc)
    {
        if (g_guardAddr == 0 || g_guardTripped)
            return;
        uint32_t v;
        std::memcpy(&v, g_rdram + g_guardAddr, 4);
        if (v != g_guardValue)
        {
            g_guardTripped = true;
            logf("GUARD 0x%06X changed 0x%08X -> 0x%08X, noticed on entry to 0x%06X", g_guardAddr, g_guardValue, v, pc);
            dumpRing("guard");
        }
    }

    // Report a write of [dst, dst+len) that covers the guarded word, before it happens.
    void checkGuardWrite(const char *who, uint32_t dst, uint32_t len, uint32_t ra)
    {
        if (g_guardAddr != 0 && g_guardAddr >= (dst & PS2_RAM_MASK) && g_guardAddr < (dst & PS2_RAM_MASK) + len)
            logf("GUARD write by %s: dst=0x%07X len=0x%X covers 0x%06X [ra=0x%06X]", who, dst, len, g_guardAddr, ra);
    }

    // Value watch (diagnostic): MAXIMO_VALWATCH=first:last:addr[,addr...] reports every change of these words, as
    // noticed on function entry, while the pad-read clock is in [first, last]. Unlike the guard it re-arms.
    uint64_t g_padReads = 0; // scePadRead calls so far: the scripted-input clock
    struct ValWatch
    {
        uint32_t addr, value;
    };
    std::vector<ValWatch> g_valWatches;
    uint64_t g_valWatchFirst = 0, g_valWatchLast = 0;
    uint32_t g_valWatchPrevPc = 0, g_valWatchPrevRa = 0;

    void checkValWatches(uint32_t pc, uint32_t ra)
    {
        if (g_valWatches.empty() || g_padReads < g_valWatchFirst || g_padReads > g_valWatchLast)
            return;
        for (ValWatch &w : g_valWatches)
        {
            uint32_t v;
            std::memcpy(&v, g_rdram + w.addr, 4);
            if (v == w.value)
                continue;
            float before, after;
            std::memcpy(&before, &w.value, 4);
            std::memcpy(&after, &v, 4);
            logf("VALWATCH #%llu 0x%06X %g -> %g (0x%08X), noticed entering 0x%06X [ra=0x%06X], previous entry 0x%06X "
                 "[ra=0x%06X]",
                 static_cast<unsigned long long>(g_padReads), w.addr, before, after, v, pc, ra, g_valWatchPrevPc,
                 g_valWatchPrevRa);
            w.value = v;
        }
        g_valWatchPrevPc = pc;
        g_valWatchPrevRa = ra;
    }

    // ---- heap and crash diagnostics -------------------------------------------------------------------------------
    // The game allocates through 0x181DD0 and 0x181DC0 (both into func_001812A0 on the heap object 0x2393B0), out of
    // one block crt0 sizes to the end of the heap (EndOfHeap). A request that cannot be met returns 0, which the game
    // mostly does not check. Logged: every failure (capped), and the highest address handed out each time it grows
    // by 256 KB, with what is left before the heap end. watchEntry feeds these from the two allocator entries.
    // When its free list has no block big enough, the pool allocator (func_001819D0) does not fail: it hands out the
    // list's end sentinel, i.e. memory past the end of the pool, and the game then corrupts memory or freezes walking
    // a broken free list. The heap object 0x2393B0 holds three pools (at +0x20 the main one, +0x140, +0x260), each
    // with its end sentinel at +0x110; a result of sentinel + 0x10 is that overrun, reported as OUT OF MEMORY. The
    // whole block runs from the heap start (0x2F5E00) for the size func_001811D0 stored at +0 of the heap object.
    constexpr uint32_t kGameAllocFn = GA(0x00181DD0u), kGameAllocFn2 = GA(0x00181DC0u);
    constexpr uint32_t kGamePoolStart = GA(0x002F5E00u), kGamePoolObject = GA(0x002393B0u);
    constexpr uint32_t kHeapReportStep = 256u * 1024u;
    uint32_t g_heapHigh = 0, g_heapNextReport = 0, g_heapEnd = 0;
    uint64_t g_heapAllocs = 0, g_heapFails = 0, g_heapOverruns = 0;

    int64_t heapLeftKb() { return (static_cast<int64_t>(g_heapEnd) - static_cast<int64_t>(g_heapHigh)) / 1024; }

    void heapNote(PS2Runtime *runtime, uint32_t size, uint32_t result, uint32_t ra)
    {
        (void)runtime;
        const uint32_t poolSize = rd32(kGamePoolObject);
        g_heapEnd = poolSize ? kGamePoolStart + poolSize : runtime->guestHeapLimit();
        ++g_heapAllocs;
        if (result != 0 && poolSize)
            for (const uint32_t pool : {0x20u, 0x140u, 0x260u})
            {
                const uint32_t sentinel = rd32(kGamePoolObject + pool + 0x110u);
                if (sentinel == 0 || (result & PS2_RAM_MASK) != ((sentinel + 0x10u) & PS2_RAM_MASK))
                    continue;
                if (g_heapOverruns++ < 8)
                    logf("heap: OUT OF MEMORY: the game's pool at +0x%X (ends 0x%07X) is full; %u bytes were handed "
                         "out past its end [ra=0x%06X]. The game corrupts memory or freezes from here: use fewer "
                         "enemies or more memory.",
                         pool, sentinel, size, ra);
                return;
            }
        if (result == 0)
        {
            if (g_heapFails++ < 32)
                logf("heap: allocation of %u bytes FAILED [ra=0x%06X]; highest block ends at 0x%07X, heap end 0x%07X "
                     "(%lld KB left)",
                     size, ra, g_heapHigh, g_heapEnd, static_cast<long long>(heapLeftKb()));
            return;
        }
        const uint64_t end = static_cast<uint64_t>(result & PS2_RAM_MASK) + size;
        if (end <= g_heapHigh)
            return;
        g_heapHigh = static_cast<uint32_t>(std::min<uint64_t>(end, 0xFFFFFFFFull));
        // Each pool's free space (heap object + pool + 0x104, in 16-byte units; size at +0x100): logged when it
        // first drops below half, a quarter, a tenth and a twentieth of the pool.
        if (poolSize)
        {
            static const uint32_t kPools[3] = {0x20u, 0x260u, 0x140u};
            static int stage[3] = {0, 0, 0};
            static const uint32_t kPercent[4] = {50, 25, 10, 5};
            for (int i = 0; i < 3; ++i)
            {
                const uint64_t total = rd32(kGamePoolObject + kPools[i] + 0x100u);
                const uint64_t freeUnits = rd32(kGamePoolObject + kPools[i] + 0x104u);
                if (total == 0 || stage[i] >= 4 || freeUnits * 100 >= total * kPercent[stage[i]])
                    continue;
                while (stage[i] < 4 && freeUnits * 100 < total * kPercent[stage[i]])
                    ++stage[i];
                logf("heap: pool +0x%X under %u%% free: %llu KB of %llu KB left [ra=0x%06X]", kPools[i],
                     kPercent[stage[i] - 1], static_cast<unsigned long long>(freeUnits / 64),
                     static_cast<unsigned long long>(total / 64), ra);
            }
        }
        if (g_heapHigh < g_heapNextReport)
            return;
        g_heapNextReport = g_heapHigh + kHeapReportStep;
        logf("heap: high-water 0x%07X after %llu allocations, %lld KB left before the heap end 0x%07X", g_heapHigh,
             static_cast<unsigned long long>(g_heapAllocs), static_cast<long long>(heapLeftKb()), g_heapEnd);
    }

    // A guest function that returns (or jumps) to address 0 ends the game thread ("the game thread finished"),
    // usually through a null function pointer or a smashed return address. The first one is reported in full.
    bool g_zeroJumpReported = false;

    void reportZeroJump(R5900Context *ctx, uint32_t fn, uint32_t entryRa, uint32_t entrySp)
    {
        if (g_zeroJumpReported)
            return;
        g_zeroJumpReported = true;
        logf("CRASH: guest function 0x%06X (return address 0x%06X) left for address 0", fn, entryRa);
        logf("  at entry sp=0x%07X; now sp=0x%07X ra=0x%08X v0=0x%08X v1=0x%08X", entrySp, reg(ctx, 29), reg(ctx, 31),
             reg(ctx, 2), reg(ctx, 3));
        logf("  a0-a3 %08X %08X %08X %08X  t9 %08X  gp %08X", reg(ctx, 4), reg(ctx, 5), reg(ctx, 6), reg(ctx, 7),
             reg(ctx, 25), reg(ctx, 28));
        logf("  s0-s7 %08X %08X %08X %08X %08X %08X %08X %08X", reg(ctx, 16), reg(ctx, 17), reg(ctx, 18),
             reg(ctx, 19), reg(ctx, 20), reg(ctx, 21), reg(ctx, 22), reg(ctx, 23));
        const uint32_t sp = reg(ctx, 29) & PS2_RAM_MASK;
        if (sp + 64 <= PS2_RAM_SIZE)
        {
            uint32_t w[16];
            std::memcpy(w, g_rdram + sp, sizeof w);
            logf("  stack %08X %08X %08X %08X %08X %08X %08X %08X", w[0], w[1], w[2], w[3], w[4], w[5], w[6], w[7]);
            logf("        %08X %08X %08X %08X %08X %08X %08X %08X", w[8], w[9], w[10], w[11], w[12], w[13], w[14],
                 w[15]);
        }
        std::string line;
        for (size_t i = g_ring.size() - 32; i < g_ring.size(); ++i)
        {
            char b[16];
            std::snprintf(b, sizeof b, " %06X", g_ring[(g_ringPos + i) % g_ring.size()]);
            line += b;
        }
        logf("  last 32 functions entered (oldest first):%s", line.c_str());
        logf("  heap: %llu allocations, %llu failed, high-water 0x%07X, heap end 0x%07X (%lld KB left)",
             static_cast<unsigned long long>(g_heapAllocs), static_cast<unsigned long long>(g_heapFails), g_heapHigh,
             g_heapEnd, static_cast<long long>(heapLeftKb()));
    }

    bool g_traceFull = false; // MAXIMO_TRACE=full, a memory guard or a value watch (set at start)
    void traceEntryLight(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime);
    void traceEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        // (the hooks - watch points, milestones, the frame hooks - call this one: the light form unless tracing)
        if (!g_traceFull)
        {
            traceEntryLight(rdram, ctx, runtime);
            return;
        }
        const uint32_t pc = ctx->pc;
        checkGuard(pc);
        checkValWatches(pc, reg(ctx, 31));
        if (pc == GA(0x0019B100u)) // memcpy(dst, src, n)
            checkGuardWrite("memcpy", reg(ctx, 4), reg(ctx, 6), reg(ctx, 31));
        const uint32_t slot = slotOf(pc);
        if (g_entryCount[slot]++ == 0)
            ++g_distinct;
        const bool handler = isHandlerEntry(pc);
        if (handler)
            g_inHandler = true;
        if (!g_inHandler)
        {
            g_ring[g_ringPos++ % g_ring.size()] = pc;
            if (pc < kSdkStart)
                g_lastGameFn = pc;
        }
        const uint32_t entryRa = reg(ctx, 31), entrySp = reg(ctx, 29);
        g_original[slot](rdram, ctx, runtime);
        // Interrupt handlers and thread roots start with ra = 0 and end by returning there, and DMA/interrupt callbacks
        // run on the callback stacks at 0x0F0000-0x100000 and end the same way; only other code that still ends up at
        // 0 is a crash.
        if (ctx->pc == 0u && entryRa != 0u && !(entrySp >= 0x000F0000u && entrySp <= 0x00100000u) &&
            !g_zeroJumpReported)
            reportZeroJump(ctx, pc, entryRa, entrySp);
        if (handler)
            g_inHandler = false;
    }

    // The same without the diagnostics nobody asked for in this run (the memory guard, the value watches, the
    // per-function entry counts): only the call ring and the jump-to-0 report, which crash reports need. Every guest
    // function call goes through one of these two, so the lighter one is the default (MAXIMO_TRACE=full, or a
    // guard / value watch on the command line, gives the full one).
    void traceEntryLight(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t pc = ctx->pc;
        const bool handler = isHandlerEntry(pc);
        if (handler)
            g_inHandler = true;
        if (!g_inHandler)
        {
            g_ring[g_ringPos++ % g_ring.size()] = pc;
            if (pc < kSdkStart)
                g_lastGameFn = pc;
        }
        const uint32_t entryRa = reg(ctx, 31), entrySp = reg(ctx, 29);
        g_original[slotOf(pc)](rdram, ctx, runtime);
        if (ctx->pc == 0u && entryRa != 0u && !(entrySp >= 0x000F0000u && entrySp <= 0x00100000u) &&
            !g_zeroJumpReported)
            reportZeroJump(ctx, pc, entryRa, entrySp);
        if (handler)
            g_inHandler = false;
    }

    // Milestones: the steps of main (0x1000C0) in order. Entering step N+1 proves step N returned.
    struct Milestone
    {
        uint32_t addr;
        const char *what;
        bool reached = false;
    };
    std::vector<Milestone> g_milestones = {
        {GA(0x00100008u), "_start (crt0)"},
        {GA(0x001ACCF0u), "crt0: libc/kernel init"},
        {GA(0x001000C0u), "main"},
        {GA(0x0016D450u), "main: engine init"},
        {GA(0x001B3960u), "main: sceSifRebootIop(IOPRP22.IMG)"},
        {GA(0x001B3460u), "main: first sceSifLoadModule"},
        {GA(0x00158260u), "main: graphics init (GS/DMA setup)"},
        {GA(0x001C0110u), "main: scePadInit"},
        {GA(0x001C0370u), "main: scePadPortOpen"},
        {GA(0x001B6D40u), "main: sceMcInit"},
        {GA(0x00147860u), "main: font load (PSXDATA\\GAME\\FONT.PRS/PRT)"},
        {GA(0x001202C0u), "main: FLAG.PSX load"},
        {GA(0x0016D760u), "main: sound init"},
        {GA(0x0014B2C0u), "main: fade"},
        {GA(0x0011F160u), "main: frontend"},
        {GA(0x0014AD60u), "main: post-frontend"},
        {GA(0x00147FE0u), "main: 0x147FE0"},
        {GA(0x0014AEE0u), "main: game session"},
        {GA(0x001C0558u), "main: shutdown (scePadPortClose)"},
        {GA(0x001ABAC0u), "exit() (main returned or exit called)"},
        {GA(0x00197F50u), "abort()"},
    };

    void milestoneEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t pc = ctx->pc;
        const uint32_t ra = reg(ctx, 31);
        const bool fromMain = ra >= GA(0x001000C0u) && ra < GA(0x00100450u);
        for (Milestone &m : g_milestones)
            if (m.addr == pc && !m.reached && (fromMain || std::strncmp(m.what, "main:", 5) != 0))
            {
                m.reached = true;
                logf("MILESTONE 0x%06X %s (ra=0x%06X)", pc, m.what, reg(ctx, 31));
            }
        traceEntry(rdram, ctx, runtime);
    }

    // Watch points: game functions whose entry (and arguments) are logged on every call, capped.
    struct Watch
    {
        const char *what;
        int nargs;
        bool logReturn = false; // log v0 whenever it differs from the previous completed call
        uint64_t calls = 0;
        int64_t lastReturn = INT64_MIN;
    };
    // Frame timing. A frame is the interval between entries to sceGsSyncV (0x1AAA18), which the game calls once
    // per displayed frame (0x158430). The wait runs from that entry to the next VBlank (the runtime timestamps
    // it); "busy" is the rest of the frame, split into VU1 and GS (runtime profile scopes) and EE (the remainder).
    // The wrapper cannot time the call itself: the dispatcher leaves the polling loop at cycle checkpoints.
    // With the GS worker (PS2X_GS_THREAD=1) GS time is spent on the worker, in parallel; the game thread's share
    // is the stall (kGsWait) it spends waiting on the worker, and EE is busy minus VU1 minus that stall.
    // With the VU1 worker (PS2X_VU1_THREAD=1) VU1 and GS time are both spent on the worker; the game thread's share
    // is its wait for the worker (kVu1Wait, reported in the VU1 column) and EE is busy minus that wait.
    struct FrameStats
    {
        uint64_t frames = 0, slowFrames = 0;
        double frameMs = 0, worstFrameMs = 0, busyMs = 0, worstBusyMs = 0, waitMs = 0, vu1Ms = 0, gsMs = 0, stallMs = 0;
        double worstVu1Ms = 0, worstGsMs = 0; // split of the worst busy frame
        double vu1WaitMs = 0;                 // game thread waiting for the VU1 worker
        uint64_t presentNs0 = 0;
        uint64_t vblanks = 0; // guest VBlanks elapsed over these frames (guest time, see logFrameStats)
        std::vector<float> frameSamples, busySamples; // per frame, for percentiles
    };
    FrameStats g_perfWindow, g_perfTotal;
    uint64_t g_lastSyncEntryNs = 0, g_perfWindowStartNs = 0, g_lastVu1Ns = 0, g_lastGsNs = 0, g_lastStallNs = 0;
    uint64_t g_lastVBlankTick = 0;
    bool g_asyncGs = false;
    bool g_vu1Thread = false;
    uint64_t g_lastVu1WaitNs = 0;
    double vblankMs() // the runtime's current field period (PAL 20 ms, NTSC 16.7 ms)
    {
        const double hz = g_runtime ? g_runtime->eeScheduler().vblankHz() : 0.0;
        return hz > 0.0 ? 1000.0 / hz : 1000.0 / 60.0;
    }

    void addFrame(FrameStats &s, double frameMs, double waitMs, double vu1Ms, double gsMs, double stallMs,
                  uint64_t vblanks, double vu1WaitMs)
    {
        const double busyMs = frameMs - waitMs;
        s.vu1WaitMs += vu1WaitMs;
        s.frameSamples.push_back(static_cast<float>(frameMs));
        s.busySamples.push_back(static_cast<float>(busyMs));
        ++s.frames;
        s.vblanks += vblanks;
        s.frameMs += frameMs;
        s.waitMs += waitMs;
        s.busyMs += busyMs;
        s.vu1Ms += vu1Ms;
        s.gsMs += gsMs;
        s.stallMs += stallMs;
        s.worstFrameMs = std::max(s.worstFrameMs, frameMs);
        if (frameMs > vblankMs() * 1.5)
            ++s.slowFrames;
        if (busyMs > s.worstBusyMs)
        {
            s.worstBusyMs = busyMs;
            s.worstVu1Ms = vu1Ms;
            s.worstGsMs = gsMs;
        }
    }

    double percentile(std::vector<float> samples, double q)
    {
        if (samples.empty())
            return 0.0;
        const size_t k = static_cast<size_t>(q * static_cast<double>(samples.size() - 1));
        std::nth_element(samples.begin(), samples.begin() + static_cast<std::ptrdiff_t>(k), samples.end());
        return samples[k];
    }

    uint64_t mmioAccesses(uint32_t phys, bool writes); // MMIO hook counters, defined with g_mmio below
    void logAudioWindow(const char *label, double seconds); // [audio-perf], defined with the audio section below

    void logFrameStats(const char *label, const FrameStats &s, double seconds)
    {
        if (s.frames == 0)
            return;
        const double n = static_cast<double>(s.frames);
        const double presentMs =
            static_cast<double>(ps2_host_profile::total(ps2_host_profile::kPresent) - s.presentNs0) / 1e6;
        if (g_vu1Thread)
        {
            // Same layout as inline, so the bench parsers keep working: VU1 is the game thread's wait for the VU1
            // worker and GS is 0 (both run on the worker; their times are on the next line).
            logf("[perf] %s frames=%llu fps=%.1f frame avg=%.2fms worst=%.2fms slow(>1.5 VBlank)=%llu | busy avg=%.2fms "
                 "(EE %.2f, VU1 %.2f, GS %.2f) worst=%.2fms (VU1 %.2f, GS %.2f) | VBlank wait avg=%.2fms | "
                 "present %.2fms/s",
                 label, static_cast<unsigned long long>(s.frames), n / seconds, s.frameMs / n, s.worstFrameMs,
                 static_cast<unsigned long long>(s.slowFrames), s.busyMs / n, (s.busyMs - s.vu1WaitMs) / n,
                 s.vu1WaitMs / n, 0.0, s.worstBusyMs, s.worstVu1Ms, s.worstGsMs, s.waitMs / n, presentMs / seconds);
            logf("[perf] %s VU1 worker (in parallel): VU1 %.2f GS %.2f ms/frame | game thread waits for it %.2f ms/frame",
                 label, s.vu1Ms / n, s.gsMs / n, s.vu1WaitMs / n);
        }
        else
        logf("[perf] %s frames=%llu fps=%.1f frame avg=%.2fms worst=%.2fms slow(>1.5 VBlank)=%llu | busy avg=%.2fms "
             "(EE %.2f, VU1 %.2f, %s %.2f) worst=%.2fms (VU1 %.2f, GS %.2f) | VBlank wait avg=%.2fms | "
             "present %.2fms/s",
             label, static_cast<unsigned long long>(s.frames), n / seconds, s.frameMs / n, s.worstFrameMs,
             static_cast<unsigned long long>(s.slowFrames), s.busyMs / n,
             (s.busyMs - s.vu1Ms - (g_asyncGs ? s.stallMs : s.gsMs)) / n, s.vu1Ms / n,
             g_asyncGs ? "GS stall" : "GS", (g_asyncGs ? s.stallMs : s.gsMs) / n, s.worstBusyMs, s.worstVu1Ms,
             s.worstGsMs, s.waitMs / n, presentMs / seconds);
        logf("[perf] %s p95 frame=%.2fms busy=%.2fms | p99 frame=%.2fms busy=%.2fms", label,
             percentile(s.frameSamples, 0.95), percentile(s.busySamples, 0.95), percentile(s.frameSamples, 0.99),
             percentile(s.busySamples, 0.99));
        if (g_asyncGs)
            logf("[perf] %s GS worker avg=%.2fms/frame (in parallel with the game thread)", label, s.gsMs / n);
        {
            const VU1Interpreter::AotStats a = VU1Interpreter::aotStats();
            if (a.calls != 0 || a.verified != 0)
                logf("[perf] %s VU1 AOT totals: calls=%llu completed=%llu handovers=%llu notCompiled=%llu verified=%llu "
                     "mismatches=%llu cycles compiled=%llu interpreted=%llu suspended=%llu resumed=%llu",
                     label, static_cast<unsigned long long>(a.calls), static_cast<unsigned long long>(a.completed),
                     static_cast<unsigned long long>(a.handovers), static_cast<unsigned long long>(a.notCompiled),
                     static_cast<unsigned long long>(a.verified), static_cast<unsigned long long>(a.mismatches),
                     static_cast<unsigned long long>(a.compiledCycles),
                     static_cast<unsigned long long>(a.interpretedCycles), static_cast<unsigned long long>(a.suspended),
                     static_cast<unsigned long long>(a.resumed));
        }
        // Guest time: the game runs at full speed when guest VBlanks elapse at the video mode's field rate in wall
        // time. Rendered frames per second alone do not show this (a game may draw once per two fields).
        const double hz = g_runtime ? g_runtime->eeScheduler().vblankHz() : 0.0;
        if (hz > 0.0)
            logf("[perf] %s guest vblanks=%llu (%.2f per frame) guest time %.2fs in %.2fs wall: speed %.1f%% of %.2f Hz",
                 label, static_cast<unsigned long long>(s.vblanks), s.vblanks / n, s.vblanks / hz, seconds,
                 100.0 * s.vblanks / hz / seconds, hz);
    }

    std::atomic<HANDLE> g_gameThread{nullptr}; // the host thread running guest code, for the sampling profiler
    std::atomic<HANDLE> g_vu1WorkerHandle{nullptr};  // the VU1 / GS worker thread (the one GIF packets come from), likewise
    void noteVu1Thread()
    {
        static thread_local bool noted = false;
        if (noted)
            return;
        noted = true;
        if (g_vu1WorkerHandle.load(std::memory_order_relaxed))
            return;
        HANDLE self = nullptr;
        DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &self,
                        THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, 0);
        g_vu1WorkerHandle.store(self, std::memory_order_release);
    }
    void startMenuProfile(); // PERFORMANCE PROFILE in the debug menu (defined with the sampling profiler)
    std::atomic<int> g_menuProfileState{0}; // 0 idle, 1 sampling, 2 writing the report
    std::atomic<uint64_t> g_guestFrame{0};     // sceGsSyncV calls so far, for MAXIMO_PROFILE=f<first>,<last>
    uint64_t g_lastGpuVsyncTick = 0;           // VBlank tick of the last GPU device vsync (game thread only)

    // ---- widescreen (MAXIMO_ASPECT=16:9, WIDESCREEN.md) -----------------------------------------------------------
    // func_15B100 builds the 3D camera's projection; its only caller (0x147860) passes the horizontal scale 1.0 in
    // $f13. Scaling it by (4/3)/(16/9) shows more at the sides and keeps the vertical framing (Hor+).
    constexpr float kWideXScale = 0.75f;
    // func_16AE00 sets up the visibility frustum whenever the 3D world is drawn. It reads the horizontal angle
    // factor (1.4) from this data word; widened by 4/3 so objects at the new screen edges are not culled.
    constexpr uint32_t kCullFactorAddr = GA(0x001E25F4u);
    constexpr float kWideCullFactor = 1.4f * 4.0f / 3.0f;
    bool g_widescreen = false;

    // ---- on-screen object budget (MAXIMO_OBJECT_BUDGET=more|101..1000) --------------------------------------------
    // The script VM stops running an entity once its load class's running weight total would pass the class limit
    // (CVirtualMachine::ExecuteApparitions), so a busy level drops enemies and objects. Two places set the limits and
    // both are needed (ported from the randomizer's patch_object_budget, located in this PAL image):
    //  - the constructor func_00102120 stores 100 into classes 1..5 at this+0x11C..0x12C (class 6, 1000 at +0x130, is
    //    left alone). Its code is recompiled, so patching the immediate would do nothing: the wrapper lets the game
    //    build the object, then rewrites those five words.
    //  - mglRunLevel (func_00121050) feeds LevelTable row +0x1E (u16) to SetLoadClassLimit (0x103750) for class 3 at
    //    every level load, which overrides the constructor's value for enemies. The LevelTable is data at 0x1DA030
    //    (31 rows, stride 0x34). Its rows are scaled by budget/100, so the tighter Castle row (80) stays tighter.
    // The budget is a weight total, not an object count, and it only lets more of a level's own entities run.
    // The engine's object lists (DarkEngine+0x6B40, capacity 320, and +0x6B50, capacity 384, set up in func_00147470
    // through func_0018B8E0) are growable vectors: func_0018B980 adds 4 slots and copies when one fills, so a larger
    // budget cannot overflow them. The one fixed limit found is the per-level entity flag table of 4 rows x 384
    // bytes (func_00120570 and its neighbours, stride baked into the code). It is indexed by entity number, so it
    // only matters if a level gets MORE entities than the 304 its densest level has; a budget does not add any.
    // "more" is 200, the value the randomizer tested. Higher numbers are untested; above 1000 is refused.
    constexpr uint32_t kVmCtorFn = GA(0x00102120u);
    constexpr uint32_t kVmClassLimitOff = 0x11Cu; // classes 1..5, one word each
    constexpr uint32_t kVmClassLimitCount = 5;
    constexpr uint32_t kLevelTableAddr = GA(0x001DA030u);
    constexpr uint32_t kLevelTableRows = 31;
    constexpr uint32_t kLevelTableStride = 0x34u;
    constexpr uint32_t kLevelEnemyLimitOff = 0x1Eu;
    constexpr int kObjectBudgetVanilla = 100;
    constexpr int kObjectBudgetMore = 200; // "more": the randomizer's tested value
    constexpr int kObjectBudgetMax = 1000; // refused above this
    int g_objectBudget = kObjectBudgetVanilla;
    bool g_levelLimitsScaled = false;

    // "more" = 200, a number 101..1000, unset / "vanilla" / 100 = vanilla. Anything else is refused (vanilla).
    int parseObjectBudget(const char *s)
    {
        if (!s || !*s || _stricmp(s, "vanilla") == 0)
            return kObjectBudgetVanilla;
        if (_stricmp(s, "more") == 0)
            return kObjectBudgetMore;
        char *end = nullptr;
        const long v = std::strtol(s, &end, 10);
        if (end == s || *end || v < kObjectBudgetVanilla || v > kObjectBudgetMax)
        {
            logf("object budget: MAXIMO_OBJECT_BUDGET=%s refused (use more, vanilla or %d..%d); keeping vanilla", s,
                 kObjectBudgetVanilla, kObjectBudgetMax);
            return kObjectBudgetVanilla;
        }
        return static_cast<int>(v);
    }

    void objectBudgetCtorEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t self = reg(ctx, 4) & PS2_RAM_MASK; // a0 = this, before the original clobbers registers
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        for (uint32_t i = 0; i < kVmClassLimitCount; ++i)
        {
            uint8_t *p = rdram + self + kVmClassLimitOff + i * 4;
            uint32_t cur = 0;
            std::memcpy(&cur, p, sizeof(cur));
            if (cur != static_cast<uint32_t>(kObjectBudgetVanilla))
            {
                logf("object budget: class %u limit is %u, not %d; left alone", i + 1, cur, kObjectBudgetVanilla);
                continue;
            }
            const uint32_t next = static_cast<uint32_t>(g_objectBudget);
            std::memcpy(p, &next, sizeof(next));
        }
        if (g_levelLimitsScaled)
            return;
        uint16_t rows[kLevelTableRows];
        for (uint32_t i = 0; i < kLevelTableRows; ++i)
        {
            std::memcpy(&rows[i], rdram + kLevelTableAddr + i * kLevelTableStride + kLevelEnemyLimitOff, 2);
            if (rows[i] < 1 || rows[i] > kObjectBudgetVanilla) // vanilla rows are 80 or 100
            {
                logf("object budget: LevelTable row %u enemy limit is %u, not a vanilla value; rows left alone", i,
                     static_cast<unsigned>(rows[i]));
                return;
            }
        }
        for (uint32_t i = 0; i < kLevelTableRows; ++i)
        {
            const uint16_t scaled = static_cast<uint16_t>(
                (static_cast<int>(rows[i]) * g_objectBudget + kObjectBudgetVanilla / 2) / kObjectBudgetVanilla);
            std::memcpy(rdram + kLevelTableAddr + i * kLevelTableStride + kLevelEnemyLimitOff, &scaled, 2);
        }
        g_levelLimitsScaled = true;
        logf("object budget %d: class 1..5 limits and %u LevelTable enemy limits raised%s", g_objectBudget,
             kLevelTableRows, g_objectBudget > kObjectBudgetMore ? " (above 200 is untested)" : "");
    }

    // ---- entity flag table past 384 entities (on with the object budget) ------------------------------------------
    // Every entity a level loads gets a handle from a counter (func_00104780 stores VM+0x9C into object+0 and
    // counts up). Per-entity state bytes (dead, collected, ...) live in a table of a 4-byte header and 4 rows of 384
    // bytes, 0x604 in all; handle h of row r is at table + 4 + r*384 + (h - 1). The 384 is baked into the code, so a
    // level with more than 384 entities would read and write past its row (into the next row, then the checkpoint
    // copy and the save data after it). The tables (PAL image):
    //  - live level: profile+0x21090 (0x25A910), read at load by func_00102480 (row 1: negative = do not spawn) and
    //    by the script VM opcodes at 0x1158B0..0x115B20 through VM+0xE08;
    //  - checkpoint: profile+0x21694 (0x25AF14), copied from the live one by func_0011F740 (level start) and
    //    func_0011F920 (checkpoint) with plain loops, row 1 copied back by func_0011F890 on a restart;
    //  - per-level save slots: profile+0x15923+level*0x184 (header + row 0, 30 levels), copied into the live table by
    //    func_001205E0 and back with memcpy (0x11FF8C); they go to the memory card inside the profile.
    // Accessors: func_00120590 read(table, h, out, row), func_001205C0 write(table, h, value, row), func_00120570
    // clear row, func_001205E0 load from slot, func_00120640 copy one row, func_00120670 count negative bytes of row 0.
    // Handles 1..384 stay exactly as the game has them. A handle above 384 gets its byte here instead, kept per table
    // address, and every copy or clear the game makes (the accessors above, the two loops, memcpy 0x19B100 and memset
    // 0x19B2B8 over a whole row) is mirrored. These bytes live only while the game runs: the memory card keeps 384 per
    // level, so after loading a save the extra entities start fresh (an extra enemy killed before saving comes back).
    // The marker below is what the launcher looks for before it lets a level file grow past 384 entities.
    constexpr const char *kEntityFlagsMarker = "entity flags: extended past 384 entities";
    constexpr uint32_t kFlagRowLen = 384;
    constexpr uint32_t kFlagRows = 4;
    constexpr uint32_t kFlagHandleMax = 65535;
    constexpr uint32_t kProfileLiveOff = 0x21090u;
    constexpr uint32_t kProfileCheckpointOff = 0x21694u;
    constexpr uint32_t kProfileSlotOff = 0x15923u;
    constexpr uint32_t kProfileSlotStride = 0x184u;
    constexpr int kProfileSlots = 30;
    struct ExtFlags
    {
        std::array<std::vector<uint8_t>, kFlagRows> row; // row[r][h - 385]
    };
    std::map<uint32_t, ExtFlags> g_extFlags; // key: table address & PS2_RAM_MASK
    uint64_t g_extFlagWrites = 0;

    uint32_t flagKey(uint32_t addr) { return addr & PS2_RAM_MASK; }

    bool extHandle(uint32_t h, uint32_t row) { return h > kFlagRowLen && h <= kFlagHandleMax && row < kFlagRows; }

    uint8_t extGet(uint32_t table, uint32_t row, uint32_t h)
    {
        const auto it = g_extFlags.find(flagKey(table));
        if (it == g_extFlags.end())
            return 0;
        const std::vector<uint8_t> &v = it->second.row[row];
        const uint32_t i = h - kFlagRowLen - 1;
        return i < v.size() ? v[i] : 0;
    }

    void extSet(uint32_t table, uint32_t row, uint32_t h, uint8_t value)
    {
        std::vector<uint8_t> &v = g_extFlags[flagKey(table)].row[row];
        const uint32_t i = h - kFlagRowLen - 1;
        if (i >= v.size())
        {
            if (value == 0)
                return;
            v.resize(i + 1, 0);
        }
        v[i] = value;
        if (g_extFlagWrites++ == 0)
            logf("entity flags: handle %u (row %u) is past 384 and kept by the recomp", h, row);
    }

    void extCopyRow(uint32_t dst, uint32_t src, uint32_t row)
    {
        if (flagKey(dst) == flagKey(src))
            return;
        const auto it = g_extFlags.find(flagKey(src));
        if (it == g_extFlags.end() || it->second.row[row].empty())
        {
            const auto d = g_extFlags.find(flagKey(dst));
            if (d != g_extFlags.end())
                d->second.row[row].clear();
            return;
        }
        std::vector<uint8_t> copy = it->second.row[row]; // the map may rehash below
        g_extFlags[flagKey(dst)].row[row] = std::move(copy);
    }

    void extClearRow(uint32_t table, uint32_t row)
    {
        const auto it = g_extFlags.find(flagKey(table));
        if (it != g_extFlags.end())
            it->second.row[row].clear();
    }

    // Rows of tables starting in [base, base+n) that lie wholly inside it.
    template <typename F> void extRowsIn(uint32_t base, uint32_t n, F &&f)
    {
        const uint32_t lo = flagKey(base);
        const uint64_t hi = static_cast<uint64_t>(lo) + n;
        for (auto it = g_extFlags.lower_bound(lo); it != g_extFlags.end() && it->first < hi; ++it)
            for (uint32_t r = 0; r < kFlagRows; ++r)
                if (static_cast<uint64_t>(it->first) + 4 + (r + 1) * kFlagRowLen <= hi)
                    f(it->first, r);
    }

    // memcpy 0x19B100 (dst, src, n): a block holding whole rows carries their extra bytes with it (save slots, the
    // profile going to or coming from the memory card buffer), and rows it overwrites lose theirs.
    void extFlagsMemcpyEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (!g_extFlags.empty() && ctx->pc == GA(0x0019B100u)) // not when a paused copy resumes part way
        {
            const uint32_t dst = flagKey(reg(ctx, 4)), src = flagKey(reg(ctx, 5)), n = reg(ctx, 6);
            std::vector<std::pair<uint32_t, std::pair<uint32_t, std::vector<uint8_t>>>> moved;
            extRowsIn(src, n, [&](uint32_t key, uint32_t r) {
                const std::vector<uint8_t> &v = g_extFlags[key].row[r];
                if (!v.empty())
                    moved.push_back({key - src, {r, v}});
            });
            extRowsIn(dst, n, [&](uint32_t key, uint32_t r) { g_extFlags[key].row[r].clear(); });
            for (auto &m : moved)
                g_extFlags[dst + m.first].row[m.second.first] = std::move(m.second.second);
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // memset 0x19B2B8 (dst, value, n) over whole rows.
    void extFlagsMemsetEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (!g_extFlags.empty() && ctx->pc == GA(0x0019B2B8u))
        {
            const uint8_t value = static_cast<uint8_t>(reg(ctx, 5));
            extRowsIn(reg(ctx, 4), reg(ctx, 6), [&](uint32_t key, uint32_t r) {
                std::vector<uint8_t> &v = g_extFlags[key].row[r];
                if (value == 0)
                    v.clear();
                else
                    std::fill(v.begin(), v.end(), value); // only bytes already present; absent ones read as 0
            });
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // func_00120590 (table, h, out, row): *out = flag; returns 1.
    void extFlagsReadEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t h = reg(ctx, 5), row = reg(ctx, 7);
        if (ctx->pc != GA(0x00120590u) || !extHandle(h, row))
        {
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            return;
        }
        *mem(reg(ctx, 6)) = extGet(reg(ctx, 4), row, h);
        ret(ctx, 1);
    }

    // func_001205C0 (table, h, value, row): flag = value; returns 1.
    void extFlagsWriteEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t h = reg(ctx, 5), row = reg(ctx, 7);
        if (ctx->pc != GA(0x001205C0u) || !extHandle(h, row))
        {
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            return;
        }
        extSet(reg(ctx, 4), row, h, static_cast<uint8_t>(reg(ctx, 6)));
        ret(ctx, 1);
    }

    // func_00120570 (table, row): clears the row.
    void extFlagsClearRowEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == GA(0x00120570u) && reg(ctx, 5) < kFlagRows)
            extClearRow(reg(ctx, 4), reg(ctx, 5));
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // func_001205E0 (table, slot): header and row 0 from the slot, rows 1..3 cleared.
    void extFlagsLoadSlotEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == GA(0x001205E0u))
        {
            const uint32_t dst = reg(ctx, 4), src = reg(ctx, 5);
            extCopyRow(dst, src, 0);
            for (uint32_t r = 1; r < kFlagRows; ++r)
                extClearRow(dst, r);
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // func_00120640 (dst, src, row): one row copied.
    void extFlagsCopyRowEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == GA(0x00120640u) && reg(ctx, 6) < kFlagRows)
            extCopyRow(reg(ctx, 4), reg(ctx, 5), reg(ctx, 6));
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // func_00120670 (table): number of negative bytes in row 0 (the level's tally). Done here in full so the extra
    // bytes are counted too; the game's version is a plain loop with no side effects.
    void extFlagsCountEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc != GA(0x00120670u))
        {
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            return;
        }
        const uint32_t table = reg(ctx, 4);
        int32_t n = 0;
        for (uint32_t i = 0; i < kFlagRowLen; ++i)
            if (static_cast<int8_t>(*mem(table + 4 + i)) < 0)
                ++n;
        const auto it = g_extFlags.find(flagKey(table));
        if (it != g_extFlags.end())
            for (uint8_t b : it->second.row[0])
                if (static_cast<int8_t>(b) < 0)
                    ++n;
        ret(ctx, n);
    }

    // func_0011F740 (profile, level): live table from the level's save slot (or cleared when level >= 30), then the
    // whole live table copied to the checkpoint with a loop. Mirrored before the game runs, so a loop that pauses
    // part way and resumes later does not matter.
    void extFlagsLevelStartEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == GA(0x0011F740u))
        {
            const uint32_t profile = reg(ctx, 4);
            const int32_t level = static_cast<int32_t>(reg(ctx, 5));
            const uint32_t live = profile + kProfileLiveOff, checkpoint = profile + kProfileCheckpointOff;
            for (uint32_t r = 0; r < kFlagRows; ++r)
                extClearRow(live, r);
            if (level < kProfileSlots)
                extCopyRow(live, profile + kProfileSlotOff + static_cast<uint32_t>(level) * kProfileSlotStride, 0);
            for (uint32_t r = 0; r < kFlagRows; ++r)
                extCopyRow(checkpoint, live, r);
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // func_0011F920 (profile): checkpoint reached, the live table copied to the checkpoint with a loop.
    void extFlagsCheckpointEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == GA(0x0011F920u))
            for (uint32_t r = 0; r < kFlagRows; ++r)
                extCopyRow(reg(ctx, 4) + kProfileCheckpointOff, reg(ctx, 4) + kProfileLiveOff, r);
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // Checks that the code still is what the notes above describe before anything is replaced.
    bool extFlagsCodeMatches(const uint8_t *rdram)
    {
        struct Word
        {
            uint32_t addr, value;
        };
        static constexpr Word kWords[] = {
            {GA(0x00120584u), GW(0x00120584u, 0x24060180u)}, // func_00120570: addiu a2, zero, 0x180
            {GA(0x001205A4u), GW(0x001205A4u, 0x90430003u)}, // func_00120590: lbu v1, 3(v0)
            {GA(0x001205D4u), GW(0x001205D4u, 0xA0460003u)}, // func_001205C0: sb a2, 3(v0)
            {GA(0x0012060Cu), GW(0x0012060Cu, 0x24060180u)}, // func_001205E0: row 0 is 0x180 bytes
            {GA(0x00120614u), GW(0x00120614u, 0x24060480u)}, // func_001205E0: rows 1..3 are 0x480 bytes
            {GA(0x00120660u), GW(0x00120660u, 0x24060180u)}, // func_00120640: addiu a2, zero, 0x180
            {GA(0x00120694u), GW(0x00120694u, 0x28A30180u)}, // func_00120670: slti v1, a1, 0x180
            {GA(0x001047C0u), GW(0x001047C0u, 0xAE300000u)}, // func_00104780: sw s0, 0(s1) - the handle
            {GA(0x0011F7D0u), GW(0x0011F7D0u, 0x24060604u)}, // func_0011F740: memset of the whole 0x604 table
            {GA(0x0011F938u), GW(0x0011F938u, 0x240600C0u)}, // func_0011F920: 0xC0 doublewords copied
        };
        for (const Word &w : kWords)
        {
            uint32_t v;
            std::memcpy(&v, rdram + w.addr, 4);
            if (v != w.value)
            {
                logf("entity flags: code at 0x%06X is %08X, not %08X; the 384 limit is left as it is", w.addr, v,
                     w.value);
                return false;
            }
        }
        return true;
    }

    void installExtFlags(PS2Runtime &rt, const uint8_t *rdram)
    {
        if (!extFlagsCodeMatches(rdram))
            return;
        rt.replaceFunction(GA(0x00120590u), extFlagsReadEntry);
        rt.replaceFunction(GA(0x001205C0u), extFlagsWriteEntry);
        rt.replaceFunction(GA(0x00120570u), extFlagsClearRowEntry);
        rt.replaceFunction(GA(0x001205E0u), extFlagsLoadSlotEntry);
        rt.replaceFunction(GA(0x00120640u), extFlagsCopyRowEntry);
        rt.replaceFunction(GA(0x00120670u), extFlagsCountEntry);
        rt.replaceFunction(GA(0x0011F740u), extFlagsLevelStartEntry);
        rt.replaceFunction(GA(0x0011F920u), extFlagsCheckpointEntry);
        rt.replaceFunction(GA(0x0019B100u), extFlagsMemcpyEntry);
        rt.replaceFunction(GA(0x0019B2B8u), extFlagsMemsetEntry);
        logf("%s (up to %u per level; the extra ones are not kept on the memory card)", kEntityFlagsMarker,
             kFlagHandleMax);
    }

    // ---- the game's memory pools with extended RAM (MAXIMO_RAM=64) -------------------------------------------------
    // The static constructor sinit_001EA400 tail-jumps into func_001811D0(heap object 0x2393B0), which mallocs one
    // block for all of the game's pools: 0x2000000 - [0x238EDC] (main stack size, 0xB5000) - [0x238ED8] (heap start,
    // 0x2F5E00) - 0xC800, with the 32 MB written into the code (lui a1, 0x200). It then splits it: a 0xCB000 pool,
    // a 0x640000 pool, and everything else for the main pool. Only func_001811D0 reads the stack-size word, so while
    // the constructor runs it holds 0x2000000 - <heap end>: the subtraction then ends the block below the runtime's
    // pools instead of at the retail stack, and the main pool takes the extra ~31 MB. The word is put back after.
    // The tail jump is a direct call in the recompiled code, so the constructor (called through the .ctors table at
    // 0x1EC0F0) is the place to hook.
    constexpr uint32_t kHeapSetupCtor = GA(0x001EA400u);
    constexpr uint32_t kHeapStartWord = GA(0x00238ED8u), kStackSizeWord = GA(0x00238EDCu);
    constexpr uint32_t kRetailHeapStart = GA(0x002F5E00u), kRetailStackSize = 0x000B5000u;
    constexpr uint32_t kHeapObject = GA(0x002393B0u); // +0: the block size func_001811D0 computed

    bool heapSetupCodeMatches()
    {
        struct Word
        {
            uint32_t addr, value;
        };
        static constexpr Word kWords[] = {
            {GA(0x001811ECu), GW(0x001811ECu, 0x8F83876Cu)}, // lw v1, -0x7894(gp): stack size
            {GA(0x001811F0u), GW(0x001811F0u, 0x8F828768u)}, // lw v0, -0x7898(gp): heap start
            {GA(0x001811F4u), GW(0x001811F4u, 0x3C050200u)}, // lui a1, 0x200: the retail 32 MB
            {GA(0x001811FCu), GW(0x001811FCu, 0x00A31823u)}, // subu v1, a1, v1
            {GA(0x00181200u), GW(0x00181200u, 0x00621023u)}, // subu v0, v1, v0
            {GA(0x001EA404u), GW(0x001EA404u, 0x08060474u)}, // sinit_001EA400: j 0x1811D0
            {GA(0x001EA408u), GW(0x001EA408u, 0x244493B0u)}, // addiu a0, v0, -0x6C50 (0x2393B0)
            {GA(0x001EC0F0u), GA(0x001EA400u)}, // .ctors entry
        };
        for (const Word &w : kWords)
            if (rd32(w.addr) != w.value)
            {
                logf("memory: code at 0x%06X is %08X, not %08X; the game's pools keep their retail size", w.addr,
                     rd32(w.addr), w.value);
                return false;
            }
        if (rd32(kHeapStartWord) != kRetailHeapStart || rd32(kStackSizeWord) != kRetailStackSize)
        {
            logf("memory: heap start/stack size words are 0x%X/0x%X, not 0x%X/0x%X; the game's pools keep their retail "
                 "size",
                 rd32(kHeapStartWord), rd32(kStackSizeWord), kRetailHeapStart, kRetailStackSize);
            return false;
        }
        return true;
    }

    // func_001811D0 then splits the block with func_00181480(pool, base, bytes), which returns the next base: the main
    // pool (heap object +0x20) gets the block minus 0x70B030, then the pool at +0x260 a fixed 0x640000 and the pool
    // at +0x140 a fixed 0xCB000 (immediates in the code). A larger block only grew the main pool, and 10x enemies ran
    // the 812 KB pool at +0x140 out (224-byte objects from 0x15E04C) while ~90 MB sat unused. So once the game has
    // built its (still empty) pools, they are built again over the same block with the two fixed pools scaled up.
    constexpr uint32_t kPoolInitFn = GA(0x00181480u);
    constexpr uint32_t kMainPool = 0x20u, kMidPool = 0x260u, kSmallPool = 0x140u; // offsets in the heap object
    constexpr uint32_t kRetailMidPool = 0x640000u, kRetailSmallPool = 0xCB000u;
    // The +0x260 pool is the one the randomizer calls the RESOURCE pool: a world's .PRS models and .PRT textures. Six
    // times retail (37.5 MB) holds a world with every cross-world enemy, the six bosses, Grim and the four sorceresses
    // (about 25 MB) with the randomizer's 90.4% occupancy margin; the main pool keeps more than 60 MB.
    constexpr uint32_t kMidPoolScale = 6, kSmallPoolScale = 16;
    constexpr uint32_t kGuestCallMarker = 0x00000004u; // a return address no guest code uses

    // Runs a guest function to completion with up to three arguments, keeping the caller's registers. Returns false
    // if it did not come back to the marker (it paused or went astray).
    bool callGuest(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t fn, uint32_t a0, uint32_t a1,
                   uint32_t a2, uint32_t &v0)
    {
        __m128i saved[32];
        std::memcpy(saved, ctx->r, sizeof saved);
        const uint32_t savedPc = ctx->pc, savedBranch = ctx->branch_pc;
        const bool savedDelay = ctx->in_delay_slot;
        SET_GPR_U32(ctx, 4, a0);
        SET_GPR_U32(ctx, 5, a1);
        SET_GPR_U32(ctx, 6, a2);
        SET_GPR_U32(ctx, 31, kGuestCallMarker);
        ctx->pc = fn;
        g_original[slotOf(fn)](rdram, ctx, runtime);
        const bool ok = ctx->pc == kGuestCallMarker;
        v0 = reg(ctx, 2);
        std::memcpy(ctx->r, saved, sizeof saved);
        ctx->pc = savedPc;
        ctx->branch_pc = savedBranch;
        ctx->in_delay_slot = savedDelay;
        return ok;
    }

    // The same with up to eight arguments: a0-a3 and t0-t3 (the EE's EABI passes eight in registers).
    bool callGuestArgs(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t fn,
                       const std::array<uint32_t, 8> &args, uint32_t &v0)
    {
        __m128i saved[32];
        std::memcpy(saved, ctx->r, sizeof saved);
        const uint32_t savedPc = ctx->pc, savedBranch = ctx->branch_pc;
        const bool savedDelay = ctx->in_delay_slot;
        for (int i = 0; i < 8; ++i)
            SET_GPR_U32(ctx, 4 + i, args[i]);
        SET_GPR_U32(ctx, 31, kGuestCallMarker);
        ctx->pc = fn;
        g_original[slotOf(fn)](rdram, ctx, runtime);
        // Generated code stops at scheduler checkpoints (a timer or interrupt is due) with $pc wherever it was, and
        // the scheduler's run loop resumes it from there; returns inside it then carry on to their callers' return
        // addresses the same way. A long call (an enemy script's first run) hits these, so it is resumed here
        // likewise until it comes back to the marker. A thread switch can't be followed: that is a failure.
        for (int steps = 0; ctx->pc != kGuestCallMarker && steps < 4000000; ++steps)
        {
            if (ctx->pc == 0u || !runtime->hasFunction(ctx->pc) || runtime->isStopRequested() ||
                runtime->eeScheduler().transferPending())
                break;
            runtime->lookupFunction(ctx->pc)(rdram, ctx, runtime);
        }
        const bool ok = ctx->pc == kGuestCallMarker;
        if (!ok)
            logf("debug: guest call 0x%07X stopped at pc 0x%07X%s", fn, ctx->pc,
                 runtime->eeScheduler().transferPending() ? " (thread switch)" : "");
        v0 = reg(ctx, 2);
        std::memcpy(ctx->r, saved, sizeof saved);
        ctx->pc = savedPc;
        ctx->branch_pc = savedBranch;
        ctx->in_delay_slot = savedDelay;
        return ok;
    }

    uint32_t poolSizeKb(uint32_t pool) { return rd32(kHeapObject + pool + 0x100u) / 64u; }

    void heapSetupEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc != kHeapSetupCtor || !runtime->extendedRam() || !heapSetupCodeMatches())
        {
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            return;
        }
        const uint32_t ra = reg(ctx, 31);
        const uint32_t end = runtime->guestHeapLimit(); // EndOfHeap: 0x7F00000 with 128 MB
        wr32(kStackSizeWord, PS2_RETAIL_RAM_SIZE - end); // wraps: 0x2000000 - this == end
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        wr32(kStackSizeWord, kRetailStackSize);
        if (ctx->pc != ra)
        {
            logf("memory: the pool setup paused part way; the pools keep the game's split");
            return;
        }
        const uint32_t size = rd32(kHeapObject);
        logf("memory: the game's pools take %u KB (retail: %u KB), ending below 0x%07X", size / 1024,
             (PS2_RETAIL_RAM_SIZE - kRetailStackSize - kRetailHeapStart - 0xC800u) / 1024, end);

        // The split, again: same block, same order, bigger fixed pools.
        const uint32_t base = rd32(kHeapObject + kMainPool + 0x108u);
        const uint32_t blockEnd = rd32(kHeapObject + kSmallPool + 0x110u) + 0x10u; // the game's own split ends here
        const uint32_t mid = kRetailMidPool * kMidPoolScale, small = kRetailSmallPool * kSmallPoolScale;
        if (base == 0 || blockEnd <= base || blockEnd - base < mid + small + 0x30u + 0x2000000u)
        {
            logf("memory: pool block 0x%07X-0x%07X is not what was expected; the pools keep the game's split", base,
                 blockEnd);
            return;
        }
        const uint32_t mainBytes = blockEnd - base - mid - small - 0x30u;
        uint32_t next = 0, next2 = 0, next3 = 0;
        if (!callGuest(rdram, ctx, runtime, kPoolInitFn, kHeapObject + kMainPool, base, mainBytes, next) ||
            !callGuest(rdram, ctx, runtime, kPoolInitFn, kHeapObject + kMidPool, next, mid, next2) ||
            !callGuest(rdram, ctx, runtime, kPoolInitFn, kHeapObject + kSmallPool, next2, small, next3))
        {
            logf("memory: rebuilding the pools did not complete; expect trouble");
            return;
        }
        // (The launcher's cross-world option looks for "resource pool six times retail" in maximo_boot.exe.)
        logf("memory: pools rebuilt: main %u KB, +0x260 %u KB (retail %u; resource pool six times retail), "
             "+0x140 %u KB (retail %u); ends 0x%07X (block end 0x%07X)",
             poolSizeKb(kMainPool), poolSizeKb(kMidPool), kRetailMidPool / 1024, poolSizeKb(kSmallPool),
             kRetailSmallPool / 1024, next3, blockEnd);
    }

    bool g_wideCamera = false;  // MAXIMO_ASPECT=16:9: widen the projection
    float g_cameraZoom = 1.0f;  // FREE CAMERA ZOOM, set by the debug menu (1 when the free camera is off)
    // func_15B100(matrix, $f12 focal scale on both axes, $f13 horizontal scale, ...) builds the 3D projection.
    void wideCameraEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_wideCamera)
            ctx->f[13] *= kWideXScale;
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // Also the presenter's sign that this frame shows the widescreen 3D view (ps2_display_aspect.h).
    void wideCullEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        std::memcpy(rdram + kCullFactorAddr, &kWideCullFactor, sizeof(kWideCullFactor));
        ps2_display_aspect::noteWideFrame();
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // func_17E8C0 draws the full-screen flash (lightning; func_17E6C0 starts it, func_17E7B0 fades it): a quad of
    // +-0.5 in the screen view, scaled by 2.0 with func_15AF20 to cover the 4:3 picture. The widened camera leaves it
    // a 4:3 square in the middle, so its horizontal scale is widened back by 4/3.
    constexpr uint32_t kScaleMatrixFn = GA(0x0015AF20u);
    constexpr uint32_t kFlashScaleRa = GA(0x0017E90Cu);

    void wideFlashEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (reg(ctx, 31) == kFlashScaleRa)
            ctx->f[12] /= kWideXScale;
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // func_148470 draws the in-level overlay layer (HUD; the darkened backdrop and menu while paused). Its two
    // callers are the in-level frame loops, so it runs on every gameplay and pause frame but not on the title,
    // loading screens or movies. The pause screen stops drawing the world (no func_16AE00), so without this the
    // paused 16:9 picture would be presented squeezed into 4:3.
    void wideOverlayEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        ps2_display_aspect::noteWideFrame();
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // ---- widescreen HUD -----------------------------------------------------------------------------------------------
    // func_14AEE0 draws the screen-space layer of every screen: HUD models, then the level's stencil shadow volumes, then
    // the text list. Both HUD and text go through the 3D screen transform that wideCameraEntry narrows, so in 16:9 they
    // already keep their 4:3 proportions, grouped in the middle of the picture. The loading screen (func_1482A0) draws
    // its shield and caption the same way. Both mark the frame as widescreen, so a loading screen without a 3D world
    // is shown 16:9 too; logos, title screens and movies draw neither and stay 4:3. What is left is moving the gameplay
    // HUD out to the screen edges.
    //
    // The HUD models are a tree under renderer +0x6878 (func_16A6B0's root), the loading screen's under +0x7910, both
    // drawn by func_1670E0; node +0x30 is the child count, +0x38 the child array, +0x60 the node's absolute matrix,
    // which func_1670E0 selects with func_15FF00 right before drawing the node. HUD units are 4:3 pixels at depth 362
    // (x = -320..320 is the 4:3 width). Each top-level group of a root (the gauge, the counters, the item ring...) that
    // is laid out from a screen edge (its own origin at the edge, like the item ring at x = -320) or lies wholly left or
    // right of the centre gets copies of its node matrices moved toward that edge by a quarter of the 16:9 buffer's
    // half width: its distance from the screen edge is then the same as in 4:3, and its size and internal layout are
    // unchanged. Other groups and all text (menus, dialogue, level titles) stay centred. The renderer reads selected
    // matrices by address when it sends the frame, so the copies live in their own memory, double-buffered per frame.
    constexpr uint32_t kHudRootOffset = 0x6878u;
    constexpr uint32_t kLoadingRootOffset = 0x7910u;
    constexpr uint32_t kHudViewOffset = 0x6C90u;
    constexpr uint32_t kSetViewFn = GA(0x0015FF30u);
    constexpr uint32_t kSetModelFn = GA(0x0015FF00u);
    constexpr uint32_t kLayerStartRa = GA(0x0014AF34u);   // func_14AEE0's func_15FF30 call (HUD view; renderer in $s3)
    constexpr uint32_t kLoadingStartRa = GA(0x00148360u); // func_1482A0's func_15FF30 call (HUD view; renderer in $s1)
    constexpr uint32_t kNodeMatrixRa = GA(0x0016722Cu);   // func_1670E0's func_15FF00(node + 0x60) call
    constexpr uint32_t kAnchorPool = 0x00074000u;     // 2 x 256 matrices in the EE kernel area, unused by game and runtime
    constexpr uint32_t kAnchorPoolSlots = 256u;
    constexpr float kHudHalfWidth = 320.0f;
    constexpr float kHudDepth = 362.0f;
    constexpr float kHudCentreMargin = 32.0f; // a group reaching further than this past the centre stays centred
    constexpr float kHudEdgeOrigin = 300.0f;  // a group whose own origin is this far out is laid out from that edge
    bool g_hudWide = false;                   // the picture is 16:9: move the HUD's side groups to the edges
    bool g_hudEdges = true;                   // MAXIMO_WIDE_HUD=0 leaves the HUD in the 4:3 area
    std::unordered_map<uint32_t, int> g_hudAnchors; // node -> -1 left edge, +1 right edge
    uint32_t g_hudView = 0;
    uint32_t g_anchorPoolBase = kAnchorPool;
    uint32_t g_anchorSlot = 0;

    float guestFloat(uint8_t *rdram, uint32_t address)
    {
        float f;
        std::memcpy(&f, rdram + (address & PS2_RAM_MASK), sizeof(f));
        return f;
    }

    uint32_t guestWord(uint8_t *rdram, uint32_t address)
    {
        uint32_t w;
        std::memcpy(&w, rdram + (address & PS2_RAM_MASK), sizeof(w));
        return w;
    }

    template <typename Visit> void forEachHudNode(uint8_t *rdram, uint32_t node, int depth, const Visit &visit)
    {
        if (!node || depth > 16)
            return;
        visit(node);
        const uint32_t count = guestWord(rdram, node + 0x30);
        const uint32_t children = guestWord(rdram, node + 0x38);
        for (uint32_t i = 0; i < count && i < 256; ++i)
            forEachHudNode(rdram, guestWord(rdram, children + i * 4), depth + 1, visit);
    }

    void classifyHudGroups(uint8_t *rdram, uint32_t renderer, uint32_t rootOffset)
    {
        g_hudAnchors.clear();
        g_hudView = renderer + kHudViewOffset;
        g_anchorPoolBase = g_anchorPoolBase == kAnchorPool ? kAnchorPool + kAnchorPoolSlots * 64 : kAnchorPool;
        g_anchorSlot = 0;
        const uint32_t root = guestWord(rdram, renderer + rootOffset);
        if (!root)
            return;
        const uint32_t count = guestWord(rdram, root + 0x30);
        const uint32_t children = guestWord(rdram, root + 0x38);
        for (uint32_t i = 0; i < count && i < 256; ++i)
        {
            const uint32_t group = guestWord(rdram, children + i * 4);
            float lo = 1e9f, hi = -1e9f;
            forEachHudNode(rdram, group, 0, [&](uint32_t node) {
                if (guestWord(rdram, node + 0x30) != 0)
                    return;
                const float x = guestFloat(rdram, node + 0x60 + 48);
                if (std::isfinite(x))
                {
                    lo = std::min(lo, x);
                    hi = std::max(hi, x);
                }
            });
            const float origin = guestFloat(rdram, group + 0x60 + 48);
            int side = 0;
            if (std::isfinite(origin) && std::fabs(origin) >= kHudEdgeOrigin)
                side = origin < 0.0f ? -1 : 1;
            else if (lo <= hi && hi <= kHudCentreMargin && lo < -kHudCentreMargin)
                side = -1;
            else if (lo <= hi && lo >= -kHudCentreMargin && hi > kHudCentreMargin)
                side = 1;
            if (side)
                forEachHudNode(rdram, group, 0, [&](uint32_t node) { g_hudAnchors[node] = side; });
        }
    }

    // $a0 of func_1670E0's func_15FF00 call: a side group's node matrix is replaced by a moved copy.
    void anchorNodeMatrix(uint8_t *rdram, R5900Context *ctx)
    {
        const uint32_t matrix = reg(ctx, 4);
        const auto it = g_hudAnchors.find(matrix - 0x60);
        if (it == g_hudAnchors.end() || g_anchorSlot >= kAnchorPoolSlots)
            return;
        float m[16];
        std::memcpy(m, rdram + (matrix & PS2_RAM_MASK), sizeof(m));
        // The node's depth in the HUD view (column 2 of a row-vector matrix): under perspective the move scales with it.
        float depth = guestFloat(rdram, g_hudView + 48 + 8);
        for (int r = 0; r < 3; ++r)
            depth += m[12 + r] * guestFloat(rdram, g_hudView + r * 16 + 8);
        if (!std::isfinite(depth) || depth <= 0.0f)
            return;
        // A third of the half width in HUD units is a quarter of it once the screen transform narrows x by 3/4.
        m[12] += static_cast<float>(it->second) * (kHudHalfWidth / 3.0f) * (depth / kHudDepth);
        const uint32_t copy = g_anchorPoolBase + g_anchorSlot++ * 64;
        std::memcpy(rdram + copy, m, sizeof(m));
        SET_GPR_U32(ctx, 4, copy);
    }

    // func_15FF30 (view) and func_15FF00 (model matrix), acting only on the calls above.
    void wideHudEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t ra = reg(ctx, 31);
        if (ctx->pc == kSetViewFn && (ra == kLayerStartRa || ra == kLoadingStartRa))
        {
            ps2_display_aspect::noteWideFrame();
            if (g_hudWide && g_hudEdges)
            {
                if (ra == kLayerStartRa)
                    classifyHudGroups(rdram, reg(ctx, 19), kHudRootOffset);
                else
                    classifyHudGroups(rdram, reg(ctx, 17), kLoadingRootOffset);
            }
        }
        else if (ctx->pc == kSetModelFn && ra == kNodeMatrixRa && g_hudWide && g_hudEdges)
            anchorNodeMatrix(rdram, ctx);
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // ---- movie bit reader (MAXIMO_MOVIE_BITS, MOVIE_SPEED.md) ----------------------------------------------------
    // The FMV player reads its stream through a byte-fed bit reader of tiny functions that it calls for nearly every
    // field: func_1BD870 (peek n bits), func_1BD890 (skip n bits, refill byte by byte), func_1BD928 (read n bits) and
    // func_1BD978 (read 1 bit). Their per-call overhead in the port dwarfs their work, so they run natively here, with
    // the same results and the same EE cycles charged (8 per call they made, 32 per refill loop back edge).
    // Reader: +0x00 u64 bit buffer (msb first), +0x0C u32 read pointer, +0x10 u32 bits in the buffer, +0x18 u64 bits
    // consumed, +0x20 ring start, +0x24 ring end. MAXIMO_MOVIE_BITS=0 keeps the game's code; =verify runs both and
    // logs any difference.
    constexpr uint32_t kBitsPeekFn = GA(0x001BD870u);
    constexpr uint32_t kBitsSkipFn = GA(0x001BD890u);
    constexpr uint32_t kBitsReadFn = GA(0x001BD928u);
    constexpr uint32_t kBitsReadBitFn = GA(0x001BD978u);
    bool g_movieBitsVerify = false;
    uint64_t g_movieBitsCalls = 0, g_movieBitsChecked = 0, g_movieBitsMismatches = 0;

    struct BitReader
    {
        uint64_t buffer, consumed;
        uint32_t ptr, count, start, end;
    };

    BitReader loadBitReader(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t r)
    {
        return {READ64(r), READ64(r + 0x18u), READ32(r + 0x0Cu), READ32(r + 0x10u), READ32(r + 0x20u),
                READ32(r + 0x24u)};
    }

    void storeBitReader(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t r, const BitReader &b)
    {
        WRITE64(r, b.buffer);
        WRITE32(r + 0x0Cu, b.ptr);
        WRITE32(r + 0x10u, b.count);
        WRITE64(r + 0x18u, b.consumed);
    }

    // func_1BD870: the top n bits, as a sign-extended 32-bit value.
    uint64_t bitsPeek(const BitReader &b, uint64_t n)
    {
        const uint64_t v = b.buffer >> ((64u - static_cast<uint32_t>(n)) & 63u);
        return static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(v)));
    }

    // func_1BD890: drop n bits and refill to more than 56; returns the new consumed count. Counts loop back edges.
    template <typename ReadByte> uint64_t bitsSkip(BitReader &b, uint64_t n, ReadByte readByte, uint32_t &backEdges)
    {
        b.buffer <<= (n & 63u);
        b.count -= static_cast<uint32_t>(n);
        if (b.count < 57u)
        {
            for (;;)
            {
                b.buffer |= static_cast<uint64_t>(readByte(b.ptr)) << ((0x38u - b.count) & 63u);
                ++b.ptr;
                if (!(b.ptr < b.end))
                    b.ptr = b.start;
                b.count += 8u;
                if (b.count >= 57u)
                    break;
                ++backEdges;
            }
        }
        b.consumed += n;
        return b.consumed;
    }

    bool sameBitReader(const BitReader &a, const BitReader &b)
    {
        return a.buffer == b.buffer && a.consumed == b.consumed && a.ptr == b.ptr && a.count == b.count;
    }

    void movieBitsEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t fn = ctx->pc;
        const uint32_t r = reg(ctx, 4);
        const uint64_t n = fn == kBitsReadBitFn ? 1u : GPR_U64(ctx, 5);
        BitReader b = loadBitReader(rdram, ctx, runtime, r);
        uint32_t backEdges = 0;
        const auto readByte = [&](uint32_t address) { return READ8(address); };
        uint64_t result;
        if (fn == kBitsPeekFn)
            result = bitsPeek(b, n);
        else if (fn == kBitsSkipFn)
            result = bitsSkip(b, n, readByte, backEdges);
        else
        {
            result = bitsPeek(b, n);
            bitsSkip(b, n, readByte, backEdges);
        }
        ++g_movieBitsCalls;
        if (g_movieBitsVerify)
        {
            const uint32_t ra = reg(ctx, 31);
            g_original[slotOf(fn)](rdram, ctx, runtime);
            if (ctx->pc != ra)
                return; // paused at a loop checkpoint; it resumes later from there and is not compared
            ++g_movieBitsChecked;
            const BitReader game = loadBitReader(rdram, ctx, runtime, r);
            if ((!sameBitReader(game, b) || GPR_U64(ctx, 2) != result) && ++g_movieBitsMismatches <= 16)
                logf("movie bits: 0x%06X(n=%llu) differs: v0 %016llX vs %016llX, buffer %016llX vs %016llX, ptr %08X vs "
                     "%08X, count %u vs %u, consumed %llu vs %llu",
                     fn, static_cast<unsigned long long>(n), static_cast<unsigned long long>(GPR_U64(ctx, 2)),
                     static_cast<unsigned long long>(result), static_cast<unsigned long long>(game.buffer),
                     static_cast<unsigned long long>(b.buffer), game.ptr, b.ptr, game.count, b.count,
                     static_cast<unsigned long long>(game.consumed), static_cast<unsigned long long>(b.consumed));
            return;
        }
        if (fn != kBitsPeekFn)
            storeBitReader(rdram, ctx, runtime, r, b);
        // The game's func_1BD928 and func_1BD978 call the other two (two dispatches of 8 cycles each).
        const uint32_t cycles = backEdges * 32u + (fn == kBitsReadFn || fn == kBitsReadBitFn ? 16u : 0u);
        if (cycles)
            runtime->eeScheduler().accountCycles(cycles);
        SET_GPR_U64(ctx, 2, result);
        ctx->pc = reg(ctx, 31);
    }

    // ---- movie packet headers (MAXIMO_MOVIE_BITS, MOVIE_SPEED.md) ------------------------------------------------
    // The player polls the demuxer func_1BDCD0 about 80,000 times a second while its video buffer is full. Each call
    // parses a pack header (func_1BE0D8, with func_1BE228 for a system header) and a PES packet header (func_1BE298)
    // through the bit reader, about 40 reader calls, before the stream callback turns the packet down. Those two
    // parsers run natively here with the same results and the same EE cycles charged: 8 per call they made, 16 more
    // for each func_1BD928/func_1BD978 (the two calls those make) and 32 per loop back edge. Where func_1BE298 prints
    // an error (a pack header inside a PES header) the game's code runs instead.
    constexpr uint32_t kPackHeaderFn = GA(0x001BE0D8u);
    constexpr uint32_t kPesHeaderFn = GA(0x001BE298u);
    constexpr uint32_t kPesFlagBitsTable = GA(0x001E70B0u); // bits taken by the ES rate, trick mode, copy info and CRC fields
    uint64_t g_movieHeaderCalls = 0, g_movieHeaderChecked = 0, g_movieHeaderMismatches = 0;

    // A copy of the game's bit reader (+0x08 start of the stream, +0x28 ring size) with the cost of the calls made.
    struct HeaderBits
    {
        uint8_t *rdram;
        R5900Context *ctx;
        PS2Runtime *runtime;
        BitReader b;
        uint32_t streamStart, ringSize;
        uint32_t cycles = 0;

        uint64_t peek(uint64_t n)
        {
            cycles += 8u;
            return bitsPeek(b, n);
        }
        uint64_t read(uint64_t n) // func_1BD928, and func_1BD978 for n = 1
        {
            const uint64_t v = bitsPeek(b, n);
            uint32_t backEdges = 0;
            bitsSkip(b, n, [&](uint32_t address) { return READ8(address); }, backEdges);
            cycles += 24u + backEdges * 32u;
            return v;
        }
        void backEdge() { cycles += 32u; }
        uint8_t byte(uint32_t address) { return READ8(address); }
        // func_1BD9C0: move n bytes on (n may be negative), then refill the empty buffer through func_1BD890.
        void skipBytes(int32_t n)
        {
            cycles += 8u;
            b.consumed += static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(static_cast<uint32_t>(n) << 3)));
            b.ptr = streamStart + static_cast<uint32_t>(b.consumed >> 3);
            if (!(b.ptr < b.end))
                b.ptr -= ringSize;
            b.buffer = 0;
            b.count = 0;
            uint32_t backEdges = 0;
            bitsSkip(b, 0, [&](uint32_t address) { return READ8(address); }, backEdges);
            cycles += backEdges * 32u;
        }
    };

    // The fields a header parser stores, applied (or compared) after it has run.
    struct HeaderOut
    {
        struct Field
        {
            uint32_t offset, size;
            uint64_t value;
        };
        Field fields[8];
        uint32_t n = 0;

        void set(uint32_t offset, uint32_t size, uint64_t value)
        {
            for (uint32_t i = 0; i < n; ++i)
                if (fields[i].offset == offset)
                {
                    fields[i].value = value;
                    return;
                }
            fields[n++] = {offset, size, value};
        }
    };

    // func_1BE298 timestamp: 4 bits, then 3, 15 and 15 bits of the 33-bit value, each followed by a marker bit.
    uint64_t readTimestamp(HeaderBits &h)
    {
        h.read(4);
        const uint32_t hi = static_cast<uint32_t>(h.read(3));
        h.read(1);
        const uint32_t mid = static_cast<uint32_t>(h.read(15));
        h.read(1);
        const uint32_t lo = static_cast<uint32_t>(h.read(15));
        h.read(1);
        return (static_cast<uint64_t>((hi >> 2) & 1u) << 32) | ((hi << 30) | (mid << 15) | lo);
    }

    // func_1BE298(reader, out): a PES packet header, skipping the payload. Out: +0x00 stream id << 32 (| sub-stream
    // word for 0xBD and 0xBF), +0x08 length, +0x0C scrambling, +0x10 PTS, +0x18 DTS (-1 if none), +0x20 bit position of
    // the payload, +0x24 payload length - 3, +0x28 bit position of the packet. False where the game prints an error.
    bool parsePesHeader(HeaderBits &h, HeaderOut &o)
    {
        o.set(0x28, 4, static_cast<uint32_t>(h.b.consumed));
        h.read(24);
        const uint64_t id = h.read(8) << 32;
        o.set(0x00, 8, id);
        const uint32_t length = static_cast<uint32_t>(h.read(16));
        o.set(0x08, 4, length);
        o.set(0x10, 8, ~0ull);
        o.set(0x18, 8, ~0ull);
        switch (id >> 32)
        {
        case 0xBC: case 0xBE: case 0xBF: case 0xF0: case 0xF1: case 0xF2: case 0xF8: case 0xFF:
        {
            uint32_t rest = length;
            if (id >> 32 == 0xBF)
            {
                o.set(0x00, 8, id | static_cast<uint32_t>(h.read(32)));
                rest -= 4u;
            }
            if (rest)
                h.skipBytes(static_cast<int32_t>(rest));
            return true;
        }
        default:
            break;
        }
        h.read(2);
        o.set(0x0C, 4, static_cast<uint32_t>(h.read(2)));
        h.read(4);
        const uint64_t ptsDts = h.read(2);
        const uint64_t escr = h.read(1);
        const uint64_t flags = h.read(4);
        const uint64_t extension = h.read(1);
        const uint32_t headerLength = static_cast<uint32_t>(h.read(8));
        const uint64_t dataStart = static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(h.b.consumed)));
        if (ptsDts & 2u)
            o.set(0x10, 8, readTimestamp(h));
        if (ptsDts == 3u)
            o.set(0x18, 8, readTimestamp(h));
        if (escr == 1u)
            h.read(48);
        if (flags)
            h.read(h.byte(kPesFlagBitsTable + static_cast<uint32_t>(flags)));
        if (extension == 1u)
        {
            const uint64_t privateData = h.read(1), packHeader = h.read(1), sequenceCounter = h.read(1),
                           pstdBuffer = h.read(1);
            h.read(3);
            const uint64_t extension2 = h.read(1);
            if (privateData == 1u)
            {
                h.read(48);
                h.read(48);
                h.read(32);
            }
            if (packHeader == 1u)
                return false;
            if (sequenceCounter == 1u)
                h.read(16);
            if (pstdBuffer == 1u)
                h.read(16);
            if (extension2 == 1u)
            {
                h.read(1);
                const uint32_t count = static_cast<uint32_t>(h.read(7));
                for (uint32_t i = 0; i < count; ++i)
                {
                    h.read(8);
                    if (i + 1u < count)
                        h.backEdge();
                }
            }
        }
        const uint32_t headerRead = static_cast<uint32_t>((h.b.consumed - dataStart) >> 3);
        if (const uint32_t rest = headerLength - headerRead)
            h.skipBytes(static_cast<int32_t>(rest));
        const uint32_t payload = length - headerLength;
        o.set(0x24, 4, payload - 3u);
        o.set(0x20, 4, static_cast<uint32_t>(h.b.consumed));
        uint32_t rest = payload - 3u;
        if (id >> 32 == 0xBD)
        {
            o.set(0x00, 8, id | static_cast<uint32_t>(h.read(32)));
            rest = payload - 7u;
        }
        if (rest)
            h.skipBytes(static_cast<int32_t>(rest));
        return true;
    }

    // func_1BE0D8(reader, out): a pack header, and the system header after it if there is one. Out: +0x00 SCR
    // extension, +0x04 SCR bits 0-31, +0x08 SCR bit 32, +0x0C 1 if a system header followed.
    bool parsePackHeader(HeaderBits &h, HeaderOut &o)
    {
        h.read(34);
        const uint32_t hi = static_cast<uint32_t>(h.read(3));
        h.read(1);
        const uint32_t mid = static_cast<uint32_t>(h.read(15));
        h.read(1);
        const uint32_t lo = static_cast<uint32_t>(h.read(15));
        h.read(1);
        o.set(0x00, 4, static_cast<uint32_t>(h.read(9)));
        h.read(30);
        const uint32_t stuffing = static_cast<uint32_t>(h.read(3));
        o.set(0x08, 4, (hi >> 2) & 1u);
        o.set(0x04, 4, (hi << 30) | (mid << 15) | lo);
        for (uint32_t i = 0; i < stuffing; ++i)
        {
            h.read(8);
            if (i + 1u < stuffing)
                h.backEdge();
        }
        if (h.peek(32) != 0x1BBu)
        {
            o.set(0x0C, 4, 0);
            return true;
        }
        o.set(0x0C, 4, 1);
        h.cycles += 8u; // func_1BE228: the system header, skipped
        h.read(56);
        h.read(40);
        while (h.peek(1) == 1u)
        {
            h.backEdge();
            h.read(24);
        }
        return true;
    }

#ifdef MAXIMO_HAS_FFMPEG
    // ---- movie pictures through FFmpeg (MAXIMO_MOVIE_FFMPEG, MOVIE_SPEED.md) ---------------------------------------
    // sceMpegGetPicture (func_1BEB40) decodes the next picture with FFmpeg instead of the game's libmpeg, which drives
    // the IPU macroblock by macroblock. The stream still arrives the game's way, DMA channel 4 from the video buffer
    // into the IPU input FIFO, and is taken from there in whole bytes (Ps2Ipu::peekInput), so the buffer accounting
    // and the timestamp lookup (func_190A20, which reads D4_MADR and IPU_BP) see the input consumed as libmpeg would.
    // What the game reads afterwards is what libmpeg leaves:
    //  - the picture as RGBA32 macroblocks in column order (x outer), through the IPU's colour conversion with alpha
    //    0x80 (the movies set no thresholds);
    //  - sceMpeg +0 width, +4 height, +8 pictures returned before this one, +0x10/+0x18 PTS/DTS from this picture's
    //    timestamp callback, +0x20 flags 0x1D8 | picture type (unused by the player); sys +0 end of stream (set with
    //    the last picture), +4 picture headers read, +8 = 2;
    //  - the timestamp callback (type 5) once per picture header, with the input consumed up to its start code, and
    //    the no-data callback (type 1) whenever the input runs dry. Callbacks run as guest code: ra points back at
    //    func_1BEB40, where the decoder resumes from its saved state once the callback returns.
    // sceMpegCreate (func_1BE8C0) and sceMpegReset (func_1BEC78) also reset the decoder.
    constexpr uint32_t kGetPictureFn = GA(0x001BEB40u);
    constexpr uint32_t kMpegCreateFn = GA(0x001BE8C0u);
    constexpr uint32_t kMpegResetFn = GA(0x001BEC78u);
    constexpr uint32_t kMpegCbNodata = 1u, kMpegCbTimeStamp = 5u;
    constexpr uint32_t kMpegCbFrame = 0x40u; // the callback's data block, below the caller's stack pointer

    struct MovieDecoder
    {
        AVCodecContext *codec = nullptr;
        AVPacket *packet = nullptr;
        std::deque<AVFrame *> frames;       // decoded, in display order
        std::vector<uint8_t> unit;          // the access unit being gathered, from its first start code
        uint32_t scan = 0xFFFFFFFFu;        // the last four stream bytes, for start codes
        bool inSlices = false;              // slice data seen since the unit's picture header
        bool ended = false;                 // sequence end code read and the decoder drained
        int64_t unitPicture = -1;           // decode index of the unit's picture
        struct Stamp
        {
            int64_t pts = -1, dts = -1;
        };
        std::vector<Stamp> stamps;          // by decode index
        uint32_t returned = 0;
        // The call in progress while a callback runs.
        uint32_t pending = 0;               // callback type, 0 = none
        uint32_t mp = 0, buf = 0, mbCount = 0, ra = 0, sp = 0;
        uint64_t pictures = 0, units = 0, errors = 0; // totals for the exit summary
    };
    MovieDecoder g_movie;
    bool g_movieFfmpeg = false;

    void movieDecoderReset(MovieDecoder &d)
    {
        for (AVFrame *f : d.frames)
            av_frame_free(&f);
        d.frames.clear();
        if (d.codec)
            avcodec_flush_buffers(d.codec);
        d.unit.clear();
        d.scan = 0xFFFFFFFFu;
        d.inSlices = d.ended = false;
        d.unitPicture = -1;
        d.stamps.clear();
        d.returned = 0;
        d.pending = 0;
    }

    bool movieDecoderOpen(MovieDecoder &d)
    {
        av_log_set_level(AV_LOG_ERROR);
        const AVCodec *codec = avcodec_find_decoder(AV_CODEC_ID_MPEG2VIDEO);
        d.codec = codec ? avcodec_alloc_context3(codec) : nullptr;
        d.packet = av_packet_alloc();
        if (!d.codec || !d.packet)
            return false;
        d.codec->thread_count = 1;
        return avcodec_open2(d.codec, codec, nullptr) == 0;
    }

    void movieReceiveFrames(MovieDecoder &d)
    {
        for (;;)
        {
            AVFrame *f = av_frame_alloc();
            if (avcodec_receive_frame(d.codec, f) != 0)
            {
                av_frame_free(&f);
                return;
            }
            d.frames.push_back(f);
        }
    }

    // Sends the first n bytes of the unit (one picture with the headers before it) to the decoder.
    void movieSendUnit(MovieDecoder &d, size_t n)
    {
        if (d.unitPicture >= 0 && n != 0u)
        {
            d.packet->data = d.unit.data();
            d.packet->size = static_cast<int>(n);
            d.packet->pts = d.unitPicture;
            if (avcodec_send_packet(d.codec, d.packet) != 0 && ++d.errors <= 8)
                logf("movie decoder: picture %lld rejected", static_cast<long long>(d.unitPicture));
            d.packet->data = nullptr;
            d.packet->size = 0;
            ++d.units;
            movieReceiveFrames(d);
        }
        d.unit.erase(d.unit.begin(), d.unit.begin() + static_cast<std::ptrdiff_t>(n));
        d.unitPicture = -1;
    }

    // Takes input until a picture header has been read (true: its timestamp callback is due), the sequence ends or
    // the input runs dry.
    bool movieReadInput(MovieDecoder &d, Ps2Ipu &ipu)
    {
        uint8_t window[160];
        while (!d.ended)
        {
            const uint32_t n = ipu.peekInput(window, sizeof window);
            if (n == 0u)
                return false;
            uint32_t used = 0;
            bool header = false;
            while (used < n && !header && !d.ended)
            {
                const uint8_t b = window[used++];
                d.unit.push_back(b);
                d.scan = (d.scan << 8) | b;
                if ((d.scan & 0xFFFFFF00u) != 0x00000100u)
                    continue;
                if (b >= 0x01u && b <= 0xAFu)
                {
                    d.inSlices = true;
                    continue;
                }
                if (d.inSlices && (b == 0x00u || b == 0xB3u || b == 0xB8u || b == 0xB7u))
                {
                    movieSendUnit(d, d.unit.size() - 4u); // the previous picture ends at this start code
                    d.inSlices = false;
                }
                if (b == 0x00u)
                {
                    d.unitPicture = static_cast<int64_t>(d.stamps.size());
                    d.stamps.emplace_back();
                    header = true;
                }
                else if (b == 0xB7u)
                {
                    d.unit.clear();
                    avcodec_send_packet(d.codec, nullptr);
                    movieReceiveFrames(d);
                    d.ended = true;
                }
            }
            ipu.skipInput(used);
            if (header)
                return true;
        }
        return false;
    }

    // One picture into the game's buffer: RGBA32 macroblocks in column order, through the IPU's colour conversion
    // (ps2_ipu::macroblockToRgb32: BT.601 with 7-bit coefficients, alpha 0x80), eight pixels at a time.
    void movieWritePicture(const AVFrame *f, uint32_t buf, uint32_t mbCount)
    {
        const int mbw = (f->width + 15) / 16, mbh = (f->height + 15) / 16;
        const uint32_t count = std::min<uint32_t>(mbCount, static_cast<uint32_t>(mbw * mbh));
        const uint32_t base = buf & PS2_RAM_MASK;
        if (base + count * 1024u > PS2_RAM_SIZE)
            return;
        const __m256i k16 = _mm256_set1_epi32(16), k128 = _mm256_set1_epi32(128), k64 = _mm256_set1_epi32(64);
        const __m256i zero = _mm256_setzero_si256(), max = _mm256_set1_epi32(255), alpha = _mm256_set1_epi32(INT32_MIN);
        const auto channel = [&](__m256i sum)
        { return _mm256_min_epi32(_mm256_max_epi32(_mm256_srai_epi32(_mm256_add_epi32(sum, k64), 7), zero), max); };
        for (uint32_t i = 0; i < count; ++i)
        {
            const int mx = static_cast<int>(i) / mbh, my = static_cast<int>(i) % mbh;
            uint32_t *out = reinterpret_cast<uint32_t *>(g_rdram + base + i * 1024u);
            for (int py = 0; py < 16; ++py)
            {
                const int y = std::min(my * 16 + py, f->height - 1);
                const uint8_t *lumRow = f->data[0] + y * f->linesize[0] + mx * 16;
                const uint8_t *cbRow = f->data[1] + (y >> 1) * f->linesize[1] + mx * 8;
                const uint8_t *crRow = f->data[2] + (y >> 1) * f->linesize[2] + mx * 8;
                for (int px = 0; px < 16; px += 8)
                {
                    const __m256i lum = _mm256_mullo_epi32(
                        _mm256_sub_epi32(_mm256_cvtepu8_epi32(_mm_loadl_epi64(reinterpret_cast<const __m128i *>(lumRow + px))), k16),
                        _mm256_set1_epi32(149));
                    int32_t cbBytes, crBytes;
                    std::memcpy(&cbBytes, cbRow + px / 2, 4);
                    std::memcpy(&crBytes, crRow + px / 2, 4);
                    const __m128i cb8 = _mm_cvtsi32_si128(cbBytes), cr8 = _mm_cvtsi32_si128(crBytes);
                    const __m256i cb = _mm256_sub_epi32(_mm256_cvtepu8_epi32(_mm_unpacklo_epi8(cb8, cb8)), k128);
                    const __m256i cr = _mm256_sub_epi32(_mm256_cvtepu8_epi32(_mm_unpacklo_epi8(cr8, cr8)), k128);
                    const __m256i r = channel(_mm256_add_epi32(lum, _mm256_mullo_epi32(cr, _mm256_set1_epi32(204))));
                    const __m256i g = channel(_mm256_sub_epi32(_mm256_sub_epi32(lum, _mm256_mullo_epi32(cb, _mm256_set1_epi32(50))),
                                                               _mm256_mullo_epi32(cr, _mm256_set1_epi32(104))));
                    const __m256i b = channel(_mm256_add_epi32(lum, _mm256_mullo_epi32(cb, _mm256_set1_epi32(258))));
                    const __m256i pixel = _mm256_or_si256(_mm256_or_si256(r, _mm256_slli_epi32(g, 8)),
                                                          _mm256_or_si256(_mm256_slli_epi32(b, 16), alpha));
                    _mm256_storeu_si256(reinterpret_cast<__m256i *>(out + py * 16 + px), pixel);
                }
            }
        }
    }

    // Runs mpeg callback `type` as guest code; func_1BEB40 resumes when it returns.
    void movieCallCallback(MovieDecoder &d, R5900Context *ctx, uint8_t *rdram, PS2Runtime *runtime, uint32_t type)
    {
        const uint32_t sys = READ32(d.mp + 0x40u);
        const uint32_t frame = d.sp - kMpegCbFrame;
        WRITE32(frame, type);
        WRITE64(frame + 0x08u, ~0ull);
        WRITE64(frame + 0x10u, ~0ull);
        SET_GPR_U32(ctx, 29, frame);
        SET_GPR_U32(ctx, 4, d.mp);
        SET_GPR_U32(ctx, 5, frame);
        SET_GPR_U32(ctx, 6, READ32(sys + 0x10u + type * 8u));
        SET_GPR_U32(ctx, 31, kGetPictureFn);
        ctx->pc = READ32(sys + 0x0Cu + type * 8u);
        d.pending = type;
    }

    void movieGetPictureEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        MovieDecoder &d = g_movie;
        if (d.pending != 0u && reg(ctx, 29) == d.sp - kMpegCbFrame)
        {
            if (d.pending == kMpegCbTimeStamp && !d.stamps.empty())
                d.stamps.back() = {static_cast<int64_t>(READ64(d.sp - kMpegCbFrame + 0x08u)),
                                   static_cast<int64_t>(READ64(d.sp - kMpegCbFrame + 0x10u))};
            SET_GPR_U32(ctx, 29, d.sp);
            d.pending = 0;
        }
        else
        {
            d.pending = 0;
            d.mp = reg(ctx, 4);
            d.buf = reg(ctx, 5);
            d.mbCount = reg(ctx, 6);
            d.ra = reg(ctx, 31);
            d.sp = reg(ctx, 29);
        }
        const uint32_t sys = READ32(d.mp + 0x40u);
        Ps2Ipu &ipu = runtime->memory().ipu();
        while (d.frames.empty() && !d.ended)
        {
            const uint32_t callback = movieReadInput(d, ipu) ? kMpegCbTimeStamp : d.ended ? 0u : kMpegCbNodata;
            WRITE32(sys + 4u, static_cast<uint32_t>(d.stamps.size()));
            if (callback != 0u)
            {
                movieCallCallback(d, ctx, rdram, runtime, callback);
                return;
            }
        }
        if (d.frames.empty())
        {
            // The sequence ended with no picture left to return.
            WRITE32(sys, 1u);
            SET_GPR_U64(ctx, 2, ~0ull);
            ctx->pc = d.ra;
            return;
        }
        AVFrame *f = d.frames.front();
        d.frames.pop_front();
        movieWritePicture(f, d.buf, d.mbCount);
        const MovieDecoder::Stamp stamp = f->pts >= 0 && static_cast<size_t>(f->pts) < d.stamps.size()
                                       ? d.stamps[static_cast<size_t>(f->pts)]
                                       : MovieDecoder::Stamp{};
        WRITE32(d.mp + 0x00u, static_cast<uint32_t>(f->width));
        WRITE32(d.mp + 0x04u, static_cast<uint32_t>(f->height));
        WRITE32(d.mp + 0x08u, d.returned);
        ++d.returned;
        WRITE64(d.mp + 0x10u, static_cast<uint64_t>(stamp.pts));
        WRITE64(d.mp + 0x18u, static_cast<uint64_t>(stamp.dts));
        WRITE64(d.mp + 0x20u, 0x1D8u | static_cast<uint32_t>(f->pict_type));
        WRITE32(sys + 0u, d.ended && d.frames.empty() ? 1u : 0u);
        WRITE32(sys + 4u, static_cast<uint32_t>(d.stamps.size()));
        WRITE32(sys + 8u, 2u);
        av_frame_free(&f);
        ++d.pictures;
        SET_GPR_U64(ctx, 2, 1u);
        ctx->pc = d.ra;
    }

    // sceMpegCreate, sceMpegReset: a new stream for the decoder too.
    void movieMpegResetEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        movieDecoderReset(g_movie);
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }
#endif

    void movieHeaderEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t fn = ctx->pc;
        const uint32_t r = reg(ctx, 4), out = reg(ctx, 5), ra = reg(ctx, 31);
        HeaderBits h{rdram, ctx, runtime, loadBitReader(rdram, ctx, runtime, r), READ32(r + 0x08u), READ32(r + 0x28u)};
        HeaderOut o;
        const bool native = fn == kPesHeaderFn ? parsePesHeader(h, o) : parsePackHeader(h, o);
        if (!native || g_movieBitsVerify)
        {
            const uint64_t before = runtime->eeScheduler().currentCycle();
            g_original[slotOf(fn)](rdram, ctx, runtime);
            if (!native || ctx->pc != ra)
                return; // the game's error path, or paused at a checkpoint (resumes there, not compared)
            ++g_movieHeaderChecked;
            const uint64_t spent = runtime->eeScheduler().currentCycle() - before;
            const BitReader game = loadBitReader(rdram, ctx, runtime, r);
            bool same = sameBitReader(game, h.b) && GPR_U64(ctx, 2) == 1u && spent == h.cycles;
            for (uint32_t i = 0; i < o.n; ++i)
            {
                const HeaderOut::Field &f = o.fields[i];
                same &= (f.size == 8 ? READ64(out + f.offset) : READ32(out + f.offset)) == f.value;
            }
            if (!same && ++g_movieHeaderMismatches <= 16)
                logf("movie headers: 0x%06X differs: cycles %llu vs %llu, consumed %llu vs %llu, v0 %llu", fn,
                     static_cast<unsigned long long>(spent), static_cast<unsigned long long>(h.cycles),
                     static_cast<unsigned long long>(game.consumed), static_cast<unsigned long long>(h.b.consumed),
                     static_cast<unsigned long long>(GPR_U64(ctx, 2)));
            return;
        }
        ++g_movieHeaderCalls;
        storeBitReader(rdram, ctx, runtime, r, h.b);
        for (uint32_t i = 0; i < o.n; ++i)
        {
            const HeaderOut::Field &f = o.fields[i];
            if (f.size == 8)
                WRITE64(out + f.offset, f.value);
            else
                WRITE32(out + f.offset, static_cast<uint32_t>(f.value));
        }
        runtime->eeScheduler().accountCycles(h.cycles);
        SET_GPR_U64(ctx, 2, 1u);
        ctx->pc = ra;
    }

    // ---- frame rate (MAXIMO_FRAME_RATE=60, FRAME_RATE.md) ----------------------------------------------------------
    // The game times every frame itself: func_1480C0 turns the EE cycles since the last frame into a delta in 1/60 s
    // (0x25B9F0; 1.18 at 50 Hz, 0.98 at 60 Hz) and moves, animates and counts down by it. A 60 Hz field rate therefore
    // keeps the game's speed and only shows more frames. Only gameplay runs at 60 Hz; the rest stays at 50 Hz:
    // - the front end (boot logos, title, menus, loading screens) times several of its screens in frames, so at
    //   60 Hz they would end early;
    // - the level loop func_1221F0(mode) stores its mode in 0x238FD4; above 0 (the attract demo replaying recorded
    //   input) the delta is fixed at 1.2 per frame, so at 60 Hz it would run 20% fast;
    // - FMV pictures are 25 fps; the player flips the display without calling sceGsSyncV, and 50 Hz keeps its cadence.
    constexpr uint32_t kLevelLoopFn = GA(0x001221F0u);
    constexpr uint32_t kFixedStepModeAddr = GA(0x00238FD4u);
    constexpr uint32_t k60HzFieldMicroseconds = 16667u; // the runtime's NTSC field period
    // The USA and Japanese releases are NTSC: their own rate is 60 Hz, and their frame-counted timings (the front
    // end's screens, the counters and steps paced below) already are for 60, so nothing is paced there.
    constexpr uint32_t kNativeHz = maximo_region::kNativeHz;
    constexpr bool kPalTimings = maximo_region::kPal;
    uint32_t g_frameRateHz = kNativeHz; // the requested rate
    uint32_t g_fieldRateHz = kNativeHz; // the rate in effect
    uint64_t g_lastSyncVTick = 0;  // VBlank tick of the last sceGsSyncV
    uint64_t g_frameRateSwitches = 0;
    bool g_levelLoopRan = false;   // func_1221F0 ran since the last sceGsSyncV
    // NETPLAY SPLIT VIEW: with a joining game watching, the host renders player 1's and player 2's views on
    // alternating level frames; each frame's view is noted at its sceGsSyncV under its guest frame number, and the
    // presenter shows player 1's here and sends player 2's to the joining game.
    bool g_viewP2Now = false;            // this level frame renders player 2's view
    uint64_t g_viewCounter = 0;
    std::atomic<uint8_t> g_viewTag[64];  // by guest frame & 63: 1 = player 2's view
    std::atomic<bool> g_splitActive{false};
    int g_viewLag = 0;                   // SPLIT VIEW DELAY (debug menu): guest frames between rendering and a tag
    bool g_splitEnabled = false;         // SPLIT VIEW (debug menu): off by default - it doubles the host's drawing
    bool splitViewWanted();

    // FAST LOADING: the game loads a level through the CD stream calls, a fixed amount per frame behind its loading
    // screen, so a load lasts a fixed number of frames whatever the PC could do (5 seconds for a world's archive at
    // 50 Hz, with the game thread idle most of each frame). While a stream is open outside gameplay, the field
    // rate goes up to kLoadingHz: the same frames, as fast as the work in them allows. MAXIMO_FAST_LOAD=0: off.
    constexpr uint32_t kLoadingHz = 250u;
    bool g_fastLoad = true;
    bool g_cdStreamOpen = false;
    std::chrono::steady_clock::time_point g_cdStreamLast{};
    bool loadingNow()
    {
        if (!g_fastLoad)
            return false;
        // (between two files the stream is closed for a moment: a quarter of a second of grace)
        return g_cdStreamOpen || std::chrono::steady_clock::now() - g_cdStreamLast < std::chrono::milliseconds(250);
    }
    // After a load the game spends seconds on screens nobody plays: the logo screens at boot, the main menu's
    // opening, a level's start before Maximo can move (5.7 s of 50 Hz frames before survival's arena). Those run at
    // the loading rate too: from the end of a load until gameplay begins, a button is pressed, or 1.5 seconds have
    // gone by (7 seconds of game time).
    uint16_t g_padHeldForRate = 0; // the pad's buttons (debugPadRead)
    std::chrono::steady_clock::time_point g_afterLoadEnd{};
    bool afterLoadNow(bool loading)
    {
        static bool wasLoading = false;
        auto &windowEnd = g_afterLoadEnd;
        const auto now = std::chrono::steady_clock::now();
        if (!g_fastLoad)
            return false;
        if (loading)
        {
            wasLoading = true;
            return false;
        }
        if (wasLoading)
        {
            wasLoading = false;
            windowEnd = now + std::chrono::milliseconds(1500);
        }
        if (g_padHeldForRate != 0)
            windowEnd = now;
        return now < windowEnd;
    }
    void endAfterLoadWindow() { g_afterLoadEnd = std::chrono::steady_clock::now(); } // (gameplay began)
    void cdStStartEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        g_cdStreamOpen = true;
        g_cdStreamLast = std::chrono::steady_clock::now();
        ps2_stubs::sceCdStStart(rdram, ctx, runtime);
    }
    void cdStStopEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        g_cdStreamOpen = false;
        g_cdStreamLast = std::chrono::steady_clock::now();
        ps2_stubs::sceCdStStop(rdram, ctx, runtime);
    }
    void cdStReadEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        g_cdStreamLast = std::chrono::steady_clock::now();
        ps2_stubs::sceCdStRead(rdram, ctx, runtime);
    }

    void setFieldRate(PS2Runtime *runtime, uint32_t hz, const char *why)
    {
        if (hz == g_fieldRateHz)
            return;
        g_fieldRateHz = hz;
        // 50 Hz is the console's own; 60 Hz the runtime's NTSC field; SPLIT VIEW runs twice the gameplay rate.
        runtime->eeScheduler().setVBlankPeriodOverride(hz == kNativeHz ? 0u : hz == 60 ? k60HzFieldMicroseconds : 1000000u / hz);
        const uint64_t n = ++g_frameRateSwitches;
        if (n <= 16 || (n & (n - 1)) == 0)
            logf("[frame-rate] %u Hz (%s) at VBlank %llu, switch #%llu", hz, why,
                 static_cast<unsigned long long>(runtime->eeScheduler().currentVSyncTick()), static_cast<unsigned long long>(n));
    }

    // The game also keeps two frame counters, incremented once per frame by func_1480C0 (0x25B9E0: game frames, held
    // while paused; 0x25B9E4: all frames; both reset by func_121050). Some code times by them in PAL frames: deadlines
    // of "counter + seconds * 50" (func_178F30), countdowns run on even frames (func_157F10, func_12CB10), effects
    // spawned every 8th frame (func_125180), blinking (counter & 8). At 60 Hz each counter skips one increment in six,
    // so it advances 50 times per second as at 50 Hz. Only an increment to an odd value is skipped: the game then sees
    // that odd value for two frames, while every even value, on which the every-2nd/4th/8th-frame work runs, still
    // appears exactly once. The player's idle counter (gp-0x7740, func_13D020: +1 per standing frame, the idle fidget
    // animation after 150) is paced the same way, so it plays after 3 s as at 50 Hz.
    // Any rate above the release's own (60 or 120 Hz in PAL, 120 in the NTSC releases) is paced the same way, by a
    // balance: each frame that moves the counter earns the release's rate and the increment costs the field rate;
    // an increment to an odd value is undone while the balance is short. At 60 Hz in PAL that is one in six (as
    // before); at twice the rate every even value still shows once and the odd ones fill the frames between.
    struct PacedCounter
    {
        uint32_t addr;
        uint32_t last = 0;
        int64_t balance = 0;
        bool valid = false;
    };
    PacedCounter g_pacedCounters[3] = {{GA(0x0025B9E0u)}, {GA(0x0025B9E4u)}, {GA(0x00239030u)}};
    uint64_t g_counterHolds = 0;

    void paceFrameCounter(PacedCounter &c, bool pace, uint32_t nativeHz, uint32_t fieldHz)
    {
        uint32_t v = rd32(c.addr);
        if (!pace || !c.valid || v - c.last > 1) // the release's own rate, the first frame, or a reset
        {
            c = {c.addr, v, 0, true};
            return;
        }
        if (v == c.last)
            return;
        c.balance += static_cast<int64_t>(nativeHz) - static_cast<int64_t>(fieldHz);
        if ((v & 1) && c.balance < 0)
        {
            wr32(c.addr, --v); // undo this frame's increment: the game sees the odd value again next frame
            c.balance += fieldHz;
            ++g_counterHolds;
        }
        c.last = v;
    }

    // HIGH FRAME RATE: gameplay above the release's own rate. The game steps by its measured time, but a few things
    // go by frames; they are paced to the release's own rate (above and below).
    bool highRateNow();
    float g_highRateCredit[2] = {0.0f, 0.0f}; // camera shake, lightning: frames owed at the release's own rate
    // This frame counts as a frame at the release's own rate (a share of nativeHz / fieldHz of the frames do).
    bool highRateStep(float &credit)
    {
        credit += static_cast<float>(kNativeHz) / static_cast<float>(std::max<uint32_t>(1u, g_fieldRateHz));
        if (credit >= 1.0f)
        {
            credit -= 1.0f;
            return true;
        }
        return false;
    }

    // ADAPTIVE FRAME RATE: at 60 Hz a frame has 16.7 ms. A scene with many enemies needs more than that (the VU1
    // worker alone took 16-20 ms a frame with 8 of them on screen), and since guest time only moves as fast as the
    // frames get done, the game then ran in slow motion (70-90% speed, 45 frames a second). The console's own 50 Hz
    // gives a frame 20 ms: such a scene runs there at full speed, 50 frames a second. So while the work of a frame
    // (g_frameBusyMs: the frame without its wait for VBlank, smoothed over about 20 frames) does not fit 60 Hz the
    // gameplay runs at 50 Hz, and goes back to 60 once it has fitted comfortably for 3 seconds. The game measures
    // its own time step, and everything paced for 60 Hz here is already switched by g_fieldRateHz (front end, movies
    // and the attract demo change rate the same way). MAXIMO_ADAPTIVE_RATE=0: off.
    bool g_adaptiveRate = false; // (removed at the player's request: the rate stays at what was asked for)
    double g_frameBusyMs = 0.0; // smoothed (gsSyncVEntry)
    bool heavySceneNow()
    {
        static bool heavy = false;
        static uint32_t calmFrames = 0;
        static uint64_t switches = 0;
        if (!g_adaptiveRate)
            return false;
        if (!heavy)
        {
            if (g_frameBusyMs > 16.0)
            {
                heavy = true;
                calmFrames = 0;
                if (++switches <= 20)
                    logf("[frame-rate] heavy scene (a frame's work %.1f ms): 50 Hz until it fits 60 Hz again", g_frameBusyMs);
            }
        }
        else if (g_frameBusyMs < 14.2)
        {
            if (++calmFrames >= 150u)
            {
                heavy = false;
                if (switches <= 20)
                    logf("[frame-rate] the scene fits 60 Hz again (a frame's work %.1f ms)", g_frameBusyMs);
            }
        }
        else
            calmFrames = 0;
        return heavy;
    }

    // Gameplay at a rate above the release's own (the frame that just ran).
    bool highRateNow() { return g_frameRateHz > kNativeHz && g_fieldRateHz == g_frameRateHz; }

    // Every frame: the requested rate for a gameplay frame, otherwise the release's own.
    void frameRateOnSyncV(PS2Runtime *runtime)
    {
        g_lastSyncVTick = runtime->eeScheduler().currentVSyncTick();
        for (PacedCounter &c : g_pacedCounters) // for the frame that just ran, at the rate it ran at
            paceFrameCounter(c, highRateNow(), kNativeHz, g_fieldRateHz);
        const bool level = std::exchange(g_levelLoopRan, false);
        const bool loading = !level && loadingNow();
        const bool afterLoad = afterLoadNow(loading);
        if (level)
            endAfterLoadWindow();
        if (loading)
            setFieldRate(runtime, kLoadingHz, "loading: fast");
        else if (!level && afterLoad)
            setFieldRate(runtime, kLoadingHz, "after a load: fast until play begins");
        else if (!level)
            setFieldRate(runtime, kNativeHz, "front end");
        else if (static_cast<int32_t>(rd32(kFixedStepModeAddr)) > 0)
            setFieldRate(runtime, kNativeHz, "attract demo: fixed step");
        else if (g_splitActive)
            setFieldRate(runtime, g_frameRateHz * 2u, "gameplay, split view: every other frame is player 2's view");
        else
            setFieldRate(runtime, g_frameRateHz, "gameplay");
    }

    void player2AfterFrame(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t mode);
    // Always hooked: the 60 Hz pacing (when MAXIMO_FRAME_RATE=60) and PLAYER 2, which runs after each level frame.
    void levelLoopEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        g_levelLoopRan = true;
        const uint32_t mode = reg(ctx, 4);
        const bool split = splitViewWanted();
        g_splitActive = split;
        // SPLIT VIEW: frames come in pairs - the game's own frame (player 1's view), then a view frame that only
        // draws the world again from player 2's camera. A view frame moves nothing: its time step is held near 0
        // (applyFrameControls), the level scripts and particles are paused (pausableEntry), player 1 gets the same pad
        // again (scriptedPadRead), the game's frame counters are put back, and player 2 takes no turn.
        g_viewP2Now = split && (++g_viewCounter & 1u) == 0u;
        if (g_viewP2Now)
        {
            uint32_t counters[3];
            for (int i = 0; i < 3; ++i)
                counters[i] = rd32(g_pacedCounters[i].addr);
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            for (int i = 0; i < 3; ++i)
                wr32(g_pacedCounters[i].addr, counters[i]);
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        player2AfterFrame(rdram, ctx, runtime, mode);
    }

    // A display flip two or more fields after the last sceGsSyncV is the FMV player's.
    void frameRateOnFlip(PS2Runtime *runtime)
    {
        if (runtime->eeScheduler().currentVSyncTick() - g_lastSyncVTick >= 2)
            setFieldRate(runtime, kNativeHz, "movie");
    }

    // A few gameplay steps are per frame rather than by the delta; at 60 Hz they are scaled to 50 per second.
    // - func_167650(node, f12 = step) advances a node's animation. func_16A500 advances a model's animations by the
    //   delta, except the one at +0x928, which it steps by 1.0 per frame. (The front end's constant steps, such as the
    //   loading screen's shield at 1.2 per frame, run at 50 Hz and are left alone.)
    constexpr uint32_t kAnimStepFn = GA(0x00167650u);
    constexpr uint32_t kModelFrameAnimRa = GA(0x0016A598u);
    uint64_t g_fixedAnimSteps = 0;
    void animStepEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (highRateNow() && reg(ctx, 31) == kModelFrameAnimRa)
        {
            ctx->f[12] *= static_cast<float>(kNativeHz) / static_cast<float>(g_fieldRateHz);
            ++g_fixedAnimSteps;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // - Camera shake (func_1406C0, once per frame on the camera object, mode +0x28 == 0). Before a shake starts, a
    //   delay (+0x610) counts down by 1 per frame; at 60 Hz it is given back a sixth of that step first. The shake
    //   itself (+0x60C, of +0x614 steps, which also fades its amplitude) is throttled by the global gp-0x773C: while
    //   it is above 0 the frame only takes 1.2 off it, otherwise the frame adds 2.0 and moves the shake one step. That
    //   is a step on 1.2 / 3.2 of the frames, 18.75 per second at 50 Hz. At 60 Hz the frame takes 10/11 instead, a
    //   step on (10/11) / (32/11) = 5/16 of the frames: 18.75 per second again, so the shake lasts as long.
    // - Style 1 (the checkpoint slam) is a scripted move instead: while +0x640 is set, func_140530 walks a table at
    //   +0x2C of 20-byte segments {dx, dy, dz, frames, scale}. Starting a segment (+0x0 set) sets the per-frame move
    //   +0x10..+0x18 = d * scale / frames and the counter +0x20 = frames; each frame with the counter above 0 takes 1
    //   off it and adds the move to the eye (+0x1A0) and target (+0x1D0). The update builds the eye on the previous
    //   frame's eye (+0x220), so the moves add up. At 60 Hz each frame moves 5/6 of the step and takes 5/6 off the
    //   counter: the same distance in the same time. (A segment's first frame is set up inside the call, so its sixth
    //   is given back on the next frame.)
    constexpr uint32_t kCameraShakeFn = GA(0x001406C0u);
    constexpr uint32_t kShakeThrottleAddr = GA(0x00239034u); // gp-0x773C
    uint64_t g_shakeFrames = 0;
    float g_slamCounterOwed = 0.0f;
    void slamShakePace(uint8_t *rdram, uint32_t cam)
    {
        auto rf = [&](uint32_t a) { float v; std::memcpy(&v, rdram + a, sizeof(v)); return v; };
        auto wf = [&](uint32_t a, float v) { std::memcpy(rdram + a, &v, sizeof(v)); };
        float move[3];
        if (rdram[cam] != 0) // a segment starts in this call: work out its move the way func_140530 does
        {
            const uint32_t seg = (rd32(cam + 0x2C) + rd32(cam + 0x24) * 20u) & 0x01FFFFFFu;
            if (seg + 20u > PS2_RAM_SIZE)
                return;
            for (int i = 0; i < 3; ++i)
                move[i] = rf(seg + 4u * i) * rf(seg + 0x10u) / rf(seg + 0xCu);
            g_slamCounterOwed = 1.0f / 6.0f;
        }
        else
        {
            const float counter = rf(cam + 0x20);
            if (!(counter > 0.0f))
                return;
            for (int i = 0; i < 3; ++i)
                move[i] = rf(cam + 0x10 + 4u * i);
            wf(cam + 0x20, counter + 1.0f / 6.0f + g_slamCounterOwed);
            g_slamCounterOwed = 0.0f;
        }
        for (int i = 0; i < 3; ++i)
        {
            if (!std::isfinite(move[i]))
                return;
        }
        for (int i = 0; i < 3; ++i)
        {
            wf(cam + 0x1A0 + 4u * i, rf(cam + 0x1A0 + 4u * i) - move[i] / 6.0f);
            wf(cam + 0x1D0 + 4u * i, rf(cam + 0x1D0 + 4u * i) - move[i] / 6.0f);
        }
        ++g_shakeFrames;
    }

    void cameraShakeEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t cam = reg(ctx, 4);
        if (highRateNow() && !(kPalTimings && g_fieldRateHz == 60) && cam && rd32(cam + 0x28) == 0)
        {
            // Other rates (120 Hz, and the NTSC releases, whose shake code differs): the shake's state only moves on
            // the frames that count at the release's own rate; on the others it is put back after the frame.
            const bool step = highRateStep(g_highRateCredit[0]);
            uint32_t saved[3] = {rd32(kShakeThrottleAddr), rd32(cam + 0x60Cu), rd32(cam + 0x610u)};
            const uint8_t shaking = rdram[(cam + 0x640u) & PS2_RAM_MASK];
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            if (!step)
            {
                wr32(kShakeThrottleAddr, saved[0]);
                wr32(cam + 0x60Cu, saved[1]);
                wr32(cam + 0x610u, saved[2]);
                rdram[(cam + 0x640u) & PS2_RAM_MASK] = shaking;
                ++g_shakeFrames;
            }
            return;
        }
        if (kPalTimings && g_fieldRateHz == 60 && g_frameRateHz == 60 && cam && rd32(cam + 0x28) == 1 &&
            rdram[(cam + 0x640u) & PS2_RAM_MASK] != 0)
            slamShakePace(rdram, cam); // (the checkpoint slam, from the Maximo_Recomp_Mods fork)
        else if (kPalTimings && g_fieldRateHz == 60 && g_frameRateHz == 60 && cam && rd32(cam + 0x28) == 0)
        {
            const bool shaking = rdram[cam + 0x640] != 0;
            const uint32_t addr = shaking ? kShakeThrottleAddr : cam + 0x610u;
            float t;
            std::memcpy(&t, rdram + addr, sizeof(t));
            if (t > 0.0f)
            {
                t += shaking ? 1.2f - 10.0f / 11.0f : 1.0f / 6.0f;
                std::memcpy(rdram + addr, &t, sizeof(t));
                ++g_shakeFrames;
            }
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // ---- camera probe and free camera (x-camera) ----------------------------------------------------------------------
    // func_13D020 is the camera update, called once per frame from func_1221F0 with the camera object (0x239890). It
    // starts by copying +0x1A0 to +0x220: +0x1A0 is the eye, +0x1D0 the look-at target, +0x1F0 the followed point
    // (Maximo); y is up. The update often returns to the run loop part way: its line-of-sight query (func_153170)
    // jumps into another recompiled piece (0x1530F4), and the run loop then finishes the update without returning
    // through a hook (an EE checkpoint can do the same). So nothing is done after it returns. Instead the change is made on entry to func_13C8B0(cam), which the
    // update calls at the end of each camera branch to build the view matrix (+0xA0, via func_13C6C0) from the eye and
    // target and to copy the eye to +0x1B0; the update's own entry puts the game's eye back first. func_1483C0, the
    // level loop's next call (0x1222EC), ends the frame's update. So the change is draw-only for the game's camera
    // logic, while everything after the matrix (Maximo's camera-relative movement included) sees the turned view.
    // MAXIMO_CAMERA_LOG=1 logs the vectors twice a second and camera type changes. MAXIMO_CAMERA_TEST=<deg>[,x|y|z]
    // turns the eye around the target by that angle (probe).
    constexpr uint32_t kCameraUpdateFn = GA(0x0013D020u);
    constexpr uint32_t kCameraApplyFn = GA(0x001483C0u); // the level loop's next call: the frame's update is over
    constexpr uint32_t kCameraViewFn = GA(0x0013C8B0u);  // (cam): view matrix from the eye and target
    constexpr uint32_t kLineOfSightFn = GA(0x00153640u); // (from, to): 1 and `to` moved to the hit when blocked
    bool g_camLog = false;
    float g_camTestDeg = 0.0f;
    int g_camTestAxis = 1;
    bool g_camSaved = false;
    uint32_t g_camObj = 0;
    uint32_t g_camPending = 0; // camera object of the update in progress (until func_1483C0)
    bool g_camAppliedThisUpdate = false;
    float g_camSavedEye[4]{};
    uint64_t g_camCalls = 0;

    void camVec(uint32_t addr, float *v) { std::memcpy(v, mem(addr), 16); }

    // ---- free camera (MAXIMO_CAMERA=orbit) ---------------------------------------------------------------------------
    // The right stick orbits the eye around the game's look-at target: left/right turns (yaw about y), up/down tilts
    // (pitch, kept between kPitchMin and kPitchMax). Until the stick is first moved the game's own camera shows
    // unchanged. Then the view direction is the player's (the game's camera keeps chasing behind Maximo underneath; only
    // its distance to the target is used). Turning speeds up and slows down smoothly (kTurnSmoothing) and small stick
    // moves turn slowly (response curve). After kReturnDelay seconds without camera input the view swings back to the
    // game's (behind Maximo) on a soft critically damped spring; R3 does the same at once on a stiffer one. The game's
    // own line-of-sight query (func_153640, called from the hook with EE checkpoints held) keeps the eye in front of
    // walls: the camera pulls in at once and eases back out when the way is clear.
    // It only acts on the follow camera (+0x248 type 0) in normal play: any other camera type (scripted shots, level-
    // start views), the attract demo (fixed-step mode 0x238FD4 > 0) or a jump of the followed point (level start,
    // respawn, teleport) cuts straight back to the game's camera. While the game is paused the camera holds.
    // MAXIMO_CAMERA_SPEED = turn speed in degrees per second at full stick (default 150; tilting runs at 60% of it).
    // Stick up looks up (the eye goes down), as in most current games; MAXIMO_CAMERA_INVERT_Y=1 flips that.
    // MAXIMO_CAMERA_RETURN = seconds without camera input before the view swings back (default 3, 0 = never).
    enum class OrbitMode
    {
        Game,      // the game's camera, untouched
        Player,    // the player's view direction
        Returning, // swinging back to the game's
    };
    struct OrbitState
    {
        bool on = false;
        bool invertY = false;
        float speed = 150.0f * 3.14159265f / 180.0f; // radians per second at full stick
        float returnDelay = 3.0f;
        OrbitMode mode = OrbitMode::Game;
        bool fastReturn = false;
        float yaw = 0.0f, pitch = 0.0f;       // the view shown (eye around the target)
        float yawVel = 0.0f, pitchVel = 0.0f; // radians per second
        float idle = 0.0f;                    // seconds without camera input
        float shownDist = 0.0f;               // eye distance shown (pulled in by walls)
        float lift = 0.0f;                    // extra pitch while pulled in (looks down over Maximo)
        uint32_t lastFrame = 0;
        int sameFrame = 0;
        float lastFollow[3]{};
        bool haveFollow = false;
        // right stick and R3 from the last scePadRead (analog mode only), written by scriptedPadRead
        uint8_t rx = 0x80, ry = 0x80;
        bool r3 = false, r3Was = false;
        uint64_t takes = 0, wallHits = 0, losCalls = 0, losFailures = 0;
    };
    OrbitState g_orbit;
    constexpr float kPitchMin = -0.17f;      // ~-10 degrees: a little below the target
    constexpr float kPitchMax = 1.22f;       // ~70 degrees: high above
    constexpr float kStickDeadZone = 0.18f;
    constexpr float kTurnSmoothing = 0.08f;  // seconds: time constant of the turn speed following the stick
    constexpr float kReturnSpringSlow = 2.2f; // rad/s: the spring after kReturnDelay (settles in ~2 s)
    constexpr float kReturnSpringFast = 7.0f; // rad/s: the spring after R3 (~0.6 s)
    constexpr float kWallMargin = 0.6f;      // eye kept this far in front of a wall hit
    constexpr float kMinDist = 1.2f;
    constexpr float kWallEaseOut = 0.35f;    // seconds: time constant of moving back out from a wall
    constexpr float kWallLift = 0.7f;        // radians (~40 degrees) of extra pitch when pulled all the way in
    constexpr float kLiftSmoothing = 0.15f;  // seconds
    constexpr uint32_t kGameFrameCounterAddr = GA(0x0025B9E0u);
    constexpr uint32_t kFrameDeltaAddr = GA(0x0025B9F0u); // this frame's step in 1/60 s

    float orbitStick(uint8_t b)
    {
        const float v = std::clamp((static_cast<int>(b) - 128) / 127.0f, -1.0f, 1.0f);
        const float a = std::fabs(v);
        if (a < kStickDeadZone)
            return 0.0f;
        const float t = (a - kStickDeadZone) / (1.0f - kStickDeadZone);
        return std::copysign(t * (0.35f + 0.65f * t), v); // gentle near the centre, full at the edge
    }

    float wrapAngle(float a)
    {
        constexpr float kPi = 3.14159265f;
        while (a > kPi)
            a -= 2.0f * kPi;
        while (a < -kPi)
            a += 2.0f * kPi;
        return a;
    }

    void orbitSetMode(OrbitMode mode, const char *why)
    {
        if (mode == g_orbit.mode)
            return;
        if (g_camLog || g_orbit.on)
            logf("camera orbit: %s -> %s (%s) at frame %llu",
                 g_orbit.mode == OrbitMode::Game ? "game" : g_orbit.mode == OrbitMode::Player ? "player" : "returning",
                 mode == OrbitMode::Game ? "game" : mode == OrbitMode::Player ? "player" : "returning", why,
                 static_cast<unsigned long long>(g_guestFrame.load()));
        g_orbit.mode = mode;
    }

    extern "C" void ps2xHoldEeCheckpoints(int hold);

    // The game's line-of-sight query from the host: func_153640(from, to) on vectors below the guest stack, with EE
    // checkpoints held (nothing would resume it if one stopped it) and every register put back afterwards.
    bool guestLineOfSight(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, const float *from, float *to)
    {
        static R5900Context saved;
        saved = *ctx;
        const uint32_t sp = (GPR_U32(ctx, 29) - 0x400u) & ~0xFu;
        const uint32_t a = sp + 0x10u, b = sp + 0x20u;
        constexpr uint32_t kReturnMark = 0x00100004u;
        std::memcpy(mem(a), from, 16);
        std::memcpy(mem(b), to, 16);
        SET_GPR_U32(ctx, 29, sp);
        SET_GPR_U32(ctx, 4, a);
        SET_GPR_U32(ctx, 5, b);
        SET_GPR_U32(ctx, 31, kReturnMark);
        ctx->pc = kLineOfSightFn;
        ps2xHoldEeCheckpoints(1);
        // Like the run loop: a jump into another recompiled piece returns with $pc there, so dispatch until the call
        // reaches its return mark.
        for (int steps = 0; steps < 100000 && ctx->pc != kReturnMark && ctx->pc != 0u; ++steps)
        {
            const uint32_t pc = ctx->pc;
            if (!runtime->hasFunction(pc))
                break;
            runtime->lookupFunction(pc)(rdram, ctx, runtime);
        }
        ps2xHoldEeCheckpoints(-1);
        const bool ok = ctx->pc == kReturnMark;
        const bool hit = ok && GPR_U32(ctx, 2) != 0;
        if (hit)
            std::memcpy(to, mem(b), 16);
        ++g_orbit.losCalls;
        if (!ok && ++g_orbit.losFailures <= 4)
            logf("camera orbit: line-of-sight call ended at pc %06X, not the return mark", ctx->pc);
        *ctx = saved;
        return hit;
    }

    // Once per frame (first view-matrix build of the update). A later build in the same update only places the eye
    // again from the same state (placeOnly).
    void orbitCamera(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t cam, bool placeOnly)
    {
        OrbitState &o = g_orbit;
        float eye[4], tgt[4], fol[4];
        camVec(cam + 0x1A0, eye);
        camVec(cam + 0x1D0, tgt);
        camVec(cam + 0x1F0, fol);
        if (placeOnly)
        {
            if (o.mode == OrbitMode::Game || o.shownDist <= 0.0f)
                return;
            float out[4];
            std::memcpy(out, eye, 16);
            const float p = std::min(o.pitch + o.lift, kPitchMax);
            out[0] = tgt[0] + std::cos(p) * std::sin(o.yaw) * o.shownDist;
            out[1] = tgt[1] + std::sin(p) * o.shownDist;
            out[2] = tgt[2] + std::cos(p) * std::cos(o.yaw) * o.shownDist;
            if (!g_camSaved)
                std::memcpy(g_camSavedEye, eye, 16);
            g_camSaved = true;
            g_camObj = cam;
            std::memcpy(mem(cam + 0x1A0), out, 16);
            return;
        }
        const bool r3Edge = o.r3 && !o.r3Was;
        o.r3Was = o.r3;
        // Eligibility: the follow camera in normal play, no jump of the followed point.
        const float jx = fol[0] - o.lastFollow[0], jy = fol[1] - o.lastFollow[1], jz = fol[2] - o.lastFollow[2];
        const bool jumped = o.haveFollow && jx * jx + jy * jy + jz * jz > 20.0f * 20.0f;
        std::memcpy(o.lastFollow, fol, sizeof o.lastFollow);
        o.haveFollow = true;
        const uint32_t type = rd32(cam + 0x248);
        // (+0x28 is not a camera mode: func_13C660 starts a shake and stores the shake's style there, e.g. 1 for the
        // checkpoint slam, and it stays set after the shake.)
        if (type != 0 || static_cast<int32_t>(rd32(kFixedStepModeAddr)) > 0 || jumped)
        {
            orbitSetMode(OrbitMode::Game, type != 0 ? "scripted camera" : jumped ? "followed point jumped" : "attract demo");
            o.yawVel = o.pitchVel = 0.0f;
            return;
        }
        // The game's own view, as yaw/pitch/distance of the eye around the target.
        const float dx = eye[0] - tgt[0], dy = eye[1] - tgt[1], dz = eye[2] - tgt[2];
        const float flat = std::sqrt(dx * dx + dz * dz), dist = std::sqrt(dx * dx + dy * dy + dz * dz);
        if (dist < 0.01f)
            return;
        const float gameYaw = std::atan2(dx, dz), gamePitch = std::atan2(dy, flat);
        // Time: the game's frame step (1/60 s units). Paused = the game frame counter held for two calls running (at
        // 60 Hz the frame-rate code holds it for one frame in six, which is not a pause).
        const uint32_t frame = rd32(kGameFrameCounterAddr);
        o.sameFrame = frame == o.lastFrame ? o.sameFrame + 1 : 0;
        o.lastFrame = frame;
        float stepDt;
        std::memcpy(&stepDt, mem(kFrameDeltaAddr), 4);
        const float dt = o.sameFrame >= 2 || !(stepDt > 0.0f && stepDt < 10.0f) ? 0.0f : stepDt / 60.0f;

        const float sx = orbitStick(o.rx), sy = orbitStick(o.ry) * (o.invertY ? -1.0f : 1.0f);
        const bool input = sx != 0.0f || sy != 0.0f;
        if (o.mode == OrbitMode::Game)
        {
            o.yaw = gameYaw;
            o.pitch = gamePitch;
            o.shownDist = dist;
            o.lift = 0.0f;
        }
        if (input && dt > 0.0f)
        {
            if (o.mode == OrbitMode::Game)
                ++o.takes;
            orbitSetMode(OrbitMode::Player, "right stick");
            o.idle = 0.0f;
        }
        else if (r3Edge && o.mode != OrbitMode::Game)
        {
            o.fastReturn = true;
            orbitSetMode(OrbitMode::Returning, "R3");
        }
        else if (o.mode == OrbitMode::Player && dt > 0.0f)
        {
            o.idle += dt;
            if (o.returnDelay > 0.0f && o.idle >= o.returnDelay)
            {
                o.fastReturn = false;
                orbitSetMode(OrbitMode::Returning, "no camera input");
            }
        }
        if (o.mode == OrbitMode::Game)
            return;

        if (dt > 0.0f)
        {
            if (o.mode == OrbitMode::Player)
            {
                // Turn speed eases toward the stick's.
                const float k = 1.0f - std::exp(-dt / kTurnSmoothing);
                o.yawVel += (-sx * o.speed - o.yawVel) * k;
                o.pitchVel += (sy * o.speed * 0.6f - o.pitchVel) * k;
            }
            else
            {
                // Critically damped spring toward the game's view.
                const float w = o.fastReturn ? kReturnSpringFast : kReturnSpringSlow;
                const float ey = wrapAngle(gameYaw - o.yaw), ep = gamePitch - o.pitch;
                o.yawVel += (w * w * ey - 2.0f * w * o.yawVel) * dt;
                o.pitchVel += (w * w * ep - 2.0f * w * o.pitchVel) * dt;
                if (std::fabs(ey) < 0.01f && std::fabs(ep) < 0.01f && std::fabs(o.yawVel) < 0.05f &&
                    std::fabs(o.pitchVel) < 0.05f)
                {
                    orbitSetMode(OrbitMode::Game, "back behind Maximo");
                    o.yawVel = o.pitchVel = 0.0f;
                    return;
                }
            }
            o.yaw = wrapAngle(o.yaw + o.yawVel * dt);
            o.pitch += o.pitchVel * dt;
            if (o.pitch < kPitchMin || o.pitch > kPitchMax)
            {
                o.pitch = std::clamp(o.pitch, kPitchMin, kPitchMax);
                o.pitchVel = 0.0f;
            }
        }
        // Pulled in by a wall: tilt up toward kWallLift so the view looks down over Maximo instead of into him.
        if (dt > 0.0f)
        {
            const float pulled = std::clamp(1.0f - (o.shownDist - kMinDist) / std::max(0.01f, dist - kMinDist), 0.0f, 1.0f);
            o.lift += (pulled * kWallLift - o.lift) * (1.0f - std::exp(-dt / kLiftSmoothing));
        }
        const float shownPitch = std::min(o.pitch + o.lift, kPitchMax);
        const float dir[3] = {std::cos(shownPitch) * std::sin(o.yaw), std::sin(shownPitch),
                              std::cos(shownPitch) * std::cos(o.yaw)};
        // Walls: the game's line of sight from the target to where the eye would be.
        float want[4] = {tgt[0] + dir[0] * dist, tgt[1] + dir[1] * dist, tgt[2] + dir[2] * dist, eye[3]};
        float allowed = dist;
        if (guestLineOfSight(rdram, ctx, runtime, tgt, want))
        {
            const float hx = want[0] - tgt[0], hy = want[1] - tgt[1], hz = want[2] - tgt[2];
            allowed = std::max(kMinDist, std::sqrt(hx * hx + hy * hy + hz * hz) - kWallMargin);
            ++o.wallHits;
        }
        if (allowed < o.shownDist || o.shownDist <= 0.0f)
            o.shownDist = allowed;
        else if (dt > 0.0f)
            o.shownDist += (allowed - o.shownDist) * (1.0f - std::exp(-dt / kWallEaseOut));
        float out[4];
        std::memcpy(out, eye, 16);
        for (int i = 0; i < 3; ++i)
            out[i] = tgt[i] + dir[i] * o.shownDist;
        std::memcpy(g_camSavedEye, eye, 16);
        g_camSaved = true;
        g_camObj = cam;
        std::memcpy(mem(cam + 0x1A0), out, 16);
    }

    void cameraTestTurn(uint32_t cam)
    {
        float eye[4], tgt[4];
        camVec(cam + 0x1A0, eye);
        camVec(cam + 0x1D0, tgt);
        std::memcpy(g_camSavedEye, eye, 16);
        g_camSaved = true;
        g_camObj = cam;
        const float a = g_camTestDeg * 3.14159265f / 180.0f, c = std::cos(a), s = std::sin(a);
        const int i = g_camTestAxis == 0 ? 1 : 0, j = g_camTestAxis == 2 ? 1 : 2; // the plane turned
        const float di = eye[i] - tgt[i], dj = eye[j] - tgt[j];
        float out[4];
        std::memcpy(out, eye, 16);
        out[i] = tgt[i] + di * c - dj * s;
        out[j] = tgt[j] + di * s + dj * c;
        std::memcpy(mem(cam + 0x1A0), out, 16);
    }

    // (func_13D020 is also hooked as playerUpdateEntry, which runs this first: one hook per address.)
    void cameraUpdatePre(R5900Context *ctx)
    {
        if (!(g_camLog || g_camTestDeg != 0.0f || g_orbit.on))
            return;
        const uint32_t cam = reg(ctx, 4);
        if (g_camSaved && cam == g_camObj)
            std::memcpy(mem(cam + 0x1A0), g_camSavedEye, 16);
        g_camSaved = false;
        g_camPending = cam;
        g_camAppliedThisUpdate = false;
    }

    void cameraViewEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t cam = reg(ctx, 4);
        if (cam && cam == g_camPending)
        {
            const bool again = std::exchange(g_camAppliedThisUpdate, true);
            if (g_orbit.on)
                orbitCamera(rdram, ctx, runtime, cam, again);
            else if (g_camTestDeg != 0.0f && !again)
                cameraTestTurn(cam);
            SET_GPR_U32(ctx, 4, cam); // the line-of-sight call put every register back; keep a0 explicit
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    void cameraApplyEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (const uint32_t cam = std::exchange(g_camPending, 0u))
        {
            ++g_camCalls;
            // Events (camera type, shake style +0x28, whether the update built its view through func_13C8B0) are logged
            // whenever the free camera is on, so a tester's log shows why it handed back.
            if (g_camLog || g_orbit.on)
            {
                static uint32_t lastType = ~0u, lastReq = ~0u, lastMode = ~0u;
                static int lastBuilt = -1;
                const uint32_t type = rd32(cam + 0x248), req = rd32(cam + 0x5FC), mode = rd32(cam + 0x28);
                const int built = g_camAppliedThisUpdate ? 1 : 0;
                if (type != lastType || req != lastReq || mode != lastMode || built != lastBuilt)
                    logf("camera %llu type %u -> %u, request %d -> %d, shake style %u -> %u, view built %d, +0x604 %u, "
                         "frame %llu",
                         static_cast<unsigned long long>(g_camCalls), lastType, type, static_cast<int>(lastReq),
                         static_cast<int>(req), lastMode, mode, built, rd32(cam + 0x604),
                         static_cast<unsigned long long>(g_guestFrame.load()));
                lastType = type;
                lastReq = req;
                lastMode = mode;
                lastBuilt = built;
            }
            if (g_camLog)
            {
                float eye[4], tgt[4], fol[4];
                camVec(cam + 0x1A0, eye);
                camVec(cam + 0x1D0, tgt);
                camVec(cam + 0x1F0, fol);
                if (g_camCalls % 25 == 0)
                {
                    const float dx = eye[0] - tgt[0], dy = eye[1] - tgt[1], dz = eye[2] - tgt[2];
                    logf("camera %llu obj %08X mode %u: eye (%.1f %.1f %.1f %.2f) target (%.1f %.1f %.1f) follow (%.1f "
                         "%.1f %.1f) eye-target (%.1f %.1f %.1f) dist %.1f | orbit %d yaw %.0f pitch %.0f shown %.1f "
                         "walls %llu/%llu",
                         static_cast<unsigned long long>(g_camCalls), cam, rd32(cam + 0x28), eye[0], eye[1], eye[2],
                         eye[3], tgt[0], tgt[1], tgt[2], fol[0], fol[1], fol[2], dx, dy, dz,
                         std::sqrt(dx * dx + dy * dy + dz * dz), static_cast<int>(g_orbit.mode),
                         g_orbit.yaw * 57.2958f, g_orbit.pitch * 57.2958f, g_orbit.shownDist,
                         static_cast<unsigned long long>(g_orbit.wallHits),
                         static_cast<unsigned long long>(g_orbit.losCalls));
                }
            }
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // - Lightning (the weather object's func_170AC0, once per frame): while lightning is on (+0xA1, +0x2F0 and +0x2F4
    //   set) a countdown at +0x300 drops by 1 per frame and, at 0, strikes (func_17E6C0: flash and thunder) and draws
    //   the next wait from +0x2F8..+0x2FC frames. At 60 Hz every sixth frame gives a step back, so it counts 50 a
    //   second and lightning strikes as often as at 50 Hz. (The flash itself lasts 2 to 4 frames and is left alone.)
    constexpr uint32_t kLightningFn = GA(0x00170AC0u);
    uint64_t g_lightningFrames = 0;
    void lightningEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t obj = reg(ctx, 4);
        if (highRateNow() && obj && rdram[(obj + 0xA1) & PS2_RAM_MASK] && rd32(obj + 0x2F0) && rd32(obj + 0x2F4))
        {
            // The countdown steps only on the frames that count at the release's own rate (one in six skipped at
            // 60 Hz in PAL, every other one at 120 Hz in NTSC).
            const uint32_t wait = rd32(obj + 0x300);
            if (!highRateStep(g_highRateCredit[1]) && wait > 1 && wait < 0x80000000u)
            {
                wr32(obj + 0x300, wait + 1);
                ++g_lightningFrames;
            }
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    void survivalClockTick(); // (SURVIVAL TIMER, after g_survival)

    void gsSyncVEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        // FAST LOADING: every finished frame is marked; when none has come for 4 fields the game is loading, and
        // the scheduler stops holding guest time to the clock until the next frame (EeScheduler's
        // processDueDeadlines). A load then takes what this computer needs for it, not what the PS2 took.
        ps2_host_profile::noteFrame(runtime->eeScheduler().currentVSyncTick());
        {
            static uint64_t logged = 0;
            const uint64_t fields = ps2_host_profile::g_fastForwardFields.load(std::memory_order_relaxed);
            if (fields != logged)
            {
                logf("fast loading: %llu fields went by without waiting for the clock (%.1f s of PS2 time)",
                     static_cast<unsigned long long>(fields - logged), static_cast<double>(fields - logged) / kNativeHz);
                logged = fields;
            }
        }
        frameRateOnSyncV(runtime); // (every frame: the fast loading and the start of play switch rates at 50 Hz too)
        survivalClockTick();
        // Frame boundary: the frame's GS work must be complete before it is presented. With pipelined frames the
        // worker presents it once that work is done (vu1ThreadFrame below) and the game goes on to the next frame.
        ps2_vu1_overlap_model::sync(ps2_vu1_overlap_model::kSyncVSync);
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        const bool pipelined = g_gpuMode && g_gpuShadow && runtime->memory().vu1ThreadPipelined();
#else
        const bool pipelined = false;
#endif
        if (!pipelined)
            runtime->memory().vu1ThreadWait(Vu1Thread::kWaitFrame);
        static uint64_t guestFrame = 0;
        static const char *captureFrames = std::getenv("PS2_GS_CAPTURE_FRAMES");
        static const char *benchRange = std::getenv("PS2_GS_BENCH_RANGE");
        static uint64_t benchBegin = 0, benchEnd = 0;
        static bool benchParsed = false;
        static FrameStats benchStats;
        if (!benchParsed)
        {
            benchParsed = true;
            if (benchRange)
                std::sscanf(benchRange, "%llu:%llu", &benchBegin, &benchEnd);
        }
        ++guestFrame;
        g_guestFrame.store(guestFrame, std::memory_order_relaxed);
        g_viewTag[guestFrame & 63u].store(g_viewP2Now ? 1u : 0u, std::memory_order_relaxed); // SPLIT VIEW
        if (g_widescreen)
        {
            // The HUD's side groups move to the edges while the picture is 16:9 (the presenter's rule: a widescreen
            // frame within the last 10).
            static uint64_t lastWide = 0;
            static int sinceWide = 10;
            const uint64_t wide = ps2_display_aspect::wideFrameCount();
            sinceWide = wide != lastWide ? 0 : std::min(sinceWide + 1, 10);
            lastWide = wide;
            g_hudWide = sinceWide < 10;
        }
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        if (captureFrames && !g_gpuMode)
#else
        if (captureFrames)
#endif
        {
            const char *p = captureFrames;
            while (*p)
            {
                char *end = nullptr;
                const uint64_t wanted = std::strtoull(p, &end, 10);
                if (end == p) break;
                if (wanted == guestFrame)
                {
                    // An asynchronous GS must first reach this point in the stream, so captures match inline runs.
                    runtime->gs().waitForQueuedWork();
                    runtime->gs().latchHostPresentationFrame();
                    std::vector<uint8_t> pixels;
                    uint32_t width = 0, height = 0;
                    if (runtime->gs().copyLatchedHostPresentationFrame(pixels, width, height))
                    {
                        char name[64];
                        std::snprintf(name, sizeof(name), "gs_sync_%04llu_%ux%u.rgba",
                                      static_cast<unsigned long long>(guestFrame), width, height);
                        std::ofstream file(name, std::ios::binary);
                        file.write(reinterpret_cast<const char *>(pixels.data()), static_cast<std::streamsize>(pixels.size()));
                        logf("[gs-capture] frame=%llu size=%ux%u bytes=%llu", static_cast<unsigned long long>(guestFrame),
                             width, height, static_cast<unsigned long long>(pixels.size()));
                    }
                    break;
                }
                p = *end == ',' ? end + 1 : end;
                if (*p && *p != ',' && p == end) break;
            }
        }
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        if (g_gpuShadow)
        {
            const bool capture = std::find(g_gpuShadowFrames.begin(), g_gpuShadowFrames.end(), guestFrame) !=
                                 g_gpuShadowFrames.end();
            if (capture && !g_gpuMode)
            {
                // The CPU GS is inline, so its VRAM is current once pending backend work is flushed.
                runtime->gs().latchHostPresentationFrame();
            }
            if (pipelined)
            {
                const uint64_t csr = runtime->memory().gs().csr.load(std::memory_order_relaxed);
                const uint64_t frame = guestFrame;
                runtime->memory().vu1ThreadFrame(
                    [runtime, frame, capture, csr]
                    {
                        const bool ok = g_gpuShadow->onGuestFrame(frame, capture, csr);
                        if (capture)
                            logf("[gs-capture] frame=%llu gpu %s", static_cast<unsigned long long>(frame), ok ? "ok" : "failed");
                        if (!ok && !g_gpuFailed.exchange(true))
                        {
                            logf("[gpu] frame=%llu: GPU frame failed; stopping", static_cast<unsigned long long>(frame));
                            runtime->requestStop();
                        }
                    },
                    capture);
                g_lastGpuVsyncTick = runtime->eeScheduler().currentVSyncTick();
            }
            else
            {
                const bool ok = g_gpuShadow->onGuestFrame(guestFrame, capture);
                g_lastGpuVsyncTick = runtime->eeScheduler().currentVSyncTick();
                if (g_gpuMode)
                {
                    if (capture)
                        logf("[gs-capture] frame=%llu gpu %s", static_cast<unsigned long long>(guestFrame), ok ? "ok" : "failed");
                    if (!ok && !g_gpuFailed.exchange(true))
                    {
                        logf("[gpu] frame=%llu: GPU frame failed; stopping", static_cast<unsigned long long>(guestFrame));
                        runtime->requestStop();
                    }
                }
                else if (capture)
                {
                    logf("[gpu-shadow] frame=%llu packets=%llu capture=%s",
                         static_cast<unsigned long long>(guestFrame),
                         static_cast<unsigned long long>(g_gpuShadow->packetCount()), ok ? "ok" : "failed");
                    if (guestFrame == g_gpuShadowFrames.back())
                        runtime->requestStop();
                }
            }
        }
#endif
        if (!g_gameThread.load(std::memory_order_relaxed))
        {
            HANDLE self = nullptr;
            DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &self,
                            THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, 0);
            g_gameThread.store(self, std::memory_order_release);
        }
        const uint64_t enter = ps2_host_profile::nowNs();
        ps2_pacing_trace::note(ps2_pacing_trace::kSyncEntry, static_cast<uint32_t>(guestFrame));
        const uint64_t release = ps2_host_profile::g_waitReleaseNs.load(std::memory_order_acquire);
        ps2_host_profile::g_waitMarkNs.store(enter, std::memory_order_release);
        const uint64_t vu1 = ps2_host_profile::total(ps2_host_profile::kVu1);
        const uint64_t gs = ps2_host_profile::total(ps2_host_profile::kGs);
        const uint64_t stall = ps2_host_profile::total(ps2_host_profile::kGsWait);
        const uint64_t vu1Wait = ps2_host_profile::total(ps2_host_profile::kVu1Wait);
        const uint64_t vblankTick = runtime->eeScheduler().currentVSyncTick();
        g_asyncGs = runtime->gs().asyncEnabled();
        g_vu1Thread = runtime->memory().vu1ThreadActive();
        if (g_lastSyncEntryNs != 0)
        {
            const double frameMs = static_cast<double>(enter - g_lastSyncEntryNs) / 1e6;
            const double waitMs = release > g_lastSyncEntryNs ? static_cast<double>(release - g_lastSyncEntryNs) / 1e6 : 0.0;
            const double vu1Ms = static_cast<double>(vu1 - g_lastVu1Ns) / 1e6;
            const double gsMs = static_cast<double>(gs - g_lastGsNs) / 1e6;
            const double stallMs = static_cast<double>(stall - g_lastStallNs) / 1e6;
            const uint64_t vblanks = vblankTick - g_lastVBlankTick;
            const double vu1WaitMs = static_cast<double>(vu1Wait - g_lastVu1WaitNs) / 1e6;
            // (for the adaptive frame rate: a single very long frame - a load, a hitch - counts as 40 ms at most)
            g_frameBusyMs += (std::min(frameMs - waitMs, 40.0) - g_frameBusyMs) * 0.05;
            addFrame(g_perfWindow, frameMs, waitMs, vu1Ms, gsMs, stallMs, vblanks, vu1WaitMs);
            addFrame(g_perfTotal, frameMs, waitMs, vu1Ms, gsMs, stallMs, vblanks, vu1WaitMs);
            if (benchBegin != 0 && guestFrame >= benchBegin && guestFrame < benchEnd)
            {
                if (guestFrame == benchBegin)
                    benchStats.presentNs0 = ps2_host_profile::total(ps2_host_profile::kPresent);
                addFrame(benchStats, frameMs, waitMs, vu1Ms, gsMs, stallMs, vblanks, vu1WaitMs);
            }
            if (benchBegin != 0 && guestFrame == benchEnd)
            {
                logf("[gs-bench] guest frames %llu:%llu", static_cast<unsigned long long>(benchBegin),
                     static_cast<unsigned long long>(benchEnd));
                logFrameStats("guest-range", benchStats, benchStats.frameMs / 1000.0);
                // PS2_GS_BENCH_EXIT=1 ends the run once the range is measured.
                if (std::getenv("PS2_GS_BENCH_EXIT"))
                    runtime->requestStop();
            }
        }
        else
        {
            g_perfWindowStartNs = enter;
            g_perfTotal.presentNs0 = g_perfWindow.presentNs0 = ps2_host_profile::total(ps2_host_profile::kPresent);
        }
        g_lastSyncEntryNs = enter;
        g_lastVu1Ns = vu1;
        g_lastGsNs = gs;
        g_lastStallNs = stall;
        g_lastVu1WaitNs = vu1Wait;
        g_lastVBlankTick = vblankTick;
        if (enter - g_perfWindowStartNs >= 1000000000ull)
        {
            logFrameStats("1s", g_perfWindow, static_cast<double>(enter - g_perfWindowStartNs) / 1e9);
            logAudioWindow("1s", static_cast<double>(enter - g_perfWindowStartNs) / 1e9);
#ifdef MAXIMO_ENABLE_GPU_SHADOW
            if (g_gpuMode && g_gpuShadow)
            {
                // What reached the window: a black screen with the game running shows up here.
                const GpuShadow::Health h = g_gpuShadow->takeHealth();
                logf("[gpu-health] 1s published=%llu shown=%llu black=%llu noScanout=%llu failed=%llu | last frame %ux%u "
                     "brightness mean %u max %u",
                     static_cast<unsigned long long>(h.published), static_cast<unsigned long long>(h.taken),
                     static_cast<unsigned long long>(h.black), static_cast<unsigned long long>(h.noScanout),
                     static_cast<unsigned long long>(h.failed), h.width, h.height, h.brightnessMean, h.brightnessMax);
            }
#endif
            // PS2_GS_SYNC_STATS=1: per-second GS stream events that an asynchronous GS would have to order or
            // wait for, plus the game's CSR/SIGLBLID/BUSDIR reads (see GS_THREADING_PLAN.md).
            static const bool syncStats = std::getenv("PS2_GS_SYNC_STATS") != nullptr;
            if (syncStats)
            {
                const GSStreamStats &st = runtime->gs().streamStats();
                const uint64_t now[] = {st.packets.load(), st.packetBytes.load(), st.directRegWrites.load(),
                                        st.signal.load(), st.finish.load(), st.label.load(), st.trxdir[0].load(),
                                        st.trxdir[1].load(), st.trxdir[2].load(), st.localToHostReads.load(),
                                        st.presentationLatches.load(), runtime->gs().nativeImageUploadCount(),
                                        runtime->gs().nativePackedGIFPacketCount(), mmioAccesses(0x12001000u, false),
                                        mmioAccesses(0x12001080u, false),
                                        mmioAccesses(0x12001040u, false) + mmioAccesses(0x12001040u, true)};
                static uint64_t last[std::size(now)]{};
                uint64_t d[std::size(now)];
                for (size_t i = 0; i < std::size(now); ++i)
                    d[i] = now[i] - last[i], last[i] = now[i];
                logf("[gs-sync] frames=%llu packets=%llu bytes=%llu directRegs=%llu signal=%llu finish=%llu label=%llu "
                     "trx(h2l=%llu l2h=%llu l2l=%llu) l2hReads=%llu latches=%llu nativeUpload=%llu nativePacked=%llu "
                     "csrReads=%llu siglblidReads=%llu busdir=%llu",
                     static_cast<unsigned long long>(g_perfWindow.frames), d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7],
                     d[8], d[9], d[10], d[11], d[12], d[13], d[14], d[15]);
            }
            if (gs_cpu_profile::enabled())
            {
                const auto stages = gs_cpu_profile::take();
                const double frames = static_cast<double>(std::max<uint64_t>(g_perfWindow.frames, 1));
                static const char *names[] = {"triangle", "sprite", "pixel", "texture", "texture-read",
                                              "frame-read", "depth-read", "blend", "frame-write", "depth-write", "page-copy"};
                const uint64_t timerOverhead = gs_cpu_profile::timerOverhead();
                logf("[gs-profile] timer overhead %llu ns/sample", static_cast<unsigned long long>(timerOverhead));
                for (unsigned stage = 0; stage < gs_cpu_profile::Count; ++stage)
                {
                    const double scale = stage == gs_cpu_profile::Triangle || stage == gs_cpu_profile::Sprite ||
                                                 stage == gs_cpu_profile::PageCopy ? 1.0 : 64.0;
                    const uint64_t adjustedNs = stages.nanoseconds[stage] > stages.calls[stage] * timerOverhead
                                                    ? stages.nanoseconds[stage] - stages.calls[stage] * timerOverhead : 0;
                    logf("[gs-profile] %s %.3f ms/frame sampled=%llu raw=%.3f ms/frame", names[stage],
                         static_cast<double>(adjustedNs) * scale / frames / 1e6,
                         static_cast<unsigned long long>(stages.calls[stage]),
                         static_cast<double>(stages.nanoseconds[stage]) * scale / frames / 1e6);
                }
            }
            g_perfWindow = FrameStats{};
            g_perfWindow.presentNs0 = ps2_host_profile::total(ps2_host_profile::kPresent);
            g_perfWindowStartNs = enter;
        }
        traceEntry(rdram, ctx, runtime);
    }

    // 0x1AA1C0 sceGsPutDispEnv: writes PMODE, SMODE2, DISPFB2, DISPLAY2 and BGCOLOR (the display flip).
    // GPU mode runs the device vsync, and so publishes a picture, from sceGsSyncV. The FMV player (OPENING.PSS and the
    // in-game movies) never calls sceGsSyncV: it flips once per VBlank through this function, so the window kept the
    // last pre-movie frame (black after the fade) for the whole movie. A flip with no device vsync in the previous
    // field runs one, after the new display registers are written. In gameplay every flip follows a sceGsSyncV in the
    // same or the next field, so this path does not run there (counted in g_flipVsyncs).
    uint64_t g_flipVsyncs = 0, g_flipVsyncsLogged = 0, g_flipHealthNs = 0;

    void putDispEnvEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        ps2_host_profile::noteFrame(runtime->eeScheduler().currentVSyncTick()); // (fast loading: a frame was shown)
        if (g_frameRateHz != 50 || g_fieldRateHz != 50)
            frameRateOnFlip(runtime);
        traceEntry(rdram, ctx, runtime);
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        if (!g_gpuMode || !g_gpuShadow || g_gpuFailed.load())
            return;
        const uint64_t tick = runtime->eeScheduler().currentVSyncTick();
        if (tick - g_lastGpuVsyncTick < 2)
            return;
        g_lastGpuVsyncTick = tick;
        ++g_flipVsyncs;
        const uint64_t frame = g_guestFrame.load(std::memory_order_relaxed);
        const uint64_t csr = runtime->memory().gs().csr.load(std::memory_order_relaxed);
        runtime->memory().vu1ThreadFrame(
            [runtime, frame, tick, csr]
            {
                if (!g_gpuShadow->onGuestFrame(frame, false, csr) && !g_gpuFailed.exchange(true))
                {
                    logf("[gpu] display flip at VBlank %llu: GPU frame failed; stopping", static_cast<unsigned long long>(tick));
                    runtime->requestStop();
                }
            },
            false);
        if (g_gpuFailed.load())
            return;
        // sceGsSyncV, which writes the per-second lines, is not called during a movie: report from here instead.
        const uint64_t now = ps2_host_profile::nowNs();
        if (now - g_flipHealthNs >= 1000000000ull)
        {
            const GpuShadow::Health h = g_gpuShadow->takeHealth();
            logf("[gpu-health] 1s published=%llu shown=%llu black=%llu noScanout=%llu failed=%llu | last frame %ux%u "
                 "brightness mean %u max %u | flip vsyncs %llu (no sceGsSyncV)",
                 static_cast<unsigned long long>(h.published), static_cast<unsigned long long>(h.taken),
                 static_cast<unsigned long long>(h.black), static_cast<unsigned long long>(h.noScanout),
                 static_cast<unsigned long long>(h.failed), h.width, h.height, h.brightnessMean, h.brightnessMax,
                 static_cast<unsigned long long>(g_flipVsyncs - g_flipVsyncsLogged));
            g_flipVsyncsLogged = g_flipVsyncs;
            g_flipHealthNs = now;
        }
#else
        (void)rdram, (void)ctx, (void)runtime;
#endif
    }

    // 0x1AC320, called by sceGsSyncV: clear INTC_STAT.VBLANK_START, spin until it is set again, clear it, return.
    //   sw 4,INTC_STAT; loop: lw v0,INTC_STAT; andi v0,4; beqz v0,loop; sw 4,INTC_STAT; jr ra
    // The loop body only reads INTC_STAT. Default: the same 32-cycle checkpoint per spin as the generated code (the
    // IOP is stepped by the EE's checkpoint size, so this keeps the guest trajectory identical), reading INTC_STAT
    // straight from PS2Memory instead of through Load32's MMIO trace hook (those reads are left out of the hardware
    // register report).
    // MAXIMO_SPIN_SKIP=chunk: account up to kVBlankSpinChunk cycles per spin, never past the next scheduled event,
    //   EE timer interrupt or time-slice end. Much cheaper, but the coarser IOP stepping changes the trajectory
    //   (reference frame 1620 differs by 42 pixels), so it is not the default.
    // MAXIMO_SPIN_SKIP=0: the generated loop.
    constexpr uint32_t kVBlankSpinChunk = 1024u;
    bool g_vblankSpinChunked = false;
    void vblankSpinEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        constexpr uint32_t kIntcStat = 0x1000F000u, kVBlankStart = 4u;
        if (ctx->pc != GA(0x1AC330u)) // a resume after a checkpoint enters at the loop
        {
            ctx->pc = GA(0x1AC32Cu);
            SET_GPR_U32(ctx, 2, kIntcStat);
            SET_GPR_U32(ctx, 3, kVBlankStart);
            runtime->Store32(rdram, ctx, kIntcStat, kVBlankStart);
        }
        for (;;)
        {
            ctx->pc = GA(0x1AC334u);
            const uint32_t stat = runtime->memory().intcStatus(); // = read32(kIntcStat)
            SET_GPR_U32(ctx, 2, stat & kVBlankStart);
            if ((stat & kVBlankStart) != 0u)
                break;
            ctx->pc = GA(0x1AC330u);
            if (runtime->eeCheckpointDue(g_vblankSpinChunked ? runtime->eeScheduler().idleCycleBudget(kVBlankSpinChunk)
                                                             : 32u))
                return;
        }
        ctx->pc = GA(0x1AC35Cu);
        SET_GPR_U32(ctx, 2, kVBlankStart);
        SET_GPR_U32(ctx, 1, 0x10010000u);
        runtime->Store32(rdram, ctx, kIntcStat, kVBlankStart);
        ctx->pc = reg(ctx, 31);
    }

    uint64_t g_frameTimeouts = 0; // frame waits that ended through the game's 2-VBlank reset path
    std::map<uint32_t, Watch> g_watches = {
        {GA(0x001201E0u), {"FLAG screen: show option (a0=option, a1=screen object)", 2}},
        {GA(0x001201A0u), {"FLAG screen: confirm (a0=0x239880 settings, a1=table index)", 2}},
        {GA(0x0014A610u), {"FLAG screen: after confirm 0x14A610", 1}},
        {GA(0x001492D0u), {"FLAG screen: cleanup 0x1492D0", 1}},
        {GA(0x00147AD0u), {"asset/screen open 0x147AD0 (a1=name?)", 2}},
        {GA(0x0011B380u), {"frontend: 0x11B380 (a1=mode)", 2}},
        {GA(0x0011BAB0u), {"frontend: per-frame state 0x11BAB0", 1, true}},
        {kGameAllocFn, {"allocator 0x181DD0 (a0=size): heap diagnostics", 1}},
        {kGameAllocFn2, {"allocator 0x181DC0 (a0=size): heap diagnostics", 1}},
        {GA(0x001ABE80u), {"EndOfHeap wrapper 0x1ABE80", 0, true}},
        {GA(0x0019A540u), {"libc malloc 0x19A540", 1, true}},
        {GA(0x00192FB0u), {"stream file Read (a0=file, a1=dst, a2=bytes)", 3}},
        {GA(0x0014ABD0u), {"sound effect request 0x14ABD0 (a1=id)", 2}},
        {GA(0x00160960u), {"VIF1 interrupt handler 0x160960 (a0=cause)", 1}},
        {GA(0x00160DA0u), {"frame wait 0x160DA0 (a0=render context)", 1}},
        {GA(0x00160F20u), {"packet close 0x160F20 (a0=render context)", 1}},
        {GA(0x001610F0u), {"packet DMA start 0x1610F0 (a0=render context)", 1}},
        {GA(0x001611D0u), {"packet reset 0x1611D0 (a0=render context)", 1}},
        {GA(0x001A3C68u), {"DMA reset 0x1A3C68 (ra=0x160E14: frame-wait 2-VBlank timeout path)", 0}},
    };
    constexpr uint64_t kWatchLogCap = 64;

    void watchEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t pc = ctx->pc, ra = reg(ctx, 31);
        Watch &w = g_watches.at(pc);
        if (pc == kGameAllocFn || pc == kGameAllocFn2)
        {
            const uint32_t size = reg(ctx, 4);
            ++w.calls;
            traceEntry(rdram, ctx, runtime);
            if (ctx->pc == ra) // a call that yielded part way is not counted
                heapNote(runtime, size, reg(ctx, 2), ra);
            return;
        }
        if (pc == GA(0x001A3C68u) && ra == GA(0x00160E14u))
            ++g_frameTimeouts;
        // (read once: getenv on every call of these four - the render queue's functions, called all frame long -
        // was 5% of the game thread in the profile)
        static const bool vif1IrqTrace = std::getenv("PS2_VIF1_IRQ_TRACE") != nullptr;
        if ((pc == GA(0x00160F20u) || pc == GA(0x001610F0u) || pc == GA(0x001611D0u) || pc == GA(0x00160DA0u)) && vif1IrqTrace &&
            w.calls < 40)
        {
            const uint32_t rc = reg(ctx, 4) & PS2_RAM_MASK;
            uint32_t cur = 0, size = 0;
            std::memcpy(&cur, g_rdram + rc + 0x80200u, 4);
            std::memcpy(&size, g_rdram + rc + (cur % 32u) * 0x4010u + 0x4000u, 4);
            const uint8_t *slot = g_rdram + rc + (cur % 32u) * 0x4010u;
            std::fprintf(stderr, "[slots] 0x%06X ra=0x%06X cur=%u size=%u inflight=%u queued=%u idle=%u d1chcr=0x%X\n", pc,
                         ra, cur, size, slot[0x4004], slot[0x4005], g_rdram[rc + 0x80208u],
                         runtime->memory().readIORegister(0x10009000u));
        }
        if (pc == GA(0x00160960u) && w.calls < 160)
        {
            // What the handler is about to see: VIF1 mark/stat, the render context's end slot
            // (ctx+0x80200) and the "frame closed" byte behind gp-0x76AC.
            const uint32_t gp = reg(ctx, 28);
            uint32_t rc = 0, flagPtr = 0, endSlot = 0;
            std::memcpy(&rc, g_rdram + ((gp - 0x76F0u) & PS2_RAM_MASK), 4);
            std::memcpy(&flagPtr, g_rdram + ((gp - 0x76ACu) & PS2_RAM_MASK), 4);
            std::memcpy(&endSlot, g_rdram + ((rc + 0x80200u) & PS2_RAM_MASK), 4);
            const auto &v = runtime->memory().vif1_regs;
            logf("VIF1IRQ #%llu mark=%u stat=0x%X endSlot=%u closed=%u d1chcr=0x%X",
                 static_cast<unsigned long long>(w.calls + 1), v.mark, v.stat, endSlot,
                 g_rdram[flagPtr & PS2_RAM_MASK], runtime->memory().readIORegister(0x10009000u));
            if (vif1IrqTrace)
                std::fprintf(stderr, "[handler] #%llu mark=%u stat=0x%X endSlot=%u\n",
                             static_cast<unsigned long long>(w.calls + 1), v.mark, v.stat, endSlot);
        }
        if (!w.logReturn && w.calls++ < kWatchLogCap)
        {
            char args[96] = {};
            int len = 0;
            for (int i = 0; i < w.nargs; ++i)
                len += std::snprintf(args + len, sizeof(args) - len, "%s0x%X", i ? ", " : "", reg(ctx, 4 + i));
            logf("WATCH 0x%06X %s (%s) [ra=0x%06X, call #%llu]", ctx->pc, w.what, args, reg(ctx, 31),
                 static_cast<unsigned long long>(w.calls));
        }
        if (!w.logReturn)
        {
            traceEntry(rdram, ctx, runtime);
            return;
        }
        ++w.calls;
        traceEntry(rdram, ctx, runtime);
        // A call that yielded (thread switch) returns here before it finishes; only completed calls count.
        if (ctx->pc == ra && GPR_S32(ctx, 2) != w.lastReturn)
        {
            w.lastReturn = GPR_S32(ctx, 2);
            logf("WATCH 0x%06X %s returned %d [call #%llu, pad read #%llu]", pc, w.what, GPR_S32(ctx, 2),
                 static_cast<unsigned long long>(w.calls), static_cast<unsigned long long>(g_padReads));
        }
    }

    // ---- skip the intro (MAXIMO_SKIP_INTRO=1) ----------------------------------------------------------------------
    // After the language flags, func_001206B0 shows func_001212E0("PSXDATA\GAME\LOGO.PSX", 100 frames, no skip
    // buttons) and the same for GAME2\CDS.PSX (Capcom and Capcom Digital Studios, neither can be pressed past), then
    // func_00121510("PSXDATA\GAME2\COPYR.PSX"): the copyright notice and the title, which returns 10 when START is
    // pressed (on to the main menu, func_00125E20) or 9 when it times out (attract mode). Each opens and closes its own
    // screen. With the option on, the logos return at once and the title returns 10, so the game boots straight from
    // the language flags into the main menu. Only those three calls (at 0x12073C, 0x12075C, 0x120770) are changed.
    constexpr uint32_t kLogoScreenFn = GA(0x001212E0u), kTitleScreenFn = GA(0x00121510u);
    constexpr uint32_t kLogoName = GA(0x001E7CA0u), kCdsName = GA(0x001E7C60u), kCopyrName = GA(0x001E7C80u);
    uint32_t g_introSkipped = 0;

    bool introCodeMatches()
    {
        struct Word
        {
            uint32_t addr, value;
        };
        static constexpr Word kWords[] = {
            {GA(0x00120730u), GW(0x00120730u, 0x24447CA0u)}, // addiu a0, v0, 0x7CA0: LOGO.PSX
            {GA(0x0012073Cu), GW(0x0012073Cu, 0x0C0484B8u)}, // jal 0x1212E0
            {GA(0x00120748u), GW(0x00120748u, 0x24447C60u)}, // addiu a0, v0, 0x7C60: CDS.PSX
            {GA(0x0012075Cu), GW(0x0012075Cu, 0x0C0484B8u)}, // jal 0x1212E0
            {GA(0x00120768u), GW(0x00120768u, 0x24447C80u)}, // addiu a0, v0, 0x7C80: COPYR.PSX
            {GA(0x00120770u), GW(0x00120770u, 0x0C048544u)}, // jal 0x121510
        };
        for (const Word &w : kWords)
            if (rd32(w.addr) != w.value)
            {
                logf("skip intro: code at 0x%06X is %08X, not %08X; the intro plays", w.addr, rd32(w.addr), w.value);
                return false;
            }
        return true;
    }

    void skipLogoEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t name = reg(ctx, 4);
        if (ctx->pc == kLogoScreenFn && (name == kLogoName || name == kCdsName))
        {
            logf("skip intro: %s skipped", name == kLogoName ? "Capcom logo" : "Capcom Digital Studios logo");
            ++g_introSkipped;
            ret(ctx, 9); // as if it had run its time out
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    void skipTitleEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == kTitleScreenFn && reg(ctx, 4) == kCopyrName)
        {
            logf("skip intro: copyright and title skipped, on to the main menu");
            ++g_introSkipped;
            ret(ctx, 10); // as if START had been pressed
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // ---- debug menu and free camera -------------------------------------------------------------------------------
    // Hold L1 + R1 and press SELECT to open or close the menu (D-pad up/down to pick, left/right or CROSS to change,
    // CIRCLE to close). While the menu is open, or the free camera is on, the game is sent a released controller,
    // so Maximo stands still. The menu is drawn onto each finished frame in GPU presentation (the launcher's mode).
    //
    // Free camera: every view the game draws comes from func_0013C6C0(camera, view matrix, eye, target), a look-at
    // that turns eye and target into the camera's angles (+0x1C0..) and view matrix; func_00143210 and the camera
    // update copy that matrix to the render matrices at 0x2625F0/0x262630, and culling (func_16AE00) follows it. The
    // hook below swaps in the free camera's eye and target (the game's camera keeps running underneath, so turning
    // it off snaps straight back). Left stick flies, right stick looks, L2/R2 go down/up, hold CROSS for speed.
    constexpr uint32_t kLookAtFn = GA(0x0013C6C0u);
    // Spawn tool: the game's own runtime spawn, as its scripts use it (item drops at 0x117E60, script opcode at
    // 0x10E6C0): func_001028D0(vm, type id) looks the type up in the class table (func_00104E20(vm + 0xB8, type),
    // 0 when this world has not loaded it), allocates and registers the object and returns its handle (0 when it is
    // refused, e.g. by the object limit); func_001032A0(vm, handle) gives its entity record, whose first word is the
    // actor, and the actor's position is the vector at +0xA0 (the drop code writes it there). vm is the level's
    // script VM, gp-0x77B0 (0x238FC0), 0 outside a level. Type ids are the level files' (randomizer spawn_config).
    constexpr uint32_t kSpawnFn = GA(0x001028D0u), kHandleToEntityFn = GA(0x001032A0u), kClassLookupFn = GA(0x00104E20u);
    constexpr uint32_t kLevelVmPtr = GA(0x00238FC0u);
    // The game's own spawn (func_001028D0) makes enemies whose script ends at once; spawnOne below follows the level
    // loader instead.
    constexpr uint32_t kObjAllocFn = GA(0x00181DC0u), kObjCtorFn = GA(0x00101570u), kObjIdFn = GA(0x001056F0u),
                       kObjInitFn = GA(0x00101690u), kObjRegisterFn = GA(0x00104780u), kObjStartFn = GA(0x00101820u),
                       kEntityCreateFn = GA(0x00105630u), kObjActivateFn = GA(0x00103C30u),
                       kObjDestroyFn = GA(0x00101610u), kPropAllocFn = GA(0x00181DD0u), kPropApplyFn = GA(0x00103CB0u);
    // A property as a level record carries it (and as func_00103EA0 keeps it): 32 bytes, +0 type, +4 index of the
    // variable it sets, +8 value. func_00103CB0(vm, variables, class+0x10, property, t0) writes it into the variable
    // (t0 is the loader's string context, only read for string properties).
    struct RecordProp
    {
        uint32_t type, index, value;
    };
    bool callGuestArgs(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t fn,
                       const std::array<uint32_t, 8> &args, uint32_t &v0);
    // Home: the world(s) whose files hold the enemy (empty: nearly everywhere). A type listed in a level outside its
    // home is one the cross-world tool (tools/cross_world_enemies.py, the launcher's Cross-world enemies option) ported
    // into that world's .BEF/.PRS/.PRT; the spawn list is simply every type the current world's class table has.
    struct EnemyType
    {
        uint32_t id;
        const char *name;
        const char *home;
    };
    constexpr EnemyType kEnemyTypes[] = {
        {0x47, "BASIC SKELETON", ""},           {0x2F, "AXE SKELETON", ""},
        {0x39, "SWORD SKELETON", ""},           {0x54, "GUARD SKELETON", ""},
        {0x49, "BOMB SKELETON", "CASTLE/ICE/UNDER"}, {0x3A, "PIRATE SKELETON", "ICE"},
        {0x48, "BASIC ZOMBIE", ""},             {0x6A, "TORSO ZOMBIE", ""},
        {0x10, "FROZEN ZOMBIE", "ICE"},         {0x19, "SWAMP ZOMBIE", "SWAMP"},
        {0x0D, "ZOMBIE CROCODILE", "SWAMP"},    {0x6B, "GHOST", ""},
        {0x37, "RAVEN", ""},                    {0x1D, "SNOWMAN", "ICE"},
        {0x13, "GOAT DEVIL", "UNDER"},          {0x27, "HAMMER DEVIL", "UNDER"},
        {0x5D, "DOOMED SOUL", "UNDER"},         {0x08, "PLANT MONSTER", "CASTLE/SWAMP"},
        {0x53, "DARK KNIGHT", "CASTLE"},        {0x58, "CRAZED PRISONER", "CASTLE"},
        {0x16, "AXE GUARD", "CASTLE"},
        // Bosses: on the disc their classes are only in their arena's archive (GRAVE_B, SWAMP_B, ICE_B, UNDER_B,
        // CASTLE_K, CASTLE_Q). In an arena an extra one copies the arena boss's variables and properties; with the
        // launcher's Cross-world enemies option the tool adds all six to every world (their level-exit and boss-camera
        // calls removed), and in a level they start from their class defaults.
        {0x29, "GRAVEDIGGER", "BOSS"},          {0x2E, "BOKOR LABAS", "BOSS"},
        {0x82, "CAPTAIN CADAVER", "BOSS"},      {0x8E, "LORD GLUTTERSCUM", "BOSS"},
        {0x9C, "KING", "BOSS"},                 {0x9D, "QUEEN", "BOSS"},
        // NPCs, only with the cross-world tool (types it gives them; nothing on the disc uses 0xF7-0xFE). Grim has no
        // class of his own (the continue screen draws him itself): one class per continue-screen animation. The
        // sorceresses are each arena's class 0x99 with its rescue count (GameVarAdd) removed.
        {0xF7, "GRIM 1", "NPC"},                {0xF8, "GRIM 2", "NPC"},
        {0xF9, "GRIM 3", "NPC"},                {0xFA, "GRIM 4", "NPC"},
        {0xFB, "SORCERESS 1", "GRAVE"},         {0xFC, "SORCERESS 2", "SWAMP"},
        {0xFD, "SORCERESS 3", "ICE"},           {0xFE, "SORCERESS 4", "UNDER"},
        // Hidden classes: in the game's archives with a model and animations, but placed by no level. Most are
        // spawned only by a boss or another enemy's script (found by decoding every script's Spawn calls); the
        // UNUSED ones are spawned by nothing at all. Names say where they come from: what each one is was not
        // checked in the game. A boss's are in its arena, and in every world with the cross-world tool (it brings
        // each boss's whole spawn set along). Experimental: one made without its parent may behave oddly.
        {0x3D, "GRAVEDIGGER SUMMON A (2 MODELS)", "HIDDEN"}, {0x43, "GRAVEDIGGER SUMMON B", "HIDDEN"},
        {0x22, "GRAVEDIGGER SUMMON C", "HIDDEN"},  {0xDE, "GRAVEDIGGER SUMMON D", "HIDDEN"},
        {0x5A, "GRAVEDIGGER ARENA UNUSED", "HIDDEN"},
        {0x73, "BOKOR SUMMON A", "HIDDEN"},        {0x75, "BOKOR SUMMON B", "HIDDEN"},
        {0x34, "BOKOR SUMMON C", "HIDDEN"},        {0x64, "BOKOR SUMMON D", "HIDDEN"},
        {0x74, "CADAVER/GLUTTERSCUM SUMMON", "HIDDEN"}, {0x88, "CADAVER SUMMON B", "HIDDEN"},
        {0x89, "CADAVER SUMMON C", "HIDDEN"},      {0xAB, "CADAVER SUMMON D", "HIDDEN"},
        {0x7A, "GLUTTERSCUM SUMMON B", "HIDDEN"},
        {0x83, "KING ARENA UNUSED A (BIG)", "HIDDEN"}, {0x7B, "KING ARENA UNUSED B", "HIDDEN"},
        {0x80, "KING ARENA UNUSED C", "HIDDEN"},   {0x86, "KING ARENA SUMMON D", "HIDDEN"},
        {0x84, "KING ARENA SUMMON E", "HIDDEN"},   {0xE9, "QUEEN SUMMON", "HIDDEN"},
        {0xDD, "UNUSED CHARACTER (ALL WORLDS)", "HIDDEN"}, {0x5E, "BOMB SKELETON SPAWN", "HIDDEN"},
        {0x26, "COLLECTOR SPAWN", "HIDDEN"},       {0x9E, "BONE TOWER SPAWN", "HIDDEN"},
        {0x9B, "ICE SHIP UNUSED", "HIDDEN"},       {0xF3, "UNUSED OBJECT (ALL WORLDS)", "HIDDEN"},
    };
    // Pickups for the spawn tool, each with the properties a level record of it has (read from the disc's .PSX files):
    // type 7 = number, 1 = flag, 11 = float bits. They are made from these, not copied from a live one.
    struct PickupType
    {
        uint32_t id;
        const char *name;
    };
    constexpr PickupType kItemTypes[] = {
        {0x21, "GOLD COIN"},     {0x46, "BAG OF GOLD"},  {0x52, "GEM"},           {0x51, "FULL HEALTH"},
        {0x4E, "EXTRA LIFE"},    {0x45, "ARMOR POWER-UP"}, {0x65, "SKELETON KEY"}, {0x2B, "GOLD KEY"},
        {0x25, "WOODEN CHEST"},  {0x5B, "LOCKED CHEST"},
    };
    // Ability pickups (class 0x20, "Ability"): property 0 is the power it gives (the game's ability numbers, as the
    // randomizer's elf_patch.py names them), property 1 a flag that is 0 in every placed one.
    constexpr uint32_t kAbilityType = 0x20u;
    constexpr PickupType kSkills[] = {
        {0, "INCREASE SWORD POWER"}, {1, "SWORD 720 SPIN"},     {2, "DOUBLE SLASH"},       {3, "MIGHTY BLOW"},
        {4, "MAGIC BOLT"},           {5, "MASQUERADE"},         {6, "RETURN SHIELD"},      {7, "WIDE SHOCKWAVE"},
        {8, "DAMAGE SHOCKWAVE"},     {9, "FIND TREASURE"},      {10, "SMART BOMB"},        {11, "HOVER SHIELD"},
        {12, "SHIELD"},              {13, "SHIELD STAMINA +5"}, {14, "INCREASE ARMOR"},    {19, "WIND SHIELD"},
        {20, "MAGNETIC SHIELD"},     {21, "LIGHTNING SHIELD"},  {22, "FULL HEALTH (HEART)"}, {23, "INCREASE THROW"},
        {24, "SWORD STAMINA +5"},
        // (Ability ids 25-30, two no-ops and the four kisses, crash the game as pickups: the pickup's own tables
        // stop at 24. Not offered.)
    };
    constexpr PickupType kSwordPowers[] = {
        {15, "FIRE SWORD"}, {16, "ICE SWORD"}, {17, "SUN SWORD"}, {18, "ARMAGEDDON SWORD"},
    };
    // The properties for an item of kItemTypes, as its placed records have them.
    std::vector<RecordProp> itemProps(uint32_t type)
    {
        switch (type)
        {
        case 0x21: case 0x46: case 0x52:
            return {{1u, 0u, 0u}};
        case 0x51: case 0x4E:
            return {{1u, 3u, 0u}, {1u, 4u, 0u}};
        case 0x45:
            return {{1u, 3u, 0u}, {1u, 4u, 0u}, {1u, 5u, 0u}};
        case 0x65:
            return {{1u, 4u, 0u}, {1u, 5u, 0u}, {1u, 6u, 0u}};
        case 0x2B:
            return {{1u, 4u, 0u}};
        case 0x25: // C_SUB1 "battlehall1": not hidden, 5 gold and a prize
        {
            std::vector<RecordProp> p = {{1u, 3u, 0u}, {1u, 4u, 0u}, {11u, 5u, 0x40A00000u}, {7u, 6u, 0u}, {1u, 15u, 0u}};
            for (uint32_t i = 16; i <= 32; ++i)
                p.push_back({7u, i, i == 16 ? 5u : i == 17 ? 1u : 0u});
            p.push_back({11u, 33u, 0x41A00000u});
            p.push_back({1u, 34u, 0u});
            return p;
        }
        case 0x5B: // C_SUB1 "garden1": opened with a skeleton key, 5 gold and an ability
        {
            std::vector<RecordProp> p = {{1u, 3u, 0u}, {1u, 4u, 0u}, {11u, 5u, 0x40A00000u}, {7u, 6u, 1u}};
            for (uint32_t i = 7; i <= 22; ++i)
                p.push_back({7u, i, i == 7 ? 5u : i == 21 ? 1u : 0u});
            return p;
        }
        }
        return {};
    }
    std::vector<RecordProp> abilityProps(uint32_t ability) { return {{7u, 0u, ability}, {1u, 1u, 0u}}; }

    // Structures: the 24 types of the randomizer's spawn_config.STRUCTURE_TYPES (the cross-world tool ports them into
    // every world). `copy` types (trigger volumes in their records) take their variables from a live instance of the
    // same class instead of from itemProps-style defaults.
    struct StructureType
    {
        uint32_t id;
        const char *name;
        bool copy;
    };
    constexpr StructureType kStructures[] = {
        {0x01, "BONE TOWER", false},       {0x02, "BONUS GRAVE", false},      {0x0A, "POPUP HEADSTONE", false},
        {0x17, "SMASHABLE SNOW PIRATE", false}, {0x18, "ICE TORCH", false},  {0x1B, "POPUP ICE", false},
        {0x30, "GRAVE SMASHABLE GLASS", false}, {0x4A, "GLOBE", false},      {0x50, "STANDING COFFIN", false},
        {0x5F, "MONSTER GENERATOR", false}, {0x6C, "COIN CONTAINER", false},  {0x7C, "ICE PLANT", false},
        {0xA0, "COFFIN LID", false},      {0xAA, "SMASHABLE DOOR", false},   {0xAD, "SPIRIT TORCH", false},
        {0xAE, "UNDERWORLD LANTERN", false}, {0xB0, "SMASHABLE TORCH", false}, {0xB1, "SMASHABLE ICE WALL", false},
        {0xB5, "SWAMP COFFIN LID", false}, {0xBB, "THORN VINE", false},      {0xBC, "SMASHABLE COFFIN", false},
        {0xC8, "SPIRIT STATUE", false},   {0xC9, "BREAKABLE ROCKWALL", false}, {0xD3, "BREAKABLE TIKI TORCH", false},
    };
    // SPAWN ANY CLASS: every class id the current world has loaded, with its name from the level records (the 119
    // classes the levels place); the others are named from kEnemyTypes when listed there, else UNPLACED.
    struct ClassName
    {
        uint32_t id;
        const char *name;
    };
    constexpr ClassName kClassNames[] = {
        {0x1, "BONE_TOWER"}, {0x2, "BONUS_GRAVE"}, {0x4, "HUT_GATE"}, {0x7, "HOLY_GROUND"}, {0x8, "PLANT_MONSTER"},
        {0xa, "POPUP_HEADSTONE"}, {0xc, "TELEPORT_POOL"}, {0xd, "ZOMBIE_CROCODILE"}, {0x10, "FROZEN_ZOMBIE"},
        {0x11, "WHALE"}, {0x12, "SEA_WEED"}, {0x13, "GOAT_DEVIL"}, {0x15, "IRON_MAIDEN"}, {0x16, "AXE_GUARD"},
        {0x17, "SMASHABLE_SNOW_PIRATE"}, {0x18, "ICE_TORCH"}, {0x19, "SWAMP_ZOMBIE"}, {0x1b, "POPUP_ICE"},
        {0x1d, "SNOWMAN"}, {0x1f, "TRIGGER"}, {0x20, "ABILITY"}, {0x21, "GOLDCOIN"}, {0x23, "CURSED_HAND"},
        {0x24, "COLLECTOR"}, {0x25, "WOODEN_CHEST"}, {0x27, "HAMMER_DEVIL"}, {0x29, "GRAVEDIGGER"}, {0x2b, "GOLD_KEY"},
        {0x2d, "STEAM_VENT"}, {0x2e, "BOKORLABAS"}, {0x2f, "AXE_SKELETON"}, {0x30, "GRAVE_SMASHABLE_GLASS"},
        {0x32, "ENTITYZONE"}, {0x33, "BOSS_CAMERA"}, {0x37, "RAVEN"}, {0x39, "SWORDSKELETON"},
        {0x3a, "PIRATESKELETON"}, {0x3b, "SMOKE"}, {0x3f, "UNDERWORLD_GATE"}, {0x40, "FLAME_JET_DRAGON_HEAD"},
        {0x41, "LIFT_GATE"}, {0x45, "ARMOR_POWER_UP"}, {0x46, "BAG_OF_GOLD"}, {0x47, "BASIC_SKELETON"},
        {0x48, "BASIC_ZOMBIE"}, {0x49, "BOMB_SKELETON"}, {0x4a, "GLOBE"}, {0x4d, "END_OF_SUBLEVEL_COLUMN"},
        {0x4e, "EXTRA_LIFE"}, {0x50, "STANDING_COFFIN"}, {0x51, "FULL_HEALTH"}, {0x52, "GEM"}, {0x53, "DARK_KNIGHT"},
        {0x54, "GUARD_SKELETON"}, {0x56, "FIRE_FLIES"}, {0x58, "CRAZED_PRISONER"}, {0x59, "LEVEL_COLUMN"},
        {0x5b, "LOCKED_CHEST"}, {0x5d, "DOOMED_SOUL"}, {0x5f, "MONSTER_GENERATOR"}, {0x62, "HUBCLEAR"},
        {0x65, "SKELETON_KEY"}, {0x66, "SMASHABLE_WALL"}, {0x6a, "TORSO_ZOMBIE"}, {0x6b, "GHOST"},
        {0x6c, "COINCONTAINER"}, {0x6d, "ENTITY_LIGHT"}, {0x6f, "SPARKS"}, {0x71, "LOCK_BOX"},
        {0x72, "MAUSOLEUM_GATE"}, {0x77, "ICE_BOSS_GRATE"}, {0x78, "GLOW"}, {0x7c, "ICE_PLANT"},
        {0x81, "KING_ELECTRODE"}, {0x82, "PIRATE_CAPTAIN_BOSS"}, {0x8a, "QUEENWAYPOINT"}, {0x8b, "ICE_GOLD_GATE"},
        {0x8d, "BEAR_TRAP"}, {0x8e, "LORD_OF_DARKNESS"}, {0x9a, "ICE_FLOW"}, {0x9c, "KING"}, {0x9d, "QUEEN"},
        {0x9f, "FLAME_JET"}, {0xa0, "COFFIN_LID"}, {0xa1, "ICE_GATE_SMALL"}, {0xa3, "CAMTRIGGER"},
        {0xaa, "SMASHABLE_DOOR"}, {0xad, "SPIRIT_TORCH"}, {0xae, "UNDERWORLD_LANTERN"}, {0xaf, "TRIGGERONHIT"},
        {0xb0, "SMASHABLE_TORCH"}, {0xb1, "SMASHABLE_ICE_WALL"}, {0xb5, "SWAMP_COFFIN_LID"}, {0xbb, "THORN_VINE"},
        {0xbc, "SMASHABLE_COFFIN"}, {0xc0, "SWAMP_LOCKED_GATE"}, {0xc4, "SINKING_SKULL"}, {0xc5, "SINKING_ICE"},
        {0xc8, "SPIRIT_STATUE"}, {0xc9, "BREAKABLE_ROCKWALL"}, {0xcb, "DROP_LOCKED_GATE"},
        {0xcc, "FIRING_CANNON_HAZARD"}, {0xce, "FOG_VOLUME"}, {0xcf, "SNOW"}, {0xd0, "RAIN"}, {0xd1, "SWINGING_SPIKE"},
        {0xd3, "BREAKABLE_TIKKITORCH"}, {0xd7, "RAIN_TRIGGER"}, {0xd8, "SNOW_TRIGGER"}, {0xdb, "SWAMP_CRYPT_GATE"},
        {0xdc, "AMBIENT_MACHINE_NOISE"}, {0xe0, "FIRE"}, {0xe2, "GRAVEINTRO_LOCKED_SWINGING_GATE"},
        {0xe7, "AMBIENT_WATERFALL_NOISE"}, {0xea, "DUST_TRIGGER"}, {0xed, "KING_ECTOSPHERE"}, {0xf1, "CAMFLAGS"},
        {0xf2, "AMBIENT_SOUND"}, {0xf6, "BGANIM_SOUND"},
    };
    const char *classNameFor(uint32_t id)
    {
        for (const ClassName &c : kClassNames)
            if (c.id == id)
                return c.name;
        for (const EnemyType &e : kEnemyTypes)
            if (e.id == id)
                return e.name;
        return "UNPLACED";
    }

    // ENEMY CLASS: the enemies whose level record picks a class / level (the randomizer's ENEMY_TIER_TYPES: property
    // 0, a number) or a variant (Ghost: property 0, a flag: poltergeist or castle blue ghost). The menu's class 1-3
    // is the n-th value here (the last one when there are fewer). A class whose models a world does not load (e.g.
    // Basic Zombie 3 outside Swamp / Underworld) may come out wrong there.
    struct EnemyClassInfo
    {
        uint32_t id;
        uint32_t propType;
        std::array<uint32_t, 3> values;
        int count;
    };
    constexpr EnemyClassInfo kEnemyClasses[] = {
        {0x47, 7, {0, 1, 2}, 3}, // Basic Skeleton
        {0x39, 7, {0, 1, 2}, 3}, // Sword Skeleton
        {0x48, 7, {0, 1, 2}, 3}, // Basic Zombie (3: rotting, splits into a Torso Zombie)
        {0x54, 7, {0, 1, 2}, 3}, // Guard Skeleton
        {0x49, 7, {0, 1, 2}, 3}, // Bomb Skeleton
        {0x08, 7, {0, 1, 0}, 2}, // Plant Monster
        {0x3A, 7, {0, 2, 0}, 2}, // Pirate Skeleton
        {0x6B, 1, {0, 1, 0}, 2}, // Ghost: poltergeist / castle blue ghost
    };
    constexpr const char *kEnemyClassNames[] = {"AS PLACED", "CLASS 1", "CLASS 2", "CLASS 3"};

    // The Collector (type 0x24, the shop): property 0 is its stock (0-10, all used in the levels; 5 is S_SUB2's
    // mystery shop), property 1 a hub phase (0 for a shop).
    constexpr uint32_t kCollectorType = 0x24u;
    constexpr int kCollectorStocks = 11;

    // Properties of a placed record of each structure type (read from the disc's .PSX files; most have none).
    std::vector<RecordProp> structureProps(uint32_t type)
    {
        switch (type)
        {
        case 0x0A: return {{11u, 0u, 1097859072u}};
        case 0x1B: return {{11u, 0u, 1103626240u}};
        case 0x6C: return {{7u, 0u, 2u}};
        case 0x7C: case 0xBB: return {{1u, 0u, 0u}};
        case 0x01: // G_BONUS: wakes when Maximo is within 20 of it
            return {{12u, 0u, 0x41A00000u}, {11u, 4u, 0x42200000u}, {14u, 5u, 2000u}, {14u, 6u, 500u}, {7u, 7u, 3u},
                    {4u, 8u, 0x3ECCCCCDu}};
        case 0x5F: // C_HUB
            return {{11u, 0u, 0x42200000u}, {14u, 1u, 3000u}, {11u, 2u, 0x42340000u}, {7u, 3u, 0u}, {1u, 4u, 0u},
                    {1u, 5u, 1u}, {1u, 6u, 0u}, {3u, 7u, 2u}, {7u, 8u, 1u}, {4u, 15u, 0x41200000u},
                    {4u, 16u, 0x3FC00000u}, {4u, 17u, 0x40200000u}, {4u, 18u, 0x3F000000u}};
        case 0x50: case 0xBC:
        {
            std::vector<RecordProp> p;
            for (uint32_t i = 0; i < 16; ++i)
                p.push_back({7u, i, i == 0 ? 5u : 0u});
            return p;
        }
        }
        return {};
    }

    // Level select. The game's level list is the LevelTable (kLevelTableAddr, 31 rows: the .PSX it loads, the sky,
    // the world folder, ...); the main loop (func_00120900) loads row gp-0x77C4 with func_00121050, which runs the
    // level until the exit code at gp-0x77BC is set and returns it. Code 4 is a sub-level change: func_00120EB0(delta)
    // (what entering or leaving a sub-level calls) sets the code, the target row (gp-0x77B4 = current + delta) and the
    // loop mode 6; the main loop then stores the level (func_00122490), takes the target from func_00120F70 and loads
    // that row like any level start. The level select calls the same function. gp = 0x240770.
    constexpr uint32_t kCurrentLevelAddr = GA(0x00238FACu); // gp-0x77C4: LevelTable row being played
    constexpr uint32_t kLevelExitCodeAddr = GA(0x00238FB4u); // gp-0x77BC: 0 while the level runs
    constexpr uint32_t kChangeLevelFn = GA(0x00120EB0u);
    constexpr const char *kLevelNames[31] = {
        "GRAVEYARD INTRO",       "GRAVEYARD HUB",    "GRAVEYARD SUB 1",  "GRAVEYARD SUB 2",
        "GRAVEYARD INTRO B",     "GRAVEDIGGER (BOSS)", "SWAMP INTRO",    "SWAMP HUB",
        "SWAMP SUB 1",           "SWAMP SUB 2",      "SWAMP SUB 3",      "BOKOR LABAS (BOSS)",
        "ICE SHIP INTRO",        "ICE SHIP HUB",     "ICE SHIP SUB 1",   "ICE SHIP SUB 2",
        "ICE SHIP SUB 3",        "CAPTAIN CADAVER (BOSS)", "UNDERWORLD INTRO", "UNDERWORLD HUB",
        "UNDERWORLD SUB 1",      "UNDERWORLD SUB 2", "UNDERWORLD SUB 3", "LORD GLUTTERSCUM (BOSS)",
        "CASTLE INTRO",          "CASTLE HUB",       "CASTLE SUB 1",     "CASTLE SUB 2",
        "KING (BOSS)",           "QUEEN (BOSS)",     "GRAVEYARD BONUS",
    };
    constexpr const char *kLevelFiles[31] = {
        "G_INTRO", "G_HUB",  "G_SUB1", "G_SUB2", "G_INTROB", "G_BOSS",  "S_INTRO", "S_HUB",
        "S_SUB1",  "S_SUB2", "S_SUB3", "S_BOSS", "I_INTRO",  "I_HUB",   "I_SUB1",  "I_SUB2",
        "I_SUB3",  "I_BOSS", "U_INTRO", "U_HUB", "U_SUB1",   "U_SUB2",  "U_SUB3",  "U_BOSS",
        "C_INTRO", "C_HUB",  "C_SUB1", "C_SUB2", "C_KING",   "C_QUEEN", "G_BONUS",
    };
    constexpr int kSpawnCounts[] = {1, 3, 5, 10};
    bool callGuest(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t fn, uint32_t a0, uint32_t a1,
                   uint32_t a2, uint32_t &v0);
    // libpad button bits (active-high after inverting data[2..3]).
    constexpr uint16_t kBtnSelect = 1u << 0, kBtnR3 = 1u << 2, kBtnUp = 1u << 4, kBtnRight = 1u << 5,
                       kBtnDown = 1u << 6, kBtnLeft = 1u << 7, kBtnL2 = 1u << 8, kBtnR2 = 1u << 9, kBtnL1 = 1u << 10,
                       kBtnR1 = 1u << 11, kBtnTriangle = 1u << 12, kBtnCircle = 1u << 13, kBtnCross = 1u << 14;

    // 5x7 glyphs for the debug overlay: 7 rows of 5 columns each, '#' lit. Upper case; lower case is drawn as upper.
    struct DebugGlyph
    {
        char c;
        const char *rows[7];
    };
    constexpr DebugGlyph kDebugFont[] = {
        {' ', {".....", ".....", ".....", ".....", ".....", ".....", "....."}},
        {'A', {".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"}},
        {'B', {"####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."}},
        {'C', {".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."}},
        {'D', {"####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."}},
        {'E', {"#####", "#....", "#....", "####.", "#....", "#....", "#####"}},
        {'F', {"#####", "#....", "#....", "####.", "#....", "#....", "#...."}},
        {'G', {".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"}},
        {'H', {"#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"}},
        {'I', {".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."}},
        {'J', {"..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."}},
        {'K', {"#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"}},
        {'L', {"#....", "#....", "#....", "#....", "#....", "#....", "#####"}},
        {'M', {"#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"}},
        {'N', {"#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"}},
        {'O', {".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."}},
        {'P', {"####.", "#...#", "#...#", "####.", "#....", "#....", "#...."}},
        {'Q', {".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"}},
        {'R', {"####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"}},
        {'S', {".####", "#....", "#....", ".###.", "....#", "....#", "####."}},
        {'T', {"#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."}},
        {'U', {"#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."}},
        {'V', {"#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."}},
        {'W', {"#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."}},
        {'X', {"#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"}},
        {'Y', {"#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."}},
        {'Z', {"#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"}},
        {'0', {".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."}},
        {'1', {"..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."}},
        {'2', {".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"}},
        {'3', {"#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."}},
        {'4', {"...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."}},
        {'5', {"#####", "#....", "####.", "....#", "....#", "#...#", ".###."}},
        {'6', {"..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."}},
        {'7', {"#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."}},
        {'8', {".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."}},
        {'9', {".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."}},
        {'.', {".....", ".....", ".....", ".....", ".....", ".##..", ".##.."}},
        {',', {".....", ".....", ".....", ".....", ".##..", "..#..", ".#..."}},
        {':', {".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."}},
        {'-', {".....", ".....", ".....", "#####", ".....", ".....", "....."}},
        {'+', {".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."}},
        {'/', {".....", "....#", "...#.", "..#..", ".#...", "#....", "....."}},
        {'(', {"...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."}},
        {')', {".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."}},
        {'[', {".###.", ".#...", ".#...", ".#...", ".#...", ".#...", ".###."}},
        {']', {".###.", "...#.", "...#.", "...#.", "...#.", "...#.", ".###."}},
        {'>', {".#...", "..#..", "...#.", "....#", "...#.", "..#..", ".#..."}},
        {'<', {"...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#."}},
        {'=', {".....", ".....", "#####", ".....", "#####", ".....", "....."}},
        {'!', {"..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."}},
        {'?', {".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."}},
        {'%', {"##...", "##..#", "...#.", "..#..", ".#...", "#..##", "...##"}},
        {'\'', {"..#..", "..#..", ".#...", ".....", ".....", ".....", "....."}},
        {'_', {".....", ".....", ".....", ".....", ".....", ".....", "#####"}},
    };

    const DebugGlyph *debugGlyph(char c)
    {
        if (c >= 'a' && c <= 'z')
            c = static_cast<char>(c - 'a' + 'A');
        for (const DebugGlyph &g : kDebugFont)
            if (g.c == c)
                return &g;
        return &kDebugFont[0];
    }

    struct DebugState
    {
        bool menuOpen = false;
        int selected = 0;
        bool freeCam = false, freeCamReady = false;
        int speed = 1; // 0 slow, 1 normal, 2 fast
        bool showPosition = false, invertX = false, invertY = false;
        uint16_t prevButtons = 0;
        uint64_t lastMoveFrame = 0;
        // Free camera: eye, unit view direction, world up (+Y or -Y, taken from the game's camera when it starts).
        float eye[3] = {}, dir[3] = {0, 0, 1};
        float up = 1.0f;
        // The game's own eye and target at its last look-at, to start from and to show.
        float gameEye[3] = {}, gameTarget[3] = {};
        bool haveGameCamera = false;
        uint8_t lx = 0x80, ly = 0x80, rx = 0x80, ry = 0x80;
        uint16_t held = 0;
        // Spawn tool: the enemy types this world has loaded (indices into kEnemyTypes), refreshed when the menu opens.
        std::vector<int> spawnable;
        int spawnPick = 0, spawnCount = 0;
        bool inLevel = false;
        // Pickups: the kItemTypes this world has (indices), whether it has the Ability class, and the picks
        // (0 = ALL, else 1 + index into the list).
        std::vector<int> items;
        bool haveAbility = false;
        int itemPick = 0, skillPick = 0, swordPick = 0;
        std::vector<int> structs; // kStructures indices loaded in this world
        int structPick = 0;
        bool haveCollector = false;
        std::vector<uint32_t> anyClasses; // every class id loaded here (SPAWN ANY CLASS)
        int anyPick = 0;
        int collectorPick = 0; // 0 = all stocks, else 1 + stock
        bool pauseInFreeCam = false, noHud = false;
        bool firstPerson = false; // FIRST PERSON CAMERA: the view from Maximo's eyes
        bool moveMaximo = false; // MOVE MAXIMO IN FREE CAMERA: the d-pad flies the camera, the game gets the rest
        bool infinite = false;  // INFINITE LIVES / KOINS / KEYS
        bool oneHitKill = false; // ONE HIT KILL
        int gameSpeed = 2;      // index into kGameSpeeds (1x)
        int givePick = 0;       // GIVE ABILITY: index into kGiveAbilities
        int enemyClass = 0;     // ENEMY CLASS: 0 as placed (copied), else class 1-3
        bool spawnAtCamera = false; // SPAWN AT: false = in front of Maximo, true = in front of the camera
        float zoom = 1.0f;          // FREE CAMERA ZOOM (1 = the game's view)
        int levelPick = 0; // LevelTable row for GO TO LEVEL
        std::string spawnMessage;
        // Spawned objects followed for a few seconds in the log (where the actor is, and the object's and actor's
        // fields), to see what a spawn that never shows up does differently from one that does.
        struct Tracked
        {
            uint32_t obj, handle, tmpl, type;
            uint64_t born;
        };
        std::vector<Tracked> tracked;
        uint64_t padFrames = 0;
    };
    DebugState g_dbg; // game thread only
    int g_resourceProbeLeft = 0; // model requests still to log after a spawn (resourceCopyProbeEntry)

    // God mode and no clip. Found in the disc's main program: the main loop (func_00122xxx) calls the player's update
    // func_0013D020 once a frame with a0 = gPlayer (0x239890, a struct, not a pointer); damage goes through
    // GameVarSub (func_0012A3F0: a0 = the player, a1 = the variable, a2 = the amount) and Player::OnHit treats a
    // non-zero result as "still alive". Variable 2 is health, 3 the armor shown, 4 the lives, so God Mode answers
    // "alive" to a subtraction from those three without making it. The player's position is looked for (first
    // 0x1A0, else a scan) as the three floats next to the game camera's target, and No Clip rewrites it after every
    // update so walls, floors and enemies cannot push it anywhere.
    constexpr uint32_t kPlayerUpdateFn = GA(0x0013D020u), kGameVarSubFn = GA(0x0012A3F0u), kPlayerAddr = GA(0x00239890u);
    bool g_godMode = false, g_noClip = false;
    // POST EFFECTS in the debug menu: bloom and a grade drawn by the GPU renderer (gpu_shadow.cpp applyPost).
    int g_postLevel = [] {
        const char *v = std::getenv("MAXIMO_POST");
        return v ? std::clamp(std::atoi(v), 0, 2) : 0;
    }();
    void setPostLevel(int level)
    {
        g_postLevel = level;
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        GpuShadow::setPostLevel(level);
#endif
    }
    int g_lightLevel = [] {
        const char *v = std::getenv("MAXIMO_SCENE_LIGHT");
        return v ? std::clamp(std::atoi(v), 0, 4) : 0;
    }();
    void setLightLevel(int level)
    {
        g_lightLevel = level;
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        GpuShadow::setLightLevel(level);
#endif
    }
    bool gpuRendererOn()
    {
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        return g_gpuMode;
#else
        return false;
#endif
    }
    void normalize3(float *v);
    float stickAxis(uint8_t v);
    // The player as the game's own code addresses him (gPlayer): the Ability pickup calls
    // SetAbility__6PlayerFib(0x263540, its ability, 1) (func_117CA0), and SetAbility adds shield points with
    // GameVarAdd(this, 10, 5). His 17 game variables are words at +0x48 (the GameVarTbl in the ELF at 0xE0190 gives
    // each one's start and cap): #4 lives (cap 99), #5 koins (9999), #7 keys (9).
    constexpr uint32_t kGamePlayerAddr = GA(0x00263540u), kSetAbilityFn = GA(0x0012CE10u);
    constexpr uint32_t kVarLives = 4, kVarKoins = 5, kVarKeys = 7, kVarSouls = 6; // (6: souls, the HUD's "spirit", cap 999)
    uint32_t gameVarAddr(uint32_t var) { return kGamePlayerAddr + 0x48u + var * 4u; }
    constexpr float kGameSpeeds[] = {0.25f, 0.5f, 1.0f, 2.0f, 3.0f};
    constexpr const char *kGameSpeedNames[] = {"0.25X", "0.5X", "1X (NORMAL)", "2X", "3X"};
    // GIVE ABILITY: SetAbility straight on the player, as the sorceresses' scripts and the pickups do. 25 and 26 do
    // nothing (measured by the randomizer), 27-30 are the four kisses (their pickups crash: the pickup's own tables
    // stop at 24). 31 ("ration") is left out: SetAbility(31) reads objects that are not always set up.
    constexpr const char *kGiveAbilities[] = {
        "INCREASE SWORD POWER", "SWORD 720 SPIN", "DOUBLE SLASH", "MIGHTY BLOW", "MAGIC BOLT", "MASQUERADE",
        "RETURN SHIELD", "WIDE SHOCKWAVE", "DAMAGE SHOCKWAVE", "FIND TREASURE", "SMART BOMB", "HOVER SHIELD",
        "SHIELD", "SHIELD STAMINA +5", "INCREASE ARMOR", "FIRE SWORD", "ICE SWORD", "SUN SWORD", "ARMAGEDDON SWORD",
        "WIND SHIELD", "MAGNETIC SHIELD", "LIGHTNING SHIELD", "FULL HEALTH", "INCREASE THROW", "SWORD STAMINA +5",
        "UNUSED 25", "UNUSED 26", "PURPLE KISS", "GREEN KISS", "BLUE KISS", "RED KISS",
    };

    constexpr uint32_t kAppModeAddr = GA(0x002395F0u), kAppSubmodeAddr = GA(0x002395CCu); // gp-29056, gp-29092
    // The boss freeze hunt: Maximo's state reads 0 (walking) while he is stuck, so the lock is elsewhere. Two snapshots
    // are compared: one taken when an enemy is spawned from the menu (before the fight), one when RELEASE MAXIMO is
    // pressed (while stuck). Every word that changed in the player structures is logged.
    struct FreezeRegion
    {
        uint32_t addr, bytes;
        const char *name;
    };
    constexpr FreezeRegion kFreezeRegions[] = {
        {GA(0x00239890u), 0x1000u, "gPlayer(update)"}, {GA(0x00263540u), 0x1000u, "gPlayer(abilities)"},
        {GA(0x0025B9E0u), 0x7C00u, "interface"},       {GA(0x00238F80u), 0x800u, "gp globals"},
    };
    std::vector<std::vector<uint32_t>> g_freezeBaseline;
    void takeFreezeBaseline()
    {
        g_freezeBaseline.clear();
        for (const FreezeRegion &r : kFreezeRegions)
        {
            std::vector<uint32_t> w(r.bytes / 4);
            std::memcpy(w.data(), mem(r.addr), r.bytes);
            g_freezeBaseline.push_back(std::move(w));
        }
    }
    void logFreezeDiff()
    {
        if (g_freezeBaseline.size() != std::size(kFreezeRegions))
        {
            logf("debug: release diff: no snapshot yet (spawn something from the menu before the fight)");
            return;
        }
        int shown = 0;
        for (size_t k = 0; k < std::size(kFreezeRegions); ++k)
        {
            const FreezeRegion &r = kFreezeRegions[k];
            std::string line;
            int inRegion = 0;
            for (uint32_t i = 0; i < r.bytes / 4 && shown < 600 && inRegion < 200; ++i)
            {
                const uint32_t now = rd32(r.addr + i * 4u), was = g_freezeBaseline[k][i];
                if (now == was)
                    continue;
                // Floats that merely moved (positions, timers) are left out: flags and counters are what can hold him.
                float fn, fw;
                std::memcpy(&fn, &now, 4);
                std::memcpy(&fw, &was, 4);
                const bool floaty = std::isfinite(fn) && std::isfinite(fw) && std::fabs(fn) > 1e-3f &&
                                    std::fabs(fw) > 1e-3f && std::fabs(fn) < 1e7f && std::fabs(fw) < 1e7f &&
                                    now > 0x00FFFFFFu && was > 0x00FFFFFFu;
                if (floaty)
                    continue;
                char t[48];
                std::snprintf(t, sizeof t, " +0x%X:%X->%X", i * 4u, was, now);
                line += t;
                ++shown;
                ++inRegion;
            }
            logf("debug: release diff %s 0x%07X:%s", r.name, r.addr, line.empty() ? " (no change)" : line.c_str());
        }
    }

    struct NoClipState
    {
        bool have = false;
        // Every copy of the player's position found when no clip starts: gPlayer fields and fields of structures
        // gPlayer points to, whose three floats sit within a few units of gPlayer+0x1A0. Each keeps its offset from it.
        struct Copy
        {
            uint32_t addr;
            float delta[3];
        };
        std::vector<Copy> copies;
        float pos[3] = {};     // where no clip keeps gPlayer+0x1A0
    } g_nc;

    // ONE HIT KILL: Player::SetAction gives Maximo's sword its damage with DEDynamicObject::SetDamage(object, min, max,
    // ...) (func_14DFE0; the randomizer's patch_damage_dealt signature finds the call at 0x12E040, returning to
    // 0x12E048), and ObjectCollision hands that damage to the enemy it hits. Only that call is changed: enemies'
    // own attacks use SetDamage from elsewhere.
    constexpr uint32_t kSetDamageFn = GA(0x0014DFE0u), kSwordDamageRa = GA(0x0012E048u);
    bool g_oneHitKill = false; // mirrored from the debug menu
    // Survival: the spawned enemies get the same health a level gives them (their scripts: a Sword Skeleton 2-4 by
    // class, a Snowman 4, a Swamp Zombie 3), but a sword hit reached their hit handler as Maximo's full damage - the
    // attack's base times 4 to 7 (Player::SetAction, 0x12DF90), 8 a swing (a Swamp Zombie went from 3 to -5 in one
    // hit), where in a level the same enemies take one point a hit (a Sword Skeleton needs its 2-4 hits). During
    // survival waves the sword does an eighth (at least 1): one point a plain swing, more for the stronger attacks.
    // Maximo's other damage-giving calls to SetDamage are scaled the same way in survival: the thrown shield
    // (func_00132D90: 4, or 1 without the return-shield power), the special action's 3 (SetAction, 0x12E084) and
    // func_001345F0's (its power's value, doubled with the power-up): the shield killed in one hit.
    constexpr uint32_t kShieldDamageRa = GA(0x001330A4u), kActionDamageRa = GA(0x0012E08Cu), kBoltDamageRa = GA(0x00134754u);
    bool g_survivalSoftSword = false;
    bool g_survivalBossWave = false; // a survival boss wave (the boss alone): the sword does 3x, so it has a third of the life
    bool g_survivalLevelBoss = false; // ... in survival in a normal level (no arena traps): the boss takes more damage
    // The sword damage of Maximo's latest attack and the pad frame it was set: ENEMY HEALTH BARS learns which script
    // variable of an enemy is its health from the one that drops by exactly this much.
    uint32_t g_lastSwordDamage = 0;
    uint64_t g_lastSwordFrame = 0;
    uint64_t g_lastOtherAttackFrame = 0; // the shield throw and Maximo's other attacks (Captain Cadaver's hits)
    void setDamageEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        // Only a1 is the damage: a2 is the kind of attack (0, 1 or 2 in the log: 8-1 a swing, 8-2 / 14-2 the
        // stronger ones), which some enemies check - scaling it too turned the jump attack's 2 into a 1, and pirate
        // skeletons, which only that attack kills, could not die.
        // Survival's softer damage (not in a boss wave: the normal boss fight's damage). The sword's damage is the
        // action's base times the enchantment (SetAction, func_0012DD70: with the power-up x4 to x7 by GameVar 1,
        // the enchantment; without it the base plus a bonus from the enchantment's table). Divided by 8 for every
        // attack, an enchanted swing came out at the same 1 as a plain one - the enchantment did nothing. With an
        // enchantment it is divided by 4 instead (rounded), so it hits about twice as hard as without.
        const uint32_t ra = reg(ctx, 31), before = reg(ctx, 5) & 0xFFu;
        const uint32_t enchant = rd32(GA(0x00263540u) + 0x4Cu); // GameVar 1
        const bool playerAttack =
            ra == kSwordDamageRa || ra == kShieldDamageRa || ra == kActionDamageRa || ra == kBoltDamageRa;
        if (g_oneHitKill && ra == kSwordDamageRa)
            SET_GPR_U32(ctx, 5, 255u); // the damage is a byte (masked with 0xFF just before)
        else if (g_survivalSoftSword && !g_survivalBossWave && playerAttack)
        {
            const uint32_t div = ra == kSwordDamageRa ? (enchant ? 4u : 8u) : (enchant ? 2u : 4u);
            SET_GPR_U32(ctx, 5, std::max<uint32_t>(1u, (before + div / 2u) / div));
        }
        else if (g_survivalSoftSword && g_survivalBossWave && g_survivalLevelBoss && playerAttack)
        {
            // A boss away from its arena has none of the arena's traps and openings to finish him with: every hit
            // that does land counts 3x (2x with an enchantment, which already hits harder).
            const uint32_t mul = enchant ? 2u : 3u;
            SET_GPR_U32(ctx, 5, std::min<uint32_t>(255u, before * mul));
        }
        if (g_survivalSoftSword && playerAttack)
        {
            static int logged = 0;
            if (logged < 200)
            {
                ++logged;
                logf("debug: survival damage: %s %u -> %u (kind %u, enchantment %u%s, boss bar %u)",
                     ra == kSwordDamageRa ? "sword" : ra == kShieldDamageRa ? "shield" : ra == kActionDamageRa ? "special"
                                                                                                             : "bolt",
                     before, reg(ctx, 5) & 0xFFu, reg(ctx, 6), enchant,
                     g_survivalBossWave ? (g_survivalLevelBoss ? ", boss wave in a level" : ", boss wave") : "",
                     rd32(GA(0x00263540u) + 0x48u + 4u * 15u));
            }
        }
        if (reg(ctx, 31) == kSwordDamageRa)
        {
            g_lastSwordDamage = reg(ctx, 5) & 0xFFu;
            g_lastSwordFrame = g_dbg.padFrames;
        }
        if (reg(ctx, 31) == kShieldDamageRa || reg(ctx, 31) == kActionDamageRa || reg(ctx, 31) == kBoltDamageRa)
            g_lastOtherAttackFrame = g_dbg.padFrames;
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // PLAYER 2 diagnostics: whose turn it is when Maximo loses health or armor (0 player 1's frame, 1 player 2's
    // turn, 2 an enemy script run with player 2 swapped in) and the object whose script is running, if any.
    int g_turnContext = 0;
    uint32_t g_scriptObject = 0;
    uint32_t g_collisionA = 0, g_collisionB = 0; // the bodies of the collision being answered, if any
    void godVarSubEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t var = reg(ctx, 5);
        if ((var == 2u || var == 3u) && reg(ctx, 6) != 0u)
        {
            static int logged = 0;
            if (logged < 200)
            {
                ++logged;
                const uint32_t obj = g_scriptObject;
                const uint32_t tmpl = obj && (obj & PS2_RAM_MASK) == obj ? rd32(obj + 0xB4u) : 0u;
                logf("debug: hit: GameVarSub(0x%07X, var %u, %u) from 0x%06X, collision 0x%07X / 0x%07X, turn %s, script "
                     "object 0x%07X type 0x%02X, player body 0x%07X", reg(ctx, 4), var, reg(ctx, 6), reg(ctx, 31),
                     g_collisionA, g_collisionB,
                     g_turnContext == 1   ? "player 2"
                     : g_turnContext == 2 ? "enemy aimed at player 2"
                     : g_turnContext == 3 ? "collision with player 2"
                                          : "player 1 / world",
                     obj, tmpl && (tmpl & PS2_RAM_MASK) == tmpl ? rd32(tmpl) : 0u, rd32(GA(0x00263540u)));
            }
        }
        if (g_godMode && (var == 2u || var == 3u || var == 4u))
        {
            ret(ctx, 1); // taken, and still alive
            return;
        }
        // Survival: a boss arena doubles what hits take from Maximo (health 2 / armor 3); halved back to a level's.
        if (g_survivalSoftSword && (var == 2u || var == 3u) && reg(ctx, 6) > 1u)
            SET_GPR_U32(ctx, 6, reg(ctx, 6) / 2u);
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // gPlayer+0x1A0 is only the point the camera follows (moving it alone moved only the camera), and no pointer in
    // gPlayer has an actor-style position at +0xA0. So every copy of the position is collected: any three floats in
    // gPlayer (first 0x1000 bytes) or in the first 0x400 bytes of a structure it points to that lie within 3 units of
    // gPlayer+0x1A0; all of them are rewritten after each update, keeping their offsets.
    void noClipStep(uint8_t *rdram, uint32_t player, bool move = true)
    {
        uint8_t *base = rdram + (player & PS2_RAM_MASK);
        float cur[3] = {};
        std::memcpy(cur, base + 0x1A0, 12);
        if (!std::isfinite(cur[0]) || !std::isfinite(cur[1]) || !std::isfinite(cur[2]) ||
            (cur[0] == 0.0f && cur[1] == 0.0f && cur[2] == 0.0f))
            return; // not in a level yet
        if (!g_nc.have)
        {
            g_nc.copies.clear();
            std::string seen;
            auto consider = [&](uint32_t addr, const char *how, uint32_t a, uint32_t b) {
                if (addr + 12 > 0x02000000u || g_nc.copies.size() >= 24)
                    return;
                float v[3];
                std::memcpy(v, rdram + addr, 12);
                if (!std::isfinite(v[0]) || !std::isfinite(v[1]) || !std::isfinite(v[2]))
                    return;
                const float d = std::fabs(v[0] - cur[0]) + std::fabs(v[1] - cur[1]) + std::fabs(v[2] - cur[2]);
                if (d > 3.0f || std::fabs(v[0]) < 1.0f)
                    return;
                for (const auto &c : g_nc.copies)
                    if (addr < c.addr + 12 && c.addr < addr + 12)
                        return; // overlaps one already taken
                NoClipState::Copy c{addr, {v[0] - cur[0], v[1] - cur[1], v[2] - cur[2]}};
                g_nc.copies.push_back(c);
                char t[96];
                std::snprintf(t, sizeof t, " %s+0x%X+0x%X(%.1f %.1f %.1f)", how, a, b, v[0], v[1], v[2]);
                seen += t;
            };
            for (uint32_t off = 0; off < 0x1000u; off += 4)
                consider((player & PS2_RAM_MASK) + off, "gPlayer", off, 0);
            // Player::Update's other position vectors (0x1D0: last frame, 0x1F0: the base some states add an offset
            // to), kept with whatever offset they have.
            for (uint32_t off : std::initializer_list<uint32_t>{}) // (0x1D0/0x1F0/0x200 are the camera's: left alone)
            {
                float v[3];
                std::memcpy(v, base + off, 12);
                bool taken = false;
                for (const auto &c : g_nc.copies)
                    taken = taken || c.addr == (player & PS2_RAM_MASK) + off;
                if (!taken && std::isfinite(v[0]) && std::isfinite(v[1]) && std::isfinite(v[2]) &&
                    std::fabs(v[0] - cur[0]) + std::fabs(v[2] - cur[2]) < 50.0f)
                {
                    g_nc.copies.push_back({(player & PS2_RAM_MASK) + off, {v[0] - cur[0], v[1] - cur[1], v[2] - cur[2]}});
                    char t[96];
                    std::snprintf(t, sizeof t, " gPlayer+0x%X(%.1f %.1f %.1f)", off, v[0], v[1], v[2]);
                    seen += t;
                }
            }
            for (uint32_t off = 0; off < 0x1000u; off += 4)
            {
                uint32_t p = 0;
                std::memcpy(&p, base + off, 4);
                if (p < 0x00100000u || p >= 0x02000000u - 0x400u || (p & 3u) ||
                    (p >= (player & PS2_RAM_MASK) && p < (player & PS2_RAM_MASK) + 0x1000u))
                    continue;
                for (uint32_t f = 0; f < 0x400u; f += 4)
                    consider(p + f, "ptr", off, f);
            }
            // Exact copies anywhere in RAM (the model's matrix, the collision body, ...): the player kept moving with
            // every gPlayer copy rewritten, so the position the game moves lives somewhere else.
            size_t ramCopies = 0;
            // Data only (the program image ends at 0x238F80); NaNs compare false, so finiteness is checked first.
            for (uint32_t addr = GA(0x00238F80u); addr + 12 <= 0x02000000u && g_nc.copies.size() < 48; addr += 4)
            {
                if (addr >= (player & PS2_RAM_MASK) && addr < (player & PS2_RAM_MASK) + 0x1000u)
                    continue;
                float v[3];
                std::memcpy(v, rdram + addr, 12);
                if (!std::isfinite(v[0]) || !std::isfinite(v[1]) || !std::isfinite(v[2]) ||std::fabs(v[0] - cur[0]) > 0.05f || std::fabs(v[2] - cur[2]) > 0.05f ||
                    std::fabs(v[1] - cur[1]) > 2.0f)
                    continue;
                bool overlap = false;
                for (const auto &c : g_nc.copies)
                    overlap = overlap || (addr < c.addr + 12 && c.addr < addr + 12);
                if (overlap)
                    continue;
                g_nc.copies.push_back({addr, {v[0] - cur[0], v[1] - cur[1], v[2] - cur[2]}});
                char t[64];
                std::snprintf(t, sizeof t, " ram 0x%07X(%.1f %.1f %.1f)", addr, v[0], v[1], v[2]);
                seen += t;
                ++ramCopies;
            }
            (void)ramCopies;
            // gPlayer+0x1A0 turned out to be the point the camera follows (above and ahead of Maximo); his feet are
            // gPlayer+0x1F0, which the update rebuilds every frame from wherever the game keeps him. Exact copies of
            // it in data RAM (his model's matrices, the collision body) are taken too.
            {
                float feet[3];
                std::memcpy(feet, base + 0x1F0, 12);
                for (uint32_t addr = GA(0x00238F80u); addr + 12 <= 0x02000000u && g_nc.copies.size() < 72; addr += 4)
                {
                    if (addr >= (player & PS2_RAM_MASK) && addr < (player & PS2_RAM_MASK) + 0x1000u)
                        continue;
                    float v[3];
                    std::memcpy(v, rdram + addr, 12);
                    if (!std::isfinite(v[0]) || !std::isfinite(v[1]) || !std::isfinite(v[2]) ||
                        std::fabs(v[0] - feet[0]) > 0.02f || std::fabs(v[1] - feet[1]) > 0.02f ||
                        std::fabs(v[2] - feet[2]) > 0.02f || std::fabs(feet[0]) < 1.0f)
                        continue;
                    bool overlap = false;
                    for (const auto &c : g_nc.copies)
                        overlap = overlap || (addr < c.addr + 12 && c.addr < addr + 12);
                    if (overlap)
                        continue;
                    g_nc.copies.push_back({addr, {v[0] - cur[0], v[1] - cur[1], v[2] - cur[2]}});
                    char t[64];
                    std::snprintf(t, sizeof t, " feet 0x%07X", addr);
                    seen += t;
                }
            }
            for (int i = 0; i < 3; ++i)
                g_nc.pos[i] = cur[i];
            g_nc.have = true;
            logf("debug: no clip: gPlayer+0x1A0 = (%.1f, %.1f, %.1f); %zu position copies:%s", cur[0], cur[1], cur[2],
                 g_nc.copies.size(), seen.empty() ? " none" : seen.c_str());
        }
        // A new level: start over there (the game moving the player is what no clip overrides, so distance is not
        // a sign of a respawn).
        static uint32_t level = ~0u;
        if (rd32(kCurrentLevelAddr) != level)
        {
            level = rd32(kCurrentLevelAddr);
            g_nc.have = false;
            return;
        }
        if (move && !g_dbg.menuOpen && g_dbg.haveGameCamera)
        {
            const float up[3] = {0.0f, g_dbg.up, 0.0f};
            float h[3] = {g_dbg.gameTarget[0] - g_dbg.gameEye[0], 0.0f, g_dbg.gameTarget[2] - g_dbg.gameEye[2]};
            normalize3(h);
            float right[3] = {h[1] * up[2] - h[2] * up[1], h[2] * up[0] - h[0] * up[2], h[0] * up[1] - h[1] * up[0]};
            normalize3(right);
            static const float kSpeeds[] = {0.15f, 0.4f, 1.0f};
            const float speed = kSpeeds[g_dbg.speed];
            const float fwd = -stickAxis(g_dbg.ly) * speed;
            const float side = stickAxis(g_dbg.lx) * speed;
            const float lift = ((g_dbg.held & kBtnR2) ? speed : 0.0f) - ((g_dbg.held & kBtnL2) ? speed : 0.0f);
            for (int i = 0; i < 3; ++i)
                g_nc.pos[i] += h[i] * fwd + right[i] * side + up[i] * lift;
        }
        for (const auto &c : g_nc.copies)
        {
            float v[3];
            for (int i = 0; i < 3; ++i)
                v[i] = g_nc.pos[i] + c.delta[i];
            std::memcpy(rdram + c.addr, v, 12);
        }
        // Player::Update integrates gPlayer+0x1A0 = gPlayer+0x220 (last position) + gPlayer+0x210 (velocity) * dt:
        // no velocity, so neither walking nor gravity carries Maximo away from where no clip holds him.
        std::memset(base + 0x210, 0, 12);
        std::memset(base + 0x860, 0, 12); // the update's own velocity estimate (0x1A0 - 0x1D0)
    }

    // func_0015FED0 runs late in the main loop's frame, after the collision and object passes that follow the player
    // update; no clip writes the position again there so the next frame starts from it everywhere.
    constexpr uint32_t kFrameLateFn = GA(0x0015FED0u);
    void survivalBoundaryLate(uint8_t *rdram);
    void frameLateEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        if (g_noClip && g_nc.have)
            noClipStep(rdram, kPlayerAddr, false);
        else
            survivalBoundaryLate(rdram);
    }

    // PAUSE GAME IN FREE CAMERA: func_1480C0 works out the frame's time step (0x25B9F0, in 1/60 s) that everything
    // moves, animates and counts down by; held near 0 the world stands still while it is still drawn.
    constexpr uint32_t kFrameStepFn = GA(0x001480C0u), kFrameStepAddr = GA(0x0025B9F0u);
    void frameStepEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // PAUSE GAME IN FREE CAMERA (the time step alone did not stop it): the level loop's updates are skipped while
    // the world is still drawn: the player (playerUpdateEntry), every level object and enemy (func_102B70(vm), its
    // script VM) and the particle systems (func_182810 / func_1823E0 on 0x2633D4).
    constexpr uint32_t kVmUpdateFn = GA(0x00102B70u), kParticlesFn = GA(0x00182810u), kParticlesExtraFn = GA(0x001823E0u);
    void pausableEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if ((g_dbg.freeCam && g_dbg.pauseInFreeCam) || g_viewP2Now)
        {
            ret(ctx, 0);
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // HIDE HUD: func_14AEE0 draws the HUD models with func_16A6B0(renderer + 0x5F50) (gauge, counters, item ring);
    // that one call (it returns to 0x14AF54) is skipped.
    constexpr uint32_t kHudDrawFn = GA(0x0016A6B0u), kHudDrawRa = GA(0x0014AF54u);
    void hudDrawEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_dbg.noHud && reg(ctx, 31) == kHudDrawRa)
        {
            ret(ctx, 0);
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    void cameraUpdatePre(R5900Context *ctx); // (the orbit camera's start of the update, below)
    void playerUpdateEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        cameraUpdatePre(ctx);
        const uint32_t self = reg(ctx, 4);
        // No clip writes the held position before the update too, so the update starts from it.
        if (g_noClip && g_nc.have && self == kPlayerAddr)
            noClipStep(rdram, self, false);
        // No clip lets the update run (it also moves the camera, which stayed behind when it was skipped) with the
        // pad neutral (debugPadRead) and the held position written before it; noClipStep after it moves the held
        // position by the stick and writes it over whatever gravity and collisions did.
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        if (g_noClip && self == kPlayerAddr)
        {
            // Diagnostic once a second: where the update put the player before no clip overrides it.
            static uint32_t n = 0;
            if (g_nc.have && (++n % 60) == 0)
            {
                auto v = [&](uint32_t off, int i) { float f; std::memcpy(&f, rdram + kPlayerAddr + off + i * 4, 4); return f; };
                logf("debug: no clip: after update state %u: 0x1A0 (%.1f %.1f %.1f) 0x1D0 (%.1f %.1f %.1f) 0x1F0 (%.1f %.1f "
                     "%.1f) 0x220 (%.1f %.1f %.1f); held (%.1f %.1f %.1f)",
                     rd32(kPlayerAddr + 0x248u), v(0x1A0, 0), v(0x1A0, 1), v(0x1A0, 2), v(0x1D0, 0), v(0x1D0, 1),
                     v(0x1D0, 2), v(0x1F0, 0), v(0x1F0, 1), v(0x1F0, 2), v(0x220, 0), v(0x220, 1), v(0x220, 2),
                     g_nc.pos[0], g_nc.pos[1], g_nc.pos[2]);
            }
            noClipStep(rdram, self);
        }
    }

    // What the window thread draws, copied out under the lock.
    struct SurvivalFoe
    {
        uint32_t obj, handle, tmpl;
        uint64_t born = 0;
        uint32_t type = 0;
        float last[3] = {0.0f, 0.0f, 0.0f}; // where it was at the last check
        uint64_t lastMove = 0;              // when it last moved (an enemy that never wakes never moves)
        float at[3] = {0.0f, 0.0f, 0.0f};   // where it is (kept up every few frames: its drop falls there)
        bool haveAt = false;
    };
    struct Survival
    {
        bool active = false;
        int wave = 0, best = 0, kills = 0;
        uint32_t vm = 0, lives = 0;
        uint64_t nextWaveAt = 0, waveStarted = 0;
        std::vector<SurvivalFoe> foes;
        std::string result; // shown after a run
        bool bestLoaded = false;
        // 1 = on the way to the arena, 2 = arrived (clearing the arena's boss), 3 = waves
        int phase = 0;
        int arenaRow = -1;
        uint32_t arenaBoss = 0, leftVm = 0;
        uint64_t phaseFrame = 0;
        // Where Maximo has stood in the arena: spawn points that are sure to be on its floor.
        std::vector<std::array<float, 3>> trail;
        std::vector<std::array<float, 3>> walked; // only where Maximo himself stood (no spots made around the boss)
        // Enemy types whose spawns died by themselves this run (left out from then on), and how many to replace.
        std::vector<uint32_t> badTypes;
        int replace = 0;
        std::array<float, 3> arenaMid{};
        bool haveMid = false;
        // Where the run started, and the trip back there after Maximo dies (the arena restarts with its boss).
        int startRow = -1, returnRow = -1;
        uint64_t returnAt = 0, returnDeadline = 0;
        uint64_t lastProgress = 0; // the wave's start or its last kill (the stuck-enemy watchdog)
        // Maximo's own inventory while survival gives him the default one (saved at the start, put back at the end).
        bool invSaved = false;
        std::array<uint8_t, 68> invVars{};  // GameVars 0-16 (Player+0x48)
        std::array<uint8_t, 62> invAbil{};  // the abilities (Player+0x424, two bytes each)
        uint32_t invFlags = 0;
        uint8_t invB6 = 0;
        // The chest before a boss wave: 1 = a sword enchantment, 2 = a mimic, 3 = nothing inside.
        uint32_t chestObj = 0, chestHandle = 0;
        int chestKind = 0;
        float chestAt[3] = {0.0f, 0.0f, 0.0f};
        struct Chest
        {
            float at[3];
            int kind; // 1 a sword enchantment, 2 a mimic, 3 nothing, 4 armor
        };
        std::vector<Chest> chests; // this reward's chests (their prizes are kept by the loot tick)
        std::unordered_set<uint32_t> baseline; // the arena's own objects when the waves start (never taken away)
        int bossCamStill = 0;                  // frames the stick was held with Maximo not moving (boss camera)
        int bossCamStage = 0;                  // 1: Maximo and the boss were unfrozen once
        bool bossCamOff = false;               // the boss camera held Maximo still: off for this boss
        float bossCamLast[3] = {0.0f, 0.0f, 0.0f};
        bool bossCam = false; // the arena's boss camera is on (a boss wave's boss is alive)
        // Drops and the chest are made one per frame from here, and tried again while a class lookup fails (two in the
        // same frame: the second lookup did not answer, and that drop - or the chest - was lost).
        struct Pending
        {
            uint32_t type;
            std::vector<RecordProp> props;
            float pos[3];
            std::string what;
            int tries = 0;
            bool column = false; // a ring piece (its handle goes to the ring's list)
            int slot = -1;       // its place in the ring
            bool haveYaw = false;
            float yaw = 0.0f;
        };
        std::vector<Pending> pending;
        int chestWanted = 0; // chests still to make for this reward
        // SURVIVAL IN THIS LEVEL: the fight is where Maximo stood when it started, inside a ring of the purple
        // end-of-sub-level columns (radius kLevelArenaRadius); he is held inside it.
        bool levelMode = false;
        float ringCenter[3] = {0.0f, 0.0f, 0.0f};
        std::vector<uint32_t> columns; // the ring pieces' handles (a column's level exit is not passed on)
        bool ringMade = false;
        // The ring's places: every piece made once; one that vanished is made again (survivalRingTick).
        struct RingSlot
        {
            float pos[3];
            float yaw;
            uint32_t handle = 0, obj = 0;
            bool queued = false;
        };
        std::vector<RingSlot> ring;
        uint32_t ringType = 0;
        uint32_t ringCheckAt = 0; // the next check for lost ring pieces (frame)
        int music = -1; // the boss music playing for this run (-1: the level's own)
        // SURVIVAL TIMER: the run's time in play, from the game's own frame counter (held while paused or loading).
        double seconds = 0.0;
        uint32_t clockLast = 0;
        bool clockValid = false;
    } g_survival;
    constexpr float kSurvivalHudSize = 0.75f; // survival's lines on screen, against the debug menu's letters

    // SURVIVAL TIMER: the game's frame counter (0x25B9E0) moves once per frame of play and stands still while the
    // game is paused; it advances kNativeHz times a second (at 60 Hz in PAL its increments are paced to 50).
    void survivalClockTick()
    {
        if (!g_survival.active)
            return;
        const uint32_t v = rd32(g_pacedCounters[0].addr);
        if (g_survival.clockValid && v > g_survival.clockLast && v - g_survival.clockLast < 8u)
            g_survival.seconds += static_cast<double>(v - g_survival.clockLast) / kNativeHz;
        g_survival.clockLast = v; // (a reset at a level start is just taken as the new start)
        g_survival.clockValid = true;
    }

    bool g_survivalHere = false; // the menu's choice: false a boss arena, true the level Maximo is in
    // ---- ENEMY HEALTH BARS (debug menu, on by default) ----------------------------------------------------------
    // A bar over each ordinary enemy's head. An enemy's health is one of its script variables (obj+0x84), and which
    // one depends on its script, so it is learned: when a sword hit takes exactly the sword's damage
    // (g_lastSwordDamage) off a variable that had been still, that variable is the type's health - kept in
    // enemy_health_vars.txt, so a type shows bars from then on. The bar's full length is the most that variable
    // was seen holding. Its place on screen: the head (actor position + up) through the game's own view matrix
    // (0x2625F0, row vectors) and projection (0x2625A0, built by func_15B100 from the caller at 0x147860: GS
    // coordinates centred on 2048, x to +-320 at the picture's edges, y to +-134.4 = 0.56 * 0.75 * 320).
    // Up and down on screen are checked every frame against Maximo (his head must be above his feet).
    bool g_healthBars = false; // off by default for now
    // Files kept across runs go next to maximo_boot.exe: the game runs in each session's own log folder.
    std::string persistentPath(const char *name)
    {
        char exe[MAX_PATH] = {};
        if (GetModuleFileNameA(nullptr, exe, MAX_PATH) == 0)
            return name;
        return (fs::path(exe).parent_path() / name).string();
    }
    constexpr const char *kHealthVarsFile = "enemy_health_vars.txt";
    constexpr uint32_t kViewMatrixAddr = GA(0x002625F0u), kProjMatrixAddr = GA(0x002625A0u);
    // Maximo is about 5 units tall (the snapshot: his feet at 0.80 of the picture, 4 units up at the middle).
    constexpr float kHeadHeight = 6.5f, kBarRange = 70.0f;
    float headHeight(uint32_t type)
    {
        switch (type)
        {
        case 0x53: return 9.0f;  // dark knight
        case 0x27: return 9.0f;  // hammer devil
        case 0x1D: return 8.5f;  // snowman
        case 0x13: return 8.0f;  // goat devil
        case 0x0D: return 5.0f;  // zombie crocodile
        case 0x6A: return 4.0f;  // torso zombie
        default: return kHeadHeight;
        }
    }
    struct HpWatch
    {
        uint32_t obj, handle, tmpl, type, words;
        uint32_t prev[48];
        uint32_t maxSeen[48];
        uint64_t still[48]; // pad frame each word last changed
        uint64_t seen;
        float start[3];  // where it was first seen
        bool awake;      // has moved from there (asleep enemies wait in place, often still under the ground)
    };
    std::vector<HpWatch> g_hpWatch;
    std::map<uint32_t, int> g_healthVar; // type -> word index of its health
    struct HpBar
    {
        float x, y, frac;
    };
    std::mutex g_hpBarsMutex;
    std::vector<HpBar> g_hpBars;

    void loadHealthVars()
    {
        // Learned in play (sword damage 1-2, each from a 1-3 health enemy): basic skeleton, axe skeleton, sword
        // skeleton, basic zombie.
        g_healthVar = {{0x47u, 26}, {0x2Fu, 12}, {0x39u, 9}, {0x48u, 14}};
        std::ifstream f(persistentPath(kHealthVarsFile));
        unsigned type = 0;
        int index = 0;
        while (f >> std::hex >> type >> std::dec >> index)
            if (index >= 0 && index < 48)
                g_healthVar[type] = index;
    }

    void saveHealthVars()
    {
        std::ofstream f(persistentPath(kHealthVarsFile));
        for (const auto &[type, index] : g_healthVar)
            f << std::hex << type << ' ' << std::dec << index << '\n';
    }

    bool barEnemyType(uint32_t type)
    {
        for (const EnemyType &t : kEnemyTypes)
            if (t.id == type)
                return std::strcmp(t.home, "BOSS") != 0 && std::strcmp(t.home, "NPC") != 0; // bosses have their own bar
        return false;
    }

    // World point -> picture position (0..1 across and down). False when behind the camera or far off the picture.
    bool projectToPicture(const float w[3], float &nx, float &ny)
    {
        float V[16], P[16];
        std::memcpy(V, mem(kViewMatrixAddr), sizeof V);
        std::memcpy(P, mem(kProjMatrixAddr), sizeof P);
        float v[4], c[4];
        for (int i = 0; i < 4; ++i)
            v[i] = w[0] * V[i] + w[1] * V[4 + i] + w[2] * V[8 + i] + V[12 + i];
        for (int i = 0; i < 4; ++i)
            c[i] = v[0] * P[i] + v[1] * P[4 + i] + v[2] * P[8 + i] + v[3] * P[12 + i];
        if (!(c[3] > 0.5f) || !std::isfinite(c[0]) || !std::isfinite(c[1]))
            return false;
        nx = (c[0] / c[3] - 2048.0f) / 640.0f + 0.5f;
        ny = (c[1] / c[3] - 2048.0f) / 268.8f + 0.5f;
        return nx > -0.2f && nx < 1.2f && ny > -0.2f && ny < 1.2f;
    }

    void healthBarsTick()
    {
        static bool loaded = false;
        if (!loaded)
        {
            loadHealthVars();
            loaded = true;
        }
        std::vector<HpBar> bars;
        const uint32_t vm = rd32(kLevelVmPtr);
        if (!g_healthBars || !vm || (vm & PS2_RAM_MASK) != vm)
        {
            g_hpWatch.clear();
            std::lock_guard<std::mutex> lock(g_hpBarsMutex);
            g_hpBars.clear();
            return;
        }
        const uint64_t now = g_dbg.padFrames;
        // Screen orientation from Maximo: his head must come out above his feet.
        float feet[3];
        std::memcpy(feet, mem(kPlayerAddr + 0x1F0u), sizeof feet);
        // World up: the side of Maximo the game's camera looks down from.
        const float up = !g_dbg.haveGameCamera || g_dbg.gameEye[1] >= feet[1] ? 1.0f : -1.0f;
        bool flip = false;
        {
            const float head[3] = {feet[0], feet[1] + up * kHeadHeight, feet[2]};
            float fx = 0, fy = 0, hx = 0, hy = 0;
            if (projectToPicture(feet, fx, fy) && projectToPicture(head, hx, hy))
                flip = hy > fy;
            static int calls = 0;
            if (++calls % 600 == 1)
            {
                logf("debug: health bars: Maximo's feet on screen (%.2f, %.2f), head (%.2f, %.2f)%s, %zu enemies watched",
                     fx, flip ? 1.0f - fy : fy, hx, flip ? 1.0f - hy : hy, flip ? " (flipped)" : "", g_hpWatch.size());
            }
        }
        for (uint32_t o = rd32(vm + 0x9Cu + 8u), guard = 0; o && (o & PS2_RAM_MASK) == o && guard < 20000;
             o = rd32(o + 0xBCu), ++guard)
        {
            const uint32_t tmpl = rd32(o + 0xB4u);
            if (!tmpl || (tmpl & PS2_RAM_MASK) != tmpl)
                continue;
            const uint32_t type = rd32(tmpl);
            if (!barEnemyType(type))
                continue;
            const uint32_t vars = rd32(o + 0x84u), bytes = rd32(tmpl + 4u);
            if (!vars || (vars & PS2_RAM_MASK) != vars || !bytes || bytes >= 0x400u)
                continue;
            const uint32_t handle = rd32(o), words = std::min(bytes / 4u, 48u);
            auto it = std::find_if(g_hpWatch.begin(), g_hpWatch.end(), [&](const HpWatch &w) { return w.obj == o; });
            if (it == g_hpWatch.end() || it->handle != handle || it->tmpl != tmpl)
            {
                if (it == g_hpWatch.end())
                    it = g_hpWatch.insert(g_hpWatch.end(), HpWatch{});
                *it = HpWatch{o, handle, tmpl, type, words, {}, {}, {}, now, {}, false};
                {
                    const uint32_t e = rd32(o + 0xB8u);
                    const uint32_t a = e && (e & PS2_RAM_MASK) == e ? rd32(e) : 0u;
                    if (a && (a & PS2_RAM_MASK) == a)
                        std::memcpy(it->start, mem(a + 0xA0u), sizeof it->start);
                }
                for (uint32_t i = 0; i < words; ++i)
                {
                    it->prev[i] = it->maxSeen[i] = rd32(vars + 4u * i);
                    it->still[i] = now;
                }
            }
            HpWatch &w = *it;
            w.seen = now;
            for (uint32_t i = 0; i < w.words; ++i)
            {
                const uint32_t cur = rd32(vars + 4u * i);
                if (cur == w.prev[i])
                    continue;
                const int32_t before = static_cast<int32_t>(w.prev[i]), drop = before - static_cast<int32_t>(cur);
                if (!g_healthVar.count(type) && g_lastSwordDamage && drop == static_cast<int32_t>(g_lastSwordDamage) &&
                    now - g_lastSwordFrame < 120 && now - w.still[i] > 20 && before > 0 && before <= 255)
                {
                    g_healthVar[type] = static_cast<int>(i);
                    saveHealthVars();
                    logf("debug: health bars: type 0x%02X keeps its health in variable %u (%d -> %d, sword damage %u)",
                         type, i, before, static_cast<int32_t>(cur), g_lastSwordDamage);
                }
                if (static_cast<int32_t>(cur) > static_cast<int32_t>(w.maxSeen[i]))
                    w.maxSeen[i] = cur;
                w.prev[i] = cur;
                w.still[i] = now;
            }
            const auto known = g_healthVar.find(type);
            if (known == g_healthVar.end() || known->second >= static_cast<int>(w.words))
                continue;
            const int32_t hp = static_cast<int32_t>(w.prev[known->second]);
            const int32_t full = static_cast<int32_t>(w.maxSeen[known->second]);
            if (hp <= 0 || full <= 0)
                continue;
            const uint32_t entity = rd32(o + 0xB8u);
            const uint32_t actor = entity && (entity & PS2_RAM_MASK) == entity ? rd32(entity) : 0u;
            if (!actor || (actor & PS2_RAM_MASK) != actor)
                continue;
            float pos[3];
            std::memcpy(pos, mem(actor + 0xA0u), sizeof pos);
            if (!w.awake)
            {
                const float mx = pos[0] - w.start[0], my = pos[1] - w.start[1], mz = pos[2] - w.start[2];
                w.awake = mx * mx + my * my + mz * mz > 0.25f || hp < full;
                if (!w.awake)
                    continue;
            }
            const float dx = pos[0] - feet[0], dy = pos[1] - feet[1], dz = pos[2] - feet[2];
            if (dx * dx + dy * dy + dz * dz > kBarRange * kBarRange)
                continue;
            const float head[3] = {pos[0], pos[1] + up * headHeight(type), pos[2]};
            float nx, ny;
            if (!projectToPicture(head, nx, ny))
                continue;
            bars.push_back({nx, flip ? 1.0f - ny : ny, std::min(1.0f, static_cast<float>(hp) / static_cast<float>(full))});
        }
        g_hpWatch.erase(std::remove_if(g_hpWatch.begin(), g_hpWatch.end(), [&](const HpWatch &w) { return w.seen != now; }),
                        g_hpWatch.end());
        std::lock_guard<std::mutex> lock(g_hpBarsMutex);
        g_hpBars.swap(bars);
    }

    void drawHealthBars(std::vector<uint8_t> &px, uint32_t width, uint32_t height)
    {
        std::vector<HpBar> bars;
        {
            std::lock_guard<std::mutex> lock(g_hpBarsMutex);
            bars = g_hpBars;
        }
        const int bw = std::max(48, static_cast<int>(height) / 9), bh = std::max(7, static_cast<int>(height) / 60);
        const int border = std::max(1, bh / 4);
        for (const HpBar &b : bars)
        {
            const int x0 = static_cast<int>(b.x * width) - bw / 2, y0 = static_cast<int>(b.y * height) - bh / 2;
            const int fill = static_cast<int>(b.frac * (bw - 2 * border) + 0.5f);
            const uint8_t r = b.frac > 0.5f ? static_cast<uint8_t>(510 * (1.0f - b.frac)) : 255;
            const uint8_t g = b.frac > 0.5f ? 220 : static_cast<uint8_t>(440 * b.frac);
            for (int y = y0; y < y0 + bh; ++y)
            {
                if (y < 0 || y >= static_cast<int>(height))
                    continue;
                for (int x = x0; x < x0 + bw; ++x)
                {
                    if (x < 0 || x >= static_cast<int>(width))
                        continue;
                    uint8_t *p = &px[(static_cast<size_t>(y) * width + x) * 4u];
                    const bool edge = y < y0 + border || y >= y0 + bh - border || x < x0 + border || x >= x0 + bw - border;
                    if (edge)
                        p[0] = p[1] = p[2] = 0;
                    else if (x - x0 - border < fill)
                    {
                        p[0] = r;
                        p[1] = g;
                        p[2] = 40;
                    }
                    else
                    {
                        p[0] = static_cast<uint8_t>(p[0] / 4 + 40);
                        p[1] = static_cast<uint8_t>(p[1] / 4);
                        p[2] = static_cast<uint8_t>(p[2] / 4);
                    }
                }
            }
        }
    }

    // ---- PLAYER 2 (experimental, debug menu) ----------------------------------------------------------------------
    // A second Maximo on the second controller. The game keeps Maximo in two objects, both fixed globals that ~80
    // places in its code address directly: the game Player (0x263540, 0x490 bytes: the pad, his 17 game variables,
    // abilities and, in its first word, his body - a world object made from his model) and the player controller
    // (0x239890 = App 0x239880 + 0x10, up to App+0x21EAC: his move state at +0x248, positions at +0x1A0..+0x220, the
    // camera). So player 2 is a second copy of both blocks, swapped in for his turn ("context swap"): the game's own
    // code runs him, moves, sword, abilities and all.
    // - Made by the game's own level start for Maximo, Player::LevelStart (func_1297C0(player, level), called from
    //   func_121050): run once more with the Player block's body pointer cleared (func_129770 only destroys a body
    //   that is set), it builds a second body (func_148E90(world 0x25B9E0, -1, -1, 0, model *0x2639E0)) with
    //   everything attached. That block is player 2's; player 1's is put back. His controller starts as a copy of
    //   player 1's, moved 3 units aside.
    // - Each level frame (func_1221F0), afterwards: both blocks swapped in, the per-frame player calls the level loop
    //   makes - Player pad (func_128CB0, whose scePadRead gets the second controller instead), controller update
    //   (func_13D020) and its late part (func_13CE30), Player update (func_1302F0) - then player 1 put back, with the
    //   render matrices (0x2625A0..0x262670) as player 1's camera left them: the camera stays on player 1.
    // - His old body is removed only while it is still in the world's object list (count 0x262524, array 0x262528):
    //   a new level has already thrown it away with the rest of the world.
    // Not handled yet: enemies chase and hit player 1 only, and player 2's pickups and health use his own copy.
    constexpr uint32_t kLevelStartFn = GA(0x001297C0u), kBodyDestroyFn = GA(0x00148FA0u), kWorldAddr = GA(0x0025B9E0u);
    constexpr uint32_t kPlayerPadFn = GA(0x00128CB0u), kControllerLateFn = GA(0x0013CE30u), kPlayerActFn = GA(0x001302F0u);
    constexpr uint32_t kGamePlayerSize = 0x490u, kControllerSize = 0x21E9Cu;
    constexpr uint32_t kWorldObjCount = GA(0x00262524u), kWorldObjArray = GA(0x00262528u);
    constexpr uint32_t kRenderMatsAddr = GA(0x002625A0u), kRenderMatsSize = 0xD0u;
    struct Player2State
    {
        bool enabled = false, exists = false, wantCreate = false, pass = false, haveLevelArg = false;
        uint32_t levelArg = 0, body = 0, level = 0;
        std::vector<uint8_t> player, controller;
        uint64_t frames = 0;
        uint16_t buttons = 0; // active-high, libpad order (the last read)
        uint16_t buttonsSeen = 0;            // every button read since the last status line
        uint32_t reads = 0, stickReads = 0;  // pad reads in his turns since the last status line, with the stick moved
        uint8_t padState = 0;                // Player+0x18 at his turn (the game only reads a pad in state 4, 5 or 7)
        uint16_t gameButtons = 0, gameRaw = 0; // Player+0x4 / +0xA after his pad call
        uint8_t gameLx = 0, gameLy = 0;      // Player+0x10 / +0x11
        uint8_t lx = 0x80, ly = 0x80, rx = 0x80, ry = 0x80;
        // His own inventory: GameVars (+0x48, 17 words) and abilities (+0x424, 31 halfwords), kept when he is made
        // again (player 1's level restart, a new level, his own respawn).
        bool haveInventory = false;
        std::vector<uint8_t> vars, abilities;
        uint64_t downAt = 0; // frame his health reached 0 (0: alive)
    };
    Player2State g_p2;

    // ---- NETPLAY (debug menu): two games, the same level, over UDP ----------------------------------------------
    // Each game runs its own world; the other game's Maximo is player 2 here. Every level frame each side sends its
    // player 1: the pad the game read, his body's transform (actor +0x60..+0xBF: rotation rows, translation,
    // position, heading angles), his health, armor and lives, and the level. Player 2 then moves on that pad (his
    // swings and jumps play) and is put where the other game has him; his health, armor and lives are the other
    // game's own, so each player keeps his own inventory. Host listens on UDP 34567; join sends to MAXIMO_NET_PEER
    // (an IP address) or 127.0.0.1 - two windows on one PC.
    struct NetPacket
    {
        uint32_t magic, seq;
        int32_t level;
        uint32_t inLevel;
        uint16_t buttons; // active-high, libpad order
        uint8_t lx, ly, rx, ry;
        uint8_t pad[2];
        float actor[24];
        int32_t health, armor, lives;
    };
    constexpr uint32_t kNetMagic = 0x504E584Du; // "MXNP"
    constexpr uint16_t kNetPort = 34567;
    struct NetState
    {
        int mode = 0;     // 0 off, 1 host, 2 join
        int controls = 0; // player 1's input here: 0 keyboard + gamepad, 1 keyboard, 2 first gamepad, 3 second gamepad
        SOCKET sock = INVALID_SOCKET;
        sockaddr_in peer{};
        bool havePeer = false, haveRemote = false, linked = false;
        uint32_t seq = 0, remoteSeq = 0;
        uint64_t remoteAt = 0, frames = 0;
        NetPacket remote{};
        uint16_t localButtons = 0;
        uint8_t localLx = 0x80, localLy = 0x80, localRx = 0x80, localRy = 0x80;
    };
    NetState g_net;

    // ---- NETPLAY picture stream -----------------------------------------------------------------------------------
    // One world: the host's. The joining game shows the host's picture (each presented frame, downscaled to at most
    // 960 wide, compressed with miniz, over TCP 34568) and sends only its pad (the UDP packets above), which drives
    // player 2 in the host's game. Everything - enemies, kills, pickups, the ground rising - happens once, there.
    constexpr uint16_t kStreamPort = 34568;
    constexpr uint32_t kStreamMagic = 0x464E584Du; // "MXNF"
    struct StreamHeader
    {
        uint32_t magic, width, height, packed, raw;
        int32_t p2Health, p2Armor, p2Lives, p2In;
    };
    struct StreamState
    {
        std::atomic<bool> running{false}, connected{false};
        std::thread worker;
        std::mutex m;
        std::condition_variable cv;
        std::vector<uint8_t> offered; // host: the next frame to send (RGB)
        uint32_t ow = 0, oh = 0;
        bool haveOffer = false;
        std::vector<uint8_t> latest; // joiner: the newest frame received (RGB)
        uint32_t lw = 0, lh = 0;
        bool haveLatest = false;
        std::chrono::steady_clock::time_point latestAt{};
        int32_t p2[4] = {};
        std::atomic<int32_t> hostP2[4] = {};
        std::atomic<SOCKET> sock{INVALID_SOCKET}, listener{INVALID_SOCKET};
        uint64_t sent = 0, received = 0;
    };
    StreamState g_stream;
    float g_p2Eye[3] = {}, g_p2Target[3] = {}; // player 2's camera, from the look-at in his turn
    bool g_haveP2Camera = false;

    // World sync: the host's world is the shared one. The host sends the level's objects round-robin (their actor's
    // transform and their script variables - health among them) plus, twice a second, a roster of every object it
    // has; the joining game applies them to its own objects with the same handle and type, removes enemies the host
    // no longer has, and makes enemies it is missing (the host's spawns). The joiner's Maximo is player 2 in the
    // host's game, moving on the joiner's pad, so his sword, the switches he steps on and the ground he raises happen
    // in the host's world and come back from there. A spawn from the joiner's menu asks the host to make it there.
    struct NetObjRecord
    {
        uint32_t handle, type;
        float actor[24];
        uint32_t varCount;
        uint32_t vars[24];
    };
    constexpr int kNetObjsPerPacket = 6;
    struct NetObjPacket
    {
        uint32_t magic, seq, count, pad;
        NetObjRecord rec[kNetObjsPerPacket];
    };
    constexpr int kNetRosterMax = 160;
    struct NetRosterPacket
    {
        uint32_t magic, seq, part, parts, count, pad[3];
        uint32_t entry[kNetRosterMax * 2]; // handle, type
    };
    struct NetSpawnPacket
    {
        uint32_t magic, type, count, enemyClass;
        float at[3];
    };
    constexpr uint32_t kNetObjMagic = 0x4F4E584Du, kNetRosterMagic = 0x524E584Du, kNetSpawnMagic = 0x534E584Du;
    struct NetWorld
    {
        uint32_t cursor = 0;                    // host: where the round-robin is in the object list
        std::map<uint32_t, uint32_t> handleMap; // joiner: host handle -> our handle, for enemies made from the host's
        std::set<uint32_t> rosterNow, rosterLast; // joiner: host handles in the latest rosters
        uint32_t rosterSeq = 0, rosterParts = 0, rosterGot = 0;
        std::vector<NetSpawnPacket> spawnRequests; // host: spawns asked for by the joiner
        uint64_t applied = 0, made = 0, removed = 0;
    };
    NetWorld g_netWorld;
    // spawnPicked from a joiner's request: what and where, instead of the menu's pick and the camera.
    struct SpawnOverride
    {
        bool active = false;
        uint32_t type = 0, count = 1, enemyClass = 0;
        float at[3] = {};
    } g_spawnOverride;
    std::mutex g_p2HudMutex;
    std::string g_p2Hud; // drawn top right while he is in
    uint64_t g_p2Collisions = 0; // collisions with player 2's body (status line)
    // Every script API that reads the game Player directly (its handler reads *0x263540), with its name.
    struct TargetApi
    {
        uint32_t fn;
        const char *name;
        uint64_t calls[3]; // [0] in a script aimed at player 1, [1] aimed at player 2, [2] outside any object's script run
    };
    TargetApi g_targetApis[] = {
        {GA(0x0010F4B0u), "IsFreezing", {}},        {GA(0x0010F530u), "SetFreeze", {}},          {GA(0x0010F6A0u), "ResetFreeze", {}},
        {GA(0x00112250u), "SetCursedHand", {}},     {GA(0x001124D0u), "IsPlayerOnCursedLand", {}}, {GA(0x00112700u), "PlayerHit", {}},
        {GA(0x001163D0u), "api@1163D0", {}},        {GA(0x001189F0u), "GetLandType", {}},        {GA(0x00118B00u), "GetPosition", {}},
        {GA(0x00118BB0u), "GetDistance", {}},       {GA(0x00118C70u), "GetDistanceSquared", {}}, {GA(0x00118D20u), "GetOrientationAngles", {}},
        {GA(0x00118EF0u), "GetForwardVector", {}},  {GA(0x00119020u), "GetOrientationY", {}},    {GA(0x001190B0u), "GetVelocity", {}},
        {GA(0x001191B0u), "GetGroundPosition", {}}, {GA(0x001194C0u), "GetAngleYToTarget", {}},  {GA(0x001196B0u), "GetRelAngleYToTarget", {}},
        {GA(0x00119A50u), "GetAngleYToPoint", {}},  {GA(0x00119D20u), "GetRelAngleYToPoint", {}}, {GA(0x00119FA0u), "GetBonePosition", {}},
        {GA(0x0011A380u), "IsFacing", {}},          {GA(0x0011A4B0u), "CheckInScreen", {}},      {GA(0x0011A520u), "CheckWallHit", {}},
    };
    uint64_t g_apiPlayer[2] = {}, g_apiOther[2] = {}; // [0] aimed at player 1, [1] aimed at player 2
    uint64_t g_p2EnemyTurns = 0, g_p1EnemyTurns = 0; // enemy scripts run aimed at each player (status line)

    // The second connected gamepad (the runtime gives the first one, and the keyboard, to player 1).
    int player2Gamepad()
    {
        int found = 0;
        for (int i = 0; i < 8; ++i)
            if (IsGamepadAvailable(i) && ++found == 2)
                return i;
        return -1;
    }

    // scePadRead's buffer for player 2's turn: a DualShock in analog mode, buttons active-low.
    void fillPlayer2Pad(uint32_t data)
    {
        uint16_t b = 0;
        uint8_t lx = 0x80, ly = 0x80, rx = 0x80, ry = 0x80;
        const int pad = g_net.linked ? -1 : player2Gamepad();
        if (g_net.linked)
        {
            // The other game's player 1.
            b = g_net.remote.buttons;
            lx = g_net.remote.lx;
            ly = g_net.remote.ly;
            rx = g_net.remote.rx;
            ry = g_net.remote.ry;
        }
        else if (pad >= 0)
        {
            // raylib's GamepadButton numbers.
            static const struct { int button; uint16_t bit; } kMap[] = {
                {1, 1u << 4},  {2, 1u << 5},  {3, 1u << 6},  {4, 1u << 7},   // d-pad up, right, down, left
                {5, 1u << 12}, {6, 1u << 13}, {7, 1u << 14}, {8, 1u << 15},  // triangle, circle, cross, square
                {9, 1u << 10}, {10, 1u << 8}, {11, 1u << 11}, {12, 1u << 9}, // L1, L2, R1, R2
                {16, 1u << 1}, {17, 1u << 2},                                // L3, R3
            };
            for (const auto &m : kMap)
                if (IsGamepadButtonDown(pad, m.button))
                    b |= m.bit;
            auto axis = [&](int a)
            {
                const float v = std::clamp(GetGamepadAxisMovement(pad, a), -1.0f, 1.0f);
                return static_cast<uint8_t>(std::lround((v + 1.0f) * 127.5f));
            };
            lx = axis(0);
            ly = axis(1);
            rx = axis(2);
            ry = axis(3);
        }
        // The keyboard too, on keys player 1's mapping (arrows, WASD, Z X C V, Q E 1 3, Enter, Shift, Ctrl) leaves
        // free: I J K L walk, N jump (cross), B attack (square), M circle, comma triangle, U / O L1 / R1, 7 / 9 L2 / R2.
        // raylib key codes are the ASCII of the capital letter or digit.
        if (!g_net.linked)
        {
            static const struct { int key; uint16_t bit; } kKeys[] = {
                {'N', 1u << 14}, {'B', 1u << 15}, {'M', 1u << 13}, {',', 1u << 12},
                {'U', 1u << 10}, {'O', 1u << 11}, {'7', 1u << 8},  {'9', 1u << 9},
            };
            for (const auto &k : kKeys)
                if (IsKeyDown(k.key))
                    b |= k.bit;
            const int kx = (IsKeyDown('L') ? 1 : 0) - (IsKeyDown('J') ? 1 : 0);
            const int ky = (IsKeyDown('K') ? 1 : 0) - (IsKeyDown('I') ? 1 : 0);
            if (kx || ky)
            {
                lx = static_cast<uint8_t>(kx > 0 ? 0xFF : kx < 0 ? 0x00 : 0x80);
                ly = static_cast<uint8_t>(ky > 0 ? 0xFF : ky < 0 ? 0x00 : 0x80);
            }
        }
        uint8_t *d = mem(data);
        std::memset(d, 0, 32);
        d[1] = 0x73;
        d[2] = static_cast<uint8_t>(~b & 0xFFu);
        d[3] = static_cast<uint8_t>((~b >> 8) & 0xFFu);
        d[4] = rx;
        d[5] = ry;
        d[6] = lx;
        d[7] = ly;
        g_p2.buttons = b;
        g_p2.lx = lx;
        g_p2.ly = ly;
        g_p2.buttonsSeen |= b;
        ++g_p2.reads;
        if (lx != 0x80 || ly != 0x80)
            ++g_p2.stickReads;
    }

    bool inWorldList(uint32_t object)
    {
        const uint32_t count = rd32(kWorldObjCount), array = rd32(kWorldObjArray);
        if (!array || (array & PS2_RAM_MASK) != array || count > 4096u)
            return false;
        for (uint32_t i = 0; i < count; ++i)
            if (rd32(array + 4u * i) == object)
                return true;
        return false;
    }

    void savePlayer2Inventory()
    {
        if (g_p2.player.size() < kGamePlayerSize)
            return;
        g_p2.vars.assign(g_p2.player.begin() + 0x48, g_p2.player.begin() + 0x48 + 17 * 4);
        g_p2.abilities.assign(g_p2.player.begin() + 0x424, g_p2.player.begin() + 0x424 + 31 * 2);
        g_p2.haveInventory = true;
    }

    uint32_t p2Var(uint32_t i)
    {
        uint32_t v = 0;
        if (g_p2.player.size() >= kGamePlayerSize)
            std::memcpy(&v, g_p2.player.data() + 0x48 + 4 * i, 4);
        return v;
    }
    void setP2Var(uint32_t i, uint32_t v)
    {
        if (g_p2.player.size() >= kGamePlayerSize)
            std::memcpy(g_p2.player.data() + 0x48 + 4 * i, &v, 4);
    }

    // A new game's inventory (Player::InitNewGame's table at 0x1E0110, 6 bytes a variable: its start value first)
    // with a new life's health and armor (func_001293D0: armor 2, health twice the word at 0x238ABC), no abilities.
    void player2NewGameInventory()
    {
        for (uint32_t i = 0; i < 17; ++i)
            setP2Var(i, static_cast<uint32_t>(static_cast<int32_t>(static_cast<int16_t>(rd32(GA(0x001E0110u) + 6u * i) & 0xFFFFu))));
        setP2Var(3, 2u);
        setP2Var(2, rd32(GA(0x00238ABCu)) * 2u);
        for (uint32_t i = 0; i < 31; ++i)
        {
            g_p2.player[0x424 + 2 * i] = 0xFF;
            g_p2.player[0x425 + 2 * i] = 0;
        }
    }

    void removePlayer2(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, const char *why)
    {
        if (!g_p2.exists)
            return;
        uint32_t v0 = 0;
        const bool there = g_p2.body && inWorldList(g_p2.body);
        if (there)
            callGuest(rdram, ctx, runtime, kBodyDestroyFn, kWorldAddr, g_p2.body, 0, v0);
        savePlayer2Inventory();
        logf("debug: player 2 removed (%s)%s; his health %u, armor %u, lives %u kept", why,
             there ? ", body destroyed" : ", body already gone with the level", p2Var(2), p2Var(3), p2Var(4));
        g_p2.exists = false;
        g_p2.body = 0;
        g_p2.player.clear();
        g_p2.controller.clear();
        g_stream.hostP2[3] = 0;
        std::lock_guard<std::mutex> lock(g_p2HudMutex);
        g_p2Hud.clear();
    }

    // Runs the hooked function to its real return. Generated code stops at the scheduler's checkpoints (a timer or
    // interrupt due) with $pc mid-function and the run loop resumes it later - after a hook's own "afterwards" code,
    // which then ran too early (player 1 put back while the script went on). This follows the call to its return
    // address the way callGuestArgs does; a thread switch can't be followed and ends it early.
    void runToReturn(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t ra = reg(ctx, 31);
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        for (int steps = 0; ctx->pc != ra && steps < 4000000; ++steps)
        {
            if (ctx->pc == 0u || !runtime->hasFunction(ctx->pc) || runtime->isStopRequested() ||
                runtime->eeScheduler().transferPending())
                break;
            runtime->lookupFunction(ctx->pc)(rdram, ctx, runtime);
        }
    }

    // The screen fade (fade-busy byte 0x2633B6, rate 0x2633B8, value 0x2633BC, target 0x2633C0) is player 1's: player
    // 2's death started a fade to black that only player 1's continue would ever have faded back.
    constexpr uint32_t kFadeAddr = GA(0x002633B4u), kFadeSize = 0x10u;
    struct BlockSwap
    {
        std::vector<uint8_t> player, controller, mats, fade;
        void save()
        {
            player.assign(mem(kGamePlayerAddr), mem(kGamePlayerAddr) + kGamePlayerSize);
            controller.assign(mem(kPlayerAddr), mem(kPlayerAddr) + kControllerSize);
            mats.assign(mem(kRenderMatsAddr), mem(kRenderMatsAddr) + kRenderMatsSize);
            fade.assign(mem(kFadeAddr), mem(kFadeAddr) + kFadeSize);
        }
        void restore() const
        {
            std::memcpy(mem(kGamePlayerAddr), player.data(), kGamePlayerSize);
            std::memcpy(mem(kPlayerAddr), controller.data(), kControllerSize);
            std::memcpy(mem(kRenderMatsAddr), mats.data(), kRenderMatsSize);
            std::memcpy(mem(kFadeAddr), fade.data(), kFadeSize);
        }
    };

    void createPlayer2(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (!g_p2.haveLevelArg)
        {
            g_dbg.spawnMessage = "PLAYER 2: STARTS WITH THE NEXT LEVEL";
            return;
        }
        BlockSwap p1;
        p1.save();
        wr32(kGamePlayerAddr, 0u); // a new body, not player 1's destroyed
        g_p2.pass = true;
        uint32_t v0 = 0;
        // callGuestArgs follows the call through the scheduler's checkpoints (level start is long and hits them).
        const bool ok = callGuestArgs(rdram, ctx, runtime, kLevelStartFn, {kGamePlayerAddr, g_p2.levelArg}, v0);
        g_p2.pass = false;
        const uint32_t body = rd32(kGamePlayerAddr);
        g_p2.player.assign(mem(kGamePlayerAddr), mem(kGamePlayerAddr) + kGamePlayerSize);
        p1.restore();
        if (!ok || !body || (body & PS2_RAM_MASK) != body)
        {
            logf("debug: player 2: level start %s, body 0x%07X - not made", ok ? "ran" : "stopped", body);
            if (body && (body & PS2_RAM_MASK) == body && inWorldList(body))
                callGuest(rdram, ctx, runtime, kBodyDestroyFn, kWorldAddr, body, 0, v0);
            g_dbg.spawnMessage = "PLAYER 2: COULD NOT BE MADE";
            g_p2.player.clear();
            return;
        }
        // Three units aside: his body and the controller's position vectors (copied from player 1's).
        g_p2.controller = p1.controller;
        auto shift = [](uint8_t *base, uint32_t off)
        {
            float v[3];
            std::memcpy(v, base + off, 12);
            v[0] += 3.0f;
            std::memcpy(base + off, v, 12);
        };
        for (uint32_t off : {0x1A0u, 0x1D0u, 0x1F0u, 0x200u, 0x220u})
            shift(g_p2.controller.data(), off);
        const uint32_t actor = rd32(body);
        float bodyPos[3] = {}, p1Pos[3] = {};
        if (actor && (actor & PS2_RAM_MASK) == actor)
        {
            std::memcpy(bodyPos, mem(actor + 0xA0u), 12);
            std::memcpy(p1Pos, p1.controller.data() + 0x1F0, 12);
            const float to[3] = {p1Pos[0] + 3.0f, p1Pos[1], p1Pos[2]};
            std::memcpy(mem(actor + 0xA0u), to, 12);
        }
        // His own inventory: the one he had, else a new game's (not a copy of player 1's).
        if (g_p2.haveInventory)
        {
            std::memcpy(g_p2.player.data() + 0x48, g_p2.vars.data(), g_p2.vars.size());
            std::memcpy(g_p2.player.data() + 0x424, g_p2.abilities.data(), g_p2.abilities.size());
        }
        else
            player2NewGameInventory();
        g_p2.downAt = 0;
        g_p2.body = body;
        g_p2.exists = true;
        g_p2.frames = 0;
        g_p2.level = rd32(kCurrentLevelAddr);
        logf("debug: player 2 made: body 0x%07X (player 1's 0x%07X), level %u arg %d, body was at (%.1f, %.1f, %.1f), "
             "moved beside player 1 at (%.1f, %.1f, %.1f); second gamepad %d",
             body, rd32(kGamePlayerAddr), g_p2.level, static_cast<int32_t>(g_p2.levelArg), bodyPos[0], bodyPos[1],
             bodyPos[2], p1Pos[0] + 3.0f, p1Pos[1], p1Pos[2], player2Gamepad());
        g_dbg.spawnMessage = player2Gamepad() >= 0 ? "PLAYER 2 JOINED (2ND CONTROLLER OR IJKL + N/B)"
                                                   : "PLAYER 2 JOINED: IJKL WALK, N JUMP, B ATTACK";
    }

    // Player::LevelStart: notes its level argument (player 2 is made from it), clears player 2 away before the game
    // builds player 1 again, and has player 2 made after this frame.
    void levelStartEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const bool mine = !g_p2.pass && reg(ctx, 4) == kGamePlayerAddr;
        if (mine)
        {
            g_p2.levelArg = reg(ctx, 5);
            g_p2.haveLevelArg = true;
            removePlayer2(rdram, ctx, runtime, "level start");
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        if (mine && g_p2.enabled)
            g_p2.wantCreate = true;
    }

    bool sendAll(SOCKET s, const void *data, size_t size)
    {
        const char *p = static_cast<const char *>(data);
        while (size)
        {
            const int n = send(s, p, static_cast<int>(std::min<size_t>(size, 1u << 20)), 0);
            if (n <= 0)
                return false;
            p += n;
            size -= static_cast<size_t>(n);
        }
        return true;
    }
    bool recvAll(SOCKET s, void *data, size_t size)
    {
        char *p = static_cast<char *>(data);
        while (size)
        {
            const int n = recv(s, p, static_cast<int>(std::min<size_t>(size, 1u << 20)), 0);
            if (n <= 0)
                return false;
            p += n;
            size -= static_cast<size_t>(n);
        }
        return true;
    }

    // Host: accept the joining game, then send it every frame offered.
    void streamHostLoop()
    {
        SOCKET l = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        BOOL yes = TRUE;
        setsockopt(l, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char *>(&yes), sizeof yes);
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_addr.s_addr = htonl(INADDR_ANY);
        a.sin_port = htons(kStreamPort);
        if (l == INVALID_SOCKET || bind(l, reinterpret_cast<sockaddr *>(&a), sizeof a) != 0 || listen(l, 1) != 0)
        {
            logf("debug: netplay: could not open the picture stream on TCP %u (%d)", kStreamPort, WSAGetLastError());
            if (l != INVALID_SOCKET)
                closesocket(l);
            return;
        }
        g_stream.listener = l;
        std::vector<uint8_t> frame, packed;
        while (g_stream.running)
        {
            fd_set rd;
            FD_ZERO(&rd);
            FD_SET(l, &rd);
            timeval tv{0, 200000};
            if (select(0, &rd, nullptr, nullptr, &tv) <= 0)
                continue;
            sockaddr_in who{};
            int whoLen = sizeof who;
            SOCKET c = accept(l, reinterpret_cast<sockaddr *>(&who), &whoLen);
            if (c == INVALID_SOCKET)
                continue;
            const bool local = (ntohl(who.sin_addr.s_addr) >> 24) == 127u;
            BOOL noDelay = TRUE;
            setsockopt(c, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char *>(&noDelay), sizeof noDelay);
            g_stream.sock = c;
            g_stream.connected = true;
            logf("debug: netplay: the joining game is watching");
            while (g_stream.running)
            {
                uint32_t w = 0, h = 0;
                {
                    std::unique_lock<std::mutex> lock(g_stream.m);
                    g_stream.cv.wait_for(lock, std::chrono::milliseconds(200), [] { return g_stream.haveOffer || !g_stream.running; });
                    if (!g_stream.haveOffer)
                        continue;
                    frame.swap(g_stream.offered);
                    w = g_stream.ow;
                    h = g_stream.oh;
                    g_stream.haveOffer = false;
                }
                // A joiner on this PC gets the picture as it is (compressing ~1 MB 60 times a second cost the most);
                // one on another machine gets it compressed.
                mz_ulong size = static_cast<mz_ulong>(frame.size());
                if (local)
                    packed.assign(frame.begin(), frame.end());
                else
                {
                    size = mz_compressBound(static_cast<mz_ulong>(frame.size()));
                    packed.resize(size);
                    if (mz_compress2(packed.data(), &size, frame.data(), static_cast<mz_ulong>(frame.size()), 1) != MZ_OK)
                        continue;
                }
                StreamHeader hd{kStreamMagic, w, h, static_cast<uint32_t>(size), static_cast<uint32_t>(frame.size()),
                                g_stream.hostP2[0], g_stream.hostP2[1], g_stream.hostP2[2], g_stream.hostP2[3]};
                if (!sendAll(c, &hd, sizeof hd) || !sendAll(c, packed.data(), size))
                    break;
                ++g_stream.sent;
            }
            g_stream.connected = false;
            g_stream.sock = INVALID_SOCKET;
            closesocket(c);
            logf("debug: netplay: the joining game stopped watching");
        }
        g_stream.listener = INVALID_SOCKET;
        closesocket(l);
    }

    // Joiner: connect to the host (again every second until it answers) and keep the newest frame.
    void streamJoinLoop(sockaddr_in host)
    {
        host.sin_port = htons(kStreamPort);
        std::vector<uint8_t> packed, raw;
        while (g_stream.running)
        {
            SOCKET c = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
            if (c == INVALID_SOCKET || connect(c, reinterpret_cast<sockaddr *>(&host), sizeof host) != 0)
            {
                if (c != INVALID_SOCKET)
                    closesocket(c);
                std::this_thread::sleep_for(std::chrono::seconds(1));
                continue;
            }
            g_stream.sock = c;
            g_stream.connected = true;
            logf("debug: netplay: watching the host's game");
            while (g_stream.running)
            {
                StreamHeader hd;
                if (!recvAll(c, &hd, sizeof hd) || hd.magic != kStreamMagic || hd.raw != hd.width * hd.height * 3u ||
                    hd.raw > (64u << 20) || hd.packed > (64u << 20))
                    break;
                packed.resize(hd.packed);
                raw.resize(hd.raw);
                if (!recvAll(c, packed.data(), hd.packed))
                    break;
                if (hd.packed == hd.raw)
                    raw.swap(packed); // sent as it is (same PC)
                else
                {
                    mz_ulong size = hd.raw;
                    if (mz_uncompress(raw.data(), &size, packed.data(), hd.packed) != MZ_OK || size != hd.raw)
                        continue;
                }
                std::lock_guard<std::mutex> lock(g_stream.m);
                g_stream.latest.swap(raw);
                g_stream.lw = hd.width;
                g_stream.lh = hd.height;
                g_stream.haveLatest = true;
                g_stream.latestAt = std::chrono::steady_clock::now();
                g_stream.p2[0] = hd.p2Health;
                g_stream.p2[1] = hd.p2Armor;
                g_stream.p2[2] = hd.p2Lives;
                g_stream.p2[3] = hd.p2In;
                ++g_stream.received;
            }
            g_stream.connected = false;
            g_stream.sock = INVALID_SOCKET;
            closesocket(c);
        }
    }

    void streamStop()
    {
        g_stream.running = false;
        g_stream.cv.notify_all();
        if (SOCKET s = g_stream.sock.exchange(INVALID_SOCKET); s != INVALID_SOCKET)
            closesocket(s);
        if (SOCKET s = g_stream.listener.exchange(INVALID_SOCKET); s != INVALID_SOCKET)
            closesocket(s);
        if (g_stream.worker.joinable())
            g_stream.worker.join();
        g_stream.connected = false;
        std::lock_guard<std::mutex> lock(g_stream.m);
        g_stream.haveLatest = g_stream.haveOffer = false;
    }

    void streamStart(int mode, const sockaddr_in &peer)
    {
        streamStop();
        g_stream.running = true;
        if (mode == 1)
            g_stream.worker = std::thread(streamHostLoop);
        else
            g_stream.worker = std::thread(streamJoinLoop, peer);
    }

    // Presentation thread, host: hand the finished frame (before the debug menu is drawn on it) to the sender,
    // downscaled to at most 960 wide, RGB. A frame still waiting is replaced.
    void streamOfferFrame(const std::vector<uint8_t> &px, uint32_t width, uint32_t height)
    {
        if (g_net.mode != 1 || !g_stream.connected || width == 0 || height == 0)
            return;
        const uint32_t f = (width + 959u) / 960u, w = width / f, h = height / f;
        std::lock_guard<std::mutex> lock(g_stream.m);
        g_stream.offered.resize(static_cast<size_t>(w) * h * 3u);
        uint8_t *out = g_stream.offered.data();
        for (uint32_t y = 0; y < h; ++y)
        {
            const uint8_t *row = px.data() + (static_cast<size_t>(y) * f * width) * 4u;
            for (uint32_t x = 0; x < w; ++x, out += 3)
            {
                const uint8_t *p = row + static_cast<size_t>(x) * f * 4u;
                out[0] = p[0];
                out[1] = p[1];
                out[2] = p[2];
            }
        }
        g_stream.ow = w;
        g_stream.oh = h;
        g_stream.haveOffer = true;
        g_stream.cv.notify_one();
    }

    // Presentation thread, joiner: the host's picture instead of this game's.
    bool streamShowFrame(std::vector<uint8_t> &px, uint32_t &width, uint32_t &height)
    {
        if (g_net.mode != 2 || !g_stream.connected)
            return false;
        std::lock_guard<std::mutex> lock(g_stream.m);
        if (!g_stream.haveLatest || std::chrono::steady_clock::now() - g_stream.latestAt > std::chrono::seconds(2))
            return false;
        width = g_stream.lw;
        height = g_stream.lh;
        px.resize(static_cast<size_t>(width) * height * 4u);
        const uint8_t *in = g_stream.latest.data();
        uint8_t *out = px.data();
        for (size_t i = 0, n = static_cast<size_t>(width) * height; i < n; ++i, in += 3, out += 4)
        {
            out[0] = in[0];
            out[1] = in[1];
            out[2] = in[2];
            out[3] = 255;
        }
        return true;
    }

    // Split view: hosting, a joining game watching, and player 2 here with a camera of his own.
    bool splitViewWanted()
    {
        return g_splitEnabled && g_net.mode == 1 && g_net.linked && g_net.remote.pad[0] == 1u && g_stream.connected && g_p2.exists &&
               g_haveP2Camera && !(g_dbg.freeCam && g_dbg.freeCamReady);
    }

    void netClose()
    {
        streamStop();
        if (g_net.sock != INVALID_SOCKET)
            closesocket(g_net.sock);
        g_net.sock = INVALID_SOCKET;
        g_net.havePeer = g_net.haveRemote = g_net.linked = false;
    }

    void netOpen(int mode)
    {
        netClose();
        g_net.mode = mode;
        if (!mode)
            return;
        static bool started = false;
        if (!started)
        {
            WSADATA wsa;
            started = WSAStartup(MAKEWORD(2, 2), &wsa) == 0;
        }
        g_net.sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (g_net.sock == INVALID_SOCKET)
        {
            logf("debug: netplay: no socket (%d)", WSAGetLastError());
            g_net.mode = 0;
            return;
        }
        u_long nonBlocking = 1;
        ioctlsocket(g_net.sock, FIONBIO, &nonBlocking);
        sockaddr_in local{};
        local.sin_family = AF_INET;
        local.sin_addr.s_addr = htonl(INADDR_ANY);
        local.sin_port = htons(mode == 1 ? kNetPort : 0);
        if (bind(g_net.sock, reinterpret_cast<sockaddr *>(&local), sizeof local) != 0)
        {
            logf("debug: netplay: could not listen on UDP %u (%d) - is another game hosting?", kNetPort, WSAGetLastError());
            g_dbg.spawnMessage = "NETPLAY: PORT 34567 IS TAKEN";
            netClose();
            g_net.mode = 0;
            return;
        }
        if (mode == 2)
        {
            const char *peer = std::getenv("MAXIMO_NET_PEER");
            g_net.peer.sin_family = AF_INET;
            g_net.peer.sin_port = htons(kNetPort);
            if (inet_pton(AF_INET, peer && *peer ? peer : "127.0.0.1", &g_net.peer.sin_addr) != 1)
                inet_pton(AF_INET, "127.0.0.1", &g_net.peer.sin_addr);
            g_net.havePeer = true;
        }
        g_net.seq = g_net.remoteSeq = 0;
        streamStart(mode, g_net.peer);
        logf("debug: netplay: %s", mode == 1 ? "hosting on UDP 34567 (picture on TCP 34568)"
                                             : "joining: this window shows the host's game, your pad is player 2 there");
    }

    uint32_t objectActor(uint32_t obj)
    {
        const uint32_t entity = obj && (obj & PS2_RAM_MASK) == obj ? rd32(obj + 0xB8u) : 0u;
        const uint32_t actor = entity && (entity & PS2_RAM_MASK) == entity ? rd32(entity) : 0u;
        return actor && (actor & PS2_RAM_MASK) == actor ? actor : 0u;
    }
    uint32_t objectType(uint32_t obj)
    {
        const uint32_t tmpl = obj && (obj & PS2_RAM_MASK) == obj ? rd32(obj + 0xB4u) : 0u;
        return tmpl && (tmpl & PS2_RAM_MASK) == tmpl ? rd32(tmpl) : 0u;
    }
    bool netEnemyType(uint32_t type)
    {
        for (const EnemyType &t : kEnemyTypes)
            if (t.id == type)
                return std::strcmp(t.home, "NPC") != 0;
        return false;
    }

    void netSend(const void *data, size_t size)
    {
        if (g_net.havePeer)
            sendto(g_net.sock, reinterpret_cast<const char *>(data), static_cast<int>(size), 0,
                   reinterpret_cast<const sockaddr *>(&g_net.peer), sizeof g_net.peer);
    }

    // Host: three packets of objects a frame, round-robin through the level's list; a roster every 30 frames.
    void netSendWorld()
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        std::vector<uint32_t> objs;
        for (uint32_t o = rd32(vm + 0x9Cu + 8u), guard = 0; o && (o & PS2_RAM_MASK) == o && guard < 20000;
             o = rd32(o + 0xBCu), ++guard)
            if (objectActor(o) && objectType(o))
                objs.push_back(o);
        if (objs.empty())
            return;
        for (int p = 0; p < 3; ++p)
        {
            NetObjPacket pk{};
            pk.magic = kNetObjMagic;
            pk.seq = g_net.seq;
            for (int i = 0; i < kNetObjsPerPacket; ++i)
            {
                const uint32_t o = objs[g_netWorld.cursor++ % objs.size()];
                NetObjRecord &r = pk.rec[pk.count++];
                r.handle = rd32(o);
                r.type = objectType(o);
                std::memcpy(r.actor, mem(objectActor(o) + 0x60u), sizeof r.actor);
                const uint32_t vars = rd32(o + 0x84u), bytes = rd32(rd32(o + 0xB4u) + 4u);
                r.varCount = vars && (vars & PS2_RAM_MASK) == vars && bytes < 0x400u ? std::min(bytes / 4u, 24u) : 0u;
                if (r.varCount)
                    std::memcpy(r.vars, mem(vars), r.varCount * 4u);
            }
            netSend(&pk, sizeof pk);
        }
        if (g_net.frames % 30u == 0u)
        {
            const uint32_t parts = static_cast<uint32_t>((objs.size() + kNetRosterMax - 1) / kNetRosterMax);
            for (uint32_t part = 0; part < parts; ++part)
            {
                NetRosterPacket pk{};
                pk.magic = kNetRosterMagic;
                pk.seq = g_net.seq;
                pk.part = part;
                pk.parts = parts;
                for (size_t i = part * kNetRosterMax; i < objs.size() && pk.count < static_cast<uint32_t>(kNetRosterMax); ++i)
                {
                    pk.entry[pk.count * 2] = rd32(objs[i]);
                    pk.entry[pk.count * 2 + 1] = objectType(objs[i]);
                    ++pk.count;
                }
                netSend(&pk, sizeof pk);
            }
        }
    }

    uint32_t objectForHandle(uint32_t handle);
    uint32_t spawnOne(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t vm, uint32_t tmpl,
                      const float pos[3], uint32_t scratch, std::string &why, const std::vector<RecordProp> *recordProps,
                      const RecordProp *classProp);
    bool destroyLevelObject(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t vm, uint32_t obj,
                            bool detach = true);

    // Joiner: an object record from the host onto our object with that handle (or the one made for it).
    void netApplyObject(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, const NetObjRecord &r)
    {
        const auto mapped = g_netWorld.handleMap.find(r.handle);
        const uint32_t handle = mapped != g_netWorld.handleMap.end() ? mapped->second : r.handle;
        uint32_t o = objectForHandle(handle);
        if (o && objectType(o) != r.type)
            o = 0;
        if (!o)
        {
            // An enemy we don't have (the host spawned it): make one there.
            if (!netEnemyType(r.type) || mapped != g_netWorld.handleMap.end() || g_netWorld.handleMap.size() > 256u)
                return;
            const uint32_t vm = rd32(kLevelVmPtr);
            uint32_t tmpl = 0;
            if (!callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, r.type, 0, tmpl) || !tmpl)
                return;
            const uint32_t savedSp = reg(ctx, 29), sp = (savedSp - 0x200u) & ~0xFu;
            SET_GPR_U32(ctx, 29, sp);
            std::string why;
            const uint32_t made = spawnOne(rdram, ctx, runtime, vm, tmpl, r.actor + 16, sp + 0x20u, why, nullptr, nullptr);
            SET_GPR_U32(ctx, 29, savedSp);
            if (!made)
                return;
            g_netWorld.handleMap[r.handle] = made;
            ++g_netWorld.made;
            o = objectForHandle(made);
            if (!o)
                return;
        }
        const uint32_t actor = objectActor(o);
        if (actor)
            std::memcpy(mem(actor + 0x60u), r.actor, sizeof r.actor);
        const uint32_t vars = rd32(o + 0x84u), bytes = rd32(rd32(o + 0xB4u) + 4u);
        if (r.varCount && vars && (vars & PS2_RAM_MASK) == vars && bytes / 4u >= r.varCount)
            for (uint32_t i = 0; i < r.varCount; ++i)
            {
                // Not a word that looks like an address: the two games' heaps need not match.
                const uint32_t v = r.vars[i];
                if (v >= 0x00100000u && v < PS2_RAM_SIZE && (v & 3u) == 0)
                    continue;
                wr32(vars + 4u * i, v);
            }
        ++g_netWorld.applied;
    }

    // Joiner: enemies the host no longer has (in neither of its last two rosters) go here too.
    void netApplyRoster(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        std::set<uint32_t> ours; // our handles the host accounts for
        for (uint32_t h : g_netWorld.rosterNow)
        {
            const auto m = g_netWorld.handleMap.find(h);
            ours.insert(m != g_netWorld.handleMap.end() ? m->second : h);
        }
        for (uint32_t h : g_netWorld.rosterLast)
        {
            const auto m = g_netWorld.handleMap.find(h);
            ours.insert(m != g_netWorld.handleMap.end() ? m->second : h);
        }
        std::vector<uint32_t> gone;
        for (uint32_t o = rd32(vm + 0x9Cu + 8u), guard = 0; o && (o & PS2_RAM_MASK) == o && guard < 20000;
             o = rd32(o + 0xBCu), ++guard)
            if (netEnemyType(objectType(o)) && objectActor(o) && !ours.count(rd32(o)))
                gone.push_back(o);
        for (uint32_t o : gone)
            if (destroyLevelObject(rdram, ctx, runtime, vm, o))
                ++g_netWorld.removed;
    }

    // Host: a spawn the joiner asked for, made here (spawnPicked with its type, count, class and place).
    void spawnPicked(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime);

    // Once a level frame: send our player 1, take the newest packet from the other game.
    void netTick(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (!g_net.mode || g_net.sock == INVALID_SOCKET)
        {
            g_net.linked = false;
            return;
        }
        ++g_net.frames;
        const uint32_t vm = rd32(kLevelVmPtr);
        const bool inLevel = vm && (vm & PS2_RAM_MASK) == vm && rd32(kLevelExitCodeAddr) == 0;
        if (g_net.havePeer)
        {
            NetPacket out{};
            out.magic = kNetMagic;
            out.seq = ++g_net.seq;
            out.level = static_cast<int32_t>(rd32(kCurrentLevelAddr));
            out.inLevel = inLevel ? 1u : 0u;
            out.buttons = g_net.localButtons;
            out.lx = g_net.localLx;
            out.ly = g_net.localLy;
            out.rx = g_net.localRx;
            out.ry = g_net.localRy;
            const uint32_t body = rd32(kGamePlayerAddr);
            const uint32_t actor = body && (body & PS2_RAM_MASK) == body ? rd32(body) : 0u;
            if (inLevel && actor && (actor & PS2_RAM_MASK) == actor)
                std::memcpy(out.actor, mem(actor + 0x60u), sizeof out.actor);
            out.health = static_cast<int32_t>(rd32(gameVarAddr(2)));
            out.armor = static_cast<int32_t>(rd32(gameVarAddr(3)));
            out.lives = static_cast<int32_t>(rd32(gameVarAddr(4)));
            out.pad[0] = g_net.mode == 2 ? 1u : 0u; // a joiner watches the host's game: its pad is the host's player 2
            sendto(g_net.sock, reinterpret_cast<const char *>(&out), sizeof out, 0,
                   reinterpret_cast<const sockaddr *>(&g_net.peer), sizeof g_net.peer);
        }
        static std::vector<uint8_t> buffer(sizeof(NetRosterPacket) + 64);
        for (int i = 0; i < 256; ++i)
        {
            NetPacket in;
            sockaddr_in from{};
            int fromLen = sizeof from;
            const int n = recvfrom(g_net.sock, reinterpret_cast<char *>(buffer.data()), static_cast<int>(buffer.size()), 0,
                                   reinterpret_cast<sockaddr *>(&from), &fromLen);
            if (n < 0)
                break;
            uint32_t magic = 0;
            if (n >= 4)
                std::memcpy(&magic, buffer.data(), 4);
            const uint32_t vmNow = rd32(kLevelVmPtr);
            const bool here = vmNow && (vmNow & PS2_RAM_MASK) == vmNow && rd32(kLevelExitCodeAddr) == 0;
            if (magic == kNetObjMagic && n == static_cast<int>(sizeof(NetObjPacket)))
            {
                if (g_net.mode == 2 && g_net.linked && here)
                {
                    NetObjPacket pk;
                    std::memcpy(&pk, buffer.data(), sizeof pk);
                    for (uint32_t k = 0; k < pk.count && k < static_cast<uint32_t>(kNetObjsPerPacket); ++k)
                        netApplyObject(rdram, ctx, runtime, pk.rec[k]);
                }
                continue;
            }
            if (magic == kNetRosterMagic && n == static_cast<int>(sizeof(NetRosterPacket)))
            {
                if (g_net.mode == 2 && g_net.linked && here)
                {
                    NetRosterPacket pk;
                    std::memcpy(&pk, buffer.data(), sizeof pk);
                    if (pk.seq != g_netWorld.rosterSeq)
                    {
                        g_netWorld.rosterSeq = pk.seq;
                        g_netWorld.rosterParts = pk.parts;
                        g_netWorld.rosterGot = 0;
                        g_netWorld.rosterLast.swap(g_netWorld.rosterNow);
                        g_netWorld.rosterNow.clear();
                    }
                    for (uint32_t k = 0; k < pk.count && k < static_cast<uint32_t>(kNetRosterMax); ++k)
                        g_netWorld.rosterNow.insert(pk.entry[k * 2]);
                    if (++g_netWorld.rosterGot == g_netWorld.rosterParts && !g_netWorld.rosterLast.empty())
                        netApplyRoster(rdram, ctx, runtime);
                }
                continue;
            }
            if (magic == kNetSpawnMagic && n == static_cast<int>(sizeof(NetSpawnPacket)))
            {
                if (g_net.mode == 1 && here)
                {
                    NetSpawnPacket pk;
                    std::memcpy(&pk, buffer.data(), sizeof pk);
                    g_netWorld.spawnRequests.push_back(pk);
                }
                continue;
            }
            if (magic != kNetMagic || n != static_cast<int>(sizeof in))
                continue;
            std::memcpy(&in, buffer.data(), sizeof in);
            if (g_net.mode == 1 && !g_net.havePeer)
            {
                g_net.peer = from;
                g_net.havePeer = true;
                logf("debug: netplay: the other game joined");
                g_dbg.spawnMessage = "NETPLAY: CONNECTED";
            }
            // Newer packets only (a restarted game starts its count again).
            if (!g_net.haveRemote || in.seq > g_net.remoteSeq || in.seq + 600u < g_net.remoteSeq)
            {
                g_net.remote = in;
                g_net.remoteSeq = in.seq;
                g_net.haveRemote = true;
                g_net.remoteAt = g_net.frames;
            }
        }
        const bool fresh = g_net.haveRemote && g_net.frames - g_net.remoteAt < 120u;
        const bool wasLinked = g_net.linked;
        // Host: the joining game is player 2 here whenever this game is in a level (the joiner watches this world).
        // Joiner: no player 2 of its own - it is the host's.
        g_net.linked = g_net.mode == 1 && fresh && inLevel && g_net.remote.pad[0] == 1u;
        if (g_net.linked && g_net.mode == 1 && g_net.remote.pad[0] != 1u)
        {
            netSendWorld();
            for (const NetSpawnPacket &rq : g_netWorld.spawnRequests)
            {
                g_spawnOverride.active = true;
                g_spawnOverride.type = rq.type;
                g_spawnOverride.count = std::clamp<uint32_t>(rq.count, 1u, 20u);
                g_spawnOverride.enemyClass = rq.enemyClass;
                std::memcpy(g_spawnOverride.at, rq.at, sizeof rq.at);
                spawnPicked(rdram, ctx, runtime);
                g_spawnOverride.active = false;
            }
            g_netWorld.spawnRequests.clear();
        }
        if (g_net.linked != wasLinked)
        {
            g_netWorld.handleMap.clear();
            g_netWorld.rosterNow.clear();
            g_netWorld.rosterLast.clear();
            logf("debug: netplay: %s", g_net.linked ? "both in the same level - the other player is player 2 here"
                                                    : "not in the same level any more");
            g_dbg.spawnMessage = g_net.linked ? "NETPLAY: THE OTHER PLAYER IS HERE" : "NETPLAY: THE OTHER PLAYER LEFT";
        }
    }

    // After player 2's turn: put him where the other game has its Maximo, facing the same way, with its health.
    void netSnapPlayer2()
    {
        const uint32_t actor = g_p2.body && (g_p2.body & PS2_RAM_MASK) == g_p2.body ? rd32(g_p2.body) : 0u;
        if (!actor || (actor & PS2_RAM_MASK) != actor || g_p2.controller.size() < kControllerSize)
            return;
        const float *pos = g_net.remote.actor + 16; // +0xA0
        if (!(std::fabs(pos[0]) + std::fabs(pos[1]) + std::fabs(pos[2]) > 0.0f))
            return;
        float feet[3];
        std::memcpy(feet, g_p2.controller.data() + 0x1F0, 12);
        const float d[3] = {pos[0] - feet[0], pos[1] - feet[1], pos[2] - feet[2]};
        for (uint32_t off : {0x1A0u, 0x1D0u, 0x1F0u, 0x200u, 0x220u})
        {
            float v[3];
            std::memcpy(v, g_p2.controller.data() + off, 12);
            for (int i = 0; i < 3; ++i)
                v[i] += d[i];
            std::memcpy(g_p2.controller.data() + off, v, 12);
        }
        std::memcpy(mem(actor + 0x60u), g_net.remote.actor, sizeof g_net.remote.actor);
        setP2Var(2, static_cast<uint32_t>(g_net.remote.health));
        setP2Var(3, static_cast<uint32_t>(g_net.remote.armor));
        setP2Var(4, static_cast<uint32_t>(g_net.remote.lives));
    }

    void player2AfterFrame(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t mode)
    {
        if (g_net.mode != 2) // a joiner ticks from its pad reads, in any screen
            netTick(rdram, ctx, runtime);
        if (g_net.mode)
        {
            // With netplay, player 2 is the other game's player, here while both are in the same level.
            if (g_net.linked && !g_p2.enabled)
                g_p2.enabled = true, g_p2.wantCreate = true;
            else if (!g_net.linked && g_p2.enabled)
                g_p2.enabled = false;
        }
        const uint32_t vm = rd32(kLevelVmPtr);
        const bool level = vm && (vm & PS2_RAM_MASK) == vm && rd32(kLevelExitCodeAddr) == 0;
        if (!g_p2.enabled || !level)
        {
            if (g_p2.exists && !g_p2.enabled)
                removePlayer2(rdram, ctx, runtime, "turned off");
            g_p2.wantCreate = false;
            return;
        }
        if (g_p2.wantCreate)
        {
            g_p2.wantCreate = false;
            createPlayer2(rdram, ctx, runtime);
            return;
        }
        // Only in play: not the attract demo or the level loop's other modes, nor while player 1's controls are off
        // (pause, cutscenes, a boss's end).
        if (!g_p2.exists || mode != 0u || rd32(kGamePlayerAddr + 0x84u) == 0u)
            return;
        BlockSwap p1;
        p1.save();
        std::memcpy(mem(kGamePlayerAddr), g_p2.player.data(), kGamePlayerSize);
        std::memcpy(mem(kPlayerAddr), g_p2.controller.data(), kControllerSize);
        g_p2.pass = true;
        g_turnContext = 1;
        uint32_t v0 = 0;
        const char *stopped = nullptr;
        g_p2.padState = *mem(kGamePlayerAddr + 0x18u);
        // The pad's connection state (Player+0x18..0x1A: the game reads only in state 4, 5 or 7) is the hardware's,
        // the same for both: player 1's.
        std::memcpy(mem(kGamePlayerAddr + 0x18u), p1.player.data() + 0x18, 3);
        if (!callGuestArgs(rdram, ctx, runtime, kPlayerPadFn, {kGamePlayerAddr, mode}, v0))
            stopped = "pad";
        g_p2.gameButtons = static_cast<uint16_t>(rd32(kGamePlayerAddr + 0x4u) & 0xFFFFu);
        g_p2.gameRaw = static_cast<uint16_t>(rd32(kGamePlayerAddr + 0x8u) >> 16);
        g_p2.gameLx = *mem(kGamePlayerAddr + 0x10u);
        g_p2.gameLy = *mem(kGamePlayerAddr + 0x11u);
        if (stopped)
            ;
        else if (!callGuestArgs(rdram, ctx, runtime, kPlayerUpdateFn, {kPlayerAddr}, v0))
            stopped = "controller update";
        else if (!callGuestArgs(rdram, ctx, runtime, kControllerLateFn, {kPlayerAddr}, v0))
            stopped = "controller late";
        else if (!callGuestArgs(rdram, ctx, runtime, kPlayerActFn, {kGamePlayerAddr}, v0))
            stopped = "player update";
        const bool ok = stopped == nullptr;
        g_p2.pass = false;
        g_turnContext = 0;
        g_p2.player.assign(mem(kGamePlayerAddr), mem(kGamePlayerAddr) + kGamePlayerSize);
        g_p2.controller.assign(mem(kPlayerAddr), mem(kPlayerAddr) + kControllerSize);
        p1.restore();
        ++g_p2.frames;
        if (g_net.linked && g_net.remote.pad[0] != 1u)
            netSnapPlayer2(); // the other game decides where he is and how he is - not this one
        // His death: the game's own death and continue only ever look at player 1, so player 2 is handled here. At
        // health 0 he goes down; 150 frames (about 3 s) later he loses a life and comes back beside player 1 with a new
        // life's health and armor - or, with no lives left, he is out until PLAYER 2 is turned on again.
        {
            const int32_t health = g_net.linked && g_net.remote.pad[0] != 1u ? 1 : static_cast<int32_t>(p2Var(2));
            if (health <= 0 && !g_p2.downAt)
            {
                g_p2.downAt = g_p2.frames;
                logf("debug: player 2 is down (lives %u)", p2Var(4));
                g_dbg.spawnMessage = "PLAYER 2 IS DOWN";
            }
            else if (g_p2.downAt && g_p2.frames - g_p2.downAt >= 150u)
            {
                const uint32_t lives = p2Var(4);
                if (lives <= 1u)
                {
                    setP2Var(4, 0u);
                    removePlayer2(rdram, ctx, runtime, "out of lives");
                    g_p2.haveInventory = false; // a fresh start next time he joins
                    g_p2.enabled = false;
                    g_dbg.spawnMessage = "PLAYER 2 IS OUT (NO LIVES LEFT)";
                    std::lock_guard<std::mutex> lock(g_p2HudMutex);
                    g_p2Hud.clear();
                    return;
                }
                setP2Var(4, lives - 1u);
                setP2Var(3, 2u);
                setP2Var(2, rd32(GA(0x00238ABCu)) * 2u);
                removePlayer2(rdram, ctx, runtime, "respawn");
                g_p2.wantCreate = true;
                g_dbg.spawnMessage = "PLAYER 2 IS BACK";
                return;
            }
        }
        {
            char b[96];
            g_stream.hostP2[0] = static_cast<int32_t>(p2Var(2));
            g_stream.hostP2[1] = static_cast<int32_t>(p2Var(3));
            g_stream.hostP2[2] = static_cast<int32_t>(p2Var(4));
            g_stream.hostP2[3] = 1;
            std::snprintf(b, sizeof b, "%s  HEALTH %d  ARMOR %d  LIVES %d%s", g_net.linked ? "P2 (ONLINE)" : "P2",
                          static_cast<int32_t>(p2Var(2)),
                          static_cast<int32_t>(p2Var(3)), static_cast<int32_t>(p2Var(4)), g_p2.downAt ? "  (DOWN)" : "");
            std::lock_guard<std::mutex> lock(g_p2HudMutex);
            g_p2Hud = b;
        }
        if (g_p2.frames % 120u == 1u || !ok)
        {
            float pos[3] = {};
            const uint32_t actor = rd32(g_p2.body);
            if (actor && (actor & PS2_RAM_MASK) == actor)
                std::memcpy(pos, mem(actor + 0xA0u), 12);
            float c[3];
            std::memcpy(c, g_p2.controller.data() + 0x1F0, 12);
            uint32_t state, health;
            std::memcpy(&state, g_p2.controller.data() + 0x248, 4);
            std::memcpy(&health, g_p2.player.data() + 0x48 + 2 * 4, 4);
            if (!ok)
                logf("debug: player 2: the %s call stopped", stopped);
            logf("debug: player 2 frame %llu%s: body at (%.1f, %.1f, %.1f), controller feet (%.1f, %.1f, %.1f), state %u, "
                 "health %u, gamepad %d | pad reads %u (stick moved in %u), buttons seen 0x%04X | pad state %u, after "
                 "the pad call: buttons 0x%04X raw 0x%04X stick (%u, %u)", static_cast<unsigned long long>(g_p2.frames),
                 ok ? "" : " (a call stopped)", pos[0], pos[1], pos[2], c[0], c[1], c[2], state, health,
                 player2Gamepad(), g_p2.reads, g_p2.stickReads, g_p2.buttonsSeen, g_p2.padState, g_p2.gameButtons,
                 g_p2.gameRaw, g_p2.gameLx, g_p2.gameLy);
            logf("debug: player 2: since the last line: script runs %llu aimed at player 2, %llu at player 1; "
                 "%llu collisions with his body", static_cast<unsigned long long>(g_p2EnemyTurns),
                 static_cast<unsigned long long>(g_p1EnemyTurns), static_cast<unsigned long long>(g_p2Collisions));
            logf("debug: player 2: targeting calls - aimed at player 2: %llu at the player, %llu at other objects; aimed "
                 "at player 1: %llu at the player, %llu at other objects", static_cast<unsigned long long>(g_apiPlayer[1]),
                 static_cast<unsigned long long>(g_apiOther[1]), static_cast<unsigned long long>(g_apiPlayer[0]),
                 static_cast<unsigned long long>(g_apiOther[0]));
            g_apiPlayer[0] = g_apiPlayer[1] = g_apiOther[0] = g_apiOther[1] = 0;
            if (g_net.linked)
                logf("debug: netplay: world sync - %llu objects applied, %llu enemies made, %llu removed so far (%s)",
                     static_cast<unsigned long long>(g_netWorld.applied), static_cast<unsigned long long>(g_netWorld.made),
                     static_cast<unsigned long long>(g_netWorld.removed), g_net.mode == 1 ? "host" : "joining");
            std::string used;
            for (TargetApi &t : g_targetApis)
            {
                if (t.calls[0] || t.calls[1] || t.calls[2])
                {
                    char b[96];
                    std::snprintf(b, sizeof b, " %s %llu/%llu/%llu", t.name, static_cast<unsigned long long>(t.calls[0]),
                                  static_cast<unsigned long long>(t.calls[1]), static_cast<unsigned long long>(t.calls[2]));
                    used += b;
                }
                t.calls[0] = t.calls[1] = t.calls[2] = 0;
            }
            logf("debug: player 2: player APIs used (aimed at player 1 / aimed at player 2 / outside a script run):%s",
                 used.empty() ? " none" : used.c_str());
            g_p2EnemyTurns = g_p1EnemyTurns = g_p2Collisions = 0;
            g_p2.reads = g_p2.stickReads = 0;
            g_p2.buttonsSeen = 0;
        }
    }

    // Enemies go for the nearest Maximo. Their scripts find "the player" by reading the game Player directly: the
    // targeting calls (GetDistance, GetAngleYToTarget, IsFacing, ..., in the API table at 0x1C8F40, slot k's function
    // at +16k+8) take target -1 as *(*0x263540) + 0xA0, his body's position, and PlayerHit hits the Player at
    // 0x263540. The level VM runs each object's script with func_1052B0(vm+0x98, object) (from func_102B70), so an
    // enemy nearer player 2 runs with player 2's blocks swapped in, and they are saved back afterwards: it chases,
    // faces and hits him. Damage from attacks that land by collision already goes to whoever was hit: Player's hit
    // check (func_12EC30, in Player update) reads it from his own body.
    constexpr uint32_t kRunObjectScriptFn = GA(0x001052B0u);


    bool targetingObject(uint32_t type)
    {
        for (const EnemyType &t : kEnemyTypes)
            if (t.id == type)
                return std::strcmp(t.home, "NPC") != 0;
        return false;
    }

    bool bodyPosition(uint32_t body, float out[3])
    {
        const uint32_t actor = body && (body & PS2_RAM_MASK) == body ? rd32(body) : 0u;
        if (!actor || (actor & PS2_RAM_MASK) != actor)
            return false;
        std::memcpy(out, mem(actor + 0xA0u), 12);
        return true;
    }

    // Every object's script runs with the nearer Maximo as "the player": enemies chase and hit him, an item he
    // reaches gives him its ability (the pickups call SetAbility on the Player at 0x263540), and the pieces of armour
    // he loses change him, not player 1. Scripts run from two places: func_1052B0(interpreter, object) in the level
    // VM's object loop, and func_105370(interpreter, event, handle, ...) - the event entry that func_103BC0 ->
    // func_1039D0 and the others use after the loop, where the measured player APIs were all called from.
    constexpr uint32_t kRunObjectEventFn = GA(0x00105370u);

    uint32_t objectForHandle(uint32_t handle)
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        if (!vm || (vm & PS2_RAM_MASK) != vm || !handle)
            return 0;
        for (uint32_t o = rd32(vm + 0x9Cu + 8u), guard = 0; o && (o & PS2_RAM_MASK) == o && guard < 20000;
             o = rd32(o + 0xBCu), ++guard)
            if (rd32(o) == handle)
                return o;
        return 0;
    }

    // Player 2 is the nearer one to this object (by its actor's position).
    bool nearerPlayer2(uint32_t obj)
    {
        if (!g_p2.exists || g_p2.pass || g_turnContext != 0 || !obj || (obj & PS2_RAM_MASK) != obj)
            return false;
        const uint32_t entity = rd32(obj + 0xB8u);
        const uint32_t actor = entity && (entity & PS2_RAM_MASK) == entity ? rd32(entity) : 0u;
        float me[3], p1[3], p2[3];
        if (!actor || (actor & PS2_RAM_MASK) != actor || !bodyPosition(rd32(kGamePlayerAddr), p1) ||
            !bodyPosition(g_p2.body, p2))
            return false;
        std::memcpy(me, mem(actor + 0xA0u), 12);
        auto d2 = [&](const float *p)
        {
            return (p[0] - me[0]) * (p[0] - me[0]) + (p[1] - me[1]) * (p[1] - me[1]) + (p[2] - me[2]) * (p[2] - me[2]);
        };
        return d2(p2) < d2(p1);
    }

    void runScriptForNearest(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t obj)
    {
        const uint32_t outerObject = g_scriptObject;
        g_scriptObject = obj;
        if (!nearerPlayer2(obj))
        {
            if (g_p2.exists)
            {
                ++g_p1EnemyTurns;
                runToReturn(rdram, ctx, runtime); // (so the diagnostics see the whole run)
            }
            else
                g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            g_scriptObject = outerObject;
            return;
        }
        ++g_p2EnemyTurns;
        BlockSwap p1;
        p1.save();
        std::memcpy(mem(kGamePlayerAddr), g_p2.player.data(), kGamePlayerSize);
        std::memcpy(mem(kPlayerAddr), g_p2.controller.data(), kControllerSize);
        g_turnContext = 2;
        runToReturn(rdram, ctx, runtime);
        g_turnContext = 0;
        g_scriptObject = outerObject;
        g_p2.player.assign(mem(kGamePlayerAddr), mem(kGamePlayerAddr) + kGamePlayerSize);
        g_p2.controller.assign(mem(kPlayerAddr), mem(kPlayerAddr) + kControllerSize);
        p1.restore();
    }

    void runObjectScriptEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        runScriptForNearest(rdram, ctx, runtime, reg(ctx, 5));
    }

    void bossEventDiag(uint32_t event, uint32_t handle);
    // Captain Cadaver away from his arena (survival): every hit of Maximo's counts, not only the ones that put him
    // in his hurt animation (a hit on his leg mostly does not). His collision handler is his event 2 here (his
    // class's second event handler: it reads the attack's kind, 9 being his deck's trap); each attack
    // of Maximo's (a sword swing, the shield) that reaches it within 40 frames of starting counts once. The hits
    // are applied by bossHitTick.
    uint32_t g_cadaverWatchHandle = 0;
    int g_cadaverPendingHits = 0;
    uint64_t g_cadaverLastHitAttack = 0, g_cadaverLastEventHitFrame = 0;
    void cadaverEventHit(uint32_t event, uint32_t handle)
    {
        if (!g_cadaverWatchHandle || handle != g_cadaverWatchHandle)
            return;
        const uint64_t attack = std::max(g_lastSwordFrame, g_lastOtherAttackFrame);
        static int logged = 0;
        if (logged < 40)
        {
            ++logged;
            logf("debug: Captain Cadaver event %u (frame %llu, last attack %llu)", event,
                 static_cast<unsigned long long>(g_dbg.padFrames), static_cast<unsigned long long>(attack));
        }
        // (Event 2 in this hook's numbering, his class's second handler: the log showed it with every swing that
        // touched him, hurt animation or not. A burst of the shield's bolts is a new attack every 4 frames: at most
        // one hit counts every 20 frames.)
        if (event != 2u || attack == 0 || g_dbg.padFrames - attack > 40u || attack == g_cadaverLastHitAttack ||
            g_dbg.padFrames - g_cadaverLastEventHitFrame < 20u)
            return;
        g_cadaverLastHitAttack = attack;
        g_cadaverLastEventHitFrame = g_dbg.padFrames;
        ++g_cadaverPendingHits;
    }
    void runObjectEventEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        bossEventDiag(reg(ctx, 5), reg(ctx, 6));
        cadaverEventHit(reg(ctx, 5), reg(ctx, 6));
        runScriptForNearest(rdram, ctx, runtime, g_p2.exists ? objectForHandle(reg(ctx, 6)) : 0u);
    }

    // Collisions: func_14C7D0(body A, body B) answers two world objects touching (from func_14C5B0). When one is
    // marked as the player (body+0xA4 negative - player 2's body is made the same way, so it has the mark too) it
    // calls Maximo's own hit code, func_1363F0 / func_136B50, on the Player at 0x263540: player 1, whichever body was
    // hit. So a collision with player 2's body runs with player 2's blocks swapped in (and saved back afterwards):
    // his hits, knockback and pickups go to him.
    constexpr uint32_t kBodyCollisionFn = GA(0x0014C7D0u);
    // Hits land on more than the body: the Player block points at ~25 more of Maximo's world objects (+0x118,
    // +0x2C0..+0x314 - his armour and model pieces -, +0x348, +0x37C..+0x390). One of player 2's is any object his
    // block points at that player 1's does not.
    bool isPlayer2Object(uint32_t obj)
    {
        if (!obj || g_p2.player.size() < kGamePlayerSize)
            return false;
        if (obj == g_p2.body)
            return true;
        for (uint32_t off = 0; off < kGamePlayerSize; off += 4)
        {
            uint32_t v;
            std::memcpy(&v, g_p2.player.data() + off, 4);
            if (v == obj)
                return rd32(kGamePlayerAddr + off) != obj; // not shared with player 1
        }
        return false;
    }

    // Captain Cadaver outside his own arena: his fight only ends through his arena's trap (the deck sections his
    // script opens with AnimateLandTo, part of that arena's floor), so anywhere else he could not die. There he
    // counts Maximo's hits instead: a collision of his body with any of Maximo's objects (body, sword, shield - the
    // world objects the Player block points at) while an attack of Maximo's is on (its SetDamage within 40 frames),
    // one hit per attack. bossHitTick turns the hits into his health bar and his death.
    uint32_t g_hitCountedEntity = 0;
    int g_hitCountedPending = 0;
    uint64_t g_hitCountedLast = 0;
    bool isPlayer1Object(uint32_t obj)
    {
        if (!obj)
            return false;
        for (uint32_t off = 0; off < 0x400u; off += 4u)
            if (rd32(kGamePlayerAddr + off) == obj)
                return true;
        return false;
    }
    void bodyCollisionEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t a = reg(ctx, 4), b = reg(ctx, 5);
        // One hit per attack of Maximo's (each swing or throw gives its weapon damage with SetDamage once), and
        // only by his weapons: touching his body is not a hit (counted that way, Captain Cadaver lost all 12 within
        // seconds of appearing).
        const uint64_t attack = std::max(g_lastSwordFrame, g_lastOtherAttackFrame);
        const uint32_t other = a == g_hitCountedEntity ? b : a;
        if (g_hitCountedEntity && (a == g_hitCountedEntity || b == g_hitCountedEntity) && attack > g_hitCountedLast &&
            g_dbg.padFrames - attack < 40u && other != rd32(kGamePlayerAddr) && isPlayer1Object(other))
        {
            ++g_hitCountedPending;
            g_hitCountedLast = attack;
        }
        const uint32_t outerA = g_collisionA, outerB = g_collisionB;
        g_collisionA = a;
        g_collisionB = b;
        if (!g_p2.exists || g_p2.pass || !g_p2.body || (!isPlayer2Object(a) && !isPlayer2Object(b)) || g_turnContext != 0)
        {
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            g_collisionA = outerA;
            g_collisionB = outerB;
            return;
        }
        ++g_p2Collisions;
        BlockSwap p1;
        p1.save();
        std::memcpy(mem(kGamePlayerAddr), g_p2.player.data(), kGamePlayerSize);
        std::memcpy(mem(kPlayerAddr), g_p2.controller.data(), kControllerSize);
        g_turnContext = 3;
        runToReturn(rdram, ctx, runtime);
        g_turnContext = 0;
        g_collisionA = outerA;
        g_collisionB = outerB;
        g_p2.player.assign(mem(kGamePlayerAddr), mem(kGamePlayerAddr) + kGamePlayerSize);
        g_p2.controller.assign(mem(kPlayerAddr), mem(kPlayerAddr) + kControllerSize);
        p1.restore();
    }

    // Diagnostics: the targeting calls enemy scripts make (API slots 99 GetDistance, 116 GetDistanceSquared,
    // 79 GetAngleYToTarget, 90 GetRelAngleYToTarget, 67 IsFacing; the API's context: own handle +0xBEC, first
    // argument - the target, -1 the player - at +0xBF0), counted per target kind while aimed at player 2.
    void targetApiEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_p2.exists)
        {
            const int32_t target = static_cast<int32_t>(rd32(reg(ctx, 4) + 0xBF0u));
            const int who = !g_scriptObject ? 2 : g_turnContext == 2 ? 1 : 0;
            for (TargetApi &t : g_targetApis)
                if (t.fn == ctx->pc)
                {
                    ++t.calls[who];
                    break;
                }
            if (target == -1 || target == -2)
                ++g_apiPlayer[who == 1 ? 1 : 0];
            else
                ++g_apiOther[who == 1 ? 1 : 0];
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    struct DebugOverlay
    {
        std::vector<std::string> lines;
        int highlight = -1; // index into lines, or -1
        bool panel = false; // draw the menu box (else a small status line)
        std::string banner; // survival's line, drawn on its own at the top middle
        float bannerSize = 1.0f; // (survival's lines are drawn smaller)
        std::string timer;  // survival: how long the run has gone on, top left
    };
    std::mutex g_dbgOverlayMutex;
    DebugOverlay g_dbgOverlay;

    enum DebugItem
    {
        kItemFreeCam,
        kItemPauseCam,
        kItemMoveMaximo,
        kItemFirstPerson,
        kItemSpeed,
        kItemZoom,
        kItemInvertX,
        kItemInvertY,
        kItemPosition,
        kItemNoHud,
        kItemGod,
        kItemNoClip,
        kItemInfinite,
        kItemOneHit,
        kItemHealthBars,
        kItemGameSpeed,
        kItemGiveAbility,
        kItemKillAll,
        kItemSurvival,
        kItemLevel,
        kItemSpawnType,
        kItemEnemyClass,
        kItemSpawnAt,
        kItemSpawnItem,
        kItemSpawnSkill,
        kItemSpawnSword,
        kItemSpawnStruct,
        kItemSpawnCollector,
        kItemSpawnAny,
        kItemSpawnCount,
        kItemClose,
        kItemCount,
        // Taken off the menu (their code stays): release Maximo, player 2, netplay, its controls, split view, the RAM
        // snapshot and the performance profile.
        kItemSnapshot,
        kItemProfile,
        kItemUnfreeze,
        kItemPlayer2,
        kItemNetplay,
        kItemControls,
        kItemViewLag,
    };

    // What the debug options put on screen outside the menu (the free camera's line, the position) shows for 3
    // seconds after the menu closes or the free camera's pause changes, then goes.
    std::chrono::steady_clock::time_point g_hintsUntil{};
    void showHints() { g_hintsUntil = std::chrono::steady_clock::now() + std::chrono::seconds(3); }
    bool hintsShowing() { return std::chrono::steady_clock::now() < g_hintsUntil; }

    void publishDebugOverlay()
    {
        DebugOverlay o;
        static const char *kSpeeds[] = {"SLOW", "NORMAL", "FAST"};
        char b[96];
        if (g_dbg.menuOpen)
        {
            o.panel = true;
            o.lines.push_back("DEBUG MENU");
            o.lines.push_back("");
            std::snprintf(b, sizeof b, "FREE CAMERA: %s", g_dbg.freeCam ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "PAUSE GAME IN FREE CAMERA: %s", g_dbg.pauseInFreeCam ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "MOVE MAXIMO IN FREE CAMERA: %s%s", g_dbg.moveMaximo ? "ON" : "OFF",
                          g_dbg.moveMaximo ? "  (D-PAD: CAMERA, STICK: MAXIMO, R3: PAUSE)" : "");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "FIRST PERSON CAMERA: %s%s", g_dbg.firstPerson ? "ON" : "OFF",
                          g_dbg.firstPerson ? "  (RIGHT STICK: LOOK AROUND)" : "");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "CAMERA SPEED: %s", kSpeeds[g_dbg.speed]);
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "FREE CAMERA ZOOM: %.1fX  (R1 IN, L1 OUT WHILE FLYING)", g_dbg.zoom);
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "INVERT LOOK X: %s", g_dbg.invertX ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "INVERT LOOK Y: %s", g_dbg.invertY ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "SHOW POSITION: %s", g_dbg.showPosition ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "HIDE HUD: %s", g_dbg.noHud ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "GOD MODE: %s", g_godMode ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "NO CLIP: %s  (STICK MOVES, R2 UP, L2 DOWN)", g_noClip ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "INFINITE LIVES / KOINS / KEYS: %s", g_dbg.infinite ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "ONE HIT KILL: %s", g_dbg.oneHitKill ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "ENEMY HEALTH BARS: %s", g_healthBars ? "ON" : "OFF");
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "GAME SPEED: %s", kGameSpeedNames[std::clamp(g_dbg.gameSpeed, 0, 4)]);
            o.lines.push_back(b);
            std::snprintf(b, sizeof b, "GIVE ABILITY: < %s >  (%d OF %d)", kGiveAbilities[g_dbg.givePick],
                          g_dbg.givePick + 1, static_cast<int>(std::size(kGiveAbilities)));
            o.lines.push_back(b);
            o.lines.push_back(g_dbg.inLevel ? "DESPAWN ALL ENEMIES (CROSS)" : "DESPAWN ALL ENEMIES: (ONLY IN A LEVEL)");
            if (g_survival.active)
                std::snprintf(b, sizeof b, "SURVIVAL MODE: WAVE %d  (CROSS: STOP)", g_survival.wave);
            else
                std::snprintf(b, sizeof b, "SURVIVAL MODE: < %s >  CROSS TO START  (BEST: WAVE %d)",
                              g_survivalHere ? "THIS LEVEL (PURPLE RING)" : "BOSS ARENA", g_survival.best);
            o.lines.push_back(b);
            if (!g_dbg.inLevel)
                std::snprintf(b, sizeof b, "GO TO LEVEL: (ONLY IN A LEVEL)");
            else
                std::snprintf(b, sizeof b, "GO TO LEVEL: < %s (%s) >  (%d OF 31)", kLevelNames[g_dbg.levelPick],
                              kLevelFiles[g_dbg.levelPick], g_dbg.levelPick + 1);
            o.lines.push_back(b);
            if (!g_dbg.inLevel)
                std::snprintf(b, sizeof b, "SPAWN ENEMY: (ONLY IN A LEVEL)");
            else if (g_dbg.spawnable.empty())
                std::snprintf(b, sizeof b, "SPAWN ENEMY: (NONE LOADED HERE)");
            else
            {
                const EnemyType &t = kEnemyTypes[g_dbg.spawnable[g_dbg.spawnPick]];
                std::snprintf(b, sizeof b, "SPAWN ENEMY: < %s%s%s%s >  (%d OF %d)", t.name, *t.home ? " (" : "", t.home,
                              *t.home ? ")" : "", g_dbg.spawnPick + 1, static_cast<int>(g_dbg.spawnable.size()));
            }
            o.lines.push_back(b);
            {
                // Which classes the picked enemy has (if any), so the line says what CLASS n gives it.
                const char *has = "";
                if (g_dbg.inLevel && !g_dbg.spawnable.empty())
                {
                    const uint32_t id = kEnemyTypes[g_dbg.spawnable[g_dbg.spawnPick]].id;
                    has = "  (THIS ONE HAS NO CLASSES)";
                    for (const EnemyClassInfo &c : kEnemyClasses)
                        if (c.id == id)
                            has = c.count == 3 ? "  (THIS ONE: CLASS 1-3)"
                                  : c.propType == 1u ? "  (THIS ONE: 1 POLTERGEIST, 2 BLUE GHOST)"
                                                     : "  (THIS ONE: CLASS 1-2)";
                }
                std::snprintf(b, sizeof b, "ENEMY CLASS: %s%s", kEnemyClassNames[std::clamp(g_dbg.enemyClass, 0, 3)],
                              has);
                o.lines.push_back(b);
            }
            o.lines.push_back(g_dbg.spawnAtCamera ? "SPAWN AT: THE CAMERA (12 UNITS AHEAD)"
                                                  : "SPAWN AT: MAXIMO (FREE CAMERA: IN FRONT OF IT)");
            // The three pickup lines: "ALL" first, then each one.
            auto pickupLine = [&](const char *label, bool have, int pick, int n, const char *one, const char *all)
            {
                if (!g_dbg.inLevel)
                    std::snprintf(b, sizeof b, "%s: (ONLY IN A LEVEL)", label);
                else if (!have || n == 0)
                    std::snprintf(b, sizeof b, "%s: (NOT LOADED HERE)", label);
                else
                    std::snprintf(b, sizeof b, "%s: < %s >  (%d OF %d)", label, pick == 0 ? all : one, pick + 1, n + 1);
                o.lines.push_back(b);
            };
            {
                const int n = static_cast<int>(g_dbg.items.size());
                const int pick = std::min(g_dbg.itemPick, n);
                pickupLine("SPAWN ITEM", n > 0, pick, n, pick > 0 ? kItemTypes[g_dbg.items[pick - 1]].name : "",
                           "ALL ITEMS");
            }
            pickupLine("SPAWN SKILL", g_dbg.haveAbility, g_dbg.skillPick, static_cast<int>(std::size(kSkills)),
                       g_dbg.skillPick > 0 ? kSkills[g_dbg.skillPick - 1].name : "", "ALL SKILLS");
            pickupLine("SPAWN SWORD POWER", g_dbg.haveAbility, g_dbg.swordPick,
                       static_cast<int>(std::size(kSwordPowers)),
                       g_dbg.swordPick > 0 ? kSwordPowers[g_dbg.swordPick - 1].name : "", "ALL SWORD POWERS");
            {
                const int n = static_cast<int>(g_dbg.structs.size());
                const int pick = std::min(g_dbg.structPick, n);
                pickupLine("SPAWN STRUCTURE", n > 0, pick, n, pick > 0 ? kStructures[g_dbg.structs[pick - 1]].name : "",
                           "ALL STRUCTURES");
            }
            {
                char one[32];
                const int pick = g_dbg.collectorPick;
                std::snprintf(one, sizeof one, "STOCK %d%s", pick - 1, pick - 1 == 5 ? " (MYSTERY)" : "");
                pickupLine("SPAWN COLLECTOR (SHOP)", g_dbg.haveCollector, pick, kCollectorStocks, one, "ALL STOCKS");
            }
            {
                const int n = static_cast<int>(g_dbg.anyClasses.size());
                if (!g_dbg.inLevel)
                    std::snprintf(b, sizeof b, "SPAWN ANY CLASS: (ONLY IN A LEVEL)");
                else if (n == 0)
                    std::snprintf(b, sizeof b, "SPAWN ANY CLASS: (NOTHING LOADED)");
                else
                {
                    const int pick = std::clamp(g_dbg.anyPick, 0, n - 1);
                    std::snprintf(b, sizeof b, "SPAWN ANY CLASS: < 0x%02X %s >  (%d OF %d)", g_dbg.anyClasses[pick],
                                  classNameFor(g_dbg.anyClasses[pick]), pick + 1, n);
                }
                o.lines.push_back(b);
            }
            std::snprintf(b, sizeof b, "SPAWN COUNT: %d", kSpawnCounts[g_dbg.spawnCount]);
            o.lines.push_back(b);
            o.lines.push_back("CLOSE");
            o.highlight = 2 + g_dbg.selected;
            o.lines.push_back("");
            o.lines.push_back("D-PAD: CHOOSE   LEFT/RIGHT: CHANGE   CROSS: SPAWN / GO TO LEVEL");
            if (!g_dbg.spawnMessage.empty())
                o.lines.push_back(g_dbg.spawnMessage);
            o.lines.push_back("CIRCLE OR L1+R1+SELECT: CLOSE");
            if (g_dbg.freeCam)
                o.lines.push_back("FREE CAM: L-STICK FLY  R-STICK LOOK  L2/R2 DOWN/UP  CROSS FAST  TRIANGLE PAUSE");
        }
        else if (g_dbg.freeCam && hintsShowing())
        {
            const char *key = g_dbg.moveMaximo ? "R3" : "TRIANGLE";
            std::snprintf(b, sizeof b, g_dbg.pauseInFreeCam ? "FREE CAMERA - GAME PAUSED  (%s: RESUME, L1+R1+SELECT: MENU)"
                                                            : "FREE CAMERA  (%s: PAUSE, L1+R1+SELECT: MENU)", key);
            o.lines.push_back(b);
        }
        if (g_survival.active && !g_dbg.menuOpen)
        {
            o.bannerSize = kSurvivalHudSize;
            const uint32_t t = static_cast<uint32_t>(g_survival.seconds);
            if (t >= 3600u)
                std::snprintf(b, sizeof b, "TIME %u:%02u:%02u", t / 3600u, t / 60u % 60u, t % 60u);
            else
                std::snprintf(b, sizeof b, "TIME %u:%02u", t / 60u, t % 60u);
            o.timer = b;
        }
        if (!g_dbg.menuOpen && g_survival.active && g_survival.phase < 3)
            o.banner = g_survival.phase == 1 ? "SURVIVAL: GOING TO THE ARENA" : "SURVIVAL: CLEARING THE ARENA";
        else if (!g_dbg.menuOpen && g_survival.active)
        {
            int left = 0;
            for (const SurvivalFoe &f : g_survival.foes)
                if (rd32(f.obj) == f.handle && rd32(f.obj + 0xB4u) == f.tmpl)
                    ++left;
            if (g_survival.nextWaveAt)
            {
                const int secs = static_cast<int>(
                    (g_survival.nextWaveAt - std::min(g_survival.nextWaveAt, g_dbg.padFrames) + 59u) / 60u);
                std::snprintf(b, sizeof b, "WAVE %d IN %d   KILLS %d   BEST %d", g_survival.wave + 1, secs,
                              g_survival.kills, g_survival.best);
            }
            else
                std::snprintf(b, sizeof b, "WAVE %d   ENEMIES LEFT %d   KILLS %d   BEST %d", g_survival.wave,
                              left, g_survival.kills, g_survival.best);
            o.banner = b;
        }
        else if (!g_dbg.menuOpen && !g_survival.result.empty())
        {
            o.banner = g_survival.result;
            o.bannerSize = kSurvivalHudSize;
        }
        if (g_dbg.showPosition && (g_dbg.menuOpen || hintsShowing()))
        {
            const float *e = g_dbg.freeCam && g_dbg.freeCamReady ? g_dbg.eye : g_dbg.gameEye;
            std::snprintf(b, sizeof b, "%s X %.1f  Y %.1f  Z %.1f", g_dbg.freeCam ? "FREE CAM" : "CAMERA", e[0], e[1],
                          e[2]);
            o.lines.push_back(b);
        }
        std::lock_guard<std::mutex> lock(g_dbgOverlayMutex);
        g_dbgOverlay = std::move(o);
    }

    void normalize3(float *v)
    {
        const float len = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
        if (len > 1e-6f)
            for (int i = 0; i < 3; ++i)
                v[i] /= len;
    }

    // Start the free camera where the game's camera is.
    void startFreeCamera()
    {
        g_dbg.freeCamReady = false;
        if (!g_dbg.haveGameCamera)
            return;
        float d[3] = {g_dbg.gameTarget[0] - g_dbg.gameEye[0], g_dbg.gameTarget[1] - g_dbg.gameEye[1],
                      g_dbg.gameTarget[2] - g_dbg.gameEye[2]};
        normalize3(d);
        if (d[0] == 0 && d[1] == 0 && d[2] == 0)
            d[2] = 1.0f;
        // The game's camera hangs above Maximo looking down at him, so the axis it looks down along is world down.
        g_dbg.up = d[1] <= 0.0f ? 1.0f : -1.0f;
        std::memcpy(g_dbg.eye, g_dbg.gameEye, sizeof g_dbg.eye);
        std::memcpy(g_dbg.dir, d, sizeof g_dbg.dir);
        g_dbg.freeCamReady = true;
        logf("debug: free camera from eye (%.1f, %.1f, %.1f), looking (%.2f, %.2f, %.2f), world up %+.0f Y",
             g_dbg.eye[0], g_dbg.eye[1], g_dbg.eye[2], d[0], d[1], d[2], g_dbg.up);
    }

    float stickAxis(uint8_t v)
    {
        const float a = (static_cast<float>(v) - 128.0f) / 127.0f;
        return std::fabs(a) < 0.18f ? 0.0f : std::clamp(a, -1.0f, 1.0f);
    }

    // Once per guest frame while the free camera is on and the menu is closed.
    void moveFreeCamera()
    {
        const float up[3] = {0.0f, g_dbg.up, 0.0f};
        // Heading (dir flattened) and the right of it: cross(heading, up).
        float h[3] = {g_dbg.dir[0], 0.0f, g_dbg.dir[2]};
        normalize3(h);
        float right[3] = {h[1] * up[2] - h[2] * up[1], h[2] * up[0] - h[0] * up[2], h[0] * up[1] - h[1] * up[0]};
        normalize3(right);
        float pitch = std::asin(std::clamp(g_dbg.dir[1] * g_dbg.up, -1.0f, 1.0f));
        // Look: right stick.
        // (per frame at 60 Hz; above that each frame moves its share)
        const float perFrame = g_fieldRateHz > 60u ? 60.0f / static_cast<float>(g_fieldRateHz) : 1.0f;
        const float kLook = 2.5f * 3.14159265f / 180.0f * perFrame; // radians per frame at full stick
        const float yaw = stickAxis(g_dbg.rx) * kLook * (g_dbg.invertX ? -1.0f : 1.0f);
        pitch += -stickAxis(g_dbg.ry) * kLook * (g_dbg.invertY ? -1.0f : 1.0f);
        pitch = std::clamp(pitch, -1.48f, 1.48f);
        if (yaw != 0.0f)
        {
            const float c = std::cos(yaw), s = std::sin(yaw);
            for (int i = 0; i < 3; ++i)
                h[i] = h[i] * c + right[i] * s;
            normalize3(h);
            right[0] = h[1] * up[2] - h[2] * up[1];
            right[1] = h[2] * up[0] - h[0] * up[2];
            right[2] = h[0] * up[1] - h[1] * up[0];
            normalize3(right);
        }
        for (int i = 0; i < 3; ++i)
            g_dbg.dir[i] = h[i] * std::cos(pitch) + up[i] * std::sin(pitch);
        normalize3(g_dbg.dir);
        // Fly: left stick along the view and to the side, L2/R2 straight down/up.
        static const float kSpeeds[] = {0.15f, 0.5f, 1.5f};
        const float speed = kSpeeds[g_dbg.speed] * ((g_dbg.held & kBtnCross) && !g_dbg.moveMaximo ? 4.0f : 1.0f) * perFrame;
        float fwd = -stickAxis(g_dbg.ly) * speed;
        float side = stickAxis(g_dbg.lx) * speed * (g_dbg.invertX ? -1.0f : 1.0f);
        if (g_dbg.moveMaximo)
        {
            // MOVE MAXIMO IN FREE CAMERA: the left stick is Maximo's; the d-pad flies the camera instead.
            fwd = (((g_dbg.held & kBtnUp) ? 1.0f : 0.0f) - ((g_dbg.held & kBtnDown) ? 1.0f : 0.0f)) * speed;
            side = (((g_dbg.held & kBtnRight) ? 1.0f : 0.0f) - ((g_dbg.held & kBtnLeft) ? 1.0f : 0.0f)) * speed *
                   (g_dbg.invertX ? -1.0f : 1.0f);
        }
        const float lift = ((g_dbg.held & kBtnR2) ? speed : 0.0f) - ((g_dbg.held & kBtnL2) ? speed : 0.0f);
        for (int i = 0; i < 3; ++i)
            g_dbg.eye[i] += g_dbg.dir[i] * fwd + right[i] * side + up[i] * lift;
    }

    // The enemy types the current world has loaded, from the game's class table.
    void refreshSpawnable(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        g_dbg.inLevel = vm != 0 && (vm & PS2_RAM_MASK) == vm;
        g_dbg.spawnable.clear();
        g_dbg.items.clear();
        g_dbg.structs.clear();
        g_dbg.haveAbility = false;
        g_dbg.haveCollector = false;
        g_dbg.anyClasses.clear();
        if (!g_dbg.inLevel)
            return;
        for (uint32_t id = 1; id < 0x100u; ++id)
        {
            uint32_t cls = 0;
            if (callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, id, 0, cls) && cls)
                g_dbg.anyClasses.push_back(id);
        }
        g_dbg.anyPick = std::clamp(g_dbg.anyPick, 0, std::max(0, static_cast<int>(g_dbg.anyClasses.size()) - 1));
        logf("debug: %zu classes loaded here", g_dbg.anyClasses.size());
        {
            uint32_t cls = 0;
            g_dbg.haveCollector = callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, kCollectorType, 0, cls) && cls;
        }
        for (int i = 0; i < static_cast<int>(std::size(kStructures)); ++i)
        {
            uint32_t cls = 0;
            if (callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, kStructures[i].id, 0, cls) && cls != 0)
                g_dbg.structs.push_back(i);
        }
        g_dbg.structPick = std::clamp(g_dbg.structPick, 0, static_cast<int>(g_dbg.structs.size()));
        {
            std::string have;
            for (int i : g_dbg.structs)
                have += std::string(have.empty() ? "" : ", ") + kStructures[i].name;
            logf("debug: structures here: %s", have.empty() ? "(none)" : have.c_str());
        }
        for (int i = 0; i < static_cast<int>(std::size(kItemTypes)); ++i)
        {
            uint32_t cls = 0;
            if (callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, kItemTypes[i].id, 0, cls) && cls != 0)
                g_dbg.items.push_back(i);
        }
        g_dbg.itemPick = std::clamp(g_dbg.itemPick, 0, static_cast<int>(g_dbg.items.size()));
        {
            uint32_t cls = 0;
            g_dbg.haveAbility = callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, kAbilityType, 0, cls) && cls;
        }
        {
            std::string have;
            for (int i : g_dbg.items)
                have += std::string(have.empty() ? "" : ", ") + kItemTypes[i].name;
            logf("debug: items here: %s; ability pickups %s", have.empty() ? "(none)" : have.c_str(),
                 g_dbg.haveAbility ? "loaded" : "not loaded");
        }
        for (int i = 0; i < static_cast<int>(std::size(kEnemyTypes)); ++i)
        {
            uint32_t cls = 0;
            if (callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, kEnemyTypes[i].id, 0, cls) && cls != 0)
                g_dbg.spawnable.push_back(i);
        }
        g_dbg.spawnPick = std::clamp(g_dbg.spawnPick, 0, std::max(0, static_cast<int>(g_dbg.spawnable.size()) - 1));
        std::string names;
        for (int i : g_dbg.spawnable)
            names += std::string(names.empty() ? "" : ", ") + kEnemyTypes[i].name;
        logf("debug: spawnable here: %s", names.empty() ? "(none)" : names.c_str());
    }

    // One enemy of class `tmpl`, made the way the level loader (func_00102480) makes a placed one, and put at `pos`.
    // The loader: alloc, ctor, init, register; variables from the record (func_00103CB0); the spawn block at
    // obj+0xF8 (func_00103EA0: properties array, count, position, rotation), which the enemy's script reads when it
    // first runs; activate (func_00103C30, script start); func_00101820; then the world entity at that position and
    // rotation (func_00105630, t1=1). func_001028D0 skips the spawn block and passes zero vectors, so an enemy made
    // with it has nothing to stand on and its script ends at once. With no level record here, the variables and
    // properties are copied from a live enemy of the same class. Returns the handle (0: failed, `why` says where).
    struct SpawnedObject
    {
        uint32_t obj, handle, tmpl;
    };
    std::vector<SpawnedObject> g_spawnedObjects; // everything the debug menu made (DESPAWN ALL ENEMIES)
    bool g_spawnedThisLevel = false;
    // How each spawn was made (by its handle), so a spawn that fell asleep can be made again.
    struct SpawnRecipe
    {
        uint32_t tmpl = 0;
        std::vector<RecordProp> props;
        RecordProp classProp{};
        bool hasProps = false, hasClass = false;
    };
    std::map<uint32_t, SpawnRecipe> g_spawnRecipes;
    struct SleepingSpawn
    {
        SpawnRecipe recipe;
        float pos[3];
    };
    std::vector<SleepingSpawn> g_sleepingSpawns;

    // Spawned objects stay: func_00102D30(vm, handle) is the level's "this object left the active area" step (from
    // func_0014F2A0). A level's own object (the placed mark, obj+0xF0) is reset and put to sleep, to come back when
    // Maximo returns; anything else is destroyed - so whatever the debug menu spawned (pickups, and enemies not copied
    // from a level one) vanished for good once Maximo walked away from where it was made. For those it is skipped.
    constexpr uint32_t kDisappearFn = GA(0x00102D30u);
    // ...and before it, the world side: func_0014F2A0(world object, 1), from the distance check at 0x183234 (its
    // partner at 0x1831AC wakes objects up again with 0), deletes the object's body and leaves an empty stand-in in
    // its place, then calls the step above. For a spawned object that whole call is skipped (handle at +0xA4).
    constexpr uint32_t kWorldSleepFn = GA(0x0014F2A0u), kWorldSleepRa = GA(0x0018323Cu);
    // Bosses spawned from the menu: GraveDigger, BokorLaBas, Captain Cadaver, Lord Glutterscum, King, Queen.
    constexpr uint32_t kBossTypes[] = {0x29u, 0x2Eu, 0x82u, 0x8Eu, 0x9Cu, 0x9Du};
    struct DebugBoss
    {
        uint32_t obj, handle;
        uint64_t born;
        uint64_t removeAt; // 0 until it dies
    };
    std::vector<DebugBoss> g_debugBosses;

    // Diagnostics for GraveDigger in survival (he could not be stunned): his script stuns him (state 12) when a hit
    // of Maximo's (event 2) lands while he is in state 11 with his member 0x17 set. Each event he gets is logged with
    // his state (60 lines at most).
    void bossEventDiag(uint32_t event, uint32_t handle)
    {
        if (!g_survival.active || !handle)
            return;
        static int logged = 0;
        if (logged >= 60)
            return;
        for (const DebugBoss &b : g_debugBosses)
            if (b.handle == handle && rd32(b.obj) == handle)
            {
                const uint32_t cls = rd32(b.obj + 0xB4u);
                const uint32_t type = cls && (cls & PS2_RAM_MASK) == cls ? rd32(cls) : 0u;
                if (type != 0x29u)
                    return;
                ++logged;
                logf("debug: GraveDigger event %u (frame %llu, last sword %llu)", event,
                     static_cast<unsigned long long>(g_dbg.padFrames), static_cast<unsigned long long>(g_lastSwordFrame));
                return;
            }
    }
    bool isSpawnedHandle(uint32_t handle)
    {
        for (const SpawnedObject &o : g_spawnedObjects)
            if (o.handle == handle && rd32(o.obj) == handle)
                return true;
        return false;
    }
    // The distance check itself (func_001830C0, per world object): too far from the viewer, it puts the object to
    // sleep (func_14F2A0 above) AND takes it off the world's update/draw list (func_18BC30(0x262530, object)) - the
    // second is what made spawns vanish. It skips both for an object whose VM state (func_00103030(vm, handle),
    // asked from 0x1831D4) is 1, so for a spawned object that call answers 1 there.
    constexpr uint32_t kObjectStateFn = GA(0x00103030u), kObjectStateDistanceRa = GA(0x001831DCu);
    void objectStateEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        // Only bosses (the menu's, and survival's arena) are kept awake; every other spawn sleeps and wakes at the
        // game's own distances, as a level object does (it carries the placed mark, see spawnOne).
        const uint32_t handle = reg(ctx, 5);
        if (reg(ctx, 31) == kObjectStateDistanceRa && isSpawnedHandle(handle) &&
            (g_survival.active || std::any_of(g_debugBosses.begin(), g_debugBosses.end(),
                                              [&](const DebugBoss &b) { return b.handle == handle; })))
        {
            ret(ctx, 1); // stays in the world
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }
    // The boss meter's model (interface 0x25B9E0 + 0x7788, "boss_bar_whole") has five parts (boss_bar_color, _back,
    // _base, _cover, _red) that are drawn on their own: switching the whole off (func_00145F80, func_0018DCB0 does not
    // go down into them) left the bar on screen. The parts are switched off too, and back on when the game shows the
    // meter again for the next boss.
    bool g_bossPartsHidden = false;
    void setModelPartsVisible(uint32_t model, uint32_t visible, int depth = 0)
    {
        if (!model || (model & PS2_RAM_MASK) != model || depth > 6)
            return;
        const uint32_t n = rd32(model + 0x30u), parts = rd32(model + 0x38u);
        if (n > 64u || !parts || (parts & PS2_RAM_MASK) != parts)
            return;
        for (uint32_t i = 0; i < n; ++i)
        {
            const uint32_t part = rd32(parts + 4u * i);
            if (!part || (part & PS2_RAM_MASK) != part)
                continue;
            wr32(part + 0x28u, visible);
            setModelPartsVisible(part, visible, depth + 1);
        }
    }
    void bossMeterTick()
    {
        if (!g_bossPartsHidden || !g_dbg.inLevel)
            return;
        const uint32_t model = rd32(kWorldAddr + 0x7788u);
        if (model && (model & PS2_RAM_MASK) == model && rd32(model + 0x28u) != 0u)
        {
            setModelPartsVisible(model, 1u);
            g_bossPartsHidden = false;
            logf("debug: boss health bar shown again (its parts back on)");
        }
    }
    bool g_bossBarCleanup = false; // a tracked boss left the level with no other alive: take the health bar off
    // Boss bar research: one RAM snapshot 3 s after the first boss of the session is spawned (bar showing) and one
    // 2 s after it is cleared (bar wrongly still showing), to compare.
    uint64_t g_bossSnapAliveAt = 0, g_bossSnapDeadAt = 0;
    bool g_bossSnapAliveDone = false, g_bossSnapDeadDone = false;

    // Survival bosses have a third of their life. A boss counts its hits its own way (the sword's damage number did
    // nothing to them), so it is done on its health itself: a script variable of the boss (a small whole number)
    // that drops within half a second of Maximo's sword, after being still, is its health - that drop is taken twice
    // more. Once found, only that variable is watched for that boss.
    struct BossHealthWatch
    {
        uint32_t obj, handle, words;
        uint32_t prev[48];
        uint64_t still[48];
        int index = -1;
    };
    std::vector<BossHealthWatch> g_bossHealth;
    void watchSurvivalBoss(uint32_t obj, uint32_t handle)
    {
        BossHealthWatch w{obj, handle, 0, {}, {}, -1};
        const uint32_t vars = rd32(obj + 0x84u), bytes = rd32(rd32(obj + 0xB4u) + 4u);
        if (vars && (vars & PS2_RAM_MASK) == vars && bytes < 0x400u)
            w.words = std::min(bytes / 4u, 48u);
        for (uint32_t i = 0; i < w.words; ++i)
        {
            w.prev[i] = rd32(vars + 4u * i);
            w.still[i] = g_dbg.padFrames;
        }
        g_bossHealth.push_back(w);
    }
    void survivalBossHealthTick()
    {
        const uint64_t now = g_dbg.padFrames;
        for (size_t k = 0; k < g_bossHealth.size();)
        {
            BossHealthWatch &w = g_bossHealth[k];
            const uint32_t vars = rd32(w.obj) == w.handle ? rd32(w.obj + 0x84u) : 0u;
            if (!vars || (vars & PS2_RAM_MASK) != vars)
            {
                g_bossHealth.erase(g_bossHealth.begin() + static_cast<std::ptrdiff_t>(k));
                continue;
            }
            for (uint32_t i = 0; i < w.words; ++i)
            {
                int32_t cur = static_cast<int32_t>(rd32(vars + 4u * i));
                const int32_t before = static_cast<int32_t>(w.prev[i]);
                if (cur == before)
                    continue;
                if (cur < before && before > 0 && before <= 1000 && cur >= 0 && now - g_lastSwordFrame < 30u &&
                    (w.index == static_cast<int>(i) || (w.index < 0 && now - w.still[i] > 10u)))
                {
                    const int32_t lowered = std::max(0, cur - 3 * (before - cur)); // each hit counts 4 times
                    wr32(vars + 4u * i, static_cast<uint32_t>(lowered));
                    if (w.index < 0)
                        logf("debug: survival boss (object 0x%07X): its health is variable %u (%d -> %d); every hit now "
                             "takes 4x", w.obj, i, before, cur);
                    w.index = static_cast<int>(i);
                    cur = lowered;
                }
                w.prev[i] = static_cast<uint32_t>(cur);
                w.still[i] = now;
            }
            ++k;
        }
    }

    // Where spawns go. SPAWN AT MAXIMO: a few steps past what the game camera looks at (him); with the free camera
    // on, in front of it as before. SPAWN AT CAMERA: 12 units in front of whichever camera is showing, free or game.
    void spawnAnchor(float at[3])
    {
        const bool free = g_dbg.freeCam && g_dbg.freeCamReady;
        if (free)
        {
            for (int i = 0; i < 3; ++i)
                at[i] = g_dbg.eye[i] + g_dbg.dir[i] * 12.0f;
            return;
        }
        if (g_dbg.spawnAtCamera)
        {
            float d[3] = {g_dbg.gameTarget[0] - g_dbg.gameEye[0], g_dbg.gameTarget[1] - g_dbg.gameEye[1],
                          g_dbg.gameTarget[2] - g_dbg.gameEye[2]};
            normalize3(d);
            for (int i = 0; i < 3; ++i)
                at[i] = g_dbg.gameEye[i] + d[i] * 12.0f;
            return;
        }
        float h[3] = {g_dbg.gameTarget[0] - g_dbg.gameEye[0], 0.0f, g_dbg.gameTarget[2] - g_dbg.gameEye[2]};
        normalize3(h);
        for (int i = 0; i < 3; ++i)
            at[i] = g_dbg.gameTarget[i] + h[i] * 6.0f;
    }

    struct SpawnYaw
    {
        bool set = false;
        float degrees = 0.0f;
        bool ownHandle = false; // also: the object's own handle into its script variable 0
    } g_spawnYaw; // the next spawnOne's rotation about Y (survival's wall pieces)
    uint32_t spawnOne(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t vm, uint32_t tmpl,
                      const float pos[3], uint32_t scratch, std::string &why,
                      const std::vector<RecordProp> *recordProps = nullptr, const RecordProp *classProp = nullptr)
    {
        auto call = [&](uint32_t fn, std::array<uint32_t, 8> args, const char *step, uint32_t &v0)
        {
            if (callGuestArgs(rdram, ctx, runtime, fn, args, v0))
                return true;
            why = std::string(step) + " did not return";
            return false;
        };
        auto inRam = [](uint32_t a) { return a != 0 && (a & PS2_RAM_MASK) == a; };
        uint32_t obj = 0, v = 0, id = 0, handle = 0;
        if (!call(kObjAllocFn, {0x120u}, "alloc", obj))
            return 0;
        if (!inRam(obj))
            return why = "no memory for the object", 0u;
        if (!call(kObjCtorFn, {obj}, "ctor", obj))
            return 0;
        if (!inRam(obj))
            return why = "constructor failed", 0u;
        if (!call(kObjIdFn, {rd32(vm + 0x98u)}, "id", id))
            return 0;
        if (!call(kObjInitFn, {obj, tmpl, vm + 0xB8u, id, vm + 0xD4u}, "init", v))
            return 0;
        if (v == 0)
        {
            why = "class init failed";
            call(kObjDestroyFn, {obj, 1u}, "destroy", v);
            return 0;
        }
        if (!call(kObjRegisterFn, {vm + 0x9Cu, obj}, "register", handle))
            return 0;
        if (handle == 0)
        {
            why = "register failed (object table full?)";
            call(kObjDestroyFn, {obj, 1u}, "destroy", v);
            return 0;
        }
        // A live enemy of the same class to copy from (the newest one: likely still in its starting state), unless the
        // caller gives the properties a level record would.
        uint32_t other = 0;
        // The object table (func_00104780): +0 next handle, +4 count, +8 list head, linked through obj+0xBC.
        if (!recordProps)
        {
            uint32_t o = rd32(vm + 0x9Cu + 8u);
            for (int guard = 0; inRam(o) && guard < 20000; ++guard, o = rd32(o + 0xBCu))
                if (o != obj && rd32(o + 0xB4u) == tmpl)
                    other = o;
        }
        const uint32_t varBytes = rd32(tmpl + 4u), vars = rd32(obj + 0x84u);
        const bool copyVars = other && varBytes && varBytes < 0x10000u && inRam(vars) && inRam(rd32(other + 0x84u));
        if (copyVars)
            std::memcpy(mem(vars), mem(rd32(other + 0x84u)), varBytes);
        // The spawn block (func_00103EA0's layout): +0 properties (32 bytes each, from the game's allocator so that
        // destroying the object frees them as usual), +4 count, +8 position, +0x14 rotation.
        const uint32_t block = obj + 0xF8u;
        uint32_t props = 0, count = 0;
        const uint32_t otherProps = other ? rd32(other + 0xF8u) : 0u, otherCount = other ? rd32(other + 0xFCu) : 0u;
        if (recordProps && !recordProps->empty())
        {
            // Like the loader: each property into its variable, then the list kept in the spawn block.
            const uint32_t entry = scratch + 0x40u, context = scratch + 0x80u;
            std::memset(mem(context), 0, 0x40);
            const uint32_t n = static_cast<uint32_t>(recordProps->size());
            if (call(kPropAllocFn, {n * 32u}, "props alloc", props) && inRam(props))
                count = n;
            else
                props = 0;
            for (uint32_t i = 0; i < n; ++i)
            {
                std::memset(mem(entry), 0, 32);
                wr32(entry + 0u, (*recordProps)[i].type);
                wr32(entry + 4u, (*recordProps)[i].index);
                wr32(entry + 8u, (*recordProps)[i].value);
                if ((*recordProps)[i].type == 12u) // a sphere (trigger / wake-up area): centred on the spawn point
                {
                    for (int k = 0; k < 3; ++k)
                        std::memcpy(mem(entry + 8u + k * 4u), &pos[k], 4);
                    wr32(entry + 0x14u, (*recordProps)[i].value); // radius (float bits)
                }
                if (inRam(vars))
                    call(kPropApplyFn, {vm, vars, rd32(tmpl + 0x10u), entry, context}, "property", v);
                if (props)
                    std::memcpy(mem(props + i * 32u), mem(entry), 32);
            }
        }
        else if (inRam(otherProps) && otherCount > 0 && otherCount < 256u &&
                 call(kPropAllocFn, {otherCount * 32u}, "props alloc", props) && inRam(props))
        {
            std::memcpy(mem(props), mem(otherProps), otherCount * 32u);
            count = otherCount;
        }
        else
            props = 0;
        // ENEMY CLASS: the class / level property (or a variant flag) over whatever was copied, into its variable
        // as the loader would, and into the kept property list when it holds that property.
        if (classProp)
        {
            const uint32_t entry = scratch + 0x40u, context = scratch + 0x80u;
            std::memset(mem(context), 0, 0x40);
            std::memset(mem(entry), 0, 32);
            wr32(entry + 0u, classProp->type);
            wr32(entry + 4u, classProp->index);
            wr32(entry + 8u, classProp->value);
            if (inRam(vars))
                call(kPropApplyFn, {vm, vars, rd32(tmpl + 0x10u), entry, context}, "class property", v);
            for (uint32_t i = 0; props && i < count; ++i)
                if (rd32(props + i * 32u + 4u) == classProp->index && (rd32(props + i * 32u) & 0xFFu) == classProp->type)
                    wr32(props + i * 32u + 8u, classProp->value);
        }
        // The Dark Knight's wall lives while the object whose handle is in its script variable 0 (the first 4 bytes
        // of its variables, obj+0x84) does: a wall piece of survival's ring gets its own handle there.
        if (g_spawnYaw.ownHandle && inRam(vars) && varBytes >= 4u)
            wr32(vars, handle);
        wr32(block + 0u, props);
        wr32(block + 4u, count);
        std::memcpy(mem(block + 8u), pos, 12);
        float rot[3] = {0.0f, 0.0f, 0.0f};
        if (other)
            std::memcpy(rot, mem(other + 0x10Cu), 12);
        if (g_spawnYaw.set)
        {
            rot[0] = rot[2] = 0.0f;
            rot[1] = g_spawnYaw.degrees; // (rotations are in degrees, about Y)
        }
        std::memcpy(mem(block + 0x14u), rot, 12);
        // The loader's "placed by the level" mark (func_00102560 sets it on every record's object). It matters: the
        // VM's apparition pass resets an object when it wakes (func_00101AD0: script variables back to the class
        // defaults) and puts the properties back only on a placed one, so an unmarked spawn lost every property the
        // moment it woke - no health (one hit killed it), and some (crocodile, prisoner, pirate, goat devil) ended
        // their script at once.
        // Every spawn carries it, so the game's own sleep (func_00102D30) resets it and puts it to sleep instead of
        // destroying it, and its wake brings it back at the same place - exactly like a level's object.
        wr32(obj + 0xF0u, 1u);
        // The entity's position and rotation for func_00105630, in scratch below the stack.
        std::memcpy(mem(scratch), pos, 12);
        std::memcpy(mem(scratch + 0x10u), rot, 12);
        // Its apparition data (RestoreApparitionData / SaveApparitionData: per-handle bytes, 4 slots of 0x180, in the
        // script context's table at +0xE08, and slot 1 also in the saved table at 0x25AF14) belongs to whatever had
        // this handle before - a killed enemy's "dead" mark among it. A new object starts clean, as a placed one does.
        {
            const uint32_t handleNow = rd32(obj), context = rd32(obj + 0xF4u);
            const uint32_t table = inRam(context) ? rd32(context + 0xE08u) : 0u;
            if (handleNow < 0x17Cu)
            {
                for (uint32_t slot = 0; slot < 4 && inRam(table); ++slot)
                    *mem(table + slot * 0x180u + handleNow + 3u) = 0;
                *mem(GA(0x0025AF14u) + 0x180u + handleNow + 3u) = 0;
            }
        }
        uint32_t active = 0;
        if (!call(kObjActivateFn, {vm, obj}, "activate", active))
            return 0;
        if (active == 0)
        {
            why = std::string("its script ended at once") + (other ? "" : " (no live enemy of this class to copy)");
            call(kObjDestroyFn, {obj, 1u}, "destroy", v);
            return 0;
        }
        // func_00101820(object, owner): the loader passes the object as both. It registers the class's event handler
        // (class+0x1C: what runs when Maximo's sword hits it) with the script context at owner+0xF4, once per class
        // and level. Called with no owner it read address 0xF4 instead, so a class with no enemy placed in the level
        // (every survival enemy in an arena) never got its hit handler: one hit, whatever the health.
        call(kObjStartFn, {obj, obj}, "start", v);
        if (!call(kEntityCreateFn, {rd32(vm + 0x98u), rd32(obj), rd32(rd32(obj + 0xB4u)), scratch, scratch + 0x10u, 1u},
                  "entity", v))
            return 0;
        const uint32_t entity = rd32(obj + 0xB8u);
        g_spawnedObjects.push_back({obj, handle, tmpl}); // for DESPAWN ALL ENEMIES
        g_spawnedThisLevel = true;                       // lets the automatic boss-freeze release run
        {
            const uint32_t type = rd32(tmpl);
            for (uint32_t b : kBossTypes)
                if (type == b)
                {
                    g_debugBosses.push_back({obj, handle, g_dbg.padFrames, 0u});
                    // (The boss bar research RAM snapshots are off: two 128 MB files written mid-fight held up the
                    // game for over half a second while the disk caught up - the stutter after the first boss.)
                    logf("debug: boss type 0x%02X spawned: Maximo's controls are kept on, and the boss is removed "
                         "(with its health bar) once it dies", type);
                }
        }
        logf("debug: spawned handle %u (object 0x%07X, entity 0x%07X): %s, %u properties", handle, obj, entity,
             recordProps ? "properties as given" : copyVars ? "variables copied from a live one" : "class default variables",
             count);
        return handle;
    }

    // The Queen flies a ring of eight QueenWaypoint objects (class 0x8A): her first script step collects them with
    // FindApparitions(0x8A, radius 1000) into an array of eight and every move goes to one of them, so without them she
    // never shows up. In C_QUEEN they are level records; anywhere else (the cross-world tool ports class 0x8A with her)
    // the ring is laid out here first, as C_QUEEN has it relative to her at half the size, with each record's four
    // properties: its id 1-8, the next and previous ids and the alternating flag.
    constexpr uint32_t kQueenType = 0x9Du, kQueenWaypointType = 0x8Au;
    struct QueenWaypoint
    {
        float dx, dy, dz; // from the Queen's C_QUEEN position (0.47, -3.40, -60.66)
        uint32_t id, next, prev, flag;
    };
    constexpr QueenWaypoint kQueenWaypoints[] = {
        {-49.20f, 3.40f, 12.96f, 8, 1, 7, 1}, {-0.55f, 3.40f, -1.08f, 1, 2, 8, 0},
        {52.73f, 3.40f, 22.06f, 2, 3, 1, 1},  {65.13f, 3.40f, 62.96f, 3, 4, 2, 0},
        {46.43f, 3.40f, 111.76f, 4, 5, 3, 1}, {-1.37f, 3.40f, 127.76f, 5, 6, 4, 0},
        {-47.87f, 3.40f, 108.76f, 6, 7, 5, 1}, {-68.17f, 3.40f, 59.06f, 7, 8, 6, 0},
    };
    constexpr float kQueenRingScale = 0.5f;

    void spawnQueenWaypoints(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t vm, const float at[3],
                             uint32_t scratch)
    {
        uint32_t wpClass = 0;
        if (!callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, kQueenWaypointType, 0, wpClass) || wpClass == 0)
        {
            logf("debug: QueenWaypoint (0x8A) is not loaded here; the Queen will have nowhere to fly");
            return;
        }
        for (uint32_t o = rd32(vm + 0x9Cu + 8u), guard = 0; o && (o & PS2_RAM_MASK) == o && guard < 20000;
             o = rd32(o + 0xBCu), ++guard)
            if (rd32(o + 0xB4u) == wpClass)
                return; // a ring is already there (C_QUEEN, or an earlier Queen)
        int made = 0;
        std::string why;
        for (const QueenWaypoint &w : kQueenWaypoints)
        {
            const float pos[3] = {at[0] + w.dx * kQueenRingScale, at[1] + w.dy, at[2] + w.dz * kQueenRingScale};
            const std::vector<RecordProp> props = {{7u, 0u, w.id}, {7u, 1u, w.next}, {7u, 2u, w.prev}, {1u, 3u, w.flag}};
            if (spawnOne(rdram, ctx, runtime, vm, wpClass, pos, scratch, why, &props))
                ++made;
        }
        logf("debug: Queen waypoints: %d of 8 laid out around (%.1f, %.1f, %.1f)%s%s", made, at[0], at[1], at[2],
             why.empty() ? "" : "; ", why.c_str());
    }

    // Spawns the picked enemy, count times, a little in front of the camera (free camera: where it looks).
    void spawnPicked(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        takeFreezeBaseline(); // the "before the fight" picture for RELEASE MAXIMO
        const uint32_t vm = rd32(kLevelVmPtr);
        const bool remote = g_spawnOverride.active; // asked for by the joining game (NETPLAY)
        if (!vm || (!remote && (g_dbg.spawnable.empty() || !g_dbg.haveGameCamera)))
        {
            g_dbg.spawnMessage = "NOTHING TO SPAWN HERE";
            return;
        }
        const EnemyType *picked = remote ? nullptr : &kEnemyTypes[g_dbg.spawnable[g_dbg.spawnPick]];
        if (remote)
            for (const EnemyType &t : kEnemyTypes)
                if (t.id == g_spawnOverride.type)
                    picked = &t;
        if (!picked)
            return;
        const EnemyType &type = *picked;
        if (!remote && g_net.mode == 2 && g_net.linked)
        {
            // NETPLAY: the host's world is the shared one - it makes the spawn, and it comes back from there.
            NetSpawnPacket pk{};
            pk.magic = kNetSpawnMagic;
            pk.type = type.id;
            pk.count = static_cast<uint32_t>(kSpawnCounts[g_dbg.spawnCount]);
            pk.enemyClass = static_cast<uint32_t>(g_dbg.enemyClass);
            spawnAnchor(pk.at);
            netSend(&pk, sizeof pk);
            g_dbg.spawnMessage = std::string("ASKED THE HOST TO SPAWN ") + type.name;
            logf("debug: netplay: asked the host to spawn %s x%u at (%.1f, %.1f, %.1f)", type.name, pk.count, pk.at[0],
                 pk.at[1], pk.at[2]);
            return;
        }
        uint32_t tmpl = 0;
        if (!callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, type.id, 0, tmpl) || tmpl == 0)
        {
            g_dbg.spawnMessage = std::string(type.name) + " IS NOT LOADED HERE";
            return;
        }
        // Where: in front of the free camera, or a few steps past what the game camera looks at (Maximo).
        float at[3];
        if (remote)
            std::memcpy(at, g_spawnOverride.at, sizeof at);
        else
            spawnAnchor(at);
        // Scratch for the entity names: below the caller's stack, which the calls run under.
        const uint32_t savedSp = reg(ctx, 29);
        const uint32_t sp = (savedSp - 0x200u) & ~0xFu;
        SET_GPR_U32(ctx, 29, sp);
        const int count = remote ? static_cast<int>(g_spawnOverride.count) : kSpawnCounts[g_dbg.spawnCount];
        const int enemyClass = remote ? static_cast<int>(g_spawnOverride.enemyClass) : g_dbg.enemyClass;
        int made = 0;
        std::string why;
        if (type.id == kQueenType)
            spawnQueenWaypoints(rdram, ctx, runtime, vm, at, sp + 0x20u);
        for (int n = 0; n < count; ++n)
        {
            const float angle = 6.2831853f * static_cast<float>(n) / static_cast<float>(count);
            const float radius = count > 1 ? 3.0f + 0.5f * static_cast<float>(count) : 0.0f;
            const float pos[3] = {at[0] + radius * std::cos(angle), at[1], at[2] + radius * std::sin(angle)};
            // A Doomed Soul rises only when Maximo enters its two spheres (record properties 1 and 5); copied from a
            // live one they stayed where that one is, so they are made here around the new soul (U_INTRO radii).
            static const std::vector<RecordProp> kDoomedSoulProps = {
                {7u, 0u, 0u}, {12u, 1u, 0x41A00000u}, {12u, 5u, 0x41F00000u}, {7u, 9u, 0u}};
            const std::vector<RecordProp> *props = type.id == 0x5Du ? &kDoomedSoulProps : nullptr;
            RecordProp classProp{};
            const RecordProp *classPtr = nullptr;
            if (enemyClass > 0)
                for (const EnemyClassInfo &c : kEnemyClasses)
                    if (c.id == type.id)
                    {
                        classProp = {c.propType, 0u, c.values[std::min(enemyClass, c.count) - 1]};
                        classPtr = &classProp;
                    }
            if (!spawnOne(rdram, ctx, runtime, vm, tmpl, pos, sp + 0x20u, why, props, classPtr))
                break;
            ++made;
        }
        SET_GPR_U32(ctx, 29, savedSp);
        char b[128];
        if (made == 0)
            std::snprintf(b, sizeof b, "COULD NOT SPAWN %s: %s", type.name, why.c_str());
        else
            std::snprintf(b, sizeof b, "SPAWNED %d %s AT %.0f %.0f %.0f", made, type.name, at[0], at[1], at[2]);
        for (char &c : b)
            c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        g_dbg.spawnMessage = b;
        logf("debug: spawn %s (type 0x%02X, class 0x%07X) x%d: %d made, at (%.1f, %.1f, %.1f)%s%s", type.name, type.id,
             tmpl, count, made, at[0], at[1], at[2], why.empty() ? "" : "; stopped: ", why.c_str());
    }

    // Spawns pickups from the debug menu: `which` 0 = the item line, 1 = skills, 2 = sword powers. Pick 0 (ALL) puts
    // one of each in a ring; any other pick makes SPAWN COUNT of that one. Each is made from the properties its placed
    // records have (itemProps / abilityProps), at the same spot an enemy would go.
    std::vector<std::pair<uint32_t, uint32_t>> g_despawned; // (object, handle) taken out by DESPAWN ALL ENEMIES

    constexpr uint32_t kObjectListRemoveFn = GA(0x001049B0u); // (list, object): unlink, the inverse of func_104780
    // How the game itself takes an object away when its script ends (the interpreter at 0x108AB4, and the VM's
    // apparition removal at 0x102D6C): func_00104850(vm + 0x9C, handle) takes it off the object table, then its
    // destructor func_00101610(object, 1) frees it with everything it owns (entity, model, particle systems).
    // Only unlinking it left those behind, still pointing at it: the next level change crashed in the particle
    // update (0x1846A8 / 0x184CE8 reading freed memory).
    constexpr uint32_t kObjectTableRemoveFn = GA(0x00104850u);
    // A scene node (the actor class, vtable 0x1EC380) keeps its children at +0x48 (count) / +0x50 (array); its
    // virtual func_0018E040 updates them every frame. Lord Glutterscum's model is a child of another of his arena's
    // nodes, and his destructor does not take it out of that list (in the game he is never destroyed): wave 1 then
    // reused the freed memory and the parent's update jumped through a garbage vtable. So before destroying anything
    // its actor is taken out of every child list that holds it.
    // (Several actors in one go: the two passes over the whole RAM are what this costs - 128 MB each - and one
    // removal at a time made DESPAWN ALL ENEMIES and survival's start in a level take a quarter of a second per
    // enemy, 7.7 seconds for 35. The passes themselves only look closer at words inside the range the wanted values
    // lie in.)
    std::unordered_set<uint32_t> g_detachedAlready; // actors handled by a batch (destroyLevelObject skips them)
    void detachManyFromParents(const std::vector<uint32_t> &actors)
    {
        auto inRam = [](uint32_t a) { return a >= 0x00100000u && a < PS2_RAM_SIZE && (a & 3u) == 0; };
        auto isVtable = [](uint32_t v) { return v >= GA(0x001E0000u) && v < GA(0x00200000u) && (v & 3u) == 0; };
        // Each actor's whole subtree (its own child nodes go with it when it is freed): every node in it is taken
        // out of any child list outside that subtree.
        std::unordered_map<uint32_t, uint32_t> nodes; // node -> which actor's subtree
        uint32_t group = 0;
        for (const uint32_t actor : actors)
        {
            ++group;
            if (!inRam(actor) || nodes.count(actor))
                continue;
            std::vector<uint32_t> subtree{actor};
            for (size_t i = 0; i < subtree.size() && subtree.size() < 256u; ++i)
            {
                const uint32_t n = subtree[i], count = rd32(n + 0x48u), arr = rd32(n + 0x50u);
                if (!isVtable(rd32(n)) || count > 256u || !inRam(arr))
                    continue;
                for (uint32_t k = 0; k < count; ++k)
                {
                    const uint32_t c = rd32(arr + 4u * k);
                    if (inRam(c) && isVtable(rd32(c)) && std::find(subtree.begin(), subtree.end(), c) == subtree.end())
                        subtree.push_back(c);
                }
            }
            for (const uint32_t n : subtree)
                nodes.emplace(n, group);
        }
        if (nodes.empty())
            return;
        constexpr uint32_t kLo = 0x00100000u, kHi = PS2_RAM_SIZE;
        const uint32_t *ram = reinterpret_cast<const uint32_t *>(mem(0));
        uint32_t nodeLo = ~0u, nodeHi = 0u;
        for (const auto &[n, g] : nodes)
        {
            nodeLo = std::min(nodeLo, n);
            nodeHi = std::max(nodeHi, n);
        }
        // Where any of them is referenced, then which of those words lie in a parent's child array.
        std::unordered_set<uint32_t> arrays;
        uint32_t arrLo = ~0u, arrHi = 0u;
        int refs = 0;
        for (uint32_t a = kLo; a < kHi; a += 4u)
        {
            const uint32_t v = ram[a >> 2];
            if (v < nodeLo || v > nodeHi || !nodes.count(v) || refs >= 16384)
                continue;
            ++refs;
            for (uint32_t k = 0; k < 256u && a >= kLo + 4u * k; ++k)
                arrays.insert(a - 4u * k);
            arrLo = std::min(arrLo, a >= kLo + 1020u ? a - 1020u : kLo);
            arrHi = std::max(arrHi, a);
        }
        int removed = 0;
        for (uint32_t q = kLo + 0x50u; q < kHi && !arrays.empty(); q += 4u)
        {
            const uint32_t arr = ram[q >> 2];
            if (arr < arrLo || arr > arrHi || (arr & 3u) || !arrays.count(arr))
                continue;
            const uint32_t node = q - 0x50u, count = rd32(node + 0x48u);
            if (count == 0 || count > 256u || !isVtable(rd32(node)))
                continue;
            const auto parent = nodes.find(node);
            for (uint32_t i = 0; i < count;)
            {
                const uint32_t c = rd32(arr + 4u * i);
                const auto child = nodes.find(c);
                // (a child inside its own actor's subtree stays: it goes with that actor)
                if (child == nodes.end() || (parent != nodes.end() && parent->second == child->second))
                {
                    ++i;
                    continue;
                }
                const uint32_t n = rd32(node + 0x48u);
                for (uint32_t j = i; j + 1 < n; ++j)
                    wr32(arr + 4u * j, rd32(arr + 4u * (j + 1)));
                wr32(arr + 4u * (n - 1), 0u);
                wr32(node + 0x48u, n - 1);
                ++removed;
                logf("debug:   node 0x%07X taken out of parent 0x%07X (vtable 0x%06X), %u children left", c, node,
                     rd32(node), n - 1);
                if (n - 1 <= i)
                    break;
            }
        }
        logf("debug:   detach: %zu actor(s), %zu nodes in their subtrees, %d references, %d taken out of parents",
             actors.size(), nodes.size(), refs, removed);
    }
    void detachFromParents(uint32_t actor)
    {
        if (g_detachedAlready.count(actor))
            return;
        detachManyFromParents({actor});
    }

    // Lord Glutterscum cannot be destroyed (his arena keeps pointers into him that the destructor leaves behind:
    // the game crashed right after wave 1 even with his nodes taken out of their parents). So he is parked
    // instead: unlinked from the level's object list (nothing updates or draws him), and linked back the moment the
    // level starts to end (exit code set), so the level's own teardown destroys him the normal way - unlinked and
    // never put back is what crashed the next level change before.
    constexpr uint32_t kGlutterscumType = 0x8Eu;
    std::vector<std::pair<uint32_t, uint32_t>> g_parked; // (object, handle)
    void parkLevelObject(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t vm, uint32_t obj)
    {
        uint32_t v0 = 0;
        g_parked.push_back({obj, rd32(obj)});
        // His model is drawn from his actor whether or not his object is updated: it is kept far off to the side
        // every frame (the DESPAWN keep-away list), until the level ends.
        g_despawned.push_back({obj, rd32(obj)});
        {
            const uint32_t ent = rd32(obj + 0xB8u);
            const uint32_t act = ent && (ent & PS2_RAM_MASK) == ent ? rd32(ent) : 0u;
            if (act && (act & PS2_RAM_MASK) == act)
            {
                const float away = 100000.0f;
                std::memcpy(mem(act + 0xA0u), &away, 4);
            }
        }
        callGuest(rdram, ctx, runtime, kObjectListRemoveFn, vm + 0x9Cu, obj, 0, v0);
        logf("debug:   object 0x%07X parked (unlinked until the level ends)", obj);
    }
    void unparkAll()
    {
        if (g_parked.empty())
            return;
        const uint32_t vm = rd32(kLevelVmPtr), list = vm + 0x9Cu;
        for (const auto &[obj, handle] : g_parked)
        {
            if (!vm || rd32(obj) != handle)
                continue;
            // func_00104780's append: to the end of the chain through +0xBC, count at +4.
            wr32(obj + 0xBCu, 0u);
            uint32_t at = list + 8u;
            for (int guard = 0; rd32(at) && guard < 20000; ++guard)
            {
                if (rd32(at) == obj)
                    break;
                at = rd32(at) + 0xBCu;
            }
            if (rd32(at) == obj)
                continue;
            wr32(at, obj);
            wr32(list + 4u, rd32(list + 4u) + 1u);
            g_despawned.erase(std::remove_if(g_despawned.begin(), g_despawned.end(),
                                             [&](const auto &d) { return d.first == obj; }),
                              g_despawned.end());
            logf("debug: parked object 0x%07X linked back for the level's teardown", obj);
        }
        g_parked.clear();
    }

    // (detach = false: for the simple objects the game destroys this same way all the time - pickups, the Dark
    // Knight's wall pieces - whose actors are nobody's child; the search for parents is a pass over the whole RAM,
    // a visible hitch each time a soul or a drop was taken away.)
    bool destroyLevelObject(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t vm, uint32_t obj,
                            bool detach)
    {
        const uint32_t handle = rd32(obj);
        // Only a boss's nodes are ever another object's children (Lord Glutterscum's arena; no enemy's actor was
        // found in a parent's list in any log): the search, two passes over the whole 128 MB of RAM, a tenth of a
        // second each time, is left for the bosses.
        if (detach)
        {
            const uint32_t cls = rd32(obj + 0xB4u);
            const uint32_t type = cls && (cls & PS2_RAM_MASK) == cls ? rd32(cls) : 0u;
            detach = std::find(std::begin(kBossTypes), std::end(kBossTypes), type) != std::end(kBossTypes);
        }
        if (detach)
        {
            const uint32_t ent = rd32(obj + 0xB8u);
            if (ent && (ent & PS2_RAM_MASK) == ent)
                detachFromParents(rd32(ent));
        }
        uint32_t v0 = 0;
        if (!callGuestArgs(rdram, ctx, runtime, kObjectTableRemoveFn, {vm + 0x9Cu, handle, 0, 0, 0, 0, 0, 0}, v0))
            return false;
        callGuestArgs(rdram, ctx, runtime, kObjDestroyFn, {obj, 1u, 0, 0, 0, 0, 0, 0}, v0);
        return true;
    }

    // GIVE ABILITY: SetAbility__6PlayerFib(gPlayer, id, 1), the call the Ability pickup makes.
    void giveAbility(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (!g_dbg.inLevel)
        {
            g_dbg.spawnMessage = "ONLY IN A LEVEL";
            return;
        }
        const uint32_t id = static_cast<uint32_t>(g_dbg.givePick);
        uint32_t v0 = 0;
        // callGuestArgs follows the call through scheduler checkpoints (the plain callGuest stopped part way).
        const bool ok = callGuestArgs(rdram, ctx, runtime, kSetAbilityFn, {kGamePlayerAddr, id, 1u, 0, 0, 0, 0, 0}, v0);
        g_dbg.spawnMessage = std::string(ok ? "GAVE " : "COULD NOT GIVE ") + kGiveAbilities[id];
        logf("debug: give ability %u (%s): %s", id, kGiveAbilities[id], ok ? "done" : "call failed");
    }

    // DESPAWN ALL ENEMIES: every object of an enemy or boss class (kEnemyTypes up to the NPCs) has its actor moved
    // far off, out of sight and reach, and kept there every frame (applyFrameControls) while the object lives.
    void despawnEnemies(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        // (The whole RAM: a spawned enemy's actor lives in the extended RAM above 32 MB, e.g. 0x72A1BE0.)
        auto inRam = [](uint32_t a) { return a >= 0x00100000u && a < PS2_RAM_SIZE && (a & 3u) == 0; };
        const uint32_t vm = rd32(kLevelVmPtr);
        if (!g_dbg.inLevel || !inRam(vm))
        {
            g_dbg.spawnMessage = "ONLY IN A LEVEL";
            return;
        }
        std::vector<uint32_t> classes;
        for (const EnemyType &e : kEnemyTypes)
        {
            if (std::strcmp(e.home, "NPC") == 0 || (e.id >= 0xFBu && e.id <= 0xFEu))
                continue; // Grim and the sorceresses stay
            uint32_t cls = 0;
            if (callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, e.id, 0, cls) && cls)
                classes.push_back(cls);
        }
        int gone = 0;
        std::vector<uint32_t> victims;
        for (uint32_t o = rd32(vm + 0x9Cu + 8u), guard = 0; inRam(o) && guard < 20000; o = rd32(o + 0xBCu), ++guard)
        {
            if (std::find(classes.begin(), classes.end(), rd32(o + 0xB4u)) == classes.end())
                continue;
            victims.push_back(o);
            ++gone;
        }
        // Every enemy the debug menu spawned, too (the list walk alone missed them), while it is still alive.
        int spawnedGone = 0;
        for (const SpawnedObject &sp : g_spawnedObjects)
        {
            if (rd32(sp.obj) != sp.handle || rd32(sp.obj + 0xB4u) != sp.tmpl)
                continue;
            if (std::find(classes.begin(), classes.end(), sp.tmpl) == classes.end())
                continue;
            if (std::find(victims.begin(), victims.end(), sp.obj) != victims.end())
                continue;
            victims.push_back(sp.obj);
            ++gone;
            ++spawnedGone;
        }
        g_spawnedObjects.erase(std::remove_if(g_spawnedObjects.begin(), g_spawnedObjects.end(),
                                              [](const SpawnedObject &sp) { return rd32(sp.obj) != sp.handle; }),
                               g_spawnedObjects.end());
        // Each goes the way the game removes an object whose script ended (destroyLevelObject).
        // Their actors are taken out of their parents' child lists first, all in one pass over the RAM (the
        // bosses among them: see destroyLevelObject).
        {
            std::vector<uint32_t> actors;
            for (uint32_t o : victims)
            {
                const uint32_t cls = rd32(o + 0xB4u);
                const uint32_t type = cls && (cls & PS2_RAM_MASK) == cls ? rd32(cls) : 0u;
                if (std::find(std::begin(kBossTypes), std::end(kBossTypes), type) == std::end(kBossTypes))
                    continue;
                const uint32_t ent = rd32(o + 0xB8u);
                if (ent && (ent & PS2_RAM_MASK) == ent)
                    actors.push_back(rd32(ent));
            }
            detachManyFromParents(actors);
            g_detachedAlready.insert(actors.begin(), actors.end());
        }
        for (uint32_t o : victims)
            destroyLevelObject(rdram, ctx, runtime, vm, o);
        g_detachedAlready.clear();
        char msg[64];
        std::snprintf(msg, sizeof msg, "DESPAWNED %d ENEMIES", gone);
        g_dbg.spawnMessage = msg;
        logf("debug: despawned %d enemies, %d of them spawned by the menu (%zu enemy classes loaded here)", gone,
             spawnedGone, classes.size());
    }

    void spawnPickups(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, int which)
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        if (!vm || !g_dbg.haveGameCamera)
        {
            g_dbg.spawnMessage = "NOTHING TO SPAWN HERE";
            return;
        }
        struct One
        {
            uint32_t type;
            std::vector<RecordProp> props;
            const char *name;
            bool copy = false; // take the variables from a live instance (nullptr recordProps)
        };
        std::vector<One> list;
        const char *label = "";
        int pick = 0;
        if (which == 0)
        {
            pick = std::min(g_dbg.itemPick, static_cast<int>(g_dbg.items.size()));
            for (size_t i = 0; i < g_dbg.items.size(); ++i)
                if (pick == 0 || static_cast<int>(i) == pick - 1)
                {
                    const PickupType &t = kItemTypes[g_dbg.items[i]];
                    list.push_back({t.id, itemProps(t.id), t.name});
                }
            label = "ALL ITEMS";
        }
        else if ((which == 1 || which == 2) && g_dbg.haveAbility)
        {
            const PickupType *table = which == 1 ? kSkills : kSwordPowers;
            const int n = which == 1 ? static_cast<int>(std::size(kSkills)) : static_cast<int>(std::size(kSwordPowers));
            pick = which == 1 ? g_dbg.skillPick : g_dbg.swordPick;
            for (int i = 0; i < n; ++i)
                if (pick == 0 || i == pick - 1)
                    list.push_back({kAbilityType, abilityProps(table[i].id), table[i].name});
            label = which == 1 ? "ALL SKILLS" : "ALL SWORD POWERS";
        }
        else if (which == 3)
        {
            pick = std::min(g_dbg.structPick, static_cast<int>(g_dbg.structs.size()));
            for (size_t i = 0; i < g_dbg.structs.size(); ++i)
                if (pick == 0 || static_cast<int>(i) == pick - 1)
                {
                    const StructureType &t = kStructures[g_dbg.structs[i]];
                    list.push_back({t.id, structureProps(t.id), t.name, t.copy});
                }
            label = "ALL STRUCTURES";
        }
        else if (which == 4 && g_dbg.haveCollector)
        {
            static char names[kCollectorStocks][24];
            pick = g_dbg.collectorPick;
            for (int i = 0; i < kCollectorStocks; ++i)
                if (pick == 0 || i == pick - 1)
                {
                    std::snprintf(names[i], sizeof names[i], "COLLECTOR STOCK %d", i);
                    list.push_back({kCollectorType, {{7u, 0u, static_cast<uint32_t>(i)}, {7u, 1u, 0u}}, names[i]});
                }
            label = "ALL COLLECTORS";
        }
        else if (which == 5 && !g_dbg.anyClasses.empty())
        {
            // One class, copied from a live instance when there is one (its variables and properties), else made
            // from its class defaults with no properties.
            static char name[64];
            const uint32_t id = g_dbg.anyClasses[std::clamp(g_dbg.anyPick, 0, int(g_dbg.anyClasses.size()) - 1)];
            std::snprintf(name, sizeof name, "0x%02X %s", id, classNameFor(id));
            list.push_back({id, {}, name, true});
            pick = 1;
            label = name;
        }
        if (list.empty())
        {
            g_dbg.spawnMessage = "NOT LOADED HERE";
            return;
        }
        if (pick != 0)
            label = list[0].name;
        float at[3];
        spawnAnchor(at);
        const int count = pick == 0 ? static_cast<int>(list.size()) : kSpawnCounts[g_dbg.spawnCount];
        // About 2.5 units between neighbours on the ring.
        const float radius = count > 1 ? std::max(3.0f, 2.5f * static_cast<float>(count) / 6.2831853f) : 0.0f;
        // Look every class up once, before the stack is lowered (as spawnPicked does): a lookup made after a few
        // spawns, inside the lowered frame, stopped answering, which is why only the first two ever appeared.
        std::vector<uint32_t> classes(list.size(), 0);
        for (size_t i = 0; i < list.size(); ++i)
        {
            uint32_t t = 0;
            if (callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, list[i].type, 0, t))
                classes[i] = t;
        }
        const uint32_t savedSp = reg(ctx, 29);
        const uint32_t sp = (savedSp - 0x200u) & ~0xFu;
        SET_GPR_U32(ctx, 29, sp);
        int made = 0;
        std::string why, failed;
        for (int n = 0; n < count; ++n)
        {
            const size_t li = pick == 0 ? static_cast<size_t>(n) : 0;
            const One &one = list[li];
            const uint32_t tmpl = classes[li];
            if (tmpl == 0)
            {
                failed += std::string(failed.empty() ? "" : ", ") + one.name;
                why = "class lookup failed";
                continue;
            }
            const float angle = 6.2831853f * static_cast<float>(n) / static_cast<float>(count);
            const float pos[3] = {at[0] + radius * std::cos(angle), at[1], at[2] + radius * std::sin(angle)};
            std::string oneWhy;
            if (spawnOne(rdram, ctx, runtime, vm, tmpl, pos, sp + 0x20u, oneWhy, one.copy ? nullptr : &one.props))
                ++made;
            else
            {
                why = oneWhy;
                failed += std::string(failed.empty() ? "" : ", ") + one.name;
            }
        }
        SET_GPR_U32(ctx, 29, savedSp);
        char b[128];
        if (made == 0)
            std::snprintf(b, sizeof b, "COULD NOT SPAWN %s: %s", label, why.empty() ? "NOT LOADED" : why.c_str());
        else
            std::snprintf(b, sizeof b, "SPAWNED %d %s AT %.0f %.0f %.0f", made, label, at[0], at[1], at[2]);
        for (char &c : b)
            c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        g_dbg.spawnMessage = b;
        logf("debug: spawn %s: %d of %d made at (%.1f, %.1f, %.1f)%s%s%s%s", label, made, count, at[0], at[1], at[2],
             failed.empty() ? "" : "; failed: ", failed.c_str(), why.empty() ? "" : "; last reason: ", why.c_str());
    }

    // GO TO LEVEL: ends the current level the way a sub-level door does (func_00120EB0, exit code 4) with the picked
    // LevelTable row as the target; the game's main loop then loads it. The menu closes so the game gets the controller.
    bool goToLevel(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, int target)
    {
        const uint32_t current = rd32(kCurrentLevelAddr);
        if (!g_dbg.inLevel || current >= std::size(kLevelNames))
        {
            g_dbg.spawnMessage = "GO TO LEVEL: ONLY IN A LEVEL";
            return false;
        }
        if (rd32(kLevelExitCodeAddr) != 0)
        {
            g_dbg.spawnMessage = "GO TO LEVEL: THE LEVEL IS ALREADY ENDING";
            return false;
        }
        uint32_t v0 = 0;
        const uint32_t delta = static_cast<uint32_t>(target - static_cast<int>(current));
        if (!callGuest(rdram, ctx, runtime, kChangeLevelFn, delta, 0, 0, v0))
        {
            g_dbg.spawnMessage = "GO TO LEVEL: THE GAME DID NOT TAKE IT";
            logf("debug: go to level %d: func_00120EB0 did not return", target);
            return false;
        }
        logf("debug: go to level %d %s (%s) from %u: exit code %u, target %u", target, kLevelNames[target],
             kLevelFiles[target], current, rd32(kLevelExitCodeAddr), rd32(GA(0x00238FBCu)));
        g_dbg.tracked.clear(); // this level's objects are about to go
        g_dbg.spawnMessage.clear();
        g_dbg.menuOpen = false;
        return true;
    }

    // scePadRead's buffer for port 0, after the runtime filled it: menu and free camera input, then a released
    // controller for the game while either is in use.
    std::string hexWords(uint32_t addr, uint32_t count)
    {
        std::string out;
        char b[16];
        for (uint32_t i = 0; i < count; ++i)
        {
            std::snprintf(b, sizeof b, "%s%08X", i == 0 ? "" : i % 8 == 0 ? " | " : " ", rd32(addr + i * 4u));
            out += b;
        }
        return out;
    }

    // SAVE RAM SNAPSHOT: all of the PS2's RAM to ram_snapshot_<n>.bin next to boot_log.txt, for working out how the
    // game builds Maximo (second-player research). The log gets the objects it starts from: the game Player
    // (0x263540, whose first word points at Maximo's body), the player controller (0x239890) and the level VM.
    std::string g_logPath = "boot_log.txt";
    void saveRamSnapshot()
    {
        static int count = 0;
        const fs::path dir = fs::absolute(fs::path(g_logPath)).parent_path();
        char name[48];
        std::snprintf(name, sizeof name, "ram_snapshot_%d.bin", ++count);
        const fs::path file = dir / name;
        std::ofstream(file, std::ios::binary).write(reinterpret_cast<const char *>(g_rdram), PS2_RAM_SIZE);
        const uint32_t body = rd32(kGamePlayerAddr);
        logf("debug: RAM snapshot %s (frame %llu, level row %d): Player 0x263540 body 0x%07X, level VM 0x%07X",
             file.string().c_str(), static_cast<unsigned long long>(g_guestFrame.load()), g_dbg.levelPick, body,
             rd32(kLevelVmPtr));
        logf("debug:   Player   : %s", hexWords(kGamePlayerAddr, 32).c_str());
        if (body && (body & PS2_RAM_MASK) == body)
            logf("debug:   body     : %s", hexWords(body, 64).c_str());
        logf("debug:   gPlayer  : %s", hexWords(kPlayerAddr, 32).c_str());
        g_dbg.spawnMessage = std::string("SAVED ") + name;
    }

    void trackSpawns()
    {
        // The debug menu's and survival's timings count frames at 60 a second; above 60 Hz the count is paced.
        {
            static uint32_t credit = 0;
            const uint32_t hz = g_fieldRateHz > 60u && g_fieldRateHz == g_frameRateHz ? g_fieldRateHz : 60u;
            credit += 60u;
            if (credit >= hz)
            {
                credit -= hz;
                ++g_dbg.padFrames;
            }
        }
        for (size_t i = 0; i < g_dbg.tracked.size();)
        {
            const auto &t = g_dbg.tracked[i];
            const uint64_t age = g_dbg.padFrames - t.born;
            const bool alive = rd32(t.obj) == t.handle && rd32(t.obj + 0xB4u) == t.tmpl;
            if (!alive)
            {
                logf("debug: track handle %u: gone after %llu frames", t.handle, static_cast<unsigned long long>(age));
                g_dbg.tracked.erase(g_dbg.tracked.begin() + static_cast<std::ptrdiff_t>(i));
                continue;
            }
            if (age == 1 || age == 30 || age == 120 || age == 300)
            {
                const uint32_t entity = rd32(t.obj + 0xB8u);
                const uint32_t actor = entity && (entity & PS2_RAM_MASK) == entity ? rd32(entity) : 0u;
                float pos[3] = {};
                if (actor && (actor & PS2_RAM_MASK) == actor)
                    std::memcpy(pos, mem(actor + 0xA0u), 12);
                logf("debug: track handle %u at %llu frames: actor 0x%07X at (%.1f, %.1f, %.1f)", t.handle,
                     static_cast<unsigned long long>(age), actor, pos[0], pos[1], pos[2]);
                logf("debug:   object 0x%07X: %s", t.obj, hexWords(t.obj, 72).c_str());
                {
                    // Its script variables (what health lives in), state and flags.
                    const uint32_t vars = rd32(t.obj + 0x84u), bytes = rd32(t.tmpl + 4u);
                    if (vars && (vars & PS2_RAM_MASK) == vars && bytes && bytes < 0x400u)
                        logf("debug:   vars 0x%07X (state %u flags 0x%X placed %u): %s", vars, rd32(t.obj + 0xA8u),
                             rd32(t.obj + 0xA0u), rd32(t.obj + 0xF0u), hexWords(vars, std::min(bytes / 4u, 48u)).c_str());
                }
                if (age == 1)
                {
                    // The class as the game holds it, and what its resource list points at.
                    logf("debug:   class  0x%07X: %s", t.tmpl, hexWords(t.tmpl, 64).c_str());
                    const uint32_t vm = rd32(kLevelVmPtr);
                    const uint32_t res = vm ? vm + 0xB8u : 0u;
                    logf("debug:   class 0x%07X words at +0x10 0x%07X, +0x50 0x%08X 0x%08X 0x%08X", t.tmpl,
                         rd32(t.tmpl + 0x10u), rd32(t.tmpl + 0x50u), rd32(t.tmpl + 0x54u), rd32(t.tmpl + 0x58u));
                    (void)res;
                }
                if (actor && (actor & PS2_RAM_MASK) == actor)
                    logf("debug:   actor  0x%07X: %s", actor, hexWords(actor, 192).c_str());
            }
            if (age >= 300)
            {
                g_dbg.tracked.erase(g_dbg.tracked.begin() + static_cast<std::ptrdiff_t>(i));
                continue;
            }
            ++i;
        }
    }

    // Probe: func_0015C8C0(?, resources a1, ?, category a3, index t0) hands out a copy of a world resource; for a
    // model (category 1) it reads the template at table [a1+4][index] (count at [a1]) and copies it as rigid (its
    // type, vtable+0xC, 1) or skinned (2). While spawns are tracked, every model request past index 140 is logged
    // with what sits in that slot, read before the game's code runs (nothing is changed).
    constexpr uint32_t kResourceCopyFn = GA(0x0015C8C0u), kResourceLookupFn = GA(0x0015BA70u);
    uint32_t g_resourceLookupCaller = 0;

    // func_0015BA70(resources, reference): decodes the reference and calls func_0015C8C0; noted for the probe above.
    void resourceLookupProbeEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == kResourceLookupFn && g_resourceProbeLeft > 0)
        {
            g_resourceLookupCaller = reg(ctx, 31);
            logf("debug: resource lookup 0x%08X from 0x%06X", reg(ctx, 5), reg(ctx, 31));
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    void resourceCopyProbeEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (ctx->pc == kResourceCopyFn && g_resourceProbeLeft > 0)
        {
            const uint32_t res = reg(ctx, 5), category = reg(ctx, 7), index = reg(ctx, 8);
            if (index < 4096u && res && (res & PS2_RAM_MASK) == res)
            {
                --g_resourceProbeLeft;
                uint32_t tmpl = 0, vtable = 0;
                if (category == 1u)
                {
                    const uint32_t count = rd32(res), table = rd32(res + 4u);
                    tmpl = index < count && table ? rd32(table + index * 4u) : 0u;
                    vtable = tmpl && (tmpl & PS2_RAM_MASK) == tmpl ? rd32(tmpl) : 0u;
                }
                logf("debug: resource request category %u index %u (lookup called from 0x%06X): template 0x%07X "
                     "vtable 0x%06X", category, index, g_resourceLookupCaller, tmpl, vtable);
            }
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    // Once per pad read, which the game does right after func_147FE0 has worked out the frame's time step (0x25B9F0)
    // and before anything moves by it (a hook on func_1480C0 itself never showed in the step: the FADE probe kept
    // reading 0.983 at 2x). GAME SPEED scales the step, PAUSE GAME IN FREE CAMERA holds it near 0 (not 0: anything
    // dividing by it stays finite), INFINITE holds the counters, DESPAWN keeps its enemies away. The game can read
    // the pad more than once a frame, so a step already written is recognised and left alone.
    // Player+0x84 (the game Player at 0x263540) is the "controls enabled" flag: while it is 0 the player update clears
    // every button except Start/Select (0x130530). A boss's death sets it to 0 for the end-of-level sequence, and only
    // the arena's level change sets it back, so a boss killed inside a normal level leaves Maximo stuck.
    constexpr uint32_t kPlayerControlFlag = GA(0x00263540u) + 0x84u;
    // HIGH FRAME RATE in the NTSC releases (see applyFrameControls): func_147FE0 works out the step (func_1480C0)
    // and then runs the frame through func_14B7E0 or func_14B4F0, which move the screen fade and more before the pad
    // is read; so the real step is written just before those run.
    constexpr uint32_t kFrameUpdateFns[2] = {GA(0x0014B7E0u), GA(0x0014B4F0u)};
    // HIGH FRAME RATE: the step each frame moves the game by (0x25B9F0, in 60ths of a second), written before the
    // frame's update runs (frameUpdateEntry) and again at the pad read (applyFrameControls, which then scales it).
    // - Gameplay above the release's own rate: the real time since the last frame on the PC's clock, smoothed a little
    //   (and what the smoothing holds back is paid out over the next frames, so game time keeps up with the clock).
    //   The game's own step follows guest time, which only runs as fast as the frames get done: at 90 frames a second
    //   in 120 Hz gameplay the game ran at 75% speed. The NTSC releases also round their step to whole 60ths, which
    //   at 120 Hz came out 0 (the game stood still).
    // - The NTSC releases above 60 Hz otherwise (the fast rate after a load): the guest fields since the last frame,
    //   so that stretch still runs ahead of the clock as it does in PAL.
    void ntscRealStep()
    {
        if (!g_runtime)
            return;
        static uint64_t lastTick = 0;
        static std::chrono::steady_clock::time_point lastTime{};
        static float ema = 0.0f, debt = 0.0f;
        const uint64_t tick = g_runtime->eeScheduler().currentVSyncTick();
        if (tick == lastTick)
            return; // (the second call of the same frame)
        const uint64_t fields = lastTick != 0 && tick > lastTick ? std::min<uint64_t>(tick - lastTick, 8u) : 1u;
        lastTick = tick;
        const auto now = std::chrono::steady_clock::now();
        const float nominal = 60.0f / static_cast<float>(std::max<uint32_t>(1u, g_fieldRateHz));
        float step = -1.0f;
        if (highRateNow())
        {
            float raw = std::chrono::duration<float>(now - lastTime).count() * 60.0f;
            if (lastTime.time_since_epoch().count() == 0 || raw > 15.0f) // the first frame, or after a stall or a load
            {
                raw = nominal;
                ema = 0.0f;
                debt = 0.0f;
            }
            raw = std::clamp(raw, 0.1f, 6.0f);
            ema = ema > 0.0f ? ema + (raw - ema) * 0.3f : raw;
            debt += raw - ema;
            const float pay = std::clamp(debt * 0.2f, -0.5f * ema, 0.5f * ema);
            debt -= pay;
            step = ema + pay;
        }
        else if (!kPalTimings && g_fieldRateHz > 60u)
        {
            step = 60.0f * static_cast<float>(fields) / static_cast<float>(g_fieldRateHz);
            ema = debt = 0.0f;
        }
        else
            ema = debt = 0.0f;
        lastTime = now;
        if (step > 0.0f)
            std::memcpy(mem(kFrameStepAddr), &step, 4);
    }

    void frameUpdateEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        ntscRealStep();
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }

    void applyFrameControls()
    {
        static float lastWritten = -1.0f;
        ntscRealStep();
        float step = 0.0f;
        std::memcpy(&step, mem(kFrameStepAddr), 4);
        if (step != lastWritten && std::isfinite(step))
        {
            float scaled = step;
            if ((g_dbg.freeCam && g_dbg.pauseInFreeCam) || g_viewP2Now)
                scaled = 0.001f;
            else
                scaled = step * kGameSpeeds[std::clamp(g_dbg.gameSpeed, 0, 4)];
            if (scaled != step)
                std::memcpy(mem(kFrameStepAddr), &scaled, 4);
            lastWritten = scaled;
        }
        // Player / app state changes, for the "cannot move after a boss dies in a level" freeze: Player::Update's state
        // (gPlayer+0x248, 0 while walking), the app mode (gp-29056) and submode (gp-29092) that mglGotoNextLevel sets
        // to 6 / 3 (the randomizer's boss_inject notes: control comes back only when that transition completes).
        {
            static uint32_t last[3] = {~0u, ~0u, ~0u};
            const uint32_t now[3] = {rd32(kPlayerAddr + 0x248u), rd32(kAppModeAddr), rd32(kAppSubmodeAddr)};
            if (g_dbg.inLevel && (now[0] != last[0] || now[1] != last[1] || now[2] != last[2]))
            {
                logf("debug: player state %u, app mode %u, submode %u", now[0], now[1], now[2]);
                std::memcpy(last, now, sizeof last);
            }
        }
        // Automatic release, instant: while a boss spawned from the menu is in this level, Maximo's controls are given
        // back the same frame anything turns them off (the boss's intro or its death sequence), before the player update
        // reads the pad. A boss's death is that switch-off once it has been alive for 5 seconds; 3 seconds later (its
        // death animation) it is taken out of the level, which also takes its health bar off the screen.
        if (!g_dbg.inLevel)
        {
            g_spawnedThisLevel = false;
            g_debugBosses.clear();
        }
        else
        {
            // A boss that took itself out of the level is dead too: its health bar goes once no boss is left.
            const size_t before = g_debugBosses.size();
            g_debugBosses.erase(std::remove_if(g_debugBosses.begin(), g_debugBosses.end(),
                                               [](const DebugBoss &b) { return rd32(b.obj) != b.handle; }),
                                g_debugBosses.end());
            if (g_debugBosses.size() != before &&
                std::none_of(g_debugBosses.begin(), g_debugBosses.end(), [](const DebugBoss &o) { return o.removeAt == 0u; }))
                g_bossBarCleanup = true;
            if ((!g_debugBosses.empty() || g_survival.active) && rd32(kPlayerControlFlag) == 0u)
            {
                wr32(kPlayerControlFlag, 1u);
                for (DebugBoss &b : g_debugBosses)
                    if (b.removeAt == 0u && g_dbg.padFrames - b.born >= 60u)
                    {
                        b.removeAt = g_dbg.padFrames + 180u;
                        logf("debug: debug boss (object 0x%07X) died: controls kept on, removal in 3 s", b.obj);
                    }
            }
        }
        if (g_dbg.infinite && g_dbg.inLevel)
        {
            wr32(gameVarAddr(kVarLives), 99u);
            wr32(gameVarAddr(kVarKoins), 9999u);
            wr32(gameVarAddr(kVarKeys), 9u);
        }
        {
            // A new level's objects can reuse the old ones' addresses and handles: forget the old level's removals.
            // (The level VM is at the same address in every level: a level change shows as the exit code.)
            if (!g_dbg.inLevel || rd32(kLevelExitCodeAddr) != 0)
            {
                g_despawned.clear();
                unparkAll();
            }
            // A death resets the level's objects without a level change: parked objects go back first.
            static uint32_t lastLives = 0;
            const uint32_t livesNow = rd32(gameVarAddr(kVarLives));
            if (livesNow < lastLives)
                unparkAll();
            lastLives = livesNow;
        }
        for (size_t i = 0; i < g_despawned.size();)
        {
            const auto [obj, handle] = g_despawned[i];
            if (rd32(obj) != handle)
            {
                g_despawned.erase(g_despawned.begin() + static_cast<std::ptrdiff_t>(i));
                continue;
            }
            // The actor is looked up every frame: a spawned enemy's entity swaps it once its model has loaded.
            const uint32_t entity = rd32(obj + 0xB8u);
            const uint32_t actor = entity && (entity & PS2_RAM_MASK) == entity ? rd32(entity) : 0u;
            if (actor >= 0x00100000u && (actor & PS2_RAM_MASK) == actor)
            {
                const float away = 100000.0f;
                std::memcpy(mem(actor + 0xA0u), &away, 4);
            }
            ++i;
        }
    }

    // The interface's on-screen displays (boss health bars): list at interface+0x6B60 (count), +0x6B68 (array); each
    // {kind, vtable, object, ...}. `remove(ref)` true removes one; nullptr removes them all. Logs every one it sees.
    int removeDisplays(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime,
                       const std::function<bool(uint32_t)> *remove, const char *why)
    {
        constexpr uint32_t kIface = GA(0x0025B9E0u), kRemoveFn = GA(0x001493C0u);
        auto inRam = [](uint32_t a) { return a != 0 && (a & PS2_RAM_MASK) == a; };
        const uint32_t count = rd32(kIface + 0x6B60u), arr = rd32(kIface + 0x6B68u);
        int gone = 0;
        for (uint32_t k = count; inRam(arr) && count < 64u && k-- > 0;)
        {
            const uint32_t disp = rd32(arr + 4u * k);
            if (!inRam(disp))
                continue;
            const uint32_t ref = rd32(disp + 8u);
            const bool go = !remove || (*remove)(ref);
            logf("debug:   display %u/%u (%s): 0x%07X kind %u object 0x%07X%s", k, count, why, disp, rd32(disp), ref,
                 go ? " -> removed" : "");
            if (go)
            {
                uint32_t r = 0;
                callGuest(rdram, ctx, runtime, kRemoveFn, kIface, disp, 0, r);
                ++gone;
            }
        }
        if (count == 0)
            logf("debug:   no on-screen displays (%s)", why);
        return gone;
    }

    // Takes one object out of the level the way the game does (destroyLevelObject), together with every on-screen
    // display pointing at it first (a boss's health bar).
    constexpr uint32_t kInterfaceAddr = GA(0x0025B9E0u), kRemoveDisplayFn = GA(0x001493C0u);
    void removeLevelObject(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t vm, uint32_t obj,
                           uint32_t handle, bool detach = true)
    {
        auto inRam = [](uint32_t a) { return a != 0 && (a & PS2_RAM_MASK) == a; };
        const struct
        {
            uint32_t obj, handle;
        } b{obj, handle};
        if (rd32(obj) != handle)
            return;
        const uint32_t entity = rd32(b.obj + 0xB8u);
        const uint32_t actor = inRam(entity) ? rd32(entity) : 0u;
        // Its health bar: every display pointing at the object, its entity or its actor.
        {
            const std::function<bool(uint32_t)> mine = [&](uint32_t ref)
            { return ref == b.obj || ref == entity || (actor && ref == actor); };
            removeDisplays(rdram, ctx, runtime, &mine, "object removed");
        }
        if (rd32(rd32(b.obj + 0xB4u)) == kGlutterscumType)
            parkLevelObject(rdram, ctx, runtime, vm, b.obj);
        else
            destroyLevelObject(rdram, ctx, runtime, vm, b.obj, detach);
    }

    // Maximo's feet: gPlayer+0x1F0, rebuilt by the player update every frame (gPlayer+0x1A0 is the point the camera
    // follows, above and ahead of him).
    bool maximoFeet(float out[3])
    {
        std::memcpy(out, mem(kPlayerAddr + 0x1F0u), 12);
        return std::isfinite(out[0]) && std::isfinite(out[1]) && std::isfinite(out[2]) &&
               std::fabs(out[0]) + std::fabs(out[1]) + std::fabs(out[2]) > 0.01f &&
               std::fabs(out[0]) < 100000.0f && std::fabs(out[1]) < 100000.0f && std::fabs(out[2]) < 100000.0f;
    }

    // Survival's own inventory: what a new game gives (Player::InitNewGame, func_00129360: GameVars 0-16 from the
    // table at 0x1E0110, every ability off, flags 0x12) with the health and armor a new life gets (func_001293D0:
    // armor 2, health twice the value at 0x238ABC); his lives stay as they are. Whatever he brought (armor, abilities,
    // koins, keys) is put back when survival ends.
    constexpr uint32_t kSurvivalPlayer = GA(0x00263540u);
    void survivalSaveInventory();
    void survivalDefaultInventory(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime);
    void survivalRestoreInventory();

    // ---- SURVIVAL MODE -------------------------------------------------------------------------------------------
    // Starting it takes Maximo to a random boss arena (not the Queen's), takes that arena's boss out, and sends waves
    // of ordinary enemies at him, spawned a little away from him. Wave n has 3 + n enemies (12 at most), stronger
    // classes from wave 4 (class 2) and 7 (class 3), and every fifth wave also brings a boss when one is loaded there.
    // The next wave comes 4 seconds after the last enemy of a wave dies. The run ends when Maximo loses a life or the
    // level changes; the best wave is kept in survival_best.txt. While it runs the level's load-class limits (the
    // object budget) are raised, so the script VM keeps running every enemy instead of dropping some.
    struct SurvivalArena
    {
        int row;
        uint32_t boss;
    };
    constexpr SurvivalArena kSurvivalArenas[] = {{5, 0x29u}, {11, 0x2Eu}, {17, 0x82u}, {23, 0x8Eu}, {28, 0x9Cu}};
    constexpr uint32_t kSurvivalBudget = 1000u;
    constexpr float kLevelArenaRadius = 80.0f;
    constexpr uint32_t kEndColumnType = 0x4Du; // END_OF_SUBLEVEL_COLUMN, the purple lightning column
    constexpr uint32_t kKnightWallType = 0x8Cu; // the Dark Knight's wall
    constexpr int kRingPieces = 16;                // the ring's wall pieces (fewer: less frame time)
    // PSound::PlayMusic (func_0016D9F0(0x262704, track)); the track of a level is LevelTable row +0x12. The boss
    // tracks: 17 G_BOSS, 18 I_BOSS, 19 S_BOSS, 20 U_BOSS, 21 C_BOSS1 (the King). (22 is the Queen's.)
    constexpr uint32_t kPlayMusicFn = GA(0x0016D9F0u), kSoundManagerAddr = GA(0x00262704u);
    constexpr uint32_t kStopMusicFn = GA(0x0016E120u), kSoundQueueCount = GA(0x00239104u); // PSound stop; the command queue's length
    constexpr int kBossMusic[] = {17, 18, 19, 20, 21};
    constexpr int kOrdinaryEnemyCount = 21; // kEnemyTypes[0..20]: skeletons to axe guards
    // The properties a level record gives each survival enemy (the most common record of the type on the disc; its
    // sight / chase radii, the float properties, at least 25 so they come for Maximo from where they spawn). The
    // sublevel property is 0: every one of these scripts starts with CheckSubLevelCompleted(that property) and
    // removes itself when the save has that sublevel done - the crocodile (3), goat devil (2), prisoner and pirate
    // records chosen here named sublevels already completed, so those died two frames after spawning.
    struct EnemyRecordDefaults
    {
        uint32_t id;
        std::vector<RecordProp> props;
    };
    const std::vector<EnemyRecordDefaults> &enemyRecordDefaults()
    {
        static const std::vector<EnemyRecordDefaults> table = {
        {0x47u, {{7u, 0u, 0x00000001u}, {11u, 1u, 0x41C80000u}, {11u, 2u, 0x41C80000u}, {11u, 3u, 0x41C80000u}, {7u, 4u, 0x00000000u}, {7u, 5u, 0x00000000u}, {1u, 8u, 0x00000001u}, {1u, 9u, 0x00000000u}}},
        {0x2Fu, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41F00000u}, {11u, 2u, 0x41F00000u}, {11u, 3u, 0x42200000u}, {7u, 4u, 0x00000000u}, {7u, 5u, 0x00000000u}}},
        {0x39u, {{7u, 0u, 0x00000002u}, {11u, 1u, 0x42200000u}, {11u, 2u, 0x42480000u}, {11u, 3u, 0x42700000u}, {7u, 4u, 0x00000000u}, {7u, 5u, 0x00000000u}, {1u, 6u, 0x00000000u}}},
        {0x54u, {{7u, 0u, 0x00000001u}, {11u, 1u, 0x41C80000u}, {11u, 2u, 0x41C80000u}, {7u, 3u, 0x00000000u}, {7u, 4u, 0x00000000u}}},
        {0x49u, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41F00000u}, {11u, 2u, 0x41C80000u}, {11u, 3u, 0x420C0000u}, {7u, 4u, 0x00000000u}, {1u, 5u, 0x00000000u}, {7u, 6u, 0x00000000u}}},
        {0x3Au, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41F00000u}, {11u, 2u, 0x41C80000u}, {11u, 3u, 0x42200000u}, {7u, 4u, 0x00000000u}, {7u, 5u, 0x00000000u}, {1u, 6u, 0x00000000u}, {1u, 7u, 0x00000000u}}},
        {0x48u, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41F00000u}, {11u, 2u, 0x41C80000u}, {11u, 3u, 0x42200000u}, {7u, 4u, 0x00000000u}, {7u, 5u, 0x00000000u}, {1u, 6u, 0x00000001u}, {1u, 7u, 0x00000000u}}},
        {0x6Au, {{4u, 0u, 0x41A00000u}, {7u, 1u, 0x00000003u}, {7u, 2u, 0x00000000u}, {1u, 4u, 0x00000000u}}},
        {0x10u, {{14u, 0u, 0x00000000u}, {11u, 1u, 0x420C0000u}, {11u, 2u, 0x420C0000u}, {11u, 3u, 0x42340000u}, {1u, 4u, 0x00000001u}, {7u, 5u, 0x00000000u}, {7u, 6u, 0x00000000u}, {1u, 7u, 0x00000000u}}},
        {0x19u, {{11u, 1u, 0x41C80000u}, {11u, 2u, 0x41C80000u}, {11u, 3u, 0x41C80000u}, {7u, 4u, 0x00000000u}, {1u, 5u, 0x00000000u}}},
        {0x0Du, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41C80000u}, {11u, 2u, 0x41C80000u}, {11u, 3u, 0x41C80000u}, {7u, 4u, 0x00000000u}}},
        {0x1Du, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41C80000u}, {11u, 2u, 0x42200000u}, {7u, 3u, 0x00000000u}}},
        {0x13u, {{11u, 0u, 0x42200000u}, {11u, 1u, 0x41F00000u}, {11u, 2u, 0x42340000u}, {7u, 3u, 0x00000000u}, {7u, 4u, 0x00000000u}}},
        {0x27u, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41F00000u}, {11u, 2u, 0x420C0000u}, {11u, 3u, 0x42200000u}, {7u, 4u, 0x00000000u}, {7u, 5u, 0x00000000u}}},
        {0x08u, {{7u, 0u, 0x00000001u}, {11u, 1u, 0x41F00000u}, {11u, 2u, 0x42200000u}, {7u, 3u, 0x00000000u}}},
        {0x58u, {{11u, 0u, 0x42200000u}, {11u, 1u, 0x41C80000u}, {11u, 2u, 0x41F00000u}, {7u, 3u, 0x00000000u}}},
        {0x53u, {{7u, 0u, 0x00000000u}, {11u, 1u, 0x41C80000u}, {11u, 2u, 0x41C80000u}, {11u, 3u, 0x41F00000u}, {7u, 4u, 0x00000000u}, {7u, 5u, 0x00000000u}}},
        {0x5Du, {{7u, 0u, 0x00000000u}, {12u, 1u, 0x42200000u}, {12u, 5u, 0x42480000u}, {7u, 9u, 0x00000000u}}}, // (its two wake-up spheres: radius 40 and 50 around where it is made)
        };
        return table;
    }
    // In survival every distance property (the float ones: how far an enemy notices, follows and keeps Maximo) is
    // at least 120 units, more than any arena is wide: with the record's 25-60 an enemy Maximo ran away from went
    // back to sleep where it stood and never came after him again.
    const std::vector<RecordProp> *enemyDefaultProps(uint32_t id)
    {
        static std::vector<EnemyRecordDefaults> survivalTable;
        if (survivalTable.empty())
        {
            survivalTable = enemyRecordDefaults();
            const float farDist = 120.0f;
            uint32_t farBits;
            std::memcpy(&farBits, &farDist, 4);
            for (EnemyRecordDefaults &e : survivalTable)
                for (RecordProp &p : e.props)
                    if (p.type == 11u)
                    {
                        float v;
                        std::memcpy(&v, &p.value, 4);
                        if (!(v >= farDist))
                            p.value = farBits;
                    }
        }
        for (const EnemyRecordDefaults &e : survivalTable)
            if (e.id == id)
                return &e.props;
        return nullptr;
    }
    constexpr const char *kSurvivalBestFile = "survival_best.txt";

    void survivalLoadBest()
    {
        if (g_survival.bestLoaded)
            return;
        g_survival.bestLoaded = true;
        if (FILE *f = std::fopen(persistentPath(kSurvivalBestFile).c_str(), "r"))
        {
            if (std::fscanf(f, "%d", &g_survival.best) != 1)
                g_survival.best = 0;
            std::fclose(f);
        }
    }
    void survivalSaveBest()
    {
        if (FILE *f = std::fopen(persistentPath(kSurvivalBestFile).c_str(), "w"))
        {
            std::fprintf(f, "%d\n", g_survival.best);
            std::fclose(f);
        }
    }
    int survivalAlive()
    {
        int n = 0;
        for (const SurvivalFoe &f : g_survival.foes)
            if (rd32(f.obj) == f.handle && rd32(f.obj + 0xB4u) == f.tmpl)
                ++n;
        return n;
    }
    void survivalSaveInventory()
    {
        if (g_survival.invSaved)
            return;
        std::memcpy(g_survival.invVars.data(), mem(kSurvivalPlayer + 0x48u), g_survival.invVars.size());
        std::memcpy(g_survival.invAbil.data(), mem(kSurvivalPlayer + 0x424u), g_survival.invAbil.size());
        g_survival.invFlags = rd32(kSurvivalPlayer + 0x24u);
        g_survival.invB6 = *mem(kSurvivalPlayer + 0xB6u);
        g_survival.invSaved = true;
        logf("debug: survival: Maximo's inventory saved");
    }
    void survivalDefaultInventory(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t lives = rd32(gameVarAddr(kVarLives));
        for (uint32_t i = 0; i < 17; ++i)
        {
            const int16_t v = static_cast<int16_t>(rd32(GA(0x001E0110u) + 6u * i) & 0xFFFFu);
            wr32(kSurvivalPlayer + 0x48u + 4u * i, static_cast<uint32_t>(static_cast<int32_t>(v)));
        }
        wr32(gameVarAddr(3), 2u);                         // armor
        wr32(gameVarAddr(2), rd32(GA(0x00238ABCu)) * 2u);     // health
        wr32(gameVarAddr(kVarLives), lives);
        for (uint32_t i = 0; i < 31; ++i)
        {
            *mem(kSurvivalPlayer + 0x424u + 2u * i) = 0xFF;
            *mem(kSurvivalPlayer + 0x425u + 2u * i) = 0;
        }
        wr32(kSurvivalPlayer + 0x24u, 0x12u);
        *mem(kSurvivalPlayer + 0xB6u) = 0;
        // Survival starts with 100 souls and every ability (each given with SetAbility, as its pickup does): all the
        // skills of SPAWN SKILL but the full-health heart, and no sword enchantment (those come from the chests).
        wr32(gameVarAddr(kVarSouls), 100u);
        // Shield points (GameVar 10) are capped at five times GameVar 14 (GameVarAdd, func_00129FE0: 2 at a new game,
        // so 10 points - the most he already had, and SHIELD STAMINA +5 added nothing). Survival gives the top of 14
        // (4: up to 20 points), with the shield full.
        wr32(gameVarAddr(14), 4u);
        int given = 0;
        for (const PickupType &k : kSkills)
        {
            if (k.id == 22u)
                continue;
            uint32_t v0 = 0;
            if (callGuestArgs(rdram, ctx, runtime, kSetAbilityFn, {kGamePlayerAddr, k.id, 1u, 0, 0, 0, 0, 0}, v0))
                ++given;
        }
        wr32(gameVarAddr(10), 20u);
        logf("debug: survival: default inventory (health %u, armor %u, lives %u, souls %u, %d abilities)",
             rd32(gameVarAddr(2)), rd32(gameVarAddr(3)), lives, rd32(gameVarAddr(kVarSouls)), given);
    }
    void survivalRestoreInventory()
    {
        if (!g_survival.invSaved)
            return;
        std::memcpy(mem(kSurvivalPlayer + 0x48u), g_survival.invVars.data(), g_survival.invVars.size());
        std::memcpy(mem(kSurvivalPlayer + 0x424u), g_survival.invAbil.data(), g_survival.invAbil.size());
        wr32(kSurvivalPlayer + 0x24u, g_survival.invFlags);
        *mem(kSurvivalPlayer + 0xB6u) = g_survival.invB6;
        g_survival.invSaved = false;
        logf("debug: survival: Maximo's own inventory put back");
    }

    void survivalEnd(const char *why)
    {
        if (!g_survival.active)
            return;
        g_survival.active = false;
        g_survival.phase = 0;
        const int reached = std::max(0, g_survival.wave - (survivalAlive() > 0 ? 1 : 0));
        const bool record = reached > g_survival.best;
        if (record)
        {
            g_survival.best = reached;
            survivalSaveBest();
        }
        char b[128];
        const uint32_t t = static_cast<uint32_t>(g_survival.seconds);
        std::snprintf(b, sizeof b, "SURVIVAL OVER (%s): %d WAVES, %d KILLS, %u:%02u%s", why, reached, g_survival.kills,
                      t / 60u, t % 60u, record ? "  NEW BEST!" : "");
        g_survival.result = b;
        g_dbg.spawnMessage = b;
        logf("debug: survival over (%s): %d waves survived, %d kills, best %d", why, reached, g_survival.kills,
             g_survival.best);
        if (std::strcmp(why, "MAXIMO LOST A LIFE") != 0)
            survivalRestoreInventory(); // (after a death the run restarts with the default one again)
    }
    void survivalStart(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, int forceRow = -1)
    {
        survivalLoadBest();
        if (!g_dbg.inLevel || !g_dbg.haveGameCamera)
        {
            g_dbg.spawnMessage = "SURVIVAL: ONLY IN A LEVEL";
            return;
        }
        static std::mt19937 arenaRng(static_cast<uint32_t>(std::chrono::steady_clock::now().time_since_epoch().count()));
        // (a restart after a death keeps the kind of run it was)
        const bool here = forceRow >= 0 ? g_survival.levelMode : g_survivalHere;
        SurvivalArena arena =
            kSurvivalArenas[std::uniform_int_distribution<size_t>(0, std::size(kSurvivalArenas) - 1)(arenaRng)];
        for (const SurvivalArena &a : kSurvivalArenas)
            if (a.row == forceRow)
                arena = a; // a restart after a death: the same arena
        const int current = static_cast<int>(rd32(kCurrentLevelAddr));
        float feetStart[3] = {0.0f, 0.0f, 0.0f};
        if (here)
        {
            if (!maximoFeet(feetStart))
            {
                g_dbg.spawnMessage = "SURVIVAL: MAXIMO NOT FOUND";
                return;
            }
            arena = {current, 0u};
        }
        else if (current != arena.row && !goToLevel(rdram, ctx, runtime, arena.row))
            return; // goToLevel left the reason in spawnMessage
        if (forceRow < 0)
        {
            g_survival.startRow = current;
            survivalSaveInventory();
        }
        g_survival.returnRow = -1;
        g_survival.phase = current == arena.row ? 2 : 1;
        g_survival.arenaRow = arena.row;
        g_survival.arenaBoss = arena.boss;
        g_survival.leftVm = rd32(kLevelVmPtr);
        g_survival.phaseFrame = g_dbg.padFrames;
        g_survival.active = true;
        if (forceRow < 0) // (a restart after a death keeps counting)
            g_survival.seconds = 0.0;
        g_survival.clockValid = false;
        g_survival.wave = 0;
        g_survival.kills = 0;
        g_survival.foes.clear();
        g_survival.trail.clear();
        g_survival.walked.clear();
        g_survival.pending.clear();
        g_survival.chestWanted = 0;
        g_survival.chests.clear();
        g_survival.haveMid = false;
        g_survival.badTypes.clear();
        g_survival.replace = 0;
        g_survival.result.clear();
        g_survival.vm = rd32(kLevelVmPtr);
        g_survival.lives = rd32(gameVarAddr(kVarLives));
        g_survival.nextWaveAt = 0;
        g_survival.levelMode = here;
        g_survival.ringMade = false; // (columns of an earlier ring stay listed: they still must not end the level)
        if (here)
        {
            // Straight to the waves (2 seconds), with the ring's middle as the arena's middle.
            std::memcpy(g_survival.ringCenter, feetStart, sizeof feetStart);
            g_survival.arenaMid = {feetStart[0], feetStart[1], feetStart[2]};
            g_survival.haveMid = true;
            g_survival.phase = 2;
            g_survival.phaseFrame = g_dbg.padFrames - 180u;
            g_dbg.spawnMessage = "SURVIVAL HERE: THE PURPLE RING IS THE ARENA";
            logf("debug: survival started in this level (%d %s) at (%.1f, %.1f, %.1f), ring radius %.0f", current,
                 kLevelNames[current], feetStart[0], feetStart[1], feetStart[2], kLevelArenaRadius);
            return;
        }
        g_dbg.spawnMessage = std::string("SURVIVAL: GOING TO ") + kLevelNames[arena.row];
        logf("debug: survival started: arena %d %s (lives %u, best %d)", arena.row, kLevelNames[arena.row],
             g_survival.lives, g_survival.best);
    }

    // extra > 0: that many replacements for enemies that died by themselves (same wave, no boss).
    void survivalSpawnWave(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, int extra = 0)
    {
        static std::mt19937 rng(static_cast<uint32_t>(std::chrono::steady_clock::now().time_since_epoch().count()));
        const uint32_t vm = rd32(kLevelVmPtr);
        struct Pick
        {
            int index;
            uint32_t tmpl;
        };
        std::vector<Pick> ordinary, bosses;
        for (int i = 0; i < static_cast<int>(std::size(kEnemyTypes)); ++i)
        {
            const bool boss = std::strcmp(kEnemyTypes[i].home, "BOSS") == 0;
            // Not in survival: the axe guard and raven, and a type whose spawns died by themselves three times this
            // run. (The Doomed Soul rises when Maximo comes within its wake-up spheres: it gets them below. The ghost
            // is in: the cross-world tool adds it to every arena.)
            const uint32_t id = kEnemyTypes[i].id;
            if ((i >= kOrdinaryEnemyCount && !boss) || id == 0x16u || id == 0x37u || std::count(g_survival.badTypes.begin(), g_survival.badTypes.end(), id) >= 3)
                continue;
            uint32_t tmpl = 0;
            if (callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, kEnemyTypes[i].id, 0, tmpl) && tmpl)
                (boss ? bosses : ordinary).push_back({i, tmpl});
        }
        if (ordinary.empty() && bosses.empty())
        {
            survivalEnd("NO ENEMIES LOADED HERE");
            return;
        }
        if (extra > 0 && ordinary.empty())
            return;
        if (extra == 0)
            ++g_survival.wave;
        const int wave = g_survival.wave;
        const bool bossWave = extra == 0 && wave % 5 == 0 && !bosses.empty();
        if (extra == 0)
            g_survivalBossWave = bossWave;
        // Wave n has n enemies (wave 1 one, wave 9 nine, wave 15 fifteen...), 60 at most; a boss wave is the boss alone.
        const int count = extra > 0 ? extra : bossWave ? 0 : std::min(wave, 60);
        // Around Maximo's feet (the camera's target was not him: right after the arena camera it sat 200 units up,
        // and wave 1 spawned in the sky).
        float center[3];
        if (!maximoFeet(center))
            spawnAnchor(center);
        const uint32_t savedSp = reg(ctx, 29);
        const uint32_t sp = (savedSp - 0x200u) & ~0xFu;
        SET_GPR_U32(ctx, 29, sp);
        g_survival.foes.erase(std::remove_if(g_survival.foes.begin(), g_survival.foes.end(),
                                             [](const SurvivalFoe &f)
                                             { return rd32(f.obj) != f.handle || rd32(f.obj + 0xB4u) != f.tmpl; }),
                              g_survival.foes.end());
        // Classes: 1-2 at first, up to 3 from wave 3, and no class 1 from wave 6 (class 1 is the weakest: most of
        // those die in one hit).
        const int maxTier = wave >= 3 ? 3 : 2;
        const int minTier = wave >= 6 ? 2 : 1;
        std::string why;
        int made = 0;
        const float turn = std::uniform_real_distribution<float>(0.0f, 6.2831853f)(rng);
        // The four sides of the arena: from the arena's middle (where its boss stood, else where Maximo first stood),
        // the walked spot (on its floor) farthest out toward +X, -X, +Z and -Z (within 30 units, inside a 90 degree
        // cone each). Enemies come in at the sides away from Maximo (8 units or more), a little scattered; a boss
        // at the side farthest from him.
        // Where they come in: spots Maximo himself has stood on in this arena (sure to be its floor), away from its
        // edges - within three quarters of the farthest one from the middle - at least 6 units from Maximo and at
        // about his height (no ledges or pits). The farthest walked spots, used before, were the arena's edges, and
        // enemies made there fell out of it or stuck in its walls. Only while too few have been walked do the spots
        // made around the boss's place (4 and 7 units from the middle) help out. Each enemy takes a random spot, a
        // little scattered; a boss the one farthest from Maximo.
        std::vector<std::array<float, 3>> spots;
        {
            const std::array<float, 3> mid = g_survival.haveMid ? g_survival.arenaMid
                                             : !g_survival.walked.empty() ? g_survival.walked.front()
                                             : std::array<float, 3>{center[0], center[1], center[2]};
            auto d2 = [](const std::array<float, 3> &p, const float *q)
            { return (p[0] - q[0]) * (p[0] - q[0]) + (p[2] - q[2]) * (p[2] - q[2]); };
            float maxR = 0.0f;
            for (const std::array<float, 3> &t : g_survival.walked)
                maxR = std::max(maxR, std::sqrt(d2(t, mid.data())));
            maxR = std::min(maxR, 30.0f);
            auto take = [&](const std::vector<std::array<float, 3>> &from, float limit)
            {
                for (const std::array<float, 3> &t : from)
                    if (d2(t, center) >= 36.0f && std::fabs(t[1] - center[1]) < 6.0f &&
                        std::sqrt(d2(t, mid.data())) <= limit)
                        spots.push_back(t);
            };
            if (g_survival.levelMode)
                maxR = std::min(maxR, kLevelArenaRadius - 1.0f) / 0.75f; // (inside the ring)
            take(g_survival.walked, std::max(6.0f, 0.75f * maxR));
            if (spots.size() < 3u)
                take(g_survival.trail, 8.0f);
            if (spots.empty())
                for (const std::array<float, 3> &t : g_survival.walked)
                    if (d2(t, center) >= 16.0f)
                        spots.push_back(t);
        }
        std::shuffle(spots.begin(), spots.end(), rng);
        if (bossWave && !spots.empty())
            std::iter_swap(spots.begin(),
                           std::max_element(spots.begin(), spots.end(),
                                            [&](const std::array<float, 3> &a, const std::array<float, 3> &b)
                                            {
                                                const float da = (a[0] - center[0]) * (a[0] - center[0]) +
                                                                 (a[2] - center[2]) * (a[2] - center[2]);
                                                const float db = (b[0] - center[0]) * (b[0] - center[0]) +
                                                                 (b[2] - center[2]) * (b[2] - center[2]);
                                                return da < db;
                                            }));
        for (int n = 0; n < count + (bossWave ? 1 : 0); ++n)
        {
            const bool boss = bossWave && n == count;
            const std::vector<Pick> &pool = boss || ordinary.empty() ? bosses : ordinary;
            const Pick p = pool[std::uniform_int_distribution<size_t>(0, pool.size() - 1)(rng)];
            // At one of the arena's sides (above), a little scattered; a ring around Maximo only while there is no
            // walked spot at all.
            float pos[3];
            if (!spots.empty())
            {
                const std::array<float, 3> &t =
                    boss ? spots.front() : spots[std::uniform_int_distribution<size_t>(0, spots.size() - 1)(rng)];
                const float jitter = std::uniform_real_distribution<float>(0.0f, 1.0f)(rng);
                const float a = std::uniform_real_distribution<float>(0.0f, 6.2831853f)(rng);
                pos[0] = t[0] + jitter * std::cos(a);
                pos[1] = t[1] + 1.0f;
                pos[2] = t[2] + jitter * std::sin(a);
            }
            else
            {
                const float angle = turn + 6.2831853f * static_cast<float>(n) / static_cast<float>(count + 1);
                pos[0] = center[0] + 8.0f * std::cos(angle);
                pos[1] = center[1] + 1.0f;
                pos[2] = center[2] + 8.0f * std::sin(angle);
            }
            RecordProp classProp{};
            const RecordProp *classPtr = nullptr;
            for (const EnemyClassInfo &c : kEnemyClasses)
                if (c.id == kEnemyTypes[p.index].id)
                {
                    const int top = std::min(maxTier, c.count);
                    const int tier = std::uniform_int_distribution<int>(std::min(minTier, top), top)(rng);
                    classProp = {c.propType, 0u, c.values[tier - 1]};
                    classPtr = &classProp;
                }
            // (spawnOne returns the handle; the object is the one it just recorded.)
            // The Queen collects her eight waypoints with FindApparitions when she starts: without them she never
            // appeared. The menu lays out a ring of them around her; so does survival.
            if (boss && kEnemyTypes[p.index].id == kQueenType)
                spawnQueenWaypoints(rdram, ctx, runtime, vm, pos, sp + 0x20u);
            const std::vector<RecordProp> *recordProps = boss ? nullptr : enemyDefaultProps(kEnemyTypes[p.index].id);
            if (!spawnOne(rdram, ctx, runtime, vm, p.tmpl, pos, sp + 0x20u, why, recordProps, classPtr) ||
                g_spawnedObjects.empty())
                break;
            const SpawnedObject &made1 = g_spawnedObjects.back();
            if (boss)
                watchSurvivalBoss(made1.obj, made1.handle);
            {
                SurvivalFoe foe{made1.obj, made1.handle, made1.tmpl, g_dbg.padFrames, kEnemyTypes[p.index].id};
                foe.last[0] = pos[0];
                foe.last[1] = pos[1];
                foe.last[2] = pos[2];
                foe.lastMove = g_dbg.padFrames;
                g_survival.foes.push_back(foe);
            }
            ++made;
        }
        SET_GPR_U32(ctx, 29, savedSp);
        if (extra > 0)
        {
            logf("debug: survival: %d of %d replacements made", made, extra);
            return;
        }
        g_survival.waveStarted = g_dbg.padFrames;
        g_survival.lastProgress = g_dbg.padFrames;
        g_survival.nextWaveAt = 0;
        char b[96];
        if (bossWave)
            std::snprintf(b, sizeof b, "WAVE %d: A BOSS!", wave);
        else
            std::snprintf(b, sizeof b, "WAVE %d: %d ENEMIES", wave, made);
        g_dbg.spawnMessage = b;
        logf("debug: survival wave %d: %d made (%zu ordinary, %zu boss classes here; %zu of %zu spots as sides)%s%s",
             wave, made, ordinary.size(), bosses.size(), spots.size(), g_survival.trail.size(),
             why.empty() ? "" : "; stopped: ", why.c_str());
        if (made == 0)
            survivalEnd("COULD NOT SPAWN");
    }



    // The ground under a point: func_0014F050(_, pos, query) - what GetGroundPositionXYZ (script API slot 110) calls -
    // casts down from pos and writes the ground's height into pos.y (-1000 when nothing is below). Its query is set up
    // as that API does (0x02001044 at +0x74, 1 at +0x79, -1000 at +0x40, zeros). Cast from 3 units above the point;
    // a ground more than 40 units below is not taken.
    bool groundAt(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, float pos[3], float lift = 3.0f)
    {
        const uint32_t savedSp = reg(ctx, 29);
        const uint32_t scratch = (savedSp - 0x400u) & ~0xFu;
        const uint32_t p = scratch, q = scratch + 0x20u;
        const float start[4] = {pos[0], pos[1] + lift, pos[2], 1.0f};
        std::memcpy(mem(p), start, 16);
        std::memset(mem(q), 0, 0x80);
        wr32(q + 0x74u, 0x02001044u);
        *mem(q + 0x79u) = 1;
        const float noGround = -1000.0f;
        std::memcpy(mem(q + 0x40u), &noGround, 4);
        SET_GPR_U32(ctx, 29, scratch - 0x40u);
        uint32_t v0 = 0;
        const bool ok = callGuestArgs(rdram, ctx, runtime, GA(0x0014F050u), {0u, p, q, 0, 0, 0, 0, 0}, v0);
        SET_GPR_U32(ctx, 29, savedSp);
        float y = 0.0f;
        std::memcpy(&y, mem(p + 4u), 4);
        if (!ok || !std::isfinite(y) || y < pos[1] - 40.0f || y > pos[1] + lift + 0.5f)
            return false;
        pos[1] = y;
        return true;
    }
    std::mt19937 &survivalRng()
    {
        static std::mt19937 rng(static_cast<uint32_t>(std::chrono::steady_clock::now().time_since_epoch().count() ^ 0x5EEDu));
        return rng;
    }
    // One object of a class (looked up first, as spawnPickups does), made from record properties at a spot.
    // Returns the made object's spawn record, or nullptr.
    const SpawnedObject *survivalSpawnThing(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t type,
                                            const std::vector<RecordProp> &props, const float pos[3], const char *what)
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        uint32_t tmpl = 0;
        if (!vm || !callGuest(rdram, ctx, runtime, kClassLookupFn, vm + 0xB8u, type, 0, tmpl) || !tmpl)
        {
            logf("debug: survival: %s (class 0x%02X) is not loaded here", what, type);
            return nullptr;
        }
        const size_t before = g_spawnedObjects.size();
        const uint32_t savedSp = reg(ctx, 29);
        const uint32_t sp = (savedSp - 0x200u) & ~0xFu;
        SET_GPR_U32(ctx, 29, sp);
        std::string why;
        const bool ok = spawnOne(rdram, ctx, runtime, vm, tmpl, pos, sp + 0x20u, why, &props) != 0;
        SET_GPR_U32(ctx, 29, savedSp);
        if (!ok || g_spawnedObjects.size() == before)
        {
            logf("debug: survival: %s could not be made: %s", what, why.c_str());
            return nullptr;
        }
        logf("debug: survival: %s made at (%.1f, %.1f, %.1f)", what, pos[0], pos[1], pos[2]);
        return &g_spawnedObjects.back();
    }

    // Drops: every survival enemy that is killed has a small chance to leave a full-health potion or a piece of
    // armor where it died, and most leave nothing. About one drop in every nine kills: a full wave of 12 gives one
    // or two, a short early wave usually none. (Both classes are in every boss arena.)
    // Also the shield and sword point refills: the Ability pickups SHIELD STAMINA (13: +5 shield points, GameVar 10)
    // and SWORD STAMINA (24: +5 sword points, GameVar 11). In all about one kill in six drops something.
    constexpr int kDropHealthPercent = 5, kDropArmorPercent = 3, kDropShieldPercent = 4, kDropSwordPercent = 4;
    constexpr int kDropShieldItemPercent = 3; // the SHIELD pickup itself (ability 12: Maximo's shield back)
    void survivalDrop(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, const SurvivalFoe &f)
    {
        if (!f.haveAt || std::find(std::begin(kBossTypes), std::end(kBossTypes), f.type) != std::end(kBossTypes))
            return;
        const int roll = std::uniform_int_distribution<int>(0, 99)(survivalRng());
        uint32_t type = 0;
        const char *what = "";
        if (roll < kDropHealthPercent)
        {
            type = 0x51u;
            what = "drop: full health";
        }
        else if (roll < kDropHealthPercent + kDropArmorPercent)
        {
            type = 0x45u;
            what = "drop: armor";
        }
        uint32_t ability = 0;
        if (!type)
        {
            const int r2 = roll - kDropHealthPercent - kDropArmorPercent;
            if (r2 >= 0 && r2 < kDropShieldPercent)
            {
                ability = 13u;
                what = "drop: shield points";
            }
            else if (r2 >= kDropShieldPercent && r2 < kDropShieldPercent + kDropSwordPercent)
            {
                // Sword points are an enchantment's: GameVarAdd adds to GameVar 11 only while GameVar 1 (the
                // enchantment) is set, so without one this drop is shield points instead.
                ability = rd32(gameVarAddr(1)) != 0u ? 24u : 13u;
                what = ability == 24u ? "drop: sword points" : "drop: shield points";
            }
            else if (r2 >= kDropShieldPercent + kDropSwordPercent &&
                     r2 < kDropShieldPercent + kDropSwordPercent + kDropShieldItemPercent)
            {
                ability = 12u;
                what = "drop: shield";
            }
            else
                return;
            type = kAbilityType;
        }
        Survival::Pending p{type, ability ? abilityProps(ability) : itemProps(type), {f.at[0], f.at[1] + 1.0f, f.at[2]}, what};
        g_survival.pending.push_back(p);
    }

    // The chest before a boss wave, in the arena's middle: a wooden chest (class 0x25, which the cross-world tool
    // adds to every arena) with none of its usual contents. Its script spawns one prize for each of its properties
    // 16-31 that is set (decoded from the Wooden Chest's script: 16-18 gold and random drops, 19 the full-health heart,
    // 20 a full health, 21 an extra life, 23 the fire sword, 24 ice, 25 sun, 29 armageddon, 30 armor, 31 a key).
    // 60%: one of the four sword enchantments. 15%: armor. 15%: a mimic (property 6 = 1). 10%: nothing. The boss wave
    // comes 15 seconds after the wave before it ends, so there is time to open it.
    constexpr uint32_t kWoodenChestType = 0x25u;
    constexpr uint32_t kChestSwordProps[] = {23u, 24u, 25u, 29u};
    constexpr const char *kChestSwordNames[] = {"FIRE SWORD", "ICE SWORD", "SUN SWORD", "ARMAGEDDON SWORD"};
    constexpr uint64_t kChestWaitFrames = 15u * 60u;
    constexpr uint32_t kChestArmorProp = 30u;
    bool survivalSpawnChest(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        // On the floor near the middle: the spot Maximo has stood on nearest to the arena's middle (the boss's place
        // was not always on the floor: in Captain Cadaver's arena the chest hung in the air and could not be opened).
        float pos[3];
        if (!maximoFeet(pos))
        {
            if (g_survival.walked.empty() && !g_survival.haveMid)
                return false;
            const std::array<float, 3> &w = g_survival.walked.empty() ? g_survival.arenaMid : g_survival.walked.front();
            pos[0] = w[0];
            pos[1] = w[1];
            pos[2] = w[2];
        }
        {
            const float mid[3] = {g_survival.haveMid ? g_survival.arenaMid[0] : pos[0],
                                  g_survival.haveMid ? g_survival.arenaMid[1] : pos[1],
                                  g_survival.haveMid ? g_survival.arenaMid[2] : pos[2]};
            float best = 1e30f;
            for (const std::array<float, 3> &t : g_survival.walked)
            {
                const float d = (t[0] - mid[0]) * (t[0] - mid[0]) + (t[2] - mid[2]) * (t[2] - mid[2]);
                // (several chests: each at least 4 units from the others)
                const bool crowded = std::any_of(g_survival.chests.begin(), g_survival.chests.end(),
                                                 [&](const Survival::Chest &c)
                                                 {
                                                     const float cx = t[0] - c.at[0], cz = t[2] - c.at[2];
                                                     return cx * cx + cz * cz < 16.0f;
                                                 });
                if (crowded)
                    continue;
                if (d < best)
                {
                    best = d;
                    pos[0] = t[0];
                    pos[1] = t[1];
                    pos[2] = t[2];
                }
            }
            if (!groundAt(rdram, ctx, runtime, pos))
                logf("debug: survival: no ground found under the chest's spot");
            pos[1] += 0.2f;
        }
        const int roll = std::uniform_int_distribution<int>(0, 99)(survivalRng());
        const int kind = roll < 60 ? 1 : roll < 75 ? 4 : roll < 90 ? 2 : 3;
        const size_t sword = std::uniform_int_distribution<size_t>(0, std::size(kChestSwordProps) - 1)(survivalRng());
        std::vector<RecordProp> props = itemProps(kWoodenChestType);
        for (RecordProp &p : props)
        {
            if (p.type != 7u)
                continue;
            if (p.index >= 16u && p.index <= 32u)
                p.value = (kind == 1 && p.index == kChestSwordProps[sword]) || (kind == 4 && p.index == kChestArmorProp)
                              ? 1u
                              : 0u; // only the enchantment or the armor
            if (p.index == 6u)
                p.value = kind == 2 ? 1u : 0u; // the mimic
        }
        char what[64];
        std::snprintf(what, sizeof what, "chest (%s)",
                      kind == 1 ? kChestSwordNames[sword] : kind == 4 ? "armor" : kind == 2 ? "mimic" : "empty");
        const SpawnedObject *chest = survivalSpawnThing(rdram, ctx, runtime, kWoodenChestType, props, pos, what);
        if (!chest)
            return false;
        g_survival.chestObj = chest->obj;
        g_survival.chestHandle = chest->handle;
        g_survival.chestKind = kind;
        std::memcpy(g_survival.chestAt, pos, sizeof pos);
        g_survival.chests.push_back({{pos[0], pos[1], pos[2]}, kind});
        return true;
    }

    // Loot and souls during the waves (every other frame, over the level's object list):
    // - Pickups that appear in the arena (an enemy's own drops: gold, gems, keys, lives, abilities, its own health
    //   and armor) are taken away again: only survival's drops (survivalDrop, made by us) and the chest's sword
    //   enchantment (an ability within 4 units of the chest) stay, with the arena's own objects from the start.
    // - A soul (class 0x0E, the ghost's; 0x67, the bonus grave's) floats to Maximo and adds one when it reaches him
    //   (GameVarAdd(6, 1) in its script): it is put on Maximo at once, so it counts straight away.
    constexpr uint32_t kLootTypes[] = {0x20u, 0x21u, 0x46u, 0x52u, 0x4Eu, 0x51u, 0x45u, 0x65u, 0x2Bu, 0x4Bu, 0x6Cu};
    void survivalLootTick(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_survival.phase != 3 || g_dbg.padFrames % 2u != 0u)
            return;
        const uint32_t vm = rd32(kLevelVmPtr);
        if (!vm)
            return;
        std::vector<std::pair<uint32_t, uint32_t>> gone;
        for (uint32_t o = rd32(vm + 0x9Cu + 8u), guard = 0; o && (o & PS2_RAM_MASK) == o && guard < 20000;
             o = rd32(o + 0xBCu), ++guard)
        {
            const uint32_t handle = rd32(o);
            const uint32_t cls = rd32(o + 0xB4u);
            if (!cls || (cls & PS2_RAM_MASK) != cls || g_survival.baseline.count(handle))
                continue;
            const uint32_t type = rd32(cls);
            const uint32_t ent = rd32(o + 0xB8u);
            const uint32_t act = ent && (ent & PS2_RAM_MASK) == ent ? rd32(ent) : 0u;
            const bool haveAct = act && (act & PS2_RAM_MASK) == act;
            if (type == 0x0Eu || type == 0x67u)
            {
                // Counted the moment it appears (what its script adds when it reaches Maximo) and taken away: moving
                // it onto Maximo was not enough, its script keeps flying it in.
                const uint32_t souls = std::min<uint32_t>(999u, rd32(gameVarAddr(kVarSouls)) + 1u);
                wr32(gameVarAddr(kVarSouls), souls);
                gone.push_back({o, handle});
                g_survival.baseline.insert(handle);
                logf("debug: survival: a soul counted at once (%u souls)", souls);
                continue;
            }
            if (std::find(std::begin(kLootTypes), std::end(kLootTypes), type) == std::end(kLootTypes) ||
                isSpawnedHandle(handle))
                continue;
            if ((type == 0x20u || type == 0x45u) && haveAct)
            {
                // A chest's prize (its sword enchantment, an ability 0x20, or its armor 0x45) within 4 units: kept.
                float p[3];
                std::memcpy(p, mem(act + 0xA0u), 12);
                const int want = type == 0x20u ? 1 : 4;
                const bool fromChest = std::any_of(g_survival.chests.begin(), g_survival.chests.end(),
                                                   [&](const Survival::Chest &c)
                                                   {
                                                       const float dx = p[0] - c.at[0], dz = p[2] - c.at[2];
                                                       return c.kind == want && dx * dx + dz * dz < 16.0f;
                                                   });
                if (fromChest)
                {
                    g_survival.baseline.insert(handle);
                    g_dbg.spawnMessage = want == 1 ? "THE CHEST HAD A SWORD ENCHANTMENT!" : "THE CHEST HAD ARMOR!";
                    logf("debug: survival: a chest's %s appeared", want == 1 ? "sword enchantment" : "armor");
                    continue;
                }
            }
            gone.push_back({o, handle});
            logf("debug: survival: an enemy's drop (class 0x%02X) taken away", type);
        }
        for (const auto &g : gone)
            removeLevelObject(rdram, ctx, runtime, vm, g.first, g.second, false);
    }


    // ResetFreeze (script API slot 3) does func_00150140(entity, 0.0, 0.0): a boss's intro freezes Maximo (SetFreeze
    // on the player, 250-400) and itself while its camera plays. Done here for Maximo and the given entity.
    void survivalUnfreeze(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t other)
    {
        const float f12 = ctx->f[12], f13 = ctx->f[13];
        for (uint32_t ent : {rd32(kSurvivalPlayer), other})
        {
            if (!ent || (ent & PS2_RAM_MASK) != ent)
                continue;
            ctx->f[12] = 0.0f;
            ctx->f[13] = 0.0f;
            uint32_t v0 = 0;
            callGuest(rdram, ctx, runtime, GA(0x00150140u), ent, 0, 0, v0);
        }
        ctx->f[12] = f12;
        ctx->f[13] = f13;
    }


    // No boss cutscenes in survival: while a run is on, the bosses' cutscene calls are not passed on - CutScene_Start
    // (script API slot 184, func_00118550) and CutScene_SetupKingCamera (251, func_001188B0) - nor a boss's
    // SetFreeze (89, func_0010F530: its intro freezes Maximo and itself while the cutscene plays), and the intro bit
    // 0x40 of CameraSetBossFlag (232, func_001186F0: Camera::SetBossFlag, the intro camera that holds Maximo) is
    // dropped. The scripts go on as after their cutscene: CutScenePlaying answers 0 at once.
    constexpr uint32_t kCutSceneStartApiFn = GA(0x00118550u), kKingCameraApiFn = GA(0x001188B0u), kSetFreezeApiFn = GA(0x0010F530u),
                       kSetBossFlagApiFn = GA(0x001186F0u);
    bool survivalCallerIsBoss(uint32_t interp)
    {
        if (!interp || (interp & PS2_RAM_MASK) != interp)
            return false;
        const uint32_t obj = objectForHandle(rd32(interp + 0xBECu));
        const uint32_t cls = obj ? rd32(obj + 0xB4u) : 0u;
        const uint32_t type = cls && (cls & PS2_RAM_MASK) == cls ? rd32(cls) : 0u;
        return std::find(std::begin(kBossTypes), std::end(kBossTypes), type) != std::end(kBossTypes);
    }
    void survivalCutsceneEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_survival.active)
        {
            static int logged = 0;
            if (logged < 20)
            {
                ++logged;
                logf("debug: survival: a boss cutscene call (0x%06X) skipped", ctx->pc);
            }
            ret(ctx, 0);
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }
    void survivalSetFreezeEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_survival.active && survivalCallerIsBoss(reg(ctx, 4)))
        {
            ret(ctx, 0);
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }
    void survivalBossFlagEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t interp = reg(ctx, 4);
        if (g_survival.active && interp && (interp & PS2_RAM_MASK) == interp)
        {
            const uint32_t flags = rd32(interp + 0xBF0u) & ~0x40u;
            if (flags == 0u)
            {
                ret(ctx, 0);
                return;
            }
            wr32(interp + 0xBF0u, flags);
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }


    void survivalPendingTick(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_survival.phase != 3 || g_dbg.menuOpen)
            return;
        if (g_survival.chestWanted > 0)
        {
            if (survivalSpawnChest(rdram, ctx, runtime))
                --g_survival.chestWanted;
            else if (g_survival.nextWaveAt == 0 || g_dbg.padFrames + 120u > g_survival.nextWaveAt)
            {
                logf("debug: survival: %d chest(s) could not be made", g_survival.chestWanted);
                g_survival.chestWanted = 0; // the boss is about to come
            }
            return; // one thing a frame
        }
        if (g_survival.pending.empty())
            return;
        Survival::Pending &p = g_survival.pending.front();
        if (p.column && p.tries == 0)
            groundAt(rdram, ctx, runtime, p.pos, 12.0f); // each ring piece on the ground under it (uneven levels)
        g_spawnYaw.set = p.haveYaw;
        g_spawnYaw.degrees = p.yaw;
        g_spawnYaw.ownHandle = p.haveYaw && p.type == kKnightWallType;
        const SpawnedObject *made = survivalSpawnThing(rdram, ctx, runtime, p.type, p.props, p.pos, p.what.c_str());
        g_spawnYaw.set = g_spawnYaw.ownHandle = false;
        const bool haveSlot = p.slot >= 0 && static_cast<size_t>(p.slot) < g_survival.ring.size();
        if (made && p.column)
        {
            g_survival.columns.push_back(made->handle);
            g_survival.baseline.insert(made->handle);
            if (haveSlot)
            {
                g_survival.ring[static_cast<size_t>(p.slot)].handle = made->handle;
                g_survival.ring[static_cast<size_t>(p.slot)].obj = made->obj;
            }
        }
        if (made || ++p.tries >= 30)
        {
            if (haveSlot)
                g_survival.ring[static_cast<size_t>(p.slot)].queued = false;
            g_survival.pending.erase(g_survival.pending.begin());
        }
    }


    // A ring piece into the spawn queue (at the back while the ring is first made, at the front when one is
    // replaced so it does not wait behind a wave).
    void survivalQueueRingPiece(int index, bool front)
    {
        if (index < 0 || static_cast<size_t>(index) >= g_survival.ring.size())
            return;
        Survival::RingSlot &slot = g_survival.ring[static_cast<size_t>(index)];
        Survival::Pending p{kKnightWallType, {}, {slot.pos[0], slot.pos[1], slot.pos[2]}, "ring wall piece"};
        p.column = true;
        p.slot = index;
        p.haveYaw = true;
        p.yaw = slot.yaw;
        if (front)
            g_survival.pending.insert(g_survival.pending.begin(), p);
        else
            g_survival.pending.push_back(p);
        slot.queued = true;
    }

    // SURVIVAL IN THIS LEVEL: the ring of purple columns (END_OF_SUBLEVEL_COLUMN, class 0x4D, made with its class
    // defaults), one about every 4 units around the ring - 19 at radius 12 -, at the height of where it started.
    // A column ends the level when Maximo walks into it (GotoNextLevel, script API slot 113, func_00117B80): the
    // ring's do not (survivalGotoEntry). Spawn spots stay inside the ring, and survivalBoundaryLate holds Maximo
    // inside it even where the columns leave gaps.
    void survivalMakeRing(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        if (g_survival.ringMade)
            return;
        g_survival.ringMade = true;
        const float r = kLevelArenaRadius;
        // kRingPieces pieces around the ring (31, then 20, still cost frame rate), all made once (queued, one a frame) and kept. Making
        // them only near Maximo and taking the far ones away cost more than it saved (a spawn and a removal every
        // few frames). The ring is always the Dark Knight's wall (class 0x8C): its class lookup can fail in the very
        // frame the level's enemies are taken away (several objects changed in one frame), so it is not asked here;
        // each queued piece retries its own spawn. A wall piece lives while the object whose handle is in its
        // variable 0 does (its script: IsApparitionAlive, the knight who made it): each piece gets its own handle
        // there, turned to face along the radius (yaw 90 - angle, in degrees). An earlier ring's pieces go first.
        const uint32_t vm = rd32(kLevelVmPtr);
        for (uint32_t h : g_survival.columns)
            if (const uint32_t o = objectForHandle(h))
                removeLevelObject(rdram, ctx, runtime, vm, o, h, false);
        g_survival.columns.clear();
        const int pieces = kRingPieces;
        g_survival.ring.clear();
        g_survival.ringType = kKnightWallType;
        g_survival.ringCheckAt = g_dbg.padFrames + 600u; // (the first check once they are all made)
        for (int i = 0; i < pieces; ++i)
        {
            const float a = 6.2831853f * static_cast<float>(i) / static_cast<float>(pieces);
            Survival::RingSlot slot;
            slot.pos[0] = g_survival.ringCenter[0] + r * std::cos(a);
            slot.pos[1] = g_survival.ringCenter[1];
            slot.pos[2] = g_survival.ringCenter[2] + r * std::sin(a);
            slot.yaw = 90.0f - a * 57.29578f;
            g_survival.ring.push_back(slot);
            survivalQueueRingPiece(static_cast<int>(i), false);
        }
        logf("debug: survival here: %d pieces of the Dark Knight's wall queued (made once)", pieces);
    }
    void survivalGotoEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t interp = reg(ctx, 4);
        if (interp && (interp & PS2_RAM_MASK) == interp && !g_survival.columns.empty() &&
            std::find(g_survival.columns.begin(), g_survival.columns.end(), rd32(interp + 0xBECu)) !=
                g_survival.columns.end())
        {
            ret(ctx, 0);
            return;
        }
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
    }
    // After the frame's updates (func_0015FED0, where no clip writes its position too): Maximo outside the ring is put
    // back just inside it, the way no clip moves him (every copy of his position it found, and no velocity left).
    void survivalBoundaryLate(uint8_t *rdram)
    {
        if (!g_survival.active || !g_survival.levelMode || g_survival.phase != 3 || g_dbg.menuOpen)
            return;
        float feet[3];
        if (!maximoFeet(feet))
            return;
        const float dx = feet[0] - g_survival.ringCenter[0], dz = feet[2] - g_survival.ringCenter[2];
        const float d = std::sqrt(dx * dx + dz * dz);
        const float limit = kLevelArenaRadius - 0.8f;
        if (d <= limit || d > kLevelArenaRadius * 4.0f) // (far away: a respawn or a teleport, not a step out)
            return;
        if (!g_nc.have)
            noClipStep(rdram, kPlayerAddr, false); // finds the copies (and writes them back unchanged)
        if (!g_nc.have)
            return;
        const float k = (limit - 0.1f) / d - 1.0f; // move along the radius, back inside
        float cur[3];
        std::memcpy(cur, mem(kPlayerAddr + 0x1A0u), 12);
        g_nc.pos[0] = cur[0] + dx * k;
        g_nc.pos[1] = cur[1];
        g_nc.pos[2] = cur[2] + dz * k;
        noClipStep(rdram, kPlayerAddr, false);
    }


    // The ring's pieces are made once. A piece that is gone (they all went when a Dark Knight was killed: the
    // knights' death takes walls with it) is made again - checked once a second, one piece queued per check, so a
    // lost ring comes back over half a minute without any churn while it stands.
    void survivalRingTick(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        (void)rdram; (void)ctx; (void)runtime;
        if (!g_survival.levelMode || g_survival.phase != 3 || g_survival.ring.empty() ||
            g_dbg.padFrames < g_survival.ringCheckAt)
            return;
        g_survival.ringCheckAt = g_dbg.padFrames + 60u;
        int lost = 0, requeued = -1;
        for (size_t i = 0; i < g_survival.ring.size(); ++i)
        {
            Survival::RingSlot &slot = g_survival.ring[i];
            if (slot.queued)
                continue;
            if (slot.handle && rd32(slot.obj) == slot.handle)
                continue;
            if (slot.handle)
            {
                g_survival.columns.erase(std::remove(g_survival.columns.begin(), g_survival.columns.end(), slot.handle),
                                         g_survival.columns.end());
                g_survival.baseline.erase(slot.handle);
                slot.handle = slot.obj = 0;
            }
            ++lost;
            if (requeued < 0)
            {
                requeued = static_cast<int>(i);
                survivalQueueRingPiece(requeued, true);
            }
        }
        if (lost)
        {
            static int logged = 0;
            if (logged < 40)
            {
                ++logged;
                logf("debug: survival here: %d wall piece(s) gone, piece %d made again", lost, requeued);
            }
        }
    }
    void survivalPlayMusic(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, int track)
    {
        if (track < 0)
            return;
        // The way the game changes the music itself (the continue screen, func_001260B0): PSound stop
        // (func_0016E120: the stream stopped, state 2), then PlayMusic (func_0016D9F0), which queues sound command 1
        // for the IOP; the main loop's per-frame flush (func_0016D760) sends it. Run with the resuming guest call
        // (a plain call came back early and the command never went out), and logged with the command queue's
        // length (gp-0x766C) before and after, so a lost command shows.
        const uint32_t before = rd32(kSoundQueueCount);
        uint32_t v0 = 0;
        const bool stopped = callGuestArgs(rdram, ctx, runtime, kStopMusicFn, {kSoundManagerAddr}, v0);
        const bool played =
            callGuestArgs(rdram, ctx, runtime, kPlayMusicFn, {kSoundManagerAddr, static_cast<uint32_t>(track)}, v0);
        logf("debug: survival: music track %d (stop %s, play %s, sound queue %u -> %u)", track, stopped ? "ok" : "FAILED",
             played ? "ok" : "FAILED", before, rd32(kSoundQueueCount));
    }

    void survivalTick(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        survivalLoadBest();
        {
            // Ring columns belong to the level they were made in: forgotten when the level changes (their handles
            // are used again by the next level's objects).
            static uint32_t ringLevel = ~0u, ringVm = 0;
            if (rd32(kCurrentLevelAddr) != ringLevel || rd32(kLevelVmPtr) != ringVm || rd32(kLevelExitCodeAddr) != 0)
            {
                if (!g_survival.active || rd32(kLevelExitCodeAddr) != 0)
                    g_survival.columns.clear();
                ringLevel = rd32(kCurrentLevelAddr);
                ringVm = rd32(kLevelVmPtr);
            }
            if (!g_survival.active && g_survival.music >= 0 && g_dbg.inLevel)
            {
                // The run is over: the level's own music again (LevelTable row +0x12).
                g_survival.music = -1;
                const uint32_t row = rd32(kCurrentLevelAddr);
                // (only while still in the level the run was in: a new level starts its own music)
                if (row < kLevelTableRows && static_cast<int>(row) == g_survival.arenaRow &&
                    rd32(kLevelExitCodeAddr) == 0 && rd32(kLevelVmPtr) == g_survival.vm)
                {
                    const int own = static_cast<int8_t>(*mem(kLevelTableAddr + row * kLevelTableStride + 0x12u));
                    survivalPlayMusic(rdram, ctx, runtime, own);
                    logf("debug: survival here: the level's music again (track %d)", own);
                }
            }
            if (!g_survival.active && !g_survival.columns.empty() && g_dbg.inLevel && ringVm && !(rd32(kCurrentLevelAddr) != ringLevel))
            {
                // The run is over: the ring goes.
                for (uint32_t h : g_survival.columns)
                    if (const uint32_t o = objectForHandle(h))
                        removeLevelObject(rdram, ctx, runtime, ringVm, o, h, false);
                g_survival.columns.clear();
                logf("debug: survival here: the ring taken away");
            }
        }
        g_survivalSoftSword = g_survival.active && g_survival.phase == 3;
        g_survivalLevelBoss = g_survivalSoftSword && g_survival.levelMode;
        g_bossHealth.clear(); // (bosses keep their normal health: the boss-health multiplier is off)
        if (!g_survivalSoftSword)
            g_survivalBossWave = false;
        // After a death: a boss arena does not reload (no exit code): the game puts Maximo back at its start and its
        // boss comes back. The run restarts once that has happened - the arena's boss is in the object list again,
        // or 4 seconds on with the camera out of the death view (gPlayer+0x248 9) - unless it was the last life.
        if (!g_survival.active && g_survival.returnRow >= 0)
        {
            const uint64_t since = g_dbg.padFrames - g_survival.returnAt; // returnAt: the death's frame
            bool ready = false;
            if (since > 60u * 60u || rd32(gameVarAddr(kVarLives)) == 0u)
            {
                g_survival.returnRow = -1; // game over, or it never came back
                survivalRestoreInventory();
            }
            else if (since >= 120u && since % 10u == 0u && !g_dbg.menuOpen && rd32(kLevelExitCodeAddr) == 0)
            {
                const uint32_t vm = rd32(kLevelVmPtr);
                for (uint32_t o = vm ? rd32(vm + 0x9Cu + 8u) : 0u, guard = 0;
                     o && (o & PS2_RAM_MASK) == o && guard < 20000 && !ready; o = rd32(o + 0xBCu), ++guard)
                {
                    const uint32_t cls = rd32(o + 0xB4u);
                    ready = cls && (cls & PS2_RAM_MASK) == cls && rd32(cls) == g_survival.arenaBoss;
                }
                ready = ready || (since >= 240u && rd32(kPlayerAddr + 0x248u) != 9u);
            }
            if (ready)
            {
                const int row = g_survival.returnRow;
                g_survival.returnRow = -1;
                logf("debug: survival: restarting in %s after the death", kLevelNames[row]);
                survivalStart(rdram, ctx, runtime, row);
                if (g_survival.active)
                    g_dbg.menuOpen = false;
            }
        }
        if (!g_survival.active)
            return;
        const uint32_t vmNow = rd32(kLevelVmPtr);
        if (g_survival.phase == 1)
        {
            // On the way: the level VM sits at the same address in every level, so arrival is the exit code (set to 4
            // by the level change) back at 0 with the arena as the level being played.
            if (g_dbg.inLevel && vmNow && g_dbg.padFrames - g_survival.phaseFrame > 30u &&
                static_cast<int>(rd32(kCurrentLevelAddr)) == g_survival.arenaRow && rd32(kLevelExitCodeAddr) == 0)
            {
                g_survival.phase = 2;
                g_survival.phaseFrame = g_dbg.padFrames;
                logf("debug: survival: arrived in %s", kLevelNames[g_survival.arenaRow]);
            }
            else if (g_dbg.padFrames - g_survival.phaseFrame > 60u * 60u)
                survivalEnd("THE ARENA DID NOT LOAD");
            return;
        }
        // The load-class limits, raised for the whole run (each new level VM starts from its own).
        if (vmNow)
            for (uint32_t c = 0; c < 5; ++c)
            {
                const uint32_t at = vmNow + 0x11Cu + c * 4u, cur = rd32(at);
                if (cur >= 1u && cur < kSurvivalBudget)
                    wr32(at, kSurvivalBudget);
            }
        // A normal level's camera instead of the boss arena's: the camera (gPlayer, 0x239890) runs mode 4, the arena
        // camera that circles the boss's spot, where a normal level runs mode 0 (+0x248; the camera-type switch at
        // 0x13B870 writes 0 for the plain follow camera), and the boss flags Camera::SetBossFlag (0x1434A0) ORs into
        // +0x8B8 go too.
        // During a boss wave, while its boss lives, the arena's own boss camera instead, as in the arena's normal
        // fight: the game's camera reset (func_0013B600) gives a boss arena func_0013BAD0(camera), mode 4 and a
        // variant at +0x8B4 (1 in BokorLaBas' arena, 2 in Lord Glutterscum's, 0 in the others); it is set up the
        // same way here when the boss appears, and the normal camera comes back when it dies.
        const bool bossAlive =
            g_survival.phase == 3 && g_survivalBossWave &&
            std::any_of(g_survival.foes.begin(), g_survival.foes.end(),
                        [](const SurvivalFoe &f)
                        { return std::find(std::begin(kBossTypes), std::end(kBossTypes), f.type) != std::end(kBossTypes); });
        // (+0x248 is the player's state, Player::Update's switch: 4 is the boss-fight state, with its variant at
        // +0x8B4 - what the game's own reset gives each arena when it loads: 1 in BokorLaBas' arena, 2 in Lord
        // Glutterscum's, 0 in the others.) When a boss wave's boss appears, the state is set ONCE, as the arena's
        // load does, and from then on the game runs it: the boss's own script sets its camera flags and position
        // (CameraSetBossFlag, Camera_SetBossPosition) as in the normal fight. Forcing the state back every frame and
        // clearing those flags is what held Maximo and the boss still (the boss's intro never finished). If the
        // stick is held for 1.5 seconds with Maximo not moving anyway: first both are unfrozen (a boss intro's
        // SetFreeze), and if he is still stuck the boss-fight state goes for the rest of that boss.
        if (!bossAlive)
        {
            g_survival.bossCamOff = false;
            g_survival.bossCamStill = 0;
            g_survival.bossCamStage = 0;
        }
        // In a normal level the boss camera's arena is given by hand. That camera (Player::Update, state 4 variant 0)
        // looks at the boss's place (+0x890, what Camera_SetBossPosition sets) from behind Maximo, +0x8CC units back
        // (15), and keeps the eye within +0x880 (35 in an arena) of the arena's middle +0x8A0 (the origin in every
        // arena) and +0x87C (10) from its target. A level has none of those set - the eye was pulled to the level's
        // origin, far away. Here: the ring's middle and its radius plus 25, and the boss's place every frame.
        // The arena's boss camera only in a boss arena's survival; survival in a normal level keeps the normal
        // camera in its boss waves too.
        const bool useBossCamera = !g_survival.levelMode;
        if (useBossCamera && bossAlive && !g_survival.bossCamOff)
        {
            if (!g_survival.bossCam)
            {
                uint32_t v0 = 0;
                callGuest(rdram, ctx, runtime, GA(0x0013BAD0u), kPlayerAddr, 0, 0, v0);
                const int row = g_survival.arenaRow;
                wr32(kPlayerAddr + 0x248u, 4u);
                // The variant is the arena's own boss's (1 BokorLaBas, 2 Lord Glutterscum) only when that boss is the
                // one fighting; any other boss gets the plain 0 (GraveDigger in BokorLaBas' arena had variant 1).
                uint32_t bossType = 0;
                for (const SurvivalFoe &f : g_survival.foes)
                    if (std::find(std::begin(kBossTypes), std::end(kBossTypes), f.type) != std::end(kBossTypes))
                        bossType = f.type;
                const uint32_t variant =
                    (row == 23 && bossType == 0x8Eu) ? 2u : (row == 11 && bossType == 0x2Eu) ? 1u : 0u;
                wr32(kPlayerAddr + 0x8B4u, variant);
                wr32(kPlayerAddr + 0x8B8u, 0u);
                if (g_survival.levelMode)
                {
                    const float minDist = 10.0f, radius = kLevelArenaRadius + 25.0f, back = 15.0f;
                    std::memcpy(mem(kPlayerAddr + 0x87Cu), &minDist, 4);
                    std::memcpy(mem(kPlayerAddr + 0x880u), &radius, 4);
                    std::memcpy(mem(kPlayerAddr + 0x8CCu), &back, 4);
                    std::memcpy(mem(kPlayerAddr + 0x8A0u), g_survival.ringCenter, 12);
                    *mem(kPlayerAddr + 0x259u) = 1; // (the camera's "just started" mark: it snaps into place)
                }
                logf("debug: survival: boss wave: the boss-fight state (4, variant %u), once",
                     rd32(kPlayerAddr + 0x8B4u));
                g_survival.bossCam = true;
                maximoFeet(g_survival.bossCamLast);
                g_survival.bossCamStill = 0;
            }
            if (g_survival.levelMode)
            {
                // The boss's place for the camera, every frame (not every boss's script sets it).
                for (const SurvivalFoe &f : g_survival.foes)
                    if (std::find(std::begin(kBossTypes), std::end(kBossTypes), f.type) != std::end(kBossTypes) &&
                        f.haveAt)
                    {
                        const float at[3] = {f.at[0], f.at[1] + 1.5f, f.at[2]};
                        std::memcpy(mem(kPlayerAddr + 0x890u), at, 12);
                    }
                std::memcpy(mem(kPlayerAddr + 0x8A0u), g_survival.ringCenter, 12);
            }
            float now[3];
            const bool pushing = std::fabs(stickAxis(g_dbg.lx)) + std::fabs(stickAxis(g_dbg.ly)) > 0.5f;
            if (maximoFeet(now))
            {
                const float dx = now[0] - g_survival.bossCamLast[0], dz = now[2] - g_survival.bossCamLast[2];
                if (dx * dx + dz * dz > 0.04f)
                {
                    std::memcpy(g_survival.bossCamLast, now, sizeof now);
                    g_survival.bossCamStill = 0;
                }
                else if (pushing && !g_dbg.menuOpen)
                    ++g_survival.bossCamStill;
            }
            if (g_survival.bossCamStill > 90)
            {
                g_survival.bossCamStill = 0;
                uint32_t bossEnt = 0;
                for (const SurvivalFoe &f : g_survival.foes)
                    if (std::find(std::begin(kBossTypes), std::end(kBossTypes), f.type) != std::end(kBossTypes) &&
                        rd32(f.obj) == f.handle)
                        bossEnt = rd32(f.obj + 0xB8u);
                survivalUnfreeze(rdram, ctx, runtime, bossEnt);
                if (++g_survival.bossCamStage >= 2)
                {
                    g_survival.bossCamOff = true;
                    wr32(kPlayerAddr + 0x8B8u, 0u);
                    logf("debug: survival: Maximo still stuck: boss-fight state off for this boss");
                }
                else
                    logf("debug: survival: Maximo held still in the boss fight: Maximo and the boss unfrozen");
            }
        }
        else
        {
            if (g_survival.bossCam)
                logf("debug: survival: boss fight over: normal state again");
            g_survival.bossCam = false;
            if (rd32(kPlayerAddr + 0x248u) == 4u)
            {
                wr32(kPlayerAddr + 0x248u, 0u);
                wr32(kPlayerAddr + 0x8B4u, 0u);
                static uint64_t lastLog = 0;
                if (g_dbg.padFrames - lastLog > 600u)
                {
                    logf("debug: survival: arena camera (mode 4, boss flags 0x%X) -> normal camera",
                         rd32(kPlayerAddr + 0x8B8u));
                    lastLog = g_dbg.padFrames;
                }
            }
            wr32(kPlayerAddr + 0x8B8u, 0u);
        }
        // The trail: every third of a second, Maximo's spot (the game camera's target) when it is 3 units from all
        // the others (400 at most).
        float feetNow[3];
        if (g_dbg.padFrames % 20u == 0u && !g_dbg.menuOpen && !g_noClip && maximoFeet(feetNow) &&
            rd32(kPlayerControlFlag) != 0u)
        {
            const std::array<float, 3> here = {feetNow[0], feetNow[1], feetNow[2]};
            if (g_survival.trail.size() < 400u &&
                std::none_of(g_survival.trail.begin(), g_survival.trail.end(),
                             [&](const std::array<float, 3> &t)
                             {
                                 const float dx = t[0] - here[0], dz = t[2] - here[2];
                                 return dx * dx + dz * dz < 9.0f;
                             }))
            {
                g_survival.trail.push_back(here);
                g_survival.walked.push_back(here);
            }
        }
        if (g_survival.phase == 2)
        {
            // Arrived: for 10 seconds, take out every boss the arena has (its own and any spawned later by its intro),
            // then start; the first wave comes 3 seconds after.
            // Every frame (it was every 15): the boss goes before its intro cutscene can start, and Maximo is
            // unfrozen (the intro's SetFreeze) while the arena is being cleared.
            if ((g_dbg.padFrames - g_survival.phaseFrame) % 15u == 0u)
                survivalUnfreeze(rdram, ctx, runtime, 0u);
            if (vmNow)
            {
                std::vector<uint32_t> victims;
                for (uint32_t o = rd32(vmNow + 0x9Cu + 8u), guard = 0; o && (o & PS2_RAM_MASK) == o && guard < 20000;
                     o = rd32(o + 0xBCu), ++guard)
                {
                    const uint32_t cls = rd32(o + 0xB4u);
                    if (!cls || (cls & PS2_RAM_MASK) != cls)
                        continue;
                    const uint32_t type = rd32(cls);
                    if (std::find(std::begin(kBossTypes), std::end(kBossTypes), type) != std::end(kBossTypes) &&
                        std::none_of(g_despawned.begin(), g_despawned.end(), [&](const auto &d) { return d.first == o; }))
                        victims.push_back(o);
                }
                for (uint32_t o : victims)
                {
                    // Where it stood is the arena's middle, on its floor: spawn spots around it (radius 4 and 7), so
                    // a wave never needs Maximo to have walked first.
                    const uint32_t ent = rd32(o + 0xB8u);
                    const uint32_t act = ent && (ent & PS2_RAM_MASK) == ent ? rd32(ent) : 0u;
                    float bp[3] = {};
                    if (act && (act & PS2_RAM_MASK) == act)
                        std::memcpy(bp, mem(act + 0xA0u), 12);
                    float feetHere[3] = {};
                    const bool haveFeet = maximoFeet(feetHere);
                    if (std::isfinite(bp[0]) && std::isfinite(bp[1]) && std::isfinite(bp[2]) &&
                        std::fabs(bp[0]) + std::fabs(bp[2]) > 0.01f && std::fabs(bp[0]) < 50000.0f &&
                        (!haveFeet || std::fabs(bp[1] - feetHere[1]) < 10.0f)) // (Glutterscum waits 210 up)
                    {
                        g_survival.trail.push_back({bp[0], bp[1], bp[2]});
                        g_survival.arenaMid = {bp[0], bp[1], bp[2]};
                        g_survival.haveMid = true;
                        for (int k = 0; k < 8; ++k)
                        {
                            const float a = 0.785398f * static_cast<float>(k), r = k % 2 ? 7.0f : 4.0f;
                            g_survival.trail.push_back({bp[0] + r * std::cos(a), bp[1], bp[2] + r * std::sin(a)});
                        }
                        logf("debug: survival: arena middle (the boss's spot) (%.1f, %.1f, %.1f)", bp[0], bp[1], bp[2]);
                    }
                    logf("debug: survival: arena boss (type 0x%02X, object 0x%07X) taken out", rd32(rd32(o + 0xB4u)), o);
                    removeLevelObject(rdram, ctx, runtime, vmNow, o, rd32(o));
                }
            }
            // (1.5 seconds: the arena's bosses are all there when it starts, and go in its first frames; the old 5
            // seconds were spent standing still.)
            if (g_dbg.padFrames - g_survival.phaseFrame >= 90u)
            {
                g_survival.phase = 3;
                if (g_survival.levelMode)
                {
                    // The level's own enemies go first (asleep ones too), then the ring and a boss fight's music.
                    despawnEnemies(rdram, ctx, runtime);
                    survivalMakeRing(rdram, ctx, runtime);
                    g_survival.music = kBossMusic[std::uniform_int_distribution<size_t>(0, std::size(kBossMusic) - 1)(survivalRng())];
                    survivalPlayMusic(rdram, ctx, runtime, g_survival.music);
                    logf("debug: survival here: boss music track %d", g_survival.music);
                }
                survivalDefaultInventory(rdram, ctx, runtime);
                g_survival.baseline.clear();
                for (uint32_t o = vmNow ? rd32(vmNow + 0x9Cu + 8u) : 0u, guard = 0;
                     o && (o & PS2_RAM_MASK) == o && guard < 20000; o = rd32(o + 0xBCu), ++guard)
                    g_survival.baseline.insert(rd32(o));
                g_survival.chestKind = 0;
                g_survival.chests.clear();
                g_survival.vm = vmNow;
                g_survival.lives = rd32(gameVarAddr(kVarLives));
                g_survival.nextWaveAt = g_dbg.padFrames;
                g_dbg.spawnMessage = "SURVIVAL: FIRST WAVE";
                logf("debug: survival: arena ready, waves start");
                // The arena boss's health bar: no level has an on-screen display of its own, so every one left
                // (the removed boss's) goes.
                removeDisplays(rdram, ctx, runtime, nullptr, "arena cleared");
            }
            else if (g_dbg.padFrames - g_survival.phaseFrame == 1u)
                g_dbg.spawnMessage = "SURVIVAL: CLEARING THE ARENA";
            return;
        }
        if (!g_dbg.inLevel || vmNow != g_survival.vm || rd32(kLevelExitCodeAddr) != 0 ||
            static_cast<int>(rd32(kCurrentLevelAddr)) != g_survival.arenaRow)
        {
            survivalEnd("LEVEL CHANGED");
            return;
        }
        const uint32_t lives = rd32(gameVarAddr(kVarLives));
        if (lives < g_survival.lives && !g_dbg.infinite)
        {
            survivalEnd("MAXIMO LOST A LIFE");
            // The death resets the arena's objects (the boss comes back): a parked Lord Glutterscum must be in the
            // list for that, or the reset frees what he still points at (the crash after dying in his arena).
            unparkAll();
            // Auto restart: once the arena has reloaded after the death, a new run starts there.
            {
                g_survival.returnRow = g_survival.arenaRow;
                g_survival.returnAt = g_dbg.padFrames;
                g_survival.returnDeadline = g_dbg.padFrames + 60u * 60u; // (a game over never restarts the arena)
            }
            return;
        }
        g_survival.lives = lives;
        if (g_dbg.menuOpen)
            return; // nothing happens while the menu is open
        // Where each foe is (its drop falls there), every 6 frames.
        if (g_dbg.padFrames % 6u == 0u)
            for (SurvivalFoe &f : g_survival.foes)
            {
                if (rd32(f.obj) != f.handle)
                    continue;
                const uint32_t ent = rd32(f.obj + 0xB8u);
                const uint32_t act = ent && (ent & PS2_RAM_MASK) == ent ? rd32(ent) : 0u;
                if (!act || (act & PS2_RAM_MASK) != act)
                    continue;
                float p[3];
                std::memcpy(p, mem(act + 0xA0u), 12);
                if (std::isfinite(p[0]) && std::isfinite(p[1]) && std::isfinite(p[2]) && std::fabs(p[0]) < 100000.0f &&
                    std::fabs(p[1]) < 100000.0f && std::fabs(p[2]) < 100000.0f)
                {
                    std::memcpy(f.at, p, sizeof p);
                    f.haveAt = true;
                }
            }
        survivalLootTick(rdram, ctx, runtime);
        survivalRingTick(rdram, ctx, runtime);
        survivalPendingTick(rdram, ctx, runtime);
        // Kills: foes whose object is gone.
        for (size_t i = 0; i < g_survival.foes.size();)
        {
            const SurvivalFoe &f = g_survival.foes[i];
            if (rd32(f.obj) != f.handle || rd32(f.obj + 0xB4u) != f.tmpl)
            {
                if (g_dbg.padFrames - f.born < 45u && f.type)
                {
                    // Died by itself right after spawning: not a kill, and replaced (its type left out after 3).
                    g_survival.badTypes.push_back(f.type);
                    logf("debug: survival: type 0x%02X died by itself %llu frames after spawning (%d time(s))", f.type,
                         static_cast<unsigned long long>(g_dbg.padFrames - f.born),
                         static_cast<int>(std::count(g_survival.badTypes.begin(), g_survival.badTypes.end(), f.type)));
                    ++g_survival.replace;
                }
                else
                {
                    ++g_survival.kills;
                    g_survival.lastProgress = g_dbg.padFrames;
                    const SurvivalFoe dead = f;
                    survivalDrop(rdram, ctx, runtime, dead);
                }
                g_survival.foes.erase(g_survival.foes.begin() + static_cast<std::ptrdiff_t>(i));
            }
            else
                ++i;
        }
        // Stuck enemies (a wave said some were left that could not be found): one that fell out of the arena (30
        // units below Maximo, or 150 away) goes at once; and after 40 seconds without a kill, every one left but a
        // boss goes.
        // Neither counts as a kill.
        if (g_survival.nextWaveAt == 0 && !g_survival.foes.empty())
        {
            float feet[3];
            const bool haveFeet = maximoFeet(feet);
            const bool timeout = g_dbg.padFrames - g_survival.lastProgress > 40u * 60u;
            const uint32_t vmNow2 = rd32(kLevelVmPtr);
            for (size_t i = 0; i < g_survival.foes.size();)
            {
                const SurvivalFoe f = g_survival.foes[i];
                const uint32_t ent = rd32(f.obj + 0xB8u);
                const uint32_t act = ent && (ent & PS2_RAM_MASK) == ent ? rd32(ent) : 0u;
                float p[3] = {};
                if (act && (act & PS2_RAM_MASK) == act)
                    std::memcpy(p, mem(act + 0xA0u), 12);
                const bool known = std::isfinite(p[0]) && std::isfinite(p[1]) && std::isfinite(p[2]);
                // Never woke: an enemy that has not moved half a unit in the 12 seconds since it was made (it sat in the
                // air where it was made, like a
                // crocodile waiting for water or the pirate skeletons logged at their spawn spot) is replaced by
                // another, and its kind is left out after two of those.
                bool asleep = false;
                if (known && g_dbg.padFrames % 30u == 0u)
                {
                    const float mx = p[0] - g_survival.foes[i].last[0], my = p[1] - g_survival.foes[i].last[1],
                                mz = p[2] - g_survival.foes[i].last[2];
                    if (mx * mx + my * my + mz * mz > 0.25f)
                    {
                        std::memcpy(g_survival.foes[i].last, p, 12);
                        g_survival.foes[i].lastMove = g_dbg.padFrames;
                    }
                }
                const bool bossFoe =
                    std::find(std::begin(kBossTypes), std::end(kBossTypes), f.type) != std::end(kBossTypes);
                if (!bossFoe && f.type != 0x08u /* a plant never moves */ &&
                    g_survival.foes[i].lastMove == g_survival.foes[i].born &&
                    g_dbg.padFrames - g_survival.foes[i].born > 12u * 60u)
                    asleep = true; // (not even fallen the unit it was made above the floor)
                const float dx = p[0] - feet[0], dz = p[2] - feet[2];
                const bool lost = haveFeet && known && g_dbg.padFrames - f.born > 120u &&
                                  (p[1] < feet[1] - 30.0f || dx * dx + dz * dz > 150.0f * 150.0f);
                if (asleep)
                {
                    logf("debug: survival: enemy type 0x%02X never moved from (%.1f, %.1f, %.1f): replaced", f.type, p[0],
                         p[1], p[2]);
                    g_survival.badTypes.push_back(f.type);
                    g_survival.badTypes.push_back(f.type); // (counts double: out after two)
                    ++g_survival.replace;
                    g_survival.foes.erase(g_survival.foes.begin() + static_cast<std::ptrdiff_t>(i));
                    if (rd32(f.obj) == f.handle && vmNow2)
                        removeLevelObject(rdram, ctx, runtime, vmNow2, f.obj, f.handle);
                    continue;
                }
                if (!lost && (!timeout || bossFoe)) // (a boss fight may take longer)
                {
                    ++i;
                    continue;
                }
                logf("debug: survival: %s enemy type 0x%02X at (%.1f, %.1f, %.1f) removed", lost ? "lost" : "stuck",
                     f.type, p[0], p[1], p[2]);
                g_survival.foes.erase(g_survival.foes.begin() + static_cast<std::ptrdiff_t>(i));
                if (rd32(f.obj) == f.handle && vmNow2)
                    removeLevelObject(rdram, ctx, runtime, vmNow2, f.obj, f.handle);
            }
            if (timeout)
                g_dbg.spawnMessage = "STUCK ENEMIES REMOVED";
        }
        if (g_survival.replace > 0 && g_survival.nextWaveAt == 0)
        {
            const int n = g_survival.replace;
            g_survival.replace = 0;
            survivalSpawnWave(rdram, ctx, runtime, n);
        }
        if (g_survival.nextWaveAt == 0)
        {
            if (g_survival.foes.empty())
            {
                g_survival.nextWaveAt = g_dbg.padFrames + 300u; // 5 seconds between waves
                char b[96];
                std::snprintf(b, sizeof b, "WAVE %d CLEARED! NEXT WAVE IN 5 SECONDS", g_survival.wave);
                // Before a boss wave (every fifth, when a boss class is loaded here): the chest, and 15 seconds.
                bool bossNext = (g_survival.wave + 1) % 5 == 0;
                if (bossNext)
                {
                    bool anyBoss = false;
                    const uint32_t vmc = rd32(kLevelVmPtr);
                    for (uint32_t bt : kBossTypes)
                    {
                        uint32_t t = 0;
                        if (vmc && callGuest(rdram, ctx, runtime, kClassLookupFn, vmc + 0xB8u, bt, 0, t) && t)
                            anyBoss = true;
                    }
                    bossNext = anyBoss;
                }
                if (bossNext)
                {
                    // One chest more each reward: 1 before wave 5, 2 before wave 10, 3 before wave 15... (made by
                    // survivalPendingTick, one a frame, each tried until it is).
                    g_survival.chestWanted = std::clamp((g_survival.wave + 1) / 5, 1, 8);
                    g_survival.chests.clear();
                    g_survival.nextWaveAt = g_dbg.padFrames + kChestWaitFrames;
                    if (g_survival.chestWanted == 1)
                        std::snprintf(b, sizeof b, "WAVE %d CLEARED! A CHEST! THE BOSS COMES IN 15 SECONDS", g_survival.wave);
                    else
                        std::snprintf(b, sizeof b, "WAVE %d CLEARED! %d CHESTS! THE BOSS COMES IN 15 SECONDS",
                                      g_survival.wave, g_survival.chestWanted);
                }
                g_dbg.spawnMessage = b;
                if (g_survival.wave > g_survival.best)
                {
                    g_survival.best = g_survival.wave;
                    survivalSaveBest();
                }
            }
        }
        else if (g_dbg.padFrames >= g_survival.nextWaveAt)
            survivalSpawnWave(rdram, ctx, runtime);
    }

    // Bosses out of their own arena (spawned by survival or the menu):
    // - Captain Cadaver (0x82) away from his arena (row 17): 12 hits kill him (Maximo's hits counted in
    //   bodyCollisionEntry); his health bar is GameVar 15, the boss meter (the HUD element of a game variable has
    //   its number: GameVarSet(15) is how the bosses show their health), set to the hits he has left.
    // - The King, Achille (0x9C), away from his arena (row 28): his first form needs the arena's electrodes (0x81,
    //   FindApparitions) to be beaten, so he goes straight to his final spear form. His script's states (the
    //   class's state table): 0 start, 2 intro, 3 first form, 7 the change, 8-10 the change's animations, 17 the
    //   final form. Once his start has run (state 2 or 3) he is set to 8 (func_00102F30, what SetState calls), the
    //   change without its cutscene, which ends in state 17.
    constexpr uint32_t kCadaverType = 0x82u, kKingType = 0x9Cu;
    constexpr int kCadaverHits = 12;
    struct BossOutOfArena
    {
        uint32_t handle = 0;
        int hits = 0;
        bool done = false;
        uint32_t state = ~0u;  // Cadaver: his script state at the last look
        uint64_t since = 0;    // and since when
        uint64_t removeAt = 0; // his death animation is playing: taken away then
    };
    std::vector<BossOutOfArena> g_outOfArena;
    void bossHitTick(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        if (!g_dbg.inLevel || !vm)
        {
            g_outOfArena.clear();
            g_hitCountedEntity = 0;
            g_cadaverWatchHandle = 0;
            return;
        }
        const int row = static_cast<int>(rd32(kCurrentLevelAddr));
        uint32_t cadaverEnt = 0;
        for (const DebugBoss &b : g_debugBosses)
        {
            if (rd32(b.obj) != b.handle || b.removeAt)
                continue;
            const uint32_t cls = rd32(b.obj + 0xB4u);
            const uint32_t type = cls && (cls & PS2_RAM_MASK) == cls ? rd32(cls) : 0u;
            auto it = std::find_if(g_outOfArena.begin(), g_outOfArena.end(),
                                   [&](const BossOutOfArena &o) { return o.handle == b.handle; });
            if (type == kCadaverType && row != 17)
            {
                // His own script answers a hit of Maximo's: from his fight loop (state 5) to 16, his hurt animation,
                // then 17 - which waits for his arena's deck (AnimateLandTo) and so stood still forever outside it.
                // (States read from his class's state table: 15/16/18 the hurt states, 21-23 his death.) Each entry
                // into a hurt state is a hit; a hurt state held 2.5 seconds goes back to 5. The 12th hit plays his
                // death (state 21) and he is taken away 4 seconds later.
                if (it == g_outOfArena.end())
                {
                    BossOutOfArena o;
                    o.handle = b.handle;
                    g_outOfArena.push_back(o);
                    it = g_outOfArena.end() - 1;
                    // The boss meter's full length is the first value it is shown with (its entry's +0xB, taken from
                    // GameVar 15 when it is 0: func_00145F90): his own start had already shown his trap health there,
                    // so the meter stayed full for the first hits. Cleared first, it takes the 12 hits as full.
                    *mem(kWorldAddr + 0x7788u + 0xBu) = 0;
                    uint32_t v0 = 0;
                    callGuest(rdram, ctx, runtime, GA(0x00129D40u), kGamePlayerAddr, 15u, kCadaverHits, v0);
                    logf("debug: Captain Cadaver out of his arena: %d hits kill him (boss meter full at %u)", kCadaverHits,
                         *mem(kWorldAddr + 0x7788u + 0xBu));
                    g_cadaverWatchHandle = b.handle;
                    g_cadaverPendingHits = 0;
                }
                auto countHit = [&](const char *how) -> bool // true once the last hit killed him
                {
                    ++it->hits;
                    const int left = std::max(0, kCadaverHits - it->hits);
                    uint32_t v0 = 0;
                    callGuest(rdram, ctx, runtime, GA(0x00129D40u), kGamePlayerAddr, 15u, static_cast<uint32_t>(left), v0);
                    logf("debug: Captain Cadaver hit (%d of %d; %s)", it->hits, kCadaverHits, how);
                    if (left != 0)
                        return false;
                    callGuest(rdram, ctx, runtime, GA(0x00102F30u), vm, b.handle, 21u, v0);
                    it->removeAt = g_dbg.padFrames + 240u;
                    g_cadaverWatchHandle = 0;
                    logf("debug: Captain Cadaver beaten by hits: his death animation");
                    return true;
                };
                if (!it->removeAt && g_cadaverWatchHandle == b.handle && g_cadaverPendingHits > 0)
                {
                    g_cadaverPendingHits = 0;
                    if (countHit("his collision event"))
                        continue;
                }
                if (it->removeAt)
                {
                    if (g_dbg.padFrames >= it->removeAt)
                    {
                        removeLevelObject(rdram, ctx, runtime, vm, b.obj, b.handle);
                        g_bossBarCleanup = true;
                        logf("debug: Captain Cadaver taken away after his death animation");
                        break;
                    }
                    continue;
                }
                uint32_t state = 0;
                if (!callGuest(rdram, ctx, runtime, GA(0x00103030u), vm, b.handle, 0, state))
                    continue;
                const bool hurt = state == 15u || state == 16u || state == 17u || state == 18u || state == 19u;
                if (state != it->state)
                {
                    const bool wasHurt = it->state >= 15u && it->state <= 19u;
                    // (a hurt animation the collision event already counted is not counted again)
                    if (hurt && !wasHurt && g_dbg.padFrames - g_cadaverLastEventHitFrame > 60u)
                    {
                        if (countHit("his hurt animation"))
                            continue;
                    }
                    it->state = state;
                    it->since = g_dbg.padFrames;
                }
                else if (hurt && g_dbg.padFrames - it->since > 150u)
                {
                    uint32_t v0 = 0;
                    callGuest(rdram, ctx, runtime, GA(0x00102F30u), vm, b.handle, 5u, v0);
                    logf("debug: Captain Cadaver held in his hurt state %u: back to his fight (5)", state);
                    it->state = 5u;
                    it->since = g_dbg.padFrames;
                }
            }
            else if (type == kKingType && (row != 28 || g_survival.active))
            {
                uint32_t state = 0;
                if (!callGuest(rdram, ctx, runtime, GA(0x00103030u), vm, b.handle, 0, state))
                    continue;
                if (it == g_outOfArena.end())
                {
                    BossOutOfArena o;
                    o.handle = b.handle;
                    o.since = g_dbg.padFrames;
                    g_outOfArena.push_back(o);
                    it = g_outOfArena.end() - 1;
                }
                if (state != it->state)
                {
                    logf("debug: the King out of his arena: state %d -> %u", it->state == ~0u ? -1 : static_cast<int>(it->state),
                         state);
                    it->state = state;
                    it->since = g_dbg.padFrames;
                }
                if (it->done)
                    continue;
                // His member 3 is how many rounds of the first form are left: state 3's entry takes one and goes
                // to state 4 (the first form, beaten only with the arena's electrodes), or, at 0, to state 7, the
                // change into the final form (8-16: its animations, without the cutscene here; 17: the final form).
                // Kept at 0 from his start, so his own script goes there; setting state 8 from outside during his
                // intro was undone by the intro (state 2 goes to 3 when no cutscene plays).
                const uint32_t obj = objectForHandle(b.handle);
                const uint32_t tmpl = obj ? rd32(obj + 0xB4u) : 0u;
                const uint32_t vars = obj ? rd32(obj + 0x84u) : 0u;
                if (vars && (vars & PS2_RAM_MASK) == vars && tmpl && rd32(tmpl + 4u) >= 16u)
                {
                    if (rd32(vars + 12u) != 0u)
                        logf("debug: the King out of his arena: first-form rounds %u -> 0", rd32(vars + 12u));
                    wr32(vars + 12u, 0u);
                }
                if (state == 4u || (state == 3u && g_dbg.padFrames - it->since > 30u))
                {
                    uint32_t v0 = 0;
                    callGuest(rdram, ctx, runtime, GA(0x00102F30u), vm, b.handle, 7u, v0);
                    logf("debug: the King out of his arena: state %u -> 7 (the change into his final form)", state);
                }
                if (state >= 7u)
                    it->done = true;
            }
        }
        (void)cadaverEnt; // (hits now come from his own hurt states, not from collisions)
        g_hitCountedEntity = 0;
        g_outOfArena.erase(std::remove_if(g_outOfArena.begin(), g_outOfArena.end(),
                                          [](const BossOutOfArena &o)
                                          {
                                              return std::none_of(g_debugBosses.begin(), g_debugBosses.end(),
                                                                  [&](const DebugBoss &b) { return b.handle == o.handle; });
                                          }),
                           g_outOfArena.end());
    }


    // The dead bosses' removal, the same way DESPAWN ALL ENEMIES does it: unlinked from the level's object list and
    // their actor kept far away, so nothing updates or draws them (or their health bar) any more.
    void removeDeadBosses(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t vm = rd32(kLevelVmPtr);
        // The boss health bar is the HUD's, shown while the boss flags (gPlayer+0x8B8, SetBossFlag 0x1434A0) are set -
        // the display list had nothing in it when a boss died. With no boss left they are cleared, and a boss camera
        // (mode 4 at gPlayer+0x248) goes back to the normal one, as survival does when it empties an arena.
        auto clearBossHud = [&](const char *why)
        {
            if (!vm || !g_dbg.inLevel)
                return;
            wr32(kPlayerAddr + 0x8B8u, 0u);
            if (rd32(kPlayerAddr + 0x248u) == 4u)
                wr32(kPlayerAddr + 0x248u, 0u);
            // The meter itself: the interface's boss meter (0x25B9E0 + 0x7788: its model, then its value; set by
            // FlashBossMeter -> func_00145EF0) is hidden by the game's own func_00145F80(interface), which the level
            // setup calls: value 0 and the model switched off.
            uint32_t v0 = 0;
            callGuest(rdram, ctx, runtime, GA(0x00145F80u), kWorldAddr, 0, 0, v0);
            const uint32_t meter = rd32(kWorldAddr + 0x7788u);
            if (meter && (meter & PS2_RAM_MASK) == meter)
                wr32(meter + 0x28u, 0u);
            // The meter's HUD slot (model, value, then a short and a byte the game uses as "shown": func_00145F90
            // only switches the meter on again while that byte is 0). func_00145F80 leaves it set, so the next boss
            // never got its bar: it is put back to 0 here, as when the level starts.
            *mem(kWorldAddr + 0x7788u + 0xBu) = 0;
            std::memset(mem(kWorldAddr + 0x7788u + 0x8u), 0, 2);
            setModelPartsVisible(meter, 0u);
            g_bossPartsHidden = true;
            removeDisplays(rdram, ctx, runtime, nullptr, why);
            logf("debug: boss health bar cleared (%s)", why);
        };
        if (g_bossBarCleanup)
        {
            g_bossBarCleanup = false;
            clearBossHud("the boss left the level");
        }
        auto inRam = [](uint32_t a) { return a != 0 && (a & PS2_RAM_MASK) == a; };
        for (size_t i = 0; i < g_debugBosses.size();)
        {
            const DebugBoss b = g_debugBosses[i];
            if (b.removeAt == 0u || g_dbg.padFrames < b.removeAt)
            {
                ++i;
                continue;
            }
            g_debugBosses.erase(g_debugBosses.begin() + static_cast<std::ptrdiff_t>(i));
            if (!vm || rd32(b.obj) != b.handle)
                continue;
            removeLevelObject(rdram, ctx, runtime, vm, b.obj, b.handle);
            // The bar may point at something else of the boss's: with no other boss alive, every display goes.
            if (std::none_of(g_debugBosses.begin(), g_debugBosses.end(),
                             [](const DebugBoss &o) { return rd32(o.obj) == o.handle && o.removeAt == 0u; }))
                clearBossHud("no boss left");
            logf("debug: dead debug boss (object 0x%07X) removed from the level", b.obj);
        }
    }

    // Waking spawns. The game checks a level's objects in their level sectors every frame, asleep or not, and
    // wakes one when the camera comes within the wake range (func_001830C0: squared distance under the world's
    // range at 0x25B9FC squared, then func_0014F2A0(object, 0) and func_00148DC0(0x25B9E0, object)). A spawn has no
    // sector (+0xF8 is 0): it is checked only while on the world's active list, which its sleep takes it off - so
    // once asleep it was never checked again and never came back. The same check is done here for sleeping spawns.
    void spawnWakeTick(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        static uint64_t lastCheck = 0;
        if (!g_dbg.inLevel || !rd32(kLevelVmPtr) || g_dbg.padFrames - lastCheck < 4u)
            return;
        lastCheck = g_dbg.padFrames;
        auto inRam = [](uint32_t a) { return a != 0 && (a & PS2_RAM_MASK) == a; };
        uint32_t viewer = 0;
        if (rd32(GA(0x00239AD8u)) == 9u)
            viewer = GA(0x00239A30u);
        else
        {
            const uint32_t cam = rd32(GA(0x00263540u));
            const uint32_t camObj = inRam(cam) ? rd32(cam) : 0u;
            if (inRam(camObj))
                viewer = camObj + 0xA0u;
        }
        if (!viewer)
            return;
        float range, vp[3];
        std::memcpy(&range, mem(GA(0x0025B9FCu)), 4);
        std::memcpy(vp, mem(viewer), 12);
        const float wake2 = range * range;
        for (const SpawnedObject &o : g_spawnedObjects)
        {
            if (rd32(o.obj) != o.handle)
                continue;
            const uint32_t w = rd32(o.obj + 0xB8u);
            if (!inRam(w) || rd32(w + 0xA4u) != o.handle || static_cast<int8_t>(*mem(w + 0x37u)) != 1 ||
                *mem(w + 0x3Cu) != 0 || rd32(w + 0xF8u) != 0)
                continue;
            const uint32_t body = rd32(w);
            if (!inRam(body))
                continue;
            float p[3];
            std::memcpy(p, mem(body + 0xA0u), 12);
            const float dx = p[0] - vp[0], dy = p[1] - vp[1], dz = p[2] - vp[2];
            if (dx * dx + dy * dy + dz * dz >= wake2)
                continue;
            uint32_t v0 = 0;
            callGuest(rdram, ctx, runtime, GA(0x0014F2A0u), w, 0, 0, v0);
            callGuest(rdram, ctx, runtime, GA(0x00148DC0u), kWorldAddr, w, 0, v0);
            logf("debug: spawned handle %u woke up (Maximo came back)", o.handle);
        }
    }

    void debugPadRead(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime, uint32_t dataAddr)
    {
        applyFrameControls();
        if (g_viewP2Now)
            return; // SPLIT VIEW view frame: nothing ticks, the menu reads the pad on game frames
        removeDeadBosses(rdram, ctx, runtime);
        bossMeterTick();
        bossHitTick(rdram, ctx, runtime);
        if (g_bossSnapAliveAt && g_dbg.padFrames >= g_bossSnapAliveAt && !g_bossSnapAliveDone)
        {
            g_bossSnapAliveDone = true;
            logf("debug: boss bar research: snapshot with the boss alive");
            saveRamSnapshot();
        }
        if (g_bossSnapDeadAt && g_dbg.padFrames >= g_bossSnapDeadAt && !g_bossSnapDeadDone)
        {
            g_bossSnapDeadDone = true;
            logf("debug: boss bar research: snapshot after the boss was cleared");
            saveRamSnapshot();
        }
        trackSpawns();
        spawnWakeTick(rdram, ctx, runtime);
        survivalTick(rdram, ctx, runtime);
        healthBarsTick();
        uint8_t *data = mem(dataAddr);
        const uint16_t buttons = static_cast<uint16_t>(~(data[2] | (data[3] << 8)));
        const uint16_t pressed = buttons & static_cast<uint16_t>(~g_dbg.prevButtons);
        g_dbg.prevButtons = buttons;
        g_dbg.held = buttons;
        g_padHeldForRate = buttons; // (a button ends the fast frames after a load)
        g_dbg.rx = data[4];
        g_dbg.ry = data[5];
        g_dbg.lx = data[6];
        g_dbg.ly = data[7];

        bool changed = false;
        const bool combo = (pressed & kBtnSelect) && (buttons & kBtnL1) && (buttons & kBtnR1);
        if (combo)
        {
            g_dbg.menuOpen = !g_dbg.menuOpen;
            logf("debug: menu %s", g_dbg.menuOpen ? "opened" : "closed");
            if (g_dbg.menuOpen)
            {
                g_dbg.spawnMessage.clear();
                refreshSpawnable(rdram, ctx, runtime);
                const uint32_t current = rd32(kCurrentLevelAddr);
                if (current < std::size(kLevelNames))
                    g_dbg.levelPick = static_cast<int>(current);
            }
            changed = true;
        }
        else if (g_dbg.menuOpen)
        {
            if (pressed & kBtnUp)
                g_dbg.selected = (g_dbg.selected + kItemCount - 1) % kItemCount;
            if (pressed & kBtnDown)
                g_dbg.selected = (g_dbg.selected + 1) % kItemCount;
            const int step = (pressed & kBtnLeft) ? -1 : (pressed & (kBtnRight | kBtnCross)) ? 1 : 0;
            if (step)
                switch (g_dbg.selected)
                {
                case kItemFreeCam:
                    g_dbg.freeCam = !g_dbg.freeCam;
                    if (g_dbg.freeCam)
                        g_dbg.firstPerson = false;
                    if (g_dbg.freeCam)
                        startFreeCamera();
                    logf("debug: free camera %s", g_dbg.freeCam ? "on" : "off");
                    break;
                case kItemSpeed:
                    g_dbg.speed = (g_dbg.speed + 3 + step) % 3;
                    break;
                case kItemZoom:
                {
                    static const float kZooms[] = {1.0f, 1.5f, 2.0f, 3.0f, 4.0f, 6.0f, 8.0f};
                    int z = 0;
                    for (int i = 0; i < 7; ++i)
                        if (g_dbg.zoom >= kZooms[i] - 0.01f)
                            z = i;
                    g_dbg.zoom = kZooms[(z + 7 + step) % 7];
                    break;
                }
                case kItemSpawnAt:
                    g_dbg.spawnAtCamera = !g_dbg.spawnAtCamera;
                    break;
                case kItemPauseCam:
                    g_dbg.pauseInFreeCam = !g_dbg.pauseInFreeCam;
                    logf("debug: pause in free camera %s", g_dbg.pauseInFreeCam ? "on" : "off");
                    break;
                case kItemFirstPerson:
                    g_dbg.firstPerson = !g_dbg.firstPerson;
                    if (g_dbg.firstPerson)
                        g_dbg.freeCam = false; // (one camera of ours at a time)
                    logf("debug: first person camera %s", g_dbg.firstPerson ? "on" : "off");
                    break;
                case kItemMoveMaximo:
                    g_dbg.moveMaximo = !g_dbg.moveMaximo;
                    logf("debug: move Maximo in free camera %s", g_dbg.moveMaximo ? "on" : "off");
                    break;
                case kItemNoHud:
                    g_dbg.noHud = !g_dbg.noHud;
                    logf("debug: hide HUD %s", g_dbg.noHud ? "on" : "off");
                    break;
                case kItemInvertX:
                    g_dbg.invertX = !g_dbg.invertX;
                    break;
                case kItemInvertY:
                    g_dbg.invertY = !g_dbg.invertY;
                    break;
                case kItemPosition:
                    g_dbg.showPosition = !g_dbg.showPosition;
                    break;
                case kItemGod:
                    g_godMode = !g_godMode;
                    logf("debug: god mode %s", g_godMode ? "on" : "off");
                    break;
                case kItemNoClip:
                    g_noClip = !g_noClip;
                    g_nc.have = false;
                    if (g_noClip && !g_dbg.inLevel)
                    {
                        g_noClip = false;
                        g_dbg.spawnMessage = "NO CLIP: ONLY IN A LEVEL";
                    }
                    logf("debug: no clip %s", g_noClip ? "on" : "off");
                    break;
                case kItemLevel:
                    if (pressed & kBtnCross)
                        goToLevel(rdram, ctx, runtime, g_dbg.levelPick);
                    else
                        g_dbg.levelPick = (g_dbg.levelPick + 31 + step) % 31;
                    break;
                case kItemSpawnType:
                    if (pressed & kBtnCross)
                        spawnPicked(rdram, ctx, runtime);
                    else if (!g_dbg.spawnable.empty())
                    {
                        const int n = static_cast<int>(g_dbg.spawnable.size());
                        g_dbg.spawnPick = (g_dbg.spawnPick + n + step) % n;
                    }
                    break;
                case kItemEnemyClass:
                    g_dbg.enemyClass = (g_dbg.enemyClass + 4 + step) % 4;
                    logf("debug: enemy class %s", kEnemyClassNames[g_dbg.enemyClass]);
                    break;
                case kItemSpawnItem:
                case kItemSpawnSkill:
                case kItemSpawnSword:
                case kItemSpawnStruct:
                case kItemSpawnCollector:
                {
                    const int which = g_dbg.selected == kItemSpawnCollector ? 4
                                      : g_dbg.selected == kItemSpawnStruct  ? 3
                                                                           : g_dbg.selected - kItemSpawnItem;
                    if (pressed & kBtnCross)
                    {
                        spawnPickups(rdram, ctx, runtime, which);
                        break;
                    }
                    const int n = 1 + (which == 0   ? static_cast<int>(g_dbg.items.size())
                                       : which == 1 ? static_cast<int>(std::size(kSkills))
                                       : which == 3 ? static_cast<int>(g_dbg.structs.size())
                                       : which == 4 ? kCollectorStocks
                                                    : static_cast<int>(std::size(kSwordPowers)));
                    int &pick = which == 0   ? g_dbg.itemPick
                                : which == 1 ? g_dbg.skillPick
                                : which == 3 ? g_dbg.structPick
                                : which == 4 ? g_dbg.collectorPick
                                             : g_dbg.swordPick;
                    pick = (std::min(pick, n - 1) + n + step) % n;
                    break;
                }
                case kItemOneHit:
                    g_dbg.oneHitKill = !g_dbg.oneHitKill;
                    g_oneHitKill = g_dbg.oneHitKill;
                    logf("debug: one hit kill %s", g_dbg.oneHitKill ? "on" : "off");
                    break;
                case kItemHealthBars:
                    g_healthBars = !g_healthBars;
                    logf("debug: enemy health bars %s", g_healthBars ? "on" : "off");
                    break;
                case kItemInfinite:
                    g_dbg.infinite = !g_dbg.infinite;
                    logf("debug: infinite lives/koins/keys %s", g_dbg.infinite ? "on" : "off");
                    break;
                case kItemGameSpeed:
                    g_dbg.gameSpeed = (g_dbg.gameSpeed + 5 + step) % 5;
                    logf("debug: game speed %s", kGameSpeedNames[g_dbg.gameSpeed]);
                    break;
                case kItemGiveAbility:
                    if (pressed & kBtnCross)
                        giveAbility(rdram, ctx, runtime);
                    else
                    {
                        const int n = static_cast<int>(std::size(kGiveAbilities));
                        g_dbg.givePick = (g_dbg.givePick + n + step) % n;
                    }
                    break;
                case kItemKillAll:
                    if (pressed & kBtnCross)
                        despawnEnemies(rdram, ctx, runtime);
                    break;
                case kItemSurvival:
                    if ((pressed & (kBtnLeft | kBtnRight)) && !g_survival.active)
                        g_survivalHere = !g_survivalHere;
                    if (pressed & kBtnCross)
                    {
                        if (g_survival.active)
                            survivalEnd("STOPPED");
                        else
                        {
                            survivalStart(rdram, ctx, runtime);
                            if (g_survival.active)
                                g_dbg.menuOpen = false; // straight into the fight
                        }
                    }
                    break;
                case kItemSnapshot:
                    if (pressed & kBtnCross)
                        saveRamSnapshot();
                    break;
                case kItemProfile:
                    if (pressed & kBtnCross)
                        startMenuProfile();
                    break;
                case kItemNetplay:
                    netOpen((g_net.mode + (step < 0 ? 2 : 1)) % 3);
                    if (!g_net.mode && g_p2.enabled)
                        g_p2.enabled = false;
                    break;
                case kItemViewLag:
                    // OFF -> ON delay 0 -> 1 -> 2 -> OFF
                    if (!g_splitEnabled)
                    {
                        g_splitEnabled = true;
                        g_viewLag = 0;
                    }
                    else if (g_viewLag < 2)
                        ++g_viewLag;
                    else
                        g_splitEnabled = false;
                    logf("debug: split view %s, delay %d", g_splitEnabled ? "on" : "off", g_viewLag);
                    break;
                case kItemControls:
                    g_net.controls = (g_net.controls + (step < 0 ? 3 : 1)) % 4;
                    logf("debug: my controls %d", g_net.controls);
                    break;
                case kItemPlayer2:
                    g_p2.enabled = !g_p2.enabled;
                    if (g_p2.enabled && !g_p2.exists)
                        g_p2.wantCreate = true; // made after this level frame
                    logf("debug: player 2 %s", g_p2.enabled ? "on" : "off");
                    break;
                case kItemUnfreeze:
                    if (pressed & kBtnCross)
                    {
                        // Back to the walking state; the log line before tells which state he was held in.
                        logf("debug: release Maximo: state %u -> 0 (app mode %u, submode %u)",
                             rd32(kPlayerAddr + 0x248u), rd32(kAppModeAddr), rd32(kAppSubmodeAddr));
                        logFreezeDiff();
                        wr32(kPlayerAddr + 0x248u, 0u);
                        wr32(kPlayerControlFlag, 1u); // the real cause: the controls-enabled flag a boss death clears
                        g_dbg.spawnMessage = "RELEASED MAXIMO";
                    }
                    break;
                case kItemSpawnAny:
                    if (pressed & kBtnCross)
                        spawnPickups(rdram, ctx, runtime, 5);
                    else if (!g_dbg.anyClasses.empty())
                    {
                        const int n = static_cast<int>(g_dbg.anyClasses.size());
                        g_dbg.anyPick = (std::clamp(g_dbg.anyPick, 0, n - 1) + n + step) % n;
                    }
                    break;
                case kItemSpawnCount:
                    g_dbg.spawnCount = (g_dbg.spawnCount + 4 + step) % 4;
                    break;
                case kItemClose:
                    g_dbg.menuOpen = false;
                    break;
                }
            if (pressed & kBtnCircle)
                g_dbg.menuOpen = false;
            changed = changed || pressed != 0;
        }

        // Fly once per guest frame (the game also polls during movies, many times a frame).
        const uint64_t frame = g_guestFrame.load(std::memory_order_relaxed);
        if (g_dbg.freeCam && !g_dbg.menuOpen && frame != g_dbg.lastMoveFrame)
        {
            g_dbg.lastMoveFrame = frame;
            if (!g_dbg.freeCamReady)
                startFreeCamera();
            if (g_dbg.freeCamReady)
                moveFreeCamera();
            // TRIANGLE: pause / resume the game while flying (the menu's PAUSE GAME IN FREE CAMERA).
            // (R3 instead while Maximo is moved: triangle is his.)
            if ((pressed & (g_dbg.moveMaximo ? kBtnR3 : kBtnTriangle)) != 0)
            {
                g_dbg.pauseInFreeCam = !g_dbg.pauseInFreeCam;
                logf("debug: pause in free camera %s (triangle)", g_dbg.pauseInFreeCam ? "on" : "off");
                showHints();
                changed = true;
            }
            // Zoom while flying: R1 in, L1 out (both together is the menu combo), 1x-8x.
            const bool r1 = (g_dbg.held & kBtnR1) != 0, l1 = (g_dbg.held & kBtnL1) != 0;
            if (r1 != l1 && !g_dbg.moveMaximo)
                g_dbg.zoom = std::clamp(g_dbg.zoom * (r1 ? 1.03f : 1.0f / 1.03f), 1.0f, 8.0f);
        }
        g_cameraZoom = g_dbg.freeCam ? g_dbg.zoom : 1.0f;
        static uint64_t lastPublish = ~0ull;
        static uint64_t resultUntil = 0;
        if (!g_survival.result.empty() && resultUntil == 0)
            resultUntil = g_dbg.padFrames + 600u; // the result stays up 10 seconds
        if (resultUntil && g_dbg.padFrames >= resultUntil)
        {
            g_survival.result.clear();
            resultUntil = 0;
            changed = true;
        }
        // The menu just closed: the options' on-screen lines show for 3 seconds more.
        {
            static bool wasOpen = false;
            if (wasOpen && !g_dbg.menuOpen)
                showHints();
            wasOpen = g_dbg.menuOpen;
        }
        if (changed || ((g_dbg.showPosition || g_dbg.freeCam || g_survival.active || !g_survival.result.empty()) &&
                        frame != lastPublish))
        {
            lastPublish = frame;
            publishDebugOverlay();
        }

        // (No clip too: the stick flies Maximo, so the game must not also walk him with it.)
        // Buttons pressed in the menu (CROSS to change an option, CIRCLE to close it) stay away from the game until
        // they are let go: closing the menu with them held made Maximo jump or attack.
        static bool swallow = false;
        if (g_dbg.menuOpen || combo)
            swallow = true;
        else if ((buttons & ~(kBtnUp | kBtnDown | kBtnLeft | kBtnRight)) == 0)
            swallow = false;
        if (swallow && !g_dbg.menuOpen && !combo)
        {
            data[2] = data[3] = 0xFF;
        }
        if (g_dbg.freeCam && g_dbg.moveMaximo && !g_dbg.menuOpen && !combo && !g_noClip)
        {
            // MOVE MAXIMO IN FREE CAMERA: the game keeps the left stick and the buttons; the d-pad (the camera's
            // flying), L2/R2 (its up/down) and the right stick (its look) are taken out.
            data[2] |= 0xF4;              // d-pad and R3 released (active low: up, right, down, left; R3 the pause)
            data[3] |= 0x03;              // L2, R2 released
            data[4] = data[5] = 0x80;     // right stick centred
        }
        else if (g_dbg.menuOpen || g_dbg.freeCam || combo || g_noClip)
        {
            data[2] = data[3] = 0xFF; // nothing pressed
            data[4] = data[5] = data[6] = data[7] = 0x80; // sticks centred
        }
    }

    // FIRST PERSON CAMERA: the eye at Maximo's head, looking the way he faces. His facing comes from his actor's
    // matrix (actor +0x60, 4x4; the actor is the first word of his body, Player +0): which of its axes is "forward"
    // (a row or a column, either sign) is found while he walks - the axis that keeps pointing the way his feet move
    // wins. The right stick looks around from there (springing back when let go). The look-at turns the game's
    // camera angles too, so his stick moves him the way the view looks.
    struct FirstPerson
    {
        float lastFeet[3] = {0.0f, 0.0f, 0.0f};
        bool haveLast = false;
        float votes[8] = {};
        int axis = -1;
        float yaw = 0.0f, pitch = 0.0f; // the right stick's look, radians
        float heading[3] = {0.0f, 0.0f, 1.0f};
        float eyeY = 0.0f;
        bool haveEyeY = false;
    } g_fp;
    bool firstPersonView(float eye[3], float target[3])
    {
        float feet[3];
        if (!maximoFeet(feet))
            return false;
        const uint32_t body = rd32(GA(0x00263540u));
        const uint32_t actor = body && (body & PS2_RAM_MASK) == body ? rd32(body) : 0u;
        if (!actor || (actor & PS2_RAM_MASK) != actor)
            return false;
        float m[16];
        std::memcpy(m, mem(actor + 0x60u), sizeof m);
        // Candidates, flattened: rows 0 and 2, columns 0 and 2, each either way.
        float cand[8][2];
        const float base[4][2] = {{m[0], m[2]}, {m[8], m[10]}, {m[0], m[8]}, {m[2], m[10]}};
        for (int i = 0; i < 4; ++i)
        {
            cand[i][0] = base[i][0];
            cand[i][1] = base[i][1];
            cand[i + 4][0] = -base[i][0];
            cand[i + 4][1] = -base[i][1];
        }
        if (g_fp.haveLast)
        {
            const float dx = feet[0] - g_fp.lastFeet[0], dz = feet[2] - g_fp.lastFeet[2];
            const float d = std::sqrt(dx * dx + dz * dz);
            if (d > 0.05f && d < 5.0f)
                for (int i = 0; i < 8; ++i)
                {
                    const float l = std::sqrt(cand[i][0] * cand[i][0] + cand[i][1] * cand[i][1]);
                    if (l > 0.1f)
                        g_fp.votes[i] = g_fp.votes[i] * 0.995f + (cand[i][0] * dx + cand[i][1] * dz) / (l * d);
                }
        }
        std::memcpy(g_fp.lastFeet, feet, sizeof feet);
        g_fp.haveLast = true;
        int best = g_fp.axis < 0 ? 1 : g_fp.axis; // (row 2, the model's +Z, until he has walked)
        for (int i = 0; i < 8; ++i)
            if (g_fp.votes[i] > g_fp.votes[best] + 2.0f)
                best = i;
        if (best != g_fp.axis)
        {
            g_fp.axis = best;
            logf("debug: first person camera: Maximo's forward is axis %d", best);
        }
        float hx = cand[best][0], hz = cand[best][1];
        const float hl = std::sqrt(hx * hx + hz * hz);
        if (hl > 0.1f)
        {
            // smoothed a little, so the view does not snap with every turn of his body
            g_fp.heading[0] += (hx / hl - g_fp.heading[0]) * 0.35f;
            g_fp.heading[2] += (hz / hl - g_fp.heading[2]) * 0.35f;
        }
        // The right stick: look around, back to straight ahead when it is let go.
        const float sx = stickAxis(g_dbg.rx), sy = stickAxis(g_dbg.ry);
        // (gentler than at first: at most about 50 degrees to the side and 30 up or down, eased in)
        const float wantYaw = sx * 0.9f * (g_dbg.invertX ? -1.0f : 1.0f), wantPitch = -sy * 0.5f * (g_dbg.invertY ? -1.0f : 1.0f);
        g_fp.yaw += (wantYaw - g_fp.yaw) * 0.08f;
        g_fp.pitch += (wantPitch - g_fp.pitch) * 0.08f;
        float h[2] = {g_fp.heading[0], g_fp.heading[2]};
        const float l = std::sqrt(h[0] * h[0] + h[1] * h[1]);
        if (l < 0.01f)
            return false;
        h[0] /= l;
        h[1] /= l;
        const float c = std::cos(g_fp.yaw), s = std::sin(g_fp.yaw);
        const float dirx = h[0] * c - h[1] * s, dirz = h[0] * s + h[1] * c;
        // The eye over his model's own position (actor +0xA0), not gPlayer's feet point, which dropped during
        // jumping attacks and stayed low; its height eased so jumps and landings do not jolt the view.
        constexpr float kEyeHeight = 4.2f, kEyeAhead = 0.9f;
        float model[3];
        std::memcpy(model, mem(actor + 0xA0u), 12);
        const bool modelOk = std::isfinite(model[0]) && std::isfinite(model[1]) && std::isfinite(model[2]) &&
                             std::fabs(model[0] - feet[0]) + std::fabs(model[2] - feet[2]) < 6.0f;
        const float *at = modelOk ? model : feet;
        if (!g_fp.haveEyeY || std::fabs(at[1] - g_fp.eyeY) > 20.0f)
            g_fp.eyeY = at[1];
        g_fp.eyeY += (at[1] - g_fp.eyeY) * 0.3f;
        g_fp.haveEyeY = true;
        eye[0] = at[0] + h[0] * kEyeAhead;
        eye[1] = g_fp.eyeY + kEyeHeight;
        eye[2] = at[2] + h[1] * kEyeAhead;
        const float cp = std::cos(g_fp.pitch), sp = std::sin(g_fp.pitch);
        target[0] = eye[0] + dirx * cp * 16.0f;
        target[1] = eye[1] + sp * 16.0f;
        target[2] = eye[2] + dirz * cp * 16.0f;
        return true;
    }

    // func_0013C6C0(camera, matrix, eye, target): note the game's camera; with the free camera on, look from ours.
    void debugLookAtEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t lookCamera = reg(ctx, 4), lookMatrix = reg(ctx, 5), lookEye = reg(ctx, 6), lookTarget = reg(ctx, 7);
        if (ctx->pc == kLookAtFn && g_p2.pass && lookCamera == kPlayerAddr)
        {
            // Player 2's turn: his camera, for his view in SPLIT VIEW frames.
            std::memcpy(g_p2Eye, mem(lookEye), 12);
            std::memcpy(g_p2Target, mem(lookTarget), 12);
            g_haveP2Camera = std::isfinite(g_p2Eye[0]) && std::isfinite(g_p2Target[0]);
            g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
            return;
        }
        if (ctx->pc == kLookAtFn)
        {
            const uint32_t eyeAddr = reg(ctx, 6), targetAddr = reg(ctx, 7);
            if (g_dbg.freeCam && g_dbg.freeCamReady)
            {
                float target[3];
                for (int i = 0; i < 3; ++i)
                    target[i] = g_dbg.eye[i] + g_dbg.dir[i] * 16.0f;
                std::memcpy(mem(eyeAddr), g_dbg.eye, 12); // w (+12) is left as the game set it
                std::memcpy(mem(targetAddr), target, 12);
            }
            else
            {
                std::memcpy(g_dbg.gameEye, mem(eyeAddr), 12);
                std::memcpy(g_dbg.gameTarget, mem(targetAddr), 12);
                g_dbg.haveGameCamera = std::isfinite(g_dbg.gameEye[0]) && std::isfinite(g_dbg.gameTarget[0]);
                float eye[3], target[3];
                if (g_dbg.firstPerson && lookCamera == kPlayerAddr && firstPersonView(eye, target))
                {
                    std::memcpy(mem(eyeAddr), eye, 12);
                    std::memcpy(mem(targetAddr), target, 12);
                }
            }
        }
        const uint32_t viewMatrix = ctx->pc == kLookAtFn ? reg(ctx, 5) : 0u;
        g_original[slotOf(ctx->pc)](rdram, ctx, runtime);
        // FREE CAMERA ZOOM: the projection is built once per level, so scaling it did nothing. The look-at's view
        // matrix (row vectors: column 0 is view x, column 1 view y) is scaled instead: x and y grow, depth does
        // not, which is a zoom; culling works in the same scaled space, so what leaves the screen is still culled.
        if (viewMatrix && g_dbg.freeCam && g_dbg.freeCamReady && g_dbg.zoom > 1.001f)
        {
            float m[16];
            std::memcpy(m, mem(viewMatrix), sizeof m);
            for (int r = 0; r < 4; ++r)
            {
                m[r * 4 + 0] *= g_dbg.zoom;
                m[r * 4 + 1] *= g_dbg.zoom;
            }
            std::memcpy(mem(viewMatrix), m, sizeof m);
        }
        // SPLIT VIEW, player 2's frame: the look-at again from his eye and target, so the view matrix (and the
        // render matrices and culling that follow it) are his; player 1's camera angles (camera +0x1C0..+0x1C8,
        // which his camera-relative controls use) and the caller's eye and target are put back.
        if (viewMatrix && g_viewP2Now && g_haveP2Camera && !g_p2.pass && lookCamera == kPlayerAddr &&
            !(g_dbg.freeCam && g_dbg.freeCamReady))
        {
            uint8_t angles[12], eye[16], target[16];
            std::memcpy(angles, mem(lookCamera + 0x1C0u), sizeof angles);
            std::memcpy(eye, mem(lookEye), sizeof eye);
            std::memcpy(target, mem(lookTarget), sizeof target);
            std::memcpy(mem(lookEye), g_p2Eye, 12);
            std::memcpy(mem(lookTarget), g_p2Target, 12);
            uint32_t v0 = 0;
            callGuestArgs(rdram, ctx, runtime, kLookAtFn, {lookCamera, lookMatrix, lookEye, lookTarget}, v0);
            std::memcpy(mem(lookCamera + 0x1C0u), angles, sizeof angles);
            std::memcpy(mem(lookEye), eye, sizeof eye);
            std::memcpy(mem(lookTarget), target, sizeof target);
        }
    }

    // Window thread: the overlay onto a finished RGBA frame.
    // Survival's line: centred at the top of the screen, a little larger than the menu's text.
    // A line of text in a dark box at the top of the picture: align -1 left, 0 middle, 1 right; size scales the
    // letters (1.0: the debug menu's size; any size, the 5x7 letters are sampled per pixel).
    void drawDebugBanner(std::vector<uint8_t> &px, uint32_t width, uint32_t height, const std::string &text,
                         int align = 0, float size = 1.0f)
    {
        const float s = std::max(1.0f, static_cast<float>(std::max(1u, height / 200u)) * size);
        const int cw = static_cast<int>(std::lround(6.0f * s)), pad = static_cast<int>(std::lround(5.0f * s));
        const int glyphH = static_cast<int>(std::lround(7.0f * s));
        const int textW = static_cast<int>(text.size()) * cw;
        const int margin = static_cast<int>(std::lround(6.0f * s));
        const int boxW = std::min<int>(static_cast<int>(width), textW + 2 * pad - (cw - static_cast<int>(std::lround(5.0f * s))));
        const int boxH = glyphH + 2 * pad;
        const int x0 = align > 0 ? static_cast<int>(width) - boxW - margin
                       : align < 0 ? margin
                                   : (static_cast<int>(width) - boxW) / 2;
        const int y0 = margin;
        auto blend = [&](int x, int y, uint8_t r, uint8_t g, uint8_t b, int alpha)
        {
            if (x < 0 || y < 0 || x >= static_cast<int>(width) || y >= static_cast<int>(height))
                return;
            uint8_t *p = &px[(static_cast<size_t>(y) * width + x) * 4u];
            p[0] = static_cast<uint8_t>((p[0] * (255 - alpha) + r * alpha) / 255);
            p[1] = static_cast<uint8_t>((p[1] * (255 - alpha) + g * alpha) / 255);
            p[2] = static_cast<uint8_t>((p[2] * (255 - alpha) + b * alpha) / 255);
        };
        for (int y = y0; y < y0 + boxH; ++y)
            for (int x = x0; x < x0 + boxW; ++x)
                blend(x, y, 0, 0, 0, 150);
        const int glyphW = static_cast<int>(std::lround(5.0f * s));
        for (size_t ci = 0; ci < text.size(); ++ci)
        {
            const DebugGlyph *gl = debugGlyph(text[ci]);
            const int tx = x0 + pad + static_cast<int>(ci) * cw, ty = y0 + pad;
            if (tx + glyphW > x0 + boxW)
                break;
            for (int y = 0; y < glyphH; ++y)
                for (int x = 0; x < glyphW; ++x)
                {
                    const int row = std::min(6, static_cast<int>(y / s)), col = std::min(4, static_cast<int>(x / s));
                    if (gl->rows[row][col] == '#')
                        blend(tx + x, ty + y, 255, 220, 90, 255);
                }
        }
    }

    void drawDebugOverlay(std::vector<uint8_t> &px, uint32_t width, uint32_t height)
    {
        if (px.size() >= static_cast<size_t>(width) * height * 4u && width >= 64 && height >= 64)
        {
            drawHealthBars(px, width, height);
            std::string p2;
            {
                std::lock_guard<std::mutex> lock(g_p2HudMutex);
                p2 = g_p2Hud;
            }
            if (!p2.empty())
                drawDebugBanner(px, width, height, p2, 1); // player 2's health, armor and lives, top right
        }
        DebugOverlay o;
        {
            std::lock_guard<std::mutex> lock(g_dbgOverlayMutex);
            if (g_dbgOverlay.lines.empty() && g_dbgOverlay.banner.empty())
                return;
            o = g_dbgOverlay;
        }
        if (px.size() < static_cast<size_t>(width) * height * 4u || width < 64 || height < 64)
            return;
        if (!o.banner.empty())
            drawDebugBanner(px, width, height, o.banner, 0, o.bannerSize);
        if (!o.timer.empty())
            drawDebugBanner(px, width, height, o.timer, -1, o.bannerSize); // SURVIVAL: the run's time, top left
        if (o.lines.empty())
            return;
        // Pixel size of one font dot: the menu (now 27 lines) must fit the window height.
        const int rows = static_cast<int>(o.lines.size()) + 4;
        const int s = std::max(1, static_cast<int>(height) / std::max(270, rows * 11));
        const int cw = 6 * s, ch = 10 * s, margin = 8 * s;
        size_t longest = 0;
        for (const std::string &l : o.lines)
            longest = std::max(longest, l.size() + 2);
        const int boxW = std::min<int>(static_cast<int>(width) - 2 * margin, static_cast<int>(longest) * cw + 2 * margin);
        const int boxH = std::min<int>(static_cast<int>(height) - 2 * margin,
                                       static_cast<int>(o.lines.size()) * ch + 2 * margin);
        auto blend = [&](int x, int y, uint8_t r, uint8_t g, uint8_t b, int alpha)
        {
            if (x < 0 || y < 0 || x >= static_cast<int>(width) || y >= static_cast<int>(height))
                return;
            uint8_t *p = &px[(static_cast<size_t>(y) * width + x) * 4u];
            p[0] = static_cast<uint8_t>((p[0] * (255 - alpha) + r * alpha) / 255);
            p[1] = static_cast<uint8_t>((p[1] * (255 - alpha) + g * alpha) / 255);
            p[2] = static_cast<uint8_t>((p[2] * (255 - alpha) + b * alpha) / 255);
        };
        const int x0 = margin, y0 = margin;
        for (int y = y0; y < y0 + boxH; ++y)
            for (int x = x0; x < x0 + boxW; ++x)
                blend(x, y, 0, 0, 0, o.panel ? 190 : 140);
        for (size_t li = 0; li < o.lines.size(); ++li)
        {
            const bool title = o.panel && li == 0, hi = static_cast<int>(li) == o.highlight;
            const uint8_t r = hi ? 255 : title ? 255 : 230, g = hi ? 210 : title ? 190 : 230, b = hi ? 60 : title ? 60 : 230;
            const std::string text = (o.highlight >= 0 && li >= 2 && li < 2u + kItemCount ? (hi ? "> " : "  ") : "") +
                                     o.lines[li];
            const int ty = y0 + margin + static_cast<int>(li) * ch;
            for (size_t ci = 0; ci < text.size(); ++ci)
            {
                const DebugGlyph *gl = debugGlyph(text[ci]);
                const int tx = x0 + margin + static_cast<int>(ci) * cw;
                if (tx + cw > x0 + boxW)
                    break;
                for (int row = 0; row < 7; ++row)
                    for (int col = 0; col < 5; ++col)
                        if (gl->rows[row][col] == '#')
                            for (int dy = 0; dy < s; ++dy)
                                for (int dx = 0; dx < s; ++dx)
                                    blend(tx + col * s + dx, ty + row * s + dy, r, g, b, 255);
            }
        }
    }

    // ---- scripted controller input ---------------------------------------------------------------
    //
    // Script: comma-separated "start:BUTTONS:length" steps, BUTTONS joined by '+'. Times count
    // scePadRead calls (the game polls once per frame), so a script replays identically however fast
    // the host runs. Example: "90:RIGHT:4,150:CROSS:4". LUP/LDOWN/LLEFT/LRIGHT push the left analog stick
    // to its limit (the game steers Maximo with it in DualShock analog mode). LX=hh / LY=hh set an exact stick byte.
    // A start written "f<n>" is a guest frame (sceGsSyncV count) instead: the step holds its buttons for the first
    // `length` pad reads from that frame on, or from the <d>th read after it with "f<n>+<d>". Movies poll the pad
    // without drawing frames, so this keeps steps after a movie in place however many reads it took.
    // A step written "t<ms>:BUTTONS:<ms>" is timed in guest milliseconds on the EE clock, so it replays the same at any
    // field rate (A/B runs of MAXIMO_FRAME_RATE).

    struct PadStep
    {
        uint64_t start, length;
        uint16_t pressed; // active-high mask in libpad bit order
        std::string text;
        uint8_t lx = 0x80, ly = 0x80; // left stick, 0x80 = centred
        uint8_t rx = 0x80, ry = 0x80; // right stick
        bool byFrame = false;
        bool byTime = false;    // "t<ms>:...:<ms>": start and length in guest milliseconds (EE clock)
        bool anchored = false;  // "a<ms>:...:<ms>": the same, counted from the MAXIMO_PAD_ANCHOR moment
        uint64_t delay = 0;     // pad reads from the frame to the first press, for a frame-keyed step
        uint64_t readsHeld = 0; // pad reads so far of a frame-keyed step, from its frame on
    };
    std::vector<PadStep> g_padScript;
    uint16_t g_padApplied = 0;

    uint16_t padButton(const std::string &name)
    {
        static const std::map<std::string, uint16_t> names = {
            {"SELECT", 1u << 0}, {"L3", 1u << 1},       {"R3", 1u << 2},      {"START", 1u << 3},
            {"UP", 1u << 4},     {"RIGHT", 1u << 5},    {"DOWN", 1u << 6},    {"LEFT", 1u << 7},
            {"L2", 1u << 8},     {"R2", 1u << 9},       {"L1", 1u << 10},     {"R1", 1u << 11},
            {"TRIANGLE", 1u << 12}, {"CIRCLE", 1u << 13}, {"CROSS", 1u << 14}, {"SQUARE", 1u << 15},
        };
        const auto it = names.find(name);
        return it == names.end() ? 0 : it->second;
    }

    bool parsePadScript(const std::string &script)
    {
        size_t pos = 0;
        while (pos < script.size())
        {
            size_t end = script.find(',', pos);
            if (end == std::string::npos)
                end = script.size();
            const std::string step = script.substr(pos, end - pos);
            pos = end + 1;
            const size_t a = step.find(':'), b = step.rfind(':');
            if (a == std::string::npos || a == b)
                return false;
            const bool byFrame = step[0] == 'f', anchored = step[0] == 'a', byTime = step[0] == 't' || anchored;
            PadStep s{std::stoull(step.substr(byFrame || byTime ? 1 : 0, a)), std::stoull(step.substr(b + 1)), 0, step};
            s.byFrame = byFrame;
            s.byTime = byTime;
            s.anchored = anchored;
            if (const size_t plus = step.find('+'); byFrame && plus < a)
                s.delay = std::stoull(step.substr(plus + 1, a - plus - 1));
            const std::string buttons = step.substr(a + 1, b - a - 1);
            size_t p = 0;
            while (p <= buttons.size())
            {
                size_t q = buttons.find('+', p);
                if (q == std::string::npos)
                    q = buttons.size();
                const std::string name = buttons.substr(p, q - p);
                const uint16_t m = padButton(name);
                if (name == "LUP")
                    s.ly = 0x00;
                else if (name == "LDOWN")
                    s.ly = 0xFF;
                else if (name == "LLEFT")
                    s.lx = 0x00;
                else if (name == "LRIGHT")
                    s.lx = 0xFF;
                else if (name == "RUP")
                    s.ry = 0x00;
                else if (name == "RDOWN")
                    s.ry = 0xFF;
                else if (name == "RLEFT")
                    s.rx = 0x00;
                else if (name == "RRIGHT")
                    s.rx = 0xFF;
                else if (name.size() > 3 && (name.compare(0, 3, "LX=") == 0 || name.compare(0, 3, "LY=") == 0))
                    (name[1] == 'X' ? s.lx : s.ly) = static_cast<uint8_t>(std::stoul(name.substr(3), nullptr, 16));
                else if (!m)
                    return false;
                s.pressed |= m;
                p = q + 1;
            }
            g_padScript.push_back(s);
        }
        return true;
    }

    void scriptedPadRead(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t padPort = reg(ctx, 4), padData = reg(ctx, 6); // scePadRead(port, slot, data)
        if (g_p2.pass && padData != 0)
        {
            fillPlayer2Pad(padData); // player 2's turn: the second controller
            SET_GPR_U32(ctx, 2, 1u);
            return;
        }
        const uint64_t n = g_padReads++;
        const uint64_t guestMs = runtime->eeScheduler().currentCycle() / (EeScheduler::kEeClockHz / 1000u);
        // MAXIMO_PAD_ANCHOR=<hex address>: "a" steps count from the first pad read at which that word is non-zero
        // (for A/B runs: e.g. the player's position, so input lines up with the level start at any frame rate).
        static const uint32_t anchorAddr = []
        {
            const char *text = std::getenv("MAXIMO_PAD_ANCHOR");
            return text ? static_cast<uint32_t>(std::strtoul(text, nullptr, 16)) & ~3u : 0u;
        }();
        static uint64_t anchorMs = UINT64_MAX;
        if (anchorAddr && anchorMs == UINT64_MAX && rd32(anchorAddr) != 0)
        {
            anchorMs = guestMs;
            logf("pad script anchor: 0x%06X set at guest %llu ms, pad read #%llu", anchorAddr,
                 static_cast<unsigned long long>(guestMs), static_cast<unsigned long long>(n));
        }
        // MAXIMO_RAM_DUMP_MS=ms0,ms1,...: RDRAM to ram_a<ms>.bin at the first pad read that many guest ms after the
        // anchor (A/B runs compare the game's state at the same game time).
        if (static const char *dumps = std::getenv("MAXIMO_RAM_DUMP_MS"); dumps && anchorMs != UINT64_MAX)
        {
            static size_t next = 0;
            static std::vector<uint64_t> times = [&]
            {
                std::vector<uint64_t> v;
                for (const char *p = dumps; *p;)
                {
                    char *end = nullptr;
                    v.push_back(std::strtoull(p, &end, 10));
                    if (end == p)
                        break;
                    p = *end == ',' ? end + 1 : end;
                }
                return v;
            }();
            while (next < times.size() && guestMs - anchorMs >= times[next])
            {
                char name[48];
                std::snprintf(name, sizeof(name), "ram_a%llu.bin", static_cast<unsigned long long>(times[next]));
                std::ofstream(name, std::ios::binary).write(reinterpret_cast<const char *>(g_rdram), PS2_RAM_SIZE);
                logf("RAM dump %s at anchor + %llu ms, frame %llu", name,
                     static_cast<unsigned long long>(guestMs - anchorMs), static_cast<unsigned long long>(g_guestFrame.load()));
                ++next;
            }
        }
        uint16_t pressed = 0;
        uint8_t lx = 0x80, ly = 0x80, rx = 0x80, ry = 0x80;
        for (PadStep &s : g_padScript)
        {
            if (s.anchored && anchorMs == UINT64_MAX)
                continue;
            const uint64_t ms = s.anchored ? guestMs - anchorMs : guestMs;
            if (s.byTime ? ms >= s.start && ms < s.start + s.length
                : s.byFrame ? g_guestFrame.load(std::memory_order_relaxed) >= s.start && s.readsHeld++ >= s.delay &&
                                  s.readsHeld <= s.delay + s.length
                            : n >= s.start && n < s.start + s.length)
            {
                pressed |= s.pressed;
                lx = s.lx != 0x80 ? s.lx : lx;
                ly = s.ly != 0x80 ? s.ly : ly;
                rx = s.rx != 0x80 ? s.rx : rx;
                ry = s.ry != 0x80 ? s.ry : ry;
            }
        }
        const bool stickMoved = lx != 0x80 || ly != 0x80 || rx != 0x80 || ry != 0x80;
        // MAXIMO_RAM_DUMP=n0,n1,...: write all of RDRAM to ram_<n>.bin at these pad reads (diagnostic snapshots).
        if (static const char *dumps = std::getenv("MAXIMO_RAM_DUMP"); dumps)
            for (const char *p = dumps; *p;)
            {
                char *end = nullptr;
                const uint64_t wanted = std::strtoull(p, &end, 10);
                if (end == p)
                    break;
                if (wanted == n)
                {
                    char name[48];
                    std::snprintf(name, sizeof(name), "ram_%llu.bin", static_cast<unsigned long long>(n));
                    std::ofstream(name, std::ios::binary).write(reinterpret_cast<const char *>(g_rdram), PS2_RAM_SIZE);
                    logf("RAM dump %s at pad read #%llu, frame %llu", name, static_cast<unsigned long long>(n),
                         static_cast<unsigned long long>(g_guestFrame.load()));
                }
                p = *end == ',' ? end + 1 : end;
            }
        // MAXIMO_PEEK=first:last:addr/count[,addr#count...]: log `count` floats (/) or hex words (#) at each addr on
        // pad reads [first, last].
        if (static const char *peek = std::getenv("MAXIMO_PEEK"); peek)
        {
            unsigned long long first = 0, last = 0;
            int used = 0;
            if (std::sscanf(peek, "%llu:%llu:%n", &first, &last, &used) == 2 && n >= first && n <= last)
            {
                std::string line;
                for (const char *p = peek + used; *p;)
                {
                    unsigned addr = 0, count = 0;
                    char kind = 0;
                    int len = 0;
                    if (std::sscanf(p, "%x%c%u%n", &addr, &kind, &count, &len) != 3 || (kind != '/' && kind != '#'))
                        break;
                    char b[32];
                    std::snprintf(b, sizeof b, " %X:", addr);
                    line += b;
                    for (unsigned i = 0; i < count; ++i)
                    {
                        if (kind == '#')
                            std::snprintf(b, sizeof b, " %X", rd32(addr + 4 * i));
                        else
                        {
                            float f;
                            std::memcpy(&f, mem(addr + 4 * i), 4);
                            std::snprintf(b, sizeof b, " %.4g", f);
                        }
                        line += b;
                    }
                    p += len;
                    p += *p == ',';
                }
                logf("PEEK #%llu f%llu t%llu%s", static_cast<unsigned long long>(n),
                     static_cast<unsigned long long>(g_guestFrame.load()), static_cast<unsigned long long>(guestMs),
                     line.c_str());
            }
        }
        // The probes below look at most once per guest VBlank. In play the game reads the pad once per frame, but
        // during a movie it polls it ~30,000 times a second, and probing every read cost 5-7% of the game thread.
        static uint64_t probeTick = ~0ull;
        const uint64_t tick = runtime->eeScheduler().currentVSyncTick();
        const bool probe = tick != probeTick;
        probeTick = tick;
        // SFX slot monitor: the game's 7 effect slots (halfwords at 0x26271C: 0x3FFF free, |0x4000 requested,
        // |0x8000 playing, 0x3FFE/0xBFFE stopping) and their voice status bytes (0x1E5A42.., one per slot), logged
        // whenever they change. A slot stuck non-free is a leaked voice ("Out of free channels").
        if (probe)
        {
            static uint8_t lastSlots[34] = {};
            uint8_t now[34] = {};
            // MJOLNIR's own per-channel state (SPU_KeyStatus, IOP 0x15EA4; 2 = owned by a stream).
            uint8_t *keyStatus = now + 25;
            (void)g_runtime->readIopMemory(0x15EA4u, keyStatus, 9);
            std::memcpy(now, mem(GA(0x26271Cu)), 14);
            std::memcpy(now + 14, mem(GA(0x1E5A42u)), 7);
            // MJOLNIR's reply (0x2EC980): stream count, 3 words per stream, 5 words, then 2 status bits per channel.
            const uint32_t replyStatus = rd32(GA(0x2EC980u) + 4u * (3u * (rd32(GA(0x2EC980u)) & 0xFFu) + 6u));
            std::memcpy(now + 21, &replyStatus, 4);
            if (std::memcmp(now, lastSlots, sizeof now) != 0)
            {
                std::memcpy(lastSlots, now, sizeof now);
                char line[160];
                int len = 0;
                for (int i = 0; i < 7; ++i)
                {
                    uint16_t s;
                    std::memcpy(&s, now + 2 * i, 2);
                    len += std::snprintf(line + len, sizeof line - len, " %04X/%u", s, now[14 + i]);
                }
                logf("SFX slots #%llu f%llu%s | reply streams %u ch0-15 %08X | IOP keystatus ch0-8 %u%u%u%u%u%u%u%u%u",
                     static_cast<unsigned long long>(n), static_cast<unsigned long long>(g_guestFrame.load()), line,
                     rd32(GA(0x2EC980u)), replyStatus, keyStatus[0], keyStatus[1], keyStatus[2], keyStatus[3],
                     keyStatus[4], keyStatus[5], keyStatus[6], keyStatus[7], keyStatus[8]);
            }
        }
        // Game-side view: FLAG screen fade-busy byte and the pad object's raw/edge button words
        // (pad object 0x263540: +10 raw, +14 newly pressed), as left by the previous frame.
        const bool periodic = probe && tick % 30 == 0;
        if (periodic || pressed != g_padApplied)
            logf("PAD probe read #%llu: fadeBusy[0x2633B6]=%u raw=0x%04X edge=0x%04X type=%u", static_cast<unsigned long long>(n),
                 *mem(GA(0x002633B6u)), rd32(GA(0x0026354Au)) & 0xFFFFu, rd32(GA(0x0026354Eu)) & 0xFFFFu, *mem(GA(0x00263558u)));
        if (periodic)
        {
            // Fade state in the game object 0x25B9E0: +16 frame delta, +0x79D8 rate, +0x79DC value, +0x79E0 target.
            auto f = [](uint32_t a) { float v; std::memcpy(&v, mem(a), 4); return v; };
            logf("FADE probe: dt=%g rate=%g value=%g target=%g", f(GA(0x0025B9F0u)), f(GA(0x002633B8u)), f(GA(0x002633BCu)),
                 f(GA(0x002633C0u)));
        }
        if (pressed != g_padApplied)
        {
            logf("PAD read #%llu: buttons 0x%04X -> 0x%04X (active-high, libpad order)",
                 static_cast<unsigned long long>(n), g_padApplied, pressed);
            g_padApplied = pressed;
        }
        if (pressed || stickMoved)
            ps2_stubs::setPadOverrideState(static_cast<uint16_t>(~pressed), lx, ly, rx, ry);
        else if (g_net.controls != 0 && padPort == 0)
        {
            // MY CONTROLS: player 1 from the keyboard only, or one gamepad only (two windows on one PC: the gamepads
            // reach both, the keyboard only the window in front).
            uint16_t b = 0;
            uint8_t sx = 0x80, sy = 0x80, srx = 0x80, sry = 0x80;
            if (g_net.controls == 1)
            {
                static const struct { int key; uint16_t bit; } kKeys[] = {
                    {265, 1u << 4},  {262, 1u << 5},  {264, 1u << 6},  {263, 1u << 7}, // arrows: d-pad
                    {'Z', 1u << 15}, {'X', 1u << 14}, {'C', 1u << 13}, {'V', 1u << 12},
                    {'Q', 1u << 10}, {'E', 1u << 11}, {'1', 1u << 8},  {'3', 1u << 9},
                    {257, 1u << 3},  {344, 1u << 0},  {341, 1u << 1},  {345, 1u << 2}, // Enter, right Shift, Ctrls
                };
                for (const auto &k : kKeys)
                    if (IsKeyDown(k.key))
                        b |= k.bit;
                const int kx = (IsKeyDown('D') ? 1 : 0) - (IsKeyDown('A') ? 1 : 0);
                const int ky = (IsKeyDown('S') ? 1 : 0) - (IsKeyDown('W') ? 1 : 0);
                sx = static_cast<uint8_t>(kx > 0 ? 0xFF : kx < 0 ? 0x00 : 0x80);
                sy = static_cast<uint8_t>(ky > 0 ? 0xFF : ky < 0 ? 0x00 : 0x80);
            }
            else
            {
                int pad = -1, found = 0;
                for (int i = 0; i < 8 && pad < 0; ++i)
                    if (IsGamepadAvailable(i) && ++found == (g_net.controls == 3 ? 2 : 1))
                        pad = i;
                if (pad >= 0)
                {
                    static const struct { int button; uint16_t bit; } kMap[] = {
                        {1, 1u << 4},  {2, 1u << 5},  {3, 1u << 6},  {4, 1u << 7},  {5, 1u << 12}, {6, 1u << 13},
                        {7, 1u << 14}, {8, 1u << 15}, {9, 1u << 10}, {10, 1u << 8}, {11, 1u << 11}, {12, 1u << 9},
                        {13, 1u << 0}, {15, 1u << 3}, {16, 1u << 1}, {17, 1u << 2},
                    };
                    for (const auto &m : kMap)
                        if (IsGamepadButtonDown(pad, m.button))
                            b |= m.bit;
                    auto axis = [&](int a)
                    {
                        const float v = std::clamp(GetGamepadAxisMovement(pad, a), -1.0f, 1.0f);
                        return static_cast<uint8_t>(std::lround((v + 1.0f) * 127.5f));
                    };
                    sx = axis(0);
                    sy = axis(1);
                    srx = axis(2);
                    sry = axis(3);
                }
            }
            ps2_stubs::setPadOverrideState(static_cast<uint16_t>(~b), sx, sy, srx, sry);
        }
        else
            ps2_stubs::clearPadOverrideState();
        const uint32_t dataAddr = reg(ctx, 6);
        ps2_stubs::scePadRead(rdram, ctx, runtime);
        // The free camera's input: right stick and R3 from the data the game just got (analog mode, ID 0x7_).
        if (g_orbit.on && GPR_S32(ctx, 2) != 0 && dataAddr)
        {
            const uint8_t *d = mem(dataAddr);
            static int lastId = -1;
            if (d[1] != lastId)
            {
                logf("camera orbit: pad ID %02X -> %02X (%s) at frame %llu", lastId & 0xFF, d[1],
                     (d[1] & 0xF0) == 0x70 ? "analog: right stick read" : "not analog: no right stick",
                     static_cast<unsigned long long>(g_guestFrame.load()));
                lastId = d[1];
            }
            if ((d[1] & 0xF0) == 0x70)
            {
                g_orbit.rx = d[4];
                g_orbit.ry = d[5];
                g_orbit.r3 = (d[2] & 0x04) == 0; // active-low, R3 = bit 2 of the first button byte
            }
            else
            {
                g_orbit.rx = g_orbit.ry = 0x80;
                g_orbit.r3 = false;
            }
        }
        if (padPort == 0 && padData != 0)
        {
            // NETPLAY: what this game's player 1 is pressing, for the other game's player 2.
            const uint8_t *d = mem(padData);
            g_net.localButtons = static_cast<uint16_t>(~(d[2] | (d[3] << 8)));
            g_net.localRx = d[4];
            g_net.localRy = d[5];
            g_net.localLx = d[6];
            g_net.localLy = d[7];
        }
        if (padPort == 0 && padData != 0)
            debugPadRead(rdram, ctx, runtime, padData); // debug menu, free camera, spawn (L1 + R1 + SELECT)
        if (padPort == 0 && padData != 0 && g_net.mode == 2)
        {
            // NETPLAY, joining: this pad is player 2 in the host's game; this window's own game gets none of it.
            static uint64_t lastTick = ~0ull;
            if (g_padReads != lastTick)
            {
                lastTick = g_padReads;
                netTick(rdram, ctx, runtime);
            }
            if (g_stream.connected && !g_dbg.menuOpen)
            {
                uint8_t *d = mem(padData);
                d[2] = d[3] = 0xFF;
                d[4] = d[5] = d[6] = d[7] = 0x80;
            }
            // Player 2's numbers from the host, top right.
            char b[96] = {};
            {
                std::lock_guard<std::mutex> lock(g_stream.m);
                if (g_stream.connected && g_stream.p2[3])
                    std::snprintf(b, sizeof b, "YOU (P2)  HEALTH %d  ARMOR %d  LIVES %d", g_stream.p2[0], g_stream.p2[1],
                                  g_stream.p2[2]);
            }
            std::lock_guard<std::mutex> lock(g_p2HudMutex);
            g_p2Hud = b;
        }
        if (padPort == 0 && padData != 0 && !g_p2.pass)
        {
            // SPLIT VIEW: a view frame gets the pad the game frame before it had, so nothing is pressed twice.
            static uint8_t lastGameFrame[32] = {};
            if (g_viewP2Now)
                std::memcpy(mem(padData), lastGameFrame, sizeof lastGameFrame);
            else
                std::memcpy(lastGameFrame, mem(padData), sizeof lastGameFrame);
        }
    }

    // ---- MMIO trace ----------------------------------------------------------------------------

    struct MmioStat
    {
        uint64_t reads = 0, writes = 0;
        uint64_t lastWrite = 0;
        uint32_t lastPc = 0;
    };
    std::map<uint32_t, MmioStat> g_mmio;

    uint64_t mmioAccesses(uint32_t phys, bool writes)
    {
        const auto it = g_mmio.find(phys);
        return it == g_mmio.end() ? 0u : (writes ? it->second.writes : it->second.reads);
    }
    uint64_t g_scratchAccesses = 0, g_uncachedAccesses = 0;
    constexpr uint64_t kMmioEventsPerReg = 8;

    const char *mmioUnit(uint32_t a)
    {
        if (a >= 0x12000000u && a < 0x12002000u) return "GS-priv";
        if (a >= 0x10000000u && a < 0x10002000u) return "TIMER";
        if (a >= 0x10002000u && a < 0x10003000u) return "IPU";
        if (a >= 0x10003000u && a < 0x10003800u) return "GIF";
        if (a >= 0x10003800u && a < 0x10003C00u) return "VIF0";
        if (a >= 0x10003C00u && a < 0x10004000u) return "VIF1";
        if (a >= 0x10004000u && a < 0x10008000u) return "FIFO";
        if (a >= 0x10008000u && a < 0x1000E000u) return "DMAC-ch";
        if (a >= 0x1000E000u && a < 0x1000F000u) return "DMAC";
        if (a >= 0x1000F000u && a < 0x10010000u) return "INTC/SIF/misc";
        if (a >= 0x11000000u && a < 0x11010000u) return "VU-mem";
        return "other";
    }

    void mmioHook(bool isWrite, uint32_t address, uint32_t size, uint64_t lo, uint64_t, const R5900Context *ctx)
    {
        if (address >= 0x70000000u && address < 0x70004000u)
        {
            ++g_scratchAccesses;
            return;
        }
        const uint32_t phys = address & 0x1FFFFFFFu;
        if (phys < PS2_RAM_SIZE)
        {
            ++g_uncachedAccesses; // kseg1 / uncached-accelerated RAM mirrors
            return;
        }
        // Polling loops hit the same register repeatedly; std::map nodes are stable, so cache the last one.
        thread_local uint32_t lastPhys = 0xFFFFFFFFu;
        thread_local MmioStat *lastStat = nullptr;
        if (phys != lastPhys)
        {
            lastStat = &g_mmio[phys];
            lastPhys = phys;
        }
        MmioStat &s = *lastStat;
        const uint64_t n = isWrite ? s.writes++ : s.reads++;
        if (isWrite)
            s.lastWrite = lo;
        s.lastPc = ctx ? ctx->pc : 0;
        if (n < kMmioEventsPerReg)
            logf("HW %-5s %-13s 0x%08X size=%u %s0x%llX pc~0x%06X", isWrite ? "WRITE" : "READ", mmioUnit(phys), phys,
                 size, isWrite ? "value=" : "", static_cast<unsigned long long>(isWrite ? lo : 0), s.lastPc);
        else if (n == kMmioEventsPerReg)
            logf("HW %-5s %-13s 0x%08X further %s not logged individually", isWrite ? "WRITE" : "READ", mmioUnit(phys),
                 phys, isWrite ? "writes" : "reads");
    }

    // ---- wrappers around runtime SDK implementations ---------------------------------------------

    using Fn = PS2Runtime::RecompiledFunction;

    struct Bound
    {
        const char *name;
        Fn impl;
        int nargs;
        uint64_t calls = 0;
    };
    std::map<uint32_t, Bound> g_bound;

    void boundEntry(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t pc = ctx->pc;
        const uint32_t ra = reg(ctx, 31);
        Bound &b = g_bound.at(pc);
        const uint64_t n = b.calls++;
        static const int argRegs[] = {4, 5, 6, 7, 8, 9, 10, 11};
        char args[160] = {};
        int len = 0;
        for (int i = 0; i < b.nargs; ++i)
            len += std::snprintf(args + len, sizeof(args) - len, "%s0x%X", i ? ", " : "", reg(ctx, argRegs[i]));
        if (pc == GA(0x001A2BF8u)) // sceCdStRead(sectors, buffer, mode, err)
            checkGuardWrite("sceCdStRead", reg(ctx, 5), reg(ctx, 4) * 2048u, ra);
        b.impl(rdram, ctx, runtime);
        if (ctx->pc == pc)
            ctx->pc = ra;
        if (n < 32 || (n & (n - 1)) == 0)
            logf("SDK %s(%s) -> %d  [call #%llu, ra=0x%06X]", b.name, args, GPR_S32(ctx, 2),
                 static_cast<unsigned long long>(n + 1), ra);
    }

    // ---- Native audio (MAXIMO_AUDIO=1) ---------------------------------------------------------------
    // The game's own sound driver MJOLNIR.IRX runs in the emulated IOP (ps2xIOP) over a native libsd and SPU2,
    // and serves the sound RPC (sid 0x12345) itself. Without the switch the driver is not run and its RPC is
    // logged and answered with zeroes, as before.
    // MAXIMO_AUDIO_WAV=<file>: also writes the mixed SPU2 output (48 kHz stereo, guest time order) as a WAV file.
    bool g_iopAudio = false;
    constexpr uint32_t kSoundDriverSid = 0x00012345u;
    struct AudioCapture
    {
        std::FILE *wav = nullptr;
        uint64_t frames = 0;
        uint64_t hash = 0xCBF29CE484222325ull; // FNV-1a over the little-endian samples
        int32_t peak = 0;
        double sumSquares = 0.0;
        uint64_t nonzeroFrames = 0;
        uint64_t firstNonzeroFrame = UINT64_MAX;
        // per guest second
        int32_t secondPeak = 0;
        double secondSquares = 0.0;
        uint64_t secondNonzero = 0;
    };
    AudioCapture g_audio;
    uint64_t g_soundRpcCalls = 0;

    // MAXIMO_AUDIO_WAV is written by its own thread: the game thread only appends to a queue, so disk writes (and
    // their occasional stalls) never hold up the emulation.
    struct WavWriter
    {
        std::mutex mutex;
        std::condition_variable wake;
        std::vector<int16_t> pending;
        bool stop = false;
        std::thread thread;

        void start(std::FILE *file)
        {
            thread = std::thread([this, file]()
                                 {
                std::vector<int16_t> chunk;
                for (;;)
                {
                    {
                        std::unique_lock<std::mutex> lock(mutex);
                        wake.wait(lock, [this]() { return stop || !pending.empty(); });
                        if (pending.empty() && stop)
                            return;
                        chunk.swap(pending);
                    }
                    std::fwrite(chunk.data(), sizeof(int16_t), chunk.size(), file);
                    chunk.clear();
                } });
        }
        void append(const int16_t *samples, size_t count)
        {
            {
                std::lock_guard<std::mutex> lock(mutex);
                pending.insert(pending.end(), samples, samples + count);
            }
            wake.notify_one();
        }
        void finish()
        {
            if (!thread.joinable())
                return;
            {
                std::lock_guard<std::mutex> lock(mutex);
                stop = true;
            }
            wake.notify_one();
            thread.join();
        }
    };
    WavWriter g_wavWriter;

    void writeWavHeader(std::FILE *f, uint64_t frames)
    {
        const uint32_t dataBytes = static_cast<uint32_t>(std::min<uint64_t>(frames * 4u, 0xFFFFFFF0ull - 36u));
        auto u32 = [&](uint32_t v) { std::fwrite(&v, 4, 1, f); };
        auto u16 = [&](uint16_t v) { std::fwrite(&v, 2, 1, f); };
        std::fseek(f, 0, SEEK_SET);
        std::fwrite("RIFF", 1, 4, f);
        u32(36u + dataBytes);
        std::fwrite("WAVEfmt ", 1, 8, f);
        u32(16u);
        u16(1u);     // PCM
        u16(2u);     // stereo
        u32(48000u); // SPU2 output rate
        u32(48000u * 4u);
        u16(4u);
        u16(16u);
        std::fwrite("data", 1, 4, f);
        u32(dataBytes);
        std::fseek(f, 0, SEEK_END);
    }

    void onIopAudio(const int16_t *samples, size_t frames)
    {
        AudioCapture &a = g_audio;
        for (size_t i = 0; i < frames; ++i)
        {
            const int16_t l = samples[2 * i], r = samples[2 * i + 1];
            for (const int16_t v : {l, r})
            {
                const uint16_t u = static_cast<uint16_t>(v);
                a.hash = (a.hash ^ (u & 0xFFu)) * 0x100000001B3ull;
                a.hash = (a.hash ^ (u >> 8)) * 0x100000001B3ull;
                const int32_t m = std::abs(static_cast<int32_t>(v));
                a.peak = std::max(a.peak, m);
                a.secondPeak = std::max(a.secondPeak, m);
                a.sumSquares += static_cast<double>(v) * v;
                a.secondSquares += static_cast<double>(v) * v;
            }
            if (l || r)
            {
                ++a.nonzeroFrames;
                ++a.secondNonzero;
                if (a.firstNonzeroFrame == UINT64_MAX)
                    a.firstNonzeroFrame = a.frames;
            }
            ++a.frames;
            if (a.frames % 48000u == 0u)
            {
                logf("[audio] guest second %llu: peak=%d rms=%.1f nonzero frames=%llu",
                     static_cast<unsigned long long>(a.frames / 48000u), a.secondPeak,
                     std::sqrt(a.secondSquares / (2.0 * 48000.0)), static_cast<unsigned long long>(a.secondNonzero));
                a.secondPeak = 0;
                a.secondSquares = 0.0;
                a.secondNonzero = 0;
            }
        }
        if (a.wav)
            g_wavWriter.append(samples, frames * 2u);
    }

    // Per-window audio pipeline report: IOP interpretation and SPU2 mixing cost on the game thread, samples produced
    // against samples the host device consumed, and the host ring's fill level (frames at 48 kHz).
    void logAudioWindow(const char *label, double seconds)
    {
        if (!g_iopAudio || seconds <= 0.0)
            return;
        static ps2x::iop::HostTiming last{};
        static uint64_t lastIopCycles = 0, lastEeCycles = 0, lastVBlank = 0;
        const ps2x::iop::DebugSnapshot iop = g_runtime->iopDebugSnapshot();
        const ps2x::iop::HostTiming &t = iop.hostTiming;
        const PS2Runtime::IopAudioStats a = g_runtime->takeIopAudioStats();
        const uint64_t eeCycles = g_runtime->eeScheduler().currentCycle();
        const uint64_t vblank = g_runtime->eeScheduler().currentVSyncTick();
        const uint64_t samples = t.spu2Samples - last.spu2Samples;
        const double runMs = static_cast<double>(t.runNs - last.runNs) / 1e6;
        const double spu2Ms = static_cast<double>(t.spu2Ns - last.spu2Ns) / 1e6;
        const double outputMs = static_cast<double>(t.outputNs - last.outputNs) / 1e6;
        const uint64_t vblanks = vblank - lastVBlank;
        logf("[audio-perf] %s IOP %.1f ms/s (%llu active runs, %.2f M IOP cycles) SPU2 %.1f ms/s (mix %.0f ns/sample, "
             "output %.1f ms/s, longest sync %.2f ms, longest output %.2f ms) | guest: %llu samples in %llu VBlanks "
             "(%.0f per VBlank, %.0f EE cycles per VBlank) | host: produced %llu consumed %llu silent %llu dropped %llu "
             "underruns %llu callbacks %llu max gap %.1f ms | ring min %llu max %llu now %llu frames, pacing %+d ppm | "
             "clock %s (ticks %llu fallbacks %llu anchors %llu) IOP-EE lead %+.1f ms",
             label, runMs / seconds, static_cast<unsigned long long>(t.activeRuns - last.activeRuns),
             static_cast<double>(iop.emulatorCycles - lastIopCycles) / 1e6, spu2Ms / seconds,
             samples ? (spu2Ms - outputMs) * 1e6 / static_cast<double>(samples) : 0.0, outputMs / seconds,
             static_cast<double>(t.spu2MaxNs) / 1e6, static_cast<double>(t.outputMaxNs) / 1e6,
             static_cast<unsigned long long>(samples), static_cast<unsigned long long>(vblanks),
             vblanks ? static_cast<double>(samples) / static_cast<double>(vblanks) : 0.0,
             vblanks ? static_cast<double>(eeCycles - lastEeCycles) / static_cast<double>(vblanks) : 0.0,
             static_cast<unsigned long long>(a.framesQueued), static_cast<unsigned long long>(a.framesRequested),
             static_cast<unsigned long long>(a.silentFrames), static_cast<unsigned long long>(a.droppedFrames),
             static_cast<unsigned long long>(a.underruns), static_cast<unsigned long long>(a.callbacks),
             static_cast<double>(a.maxCallbackGapUs) / 1e3, static_cast<unsigned long long>(a.minBuffered),
             static_cast<unsigned long long>(a.maxBuffered), static_cast<unsigned long long>(a.buffered), a.pacingPpm,
             a.realTime ? "realtime" : "lockstep", static_cast<unsigned long long>(a.realTimeTicks),
             static_cast<unsigned long long>(a.realTimeFallbacks), static_cast<unsigned long long>(a.realTimeAnchors),
             (static_cast<double>(iop.emulatorCycles) - static_cast<double>(eeCycles) / 8.0) / 36864.0);
        last = t;
        lastIopCycles = iop.emulatorCycles;
        lastEeCycles = eeCycles;
        lastVBlank = vblank;
    }

    void finishAudio()
    {
        if (!g_iopAudio)
            return;
        AudioCapture &a = g_audio;
        logf("[audio] SPU2 output: %llu frames (%.2f s), hash=%016llX peak=%d rms=%.1f nonzero frames=%llu first at "
             "%.3f s; sound RPC calls served by MJOLNIR: %llu",
             static_cast<unsigned long long>(a.frames), a.frames / 48000.0, static_cast<unsigned long long>(a.hash),
             a.peak, a.frames ? std::sqrt(a.sumSquares / (2.0 * a.frames)) : 0.0,
             static_cast<unsigned long long>(a.nonzeroFrames),
             a.firstNonzeroFrame == UINT64_MAX ? -1.0 : a.firstNonzeroFrame / 48000.0,
             static_cast<unsigned long long>(g_soundRpcCalls));
        logf("[audio] host playback: %llu underruns, %llu frames dropped",
             static_cast<unsigned long long>(g_runtime->iopAudioUnderruns()),
             static_cast<unsigned long long>(g_runtime->iopAudioDroppedFrames()));
        if (a.wav)
        {
            g_wavWriter.finish();
            writeWavHeader(a.wav, a.frames);
            std::fclose(a.wav);
            a.wav = nullptr;
        }
        const ps2x::iop::DebugSnapshot iop = g_runtime->iopDebugSnapshot();
        logf("[audio] IOP: %llu cycles, %llu instructions, %u modules, %u threads, %u RPC servers",
             static_cast<unsigned long long>(iop.emulatorCycles), static_cast<unsigned long long>(iop.emulatorInstructions),
             iop.emulatorLoadedModules, iop.emulatorThreads, iop.emulatorRpcServers);
    }

    // ---- SIF / IOP stubs -----------------------------------------------------------------------

    struct RpcServer
    {
        uint32_t sid;
        uint64_t calls = 0;
    };
    std::vector<RpcServer> g_servers; // index = fake IOP server address slot
    constexpr uint32_t kFakeServerBase = 0x001F0000u; // IOP-side addresses handed back to the EE
    std::set<std::string> g_loadedModules;
    int32_t g_nextModuleId = 1;
    std::map<uint32_t, uint64_t> g_unhandledRpc; // (sid << 8 | fn) -> count

    const char *sidName(uint32_t sid)
    {
        switch (sid)
        {
        case 0x80000001: return "FILEIO";
        case 0x80000003: return "IOP heap";
        case 0x80000006: return "LOADFILE";
        case 0x80000100: return "PADMAN";
        case 0x80000101: return "PADMAN ext";
        case 0x80000400: return "MCSERV";
        case 0x80000592: return "CDVD init/N";
        case 0x80000593: return "CDVD S-cmd";
        case 0x80000595: return "CDVD N-cmd";
        case 0x80000597: return "CDVD search";
        case 0x80000701: return "LIBSD";
        default: return "?";
        }
    }

    // "cdrom0:\PSXIRX\SIO2MAN.IRX;1" -> <disc>/PSXIRX/SIO2MAN.IRX
    fs::path resolveDiscPath(const std::string &ps2)
    {
        std::string p = ps2;
        const size_t colon = p.find(':');
        if (colon != std::string::npos)
            p = p.substr(colon + 1);
        const size_t semi = p.rfind(';');
        if (semi != std::string::npos)
            p = p.substr(0, semi);
        std::replace(p.begin(), p.end(), '\\', '/');
        while (!p.empty() && p.front() == '/')
            p.erase(p.begin());
        return g_disc / fs::path(p);
    }

    void stub_sceSifInitRpc(uint8_t *, R5900Context *ctx, PS2Runtime *)
    {
        logf("IOP sceSifInitRpc(mode=%u) [ra=0x%06X]", reg(ctx, 4), reg(ctx, 31));
        ret(ctx, 0);
    }

    void stub_sceSifRebootIop(uint8_t *, R5900Context *ctx, PS2Runtime *)
    {
        const std::string img = cstr(reg(ctx, 4));
        const fs::path host = resolveDiscPath(img);
        const bool ok = fs::is_regular_file(host);
        logf("IOP sceSifRebootIop(\"%s\") host=%s exists=%d -> %d (IOP reset: modules cleared, replacement "
             "image not interpreted)",
             img.c_str(), host.string().c_str(), ok, ok ? 1 : 0);
        g_loadedModules.clear();
        g_servers.clear();
        ret(ctx, ok ? 1 : 0);
    }

    void stub_sceSifSyncIop(uint8_t *, R5900Context *ctx, PS2Runtime *)
    {
        logf("IOP sceSifSyncIop() -> 1");
        ret(ctx, 1);
    }

    void stub_sceSifLoadModule(uint8_t *, R5900Context *ctx, PS2Runtime *)
    {
        const std::string path = cstr(reg(ctx, 4));
        const int32_t argLen = static_cast<int32_t>(reg(ctx, 5));
        const fs::path host = resolveDiscPath(path);
        const bool ok = fs::is_regular_file(host);
        const int32_t id = ok ? g_nextModuleId++ : -203; // -ENOENT as returned by LOADFILE
        if (ok)
            g_loadedModules.insert(host.filename().string());
        if (g_iopAudio && ok && host.filename() == "MJOLNIR.IRX")
        {
            std::vector<uint8_t> args(argLen > 0 ? static_cast<size_t>(argLen) : 0u);
            if (!args.empty())
                std::memcpy(args.data(), mem(reg(ctx, 6)), args.size());
            const ps2x::iop::ModuleLoadResult r =
                g_runtime->loadIopModule(path, args.empty() ? nullptr : args.data(), static_cast<uint32_t>(args.size()));
            logf("IOP sceSifLoadModule(\"%s\", arglen=%d) -> %d: executed in the emulated IOP (IRX id %d, start %d)",
                 path.c_str(), argLen, id, r.moduleId, r.startResult);
            ret(ctx, r.moduleId > 0 ? id : -1);
            return;
        }
        logf("IOP sceSifLoadModule(\"%s\", arglen=%d, args=0x%X) host=%s size=%lld -> %d (module not executed)",
             path.c_str(), argLen, reg(ctx, 6), host.string().c_str(),
             ok ? static_cast<long long>(fs::file_size(host)) : -1LL, id);
        ret(ctx, id);
    }

    // SifRpcClientData: hdr{pkt_addr, rpc_id, sema_id, mode} command buff cbuff end_function end_param server
    constexpr uint32_t kCdCommand = 16, kCdBuff = 20, kCdCbuff = 24, kCdServer = 36;

    void stub_sceSifBindRpc(uint8_t *, R5900Context *ctx, PS2Runtime *)
    {
        const uint32_t cd = reg(ctx, 4), sid = reg(ctx, 5), mode = reg(ctx, 6);
        uint32_t index = 0;
        while (index < g_servers.size() && g_servers[index].sid != sid)
            ++index;
        if (index == g_servers.size())
            g_servers.push_back({sid});
        const uint32_t server = kFakeServerBase + index * 0x40u;
        wr32(cd + 0, 0);
        wr32(cd + 8, 0xFFFFFFFFu);
        wr32(cd + kCdCommand, 0);
        wr32(cd + kCdBuff, server + 0x20u);
        wr32(cd + kCdCbuff, server + 0x30u);
        wr32(cd + kCdServer, server);
        logf("IOP sceSifBindRpc(cd=0x%X, sid=0x%08X %s, mode=%u) -> 0, server=0x%X [ra=0x%06X]", cd, sid, sidName(sid),
             mode, server, reg(ctx, 31));
        ret(ctx, 0);
    }

    // FILEIO 2200 (sid 0x80000001), read-only, backed by the extracted disc. Every request packet starts with
    // {sema, dest, destSize}: the IOP acknowledges the RPC with a nonzero reply word, later sends the result
    // (copied to dest by the EE handler 0x1AF7F0 from the buffer registered by fn 255) and signals sema. The
    // HLE completes the request before the RPC returns, so the library's WaitSema passes at once.
    // Packets: open {.., flags, mode, name}, close {.., fd}, read {.., fd, buf, size}, lseek {.., fd, offset, whence}.
    struct FioFile
    {
        std::string name;
        std::vector<uint8_t> data; // whole file; Maximo's FILEIO use is the FMV files
        uint32_t pos = 0;
    };
    std::map<int32_t, FioFile> g_fioFiles;
    int32_t g_fioNextFd = 1;
    uint64_t g_fioReads = 0, g_fioReadBytes = 0;

    constexpr int32_t kFioENOENT = -2, kFioEBADF = -9, kFioEACCES = -13, kFioENODEV = -19, kFioEINVAL = -22;

    void completeFio(uint32_t send, uint32_t recv, uint32_t rsize, int32_t result)
    {
        if (rsize >= 4)
            wr32(recv, 1); // accepted
        const uint32_t dest = rd32(send + 4), destSize = rd32(send + 8);
        if (dest != 0 && destSize >= 4)
            wr32(dest, static_cast<uint32_t>(result));
        g_runtime->eeScheduler().signalSemaphore(static_cast<int>(rd32(send)), true);
    }

    bool serveFileIo(uint32_t fn, uint32_t send, uint32_t ssize, uint32_t recv, uint32_t rsize, std::string &note)
    {
        char b[512];
        if (fn == 255 && rsize >= 4) // init: the module's version, compared with "2200" at 0x1E6E1C
        {
            std::memcpy(mem(recv), "2200", 4);
            std::snprintf(b, sizeof b, "version \"2200\", result buffer 0x%X", ssize >= 4 ? rd32(send) : 0);
            note = b;
            return true;
        }
        if (ssize < 12)
            return false;
        if (fn == 0 && ssize > 20) // open
        {
            const std::string name = cstr(send + 20);
            const uint32_t flags = rd32(send + 12);
            int32_t result;
            if (name.rfind("cdrom0:", 0) != 0)
                result = kFioENODEV; // only the disc is provided (no rom0:, host:, mc0: through FILEIO)
            else if ((flags & 3u) != 1u)
                result = kFioEACCES; // the disc is read-only
            else
            {
                const fs::path host = resolveDiscPath(name);
                std::ifstream in(host, std::ios::binary);
                if (!in)
                    result = kFioENOENT;
                else
                {
                    FioFile file;
                    file.name = name;
                    file.data.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
                    result = g_fioNextFd++;
                    g_fioFiles[result] = std::move(file);
                }
            }
            std::snprintf(b, sizeof b, "open \"%s\" flags=0x%X -> %d", name.c_str(), flags, result);
            note = b;
            completeFio(send, recv, rsize, result);
            return true;
        }
        const int32_t fd = static_cast<int32_t>(rd32(send + 12));
        const auto it = g_fioFiles.find(fd);
        if (fn == 1 && ssize >= 16) // close
        {
            int32_t result = kFioEBADF;
            if (it != g_fioFiles.end())
            {
                g_fioFiles.erase(it);
                result = 0;
            }
            std::snprintf(b, sizeof b, "close %d -> %d", fd, result);
            note = b;
            completeFio(send, recv, rsize, result);
            return true;
        }
        if (fn == 2 && ssize >= 24) // read
        {
            const uint32_t buf = rd32(send + 16), size = rd32(send + 20);
            int32_t result = kFioEBADF;
            if (it != g_fioFiles.end())
            {
                FioFile &file = it->second;
                const uint32_t n = std::min<uint32_t>(size, static_cast<uint32_t>(file.data.size()) - file.pos);
                for (uint32_t i = 0; i < n; ++i) // byte-wise: buf need not be aligned and may wrap the mask
                    *mem(buf + i) = file.data[file.pos + i];
                file.pos += n;
                result = static_cast<int32_t>(n);
                ++g_fioReads;
                g_fioReadBytes += n;
            }
            std::snprintf(b, sizeof b, "read %d buf=0x%X size=%u -> %d", fd, buf, size, result);
            note = b;
            completeFio(send, recv, rsize, result);
            return true;
        }
        if (fn == 4 && ssize >= 24) // lseek
        {
            const int32_t offset = static_cast<int32_t>(rd32(send + 16));
            const uint32_t whence = rd32(send + 20);
            int32_t result = kFioEBADF;
            if (it != g_fioFiles.end())
            {
                FioFile &file = it->second;
                const int64_t base = whence == 0 ? 0 : whence == 1 ? file.pos : static_cast<int64_t>(file.data.size());
                const int64_t target = base + offset;
                if (whence > 2 || target < 0 || target > static_cast<int64_t>(file.data.size()))
                    result = kFioEINVAL;
                else
                {
                    file.pos = static_cast<uint32_t>(target);
                    result = static_cast<int32_t>(target);
                }
            }
            std::snprintf(b, sizeof b, "lseek %d offset=%d whence=%u -> %d", fd, offset, whence, result);
            note = b;
            completeFio(send, recv, rsize, result);
            return true;
        }
        return false;
    }

    // ---- movie-audio drain (LIBSD, SID 0x80000701), for runs without the emulated sound system ---------------
    // For the FMV path only. This is not a sound implementation: with
    // MAXIMO_AUDIO the calls go to the emulated IOP instead (serveSdRemote below) and the soundtrack plays.
    // The movie player (0x191960.., 0x192130..) streams the PSS PCM into an IOP ring started with
    // sceSdBlockTrans(core 0, WRITE_FROM|LOOP, 0x196040, 0xC000, start) and refills it as far as
    // sceSdBlockTransStatus says SPU2 has read (reply word 0 = bank << 24 | IOP read address; the player
    // keeps the low 24 bits). With no reply the read address never moves, the ring stays full, and playback
    // stops after ~1 s. The drain models only that read cursor: it advances at the SPU2 output rate on the
    // emulated EE clock and discards the samples. OPENING.PSS's SShd header says 48000 Hz, 2 channels,
    // 16-bit PCM, so 192000 bytes/s. Every other LIBSD command stays unhandled. Delete this block when a
    // real SPU2/LIBSD path exists.
    constexpr uint32_t kSdBlockTrans = 0x80E0, kSdBlockTransStatus = 0x8100;
    constexpr uint32_t kSdTransStop = 2, kSdTransWriteFrom = 3, kSdBlockLoop = 0x10;
    constexpr uint64_t kSpu2BytesPerSecond = 48000ull * 2 * 2;

    struct MovieAudioDrain
    {
        bool active = false;
        uint32_t base = 0, size = 0, startOffset = 0;
        uint64_t startCycle = 0;
        uint64_t statusCalls = 0, transfers = 0;
    };
    MovieAudioDrain g_movieAudio[2]; // per SPU2 core
    uint64_t g_movieAudioDrainedBytes = 0;

    uint32_t movieAudioOffset(const MovieAudioDrain &d)
    {
        const uint64_t cycles = g_runtime->eeScheduler().currentCycle() - d.startCycle;
        const uint64_t bytes = cycles * kSpu2BytesPerSecond / EeScheduler::kEeClockHz & ~3ull; // whole L/R frames
        return static_cast<uint32_t>((d.startOffset + bytes) % d.size);
    }

    uint32_t movieAudioStatus(const MovieAudioDrain &d)
    {
        const uint32_t offset = movieAudioOffset(d);
        return (offset >= d.size / 2 ? 1u << 24 : 0u) | ((d.base + offset) & 0xFFFFFFu);
    }

    // The whole reply is the result in word 0, the rest zero (recv may alias send, so write it last).
    void movieAudioReply(uint32_t recv, uint32_t rsize, uint32_t result)
    {
        std::memset(mem(recv), 0, std::min<uint32_t>(rsize, 16));
        wr32(recv, result);
    }

    bool serveMovieAudio(uint32_t fn, uint32_t send, uint32_t ssize, uint32_t recv, uint32_t rsize, std::string &note)
    {
        // Packet built by sceSdRemote (0x1C22B8): {self, core, mode, addr, size, start}; the result is recv word 0.
        if (ssize < 12 || rsize < 4)
            return false;
        const uint32_t core = rd32(send + 4);
        if (core > 1)
            return false;
        MovieAudioDrain &d = g_movieAudio[core];
        char b[160];
        if (fn == kSdBlockTransStatus)
        {
            if (!d.active)
                return false;
            const uint32_t status = movieAudioStatus(d);
            movieAudioReply(recv, rsize, status);
            const uint64_t n = ++d.statusCalls;
            if ((n & (n - 1)) == 0) // the player polls continuously; log powers of two only
            {
                std::snprintf(b, sizeof b, "movie-audio drain status #%llu -> 0x%08X (EE cycle %llu, VBlank %llu)",
                              static_cast<unsigned long long>(n), status,
                              static_cast<unsigned long long>(g_runtime->eeScheduler().currentCycle()),
                              static_cast<unsigned long long>(g_runtime->eeScheduler().currentVSyncTick()));
                note = b;
            }
            return true;
        }
        if (fn != kSdBlockTrans || ssize < 24)
            return false;
        const uint32_t mode = rd32(send + 8);
        if ((mode & 3) == kSdTransStop)
        {
            if (!d.active)
                return false;
            const uint32_t status = movieAudioStatus(d);
            g_movieAudioDrainedBytes += (g_runtime->eeScheduler().currentCycle() - d.startCycle) * kSpu2BytesPerSecond /
                                        EeScheduler::kEeClockHz;
            d.active = false;
            movieAudioReply(recv, rsize, status);
            std::snprintf(b, sizeof b, "movie-audio drain stop core %u -> 0x%08X", core, status);
            note = b;
            return true;
        }
        const uint32_t addr = rd32(send + 12), size = rd32(send + 16), start = rd32(send + 20);
        // Only the looping write the movie player uses; one-shot and read transfers remain unhandled.
        if ((mode & 3) != kSdTransWriteFrom && (mode & 3) != 0)
            return false;
        if (!(mode & kSdBlockLoop) || size == 0 || (size & 3) != 0)
            return false;
        d.active = true;
        d.base = addr;
        d.size = size;
        d.startOffset = (mode & 3) == kSdTransWriteFrom && start - addr < size ? (start - addr) & ~3u : 0;
        d.startCycle = g_runtime->eeScheduler().currentCycle();
        ++d.transfers;
        movieAudioReply(recv, rsize, 0);
        std::snprintf(b, sizeof b, "movie-audio drain start core %u ring 0x%X+0x%X from +0x%X at %llu B/s (silent)", core,
                      addr, size, d.startOffset, static_cast<unsigned long long>(kSpu2BytesPerSecond));
        note = b;
        return true;
    }

    // With the emulated sound system (MAXIMO_AUDIO), the EE's LIBSD calls run on the emulated IOP's libsd and SPU2,
    // as LIBSD's RPC server runs them on a PS2. The movie player initialises the SPU2, sets the master and input
    // volumes and streams the soundtrack through core 0's sound data input from its IOP ring; the ring's read
    // position (sceSdBlockTransStatus) follows the SPU2 as it mixes, on the audio clock. Packet built by
    // sceSdRemote (0x1C22B8): {self, arg0..arg4}; the result is recv word 0.
    std::map<uint32_t, uint64_t> g_sdRemoteCalls;

    bool serveSdRemote(uint32_t fn, uint32_t send, uint32_t ssize, uint32_t recv, uint32_t rsize, std::string &note)
    {
        if (ssize < 8 || rsize < 4)
            return false;
        std::array<uint32_t, 5> args{};
        for (uint32_t i = 0; i < args.size() && 8 + i * 4 <= ssize; ++i)
            args[i] = rd32(send + 4 + i * 4);
        uint32_t result = 0;
        if (!g_runtime->iopSdRemote(fn, args, result))
            return false;
        movieAudioReply(recv, rsize, result);
        const uint64_t n = ++g_sdRemoteCalls[fn];
        // The player polls the status continuously, and a skipped movie fades MVOL with a busy loop of SetParam calls
        // (~130,000 a second): past the first 16 calls of a function, log only power-of-two call numbers.
        if (n <= 16 || (n & (n - 1)) == 0)
        {
            char b[200];
            std::snprintf(b, sizeof b, "libsd 0x%X(0x%X, 0x%X, 0x%X, 0x%X, 0x%X) -> 0x%X [call #%llu] (VBlank %llu)", fn,
                          args[0], args[1], args[2], args[3], args[4], result, static_cast<unsigned long long>(n),
                          static_cast<unsigned long long>(g_runtime->eeScheduler().currentVSyncTick()));
            note = b;
        }
        return true;
    }

    // Native IOP services. Returns false if the call is not understood (reply left zeroed).
    bool serveRpc(uint32_t sid, uint32_t fn, uint32_t send, uint32_t ssize, uint32_t recv, uint32_t rsize, std::string &note)
    {
        // The IOP heap services allocate from the emulated IOP's own heap, which its modules (MJOLNIR.IRX) share,
        // so blocks never overlap and freed ones are used again. 0 means the heap is full, as on the IOP.
        if (sid == 0x80000003 && fn == 1 && ssize >= 4 && rsize >= 4) // sceSifAllocIopHeap(size)
        {
            const uint32_t addr = g_runtime->allocateIopMemory(rd32(send), 64u);
            wr32(recv, addr);
            char b[80];
            std::snprintf(b, sizeof b, addr ? "alloc %u bytes at IOP 0x%X" : "alloc %u bytes FAILED (IOP heap full)",
                          rd32(send), addr);
            note = b;
            return true;
        }
        if (sid == 0x80000003 && fn == 2 && ssize >= 4 && rsize >= 4) // sceSifFreeIopHeap(addr)
        {
            const bool freed = g_runtime->freeIopMemory(rd32(send));
            wr32(recv, freed ? 0u : 0xFFFFFFFFu);
            note = freed ? "free" : "free FAILED (not an allocation)";
            return true;
        }
        // FILEIO init (sceFsInit): the IOP module answers with its 4-character version, which the EE library
        // compares with the versions it supports ("2200" at 0x1E6E1C, matching the disc's IOPRP22.IMG).
        if (sid == 0x80000001 && fn == 255 && rsize >= 4)
        {
            std::memcpy(mem(recv), "2200", 4);
            char b[64];
            std::snprintf(b, sizeof b, "version \"2200\", result buffer 0x%X", ssize >= 4 ? rd32(send) : 0);
            note = b;
            return true;
        }
        if (sid == 0x80000001)
            return serveFileIo(fn, send, ssize, recv, rsize, note);
        if (sid == 0x80000701)
            return g_iopAudio ? serveSdRemote(fn, send, ssize, recv, rsize, note)
                              : serveMovieAudio(fn, send, ssize, recv, rsize, note);
        return false;
    }

    // Sound driver (SDRDRV/MJOLNIR, SID 0x12345) command packets, built by 0x18D220: halfword 0 is the
    // command count, then per command {id, nargs, args...}. Decoded for the log only; the driver
    // itself is not emulated, so no reply is fabricated (see BOOT_FINDINGS.md).
    struct SoundCmdStat
    {
        uint64_t count = 0;
        uint32_t nargs = 0;
    };
    std::map<uint32_t, SoundCmdStat> g_soundCmds;
    uint64_t g_soundPackets = 0, g_soundMalformed = 0;

    uint16_t rd16(uint32_t addr)
    {
        uint16_t v;
        std::memcpy(&v, mem(addr), 2);
        return v;
    }

    void decodeSoundPacket(uint32_t send, uint32_t size)
    {
        ++g_soundPackets;
        const uint32_t halfwords = size / 2;
        const uint32_t count = rd16(send);
        uint32_t i = 1;
        for (uint32_t c = 0; c < count; ++c)
        {
            if (i + 2 > halfwords)
            {
                ++g_soundMalformed;
                return;
            }
            const uint32_t id = rd16(send + i * 2), n = rd16(send + i * 2 + 2);
            if (i + 2 + n > halfwords)
            {
                ++g_soundMalformed;
                return;
            }
            SoundCmdStat &st = g_soundCmds[id];
            // MAXIMO_SOUND_TRACE=1: every command except the per-frame 2 and 9 (MAXIMO_SOUND_TRACE=2: all).
            static const int soundTrace = []
            {
                const char *value = std::getenv("MAXIMO_SOUND_TRACE");
                return value ? std::atoi(value) : 0;
            }();
            // Music start (1), pause/resume (10/11) and volume (31) are rare and always logged, for bug reports.
            const bool always = id == 1u || id == 10u || id == 11u || id == 31u;
            if (st.count++ < 3 || always || soundTrace >= 2 || (soundTrace == 1 && id != 2u && id != 9u))
            {
                char args[160] = {};
                int len = 0;
                for (uint32_t a = 0; a < n && a < 12; ++a)
                    len += std::snprintf(args + len, sizeof(args) - len, " %04X", rd16(send + (i + 2 + a) * 2));
                logf("SOUND cmd %u nargs=%u args:%s (packet #%llu)", id, n, args, static_cast<unsigned long long>(g_soundPackets));
            }
            st.nargs = n;
            i += 2 + n;
        }
    }

    void stub_sceSifCallRpc(uint8_t *rdram, R5900Context *ctx, PS2Runtime *)
    {
        const uint32_t cd = reg(ctx, 4), fn = reg(ctx, 5), mode = reg(ctx, 6), send = reg(ctx, 7);
        const uint32_t ssize = reg(ctx, 8), recv = reg(ctx, 9), rsize = reg(ctx, 10), endFn = reg(ctx, 11);
        const uint32_t endParam = rd32(reg(ctx, 29));
        const uint32_t server = rd32(cd + kCdServer);
        const uint32_t index = (server - kFakeServerBase) / 0x40u;
        const bool bound = server >= kFakeServerBase && index < g_servers.size();
        const uint32_t sid = bound ? g_servers[index].sid : 0;
        if (bound)
            ++g_servers[index].calls;

        // The sound driver runs in the emulated IOP: its own RPC server takes the packet and writes the reply.
        if (g_iopAudio && sid == kSoundDriverSid && g_runtime->canBindIopRpc(sid))
        {
            if (fn == 0)
                decodeSoundPacket(send, ssize);
            ps2x::iop::RpcRequest request{};
            request.clientAddress = cd;
            request.sid = sid;
            request.function = fn;
            request.mode = mode;
            request.send = {send, ssize};
            request.receive = {recv, rsize};
            request.endFunction = endFn;
            request.endParameter = endParam;
            const ps2x::iop::RpcResult result = g_runtime->handleIopRpc(rdram, ctx, request);
            const uint64_t n = ++g_soundRpcCalls;
            if (n <= 16 || (n & (n - 1)) == 0)
                logf("IOP sceSifCallRpc(sid=0x%08X MJOLNIR, fn=%u, mode=%u, send=0x%X/%u, recv=0x%X/%u, end=0x%X) -> "
                     "served by the emulated IOP (handled=%d) call #%llu [ra=0x%06X]",
                     sid, fn, mode, send, ssize, recv, rsize, endFn, result.handled ? 1 : 0,
                     static_cast<unsigned long long>(n), reg(ctx, 31));
            ret(ctx, 0);
            // The reply is in place, so the completion callback runs now, as the SIF interrupt would run it when the
            // reply DMA ends. The game unpacks each voice's state there (0x18D2E0); without it, it only unpacks the
            // previous reply at the start of its next frame. A sound it has just keyed on then reads as off twice,
            // one more than it tolerates, so it frees the effect slot without keying the voice off: looping
            // effects (the attract demo's bone, effect 0x75) pile up on the voices and the channels run out.
            if (result.handled && endFn != 0 && g_runtime->hasFunction(endFn))
            {
                GuestInvocation callback{};
                callback.kind = GuestInvocationKind::RpcCallback;
                callback.context = *ctx;
                callback.context.pc = endFn;
                SET_GPR_U32(&callback.context, 4, endParam);
                SET_GPR_U32(&callback.context, 29, g_runtime->eeScheduler().invocationStackTop());
                SET_GPR_U32(&callback.context, 31, 0u);
                callback.onComplete = [](const R5900Context &, R5900Context &) {};
                g_runtime->eeScheduler().invokeCurrent(std::move(callback));
            }
            return;
        }
        if (g_iopAudio && sid == kSoundDriverSid)
            logf("IOP sceSifCallRpc(sid=0x%08X): the emulated sound driver has not registered its server yet", sid);

        // The first send words, for the log; formatted only for calls that are logged (pollers make millions).
        uint32_t sent[6] = {};
        const uint32_t sentWords = std::min<uint32_t>(ssize / 4, 6);
        for (uint32_t i = 0; i < sentWords; ++i)
            sent[i] = rd32(send + i * 4);

        if (sid == 0x00012345u && fn == 0)
            decodeSoundPacket(send, ssize);
        std::string note;
        // Unhandled calls get a zeroed reply. LIBSD's sceSdRemote receives into its own send buffer, so the
        // packet must be served before anything is cleared; zero up front only when the buffers are disjoint.
        const bool zeroable = recv != 0 && rsize != 0 && rsize < 0x100000u;
        const bool overlap = recv < send + ssize && send < recv + rsize;
        if (zeroable && !overlap)
            std::memset(mem(recv), 0, rsize);
        const bool handled = bound && serveRpc(sid, fn, send, ssize, recv, rsize, note);
        if (zeroable && overlap && !handled)
            std::memset(mem(recv), 0, rsize);
        if (!handled)
            ++g_unhandledRpc[(static_cast<uint64_t>(sid) << 16) | (fn & 0xFFFF)];
        const uint64_t n = bound ? g_servers[index].calls : 0;
        // Handled calls log when they leave a note (high-rate pollers such as the movie-audio drain stay quiet).
        const bool logCall = (handled && !note.empty()) || n <= 16 || (n & (n - 1)) == 0;
        if (logCall)
        {
            char words[96] = {};
            int len = 0;
            for (uint32_t i = 0; i < sentWords; ++i)
                len += std::snprintf(words + len, sizeof(words) - len, " %08X", sent[i]);
            logf("IOP sceSifCallRpc(sid=0x%08X %s, fn=%u, mode=%u, send=0x%X/%u:%s, recv=0x%X/%u, end=0x%X(0x%X)) -> 0 "
                 "%s%s [ra=0x%06X]",
                 sid, bound ? sidName(sid) : "UNBOUND", fn, mode, send, ssize, words, recv, rsize, endFn, endParam,
                 handled ? "served: " : "UNHANDLED (reply zeroed)", note.c_str(), reg(ctx, 31));
        }
        if (endFn != 0 && logCall)
            logf("IOP   note: completion callback 0x%X not invoked (no reply to deliver)", endFn);
        ret(ctx, 0);
    }

    void stub_sceSifCheckStatRpc(uint8_t *, R5900Context *ctx, PS2Runtime *)
    {
        ret(ctx, 0); // every stubbed RPC completes synchronously
    }

    // ---- libc ----------------------------------------------------------------------------------

    // Minimal printf for the EE ABI: integer varargs in a1..a3, t0..t3, then the caller's stack.
    std::string formatGuest(R5900Context *ctx, const std::string &fmt, int firstArg)
    {
        static const int argRegs[] = {4, 5, 6, 7, 8, 9, 10, 11};
        int argi = firstArg;
        auto next32 = [&]() -> uint32_t
        {
            const int i = argi++;
            return i < 8 ? reg(ctx, argRegs[i]) : rd32(reg(ctx, 29) + (i - 8) * 8);
        };
        std::string out;
        for (size_t i = 0; i < fmt.size(); ++i)
        {
            if (fmt[i] != '%')
            {
                out += fmt[i];
                continue;
            }
            std::string spec = "%";
            ++i;
            while (i < fmt.size() && std::strchr("-+ #0123456789.lhzjt", fmt[i]))
                spec += fmt[i++];
            if (i >= fmt.size())
                break;
            const char c = fmt[i];
            spec.erase(std::remove_if(spec.begin() + 1, spec.end(), [](char ch) { return std::strchr("lhzjt", ch); }),
                       spec.end());
            char buf[512];
            switch (c)
            {
            case 'd': case 'i':
                std::snprintf(buf, sizeof buf, (spec + "d").c_str(), static_cast<int32_t>(next32()));
                out += buf;
                break;
            case 'u': case 'x': case 'X': case 'o': case 'c':
                std::snprintf(buf, sizeof buf, (spec + c).c_str(), next32());
                out += buf;
                break;
            case 'p':
                std::snprintf(buf, sizeof buf, "0x%08X", next32());
                out += buf;
                break;
            case 's':
                std::snprintf(buf, sizeof buf, (spec + "s").c_str(), cstr(next32()).c_str());
                out += buf;
                break;
            case 'f': case 'e': case 'g':
                out += "<float>";
                next32();
                break;
            case '%':
                out += '%';
                break;
            default:
                out += spec + c;
            }
        }
        return out;
    }

    void stub_printf(uint8_t *, R5900Context *ctx, PS2Runtime *)
    {
        const std::string s = formatGuest(ctx, cstr(reg(ctx, 4), 1024), 1);
        logf("GAME printf: %s  [ra=0x%06X]", printable(s).c_str(), reg(ctx, 31));
        ret(ctx, static_cast<int32_t>(s.size()));
    }

    void stub_assert(uint8_t *, R5900Context *ctx, PS2Runtime *runtime)
    {
        logf("GAME ASSERTION FAILED: \"%s\" file \"%s\" line %d [ra=0x%06X] -- stopping",
             cstr(reg(ctx, 6)).c_str(), cstr(reg(ctx, 4)).c_str(), GPR_S32(ctx, 5), reg(ctx, 31));
        runtime->requestStop();
        ctx->pc = 0;
    }

    // ---- crash reporting -----------------------------------------------------------------------

    void dumpRing(const char *why)
    {
        logf("---- %s: last %zu guest function entries (oldest first) ----", why, g_ring.size());
        std::string line;
        for (size_t i = 0; i < g_ring.size(); ++i)
        {
            const uint32_t pc = g_ring[(g_ringPos + i) % g_ring.size()];
            if (pc == 0)
                continue;
            char b[16];
            std::snprintf(b, sizeof b, " %06X", pc);
            line += b;
            if (line.size() > 100)
            {
                logf("  %s", line.c_str());
                line.clear();
            }
        }
        if (!line.empty())
            logf("  %s", line.c_str());
        logf("  last game function entered: 0x%06X", g_lastGameFn);
        logf("  heap: %llu allocations, %llu failed, %llu past the end of the pool, high-water 0x%07X, pool end "
             "0x%07X (%lld KB left)",
             static_cast<unsigned long long>(g_heapAllocs), static_cast<unsigned long long>(g_heapFails),
             static_cast<unsigned long long>(g_heapOverruns), g_heapHigh, g_heapEnd,
             static_cast<long long>(heapLeftKb()));
        for (const auto &[addr, w] : g_watches)
            if (w.calls != 0)
                logf("  WATCH total 0x%06X %-60.60s %llu calls", addr, w.what,
                     static_cast<unsigned long long>(w.calls));
        logf("  frame waits ended by the 2-VBlank timeout path: %llu",
             static_cast<unsigned long long>(g_frameTimeouts));
        if (g_perfTotal.frames != 0)
            logFrameStats("whole run", g_perfTotal,
                          std::chrono::duration<double>(std::chrono::steady_clock::now() - g_start).count());
        // MAXIMO_DUMP_CLUT=cbp0,cbp1,i0,i1: log CT32 CSM1 palette entries i0 and i1 for every CLUT in [cbp0, cbp1).
        if (const char *dump = std::getenv("MAXIMO_DUMP_CLUT"))
        {
            unsigned cbp0 = 0, cbp1 = 0, i0 = 0, i1 = 0;
            if (std::sscanf(dump, "%x,%x,%x,%x", &cbp0, &cbp1, &i0, &i1) == 4)
                for (unsigned cbp = cbp0; cbp < cbp1; cbp += 4)
                {
                    auto entry = [&](unsigned i)
                    {
                        const unsigned p = (i & ~0x18u) | ((i & 0x08u) << 1) | ((i & 0x10u) >> 1);
                        return g_runtime->gs().ReadVram(0u, cbp, 1u, p & 15u, p >> 4);
                    };
                    logf("  CLUT cbp=0x%X [0x%02X]=0x%08X [0x%02X]=0x%08X", cbp, i0, entry(i0), i1, entry(i1));
                }
        }
        // MAXIMO_DUMP_T8=tbp,tbw,width,height: write GS memory read as PSMT8 indices to gs_t8.pgm.
        if (const char *dump = std::getenv("MAXIMO_DUMP_T8"))
        {
            unsigned tbp = 0, tbw = 0, width = 0, height = 0;
            if (std::sscanf(dump, "%x,%u,%u,%u", &tbp, &tbw, &width, &height) == 4 && width && height)
                if (FILE *f = std::fopen("gs_t8.pgm", "wb"))
                {
                    std::fprintf(f, "P5\n%u %u\n255\n", width, height);
                    std::vector<uint8_t> row(width);
                    for (unsigned y = 0; y < height; ++y)
                    {
                        for (unsigned x = 0; x < width; ++x)
                            row[x] = static_cast<uint8_t>(g_runtime->gs().ReadVram(0x13u, tbp, tbw, x, y));
                        std::fwrite(row.data(), 1, row.size(), f);
                    }
                    std::fclose(f);
                    logf("  wrote gs_t8.pgm: PSMT8 tbp=0x%X tbw=%u %ux%u", tbp, tbw, width, height);
                }
        }
        const GSRegisters &gsr = g_runtime->memory().gs();
        logf("  GS display: PMODE=0x%llX SMODE2=0x%llX DISPFB1=0x%llX DISPLAY1=0x%llX DISPFB2=0x%llX "
             "DISPLAY2=0x%llX BGCOLOR=0x%llX",
             static_cast<unsigned long long>(gsr.pmode), static_cast<unsigned long long>(gsr.smode2),
             static_cast<unsigned long long>(gsr.dispfb1), static_cast<unsigned long long>(gsr.display1),
             static_cast<unsigned long long>(gsr.dispfb2), static_cast<unsigned long long>(gsr.display2),
             static_cast<unsigned long long>(gsr.bgcolor));
        std::fflush(g_log); // (a crash report goes to the disk at once)
    }

    LONG WINAPI crashFilter(EXCEPTION_POINTERS *info)
    {
        const R5900Context &c = g_runtime->cpu();
        logf("HOST CRASH: exception 0x%08lX at host address %p (guest pc register 0x%06X, ra 0x%06X)",
             info->ExceptionRecord->ExceptionCode, info->ExceptionRecord->ExceptionAddress, c.pc,
             static_cast<uint32_t>(_mm_cvtsi128_si32(c.r[31])));
        if (info->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION)
            logf("  access violation: %s host address 0x%llX", info->ExceptionRecord->ExceptionInformation[0] ? "write" : "read",
                 static_cast<unsigned long long>(info->ExceptionRecord->ExceptionInformation[1]));
        dumpRing("crash");
        std::fflush(g_log);
        return EXCEPTION_EXECUTE_HANDLER;
    }

    bool isMilestone(uint32_t addr)
    {
        return std::any_of(g_milestones.begin(), g_milestones.end(), [&](const Milestone &m) { return m.addr == addr; });
    }

    // A milestone keeps its wrapper in the table; the replacement goes behind it.
    void bindStub(PS2Runtime &rt, uint32_t addr, Fn fn)
    {
        if (isMilestone(addr))
            g_original[slotOf(addr)] = fn;
        else
            rt.replaceFunction(addr, fn);
    }

    void bindRuntime(PS2Runtime &rt, uint32_t addr, const char *name, Fn impl, int nargs)
    {
        g_bound[addr] = {name, impl, nargs};
        bindStub(rt, addr, boundEntry);
    }
}

// ---- sampling profiler (MAXIMO_PROFILE=<start seconds>[,<end seconds>] or f<first frame>,<last frame>) ----
// Suspends the game thread about once a millisecond, records its instruction pointer and up to 12 callers
// (x64 unwind tables), and names them with the PDB at exit: exclusive (self) and inclusive counts per function.
namespace
{
    constexpr int kProfileFrames = 12;
    std::mutex g_profileMutex;
    std::vector<std::array<DWORD64, kProfileFrames>> g_profileSamples;
    std::vector<bool> g_profileInWait; // sample taken between sceGsSyncV entry and the VBlank that releases it
    std::atomic<bool> g_profileStop{false};

    int unwindStack(CONTEXT context, std::array<DWORD64, kProfileFrames> &frames)
    {
        int n = 0;
        while (n < kProfileFrames && context.Rip != 0)
        {
            frames[n++] = context.Rip;
            DWORD64 imageBase = 0;
            PRUNTIME_FUNCTION function = RtlLookupFunctionEntry(context.Rip, &imageBase, nullptr);
            if (!function)
            {
                // Leaf function without unwind data: the return address is at [rsp].
                context.Rip = *reinterpret_cast<DWORD64 *>(context.Rsp);
                context.Rsp += 8;
                continue;
            }
            PVOID handlerData = nullptr;
            DWORD64 establisherFrame = 0;
            RtlVirtualUnwind(UNW_FLAG_NHANDLER, imageBase, context.Rip, function, &context, &handlerData,
                             &establisherFrame, nullptr);
        }
        return n;
    }

    void profileThread(double startSeconds, double endSeconds, bool guestFrames)
    {
        while (!g_profileStop.load())
        {
            const double t = guestFrames ? static_cast<double>(g_guestFrame.load(std::memory_order_relaxed))
                                         : std::chrono::duration<double>(std::chrono::steady_clock::now() - g_start).count();
            HANDLE thread = g_gameThread.load();
            if (thread && t >= startSeconds && t < endSeconds && SuspendThread(thread) != static_cast<DWORD>(-1))
            {
                CONTEXT context{};
                context.ContextFlags = CONTEXT_FULL;
                std::array<DWORD64, kProfileFrames> frames{};
                if (GetThreadContext(thread, &context))
                    unwindStack(context, frames); // the stack is frozen while suspended
                ResumeThread(thread);
                const bool inWait = ps2_host_profile::g_waitMarkNs.load(std::memory_order_acquire) >
                                    ps2_host_profile::g_waitReleaseNs.load(std::memory_order_acquire);
                std::lock_guard<std::mutex> lock(g_profileMutex);
                g_profileSamples.push_back(frames);
                g_profileInWait.push_back(inWait);
            }
            Sleep(1);
        }
    }

    // PERFORMANCE PROFILE (debug menu): 10 seconds of samples of the game thread and of the VU1 / GS worker thread,
    // starting 2 seconds after it is chosen (time to close the menu), then a report in the log: for each thread the
    // functions it was in (self), the functions on its stack (inclusive) and the source lines (the generated VU1
    // code is one function per image, so the lines say which part). Named with the PDB next to the build.
    void menuProfileRun()
    {
        using Frames = std::array<DWORD64, kProfileFrames>;
        std::vector<Frames> samples[2];
        Sleep(2000);
        const auto end = std::chrono::steady_clock::now() + std::chrono::seconds(10);
        while (std::chrono::steady_clock::now() < end && !g_profileStop.load())
        {
            HANDLE threads[2] = {g_gameThread.load(), g_vu1WorkerHandle.load()};
            if (threads[1] == threads[0])
                threads[1] = nullptr;
            for (int t = 0; t < 2; ++t)
            {
                HANDLE thread = threads[t];
                if (!thread || SuspendThread(thread) == static_cast<DWORD>(-1))
                    continue;
                CONTEXT context{};
                context.ContextFlags = CONTEXT_FULL;
                Frames frames{};
                if (GetThreadContext(thread, &context))
                    unwindStack(context, frames);
                ResumeThread(thread);
                samples[t].push_back(frames);
            }
            Sleep(1);
        }
        g_menuProfileState.store(2);
        {
            std::lock_guard<std::mutex> lock(g_profileMutex);
            HANDLE process = GetCurrentProcess();
            SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_LOAD_LINES);
            SymInitialize(process, nullptr, TRUE);
            std::map<DWORD64, std::string> names;
            auto nameOf = [&](DWORD64 address) -> const std::string &
            {
                auto it = names.find(address);
                if (it != names.end())
                    return it->second;
                alignas(SYMBOL_INFO) char buffer[sizeof(SYMBOL_INFO) + 512] = {};
                auto *symbol = reinterpret_cast<SYMBOL_INFO *>(buffer);
                symbol->SizeOfStruct = sizeof(SYMBOL_INFO);
                symbol->MaxNameLen = 511;
                DWORD64 displacement = 0;
                char fallback[32];
                std::snprintf(fallback, sizeof fallback, "0x%llX", static_cast<unsigned long long>(address));
                return names[address] = SymFromAddr(process, address, &displacement, symbol) ? symbol->Name : fallback;
            };
            static const char *kThreadNames[2] = {"game thread", "VU1 / GS worker thread"};
            for (int t = 0; t < 2; ++t)
            {
                const std::vector<Frames> &list = samples[t];
                if (list.empty())
                {
                    logf("---- menu profile: %s: no samples ----", kThreadNames[t]);
                    continue;
                }
                std::map<std::string, uint32_t> self, inclusive, lines;
                for (const Frames &frames : list)
                {
                    if (!frames[0])
                        continue;
                    ++self[nameOf(frames[0])];
                    std::set<std::string> seen;
                    for (DWORD64 address : frames)
                        if (address && seen.insert(nameOf(address)).second)
                            ++inclusive[nameOf(address)];
                    IMAGEHLP_LINE64 line{};
                    line.SizeOfStruct = sizeof line;
                    DWORD displacement = 0;
                    char key[300];
                    if (SymGetLineFromAddr64(process, frames[0], &displacement, &line))
                    {
                        const char *file = std::strrchr(line.FileName, '\\');
                        std::snprintf(key, sizeof key, "%s:%lu", file ? file + 1 : line.FileName, line.LineNumber);
                    }
                    else
                        std::snprintf(key, sizeof key, "%s", nameOf(frames[0]).c_str());
                    ++lines[key];
                }
                auto print = [&](const char *title, const std::map<std::string, uint32_t> &counts, size_t most)
                {
                    std::vector<std::pair<uint32_t, std::string>> sorted;
                    for (const auto &[name, count] : counts)
                        sorted.push_back({count, name});
                    std::sort(sorted.rbegin(), sorted.rend());
                    logf("---- menu profile: %s: %s (%zu samples) ----", kThreadNames[t], title, list.size());
                    for (size_t i = 0; i < sorted.size() && i < most; ++i)
                        logf("  %5.1f%%  %6u  %.300s", 100.0 * sorted[i].first / list.size(), sorted[i].first,
                             sorted[i].second.c_str());
                };
                print("self", self, 40);
                print("inclusive", inclusive, 40);
                print("self by line", lines, 60);
            }
            SymCleanup(process);
        }
        logf("---- menu profile: done ----");
        g_menuProfileState.store(0);
    }
    void startMenuProfile()
    {
        int idle = 0;
        if (!g_menuProfileState.compare_exchange_strong(idle, 1))
            return;
        logf("debug: performance profile: sampling starts in 2 seconds, for 10 seconds");
        std::thread(menuProfileRun).detach();
    }

    void reportProfile()
    {
        std::lock_guard<std::mutex> lock(g_profileMutex);
        if (g_profileSamples.empty())
            return;
        HANDLE process = GetCurrentProcess();
        SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS);
        SymInitialize(process, nullptr, TRUE);
        std::map<DWORD64, std::string> names;
        auto nameOf = [&](DWORD64 address) -> const std::string &
        {
            auto it = names.find(address);
            if (it != names.end())
                return it->second;
            alignas(SYMBOL_INFO) char buffer[sizeof(SYMBOL_INFO) + 512] = {};
            auto *symbol = reinterpret_cast<SYMBOL_INFO *>(buffer);
            symbol->SizeOfStruct = sizeof(SYMBOL_INFO);
            symbol->MaxNameLen = 511;
            DWORD64 displacement = 0;
            char fallback[32];
            std::snprintf(fallback, sizeof fallback, "0x%llX", static_cast<unsigned long long>(address));
            return names[address] = SymFromAddr(process, address, &displacement, symbol) ? symbol->Name : fallback;
        };
        std::map<std::string, uint32_t> self, inclusive;
        for (const auto &frames : g_profileSamples)
        {
            if (!frames[0])
                continue;
            ++self[nameOf(frames[0])];
            std::set<std::string> seen;
            for (DWORD64 address : frames)
                if (address && seen.insert(nameOf(address)).second)
                    ++inclusive[nameOf(address)];
        }
        auto print = [&](const char *title, const std::map<std::string, uint32_t> &counts)
        {
            std::vector<std::pair<uint32_t, std::string>> sorted;
            for (const auto &[name, count] : counts)
                sorted.push_back({count, name});
            std::sort(sorted.rbegin(), sorted.rend());
            logf("---- profile: %s (%zu samples) ----", title, g_profileSamples.size());
            for (size_t i = 0; i < sorted.size() && i < 45; ++i)
                logf("  %5.1f%%  %6u  %.400s", 100.0 * sorted[i].first / g_profileSamples.size(), sorted[i].first,
                     sorted[i].second.c_str());
        };
        print("self", self);
        print("inclusive", inclusive);
        // MAXIMO_PROFILE_CALLERS=a,b,...: for samples whose innermost function contains one of these names, the
        // chain of the next callers outside it (MAXIMO_PROFILE_CALLER_DEPTH of them, default 3).
        if (const char *patterns = std::getenv("MAXIMO_PROFILE_CALLERS"))
        {
            const char *depthText = std::getenv("MAXIMO_PROFILE_CALLER_DEPTH");
            const int maxDepth = depthText ? std::clamp(std::atoi(depthText), 1, kProfileFrames - 1) : 3;
            std::string list = patterns;
            for (size_t start = 0; start < list.size();)
            {
                size_t end = list.find(',', start);
                if (end == std::string::npos)
                    end = list.size();
                const std::string pattern = list.substr(start, end - start);
                start = end + 1;
                std::map<std::string, uint32_t> callers;
                for (const auto &frames : g_profileSamples)
                {
                    if (!frames[0] || nameOf(frames[0]).find(pattern) == std::string::npos)
                        continue;
                    std::string chain;
                    int depth = 0;
                    for (int i = 1; i < kProfileFrames && frames[i] && depth < maxDepth; ++i)
                    {
                        const std::string &name = nameOf(frames[i]);
                        if (depth == 0 && name.find(pattern) != std::string::npos)
                            continue;
                        chain += (depth ? " < " : "") + name.substr(0, 48);
                        ++depth;
                    }
                    ++callers[chain];
                }
                print(("callers of " + pattern).c_str(), callers);
            }
        }
        // MAXIMO_PROFILE_LINES=1: self samples by source line (generated VU1 code is one function per image).
        if (std::getenv("MAXIMO_PROFILE_LINES"))
        {
            std::map<std::string, uint32_t> lines;
            for (const auto &frames : g_profileSamples)
            {
                if (!frames[0])
                    continue;
                IMAGEHLP_LINE64 line{};
                line.SizeOfStruct = sizeof line;
                DWORD displacement = 0;
                char key[300];
                if (SymGetLineFromAddr64(process, frames[0], &displacement, &line))
                {
                    const char *file = std::strrchr(line.FileName, '\\');
                    std::snprintf(key, sizeof key, "%s:%lu", file ? file + 1 : line.FileName, line.LineNumber);
                }
                else
                    std::snprintf(key, sizeof key, "%s", nameOf(frames[0]).c_str());
                ++lines[key];
            }
            print("self by line", lines);
        }
        {
            std::map<std::string, uint32_t> waiting;
            size_t waitSamples = 0;
            for (size_t i = 0; i < g_profileSamples.size(); ++i)
                if (g_profileInWait[i] && g_profileSamples[i][0])
                {
                    ++waitSamples;
                    std::set<std::string> seen;
                    for (DWORD64 address : g_profileSamples[i])
                        if (address && seen.insert(nameOf(address)).second)
                            ++waiting[nameOf(address)];
                }
            logf("---- profile: %zu of the samples were in a VBlank wait ----", waitSamples);
            print("inclusive, VBlank wait only", waiting);
        }
        SymCleanup(process);
    }
}

void registerMaximoVu1Aot(); // vu1aot/vu1aot_registry.cpp

int main(int argc, char **argv)
{
    // Launcher tool mode: convert an HD texture pack (hdtex/hdtex_convert.cpp); the game does not start.
    if (argc > 1 && std::strcmp(argv[1], "--hdtex-convert") == 0)
        return hdtex::convertMain(argc - 1, argv + 1);
    // Compiled VU1 microprograms; PS2X_VU1_AOT=0 runs the interpreter only.
    registerMaximoVu1Aot();
    if (argc < 2)
    {
        std::fprintf(stderr, "usage: maximo_boot <disc folder> [seconds=20] [log=boot_log.txt]\n");
        return 2;
    }
    g_disc = fs::absolute(argv[1]);
    const double seconds = argc > 2 ? std::atof(argv[2]) : 20.0;
    const std::string logPath = argc > 3 ? argv[3] : "boot_log.txt";
    g_logPath = logPath;
    const std::string padScript = argc > 4 ? argv[4] : "";
    if (!parsePadScript(padScript))
    {
        std::fprintf(stderr, "bad pad script \"%s\" (expected start:BUTTON[+BUTTON]:length,...)\n", padScript.c_str());
        return 2;
    }
    g_log = std::fopen(logPath.c_str(), "w");
    if (!g_log)
        g_log = stdout;
    SetUnhandledExceptionFilter(crashFilter);
    if (const char *replay = std::getenv("MAXIMO_VU1_REPLAY"))
        return vu1cap::replay(replay, logf);
    if (const char *vu1Thread = std::getenv("PS2X_VU1_THREAD"); vu1Thread && vu1Thread[0] == '1')
    {
        // VU1 capture and the GPU shadow diagnostic hook VU1 and the GS from the game thread.
        if (std::getenv("MAXIMO_VU1_CAPTURE") || std::getenv("MAXIMO_GPU_SHADOW_FRAME"))
        {
            _putenv("PS2X_VU1_THREAD=0");
            logf("PS2X_VU1_THREAD=1 ignored: not supported with MAXIMO_VU1_CAPTURE or MAXIMO_GPU_SHADOW_FRAME");
        }
        else
            logf("PS2X_VU1_THREAD=1: VU1 and the GS back end run on a worker thread (experimental)");
    }

    const fs::path elf = g_disc / maximo_region::kElfName;
    logf("maximo_boot: %s release (%s), disc=%s elf=%s limit=%.1fs", maximo_region::kName, maximo_region::kSerial,
         g_disc.string().c_str(), elf.string().c_str(), seconds);
    for (const PadStep &s : g_padScript)
        logf("pad script: %s", s.text.c_str());

    static PS2Runtime runtime;
    g_runtime = &runtime;
    // MAXIMO_WINDOW=1 runs the runtime's windowed path (CPU GS backend presented through raylib);
    // the default stays headless.
    const bool windowed = std::getenv("MAXIMO_WINDOW") != nullptr;
    runtime.setHeadless(!windowed, seconds);
    logf("presentation: %s", windowed ? "window (CPU GS backend)" : "headless");
    runtime.setMissingFunctionPolicy(PS2Runtime::MissingFunctionPolicy::Stop);
    // Memory cards live in the run directory, never next to the extracted disc files. MAXIMO_MC_ROOT=<dir> uses a
    // card that persists across runs instead (play_with_audio.cmd keeps one in boot\memcard\mc0).
    const char *mcRoot = std::getenv("MAXIMO_MC_ROOT");
    PS2Runtime::setMemoryCardRoot(mcRoot && *mcRoot ? fs::path(mcRoot) : fs::current_path() / "mc0");
    // MAXIMO_ASPECT=16:9: widescreen (WIDESCREEN.md). Set before initialize, which opens a 16:9 window for it.
    const char *aspect = std::getenv("MAXIMO_ASPECT");
    const bool widescreen = aspect && std::strcmp(aspect, "16:9") == 0;
    // MAXIMO_OBJECT_BUDGET=more (or 101..1000): let more of a level's enemies and objects run at once.
    g_objectBudget = parseObjectBudget(std::getenv("MAXIMO_OBJECT_BUDGET"));
    if (widescreen)
        ps2_display_aspect::enableWide();
    if (!runtime.initialize("Maximo boot test") || !runtime.loadELF(elf.string()))
    {
        logf("runtime initialisation or ELF load failed");
        return 1;
    }
#ifdef MAXIMO_ENABLE_GPU_SHADOW
    if (const char *frameText = std::getenv("MAXIMO_GPU_SHADOW_FRAME"))
    {
        if (!windowed || std::getenv("PS2X_GS_THREAD"))
        {
            logf("GPU shadow capture requires MAXIMO_WINDOW=1 and inline CPU GS");
            return 2;
        }
        for (const char *p = frameText; *p;)
        {
            char *end = nullptr;
            const uint64_t frame = std::strtoull(p, &end, 10);
            if (end == p || frame == 0)
            {
                logf("MAXIMO_GPU_SHADOW_FRAME must be a comma-separated list of positive guest frame numbers");
                return 2;
            }
            g_gpuShadowFrames.push_back(frame);
            p = *end == ',' ? end + 1 : end;
        }
        std::sort(g_gpuShadowFrames.begin(), g_gpuShadowFrames.end());
        g_gpuShadow = std::make_unique<GpuShadow>(runtime);
        runtime.gifArbiter().setPathPacketFn([](GifPathId path, const uint8_t *data, uint32_t size, bool syntheticImageTag)
        {
            // Diagnostic shadow: CPU remains the authoritative GS, including all fallback semantics. The shadow goes
            // first so its lazy VRAM snapshot is taken before the CPU applies the packet that triggers it.
            // paraLLEl-GS keeps GIF state across packets like the real GIF, so it must not see host-added tags.
            const uint32_t skip = syntheticImageTag ? 16u : 0u;
            if (size > skip)
                g_gpuShadow->submitPacket(path, data + skip, size - skip);
            g_runtime->gs().processGIFPacket(data, size);
        });
        logf("[gpu-shadow] opt-in capture at guest frames %s", frameText);
    }
    else if (const char *backend = std::getenv("MAXIMO_GS_BACKEND"); backend && std::strcmp(backend, "gpu") == 0)
    {
        if (!windowed || std::getenv("PS2X_GS_THREAD"))
        {
            logf("MAXIMO_GS_BACKEND=gpu requires MAXIMO_WINDOW=1 and no PS2X_GS_THREAD");
            return 2;
        }
        // PS2_GS_CAPTURE_FRAMES is honoured with GPU frames (gpu_sync_NNNN_WxH.rgba); the run does not stop early.
        if (const char *frames = std::getenv("PS2_GS_CAPTURE_FRAMES"))
            for (const char *p = frames; *p;)
            {
                char *end = nullptr;
                const uint64_t frame = std::strtoull(p, &end, 10);
                if (end == p)
                    break;
                g_gpuShadowFrames.push_back(frame);
                p = *end == ',' ? end + 1 : end;
            }
        g_gpuMode = true;
        // Vulkan's implicit layers (overlays and capture hooks other programs install: Steam's overlay and shader
        // cache, OBS, RivaTuner, NVIDIA's Optimus switch) all load into the GPU renderer, which only computes - its
        // picture is shown through the OpenGL window - and creating the Vulkan device with them took 5.2 seconds
        // of every start. They are switched off for this process (MAXIMO_VULKAN_LAYERS=1 keeps them).
        if (const char *layers = std::getenv("MAXIMO_VULKAN_LAYERS"); !(layers && std::strcmp(layers, "1") == 0) &&
                                                                       !std::getenv("VK_LOADER_LAYERS_DISABLE"))
        {
            _putenv_s("VK_LOADER_LAYERS_DISABLE", "~implicit~");
            logf("GPU: Vulkan implicit layers (overlays) off for the renderer (MAXIMO_VULKAN_LAYERS=1 keeps them)");
        }
        // The GPU's pipeline cache next to the executable (gpu_shadow.cpp; MAXIMO_PIPELINE_CACHE=0: none).
        if (!std::getenv("MAXIMO_PIPELINE_CACHE"))
        {
            char exe[MAX_PATH] = {};
            const DWORD n = GetModuleFileNameA(nullptr, exe, MAX_PATH);
            std::string dir(exe, n);
            const size_t slash = dir.find_last_of("\\/");
            dir = slash == std::string::npos ? std::string(".") : dir.substr(0, slash);
            _putenv_s("MAXIMO_PIPELINE_CACHE", (dir + "\\maximo_pipeline_cache.bin").c_str());
        }
        g_gpuShadow = std::make_unique<GpuShadow>(runtime);
        g_gpuShadow->enablePresentation();
        runtime.gs().setRasterBackend(std::make_unique<GSDiscardBackend>());
        runtime.gs().setDiscardDraws(true); // (no draw batches built for a backend that drops them)
        runtime.gifArbiter().setPathPacketFn([](GifPathId path, const uint8_t *data, uint32_t size, bool syntheticImageTag)
        {
            noteVu1Thread();
            const uint32_t skip = syntheticImageTag ? 16u : 0u;
            if (size > skip)
                g_gpuShadow->submitPacket(path, data + skip, size - skip);
            // Register state, SIGNAL/FINISH/LABEL and CSR stay with the CPU frontend; its backend discards drawing.
            // A packet that is only geometry (no A+D register writes, no image data) can change none of that, and
            // is not parsed a second time on the CPU (most of VU1's XGKICK packets with many objects on screen).
            if (gifPacketNeedsCpuGs(data, size))
                g_runtime->gs().processGIFPacket(data, size);
            else
                g_gpuHostSkippedGifPackets.fetch_add(1, std::memory_order_relaxed); // (the GPU's completeness check)
        });
        runtime.setHostFrameSource([](std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height)
                                   {
                                       uint64_t guestFrame = 0;
                                       if (!g_gpuShadow->takeFrame(pixels, width, height, guestFrame))
                                           return false;
                                       if (g_splitActive)
                                       {
                                           // SPLIT VIEW: player 2's frames go to the joining game only.
                                           const uint64_t tagged = guestFrame - static_cast<uint64_t>(g_viewLag);
                                           if (g_viewTag[tagged & 63u].load(std::memory_order_relaxed))
                                           {
                                               streamOfferFrame(pixels, width, height);
                                               return false;
                                           }
                                       }
                                       else
                                           streamOfferFrame(pixels, width, height); // NETPLAY host: the joiner's picture
                                       streamShowFrame(pixels, width, height);  // NETPLAY joiner: the host's picture
                                       drawDebugOverlay(pixels, width, height); // the debug menu, when open
                                       return true;
                                   });
        logf("presentation: paraLLEl-GS (MAXIMO_GS_BACKEND=gpu); CPU GS rasterisation disabled");
    }
#else
    if (std::getenv("MAXIMO_GPU_SHADOW_FRAME") || std::getenv("MAXIMO_GS_BACKEND"))
    {
        logf("GPU shadow/backend requested, but this build has MAXIMO_ENABLE_GPU_SHADOW=OFF");
        return 2;
    }
#endif
    if (const char *capture = std::getenv("MAXIMO_VU1_CAPTURE"))
        vu1cap::installCapture(runtime, capture, logf);
    g_rdram = runtime.memory().getRDRAM();
    if (const char *audio = std::getenv("MAXIMO_AUDIO"); audio && std::strcmp(audio, "0") != 0)
    {
        g_iopAudio = true;
        if (const char *wav = std::getenv("MAXIMO_AUDIO_WAV"))
        {
            g_audio.wav = std::fopen(wav, "wb");
            if (g_audio.wav)
            {
                writeWavHeader(g_audio.wav, 0);
                g_wavWriter.start(g_audio.wav);
            }
            logf("audio: capturing the SPU2 output to %s%s", wav, g_audio.wav ? "" : " (open FAILED)");
        }
        runtime.setIopAudioSink(onIopAudio);
        runtime.setIopAudioPlayback(true);
        // MAXIMO_AUDIO_CLOCK=lockstep|realtime: what times the IOP while its audio plays (see PS2Runtime::IopAudioClock).
        const char *clock = std::getenv("MAXIMO_AUDIO_CLOCK");
        const bool realTime = !(clock && std::strcmp(clock, "lockstep") == 0);
        runtime.setIopAudioClock(realTime ? PS2Runtime::IopAudioClock::RealTime : PS2Runtime::IopAudioClock::Lockstep);
        logf("audio: IOP clock %s", realTime ? "realtime (the audio device's)" : "lockstep (EE time / 8)");
        logf("audio: MJOLNIR.IRX runs in the emulated IOP over native libsd/SPU2 (MAXIMO_AUDIO)");
    }
    // crt0 puts the main thread stack at 0x1F4B000-0x2000000, where the runtime would otherwise carve
    // interrupt/callback stacks. Move them below the ELF image (the real kernel uses its own memory).
    constexpr uint32_t kCbStackFloor = 0x000F0000u, kCbStackTop = 0x00100000u;
    runtime.setAsyncCallbackStackRegion(kCbStackFloor, kCbStackTop);
    logf("interrupt/callback stacks: 0x%06X-0x%06X", kCbStackFloor, kCbStackTop);
    const PS2Runtime::IoPaths &io = PS2Runtime::getIoPaths();
    logf("io paths: cdRoot=%s hostRoot=%s mcRoot=%s", io.cdRoot.string().c_str(), io.hostRoot.string().c_str(),
         io.mcRoot.string().c_str());
    logf("entry pc=0x%06X", runtime.cpu().pc);
    if (const char *guard = std::getenv("MAXIMO_GUARD"))
    {
        g_guardAddr = static_cast<uint32_t>(std::strtoul(guard, nullptr, 16)) & ~3u;
        g_guardValue = rd32(g_guardAddr);
        logf("memory guard on 0x%06X (initial 0x%08X)", g_guardAddr, g_guardValue);
    }
    if (const char *watch = std::getenv("MAXIMO_VALWATCH"))
    {
        int used = 0;
        unsigned long long first = 0, last = 0;
        if (std::sscanf(watch, "%llu:%llu:%n", &first, &last, &used) == 2)
        {
            g_valWatchFirst = first;
            g_valWatchLast = last;
            for (const char *p = watch + used; *p;)
            {
                char *end = nullptr;
                const uint32_t addr = static_cast<uint32_t>(std::strtoul(p, &end, 16)) & PS2_RAM_MASK & ~3u;
                if (end == p)
                    break;
                g_valWatches.push_back({addr, rd32(addr)});
                p = *end == ',' ? end + 1 : end;
            }
        }
    }

    // Wrap every recompiled entry point with the tracer (milestones get their own wrapper). MAXIMO_TRACE=0 leaves the
    // entry points unwrapped (no call ring, entry counts or guard checks).
    g_original.assign(g_ps2RecompiledFunctionTable, g_ps2RecompiledFunctionTable + g_ps2RecompiledFunctionTableSlotCount);
    g_entryCount.assign(g_ps2RecompiledFunctionTableSlotCount, 0);
    const char *traceSetting = std::getenv("MAXIMO_TRACE");
    const bool traceCalls = !traceSetting || std::strcmp(traceSetting, "0") != 0;
    const bool traceFull = (traceSetting && std::strcmp(traceSetting, "full") == 0) || g_guardAddr != 0 ||
                           !g_valWatches.empty();
    g_traceFull = traceFull;
    uint32_t populated = 0;
    for (uint32_t i = 0; i < g_ps2RecompiledFunctionTableSlotCount; ++i)
        if (g_original[i] && traceCalls)
        {
            g_ps2RecompiledFunctionTable[i] = traceFull ? traceEntry : traceEntryLight;
            ++populated;
        }
    for (const Milestone &m : g_milestones)
        if (m.addr) // (0: not in this release)
            runtime.replaceFunction(m.addr, milestoneEntry);
    logf("function table: %u entry points wrapped (%s)", populated,
         traceFull ? "full tracing" : "call ring only; MAXIMO_TRACE=full for entry counts");

    // SIF / IOP boundary.
    bindStub(runtime, GA(0x001AE738u), stub_sceSifInitRpc);
    bindStub(runtime, GA(0x001AEDA8u), stub_sceSifBindRpc);
    bindStub(runtime, GA(0x001AEF78u), stub_sceSifCallRpc);
    bindStub(runtime, GA(0x001AF168u), stub_sceSifCheckStatRpc);
    bindStub(runtime, GA(0x001B3918u), stub_sceSifSyncIop);
    bindStub(runtime, GA(0x001B3960u), stub_sceSifRebootIop);
    bindStub(runtime, GA(0x001B3460u), stub_sceSifLoadModule);
    // libcdvd -> runtime host-file implementation.
    bindRuntime(runtime, GA(0x001A1BA8u), "sceCdInit", ps2_stubs::sceCdInit, 1);
    bindRuntime(runtime, GA(0x001A2598u), "sceCdMmode", ps2_stubs::sceCdMmode, 1);
    bindRuntime(runtime, GA(0x001A08A0u), "sceCdSearchFile", ps2_stubs::sceCdSearchFile, 2);
    // libcdvd S-commands (CDVD S-cmd server 0x80000593): fn 12 status, fn 3 disc type (the disc is a
    // 2.27 GB DVD, so the runtime's SCECdPS2DVD answer is the right one).
    bindRuntime(runtime, GA(0x001A21D0u), "sceCdStatus", ps2_stubs::sceCdStatus, 0);
    bindRuntime(runtime, GA(0x001A2138u), "sceCdGetDiskType", ps2_stubs::sceCdGetDiskType, 0);
    bindRuntime(runtime, GA(0x001A10B0u), "sceCdRead", ps2_stubs::sceCdRead, 4);
    bindRuntime(runtime, GA(0x001A1928u), "sceCdSync", ps2_stubs::sceCdSync, 1);
    bindRuntime(runtime, GA(0x001A19C8u), "sceCdSyncS", ps2_stubs::sceCdSyncS, 1);
    bindRuntime(runtime, GA(0x001A1E88u), "sceCdDiskReady", ps2_stubs::sceCdDiskReady, 1);
    bindRuntime(runtime, GA(0x001A2AF8u), "sceCdStInit", ps2_stubs::sceCdStInit, 3);
    bindRuntime(runtime, GA(0x001A2B28u), "sceCdStStart", cdStStartEntry, 2);
    bindRuntime(runtime, GA(0x001A2B60u), "sceCdStSeekF", ps2_stubs::sceCdStSeekF, 1);
    bindRuntime(runtime, GA(0x001A2B90u), "sceCdStSeek", ps2_stubs::sceCdStSeek, 1);
    bindRuntime(runtime, GA(0x001A2BC0u), "sceCdStStop", cdStStopEntry, 0);
    bindRuntime(runtime, GA(0x001A2BF8u), "sceCdStRead", cdStReadEntry, 4);
    if (const char *fast = std::getenv("MAXIMO_FAST_LOAD"); fast && std::strcmp(fast, "0") == 0)
        g_fastLoad = false;
    if (const char *adaptive = std::getenv("MAXIMO_ADAPTIVE_RATE"); adaptive && std::strcmp(adaptive, "0") == 0)
        g_adaptiveRate = false;
    logf("loading: %s", g_fastLoad ? "fast (the field rate goes up while the game streams a level in; MAXIMO_FAST_LOAD=0: off)"
                                   : "at the game's own pace (MAXIMO_FAST_LOAD=0)");
    bindRuntime(runtime, GA(0x001A2D80u), "sceCdStPause", ps2_stubs::sceCdStPause, 0);
    bindRuntime(runtime, GA(0x001A2DD0u), "sceCdStResume", ps2_stubs::sceCdStResume, 0);
    bindRuntime(runtime, GA(0x001A2E28u), "sceCdStStat", ps2_stubs::sceCdStStat, 0);
    // libpad / libmc -> runtime implementation.
    bindRuntime(runtime, GA(0x001C0110u), "scePadInit", ps2_stubs::scePadInit, 1);
    bindRuntime(runtime, GA(0x001C0370u), "scePadPortOpen", ps2_stubs::scePadPortOpen, 3);
    bindRuntime(runtime, GA(0x001B6D40u), "sceMcInit", ps2_stubs::sceMcInit, 0);
    bindRuntime(runtime, GA(0x001C06C0u), "scePadRead", scriptedPadRead, 3);
    bindRuntime(runtime, GA(0x001C0740u), "scePadGetState", ps2_stubs::scePadGetState, 2);
    bindRuntime(runtime, GA(0x001C0558u), "scePadPortClose", ps2_stubs::scePadPortClose, 2);
    bindRuntime(runtime, GA(0x001C02F0u), "scePadEnd", ps2_stubs::scePadEnd, 0);
    // The game's poller (0x128CB0) learns the controller type through these before it ever reads.
    bindRuntime(runtime, GA(0x001C07B8u), "scePadGetReqState", ps2_stubs::scePadGetReqState, 2);
    bindRuntime(runtime, GA(0x001C07F0u), "scePadSetReqState", ps2_stubs::scePadSetReqState, 3);
    bindRuntime(runtime, GA(0x001C08E0u), "scePadInfoAct", ps2_stubs::scePadInfoAct, 4);
    bindRuntime(runtime, GA(0x001C0B20u), "scePadInfoMode", ps2_stubs::scePadInfoMode, 4);
    bindRuntime(runtime, GA(0x001C0C58u), "scePadSetMainMode", ps2_stubs::scePadSetMainMode, 4);
    bindRuntime(runtime, GA(0x001C0D10u), "scePadSetActDirect", ps2_stubs::scePadSetActDirect, 3);
    bindRuntime(runtime, GA(0x001C0DC8u), "scePadSetActAlign", ps2_stubs::scePadSetActAlign, 3);
    // libmc client (identified by the MCSERV RPC function each one sends: 1 GetInfo, 2 Open, 3 Close,
    // 4 Seek, 5 Read, 6 Write, 12 ChDir, 13 GetDir, 16 Format; Mkdir is Open with mode 0x40; Sync
    // polls sceSifCheckStatRpc). The original client talks to MCSERV, which is not running.
    bindRuntime(runtime, GA(0x001B77F8u), "sceMcGetInfo", ps2_stubs::sceMcGetInfo, 5);
    bindRuntime(runtime, GA(0x001B7068u), "sceMcOpen", ps2_stubs::sceMcOpen, 4);
    bindRuntime(runtime, GA(0x001B7178u), "sceMcMkdir", ps2_stubs::sceMcMkdir, 3);
    bindRuntime(runtime, GA(0x001B71B0u), "sceMcClose", ps2_stubs::sceMcClose, 1);
    bindRuntime(runtime, GA(0x001B7268u), "sceMcSeek", ps2_stubs::sceMcSeek, 3);
    bindRuntime(runtime, GA(0x001B73D0u), "sceMcRead", ps2_stubs::sceMcRead, 3);
    bindRuntime(runtime, GA(0x001B74F0u), "sceMcWrite", ps2_stubs::sceMcWrite, 3);
    bindRuntime(runtime, GA(0x001B7B38u), "sceMcChdir", ps2_stubs::sceMcChdir, 4);
    bindRuntime(runtime, GA(0x001B7980u), "sceMcGetDir", ps2_stubs::sceMcGetDir, 6);
    bindRuntime(runtime, GA(0x001B7C68u), "sceMcFormat", ps2_stubs::sceMcFormat, 2);
    bindRuntime(runtime, GA(0x001B76C0u), "sceMcSync", ps2_stubs::sceMcSync, 3);
    // MAXIMO_WATCHFN=addr[,addr...]: extra watch points (first four arguments logged), for diagnostics.
    if (const char *extra = std::getenv("MAXIMO_WATCHFN"))
        for (const char *p = extra; *p;)
        {
            char *end = nullptr;
            const uint32_t addr = static_cast<uint32_t>(std::strtoul(p, &end, 16));
            if (end == p)
                break;
            g_watches.emplace(addr, Watch{"MAXIMO_WATCHFN", 4});
            p = *end == ',' ? end + 1 : end;
        }
    for (const auto &[addr, w] : g_watches)
        if (addr && !isMilestone(addr)) // (0: not in this release)
            runtime.replaceFunction(addr, watchEntry);
    runtime.replaceFunction(GA(0x001AAA18u), gsSyncVEntry); // sceGsSyncV: frame timing
    runtime.replaceFunction(kLookAtFn, debugLookAtEntry); // debug menu: free camera
    runtime.replaceFunction(kResourceCopyFn, resourceCopyProbeEntry); // debug menu: spawn diagnostics
    runtime.replaceFunction(kResourceLookupFn, resourceLookupProbeEntry);
    if (const char *skip = std::getenv("MAXIMO_SKIP_INTRO"); skip && std::strcmp(skip, "0") != 0)
    {
        if (introCodeMatches())
        {
            runtime.replaceFunction(kLogoScreenFn, skipLogoEntry);
            runtime.replaceFunction(kTitleScreenFn, skipTitleEntry);
            logf("skip intro: on (logos and title screen)");
        }
    }
    runtime.replaceFunction(kGameVarSubFn, godVarSubEntry);
    runtime.replaceFunction(kLevelLoopFn, levelLoopEntry); // PLAYER 2 (and the 60 Hz pacing)
    runtime.replaceFunction(kLevelStartFn, levelStartEntry);
    runtime.replaceFunction(kObjectStateFn, objectStateEntry);
    runtime.replaceFunction(GA(0x00117B80u), survivalGotoEntry); // survival here: the ring's columns do not end the level
    runtime.replaceFunction(kCutSceneStartApiFn, survivalCutsceneEntry); // survival: no boss cutscenes
    runtime.replaceFunction(kKingCameraApiFn, survivalCutsceneEntry);
    runtime.replaceFunction(kSetFreezeApiFn, survivalSetFreezeEntry);
    runtime.replaceFunction(kSetBossFlagApiFn, survivalBossFlagEntry);
    runtime.replaceFunction(kRunObjectScriptFn, runObjectScriptEntry); // PLAYER 2: scripts see the nearest Maximo
    runtime.replaceFunction(kRunObjectEventFn, runObjectEventEntry);
    runtime.replaceFunction(kBodyCollisionFn, bodyCollisionEntry);     // PLAYER 2: hits on his body are his
    for (const TargetApi &t : g_targetApis)
        runtime.replaceFunction(t.fn, targetApiEntry); // PLAYER 2 diagnostics
    runtime.replaceFunction(kPlayerUpdateFn, playerUpdateEntry);
    runtime.replaceFunction(kFrameLateFn, frameLateEntry);
    runtime.replaceFunction(kSetDamageFn, setDamageEntry); // debug menu: one hit kill
    runtime.replaceFunction(GA(0x0015B100u), wideCameraEntry); // widescreen (MAXIMO_ASPECT) and the free camera's zoom
    runtime.replaceFunction(kHudDrawFn, hudDrawEntry);
    runtime.replaceFunction(kVmUpdateFn, pausableEntry);
    runtime.replaceFunction(kParticlesFn, pausableEntry);
    runtime.replaceFunction(kParticlesExtraFn, pausableEntry);
    runtime.replaceFunction(GA(0x001AA1C0u), putDispEnvEntry); // sceGsPutDispEnv: GPU-mode movie presentation
    if (g_objectBudget > kObjectBudgetVanilla)
    {
        runtime.replaceFunction(kVmCtorFn, objectBudgetCtorEntry);
        installExtFlags(runtime, g_rdram);
    }
    // MAXIMO_RAM=more: more memory for the game. The emulated RAM is PS2_RAM_SIZE (128 MB in this build); the game only
    // ever uses what its heap is given. crt0 puts the main stack at 0x1F4B000-0x2000000 and ends the heap at that
    // stack (SetupHeap(0x2F5E00, -1)), so the runtime moves the stack to the top of RAM and the heap ends below the
    // runtime's own pools instead: about 95 MB more for enemies, models and effects. Unset: the retail 32 MB layout.
    // (Each extra enemy costs the game roughly 100 KB: 10x enemies filled a 64 MB pool and froze.)
    {
        const char *ram = std::getenv("MAXIMO_RAM");
        const bool extended = ram && (std::strcmp(ram, "64") == 0 || std::strcmp(ram, "128") == 0 ||
                                      std::strcmp(ram, "more") == 0 || std::strcmp(ram, "1") == 0);
        runtime.setExtendedRam(extended);
        if (runtime.extendedRam())
            runtime.replaceFunction(kHeapSetupCtor, heapSetupEntry);

        if (extended && !runtime.extendedRam())
            logf("memory: MAXIMO_RAM=%s ignored: this build has %u MB of RAM", ram, PS2_RAM_SIZE >> 20);
        else if (extended)
            logf("memory: %u MB; the game's heap may grow to 0x%07X (retail 32 MB: 0x1F4B000), main stack at the top "
                 "of RAM",
                 PS2_RAM_SIZE >> 20, PS2_RUNTIME_RESERVED_BASE);
        else
            logf("memory: retail layout, the game's heap ends at 0x1F4B000 (%u MB emulated; MAXIMO_RAM=64 for more)",
                 PS2_RAM_SIZE >> 20);
    }
    if (widescreen)
    {
        g_wideCamera = true;
        runtime.replaceFunction(GA(0x0016AE00u), wideCullEntry);
        runtime.replaceFunction(GA(0x00148470u), wideOverlayEntry);
        runtime.replaceFunction(kScaleMatrixFn, wideFlashEntry);
        runtime.replaceFunction(kSetViewFn, wideHudEntry);
        runtime.replaceFunction(kSetModelFn, wideHudEntry);
        const char *hud = std::getenv("MAXIMO_WIDE_HUD");
        g_hudEdges = !hud || std::strcmp(hud, "0") != 0;
        logf("widescreen 16:9: HUD side groups %s (MAXIMO_WIDE_HUD=0 keeps them in the 4:3 area)",
             g_hudEdges ? "at the screen edges" : "in the 4:3 area");
        g_widescreen = true;
        logf("widescreen 16:9: camera x scale %.4f, culling factor %.4f", kWideXScale, kWideCullFactor);
    }
    if (const char *bits = std::getenv("MAXIMO_MOVIE_BITS"); !bits || std::strcmp(bits, "0") != 0)
    {
        g_movieBitsVerify = bits && std::strcmp(bits, "verify") == 0;
        for (uint32_t fn : {kBitsPeekFn, kBitsSkipFn, kBitsReadFn, kBitsReadBitFn})
            runtime.replaceFunction(fn, movieBitsEntry);
        for (uint32_t fn : {kPackHeaderFn, kPesHeaderFn})
            runtime.replaceFunction(fn, movieHeaderEntry);
        logf("movie bit reader and packet headers: native%s (MAXIMO_MOVIE_BITS=0 keeps the game's code)",
             g_movieBitsVerify ? ", checked against the game's code" : "");
    }
#ifdef MAXIMO_HAS_FFMPEG
    if (const char *ff = std::getenv("MAXIMO_MOVIE_FFMPEG"); !ff || std::strcmp(ff, "0") != 0)
    {
        // The FFmpeg DLLs are delay-loaded: without them the game's decoder stays.
        if (LoadLibraryA("avutil-59.dll") && LoadLibraryA("avcodec-61.dll") && movieDecoderOpen(g_movie))
        {
            runtime.replaceFunction(kGetPictureFn, movieGetPictureEntry);
            for (uint32_t fn : {kMpegCreateFn, kMpegResetFn})
                runtime.replaceFunction(fn, movieMpegResetEntry);
            g_movieFfmpeg = true;
            logf("movie pictures: FFmpeg %s (MAXIMO_MOVIE_FFMPEG=0 keeps the game's decoder)", av_version_info());
        }
        else
        {
            logf("movie pictures: FFmpeg not available, the game's decoder decodes them");
        }
    }
#endif
    // MAXIMO_FRAME_RATE: the gameplay rate - 50 (PAL's own), 60 or 120 (HIGH FRAME RATE). The front end, the
    // attract demo and movies stay at the release's own rate.
    {
        // MAXIMO_FRAME_RATE=refresh: the monitor's refresh rate (165 on a 165 Hz screen), so with VSync every refresh
        // shows a new frame - 120 frames on a 165 Hz screen can only be shown unevenly (one or two refreshes each).
        const char *rate = std::getenv("MAXIMO_FRAME_RATE");
        uint32_t asked = rate && *rate ? static_cast<uint32_t>(std::atoi(rate)) : kNativeHz;
        if (rate && std::strcmp(rate, "refresh") == 0)
        {
            const int hz = GetMonitorRefreshRate(GetCurrentMonitor());
            asked = hz >= 60 ? static_cast<uint32_t>(hz) : kNativeHz;
            logf("frame rate: the monitor refreshes at %d Hz", hz);
        }
        if (asked >= 60 && asked <= 240)
            g_frameRateHz = std::max(asked, kNativeHz);
        else if (asked != kNativeHz && asked != 50)
            logf("frame rate: MAXIMO_FRAME_RATE=%s is not supported (50 to 240, or refresh); keeping %u Hz", rate,
                 kNativeHz);
        if (g_frameRateHz > kNativeHz || !kPalTimings)
            runtime.replaceFunction(kLevelLoopFn, levelLoopEntry); // (gameplay detection; PLAYER 2)
        for (const uint32_t fn : kFrameUpdateFns)
            runtime.replaceFunction(fn, frameUpdateEntry); // (the real step above the release's own rate)
        if (g_frameRateHz > kNativeHz)
        {
            runtime.replaceFunction(kAnimStepFn, animStepEntry);
            runtime.replaceFunction(kCameraShakeFn, cameraShakeEntry);
            runtime.replaceFunction(kLightningFn, lightningEntry);
        }
        logf("frame rate: %u Hz in gameplay, %u Hz (the %s release's own) in the front end, attract demo and movies%s",
             g_frameRateHz, kNativeHz, maximo_region::kName,
             g_frameRateHz > 60 ? " - above 60 Hz wants a monitor at that rate or a multiple of it (or G-Sync/FreeSync)" : "");
        if (std::getenv("PS2X_VBLANK_HZ"))
            logf("frame rate: PS2X_VBLANK_HZ is set and pins the field rate; MAXIMO_FRAME_RATE has no effect");
    }
    // MAXIMO_FAST_LOAD=0 keeps loading at the PS2's pace (see gsSyncVEntry).
    if (const char *fast = std::getenv("MAXIMO_FAST_LOAD"); !fast || std::strcmp(fast, "0") != 0)
    {
        ps2_host_profile::g_fastForwardAfter.store(4u, std::memory_order_relaxed);
        logf("fast loading: on (no frame for 4 fields: guest time is not held to the clock; MAXIMO_FAST_LOAD=0: off)");
    }
    {
        const char *camLog = std::getenv("MAXIMO_CAMERA_LOG");
        const char *camTest = std::getenv("MAXIMO_CAMERA_TEST");
        g_camLog = camLog && std::strcmp(camLog, "0") != 0;
        if (camTest)
        {
            g_camTestDeg = static_cast<float>(std::atof(camTest));
            if (const char *comma = std::strchr(camTest, ','))
                g_camTestAxis = comma[1] == 'x' ? 0 : comma[1] == 'z' ? 2 : 1;
        }
        if (const char *mode = std::getenv("MAXIMO_CAMERA"); mode && std::strcmp(mode, "orbit") == 0)
        {
            g_orbit.on = true;
            if (const char *speed = std::getenv("MAXIMO_CAMERA_SPEED"))
            {
                const float deg = static_cast<float>(std::atof(speed));
                if (deg > 0.0f && deg <= 1000.0f)
                    g_orbit.speed = deg * 3.14159265f / 180.0f;
            }
            if (const char *ret = std::getenv("MAXIMO_CAMERA_RETURN"))
                g_orbit.returnDelay = std::max(0.0f, static_cast<float>(std::atof(ret)));
            const char *invert = std::getenv("MAXIMO_CAMERA_INVERT_Y");
            g_orbit.invertY = invert && std::strcmp(invert, "0") != 0;
            logf("camera: free camera on (right stick orbits, R3 hands back), %.0f degrees/s%s, back after %.1f s",
                 g_orbit.speed * 57.2958f, g_orbit.invertY ? ", inverted up/down" : "", g_orbit.returnDelay);
        }
        if (g_camLog || g_camTestDeg != 0.0f || g_orbit.on)
        {
            runtime.replaceFunction(kCameraApplyFn, cameraApplyEntry);
            runtime.replaceFunction(kCameraViewFn, cameraViewEntry);
            logf("camera probe: log %d, test %.1f degrees about %c", g_camLog ? 1 : 0, g_camTestDeg,
                 "xyz"[g_camTestAxis]);
        }
    }
    if (const char *spin = std::getenv("MAXIMO_SPIN_SKIP"); !spin || std::strcmp(spin, "0") != 0)
    {
        // Chunked by default now: waiting for VBlank the game spins through the rest of the field's EE cycles, and
        // at 32 cycles a turn that was tens of thousands of turns every frame (each one stepping the timers and
        // the IOP) - host CPU time spent on waiting. MAXIMO_SPIN_SKIP=exact keeps the 32-cycle steps (the reference
        // trajectory for frame comparisons).
        g_vblankSpinChunked = !spin || std::strcmp(spin, "exact") != 0;
        logf("VBlank wait: %s", g_vblankSpinChunked ? "idle cycles in chunks of up to 1024 (MAXIMO_SPIN_SKIP=exact: 32)"
                                                    : "32-cycle steps (MAXIMO_SPIN_SKIP=exact)");
        runtime.replaceFunction(GA(0x001AC320u), vblankSpinEntry); // sceGsSyncV's VBlank spin: idle cycles in chunks
        runtime.replaceFunction(GA(0x001AC330u), vblankSpinEntry); // its resume point after a checkpoint
    }
    // libc.
    bindStub(runtime, GA(0x0019C3E8u), stub_printf);
    bindStub(runtime, GA(0x00197F88u), stub_assert);

    ps2_stubs::setCdStreamPastFileEnd(true);
    // MAXIMO_MMIO_TRACE=0 leaves out the hardware-register access log (the report at exit).
    if (const char *trace = std::getenv("MAXIMO_MMIO_TRACE"); !trace || std::strcmp(trace, "0") != 0)
        PS2Runtime::setMmioTraceHook(mmioHook);
    logf("---- run ----");
    // The windowed loop has no time limit of its own.
    std::thread windowTimer;
    if (windowed)
        windowTimer = std::thread([seconds]()
                                  {
            std::this_thread::sleep_for(std::chrono::duration<double>(seconds));
            g_runtime->requestStop(); });
    if (windowTimer.joinable())
        windowTimer.detach(); // closing the window early must not wait out the timer
    std::thread profiler;
    if (const char *profile = std::getenv("MAXIMO_PROFILE"))
    {
        double start = 0.0, end = 1e9;
        // f<first>,<last>: a guest frame range (sceGsSyncV count) instead of wall-clock seconds.
        const bool guestFrames = profile[0] == 'f';
        std::sscanf(profile + (guestFrames ? 1 : 0), "%lf,%lf", &start, &end);
        profiler = std::thread(profileThread, start, end, guestFrames);
    }
    if (const char *pipeline = std::getenv("PS2X_VU1_PIPELINE"); pipeline && pipeline[0] == '1')
    {
        // The worker presents frames itself, which only GPU mode supports.
        const char *vu1Thread = std::getenv("PS2X_VU1_THREAD");
#ifdef MAXIMO_ENABLE_GPU_SHADOW
        const bool gpuMode = g_gpuMode;
#else
        const bool gpuMode = false;
#endif
        if (!gpuMode || !vu1Thread || vu1Thread[0] != '1')
        {
            _putenv("PS2X_VU1_PIPELINE=0");
            logf("PS2X_VU1_PIPELINE=1 ignored: needs PS2X_VU1_THREAD=1 and MAXIMO_GS_BACKEND=gpu");
        }
        else
            logf("PS2X_VU1_PIPELINE=1: the VU1 worker presents frame N while the game builds frame N+1 (experimental)");
    }
    runtime.run();
    logf("shutdown: window loop ended: %s", ps2_exit_reason::get());
#ifdef MAXIMO_ENABLE_GPU_SHADOW
    runtime.setHostFrameSource(nullptr);
    runtime.gifArbiter().setPathPacketFn(nullptr);
    g_gpuShadow.reset();
    if (g_gpuFailed.load())
        logf("[gpu] run stopped by a GPU-mode error (see stderr)");
#endif
    g_profileStop.store(true);
    if (profiler.joinable())
        profiler.join();
    PS2Runtime::setMmioTraceHook(nullptr);
    logf("---- stopped ----");
    vu1cap::finishCapture(logf);
    reportProfile();

    // ---- report ------------------------------------------------------------------------------
    const R5900Context &c = runtime.cpu();
    logf("final pc register 0x%06X; distinct guest functions entered: %u", c.pc, g_distinct);
    for (const Milestone &m : g_milestones)
        logf("  milestone %-3s 0x%06X %s", m.reached ? "YES" : "no", m.addr, m.what);
    dumpRing("end of run");

    std::vector<std::pair<uint32_t, uint32_t>> hot;
    for (uint32_t i = 0; i < g_entryCount.size(); ++i)
        if (g_entryCount[i])
            hot.push_back({g_entryCount[i], g_ps2RecompiledFunctionTableBase + i * 4});
    std::sort(hot.rbegin(), hot.rend());
    // Heuristic backtrace: words on the main thread's stack that point just past a jal/jalr.
    for (const EeThreadSnapshot &t : runtime.eeScheduler().snapshot().threads)
    {
        if (t.id != 1 || t.sp == 0)
            continue;
        logf("---- main thread backtrace (stack scan from sp=0x%07X) ----", t.sp);
        logf("  pc 0x%06X  ra 0x%06X", t.pc, t.ra);
        int frames = 0;
        for (uint32_t a = t.sp & ~3u; a < PS2_RAM_SIZE && frames < 24; a += 4)
        {
            const uint32_t v = rd32(a);
            if (v < GA(0x00100008u) || v >= GA(0x001C3E80u) || (v & 3u))
                continue;
            const uint32_t call = rd32(v - 8u);
            const bool jal = (call >> 26) == 3u;
            const bool jalr = (call & 0xFC00003Fu) == 0x00000009u;
            if (!jal && !jalr)
                continue;
            if (jal)
                logf("  [sp+0x%05X] return 0x%06X  (call to 0x%06X)", a - t.sp, v, (call & 0x03FFFFFFu) << 2);
            else
                logf("  [sp+0x%05X] return 0x%06X  (indirect call)", a - t.sp, v);
            ++frames;
        }
    }

    logf("---- most-entered functions ----");
    for (size_t i = 0; i < std::min<size_t>(hot.size(), 25); ++i)
        logf("  0x%06X entered %u times", hot[i].second, hot[i].first);

    const EeKernelSnapshot snap = runtime.eeScheduler().snapshot();
    logf("---- EE threads (cycle %llu) ----", static_cast<unsigned long long>(snap.eeCycle));
    static const char *status[] = {"Running", "Ready", "Waiting", "WaitingSuspended", "Suspended", "Dormant"};
    static const char *wait[] = {"None", "Sleep", "Semaphore", "EventFlag", "VSync", "External", "Mpeg"};
    for (const EeThreadSnapshot &t : snap.threads)
        logf("  thread %d pc=0x%06X ra=0x%06X entry=0x%06X prio=%d %s wait=%s(%d)", t.id, t.pc, t.ra, t.entry,
             t.currentPriority, status[static_cast<int>(t.status)], wait[static_cast<int>(t.waitReason)], t.waitId);

    finishAudio();
    logf("---- SDK boundary calls ----");
    for (const auto &[addr, b] : g_bound)
        if (b.calls)
            logf("  0x%06X %-16s %llu calls", addr, b.name, static_cast<unsigned long long>(b.calls));
    for (const RpcServer &s : g_servers)
        logf("  RPC server 0x%08X %-12s %llu calls", s.sid, sidName(s.sid), static_cast<unsigned long long>(s.calls));
    for (const auto &[key, n] : g_unhandledRpc)
        logf("  UNHANDLED RPC sid=0x%08X fn=0x%X: %llu calls", static_cast<uint32_t>(key >> 16),
             static_cast<uint32_t>(key & 0xFFFF), static_cast<unsigned long long>(n));
    for (uint32_t core = 0; core < 2; ++core)
        if (g_movieAudio[core].transfers)
            logf("  movie-audio drain core %u: %llu block transfers, %llu status replies, %s, %llu bytes discarded at stop",
                 core, static_cast<unsigned long long>(g_movieAudio[core].transfers),
                 static_cast<unsigned long long>(g_movieAudio[core].statusCalls),
                 g_movieAudio[core].active ? "still running" : "stopped",
                 static_cast<unsigned long long>(g_movieAudioDrainedBytes));
    for (const auto &[fn, n] : g_sdRemoteCalls)
        logf("  LIBSD remote 0x%X (emulated IOP libsd): %llu calls", fn, static_cast<unsigned long long>(n));
    logf("  GPU vsyncs from display flips without sceGsSyncV (movies): %llu", static_cast<unsigned long long>(g_flipVsyncs));
    if (g_movieBitsCalls)
        logf("  movie bit reader: %llu native calls%s", static_cast<unsigned long long>(g_movieBitsCalls),
             g_movieBitsVerify ? (", " + std::to_string(g_movieBitsChecked) + " checked against the game's code, " +
                                  std::to_string(g_movieBitsMismatches) + " differ").c_str()
                               : "");
    if (g_movieHeaderCalls || g_movieHeaderChecked)
        logf("  movie packet headers: %llu native calls%s", static_cast<unsigned long long>(g_movieHeaderCalls),
             g_movieBitsVerify ? (", " + std::to_string(g_movieHeaderChecked) + " checked against the game's code, " +
                                  std::to_string(g_movieHeaderMismatches) + " differ").c_str()
                               : "");
#ifdef MAXIMO_HAS_FFMPEG
    if (g_movieFfmpeg)
        logf("  movie pictures: %llu decoded by FFmpeg from %llu access units, %llu rejected",
             static_cast<unsigned long long>(g_movie.pictures), static_cast<unsigned long long>(g_movie.units),
             static_cast<unsigned long long>(g_movie.errors));
#endif
    if (g_frameRateHz != 50)
        logf("  frame rate %u Hz: %llu rate switches, %llu frame-counter increments skipped, %llu animation steps, "
             "%llu camera shake frames and %llu lightning frames paced", g_frameRateHz, static_cast<unsigned long long>(g_frameRateSwitches),
             static_cast<unsigned long long>(g_counterHolds), static_cast<unsigned long long>(g_fixedAnimSteps),
             static_cast<unsigned long long>(g_shakeFrames), static_cast<unsigned long long>(g_lightningFrames));
    logf("  sound driver: %llu packets, %llu malformed", static_cast<unsigned long long>(g_soundPackets),
         static_cast<unsigned long long>(g_soundMalformed));
    for (const auto &[id, st] : g_soundCmds)
        logf("    sound cmd %3u nargs=%u: %llu", id, st.nargs, static_cast<unsigned long long>(st.count));
    for (const std::string &m : g_loadedModules)
        logf("  module accepted: %s", m.c_str());

    logf("---- game heap object 0x2393B0 (pools at +0x20, +0x140, +0x260) ----");
    for (uint32_t off = 0; off < 0x60; off += 16)
        logf("  +0x%03X: %08X %08X %08X %08X", off, rd32(GA(0x2393B0u) + off), rd32(GA(0x2393B4u) + off), rd32(GA(0x2393B8u) + off),
             rd32(GA(0x2393BCu) + off));
    logf("---- RAM below the ELF image (0x000000-0x100000) written by the run ----");
    for (uint32_t page = 0; page < 0x100000u; page += 0x1000u)
    {
        uint32_t nonzero = 0;
        for (uint32_t a = page; a < page + 0x1000u; a += 4)
            nonzero += rd32(a) != 0;
        if (nonzero)
            logf("  page 0x%06X: %u nonzero words", page, nonzero);
    }

    logf("---- hardware registers touched ----");
    for (const auto &[addr, s] : g_mmio)
        logf("  %-13s 0x%08X reads=%llu writes=%llu last=0x%llX pc~0x%06X", mmioUnit(addr), addr,
             static_cast<unsigned long long>(s.reads), static_cast<unsigned long long>(s.writes),
             static_cast<unsigned long long>(s.lastWrite), s.lastPc);
    logf("  scratchpad accesses via slow path: %llu; uncached RAM mirror accesses: %llu",
         static_cast<unsigned long long>(g_scratchAccesses), static_cast<unsigned long long>(g_uncachedAccesses));
    // The launcher treats this line as "the game finished shutting down": a process still alive well after it is
    // hanging in process exit. stdout and stderr are redirected to a file by the launcher, so flush them too.
    logf("shutdown: log complete, exiting process");
    std::fflush(stdout);
    std::fflush(stderr);
    std::fflush(g_log);
    std::_Exit(0); // guest threads may still be parked; skip static destructors
}
