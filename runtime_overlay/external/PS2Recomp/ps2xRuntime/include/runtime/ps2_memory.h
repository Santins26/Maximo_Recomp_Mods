#ifndef PS2_MEMORY_H
#define PS2_MEMORY_H

#include <cstddef>
#include <cstdint>
#include <algorithm>
#include <array>
#include <functional>
#include <vector>
#include <memory>
#include <unordered_map>
#include <atomic>
#include <iostream>
#include <mutex>

#include "gs/ps2_gif_arbiter.h"
#if defined(_MSC_VER)
#include <intrin.h>
#elif defined(USE_SSE2NEON)
#include "sse2neon.h"
#else
#include <immintrin.h> // For SSE/AVX instructions
#include <smmintrin.h> // For SSE4.1 instructions
#endif

class GS;
class Vu1Thread;

// Size of the emulated EE main RAM. A retail PS2 has 32MB; this branch defaults to 128MB so a host can give a game a
// larger heap (PS2Runtime::setExtendedRam). A game that is not given it only ever touches the low 32MB, so it runs
// the same either way. Build with PS2_RAM_SIZE_MB=32 for the exact retail size. A power of two, at most 128 (above
// that, RAM would reach the I/O and VU windows at 0x10000000 once mirrors are masked).
#ifndef PS2_RAM_SIZE_MB
#define PS2_RAM_SIZE_MB 128
#endif
constexpr uint32_t PS2_RAM_SIZE = static_cast<uint32_t>(PS2_RAM_SIZE_MB) * 1024u * 1024u;
constexpr uint32_t PS2_RAM_MASK = PS2_RAM_SIZE - 1u;
constexpr uint32_t PS2_RETAIL_RAM_SIZE = 32u * 1024u * 1024u;
// The runtime keeps its own guest-side pools (SIF RPC packets and servers, TLS, boot modes) and the default heap end
// in the top megabyte of RAM: 0x1F00000 with 32MB, 0x7F00000 with 128MB.
constexpr uint32_t PS2_RUNTIME_RESERVED_BASE = PS2_RAM_SIZE - 0x00100000u;
constexpr uint32_t PS2_RAM_BASE = 0x00000000;          // Physical base of RDRAM
constexpr uint32_t PS2_SCRATCHPAD_BASE = 0x70000000;
constexpr uint32_t PS2_SCRATCHPAD_ALIAS_BASE = 0xF0000000;
constexpr uint32_t PS2_SCRATCHPAD_SIZE = 16u * 1024u;  // 16KB
constexpr uint32_t PS2_IO_BASE = 0x10000000;           // Base for many I/O regs (Timers, DMAC, INTC)
constexpr uint32_t PS2_IO_SIZE = 0x10000;              // 64KB
constexpr uint32_t PS2_BIOS_BASE = 0x1FC00000;         // Or BFC00000 depending on KSEG
constexpr uint32_t PS2_BIOS_SIZE = 4u * 1024u * 1024u; // 4MB

constexpr uint32_t PS2_VU0_CODE_BASE = 0x11000000; // Base address as seen from EE
constexpr uint32_t PS2_VU0_DATA_BASE = 0x11004000;
constexpr uint32_t PS2_VU0_CODE_SIZE = 4u * 1024u; // 4KB Micro Memory
constexpr uint32_t PS2_VU0_DATA_SIZE = 4u * 1024u; // 4KB Data Memory (VU Mem)

constexpr uint32_t PS2_VU1_CODE_BASE = 0x11008000;
constexpr uint32_t PS2_VU1_DATA_BASE = 0x1100C000;
constexpr uint32_t PS2_VU1_MEM_BASE = PS2_VU1_CODE_BASE; // Alias used by older code paths
constexpr uint32_t PS2_VU1_CODE_SIZE = 16u * 1024u;      // 16KB Micro Memory
constexpr uint32_t PS2_VU1_DATA_SIZE = 16u * 1024u;      // 16KB Data Memory (VU Mem)

