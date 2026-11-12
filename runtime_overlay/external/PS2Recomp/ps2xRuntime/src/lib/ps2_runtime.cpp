#include "ps2_runtime.h"
#include "ps2_log.h"
#include "ps2_stubs.h"
#include "ps2_syscalls.h"
#include "game_overrides.h"
#include "ps2_runtime_macros.h"
#include "runtime/gs/gs_frontend.h"
#include "runtime/gs/gs_async_queue.h"
#include "runtime/ee_scheduler.h"
#include "runtime/ps2_host_profile.h"
#include "runtime/ps2_pacing_trace.h"
#include "runtime/ps2_present_pacing.h"
#include "runtime/ps2_exit_reason.h"
#include "runtime/ps2_vu1_overlap_model.h"
#include "runtime/ps2_vu1_thread.h"
#include "runtime/ps2_display_aspect.h"
#include "ThreadNaming.h"
#include "Kernel/Stubs/Audio.h"
#include "Kernel/Stubs/GS.h"
#include "Kernel/Stubs/MPEG.h"
#include "ps2_host_backend.h"
#include "rlgl.h"
#include "ps2_iop_host.h"
#include "ps2x/iop/iop_subsystem.h"

#include <iostream>
#include <stdexcept>
#include <fstream>
#include <algorithm>
#include <cmath>
#include <array>
#include <cstring>
#include <limits>
#include <chrono>
#include <atomic>
#include <thread>
#include <unordered_map>
#include <sstream>

extern "C" void *glfwGetProcAddress(const char *procname); // raylib's GLFW

#if defined(_WIN32)
#include <atomic>
#include <ctime>
#include <filesystem>
#include <string>
#include <system_error>
#include <thread>

// F12: a screenshot for the launcher's banner, in PS2X_SHOT_DIR. raylib handles F12 itself inside EndDrawing (a
// full-size PNG in the working directory, encoded on the window thread), so the key stops here and never reaches it.
using KeyCallback = void (*)(void *, int, int, int, int);
extern "C" KeyCallback glfwSetKeyCallback(void *window, KeyCallback callback);
extern "C" void *glfwGetCurrentContext();
static std::atomic<bool> g_shotRequested{false};
static KeyCallback g_raylibKeyCallback = nullptr;

static void shotKeyCallback(void *window, int key, int scancode, int action, int mods)
{
    const int glfwKeyF12 = 301, glfwPress = 1;
    if (key == glfwKeyF12)
    {
        if (action == glfwPress)
            g_shotRequested.store(true, std::memory_order_relaxed);
        return;
    }
    if (g_raylibKeyCallback)
        g_raylibKeyCallback(window, key, scancode, action, mods);
}

// Reads the window here, then scales and encodes on another thread, so the game does not stop for it.
static void saveWindowShot(Rectangle picture)
{
    rlDrawRenderBatchActive();
    Image image = LoadImageFromScreen();
    std::thread([image, picture]() mutable {
        const char *dirEnv = std::getenv("PS2X_SHOT_DIR");
        const std::string dir = dirEnv && *dirEnv ? dirEnv : "screenshots";
        std::error_code error;
        std::filesystem::create_directories(std::filesystem::u8path(dir), error);
        ImageCrop(&image, picture); // no bars beside a 4:3 picture
        if (image.width > 1280)
            ImageResize(&image, 1280, image.height * 1280 / image.width);
        char name[64];
        const std::time_t now = std::time(nullptr);
        std::tm local{};
        localtime_s(&local, &now);
        std::strftime(name, sizeof(name), "shot-%Y%m%d-%H%M%S.png", &local);
        ExportImage(image, (dir + "/" + name).c_str());
        UnloadImage(image);
    }).detach();
}
#endif

namespace ps2_stubs
{
    void resetSifState();
}

#define ELF_MAGIC 0x464C457F // "\x7FELF" in little endian
#define ET_EXEC 2            // Executable file
#define EM_MIPS 8            // MIPS architecture
#define PT_LOAD 1            // Loadable segment

static constexpr int FB_WIDTH = 640;
static constexpr int FB_HEIGHT = 512;
static constexpr int DEFAULT_DISPLAY_HEIGHT = 448;
static constexpr uint32_t DEFAULT_FB_SIZE = FB_WIDTH * FB_HEIGHT * 4;
static constexpr uint32_t DEFAULT_FB_ADDR = (PS2_RAM_SIZE - DEFAULT_FB_SIZE - 0x10000u);
#if defined(PLATFORM_VITA)
static constexpr int HOST_WINDOW_WIDTH = 960;
static constexpr int HOST_WINDOW_HEIGHT = 544;
#else
static constexpr int HOST_WINDOW_WIDTH = FB_WIDTH;
static constexpr int HOST_WINDOW_HEIGHT = FB_WIDTH * 3 / 4; // 4:3, like the TV picture
#endif
struct ElfHeader
{
    uint32_t magic;
    uint8_t elf_class;
    uint8_t endianness;
    uint8_t version;
    uint8_t os_abi;
    uint8_t abi_version;
    uint8_t padding[7];
    uint16_t type;
    uint16_t machine;
    uint32_t version2;
    uint32_t entry;
    uint32_t phoff;
    uint32_t shoff;
    uint32_t flags;
    uint16_t ehsize;
    uint16_t phentsize;
    uint16_t phnum;
    uint16_t shentsize;
    uint16_t shnum;
    uint16_t shstrndx;
};

struct ProgramHeader
{
    uint32_t type;
    uint32_t offset;
    uint32_t vaddr;
    uint32_t paddr;
    uint32_t filesz;
    uint32_t memsz;
    uint32_t flags;
    uint32_t align;
};

namespace
{
    constexpr uint32_t kGuestHeapDefaultBase = 0x00100000u;
    constexpr uint32_t kGuestHeapDefaultAlignment = 16u;
    constexpr uint32_t kGuestHeapSafetyPad = 0x1000u;
    constexpr uint32_t kGuestHeapHardLimit = PS2_RUNTIME_RESERVED_BASE; // 0x1F00000 with 32MB of RAM

    constexpr uint32_t COP0_CAUSE_EXCCODE_MASK = 0x0000007Cu;
    constexpr uint32_t COP0_CAUSE_BD = 0x80000000u;
    constexpr uint32_t COP0_STATUS_EXL = 0x00000002u;
    constexpr uint32_t COP0_STATUS_BEV = 0x00400000u;
    constexpr uint32_t EXCEPTION_VECTOR_GENERAL = 0x80000080u;
    constexpr uint32_t EXCEPTION_VECTOR_TLB_REFILL = 0x80000000u;
    constexpr uint32_t EXCEPTION_VECTOR_BOOT = 0xBFC00200u;

    struct DispatchHistory
    {
        std::array<uint32_t, 64> pcs{};
        uint32_t next = 0u;
        bool wrapped = false;
    };

    thread_local DispatchHistory g_dispatchHistory;

    bool computeFileCrc32(const std::string &path, uint32_t &crcOut)
    {
        std::ifstream file(path, std::ios::binary);
        if (!file.is_open())
        {
            return false;
        }

        static const std::array<uint32_t, 256> table = []
        {
            std::array<uint32_t, 256> values{};
            for (uint32_t i = 0; i < values.size(); ++i)
            {
                uint32_t value = i;
                for (uint32_t bit = 0; bit < 8; ++bit)
                {
                    value = (value & 1u) ? (0xEDB88320u ^ (value >> 1u)) : (value >> 1u);
                }
                values[i] = value;
            }
            return values;
        }();

        uint32_t crc = 0xFFFFFFFFu;
        std::array<uint8_t, 16 * 1024> buffer{};
        while (file.good())
        {
            file.read(reinterpret_cast<char *>(buffer.data()), static_cast<std::streamsize>(buffer.size()));
            const std::streamsize count = file.gcount();
            for (std::streamsize i = 0; i < count; ++i)
            {
                crc = table[(crc ^ buffer[static_cast<size_t>(i)]) & 0xFFu] ^ (crc >> 8u);
            }
        }
        if (file.bad())
        {
            return false;
        }
        crcOut = ~crc;
        return true;
    }

    void pushDispatchPc(uint32_t pc)
    {
        DispatchHistory &h = g_dispatchHistory;
        h.pcs[h.next] = pc;
        h.next = (h.next + 1u) % static_cast<uint32_t>(h.pcs.size());
        if (h.next == 0u)
        {
            h.wrapped = true;
        }
    }

    std::string formatDispatchHistory()
    {
        const DispatchHistory &h = g_dispatchHistory;
        const uint32_t count = h.wrapped ? static_cast<uint32_t>(h.pcs.size()) : h.next;
        if (count == 0u)
        {
            return "(empty)";
        }

        std::ostringstream oss;
        bool first = true;
        for (uint32_t i = 0u; i < count; ++i)
        {
            const uint32_t idx = (h.next + h.pcs.size() - count + i) % static_cast<uint32_t>(h.pcs.size());
            if (!first)
            {
                oss << " -> ";
            }
            first = false;
            oss << "0x" << std::hex << h.pcs[idx];
        }
        return oss.str();
    }

    uint32_t selectExceptionVector(const R5900Context *ctx, bool tlbRefill)
    {
        if (ctx->cop0_status & COP0_STATUS_BEV)
        {
            return EXCEPTION_VECTOR_BOOT;
        }
        return tlbRefill ? EXCEPTION_VECTOR_TLB_REFILL : EXCEPTION_VECTOR_GENERAL;
    }

    void seedVu0IdleSuccess(R5900Context *ctx)
    {
        if (!ctx)
        {
            return;
        }

        ctx->vu0_clip_flags = 0;
        ctx->vu0_clip_flags2 = 0;
        ctx->vu0_mac_flags = 0;
        ctx->vu0_status = 0;
        ctx->vu0_q = 1.0f;
        ctx->vu0_r = _mm_castsi128_ps(_mm_set1_epi32(0x3F800000));
        ctx->vu0_vpu_stat = 0;
        ctx->vu0_vpu_stat2 = 0;
    }

    void copyVu0ContextToState(const R5900Context *ctx, VU1State &state)
    {
        std::memset(&state, 0, sizeof(state));

        for (uint32_t i = 0; i < 32u; ++i)
        {
            _mm_storeu_ps(state.vf[i], ctx->vu0_vf[i]);
        }
        for (uint32_t i = 0; i < 16u; ++i)
        {
            state.vi[i] = static_cast<int16_t>(ctx->vi[i]);
        }

        _mm_storeu_ps(state.acc, ctx->vu0_acc);
        state.q = ctx->vu0_q;
        state.p = ctx->vu0_p;
        state.i = ctx->vu0_i;
        alignas(16) uint32_t rWords[4]{};
        _mm_storeu_si128(reinterpret_cast<__m128i *>(rWords), _mm_castps_si128(ctx->vu0_r));
        state.r = 0x3F800000u | (rWords[0] & 0x007FFFFFu);
        state.pc = ctx->vu0_pc;
        state.mac = ctx->vu0_mac_flags;
        state.clip = ctx->vu0_clip_flags;
        state.status = ctx->vu0_status;
        state.itop = ctx->vu0_itop;
        state.dBitEnabled = (ctx->vu0_fbrst & (1u << 2)) != 0u;
        state.tBitEnabled = (ctx->vu0_fbrst & (1u << 3)) != 0u;

        state.vf[0][0] = 0.0f;
        state.vf[0][1] = 0.0f;
        state.vf[0][2] = 0.0f;
        state.vf[0][3] = 1.0f;
        state.vi[0] = 0;
    }

    void copyVu0StateToContext(const VU1State &state, R5900Context *ctx)
    {
        for (uint32_t i = 0; i < 32u; ++i)
        {
            ctx->vu0_vf[i] = _mm_loadu_ps(state.vf[i]);
        }
        for (uint32_t i = 0; i < 16u; ++i)
        {
            ctx->vi[i] = static_cast<uint16_t>(state.vi[i]);
        }

        ctx->vu0_acc = _mm_loadu_ps(state.acc);
        ctx->vu0_q = state.q;
        ctx->vu0_p = state.p;
        ctx->vu0_i = state.i;
        ctx->vu0_r = _mm_castsi128_ps(_mm_set1_epi32(static_cast<int32_t>(state.r)));
        ctx->vu0_mac_flags = state.mac;
        ctx->vu0_clip_flags = state.clip;
        ctx->vu0_clip_flags2 = state.clip;
        ctx->vu0_status = static_cast<uint16_t>(state.status);
        ctx->vu0_itop = state.itop;
        ctx->vu0_pc = state.pc;
        ctx->vu0_tpc = state.pc;
        ctx->vu0_vpu_stat = (ctx->vu0_vpu_stat & 0xFF00u) | (state.stoppedByD ? (1u << 1) : 0u) | (state.stoppedByT ? (1u << 2) : 0u);
        ctx->vu0_vpu_stat2 = 0;

        ctx->vu0_vf[0] = _mm_set_ps(1.0f, 0.0f, 0.0f, 0.0f);
        ctx->vi[0] = 0;
    }

    void raiseCop0Exception(R5900Context *ctx, uint32_t exceptionCode, bool tlbRefill = false)
    {
        if (ctx->in_delay_slot)
        {
            ctx->cop0_epc = ctx->branch_pc;
            ctx->cop0_cause = (ctx->cop0_cause & ~COP0_CAUSE_EXCCODE_MASK) |
                              ((exceptionCode << 2) & COP0_CAUSE_EXCCODE_MASK) |
                              COP0_CAUSE_BD;
        }
        else
        {
            ctx->cop0_epc = ctx->pc;
            ctx->cop0_cause = (ctx->cop0_cause & ~(COP0_CAUSE_EXCCODE_MASK | COP0_CAUSE_BD)) |
                              ((exceptionCode << 2) & COP0_CAUSE_EXCCODE_MASK);
        }

        ctx->cop0_status |= COP0_STATUS_EXL;
        ctx->pc = selectExceptionVector(ctx, tlbRefill);
        ctx->in_delay_slot = false;
    }

    std::filesystem::path normalizeAbsolutePath(const std::filesystem::path &path)
    {
        if (path.empty())
        {
            return {};
        }

#if defined(PLATFORM_VITA)
        const std::string generic = path.generic_string();
        const std::size_t colon = generic.find(':');
        if (colon != std::string::npos && colon != 0u)
        {
            const std::size_t slash = generic.find_first_of("/\\");
            if (slash == std::string::npos || colon < slash)
            {
                return path.lexically_normal();
            }
        }
#endif

        std::error_code ec;
        const std::filesystem::path absolute = std::filesystem::absolute(path, ec);
        if (ec)
        {
            return path.lexically_normal();
        }
        return absolute.lexically_normal();
    }

    bool g_memoryCardRootPinned = false;

    PS2Runtime::IoPaths &runtimeIoPaths()
    {
        static PS2Runtime::IoPaths paths = []()
        {
            PS2Runtime::IoPaths defaults;
            std::error_code ec;
            const std::filesystem::path cwd = std::filesystem::current_path(ec);
            defaults.elfDirectory = ec ? std::filesystem::path(".") : cwd.lexically_normal();
            defaults.hostRoot = defaults.elfDirectory;
            defaults.cdRoot = defaults.elfDirectory;
            defaults.mcRoot = defaults.elfDirectory / "mc0";
            return defaults;
        }();

        return paths;
    }

    std::string readGuestPrintableString(const uint8_t *rdram, uint32_t addr, size_t maxLen)
    {
        std::string out;
        if (!rdram || maxLen == 0)
        {
            return out;
        }

        out.reserve(std::min<size_t>(maxLen, 64));
        for (size_t i = 0; i < maxLen; ++i)
        {
            const char ch = static_cast<char>(rdram[(addr + static_cast<uint32_t>(i)) & PS2_RAM_MASK]);
            if (ch == '\0')
            {
                break;
            }
            if (ch >= 0x20 && ch < 0x7F)
            {
                out.push_back(ch);
            }
            else
            {
                out.push_back('.');
            }
        }
        return out;
    }
}

