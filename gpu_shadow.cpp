#include "gpu_shadow.h"

#include "ps2_runtime.h"
#include "runtime/ps2_pacing_trace.h"
#include "runtime/ps2_display_aspect.h"
#include "context.hpp"
#include "device.hpp"
#include "gs_interface.hpp"
#include "hdtex/hdtex_runtime.h"
#include "thread_id.hpp"
#include "shaders/slangmosh.hpp"
// MAXIMO_SMAA: Granite's SMAA shaders as SPIR-V (shaders/smaa/build_smaa_spv.sh) and SMAA's lookup tables
// (Granite/tools/smaa; SMAA is MIT, Jorge Jimenez et al.).
#include "smaa_spv.h"
#include "maximo_post_spv.h" // POST EFFECTS (shaders/smaa/post)
#include "AreaTex.h"
#include "SearchTex.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <atomic>
#include <chrono>
#include <mutex>
#include <condition_variable>
#include <deque>
#include <thread>
#include <algorithm>
#include <cstring>
#include <string>
#include <fstream>
#include <utility>
#include <vector>

// POST EFFECTS level (0 off, 1 soft, 2 strong): set by the debug menu (game thread), read by the GS worker.
namespace
{
std::atomic<int> g_gpuPostLevel{[] {
    const char *v = std::getenv("MAXIMO_POST");
    return v ? std::clamp(std::atoi(v), 0, 2) : 0;
}()};
}
static std::atomic<int> g_gpuLightLevel{[] {
    const char *v = std::getenv("MAXIMO_SCENE_LIGHT");
    return v ? std::clamp(std::atoi(v), 0, 4) : 0;
}()};
void GpuShadow::setLightLevel(int level) { g_gpuLightLevel.store(std::clamp(level, 0, 4), std::memory_order_relaxed); }
int GpuShadow::lightLevel() { return g_gpuLightLevel.load(std::memory_order_relaxed); }
void GpuShadow::setPostLevel(int level) { g_gpuPostLevel.store(std::clamp(level, 0, 2), std::memory_order_relaxed); }
int GpuShadow::postLevel() { return g_gpuPostLevel.load(std::memory_order_relaxed); }

extern std::atomic<bool> pgs_maximo_draw_log; // paraLLEl-GS diagnostics (gs_interface.cpp)
// GIF packets the host sent to the GPU only (pure geometry; maximo_boot's gifPacketNeedsCpuGs): the CPU frontend's
// packet count is short by these, and they count as seen by it for the completeness checks.
std::atomic<uint64_t> g_gpuHostSkippedGifPackets{0};

namespace
{
// PrivRegisterState mirrors the GS privileged MMIO map: register n at 0x12000000 + n * 0x10 is qwords_lo[2n], and the
// 0x12001000 block is qwords_hi. The typed bitfield members are not all 8 bytes (SMODE2Bits pads to 12), so the raw
// 64-bit values go through the qword views.
// The privileged registers as the guest left them at a vsync, so the GS worker can apply them in stream order.
struct PrivSnapshot
{
    uint64_t lo[15];
    uint64_t csr, imr, busdir, siglblid;
};

PrivSnapshot snapshotPrivileged(const GSRegisters &src)
{
    return PrivSnapshot{{src.pmode, src.smode1, src.smode2, src.srfsh, src.synch1, src.synch2, src.syncv, src.dispfb1,
                         src.display1, src.dispfb2, src.display2, src.extbuf, src.extdata, src.extwrite, src.bgcolor},
                        src.csr.load(std::memory_order_relaxed), src.imr, src.busdir, src.siglblid};
}

void copyPrivileged(ParallelGS::PrivRegisterState &dst, const PrivSnapshot &src)
{
    for (size_t i = 0; i < std::size(src.lo); ++i)
        dst.qwords_lo[i * 2] = src.lo[i];
    dst.qwords_hi[0x00 / 8] = src.csr;
    dst.qwords_hi[0x10 / 8] = src.imr;
    dst.qwords_hi[0x40 / 8] = src.busdir;
    dst.qwords_hi[0x80 / 8] = src.siglblid;
}

// MAXIMO_GPU_FRAME_HASHES=<file>: "guestFrame hash" for every published frame, to check that two ways of driving
// the device (the GS worker and the game thread) give the same pictures.
uint64_t hashPixels(const std::vector<uint8_t> &pixels)
{
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i + 8 <= pixels.size(); i += 8)
    {
        uint64_t v;
        std::memcpy(&v, pixels.data() + i, 8);
        h = (h ^ v) * 1099511628211ull;
    }
    return h;
}

bool writeFile(const char *name, const void *data, size_t size)
{
    std::ofstream file(name, std::ios::binary);
    file.write(static_cast<const char *>(data), static_cast<std::streamsize>(size));
    return file.good();
}

// Counters for every way GS state can change outside the arbiter packets the shadow receives.
struct BypassCounts
{
    uint64_t packets = 0, directRegWrites = 0, nativePacked = 0, nativeUploads = 0, localToHost = 0;

    static BypassCounts read(const GSStreamStats &s)
    {
        BypassCounts c;
        c.packets = s.packets.load(std::memory_order_relaxed) + g_gpuHostSkippedGifPackets.load(std::memory_order_relaxed);
        c.directRegWrites = s.directRegWrites.load(std::memory_order_relaxed);
        c.nativePacked = s.nativePackedPackets.load(std::memory_order_relaxed);
        c.nativeUploads = s.nativeImageUploads.load(std::memory_order_relaxed);
        c.localToHost = s.localToHostReads.load(std::memory_order_relaxed);
        return c;
    }
};

// Enhancement (opt-in): MAXIMO_SSAA=1|2|4|8|16 super-samples rendering. From 4x up, paraLLEl-GS also scans out a
// double-resolution picture (1280x1024 for PAL) built from the samples. Unset or 1 keeps the original output exactly.
uint32_t superSamplingFromEnv()
{
    const char *v = std::getenv("MAXIMO_SSAA");
    const uint32_t rate = v ? uint32_t(std::atoi(v)) : 1u;
    return rate == 2 || rate == 4 || rate == 8 || rate == 16 ? rate : 1u;
}

bool envFlag(const char *name, bool fallback)
{
    const char *v = std::getenv(name);
    return v && *v ? std::atoi(v) != 0 : fallback;
}

// MAXIMO_FSR_SHARPNESS=<stops>: RCAS sharpness, 0 = sharpest, 2 = soft (AMD's scale); below 0 skips RCAS.
float fsrSharpnessFromEnv()
{
    const char *v = std::getenv("MAXIMO_FSR_SHARPNESS");
    return v && *v ? float(std::atof(v)) : 0.25f;
}

uint32_t floatToHalfBits(float f)
{
    uint32_t x;
    std::memcpy(&x, &f, sizeof(x));
    const uint32_t sign = (x >> 16) & 0x8000u;
    const int32_t exponent = int32_t((x >> 23) & 0xffu) - 127 + 15;
    const uint32_t mantissa = x & 0x7fffffu;
    if (exponent <= 0)
        return sign;
    if (exponent >= 31)
        return sign | 0x7c00u;
    return sign | uint32_t(exponent) << 10 | (mantissa + 0x1000u) >> 13;
}

// AMD FidelityFX FSR 1 constants (the CPU half of ffx_fsr1.h, as in Granite's renderer/post/aa.cpp).
void fsrEasuConstants(float con[16], float inWidth, float inHeight, float outWidth, float outHeight)
{
    con[0] = inWidth / outWidth;
    con[1] = inHeight / outHeight;
    con[2] = 0.5f * inWidth / outWidth - 0.5f;
    con[3] = 0.5f * inHeight / outHeight - 0.5f;
    con[4] = 1.0f / inWidth;
    con[5] = 1.0f / inHeight;
    con[6] = 1.0f / inWidth;
    con[7] = -1.0f / inHeight;
    con[8] = -1.0f / inWidth;
    con[9] = 2.0f / inHeight;
    con[10] = 1.0f / inWidth;
    con[11] = 2.0f / inHeight;
    con[12] = 0.0f / inWidth;
    con[13] = 4.0f / inHeight;
    con[14] = con[15] = 0.0f;
}

void fsrRcasConstants(float con[4], float sharpness)
{
    sharpness = std::exp2(-sharpness);
    const uint32_t half = floatToHalfBits(sharpness);
    const uint32_t halves = half | half << 16;
    con[0] = sharpness;
    std::memcpy(&con[1], &halves, sizeof(halves));
    con[2] = con[3] = 0.0f;
}
}

struct GpuShadow::Impl
{
    explicit Impl(PS2Runtime &runtime_) : runtime(runtime_) {}

