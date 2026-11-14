// The Build button: fetches the tools and the source, then compiles the game from the player's own disc on this PC.
// Everything it makes stays in .\source, .\tools and .\game. The game is never downloaded, because it can't be shared.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

static class Builder
{
    // The modded source (survival, debug menu, USA/Japanese support, cross-world enemies...). Its maximo1randomizer
    // folder (the randomizer's Python code, no game files) and tools\cross_world_enemies.py are kept after the build
    // for the cross-world enemies. MAXIMO_SOURCE_URL overrides it (another fork or a local path).
    const string DefaultSourceUrl = "https://github.com/Santins26/Maximo_Recomp_Mods.git";
    static string SourceUrl
    {
        get
        {
            string v = Environment.GetEnvironmentVariable("MAXIMO_SOURCE_URL");
            return string.IsNullOrEmpty(v) ? DefaultSourceUrl : v;
        }
    }
    const string GitUrl = "https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip";
    const string GitSha256 = "E3BF78351FE777FBA47DDEE5950082F8A98BB0F02604FDC7BA3EE2CB68FFA877";
    const string CppToolsUrl = "https://aka.ms/vs/18/stable/vs_BuildTools.exe";
    const string CppToolsArgs = "--passive --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended " +
                                "--add Microsoft.VisualStudio.Component.VC.CMake.Project";
    const int MaxRootLength = 100;
    const long GB = 1024L * 1024 * 1024;
    const long BuildSpace = 4 * GB, CppToolsSpace = 8 * GB;

    static readonly string Source = Path.Combine(Paths.Root, "source");
    static readonly string Tools = Path.Combine(Paths.Root, "tools");
    static readonly string OwnGit = Path.Combine(Tools, "git");
    static readonly string BuildLog = Path.Combine(Paths.Logs, "build.log");

    static readonly object Gate = new object();
    static Process current;
    static volatile bool cancelled;

    // Tests on a PC with Git installed use this to exercise the downloaded copy.
    public static bool ForceOwnGit;

    public static bool NeedsCppTools { get { return CppToolsFolder() == null; } }

