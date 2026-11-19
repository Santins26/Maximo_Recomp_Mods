#include <map>
// NATIVE RENDERER (step 3), see native_renderer.h. Ported from the offline viewer (tools/native_renderer/viewer,
// view5.py), which redrew a recorded frame to match the game's own picture.
#include "native_renderer.h"
#include "hud_atlas.h" // the modern HUD's digits and icons (tools/mkhudatlas.py)

#ifndef NATIVE_NO_VULKAN
#include "device.hpp"
#include "native_spv.h"
#endif

#include <deque>
#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <unordered_map>

namespace native
{
namespace
{
constexpr uint32_t kVramSize = 4u * 1024u * 1024u;
constexpr uint32_t kVramMask = kVramSize - 1u;

std::atomic<bool> g_enabled{[] {
    const char *v = std::getenv("MAXIMO_NATIVE");
    return v && std::atoi(v) != 0;
}()};

std::atomic<bool> g_shadows{[] {
    const char *v = std::getenv("MAXIMO_NATIVE_SHADOWS");
    return !v || std::atoi(v) != 0;
}()};
const float g_shadowStrength = [] {
    const char *v = std::getenv("MAXIMO_NATIVE_SHADOW_STRENGTH");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 1.0f) : 0.65f;
}();
const float g_shadowRange = [] {
    const char *v = std::getenv("MAXIMO_NATIVE_SHADOW_RANGE");
    return v ? std::max(20.0f, float(std::atof(v))) : 220.0f;
}();
const float g_shadowTilt = [] { // degrees the shadows' light leans away from straight down (0: as the game has it)
    const char *v = std::getenv("MAXIMO_NATIVE_SHADOW_ANGLE");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 75.0f) : 40.0f;
}();
std::atomic<bool> g_lighting{[] {
    const char *v = std::getenv("MAXIMO_NATIVE_LIGHTING");
    return !v || std::atoi(v) != 0;
}()};
const float g_specular = [] {
    const char *v = std::getenv("MAXIMO_NATIVE_SPECULAR");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 2.0f) : 0.0f;
}();
const float g_pointLights = [] { // how strongly fire and glows light their surroundings (0: not at all)
    const char *v = std::getenv("MAXIMO_NATIVE_POINT_LIGHTS");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 4.0f) : 1.4f;
}();
const float g_aoStrength = [] { // ambient occlusion (contact shadows) strength, 0 = off
    const char *v = std::getenv("MAXIMO_NATIVE_AO");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 1.0f) : 1.0f;
}();
const float g_aoRadius = [] { // in world units (Maximo is about 7 tall)
    const char *v = std::getenv("MAXIMO_NATIVE_AO_RADIUS");
    return v ? std::clamp(float(std::atof(v)), 0.1f, 20.0f) : 4.0f;
}();
constexpr size_t kMaxPointLights = 16;
const float g_lavaLight = [] { // how strongly lava lights its surroundings (0: not at all)
    const char *e = std::getenv("MAXIMO_NATIVE_LAVA_LIGHT");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_lavaGlow = [] { // how much brighter lava is drawn (1: as the game draws it)
    const char *e = std::getenv("MAXIMO_NATIVE_LAVA_GLOW");
    return e ? std::clamp(float(std::atof(e)), 0.5f, 3.0f) : 1.0f;
}();
const float g_particleBright = [] { // fire, sparks and mist brightness (their colours are in a 0..255 scale)
    const char *e = std::getenv("MAXIMO_NATIVE_PARTICLE_BRIGHT");
    return e ? std::clamp(float(std::atof(e)), 0.1f, 2.0f) : 1.0f;
}();
const float g_grass = [] { // GRASS: blades growing out of grassy ground (0 = off, 1 = normal)
    const char *e = std::getenv("MAXIMO_NATIVE_GRASS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_grassHeight = [] { // in world units (Maximo is about 7 tall)
    const char *e = std::getenv("MAXIMO_NATIVE_GRASS_HEIGHT");
    return e ? std::clamp(float(std::atof(e)), 0.1f, 6.0f) : 1.0f;
}();
const float g_relief = [] { // ROCKS AND GROUND: bumps worked out from the textures (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_RELIEF");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_moss = [] { // moss on the tops of rocks and tombstones (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_MOSS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_pebbles = [] { // stones and twigs on bare ground (0 = off, 1 = normal)
    const char *e = std::getenv("MAXIMO_NATIVE_PEBBLES");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_fog = [] { // mist lying on the ground (0 = off, 1 = normal)
    const char *e = std::getenv("MAXIMO_NATIVE_FOG");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_grime = [] { // WALLS: dirt and water streaks (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_GRIME");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_parallax = [] { // carved depth of stone and bricks (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_DEPTH");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_ivy = [] { // ivy on walls (0 = off; higher = bigger leaves)
    const char *e = std::getenv("MAXIMO_NATIVE_IVY");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_flicker = [] { // LIGHTING: how much torch and fire light flickers and sways (0 = steady)
    const char *e = std::getenv("MAXIMO_NATIVE_FLICKER");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_haze = [] { // heat haze over lava and fire (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_HAZE");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_embers = [] { // embers rising from lava (0 = off; 2 = twice as many)
    const char *e = std::getenv("MAXIMO_NATIVE_EMBERS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_rim = [] { // moonlit rim light on characters (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_RIM");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 0.35f;
}();
const float g_wisps = [] { // ATMOSPHERE: low mist drifting in wisps, parting around Maximo's legs (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_WISPS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_distHaze = [] { // far scenery fading into blue-grey mist (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_DISTANCE_HAZE");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_fallingLeaves = [] { // leaves drifting down from the trees (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_FALLING_LEAVES");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_ash = [] { // ash floating over lava (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_ASH");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_bats = [] { // bats and crows flying past (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_BATS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_cracks = [] { // SURFACES: cracks and stains on grey stone (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_CRACKS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_footprints = [] { // footprints and splashes behind Maximo in the rain (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_FOOTPRINTS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_contactShadows = [] { // CHARACTERS: a soft dark spot on the ground under each character (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_CONTACT_SHADOWS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_hitFx = [] { // sparks where blows land, dust where Maximo lands (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_HIT_FX");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_ghostTrails = [] { // see-through characters (ghosts, spirits) leave a fading trail (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_GHOST_TRAILS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 0.0f;
}();
const float g_houses = [] { // HOUSES: glow and mist in doorways, rain off the eaves, hanging vines, offerings (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_HOUSES");
    // (off by default: gates and archways passed for rooms, with rain pouring and vines hanging around them)
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 0.0f;
}();
const float g_coffins = [] { // COFFINS: green glow and mist, worn wood and iron, splinters when they burst (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_COFFINS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_models = [] { // MODELS: Maximo and the enemies rounder, shaded in their folds, with materials (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_MODELS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 1.5f) : 1.0f;
}();
const float g_eyes = [] { // enemies' eyes glowing (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_EYES");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 0.0f; // (off: not liked)
}();
const float g_hud = [] { // HUD: sharper, living orb, gleaming gold, soft shadows (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_HUD");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_props = [] { // STRUCTURES: ferns, mushrooms and old bones at the foot of walls and tombs (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_PROPS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_leaves = [] { // fallen leaves on bare ground (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_LEAVES");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_cobwebs = [] { // cobwebs where walls meet the ground (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_COBWEBS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 1.0f) : 1.0f;
}();
const float g_metal = [] { // STRUCTURES: metal shine (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_METAL");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_windows = [] { // lit windows and candles glowing and lighting around them (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_WINDOWS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_reflect = [] { // reflections in water, metal and wet ground (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_REFLECTIONS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_rain = [] { // RAIN: drops (0 = off; 2 = twice as heavy); also wets the ground
    const char *e = std::getenv("MAXIMO_NATIVE_RAIN");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
// when it rains: 0 never, 1 only in levels where the game itself seems to make it rain, 2 always in levels (default)
const int g_rainMode = [] {
    const char *e = std::getenv("MAXIMO_NATIVE_RAIN_MODE");
    if (!e)
        return 2; // (rain in every level, as it was: the game draws no rain of its own to go by)
    if (!std::strcmp(e, "always") || !std::strcmp(e, "2"))
        return 2;
    if (!std::strcmp(e, "never") || !std::strcmp(e, "0"))
        return 0;
    return 1;
}();
const float g_puddles = [] { // puddles on flat ground in the rain (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_PUDDLES");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 1.0f) : 1.0f;
}();
const float g_fireflies = [] { // fireflies (0 = off; 2 = twice as many)
    const char *e = std::getenv("MAXIMO_NATIVE_FIREFLIES");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_dust = [] { // dust drifting in the air (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_DUST");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_dof = [] { // depth of field: far scenery out of focus (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_DOF");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 0.5f;
}();
const float g_sky = [] { // SKY: clouds, stars and the moon (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_SKY");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_shafts = [] { // light shafts from the moon (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_SHAFTS");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 3.0f) : 1.0f;
}();
const float g_lightning = [] { // lightning flashes (0 = off, 1 = normal, 2 = twice as often)
    const char *e = std::getenv("MAXIMO_NATIVE_LIGHTNING");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_grade = [] { // COLOUR GRADING strength (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_GRADE");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 2.0f) : 1.0f;
}();
const float g_vignette = [] { // darker screen corners (0 = off)
    const char *e = std::getenv("MAXIMO_NATIVE_VIGNETTE");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 1.0f) : 0.18f;
}();
const float g_wind = [] { // how much grass tufts and leaves sway (0 = still)
    const char *e = std::getenv("MAXIMO_NATIVE_WIND");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 4.0f) : 1.0f;
}();
const float g_bloom = [] { // glow around bright things (fire, lava, light beams), 0 = off
    const char *v = std::getenv("MAXIMO_NATIVE_BLOOM");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 3.0f) : 0.7f;
}();
const float g_bloomThreshold = [] { // how bright (0..1) a pixel must be to glow
    const char *v = std::getenv("MAXIMO_NATIVE_BLOOM_THRESHOLD");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 1.0f) : 0.65f;
}();
const float g_sunStrength = [] { // how much brighter the sunlit side gets than the side away from it
    const char *v = std::getenv("MAXIMO_NATIVE_SUN");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 2.0f) : 0.55f;
}();
const float g_sunAmbient = [] { // how bright the side away from the sun (and the shadows) stays
    const char *v = std::getenv("MAXIMO_NATIVE_SUN_AMBIENT");
    return v ? std::clamp(float(std::atof(v)), 0.0f, 1.5f) : 0.72f;
}();
const uint32_t g_ssaa = [] { // the picture is drawn this many times wider and taller, then scaled down
    const char *v = std::getenv("MAXIMO_NATIVE_SSAA");
    return v ? std::clamp(uint32_t(std::atoi(v)), 1u, 3u) : 2u;
}();
const bool g_mipmaps = [] {
    const char *v = std::getenv("MAXIMO_NATIVE_MIPMAPS");
    return !v || std::atoi(v) != 0;
}();
const float g_anisotropy = [] {
    const char *v = std::getenv("MAXIMO_NATIVE_ANISO");
    return v ? std::clamp(float(std::atof(v)), 1.0f, 16.0f) : 8.0f;
}();
const uint32_t g_shadowSize = [] {
    const char *v = std::getenv("MAXIMO_NATIVE_SHADOW_SIZE");
    return v ? std::clamp(uint32_t(std::atoi(v)), 512u, 8192u) : 2048u;
}();

std::mutex g_frameMutex;
std::deque<std::unique_ptr<Frame>> g_frames; // newest at the back

// ---------------------------------------------------------------------------------------------------------------
// GS local memory addressing (PSMCT32 / PSMCT16 / PSMT8 / PSMT4 swizzles, as in PCSX2's GSLocalMemory tables).
const uint8_t kBlock32[4][8] = {{0, 1, 4, 5, 16, 17, 20, 21}, {2, 3, 6, 7, 18, 19, 22, 23},
                                {8, 9, 12, 13, 24, 25, 28, 29}, {10, 11, 14, 15, 26, 27, 30, 31}};
const uint8_t kCol32[8][8] = {{0, 1, 4, 5, 8, 9, 12, 13},         {2, 3, 6, 7, 10, 11, 14, 15},
                              {16, 17, 20, 21, 24, 25, 28, 29},   {18, 19, 22, 23, 26, 27, 30, 31},
                              {32, 33, 36, 37, 40, 41, 44, 45},   {34, 35, 38, 39, 42, 43, 46, 47},
                              {48, 49, 52, 53, 56, 57, 60, 61},   {50, 51, 54, 55, 58, 59, 62, 63}};
const uint8_t kBlock16[8][4] = {{0, 2, 8, 10},    {1, 3, 9, 11},    {4, 6, 12, 14},   {5, 7, 13, 15},
                                {16, 18, 24, 26}, {17, 19, 25, 27}, {20, 22, 28, 30}, {21, 23, 29, 31}};
uint8_t kCol16[8][16];
uint8_t kCol8[16][16];
uint16_t kCol4[16][32];
bool g_tablesReady = false;

void buildTables()
{
    if (g_tablesReady)
        return;
    // PSMCT16 columns: rows 0-1, then +32 for rows 2-3, +64 for rows 4-7.
    const uint8_t c16[2][16] = {{0, 2, 8, 10, 16, 18, 24, 26, 1, 3, 9, 11, 17, 19, 25, 27},
                                {4, 6, 12, 14, 20, 22, 28, 30, 5, 7, 13, 15, 21, 23, 29, 31}};
    for (int y = 0; y < 8; ++y)
        for (int x = 0; x < 16; ++x)
            kCol16[y][x] = uint8_t(c16[y & 1][x] + ((y >> 1) & 1) * 32 + (y >> 2) * 64);
    // PSMT8 columns (checked against the game's textures with the viewer): each group of 4 rows is the first
    // row pattern, rows 2-3 with the halves swapped (+1), rows 4-5 swapped (+64), rows 6-7 straight (+65); rows
    // 8-15 = rows 0-7 + 128.
    const uint8_t r0[16] = {0, 4, 16, 20, 32, 36, 48, 52, 2, 6, 18, 22, 34, 38, 50, 54};
    for (int y = 0; y < 16; ++y)
        for (int x = 0; x < 16; ++x)
        {
            const int yy = y & 7;
            const int add = (yy & 1) ? 8 : 0; // row 1 = row 0 + 8
            const int g = yy >> 1;           // 0: straight, 1: swapped +1, 2: swapped +64, 3: straight +65
            int xs = x;
            int extra = 0;
            if (g == 1 || g == 2)
                xs = (x & 8) | ((x + 4) & 7) ; // swap the two 4-entry halves within each 8
            if (g == 1)
                extra = 1;
            else if (g == 2)
                extra = 64;
            else if (g == 3)
                extra = 65;
            kCol8[y][x] = uint8_t(r0[xs] + add + extra + (y >> 3) * 128);
        }
    // PSMT4 columns (nibbles): row 0 = (j>>1)*32 + (j&1)*8 + 2*group, row 1 = +16; rows 2-3 swapped halves +1,
    // rows 4-5 swapped +128, rows 6-7 straight +129; rows 8-15 = rows 0-7 + 256.
    for (int y = 0; y < 16; ++y)
        for (int x = 0; x < 32; ++x)
        {
            const int yy = y & 7, g = yy >> 1;
            int j = x & 7;
            const int group = x >> 3;
            if (g == 1 || g == 2)
                j ^= 4;
            int v = (j >> 1) * 32 + (j & 1) * 8 + group * 2 + ((yy & 1) ? 16 : 0);
            if (g == 1)
                v += 1;
            else if (g == 2)
                v += 128;
            else if (g == 3)
                v += 129;
            kCol4[y][x] = uint16_t(v + (y >> 3) * 256);
        }
    g_tablesReady = true;
}

inline uint32_t addr32(uint32_t bp, uint32_t bw, uint32_t x, uint32_t y)
{
    const uint32_t page = (y >> 5) * std::max(bw, 1u) + (x >> 6);
    return ((bp + page * 32u + kBlock32[(y >> 3) & 3][(x >> 3) & 7]) * 256u + kCol32[y & 7][x & 7] * 4u) & kVramMask;
}
inline uint32_t addr16(uint32_t bp, uint32_t bw, uint32_t x, uint32_t y)
{
    const uint32_t page = (y >> 6) * std::max(bw, 1u) + (x >> 6);
    return ((bp + page * 32u + kBlock16[(y >> 3) & 7][(x >> 4) & 3]) * 256u + kCol16[y & 7][x & 15] * 2u) & kVramMask;
}
inline uint32_t addr8(uint32_t bp, uint32_t bw, uint32_t x, uint32_t y)
{
    const uint32_t page = (y >> 6) * std::max(bw >> 1, 1u) + (x >> 7);
    return ((bp + page * 32u + kBlock32[(y >> 4) & 3][(x >> 4) & 7]) * 256u + kCol8[y & 15][x & 15]) & kVramMask;
}
// Nibble address (byte = >> 1, high nibble when odd).
inline uint32_t addr4(uint32_t bp, uint32_t bw, uint32_t x, uint32_t y)
{
    const uint32_t page = (y >> 7) * std::max(bw >> 1, 1u) + (x >> 7);
    return ((bp + page * 32u + kBlock16[(y >> 4) & 7][(x >> 5) & 3]) * 512u + kCol4[y & 15][x & 31]) &
           (kVramMask * 2u + 1u);
}

// ---------------------------------------------------------------------------------------------------------------
// The GIF stream follower: a copy of GS memory kept up to date with every host -> local transfer, and every TEX0
// write turned into a decoded texture version.
struct TexVersion
{
    uint32_t width = 0, height = 0;
    uint32_t psm = 0xFFFFFFFFu; // the GS pixel format it was decoded from
    std::vector<uint32_t> rgba; // R in the low byte; alpha in the GS's 0..128 scale
    uint64_t hash = 0;
    uint64_t lastUsed = 0;
    // GLOWING SURFACES: how much of the texture is hot (lava-like: bright, red-orange), and the average hot colour
    float hot = 0.0f;
    float hotCol[3] = {};
    // VEGETATION: how much of it is green (grass), and how much is see-through (cut-outs: grass tufts, leaves)
    float green = 0.0f, clear = 0.0f;
    // STRUCTURES: grey (iron, stone, silver), blue (water) and warm bright (lit windows, candle flames) shares
    float grey = 0.0f, blue = 0.0f, warm = 0.0f;
    float bright = 0.0f; // share of bright pixels (a moon)
    // GRASS: 0 = not ground vegetation grows on (stone, tiles, metal, wood), 1 = partly (mossy stone, a path with
    // grass at its edges: short tufts), 2 = grassy ground (tall blades)
    uint8_t grassy = 0;
    bool lavaLike = false; // deep red-orange all over (lava, even when the game does not draw it glowing)
    bool glass = false;    // stained glass: strong colours of several different hues
    float vivid = 0.0f;    // share of strongly coloured pixels
    float glassCol[3] = {}; // its average strong colour (0..1)
#ifndef NATIVE_NO_VULKAN
    Vulkan::ImageHandle image;
#endif
};

struct Bind
{
    uint64_t key;
    uint32_t version;
};

struct GsRecord
{
    uint64_t guestFrame = 0;
    std::vector<Bind> binds;
    std::vector<uint64_t> sortedKeys; // for matching
};

struct Tracker
{
    bool seeded = false;
    std::vector<uint8_t> vram;
    uint32_t pageGen[kVramSize / 8192] = {};
    uint32_t gen = 1;
    uint64_t bitbltbuf = 0, trxpos = 0, trxreg = 0, texa = 0x0000008000000080ull;
    // host -> local transfer in progress
    bool xfer = false;
    std::vector<uint8_t> xferBuf; // the whole upload, written when complete
    uint32_t xferBytes = 0;
    struct Path
    {
        uint32_t nloop = 0, nreg = 0, cur = 0, flg = 0, skip = 0, listLeft = 0;
        uint64_t regs = 0;
    } paths[4];
    std::vector<Bind> binds;
    std::deque<GsRecord> records;
    struct KeyState
    {
        uint32_t decodedGen = 0, version = 0;
    };
    std::unordered_map<uint64_t, KeyState> keys;
    std::unordered_map<uint32_t, TexVersion> versions;
    std::unordered_map<uint64_t, uint32_t> byHash;
    uint32_t nextVersion = 1;
    uint64_t frameNo = 0;
    // statistics for the log
    uint64_t statUploads = 0, statDecodes = 0, statUnsupported = 0, statLocalCopies = 0;
    double statStreamMs = 0.0;

    void reset()
    {
        seeded = false;
        vram.clear();
        vram.shrink_to_fit();
        binds.clear();
        records.clear();
        keys.clear();
        versions.clear();
        byHash.clear();
        byRaw.clear();
        xfer = false;
        xferBuf.clear();
        for (auto &p : paths)
            p = Path{};
    }

    void touch(uint32_t byteAddr) { pageGen[(byteAddr & kVramMask) >> 13] = gen; }

    // Writes one pixel (the slow path; whole aligned uploads go block by block in commitTransfer).
    template <typename T> void store(uint32_t a, T v)
    {
        std::memcpy(&vram[a], &v, sizeof(T));
        touch(a);
    }
    void writePixel(uint32_t psm, uint32_t bp, uint32_t bw, uint32_t x, uint32_t y, uint32_t v)
    {
        switch (psm)
        {
        case 0x00: store<uint32_t>(addr32(bp, bw, x, y), v); break;
        case 0x01:
        {
            const uint32_t a = addr32(bp, bw, x, y);
            uint32_t old;
            std::memcpy(&old, &vram[a], 4);
            store<uint32_t>(a, (old & 0xFF000000u) | (v & 0xFFFFFFu));
            break;
        }
        case 0x02: case 0x0A: store<uint16_t>(addr16(bp, bw, x, y), uint16_t(v)); break;
        case 0x13: store<uint8_t>(addr8(bp, bw, x, y), uint8_t(v)); break;
        case 0x14:
        {
            const uint32_t n = addr4(bp, bw, x, y);
            const uint8_t b = vram[n >> 1];
            store<uint8_t>(n >> 1, (n & 1) ? uint8_t((b & 0x0F) | ((v & 15) << 4)) : uint8_t((b & 0xF0) | (v & 15)));
            break;
        }
        case 0x1B: store<uint8_t>(addr32(bp, bw, x, y) + 3, uint8_t(v)); break;
        case 0x24: { const uint32_t a = addr32(bp, bw, x, y) + 3; store<uint8_t>(a, uint8_t((vram[a] & 0xF0) | (v & 15))); break; }
        case 0x2C: { const uint32_t a = addr32(bp, bw, x, y) + 3; store<uint8_t>(a, uint8_t((vram[a] & 0x0F) | ((v & 15) << 4))); break; }
        default: break;
        }
    }
    uint32_t readPixel(uint32_t psm, uint32_t bp, uint32_t bw, uint32_t x, uint32_t y) const
    {
        switch (psm)
        {
        case 0x00: { uint32_t v; std::memcpy(&v, &vram[addr32(bp, bw, x, y)], 4); return v; }
        case 0x01: { uint32_t v; std::memcpy(&v, &vram[addr32(bp, bw, x, y)], 4); return v & 0xFFFFFFu; }
        case 0x02: case 0x0A: { uint16_t h; std::memcpy(&h, &vram[addr16(bp, bw, x, y)], 2); return h; }
        case 0x13: return vram[addr8(bp, bw, x, y)];
        case 0x14: { const uint32_t n = addr4(bp, bw, x, y); return (vram[n >> 1] >> ((n & 1) * 4)) & 15u; }
        case 0x1B: return vram[addr32(bp, bw, x, y) + 3];
        case 0x24: return vram[addr32(bp, bw, x, y) + 3] & 15u;
        case 0x2C: return vram[addr32(bp, bw, x, y) + 3] >> 4;
        default: return 0;
        }
    }
    static uint32_t bitsPerPixel(uint32_t psm)
    {
        switch (psm)
        {
        case 0x00: return 32;
        case 0x01: return 24;
        case 0x02: case 0x0A: return 16;
        case 0x13: case 0x1B: return 8;
        case 0x14: case 0x24: case 0x2C: return 4;
        default: return 0;
        }
    }

    void startTransfer(uint32_t dir)
    {
        if (dir == 0)
        {
            const uint32_t dpsm = uint32_t(bitbltbuf >> 56) & 0x3F;
            const uint32_t rrw = uint32_t(trxreg) & 0xFFF, rrh = uint32_t(trxreg >> 32) & 0xFFF;
            const uint32_t bpp = bitsPerPixel(dpsm);
            ++statUploads;
            xfer = false;
            if (!bpp || !rrw || !rrh)
            {
                if (!bpp)
                    ++statUnsupported;
                return;
            }
            xfer = true;
            xferBytes = uint32_t((uint64_t(rrw) * rrh * bpp + 7) / 8);
            xferBuf.clear();
            xferBuf.reserve(xferBytes);
            ++gen;
        }
        else if (dir == 2)
        {
            // local -> local (rare): pixel by pixel.
            ++gen;
            ++statLocalCopies;
            const uint32_t sbp = uint32_t(bitbltbuf) & 0x3FFF, sbw = uint32_t(bitbltbuf >> 16) & 0x3F,
                           spsm = uint32_t(bitbltbuf >> 24) & 0x3F;
            const uint32_t dbp = uint32_t(bitbltbuf >> 32) & 0x3FFF, dbw = uint32_t(bitbltbuf >> 48) & 0x3F,
                           dpsm = uint32_t(bitbltbuf >> 56) & 0x3F;
            const uint32_t ssax = uint32_t(trxpos) & 0x7FF, ssay = uint32_t(trxpos >> 16) & 0x7FF,
                           dsax = uint32_t(trxpos >> 32) & 0x7FF, dsay = uint32_t(trxpos >> 48) & 0x7FF;
            const uint32_t rrw = uint32_t(trxreg) & 0xFFF, rrh = uint32_t(trxreg >> 32) & 0xFFF;
            if (rrw * rrh > 2048u * 2048u)
                return;
            for (uint32_t y = 0; y < rrh; ++y)
                for (uint32_t x = 0; x < rrw; ++x)
                    writePixel(dpsm, dbp, dbw, (dsax + x) & 2047, (dsay + y) & 2047,
                               readPixel(spsm, sbp, sbw, (ssax + x) & 2047, (ssay + y) & 2047));
        }
        else
            xfer = false;
    }

    void imageData(const uint8_t *data, uint32_t size)
    {
        if (!xfer)
            return;
        const uint32_t n = std::min(size, xferBytes - uint32_t(xferBuf.size()));
        xferBuf.insert(xferBuf.end(), data, data + n);
        if (xferBuf.size() >= xferBytes)
        {
            commitTransfer();
            xfer = false;
        }
    }

    // The finished upload into GS memory: 8x8 (PSMCT32) or 16x16 (PSMT8) blocks at a time when it is block
    // aligned, which is how the game uploads its textures; any other shape pixel by pixel.
    void commitTransfer()
    {
        const uint32_t dbp = uint32_t(bitbltbuf >> 32) & 0x3FFF, dbw = uint32_t(bitbltbuf >> 48) & 0x3F,
                       dpsm = uint32_t(bitbltbuf >> 56) & 0x3F;
        const uint32_t dsax = uint32_t(trxpos >> 32) & 0x7FF, dsay = uint32_t(trxpos >> 48) & 0x7FF;
        const uint32_t rrw = uint32_t(trxreg) & 0xFFF, rrh = uint32_t(trxreg >> 32) & 0xFFF;
        const uint8_t *src = xferBuf.data();
        if (dpsm == 0x00 && !(dsax & 7) && !(dsay & 7) && !(rrw & 7) && !(rrh & 7) && dsax + rrw <= 2048 &&
            dsay + rrh <= 2048)
        {
            for (uint32_t by = 0; by < rrh; by += 8)
                for (uint32_t bx = 0; bx < rrw; bx += 8)
                {
                    const uint32_t base = addr32(dbp, dbw, dsax + bx, dsay + by);
                    touch(base);
                    for (uint32_t yy = 0; yy < 8; ++yy)
                    {
                        const uint8_t *row = src + (size_t(by + yy) * rrw + bx) * 4;
                        for (uint32_t xx = 0; xx < 8; ++xx)
                            std::memcpy(&vram[base + kCol32[yy][xx] * 4u], row + xx * 4, 4);
                    }
                }
            return;
        }
        if (dpsm == 0x13 && !(dsax & 15) && !(dsay & 15) && !(rrw & 15) && !(rrh & 15) && dsax + rrw <= 2048 &&
            dsay + rrh <= 2048)
        {
            for (uint32_t by = 0; by < rrh; by += 16)
                for (uint32_t bx = 0; bx < rrw; bx += 16)
                {
                    const uint32_t base = addr8(dbp, dbw, dsax + bx, dsay + by);
                    touch(base);
                    for (uint32_t yy = 0; yy < 16; ++yy)
                    {
                        const uint8_t *row = src + size_t(by + yy) * rrw + bx;
                        for (uint32_t xx = 0; xx < 16; ++xx)
                            vram[base + kCol8[yy][xx]] = row[xx];
                    }
                }
            return;
        }
        const uint32_t bpp = bitsPerPixel(dpsm);
        uint32_t i = 0;
        for (uint32_t y = 0; y < rrh; ++y)
            for (uint32_t x = 0; x < rrw; ++x)
            {
                uint32_t v = 0;
                if (bpp == 4)
                {
                    v = (src[i >> 1] >> ((i & 1) * 4)) & 15u;
                    ++i;
                }
                else
                {
                    std::memcpy(&v, src + size_t(i) * (bpp / 8), bpp / 8);
                    ++i;
                }
                writePixel(dpsm, dbp, dbw, (dsax + x) & 2047, (dsay + y) & 2047, v);
            }
    }

    uint32_t clutColour(uint32_t cpsm, uint32_t cbp, uint32_t index, bool t8) const
    {
        uint32_t x, y;
        if (t8)
        {
            const uint32_t sw = (index & 0xE7u) | ((index & 0x08u) << 1) | ((index & 0x10u) >> 1);
            x = sw & 15u;
            y = sw >> 4;
        }
        else
        {
            x = index & 7u;
            y = index >> 3;
        }
        if (cpsm == 0)
        {
            uint32_t v;
            std::memcpy(&v, &vram[addr32(cbp, 1, x, y)], 4);
            return v;
        }
        uint16_t h;
        std::memcpy(&h, &vram[addr16(cbp, 1, x, y)], 2);
        return expand16(h);
    }
    uint32_t expand16(uint16_t h) const
    {
        const uint32_t r = (h & 31u) << 3, g = ((h >> 5) & 31u) << 3, b = ((h >> 10) & 31u) << 3;
        const bool aem = (texa >> 15) & 1;
        uint32_t a = (h & 0x8000u) ? uint32_t(texa >> 32) & 0xFFu : uint32_t(texa) & 0xFFu;
        if (aem && !(h & 0x7FFFu))
            a = 0;
        return r | (g << 8) | (b << 16) | (a << 24);
    }

    // A pixel is grassy when it is green, or olive (the mossy ground of the title menu): not grey, not brown
    static bool isGrassy(int r, int g, int b)
    {
        return (g >= r + 3 && g >= b + 1 && g > 6) || (g + 6 >= r && g >= b + 10 && g > 20);
    }
    // A pixel is warm when it is bright yellow-orange: a lit window, a candle flame (not dull straw or wood)
    static bool isWarm(uint32_t r, uint32_t g, uint32_t b)
    {
        return r >= 150 && g * 100 >= r * 55 && g * 100 <= r * 95 && b * 10 <= r * 6;
    }
    // A pixel is hot when it is bright and red-orange (lava, embers), not yellow (straw, gold) or brown (wood).
    static void measureHeat(TexVersion &v)
    {
        uint64_t n = 0, hot = 0, green = 0, grey = 0, blue = 0, warm = 0, bright = 0, vivid = 0;
        double c[3] = {}, sum[3] = {}, gg = 0.0, vc[3] = {};
        uint64_t hues[6] = {};
        // (every third pixel: enough to tell, and this runs for every new texture while following the stream)
        for (size_t pi = 0; pi < v.rgba.size(); pi += 3)
        {
            const uint32_t px = v.rgba[pi];
            const uint32_t r = px & 0xFF, g = (px >> 8) & 0xFF, b = (px >> 16) & 0xFF, a = px >> 24;
            if (a < 16)
                continue;
            ++n;
            sum[0] += r;
            sum[1] += g;
            sum[2] += b;
            gg += double(g) * g;
            {
                // strong colour (stained glass): saturated and not dark, by hue
                const uint32_t mx = std::max({r, g, b}), mn = std::min({r, g, b});
                if (mx > 45 && (mx - mn) * 100 > mx * 50)
                {
                    ++vivid;
                    vc[0] += r;
                    vc[1] += g;
                    vc[2] += b;
                    int h;
                    if (mx == r)
                        h = g >= b ? (g - mn) * 2 > (mx - mn) ? 1 : 0 : (b - mn) * 2 > (mx - mn) ? 5 : 0;
                    else if (mx == g)
                        h = r >= b ? (r - mn) * 2 > (mx - mn) ? 1 : 2 : (b - mn) * 2 > (mx - mn) ? 3 : 2;
                    else
                        h = g >= r ? (g - mn) * 2 > (mx - mn) ? 3 : 4 : (r - mn) * 2 > (mx - mn) ? 5 : 4;
                    ++hues[h];
                }
            }
            if (isGrassy(int(r), int(g), int(b)))
                ++green;
            if (r * 299 + g * 587 + b * 114 > 80000) // (bright for a GS texture: 128 is full)
                ++bright;
            const uint32_t mx = std::max({r, g, b}), mn = std::min({r, g, b});
            if (mx - mn < 14 && mx > 20)
                ++grey;
            if (b > r + 10 && b + 4 >= g)
                ++blue;
            if (isWarm(r, g, b))
                ++warm;
            if (r >= 170 && g * 4 <= r * 3 && b * 5 <= r * 2)
            {
                ++hot;
                c[0] += r;
                c[1] += g;
                c[2] += b;
            }
        }
        v.hot = n ? float(double(hot) / double(n)) : 0.0f;
        v.green = n ? float(double(green) / double(n)) : 0.0f;
        v.grey = n ? float(double(grey) / double(n)) : 0.0f;
        v.blue = n ? float(double(blue) / double(n)) : 0.0f;
        v.warm = n ? float(double(warm) / double(n)) : 0.0f;
        v.bright = n ? float(double(bright) / double(n)) : 0.0f;
        v.clear = v.rgba.empty() ? 0.0f : 1.0f - float(double(n) / double((v.rgba.size() + 2) / 3));
        for (int k = 0; k < 3; ++k)
            v.hotCol[k] = hot ? float(c[k] / double(hot) / 255.0) : 0.0f;
        // Grassy ground, told apart from green-tinted stone (tiles, carved panels, teal slabs) by the whole
        // texture: grass is clearly greener than it is red or blue on average, and busy (blades, clumps), where
        // tinted stone is only a little green and smooth. Olive (the title menu's mossy ground) counts when it is
        // clearly yellow-green and busy.
        v.lavaLike = n && sum[0] / double(n) > 55.0 && sum[0] > sum[1] * 2.5 && sum[0] > sum[2] * 3.0;
        {
            int many = 0;
            for (uint64_t hc : hues)
                many += vivid && hc * 100 >= vivid * 8;
            v.glass = n && vivid * 100 >= n * 35 && many >= 3;
            v.vivid = n ? float(vivid) / float(n) : 0.0f;
            for (int k = 0; k < 3; ++k)
                v.glassCol[k] = vivid ? float(vc[k] / double(vivid) / 128.0) : 0.0f;
        }
        v.grassy = 0;
        if (n && v.green >= 0.12f)
        {
            const double R = sum[0] / double(n), G = sum[1] / double(n), B = sum[2] / double(n);
            const double sd = std::sqrt(std::max(0.0, gg / double(n) - G * G));
            const double chroma = G > 1.0 ? (G - std::max(R, B)) / G : 0.0;
            const bool olive = G > 1.0 && (G - B) / G >= 0.25 && G >= R * 0.97;
            const bool busy = G > 1.0 && sd / G >= 0.2;
            if (busy && chroma >= 0.09 && v.green >= 0.3f)
                v.grassy = 2;
            else if (busy && (chroma >= 0.03 || olive))
                v.grassy = 1;
        }
    }

    bool decode(uint64_t tex0, TexVersion &out) const
    {
        const uint32_t tbp = uint32_t(tex0) & 0x3FFF, tbw = uint32_t(tex0 >> 14) & 0x3F,
                       psm = uint32_t(tex0 >> 20) & 0x3F;
        const uint32_t tw = std::min(uint32_t(tex0 >> 26) & 15u, 10u), th = std::min(uint32_t(tex0 >> 30) & 15u, 10u);
        const uint32_t cbp = uint32_t(tex0 >> 37) & 0x3FFF, cpsm = uint32_t(tex0 >> 51) & 15u;
        const uint32_t w = 1u << tw, h = 1u << th;
        out.width = w;
        out.height = h;
        out.psm = psm;
        out.rgba.resize(size_t(w) * h);
        uint32_t *dst = out.rgba.data();
        const bool aem = (texa >> 15) & 1;
        const uint32_t ta0 = uint32_t(texa) & 0xFF;
        switch (psm)
        {
        case 0x00:
            for (uint32_t y = 0; y < h; ++y)
                for (uint32_t x = 0; x < w; ++x)
                    std::memcpy(&dst[y * w + x], &vram[addr32(tbp, tbw, x, y)], 4);
            return true;
        case 0x01:
            for (uint32_t y = 0; y < h; ++y)
                for (uint32_t x = 0; x < w; ++x)
                {
                    uint32_t v;
                    std::memcpy(&v, &vram[addr32(tbp, tbw, x, y)], 4);
                    v &= 0xFFFFFFu;
                    dst[y * w + x] = v | ((aem && !v) ? 0u : ta0 << 24);
                }
            return true;
        case 0x02: case 0x0A:
            for (uint32_t y = 0; y < h; ++y)
                for (uint32_t x = 0; x < w; ++x)
                {
                    uint16_t v;
                    std::memcpy(&v, &vram[addr16(tbp, tbw, x, y)], 2);
                    dst[y * w + x] = expand16(v);
                }
            return true;
        case 0x13: case 0x1B:
        {
            uint32_t pal[256];
            for (uint32_t i = 0; i < 256; ++i)
                pal[i] = clutColour(cpsm, cbp, i, true);
            for (uint32_t y = 0; y < h; ++y)
                for (uint32_t x = 0; x < w; ++x)
                    dst[y * w + x] = pal[readPixel(psm, tbp, tbw, x, y) & 255u];
            return true;
        }
        case 0x14: case 0x24: case 0x2C:
        {
            uint32_t pal[16];
            for (uint32_t i = 0; i < 16; ++i)
                pal[i] = clutColour(cpsm, cbp, i, false);
            for (uint32_t y = 0; y < h; ++y)
                for (uint32_t x = 0; x < w; ++x)
                    dst[y * w + x] = pal[readPixel(psm, tbp, tbw, x, y) & 15u];
            return true;
        }
        default:
            return false;
        }
    }

    // The newest write to any page the texture (or its palette) occupies, and the page range it spans.
    uint32_t newestWrite(uint64_t tex0, uint32_t &firstPage, uint32_t &lastPage) const
    {
        const uint32_t tbp = uint32_t(tex0) & 0x3FFF, tbw = uint32_t(tex0 >> 14) & 0x3F,
                       psm = uint32_t(tex0 >> 20) & 0x3F;
        const uint32_t w = 1u << std::min(uint32_t(tex0 >> 26) & 15u, 10u),
                       h = 1u << std::min(uint32_t(tex0 >> 30) & 15u, 10u);
        const uint32_t cbp = uint32_t(tex0 >> 37) & 0x3FFF;
        uint32_t newest = 0;
        firstPage = 0xFFFFFFFFu;
        lastPage = 0;
        // one probe per 8x8 pixels is finer than any format's block
        for (uint32_t y = 0; y < h; y += 8)
            for (uint32_t x = 0; x < w; x += 8)
            {
                uint32_t a;
                switch (psm)
                {
                case 0x02: case 0x0A: a = addr16(tbp, tbw, x, y); break;
                case 0x13: a = addr8(tbp, tbw, x, y); break;
                case 0x14: a = addr4(tbp, tbw, x, y) >> 1; break;
                default: a = addr32(tbp, tbw, x, y); break;
                }
                const uint32_t page = a >> 13;
                newest = std::max(newest, pageGen[page]);
                firstPage = std::min(firstPage, page);
                lastPage = std::max(lastPage, page);
            }
        newest = std::max(newest, pageGen[((cbp * 256u) & kVramMask) >> 13]);
        newest = std::max(newest, pageGen[((cbp * 256u + 1024u) & kVramMask) >> 13]);
        return newest;
    }

    // What the texture's texels come from: its pages and its palette's kilobyte, hashed (cheap next to a decode;
    // the game uploads the same textures into the same places every frame).
    uint64_t rawHash(uint64_t tex0, uint32_t, uint32_t) const
    {
        // Only the texture's own 256-byte blocks (and its palette), so a neighbour uploaded into the same page does
        // not make it look new.
        const uint32_t tbp = uint32_t(tex0) & 0x3FFF, tbw = uint32_t(tex0 >> 14) & 0x3F,
                       psm = uint32_t(tex0 >> 20) & 0x3F;
        const uint32_t w = 1u << std::min(uint32_t(tex0 >> 26) & 15u, 10u),
                       h = 1u << std::min(uint32_t(tex0 >> 30) & 15u, 10u);
        uint64_t hs = tex0 * 0x9E3779B97F4A7C15ull;
        auto mix = [&](uint32_t a, size_t n) {
            a &= kVramMask;
            if (a + n > kVramSize)
                return;
            const uint8_t *p = &vram[a];
            for (size_t i = 0; i + 8 <= n; i += 8)
            {
                uint64_t v;
                std::memcpy(&v, p + i, 8);
                hs = (hs ^ v) * 0x100000001B3ull;
                hs ^= hs >> 31;
            }
        };
        uint32_t bw = 8, bh = 8;
        switch (psm)
        {
        case 0x02: case 0x0A: bw = 16; bh = 8; break;
        case 0x13: bw = 16; bh = 16; break;
        case 0x14: bw = 32; bh = 16; break;
        default: break;
        }
        for (uint32_t y = 0; y < h; y += bh)
            for (uint32_t x = 0; x < w; x += bw)
            {
                uint32_t a;
                switch (psm)
                {
                case 0x02: case 0x0A: a = addr16(tbp, tbw, x, y); break;
                case 0x13: a = addr8(tbp, tbw, x, y); break;
                case 0x14: a = addr4(tbp, tbw, x, y) >> 1; break;
                default: a = addr32(tbp, tbw, x, y); break;
                }
                mix(a & ~255u, 256);
            }
        const uint32_t cbp = uint32_t(tex0 >> 37) & 0x3FFF;
        mix(cbp * 256u, (psm == 0x13 || psm == 0x1B) ? 1024 : 256);
        return hs;
    }

    static uint64_t keyOf(uint64_t tex0)
    {
        // TBP TBW PSM TW TH (bits 0-33), CBP CPSM CSM CSA (bits 37-60): what decides the texels.
        return tex0 & 0x1FFFFFE3FFFFFFFFull;
    }

    std::unordered_map<uint64_t, uint32_t> byRaw; // raw hash -> version

    void onTex0(uint64_t tex0)
    {
        const uint64_t key = keyOf(tex0);
        KeyState &ks = keys[key];
        uint32_t firstPage, lastPage;
        const uint32_t newest = newestWrite(tex0, firstPage, lastPage);
        if (!ks.version || newest > ks.decodedGen)
        {
            ks.decodedGen = gen;
            const uint64_t raw = rawHash(key, firstPage, lastPage);
            auto r = byRaw.find(raw);
            if (r != byRaw.end() && versions.count(r->second))
                ks.version = r->second;
            else
            {
                TexVersion v;
                if (!decode(tex0, v))
                {
                    ks.version = 0;
                    return;
                }
                ++statDecodes;
                uint64_t hsh = 1469598103934665603ull ^ (uint64_t(v.width) << 32 | v.height);
                for (uint32_t px : v.rgba)
                    hsh = (hsh ^ px) * 1099511628211ull;
                v.hash = hsh;
                measureHeat(v);
                auto it = byHash.find(hsh);
                if (it != byHash.end())
                    ks.version = it->second;
                else
                {
                    const uint32_t id = nextVersion++;
                    byHash[hsh] = id;
                    versions.emplace(id, std::move(v));
                    ks.version = id;
                }
                byRaw[raw] = ks.version;
            }
        }
        if (binds.size() < 65536)
            binds.push_back({key, ks.version});
    }

    void writeReg(uint32_t reg, uint64_t v)
    {
        switch (reg)
        {
        case 0x06: case 0x07: onTex0(v); break;
        case 0x3B: texa = v; break;
        case 0x50: bitbltbuf = v; break;
        case 0x51: trxpos = v; break;
        case 0x52: trxreg = v; break;
        case 0x53: startTransfer(uint32_t(v) & 3u); break;
        default: break;
        }
    }

    void packet(uint32_t pathIndex, const uint8_t *data, uint32_t size)
    {
        Path &p = paths[pathIndex & 3u];
        uint32_t off = 0;
        while (off < size)
        {
            if (p.listLeft)
            {
                if (off + 8 > size)
                    break;
                uint64_t v;
                std::memcpy(&v, data + off, 8);
                const uint32_t desc = uint32_t(p.regs >> (4 * p.cur)) & 15u;
                if (desc == 0x6 || desc == 0x7)
                    onTex0(v);
                off += 8;
                if (++p.cur >= p.nreg)
                    p.cur = 0;
                --p.listLeft;
                continue;
            }
            if (p.skip)
            {
                const uint32_t n = std::min(p.skip, size - off);
                p.skip -= n;
                off += n;
                continue;
            }
            if (off + 16 > size)
                break;
            uint64_t lo, hi;
            std::memcpy(&lo, data + off, 8);
            std::memcpy(&hi, data + off + 8, 8);
            if (p.nloop == 0)
            {
                p.nloop = uint32_t(lo & 0x7FFF);
                p.flg = uint32_t(lo >> 58) & 3u;
                p.nreg = uint32_t(lo >> 60) & 15u;
                if (!p.nreg)
                    p.nreg = 16;
                p.regs = hi;
                p.cur = 0;
                off += 16;
                if (p.flg == 1 && p.nloop)
                {
                    p.listLeft = p.nloop * p.nreg;
                    p.skip = (p.listLeft & 1) ? 8u : 0u; // (padding after the list, used once the list is read)
                    p.nloop = 0;
                }
                continue;
            }
            if (p.flg == 0)
            {
                const uint32_t desc = uint32_t(p.regs >> (4 * p.cur)) & 15u;
                if (desc == 0xE)
                    writeReg(uint32_t(hi & 0xFF), lo);
                else if (desc == 0x6 || desc == 0x7) // TEX0 as a packed register
                    onTex0(lo);
                off += 16;
                if (++p.cur >= p.nreg)
                {
                    p.cur = 0;
                    --p.nloop;
                }
            }
            else
            {
                const uint32_t n = std::min(p.nloop, (size - off) / 16);
                imageData(data + off, n * 16);
                off += n * 16;
                p.nloop -= n;
            }
        }
    }
};

Tracker g_tracker; // GS thread only
std::unordered_map<uint64_t, uint32_t> g_identityVersion; // texture id / packet address -> last version it had
// How often each version was resolved for a texture (id / packet + key): the same texture shows the same image every
// frame, so the most frequent one wins and a frame that pairs a bind with the wrong upload does not flash.
std::unordered_map<uint64_t, std::unordered_map<uint32_t, float>> g_identityVotes;
std::unordered_map<uint64_t, uint32_t> g_identityShown; // the image each texture identity is showing
std::atomic<bool> g_gsSkipped{false}; // paraLLEl-GS is not drawing (the native renderer has the screen)
double g_vegMs = 0.0; // time spent on the vegetation (stats)

// ---------------------------------------------------------------------------------------------------------------
// Frame -> triangles.
struct Mat
{
    float m[16];
    static Mat identity()
    {
        Mat r{};
        r.m[0] = r.m[5] = r.m[10] = r.m[15] = 1.0f;
        return r;
    }
};
Mat mul(const Mat &a, const Mat &b)
{
    Mat r;
    for (int i = 0; i < 4; ++i)
        for (int j = 0; j < 4; ++j)
            r.m[i * 4 + j] = a.m[i * 4 + 0] * b.m[0 * 4 + j] + a.m[i * 4 + 1] * b.m[1 * 4 + j] +
                             a.m[i * 4 + 2] * b.m[2 * 4 + j] + a.m[i * 4 + 3] * b.m[3 * 4 + j];
    return r;
}
inline void xform(const float v[4], const Mat &M, float out[4])
{
    for (int j = 0; j < 4; ++j)
        out[j] = v[0] * M.m[j] + v[1] * M.m[4 + j] + v[2] * M.m[8 + j] + v[3] * M.m[12 + j];
}

struct Vtx
{
    float pos[4];
    float uv[2];
    float col[4];
    float world[3]; // for the real-time shadows
    float normal[3]; // world space: only surfaces facing the light take the new shadows
    float raw[3];    // the vertex colour before the game's lighting (per-pixel lighting, F11)
    float mnormal[3]; // the normal as the game lights it (model space)
};
std::vector<Vtx> g_vertPool; // the vertex list's memory, kept from frame to frame
// GRASS: the spots where something grows, per ground triangle (by its corners and texture), worked out once
struct GrassSpot
{
    float u, v;    // where on the triangle (the weights of its 2nd and 3rd corner)
    uint32_t seed; // what grows there: its own random numbers
};
struct GrassChunk
{
    float c[3], r;          // a ball around these spots
    uint32_t first, count;  // which spots
};
struct GrassTri
{
    std::vector<GrassSpot> spots;   // grouped by place, chunk by chunk
    std::vector<GrassChunk> chunks; // so the spots far from the camera are skipped a chunk at a time
    uint64_t lastUsed = 0;
    uint64_t born = 0; // the frame it came into view (again): what grows on it grows in from then
};
// the spots of a (big) triangle sorted into 4-unit cells, one chunk per cell
void buildChunks(GrassTri &gt, const float a[3], const float e1[3], const float e2[3])
{
    auto pos = [&](const GrassSpot &s, float p[3]) {
        for (int q = 0; q < 3; ++q)
            p[q] = a[q] + e1[q] * s.u + e2[q] * s.v;
    };
    auto key = [&](const GrassSpot &s) {
        float p[3];
        pos(s, p);
        return (uint64_t(uint32_t(int32_t(std::floor(p[0] / 4.0f)) & 0x1FFFFF)) << 42) |
               (uint64_t(uint32_t(int32_t(std::floor(p[1] / 4.0f)) & 0x1FFFFF)) << 21) |
               uint64_t(uint32_t(int32_t(std::floor(p[2] / 4.0f)) & 0x1FFFFF));
    };
    std::vector<std::pair<uint64_t, GrassSpot>> keyed;
    keyed.reserve(gt.spots.size());
    for (const GrassSpot &s : gt.spots)
        keyed.push_back({key(s), s});
    std::stable_sort(keyed.begin(), keyed.end(), [](const auto &x, const auto &y) { return x.first < y.first; });
    gt.chunks.clear();
    for (size_t i = 0; i < keyed.size();)
    {
        size_t j = i;
        double c[3] = {};
        while (j < keyed.size() && keyed[j].first == keyed[i].first)
        {
            float p[3];
            pos(keyed[j].second, p);
            for (int q = 0; q < 3; ++q)
                c[q] += p[q];
            gt.spots[j] = keyed[j].second;
            ++j;
        }
        GrassChunk ch;
        for (int q = 0; q < 3; ++q)
            ch.c[q] = float(c[q] / double(j - i));
        ch.r = 0.0f;
        for (size_t k = i; k < j; ++k)
        {
            float p[3];
            pos(gt.spots[k], p);
            const float dx = p[0] - ch.c[0], dy = p[1] - ch.c[1], dz = p[2] - ch.c[2];
            ch.r = std::max(ch.r, std::sqrt(dx * dx + dy * dy + dz * dz));
        }
        ch.first = uint32_t(i);
        ch.count = uint32_t(j - i);
        gt.chunks.push_back(ch);
        i = j;
    }
}
std::unordered_map<uint64_t, GrassTri> g_grassCache;

enum class OpType : uint8_t
{
    Draw,
    ClearDepth
};
struct Op
{
    OpType type = OpType::Draw;
    int target = 0; // 0 main, n > 0: offscreen target n - 1
    uint32_t first = 0, count = 0;
    uint32_t version = 0;   // texture version, 0 none
    int rtTexture = -1;     // offscreen target sampled as the texture
    bool textured = false, abe = false, zwrite = true, clampUV = false;
    uint32_t alphaReg = 0x44, fix = 0x80;
    uint64_t test = 0x50000;
    int viewId = -1;          // which distinct view matrix (the main camera, the HUD's, the sky's...)
    bool special = false;     // shadow-map ground pass or reflection pass (neither casts shadows)
    bool casts = false, receives = false;
    bool lit = false;          // has the game's lights (LD / LC below), for per-pixel lighting
    bool shiny = false;        // lit by real directional lights (characters, objects): gets highlights
    bool emissive = false;     // gives off light (glows, fire, lava): no sun, shadows or point lights on it
    bool lava = false;         // a glowing surface (lava): drawn a little brighter, and lights what is around it
    float shell = 0.0f;        // (unused: grass layers, replaced by blades)
    float relief = 0.0f;       // ROCKS AND GROUND: bumps from the texture, this strong
    float moss = 0.0f;         // moss on what faces up, this strong
    bool scrolls = false;      // its texture slides (the game's ST animation: water, lava, clouds)
    float metal = 0.0f;        // STRUCTURES: metal shine (armour, gates, fences), this strong
    float glow = 0.0f;         // windows and candles giving off light, this strong
    float reflect = 0.0f;      // how much it reflects (water)
    bool generated = false;    // added by this renderer (grass, stones, ivy, rain...), not drawn by the game
    bool skinned = false;      // moved by bones: a character (Maximo, an enemy)
    float dry = 0.0f;          // under cover (a roof, a cave): no rain wetness or puddles on it (0..1)
    float look[4] = {};        // what it is, for the shader (see native.frag: HUD piece, its shadow, orb, characters)
    bool moves = false;        // an object that moved lately (a gate opening, a coffin lid): no surface effects laid
                               // out in the world's space on it (moss, cracks, grime, rain trickles, plants), which
                               // would slide over it as it moves
    float LD[16] = {}, LC[16] = {};
};
struct Offscreen
{
    uint32_t fbp = 0, width = 128, height = 128;
};
struct DrawList
{
    std::vector<Vtx> verts;
    std::vector<Op> ops;
    std::vector<Offscreen> targets;
    uint32_t missingTextures = 0;
    std::vector<Mat> views;      // distinct view matrices, by viewId
    std::vector<Mat> viewScreens; // the screen matrix last used with each view
    int mainView = -1;           // the camera most of the scene is drawn with
    bool haveLight = false;      // the game's shadow pass gave a light direction
    float lightDir[3] = {0, -1, 0};
    float camPos[3] = {}, camFwd[3] = {0, 0, 1};
    // POINT LIGHTS: fire, torches, glows (additive draws of the main camera), strongest first
    struct PointLight
    {
        float pos[3];
        float radius;
        float col[3];
        float weight;
        float flicker = 9.0f; // how fast it flickers (fire fast, lava slowly)
    };
    std::vector<PointLight> lights;
    // the height of the ground around Maximo (for the ground fog)
    bool haveGround = false;
    // in a level (not the title menu or other front-end screens): the game sends the camera's screen matrix or
    // Maximo's shadow map there every frame, and neither in the menu. Rain, wet ground and the added sky only there.
    bool inLevel = false;
    float groundY = 0.0f;
    std::vector<std::array<float, 3>> flames; // candle flames added by the props (tips of the floor candles)
    float indoor = 0.0f;        // Maximo under a roof or a high ceiling (0..1): less haze and mist
    uint32_t rainParticles = 0; // the game's own rain this frame: many small particles over a wide area
    bool raining = false;       // RAIN on (see g_rainMode)
    float mistY = 0.0f;         // the ground mist's height: the ground, followed smoothly
    bool havePlayer = false;
    float player[3] = {}; // Maximo's feet (the character in the middle of the view)
    // the game's own moon (a bright texture in the sky): where it is on screen, so the added one is not drawn too
    bool gameMoon = false;
    float gameMoonUV[2] = {};
};

// Persistent between frames: matrices the game sets once (the screen matrix).
struct State
{
    // (the screen matrix the game uses in its levels, until it sends one: the title menu never does, and without it
    // the native renderer could not be turned on there)
    Mat world = Mat::identity(), view = Mat::identity(), screen = defaultScreen();
    bool haveScreen = true;
    static Mat defaultScreen()
    {
        Mat m = Mat::identity();
        m.m[0] = 270.0f;
        m.m[5] = 169.2f;
        m.m[10] = 0.0f;
        m.m[8] = 2048.0f;
        m.m[9] = 2048.0f;
        m.m[10] = -3256.0f;
        m.m[11] = 1.0f;
        m.m[14] = 16777216.0f;
        m.m[15] = 0.0f;
        return m;
    }
} g_state;

Mat matAt(const Frame &f, const Cmd &c)
{
    Mat r = Mat::identity();
    if (c.blobSize >= 64)
        std::memcpy(r.m, f.data.data() + c.blobOff, 64);
    return r;
}

// Which texture version each bind of the native frame gets: per key, the n-th change of texture in the frame
// takes the n-th distinct version the GS saw bound under that key in the same frame.
struct Resolver
{
    std::unordered_map<uint64_t, std::vector<uint32_t>> seq; // key -> distinct consecutive versions
    std::unordered_map<uint64_t, std::pair<uint64_t, uint32_t>> cursor; // key -> (last identity, index)
    // The matched GS frame first; a key it lacks is looked up in the other kept GS frames (newest first), then
    // in the version the key last had (a texture whose TEX0 the GS stream did not repeat this frame).
    const std::deque<GsRecord> *records = nullptr;
    size_t primary = 0;
    const std::unordered_map<uint64_t, Tracker::KeyState> *latest = nullptr;
    static void addRecord(std::unordered_map<uint64_t, std::vector<uint32_t>> &into, const GsRecord &r)
    {
        for (const Bind &b : r.binds)
        {
            auto &v = into[b.key];
            if (b.version && (v.empty() || v.back() != b.version))
                v.push_back(b.version);
        }
    }
    explicit Resolver(const GsRecord &r) { addRecord(seq, r); }
    Resolver(const std::deque<GsRecord> &recs, size_t primaryIndex,
             const std::unordered_map<uint64_t, Tracker::KeyState> &keys)
        : records(&recs), primary(primaryIndex), latest(&keys)
    {
        addRecord(seq, recs[primaryIndex]);
    }
    std::unordered_map<uint64_t, std::vector<uint32_t>> others;
    bool othersBuilt = false;
    uint32_t fallback(uint64_t key)
    {
        if (records && !othersBuilt)
        {
            othersBuilt = true;
            for (size_t i = records->size(); i-- > 0;)
                if (i != primary)
                {
                    std::unordered_map<uint64_t, std::vector<uint32_t>> one;
                    addRecord(one, (*records)[i]);
                    for (auto &kv : one)
                        if (!others.count(kv.first))
                            others[kv.first] = kv.second;
                }
        }
        auto o = others.find(key);
        if (o != others.end() && !o->second.empty())
            return o->second.back();
        if (latest)
        {
            auto k = latest->find(key);
            if (k != latest->end())
                return k->second.version;
        }
        return 0;
    }
    uint32_t resolve(uint64_t key, uint64_t identity)
    {
        auto it = seq.find(key);
        if (it == seq.end() || it->second.empty())
            return fallback(key);
        auto c = cursor.find(key);
        uint32_t index = 0;
        if (c == cursor.end())
            cursor[key] = {identity, 0};
        else
        {
            if (c->second.first != identity)
            {
                c->second.first = identity;
                c->second.second++;
            }
            index = c->second.second;
        }
        return it->second[std::min<size_t>(index, it->second.size() - 1)];
    }
};

struct P
{
    float clip[4];
    float uv[2];
    float col[4];
    float world[3];
    float normal[3];
    float raw[3];
    float mnormal[3];
    bool adc;
};

// VEGETATION: grass grows out of grassy ground, and grass tufts and leaves sway in the wind.
// Grass: blades (thin tapered triangles) scattered over the ground draws whose texture is mostly green, near the
// camera, only where the ground's own pixel is green; coloured like the ground under them, darker at the root.
// Wind: the cut-outs (alpha-tested tufts, leaves, hanging roots) bend, more towards their top.
// SKY: the game's own moon, if it draws one: the sky draw (another camera than the main one) whose texture is
// mostly bright. Its middle on screen becomes the moon's place (light shafts, reflections) and the added moon is
// left out, so there is one moon.
void findGameMoon(DrawList &out)
{
    float best = 0.0f;
    for (const Op &op : out.ops)
    {
        if (op.type != OpType::Draw || op.target != 0 || op.viewId == out.mainView || !op.textured || !op.version ||
            op.count < 3)
            continue;
        const auto it = g_tracker.versions.find(op.version);
        if (it == g_tracker.versions.end() || it->second.bright < 0.25f)
            continue;
        double sx = 0.0, sy = 0.0, n = 0.0;
        for (uint32_t i = op.first; i < op.first + op.count; ++i)
        {
            const Vtx &v = out.verts[i];
            if (v.pos[3] <= 1e-3f)
                continue;
            sx += v.pos[0] / v.pos[3];
            sy += v.pos[1] / v.pos[3];
            n += 1.0;
        }
        if (n < 3.0)
            continue;
        // (a moon is a compact shape on screen, not the whole sky dome)
        float lo[2] = {1e9f, 1e9f}, hi[2] = {-1e9f, -1e9f};
        for (uint32_t i = op.first; i < op.first + op.count; ++i)
        {
            const Vtx &v = out.verts[i];
            if (v.pos[3] <= 1e-3f)
                continue;
            const float x = v.pos[0] / v.pos[3], y = v.pos[1] / v.pos[3];
            lo[0] = std::min(lo[0], x);
            hi[0] = std::max(hi[0], x);
            lo[1] = std::min(lo[1], y);
            hi[1] = std::max(hi[1], y);
        }
        const float cx = float(sx / n), cy = float(sy / n);
        if (hi[0] - lo[0] > 3.0f || hi[1] - lo[1] > 3.0f || std::fabs(cx) > 1.6f || std::fabs(cy) > 1.6f)
            continue;
        const float score = it->second.bright * float(op.count);
        if (score > best)
        {
            best = score;
            out.gameMoon = true;
            out.gameMoonUV[0] = float(sx / n) * 0.5f + 0.5f;
            out.gameMoonUV[1] = float(sy / n) * 0.5f + 0.5f;
        }
    }
}

// RAIN, FIREFLIES AND DUST (F11): drawn after the main camera's last draw (before the HUD), see-through and
// lit by nothing (they give off their own light, added on top):
// rain: thin streaks falling at a slant in a box around the camera, each drop fixed in the world (the box wraps as
// the camera moves), fading out towards the top of the streak; splashes (added by the pebbles, on bare ground);
// fireflies: soft yellow-green points drifting and blinking a little above the ground near Maximo;
// dust: faint specks drifting slowly in the air around him.
std::vector<Vtx> g_weatherVerts; // the splashes, gathered by addVegetation

// RAIN SHELTER: the highest solid surface over each 1-unit cell of a square around the camera (the ground in the
// open; a roof, a cave's ceiling or a tree's leaves where there is cover). A raindrop below it is under cover and
// not drawn, a splash only where its spot is that highest surface.
struct RoofGrid
{
    static constexpr int kN = 48;      // cells each way
    static constexpr float kCell = 1.0f;
    float x0 = 0.0f, z0 = 0.0f;
    float h[kN * kN];
    bool valid = false;
    float at(float x, float z) const
    {
        const int ix = int(std::floor((x - x0) / kCell)), iz = int(std::floor((z - z0) / kCell));
        if (!valid || ix < 0 || iz < 0 || ix >= kN || iz >= kN)
            return -1e30f;
        return h[iz * kN + ix];
    }
};
RoofGrid g_roof;

void buildRoof(const DrawList &out, const float centre[3])
{
    RoofGrid &g = g_roof;
    g.valid = false;
    g.x0 = std::floor((centre[0] - RoofGrid::kN * 0.5f * RoofGrid::kCell));
    g.z0 = std::floor((centre[2] - RoofGrid::kN * 0.5f * RoofGrid::kCell));
    std::fill(std::begin(g.h), std::end(g.h), -1e30f);
    const float x1 = g.x0 + RoofGrid::kN * RoofGrid::kCell, z1 = g.z0 + RoofGrid::kN * RoofGrid::kCell;
    for (const Op &op : out.ops)
    {
        if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.generated || op.shiny ||
            op.emissive || (op.abe && !(op.test & 1)))
            continue;
        for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
        {
            const Vtx &a = out.verts[i], &b = out.verts[i + 1], &c = out.verts[i + 2];
            const float lx = std::min({a.world[0], b.world[0], c.world[0]}), hx = std::max({a.world[0], b.world[0], c.world[0]});
            const float lz = std::min({a.world[2], b.world[2], c.world[2]}), hz = std::max({a.world[2], b.world[2], c.world[2]});
            if (hx < g.x0 || lx > x1 || hz < g.z0 || lz > z1)
                continue;
            const int ix0 = std::max(0, int(std::floor((lx - g.x0) / RoofGrid::kCell)));
            const int ix1 = std::min(RoofGrid::kN - 1, int(std::floor((hx - g.x0) / RoofGrid::kCell)));
            const int iz0 = std::max(0, int(std::floor((lz - g.z0) / RoofGrid::kCell)));
            const int iz1 = std::min(RoofGrid::kN - 1, int(std::floor((hz - g.z0) / RoofGrid::kCell)));
            const float det = (b.world[2] - c.world[2]) * (a.world[0] - c.world[0]) +
                              (c.world[0] - b.world[0]) * (a.world[2] - c.world[2]);
            if (ix1 - ix0 <= 1 && iz1 - iz0 <= 1)
            {
                // small (or a wall seen from above): its corners into their cells
                for (const Vtx *v : {&a, &b, &c})
                {
                    const int ix = int(std::floor((v->world[0] - g.x0) / RoofGrid::kCell));
                    const int iz = int(std::floor((v->world[2] - g.z0) / RoofGrid::kCell));
                    if (ix >= 0 && iz >= 0 && ix < RoofGrid::kN && iz < RoofGrid::kN)
                        g.h[iz * RoofGrid::kN + ix] = std::max(g.h[iz * RoofGrid::kN + ix], v->world[1]);
                }
                continue;
            }
            if (std::fabs(det) < 1e-6f)
                continue; // a wall: its top is some other triangle
            const float inv = 1.0f / det;
            for (int iz = iz0; iz <= iz1; ++iz)
                for (int ix = ix0; ix <= ix1; ++ix)
                {
                    const float px = g.x0 + (float(ix) + 0.5f) * RoofGrid::kCell, pz = g.z0 + (float(iz) + 0.5f) * RoofGrid::kCell;
                    const float l0 = ((b.world[2] - c.world[2]) * (px - c.world[0]) + (c.world[0] - b.world[0]) * (pz - c.world[2])) * inv;
                    const float l1 = ((c.world[2] - a.world[2]) * (px - c.world[0]) + (a.world[0] - c.world[0]) * (pz - c.world[2])) * inv;
                    const float l2 = 1.0f - l0 - l1;
                    if (l0 < -0.05f || l1 < -0.05f || l2 < -0.05f)
                        continue;
                    const float y = a.world[1] * l0 + b.world[1] * l1 + c.world[1] * l2;
                    float &cell = g.h[iz * RoofGrid::kN + ix];
                    cell = std::max(cell, y);
                }
        }
    }
    g.valid = true;
}

// CHARACTERS: the main camera's draws moved by bones, gathered into one box per character (the parts of one
// body: its overlapping or touching pieces)
struct CharBlob
{
    float lo[3], hi[3];
    uint32_t verts = 0, seeThrough = 0; // (how many of its points are drawn see-through: a ghost)
};
std::vector<CharBlob> findCharacters(const DrawList &out)
{
    std::vector<CharBlob> blobs;
    for (const Op &op : out.ops)
    {
        if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || !op.skinned || op.generated ||
            op.count < 3)
            continue;
        CharBlob b;
        for (int k = 0; k < 3; ++k)
        {
            b.lo[k] = 1e30f;
            b.hi[k] = -1e30f;
        }
        for (uint32_t i = op.first; i < op.first + op.count; ++i)
            for (int k = 0; k < 3; ++k)
            {
                b.lo[k] = std::min(b.lo[k], out.verts[i].world[k]);
                b.hi[k] = std::max(b.hi[k], out.verts[i].world[k]);
            }
        if (b.hi[0] - b.lo[0] > 20.0f || b.hi[2] - b.lo[2] > 20.0f)
            continue; // (not a character: something huge)
        b.verts = op.count;
        b.seeThrough = op.abe ? op.count : 0;
        blobs.push_back(b);
    }
    // merge the boxes that touch (within half a unit), until none do
    bool merged = true;
    while (merged)
    {
        merged = false;
        for (size_t i = 0; i < blobs.size() && !merged; ++i)
            for (size_t j = i + 1; j < blobs.size(); ++j)
            {
                CharBlob &a = blobs[i], &b = blobs[j];
                bool touch = true;
                for (int k = 0; k < 3; ++k)
                    touch = touch && a.lo[k] - 0.5f <= b.hi[k] && b.lo[k] - 0.5f <= a.hi[k];
                if (!touch)
                    continue;
                for (int k = 0; k < 3; ++k)
                {
                    a.lo[k] = std::min(a.lo[k], b.lo[k]);
                    a.hi[k] = std::max(a.hi[k], b.hi[k]);
                }
                a.verts += b.verts;
                a.seeThrough += b.seeThrough;
                blobs.erase(blobs.begin() + long(j));
                merged = true;
                break;
            }
    }
    return blobs;
}

// THE FLOOR around the camera: in cells of one unit, the heights of the up-facing level surfaces there (up to four
// each: a floor, a step, a tomb's top...), to stand props on the ground and not bury them or float them.
struct FloorGrid
{
    static constexpr int kN = 128;
    static constexpr int kMax = 4;
    float x0 = 0.0f, z0 = 0.0f;
    float h[kN * kN][kMax];
    uint8_t n[kN * kN];
    float lavaY[kN * kN]; // the lava (or water) surface's height there (-1e30: none)
    bool anyLava = false;
    std::vector<std::array<float, 3>> hotCells; // lava proper (not water) near the middle: where embers rise from
    uint32_t buildId = 0;                       // (counts the rebuilds)
    bool valid = false;
    // lava at or just over this point, or a little under it (in it, or poking up through it): nothing grows there
    bool inLava(float x, float z, float y) const
    {
        if (!valid || !anyLava)
            return false;
        const int ix = int(std::floor(x - x0)), iz = int(std::floor(z - z0));
        if (ix < 0 || iz < 0 || ix >= kN || iz >= kN)
            return false;
        const float l = lavaY[iz * kN + ix]; // (spread a cell each way when built)
        return l >= y - 6.0f && l <= y + 4.0f;
    }
    float liquidAt(float x, float z) const
    {
        const int ix = int(std::floor(x - x0)), iz = int(std::floor(z - z0));
        if (!valid || ix < 0 || iz < 0 || ix >= kN || iz >= kN)
            return -1e30f;
        return lavaY[iz * kN + ix];
    }
    // the floor height nearest to y within [y - below, y + above], or -1e30 when there is none
    float heightNear(float x, float z, float y, float below, float above) const
    {
        const int ix = int(std::floor(x - x0)), iz = int(std::floor(z - z0));
        if (!valid || ix < 0 || iz < 0 || ix >= kN || iz >= kN)
            return -1e30f;
        const int c = iz * kN + ix;
        float best = -1e30f, bd = 1e30f;
        for (int k = 0; k < n[c]; ++k)
        {
            const float d = h[c][k] - y;
            if (d >= -below && d <= above && std::fabs(d) < bd)
            {
                bd = std::fabs(d);
                best = h[c][k];
            }
        }
        return best;
    }
    void add(int c, float y)
    {
        for (int k = 0; k < n[c]; ++k)
            if (std::fabs(h[c][k] - y) < 0.25f)
            {
                h[c][k] = std::max(h[c][k], y);
                return;
            }
        if (n[c] < kMax)
            h[c][n[c]++] = y;
    }
};
FloorGrid g_floor;

// TREES around the camera: in cells of one unit, the lowest branch (or the game's own leaves) above, spread out
// three cells each way, so dead leaves lie on the ground under trees, and nowhere else (not in caves, not on open
// paths).
struct CanopyGrid
{
    static constexpr int kN = 96;
    float x0 = 0.0f, z0 = 0.0f;
    float lowY[kN * kN];
    bool valid = false;
    float at(float x, float z) const
    {
        const int ix = int(std::floor(x - x0)), iz = int(std::floor(z - z0));
        if (!valid || ix < 0 || iz < 0 || ix >= kN || iz >= kN)
            return 1e30f;
        return lowY[iz * kN + ix];
    }
};
CanopyGrid g_canopy;

void buildCanopy(const DrawList &out, const float centre[3], const std::unordered_map<uint32_t, TexVersion> &versions)
{
    CanopyGrid &g = g_canopy;
    constexpr int N = CanopyGrid::kN;
    g.x0 = std::floor(centre[0] - N * 0.5f);
    g.z0 = std::floor(centre[2] - N * 0.5f);
    std::fill(std::begin(g.lowY), std::end(g.lowY), 1e30f);
    for (const Op &op : out.ops)
    {
        if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.generated || op.emissive ||
            op.abe || !(op.test & 1) || !op.textured || !op.version)
            continue;
        const auto it = versions.find(op.version);
        if (it == versions.end())
            continue;
        const TexVersion &tv = it->second;
        // bare branches (grey cut-outs) or leaves (red-brown cut-outs), not tufts, fences or roots
        if (tv.clear < 0.5f || tv.clear > 0.9f || tv.width < 128 || tv.green >= 0.3f)
            continue;
        bool leafy = false;
        if (tv.grey < 0.6f)
        {
            uint64_t n = 0;
            double r = 0.0, gsum = 0.0;
            for (size_t i = 0; i < tv.rgba.size(); i += 7)
                if ((tv.rgba[i] >> 24) >= 16)
                {
                    r += tv.rgba[i] & 0xFF;
                    gsum += (tv.rgba[i] >> 8) & 0xFF;
                    ++n;
                }
            leafy = n && r > gsum * 1.35;
            if (!leafy)
                continue;
        }
        for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
        {
            const Vtx &a = out.verts[i], &b = out.verts[i + 1], &c = out.verts[i + 2];
            const float ly = std::min({a.world[1], b.world[1], c.world[1]});
            const int ix0 = std::max(0, int(std::floor(std::min({a.world[0], b.world[0], c.world[0]}) - g.x0)));
            const int ix1 = std::min(N - 1, int(std::floor(std::max({a.world[0], b.world[0], c.world[0]}) - g.x0)));
            const int iz0 = std::max(0, int(std::floor(std::min({a.world[2], b.world[2], c.world[2]}) - g.z0)));
            const int iz1 = std::min(N - 1, int(std::floor(std::max({a.world[2], b.world[2], c.world[2]}) - g.z0)));
            if (ix1 - ix0 > 12 || iz1 - iz0 > 12)
                continue; // (a huge card: not a branch)
            for (int iz = iz0; iz <= iz1; ++iz)
                for (int ix = ix0; ix <= ix1; ++ix)
                    g.lowY[iz * N + ix] = std::min(g.lowY[iz * N + ix], ly);
        }
    }
    // spread three cells each way (leaves blow a little out from under the branches)
    static float tmp[CanopyGrid::kN * CanopyGrid::kN];
    for (int pass = 0; pass < 2; ++pass)
    {
        const float *src = pass ? tmp : g.lowY;
        float *dst = pass ? g.lowY : tmp;
        for (int iz = 0; iz < N; ++iz)
            for (int ix = 0; ix < N; ++ix)
            {
                float m = 1e30f;
                for (int d = -3; d <= 3; ++d)
                {
                    const int jx = pass ? ix : ix + d, jz = pass ? iz + d : iz;
                    if (jx >= 0 && jz >= 0 && jx < N && jz < N)
                        m = std::min(m, src[jz * N + jx]);
                }
                dst[iz * N + ix] = m;
            }
    }
    g.valid = true;
}

void buildFloor(const DrawList &out, const float centre[3])
{
    FloorGrid &g = g_floor;
    g.x0 = std::floor(centre[0] - FloorGrid::kN * 0.5f);
    g.z0 = std::floor(centre[2] - FloorGrid::kN * 0.5f);
    std::memset(g.n, 0, sizeof(g.n));
    std::fill(std::begin(g.lavaY), std::end(g.lavaY), -1e30f);
    g.hotCells.clear();
    const float x1 = g.x0 + FloorGrid::kN, z1 = g.z0 + FloorGrid::kN;
    for (const Op &op : out.ops)
    {
        bool lavaOp = op.type == OpType::Draw && op.target == 0 && op.viewId == out.mainView && !op.generated &&
                      (op.lava || (op.textured && op.version && op.count >= 3));
        // (water counts too: the game's water grids and sliding bluish textures; not every sliding texture, the
        // drifting mist layers over the ground slide too)
        const bool liquid = lavaOp && (op.lava || op.reflect > 0.5f);
        const bool water = op.reflect > 0.5f && !op.lava;
        if (lavaOp && !liquid)
        {
            const auto vt = g_tracker.versions.find(op.version);
            lavaOp = vt != g_tracker.versions.end() && vt->second.lavaLike;
        }
        if (lavaOp)
        {
            // LAVA: its surface into the cells it covers
            for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
            {
                const Vtx &a = out.verts[i], &b = out.verts[i + 1], &c = out.verts[i + 2];
                {
                    // (lying flat: a pool or river of lava, not a red banner or a wall)
                    float e1[3], e2[3];
                    for (int k = 0; k < 3; ++k)
                    {
                        e1[k] = b.world[k] - a.world[k];
                        e2[k] = c.world[k] - a.world[k];
                    }
                    const float nx = e1[1] * e2[2] - e1[2] * e2[1], ny = e1[2] * e2[0] - e1[0] * e2[2], nz = e1[0] * e2[1] - e1[1] * e2[0];
                    if (std::fabs(ny) < 0.7f * std::sqrt(nx * nx + ny * ny + nz * nz))
                        continue;
                }
                const int ix0 = std::max(0, int(std::floor(std::min({a.world[0], b.world[0], c.world[0]}) - g.x0)));
                const int ix1 = std::min(FloorGrid::kN - 1, int(std::floor(std::max({a.world[0], b.world[0], c.world[0]}) - g.x0)));
                const int iz0 = std::max(0, int(std::floor(std::min({a.world[2], b.world[2], c.world[2]}) - g.z0)));
                const int iz1 = std::min(FloorGrid::kN - 1, int(std::floor(std::max({a.world[2], b.world[2], c.world[2]}) - g.z0)));
                const float det = (b.world[2] - c.world[2]) * (a.world[0] - c.world[0]) +
                                  (c.world[0] - b.world[0]) * (a.world[2] - c.world[2]);
                const float ymax = std::max({a.world[1], b.world[1], c.world[1]});
                for (int iz = iz0; iz <= iz1; ++iz)
                    for (int ix = ix0; ix <= ix1; ++ix)
                    {
                        float y = ymax;
                        if (std::fabs(det) > 1e-6f)
                        {
                            const float px = g.x0 + float(ix) + 0.5f, pz = g.z0 + float(iz) + 0.5f;
                            const float l0 = ((b.world[2] - c.world[2]) * (px - c.world[0]) + (c.world[0] - b.world[0]) * (pz - c.world[2])) / det;
                            const float l1 = ((c.world[2] - a.world[2]) * (px - c.world[0]) + (a.world[0] - c.world[0]) * (pz - c.world[2])) / det;
                            const float l2 = 1.0f - l0 - l1;
                            if (l0 < -0.1f || l1 < -0.1f || l2 < -0.1f)
                                continue;
                            y = a.world[1] * l0 + b.world[1] * l1 + c.world[1] * l2;
                        }
                        float &cell = g.lavaY[iz * FloorGrid::kN + ix];
                        cell = std::max(cell, y);
                        if (!water && (ix & 1) == 0 && (iz & 1) == 0)
                        {
                            const float px = g.x0 + float(ix) + 0.5f, pz = g.z0 + float(iz) + 0.5f;
                            if (std::fabs(px - centre[0]) < 36.0f && std::fabs(pz - centre[2]) < 36.0f &&
                                g.hotCells.size() < 4096)
                                g.hotCells.push_back({px, y, pz});
                        }
                    }
            }
            continue;
        }
        if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.generated || op.emissive ||
            (op.abe && !(op.test & 1)) || (op.test & 1))
            continue;
        for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
        {
            const Vtx &a = out.verts[i], &b = out.verts[i + 1], &c = out.verts[i + 2];
            const float lx = std::min({a.world[0], b.world[0], c.world[0]}), hx = std::max({a.world[0], b.world[0], c.world[0]});
            const float lz = std::min({a.world[2], b.world[2], c.world[2]}), hz = std::max({a.world[2], b.world[2], c.world[2]});
            if (hx < g.x0 || lx > x1 || hz < g.z0 || lz > z1)
                continue;
            float e1[3], e2[3];
            for (int k = 0; k < 3; ++k)
            {
                e1[k] = b.world[k] - a.world[k];
                e2[k] = c.world[k] - a.world[k];
            }
            const float nx = e1[1] * e2[2] - e1[2] * e2[1], ny = e1[2] * e2[0] - e1[0] * e2[2], nz = e1[0] * e2[1] - e1[1] * e2[0];
            const float nl = std::sqrt(nx * nx + ny * ny + nz * nz);
            if (nl < 1e-6f || std::fabs(ny) < 0.6f * nl)
                continue; // (walls)
            const int ix0 = std::max(0, int(std::floor(lx - g.x0))), ix1 = std::min(FloorGrid::kN - 1, int(std::floor(hx - g.x0)));
            const int iz0 = std::max(0, int(std::floor(lz - g.z0))), iz1 = std::min(FloorGrid::kN - 1, int(std::floor(hz - g.z0)));
            const float det = (b.world[2] - c.world[2]) * (a.world[0] - c.world[0]) +
                              (c.world[0] - b.world[0]) * (a.world[2] - c.world[2]);
            if (std::fabs(det) < 1e-6f)
                continue;
            const float inv = 1.0f / det;
            bool any = false;
            for (int iz = iz0; iz <= iz1; ++iz)
                for (int ix = ix0; ix <= ix1; ++ix)
                {
                    const float px = g.x0 + float(ix) + 0.5f, pz = g.z0 + float(iz) + 0.5f;
                    const float l0 = ((b.world[2] - c.world[2]) * (px - c.world[0]) + (c.world[0] - b.world[0]) * (pz - c.world[2])) * inv;
                    const float l1 = ((c.world[2] - a.world[2]) * (px - c.world[0]) + (a.world[0] - c.world[0]) * (pz - c.world[2])) * inv;
                    const float l2 = 1.0f - l0 - l1;
                    if (l0 < -0.05f || l1 < -0.05f || l2 < -0.05f)
                        continue;
                    g.add(iz * FloorGrid::kN + ix, a.world[1] * l0 + b.world[1] * l1 + c.world[1] * l2);
                    any = true;
                }
            if (!any) // (smaller than a cell: its corners into their cells)
                for (const Vtx *v : {&a, &b, &c})
                {
                    const int ix = int(std::floor(v->world[0] - g.x0)), iz = int(std::floor(v->world[2] - g.z0));
                    if (ix >= 0 && iz >= 0 && ix < FloorGrid::kN && iz < FloorGrid::kN)
                        g.add(iz * FloorGrid::kN + ix, v->world[1]);
                }
        }
    }
    ++g.buildId;
    // the lava spread two cells each way (its banks)
    g.anyLava = false;
    for (float l : g.lavaY)
        if (l > -1e29f)
        {
            g.anyLava = true;
            break;
        }
    if (g.anyLava)
    {
        static float tmp[FloorGrid::kN * FloorGrid::kN];
        constexpr int N = FloorGrid::kN;
        for (int pass = 0; pass < 2; ++pass)
        {
            const float *src = pass ? tmp : g.lavaY;
            float *dst = pass ? g.lavaY : tmp;
            for (int iz = 0; iz < N; ++iz)
                for (int ix = 0; ix < N; ++ix)
                {
                    float m = src[iz * N + ix];
                    for (int d = -2; d <= 2; ++d)
                    {
                        const int jx = pass ? ix : ix + d, jz = pass ? iz + d : iz;
                        if (d && jx >= 0 && jz >= 0 && jx < N && jz < N)
                            m = std::max(m, src[jz * N + jx]);
                    }
                    dst[iz * N + ix] = m;
                }
        }
    }
    g.valid = true;
}

// MODERN HUD: the game's HUD (the orb and the bars at the top left) replaced by a new one drawn from the player's own
// values (HudState, read by the game thread at the frame's end): a health bar that drains smoothly with a trail of
// the damage just taken and flashes when hit, the armor as shields, the shield points as a thin blue bar, the lives
// under a helmet, and souls, koins and keys counted at the top right. It fades away while nothing changes, comes back
// when something does (always shown with little health left), and what is picked up pops in at the side.
HudState g_hudNow;
const float g_hudCanvasW = [] { // the HUD's canvas: 720 high, as wide as the picture's shape
    const char *e = std::getenv("MAXIMO_ASPECT");
    return e && std::strcmp(e, "16:9") == 0 ? 1280.0f : 960.0f;
}();
bool g_hudMenu = false; // a menu or pause screen this frame (many 2D pieces): the new HUD steps aside
const float g_modernHud = [] { // 0 = the game's own HUD
    const char *e = std::getenv("MAXIMO_NATIVE_MODERN_HUD");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 1.0f) : 1.0f;
}();
const float g_hudAutoHide = [] { // seconds the HUD stays after a change (0 = always shown)
    const char *e = std::getenv("MAXIMO_NATIVE_HUD_HIDE");
    return e ? std::clamp(float(std::atof(e)), 0.0f, 60.0f) : 4.0f;
}();
constexpr uint32_t kHudVersion = 0xFFFFF001u; // the atlas, a texture of this renderer's own

const HudRect *hudRect(const char *name)
{
    for (const HudRect &r : kHudRects)
        if (std::strcmp(r.name, name) == 0)
            return &r;
    return nullptr;
}

void ensureHudAtlas()
{
    auto it = g_tracker.versions.find(kHudVersion);
    if (it == g_tracker.versions.end())
    {
        TexVersion v;
        v.width = kHudAtlasW;
        v.height = kHudAtlasH;
        v.rgba.resize(size_t(v.width) * v.height);
        size_t at = 0;
        for (size_t k = 0; k + 2 < sizeof(kHudAtlasRle) && at < v.rgba.size(); k += 3)
        {
            const uint32_t L = kHudAtlasRle[k + 1], A = kHudAtlasRle[k + 2];
            const uint32_t px = L | (L << 8) | (L << 16) | (A << 24);
            for (uint32_t n = 0; n < kHudAtlasRle[k] && at < v.rgba.size(); ++n)
                v.rgba[at++] = px;
        }
        v.hash = 0xC0FFEE00D15EA5E5ull;
        it = g_tracker.versions.emplace(kHudVersion, std::move(v)).first;
    }
    it->second.lastUsed = g_tracker.frameNo; // (never forgotten)
}

struct HudDraw
{
    DrawList &out;
    std::vector<Vtx> norm, add; // blended pieces, glowing (added) pieces
    float W = 1280.0f;          // the canvas: 720 high, as wide as the picture's shape
    void quadUV(std::vector<Vtx> &dst, float x, float y, float w, float h, float u0, float v0, float u1, float v1,
                const float c[4], const float c2[4] = nullptr)
    {
        const float X0 = x / W * 2.0f - 1.0f, X1 = (x + w) / W * 2.0f - 1.0f;
        const float Y0 = y / 360.0f - 1.0f, Y1 = (y + h) / 360.0f - 1.0f;
        const float P[4][4] = {{X0, Y0, u0, v0}, {X1, Y0, u1, v0}, {X1, Y1, u1, v1}, {X0, Y1, u0, v1}};
        const int idx[6] = {0, 1, 2, 0, 2, 3};
        for (int k : idx)
        {
            Vtx v{};
            v.pos[0] = P[k][0];
            v.pos[1] = P[k][1];
            v.pos[2] = 0.5f;
            v.pos[3] = 1.0f;
            v.uv[0] = P[k][2];
            v.uv[1] = P[k][3];
            const float *cc = (c2 && k >= 2) ? c2 : c; // (a second colour: the bottom edge, a gradient)
            for (int q = 0; q < 3; ++q)
                v.col[q] = v.raw[q] = cc[q] * 128.0f;
            v.col[3] = std::clamp(cc[3], 0.0f, 1.0f) * 128.0f;
            v.normal[1] = v.mnormal[1] = 1.0f;
            dst.push_back(v);
        }
    }
    void piece(const char *name, float x, float y, float w, float h, const float c[4], bool glow = false,
               const float c2[4] = nullptr, float cutL = 0.0f, float cutR = 1.0f)
    {
        const HudRect *r = hudRect(name);
        if (!r || c[3] <= 0.003f)
            return;
        const float iw = 1.0f / float(kHudAtlasW), ih = 1.0f / float(kHudAtlasH);
        const float u0 = (r->x + 0.5f + (r->w - 1.0f) * cutL) * iw, u1 = (r->x + 0.5f + (r->w - 1.0f) * cutR) * iw;
        quadUV(glow ? add : norm, x + w * cutL, y, w * (cutR - cutL), h, u0, (r->y + 0.5f) * ih, u1,
               (r->y + r->h - 0.5f) * ih, c, c2);
    }
    // a bar: round ends kept, its middle stretched (three pieces of the capsule)
    void bar(float x, float y, float w, float h, const float c[4], const float c2[4] = nullptr, bool glow = false)
    {
        const HudRect *r = hudRect("bar");
        if (!r || w <= 0.5f || c[3] <= 0.003f)
            return;
        const float iw = 1.0f / float(kHudAtlasW), ih = 1.0f / float(kHudAtlasH);
        const float cap = std::min(h * 0.5f, w * 0.5f), capU = r->h * 0.5f * iw;
        const float ux = r->x * iw, uw = r->w * iw, v0 = (r->y + 0.5f) * ih, v1 = (r->y + r->h - 0.5f) * ih;
        auto &dst = glow ? add : norm;
        quadUV(dst, x, y, cap, h, ux, v0, ux + capU, v1, c, c2);
        quadUV(dst, x + cap, y, w - 2.0f * cap, h, ux + capU, v0, ux + uw - capU, v1, c, c2);
        quadUV(dst, x + w - cap, y, cap, h, ux + uw - capU, v0, ux + uw, v1, c, c2);
    }
    // a number (digits, '+', 'x', '/'), h high; right = right-aligned at x. Returns its width.
    float text(const char *s, float x, float y, float h, const float c[4], bool right = false)
    {
        static const char kGlyphs[] = "0123456789+x/";
        const float sc = h / 64.0f;
        float wsum = 0.0f;
        for (const char *p = s; *p; ++p)
            if (const char *g = std::strchr(kGlyphs, *p))
                wsum += kHudGlyphAdvance[g - kGlyphs] * sc;
        float cx = right ? x - wsum : x;
        for (const char *p = s; *p; ++p)
        {
            const char *g = std::strchr(kGlyphs, *p);
            if (!g)
                continue;
            const float adv = kHudGlyphAdvance[g - kGlyphs] * sc;
            const char name[3] = {'g', *p, 0};
            piece(name, cx + adv * 0.5f - 20.0f * sc, y, 40.0f * sc, h, c);
            cx += adv;
        }
        return wsum;
    }
    void flush(bool glow)
    {
        std::vector<Vtx> &src = glow ? add : norm;
        if (src.empty())
            return;
        Op op;
        op.first = uint32_t(out.verts.size());
        op.count = uint32_t(src.size());
        out.verts.insert(out.verts.end(), src.begin(), src.end());
        op.textured = true;
        op.version = kHudVersion;
        op.clampUV = true;
        op.abe = true;
        op.alphaReg = glow ? 0x48 : 0x44; // added, or blended over the picture
        op.zwrite = false;
        op.test = 0x30000; // depth: always
        op.generated = true;
        out.ops.push_back(op);
        src.clear();
    }
};

void addModernHud(DrawList &out, bool gameHudSeen)
{
    using clock = std::chrono::steady_clock;
    static clock::time_point last = clock::now();
    const clock::time_point nowT = clock::now();
    const float dt = std::clamp(std::chrono::duration<float>(nowT - last).count(), 0.0f, 0.1f);
    last = nowT;
    static float t = 0.0f;
    t += dt;
    const HudState &hs = g_hudNow;
    // the values, smoothed; when each last changed
    struct Track
    {
        float shown = -1.0f, trail = 0.0f, trailHold = 0.0f, changed = -100.0f, flash = 0.0f;
        int32_t prev = -1;
    };
    static Track hp, armor, shield, lives, souls, koins, keys;
    static int32_t armorMax = 4, shieldMaxSeen = 5;
    static float visAll = 0.0f, visTop = 0.0f, shake = 0.0f;
    struct Popup
    {
        int kind; // 0 soul, 1 koin, 2 key, 3 life
        int32_t amount;
        float born;
    };
    static std::vector<Popup> popups;
    const bool live = hs.valid && gameHudSeen && !g_hudMenu;
#ifdef NATIVE_TEST
    std::printf("  modern hud: valid %d seen %d menu %d\n", int(hs.valid), int(gameHudSeen), int(g_hudMenu));
#endif
    auto follow = [&](Track &tr, int32_t v, int popupKind) {
        if (tr.prev < 0)
        {
            tr.prev = v;
            tr.shown = tr.trail = float(v);
            return;
        }
        if (v != tr.prev)
        {
            if (live)
            {
                tr.changed = t;
                if (v < tr.prev)
                {
                    tr.flash = 1.0f;
                    tr.trailHold = 0.6f;
                }
                if (popupKind >= 0 && v > tr.prev)
                {
                    // (one popup per kind, adding up while it is still fresh)
                    bool merged = false;
                    for (Popup &p : popups)
                        if (p.kind == popupKind && t - p.born < 1.6f)
                        {
                            p.amount += v - tr.prev;
                            p.born = t;
                            merged = true;
                        }
                    if (!merged)
                        popups.push_back({popupKind, v - tr.prev, t});
                }
            }
            tr.prev = v;
        }
        tr.shown += (float(v) - tr.shown) * std::min(1.0f, dt * 10.0f);
        if (tr.trailHold > 0.0f)
            tr.trailHold -= dt;
        else
            tr.trail += (tr.shown - tr.trail) * std::min(1.0f, dt * 2.5f);
        if (tr.trail < tr.shown)
            tr.trail = tr.shown;
        tr.flash = std::max(0.0f, tr.flash - dt * 2.5f);
    };
    if (!hs.valid)
        return;
    {
        // (each change of the values in the log, to check what the game's variables mean)
        static int32_t logged[17] = {}, loggedMax = -1;
        if (std::memcmp(logged, hs.vars, sizeof logged) != 0 || loggedMax != hs.healthMax)
        {
            std::memcpy(logged, hs.vars, sizeof logged);
            loggedMax = hs.healthMax;
            std::fprintf(stderr, "[native] hud: health %d/%d armor %d lives %d koins %d souls %d keys %d shield %d (cap %d) "
                                 "sword %d | vars", hs.vars[2], hs.healthMax, hs.vars[3], hs.vars[4], hs.vars[5], hs.vars[6],
                         hs.vars[7], hs.vars[10], hs.vars[14] * 5, hs.vars[11]);
            for (int k = 0; k < 17; ++k)
                std::fprintf(stderr, " %d", hs.vars[k]);
            std::fprintf(stderr, "%s\n", hs.boss ? " | boss" : "");
        }
    }
    const int32_t hpMax = std::max(1, hs.healthMax);
    follow(hp, hs.vars[2], -1);
    follow(armor, hs.vars[3], -1);
    follow(shield, hs.vars[10], -1);
    follow(lives, hs.vars[4], 3);
    follow(souls, hs.vars[6], 0);
    follow(koins, hs.vars[5], 1);
    follow(keys, hs.vars[7], 2);
    armorMax = std::max(armorMax, hs.vars[3]);
    const int32_t shieldMax = std::max({5, hs.vars[14] * 5, shieldMaxSeen});
    shieldMaxSeen = std::max(shieldMaxSeen, hs.vars[10]);
    if (hp.flash >= 0.99f)
        shake = 0.35f;
    shake = std::max(0.0f, shake - dt);
    if (!live)
    {
        visAll = std::max(0.0f, visAll - dt * 4.0f);
        visTop = std::max(0.0f, visTop - dt * 4.0f);
        popups.clear();
        if (visAll <= 0.0f)
            return;
    }
    else if (std::getenv("NR_HUDTEST"))
        visAll = visTop = 1.0f;
    else
    {
        // AUTO-HIDE: the left group after a change of health, armor, shield or lives, or with little health; the
        // counters after a change of theirs (or when the left group is up)
        const float lastLeft = std::max({hp.changed, armor.changed, shield.changed, lives.changed});
        const float lastTop = std::max({souls.changed, koins.changed, keys.changed});
        const bool low = hs.vars[2] * 4 <= hpMax;
        const bool wantAll = g_hudAutoHide <= 0.0f || low || t - lastLeft < g_hudAutoHide || hs.boss;
        const bool wantTop = wantAll || t - lastTop < g_hudAutoHide;
        visAll = std::clamp(visAll + (wantAll ? dt * 5.0f : -dt * 1.6f), 0.0f, 1.0f);
        visTop = std::clamp(visTop + (wantTop ? dt * 5.0f : -dt * 1.6f), 0.0f, 1.0f);
    }
    ensureHudAtlas();
    HudDraw d{out};
    d.W = g_hudCanvasW;
    const float W = d.W;
    auto ease = [](float x) { return x * x * (3.0f - 2.0f * x); };
    const float aL = ease(visAll), aT = ease(std::max(visTop, visAll));
    // ---- the left group, sliding in from the left
    {
        const float ox = 34.0f - (1.0f - aL) * 40.0f + (shake > 0.0f ? std::sin(t * 90.0f) * 5.0f * shake : 0.0f);
        const float oy = 30.0f;
        const float barW = std::clamp(150.0f + 22.0f * float(hpMax), 220.0f, 380.0f), barH = 20.0f;
        const float bx = ox + 58.0f, by = oy + 12.0f;
        const float shadowC[4] = {0.0f, 0.0f, 0.0f, 0.55f * aL};
        // a dark glass panel behind it all
        d.piece("panel", ox - 18.0f, oy - 14.0f, barW + 100.0f, 108.0f, shadowC);
        // the heart, beating faster with little health
        const float frac = std::clamp(hp.shown / float(hpMax), 0.0f, 1.0f);
        const float beat = 1.0f + 0.06f * std::pow(std::max(0.0f, std::sin(t * (frac < 0.3f ? 9.0f : 3.2f))), 8.0f);
        const float hs2 = 46.0f * beat;
        const float heartC[4] = {0.95f, 0.22f + 0.5f * hp.flash, 0.2f + 0.5f * hp.flash, aL};
        const float glowC[4] = {0.8f, 0.1f, 0.08f, (0.35f + 0.4f * hp.flash + (frac < 0.3f ? 0.3f : 0.0f)) * aL};
        d.piece("glow", ox + 23.0f - hs2, by + barH * 0.5f - hs2, hs2 * 2.0f, hs2 * 2.0f, glowC, true);
        d.piece("heart", ox + 23.0f - hs2 * 0.5f, by + barH * 0.5f - hs2 * 0.5f, hs2, hs2, heartC);
        // the bar: a gold rim, a dark inside, the damage trail, the fill (lighter at the top), notches
        const float rim[4] = {0.85f, 0.66f, 0.32f, 0.95f * aL}, rim2[4] = {0.45f, 0.3f, 0.12f, 0.95f * aL};
        d.bar(bx - 3.0f, by - 3.0f, barW + 6.0f, barH + 6.0f, rim, rim2);
        const float well[4] = {0.06f, 0.03f, 0.03f, 0.95f * aL};
        d.bar(bx, by, barW, barH, well);
        const float trailFrac = std::clamp(hp.trail / float(hpMax), 0.0f, 1.0f);
        const float trailC[4] = {1.0f, 0.85f, 0.7f, 0.8f * aL};
        if (trailFrac > frac + 0.002f)
            d.bar(bx, by, barW * trailFrac, barH, trailC);
        const float top[4] = {1.0f, 0.35f + 0.6f * hp.flash, 0.28f + 0.6f * hp.flash, aL};
        const float bot[4] = {0.55f, 0.04f + 0.4f * hp.flash, 0.05f + 0.4f * hp.flash, aL};
        d.bar(bx, by, barW * frac, barH, top, bot);
        // (a slow gleam running along the fill)
        const float g = std::fmod(t * 0.35f, 1.6f);
        if (g < 1.0f && frac > 0.05f)
        {
            const float gc[4] = {1.0f, 0.75f, 0.6f, 0.35f * aL};
            const float gx = bx + barW * frac * g;
            d.piece("glow", gx - 24.0f, by - 8.0f, 48.0f, barH + 16.0f, gc, true);
        }
        if (hpMax > 1 && hpMax <= 16)
            for (int k = 1; k < hpMax; ++k)
            {
                const float nc[4] = {0.0f, 0.0f, 0.0f, 0.45f * aL};
                d.piece("white", bx + barW * float(k) / float(hpMax) - 1.0f, by + 3.0f, 2.0f, barH - 6.0f, nc);
            }
        // the shield points: a thin blue bar under it
        const float sy = by + barH + 9.0f, sh = 8.0f, sw = barW * 0.8f;
        const float sWell[4] = {0.03f, 0.04f, 0.08f, 0.85f * aL};
        d.bar(bx, sy, sw, sh, sWell);
        const float sTop[4] = {0.55f, 0.85f, 1.0f, aL}, sBot[4] = {0.15f, 0.35f, 0.85f, aL};
        d.bar(bx, sy, sw * std::clamp(shield.shown / float(shieldMax), 0.0f, 1.0f), sh, sTop, sBot);
        // the armor: shields, lit for each level he has
        const float ay = sy + sh + 8.0f;
        for (int k = 0; k < std::min(armorMax, 8); ++k)
        {
            const bool has = k < hs.vars[3];
            const float lit = has ? 1.0f : 0.0f;
            const float c[4] = {0.55f + 0.35f * lit, 0.6f + 0.3f * lit, 0.68f + 0.25f * lit, (has ? 1.0f : 0.3f) * aL};
            d.piece("shield", bx + k * 26.0f - 3.0f, ay - 3.0f, 30.0f, 30.0f, c);
        }
        // the lives: a helmet and its count
        char buf[16];
        std::snprintf(buf, sizeof buf, "x%d", std::max(0, hs.vars[4]));
        const float lc[4] = {0.85f, 0.85f, 0.9f, aL};
        const float lx = bx + std::min(armorMax, 8) * 26.0f + 18.0f;
        d.piece("helmet", lx, ay - 6.0f, 34.0f, 34.0f, lc);
        const float tc[4] = {0.95f, 0.9f, 0.8f, aL};
        d.text(buf, lx + 34.0f, ay - 4.0f, 30.0f, tc);
    }
    // ---- the counters at the top right, sliding in from the right
    {
        const float rx = W - 34.0f + (1.0f - aT) * 50.0f;
        float y = 30.0f;
        auto counter = [&](const char *icon, const float col[4], const float glowCol[4], int32_t value, float pulse) {
            char buf[16];
            std::snprintf(buf, sizeof buf, "%d", std::max(0, value));
            const float h = 34.0f * (1.0f + 0.18f * pulse);
            const float tc[4] = {1.0f, 0.95f, 0.85f, aT};
            const float shadowC[4] = {0.0f, 0.0f, 0.0f, 0.5f * aT};
            d.piece("panel", rx - 150.0f, y - 10.0f, 160.0f, 56.0f, shadowC);
            const float wdt = d.text(buf, rx, y + 2.0f - (h - 34.0f) * 0.5f, h, tc, true);
            const float ic = 40.0f;
            const float gc[4] = {glowCol[0], glowCol[1], glowCol[2], glowCol[3] * aT * (0.6f + 0.6f * pulse)};
            d.piece("glow", rx - wdt - ic - 14.0f, y - 12.0f, ic + 24.0f, ic + 24.0f, gc, true);
            const float cc[4] = {col[0], col[1], col[2], aT};
            d.piece(icon, rx - wdt - ic - 2.0f, y, ic, ic, cc);
            y += 50.0f;
        };
        auto pulseOf = [&](const Track &tr) { return std::max(0.0f, 1.0f - (t - tr.changed) * 3.0f); };
        const float soulC[4] = {0.55f, 0.85f, 1.0f, 1.0f}, soulG[4] = {0.25f, 0.6f, 1.0f, 0.55f};
        counter("soul", soulC, soulG, hs.vars[6], pulseOf(souls));
        const float coinC[4] = {1.0f, 0.78f, 0.36f, 1.0f}, coinG[4] = {1.0f, 0.7f, 0.2f, 0.35f};
        counter("coin", coinC, coinG, hs.vars[5], pulseOf(koins));
        if (hs.vars[7] > 0)
        {
            const float keyC[4] = {0.95f, 0.8f, 0.45f, 1.0f}, keyG[4] = {1.0f, 0.75f, 0.3f, 0.3f};
            counter("key", keyC, keyG, hs.vars[7], pulseOf(keys));
        }
        // ---- PICKUPS: "+N" with its icon, sliding in under the counters, then fading
        float py = y + 16.0f;
        for (auto it = popups.begin(); it != popups.end();)
        {
            const float age = t - it->born;
            if (age > 2.4f)
            {
                it = popups.erase(it);
                continue;
            }
            const float in = ease(std::clamp(age / 0.25f, 0.0f, 1.0f)), outF = 1.0f - std::clamp((age - 1.8f) / 0.6f, 0.0f, 1.0f);
            const float a = in * outF;
            const float x = W - 34.0f + (1.0f - in) * 120.0f;
            static const char *icons[4] = {"soul", "coin", "key", "helmet"};
            static const float cols[4][3] = {{0.55f, 0.85f, 1.0f}, {1.0f, 0.78f, 0.36f}, {0.95f, 0.8f, 0.45f}, {0.85f, 0.85f, 0.9f}};
            char buf[16];
            std::snprintf(buf, sizeof buf, "+%d", it->amount);
            const float tc[4] = {cols[it->kind][0], cols[it->kind][1], cols[it->kind][2], a};
            const float shadowC[4] = {0.0f, 0.0f, 0.0f, 0.45f * a};
            d.piece("panel", x - 140.0f, py - 8.0f, 150.0f, 46.0f, shadowC);
            const float wdt = d.text(buf, x, py, 30.0f, tc, true);
            const float ic[4] = {cols[it->kind][0], cols[it->kind][1], cols[it->kind][2], a};
            d.piece(icons[it->kind], x - wdt - 36.0f, py - 1.0f, 32.0f, 32.0f, ic);
            py += 44.0f;
            ++it;
        }
    }
    d.flush(false);
    d.flush(true);
}

void addWeather(DrawList &out)
{
    if (!g_lighting.load(std::memory_order_relaxed))
    {
        g_weatherVerts.clear();
        return;
    }
    int lastMain = -1;
    for (size_t i = 0; i < out.ops.size(); ++i)
        if (out.ops[i].type == OpType::Draw && out.ops[i].target == 0 && out.ops[i].viewId == out.mainView &&
            !out.ops[i].abe && out.ops[i].zwrite)
            lastMain = int(i);
    if (lastMain < 0)
    {
        g_weatherVerts.clear();
        return;
    }
    int insertAt = lastMain;
    for (size_t i = 0; i < out.ops.size(); ++i)
        if (out.ops[i].type == OpType::Draw && out.ops[i].target == 0 && out.ops[i].viewId == out.mainView)
            insertAt = int(i);
    const Mat vs = mul(out.views[out.mainView], out.viewScreens[out.mainView]);
    auto toClip = [&](const float w[3], float pos[4]) {
        const float p4[4] = {w[0], w[1], w[2], 1.0f};
        float sp[4];
        xform(p4, vs, sp);
        pos[0] = (sp[0] - 2048.0f * sp[3]) / 320.0f;
        pos[1] = (sp[1] - 2048.0f * sp[3]) / 112.0f;
        pos[2] = 1.0f;
        pos[3] = sp[3];
    };
    // UNDER COVER: a surface with something solid over most of it (a cave's ceiling, a roof) stays dry in the rain:
    // no wet darkening, no puddles (they showed in caves and under roofs)
    if (out.raining && g_roof.valid)
        for (Op &op : out.ops)
        {
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.count < 3 || op.relief <= 0.0f)
                continue;
            const uint32_t step = std::max(1u, op.count / 96u);
            int known = 0, covered = 0;
            for (uint32_t i = op.first; i < op.first + op.count; i += step)
            {
                const Vtx &v = out.verts[i];
                const float r = g_roof.at(v.world[0], v.world[2]);
                if (r < -1e29f)
                    continue;
                ++known;
                covered += r > v.world[1] + 1.5f;
            }
            const float share = known >= 3 ? float(covered) / float(known) : 0.0f;
            op.dry = std::clamp((share - 0.15f) / 0.3f, 0.0f, 1.0f);
            // POLISHED STONE: a dry stone floor under cover (a hall, a cathedral) shows faint reflections of the
            // candles and windows
            if (op.dry > 0.6f && op.textured && !op.abe && op.zwrite && op.reflect < 0.15f && !op.generated)
            {
                const auto vt = g_tracker.versions.find(op.version);
                if (vt != g_tracker.versions.end() && vt->second.grassy == 0 && vt->second.grey > 0.4f)
                {
                    int flat = 0, all = 0;
                    for (uint32_t i = op.first; i + 2 < op.first + op.count && all < 32; i += 3 * step)
                    {
                        const Vtx &A = out.verts[i], &B = out.verts[i + 1], &C = out.verts[i + 2];
                        float e1[3], e2[3];
                        for (int k = 0; k < 3; ++k)
                        {
                            e1[k] = B.world[k] - A.world[k];
                            e2[k] = C.world[k] - A.world[k];
                        }
                        const float n[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
                        const float l = std::sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]);
                        ++all;
                        flat += l > 1e-6f && std::fabs(n[1]) > 0.9f * l;
                    }
                    if (all && flat * 10 >= all * 8)
                        op.reflect = 0.18f;
                }
            }
        }
    if (out.havePlayer && g_roof.valid)
    {
        // INDOORS: something high over Maximo (a roof, a vault): the haze and the mist thin out
        static float in = 0.0f;
        const float r = g_roof.at(out.player[0], out.player[2]);
        const float target = r > out.player[1] + 3.0f ? 1.0f : 0.0f;
        in += (target - in) * 0.03f;
        out.indoor = in;
    }
    const Mat &V = out.views[out.mainView];
    const float right[3] = {V.m[0], V.m[4], V.m[8]}, up[3] = {V.m[1], V.m[5], V.m[9]};
    const float t = float(g_tracker.frameNo % 360000u) / 60.0f;
    auto hash = [](uint32_t n) {
        n = (n ^ 61u) ^ (n >> 16);
        n *= 9u;
        n ^= n >> 4;
        n *= 0x27d4eb2du;
        n ^= n >> 15;
        return float(n & 0xFFFFFF) / 16777215.0f;
    };
    Vtx blank{};
    std::vector<Vtx> verts;
    verts.swap(g_weatherVerts);
    static std::vector<Vtx> solid; // (things drawn solid, not as light: leaves, bats, crows)
    solid.clear();
    auto solidTri = [&](const float A[3], const float B[3], const float C[3], const float rgb[3]) {
        for (const float *P : {A, B, C})
        {
            Vtx o = blank;
            for (int k = 0; k < 3; ++k)
            {
                o.world[k] = P[k];
                o.col[k] = o.raw[k] = rgb[k];
            }
            o.normal[1] = 1.0f;
            o.col[3] = 128.0f;
            toClip(o.world, o.pos);
            solid.push_back(o);
        }
    };
    float centre[3];
    for (int k = 0; k < 3; ++k)
        centre[k] = out.camPos[k] + out.camFwd[k] * 12.0f;
    // RAIN (in levels only: not on the title menu)
    if (out.raining)
    {
        constexpr float kBox = 36.0f, kTall = 26.0f;
        float slant[3] = {0.13f, -1.0f, 0.07f};
        const float sl = std::sqrt(slant[0] * slant[0] + slant[1] * slant[1] + slant[2] * slant[2]);
        for (float &x : slant)
            x /= sl;
        const int drops = int(1100.0f * g_rain);
        const float lo[3] = {centre[0] - kBox * 0.5f, centre[1] - kTall * 0.45f, centre[2] - kBox * 0.5f};
        for (int i = 0; i < drops; ++i)
        {
            const float hx = hash(uint32_t(i) * 3u + 1u), hz = hash(uint32_t(i) * 3u + 2u), hy = hash(uint32_t(i) * 3u + 3u);
            const float speed = 20.0f + 6.0f * hy;
            float p[3];
            const float fy = hy - t * speed / kTall;
            p[1] = lo[1] + (fy - std::floor(fy)) * kTall;
            const float fx = (hx * kBox - lo[0]) / kBox, fz = (hz * kBox - lo[2]) / kBox;
            p[0] = lo[0] + (fx - std::floor(fx)) * kBox + slant[0] * (p[1] - lo[1]);
            p[2] = lo[2] + (fz - std::floor(fz)) * kBox + slant[2] * (p[1] - lo[1]);
            // under cover (a roof, a cave, leaves) or below the ground: not drawn
            if (g_roof.valid)
            {
                if (p[1] < g_roof.at(p[0], p[2]) + 0.05f)
                    continue;
            }
            else if (out.haveGround && p[1] < out.groundY - 1.0f)
                continue;
            float toCam[3], side[3];
            for (int k = 0; k < 3; ++k)
                toCam[k] = out.camPos[k] - p[k];
            const float dist = std::sqrt(toCam[0] * toCam[0] + toCam[1] * toCam[1] + toCam[2] * toCam[2]);
            if (dist < 1.5f)
                continue;
            side[0] = slant[1] * toCam[2] - slant[2] * toCam[1];
            side[1] = slant[2] * toCam[0] - slant[0] * toCam[2];
            side[2] = slant[0] * toCam[1] - slant[1] * toCam[0];
            const float sdl = std::sqrt(side[0] * side[0] + side[1] * side[1] + side[2] * side[2]);
            if (sdl < 1e-5f)
                continue;
            const float w = 0.012f * std::max(1.0f, dist / 10.0f);
            const float len = 0.7f + 0.35f * hx;
            Vtx q4[4];
            for (int j = 0; j < 4; ++j)
            {
                Vtx &v = q4[j];
                v = blank;
                const bool top = (j & 1) == 0;
                const float sd = (j & 2) ? w : -w;
                for (int k = 0; k < 3; ++k)
                    v.world[k] = p[k] + (top ? 0.0f : slant[k] * len) + side[k] / sdl * sd;
                v.col[0] = 150.0f;
                v.col[1] = 160.0f;
                v.col[2] = 182.0f;
                v.col[3] = top ? 0.0f : 34.0f;
                for (int k = 0; k < 3; ++k)
                    v.raw[k] = v.col[k];
                toClip(v.world, v.pos);
            }
            for (int j : {0, 1, 2, 1, 3, 2})
                verts.push_back(q4[j]);
        }
    }
    // a soft round point facing the camera: bright in the middle, fading to nothing at the rim
    auto glowPoint = [&](const float p[3], float r, const float rgb[3], float alpha) {
        Vtx c = blank;
        for (int k = 0; k < 3; ++k)
        {
            c.world[k] = p[k];
            c.col[k] = c.raw[k] = rgb[k];
        }
        c.col[3] = alpha;
        toClip(c.world, c.pos);
        Vtx rim[7];
        for (int s = 0; s <= 6; ++s)
        {
            const float a = float(s) * 1.0471976f;
            rim[s] = c;
            for (int k = 0; k < 3; ++k)
                rim[s].world[k] = p[k] + (right[k] * std::cos(a) + up[k] * std::sin(a)) * r;
            rim[s].col[3] = 0.0f;
            toClip(rim[s].world, rim[s].pos);
        }
        for (int s = 0; s < 6; ++s)
        {
            verts.push_back(c);
            verts.push_back(rim[s]);
            verts.push_back(rim[s + 1]);
        }
    };
    // CANDLE FLAMES (on the props' floor candles): a small bright tongue of flame and a soft warm halo, each
    // flickering at its own pace
    for (size_t i = 0; i < out.flames.size(); ++i)
    {
        const auto &f = out.flames[i];
        const float h = hash(uint32_t(i) * 7u + uint32_t(f[0] * 13.0f) + uint32_t(f[2] * 17.0f));
        const float fl = 0.8f + 0.12f * std::sin(t * 9.0f + h * 40.0f) + 0.08f * std::sin(t * 23.0f + h * 13.0f);
        const float tongue[3] = {f[0] + std::sin(t * 5.0f + h * 9.0f) * 0.008f, f[1], f[2]};
        const float core[3] = {255.0f, 200.0f, 110.0f}, halo[3] = {255.0f, 150.0f, 60.0f};
        glowPoint(tongue, 0.045f, core, 220.0f * fl);
        glowPoint(tongue, 0.28f, halo, 26.0f * fl);
    }
    // LIGHT BEAMS: dust drifting slowly in the game's beams of light (its long pale see-through glow cards),
    // each speck twinkling as it turns in the light
    if (g_dust > 0.0f)
    {
        struct Beam
        {
            uint32_t first, count;
        };
        std::vector<Beam> beams;
        for (const Op &op : out.ops)
        {
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.generated || !op.abe ||
                !op.textured || op.count < 3 || op.skinned || op.lava)
                continue;
            const auto vt = g_tracker.versions.find(op.version);
            if (vt == g_tracker.versions.end() || vt->second.hot > 0.05f || vt->second.glass)
                continue;
            float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
            for (uint32_t i = op.first; i < op.first + op.count; ++i)
                for (int k = 0; k < 3; ++k)
                {
                    lo[k] = std::min(lo[k], out.verts[i].world[k]);
                    hi[k] = std::max(hi[k], out.verts[i].world[k]);
                }
            // (long and tall: a beam slanting down, not a puff of smoke or a flat mist layer)
            const float ey = hi[1] - lo[1], exz = std::max(hi[0] - lo[0], hi[2] - lo[2]);
            if (ey < 6.0f || ey > 80.0f || exz > 60.0f || ey < 0.5f * exz || op.special)
                continue;
            {
                // (standing or slanting cards, not a layer lying over the ground)
                float upish = 0.0f, all = 0.0f;
                for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
                {
                    const Vtx &A = out.verts[i], &B = out.verts[i + 1], &C = out.verts[i + 2];
                    float e1[3], e2[3];
                    for (int k = 0; k < 3; ++k)
                    {
                        e1[k] = B.world[k] - A.world[k];
                        e2[k] = C.world[k] - A.world[k];
                    }
                    const float n[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
                    const float l = std::sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]);
                    if (l > 1e-6f)
                    {
                        upish += std::fabs(n[1]) / l;
                        all += 1.0f;
                    }
                }
                if (all <= 0.0f || upish / all > 0.7f)
                    continue;
            }
            const float dx = (lo[0] + hi[0]) * 0.5f - out.camPos[0], dz = (lo[2] + hi[2]) * 0.5f - out.camPos[2];
            if (dx * dx + dz * dz > 60.0f * 60.0f)
                continue;
            beams.push_back({op.first, op.count - op.count % 3});
            if (beams.size() >= 8)
                break;
        }
        for (size_t b = 0; b < beams.size(); ++b)
        {
            const Beam &bm = beams[b];
            const uint32_t tris = bm.count / 3;
            for (int i = 0; i < 40; ++i)
            {
                const uint32_t sd = uint32_t(b) * 977u + uint32_t(i) * 131u;
                const uint32_t tri = std::min(tris - 1, uint32_t(hash(sd) * float(tris)));
                float u = hash(sd + 1u), v = hash(sd + 2u);
                if (u + v > 1.0f)
                {
                    u = 1.0f - u;
                    v = 1.0f - v;
                }
                const Vtx &A = out.verts[bm.first + tri * 3], &B = out.verts[bm.first + tri * 3 + 1],
                          &C = out.verts[bm.first + tri * 3 + 2];
                const float h = hash(sd + 3u);
                float p[3];
                for (int k = 0; k < 3; ++k)
                    p[k] = A.world[k] + (B.world[k] - A.world[k]) * u + (C.world[k] - A.world[k]) * v;
                p[0] += std::sin(t * 0.21f + h * 30.0f) * 0.4f;
                p[1] += std::sin(t * 0.15f + h * 17.0f) * 0.5f;
                p[2] += std::cos(t * 0.18f + h * 23.0f) * 0.4f;
                const float tw = std::sin(t * (1.5f + h) + h * 40.0f);
                const float rgb[3] = {235.0f, 225.0f, 195.0f};
                glowPoint(p, 0.035f + 0.02f * h, rgb, 25.0f + 70.0f * tw * tw);
            }
        }
    }
    // STAINED GLASS: the colours of a stained-glass window thrown onto the floor in front of it, a soft pool of
    // coloured light where the light coming through it falls
    if (out.inLevel && g_floor.valid)
    {
        int pools = 0;
        for (const Op &op : out.ops)
        {
            if (pools >= 10)
                break;
            // (a window: a few big flat panels, not a colourful object with many small faces)
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.generated || !op.textured ||
                op.count < 3 || op.count > 60 || op.skinned || op.shiny)
                continue;
            const auto vt = g_tracker.versions.find(op.version);
            if (vt == g_tracker.versions.end() || !vt->second.glass)
                continue;
            float c[3] = {}, lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f}, nsum[3] = {};
            for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
            {
                const Vtx &A = out.verts[i], &B = out.verts[i + 1], &C = out.verts[i + 2];
                float e1[3], e2[3];
                for (int k = 0; k < 3; ++k)
                {
                    e1[k] = B.world[k] - A.world[k];
                    e2[k] = C.world[k] - A.world[k];
                }
                const float n[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
                // (all facing one way: towards the camera's side, the inside of the building)
                const float toCam = (out.camPos[0] - A.world[0]) * n[0] + (out.camPos[2] - A.world[2]) * n[2];
                for (int k = 0; k < 3; ++k)
                    nsum[k] += toCam >= 0.0f ? n[k] : -n[k];
                for (const Vtx *V : {&A, &B, &C})
                    for (int k = 0; k < 3; ++k)
                    {
                        c[k] += V->world[k];
                        lo[k] = std::min(lo[k], V->world[k]);
                        hi[k] = std::max(hi[k], V->world[k]);
                    }
            }
            const float cnt = float(op.count - op.count % 3);
            for (float &x : c)
                x /= cnt;
            const float nl = std::sqrt(nsum[0] * nsum[0] + nsum[2] * nsum[2]);
            if (nl < 1e-6f || std::fabs(nsum[1]) > nl)
                continue; // (lying flat: not a window)
            const float nx = nsum[0] / nl, nz = nsum[2] / nl;
            const float height = std::max(1.5f, c[1] - out.groundY);
            const float tx = c[0] + nx * height * 0.9f, tz = c[2] + nz * height * 0.9f;
            float fy = g_floor.heightNear(tx, tz, out.groundY, 6.0f, 6.0f);
            if (fy < -1e29f)
                continue;
            const float dx = tx - out.camPos[0], dz = tz - out.camPos[2];
            if (dx * dx + dz * dz > 60.0f * 60.0f)
                continue;
            if (hi[1] - lo[1] < 2.5f || std::max(hi[0] - lo[0], hi[2] - lo[2]) < 1.5f)
                continue; // (too small for a window)
            const float ext = std::max({hi[0] - lo[0], hi[2] - lo[2], hi[1] - lo[1]});
            const float rx = std::clamp(ext * 0.45f, 1.0f, 5.0f);
            const float *gc = vt->second.glassCol;
            const float rgb[3] = {std::min(255.0f, gc[0] * 255.0f * 1.4f), std::min(255.0f, gc[1] * 255.0f * 1.4f),
                                  std::min(255.0f, gc[2] * 255.0f * 1.4f)};
            // a flat soft oval on the floor, longer away from the window
            Vtx cv = blank;
            cv.world[0] = tx;
            cv.world[1] = fy + 0.05f;
            cv.world[2] = tz;
            for (int k = 0; k < 3; ++k)
                cv.col[k] = cv.raw[k] = rgb[k];
            cv.col[3] = 40.0f;
            toClip(cv.world, cv.pos);
            constexpr int kSeg = 16;
            Vtx rim[kSeg + 1];
            for (int k = 0; k <= kSeg; ++k)
            {
                const float an = 6.2831853f * float(k) / float(kSeg);
                const float a1 = std::cos(an) * rx * 1.5f, a2 = std::sin(an) * rx; // (along the light, across it)
                rim[k] = cv;
                rim[k].world[0] = tx + nx * a1 - nz * a2;
                rim[k].world[2] = tz + nz * a1 + nx * a2;
                rim[k].col[3] = 0.0f;
                toClip(rim[k].world, rim[k].pos);
            }
            for (int k = 0; k < kSeg; ++k)
            {
                verts.push_back(cv);
                verts.push_back(rim[k]);
                verts.push_back(rim[k + 1]);
            }
            ++pools;
        }
    }
    // HOUSES (mausoleums, crypts, porches): covered rooms found in the floor map, a floor with a solid roof three to
    // twenty-two units over it (the floor map holds every solid surface facing up, the roofs too, not the trees), and
    // their open edges (doorways, the eaves). Worked out again whenever the floor map is.
    struct RoomEdge
    {
        float in[3];    // the covered cell's middle, on the floor
        float out[2];   // the way out (x, z)
        float roofY, outY;
    };
    struct Room
    {
        float c[3];
        float roofY;
        int cells;
        std::vector<RoomEdge> edges;
    };
    static std::vector<Room> rooms;
    static uint32_t roomsFor = ~0u;
    if (g_houses > 0.0f && out.inLevel && g_floor.valid && out.havePlayer && roomsFor != g_floor.buildId)
    {
        roomsFor = g_floor.buildId;
        rooms.clear();
        constexpr int N = FloorGrid::kN;
        const FloorGrid &g = g_floor;
        const int px = int(std::floor(out.player[0] - g.x0)), pz = int(std::floor(out.player[2] - g.z0));
        const int r0x = std::max(1, px - 34), r1x = std::min(N - 2, px + 34), r0z = std::max(1, pz - 34), r1z = std::min(N - 2, pz + 34);
        std::vector<float> lowY(N * N, -1e30f), topY(N * N, -1e30f);
        std::vector<int8_t> kind(N * N, 0); // 1 covered, 2 open
        for (int iz = r0z; iz <= r1z; ++iz)
            for (int ix = r0x; ix <= r1x; ++ix)
            {
                const int c = iz * N + ix;
                if (!g.n[c])
                    continue;
                float lo = 1e30f;
                for (int k = 0; k < g.n[c]; ++k)
                    lo = std::min(lo, g.h[c][k]);
                if (std::fabs(lo - out.player[1]) > 5.0f)
                    continue;
                float roof = -1e30f;
                bool between = false;
                for (int k = 0; k < g.n[c]; ++k)
                {
                    const float d = g.h[c][k] - lo;
                    if (d >= 3.0f && d <= 22.0f)
                        roof = std::max(roof, g.h[c][k]);
                    else if (d > 0.6f && d < 3.0f)
                        between = true; // (a step or a ledge inside: not open floor)
                }
                lowY[c] = lo;
                topY[c] = roof;
                kind[c] = roof > -1e29f ? 1 : (between ? 0 : 2);
            }
        std::vector<int> label(N * N, -1);
        for (int iz = r0z; iz <= r1z; ++iz)
            for (int ix = r0x; ix <= r1x; ++ix)
            {
                const int c0 = iz * N + ix;
                if (kind[c0] != 1 || label[c0] >= 0)
                    continue;
                // one covered room: the covered cells joined to this one
                std::vector<int> stack{c0}, cells;
                label[c0] = int(rooms.size());
                while (!stack.empty())
                {
                    const int c = stack.back();
                    stack.pop_back();
                    cells.push_back(c);
                    for (int d : {1, -1, N, -N})
                    {
                        const int nb = c + d;
                        if (nb >= 0 && nb < N * N && kind[nb] == 1 && label[nb] < 0 && std::fabs(lowY[nb] - lowY[c]) < 0.7f)
                        {
                            label[nb] = label[c0];
                            stack.push_back(nb);
                        }
                    }
                }
                Room rm{};
                rm.cells = int(cells.size());
                for (int c : cells)
                {
                    rm.c[0] += g.x0 + float(c % N) + 0.5f;
                    rm.c[1] += lowY[c];
                    rm.c[2] += g.z0 + float(c / N) + 0.5f;
                    rm.roofY += topY[c];
                    for (int d : {1, -1, N, -N})
                    {
                        const int nb = c + d;
                        if (nb < 0 || nb >= N * N || kind[nb] != 2 || std::fabs(lowY[nb] - lowY[c]) > 0.7f)
                            continue;
                        RoomEdge e;
                        e.in[0] = g.x0 + float(c % N) + 0.5f;
                        e.in[1] = lowY[c];
                        e.in[2] = g.z0 + float(c / N) + 0.5f;
                        e.out[0] = d == 1 ? 1.0f : d == -1 ? -1.0f : 0.0f;
                        e.out[1] = d == N ? 1.0f : d == -N ? -1.0f : 0.0f;
                        e.roofY = topY[c];
                        e.outY = lowY[nb];
                        rm.edges.push_back(e);
                    }
                }
                for (int k = 0; k < 3; ++k)
                    rm.c[k] /= float(rm.cells);
                rm.roofY /= float(rm.cells);
                // (a room: big enough to stand in, and open somewhere)
                if (rm.cells >= 16 && !rm.edges.empty())
                    rooms.push_back(std::move(rm));
                else
                    for (int c : cells)
                        label[c] = -2;
            }
    }
    if (g_houses > 0.0f && out.inLevel)
        for (size_t ri = 0; ri < rooms.size(); ++ri)
        {
            const Room &rm = rooms[ri];
            const float dx = rm.c[0] - out.camPos[0], dz = rm.c[2] - out.camPos[2];
            if (dx * dx + dz * dz > 45.0f * 45.0f)
                continue;
            const float hr = hash(uint32_t(ri) * 31u + uint32_t(rm.c[0] * 7.0f) + uint32_t(rm.c[2] * 13.0f));
            // DOORWAY GLOW: a dim warm light inside (a candle somewhere in the dark), and a little mist drifting
            // out through the openings
            {
                const float fl = 0.85f + 0.1f * std::sin(t * 3.1f + hr * 20.0f) + 0.05f * std::sin(t * 7.3f + hr * 9.0f);
                const float p[3] = {rm.c[0], rm.c[1] + 1.2f, rm.c[2]};
                const float warm[3] = {255.0f, 160.0f, 80.0f};
                glowPoint(p, std::clamp(std::sqrt(float(rm.cells)) * 0.45f, 1.2f, 3.0f), warm, 22.0f * fl * g_houses);
                const float core[3] = {255.0f, 190.0f, 110.0f};
                const float lp[3] = {rm.c[0], rm.c[1] + 0.5f, rm.c[2]};
                glowPoint(lp, 0.5f, core, 30.0f * fl * g_houses);
                for (int k = 0; k < 6; ++k)
                {
                    const float h1 = hash(uint32_t(ri) * 101u + uint32_t(k) * 7u);
                    const RoomEdge &e = rm.edges[size_t(h1 * float(rm.edges.size())) % rm.edges.size()];
                    const float life = 6.0f + 3.0f * h1, ph = std::fmod(t / life + h1, 1.0f);
                    const float mp[3] = {e.in[0] + e.out[0] * (ph * 3.0f - 0.5f) + std::sin(t * 0.4f + h1 * 9.0f) * 0.3f,
                                         e.in[1] + 0.3f + ph * 0.6f,
                                         e.in[2] + e.out[1] * (ph * 3.0f - 0.5f) + std::cos(t * 0.3f + h1 * 9.0f) * 0.3f};
                    const float mist[3] = {120.0f, 125.0f, 130.0f};
                    glowPoint(mp, 0.6f + 0.6f * ph, mist, 14.0f * std::sin(ph * 3.1415927f) * g_houses);
                }
            }
            for (size_t ei = 0; ei < rm.edges.size(); ++ei)
            {
                const RoomEdge &e = rm.edges[ei];
                const float he = hash(uint32_t(ri) * 977u + uint32_t(ei) * 131u);
                // the eave: on the roof's edge over the opening
                const float top[3] = {e.in[0] + e.out[0] * 0.5f, e.roofY - 0.05f, e.in[2] + e.out[1] * 0.5f};
                // RAIN OFF THE ROOF: a thin stream falling from the edge, drops running down it, a splash below
                if (out.raining && (ei & 1) == 0 && e.roofY - e.outY > 2.0f)
                {
                    const float bot[3] = {top[0] + e.out[0] * 0.15f, e.outY, top[2] + e.out[1] * 0.15f};
                    float dir[3] = {bot[0] - top[0], bot[1] - top[1], bot[2] - top[2]};
                    float toCam[3] = {out.camPos[0] - top[0], out.camPos[1] - top[1], out.camPos[2] - top[2]};
                    float side[3] = {dir[1] * toCam[2] - dir[2] * toCam[1], dir[2] * toCam[0] - dir[0] * toCam[2],
                                     dir[0] * toCam[1] - dir[1] * toCam[0]};
                    const float sl = std::sqrt(side[0] * side[0] + side[1] * side[1] + side[2] * side[2]);
                    if (sl > 1e-6f)
                    {
                        Vtx q[4];
                        for (int j = 0; j < 4; ++j)
                        {
                            q[j] = blank;
                            const float *P = (j & 1) ? bot : top;
                            const float w = ((j & 2) ? 0.025f : -0.025f);
                            for (int k = 0; k < 3; ++k)
                                q[j].world[k] = P[k] + side[k] / sl * w;
                            q[j].col[0] = q[j].raw[0] = 150.0f;
                            q[j].col[1] = q[j].raw[1] = 165.0f;
                            q[j].col[2] = q[j].raw[2] = 190.0f;
                            q[j].col[3] = 28.0f;
                            toClip(q[j].world, q[j].pos);
                        }
                        for (int j : {0, 1, 2, 1, 3, 2})
                            verts.push_back(q[j]);
                    }
                    const float drop[3] = {170.0f, 185.0f, 210.0f};
                    for (int k = 0; k < 3; ++k)
                    {
                        const float f = std::fmod(t * 1.6f + he * 7.0f + float(k) / 3.0f, 1.0f);
                        const float dp[3] = {top[0] + dir[0] * f, top[1] + dir[1] * f, top[2] + dir[2] * f};
                        glowPoint(dp, 0.035f, drop, 70.0f);
                    }
                    const float sp[3] = {bot[0], bot[1] + 0.05f, bot[2]};
                    glowPoint(sp, 0.12f + 0.06f * std::sin(t * 9.0f + he * 30.0f), drop, 40.0f);
                }
                // HANGING VINES from the eaves, swaying a little
                if (he < 0.3f)
                {
                    const float len = 0.8f + 1.4f * hash(uint32_t(ei) * 7u + 3u + uint32_t(ri));
                    const float green[3] = {30.0f, 52.0f, 24.0f}, leafC[3] = {36.0f, 62.0f, 28.0f};
                    const float along[2] = {-e.out[1], e.out[0]};
                    const float off = (he / 0.3f - 0.5f) * 0.8f;
                    float prev[3] = {top[0] + along[0] * off, top[1], top[2] + along[1] * off};
                    const int segs = 6;
                    for (int s = 1; s <= segs; ++s)
                    {
                        const float f = float(s) / float(segs);
                        const float sw = std::sin(t * 1.2f + he * 30.0f + f * 2.0f) * 0.12f * f;
                        const float cur[3] = {top[0] + along[0] * off + e.out[0] * sw, top[1] - len * f,
                                              top[2] + along[1] * off + e.out[1] * sw};
                        const float w = 0.02f;
                        const float A[3] = {prev[0] - along[0] * w, prev[1], prev[2] - along[1] * w};
                        const float B[3] = {prev[0] + along[0] * w, prev[1], prev[2] + along[1] * w};
                        const float C[3] = {cur[0] + along[0] * w, cur[1], cur[2] + along[1] * w};
                        const float D[3] = {cur[0] - along[0] * w, cur[1], cur[2] - along[1] * w};
                        solidTri(A, B, C, green);
                        solidTri(A, C, D, green);
                        // a leaf or two on each piece
                        const float lr = hash(uint32_t(ei) * 13u + uint32_t(s) * 5u + uint32_t(ri));
                        const float sd = lr < 0.5f ? 1.0f : -1.0f, ls = 0.09f + 0.05f * lr;
                        const float L0[3] = {cur[0], cur[1] + 0.05f, cur[2]};
                        const float L1[3] = {cur[0] + along[0] * ls * sd + e.out[0] * 0.04f, cur[1] + ls * 0.6f,
                                             cur[2] + along[1] * ls * sd + e.out[1] * 0.04f};
                        const float L2[3] = {cur[0] + along[0] * ls * 1.6f * sd, cur[1] - ls * 0.2f, cur[2] + along[1] * ls * 1.6f * sd};
                        const float L3[3] = {cur[0] + along[0] * ls * 0.6f * sd - e.out[0] * 0.03f, cur[1] - ls * 0.5f,
                                             cur[2] + along[1] * ls * 0.6f * sd - e.out[1] * 0.03f};
                        solidTri(L0, L1, L2, leafC);
                        solidTri(L0, L2, L3, leafC);
                        std::memcpy(prev, cur, sizeof(prev));
                    }
                }
            }
            // OFFERINGS on the front steps: by one or two of the openings, melted candles burning, wilted flowers
            // and a few stones
            const size_t nOff = rm.edges.size() >= 6 ? 2 : 1;
            for (size_t o = 0; o < nOff; ++o)
            {
                const RoomEdge &e = rm.edges[size_t(hash(uint32_t(ri) * 53u + uint32_t(o) * 17u) * float(rm.edges.size())) % rm.edges.size()];
                const float base[3] = {e.in[0] + e.out[0] * 0.95f, e.outY, e.in[2] + e.out[1] * 0.95f};
                const float along[2] = {-e.out[1], e.out[0]};
                const float wax[3] = {150.0f, 140.0f, 112.0f};
                for (int k = 0; k < 3; ++k)
                {
                    const float hk = hash(uint32_t(ri) * 71u + uint32_t(o) * 29u + uint32_t(k) * 11u);
                    const float cp[3] = {base[0] + along[0] * (float(k) - 1.0f) * 0.22f + e.out[0] * hk * 0.1f, base[1],
                                         base[2] + along[1] * (float(k) - 1.0f) * 0.22f + e.out[1] * hk * 0.1f};
                    const float h = 0.12f + 0.25f * hk, r = 0.035f;
                    // a six-sided candle
                    for (int s = 0; s < 6; ++s)
                    {
                        const float a0 = 1.0471976f * float(s), a1 = 1.0471976f * float(s + 1);
                        const float P0[3] = {cp[0] + std::cos(a0) * r * 1.3f, cp[1], cp[2] + std::sin(a0) * r * 1.3f};
                        const float P1[3] = {cp[0] + std::cos(a1) * r * 1.3f, cp[1], cp[2] + std::sin(a1) * r * 1.3f};
                        const float Q0[3] = {cp[0] + std::cos(a0) * r, cp[1] + h, cp[2] + std::sin(a0) * r};
                        const float Q1[3] = {cp[0] + std::cos(a1) * r, cp[1] + h, cp[2] + std::sin(a1) * r};
                        solidTri(P0, P1, Q1, wax);
                        solidTri(P0, Q1, Q0, wax);
                        const float T[3] = {cp[0], cp[1] + h, cp[2]};
                        solidTri(Q0, Q1, T, wax);
                    }
                    const float fp[3] = {cp[0], cp[1] + h + 0.06f, cp[2]};
                    const float fl = 0.8f + 0.12f * std::sin(t * 9.0f + hk * 40.0f) + 0.08f * std::sin(t * 23.0f + hk * 13.0f);
                    const float core[3] = {255.0f, 200.0f, 110.0f}, halo[3] = {255.0f, 150.0f, 60.0f};
                    glowPoint(fp, 0.045f, core, 220.0f * fl);
                    glowPoint(fp, 0.3f, halo, 26.0f * fl);
                }
                // wilted flowers: a bent stem and a drooping dull head
                for (int k = 0; k < 2; ++k)
                {
                    const float hk = hash(uint32_t(ri) * 37u + uint32_t(o) * 41u + uint32_t(k) * 19u);
                    const float fp[3] = {base[0] + along[0] * (k ? 0.45f : -0.45f) + e.out[0] * 0.15f, base[1],
                                         base[2] + along[1] * (k ? 0.45f : -0.45f) + e.out[1] * 0.15f};
                    const float stem[3] = {40.0f, 46.0f, 26.0f};
                    const float tipP[3] = {fp[0] + e.out[0] * 0.12f, fp[1] + 0.28f, fp[2] + e.out[1] * 0.12f};
                    const float A[3] = {fp[0] - along[0] * 0.012f, fp[1], fp[2] - along[1] * 0.012f};
                    const float B[3] = {fp[0] + along[0] * 0.012f, fp[1], fp[2] + along[1] * 0.012f};
                    solidTri(A, B, tipP, stem);
                    static const float heads[3][3] = {{70, 40, 60}, {80, 60, 35}, {60, 30, 30}};
                    const float *hc = heads[int(hk * 3.0f) % 3];
                    for (int pt = 0; pt < 5; ++pt)
                    {
                        const float a0 = 1.2566371f * float(pt), a1 = a0 + 0.9f;
                        const float P0[3] = {tipP[0] + std::cos(a0) * 0.06f, tipP[1] - 0.05f, tipP[2] + std::sin(a0) * 0.06f};
                        const float P1[3] = {tipP[0] + std::cos(a1) * 0.06f, tipP[1] - 0.05f, tipP[2] + std::sin(a1) * 0.06f};
                        solidTri(tipP, P0, P1, hc);
                    }
                }
                // a few stones
                for (int k = 0; k < 3; ++k)
                {
                    const float hk = hash(uint32_t(ri) * 83u + uint32_t(o) * 7u + uint32_t(k) * 23u);
                    const float sc[3] = {base[0] + along[0] * (hk - 0.5f) * 0.9f + e.out[0] * 0.3f, base[1] + 0.03f,
                                         base[2] + along[1] * (hk - 0.5f) * 0.9f + e.out[1] * 0.3f};
                    const float s = 0.04f + 0.04f * hk;
                    const float grey[3] = {70.0f, 70.0f, 68.0f};
                    const float px2[3] = {sc[0] + s, sc[1], sc[2]}, nx2[3] = {sc[0] - s, sc[1], sc[2]};
                    const float pz2[3] = {sc[0], sc[1], sc[2] + s}, nz2[3] = {sc[0], sc[1], sc[2] - s};
                    const float up2[3] = {sc[0], sc[1] + s * 0.8f, sc[2]};
                    solidTri(px2, pz2, up2, grey);
                    solidTri(pz2, nx2, up2, grey);
                    solidTri(nx2, nz2, up2, grey);
                    solidTri(nz2, px2, up2, grey);
                }
            }
        }
    // COFFINS (the standing coffins enemies burst out of): drawn as moving objects (lit by real lights), not
    // characters, as tall as a man, narrow. Worn: deeper grain, shining iron; a green light leaks from their seams with
    // mist creeping out at the foot; and when one bursts open (its lid flies), splinters and a cloud of dust.
    if (g_coffins > 0.0f && out.inLevel)
    {
        struct Seen
        {
            uint32_t version, count;
            float c[3];
        };
        static std::vector<Seen> last;
        std::vector<Seen> now;
        struct Coffin
        {
            float lo[3], hi[3];
        };
        std::vector<Coffin> coffins;
        for (Op &op : out.ops)
        {
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || !op.shiny || op.skinned ||
                op.generated || op.abe || op.count < 6 || op.count > 160)
                continue;
            float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
            for (uint32_t i = op.first; i < op.first + op.count; ++i)
                for (int k = 0; k < 3; ++k)
                {
                    lo[k] = std::min(lo[k], out.verts[i].world[k]);
                    hi[k] = std::max(hi[k], out.verts[i].world[k]);
                }
            const float sy = hi[1] - lo[1], sxz = std::max(hi[0] - lo[0], hi[2] - lo[2]);
            if (sy < 2.8f || sy > 5.0f || sxz > 5.0f)
                continue;
            const float dx = (lo[0] + hi[0]) * 0.5f - out.camPos[0], dz = (lo[2] + hi[2]) * 0.5f - out.camPos[2];
            if (dx * dx + dz * dz > 45.0f * 45.0f)
                continue;
            op.relief = std::max(op.relief, g_relief * 1.4f);
            op.metal = std::max(op.metal, 0.6f);
            now.push_back({op.version, op.count, {(lo[0] + hi[0]) * 0.5f, (lo[1] + hi[1]) * 0.5f, (lo[2] + hi[2]) * 0.5f}});
            bool merged = false;
            for (Coffin &cf : coffins)
                if (std::fabs((cf.lo[0] + cf.hi[0]) * 0.5f - now.back().c[0]) < 2.0f &&
                    std::fabs((cf.lo[2] + cf.hi[2]) * 0.5f - now.back().c[2]) < 2.0f)
                {
                    for (int k = 0; k < 3; ++k)
                    {
                        cf.lo[k] = std::min(cf.lo[k], lo[k]);
                        cf.hi[k] = std::max(cf.hi[k], hi[k]);
                    }
                    merged = true;
                }
            if (!merged)
                coffins.push_back({{lo[0], lo[1], lo[2]}, {hi[0], hi[1], hi[2]}});
        }
        for (size_t ci = 0; ci < coffins.size(); ++ci)
        {
            const Coffin &cf = coffins[ci];
            const float cx = (cf.lo[0] + cf.hi[0]) * 0.5f, cz = (cf.lo[2] + cf.hi[2]) * 0.5f;
            const float hc = hash(uint32_t(cx * 11.0f) * 7u + uint32_t(cz * 13.0f));
            const float pulse = 0.75f + 0.25f * std::sin(t * 1.3f + hc * 20.0f);
            const float green[3] = {90.0f, 255.0f, 130.0f};
            // the seams: a few small glows up its front, a soft light at its foot
            for (int k = 0; k < 4; ++k)
            {
                const float hk = hash(uint32_t(ci) * 17u + uint32_t(k) * 5u + uint32_t(hc * 1000.0f));
                const float p[3] = {cx + (hk - 0.5f) * (cf.hi[0] - cf.lo[0]) * 0.8f, cf.lo[1] + 0.3f + hk * (cf.hi[1] - cf.lo[1]) * 0.8f,
                                    cz + (hash(uint32_t(k) * 3u + uint32_t(ci)) - 0.5f) * (cf.hi[2] - cf.lo[2]) * 0.8f};
                glowPoint(p, 0.25f, green, 26.0f * pulse * g_coffins);
            }
            const float foot[3] = {cx, cf.lo[1] + 0.2f, cz};
            glowPoint(foot, 1.3f, green, 16.0f * pulse * g_coffins);
            // mist creeping out at its foot
            for (int k = 0; k < 4; ++k)
            {
                const float hk = hash(uint32_t(ci) * 23u + uint32_t(k) * 7u + uint32_t(hc * 997.0f));
                const float life = 5.0f + 2.0f * hk, ph = std::fmod(t / life + hk, 1.0f);
                const float a = hk * 6.2831853f;
                const float mp[3] = {cx + std::cos(a) * (0.4f + ph * 1.6f), cf.lo[1] + 0.2f + ph * 0.3f, cz + std::sin(a) * (0.4f + ph * 1.6f)};
                const float mist[3] = {100.0f, 140.0f, 115.0f};
                glowPoint(mp, 0.5f + 0.5f * ph, mist, 12.0f * std::sin(ph * 3.1415927f) * g_coffins);
            }
        }
        // BURSTING OPEN: one of their pieces (the lid) jumping since the last frame
        struct Shard
        {
            float p[3], v[3], born, spin;
            bool dust;
        };
        static std::vector<Shard> shards;
        static float lastBurstT = -10.0f, prevT = 0.0f;
        const float dtF = std::clamp(t - prevT, 0.0f, 0.1f);
        prevT = t;
        for (const Seen &s : now)
            for (const Seen &l : last)
                if (s.version == l.version && s.count == l.count)
                {
                    const float d = std::fabs(s.c[0] - l.c[0]) + std::fabs(s.c[1] - l.c[1]) + std::fabs(s.c[2] - l.c[2]);
                    if (d > 0.35f && d < 4.0f && t - lastBurstT > 1.5f)
                    {
                        lastBurstT = t;
                        for (int k = 0; k < int(22.0f * g_coffins); ++k)
                        {
                            const uint32_t sd = uint32_t(t * 1000.0f) * 13u + uint32_t(k) * 7u;
                            const float a = hash(sd) * 6.2831853f, sp = 2.0f + 4.0f * hash(sd + 1u);
                            shards.push_back({{l.c[0], l.c[1], l.c[2]},
                                              {std::cos(a) * sp, 2.0f + 4.0f * hash(sd + 2u), std::sin(a) * sp},
                                              t, hash(sd + 3u) * 20.0f, false});
                        }
                        for (int k = 0; k < 10; ++k)
                        {
                            const float a = 6.2831853f * float(k) / 10.0f;
                            shards.push_back({{l.c[0], l.c[1] - 1.0f, l.c[2]}, {std::cos(a) * 1.2f, 0.5f, std::sin(a) * 1.2f}, t, 0.0f, true});
                        }
                    }
                }
        last.swap(now);
        for (Shard &s : shards)
        {
            if (!s.dust)
                s.v[1] -= 9.0f * dtF;
            else
                for (float &v : s.v)
                    v *= std::max(0.0f, 1.0f - 2.0f * dtF);
            for (int k = 0; k < 3; ++k)
                s.p[k] += s.v[k] * dtF;
        }
        shards.erase(std::remove_if(shards.begin(), shards.end(),
                                    [&](const Shard &s) { return t < s.born || t - s.born > (s.dust ? 1.4f : 1.2f); }),
                     shards.end());
        for (const Shard &s : shards)
        {
            const float age = t - s.born;
            if (s.dust)
            {
                const float rgb[3] = {95.0f, 88.0f, 78.0f};
                glowPoint(s.p, 0.3f + age * 0.9f, rgb, 30.0f * (1.0f - age / 1.4f));
            }
            else
            {
                // a splinter: a thin tumbling sliver of dark wood
                const float a = s.spin + age * 15.0f;
                const float dir[3] = {std::cos(a) * 0.14f, std::sin(a) * 0.14f, std::cos(a * 0.7f) * 0.06f};
                const float wood[3] = {70.0f, 48.0f, 30.0f};
                const float A[3] = {s.p[0] - dir[0], s.p[1] - dir[1], s.p[2] - dir[2]};
                const float B[3] = {s.p[0] + dir[0], s.p[1] + dir[1], s.p[2] + dir[2]};
                const float C[3] = {s.p[0] + 0.02f, s.p[1] + 0.03f, s.p[2] - 0.02f};
                solidTri(A, B, C, wood);
            }
        }
    }
    // FIREFLIES
    if (g_fireflies > 0.0f && out.haveGround)
    {
        constexpr float kBox = 44.0f;
        const int n = int(45.0f * g_fireflies);
        int lights = 0;
        for (int i = 0; i < n; ++i)
        {
            const float h1 = hash(uint32_t(i) * 7u + 11u), h2 = hash(uint32_t(i) * 7u + 12u), h3 = hash(uint32_t(i) * 7u + 13u);
            const float lo0 = centre[0] - kBox * 0.5f, lo2 = centre[2] - kBox * 0.5f;
            const float fx = (h1 * kBox - lo0) / kBox, fz = (h2 * kBox - lo2) / kBox;
            float p[3] = {lo0 + (fx - std::floor(fx)) * kBox + std::sin(t * 0.31f + h3 * 40.0f) * 1.6f,
                          out.groundY + 0.6f + 2.4f * h3 + std::sin(t * 0.7f + h1 * 20.0f) * 0.4f,
                          lo2 + (fz - std::floor(fz)) * kBox + std::cos(t * 0.27f + h2 * 40.0f) * 1.6f};
            const float blink = std::clamp(std::sin(t * (0.6f + 0.5f * h2) + h1 * 30.0f) * 1.6f - 0.2f, 0.0f, 1.0f);
            if (blink <= 0.02f)
                continue;
            const float rgb[3] = {190.0f, 235.0f, 80.0f};
            glowPoint(p, 0.14f, rgb, 110.0f * blink);
            if (lights < 2 && blink > 0.5f)
            {
                DrawList::PointLight L;
                std::memcpy(L.pos, p, sizeof(p));
                L.col[0] = 0.25f;
                L.col[1] = 0.35f;
                L.col[2] = 0.1f;
                L.radius = 5.0f;
                L.weight = 0.0f;
                L.flicker = 0.0f;
                if (out.lights.size() < kMaxPointLights)
                {
                    out.lights.push_back(L);
                    ++lights;
                }
            }
        }
    }
    // FOOTPRINTS AND SPLASHES: in the rain, Maximo leaves dark wet prints behind him that fade away, and his steps
    // kick up little splashes
    static std::vector<Vtx> prints; // (see-through: blended over the ground)
    prints.clear();
    if (g_footprints > 0.0f && out.raining && out.havePlayer)
    {
        struct Print
        {
            float x, y, z, yaw, born;
        };
        struct Drop
        {
            float p[3], v[3], born;
        };
        static std::vector<Print> steps;
        static std::vector<Drop> drops;
        static float last[3] = {1e30f, 0.0f, 0.0f}, walked = 0.0f, prevT = 0.0f;
        static bool left = false;
        const float *pl = out.player;
        const float dtF = std::clamp(t - prevT, 0.0f, 0.1f);
        prevT = t;
        if (last[0] > 1e29f || std::fabs(pl[0] - last[0]) + std::fabs(pl[2] - last[2]) > 6.0f)
        {
            std::memcpy(last, pl, sizeof(last)); // (a jump in place: a cut or a new level)
            walked = 0.0f;
        }
        const float mx = pl[0] - last[0], mz = pl[2] - last[2];
        const float moved = std::sqrt(mx * mx + mz * mz);
        const bool onGround = pl[1] < out.groundY + 0.35f;
        if (moved > 0.02f)
        {
            walked += moved;
            const float yaw = std::atan2(mz, mx);
            if (walked > 1.0f && onGround && !g_floor.inLava(pl[0], pl[2], pl[1]))
            {
                walked = 0.0f;
                left = !left;
                const float side = left ? 0.22f : -0.22f;
                steps.push_back({pl[0] - std::sin(yaw) * side, pl[1] + 0.03f, pl[2] + std::cos(yaw) * side, yaw, t});
                if (steps.size() > 48)
                    steps.erase(steps.begin());
                // a splash: a few drops thrown up and out
                for (int k = 0; k < 6; ++k)
                {
                    const float a = hash(uint32_t(t * 977.0f) + uint32_t(k) * 13u) * 6.2831853f;
                    const float sp = 0.8f + 1.2f * hash(uint32_t(t * 613.0f) + uint32_t(k) * 7u);
                    drops.push_back({{steps.back().x, pl[1] + 0.05f, steps.back().z},
                                     {std::cos(a) * sp + mx / std::max(dtF, 1e-3f) * 0.2f, 2.0f + 1.5f * hash(uint32_t(k) * 31u + uint32_t(t * 100.0f)),
                                      std::sin(a) * sp + mz / std::max(dtF, 1e-3f) * 0.2f},
                                     t});
                }
                if (drops.size() > 120)
                    drops.erase(drops.begin(), drops.begin() + (drops.size() - 120));
            }
            std::memcpy(last, pl, sizeof(last));
        }
        // the drops fly and fall
        for (Drop &d : drops)
        {
            d.v[1] -= 9.0f * dtF;
            for (int k = 0; k < 3; ++k)
                d.p[k] += d.v[k] * dtF;
        }
        drops.erase(std::remove_if(drops.begin(), drops.end(), [&](const Drop &d) { return t - d.born > 0.6f || t < d.born; }),
                    drops.end());
        for (const Drop &d : drops)
        {
            const float a = 1.0f - (t - d.born) / 0.6f;
            const float rgb[3] = {170.0f, 185.0f, 210.0f};
            glowPoint(d.p, 0.035f, rgb, 90.0f * a);
        }
        // the prints: dark ovals on the ground, fading over twenty seconds
        steps.erase(std::remove_if(steps.begin(), steps.end(), [&](const Print &s) { return t - s.born > 20.0f || t < s.born; }),
                    steps.end());
        for (const Print &s : steps)
        {
            const float a = (1.0f - (t - s.born) / 20.0f) * 85.0f * std::min(1.0f, g_footprints);
            const float c = std::cos(s.yaw), sn = std::sin(s.yaw);
            const float L = 0.24f, W = 0.12f;
            const float corner[4][2] = {{-L, -W}, {L, -W * 1.2f}, {L, W * 1.2f}, {-L, W}};
            Vtx q[4];
            for (int j = 0; j < 4; ++j)
            {
                Vtx &o = q[j];
                o = blank;
                o.world[0] = s.x + c * corner[j][0] - sn * corner[j][1];
                o.world[1] = s.y;
                o.world[2] = s.z + sn * corner[j][0] + c * corner[j][1];
                o.col[0] = o.raw[0] = 8.0f;
                o.col[1] = o.raw[1] = 7.0f;
                o.col[2] = o.raw[2] = 6.0f;
                o.col[3] = a;
                o.normal[1] = 1.0f;
                toClip(o.world, o.pos);
            }
            for (int j : {0, 1, 2, 0, 2, 3})
                prints.push_back(q[j]);
        }
    }
    // CONTACT SHADOWS: under every character a soft dark spot on the floor, smaller and fainter the higher it is
    // off the ground (a jump), so they stand on the ground instead of floating over it
    const std::vector<CharBlob> chars = findCharacters(out);
    if (g_contactShadows > 0.0f)
        for (const CharBlob &b : chars)
        {
            const float cx = (b.lo[0] + b.hi[0]) * 0.5f, cz = (b.lo[2] + b.hi[2]) * 0.5f;
            const float dx = cx - out.camPos[0], dz = cz - out.camPos[2];
            if (dx * dx + dz * dz > 45.0f * 45.0f || b.verts < 60)
                continue;
            float floorY = g_floor.heightNear(cx, cz, b.lo[1], 4.0f, 0.4f);
            if (floorY < -1e29f)
                floorY = b.lo[1];
            const float h = std::max(0.0f, b.lo[1] - floorY);
            const float ext = std::max(b.hi[0] - b.lo[0], b.hi[2] - b.lo[2]);
            const float r = std::clamp(ext * 0.55f, 0.45f, 2.6f) * (1.0f + h * 0.12f);
            const float a = 75.0f * std::clamp(1.0f - h / 4.0f, 0.0f, 1.0f) * std::min(1.0f, g_contactShadows) *
                            (b.seeThrough * 2 > b.verts ? 0.35f : 1.0f); // (ghosts: faint)
            if (a <= 1.0f)
                continue;
            Vtx c = blank;
            c.world[0] = cx;
            c.world[1] = floorY + 0.04f;
            c.world[2] = cz;
            c.col[3] = a;
            c.normal[1] = 1.0f;
            toClip(c.world, c.pos);
            constexpr int kSeg = 14;
            Vtx rim[kSeg + 1];
            for (int k = 0; k <= kSeg; ++k)
            {
                const float an = 6.2831853f * float(k) / float(kSeg);
                rim[k] = c;
                rim[k].world[0] = cx + std::cos(an) * r;
                rim[k].world[2] = cz + std::sin(an) * r;
                rim[k].col[3] = 0.0f;
                toClip(rim[k].world, rim[k].pos);
            }
            // (an inner ring at half the darkness, so it fades softly)
            Vtx mid[kSeg + 1];
            for (int k = 0; k <= kSeg; ++k)
            {
                mid[k] = rim[k];
                for (int q = 0; q < 3; q += 2)
                    mid[k].world[q] = c.world[q] + (rim[k].world[q] - c.world[q]) * 0.55f;
                mid[k].col[3] = a * 0.6f;
                toClip(mid[k].world, mid[k].pos);
            }
            for (int k = 0; k < kSeg; ++k)
            {
                prints.push_back(c);
                prints.push_back(mid[k]);
                prints.push_back(mid[k + 1]);
                prints.push_back(mid[k]);
                prints.push_back(rim[k]);
                prints.push_back(rim[k + 1]);
                prints.push_back(mid[k]);
                prints.push_back(rim[k + 1]);
                prints.push_back(mid[k + 1]);
            }
        }
    // HIT SPARKS AND LANDING DUST: where a new flash of light bursts out next to Maximo (a blow landing), a spray
    // of sparks; where he lands from a jump, a ring of dust
    if (g_hitFx > 0.0f && out.inLevel)
    {
        struct Spark
        {
            float p[3], v[3], born, life;
            bool dust;
        };
        static std::vector<Spark> sparks;
        static std::deque<std::vector<std::array<float, 3>>> prevFlashes; // (the last ten frames')
        static float prevT = 0.0f, lastBurst = -10.0f, airborneSince = -1.0f;
        const float dtF = std::clamp(t - prevT, 0.0f, 0.1f);
        prevT = t;
        std::vector<std::array<float, 3>> flashes;
        for (const Op &op : out.ops)
        {
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.generated || !op.abe ||
                op.count < 3)
                continue;
            const uint32_t A = op.alphaReg & 3, B = (op.alphaReg >> 2) & 3, D = (op.alphaReg >> 6) & 3;
            if (!(A == 0 && B == 2 && D == 1))
                continue;
            float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
            for (uint32_t i = op.first; i < op.first + op.count; ++i)
                for (int k = 0; k < 3; ++k)
                {
                    lo[k] = std::min(lo[k], out.verts[i].world[k]);
                    hi[k] = std::max(hi[k], out.verts[i].world[k]);
                }
            const float ext = std::max({hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]});
            if (ext > 4.0f || ext < 0.9f)
                continue; // (a blow's burst of light: not a huge glow, not a little flame of a burning sword)
            flashes.push_back({(lo[0] + hi[0]) * 0.5f, (lo[1] + hi[1]) * 0.5f, (lo[2] + hi[2]) * 0.5f});
        }
        if (out.havePlayer)
            for (const auto &f : flashes)
            {
                // (new: nothing like it near there for the last ten frames; a burning sword's flames come and go
                // all the time, and kept throwing sparks)
                bool seen = false;
                for (const auto &fr : prevFlashes)
                    for (const auto &q : fr)
                        seen = seen || std::fabs(q[0] - f[0]) + std::fabs(q[1] - f[1]) + std::fabs(q[2] - f[2]) < 3.0f;
                const float dx = f[0] - out.player[0], dz = f[2] - out.player[2];
                if (seen || dx * dx + dz * dz > 5.0f * 5.0f || t - lastBurst < 0.4f)
                    continue;
                lastBurst = t;
                const int n = int(14.0f * g_hitFx);
                for (int k = 0; k < n; ++k)
                {
                    const uint32_t sd = uint32_t(t * 1000.0f) * 31u + uint32_t(k) * 7u;
                    const float a = hash(sd) * 6.2831853f, el = hash(sd + 1u) * 1.2f - 0.2f, sp = 3.0f + 4.0f * hash(sd + 2u);
                    sparks.push_back({{f[0], f[1], f[2]},
                                      {std::cos(a) * std::cos(el) * sp, std::sin(el) * sp + 1.5f, std::sin(a) * std::cos(el) * sp},
                                      t, 0.3f + 0.25f * hash(sd + 3u), false});
                }
            }
        prevFlashes.push_front(std::move(flashes));
        while (prevFlashes.size() > 10)
            prevFlashes.pop_back();
        // landing: in the air for a moment (a quarter of a second or more), then back on the ground
        if (out.havePlayer)
        {
            const bool air = out.player[1] > out.groundY + 0.8f;
            if (air && airborneSince < 0.0f)
                airborneSince = t;
            else if (!air && airborneSince >= 0.0f)
            {
                if (t - airborneSince > 0.25f && t - airborneSince < 5.0f)
                {
                    const int n = int(16.0f * g_hitFx);
                    for (int k = 0; k < n; ++k)
                    {
                        const float a = 6.2831853f * (float(k) + hash(uint32_t(t * 100.0f) + uint32_t(k))) / float(n);
                        const float sp = 1.2f + 0.8f * hash(uint32_t(k) * 17u + uint32_t(t * 50.0f));
                        sparks.push_back({{out.player[0] + std::cos(a) * 0.3f, out.player[1] + 0.1f, out.player[2] + std::sin(a) * 0.3f},
                                          {std::cos(a) * sp, 0.35f, std::sin(a) * sp},
                                          t, 0.9f, true});
                    }
                }
                airborneSince = -1.0f;
            }
        }
        for (Spark &k : sparks)
        {
            if (k.dust)
            {
                for (int q = 0; q < 3; ++q)
                    k.v[q] *= std::max(0.0f, 1.0f - 2.5f * dtF);
            }
            else
                k.v[1] -= 9.0f * dtF;
            for (int q = 0; q < 3; ++q)
                k.p[q] += k.v[q] * dtF;
        }
        sparks.erase(std::remove_if(sparks.begin(), sparks.end(), [&](const Spark &k) { return t - k.born > k.life || t < k.born; }),
                     sparks.end());
        if (sparks.size() > 400)
            sparks.erase(sparks.begin(), sparks.begin() + long(sparks.size() - 400));
        for (const Spark &k : sparks)
        {
            const float age = (t - k.born) / k.life;
            if (k.dust)
            {
                const float rgb[3] = {95.0f, 90.0f, 82.0f};
                glowPoint(k.p, 0.12f + 0.35f * age, rgb, 30.0f * (1.0f - age));
            }
            else
            {
                const float rgb[3] = {255.0f, 210.0f - 90.0f * age, 120.0f - 90.0f * age};
                glowPoint(k.p, 0.035f, rgb, 200.0f * (1.0f - age));
                const float tail[3] = {k.p[0] - k.v[0] * 0.025f, k.p[1] - k.v[1] * 0.025f, k.p[2] - k.v[2] * 0.025f};
                glowPoint(tail, 0.028f, rgb, 120.0f * (1.0f - age));
            }
        }
    }
    // GHOST TRAILS: see-through characters (ghosts, spirits) leave fading copies of themselves where they were a
    // moment ago
    static std::deque<std::vector<std::pair<Op, std::vector<Vtx>>>> ghostHistory;
    std::vector<std::pair<Op, std::vector<Vtx>>> ghostNow;
    struct GhostTrack
    {
        float p[3];
        int frames;
        uint64_t seen;
    };
    static std::vector<GhostTrack> ghostTracks;
    if (g_ghostTrails > 0.0f)
    {
        // how long each see-through character has been see-through (followed from frame to frame)
        const uint64_t now = g_tracker.frameNo;
        for (const CharBlob &b : chars)
        {
            if (b.seeThrough * 10 <= b.verts * 6)
                continue;
            const float c[3] = {(b.lo[0] + b.hi[0]) * 0.5f, (b.lo[1] + b.hi[1]) * 0.5f, (b.lo[2] + b.hi[2]) * 0.5f};
            GhostTrack *best = nullptr;
            for (auto &tr : ghostTracks)
                if (tr.seen != now && std::fabs(tr.p[0] - c[0]) + std::fabs(tr.p[2] - c[2]) < 3.0f)
                    best = &tr;
            if (!best)
            {
                ghostTracks.push_back({{c[0], c[1], c[2]}, 0, now});
                best = &ghostTracks.back();
            }
            std::memcpy(best->p, c, sizeof(c));
            best->frames = best->seen + 1 >= now ? best->frames + 1 : 1;
            best->seen = now;
        }
        ghostTracks.erase(std::remove_if(ghostTracks.begin(), ghostTracks.end(),
                                         [&](const GhostTrack &tr) { return tr.seen + 2 < now || tr.seen > now; }),
                          ghostTracks.end());
        for (const Op &op : out.ops)
        {
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || !op.skinned || !op.abe ||
                op.generated || op.count < 3)
                continue;
            // (only a character drawn mostly see-through: not Maximo's own see-through bits)
            float c[3] = {};
            for (uint32_t i = op.first; i < op.first + op.count; ++i)
                for (int k = 0; k < 3; ++k)
                    c[k] += out.verts[i].world[k] / float(op.count);
            bool ghost = false;
            for (const CharBlob &b : chars)
                if (c[0] >= b.lo[0] && c[0] <= b.hi[0] && c[1] >= b.lo[1] && c[1] <= b.hi[1] && c[2] >= b.lo[2] &&
                    c[2] <= b.hi[2])
                    ghost = ghost || b.seeThrough * 10 > b.verts * 6;
            // (and one that has been see-through for a while, a real ghost: an enemy fading as it dies, or a
            // pickup's flash, is see-through only for a moment, and its trail broke into wrong textures)
            if (ghost)
            {
                bool steady = false;
                for (const auto &tr : ghostTracks)
                    if (std::fabs(tr.p[0] - c[0]) + std::fabs(tr.p[2] - c[2]) < 3.0f && tr.frames >= 45)
                        steady = true;
                ghost = steady;
            }
            if (!ghost)
                continue;
            ghostNow.emplace_back(op, std::vector<Vtx>(out.verts.begin() + op.first, out.verts.begin() + op.first + op.count));
        }
    }
    ghostHistory.push_front(std::move(ghostNow));
    while (ghostHistory.size() > 13)
        ghostHistory.pop_back();
    std::vector<std::pair<Op, std::vector<Vtx>>> ghostCopies;
    if (g_ghostTrails > 0.0f)
    {
        static const int back[3] = {4, 8, 12};
        static const float fadeK[3] = {0.4f, 0.24f, 0.12f};
        // (a copy only with a texture the same ghost is drawn with right now)
        std::vector<uint32_t> liveVersions;
        if (!ghostHistory.empty())
            for (const auto &g : ghostHistory[0])
                liveVersions.push_back(g.first.version);
        for (int j = 0; j < 3; ++j)
        {
            if (size_t(back[j]) >= ghostHistory.size())
                break;
            for (const auto &g : ghostHistory[size_t(back[j])])
            {
                if (g.first.rtTexture >= 0 || (g.first.textured && g.first.version && !g_tracker.versions.count(g.first.version)) ||
                    std::find(liveVersions.begin(), liveVersions.end(), g.first.version) == liveVersions.end())
                    continue; // (its texture is gone, or drawn this frame)
                std::pair<Op, std::vector<Vtx>> cp = g;
                for (Vtx &v : cp.second)
                {
                    v.col[3] *= fadeK[j] * std::min(1.0f, g_ghostTrails);
                    toClip(v.world, v.pos);
                }
                ghostCopies.push_back(std::move(cp));
            }
        }
    }
    // FALLING LEAVES: now and then a dead leaf drifting down from the branches, rocking from side to side and
    // turning over as it falls (only under trees: where the branches are overhead)
    if (g_fallingLeaves > 0.0f && out.inLevel && g_canopy.valid && out.haveGround)
    {
        constexpr float kBox = 40.0f;
        const int n = int(120.0f * g_fallingLeaves); // (over four tiles)
        static const float cols[4][3] = {{74, 46, 26}, {86, 62, 30}, {62, 40, 28}, {80, 34, 22}};
        for (int i = 0; i < n; ++i)
        {
            const float h1 = hash(uint32_t(i) * 11u + 501u), h2 = hash(uint32_t(i) * 11u + 502u);
            const float fall = 7.0f + 4.0f * h2; // seconds from the branch to the ground
            const float cyc = t / fall + h1;
            const float age = cyc - std::floor(cyc);
            const uint32_t round = uint32_t(int64_t(std::floor(cyc)) & 0xFFFFFF);
            // (fixed in the world while it falls, not carried along with the camera: the area is cut into
            // 40-unit tiles, each leaf in one of the four tiles nearest the camera's view)
            // (each leaf always in tiles of the same parity, so a tile keeps its leaves when the four move on)
            const int bx = int(std::floor(centre[0] / kBox - 0.5f)), bz = int(std::floor(centre[2] / kBox - 0.5f));
            const float tx = float(bx + ((((i & 1) - bx) % 2) + 2) % 2), tz = float(bz + (((((i >> 1) & 1) - bz) % 2) + 2) % 2);
            const uint32_t tileKey = uint32_t(int32_t(tx)) * 73856093u ^ uint32_t(int32_t(tz)) * 19349663u;
            const float x0 = (tx + hash(round * 17u + uint32_t(i) * 5u + tileKey)) * kBox;
            const float z0 = (tz + hash(round * 23u + uint32_t(i) * 7u + tileKey * 3u)) * kBox;
            const float top = g_canopy.at(x0, z0);
            if (top > 1e29f || top < out.groundY + 2.0f || top > out.groundY + 30.0f)
                continue; // (no tree here this time round)
            const float y = top - (top - out.groundY) * age;
            const float rock = std::sin(t * 1.6f + h1 * 30.0f);
            const float p[3] = {x0 + rock * 0.6f + std::sin(t * 0.4f + h2 * 9.0f) * 0.8f, y + std::fabs(rock) * 0.15f,
                                z0 + std::cos(t * 0.5f + h1 * 9.0f) * 0.8f};
            {
                // (none right in front of the lens: a leaf the size of the screen)
                const float cx = p[0] - out.camPos[0], cy = p[1] - out.camPos[1], cz = p[2] - out.camPos[2];
                if (cx * cx + cy * cy + cz * cz < 4.0f * 4.0f)
                    continue;
            }
            const float spin = t * (1.5f + h2) + h1 * 20.0f, tilt = rock * 0.9f;
            const float sz = 0.11f + 0.05f * h2;
            const float ax[3] = {std::cos(spin) * sz, std::sin(tilt) * sz, std::sin(spin) * sz};
            const float az[3] = {-std::sin(spin) * sz * 0.5f, std::cos(tilt) * sz * 0.3f, std::cos(spin) * sz * 0.5f};
            const float *c = cols[std::min(3, int(h2 * 4.0f))];
            const float A[3] = {p[0] - ax[0], p[1] - ax[1], p[2] - ax[2]}, B[3] = {p[0] + az[0], p[1] + az[1], p[2] + az[2]};
            const float C[3] = {p[0] + ax[0], p[1] + ax[1], p[2] + ax[2]}, D[3] = {p[0] - az[0], p[1] - az[1], p[2] - az[2]};
            const float fade = std::clamp((1.0f - age) / 0.05f, 0.0f, 1.0f); // (gone as it reaches the ground)
            const float rgb[3] = {c[0] * fade, c[1] * fade, c[2] * fade};
            if (fade <= 0.05f)
                continue;
            solidTri(A, B, C, rgb);
            solidTri(A, C, D, rgb);
        }
    }
    // ASH: pale flakes floating up and drifting over lava, a faint glow underneath from the heat
    if (g_ash > 0.0f && g_floor.valid && !g_floor.hotCells.empty())
    {
        const auto &cells = g_floor.hotCells;
        const int n = int(50.0f * g_ash);
        for (int i = 0; i < n; ++i)
        {
            const float h1 = hash(uint32_t(i) * 13u + 701u), h2 = hash(uint32_t(i) * 13u + 702u);
            const float life = 6.0f + 4.0f * h2;
            const float cyc = t / life + h1;
            const float age = cyc - std::floor(cyc);
            const uint32_t round = uint32_t(int64_t(std::floor(cyc)) & 0xFFFFFF);
            const auto &c = cells[size_t(hash(uint32_t(i) * 197u + round * 104729u) * float(cells.size())) % cells.size()];
            float p[3] = {c[0] + std::sin(t * 0.5f + h1 * 40.0f) * 1.5f + age * 2.0f, c[1] + 1.0f + age * 7.0f,
                          c[2] + std::cos(t * 0.4f + h2 * 40.0f) * 1.5f + age * 1.2f};
            const float dx = p[0] - centre[0], dz = p[2] - centre[2];
            if (dx * dx + dz * dz > 34.0f * 34.0f)
                continue;
            const float fade = std::clamp(age / 0.15f, 0.0f, 1.0f) * (1.0f - age);
            const float rgb[3] = {150.0f, 140.0f, 130.0f};
            glowPoint(p, 0.035f + 0.03f * h2, rgb, 60.0f * fade);
        }
    }
    // BATS AND CROWS: small dark shapes flapping across the sky over the level now and then: bats flitting in
    // loose circles, crows flying straight across, slower
    if (g_bats > 0.0f && out.inLevel && out.haveGround)
    {
        const float dark[3] = {10.0f, 9.0f, 11.0f};
        auto flyer = [&](const float p[3], const float dir[3], float span, float flap) {
            // a body and two wings beating up and down (tips higher and lower than the body)
            const float side[3] = {-dir[2], 0.0f, dir[0]};
            const float tipY = std::sin(flap) * span * 0.45f;
            const float body = span * 0.18f;
            const float nose[3] = {p[0] + dir[0] * body, p[1], p[2] + dir[2] * body};
            const float tail[3] = {p[0] - dir[0] * body, p[1], p[2] - dir[2] * body};
            for (int sd = -1; sd <= 1; sd += 2)
            {
                const float tip[3] = {p[0] + side[0] * span * 0.5f * float(sd) - dir[0] * span * 0.1f, p[1] + tipY,
                                      p[2] + side[2] * span * 0.5f * float(sd) - dir[2] * span * 0.1f};
                const float elbow[3] = {p[0] + side[0] * span * 0.25f * float(sd), p[1] + tipY * 0.5f + span * 0.04f,
                                        p[2] + side[2] * span * 0.25f * float(sd)};
                solidTri(nose, elbow, tail, dark);
                solidTri(elbow, tip, tail, dark);
            }
        };
        const int bats = int(7.0f * g_bats);
        for (int i = 0; i < bats; ++i)
        {
            const float h1 = hash(uint32_t(i) * 7u + 901u), h2 = hash(uint32_t(i) * 7u + 902u), h3 = hash(uint32_t(i) * 7u + 903u);
            // a loose circle around a point that wanders slowly; out of sight now and then (half the time)
            const float on = std::sin(t * 0.05f + h1 * 20.0f);
            if (on < 0.0f)
                continue;
            const float r = 7.0f + 10.0f * h2;
            const float ang = t * (0.5f + 0.4f * h3) * (h1 > 0.5f ? 1.0f : -1.0f) + h1 * 30.0f;
            const float cx = centre[0] + std::sin(t * 0.07f + h2 * 10.0f) * 12.0f, cz = centre[2] + std::cos(t * 0.06f + h3 * 10.0f) * 12.0f;
            const float p[3] = {cx + std::cos(ang) * r + std::sin(t * 2.3f + h1 * 9.0f) * 0.8f,
                                out.groundY + 6.0f + 6.0f * h3 + std::sin(t * 1.7f + h2 * 9.0f) * 1.2f,
                                cz + std::sin(ang) * r};
            const float s = h1 > 0.5f ? 1.0f : -1.0f;
            const float dir[3] = {-std::sin(ang) * s, 0.0f, std::cos(ang) * s};
            flyer(p, dir, 0.9f, t * 15.0f + h1 * 6.0f);
        }
        const int crows = int(3.0f * g_bats);
        for (int i = 0; i < crows; ++i)
        {
            const float h1 = hash(uint32_t(i) * 7u + 951u), h2 = hash(uint32_t(i) * 7u + 952u);
            // straight across, 80 units, every 25 to 40 seconds
            const float period = 25.0f + 15.0f * h2;
            const float cyc = t / period + h1;
            const float age = cyc - std::floor(cyc);
            if (age > 0.45f)
                continue; // (off somewhere else for a while)
            const uint32_t round = uint32_t(int64_t(std::floor(cyc)) & 0xFFFFFF);
            const float hd = hash(round * 41u + uint32_t(i)) * 6.2831853f;
            const float dir[3] = {std::cos(hd), 0.0f, std::sin(hd)};
            const float u = age / 0.45f;
            const float p[3] = {centre[0] + dir[0] * (u - 0.5f) * 80.0f + dir[2] * (h2 - 0.5f) * 20.0f,
                                out.groundY + 16.0f + 8.0f * h1,
                                centre[2] + dir[2] * (u - 0.5f) * 80.0f - dir[0] * (h2 - 0.5f) * 20.0f};
            // (beats its wings in bursts, gliding in between)
            const float burst = std::sin(t * 0.8f + h1 * 7.0f) > 0.0f ? t * 6.0f : 0.25f;
            flyer(p, dir, 1.6f, burst + h2 * 3.0f);
        }
    }
    // EMBERS: sparks rising off the lava, drifting and fading as they climb, each starting again from another
    // spot of lava once it has burnt out
    if (g_embers > 0.0f && g_floor.valid && !g_floor.hotCells.empty())
    {
        const auto &cells = g_floor.hotCells;
        const int n = int(90.0f * g_embers);
        int lights = 0;
        for (int i = 0; i < n; ++i)
        {
            const float h1 = hash(uint32_t(i) * 9u + 301u), h2 = hash(uint32_t(i) * 9u + 302u), h3 = hash(uint32_t(i) * 9u + 303u);
            const float life = 2.2f + 1.8f * h2;
            const float cyc = t / life + h1;
            const float age = cyc - std::floor(cyc);
            const uint32_t round = uint32_t(int64_t(std::floor(cyc)) & 0xFFFFFF);
            const auto &c = cells[size_t(hash(uint32_t(i) * 131u + round * 7919u) * float(cells.size())) % cells.size()];
            const float jx = hash(round * 31u + uint32_t(i)) - 0.5f, jz = hash(round * 37u + uint32_t(i) * 3u) - 0.5f;
            const float rise = (2.5f + 2.5f * h3) * age;
            float p[3] = {c[0] + jx * 2.0f + std::sin(t * 1.3f + h3 * 20.0f) * 0.35f * age,
                          c[1] + 0.1f + rise,
                          c[2] + jz * 2.0f + std::cos(t * 1.1f + h1 * 20.0f) * 0.35f * age};
            const float dx = p[0] - centre[0], dz = p[2] - centre[2];
            if (dx * dx + dz * dz > 34.0f * 34.0f)
                continue;
            const float fade = std::clamp(age / 0.12f, 0.0f, 1.0f) * (1.0f - age) * (1.0f - age);
            if (fade <= 0.02f)
                continue;
            const float rgb[3] = {255.0f, 120.0f + 60.0f * (1.0f - age), 30.0f};
            glowPoint(p, 0.08f + 0.07f * h2, rgb, 230.0f * fade);
            (void)lights;
        }
    }
    // DUST
    if (g_dust > 0.0f)
    {
        constexpr float kBox = 26.0f;
        const int n = int(160.0f * g_dust);
        for (int i = 0; i < n; ++i)
        {
            const float h1 = hash(uint32_t(i) * 5u + 101u), h2 = hash(uint32_t(i) * 5u + 102u), h3 = hash(uint32_t(i) * 5u + 103u);
            float lo[3] = {centre[0] - kBox * 0.5f, centre[1] - kBox * 0.3f, centre[2] - kBox * 0.5f};
            const float fx = (h1 * kBox + t * 0.25f - lo[0]) / kBox, fy = (h3 * kBox * 0.6f - t * 0.08f - lo[1]) / (kBox * 0.6f),
                        fz = (h2 * kBox + t * 0.12f - lo[2]) / kBox;
            float p[3] = {lo[0] + (fx - std::floor(fx)) * kBox, lo[1] + (fy - std::floor(fy)) * kBox * 0.6f,
                          lo[2] + (fz - std::floor(fz)) * kBox};
            p[0] += std::sin(t * 0.5f + h3 * 30.0f) * 0.3f;
            const float rgb[3] = {150.0f, 150.0f, 140.0f};
            glowPoint(p, 0.04f + 0.03f * h2, rgb, 40.0f);
        }
    }
    // CHARACTERS: Maximo, the enemies and the ghosts (see findCharacters) get their materials, the enemies glowing
    // eyes, and their bodies rounded and shaded in their folds
    if (out.inLevel && (g_models > 0.0f || g_eyes > 0.0f))
    {
        int ci = 0;
        for (const CharBlob &b : chars)
        {
            ++ci;
            const float cx = (b.lo[0] + b.hi[0]) * 0.5f, cz = (b.lo[2] + b.hi[2]) * 0.5f;
            const float ddx = cx - out.camPos[0], ddz = cz - out.camPos[2];
            if (ddx * ddx + ddz * ddz > 60.0f * 60.0f || b.verts < 60)
                continue;
            const bool ghost = b.seeThrough * 10 > b.verts * 6;
            const bool isPlayer = out.havePlayer && std::fabs(cx - out.player[0]) < 1.5f && std::fabs(cz - out.player[2]) < 1.5f;
            const float hgt = b.hi[1] - b.lo[1];
            const float mode = ghost ? 7.0f : (isPlayer || g_eyes <= 0.0f ? 5.0f : 6.0f);
            const float eyeCol = float(int(hash(uint32_t(ci) * 7u + uint32_t(b.verts)) * 3.0f) % 3);
            for (Op &op : out.ops)
            {
                if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || op.generated || op.count < 3 ||
                    !op.lit)
                    continue;
                // (this character's parts: their middle inside its box)
                const Vtx &v0 = out.verts[op.first];
                if (v0.world[0] < b.lo[0] - 0.3f || v0.world[0] > b.hi[0] + 0.3f || v0.world[1] < b.lo[1] - 0.3f ||
                    v0.world[1] > b.hi[1] + 0.3f || v0.world[2] < b.lo[2] - 0.3f || v0.world[2] > b.hi[2] + 0.3f)
                    continue;
                if (op.look[0] != 0.0f)
                    continue;
                if (g_models > 0.0f || mode == 6.0f)
                {
                    op.look[0] = (g_models > 0.0f || mode != 5.0f) ? mode : 0.0f;
                    op.look[1] = b.hi[1] - hgt * 0.2f; // (the head: the top fifth)
                    op.look[2] = eyeCol;
                }
                if (g_models > 0.0f && !op.abe)
                    op.metal = std::max(op.metal, 0.7f); // (grey parts: armour, blades, buckles)
                // ROUNDER, SHADED IN THE FOLDS: the body's corners welded to give smooth normals (sharp creases
                // kept), each point darkened by how much its neighbours fold in around it, then every triangle
                // split in four on a curved surface through its corners (PN triangles)
                if (g_models <= 0.0f || !op.skinned || op.count % 3 || op.count > 9000 || op.abe)
                    continue;
                const uint32_t n = op.count;
                std::vector<float> fn(size_t(n / 3) * 3);
                std::unordered_map<uint64_t, std::vector<uint32_t>> at;
                at.reserve(n);
                auto qkey = [](const float *w) {
                    const int64_t x = int64_t(std::floor(w[0] * 500.0f)), y = int64_t(std::floor(w[1] * 500.0f)),
                                  z = int64_t(std::floor(w[2] * 500.0f));
                    return uint64_t(x * 73856093) ^ uint64_t(y * 19349663) ^ uint64_t(z * 83492791);
                };
                for (uint32_t tri = 0; tri < n / 3; ++tri)
                {
                    const Vtx &a = out.verts[op.first + tri * 3], &bb = out.verts[op.first + tri * 3 + 1],
                              &c = out.verts[op.first + tri * 3 + 2];
                    float e1[3], e2[3];
                    for (int k = 0; k < 3; ++k)
                    {
                        e1[k] = bb.world[k] - a.world[k];
                        e2[k] = c.world[k] - a.world[k];
                    }
                    fn[tri * 3] = e1[1] * e2[2] - e1[2] * e2[1];
                    fn[tri * 3 + 1] = e1[2] * e2[0] - e1[0] * e2[2];
                    fn[tri * 3 + 2] = e1[0] * e2[1] - e1[1] * e2[0];
                    // (the game's normal says which way is out: the winding may be either way)
                    const float dn = fn[tri * 3] * (a.normal[0] + bb.normal[0] + c.normal[0]) +
                                     fn[tri * 3 + 1] * (a.normal[1] + bb.normal[1] + c.normal[1]) +
                                     fn[tri * 3 + 2] * (a.normal[2] + bb.normal[2] + c.normal[2]);
                    if (dn < 0.0f)
                        for (int k = 0; k < 3; ++k)
                            fn[tri * 3 + k] = -fn[tri * 3 + k];
                    for (int k = 0; k < 3; ++k)
                        at[qkey(out.verts[op.first + tri * 3 + k].world)].push_back(tri);
                }
                std::vector<float> sn(size_t(n) * 3), pn(size_t(n) * 3), ao(n, 0.0f);
                std::unordered_map<uint64_t, int> edgeUse;
                edgeUse.reserve(n);
                auto ekey = [&](uint32_t i, uint32_t j) {
                    uint64_t a = qkey(out.verts[op.first + i].world), c = qkey(out.verts[op.first + j].world);
                    if (a > c)
                        std::swap(a, c);
                    return a * 0x9E3779B97F4A7C15ull ^ c;
                };
                for (uint32_t tri = 0; tri < n / 3; ++tri)
                    for (int k = 0; k < 3; ++k)
                        ++edgeUse[ekey(tri * 3 + k, tri * 3 + (k + 1) % 3)];
                for (uint32_t i = 0; i < n; ++i)
                {
                    const uint32_t tri = i / 3;
                    const float *f0 = &fn[tri * 3];
                    const float l0 = std::sqrt(f0[0] * f0[0] + f0[1] * f0[1] + f0[2] * f0[2]);
                    float s3[3] = {0.0f, 0.0f, 0.0f};
                    float occl = 0.0f;
                    int cnt = 0;
                    const float *p = out.verts[op.first + i].world;
                    const auto &list = at[qkey(p)];
                    for (uint32_t t2 : list)
                    {
                        const float *f = &fn[t2 * 3];
                        const float l = std::sqrt(f[0] * f[0] + f[1] * f[1] + f[2] * f[2]);
                        if (l < 1e-12f || l0 < 1e-12f)
                            continue;
                        if ((f[0] * f0[0] + f[1] * f0[1] + f[2] * f0[2]) / (l * l0) > 0.45f) // (creases kept)
                            for (int k = 0; k < 3; ++k)
                                s3[k] += f[k];
                    }
                    float sl = std::sqrt(s3[0] * s3[0] + s3[1] * s3[1] + s3[2] * s3[2]);
                    if (sl < 1e-12f)
                    {
                        for (int k = 0; k < 3; ++k)
                            s3[k] = out.verts[op.first + i].normal[k];
                        sl = std::max(1e-6f, std::sqrt(s3[0] * s3[0] + s3[1] * s3[1] + s3[2] * s3[2]));
                    }
                    for (int k = 0; k < 3; ++k)
                        sn[size_t(i) * 3 + k] = s3[k] / sl;
                    // (the shape's normal at this point: every face around it, creases or not, so the faces meeting
                    // here all bend their edges the same way and no cracks open)
                    float a3[3] = {0.0f, 0.0f, 0.0f};
                    for (uint32_t t2 : list)
                        for (int k = 0; k < 3; ++k)
                            a3[k] += fn[t2 * 3 + k];
                    const float al = std::sqrt(a3[0] * a3[0] + a3[1] * a3[1] + a3[2] * a3[2]);
                    for (int k = 0; k < 3; ++k)
                        pn[size_t(i) * 3 + k] = al > 1e-12f ? a3[k] / al : sn[size_t(i) * 3 + k];
                    // the fold: the middles of the faces around this point, in front of its normal = folding in
                    for (uint32_t t2 : list)
                    {
                        float cc[3];
                        for (int k = 0; k < 3; ++k)
                            cc[k] = (out.verts[op.first + t2 * 3].world[k] + out.verts[op.first + t2 * 3 + 1].world[k] +
                                     out.verts[op.first + t2 * 3 + 2].world[k]) / 3.0f - p[k];
                        const float cl = std::sqrt(cc[0] * cc[0] + cc[1] * cc[1] + cc[2] * cc[2]);
                        if (cl < 1e-6f)
                            continue;
                        occl += std::max(0.0f, (cc[0] * s3[0] + cc[1] * s3[1] + cc[2] * s3[2]) / (cl * sl));
                        ++cnt;
                    }
                    ao[i] = cnt ? std::min(1.0f, occl / float(cnt) * 2.5f) : 0.0f;
                }
                // (lower down the body a little darker too: the ground's shade on the legs)
                const size_t first = out.verts.size();
                out.verts.reserve(first + size_t(n) * 4);
                auto shadeOf = [&](uint32_t i, float y) {
                    const float low = std::clamp((y - b.lo[1]) / std::max(hgt, 0.1f), 0.0f, 1.0f);
                    return (1.0f - 0.45f * ao[i] * g_models) * (0.82f + 0.18f * std::sqrt(low));
                };
                for (uint32_t tri = 0; tri < n / 3; ++tri)
                {
                    Vtx V[3];
                    float N3[3][3], P3[3][3];
                    bool open[3];
                    for (int k = 0; k < 3; ++k)
                        open[k] = edgeUse[ekey(tri * 3 + k, tri * 3 + (k + 1) % 3)] < 2;
                    for (int k = 0; k < 3; ++k)
                    {
                        V[k] = out.verts[op.first + tri * 3 + k];
                        const float sh = shadeOf(tri * 3 + k, V[k].world[1]);
                        for (int q = 0; q < 3; ++q)
                        {
                            N3[k][q] = sn[size_t(tri * 3 + k) * 3 + q];
                            P3[k][q] = pn[size_t(tri * 3 + k) * 3 + q];
                            V[k].normal[q] = N3[k][q];
                            V[k].col[q] *= sh;
                            V[k].raw[q] *= sh;
                        }
                    }
                    // (a joining triangle with no area, between two strips of the model: kept as it is; split, its
                    // bent edge middles would open it into a long thin sliver across the screen)
                    {
                        float e1[3], e2[3], e3[3];
                        for (int q = 0; q < 3; ++q)
                        {
                            e1[q] = V[1].world[q] - V[0].world[q];
                            e2[q] = V[2].world[q] - V[0].world[q];
                            e3[q] = V[2].world[q] - V[1].world[q];
                        }
                        const float cx = e1[1] * e2[2] - e1[2] * e2[1], cy = e1[2] * e2[0] - e1[0] * e2[2],
                                    cz = e1[0] * e2[1] - e1[1] * e2[0];
                        const float twoA = std::sqrt(cx * cx + cy * cy + cz * cz);
                        const float m2 = std::max({e1[0] * e1[0] + e1[1] * e1[1] + e1[2] * e1[2],
                                                   e2[0] * e2[0] + e2[1] * e2[1] + e2[2] * e2[2],
                                                   e3[0] * e3[0] + e3[1] * e3[1] + e3[2] * e3[2]});
                        if (twoA <= 0.02f * m2 || m2 > hgt * hgt * 0.25f)
                        {
                            // (four copies of itself, so the triangle count stays a multiple of the split's)
                            for (int rep = 0; rep < 4; ++rep)
                                for (int k = 0; k < 3; ++k)
                                    out.verts.push_back(rep == 0 ? V[k] : V[0]);
                            continue;
                        }
                    }
                    // the curved edge middles (cubic Bezier triangle at the edge's half way)
                    auto mid = [&](int i, int j) {
                        Vtx m = V[i];
                        float d[3];
                        for (int q = 0; q < 3; ++q)
                            d[q] = V[j].world[q] - V[i].world[q];
                        const float wij = d[0] * P3[i][0] + d[1] * P3[i][1] + d[2] * P3[i][2];
                        const float wji = -(d[0] * P3[j][0] + d[1] * P3[j][1] + d[2] * P3[j][2]);
                        // (an edge with nothing on its other side: where this part meets another; kept straight)
                        const bool straight = open[(i + 1) % 3 == j ? i : j];
                        const float elen = std::sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
                        float off[3];
                        for (int q = 0; q < 3; ++q)
                        {
                            const float b210 = (2.0f * V[i].world[q] + V[j].world[q] - wij * P3[i][q]) / 3.0f;
                            const float b120 = (2.0f * V[j].world[q] + V[i].world[q] - wji * P3[j][q]) / 3.0f;
                            const float pm = (V[i].world[q] + V[j].world[q]) / 8.0f + (b210 + b120) * 0.375f;
                            off[q] = pm - (V[i].world[q] + V[j].world[q]) * 0.5f;
                        }
                        const float ol = std::sqrt(off[0] * off[0] + off[1] * off[1] + off[2] * off[2]);
                        const float lim = 0.15f * elen; // (never more than a little bulge)
                        const float k = straight ? 0.0f : (ol > lim ? lim / ol : 1.0f);
                        float nm[3];
                        for (int q = 0; q < 3; ++q)
                        {
                            m.world[q] = (V[i].world[q] + V[j].world[q]) * 0.5f + off[q] * k * std::min(1.0f, g_models);
                            m.col[q] = (V[i].col[q] + V[j].col[q]) * 0.5f;
                            m.raw[q] = (V[i].raw[q] + V[j].raw[q]) * 0.5f;
                            m.mnormal[q] = (V[i].mnormal[q] + V[j].mnormal[q]) * 0.5f;
                            nm[q] = N3[i][q] + N3[j][q];
                        }
                        const float nl = std::max(1e-6f, std::sqrt(nm[0] * nm[0] + nm[1] * nm[1] + nm[2] * nm[2]));
                        for (int q = 0; q < 3; ++q)
                            m.normal[q] = nm[q] / nl;
                        m.col[3] = (V[i].col[3] + V[j].col[3]) * 0.5f;
                        m.uv[0] = (V[i].uv[0] + V[j].uv[0]) * 0.5f;
                        m.uv[1] = (V[i].uv[1] + V[j].uv[1]) * 0.5f;
                        toClip(m.world, m.pos);
                        return m;
                    };
                    const Vtx m01 = mid(0, 1), m12 = mid(1, 2), m20 = mid(2, 0);
                    const Vtx *seq[12] = {&V[0], &m01, &m20, &m01, &V[1], &m12, &m20, &m12, &V[2], &m01, &m12, &m20};
                    for (const Vtx *vv : seq)
                        out.verts.push_back(*vv);
                }
                op.first = uint32_t(first);
                op.count = uint32_t(out.verts.size() - first);
            }
        }
    }
    // THE HUD: after the main picture, the 2D pieces drawn over it (the soul orb, the bars, the coins, the boss
    // bar): sharper, the orb alive, gleams on the gold, and a soft shadow under each; nothing of the picture's
    // effects on them (they are drawn after all of it)
    bool gameHudSeen = false;
    g_hudMenu = false;
    if ((g_hud > 0.0f || g_modernHud > 0.0f) && out.inLevel)
    {
        size_t hudStart = 0;
        for (size_t i = 0; i < out.ops.size(); ++i)
            if (out.ops[i].type == OpType::Draw && out.ops[i].target == 0 && out.ops[i].viewId == out.mainView)
                hudStart = i + 1;
#ifdef NATIVE_TEST
        if (std::getenv("NR_THIN"))
            for (size_t i = 0; i < out.ops.size(); ++i)
            {
                const Op &op = out.ops[i];
                if (op.type != OpType::Draw || op.count % 3)
                    continue;
                int thin = 0;
                float worst = 0.0f;
                uint32_t wk = 0;
                for (uint32_t k = op.first; k + 2 < op.first + op.count; k += 3)
                {
                    const Vtx *t = &out.verts[k];
                    if (t[0].pos[3] < 0.5f || t[1].pos[3] < 0.5f || t[2].pos[3] < 0.5f)
                        continue;
                    float P[3][2];
                    for (int q = 0; q < 3; ++q)
                    {
                        P[q][0] = t[q].pos[0] / t[q].pos[3] * 1.78f;
                        P[q][1] = t[q].pos[1] / t[q].pos[3];
                    }
                    float mx = 0.0f;
                    for (int q = 0; q < 3; ++q)
                    {
                        const float dx = P[(q + 1) % 3][0] - P[q][0], dy = P[(q + 1) % 3][1] - P[q][1];
                        mx = std::max(mx, std::sqrt(dx * dx + dy * dy));
                    }
                    const float area = std::fabs((P[1][0] - P[0][0]) * (P[2][1] - P[0][1]) - (P[2][0] - P[0][0]) * (P[1][1] - P[0][1])) * 0.5f;
                    const float width = mx > 0 ? 2.0f * area / mx : 0.0f;
                    if (mx > 0.25f && width < 0.01f && std::fabs(P[0][0]) < 2 && std::fabs(P[0][1]) < 1.2f)
                    {
                        ++thin;
                        if (mx > worst)
                        {
                            worst = mx;
                            wk = k;
                        }
                    }
                }
                if (thin)
                {
                    const Vtx *t = &out.verts[wk];
                    std::printf("  thin: op %zu gen %d skinned %d view %d v%u abe %d look %.0f n %u: %d long thin, longest %.2f at world %.1f %.1f %.1f / %.1f %.1f %.1f / %.1f %.1f %.1f col %.0f %.0f %.0f %.0f\n",
                                i, int(op.generated), int(op.skinned), op.viewId, op.version, int(op.abe), op.look[0], op.count,
                                thin, worst, t[0].world[0], t[0].world[1], t[0].world[2], t[1].world[0], t[1].world[1],
                                t[1].world[2], t[2].world[0], t[2].world[1], t[2].world[2], t[0].col[0], t[0].col[1],
                                t[0].col[2], t[0].col[3]);
                }
            }
        if (std::getenv("NR_OPS"))
            for (size_t i = 0; i < out.ops.size(); ++i)
            {
                const Op &op = out.ops[i];
                if (op.type != OpType::Draw || op.generated)
                    continue;
                float lo[2] = {1e30f, 1e30f}, hi[2] = {-1e30f, -1e30f};
                for (uint32_t k = op.first; k < op.first + op.count; ++k)
                    for (int q = 0; q < 2; ++q)
                    {
                        const float c = out.verts[k].pos[q] / std::max(out.verts[k].pos[3], 1e-6f);
                        lo[q] = std::min(lo[q], c);
                        hi[q] = std::max(hi[q], c);
                    }
                int wild = 0;
                for (uint32_t k = op.first; k < op.first + op.count; ++k)
                    if (std::fabs(out.verts[k].pos[0]) > 20.0f * std::fabs(out.verts[k].pos[3]))
                    {
                        if (wild++ < 3)
                            std::printf("    wild vert %u pos %.3f %.3f %.3f %.5f world %.2f %.2f %.2f col %.0f %.0f %.0f %.0f\n", k,
                                        out.verts[k].pos[0], out.verts[k].pos[1], out.verts[k].pos[2], out.verts[k].pos[3],
                                        out.verts[k].world[0], out.verts[k].world[1], out.verts[k].world[2], out.verts[k].col[0],
                                        out.verts[k].col[1], out.verts[k].col[2], out.verts[k].col[3]);
                    }
                if (wild)
                    std::printf("    %d wild verts; abe %d skinned %d lit %d emissive %d special %d\n", wild, int(op.abe),
                                int(op.skinned), int(op.lit), int(op.emissive), int(op.special));
                std::printf("  op %zu view %d tgt %d tex %d v%u n %u box %.2f %.2f .. %.2f %.2f w0 %.3f\n", i, op.viewId,
                            op.target, int(op.textured), op.version, op.count, lo[0], lo[1], hi[0], hi[1],
                            op.count ? out.verts[op.first].pos[3] : 0.0f);
            }
#endif
        // (no 3D picture this frame, a pause or menu screen or a cutscene's own pictures: left as the game draws it)
        if (hudStart == 0)
            hudStart = out.ops.size();
        std::vector<std::pair<size_t, Op>> shadows;
        std::vector<std::pair<size_t, std::array<float, 4>>> hudBox;
        float orbLo[2] = {1e30f, 1e30f}, orbHi[2] = {-1e30f, -1e30f};
        for (size_t i = hudStart; i < out.ops.size(); ++i)
        {
            Op &op = out.ops[i];
            if (op.type != OpType::Draw || op.target != 0 || !op.textured || op.count < 3 || op.rtTexture >= 0)
                continue;
            float lo[2] = {1e30f, 1e30f}, hi[2] = {-1e30f, -1e30f};
            bool bad = op.viewId == out.mainView || op.generated;
            for (uint32_t k = op.first; k < op.first + op.count && !bad; ++k)
            {
                const Vtx &v = out.verts[k];
                if (v.pos[3] <= 0.0f)
                {
                    bad = true;
                    break;
                }
                for (int q = 0; q < 2; ++q)
                {
                    const float c = v.pos[q] / v.pos[3];
                    lo[q] = std::min(lo[q], c);
                    hi[q] = std::max(hi[q], c);
                }
            }
            const float w = hi[0] - lo[0], h = hi[1] - lo[1];
            if (bad || w * h > 0.6f || w * h <= 0.0f) // (not a full-screen fade or flash)
                continue;
            const auto vt = g_tracker.versions.find(op.version);
            const TexVersion *tv = vt != g_tracker.versions.end() ? &vt->second : nullptr;
            float mode = 1.0f;
#ifdef NATIVE_TEST
            std::printf("  hud op %zu v%u box %.2f %.2f .. %.2f %.2f vivid %.2f warm %.2f bright %.2f n %u\n", i, op.version,
                        lo[0], lo[1], hi[0], hi[1], tv ? tv->vivid : -1.0f, tv ? tv->warm : -1.0f, tv ? tv->bright : -1.0f,
                        op.count);
#endif
            // the orb: square-ish, strongly coloured, in a corner (top left)
            const float cxh = (lo[0] + hi[0]) * 0.5f, cyh = (lo[1] + hi[1]) * 0.5f;
            if (tv && tv->vivid > 0.3f && cxh < -0.6f && std::fabs(cyh) > 0.55f && w > 0.05f && w < 0.4f &&
                h / std::max(w, 1e-4f) > 0.6f && h / std::max(w, 1e-4f) < 3.5f)
                mode = 3.0f;
            else if (tv && tv->warm > 0.12f)
                mode = 4.0f;
            op.look[0] = mode;
            if (mode == 3.0f)
            {
                orbLo[0] = lo[0]; orbLo[1] = lo[1]; orbHi[0] = hi[0]; orbHi[1] = hi[1];
            }
            hudBox.push_back({i, {lo[0], lo[1], hi[0], hi[1]}});
            Op so = op;
            so.look[0] = 2.0f;
            shadows.push_back({i, so});
        }
        // (a screen full of 2D pieces, a menu or the pause screen: left as the game draws it)
        if (hudBox.size() > 40 || g_hud <= 0.0f)
        {
            g_hudMenu = hudBox.size() > 40;
            for (const auto &hb : hudBox)
                out.ops[hb.first].look[0] = 0.0f;
            if (g_hudMenu)
                hudBox.clear();
            shadows.clear();
            orbLo[0] = 1e30f;
            orbHi[0] = -1e30f;
        }
        // MODERN HUD: the game's own HUD at the top left (the orb, the health and power bars) not drawn: the new
        // one takes its place, and is shown only while the game shows its own (not in cutscenes)
        if (g_modernHud > 0.0f && g_hudNow.valid && !g_hudMenu)
        {
            // every 2D draw after the picture (any size: the game draws its orb, ring and bars as one piece that can
            // reach past the screen's edge), triangle by triangle
            for (size_t i = hudStart; i < out.ops.size(); ++i)
            {
                Op &op = out.ops[i];
                if (op.type != OpType::Draw || op.target != 0 || op.generated || op.viewId == out.mainView ||
                    op.count % 3 || op.count < 3)
                    continue;
                bool any = false;
                for (uint32_t k = op.first; k + 2 < op.first + op.count; k += 3)
                {
                    Vtx *tv = &out.verts[k];
                    if (tv[0].pos[3] <= 0.0f || tv[1].pos[3] <= 0.0f || tv[2].pos[3] <= 0.0f)
                        continue;
                    float lo[2] = {1e30f, 1e30f}, hi[2] = {-1e30f, -1e30f}, cc[2] = {0.0f, 0.0f};
                    for (int q = 0; q < 3; ++q)
                        for (int a = 0; a < 2; ++a)
                        {
                            const float v = tv[q].pos[a] / tv[q].pos[3];
                            lo[a] = std::min(lo[a], v);
                            hi[a] = std::max(hi[a], v);
                            cc[a] += v / 3.0f;
                        }
                    if (hi[0] - lo[0] > 0.8f || hi[1] - lo[1] > 0.8f) // (a full-screen fade: not the HUD)
                        continue;
                    // (the orb and its ring come with a height past the screen's bottom edge, yet show at the top:
                    // hidden too; nothing drawn down there is seen anyway)
                    // (and the counters the game shows at the top right when something is picked up: the new HUD
                    // has its own; not in a boss fight, whose bar runs along the top)
                    const bool counter = !g_hudNow.boss && cc[0] > 0.25f && cc[1] < -0.3f && hi[0] - lo[0] < 0.4f;
                    if ((cc[0] < -0.25f && (cc[1] < -0.3f || cc[1] > 1.0f)) || counter)
                    {
                        for (int q = 0; q < 3; ++q)
                            std::memcpy(tv[q].pos, tv[0].pos, sizeof tv[0].pos); // (no area: not drawn)
                        any = true;
                    }
                }
                if (any)
                {
                    gameHudSeen = true;
                    for (size_t k = 0; k < shadows.size();)
                        if (shadows[k].first == i)
                            shadows.erase(shadows.begin() + long(k));
                        else
                            ++k;
                }
            }
        }
        // (the metal ring around the orb gleams like the gold)
        if (orbLo[0] < orbHi[0])
            for (const auto &hb : hudBox)
            {
                Op &op = out.ops[hb.first];
                const float cxh = (hb.second[0] + hb.second[2]) * 0.5f, cyh = (hb.second[1] + hb.second[3]) * 0.5f;
                const float mx = (orbHi[0] - orbLo[0]) * 0.6f, my = (orbHi[1] - orbLo[1]) * 0.6f;
                if (op.look[0] == 1.0f && cxh > orbLo[0] - mx && cxh < orbHi[0] + mx && cyh > orbLo[1] - my && cyh < orbHi[1] + my)
                    op.look[0] = 4.0f;
            }
        // the shadows: each piece again just before itself, moved a little right and down, dark and soft
        for (size_t s = shadows.size(); s-- > 0;)
        {
            Op so = shadows[s].second;
            const uint32_t first = uint32_t(out.verts.size());
            for (uint32_t k = so.first; k < so.first + so.count; ++k)
            {
                Vtx v = out.verts[k];
                v.pos[0] += 0.006f * v.pos[3];
                v.pos[1] += 0.012f * v.pos[3];
                v.col[3] *= 0.55f * std::min(1.0f, g_hud);
                out.verts.push_back(v);
            }
            so.first = first;
            so.test &= ~uint64_t(1); // (soft: no alpha test)
            so.abe = true;
            so.alphaReg = 0x44;
            so.zwrite = false; // (or the piece itself, drawn at the same depth after it, fails its depth test)
            out.ops.insert(out.ops.begin() + long(shadows[s].first), so);
        }
    }
    if (g_modernHud > 0.0f && out.inLevel)
        addModernHud(out, gameHudSeen);
    for (auto &g : ghostCopies)
    {
        Op go = g.first;
        go.first = uint32_t(out.verts.size());
        go.count = uint32_t(g.second.size());
        go.zwrite = false;
        go.casts = false;
        go.generated = true;
        out.verts.insert(out.verts.end(), g.second.begin(), g.second.end());
        out.ops.insert(out.ops.begin() + insertAt + 1, go);
        ++insertAt;
    }
    if (!prints.empty())
    {
        // (footprints and contact shadows: blended over the ground, not writing depth)
        Op po = out.ops[size_t(lastMain)];
        po.first = uint32_t(out.verts.size());
        po.count = uint32_t(prints.size());
        po.textured = false;
        po.version = 0;
        po.abe = true;
        po.alphaReg = 0x44; // Cs * As + Cd * (1 - As)
        po.zwrite = false;
        po.test &= ~uint64_t(1);
        po.casts = po.receives = false;
        po.emissive = true;
        po.lava = po.lit = po.shiny = po.skinned = false;
        po.relief = po.moss = po.metal = po.glow = po.reflect = 0.0f;
        po.generated = true;
        out.verts.insert(out.verts.end(), prints.begin(), prints.end());
        out.ops.insert(out.ops.begin() + insertAt + 1, po);
        ++insertAt;
    }
    if (!solid.empty())
    {
        // (the solid ones first, drawn like the level: depth tested and written)
        Op so = out.ops[size_t(lastMain)];
        so.first = uint32_t(out.verts.size());
        so.count = uint32_t(solid.size());
        so.textured = false;
        so.version = 0;
        so.abe = false;
        so.zwrite = true;
        so.test &= ~uint64_t(1);
        so.casts = so.receives = false;
        so.emissive = true; // (its own colour: no sun, shadows or torch light on it)
        so.lava = so.lit = so.shiny = so.skinned = false;
        so.relief = so.moss = so.metal = so.glow = so.reflect = 0.0f;
        so.generated = true;
        out.verts.insert(out.verts.end(), solid.begin(), solid.end());
        out.ops.insert(out.ops.begin() + insertAt + 1, so);
        ++insertAt;
    }
    if (verts.empty())
        return;
    Op op = out.ops[size_t(lastMain)];
    op.first = uint32_t(out.verts.size());
    op.count = uint32_t(verts.size());
    op.textured = false;
    op.version = 0;
    op.abe = true;
    op.alphaReg = 0x48; // Cs * As + Cd: light added on top
    op.zwrite = false;
    op.test &= ~uint64_t(1);
    op.casts = op.receives = false;
    op.emissive = true;
    op.lava = false;
    op.lit = false;
    op.shiny = false;
    op.relief = op.moss = op.metal = op.glow = op.reflect = 0.0f;
    out.verts.insert(out.verts.end(), verts.begin(), verts.end());
    op.generated = true;
    out.ops.insert(out.ops.begin() + insertAt + 1, op);
}

// SKY: the direction towards the moon, from where the light comes, low enough to be seen over the scenery
void moonDirection(const DrawList &dl, float moon[3])
{
    if (dl.gameMoon && size_t(dl.mainView) < dl.viewScreens.size())
    {
        // the direction of the game's moon, back from its place on screen through the main camera
        const Mat &V = dl.views[dl.mainView];
        const Mat &scr = dl.viewScreens[dl.mainView];
        const float nx = dl.gameMoonUV[0] * 2.0f - 1.0f, ny = dl.gameMoonUV[1] * 2.0f - 1.0f;
        const float dv[3] = {nx * 320.0f / scr.m[0], ny * 112.0f / scr.m[5], 1.0f};
        float w[3];
        for (int k = 0; k < 3; ++k)
            w[k] = V.m[k * 4 + 0] * dv[0] + V.m[k * 4 + 1] * dv[1] + V.m[k * 4 + 2] * dv[2];
        const float l = std::sqrt(w[0] * w[0] + w[1] * w[1] + w[2] * w[2]);
        if (l > 1e-6f)
        {
            for (int k = 0; k < 3; ++k)
                moon[k] = w[k] / l;
            return;
        }
    }
    moon[0] = -dl.lightDir[0];
    moon[1] = 0.0f;
    moon[2] = -dl.lightDir[2];
    float l = std::sqrt(moon[0] * moon[0] + moon[2] * moon[2]);
    if (l < 1e-3f)
    {
        moon[0] = 0.6f;
        moon[2] = 0.8f;
        l = 1.0f;
    }
    const float el = 22.0f * 3.14159265f / 180.0f;
    moon[0] = moon[0] / l * std::cos(el);
    moon[2] = moon[2] / l * std::cos(el);
    moon[1] = std::sin(el);
}

// smooth value noise in 3D (0..1), for where ivy grows
float noise3(float x, float y, float z)
{
    auto h = [](int32_t i, int32_t j, int32_t k) {
        uint32_t n = uint32_t(i) * 73856093u ^ uint32_t(j) * 19349663u ^ uint32_t(k) * 83492791u;
        n = (n ^ (n >> 13)) * 0x5bd1e995u;
        n ^= n >> 15;
        return float(n & 0xFFFFFF) / 16777215.0f;
    };
    const float fx = std::floor(x), fy = std::floor(y), fz = std::floor(z);
    const int32_t ix = int32_t(fx), iy = int32_t(fy), iz = int32_t(fz);
    float tx = x - fx, ty = y - fy, tz = z - fz;
    tx = tx * tx * (3.0f - 2.0f * tx);
    ty = ty * ty * (3.0f - 2.0f * ty);
    tz = tz * tz * (3.0f - 2.0f * tz);
    auto lerp = [](float a, float b, float t) { return a + (b - a) * t; };
    const float x00 = lerp(h(ix, iy, iz), h(ix + 1, iy, iz), tx), x10 = lerp(h(ix, iy + 1, iz), h(ix + 1, iy + 1, iz), tx);
    const float x01 = lerp(h(ix, iy, iz + 1), h(ix + 1, iy, iz + 1), tx);
    const float x11 = lerp(h(ix, iy + 1, iz + 1), h(ix + 1, iy + 1, iz + 1), tx);
    return lerp(lerp(x00, x10, ty), lerp(x01, x11, ty), tz);
}

void addVegetation(DrawList &out)
{
    out.flames.clear();
    const bool grass = g_grass > 0.0f, wind = g_wind > 0.0f, pebbles = g_pebbles > 0.0f, ivy = g_ivy > 0.0f,
               webs = g_cobwebs > 0.0f, props = g_props > 0.0f;
    const bool raining = out.raining && g_lighting.load(std::memory_order_relaxed);
    g_weatherVerts.clear();
    const Mat vs = mul(out.views[out.mainView], out.viewScreens[out.mainView]);
    auto toClip = [&](const float w[3], float pos[4]) {
        const float p4[4] = {w[0], w[1], w[2], 1.0f};
        float sp[4];
        xform(p4, vs, sp);
        pos[0] = (sp[0] - 2048.0f * sp[3]) / 320.0f;
        pos[1] = (sp[1] - 2048.0f * sp[3]) / 112.0f;
        pos[2] = 1.0f;
        pos[3] = sp[3];
    };
    const float t = float(g_tracker.frameNo % 360000u) / 60.0f;
    // the wind blows along x+z, gusting
    auto sway = [&](const float w[3], float amount, float out3[3]) {
        const float ph = w[0] * 0.07f + w[2] * 0.05f;
        const float gust = 0.6f + 0.4f * std::sin(t * 0.37f + w[0] * 0.01f);
        const float s = (std::sin(t * 1.9f + ph) * 0.7f + std::sin(t * 3.3f + ph * 1.9f) * 0.3f) * gust * amount;
        out3[0] = s * 0.8f;
        out3[1] = 0.0f;
        out3[2] = s * 0.6f;
    };
    // the grass area: around a point a little ahead of the camera (Maximo is about 15 units in front of it)
    float centre[3];
    for (int k = 0; k < 3; ++k)
        centre[k] = out.camPos[k] + out.camFwd[k] * 22.0f;
    // (reaching well ahead: things are grown in before Maximo gets near them, the far ones sparse and cheap)
    constexpr float kGrassNear = 38.0f, kGrassFar = 58.0f, kGrassFull = 14.0f, kGrassNearCam = 6.0f, kPebbleFar = 40.0f, kWallFar = 54.0f;
    // the ground's height around Maximo: the lower quarter of the upward-facing level surfaces near him
    {
        float fx = out.camFwd[0], fz = out.camFwd[2];
        const float fl = std::sqrt(fx * fx + fz * fz);
        if (fl > 1e-3f)
        {
            fx /= fl;
            fz /= fl;
        }
        const float mx = out.camPos[0] + fx * 15.0f, mz = out.camPos[2] + fz * 15.0f;
        std::vector<float> ys;
        for (const Op &op : out.ops)
            if (op.type == OpType::Draw && op.target == 0 && op.viewId == out.mainView && op.receives && !op.abe &&
                op.zwrite && !op.shiny)
                for (uint32_t i = op.first; i < op.first + op.count; ++i)
                {
                    const Vtx &v = out.verts[i];
                    const float dx = v.world[0] - mx, dz = v.world[2] - mz;
                    if (v.normal[1] > 0.8f && dx * dx + dz * dz < 20.0f * 20.0f)
                        ys.push_back(v.world[1]);
                }
        if (ys.size() >= 6)
        {
            std::nth_element(ys.begin(), ys.begin() + ys.size() / 4, ys.end());
            out.groundY = ys[ys.size() / 4];
            out.haveGround = true;
        }
    }
    // MAXIMO: the character (drawn with bones) nearest the camera's line of sight, 5 to 35 units ahead; his feet
    // are the bottom of his box
    {
        float best = 1e30f;
        float bc[3] = {}, bLow = 0.0f;
        for (const CharBlob &b : findCharacters(out))
        {
            const float c[3] = {(b.lo[0] + b.hi[0]) * 0.5f, (b.lo[1] + b.hi[1]) * 0.5f, (b.lo[2] + b.hi[2]) * 0.5f};
            const float d[3] = {c[0] - out.camPos[0], c[1] - out.camPos[1], c[2] - out.camPos[2]};
            const float along = d[0] * out.camFwd[0] + d[1] * out.camFwd[1] + d[2] * out.camFwd[2];
            if (along < 5.0f || along > 35.0f || b.verts < 200)
                continue;
            const float off2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2] - along * along;
            const float score = off2 + along * 0.5f;
            if (score < best)
            {
                best = score;
                std::memcpy(bc, c, sizeof(bc));
                bLow = b.lo[1];
            }
        }
        out.havePlayer = best < 1e29f;
        if (out.havePlayer)
        {
            out.player[0] = bc[0];
            out.player[1] = bLow;
            out.player[2] = bc[2];
            // the ground is where his feet are: the lowest they have been over the last second (not raised by a
            // jump), better than the level's surfaces around (a lava pit below counted as the ground)
            static float feet[64];
            static int nFeet = 0, at = 0;
            feet[at] = bLow;
            at = (at + 1) % 64;
            nFeet = std::min(nFeet + 1, 64);
            float lowest = 1e30f;
            for (int k = 0; k < nFeet; ++k)
                lowest = std::min(lowest, feet[k]);
            out.groundY = lowest;
            out.haveGround = true;
        }
    }
    // a camera looking down on the scene from high up (the title menu): the grass area goes where its line of
    // sight meets the ground, not into the air in front of it
    if (out.haveGround && out.camFwd[1] < -0.25f)
    {
        const float t = std::clamp((out.groundY - out.camPos[1]) / out.camFwd[1], out.inLevel ? 22.0f : 12.0f, 70.0f);
        for (int k = 0; k < 3; ++k)
            centre[k] = out.camPos[k] + out.camFwd[k] * t;
    }
    const float camToCentre = std::sqrt((centre[0] - out.camPos[0]) * (centre[0] - out.camPos[0]) +
                                        (centre[1] - out.camPos[1]) * (centre[1] - out.camPos[1]) +
                                        (centre[2] - out.camPos[2]) * (centre[2] - out.camPos[2]));
    if (props || pebbles || grass)
    {
        // (redone every eighth frame, and when the camera has moved on: the level itself does not move)
        static uint64_t lastFloor = ~uint64_t(0);
        const float mx = std::fabs(centre[0] - (g_floor.x0 + FloorGrid::kN * 0.5f)),
                    mz = std::fabs(centre[2] - (g_floor.z0 + FloorGrid::kN * 0.5f));
        if (!g_floor.valid || g_tracker.frameNo - lastFloor >= 30 || lastFloor > g_tracker.frameNo || mx > 8.0f || mz > 8.0f)
        {
            buildFloor(out, centre);
            lastFloor = g_tracker.frameNo;
        }
    }
    if (g_leaves > 0.0f && pebbles)
    {
        static uint64_t lastCanopy = ~uint64_t(0);
        const float mx = std::fabs(centre[0] - (g_canopy.x0 + CanopyGrid::kN * 0.5f)),
                    mz = std::fabs(centre[2] - (g_canopy.z0 + CanopyGrid::kN * 0.5f));
        if (!g_canopy.valid || g_tracker.frameNo - lastCanopy >= 8 || lastCanopy > g_tracker.frameNo || mx > 4.0f ||
            mz > 4.0f)
        {
            buildCanopy(out, centre, g_tracker.versions);
            lastCanopy = g_tracker.frameNo;
        }
    }
    constexpr float kLodShare = 0.65f; // (full detail out to this share of the way to the grass area's centre)
    std::vector<Vtx> flowers;
    constexpr size_t kMaxGrassVerts = 900000;
    constexpr uint32_t kMaxGrassCandidates = 1500000;
    uint32_t grassCandidates = 0;
    // (new places worked out a little at a time: walking into a new part of the level worked out all of its grass,
    // stones and ivy in one frame, a stutter; what is left grows in over the next frames)
    int newSpotBudget = 30000;
    auto triArea = [](const Vtx &a, const Vtx &b, const Vtx &c) {
        const float e1[3] = {b.world[0] - a.world[0], b.world[1] - a.world[1], b.world[2] - a.world[2]};
        const float e2[3] = {c.world[0] - a.world[0], c.world[1] - a.world[1], c.world[2] - a.world[2]};
        const float n[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
        return 0.5f * std::sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]);
    };
    const size_t grassStart = out.verts.size();
    if (raining)
    {
        // (redone every fourth frame: the level does not move, and the camera only a little in that time)
        static uint64_t lastRoof = ~uint64_t(0);
        if (!g_roof.valid || g_tracker.frameNo - lastRoof >= 4 || lastRoof > g_tracker.frameNo)
        {
            float rc[3];
            for (int k = 0; k < 3; ++k)
                rc[k] = out.camPos[k] + out.camFwd[k] * 12.0f;
            buildRoof(out, rc);
            lastRoof = g_tracker.frameNo;
        }
    }
    else
        g_roof.valid = false;
    auto hashF = [](float x) {
        uint32_t u;
        std::memcpy(&u, &x, 4);
        u ^= u >> 16;
        u *= 0x7feb352du;
        u ^= u >> 15;
        u *= 0x846ca68bu;
        u ^= u >> 16;
        return u;
    };
    auto rnd = [](uint32_t &s) {
        s = s * 1664525u + 1013904223u;
        uint32_t x = s ^ (s >> 16);
        x *= 0x45d9f3bu;
        x ^= x >> 16;
        return float(x & 0xFFFFFF) / 16777216.0f;
    };
    // STRUCTURES: what stands at the foot of walls, tombs and cliffs. Out of the box from the wall's lowest edge:
    // ferns (arching fronds), clusters of mushrooms, and old bones with a skull. Coloured by the light on the wall
    // there (its own lit colour), so they sit in the same shade.
    constexpr float kPropFar = 42.0f;
    // A surface the game starts drawing (it draws only what is near and in view, so ground and walls come and go as
    // Maximo walks): what grows on it grows in over a third of a second, instead of popping up all at once.
    // The bed and banks of lava or water: a triangle reaching down to (or under) the surface. Nothing grows anywhere
    // on it, not just where it is under the surface (stones and dead flowers stood in the lava on sloping banks).
    auto touchesLiquid = [&](const Vtx &a, const Vtx &b, const Vtx &c) {
        if (!g_floor.anyLava)
            return false;
        // (only for smaller triangles: a big ground triangle that only touches the water at one corner keeps its
        // grass; its spots are checked one by one instead, inLava)
        const float ex = std::max({a.world[0], b.world[0], c.world[0]}) - std::min({a.world[0], b.world[0], c.world[0]});
        const float ez = std::max({a.world[2], b.world[2], c.world[2]}) - std::min({a.world[2], b.world[2], c.world[2]});
        if (ex > 6.0f || ez > 6.0f)
            return false;
        for (const Vtx *v : {&a, &b, &c})
        {
            const float l = g_floor.liquidAt(v->world[0], v->world[2]);
            if (l > -1e29f && v->world[1] < l + 0.8f && v->world[1] > l - 6.0f)
                return true;
        }
        return false;
    };
    auto touchTri = [&](GrassTri &gt) {
        const uint64_t now = g_tracker.frameNo;
        if (now - gt.lastUsed > 3 || gt.lastUsed > now)
            gt.born = now;
        gt.lastUsed = now;
        return std::clamp(float(now - gt.born) / 20.0f, 0.0f, 1.0f);
    };
    auto dist3 = [](const float lo[3], const float hi[3], const float p[3]) {
        float d2 = 0.0f;
        for (int k = 0; k < 3; ++k)
        {
            const float e = std::max({lo[k] - p[k], 0.0f, p[k] - hi[k]});
            d2 += e * e;
        }
        return std::sqrt(d2);
    };
    // one flat-shaded triangle of a prop
    auto propTri = [&](const float A[3], const float B[3], const float C[3], const float col[3], float shadeA = 1.0f,
                       float shadeB = 1.0f, float shadeC = 1.0f) {
        float e1[3], e2[3];
        for (int q = 0; q < 3; ++q)
        {
            e1[q] = B[q] - A[q];
            e2[q] = C[q] - A[q];
        }
        float n[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
        const float l = std::sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]);
        if (l < 1e-12f)
            return;
        if (n[1] < 0.0f)
            for (float &x : n)
                x = -x;
        const float *P[3] = {A, B, C};
        const float sh[3] = {shadeA, shadeB, shadeC};
        for (int j = 0; j < 3; ++j)
        {
            Vtx o{};
            for (int q = 0; q < 3; ++q)
            {
                o.world[q] = P[j][q];
                o.normal[q] = n[q] / l;
                o.mnormal[q] = 0.0f;
                o.col[q] = o.raw[q] = std::min(255.0f, col[q] * sh[j]);
            }
            o.col[3] = 128.0f;
            toClip(o.world, o.pos);
            out.verts.push_back(o);
        }
    };
    // a closed tube (a stem, a bone) of `sides` faces from A to B
    auto propTube = [&](const float A[3], const float B[3], float r0, float r1, int sides, const float col[3]) {
        float d[3] = {B[0] - A[0], B[1] - A[1], B[2] - A[2]};
        const float dl = std::sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
        if (dl < 1e-6f)
            return;
        for (float &x : d)
            x /= dl;
        float s1[3] = {d[1] * 0.0f - d[2] * 1.0f, d[2] * 0.0f - d[0] * 0.0f, d[0] * 1.0f - d[1] * 0.0f}; // d x (0,1,0)
        float sl = std::sqrt(s1[0] * s1[0] + s1[1] * s1[1] + s1[2] * s1[2]);
        if (sl < 1e-3f)
        {
            s1[0] = 1.0f;
            s1[1] = s1[2] = 0.0f;
            sl = 1.0f;
        }
        for (float &x : s1)
            x /= sl;
        const float s2[3] = {d[1] * s1[2] - d[2] * s1[1], d[2] * s1[0] - d[0] * s1[2], d[0] * s1[1] - d[1] * s1[0]};
        for (int k = 0; k < sides; ++k)
        {
            const float a0 = 6.2831853f * float(k) / float(sides), a1 = 6.2831853f * float(k + 1) / float(sides);
            float p00[3], p01[3], p10[3], p11[3];
            for (int q = 0; q < 3; ++q)
            {
                const float o0 = std::cos(a0) * s1[q] + std::sin(a0) * s2[q], o1 = std::cos(a1) * s1[q] + std::sin(a1) * s2[q];
                p00[q] = A[q] + o0 * r0;
                p01[q] = A[q] + o1 * r0;
                p10[q] = B[q] + o0 * r1;
                p11[q] = B[q] + o1 * r1;
            }
            const float face = 0.75f + 0.35f * std::max(0.0f, std::sin((a0 + a1) * 0.5f)); // (lighter on top)
            propTri(p00, p01, p10, col, 0.85f * face, 0.85f * face, face);
            propTri(p01, p11, p10, col, 0.85f * face, face, face);
        }
    };
    // a lumpy ball (a knob of bone, a skull): an octahedron, its corners scaled
    auto propBlob = [&](const float c[3], const float rad[3], const float fwd[3], const float col[3]) {
        const float side[3] = {fwd[2], 0.0f, -fwd[0]};
        const float axes[3][3] = {{side[0], side[1], side[2]}, {0.0f, 1.0f, 0.0f}, {fwd[0], fwd[1], fwd[2]}};
        float pt[6][3];
        for (int k = 0; k < 6; ++k)
            for (int q = 0; q < 3; ++q)
                pt[k][q] = c[q] + axes[k / 2][q] * rad[k / 2] * ((k & 1) ? -1.0f : 1.0f);
        static const int faces[8][3] = {{0, 2, 4}, {2, 1, 4}, {1, 3, 4}, {3, 0, 4}, {2, 0, 5}, {1, 2, 5}, {3, 1, 5}, {0, 3, 5}};
        for (const auto &f : faces)
        {
            const float sh = (f[0] == 2 || f[1] == 2 || f[2] == 2) ? 1.12f : 0.82f; // (the top half lighter)
            propTri(pt[f[0]], pt[f[1]], pt[f[2]], col, sh, sh, sh);
        }
    };
    // (a flame only on candles near enough to be seen properly, and not too many)
    auto appearFlame = [&](const float p[3]) {
        const float dx = p[0] - out.camPos[0], dz = p[2] - out.camPos[2];
        return out.flames.size() < 160 && dx * dx + dz * dz < 40.0f * 40.0f;
    };
    auto addFootProps = [&](const Vtx &a, const Vtx &b, const Vtx &c, uint32_t version, float opLoY, const float nrm[3],
                            const float ax[3], bool stone) {
        (void)opLoY;
        // where this wall meets the floor: across the wall every metre or so, the floor just out from it, where its
        // height is within the wall's height there
        const Vtx *V[3] = {&a, &b, &c};
        float tv3[3];
        for (int k = 0; k < 3; ++k)
            tv3[k] = (V[k]->world[0] - a.world[0]) * ax[0] + (V[k]->world[2] - a.world[2]) * ax[2];
        const float tlo = std::min({tv3[0], tv3[1], tv3[2]}), thi = std::max({tv3[0], tv3[1], tv3[2]});
        if (thi - tlo < 0.3f)
            return;
        // the wall's vertical segment at `t` across it: where the triangle's edges cross it
        auto segAt = [&](float t, float lowP[3], float highP[3]) {
            int got = 0;
            float pts[3][3];
            for (int e = 0; e < 3; ++e)
            {
                const int i = e, j = (e + 1) % 3;
                const float ti = tv3[i], tj = tv3[j];
                if ((t < std::min(ti, tj)) || (t > std::max(ti, tj)) || std::fabs(tj - ti) < 1e-6f)
                    continue;
                const float f = (t - ti) / (tj - ti);
                for (int q = 0; q < 3; ++q)
                    pts[got][q] = V[i]->world[q] + (V[j]->world[q] - V[i]->world[q]) * f;
                if (++got == 3)
                    break;
            }
            if (got < 2)
                return false;
            int lo = 0, hi = 0;
            for (int k = 1; k < got; ++k)
            {
                if (pts[k][1] < pts[lo][1])
                    lo = k;
                if (pts[k][1] > pts[hi][1])
                    hi = k;
            }
            std::memcpy(lowP, pts[lo], 12);
            std::memcpy(highP, pts[hi], 12);
            return true;
        };
        uint64_t key = 0xD1B54A32D192ED03ull ^ version;
        for (const Vtx *t : V)
            for (int k = 0; k < 3; ++k)
            {
                uint32_t bits;
                std::memcpy(&bits, &t->world[k], 4);
                key = (key ^ bits) * 1099511628211ull;
            }
        auto it = g_grassCache.find(key);
        if (it == g_grassCache.end())
        {
            // (worked out only once the floor around it is known: inside the floor grid, well clear of its edges)
            const float cx = (a.world[0] + b.world[0] + c.world[0]) / 3.0f, cz = (a.world[2] + b.world[2] + c.world[2]) / 3.0f;
            const float gx = g_floor.x0 + FloorGrid::kN * 0.5f, gz = g_floor.z0 + FloorGrid::kN * 0.5f;
            if (!g_floor.valid || std::fabs(cx - gx) > FloorGrid::kN * 0.5f - 3.0f ||
                std::fabs(cz - gz) > FloorGrid::kN * 0.5f - 3.0f)
                return;
            if (newSpotBudget <= 0)
                return;
            newSpotBudget -= 40;
            it = g_grassCache.try_emplace(key).first;
            GrassTri &gt = it->second;
            uint32_t seed = uint32_t(key) ^ uint32_t(key >> 32);
            const float width = thi - tlo;
            const int n = std::max(1, int(width / 1.1f));
            for (int k = 0; k < n; ++k)
            {
                const float t = tlo + width * (float(k) + 0.15f + 0.7f * rnd(seed)) / float(n);
                const float kind = rnd(seed);
                const float share = 0.62f * std::min(1.0f, g_props);
                if (kind >= share)
                    continue;
                float lowP[3], highP[3];
                if (!segAt(t, lowP, highP))
                    continue;
                const float fy = g_floor.heightNear(lowP[0] + nrm[0] * 0.4f, lowP[2] + nrm[2] * 0.4f, lowP[1] - 0.1f, 0.0f,
                                              highP[1] - lowP[1] + 0.1f);
                if (fy < -1e29f || fy > highP[1] - 0.3f)
                    continue; // (no floor along this wall here, or only at its very top)
                gt.spots.push_back({t, fy, (seed ^ (uint32_t(k) * 2654435761u)) | 1u});
                gt.spots.back().seed = (gt.spots.back().seed & ~0xFFu) | uint32_t(kind / share * 255.0f);
            }
        }
        GrassTri &gt = it->second;
        const float ageFade = touchTri(gt);
        for (const GrassSpot &spot : gt.spots)
        {
            if (out.verts.size() - grassStart > kMaxGrassVerts)
                return;
            uint32_t seed = spot.seed;
            float lowP[3], highP[3];
            if (!segAt(spot.u, lowP, highP))
                continue;
            float p[3];
            const float f = std::clamp((spot.v - lowP[1]) / std::max(1e-4f, highP[1] - lowP[1]), 0.0f, 1.0f);
            for (int q = 0; q < 3; ++q)
                p[q] = lowP[q] + (highP[q] - lowP[q]) * f + nrm[q] * 0.06f;
            p[1] = spot.v;
            const float dc[3] = {p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]};
            const float dist = std::sqrt(dc[0] * dc[0] + dc[1] * dc[1] + dc[2] * dc[2]);
            if (dist > kPropFar)
                continue;
            const float cc[3] = {p[0] - out.camPos[0], p[1] - out.camPos[1], p[2] - out.camPos[2]};
            if (cc[0] * cc[0] + cc[1] * cc[1] + cc[2] * cc[2] < 4.0f * 4.0f)
                continue;
            // the light there: the wall's own lit colour (its corners' average)
            float lc[3];
            for (int q = 0; q < 3; ++q)
                lc[q] = (a.col[q] + b.col[q] + c.col[q]) / 3.0f;
            const float lf = std::clamp((lc[0] * 0.3f + lc[1] * 0.59f + lc[2] * 0.11f) / 100.0f, 0.35f, 1.3f);
            const float r1 = rnd(seed), r2 = rnd(seed), r3 = rnd(seed);
            float kind = float(spot.seed & 0xFFu) / 255.0f * 0.999f;
            if (g_floor.inLava(p[0], p[2], p[1]))
                continue;
            const size_t propStart = out.verts.size();
            // (at the foot of grey stone, walls and pillars, a third of them are candles; the rest as elsewhere)
            bool candle = false;
            if (stone)
            {
                if (kind < 0.33f)
                    candle = true;
                else
                    kind = (kind - 0.33f) / 0.67f;
            }
            if (candle)
            {
                // CANDLES: two to five wax candles of different heights, a little melted (wider at the foot), each
                // with a flame on top (the flames themselves are drawn with the glows, see addWeather)
                const float wax[3] = {196.0f * lf, 182.0f * lf, 146.0f * lf};
                const int nc = 2 + int(r1 * 4.0f);
                for (int m = 0; m < nc; ++m)
                {
                    const float ra = rnd(seed), rb = rnd(seed), rc = rnd(seed);
                    const float cp[3] = {p[0] + nrm[0] * (0.12f + 0.2f * ra) + ax[0] * (rb - 0.5f) * 0.5f, p[1] - 0.01f,
                                         p[2] + nrm[2] * (0.12f + 0.2f * ra) + ax[2] * (rb - 0.5f) * 0.5f};
                    const float h = 0.15f + 0.4f * rc * rc, rad = 0.035f + 0.025f * ra;
                    const float top[3] = {cp[0], cp[1] + h, cp[2]};
                    propTube(cp, top, rad * 1.35f, rad, 6, wax);
                    // the wick end: a darker little cap
                    const float tip[3] = {top[0], top[1] + 0.025f, top[2]};
                    const float wick[3] = {40.0f, 32.0f, 26.0f};
                    propTube(top, tip, rad * 0.25f, rad * 0.15f, 3, wick);
                    if (appearFlame(p))
                        out.flames.push_back({tip[0], tip[1] + 0.05f, tip[2]});
                }
            }
            else if (kind < 0.66f)
            {
                // FERN: fronds arching out from the wall's foot, each a strip that widens and narrows along it
                // (leaflets), bent by the wind
                const int fronds = 5 + int(r1 * 3.0f);
                const float L0 = (0.55f + 0.5f * r2) * std::max(0.6f, std::min(1.4f, g_props));
                float sw[3];
                sway(p, 0.12f * g_wind, sw);
                const float green[3] = {30.0f * lf, 54.0f * lf, 24.0f * lf};
                for (int f = 0; f < fronds; ++f)
                {
                    const float rf = rnd(seed), rl = rnd(seed);
                    const float th = -1.25f + 2.5f * (float(f) + rf * 0.8f) / float(fronds);
                    const float dir[3] = {nrm[0] * std::cos(th) + ax[0] * std::sin(th), 0.0f,
                                          nrm[2] * std::cos(th) + ax[2] * std::sin(th)};
                    const float sideV[3] = {-dir[2], 0.0f, dir[0]};
                    const float L = L0 * (0.7f + 0.45f * rl);
                    const float rise = 0.9f + 0.5f * rf;
                    const int segs = 5;
                    float prevL[3], prevR[3];
                    for (int sg = 0; sg <= segs; ++sg)
                    {
                        const float t = float(sg) / float(segs);
                        float c3[3];
                        for (int q = 0; q < 3; ++q)
                            c3[q] = p[q] + dir[q] * L * t + sw[q] * t * t;
                        c3[1] = p[1] + L * (rise * t - (rise * 0.9f) * t * t) + 0.02f;
                        const float w = L * 0.13f * std::sin(3.1415927f * std::min(1.0f, t * 1.1f)) *
                                        ((sg & 1) ? 1.0f : 0.6f) + 0.006f;
                        const float Lp[3] = {c3[0] - sideV[0] * w, c3[1], c3[2] - sideV[2] * w};
                        const float Rp[3] = {c3[0] + sideV[0] * w, c3[1], c3[2] + sideV[2] * w};
                        if (sg > 0)
                        {
                            const float s0 = 0.65f + 0.5f * (t - 1.0f / float(segs)), s1 = 0.65f + 0.5f * t;
                            propTri(prevL, prevR, Lp, green, s0, s0, s1);
                            propTri(prevR, Rp, Lp, green, s0, s1, s1);
                        }
                        std::memcpy(prevL, Lp, sizeof(prevL));
                        std::memcpy(prevR, Rp, sizeof(prevR));
                    }
                }
            }
            else if (kind < 0.92f)
            {
                // MUSHROOMS: two to four, a pale stem and a cone-shaped cap each, in one of a few colours
                static const float caps[4][3] = {{82, 54, 34}, {98, 46, 30}, {108, 100, 84}, {70, 62, 40}};
                const float *capC = caps[std::min(3, int(r3 * 4.0f))];
                const int nm = 2 + int(r1 * 3.0f);
                for (int m = 0; m < nm; ++m)
                {
                    const float ra = rnd(seed), rb = rnd(seed), rc = rnd(seed);
                    const float mp[3] = {p[0] + nrm[0] * 0.12f * ra + ax[0] * (rb - 0.5f) * 0.4f, p[1] - 0.01f,
                                         p[2] + nrm[2] * 0.12f * ra + ax[2] * (rb - 0.5f) * 0.4f};
                    const float h = 0.07f + 0.16f * rc, rcap = 0.045f + 0.07f * rc;
                    const float stem[3] = {104.0f * lf, 98.0f * lf, 82.0f * lf};
                    const float cap[3] = {capC[0] * lf, capC[1] * lf, capC[2] * lf};
                    const float top[3] = {mp[0] + (ra - 0.5f) * 0.03f, mp[1] + h, mp[2] + (rb - 0.5f) * 0.03f};
                    propTube(mp, top, 0.022f, 0.016f, 4, stem);
                    float rim[7][3];
                    for (int k = 0; k <= 6; ++k)
                    {
                        const float an = float(k) * 1.0471976f + ra;
                        rim[k][0] = top[0] + std::cos(an) * rcap;
                        rim[k][1] = top[1] - rcap * 0.25f;
                        rim[k][2] = top[2] + std::sin(an) * rcap;
                    }
                    const float apex[3] = {top[0], top[1] + rcap * 0.55f, top[2]};
                    const float under[3] = {cap[0] * 0.55f, cap[1] * 0.55f, cap[2] * 0.55f};
                    for (int k = 0; k < 6; ++k)
                    {
                        propTri(rim[k], rim[k + 1], apex, cap, 0.85f, 0.85f, 1.15f);
                        propTri(rim[k + 1], rim[k], top, under, 1.0f, 1.0f, 0.8f);
                    }
                }
            }
            else
            {
                // OLD BONES: a skull (a lumpy ball, dark eye sockets, a jaw) and a bone or two lying by it
                const float bone[3] = {112.0f * lf, 104.0f * lf, 84.0f * lf};
                const float yaw = std::atan2(nrm[2], nrm[0]) + (r1 - 0.5f) * 1.6f;
                const float fwd[3] = {std::cos(yaw), 0.0f, std::sin(yaw)};
                const float side[3] = {fwd[2], 0.0f, -fwd[0]};
                const float s = 0.85f + 0.3f * r2;
                const float sc[3] = {p[0] + nrm[0] * 0.15f, p[1] + 0.1f * s, p[2] + nrm[2] * 0.15f};
                const float rad[3] = {0.11f * s, 0.11f * s, 0.14f * s};
                propBlob(sc, rad, fwd, bone);
                // the jaw: a smaller lump under the front
                const float jc[3] = {sc[0] + fwd[0] * 0.07f * s, sc[1] - 0.06f * s, sc[2] + fwd[2] * 0.07f * s};
                const float jr[3] = {0.07f * s, 0.035f * s, 0.07f * s};
                propBlob(jc, jr, fwd, bone);
                // eye sockets: small dark triangles on the front
                const float dark[3] = {14.0f, 11.0f, 9.0f};
                for (int k = -1; k <= 1; k += 2)
                {
                    float ec[3];
                    for (int q = 0; q < 3; ++q)
                        ec[q] = sc[q] + fwd[q] * 0.112f * s + side[q] * float(k) * 0.042f * s;
                    ec[1] = sc[1] + 0.015f * s;
                    const float A[3] = {ec[0] - side[0] * 0.028f * s, ec[1] + 0.022f * s, ec[2] - side[2] * 0.028f * s};
                    const float B[3] = {ec[0] + side[0] * 0.028f * s, ec[1] + 0.022f * s, ec[2] + side[2] * 0.028f * s};
                    const float C[3] = {ec[0] + fwd[0] * 0.005f, ec[1] - 0.024f * s, ec[2] + fwd[2] * 0.005f};
                    propTri(A, B, C, dark);
                }
                // a bone or two: a tube with a knob at each end, lying on the ground
                const int nb = 1 + int(r3 * 2.0f);
                for (int k = 0; k < nb; ++k)
                {
                    const float ra = rnd(seed), rb = rnd(seed);
                    const float by = yaw + 1.5707963f + (ra - 0.5f) * 1.8f;
                    const float bl = 0.35f + 0.25f * rb;
                    const float bc[3] = {sc[0] + side[0] * (k ? -0.28f : 0.28f) + nrm[0] * 0.1f, p[1] + 0.035f,
                                         sc[2] + side[2] * (k ? -0.28f : 0.28f) + nrm[2] * 0.1f};
                    const float A[3] = {bc[0] - std::cos(by) * bl * 0.5f, bc[1], bc[2] - std::sin(by) * bl * 0.5f};
                    const float B[3] = {bc[0] + std::cos(by) * bl * 0.5f, bc[1] + 0.01f, bc[2] + std::sin(by) * bl * 0.5f};
                    propTube(A, B, 0.025f, 0.022f, 5, bone);
                    const float kr[3] = {0.045f, 0.035f, 0.045f};
                    propBlob(A, kr, fwd, bone);
                    propBlob(B, kr, fwd, bone);
                }
            }
            // (grows in, not pops in: from nothing at the edge of reach to full size 8 units nearer, each at its
            // own pace)
            const float appear = ageFade * std::clamp((kPropFar - dist) / 8.0f - r3 * 0.3f, 0.0f, 1.0f) *
                                 std::clamp((std::sqrt(cc[0] * cc[0] + cc[1] * cc[1] + cc[2] * cc[2]) - 4.0f) / 2.0f, 0.0f, 1.0f);
            if (appear < 1.0f)
                for (size_t vi = propStart; vi < out.verts.size(); ++vi)
                {
                    Vtx &o = out.verts[vi];
                    for (int q = 0; q < 3; ++q)
                        o.world[q] = p[q] + (o.world[q] - p[q]) * appear;
                    toClip(o.world, o.pos);
                }
        }
    };
    // forget the grass of places not drawn for a while (another part of the level, or another level)
    // (a little each frame, a slice of the table at a time: sweeping it all at once every two seconds was a stutter)
    {
        static size_t cursor = 0;
        static std::vector<uint64_t> stale;
        stale.clear();
        const size_t buckets = g_grassCache.bucket_count();
        const size_t slice = g_grassCache.size() > 200000 ? buckets : std::max<size_t>(1, buckets / 128);
        for (size_t n = 0; n < slice && buckets; ++n)
        {
            const size_t bk = (cursor + n) % buckets;
            for (auto it = g_grassCache.begin(bk); it != g_grassCache.end(bk); ++it)
                if (g_tracker.frameNo - it->second.lastUsed > 600)
                    stale.push_back(it->first);
        }
        cursor = buckets ? (cursor + slice) % buckets : 0;
        for (uint64_t k : stale)
            g_grassCache.erase(k);
    }
    std::vector<Op> ops;
    ops.reserve(out.ops.size() + 64);
    for (const Op &src : out.ops)
    {
        ops.push_back(src);
        if (src.type != OpType::Draw || src.target != 0 || src.viewId != out.mainView || !src.textured ||
            !src.version || (src.special && src.abe) || src.emissive || src.count < 3 || src.skinned || src.moves)
            continue; // (nothing grows on what moves with bones: characters, doors and gates swinging open)
        // (a solid surface the game also lays a shadow or reflection over is still ground: the title menu's stone
        // platform under Maximo; lit by real lights only skips the small things, characters and pickups)
        if (src.shiny)
        {
            float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
            for (uint32_t i = src.first; i < src.first + src.count; ++i)
                for (int k = 0; k < 3; ++k)
                {
                    lo[k] = std::min(lo[k], out.verts[i].world[k]);
                    hi[k] = std::max(hi[k], out.verts[i].world[k]);
                }
            if (std::max(hi[0] - lo[0], hi[2] - lo[2]) < 12.0f)
                continue;
        }
        const auto it = g_tracker.versions.find(src.version);
        if (it == g_tracker.versions.end())
            continue;
        const TexVersion &tv = it->second;
        const bool ate = (src.test & 1) != 0;
        // how much of it is wall (steep): rocks, tombstones and cliffs have a lot, paths and floors little
        float wallArea = 0.0f, allArea = 0.0f;
        for (uint32_t i = src.first; i + 2 < src.first + src.count; i += 3)
        {
            const Vtx *tri = &out.verts[i];
            float e1[3], e2[3];
            for (int k = 0; k < 3; ++k)
            {
                e1[k] = tri[1].world[k] - tri[0].world[k];
                e2[k] = tri[2].world[k] - tri[0].world[k];
            }
            const float nx = e1[1] * e2[2] - e1[2] * e2[1], ny = e1[2] * e2[0] - e1[0] * e2[2], nz = e1[0] * e2[1] - e1[1] * e2[0];
            const float ar = std::sqrt(nx * nx + ny * ny + nz * nz);
            allArea += ar;
            if (std::fabs(ny) < 0.5f * ar)
                wallArea += ar;
        }
        const float wallShare = allArea > 0.0f ? wallArea / allArea : 0.0f;
        // a blended ground layer (the title menu draws its whole floor see-through, without writing depth): grass and
        // stones still grow on it, drawn solid themselves
        const bool groundLayer = src.abe && !ate && (src.alphaReg & 0xFF) == 0x44 && wallShare < 0.3f &&
                                 allArea > 2.0f * 400.0f;
        constexpr bool bankOfLiquid = false; // (per triangle instead: touchesLiquid)
        // ROCKS AND GROUND: bumps on every solid textured surface of the level and its objects (not on
        // characters, glows or see-through layers); moss on stone (not grass) with walls to it
        if (!src.abe || ate)
        {
            ops.back().relief = g_relief;
            if (!ate && !src.shiny && tv.grassy < 2 && wallShare > 0.2f)
                ops.back().moss = g_moss;
        }
        // WIND: tufts (green cut-outs) bend from the bottom of each triangle up; leaves (mostly see-through)
        // drift a little all over
        if (wind && ate && (tv.clear >= 0.6f || (tv.green >= 0.3f && tv.clear >= 0.3f)))
        {
            const bool tuft = tv.green >= 0.3f && tv.clear < 0.6f;
            for (uint32_t i = src.first; i + 2 < src.first + src.count; i += 3)
            {
                Vtx *tri = &out.verts[i];
                const float lo = std::min({tri[0].world[1], tri[1].world[1], tri[2].world[1]});
                for (int j = 0; j < 3; ++j)
                {
                    float h = tuft ? std::clamp((tri[j].world[1] - lo) / 4.0f, 0.0f, 1.0f) : 1.0f;
                    h = tuft ? h * h * 0.6f : 0.18f;
                    float d[3];
                    sway(tri[j].world, h * g_wind, d);
                    for (int k = 0; k < 3; ++k)
                        tri[j].world[k] += d[k];
                    toClip(tri[j].world, tri[j].pos);
                }
            }
            continue;
        }
        // WALLS: ivy growing in patches over stone walls and cliffs near the camera (small leaves lying on the
        // wall, with a few vines hanging down from the patches), and cobwebs in the creases where walls meet the
        // ground. Spots per wall triangle worked out once and kept, like the grass.
        if ((ivy || webs || props) && !ate && !src.abe && src.zwrite && !src.shiny && tv.grassy < 2 && tv.clear <= 0.1f &&
            wallShare > 0.15f)
        {
            // grey stone (walls, pillars, tombs): candles among the props there
            const bool stoneWall = tv.grassy == 0 && tv.grey > 0.45f && !tv.glass;
            // the foot of this wall or tomb: its lowest point (the props stand along the edges down there)
            float opLoY = 1e30f;
            for (uint32_t i = src.first; i < src.first + src.count; ++i)
                opLoY = std::min(opLoY, out.verts[i].world[1]);
            Op leafOp = src;
            leafOp.first = uint32_t(out.verts.size());
            leafOp.textured = false;
            leafOp.version = 0;
            leafOp.casts = false;
            leafOp.relief = leafOp.moss = 0.0f;
            leafOp.test &= ~uint64_t(1);
            std::vector<Vtx> webVerts;
            for (uint32_t i = src.first; i + 2 < src.first + src.count; i += 3)
            {
                if (out.verts.size() - grassStart > kMaxGrassVerts)
                    break;
                const Vtx a = out.verts[i], b = out.verts[i + 1], c = out.verts[i + 2];
                if (touchesLiquid(a, b, c))
                    continue;
                float lo[3], hi[3];
                for (int k = 0; k < 3; ++k)
                {
                    lo[k] = std::min({a.world[k], b.world[k], c.world[k]});
                    hi[k] = std::max({a.world[k], b.world[k], c.world[k]});
                }
                float d2 = 0.0f;
                for (int k = 0; k < 3; ++k)
                {
                    const float e = std::max({lo[k] - centre[k], 0.0f, centre[k] - hi[k]});
                    d2 += e * e;
                }
                if (d2 > kWallFar * kWallFar)
                    continue;
                float e1[3], e2[3];
                for (int k = 0; k < 3; ++k)
                {
                    e1[k] = b.world[k] - a.world[k];
                    e2[k] = c.world[k] - a.world[k];
                }
                float nrm[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
                const float twoArea = std::sqrt(nrm[0] * nrm[0] + nrm[1] * nrm[1] + nrm[2] * nrm[2]);
                if (twoArea < 1e-4f)
                    continue;
                for (float &x : nrm)
                    x /= twoArea;
                if (std::fabs(nrm[1]) > 0.35f) // walls only
                    continue;
                // the side the wall faces: the way the game's own normals point
                const float vn = (a.normal[0] + b.normal[0] + c.normal[0]) * nrm[0] +
                                 (a.normal[1] + b.normal[1] + c.normal[1]) * nrm[1] +
                                 (a.normal[2] + b.normal[2] + c.normal[2]) * nrm[2];
                if (vn < 0.0f)
                    for (float &x : nrm)
                        x = -x;
                // across the wall (horizontal) and up it
                float ax[3] = {-nrm[2], 0.0f, nrm[0]};
                {
                    const float l = std::sqrt(ax[0] * ax[0] + ax[2] * ax[2]);
                    if (l < 1e-4f)
                        continue;
                    ax[0] /= l;
                    ax[2] /= l;
                }
                // up the wall: across x normal
                float upv[3] = {ax[1] * nrm[2] - ax[2] * nrm[1], ax[2] * nrm[0] - ax[0] * nrm[2],
                                ax[0] * nrm[1] - ax[1] * nrm[0]};
                if (upv[1] < 0.0f)
                    for (float &x : upv)
                        x = -x;
                // PROPS AT THE FOOT: along the bottom edge of the wall (where it stands on the ground), now and then a
                // fern, a cluster of mushrooms or old bones (a skull); spots along the edge worked out once and kept
                if (props && !bankOfLiquid && dist3(lo, hi, centre) < kPropFar)
                    addFootProps(a, b, c, src.version, opLoY, nrm, ax, stoneWall);
                uint64_t tkey = 0xC2B2AE3D27D4EB4Full ^ src.version;
                for (const Vtx *tp : {&a, &b, &c})
                    for (int k = 0; k < 3; ++k)
                    {
                        uint32_t bits;
                        std::memcpy(&bits, &tp->world[k], 4);
                        tkey = (tkey ^ bits) * 1099511628211ull;
                    }
                if (newSpotBudget <= 0 && !g_grassCache.count(tkey))
                    continue;
                auto [slot, isNew] = g_grassCache.try_emplace(tkey);
                if (isNew)
                    newSpotBudget -= int(triArea(a, b, c) * 6.0f) + 20;
                GrassTri &gt = slot->second;
                const float ageFade = touchTri(gt);
                if (isNew)
                {
                    // leaves only where a slow noise across the walls says ivy grows (patches, about a third)
                    uint32_t seed = uint32_t(tkey) ^ uint32_t(tkey >> 32);
                    const float want = twoArea * 0.5f * 15.0f;
                    const uint32_t n = std::min(60000u, uint32_t(want));
                    for (uint32_t k = 0; k < n; ++k)
                    {
                        float u = rnd(seed), v = rnd(seed);
                        if (u + v > 1.0f)
                        {
                            u = 1.0f - u;
                            v = 1.0f - v;
                        }
                        float p[3];
                        for (int q = 0; q < 3; ++q)
                            p[q] = a.world[q] + e1[q] * u + e2[q] * v;
                        const float patch = noise3(p[0] * 0.22f, p[1] * 0.22f, p[2] * 0.22f) * 0.7f +
                                            noise3(p[0] * 0.7f, p[1] * 0.7f, p[2] * 0.7f) * 0.3f;
                        if (patch < 0.56f)
                            continue;
                        gt.spots.push_back({u, v, seed ^ (k * 2654435761u)});
                    }
                }
                if (isNew)
                    buildChunks(gt, a.world, e1, e2);
                size_t chunkAt = 0;
                for (size_t si = 0; si < gt.spots.size(); ++si)
                {
                    if (chunkAt < gt.chunks.size() && si == gt.chunks[chunkAt].first)
                    {
                        const GrassChunk &ch = gt.chunks[chunkAt++];
                        const float cx = ch.c[0] - centre[0], cy = ch.c[1] - centre[1], cz = ch.c[2] - centre[2];
                        if (std::sqrt(cx * cx + cy * cy + cz * cz) - ch.r > kWallFar)
                        {
                            si += ch.count - 1; // (the whole chunk is out of reach)
                            continue;
                        }
                    }
                    const GrassSpot &spot = gt.spots[si];
                    uint32_t seed = spot.seed;
                    float p[3];
                    for (int q = 0; q < 3; ++q)
                        p[q] = a.world[q] + e1[q] * spot.u + e2[q] * spot.v;
                    const float dx = p[0] - centre[0], dy = p[1] - centre[1], dz = p[2] - centre[2];
                    const float dist = std::sqrt(dx * dx + dy * dy + dz * dz);
                    if (dist > kWallFar)
                        continue;
                    const float r1 = rnd(seed), r2 = rnd(seed), r3 = rnd(seed), r4 = rnd(seed), kind = rnd(seed);
                    float dcam;
                    {
                        const float cx = p[0] - out.camPos[0], cy = p[1] - out.camPos[1], cz = p[2] - out.camPos[2];
                        dcam = std::sqrt(cx * cx + cy * cy + cz * cz);
                    }
                    const float lodFar = camToCentre * 0.5f;
                    const float lod = dcam < lodFar ? 1.0f : (lodFar / dcam) * (lodFar / dcam);
                    const float keep = std::clamp((kWallFar - dist) / 12.0f, 0.0f, 1.0f) * lod;
                    if (r4 > keep)
                        continue;
                    // (grows in, not pops in: from nothing when it just comes into reach, to full size a little nearer)
                    const float appear = std::min(1.0f, (keep - r4) / 0.25f) * ageFade;
                    const float widen = std::min(3.0f, 1.0f / std::sqrt(std::max(lod, 0.05f))) * appear;
                    // (up in the corners too: under arches and ceilings, from the top of a wall)
                    const bool webHigh = webs && kind > 0.9975f && dist < 25.0f && hi[1] - p[1] < 0.6f && hi[1] - lo[1] > 3.0f &&
                                         tv.grey > 0.4f;
                    const bool webLow = webs && kind > 0.9985f && dist < 25.0f && p[1] - lo[1] < 0.6f;
                    if (webLow || webHigh)
                    {
                        const float vs = webHigh && !webLow ? -1.0f : 1.0f; // (hanging up into the corner above)
                        // COBWEB: a sheet of threads spanning the crease below this spot, from the wall to the ground
                        // (opening out as it comes into reach)
                        const float r = (0.5f + 0.7f * r1) * appear * std::clamp((25.0f - dist) / 5.0f, 0.0f, 1.0f);
                        const float o[3] = {p[0] + nrm[0] * 0.02f, p[1], p[2] + nrm[2] * 0.02f};
                        // the corner of the web on the wall, a little up, spreading out and down onto the floor
                        auto webPt = [&](float ang, float rad, float outp[3]) {
                            const float ca = std::cos(ang), sa = std::sin(ang);
                            // ang 0..pi/2: from along the wall to straight out from it, tilted down 45 degrees
                            for (int q = 0; q < 3; ++q)
                                outp[q] = o[q] + (ax[q] * ca * (r2 < 0.5f ? 1.0f : -1.0f) + nrm[q] * sa * 0.7f) * rad -
                                          upv[q] * sa * rad * 0.7f * vs;
                        };
                        auto thread = [&](const float A[3], const float B[3]) {
                            // a thin strip along A..B, edge-on to the camera as little as possible
                            float dir[3], toCam[3], side[3];
                            for (int q = 0; q < 3; ++q)
                            {
                                dir[q] = B[q] - A[q];
                                toCam[q] = out.camPos[q] - A[q];
                            }
                            side[0] = dir[1] * toCam[2] - dir[2] * toCam[1];
                            side[1] = dir[2] * toCam[0] - dir[0] * toCam[2];
                            side[2] = dir[0] * toCam[1] - dir[1] * toCam[0];
                            const float sl = std::sqrt(side[0] * side[0] + side[1] * side[1] + side[2] * side[2]);
                            if (sl < 1e-6f)
                                return;
                            const float w = 0.006f * std::max(1.0f, dist / 8.0f);
                            Vtx q4[4];
                            for (int j = 0; j < 4; ++j)
                            {
                                Vtx &vv = q4[j];
                                const float *P = (j & 1) ? B : A;
                                const float sd = (j & 2) ? w : -w;
                                for (int q = 0; q < 3; ++q)
                                {
                                    vv.world[q] = P[q] + side[q] / sl * sd;
                                    vv.normal[q] = nrm[q];
                                    vv.mnormal[q] = 0.0f;
                                }
                                vv.col[0] = vv.col[1] = 175.0f;
                                vv.col[2] = 185.0f;
                                vv.col[3] = 70.0f;
                                for (int q = 0; q < 3; ++q)
                                    vv.raw[q] = vv.col[q];
                                vv.uv[0] = vv.uv[1] = 0.0f;
                                toClip(vv.world, vv.pos);
                            }
                            for (int j : {0, 1, 2, 1, 3, 2})
                                webVerts.push_back(q4[j]);
                        };
                        const int spokes = 6;
                        float rim[spokes + 1][3];
                        for (int k = 0; k <= spokes; ++k)
                        {
                            webPt(1.5707963f * float(k) / float(spokes), r, rim[k]);
                            thread(o, rim[k]);
                        }
                        for (int ring = 1; ring <= 4; ++ring)
                        {
                            const float rr = r * float(ring) / 4.6f;
                            float prev[3];
                            webPt(0.0f, rr, prev);
                            for (int k = 1; k <= spokes; ++k)
                            {
                                float cur[3];
                                webPt(1.5707963f * float(k) / float(spokes), rr * (0.92f + 0.16f * rnd(seed)), cur);
                                thread(prev, cur);
                                std::memcpy(prev, cur, sizeof(prev));
                            }
                        }
                        continue;
                    }
                    if (!ivy)
                        continue;
                    // (none right in front of the lens: leaves the size of the screen when the camera brushes a wall)
                    if (dcam < 5.0f)
                        continue;
                    // IVY LEAF: a small pointed leaf lying on the wall, turned a little, a few shades of dark green
                    const float sz = (0.09f + 0.08f * r1) * widen * g_ivy * std::clamp((dcam - 5.0f) / 2.0f, 0.0f, 1.0f);
                    const float ang = (r2 - 0.5f) * 2.4f;
                    const float ca = std::cos(ang), sa = std::sin(ang);
                    float lx[3], ly[3];
                    for (int q = 0; q < 3; ++q)
                    {
                        lx[q] = ax[q] * ca + upv[q] * sa;
                        ly[q] = -ax[q] * sa + upv[q] * ca;
                    }
                    const float lift = 0.02f + 0.05f * r3; // off the wall, so the leaves overlap in layers
                    const float shade = 0.75f + 0.5f * r3;
                    const float base[3] = {34.0f * shade, 58.0f * shade, 26.0f * shade};
                    const float ln[3] = {nrm[0] + lx[0] * 0.3f * (r1 - 0.5f), nrm[1] + 0.25f, nrm[2] + lx[2] * 0.3f * (r1 - 0.5f)};
                    const float pts[4][2] = {{0.0f, -0.9f}, {0.55f, 0.0f}, {0.0f, 1.0f}, {-0.55f, 0.0f}};
                    Vtx q4[4];
                    for (int j = 0; j < 4; ++j)
                    {
                        Vtx &vv = q4[j];
                        for (int q = 0; q < 3; ++q)
                        {
                            vv.world[q] = p[q] + nrm[q] * (lift + (j == 2 ? 0.04f : 0.0f)) +
                                          (lx[q] * pts[j][0] + ly[q] * pts[j][1]) * sz;
                            vv.normal[q] = ln[q];
                            vv.mnormal[q] = 0.0f;
                            vv.col[q] = vv.raw[q] = base[q] * (j == 2 ? 1.12f : (j == 0 ? 0.8f : 1.0f));
                        }
                        vv.col[3] = 128.0f;
                        vv.uv[0] = vv.uv[1] = 0.0f;
                        toClip(vv.world, vv.pos);
                    }
                    for (int j : {0, 1, 2, 0, 2, 3})
                        out.verts.push_back(q4[j]);
                    // now and then a vine hanging from the patch: a thin strand down the wall, with a few leaves
                    if (kind < 0.012f && dist < 25.0f)
                    {
                        const int segs = 5;
                        const float len = (0.8f + 2.0f * r1) * appear * std::clamp((25.0f - dist) / 5.0f, 0.0f, 1.0f);
                        float sway3[3];
                        sway(p, 0.12f * g_wind, sway3);
                        float prev[3] = {p[0] + nrm[0] * 0.05f, p[1], p[2] + nrm[2] * 0.05f};
                        for (int sgi = 1; sgi <= segs; ++sgi)
                        {
                            const float f = float(sgi) / float(segs);
                            float cur[3];
                            for (int q = 0; q < 3; ++q)
                                cur[q] = p[q] + nrm[q] * (0.05f + 0.12f * f * f) - upv[q] * len * f + sway3[q] * f * f;
                            float side[3] = {ax[0] * 0.025f, ax[1] * 0.025f, ax[2] * 0.025f};
                            Vtx s4[4];
                            for (int j = 0; j < 4; ++j)
                            {
                                Vtx &vv = s4[j];
                                const float *P = (j & 1) ? cur : prev;
                                const float sd = (j & 2) ? 1.0f : -1.0f;
                                for (int q = 0; q < 3; ++q)
                                {
                                    vv.world[q] = P[q] + side[q] * sd;
                                    vv.normal[q] = nrm[q];
                                    vv.mnormal[q] = 0.0f;
                                    vv.col[q] = vv.raw[q] = base[q] * 0.75f;
                                }
                                vv.col[3] = 128.0f;
                                vv.uv[0] = vv.uv[1] = 0.0f;
                                toClip(vv.world, vv.pos);
                            }
                            for (int j : {0, 1, 2, 1, 3, 2})
                                out.verts.push_back(s4[j]);
                            if (sgi % 2 == 1)
                            {
                                // a leaf on the strand
                                Vtx l4[4];
                                for (int j = 0; j < 4; ++j)
                                {
                                    Vtx &vv = l4[j];
                                    for (int q = 0; q < 3; ++q)
                                    {
                                        vv.world[q] = cur[q] + nrm[q] * 0.02f +
                                                      (ax[q] * pts[j][0] - upv[q] * pts[j][1]) * sz * 0.8f;
                                        vv.normal[q] = ln[q];
                                        vv.mnormal[q] = 0.0f;
                                        vv.col[q] = vv.raw[q] = base[q];
                                    }
                                    vv.col[3] = 128.0f;
                                    vv.uv[0] = vv.uv[1] = 0.0f;
                                    toClip(vv.world, vv.pos);
                                }
                                for (int j : {0, 1, 2, 0, 2, 3})
                                    out.verts.push_back(l4[j]);
                            }
                            std::memcpy(prev, cur, sizeof(prev));
                        }
                    }
                }
            }
            leafOp.count = uint32_t(out.verts.size()) - leafOp.first;
            if (leafOp.count)
            {
                leafOp.generated = true;
                ops.push_back(leafOp);
            }
            if (!webVerts.empty())
            {
                Op webOp = leafOp;
                webOp.first = uint32_t(out.verts.size());
                out.verts.insert(out.verts.end(), webVerts.begin(), webVerts.end());
                webOp.count = uint32_t(webVerts.size());
                webOp.abe = true;
                webOp.alphaReg = 0x44; // (Cs - Cd) * As + Cd: see-through threads
                webOp.zwrite = false;
                webOp.receives = true;
                webOp.generated = true;
                ops.push_back(webOp);
            }
        }
        // PEBBLES AND DEBRIS: small stones and twigs on bare, flat ground (paths, dirt) near the camera, where the
        // ground's pixel is not green; the stones coloured like the ground under them, a little lighter or darker
        // each. Placed like the grass (spots kept per ground triangle).
        if (pebbles && !bankOfLiquid && !ate && ((!src.abe && src.zwrite) || groundLayer) && tv.grassy < 2 && tv.clear <= 0.1f && !tv.rgba.empty() &&
            wallShare < 0.3f)
        {
            Op op = src;
            op.abe = false; // (solid, writing depth, even over a blended ground layer)
            op.zwrite = true;
            op.first = uint32_t(out.verts.size());
            op.casts = false;
            op.relief = op.moss = 0.0f;
            const float cell = 1.0f / std::sqrt(std::max(0.8f * g_pebbles, 0.01f));
            for (uint32_t i = src.first; i + 2 < src.first + src.count; i += 3)
            {
                if (out.verts.size() - grassStart > kMaxGrassVerts)
                    break;
                const Vtx a = out.verts[i], b = out.verts[i + 1], c = out.verts[i + 2];
                if (touchesLiquid(a, b, c))
                    continue;
                float lo[3], hi[3];
                for (int k = 0; k < 3; ++k)
                {
                    lo[k] = std::min({a.world[k], b.world[k], c.world[k]});
                    hi[k] = std::max({a.world[k], b.world[k], c.world[k]});
                }
                float d2 = 0.0f;
                for (int k = 0; k < 3; ++k)
                {
                    const float e = std::max({lo[k] - centre[k], 0.0f, centre[k] - hi[k]});
                    d2 += e * e;
                }
                if (d2 > kPebbleFar * kPebbleFar)
                    continue;
                float e1[3], e2[3];
                for (int k = 0; k < 3; ++k)
                {
                    e1[k] = b.world[k] - a.world[k];
                    e2[k] = c.world[k] - a.world[k];
                }
                float nrm[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
                const float twoArea = std::sqrt(nrm[0] * nrm[0] + nrm[1] * nrm[1] + nrm[2] * nrm[2]);
                if (twoArea < 1e-4f)
                    continue;
                for (float &x : nrm)
                    x /= twoArea;
                if (nrm[1] < 0.0f)
                    for (float &x : nrm)
                        x = -x;
                if (nrm[1] < 0.8f) // flat ground only
                    continue;
                uint64_t tkey = 0x9E3779B97F4A7C15ull ^ src.version; // (salted: not the grass spots)
                for (const Vtx *tp : {&a, &b, &c})
                    for (int k = 0; k < 3; ++k)
                    {
                        uint32_t bits;
                        std::memcpy(&bits, &tp->world[k], 4);
                        tkey = (tkey ^ bits) * 1099511628211ull;
                    }
                if (newSpotBudget <= 0 && !g_grassCache.count(tkey))
                    continue;
                auto [slot, isNew] = g_grassCache.try_emplace(tkey);
                if (isNew)
                    newSpotBudget -= int(triArea(a, b, c) * 6.0f) + 20;
                GrassTri &gt = slot->second;
                const float ageFade = touchTri(gt);
                if (isNew)
                {
                    const float det = (b.world[2] - c.world[2]) * (a.world[0] - c.world[0]) +
                                      (c.world[0] - b.world[0]) * (a.world[2] - c.world[2]);
                    const int x0 = int(std::floor(lo[0] / cell)), x1 = int(std::floor(hi[0] / cell));
                    const int z0 = int(std::floor(lo[2] / cell)), z1 = int(std::floor(hi[2] / cell));
                    if (std::fabs(det) > 1e-6f && int64_t(x1 - x0 + 1) * (z1 - z0 + 1) <= 400000)
                    {
                        const float invDet = 1.0f / det;
                        for (int iz = z0; iz <= z1; ++iz)
                            for (int ix = x0; ix <= x1; ++ix)
                            {
                                uint32_t seed = uint32_t(ix) * 83492791u ^ uint32_t(iz) * 2654435761u ^ 0x5bd1e995u;
                                seed = hashF(float(seed & 0xFFFFFF)) ^ seed;
                                const float px0 = (float(ix) + rnd(seed)) * cell, pz0 = (float(iz) + rnd(seed)) * cell;
                                const float l0 = ((b.world[2] - c.world[2]) * (px0 - c.world[0]) +
                                                  (c.world[0] - b.world[0]) * (pz0 - c.world[2])) * invDet;
                                const float l1 = ((c.world[2] - a.world[2]) * (px0 - c.world[0]) +
                                                  (a.world[0] - c.world[0]) * (pz0 - c.world[2])) * invDet;
                                const float l2 = 1.0f - l0 - l1;
                                if (l0 < 0.0f || l1 < 0.0f || l2 < 0.0f)
                                    continue;
                                const float tu = a.uv[0] * l0 + b.uv[0] * l1 + c.uv[0] * l2;
                                const float tvv = a.uv[1] * l0 + b.uv[1] * l1 + c.uv[1] * l2;
                                const int px = int(std::floor(tu * float(tv.width))) & int(tv.width - 1);
                                const int py = int(std::floor(tvv * float(tv.height))) & int(tv.height - 1);
                                const uint32_t texel = tv.rgba[size_t(py) * tv.width + px];
                                const int tr = texel & 0xFF, tg = (texel >> 8) & 0xFF, tb = (texel >> 16) & 0xFF;
                                if (tv.grassy && Tracker::isGrassy(tr, tg, tb)) // green: grass grows there instead
                                    continue;
                                gt.spots.push_back({l1, l2, seed});
                            }
                    }
                }
                if (isNew)
                    buildChunks(gt, a.world, e1, e2);
                size_t chunkAt = 0;
                for (size_t si = 0; si < gt.spots.size(); ++si)
                {
                    if (chunkAt < gt.chunks.size() && si == gt.chunks[chunkAt].first)
                    {
                        const GrassChunk &ch = gt.chunks[chunkAt++];
                        const float cx = ch.c[0] - centre[0], cy = ch.c[1] - centre[1], cz = ch.c[2] - centre[2];
                        if (std::sqrt(cx * cx + cy * cy + cz * cz) - ch.r > kPebbleFar)
                        {
                            si += ch.count - 1; // (the whole chunk is out of reach)
                            continue;
                        }
                    }
                    const GrassSpot &spot = gt.spots[si];
                    uint32_t seed = spot.seed;
                    const float u = spot.u, v = spot.v, w0 = 1.0f - u - v;
                    float p[3];
                    for (int q = 0; q < 3; ++q)
                        p[q] = a.world[q] + e1[q] * u + e2[q] * v;
                    const float dx = p[0] - centre[0], dy = p[1] - centre[1], dz = p[2] - centre[2];
                    const float dd = dx * dx + dy * dy + dz * dz;
                    if (dd > kPebbleFar * kPebbleFar)
                        continue;
                    const float r1 = rnd(seed), r2 = rnd(seed), r3 = rnd(seed), r4 = rnd(seed), kind = rnd(seed);
                    // fewer, and only the bigger ones, further away
                    const float dist = std::sqrt(dd);
                    const float reach = std::clamp((kPebbleFar - dist) / 10.0f, 0.0f, 1.0f);
                    if (r4 > reach)
                        continue;
                    // (grows in, not pops in, as it comes into reach)
                    const float appear = std::min(1.0f, (reach - r4) / 0.25f) * ageFade;
                    if (g_floor.inLava(p[0], p[2], p[1]))
                        continue; // (not in or by the lava)
                    Vtx base;
                    for (int q = 0; q < 4; ++q)
                        base.col[q] = a.col[q] * w0 + b.col[q] * u + c.col[q] * v;
                    base.uv[0] = a.uv[0] * w0 + b.uv[0] * u + c.uv[0] * v;
                    base.uv[1] = a.uv[1] * w0 + b.uv[1] * u + c.uv[1] * v;
                    for (int q = 0; q < 3; ++q)
                    {
                        base.raw[q] = base.col[q];
                        base.mnormal[q] = 0.0f;
                    }
                    const float yaw = r3 * 6.2831853f;
                    // RAIN: a splash now and then on this spot: a ring spreading out and fading
                    if (raining && dist < 18.0f && p[1] >= g_roof.at(p[0], p[2]) - 0.2f)
                    {
                        const float r5 = rnd(seed), r6 = rnd(seed);
                        const float period = 0.6f + 0.9f * r5;
                        const float tt = float(g_tracker.frameNo % 360000u) / 60.0f / period + r6;
                        const float ph = tt - std::floor(tt);
                        if (r5 < 0.5f && ph < 0.3f)
                        {
                            const float f = ph / 0.3f;
                            const float rr = 0.05f + 0.28f * f;
                            const float alpha = 55.0f * (1.0f - f);
                            Vtx rim[9];
                            for (int k = 0; k <= 8; ++k)
                            {
                                const float ang = float(k) * 0.7853982f;
                                Vtx &o = rim[k];
                                o = Vtx{};
                                o.world[0] = p[0] + std::cos(ang) * rr;
                                o.world[1] = p[1] + 0.03f;
                                o.world[2] = p[2] + std::sin(ang) * rr;
                                o.col[0] = o.raw[0] = 160.0f;
                                o.col[1] = o.raw[1] = 170.0f;
                                o.col[2] = o.raw[2] = 190.0f;
                                o.col[3] = alpha;
                                toClip(o.world, o.pos);
                            }
                            Vtx mid = rim[0];
                            mid.world[0] = p[0];
                            mid.world[2] = p[2];
                            mid.col[3] = 0.0f;
                            toClip(mid.world, mid.pos);
                            for (int k = 0; k < 8; ++k)
                            {
                                g_weatherVerts.push_back(mid);
                                g_weatherVerts.push_back(rim[k]);
                                g_weatherVerts.push_back(rim[k + 1]);
                            }
                        }
                    }
                    if (kind < 0.15f)
                    {
                        // a twig: a thin dark strip lying on the ground
                        const float len = (0.35f + 0.6f * r1) * appear, wd = (0.03f + 0.02f * r2) * appear;
                        const float ax = std::cos(yaw), az = std::sin(yaw);
                        Vtx q4[4];
                        for (int j = 0; j < 4; ++j)
                        {
                            Vtx &o = q4[j];
                            o = base;
                            const float along = (j & 1) ? len * 0.5f : -len * 0.5f, side = (j & 2) ? wd : -wd;
                            o.world[0] = p[0] + ax * along - az * side;
                            o.world[1] = p[1] + 0.03f + along * 0.04f;
                            o.world[2] = p[2] + az * along + ax * side;
                            const float shade = 0.75f + 0.5f * r4;
                            o.col[0] = o.raw[0] = 58.0f * shade;
                            o.col[1] = o.raw[1] = 44.0f * shade;
                            o.col[2] = o.raw[2] = 32.0f * shade;
                            o.col[3] = 128.0f;
                            for (int q = 0; q < 3; ++q)
                                o.normal[q] = nrm[q];
                            toClip(o.world, o.pos);
                        }
                        for (int j : {0, 1, 2, 1, 3, 2})
                            flowers.push_back(q4[j]);
                        continue;
                    }
                    // (only under trees: a branch 3.5 to 30 units overhead, near enough)
                    const float overhead = g_leaves > 0.0f ? g_canopy.at(p[0], p[2]) - p[1] : -1.0f;
                    if (overhead >= 3.5f && overhead <= 30.0f && kind < 0.15f + 0.6f * std::min(1.0f, g_leaves))
                    {
                        // FALLEN LEAVES: a few dead leaves lying around the spot, each a pointed leaf a little
                        // curled up at its tip, in dull browns and ochres (darkened by the light on the ground)
                        static const float leafCols[4][3] = {{74, 46, 26}, {86, 62, 30}, {62, 40, 28}, {80, 34, 22}};
                        const float lf = std::clamp((base.col[0] * 0.3f + base.col[1] * 0.59f + base.col[2] * 0.11f) / 100.0f,
                                                    0.35f, 1.3f);
                        for (int lv = 0; lv < 3; ++lv)
                        {
                            const float ra = rnd(seed), rb = rnd(seed), rc = rnd(seed);
                            const float lx = p[0] + (ra - 0.5f) * 0.7f, lz = p[2] + (rb - 0.5f) * 0.7f;
                            const float ang = rc * 6.2831853f, ca = std::cos(ang), sa = std::sin(ang);
                            const float ls = (0.08f + 0.06f * ra) * std::max(0.6f, g_leaves) * appear;
                            const float *lcol = leafCols[std::min(3, int(rb * 4.0f))];
                            const float shade = (0.75f + 0.4f * rc) * lf;
                            const float pts[4][3] = {{-1.0f, 0.0f, 0.0f}, {0.0f, 0.0f, 0.45f}, {1.0f, 0.12f, 0.0f}, {0.0f, 0.0f, -0.45f}};
                            Vtx q4[4];
                            for (int j = 0; j < 4; ++j)
                            {
                                Vtx &o = q4[j];
                                o = base;
                                o.world[0] = lx + (ca * pts[j][0] - sa * pts[j][2]) * ls;
                                o.world[1] = p[1] + 0.025f + pts[j][1] * ls * 2.0f;
                                o.world[2] = lz + (sa * pts[j][0] + ca * pts[j][2]) * ls;
                                for (int q = 0; q < 3; ++q)
                                {
                                    o.col[q] = o.raw[q] = lcol[q] * shade * (j == 2 ? 1.15f : 1.0f);
                                    o.normal[q] = nrm[q];
                                }
                                o.col[3] = 128.0f;
                                toClip(o.world, o.pos);
                            }
                            for (int j : {0, 1, 2, 0, 2, 3})
                                flowers.push_back(q4[j]);
                        }
                        continue;
                    }
                    // a stone: a low five-sided mound, a little sunk into the ground; mostly small, some bigger
                    // (the small ones shrink away further out, the big ones stay)
                    const float smallFade = r1 < 0.5f ? std::clamp((kPebbleFar * 0.75f - dist) / 6.0f, 0.0f, 1.0f) : 1.0f;
                    if (smallFade <= 0.0f)
                        continue;
                    const float rad = (0.06f + 0.24f * r1 * r1) * appear * smallFade, hgt = rad * (0.45f + 0.45f * r2);
                    const float tint = 0.75f + 0.35f * r4;
                    float ring[5][3];
                    uint32_t s2 = seed;
                    for (int k = 0; k < 5; ++k)
                    {
                        const float ang = yaw + float(k) * 1.2566371f;
                        const float rr = rad * (0.7f + 0.6f * rnd(s2));
                        ring[k][0] = p[0] + std::cos(ang) * rr;
                        ring[k][1] = p[1] - 0.03f;
                        ring[k][2] = p[2] + std::sin(ang) * rr;
                    }
                    const float top[3] = {p[0] + (r2 - 0.5f) * rad * 0.4f, p[1] + hgt, p[2] + (r3 - 0.5f) * rad * 0.4f};
                    for (int k = 0; k < 5; ++k)
                    {
                        const float *A = ring[k], *B = ring[(k + 1) % 5];
                        float f1[3], f2[3];
                        for (int q = 0; q < 3; ++q)
                        {
                            f1[q] = B[q] - A[q];
                            f2[q] = top[q] - A[q];
                        }
                        float fn[3] = {f1[1] * f2[2] - f1[2] * f2[1], f1[2] * f2[0] - f1[0] * f2[2], f1[0] * f2[1] - f1[1] * f2[0]};
                        const float fl = std::sqrt(fn[0] * fn[0] + fn[1] * fn[1] + fn[2] * fn[2]);
                        if (fl < 1e-9f)
                            continue;
                        if (fn[1] < 0.0f)
                            for (float &x : fn)
                                x = -x;
                        // each face a little lighter or darker by which way it faces (soft light from above), the
                        // top lighter and the foot darker, so the stone reads as a lump and not a flat patch
                        const float face = 0.7f + 0.45f * std::max(0.0f, (fn[0] * 0.3f + fn[1] * 0.9f + fn[2] * 0.25f) / fl);
                        for (const float *P : {A, B, top})
                        {
                            Vtx o = base;
                            const float shade = tint * face * (P == top ? 1.15f : 0.8f);
                            for (int q = 0; q < 3; ++q)
                            {
                                o.world[q] = P[q];
                                o.normal[q] = fn[q] / fl;
                                o.col[q] *= shade;
                                o.raw[q] = o.col[q];
                            }
                            toClip(o.world, o.pos);
                            out.verts.push_back(o);
                        }
                    }
                }
            }
            op.count = uint32_t(out.verts.size()) - op.first;
            if (op.count)
            {
                op.generated = true;
                ops.push_back(op);
            }
            if (!flowers.empty())
            {
                Op fo = op;
                fo.first = uint32_t(out.verts.size());
                fo.textured = false;
                fo.version = 0;
                out.verts.insert(out.verts.end(), flowers.begin(), flowers.end());
                fo.count = uint32_t(flowers.size());
                flowers.clear();
                fo.generated = true;
                ops.push_back(fo);
            }
            if (tv.grassy == 0 && !(!out.inLevel && tv.green >= 0.12f && tv.grey < 0.4f))
                continue; // (mossy stone goes on to the moss tufts below as well)
        }
        // GRASS: blades (one thin tapered triangle each) scattered over the grassy ground near the camera, where the
        // ground's own pixel is green, each coloured by that pixel and bent by the wind. Their spots come from a hash
        // of the ground triangle, so they stay put from frame to frame.
        // Ground only partly green (mossy stone, a worn path with grass at its edges): short moss tufts in its
        // green parts instead of tall blades.
        // (the title menu: its mossy ground gets the tall grass of the levels)
        const bool menuGrass = !out.inLevel && tv.green >= 0.12f && tv.grey < 0.4f;
        if (!grass || bankOfLiquid || ate || !((!src.abe && src.zwrite) || groundLayer) || (tv.grassy == 0 && !menuGrass) || tv.clear > 0.1f ||
            tv.rgba.empty() || (src.shiny && wallShare >= 0.3f))
            continue;
        const bool mossy = tv.grassy == 1 && !menuGrass;
        Op op = src;
        op.abe = false; // (solid, writing depth, even over a blended ground layer)
        op.zwrite = true;
        op.first = uint32_t(out.verts.size());
        op.casts = false;
        op.relief = op.moss = 0.0f;
        const float density = (mossy ? 4.0f : 4.5f) * g_grass; // clumps per square unit
        const float cellSize = 1.0f / std::sqrt(std::max(density, 0.01f));
        for (uint32_t i = src.first; i + 2 < src.first + src.count; i += 3)
        {
            if (out.verts.size() - grassStart > kMaxGrassVerts || grassCandidates > kMaxGrassCandidates)
                break;
            // (copies: the blades are added to the same vertex list, which can move in memory as it grows)
            const Vtx a = out.verts[i], b = out.verts[i + 1], c = out.verts[i + 2];
            if (touchesLiquid(a, b, c))
                continue;
            float lo[3], hi[3];
            for (int k = 0; k < 3; ++k)
            {
                lo[k] = std::min({a.world[k], b.world[k], c.world[k]});
                hi[k] = std::max({a.world[k], b.world[k], c.world[k]});
            }
            // the triangle's box against the grass area around the centre
            float d2 = 0.0f;
            for (int k = 0; k < 3; ++k)
            {
                const float e = std::max({lo[k] - centre[k], 0.0f, centre[k] - hi[k]});
                d2 += e * e;
            }
            if (d2 > kGrassFar * kGrassFar)
                continue;
            float e1[3], e2[3];
            for (int k = 0; k < 3; ++k)
            {
                e1[k] = b.world[k] - a.world[k];
                e2[k] = c.world[k] - a.world[k];
            }
            float nrm[3] = {e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]};
            const float twoArea = std::sqrt(nrm[0] * nrm[0] + nrm[1] * nrm[1] + nrm[2] * nrm[2]);
            if (twoArea < 1e-4f)
                continue;
            for (float &x : nrm)
                x /= twoArea;
            if (nrm[1] < 0.0f)
                for (float &x : nrm)
                    x = -x;
            if (nrm[1] < 0.45f) // walls and steep banks: no grass
                continue;
            // The spots where something grows on this triangle: on a grid across the ground (x, z), each moved a
            // little by a hash of its cell (the same spots every frame, every spot in exactly one triangle), kept
            // only where the ground's pixel is green. Worked out once per triangle and kept: the ground does not
            // move, and this is most of the work.
            uint64_t tkey = 1469598103934665603ull ^ src.version;
            for (const Vtx *t : {&a, &b, &c})
                for (int k = 0; k < 3; ++k)
                {
                    uint32_t bits;
                    std::memcpy(&bits, &t->world[k], 4);
                    tkey = (tkey ^ bits) * 1099511628211ull;
                }
            if (newSpotBudget <= 0 && !g_grassCache.count(tkey))
                continue;
            auto [slot, isNew] = g_grassCache.try_emplace(tkey);
            if (isNew)
                newSpotBudget -= int(triArea(a, b, c) * 6.0f) + 20;
            GrassTri &gt = slot->second;
            const float ageFade = touchTri(gt);
            if (isNew)
            {
                const float det = (b.world[2] - c.world[2]) * (a.world[0] - c.world[0]) +
                                  (c.world[0] - b.world[0]) * (a.world[2] - c.world[2]);
                const int x0 = int(std::floor(lo[0] / cellSize)), x1 = int(std::floor(hi[0] / cellSize));
                const int z0 = int(std::floor(lo[2] / cellSize)), z1 = int(std::floor(hi[2] / cellSize));
                if (std::fabs(det) > 1e-6f && int64_t(x1 - x0 + 1) * (z1 - z0 + 1) <= 400000)
                {
                    const float invDet = 1.0f / det;
                    for (int iz = z0; iz <= z1; ++iz)
                        for (int ix = x0; ix <= x1; ++ix)
                        {
                            uint32_t seed = uint32_t(ix) * 73856093u ^ uint32_t(iz) * 19349663u;
                            seed = hashF(float(seed & 0xFFFFFF)) ^ seed;
                            const float px0 = (float(ix) + rnd(seed)) * cellSize, pz0 = (float(iz) + rnd(seed)) * cellSize;
                            const float l0 = ((b.world[2] - c.world[2]) * (px0 - c.world[0]) +
                                              (c.world[0] - b.world[0]) * (pz0 - c.world[2])) * invDet;
                            const float l1 = ((c.world[2] - a.world[2]) * (px0 - c.world[0]) +
                                              (a.world[0] - c.world[0]) * (pz0 - c.world[2])) * invDet;
                            const float l2 = 1.0f - l0 - l1;
                            if (l0 < 0.0f || l1 < 0.0f || l2 < 0.0f)
                                continue;
                            const float tu = a.uv[0] * l0 + b.uv[0] * l1 + c.uv[0] * l2;
                            const float tvv = a.uv[1] * l0 + b.uv[1] * l1 + c.uv[1] * l2;
                            const int px = int(std::floor(tu * float(tv.width))) & int(tv.width - 1);
                            const int py = int(std::floor(tvv * float(tv.height))) & int(tv.height - 1);
                            const uint32_t texel = tv.rgba[size_t(py) * tv.width + px];
                            const int tr = texel & 0xFF, tg = (texel >> 8) & 0xFF, tb = (texel >> 16) & 0xFF;
                            if (!Tracker::isGrassy(tr, tg, tb))
                                continue;
                            gt.spots.push_back({l1, l2, seed});
                        }
                }
                grassCandidates += uint32_t(gt.spots.size());
            }
            if (isNew)
                buildChunks(gt, a.world, e1, e2);
            size_t chunkAt = 0;
            for (size_t si = 0; si < gt.spots.size(); ++si)
            {
                if (chunkAt < gt.chunks.size() && si == gt.chunks[chunkAt].first)
                {
                    const GrassChunk &ch = gt.chunks[chunkAt++];
                    const float cx = ch.c[0] - centre[0], cy = ch.c[1] - centre[1], cz = ch.c[2] - centre[2];
                    if (std::sqrt(cx * cx + cy * cy + cz * cz) - ch.r > kGrassFar)
                    {
                        si += ch.count - 1; // (the whole chunk is out of reach)
                        continue;
                    }
                }
                const GrassSpot &spot = gt.spots[si];
                uint32_t seed = spot.seed;
                const float u = spot.u, v = spot.v;
                const float r4 = rnd(seed);
                float p[3];
                for (int q = 0; q < 3; ++q)
                    p[q] = a.world[q] + e1[q] * u + e2[q] * v;
                const float dx = p[0] - centre[0], dy = p[1] - centre[1], dz = p[2] - centre[2];
                const float d2 = dx * dx + dy * dy + dz * dz;
                if (d2 > kGrassFar * kGrassFar)
                    continue;
                const float dist = std::sqrt(d2);
                // none right in front of the lens (giant blades filling the screen when the camera is low)
                float dcam;
                {
                    const float cx = p[0] - out.camPos[0], cy = p[1] - out.camPos[1], cz = p[2] - out.camPos[2];
                    const float c2 = cx * cx + cy * cy + cz * cz;
                    if (c2 < kGrassNearCam * kGrassNearCam)
                        continue;
                    dcam = std::sqrt(c2);
                }
                // thinning out with distance (the blades left get shorter, so nothing pops)
                const float keep = std::clamp((kGrassFar - dist) / (kGrassFar - kGrassNear), 0.0f, 1.0f);
                // fewer, wider blades further away (they are smaller on screen: the ground stays as covered for a
                // fraction of the work), and the far ones in one triangle instead of three. By the distance from
                // the camera (the size on screen): the blades between the camera and Maximo are the biggest on
                // screen, and get full detail.
                const float lodFar = camToCentre * kLodShare;
                const float lod = dcam < lodFar ? 1.0f : (lodFar / dcam) * (lodFar / dcam);
                if (r4 > keep * lod)
                    continue;
                // (grows up out of the ground as it comes into reach or the detail rises, instead of popping in)
                const float appear = ageFade * std::min(1.0f, (keep * lod - r4) / 0.2f) *
                                     std::clamp((dcam - kGrassNearCam) / 2.5f, 0.0f, 1.0f);
                if (g_floor.inLava(p[0], p[2], p[1]))
                    continue;
                const float r1 = rnd(seed), r2 = rnd(seed), r3 = rnd(seed);
                const float widen = std::min(2.5f, 1.0f / std::sqrt(lod));
                const bool simple = dcam > lodFar + 9.0f;
                const float w0 = 1.0f - u - v;
                const float tu = a.uv[0] * w0 + b.uv[0] * u + c.uv[0] * v, tv2 = a.uv[1] * w0 + b.uv[1] * u + c.uv[1] * v;
                // what grows here: mostly blades of grass, some tall weeds, now and then a wild flower
                const float kind = rnd(seed);
                const bool weed = !mossy && kind < 0.035f, flower = kind > (mossy ? 0.997f : 0.993f);
                const float h = g_grassHeight * (mossy ? 0.4f : 1.0f) * (0.6f + 0.6f * r1) * appear *
                                (weed ? 1.4f : (flower ? 0.9f : 1.0f));
                const float wdt = (0.05f + 0.035f * r2) * (weed ? 1.6f : 1.0f) * widen;
                const float yaw = r3 * 6.2831853f;
                float sx = std::cos(yaw) * wdt, sz = std::sin(yaw) * wdt;
                float hb = h; // (this blade's height: the clump's others are shorter)
                float bend[3];
                sway(p, 0.35f * g_wind * (weed ? 1.5f : 1.0f), bend);
                // a little lean of its own
                bend[0] += (r2 - 0.5f) * 0.5f * h;
                bend[2] += (r1 - 0.5f) * 0.5f * h;
                Vtx base;
                for (int q = 0; q < 4; ++q)
                    base.col[q] = a.col[q] * w0 + b.col[q] * u + c.col[q] * v;
                for (int q = 0; q < 3; ++q)
                {
                    base.raw[q] = a.raw[q] * w0 + b.raw[q] * u + c.raw[q] * v;
                    base.normal[q] = nrm[q];
                    base.mnormal[q] = 0.0f; // coloured by the ground's lit colour, not relit
                }
                base.uv[0] = tu;
                base.uv[1] = tv2;
                // a curved blade: a quad from the root to 55% up (bent a little), then the tip (bent fully);
                // darker at the root (in the others' shade), lighter and a little greener up top
                auto at = [&](float side, float up, float bendShare, float shade) {
                    Vtx o = base;
                    const float taper = 1.0f - up * 0.75f;
                    o.world[0] = p[0] + side * sx * taper + bend[0] * bendShare;
                    o.world[1] = p[1] + hb * up;
                    o.world[2] = p[2] + side * sz * taper + bend[2] * bendShare;
                    for (int q = 0; q < 3; ++q)
                    {
                        o.col[q] *= shade * (shade > 1.0f ? (q == 1 ? 1.05f : 0.95f) : 1.0f);
                        o.raw[q] = o.col[q];
                    }
                    toClip(o.world, o.pos);
                    return o;
                };
                const Vtx r0 = at(-1.0f, 0.0f, 0.0f, 0.6f), r1v = at(1.0f, 0.0f, 0.0f, 0.6f);
                const Vtx tp = at(0.0f, 1.0f, 1.0f, 1.15f);
                if (simple)
                {
                    out.verts.push_back(r0);
                    out.verts.push_back(r1v);
                    out.verts.push_back(tp);
                }
                else
                {
                    const Vtx m0 = at(-1.0f, 0.55f, 0.3f, 0.9f), m1 = at(1.0f, 0.55f, 0.3f, 0.9f);
                    for (const Vtx *vv : {&r0, &r1v, &m0, &r1v, &m1, &m0, &m0, &m1, &tp})
                        out.verts.push_back(*vv);
                    // a clump, not a lone spike: a couple of shorter blades from the same root, fanning out
                    if (!weed && !flower)
                    {
                        const float p0[3] = {p[0], p[1], p[2]};
                        const float b0[3] = {bend[0], bend[1], bend[2]};
                        // (the further ones with fewer, but faded out with distance, not dropped: the first extra
                        // blade shrinks away before the clump turns into one simple blade, the second a little nearer)
                        const int extra = mossy ? 1 : 2;
                        for (int e = 0; e < extra; ++e)
                        {
                            const float ry = rnd(seed), rh = rnd(seed);
                            const float grow = e == 0 ? std::clamp((lodFar + 9.0f - dcam) / 5.0f, 0.0f, 1.0f)
                                                      : std::clamp((lodFar + 2.0f - dcam) / 4.0f, 0.0f, 1.0f);
                            if (grow <= 0.0f)
                                continue;
                            const float y2 = yaw + 2.1f * float(e + 1) + (ry - 0.5f) * 0.8f;
                            const float ox = std::cos(y2 + 1.5707963f), oz = std::sin(y2 + 1.5707963f);
                            hb = h * (0.6f + 0.3f * rh) * grow;
                            sx = std::cos(y2) * wdt * 0.85f;
                            sz = std::sin(y2) * wdt * 0.85f;
                            p[0] = p0[0] + ox * 0.05f;
                            p[2] = p0[2] + oz * 0.05f;
                            bend[0] = b0[0] + ox * 0.45f * hb;
                            bend[2] = b0[2] + oz * 0.45f * hb;
                            // (one triangle each: short and thin, the curve does not show)
                            out.verts.push_back(at(-1.0f, 0.0f, 0.0f, 0.55f));
                            out.verts.push_back(at(1.0f, 0.0f, 0.0f, 0.55f));
                            out.verts.push_back(at(0.0f, 1.0f, 1.0f, 1.1f));
                        }
                        p[0] = p0[0];
                        p[2] = p0[2];
                    }
                }
                if (flower)
                {
                    // the flower head: five petals around a light middle, opening upwards and tipped a little,
                    // in one of a few soft colours (dark like the rest of the graveyard)
                    static const float petals[4][3] = {{120, 110, 70}, {95, 72, 112}, {118, 118, 118}, {112, 62, 68}};
                    const float *pc = petals[std::min(3, int(r4 * 4.0f))];
                    const float s = 0.075f * g_grassHeight * appear;
                    Vtx mid = tp;
                    mid.col[0] = mid.raw[0] = 150.0f;
                    mid.col[1] = mid.raw[1] = 135.0f;
                    mid.col[2] = mid.raw[2] = 60.0f;
                    mid.col[3] = 128.0f;
                    for (int k = 0; k < 5; ++k)
                    {
                        const float a0 = yaw + float(k) * 1.2566371f;
                        Vtx tri3[3] = {mid, tp, tp};
                        for (int e = 1; e <= 2; ++e)
                        {
                            const float a = a0 + (e == 1 ? -0.42f : 0.42f);
                            Vtx &o = tri3[e];
                            o.world[0] = tp.world[0] + std::cos(a) * s;
                            o.world[1] = tp.world[1] + s * 0.35f + std::cos(a - yaw) * s * 0.25f;
                            o.world[2] = tp.world[2] + std::sin(a) * s;
                            for (int q = 0; q < 3; ++q)
                                o.col[q] = o.raw[q] = pc[q];
                            o.col[3] = 128.0f;
                            o.normal[0] = 0.0f;
                            o.normal[1] = 1.0f;
                            o.normal[2] = 0.0f;
                        }
                        for (Vtx &o : tri3)
                        {
                            toClip(o.world, o.pos);
                            flowers.push_back(o);
                        }
                    }
                }
            }
        }
        op.count = uint32_t(out.verts.size()) - op.first;
        if (op.count)
        {
            op.generated = true;
            ops.push_back(op);
        }
        if (!flowers.empty())
        {
            Op fo = op;
            fo.first = uint32_t(out.verts.size());
            fo.textured = false;
            fo.version = 0;
            out.verts.insert(out.verts.end(), flowers.begin(), flowers.end());
            fo.count = uint32_t(flowers.size());
            flowers.clear();
            fo.generated = true;
            ops.push_back(fo);
        }
    }
    out.ops.swap(ops);
}

void buildDrawList(const Frame &f, Resolver &res, DrawList &out)
{
    g_hudNow = f.hud;
    Mat world = g_state.world, view = g_state.view, screen = g_state.screen;
    Mat LD{}, LC{};
    LD.m[15] = 1.0f;
    LC.m[12] = LC.m[13] = LC.m[14] = 1.0f;
    Mat stAnim = Mat::identity(), smm = Mat::identity();
    std::unordered_map<uint32_t, Mat> bones;
    std::unordered_map<uint32_t, std::pair<Mat, Mat>> boneCache; // bone * world, and * view * screen
    std::vector<P> pv;
    out.views.push_back(view); // the view carried over from the previous frame
    int viewId = 0;
    uint32_t version = 0;
    int rtTex = -1;
    bool textured = false;
    uint32_t alphaReg = 0x44, prmode = 0, clampReg = 0;
    uint64_t test = 0x50000, spriteTest = 0x50000;
    bool zwrite = true, stFlag = false, smFlag = false, prmodeCont = true, refFlag = false;
    int galpha = -1;
    int target = 0;
    for (const Cmd &c : f.cmds)
    {
        switch (c.cmd)
        {
        case 1: world = matAt(f, c); break;
        case 2:
        {
            view = matAt(f, c);
            viewId = -1;
            for (size_t v = 0; v < out.views.size(); ++v)
                if (!std::memcmp(out.views[v].m, view.m, sizeof(view.m)))
                    viewId = int(v);
            if (viewId < 0)
            {
                out.views.push_back(view);
                viewId = int(out.views.size()) - 1;
            }
            if (target && !out.haveLight)
            {
                // the view of the game's shadow pass looks along the light: its z axis in world space
                out.lightDir[0] = view.m[2];
                out.lightDir[1] = view.m[6];
                out.lightDir[2] = view.m[10];
                const float l = std::sqrt(out.lightDir[0] * out.lightDir[0] + out.lightDir[1] * out.lightDir[1] +
                                          out.lightDir[2] * out.lightDir[2]);
                if (l > 1e-4f)
                {
                    for (float &x : out.lightDir)
                        x /= l;
                    out.haveLight = true;
                    // The game lights Maximo's blob shadow from almost straight above, so shadows would only fall
                    // under things. Lean the light (keeping the game's direction across the ground, if it has one)
                    // so objects cast shadows that can be seen.
                    if (g_shadowTilt > 0.0f)
                    {
                        float *d = out.lightDir;
                        // "down" is the light's main axis; the across-ground part is what is left of it
                        int axis = 0;
                        for (int i = 1; i < 3; ++i)
                            if (std::fabs(d[i]) > std::fabs(d[axis]))
                                axis = i;
                        float down[3] = {0, 0, 0};
                        down[axis] = d[axis] > 0 ? 1.0f : -1.0f;
                        float across[3] = {d[0] - down[0] * std::fabs(d[axis]), d[1] - down[1] * std::fabs(d[axis]),
                                           d[2] - down[2] * std::fabs(d[axis])};
                        float al = std::sqrt(across[0] * across[0] + across[1] * across[1] + across[2] * across[2]);
                        if (al < 1e-3f)
                        {
                            across[0] = across[1] = across[2] = 0.0f;
                            across[(axis + 1) % 3] = 0.8f;
                            across[(axis + 2) % 3] = 0.6f;
                            al = 1.0f;
                        }
                        const float t = g_shadowTilt * 3.14159265f / 180.0f;
                        for (int i = 0; i < 3; ++i)
                            d[i] = down[i] * std::cos(t) + across[i] / al * std::sin(t);
                    }
                }
            }
            break;
        }
        case 3:
            screen = matAt(f, c);
            g_state.haveScreen = true;
            out.inLevel = true;
            break;
        case 5: LD = matAt(f, c); break;
        case 6: LC = matAt(f, c); break;
        case 43: bones[c.w[0]] = matAt(f, c); break;
        case 41: bones.clear(); break;
        case 11: case 12:
        {
            if (c.blobSize < 0x38)
                break;
            uint64_t tex0;
            std::memcpy(&tex0, f.data.data() + c.blobOff + 0x30, 8);
            const uint32_t tbp = uint32_t(tex0) & 0x3FFF, psm = uint32_t(tex0 >> 20) & 0x3F;
            rtTex = -1;
            for (size_t t = 0; t < out.targets.size(); ++t)
                if (out.targets[t].fbp * 32u == tbp && psm == 0)
                    rtTex = int(t);
            // identity: the texture id (Texture) or the packet address (TextureEnv)
            const uint64_t identity = c.cmd == 11 ? (uint64_t(c.w[0]) | (uint64_t(1) << 40)) : uint64_t(c.w[0]);
            version = rtTex >= 0 ? 0 : res.resolve(Tracker::keyOf(tex0), identity);
            if (rtTex < 0)
            {
                // keyed by the texture alone (its id, or its packet for level textures): the game's texture cache
                // puts the same texture in a different slot from frame to frame, but its image stays the same
                // ...and by its size and format: the same id can stand for different textures (the sky and the
                // HUD's icon sheet were sharing one), but a texture's size and format never change with its slot
                const uint32_t tw = std::min(uint32_t(tex0 >> 26) & 15u, 10u), th = std::min(uint32_t(tex0 >> 30) & 15u, 10u);
                const uint32_t psm = uint32_t(tex0 >> 20) & 0x3F;
                const uint64_t ik = identity ^ (uint64_t(tw | (th << 4) | (psm << 8)) << 44);
                auto fits = [&](uint32_t v) {
                    auto it = g_tracker.versions.find(v);
                    return it != g_tracker.versions.end() && it->second.width == (1u << tw) &&
                           it->second.height == (1u << th) && (it->second.psm == psm || it->second.psm == 0xFFFFFFFFu);
                };
                if (version && !fits(version))
                    version = 0; // a picture of the wrong size or format: certainly another texture
                auto &votes = g_identityVotes[ik];
                // Recent sightings count most (older ones fade by 15% each time): a one-frame wrong pick is
                // outvoted, but a texture that really changed (the HUD after a menu, a reused packet) takes over
                // within a few frames instead of the old image winning forever.
                if (version && g_tracker.versions.count(version))
                {
                    for (auto &vc : votes)
                        vc.second *= 0.92f;
                    votes[version] += 1.0f;
                    if (votes.size() > 8)
                        for (auto it = votes.begin(); it != votes.end();)
                            it = it->second < 0.05f && it->first != version ? votes.erase(it) : std::next(it);
                }
                uint32_t best = 0;
                float bestCount = 0.0f;
                for (auto &vc : votes)
                    if (vc.second > bestCount && fits(vc.first))
                    {
                        best = vc.first;
                        bestCount = vc.second;
                    }
                // ...and the image shown only changes when another one wins by a quarter more, so two
                // images close in votes do not take turns from frame to frame (flicker)
                uint32_t &shown = g_identityShown[ik];
                if (shown && shown != best && g_tracker.versions.count(shown))
                {
                    auto sv = votes.find(shown);
                    if (sv != votes.end() && bestCount < sv->second * 1.25f && fits(shown))
                        best = shown;
                }
                if (best)
                {
                    shown = best;
                    version = best; // also covers a bind the GS frame did not show
                }
            }
            break;
        }
        case 16:
        {
            Op op;
            op.type = OpType::ClearDepth;
            op.target = target;
            out.ops.push_back(op);
            break;
        }
        case 17: zwrite = false; break;
        case 18: zwrite = true; break;
        case 19: galpha = -1; alphaReg = 0x44; break;
        case 20: galpha = int(c.w[0] & 0xFF); if (c.w[2] & 0xFF) alphaReg = c.w[2] & 0xFF; break;
        case 30: stFlag = (c.w[0] & 1) != 0; break;
        case 31: stAnim = matAt(f, c); break;
        case 32: refFlag = (c.w[0] & 0xFFu) == 1u; break; // reflection map: UVs from the view-space normal
        case 33:
            smFlag = c.w[0] == 1;
            out.inLevel = true;
            break;
        case 34: smm = matAt(f, c); break;
        case 39:
        {
            Offscreen o;
            o.fbp = c.w[0] & 0x1FF;
            o.width = o.height = std::max(64u, ((c.w[0] >> 16) & 0x3F) * 64u);
            if (o.width > 1024)
                o.width = o.height = 1024;
            out.targets.push_back(o);
            target = int(out.targets.size());
            break;
        }
        case 21:
        {
            const uint32_t reg = c.w[0], val = c.w[2];
            if (reg == 0x1A)
            {
                prmodeCont = val != 0;
                if (val == 1)
                    target = 0;
            }
            else if (reg == 0x1B)
                prmode = val;
            else if (reg == 0x42 || reg == 0x43)
                alphaReg = val;
            else if (reg == 0x47 || reg == 0x48)
                test = val;
            else if (reg == 0x08 || reg == 0x09)
                clampReg = val;
            break;
        }
        case 24:
        {
            // Particles (mist, fire, sparks, dust): per particle the centre (x, y, z) and a size (w), then the colour
            // as four integers, 32 bytes; w[1] = how many, w[2..4] = three floats the VU1 code is given (0.75, the
            // 4:3 aspect, then a size scale). Drawn as camera-facing squares: half side = size * scale, in view space.
            const uint32_t count = std::min(c.w[1], c.blobSize / 32u);
            if (!count)
                break;
            if (!version && rtTex < 0)
            {
                ++out.missingTextures;
                break;
            }
            float scale;
            std::memcpy(&scale, &c.w[3], 4);
            if (!(scale > 0.0f && scale < 1000.0f))
                scale = 1.0f;
            const Mat wv = mul(world, view);
            if (target == 0 && count >= 60)
            {
                // the game's own rain: a lot of small particles spread over a wide area
                float lo[2] = {1e30f, 1e30f}, hi[2] = {-1e30f, -1e30f}, sizes = 0.0f;
                for (uint32_t k = 0; k < count; ++k)
                {
                    float q[4];
                    std::memcpy(q, f.data.data() + c.blobOff + k * 32u, 16);
                    const float p4[4] = {q[0], q[1], q[2], 1.0f};
                    float wp[4];
                    xform(p4, world, wp);
                    lo[0] = std::min(lo[0], wp[0]);
                    hi[0] = std::max(hi[0], wp[0]);
                    lo[1] = std::min(lo[1], wp[2]);
                    hi[1] = std::max(hi[1], wp[2]);
                    sizes += q[3] * scale;
                }
                if (std::max(hi[0] - lo[0], hi[1] - lo[1]) > 10.0f && sizes / float(count) < 0.3f)
                    out.rainParticles += count;
            }
            const float halfW = target ? out.targets[target - 1].width * 0.5f : 320.0f;
            const float halfH = target ? out.targets[target - 1].height * 0.5f : 112.0f;
            Op op;
            op.target = target;
            op.first = uint32_t(out.verts.size());
            op.version = version;
            op.rtTexture = rtTex;
            op.textured = true;
            op.abe = true;
            op.zwrite = false;
            op.alphaReg = alphaReg;
            op.test = test & ~uint64_t(1); // no alpha test: soft puffs
            op.viewId = viewId;
            for (uint32_t k = 0; k < count; ++k)
            {
                float q[4];
                uint32_t ci[4];
                std::memcpy(q, f.data.data() + c.blobOff + k * 32u, 16);
                std::memcpy(ci, f.data.data() + c.blobOff + k * 32u + 16, 16);
                const float r = q[3] * scale; // (size * scale is the half side)
                if (!(r > 0.0f) || ci[3] == 0)
                    continue;
                const float p4[4] = {q[0], q[1], q[2], 1.0f};
                float vc[4];
                xform(p4, wv, vc);
                Vtx corner[4];
                bool behind = false;
                for (int j = 0; j < 4; ++j)
                {
                    const float dx = (j & 1) ? r : -r, dy = (j & 2) ? r : -r;
                    const float cv[4] = {vc[0] + dx, vc[1] + dy, vc[2], vc[3]};
                    float sp[4];
                    xform(cv, screen, sp);
                    if (sp[3] <= 1.0f)
                        behind = true;
                    Vtx &v = corner[j];
                    v.pos[0] = (sp[0] - 2048.0f * sp[3]) / halfW;
                    v.pos[1] = (sp[1] - 2048.0f * sp[3]) / halfH;
                    v.pos[2] = 1.0f;
                    v.pos[3] = sp[3];
                    v.uv[0] = (j & 1) ? 1.0f : 0.0f;
                    v.uv[1] = (j & 2) ? 1.0f : 0.0f;
                    for (int cc = 0; cc < 4; ++cc)
                        v.col[cc] = float(std::min(ci[cc], 255u)) * (cc < 3 ? g_particleBright : 1.0f);
                    if (galpha >= 0)
                        v.col[3] = float(galpha);
                    for (int cc = 0; cc < 3; ++cc)
                    {
                        v.world[cc] = q[cc];
                        v.normal[cc] = 0.0f;
                        v.raw[cc] = v.col[cc];
                        v.mnormal[cc] = 0.0f;
                    }
                }
                if (behind)
                    continue;
                for (int j : {0, 1, 2, 1, 3, 2})
                    out.verts.push_back(corner[j]);
            }
            op.count = uint32_t(out.verts.size()) - op.first;
            if (op.count)
                out.ops.push_back(op);
            break;
        }
        case 35: spriteTest = (c.w[2] & 0x70000u) == 0x50000u ? uint64_t(c.w[2]) : test; break;
        case 36:
        {
            // Sprites (HUD numbers, text): per sprite two corners (x, y, z, 1), the UV rectangle (u0, v0, u1, v1)
            // and a colour (r, g, b, a floats), 64 bytes; w[1] = how many. Drawn as quads with the current matrices.
            const uint32_t count = std::min(c.w[1], c.blobSize / 64u);
            if (!count)
                break;
            if (!version && rtTex < 0)
            {
                ++out.missingTextures;
                break;
            }
            const Mat wvs = mul(mul(world, view), screen);
            const float halfW = target ? out.targets[target - 1].width * 0.5f : 320.0f;
            const float halfH = target ? out.targets[target - 1].height * 0.5f : 112.0f;
            Op op;
            op.target = target;
            op.first = uint32_t(out.verts.size());
            op.version = version;
            op.rtTexture = rtTex;
            op.textured = true;
            op.abe = true;
            op.zwrite = zwrite;
            op.alphaReg = alphaReg;
            op.test = spriteTest;
            for (uint32_t k = 0; k < count; ++k)
            {
                float q[16];
                std::memcpy(q, f.data.data() + c.blobOff + k * 64u, 64);
                const float xs[2] = {q[0], q[4]}, ys[2] = {q[1], q[5]}, us[2] = {q[8], q[10]}, vs2[2] = {q[9], q[11]};
                Vtx corner[4];
                for (int j = 0; j < 4; ++j)
                {
                    const int ix = j & 1, iy = j >> 1;
                    const float p4[4] = {xs[ix], ys[iy], q[2], 1.0f};
                    float sp[4];
                    xform(p4, wvs, sp);
                    Vtx &v = corner[j];
                    v.pos[0] = (sp[0] - 2048.0f * sp[3]) / halfW;
                    v.pos[1] = (sp[1] - 2048.0f * sp[3]) / halfH;
                    v.pos[2] = 1.0f; // reversed depth: 1/w
                    v.pos[3] = sp[3];
                    v.uv[0] = us[ix];
                    v.uv[1] = vs2[iy];
                    std::memcpy(v.col, q + 12, 16);
                    v.world[0] = v.world[1] = v.world[2] = 0.0f;
                    v.normal[0] = v.normal[1] = v.normal[2] = 0.0f;
                    std::memcpy(v.raw, v.col, 12);
                    v.mnormal[0] = v.mnormal[1] = v.mnormal[2] = 0.0f;
                    if (galpha >= 0)
                        v.col[3] = float(galpha);
                }
                for (int j : {0, 1, 2, 1, 3, 2})
                    out.verts.push_back(corner[j]);
            }
            op.count = uint32_t(out.verts.size()) - op.first;
            out.ops.push_back(op);
            break;
        }
        case 27:
        {
            // WATER (Grid): the game's water surface, a grid it moves on VU1 with its own program. Its description
            // (0x3D0 bytes) holds the four corners in the world (+0x230, x y z 1 each), the colour (+0x30, r g b a
            // floats, 128 = full) and the texture (bound just before). Drawn here as a flat surface over the
            // corners, the texture tiled across it; the shader adds the waves and the reflections.
            if (c.blobSize < 0x270u)
                break;
            if (!version && rtTex < 0)
            {
                ++out.missingTextures;
                break;
            }
            const uint8_t *g = f.data.data() + c.blobOff;
            float corner[4][4], colour[4];
            std::memcpy(corner, g + 0x230, sizeof(corner));
            std::memcpy(colour, g + 0x30, sizeof(colour));
            bool sane = true;
            for (auto &cc : corner)
                for (int k = 0; k < 3; ++k)
                    sane = sane && std::isfinite(cc[k]) && std::fabs(cc[k]) < 1e6f;
            for (float &x : colour)
                sane = sane && std::isfinite(x);
            if (!sane)
                break;
            const Mat vs = mul(view, screen);
            const float halfW = target ? out.targets[target - 1].width * 0.5f : 320.0f;
            const float halfH = target ? out.targets[target - 1].height * 0.5f : 112.0f;
            Op op;
            op.target = target;
            op.first = uint32_t(out.verts.size());
            op.version = version;
            op.rtTexture = rtTex;
            op.textured = true;
            op.abe = true;
            op.zwrite = zwrite;
            op.alphaReg = alphaReg;
            op.test = test & ~uint64_t(1);
            op.viewId = viewId;
            op.receives = true;
            op.reflect = 0.75f;
            constexpr int kN = 16; // pieces each way (smaller triangles: the fog and the lights look right on them)
            Vtx grid[kN + 1][kN + 1];
            for (int iz = 0; iz <= kN; ++iz)
                for (int ix = 0; ix <= kN; ++ix)
                {
                    const float s0 = float(ix) / kN, t0 = float(iz) / kN;
                    Vtx &v = grid[iz][ix];
                    float w[4];
                    for (int k = 0; k < 3; ++k)
                    {
                        const float top = corner[0][k] + (corner[1][k] - corner[0][k]) * s0;
                        const float bot = corner[2][k] + (corner[3][k] - corner[2][k]) * s0;
                        w[k] = top + (bot - top) * t0;
                    }
                    w[3] = 1.0f;
                    float sp[4];
                    xform(w, vs, sp);
                    v.pos[0] = (sp[0] - 2048.0f * sp[3]) / halfW;
                    v.pos[1] = (sp[1] - 2048.0f * sp[3]) / halfH;
                    v.pos[2] = 1.0f;
                    v.pos[3] = sp[3];
                    v.uv[0] = w[0] * 0.0625f;
                    v.uv[1] = w[2] * 0.0625f;
                    std::memcpy(v.col, colour, 16);
                    if (galpha >= 0)
                        v.col[3] = float(galpha);
                    std::memcpy(v.world, w, 12);
                    v.normal[0] = 0.0f;
                    v.normal[1] = 1.0f;
                    v.normal[2] = 0.0f;
                    std::memcpy(v.raw, v.col, 12);
                    v.mnormal[0] = v.mnormal[1] = v.mnormal[2] = 0.0f;
                }
            for (int iz = 0; iz < kN; ++iz)
                for (int ix = 0; ix < kN; ++ix)
                    for (const Vtx *v : {&grid[iz][ix], &grid[iz][ix + 1], &grid[iz + 1][ix], &grid[iz][ix + 1],
                                         &grid[iz + 1][ix + 1], &grid[iz + 1][ix]})
                        out.verts.push_back(*v);
            op.count = uint32_t(out.verts.size()) - op.first;
            out.ops.push_back(op);
            break;
        }
        case 7:
        {
            if (!c.blobSize)
                break;
            // MOVING OBJECTS: the same model (its vertex data's address, and which time this frame it is drawn) with
            // a different placement than last frame moved; it counts as moving for a second and a half after
            bool movesNow = false;
            {
                struct Placed
                {
                    float m[16];
                    uint64_t movedAt;
                };
                static std::unordered_map<uint64_t, Placed> placed;
                static std::unordered_map<uint32_t, uint32_t> seen;
                static uint64_t seenFrame = ~0ull;
                if (seenFrame != g_tracker.frameNo)
                {
                    seenFrame = g_tracker.frameNo;
                    seen.clear();
                    if (placed.size() > 20000)
                        placed.clear();
                }
                const uint64_t key = (uint64_t(c.w[0]) << 16) ^ seen[c.w[0]]++;
                auto [it, fresh] = placed.try_emplace(key);
                if (fresh)
                {
                    std::memcpy(it->second.m, world.m, 64);
                    it->second.movedAt = 0;
                }
                else
                {
                    float d = 0.0f;
                    for (int k = 12; k < 15; ++k)
                        d = std::max(d, std::fabs(it->second.m[k] - world.m[k]));
                    for (int k = 0; k < 11; ++k)
                        d = std::max(d, std::fabs(it->second.m[k] - world.m[k]) * 10.0f);
                    if (d > 0.01f)
                        it->second.movedAt = g_tracker.frameNo + 1;
                    std::memcpy(it->second.m, world.m, 64);
                }
                movesNow = it->second.movedAt && g_tracker.frameNo + 1 - it->second.movedAt < 90;
            }
            const uint8_t *buf = f.data.data() + c.blobOff;
            const uint32_t size = c.blobSize;
            const Mat vs = mul(view, screen);
            const Mat wvs = mul(world, vs);
            boneCache.clear();
            const float halfW = target ? out.targets[target - 1].width * 0.5f : 320.0f;
            const float halfH = target ? out.targets[target - 1].height * 0.5f : 112.0f;
            // VIF codes: UNPACK V4-32 blocks, each a GIF tag and its vertices (4 qwords each).
            uint32_t i = 0;
            while (i + 4 <= size)
            {
                uint32_t code;
                std::memcpy(&code, buf + i, 4);
                i += 4;
                const uint32_t cmd = (code >> 24) & 0x7F, num = (code >> 16) & 0xFF, imm = code & 0xFFFF;
                if (cmd >= 0x60)
                {
                    const uint32_t vn = (cmd >> 2) & 3, vl = cmd & 3;
                    const uint32_t n = num ? num : 256;
                    static const uint32_t kVl[4] = {32, 16, 8, 5};
                    uint32_t bytes = (vn == 3 && vl == 3) ? n * 2 : (n * (vn + 1) * kVl[vl] + 31) / 32 * 4;
                    bytes = (bytes + 3) & ~3u;
                    if (vn == 3 && vl == 0 && i + bytes <= size)
                    {
                        const uint8_t *d = buf + i;
                        const uint32_t nq = bytes / 16;
                        uint32_t q = 0;
                        while (q < nq)
                        {
                            uint64_t tag;
                            std::memcpy(&tag, d + q * 16, 8);
                            const uint32_t nloop = uint32_t(tag & 0x7FFF);
                            const bool eop = (tag >> 15) & 1;
                            uint32_t prim = ((tag >> 46) & 1) ? uint32_t(tag >> 47) & 0x7FF : 0x1C;
                            ++q;
                            if (nloop && q + nloop * 4 <= nq)
                            {
                                if (!prmodeCont)
                                    prim = (prim & 7) | (prmode & 0x7F8);
                                const bool tme = (prim >> 4) & 1;
                                const bool abe = ((prim >> 6) & 1) || galpha >= 0;
                                const uint32_t type = prim & 7;
                                if (tme && !version && rtTex < 0)
                                {
                                    ++out.missingTextures;
                                    q += nloop * 4;
                                    if (eop)
                                        break;
                                    continue;
                                }
                                // transform the batch
                                pv.resize(nloop);
                                bool anySkinned = false; // (bones: a character)
                                for (uint32_t k = 0; k < nloop; ++k)
                                {
                                    const uint8_t *vq = d + (q + k * 4) * 16;
                                    float st[4], rgba[4], pos[4], nrm[4];
                                    std::memcpy(st, vq, 16);
                                    std::memcpy(rgba, vq + 16, 16);
                                    std::memcpy(pos, vq + 32, 16);
                                    std::memcpy(nrm, vq + 48, 16);
                                    uint32_t pw;
                                    std::memcpy(&pw, vq + 44, 4);
                                    const bool rigid = (pw & 0x7FFF0000u) == 0x3F800000u;
                                    float p4[4] = {pos[0], pos[1], pos[2], 1.0f}, wp[4], sp[4];
                                    const Mat *M = &world, *MS = &wvs;
                                    if (!rigid)
                                    {
                                        anySkinned = true;
                                        const uint32_t bi = ((pw & 0x7FFFu) - 0x190u) / 4u;
                                        auto cb = boneCache.find(bi);
                                        if (cb == boneCache.end())
                                        {
                                            auto b = bones.find(bi);
                                            if (b != bones.end())
                                            {
                                                const Mat bw = mul(b->second, world);
                                                cb = boneCache.emplace(bi, std::make_pair(bw, mul(bw, vs))).first;
                                            }
                                        }
                                        if (cb != boneCache.end())
                                        {
                                            M = &cb->second.first;
                                            MS = &cb->second.second;
                                        }
                                    }
                                    xform(p4, *M, wp);
                                    xform(p4, *MS, sp);
                                    P &o = pv[k];
                                    o.adc = (pw & 0x8000u) != 0;
                                    o.world[0] = wp[0];
                                    o.world[1] = wp[1];
                                    o.world[2] = wp[2];
                                    {
                                        const float n0[4] = {nrm[0], nrm[1], nrm[2], 0.0f};
                                        float nw[4];
                                        xform(n0, *M, nw);
                                        const float nl = std::sqrt(nw[0] * nw[0] + nw[1] * nw[1] + nw[2] * nw[2]);
                                        const float inv = nl > 1e-6f ? 1.0f / nl : 0.0f;
                                        o.normal[0] = nw[0] * inv;
                                        o.normal[1] = nw[1] * inv;
                                        o.normal[2] = nw[2] * inv;
                                    }
                                    o.clip[0] = (sp[0] - 2048.0f * sp[3]) / halfW;
                                    o.clip[1] = (sp[1] - 2048.0f * sp[3]) / halfH;
                                    // reversed depth: z/w = 1/w (1 at the near plane w = 1, towards 0 far away), which
                                    // keeps a float depth buffer precise at every distance
                                    o.clip[2] = 1.0f;
                                    o.clip[3] = sp[3];
                                    // lighting: d = (n, 1) * LD, the three lights clamped at 0, then d * LC
                                    float n4[4] = {nrm[0], nrm[1], nrm[2], 1.0f}, dl[4], lit[4];
                                    xform(n4, LD, dl);
                                    for (int j = 0; j < 3; ++j)
                                        dl[j] = std::max(dl[j], 0.0f);
                                    dl[3] = 1.0f;
                                    xform(dl, LC, lit);
                                    for (int j = 0; j < 3; ++j)
                                        o.col[j] = rgba[j] * std::max(lit[j], 0.0f);
                                    o.col[3] = galpha >= 0 ? float(galpha) : rgba[3];
                                    for (int j = 0; j < 3; ++j)
                                    {
                                        o.raw[j] = rgba[j];
                                        o.mnormal[j] = nrm[j];
                                    }
                                    if (smFlag)
                                    {
                                        float sm[4];
                                        xform(wp, smm, sm); // (wp is only needed here)
                                        const float w = std::fabs(sm[3]) > 1e-6f ? sm[3] : 1e-6f;
                                        o.uv[0] = sm[0] / w;
                                        o.uv[1] = sm[1] / w;
                                    }
                                    else if (refFlag)
                                    {
                                        // sphere map: the normal turned into view space, x and y to 0..1
                                        const float n0[4] = {nrm[0], nrm[1], nrm[2], 0.0f};
                                        float nw[4], nv[4];
                                        xform(n0, *M, nw);
                                        xform(nw, view, nv);
                                        const float len = std::sqrt(nv[0] * nv[0] + nv[1] * nv[1] + nv[2] * nv[2]);
                                        const float inv = len > 1e-6f ? 1.0f / len : 0.0f;
                                        o.uv[0] = nv[0] * inv * 0.5f + 0.5f;
                                        o.uv[1] = nv[1] * inv * 0.5f + 0.5f;
                                    }
                                    else if (stFlag)
                                    {
                                        const float s4[4] = {st[0], st[1], 0.0f, 1.0f};
                                        float r4[4];
                                        xform(s4, stAnim, r4);
                                        o.uv[0] = r4[0];
                                        o.uv[1] = r4[1];
                                    }
                                    else
                                    {
                                        o.uv[0] = st[0];
                                        o.uv[1] = st[1];
                                    }
                                }
                                Op op;
                                op.target = target;
                                op.first = uint32_t(out.verts.size());
                                op.version = tme ? version : 0;
                                op.rtTexture = tme ? rtTex : -1;
                                op.textured = tme;
                                op.abe = abe;
                                op.zwrite = zwrite;
                                op.alphaReg = alphaReg;
                                op.test = test;
                                op.clampUV = smFlag && (clampReg & 3u);
                                op.viewId = viewId;
                                if (out.viewScreens.size() < out.views.size())
                                    out.viewScreens.resize(out.views.size(), screen);
                                if (viewId >= 0)
                                    out.viewScreens[viewId] = screen;
                                op.special = smFlag || refFlag;
                                op.scrolls = stFlag;
                                op.lit = true;
                                op.skinned = anySkinned;
                                op.moves = movesNow;
                                std::memcpy(op.LD, LD.m, 64);
                                std::memcpy(op.LC, LC.m, 64);
                                {
                                    float dirs = 0.0f, cols = 0.0f;
                                    for (int r = 0; r < 3; ++r)
                                        for (int cc = 0; cc < 3; ++cc)
                                        {
                                            dirs += std::fabs(LD.m[r * 4 + cc]);
                                            cols += std::fabs(LC.m[r * 4 + cc]);
                                        }
                                    op.shiny = dirs > 0.01f && cols > 0.01f;
                                }
                                auto emit = [&](uint32_t a, uint32_t b, uint32_t cc) {
                                    for (uint32_t idx : {a, b, cc})
                                    {
                                        Vtx v;
                                        std::memcpy(v.pos, pv[idx].clip, 16);
                                        std::memcpy(v.uv, pv[idx].uv, 8);
                                        std::memcpy(v.col, pv[idx].col, 16);
                                        std::memcpy(v.world, pv[idx].world, 12);
                                        std::memcpy(v.normal, pv[idx].normal, 12);
                                        std::memcpy(v.raw, pv[idx].raw, 12);
                                        std::memcpy(v.mnormal, pv[idx].mnormal, 12);
                                        out.verts.push_back(v);
                                    }
                                };
                                if (type == 3) // triangle list
                                {
                                    for (uint32_t k = 0; k + 2 < nloop; k += 3)
                                        emit(k, k + 1, k + 2);
                                }
                                else if (type == 5) // fan
                                {
                                    for (uint32_t k = 2; k < nloop; ++k)
                                        if (!pv[k].adc)
                                            emit(0, k - 1, k);
                                }
                                else // strip
                                {
                                    for (uint32_t k = 2; k < nloop; ++k)
                                        if (!pv[k].adc)
                                            emit(k - 2, k - 1, k);
                                }
                                op.count = uint32_t(out.verts.size()) - op.first;
                                if (op.count)
                                {
                                    // merge with the previous draw when the state is the same
                                    if (!out.ops.empty())
                                    {
                                        Op &prev = out.ops.back();
                                        if (prev.type == OpType::Draw && prev.target == op.target &&
                                            prev.version == op.version && prev.rtTexture == op.rtTexture &&
                                            prev.textured == op.textured && prev.abe == op.abe &&
                                            prev.zwrite == op.zwrite && prev.alphaReg == op.alphaReg &&
                                            prev.test == op.test && prev.clampUV == op.clampUV &&
                                            prev.viewId == op.viewId && prev.special == op.special &&
                                            prev.scrolls == op.scrolls && prev.skinned == op.skinned &&
                                            prev.moves == op.moves &&
                                            prev.lit == op.lit && !std::memcmp(prev.LD, op.LD, 64) &&
                                            !std::memcmp(prev.LC, op.LC, 64) &&
                                            prev.first + prev.count == op.first)
                                        {
                                            prev.count += op.count;
                                        }
                                        else
                                            out.ops.push_back(op);
                                    }
                                    else
                                        out.ops.push_back(op);
                                }
                            }
                            q += nloop * 4;
                            if (eop)
                                break;
                        }
                    }
                    i += bytes;
                }
                else if (cmd == 0x20)
                    i += 4;
                else if (cmd == 0x30 || cmd == 0x31)
                    i += 16;
                else if (cmd == 0x4A)
                    i += (num ? num : 256) * 8;
                else if (cmd == 0x50 || cmd == 0x51)
                    i += (imm ? imm : 65536) * 16;
            }
            break;
        }
        default:
            break;
        }
    }
    g_state.world = world;
    g_state.view = view;
    g_state.screen = screen;

    // The main camera: the view most of the main picture's vertices are drawn with (the sky and the HUD have
    // their own). Its geometry casts and receives the real-time shadows.
    std::vector<uint64_t> perView(out.views.size(), 0);
    for (const Op &op : out.ops)
        if (op.type == OpType::Draw && op.target == 0 && op.viewId >= 0)
            perView[op.viewId] += op.count;
    uint64_t most = 0;
    for (size_t v = 0; v < perView.size(); ++v)
        if (perView[v] > most)
        {
            most = perView[v];
            out.mainView = int(v);
        }
    if (out.mainView >= 0)
    {
        const Mat &V = out.views[out.mainView];
        // world -> view is p * V (rows); the camera sits at -t * R^T and looks along view +z
        const float t[3] = {V.m[12], V.m[13], V.m[14]};
        for (int i = 0; i < 3; ++i)
            out.camPos[i] = -(t[0] * V.m[i * 4 + 0] + t[1] * V.m[i * 4 + 1] + t[2] * V.m[i * 4 + 2]);
        out.camFwd[0] = V.m[2];
        out.camFwd[1] = V.m[6];
        out.camFwd[2] = V.m[10];
        for (Op &op : out.ops)
            if (op.type == OpType::Draw && op.target == 0 && op.viewId == out.mainView)
            {
                op.receives = true;
                op.casts = !op.abe && op.zwrite && !op.special;
                const uint32_t A = op.alphaReg & 3, B = (op.alphaReg >> 2) & 3, D = (op.alphaReg >> 6) & 3;
                if (op.abe && A == 0 && B == 2 && D == 1 && !op.special)
                    op.emissive = true; // additive: light itself (lighting it again made fire into white blobs)
                else if (op.textured && op.version && !op.special && op.count >= 3)
                {
                    auto it = g_tracker.versions.find(op.version);
                    if (it != g_tracker.versions.end() && it->second.hot >= 0.45f)
                        op.emissive = op.lava = true;
                }
                if (op.emissive)
                {
                    op.receives = false;
                    op.casts = false;
                }
                // STRUCTURES
                if (op.textured && op.version && !op.special && !op.emissive)
                {
                    auto it = g_tracker.versions.find(op.version);
                    if (it != g_tracker.versions.end())
                    {
                        const TexVersion &tv = it->second;
                        const bool ate = (op.test & 1) != 0;
                        // metal: characters and objects lit by real lights (armour, swords, shields: the shader
                        // picks the grey, light parts), and grey cut-outs (iron gates and fences)
                        if (op.shiny || (ate && tv.clear >= 0.15f && tv.clear <= 0.6f && tv.grey >= 0.6f && tv.green < 0.2f))
                            op.metal = g_metal;
                        // lit windows and candles: a few warm, bright pixels in an otherwise dark texture
                        if (!op.shiny && tv.warm >= 0.01f && tv.warm <= 0.35f && tv.hot < 0.45f)
                            op.glow = g_windows;
                        // water: a sliding, bluish texture
                        if (op.scrolls && tv.blue >= 0.4f)
                            op.reflect = 0.75f;
                    }
                }
            }
        // Glowing things light their surroundings: every additive draw (ALPHA = Cs*As + Cd) of the main camera is
        // a light at its centre, as bright and coloured as what it draws.
        for (const Op &op : out.ops)
        {
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != out.mainView || !op.abe || !op.count ||
                op.special) // (a reflection pass is a shine on a surface, not a light)
                continue;
            const uint32_t A = op.alphaReg & 3, B = (op.alphaReg >> 2) & 3, D = (op.alphaReg >> 6) & 3;
            if (!(A == 0 && B == 2 && D == 1))
                continue;
            double c[3] = {}, col[3] = {}, w = 0.0;
            float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
            for (uint32_t i = op.first; i < op.first + op.count; ++i)
            {
                const Vtx &v = out.verts[i];
                const double a = std::clamp(v.col[3], 0.0f, 255.0f) / 128.0;
                for (int k = 0; k < 3; ++k)
                {
                    c[k] += v.world[k];
                    col[k] += std::clamp(v.raw[k], 0.0f, 255.0f) / 255.0 * a;
                    lo[k] = std::min(lo[k], v.world[k]);
                    hi[k] = std::max(hi[k], v.world[k]);
                }
                w += a;
            }
            const double n = double(op.count);
            DrawList::PointLight L;
            float extent = 0.0f;
            for (int k = 0; k < 3; ++k)
            {
                L.pos[k] = float(c[k] / n);
                extent = std::max(extent, hi[k] - lo[k]);
            }
            const float total = float(col[0] + col[1] + col[2]);
            if (total <= 0.01f || extent > 200.0f) // nothing bright, or a huge layer (not a light source)
                continue;
            for (int k = 0; k < 3; ++k)
                L.col[k] = float(col[k] / n);
            L.weight = float(w / n) * std::min(1.0f, float(n) / 6.0f) * total;
            L.radius = std::clamp(extent * 2.5f, 15.0f, 45.0f);
            // merge with a light already close by (one torch drawn in several pieces)
            bool merged = false;
            for (auto &M : out.lights)
            {
                const float dx = M.pos[0] - L.pos[0], dy = M.pos[1] - L.pos[1], dz = M.pos[2] - L.pos[2];
                if (dx * dx + dy * dy + dz * dz < 36.0f)
                {
                    const float t = L.weight / (M.weight + L.weight);
                    for (int k = 0; k < 3; ++k)
                    {
                        M.pos[k] += (L.pos[k] - M.pos[k]) * t;
                        M.col[k] = std::max(M.col[k], L.col[k]);
                    }
                    M.radius = std::max(M.radius, L.radius);
                    M.weight += L.weight;
                    merged = true;
                    break;
                }
            }
            if (!merged)
                out.lights.push_back(L);
        }
        // Windows and candles light their surroundings: the triangles whose middle is a warm, bright pixel,
        // gathered into cells, one warm light per cell
        if (g_windows > 0.0f)
        {
            constexpr float kCell = 3.0f;
            struct WAcc
            {
                double p[3] = {}, n = 0.0;
            };
            std::unordered_map<uint64_t, WAcc> cells;
            for (const Op &op : out.ops)
            {
                if (op.glow <= 0.0f || op.viewId != out.mainView)
                    continue;
                const auto it = g_tracker.versions.find(op.version);
                if (it == g_tracker.versions.end() || it->second.rgba.empty())
                    continue;
                const TexVersion &tv = it->second;
                for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
                {
                    const Vtx &a = out.verts[i], &b = out.verts[i + 1], &c = out.verts[i + 2];
                    const float tu = (a.uv[0] + b.uv[0] + c.uv[0]) / 3.0f, tvv = (a.uv[1] + b.uv[1] + c.uv[1]) / 3.0f;
                    const int px = int(std::floor(tu * float(tv.width))) & int(tv.width - 1);
                    const int py = int(std::floor(tvv * float(tv.height))) & int(tv.height - 1);
                    const uint32_t texel = tv.rgba[size_t(py) * tv.width + px];
                    if (!Tracker::isWarm(texel & 0xFF, (texel >> 8) & 0xFF, (texel >> 16) & 0xFF))
                        continue;
                    float m[3];
                    for (int k = 0; k < 3; ++k)
                        m[k] = (a.world[k] + b.world[k] + c.world[k]) / 3.0f;
                    const uint64_t key = (uint64_t(int64_t(std::floor(m[0] / kCell)) & 0x1FFFFF) << 42) |
                                         (uint64_t(int64_t(std::floor(m[1] / kCell)) & 0x1FFFFF) << 21) |
                                         uint64_t(int64_t(std::floor(m[2] / kCell)) & 0x1FFFFF);
                    WAcc &w = cells[key];
                    for (int k = 0; k < 3; ++k)
                        w.p[k] += m[k];
                    w.n += 1.0;
                }
            }
            for (const auto &kv : cells)
            {
                DrawList::PointLight L;
                for (int k = 0; k < 3; ++k)
                    L.pos[k] = float(kv.second.p[k] / kv.second.n);
                L.col[0] = 1.0f * g_windows;
                L.col[1] = 0.68f * g_windows;
                L.col[2] = 0.32f * g_windows;
                L.radius = 16.0f;
                L.weight = 1.2f;
                L.flicker = 3.0f;
                out.lights.push_back(L);
            }
        }
        // Lava lights its surroundings: its triangles gathered into cells of a grid, one light per cell at the
        // middle of the lava in it, as bright as the cell is full of it.
        {
            constexpr float kCell = 12.0f; // (Maximo is about 7 tall)
            struct Acc
            {
                double p[3] = {}, area = 0.0, col[3] = {};
            };
            std::unordered_map<uint64_t, Acc> cells;
            for (const Op &op : out.ops)
            {
                if (!op.lava)
                    continue;
                const auto it = g_tracker.versions.find(op.version);
                if (it == g_tracker.versions.end())
                    continue;
                const TexVersion &tv = it->second;
                for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
                {
                    const Vtx &a = out.verts[i], &b = out.verts[i + 1], &c = out.verts[i + 2];
                    float e1[3], e2[3], m[3];
                    for (int k = 0; k < 3; ++k)
                    {
                        e1[k] = b.world[k] - a.world[k];
                        e2[k] = c.world[k] - a.world[k];
                        m[k] = (a.world[k] + b.world[k] + c.world[k]) / 3.0f;
                    }
                    const float cx = e1[1] * e2[2] - e1[2] * e2[1], cy = e1[2] * e2[0] - e1[0] * e2[2],
                                cz = e1[0] * e2[1] - e1[1] * e2[0];
                    const double area = 0.5 * std::sqrt(double(cx) * cx + double(cy) * cy + double(cz) * cz) * tv.hot;
                    if (!(area > 0.0) || area > 1e7)
                        continue;
                    const int64_t gx = int64_t(std::floor(m[0] / kCell)), gy = int64_t(std::floor(m[1] / kCell)),
                                  gz = int64_t(std::floor(m[2] / kCell));
                    const uint64_t key = (uint64_t(gx & 0x1FFFFF) << 42) | (uint64_t(gy & 0x1FFFFF) << 21) |
                                         uint64_t(gz & 0x1FFFFF);
                    Acc &acc = cells[key];
                    for (int k = 0; k < 3; ++k)
                    {
                        acc.p[k] += m[k] * area;
                        acc.col[k] += tv.hotCol[k] * area;
                    }
                    acc.area += area;
                }
            }
            for (const auto &kv : cells)
            {
                const Acc &acc = kv.second;
                if (acc.area < 6.0)
                    continue;
                DrawList::PointLight L;
                float mx = 0.0f;
                for (int k = 0; k < 3; ++k)
                {
                    L.pos[k] = float(acc.p[k] / acc.area);
                    L.col[k] = float(acc.col[k] / acc.area);
                    mx = std::max(mx, L.col[k]);
                }
                if (mx <= 0.01f)
                    continue;
                const float fill = std::min(1.0f, float(acc.area / (kCell * kCell * 0.5)));
                for (int k = 0; k < 3; ++k)
                    L.col[k] = L.col[k] / mx * g_lavaLight * (0.4f + 0.6f * fill);
                L.radius = 30.0f;
                L.weight = 1.5f * fill;
                L.flicker = 1.7f;
                out.lights.push_back(L);
            }
        }
        // The lights that matter most: bright ones near Maximo (a little ahead of the camera)
        float focus[3];
        for (int k = 0; k < 3; ++k)
            focus[k] = out.camPos[k] + out.camFwd[k] * 77.0f;
        auto priority = [&](const DrawList::PointLight &L) {
            const float dx = L.pos[0] - focus[0], dy = L.pos[1] - focus[1], dz = L.pos[2] - focus[2];
            return L.weight / (1.0f + (dx * dx + dy * dy + dz * dz) / (150.0f * 150.0f));
        };
        std::sort(out.lights.begin(), out.lights.end(),
                  [&](const DrawList::PointLight &a, const DrawList::PointLight &b) { return priority(a) > priority(b); });
        if (out.lights.size() > kMaxPointLights)
            out.lights.resize(kMaxPointLights);
        if (size_t(out.mainView) < out.viewScreens.size())
        {
            const auto t0 = std::chrono::steady_clock::now();
            {
                // RAIN: in a level where the game makes it rain itself (its rain seen in a good share of the last
                // two seconds), or always / never as set
                static uint8_t seen[120] = {};
                static int at = 0, sum = 0;
                const uint8_t now = out.rainParticles >= 60 ? 1 : 0;
                sum += int(now) - int(seen[at]);
                seen[at] = now;
                at = (at + 1) % 120;
                // (once it rains in a level it keeps raining: the game's rain comes in bursts, and it stopped and
                // started again; it stops only after fifteen seconds without any of the game's rain, or out of the
                // level)
                static bool wasRaining = false;
                static int dryFrames = 0, outFrames = 0;
                dryFrames = now ? 0 : dryFrames + 1;
                outFrames = out.inLevel ? 0 : outFrames + 1;
                bool gameRain = wasRaining ? (dryFrames < 900 && outFrames < 60) : sum >= 24;
                wasRaining = gameRain;
                out.raining = out.inLevel && g_rain > 0.0f && (g_rainMode == 2 || (g_rainMode == 1 && gameRain));
            }
            addVegetation(out);
            {
                // the mist's height follows the ground slowly (stepping onto a ledge or down stairs moved the whole
                // mist at once, right in front of Maximo); a big jump (another place) is taken at once
                static float mist = 0.0f;
                static bool have = false;
                if (out.haveGround)
                {
                    if (!have || std::fabs(out.groundY - mist) > 12.0f)
                        mist = out.groundY;
                    else
                        mist += (out.groundY - mist) * 0.02f;
                    have = true;
                }
                else if (have)
                {
                    // (no ground found this frame: keep the last one, the mist switched off and on otherwise)
                    out.haveGround = true;
                    out.groundY = mist;
                }
                out.mistY = have ? mist : out.groundY;
            }
            addWeather(out);
            findGameMoon(out);
            g_vegMs += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
        }
    }
}

// World -> shadow map: an orthographic box along the light, centred a little ahead of the camera (where Maximo is),
// its centre snapped to whole shadow-map texels so the shadows do not shimmer as the camera moves.
void buildLightMatrix(const DrawList &dl, float out[16])
{
    const float *f = dl.lightDir;
    const float upY[3] = {0, 1, 0}, upX[3] = {1, 0, 0};
    const float *up = std::fabs(f[1]) < 0.95f ? upY : upX;
    auto cross = [](const float *a, const float *b, float *o) {
        o[0] = a[1] * b[2] - a[2] * b[1];
        o[1] = a[2] * b[0] - a[0] * b[2];
        o[2] = a[0] * b[1] - a[1] * b[0];
    };
    auto dot = [](const float *a, const float *b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; };
    float r[3], u[3];
    cross(up, f, r);
    const float rl = std::sqrt(dot(r, r));
    for (float &x : r)
        x /= rl;
    cross(f, r, u);
    const float R = g_shadowRange, D = g_shadowRange * 2.0f;
    float c[3];
    for (int i = 0; i < 3; ++i)
        c[i] = dl.camPos[i] + dl.camFwd[i] * (g_shadowRange * 0.35f);
    const float texel = 2.0f * R / float(g_shadowSize);
    const float cr = std::floor(dot(c, r) / texel) * texel, cu = std::floor(dot(c, u) / texel) * texel,
                cf = dot(c, f);
    // rows: x = (p.r - cr) / R, y = (p.u - cu) / R, z = (p.f - cf + D) / 2D; stored column-major
    const float rows[4][4] = {{r[0] / R, r[1] / R, r[2] / R, -cr / R},
                              {u[0] / R, u[1] / R, u[2] / R, -cu / R},
                              {f[0] / (2 * D), f[1] / (2 * D), f[2] / (2 * D), (D - cf) / (2 * D)},
                              {0, 0, 0, 1}};
    for (int col = 0; col < 4; ++col)
        for (int row = 0; row < 4; ++row)
            out[col * 4 + row] = rows[row][col];
}

#ifndef NATIVE_NO_VULKAN
// ---------------------------------------------------------------------------------------------------------------
// Vulkan.
Vulkan::Buffer *g_frameVerts = nullptr; // this frame's vertices (render), while it is being drawn

struct Gpu
{
    bool failed = false;
    Vulkan::Program *program = nullptr;
    Vulkan::Program *shadowProgram = nullptr;
    Vulkan::Program *aoProgram = nullptr;
    Vulkan::Program *fogProgram = nullptr;
    Vulkan::Program *postProgram = nullptr;
    Vulkan::ImageHandle postSrc[3]; // a copy of the picture for the finishing pass to read
    Vulkan::Program *bloomProgram = nullptr;
    Vulkan::ImageHandle bloomA[3], bloomB[3];
    Vulkan::ImageHandle shadowMaps[3];
    Vulkan::SamplerHandle samplerWrap, samplerClamp; // trilinear + anisotropic, for the game's textures
    Vulkan::ImageHandle outputs[3];                  // the scaled-down picture handed to the presenter
    Vulkan::ImageHandle white;
    // Images kept between frames (a ring of 3, so the readback of a frame is long done when its image is reused).
    struct Slot
    {
        Vulkan::ImageHandle color, depth;
    };
    Slot main[3];
    std::vector<Slot> offscreen[3];
    uint32_t ring = 0;
    Vulkan::ImageHandle last; // the last native picture (shown again for a frame with no match)
    uint64_t lastFrame = 0;
};
Gpu g_gpu;

bool setupGpu(Vulkan::Device &device)
{
    if (g_gpu.program || g_gpu.failed)
        return g_gpu.program != nullptr;
    Vulkan::ResourceLayout vsLayout;
    vsLayout.input_mask = 0x7Fu;
    vsLayout.push_constant_size = 112;
    Vulkan::ResourceLayout fsLayout;
    fsLayout.output_mask = 1u;
    fsLayout.push_constant_size = 112;
    fsLayout.sets[0].sampled_image_mask = 3u;
    fsLayout.sets[0].fp_mask = 3u;
    fsLayout.sets[0].meta[0].array_size = 1;
    fsLayout.sets[0].meta[1].array_size = 1;
    fsLayout.sets[0].uniform_buffer_mask = 1u << 2;
    fsLayout.sets[0].meta[2].array_size = 1;
    Vulkan::Shader *vs = device.request_shader(native_vert_spv, sizeof(native_vert_spv), &vsLayout);
    Vulkan::Shader *fs = device.request_shader(native_frag_spv, sizeof(native_frag_spv), &fsLayout);
    if (vs && fs)
        g_gpu.program = device.request_program(vs, fs);
    // real-time shadows: depth from the light, cut-outs discarded
    Vulkan::ResourceLayout svsLayout;
    svsLayout.input_mask = (1u << 1) | (1u << 3);
    svsLayout.push_constant_size = 112;
    Vulkan::ResourceLayout sfsLayout;
    sfsLayout.output_mask = 0;
    sfsLayout.push_constant_size = 112;
    sfsLayout.sets[0].sampled_image_mask = 1u;
    sfsLayout.sets[0].fp_mask = 1u;
    sfsLayout.sets[0].meta[0].array_size = 1;
    Vulkan::Shader *svs = device.request_shader(native_shadow_vert_spv, sizeof(native_shadow_vert_spv), &svsLayout);
    Vulkan::Shader *sfs = device.request_shader(native_shadow_frag_spv, sizeof(native_shadow_frag_spv), &sfsLayout);
    if (svs && sfs)
        g_gpu.shadowProgram = device.request_program(svs, sfs);
    if (!g_gpu.shadowProgram)
        std::fprintf(stderr, "[native] shadow setup failed; drawing without real-time shadows\n");
    {
        Vulkan::ResourceLayout avs;
        avs.input_mask = 1u;
        avs.push_constant_size = 32;
        Vulkan::ResourceLayout afs;
        afs.output_mask = 1u;
        afs.push_constant_size = 32;
        afs.sets[0].sampled_image_mask = 1u;
        afs.sets[0].fp_mask = 1u;
        afs.sets[0].meta[0].array_size = 1;
        Vulkan::Shader *v = device.request_shader(native_ao_vert_spv, sizeof(native_ao_vert_spv), &avs);
        Vulkan::Shader *f = device.request_shader(native_ao_frag_spv, sizeof(native_ao_frag_spv), &afs);
        if (v && f)
            g_gpu.aoProgram = device.request_program(v, f);
        if (!g_gpu.aoProgram)
            std::fprintf(stderr, "[native] ambient occlusion setup failed; drawing without it\n");
        {
            Vulkan::ResourceLayout ffs = afs;
            ffs.push_constant_size = 112;
            Vulkan::Shader *ff = device.request_shader(native_fog_frag_spv, sizeof(native_fog_frag_spv), &ffs);
            if (v && ff)
                g_gpu.fogProgram = device.request_program(v, ff);
            if (!g_gpu.fogProgram)
                std::fprintf(stderr, "[native] ground fog setup failed; drawing without it\n");
        }
        {
            Vulkan::ResourceLayout pfs = afs;
            pfs.push_constant_size = 128;
            pfs.sets[0].sampled_image_mask = 3u;
            pfs.sets[0].fp_mask = 3u;
            pfs.sets[0].meta[1].array_size = 1;
            Vulkan::Shader *pf = device.request_shader(native_post_frag_spv, sizeof(native_post_frag_spv), &pfs);
            if (v && pf)
                g_gpu.postProgram = device.request_program(v, pf);
            if (!g_gpu.postProgram)
                std::fprintf(stderr, "[native] sky / colour grading setup failed; drawing without them\n");
        }
        Vulkan::Shader *bf = device.request_shader(native_bloom_frag_spv, sizeof(native_bloom_frag_spv), &afs);
        if (v && bf)
            g_gpu.bloomProgram = device.request_program(v, bf);
        if (!g_gpu.bloomProgram)
            std::fprintf(stderr, "[native] bloom setup failed; drawing without it\n");
    }
    // IMAGE QUALITY: mipmapped, anisotropic sampling of the game's textures (the PS2 had no mipmaps here, so
    // distant grass and ground shimmered)
    {
        Vulkan::SamplerCreateInfo si = {};
        si.mag_filter = VK_FILTER_LINEAR;
        si.min_filter = VK_FILTER_LINEAR;
        si.mipmap_mode = g_mipmaps ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST;
        si.address_mode_u = si.address_mode_v = si.address_mode_w = VK_SAMPLER_ADDRESS_MODE_REPEAT;
        si.anisotropy_enable = device.get_device_features().enabled_features.samplerAnisotropy && g_anisotropy > 1.0f;
        si.max_anisotropy = g_anisotropy;
        si.min_lod = 0.0f;
        si.max_lod = g_mipmaps ? VK_LOD_CLAMP_NONE : 0.0f;
        si.compare_op = VK_COMPARE_OP_NEVER;
        si.border_color = VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK;
        g_gpu.samplerWrap = device.create_sampler(si);
        si.address_mode_u = si.address_mode_v = si.address_mode_w = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
        g_gpu.samplerClamp = device.create_sampler(si);
    }
    const uint32_t whitePixel = 0x80FFFFFFu;
    auto info = Vulkan::ImageCreateInfo::immutable_2d_image(1, 1, VK_FORMAT_R8G8B8A8_UNORM);
    const Vulkan::ImageInitialData init = {&whitePixel, 1, 1};
    g_gpu.white = device.create_image(info, &init);
    if (!g_gpu.program || !g_gpu.white)
    {
        std::fprintf(stderr, "[native] renderer setup failed; showing paraLLEl-GS\n");
        g_gpu.failed = true;
        return false;
    }
    std::fprintf(stderr, "[native] renderer ready (F9 switches between it and paraLLEl-GS)\n");
    return true;
}

const Vulkan::ImageView *versionView(Vulkan::Device &device, uint32_t id)
{
    auto it = g_tracker.versions.find(id);
    if (it == g_tracker.versions.end())
        return nullptr;
    TexVersion &v = it->second;
    v.lastUsed = g_tracker.frameNo;
    if (!v.image)
    {
        auto info = Vulkan::ImageCreateInfo::immutable_2d_image(v.width, v.height, VK_FORMAT_R8G8B8A8_UNORM,
                                                                g_mipmaps && (v.width > 1 || v.height > 1));
        const Vulkan::ImageInitialData init = {v.rgba.data(), v.width, v.height};
        v.image = device.create_image(info, &init);
        if (!v.image)
            return nullptr;
    }
    return &v.image->get_view();
}

void setBlend(Vulkan::CommandBuffer &cmd, const Op &op)
{
    if (!op.abe)
    {
        cmd.set_blend_enable(false);
        cmd.set_color_write_mask(0xF);
        return;
    }
    cmd.set_blend_enable(true);
    cmd.set_blend_op(VK_BLEND_OP_ADD);
    const uint32_t A = op.alphaReg & 3, B = (op.alphaReg >> 2) & 3, C = (op.alphaReg >> 4) & 3, D = (op.alphaReg >> 6) & 3;
    const VkBlendFactor alphaF = C == 2 ? VK_BLEND_FACTOR_CONSTANT_ALPHA : VK_BLEND_FACTOR_SRC_ALPHA;
    const VkBlendFactor invAlphaF = C == 2 ? VK_BLEND_FACTOR_ONE_MINUS_CONSTANT_ALPHA : VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    VkBlendFactor src = alphaF, dst = invAlphaF;
    if (A == 0 && B == 1 && D == 1)
    {
        src = alphaF;
        dst = invAlphaF; // Cs*a + Cd*(1-a)
    }
    else if (A == 2 && B == 1 && D == 1)
    {
        src = VK_BLEND_FACTOR_ZERO;
        dst = invAlphaF; // Cd - Cd*a
    }
    else if (A == 0 && B == 2 && D == 1)
    {
        src = alphaF;
        dst = VK_BLEND_FACTOR_ONE; // Cs*a + Cd
    }
    else if (A == 0 && B == 2 && D == 2)
    {
        src = alphaF;
        dst = VK_BLEND_FACTOR_ZERO; // Cs*a
    }
    else if (A == 1 && B == 0 && D == 0)
    {
        src = invAlphaF;
        dst = alphaF; // Cd*a + Cs*(1-a)
    }
    else if (A == 1 && B == 2 && D == 0)
    {
        src = VK_BLEND_FACTOR_ONE;
        dst = alphaF; // Cd*a + Cs
    }
    if (C == 2)
    {
        const float k = float(op.fix) / 128.0f;
        const float constants[4] = {k, k, k, k};
        cmd.set_blend_constants(constants);
    }
    // (water writes its reflectivity to the picture's alpha; everything else blended leaves the alpha as it was)
    if (op.reflect > 0.0f && op.target == 0)
        cmd.set_blend_factors(src, VK_BLEND_FACTOR_ONE, dst, VK_BLEND_FACTOR_ZERO);
    else
        cmd.set_blend_factors(src, VK_BLEND_FACTOR_ZERO, dst, VK_BLEND_FACTOR_ONE);
    cmd.set_color_write_mask(0xF);
}

struct ShadowInfo
{
    const Vulkan::ImageView *map = nullptr; // nullptr: no shadows this frame
    float light[16] = {};                  // world -> shadow map, column-major
    float dir[3] = {0, -1, 0};             // the direction the light travels
};

void drawOps(Vulkan::CommandBuffer &cmd, Vulkan::Device &device, const DrawList &dl, int target,
             const std::vector<Vulkan::ImageHandle> &rts, uint32_t width, uint32_t height, const ShadowInfo &sh,
             size_t from = 0, size_t to = SIZE_MAX)
{
    cmd.set_program(g_gpu.program);
    cmd.set_vertex_attrib(0, 0, VK_FORMAT_R32G32B32A32_SFLOAT, offsetof(Vtx, pos));
    cmd.set_vertex_attrib(1, 0, VK_FORMAT_R32G32_SFLOAT, offsetof(Vtx, uv));
    cmd.set_vertex_attrib(2, 0, VK_FORMAT_R32G32B32A32_SFLOAT, offsetof(Vtx, col));
    cmd.set_vertex_attrib(3, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vtx, world));
    cmd.set_vertex_attrib(4, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vtx, normal));
    cmd.set_vertex_attrib(5, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vtx, raw));
    cmd.set_vertex_attrib(6, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vtx, mnormal));
    const bool perPixel = g_lighting.load(std::memory_order_relaxed);
    cmd.set_texture(0, 1, sh.map ? *sh.map : g_gpu.white->get_view(), Vulkan::StockSampler::NearestClamp);
    cmd.set_primitive_topology(VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST);
    cmd.set_cull_mode(VK_CULL_MODE_NONE);
    to = std::min(to, dl.ops.size());
    for (size_t oi = from; oi < to; ++oi)
    {
        const Op &op = dl.ops[oi];
        if (op.target != target)
            continue;
        if (op.type == OpType::ClearDepth)
        {
            VkClearRect rect = {};
            rect.rect.extent = {width, height};
            rect.layerCount = 1;
            VkClearValue v = {};
            v.depthStencil.depth = 0.0f;
            cmd.clear_quad(0, rect, v, VK_IMAGE_ASPECT_DEPTH_BIT);
            continue;
        }
        // depth: the GS keeps GEQUAL on its Z, and so does the reversed depth here (1/w, larger is nearer)
        const bool zte = (op.test >> 16) & 1;
        const uint32_t ztst = uint32_t(op.test >> 17) & 3;
        if (!zte || ztst == 1)
        {
            cmd.set_depth_test(true, op.zwrite && zte);
            cmd.set_depth_compare(VK_COMPARE_OP_ALWAYS);
        }
        else if (ztst == 0)
            continue; // NEVER
        else
        {
            cmd.set_depth_test(true, op.zwrite);
            cmd.set_depth_compare(ztst == 3 ? VK_COMPARE_OP_GREATER : VK_COMPARE_OP_GREATER_OR_EQUAL);
        }
        setBlend(cmd, op);
        // Overlays drawn on top of other surfaces without writing depth (blob and tree shadows, decals, glows) sit
        // exactly on, or a hair above, what they cover; the GS's integer Z let them pass, a float depth makes them
        // fight (the flicker). Pull them slightly towards the camera.
        if (!op.zwrite || op.abe)
        {
            cmd.set_depth_bias(true);
            cmd.set_depth_bias(1.0f, 2.0f);
        }
        else
            cmd.set_depth_bias(false);
        const Vulkan::ImageView *view = &g_gpu.white->get_view();
        float alphaScale = 255.0f;
        if (op.textured)
        {
            if (op.rtTexture >= 0 && size_t(op.rtTexture) < rts.size() && rts[op.rtTexture])
            {
                view = &rts[op.rtTexture]->get_view();
                alphaScale = 128.0f;
            }
            else if (const Vulkan::ImageView *v = versionView(device, op.version))
                view = v;
        }
        if (alphaScale == 255.0f && view != &g_gpu.white->get_view() && g_gpu.samplerWrap && g_gpu.samplerClamp)
            cmd.set_texture(0, 0, *view, op.clampUV ? *g_gpu.samplerClamp : *g_gpu.samplerWrap);
        else
            cmd.set_texture(0, 0, *view,
                            op.clampUV ? Vulkan::StockSampler::LinearClamp : Vulkan::StockSampler::LinearWrap);
        const uint32_t ate = uint32_t(op.test) & 1, atst = uint32_t(op.test >> 1) & 7, aref = uint32_t(op.test >> 4) & 0xFF;
        float push[28] = {op.textured ? 1.0f : 0.0f, float(atst), float(aref), float(ate), alphaScale,
                          (sh.map && op.receives) ? 1.0f : 0.0f, g_shadowStrength, 1.0f / float(g_shadowSize),
                          sh.dir[0], sh.dir[1], sh.dir[2], op.shell};
        std::memcpy(push + 12, sh.light, sizeof(sh.light));
        cmd.push_constants(push, 0, sizeof(push));
        // LIGHTING (F11): the game's own lights per pixel instead of per vertex, plus highlights from the sun on
        // what real lights shine on (characters, objects)
        {
            struct Lights
            {
                float LD[16], LC[16];
                float flags[4]; // per pixel, highlight strength, sun on, sun ambient
                float cam[4];
                float sun[4];
                float point[4];       // x: how many point lights, y: their strength
                float pointPos[kMaxPointLights][4]; // xyz, radius
                float pointCol[kMaxPointLights][4]; // rgb, how fast it flickers
                float extra[4];                      // relief, moss
                float extra2[4];                     // grime, ground height, carved depth, ground known
                float extra3[4];                     // metal, glow, reflectivity, write reflectivity to alpha
                float moon[4];                       // towards the moon
                float weather[4];                    // wetness, puddles
                float look[4];                       // what the draw is (HUD piece, its shadow, orb, gold, characters)
            };
            auto *u = static_cast<Lights *>(cmd.allocate_constant_data(0, 2, sizeof(Lights)));
            std::memcpy(u->LD, op.LD, 64);
            std::memcpy(u->LC, op.LC, 64);
            u->flags[0] = (perPixel && op.lit) ? 1.0f : 0.0f;
            u->flags[1] = (perPixel && op.lit && op.shiny && op.viewId == dl.mainView) ? g_specular : 0.0f;
            // SUN: the whole main picture (level included) shaded by the light the shadows come from
            const bool sun = perPixel && dl.haveLight && op.viewId == dl.mainView && op.target == 0 && !op.emissive;
            u->flags[2] = sun ? 1.0f : 0.0f;
            u->flags[3] = g_sunAmbient;
            u->cam[0] = dl.camPos[0];
            u->cam[1] = dl.camPos[1];
            u->cam[2] = dl.camPos[2];
            u->cam[3] = g_sunStrength;
            u->sun[0] = dl.lightDir[0];
            u->sun[1] = dl.lightDir[1];
            u->sun[2] = dl.lightDir[2];
            u->sun[3] = 0.0f;
            const bool points = perPixel && op.receives && g_pointLights > 0.0f && !dl.lights.empty();
            u->point[0] = points ? float(dl.lights.size()) : 0.0f;
            u->point[1] = g_pointLights;
            u->point[2] = (perPixel && op.lava) ? g_lavaGlow : 1.0f;
            const bool still = !op.moves && !op.skinned; // (world-space patterns only on what stays put)
            u->extra[0] = (perPixel && op.textured && op.viewId == dl.mainView && still) ? op.relief : 0.0f;
            u->extra[1] = (perPixel && op.viewId == dl.mainView && dl.haveLight && still) ? op.moss : 0.0f;
            u->extra[2] = (perPixel && op.textured && op.viewId == dl.mainView && op.relief > 0.0f && !op.generated && still)
                              ? g_cracks
                              : 0.0f;
            u->extra[3] = 0.0f;
            const bool solid = perPixel && op.textured && op.viewId == dl.mainView && op.relief > 0.0f && still;
            u->extra2[0] = solid ? g_grime : 0.0f;
            u->extra2[1] = dl.groundY;
            u->extra2[2] = solid ? g_parallax : 0.0f;
            u->extra2[3] = dl.haveGround ? 1.0f : 0.0f;
            const bool mainPic = perPixel && op.target == 0;
            u->extra3[0] = mainPic ? op.metal : 0.0f;
            u->extra3[1] = mainPic ? op.glow : 0.0f;
            u->extra3[2] = mainPic ? op.reflect : 0.0f;
            // (only solid draws: a see-through draw's alpha is how much of it shows, and writing the reflectivity
            // there made fire, glows, beams, rain and cobwebs vanish)
            u->extra3[3] = (mainPic && g_reflect > 0.0f && !op.abe) ? 1.0f : 0.0f;
            moonDirection(dl, u->moon);
            u->moon[3] = 0.0f;
            u->weather[0] = (perPixel && dl.raining) ? std::min(1.0f, 0.6f + 0.4f * g_rain) * (1.0f - op.dry)
                                                                       : 0.0f;
            u->weather[1] = g_puddles * (1.0f - op.dry);
            u->weather[2] = g_flicker;
            // (the moonlit rim on characters: drawn with bones, in the main picture)
            u->weather[3] = (perPixel && op.skinned && !op.generated && !op.emissive && op.viewId == dl.mainView &&
                             op.target == 0 && dl.inLevel)
                                ? g_rim
                                : 0.0f;
            std::memcpy(u->look, op.look, sizeof(u->look));
            if (!perPixel && op.look[0] >= 5.0f)
                u->look[0] = 0.0f;
            u->point[3] = float(g_tracker.frameNo % 36000u) / 60.0f; // seconds, for the flicker
            for (size_t k = 0; k < kMaxPointLights; ++k)
            {
                if (k < dl.lights.size())
                {
                    const auto &L = dl.lights[k];
                    u->pointPos[k][0] = L.pos[0];
                    u->pointPos[k][1] = L.pos[1];
                    u->pointPos[k][2] = L.pos[2];
                    u->pointPos[k][3] = L.radius;
                    u->pointCol[k][0] = L.col[0];
                    u->pointCol[k][1] = L.col[1];
                    u->pointCol[k][2] = L.col[2];
                    u->pointCol[k][3] = L.flicker;
                }
                else
                {
                    std::memset(u->pointPos[k], 0, 16);
                    std::memset(u->pointCol[k], 0, 16);
                }
            }
        }
        if (g_frameVerts)
            cmd.set_vertex_binding(0, *g_frameVerts, VkDeviceSize(op.first) * sizeof(Vtx), sizeof(Vtx));
        else
        {
            void *dst = cmd.allocate_vertex_data(0, VkDeviceSize(op.count) * sizeof(Vtx), sizeof(Vtx));
            if (!dst)
                continue;
            std::memcpy(dst, dl.verts.data() + op.first, size_t(op.count) * sizeof(Vtx));
        }
        cmd.draw(op.count, 1, 0, 0);
    }
}

#endif

struct Stats
{
    uint64_t frames = 0, rendered = 0, noFrame = 0, missing = 0;
    double drawMs = 0.0;
    std::chrono::steady_clock::time_point last = std::chrono::steady_clock::now();
} g_stats;
} // namespace

// ---------------------------------------------------------------------------------------------------------------
bool enabled() { return g_enabled.load(std::memory_order_relaxed); }
bool shadows() { return g_shadows.load(std::memory_order_relaxed); }
bool lighting() { return g_lighting.load(std::memory_order_relaxed); }
void setLighting(bool on)
{
    g_lighting.store(on, std::memory_order_relaxed);
    std::fprintf(stderr, "[native] per-pixel lighting %s\n", on ? "ON" : "OFF");
}
void setShadows(bool on)
{
    g_shadows.store(on, std::memory_order_relaxed);
    std::fprintf(stderr, "[native] real-time shadows %s\n", on ? "ON" : "OFF");
}
void setEnabled(bool on)
{
    g_enabled.store(on, std::memory_order_relaxed);
    std::fprintf(stderr, "[native] native renderer %s\n", on ? "ON" : "OFF (paraLLEl-GS)");
}

void submitFrame(std::unique_ptr<Frame> frame)
{
    std::lock_guard<std::mutex> lock(g_frameMutex);
    g_frames.push_back(std::move(frame));
    while (g_frames.size() > 6)
        g_frames.pop_front();
}

void shutdown()
{
#ifndef NATIVE_NO_VULKAN
    for (auto &kv : g_tracker.versions)
        kv.second.image.reset();
    for (auto &slot : g_gpu.main)
        slot = {};
    for (auto &v : g_gpu.offscreen)
        v.clear();
    g_gpu.last.reset();
    g_gpu.white.reset();
    for (auto &m : g_gpu.shadowMaps)
        m.reset();
    for (auto &o : g_gpu.outputs)
        o.reset();
    g_gpu.samplerWrap.reset();
    g_gpu.samplerClamp.reset();
    g_gpu.program = nullptr;
    g_gpu.shadowProgram = nullptr;
    g_gpu.aoProgram = nullptr;
    g_gpu.fogProgram = nullptr;
    g_gpu.postProgram = nullptr;
    g_gpu.bloomProgram = nullptr;
    for (auto &b : g_gpu.bloomA)
        b.reset();
    for (auto &b : g_gpu.bloomB)
        b.reset();
#endif
    g_tracker.reset();
}

bool needsSeed() { return enabled() && !g_tracker.seeded; }
void seed(const uint8_t *vram)
{
    buildTables();
    g_tracker.reset();
    g_tracker.vram.assign(vram, vram + kVramSize);
    g_tracker.seeded = true;
    std::fprintf(stderr, "[native] following the GS stream (GS memory copied)\n");
}
bool tracking() { return g_tracker.seeded; }
const uint8_t *mirror()
{
    return g_tracker.seeded && g_tracker.vram.size() >= kVramSize ? g_tracker.vram.data() : nullptr;
}
void setGsSkipped(bool skipped) { g_gsSkipped.store(skipped, std::memory_order_relaxed); }
void gifPacket(uint32_t path, const uint8_t *data, uint32_t size)
{
    if (!g_tracker.seeded)
        return;
    const auto t0 = std::chrono::steady_clock::now();
    g_tracker.packet(path, data, size);
    g_tracker.statStreamMs += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}
void endGsFrame(uint64_t guestFrame)
{
    if (!g_tracker.seeded)
        return;
    if (!enabled())
    {
        g_tracker.reset();
        g_identityVersion.clear();
        g_identityVotes.clear();
        g_identityShown.clear();
        g_grassCache.clear();
        std::lock_guard<std::mutex> lock(g_frameMutex);
        g_frames.clear();
        return;
    }
    GsRecord r;
    r.guestFrame = guestFrame;
    r.binds.swap(g_tracker.binds);
    r.sortedKeys.reserve(r.binds.size());
    for (const Bind &b : r.binds)
        r.sortedKeys.push_back(b.key);
    std::sort(r.sortedKeys.begin(), r.sortedKeys.end());
    r.sortedKeys.erase(std::unique(r.sortedKeys.begin(), r.sortedKeys.end()), r.sortedKeys.end());
    g_tracker.records.push_back(std::move(r));
    while (g_tracker.records.size() > 3)
        g_tracker.records.pop_front();
    ++g_tracker.frameNo;
    // forget texture versions not used for 10 seconds
    if ((g_tracker.frameNo & 255) == 0)
    {
        for (auto it = g_tracker.versions.begin(); it != g_tracker.versions.end();)
        {
            if (g_tracker.frameNo - it->second.lastUsed > 600 && g_tracker.frameNo > 600)
            {
                g_tracker.byHash.erase(it->second.hash);
                for (auto &k : g_tracker.keys)
                    if (k.second.version == it->first)
                        k.second.version = 0;
                it = g_tracker.versions.erase(it);
            }
            else
                ++it;
        }
    }
}

#ifndef NATIVE_NO_VULKAN
// Draws the newest native frame that matches a GS frame's textures; nullptr = show paraLLEl-GS this frame.
// SKY, LIGHTNING AND COLOUR GRADING: the picture so far (before the HUD) copied aside, then drawn back through the
// finishing shader, which also reads the depth (the sky is where nothing was drawn in front of it).
void finishPicture(Vulkan::CommandBuffer &cmd, Vulkan::Device &device, const DrawList &dl, const Vulkan::Image &color,
                   const Vulkan::Image &depth, uint32_t bigW, uint32_t bigH)
{
    Vulkan::ImageHandle &src = g_gpu.postSrc[g_gpu.ring];
    if (!src || src->get_width() != bigW || src->get_height() != bigH)
    {
        auto ci = Vulkan::ImageCreateInfo::immutable_2d_image(bigW, bigH, VK_FORMAT_R8G8B8A8_UNORM);
        ci.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
        ci.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
        src = device.create_image(ci);
        if (!src)
            return;
    }
    // LIGHTNING: every 10 to 30 seconds (less with the setting lower), a flicker of two or three flashes
    static uint64_t nextFlash = 0, flashStart = 0;
    static uint32_t flashSeed = 12345u;
    const uint64_t now = g_tracker.frameNo;
    float flash = 0.0f;
    if (g_lightning > 0.0f)
    {
        auto rnd = [&]() {
            flashSeed = flashSeed * 1664525u + 1013904223u;
            return float((flashSeed >> 8) & 0xFFFF) / 65535.0f;
        };
        if (nextFlash == 0 || nextFlash > now + 60u * 120u)
            nextFlash = now + uint64_t(60.0f * (5.0f + 10.0f * rnd()));
        if (now >= nextFlash)
        {
            flashStart = now;
            nextFlash = now + uint64_t(60.0f * (10.0f + 20.0f * rnd()) / g_lightning);
        }
        const float ts = float(now - flashStart) / 60.0f;
        if (flashStart && ts < 1.2f)
        {
            const float at[3] = {0.0f, 0.13f, 0.38f}, amp[3] = {0.55f, 0.3f, 0.75f};
            for (int i = 0; i < 3; ++i)
                if (ts >= at[i])
                    flash = std::max(flash, amp[i] * std::exp(-(ts - at[i]) * 11.0f));
        }
    }
    // the moon: towards where the light comes from, low enough to be seen over the scenery
    float moon[3];
    moonDirection(dl, moon);
    const Mat &V = dl.views[dl.mainView];
    const Mat &scr = dl.viewScreens[dl.mainView];
    float moonUV[2] = {0.5f, 0.5f};
    bool moonFront = false;
    {
        const Mat vs = mul(V, scr);
        const float p4[4] = {dl.camPos[0] + moon[0] * 1000.0f, dl.camPos[1] + moon[1] * 1000.0f,
                             dl.camPos[2] + moon[2] * 1000.0f, 1.0f};
        float sp[4];
        xform(p4, vs, sp);
        if (dl.gameMoon)
        {
            moonUV[0] = dl.gameMoonUV[0];
            moonUV[1] = dl.gameMoonUV[1];
            moonFront = true;
        }
        else if (sp[3] > 1.0f)
        {
            moonUV[0] = ((sp[0] - 2048.0f * sp[3]) / 320.0f / sp[3]) * 0.5f + 0.5f;
            moonUV[1] = ((sp[1] - 2048.0f * sp[3]) / 112.0f / sp[3]) * 0.5f + 0.5f;
            moonFront = true;
        }
    }
    cmd.image_barrier(color, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                      VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                      VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
    cmd.image_barrier(*src, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                      VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0, VK_PIPELINE_STAGE_2_COPY_BIT,
                      VK_ACCESS_2_TRANSFER_WRITE_BIT);
    cmd.copy_image(*src, color);
    cmd.image_barrier(*src, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                      VK_PIPELINE_STAGE_2_COPY_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT,
                      VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
    cmd.image_barrier(color, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                      VK_PIPELINE_STAGE_2_COPY_BIT, 0, VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT,
                      VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_2_COLOR_ATTACHMENT_READ_BIT);
    cmd.image_barrier(depth, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                      VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT, VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT,
                      VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
    Vulkan::RenderPassInfo rp;
    rp.num_color_attachments = 1;
    rp.color_attachments[0] = &color.get_view();
    rp.store_attachments = 1u; // (every pixel is written: nothing to load)
    cmd.begin_render_pass(rp);
    VkViewport vp = {0.0f, 0.0f, float(bigW), float(bigH), 0.0f, 1.0f};
    cmd.set_viewport(vp);
    cmd.set_scissor({{0, 0}, {bigW, bigH}});
    cmd.set_program(g_gpu.postProgram);
    cmd.set_depth_test(false, false);
    cmd.set_depth_bias(false);
    cmd.set_cull_mode(VK_CULL_MODE_NONE);
    cmd.set_blend_enable(false);
    cmd.set_color_write_mask(0xF);
    cmd.set_texture(0, 0, src->get_view(), Vulkan::StockSampler::NearestClamp);
    cmd.set_texture(0, 1, depth.get_view(), Vulkan::StockSampler::NearestClamp);
    const float push[32] = {scr.m[0], scr.m[5], 320.0f, 112.0f,
                            dl.camPos[0], dl.camPos[1], dl.camPos[2], float(now % 360000u) / 60.0f,
                            V.m[0], V.m[4], V.m[8], moon[0],
                            V.m[1], V.m[5], V.m[9], moon[1],
                            V.m[2], V.m[6], V.m[10], moon[2],
                            moonUV[0], moonUV[1], (moonFront ? 1.0f : -1.0f) * (1.0f + g_shafts), flash,
                            g_grade, g_vignette, g_sky, g_sky,
                            g_reflect, g_dof, dl.gameMoon ? 1.0f : 0.0f, dl.inLevel ? g_haze : 0.0f};
    cmd.push_constants(push, 0, sizeof(push));
    auto *tri = static_cast<float *>(cmd.allocate_vertex_data(0, 6 * sizeof(float), 2 * sizeof(float)));
    const float verts[6] = {-1.0f, -1.0f, 3.0f, -1.0f, -1.0f, 3.0f};
    std::memcpy(tri, verts, sizeof(verts));
    cmd.set_vertex_attrib(0, 0, VK_FORMAT_R32G32_SFLOAT, 0);
    cmd.set_primitive_topology(VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST);
    cmd.draw(3);
    cmd.end_render_pass();
    cmd.image_barrier(depth, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                      VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                      VK_PIPELINE_STAGE_2_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT,
                      VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT | VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_READ_BIT);
}

Vulkan::ImageHandle render(Vulkan::Device &device, uint32_t width, uint32_t height, uint64_t guestFrame)
{
    if (!enabled() || !g_tracker.seeded || g_tracker.records.empty() || !setupGpu(device))
        return {};
    ++g_stats.frames;
    // Pair a native frame with a GS frame: the native frame's texture keys should all be among the GS frame's.
    std::unique_ptr<Frame> frame;
    size_t recordIndex = 0;
    {
        std::lock_guard<std::mutex> lock(g_frameMutex);
        int bestFrame = -1, bestRecord = -1;
        double bestScore = -1.0;
        for (int fi = int(g_frames.size()) - 1; fi >= 0 && fi >= int(g_frames.size()) - 3; --fi)
        {
            const Frame &f = *g_frames[fi];
            std::vector<uint64_t> keys;
            for (const Cmd &c : f.cmds)
                if ((c.cmd == 11 || c.cmd == 12) && c.blobSize >= 0x38)
                {
                    uint64_t tex0;
                    std::memcpy(&tex0, f.data.data() + c.blobOff + 0x30, 8);
                    keys.push_back(Tracker::keyOf(tex0));
                }
            for (int ri = int(g_tracker.records.size()) - 1; ri >= 0; --ri)
            {
                const GsRecord &r = g_tracker.records[ri];
                size_t hit = 0;
                for (uint64_t k : keys)
                    hit += std::binary_search(r.sortedKeys.begin(), r.sortedKeys.end(), k) ? 1 : 0;
                const double score = keys.empty() ? 0.0 : double(hit) / double(keys.size());
                // prefer the newest pairing at equal score
                if (score > bestScore + 1e-9)
                {
                    bestScore = score;
                    bestFrame = fi;
                    bestRecord = ri;
                }
            }
        }
        if (bestFrame < 0 || bestScore < 0.5)
        {
            ++g_stats.noFrame;
            // (while paraLLEl-GS is not drawing, its picture is stale: the last native one is shown whatever its age)
            if (g_gpu.last && g_gpu.last->get_width() == width && g_gpu.last->get_height() == height &&
                (g_tracker.frameNo - g_gpu.lastFrame < 4 || g_gsSkipped.load(std::memory_order_relaxed)))
                return g_gpu.last;
            return {};
        }
        frame = std::move(g_frames[bestFrame]);
        g_frames.erase(g_frames.begin(), g_frames.begin() + bestFrame + 1);
        recordIndex = size_t(bestRecord);
    }
    const auto tBuild = std::chrono::steady_clock::now();
    Resolver res(g_tracker.records, recordIndex, g_tracker.keys);
    DrawList dl;
    // the vertex list's memory is kept from frame to frame (with the grass it holds a few hundred thousand
    // vertices: allocating and growing it anew every frame cost more than filling it)
    dl.verts.swap(g_vertPool);
    dl.verts.clear();
    dl.verts.reserve(65536);
    struct KeepVerts
    {
        DrawList &d;
        ~KeepVerts() { g_vertPool.swap(d.verts); }
    } keepVerts{dl};
    buildDrawList(*frame, res, dl);
    g_stats.missing += dl.missingTextures;
    if (dl.verts.empty() || !g_state.haveScreen)
    {
        ++g_stats.noFrame;
        return {};
    }
    // All the frame's vertices in one buffer in system memory, each draw reading its part of it. (Each draw's own
    // vertex memory from the command buffer came out of 4 KB blocks: every bigger draw, the grass above all, got a
    // buffer of its own in the small memory both the CPU and GPU see, which ran out every frame: thousands of
    // warnings, clean-ups and fallbacks per second.)
    Vulkan::BufferHandle frameVerts;
    {
        Vulkan::BufferCreateInfo bi = {};
        bi.domain = Vulkan::BufferDomain::CachedHost;
        bi.size = VkDeviceSize(dl.verts.size()) * sizeof(Vtx);
        bi.usage = VK_BUFFER_USAGE_VERTEX_BUFFER_BIT;
        frameVerts = device.create_buffer(bi, dl.verts.data());
    }
    g_frameVerts = frameVerts.get();
    struct ClearFrameVerts
    {
        ~ClearFrameVerts() { g_frameVerts = nullptr; }
    } clearFrameVerts;

    g_gpu.ring = (g_gpu.ring + 1) % 3;
    auto makeSlot = [&](Gpu::Slot &slot, uint32_t w, uint32_t h, bool transferSrc) {
        if (slot.color && slot.color->get_width() == w && slot.color->get_height() == h)
            return;
        auto ci = Vulkan::ImageCreateInfo::immutable_2d_image(w, h, VK_FORMAT_R8G8B8A8_UNORM);
        ci.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
        ci.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                   (transferSrc ? VK_IMAGE_USAGE_TRANSFER_SRC_BIT : 0);
        slot.color = device.create_image(ci);
        auto di = Vulkan::ImageCreateInfo::render_target(w, h, VK_FORMAT_D32_SFLOAT);
        di.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
        di.usage |= VK_IMAGE_USAGE_SAMPLED_BIT; // (ambient occlusion reads it)
        slot.depth = device.create_image(di);
    };
    // IMAGE QUALITY: the main picture is drawn g_ssaa times larger each way and scaled down (smooth edges)
    const uint32_t bigW = width * g_ssaa, bigH = height * g_ssaa;
    Gpu::Slot &mainSlot = g_gpu.main[g_gpu.ring];
    makeSlot(mainSlot, bigW, bigH, true);
    Vulkan::ImageHandle color = mainSlot.color, depth = mainSlot.depth;
    if (!color || !depth)
        return {};

    auto cmd = device.request_command_buffer();
    auto beginPass = [&](const Vulkan::Image &col, const Vulkan::Image &dep) {
        cmd->image_barrier(col, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                           VK_PIPELINE_STAGE_2_COPY_BIT | VK_PIPELINE_STAGE_2_BLIT_BIT |
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT | VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT,
                           0,
                           VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT);
        cmd->image_barrier(dep, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                           VK_PIPELINE_STAGE_2_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT, 0,
                           VK_PIPELINE_STAGE_2_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT,
                           VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT | VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_READ_BIT);
        Vulkan::RenderPassInfo rp;
        rp.num_color_attachments = 1;
        rp.color_attachments[0] = &col.get_view();
        rp.depth_stencil = &dep.get_view();
        rp.clear_attachments = 1u;
        rp.store_attachments = 1u;
        // (the depth is kept: ambient occlusion reads it, and the overlays after it test against it)
        rp.op_flags = Vulkan::RENDER_PASS_OP_CLEAR_DEPTH_STENCIL_BIT | Vulkan::RENDER_PASS_OP_STORE_DEPTH_STENCIL_BIT;
        std::memset(&rp.clear_color[0], 0, sizeof(rp.clear_color[0]));
        rp.clear_depth_stencil.depth = 0.0f;
        cmd->begin_render_pass(rp);
        VkViewport vp = {0.0f, 0.0f, float(col.get_width()), float(col.get_height()), 0.0f, 1.0f};
        cmd->set_viewport(vp);
        cmd->set_scissor({{0, 0}, {col.get_width(), col.get_height()}});
    };
    // offscreen targets first (the shadow map), each drawn before the main picture samples it
    std::vector<Vulkan::ImageHandle> rts(dl.targets.size());
    for (size_t t = 0; t < dl.targets.size(); ++t)
    {
        const Offscreen &o = dl.targets[t];
        auto &ring = g_gpu.offscreen[g_gpu.ring];
        if (ring.size() <= t)
            ring.resize(t + 1);
        makeSlot(ring[t], o.width, o.height, false);
        rts[t] = ring[t].color;
        Vulkan::ImageHandle rd = ring[t].depth;
        if (!rts[t] || !rd)
        {
            rts[t].reset();
            continue;
        }
        beginPass(*rts[t], *rd);
        drawOps(*cmd, device, dl, int(t) + 1, rts, o.width, o.height, ShadowInfo{});
        cmd->end_render_pass();
        cmd->image_barrier(*rts[t], VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                           VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
    }
    // REAL-TIME SHADOWS (F10): the main camera's solid geometry drawn from the game's own light direction into a
    // depth map around the camera's focus, then every surface of the main picture darkened where it is hidden.
    ShadowInfo sh;
    if (g_shadows.load(std::memory_order_relaxed) && g_gpu.shadowProgram && dl.haveLight && dl.mainView >= 0)
    {
        Vulkan::ImageHandle &map = g_gpu.shadowMaps[g_gpu.ring];
        if (!map || map->get_width() != g_shadowSize)
        {
            auto di = Vulkan::ImageCreateInfo::render_target(g_shadowSize, g_shadowSize, VK_FORMAT_D32_SFLOAT);
            di.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
            di.usage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
            map = device.create_image(di);
        }
        if (map)
        {
            buildLightMatrix(dl, sh.light);
            std::memcpy(sh.dir, dl.lightDir, sizeof(sh.dir));
            cmd->image_barrier(*map, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                               VK_PIPELINE_STAGE_2_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT,
                               VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT | VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_READ_BIT);
            Vulkan::RenderPassInfo rp;
            rp.num_color_attachments = 0;
            rp.depth_stencil = &map->get_view();
            rp.op_flags = Vulkan::RENDER_PASS_OP_CLEAR_DEPTH_STENCIL_BIT | Vulkan::RENDER_PASS_OP_STORE_DEPTH_STENCIL_BIT;
            rp.clear_depth_stencil.depth = 1.0f;
            cmd->begin_render_pass(rp);
            VkViewport vp = {0.0f, 0.0f, float(g_shadowSize), float(g_shadowSize), 0.0f, 1.0f};
            cmd->set_viewport(vp);
            cmd->set_scissor({{0, 0}, {g_shadowSize, g_shadowSize}});
            cmd->set_program(g_gpu.shadowProgram);
            cmd->set_vertex_attrib(1, 0, VK_FORMAT_R32G32_SFLOAT, offsetof(Vtx, uv));
            cmd->set_vertex_attrib(3, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vtx, world));
            cmd->set_primitive_topology(VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST);
            cmd->set_cull_mode(VK_CULL_MODE_NONE);
            cmd->set_blend_enable(false);
            cmd->set_depth_test(true, true);
            cmd->set_depth_compare(VK_COMPARE_OP_LESS_OR_EQUAL);
            cmd->set_depth_bias(true);
            cmd->set_depth_bias(1.5f, 2.0f);
            for (const Op &op : dl.ops)
            {
                if (op.type != OpType::Draw || !op.casts)
                    continue;
                const Vulkan::ImageView *view = &g_gpu.white->get_view();
                float alphaScale = 255.0f;
                if (op.textured)
                {
                    if (op.rtTexture >= 0 && size_t(op.rtTexture) < rts.size() && rts[op.rtTexture])
                    {
                        view = &rts[op.rtTexture]->get_view();
                        alphaScale = 128.0f;
                    }
                    else if (const Vulkan::ImageView *v = versionView(device, op.version))
                        view = v;
                }
                cmd->set_texture(0, 0, *view, Vulkan::StockSampler::LinearWrap);
                const uint32_t ate = uint32_t(op.test) & 1, aref = uint32_t(op.test >> 4) & 0xFF;
                float push[28] = {op.textured ? 1.0f : 0.0f, 0.0f, float(aref), float(ate), alphaScale, 0, 0, 0,
                                  0, 0, 0, 0};
                std::memcpy(push + 12, sh.light, sizeof(sh.light));
                cmd->push_constants(push, 0, sizeof(push));
                if (g_frameVerts)
                    cmd->set_vertex_binding(0, *g_frameVerts, VkDeviceSize(op.first) * sizeof(Vtx), sizeof(Vtx));
                else
                {
                    void *dst = cmd->allocate_vertex_data(0, VkDeviceSize(op.count) * sizeof(Vtx), sizeof(Vtx));
                    if (!dst)
                        continue;
                    std::memcpy(dst, dl.verts.data() + op.first, size_t(op.count) * sizeof(Vtx));
                }
                cmd->draw(op.count, 1, 0, 0);
            }
            cmd->set_depth_bias(false);
            cmd->end_render_pass();
            cmd->image_barrier(*map, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                               VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT, VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT,
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
            sh.map = &map->get_view();
        }
    }
    beginPass(*color, *depth);
    // AMBIENT OCCLUSION: the scene up to the main camera's last draw, then the occlusion multiplied in from the
    // depth buffer, then what comes after (the HUD, overlays) on top, untouched.
    size_t split = dl.ops.size();
    const bool ao = g_lighting.load(std::memory_order_relaxed) && g_aoStrength > 0.0f && g_gpu.aoProgram &&
                    dl.mainView >= 0 && size_t(dl.mainView) < dl.viewScreens.size();
    const bool bloom = g_lighting.load(std::memory_order_relaxed) && g_bloom > 0.0f && g_gpu.bloomProgram &&
                       dl.mainView >= 0;
    const bool fog = g_lighting.load(std::memory_order_relaxed) && g_fog > 0.0f && g_gpu.fogProgram &&
                     dl.haveGround && dl.mainView >= 0 && size_t(dl.mainView) < dl.viewScreens.size();
    const bool post = g_lighting.load(std::memory_order_relaxed) && g_gpu.postProgram && dl.mainView >= 0 &&
                      size_t(dl.mainView) < dl.viewScreens.size() &&
                      (g_sky > 0.0f || g_shafts > 0.0f || g_lightning > 0.0f || g_grade > 0.0f || g_vignette > 0.0f ||
                       g_reflect > 0.0f || g_dof > 0.0f);
    if (ao || bloom || fog || post)
    {
        split = 0;
        for (size_t i = 0; i < dl.ops.size(); ++i)
            if (dl.ops[i].type == OpType::Draw && dl.ops[i].target == 0 && dl.ops[i].viewId == dl.mainView)
                split = i + 1;
    }
    drawOps(*cmd, device, dl, 0, rts, bigW, bigH, sh, 0, split);
    cmd->end_render_pass();
    if (ao || fog)
    {
        cmd->image_barrier(*depth, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                           VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT, VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
        Vulkan::RenderPassInfo rp;
        rp.num_color_attachments = 1;
        rp.color_attachments[0] = &color->get_view();
        rp.load_attachments = 1u;
        rp.store_attachments = 1u;
        cmd->begin_render_pass(rp);
        VkViewport vp = {0.0f, 0.0f, float(bigW), float(bigH), 0.0f, 1.0f};
        cmd->set_viewport(vp);
        cmd->set_scissor({{0, 0}, {bigW, bigH}});
        const Mat &scr = dl.viewScreens[dl.mainView];
        auto fullScreen = [&]() {
            auto *tri = static_cast<float *>(cmd->allocate_vertex_data(0, 6 * sizeof(float), 2 * sizeof(float)));
            const float verts[6] = {-1.0f, -1.0f, 3.0f, -1.0f, -1.0f, 3.0f};
            std::memcpy(tri, verts, sizeof(verts));
            cmd->set_vertex_attrib(0, 0, VK_FORMAT_R32G32_SFLOAT, 0);
            cmd->set_primitive_topology(VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST);
            cmd->draw(3);
        };
        cmd->set_depth_test(false, false);
        cmd->set_depth_bias(false);
        cmd->set_cull_mode(VK_CULL_MODE_NONE);
        cmd->set_blend_enable(true);
        cmd->set_blend_op(VK_BLEND_OP_ADD);
        cmd->set_texture(0, 0, depth->get_view(), Vulkan::StockSampler::NearestClamp);
        if (ao)
        {
            cmd->set_program(g_gpu.aoProgram);
            // colour * occlusion, alpha kept
            cmd->set_blend_factors(VK_BLEND_FACTOR_ZERO, VK_BLEND_FACTOR_ZERO, VK_BLEND_FACTOR_SRC_COLOR, VK_BLEND_FACTOR_ONE);
            cmd->set_color_write_mask(0xF);
            const float push[8] = {1.0f / float(bigW), 1.0f / float(bigH), scr.m[0], scr.m[5],
                                   g_aoRadius, g_aoStrength, 320.0f, 112.0f};
            cmd->push_constants(push, 0, sizeof(push));
            fullScreen();
        }
        if (fog)
        {
            // GROUND FOG: the mist colour laid over the picture as thick as the mist along each line of sight
            cmd->set_program(g_gpu.fogProgram);
            cmd->set_blend_factors(VK_BLEND_FACTOR_SRC_ALPHA, VK_BLEND_FACTOR_ZERO, VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA,
                                   VK_BLEND_FACTOR_ONE);
            cmd->set_color_write_mask(0x7);
            const Mat &V = dl.views[dl.mainView];
            const float push[32] = {scr.m[0], scr.m[5], 320.0f, 112.0f,
                                    dl.camPos[0], dl.camPos[1], dl.camPos[2], float(g_tracker.frameNo % 360000u) / 60.0f,
                                    V.m[0], V.m[4], V.m[8], 0.0f,
                                    V.m[1], V.m[5], V.m[9], 0.0f,
                                    V.m[2], V.m[6], V.m[10], 0.0f,
                                    dl.mistY, 1.8f, 0.03f * g_fog, std::min(0.7f, 0.42f * std::sqrt(g_fog)),
                                    0.23f, 0.26f, 0.30f, g_wisps * g_fog * (1.0f - 0.5f * dl.indoor),
                                    dl.player[0], dl.player[2], dl.havePlayer ? 1.0f : 0.0f,
                                    dl.inLevel ? g_distHaze * (1.0f - 0.6f * dl.indoor) : 0.0f};
            cmd->push_constants(push, 0, sizeof(push));
            fullScreen();
        }
        cmd->end_render_pass();
        cmd->image_barrier(*depth, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                           VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                           VK_PIPELINE_STAGE_2_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_2_LATE_FRAGMENT_TESTS_BIT,
                           VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT | VK_ACCESS_2_DEPTH_STENCIL_ATTACHMENT_READ_BIT);
    }
    if (bloom)
    {
        // BLOOM: the bright parts at a quarter of the size, blurred across then down, added back to the picture
        const uint32_t qw = std::max(1u, bigW / 4), qh = std::max(1u, bigH / 4);
        auto quarter = [&](Vulkan::ImageHandle &img) {
            if (img && img->get_width() == qw && img->get_height() == qh)
                return;
            auto ci = Vulkan::ImageCreateInfo::immutable_2d_image(qw, qh, VK_FORMAT_R8G8B8A8_UNORM);
            ci.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
            ci.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
            img = device.create_image(ci);
        };
        Vulkan::ImageHandle &ba = g_gpu.bloomA[g_gpu.ring], &bb = g_gpu.bloomB[g_gpu.ring];
        quarter(ba);
        quarter(bb);
        if (ba && bb)
        {
            auto fullscreen = [&](const Vulkan::Image &target, const Vulkan::ImageView &src, float mode, bool load) {
                if (!load)
                    cmd->image_barrier(target, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                                       VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                                       VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT,
                                       VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT);
                Vulkan::RenderPassInfo rp;
                rp.num_color_attachments = 1;
                rp.color_attachments[0] = &target.get_view();
                rp.store_attachments = 1u;
                if (load)
                    rp.load_attachments = 1u;
                cmd->begin_render_pass(rp);
                VkViewport vp = {0.0f, 0.0f, float(target.get_width()), float(target.get_height()), 0.0f, 1.0f};
                cmd->set_viewport(vp);
                cmd->set_scissor({{0, 0}, {target.get_width(), target.get_height()}});
                cmd->set_program(g_gpu.bloomProgram);
                cmd->set_depth_test(false, false);
                cmd->set_depth_bias(false);
                cmd->set_cull_mode(VK_CULL_MODE_NONE);
                if (load)
                {
                    cmd->set_blend_enable(true);
                    cmd->set_blend_op(VK_BLEND_OP_ADD);
                    cmd->set_blend_factors(VK_BLEND_FACTOR_ONE, VK_BLEND_FACTOR_ZERO, VK_BLEND_FACTOR_ONE,
                                           VK_BLEND_FACTOR_ONE);
                    cmd->set_color_write_mask(0x7);
                }
                else
                {
                    cmd->set_blend_enable(false);
                    cmd->set_color_write_mask(0xF);
                }
                cmd->set_texture(0, 0, src, Vulkan::StockSampler::LinearClamp);
                const float push[8] = {1.0f / float(qw), 1.0f / float(qh), mode, g_bloomThreshold, g_bloom, 0, 0, 0};
                cmd->push_constants(push, 0, sizeof(push));
                auto *tri = static_cast<float *>(cmd->allocate_vertex_data(0, 6 * sizeof(float), 2 * sizeof(float)));
                const float verts[6] = {-1.0f, -1.0f, 3.0f, -1.0f, -1.0f, 3.0f};
                std::memcpy(tri, verts, sizeof(verts));
                cmd->set_vertex_attrib(0, 0, VK_FORMAT_R32G32_SFLOAT, 0);
                cmd->set_primitive_topology(VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST);
                cmd->draw(3);
                cmd->end_render_pass();
            };
            auto toRead = [&](const Vulkan::Image &img) {
                cmd->image_barrier(img, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                                   VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT,
                                   VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT, VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT,
                                   VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
            };
            toRead(*color);
            fullscreen(*ba, color->get_view(), 0.0f, false);
            toRead(*ba);
            fullscreen(*bb, ba->get_view(), 1.0f, false);
            toRead(*bb);
            cmd->image_barrier(*color, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL,
                               VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                               VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT,
                               VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_2_COLOR_ATTACHMENT_READ_BIT);
            fullscreen(*color, bb->get_view(), 2.0f, true);
        }
    }
    if (post)
        finishPicture(*cmd, device, dl, *color, *depth, bigW, bigH);
    if ((ao || bloom || fog || post) && split < dl.ops.size())
    {
        VkViewport vp = {0.0f, 0.0f, float(bigW), float(bigH), 0.0f, 1.0f};
            Vulkan::RenderPassInfo rb;
            rb.num_color_attachments = 1;
            rb.color_attachments[0] = &color->get_view();
            rb.depth_stencil = &depth->get_view();
            rb.load_attachments = 1u;
            rb.store_attachments = 1u;
            rb.op_flags = Vulkan::RENDER_PASS_OP_LOAD_DEPTH_STENCIL_BIT | Vulkan::RENDER_PASS_OP_STORE_DEPTH_STENCIL_BIT;
            cmd->begin_render_pass(rb);
            cmd->set_viewport(vp);
            cmd->set_scissor({{0, 0}, {bigW, bigH}});
            drawOps(*cmd, device, dl, 0, rts, bigW, bigH, sh, split, dl.ops.size());
            cmd->end_render_pass();
        }
    cmd->image_barrier(*color, VK_IMAGE_LAYOUT_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                       VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT, VK_ACCESS_2_COLOR_ATTACHMENT_WRITE_BIT,
                       VK_PIPELINE_STAGE_2_BLIT_BIT, VK_ACCESS_2_TRANSFER_READ_BIT);
    Vulkan::ImageHandle result = color;
    if (g_ssaa > 1)
    {
        Vulkan::ImageHandle &out = g_gpu.outputs[g_gpu.ring];
        if (!out || out->get_width() != width || out->get_height() != height)
        {
            auto oi = Vulkan::ImageCreateInfo::immutable_2d_image(width, height, VK_FORMAT_R8G8B8A8_UNORM);
            oi.initial_layout = VK_IMAGE_LAYOUT_UNDEFINED;
            oi.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                       VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
            out = device.create_image(oi);
        }
        if (out)
        {
            cmd->image_barrier(*out, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                               VK_PIPELINE_STAGE_2_COPY_BIT | VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT, 0,
                               VK_PIPELINE_STAGE_2_BLIT_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT);
            cmd->blit_image(*out, *color, {0, 0, 0}, {int32_t(width), int32_t(height), 1}, {0, 0, 0},
                            {int32_t(bigW), int32_t(bigH), 1}, 0, 0, 0, 0, 1, VK_FILTER_LINEAR);
            cmd->image_barrier(*out, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                               VK_PIPELINE_STAGE_2_BLIT_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT,
                               VK_PIPELINE_STAGE_2_COPY_BIT | VK_PIPELINE_STAGE_2_FRAGMENT_SHADER_BIT,
                               VK_ACCESS_2_TRANSFER_READ_BIT | VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);
            result = out;
        }
    }
    device.submit(cmd);
    ++g_stats.rendered;
    g_gpu.last = result;
    g_gpu.lastFrame = g_tracker.frameNo;
    g_stats.drawMs += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - tBuild).count();

    const auto now = std::chrono::steady_clock::now();
    if (now - g_stats.last >= std::chrono::seconds(5))
    {
        g_stats.last = now;
        std::fprintf(stderr,
                     "[native] last 5 s: %llu frames drawn natively, %llu shown by paraLLEl-GS (no matching frame), "
                     "%llu batches skipped for a missing texture; %zu texture versions, %llu uploads, %llu decodes, "
                     "%llu uploads in an unsupported format, %llu local copies; %zu triangles, %zu draws, %zu targets; "
                     "CPU per drawn frame: %.2f ms building and recording (%.2f ms of it vegetation), %.2f ms following the GS stream\n",
                     static_cast<unsigned long long>(g_stats.rendered), static_cast<unsigned long long>(g_stats.noFrame),
                     static_cast<unsigned long long>(g_stats.missing), g_tracker.versions.size(),
                     static_cast<unsigned long long>(g_tracker.statUploads),
                     static_cast<unsigned long long>(g_tracker.statDecodes),
                     static_cast<unsigned long long>(g_tracker.statUnsupported),
                     static_cast<unsigned long long>(g_tracker.statLocalCopies), dl.verts.size() / 3, dl.ops.size(),
                     dl.targets.size(), g_stats.rendered ? g_stats.drawMs / double(g_stats.rendered) : 0.0,
                     g_stats.rendered ? g_vegMs / double(g_stats.rendered) : 0.0,
                     g_stats.rendered ? g_tracker.statStreamMs / double(g_stats.rendered) : 0.0);
        g_stats.drawMs = 0.0;
        g_vegMs = 0.0;
        g_tracker.statStreamMs = 0.0;
        g_stats.rendered = g_stats.noFrame = g_stats.missing = 0;
        g_tracker.statUploads = g_tracker.statDecodes = g_tracker.statUnsupported = g_tracker.statLocalCopies = 0;
    }
    (void)guestFrame;
    return result;
}
#endif
#ifdef NATIVE_TEST
bool testDecode(const uint8_t *vram, uint64_t tex0, std::vector<uint32_t> &out, uint32_t &w, uint32_t &h)
{
    buildTables();
    g_tracker.vram.assign(vram, vram + kVramSize);
    TexVersion v;
    if (!g_tracker.decode(tex0, v))
        return false;
    out = v.rgba; w = v.width; h = v.height;
    return true;
}
void testBuild(const Frame &f, const uint8_t *vram, const char *outPath)
{
    buildTables();
    g_tracker.vram.assign(vram, vram + kVramSize);
    g_tracker.seeded = true;
    for (const Cmd &c : f.cmds)
        if ((c.cmd == 11 || c.cmd == 12) && c.blobSize >= 0x38)
        {
            uint64_t tex0;
            std::memcpy(&tex0, f.data.data() + c.blobOff + 0x30, 8);
            g_tracker.onTex0(tex0);
        }
    GsRecord r;
    r.binds = g_tracker.binds;
    g_tracker.records.push_back(r);
    Resolver res(g_tracker.records, 0, g_tracker.keys);
    DrawList dl;
    for (int rep = 0; rep < (std::getenv("NR_REPEAT") ? 4 : 1); ++rep)
    {
        auto saved = g_identityVotes;
        dl.verts.clear();
        dl.ops.clear();
        dl.targets.clear();
        dl.views.clear();
        dl.viewScreens.clear();
        dl.lights.clear();
        dl.mainView = -1;
        dl.haveLight = false;
        g_vegMs = 0.0;
        Resolver r2(g_tracker.records, 0, g_tracker.keys);
        const auto tb = std::chrono::steady_clock::now();
        if (std::getenv("NR_HUDTEST"))
        {
            Frame f2 = f;
            f2.hud.valid = 1;
            f2.hud.healthMax = 8;
            const int32_t v[17] = {0, 0, 5, 3, 3, 1234, 57, 2, 0, 0, 12, 0, 0, 0, 4, 0, 0};
            std::memcpy(f2.hud.vars, v, sizeof v);
            buildDrawList(f2, r2, dl);
        }
        else
            buildDrawList(f, r2, dl);
        const double all = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - tb).count();
        if (std::getenv("NR_REPEAT"))
            std::printf("  build %d: %.2f ms in all, vegetation %.2f ms\n", rep, all, g_vegMs);
        if (rep == 0 && !std::getenv("NR_KEEPVOTES"))
            g_identityVotes = saved;
    }
    (void)res;
    {
        int lk[8] = {};
        for (const Op &op : dl.ops)
            if (op.type == OpType::Draw && op.look[0] >= 1.0f && op.look[0] < 8.0f)
                ++lk[int(op.look[0])];
        std::printf("looks: hud %d shadow %d orb %d gold %d maximo %d enemy %d ghost %d\n", lk[1], lk[2], lk[3], lk[4], lk[5],
                    lk[6], lk[7]);
    }
    if (std::getenv("NR_GREEN"))
    {
        std::map<uint32_t, float> upArea;
        for (const Op &op : dl.ops)
        {
            if (op.type != OpType::Draw || op.target != 0 || op.viewId != dl.mainView || op.generated || op.count % 3)
                continue;
            for (uint32_t i = op.first; i + 2 < op.first + op.count; i += 3)
            {
                const Vtx &a = dl.verts[i], &b = dl.verts[i + 1], &c = dl.verts[i + 2];
                float e1[3], e2[3];
                for (int k = 0; k < 3; ++k)
                {
                    e1[k] = b.world[k] - a.world[k];
                    e2[k] = c.world[k] - a.world[k];
                }
                const float ny = e1[2] * e2[0] - e1[0] * e2[2];
                const float nx = e1[1] * e2[2] - e1[2] * e2[1], nz = e1[0] * e2[1] - e1[1] * e2[0];
                const float l = std::sqrt(nx * nx + ny * ny + nz * nz);
                if (l > 0 && std::fabs(ny) / l > 0.6f)
                    upArea[op.version] += l * 0.5f;
            }
        }
        for (auto &kv : upArea)
            if (kv.second > 5.0f)
                std::printf("  ground version %u area %.1f\n", kv.first, kv.second);
    }
    FILE *o = std::fopen(outPath, "wb");
    uint32_t n = uint32_t(dl.verts.size()), nops = uint32_t(dl.ops.size()), nt = uint32_t(dl.targets.size()),
             nv = uint32_t(g_tracker.versions.size());
    std::fwrite(&n, 4, 1, o);
    std::fwrite(dl.verts.data(), sizeof(Vtx), n, o);
    std::fwrite(&nops, 4, 1, o);
    for (const Op &op : dl.ops)
    {
        uint32_t rec[14] = {uint32_t(op.type), uint32_t(op.target), op.first, op.count, op.version, uint32_t(op.rtTexture),
                            op.textured, op.abe, op.zwrite, op.clampUV, op.alphaReg, uint32_t(op.test), op.casts, op.receives};
        std::fwrite(rec, 4, 14, o);
    }
    std::fwrite(&nt, 4, 1, o);
    for (const Offscreen &t : dl.targets)
        std::fwrite(&t, sizeof(t), 1, o);
    std::fwrite(&nv, 4, 1, o);
    for (auto &kv : g_tracker.versions)
    {
        uint32_t hdr[3] = {kv.first, kv.second.width, kv.second.height};
        std::fwrite(hdr, 4, 3, o);
        std::fwrite(kv.second.rgba.data(), 4, kv.second.rgba.size(), o);
        if (std::getenv("NR_GREEN") && kv.second.width)
        {
            char fn[64];
            std::snprintf(fn, sizeof fn, "/tmp/ver_%u.ppm", kv.first);
            if (FILE *pf = std::fopen(fn, "wb"))
            {
                std::fprintf(pf, "P6 %u %u 255\n", kv.second.width, kv.second.height);
                for (uint32_t px : kv.second.rgba)
                {
                    const uint8_t rgb[3] = {uint8_t(std::min(255u, (px & 255u) * 2)), uint8_t(std::min(255u, ((px >> 8) & 255u) * 2)),
                                            uint8_t(std::min(255u, ((px >> 16) & 255u) * 2))};
                    std::fwrite(rgb, 1, 3, pf);
                }
                std::fclose(pf);
            }
        }
        if (std::getenv("NR_GREEN") && kv.second.green > 0.02f)
            std::printf("  green version %u %ux%u green %.2f grassy %d grey %.2f\n", kv.first, kv.second.width, kv.second.height,
                        kv.second.green, int(kv.second.grassy), kv.second.grey);
        if (kv.second.hot > 0.05f)
            std::printf("  version %u %ux%u hot %.2f (%.2f %.2f %.2f)\n", kv.first, kv.second.width, kv.second.height,
                        kv.second.hot, kv.second.hotCol[0], kv.second.hotCol[1], kv.second.hotCol[2]);
    }
    float light[16];
    buildLightMatrix(dl, light);
    uint32_t hl = dl.haveLight;
    std::fwrite(&hl, 4, 1, o);
    std::fwrite(dl.lightDir, 4, 3, o);
    std::fwrite(dl.camPos, 4, 3, o);
    std::fwrite(dl.camFwd, 4, 3, o);
    std::fwrite(light, 4, 16, o);
    std::fclose(o);
    if (FILE *g = std::fopen("nr_shell.bin", "wb"))
    {
        for (size_t i = 0; i < dl.ops.size(); ++i)
            if (dl.ops[i].shell > 0.0f)
            {
                const uint32_t idx = uint32_t(i);
                std::fwrite(&idx, 4, 1, g);
                std::fwrite(&dl.ops[i].shell, 4, 1, g);
            }
        std::fclose(g);
    }
    std::printf("verts %u ops %u targets %u versions %u missing %u (vegetation %.2f ms)\n", n, nops, nt, nv, dl.missingTextures, g_vegMs);
    if (std::getenv("NR_MOON"))
    {
        std::printf("  mainView %d gameMoon %d at %.2f %.2f\n", dl.mainView, int(dl.gameMoon), dl.gameMoonUV[0], dl.gameMoonUV[1]);
        for (const Op &op : dl.ops)
            if (op.type == OpType::Draw && op.count)
            {
                double sx = 0, sy = 0, n = 0;
                float lo[2] = {9, 9}, hi[2] = {-9, -9};
                for (uint32_t i = op.first; i < op.first + op.count; ++i)
                {
                    const Vtx &v = dl.verts[i];
                    if (v.pos[3] <= 1e-3f)
                        continue;
                    const float x = v.pos[0] / v.pos[3], y = v.pos[1] / v.pos[3];
                    sx += x; sy += y; n += 1;
                    lo[0] = std::min(lo[0], x); lo[1] = std::min(lo[1], y);
                    hi[0] = std::max(hi[0], x); hi[1] = std::max(hi[1], y);
                }
                auto it = g_tracker.versions.find(op.version);
                std::printf("  view %d v%u n%u abe%d zw%d bright %.2f  screen %.2f..%.2f x %.2f..%.2f\n", op.viewId,
                            op.version, op.count, int(op.abe), int(op.zwrite),
                            it != g_tracker.versions.end() ? it->second.bright : -1.0f, lo[0], hi[0], lo[1], hi[1]);
            }
    }
    if (std::getenv("NR_FLAGS"))
        for (const Op &op : dl.ops)
            if (op.metal > 0.0f || op.glow > 0.0f || op.reflect > 0.0f)
                std::printf("  flagged v%u n%u metal %.1f glow %.1f reflect %.2f shiny %d ate %d\n", op.version, op.count,
                            op.metal, op.glow, op.reflect, int(op.shiny), int(op.test & 1));
    if (std::getenv("NR_VIEWS"))
        for (const Op &op : dl.ops)
            if (op.type == OpType::Draw)
                std::printf("  view %d target %d n%u zwrite %d abe %d ztest %llx\n", op.viewId, op.target, op.count,
                            int(op.zwrite), int(op.abe), (unsigned long long)(op.test >> 16));
    if (std::getenv("NR_BBOX"))
        for (const Op &op : dl.ops)
            if (op.count && op.viewId == dl.mainView)
            {
                float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
                for (uint32_t i = op.first; i < op.first + op.count; ++i)
                    for (int k = 0; k < 3; ++k)
                    {
                        lo[k] = std::min(lo[k], dl.verts[i].world[k]);
                        hi[k] = std::max(hi[k], dl.verts[i].world[k]);
                    }
                std::printf("  op v%u n%u shiny%d lit%d ate%d special%d emis%d abe%d zw%d gen%d skin%d  %.1f %.1f %.1f .. %.1f %.1f %.1f\n",
                            op.version, op.count, op.shiny, op.lit, int(op.test & 1), int(op.special), int(op.emissive),
                            int(op.abe), int(op.zwrite), int(op.generated), int(op.skinned), lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]);
            }
    for (const auto &L : dl.lights)
        std::printf("  light at %.1f %.1f %.1f radius %.1f colour %.2f %.2f %.2f weight %.2f\n", L.pos[0], L.pos[1],
                    L.pos[2], L.radius, L.col[0], L.col[1], L.col[2], L.weight);
}
// Benchmark: uploads of 128x128 PSMT8 textures into cycling slots, each followed by its TEX0.
void testBench(const uint8_t *vram, int frames)
{
    buildTables();
    g_tracker.reset();
    g_tracker.vram.assign(vram, vram + kVramSize);
    g_tracker.seeded = true;
    std::vector<uint8_t> pkt;
    auto qw = [&](uint64_t lo, uint64_t hi) {
        pkt.insert(pkt.end(), reinterpret_cast<uint8_t *>(&lo), reinterpret_cast<uint8_t *>(&lo) + 8);
        pkt.insert(pkt.end(), reinterpret_cast<uint8_t *>(&hi), reinterpret_cast<uint8_t *>(&hi) + 8);
    };
    std::vector<uint8_t> texels(128 * 128);
    const auto t0 = std::chrono::steady_clock::now();
    for (int fr = 0; fr < frames; ++fr)
        for (int t = 0; t < 48; ++t)
        {
            for (size_t i = 0; i < texels.size(); ++i)
                texels[i] = uint8_t(i * 7 + (t % 12) * 13);
            const uint32_t slot = 0x3000 + (t % 8) * 0x40;
            pkt.clear();
            qw(4 | (1ull << 15) | (1ull << 60), 0xE); // A+D x4
            qw((uint64_t(slot) << 32) | (2ull << 48) | (0x13ull << 56), 0x50);
            qw(0, 0x51);
            qw(128 | (128ull << 32), 0x52);
            qw(0, 0x53);
            qw((128 * 128 / 16) | (1ull << 15) | (2ull << 58), 0); // IMAGE
            pkt.insert(pkt.end(), texels.begin(), texels.end());
            const uint64_t tex0 = slot | (2ull << 14) | (0x13ull << 20) | (7ull << 26) | (7ull << 30) | (0x3F00ull << 37);
            qw(1 | (1ull << 15) | (1ull << 60), 0xE);
            qw(tex0, 0x06);
            g_tracker.packet(3, pkt.data(), uint32_t(pkt.size()));
        }
    const double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    std::printf("bench: %.3f ms per frame (48 uploads + binds), %llu decodes total, %zu versions\n", ms / frames,
                static_cast<unsigned long long>(g_tracker.statDecodes), g_tracker.versions.size());
}
#endif
} // namespace native