constexpr uint32_t PS2_GS_BASE = 0x12000000;
constexpr uint32_t PS2_GS_PRIV_REG_BASE = PS2_GS_BASE; // GS Privileged Registers
constexpr uint32_t PS2_GS_PRIV_REG_SIZE = 0x2000;
constexpr size_t PS2_GS_VRAM_SIZE = 4u * 1024u * 1024u; // 4MB GS VRAM

inline constexpr uint32_t PS2_FIO_O_RDONLY = 0x0001;
inline constexpr uint32_t PS2_FIO_O_WRONLY = 0x0002;
inline constexpr uint32_t PS2_FIO_O_RDWR = 0x0003;
inline constexpr uint32_t PS2_FIO_O_NBLOCK = 0x0010;
inline constexpr uint32_t PS2_FIO_O_APPEND = 0x0100;
inline constexpr uint32_t PS2_FIO_O_CREAT = 0x0200;
inline constexpr uint32_t PS2_FIO_O_TRUNC = 0x0400;
inline constexpr uint32_t PS2_FIO_O_EXCL = 0x0800;
inline constexpr uint32_t PS2_FIO_O_NOWAIT = 0x8000;

inline constexpr uint32_t PS2_FIO_SEEK_SET = 0;
inline constexpr uint32_t PS2_FIO_SEEK_CUR = 1;
inline constexpr uint32_t PS2_FIO_SEEK_END = 2;

inline constexpr uint32_t PS2_FIO_S_IFDIR = 0x1000;
inline constexpr uint32_t PS2_FIO_S_IFREG = 0x2000;

static_assert((PS2_RAM_SIZE & (PS2_RAM_SIZE - 1u)) == 0u, "PS2_RAM_SIZE must be a power of two");
static_assert(PS2_RAM_MASK == (PS2_RAM_SIZE - 1u), "PS2_RAM_MASK must match PS2_RAM_SIZE");
static_assert(PS2_RAM_SIZE >= PS2_RETAIL_RAM_SIZE && PS2_RAM_SIZE <= 128u * 1024u * 1024u,
              "PS2_RAM_SIZE must be 32..128MB");

inline std::atomic<uint8_t *> &ps2ScratchpadHostPtrStorage()
{
    static std::atomic<uint8_t *> ptr{nullptr};
    return ptr;
}

inline void ps2SetScratchpadHostPtr(uint8_t *ptr)
{
    ps2ScratchpadHostPtrStorage().store(ptr, std::memory_order_relaxed);
}

inline uint8_t *ps2GetScratchpadHostPtr()
{
    return ps2ScratchpadHostPtrStorage().load(std::memory_order_relaxed);
}

inline bool ps2IsScratchpadAddress(uint32_t addr)
{
    if (addr >= PS2_SCRATCHPAD_BASE && addr < (PS2_SCRATCHPAD_BASE + PS2_SCRATCHPAD_SIZE))
    {
        return true;
    }

    if ((addr & 0x80000000u) != 0u)
    {
        const uint32_t lower = addr & 0x7FFFFFFFu;
        return lower >= PS2_SCRATCHPAD_BASE &&
               lower < (PS2_SCRATCHPAD_BASE + PS2_SCRATCHPAD_SIZE);
    }

    return false;
}

inline uint32_t ps2ScratchpadOffset(uint32_t addr)
{
    if (addr >= PS2_SCRATCHPAD_BASE && addr < (PS2_SCRATCHPAD_BASE + PS2_SCRATCHPAD_SIZE))
    {
        return addr - PS2_SCRATCHPAD_BASE;
    }

    const uint32_t lower = addr & 0x7FFFFFFFu;
    return lower - PS2_SCRATCHPAD_BASE;
}

