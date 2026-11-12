MAXIMO-GTG MODS - debug menu, survival mode, cross-world enemies
=================================================================

This package has only source code and the launcher. The game is built on your PC from
your own PAL disc (SLES-50703), like the normal Maximo-GTG build: nothing from the game
is in here.

You need (install once)
  - Windows 10/11 64-bit, a CPU with AVX2, a Vulkan graphics card
  - Git: https://git-scm.com/
  - Visual Studio Community with "Desktop development with C++"
    (tick "C++ CMake tools for Windows"): https://visualstudio.microsoft.com/
  - Python 3: https://www.python.org/  (for the "Cross-world enemies" option)
  - Your Maximo PAL .iso (or a folder with the disc's files), about 10 GB free

Build and play
  1. Unzip this package to a short path without spaces, e.g. C:\MaximoMods
  2. Open MaximoLauncher.exe and press BUILD.
  3. Choose your disc image. The build window shows the progress; the first build takes
     a good while.
  4. When it's done the launcher opens, ready to Play. Next time just open
     MaximoLauncher.exe again: it goes straight to the game.
  On the Controls page tick "Cross-world enemies" (survival needs it: it puts every enemy
  and boss into the boss arenas). The first Play with it takes a few minutes.

A newer package: unzip it over this folder and open MaximoLauncher.exe. It shows
UPDATE: press it and only what changed is recompiled (usually a few minutes; no disc
needed). An update to the base project asks for the disc and builds everything again.

Debug menu (in game)
  L1 + R1 + SELECT       open / close
  D-pad up/down          choose a line
  D-pad left/right       change the choice
  CROSS                  use the line (spawn, toggle, go to level...)
  CIRCLE                 close

SURVIVAL MODE (menu line under RELEASE MAXIMO, press CROSS)
  - Takes you to a random boss arena (Gravedigger, Bokor, Cadaver, Glutterscum, King),
    removes its boss and sends waves of enemies from the four sides of the arena.
  - Wave 1 has 2 enemies, one more each wave (12 at most); every 5th wave brings a boss.
  - 5 seconds between waves. Wave, enemies left, kills and best wave at the top.
  - You get the new-game inventory for the run; your own comes back when it ends.
  - Dying restarts the run in the same arena. Press CROSS on the line again to stop.
  - Enemies that get stuck are removed after 40 seconds without a kill.

Other cheats in the menu: free camera, god mode, no clip, infinite lives/koins/keys,
one hit kill, game speed, give ability, kill/despawn all enemies, enemy class, spawn.

If the build stops, send build_log.txt (next to the launcher).
If the game misbehaves, send the newest source\dist\logs\session-...\boot_log.txt.

Keep the source folder to yourself once built: it contains the game's code.