    PS2Runtime &runtime;
    const uint32_t superSampling = superSamplingFromEnv();
    // High-resolution scanout needs at least 4x (2x2) samples. MAXIMO_HIRES_SCANOUT=0 keeps native-size scanout.
    const bool hiresScanout = superSampling >= 4 && envFlag("MAXIMO_HIRES_SCANOUT", true);
    // MAXIMO_SCANOUT=4 (needs MAXIMO_SSAA=16): scan out 4x per axis (2560x1024 for PAL) instead of 2x, one super
    // sample per output pixel (paraLLEl-GS branch maximo-uhd). Falls back to 2x if the device runs fewer samples.
    const bool ultraScanout = hiresScanout && superSampling == 16 && [] {
        const char *v = std::getenv("MAXIMO_SCANOUT");
        return v && std::atoi(v) == 4;
    }();
    // MAXIMO_FSR=1: AMD FidelityFX FSR 1 on the GPU (EASU upscale, then RCAS sharpening; the shaders come with
    // paraLLEl-GS/Granite) scales each presented frame to the window's picture size before the readback, instead of
    // the window's bilinear stretch. Only axes the window shows larger than the scanout are scaled. Capture frames
    // (validate, MAXIMO_GPU_SYNC_READBACK) stay unscaled.
    const bool fsr = envFlag("MAXIMO_FSR", false);
    const float fsrSharpness = fsrSharpnessFromEnv();
    Vulkan::Program *fsrUpscale = nullptr, *fsrSharpen = nullptr;
    uint32_t fsrLoggedIn[2] = {}, fsrLoggedOut[2] = {};
    // MAXIMO_SMAA=1: SMAA 1x (ULTRA preset, luma edges) on each presented scanout, before FSR. Meant for the 4x
    // scanout, which shows one super sample per pixel and so has no anti-aliasing of its own. Capture frames
    // (validate, MAXIMO_GPU_SYNC_READBACK) are left untouched, like FSR.
    const bool smaa = envFlag("MAXIMO_SMAA", false);
    Vulkan::Program *smaaEdge = nullptr, *smaaWeight = nullptr, *smaaBlend = nullptr;
    bool smaaFailed = false;
    uint32_t smaaLogged[2] = {};
    // POST EFFECTS (debug menu / MAXIMO_POST=1|2): bloom and a light grade on the GPU, after SMAA, before FSR.
    Vulkan::Program *postBright = nullptr, *postBlur = nullptr, *postComposite = nullptr;
    bool postFailed = false;
    // SCENE LIGHTING (debug menu / MAXIMO_SCENE_LIGHT=1..4): ambient occlusion from the game's Z buffer in GS memory.
    Vulkan::Program *lightAo = nullptr, *lightComposite = nullptr;
    bool lightFailed = false;
    uint64_t lightLoggedKey = ~0ull;
    float lightLogFar = 20.0f; // debug depth view: log2(z) shown as black (updated from the depth statistics)
    Vulkan::BufferHandle lightStatsBuffer;
    Vulkan::Context context;
    Vulkan::Device device;
    // SMAA's lookup tables (after `device`, so they are released before it).
    Vulkan::ImageHandle smaaAreaTex, smaaSearchTex;
    ParallelGS::GSInterface gs;
    // MAXIMO_HDTEX_PACKS: HD texture replacement (after `gs`, so it is released first).
    std::unique_ptr<hdtex::Runtime> hdtex;
    bool attempted = false;
    bool ready = false;
    uint64_t packets = 0;
    uint64_t vsyncs = 0;
    BypassCounts atInit;
    // MAXIMO_GPU_SHADOW_STREAM=<file>: every forwarded packet as {u32 path, u32 bytes, payload}, and every vsync as
    // {u32 0xFFFFFFFF, u32 8 + 20 * 8, u64 guestFrame, GSRegisters as 20 u64}. For offline decoding and replay.
    std::FILE *stream = nullptr;

    // GPU presentation (MAXIMO_GS_BACKEND=gpu): the latest scanout, handed to the window thread.
    bool presenting = false;
    Vulkan::BufferHandle readbackBuffer;
    std::vector<uint8_t> framePixels;
    // Presentation readback one frame behind: frame N's copy is submitted and frame N-1's (complete by then) is shown,
    // so the game thread does not wait for the GPU. Capture frames and MAXIMO_GPU_SYNC_READBACK=1 read synchronously.
    struct PendingReadback
    {
        Vulkan::BufferHandle buffer;
        Vulkan::Fence fence;
        uint32_t width = 0, height = 0;
        VkFormat format = VK_FORMAT_UNDEFINED;
        bool valid = false;
    };
    PendingReadback pendingReadback[2];
    unsigned nextReadback = 0;
    const bool syncReadback = [] {
        const char *v = std::getenv("MAXIMO_GPU_SYNC_READBACK");
        return v && std::atoi(v) != 0;
    }();
    // FRAME PACING: finished frames wait in a short queue and the window takes them one per present, oldest first.
    // The GS worker finishes frames whenever their work is done, not on the window's beat; with a single slot a
    // present could find two new frames (one never shown) and the next none (one shown twice), which is the judder
    // of uneven pacing. The queue holds kQueueMax; a backlog that persists is trimmed by one frame so it adds at most
    // about a frame of latency.
    struct QueuedFrame
    {
        std::vector<uint8_t> pixels;
        uint32_t width = 0, height = 0;
        uint64_t guestFrame = 0;
    };
    static constexpr size_t kQueueMax = 3;
    std::mutex frameMutex;
    std::deque<QueuedFrame> frameQueue;
    std::vector<std::vector<uint8_t>> spareBuffers; // buffers of shown or dropped frames, refilled by later ones
    uint32_t backlogTakes = 0;
    uint64_t presentGeneration = 0, takenGeneration = 0;
    std::atomic<uint64_t> queueDropped{0}, queueTrimmed{0};
    uint64_t submitNs = 0, readbackNs = 0;
    uint32_t timedFrames = 0;
    std::FILE *frameHashes = nullptr;
    // takeHealth counters.
    std::atomic<uint64_t> healthPublished{0}, healthTaken{0}, healthBlack{0}, healthNoScanout{0}, healthFailed{0};
    std::atomic<uint32_t> lastWidth{0}, lastHeight{0}, lastBrightnessMean{0}, lastBrightnessMax{0};

    void noteBrightness(const std::vector<uint8_t> &pixels, uint32_t width, uint32_t height)
    {
        uint64_t sum = 0;
        uint32_t max = 0, n = 0;
        if (width && height && pixels.size() >= size_t(width) * height * 4)
            for (uint32_t sy = 0; sy < 48; ++sy)
                for (uint32_t sx = 0; sx < 64; ++sx)
                {
                    const uint8_t *p = pixels.data() + (size_t(sy * height / 48) * width + sx * width / 64) * 4;
                    const uint32_t v = std::max({p[0], p[1], p[2]});
                    sum += v;
                    max = std::max(max, v);
                    ++n;
                }
        lastWidth.store(width, std::memory_order_relaxed);
        lastHeight.store(height, std::memory_order_relaxed);
        lastBrightnessMean.store(n ? uint32_t(sum / n) : 0u, std::memory_order_relaxed);
        lastBrightnessMax.store(max, std::memory_order_relaxed);
        healthPublished.fetch_add(1, std::memory_order_relaxed);
        if (max < 8)
            healthBlack.fetch_add(1, std::memory_order_relaxed);
    }

    // GPU mode: the device runs on a GS worker thread (MAXIMO_GPU_WORKER=0: on the game thread, as before). The game
    // thread appends packets to a batch and hands batches and vsyncs over in order, and the worker replays them on
    // the device, so it receives the same command stream either way. Capture frames drain the worker and run on the
    // game thread.
    struct WorkItem
    {
        std::vector<uint8_t> packets; // records of {u32 path, u32 bytes, u64 pad} + payload, 16-byte aligned
        bool vsync = false;
        uint64_t guestFrame = 0;
        PrivSnapshot priv{};
    };
    static constexpr size_t kBatchBytes = 64 * 1024;
    static constexpr uint32_t kMaxQueuedVsyncs = 2; // the game thread waits when the worker is this far behind
    bool useWorker = false;
    std::thread worker;
    std::mutex workMutex;
    std::condition_variable workReady, workDone;
    std::deque<WorkItem> work;
    std::vector<std::vector<uint8_t>> spareBatches;
    std::vector<uint8_t> batch;
    uint32_t queuedVsyncs = 0;
    bool workerBusy = false, stopWorker = false;
    std::atomic<bool> workerFailed{false};

    ~Impl()
    {
        if (worker.joinable())
        {
            {
                std::lock_guard<std::mutex> lock(workMutex);
                if (!batch.empty())
                    work.push_back(WorkItem{std::move(batch)});
                stopWorker = true;
            }
            workReady.notify_all();
            worker.join();
        }
        savePipelineCache();
        if (stream)
            std::fclose(stream);
        if (frameHashes)
            std::fclose(frameHashes);
    }

    void queuePacket(uint32_t path, const uint8_t *data, uint32_t size)
    {
        // Appended, not resized and then overwritten (resize zero-fills every byte first), into a batch that keeps
        // its capacity between hand-overs.
        const size_t padded = (size_t(size) + 15) & ~size_t(15);
        if (batch.capacity() < kBatchBytes + 16 + padded)
            batch.reserve(std::max(batch.size() + 16 + padded, kBatchBytes * 2));
        const uint32_t header[4] = {path, size, 0, 0};
        const uint8_t *h = reinterpret_cast<const uint8_t *>(header);
        batch.insert(batch.end(), h, h + sizeof(header));
        batch.insert(batch.end(), data, data + size);
        batch.insert(batch.end(), padded - size, uint8_t{0});
        if (batch.size() >= kBatchBytes)
            handOver(nullptr);
    }

    // Queues the pending batch and, when `vsync` is set, that vsync after it.
    void handOver(const WorkItem *vsync)
    {
        {
            std::lock_guard<std::mutex> lock(workMutex);
            if (!batch.empty())
            {
                work.push_back(WorkItem{std::move(batch)});
                batch.clear();
                if (!spareBatches.empty())
                {
                    batch = std::move(spareBatches.back());
                    spareBatches.pop_back();
                }
            }
            if (vsync)
            {
                work.push_back(*vsync);
                ++queuedVsyncs;
            }
        }
        workReady.notify_one();
    }

    void waitForWorker(bool idle)
    {
        std::unique_lock<std::mutex> lock(workMutex);
        workDone.wait(lock, [&] { return idle ? work.empty() && !workerBusy : queuedVsyncs <= kMaxQueuedVsyncs; });
    }

    void workerLoop()
    {
        Util::register_thread_index(0);
        for (;;)
        {
            WorkItem item;
            {
                std::unique_lock<std::mutex> lock(workMutex);
                workReady.wait(lock, [&] { return stopWorker || !work.empty(); });
                if (work.empty())
                    return;
                item = std::move(work.front());
                work.pop_front();
                workerBusy = true;
            }
            for (size_t at = 0; at + 16 <= item.packets.size();)
            {
                uint32_t header[2];
                std::memcpy(header, item.packets.data() + at, sizeof(header));
                gs.gif_transfer(header[0], item.packets.data() + at + 16, header[1]);
                at += 16 + ((size_t(header[1]) + 15) & ~size_t(15));
            }
            if (item.vsync && !processVsync(item.guestFrame, false, item.priv))
                workerFailed.store(true);
            {
                std::lock_guard<std::mutex> lock(workMutex);
                if (item.vsync)
                    --queuedVsyncs;
                else if (spareBatches.size() < 8)
                {
                    item.packets.clear();
                    spareBatches.push_back(std::move(item.packets));
                }
                workerBusy = false;
            }
            workDone.notify_all();
        }
    }

    void logPacket(uint32_t path, const uint8_t *data, uint32_t size)
    {
        if (!stream)
            return;
        const uint32_t header[2] = {path, size};
        std::fwrite(header, sizeof(header), 1, stream);
        std::fwrite(data, 1, size, stream);
    }

