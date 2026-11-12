#include "iop_spu2.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace ps2x::iop::detail
{
    namespace
    {
        // PS2X_SPU2_RAM_MB: sound RAM size in MB, a power of two from 2 to 32 (anything else: the PS2's 2 MB).
        uint32_t soundRamBytes()
        {
            const char *env = std::getenv("PS2X_SPU2_RAM_MB");
            const unsigned long mb = env ? std::strtoul(env, nullptr, 10) : 0ul;
            if (mb < 2ul || mb > IopSpu2::MaxRamBytes / (1024u * 1024u) || (mb & (mb - 1ul)) != 0ul)
                return IopSpu2::RamBytes;
            return static_cast<uint32_t>(mb) * 1024u * 1024u;
        }
        constexpr uint32_t kCoreStride = 0x400u;
        constexpr uint32_t kMasterBase = 0x760u;
        constexpr uint32_t kMasterStride = 0x28u;
        constexpr uint32_t kRegisterBytes = 0x800u;

        // Core registers (offsets inside a core's 0x400-byte block).
        constexpr uint32_t kPmon = 0x180u;
        constexpr uint32_t kNon = 0x184u;
        constexpr uint32_t kVmixl = 0x188u;
        constexpr uint32_t kVmixel = 0x18Cu;
        constexpr uint32_t kVmixr = 0x190u;
        constexpr uint32_t kVmixer = 0x194u;
        constexpr uint32_t kMmix = 0x198u;
        constexpr uint32_t kAttr = 0x19Au;
        constexpr uint32_t kKon = 0x1A0u;
        constexpr uint32_t kKoff = 0x1A4u;
        constexpr uint32_t kVoiceAddressBase = 0x1C0u;
        constexpr uint32_t kVoiceAddressEnd = kVoiceAddressBase + 24u * 12u;
        constexpr uint32_t kEndx = 0x340u;
        constexpr uint32_t kStatx = 0x344u;

        // Master registers (offsets inside a core's 0x28-byte block at 0x760).
        constexpr uint32_t kMvoll = 0x00u;
        constexpr uint32_t kMvolr = 0x02u;
        constexpr uint32_t kEvoll = 0x04u;
        constexpr uint32_t kEvolr = 0x06u;
        constexpr uint32_t kAvoll = 0x08u;
        constexpr uint32_t kAvolr = 0x0Au;
        constexpr uint32_t kBvoll = 0x0Cu;
        constexpr uint32_t kBvolr = 0x0Eu;
        constexpr uint32_t kMvolxl = 0x10u;
        constexpr uint32_t kMvolxr = 0x12u;

        // MMIX gates.
        constexpr uint16_t kMixExtWetR = 0x001u, kMixExtWetL = 0x002u, kMixExtDryR = 0x004u, kMixExtDryL = 0x008u;
        constexpr uint16_t kMixInWetR = 0x010u, kMixInWetL = 0x020u, kMixInDryR = 0x040u, kMixInDryL = 0x080u;
        constexpr uint16_t kMixSndWetR = 0x100u, kMixSndWetL = 0x200u, kMixSndDryR = 0x400u, kMixSndDryL = 0x800u;

        // ADPCM block flags.
        constexpr uint8_t kLoopEnd = 1u, kLoopRepeat = 2u, kLoopStart = 4u;

        constexpr int32_t kFilterPositive[5] = {0, 60, 115, 98, 122};
        constexpr int32_t kFilterNegative[5] = {0, 0, -52, -55, -60};

        int32_t clamp16(int32_t value)
        {
            return std::clamp(value, -0x8000, 0x7FFF);
        }

        // A fixed volume register holds volume/2 in bits 14-0 (signed).
        int32_t fixedVolume(uint16_t value)
        {
            return static_cast<int16_t>(static_cast<uint16_t>(value << 1));
        }

        // The envelope step used by the ADSR and the volume sweeps.
        struct EnvelopeStep
        {
            int32_t cycles;
            int32_t step;
        };

        EnvelopeStep envelopeStep(uint32_t shift, int32_t step, bool exponential, bool decrease, int32_t level)
        {
            int32_t cycles = 1 << std::max<int32_t>(0, static_cast<int32_t>(shift) - 11);
            int32_t delta = step << std::max<int32_t>(0, 11 - static_cast<int32_t>(shift));
            if (exponential && !decrease && level > 0x6000)
                cycles *= 4;
            if (exponential && decrease)
                delta = static_cast<int32_t>((static_cast<int64_t>(delta) * level) >> 15);
            return {cycles, delta};
        }

        std::array<int16_t, 512> buildInterpolationTable()
        {
            // The SPU2 interpolates four samples through a 512-entry Gaussian table. The hardware table is not
            // reproduced here: this is a Gaussian kernel (sigma 0.627 samples, matching the hardware's centre
            // weight of about 0.64) normalised so that the four taps of every phase sum to 0x8000.
            constexpr double kSigma = 0.627;
            std::array<double, 512> kernel{};
            for (uint32_t k = 0; k < 512u; ++k)
            {
                const double distance = 2.0 - static_cast<double>(k) / 256.0;
                kernel[k] = std::exp(-(distance * distance) / (2.0 * kSigma * kSigma));
            }
            std::array<int16_t, 512> table{};
            for (uint32_t i = 0; i < 256u; ++i)
            {
                const double sum = kernel[255u - i] + kernel[511u - i] + kernel[256u + i] + kernel[i];
                const auto scaled = [&](uint32_t k) { return std::lround(kernel[k] * 32768.0 / sum); };
                const long a = scaled(255u - i), b = scaled(511u - i), c = scaled(256u + i);
                table[255u - i] = static_cast<int16_t>(a);
                table[511u - i] = static_cast<int16_t>(b);
                table[256u + i] = static_cast<int16_t>(c);
                table[i] = static_cast<int16_t>(std::clamp<long>(32768 - a - b - c, -0x8000, 0x7FFF));
            }
            return table;
        }
    }

    const std::array<int16_t, 512> &IopSpu2::interpolationTable()
    {
        static const std::array<int16_t, 512> table = buildInterpolationTable();
        return table;
    }

    void IopSpu2::Volume::write(uint16_t value)
    {
        reg = value;
        counter = 0;
        if ((value & 0x8000u) == 0u)
            current = fixedVolume(value);
    }

    void IopSpu2::Volume::tick()
    {
        if ((reg & 0x8000u) == 0u)
            return;
        // Sweep: bit 14 exponential, bit 13 decrease, bit 12 negative phase, bits 6-0 rate.
        const bool exponential = (reg & 0x4000u) != 0u;
        const bool decrease = (reg & 0x2000u) != 0u;
        const bool negative = (reg & 0x1000u) != 0u;
        const uint32_t rate = reg & 0x7Fu;
        const int32_t step = decrease ? -8 + static_cast<int32_t>(rate & 3u) : 7 - static_cast<int32_t>(rate & 3u);
        int32_t magnitude = std::abs(current);
        const EnvelopeStep s = envelopeStep(rate >> 2u, step, exponential, decrease, magnitude);
        if (--counter > 0)
            return;
        counter = s.cycles;
        magnitude = std::clamp(magnitude + s.step, 0, 0x7FFF);
        current = negative ? -magnitude : magnitude;
    }

    void IopSpu2::tickAdsr(Envelope &envelope, uint16_t adsr1, uint16_t adsr2)
    {
        uint32_t shift = 0u;
        int32_t step = 0;
        bool exponential = false;
        bool decrease = false;
        switch (envelope.phase)
        {
        case 1: // attack
        {
            const uint32_t rate = (adsr1 >> 8u) & 0x7Fu;
            shift = rate >> 2u;
            step = 7 - static_cast<int32_t>(rate & 3u);
            exponential = (adsr1 & 0x8000u) != 0u;
            break;
        }
        case 2: // decay: bits 7-4 are the shift itself (the rate is shift*4, with step -8)
            shift = (adsr1 >> 4u) & 0xFu;
            step = -8;
            exponential = true;
            decrease = true;
            break;
        case 3: // sustain
        {
            const uint32_t rate = (adsr2 >> 6u) & 0x7Fu;
            shift = rate >> 2u;
            decrease = (adsr2 & 0x4000u) != 0u;
            step = decrease ? -8 + static_cast<int32_t>(rate & 3u) : 7 - static_cast<int32_t>(rate & 3u);
            exponential = (adsr2 & 0x8000u) != 0u;
            break;
        }
        case 4: // release: bits 4-0 are the shift itself (the rate is shift*4, with step -8)
            shift = adsr2 & 0x1Fu;
            step = -8;
            exponential = (adsr2 & 0x20u) != 0u;
            decrease = true;
            break;
        default:
            return;
        }

        const EnvelopeStep s = envelopeStep(shift, step, exponential, decrease, envelope.level);
        if (--envelope.counter > 0)
            return;
        envelope.counter = s.cycles;
        envelope.level = std::clamp(envelope.level + s.step, 0, 0x7FFF);

        switch (envelope.phase)
        {
        case 1:
            if (envelope.level >= 0x7FFF)
            {
                envelope.phase = 2;
                envelope.counter = 0;
            }
            break;
        case 2:
        {
            const int32_t sustainLevel = ((adsr1 & 0xFu) + 1) * 0x800;
            if (envelope.level <= sustainLevel)
            {
                envelope.phase = 3;
                envelope.counter = 0;
            }
            break;
        }
        case 4:
            if (envelope.level <= 0)
                envelope.phase = 0;
            break;
        default:
            break;
        }
    }

    int16_t IopSpu2::decodeAdpcmBlock(const uint8_t block[16], int16_t &history1, int16_t &history2, int16_t out[28])
    {
        uint32_t shift = block[0] & 0xFu;
        if (shift > 12u)
            shift = 9u; // shift values 13-15 behave like 9
        const uint32_t filter = std::min<uint32_t>((block[0] >> 4u) & 0x7u, 4u);
        const int32_t f0 = kFilterPositive[filter];
        const int32_t f1 = kFilterNegative[filter];
        int32_t s1 = history1;
        int32_t s2 = history2;
        for (uint32_t i = 0; i < 28u; ++i)
        {
            const uint8_t byte = block[2u + (i >> 1u)];
            const uint32_t nibble = (i & 1u) ? (byte >> 4u) : (byte & 0xFu);
            const int32_t raw = static_cast<int16_t>(static_cast<uint16_t>(nibble << 12u)) >> shift;
            const int32_t sample = clamp16(raw + ((s1 * f0 + s2 * f1 + 32) >> 6));
            out[i] = static_cast<int16_t>(sample);
            s2 = s1;
            s1 = sample;
        }
        history1 = static_cast<int16_t>(s1);
        history2 = static_cast<int16_t>(s2);
        return block[1];
    }

    IopSpu2::IopSpu2()
        : m_ramBytes(soundRamBytes()), m_addressMask(m_ramBytes / 2u - 1u), m_ram(m_ramBytes, 0u)
    {
        if (m_ramBytes != RamBytes)
            std::fprintf(stderr, "spu2: sound RAM extended to %u MB (PS2X_SPU2_RAM_MB)\n", m_ramBytes >> 20u);
        reset();
    }

    void IopSpu2::reset()
    {
        std::fill(m_ram.begin(), m_ram.end(), uint8_t{0});
        resetRegisters();
        m_samples = 0u;
        m_keyOns = 0u;
    }

    void IopSpu2::resetRegisters()
    {
        m_regs.fill(0u);
        for (Core &core : m_cores)
            core = Core{};
    }

    void IopSpu2::startInput(uint32_t core)
    {
        Core &c = m_cores[core & 1u];
        c.inputRunning = true;
        c.inputFrame = InputBlockFrames;
    }

    void IopSpu2::stopInput(uint32_t core)
    {
        Core &c = m_cores[core & 1u];
        c.inputRunning = false;
        c.inputFrame = InputBlockFrames;
    }

    uint32_t IopSpu2::activeVoices() const noexcept
    {
        uint32_t count = 0u;
        for (const Core &core : m_cores)
            for (const Voice &voice : core.voices)
                count += voice.envelope.phase != 0u ? 1u : 0u;
        return count;
    }

    void IopSpu2::writeRam(uint32_t byteAddress, const uint8_t *data, size_t size)
    {
        for (size_t i = 0; i < size; ++i)
            m_ram[(byteAddress + i) & (m_ramBytes - 1u)] = data[i];
    }

    void IopSpu2::readRam(uint32_t byteAddress, uint8_t *data, size_t size) const
    {
        for (size_t i = 0; i < size; ++i)
            data[i] = m_ram[(byteAddress + i) & (m_ramBytes - 1u)];
    }

    void IopSpu2::zeroRam(uint32_t byteAddress, size_t size)
    {
        for (size_t i = 0; i < size; ++i)
            m_ram[(byteAddress + i) & (m_ramBytes - 1u)] = 0u;
    }

    uint16_t IopSpu2::voiceNax(const Voice &voice, bool high) const
    {
        // NAX is the next halfword to be read: the block header, then the data halfword of the next sample.
        const uint32_t nax = voice.needFetch ? voice.block : ((voice.block + 1u + voice.sampleIndex / 4u) & m_addressMask);
        return static_cast<uint16_t>(high ? (nax >> 16u) : (nax & 0xFFFFu));
    }

    uint16_t IopSpu2::read16(uint32_t offset) const
    {
        offset &= 0xFFFFu;
        if (offset >= kRegisterBytes)
            return 0u;
        offset &= ~1u;
        if (offset >= kMasterBase && offset < kMasterBase + 2u * kMasterStride)
        {
            const Core &core = m_cores[(offset - kMasterBase) / kMasterStride];
            switch ((offset - kMasterBase) % kMasterStride)
            {
            case kMvolxl:
                return static_cast<uint16_t>(core.masterLeft.current);
            case kMvolxr:
                return static_cast<uint16_t>(core.masterRight.current);
            default:
                return m_regs[offset >> 1u];
            }
        }
        if (offset >= kMasterBase)
            return m_regs[offset >> 1u];

        const uint32_t coreIndex = offset / kCoreStride;
        const uint32_t local = offset % kCoreStride;
        const Core &core = m_cores[coreIndex];
        if (local < 0x180u)
        {
            const Voice &voice = core.voices[local >> 4u];
            switch (local & 0xFu)
            {
            case 0x0A: // ENVX
                return static_cast<uint16_t>(voice.envelope.level);
            case 0x0C: // VOLXL
                return static_cast<uint16_t>(voice.left.current);
            case 0x0E: // VOLXR
                return static_cast<uint16_t>(voice.right.current);
            default:
                return m_regs[offset >> 1u];
            }
        }
        if (local >= kVoiceAddressBase && local < kVoiceAddressEnd)
        {
            const uint32_t rel = local - kVoiceAddressBase;
            const Voice &voice = core.voices[rel / 12u];
            switch (rel % 12u)
            {
            case 4: // LSAX
                return static_cast<uint16_t>(voice.lsax >> 16u);
            case 6:
                return static_cast<uint16_t>(voice.lsax & 0xFFFFu);
            case 8: // NAX
                return voiceNax(voice, true);
            case 10:
                return voiceNax(voice, false);
            default:
                return m_regs[offset >> 1u];
            }
        }
        switch (local)
        {
        case kEndx:
            return static_cast<uint16_t>(core.endx & 0xFFFFu);
        case kEndx + 2u:
            return static_cast<uint16_t>(core.endx >> 16u);
        case kStatx:
            return 0u; // transfers complete at once: never busy
        case kKon:
        case kKon + 2u:
        case kKoff:
        case kKoff + 2u:
            return 0u;
        default:
            return m_regs[offset >> 1u];
        }
    }

    void IopSpu2::write16(uint32_t offset, uint16_t value)
    {
        offset &= 0xFFFFu;
        if (offset >= kRegisterBytes)
            return;
        offset &= ~1u;
        m_regs[offset >> 1u] = value;

        if (offset >= kMasterBase && offset < kMasterBase + 2u * kMasterStride)
        {
            Core &core = m_cores[(offset - kMasterBase) / kMasterStride];
            switch ((offset - kMasterBase) % kMasterStride)
            {
            case kMvoll: core.masterLeft.write(value); break;
            case kMvolr: core.masterRight.write(value); break;
            case kEvoll: core.effectLeft = static_cast<int16_t>(value); break;
            case kEvolr: core.effectRight = static_cast<int16_t>(value); break;
            case kAvoll: core.externalLeft = static_cast<int16_t>(value); break;
            case kAvolr: core.externalRight = static_cast<int16_t>(value); break;
            case kBvoll: core.inputLeft = static_cast<int16_t>(value); break;
            case kBvolr: core.inputRight = static_cast<int16_t>(value); break;
            default: break;
            }
            return;
        }
        if (offset >= kMasterBase)
            return;

        const uint32_t coreIndex = offset / kCoreStride;
        const uint32_t local = offset % kCoreStride;
        Core &core = m_cores[coreIndex];
        const auto setLow = [&](uint32_t &bits) { bits = (bits & 0xFF0000u) | value; };
        const auto setHigh = [&](uint32_t &bits) { bits = (bits & 0x00FFFFu) | (static_cast<uint32_t>(value & 0xFFu) << 16u); };

        if (local < 0x180u)
        {
            Voice &voice = core.voices[local >> 4u];
            switch (local & 0xFu)
            {
            case 0x0: voice.left.write(value); break;
            case 0x2: voice.right.write(value); break;
            case 0x4: voice.pitch = value; break;
            case 0x6: voice.adsr1 = value; break;
            case 0x8: voice.adsr2 = value; break;
            case 0xA: voice.envelope.level = value & 0x7FFFu; break; // ENVX is writable
            default: break;
            }
            return;
        }
        if (local >= kVoiceAddressBase && local < kVoiceAddressEnd)
        {
            const uint32_t rel = local - kVoiceAddressBase;
            Voice &voice = core.voices[rel / 12u];
            const auto setAddress = [&](uint32_t &address, bool high)
            {
                // The PS2's high halfword holds 4 bits (2 MB); a larger sound RAM takes as many as it needs.
                address = high ? ((address & 0xFFFFu) | (static_cast<uint32_t>(value & (m_addressMask >> 16u)) << 16u))
                               : ((address & ~0xFFFFu) | value);
            };
            switch (rel % 12u)
            {
            case 0: setAddress(voice.ssa, true); break;
            case 2: setAddress(voice.ssa, false); break;
            case 4: setAddress(voice.lsax, true); voice.lsaxWritten = true; break;
            case 6: setAddress(voice.lsax, false); voice.lsaxWritten = true; break;
            case 8:
            case 10:
            {
                uint32_t nax = voice.block;
                setAddress(nax, rel % 12u == 8u);
                voice.block = nax & ~7u;
                voice.needFetch = true;
                break;
            }
            default: break;
            }
            return;
        }
        switch (local)
        {
        case kPmon: setLow(core.pmon); break;
        case kPmon + 2u: setHigh(core.pmon); break;
        case kNon: setLow(core.non); break;
        case kNon + 2u: setHigh(core.non); break;
        case kVmixl: setLow(core.vmixl); break;
        case kVmixl + 2u: setHigh(core.vmixl); break;
        case kVmixel: setLow(core.vmixel); break;
        case kVmixel + 2u: setHigh(core.vmixel); break;
        case kVmixr: setLow(core.vmixr); break;
        case kVmixr + 2u: setHigh(core.vmixr); break;
        case kVmixer: setLow(core.vmixer); break;
        case kVmixer + 2u: setHigh(core.vmixer); break;
        case kMmix: core.mmix = value; break;
        case kAttr: core.attr = value; break;
        case kKon: keyOn(core, value); break;
        case kKon + 2u: keyOn(core, static_cast<uint32_t>(value & 0xFFu) << 16u); break;
        case kKoff: keyOff(core, value); break;
        case kKoff + 2u: keyOff(core, static_cast<uint32_t>(value & 0xFFu) << 16u); break;
        case kEndx: core.endx &= 0xFF0000u; break; // a write clears that half
        case kEndx + 2u: core.endx &= 0x00FFFFu; break;
        default: break;
        }
    }

    std::string IopSpu2::describeVoice(uint32_t coreIndex, uint32_t voiceIndex) const
    {
        const Core &core = m_cores[coreIndex & 1u];
        const Voice &voice = core.voices[voiceIndex % VoicesPerCore];
        const uint32_t bit = 1u << (voiceIndex % VoicesPerCore);
        uint8_t block[16];
        readRam((voice.block & m_addressMask) * 2u, block, sizeof(block));
        uint32_t nonzero = 0u; // data bytes (not block headers) that are not zero, over the next 64 blocks
        for (uint32_t b = 0; b < 64u; ++b)
        {
            uint8_t data[16];
            readRam(((voice.block + 8u * b) & m_addressMask) * 2u, data, sizeof(data));
            for (uint32_t i = 2u; i < 16u; ++i)
                nonzero += data[i] != 0u ? 1u : 0u;
        }
        char text[400];
        std::snprintf(text, sizeof text,
                      "core %u voice %2u: SSA 0x%05X LSAX 0x%05X pitch 0x%04X ADSR %04X/%04X env %u:%04X VOL %04X/%04X "
                      "(now %d/%d) mix dry %c%c wet %c%c PMON %u NON %u MMIX %04X MVOL %d/%d ENDX %u | block %02X %02X "
                      "(%u nonzero data bytes in 64 blocks)",
                      coreIndex & 1u, voiceIndex % VoicesPerCore, voice.ssa * 2u, voice.lsax * 2u, voice.pitch,
                      voice.adsr1, voice.adsr2, voice.envelope.phase, voice.envelope.level, voice.left.reg,
                      voice.right.reg, voice.left.current, voice.right.current, (core.vmixl & bit) ? 'L' : '-',
                      (core.vmixr & bit) ? 'R' : '-', (core.vmixel & bit) ? 'L' : '-', (core.vmixer & bit) ? 'R' : '-',
                      (core.pmon & bit) ? 1u : 0u, (core.non & bit) ? 1u : 0u, core.mmix, core.masterLeft.current,
                      core.masterRight.current, (core.endx & bit) ? 1u : 0u, block[0], block[1], nonzero);
        return text;
    }

    int32_t IopSpu2::voicePeak(uint32_t core, uint32_t voice) const
    {
        return m_cores[core & 1u].voices[voice % VoicesPerCore].peak;
    }

    void IopSpu2::keyOn(Core &core, uint32_t bits)
    {
        for (uint32_t v = 0; v < VoicesPerCore; ++v)
        {
            if ((bits & (1u << v)) == 0u)
                continue;
            Voice &voice = core.voices[v];
            voice.block = voice.ssa & ~7u;
            voice.needFetch = true;
            voice.sampleIndex = 0u;
            voice.counter = 0u;
            voice.history1 = voice.history2 = 0;
            voice.previous.fill(0);
            voice.lsaxWritten = false;
            voice.envelope = Envelope{0, 0, 1};
            voice.output = 0;
            voice.peak = 0;
            core.endx &= ~(1u << v);
            ++m_keyOns;
        }
    }

    void IopSpu2::keyOff(Core &core, uint32_t bits)
    {
        for (uint32_t v = 0; v < VoicesPerCore; ++v)
        {
            if ((bits & (1u << v)) == 0u)
                continue;
            Envelope &envelope = core.voices[v].envelope;
            if (envelope.phase != 0u)
            {
                envelope.phase = 4;
                envelope.counter = 0;
            }
        }
    }

    void IopSpu2::fetchBlock(Voice &voice)
    {
        uint8_t block[16];
        readRam((voice.block & m_addressMask) * 2u, block, sizeof(block));
        voice.flags = static_cast<uint8_t>(decodeAdpcmBlock(block, voice.history1, voice.history2, voice.decoded.data()));
        // A loop-start flag sets the loop address unless the program wrote LSAX after key-on.
        if ((voice.flags & kLoopStart) != 0u && !voice.lsaxWritten)
            voice.lsax = voice.block;
        voice.sampleIndex = 0u;
        voice.needFetch = false;
    }

    int16_t IopSpu2::nextSample(Core &core, uint32_t coreIndex, uint32_t voiceIndex)
    {
        (void)coreIndex;
        Voice &voice = core.voices[voiceIndex];
        if (voice.needFetch)
            fetchBlock(voice);
        const int16_t sample = voice.decoded[voice.sampleIndex++];
        if (voice.sampleIndex >= 28u)
        {
            if ((voice.flags & kLoopEnd) != 0u)
            {
                core.endx |= 1u << voiceIndex;
                voice.block = voice.lsax & ~7u;
                if ((voice.flags & kLoopRepeat) == 0u)
                {
                    // End without repeat: the voice stops; it keeps fetching from the loop address in silence.
                    voice.envelope.phase = 0;
                    voice.envelope.level = 0;
                }
            }
            else
            {
                voice.block = (voice.block + 8u) & m_addressMask;
            }
            voice.needFetch = true;
            voice.sampleIndex = 0u;
        }
        return sample;
    }

    void IopSpu2::tickNoise(Core &core)
    {
        // Noise generator: bits 13-8 of ATTR set the clock (shift in bits 13-10, step in bits 9-8).
        const uint32_t clock = (core.attr >> 8u) & 0x3Fu;
        const uint32_t shift = clock >> 2u;
        const int32_t step = 4 + static_cast<int32_t>(clock & 3u);
        core.noiseCounter -= step;
        if (core.noiseCounter > 0)
            return;
        core.noiseCounter += 0x20000 >> shift;
        const uint32_t level = static_cast<uint16_t>(core.noiseLevel);
        const uint32_t parity = ((level >> 15u) ^ (level >> 12u) ^ (level >> 11u) ^ (level >> 10u) ^ 1u) & 1u;
        core.noiseLevel = static_cast<int16_t>(static_cast<uint16_t>((level << 1u) | parity));
    }

    void IopSpu2::mixCore(uint32_t coreIndex, int32_t externalLeft, int32_t externalRight, int32_t &outLeft,
                          int32_t &outRight)
    {
        Core &core = m_cores[coreIndex];
        const std::array<int16_t, 512> &gauss = interpolationTable();
        tickNoise(core);

        int32_t dryLeft = 0, dryRight = 0, wetLeft = 0, wetRight = 0;
        int32_t previousOutput = 0;
        for (uint32_t v = 0; v < VoicesPerCore; ++v)
        {
            Voice &voice = core.voices[v];
            const uint32_t bit = 1u << v;

            uint32_t step = std::min<uint32_t>(voice.pitch, 0x3FFFu);
            if (v > 0u && (core.pmon & bit) != 0u)
                step = static_cast<uint32_t>(std::clamp<int64_t>((static_cast<int64_t>(step) * (0x8000 + previousOutput)) >> 15, 0, 0x3FFF));
            voice.counter += step;
            while (voice.counter >= 0x1000u)
            {
                voice.counter -= 0x1000u;
                voice.previous[3] = voice.previous[2];
                voice.previous[2] = voice.previous[1];
                voice.previous[1] = voice.previous[0];
                voice.previous[0] = nextSample(core, coreIndex, v);
            }

            if (voice.envelope.phase == 0u && voice.envelope.level == 0)
            {
                voice.output = 0;
                previousOutput = 0;
                voice.left.tick();
                voice.right.tick();
                continue;
            }

            int32_t sample;
            if ((core.non & bit) != 0u)
            {
                sample = core.noiseLevel;
            }
            else
            {
                const uint32_t i = (voice.counter >> 4u) & 0xFFu;
                sample = (gauss[255u - i] * voice.previous[3] + gauss[511u - i] * voice.previous[2] +
                          gauss[256u + i] * voice.previous[1] + gauss[i] * voice.previous[0]) >> 15;
            }
            sample = clamp16((sample * voice.envelope.level) >> 15);
            tickAdsr(voice.envelope, voice.adsr1, voice.adsr2);
            voice.output = sample;
            voice.peak = std::max(voice.peak, std::abs(sample));
            previousOutput = sample;

            const int32_t left = (sample * voice.left.current) >> 15;
            const int32_t right = (sample * voice.right.current) >> 15;
            voice.left.tick();
            voice.right.tick();
            if (core.vmixl & bit) dryLeft += left;
            if (core.vmixr & bit) dryRight += right;
            if (core.vmixel & bit) wetLeft += left;
            if (core.vmixer & bit) wetRight += right;
        }

        int32_t inputLeft = 0, inputRight = 0;
        if (core.inputRunning)
        {
            if (core.inputFrame >= InputBlockFrames)
            {
                core.inputFrame = 0u;
                if (!m_inputFetch || !m_inputFetch(coreIndex, core.inputBlock.data()))
                {
                    core.inputRunning = false;
                    core.inputBlock.fill(0);
                }
            }
            inputLeft = (core.inputBlock[core.inputFrame] * core.inputLeft) >> 15;
            inputRight = (core.inputBlock[InputBlockFrames + core.inputFrame] * core.inputRight) >> 15;
            ++core.inputFrame;
        }

        const uint16_t mmix = core.mmix;
        int32_t left = ((mmix & kMixSndDryL) ? dryLeft : 0) + ((mmix & kMixExtDryL) ? externalLeft : 0) +
                       ((mmix & kMixInDryL) ? inputLeft : 0);
        int32_t right = ((mmix & kMixSndDryR) ? dryRight : 0) + ((mmix & kMixExtDryR) ? externalRight : 0) +
                        ((mmix & kMixInDryR) ? inputRight : 0);
        // The wet path feeds the reverb, which is not emulated yet: its output is silence.
        (void)kMixSndWetL;
        (void)kMixSndWetR;
        (void)kMixExtWetL;
        (void)kMixExtWetR;
        (void)kMixInWetL;
        (void)kMixInWetR;
        (void)wetLeft;
        (void)wetRight;
        outLeft = clamp16(left);
        outRight = clamp16(right);
    }

    void IopSpu2::run(uint32_t frames, std::vector<int16_t> &out)
    {
        out.reserve(out.size() + static_cast<size_t>(frames) * 2u);
        for (uint32_t f = 0; f < frames; ++f)
        {
            int32_t core0Left = 0, core0Right = 0;
            mixCore(0u, 0, 0, core0Left, core0Right);
            Core &core0 = m_cores[0];
            core0Left = clamp16((core0Left * core0.masterLeft.current) >> 15);
            core0Right = clamp16((core0Right * core0.masterRight.current) >> 15);
            core0.masterLeft.tick();
            core0.masterRight.tick();

            Core &core1 = m_cores[1];
            const int32_t externalLeft = (core0Left * core1.externalLeft) >> 15;
            const int32_t externalRight = (core0Right * core1.externalRight) >> 15;
            int32_t left = 0, right = 0;
            mixCore(1u, externalLeft, externalRight, left, right);
            left = clamp16((left * core1.masterLeft.current) >> 15);
            right = clamp16((right * core1.masterRight.current) >> 15);
            core1.masterLeft.tick();
            core1.masterRight.tick();

            out.push_back(static_cast<int16_t>(left));
            out.push_back(static_cast<int16_t>(right));
        }
        m_samples += frames;
    }
}
