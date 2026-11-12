# Maximo-GTG

<img width="2559" height="1439" alt="github shot" src="https://github.com/user-attachments/assets/2f0f4e37-8f4a-40ad-8169-e8fdebcbb734" />


An unofficial native Windows port of **Maximo: Ghosts to Glory** (PAL, SLES-50703), built on your own PC from your own
disc. It uses static recompilation: the game's code is translated to C++ and compiled, with no emulator in between.

**This repository contains no game code or data, and no copy of the game.** You need your own legally obtained disc.
Never share the `generated`, `build` or `dist` folders: they contain the game's code.

## Get started

1. Download `MaximoLauncher.zip` from the [Releases page](https://github.com/BioRhythmicStudios/Maximo-GTG/releases) and
   unzip it to a folder with a short path, such as `C:\Games\Maximo`. Keep the `art` folder beside the launcher.
2. Open `MaximoLauncher.exe` and press **Build**.
3. Choose your disc image. The launcher checks it against the PAL release, opens it read-only and never changes it.
4. When the build finishes, press **Play**.

The launcher downloads what it needs: the source code, the libraries, FFmpeg for faster movies, and Git if your PC doesn't
have it. If your PC has no C++ compiler, it offers to install Microsoft's free C++ Build Tools, and Windows will ask for your
permission. The game itself is never downloaded: it is compiled on your PC from your own disc, which can take a while.

## If Windows or your antivirus warns you

The launcher is unsigned and new, so some security tools are cautious about it. Its source is in this repository, and it
is built with the C# compiler that ships with Windows.

- **SmartScreen** may say Windows protected your PC. Choose **More info**, then **Run anyway**, if you trust this download.
- **Antivirus:** a few scanners may flag the launcher through general rules, because it downloads tools and starts a build.
  Only download it from the Releases page above. If you are unsure, read the source or build it yourself.
- **Smart App Control** blocks unsigned programs, so it stops the launcher, and probably the game built on your PC. Find it
  under Windows Security, App & browser control. Read Microsoft's notes before you change it, because it may be hard to
  turn back on. Your PC is your call: if you would rather keep it on, this port won't run for now.

## What you need

- Windows 10 or 11, 64-bit, and a CPU with AVX2
- A graphics card with Vulkan
- A disc image of the PAL release of Maximo: Ghosts to Glory (`.iso`)
- About 4 GB of free disk space while the game builds, plus about 8 GB if the C++ Build Tools need installing.
  Afterwards the build files are removed: the game takes about 60 MB, next to the 2.2 GB of files from your disc.
- Optional: the HD texture pack download is 9.7 GB.

To update the game, open **Game Files** and press **Update and rebuild the game**.

## Build it by hand

You need [Git](https://git-scm.com/) and Visual Studio with **Desktop development with C++**, including **C++ CMake tools
for Windows**. Visual Studio 2026 is the tested version.

```bat
git clone https://github.com/BioRhythmicStudios/Maximo-GTG.git
cd Maximo-GTG
build.cmd "C:\path\to\your\Maximo.iso"
```

Then open `dist\MaximoLauncher.exe`. Add `--no-ffmpeg` to skip FFmpeg; the game then uses its own movie decoder.

## How it works

1. `tools\kit\maximo_kit.cpp` reads the game's executable from your disc and verifies it.
2. PS2Recomp recompiles the executable using the function map in `data\funcs.csv`.
3. The VU1 microcode is assembled from your executable by `data\vu1\recipe.txt` and compiled ahead of time.
4. Everything is compiled against the runtime in `external\PS2Recomp` and the paraLLEl-GS renderer in
   `external\parallel-gs`.

## In the game

| Key | Action |
| --- | --- |
| WASD or arrow keys | D-pad |
| X or Space, C, Z, V | Cross, Circle, Square, Triangle |
| Q, E, Left Shift, Right Shift | L1, R1, L2, R2 |
| Enter, Tab | Start, Select |
| I, J, K, L | Turn the free camera (right stick) |
| R | R3: free camera back behind Maximo |
| F12 | Screenshot for the launcher's banner |
| Alt+Enter | Fullscreen or window |
| Esc | Quit |

Controllers work too: Xbox, PlayStation (DualSense, DualShock 4) and most others. The game uses the first one it
recognises, and the keyboard keeps working alongside it. The launcher's banner is built from your own disc and your F12
screenshots, on your PC only.

### Changing the controls

Open **Controls** in the launcher and press **Change controls...** to set two keys and a controller button for each PS2
button. You can also bind keys or buttons that push the left stick fully, or the right stick (the free camera). Esc, F12 and Alt+Enter stay with the game
window.

The launcher saves your choice in `controls.ini` beside it, and the game reads that file itself. With an older launcher
you can write the file by hand:

```ini
[keyboard]
cross = X, SPACE
stick_up = W
[controller]
cross = SOUTH
```

Each line is one PS2 button (`up down left right cross circle square triangle l1 r1 l2 r2 l3 r3 start select`) or a full
left-stick push (`stick_up stick_down stick_left stick_right`) or right-stick push (`camera_up camera_down camera_left
camera_right`). Keys use their position on a US keyboard (`A`, `1`,
`SPACE`, `LEFT_SHIFT`, `KP_0`, `F1`...). Controller buttons are `SOUTH EAST WEST NORTH` (by position), `DPAD_UP`,
`DPAD_DOWN`, `DPAD_LEFT`, `DPAD_RIGHT`, `L1 R1 L2 R2 L3 R3`, `START` and `SELECT`. Lines you leave out keep their
defaults. Delete the file to go back to the defaults.

A controller the game doesn't recognise: put a `gamecontrollerdb.txt` (from the SDL_GameControllerDB project) in the
`game` folder. Each session's `stdout.txt` in `logs` lists the controllers the game found, in lines starting `[pad]`.

## HD textures

The launcher can install Marcus' Ultimate Remaster texture pack from archive.org (9.7 GB), or your own PCSX2-format pack.

## Credits

- **Biorhythmic Studios:** port
- **Santins:** testing
- **Marcus of "Maximo Capcom Videos":** owner and creator of the HD textures
- Built with [PS2Recomp](https://github.com/ran-j/PS2Recomp),
  [paraLLEl-GS](https://github.com/Arntzen-Software/parallel-gs) and the projects in `THIRD_PARTY_NOTICES.txt`

## Legal

Unofficial fan project. Not affiliated with or endorsed by Capcom. All rights to Maximo: Ghosts to Glory are reserved to
Capcom.

The code in this repository is licensed under the GNU General Public License v3.0, see `LICENSE`. The launcher's art is
by Biorhythmic Studios and is included under the same licence.