static void UploadFrame(Texture2D &tex, PS2Runtime *rt, uint32_t &outWidth, uint32_t &outHeight)
{
    static uint64_t s_lastPresentationTick = std::numeric_limits<uint64_t>::max();
    static bool s_hasLatchedInitialFrame = false;
    static uint32_t s_lastDisplayFbp = std::numeric_limits<uint32_t>::max();
    static uint32_t s_lastSourceFbp = std::numeric_limits<uint32_t>::max();
    static bool s_lastPreferred = false;
    static uint32_t s_lastWidth = 0u;
    static uint32_t s_lastHeight = 0u;
    static bool s_hasUploadedFrame = false;
    static std::vector<uint8_t> s_scratch;
    static std::vector<uint8_t> s_uploadBuffer(DEFAULT_FB_SIZE, 0u);
    // PS2X_PRESENT_FILTER=linear: bilinear scaling to the window instead of nearest-neighbour (the default).
    static const bool s_linearFilter = [] {
        const char *v = std::getenv("PS2X_PRESENT_FILTER");
        return v && std::strcmp(v, "linear") == 0;
    }();
    static unsigned int s_filteredTextureId = 0u;
    if (s_linearFilter && tex.id != s_filteredTextureId)
    {
        SetTextureFilter(tex, TEXTURE_FILTER_BILINEAR);
        s_filteredTextureId = tex.id;
    }

    if (const auto &source = rt->hostFrameSource())
    {
        uint32_t width = 0u, height = 0u;
        if (source(s_scratch, width, height) && width != 0u && height != 0u &&
            s_scratch.size() >= static_cast<size_t>(width) * height * 4u)
        {
            // A frame larger than the GS framebuffer (high-resolution scanout) gets a texture of its own size;
            // frames that fit keep the framebuffer-sized texture.
            const bool large = width > FB_WIDTH || height > FB_HEIGHT;
            const int texWidth = large ? static_cast<int>(width) : FB_WIDTH;
            const int texHeight = large ? static_cast<int>(height) : FB_HEIGHT;
            if (tex.width != texWidth || tex.height != texHeight)
            {
                UnloadTexture(tex);
                Image blank = GenImageColor(texWidth, texHeight, BLANK);
                tex = LoadTextureFromImage(blank);
                UnloadImage(blank);
                if (s_linearFilter)
                {
                    SetTextureFilter(tex, TEXTURE_FILTER_BILINEAR);
                    s_filteredTextureId = tex.id;
                }
            }
            if (large)
            {
                UpdateTexture(tex, s_scratch.data());
                s_lastWidth = width;
                s_lastHeight = height;
                s_hasUploadedFrame = true;
                outWidth = s_lastWidth;
                outHeight = s_lastHeight;
                return;
            }
            std::fill(s_uploadBuffer.begin(), s_uploadBuffer.end(), 0u);
            const uint32_t copyWidth = std::min<uint32_t>(width, FB_WIDTH);
            const uint32_t copyHeight = std::min<uint32_t>(height, FB_HEIGHT);
            for (uint32_t y = 0; y < copyHeight; ++y)
                std::memcpy(s_uploadBuffer.data() + static_cast<size_t>(y) * FB_WIDTH * 4u,
                            s_scratch.data() + static_cast<size_t>(y) * width * 4u, static_cast<size_t>(copyWidth) * 4u);
            UpdateTexture(tex, s_uploadBuffer.data());
            s_lastWidth = width;
            s_lastHeight = height;
            s_hasUploadedFrame = true;
        }
        outWidth = s_hasUploadedFrame ? s_lastWidth : FB_WIDTH;
        outHeight = s_hasUploadedFrame ? s_lastHeight : DEFAULT_DISPLAY_HEIGHT;
        return;
    }

    // With a GS worker (PS2X_GS_THREAD=1) the worker latches each flip at its place in the stream; the presenter
    // only picks up a newly latched frame. Inline, the presenter latches once per VBlank as before.
    const bool asyncGs = rt->gs().asyncEnabled();
    const uint64_t currentTick = asyncGs ? rt->gs().hostPresentationGeneration() : rt->eeScheduler().currentVSyncTick();
    const bool needsLatch = !s_hasLatchedInitialFrame || currentTick != s_lastPresentationTick;
    if (needsLatch)
    {
        if (!asyncGs)
            rt->gs().latchHostPresentationFrame();
        s_lastPresentationTick = currentTick;
        s_hasLatchedInitialFrame = true;
    }
    else if (s_hasUploadedFrame)
    {
        outWidth = (s_lastWidth != 0u) ? s_lastWidth : FB_WIDTH;
        outHeight = (s_lastHeight != 0u) ? s_lastHeight : DEFAULT_DISPLAY_HEIGHT;
        return;
    }

    s_scratch.clear();
    uint32_t width = 0u;
    uint32_t height = 0u;
    uint32_t displayFbp = 0u;
    uint32_t sourceFbp = 0u;
    bool usedPreferredDisplaySource = false;
    if (!rt->gs().copyLatchedHostPresentationFrame(s_scratch,
                                                   width,
                                                   height,
                                                   &displayFbp,
                                                   &sourceFbp,
                                                   &usedPreferredDisplaySource))
    {
        Image blank = GenImageColor(FB_WIDTH, FB_HEIGHT, MAGENTA);
        UpdateTexture(tex, blank.data);
        UnloadImage(blank);
        outWidth = FB_WIDTH;
        outHeight = DEFAULT_DISPLAY_HEIGHT;
        s_lastWidth = outWidth;
        s_lastHeight = outHeight;
        s_hasUploadedFrame = true;
        return;
    }

    PS2_IF_AGRESSIVE_LOGS({
        static uint32_t s_uploadDebugCount = 0u;
        if (s_uploadDebugCount < 128u ||
            displayFbp != s_lastDisplayFbp ||
            sourceFbp != s_lastSourceFbp ||
            usedPreferredDisplaySource != s_lastPreferred ||
            width != s_lastWidth ||
            height != s_lastHeight)
        {
            std::cout << "[frame:upload] idx=" << s_uploadDebugCount
                      << " tick=" << currentTick
                      << " displayFbp=" << displayFbp
                      << " sourceFbp=" << sourceFbp
                      << " size=" << width << "x" << height
                      << " preferred=" << static_cast<uint32_t>(usedPreferredDisplaySource ? 1u : 0u)
                      << std::endl;
        }
        ++s_uploadDebugCount;
    });
    s_lastDisplayFbp = displayFbp;
    s_lastSourceFbp = sourceFbp;
    s_lastPreferred = usedPreferredDisplaySource;
    s_lastWidth = width;
    s_lastHeight = height;

    std::fill(s_uploadBuffer.begin(), s_uploadBuffer.end(), 0u);
    if (!s_scratch.empty() && width != 0u && height != 0u)
    {
        const uint32_t copyWidth = std::min<uint32_t>(width, FB_WIDTH);
        const uint32_t copyHeight = std::min<uint32_t>(height, FB_HEIGHT);
        const size_t srcRowBytes = static_cast<size_t>(width) * 4u;
        const size_t dstRowBytes = static_cast<size_t>(FB_WIDTH) * 4u;
        const size_t copyRowBytes = static_cast<size_t>(copyWidth) * 4u;
        for (uint32_t y = 0; y < copyHeight; ++y)
        {
            const size_t srcOffset = static_cast<size_t>(y) * srcRowBytes;
            const size_t dstOffset = static_cast<size_t>(y) * dstRowBytes;
            if (srcOffset + copyRowBytes > s_scratch.size() ||
                dstOffset + copyRowBytes > s_uploadBuffer.size())
            {
                break;
            }
            std::memcpy(s_uploadBuffer.data() + dstOffset, s_scratch.data() + srcOffset, copyRowBytes);
        }
    }

    UpdateTexture(tex, s_uploadBuffer.data());
    outWidth = width;
    outHeight = height;
    s_hasUploadedFrame = true;
}

namespace
{
    // Host playback of the IOP's SPU2 output. The EE thread (the producer) appends guest-timed frames to a lock-free
    // single-producer/single-consumer ring and the audio device's callback (the consumer) takes them: neither side
    // blocks, locks or allocates. Playback starts once kTargetFrames (60 ms) are queued; a dry ring plays silence
    // (an underrun) until kTargetFrames are queued again. The ring is bounded: frames that do not fit are dropped and
    // counted. Nothing is resampled, duplicated or skipped. Instead, the guest is kept at the device's real-time rate:
    // the callback turns the ring's average fill into a pacing correction of at most 0.5% that EeScheduler applies to
    // the VBlank period (iopAudioPacing), so the guest's 48 kHz runs at the device's 48 kHz.
    struct IopAudioPlayer
    {
        static constexpr uint64_t kCapacityFrames = 16384u; // 341 ms; a power of two
        static constexpr uint64_t kTargetFrames = 2880u;    // 60 ms
        static constexpr int32_t kMaxPacingPpm = 5000;      // 0.5%
        std::vector<int16_t> ring = std::vector<int16_t>(kCapacityFrames * 2u);
        std::atomic<uint64_t> written{0}; // frames, monotonic; advanced only by the producer
        std::atomic<uint64_t> read{0};    // frames, monotonic; advanced only by the consumer
        std::atomic<bool> playing{false};
        std::atomic<int32_t> pacingPpm{0};
        std::atomic<int64_t> lastCallbackNs{0}; // steady_clock time of the latest device callback
        bool started = false;
        AudioStream stream{};

        // Consumer-only state.
        double averageFill = static_cast<double>(kTargetFrames);
        std::chrono::steady_clock::time_point lastCallback{};

        // Statistics: totals, and a window that takeIopAudioStats() resets.
        std::atomic<uint64_t> underruns{0}, dropped{0};
        std::atomic<uint64_t> wCallbacks{0}, wRequested{0}, wSilent{0}, wQueued{0}, wDropped{0}, wUnderruns{0};
        std::atomic<uint64_t> wMinFill{UINT64_MAX}, wMaxFill{0}, wMaxGapUs{0};
        std::atomic<uint64_t> wRealTimeTicks{0}, wRealTimeFallbacks{0}, wRealTimeAnchors{0};
        std::atomic<bool> realTime{false};

        static void atomicMin(std::atomic<uint64_t> &value, uint64_t candidate)
        {
            uint64_t current = value.load(std::memory_order_relaxed);
            while (candidate < current && !value.compare_exchange_weak(current, candidate, std::memory_order_relaxed))
            {
            }
        }
        static void atomicMax(std::atomic<uint64_t> &value, uint64_t candidate)
        {
            uint64_t current = value.load(std::memory_order_relaxed);
            while (candidate > current && !value.compare_exchange_weak(current, candidate, std::memory_order_relaxed))
            {
            }
        }
        void noteFill(uint64_t fill)
        {
            atomicMin(wMinFill, fill);
            atomicMax(wMaxFill, fill);
        }
    };

    IopAudioPlayer &iopAudioPlayer()
    {
        static IopAudioPlayer player;
        return player;
    }

    void iopAudioCallback(void *buffer, unsigned int frames)
    {
        IopAudioPlayer &p = iopAudioPlayer();
        int16_t *out = static_cast<int16_t *>(buffer);
        const auto now = std::chrono::steady_clock::now();
        if (p.lastCallback != std::chrono::steady_clock::time_point{})
            IopAudioPlayer::atomicMax(p.wMaxGapUs, static_cast<uint64_t>(
                                                       std::chrono::duration_cast<std::chrono::microseconds>(now - p.lastCallback).count()));
        p.lastCallback = now;
        p.lastCallbackNs.store(std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch()).count(),
                               std::memory_order_release);
        p.wCallbacks.fetch_add(1u, std::memory_order_relaxed);
        p.wRequested.fetch_add(frames, std::memory_order_relaxed);

        const uint64_t read = p.read.load(std::memory_order_relaxed);
        const uint64_t available = p.written.load(std::memory_order_acquire) - read;
        p.noteFill(available);
        bool playing = p.playing.load(std::memory_order_relaxed);
        if (!playing && available >= IopAudioPlayer::kTargetFrames)
        {
            playing = true;
            p.averageFill = static_cast<double>(available);
        }

        uint64_t taken = 0;
        if (playing)
        {
            taken = std::min<uint64_t>(available, frames);
            for (uint64_t i = 0; i < taken; ++i)
            {
                const uint64_t slot = (read + i) & (IopAudioPlayer::kCapacityFrames - 1u);
                out[2u * i] = p.ring[2u * slot];
                out[2u * i + 1u] = p.ring[2u * slot + 1u];
            }
            p.read.store(read + taken, std::memory_order_release);
            if (taken < frames)
            {
                // The ring ran dry: play silence and wait for kTargetFrames again.
                playing = false;
                p.underruns.fetch_add(1u, std::memory_order_relaxed);
                p.wUnderruns.fetch_add(1u, std::memory_order_relaxed);
            }
        }
        if (taken < frames)
        {
            std::memset(out + 2u * taken, 0, static_cast<size_t>(frames - taken) * 2u * sizeof(int16_t));
            p.wSilent.fetch_add(frames - taken, std::memory_order_relaxed);
        }
        p.noteFill(available - taken);

        // Pacing: a ring fuller than the target slows the guest down, an emptier one speeds it up, by at most 0.5%.
        // The average spans about half a second, smoothing the one-field bursts in which the guest produces audio.
        int32_t ppm = 0;
        if (playing)
        {
            p.averageFill += (static_cast<double>(available) - p.averageFill) * 0.02;
            const double error = (p.averageFill - static_cast<double>(IopAudioPlayer::kTargetFrames)) /
                                 static_cast<double>(IopAudioPlayer::kTargetFrames);
            ppm = static_cast<int32_t>(std::clamp(error * 10000.0, -static_cast<double>(IopAudioPlayer::kMaxPacingPpm),
                                                  static_cast<double>(IopAudioPlayer::kMaxPacingPpm)));
        }
        p.pacingPpm.store(ppm, std::memory_order_relaxed);
        p.playing.store(playing, std::memory_order_relaxed);
    }
}

void PS2Runtime::playIopAudio(const int16_t *samples, size_t frames)
{
    if (!m_iopAudioPlayback || !IsAudioDeviceReady())
        return;
    IopAudioPlayer &p = iopAudioPlayer();
    if (!p.started)
    {
        p.started = true;
        SetAudioStreamBufferSizeDefault(1024);
        p.stream = LoadAudioStream(48000u, 16u, 2u);
        SetAudioStreamCallback(p.stream, iopAudioCallback);
        PlayAudioStream(p.stream);
    }
    const uint64_t written = p.written.load(std::memory_order_relaxed);
    const uint64_t space = IopAudioPlayer::kCapacityFrames - (written - p.read.load(std::memory_order_acquire));
    const uint64_t accepted = std::min<uint64_t>(space, frames);
    for (uint64_t i = 0; i < accepted; ++i)
    {
        const uint64_t slot = (written + i) & (IopAudioPlayer::kCapacityFrames - 1u);
        p.ring[2u * slot] = samples[2u * i];
        p.ring[2u * slot + 1u] = samples[2u * i + 1u];
    }
    p.written.store(written + accepted, std::memory_order_release);
    p.wQueued.fetch_add(frames, std::memory_order_relaxed);
    if (accepted < frames)
    {
        p.dropped.fetch_add(frames - accepted, std::memory_order_relaxed);
        p.wDropped.fetch_add(frames - accepted, std::memory_order_relaxed);
    }
    p.noteFill(written + accepted - p.read.load(std::memory_order_acquire));
}

bool PS2Runtime::iopAudioPacing(int32_t &ppm, bool &catchUp) const
{
    IopAudioPlayer &p = iopAudioPlayer();
    // With the IOP on the device's clock the EE keeps its own field-rate pacing: the audio no longer depends on it.
    if (!m_iopAudioPlayback || !p.started || iopOnRealTimeClock())
    {
        ppm = 0;
        catchUp = false;
        return false;
    }
    ppm = p.pacingPpm.load(std::memory_order_relaxed);
    // Lost time is made up only while the ring is short of its target. After an underrun the device has already
    // played the missing time as silence, so catching up beyond the target would only add latency.
    catchUp = p.written.load(std::memory_order_acquire) - p.read.load(std::memory_order_acquire) <
              IopAudioPlayer::kTargetFrames;
    return true;
}

PS2Runtime::IopAudioStats PS2Runtime::takeIopAudioStats()
{
    IopAudioPlayer &p = iopAudioPlayer();
    IopAudioStats window;
    const uint64_t fill = p.written.load(std::memory_order_acquire) - p.read.load(std::memory_order_acquire);
    window.callbacks = p.wCallbacks.exchange(0u, std::memory_order_relaxed);
    window.framesRequested = p.wRequested.exchange(0u, std::memory_order_relaxed);
    window.silentFrames = p.wSilent.exchange(0u, std::memory_order_relaxed);
    window.framesQueued = p.wQueued.exchange(0u, std::memory_order_relaxed);
    window.droppedFrames = p.wDropped.exchange(0u, std::memory_order_relaxed);
    window.underruns = p.wUnderruns.exchange(0u, std::memory_order_relaxed);
    window.minBuffered = std::min(p.wMinFill.exchange(fill, std::memory_order_relaxed), fill);
    window.maxBuffered = std::max(p.wMaxFill.exchange(fill, std::memory_order_relaxed), fill);
    window.maxCallbackGapUs = p.wMaxGapUs.exchange(0u, std::memory_order_relaxed);
    window.buffered = fill;
    window.pacingPpm = p.pacingPpm.load(std::memory_order_relaxed);
    window.realTime = p.realTime.load(std::memory_order_relaxed);
    window.realTimeTicks = p.wRealTimeTicks.exchange(0u, std::memory_order_relaxed);
    window.realTimeFallbacks = p.wRealTimeFallbacks.exchange(0u, std::memory_order_relaxed);
    window.realTimeAnchors = p.wRealTimeAnchors.exchange(0u, std::memory_order_relaxed);
    return window;
}

uint64_t PS2Runtime::iopAudioUnderruns() const
{
    return iopAudioPlayer().underruns.load(std::memory_order_relaxed);
}

uint64_t PS2Runtime::iopAudioDroppedFrames() const
{
    return iopAudioPlayer().dropped.load(std::memory_order_relaxed);
}