inline bool ps2ResolveGuestPointer(uint32_t addr, uint32_t &offset, bool &scratch)
{
    if (ps2IsScratchpadAddress(addr))
    {
        scratch = true;
        offset = ps2ScratchpadOffset(addr);
        return true;
    }

    uint32_t phys = 0;
    if (addr < 0x20000000u)
    {
        phys = addr;
    }
    else if ((addr >= 0x20000000u && addr < 0x40000000u) ||
             (addr >= 0x80000000u && addr < 0xC0000000u))
    {
        phys = addr & 0x1FFFFFFFu;
    }

    if (phys >= PS2_RAM_SIZE)
    {
        phys &= PS2_RAM_MASK;
    }

    scratch = false;
    offset = phys;
    return true;
}
inline uint8_t *getMemPtr(uint8_t *rdram, uint32_t addr)
{
    if (rdram == nullptr)
    {
        return nullptr;
    }

    uint32_t offset = 0;
    bool scratch = false;
    if (!ps2ResolveGuestPointer(addr, offset, scratch))
    {
        return nullptr;
    }

    if (scratch)
    {
        uint8_t *scratchpad = ps2GetScratchpadHostPtr();
        return scratchpad ? (scratchpad + offset) : nullptr;
    }
    return rdram + offset;
}

inline const uint8_t *getConstMemPtr(const uint8_t *rdram, uint32_t addr)
{
    if (rdram == nullptr)
    {
        return nullptr;
    }

    uint32_t offset = 0;
    bool scratch = false;
    if (!ps2ResolveGuestPointer(addr, offset, scratch))
    {
        return nullptr;
    }

    if (scratch)
    {
        const uint8_t *scratchpad = ps2GetScratchpadHostPtr();
        return scratchpad ? (scratchpad + offset) : nullptr;
    }
    return rdram + offset;
}

// PS2 GS (Graphics Synthesizer) registers
struct GSRegisters
{
    uint64_t pmode;    // Pixel mode
    uint64_t smode1;   // Sync mode 1
    uint64_t smode2;   // Sync mode 2
    uint64_t srfsh;    // Refresh control
    uint64_t synch1;   // Synchronization control 1
    uint64_t synch2;   // Synchronization control 2
    uint64_t syncv;    // Synchronization control V
    uint64_t dispfb1;  // Display buffer 1
    uint64_t display1; // Display area 1
    uint64_t dispfb2;  // Display buffer 2
    uint64_t display2; // Display area 2
    uint64_t extbuf;   // External buffer
    uint64_t extdata;  // External data
    uint64_t extwrite; // External write
    uint64_t bgcolor;  // Background color
    std::atomic<uint64_t> csr;
    std::atomic<uint64_t> vsyncTick;
    uint64_t imr;      // Interrupt mask
    uint64_t busdir;   // Bus direction
    uint64_t siglblid; // Signal label ID
};
static_assert(sizeof(GSRegisters) == (20u * sizeof(uint64_t)), "GSRegisters layout changed unexpectedly");
static_assert(alignof(GSRegisters) == alignof(uint64_t), "GSRegisters alignment must remain 64-bit");
static_assert(std::atomic<uint64_t>::is_always_lock_free, "GS CSR atomic must be lock-free on all supported targets");

// PS2 VIF (VPU Interface) registers
bool vif1IrqTraceEnabled(); // PS2_VIF1_IRQ_TRACE set: log VIF1 i-bit stalls and STC resumes

struct VIFRegisters
{
    uint32_t stat;   // Status
    uint32_t fbrst;  // VIF Force Break
    uint32_t err;    // Error status
    uint32_t mark;   // Interrupt control
    uint32_t cycle;  // Transfer mode
    uint32_t mode;   // Mode control
    uint32_t num;    // Data amount counter
    uint32_t mask;   // Data mask
    uint32_t code;   // VIFcode
    uint32_t itops;  // ITOP save
    uint32_t base;   // Base address
    uint32_t ofst;   // Offset
    uint32_t tops;   // TOPS
    uint32_t itop;   // ITOP
    uint32_t top;    // TOP
    uint32_t row[4]; // Transfer row data
    uint32_t col[4]; // Transfer column data
};
static_assert(sizeof(VIFRegisters) == (23u * sizeof(uint32_t)), "VIFRegisters layout changed unexpectedly");

// PS2 DMA registers
struct DMARegisters
{
    uint32_t chcr; // Channel control
    uint32_t madr; // Memory address
    uint32_t qwc;  // Quadword count
    uint32_t tadr; // Tag address
    uint32_t asr0; // Address stack 0
    uint32_t asr1; // Address stack 1
    uint32_t sadr; // Source address
};
static_assert(sizeof(DMARegisters) == (7u * sizeof(uint32_t)), "DMARegisters layout changed unexpectedly");

