#ifndef PS2_RUNTIME_H
#define PS2_RUNTIME_H

#include <cstring>
#include <cstdint>
#include <vector>
#include <string>
#include <string_view>
#include <functional>
#if defined(_MSC_VER)
#include <intrin.h>
#elif defined(USE_SSE2NEON)
#include "sse2neon.h"
#else
#include <immintrin.h> // For SSE/AVX instructions
#include <smmintrin.h> // For SSE4.1 instructions
#endif
#include <atomic>
#include <array>
#include <mutex>
#include <filesystem>
#include <memory>
#include <unordered_map>
#include <unordered_set>

#include "ps2_log.h"
#include "runtime/ps2_address.h"
#include "runtime/gs/ps2_gif_arbiter.h"
#include "runtime/ps2_memory.h"
#include "runtime/gs/gs_frontend.h"
#include "runtime/ps2_vu1.h"
#include "runtime/ps2_audio.h"
#include "runtime/ps2_pad.h"
#include "runtime/ps2_rom_device.h"
#include "runtime/ps2_vfs.h"
#include "ps2x/iop/iop_types.h"

namespace ps2x::iop
{
    class IopSubsystem;
}

class PS2IopHostAdapter;
class PS2IopTransport;
class EeScheduler;
struct EeEvent;

enum PS2Exception
{
    EXCEPTION_TLB_REFILL = 0x02,          // TLB refill/load exception
    EXCEPTION_ADDRESS_ERROR_LOAD = 0x04,  // Address error on load
    EXCEPTION_ADDRESS_ERROR_STORE = 0x05, // Address error on store
    EXCEPTION_SYSCALL = 0x08,             // SYSCALL instruction
    EXCEPTION_BREAKPOINT = 0x09,          // BREAK instruction
    EXCEPTION_RESERVED_INSTRUCTION = 0x0A,
    EXCEPTION_INTEGER_OVERFLOW = 0x0C, // From MIPS spec
    EXCEPTION_TRAP = 0x0D,             // Trap instruction condition met
};

// PS2 CPU context (R5900)
struct alignas(16) R5900Context
{
    // General Purpose Registers (128-bit)
    __m128i r[32]; // Main registers

    // Control registers
    uint32_t pc;         // Program counter
    uint64_t insn_count; // Instruction counter
    uint64_t hi, lo;     // HI/LO registers for mult/div results
    uint64_t hi1, lo1;   // Secondary HI/LO registers for MULT1/DIV1
    uint32_t sa;         // Shift amount register

    // VU0 registers (when used in macro mode)
    __m128 vu0_vf[32];        // VU0 vector float registers
    uint16_t vi[16];          // VU0 vector integer registers
    float vu0_q;              // VU0 Q register (quotient)
    float vu0_p;              // VU0 P register (EFU result)
    float vu0_i;              // VU0 I register (integer value)
    __m128 vu0_r;             // VU0 R register
    __m128 vu0_acc;           // VU0 ACC accumulator register
    uint16_t vu0_status;      // VU0 status register
    uint32_t vu0_mac_flags;   // VU0 MAC flags
    uint32_t vu0_clip_flags;  // VU0 clipping flags
    uint32_t vu0_clip_flags2; // VU0 clipping flags
    uint32_t vu0_cmsar0;      // VU0 microprogram start address
    uint32_t vu0_cmsar1;      // VU0 microprogram start address
    uint32_t vu0_cmsar2;      // VU0 microprogram start address
    uint32_t vu0_cmsar3;      // VU0 microprogram start address
    uint32_t vu0_vpu_stat;
    uint32_t vu0_vpu_stat2; // extra VPU status (used by CR_VPU_STAT2)
    uint32_t vu0_vpu_stat3; // extra VPU status 3
    uint32_t vu0_vpu_stat4; // extra VPU status 4
    uint32_t vu0_tpc;       // TPC (VU0 PC)
    uint32_t vu0_tpc2;      // second TPC
    uint32_t vu0_fbrst;     // VIF/VU reset register
    uint32_t vu0_fbrst2;    // FBRST2
    uint32_t vu0_fbrst3;    // FBRST3
    uint32_t vu0_fbrst4;    // FBRST4
    uint32_t vu0_itop;
    uint32_t vu0_top;
    uint32_t vu0_info;
    uint32_t vu0_xitop; // VU0 XITOP - input ITOP for VIF/VU sync
    uint32_t vu0_pc;

