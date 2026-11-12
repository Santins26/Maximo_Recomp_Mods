// Based on Blackline Interactive implementation
#include "runtime/ps2_memory.h"
#include "runtime/ps2_vu1_overlap_model.h"
#include "runtime/ps2_vu1_thread.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>

enum VIFCmd : uint8_t
{
    VIF_NOP = 0x00,
    VIF_STCYCL = 0x01,
    VIF_OFFSET = 0x02,
    VIF_BASE = 0x03,
    VIF_ITOP = 0x04,
    VIF_STMOD = 0x05,
    VIF_MSKPATH3 = 0x06,
    VIF_MARK = 0x07,
    VIF_FLUSHE = 0x10,
    VIF_FLUSH = 0x11,
    VIF_FLUSHA = 0x13,
    VIF_MSCAL = 0x14,
    VIF_MSCALF = 0x15,
    VIF_MSCNT = 0x17,
    VIF_STMASK = 0x20,
    VIF_STROW = 0x30,
    VIF_STCOL = 0x31,
    VIF_MPG = 0x4A,
    VIF_DIRECT = 0x50,
    VIF_DIRECTHL = 0x51,
};

namespace
{
    constexpr uint8_t kGifFmtImage = 2u;

    // Opcodes the VIF1 command decoder accepts (VIF command reference); anything else is data.
    bool isVif1Opcode(uint32_t word)
    {
        const uint8_t op = static_cast<uint8_t>((word >> 24) & 0x7Fu);
        if (op <= 0x07u)
            return true; // NOP STCYCL OFFSET BASE ITOP STMOD MSKPATH3 MARK
        switch (op)
        {
        case 0x10u: case 0x11u: case 0x13u: // FLUSHE FLUSH FLUSHA
        case 0x14u: case 0x15u: case 0x17u: // MSCAL MSCALF MSCNT
        case 0x20u: case 0x30u: case 0x31u: // STMASK STROW STCOL
        case 0x4Au: case 0x50u: case 0x51u: // MPG DIRECT DIRECTHL
            return true;
        default:
            return op >= 0x60u; // UNPACK
        }
    }

    uint32_t pendingGifImageQwc(const uint8_t *data, uint32_t sizeBytes)
    {
        if (!data || sizeBytes < 16u)
            return 0u;

        uint32_t offset = 0u;
        while (offset + 16u <= sizeBytes)
        {
            uint64_t tagLo = 0u;
            std::memcpy(&tagLo, data + offset, sizeof(tagLo));
            offset += 16u;

            const uint32_t nloop = static_cast<uint32_t>(tagLo & 0x7FFFu);
            const uint8_t flg = static_cast<uint8_t>((tagLo >> 58) & 0x3u);
            uint32_t nreg = static_cast<uint32_t>((tagLo >> 60) & 0xFu);
            if (nreg == 0u)
                nreg = 16u;

            uint64_t payloadBytes = 0u;
            if (flg == 0u) // PACKED
            {
                payloadBytes = static_cast<uint64_t>(nloop) * nreg * 16ull;
            }
            else if (flg == 1u) // REGLIST, padded to a quadword
            {
                payloadBytes = static_cast<uint64_t>(nloop) * nreg * 8ull;
                payloadBytes = (payloadBytes + 15ull) & ~15ull;
            }
            else if (flg == kGifFmtImage || flg == 3u) // FLG=3 ("disabled" in the GS manual) behaves exactly like IMAGE: NLOOP qwords of image data.
            {
                payloadBytes = static_cast<uint64_t>(nloop) * 16ull;
                const uint64_t availableBytes = sizeBytes - offset;
                if (payloadBytes > availableBytes)
                {
                    return static_cast<uint32_t>((payloadBytes - availableBytes) / 16ull);
                }
            }
            else
            {
                return 0u;
            }

            if (payloadBytes > static_cast<uint64_t>(sizeBytes - offset))
                return 0u;
            offset += static_cast<uint32_t>(payloadBytes);
        }

        return 0u;
    }
}

void PS2Memory::processVIF0Data(uint32_t srcPhys, uint32_t sizeBytes)
{
    if (sizeBytes == 0u || srcPhys >= PS2_RAM_SIZE)
        return;

    const uint64_t requestedEnd = static_cast<uint64_t>(srcPhys) + static_cast<uint64_t>(sizeBytes);
    if (requestedEnd > static_cast<uint64_t>(PS2_RAM_SIZE))
        sizeBytes = PS2_RAM_SIZE - srcPhys;

    processVIF0Data(m_rdram + srcPhys, sizeBytes);
}