struct JumpTable
{
    uint32_t address = 0;          // Base address of the jump table
    uint32_t baseRegister = 0;     // Register used for index
    std::vector<uint32_t> targets; // Jump targets
};

class Ps2Ipu;

// EE hardware register file. Behaves like std::unordered_map<uint32_t, uint32_t> for the operations the runtime uses
// (operator[], find, count, end, clear) - a register "exists" once written or referenced through operator[] - but
// word-aligned registers in 0x10000000-0x1000FFFF live in a flat table, since the EE polls them constantly.
class IoRegisterFile
{
public:
    struct Entry
    {
        uint32_t first = 0;
        uint32_t second = 0;
    };

    IoRegisterFile() : m_table(kTableSize), m_present(kTableSize, 0u) {}

    uint32_t &operator[](uint32_t address)
    {
        if (Entry *entry = tableEntry(address))
        {
            const size_t index = static_cast<size_t>(entry - m_table.data());
            if (!m_present[index])
            {
                m_present[index] = 1u;
                entry->first = address;
                entry->second = 0u;
            }
            return entry->second;
        }
        Entry &entry = m_other[address];
        entry.first = address;
        return entry.second;
    }
    Entry *find(uint32_t address)
    {
        if (Entry *entry = tableEntry(address))
            return m_present[static_cast<size_t>(entry - m_table.data())] ? entry : nullptr;
        const auto it = m_other.find(address);
        return it == m_other.end() ? nullptr : &it->second;
    }
    const Entry *find(uint32_t address) const { return const_cast<IoRegisterFile *>(this)->find(address); }
    size_t count(uint32_t address) const { return find(address) != nullptr ? 1u : 0u; }
    Entry *end() { return nullptr; }
    const Entry *end() const { return nullptr; }
    void clear()
    {
        std::fill(m_table.begin(), m_table.end(), Entry{});
        std::fill(m_present.begin(), m_present.end(), uint8_t{0});
        m_other.clear();
    }

private:
    static constexpr uint32_t kBase = 0x10000000u;
    static constexpr size_t kTableSize = 0x10000u / 4u;
    Entry *tableEntry(uint32_t address)
    {
        const uint32_t offset = address - kBase;
        if (offset >= 0x10000u || (offset & 3u) != 0u)
            return nullptr;
        return &m_table[offset >> 2];
    }
    std::vector<Entry> m_table;
    std::vector<uint8_t> m_present;
    std::unordered_map<uint32_t, Entry> m_other;
};

class PS2Memory
{
public:
    PS2Memory();
    ~PS2Memory();

    PS2Memory(const PS2Memory &) = delete;
    PS2Memory &operator=(const PS2Memory &) = delete;
    PS2Memory(PS2Memory &&) = delete;
    PS2Memory &operator=(PS2Memory &&) = delete;

    // Initialize memory
    bool initialize(size_t ramSize = PS2_RAM_SIZE);

    // Memory access methods
    uint8_t *getRDRAM() { return m_rdram; }
    uint8_t *getScratchpad() { return m_scratchpad; }
    uint8_t *getIOPRAM() { return iop_ram; }
    uint64_t dmaStartCount() const { return m_dmaStartCount.load(std::memory_order_relaxed); }
    uint64_t gifCopyCount() const { return m_gifCopyCount.load(std::memory_order_relaxed); }
    uint64_t gsWriteCount() const { return m_gsWriteCount.load(std::memory_order_relaxed); }
    uint64_t vifWriteCount() const { return m_vifWriteCount.load(std::memory_order_relaxed); }
    uint64_t getVU0CodeGeneration() const { return m_vu0CodeGeneration.load(std::memory_order_relaxed); }
    uint64_t getVU1CodeGeneration() const { return m_vu1CodeGeneration.load(std::memory_order_relaxed); }

    // Read/write memory
    uint8_t read8(uint32_t address);
    uint16_t read16(uint32_t address);
    uint32_t read32(uint32_t address);
    uint64_t read64(uint32_t address);
    __m128i read128(uint32_t address);

