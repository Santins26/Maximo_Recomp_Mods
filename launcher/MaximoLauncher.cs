// Maximo: Ghosts to Glory (PAL SLES-50703) native test build: a minimal launcher.
//
// It never contains game data. The player points it once at their own disc image (or an extracted copy of it); the
// launcher checks that it is the PAL disc (SHA-256 of the game executable), copies the disc files into .\disc, and then
// starts game\maximo_boot.exe with the same settings as the developer's play script (GPU renderer, 50 Hz, native audio,
// persistent memory card in .\memcard\mc0). Each session's logs go to .\logs\session-<date>-<time>.
//
// Built with the C# 5 compiler that ships with Windows (.NET Framework 4.8): see build.cmd.
// Experimental build: the Picture setting adds the enhance branch's upscaling (MAXIMO_SSAA, PS2X_PRESENT_FILTER).
// HD textures (experimental, HdTextures.cs): packs in .\textures, MAXIMO_HDTEX_PACKS for the game.
// Switches: --setup <iso, bin, cue or folder> (always: BUILD_GAME.cmd uses it); for testing --play <seconds>, --zip <notes>, --screenshot <png> [page],
// --hdtex-install <zip or folder>, --hdtex-marcus, --hdtex-screenshot <png>. Results go to launcher.log.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("Maximo Launcher")]
[assembly: AssemblyDescription("Launcher and builder for the unofficial Maximo PC port")]
[assembly: AssemblyCompany("Biorhythmic Studios")]
[assembly: AssemblyProduct("Maximo Launcher")]
[assembly: AssemblyCopyright("Copyright (C) 2026 Biorhythmic Studios. GPL-3.0.")]
[assembly: AssemblyVersion("1.0.3.0")]
[assembly: AssemblyFileVersion("1.0.3.0")]

static class Build
{
    public const string Name = "Maximo: Ghosts to Glory - PC port";
    public const string Version = "Maximo-GTG source build";
    // The release this launcher works with: the one the game in .\game was built from (game\region.txt, written by
    // build.cmd), or before the game is built, the one of the disc set up in .\disc (disc_region.txt, written when the
    // disc is set up). None: PAL. Read each time, so setting up a USA or Japanese disc and building it needs no restart.
    public static readonly string[] Regions = { "PAL", "USA", "JPN" };
    static readonly string[] ElfNames = { "SLES_507.03", "SLUS_200.17", "SLPM_621.27" };
    static readonly string[] ElfHashes = { "03615557E6291B882A54165E068F653C8613856852E4250F7DE4B075ABFC3C99",
                                           "12049EC55891ADFF413B85BCAAAC106567419146AE76CEEE88BC45E13E1B195F",
                                           "9F58B67B5029A93B30506B670D6C54D7C326A05A3A03780E796EA03C94A17F57" };
    static readonly string[] Titles = { "PAL (SLES-50703)", "USA (SLUS-20017)", "Japan (SLPM-62127)" };
    public static string BuiltRegion { get { return ReadRegion(Path.Combine(Paths.Root, "game", "region.txt")); } }
    public static string Region
    {
        get { return BuiltRegion ?? ReadRegion(Path.Combine(Paths.Root, "disc_region.txt")) ?? "PAL"; }
    }
    static int Index { get { return Math.Max(0, Array.IndexOf(Regions, Region)); } }
    public static string ElfName { get { return ElfNames[Index]; } }
    public static string ElfSha256 { get { return ElfHashes[Index]; } }
    public static string DiscTitle { get { return Titles[Index]; } }
    public static int DiscFileCount { get { return Region == "PAL" ? 294 : 0; } } // 0: not checked (only the PAL count is known)
    public static string ElfNameOf(string region) { return ElfNames[Math.Max(0, Array.IndexOf(Regions, region))]; }
    public static string ElfSha256Of(string region) { return ElfHashes[Math.Max(0, Array.IndexOf(Regions, region))]; }
    public static string TitleOf(string region) { return Titles[Math.Max(0, Array.IndexOf(Regions, region))]; }

    static string ReadRegion(string file)
    {
        try
        {
            string r = File.Exists(file) ? File.ReadAllText(file).Trim().ToUpperInvariant() : "";
            return Array.IndexOf(Regions, r) >= 0 ? r : null;
        }
        catch (Exception) { return null; }
    }
    public const int KeepSessions = 10;
}

static class Paths
{
    public static readonly string Root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
    public static readonly string Game = Path.Combine(Root, "game\\maximo_boot.exe");
    public static readonly string Disc = Path.Combine(Root, "disc");
    public static readonly string MemoryCard = Path.Combine(Root, "memcard\\mc0");
    public static readonly string Logs = Path.Combine(Root, "logs");
    public static readonly string Screenshots = Path.Combine(Root, "screenshots");
    public static readonly string LauncherLog = Path.Combine(Root, "launcher.log");
    public static readonly string Art = Path.Combine(Root, "art");
}

