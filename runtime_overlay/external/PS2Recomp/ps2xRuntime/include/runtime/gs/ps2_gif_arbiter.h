#ifndef PS2_GIF_ARBITER_H
#define PS2_GIF_ARBITER_H

#include <cstdint>
#include <functional>
#include <vector>

enum class GifPathId : uint8_t
{
    Path1 = 1,
    Path2 = 2,
    Path3 = 3,
};

struct GifArbiterPacket
{
    GifPathId pathId;
    bool path2DirectHl = false;
    bool path3Image = false;
    bool syntheticImageTag = false;
    std::vector<uint8_t> data;
};

class GifArbiter
{
public:
    using ProcessPacketFn = std::function<void(const uint8_t *, uint32_t)>;

    GifArbiter() = default;
    explicit GifArbiter(ProcessPacketFn processFn);

    void setProcessPacketFn(ProcessPacketFn fn) { m_processFn = std::move(fn); }
    // When set, drain() hands each packet's bytes over instead of calling the process function (used to queue
    // packets for a GS worker without copying them again). Arbitration order is unchanged.
    using TakePacketFn = std::function<void(std::vector<uint8_t> &&)>;
    void setTakePacketFn(TakePacketFn fn) { m_takeFn = std::move(fn); }
    // Optional device sink for renderers which retain independent GIF state per path.
    // Receives the same arbitration order as the CPU sink and preserves packet boundaries.
    // syntheticImageTag: the first qword is an IMAGE GIFtag the host added so that a continuation of an earlier
    // IMAGE transfer parses on its own (the CPU GS needs it). A GS which keeps GIF state across packets must skip it.
    using PathPacketFn = std::function<void(GifPathId, const uint8_t *, uint32_t, bool syntheticImageTag)>;
    void setPathPacketFn(PathPacketFn fn) { m_pathFn = std::move(fn); }

    void submit(GifPathId pathId, const uint8_t *data, uint32_t sizeBytes, bool path2DirectHl = false,
                bool syntheticImageTag = false);

    void drain();
    bool empty() const { return m_queue.empty(); }

private:
    ProcessPacketFn m_processFn;
    TakePacketFn m_takeFn;
    PathPacketFn m_pathFn;
    std::vector<GifArbiterPacket> m_queue;
    std::vector<std::vector<uint8_t>> m_spare; // packet buffers kept for reuse (no allocation per packet)

    static bool isImagePacket(const uint8_t *data, uint32_t sizeBytes);
    static uint8_t pathPriority(GifPathId id);
};

#endif