    float vu0_cf[4]; // VU0 FMAC control floating-point registers

    // COP0 System control registers
    uint32_t cop0_index;
    uint32_t cop0_random;
    uint32_t cop0_entrylo0;
    uint32_t cop0_entrylo1;
    uint32_t cop0_context;
    uint32_t cop0_pagemask;
    uint32_t cop0_wired;
    uint32_t cop0_badvaddr;
    uint32_t cop0_count;
    uint32_t cop0_entryhi;
    uint32_t cop0_compare;
    uint32_t cop0_status;
    uint32_t cop0_cause;
    uint32_t cop0_epc;
    uint32_t cop0_prid;
    uint32_t cop0_config;
    uint32_t cop0_badpaddr;
    uint32_t cop0_debug;
    uint32_t cop0_perf;
    uint32_t cop0_taglo;
    uint32_t cop0_taghi;
    uint32_t cop0_errorepc;

    // LL/SC reservation state (not part of COP0 Status bits).
    uint32_t llbit;
    uint32_t lladdr;

    // Delay slot state tracking
    bool in_delay_slot;
    uint32_t branch_pc;

    // COP2 control registers (VU0 integer + control)
    uint32_t cop2_ccr[32];

    // FPU registers (COP1)
    float f[32];
    float f_acc;    // FPU accumulator
    uint32_t fcr31; // Control/status register

    R5900Context()
    {
        std::memset(this, 0, sizeof(*this));

        // Initialize VU0 registers
        vu0_q = 1.0f; // Q register usually initialized to 1.0

        // Reset COP0 registers
        cop0_random = 47; // Start at maximum value
        // Status as the EE kernel leaves it at handoff. IE (bit 0) and EIE
        // (bit 16) are separate enables and guest code reads both; libkernel's
        // StartThread refuses to run while IE is clear.
        cop0_status = 0x00010001; // EIE | IE
        cop0_prid = 0x00002e20; // CPU ID for R5900

        in_delay_slot = false;
        branch_pc = 0;
    }

    void dump() const
    {
        std::ios_base::fmtflags flags = std::cout.flags();
        std::cout << std::hex << std::setfill('0');
        std::cout << "--- R5900 Context Dump ---\n";
        std::cout << "PC: 0x" << std::setw(8) << pc << "\n";
        std::cout << "HI: 0x" << std::setw(8) << hi << " LO: 0x" << std::setw(8) << lo << "\n";
        std::cout << "HI1:0x" << std::setw(8) << hi1 << " LO1:0x" << std::setw(8) << lo1 << "\n";
        std::cout << "SA: 0x" << std::setw(8) << sa << "\n";
        for (int i = 0; i < 32; ++i)
        {
            std::cout << "R" << std::setw(2) << std::dec << i << ": 0x" << std::hex
                      << std::setw(8) << static_cast<uint32_t>(_mm_extract_epi32(r[i], 3))
                      << std::setw(8) << static_cast<uint32_t>(_mm_extract_epi32(r[i], 2)) << "_"
                      << std::setw(8) << static_cast<uint32_t>(_mm_extract_epi32(r[i], 1))
                      << std::setw(8) << static_cast<uint32_t>(_mm_extract_epi32(r[i], 0)) << "\n";
        }
        std::cout << "Status: 0x" << std::setw(8) << cop0_status
                  << " Cause: 0x" << std::setw(8) << cop0_cause
                  << " EPC: 0x" << std::setw(8) << cop0_epc << "\n";
        std::cout << "--- End Context Dump ---\n";
        std::cout.flags(flags); // Restore format flags
    }

    ~R5900Context() = default;
};

inline uint32_t getRegU32(const R5900Context *ctx, int reg)
{
    // Check if reg is valid (0-31)
    if (reg < 0 || reg > 31)
        return 0;
    if (reg == 0)
        return 0;
    return static_cast<uint32_t>(_mm_extract_epi32(ctx->r[reg], 0));
}