void PS2Memory::processVIF0Data(const uint8_t *data, uint32_t sizeBytes)
{
    if (sizeBytes == 0u)
        return;

    uint32_t pos = 0;
    while (pos + 4 <= sizeBytes)
    {
        uint32_t cmd = 0u;
        std::memcpy(&cmd, data + pos, sizeof(cmd));
        pos += 4u;

        const uint8_t opcode = static_cast<uint8_t>((cmd >> 24) & 0x7Fu);
        const uint16_t imm = static_cast<uint16_t>(cmd & 0xFFFFu);
        const uint8_t num = static_cast<uint8_t>((cmd >> 16) & 0xFFu);
        const bool irq = (cmd & 0x80000000u) != 0u;

        vif0_regs.code = cmd;
        vif0_regs.num = num;
        if (irq)
            vif0_regs.stat |= (1u << 11);

        if (opcode == VIF_NOP)
        {
            continue;
        }
        else if (opcode == VIF_STCYCL)
        {
            vif0_regs.cycle = imm;
            continue;
        }
        else if (opcode == VIF_ITOP)
        {
            vif0_regs.itops = imm & 0x3FFu;
            continue;
        }
        else if (opcode == VIF_STMOD)
        {
            vif0_regs.mode = imm & 3u;
            continue;
        }
        else if (opcode == VIF_MARK)
        {
            vif0_regs.mark = imm;
            vif0_regs.stat |= (1u << 6);
            continue;
        }
        else if (opcode == VIF_FLUSHE || opcode == VIF_FLUSH || opcode == VIF_FLUSHA)
        {
            continue;
        }
        else if (opcode == VIF_STMASK)
        {
            if (pos + 4u > sizeBytes)
                break;
            std::memcpy(&vif0_regs.mask, data + pos, sizeof(vif0_regs.mask));
            pos += 4u;
            continue;
        }
        else if (opcode == VIF_STROW)
        {
            if (pos + 16u > sizeBytes)
                break;
            std::memcpy(vif0_regs.row, data + pos, 16u);
            pos += 16u;
            continue;
        }
        else if (opcode == VIF_STCOL)
        {
            if (pos + 16u > sizeBytes)
                break;
            std::memcpy(vif0_regs.col, data + pos, 16u);
            pos += 16u;
            continue;
        }
        else if (opcode == VIF_MPG)
        {
            const uint32_t destAddr = static_cast<uint32_t>(imm & 0x1FFu) * 8u;
            const uint32_t instructionCount = (num == 0u) ? 256u : static_cast<uint32_t>(num);
            const uint32_t mpgBytes = instructionCount * 8u;
            uint32_t copyBytes = 0u;
            if (m_vu0Code && destAddr < PS2_VU0_CODE_SIZE && mpgBytes > 0u)
            {
                copyBytes = mpgBytes;
                if (destAddr + copyBytes > PS2_VU0_CODE_SIZE)
                    copyBytes = PS2_VU0_CODE_SIZE - destAddr;
                if (pos + copyBytes <= sizeBytes)
                {
                    std::memcpy(m_vu0Code + destAddr, data + pos, copyBytes);
                    markVU0CodeModified();
                }
            }

            pos += mpgBytes;
            if (pos > sizeBytes)
                break;
            continue;
        }
        else if ((opcode & 0x60u) == 0x60u)
        {
            const uint8_t vn = static_cast<uint8_t>((opcode >> 2) & 0x3u);
            const uint8_t vl = static_cast<uint8_t>(opcode & 0x3u);
            const int components = static_cast<int>(vn) + 1;
            int bitsPerComponent = 32;
            switch (vl)
            {
            case 0:
                bitsPerComponent = 32;
                break;
            case 1:
                bitsPerComponent = 16;
                break;
            case 2:
                bitsPerComponent = 8;
                break;
            case 3:
                bitsPerComponent = (vn == 3u) ? 4 : 16;
                break;
            default:
                break;
            }
            const int bitsPerVector = (vl == 3u && vn == 3u) ? 16 : (components * bitsPerComponent);
            uint32_t bytesPerVector = static_cast<uint32_t>((bitsPerVector + 7) / 8);
            const uint32_t writeVectorCount = (num == 0u) ? 256u : static_cast<uint32_t>(num);
            uint32_t cl = vif0_regs.cycle & 0xFFu;
            uint32_t wl = (vif0_regs.cycle >> 8) & 0xFFu;
            if (cl == 0u)
                cl = 1u;
            if (wl == 0u)
                wl = 1u;
            uint32_t sourceVectorCount = writeVectorCount;
            if (cl < wl)
            {
                const uint32_t fullBlocks = writeVectorCount / wl;
                uint32_t remainder = writeVectorCount % wl;
                if (remainder > cl)
                    remainder = cl;
                sourceVectorCount = fullBlocks * cl + remainder;
            }
            uint32_t totalBytes = sourceVectorCount * bytesPerVector;
            totalBytes = (totalBytes + 3u) & ~3u;

            if (m_vu0Data && pos + totalBytes <= sizeBytes && vl == 0u)
            {
                uint32_t vuAddr = static_cast<uint32_t>(imm & 0x3FFu);
                if ((imm & 0x8000u) != 0u)
                    vuAddr = (vuAddr + (vif0_regs.tops & 0x3FFu)) & 0x3FFu;
                const uint8_t *srcBase = data + pos;
                uint32_t srcIndex = 0u;
                for (uint32_t writeIndex = 0; writeIndex < writeVectorCount; ++writeIndex)
                {
                    const uint32_t cyclePos = writeIndex % wl;
                    const bool sourceAvailable = (cl >= wl) || (cyclePos < cl);
                    uint32_t destVec = (cl >= wl) ? ((vuAddr + (writeIndex / wl) * cl + cyclePos) & 0x3FFu)
                                                  : ((vuAddr + writeIndex) & 0x3FFu);
                    const uint32_t destOff = destVec * 16u;
                    if (destOff + 16u > PS2_VU0_DATA_SIZE)
                    {
                        if (sourceAvailable && srcIndex < sourceVectorCount)
                            ++srcIndex;
                        continue;
                    }
                    if (!sourceAvailable || srcIndex >= sourceVectorCount)
                        continue;
                    const uint8_t *srcVec = srcBase + srcIndex * bytesPerVector;
                    ++srcIndex;
                    uint32_t lanes[4] = {0u, 0u, 0u, 0u};
                    std::memcpy(lanes, m_vu0Data + destOff, sizeof(lanes));
                    const uint32_t limit = (components > 4) ? 4u : static_cast<uint32_t>(components);
                    for (uint32_t c = 0; c < limit; ++c)
                    {
                        uint32_t scalar = 0u;
                        std::memcpy(&scalar, srcVec + c * 4u, sizeof(scalar));
                        lanes[c] = scalar;
                    }
                    _mm_storeu_si128(reinterpret_cast<__m128i *>(m_vu0Data + destOff), _mm_loadu_si128(reinterpret_cast<const __m128i *>(lanes)));
                }
            }
            pos += totalBytes;
            if (pos > sizeBytes)
                break;
            continue;
        }
        else
        {
            break;
        }
    }
}