// The picture settings of this experimental build, saved in settings.ini next to the launcher.
static class Picture
{
    public static readonly string[] Names =
    {
        "Original: 640x512, the exact PS2 picture",
        "Sharper: double resolution (1280x1024), 4x supersampling",
        "Sharper: double resolution (1280x1024), 8x supersampling",
        "Sharpest: double resolution (1280x1024), 16x supersampling",
        "Ultra: 4x resolution (2560x1024), 16x samples",
        "Ultra + SMAA: 4x resolution, 16x samples, smoothed edges (new)",
    };
    // Ultra (4 and 5) scans out one sample per pixel (MAXIMO_SCANOUT=4); 5 adds SMAA anti-aliasing (MAXIMO_SMAA=1).
    static readonly string[] Ssaa = { null, "4", "8", "16", "16", "16" };
    public static int Mode = 3;
    public static bool Smooth = true;
    // How the picture is scaled to the window: sharp pixels, smooth (bilinear), or AMD FSR 1 on the graphics card
    // (MAXIMO_FSR=1: edge-adaptive upscaling plus sharpening to the exact window size). Saved as smooth= and fsr=.
    public static readonly string[] ScalingNames = { "Sharp pixels", "Smooth", "FSR (sharpest)" };
    public static bool Fsr = false;
    public static int Scaling { get { return Fsr ? 2 : Smooth ? 1 : 0; } set { Fsr = value == 2; Smooth = value != 0; } }
    // Experimental: VU1 (the 3D geometry unit) and the GS submission on their own thread (PS2X_VU1_THREAD). Off by default.
    public static bool Vu1Thread = false;
    // Experimental, with Faster 3D only: the 3D worker draws and shows frame N while the game works on frame N+1
    // (PS2X_VU1_PIPELINE). On by default.
    public static bool Vu1Pipeline = true;
    // Borderless fullscreen on the current monitor (PS2X_WINDOW_MODE=borderless); Alt+Enter switches in the game. Off by default.
    public static bool Borderless = false;
    // Skip the Capcom logos, copyright and title screen (MAXIMO_SKIP_INTRO=1): boots into the main menu. Off by default.
    public static bool SkipIntro = false;
    // Smooth effects (MAXIMO_SOFT_PARTICLES=1, with supersampling only): fire, smoke and sparkle particles are drawn at
    // the full upscaled resolution instead of in blocks of one PS2 pixel. On by default.
    public static bool SoftParticles = true;
    // VSync (PS2X_VSYNC=1): the window swaps on the monitor's refresh, so the picture does not tear. On by default.
    public static bool VSync = true;
    // Experimental widescreen (MAXIMO_ASPECT=16:9): the 3D view shows more at the sides. 4:3 by default.
    public static readonly string[] AspectNames = { "4:3 (original)", "16:9 widescreen (experimental)" };
    static readonly string[] AspectValues = { "4:3", "16:9" };
    public static int Aspect = 0;
    // Gameplay frame rate (MAXIMO_FRAME_RATE): the same game speed with more frames; the front end, attract demo and
    // movies stay at the release's own rate. PAL: 50 or 60 Hz; the USA and Japanese releases run at 60 Hz already.
    // (Higher rates - MAXIMO_FRAME_RATE=120 or refresh - are left to the environment variable for now.)
    public static readonly string[] FrameRateNames = Build.Region == "PAL"
        ? new[] { "50 Hz (original)", "60 Hz in gameplay (experimental)" }
        : new[] { "60 Hz (original)" };
    static readonly string[] FrameRateValues = Build.Region == "PAL" ? new[] { "50", "60" } : new[] { "60" };
    public static int FrameRate = 0;
    // Experimental object budget (MAXIMO_OBJECT_BUDGET=more): the game stops running a level's enemies and objects once
    // a busy level passes its per-type limit; this raises the limit so more of them stay alive. Original by default.
    public static readonly string[] ObjectBudgetNames = { "Original", "More enemies and objects (experimental)",
                                                          "Maximum enemies and objects (experimental)" };
    static readonly string[] ObjectBudgetValues = { "vanilla", "more", "1000" };
    public static int ObjectBudget = 0;
    // Experimental extra enemies (MoreEnemies.cs): when Play is pressed, copies of some of each level's own enemies are
    // written into the level files in .\disc (the originals are kept and come back with Original). The object limit
    // is raised with it so they all run. Original by default. Saved as enemies=.
    public static int Enemies = 0;
    // Experimental game memory (MAXIMO_RAM=more): the game's memory pools grow from about 28 MB to about 123 MB, for
    // levels with many more enemies (each extra enemy costs about 100 KB). Always on with extra enemies. Original by
    // default. Saved as memory= ("64" from earlier builds reads as Expanded).
    public static readonly string[] MemoryNames = { "Original (32 MB, like the PS2)", "Expanded (128 MB, experimental)" };
    static readonly string[] MemoryValues = { "32", "more" };
    public static int Memory = 0;
    // Cross-world enemies for the debug menu's spawn tool (CrossWorld.cs): when Play is pressed, enemies from the other
    // worlds are ported into each world's files in .\disc (the originals are kept and come back when this is turned
    // off). Needs Python 3 and the maximo1randomizer folder; turns Expanded memory on. Always on (no longer a setting).
    public static bool CrossWorld = true;
    // Experimental free camera (MAXIMO_CAMERA=orbit): the right stick turns the camera around Maximo, R3 swings it back
    // behind him. Speed in degrees per second at full stick (MAXIMO_CAMERA_SPEED), seconds without camera input before
    // it swings back by itself (MAXIMO_CAMERA_RETURN, 0 = never), up/down inverted (MAXIMO_CAMERA_INVERT_Y). Off by default.
    public static bool FreeCamera = false;
    public static readonly string[] CameraSpeedNames = { "Slow", "Normal", "Fast", "Very fast" };
    static readonly int[] CameraSpeedValues = { 90, 150, 220, 300 };
    public static int CameraSpeed = 1;
    public static readonly string[] CameraReturnNames = { "Never", "After 2 seconds", "After 3 seconds", "After 5 seconds", "After 8 seconds" };
    static readonly int[] CameraReturnValues = { 0, 2, 3, 5, 8 };
    public static int CameraReturn = 2;
    public static bool CameraInvertY = false;
    static string File_ { get { return Path.Combine(Paths.Root, "settings.ini"); } }

