#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace ps2x::iop::detail
{
    // SPU2 sound processor: 2 MB of sound RAM and two cores of 24 voices, mixed at 48 kHz.
    // PS2X_SPU2_RAM_MB (4, 8, 16 or 32) gives it more sound RAM than the PS2 has, for mods whose sound banks no
    // longer fit in 2 MB: addresses then reach the larger RAM instead of wrapping at 2 MB. Unset, it is the 2 MB chip.
    // Registers are addressed by their byte offset from 0x1F900000 (0xBF900000 through KSEG1), as the IOP sees
    // them. Sound RAM addresses are byte addresses; the registers hold halfword (16-bit word) addresses.
    class IopSpu2
    {
    public:
        static constexpr uint32_t RamBytes = 2u * 1024u * 1024u; // the PS2's; ramBytes() is this one's
        static constexpr uint32_t MaxRamBytes = 32u * 1024u * 1024u;
        static constexpr uint32_t SampleRate = 48000u;
        static constexpr uint32_t IopCyclesPerSample = 768u; // 36.864 MHz / 48 kHz
        static constexpr uint32_t VoicesPerCore = 24u;

        IopSpu2();

        void reset();
        // Resets the register file and every voice, as the SPU2's reset does, but keeps sound RAM and the sample
        // count: sceSdInit reinitialises the chip without touching the samples a driver has already transferred.
        void resetRegisters();

        [[nodiscard]] uint16_t read16(uint32_t offset) const;
        void write16(uint32_t offset, uint16_t value);

        void writeRam(uint32_t byteAddress, const uint8_t *data, size_t size);
        void readRam(uint32_t byteAddress, uint8_t *data, size_t size) const;
        void zeroRam(uint32_t byteAddress, size_t size);

        // Mixes `frames` output samples and appends them to `out` as interleaved signed 16-bit L/R.
        void run(uint32_t frames, std::vector<int16_t> &out);

        // Sound data input: a core's auto-DMA stream of PCM blocks, each 256 left then 256 right samples. While a
        // core's input runs, the mixer asks `fetch` for the next block as it reaches it (the DMA reads a block ahead
        // of the output) and adds the input through BVOL and the MMIX input gates. A false return stops the input.
        using InputFetch = std::function<bool(uint32_t core, int16_t block[512])>;
        static constexpr uint32_t InputBlockFrames = 256u;
        void setInputFetch(InputFetch fetch) { m_inputFetch = std::move(fetch); }
        void startInput(uint32_t core);
        void stopInput(uint32_t core);
        [[nodiscard]] bool inputRunning(uint32_t core) const noexcept { return m_cores[core & 1u].inputRunning; }

        [[nodiscard]] uint32_t ramBytes() const noexcept { return m_ramBytes; }
        [[nodiscard]] uint64_t samplesMixed() const noexcept { return m_samples; }
        [[nodiscard]] uint32_t activeVoices() const noexcept;
        [[nodiscard]] uint64_t keyOns() const noexcept { return m_keyOns; }
        // One line describing a voice's registers, envelope and the ADPCM block it plays next (diagnostics).
        [[nodiscard]] std::string describeVoice(uint32_t core, uint32_t voice) const;
        // The largest output (after the envelope, before volume) a voice has produced since its last key-on.
        [[nodiscard]] int32_t voicePeak(uint32_t core, uint32_t voice) const;

        // The envelope and interpolation helpers are public for unit tests.
        struct Envelope
        {
            int32_t level = 0;
            int32_t counter = 0;
            uint8_t phase = 0; // 0 off, 1 attack, 2 decay, 3 sustain, 4 release
        };
        static void tickAdsr(Envelope &envelope, uint16_t adsr1, uint16_t adsr2);
        static int16_t decodeAdpcmBlock(const uint8_t block[16], int16_t &history1, int16_t &history2,
                                        int16_t out[28]);
        [[nodiscard]] static const std::array<int16_t, 512> &interpolationTable();

    private:
        struct Volume
        {
            uint16_t reg = 0;
            int32_t current = 0; // -0x8000..0x7FFF
            int32_t counter = 0;
            void write(uint16_t value);
            void tick();
        };

        struct Voice
        {
            Volume left;
            Volume right;
            uint16_t pitch = 0;
            uint16_t adsr1 = 0;
            uint16_t adsr2 = 0;
            uint32_t ssa = 0;   // halfword addresses
            uint32_t lsax = 0;
            uint32_t block = 0; // halfword address of the current 8-halfword ADPCM block
            uint32_t counter = 0; // pitch counter, 12-bit fraction
            uint8_t sampleIndex = 0;
            uint8_t flags = 0;
            bool needFetch = true;
            bool lsaxWritten = false;
            int16_t history1 = 0;
            int16_t history2 = 0;
            std::array<int16_t, 28> decoded{};
            std::array<int16_t, 4> previous{}; // previous[0] is the newest
            Envelope envelope;
            int32_t output = 0; // last voice output (after envelope), drives pitch modulation
            int32_t peak = 0;   // largest |output| since the last key-on (diagnostics)
        };

        struct Core
        {
            std::array<Voice, VoicesPerCore> voices;
            uint32_t endx = 0;
            uint32_t pmon = 0;
            uint32_t non = 0;
            uint32_t vmixl = 0;
            uint32_t vmixr = 0;
            uint32_t vmixel = 0;
            uint32_t vmixer = 0;
            uint16_t mmix = 0;
            uint16_t attr = 0;
            Volume masterLeft;
            Volume masterRight;
            int32_t effectLeft = 0;  // EVOL
            int32_t effectRight = 0;
            int32_t externalLeft = 0; // AVOL
            int32_t externalRight = 0;
            int32_t inputLeft = 0;    // BVOL
            int32_t inputRight = 0;
            int32_t noiseLevel = 0;
            int32_t noiseCounter = 0;
            bool inputRunning = false;
            uint32_t inputFrame = InputBlockFrames; // next frame of inputBlock; a new block is due at the end
            std::array<int16_t, 512> inputBlock{};
        };

        void keyOn(Core &core, uint32_t bits);
        void keyOff(Core &core, uint32_t bits);
        int16_t nextSample(Core &core, uint32_t coreIndex, uint32_t voiceIndex);
        void fetchBlock(Voice &voice);
        void tickNoise(Core &core);
        void mixCore(uint32_t coreIndex, int32_t externalLeft, int32_t externalRight, int32_t &outLeft,
                     int32_t &outRight);
        [[nodiscard]] uint16_t voiceNax(const Voice &voice, bool high) const;

        uint32_t m_ramBytes = RamBytes;
        uint32_t m_addressMask = RamBytes / 2u - 1u; // halfword addresses
        std::vector<uint8_t> m_ram;
        std::array<uint16_t, 0x400> m_regs{}; // raw shadow of every register halfword
        std::array<Core, 2> m_cores;
        InputFetch m_inputFetch;
        uint64_t m_samples = 0;
        uint64_t m_keyOns = 0;
    };
}