    void logVsync(uint64_t guestFrame, const GSRegisters &regs, uint64_t csr)
    {
        if (!stream)
            return;
        uint64_t raw[21] = {guestFrame};
        std::memcpy(raw + 1, &regs, sizeof(GSRegisters));
        raw[1 + 15] = csr;
        raw[1 + 16] = regs.vsyncTick.load(std::memory_order_relaxed);
        const uint32_t header[2] = {0xFFFFFFFFu, uint32_t(sizeof(raw))};
        std::fwrite(header, sizeof(header), 1, stream);
        std::fwrite(raw, sizeof(raw), 1, stream);
        std::fflush(stream);
    }

    bool initialize()
    {
        if (attempted)
            return ready;
        attempted = true;
        const auto tStart = std::chrono::steady_clock::now();
        if (!Vulkan::Context::init_loader(nullptr))
        {
            std::fprintf(stderr, "[gpu-shadow] Vulkan loader initialization failed\n");
            return false;
        }
        context.set_num_thread_indices(1);
        if (!context.init_instance_and_device(nullptr, 0, nullptr, 0,
                Vulkan::CONTEXT_CREATION_ENABLE_PUSH_DESCRIPTOR_BIT |
                Vulkan::CONTEXT_CREATION_ENABLE_DESCRIPTOR_HEAP_BIT |
                Vulkan::CONTEXT_CREATION_ENABLE_DESCRIPTOR_BUFFER_BIT))
        {
            std::fprintf(stderr, "[gpu-shadow] Vulkan device initialization failed\n");
            return false;
        }
        const auto tContext = std::chrono::steady_clock::now();
        device.set_context(context);
        loadPipelineCache();
        device.init_frame_contexts(4);
        ParallelGS::GSOptions options = {};
        options.super_sampling = ParallelGS::SuperSampling(superSampling);
        // MAXIMO_SSAA_TEXTURES=1: render targets reused as textures keep their samples (helps blits; costs memory).
        options.super_sampled_textures = superSampling > 1 && envFlag("MAXIMO_SSAA_TEXTURES", false);
        if (!gs.init(&device, options))
        {
            std::fprintf(stderr, "[gpu-shadow] paraLLEl-GS initialization failed\n");
            return false;
        }
        // Scene depth for SCENE LIGHTING: the Maximo ubershader records every opaque primitive's Z (the game's own Z
        // buffer stays nearly empty: it draws its world without Z writes).
        gs.enable_scene_depth();
        const auto tGs = std::chrono::steady_clock::now();
        auto ms = [](auto a, auto b) { return std::chrono::duration<double, std::milli>(b - a).count(); };
        std::fprintf(stderr, "[gpu] start-up: Vulkan instance and device %.0f ms, paraLLEl-GS (shaders, buffers) %.0f ms\n",
                     ms(tStart, tContext), ms(tContext, tGs));
        if (superSampling > 1)
            std::fprintf(stderr, "[gpu] enhancement: %ux SSAA, high-resolution scanout %s, super-sampled textures %s\n",
                         superSampling, hiresScanout ? (ultraScanout ? "on (4x)" : "on (2x)") : "off",
                         options.super_sampled_textures ? "on" : "off");
        if (fsr)
            std::fprintf(stderr, "[gpu] enhancement: FSR 1 scaling to the window, RCAS sharpness %.2f%s\n",
                         fsrSharpness, fsrSharpness < 0.0f ? " (RCAS off)" : "");
        if (smaa)
            std::fprintf(stderr, "[gpu] enhancement: SMAA 1x (ultra preset) on the scanout\n");
        void *gpuVram = gs.map_vram_write(0, PS2_GS_VRAM_SIZE);
        if (!gpuVram)
            return false;
        std::memcpy(gpuVram, runtime.memory().getGSVRAM(), PS2_GS_VRAM_SIZE);
        gs.end_vram_write(0, PS2_GS_VRAM_SIZE);
        hdtex = hdtex::Runtime::fromEnv();
        if (hdtex)
            hdtex->attach(gs, device, runtime.memory().getGSVRAM());
        // MAXIMO_SOFT_PARTICLES=1: tiny blended triangles (fire, smoke, sparkles) become soft splats when super-sampling.
        // MAXIMO_SOFT_PARTICLE_AREA: the size limit in PS2 pixels^2 (default 4).
        if (superSampling > 1 && envFlag("MAXIMO_SOFT_PARTICLES", false))
        {
            const char *areaEnv = std::getenv("MAXIMO_SOFT_PARTICLE_AREA");
            const float area = areaEnv ? float(std::atof(areaEnv)) : 4.0f;
            gs.set_soft_particles(true, area > 0.0f ? area : 4.0f);
            std::fprintf(stderr, "[gpu] enhancement: soft particles (triangles under %.1f PS2 pixels^2)\n",
                         area > 0.0f ? area : 4.0f);
        }
        atInit = BypassCounts::read(runtime.gs().streamStats());
        if (const char *streamPath = std::getenv("MAXIMO_GPU_SHADOW_STREAM"))
            stream = std::fopen(streamPath, "wb");
        if (const char *hashPath = std::getenv("MAXIMO_GPU_FRAME_HASHES"))
            frameHashes = std::fopen(hashPath, "w");
        ready = true;
        const char *workerEnv = std::getenv("MAXIMO_GPU_WORKER");
        useWorker = presenting && !syncReadback && !(workerEnv && std::atoi(workerEnv) == 0);
        if (useWorker)
            worker = std::thread([this] { workerLoop(); });
        std::fprintf(stderr, "[gpu] device runs on %s\n", useWorker ? "a GS worker thread" : "the game thread");
        std::fprintf(stderr, "[gpu-shadow] paraLLEl-GS initialized with CPU VRAM snapshot (CPU packets so far %llu)\n",
                     static_cast<unsigned long long>(atInit.packets));
        return true;
    }

    // The Vulkan pipeline cache, kept between runs (MAXIMO_PIPELINE_CACHE: the file, set by maximo_boot next to the
    // executable): paraLLEl-GS builds its pipelines at start-up and as new kinds of drawing appear (the "stalled
    // compile" hitches); with the cache the driver can skip compiling them again. Saved after a minute of play and
    // when the game closes.
    std::string pipelineCachePath()
    {
        const char *v = std::getenv("MAXIMO_PIPELINE_CACHE");
        return v && *v && std::strcmp(v, "0") != 0 ? std::string(v) : std::string();
    }
    void loadPipelineCache()
    {
        const std::string path = pipelineCachePath();
        if (path.empty())
            return;
        std::vector<uint8_t> data;
        if (std::FILE *f = std::fopen(path.c_str(), "rb"))
        {
            std::fseek(f, 0, SEEK_END);
            const long size = std::ftell(f);
            std::fseek(f, 0, SEEK_SET);
            if (size > 0 && size < (512l << 20))
            {
                data.resize(size_t(size));
                if (std::fread(data.data(), 1, data.size(), f) != data.size())
                    data.clear();
            }
            std::fclose(f);
        }
        const bool ok = device.init_pipeline_cache(data.empty() ? nullptr : data.data(), data.size());
        std::fprintf(stderr, "[gpu] pipeline cache %s: %zu bytes from %s\n", ok ? "ready" : "FAILED", data.size(), path.c_str());
    }
    void savePipelineCache()
    {
        const std::string path = pipelineCachePath();
        if (path.empty() || !ready)
            return;
        const size_t size = device.get_pipeline_cache_size();
        if (size == 0)
            return;
        std::vector<uint8_t> data(size);
        if (!device.get_pipeline_cache_data(data.data(), size))
            return;
        const std::string temp = path + ".tmp";
        if (std::FILE *f = std::fopen(temp.c_str(), "wb"))
        {
            const bool written = std::fwrite(data.data(), 1, size, f) == size;
            std::fclose(f);
            std::remove(path.c_str());
            if (written && std::rename(temp.c_str(), path.c_str()) == 0)
                std::fprintf(stderr, "[gpu] pipeline cache saved: %zu bytes\n", size);
        }
    }

    // The shadow only sees arbiter packets. Any other GS input since initialisation means its state is incomplete.
    bool reportCompleteness(uint64_t guestFrame)
    {
        const GSStreamStats &stats = runtime.gs().streamStats();
        const BypassCounts now = BypassCounts::read(stats);
        const uint64_t cpuPackets = now.packets - atInit.packets;
        const uint64_t direct = now.directRegWrites - atInit.directRegWrites;
        const uint64_t packed = now.nativePacked - atInit.nativePacked;
        const uint64_t uploads = now.nativeUploads - atInit.nativeUploads;
        const uint64_t readbacks = now.localToHost - atInit.localToHost;
        const bool complete = cpuPackets == packets && direct == 0 && packed == 0 && uploads == 0;
        std::fprintf(stderr,
                     "[gpu-shadow] frame=%llu stream: shadowPackets=%llu cpuPackets=%llu directRegWrites=%llu "
                     "nativePacked=%llu nativeUploads=%llu localToHostReads=%llu trxdir=%llu/%llu/%llu -> %s\n",
                     static_cast<unsigned long long>(guestFrame), static_cast<unsigned long long>(packets),
                     static_cast<unsigned long long>(cpuPackets), static_cast<unsigned long long>(direct),
                     static_cast<unsigned long long>(packed), static_cast<unsigned long long>(uploads),
                     static_cast<unsigned long long>(readbacks),
                     static_cast<unsigned long long>(stats.trxdir[0].load(std::memory_order_relaxed)),
                     static_cast<unsigned long long>(stats.trxdir[1].load(std::memory_order_relaxed)),
                     static_cast<unsigned long long>(stats.trxdir[2].load(std::memory_order_relaxed)),
                     complete ? "complete" : "INCOMPLETE");
        return complete;
    }