    public static void Load()
    {
        try
        {
            if (!File.Exists(File_)) return;
            foreach (string line in File.ReadAllLines(File_))
            {
                string[] kv = line.Split(new[] { '=' }, 2);
                if (kv.Length != 2) continue;
                int n;
                if (kv[0].Trim() == "picture" && int.TryParse(kv[1].Trim(), out n) && n >= 0 && n < Names.Length) Mode = n;
                if (kv[0].Trim() == "smooth") Smooth = kv[1].Trim() != "0";
                if (kv[0].Trim() == "fsr") Fsr = kv[1].Trim() == "1";
                if (kv[0].Trim() == "vu1thread") Vu1Thread = kv[1].Trim() == "1";
                if (kv[0].Trim() == "vu1pipeline") Vu1Pipeline = kv[1].Trim() != "0";
                if (kv[0].Trim() == "borderless") Borderless = kv[1].Trim() == "1";
                if (kv[0].Trim() == "skipintro") SkipIntro = kv[1].Trim() == "1";
                if (kv[0].Trim() == "softfx") SoftParticles = kv[1].Trim() != "0";
                if (kv[0].Trim() == "vsync") VSync = kv[1].Trim() != "0";
                if (kv[0].Trim() == "aspect") Aspect = Math.Max(0, Array.IndexOf(AspectValues, kv[1].Trim()));
                if (kv[0].Trim() == "framerate") FrameRate = Math.Max(0, Array.IndexOf(FrameRateValues, kv[1].Trim()));
                // (The object limit, extra enemies and memory are no longer settings: Original, Original and Expanded with
                // the cross-world enemies, which are always on. Older settings files' values are ignored.)
                if (kv[0].Trim() == "camera") FreeCamera = kv[1].Trim() == "1";
                if (kv[0].Trim() == "cameraspeed" && int.TryParse(kv[1].Trim(), out n) && Array.IndexOf(CameraSpeedValues, n) >= 0)
                    CameraSpeed = Array.IndexOf(CameraSpeedValues, n);
                if (kv[0].Trim() == "camerareturn" && int.TryParse(kv[1].Trim(), out n) && Array.IndexOf(CameraReturnValues, n) >= 0)
                    CameraReturn = Array.IndexOf(CameraReturnValues, n);
                if (kv[0].Trim() == "camerainvert") CameraInvertY = kv[1].Trim() == "1";
                HdTextures.Load(kv[0].Trim(), kv[1].Trim());
                Randomizer.Load(kv[0].Trim(), kv[1].Trim());
            }
        }
        catch (IOException) { }
    }

    public static void Save()
    {
        try { File.WriteAllText(File_, "picture=" + Mode + "\r\nsmooth=" + (Smooth ? 1 : 0) + "\r\nfsr=" + (Fsr ? 1 : 0) +
                                        "\r\nvu1thread=" + (Vu1Thread ? 1 : 0) +
                                        "\r\nvu1pipeline=" + (Vu1Pipeline ? 1 : 0) +
                                        "\r\nborderless=" + (Borderless ? 1 : 0) +
                                        "\r\nskipintro=" + (SkipIntro ? 1 : 0) +
                                        "\r\nsoftfx=" + (SoftParticles ? 1 : 0) +
                                        "\r\nvsync=" + (VSync ? 1 : 0) +
                                        "\r\naspect=" + AspectValues[Aspect] +
                                        "\r\nframerate=" + FrameRateValues[FrameRate] +
                                        "\r\nobjectbudget=" + ObjectBudgetValues[ObjectBudget] +
                                        "\r\nenemies=" + MoreEnemies.Keys[Enemies] +
                                        "\r\nmemory=" + MemoryValues[Memory] +
                                        "\r\ncrossworld=" + (CrossWorld ? 1 : 0) +
                                        "\r\ncamera=" + (FreeCamera ? 1 : 0) +
                                        "\r\ncameraspeed=" + CameraSpeedValues[CameraSpeed] +
                                        "\r\ncamerareturn=" + CameraReturnValues[CameraReturn] +
                                        "\r\ncamerainvert=" + (CameraInvertY ? 1 : 0) + "\r\n" + HdTextures.SaveLines() + Randomizer.SaveLines()); }
        catch (IOException) { }
    }

