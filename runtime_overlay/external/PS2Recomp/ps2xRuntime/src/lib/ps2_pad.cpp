#include "runtime/ps2_pad.h"
#include "ps2_host_backend.h"
#include "ps2_log.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>

#if !defined(PLATFORM_VITA) && !defined(__ANDROID__)
#define PS2X_PAD_GLFW 1
// raylib's desktop build contains GLFW 3.4. raylib's IsGamepadAvailable() is true for any joystick, mapped or not,
// so the mapping check needs GLFW's own joystick functions (GLFW/glfw3.h).
extern "C"
{
    int glfwJoystickPresent(int jid);
    int glfwJoystickIsGamepad(int jid);
    const char *glfwGetJoystickName(int jid);
    const char *glfwGetJoystickGUID(int jid);
    int glfwUpdateGamepadMappings(const char *string);
}
#else
#define PS2X_PAD_GLFW 0
#endif

namespace
{
    constexpr uint8_t kPadAnalogMarker = 0x73;
    constexpr uint8_t kPadStickCenter = 0x80;

    constexpr uint16_t PAD_LEFT = 0x0080u;
    constexpr uint16_t PAD_DOWN = 0x0040u;
    constexpr uint16_t PAD_RIGHT = 0x0020u;
    constexpr uint16_t PAD_UP = 0x0010u;
    constexpr uint16_t PAD_START = 0x0008u;
    constexpr uint16_t PAD_R3 = 0x0004u;
    constexpr uint16_t PAD_L3 = 0x0002u;
    constexpr uint16_t PAD_SELECT = 0x0001u;
    constexpr uint16_t PAD_SQUARE = 0x8000u;
    constexpr uint16_t PAD_CROSS = 0x4000u;
    constexpr uint16_t PAD_CIRCLE = 0x2000u;
    constexpr uint16_t PAD_TRIANGLE = 0x1000u;
    constexpr uint16_t PAD_R1 = 0x0800u;
    constexpr uint16_t PAD_L1 = 0x0400u;
    constexpr uint16_t PAD_R2 = 0x0200u;
    constexpr uint16_t PAD_L2 = 0x0100u;

    constexpr int kMaxHostPads = 4;   // raylib's MAX_GAMEPADS
    std::atomic<int> g_activePad{-1}; // set on the main thread by ps2PadHostUpdate, read on the game thread

#if PS2X_PAD_GLFW
    // Mappings missing from GLFW 3.4's built-in list, in SDL_GameControllerDB format.
    // DualSense Edge (054C:0DF2): the DualSense's DirectInput layout (GLFW's "PS5 Controller" line), new product ID.
    const char *const kExtraMappings =
        "030000004c050000f20d000000000000,DualSense Edge,a:b1,b:b2,back:b8,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,"
        "dpup:h0.1,guide:b12,leftshoulder:b4,leftstick:b10,lefttrigger:a3,leftx:a0,lefty:a1,misc1:b13,"
        "rightshoulder:b5,rightstick:b11,righttrigger:a4,rightx:a2,righty:a5,start:b9,x:b0,y:b3,platform:Windows,\n";

    void loadExtraMappings()
    {
        if (!glfwUpdateGamepadMappings(kExtraMappings))
            RUNTIME_LOG("[pad] built-in extra controller mappings were rejected\n");

        std::string file;
        if (const char *env = std::getenv("PS2X_GAMEPAD_DB"); env && *env)
            file = env;
        else
            file = std::string(GetApplicationDirectory()) + "gamecontrollerdb.txt"; // raylib: ends in a separator
        std::ifstream in(file, std::ios::binary);
        if (!in)
            return;
        std::ostringstream text;
        text << in.rdbuf();
        // GLFW skips lines it can't parse and other platforms' lines; it fails only on a bad call.
        const bool ok = glfwUpdateGamepadMappings(text.str().c_str()) != 0;
        RUNTIME_LOG("[pad] controller mappings from " << file << (ok ? " loaded" : " rejected") << "\n");
    }
#endif
}

namespace
{
    enum StickDir
    {
        kStickNone,
        kStickUp,
        kStickDown,
        kStickLeft,
        kStickRight
    };

    struct ActionDef
    {
        const char *name;
        uint16_t mask; // PS2 button bit, or 0 for a stick direction
        StickDir stick;
        bool rightStick; // camera_ actions push the right stick
    };