inline void setReturnU32(R5900Context *ctx, uint32_t value)
{
    // R5900 sign-extends 32-bit results into 64-bit GPR, even for unsigned values.
    ctx->r[2] = _mm_set_epi64x(0, static_cast<int64_t>(static_cast<int32_t>(value))); // $v0
}

inline void setReturnS32(R5900Context *ctx, int32_t value)
{
    // Signed 32-bit return should be sign-extended when observed as 64-bit.
    ctx->r[2] = _mm_set_epi64x(0, static_cast<int64_t>(value)); // $v0
}

inline void setReturnU64(R5900Context *ctx, uint64_t value)
{
    // Keep both conventions: full 64-bit value in $v0 and high 32-bit in $v1.
    ctx->r[2] = _mm_set_epi64x(0, static_cast<int64_t>(value));
    ctx->r[3] = _mm_set_epi64x(0, static_cast<int64_t>(static_cast<uint32_t>(value >> 32)));
}

inline constexpr uint32_t PS2_PATH_WATCH_ADDR = 0x01EFFFA0u;
inline constexpr uint32_t PS2_PATH_WATCH_BYTES = 0x200u;

inline uint32_t ps2PathWatchPhysAddr()
{
    return PS2_PATH_WATCH_ADDR & PS2_RAM_MASK;
}

inline uint8_t ps2PathWatchExtractByteFromWrite(uint32_t writeAddr, uint32_t watchAddr, uint64_t valueLo, uint64_t valueHi)
{
    const uint32_t byteIndex = watchAddr - writeAddr;
    if (byteIndex < 8u)
    {
        return static_cast<uint8_t>((valueLo >> (byteIndex * 8u)) & 0xFFu);
    }
    return static_cast<uint8_t>((valueHi >> ((byteIndex - 8u) * 8u)) & 0xFFu);
}

inline void ps2TraceGuestWrite(uint8_t *rdram,
                               uint32_t guestAddr,
                               uint32_t size,
                               uint64_t valueLo,
                               uint64_t valueHi,
                               const char *op,
                               const R5900Context *ctx)
{
    (void)rdram;
    (void)guestAddr;
    (void)size;
    (void)valueLo;
    (void)valueHi;
    (void)op;
    (void)ctx;
    // TODO we dont need this anymore so on next release it will be deleted
}

inline void ps2TraceGuestRangeWrite(uint8_t *rdram,
                                    uint32_t guestAddr,
                                    uint32_t size,
                                    const char *op,
                                    const R5900Context *ctx)
{
    (void)rdram;
    (void)guestAddr;
    (void)size;
    (void)op;
    (void)ctx;
    // TODO we dont need this anymore so on next release it will be deleted
}

class PS2Runtime
{
public:
    struct IoPaths
    {
        std::filesystem::path elfPath;
        std::filesystem::path elfDirectory;
        std::filesystem::path hostRoot;
        std::filesystem::path cdRoot;
        std::filesystem::path mcRoot;
        std::filesystem::path cdImage;
    };

    PS2Runtime();
    ~PS2Runtime();

    bool initialize(const char *title = "PS2 Game");
    bool syncCoreSubsystems();
    bool loadELF(const std::string &elfPath);
    void run();

    // Headless mode: no window, no audio device and no frame presentation. run() waits for the
    // game thread, or stops it once the time limit (seconds, 0 = none) has passed.
    void setHeadless(bool headless, double runTimeLimitSeconds = 0.0);

    // Guest RAM used for interrupt/callback invocation stacks (default: carved down from the top of
    // RAM). Games whose main thread stack sits at the top of RAM need it moved. Call after loadELF().
    void setAsyncCallbackStackRegion(uint32_t floor, uint32_t top);
    bool isHeadless() const { return m_headless; }

    // COP0 register 25: PCCR (MTPS/MFPS, select 0) and the counters PCR0 (select 1) and PCR1
    // (select 3). Counters run on the scheduler's guest EE cycle clock while PCCR.CTE is set and
    // their event is "processor cycle" (1) in any enabled mode; other events are not modelled.
    uint32_t readPerfCounter(uint32_t select);
    void writePerfCounter(uint32_t select, uint32_t value);