    public static string Describe()
    {
        return (Ssaa[Mode] == null ? "original resolution" : (Mode >= 4 ? "4x resolution, " : "2x resolution, ") + Ssaa[Mode] + "x SSAA" + (Mode == 5 ? ", SMAA" : "")) +
               (Fsr ? ", FSR scaling" : Smooth ? ", smooth scaling" : ", sharp scaling") + (Vu1Thread ? ", 3D on a second CPU core" + (Vu1Pipeline ? " with overlapped frames" : "") : "") +
               (Borderless ? ", borderless fullscreen" : "") + (SoftParticles && Ssaa[Mode] != null ? ", smooth effects" : "") + (VSync ? ", VSync" : "") + (Aspect == 1 ? ", 16:9 widescreen" : "") +
               (FrameRate > 0 ? (FrameRateValues[FrameRate] == "refresh" ? ", monitor-rate gameplay" : ", " + FrameRateValues[FrameRate] + " Hz gameplay") : "") + (ObjectBudget == 1 ? ", more enemies and objects" : ObjectBudget == 2 ? ", maximum enemies and objects" : "") +
               (Enemies > 0 ? ", +" + MoreEnemies.Keys[Enemies] + "% enemies" : "") +
                (CrossWorld ? ", cross-world enemies" : "") +
                (Memory == 1 || Enemies > 0 || CrossWorld ? ", 128 MB memory" : "") +
               (FreeCamera ? ", free camera (" + CameraSpeedNames[CameraSpeed].ToLowerInvariant() + ", back " +
                             (CameraReturnValues[CameraReturn] == 0 ? "never" : "after " + CameraReturnValues[CameraReturn] + " s") +
                             (CameraInvertY ? ", inverted" : "") + ")" : "") + Randomizer.Describe() + HdTextures.Describe();
    }