    static string Vswhere
    {
        get
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                                "Microsoft Visual Studio\\Installer\\vswhere.exe");
        }
    }

    static string CppToolsFolder()
    {
        if (!File.Exists(Vswhere))
            return null;
        var info = new ProcessStartInfo(Vswhere, "-latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 " +
                                                 "Microsoft.VisualStudio.Component.VC.CMake.Project -property installationPath")
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true
        };
        using (Process p = Process.Start(info))
        {
            string text = p.StandardOutput.ReadToEnd().Trim();
            p.WaitForExit();
            return text.Length > 0 ? text : null;
        }
    }

    static string SystemGit()
    {
        if (ForceOwnGit)
            return null;
        foreach (string folder in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            try
            {
                string git = Path.Combine(folder.Trim(), "git.exe");
                if (folder.Length > 0 && File.Exists(git))
                    return git;
            }
            catch (ArgumentException) { } // a malformed PATH entry
        }
        return null;
    }

    public static void Cancel()
    {
        cancelled = true;
        lock (Gate)
        {
            if (current == null)
                return;
            try
            {
                var info = new ProcessStartInfo("taskkill.exe", "/PID " + current.Id + " /T /F") { UseShellExecute = false, CreateNoWindow = true };
                using (Process kill = Process.Start(info))
                    kill.WaitForExit();
            }
            catch (Exception e) { Log.Write("build: cancel: " + e.Message); }
        }
    }

    static void CheckCancelled()
    {
        if (cancelled)
            throw new OperationCanceledException("The build was cancelled. Press Build to continue it.");
    }

    // stage: what is happening, and percent of that stage (below 0: unknown). line: output for the log view.
    public static void Run(Action<string, int> stage, Action<string> line)
    {
        cancelled = false;
        Directory.CreateDirectory(Paths.Logs);
        File.AppendAllText(BuildLog, "\r\nBuild started " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "\r\n");
        using (var log = new StreamWriter(BuildLog, true, new UTF8Encoding(false)) { AutoFlush = true })
        {
            Action<string> output = text =>
            {
                lock (log) log.WriteLine(text);
                line(text);
            };

            bool needCppTools = NeedsCppTools;
            CheckRoom(needCppTools);

            string git = SystemGit();
            if (git == null)
            {
                stage("Getting Git", -1);
                git = GetOwnGit(stage);
            }
            string gitFolder = Path.GetDirectoryName(git);
            output("Git: " + git);

            if (needCppTools)
            {
                stage("Installing Microsoft's C++ Build Tools", -1);
                InstallCppTools(output, stage);
            }

            // An update: the source and the earlier build are still here (kept since this version), so only what
            // changed is compiled again - usually just the mod's own code, a few minutes - instead of the whole game.
            string oldHead = Directory.Exists(Path.Combine(Source, ".git")) ? GitHead(git, gitFolder) : null;
            bool incremental = oldHead != null && EarlierBuild() != null;
            GetSource(git, gitFolder, stage, output, incremental ? oldHead : null);
            string why = "";
            if (incremental && CanRebuild(git, gitFolder, oldHead, output, out why))
            {
                output("Update: " + why);
                Rebuild(gitFolder, stage, output);
            }
            else
            {
                if (incremental)
                    output("Full build: " + why);
                Compile(gitFolder, stage, output);
            }

            stage("Installing the game", -1);
            // build.cmd builds a PAL disc into dist, a USA one into dist-usa and a Japanese one into dist-jpn.
            string region = Region();
            string built = Path.Combine(Source, (region == "USA" ? "dist-usa" : region == "JPN" ? "dist-jpn" : "dist") + "\\game");
            if (!Directory.Exists(built)) built = Path.Combine(Source, "dist\\game");
            output("Built for the " + region + " release: " + built);
            string target = Path.GetDirectoryName(Paths.Game);
            Directory.CreateDirectory(target);
            foreach (string file in Directory.GetFiles(built))
                File.Copy(file, Path.Combine(target, Path.GetFileName(file)), true);
            KeepCrossWorldTools(output);
            UpdateLauncher(Path.GetDirectoryName(built), output);
            // The source and the build stay (about 3.5 GB): the next update recompiles only what changed.
            output("Build finished. The source folder is kept so that updates only recompile what changed.");
        }
    }

    // The cross-world enemies (CrossWorld.cs) run tools\cross_world_enemies.py with the maximo1randomizer folder, from
    // next to the launcher: both are copied out of the source before it is removed. Only code: the randomizer's game
    // files, outputs and builds (which a source checkout never has anyway) are left out.
    static void KeepCrossWorldTools(Action<string> output)
    {
        string[] skip = { "game_files", "output", "build", "dist", "__pycache__", ".git" };
        string from = Path.Combine(Source, "maximo1randomizer"), to = Path.Combine(Paths.Root, "maximo1randomizer");
        if (Directory.Exists(from))
        {
            foreach (string file in Directory.GetFiles(from, "*", SearchOption.AllDirectories))
            {
                string relative = file.Substring(from.Length + 1);
                if (skip.Any(s => relative.StartsWith(s + "\\", StringComparison.OrdinalIgnoreCase)))
                    continue;
                string target = Path.Combine(to, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                File.Copy(file, target, true);
            }
            output("Kept the randomizer's code for the cross-world enemies: " + to);
        }
        else
            output("The source has no maximo1randomizer folder: the cross-world enemies will not be available.");
        foreach (string tool in new[] { "cross_world_enemies.py", "more_enemies.py", "randomize_game.py", "spawn_editor.py" })
        {
            string file = Path.Combine(Source, "tools", tool);
            if (!File.Exists(file)) continue;
            Directory.CreateDirectory(Path.Combine(Paths.Root, "tools"));
            File.Copy(file, Path.Combine(Paths.Root, "tools", tool), true);
        }
    }

    static void CheckRoom(bool needCppTools)
    {
        if (Paths.Root.Length > MaxRootLength)
            throw new Exception("This folder's path is too long for the compiler. Move the launcher somewhere shorter, such as C:\\Games\\Maximo.");
        long need = BuildSpace + (needCppTools ? CppToolsSpace : 0);
        long free = new DriveInfo(Path.GetPathRoot(Paths.Root)).AvailableFreeSpace;
        if (free < need)
            throw new Exception("The build needs about " + need / GB + " GB of free disk space here, and this drive has " + free / GB + " GB.");
    }

    static string GetOwnGit(Action<string, int> stage)
    {
        string git = Path.Combine(OwnGit, "cmd\\git.exe");
        if (File.Exists(git))
            return git;
        Directory.CreateDirectory(Tools);
        string zip = Path.Combine(Tools, "mingit.zip");
        Download(GitUrl, zip, "Downloading Git", stage);
        if (!string.Equals(Setup.Sha256(zip), GitSha256, StringComparison.OrdinalIgnoreCase))
        {
            File.Delete(zip);
            throw new Exception("The Git download does not match its hash. Press Build to try again.");
        }
        string unpacking = OwnGit + ".tmp";
        if (Directory.Exists(unpacking))
            Directory.Delete(unpacking, true);
        ZipFile.ExtractToDirectory(zip, unpacking);
        Directory.Move(unpacking, OwnGit);
        File.Delete(zip);
        return git;
    }

    static void InstallCppTools(Action<string> output, Action<string, int> stage)
    {
        Directory.CreateDirectory(Tools);
        string installer = Path.Combine(Tools, "vs_BuildTools.exe");
        Download(CppToolsUrl, installer, "Downloading Microsoft's installer", stage);
        if (!SignedByMicrosoft(installer))
        {
            File.Delete(installer);
            throw new Exception("Microsoft's installer failed its signature check, so it was not run.");
        }
        stage("Installing Microsoft's C++ Build Tools. This is a large download.", -1);
        var info = new ProcessStartInfo(installer, CppToolsArgs) { UseShellExecute = true, Verb = "runas" };
        try
        {
            using (Process p = Process.Start(info))
            {
                lock (Gate) current = p;
                p.WaitForExit();
                lock (Gate) current = null;
                CheckCancelled();
                output("C++ Build Tools installer: exit code " + p.ExitCode);
                if (p.ExitCode != 0 && p.ExitCode != 3010) // 3010: done, a restart is due
                    throw new Exception("Microsoft's installer stopped with code " + p.ExitCode + ".");
            }
        }
        catch (Win32Exception e)
        {
            if (e.NativeErrorCode == 1223)
                throw new OperationCanceledException("The C++ tools need your permission to install. Press Build to try again.");
            throw;
        }
        if (NeedsCppTools)
            throw new Exception("The C++ tools still aren't available. Restart Windows and press Build again.");
    }

    // A dropped connection can leave a submodule half made, which git can't continue: after two tries, start clean.
    static void GetSource(string git, string gitFolder, Action<string, int> stage, Action<string> output, string oldHead = null)
    {
        const int Attempts = 4, FreshFrom = 3;
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                FetchSource(git, gitFolder, stage, output, oldHead);
                return;
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception e)
            {
                output("Download attempt " + attempt + " of " + Attempts + " failed: " + e.Message);
                if (attempt == Attempts)
                    throw new Exception("The download failed: " + e.Message + " Check your internet connection and press Build again.");
            }
            Thread.Sleep(3000);
            CheckCancelled();
            if (attempt + 1 >= FreshFrom && Directory.Exists(Source))
            {
                oldHead = null; // (a fresh clone: everything is built again)
                output("Starting the download again from a clean folder.");
                RemoveFolder(Source, gitFolder, output);
            }
        }
    }

    static void FetchSource(string git, string gitFolder, Action<string, int> stage, Action<string> output, string oldHead)
    {
        const string longPaths = "-c core.longpaths=true "; // SPIRV-Cross test files exceed the Windows limit otherwise
        // Shallow (latest version only, no history): about 0.9 GB less to download and store.
        stage("Downloading the source code", -1);
        if (Directory.Exists(Path.Combine(Source, ".git")))
        {
            Tool(git, longPaths + "fetch --depth 1 origin main", Source, gitFolder, output);
            Tool(git, longPaths + "reset --hard origin/main", Source, gitFolder, output);
        }
        else
        {
            string cloning = Source + ".tmp";
            if (Directory.Exists(cloning))
                RemoveFolder(cloning, gitFolder, output);
            Tool(git, longPaths + "clone --progress --depth 1 --single-branch --no-tags \"" + SourceUrl + "\" \"" + cloning + "\"",
                 Paths.Root, gitFolder, output);
            Directory.Move(cloning, Source);
        }
        // An update whose libraries (the submodules) are the same commits: they are left as they are. Resetting them
        // would rewrite their files - and the runtime header every game file includes - so everything would compile
        // again. The project's changes to them are put back below, only where they differ.
        if (oldHead != null && !SubmodulesChanged(git, gitFolder, oldHead))
        {
            output("The libraries have not changed: kept as they are.");
            ApplyOverlay(gitFolder, stage, output);
            return;
        }
        stage("Downloading the libraries", -1);
        try
        {
            Tool(git, longPaths + "submodule update --init --recursive --force --depth 1", Source, gitFolder, output);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception e)
        {
            // A server that won't send one commit on its own: fetch what is still missing with its history.
            output("Shallow download of the libraries failed (" + e.Message + "); downloading the rest in full.");
            Tool(git, longPaths + "submodule update --init --recursive --force", Source, gitFolder, output);
        }
        ApplyOverlay(gitFolder, stage, output);
    }

    // The project's own changes to the runtime and the GS renderer, which the submodules' upstream commits lack (only
    // files that differ are copied, so unchanged ones keep their dates and are not compiled again).
    static void ApplyOverlay(string gitFolder, Action<string, int> stage, Action<string> output)
    {
        string overlay = Path.Combine(Source, "apply_overlay.cmd");
        if (File.Exists(overlay))
        {
            stage("Applying the mod's runtime changes", -1);
            Tool(Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe", "/d /s /c \"\"" + overlay + "\"\"", Source, gitFolder, output);
        }
    }

    // ---- updates that only recompile what changed ----

    static string Region()
    {
        string regionFile = Path.Combine(Source, "build_region.txt");
        try { return File.Exists(regionFile) ? File.ReadAllText(regionFile).Trim().ToUpperInvariant() : "PAL"; }
        catch (IOException) { return "PAL"; }
    }

    static string Suffix(string region) { return region == "USA" ? "-usa" : region == "JPN" ? "-jpn" : ""; }

    // The build folder of the last full build, if all of it is still here (its compiled game code, the code generated
    // from the disc, and the game it made).
    static string EarlierBuild()
    {
        string suffix = Suffix(Region());
        string build = Path.Combine(Source, "build" + suffix);
        if (!File.Exists(Path.Combine(build, "build.ninja")) || !File.Exists(Path.Combine(build, "maximo_boot.exe")) ||
            !Directory.Exists(Path.Combine(Source, "generated" + suffix + "\\game")) || !File.Exists(Path.Combine(Source, "rebuild.cmd")))
            return null;
        return build;
    }

    static string GitOutput(string git, string gitFolder, string arguments)
    {
        var lines = new StringBuilder();
        Tool(git, arguments, Source, gitFolder, l => lines.AppendLine(l));
        return lines.ToString();
    }

    static string GitHead(string git, string gitFolder)
    {
        try
        {
            string head = GitOutput(git, gitFolder, "rev-parse HEAD").Trim();
            return Regex.IsMatch(head, "^[0-9a-f]{40}$") ? head : null;
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception) { return null; }
    }

    static string[] ChangedFiles(string git, string gitFolder, string oldHead)
    {
        return GitOutput(git, gitFolder, "diff --name-only " + oldHead + " HEAD")
            .Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
    }

    static bool SubmodulesChanged(string git, string gitFolder, string oldHead)
    {
        try
        {
            return ChangedFiles(git, gitFolder, oldHead).Any(f => f == ".gitmodules" || f.StartsWith("external/", StringComparison.Ordinal));
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception) { return true; }
    }

    // A quick rebuild is enough unless the update changed how the game's code is made from the disc: the function
    // map, the VU1 recipe, the disc tool, the recompiler, or the libraries.
    static bool CanRebuild(string git, string gitFolder, string oldHead, Action<string> output, out string why)
    {
        string[] changed;
        try { changed = ChangedFiles(git, gitFolder, oldHead); }
        catch (OperationCanceledException) { throw; }
        catch (Exception e) { why = "could not compare with the earlier version (" + e.Message + ")"; return false; }
        string[] full = { "data/", "tools/kit/", "external/", ".gitmodules", "runtime_overlay/external/PS2Recomp/ps2xRecomp/", "build.cmd" };
        string hit = changed.FirstOrDefault(f => full.Any(p => f.StartsWith(p, StringComparison.Ordinal)));
        if (hit != null)
        {
            why = hit + " changed, which needs the whole game made again from your disc";
            return false;
        }
        why = changed.Length == 0 ? "already up to date" : changed.Length + " changed file(s), only those are compiled again";
        foreach (string f in changed.Take(20))
            output("  changed: " + f);
        return true;
    }

    static void Rebuild(string gitFolder, Action<string, int> stage, Action<string> output)
    {
        var ninja = new Regex(@"^\[(\d+)/(\d+)\]");
        string text = "Updating: compiling what changed";
        stage(text, -1);
        Action<string> parse = l =>
        {
            Match m = ninja.Match(l);
            if (m.Success)
                stage(text, (int)(100L * long.Parse(m.Groups[1].Value) / Math.Max(1L, long.Parse(m.Groups[2].Value))));
            output(l);
        };
        string script = Path.Combine(Source, "rebuild.cmd");
        Tool(Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe",
             "/d /s /c \"\"" + script + "\" " + Region().ToLowerInvariant() + "\"", Source, gitFolder, parse);
    }

    // The launcher that came with the update replaces this one. A running program can't be overwritten, but it can be
    // renamed: this one becomes MaximoLauncher.old.exe (deleted on the next start) and the new one takes its name,
    // so the next start runs it.
    static void UpdateLauncher(string distGame, Action<string> output)
    {
        try
        {
            string fresh = Path.Combine(Path.GetDirectoryName(distGame), "MaximoLauncher.exe");
            string mine = System.Reflection.Assembly.GetExecutingAssembly().Location;
            if (!File.Exists(fresh) || string.IsNullOrEmpty(mine))
                return;
            if (File.ReadAllBytes(fresh).SequenceEqual(File.ReadAllBytes(mine)))
                return;
            string old = Path.Combine(Path.GetDirectoryName(mine), "MaximoLauncher.old.exe");
            if (File.Exists(old))
                File.Delete(old);
            File.Move(mine, old);
            File.Copy(fresh, mine);
            LauncherUpdated = true;
            output("The launcher was updated too: it takes effect the next time you open it.");
        }
        catch (Exception e) { output("The launcher could not be updated (" + e.Message + "); the game was."); }
    }
    public static bool LauncherUpdated;

    public static void DeleteOldLauncher()
    {
        try
        {
            string old = Path.Combine(Paths.Root, "MaximoLauncher.old.exe");
            if (File.Exists(old))
                File.Delete(old);
        }
        catch (Exception) { }
    }

    // A newer version of the mods on GitHub than the one this game was built from (asked in the background; null
    // when it can't tell: no kept source, no Git, no connection).
    public static bool? UpdateAvailable()
    {
        try
        {
            if (!Directory.Exists(Path.Combine(Source, ".git")))
                return null;
            string git = SystemGit() ?? Path.Combine(OwnGit, "cmd\\git.exe");
            if (!File.Exists(git))
                return null;
            string gitFolder = Path.GetDirectoryName(git);
            string local = GitHead(git, gitFolder);
            string remote = GitOutput(git, gitFolder, "ls-remote origin refs/heads/main").Split('\t')[0].Trim();
            if (local == null || !Regex.IsMatch(remote, "^[0-9a-f]{40}$"))
                return null;
            return local != remote;
        }
        catch (Exception) { return null; }
    }

    // Everything in .\source is only needed while building (the next build downloads it again), so it goes once the
    // game is installed: about 3.5 GB. A failed or cancelled build keeps it, so Build can carry on from there.
    static void RemoveSource(string gitFolder, Action<string> output)
    {
        try
        {
            RemoveFolder(Source, gitFolder, output);
            output(Directory.Exists(Source) ? "Could not remove all of the source folder; it is safe to delete." : "Removed the source folder.");
        }
        catch (Exception e)
        {
            output("Could not remove the source folder (" + e.Message + "); it is safe to delete.");
        }
    }

    // Directory.Delete fails on paths over the Windows limit, which a clone can have.
    static void RemoveFolder(string folder, string gitFolder, Action<string> output)
    {
        Tool(Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe", "/d /s /c \"rmdir /s /q \"" + folder + "\"\"", Paths.Root, gitFolder, output);
    }

    static void Compile(string gitFolder, Action<string, int> stage, Action<string> output)
    {
        var step = new Regex(@"^\[(\d)/6\] (.*)$");
        var ninja = new Regex(@"^\[(\d+)/(\d+)\]");
        string text = "Building";
        Action<string> parse = l =>
        {
            Match m = step.Match(l);
            if (m.Success)
            {
                text = "Step " + m.Groups[1].Value + " of 6: " + m.Groups[2].Value;
                stage(text, -1);
            }
            else if ((m = ninja.Match(l)).Success)
                stage(text, (int)(100L * long.Parse(m.Groups[1].Value) / Math.Max(1L, long.Parse(m.Groups[2].Value))));
            output(l);
        };
        string script = Path.Combine(Source, "build.cmd");
        Tool(Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe",
             "/d /s /c \"\"" + script + "\" \"" + Paths.Disc + "\"\"", Source, gitFolder, parse);
    }

    static void Tool(string exe, string arguments, string folder, string gitFolder, Action<string> output)
    {
        CheckCancelled();
        var info = new ProcessStartInfo(exe, arguments)
        {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = folder,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        info.EnvironmentVariables["PATH"] = gitFolder + ";" + info.EnvironmentVariables["PATH"];
        info.EnvironmentVariables["GIT_TERMINAL_PROMPT"] = "0";
        string problem = null;
        using (var p = new Process { StartInfo = info })
        {
            DataReceivedEventHandler received = (s, e) =>
            {
                if (e.Data == null) return;
                if (e.Data.StartsWith("fatal:") || e.Data.StartsWith("Error:")) problem = e.Data;
                output(e.Data);
            };
            p.OutputDataReceived += received;
            p.ErrorDataReceived += received;
            p.Start();
            lock (Gate) current = p;
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            p.WaitForExit();
            lock (Gate) current = null;
            CheckCancelled();
            if (p.ExitCode != 0)
                throw new Exception((problem ?? "The build stopped (" + Path.GetFileName(exe) + ", code " + p.ExitCode + ").") + " Details are in logs\\build.log.");
        }
    }

    static void Download(string url, string target, string text, Action<string, int> stage)
    {
        ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
        string partial = target + ".part";
        for (int attempt = 1; ; attempt++)
        {
            CheckCancelled();
            try
            {
                var request = (HttpWebRequest)WebRequest.Create(url);
                request.UserAgent = "MaximoLauncher/1.0";
                request.Timeout = 60000;
                request.ReadWriteTimeout = 60000;
                using (WebResponse response = request.GetResponse())
                using (Stream from = response.GetResponseStream())
                using (var to = new FileStream(partial, FileMode.Create))
                {
                    long total = response.ContentLength, have = 0;
                    var buffer = new byte[1 << 16];
                    for (int n; (n = from.Read(buffer, 0, buffer.Length)) > 0;)
                    {
                        CheckCancelled();
                        to.Write(buffer, 0, n);
                        have += n;
                        stage(text, total > 0 ? (int)(100 * have / total) : -1);
                    }
                }
                if (File.Exists(target))
                    File.Delete(target);
                File.Move(partial, target);
                return;
            }
            catch (WebException e)
            {
                Log.Write("build: download " + url + " attempt " + attempt + ": " + e.Message);
                if (attempt >= 3)
                    throw new Exception(text + " failed: " + e.Message);
                Thread.Sleep(2000 * attempt);
            }
        }
    }

    // ---- signature check of a file about to run with administrator rights ----

    [StructLayout(LayoutKind.Sequential)]
    struct FileInfoData
    {
        public uint Size;
        public IntPtr Path, Handle, KnownSubject;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct TrustData
    {
        public uint Size;
        public IntPtr PolicyData, SipData;
        public uint UiChoice, RevocationChecks, UnionChoice;
        public IntPtr File;
        public uint StateAction;
        public IntPtr StateData, UrlReference;
        public uint ProviderFlags, UiContext;
        public IntPtr SignatureSettings;
    }

    [DllImport("wintrust.dll", CharSet = CharSet.Unicode)]
    static extern int WinVerifyTrust(IntPtr window, ref Guid action, ref TrustData data);

    public static bool SignedByMicrosoft(string path)
    {
        const uint NoUi = 2, FileChoice = 1;
        var action = new Guid("00AAC56B-CD44-11D0-8CC2-00C04FC295EE"); // WINTRUST_ACTION_GENERIC_VERIFY_V2
        IntPtr name = Marshal.StringToCoTaskMemUni(path);
        var file = new FileInfoData { Size = (uint)Marshal.SizeOf(typeof(FileInfoData)), Path = name };
        IntPtr fileMemory = Marshal.AllocCoTaskMem(Marshal.SizeOf(typeof(FileInfoData)));
        try
        {
            Marshal.StructureToPtr(file, fileMemory, false);
            var data = new TrustData
            {
                Size = (uint)Marshal.SizeOf(typeof(TrustData)), UiChoice = NoUi, UnionChoice = FileChoice, File = fileMemory
            };
            if (WinVerifyTrust(IntPtr.Zero, ref action, ref data) != 0)
                return false;
            using (var certificate = new X509Certificate(X509Certificate.CreateFromSignedFile(path)))
                return certificate.Subject.Contains("O=Microsoft Corporation");
        }
        catch (Exception e)
        {
            Log.Write("build: signature check: " + e.Message);
            return false;
        }
        finally
        {
            Marshal.FreeCoTaskMem(fileMemory);
            Marshal.FreeCoTaskMem(name);
        }
    }
}
