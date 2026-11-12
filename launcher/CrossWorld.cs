// Cross-world enemies for the debug menu's spawn tool (tools\cross_world_enemies.py, which runs the randomizer's own
// cross-world porting code from maximo1randomizer): when Play is pressed with the option on, every enemy the
// randomizer's allowlist lets into a world is ported into that world's .BEF/.PRS/.PRT in .\disc, so the debug menu can
// spawn it there. The originals are kept in .\disc_world_original and come back when the option is turned off.
// Sound: with a maximo_boot.exe that has PS2X_SPU2_RAM_MB (16 MB of emulated SPU2 sound RAM instead of the PS2's 2 MB),
// the tool also patches the sound driver (PSXIRX\MJOLNIR.IRX) for 1000 effects per bank, so every ported enemy keeps its
// sounds, and the game is started with PS2X_SPU2_RAM_MB=16. The six bosses (from their arena archives, with the
// randomizer's boss_inject port) are added to every world too, so the debug menu can spawn them in any level.
// Written for the C# 5 compiler that ships with Windows.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;

static class CrossWorld
{
    static readonly string BackupRoot = Path.Combine(Paths.Root, "disc_world_original");
    static string Stamp { get { return Path.Combine(BackupRoot, "crossworld_applied.txt"); } }
    const string Version = "8"; // fallback for the tool's VERSION (read from the script): 8 = structures, 7 = enemies, bosses, Queen waypoints, Grim, sorceresses with their SetMeshEffect resources; 175 palettes
    const string SoundRamMarker = "PS2X_SPU2_RAM_MB";
    const string PaletteMarker = "resource pool six times retail";
    static int soundRamMb;
    static bool? bigTables;

    static bool ExeHas(string text)
    {
        try
        {
            byte[] exe = File.ReadAllBytes(Paths.Game);
            byte[] marker = Encoding.ASCII.GetBytes(text);
            for (int i = 0; i + marker.Length <= exe.Length; i++)
            {
                if (exe[i] != marker[0]) continue;
                int j = 1;
                while (j < marker.Length && exe[i + j] == marker[j]) j++;
                if (j == marker.Length) return true;
            }
        }
        catch (Exception e) { Log.Write("cross-world: could not read " + Paths.Game + ": " + e.Message); }
        return false;
    }

    // A maximo_boot.exe whose RESOURCE pool is six retail pools: room for Grim and the four sorceresses besides the
    // bosses. The tool then ports with up to 440 palettes and turns the smallest ported paletted textures into 24-bit
    // ones until each world is back at the game's 175.
    static bool BigTables
    {
        get { if (bigTables == null) bigTables = ExeHas(PaletteMarker); return bigTables.Value; }
    }
    static string ToolVersion
    {
        get { return ScriptVersion() + "-" + SoundRamMb + (BigTables ? "-440-6" : "-175-4"); }
    }

    // 16 when this maximo_boot.exe can give the SPU2 more sound RAM, else 2 (the PS2's; ported enemies are then mostly
    // silent). Read once.
    public static int SoundRamMb
    {
        get
        {
            if (soundRamMb == 0)
            {
                soundRamMb = 2;
                try
                {
                    byte[] exe = File.ReadAllBytes(Paths.Game);
                    byte[] marker = Encoding.ASCII.GetBytes(SoundRamMarker);
                    for (int i = 0; i + marker.Length <= exe.Length && soundRamMb == 2; i++)
                    {
                        if (exe[i] != marker[0]) continue;
                        int j = 1;
                        while (j < marker.Length && exe[i + j] == marker[j]) j++;
                        if (j == marker.Length) soundRamMb = 16;
                    }
                }
                catch (Exception e) { Log.Write("cross-world: could not read " + Paths.Game + ": " + e.Message); }
            }
            return soundRamMb;
        }
    }
    // Set by the launcher window: shows a line in its status bar while the tool runs (null = back to "Playing").
    public static Action<string> OnProgress;

    static void Show(string text)
    {
        Action<string> show = OnProgress;
        if (show != null) show(text);
    }