    void write8(uint32_t address, uint8_t value);
    void write16(uint32_t address, uint16_t value);
    void write32(uint32_t address, uint32_t value);
    void write64(uint32_t address, uint64_t value);
    void write128(uint32_t address, __m128i value);

    // TLB handling
    uint32_t translateAddress(uint32_t virtualAddress);
    bool tlbRead(uint32_t index, uint32_t &vpn, uint32_t &pfn, uint32_t &mask, bool &valid) const;
    bool tlbWrite(uint32_t index, uint32_t vpn, uint32_t pfn, uint32_t mask, bool valid);
    int32_t tlbProbe(uint32_t vpn) const;
    size_t tlbEntryCount() const { return m_tlbEntries.size(); }

    // Hardware register interface
    bool writeIORegister(uint32_t address, uint32_t value);
    uint32_t readIORegister(uint32_t address);

    // EE timers advance from the scheduler's emulated EE-cycle clock. The
    // returned mask uses bits 0..3 for newly raised TIM0..TIM3 interrupts.
    uint32_t advanceEeTimers(uint64_t eeCycles) noexcept;
    [[nodiscard]] uint64_t cyclesUntilNextEeTimerInterrupt() const noexcept;
    void resetEeTimers() noexcept;

    using GifPacketCallback = std::function<void(const uint8_t *, uint32_t)>;
    void setGifPacketCallback(GifPacketCallback cb) { m_gifPacketCallback = std::move(cb); }
    void setGifArbiter(GifArbiter *arbiter) { m_gifArbiter = arbiter; }

    using Vu1MscalCallback = std::function<void(uint32_t startPC, uint32_t top, uint32_t itop)>;
    void setVu1MscalCallback(Vu1MscalCallback cb) { m_vu1MscalCallback = std::move(cb); }
    const Vu1MscalCallback &vu1MscalCallback() const { return m_vu1MscalCallback; }
    using Vu1MscntCallback = std::function<void(uint32_t top, uint32_t itop)>;
    void setVu1MscntCallback(Vu1MscntCallback cb) { m_vu1MscntCallback = std::move(cb); }
    const Vu1MscntCallback &vu1MscntCallback() const { return m_vu1MscntCallback; }

    // PS2X_VU1_THREAD=1 (ps2_vu1_thread.h, VU1_THREAD_PLAN.md): VU1 programs, VU1 memory, the GIF arbiter and the GS
    // run on a worker thread; VIF1 parsing, VIF1 registers, stalls, interrupts and DMA completion stay on the game
    // thread. Off (the default), every path is the inline one.
    // needsInline: called at each VIF1 kick on the game thread; true makes the kick run inline (VU1 D/T stops on).
    // eeMscal: called on the game thread for each MSCAL/MSCNT the game-thread pass parses (the worker runs VU1).
    using Vu1NeedsInlineFn = std::function<bool()>;
    using Vu1EeMscalFn = std::function<void()>;
    void setVu1ThreadHooks(Vu1NeedsInlineFn needsInline, Vu1EeMscalFn eeMscal)
    {
        m_vu1NeedsInlineFn = std::move(needsInline);
        m_vu1EeMscalFn = std::move(eeMscal);
    }
    void startVu1Thread();
    void stopVu1Thread();
    bool vu1ThreadActive() const { return m_vu1Thread != nullptr; }
    Vu1Thread *vu1Thread() { return m_vu1Thread.get(); }
    // Game thread: wait until the worker has applied everything handed to it (no-op when inline or on the worker).
    // reason is a Vu1Thread::WaitReason.
    void vu1ThreadWait(uint32_t reason) const
    {
        if (m_vu1Thread)
            vu1ThreadWaitSlow(reason);
    }
    // Game thread, at a frame boundary (sceGsSyncV or a display flip): `present` hands the frame to the presenter.
    // Normally it waits for the worker to drain and then presents. With PS2X_VU1_PIPELINE=1 it queues `present`
    // behind the frame's GS work and waits only for the previous frame, so the worker draws frame N while the game
    // builds frame N+1 (as the PS2's GS drains one display list while the EE builds the next). `exact` (frame
    // captures) always drains first. Without the worker, `present` runs at once.
    void vu1ThreadFrame(std::function<void()> present, bool exact);
    bool vu1ThreadPipelined() const { return m_vu1Thread && m_vu1Pipeline; }
    // EE scheduler, at each VBlank: restarts the CSR polling count (see m_csrReadsThisVBlank).
    void noteVBlank() { m_csrReadsThisVBlank = 0u; }
    // PS2X_VU1_THREAD_CHECK=1: the worker compares its VIF1 register copy with the game thread's after every stretch.
    uint64_t vu1ThreadCheckMismatches() const { return m_vu1CheckMismatches.load(std::memory_order_relaxed); }
    uint64_t vu1ThreadChecks() const { return m_vu1Checks.load(std::memory_order_relaxed); }
    // Called after the EE writes a privileged GS register other than CSR (regOffset from 0x12000000).
    using GsPrivWriteCallback = std::function<void(uint32_t regOffset)>;
    void setGsPrivWriteCallback(GsPrivWriteCallback cb) { m_gsPrivWriteCallback = std::move(cb); }

