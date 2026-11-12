#pragma once

#include <cstdint>
#include <memory>
#include <vector>

class PS2Runtime;
enum class GifPathId : uint8_t;

// paraLLEl-GS fed with the ordered GS stream. As a diagnostic shadow the CPU GS stays authoritative; in GPU mode
// (enablePresentation) it supplies the window frames instead.
class GpuShadow
{
public:
    explicit GpuShadow(PS2Runtime &runtime);
    ~GpuShadow();
    GpuShadow(const GpuShadow &) = delete;
    GpuShadow &operator=(const GpuShadow &) = delete;

    void submitPacket(GifPathId path, const uint8_t *data, uint32_t size);
    // Called once per guest frame (sceGsSyncV). Runs a paraLLEl-GS vsync so its field history and frame contexts
    // advance; on a capture frame it also writes the scanout and both raw VRAM images to the current directory.
    bool onGuestFrame(uint64_t guestFrame, bool capture);
    // Same, with the GS CSR as the game saw it at the frame boundary (FIELD gives the interlace phase): a pipelined
    // VU1 worker presents the frame later, after the next VBlank may have toggled FIELD.
    bool onGuestFrame(uint64_t guestFrame, bool capture, uint64_t csr);
    uint64_t packetCount() const;

    // GPU mode: read back every guest frame's scanout for the window (takeFrame, any thread) and treat any GS input
    // that bypassed the device as fatal (onGuestFrame returns false).
    void enablePresentation();
    bool takeFrame(std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height);
    // The same, with the guest frame (the onGuestFrame number) the picture was rendered for.
    bool takeFrame(std::vector<uint8_t> &pixels, uint32_t &width, uint32_t &height, uint64_t &guestFrame);

    // GPU-mode presentation since the previous call, for the per-second log: frames published by the device,
    // frames the window took, published frames with no sample brighter than 8/255, frames without a scanout (display
    // circuits off) and device frames that failed. Brightness is max(R, G, B) over 64x48 samples of the last frame.
    struct Health
    {
        uint64_t published = 0, taken = 0, black = 0, noScanout = 0, failed = 0;
        uint32_t width = 0, height = 0, brightnessMean = 0, brightnessMax = 0;
    };
    Health takeHealth();

    // POST EFFECTS (bloom + grade on the GPU before the readback): 0 off, 1 soft, 2 strong. Any thread.
    static void setPostLevel(int level);
    static int postLevel();
    // SCENE LIGHTING (ambient occlusion from the game's Z buffer): 0 off, 1 on, 2 strong, 3 show depth, 4 show AO.
    static void setLightLevel(int level);
    static int lightLevel();

private:
    struct Impl;
    std::unique_ptr<Impl> impl;
};
