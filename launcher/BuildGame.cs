// Building the game from the player's own disc, for the mod package (Maximo-GTG-Mods). The package holds this launcher,
// BUILD_GAME.cmd and the changed source files in .\overlay - nothing of the game's. BUILD_GAME.cmd downloads the
// Maximo-GTG project into .\source, puts the overlay over it and runs its build.cmd with the disc the player chooses
// here; the game it builds lands in .\source\dist, which this launcher then opens. A launcher without BUILD_GAME.cmd
// next to it (the normal dist one) never shows any of this.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;

static class BuildGame
{
    public static readonly string Script = Path.Combine(Paths.Root, "BUILD_GAME.cmd");
    public static readonly string Source = Path.Combine(Paths.Root, "source");
    public static readonly string BuiltLauncher = Path.Combine(Source, "dist\\MaximoLauncher.exe");
    public static readonly string LogFile = Path.Combine(Paths.Root, "build_log.txt");
    static readonly string PackageVersion = Path.Combine(Paths.Root, "overlay\\VERSION.txt");
    static readonly string BuiltVersion = Path.Combine(Source, "built_version.txt");

    // This launcher is the mod package's one.
    public static bool Available { get { return File.Exists(Script); } }

    static string ReadTrim(string file)
    {
        try { return File.Exists(file) ? File.ReadAllText(file).Trim() : ""; }
        catch (IOException) { return ""; }
    }

    // The game in .\source\dist was built from this package's files (a newer package wants a new build).
    public static bool Built
    {
        get
        {
            return File.Exists(BuiltLauncher) && File.Exists(Path.Combine(Source, "dist\\game\\maximo_boot.exe")) &&
                   ReadTrim(PackageVersion) == ReadTrim(BuiltVersion);
        }
    }

    // A game built from an older package is there: a newer package only recompiles what changed (no disc needed).
    public static bool CanUpdate
    {
        get
        {
            return File.Exists(BuiltLauncher) && File.Exists(Path.Combine(Source, "build\\build.ninja")) &&
                   File.Exists(Path.Combine(Source, "dist\\disc\\SLES_507.03"));
        }
    }

    // The package's launcher with no game of its own: the Play button builds instead.
    public static bool Needed { get { return Available && !File.Exists(Paths.Game) && !Built; } }

    public static bool OpenBuilt()
    {
        try
        {
            Process.Start(new ProcessStartInfo(BuiltLauncher) { WorkingDirectory = Path.GetDirectoryName(BuiltLauncher) });
            Log.Write("opened the built launcher " + BuiltLauncher);
            return true;
        }
        catch (Exception e)
        {
            Log.Write("could not open " + BuiltLauncher + ": " + e.Message);
            return false;
        }
    }
}

class BuildGameForm : Form
{
    readonly TextBox output = new TextBox();
    readonly Label info = new Label();
    readonly Button update = new Button(), fromIso = new Button(), fromFolder = new Button(), close = new Button();
    Process process;
    StreamWriter log;
    public bool Succeeded { get; private set; }

    public BuildGameForm()
    {
        Text = "Build the game - " + Build.Name;
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.Sizable;
        MinimizeBox = true;
        MaximizeBox = true;
        BackColor = Color.FromArgb(12, 9, 18);
        ForeColor = LauncherForm.Text1;
        Font = new Font("Segoe UI", 10f);
        ClientSize = new Size(900, 600);
        MinimumSize = new Size(640, 400);

        info.Dock = DockStyle.Top;
        info.Height = 92;
        info.Padding = new Padding(12, 10, 12, 0);
        info.Text = "The game is built on this PC from your own Maximo: Ghosts to Glory disc (PAL, USA or Japan; .iso, .bin/.cue or a folder). Nothing " +
                    "from the game comes with this package.\r\n" +
                    "You need Git, and Visual Studio with \"Desktop development with C++\" (including \"C++ CMake tools for " +
                    "Windows\"). About 10 GB free. The first build takes a good while; the window shows its progress.";

        var buttons = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 46, Padding = new Padding(8, 6, 8, 0) };
        Style(update, "Update (only what changed)", 230);
        Style(fromIso, "Build from my disc image (.iso/.bin/.cue)...", 300);
        Style(fromFolder, "...or from a disc folder", 200);
        Style(close, "Close", 110);
        update.Click += delegate { Start(null); };
        fromIso.Click += delegate { ChooseIso(); };
        fromFolder.Click += delegate { ChooseFolder(); };
        close.Click += delegate { Close(); };
        if (BuildGame.CanUpdate)
        {
            buttons.Controls.Add(update);
            info.Text = "A newer version of the mods is here. Update copies the changed files and recompiles only what " +
                        "changed: usually a few minutes, no disc needed.\r\n" +
                        "The full build from your disc is still there if the update asks for it.";
        }
        buttons.Controls.Add(fromIso);
        buttons.Controls.Add(fromFolder);
        buttons.Controls.Add(close);

        output.Dock = DockStyle.Fill;
        output.Multiline = true;
        output.ReadOnly = true;
        output.ScrollBars = ScrollBars.Both;
        output.WordWrap = false;
        output.BackColor = Color.FromArgb(20, 15, 26);
        output.ForeColor = LauncherForm.Text1;
        output.Font = new Font("Consolas", 9.5f);
        output.BorderStyle = BorderStyle.FixedSingle;

