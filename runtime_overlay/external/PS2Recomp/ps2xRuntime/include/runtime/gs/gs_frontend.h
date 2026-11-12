#ifndef PS2_GS_FRONTEND_H
#define PS2_GS_FRONTEND_H

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

#include "runtime/gs/gs_backend.h"

struct GSDebugSnapshot
{
    GSContext ctx[2]{};
    GSPrimReg prim{};
    GSTexaReg texa{};
    GSTexClutReg texclut{};
    uint64_t scanmsk = 0;
    uint64_t dimx = 0;
    uint64_t dthe = 0;
    uint64_t colclamp = 0;
    GSBitBltBuf bitbltbuf{};
    GSTrxPos trxpos{};
    GSTrxReg trxreg{};
    uint32_t trxdir = 0;
    uint32_t transferX = 0;
    uint32_t transferY = 0;
    uint32_t transferTotalPixels = 0;
    uint32_t transferCopiedPixels = 0;
    uint32_t lastDisplayBaseBytes = 0;
    GSFrameReg preferredDisplaySourceFrame{};
    uint32_t preferredDisplayDestFbp = 0;
    bool hasPreferredDisplaySource = false;
    uint32_t hostPresentationWidth = 0;
    uint32_t hostPresentationHeight = 0;
    uint32_t hostPresentationDisplayFbp = 0;
    uint32_t hostPresentationSourceFbp = 0;
    bool hostPresentationUsedPreferred = false;
    bool hasHostPresentationFrame = false;
    size_t localToHostPendingBytes = 0;
};

enum class GSDebugEventKind : uint8_t
{
    GifTag = 0,
    Register = 1,
    Draw = 2,
    Transfer = 3,
    Present = 4,
};

struct GSDebugHistoryEntry
{
    uint64_t seq = 0;
    uint64_t vsyncTick = 0;
    uint32_t frameIndex = 0;
    GSDebugEventKind kind = GSDebugEventKind::Register;

    uint8_t reg = 0;
    uint64_t regValue = 0;

    uint32_t gifSizeBytes = 0;
    uint32_t gifNloop = 0;
    uint8_t gifFlg = 0;
    uint8_t gifNreg = 0;

    GSPrimReg prim{};
    GSFrameReg frame{};
    GSZbufReg zbuf{};
    GSTex0Reg tex0{};
    GSScissorReg scissor{};
    uint64_t test = 0;
    uint64_t alpha = 0;

    uint32_t vertexCount = 0;
    float xMin = 0.0f;
    float xMax = 0.0f;
    float yMin = 0.0f;
    float yMax = 0.0f;
    double zMin = 0.0;
    double zMax = 0.0;
    uint8_t aMin = 0;
    uint8_t aMax = 0;

    GSBitBltBuf bitbltbuf{};
    GSTrxPos trxpos{};
    GSTrxReg trxreg{};
    uint32_t trxdir = 0;
    uint32_t transferPixels = 0;

    uint32_t displayFbp = 0;
    uint32_t sourceFbp = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    bool usedPreferred = false;
};

// Counts of GS stream events that matter for running the GS asynchronously: each one is a point where
// the producer (game thread) and the GS must agree on ordering or completion. Relaxed atomics; cheap.
struct GSStreamStats
{
    std::atomic<uint64_t> packets{0};       // GS::processGIFPacket calls (arbiter output and XGKICK without memory)
    std::atomic<uint64_t> packetBytes{0};
    std::atomic<uint64_t> directRegWrites{0}; // GS::writeRegister from outside the GIF stream (HLE stubs)
    std::atomic<uint64_t> signal{0};
    std::atomic<uint64_t> finish{0};
    std::atomic<uint64_t> label{0};
    std::atomic<uint64_t> trxdir[4]{};       // transfers started: 0 host->local, 1 local->host, 2 local->local, 3 off
    std::atomic<uint64_t> localToHostReads{0}; // consumeLocalToHostBytes calls
    std::atomic<uint64_t> presentationLatches{0};
    std::atomic<uint64_t> nativePackedPackets{0}; // processNativePackedGIFPacket shortcuts (bypass processGIFPacket)
    std::atomic<uint64_t> nativeImageUploads{0};  // uploadImageNative shortcuts (bypass processGIFPacket)
};

// The privileged display registers that presentation reads, as one snapshot. With an asynchronous GS the worker
// presents from the snapshot at its own position in the stream instead of from the live registers.
struct GSDisplayRegs
{
    uint64_t pmode = 0, smode2 = 0, dispfb1 = 0, display1 = 0, dispfb2 = 0, display2 = 0, bgcolor = 0;
};