    // The new files are the originals now (Set up from ISO / folder replaced .\disc).
    public static void ForgetBackup()
    {
        try { if (Directory.Exists(BackupRoot)) Directory.Delete(BackupRoot, true); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    static string Md5(string path)
    {
        using (var md5 = MD5.Create())
        using (var s = File.OpenRead(path))
        {
            var sb = new StringBuilder();
            foreach (byte b in md5.ComputeHash(s)) sb.Append(b.ToString("x2"));
            return sb.ToString();
        }
    }

    // True when the files the tool wrote last time are still the ones in .\disc.
    static bool Applied()
    {
        if (!File.Exists(Stamp)) return false;
        bool versionOk = false;
        foreach (string line in File.ReadAllLines(Stamp))
        {
            string[] kv = line.Split(new[] { '=' }, 2);
            if (kv.Length != 2) continue;
            if (kv[0] == "version") { versionOk = kv[1].Trim() == ToolVersion; continue; }
            string file = Path.Combine(Paths.Disc, kv[0].Replace('/', '\\'));
            if (!File.Exists(file) || Md5(file) != kv[1].Trim()) return false;
        }
        return versionOk;
    }

    // The VERSION line of tools\cross_world_enemies.py, so the launcher and the tool can never disagree (a mismatch made
    // every launch port the worlds again). Falls back to the constant when the script cannot be read.
    static string scriptVersion;
    static string ScriptVersion()
    {
        if (scriptVersion != null) return scriptVersion;
        scriptVersion = Version;
        try
        {
            string tool = Find(Path.Combine("tools", "cross_world_enemies.py"));
            if (tool != null)
                foreach (string line in File.ReadAllLines(tool))
                {
                    string t = line.Trim();
                    if (!t.StartsWith("VERSION")) continue;
                    int q1 = t.IndexOf('"'), q2 = q1 < 0 ? -1 : t.IndexOf('"', q1 + 1);
                    if (q2 > q1 + 1) { scriptVersion = t.Substring(q1 + 1, q2 - q1 - 1); break; }
                }
        }
        catch (Exception e) { Log.Write("cross-world: could not read the tool's VERSION: " + e.Message); }
        return scriptVersion;
    }

    static string Find(string relative)
    {
        foreach (string root in new[] { Paths.Root, Path.GetDirectoryName(Paths.Root) })
        {
            if (string.IsNullOrEmpty(root)) continue;
            string p = Path.Combine(root, relative);
            if (File.Exists(p) || Directory.Exists(p)) return p;
        }
        return null;
    }

    static string Restore()
    {
        // The backup has the disc's layout (PSXDATA\..., PSXIRX\MJOLNIR.IRX) next to the stamp and the lock file.
        int n = 0;
        if (Directory.Exists(BackupRoot))
        {
            foreach (string src in Directory.GetFiles(BackupRoot, "*", SearchOption.AllDirectories))
            {
                string rel = src.Substring(BackupRoot.Length).TrimStart('\\', '/');
                if (rel.IndexOfAny(new[] { '\\', '/' }) < 0) continue; // crossworld_applied.txt, crossworld.lock
                string dst = Path.Combine(Paths.Disc, rel);
                if (File.Exists(dst) && Md5(dst) == Md5(src)) continue;
                if (File.Exists(dst)) File.SetAttributes(dst, FileAttributes.Normal);
                File.Copy(src, dst, true);
                n++;
            }
        }
        File.Delete(Stamp);
        return "cross-world: original world files restored (" + n + ")";
    }

    // Runs the tool with the first Python found (the py launcher, then python). Returns its exit code, -1 = no Python.
    static int RunTool(string tool, string randomizer, string detailLog)
    {
        string args = "\"" + tool + "\" --disc \"" + Paths.Disc + "\" --randomizer \"" + randomizer + "\" --backup \"" +
                      BackupRoot + "\" --log \"" + detailLog + "\" --sound-ram-mb " + SoundRamMb +
                      (BigTables ? " --clut-table 440 --pool-scale 6" : "");
        foreach (string[] python in new[] { new[] { "py", "-3 " }, new[] { "python", "" }, new[] { "python3", "" } })
        {
            var info = new ProcessStartInfo(python[0], python[1] + args);
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            try
            {
                using (Process p = Process.Start(info))
                {
                    p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) Log.Write("cross-world: " + e.Data); };
                    p.BeginErrorReadLine();
                    string line;
                    while ((line = p.StandardOutput.ReadLine()) != null)
                    {
                        Log.Write(line);
                        if (line.StartsWith("cross-world: ") && line.Length > 13)
                            Show("Adding enemies from other worlds: " + line.Substring(13).Split(';')[0]);
                    }
                    p.WaitForExit();
                    return p.ExitCode;
                }
            }
            catch (Win32Exception) { } // not installed under this name
        }
        return -1;
    }

    // Returns one line for the launcher's log ("" = nothing to do).
    public static string Prepare(bool on)
    {
        if (!Directory.Exists(Path.Combine(Paths.Disc, "PSXDATA"))) return "";
        if (!on) return File.Exists(Stamp) ? Restore() : "";
        if (Applied()) return "cross-world: enemies from other worlds are in place";
        string tool = Find(Path.Combine("tools", "cross_world_enemies.py"));
        string randomizer = Find("maximo1randomizer");
        if (tool == null) return "cross-world: tools\\cross_world_enemies.py was not found next to the launcher or one folder up";
        if (randomizer == null) return "cross-world: the maximo1randomizer folder was not found next to the launcher or one folder up";
        Log.Write("cross-world: porting enemies between worlds (about a minute)...");
        Show("Adding enemies and bosses from other worlds to the game files (one or two minutes, only when needed)...");
        int code;
        try { code = RunTool(tool, randomizer, Path.Combine(Paths.Logs, "crossworld_detail.log")); }
        finally { Show(null); }
        if (code == 3) return "cross-world: another launcher is already adding the enemies; this game starts without waiting for it";
        if (code == -1) return "cross-world: Python 3 was not found (install it from python.org); the world files are unchanged";
        return code == 0 ? "cross-world: done" : "cross-world: finished with problems (exit code " + code + "), see the lines above";
    }
}