        Controls.Add(output);
        Controls.Add(buttons);
        Controls.Add(info);
        FormClosing += OnClosing;
    }

    static void Style(Button b, string text, int width)
    {
        b.Text = text;
        b.Width = width;
        b.Height = 32;
        b.FlatStyle = FlatStyle.Flat;
        b.BackColor = Color.FromArgb(40, 30, 50);
        b.ForeColor = LauncherForm.Text1;
        b.FlatAppearance.BorderColor = LauncherForm.Gold;
    }

    void ChooseIso()
    {
        using (var dialog = new OpenFileDialog { Filter = "Disc images (*.iso, *.cue, *.bin)|*.iso;*.cue;*.bin|All files (*.*)|*.*", Title = "Your Maximo: Ghosts to Glory disc image" })
            if (dialog.ShowDialog(this) == DialogResult.OK) Start(dialog.FileName);
    }

    void ChooseFolder()
    {
        using (var dialog = new FolderBrowserDialog { Description = "The folder (or drive) with the Maximo PAL disc's files, containing SLES_507.03" })
            if (dialog.ShowDialog(this) == DialogResult.OK) Start(dialog.SelectedPath);
    }

    void Append(string line)
    {
        if (line == null) return;
        try { if (log != null) lock (log) log.WriteLine(line); }
        catch (Exception) { }
        if (IsDisposed) return;
        BeginInvoke((Action)(() =>
        {
            if (output.TextLength > 400000) output.Text = output.Text.Substring(output.TextLength - 200000);
            output.AppendText(line + "\r\n");
        }));
    }

    void Start(string disc)
    {
        if (process != null) return;
        if (Paths.Root.IndexOf(' ') >= 0 || Paths.Root.Length > 60)
        {
            if (MessageBox.Show(this, "This folder's path is long or has spaces:\r\n" + Paths.Root + "\r\n\r\nThe build is safest " +
                                      "in a short path without spaces, like C:\\MaximoMods. Build here anyway?",
                                Build.Name, MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                return;
        }
        update.Enabled = fromIso.Enabled = fromFolder.Enabled = false;
        output.Clear();
        try { log = new StreamWriter(BuildGame.LogFile, false, Encoding.UTF8) { AutoFlush = true }; }
        catch (Exception) { log = null; }
        string argument = disc == null ? "--update" : "\"" + disc + "\"";
        Append(disc == null ? "Updating" : "Building from " + disc);
        Log.Write("build: started, " + argument);
        var info = new ProcessStartInfo("cmd.exe", "/d /c \"\"" + BuildGame.Script + "\" " + argument + "\"")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = Paths.Root,
        };
        try
        {
            process = new Process { StartInfo = info, EnableRaisingEvents = true };
            process.OutputDataReceived += (s, e) => Append(e.Data);
            process.ErrorDataReceived += (s, e) => Append(e.Data);
            process.Exited += delegate { if (!IsDisposed && IsHandleCreated) BeginInvoke((Action)Finished); };
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
        catch (Exception e)
        {
            Append("Error: could not start the build: " + e.Message);
            process = null;
            update.Enabled = fromIso.Enabled = fromFolder.Enabled = true;
        }
    }

    void Finished()
    {
        if (process == null) return;
        int code = -1;
        try { process.WaitForExit(); code = process.ExitCode; }
        catch (Exception) { }
        process = null;
        Log.Write("build: finished with exit code " + code);
        if (code == 0 && BuildGame.Built)
        {
            Append("");
            Append("Done. Opening the game's launcher...");
            CloseLog();
            Succeeded = true;
            MessageBox.Show(this, "Done. The launcher opens now, ready to play.\r\n\r\nNext time, just open this " +
                                  "MaximoLauncher.exe again: it goes straight to the game.", Build.Name, MessageBoxButtons.OK,
                            MessageBoxIcon.Information);
            Close();
            return;
        }
        if (code == 3)
        {
            Append("");
            Append("Choose your disc above for the full build.");
            CloseLog();
            update.Enabled = false;
            fromIso.Enabled = fromFolder.Enabled = true;
            return;
        }
        Append("");
        Append("The build stopped (exit code " + code + "). The message just above says why.");
        Append("The whole output is in " + BuildGame.LogFile + " - send it if you need help.");
        CloseLog();
        update.Enabled = fromIso.Enabled = fromFolder.Enabled = true;
    }

    void CloseLog()
    {
        try { if (log != null) lock (log) log.Dispose(); }
        catch (Exception) { }
        log = null;
    }

    void OnClosing(object sender, FormClosingEventArgs e)
    {
        if (process == null) return;
        if (MessageBox.Show(this, "The build is still running. Stop it?", Build.Name, MessageBoxButtons.YesNo,
                            MessageBoxIcon.Question) != DialogResult.Yes)
        {
            e.Cancel = true;
            return;
        }
        try
        {
            // The build runs git, cmake, ninja and the compiler under cmd: end the whole tree.
            Process.Start(new ProcessStartInfo("taskkill", "/T /F /PID " + process.Id) { CreateNoWindow = true, UseShellExecute = false }).WaitForExit(10000);
        }
        catch (Exception) { }
        Log.Write("build: stopped by the player");
        CloseLog();
    }
}
