#pragma once
// NATIVE RENDERER (step 3): draws the game's 3D frame from its own draw commands (PVu1DisplayList::AddCommand)
// with Vulkan, next to paraLLEl-GS. F9 switches between the two pictures (MAXIMO_NATIVE=1 starts with it on).
//
// Game thread (maximo_boot.cpp): every AddCommand is copied into a Frame (command, 8 data words, the matrix /
// vertex buffer / texture packet it points at); at sceGsSyncV the frame is handed over with the guest frame number.
// GS thread (gpu_shadow.cpp): the GIF stream is followed for texture uploads and TEX0 writes, so each texture the
// frame binds is decoded as it was when the game drew with it; at the vsync the frame is drawn and replaces the
// paraLLEl-GS scanout. No game data is stored on disk.

#include <cstdint>
#include <memory>
#include <vector>

namespace native
{
struct Cmd
{
    uint32_t cmd = 0;
    uint32_t w[8] = {};
    uint32_t blobOff = 0, blobSize = 0; // into Frame::data
};

// MODERN HUD: the player's values as the game keeps them (gPlayer's 17 game variables: 2 health, 3 armor, 4 lives,
// 5 koins, 6 souls, 7 keys, 10 shield points, 11 sword points, 14 shield capacity in fives), read at the frame's end.
struct HudState
{
    uint8_t valid = 0;     // in a level, with the player set up
    int32_t vars[17] = {};
    int32_t healthMax = 0; // a full life's health
    uint8_t boss = 0;      // a boss fight (the game's boss bar is up)
};

struct Frame
{
    uint64_t guestFrame = 0;
    HudState hud;
    std::vector<Cmd> cmds;
    std::vector<uint8_t> data;
};

// Any thread.
bool enabled();
void setEnabled(bool on);
bool shadows();          // REAL-TIME SHADOWS (F10, MAXIMO_NATIVE_SHADOWS=0 starts with them off)
void setShadows(bool on);
bool lighting();         // LIGHTING (F11, MAXIMO_NATIVE_LIGHTING=0 starts with it off)
void setLighting(bool on);

// Game thread: the frame's commands, handed over at sceGsSyncV.
void submitFrame(std::unique_ptr<Frame> frame);

// GS thread (gpu_shadow.cpp).
bool needsSeed();                     // on, and the GS memory copy not taken yet
void seed(const uint8_t *vram);       // the 4 MB of GS memory as it stands (map_vram_read), at a vsync
bool tracking();                      // following the GIF stream
void gifPacket(uint32_t path, const uint8_t *data, uint32_t size);
void endGsFrame(uint64_t guestFrame); // a vsync: this GS frame's texture binds are complete
void shutdown();                      // before the Vulkan device goes: release its images and buffers
const uint8_t *mirror();              // its copy of the GS memory (4 MB), or null: to bring paraLLEl-GS up to date
void setGsSkipped(bool skipped);      // paraLLEl-GS not drawing while the native picture is shown
} // namespace native