    // The settings of the developer's play_enhanced.cmd; a value already in the environment wins.
    public static void Apply(ProcessStartInfo info)
    {
        if (Ssaa[Mode] != null && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_SSAA")))
            info.EnvironmentVariables["MAXIMO_SSAA"] = Ssaa[Mode];
        if (Mode >= 4 && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_SCANOUT")))
            info.EnvironmentVariables["MAXIMO_SCANOUT"] = "4";
        if (Mode == 5 && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_SMAA")))
            info.EnvironmentVariables["MAXIMO_SMAA"] = "1";
        if (Smooth && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_PRESENT_FILTER")))
            info.EnvironmentVariables["PS2X_PRESENT_FILTER"] = "linear";
        if (Fsr && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_FSR")))
            info.EnvironmentVariables["MAXIMO_FSR"] = "1";
        if (Vu1Thread && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_VU1_THREAD")))
            info.EnvironmentVariables["PS2X_VU1_THREAD"] = "1";
        if (Vu1Thread && Vu1Pipeline && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_VU1_PIPELINE")))
            info.EnvironmentVariables["PS2X_VU1_PIPELINE"] = "1";
        if (SkipIntro && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_SKIP_INTRO")))
            info.EnvironmentVariables["MAXIMO_SKIP_INTRO"] = "1";
        if (Borderless && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_WINDOW_MODE")))
            info.EnvironmentVariables["PS2X_WINDOW_MODE"] = "borderless";
        info.EnvironmentVariables["PS2X_SHOT_DIR"] = Paths.Screenshots;
        if (VSync && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_VSYNC")))
            info.EnvironmentVariables["PS2X_VSYNC"] = "1";
        if (SoftParticles && Ssaa[Mode] != null && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_SOFT_PARTICLES")))
            info.EnvironmentVariables["MAXIMO_SOFT_PARTICLES"] = "1";
        if (Aspect == 1 && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_ASPECT")))
            info.EnvironmentVariables["MAXIMO_ASPECT"] = "16:9";
        if (FrameRate > 0 && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_FRAME_RATE")))
            info.EnvironmentVariables["MAXIMO_FRAME_RATE"] = FrameRateValues[FrameRate];
        if (FreeCamera && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_CAMERA")))
        {
            info.EnvironmentVariables["MAXIMO_CAMERA"] = "orbit";
            info.EnvironmentVariables["MAXIMO_CAMERA_SPEED"] = CameraSpeedValues[CameraSpeed].ToString();
            info.EnvironmentVariables["MAXIMO_CAMERA_RETURN"] = CameraReturnValues[CameraReturn].ToString();
            info.EnvironmentVariables["MAXIMO_CAMERA_INVERT_Y"] = CameraInvertY ? "1" : "0";
        }
        // Extra enemies only show up if the object limit lets them run, so it is raised with them.
        // The budget is per type and the original is 100, so it follows the enemy multiplier with room to spare
        // (triple enemies -> 400). Maximum is the recomp's ceiling, 1000.
        if ((ObjectBudget > 0 || Enemies > 0 || Randomizer.Enabled) && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_OBJECT_BUDGET")))
        {
            int budget = ObjectBudget == 2 ? 1000 : Randomizer.Enabled ? Randomizer.ObjectBudget : 200;
            budget = Math.Max(budget, Math.Min(1000, (int)Math.Ceiling(100.0 * (MoreEnemies.Multipliers[Enemies] + 1.0))));
            info.EnvironmentVariables["MAXIMO_OBJECT_BUDGET"] = budget.ToString(System.Globalization.CultureInfo.InvariantCulture);
        }
        // Extra enemies need memory for their models and AI; the PS2's 32 MB runs out first (10x crashed the intro).
        // Cross-world enemies with every sound: the sound banks need more than the PS2's 2 MB of SPU2 sound RAM.
        if ((CrossWorld || Randomizer.Enabled) && global::CrossWorld.SoundRamMb > 2 && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_SPU2_RAM_MB")))
            info.EnvironmentVariables["PS2X_SPU2_RAM_MB"] = global::CrossWorld.SoundRamMb.ToString(System.Globalization.CultureInfo.InvariantCulture);
        if ((Memory == 1 || Enemies > 0 || CrossWorld || Randomizer.Enabled) && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_RAM")))
            info.EnvironmentVariables["MAXIMO_RAM"] = "more";
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_RANDOMIZER")))
            Randomizer.Apply(info);
        string packs = HdTextures.PackList();
        if (packs.Length > 0 && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MAXIMO_HDTEX_PACKS")))
            info.EnvironmentVariables["MAXIMO_HDTEX_PACKS"] = packs;
    }
}

static class Log
{
    static readonly object Gate = new object();
    public static void Write(string text)
    {
        lock (Gate)
        {
            try { File.AppendAllText(Paths.LauncherLog, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + text + "\r\n"); }
            catch (IOException) { }
        }
    }
}

static class Setup
{
    public static string Sha256(string path)
    {
        using (var sha = SHA256.Create())
        using (var stream = File.OpenRead(path))
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "");
    }

    public static bool DiscReady(out string why)
    {
        string elf = Path.Combine(Paths.Disc, Build.ElfName);
        if (!File.Exists(elf)) { why = "Game files not set up yet."; return false; }
        int count = Directory.GetFiles(Paths.Disc, "*", SearchOption.AllDirectories).Length;
        if (Build.DiscFileCount != 0 && count != Build.DiscFileCount)
        {
            why = "The game folder is incomplete (" + count + " of " + Build.DiscFileCount + " files). Set it up again.";
            return false;
        }
        why = "Game files ready (" + Build.DiscTitle + ").";
        return true;
    }

    static string PowerShell(string script)
    {
        var info = new ProcessStartInfo("powershell.exe",
            "-NoProfile -NonInteractive -Command \"" + script.Replace("\"", "\\\"") + "\"");
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        using (var p = Process.Start(info))
        {
            string output = p.StandardOutput.ReadToEnd();
            string error = p.StandardError.ReadToEnd();
            p.WaitForExit();
            if (p.ExitCode != 0)
                throw new Exception("Windows could not open the disc image: " + error.Trim());
            return output;
        }
    }

    static string Quote(string path) { return "'" + path.Replace("'", "''") + "'"; }

    // Copies a verified disc (an ISO, which Windows mounts read-only; a .bin/.cue, which is read here; or an
    // extracted folder) into .\disc.
    public static void Install(string source, Action<string> progress)
    {
        // Any disc image (.iso, .bin, .cue) is read here, into a folder next to .\disc: Windows cannot mount a .bin/.cue,
        // and gave some ISOs no drive letter. Only an ISO this reader cannot open (a DVD with no ISO 9660 file system)
        // is still mounted by Windows.
        bool isIso = false;
        bool mountedHere = false;
        string root = source, extracted = null;
        if (File.Exists(source) && DiscImage.IsImage(source))
        {
            progress("Opening the disc image...");
            extracted = Paths.Disc + ".image";
            try
            {
                if (Directory.Exists(extracted)) Directory.Delete(extracted, true);
                int n = DiscImage.Extract(source, extracted, progress);
                Log.Write("disc image " + source + ": " + n + " files read out");
                root = extracted;
            }
            catch (Exception e)
            {
                Log.Write("disc image " + source + " could not be read here (" + e.Message + ")");
                try { if (Directory.Exists(extracted)) Directory.Delete(extracted, true); } catch (Exception) { }
                extracted = null;
                isIso = source.EndsWith(".iso", StringComparison.OrdinalIgnoreCase);
                if (!isIso) throw;
            }
        }
        if (isIso)
        {
            progress("Opening the disc image...");
            string output = PowerShell(
                "$i = Get-DiskImage -ImagePath " + Quote(source) + "; $m = 0; " +
                "if (-not $i.Attached) { $i = Mount-DiskImage -ImagePath " + Quote(source) + " -Access ReadOnly -PassThru; $m = 1 }; " +
                "Start-Sleep -Milliseconds 500; 'MOUNTED=' + $m; 'DRIVE=' + ($i | Get-Volume).DriveLetter");
            foreach (string line in output.Split('\n'))
            {
                string l = line.Trim();
                if (l == "MOUNTED=1") mountedHere = true;
                if (l.StartsWith("DRIVE=") && l.Length > 6) root = l.Substring(6) + ":\\";
            }
            if (root == source)
                throw new Exception("Windows mounted the image but gave it no drive letter.");
        }
        else if (extracted == null && !Directory.Exists(source))
            throw new Exception("Choose a disc image (.iso, .bin or .cue) or a folder with the extracted disc.");

        try
        {
            // Which release this disc is: once the game is built, it has to be that one; before, any of the three
            // (the Build button then builds for it).
            string built = Build.BuiltRegion, region = null;
            foreach (string r in Build.Regions)
                if (File.Exists(Path.Combine(root, Build.ElfNameOf(r))) && (built == null || built == r)) { region = r; break; }
            if (region == null)
                throw new Exception(built != null
                    ? "This is not the " + Build.TitleOf(built) + " Maximo disc: " + Build.ElfNameOf(built) + " is missing. " +
                      "The game here was built for Maximo: Ghosts to Glory, " + Build.TitleOf(built) + "."
                    : "This is not a Maximo: Ghosts to Glory disc (PAL SLES-50703, USA SLUS-20017 or Japan SLPM-62127).");
            string elf = Path.Combine(root, Build.ElfNameOf(region));
            progress("Checking the disc...");
            string hash = Sha256(elf);
            Log.Write("disc source " + source + " (" + root + "), " + Build.ElfNameOf(region) + " sha256 " + hash);
            if (!string.Equals(hash, Build.ElfSha256Of(region), StringComparison.OrdinalIgnoreCase))
                throw new Exception("This disc's game executable does not match the " + Build.TitleOf(region) + " release (v1.00). " +
                                    "Other versions are not supported by this build.");

            string[] files = Directory.GetFiles(root, "*", SearchOption.AllDirectories);
            string temp = Paths.Disc + ".partial";
            if (Directory.Exists(temp)) Directory.Delete(temp, true);
            long total = files.Sum(f => new FileInfo(f).Length), done = 0;
            for (int i = 0; i < files.Length; ++i)
            {
                string relative = files[i].Substring(root.Length).TrimStart('\\', '/');
                string target = Path.Combine(temp, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                File.Copy(files[i], target, true);
                File.SetAttributes(target, FileAttributes.Normal); // files copied from a disc are read-only
                done += new FileInfo(target).Length;
                progress("Copying game files: " + (100 * done / Math.Max(1, total)) + "% (" + (i + 1) + " of " + files.Length + ")");
            }
            if (Directory.Exists(Paths.Disc)) Directory.Delete(Paths.Disc, true);
            Directory.Move(temp, Paths.Disc);
            MoreEnemies.ForgetBackup(); // the new files are the originals now
            CrossWorld.ForgetBackup();
            Randomizer.ForgetBackup();
            File.WriteAllText(Path.Combine(Paths.Root, "disc_region.txt"), region); // (Build.Region before a build)
            Log.Write("disc release: " + Build.TitleOf(region));
            string why;
            if (!DiscReady(out why))
                throw new Exception(why);
            Log.Write("disc installed: " + files.Length + " files, " + total + " bytes");
        }
        finally
        {
            if (extracted != null)
            {
                try { Directory.Delete(extracted, true); }
                catch (Exception e) { Log.Write("could not remove " + extracted + ": " + e.Message); }
            }
            if (mountedHere)
            {
                try { PowerShell("Dismount-DiskImage -ImagePath " + Quote(source) + " | Out-Null"); }
                catch (Exception e) { Log.Write("dismount failed: " + e.Message); }
            }
        }
    }
}

static class Game
{
    public static string LatestSession()
    {
        if (!Directory.Exists(Paths.Logs)) return null;
        return Directory.GetDirectories(Paths.Logs, "session-*").OrderBy(d => d).LastOrDefault();
    }

    static void PruneSessions()
    {
        if (!Directory.Exists(Paths.Logs)) return;
        var sessions = Directory.GetDirectories(Paths.Logs, "session-*").OrderBy(d => d).ToList();
        for (int i = 0; i + Build.KeepSessions < sessions.Count; ++i)
        {
            try { Directory.Delete(sessions[i], true); }
            catch (IOException) { }
        }
    }

    // Starts the game and waits for it to end. Returns a one-line summary from its log.
    public static string Run(double seconds, bool recordAudio)
    {
        PruneSessions();
        string session = Path.Combine(Paths.Logs, "session-" + DateTime.Now.ToString("yyyyMMdd-HHmmss"));
        Directory.CreateDirectory(session);
        Directory.CreateDirectory(Paths.MemoryCard);
        // Extra enemies: write the changed level files into .\disc (or put the originals back) before the game reads them.
        try
        {
            string enemyNote = MoreEnemies.Prepare(MoreEnemies.Multipliers[Picture.Enemies]);
            if (enemyNote.Length > 0) Log.Write(enemyNote);
        }
        catch (Exception e)
        {
            Log.Write("enemies: could not prepare the level files: " + e.Message);
        }
        // Randomizer (Randomizer.cs) or the debug menu's cross-world enemies (CrossWorld.cs): both start from the original
        // world files, so the one that is off puts them back first. The randomizer brings its own cross-world enemies.
        try
        {
            // (the randomizer runs inside the game; it needs every class in every world: the cross-world files)
            string rndNote = Randomizer.Prepare();
            if (rndNote.Length > 0) Log.Write(rndNote);
            string crossNote = CrossWorld.Prepare(Picture.CrossWorld || Randomizer.Enabled);
            if (crossNote.Length > 0) Log.Write(crossNote);
        }
        catch (Exception e)
        {
            Log.Write("cross-world/randomizer: could not prepare the world files: " + e.Message);
        }

        var info = new ProcessStartInfo(Paths.Game,
            "\"" + Paths.Disc + "\" " + seconds.ToString(System.Globalization.CultureInfo.InvariantCulture) + " boot_log.txt");
        info.WorkingDirectory = session;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        // The settings of the developer's play_with_audio.cmd.
        info.EnvironmentVariables["MAXIMO_WINDOW"] = "1";
        info.EnvironmentVariables["MAXIMO_GS_BACKEND"] = "gpu";
        info.EnvironmentVariables["MAXIMO_AUDIO"] = "1";
        info.EnvironmentVariables["MAXIMO_MC_ROOT"] = Paths.MemoryCard;
        // One present per 50 Hz VBlank (PRESENT_PACING.md); a value already in the environment (e.g. "off") wins.
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_PRESENT_PACING")))
            info.EnvironmentVariables["PS2X_PRESENT_PACING"] = "deadline";
        if (recordAudio)
            info.EnvironmentVariables["MAXIMO_AUDIO_WAV"] = "audio.wav";
        Picture.Apply(info);
        Bindings.Apply(info);

        Log.Write("play: session " + Path.GetFileName(session) + ", picture: " + Picture.Describe() + Bindings.Describe() +
                  (recordAudio ? ", recording audio" : ""));
        var started = DateTime.Now;
        int exitCode;
        using (var stdout = new StreamWriter(Path.Combine(session, "stdout.txt")))
        using (var p = new Process())
        {
            p.StartInfo = info;
            DataReceivedEventHandler sink = (s, e) => { if (e.Data != null) lock (stdout) stdout.WriteLine(e.Data); };
            p.OutputDataReceived += sink;
            p.ErrorDataReceived += sink;
            p.Start();
            Running = p;
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            // The game's last log line is ShutdownMarker. A process still alive well after it is hanging in process
            // exit: that is logged (once) for bug reports. The process is left alone; the player can end it.
            string bootLog = Path.Combine(session, "boot_log.txt");
            DateTime? shutdownSeen = null;
            bool hangLogged = false;
            while (!p.WaitForExit(2000))
            {
                if (shutdownSeen == null && LogEndsWithShutdown(bootLog))
                    shutdownSeen = DateTime.Now;
                if (shutdownSeen != null && !hangLogged && (DateTime.Now - shutdownSeen.Value).TotalSeconds >= 10)
                {
                    Log.Write("warning: the game finished shutting down but its process (" + p.Id + ") is still running after 10 s");
                    hangLogged = true;
                }
            }
            p.WaitForExit(); // the redirected output is complete
            Running = null;
            exitCode = p.ExitCode;
            if (hangLogged)
                Log.Write("the game process exited " + (DateTime.Now - shutdownSeen.Value).TotalSeconds.ToString("0") +
                          " s after it finished shutting down");
        }
        double minutes = (DateTime.Now - started).TotalMinutes;
        string summary = Summarise(Path.Combine(session, "boot_log.txt"));
        string hd = HdSummary(Path.Combine(session, "stdout.txt"));
        if (hd.Length > 0) summary += (summary.Length > 0 ? ", " : "") + hd;
        string line = "Last session: " + minutes.ToString("0.0") + " min" + (summary.Length > 0 ? ", " + summary : "") +
                      (exitCode != 0 ? " (game exit code " + exitCode + ")" : "");
        Log.Write(line);
        if (minutes < 0.25)
            line += ". The game closed almost at once: please use 'Zip logs for a bug report' and send the zip.";
        return line;
    }

    // The game process while it runs, for the launcher's own exit.
    public static volatile Process Running;
    const string ShutdownMarker = "shutdown: log complete, exiting process";

    static bool LogEndsWithShutdown(string logPath)
    {
        try
        {
            using (var stream = new FileStream(logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            {
                stream.Seek(Math.Max(0, stream.Length - 4096), SeekOrigin.Begin);
                return new StreamReader(stream).ReadToEnd().Contains(ShutdownMarker);
            }
        }
        catch (IOException) { return false; }
    }

    // Reads the end-of-run lines: game speed against PAL 50 Hz and audio underruns.
    static string Summarise(string logPath)
    {
        if (!File.Exists(logPath)) return "no log written";
        string tail;
        using (var stream = new FileStream(logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            long start = Math.Max(0, stream.Length - 512 * 1024);
            stream.Seek(start, SeekOrigin.Begin);
            tail = new StreamReader(stream).ReadToEnd();
        }
        var parts = new List<string>();
        foreach (string l in tail.Split('\n'))
        {
            int i = l.IndexOf("[perf] whole run guest vblanks");
            if (i >= 0)
            {
                int s = l.IndexOf("speed ");
                if (s >= 0) parts.Add("average speed " + l.Substring(s + 6).Trim());
            }
            i = l.IndexOf("[audio] host playback: ");
            if (i >= 0) parts.Add("audio " + l.Substring(i + 23).Trim());
        }
        return string.Join(", ", parts);
    }

    // "[hdtex] session: N of M textures looked at had a replacement; K loaded (X MB)" -> "HD textures: K loaded (X MB)".
    static string HdSummary(string path)
    {
        if (!File.Exists(path)) return "";
        try
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            {
                stream.Seek(Math.Max(0, stream.Length - 64 * 1024), SeekOrigin.Begin);
                foreach (string l in new StreamReader(stream).ReadToEnd().Split('\n'))
                {
                    int i = l.IndexOf("[hdtex] session: ");
                    int j = l.IndexOf("; ");
                    if (i >= 0 && j > i) return "HD textures: " + l.Substring(j + 2).Trim();
                }
            }
        }
        catch (IOException) { }
        return "";
    }

    public static string ZipLatest(string notes)
    {
        string session = LatestSession();
        if (session == null) throw new Exception("There is no session to zip yet: play first.");
        string zip = Path.Combine(Paths.Logs, "maximo-report-" + Path.GetFileName(session).Substring(8) + ".zip");
        if (File.Exists(zip)) File.Delete(zip);
        using (var archive = ZipFile.Open(zip, ZipArchiveMode.Create))
        {
            foreach (string f in Directory.GetFiles(session))
                archive.CreateEntryFromFile(f, Path.GetFileName(f), CompressionLevel.Optimal);
            // The problem may have been in an earlier session: add the logs (not audio recordings) of the two before.
            var earlier = Directory.GetDirectories(Paths.Logs, "session-*").OrderBy(d => d)
                                   .Where(d => string.CompareOrdinal(d, session) < 0).ToList();
            foreach (string previous in earlier.Skip(Math.Max(0, earlier.Count - 2)))
                foreach (string f in Directory.GetFiles(previous))
                    if (!f.EndsWith(".wav", StringComparison.OrdinalIgnoreCase))
                        archive.CreateEntryFromFile(f, "earlier/" + Path.GetFileName(previous) + "/" + Path.GetFileName(f),
                                                    CompressionLevel.Optimal);
            if (File.Exists(Paths.LauncherLog))
                archive.CreateEntryFromFile(Paths.LauncherLog, "launcher.log", CompressionLevel.Optimal);
            using (var w = new StreamWriter(archive.CreateEntry("notes.txt").Open(), new UTF8Encoding(false)))
            {
                w.WriteLine("Build: " + Build.Version);
                w.WriteLine("Picture: " + Picture.Describe() + Bindings.Describe());
                if (File.Exists(Bindings.File_))
                    w.WriteLine("Controls file:\r\n" + File.ReadAllText(Bindings.File_));
                w.WriteLine("Session: " + Path.GetFileName(session) + ", zipped " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
                w.WriteLine();
                w.WriteLine(string.IsNullOrWhiteSpace(notes) ? "(no description given)" : notes.Trim());
            }
        }
        Log.Write("zipped " + zip);
        return zip;
    }
}

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        Picture.Load();
        Bindings.Load();
        try
        {
            if (args.Length == 2 && args[0] == "--setup")
            {
                Setup.Install(args[1], s => { });
                return 0;
            }
#if DEV_SWITCHES
            if (args.Length == 2 && args[0] == "--screenshot-controls")
            {
                Application.EnableVisualStyles();
                using (var form = new ControlsForm())
                {
                    form.Show();
                    Application.DoEvents();
                    using (var bitmap = new Bitmap(form.Width, form.Height))
                    {
                        form.DrawToBitmap(bitmap, new Rectangle(0, 0, form.Width, form.Height));
                        bitmap.Save(args[1], System.Drawing.Imaging.ImageFormat.Png);
                    }
                }
                return 0;
            }
            if (args.Length >= 1 && args[0] == "--build")
            {
                Builder.ForceOwnGit = args.Length > 1 && args[1] == "--own-git";
                Builder.Run((text, percent) => { if (percent <= 0) Console.WriteLine("== " + text); }, Console.WriteLine);
                return 0;
            }
            if (args.Length == 2 && args[0] == "--signed")
            {
                Console.WriteLine(Builder.SignedByMicrosoft(args[1]) ? "signed" : "not signed");
                return 0;
            }
#endif
            if (args.Length >= 2 && args[0] == "--screenshot")
            {
                Application.EnableVisualStyles();
                using (var form = new LauncherForm())
                {
                    form.Show();
                    if (args.Length > 2) form.ShowPage(int.Parse(args[2]));
                    Application.DoEvents();
                    using (var bitmap = new Bitmap(form.Width, form.Height))
                    {
                        form.DrawToBitmap(bitmap, new Rectangle(0, 0, form.Width, form.Height));
                        bitmap.Save(args[1], System.Drawing.Imaging.ImageFormat.Png);
                    }
                }
                return 0;
            }
            if (args.Length == 2 && args[0] == "--hdtex-install")
            {
                Log.Write("hdtex-install: " + HdTextures.InstallFrom(args[1], s => Console.WriteLine(s)));
                return 0;
            }
            if (args.Length == 1 && args[0] == "--hdtex-marcus")
            {
                Log.Write("hdtex-marcus: " + HdTextures.InstallMarcus(s => Console.WriteLine(s)));
                return 0;
            }
            if (args.Length == 2 && args[0] == "--hdtex-screenshot")
            {
                Application.EnableVisualStyles();
                using (var form = new HdTexturesForm())
                {
                    form.Show();
                    Application.DoEvents();
                    using (var bitmap = new Bitmap(form.Width, form.Height))
                    {
                        form.DrawToBitmap(bitmap, new Rectangle(0, 0, form.Width, form.Height));
                        bitmap.Save(args[1], System.Drawing.Imaging.ImageFormat.Png);
                    }
                }
                return 0;
            }
            if (args.Length == 2 && args[0] == "--zip")
            {
                Game.ZipLatest(args[1]);
                return 0;
            }
            if (args.Length == 2 && args[0] == "--play")
            {
                Game.Run(double.Parse(args[1], System.Globalization.CultureInfo.InvariantCulture), false);
                return 0;
            }
        }
        catch (Exception e)
        {
            Log.Write("error: " + e);
            return 1;
        }
        Log.Write("launcher started, build " + Build.Version);
        // The mod package's launcher once the game is built from it: the built launcher in .\source\dist is the one
        // that plays (its game, disc and saves live there).
        if (BuildGame.Available && !File.Exists(Paths.Game) && BuildGame.Built && BuildGame.OpenBuilt())
            return 0;
        Application.EnableVisualStyles();
        Application.Run(new LauncherForm());
        return 0;
    }
}