    uint8_t *getVU1Code() { return m_vu1Code; }
    const uint8_t *getVU1Code() const { return m_vu1Code; }
    uint8_t *getVU1Data() { return m_vu1Data; }
    const uint8_t *getVU1Data() const { return m_vu1Data; }
    uint8_t *getVU0Code() { return m_vu0Code; }
    const uint8_t *getVU0Code() const { return m_vu0Code; }
    uint8_t *getVU0Data() { return m_vu0Data; }
    const uint8_t *getVU0Data() const { return m_vu0Data; }

    bool isPath3Masked() const { return m_path3Masked; }
    void flushMaskedPath3Packets(bool drainImmediately = true);

    void submitGifPacket(GifPathId pathId, const uint8_t *data, uint32_t sizeBytes, bool drainImmediately = true, bool path2DirectHl = false,
                         bool syntheticImageTag = false);
    void processGIFPacket(uint32_t srcPhysAddr, uint32_t qwCount);
    void processGIFPacket(const uint8_t *data, uint32_t sizeBytes);
    bool tryProcessNativeGifImageUploadChain(GS &gs, uint32_t tadr, uint32_t chcr);
    bool tryProcessNativeGifPackedChain(GS &gs, uint32_t tadr, uint32_t chcr);
    void processVIF0Data(uint32_t srcPhysAddr, uint32_t sizeBytes);
    void processVIF0Data(const uint8_t *data, uint32_t sizeBytes);
    void processVIF1Data(uint32_t srcPhysAddr, uint32_t sizeBytes);
    void processVIF1Data(const uint8_t *data, uint32_t sizeBytes);
    // Inline: everything, as without the worker. Ee: the game-thread pass of the threaded mode (registers, stalls,
    // interrupts; the parsed stretches go to the worker). Worker: the back-end pass (VU1 memory, VU1, GIF).
    enum class Vif1Pass : uint8_t
    {
        Inline,
        Ee,
        Worker
    };
    void processVIF1Stream(const uint8_t *data, uint32_t sizeBytes, Vif1Pass pass);
    void processPendingTransfers();
    std::vector<uint32_t> consumeCompletedDmacCauses();
    std::vector<uint32_t> consumeCompletedIntcCauses();

    // INTC_STAT (0x1000F000): causes latch here; the kernel dispatcher acknowledges the ones it
    // delivers, so a masked cause (VBON for sceGsSyncV polling) stays set until the guest clears it.
    static constexpr uint32_t kIntcStatAddress = 0x1000F000u;
    void latchIntcStatus(uint32_t cause);
    void acknowledgeIntcStatus(uint32_t cause);
    // What read32(kIntcStatAddress) returns, without the address decoding (for host-side polling loops).
    uint32_t intcStatus() const
    {
        const IoRegisterFile::Entry *entry = m_ioRegisters.find(kIntcStatAddress);
        return entry != nullptr ? entry->second : 0u;
    }