    // Observes every slow-path guest access (I/O registers, GS privileged registers, scratchpad,
    // uncached RAM mirrors). Called before the access is performed.
    using MmioTraceHook = void (*)(bool isWrite, uint32_t address, uint32_t size,
                                   uint64_t lo, uint64_t hi, const R5900Context *ctx);
    static void setMmioTraceHook(MmioTraceHook hook);

    [[nodiscard]] ps2x::iop::ModuleLoadResult loadIopModule(std::string_view path, const void *arguments = nullptr, uint32_t argumentSize = 0);
    [[nodiscard]] ps2x::iop::ModuleLoadResult loadIopModuleBuffer(uint32_t guestAddress, const void *arguments = nullptr, uint32_t argumentSize = 0);
    [[nodiscard]] bool stopIopModule(int32_t moduleId, int32_t *result = nullptr);
    [[nodiscard]] ps2x::iop::DebugSnapshot iopDebugSnapshot() const;
    [[nodiscard]] bool canBindIopRpc(uint32_t sid) const noexcept;
    [[nodiscard]] ps2x::iop::RpcResult handleIopRpc(uint8_t *rdram, R5900Context *ctx, ps2x::iop::RpcRequest request);
    uint32_t allocateIopMemory(uint32_t size, uint32_t alignment = 16u);
    bool freeIopMemory(uint32_t address);
    bool readIopMemory(uint32_t address, void *destination, size_t size) const;
    bool writeIopMemory(uint32_t address, const void *source, size_t size);
    bool zeroIopMemory(uint32_t address, size_t size);
    bool isIopMemoryRange(uint32_t address, size_t size) const;
    // An EE sceSdRemote call (LIBSD, SID 0x80000701) run by the emulated IOP's libsd and SPU2; false if unsupported.
    bool iopSdRemote(uint32_t function, const std::array<uint32_t, 5> &args, uint32_t &result);

    using DebugUiCallback = void (*)(PS2Runtime &runtime, void *userData);
    void setDebugUiCallbacks(DebugUiCallback initCallback,
                             DebugUiCallback drawCallback,
                             DebugUiCallback shutdownCallback,
                             void *userData);

    using RecompiledFunction = void (*)(uint8_t *, R5900Context *, PS2Runtime *);

    enum class GuestBranchKind
    {
        DirectJump,
        DirectCall,
        IndirectJump,
        IndirectCall,
        Return,
    };

    enum class MissingFunctionPolicy : uint32_t
    {
        // Strict mode for tests/CI: log the bad target and request the runtime to stop.
        Stop = 0,

        // Debug mode: log once, leave ctx->pc on the bad target, and let the caller unwind.
        ContinueToTarget = 1,

        // Debug mode: same as ContinueToTarget, but triggers a debugger break once on MSVC.
        BreakOnce = 2,

        // Escape hatch only: skip missing calls by returning to fallthrough (it can hide guest bugs)
        SkipCallDebug = 3,
    };

    bool replaceFunction(uint32_t address, RecompiledFunction func);
    // TODO remove this later need to update all tests
    bool registerFunction(uint32_t address, RecompiledFunction func);
    RecompiledFunction lookupFunction(uint32_t address);
    bool hasFunction(uint32_t address) const;
    bool dispatchGuestBranch(uint8_t *rdram,
                             R5900Context *ctx,
                             uint32_t targetPc,
                             uint32_t sourcePc,
                             uint32_t fallthroughPc,
                             GuestBranchKind kind,
                             const char *debugName);
    void reportMissingFunction(uint8_t *rdram,
                               R5900Context *ctx,
                               uint32_t targetPc,
                               uint32_t sourcePc,
                               GuestBranchKind kind,
                               const char *debugName);
    void setMissingFunctionPolicy(MissingFunctionPolicy policy);
    MissingFunctionPolicy missingFunctionPolicy() const;
    void resetMissingFunctionReportOnce();

    static const IoPaths &getIoPaths();
    static void setIoPaths(const IoPaths &paths);
    // Pins the memory card root (call before loadELF): loadELF otherwise puts the cards in
    // <ELF directory>/mc0, next to the game's files.
    static void setMemoryCardRoot(const std::filesystem::path &root);
    static void configureIoPathsFromElf(const std::string &elfPath);

    void SignalException(R5900Context *ctx, PS2Exception exception);