class GSAsyncQueue;

class GS
{
public:
    GS();
    ~GS() = default;

    void init(uint8_t *vram, uint32_t vramSize, struct GSRegisters *privRegs = nullptr);
    void reset();
    void setRasterBackend(std::unique_ptr<GSRasterBackend> backend);
    // The host draws with a GS of its own (fed the same packets) and its raster backend throws draws away: then
    // no draw batch is built for it at all (it was most of the cost of each vertex kick).
    void setDiscardDraws(bool discard) { m_discardDraws = discard; }

    void processGIFPacket(const uint8_t *data, uint32_t sizeBytes);
    bool processNativePackedGIFPacket(const uint8_t *data, uint32_t sizeBytes);
    void uploadImageNative(uint64_t bitbltbuf,
                           uint64_t trxpos,
                           uint64_t trxreg,
                           uint64_t trxdir,
                           const uint8_t *data,
                           uint32_t sizeBytes);
    void writeRegister(uint8_t regAddr, uint64_t value);

    const uint8_t *lockDisplaySnapshot(uint32_t &outSize);
    void unlockDisplaySnapshot();
    uint32_t getLastDisplayBaseBytes() const;
    const GSFrameReg &getContextFrame(int index) const
    {
        return m_ctx[(index != 0) ? 1 : 0].frame;
    }
    GSDebugSnapshot getDebugSnapshot() const;
    std::vector<GSDebugHistoryEntry> getDebugHistory() const;
    void clearDebugHistory();
    bool isDebugHistoryPaused() const;
    void setDebugHistoryPaused(bool paused);
    bool getPreferredDisplaySource(GSFrameReg &outSource, uint32_t &outDestFbp) const;
    void latchHostPresentationFrame();
    bool copyLatchedHostPresentationFrame(std::vector<uint8_t> &outPixels,
                                          uint32_t &outWidth,
                                          uint32_t &outHeight,
                                          uint32_t *outDisplayFbp = nullptr,
                                          uint32_t *outSourceFbp = nullptr,
                                          bool *outUsedPreferred = nullptr) const;
    bool clearFramebufferContext(uint32_t contextIndex, uint32_t rgba);
    bool clearActiveFramebuffer(uint32_t rgba);
    uint64_t nativeImageUploadCount() const { return m_nativeImageUploadCount; }
    uint64_t nativePackedGIFPacketCount() const { return m_nativePackedGIFPacketCount; }
    const GSStreamStats &streamStats() const { return m_streamStats; }

    // Asynchronous GS support (GSAsyncQueue). setAsyncQueue(nullptr) returns to inline operation.
    void setAsyncQueue(GSAsyncQueue *queue);
    // Called by the GS worker when it reaches a display-register snapshot in the stream.
    void setQueuedDisplayRegs(const GSDisplayRegs &regs);
    // Blocks until queued GS work is processed (no-op when the GS runs inline or on the worker itself).
    void waitForQueuedWork();
    bool asyncEnabled() const { return m_asyncQueue != nullptr; }
    // Incremented every time a presentation frame is latched.
    uint64_t hostPresentationGeneration() const { return m_presentationGeneration.load(std::memory_order_acquire); }

    uint32_t consumeLocalToHostBytes(uint8_t *dst, uint32_t maxBytes);

    void refreshDisplaySnapshot();

    void WriteVram(uint32_t psm, uint32_t base, uint32_t bw, uint32_t x, uint32_t y, uint32_t value);
    uint32_t ReadVram(uint32_t psm, uint32_t base, uint32_t bw, uint32_t x, uint32_t y) const;

private:
    void snapshotVRAM();
    void writeRegisterUnlocked(uint8_t regAddr, uint64_t value);
    void writeRegisterPacked(uint8_t regDesc, uint64_t lo, uint64_t hi);
    void uploadImageNativeUnlocked(uint64_t bitbltbuf,
                                   uint64_t trxpos,
                                   uint64_t trxreg,
                                   uint64_t trxdir,
                                   const uint8_t *data,
                                   uint32_t sizeBytes);
    void vertexKick(bool drawing);

