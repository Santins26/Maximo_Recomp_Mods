#include "runtime/gs/ps2_gif_arbiter.h"
#include "runtime/ps2_vu1_overlap_model.h"
#include <algorithm>
#include <cstring>

GifArbiter::GifArbiter(ProcessPacketFn processFn)
    : m_processFn(std::move(processFn))
{
}

bool GifArbiter::isImagePacket(const uint8_t *data, uint32_t sizeBytes)
{
    if (!data || sizeBytes < 16u)
        return false;

    uint64_t tagLo = 0;
    std::memcpy(&tagLo, data, sizeof(tagLo));
    const uint8_t flg = static_cast<uint8_t>((tagLo >> 58) & 0x3u);
    // FLG=3 ("disabled" in the GS manual) behaves exactly like IMAGE: NLOOP qwords of image data.
    return flg == 2u || flg == 3u;
}

void GifArbiter::submit(GifPathId pathId, const uint8_t *data, uint32_t sizeBytes, bool path2DirectHl,
                        bool syntheticImageTag)
{
    if (!data || sizeBytes < 16 || (!m_processFn && !m_pathFn))
        return;

    GifArbiterPacket pkt;
    pkt.pathId = pathId;
    pkt.path2DirectHl = (pathId == GifPathId::Path2) && path2DirectHl;
    pkt.path3Image = (pathId == GifPathId::Path3) && isImagePacket(data, sizeBytes);
    pkt.syntheticImageTag = syntheticImageTag;
    // A buffer from an earlier packet: its memory is kept, so a packet costs a copy and no allocation.
    if (!m_spare.empty())
    {
        pkt.data = std::move(m_spare.back());
        m_spare.pop_back();
    }
    pkt.data.assign(data, data + sizeBytes);
    m_queue.push_back(std::move(pkt));
}

void GifArbiter::drain()
{
    ps2_vu1_overlap_model::WorkScope overlapWork(ps2_vu1_overlap_model::kSplit);
    if (!m_processFn && !m_pathFn)
        return;

    const auto before = [](const GifArbiterPacket &a, const GifArbiterPacket &b)
                     {
                         // DIRECTHL cannot preempt PATH3 IMAGE transfers.
                         if (a.path2DirectHl != b.path2DirectHl || a.path3Image != b.path3Image)
                         {
                             if (a.path3Image && b.path2DirectHl)
                                 return true;
                             if (a.path2DirectHl && b.path3Image)
                                 return false;
                         }
                         return pathPriority(a.pathId) < pathPriority(b.pathId);
                     };
    // (Usually one packet, or packets of one path already in order: a stable sort of a sorted queue changes
    // nothing, and is skipped - it allocated a buffer every time.)
    if (m_queue.size() > 1 && !std::is_sorted(m_queue.begin(), m_queue.end(), before))
        std::stable_sort(m_queue.begin(), m_queue.end(), before);

    for (size_t i = 0; i < m_queue.size(); ++i)
    {
        auto &pkt = m_queue[i];
        if (!pkt.data.empty())
        {
            if (m_pathFn)
                m_pathFn(pkt.pathId, pkt.data.data(), static_cast<uint32_t>(pkt.data.size()), pkt.syntheticImageTag);
            else if (m_takeFn)
                m_takeFn(std::move(pkt.data));
            else
                m_processFn(pkt.data.data(), static_cast<uint32_t>(pkt.data.size()));
        }
    }
    for (auto &pkt : m_queue)
        if (pkt.data.capacity() != 0 && m_spare.size() < 64)
        {
            pkt.data.clear();
            m_spare.push_back(std::move(pkt.data));
        }
    m_queue.clear();
}

uint8_t GifArbiter::pathPriority(GifPathId id)
{
    return static_cast<uint8_t>(id);
}
