"""
Tkinter GUI for the Maximo: Ghosts to Glory randomizer.

Two modes (tabs):
  - "Patch ISO": user just picks a Maximo ISO, hits Run, and the script
    extracts assets, randomizes them, and writes them back into the ISO.
    Backup is automatic.
  - "Folder mode": classic CLI behavior — pick a source folder of extracted
    PSX files and an output folder for randomized copies.

Usage:
  python -m randomizer.gui
  python randomizer_gui.py
"""
from __future__ import annotations
import io
import os
import random
import re
import sys
import threading
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from .cli import cmd_randomize
from .iso_patcher import patch_iso
from . import assets
from . import boss_inject


def _enable_dpi_awareness() -> None:
    """Tell Windows this process is DPI-aware BEFORE any Tk window exists."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _ui_scale(root: tk.Tk) -> float:
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        return 1.0
    return max(1.0, min(dpi / 96.0, 3.0))


def _load_image(path, max_size: tuple[int, int] | None = None):
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        return None
    try:
        from PIL import Image, ImageTk
        img = Image.open(path).convert("RGBA")
        if max_size is not None:
            img.thumbnail(max_size, Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except ImportError:
        try:
            return tk.PhotoImage(file=str(path))
        except tk.TclError:
            return None
    except Exception:
        return None


class _StreamToWidget(io.TextIOBase):
    def __init__(self, widget: tk.Text, root: tk.Tk):
        self.widget = widget
        self.root = root

    def write(self, s: str) -> int:
        if not s:
            return 0
        self.root.after(0, self._append, s)
        return len(s)

    def _append(self, s: str) -> None:
        try:
            self.widget.configure(state=tk.NORMAL)
            self.widget.insert(tk.END, s)
            self.widget.see(tk.END)
            self.widget.configure(state=tk.DISABLED)
        except Exception:
            pass

    def flush(self) -> None:
        pass


class _Args:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class ScrollableOptionsFrame(ttk.Frame):
    """A responsive container that ensures all options can be scrolled dynamically."""
    def __init__(self, container, bg_color, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        
        self.canvas = tk.Canvas(self, bg=bg_color, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas, style="MainViewport.TFrame")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.scrollable_frame.bind("<Enter>", self._bind_mousewheel)
        self.scrollable_frame.bind("<Leave>", self._unbind_mousewheel)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        if sys.platform == "win32":
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        elif sys.platform == "darwin":
            self.canvas.yview_scroll(int(-1 * event.delta), "units")

    def _bind_mousewheel(self, event):
        if sys.platform in ("win32", "darwin"):
            self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        else:
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _unbind_mousewheel(self, event):
        if sys.platform in ("win32", "darwin"):
            self.canvas.unbind_all("<MouseWheel>")
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")


class RandomizerApp:
    PAD = 8

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Maximo: Ghosts to Glory — Randomizer v10.0")
        self._scale = _ui_scale(root)
        try:
            root.tk.call("tk", "scaling", self._scale * 96.0 / 72.0)
        except tk.TclError:
            pass
        
        bw, bh = int(1040 * self._scale), int(880 * self._scale)
        root.geometry(f"{bw}x{bh}")
        root.minsize(int(940 * self._scale), int(680 * self._scale))

        if sys.platform == "win32":
            ico = assets.app_icon_path()
            if ico is not None:
                try:
                    root.iconbitmap(str(ico))
                except tk.TclError:
                    pass
                # iconbitmap() sets the title-bar icon, but PyInstaller
                # onefile builds have historically been unreliable about
                # propagating it to the TASKBAR/Alt-Tab icon on Windows (the
                # shell queries WM_GETICON, which iconbitmap doesn't always
                # populate correctly through Tcl/Tk's Windows glue).
                # root.iconphoto(True, ...) sets both the small and large
                # icon via a PhotoImage instead, which is the more reliable
                # mechanism for the taskbar specifically. Keep both calls --
                # iconbitmap for the title bar / window corner, iconphoto as
                # the robust fallback for taskbar/Alt-Tab.
                photo = _load_image(ico, max_size=(256, 256))
                if photo is not None:
                    try:
                        root.iconphoto(True, photo)
                        self._taskbar_icon_photo = photo  # keep a reference alive
                    except tk.TclError:
                        pass

        # State initialization
        cwd = Path.cwd()
        default_src = cwd / "game_files"
        default_out = cwd / "output"
        self.iso_var = tk.StringVar(value="")
        self.iso_output_var = tk.StringVar(value="")
        self.iso_backup_var = tk.BooleanVar(value=True)

        self.src_var = tk.StringVar(value=str(default_src) if default_src.is_dir() else "")
        self.out_var = tk.StringVar(value=str(default_out) if default_src.is_dir() else "")

        # Randomizer options
        self.seed_var = tk.StringVar(value="")
        self.items_var = tk.BooleanVar(value=True)
        self.chests_var = tk.BooleanVar(value=True)
        self.skills_var = tk.BooleanVar(value=True)
        self.columns_var = tk.BooleanVar(value=True)
        self.spawn_loc_var = tk.BooleanVar(value=False)
        self.exit_loc_var = tk.BooleanVar(value=False)
        self.swap_spawn_exit_var = tk.BooleanVar(value=False)
        self.gate_rando_var = tk.BooleanVar(value=False)
        self.gate_mode_var = tk.StringVar(value="isolated")
        self.damage_taken_var = tk.StringVar(value="normal")
        self.damage_dealt_var = tk.StringVar(value="normal")
        self.shield_cost_var = tk.StringVar(value="vanilla")
        self.life_cost_var = tk.StringVar(value="vanilla")
        self.perma_death_var = tk.BooleanVar(value=False)
        # DEBUG/RESEARCH switch, off by default: clears bDoCollision, so the
        # player falls through the map. Not a playable mode.
        self.disable_collision_var = tk.BooleanVar(value=False)
        # Level time limit, EXPERIMENTAL. "off" = off, otherwise one of the
        # hard/medium/easy difficulty presets, each carrying PER-WORLD durations
        # before Maximo is killed; the clock re-arms per attempt (every level
        # entry AND respawn). Left OUT of _harder_lock on purpose — not a damage
        # setting.
        self.level_timer_var = tk.StringVar(value="off")
        # On-screen countdown for the level time limit (default on). Only has an
        # effect when level_timer_var is set.
        self.timer_display_var = tk.BooleanVar(value=True)
        # Widescreen (16:9). Presentation only, so it is left OUT of
        # _harder_lock on purpose.
        self.widescreen_var = tk.BooleanVar(value=False)
        # On-screen object budget, EXPERIMENTAL. "vanilla" = off, "more" raises
        # the script VM's per-type load-class weight limit so more of a level's
        # own objects/enemies can be active at once. Left OUT of _harder_lock on
        # purpose — it is a density/performance setting, not a damage one.
        # ALWAYS "more", AND THERE IS NO CONTROL FOR IT ANY MORE. The raised
        # on-screen object budget is a standard part of a build at the user's
        # direction. The var is kept (rather than the keyword hardcoded at the
        # call sites) so `settings_profile.collect_vars` still round-trips it and
        # so there is one place to read the value from.
        self.object_budget_var = tk.StringVar(value="more")
        self.sword_cost_var = tk.StringVar(value="vanilla")
        # Status-effect durations. Three independent timers; the first two each
        # cover a COUPLED pair (one shared constant in the executable).
        self.powerup_duration_var = tk.StringVar(value="vanilla")
        self.wizard_duration_var = tk.StringVar(value="vanilla")
        self.frozen_duration_var = tk.StringVar(value="vanilla")
        self.start_gold_var = tk.StringVar(value="")
        self.start_lives_var = tk.StringVar(value="")
        self.start_keys_var = tk.StringVar(value="")
        self.start_deathcoins_var = tk.StringVar(value="")
        self.start_souls_var = tk.StringVar(value="")
        self.start_armor_var = tk.StringVar(value="")
        self.start_shields_var = tk.StringVar(value="")
        self.start_boxers_var = tk.StringVar(value="")
        self.sword_enchant_var = tk.StringVar(value="None")
        self.elemental_shield_var = tk.StringVar(value="None")
        self.randomize_start_inv_var = tk.BooleanVar(value=False)
        
        self.skill_vars = {k: tk.BooleanVar(value=False) for k in (
            "sword720", "double_slash", "mighty_blow", "masquerade", "sword_power",
            "projectile", "return_shield", "hover_shield", "increase_armor",
            "wide_shockwave", "damage_shockwave", "find_treasure", "smart_bomb",
            "increase_throw", "foot_cheese")}
        self.harder_mode_var = tk.BooleanVar(value=False)
        self.preserve_chests_var = tk.BooleanVar(value=False)
        self.preserve_iron_keys_var = tk.BooleanVar(value=False)
        self.randomize_levels_var = tk.BooleanVar(value=False)
        self.enemies_var = tk.BooleanVar(value=True)
        self.dup_bosses_var = tk.BooleanVar(value=False)
        # BOSS-ARENA ENEMY ARRIVALS. DEFAULT OFF, DELIBERATELY: the arena route
        # costs are PREDICTED (no measured row into any `_B` archive) and the
        # arena budget has NEVER been observed on console, so this is an
        # experiment the user has to opt into. It IS a real user-facing option,
        # so it is a `tk.BooleanVar` and `settings_profile.collect_vars` will
        # save it; an older profile simply has no key for it and it stays False.
        self.boss_arena_arrivals_var = tk.BooleanVar(value=False)
        # HOW MANY arrivals each arena gets: easy 3 / normal 6 / hard 9. A
        # `tk.StringVar` so `settings_profile.collect_vars` saves it; an older
        # profile has no key for it and it stays at the default.
        from .boss_arena import ARENA_DIFFICULTY_DEFAULT as _ARENA_DIFF_DEFAULT
        self.boss_arena_difficulty_var = tk.StringVar(
            value=_ARENA_DIFF_DEFAULT)
        # NO GUI STATE for cross-world enemies or cross-world structures any
        # more, and no checkbutton for either: BOTH ARE NOW ALWAYS ON. The
        # keywords are still threaded EXPLICITLY at BOTH the `_Args` and
        # `patch_iso` sites below, as the literals `cross_world=True` /
        # `cross_world_structures=True` — `_Args` has no argparse defaults, so an
        # omission is a feature that silently never runs, which is exactly how
        # `cross_world_structures` was lost once before.
        #
        # Enemy permission is NOT a flat enemy set: it comes from
        # catalog.CROSS_WORLD_ALLOWLIST via catalog.allowlist_lookup, keyed on
        # (enemy id, source world, target world). Only allowlisted routes are
        # ported; everything else is skipped and named in the run log. Ported
        # structures are COSMETIC: progression and puzzle props are excluded.
        # WHICH routes arrive is still chosen per row in the spawn-rate editor,
        # and every structure row now ships ENABLED, so a stock build ports into
        # all five worlds. `--cross-world` / `--cross-world-structures` remain
        # the CLI form and are the only way to turn either feature OFF.
        #
        # The tk.BooleanVars were REMOVED rather than pinned to True on purpose:
        # `settings_profile.collect_vars` introspects every tk.Variable on the
        # app, so a pinned var would be saved into a profile and a profile
        # written before this change would restore False and silently switch a
        # now-always-on feature back off.

        # NO GUI STATE for the draw-budget occupancy ceiling or the RESOURCE pool
        # size any more. BOTH are now MEASURED MAXIMA with nothing left to
        # explore -- occupancy bracketed at 93.0 LOADS / 93.5 CRASHES, pool size
        # bracketed at 0x800000 LOADS / 0x880000 HANGS -- so the only thing a
        # control could still offer a GUI user is a crash. The occupancy a build
        # actually DRAWS to is a separate, lower figure (`DEFAULT_MAX_PCT`,
        # 90.0), set for testing; lowering can only ship a smaller lineup. Both
        # are passed as `None` ("the default") at BOTH the `_Args` and `patch_iso`
        # sites below, and both CLI options remain for future exploration.

        # GraveDigger injection has NO GUI STATE and NO checkbutton any more: it
        # is ALWAYS OFF from the GUI, passed as the literal `inject_boss=False`
        # at BOTH construction sites below. `_Args` is a bare attribute bag with
        # NO argparse defaults, so the keyword still has to be written out at
        # every site. Its recorded risk is `boss_inject.EARLY_EXIT_RISK_TEXT`:
        # defeating the injected boss can end the level early. `boss_inject.py`
        # and `--inject-boss` are untouched — the CLI can still ask for it.

        self.boss_clones_grave_var = tk.IntVar(value=1)
        self.boss_clones_swamp_var = tk.IntVar(value=1)
        self.boss_clones_ice_var = tk.IntVar(value=1)
        self.boss_clones_under_var = tk.IntVar(value=1)
        self.boss_clones_castle_var = tk.IntVar(value=1)
        self.no_dark_knight_var = tk.BooleanVar(value=False)

        from .spawn_config import DEFAULT_CONFIG_FILENAME
        self.spawn_config_path_var = tk.StringVar(value="")
        _existing_cfg = cwd / DEFAULT_CONFIG_FILENAME
        if _existing_cfg.is_file():
            self.spawn_config_path_var.set(str(_existing_cfg))

        from .cli import ALL_WORLDS
        self._all_worlds = ALL_WORLDS
        self.all_worlds_var = tk.BooleanVar(value=True)
        self.world_vars = {w: tk.BooleanVar(value=True) for w in ALL_WORLDS}

        # Settings-profile controls. These are NOT randomizer settings, so
        # settings_profile.EXCLUDED_ATTRS keeps them out of saved profiles.
        from . import settings_profile
        _ptr = settings_profile.read_pointer()
        self.profile_slot_var = tk.StringVar(value=_ptr["last_slot"] or "")
        self.profile_autoload_var = tk.BooleanVar(value=_ptr["auto_load"])

        self._running = False
        self._setup_styles()
        self._build_ui()

        # Auto-load runs LAST: every var and every widget exists by now, and a
        # corrupt profile can never stop the window from opening.
        self._autoload_profile()

    def _setup_styles(self) -> None:
        """Configure a crisp, highly accessible professional light theme for maximum readability."""
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        bg_main = "#f8fafc"      # Slate-50
        bg_card = "#ffffff"      # Pure White
        fg_main = "#0f172a"      # Slate-900
        fg_muted = "#475569"     # Slate-600
        accent = "#2563eb"       # Blue-600
        accent_hover = "#1d4ed8" # Blue-700
        accent_dark = "#1e3a8a"  # Blue-900
        border_color = "#cbd5e1" # Slate-300

        self.root.configure(bg=bg_main)

        self.style.configure(".", background=bg_main, foreground=fg_main, font=("Segoe UI", 10))
        self.style.configure("TFrame", background=bg_main)
        self.style.configure("MainViewport.TFrame", background=bg_main)
        
        self.style.configure("TNotebook", background=bg_main, borderwidth=0)
        self.style.configure("TNotebook.Tab", background="#e2e8f0", foreground=fg_muted, padding=[20, 8], borderwidth=1, bordercolor=border_color)
        self.style.map("TNotebook.Tab", 
                       background=[("selected", bg_card), ("active", "#f1f5f9")], 
                       foreground=[("selected", accent), ("active", fg_main)])

        self.style.configure("TLabelframe", background=bg_main, bordercolor=border_color, borderwidth=1)
        self.style.configure("TLabelframe.Label", background=bg_main, foreground=accent_dark, font=("Segoe UI", 11, "bold"))

        self.style.configure("TEntry", fieldbackground=bg_card, bordercolor=border_color, foreground=fg_main, insertcolor=fg_main)
        
        self.style.configure("TCombobox", fieldbackground=bg_card, background="#f1f5f9", bordercolor=border_color, foreground=fg_main, seqcolor=fg_main)
        self.style.map("TCombobox", 
                       fieldbackground=[("readonly", bg_card)], 
                       foreground=[("readonly", fg_main)])
                       
        self.style.configure("TSpinbox", fieldbackground=bg_card, background="#f1f5f9", bordercolor=border_color, foreground=fg_main)
        self.style.map("TSpinbox", fieldbackground=[("readonly", bg_card)], foreground=[("readonly", fg_main)])

        self.style.configure("TButton", background=bg_card, foreground=fg_main, bordercolor=border_color, padding=[14, 6], anchor="center")
        self.style.map("TButton", background=[("active", "#f1f5f9"), ("disabled", "#e2e8f0")], foreground=[("disabled", "#94a3b8")])
        self.style.configure("Accent.TButton", background=accent, foreground="#ffffff", bordercolor=accent, font=("Segoe UI", 10, "bold"))
        self.style.map("Accent.TButton", background=[("active", accent_hover)], foreground=[("active", "#ffffff")])

        self.style.configure("TCheckbutton", background=bg_main, foreground=fg_main)
        self.style.map("TCheckbutton", background=[("active", bg_main)])
        self.style.configure("TRadiobutton", background=bg_main, foreground=fg_main)
        self.style.map("TRadiobutton", background=[("active", bg_main)])

    def _build_ui(self) -> None:
        top_region = ttk.Frame(self.root, padding=(self.PAD, self.PAD, self.PAD, 0))
        top_region.pack(side=tk.TOP, fill=tk.X)
        self._build_header(top_region)

        notebook_container = ttk.Frame(top_region)
        notebook_container.pack(fill=tk.X, pady=(self.PAD, 2))

        notebook = ttk.Notebook(notebook_container)
        notebook.pack(fill=tk.X)

        iso_tab = ttk.Frame(notebook, padding=self.PAD)
        notebook.add(iso_tab, text="  Patch ISO (Recommended)  ")
        self._build_iso_tab(iso_tab)

        folder_tab = ttk.Frame(notebook, padding=self.PAD)
        notebook.add(folder_tab, text="  Folder Mode (Advanced)  ")
        self._build_folder_tab(folder_tab)
        self._notebook = notebook

        bottom_region = ttk.Frame(self.root, padding=(self.PAD, 0, self.PAD, self.PAD))
        bottom_region.pack(side=tk.BOTTOM, fill=tk.X)
        
        self._build_actions_frame(bottom_region)
        self._build_log_frame(bottom_region)
        self._build_status_bar(bottom_region)

        middle_region = ttk.Frame(self.root, padding=(self.PAD, 4, self.PAD, 4))
        middle_region.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.scroll_viewport = ScrollableOptionsFrame(middle_region, bg_color="#f8fafc")
        self.scroll_viewport.pack(fill=tk.BOTH, expand=True)

        self._build_grid_options(self.scroll_viewport.scrollable_frame)

    def _build_header(self, parent: ttk.Frame) -> None:
        logo_path = assets.logo_path()
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 4))

        if logo_path is not None:
            # Bumped up from 50px tall / 300px wide -- at the old size the
            # logo occupied a small corner of the header, leaving a lot of
            # visibly empty space next to it. This is roughly 2.4x taller,
            # filling the header area properly.
            max_h = int(120 * self._scale)
            photo = _load_image(logo_path, max_size=(int(420 * self._scale), max_h))
            if photo is not None:
                self._logo_photo = photo
                ttk.Label(header, image=self._logo_photo).pack(side=tk.LEFT)
                # Credit label placed in the empty space toward the right of
                # the header (this is where the user circled in their
                # screenshot -- an empty area near the top-right, not
                # directly beside the logo). side=tk.RIGHT pushes it into
                # that space; the text itself stays left-justified rather
                # than centered or right-justified within its own label.
                ttk.Label(
                    header, text="by Santins", justify=tk.LEFT, anchor=tk.NW,
                    font=("Segoe UI", 14, "italic"), foreground="#475569"
                ).pack(side=tk.RIGHT, anchor=tk.N, padx=(0, 40), pady=(100, 0))
                return
        
        ttk.Label(header, text="MAXIMO: GHOSTS TO GLORY RANDOMIZER", font=("Segoe UI", 15, "bold"), foreground="#2563eb").pack(side=tk.LEFT)

    def _build_iso_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Maximo Disc Image:").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.iso_var, width=70).grid(row=0, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_iso, width=12).grid(row=0, column=2)

        ttk.Label(parent, text="Output (Optional):").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.iso_output_var, width=70).grid(row=1, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_iso_output, width=12).grid(row=1, column=2)

        ttk.Checkbutton(parent, text="Create asset backup safely (.backup) prior to in-place patching execution", variable=self.iso_backup_var).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(6, 2))

        info = "System Hint: Select a secure Maximo copy (.iso, .bin). If output entry path stays unconfigured, modifications overwrite source file layers directly."
        ttk.Label(parent, text=info, justify=tk.LEFT, foreground="#475569", font=("Segoe UI", 9)).grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))
        parent.columnconfigure(1, weight=1)

    def _build_folder_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Source Folder:").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.src_var, width=70).grid(row=0, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_src, width=12).grid(row=0, column=2)

        ttk.Label(parent, text="Output Folder:").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.out_var, width=70).grid(row=1, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_out, width=12).grid(row=1, column=2)
        parent.columnconfigure(1, weight=1)

    def _build_grid_options(self, parent: ttk.Frame) -> None:
        seed_row = ttk.Frame(parent)
        seed_row.pack(fill=tk.X, pady=(4, 10), padx=2)
        ttk.Label(seed_row, text="Seed Setup:", font=("Segoe UI", 11, "bold"), foreground="#1e3a8a").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(seed_row, textvariable=self.seed_var, width=18).pack(side=tk.LEFT, padx=4)
        ttk.Label(seed_row, text="(Leave blank for random generation layout mechanics)", foreground="#475569", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)
        ttk.Button(seed_row, text="Roll Random Seed", command=self._roll_seed).pack(side=tk.LEFT, padx=4)

        self._build_profile_frame(parent)

        grid_body = ttk.Frame(parent)
        grid_body.pack(fill=tk.BOTH, expand=True)

        self._harder_lock = []

        left_col = ttk.Frame(grid_body)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(2, 6))

        f_rando = ttk.Labelframe(left_col, text=" Elements to Randomize ", padding=10)
        f_rando.pack(fill=tk.X, pady=(0, 8))
        
        for text, var in [("Enemies", self.enemies_var), ("Items", self.items_var), 
                          ("Chests", self.chests_var), ("Abilities & Skills", self.skills_var), 
                          ("Structures", self.columns_var)]:
            cb = ttk.Checkbutton(f_rando, text=text, variable=var)
            cb.pack(anchor=tk.W, pady=3)
            self._harder_lock.append((cb, "normal"))

        # THE "Cross-world enemies" AND "Cross-world structures" CHECKBUTTONS
        # WERE HERE, and both are GONE along with their explanatory labels. BOTH
        # FEATURES ARE NOW ALWAYS ON: `cross_world=True` and
        # `cross_world_structures=True` are written as literals at BOTH the
        # `_Args` and `patch_iso` sites. WHICH routes arrive is still chosen per
        # row in the spawn-rate editor below, and structure rows now ship
        # ENABLED, so a stock build ports enemies AND structures into all five
        # worlds. `--cross-world` / `--cross-world-structures` are the only way
        # left to turn either off.

        # BOSS-ARENA ENEMY ARRIVALS sits in that same space, because it reads as
        # a cross-world feature: it is the one part of cross-world enemies that
        # is still opt-in. NOT added to `self._harder_lock` — it is not a
        # difficulty setting.
        _arena_cb = ttk.Checkbutton(
            f_rando,
            text="Cross-world enemies in boss arenas (EXPERIMENTAL)",
            variable=self.boss_arena_arrivals_var)
        _arena_cb.pack(anchor=tk.W, pady=(8, 0))
        # THE PROSE DESCRIPTION IS GONE, replaced by the three difficulty
        # sub-options it used to sit above. Just the three names: what each one
        # means numerically lives in `boss_arena.ARENA_DIFFICULTY_ARRIVALS` and
        # in `--boss-arena-difficulty --help`, so the counts are never restated
        # here and cannot drift out of step with the table.
        f_arena_diff = ttk.Frame(f_rando)
        f_arena_diff.pack(anchor=tk.W, padx=18, pady=(2, 2))
        for _label in ("easy", "normal", "hard"):
            ttk.Radiobutton(
                f_arena_diff,
                text=_label.capitalize(),
                value=_label,
                variable=self.boss_arena_difficulty_var).pack(anchor=tk.W)

        # THE POOL OCCUPANCY COMBOBOX WAS HERE, and is gone. 93.0% is a SETTLED
        # MEASUREMENT with nothing left to explore: 95 CRASHED, 94 CRASHED,
        # 93.5 CRASHED, 93 LOADED, so the only unexplored sliver is 93.0..93.5
        # and `resolve_max_pct` refuses 93.5 and above. A control over a settled
        # maximum can only lower the budget or hand the user a refusal, so it
        # went away. What a build DRAWS to by default is a different figure,
        # `DEFAULT_MAX_PCT` = 90.0, deliberately below the measurement and set
        # for testing. `--pool-occupancy` still exists and can still ask for
        # anything up to (not including) 93.5, 93.0 included.
        #
        # THE RESOURCE POOL SIZE COMBOBOX WAS HERE TOO, and is gone for the same
        # reason: the size was bisected on console to 0x800000 LOADS / 0x880000
        # HANGS, so 0x800000 is the measured default the build applies by itself
        # and 0x880000 and above is refused outright. `--resource-pool` still
        # exists for future exploration.

        # THE SPAWN RATE EDITOR. The two separate arrivals editors are GONE:
        # native spawn weights, arriving enemies and arriving structures are one
        # table on one scale, edited here — under the OLD spawn-rate naming,
        # because this is the one surface that edits spawn rates. This button is
        # NEVER greyed: the native half of the table is always worth editing.
        unified_row = ttk.Frame(f_rando)
        unified_row.pack(fill=tk.X, padx=18, pady=(2, 2))
        self._unified_edit_btn = ttk.Button(
            unified_row, text="Edit Spawn Rates...",
            command=self._open_unified_editor)
        self._unified_edit_btn.pack(side=tk.LEFT)
        self._unified_clear_btn = ttk.Button(
            unified_row, text="Use Stock Rates",
            command=self._clear_unified_config)
        self._unified_clear_btn.pack(side=tk.LEFT, padx=6)
        self._unified_status = ttk.Label(f_rando, text="", foreground="#475569",
                                         font=("Segoe UI", 9), wraplength=260,
                                         justify=tk.LEFT)
        self._unified_status.pack(anchor=tk.W, padx=18)
        self._sync_unified_btn()

        # THE "Inject GraveDigger into a grave level" CHECKBUTTON WAS HERE, with
        # `boss_inject.EARLY_EXIT_RISK_TEXT` underneath it, and both are GONE.
        # Injection is now ALWAYS OFF from the GUI (`inject_boss=False`, written
        # as a literal at BOTH sites). The recorded reason it is off rather than
        # on is that risk text: defeating the injected boss can end the level
        # early (`Early_Exit_Risk`). `--inject-boss` still exists.

        f_worlds = ttk.Labelframe(left_col, text=" Included Map Regions ", padding=10)
        f_worlds.pack(fill=tk.X, pady=4)
        
        cb_aw = ttk.Checkbutton(f_worlds, text="All Available Worlds Included", variable=self.all_worlds_var, command=self._on_all_worlds_toggle)
        cb_aw.pack(anchor=tk.W, pady=4)
        self._harder_lock.append((cb_aw, "normal"))
        
        for w in self._all_worlds:
            wcb = ttk.Checkbutton(f_worlds, text=w.capitalize(), variable=self.world_vars[w], command=self._on_world_toggle)
            wcb.pack(anchor=tk.W, pady=3)
            self._harder_lock.append((wcb, "normal"))

        right_col = ttk.Frame(grid_body)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(6, 2))

        f_gameplay = ttk.Labelframe(right_col, text=" Combat & Balancing Parameters ", padding=10)
        f_gameplay.pack(fill=tk.X, pady=(0, 8))
        
        row_dt = ttk.Frame(f_gameplay)
        row_dt.pack(fill=tk.X, pady=4)
        ttk.Label(row_dt, text="Damage Taken", width=18).pack(side=tk.LEFT)
        cb_dt = ttk.Combobox(row_dt, textvariable=self.damage_taken_var, width=14, state="readonly", values=["0.25", "0.50", "normal", "2x", "4x"])
        cb_dt.pack(side=tk.LEFT)
        
        row_dd = ttk.Frame(f_gameplay)
        row_dd.pack(fill=tk.X, pady=4)
        ttk.Label(row_dd, text="Damage Dealt", width=18).pack(side=tk.LEFT)
        cb_dd = ttk.Combobox(row_dd, textvariable=self.damage_dealt_var, width=14, state="readonly", values=["0.25", "0.50", "normal", "2x", "4x"])
        cb_dd.pack(side=tk.LEFT)

        # Stays enabled under Harder Mode by design — NOT added to _harder_lock.
        row_sc = ttk.Frame(f_gameplay)
        row_sc.pack(fill=tk.X, pady=4)
        ttk.Label(row_sc, text="Shield Cost", width=18).pack(side=tk.LEFT)
        cb_sc = ttk.Combobox(row_sc, textvariable=self.shield_cost_var, width=14, state="readonly", values=["vanilla", "none", "light", "medium", "heavy"])
        cb_sc.pack(side=tk.LEFT)

        # Lives spent per death. Also stays enabled under Harder Mode — NOT
        # added to _harder_lock. A new game starts with 4 lives, so 5+ means the
        # first death ends the run.
        row_lc = ttk.Frame(f_gameplay)
        row_lc.pack(fill=tk.X, pady=4)
        ttk.Label(row_lc, text="Lives Per Death", width=18).pack(side=tk.LEFT)
        cb_lc = ttk.Combobox(row_lc, textvariable=self.life_cost_var, width=14, state="readonly", values=["vanilla", "none", "double", "triple", "quadruple"])
        cb_lc.pack(side=tk.LEFT)

        # Also stays enabled under Harder Mode — NOT added to _harder_lock.
        row_swc = ttk.Frame(f_gameplay)
        row_swc.pack(fill=tk.X, pady=4)
        ttk.Label(row_swc, text="Sword Cost", width=18).pack(side=tk.LEFT)
        cb_swc = ttk.Combobox(row_swc, textvariable=self.sword_cost_var, width=14, state="readonly", values=["vanilla", "none", "light", "medium", "heavy"])
        cb_swc.pack(side=tk.LEFT)

        # Status-effect durations. All three stay enabled under Harder Mode —
        # NOT added to _harder_lock. "Power-Up Time" covers Golden Armor AND
        # Mask of Sorrow, "Wizard Form Time" covers Baby AND Old Man: each pair
        # shares one timer constant in the executable, so it cannot be split.
        row_pud = ttk.Frame(f_gameplay)
        row_pud.pack(fill=tk.X, pady=4)
        ttk.Label(row_pud, text="Power-Up Time", width=18).pack(side=tk.LEFT)
        # "easier" always favours the player, so the direction is INVERTED:
        # power-up (beneficial) easier = LONGER, wizard/frozen (harmful)
        # easier = SHORTER. "none" is 1 tick, never 0 (0 would be permanent).
        cb_pud = ttk.Combobox(row_pud, textvariable=self.powerup_duration_var, width=14, state="readonly", values=["vanilla", "none", "easier", "harder"])
        cb_pud.pack(side=tk.LEFT)

        row_wzd = ttk.Frame(f_gameplay)
        row_wzd.pack(fill=tk.X, pady=4)
        ttk.Label(row_wzd, text="Wizard Form Time", width=18).pack(side=tk.LEFT)
        cb_wzd = ttk.Combobox(row_wzd, textvariable=self.wizard_duration_var, width=14, state="readonly", values=["vanilla", "none", "easier", "harder"])
        cb_wzd.pack(side=tk.LEFT)

        row_frz = ttk.Frame(f_gameplay)
        row_frz.pack(fill=tk.X, pady=4)
        ttk.Label(row_frz, text="Frozen Time", width=18).pack(side=tk.LEFT)
        cb_frz = ttk.Combobox(row_frz, textvariable=self.frozen_duration_var, width=14, state="readonly", values=["vanilla", "none", "easier", "harder"])
        cb_frz.pack(side=tk.LEFT)

        # The "Koin Fee" spinbox was intentionally removed from the GUI. Nothing
        # is passed from here any more, so the executable keeps its vanilla
        # price; the --koin-fee CLI flag and patch_koin_fee remain available.

        self._startinv_btn = ttk.Button(f_gameplay, text="Starting Inventory Config...", command=self._open_start_inventory)
        self._startinv_btn.pack(anchor=tk.W, pady=6)
        self._startinv_summary = ttk.Label(f_gameplay, text="", foreground="#475569", font=("Segoe UI", 9))
        self._startinv_summary.pack(anchor=tk.W, padx=2)

        # THE SPAWN-RATE EDITOR LAUNCHER WAS HERE, and is gone with the editor.
        # There is now exactly ONE editor button in this window ("Edit Unified
        # Randomization...", in the Elements to Randomize group). Two editors
        # over overlapping state is what made the old surface wrong: after task 4
        # `cli.py` feeds `unified_cfg.native_weights(world, committed_routes=...)`
        # into `items.randomize_universal` and `spawn.universal_weights` is called
        # NOWHERE, so the old editor's Items and Structures tabs wrote
        # `config.items` / `config.structures`, which retyping no longer reads --
        # they were INERT. Its still-live sections (Mimics/Wizards, Enemy Levels,
        # Abilities & Skills) are folded into the unified editor as their own
        # sections, so no editing surface was lost.
        ttk.Label(f_gameplay,
                  text=("Spawn weights, arriving enemies, arriving structures, "
                        "mimics/wizards, enemy levels and ability drops are all "
                        "edited in the ONE 'Edit Spawn Rates...' editor in the "
                        "left column."),
                  foreground="#475569", font=("Segoe UI", 9),
                  wraplength=300, justify=tk.LEFT).pack(anchor=tk.W, padx=2,
                                                        pady=(6, 2))

        cb_hm = ttk.Checkbutton(f_gameplay, text="Harder Mode", variable=self.harder_mode_var, command=self._sync_harder_mode)
        cb_hm.pack(anchor=tk.W, pady=4)
        self._harder_lock.extend([
            (cb_dt, "readonly"), (cb_dd, "readonly"), (self._startinv_btn, "normal"),
        ])
        self._refresh_spawn_status()

        f_exp = ttk.Labelframe(right_col, text=" Custom Modifiers ", padding=10)
        f_exp.pack(fill=tk.X, pady=4)
        
        cb_sl = ttk.Checkbutton(f_exp, text="Randomize Maximo Entry Spawn Point", variable=self.spawn_loc_var)
        cb_sl.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_sl, "normal"))

        cb_el = ttk.Checkbutton(f_exp, text="Randomize Level Exit Location", variable=self.exit_loc_var)
        cb_el.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_el, "normal"))

        cb_ss = ttk.Checkbutton(f_exp, text="Swap Maximo Start With Level Exit", variable=self.swap_spawn_exit_var)
        cb_ss.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_ss, "normal"))
        ttk.Label(f_exp,
                  text="Starts Maximo at the level exit and moves the exit to the old start. "
                       "Also removes every gate so the level stays completable. Can soft lock the game.",
                  foreground="#475569", font=("Segoe UI", 9)).pack(anchor=tk.W, padx=18)

        cb_dk = ttk.Checkbutton(f_exp, text="Exclude Dark Knight Entity (Prevents softlocks)", variable=self.no_dark_knight_var)
        cb_dk.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_dk, "normal"))

        cb_db = ttk.Checkbutton(f_exp, text="Duplicate Bosses", variable=self.dup_bosses_var)
        cb_db.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_db, "normal"))

        clones_frame = ttk.Frame(f_exp)
        clones_frame.pack(anchor=tk.W, padx=18, pady=4)
        from .items import BOSS_CLONE_MAX
        gmax = BOSS_CLONE_MAX.get("grave", 4)
        smax = BOSS_CLONE_MAX.get("swamp", 6)
        imax = BOSS_CLONE_MAX.get("ice", 8)
        umax = BOSS_CLONE_MAX.get("under", 10)
        cmax = BOSS_CLONE_MAX.get("castle", 8)
        _clone_specs = [
            ("Grave", self.boss_clones_grave_var, gmax),
            ("Swamp", self.boss_clones_swamp_var, smax),
            ("Ice", self.boss_clones_ice_var, imax),
            ("Under", self.boss_clones_under_var, umax),
            ("Castle", self.boss_clones_castle_var, cmax),
        ]
        for _ci, (_lbl, _var, _mx) in enumerate(_clone_specs):
            _r, _c = _ci // 3, (_ci % 3) * 2
            ttk.Label(clones_frame, text=f"{_lbl}:").grid(row=_r, column=_c, sticky=tk.W, padx=(0, 2), pady=3)
            _sp = ttk.Spinbox(clones_frame, from_=1, to=_mx, width=5, textvariable=_var)
            _sp.grid(row=_r, column=_c + 1, sticky=tk.W, padx=(0, 10), pady=3)
            self._harder_lock.append((_sp, "normal"))

        cb_gate = ttk.Checkbutton(f_exp, text="Randomize Gates", variable=self.gate_rando_var, command=self._sync_gate_opts)
        cb_gate.pack(anchor=tk.W, pady=(5, 3))
        self._harder_lock.append((cb_gate, "normal"))

        self._gate_sub_frame = ttk.Frame(f_exp)
        self._gate_sub_frame.pack(anchor=tk.W, padx=18, pady=2)
        
        rb_iso = ttk.Radiobutton(self._gate_sub_frame, text="Just gates randomizer", variable=self.gate_mode_var, value="isolated")
        rb_iso.grid(row=0, column=0, sticky=tk.W, pady=2)
        rb_pool = ttk.Radiobutton(self._gate_sub_frame, text="Randomize and add gates to the randomizer pool", variable=self.gate_mode_var, value="pool")
        rb_pool.grid(row=1, column=0, sticky=tk.W, pady=2)
        
        self._harder_lock.extend([(rb_iso, "normal"), (rb_pool, "normal")])
        self._sync_gate_opts()

        cb_pc = ttk.Checkbutton(f_exp, text="Preserve Original Chest Coordinates (Contents only)", variable=self.preserve_chests_var)
        cb_pc.pack(anchor=tk.W, pady=3)
        cb_pk = ttk.Checkbutton(f_exp, text="Preserve Iron Keys", variable=self.preserve_iron_keys_var)
        cb_pk.pack(anchor=tk.W, pady=3)

        # The "Randomize Worlds" checkbutton was intentionally removed from the
        # GUI. self.randomize_levels_var stays declared (False default) and is
        # still passed to _Args/patch_iso, so the --randomize-levels CLI flag
        # keeps working.

        # Widescreen (16:9) — presentation only, a static port of the PCSX2
        # widescreen cheat (horizontal scale 1.0 -> 0.75, render aspect
        # 1.4 -> 1.866666). Deliberately NOT added to _harder_lock: it is a
        # presentation setting, not a difficulty one.
        ttk.Checkbutton(f_exp, text="Widescreen (16:9)",
                        variable=self.widescreen_var).pack(anchor=tk.W, pady=3)

        # THE "On-Screen Objects" COMBOBOX WAS HERE, and is gone. The raised
        # budget is now ALWAYS ON: `object_budget_var` is pinned to "more" at
        # construction, so every build raises the script VM's per-type load-class
        # weight limit (constructor default for classes 1-5 plus the per-level
        # enemy override the engine re-applies at every level load) and more of a
        # level's own objects and enemies can be active at once. Only
        # `randomizer.cli --object-budget vanilla` can turn it off now.

        # There is no "Cross-world enemies" toggle anywhere in this window any
        # more. The feature is ALWAYS ON; only `--cross-world` can turn it off.

        # PermaDeath. Stays enabled under Harder Mode — deliberately NOT added to
        # _harder_lock. Overrides any starting lives / death coins configured
        # elsewhere.
        cb_pd = ttk.Checkbutton(f_exp, text="Perma Death (No lifes and death koins)", variable=self.perma_death_var)
        cb_pd.pack(anchor=tk.W, pady=3)

        # Disable World Collision — DEBUG/RESEARCH switch, LAST in this group.
        # Clears the developers' own bDoCollision byte: the player and camera stop
        # colliding with the world, so Maximo falls through the map as soon as
        # gravity applies. Retail has no fly/noclip movement to compensate and a
        # soft-lock is likely. Stays enabled under Harder Mode — deliberately NOT
        # added to _harder_lock (it is not a difficulty setting at all).
       # cb_dc = ttk.Checkbutton(
           # f_exp,
            #text="Disable World Collision (debug — you will fall through the map)",
           # variable=self.disable_collision_var)
        #cb_dc.pack(anchor=tk.W, pady=3)

        f_challenge = ttk.Labelframe(right_col, text=" Challenge Mode ", padding=10)
        f_challenge.pack(fill=tk.X, pady=4)

        # Level Time Limit (EXPERIMENTAL). "off" = off. Stays enabled under
        # Harder Mode — deliberately NOT added to _harder_lock.
        f_lt = ttk.Frame(f_challenge)
        f_lt.pack(anchor=tk.W, pady=3)
        ttk.Label(f_lt, text="Level Time Limit", width=18).pack(side=tk.LEFT)
        ttk.Combobox(f_lt, width=12, state="readonly",
                     values=["off", "hard", "medium", "easy"],
                     textvariable=self.level_timer_var).pack(side=tk.LEFT)
        ttk.Label(f_lt, text="difficulty preset (per-world durations)").pack(
            side=tk.LEFT, padx=6)

        # Companion toggle for the row above: draw the remaining seconds on
        # screen. Only meaningful when the Level Time Limit is set. Like the
        # timer itself, deliberately OUT of _harder_lock.
        ttk.Checkbutton(f_challenge, text="Show Level Timer On Screen",
                        variable=self.timer_display_var).pack(anchor=tk.W,
                                                              pady=3)

        grid_body.columnconfigure(0, weight=1, uniform="equal_cols")
        grid_body.columnconfigure(1, weight=1, uniform="equal_cols")
        self._refresh_startinv_summary()
        self._sync_harder_mode()

    def _sync_gate_opts(self) -> None:
        if not hasattr(self, "_gate_sub_frame"): return
        state = "normal" if self.gate_rando_var.get() and not self.harder_mode_var.get() else "disabled"
        for child in self._gate_sub_frame.winfo_children():
            try: child.configure(state=state)
            except Exception: pass

    def _sync_harder_mode(self) -> None:
        locked = bool(self.harder_mode_var.get())
        for widget, enabled_state in getattr(self, "_harder_lock", []):
            try: widget.configure(state=("disabled" if locked else enabled_state))
            except tk.TclError: pass
        self._sync_gate_opts()

    def _level_timer_value(self) -> str | None:
        """Difficulty preset for the level time limit, or None when off.

        Delegates to `resolve_level_timer` — the ONE edge mapping shared with
        both argparse layers — so the accepted spellings can never drift. Never
        raises: "off", blank and anything unrecognised all mean feature off.
        """
        try:
            from .elf_patch import resolve_level_timer
            return resolve_level_timer(self.level_timer_var.get())
        except Exception:
            return None

    def _object_budget_value(self) -> int | None:
        """Object-budget value for patch_iso, or None when vanilla.

        Delegates to `resolve_object_budget` — the ONE edge mapping shared with
        both argparse layers — so the accepted spellings can never drift. Never
        raises: "vanilla", blank and anything unrecognised all mean feature off.
        """
        try:
            from .elf_patch import resolve_object_budget
            return resolve_object_budget(self.object_budget_var.get())
        except Exception:
            return None

    def _resource_pool_value(self):
        """Always None: "let the build apply its MEASURED default".

        The GUI combobox that fed this is gone. The pool size was walked on
        console and did NOT come out as a clean step: 0x880000 and 0x900000 HANG
        and have never loaded, 0x800000 LOADED once and HUNG once (UNRELIABLE),
        and 0x700000 has loaded and played consistently -- so the build's own
        cross-world default (`RESOURCE_POOL_RAISED`) is the only size the
        evidence supports and there is nothing useful left for a GUI user to
        choose. A retry of 0x800000 is a CLI decision, not a GUI one. Kept as a
        method, and still passed EXPLICITLY at both construction sites, because
        `_Args` has no argparse defaults and a dropped keyword silently disables
        a feature. `--resource-pool` still works for future exploration.
        """
        return None

    def _pool_occupancy_value(self):
        """Always None: "whatever `DEFAULT_MAX_PCT` is", currently 90.0%.

        Same story as `_resource_pool_value`: 93.0% (`LOADED_MAX_PCT`) is the
        highest occupancy OBSERVED TO LOAD, 93.5% and above is REFUSED, the
        remaining sliver is not worth a build, so the combobox went away. The
        DEFAULT a build draws to is a separate, LOWER figure (90.0%), set for
        testing; lowering can only ship a smaller lineup. None (not the float) is
        passed so every layer resolves the default in ONE place.
        """
        return None

    def _selected_skills(self) -> set[str]:
        return {k for k, v in self.skill_vars.items() if v.get()}

    def _sword_enchant_value(self) -> int | None:
        return {"None": None, "Fire": 1, "Ice": 2, "Sun": 3, "Armageddon": 4}.get(self.sword_enchant_var.get())

    def _elemental_shield_value(self) -> int | None:
        return {"None": None, "Wind": 1, "Magnetic": 2, "Lightning": 3}.get(self.elemental_shield_var.get())

    def _shield_cost_value(self) -> int | None:
        """Combobox preset label -> patch argument (an integer point cost).
        'vanilla', blank, or anything unrecognised means leave the game's own
        cost alone. Never raises."""
        try:
            from .elf_patch import resolve_shield_cost
            return resolve_shield_cost(self.shield_cost_var.get())
        except Exception:
            return None

    def _life_cost_value(self) -> int | None:
        """Combobox preset label -> patch argument (an integer count of lives
        spent per death). 'vanilla', blank, or anything unrecognised means leave
        the game's own cost alone. Never raises."""
        try:
            from .elf_patch import resolve_life_cost
            return resolve_life_cost(self.life_cost_var.get())
        except Exception:
            return None

    def _sword_cost_value(self) -> int | None:
        """Combobox preset label -> patch argument (an integer MULTIPLIER on each
        special move's vanilla point cost). 'vanilla', blank, or anything
        unrecognised means leave the game's own costs alone. Never raises."""
        try:
            from .elf_patch import resolve_sword_cost
            return resolve_sword_cost(self.sword_cost_var.get())
        except Exception:
            return None

    def _powerup_duration_value(self):
        """Combobox preset label -> patch argument (a MULTIPLIER on the Golden
        Armor / Mask of Sorrow timer, which is ONE shared constant). BENEFICIAL,
        so 'easier' is 2x and 'harder' is 0.5x — the resolver is given the site
        id so it picks that direction. 'none' is 1 tick. 'vanilla', blank, or
        anything unrecognised leaves the game's own timer alone. Never raises."""
        try:
            from .elf_patch import resolve_effect_duration
            return resolve_effect_duration("powerup",
                                          self.powerup_duration_var.get())
        except Exception:
            return None

    def _wizard_duration_value(self):
        """Combobox preset label -> patch argument (a MULTIPLIER on the Baby /
        Old Man form timer, which is ONE shared constant). HARMFUL, so 'easier'
        is 0.5x and 'harder' is 2x. 'none' is 1 tick. 'vanilla', blank, or
        anything unrecognised leaves the game's own timer alone. Never raises."""
        try:
            from .elf_patch import resolve_effect_duration
            return resolve_effect_duration("wizard",
                                          self.wizard_duration_var.get())
        except Exception:
            return None

    def _frozen_duration_value(self):
        """Combobox preset label -> patch argument (a MULTIPLIER on the Frozen
        ice timer). HARMFUL, so 'easier' is 0.5x and 'harder' is 2x. 'none' is
        1 tick. 'vanilla', blank, or anything unrecognised leaves the game's
        own timer alone. Never raises."""
        try:
            from .elf_patch import resolve_effect_duration
            return resolve_effect_duration("frozen",
                                          self.frozen_duration_var.get())
        except Exception:
            return None

    def _refresh_startinv_summary(self) -> None:
        if not hasattr(self, "_startinv_summary"): return
        if self.randomize_start_inv_var.get():
            txt = "Loadout Config Status: Randomized via Seed Layer"
        else:
            parts = []
            for label, var in (("gold", self.start_gold_var), ("lives", self.start_lives_var), ("keys", self.start_keys_var), ("death coins", self.start_deathcoins_var), ("souls", self.start_souls_var), ("armor", self.start_armor_var), ("shields", self.start_shields_var), ("boxers", self.start_boxers_var)):
                v = var.get().strip()
                if v: parts.append(f"{v} {label}")
            nsk = len(self._selected_skills())
            if nsk: parts.append(f"{nsk} skill{'s' if nsk != 1 else ''}")
            if self._sword_enchant_value() is not None: parts.append(f"{self.sword_enchant_var.get()} sword")
            if self._elemental_shield_value() is not None: parts.append(f"{self.elemental_shield_var.get()} shield")
            txt = f"Loadout Options Saved: {', '.join(parts)}" if parts else "Loadout Design Configuration: Standard Vanilla Defaults"
        self._startinv_summary.configure(text=txt)

    def _open_start_inventory(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Starting Loadout Layout Configuration")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg="#f8fafc")
        
        body = ttk.Frame(win, padding=16)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Configure Initial Currencies (Leave blank for game defaults):", font=("Segoe UI", 10, "bold"), foreground="#1e3a8a").grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 8))

        spins = []
        def add_spin(r, label, var, to):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky=tk.W, pady=3)
            sp = ttk.Spinbox(body, from_=0, to=to, width=10, textvariable=var)
            sp.grid(row=r, column=1, sticky=tk.W, padx=(4, 16), pady=3)
            spins.append(sp)
            return sp
            
        add_spin(1, "Initial Gold (Koins):", self.start_gold_var, 9999)
        add_spin(2, "Starting Lives:", self.start_lives_var, 99)
        add_spin(3, "Iron Keys:", self.start_keys_var, 9)
        add_spin(4, "Death Coins:", self.start_deathcoins_var, 99)
        add_spin(5, "Souls (Spirit):", self.start_souls_var, 999)
        add_spin(6, "Armor Segments:", self.start_armor_var, 4)
        add_spin(7, "Shields:", self.start_shields_var, 4)
        add_spin(8, "Boxers:", self.start_boxers_var, 3)

        boxers_hint_row = 9
        ttk.Label(body, text="Boxers picks which pair Maximo wears (0-3; the Collector sells Monogram, Pow and Cow). They are only visible while armor is 0 or 1, since that is when Maximo is stripped to his boxers. In the executable this only changes which texture is drawn, so any gameplay effect would come from level scripts rather than the game code.", foreground="#475569", font=("Segoe UI", 9), wraplength=460, justify=tk.LEFT).grid(row=boxers_hint_row, column=0, columnspan=4, sticky=tk.W, pady=(6, 0))

        hint_row = 10
        ttk.Label(body, text="Shields sets how many shield segments Maximo carries and shield points are set to 5x that to match; Armor is the armor-segment count (vanilla 2, max 4) and because this writes the new-game table value rather than going through the game's own armor-up path, a high starting armor is worth testing in-game.", foreground="#475569", font=("Segoe UI", 9), wraplength=460, justify=tk.LEFT).grid(row=hint_row, column=0, columnspan=4, sticky=tk.W, pady=(6, 0))

        sep1_row = hint_row + 1
        skills_hdr_row = sep1_row + 1
        skills_first_row = skills_hdr_row + 1
        ttk.Separator(body, orient="horizontal").grid(row=sep1_row, column=0, columnspan=4, sticky=tk.EW, pady=12)
        ttk.Label(body, text="Unlocked Passive Character Skills Matrix:", font=("Segoe UI", 10, "bold"), foreground="#1e3a8a").grid(row=skills_hdr_row, column=0, columnspan=4, sticky=tk.W, pady=(0, 6))
            
        skill_cbs = []
        _labels = [("sword720", "Sword 720 Spin"), ("double_slash", "Double Slash"), 
                   ("mighty_blow", "Mighty Blow"), ("masquerade", "Masquerade"), 
                   ("sword_power", "Sword Power"), ("projectile", "Magic Bolt"), 
                   ("return_shield", "Return Shield"), ("hover_shield", "Hover Shield"), 
                   ("increase_armor", "Increase Armor"), ("wide_shockwave", "Wide Shockwave"), 
                   ("damage_shockwave", "Damage Shockwave"), ("find_treasure", "Find Treasure"), 
                   ("smart_bomb", "Smart Bomb"), ("increase_throw", "Increase Throw"),
                   ("foot_cheese", "Foot Cheese (experimental)")]
        _cols = 3
        for i, (key, lbl) in enumerate(_labels):
            cb = ttk.Checkbutton(body, text=lbl, variable=self.skill_vars[key])
            cb.grid(row=skills_first_row + i // _cols, column=i % _cols, sticky=tk.W, padx=(4, 10), pady=3)
            skill_cbs.append(cb)

        sw_row = skills_first_row + (len(_labels) + _cols - 1) // _cols
        ttk.Label(body, text="Sword Modification:").grid(row=sw_row, column=0, sticky=tk.W, pady=(10, 4))
        sword_cb = ttk.Combobox(body, textvariable=self.sword_enchant_var, width=16, state="readonly", values=["None", "Fire", "Ice", "Sun", "Armageddon"])
        sword_cb.grid(row=sw_row, column=1, columnspan=2, sticky=tk.W, pady=(10, 4))

        sh_row = sw_row + 1
        ttk.Label(body, text="Shield Infusion:").grid(row=sh_row, column=0, sticky=tk.W, pady=4)
        shield_cb = ttk.Combobox(body, textvariable=self.elemental_shield_var, width=16, state="readonly", values=["None", "Wind", "Magnetic", "Lightning"])
        shield_cb.grid(row=sh_row, column=1, columnspan=2, sticky=tk.W, pady=4)

        sep_row = sh_row + 1
        ttk.Separator(body, orient="horizontal").grid(row=sep_row, column=0, columnspan=4, sticky=tk.EW, pady=12)

        def _sync_rand():
            dis = bool(self.randomize_start_inv_var.get())
            for w in spins + skill_cbs:
                try: w.configure(state=("disabled" if dis else "normal"))
                except Exception: pass
            try:
                sword_cb.configure(state=("disabled" if dis else "readonly"))
                shield_cb.configure(state=("disabled" if dis else "readonly"))
            except tk.TclError: pass

        rand_cb = ttk.Checkbutton(body, text="Randomize start equipment configurations dynamically via core logic seed", variable=self.randomize_start_inv_var, command=_sync_rand)
        rand_cb.grid(row=sep_row + 1, column=0, columnspan=4, sticky=tk.W, pady=2)

        btns = ttk.Frame(body)
        btns.grid(row=sep_row + 2, column=0, columnspan=4, sticky=tk.E, pady=(16, 0))

        def _clear():
            for var in (self.start_gold_var, self.start_lives_var, self.start_keys_var, self.start_deathcoins_var, self.start_souls_var, self.start_armor_var, self.start_shields_var, self.start_boxers_var): var.set("")
            for v in self.skill_vars.values(): v.set(False)
            self.sword_enchant_var.set("None")
            self.elemental_shield_var.set("None")
            self.randomize_start_inv_var.set(False)
            _sync_rand()

        ttk.Button(btns, text="Reset Fields", command=_clear).pack(side=tk.LEFT, padx=(0, 8))
        def _close():
            self._refresh_startinv_summary()
            win.destroy()
        ttk.Button(btns, text="Apply Changes", command=_close, style="Accent.TButton").pack(side=tk.LEFT)
        _sync_rand()
        win.protocol("WM_DELETE_WINDOW", _close)
        win.grab_set()

    # ------------------------------------------------------------------
    # Settings profiles (whole-GUI save / load / export / import / slots)
    # ------------------------------------------------------------------
    def _build_profile_frame(self, parent: ttk.Frame) -> None:
        f = ttk.Labelframe(parent, text=" Settings Profiles ", padding=10)
        f.pack(fill=tk.X, pady=(0, 8), padx=2)

        row = ttk.Frame(f)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Profile Slot:").pack(side=tk.LEFT, padx=(0, 6))
        self._profile_combo = ttk.Combobox(row, textvariable=self.profile_slot_var,
                                           width=24, state="readonly", values=())
        self._profile_combo.pack(side=tk.LEFT, padx=(0, 8))

        for text, cmd in (("Save", self._profile_save),
                          ("Save As...", self._profile_save_as),
                          ("Load", self._profile_load),
                          ("Delete", self._profile_delete),
                          ("Export...", self._profile_export),
                          ("Import...", self._profile_import)):
            ttk.Button(row, text=text, command=cmd).pack(side=tk.LEFT, padx=2)

        ttk.Checkbutton(f, text="Auto-load this profile on startup",
                        variable=self.profile_autoload_var,
                        command=self._profile_autoload_changed).pack(anchor=tk.W, pady=(6, 0))

        self._profile_status = ttk.Label(f, text="", foreground="#475569",
                                         font=("Segoe UI", 9))
        self._profile_status.pack(anchor=tk.W, padx=2)
        ttk.Label(f, text="A profile stores every option in this window, "
                          "including the ISO / folder paths and the seed, so "
                          "loading one restores those too. The spawn-rate "
                          "editor keeps its own file; only its path is stored.",
                  foreground="#475569", font=("Segoe UI", 9),
                  wraplength=880, justify=tk.LEFT).pack(anchor=tk.W, padx=2, pady=(2, 0))

        self._profile_last_summary = ""
        self._refresh_profile_slots()
        self._refresh_profile_status()
        # Deliberately NOT added to _harder_lock — these are not difficulty
        # settings and stay usable in Harder Mode.

    def _refresh_profile_slots(self) -> None:
        from . import settings_profile
        slots = settings_profile.list_slots()
        try:
            self._profile_combo.configure(values=slots)
        except tk.TclError:
            return
        if self.profile_slot_var.get() not in slots:
            self.profile_slot_var.set(slots[0] if slots else "")

    def _refresh_profile_status(self) -> None:
        if not hasattr(self, "_profile_status"):
            return
        slot = self.profile_slot_var.get().strip()
        active = f"Active Profile: {slot}" if slot else "No Profile Selected"
        summary = getattr(self, "_profile_last_summary", "")
        text = f"{active} — {summary}" if summary else active
        self._profile_status.configure(text=text)

    def _profile_autoload_changed(self) -> None:
        from . import settings_profile
        settings_profile.write_pointer(self.profile_slot_var.get().strip() or None,
                                       self.profile_autoload_var.get())
        self._refresh_profile_status()

    def _after_profile_load(self, res, source: str) -> None:
        """Refresh every dependent UI bit so no stale control is left on screen."""
        self._profile_last_summary = f"loaded {source}: {res.summary()}"
        for fn in (self._sync_harder_mode, self._sync_gate_opts,
                   self._refresh_startinv_summary, self._refresh_spawn_status):
            try:
                fn()
            except Exception as exc:
                print(f"[profiles] refresh step failed: {exc}")
        self._refresh_profile_status()

    def _profile_save(self) -> None:
        from . import settings_profile
        slot = self.profile_slot_var.get().strip()
        if not slot:
            self._profile_save_as()
            return
        try:
            settings_profile.save_slot(self, slot)
        except OSError as exc:
            messagebox.showerror("Settings Profiles", f"Could not save '{slot}':\n{exc}")
            return
        settings_profile.write_pointer(slot, self.profile_autoload_var.get())
        self._profile_last_summary = "saved"
        self._refresh_profile_slots()
        self.profile_slot_var.set(settings_profile.sanitize_slot_name(slot))
        self._refresh_profile_status()

    def _profile_save_as(self) -> None:
        from tkinter import simpledialog
        from . import settings_profile
        raw = simpledialog.askstring("Save Profile As",
                                     "Profile name:", parent=self.root)
        if raw is None:
            return
        slot = settings_profile.sanitize_slot_name(raw)
        if settings_profile.slot_path(slot).is_file():
            if not messagebox.askyesno("Settings Profiles",
                                       f"Profile '{slot}' already exists. Overwrite it?"):
                return
        self.profile_slot_var.set(slot)
        self._profile_save()

    def _profile_load(self) -> None:
        from . import settings_profile
        slot = self.profile_slot_var.get().strip()
        if not slot:
            messagebox.showinfo("Settings Profiles", "Pick a profile slot first.")
            return
        try:
            res = settings_profile.load_slot(self, slot)
        except settings_profile.ProfileError as exc:
            messagebox.showerror("Settings Profiles", str(exc))
            return
        settings_profile.write_pointer(slot, self.profile_autoload_var.get())
        self._after_profile_load(res, slot)

    def _profile_delete(self) -> None:
        from . import settings_profile
        slot = self.profile_slot_var.get().strip()
        if not slot:
            return
        if not messagebox.askyesno("Settings Profiles",
                                   f"Delete profile '{slot}'? This cannot be undone."):
            return
        settings_profile.delete_slot(slot)
        self._profile_last_summary = f"deleted '{slot}'"
        self.profile_slot_var.set("")
        self._refresh_profile_slots()
        self._refresh_profile_status()

    def _profile_export(self) -> None:
        from . import settings_profile
        slot = self.profile_slot_var.get().strip() or "profile"
        path = filedialog.asksaveasfilename(
            title="Export Settings Profile", defaultextension=".json",
            initialfile=f"{settings_profile.sanitize_slot_name(slot)}.json",
            filetypes=[("Profile JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            settings_profile.save_profile(self, path)
        except OSError as exc:
            messagebox.showerror("Settings Profiles", f"Could not export:\n{exc}")
            return
        self._profile_last_summary = f"exported to {Path(path).name}"
        self._refresh_profile_status()

    def _profile_import(self) -> None:
        from . import settings_profile
        path = filedialog.askopenfilename(
            title="Import Settings Profile",
            filetypes=[("Profile JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            res = settings_profile.load_profile(self, path)
        except settings_profile.ProfileError as exc:
            messagebox.showerror("Settings Profiles", str(exc))
            return
        self._after_profile_load(res, Path(path).name)

    def _autoload_profile(self) -> None:
        """Apply the auto-load slot on startup. Never raises."""
        try:
            from . import settings_profile
            slot, res = settings_profile.autoload(self)
            if res is not None:
                self.profile_slot_var.set(slot or "")
                self._after_profile_load(res, slot or "profile")
        except Exception as exc:
            print(f"[profiles] auto-load skipped: {exc}")

    def _refresh_spawn_status(self) -> None:
        """The config-file status line, now shown by the unified status label.

        Kept under its old name because `_after_profile_load` and
        `_clear_spawn_config` call it; the separate spawn-rate status label went
        with the spawn-rate editor, so there is one status line for one config
        file instead of two that could disagree.
        """
        self._refresh_unified_status()

    def _clear_spawn_config(self) -> None:
        self.spawn_config_path_var.set("")
        self._refresh_spawn_status()

    # ---- THE SPAWN RATE EDITOR -------------------------------------------
    def _sync_unified_btn(self) -> None:
        """Refresh the spawn-rate editor's status line.

        The button is NEVER greyed: this one surface carries the NATIVE spawn
        weights as well as the arriving routes, so it is always worth opening.
        """
        self._refresh_unified_status()

    def _refresh_unified_status(self) -> None:
        lbl = getattr(self, "_unified_status", None)
        if lbl is None:
            return
        # Both cross-world switches are ALWAYS ON now, so the status line states
        # that as a fact instead of reading two vars that no longer exist.
        arrivals = "enemies + structures arriving"
        path = self.spawn_config_path_var.get().strip()
        if path:
            lbl.configure(text=f"Custom spawn rates: {Path(path).name} "
                               f"({arrivals}).", foreground="#16a34a")
        else:
            lbl.configure(text=f"Using the stock spawn rates ({arrivals}).",
                          foreground="#475569")

    def _clear_unified_config(self) -> None:
        """'Use Stock Rates' for the whole table: drop the custom config path, exactly
        like the spawn editor's reset. Every weight falls back to defaults."""
        self.spawn_config_path_var.set("")
        self._refresh_spawn_status()
        self._refresh_unified_status()

    def _last_unified_records(self) -> dict:
        """The LAST BUILD's per-world figures, read off the log pane.

        The run already printed them (`unified_report.report_world`), and the log
        pane is the same source of truth `_log_has_cross_world_warning` uses, so
        no second message channel and no extra file is introduced. An empty dict
        means "no build in this session yet", which the tab says plainly rather
        than showing a zero that looks measured. Never raises.
        """
        try:
            text = self.log_text.get("1.0", tk.END)
        except Exception:
            return {}
        return parse_unified_log(text)

    def _open_unified_editor(self) -> None:
        from .spawn_config import (SpawnConfig, load_spawn_config,
                                   DEFAULT_CONFIG_FILENAME)
        path = self.spawn_config_path_var.get().strip()
        config = load_spawn_config(path) or SpawnConfig.default()
        default_save = path or str(Path.cwd() / DEFAULT_CONFIG_FILENAME)

        def _on_save(saved_path: str) -> None:
            self.spawn_config_path_var.set(saved_path)
            self._refresh_spawn_status()
            self._refresh_unified_status()

        UnifiedEditor(self.root, config, default_save, _on_save,
                      last_records=self._last_unified_records())

    @staticmethod
    def _parse_inv(var: tk.StringVar) -> int | None:
        txt = var.get().strip()
        if not txt:
            return None
        try:
            return max(0, int(txt))
        except ValueError:
            return None

    def _build_actions_frame(self, parent: ttk.Frame) -> None:
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=(4, 6))
        self.run_btn = ttk.Button(f, text="Execute Randomizer Process", command=self._run_clicked, style="Accent.TButton")
        self.run_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(f, text="Open Target Folder", command=self._open_output).pack(side=tk.LEFT, padx=4)
        ttk.Button(f, text="Clear Console Log", command=self._clear_log).pack(side=tk.LEFT, padx=4)

    def _build_log_frame(self, parent: ttk.Frame) -> None:
        f = ttk.Labelframe(parent, text=" Processing Terminal Logs ", padding=6)
        f.pack(fill=tk.BOTH, expand=True, pady=2)
        
        sb = ttk.Scrollbar(f, orient=tk.VERTICAL)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        
        # A moderate explicit height (bigger than the previous fixed 7 lines,
        # which was cramped enough that important diagnostic output like "ELF
        # patch: damage-taken patch NOT applied (reason: ...)" could scroll out
        # of view almost immediately) balanced against leaving room for the
        # options panel above. fill+expand still let it grow if the window is
        # resized taller.
        self.log_text = tk.Text(f, height=12, wrap=tk.WORD, font=("Consolas", 10), bg="#ffffff", fg="#0f172a", bd=1, relief="solid", highlightthickness=0, padx=8, pady=8)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.log_text.configure(yscrollcommand=sb.set)
        sb.configure(command=self.log_text.yview)

    def _build_status_bar(self, parent: ttk.Frame) -> None:
        self.status_var = tk.StringVar(value="Pipeline Ready.")
        sb = ttk.Label(parent, textvariable=self.status_var, anchor=tk.W, font=("Segoe UI", 9), foreground="#475569")
        sb.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))

    def _browse_iso(self) -> None:
        f = filedialog.askopenfilename(filetypes=[("Disc Images", "*.iso *.bin *.cue"), ("All Files", "*.*")])
        if f: 
            self.iso_var.set(f)
            p = Path(f)
            auto_output = p.parent / f"{p.stem}_randomized{p.suffix}"
            self.iso_output_var.set(str(auto_output))

    def _browse_iso_output(self) -> None:
        initial_dir = None
        initial_file = "Maximo - Ghosts to Glory (USA)_randomized.bin"
        current_out = self.iso_output_var.get().strip()
        current_in = self.iso_var.get().strip()
        
        if current_out:
            p = Path(current_out)
            initial_dir = str(p.parent)
            initial_file = p.name
        elif current_in:
            p = Path(current_in)
            initial_dir = str(p.parent)
            initial_file = f"{p.stem}_randomized{p.suffix}"

        f = filedialog.asksaveasfilename(
            initialdir=initial_dir,
            initialfile=initial_file,
            filetypes=[("Disc Images", "*.iso *.bin"), ("All Files", "*.*")]
        )
        if f: self.iso_output_var.set(f)

    def _browse_src(self) -> None:
        d = filedialog.askdirectory()
        if d: self.src_var.set(d)

    def _browse_out(self) -> None:
        d = filedialog.askdirectory()
        if d: self.out_var.set(d)

    def _roll_seed(self) -> None:
        self.seed_var.set(str(random.randint(100000, 999999)))

    def _on_all_worlds_toggle(self) -> None:
        val = bool(self.all_worlds_var.get())
        for w in self._all_worlds: self.world_vars[w].set(val)

    def _on_world_toggle(self) -> None:
        all_sel = all(self.world_vars[w].get() for w in self._all_worlds)
        self.all_worlds_var.set(all_sel)

    def _clear_log(self) -> None:
        try:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _open_output(self) -> None:
        out_str = self.iso_output_var.get().strip()
        if self._notebook.index(self._notebook.select()) == 1:
            out_str = self.out_var.get().strip()
        if not out_str: out_str = self.iso_var.get().strip()
        p = Path(out_str)
        if p.is_file(): p = p.parent
        if p.is_dir():
            import subprocess
            if sys.platform == "win32": os.startfile(p)
            elif sys.platform == "darwin": subprocess.Popen(["open", str(p)])
            else: subprocess.Popen(["xdg-open", str(p)])
        else:
            messagebox.showerror("Destination Error", "The designated output path directory does not exist yet.")

    def _run_clicked(self) -> None:
        if self._running: return
        self._running = True
        self.run_btn.configure(state=tk.DISABLED)
        self.status_var.set("Compiling asset randomizer distributions parameters...")
        self._clear_log()
        threading.Thread(target=self._worker_thread, daemon=True).start()

    def _worker_thread(self) -> None:
        success = False
        err_msg = ""
        stream = _StreamToWidget(self.log_text, self.root)

        with redirect_stdout(stream), redirect_stderr(stream):
            try:
                active_tab = self._notebook.index(self._notebook.select())
                seed_str = self.seed_var.get().strip()
                seed_val = int(seed_str) if seed_str.isdigit() else random.randint(100000, 999999)
                w_list = [w for w in self._all_worlds if self.world_vars[w].get()]

                gold_val = self._parse_inv(self.start_gold_var)
                lives_val = self._parse_inv(self.start_lives_var)
                keys_val = self._parse_inv(self.start_keys_var)
                coins_val = self._parse_inv(self.start_deathcoins_var)
                souls_val = self._parse_inv(self.start_souls_var)
                armor_val = self._parse_inv(self.start_armor_var)
                shields_val = self._parse_inv(self.start_shields_var)
                boxers_val = self._parse_inv(self.start_boxers_var)
                skills_set = self._selected_skills()
                gate_m = self.gate_mode_var.get() if self.gate_rando_var.get() else None

                src_folder_str = self.src_var.get().strip()
                out_folder_str = self.out_var.get().strip()

                # Build a comprehensive options lookup block
                args = _Args(
                    seed=seed_val, items=bool(self.items_var.get()), chests=bool(self.chests_var.get()),
                    skills=bool(self.skills_var.get()), columns=bool(self.columns_var.get()),
                    spawn_location=bool(self.spawn_loc_var.get()),
                    exit_location=bool(self.exit_loc_var.get()),
                    swap_spawn_with_exit=bool(self.swap_spawn_exit_var.get()),
                    gate_randomizer=bool(self.gate_rando_var.get()),
                    gate_mode=gate_m, gen_tier=True, damage_taken=self.damage_taken_var.get(),
                    damage_dealt=self.damage_dealt_var.get(), shield_cost=self._shield_cost_value(),
                    life_cost=self._life_cost_value(),
                    perma_death=bool(self.perma_death_var.get()),
                    sword_cost=self._sword_cost_value(),
                    powerup_duration=self._powerup_duration_value(),
                    wizard_duration=self._wizard_duration_value(),
                    frozen_duration=self._frozen_duration_value(),
                    disable_collision=bool(self.disable_collision_var.get()),
                    level_timer=self._level_timer_value(),
                    timer_display=bool(self.timer_display_var.get()),
                    widescreen=bool(self.widescreen_var.get()),
                    object_budget=self._object_budget_value(),
                    start_gold=gold_val,
                    start_lives=lives_val, start_keys=keys_val, start_deathcoins=coins_val,
                    start_souls=souls_val, start_armor=armor_val, start_shields=shields_val,
                    start_boxers=boxers_val,
                    sword_enchant=self._sword_enchant_value(), elemental_shield=self._elemental_shield_value(),
                    randomize_start_inv=bool(self.randomize_start_inv_var.get()), starting_skills=skills_set,
                    start_skills=skills_set, harder_mode=bool(self.harder_mode_var.get()),
                    preserve_chests=bool(self.preserve_chests_var.get()), preserve_iron_keys=bool(self.preserve_iron_keys_var.get()),
                    randomize_levels=bool(self.randomize_levels_var.get()), randomize_levels_cross=bool(self.randomize_levels_var.get()),
                    enemies=bool(self.enemies_var.get()), no_enemies=not bool(self.enemies_var.get()),
                    duplicate_bosses=bool(self.dup_bosses_var.get()), boss_clones_grave=int(self.boss_clones_grave_var.get()),
                    boss_clones_swamp=int(self.boss_clones_swamp_var.get()), boss_clones_ice=int(self.boss_clones_ice_var.get()),
                    boss_clones_under=int(self.boss_clones_under_var.get()), boss_clones_castle=int(self.boss_clones_castle_var.get()),
                    exclude_dark_knight=bool(self.no_dark_knight_var.get()),
                    # CROSS-WORLD ENEMIES ARE ALWAYS ON. Their checkbutton is
                    # gone; the keywords stay EXPLICIT (both spellings, as
                    # before) because `_Args` has no argparse defaults.
                    cross_world_enemies=True,
                    cross_world=True,
                    # CROSS-WORLD STRUCTURES ARE ALWAYS ON TOO, threaded
                    # EXPLICITLY here as well as at the patch_iso site below
                    # (Req 7.1) — this keyword was silently dropped once.
                    cross_world_structures=True,
                    # BOSS-ARENA ENEMY ARRIVALS: EXPERIMENTAL, DEFAULT OFF, now
                    # with a checkbox in "Elements to Randomize". The keyword is
                    # threaded EXPLICITLY at BOTH sites because `_Args` has no
                    # argparse defaults and an omission is a feature that
                    # silently never runs (`inject_boss`,
                    # `cross_world_structures`, `pool_occupancy` and
                    # `resource_pool` were each lost that way once).
                    boss_arena_arrivals=self.boss_arena_arrivals_var.get(),
                    # easy 3 / normal 6 / hard 9 arrivals per arena.
                    boss_arena_difficulty=self.boss_arena_difficulty_var.get(),
                    # The draw occupancy ceiling. Its GUI control is GONE (93.0%
                    # is a settled measurement), but the keyword stays EXPLICIT at
                    # both sites: `_Args` has no argparse defaults, so a dropped
                    # keyword silently disables a feature. None = `DEFAULT_MAX_
                    # PCT` (90.0), resolved in one place downstream.
                    pool_occupancy=self._pool_occupancy_value(),
                    # The RESOURCE pool size. Its GUI control is GONE too
                    # (0x800000 loads, 0x880000 hangs), and None = leave the
                    # build's own measured cross-world default alone.
                    resource_pool=self._resource_pool_value(),
                    # GraveDigger injection is ALWAYS OFF from the GUI. The
                    # keyword stays EXPLICIT anyway: `_Args` has no argparse
                    # defaults and this keyword has been lost by omission before.
                    inject_boss=False,
                    worlds=w_list, psx_folder=src_folder_str, output=out_folder_str,
                    spawn_config_path=self.spawn_config_path_var.get().strip() or None,
                    backup=bool(self.iso_backup_var.get()), make_backup=bool(self.iso_backup_var.get())
                )

                if active_tab == 0:
                    iso_in = self.iso_var.get().strip()
                    iso_out = self.iso_output_var.get().strip() or None
                    if not iso_in: raise ValueError("Source Maximo disc image target (.iso / .bin) is required.")
                    print(f"--- Launching ISO Patcher Module (Seed: {seed_val}) ---")

                    # Call patch_iso with EXPLICIT, named keyword arguments that
                    # match its real signature (iso_patcher.py) exactly. A prior
                    # version of this code built the kwargs dict by introspecting
                    # patch_iso's signature at runtime and guessing which local
                    # variable/attribute to use per parameter name (substring
                    # matching like "'damage' in param and 'taken' in param").
                    # That's fragile, unauditable, and silently drops/misroutes
                    # any parameter whose name doesn't match its heuristics or
                    # doesn't exist on `args` -- exactly the kind of bug class
                    # that can make "damage" / "starting skills" / other options
                    # silently no-op with no visible error. Explicit kwargs make
                    # every value's destination unambiguous and IDE/linter-
                    # checkable.
                    patch_iso(
                        iso_path=iso_in,
                        seed=seed_val,
                        items=bool(self.items_var.get()),
                        chests=bool(self.chests_var.get()),
                        skills=bool(self.skills_var.get()),
                        columns=bool(self.columns_var.get()),
                        spawn_location=bool(self.spawn_loc_var.get()),
                        exit_location=bool(self.exit_loc_var.get()),
                        swap_spawn_with_exit=bool(self.swap_spawn_exit_var.get()),
                        gen_tier=True,
                        gate_mode=gate_m,
                        damage_taken=self.damage_taken_var.get(),
                        damage_dealt=self.damage_dealt_var.get(),
                        shield_cost=self._shield_cost_value(),
                        life_cost=self._life_cost_value(),
                        perma_death=bool(self.perma_death_var.get()),
                        sword_cost=self._sword_cost_value(),
                        powerup_duration=self._powerup_duration_value(),
                        wizard_duration=self._wizard_duration_value(),
                        frozen_duration=self._frozen_duration_value(),
                        disable_collision=bool(self.disable_collision_var.get()),
                        level_timer=self._level_timer_value(),
                        timer_display=bool(self.timer_display_var.get()),
                        widescreen=bool(self.widescreen_var.get()),
                        object_budget=self._object_budget_value(),
                        start_gold=gold_val,
                        start_lives=lives_val,
                        start_keys=keys_val,
                        start_deathcoins=coins_val,
                        start_souls=souls_val,
                        start_armor=armor_val,
                        start_shields=shields_val,
                        start_boxers=boxers_val,
                        sword_enchant=self._sword_enchant_value(),
                        elemental_shield=self._elemental_shield_value(),
                        start_skills=skills_set,
                        randomize_start_inv=bool(self.randomize_start_inv_var.get()),
                        randomize_levels=bool(self.randomize_levels_var.get()),
                        randomize_levels_cross=bool(self.randomize_levels_var.get()),
                        harder_mode=bool(self.harder_mode_var.get()),
                        preserve_chests=bool(self.preserve_chests_var.get()),
                        preserve_iron_keys=bool(self.preserve_iron_keys_var.get()),
                        spawn_config_path=self.spawn_config_path_var.get().strip() or None,
                        enemies=bool(self.enemies_var.get()),
                        # ALWAYS ON, checkbuttons gone, keywords still EXPLICIT.
                        cross_world=True,
                        cross_world_structures=True,
                        # EXPERIMENTAL, DEFAULT OFF, driven by the checkbox —
                        # keyword EXPLICIT at this site too (see `_Args` above).
                        boss_arena_arrivals=self.boss_arena_arrivals_var.get(),
                        # easy 3 / normal 6 / hard 9 arrivals per arena.
                        boss_arena_difficulty=self.boss_arena_difficulty_var.get(),
                        # Draw occupancy ceiling: control removed (measured
                        # maximum), keyword kept EXPLICIT. None = the default.
                        pool_occupancy=self._pool_occupancy_value(),
                        # RESOURCE pool size: control removed for the same
                        # reason, keyword likewise EXPLICIT. None = the default.
                        resource_pool=self._resource_pool_value(),
                        # ALWAYS OFF from the GUI, keyword still EXPLICIT.
                        inject_boss=False,
                        worlds=(set(w_list) if set(w_list) != set(self._all_worlds) else None),
                        duplicate_bosses=bool(self.dup_bosses_var.get()),
                        exclude_dark_knight=bool(self.no_dark_knight_var.get()),
                        boss_clones_grave=int(self.boss_clones_grave_var.get()),
                        boss_clones_swamp=int(self.boss_clones_swamp_var.get()),
                        boss_clones_ice=int(self.boss_clones_ice_var.get()),
                        boss_clones_under=int(self.boss_clones_under_var.get()),
                        boss_clones_castle=int(self.boss_clones_castle_var.get()),
                        output_iso=iso_out,
                        backup=bool(self.iso_backup_var.get()),
                        log=print,
                    )
                else:
                    if not src_folder_str or not out_folder_str: raise ValueError("Both Source and Output folder paths require operational entries.")
                    print(f"--- Launching Directory Architecture Shuffler (Seed: {seed_val}) ---")
                    # cmd_randomize(args) takes a SINGLE namespace argument (see
                    # cli.py) -- it reads args.psx_folder / args.output itself.
                    # A prior version of this code called
                    # cmd_randomize(src_folder_str, out_folder_str, args) with
                    # three positional arguments, which raises a hard TypeError
                    # every time Folder Mode was used.
                    args.worlds = ",".join(sorted(w_list)) if set(w_list) != set(self._all_worlds) else "all"
                    args.no_enemies = not bool(self.enemies_var.get())
                    cmd_randomize(args)
                success = True
            except Exception as e:
                import traceback
                traceback.print_exc()
                err_msg = str(e)

        self.root.after(0, self._worker_done, success, err_msg)

    def _log_has_cross_world_warning(self) -> bool:
        """True when the run emitted a [cross-world] WARNING (a fallback).

        Reads the log pane the run already wrote to rather than adding a second
        message path. Never raises.
        """
        try:
            text = self.log_text.get("1.0", tk.END)
        except Exception:
            return False
        return any("[cross-world]" in line and "WARNING" in line
                   for line in text.splitlines())

    def _worker_done(self, success: bool, err_msg: str) -> None:
        self._running = False
        self.run_btn.configure(state=tk.NORMAL)
        if success:
            # Cross-world fallbacks are not run failures, but they DO mean an
            # enemy was placed with remapped local textures instead of its own.
            # They arrive through the same redirected-stdout path as every other
            # message (no second channel), so the log pane is the source of
            # truth for the status line.
            if self._log_has_cross_world_warning():
                self.status_var.set("Pipeline Finished: completed with warnings "
                                    "— see the [cross-world] lines in the log.")
            else:
                self.status_var.set("Pipeline Finished: Operations successfully logged.")
            messagebox.showinfo("Execution Complete", "Randomization successfully!")
        else:
            self.status_var.set("Pipeline Error: Runtime exception aborted file serialization.")
            messagebox.showerror("Execution Fault", f"Process failure encountered:\n\n{err_msg}")


# ---------------------------------------------------------------------------
# THE ONE UNIFIED RANDOMIZATION TAB  (`unified-randomization` task 5)
# ---------------------------------------------------------------------------
# Three editors became one. The spawn editor's arrivals-adjacent sections, the
# separate enemy-arrivals editor and the separate structure-arrivals editor are
# GONE; native spawn weights, arriving enemies and arriving structures are one
# table on ONE scale, edited on ONE surface.
#
# Everything below only ever NARROWS. `selectable` comes from the permission
# oracle (`spawn_config.arrival_options` / `structure_arrival_options`, built
# from the catalog allowlist plus the measured cost rows) and never from the
# config, so an unmeasured, inactive or held-back Candidate gets NO editable
# control here and no edit in this window can grant one. There is no relaxation
# control for any Requirement 7 protection anywhere in this surface, by design.

#: THE THREE LONG HELP TEXTS ARE GONE FROM THE WINDOW. `UNIFIED_POOL_HELP`,
#: `UNIFIED_BUDGET_HELP` and `UNIFIED_PROTECTION_HELP` were rendered as three
#: paragraphs above the notebook and are no longer built or packed anywhere.
#:
#: WHAT WAS KEPT, and why: the per-world Shared_Budget / OCCUPANCY readout in
#: `_readout_block`. That is DATA, not description, and it is the ONLY warning a
#: build gets — pool exhaustion is SILENT (MemoryPool::Allocate falls through and
#: returns sentinel_block + 16, allocator metadata is corrupted, the game dies at
#: level load with no log line, no build failure and no test failure), and a real
#: build has been measured running grave at 99.46% of the budget. The readout is
#: now one compact line plus one sentence naming that risk.
#:
#: The two facts the removed paragraphs carried that are NOT restated in the UI
#: are recorded here so they are not lost: the budget bounds SIZE, NOT VARIETY
#: (enemies are offered the budget first, structures keep a guaranteed 300,000 B
#: slice, and whatever does not fit is skipped quietly, so enabling twelve routes
#: and seeing five is the budget working); and every Requirement 7 progression
#: protection is IN FORCE and UNCHANGED, with NO control in this surface that
#: relaxes one — the measured outcomes without them were infinite loading at
#: level load (G_SUB2, S_SUB3, U_SUB3), unexitable levels when
#: End_Of_Sublevel_Column is replaced, and softlocked rooms.

#: The Harder Mode PRESET's two values (Req 1.7). They are written into the very
#: fields this tab edits — there is no separate override path.
HARDER_MODE_SHARE = 0.65


def apply_harder_mode_preset(config) -> tuple[float, int]:
    """Harder Mode as a PRESET of unified settings, written through the config.

    Sets `enemy_share_target = 0.65` and NOTHING ELSE. Returns `(share, 0)`;
    the second value is kept so existing callers still unpack two values.

    IT NO LONGER TOUCHES STRUCTURES. It used to untick every structure Candidate
    AND mark every world's selection EXPLICIT, and it wrote that into the SAVED
    config. "Explicit, nothing ticked" means "port no structures", so one click
    of this preset turned structures off in every world permanently, including
    after Harder Mode was switched off again, and nothing on screen said why.
    MEASURED on a real saved config: all five worlds explicit, 0 of 15-21
    structure routes enabled, and 0 structures admitted to any build. Removing
    structures for Harder Mode is a BUILD-TIME decision that `items.apply_harder_mode`
    makes when the mode is on; it must not be baked into the user's settings.
    """
    config.enemy_share_target = HARDER_MODE_SHARE
    return (HARDER_MODE_SHARE, 0)


#: The two `unified_report.report_world` lines the readouts need, matched as
#: TEXT. The run already printed them, so reading the log pane adds no second
#: message channel, writes no file and cannot affect a build.
_UNIFIED_WORLD_RE = re.compile(r"=====\s*UNIFIED PASS \[([a-z]+)\]")
_UNIFIED_SHARE_RE = re.compile(
    r"enemy_share_target\s+([0-9.]+)\s+vs REALISED\s+([0-9.]+)")
_UNIFIED_BUDGET_RE = re.compile(
    r"Shared_Budget\s+([\d,]+)\s*B; COMMITTED this pass\s+([\d,]+)\s*B; "
    r"REMAINING\s+([\d,]+)\s*B; resident total\s+([\d,]+)\s*B\s*=\s*([\d.]+)%")


def parse_unified_log(text: str) -> dict:
    """`{world: {...}}` for every world the last run reported. Never raises.

    Pure text in, data out, so it is testable with no Tk and no build.
    """
    out: dict[str, dict] = {}
    world = None
    for line in (text or "").splitlines():
        m = _UNIFIED_WORLD_RE.search(line)
        if m:
            world = m.group(1)
            out.setdefault(world, {})
            continue
        if world is None:
            continue
        m = _UNIFIED_SHARE_RE.search(line)
        if m:
            try:
                out[world]["target_share"] = float(m.group(1))
                out[world]["realised_share"] = float(m.group(2))
            except ValueError:
                pass
            continue
        m = _UNIFIED_BUDGET_RE.search(line)
        if m:
            try:
                out[world]["budget"] = int(m.group(1).replace(",", ""))
                out[world]["committed"] = int(m.group(2).replace(",", ""))
                out[world]["remaining"] = int(m.group(3).replace(",", ""))
                out[world]["resident"] = int(m.group(4).replace(",", ""))
                out[world]["occupancy"] = float(m.group(5))
            except ValueError:
                pass
    return out


# Thin module-level wrappers over the `spawn_config` oracle helpers. They exist
# so the section builders read as one line each and so the oracle entry points are
# named in exactly ONE place: `selectable` always comes from here, never from the
# config, and nothing in this module can widen what they return.
def native_candidates_for(world: str) -> list:
    from .spawn_config import native_candidates
    return native_candidates(world)


def enemy_route_candidates_for(world: str) -> list:
    from .spawn_config import enemy_route_candidates
    return enemy_route_candidates(world)


def structure_route_candidates_for(world: str) -> list:
    from .spawn_config import structure_route_candidates
    return structure_route_candidates(world)


def arrival_options_for(world: str) -> list:
    from .spawn_config import arrival_options
    return arrival_options(world)


def structure_arrival_options_for(world: str) -> list:
    from .spawn_config import structure_arrival_options
    return structure_arrival_options(world)


def _unified_max() -> float:
    """The unified table's maximum, shown in the section headers so the scale is
    never implicit."""
    from .spawn_config import UNIFIED_WEIGHT_MAX
    return float(UNIFIED_WEIGHT_MAX)


class UnifiedEditor:
    """THE one editing surface, laid out as SIX sections like the old spawn
    editor, with Structures and Enemies each split per world.

      1. Items              -- unified `native:0xNN` rows, FLAT across worlds.
      2. Structures         -- per world: native structures AND arriving routes.
      3. Mimics/Wizards     -- `config.specials`, legacy scale, as before.
      4. Enemies            -- per world: native enemies AND arriving routes.
      5. Enemy Levels       -- `config.enemy_tiers`, per world, as before.
      6. Abilities & Skills -- `config.ability_drops`, as before.

    TWO SCALES, NEVER MIXED IN A COLUMN. Sections 1, 2 and 4 are the unified
    table on its own 1,360-family scale with `UNIFIED_WEIGHT_MAX` as the spinbox
    maximum; sections 3, 5 and 6 are legacy config tables on their own 0-100 (or
    0-`ABILITY_DROP_WEIGHT_MAX`) scale. Each section says which scale it is on,
    and the Share column is computed WITHIN a section, never across two.

    There is no second editor. The old `SpawnRateEditor` is gone: after task 4
    `cli.py` feeds `native_weights(...)` into `items.randomize_universal` and
    `spawn.universal_weights` is called nowhere, so that editor's Items and
    Structures tabs wrote tables retyping no longer reads. Its still-live
    sections are sections 3, 5 and 6 here.

    A Candidate that is unmeasured, inactive or held back is drawn GREYED with
    its reason and gets no checkbutton and no spinbox. It is never hidden: a
    hidden row is indistinguishable from a row the user turned off.
    """

    #: The OLD spawn-rate naming, restored. The class keeps its internal name;
    #: this is the only string the user sees.
    TITLE = "Spawn Rates"
    #: THE BIG DESCRIPTION BLOCK AT THE TOP IS GONE. `INFO_TEXT` is no longer
    #: rendered; the per-section notes on each tab already say which scale that
    #: section is on, and greyed rows still carry their own reason in the Reason
    #: column.

    def __init__(self, parent: tk.Tk, config, default_save_path: str, on_save,
                 last_records=None):
        from .spawn_config import WORLDS
        self.config = config
        self.on_save = on_save
        self.default_save_path = default_save_path
        self.last_records = dict(last_records or {})
        self._worlds = tuple(WORLDS)
        #: `{group key: [(weight var, enabled var, share label), ...]}`. A GROUP
        #: is one table on ONE scale, and shares are only ever computed inside a
        #: group, so a unified row's share is never divided by a 0-100 total.
        self._groups: dict = {}
        #: Groups whose Share column is the raw percentage the user typed.
        self._direct_groups: set = set()
        #: Groups with no meaningful share denominator (ability drops).
        self._no_share_groups: set = set()
        #: `{world: [(kind, label widget), ...]}` for the per-world readouts, so
        #: the realised share and the budget spend refresh in every world tab.
        self._readouts: dict = {}
        #: `{group key: [widget, ...]}` -- the CONTROLS of each group, kept so a
        #: feature switch can GREY them. Separate from `self._groups` (which
        #: holds variables for the Share maths) on purpose: greying must not be
        #: able to change a value.
        self._group_widgets: dict = {}
        #: `{config field: (BooleanVar, (group key, ...))}` for the two
        #: cross-world FEATURE switches.
        self._feature_switches: dict = {}
        self._defaults = None

        self.win = tk.Toplevel(parent)
        self.win.title(self.TITLE)
        self.win.configure(bg="#f8fafc")
        self.win.transient(parent)
        try:
            scale = getattr(parent, "_scale", 1.0)
        except Exception:
            scale = 1.0
        self.win.geometry(f"{int(900 * scale)}x{int(760 * scale)}")
        self.win.minsize(int(720 * scale), int(520 * scale))

        self.share_var = tk.DoubleVar(
            value=float(getattr(self.config, "enemy_share_target", 0.20)))
        self.share_var.trace_add("write", self._on_share_changed)

        self._build()
        self.win.lift()
        self.win.focus_force()
        self.win.grab_set()

    # ---- chrome ----------------------------------------------------------
    def _build(self) -> None:
        # THE BIG DESCRIPTION BLOCK WAS HERE and is GONE: the `INFO_TEXT`
        # paragraph plus the three long help texts (UNIFIED_BUDGET_HELP,
        # UNIFIED_POOL_HELP, UNIFIED_PROTECTION_HELP) that were packed above the
        # notebook. Nothing replaces them at the top of the window.
        #
        # The per-world Shared_Budget / OCCUPANCY readout in `_readout_block`
        # STAYS, because it is measured data and the only warning a build gets
        # about silent pool exhaustion.

        # THE TARGET-WORLD COMBOBOX IS GONE. The two categories that are really
        # per world -- Structures and Enemies -- now carry their own per-world
        # sub-notebook, so a world is chosen by clicking its tab inside the
        # section it applies to instead of by a global selector that silently
        # also re-scoped the flat sections.
        self._body = ttk.Frame(self.win)
        self._body.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        btns = ttk.Frame(self.win, padding=(12, 12))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="Save Settings", command=self._save,
                   style="Accent.TButton").pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Save As...",
                   command=self._save_as).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel",
                   command=self.win.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Reset to Stock",
                   command=self._reset).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Apply Harder Mode Preset",
                   command=self._apply_harder_mode).pack(side=tk.LEFT, padx=4)

        self._build_tabs()

    # ---- the six sections ------------------------------------------------
    def _build_tabs(self) -> None:
        """The SIX sections, in order, mirroring the old spawn editor's layout.

        Items / Structures / Mimics-Wizards / Enemies / Enemy Levels /
        Abilities & Skills, with Structures and Enemies each split per world.
        """
        for child in self._body.winfo_children():
            child.destroy()
        self._groups = {}
        self._direct_groups = set()
        self._no_share_groups = set()
        self._readouts = {}
        self._group_widgets = {}
        self._feature_switches = {}
        self._defaults = None

        nb = ttk.Notebook(self._body)
        nb.pack(fill=tk.BOTH, expand=True)

        self._build_items_section(nb)
        self._build_structures_section(nb)
        self._build_specials_section(nb)
        self._build_enemies_section(nb)
        self._build_enemy_levels_section(nb)
        self._build_abilities_section(nb)

        self._refresh_readouts()

    def _build_items_section(self, nb) -> None:
        """1. ITEMS -- the unified `native:0xNN` rows for `ITEM_TYPES`. FLAT.

        Items are not split per world because an item's stock weight is the same
        decision everywhere. The unified table is still stored per world, so
        editing a row here writes that native weight into EVERY world's table;
        the note says so, and the row only writes when the user actually edits it
        so a per-world stock override survives until it is deliberately changed.
        """
        inner = self._scrollable(
            nb, "Items",
            note=("Native item spawn weights, on the UNIFIED scale "
                  f"(0-{int(_unified_max())}, the stock item table's own scale "
                  "-- NOT the old 0-100 config scale). These are the "
                  "`native:0xNN` rows the retyping draw reads through "
                  "`native_weights`. Editing a row applies it to every world; "
                  "per-world differences are shown as '(varies)' until you "
                  "change the row."))
        r = self._header(inner, 0)
        group = ("items",)
        for cand in self._flat_native_candidates("item"):
            self._add_flat_native_row(inner, r, group, cand)
            r += 1
        self._refresh_shares(group)

    # ---- the two cross-world FEATURE switches ----------------------------
    def _register_widgets(self, group, *widgets) -> None:
        """Remember a group's CONTROL widgets so a feature switch can grey them.

        Registering a widget only ever affects its `state`; it can never change
        a value, which is why this is a separate registry from `self._groups`.
        """
        if group is None:
            return
        bucket = self._group_widgets.setdefault(group, [])
        bucket.extend(w for w in widgets if w is not None)

    def _add_feature_switch(self, parent, field: str, text: str, note: str,
                            groups) -> None:
        """ONE checkbox for a whole cross-world feature, at the top of its
        section and ABOVE the per-world sub-notebook.

        WHAT IT WRITES: the config field `field` only
        (`cross_world_enemies_enabled` / `cross_world_structures_enabled`),
        immediately, through the same "write on trace" shape every row here
        uses. `cli.py` ANDs that field with the CLI / `_Args` switch, so this can
        only ever turn the feature OFF.

        WHAT IT DOES NOT WRITE: any per-route `enabled` flag. Turning the switch
        off and on again therefore restores the user's route selection EXACTLY,
        which is the whole reason this is a real switch rather than a bulk
        disable. It also never touches `mark_selection_explicit`.
        """
        var = tk.BooleanVar(value=bool(getattr(self.config, field, True)))

        def _cb(*_a, field=field, var=var):
            try:
                value = bool(var.get())
            except tk.TclError:
                return
            setattr(self.config, field, value)
            self._apply_feature_switch(field)

        var.trace_add("write", _cb)
        self._feature_switches[field] = (var, tuple(groups))

        box = ttk.Frame(parent, padding=(8, 8, 8, 2))
        box.pack(fill=tk.X)
        ttk.Checkbutton(box, variable=var, text=text).pack(anchor=tk.W)
        ttk.Label(box, text=note, foreground="#475569", wraplength=820,
                  justify=tk.LEFT, font=("Segoe UI", 9)).pack(anchor=tk.W,
                                                              pady=(2, 0))

    def _apply_feature_switch(self, field: str) -> None:
        """Grey (or un-grey) the rows a feature switch governs.

        GREYED: only the ARRIVING route rows of that section (the `enemy:*` /
        `structure:*` groups) and their own Enable all / Disable all buttons,
        because with the feature off they cannot do anything.

        NOT GREYED: the NATIVE rows and their bulk buttons, because native spawn
        weights apply whatever this switch says.

        Rows are GREYED, never hidden: a hidden row is indistinguishable from a
        row the user turned off. And nothing here writes a value -- only widget
        `state` changes -- so the stored per-route selection is untouched.
        """
        entry = self._feature_switches.get(field)
        if entry is None:
            return
        var, groups = entry
        try:
            state = "normal" if bool(var.get()) else "disabled"
        except tk.TclError:
            return
        for group in groups:
            for widget in self._group_widgets.get(group, []):
                try:
                    widget.configure(state=state)
                except tk.TclError:
                    continue

    def _build_structures_section(self, nb) -> None:
        """2. STRUCTURES -- PER WORLD: natives AND arriving routes, grouped.

        The whole-feature switch sits at the TOP, above the per-world
        sub-notebook, so it reads as the feature and not as one world.
        """
        outer = ttk.Frame(nb)
        nb.add(outer, text="Structures")
        route_groups = [("structure_routes", world) for world in self._worlds]
        self._add_feature_switch(
            outer, "cross_world_structures_enabled",
            "Cross-world structures (ports structures between worlds)",
            note=("ON by default, and this is the whole feature — not one "
                  "world. Un-tick it and NO structure is ported into any world; "
                  "the ARRIVING rows below grey out in every world tab because "
                  "they cannot do anything, but your ticks are REMEMBERED and "
                  "come back exactly as they were if you re-tick this. NATIVE "
                  "structure weights stay editable and still apply. The "
                  "command-line --cross-world-structures switch and this box "
                  "must BOTH be on for structures to port."),
            groups=route_groups)
        sub = ttk.Notebook(outer)
        sub.pack(fill=tk.BOTH, expand=True)
        for world in self._worlds:
            self._build_structure_world_tab(sub, world)
        # Applied AFTER the world tabs, because that is when the route rows and
        # their bulk buttons have registered their widgets.
        self._apply_feature_switch("cross_world_structures_enabled")

    # ---- bulk enable / disable ------------------------------------------
    def _bulk_set_enabled(self, groups, value: bool) -> None:
        """Tick or untick EVERY editable row in `groups`, through the SAME write
        path a manual tick uses.

        It sets each row's `en_var`, and nothing else. Every consequence of a
        manual tick therefore still happens, because they all hang off that
        variable's write trace in `_add_row`: the config entry is written, the
        `config.enemies` mirror is updated for native enemies
        (`_mirror_native_enemy`), and `mark_selection_explicit(world)` is called
        for structure routes. Bulk and manual edits cannot diverge because there
        is only one write path.

        UNSELECTABLE ROWS ARE SKIPPED, NOT FORCE-ENABLED, and that is structural
        rather than a check here: `_add_unselectable_row` registers NOTHING in
        `self._groups`, so a route the permission oracle withheld has no `en_var`
        for this to reach. The oracle is not widened.
        """
        for group in groups:
            for _w_var, en_var, _pct in self._groups.get(group, []):
                if en_var is None:
                    continue
                try:
                    en_var.set(bool(value))
                except tk.TclError:
                    return
            self._refresh_shares(group)

    def _bulk_buttons(self, inner, r: int, groups, what: str,
                      widget_group=None) -> int:
        """An 'Enable all' / 'Disable all' pair for this tab's rows.

        `widget_group` registers the two buttons so a feature switch can grey
        them; the NATIVE pairs pass None and are therefore never greyed.
        """
        box = ttk.Frame(inner)
        box.grid(row=r, column=0, columnspan=6, sticky=tk.W, padx=6, pady=(2, 2))
        b_on = ttk.Button(box, text="Enable all", width=12,
                          command=lambda: self._bulk_set_enabled(groups, True))
        b_on.pack(side=tk.LEFT, padx=(0, 4))
        b_off = ttk.Button(box, text="Disable all", width=12,
                           command=lambda: self._bulk_set_enabled(groups, False))
        b_off.pack(side=tk.LEFT, padx=4)
        self._register_widgets(widget_group, b_on, b_off)
        ttk.Label(box, foreground="#475569", font=("Segoe UI", 9),
                  text=(f"applies to every {what} row on this tab. Greyed rows "
                        f"are skipped — they cannot be enabled at all.")).pack(
            side=tk.LEFT, padx=8)
        return r + 1

    def _build_structure_world_tab(self, nb, world: str) -> None:
        inner = self._scrollable(nb, world.capitalize())
        r = self._readout_block(inner, 0, world)
        r = self._header(inner, r)

        group = ("structures", world)
        r = self._group(
            inner, r, f"NATIVE STRUCTURES — {world}",
            "Structure types this world already owns. Zero resident cost, so "
            "the byte budget never refuses one.")
        # ONE pair PER GROUP now, instead of one pair for both. Nothing is lost
        # -- every row is still reachable by a bulk button -- and it is what lets
        # the ARRIVING pair be greyed while the NATIVE pair stays live, because
        # native weights apply whatever the feature switch says.
        r = self._bulk_buttons(inner, r, [group], "native structure")
        for cand in self._native_candidates_of_kind(world, "structure"):
            self._add_row(inner, r, world, cand, group)
            r += 1

        route_group = ("structure_routes", world)
        r = self._group(
            inner, r, f"ARRIVING STRUCTURES — into {world}",
            "Structure arrival ROUTES into this world, from another world. "
            "Admitted AFTER enemies out of the same budget, with 300,000 B "
            "reserved so they are never starved to zero. Every row starts "
            "ENABLED and the feature switch at the top of this section starts "
            "ON, so a stock build ports structures into all five worlds. "
            "Touching any row -- ticking "
            "OR unticking, including via Disable all -- makes your ticks exact, "
            "including none. These rows are GREYED when 'Cross-world "
            "structures' at the top of this section is un-ticked.")
        r = self._bulk_buttons(inner, r, [route_group], "arriving structure",
                               widget_group=route_group)
        for cand in structure_route_candidates_for(world):
            self._add_row(inner, r, world, cand, route_group)
            r += 1
        for opt in structure_arrival_options_for(world):
            if not opt.selectable:
                self._add_unselectable_row(inner, r, opt.label, opt.reason)
                r += 1

        self._refresh_shares(group)
        self._refresh_shares(route_group)

    def _build_specials_section(self, nb) -> None:
        """3. MIMICS/WIZARDS -- `config.specials`, direct percentages, as before.

        NOT part of the unified table: `config.specials` is consumed exactly as
        it always was, so this section stays on the 0-100 config scale and says
        so in its own header.
        """
        from .spawn_config import SPECIAL_NAMES
        group = ("specials",)
        self._direct_groups.add(group)
        inner = self._scrollable(
            nb, "Mimics/Wizards",
            note=("Direct percentages (0-100) on the LEGACY config scale, not "
                  "the unified scale. `config.specials` is consumed exactly as "
                  "it always was."))
        r = self._header(inner, 0, value_label="%", cost_col=False)
        for tid, name in SPECIAL_NAMES.items():
            if tid not in self.config.specials:
                continue
            self._add_legacy_row(inner, r, group, name, self.config.specials,
                                 tid)
            r += 1
        self._refresh_shares(group)

    def _build_enemies_section(self, nb) -> None:
        """4. ENEMIES -- PER WORLD: natives AND arriving routes, grouped."""
        outer = ttk.Frame(nb)
        nb.add(outer, text="Enemies")
        route_groups = [("enemy_routes", world) for world in self._worlds]
        self._add_feature_switch(
            outer, "cross_world_enemies_enabled",
            "Cross-world enemies (ports enemies between worlds)",
            note=("ON by default, and this is the whole feature — not one "
                  "world. Un-tick it and NO enemy is ported into any world; the "
                  "ARRIVING rows below grey out in every world tab because they "
                  "cannot do anything, but your ticks are REMEMBERED and come "
                  "back exactly as they were if you re-tick this. NATIVE enemy "
                  "weights stay editable and still apply. The command-line "
                  "--cross-world switch and this box must BOTH be on for "
                  "enemies to port."),
            groups=route_groups)
        sub = ttk.Notebook(outer)
        sub.pack(fill=tk.BOTH, expand=True)
        for world in self._worlds:
            self._build_enemy_world_tab(sub, world)
        self._apply_feature_switch("cross_world_enemies_enabled")

    def _build_enemy_world_tab(self, nb, world: str) -> None:
        inner = self._scrollable(nb, world.capitalize())
        r = self._readout_block(inner, 0, world)
        r = self._header(inner, r)

        group = ("enemies", world)
        r = self._group(
            inner, r, f"NATIVE ENEMIES — {world}",
            "Enemies this world already owns, on the UNIFIED scale. Editing a "
            "row writes BOTH the unified `native:0xNN` weight (which the "
            "retyping draw reads) AND this world's `config.enemies` entry "
            "(which the even-out balancing bag and the pool exclusion read), so "
            "unticking an enemy really does remove it everywhere.")
        # ONE pair PER GROUP: the NATIVE pair is never greyed, the ARRIVING pair
        # below is.
        r = self._bulk_buttons(inner, r, [group], "native enemy")
        for cand in self._native_candidates_of_kind(world, "enemy"):
            self._add_row(inner, r, world, cand, group, mirror_enemies=True)
            r += 1

        route_group = ("enemy_routes", world)
        r = self._group(
            inner, r, f"ARRIVING ENEMIES — into {world}",
            "Enemy arrival ROUTES into this world, from another world. Offered "
            "the budget FIRST -- enemies keep admission priority. Each costs "
            "the measured resident bytes shown. These rows are GREYED when "
            "'Cross-world enemies' at the top of this section is un-ticked.")
        r = self._bulk_buttons(inner, r, [route_group], "arriving enemy",
                               widget_group=route_group)
        for cand in enemy_route_candidates_for(world):
            self._add_row(inner, r, world, cand, route_group)
            r += 1
        for opt in arrival_options_for(world):
            if not opt.selectable:
                self._add_unselectable_row(inner, r, opt.label, opt.reason)
                r += 1

        self._refresh_shares(group)
        self._refresh_shares(route_group)

    def _build_enemy_levels_section(self, nb) -> None:
        """5. ENEMY LEVELS -- `config.enemy_tiers`, per world, exactly as before.

        No enable flag: the weight itself gates a level (0 suppresses it), so
        rows are registered with `en_var` None.
        """
        outer = ttk.Frame(nb)
        nb.add(outer, text="Enemy Levels")
        sub = ttk.Notebook(outer)
        sub.pack(fill=tk.BOTH, expand=True)
        for world in self._worlds:
            self._build_enemy_tier_world_tab(sub, world)

    def _build_enemy_tier_world_tab(self, nb, world: str) -> None:
        from .spawn_config import (ENEMY_TYPES, TIER_DEFAULT_EQUAL, tier_label,
                                  world_enemy_tier_types)
        inner = self._scrollable(
            nb, world.capitalize(),
            note=("Class levels on the LEGACY 0-100 config scale. Each enemy's "
                  "levels are weighted against each other; 0 suppresses a "
                  "level. Harder Mode overrides these, forcing every classed "
                  "enemy to its top level."))
        ttk.Label(inner, text="Class Level", font=("Segoe UI", 10, "bold"),
                  anchor=tk.W).grid(row=0, column=1, sticky=tk.W, padx=6, pady=4)
        ttk.Label(inner, text="Weight", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=2, padx=6, pady=4)
        ttk.Label(inner, text="Share", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=3, padx=6, pady=4)

        store = self.config.enemy_tiers.get(world, {})
        r = 1
        for tid, (name, tiers) in world_enemy_tier_types(world).items():
            if tid not in store:
                continue
            group = ("tier", world, tid)
            display = ENEMY_TYPES.get(tid, str(name).replace("_", " "))
            ttk.Label(inner, text=display, font=("Segoe UI", 10, "bold"),
                      anchor=tk.W).grid(row=r, column=1, columnspan=3,
                                        sticky=tk.W, padx=6, pady=(8, 2))
            r += 1

            tier_vars = {}
            for tier in tiers:
                w_var = tk.DoubleVar(value=float(store[tid].get(tier, 0.0)))
                pct = ttk.Label(inner, text="", width=8)

                def _cb(*_a, tid=tid, tier=tier, w_var=w_var, group=group,
                        world=world):
                    try:
                        v = float(w_var.get())
                    except (tk.TclError, ValueError):
                        return
                    self.config.enemy_tiers[world][tid][tier] = max(
                        0.0, min(100.0, v))
                    self._refresh_shares(group)

                w_var.trace_add("write", _cb)
                tier_vars[tier] = w_var

                ttk.Label(inner, text=tier_label(tier), anchor=tk.W).grid(
                    row=r, column=1, sticky=tk.W, padx=(24, 6), pady=2)
                ttk.Spinbox(inner, from_=0, to=100, increment=1, width=6,
                            textvariable=w_var).grid(row=r, column=2, padx=6,
                                                     pady=2)
                pct.grid(row=r, column=3, padx=6, pady=2)
                self._groups.setdefault(group, []).append((w_var, None, pct))
                r += 1

            quick = ttk.Frame(inner)
            quick.grid(row=r, column=1, columnspan=3, sticky=tk.W,
                       padx=(24, 6), pady=(2, 6))
            r += 1

            def _only(tier, tier_vars=tier_vars):
                for t, v in tier_vars.items():
                    v.set(100.0 if t == tier else 0.0)

            def _even(tier_vars=tier_vars):
                for v in tier_vars.values():
                    v.set(float(TIER_DEFAULT_EQUAL))

            for tier in tiers:
                ttk.Button(quick, text=f"{tier_label(tier)} only", width=11,
                           command=lambda t=tier, f=_only: f(t)).pack(
                    side=tk.LEFT, padx=2)
            ttk.Button(quick, text="Even", width=8,
                       command=_even).pack(side=tk.LEFT, padx=2)

            self._refresh_shares(group)

    def _build_abilities_section(self, nb) -> None:
        """6. ABILITIES & SKILLS -- `config.ability_drops`, exactly as before."""
        from .spawn_config import ABILITY_DROP_TYPES
        from .elf_patch import ABILITY_DROP_WEIGHT_MAX
        group = ("ability_drops",)
        self._no_share_groups.add(group)
        inner = self._scrollable(
            nb, "Abilities & Skills",
            note=("These are the power-ups the Ability pickup can give and "
                  "drop, on their OWN scale (0-"
                  f"{ABILITY_DROP_WEIGHT_MAX}, the game's own maximum) -- not "
                  "the unified scale. As soon as you check ANY entry the list "
                  "becomes the whole pool: unchecked entries never spawn "
                  "(weight 0 in all 24 level tables and excluded from the "
                  "pickups placed on the maps). Checking an entry pins its "
                  "weight across all 24 level tables and protects it from the "
                  "ability-drop randomizer: 0 stops that power-up dropping. "
                  "'Shield Stamina (+5)' and 'Sword Stamina (+5)' are the "
                  "shield and sword-enchantment refills."))
        r = self._header(inner, 0, value_label="", cost_col=False)
        for tid, name in ABILITY_DROP_TYPES.items():
            if tid not in self.config.ability_drops:
                continue
            self._add_legacy_row(inner, r, group, name,
                                 self.config.ability_drops, tid,
                                 maximum=ABILITY_DROP_WEIGHT_MAX)
            r += 1
        self._refresh_shares(group)

    # ---- per-world readouts ---------------------------------------------
    def _readout_block(self, inner, r: int, world: str) -> int:
        """The per-world budget spend / OCCUPANCY, in ONE compact line.

        Rendered inside every per-world Structures and Enemies tab, because that
        is where the figures apply.

        THE `enemy_share_target` SPINBOX AND ITS REALISED-SHARE READOUT WERE
        HERE, and both are GONE FROM THE UI ONLY. The FIELD is untouched:
        `config.enemy_share_target` still defaults to 0.20, `native_weights`
        still applies it and still renormalises the enemy share against the item
        share, and `apply_harder_mode_preset` still writes 0.65 into it.
        `self.share_var` is kept as the mirror the preset and reset paths set; it
        simply has no widget bound to it any more, and nothing registers a
        "realised" readout, so `_refresh_readouts` has none to update.

        The budget line STAYS, and it is the reason the three help paragraphs
        could go and this could not: pool exhaustion is SILENT, so the occupancy
        figure is the only warning a build gives.
        """
        budget = ttk.Label(inner, text="", justify=tk.LEFT,
                           foreground="#0f172a", font=("Segoe UI", 9, "bold"))
        budget.grid(row=r, column=0, columnspan=6, sticky=tk.W, padx=6,
                    pady=(4, 0))
        ttk.Label(inner, wraplength=820, justify=tk.LEFT,
                  foreground="#b45309", font=("Segoe UI", 9),
                  text=("Pool exhaustion is SILENT — no log line, no failure — "
                        "and grave has been measured at 99.46% of budget, so "
                        "this occupancy figure is the only warning you get.")
                  ).grid(row=r + 1, column=0, columnspan=6, sticky=tk.W,
                         padx=6, pady=(0, 6))
        self._readouts.setdefault(world, []).append(("budget", budget))
        return r + 2

    # ---- candidate helpers ----------------------------------------------
    def _native_candidates_of_kind(self, world: str, want: str) -> list:
        """This world's native Candidates, split into item / structure / enemy.

        The split is by TYPE TABLE membership, never by a second allowlist:
        enemies are the ids `ENEMY_TYPES` names, structures the ids
        `STRUCTURE_TYPES` names, and everything else in the pool is an item. The
        candidate set itself still comes from `native_candidates`, so this only
        ever partitions what the oracle already offered.
        """
        from .spawn_config import ENEMY_TYPES, STRUCTURE_TYPES
        out = []
        for cand in native_candidates_for(world):
            if cand.type_id in ENEMY_TYPES:
                kind = "enemy"
            elif cand.type_id in STRUCTURE_TYPES:
                kind = "structure"
            else:
                kind = "item"
            if kind == want:
                out.append(cand)
        return out

    def _flat_native_candidates(self, want: str) -> list:
        """One Candidate per native type across ALL worlds, for the flat Items
        section. Keyed on the unified id, which does not carry the world."""
        seen: dict = {}
        for world in self._worlds:
            for cand in self._native_candidates_of_kind(world, want):
                seen.setdefault(cand.key, cand)
        return [seen[k] for k in sorted(seen)]

    def _scrollable(self, notebook, title, note=None):
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=title)
        if note is not None:
            ttk.Label(outer, text=note, justify=tk.LEFT, wraplength=820,
                      foreground="#475569", font=("Segoe UI", 9)).pack(
                fill=tk.X, side=tk.TOP, padx=8, pady=(4, 6))
        canvas = tk.Canvas(outer, highlightthickness=0, bg="#ffffff")
        scroll = ttk.Scrollbar(outer, orient=tk.VERTICAL,
                               command=canvas.yview)
        inner = ttk.Frame(canvas, style="MainViewport.TFrame")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel(event):
            if sys.platform == "win32":
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            elif sys.platform == "darwin":
                canvas.yview_scroll(int(-1 * event.delta), "units")
        inner.bind("<Enter>",
                   lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        inner.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    # ---- the table -------------------------------------------------------
    def _header(self, inner, r: int, value_label: str = "Share",
                cost_col: bool = True) -> int:
        # "Candidate (id)" became "Candidate": rows no longer carry the unified
        # id, so the column heading must not promise one.
        cols = [("On", 0), ("Candidate", 1), ("Weight", 2),
                (value_label, 3)]
        if cost_col:
            cols.extend([("Resident cost", 4), ("Reason", 5)])
        for text, col in cols:
            ttk.Label(inner, text=text, font=("Segoe UI", 10, "bold"),
                      anchor=tk.W).grid(row=r, column=col, sticky=tk.W,
                                        padx=6, pady=4)
        return r + 1

    def _group(self, inner, r: int, title: str, note: str) -> int:
        ttk.Label(inner, text=title, font=("Segoe UI", 10, "bold"),
                  foreground="#0f172a").grid(row=r, column=0, columnspan=6,
                                             sticky=tk.W, padx=6, pady=(10, 0))
        ttk.Label(inner, text=note, foreground="#475569",
                  font=("Segoe UI", 9), wraplength=780,
                  justify=tk.LEFT).grid(row=r + 1, column=0, columnspan=6,
                                        sticky=tk.W, padx=6, pady=(0, 4))
        return r + 2

    @staticmethod
    def _row_label(cand) -> str:
        """The row's display text: THE NAME, with no unified-id decoration.

        Rows used to read `Gold Coin  [native:0x21]`; they now read `Gold Coin`.
        The `[native:0xNN]` / `[enemy:0x53-ice]` suffix was the unified id and
        told the user nothing they could act on.

        THE SOURCE WORLD STAYS for ARRIVING routes, and that is not decoration:
        the same type can arrive into a world from more than one source, so
        `Ice Plant (from Ice)` and `Ice Plant (from Under)` are two DIFFERENT
        rows that would otherwise be indistinguishable. This matches the
        `label` property the oracle already uses for unselectable rows.
        """
        name = str(cand.name)
        src = getattr(cand, "source_world", None)
        if src:
            return f"{name} (from {str(src).capitalize()})"
        return name

    def _entry_for(self, world: str, cand) -> dict:
        """The stored `{"weight", "enabled"}` for a Candidate, created from the
        STOCK default when the config has no row for it yet.

        Creating a missing row cannot widen anything: `cand` came from the
        oracle, and `default_unified_weights` only carries selectable keys.
        """
        from .spawn_config import default_unified_weights
        table = self.config.unified_weights.setdefault(world, {})
        if cand.key not in table:
            if self._defaults is None:
                self._defaults = default_unified_weights()
            stock = self._defaults.get(world, {}).get(cand.key)
            table[cand.key] = (dict(stock) if stock
                               else {"weight": 0.0, "enabled": False})
        return table[cand.key]

    def _mirror_structure_route(self, world: str, cand) -> None:
        """Keep `config.structure_arrivals[world]` IN STEP with a unified
        `structure:*` row.

        WHICH CONSUMER READS WHICH, for the same reason `_mirror_native_enemy`
        spells it out:

          * `unified_weights[world]["structure:<tid>-<src>"]` is what this editor
            writes and what `SpawnConfig.structure_route_weights` reads for the
            route's WEIGHT.
          * `config.structure_arrivals[world]["0xNN|src"]` is what
            `SpawnConfig.enabled_structure_arrival_keys` reads, and that is the
            set `spawn_config.structure_routes_for_world` returns on its EXPLICIT
            branch -- i.e. WHICH routes are candidates at all.

        Writing only the unified row left the second table saying "no routes",
        which is why a single tick could disable structures outright. The weight
        is CLAMPED to the legacy 0-100 scale that table is on, and a non-zero
        unified weight never becomes 0, because 0 there reads as "not a route".
        """
        from .spawn_config import arrival_route_key, _clamp
        store = self.config.structure_arrivals.get(world)
        if not store:
            return
        key = arrival_route_key(cand.type_id, cand.source_world)
        entry = store.get(key)
        if entry is None:
            return          # not a route this config knows; never invent one
        weight = self.config.unified_weight(world, cand.key)
        enabled = self.config.unified_enabled(world, cand.key, False)
        entry["enabled"] = bool(enabled)
        if weight > 0:
            entry["weight"] = max(1.0, _clamp(weight))

    def _mirror_native_enemy(self, world: str, type_id: int, weight: float,
                             enabled: bool) -> None:
        """Keep `config.enemies[world]` IN STEP with the unified native row.

        WHICH CONSUMER READS WHICH, written down so this cannot drift again:

          * `unified_weights[world]["native:0xNN"]` is read by
            `SpawnConfig.native_weights`, which `cli.py` feeds into
            `items.randomize_universal`. It decides RETYPING frequency, and it is
            the only table retyping reads after task 4.
          * `config.enemies[world]` is read by
            `SpawnConfig.enemy_weights_for_world` (the even-out balancing bag)
            and `SpawnConfig.disabled_enemies` (pool EXCLUSION). Both are still
            called from `cli.py`.

        Writing only the unified row would leave an enemy the user unticked still
        in the spawn pool and still in the balancing bag, so both are written.

        The weight is CONVERTED, not copied: the legacy enemy table is on the
        0-100 config scale and the unified table on the 1,360 scale, and
        `spawn_config._migration_enemy_factor()` is the one documented ratio
        between them (it is what migration used in the other direction), so the
        ratios inside the bag are preserved. A non-zero unified weight never
        converts down to 0, because 0 is exactly what `disabled_enemies` reads as
        "excluded" and the two must agree.
        """
        from .spawn_config import _migration_enemy_factor
        store = self.config.enemies.get(world)
        if not store:
            return
        factor = _migration_enemy_factor() or 1.0
        legacy = max(0.0, min(100.0, float(weight) / factor))
        if weight > 0 and legacy <= 0:
            legacy = 0.1
        prefix = f"{int(type_id)}_"
        for key in list(store):
            same = (key == type_id
                    or (isinstance(key, str) and key.startswith(prefix)))
            if not same:
                continue
            store[key]["weight"] = legacy
            store[key]["enabled"] = bool(enabled)

    def _add_row(self, inner, r: int, world: str, cand, group,
                 mirror_enemies: bool = False) -> None:
        """One EDITABLE unified Candidate row, on the UNIFIED scale."""
        from .spawn_config import UNIFIED_KIND_STRUCTURE, UNIFIED_WEIGHT_MAX
        ent = self._entry_for(world, cand)
        en_var = tk.BooleanVar(value=bool(ent.get("enabled", True)))
        w_var = tk.DoubleVar(value=float(ent.get("weight", 0) or 0))
        pct = ttk.Label(inner, text="", width=8)

        def _cb(*_a, ent=ent, en_var=en_var, w_var=w_var, cand=cand,
                group=group, world=world, mirror_enemies=mirror_enemies):
            try:
                v = float(w_var.get())
            except (tk.TclError, ValueError):
                return
            ent["weight"] = max(0.0, min(float(UNIFIED_WEIGHT_MAX), v))
            ent["enabled"] = bool(en_var.get())
            # THE ENEMIES SECTION WRITES BOTH PLACES. See `_mirror_native_enemy`
            # for which consumer reads which table.
            if mirror_enemies:
                self._mirror_native_enemy(world, cand.type_id, ent["weight"],
                                          ent["enabled"])
            # "THE SWITCH ALONE MEANS YES" is preserved, and this is where it is
            # given up deliberately: touching a STRUCTURE row at all — tick or
            # untick — marks the selection EXPLICIT for this world, so the
            # fallback (no explicit selection => every selectable route is a
            # candidate, via `structure_routes_for_world`) stops applying and the
            # user's exact subset is honoured, including the empty one. Nothing
            # here re-derives the candidate set; that stays in
            # `SpawnConfig.structure_route_weights`.
            if cand.kind == UNIFIED_KIND_STRUCTURE:
                self.config.mark_selection_explicit(world)
                # AND MIRROR THE TICK INTO THE TABLE THE DRAW ACTUALLY READS.
                # Marking the world explicit sends
                # `spawn_config.structure_routes_for_world` down its EXPLICIT
                # branch, and that branch reads `config.structure_arrivals` --
                # the LEGACY table -- while this row lives in
                # `config.unified_weights`. Nothing mirrored one into the other,
                # so touching a single structure row switched the world to "the
                # user chose exactly these" and the draw then read an
                # all-disabled legacy table: ZERO structures, and no further
                # editing could undo it. Mirroring here is the same discipline
                # `_mirror_native_enemy` already applies to native enemy rows.
                self._mirror_structure_route(world, cand)
            self._refresh_shares(group)

        en_var.trace_add("write", _cb)
        w_var.trace_add("write", _cb)

        chk = ttk.Checkbutton(inner, variable=en_var)
        chk.grid(row=r, column=0, padx=6, pady=2)
        ttk.Label(inner, text=self._row_label(cand), anchor=tk.W).grid(
            row=r, column=1, sticky=tk.W, padx=6, pady=2)
        spin = ttk.Spinbox(inner, from_=0, to=UNIFIED_WEIGHT_MAX, increment=1,
                           width=7, textvariable=w_var)
        spin.grid(row=r, column=2, padx=6, pady=2)
        # Registered so a FEATURE switch can grey this row's controls. Greying
        # touches `state` only, never `en_var` / `w_var`, so the stored selection
        # survives a switch off/on cycle exactly.
        self._register_widgets(group, chk, spin)
        pct.grid(row=r, column=3, padx=6, pady=2)
        # BLANK for a zero-cost native; the MEASURED bytes for a route.
        cost = f"{cand.cost_bytes:,} B" if cand.cost_bytes else ""
        ttk.Label(inner, text=cost, anchor=tk.W).grid(row=r, column=4,
                                                     sticky=tk.W, padx=6, pady=2)
        ttk.Label(inner, text="", anchor=tk.W).grid(row=r, column=5,
                                                   sticky=tk.W, padx=6, pady=2)
        self._groups.setdefault(group, []).append((w_var, en_var, pct))

    def _add_flat_native_row(self, inner, r: int, group, cand) -> None:
        """A FLAT native row: one control, written into EVERY world's table.

        Still the unified scale and still the unified `native:0xNN` key, so this
        is the same row the retyping draw reads -- it is only the per-world
        duplication that is collapsed.
        """
        from .spawn_config import UNIFIED_WEIGHT_MAX
        values = []
        for world in self._worlds:
            ent = self.config.unified_weights.get(world, {}).get(cand.key)
            if ent is not None:
                values.append((float(ent.get("weight", 0) or 0),
                               bool(ent.get("enabled", True))))
        if not values:
            values = [(0.0, False)]
        weight0, enabled0 = values[0]
        varies = any(v != values[0] for v in values)

        en_var = tk.BooleanVar(value=enabled0)
        w_var = tk.DoubleVar(value=weight0)
        pct = ttk.Label(inner, text="", width=8)
        note = ttk.Label(inner, text=("(varies by world)" if varies else ""),
                         anchor=tk.W, foreground="#b45309")

        def _cb(*_a, cand=cand, en_var=en_var, w_var=w_var, group=group,
                note=note):
            try:
                v = float(w_var.get())
            except (tk.TclError, ValueError):
                return
            weight = max(0.0, min(float(UNIFIED_WEIGHT_MAX), v))
            enabled = bool(en_var.get())
            for world in self._worlds:
                ent = self._entry_for(world, cand)
                ent["weight"] = weight
                ent["enabled"] = enabled
            try:
                note.configure(text="")
            except tk.TclError:
                pass
            self._refresh_shares(group)

        en_var.trace_add("write", _cb)
        w_var.trace_add("write", _cb)

        ttk.Checkbutton(inner, variable=en_var).grid(row=r, column=0,
                                                    padx=6, pady=2)
        ttk.Label(inner, text=self._row_label(cand), anchor=tk.W).grid(
            row=r, column=1, sticky=tk.W, padx=6, pady=2)
        ttk.Spinbox(inner, from_=0, to=UNIFIED_WEIGHT_MAX, increment=1, width=7,
                    textvariable=w_var).grid(row=r, column=2, padx=6, pady=2)
        pct.grid(row=r, column=3, padx=6, pady=2)
        ttk.Label(inner, text="", anchor=tk.W).grid(row=r, column=4,
                                                   sticky=tk.W, padx=6, pady=2)
        note.grid(row=r, column=5, sticky=tk.W, padx=6, pady=2)
        self._groups.setdefault(group, []).append((w_var, en_var, pct))

    def _add_legacy_row(self, inner, r: int, group, name, store, tid,
                        maximum: float = 100.0) -> None:
        """A LEGACY-scale row (`config.specials`, `config.ability_drops`).

        These tables are NOT in the unified table and are consumed exactly as
        before, so they keep their own 0-`maximum` scale. They are never mixed
        into a unified column: each lives in its own section with its own header.
        """
        ent = store[tid]
        en_var = tk.BooleanVar(value=bool(ent.get("enabled", True)))
        w_var = tk.DoubleVar(value=float(ent.get("weight", 0)))
        pct = ttk.Label(inner, text="", width=8)

        def _cb(*_a, tid=tid, en_var=en_var, w_var=w_var, store=store,
                group=group, maximum=maximum):
            try:
                v = float(w_var.get())
            except (tk.TclError, ValueError):
                return
            store[tid]["weight"] = max(0.0, min(float(maximum), v))
            store[tid]["enabled"] = bool(en_var.get())
            self._refresh_shares(group)

        en_var.trace_add("write", _cb)
        w_var.trace_add("write", _cb)

        ttk.Checkbutton(inner, variable=en_var).grid(row=r, column=0, padx=6,
                                                    pady=2)
        ttk.Label(inner, text=name, anchor=tk.W).grid(row=r, column=1,
                                                     sticky=tk.W, padx=6, pady=2)
        ttk.Spinbox(inner, from_=0, to=maximum, increment=1, width=7,
                    textvariable=w_var).grid(row=r, column=2, padx=6, pady=2)
        pct.grid(row=r, column=3, padx=6, pady=2)
        self._groups.setdefault(group, []).append((w_var, en_var, pct))

    def _add_unselectable_row(self, inner, r: int, label: str,
                              reason: str) -> None:
        """A Candidate the oracle withheld: VISIBLE, greyed, NO control."""
        ttk.Label(inner, text="—", foreground="#94a3b8").grid(row=r, column=0,
                                                             padx=6, pady=2)
        ttk.Label(inner, text=label, anchor=tk.W, foreground="#94a3b8").grid(
            row=r, column=1, sticky=tk.W, padx=6, pady=2)
        ttk.Label(inner, text="—", foreground="#94a3b8").grid(row=r, column=2,
                                                             padx=6, pady=2)
        ttk.Label(inner, text="", foreground="#94a3b8").grid(row=r, column=3,
                                                            padx=6, pady=2)
        ttk.Label(inner, text="", foreground="#94a3b8").grid(row=r, column=4,
                                                            padx=6, pady=2)
        ttk.Label(inner, text=reason, anchor=tk.W, foreground="#94a3b8").grid(
            row=r, column=5, sticky=tk.W, padx=6, pady=2)

    # ---- readouts --------------------------------------------------------
    def _refresh_shares(self, group) -> None:
        """The Share column, computed WITHIN one group so two scales never share
        a denominator."""
        rows = self._groups.get(group, [])
        if not rows:
            return
        if group in self._no_share_groups:
            for _w, _en, pct in rows:
                try:
                    pct.configure(text="")
                except tk.TclError:
                    return
            return
        try:
            if group in self._direct_groups:
                for w_var, en_var, pct in rows:
                    on = en_var is None or en_var.get()
                    pct.configure(text=f"{w_var.get():.1f}%" if on else "0.0%")
                return
            total = sum(w.get() for w, en, _ in rows
                        if en is None or en.get())
            for w_var, en_var, pct in rows:
                on = en_var is None or en_var.get()
                if on and total > 0:
                    pct.configure(text=f"{(w_var.get() / total) * 100.0:.2f}%")
                else:
                    pct.configure(text="0.00%")
        except tk.TclError:
            return

    def _refresh_all_shares(self) -> None:
        for group in list(self._groups):
            self._refresh_shares(group)

    def _refresh_readouts(self) -> None:
        """The realised share and the budget spend, from the LAST BUILD's record,
        in EVERY per-world tab that shows them.

        When this session has not built yet the labels say so rather than showing
        a zero that would look measured.
        """
        try:
            target = float(self.share_var.get())
        except (tk.TclError, ValueError):
            target = float(getattr(self.config, "enemy_share_target", 0.20))
        for world, labels in self._readouts.items():
            rec = self.last_records.get(world) or {}
            for kind, label in labels:
                try:
                    if kind == "realised":
                        if "realised_share" in rec:
                            label.configure(
                                text=f"asked {target:.3f}  |  REALISED last "
                                     f"build {rec['realised_share']:.3f}  (gap "
                                     f"{rec['realised_share'] - target:+.3f})")
                        else:
                            label.configure(
                                text=f"asked {target:.3f}  |  REALISED: no "
                                     f"build in this session yet")
                    elif "budget" in rec:
                        label.configure(
                            text=f"Budget [{world}] {rec['budget']:,} B  |  "
                                 f"spent {rec.get('committed', 0):,} B  |  "
                                 f"left {rec.get('remaining', 0):,} B  |  "
                                 f"resident {rec.get('resident', 0):,} B = "
                                 f"{rec.get('occupancy', 0.0):.2f}% OCCUPANCY")
                    else:
                        label.configure(
                            text=f"Budget [{world}]: no build this session yet — "
                                 f"run once for the occupancy figure")
                except tk.TclError:
                    return

    def _on_share_changed(self, *_a) -> None:
        try:
            v = float(self.share_var.get())
        except (tk.TclError, ValueError):
            return
        if 0.0 < v < 1.0:
            self.config.enemy_share_target = round(v, 4)
        self._refresh_readouts()

    # ---- presets and persistence ----------------------------------------
    def _apply_harder_mode(self) -> None:
        """Harder Mode, through the same fields this tab edits (Req 1.7)."""
        share, disabled = apply_harder_mode_preset(self.config)
        self.share_var.set(share)
        self._build_tabs()
        messagebox.showinfo(
            "Harder Mode preset applied",
            f"Enemies forced to {share:.0%} of every pool. Your structure "
            f"selection and weights were NOT changed ({disabled} rows touched). "
            f"Press Reset to go back to using your weights exactly as typed.")

    def _reset(self) -> None:
        """Reset to stock: exactly the task-1 defaults, and nothing else in the
        config is touched."""
        from .spawn_config import reset_unified
        reset_unified(self.config)
        self.share_var.set(float(self.config.enemy_share_target))
        self._build_tabs()

    def _save(self) -> None:
        try:
            self.config.save(self.default_save_path)
            if self.on_save:
                self.on_save(self.default_save_path)
            self.win.destroy()
        except Exception as e:
            messagebox.showerror("Save Error",
                                 f"Failed to save the spawn rates:\n{e}")

    def _save_as(self) -> None:
        f = filedialog.asksaveasfilename(
            initialdir=str(Path(self.default_save_path).parent),
            initialfile=str(Path(self.default_save_path).name),
            filetypes=[("JSON files", "*.json"), ("All Files", "*.*")],
            defaultextension=".json")
        if f:
            try:
                self.config.save(f)
                if self.on_save:
                    self.on_save(f)
                self.win.destroy()
            except Exception as e:
                messagebox.showerror("Save Error",
                                     f"Failed to serialize file:\n{e}")


def main() -> None:
    _enable_dpi_awareness()
    root = tk.Tk()
    
    def _log_callback_exception(exc, val, tb):
        import traceback
        try:
            log_dir = (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent)
            with open(log_dir / "gui_errors.log", "a", encoding="utf-8") as f:
                f.write("\n--- Tk callback exception ---\n")
                traceback.print_exception(exc, val, tb, file=f)
        except Exception: pass
    root.report_callback_exception = _log_callback_exception

    app = RandomizerApp(root)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = getattr(app, "_scale", 1.0)
    w, h = int(1040 * scale), int(880 * scale)
    w, h = min(w, sw - 40), min(h, sh - 80)
    x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.mainloop()


if __name__ == "__main__":
    main()