void PS2Memory::processVIF1Data(uint32_t srcPhys, uint32_t sizeBytes)
{
    if (sizeBytes == 0u || srcPhys >= PS2_RAM_SIZE)
        return;

    const uint64_t requestedEnd = static_cast<uint64_t>(srcPhys) + static_cast<uint64_t>(sizeBytes);
    if (requestedEnd > static_cast<uint64_t>(PS2_RAM_SIZE))
        sizeBytes = PS2_RAM_SIZE - srcPhys;

    processVIF1Data(m_rdram + srcPhys, sizeBytes);
}

namespace
{
    struct Vif1CmdRecord
    {
        uint32_t cmd, pos, size;
    };
    Vif1CmdRecord g_vif1CmdRing[8] = {};
    uint64_t g_vif1CmdRingPos = 0;

    void dumpVif1CmdRing()
    {
        for (uint64_t i = g_vif1CmdRingPos > 8 ? g_vif1CmdRingPos - 8 : 0; i < g_vif1CmdRingPos; ++i)
        {
            const Vif1CmdRecord &r = g_vif1CmdRing[i % 8];
            std::fprintf(stderr, "[vif1-irq]   cmd 0x%08X at %u/%u\n", r.cmd, r.pos, r.size);
        }
    }
}

uint32_t PS2Memory::submitPendingPath2Image(const uint8_t *data, uint32_t availableQw, uint32_t &pendingQwc,
                                            bool &pendingDirectHl, bool submit)
{
    // Wrap the next part of a PATH2 IMAGE transfer in its own IMAGE GIFtag (EOP on the last part).
    const uint32_t chunkQw = std::min<uint32_t>(pendingQwc, availableQw);
    if (chunkQw == 0u)
        return 0u;
    if (submit)
    {
        std::vector<uint8_t> imagePacket(16u + static_cast<size_t>(chunkQw) * 16u, 0u);
        const uint64_t imageTag =
            static_cast<uint64_t>(chunkQw & 0x7FFFu) |
            ((pendingQwc == chunkQw) ? (1ull << 15) : 0ull) |
            (static_cast<uint64_t>(kGifFmtImage) << 58);
        std::memcpy(imagePacket.data(), &imageTag, sizeof(imageTag));
        std::memcpy(imagePacket.data() + 16u, data, static_cast<size_t>(chunkQw) * 16u);
        submitGifPacket(GifPathId::Path2, imagePacket.data(), static_cast<uint32_t>(imagePacket.size()), true, pendingDirectHl,
                        true);
    }

    pendingQwc -= chunkQw;
    if (pendingQwc == 0u)
        pendingDirectHl = false;
    return chunkQw;
}

bool vif1IrqTraceEnabled()
{
    static const bool enabled = std::getenv("PS2_VIF1_IRQ_TRACE") != nullptr;
    return enabled;
}

