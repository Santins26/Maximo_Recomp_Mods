#ifndef PS2_PAD_H
#define PS2_PAD_H

#include <cstddef>
#include <cstdint>

class PSPadBackend
{
public:
    PSPadBackend() = default;
    ~PSPadBackend() = default;

    bool readState(int port, int slot, uint8_t *data, size_t size);
};

// Converts a host analog stick (each axis -1..1, round gate) to DualShock 2 stick bytes (0x00..0xFF, 0x80 centred).
// A DualShock 2 reaches the corners of its square range when pushed fully on a diagonal; a round-gate stick only
// reaches about 0.71 per axis there. Games judge the push by the larger axis (Maximo runs above about 0.8 and walks
// below), so the stick is mapped from its circle onto the square: same direction, larger axis = distance pushed.
void ps2PadStickBytes(float x, float y, uint8_t &outX, uint8_t &outY);

// Main thread only, once per presented frame (after EndDrawing has polled input). Picks the controller readState
// uses: the first connected one that has a gamepad mapping, so an unmapped device (a wheel, a virtual joystick, an
// unknown pad) in the first slot no longer hides a working controller. Logs every connected controller when the set
// changes ("[pad]" lines). On the first call it adds mappings GLFW lacks, plus gamecontrollerdb.txt from the game
// exe's folder or the file named by PS2X_GAMEPAD_DB, if present (SDL_GameControllerDB format).
void ps2PadHostUpdate();

// Key and controller bindings (readState loads them once). The file is PS2X_CONTROLS, or else controls.ini in the
// folder above the game exe (the launcher's folder). Without a file the defaults below apply. Format:
//   [keyboard]                      [controller]
//   cross = X, SPACE                cross = SOUTH
// One line per action: up down left right cross circle square triangle l1 r1 l2 r2 l3 r3 start select, and
// stick_up stick_down stick_left stick_right (a full push of the left stick), camera_up camera_down camera_left
// camera_right (a full push of the right stick, which turns a free camera). Up to four names per line; an empty
// line value unbinds the action; actions left out keep their defaults. Key names are raylib's KEY_ names without
// the prefix (A, 1, SPACE, LEFT_SHIFT, KP_0, F1...). Controller names: SOUTH EAST WEST NORTH (face buttons by
// position), DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT, L1 R1 L2 R2 L3 R3, START, SELECT. The analog sticks always
// drive the PS2 sticks; a pressed stick_ action overrides the left stick, a camera_ action the right stick.

#endif