PS2Runtime::PS2Runtime()
{
    m_iopHost = std::make_unique<PS2IopHostAdapter>(*this);
    m_iopSubsystem = std::make_unique<ps2x::iop::IopSubsystem>(*m_iopHost);

    m_eeScheduler = std::make_unique<EeScheduler>(*this);

    // Assign rather than memset: R5900Context's constructor zeroes itself and
    // then applies the COP0 reset values, which a memset here would discard.
    m_cpuContext = R5900Context{};

    // R0 is always zero in MIPS
    m_cpuContext.r[0] = _mm_set1_epi32(0);
    m_cpuContext.vu0_vf[0] = _mm_set_ps(1.0f, 0.0f, 0.0f, 0.0f);
    m_cpuContext.vu0_q = 1.0f;
    m_cpuContext.vu0_r = _mm_castsi128_ps(_mm_set1_epi32(0x3F800000));

    // Stack pointer (SP) and global pointer (GP) will be set by the loaded ELF

    m_loadedModules.clear();
    m_guestHeapBlocks.clear();
    m_guestHeapBase = kGuestHeapDefaultBase;
    m_guestHeapEnd = kGuestHeapDefaultBase;
    m_guestHeapLimit = std::min(kGuestHeapHardLimit, PS2_RAM_SIZE);
    m_guestHeapSuggestedBase = kGuestHeapDefaultBase;
    m_guestHeapConfigured = false;
    m_asyncCallbackStackFloor = std::min(kGuestHeapHardLimit, PS2_RAM_SIZE);
    m_asyncCallbackStackTop = PS2_RAM_SIZE;
}

void PS2Runtime::setDebugUiCallbacks(DebugUiCallback initCallback,
                                     DebugUiCallback drawCallback,
                                     DebugUiCallback shutdownCallback,
                                     void *userData)
{
    if (m_debugUiInitialized && m_debugUiShutdownCallback)
    {
        m_debugUiShutdownCallback(*this, m_debugUiUserData);
        m_debugUiInitialized = false;
    }

    m_debugUiInitCallback = initCallback;
    m_debugUiDrawCallback = drawCallback;
    m_debugUiShutdownCallback = shutdownCallback;
    m_debugUiUserData = userData;
}

PS2Runtime::~PS2Runtime()
{
    try
    {
        requestStop();
        stopVu1Thread();
        m_iopSubsystem.reset();
        m_iopHost.reset();
#if defined(PLATFORM_VITA)
        m_audioBackend.stopAll();
        m_audioBackend.setAudioReady(false);
#else
        if (IsAudioDeviceReady())
        {
            CloseAudioDevice();
            m_audioBackend.setAudioReady(false);
        }
#endif
        if (m_debugUiInitialized && m_debugUiShutdownCallback)
        {
            m_debugUiShutdownCallback(*this, m_debugUiUserData);
            m_debugUiInitialized = false;
        }

        if (IsWindowReady())
        {
            CloseWindow();
        }

        m_loadedModules.clear();
    }
    catch (const std::exception &e)
    {
        std::cerr << "[~PS2Runtime] cleanup exception: " << e.what() << std::endl;
    }
    catch (...)
    {
        std::cerr << "[~PS2Runtime] cleanup exception: unknown" << std::endl;
    }
}

ps2x::iop::ModuleLoadResult PS2Runtime::loadIopModule(std::string_view path, const void *arguments, uint32_t argumentSize)
{
    runHeldIopCycles();
    auto scope = m_iopHost->enterCall(nullptr, m_memory.getRDRAM());
    return m_iopSubsystem->loadModule(path, arguments, argumentSize);
}

ps2x::iop::ModuleLoadResult PS2Runtime::loadIopModuleBuffer(uint32_t guestAddress, const void *arguments, uint32_t argumentSize)
{
    runHeldIopCycles();
    auto scope = m_iopHost->enterCall(nullptr, m_memory.getRDRAM());
    return m_iopSubsystem->loadModuleBuffer(guestAddress, arguments, argumentSize);
}

bool PS2Runtime::stopIopModule(int32_t moduleId, int32_t *result)
{
    runHeldIopCycles();
    auto scope = m_iopHost->enterCall(nullptr, m_memory.getRDRAM());
    return m_iopSubsystem->stopModule(moduleId, result);
}

ps2x::iop::RpcAbi PS2Runtime::selectIopRpcAbi(const ps2x::iop::RpcAbiRequest &request) const
{
    return m_iopSubsystem->selectRpcAbi(request);
}

bool PS2Runtime::canBindIopRpc(uint32_t sid) const noexcept
{
    return m_iopSubsystem->canBindRpc(sid);
}

ps2x::iop::RpcResult PS2Runtime::handleIopRpc(uint8_t *rdram, R5900Context *ctx, ps2x::iop::RpcRequest request)
{
    runHeldIopCycles();
    const uint64_t before = m_iopSubsystem->emulatorCycles();
    ps2x::iop::RpcResult result;
    {
        auto scope = m_iopHost->enterCall(ctx, rdram);
        request.callToken = scope.token();
        result = m_iopSubsystem->handleRpc(request);
    }
    // An emulated IOP module serves the call at once, on IOP time. A blocking call (mode bit 0 clear) keeps the EE
    // waiting for that long (8 EE cycles per IOP cycle); otherwise the IOP would be serving the EE for free, and a
    // game polling a server in a tight loop would leave no IOP time for the server's own threads.
    const uint64_t served = m_iopSubsystem->emulatorCycles() - before;
    if (served != 0u && (request.mode & 1u) == 0u)
        m_eeScheduler->accountCycles(static_cast<uint32_t>(std::min<uint64_t>(served * 8u, UINT32_MAX)));
    return result;
}

void PS2Runtime::notifyIopSifTransfer(uint8_t *rdram, const ps2x::iop::SifTransfer &transfer)
{
    runHeldIopCycles();
    auto scope = m_iopHost->enterCall(nullptr, rdram);
    m_iopSubsystem->onSifTransfer(transfer);
}

void PS2Runtime::runIopUntilIdle() noexcept
{
    runHeldIopCycles();
    m_iopIdleEeCycles = m_iopSubsystem->idleEeCycles();
}

// Before any other call into the IOP. That call may wake it, so the next checkpoint asks again how long it idles.
void PS2Runtime::runHeldIopCycles() noexcept
{
    if (iopOnRealTimeClock())
    {
        runIopRealTime();
        return;
    }
    if (m_iopHeldEeCycles != 0u)
        m_iopSubsystem->runEeCycles(m_iopHeldEeCycles);
    m_iopHeldEeCycles = 0u;
    m_iopIdleEeCycles = 0u;
}

// The RealTime clock. The device has played `read` frames of the ring; the IOP is kept kLeadFrames ahead of that, so
// the ring holds about kTargetFrames: the lead also covers the samples the IOP has made but not yet handed over
// (up to one 256-frame output block plus one 64-sample sync), so that after an underrun the ring still reaches
// kTargetFrames, where playback restarts. The target only moves as the device plays, so production can neither
// drift from the device's rate nor overfill the ring; time the IOP could not be run (the EE thread stalled for longer
// than the ring lasts) is not made up afterwards, so the music resumes where it stopped instead of skipping ahead.
// Anchoring sets the target from the frames produced so far, so a ring that is fuller than the lead first drains.
void PS2Runtime::runIopRealTime() noexcept
{
    constexpr uint64_t kLeadFrames = IopAudioPlayer::kTargetFrames + 384u;
    constexpr uint64_t kIopCyclesPerFrame = 768u; // IOP cycles per 48 kHz sample
    constexpr int64_t kStallNs = 200'000'000; // the device is taken to have stopped after 200 ms without a callback
    const uint64_t eeCycles = m_iopRealTimeEeCycles;
    m_iopRealTimeEeCycles = 0u;
    IopAudioPlayer &p = iopAudioPlayer();
    const int64_t lastCallbackNs = p.lastCallbackNs.load(std::memory_order_acquire);
    const int64_t nowNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
                              std::chrono::steady_clock::now().time_since_epoch())
                              .count();
    if (!p.started || lastCallbackNs == 0 || nowNs - lastCallbackNs > kStallNs)
    {
        if (m_iopRealTimeAnchored || p.realTime.load(std::memory_order_relaxed))
            p.realTime.store(false, std::memory_order_relaxed);
        m_iopRealTimeAnchored = false;
        p.wRealTimeFallbacks.fetch_add(1u, std::memory_order_relaxed);
        if (eeCycles != 0u)
            m_iopSubsystem->runEeCycles(eeCycles);
        return;
    }
    const uint64_t read = p.read.load(std::memory_order_acquire);
    if (!m_iopRealTimeAnchored)
    {
        m_iopRealTimeAnchored = true;
        m_iopRealTimeAnchorCycle = m_iopSubsystem->emulatorCycles();
        m_iopRealTimeAnchorFrame = p.written.load(std::memory_order_relaxed);
        p.realTime.store(true, std::memory_order_relaxed);
        p.wRealTimeAnchors.fetch_add(1u, std::memory_order_relaxed);
    }
    p.wRealTimeTicks.fetch_add(1u, std::memory_order_relaxed);
    if (read + kLeadFrames < m_iopRealTimeAnchorFrame)
        return; // the ring is still draining down to the lead
    m_iopSubsystem->runToCycle(m_iopRealTimeAnchorCycle +
                               (read + kLeadFrames - m_iopRealTimeAnchorFrame) * kIopCyclesPerFrame);
}

void PS2Runtime::resetIop()
{
    m_iopHeldEeCycles = 0u;
    m_iopIdleEeCycles = 0u;
    m_iopRealTimeEeCycles = 0u;
    m_iopRealTimeAnchored = false;
    m_iopSubsystem->reset();
}

ps2x::iop::DebugSnapshot PS2Runtime::iopDebugSnapshot() const
{
    return m_iopSubsystem->debugSnapshot();
}

bool PS2Runtime::iopSdRemote(uint32_t function, const std::array<uint32_t, 5> &args, uint32_t &result)
{
    if (!m_iopSubsystem)
        return false;
    runHeldIopCycles();
    const bool handled = m_iopSubsystem->sdRemote(function, args, result);
    setIopGuestFedAudio(m_iopSubsystem->soundInputRunning());
    return handled;
}

void PS2Runtime::setIopGuestFedAudio(bool fed) noexcept
{
    if (fed == m_iopGuestFedAudio)
        return;
    m_iopGuestFedAudio = fed;
    if (fed)
    {
        // From here the IOP counts EE time on from where the RealTime clock left it.
        m_iopHeldEeCycles = 0u;
        m_iopIdleEeCycles = 0u;
        m_iopRealTimeEeCycles = 0u;
        iopAudioPlayer().realTime.store(false, std::memory_order_relaxed);
    }
    else
    {
        m_iopRealTimeAnchored = false; // reattach to the device clock at the next update
    }
}

uint32_t PS2Runtime::allocateIopMemory(uint32_t size, uint32_t alignment)
{
    if (m_iopSubsystem)
        runHeldIopCycles();
    return m_iopSubsystem ? m_iopSubsystem->allocateMemory(size, alignment) : 0u;
}

bool PS2Runtime::freeIopMemory(uint32_t address)
{
    if (m_iopSubsystem)
        runHeldIopCycles();
    return m_iopSubsystem && m_iopSubsystem->freeMemory(address);
}

bool PS2Runtime::readIopMemory(uint32_t address, void *destination, size_t size) const
{
    return m_iopSubsystem && m_iopSubsystem->readMemory(address, destination, size);
}

bool PS2Runtime::writeIopMemory(uint32_t address, const void *source, size_t size)
{
    if (m_iopSubsystem)
        runHeldIopCycles();
    return m_iopSubsystem && m_iopSubsystem->writeMemory(address, source, size);
}

bool PS2Runtime::zeroIopMemory(uint32_t address, size_t size)
{
    if (m_iopSubsystem)
        runHeldIopCycles();
    return m_iopSubsystem && m_iopSubsystem->zeroMemory(address, size);
}

bool PS2Runtime::isIopMemoryRange(uint32_t address, size_t size) const
{
    return m_iopSubsystem && m_iopSubsystem->isMemoryRange(address, size);
}

bool PS2Runtime::syncCoreSubsystems()
{
    ps2_vu1_overlap_model::sync(ps2_vu1_overlap_model::kSyncGsStub);
    // HLE GS entry points come through here before they use the GS directly.
    m_memory.vu1ThreadWait(Vu1Thread::kWaitGsStub);
    uint8_t *const rdram = m_memory.getRDRAM();
    uint8_t *const gsVram = m_memory.getGSVRAM();
    if (!rdram || !gsVram)
    {
        return false;
    }

    if (m_boundRdram == rdram && m_boundGSVram == gsVram)
    {
        return true;
    }

    m_gs.init(gsVram, static_cast<uint32_t>(PS2_GS_VRAM_SIZE), &m_memory.gs());
    m_gifArbiter.setProcessPacketFn([this](const uint8_t *data, uint32_t size)
                                    { m_gs.processGIFPacket(data, size); });
    m_memory.setGifArbiter(&m_gifArbiter);
    m_memory.setVu1MscalCallback([this](uint32_t startPC, uint32_t top, uint32_t itop)
                                 {
                                     // On the PS2X_VU1_THREAD worker the EE context belongs to the game thread: kicks
                                     // with the D/T stop bits on run inline, so here they are off.
                                     R5900Context *cpuContext = nullptr;
                                     if (!Vu1Thread::onWorker())
                                     {
                                         cpuContext = m_eeScheduler ? m_eeScheduler->currentContext() : nullptr;
                                         if (!cpuContext)
                                             cpuContext = &m_cpuContext;
                                     }
                                     m_vu1.state().dBitEnabled =
                                         cpuContext && (cpuContext->vu0_fbrst & (1u << 10)) != 0u;
                                     m_vu1.state().tBitEnabled =
                                         cpuContext && (cpuContext->vu0_fbrst & (1u << 11)) != 0u;
                                     ps2_host_profile::Scope profileScope(ps2_host_profile::kVu1);
                                     ps2_vu1_overlap_model::WorkScope overlapWork(ps2_vu1_overlap_model::kSplit);
                                     m_vu1.execute(m_memory.getVU1Code(), PS2_VU1_CODE_SIZE,
                                                   m_memory.getVU1Data(), PS2_VU1_DATA_SIZE,
                                                   m_gs, &m_memory, startPC, top, itop, 65536);
                                     if (cpuContext)
                                         cpuContext->vu0_vpu_stat =
                                             (cpuContext->vu0_vpu_stat & ~0x0600u) |
                                             (m_vu1.state().stoppedByD ? 0x0200u : 0u) |
                                             (m_vu1.state().stoppedByT ? 0x0400u : 0u); });
    m_memory.setVu1MscntCallback([this](uint32_t top, uint32_t itop)
                                 {
                                     // On the PS2X_VU1_THREAD worker the EE context belongs to the game thread: kicks
                                     // with the D/T stop bits on run inline, so here they are off.
                                     R5900Context *cpuContext = nullptr;
                                     if (!Vu1Thread::onWorker())
                                     {
                                         cpuContext = m_eeScheduler ? m_eeScheduler->currentContext() : nullptr;
                                         if (!cpuContext)
                                             cpuContext = &m_cpuContext;
                                     }
                                     m_vu1.state().dBitEnabled =
                                         cpuContext && (cpuContext->vu0_fbrst & (1u << 10)) != 0u;
                                     m_vu1.state().tBitEnabled =
                                         cpuContext && (cpuContext->vu0_fbrst & (1u << 11)) != 0u;
                                     ps2_host_profile::Scope profileScope(ps2_host_profile::kVu1);
                                     ps2_vu1_overlap_model::WorkScope overlapWork(ps2_vu1_overlap_model::kSplit);
                                     m_vu1.resume(m_memory.getVU1Code(), PS2_VU1_CODE_SIZE,
                                                  m_memory.getVU1Data(), PS2_VU1_DATA_SIZE,
                                                  m_gs, &m_memory, top, itop, 65536);
                                     if (cpuContext)
                                         cpuContext->vu0_vpu_stat =
                                             (cpuContext->vu0_vpu_stat & ~0x0600u) |
                                             (m_vu1.state().stoppedByD ? 0x0200u : 0u) |
                                             (m_vu1.state().stoppedByT ? 0x0400u : 0u); });
    // PS2X_VU1_THREAD: the game thread decides per kick whether VU1 may run on the worker (D/T stop bits off), and
    // clears VPU-STAT's D/T stop flags for each MSCAL as the inline run would.
    m_memory.setVu1ThreadHooks(
        [this]()
        {
            const R5900Context *cpuContext = m_eeScheduler ? m_eeScheduler->currentContext() : nullptr;
            if (!cpuContext)
                cpuContext = &m_cpuContext;
            return (cpuContext->vu0_fbrst & ((1u << 10) | (1u << 11))) != 0u;
        },
        [this]()
        {
            R5900Context *cpuContext = m_eeScheduler ? m_eeScheduler->currentContext() : nullptr;
            if (!cpuContext)
                cpuContext = &m_cpuContext;
            cpuContext->vu0_vpu_stat &= ~0x0600u;
        });
    resetIop();
    m_vu0.reset();
    m_vu1.reset();

    m_boundRdram = rdram;
    m_boundGSVram = gsVram;
    return true;
}