    int pollDmaRegisters();

    // Track code modifications for self-modifying code
    void registerCodeRegion(uint32_t start, uint32_t end);
    bool isCodeAddress(uint32_t address) const;
    bool isCodeModified(uint32_t address, uint32_t size);
    void clearModifiedFlag(uint32_t address, uint32_t size);

    // GS register accessors
    GSRegisters &gs() { return gs_regs; }
    Ps2Ipu &ipu() { return *m_ipu; }
    const GSRegisters &gs() const { return gs_regs; }
    uint8_t *getGSVRAM() { return m_gsVRAM; }
    const uint8_t *getGSVRAM() const { return m_gsVRAM; }
    bool hasSeenGifCopy() const { return m_seenGifCopy; }
    // Main RAM (32MB)
    uint8_t *m_rdram;

    // Scratchpad memory (16KB)
    uint8_t *m_scratchpad;

    // IOP RAM (2MB)
    uint8_t *iop_ram;

    bool m_seenGifCopy;
    std::atomic<uint64_t> m_dmaStartCount{0};
    std::atomic<uint64_t> m_gifCopyCount{0};
    std::atomic<uint64_t> m_gsWriteCount{0};
    std::atomic<uint64_t> m_vifWriteCount{0};
    std::atomic<uint64_t> m_vu0CodeGeneration{0};
    std::atomic<uint64_t> m_vu1CodeGeneration{0};
    // I/O registers
    IoRegisterFile m_ioRegisters;
    std::unique_ptr<Ps2Ipu> m_ipu; // IPU registers, FIFOs and DMA channels 3/4

    // Registers
    GSRegisters gs_regs;
    uint8_t *m_gsVRAM;
    VIFRegisters vif0_regs;
    VIFRegisters vif1_regs;
    DMARegisters dma_regs[10]; // 10 DMA channels

    // TLB entries
    struct TLBEntry
    {
        uint32_t vpn;
        uint32_t pfn;
        uint32_t mask;
        bool valid;
    };

    std::vector<TLBEntry> m_tlbEntries;

    GifPacketCallback m_gifPacketCallback;
    GifArbiter *m_gifArbiter = nullptr;
    Vu1MscalCallback m_vu1MscalCallback;
    Vu1MscntCallback m_vu1MscntCallback;
    GsPrivWriteCallback m_gsPrivWriteCallback;

    uint8_t *m_vu0Code = nullptr;
    uint8_t *m_vu0Data = nullptr;
    uint8_t *m_vu1Code = nullptr;
    uint8_t *m_vu1Data = nullptr;
    bool m_path3Masked = false;
    uint32_t m_vif1PendingPath2ImageQwc = 0u;
    bool m_vif1PendingPath2DirectHl = false;
    bool m_vif1Stalled = false;            // stopped by an i-bit, waiting for FBRST.STC
    bool m_vif1DmaCompletionDeferred = false;
    std::vector<uint8_t> m_vif1StalledData;
    std::vector<std::vector<uint8_t>> m_path3MaskedFifo;

    struct PendingTransfer
    {
        bool fromScratchpad = false;
        uint32_t srcAddr = 0;
        uint32_t qwc = 0;
        std::vector<uint8_t> chainData;
    };
    std::vector<PendingTransfer> m_pendingGifTransfers;
    std::vector<PendingTransfer> m_pendingVif0Transfers;
    std::vector<PendingTransfer> m_pendingVif1Transfers;
    std::vector<uint8_t> m_chainBufSpare; // a DMA chain buffer kept for the next chain (no allocation, no regrowth)
    void recycleChainData(std::vector<PendingTransfer> &transfers);
    std::mutex m_completedDmacMutex;
    std::vector<uint32_t> m_completedDmacCauses;
    std::vector<uint32_t> m_completedIntcCauses;

    struct CodeRegion
    {
        uint32_t start;
        uint32_t end;
        std::vector<bool> modified; // Bitmap of modified 4-byte blocks
    };
    std::vector<CodeRegion> m_codeRegions;