    const ActionDef kActions[] = {
        {"up", PAD_UP, kStickNone},
        {"down", PAD_DOWN, kStickNone},
        {"left", PAD_LEFT, kStickNone},
        {"right", PAD_RIGHT, kStickNone},
        {"cross", PAD_CROSS, kStickNone},
        {"circle", PAD_CIRCLE, kStickNone},
        {"square", PAD_SQUARE, kStickNone},
        {"triangle", PAD_TRIANGLE, kStickNone},
        {"l1", PAD_L1, kStickNone},
        {"r1", PAD_R1, kStickNone},
        {"l2", PAD_L2, kStickNone},
        {"r2", PAD_R2, kStickNone},
        {"l3", PAD_L3, kStickNone},
        {"r3", PAD_R3, kStickNone},
        {"start", PAD_START, kStickNone},
        {"select", PAD_SELECT, kStickNone},
        {"stick_up", 0, kStickUp},
        {"stick_down", 0, kStickDown},
        {"stick_left", 0, kStickLeft},
        {"stick_right", 0, kStickRight},
        {"camera_up", 0, kStickUp, true},
        {"camera_down", 0, kStickDown, true},
        {"camera_left", 0, kStickLeft, true},
        {"camera_right", 0, kStickRight, true},
    };
    constexpr int kActionCount = static_cast<int>(sizeof(kActions) / sizeof(kActions[0]));
    constexpr int kMaxPerAction = 4;

    struct NamedCode
    {
        const char *name;
        int code;
    };

    const NamedCode kKeyNames[] = {
        {"A", KEY_A}, {"B", KEY_B}, {"C", KEY_C}, {"D", KEY_D}, {"E", KEY_E}, {"F", KEY_F}, {"G", KEY_G},
        {"H", KEY_H}, {"I", KEY_I}, {"J", KEY_J}, {"K", KEY_K}, {"L", KEY_L}, {"M", KEY_M}, {"N", KEY_N},
        {"O", KEY_O}, {"P", KEY_P}, {"Q", KEY_Q}, {"R", KEY_R}, {"S", KEY_S}, {"T", KEY_T}, {"U", KEY_U},
        {"V", KEY_V}, {"W", KEY_W}, {"X", KEY_X}, {"Y", KEY_Y}, {"Z", KEY_Z},
        {"0", KEY_ZERO}, {"1", KEY_ONE}, {"2", KEY_TWO}, {"3", KEY_THREE}, {"4", KEY_FOUR},
        {"5", KEY_FIVE}, {"6", KEY_SIX}, {"7", KEY_SEVEN}, {"8", KEY_EIGHT}, {"9", KEY_NINE},
        {"SPACE", KEY_SPACE}, {"ENTER", KEY_ENTER}, {"TAB", KEY_TAB}, {"BACKSPACE", KEY_BACKSPACE},
        {"INSERT", KEY_INSERT}, {"DELETE", KEY_DELETE}, {"HOME", KEY_HOME}, {"END", KEY_END},
        {"PAGE_UP", KEY_PAGE_UP}, {"PAGE_DOWN", KEY_PAGE_DOWN}, {"CAPS_LOCK", KEY_CAPS_LOCK},
        {"UP", KEY_UP}, {"DOWN", KEY_DOWN}, {"LEFT", KEY_LEFT}, {"RIGHT", KEY_RIGHT},
        {"LEFT_SHIFT", KEY_LEFT_SHIFT}, {"RIGHT_SHIFT", KEY_RIGHT_SHIFT},
        {"LEFT_CONTROL", KEY_LEFT_CONTROL}, {"RIGHT_CONTROL", KEY_RIGHT_CONTROL},
        {"LEFT_ALT", KEY_LEFT_ALT}, {"RIGHT_ALT", KEY_RIGHT_ALT},
        {"MINUS", KEY_MINUS}, {"EQUAL", KEY_EQUAL}, {"LEFT_BRACKET", KEY_LEFT_BRACKET},
        {"RIGHT_BRACKET", KEY_RIGHT_BRACKET}, {"BACKSLASH", KEY_BACKSLASH}, {"SEMICOLON", KEY_SEMICOLON},
        {"APOSTROPHE", KEY_APOSTROPHE}, {"COMMA", KEY_COMMA}, {"PERIOD", KEY_PERIOD}, {"SLASH", KEY_SLASH},
        {"GRAVE", KEY_GRAVE},
        {"F1", KEY_F1}, {"F2", KEY_F2}, {"F3", KEY_F3}, {"F4", KEY_F4}, {"F5", KEY_F5}, {"F6", KEY_F6},
        {"F7", KEY_F7}, {"F8", KEY_F8}, {"F9", KEY_F9}, {"F10", KEY_F10}, {"F11", KEY_F11},
        {"KP_0", KEY_KP_0}, {"KP_1", KEY_KP_1}, {"KP_2", KEY_KP_2}, {"KP_3", KEY_KP_3}, {"KP_4", KEY_KP_4},
        {"KP_5", KEY_KP_5}, {"KP_6", KEY_KP_6}, {"KP_7", KEY_KP_7}, {"KP_8", KEY_KP_8}, {"KP_9", KEY_KP_9},
        {"KP_DECIMAL", KEY_KP_DECIMAL}, {"KP_DIVIDE", KEY_KP_DIVIDE}, {"KP_MULTIPLY", KEY_KP_MULTIPLY},
        {"KP_SUBTRACT", KEY_KP_SUBTRACT}, {"KP_ADD", KEY_KP_ADD}, {"KP_ENTER", KEY_KP_ENTER},
    };
    // Esc (closes the game window) and F12 (screenshot) are left out on purpose.