namespace
{
    // UNPACK's vector loop. Kept out of the parser, and given the registers as values, so that its code does not
    // depend on the large function around it (VU1 memory writes through a member pointer otherwise force reloads
    // of the registers on every vector). Difference mode (STMOD 2) updates row.
    void vif1UnpackVectors(uint8_t *vu1Data, const uint8_t *srcBase, uint32_t writeVectorCount, uint32_t sourceVectorCount,
                           uint32_t bytesPerVector, uint32_t cl, uint32_t wl, uint32_t vuAddr, uint8_t vn, uint8_t vl,
                           int components, bool maskEnable, bool zeroExtend, bool readVuMem, bool writeVuMem,
                           uint32_t regMode, uint32_t maskBits, uint32_t row[4], const uint32_t col[4])
    {
        const uint32_t mode = regMode & 3u;
        // The plain case, which is nearly every UNPACK the game sends (CL = WL, no mask, no row addition, every
        // vector present in the stream, a 32/16/8-bit format): each vector goes to the next address and is all
        // data. Same result as the general loop below, without its per-vector division by WL, mask selection per
        // field and availability tests (this function was a tenth of the VU1 worker's time).
        if (cl == wl && !maskEnable && (mode == 0u || mode == 3u) && writeVuMem && sourceVectorCount >= writeVectorCount &&
            vl <= 2u && components >= 1 && components <= 4 && PS2_VU1_DATA_SIZE >= 0x4000u)
        {
            const uint32_t n = static_cast<uint32_t>(components);
            for (uint32_t i = 0; i < writeVectorCount; ++i)
            {
                uint8_t *dst = vu1Data + ((vuAddr + i) & 0x3FFu) * 16u;
                const uint8_t *src = srcBase + i * bytesPerVector;
                if (vl == 0u && n == 4u)
                {
                    std::memcpy(dst, src, 16);
                    continue;
                }
                uint32_t out[4] = {0u, 0u, 0u, 0u};
                if (n > 1u && n < 4u && readVuMem)
                    std::memcpy(out, dst, sizeof(out)); // the fields past the decoded components keep their value
                uint32_t value[4] = {0u, 0u, 0u, 0u};
                for (uint32_t c = 0; c < n; ++c)
                {
                    if (vl == 0u)
                        std::memcpy(&value[c], src + c * 4u, 4);
                    else if (vl == 1u)
                    {
                        uint16_t raw = 0;
                        std::memcpy(&raw, src + c * 2u, 2);
                        value[c] = zeroExtend ? static_cast<uint32_t>(raw)
                                              : static_cast<uint32_t>(static_cast<int32_t>(static_cast<int16_t>(raw)));
                    }
                    else
                        value[c] = zeroExtend ? static_cast<uint32_t>(src[c])
                                              : static_cast<uint32_t>(static_cast<int32_t>(static_cast<int8_t>(src[c])));
                }
                if (n == 1u)
                    out[0] = out[1] = out[2] = out[3] = value[0];
                else
                    for (uint32_t c = 0; c < n; ++c)
                        out[c] = value[c];
                std::memcpy(dst, out, sizeof(out));
            }
            return;
        }
        uint32_t srcIndex = 0u;
        for (uint32_t writeIndex = 0; writeIndex < writeVectorCount; ++writeIndex)
        {
            const uint32_t cyclePos = writeIndex % wl;
            const bool sourceAvailable = (cl >= wl) || (cyclePos < cl);

            uint32_t destVec = 0;
            if (cl >= wl)
            {
                destVec = (vuAddr + (writeIndex / wl) * cl + cyclePos) & 0x3FFu;
            }
            else
            {
                destVec = (vuAddr + writeIndex) & 0x3FFu;
            }

            uint32_t destOff = destVec * 16u;
            if (destOff + 16u > PS2_VU1_DATA_SIZE)
            {
                if (sourceAvailable && srcIndex < sourceVectorCount)
                    ++srcIndex;
                continue;
            }

            uint32_t lanes[4] = {0u, 0u, 0u, 0u};
            if (readVuMem)
                std::memcpy(lanes, vu1Data + destOff, sizeof(lanes));
            uint32_t decompressed[4] = {lanes[0], lanes[1], lanes[2], lanes[3]};
            bool decoded = false;

            const uint8_t *srcVec = nullptr;
            if (sourceAvailable && srcIndex < sourceVectorCount)
            {
                srcVec = srcBase + srcIndex * bytesPerVector;
                ++srcIndex;
                decoded = true;
            }

            auto extend16 = [&](uint16_t raw) -> uint32_t
            {
                if (zeroExtend)
                    return static_cast<uint32_t>(raw);
                return static_cast<uint32_t>(static_cast<int32_t>(static_cast<int16_t>(raw)));
            };

            auto extend8 = [&](uint8_t raw) -> uint32_t
            {
                if (zeroExtend)
                    return static_cast<uint32_t>(raw);
                return static_cast<uint32_t>(static_cast<int32_t>(static_cast<int8_t>(raw)));
            };

            bool handledFormat = true;
            if (!decoded)
            {
                handledFormat = false;
            }
            else if (vl == 0u)
            {
                if (components == 1)
                {
                    uint32_t scalar = 0;
                    std::memcpy(&scalar, srcVec, sizeof(scalar));
                    decompressed[0] = scalar;
                    decompressed[1] = scalar;
                    decompressed[2] = scalar;
                    decompressed[3] = scalar;
                }
                else
                {
                    const uint32_t limit = (components > 4) ? 4u : static_cast<uint32_t>(components);
                    for (uint32_t c = 0; c < limit; ++c)
                    {
                        uint32_t scalar = 0;
                        std::memcpy(&scalar, srcVec + c * 4u, sizeof(scalar));
                        decompressed[c] = scalar;
                    }
                }
            }
            else if (vl == 1u)
            {
                if (components == 1)
                {
                    uint16_t raw = 0;
                    std::memcpy(&raw, srcVec, sizeof(raw));
                    const uint32_t scalar = extend16(raw);
                    decompressed[0] = scalar;
                    decompressed[1] = scalar;
                    decompressed[2] = scalar;
                    decompressed[3] = scalar;
                }
                else
                {
                    const uint32_t limit = (components > 4) ? 4u : static_cast<uint32_t>(components);
                    for (uint32_t c = 0; c < limit; ++c)
                    {
                        uint16_t raw = 0;
                        std::memcpy(&raw, srcVec + c * 2u, sizeof(raw));
                        decompressed[c] = extend16(raw);
                    }
                }
            }
            else if (vl == 2u)
            {
                if (components == 1)
                {
                    const uint32_t scalar = extend8(srcVec[0]);
                    decompressed[0] = scalar;
                    decompressed[1] = scalar;
                    decompressed[2] = scalar;
                    decompressed[3] = scalar;
                }
                else
                {
                    const uint32_t limit = (components > 4) ? 4u : static_cast<uint32_t>(components);
                    for (uint32_t c = 0; c < limit; ++c)
                    {
                        decompressed[c] = extend8(srcVec[c]);
                    }
                }
            }
            else if (vl == 3u && vn == 3u)
            {
                // V4-5: packed color-like format in a single 16-bit value.
                uint16_t packed = 0;
                std::memcpy(&packed, srcVec, sizeof(packed));
                decompressed[0] = packed & 0x1Fu;
                decompressed[1] = (packed >> 5) & 0x1Fu;
                decompressed[2] = (packed >> 10) & 0x1Fu;
                decompressed[3] = (packed >> 15) & 0x01u;
            }
            else
            {
                handledFormat = false;
            }

            // Unknown compressed format fallback: preserve legacy raw-copy behavior.
            if (!handledFormat && decoded && !maskEnable && (regMode == 0u || regMode == 3u))
            {
                uint32_t copyBytes = (bytesPerVector < 16u) ? bytesPerVector : 16u;
                if (writeVuMem)
                    std::memcpy(vu1Data + destOff, srcVec, copyBytes);
                continue;
            }

            const bool canAdd = (vl != 3u || vn != 3u);
            const uint32_t colIdx = (cyclePos > 3u) ? 3u : cyclePos;
            const uint32_t maskCycle = (cyclePos > 3u) ? 3u : cyclePos;

            for (uint32_t field = 0u; field < 4u; ++field)
            {
                uint32_t maskSpec = 0u;
                if (maskEnable)
                {
                    const uint32_t shift = ((maskCycle * 4u) + field) * 2u;
                    maskSpec = (maskBits >> shift) & 0x3u;
                }

                // In fill-write cycles with suspended source reads, treat raw-data selections as row-fill.
                if (!decoded && maskSpec == 0u)
                    maskSpec = 1u;

                uint32_t writeVal = lanes[field];
                if (maskSpec == 0u)
                {
                    if (handledFormat)
                    {
                        writeVal = decompressed[field];
                        if (canAdd && (mode == 1u || mode == 2u))
                        {
                            writeVal = writeVal + row[field];
                            if (mode == 2u)
                                row[field] = writeVal;
                        }
                    }
                }
                else if (maskSpec == 1u)
                {
                    writeVal = row[field];
                }
                else if (maskSpec == 2u)
                {
                    writeVal = col[colIdx];
                }
                else
                {
                    continue; // write-protect
                }

                lanes[field] = writeVal;
            }

            if (writeVuMem)
                std::memcpy(vu1Data + destOff, lanes, sizeof(lanes));
        }
    }
}