    void executeVU0Microprogram(uint8_t *rdram, R5900Context *ctx, uint32_t address);
    void vu0StartMicroProgram(uint8_t *rdram, R5900Context *ctx, uint32_t address);

public:
    void handleSyscall(uint8_t *rdram, R5900Context *ctx);
    void handleSyscall(uint8_t *rdram, R5900Context *ctx, uint32_t encodedSyscallId);
    void handleBreak(uint8_t *rdram, R5900Context *ctx);

    void handleTrap(uint8_t *rdram, R5900Context *ctx);
    void handleTLBR(uint8_t *rdram, R5900Context *ctx);
    void handleTLBWI(uint8_t *rdram, R5900Context *ctx);
    void handleTLBWR(uint8_t *rdram, R5900Context *ctx);
    void handleTLBP(uint8_t *rdram, R5900Context *ctx);
    void clearLLBit(R5900Context *ctx);
    void configureGuestHeap(uint32_t guestBase, uint32_t guestLimit = PS2_RAM_SIZE);
    uint32_t guestMalloc(uint32_t size, uint32_t alignment = 16u);
    uint32_t guestCalloc(uint32_t count, uint32_t size, uint32_t alignment = 16u);
    uint32_t guestRealloc(uint32_t guestAddr, uint32_t newSize, uint32_t alignment = 16u);
    void guestFree(uint32_t guestAddr);
    uint32_t guestHeapBase() const;
    uint32_t guestHeapEnd() const;
    uint32_t guestHeapLimit() const;
    // Extended RAM (only when PS2_RAM_SIZE is above the retail 32MB): a main thread stack that SetupThread is given
    // at the very top of the retail 32MB is moved to the top of the larger RAM, so a heap the game sizes up to its
    // stack (SetupHeap(start, -1)) takes the extra space. Set before the game starts.
    void setExtendedRam(bool on) { m_extendedRam = on && PS2_RAM_SIZE > PS2_RETAIL_RAM_SIZE; }
    bool extendedRam() const { return m_extendedRam; }
    uint32_t reserveAsyncCallbackStack(uint32_t size, uint32_t alignment = 16u);

    void drainCompletedDmacHandlers(uint8_t *rdram);

    void requestStop();
    bool isStopRequested() const;

    EeScheduler &eeScheduler();
    const EeScheduler &eeScheduler() const;
    void postEeEvent(EeEvent event);
    bool eeCheckpointDue(uint32_t cycles = 32u) noexcept;
    [[noreturn]] void eeWaitVSyncTicks(uint32_t ticks, uint32_t resumePc);

    struct EeExitHandlerRegistration
    {
        uint32_t function = 0;
        uint32_t argument = 0;
    };
    void addEeExitHandler(int threadId, uint32_t function, uint32_t argument);
    std::vector<EeExitHandlerRegistration> takeEeExitHandlers(int threadId);
    void removeEeExitHandlers(int threadId);
    bool findEeSyscallOverride(uint32_t syscallNumber, uint32_t &handler) const;
    void setEeSyscallOverride(uint8_t *rdram, uint32_t syscallNumber, uint32_t handler);
    void initializeEeKernelState(uint8_t *rdram);

    uint8_t Load8(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr);
    uint16_t Load16(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr);
    uint32_t Load32(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr);
    uint64_t Load64(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr);
    __m128i Load128(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr);

    void Store8(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint8_t value);
    void Store16(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint16_t value);
    void Store32(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint32_t value);
    void Store64(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint64_t value);
    void Store128(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, __m128i value);
    void kickGifDmaChainFromMMIO(uint8_t *rdram,
                                 R5900Context *ctx,
                                 uint32_t dPcrValue,
                                 uint32_t dStatValue,
                                 uint32_t tadr,
                                 uint32_t chcr);

    static inline bool isSpecialAddress(uint32_t addr)
    {
        return Ps2IsSpecialAddress(addr);
    }

public:
    inline R5900Context &cpu() { return m_cpuContext; }
    inline const R5900Context &cpu() const { return m_cpuContext; }

    inline PS2Memory &memory() { return m_memory; }
    inline const PS2Memory &memory() const { return m_memory; }

    inline GS &gs() { return m_gs; }
    inline const GS &gs() const { return m_gs; }
    inline GifArbiter &gifArbiter() { return m_gifArbiter; }
    inline const GifArbiter &gifArbiter() const { return m_gifArbiter; }