    bool compareVram(uint64_t guestFrame)
    {
        const auto *gpu = static_cast<const uint8_t *>(gs.map_vram_read(0, PS2_GS_VRAM_SIZE));
        const uint8_t *cpu = runtime.memory().getGSVRAM();
        if (!gpu || !cpu)
            return false;
        char name[80];
        std::snprintf(name, sizeof(name), "gpu_vram_%04llu.bin", static_cast<unsigned long long>(guestFrame));
        bool ok = writeFile(name, gpu, PS2_GS_VRAM_SIZE);
        std::snprintf(name, sizeof(name), "cpu_vram_%04llu.bin", static_cast<unsigned long long>(guestFrame));
        ok = writeFile(name, cpu, PS2_GS_VRAM_SIZE) && ok;

        constexpr size_t kPage = 8192;
        size_t diffBytes = 0, diffPages = 0;
        for (size_t page = 0; page < PS2_GS_VRAM_SIZE / kPage; ++page)
        {
            size_t pageDiff = 0;
            for (size_t i = page * kPage; i < (page + 1) * kPage; ++i)
                pageDiff += gpu[i] != cpu[i];
            diffBytes += pageDiff;
            diffPages += pageDiff != 0;
        }
        std::fprintf(stderr, "[gpu-shadow] frame=%llu vram: %zu of %u bytes differ in %zu of %zu 8KiB pages\n",
                     static_cast<unsigned long long>(guestFrame), diffBytes, unsigned(PS2_GS_VRAM_SIZE), diffPages,
                     size_t(PS2_GS_VRAM_SIZE / kPage));
        return ok;
    }