    void recordDebugEventUnlocked(GSDebugHistoryEntry entry);
    GSDebugHistoryEntry makeDebugEventUnlocked(GSDebugEventKind kind) const;
    void recordGifTagDebugEventUnlocked(uint32_t sizeBytes, uint32_t nloop, uint8_t flg, uint32_t nreg);
    void recordRegisterDebugEventUnlocked(uint8_t regAddr, uint64_t value);
    void recordDrawDebugEventUnlocked(int vertexCount);
    void recordTransferDebugEventUnlocked();
    void recordPresentDebugEventUnlocked(uint32_t displayFbp, uint32_t sourceFbp, uint32_t width, uint32_t height, bool usedPreferred);

    void processImageData(const uint8_t *data, uint32_t sizeBytes);
    bool tryProcessNativeImageUploadPacket(const uint8_t *data, uint32_t sizeBytes);
    GSPrimitiveBatch buildDrawBatch(int vertexCount) const;
    void updatePreferredDisplaySourceForDraw(const GSPrimitiveBatch &batch);
    GSPresentationRequest buildPresentationRequestUnlocked() const;


    GSContext &activeContext();

    uint8_t *m_localMemoryStorage = nullptr;
    uint32_t m_localMemorySize = 0u;
    struct GSRegisters *m_privRegs = nullptr;
    mutable std::recursive_mutex m_stateMutex;
    mutable std::mutex m_backendLifetimeMutex;
    mutable std::mutex m_presentationMutex;

    GSContext m_ctx[2];
    GSPrimReg m_prim{};
    GSPrimReg m_primRegister{};
    GSPrimReg m_prmodeRegister{};

    uint8_t m_curR = 0x80, m_curG = 0x80, m_curB = 0x80, m_curA = 0x80;
    float m_curQ = 1.0f;
    float m_curS = 0.0f, m_curT = 0.0f;
    uint16_t m_curU = 0, m_curV = 0;
    uint8_t m_curFog = 0;
    uint8_t m_fogR = 0, m_fogG = 0, m_fogB = 0;

    bool m_prmodecont = true;
    bool m_pabe = false;
    uint64_t m_scanmsk = 0;
    uint64_t m_dimx = 0;
    uint64_t m_dthe = 0;
    uint64_t m_colclamp = 0;
    GSTexaReg m_texa{0u, false, 0u};
    GSTexClutReg m_texclut{0u, 0u, 0u};

    GSBitBltBuf m_bitbltbuf{};
    GSTrxPos m_trxpos{};
    GSTrxReg m_trxreg{};
    uint32_t m_trxdir = 3;


    static constexpr int kMaxVerts = 6;
    GSVertex m_vtxQueue[kMaxVerts];
    int m_vtxCount = 0;
    int m_vtxIndex = 0;

    std::vector<uint8_t> m_displaySnapshot;
    std::mutex m_snapshotMutex;
    uint32_t m_lastDisplayBaseBytes = 0;
    GSFrameReg m_preferredDisplaySourceFrame{};
    uint32_t m_preferredDisplayDestFbp = 0;
    bool m_hasPreferredDisplaySource = false;
    std::vector<uint8_t> m_hostPresentationFrame;
    uint32_t m_hostPresentationWidth = 0;
    uint32_t m_hostPresentationHeight = 0;
    uint32_t m_hostPresentationDisplayFbp = 0;
    uint32_t m_hostPresentationSourceFbp = 0;
    bool m_hostPresentationUsedPreferred = false;
    bool m_hasHostPresentationFrame = false;
    uint64_t m_nativeImageUploadCount = 0;
    uint64_t m_nativePackedGIFPacketCount = 0;
    GSStreamStats m_streamStats;
    GSAsyncQueue *m_asyncQueue = nullptr;
    GSDisplayRegs m_queuedDisplayRegs{};
    bool m_useQueuedDisplayRegs = false;
    std::atomic<uint64_t> m_presentationGeneration{0};
    // Entry points the game thread may call outside the GIF stream: with a GS worker they wait for it first.
    void syncWithQueuedWork(const char *what);

    static constexpr size_t kDebugHistoryCapacity = 512;
    std::array<GSDebugHistoryEntry, kDebugHistoryCapacity> m_debugHistory{};
    size_t m_debugHistoryWrite = 0;
    size_t m_debugHistoryCount = 0;
    uint64_t m_debugNextSeq = 1;
    uint32_t m_debugFrameIndex = 0;
    uint64_t m_debugLastVsyncTick = UINT64_MAX;
    bool m_debugHistoryPaused = true;

    std::unique_ptr<GSRasterBackend> m_backend;
    bool m_discardDraws = false;
};

#endif