    // Optional replacement for the CPU GS as the source of window frames (e.g. a GPU GS device). Called by the
    // presenter on the main thread; returns true and fills RGBA pixels when a frame newer than the last call is
    // ready, false to keep showing the previous one. Unset (the default), the CPU GS presentation is used.
    using HostFrameSource = std::function<bool(std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height)>;
    void setHostFrameSource(HostFrameSource source) { m_hostFrameSource = std::move(source); }
    const HostFrameSource &hostFrameSource() const { return m_hostFrameSource; }

    // Optional consumer of the emulated IOP's mixed SPU2 output: interleaved signed 16-bit L/R at 48 kHz, in guest
    // time order, called from the thread that runs the EE. Unset (the default), the output is discarded.
    using IopAudioSink = std::function<void(const int16_t *samples, size_t frames)>;
    void setIopAudioSink(IopAudioSink sink) { m_iopAudioSink = std::move(sink); }
    const IopAudioSink &iopAudioSink() const { return m_iopAudioSink; }
    // Plays the emulated IOP's SPU2 output on the host audio device (windowed runs: the device opens with the
    // window). The output is buffered about 60 ms behind the guest.
    void setIopAudioPlayback(bool enabled) { m_iopAudioPlayback = enabled; }
    // What times the IOP while its audio plays on the host.
    // - Lockstep (the default): EE time / 8, exactly as without playback. The audio device paces the EE instead
    //   (iopAudioPacing), so a guest that falls behind real time runs its audio dry.
    // - RealTime: the audio device's clock. The IOP runs, all threads and timers included, up to where the device has
    //   played plus the playback ring's target, so it keeps up when the EE is slow and never runs ahead of the device.
    //   The EE keeps its own field-rate pacing. While the device is not consuming (not started, or stalled for longer
    //   than 200 ms), the IOP falls back to EE time.
    enum class IopAudioClock
    {
        Lockstep,
        RealTime
    };
    void setIopAudioClock(IopAudioClock clock) { m_iopAudioClock = clock; }
    [[nodiscard]] IopAudioClock iopAudioClock() const { return m_iopAudioClock; }
    void playIopAudio(const int16_t *samples, size_t frames);
    // Host playback counters since the previous call (frames are 48 kHz stereo frames).
    struct IopAudioStats
    {
        uint64_t callbacks = 0;
        uint64_t framesRequested = 0; // by the audio device
        uint64_t silentFrames = 0;    // of those, played as silence (not started yet, or the ring ran dry)
        uint64_t framesQueued = 0;    // produced by the SPU2
        uint64_t droppedFrames = 0;
        uint64_t underruns = 0;
        uint64_t minBuffered = 0;
        uint64_t maxBuffered = 0;
        uint64_t buffered = 0;
        uint64_t maxCallbackGapUs = 0;
        int32_t pacingPpm = 0;
        bool realTime = false;            // the IOP followed the device clock at the end of the window
        uint64_t realTimeTicks = 0;       // device-clock IOP updates
        uint64_t realTimeFallbacks = 0;   // updates that fell back to EE time (device not consuming)
        uint64_t realTimeAnchors = 0;     // times the IOP clock was (re)attached to the device clock
    };
    IopAudioStats takeIopAudioStats();
    // While host playback runs: true, with the VBlank pacing correction (parts per million of the period; positive
    // slows the guest) that keeps the playback ring at its target fill, and whether lost time should be made up
    // (the ring is below its target).
    bool iopAudioPacing(int32_t &ppm, bool &catchUp) const;
    [[nodiscard]] uint64_t iopAudioUnderruns() const;
    [[nodiscard]] uint64_t iopAudioDroppedFrames() const;
    inline VU1Interpreter &vu0() { return m_vu0; }
    inline const VU1Interpreter &vu0() const { return m_vu0; }
    inline VU1Interpreter &vu1() { return m_vu1; }
    inline const VU1Interpreter &vu1() const { return m_vu1; }