bool PS2Runtime::initialize(const char *title)
{
    try
    {
        if (!m_memory.initialize())
        {
            std::cerr << "Failed to initialize PS2 memory" << std::endl;
            return false;
        }

        if (!syncCoreSubsystems())
        {
            std::cerr << "Failed to bind runtime core subsystems" << std::endl;
            return false;
        }
        if (m_headless)
        {
            m_audioBackend.setAudioReady(false);
            return true;
        }
#if defined(PLATFORM_VITA)
        InitWindow(HOST_WINDOW_WIDTH, HOST_WINDOW_HEIGHT, title); // raylib vita does not support audio
#else
        // PS2X_VSYNC=1: swap on the monitor's refresh, so an exclusive / borderless window does not tear. Pacing stays as it was.
        const char *vsyncEnv = std::getenv("PS2X_VSYNC");
        const bool vsync = vsyncEnv && std::strcmp(vsyncEnv, "1") == 0;
        SetConfigFlags(vsync ? (FLAG_WINDOW_RESIZABLE | FLAG_VSYNC_HINT) : FLAG_WINDOW_RESIZABLE);
        // A 16:9 game mode (ps2_display_aspect.h) opens a 16:9 window of the same height.
        InitWindow(ps2_display_aspect::isWideEnabled() ? HOST_WINDOW_HEIGHT * 16 / 9 : HOST_WINDOW_WIDTH, HOST_WINDOW_HEIGHT,
                   title);
#if defined(_WIN32)
        // Not GetWindowHandle(): on Windows that is the HWND, not the GLFW window.
        g_raylibKeyCallback = glfwSetKeyCallback(glfwGetCurrentContext(), shotKeyCallback);
#endif
        InitAudioDevice();
        m_audioBackend.setAudioReady(IsAudioDeviceReady());
#endif
        SetTargetFPS(60);
        if (m_debugUiInitCallback)
        {
            m_debugUiInitCallback(*this, m_debugUiUserData);
            m_debugUiInitialized = true;
        }

        return true;
    }
    catch (const std::exception &e)
    {
        std::cerr << "Failed to initialize PS2 runtime: " << e.what() << std::endl;
    }
    catch (...)
    {
        std::cerr << "Failed to initialize PS2 runtime: unknown exception" << std::endl;
    }

    return false;
}

bool PS2Runtime::loadELF(const std::string &elfPath)
{
    configureIoPathsFromElf(elfPath);

    std::ifstream file(elfPath, std::ios::binary);
    if (!file)
    {
        std::cerr << "Failed to open ELF file: " << elfPath << std::endl;
        return false;
    }

    file.seekg(0, std::ios::end);
    const std::streamoff fileSize = file.tellg();
    if (fileSize < static_cast<std::streamoff>(sizeof(ElfHeader)))
    {
        std::cerr << "ELF file is too small: " << elfPath << std::endl;
        return false;
    }
    file.seekg(0, std::ios::beg);

    ElfHeader header{};
    if (!file.read(reinterpret_cast<char *>(&header), sizeof(header)))
    {
        std::cerr << "Failed to read ELF header from: " << elfPath << std::endl;
        return false;
    }

    if (header.magic != ELF_MAGIC)
    {
        std::cerr << "Invalid ELF magic number" << std::endl;
        return false;
    }

    if (header.elf_class != 1u || header.endianness != 1u)
    {
        std::cerr << "Unsupported ELF format (expected 32-bit little-endian)." << std::endl;
        return false;
    }

    if (header.machine != EM_MIPS || header.type != ET_EXEC)
    {
        std::cerr << "Not a MIPS executable ELF file" << std::endl;
        return false;
    }

    if (header.phnum != 0u && header.phentsize < sizeof(ProgramHeader))
    {
        std::cerr << "Unsupported ELF program-header entry size: " << header.phentsize << std::endl;
        return false;
    }

    const uint64_t programHeaderTableEnd =
        static_cast<uint64_t>(header.phoff) +
        static_cast<uint64_t>(header.phnum) * static_cast<uint64_t>(header.phentsize);
    if (programHeaderTableEnd > static_cast<uint64_t>(fileSize))
    {
        std::cerr << "ELF program-header table is out of range." << std::endl;
        return false;
    }

    m_cpuContext.pc = header.entry;
    m_debugPc.store(m_cpuContext.pc, std::memory_order_relaxed);

    uint32_t maxLoadedRdramEnd = kGuestHeapDefaultBase;
    uint32_t moduleBase = std::numeric_limits<uint32_t>::max();
    uint32_t moduleEnd = 0u;
    bool loadedAnySegment = false;

    for (uint16_t i = 0; i < header.phnum; i++)
    {
        const uint64_t phOffset =
            static_cast<uint64_t>(header.phoff) +
            static_cast<uint64_t>(i) * static_cast<uint64_t>(header.phentsize);
        if (phOffset + sizeof(ProgramHeader) > static_cast<uint64_t>(fileSize))
        {
            std::cerr << "ELF program header " << i << " is out of range." << std::endl;
            return false;
        }

        ProgramHeader ph{};
        file.seekg(static_cast<std::streamoff>(phOffset), std::ios::beg);
        if (!file.read(reinterpret_cast<char *>(&ph), sizeof(ph)))
        {
            std::cerr << "Failed to read ELF program header " << i << std::endl;
            return false;
        }

        if (ph.type != PT_LOAD || ph.memsz == 0u)
        {
            continue;
        }

        if (ph.filesz > ph.memsz)
        {
            std::cerr << "ELF segment " << i << " has filesz > memsz." << std::endl;
            return false;
        }

        const uint64_t segmentFileEnd = static_cast<uint64_t>(ph.offset) + static_cast<uint64_t>(ph.filesz);
        if (segmentFileEnd > static_cast<uint64_t>(fileSize))
        {
            std::cerr << "ELF segment " << i << " exceeds file bounds." << std::endl;
            return false;
        }

        const bool scratch =
            ph.vaddr >= PS2_SCRATCHPAD_BASE &&
            ph.vaddr < (PS2_SCRATCHPAD_BASE + PS2_SCRATCHPAD_SIZE);

        uint32_t physAddr = 0u;
        try
        {
            physAddr = m_memory.translateAddress(ph.vaddr);
        }
        catch (const std::exception &e)
        {
            std::cerr << "Failed to translate ELF segment " << i
                      << " virtual address 0x" << std::hex << ph.vaddr
                      << std::dec << ": " << e.what() << std::endl;
            return false;
        }
        const uint64_t regionSize = scratch ? static_cast<uint64_t>(PS2_SCRATCHPAD_SIZE)
                                            : static_cast<uint64_t>(PS2_RAM_SIZE);
        const uint64_t segmentMemEnd = static_cast<uint64_t>(physAddr) + static_cast<uint64_t>(ph.memsz);
        if (segmentMemEnd > regionSize)
        {
            std::cerr << "ELF segment " << i << " exceeds "
                      << (scratch ? "scratchpad" : "RDRAM")
                      << " bounds (vaddr=0x" << std::hex << ph.vaddr
                      << " memsz=0x" << ph.memsz << std::dec << ")." << std::endl;
            return false;
        }

        uint8_t *destBase = scratch ? m_memory.getScratchpad() : m_memory.getRDRAM();
        if (!destBase)
        {
            std::cerr << "ELF segment " << i << " has no destination memory backing." << std::endl;
            return false;
        }

        uint8_t *dest = destBase + physAddr;
        if (ph.filesz > 0u)
        {
            file.seekg(static_cast<std::streamoff>(ph.offset), std::ios::beg);
            if (!file.read(reinterpret_cast<char *>(dest), ph.filesz))
            {
                std::cerr << "Failed to read ELF segment " << i << " payload." << std::endl;
                return false;
            }
        }

        if (ph.memsz > ph.filesz)
        {
            std::memset(dest + ph.filesz, 0, ph.memsz - ph.filesz);
        }

        RUNTIME_LOG("Loading segment: 0x" << std::hex << ph.vaddr
                                          << " - 0x" << (static_cast<uint64_t>(ph.vaddr) + static_cast<uint64_t>(ph.memsz))
                                          << " (filesz: 0x" << ph.filesz
                                          << ", memsz: 0x" << ph.memsz << ")"
                                          << std::dec << std::endl);

        if (!scratch)
        {
            maxLoadedRdramEnd = std::max(maxLoadedRdramEnd, static_cast<uint32_t>(segmentMemEnd));
        }

        if (ph.flags & 0x1u) // PF_X
        {
            const uint64_t execEnd = static_cast<uint64_t>(ph.vaddr) + static_cast<uint64_t>(ph.filesz);
            if (execEnd <= std::numeric_limits<uint32_t>::max())
            {
                m_memory.registerCodeRegion(ph.vaddr, static_cast<uint32_t>(execEnd));
            }
        }

        loadedAnySegment = true;
        moduleBase = std::min(moduleBase, ph.vaddr);
        const uint64_t segmentVirtualEnd = static_cast<uint64_t>(ph.vaddr) + static_cast<uint64_t>(ph.memsz);
        const uint32_t clampedVirtualEnd =
            (segmentVirtualEnd > std::numeric_limits<uint32_t>::max())
                ? std::numeric_limits<uint32_t>::max()
                : static_cast<uint32_t>(segmentVirtualEnd);
        moduleEnd = std::max(moduleEnd, clampedVirtualEnd);
    }

    if (!loadedAnySegment)
    {
        std::cerr << "ELF contains no loadable PT_LOAD segments." << std::endl;
        return false;
    }

    if (maxLoadedRdramEnd > PS2_RAM_SIZE)
    {
        maxLoadedRdramEnd = PS2_RAM_SIZE;
    }

    const uint32_t paddedEnd = (maxLoadedRdramEnd > (PS2_RAM_SIZE - kGuestHeapSafetyPad))
                                   ? PS2_RAM_SIZE
                                   : (maxLoadedRdramEnd + kGuestHeapSafetyPad);
    const uint32_t suggestedHeapBase = alignGuestHeapValue(paddedEnd, kGuestHeapDefaultAlignment);
    {
        std::lock_guard<std::mutex> lock(m_guestHeapMutex);
        if (!m_guestHeapConfigured)
        {
            const uint32_t hardLimit = std::min(kGuestHeapHardLimit, PS2_RAM_SIZE);
            m_guestHeapSuggestedBase = std::min(suggestedHeapBase, hardLimit);
            m_guestHeapBase = m_guestHeapSuggestedBase;
            m_guestHeapEnd = m_guestHeapSuggestedBase;
            m_guestHeapLimit = hardLimit;
        }
    }
    {
        std::lock_guard<std::mutex> lock(m_asyncCallbackStackMutex);
        const uint32_t hardLimit = std::min(kGuestHeapHardLimit, PS2_RAM_SIZE);
        m_asyncCallbackStackFloor = std::min(std::max(hardLimit, suggestedHeapBase), PS2_RAM_SIZE);
        m_asyncCallbackStackTop = PS2_RAM_SIZE;
    }

    LoadedModule module;
    module.name = elfPath.substr(elfPath.find_last_of("/\\") + 1);
    module.baseAddress = (moduleBase == std::numeric_limits<uint32_t>::max()) ? 0x00100000u : moduleBase;
    module.size = (moduleEnd > module.baseAddress) ? static_cast<size_t>(moduleEnd - module.baseAddress) : 0u;
    module.active = true;

    m_loadedModules.push_back(module);

    uint32_t elfCrc32 = 0u;
    const bool elfCrc32Valid = computeFileCrc32(elfPath, elfCrc32);
    if (!elfCrc32Valid)
    {
        std::cerr << "[ps2xIOP] failed to compute ELF CRC32 for '" << elfPath << "'" << std::endl;
    }
    ps2x::iop::GameIdentity identity;
    identity.elfName = module.name;
    identity.entryPoint = m_cpuContext.pc;
    identity.crc32 = elfCrc32;
    std::string romError;
    if (!m_romDevice.configure(identity, &romError))
    {
        std::cerr << "[ROM0] failed to configure profile: " << romError << std::endl;
        return false;
    }

    resetIop();

    ps2_game_overrides::applyMatching(*this,
                                      elfPath,
                                      m_cpuContext.pc,
                                      elfCrc32,
                                      elfCrc32Valid);

    RUNTIME_LOG("ELF file loaded successfully. Entry point: 0x" << std::hex << m_cpuContext.pc << std::dec);
    return true;
}

const PS2Runtime::IoPaths &PS2Runtime::getIoPaths()
{
    return runtimeIoPaths();
}

void PS2Runtime::setIoPaths(const IoPaths &paths)
{
    IoPaths normalized = paths;
    normalized.elfPath = normalizeAbsolutePath(normalized.elfPath);
    normalized.elfDirectory = normalizeAbsolutePath(normalized.elfDirectory);
    normalized.hostRoot = normalizeAbsolutePath(normalized.hostRoot);
    normalized.cdRoot = normalizeAbsolutePath(normalized.cdRoot);
    normalized.mcRoot = normalizeAbsolutePath(normalized.mcRoot);
    normalized.cdImage = normalizeAbsolutePath(normalized.cdImage);

    if (normalized.elfDirectory.empty() && !normalized.elfPath.empty())
    {
        normalized.elfDirectory = normalized.elfPath.parent_path();
    }

    if (normalized.hostRoot.empty())
    {
        normalized.hostRoot = normalized.elfDirectory;
    }
    if (normalized.cdRoot.empty())
    {
        normalized.cdRoot = normalized.elfDirectory;
    }
    if (normalized.mcRoot.empty())
    {
        normalized.mcRoot = normalized.elfDirectory / "mc0";
    }

    runtimeIoPaths() = normalized;
}

void PS2Runtime::configureIoPathsFromElf(const std::string &elfPath)
{
    IoPaths paths = runtimeIoPaths();
    paths.elfPath = normalizeAbsolutePath(std::filesystem::path(elfPath));
    if (!paths.elfPath.empty())
    {
        paths.elfDirectory = paths.elfPath.parent_path();
    }

    if (!paths.elfDirectory.empty())
    {
        paths.hostRoot = paths.elfDirectory;
        paths.cdRoot = paths.elfDirectory;
        if (!g_memoryCardRootPinned)
            paths.mcRoot = paths.elfDirectory / "mc0";
    }

    setIoPaths(paths);
}

void PS2Runtime::setMemoryCardRoot(const std::filesystem::path &root)
{
    IoPaths paths = runtimeIoPaths();
    paths.mcRoot = root;
    g_memoryCardRootPinned = !root.empty();
    setIoPaths(paths);
}

namespace
{
    bool generatedFunctionTableSlot(uint32_t address, uint32_t &slot)
    {
        if ((address & 3u) != 0u || g_ps2RecompiledFunctionTableSlotCount == 0u)
        {
            return false;
        }

        if (address < g_ps2RecompiledFunctionTableBase || address >= g_ps2RecompiledFunctionTableEnd)
        {
            return false;
        }

        const uint32_t offset = address - g_ps2RecompiledFunctionTableBase;
        slot = offset >> 2;
        return slot < g_ps2RecompiledFunctionTableSlotCount;
    }
}

bool PS2Runtime::replaceFunction(uint32_t address, RecompiledFunction func)
{
    uint32_t slot = 0u;
    if (!generatedFunctionTableSlot(address, slot))
    {
        std::cerr << "[function-table] cannot replace guest PC 0x" << std::hex << address
                  << ": outside generated dense table [0x" << g_ps2RecompiledFunctionTableBase
                  << ", 0x" << g_ps2RecompiledFunctionTableEnd << ")"
                  << std::dec << std::endl;
        return false;
    }

    g_ps2RecompiledFunctionTable[slot] = func;
    return true;
}

bool PS2Runtime::registerFunction(uint32_t address, RecompiledFunction func)
{
    return replaceFunction(address, func);
}

bool PS2Runtime::hasFunction(uint32_t address) const
{
    uint32_t slot = 0u;
    return generatedFunctionTableSlot(address, slot) && g_ps2RecompiledFunctionTable[slot] != nullptr;
}

const char *describeGuestBranchKind(PS2Runtime::GuestBranchKind kind)
{
    switch (kind)
    {
    case PS2Runtime::GuestBranchKind::DirectJump:
        return "DirectJump";
    case PS2Runtime::GuestBranchKind::DirectCall:
        return "DirectCall";
    case PS2Runtime::GuestBranchKind::IndirectJump:
        return "IndirectJump";
    case PS2Runtime::GuestBranchKind::IndirectCall:
        return "IndirectCall";
    case PS2Runtime::GuestBranchKind::Return:
        return "Return";
    default:
        return "Unknown";
    }
}

PS2Runtime::RecompiledFunction PS2Runtime::lookupFunction(uint32_t address)
{
    pushDispatchPc(address);

    uint32_t slot = 0u;
    if (generatedFunctionTableSlot(address, slot))
    {
        RecompiledFunction fn = g_ps2RecompiledFunctionTable[slot];
        if (fn != nullptr)
        {
            return fn;
        }
    }

    std::cerr << "Error: No exact recompiled function for guest PC 0x" << std::hex << address
              << " tableBase=0x" << g_ps2RecompiledFunctionTableBase
              << " tableEnd=0x" << g_ps2RecompiledFunctionTableEnd
              << " codeRegion=" << (m_memory.isCodeAddress(address) ? "yes" : "no")
              << " trace=" << formatDispatchHistory()
              << std::dec << std::endl;

    static RecompiledFunction missingFunction = [](uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t badPc = ctx->pc;
        runtime->reportMissingFunction(rdram,
                                       ctx,
                                       badPc,
                                       0u,
                                       PS2Runtime::GuestBranchKind::IndirectJump,
                                       "dispatch");
    };

    return missingFunction;
}