    const NamedCode kPadNames[] = {
        {"SOUTH", GAMEPAD_BUTTON_RIGHT_FACE_DOWN}, {"EAST", GAMEPAD_BUTTON_RIGHT_FACE_RIGHT},
        {"WEST", GAMEPAD_BUTTON_RIGHT_FACE_LEFT}, {"NORTH", GAMEPAD_BUTTON_RIGHT_FACE_UP},
        {"DPAD_UP", GAMEPAD_BUTTON_LEFT_FACE_UP}, {"DPAD_DOWN", GAMEPAD_BUTTON_LEFT_FACE_DOWN},
        {"DPAD_LEFT", GAMEPAD_BUTTON_LEFT_FACE_LEFT}, {"DPAD_RIGHT", GAMEPAD_BUTTON_LEFT_FACE_RIGHT},
        {"L1", GAMEPAD_BUTTON_LEFT_TRIGGER_1}, {"R1", GAMEPAD_BUTTON_RIGHT_TRIGGER_1},
        {"L2", GAMEPAD_BUTTON_LEFT_TRIGGER_2}, {"R2", GAMEPAD_BUTTON_RIGHT_TRIGGER_2},
        {"L3", GAMEPAD_BUTTON_LEFT_THUMB}, {"R3", GAMEPAD_BUTTON_RIGHT_THUMB},
        {"START", GAMEPAD_BUTTON_MIDDLE_RIGHT}, {"SELECT", GAMEPAD_BUTTON_MIDDLE_LEFT},
    };

    // The layout before bindings existed, less Esc as a second Circle key (Esc closes the window).
    const char *const kDefaultKeys[kActionCount] = {
        "UP, W", "DOWN, S", "LEFT, A", "RIGHT, D", "X, SPACE", "C", "Z, KP_0", "V, KP_1",
        "Q", "E", "LEFT_SHIFT", "RIGHT_SHIFT", "", "R", "ENTER", "TAB",
        "", "", "", "",
        "I", "K", "J", "L"};
    const char *const kDefaultPad[kActionCount] = {
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "SOUTH", "EAST", "WEST", "NORTH",
        "L1", "R1", "L2", "R2", "L3", "R3", "START", "SELECT",
        "", "", "", "",
        "", "", "", ""};

    struct BindingList
    {
        int codes[kMaxPerAction];
        int count;
    };

    BindingList g_keys[kActionCount];
    BindingList g_pads[kActionCount];
    std::once_flag g_bindingsOnce;

    std::string trimUpper(std::string s, bool upper)
    {
        const size_t a = s.find_first_not_of(" \t\r\n");
        const size_t b = s.find_last_not_of(" \t\r\n");
        s = a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
        for (char &c : s)
            c = static_cast<char>(upper ? std::toupper(static_cast<unsigned char>(c)) : std::tolower(static_cast<unsigned char>(c)));
        return s;
    }