    bool isAddressInRegion(uint32_t address, const CodeRegion &region);
    void markModified(uint32_t address, uint32_t size);
    void markVU0CodeModified() { m_vu0CodeGeneration.fetch_add(1, std::memory_order_relaxed); }
    void markVU1CodeModified() { m_vu1CodeGeneration.fetch_add(1, std::memory_order_relaxed); }
    bool isScratchpad(uint32_t address) const;
    uint8_t *mapVuMemory(uint32_t physAddr, uint32_t size, uint32_t &offset, uint32_t &limit);
    const uint8_t *mapVuMemory(uint32_t physAddr, uint32_t size, uint32_t &offset, uint32_t &limit) const;
    struct EeTimer
    {
        uint32_t count = 0;
        uint32_t mode = 0;
        uint32_t compare = 0;
        uint32_t hold = 0;
        uint64_t clockRemainder = 0;
    };

    std::array<EeTimer, 4> m_eeTimers{};
    bool tryProcessScratchpadDma(uint32_t channelBase, uint32_t chcr);
    bool processToScratchpadChain(uint32_t chcr);
    void completeDmacChannel(uint32_t channelBase, uint32_t cause);
    void queueCompletedDmacCause(uint32_t cause);
    void queueCompletedIntcCause(uint32_t cause);
    void raiseDmaChannelStatus(uint32_t channelBit);
    void completeVif1Dma();
    void resumeStalledVif1();
    uint32_t submitPendingPath2Image(const uint8_t *data, uint32_t availableQw, uint32_t &pendingQwc, bool &pendingDirectHl,
                                     bool submit);
    void drainGifArbiter();
    bool vu1ThreadQueues() const; // this thread hands back-end work to the worker instead of doing it
    void vu1ThreadWaitSlow(uint32_t reason) const;
    void vu1ThreadForwardVif1(const uint8_t *data, uint32_t sizeBytes, bool check);
    void vu1ThreadApply(uint32_t kind, uint32_t a, uint32_t b, const uint8_t *payload, uint32_t size);

    std::unique_ptr<Vu1Thread> m_vu1Thread;
    bool m_vu1InlineKick = false; // threaded mode, but this kick runs inline (the worker is idle)
    bool m_vu1ThreadCheck = false;
    Vu1NeedsInlineFn m_vu1NeedsInlineFn;
    Vu1EeMscalFn m_vu1EeMscalFn;
    // The worker's copy of the VIF1 parse state, advanced by the same stream and register writes as vif1_regs.
    VIFRegisters m_vif1WorkerRegs{};
    uint32_t m_vif1WorkerPendingPath2ImageQwc = 0u;
    bool m_vif1WorkerPendingPath2DirectHl = false;
    std::atomic<uint64_t> m_vu1CheckMismatches{0};
    std::atomic<uint64_t> m_vu1Checks{0};

    // Pipelined frames (vu1ThreadFrame). The worker owns the GS privileged registers' stream-ordered state: EE writes
    // to them are queued (gsPrivWritePipelined). CSR reads don't wait: FIELD and the other EE-written bits are
    // current, and SIGNAL/FINISH (set by GS packets) are as the worker left them at the previous frame boundary,
    // less the EE's own clears since; so what the game reads never depends on how far the worker has got.
    bool m_vu1Pipeline = false;
    uint32_t m_csrEeBits = 0u;            // SIGNAL/FINISH as the game thread sees them
    uint32_t m_csrClearsSinceFrame = 0u;  // SIGNAL/FINISH clears queued since the last frame boundary
    uint32_t m_csrReadsThisVBlank = 0u;   // a game polling CSR (more than kCsrPollLimit reads in one VBlank) would
                                          // wait for SIGNAL/FINISH, so the pipeline turns itself off
    static constexpr uint32_t kCsrPollLimit = 64u;
    bool gsPrivPipelined() const;
    uint64_t readCsrPipelined();
    void writeCsrPipelined(uint32_t off, uint64_t value, bool full);
    void stopPipeline(const char *why);
    void applyGsPrivWrite(uint32_t address, uint32_t width, uint64_t value);
};

#endif // PS2_MEMORY_H