void PS2Runtime::setMissingFunctionPolicy(MissingFunctionPolicy policy)
{
    m_missingFunctionPolicy.store(static_cast<uint32_t>(policy), std::memory_order_release);
}

PS2Runtime::MissingFunctionPolicy PS2Runtime::missingFunctionPolicy() const
{
    return static_cast<MissingFunctionPolicy>(m_missingFunctionPolicy.load(std::memory_order_acquire));
}

void PS2Runtime::resetMissingFunctionReportOnce()
{
    m_missingFunctionReported.store(false, std::memory_order_release);
}

void PS2Runtime::reportMissingFunction(uint8_t *rdram,
                                       R5900Context *ctx,
                                       uint32_t targetPc,
                                       uint32_t sourcePc,
                                       GuestBranchKind kind,
                                       const char *debugName)
{
    const MissingFunctionPolicy policy = missingFunctionPolicy();
    const bool firstReport = !m_missingFunctionReported.exchange(true, std::memory_order_acq_rel);

    const uint32_t pc = ctx->pc;
    const uint32_t ra = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[31], 0));
    const uint32_t sp = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[29], 0));
    const uint32_t gp = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[28], 0));
    const uint32_t a0 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[4], 0));
    const uint32_t a1 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[5], 0));
    const uint32_t a2 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[6], 0));
    const uint32_t a3 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[7], 0));
    const uint32_t s0 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[16], 0));
    const uint32_t s1 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[17], 0));
    const uint32_t v0 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[2], 0));
    const uint32_t v1 = static_cast<uint32_t>(_mm_extract_epi32(ctx->r[3], 0));

    auto readGuestU32At = [rdram](uint32_t addr, uint32_t &out) -> bool
    {
        // TODO this !rdram exist only because of test fix those test later
        if (!rdram || addr > PS2_RAM_SIZE - sizeof(uint32_t))
        {
            out = 0u;
            return false;
        }

        std::memcpy(&out, rdram + addr, sizeof(uint32_t));
        return true;
    };

    auto readGuestU32Offset = [&readGuestU32At](uint32_t base, uint32_t offset, uint32_t &out) -> bool
    {
        if (base > PS2_RAM_SIZE - sizeof(uint32_t) || offset > PS2_RAM_SIZE - sizeof(uint32_t) - base)
        {
            out = 0u;
            return false;
        }

        return readGuestU32At(base + offset, out);
    };

    uint32_t a0Word0 = 0u;
    uint32_t a0Word4 = 0u;
    uint32_t a0Word8 = 0u;
    uint32_t a0WordC = 0u;
    const bool a0Readable =
        readGuestU32Offset(a0, 0x00u, a0Word0) &&
        readGuestU32Offset(a0, 0x04u, a0Word4) &&
        readGuestU32Offset(a0, 0x08u, a0Word8) &&
        readGuestU32Offset(a0, 0x0cu, a0WordC);

    uint32_t s0Word0 = 0u;
    uint32_t s0Word4 = 0u;
    uint32_t s0Word8 = 0u;
    uint32_t s0WordC = 0u;
    const bool s0Readable =
        readGuestU32Offset(s0, 0x00u, s0Word0) &&
        readGuestU32Offset(s0, 0x04u, s0Word4) &&
        readGuestU32Offset(s0, 0x08u, s0Word8) &&
        readGuestU32Offset(s0, 0x0cu, s0WordC);

    uint32_t recordWord0 = 0u;
    uint32_t recordWord4 = 0u;
    uint32_t recordWord8 = 0u;
    uint32_t recordWordC = 0u;
    const bool recordReadable =
        s0Readable && s0Word4 != 0u &&
        readGuestU32Offset(s0Word4, 0x00u, recordWord0) &&
        readGuestU32Offset(s0Word4, 0x04u, recordWord4) &&
        readGuestU32Offset(s0Word4, 0x08u, recordWord8) &&
        readGuestU32Offset(s0Word4, 0x0cu, recordWordC);

    uint32_t vtableSlot0 = 0u;
    uint32_t vtableSlot4 = 0u;
    uint32_t vtableSlot8 = 0u;
    uint32_t vtableSlotC = 0u;
    const bool vtableReadable =
        a0Readable && a0Word0 != 0u &&
        readGuestU32Offset(a0Word0, 0x00u, vtableSlot0) &&
        readGuestU32Offset(a0Word0, 0x04u, vtableSlot4) &&
        readGuestU32Offset(a0Word0, 0x08u, vtableSlot8) &&
        readGuestU32Offset(a0Word0, 0x0cu, vtableSlotC);

    if (firstReport)
    {
        std::ostringstream oss;
        oss << "[guest-branch:missing-target] kind=" << describeGuestBranchKind(kind)
            << " op=" << (debugName ? debugName : "<unknown>")
            << " source=0x" << std::hex << sourcePc
            << " target=0x" << targetPc
            << " pc=0x" << pc
            << " ra=0x" << ra
            << " sp=0x" << sp
            << " gp=0x" << gp
            << " a0=0x" << a0
            << " a1=0x" << a1
            << " a2=0x" << a2
            << " a3=0x" << a3
            << " s0=0x" << s0
            << " s1=0x" << s1
            << " v0=0x" << v0
            << " v1=0x" << v1
            << " a0Readable=" << (a0Readable ? "yes" : "no")
            << " a0[0]=0x" << a0Word0
            << " a0[4]=0x" << a0Word4
            << " a0[8]=0x" << a0Word8
            << " a0[c]=0x" << a0WordC
            << " s0Readable=" << (s0Readable ? "yes" : "no")
            << " s0[0]=0x" << s0Word0
            << " s0[4]=0x" << s0Word4
            << " s0[8]=0x" << s0Word8
            << " s0[c]=0x" << s0WordC
            << " recordReadable=" << (recordReadable ? "yes" : "no")
            << " record[0]=0x" << recordWord0
            << " record[4]=0x" << recordWord4
            << " record[8]=0x" << recordWord8
            << " record[c]=0x" << recordWordC
            << " vtableReadable=" << (vtableReadable ? "yes" : "no")
            << " vtbl[0]=0x" << vtableSlot0
            << " vtbl[4]=0x" << vtableSlot4
            << " vtbl[8]=0x" << vtableSlot8
            << " vtbl[c]=0x" << vtableSlotC
            << " codeRegion=" << (m_memory.isCodeAddress(targetPc) ? "yes" : "no")
            << " policy=" << static_cast<uint32_t>(policy)
            << " trace=" << formatDispatchHistory()
            << std::dec;

        static std::mutex s_missingFunctionLogMutex;
        {
            std::lock_guard<std::mutex> lock(s_missingFunctionLogMutex);
            std::cerr << oss.str() << std::endl;
        }
    }

    if (firstReport && policy == MissingFunctionPolicy::BreakOnce)
    {
#if defined(_MSC_VER)
        __debugbreak();
#endif // TODO others breakpoints
    }

    if (ctx)
    {
        ctx->pc = targetPc;
    }

    if (policy == MissingFunctionPolicy::Stop)
    {
        requestStop();
    }
}

bool PS2Runtime::dispatchGuestBranch(uint8_t *rdram,
                                     R5900Context *ctx,
                                     uint32_t targetPc,
                                     uint32_t sourcePc,
                                     uint32_t fallthroughPc,
                                     GuestBranchKind kind,
                                     const char *debugName)
{
    ctx->pc = targetPc;
    const bool isCall = (kind == GuestBranchKind::DirectCall || kind == GuestBranchKind::IndirectCall);

    // Every inter-function transfer is also a deterministic EE safe point.
    // Backward edges inside generated functions use eeCheckpointDue(), while
    // this charge bounds straight-line call chains that have no local loop.
    if (m_eeScheduler && m_eeScheduler->checkpointDue(EeScheduler::kGuestDispatchCycles))
    {
        return false;
    }

    if (!isCall)
    {
        if (!hasFunction(targetPc))
        {
            reportMissingFunction(rdram, ctx, targetPc, sourcePc, kind, debugName);
        }

        ctx->pc = targetPc;
        return false;
    }

    if (!hasFunction(targetPc))
    {
        reportMissingFunction(rdram, ctx, targetPc, sourcePc, kind, debugName);

        const MissingFunctionPolicy policy = missingFunctionPolicy();

        if (policy == MissingFunctionPolicy::SkipCallDebug && isCall)
        {
            ctx->pc = fallthroughPc;
            return true;
        }

        if (policy == MissingFunctionPolicy::ContinueToTarget)
        {
            ctx->pc = targetPc;
            // if you need the app to keep open to open debug pannel change this to false
            return false;
        }

        return false;
    }

    RecompiledFunction targetFn = lookupFunction(targetPc);
    const uint32_t entryPc = ctx->pc;
    targetFn(rdram, ctx, this);

    if (m_eeScheduler && m_eeScheduler->transferPending())
    {
        return false; // the thread was switched out inside a syscall stub; $pc is where it resumes
    }

    if (isStopRequested() || ctx->pc == 0u)
    {
        return false;
    }

    if (ctx->pc == entryPc)
    {
        ctx->pc = fallthroughPc;
    }

    return ctx->pc == fallthroughPc;
}

void PS2Runtime::SignalException(R5900Context *ctx, PS2Exception exception)
{
    if (exception == EXCEPTION_INTEGER_OVERFLOW)
    {
        HandleIntegerOverflow(ctx);
        return;
    }

    raiseCop0Exception(ctx, static_cast<uint32_t>(exception),
                       exception == EXCEPTION_TLB_REFILL);
}

void PS2Runtime::executeVU0Microprogram(uint8_t *rdram, R5900Context *ctx, uint32_t address)
{
    (void)rdram;

    uint8_t *const vu0Code = m_memory.getVU0Code();
    uint8_t *const vu0Data = m_memory.getVU0Data();
    const uint32_t startPC = address & ~0x7u;

    if (!vu0Code || !vu0Data || startPC + 8u > PS2_VU0_CODE_SIZE)
    {
        seedVu0IdleSuccess(ctx);
        return;
    }

    m_vu0.reset();
    copyVu0ContextToState(ctx, m_vu0.state());
    m_vu0.execute(vu0Code, PS2_VU0_CODE_SIZE,
                  vu0Data, PS2_VU0_DATA_SIZE,
                  m_gs, &m_memory,
                  startPC, 0u, ctx->vu0_itop, 4096);
    copyVu0StateToContext(m_vu0.state(), ctx);
}

void PS2Runtime::vu0StartMicroProgram(uint8_t *rdram, R5900Context *ctx, uint32_t address)
{
    // VCALLMS and VCALLMSR both route here.
    executeVU0Microprogram(rdram, ctx, address);
}

void PS2Runtime::handleSyscall(uint8_t *rdram, R5900Context *ctx)
{
    handleSyscall(rdram, ctx, 0);
}

void PS2Runtime::handleSyscall(uint8_t *rdram, R5900Context *ctx, uint32_t encodedSyscallId)
{
    if (ctx->in_delay_slot)
    {
        throw std::runtime_error("Attempted to execute a syscall inside a branch delay slot! "
                                 "This breaks the atomic basic block model and is structurally unsupported by the emulator.");
    }

    const uint32_t syscallId = (encodedSyscallId != 0u)
                                   ? encodedSyscallId
                                   : getRegU32(ctx, 3); // $v1 / $3 is the EE kernel syscall number

    // ctx->pc already holds the instruction after the syscall. When that is `jr $ra` (a libkernel stub), a thread
    // switch the syscall requests can return instead of unwinding (EeScheduler::setSyscallReturnsDirectly).
    static const bool lazySwitch = []
    {
        const char *env = std::getenv("PS2X_EE_LAZY_SWITCH");
        return !env || std::strcmp(env, "0") != 0;
    }();
    struct StubScope
    {
        EeScheduler *scheduler;
        ~StubScope()
        {
            if (scheduler)
                scheduler->setSyscallReturnsDirectly(false);
        }
    } stubScope{nullptr};
    if (lazySwitch && m_eeScheduler)
    {
        uint32_t next = 0;
        std::memcpy(&next, rdram + (ctx->pc & PS2_RAM_MASK), sizeof(next));
        if (next == 0x03E00008u) // jr $ra
        {
            m_eeScheduler->setSyscallReturnsDirectly(true);
            stubScope.scheduler = m_eeScheduler.get();
        }
    }

    if (ps2_syscalls::dispatchNumericSyscall(syscallId, rdram, ctx, this))
    {
        return;
    }

    // God help you
    ps2_syscalls::TODO(rdram, ctx, this, encodedSyscallId);
}

void PS2Runtime::handleBreak(uint8_t *rdram, R5900Context *ctx)
{
    raiseCop0Exception(ctx, EXCEPTION_BREAKPOINT);
}

void PS2Runtime::drainCompletedDmacHandlers(uint8_t *rdram)
{
    for (uint32_t cause : m_memory.consumeCompletedDmacCauses())
    {
        ps2_syscalls::dispatchDmacHandlersForCause(rdram, this, cause);
    }
    for (uint32_t cause : m_memory.consumeCompletedIntcCauses())
    {
        if (vif1IrqTraceEnabled())
            std::fprintf(stderr, "[vif1-irq] dispatch INTC %u\n", cause);
        eeScheduler().dispatchIrq(false, cause);
    }
}

void PS2Runtime::handleTrap(uint8_t *rdram, R5900Context *ctx)
{
    raiseCop0Exception(ctx, EXCEPTION_TRAP);
}

void PS2Runtime::handleTLBR(uint8_t *rdram, R5900Context *ctx)
{
    uint32_t vpn = 0;
    uint32_t pfn = 0;
    uint32_t mask = 0;
    bool valid = false;

    const uint32_t index = ctx->cop0_index & 0x3Fu;
    if (!m_memory.tlbRead(index, vpn, pfn, mask, valid))
    {
        raiseCop0Exception(ctx, EXCEPTION_RESERVED_INSTRUCTION);
        return;
    }

    // Preserve low ASID bits in EntryHi.
    ctx->cop0_entryhi = (ctx->cop0_entryhi & 0x00000FFFu) | (vpn & 0xFFFFF000u);
    ctx->cop0_entrylo0 = (ctx->cop0_entrylo0 & ~0x03FFFFC2u) |
                         ((pfn & 0x000FFFFFu) << 6) |
                         (valid ? 0x2u : 0u);
    ctx->cop0_pagemask = mask & 0x01FFE000u;
}

void PS2Runtime::handleTLBWI(uint8_t *rdram, R5900Context *ctx)
{
    const uint32_t index = ctx->cop0_index & 0x3Fu;
    const uint32_t vpn = ctx->cop0_entryhi & 0xFFFFF000u;
    const uint32_t pfn = (ctx->cop0_entrylo0 >> 6) & 0x000FFFFFu;
    const uint32_t mask = ctx->cop0_pagemask & 0x01FFE000u;
    const bool valid = (ctx->cop0_entrylo0 & 0x2u) != 0u;

    if (!m_memory.tlbWrite(index, vpn, pfn, mask, valid))
    {
        raiseCop0Exception(ctx, EXCEPTION_RESERVED_INSTRUCTION);
    }
}

void PS2Runtime::handleTLBWR(uint8_t *rdram, R5900Context *ctx)
{
    const uint32_t entryCount = static_cast<uint32_t>(m_memory.tlbEntryCount());
    if (entryCount == 0)
    {
        raiseCop0Exception(ctx, EXCEPTION_RESERVED_INSTRUCTION);
        return;
    }

    const uint32_t wired = std::min(ctx->cop0_wired, entryCount - 1);
    uint32_t random = ctx->cop0_random % entryCount;
    if (random < wired)
    {
        random = wired;
    }

    const uint32_t vpn = ctx->cop0_entryhi & 0xFFFFF000u;
    const uint32_t pfn = (ctx->cop0_entrylo0 >> 6) & 0x000FFFFFu;
    const uint32_t mask = ctx->cop0_pagemask & 0x01FFE000u;
    const bool valid = (ctx->cop0_entrylo0 & 0x2u) != 0u;

    if (!m_memory.tlbWrite(random, vpn, pfn, mask, valid))
    {
        raiseCop0Exception(ctx, EXCEPTION_RESERVED_INSTRUCTION);
        return;
    }

    // Keep COP0 bookkeeping in sync with the selected slot.
    ctx->cop0_index = (ctx->cop0_index & ~0x3Fu) | (random & 0x3Fu);
    ctx->cop0_random = (random <= wired) ? (entryCount - 1) : (random - 1);
}

void PS2Runtime::handleTLBP(uint8_t *rdram, R5900Context *ctx)
{
    const int32_t index = m_memory.tlbProbe(ctx->cop0_entryhi & 0xFFFFF000u);
    if (index >= 0)
    {
        ctx->cop0_index = (ctx->cop0_index & ~0x8000003Fu) |
                          (static_cast<uint32_t>(index) & 0x3Fu);
    }
    else
    {
        // MIPS sets probe failure bit (P) in Index[31].
        ctx->cop0_index |= 0x80000000u;
    }
}