    // Copies the scanout image to host memory as RGBA8 and waits for the copy (explicit completion).
    bool readScanout(uint64_t guestFrame, const ParallelGS::ScanoutResult &scanout, std::vector<uint8_t> &pixels,
                     uint32_t &width, uint32_t &height)
    {
        if (!scanout.image)
        {
            std::fprintf(stderr, "[gpu-shadow] frame %llu: no scanout image\n",
                         static_cast<unsigned long long>(guestFrame));
            return false;
        }
        width = scanout.image->get_width();
        height = scanout.image->get_height();
        const VkFormat format = scanout.image->get_format();
        if (width == 0 || height == 0 ||
            (format != VK_FORMAT_R8G8B8A8_UNORM && format != VK_FORMAT_B8G8R8A8_UNORM))
        {
            std::fprintf(stderr, "[gpu-shadow] frame %llu: unsupported scanout %ux%u format=%u\n",
                         static_cast<unsigned long long>(guestFrame), width, height, unsigned(format));
            return false;
        }
        const size_t bytes = size_t(width) * height * 4;
        if (!readbackBuffer || readbackBuffer->get_create_info().size != bytes)
        {
            Vulkan::BufferCreateInfo bufferInfo = {};
            bufferInfo.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
            bufferInfo.domain = Vulkan::BufferDomain::CachedHost;
            bufferInfo.size = bytes;
            readbackBuffer = device.create_buffer(bufferInfo);
            if (!readbackBuffer)
                return false;
        }
        auto &buffer = readbackBuffer;
        auto cmd = device.request_command_buffer();
        cmd->copy_image_to_buffer(*buffer, *scanout.image, 0, {}, {width, height, 1},
                                  0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1});
        cmd->barrier(VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
                     VK_PIPELINE_STAGE_HOST_BIT, VK_ACCESS_HOST_READ_BIT);
        Vulkan::Fence fence;
        device.submit(cmd, &fence);
        fence->wait();
        auto *src = static_cast<const uint8_t *>(device.map_host_buffer(*buffer, Vulkan::MEMORY_ACCESS_READ_BIT));
        if (!src)
            return false;
        pixels.assign(src, src + bytes);
        device.unmap_host_buffer(*buffer, Vulkan::MEMORY_ACCESS_READ_BIT);
        if (format == VK_FORMAT_B8G8R8A8_UNORM)
            for (size_t i = 0; i < pixels.size(); i += 4)
                std::swap(pixels[i], pixels[i + 2]);
        return true;
    }

    // Records the copy of the scanout into `p` and submits it without waiting.
    bool startReadback(uint64_t guestFrame, const ParallelGS::ScanoutResult &scanout, PendingReadback &p)
    {
        p.width = scanout.image->get_width();
        p.height = scanout.image->get_height();
        p.format = scanout.image->get_format();
        if (p.width == 0 || p.height == 0 || (p.format != VK_FORMAT_R8G8B8A8_UNORM && p.format != VK_FORMAT_B8G8R8A8_UNORM))
        {
            std::fprintf(stderr, "[gpu-shadow] frame %llu: unsupported scanout %ux%u format=%u\n",
                         static_cast<unsigned long long>(guestFrame), p.width, p.height, unsigned(p.format));
            return false;
        }
        const size_t bytes = size_t(p.width) * p.height * 4;
        if (!p.buffer || p.buffer->get_create_info().size != bytes)
        {
            Vulkan::BufferCreateInfo bufferInfo = {};
            bufferInfo.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
            bufferInfo.domain = Vulkan::BufferDomain::CachedHost;
            bufferInfo.size = bytes;
            p.buffer = device.create_buffer(bufferInfo);
            if (!p.buffer)
                return false;
        }
        auto cmd = device.request_command_buffer();
        cmd->copy_image_to_buffer(*p.buffer, *scanout.image, 0, {}, {p.width, p.height, 1},
                                  0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1});
        cmd->barrier(VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
                     VK_PIPELINE_STAGE_HOST_BIT, VK_ACCESS_HOST_READ_BIT);
        p.fence.reset();
        device.submit(cmd, &p.fence);
        p.valid = true;
        return true;
    }

    // `count` pixels from `src` into `pixels` as opaque RGBA (32-bit words, so the compiler vectorises it).
    static void copyOpaque(std::vector<uint8_t> &pixels, const uint8_t *src, size_t count, bool bgra)
    {
        if (pixels.size() != count * 4)
            pixels.resize(count * 4);
        uint8_t *dst = pixels.data();
        if (!bgra)
        {
            for (size_t i = 0; i < count; ++i)
            {
                uint32_t v;
                std::memcpy(&v, src + i * 4, 4);
                v |= 0xFF000000u;
                std::memcpy(dst + i * 4, &v, 4);
            }
        }
        else
        {
            for (size_t i = 0; i < count; ++i)
            {
                uint32_t v;
                std::memcpy(&v, src + i * 4, 4);
                v = (v & 0x0000FF00u) | ((v >> 16) & 0xFFu) | ((v & 0xFFu) << 16) | 0xFF000000u;
                std::memcpy(dst + i * 4, &v, 4);
            }
        }
    }

    // Waits for the copy in `p` (normally long finished) and converts it to RGBA.
    void finishReadback(PendingReadback &p, std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height)
    {
        p.fence->wait();
        p.valid = false;
        const size_t bytes = size_t(p.width) * p.height * 4;
        auto *src = static_cast<const uint8_t *>(device.map_host_buffer(*p.buffer, Vulkan::MEMORY_ACCESS_READ_BIT));
        if (!src)
            return;
        // One pass over the frame: the copy out of the mapped buffer also makes it opaque (the CRTC outputs RGB only;
        // the window blends with alpha) and, for a BGRA scanout, swaps red and blue. It used to be a copy and then
        // one or two byte loops over the whole picture (10 MB at 2560x1022), every frame.
        copyOpaque(pixels, src, size_t(p.width) * p.height, p.format == VK_FORMAT_B8G8R8A8_UNORM);
        device.unmap_host_buffer(*p.buffer, Vulkan::MEMORY_ACCESS_READ_BIT);
        width = p.width;
        height = p.height;
    }

    void dropPendingReadbacks()
    {
        for (PendingReadback &p : pendingReadback)
            if (p.valid)
            {
                p.fence->wait();
                p.valid = false;
            }
    }

    bool saveScanout(uint64_t guestFrame, const std::vector<uint8_t> &pixels, uint32_t width, uint32_t height)
    {
        char filename[80];
        std::snprintf(filename, sizeof(filename), "gpu_sync_%04llu_%ux%u.rgba",
                      static_cast<unsigned long long>(guestFrame), width, height);
        const GSRegisters &regs = runtime.memory().gs();
        std::fprintf(stderr, "[gpu-shadow] frame=%llu scanout %ux%u saved=%s pmode=%llx smode2=%llx "
                     "dispfb1=%llx display1=%llx dispfb2=%llx display2=%llx\n",
                     static_cast<unsigned long long>(guestFrame), width, height, filename,
                     static_cast<unsigned long long>(regs.pmode), static_cast<unsigned long long>(regs.smode2),
                     static_cast<unsigned long long>(regs.dispfb1), static_cast<unsigned long long>(regs.display1),
                     static_cast<unsigned long long>(regs.dispfb2), static_cast<unsigned long long>(regs.display2));
        return writeFile(filename, pixels.data(), pixels.size());
    }

    // One full-screen triangle (upscale.vert/sharpen.vert read a vec2 position at location 0).
    static void drawFullscreenTriangle(Vulkan::CommandBuffer &cmd)
    {
        auto *data = static_cast<float *>(cmd.allocate_vertex_data(0, 6 * sizeof(float), 2 * sizeof(float)));
        const float tri[6] = {-1.0f, -3.0f, -1.0f, 1.0f, 3.0f, 1.0f};
        std::memcpy(data, tri, sizeof(tri));
        cmd.set_vertex_attrib(0, 0, VK_FORMAT_R32G32_SFLOAT, 0);
        cmd.set_primitive_topology(VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST);
        cmd.draw(3);
    }

    // MAXIMO_SMAA: anti-aliases the scanout (TRANSFER_SRC layout) at its own size and returns the result in
    // TRANSFER_SRC layout, or the scanout itself if SMAA could not be set up. Three passes, as in Granite's
    // renderer/post/smaa.cpp without the stencil mask: luma edges -> blend weights -> neighbourhood blend.
    Vulkan::ImageHandle applySmaa(uint64_t guestFrame, const Vulkan::ImageHandle &src)
    {
        if (smaaFailed)
            return src;
        const uint32_t width = src->get_width(), height = src->get_height();
        if (!smaaEdge)
        {
            // This Granite build has no SPIRV-Cross, so the shader interfaces are spelled out: every vertex shader
            // reads vec2 Position (location 0); every stage sees the 16-byte rt_metrics push constant; each fragment
            // shader samples `samplers` float textures at set 0, bindings 0.. and writes one colour output. The
            // prerotate spec constants (8-11) stay at their identity defaults, so no spec constant mask.
            Vulkan::ResourceLayout vsLayout;
            vsLayout.input_mask = 1u;
            vsLayout.push_constant_size = 16;
            auto program = [&](const uint32_t *vs, size_t vsSize, const uint32_t *fs, size_t fsSize,
                               uint32_t samplers) -> Vulkan::Program * {
                Vulkan::ResourceLayout fsLayout;
                fsLayout.output_mask = 1u;
                fsLayout.push_constant_size = 16;
                fsLayout.sets[0].sampled_image_mask = (1u << samplers) - 1u;
                fsLayout.sets[0].fp_mask = (1u << samplers) - 1u;
                for (uint32_t b = 0; b < samplers; ++b)
                    fsLayout.sets[0].meta[b].array_size = 1;
                Vulkan::Shader *vertex = device.request_shader(vs, vsSize, &vsLayout);
                Vulkan::Shader *fragment = device.request_shader(fs, fsSize, &fsLayout);
                return vertex && fragment ? device.request_program(vertex, fragment) : nullptr;
            };
            smaaEdge = program(maximo_smaa_edge_detection_vert, sizeof(maximo_smaa_edge_detection_vert),
                               maximo_smaa_edge_detection_frag, sizeof(maximo_smaa_edge_detection_frag), 1);
            smaaWeight = program(maximo_smaa_blend_weight_vert, sizeof(maximo_smaa_blend_weight_vert),
                                 maximo_smaa_blend_weight_frag, sizeof(maximo_smaa_blend_weight_frag), 3);
            smaaBlend = program(maximo_smaa_neighbor_blend_vert, sizeof(maximo_smaa_neighbor_blend_vert),
                                maximo_smaa_neighbor_blend_frag, sizeof(maximo_smaa_neighbor_blend_frag), 2);
            auto areaInfo = Vulkan::ImageCreateInfo::immutable_2d_image(AREATEX_WIDTH, AREATEX_HEIGHT,
                                                                        VK_FORMAT_R8G8_UNORM);
            const Vulkan::ImageInitialData area = {areaTexBytes, AREATEX_WIDTH, AREATEX_HEIGHT};
            smaaAreaTex = device.create_image(areaInfo, &area);
            auto searchInfo = Vulkan::ImageCreateInfo::immutable_2d_image(SEARCHTEX_WIDTH, SEARCHTEX_HEIGHT,
                                                                          VK_FORMAT_R8_UNORM);
            const Vulkan::ImageInitialData search = {searchTexBytes, SEARCHTEX_WIDTH, SEARCHTEX_HEIGHT};
            smaaSearchTex = device.create_image(searchInfo, &search);
            if (!smaaEdge || !smaaWeight || !smaaBlend || !smaaAreaTex || !smaaSearchTex)
            {
                std::fprintf(stderr, "[gpu] SMAA setup failed; presenting without it\n");
                smaaFailed = true;
                return src;
            }
        }
        if (smaaLogged[0] != width || smaaLogged[1] != height)
        {
            smaaLogged[0] = width;
            smaaLogged[1] = height;
            std::fprintf(stderr, "[gpu] frame=%llu SMAA %ux%u\n", static_cast<unsigned long long>(guestFrame), width,
                         height);
        }

        auto makeTarget = [&](VkFormat format, bool transferSrc) {
            auto info = Vulkan::ImageCreateInfo::immutable_2d_image(width, height, format);
            info.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
            info.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                         (transferSrc ? VK_IMAGE_USAGE_TRANSFER_SRC_BIT : 0);
            return device.create_image(info);
        };
        Vulkan::ImageHandle edges = makeTarget(VK_FORMAT_R8G8_UNORM, false);
        Vulkan::ImageHandle weights = makeTarget(VK_FORMAT_R8G8B8A8_UNORM, false);
        Vulkan::ImageHandle result = makeTarget(VK_FORMAT_R8G8B8A8_UNORM, true);
        if (!edges || !weights || !result)
            return src;

        const float rtMetrics[4] = {1.0f / float(width), 1.0f / float(height), float(width), float(height)};
        auto cmd = device.request_command_buffer();
        cmd->image_barrier(*src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                           VK_PIPELINE_STAGE_2_COPY_BIT, 0,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
        auto pass = [&](const Vulkan::Image &target, bool clear, auto &&body) {
            cmd->image_barrier(target, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, 0, 0,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT);
            Vulkan::RenderPassInfo rp;
            rp.num_color_attachments = 1;
            rp.color_attachments[0] = &target.get_view();
            rp.store_attachments = 1u << 0;
            if (clear)
            {
                rp.clear_attachments = 1u << 0;
                std::memset(&rp.clear_color[0], 0, sizeof(rp.clear_color[0]));
            }
            cmd->begin_render_pass(rp);
            cmd->set_opaque_state();
            body();
            cmd->push_constants(rtMetrics, 0, sizeof(rtMetrics));
            drawFullscreenTriangle(*cmd);
            cmd->end_render_pass();
            cmd->image_barrier(target, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
        };
        pass(*edges, true, [&] {
            cmd->set_program(smaaEdge);
            cmd->set_texture(0, 0, src->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        pass(*weights, true, [&] {
            cmd->set_program(smaaWeight);
            cmd->set_texture(0, 0, edges->get_view(), Vulkan::StockSampler::LinearClamp);
            cmd->set_texture(0, 1, smaaAreaTex->get_view(), Vulkan::StockSampler::LinearClamp);
            cmd->set_texture(0, 2, smaaSearchTex->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        pass(*result, false, [&] {
            cmd->set_program(smaaBlend);
            cmd->set_texture(0, 0, src->get_view(), Vulkan::StockSampler::LinearClamp);
            cmd->set_texture(0, 1, weights->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        // The scanout goes back to TRANSFER_SRC (it may still be read), the result is handed on in TRANSFER_SRC.
        cmd->image_barrier(*src, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                           VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
        cmd->image_barrier(*result, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                           VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
        device.submit(cmd);
        return result;
    }

    // SCENE LIGHTING: ambient occlusion computed from the game's own depth buffer. The Z buffer is read straight out
    // of GS local memory (the single-sampled VRAM paraLLEl-GS keeps on the GPU) with the PSMZ32 swizzle, at the
    // displayed buffer's position (DISPFB DBX/DBY) and single-sampled size; the occlusion then darkens the scanout.
    // Level 1 normal, 2 strong, 3 shows the depth, 4 shows the occlusion (to check the depth is read right).
    Vulkan::ImageHandle applyLighting(uint64_t guestFrame, const ParallelGS::ScanoutResult &scanout, int level)
    {
        const Vulkan::ImageHandle &src = scanout.image;
        if (lightFailed || level <= 0 || !src)
            return src;
        const auto &priv = gs.get_priv_register_state();
        const auto &dispfb = priv.pmode.EN2 ? priv.dispfb2 : priv.dispfb1;
        const uint32_t fbp = dispfb.FBP, fbw = dispfb.FBW, fpsm = dispfb.PSM;
        const uint32_t dbx = dispfb.DBX, dby = dispfb.DBY;
        const uint32_t iw = scanout.internal_width, ih = scanout.internal_height;
        // (FBP alternates between the two buffers every frame, so it is not part of the logged key.)
        const uint64_t key = (uint64_t(fpsm) << 36) ^ (uint64_t(fbw) << 28) ^
                             (uint64_t(dbx) << 16) ^ (uint64_t(dby) << 4) ^ (uint64_t(iw) << 48) ^ ih;
        if (key != lightLoggedKey)
        {
            lightLoggedKey = key;
            std::fprintf(stderr, "[gpu] frame=%llu scene lighting: displayed buffer FBP=%u FBW=%u PSM=%u at %u,%u, "
                                 "size %ux%u (scanout %ux%u), depth from the scene depth buffer\n",
                         static_cast<unsigned long long>(guestFrame), fbp, fbw, fpsm, dbx, dby, iw, ih,
                         src->get_width(), src->get_height());
        }
        if (fpsm != 0 || fbw == 0 || iw < 16 || ih < 16)
            return src; // only 32-bit colour buffers carry scene depth
        Vulkan::BufferHandle vram = gs.get_scene_depth_buffer();
        const uint32_t vramSize = gs.get_vram_size();
        if (!vram || vramSize == 0 || (vramSize & (vramSize - 1)))
            return src;
        const VkDeviceSize vramOffset = 0;
        const uint32_t zbp = fbp, zpsm = 0;
        // Debug views, every ~2 s: copy the displayed buffer's scene depth back and log its distribution (log2 of Z
        // at percentiles over a 1-in-16 pixel grid), to tune the lighting to the game's depth range.
        if (level >= 3 && (guestFrame % 120) == 0)
        {
            const uint32_t pages = fbw * ((dby + ih + 31) / 32);
            const VkDeviceSize base = VkDeviceSize(fbp) * 8192u;
            const VkDeviceSize bytes = std::min<VkDeviceSize>(VkDeviceSize(pages) * 8192u, vramSize - base);
            if (!lightStatsBuffer || lightStatsBuffer->get_create_info().size < bytes)
            {
                Vulkan::BufferCreateInfo info = {};
                info.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
                info.domain = Vulkan::BufferDomain::CachedHost;
                info.size = bytes;
                lightStatsBuffer = device.create_buffer(info);
            }
            if (lightStatsBuffer && base < vramSize)
            {
                auto copy = device.request_command_buffer();
                copy->barrier(VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, VK_ACCESS_2_MEMORY_WRITE_BIT,
                              VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
                copy->copy_buffer(*lightStatsBuffer, 0, *vram, base, bytes);
                copy->barrier(VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT,
                              VK_PIPELINE_STAGE_HOST_BIT, VK_ACCESS_HOST_READ_BIT);
                Vulkan::Fence fence;
                device.submit(copy, &fence);
                fence->wait();
                const auto *w = static_cast<const uint32_t *>(
                    device.map_host_buffer(*lightStatsBuffer, Vulkan::MEMORY_ACCESS_READ_BIT));
                static const int kBlock32[32] = {0, 1, 4, 5, 16, 17, 20, 21, 2, 3, 6, 7, 18, 19, 22, 23,
                                                 8, 9, 12, 13, 24, 25, 28, 29, 10, 11, 14, 15, 26, 27, 30, 31};
                static const int kColumn32[64] = {0, 1, 4, 5, 8, 9, 12, 13, 2, 3, 6, 7, 10, 11, 14, 15,
                                                  16, 17, 20, 21, 24, 25, 28, 29, 18, 19, 22, 23, 26, 27, 30, 31,
                                                  32, 33, 36, 37, 40, 41, 44, 45, 34, 35, 38, 39, 42, 43, 46, 47,
                                                  48, 49, 52, 53, 56, 57, 60, 61, 50, 51, 54, 55, 58, 59, 62, 63};
                std::vector<float> logs;
                uint32_t zeros = 0, total = 0;
                for (uint32_t y = 0; w && y < ih; y += 4)
                    for (uint32_t x = 0; x < iw; x += 4)
                    {
                        const uint32_t px = x + dbx, py = y + dby;
                        const uint32_t page = (py >> 5) * fbw + (px >> 6);
                        const uint32_t addr = page * 2048u + uint32_t(kBlock32[((py >> 3) & 3) * 8 + ((px >> 3) & 7)]) * 64u +
                                              uint32_t(kColumn32[(py & 7) * 8 + (px & 7)]);
                        if (VkDeviceSize(addr) * 4 >= bytes)
                            continue;
                        ++total;
                        const uint32_t z = w[addr];
                        if (!z)
                            ++zeros;
                        else
                            logs.push_back(std::log2(float(z)));
                    }
                if (w)
                    device.unmap_host_buffer(*lightStatsBuffer, Vulkan::MEMORY_ACCESS_READ_BIT);
                std::sort(logs.begin(), logs.end());
                auto pct = [&](float q) { return logs.empty() ? 0.0f : logs[size_t(q * float(logs.size() - 1))]; };
                if (!logs.empty())
                    lightLogFar = pct(0.02f);
                std::fprintf(stderr, "[gpu] scene depth: %u samples, %u empty; log2(z) min %.2f p5 %.2f p25 %.2f p50 %.2f "
                                     "p75 %.2f p95 %.2f max %.2f\n",
                             total, zeros, pct(0.0f), pct(0.05f), pct(0.25f), pct(0.5f), pct(0.75f), pct(0.95f),
                             pct(1.0f));
            }
        }
        if (!lightAo)
        {
            Vulkan::ResourceLayout vsLayout;
            vsLayout.input_mask = 1u;
            vsLayout.push_constant_size = 48;
            Vulkan::Shader *vertex = device.request_shader(maximo_post_vert, sizeof(maximo_post_vert), &vsLayout);
            Vulkan::ResourceLayout aoLayout;
            aoLayout.output_mask = 1u;
            aoLayout.push_constant_size = 48;
            aoLayout.sets[0].storage_buffer_mask = 1u;
            aoLayout.sets[0].meta[0].array_size = 1;
            Vulkan::Shader *ao = device.request_shader(maximo_post_ao_frag, sizeof(maximo_post_ao_frag), &aoLayout);
            Vulkan::ResourceLayout compLayout;
            compLayout.output_mask = 1u;
            compLayout.push_constant_size = 48;
            compLayout.sets[0].sampled_image_mask = 3u;
            compLayout.sets[0].fp_mask = 3u;
            compLayout.sets[0].meta[0].array_size = 1;
            compLayout.sets[0].meta[1].array_size = 1;
            Vulkan::Shader *comp =
                device.request_shader(maximo_post_light_frag, sizeof(maximo_post_light_frag), &compLayout);
            if (vertex && ao && comp)
            {
                lightAo = device.request_program(vertex, ao);
                lightComposite = device.request_program(vertex, comp);
            }
            if (!lightAo || !lightComposite)
            {
                std::fprintf(stderr, "[gpu] scene lighting setup failed; presenting without it\n");
                lightFailed = true;
                return src;
            }
            std::fprintf(stderr, "[gpu] scene lighting ready (ambient occlusion from the GS Z buffer)\n");
        }
        const uint32_t width = src->get_width(), height = src->get_height();
        auto makeTarget = [&](uint32_t w, uint32_t h, VkFormat format, bool transferSrc) {
            auto info = Vulkan::ImageCreateInfo::immutable_2d_image(w, h, format);
            info.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
            info.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                         (transferSrc ? VK_IMAGE_USAGE_TRANSFER_SRC_BIT : 0);
            return device.create_image(info);
        };
        Vulkan::ImageHandle aoImage = makeTarget(iw, ih, VK_FORMAT_R8G8B8A8_UNORM, false);
        Vulkan::ImageHandle result = makeTarget(width, height, VK_FORMAT_R8G8B8A8_UNORM, true);
        if (!aoImage || !result)
            return src;
        auto cmd = device.request_command_buffer();
        // paraLLEl-GS wrote VRAM in compute; make it visible to the fragment shader.
        cmd->barrier(VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, VK_ACCESS_2_MEMORY_WRITE_BIT,
                     VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_STORAGE_READ_BIT);
        cmd->image_barrier(*src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                           VK_PIPELINE_STAGE_2_COPY_BIT, 0,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
        auto pass = [&](const Vulkan::Image &target, const void *push, size_t pushSize, auto &&body) {
            cmd->image_barrier(target, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, 0, 0,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT);
            Vulkan::RenderPassInfo rp;
            rp.num_color_attachments = 1;
            rp.color_attachments[0] = &target.get_view();
            rp.store_attachments = 1u << 0;
            cmd->begin_render_pass(rp);
            cmd->set_opaque_state();
            body();
            cmd->push_constants(push, 0, static_cast<uint32_t>(pushSize));
            drawFullscreenTriangle(*cmd);
            cmd->end_render_pass();
            cmd->image_barrier(target, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
        };
        struct AoPush
        {
            uint32_t zinfo[4];
            float area[4];
            float params[4];
        } aoPush = {{zbp * 2048u, fbw, zpsm == 1 ? 1u : 0u, vramSize / 4u - 1u},
                    {float(dbx), float(dby), float(iw), float(ih)},
                    {level >= 2 ? 9.0f : 7.0f, level >= 2 ? 1.6f : 1.1f, lightLogFar, level == 3 ? 1.0f : 0.0f}};
        pass(*aoImage, &aoPush, sizeof(aoPush), [&] {
            cmd->set_program(lightAo);
            cmd->set_storage_buffer(0, 0, *vram, vramOffset, vramSize);
        });
        const float compPush[12] = {1.0f / float(width), 1.0f / float(height), 1.0f / float(iw), 1.0f / float(ih),
                                    level >= 2 ? 1.0f : 0.8f, 0.0f, 0.0f, level >= 3 ? float(level - 2) : 0.0f,
                                    0.0f, 0.0f, 0.0f, 0.0f};
        pass(*result, compPush, sizeof(compPush), [&] {
            cmd->set_program(lightComposite);
            cmd->set_texture(0, 0, src->get_view(), Vulkan::StockSampler::LinearClamp);
            cmd->set_texture(0, 1, aoImage->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        cmd->image_barrier(*src, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                           VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
        cmd->image_barrier(*result, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                           VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
        device.submit(cmd);
        return result;
    }

    // POST EFFECTS: the scanout (TRANSFER_SRC layout) with bloom and a light grade, returned in TRANSFER_SRC layout,
    // or the scanout itself if the shaders could not be set up. Four passes: quarter-size bright pass, Gaussian blur
    // across and down, then the composite at full size (picture + bloom, saturation, S-curve, tint, vignette).
    Vulkan::ImageHandle applyPost(const Vulkan::ImageHandle &src, int level)
    {
        if (postFailed || level <= 0)
            return src;
        const uint32_t width = src->get_width(), height = src->get_height();
        if (width < 16 || height < 16)
            return src;
        if (!postBright)
        {
            Vulkan::ResourceLayout vsLayout;
            vsLayout.input_mask = 1u;
            vsLayout.push_constant_size = 32;
            Vulkan::Shader *vertex = device.request_shader(maximo_post_vert, sizeof(maximo_post_vert), &vsLayout);
            auto fragment = [&](const uint32_t *fs, size_t fsSize, uint32_t samplers) -> Vulkan::Shader * {
                Vulkan::ResourceLayout fsLayout;
                fsLayout.output_mask = 1u;
                fsLayout.push_constant_size = 32;
                fsLayout.sets[0].sampled_image_mask = (1u << samplers) - 1u;
                fsLayout.sets[0].fp_mask = (1u << samplers) - 1u;
                for (uint32_t b = 0; b < samplers; ++b)
                    fsLayout.sets[0].meta[b].array_size = 1;
                return device.request_shader(fs, fsSize, &fsLayout);
            };
            Vulkan::Shader *bright = fragment(maximo_post_bright_frag, sizeof(maximo_post_bright_frag), 1);
            Vulkan::Shader *blur = fragment(maximo_post_blur_frag, sizeof(maximo_post_blur_frag), 1);
            Vulkan::Shader *composite = fragment(maximo_post_composite_frag, sizeof(maximo_post_composite_frag), 2);
            if (vertex && bright && blur && composite)
            {
                postBright = device.request_program(vertex, bright);
                postBlur = device.request_program(vertex, blur);
                postComposite = device.request_program(vertex, composite);
            }
            if (!postBright || !postBlur || !postComposite)
            {
                std::fprintf(stderr, "[gpu] post effects setup failed; presenting without them\n");
                postFailed = true;
                return src;
            }
            std::fprintf(stderr, "[gpu] post effects ready (bloom + grade)\n");
        }
        const uint32_t qw = std::max(1u, width / 4), qh = std::max(1u, height / 4);
        auto makeTarget = [&](uint32_t w, uint32_t h, bool transferSrc) {
            auto info = Vulkan::ImageCreateInfo::immutable_2d_image(w, h, VK_FORMAT_R8G8B8A8_UNORM);
            info.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
            info.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                         (transferSrc ? VK_IMAGE_USAGE_TRANSFER_SRC_BIT : 0);
            return device.create_image(info);
        };
        Vulkan::ImageHandle bloomA = makeTarget(qw, qh, false), bloomB = makeTarget(qw, qh, false);
        Vulkan::ImageHandle result = makeTarget(width, height, true);
        if (!bloomA || !bloomB || !result)
            return src;
        // SOFT and STRONG: bloom threshold, bloom strength, saturation, curve, vignette.
        const bool strong = level >= 2;
        const float threshold = strong ? 0.45f : 0.6f;
        const float compParams[4] = {strong ? 0.9f : 0.5f, strong ? 1.12f : 1.05f, strong ? 0.25f : 0.12f,
                                     strong ? 0.3f : 0.15f};
        auto cmd = device.request_command_buffer();
        cmd->image_barrier(*src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                           VK_PIPELINE_STAGE_2_COPY_BIT, 0,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
        auto pass = [&](const Vulkan::Image &target, const float texel[2], const float params[4], auto &&body) {
            cmd->image_barrier(target, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, 0, 0,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT);
            Vulkan::RenderPassInfo rp;
            rp.num_color_attachments = 1;
            rp.color_attachments[0] = &target.get_view();
            rp.store_attachments = 1u << 0;
            cmd->begin_render_pass(rp);
            cmd->set_opaque_state();
            body();
            const float push[8] = {texel[0], texel[1], 0.0f, 0.0f, params[0], params[1], params[2], params[3]};
            cmd->push_constants(push, 0, sizeof(push));
            drawFullscreenTriangle(*cmd);
            cmd->end_render_pass();
            cmd->image_barrier(target, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
        };
        const float srcTexel[2] = {1.0f / float(width), 1.0f / float(height)};
        const float qTexel[2] = {1.0f / float(qw), 1.0f / float(qh)};
        const float brightParams[4] = {threshold, 0.0f, 0.0f, 0.0f};
        const float acrossParams[4] = {1.0f, 0.0f, 0.0f, 0.0f}, downParams[4] = {0.0f, 1.0f, 0.0f, 0.0f};
        pass(*bloomA, srcTexel, brightParams, [&] {
            cmd->set_program(postBright);
            cmd->set_texture(0, 0, src->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        pass(*bloomB, qTexel, acrossParams, [&] {
            cmd->set_program(postBlur);
            cmd->set_texture(0, 0, bloomA->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        pass(*bloomA, qTexel, downParams, [&] {
            cmd->set_program(postBlur);
            cmd->set_texture(0, 0, bloomB->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        pass(*result, srcTexel, compParams, [&] {
            cmd->set_program(postComposite);
            cmd->set_texture(0, 0, src->get_view(), Vulkan::StockSampler::LinearClamp);
            cmd->set_texture(0, 1, bloomA->get_view(), Vulkan::StockSampler::LinearClamp);
        });
        cmd->image_barrier(*src, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                           VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
        cmd->image_barrier(*result, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                           VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
        device.submit(cmd);
        return result;
    }

    // MAXIMO_FSR: scales the scanout (TRANSFER_SRC layout) to the window's picture size and returns the result in
    // TRANSFER_SRC layout, or the scanout itself when nothing needs scaling up.
    Vulkan::ImageHandle applyFsr(uint64_t guestFrame, const Vulkan::ImageHandle &src)
    {
        uint32_t targetWidth = 0, targetHeight = 0;
        if (!ps2_display_aspect::presentTargetSize(targetWidth, targetHeight))
            return src;
        const uint32_t srcWidth = src->get_width(), srcHeight = src->get_height();
        // An axis the window shows smaller than the scanout stays at scanout size; the window's filter shrinks it.
        const uint32_t outWidth = std::min(std::max(targetWidth, srcWidth), 8192u);
        const uint32_t outHeight = std::min(std::max(targetHeight, srcHeight), 8192u);
        if (outWidth == srcWidth && outHeight == srcHeight)
            return src;
        if (!fsrUpscale)
        {
            Vulkan::ResourceLayout layout;
            ParallelGS::Shaders<> suite(device, layout, 0);
            fsrUpscale = device.request_program(suite.upscale_vert, suite.upscale_frag);
            fsrSharpen = device.request_program(suite.sharpen_vert, suite.sharpen_frag);
        }
        if (fsrLoggedIn[0] != srcWidth || fsrLoggedIn[1] != srcHeight || fsrLoggedOut[0] != outWidth ||
            fsrLoggedOut[1] != outHeight)
        {
            fsrLoggedIn[0] = srcWidth;
            fsrLoggedIn[1] = srcHeight;
            fsrLoggedOut[0] = outWidth;
            fsrLoggedOut[1] = outHeight;
            std::fprintf(stderr, "[gpu] frame=%llu FSR %ux%u -> %ux%u (window picture %ux%u)\n",
                         static_cast<unsigned long long>(guestFrame), srcWidth, srcHeight, outWidth, outHeight,
                         targetWidth, targetHeight);
        }

        auto imageInfo = Vulkan::ImageCreateInfo::immutable_2d_image(outWidth, outHeight, VK_FORMAT_R8G8B8A8_UNORM);
        imageInfo.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
        imageInfo.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                          VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
        const bool sharpen = fsrSharpness >= 0.0f;
        Vulkan::ImageHandle scaled = device.create_image(imageInfo);
        Vulkan::ImageHandle sharp = sharpen ? device.create_image(imageInfo) : Vulkan::ImageHandle{};
        if (!scaled || (sharpen && !sharp))
            return src;

        auto cmd = device.request_command_buffer();
        cmd->image_barrier(*src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                           VK_PIPELINE_STAGE_2_COPY_BIT, 0,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);

        auto pass = [&](const Vulkan::Image &target, auto &&body) {
            cmd->image_barrier(target, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, 0, 0,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT);
            Vulkan::RenderPassInfo rp;
            rp.num_color_attachments = 1;
            rp.color_attachments[0] = &target.get_view();
            rp.store_attachments = 1u << 0;
            cmd->begin_render_pass(rp);
            cmd->set_opaque_state();
            body();
            drawFullscreenTriangle(*cmd);
            cmd->end_render_pass();
        };
        const float outResolution[2] = {float(outWidth), float(outHeight)};

        // EASU: edge-adaptive upscale (gathers from the source, so nearest sampling).
        pass(*scaled, [&] {
            cmd->set_program(fsrUpscale);
            cmd->set_texture(0, 0, src->get_view(), Vulkan::StockSampler::NearestClamp);
            auto *con = cmd->allocate_typed_constant_data<float>(1, 0, 16);
            fsrEasuConstants(con, float(srcWidth), float(srcHeight), float(outWidth), float(outHeight));
            cmd->push_constants(outResolution, 0, sizeof(outResolution));
        });
        const Vulkan::Image *result = scaled.get();
        if (sharpen)
        {
            cmd->image_barrier(*scaled, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
            // RCAS: contrast-adaptive sharpening at output size.
            pass(*sharp, [&] {
                cmd->set_program(fsrSharpen);
                cmd->set_texture(0, 0, scaled->get_view(), Vulkan::StockSampler::NearestClamp);
                struct RcasConstants
                {
                    float params[4];
                    int32_t range[4];
                };
                auto *con = cmd->allocate_typed_constant_data<RcasConstants>(1, 0, 1);
                fsrRcasConstants(con->params, fsrSharpness);
                con->range[0] = 0;
                con->range[1] = 0;
                con->range[2] = int32_t(outWidth) - 1;
                con->range[3] = int32_t(outHeight) - 1;
                cmd->push_constants(outResolution, 0, sizeof(outResolution));
            });
            result = sharp.get();
        }
        cmd->image_barrier(*result, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                           VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
        device.submit(cmd);
        return sharpen ? sharp : scaled;
    }

    bool onGuestFrame(uint64_t guestFrame, bool capture, uint64_t csr)
    {
        if (!ready)
        {
            // Before the first packet there is nothing to show; after a failed initialisation GPU mode cannot continue.
            if (presenting)
                return !attempted;
            return !capture;
        }
        const GSRegisters &regs = runtime.memory().gs();
        logVsync(guestFrame, regs, csr);
        PrivSnapshot priv = snapshotPrivileged(regs);
        priv.csr = csr;
        // GPU mode: the CPU GS has no raster backend, so any GS input the device did not receive is fatal.
        if (presenting && !streamComplete())
        {
            reportCompleteness(guestFrame);
            std::fprintf(stderr, "[gpu] frame=%llu: GS input bypassed the GPU device; stopping\n",
                         static_cast<unsigned long long>(guestFrame));
            return false;
        }
        if (useWorker)
        {
            if (workerFailed.load())
                return false;
            if (!capture)
            {
                WorkItem vsync;
                vsync.vsync = true;
                vsync.guestFrame = guestFrame;
                vsync.priv = priv;
                handOver(&vsync);
                waitForWorker(false);
                return true;
            }
            handOver(nullptr);
            waitForWorker(true);
        }
        return processVsync(guestFrame, capture, priv);
    }

    // The device side of a guest vsync: on the GS worker, or on the game thread.
    bool processVsync(uint64_t guestFrame, bool capture, const PrivSnapshot &priv)
    {
        if (hdtex)
            hdtex->endFrame(guestFrame);
        // MAXIMO_GS_DRAW_LOG=<guest frame>: log that frame's render passes and draw states (stderr, [drawlog]).
        static const long long drawLogFrame = [] {
            const char *v = std::getenv("MAXIMO_GS_DRAW_LOG");
            return v ? std::atoll(v) : -1ll;
        }();
        if (drawLogFrame >= 0)
            pgs_maximo_draw_log.store(guestFrame == uint64_t(drawLogFrame), std::memory_order_relaxed);
        // Diagnostics: MAXIMO_SOFT_PARTICLES_FROM=<guest frame>: soft particles off before that frame, on from it
        // (A/B comparison of consecutive frames of the same scene).
        static const long long softFrom = [] {
            const char *v = std::getenv("MAXIMO_SOFT_PARTICLES_FROM");
            return v ? std::atoll(v) : -1ll;
        }();
        if (softFrom >= 0)
            gs.set_soft_particles(guestFrame >= uint64_t(softFrom));
        copyPrivileged(gs.get_priv_register_state(), priv);
        ParallelGS::VSyncInfo info = {};
        info.phase = static_cast<uint32_t>((priv.csr >> 13) & 1u);
        info.dst_layout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
        info.dst_stage = VK_PIPELINE_STAGE_2_COPY_BIT;
        info.dst_access = VK_ACCESS_2_TRANSFER_READ_BIT;
        // The CPU presenter places the framebuffer at the window origin; DISPLAY.DX/DY only move the analogue picture.
        info.crtc_offsets = false;
        info.adapt_to_internal_horizontal_resolution = true;
        if (hiresScanout)
        {
            // paraLLEl-GS only scans out at high resolution from a progressive picture; anti_blur undoes field blending.
            info.high_resolution_scanout = true;
            info.ultra_resolution_scanout = ultraScanout;
            info.force_progressive = true;
            info.anti_blur = true;
        }
        const auto t0 = std::chrono::steady_clock::now();
        gs.flush();
        auto scanout = gs.vsync(info);
        ++vsyncs;
        if (vsyncs == 3600) // (a minute in: most pipelines exist by then)
            savePipelineCache();
        if (smaa && presenting && !capture && !syncReadback && scanout.image)
            scanout.image = applySmaa(guestFrame, scanout.image);
        // Scene lighting from the scene depth the Maximo ubershader records (the game's Z buffer is nearly empty).
        if (presenting && !capture && !syncReadback && scanout.image && GpuShadow::lightLevel() > 0)
            scanout.image = applyLighting(guestFrame, scanout, GpuShadow::lightLevel());
        if (fsr && presenting && !capture && !syncReadback && scanout.image)
            scanout.image = applyFsr(guestFrame, scanout.image);
        bool ok = true;
        if (presenting)
        {
            const auto t1 = std::chrono::steady_clock::now();
            uint32_t width = 0, height = 0;
            bool publish = true;
            if (!scanout.image)
            {
                dropPendingReadbacks();
                healthNoScanout.fetch_add(1, std::memory_order_relaxed);
                // No display circuit enabled (e.g. PMODE = 0 during boot): the TV shows black.
                static bool logged = false;
                if (!logged)
                {
                    logged = true;
                    std::fprintf(stderr, "[gpu] frame=%llu: no scanout (display circuits off); presenting black\n",
                                 static_cast<unsigned long long>(guestFrame));
                }
                width = 640;
                height = 512;
                framePixels.assign(size_t(width) * height * 4, 0);
                for (size_t i = 3; i < framePixels.size(); i += 4)
                    framePixels[i] = 0xFF;
            }
            else if (!capture && !syncReadback)
            {
                // Pipelined: submit this frame's copy, show the previous one.
                PendingReadback &submit = pendingReadback[nextReadback];
                PendingReadback &show = pendingReadback[nextReadback ^ 1u];
                nextReadback ^= 1u;
                ok = startReadback(guestFrame, scanout, submit);
                if (ok && show.valid)
                    finishReadback(show, framePixels, width, height); // (already opaque: copyOpaque)
                publish = width != 0; // nothing to show on the first frame
            }
            else
            {
                // Exact frame (capture): drop any pipelined copy, so frames are never shown out of order.
                dropPendingReadbacks();
                if ((ok = readScanout(guestFrame, scanout, framePixels, width, height)))
                {
                    // The CRTC outputs RGB only; the window blends with alpha, so present the picture opaque.
                    for (size_t i = 3; i < framePixels.size(); i += 4)
                        framePixels[i] = 0xFF;
                }
            }
            const auto t2 = std::chrono::steady_clock::now();
            if (ok && publish)
            {
                std::lock_guard<std::mutex> lock(frameMutex);
                if (frameQueue.size() >= kQueueMax) // the window stopped taking frames (a stall): keep the newest
                {
                    spareBuffers.push_back(std::move(frameQueue.front().pixels));
                    frameQueue.pop_front();
                    queueDropped.fetch_add(1, std::memory_order_relaxed);
                }
                frameQueue.emplace_back();
                QueuedFrame &queued = frameQueue.back();
                queued.pixels.swap(framePixels);
                queued.width = width;
                queued.height = height;
                queued.guestFrame = guestFrame;
                if (!spareBuffers.empty()) // the next frame is read back into a recycled buffer
                {
                    framePixels.swap(spareBuffers.back());
                    spareBuffers.pop_back();
                }
                std::vector<uint8_t> &presentPixels = queued.pixels;
                ++presentGeneration;
                ps2_pacing_trace::note(ps2_pacing_trace::kPublish, static_cast<uint32_t>(guestFrame), presentGeneration);
                noteBrightness(presentPixels, width, height);
                if (frameHashes)
                    std::fprintf(frameHashes, "%llu %016llx\n", static_cast<unsigned long long>(guestFrame),
                                 static_cast<unsigned long long>(hashPixels(presentPixels)));
                if (capture)
                    ok = saveScanout(guestFrame, presentPixels, width, height);
                // MAXIMO_GPU_DUMP=<first>,<count>: save the presented frames (pipelined, so one guest frame behind)
                // without stopping the run, e.g. to check consecutive frames for field bobbing.
                static const std::pair<uint64_t, uint64_t> dump = [] {
                    const char *v = std::getenv("MAXIMO_GPU_DUMP");
                    unsigned long long first = 0, count = 0;
                    if (v && std::sscanf(v, "%llu,%llu", &first, &count) == 2)
                        return std::make_pair(uint64_t(first), uint64_t(first + count));
                    return std::make_pair(uint64_t(0), uint64_t(0));
                }();
                if (guestFrame >= dump.first && guestFrame < dump.second)
                {
                    char filename[80];
                    std::snprintf(filename, sizeof(filename), "present_%05llu_%ux%u.rgba",
                                  static_cast<unsigned long long>(guestFrame), width, height);
                    writeFile(filename, presentPixels.data(), presentPixels.size());
                }
            }
            submitNs += std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
            readbackNs += std::chrono::duration_cast<std::chrono::nanoseconds>(t2 - t1).count();
            if (++timedFrames == 120)
            {
                std::fprintf(stderr, "[gpu] frames %llu-%llu: flush+vsync %.2f ms/frame, scanout copy+wait %.2f ms/frame\n",
                             static_cast<unsigned long long>(guestFrame - 119),
                             static_cast<unsigned long long>(guestFrame), submitNs / 120e6, readbackNs / 120e6);
                timedFrames = 0;
                submitNs = readbackNs = 0;
            }
        }
        else if (capture)
        {
            ok = reportCompleteness(guestFrame);
            ok = compareVram(guestFrame) && ok;
            std::vector<uint8_t> pixels;
            uint32_t width = 0, height = 0;
            ok = readScanout(guestFrame, scanout, pixels, width, height) &&
                 saveScanout(guestFrame, pixels, width, height) && ok;
        }
        device.next_frame_context();
        if (!ok)
            healthFailed.fetch_add(1, std::memory_order_relaxed);
        return ok;
    }

    bool streamComplete() const
    {
        const BypassCounts now = BypassCounts::read(runtime.gs().streamStats());
        return now.packets - atInit.packets == packets && now.directRegWrites == atInit.directRegWrites &&
               now.nativePacked == atInit.nativePacked && now.nativeUploads == atInit.nativeUploads &&
               now.localToHost == atInit.localToHost;
    }

    bool takeFrame(std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height, uint64_t *guestFrame = nullptr)
    {
        std::lock_guard<std::mutex> lock(frameMutex);
        if (frameQueue.empty())
            return false;
        // More than one frame waiting at ten presents in a row (the window shows fewer frames than the game makes, e.g.
        // 120 Hz gameplay on a 60 Hz screen with VSync): the queue only adds latency, so skip one.
        if (frameQueue.size() >= 2)
        {
            if (++backlogTakes >= 10)
            {
                backlogTakes = 0;
                spareBuffers.push_back(std::move(frameQueue.front().pixels));
                frameQueue.pop_front();
                queueTrimmed.fetch_add(1, std::memory_order_relaxed);
            }
        }
        else
            backlogTakes = 0;
        QueuedFrame &front = frameQueue.front();
        if (front.pixels.empty())
        {
            frameQueue.pop_front();
            return false;
        }
        if (guestFrame)
            *guestFrame = front.guestFrame;
        ++takenGeneration;
        healthTaken.fetch_add(1, std::memory_order_relaxed);
        ps2_pacing_trace::note(ps2_pacing_trace::kTake, 0, takenGeneration);
        // The buffers change hands instead of the picture being copied (10 MB a frame at 2560x1022): the window
        // keeps this frame, and its old buffer comes back to be filled by a later one.
        pixels.swap(front.pixels);
        width = front.width;
        height = front.height;
        if (!front.pixels.empty())
            spareBuffers.push_back(std::move(front.pixels));
        frameQueue.pop_front();
        return true;
    }
};

GpuShadow::GpuShadow(PS2Runtime &runtime) : impl(std::make_unique<Impl>(runtime)) {}
GpuShadow::~GpuShadow() = default;

void GpuShadow::submitPacket(GifPathId path, const uint8_t *data, uint32_t size)
{
    if (impl->initialize())
    {
        impl->logPacket(static_cast<uint32_t>(path), data, size);
        if (impl->useWorker)
            impl->queuePacket(static_cast<uint32_t>(path), data, size);
        else
            impl->gs.gif_transfer(static_cast<uint32_t>(path), data, size);
        ++impl->packets;
    }
}

bool GpuShadow::onGuestFrame(uint64_t guestFrame, bool capture)
{
    return impl->onGuestFrame(guestFrame, capture, impl->runtime.memory().gs().csr.load(std::memory_order_relaxed));
}
bool GpuShadow::onGuestFrame(uint64_t guestFrame, bool capture, uint64_t csr)
{
    return impl->onGuestFrame(guestFrame, capture, csr);
}
void GpuShadow::enablePresentation() { impl->presenting = true; }
bool GpuShadow::takeFrame(std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height)
{
    return impl->takeFrame(pixels, width, height);
}
bool GpuShadow::takeFrame(std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height, uint64_t &guestFrame)
{
    return impl->takeFrame(pixels, width, height, &guestFrame);
}
uint64_t GpuShadow::packetCount() const { return impl->packets; }

GpuShadow::Health GpuShadow::takeHealth()
{
    Health h;
    h.published = impl->healthPublished.exchange(0, std::memory_order_relaxed);
    h.taken = impl->healthTaken.exchange(0, std::memory_order_relaxed);
    h.black = impl->healthBlack.exchange(0, std::memory_order_relaxed);
    h.noScanout = impl->healthNoScanout.exchange(0, std::memory_order_relaxed);
    h.failed = impl->healthFailed.exchange(0, std::memory_order_relaxed);
    h.width = impl->lastWidth.load(std::memory_order_relaxed);
    h.height = impl->lastHeight.load(std::memory_order_relaxed);
    h.brightnessMean = impl->lastBrightnessMean.load(std::memory_order_relaxed);
    h.brightnessMax = impl->lastBrightnessMax.load(std::memory_order_relaxed);
    return h;
}