    // Fills one action's list from "NAME, NAME"; unknown names are appended to `unknown`.
    void parseList(const std::string &value, const NamedCode *table, size_t tableSize, BindingList &out, std::string &unknown)
    {
        out.count = 0;
        std::stringstream parts(value);
        std::string part;
        while (std::getline(parts, part, ','))
        {
            part = trimUpper(part, true);
            if (part.empty())
                continue;
            int code = -1;
            for (size_t i = 0; i < tableSize; ++i)
                if (part == table[i].name)
                    code = table[i].code;
            if (code < 0)
                unknown += (unknown.empty() ? "" : ", ") + part;
            else if (out.count < kMaxPerAction)
                out.codes[out.count++] = code;
        }
    }

    void loadBindings()
    {
        std::string unknown;
        for (int a = 0; a < kActionCount; ++a)
        {
            parseList(kDefaultKeys[a], kKeyNames, sizeof(kKeyNames) / sizeof(kKeyNames[0]), g_keys[a], unknown);
            parseList(kDefaultPad[a], kPadNames, sizeof(kPadNames) / sizeof(kPadNames[0]), g_pads[a], unknown);
        }

        std::string file;
        if (const char *env = std::getenv("PS2X_CONTROLS"); env && *env)
            file = env;
#if PS2X_PAD_GLFW
        else
            file = std::string(GetApplicationDirectory()) + "../controls.ini"; // the launcher's folder
#endif
        std::ifstream in(file, std::ios::binary);
        if (file.empty() || !in)
        {
            RUNTIME_LOG("[pad] controls: defaults\n");
            return;
        }

        int section = -1; // 0 keyboard, 1 controller
        int lines = 0;
        std::string line;
        while (std::getline(in, line))
        {
            line = trimUpper(line, false);
            if (line.empty() || line[0] == ';' || line[0] == '#')
                continue;
            if (line == "[keyboard]")
            {
                section = 0;
                continue;
            }
            if (line == "[controller]")
            {
                section = 1;
                continue;
            }
            const size_t eq = line.find('=');
            if (section < 0 || eq == std::string::npos)
                continue;
            const std::string action = trimUpper(line.substr(0, eq), false);
            int a = 0;
            while (a < kActionCount && action != kActions[a].name)
                ++a;
            if (a == kActionCount)
            {
                unknown += (unknown.empty() ? "" : ", ") + action;
                continue;
            }
            if (section == 0)
                parseList(line.substr(eq + 1), kKeyNames, sizeof(kKeyNames) / sizeof(kKeyNames[0]), g_keys[a], unknown);
            else
                parseList(line.substr(eq + 1), kPadNames, sizeof(kPadNames) / sizeof(kPadNames[0]), g_pads[a], unknown);
            ++lines;
        }
        RUNTIME_LOG("[pad] controls from " << file << ": " << lines << " bindings"
                                           << (unknown.empty() ? std::string() : "; ignored unknown names: " + unknown) << "\n");
    }
}

void ps2PadHostUpdate()
{
#if PS2X_PAD_GLFW
    static bool mappingsLoaded = false;
    if (!mappingsLoaded)
    {
        mappingsLoaded = true;
        loadExtraMappings();
    }

    // raylib already checks presence of four slots every frame; a quarter-second rescan is enough for hot-plugging.
    static uint32_t frame = 0;
    if ((frame++ % 15u) != 0u)
        return;

    int active = -1;
    std::string summary;
    for (int jid = 0; jid < kMaxHostPads; ++jid)
    {
        if (!glfwJoystickPresent(jid))
            continue;
        const bool mapped = glfwJoystickIsGamepad(jid) == 1;
        if (mapped && active < 0)
            active = jid;
        const char *name = glfwGetJoystickName(jid);
        const char *guid = glfwGetJoystickGUID(jid);
        summary += "slot " + std::to_string(jid) + ": \"" + (name ? name : "?") + "\" " + (guid ? guid : "?") +
                   (mapped ? " (mapped); " : " (no mapping); ");
    }
    g_activePad.store(active, std::memory_order_relaxed);

    static std::string lastSummary("\x01"); // never a real summary, so the first scan is always logged
    if (summary != lastSummary)
    {
        lastSummary = summary;
        if (summary.empty())
            RUNTIME_LOG("[pad] no controller connected; keyboard only\n");
        else if (active < 0)
            RUNTIME_LOG("[pad] " << summary << "none has a mapping; keyboard only\n");
        else
            RUNTIME_LOG("[pad] " << summary << "using slot " << active << "\n");
    }
#else
    g_activePad.store(IsGamepadAvailable(0) ? 0 : -1, std::memory_order_relaxed);
#endif
}