void PS2Memory::processVIF1Data(const uint8_t *data, uint32_t sizeBytes)
{
    processVIF1Stream(data, sizeBytes, vu1ThreadQueues() ? Vif1Pass::Ee : Vif1Pass::Inline);
}

// One VIF1 parser for every mode, so the game-thread and worker passes cannot diverge: both see the same stream and
// the same EE register writes in the same order, so their register copies agree at every command.
void PS2Memory::processVIF1Stream(const uint8_t *data, uint32_t sizeBytes, Vif1Pass pass)
{
    if (sizeBytes == 0u)
        return;

    const bool workerPass = pass == Vif1Pass::Worker;
    const bool eePass = pass == Vif1Pass::Ee;
    const bool backEnd = !eePass; // this pass writes VU1 memory, runs VU1 programs and feeds the GIF
    VIFRegisters &regs = workerPass ? m_vif1WorkerRegs : vif1_regs;
    uint32_t &pendingImageQwc = workerPass ? m_vif1WorkerPendingPath2ImageQwc : m_vif1PendingPath2ImageQwc;
    bool &pendingDirectHl = workerPass ? m_vif1WorkerPendingPath2DirectHl : m_vif1PendingPath2DirectHl;
    const bool trace = vif1IrqTraceEnabled() && !workerPass;

    // Game-thread pass: the stretch [forwardStart, end) has been parsed and goes to the worker.
    uint32_t forwardStart = 0u;
    // check: the game thread's state is the state at `end` (not inside a command), so the worker can compare.
    auto forwardTo = [&](uint32_t end, bool check = true)
    {
        if (eePass && end > forwardStart)
            vu1ThreadForwardVif1(data + forwardStart, end - forwardStart, check);
        forwardStart = end;
    };

    // A stalled VIF1 holds everything that arrives until FBRST.STC resumes it. (Stalls are game-thread state.)
    if (!workerPass && m_vif1Stalled)
    {
        if (vif1IrqTraceEnabled())
            std::fprintf(stderr, "[vif1-irq] held %u more bytes while stalled\n", sizeBytes);
        m_vif1StalledData.insert(m_vif1StalledData.end(), data, data + sizeBytes);
        return;
    }

    uint32_t pos = 0;
    bool stallAfterCommand = false;

    while (pos + 4 <= sizeBytes)
    {
        // VIF i-bit: the command carrying it completes, then VIF1 stalls with STAT.INT
        // set and raises INTC VIF1 (cause 5). The rest of the stream waits for STC.
        if (stallAfterCommand)
        {
            if (trace)
                std::fprintf(stderr, "[vif1-irq] stall code=0x%08X mark=%u held=%u bytes\n", regs.code,
                             regs.mark, sizeBytes - pos);
            if (trace)
                dumpVif1CmdRing();
            forwardTo(pos);
            m_vif1Stalled = true;
            m_vif1StalledData.assign(data + pos, data + sizeBytes);
            queueCompletedIntcCause(5u);
            ps2_vu1_overlap_model::noteVif1Interrupt();
            return;
        }

        uint32_t nextWord = 0u;
        std::memcpy(&nextWord, data + pos, 4);
        if (pendingImageQwc != 0u && !isVif1Opcode(nextWord))
        {
            const uint32_t availableQw = (sizeBytes - pos) / 16u;
            if (availableQw == 0u)
            {
                break;
            }

            pos += submitPendingPath2Image(data + pos, availableQw, pendingImageQwc, pendingDirectHl, backEnd) * 16u;
            continue;
        }

        const uint32_t cmdPos = pos;
        uint32_t cmd;
        memcpy(&cmd, data + pos, 4);
        if (trace)
        {
            g_vif1CmdRing[g_vif1CmdRingPos % 8] = {cmd, pos, sizeBytes};
            ++g_vif1CmdRingPos;
        }
        pos += 4;

        uint8_t opcode = (cmd >> 24) & 0x7F;
        uint16_t imm = cmd & 0xFFFF;
        uint8_t num = (cmd >> 16) & 0xFF;
        const bool irq = (cmd & 0x80000000u) != 0u;

        // Track most-recent command for VIFn_CODE emulation.
        regs.code = cmd;
        regs.num = num;
        if (irq && (regs.err & 0x1u) == 0u) // ERR.MII masks the i-bit
        {
            regs.stat |= (1u << 11); // INT
            stallAfterCommand = !workerPass; // the worker replays stretches that end at the stall
        }

        if (opcode == VIF_NOP)
        {
            continue;
        }
        else if (opcode == VIF_STCYCL)
        {
            regs.cycle = imm;
            continue;
        }
        else if (opcode == VIF_OFFSET)
        {
            // VIF double-buffer setup. OFFSET clears DBF and resets TOPS to BASE.
            // Do not rewrite BASE from the previous TOPS value.
            regs.ofst = imm & 0x3FFu;
            regs.tops = regs.base & 0x3FFu;
            regs.stat &= ~(1u << 7); // clear DBF
            continue;
        }
        else if (opcode == VIF_BASE)
        {
            // BASE only updates the base register. TOPS changes on OFFSET/MSCAL.
            regs.base = imm & 0x3FFu;
            continue;
        }
        else if (opcode == VIF_ITOP)
        {
            // ITOP VIFcode writes pending ITOPS; VU XITOP observes it after MSCAL/MSCNT.
            regs.itops = imm & 0x3FFu;
            continue;
        }
        else if (opcode == VIF_STMOD)
        {
            regs.mode = imm & 3u;
            continue;
        }
        else if (opcode == VIF_MSKPATH3)
        {
            // VIF command docs: MSKPATH3 uses IMMEDIATE bit 15. The PATH3 mask is game-thread state; the packets it
            // releases follow the VIF1 stream up to and including this command.
            if (!workerPass)
            {
                const bool wasMasked = m_path3Masked;
                m_path3Masked = (imm & 0x8000u) != 0u;
                if (wasMasked && !m_path3Masked)
                {
                    forwardTo(pos);
                    flushMaskedPath3Packets();
                }
            }
            continue;
        }
        else if (opcode == VIF_MARK)
        {
            regs.mark = imm;
            regs.stat |= (1u << 6); // MRK
            continue;
        }
        else if (opcode == VIF_FLUSHE || opcode == VIF_FLUSH || opcode == VIF_FLUSHA)
        {
            continue;
        }
        else if (opcode == VIF_MSCAL || opcode == VIF_MSCALF)
        {
            uint32_t startPC = (uint32_t)imm * 8u;

            const uint32_t runTop = regs.tops & 0x3FFu;
            const uint32_t runItop = regs.itops & 0x3FFu;
            regs.top = runTop;
            regs.itop = runItop;

            const bool dbf = (regs.stat & (1u << 7)) != 0u;
            if (dbf)
                regs.tops = regs.base & 0x3FFu;
            else
                regs.tops = (regs.base + regs.ofst) & 0x3FFu;
            regs.stat ^= (1u << 7); // toggle DBF

            if (backEnd && m_vu1MscalCallback)
                m_vu1MscalCallback(startPC, runTop, runItop);
            else if (eePass && m_vu1EeMscalFn)
                m_vu1EeMscalFn();
            continue;
        }
        else if (opcode == VIF_MSCNT)
        {
            const uint32_t runTop = regs.tops & 0x3FFu;
            const uint32_t runItop = regs.itops & 0x3FFu;
            regs.top = runTop;
            regs.itop = runItop;

            const bool dbf = (regs.stat & (1u << 7)) != 0u;
            if (dbf)
                regs.tops = regs.base & 0x3FFu;
            else
                regs.tops = (regs.base + regs.ofst) & 0x3FFu;
            regs.stat ^= (1u << 7); // toggle DBF

            if (backEnd && m_vu1MscntCallback)
                m_vu1MscntCallback(runTop, runItop);
            else if (eePass && m_vu1EeMscalFn)
                m_vu1EeMscalFn();
            continue;
        }
        else if (opcode == VIF_STMASK)
        {
            if (pos + 4 > sizeBytes)
                break;
            uint32_t maskValue = 0;
            std::memcpy(&maskValue, data + pos, sizeof(maskValue));
            regs.mask = maskValue;
            pos += 4;
            continue;
        }
        else if (opcode == VIF_STROW)
        {
            if (pos + 16 > sizeBytes)
                break;
            std::memcpy(regs.row, data + pos, 16);
            pos += 16;
            continue;
        }
        else if (opcode == VIF_STCOL)
        {
            if (pos + 16 > sizeBytes)
                break;
            std::memcpy(regs.col, data + pos, 16);
            pos += 16;
            continue;
        }
        else if (opcode == VIF_MPG)
        {
            uint32_t destAddr = (uint32_t)imm * 8u;
            const uint32_t instructionCount = (num == 0u) ? 256u : static_cast<uint32_t>(num);
            const uint32_t mpgBytes = instructionCount * 8u;
            if (backEnd && m_vu1Code && destAddr < PS2_VU1_CODE_SIZE && mpgBytes > 0)
            {
                uint32_t copyBytes = mpgBytes;
                if (destAddr + copyBytes > PS2_VU1_CODE_SIZE)
                    copyBytes = PS2_VU1_CODE_SIZE - destAddr;
                if (pos + copyBytes <= sizeBytes)
                {
                    std::memcpy(m_vu1Code + destAddr, data + pos, copyBytes);
                    markVU1CodeModified();
                }
            }
            pos += mpgBytes;
            if (pos > sizeBytes)
                break;
            continue;
        }
        else if (opcode == VIF_DIRECT || opcode == VIF_DIRECTHL)
        {
            uint32_t qwCount = imm;
            if (qwCount == 0)
                qwCount = 65536;
            const uint32_t availableQw = (sizeBytes - pos) / 16u;
            const bool truncated = qwCount > availableQw;
            if (qwCount > availableQw)
                qwCount = availableQw;

            uint32_t imageQw = 0u;
            if (qwCount > 0 && pendingImageQwc != 0u)
            {
                imageQw = submitPendingPath2Image(data + pos, qwCount, pendingImageQwc, pendingDirectHl, backEnd);
                pos += imageQw * 16u;
                qwCount -= imageQw;
            }

            if (qwCount > 0)
            {
                const bool directHl = (opcode == VIF_DIRECTHL);
                if (backEnd)
                    submitGifPacket(GifPathId::Path2, data + pos, qwCount * 16, true, directHl);

                const uint32_t pendingImageQw = pendingGifImageQwc(data + pos, qwCount * 16u);
                if (pendingImageQw != 0u)
                {
                    if (trace)
                    {
                        const uint32_t after = pos + qwCount * 16u;
                        uint32_t w[4] = {};
                        if (after + 16u <= sizeBytes)
                            std::memcpy(w, data + after, 16u);
                        std::fprintf(stderr, "[vif1-irq] DIRECT %u leaves %u image qw pending; next words %08X %08X %08X %08X\n",
                                     qwCount, pendingImageQw, w[0], w[1], w[2], w[3]);
                    }
                    pendingImageQwc = pendingImageQw;
                    pendingDirectHl = directHl;
                }
            }

            pos += qwCount * 16;
            if (truncated)
            {
                pos = sizeBytes;
                break;
            }
            continue;
        }
        else if ((opcode & 0x60) == 0x60)
        {
            uint8_t vn = (opcode >> 2) & 0x3;
            uint8_t vl = opcode & 0x3;
            const bool maskEnable = (opcode & 0x10u) != 0u;
            int components = vn + 1;
            int bitsPerComponent = 32;
            switch (vl)
            {
            case 0:
                bitsPerComponent = 32;
                break;
            case 1:
                bitsPerComponent = 16;
                break;
            case 2:
                bitsPerComponent = 8;
                break;
            case 3:
                bitsPerComponent = (vn == 3) ? 4 : 16;
                break;
            default:
                break;
            }
            int bitsPerVector = (vl == 3 && vn == 3) ? 16 : (components * bitsPerComponent);
            uint32_t bytesPerVector = (bitsPerVector + 7) / 8;
            // UNPACK semantics: NUM is 8-bit and NUM==0 means 256 vectors (writes).
            const uint32_t writeVectorCount = (num == 0u) ? 256u : static_cast<uint32_t>(num);

            // STCYCL controls write cycles for UNPACK.
            uint32_t cl = regs.cycle & 0xFFu;
            uint32_t wl = (regs.cycle >> 8) & 0xFFu;
            if (cl == 0u)
                cl = 1u;
            if (wl == 0u)
                wl = 1u;

            uint32_t sourceVectorCount = writeVectorCount;
            if (cl < wl)
            {
                const uint32_t fullBlocks = writeVectorCount / wl;
                uint32_t remainder = writeVectorCount % wl;
                if (remainder > cl)
                    remainder = cl;
                sourceVectorCount = fullBlocks * cl + remainder;
            }

            uint32_t totalBytes = sourceVectorCount * bytesPerVector;
            totalBytes = (totalBytes + 3) & ~3u;

            uint32_t vuAddr = (uint32_t)imm & 0x3FFu;
            if ((imm & 0x8000u) != 0u)
                vuAddr = (vuAddr + (regs.tops & 0x3FFu)) & 0x3FFu;

            const bool zeroExtend = (imm & 0x4000u) != 0u;

            // The game-thread pass writes no VU1 memory, but difference mode (STMOD 2) carries the written values
            // into ROW. For V2/V3 the fields past the decoded components keep their old VU1 memory value and so
            // feed ROW from memory: then the worker must first finish everything before this command.
            bool readVuMem = true;
            const bool writeVuMem = backEnd;
            bool unpackHere = m_vu1Data && totalBytes > 0 && pos + totalBytes <= sizeBytes;
            if (unpackHere && eePass)
            {
                const bool difference = (regs.mode & 3u) == 2u;
                unpackHere = difference && vl != 3u;
                readVuMem = unpackHere && (components == 2 || components == 3);
                if (readVuMem)
                {
                    forwardTo(cmdPos, false);
                    m_vu1Thread->noteFallback(Vu1Thread::kWaitUnpack);
                    vu1ThreadWait(Vu1Thread::kWaitUnpack);
                }
            }

            if (unpackHere)
            {
                const uint8_t *srcBase = data + pos;
                uint32_t row[4];
                uint32_t col[4];
                std::memcpy(row, regs.row, sizeof(row));
                std::memcpy(col, regs.col, sizeof(col));
                vif1UnpackVectors(m_vu1Data, srcBase, writeVectorCount, sourceVectorCount, bytesPerVector, cl, wl, vuAddr, vn,
                                  vl, components, maskEnable, zeroExtend, readVuMem, writeVuMem, regs.mode, regs.mask, row,
                                  col);
                if ((regs.mode & 3u) == 2u)
                    std::memcpy(regs.row, row, sizeof(row));
            }
            pos += totalBytes;

            if (pos > sizeBytes)
                break;
            continue;
        }
        else
        {
            continue;
        }
    }

    forwardTo(sizeBytes);
    if (stallAfterCommand)
    {
        if (trace)
            std::fprintf(stderr, "[vif1-irq] stall code=0x%08X mark=%u held=0 bytes (end of data)\n",
                         regs.code, regs.mark);
        m_vif1Stalled = true;
        m_vif1StalledData.clear();
        queueCompletedIntcCause(5u);
        ps2_vu1_overlap_model::noteVif1Interrupt();
    }
}