    inline PS2AudioBackend &audioBackend() { return m_audioBackend; }
    inline const PS2AudioBackend &audioBackend() const { return m_audioBackend; }
    inline PSPadBackend &padBackend() { return m_padBackend; }
    inline const PSPadBackend &padBackend() const { return m_padBackend; }
    inline PS2RomDevice &romDevice() { return m_romDevice; }
    inline const PS2RomDevice &romDevice() const { return m_romDevice; }
    inline PS2Vfs &vfs() { return m_vfs; }
    inline const PS2Vfs &vfs() const { return m_vfs; }

private:
    struct GuestHeapBlock
    {
        uint32_t addr = 0;
        uint32_t size = 0;
        bool free = true;
    };

    static uint32_t alignGuestHeapValue(uint32_t value, uint32_t alignment);
    static bool isGuestHeapAlignmentValid(uint32_t alignment);
    static uint32_t normalizeGuestHeapAlignment(uint32_t alignment);
    uint32_t clampGuestHeapBase(uint32_t guestBase) const;
    uint32_t clampGuestHeapLimit(uint32_t guestLimit) const;
    void resetGuestHeapLocked(uint32_t guestBase, uint32_t guestLimit);
    void ensureGuestHeapInitializedLocked();
    int32_t findGuestHeapBlockIndexLocked(uint32_t guestAddr) const;
    uint32_t allocateGuestBlockLocked(uint32_t size, uint32_t alignment);
    void freeGuestBlockLocked(uint32_t guestAddr);
    void coalesceGuestHeapLocked();
    void HandleIntegerOverflow(R5900Context *ctx);
    void runHeadless();
    // PS2X_VU1_THREAD=1: VU1 and the GIF/GS back end on a worker thread for this run (ps2_vu1_thread.h).
    void startVu1ThreadIfRequested(bool gsWorker);
    void stopVu1Thread();

    [[nodiscard]] ps2x::iop::RpcAbi selectIopRpcAbi(const ps2x::iop::RpcAbiRequest &request) const;
    void notifyIopSifTransfer(uint8_t *rdram, const ps2x::iop::SifTransfer &transfer);
    // Called for every EE checkpoint. While the IOP has nothing due, the cycles are only counted here and handed over
    // when it has (or before any other call into the IOP), which leaves it in the same state at a fraction of the cost.
    void advanceIopEeCycles(uint64_t eeCycles) noexcept
    {
        if (iopOnRealTimeClock())
        {
            m_iopRealTimeEeCycles += eeCycles;
            if (m_iopRealTimeEeCycles >= kIopRealTimeTickEeCycles)
                runIopRealTime();
            return;
        }
        m_iopHeldEeCycles += eeCycles;
        if (m_iopHeldEeCycles >= m_iopIdleEeCycles)
            runIopUntilIdle();
    }
    void runIopUntilIdle() noexcept;
    void runHeldIopCycles() noexcept;
    // The RealTime clock is in effect, unless the SPU2 plays a stream the EE feeds (the movie soundtrack through the
    // sound data input): the EE writes that stream as it plays, so the IOP then follows EE time as in Lockstep, and
    // the audio device paces the EE, which keeps picture and sound together when the EE falls behind.
    [[nodiscard]] bool iopOnRealTimeClock() const noexcept
    {
        return m_iopAudioClock == IopAudioClock::RealTime && m_iopAudioPlayback && !m_iopGuestFedAudio;
    }
    void setIopGuestFedAudio(bool fed) noexcept;
    // RealTime clock: brings the IOP up to the audio device (or, while the device is not consuming, hands it the EE
    // cycles since the previous update).
    void runIopRealTime() noexcept;
    // One update per 64 SPU2 samples of EE time (1.3 ms at full speed), the SPU2's own sync granularity.
    static constexpr uint64_t kIopRealTimeTickEeCycles = 64u * 768u * 8u;
    void resetIop();