void ps2PadStickBytes(float x, float y, uint8_t &outX, uint8_t &outY)
{
    x = std::clamp(x, -1.0f, 1.0f);
    y = std::clamp(y, -1.0f, 1.0f);
    const float larger = std::max(std::fabs(x), std::fabs(y));
    if (larger > 0.0f)
    {
        const float scale = std::min(std::hypot(x, y), 1.0f) / larger;
        x = std::clamp(x * scale, -1.0f, 1.0f);
        y = std::clamp(y * scale, -1.0f, 1.0f);
    }
    outX = static_cast<uint8_t>(std::lround((x + 1.0f) * 127.5f));
    outY = static_cast<uint8_t>(std::lround((y + 1.0f) * 127.5f));
}

bool PSPadBackend::readState(int /*port*/, int /*slot*/, uint8_t *data, size_t size)
{
    if (!data || size < 32)
        return false;

    std::memset(data, 0, 32);
    data[0] = 0x01;
    data[1] = kPadAnalogMarker;
    data[2] = 0xFF;
    data[3] = 0xFF;
    data[4] = data[5] = data[6] = data[7] = kPadStickCenter;

    std::call_once(g_bindingsOnce, loadBindings);

    uint16_t btns = 0xFFFFu;
    // The controller picked by ps2PadHostUpdate; the keyboard works alongside it.
    const int gamepad = g_activePad.load(std::memory_order_relaxed);
    const bool useGamepad = gamepad >= 0 && IsGamepadAvailable(gamepad);

    if (useGamepad)
    {
        ps2PadStickBytes(GetGamepadAxisMovement(gamepad, GAMEPAD_AXIS_LEFT_X),
                         GetGamepadAxisMovement(gamepad, GAMEPAD_AXIS_LEFT_Y), data[6], data[7]);
        ps2PadStickBytes(GetGamepadAxisMovement(gamepad, GAMEPAD_AXIS_RIGHT_X),
                         GetGamepadAxisMovement(gamepad, GAMEPAD_AXIS_RIGHT_Y), data[4], data[5]);
    }

    // Alt+Enter switches the window mode (ps2_runtime.cpp), so an Enter pressed with Alt held does nothing in the
    // game until it is released.
    static std::atomic<bool> enterIsWindowToggle{false};
    const bool enterDown = IsKeyDown(KEY_ENTER);
    if (!enterDown)
        enterIsWindowToggle.store(false, std::memory_order_relaxed);
    else if (IsKeyDown(KEY_LEFT_ALT) || IsKeyDown(KEY_RIGHT_ALT))
        enterIsWindowToggle.store(true, std::memory_order_relaxed);
    const bool enterUsable = enterDown && !enterIsWindowToggle.load(std::memory_order_relaxed);

    int stickX[2] = {0, 0}, stickY[2] = {0, 0}; // [0] left stick, [1] right stick
    bool stickPressed[2] = {false, false};
    for (int a = 0; a < kActionCount; ++a)
    {
        bool down = false;
        for (int i = 0; i < g_keys[a].count && !down; ++i)
            down = g_keys[a].codes[i] == KEY_ENTER ? enterUsable : IsKeyDown(g_keys[a].codes[i]);
        for (int i = 0; useGamepad && i < g_pads[a].count && !down; ++i)
            down = IsGamepadButtonDown(gamepad, g_pads[a].codes[i]);
        if (!down)
            continue;
        if (kActions[a].mask != 0)
        {
            btns &= static_cast<uint16_t>(~kActions[a].mask);
            continue;
        }
        const int s = kActions[a].rightStick ? 1 : 0;
        stickPressed[s] = true;
        stickX[s] += kActions[a].stick == kStickRight ? 1 : kActions[a].stick == kStickLeft ? -1 : 0;
        stickY[s] += kActions[a].stick == kStickDown ? 1 : kActions[a].stick == kStickUp ? -1 : 0;
    }
    if (stickPressed[0])
        ps2PadStickBytes(static_cast<float>(stickX[0]), static_cast<float>(stickY[0]), data[6], data[7]);
    if (stickPressed[1])
        ps2PadStickBytes(static_cast<float>(stickX[1]), static_cast<float>(stickY[1]), data[4], data[5]);

    data[2] = static_cast<uint8_t>(btns & 0xFF);
    data[3] = static_cast<uint8_t>(btns >> 8);
    return true;
}