void PS2Runtime::clearLLBit(R5900Context *ctx)
{
    // LL/SC reservation is tracked separately from COP0 Status.
    ctx->llbit = 0;
    ctx->lladdr = 0;
}

uint32_t PS2Runtime::alignGuestHeapValue(uint32_t value, uint32_t alignment)
{
    if (alignment == 0)
    {
        return value;
    }

    const uint32_t mask = alignment - 1u;
    if (value > (std::numeric_limits<uint32_t>::max() - mask))
    {
        return std::numeric_limits<uint32_t>::max();
    }
    return (value + mask) & ~mask;
}

bool PS2Runtime::isGuestHeapAlignmentValid(uint32_t alignment)
{
    return alignment != 0u && (alignment & (alignment - 1u)) == 0u;
}

uint32_t PS2Runtime::normalizeGuestHeapAlignment(uint32_t alignment)
{
    if (!isGuestHeapAlignmentValid(alignment))
    {
        return kGuestHeapDefaultAlignment;
    }
    return std::max(alignment, kGuestHeapDefaultAlignment);
}

uint32_t PS2Runtime::clampGuestHeapBase(uint32_t guestBase) const
{
    uint32_t normalized = guestBase;
    if (normalized >= PS2_RAM_SIZE)
    {
        normalized &= PS2_RAM_MASK;
    }
    const uint32_t hardLimit = std::min(kGuestHeapHardLimit, PS2_RAM_SIZE);
    return std::min(normalized, hardLimit);
}

uint32_t PS2Runtime::clampGuestHeapLimit(uint32_t guestLimit) const
{
    // The heap must stay below the runtime's interrupt/callback stack region (by default the top
    // 1 MB of RAM). When that region has been moved below the ELF image, RAM itself is the limit.
    uint32_t hardLimit = PS2_RAM_SIZE;
    {
        std::lock_guard<std::mutex> lock(m_asyncCallbackStackMutex);
        if (m_asyncCallbackStackFloor >= kGuestHeapDefaultBase)
            hardLimit = std::min(hardLimit, m_asyncCallbackStackFloor);
    }
    if (guestLimit == 0u || guestLimit > hardLimit)
    {
        return hardLimit;
    }
    return guestLimit;
}

void PS2Runtime::resetGuestHeapLocked(uint32_t guestBase, uint32_t guestLimit)
{
    uint32_t base = alignGuestHeapValue(clampGuestHeapBase(guestBase), kGuestHeapDefaultAlignment);
    uint32_t limit = clampGuestHeapLimit(guestLimit);
    if (base == 0u)
    {
        const uint32_t fallbackBase = (m_guestHeapSuggestedBase != 0u) ? m_guestHeapSuggestedBase : kGuestHeapDefaultBase;
        base = alignGuestHeapValue(clampGuestHeapBase(fallbackBase), kGuestHeapDefaultAlignment);
    }

    if (limit <= base)
    {
        base = alignGuestHeapValue(clampGuestHeapBase(m_guestHeapSuggestedBase), kGuestHeapDefaultAlignment);
        limit = clampGuestHeapLimit(0u);
    }

    if (limit <= base)
    {
        base = 0u;
        limit = 0u;
    }

    m_guestHeapBlocks.clear();
    if (limit > base)
    {
        m_guestHeapBlocks.push_back({base, limit - base, true});
    }

    m_guestHeapBase = base;
    m_guestHeapEnd = base;
    m_guestHeapLimit = limit;
    m_guestHeapConfigured = true;
}

void PS2Runtime::ensureGuestHeapInitializedLocked()
{
    if (m_guestHeapConfigured)
    {
        return;
    }

    const uint32_t suggested = (m_guestHeapSuggestedBase == 0u) ? kGuestHeapDefaultBase : m_guestHeapSuggestedBase;
    resetGuestHeapLocked(suggested, clampGuestHeapLimit(0u));
}

int32_t PS2Runtime::findGuestHeapBlockIndexLocked(uint32_t guestAddr) const
{
    const uint32_t normalizedAddr = guestAddr & PS2_RAM_MASK;
    for (size_t i = 0; i < m_guestHeapBlocks.size(); ++i)
    {
        const GuestHeapBlock &block = m_guestHeapBlocks[i];
        if (!block.free && block.addr == normalizedAddr)
        {
            return static_cast<int32_t>(i);
        }
    }
    return -1;
}

uint32_t PS2Runtime::allocateGuestBlockLocked(uint32_t size, uint32_t alignment)
{
    if (size == 0u)
    {
        return 0u;
    }

    const uint32_t normalizedAlignment = normalizeGuestHeapAlignment(alignment);
    if (size > (std::numeric_limits<uint32_t>::max() - (kGuestHeapDefaultAlignment - 1u)))
    {
        return 0u;
    }

    const uint32_t allocSize = alignGuestHeapValue(size, kGuestHeapDefaultAlignment);
    if (allocSize == 0u)
    {
        return 0u;
    }

    for (size_t i = 0; i < m_guestHeapBlocks.size(); ++i)
    {
        const GuestHeapBlock block = m_guestHeapBlocks[i];
        if (!block.free)
        {
            continue;
        }

        const uint64_t blockStart = block.addr;
        const uint64_t blockEnd = blockStart + static_cast<uint64_t>(block.size);
        const uint32_t alignedAddr = alignGuestHeapValue(block.addr, normalizedAlignment);
        if (alignedAddr < block.addr)
        {
            continue;
        }

        const uint64_t alignedStart = alignedAddr;
        if (alignedStart > blockEnd)
        {
            continue;
        }

        const uint64_t allocEnd = alignedStart + static_cast<uint64_t>(allocSize);
        if (allocEnd > blockEnd)
        {
            continue;
        }

        const uint32_t prefixSize = static_cast<uint32_t>(alignedStart - blockStart);
        const uint32_t suffixSize = static_cast<uint32_t>(blockEnd - allocEnd);

        std::vector<GuestHeapBlock> replacement;
        replacement.reserve(3);
        if (prefixSize > 0u)
        {
            replacement.push_back({block.addr, prefixSize, true});
        }
        replacement.push_back({alignedAddr, allocSize, false});
        if (suffixSize > 0u)
        {
            replacement.push_back({static_cast<uint32_t>(allocEnd), suffixSize, true});
        }

        m_guestHeapBlocks.erase(m_guestHeapBlocks.begin() + static_cast<std::ptrdiff_t>(i));
        m_guestHeapBlocks.insert(m_guestHeapBlocks.begin() + static_cast<std::ptrdiff_t>(i),
                                 replacement.begin(),
                                 replacement.end());

        m_guestHeapEnd = std::max(m_guestHeapEnd, static_cast<uint32_t>(allocEnd));
        return alignedAddr;
    }

    return 0u;
}

void PS2Runtime::coalesceGuestHeapLocked()
{
    if (m_guestHeapBlocks.empty())
    {
        return;
    }

    size_t i = 1;
    while (i < m_guestHeapBlocks.size())
    {
        GuestHeapBlock &prev = m_guestHeapBlocks[i - 1];
        GuestHeapBlock &curr = m_guestHeapBlocks[i];
        const uint64_t prevEnd = static_cast<uint64_t>(prev.addr) + static_cast<uint64_t>(prev.size);
        if (prev.free && curr.free && prevEnd == curr.addr)
        {
            prev.size += curr.size;
            m_guestHeapBlocks.erase(m_guestHeapBlocks.begin() + static_cast<std::ptrdiff_t>(i));
            continue;
        }
        ++i;
    }
}

void PS2Runtime::freeGuestBlockLocked(uint32_t guestAddr)
{
    const int32_t index = findGuestHeapBlockIndexLocked(guestAddr);
    if (index < 0)
    {
        return;
    }

    m_guestHeapBlocks[static_cast<size_t>(index)].free = true;
    coalesceGuestHeapLocked();
}

void PS2Runtime::configureGuestHeap(uint32_t guestBase, uint32_t guestLimit)
{
    std::lock_guard<std::mutex> lock(m_guestHeapMutex);
    uint32_t normalizedBase = alignGuestHeapValue(clampGuestHeapBase(guestBase), kGuestHeapDefaultAlignment);
    if (normalizedBase == 0u)
    {
        normalizedBase = (m_guestHeapSuggestedBase != 0u) ? m_guestHeapSuggestedBase : kGuestHeapDefaultBase;
    }
    m_guestHeapSuggestedBase = normalizedBase;
    resetGuestHeapLocked(normalizedBase, guestLimit);
}

uint32_t PS2Runtime::guestMalloc(uint32_t size, uint32_t alignment)
{
    std::lock_guard<std::mutex> lock(m_guestHeapMutex);
    ensureGuestHeapInitializedLocked();
    return allocateGuestBlockLocked(size, alignment);
}

uint32_t PS2Runtime::guestCalloc(uint32_t count, uint32_t size, uint32_t alignment)
{
    if (count == 0u || size == 0u)
    {
        return 0u;
    }
    if (count > (std::numeric_limits<uint32_t>::max() / size))
    {
        return 0u;
    }

    const uint32_t totalSize = count * size;
    const uint32_t guestAddr = guestMalloc(totalSize, alignment);
    if (guestAddr != 0u)
    {
        uint8_t *rdram = m_memory.getRDRAM();
        if (rdram)
        {
            uint32_t physAddr = guestAddr & PS2_RAM_MASK;
            if (physAddr + totalSize <= PS2_RAM_SIZE)
                std::memset(rdram + physAddr, 0, totalSize);
        }
    }

    return guestAddr;
}

uint32_t PS2Runtime::guestRealloc(uint32_t guestAddr, uint32_t newSize, uint32_t alignment)
{
    if (guestAddr == 0u)
    {
        return guestMalloc(newSize, alignment);
    }
    if (newSize == 0u)
    {
        guestFree(guestAddr);
        return 0u;
    }

    if (newSize > (std::numeric_limits<uint32_t>::max() - (kGuestHeapDefaultAlignment - 1u)))
    {
        return 0u;
    }

    const uint32_t normalizedAlignment = normalizeGuestHeapAlignment(alignment);
    const uint32_t requestedSize = alignGuestHeapValue(newSize, kGuestHeapDefaultAlignment);

    std::lock_guard<std::mutex> lock(m_guestHeapMutex);
    ensureGuestHeapInitializedLocked();

    const int32_t index = findGuestHeapBlockIndexLocked(guestAddr);
    if (index < 0)
    {
        return 0u;
    }

    const size_t blockIndex = static_cast<size_t>(index);
    const uint32_t oldAddr = m_guestHeapBlocks[blockIndex].addr;
    const uint32_t oldSize = m_guestHeapBlocks[blockIndex].size;

    if (requestedSize <= oldSize)
    {
        if (requestedSize < oldSize)
        {
            const uint32_t tailAddr = oldAddr + requestedSize;
            const uint32_t tailSize = oldSize - requestedSize;
            m_guestHeapBlocks[blockIndex].size = requestedSize;
            m_guestHeapBlocks.insert(m_guestHeapBlocks.begin() + static_cast<std::ptrdiff_t>(blockIndex + 1u),
                                     GuestHeapBlock{tailAddr, tailSize, true});
            coalesceGuestHeapLocked();
        }
        return oldAddr;
    }

    if (blockIndex + 1u < m_guestHeapBlocks.size())
    {
        GuestHeapBlock &next = m_guestHeapBlocks[blockIndex + 1u];
        const uint64_t blockEnd = static_cast<uint64_t>(m_guestHeapBlocks[blockIndex].addr) +
                                  static_cast<uint64_t>(m_guestHeapBlocks[blockIndex].size);
        if (next.free && blockEnd == next.addr)
        {
            const uint64_t combined = static_cast<uint64_t>(m_guestHeapBlocks[blockIndex].size) +
                                      static_cast<uint64_t>(next.size);
            if (combined >= requestedSize)
            {
                const uint32_t extraNeeded = requestedSize - m_guestHeapBlocks[blockIndex].size;
                m_guestHeapBlocks[blockIndex].size = requestedSize;
                if (next.size == extraNeeded)
                {
                    m_guestHeapBlocks.erase(m_guestHeapBlocks.begin() + static_cast<std::ptrdiff_t>(blockIndex + 1u));
                }
                else
                {
                    next.addr += extraNeeded;
                    next.size -= extraNeeded;
                }
                m_guestHeapEnd = std::max(m_guestHeapEnd, oldAddr + requestedSize);
                return oldAddr;
            }
        }
    }

    const uint32_t newAddr = allocateGuestBlockLocked(newSize, normalizedAlignment);
    if (newAddr == 0u)
    {
        return 0u;
    }

    uint8_t *rdram = m_memory.getRDRAM();
    if (rdram)
    {
        const uint32_t copyBytes = std::min(oldSize, newSize);
        uint32_t dstPhys = newAddr & PS2_RAM_MASK;
        uint32_t srcPhys = oldAddr & PS2_RAM_MASK;
        if (dstPhys + copyBytes <= PS2_RAM_SIZE && srcPhys + copyBytes <= PS2_RAM_SIZE)
            std::memmove(rdram + dstPhys, rdram + srcPhys, copyBytes);
    }

    freeGuestBlockLocked(oldAddr);
    return newAddr;
}

void PS2Runtime::guestFree(uint32_t guestAddr)
{
    if (guestAddr == 0u)
    {
        return;
    }

    std::lock_guard<std::mutex> lock(m_guestHeapMutex);
    ensureGuestHeapInitializedLocked();
    freeGuestBlockLocked(guestAddr);
}

uint32_t PS2Runtime::guestHeapBase() const
{
    std::lock_guard<std::mutex> lock(m_guestHeapMutex);
    return m_guestHeapConfigured ? m_guestHeapBase : m_guestHeapSuggestedBase;
}

uint32_t PS2Runtime::guestHeapEnd() const
{
    std::lock_guard<std::mutex> lock(m_guestHeapMutex);
    return m_guestHeapConfigured ? m_guestHeapEnd : m_guestHeapSuggestedBase;
}

uint32_t PS2Runtime::guestHeapLimit() const
{
    std::lock_guard<std::mutex> lock(m_guestHeapMutex);
    return m_guestHeapConfigured ? m_guestHeapLimit : m_guestHeapSuggestedBase;
}

uint32_t PS2Runtime::reserveAsyncCallbackStack(uint32_t size, uint32_t alignment)
{
    if (size == 0u)
    {
        return 0u;
    }

    const uint32_t normalizedAlignment = normalizeGuestHeapAlignment(alignment);
    const uint32_t allocSize = alignGuestHeapValue(size, kGuestHeapDefaultAlignment);
    if (allocSize == 0u)
    {
        return 0u;
    }

    std::lock_guard<std::mutex> lock(m_asyncCallbackStackMutex);
    uint32_t top = m_asyncCallbackStackTop;
    if (top > PS2_RAM_SIZE)
    {
        top = PS2_RAM_SIZE;
    }
    top &= ~(kGuestHeapDefaultAlignment - 1u);

    if (top <= allocSize)
    {
        return 0u;
    }

    uint32_t base = top - allocSize;
    base &= ~(normalizedAlignment - 1u);
    if (base < m_asyncCallbackStackFloor || base >= top)
    {
        return 0u;
    }

    m_asyncCallbackStackTop = base;
    return top - 0x10u;
}

namespace
{
    std::atomic<PS2Runtime::MmioTraceHook> g_mmioTraceHook{nullptr};

    inline void traceMmio(bool isWrite, uint32_t address, uint32_t size, uint64_t lo, uint64_t hi, const R5900Context *ctx)
    {
        if (PS2Runtime::MmioTraceHook hook = g_mmioTraceHook.load(std::memory_order_relaxed))
        {
            hook(isWrite, address, size, lo, hi, ctx);
        }
    }
}

void PS2Runtime::setMmioTraceHook(MmioTraceHook hook)
{
    g_mmioTraceHook.store(hook, std::memory_order_relaxed);
}

uint8_t PS2Runtime::Load8(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr)
{
    traceMmio(false, vaddr, 1u, 0u, 0u, ctx);
    try
    {
        return m_memory.read8(vaddr);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_LOAD);
        return 0;
    }
}

uint16_t PS2Runtime::Load16(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr)
{
    traceMmio(false, vaddr, 2u, 0u, 0u, ctx);
    try
    {
        return m_memory.read16(vaddr);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_LOAD);
        return 0;
    }
}

uint32_t PS2Runtime::Load32(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr)
{
    traceMmio(false, vaddr, 4u, 0u, 0u, ctx);
    try
    {
        return m_memory.read32(vaddr);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_LOAD);
        return 0;
    }
}

uint64_t PS2Runtime::Load64(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr)
{
    traceMmio(false, vaddr, 8u, 0u, 0u, ctx);
    try
    {
        return m_memory.read64(vaddr);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_LOAD);
        return 0;
    }
}

__m128i PS2Runtime::Load128(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr)
{
    traceMmio(false, vaddr, 16u, 0u, 0u, ctx);
    try
    {
        return m_memory.read128(vaddr);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_LOAD);
        return _mm_setzero_si128();
    }
}

