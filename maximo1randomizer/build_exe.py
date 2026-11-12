"""
Build a single-file Windows .exe of the Maximo Randomizer GUI using PyInstaller.

Run:    python build_exe.py

Outputs:
    dist/MaximoRandomizer.exe  (single-file, no Python needed to run)

The build uses --onefile + --windowed so the .exe contains the full Python
runtime + tkinter + the randomizer module, and launches with NO console window.
"""
from __future__ import annotations
import shutil
import struct
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
APP_NAME = "maximo_randomizer_v7.3"
ENTRY = HERE / "randomizer_gui.py"
VERSION_FILE = HERE / "version_info.txt"
APP_IMAGE_DIR = HERE / "app image"
LOGO_DIR = HERE / "logo"
BACKGROUND_DIR = HERE / "background"


def _first_image(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"):
            return p
    return None


def make_icon() -> Path | None:
    """Build a Windows .ico from the app PNG. Prefer Pillow (proper multi-size
    BMP entries that Explorer + PyInstaller render correctly); fall back to a
    raw PNG-in-ICO wrapper if Pillow isn't installed."""
    APP_PNG = _first_image(APP_IMAGE_DIR)
    if not APP_PNG:
        print(f"NOTE: app icon PNG not found in ({APP_IMAGE_DIR}); building without icon.")
        return None
    ico = HERE / "app.ico"
    try:
        from PIL import Image
        img = Image.open(APP_PNG).convert("RGBA")
        # Pad to a square canvas (transparent letterbox) BEFORE generating the
        # multi-size ICO. Pillow's Image.save(..., format="ICO", sizes=[...])
        # resizes to each target size without preserving aspect ratio, so a
        # non-square source (this app image is 294x355) gets stretched --
        # squishing the artwork enough that the icon doesn't read as "the app
        # icon" at a glance (taskbar/Alt-Tab sizes are small, where distortion
        # is most obvious). Padding to square first means every generated
        # size is a clean, undistorted scale-down instead.
        side = max(img.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                 (128, 128), (256, 256)]
        square.save(ico, format="ICO", sizes=sizes)
        print(f"Generated app icon: {ico.name} (Pillow, sizes up to 256, aspect-preserved)")
        return ico
    except ImportError:
        pass
    except Exception as e:
        print(f"NOTE: Pillow icon build failed ({e}); trying raw wrapper.")
    # Fallback: wrap the PNG bytes directly into an .ico (Vista+ reads PNG).
    png = APP_PNG.read_bytes()
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        print("NOTE: app image is not a valid PNG; building without icon.")
        return None
    w, h = struct.unpack(">II", png[16:24])
    if w > 256 or h > 256:
        print(f"NOTE: app PNG is {w}x{h} (>256); building without icon.")
        return None
    bw = 0 if w == 256 else w
    bh = 0 if h == 256 else h
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", bw, bh, 0, 0, 1, 32, len(png), 22)
    ico.write_bytes(header + entry + png)
    print(f"Generated app icon: {ico.name} (raw PNG wrapper, {w}x{h})")
    return ico


def main() -> None:
    # Sanity checks
    if not ENTRY.exists():
        print(f"ERROR: entry point missing: {ENTRY}")
        sys.exit(1)

    # Print exactly which Python interpreter and PyInstaller version are
    # about to run the build. If you have more than one Python install on
    # this machine, `python build_exe.py` might not be using the one you
    # expect -- this line removes all doubt.
    print(f"Using interpreter: {sys.executable}")
    print(f"Python version:    {sys.version.split()[0]}")
    try:
        import PyInstaller
        print(f"PyInstaller version: {PyInstaller.__version__}")
    except ImportError:
        print("ERROR: PyInstaller is not installed for this interpreter.")
        print(f"       Run:  {sys.executable} -m pip install pyinstaller")
        sys.exit(1)

    # Verify every randomizer/*.py file we know about is present BEFORE
    # building, so a missing file fails loudly here instead of producing
    # a confusing "ModuleNotFoundError" inside the frozen exe later.
    randomizer_dir = HERE / "randomizer"
    required_modules = [
        "__init__", "cli", "gui", "items", "iso", "iso_patcher",
        "elf_patch", "psx", "bef", "catalog", "spawn_config", "assets",
    ]
    missing = [m for m in required_modules
               if not (randomizer_dir / f"{m}.py").exists()]
    if missing:
        print(f"ERROR: missing randomizer/{{{', '.join(missing)}}}.py "
              f"in {randomizer_dir} -- fix this before building.")
        sys.exit(1)
    print(f"Verified {len(required_modules)} randomizer modules present in {randomizer_dir}")

    icon = make_icon()

    # Clean any prior build artifacts
    for d in ("build", "dist", "__pycache__"):
        p = HERE / d
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    spec = HERE / f"{APP_NAME}.spec"
    if spec.exists():
        spec.unlink()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", APP_NAME,
        "--clean",
        "--noconfirm",
        # Hide PyInstaller's own console while building (not the resulting exe).
        "--log-level", "WARN",
        # Force-collect every submodule of the `randomizer` package.
        # PyInstaller's static import analysis can miss sibling submodules
        # pulled in via relative imports (e.g. `from .cli import ...` inside
        # randomizer/gui.py) in --onefile builds, which produces
        # "ModuleNotFoundError: No module named 'randomizer.cli'" at
        # runtime even though the build itself reports success. Explicitly
        # collecting the whole package guarantees every submodule (cli,
        # gui, items, iso, iso_patcher, elf_patch, psx, bef, catalog,
        # spawn_config) is bundled regardless of how it's imported.
        "--collect-submodules", "randomizer",
        "--hidden-import", "randomizer.cli",
        "--hidden-import", "randomizer.gui",
        "--hidden-import", "randomizer.items",
        "--hidden-import", "randomizer.iso",
        "--hidden-import", "randomizer.iso_patcher",
        "--hidden-import", "randomizer.elf_patch",
        "--hidden-import", "randomizer.psx",
        "--hidden-import", "randomizer.bef",
        "--hidden-import", "randomizer.catalog",
        "--hidden-import", "randomizer.spawn_config",
        "--hidden-import", "randomizer.assets",
        # Make sure PyInstaller resolves the `randomizer` package from
        # THIS project folder specifically, not some other `randomizer`
        # package that might exist elsewhere on sys.path.
        "--paths", str(HERE),
    ]
    if icon is not None:
        cmd += ["--icon", str(icon)]
    if VERSION_FILE.exists():
        cmd += ["--version-file", str(VERSION_FILE)]

    # Bundle the logo/background folders (and the generated app.ico) into the
    # frozen exe so randomizer/assets.py can find them at runtime via
    # sys._MEIPASS, regardless of where the user launches the exe from.
    # PyInstaller --add-data syntax is SRC<sep>DEST_DIR_IN_BUNDLE, sep is
    # ';' on Windows and ':' elsewhere.
    sep = ";" if sys.platform == "win32" else ":"
    if LOGO_DIR.is_dir() and _first_image(LOGO_DIR):
        cmd += ["--add-data", f"{LOGO_DIR}{sep}logo"]
    else:
        print(f"NOTE: no logo image found in ({LOGO_DIR}); GUI will run without a logo.")
    if BACKGROUND_DIR.is_dir() and _first_image(BACKGROUND_DIR):
        cmd += ["--add-data", f"{BACKGROUND_DIR}{sep}background"]
    else:
        print(f"NOTE: no background image found in ({BACKGROUND_DIR}); GUI will run without a background.")
    if icon is not None:
        cmd += ["--add-data", f"{icon}{sep}."]

    cmd.append(str(ENTRY))
    print(f"Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=HERE)
    if proc.returncode != 0:
        print(f"\nERROR: PyInstaller exited with code {proc.returncode}")
        sys.exit(proc.returncode)

    out = HERE / "dist" / f"{APP_NAME}.exe"
    if not out.exists():
        print(f"\nERROR: expected output {out} not found.")
        sys.exit(1)

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"\nBuild finished: {out}  ({size_mb:.1f} MB)")

    # Smoke test: actually launch the exe and confirm it doesn't die with an
    # immediate ImportError/ModuleNotFoundError. A --windowed GUI app has no
    # console, so we can't read its stdout normally -- instead we check that
    # the process is still alive after a couple seconds (a crash on import
    # exits almost instantly; a live GUI keeps running until closed).
    print("Smoke-testing the built exe...")
    try:
        proc = subprocess.Popen([str(out)], cwd=HERE)
        try:
            ret = proc.wait(timeout=3)
            print(f"\nERROR: the exe exited immediately with code {ret}.")
            print("       This usually means an import failed at startup "
                  "(e.g. ModuleNotFoundError). Re-run this script and check "
                  "the interpreter/PyInstaller lines above, and make sure no "
                  "old copy of the exe was left running.")
            sys.exit(1)
        except subprocess.TimeoutExpired:
            # Still running after 3s -- the GUI opened successfully. Close it.
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("Smoke test passed: exe launched and stayed running.")
    except OSError as e:
        print(f"NOTE: could not smoke-test the exe automatically ({e}). "
              "Launch it by hand to verify.")

    print(f"\nSUCCESS: {out}  ({size_mb:.1f} MB)")
    print(f"Distribute this single file. No Python install required to run it.")


if __name__ == "__main__":
    main()