    friend class PS2IopTransport;
    friend class EeScheduler;

private:
    PS2Memory m_memory;
    GifArbiter m_gifArbiter;
    HostFrameSource m_hostFrameSource;
    IopAudioSink m_iopAudioSink;
    bool m_iopAudioPlayback = false;
    GS m_gs;
    std::unique_ptr<PS2IopHostAdapter> m_iopHost;
    std::unique_ptr<ps2x::iop::IopSubsystem> m_iopSubsystem;
    uint64_t m_iopHeldEeCycles = 0;  // EE cycles not yet handed to the IOP
    uint64_t m_iopIdleEeCycles = 0;  // the IOP has nothing due before it is handed this many
    IopAudioClock m_iopAudioClock = IopAudioClock::Lockstep;
    uint64_t m_iopRealTimeEeCycles = 0;   // EE cycles since the last RealTime update
    bool m_iopGuestFedAudio = false;       // the SPU2's sound data input is running (see iopOnRealTimeClock)
    bool m_iopRealTimeAnchored = false;   // the anchor below is valid
    uint64_t m_iopRealTimeAnchorCycle = 0; // IOP cycle at which the ring had been handed m_iopRealTimeAnchorFrame frames
    uint64_t m_iopRealTimeAnchorFrame = 0;
    PS2AudioBackend m_audioBackend;
    PSPadBackend m_padBackend;
    PS2RomDevice m_romDevice;
    PS2Vfs m_vfs;
    VU1Interpreter m_vu0{VU1Interpreter::Unit::VU0};
    VU1Interpreter m_vu1{VU1Interpreter::Unit::VU1};
    R5900Context m_cpuContext;
    std::unique_ptr<EeScheduler> m_eeScheduler;
    mutable std::mutex m_eeKernelStateMutex;
    std::unordered_map<int, std::vector<EeExitHandlerRegistration>> m_eeExitHandlers;
    std::unordered_map<uint32_t, uint32_t> m_eeSyscallOverrides;
    std::unordered_set<uint32_t> m_eeSyscallMirrorAddresses;
    mutable std::mutex m_guestHeapMutex;
    mutable std::mutex m_asyncCallbackStackMutex;
    std::vector<GuestHeapBlock> m_guestHeapBlocks;
    uint32_t m_guestHeapBase = 0x00100000u;
    uint32_t m_guestHeapEnd = 0x00100000u;
    uint32_t m_guestHeapLimit = PS2_RAM_SIZE;
    uint32_t m_guestHeapSuggestedBase = 0x00100000u;
    bool m_guestHeapConfigured = false;
    uint32_t m_asyncCallbackStackFloor = PS2_RUNTIME_RESERVED_BASE;
    bool m_extendedRam = false;
    uint32_t m_asyncCallbackStackTop = PS2_RAM_SIZE;

    // Performance counters (see readPerfCounter): values as of m_perfCycleBase.
    uint32_t m_perfPccr = 0u;
    uint32_t m_perfPcr[2] = {0u, 0u};
    uint64_t m_perfCycleBase = 0u;
    bool m_perfUnmodelledEventReported = false;
    void foldPerfCounters();

    std::atomic<uint32_t> m_missingFunctionPolicy{static_cast<uint32_t>(MissingFunctionPolicy::ContinueToTarget)};
    std::atomic<bool> m_missingFunctionReported{false};
    std::atomic<bool> m_stopRequested{false};
    DebugUiCallback m_debugUiInitCallback = nullptr;
    DebugUiCallback m_debugUiDrawCallback = nullptr;
    DebugUiCallback m_debugUiShutdownCallback = nullptr;
    void *m_debugUiUserData = nullptr;
    bool m_debugUiInitialized = false;
    bool m_headless = false;
    double m_runTimeLimitSeconds = 0.0;

public:
    std::atomic<uint32_t> m_debugPc{0};
    std::atomic<uint32_t> m_debugRa{0};
    std::atomic<uint32_t> m_debugSp{0};
    std::atomic<uint32_t> m_debugGp{0};

private:
    struct LoadedModule
    {
        std::string name;
        uint32_t baseAddress;
        size_t size;
        bool active;
    };

    std::vector<LoadedModule> m_loadedModules;
    uint8_t *m_boundRdram = nullptr;
    uint8_t *m_boundGSVram = nullptr;
};

// Generated by ps2xRecomp in ps2xRuntime/src/runner/register_functions.cpp.
extern const uint32_t g_ps2RecompiledFunctionTableBase;
extern const uint32_t g_ps2RecompiledFunctionTableEnd;
extern const uint32_t g_ps2RecompiledFunctionTableSlotCount;
extern PS2Runtime::RecompiledFunction g_ps2RecompiledFunctionTable[];

#endif // PS2_RUNTIME_H