void PS2Runtime::Store8(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint8_t value)
{
    ps2TraceGuestWrite(rdram, vaddr, 1u, value, 0u, "WRITE8", ctx);
    traceMmio(true, vaddr, 1u, value, 0u, ctx);
    try
    {
        m_memory.write8(vaddr, value);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_STORE);
    }
}

void PS2Runtime::Store16(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint16_t value)
{
    ps2TraceGuestWrite(rdram, vaddr, 2u, value, 0u, "WRITE16", ctx);
    traceMmio(true, vaddr, 2u, value, 0u, ctx);
    try
    {
        m_memory.write16(vaddr, value);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_STORE);
    }
}

void PS2Runtime::Store32(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint32_t value)
{
    ps2TraceGuestWrite(rdram, vaddr, 4u, value, 0u, "WRITE32", ctx);
    traceMmio(true, vaddr, 4u, value, 0u, ctx);
    try
    {
        m_memory.write32(vaddr, value);
        drainCompletedDmacHandlers(rdram);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_STORE);
    }
}

void PS2Runtime::Store64(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, uint64_t value)
{
    ps2TraceGuestWrite(rdram, vaddr, 8u, value, 0u, "WRITE64", ctx);
    traceMmio(true, vaddr, 8u, value, 0u, ctx);
    try
    {
        m_memory.write64(vaddr, value);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_STORE);
    }
}

void PS2Runtime::Store128(uint8_t *rdram, R5900Context *ctx, uint32_t vaddr, __m128i value)
{
    alignas(16) uint64_t _parts[2];
    _mm_storeu_si128(reinterpret_cast<__m128i *>(_parts), value);
    ps2TraceGuestWrite(rdram, vaddr, 16u, _parts[0], _parts[1], "WRITE128", ctx);
    traceMmio(true, vaddr, 16u, _parts[0], _parts[1], ctx);
    try
    {
        m_memory.write128(vaddr, value);
    }
    catch (const std::exception &)
    {
        SignalException(ctx, EXCEPTION_ADDRESS_ERROR_STORE);
    }
}

void PS2Runtime::kickGifDmaChainFromMMIO(uint8_t *rdram,
                                         R5900Context *ctx,
                                         uint32_t dPcrValue,
                                         uint32_t dStatValue,
                                         uint32_t tadr,
                                         uint32_t chcr)
{
    constexpr uint32_t D_PCR = 0x1000E020u;
    constexpr uint32_t D_STAT = 0x1000E010u;
    constexpr uint32_t GIF_TADR = 0x1000A030u;
    constexpr uint32_t GIF_CHCR = 0x1000A000u;

    ps2TraceGuestWrite(rdram, D_PCR, 4u, dPcrValue, 0u, "WRITE32", ctx);
    m_memory.writeIORegister(D_PCR, dPcrValue);
    ps2TraceGuestWrite(rdram, D_STAT, 4u, dStatValue, 0u, "WRITE32", ctx);
    m_memory.writeIORegister(D_STAT, dStatValue);
    ps2TraceGuestWrite(rdram, GIF_TADR, 4u, tadr, 0u, "WRITE32", ctx);
    m_memory.writeIORegister(GIF_TADR, tadr);
    ps2TraceGuestWrite(rdram, GIF_CHCR, 4u, chcr, 0u, "WRITE32", ctx);
    if (m_memory.tryProcessNativeGifImageUploadChain(m_gs, tadr, chcr))
    {
        drainCompletedDmacHandlers(rdram);
        return;
    }
    if (m_memory.tryProcessNativeGifPackedChain(m_gs, tadr, chcr))
    {
        drainCompletedDmacHandlers(rdram);
        return;
    }
    m_memory.writeIORegister(GIF_CHCR, chcr);
    m_memory.processPendingTransfers();
    drainCompletedDmacHandlers(rdram);
}

void PS2Runtime::requestStop()
{
    m_stopRequested.store(true, std::memory_order_relaxed);
    if (m_eeScheduler)
    {
        m_eeScheduler->requestStop();
    }
}

bool PS2Runtime::isStopRequested() const
{
    return m_stopRequested.load(std::memory_order_relaxed);
}

EeScheduler &PS2Runtime::eeScheduler()
{
    return *m_eeScheduler;
}

const EeScheduler &PS2Runtime::eeScheduler() const
{
    return *m_eeScheduler;
}

void PS2Runtime::postEeEvent(EeEvent event)
{
    m_eeScheduler->postEvent(event);
}

bool PS2Runtime::eeCheckpointDue(uint32_t cycles) noexcept
{
    return m_eeScheduler->checkpointDue(cycles);
}

[[noreturn]] void PS2Runtime::eeWaitVSyncTicks(uint32_t ticks, uint32_t resumePc)
{
    const uint64_t currentTick = m_eeScheduler->currentVSyncTick();
    const uint64_t waitTicks = std::max<uint64_t>(1u, ticks);
    m_eeScheduler->waitVSync(currentTick + waitTicks - 1u,
                             0,
                             [resumePc](R5900Context &context)
                             {
                                 context.pc = resumePc;
                             });
}

void PS2Runtime::addEeExitHandler(int threadId, uint32_t function, uint32_t argument)
{
    std::lock_guard lock(m_eeKernelStateMutex);
    m_eeExitHandlers[threadId].push_back({function, argument});
}

std::vector<PS2Runtime::EeExitHandlerRegistration> PS2Runtime::takeEeExitHandlers(int threadId)
{
    std::lock_guard lock(m_eeKernelStateMutex);
    auto it = m_eeExitHandlers.find(threadId);
    if (it == m_eeExitHandlers.end())
    {
        return {};
    }
    auto handlers = std::move(it->second);
    m_eeExitHandlers.erase(it);
    return handlers;
}

void PS2Runtime::removeEeExitHandlers(int threadId)
{
    std::lock_guard lock(m_eeKernelStateMutex);
    m_eeExitHandlers.erase(threadId);
}

bool PS2Runtime::findEeSyscallOverride(uint32_t syscallNumber, uint32_t &handler) const
{
    std::lock_guard lock(m_eeKernelStateMutex);
    const auto it = m_eeSyscallOverrides.find(syscallNumber);
    if (it == m_eeSyscallOverrides.end())
    {
        return false;
    }
    handler = it->second;
    return true;
}

void PS2Runtime::setEeSyscallOverride(uint8_t *rdram, uint32_t syscallNumber, uint32_t handler)
{
    constexpr uint32_t kTableBase = 0x80011F80u & 0x1FFFFFFFu;
    constexpr uint32_t kMirrorLimit = 0x00080000u;
    const int64_t offset = static_cast<int64_t>(static_cast<int32_t>(syscallNumber)) * 4;
    const int64_t address = static_cast<int64_t>(kTableBase) + offset;

    std::lock_guard lock(m_eeKernelStateMutex);
    if (handler == 0u)
    {
        m_eeSyscallOverrides.erase(syscallNumber);
    }
    else
    {
        m_eeSyscallOverrides[syscallNumber] = handler;
    }
    if (!rdram || address < 0 || address + 4 > kMirrorLimit)
    {
        return;
    }
    const uint32_t guestAddress = static_cast<uint32_t>(address);
    std::memcpy(rdram + guestAddress, &handler, sizeof(handler));
    if (handler == 0u)
    {
        m_eeSyscallMirrorAddresses.erase(guestAddress);
    }
    else
    {
        m_eeSyscallMirrorAddresses.insert(guestAddress);
    }
}

void PS2Runtime::initializeEeKernelState(uint8_t *rdram)
{
    if (!rdram)
    {
        return;
    }
    constexpr uint32_t kTableGuestBase = 0x80011F80u;
    constexpr uint32_t kTableBase = kTableGuestBase & 0x1FFFFFFFu;
    constexpr uint32_t kMirrorLimit = 0x00080000u;
    constexpr uint32_t kProbeBase = 0x000002F0u;

    std::lock_guard lock(m_eeKernelStateMutex);
    for (const uint32_t address : m_eeSyscallMirrorAddresses)
    {
        const uint32_t zero = 0u;
        std::memcpy(rdram + address, &zero, sizeof(zero));
    }
    m_eeSyscallMirrorAddresses.clear();
    const uint32_t high = kTableGuestBase >> 16;
    const uint32_t low = kTableGuestBase & 0xFFFFu;
    std::memcpy(rdram + kProbeBase, &high, sizeof(high));
    std::memcpy(rdram + kProbeBase + 8u, &low, sizeof(low));
    m_eeSyscallMirrorAddresses.insert(kProbeBase);
    m_eeSyscallMirrorAddresses.insert(kProbeBase + 8u);

    for (const auto &[syscallNumber, handler] : m_eeSyscallOverrides)
    {
        const int64_t offset = static_cast<int64_t>(static_cast<int32_t>(syscallNumber)) * 4;
        const int64_t address = static_cast<int64_t>(kTableBase) + offset;
        if (address < 0 || address + 4 > kMirrorLimit)
        {
            continue;
        }
        const uint32_t guestAddress = static_cast<uint32_t>(address);
        std::memcpy(rdram + guestAddress, &handler, sizeof(handler));
        m_eeSyscallMirrorAddresses.insert(guestAddress);
    }
}

void PS2Runtime::HandleIntegerOverflow(R5900Context *ctx)
{
    raiseCop0Exception(ctx, EXCEPTION_INTEGER_OVERFLOW);
}

void PS2Runtime::setAsyncCallbackStackRegion(uint32_t floor, uint32_t top)
{
    std::lock_guard<std::mutex> lock(m_asyncCallbackStackMutex);
    m_asyncCallbackStackFloor = std::min(floor, PS2_RAM_SIZE);
    m_asyncCallbackStackTop = std::min(top, PS2_RAM_SIZE);
}

namespace
{
    // PCCR fields (EE Core User's Manual, performance counter): CTE bit 31; per counter the mode
    // enables EXL/K/S/U and a 5-bit event number.
    constexpr uint32_t kPccrCte = 1u << 31;
    constexpr uint32_t kPerfEventCycle = 1u;

    bool perfCounterCountsCycles(uint32_t pccr, int counter)
    {
        if ((pccr & kPccrCte) == 0u)
            return false;
        const uint32_t modes = counter == 0 ? (pccr >> 1) & 0xFu : (pccr >> 11) & 0xFu;
        const uint32_t event = counter == 0 ? (pccr >> 5) & 0x1Fu : (pccr >> 15) & 0x1Fu;
        return modes != 0u && event == kPerfEventCycle;
    }
}

void PS2Runtime::foldPerfCounters()
{
    const uint64_t now = m_eeScheduler->currentCycle();
    const uint64_t elapsed = now - m_perfCycleBase;
    for (int i = 0; i < 2; ++i)
        if (perfCounterCountsCycles(m_perfPccr, i))
            m_perfPcr[i] = static_cast<uint32_t>(m_perfPcr[i] + elapsed);
    m_perfCycleBase = now;
}

uint32_t PS2Runtime::readPerfCounter(uint32_t select)
{
    foldPerfCounters();
    switch (select)
    {
    case 0u: return m_perfPccr;
    case 1u: return m_perfPcr[0];
    case 3u: return m_perfPcr[1];
    default: return 0u;
    }
}

void PS2Runtime::writePerfCounter(uint32_t select, uint32_t value)
{
    foldPerfCounters();
    switch (select)
    {
    case 0u:
        m_perfPccr = value;
        if (!m_perfUnmodelledEventReported && (value & kPccrCte) != 0u)
        {
            const uint32_t ev0 = (value >> 5) & 0x1Fu, ev1 = (value >> 15) & 0x1Fu;
            const bool on0 = ((value >> 1) & 0xFu) != 0u, on1 = ((value >> 11) & 0xFu) != 0u;
            if ((on0 && ev0 != kPerfEventCycle) || (on1 && ev1 != kPerfEventCycle))
            {
                m_perfUnmodelledEventReported = true;
                std::cerr << "[perf] PCCR 0x" << std::hex << value << std::dec
                          << " selects an event other than processor cycles; it will not count" << std::endl;
            }
        }
        break;
    case 1u: m_perfPcr[0] = value; break;
    case 3u: m_perfPcr[1] = value; break;
    default: break;
    }
}

void PS2Runtime::setHeadless(bool headless, double runTimeLimitSeconds)
{
    m_headless = headless;
    m_runTimeLimitSeconds = runTimeLimitSeconds;
}

void PS2Runtime::startVu1ThreadIfRequested(bool gsWorker)
{
    const char *env = std::getenv("PS2X_VU1_THREAD");
    if (!env || env[0] != '1')
        return;
    if (gsWorker)
    {
        RUNTIME_LOG("[run] PS2X_VU1_THREAD=1 ignored: not supported together with PS2X_GS_THREAD=1");
        return;
    }
    if (ps2_vu1_overlap_model::state().enabled)
    {
        // The model times work as if it ran inline on one thread.
        ps2_vu1_overlap_model::state().enabled = false;
        RUNTIME_LOG("[run] PS2X_VU1_OVERLAP_MODEL disabled: PS2X_VU1_THREAD=1 runs the real worker");
    }
    m_memory.startVu1Thread();
    RUNTIME_LOG("[run] VU1 back-end worker thread enabled (PS2X_VU1_THREAD=1)");
}

void PS2Runtime::stopVu1Thread()
{
    if (!m_memory.vu1ThreadActive())
        return;
    m_memory.stopVu1Thread();
    if (const uint64_t checks = m_memory.vu1ThreadChecks())
        RUNTIME_LOG("[run] VU1 worker VIF1 state checks: " << checks << ", mismatches: "
                                                             << m_memory.vu1ThreadCheckMismatches());
    RUNTIME_LOG("[run] VU1 back-end worker thread stopped");
}

void PS2Runtime::runHeadless()
{
    startVu1ThreadIfRequested(false);
    std::atomic<bool> gameThreadFinished{false};
    std::thread gameThread([&]()
                           {
        ThreadNaming::SetCurrentThreadName("GameThread");
        try
        {
            m_eeScheduler->reset(m_memory.getRDRAM(), m_cpuContext);
            m_eeScheduler->run();
        }
        catch (const std::exception &e)
        {
            std::cerr << "Error during program execution: " << e.what() << std::endl;
        }
        catch (...)
        {
            std::cerr << "Error during program execution: unknown exception" << std::endl;
        }
        gameThreadFinished.store(true, std::memory_order_release); });

    const auto start = std::chrono::steady_clock::now();
    while (!isStopRequested() && !gameThreadFinished.load(std::memory_order_acquire))
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        const std::chrono::duration<double> elapsed = std::chrono::steady_clock::now() - start;
        if (m_runTimeLimitSeconds > 0.0 && elapsed.count() >= m_runTimeLimitSeconds)
        {
            RUNTIME_LOG("[run] headless time limit reached");
            break;
        }
    }

    requestStop();
    if (gameThread.joinable())
    {
        gameThread.join();
    }
    stopVu1Thread();
    ps2_pacing_trace::write();
}

namespace
{
    // PS2X_WINDOW_MODE=borderless: start in borderless fullscreen on the monitor the window opened on.
    // Alt+Enter switches between that and the window in either mode. The presenter's 4:3 fit fills the screen.
    struct WindowMode
    {
        bool borderless = false;
        bool wasFocused = true;

        void enterBorderless()
        {
            ToggleBorderlessWindowed();
            ClearWindowState(FLAG_WINDOW_TOPMOST); // raylib makes it topmost; Alt+Tab'd windows must cover it
            HideCursor();
            borderless = true;
        }

        void leaveBorderless()
        {
            ToggleBorderlessWindowed();
            ShowCursor();
            borderless = false;
        }

        void start()
        {
            const char *env = std::getenv("PS2X_WINDOW_MODE");
            if (env && std::strcmp(env, "borderless") == 0)
            {
                enterBorderless();
                RUNTIME_LOG("[run] borderless fullscreen " << GetScreenWidth() << "x" << GetScreenHeight()
                                                           << " (PS2X_WINDOW_MODE=borderless)");
            }
        }

        // After the window's frame has been presented (input is polled in EndDrawing).
        void update()
        {
            if (IsKeyPressed(KEY_ENTER) && (IsKeyDown(KEY_LEFT_ALT) || IsKeyDown(KEY_RIGHT_ALT)))
            {
                if (borderless)
                    leaveBorderless();
                else
                    enterBorderless();
                RUNTIME_LOG("[run] Alt+Enter: " << (borderless ? "borderless fullscreen " : "window ")
                                                << GetScreenWidth() << "x" << GetScreenHeight());
            }

            // The monitor's resolution may have changed while another window had the focus: refit on return.
            const bool focused = IsWindowFocused();
            if (focused && !wasFocused && borderless)
            {
                const int monitor = GetCurrentMonitor();
                const int width = GetMonitorWidth(monitor);
                const int height = GetMonitorHeight(monitor);
                if (width > 0 && height > 0 && (width != GetScreenWidth() || height != GetScreenHeight()))
                {
                    const Vector2 position = GetMonitorPosition(monitor);
                    SetWindowPosition(static_cast<int>(position.x), static_cast<int>(position.y));
                    SetWindowSize(width, height);
                    RUNTIME_LOG("[run] borderless refit to the monitor's new resolution " << width << "x" << height);
                }
            }
            wasFocused = focused;
        }
    };
}

void PS2Runtime::run()
{
    m_stopRequested.store(false, std::memory_order_relaxed);
    ps2_stubs::resetSifState();
    resetIop();
    ps2_stubs::resetAudioStubState();
    ps2_stubs::resetMpegStubState();
    initializeEeKernelState(m_memory.getRDRAM());
    m_cpuContext.r[4] = _mm_setzero_si128();
    m_cpuContext.r[5] = _mm_setzero_si128();
    m_cpuContext.r[29] = _mm_set_epi64x(0, static_cast<int64_t>(PS2_RAM_SIZE - 0x10u));
    m_debugPc.store(m_cpuContext.pc, std::memory_order_relaxed);
    m_debugRa.store(static_cast<uint32_t>(_mm_extract_epi32(m_cpuContext.r[31], 0)), std::memory_order_relaxed);
    m_debugSp.store(static_cast<uint32_t>(_mm_extract_epi32(m_cpuContext.r[29], 0)), std::memory_order_relaxed);
    m_debugGp.store(static_cast<uint32_t>(_mm_extract_epi32(m_cpuContext.r[28], 0)), std::memory_order_relaxed);

    RUNTIME_LOG("Starting execution at address 0x" << std::hex << m_cpuContext.pc << std::dec);

    if (m_headless)
    {
        runHeadless();
        return;
    }

    // PS2X_PRESENT_PACING=vblank|deadline: see runtime/ps2_present_pacing.h.
    const char *pacingEnv = std::getenv("PS2X_PRESENT_PACING");
    const bool deadlinePaced = pacingEnv && std::strcmp(pacingEnv, "deadline") == 0;
    const bool vblankPaced = deadlinePaced || (pacingEnv && std::strcmp(pacingEnv, "vblank") == 0);
    if (vblankPaced)
    {
        ps2_present_pacing::enable();
        SetTargetFPS(0); // the guest's VBlanks pace the window
        RUNTIME_LOG("[run] presentation paced by guest VBlanks (PS2X_PRESENT_PACING=" << pacingEnv << ")");
        // DISPLAY LOCK (ps2_present_pacing.h): with VSync the fields follow the monitor's refresh.
        const char *vsyncEnv = std::getenv("PS2X_VSYNC");
        const bool vsyncOn = vsyncEnv && std::strcmp(vsyncEnv, "1") == 0;
        const char *lockEnv = std::getenv("PS2X_DISPLAY_LOCK");
        const bool lockOn = deadlinePaced && vsyncOn && !(lockEnv && std::strcmp(lockEnv, "0") == 0);
        const int refreshHz = GetMonitorRefreshRate(GetCurrentMonitor());
        ps2_present_pacing::setDisplay(lockOn, refreshHz);
        RUNTIME_LOG("[run] display lock " << (lockOn ? "on" : "off") << " (monitor " << refreshHz
                                          << " Hz; PS2X_VSYNC=1 with deadline pacing, PS2X_DISPLAY_LOCK=0: off)");
    }

    // A blank image to use as a framebuffer
    Image blank = GenImageColor(FB_WIDTH, FB_HEIGHT, BLANK);
    Texture2D frameTex = LoadTextureFromImage(blank);
    UnloadImage(blank);

    WindowMode windowMode;
    // 16:9 while the game keeps reporting widescreen frames (ps2_display_aspect.h); kWideHold presents (about 0.2 s)
    // without one fall back to 4:3, so movies and 2D-only screens keep their shape.
    constexpr int kWideHold = 10;
    uint64_t lastWideFrames = ps2_display_aspect::wideFrameCount();
    int presentsSinceWide = kWideHold;
    windowMode.start();

    // PS2X_GS_THREAD=1: GS work runs on its own thread (GS_THREADING_PLAN.md). Headless runs stay inline.
    std::unique_ptr<GSAsyncQueue> gsQueue;
    if (const char *gsThread = std::getenv("PS2X_GS_THREAD"); gsThread && gsThread[0] == '1')
    {
        constexpr size_t kGsQueueBytes = 4u * 1024u * 1024u; // about three 3D frames of Maximo's GIF traffic
        gsQueue = std::make_unique<GSAsyncQueue>(m_gs, kGsQueueBytes);
        GSAsyncQueue *queue = gsQueue.get();
        const auto displayRegs = [this]()
        {
            const GSRegisters &regs = m_memory.gs();
            GSDisplayRegs snapshot;
            snapshot.pmode = regs.pmode;
            snapshot.smode2 = regs.smode2;
            snapshot.dispfb1 = regs.dispfb1;
            snapshot.display1 = regs.display1;
            snapshot.dispfb2 = regs.dispfb2;
            snapshot.display2 = regs.display2;
            snapshot.bgcolor = regs.bgcolor;
            return snapshot;
        };
        m_gs.setAsyncQueue(queue);
        queue->submitDisplayRegs(displayRegs());
        m_gifArbiter.setTakePacketFn([queue](std::vector<uint8_t> &&bytes)
                                     { queue->submitPacket(std::move(bytes)); });
        m_memory.setGsPrivWriteCallback([queue, displayRegs](uint32_t regOffset)
                                        {
            // PMODE, SMODE2, DISPFB1, DISPLAY1, DISPFB2, DISPLAY2 and BGCOLOR: the registers presentation reads.
            switch (regOffset)
            {
            case 0x00u: case 0x20u: case 0x70u: case 0x80u: case 0x90u: case 0xA0u: case 0xE0u:
                queue->submitDisplayRegs(displayRegs());
                break;
            default:
                break;
            } });
        RUNTIME_LOG("[run] GS worker thread enabled (PS2X_GS_THREAD=1)");
    }
    startVu1ThreadIfRequested(gsQueue != nullptr);

    std::atomic<bool> gameThreadFinished{false};

    std::thread gameThread([&]()
                           {
        ThreadNaming::SetCurrentThreadName("GameThread");
        try
        {
            m_eeScheduler->reset(m_memory.getRDRAM(), m_cpuContext);
            m_eeScheduler->run();
            uint32_t pc = m_debugPc.load(std::memory_order_relaxed);
            RUNTIME_LOG("Game thread returned. PC=0x" << std::hex << pc
                      << " RA=0x" << static_cast<uint32_t>(_mm_extract_epi32(m_cpuContext.r[31], 0)) << std::dec << std::endl);
        }
        catch (const std::exception &e)
        {
            std::cerr << "Error during program execution: " << e.what() << std::endl;
        }
        catch (...)
        {
            std::cerr << "Error during program execution: unknown exception" << std::endl;
        }
        gameThreadFinished.store(true, std::memory_order_release); });

    uint64_t tick = 0;
    uint64_t presentedVBlank = 0;
    int64_t presentedDeadlineNs = 0;
    while (!isStopRequested() && !gameThreadFinished.load(std::memory_order_acquire))
    {
        PS2_IF_AGRESSIVE_LOGS({
            tick++;
            if ((tick % 120) == 0)
            {
                uint64_t curDma = m_memory.dmaStartCount();
                uint64_t curGif = m_memory.gifCopyCount();
                uint64_t curGs = m_memory.gsWriteCount();
                uint64_t curVif = m_memory.vifWriteCount();
                const GSRegisters &gs = m_memory.gs();
                const uint32_t dbgPc = m_debugPc.load(std::memory_order_relaxed);
                const uint32_t dbgRa = m_debugRa.load(std::memory_order_relaxed);
                const uint32_t dbgSp = m_debugSp.load(std::memory_order_relaxed);
                const uint32_t dbgGp = m_debugGp.load(std::memory_order_relaxed);
                const auto eeSnapshot = m_eeScheduler->snapshot();

                RUNTIME_LOG("[run:tick] tick=" << tick
                                               << " pc=0x" << std::hex << dbgPc
                                               << " ra=0x" << dbgRa
                                               << " sp=0x" << dbgSp
                                               << " gp=0x" << dbgGp
                                               << " dispfb1=0x" << gs.dispfb1
                                               << " display1=0x" << gs.display1
                                               << std::dec
                                               << " activeThreads=" << eeSnapshot.threads.size()
                                               << " dma=" << curDma
                                               << " gif=" << curGif
                                               << " gsw=" << curGs
                                               << " vif=" << curVif
                                               << std::endl);

            }
        });
        if (deadlinePaced)
        {
            // One present per guest VBlank, at its host deadline. Once the deadline just presented has passed, wait
            // for the scheduler to process that VBlank and announce the next one. The timeouts keep the window
            // responsive while the guest produces no VBlanks (a long load or a stall).
            int64_t next = ps2_present_pacing::state().nextDeadlineNs.load(std::memory_order_acquire);
            while (next == presentedDeadlineNs &&
                   ps2_present_pacing::waitVBlank(presentedVBlank, std::chrono::milliseconds(100)))
                next = ps2_present_pacing::state().nextDeadlineNs.load(std::memory_order_acquire);
            if (next != presentedDeadlineNs)
            {
                const int64_t limit = ps2_present_pacing::toNs(std::chrono::steady_clock::now()) + 100000000;
                ps2_present_pacing::sleepUntilNs(std::min(next, limit));
                presentedDeadlineNs = next;
            }
        }
        else if (vblankPaced)
        {
            // One present per guest VBlank. The frame the guest finished before this VBlank is already published,
            // since both happen on the game thread in that order. The timeout keeps the window responsive while the
            // guest produces no VBlanks (a long load or a stall).
            ps2_present_pacing::waitVBlank(presentedVBlank, std::chrono::milliseconds(100));
        }
        const int64_t presentWorkStartNs = ps2_present_pacing::toNs(std::chrono::steady_clock::now());
        uint32_t presentWidth = FB_WIDTH;
        uint32_t presentHeight = DEFAULT_DISPLAY_HEIGHT;
        {
            ps2_host_profile::Scope profileScope(ps2_host_profile::kPresent);
            UploadFrame(frameTex, this, presentWidth, presentHeight);
        }

        BeginDrawing();
        ClearBackground(BLACK);
        const float srcWidth = static_cast<float>(std::max<uint32_t>(1u, presentWidth));
        const float srcHeight = static_cast<float>(std::max<uint32_t>(1u, presentHeight));
        const float screenWidth = static_cast<float>(GetScreenWidth());
        const float screenHeight = static_cast<float>(GetScreenHeight());
        // The GS drives a 4:3 TV picture whatever the buffer's pixel count (640x448 NTSC, 640x512 PAL),
        // so fit a 4:3 rectangle rather than assuming square pixels. A widescreen game mode draws its 3D view
        // anamorphically into the same buffer, shown at 16:9.
        float displayAspect = 4.0f / 3.0f;
        if (ps2_display_aspect::isWideEnabled())
        {
            const uint64_t wideFrames = ps2_display_aspect::wideFrameCount();
            presentsSinceWide = wideFrames != lastWideFrames ? 0 : std::min(presentsSinceWide + 1, kWideHold);
            lastWideFrames = wideFrames;
            if (presentsSinceWide < kWideHold)
                displayAspect = 16.0f / 9.0f;
            static float loggedAspect = 0.0f;
            if (displayAspect != loggedAspect)
            {
                loggedAspect = displayAspect;
                RUNTIME_LOG("[run] presentation aspect " << (displayAspect > 1.5f ? "16:9" : "4:3"));
            }
        }
        const float dstHeight = std::min(screenHeight, screenWidth / displayAspect);
        const float dstWidth = dstHeight * displayAspect;
        const Rectangle srcRect{0.0f, 0.0f, srcWidth, srcHeight};
        Rectangle dstRect{
            (screenWidth - dstWidth) * 0.5f,
            (screenHeight - dstHeight) * 0.5f,
            dstWidth,
            dstHeight};
        // Tell the host the picture's size in window pixels. A frame the host already rendered at that size is drawn
        // 1:1 on whole pixels, so the window's filter leaves it untouched.
        const uint32_t targetWidth = static_cast<uint32_t>(std::lround(dstWidth));
        const uint32_t targetHeight = static_cast<uint32_t>(std::lround(dstHeight));
        ps2_display_aspect::notePresentTarget(targetWidth, targetHeight);
        if (presentWidth == targetWidth && presentHeight == targetHeight)
            dstRect = Rectangle{std::floor(dstRect.x + 0.5f), std::floor(dstRect.y + 0.5f), srcWidth, srcHeight};
        DrawTexturePro(frameTex, srcRect, dstRect, Vector2{0.0f, 0.0f}, 0.0f, WHITE);
        if (m_debugUiInitialized && m_debugUiDrawCallback)
        {
            m_debugUiDrawCallback(*this, m_debugUiUserData);
        }
        {
            // PS2X_SCREENSHOT_SECONDS=<s>: save what the window shows every <s> seconds (frame_NNN.png in
            // the working directory), so a headless check can see the presented pixels.
            static const char *shotEnv = std::getenv("PS2X_SCREENSHOT_SECONDS");
            static const double shotInterval = shotEnv ? std::atof(shotEnv) : 0.0;
            static double nextShot = shotInterval;
            static int shotIndex = 0;
            if (shotInterval > 0.0 && GetTime() >= nextShot)
            {
                rlDrawRenderBatchActive(); // raylib batches draws until EndDrawing; flush before reading back
                TakeScreenshot(TextFormat("frame_%03d.png", shotIndex));
                // Also keep the presented GS frame at its native size (gs_NNN.png) for comparisons.
                Image gsFrame = LoadImageFromTexture(frameTex);
                ImageCrop(&gsFrame, Rectangle{0.0f, 0.0f, srcWidth, srcHeight});
                ExportImage(gsFrame, TextFormat("gs_%03d.png", shotIndex));
                UnloadImage(gsFrame);
                ++shotIndex;
                nextShot += shotInterval;
            }
        }
#if defined(_WIN32)
        {
            static double shotMessageUntil = 0.0;
            if (g_shotRequested.exchange(false, std::memory_order_relaxed))
            {
                saveWindowShot(dstRect);
                shotMessageUntil = GetTime() + 2.0;
            }
            if (GetTime() < shotMessageUntil)
            {
                const int size = std::max(16, GetScreenHeight() / 36);
                const char *text = "Screenshot saved for the launcher banner";
                DrawRectangle(size, size, MeasureText(text, size) + size, size * 2, Fade(BLACK, 0.7f));
                DrawText(text, size + size / 2, size + size / 2, size, GOLD);
            }
        }
#endif
        const int64_t presentWorkNs = ps2_present_pacing::toNs(std::chrono::steady_clock::now()) - presentWorkStartNs;
        EndDrawing();
        ps2_pacing_trace::note(ps2_pacing_trace::kSwap);
        if (deadlinePaced)
        {
            ps2_present_pacing::noteSwap(ps2_present_pacing::toNs(std::chrono::steady_clock::now()), presentWorkNs);
            static uint64_t lockLog = 0;
            if (++lockLog % 3600 == 0) // a minute at 60 Hz
            {
                const auto &d = ps2_present_pacing::display();
                RUNTIME_LOG("[run] display lock: refresh " << (d.periodNs.load() / 1000) << " us, present work "
                                                           << (d.workNs.load() / 1000) << " us, "
                                                           << (d.locked.load() ? "locked" : "free") << ", "
                                                           << d.lockedFields.load() << " fields locked");
            }
        }
        windowMode.update();
#if defined(_WIN32)
        // One line for bug reports: what the driver really uses, read after the window mode has settled.
        static int swapIntervalCheck = 0;
        if (++swapIntervalCheck == 300)
        {
            using GetSwapInterval = int(__stdcall *)();
            if (auto get = reinterpret_cast<GetSwapInterval>(glfwGetProcAddress("wglGetSwapIntervalEXT")))
                RUNTIME_LOG("[run] swap interval " << get());
        }
#endif
        ps2PadHostUpdate();

        if (WindowShouldClose())
        {
            // raylib also reports its exit key (Esc) as a close request; record which one it was.
            const bool escape = IsKeyPressed(KEY_ESCAPE) || IsKeyDown(KEY_ESCAPE);
            ps2_exit_reason::set(escape ? "window closed by the Esc key" : "window closed (close button or Alt+F4)");
            RUNTIME_LOG("[run] window close requested (" << ps2_exit_reason::get() << "), breaking out of loop");
            requestStop();
            break;
        }
    }
    if (gameThreadFinished.load(std::memory_order_acquire))
        ps2_exit_reason::set("the game thread finished");
    else
        ps2_exit_reason::set("stop requested by the host");

    requestStop();
    if (gameThread.joinable())
    {
        gameThread.join();
    }
    stopVu1Thread();
    ps2_pacing_trace::write();
    if (gsQueue)
    {
        m_gifArbiter.setTakePacketFn(nullptr);
        m_memory.setGsPrivWriteCallback(nullptr);
        m_gs.setAsyncQueue(nullptr); // waits for the worker to drain
        gsQueue->stop();
        gsQueue.reset();
    }

    if (m_debugUiInitialized && m_debugUiShutdownCallback)
    {
        m_debugUiShutdownCallback(*this, m_debugUiUserData);
        m_debugUiInitialized = false;
    }
    UnloadTexture(frameTex);
    CloseWindow();

    RUNTIME_LOG("[run] exiting loop");
}
