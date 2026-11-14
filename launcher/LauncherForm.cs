// The launcher's window: a gold frame with the menu on the left and one page beside it. Everything is laid out in the
// design's 1536 x 1024 units and scaled to the screen. The art in .\art holds nothing of the game's: the banner comes
// from the player's own files (GameArt.cs).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

class LauncherForm : Form
{
    const int DesignW = 1536, DesignH = 1024;
    static readonly Rectangle BannerArea = new Rectangle(30, 40, 1476, 268);
    static readonly Rectangle PageArea = new Rectangle(446, 356, 1040, 496);
    static readonly Rectangle PlayArea = new Rectangle(48, 368, 362, 76);
    static readonly Rectangle MinimizeArea = new Rectangle(1330, 60, 44, 34), CloseArea = new Rectangle(1378, 60, 44, 34);
    static readonly string[] NavNames = { "Graphics", "Controls", "Enhancements", "Randomizer", "HD Textures", "Game Files", "About", "Credits" };
    static readonly string[] NavIcons = { "icon_graphics.png", "icon_controls.png", "icon_enhancements.png", "icon_play.png", "icon_hd.png", "icon_folder.png", "icon_about.png", null };
    public static readonly Color Text1 = Color.FromArgb(238, 230, 214), Text2 = Color.FromArgb(178, 168, 186), Gold = Color.FromArgb(232, 178, 52);
    public static readonly Color Good = Color.FromArgb(150, 226, 120), Bad = Color.FromArgb(255, 160, 120);
    static readonly Color Dark = Color.FromArgb(20, 15, 26), WindowBack = Color.FromArgb(12, 9, 18);

    readonly float scale;
    readonly Bitmap shell, buttonPlay, buttonNormal, buttonHover, buttonPressed, iconPlay;
    readonly Bitmap[] navIcons = new Bitmap[NavNames.Length];
    Bitmap banner, pageBack;
    const int BuildPage = 8; // after the menu's pages; only the Build button opens it
    readonly Panel[] pages = new Panel[NavNames.Length + 1];
    int page = -1, hover = -2, pressed = -2; // -1 = Play, -3 = minimize, -4 = close
    bool busy, building;

    readonly Label status = new Label(), tip = new Label(), lastRun = new Label();
    readonly Button setupIso = new Button(), setupFolder = new Button();
    readonly Button zipLogs = new Button(), openSaves = new Button(), openLogs = new Button(), openShots = new Button();
    readonly CheckBox recordAudio = new CheckBox(), vu1Thread = new CheckBox(), vu1Pipeline = new CheckBox(), borderless = new CheckBox(), softParticles = new CheckBox(), vsync = new CheckBox(), skipIntro = new CheckBox(), crossWorld = new CheckBox();
    readonly CheckBox hdTextures = new CheckBox();
    readonly CheckBox freeCamera = new CheckBox(), cameraInvert = new CheckBox();
    readonly ComboBox cameraSpeed = new ComboBox(), cameraReturn = new ComboBox();
    readonly Button hdTexturesButton = new Button(), controlsButton = new Button();
    readonly ComboBox picture = new ComboBox(), scaling = new ComboBox(), aspect = new ComboBox(), frameRate = new ComboBox();
    readonly TextBox notes = new TextBox(), buildLog = new TextBox();
    readonly Button rebuild = new Button(), cancelBuild = new Button();
    readonly Label buildStage = new Label();
    readonly ProgressBar buildBar = new ProgressBar();
    readonly CheckBox rndOn = new CheckBox(), rndEnemies = new CheckBox(), rndStructures = new CheckBox(), rndItems = new CheckBox();
    readonly CheckBox rndSkills = new CheckBox(), rndChests = new CheckBox(), rndBossEnemies = new CheckBox(), rndBossArenas = new CheckBox();
    readonly TextBox rndSeed = new TextBox();
    readonly Button rndNewSeed = new Button(), rndSpawnEditor = new Button(), rndBalanced = new Button();
    readonly Label rndState = new Label();

    static Bitmap Art(string name)
    {
        // The art sits in .\art beside the launcher. Without it the launcher still opens, with a plain look.
        string path = Path.Combine(Paths.Art, name);
        if (!File.Exists(path))
        {
            Log.Write("art missing: " + path);
            return new Bitmap(1, 1);
        }
        using (var image = new Bitmap(path))
            return new Bitmap(image);
    }

    int R(float v) { return (int)Math.Round(v * scale); }
    Rectangle R(Rectangle r) { return new Rectangle(R(r.X), R(r.Y), R(r.Width), R(r.Height)); }
    Rectangle NavArea(int i) { return new Rectangle(64, 452 + i * 60, 336, 56); } // (eight entries)

    public LauncherForm()
    {
        Text = Build.Name;
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        DoubleBuffered = true;
        BackColor = Color.Black;
        Rectangle screen = Screen.PrimaryScreen.WorkingArea;
        scale = Math.Min(1f, Math.Min(screen.Height * 0.96f / DesignH, screen.Width * 0.96f / DesignW));
        ClientSize = new Size(R(DesignW), R(DesignH));
        Font = new Font("Segoe UI", 13f * scale, GraphicsUnit.Pixel);

        shell = Art("shell.png");
        buttonPlay = Art("btn_play.png");
        buttonNormal = Art("btn_normal.png");
        buttonHover = Art("btn_hover.png");
        buttonPressed = Art("btn_pressed.png");
        iconPlay = Art("icon_play.png");
        for (int i = 0; i < NavIcons.Length; i++) if (NavIcons[i] != null) navIcons[i] = Art(NavIcons[i]);
        BuildPageBack();
        BuildPages();

        status.BackColor = Color.Transparent;
        status.Bounds = R(new Rectangle(452, 858, 1030, 26));
        status.Font = new Font("Segoe UI", 16f * scale, FontStyle.Bold, GraphicsUnit.Pixel);
        tip.BackColor = Color.Transparent;
        tip.Bounds = R(new Rectangle(452, 884, 1030, 24));
        tip.Font = new Font("Segoe UI", 15f * scale, GraphicsUnit.Pixel);
        tip.ForeColor = Text2;
        tip.Text = "Tip: press F12 in the game to take a screenshot for the banner above.";
        Controls.Add(status);
        Controls.Add(tip);
        RefreshBanner();
        Refresh2();
        ShowPage(-1);
        FormClosing += delegate
        {
            if (building) Builder.Cancel();
            Process game = Game.Running;
            try
            {
                if (game != null && !game.HasExited)
                    Log.Write("launcher closed while the game (process " + game.Id + ") was still running");
            }
            catch (InvalidOperationException) { }
        };
    }

    // The pages sit on a dimmed copy of the scene, so standard controls can stay transparent over it.
    void BuildPageBack()
    {
        Rectangle r = R(PageArea);
        pageBack = new Bitmap(r.Width, r.Height);
        using (var g = Graphics.FromImage(pageBack))
        {
            g.InterpolationMode = InterpolationMode.HighQualityBicubic;
            g.DrawImage(shell, new Rectangle(0, 0, r.Width, r.Height), PageArea, GraphicsUnit.Pixel);
            using (var dim = new SolidBrush(Color.FromArgb(214, 14, 10, 20))) g.FillRectangle(dim, 0, 0, r.Width, r.Height);
            using (var pen = new Pen(Gold, Math.Max(1f, 2f * scale))) g.DrawRectangle(pen, 1, 1, r.Width - 3, r.Height - 3);
        }
    }

    void RefreshBanner()
    {
        Bitmap old = banner;
        banner = GameArt.Banner(Paths.Disc, Paths.Screenshots, BannerArea.Width, BannerArea.Height);
        if (old != null) old.Dispose();
        Invalidate();
    }

    // ---- painting and the menu ----

    protected override void OnPaintBackground(PaintEventArgs e) { }

    protected override void OnPaint(PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        g.InterpolationMode = InterpolationMode.HighQualityBicubic;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
        g.ScaleTransform(scale, scale);
        g.FillRectangle(Brushes.Black, 0, 0, DesignW, DesignH);
        if (banner != null) g.DrawImage(banner, BannerArea);
        g.DrawImage(shell, 0, 0, DesignW, DesignH);
        if (banner == null) DrawTitle(g);
        DrawButton(g, PlayArea, -1, building ? "Building..." : BuildGame.Needed ? (BuildGame.CanUpdate ? "Update" : "Build") : File.Exists(Paths.Game) ? "Play" : "Build", iconPlay, buttonPlay, 26f);
        for (int i = 0; i < NavNames.Length; i++)
            DrawButton(g, NavArea(i), i, NavNames[i], navIcons[i], null, 20f);
        DrawWindowButton(g, MinimizeArea, -3, "–");
        DrawWindowButton(g, CloseArea, -4, "×");
    }

    void DrawTitle(Graphics g)
    {
        using (var big = new Font("Georgia", 58f, FontStyle.Bold, GraphicsUnit.Pixel))
        using (var small = new Font("Georgia", 26f, FontStyle.Bold, GraphicsUnit.Pixel))
        using (var gold = new SolidBrush(Gold))
        using (var center = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center })
        {
            g.DrawString("MAXIMO", big, gold, new RectangleF(30, 70, 1476, 110), center);
            g.DrawString("Ghosts to Glory", small, gold, new RectangleF(30, 170, 1476, 60), center);
        }
    }

    void DrawButton(Graphics g, Rectangle r, int id, string text, Bitmap icon, Bitmap fixedArt, float fontSize)
    {
        Bitmap art = fixedArt ?? (pressed == id ? buttonPressed : hover == id || page == id ? buttonHover : buttonNormal);
        g.DrawImage(art, r);
        if (fixedArt != null && hover == id)
            using (var light = new SolidBrush(Color.FromArgb(36, 255, 255, 255)))
                g.FillPolygon(light, new[] { new Point(r.X + 44, r.Y), new Point(r.Right - 44, r.Y), new Point(r.Right, r.Y + r.Height / 2), new Point(r.Right - 44, r.Bottom), new Point(r.X + 44, r.Bottom), new Point(r.X, r.Y + r.Height / 2) });
        int iconSize = r.Height * 3 / 5;
        int ix = r.X + 40, iy = r.Y + (r.Height - iconSize) / 2;
        if (icon != null)
            g.DrawImage(icon, ix + (iconSize - iconSize * icon.Width / Math.Max(icon.Width, icon.Height)) / 2, iy,
                        iconSize * icon.Width / Math.Max(icon.Width, icon.Height), iconSize * icon.Height / Math.Max(icon.Width, icon.Height));
        else
            DrawStar(g, ix + iconSize / 2f, iy + iconSize / 2f, iconSize / 2f);
        using (var font = new Font("Segoe UI Semibold", fontSize, GraphicsUnit.Pixel))
        using (var brush = new SolidBrush(busy && id == -1 ? Text2 : Text1))
        using (var left = new StringFormat { LineAlignment = StringAlignment.Center })
            g.DrawString(text, font, brush, new RectangleF(ix + iconSize + 22, r.Y, r.Width - iconSize - 70, r.Height), left);
    }

    static void DrawStar(Graphics g, float x, float y, float radius)
    {
        var points = new PointF[10];
        for (int i = 0; i < 10; i++)
        {
            double angle = -Math.PI / 2 + i * Math.PI / 5;
            float length = i % 2 == 0 ? radius : radius * 0.45f;
            points[i] = new PointF(x + (float)Math.Cos(angle) * length, y + (float)Math.Sin(angle) * length);
        }
        using (var brush = new LinearGradientBrush(new RectangleF(x - radius, y - radius, radius * 2, radius * 2), Color.FromArgb(255, 226, 120), Color.FromArgb(214, 142, 28), 90f))
            g.FillPolygon(brush, points);
    }

    void DrawWindowButton(Graphics g, Rectangle r, int id, string text)
    {
        using (var back = new SolidBrush(Color.FromArgb(hover == id ? 220 : 130, 12, 8, 18))) g.FillRectangle(back, r);
        using (var font = new Font("Segoe UI", 22f, GraphicsUnit.Pixel))
        using (var brush = new SolidBrush(hover == id ? Color.White : Gold))
        using (var center = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center })
            g.DrawString(text, font, brush, r, center);
    }

    int HitTest(Point p)
    {
        var d = new Point((int)(p.X / scale), (int)(p.Y / scale));
        if (CloseArea.Contains(d)) return -4;
        if (MinimizeArea.Contains(d)) return -3;
        if (PlayArea.Contains(d)) return -1;
        for (int i = 0; i < NavNames.Length; i++) if (NavArea(i).Contains(d)) return i;
        return -2;
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        int now = HitTest(e.Location);
        if (now != hover) { hover = now; Cursor = now == -2 ? Cursors.Default : Cursors.Hand; Invalidate(); }
    }

    protected override void OnMouseLeave(EventArgs e)
    {
        base.OnMouseLeave(e);
        hover = pressed = -2;
        Invalidate();
    }

    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam);

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        if (e.Button != MouseButtons.Left) return;
        pressed = HitTest(e.Location);
        Invalidate();
        if (pressed == -2 && e.Y < R(BannerArea.Bottom))
        {
            const int WM_NCLBUTTONDOWN = 0xA1, HTCAPTION = 2;
            ReleaseCapture();
            SendMessage(Handle, WM_NCLBUTTONDOWN, (IntPtr)HTCAPTION, IntPtr.Zero);
        }
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        base.OnMouseUp(e);
        int id = HitTest(e.Location), was = pressed;
        pressed = -2;
        Invalidate();
        if (id == was) Activate(id);
    }

    void Activate(int id)
    {
        if (id == -4) Close();
        else if (id == -3) WindowState = FormWindowState.Minimized;
        else if (id == -1) { if (BuildGame.Needed) BuildTheGame(); else Play(); }
        else if (id >= 0) ShowPage(id == page ? -1 : id);
    }

    // Keyboard: Up and Down walk the menu, Enter opens, Esc goes home. Tab reaches the page's controls, and a list or
    // text box keeps its own keys.
    protected override bool ProcessCmdKey(ref Message message, Keys key)
    {
        bool typing = ActiveControl is TextBoxBase || ActiveControl is ComboBox;
        if (key == Keys.Escape && page >= 0 && !building) { ShowPage(-1); return true; }
        if (!typing && (key == Keys.Up || key == Keys.Down))
        {
            int count = NavNames.Length + 1; // Play, then the menu
            int at = hover >= -1 ? hover + 1 : key == Keys.Up ? 0 : -1;
            hover = (at + (key == Keys.Up ? count - 1 : 1)) % count - 1;
            Invalidate();
            return true;
        }
        if (!typing && key == Keys.Enter && hover >= -1 && !(ActiveControl is ButtonBase)) { Activate(hover); return true; }
        return base.ProcessCmdKey(ref message, key);
    }

    // The launcher's look for a standard dialog.
    public static void Theme(Control root)
    {
        root.BackColor = WindowBack;
        root.ForeColor = Text1;
        foreach (Control c in root.Controls)
        {
            var button = c as Button;
            var list = c as ListView;
            var combo = c as ComboBox;
            if (button != null)
            {
                button.FlatStyle = FlatStyle.Flat;
                button.FlatAppearance.BorderColor = Gold;
                button.BackColor = Dark;
                button.ForeColor = Text1;
                button.Paint += delegate(object s, PaintEventArgs e)
                {
                    if (button.Enabled) return; // Windows greys this out until it can't be read on a dark button
                    var box = button.ClientRectangle;
                    using (var back = new SolidBrush(Dark)) e.Graphics.FillRectangle(back, box);
                    using (var edge = new Pen(Color.FromArgb(70, 62, 78))) e.Graphics.DrawRectangle(edge, 0, 0, box.Width - 1, box.Height - 1);
                    TextRenderer.DrawText(e.Graphics, button.Text, button.Font, box, Text2, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
                };
            }
            else if (list != null)
            {
                list.BackColor = Dark;
                list.ForeColor = Text1;
                list.BorderStyle = BorderStyle.FixedSingle;
                list.OwnerDraw = true;
                if (list.Columns.Count > 0) list.Columns[list.Columns.Count - 1].Width = -2; // no bare header beyond the columns
                list.DrawItem += delegate(object s, DrawListViewItemEventArgs e) { e.DrawDefault = true; };
                list.DrawSubItem += delegate(object s, DrawListViewSubItemEventArgs e) { e.DrawDefault = true; };
                list.DrawColumnHeader += delegate(object s, DrawListViewColumnHeaderEventArgs e)
                {
                    using (var back = new SolidBrush(WindowBack)) e.Graphics.FillRectangle(back, e.Bounds);
                    TextRenderer.DrawText(e.Graphics, e.Header.Text, list.Font, e.Bounds, Gold, TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
                };
            }
            else if (combo != null) DarkCombo(combo);
            else if (!(c is ProgressBar))
            {
                c.BackColor = WindowBack;
                if (c.ForeColor == SystemColors.ControlText) c.ForeColor = Text1;
                Theme2(c);
            }
        }
    }

    // Windows ignores a drop-down list's colours, so it is painted here.
    static void DarkCombo(ComboBox c)
    {
        c.FlatStyle = FlatStyle.Flat;
        c.BackColor = Dark;
        c.ForeColor = Text1;
        c.DrawMode = DrawMode.OwnerDrawFixed;
        c.DrawItem += delegate(object s, DrawItemEventArgs e)
        {
            bool selected = (e.State & DrawItemState.Selected) != 0 && (e.State & DrawItemState.ComboBoxEdit) == 0;
            using (var back = new SolidBrush(selected ? Color.FromArgb(70, 52, 22) : Dark)) e.Graphics.FillRectangle(back, e.Bounds);
            if (e.Index >= 0)
                TextRenderer.DrawText(e.Graphics, c.Items[e.Index].ToString(), c.Font, e.Bounds, c.Enabled ? Text1 : Text2, TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
        };
    }

    static void Theme2(Control c) { if (c.Controls.Count > 0) { Color keep = c.ForeColor; Theme(c); c.ForeColor = keep; } }

    public void ShowPage(int index)
    {
        page = index;
        for (int i = 0; i < pages.Length; i++) pages[i].Visible = i == index;
        Invalidate();
    }

    // ---- pages ----

    Panel NewPage(int index, string title)
    {
        var p = new Panel { Bounds = R(PageArea), BackgroundImage = pageBack, BackgroundImageLayout = ImageLayout.None, Visible = false };
        pages[index] = p;
        p.Controls.Add(new Label
        {
            Text = title, AutoSize = true, BackColor = Color.Transparent, ForeColor = Gold, Location = new Point(R(28), R(16)),
            Font = new Font("Georgia", 26f * scale, FontStyle.Bold, GraphicsUnit.Pixel)
        });
        Controls.Add(p);
        return p;
    }

    Label Text2Label(Panel p, string text, int x, int y, int w, int h, Color color, float size = 16f)
    {
        var l = new Label { Text = text, BackColor = Color.Transparent, ForeColor = color, Bounds = R(new Rectangle(x, y, w, h)), Font = new Font("Segoe UI", size * scale, GraphicsUnit.Pixel) };
        p.Controls.Add(l);
        return l;
    }

    void Tick(Panel p, CheckBox c, string text, bool value, int x, int y, int w, Action changed)
    {
        c.Text = text;
        c.Checked = value;
        c.BackColor = Color.Transparent;
        c.ForeColor = Text1;
        c.Font = new Font("Segoe UI", 17f * scale, GraphicsUnit.Pixel);
        c.Bounds = R(new Rectangle(x, y, w, 30));
        c.CheckedChanged += delegate { changed(); };
        p.Controls.Add(c);
    }

    void Choice(Panel p, ComboBox c, string label, object[] items, int value, int y, Action changed)
    {
        Text2Label(p, label, 28, y + 4, 150, 30, Text1, 17f);
        c.DropDownStyle = ComboBoxStyle.DropDownList;
        DarkCombo(c);
        c.Font = new Font("Segoe UI", 17f * scale, GraphicsUnit.Pixel);
        c.Items.AddRange(items);
        c.SelectedIndex = value;
        c.Bounds = R(new Rectangle(190, y, 620, 30));
        c.SelectedIndexChanged += delegate { changed(); };
        p.Controls.Add(c);
    }

    // A label and a small drop-down on one row (for settings under a tick).
    void Small(Panel p, ComboBox c, string label, object[] items, int value, int x, int y, int labelWidth, int width, Action changed)
    {
        Text2Label(p, label, x, y + 4, labelWidth, 30, Text1, 17f);
        c.DropDownStyle = ComboBoxStyle.DropDownList;
        DarkCombo(c);
        c.Font = new Font("Segoe UI", 17f * scale, GraphicsUnit.Pixel);
        c.Items.AddRange(items);
        c.SelectedIndex = value;
        c.Bounds = R(new Rectangle(x + labelWidth, y, width, 30));
        c.SelectedIndexChanged += delegate { changed(); };
        p.Controls.Add(c);
    }

    void RefreshCamera()
    {
        cameraSpeed.Enabled = cameraReturn.Enabled = cameraInvert.Enabled = freeCamera.Checked;
    }

    void Action2(Panel p, Button b, string text, int x, int y, int w, EventHandler click)
    {
        b.Text = text;
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = Gold;
        b.BackColor = Dark;
        b.ForeColor = Text1;
        b.Cursor = Cursors.Hand;
        b.Font = new Font("Segoe UI", 16f * scale, GraphicsUnit.Pixel);
        b.Bounds = R(new Rectangle(x, y, w, 40));
        b.Click += click;
        p.Controls.Add(b);
    }

    // ---- the Randomizer page (Randomizer.cs) ----
    void BuildRandomizerPage(Panel p, ToolTip tips)
    {
        Tick(p, rndOn, "Randomize the game (the game does it as each level loads)", Randomizer.Enabled, 28, 72, 600,
             delegate { Randomizer.Enabled = rndOn.Checked; RefreshRandomizer(); Picture.Save(); });
        Text2Label(p, "Always with the cross-world enemies and structures, Expanded memory, a raised enemy budget and the " +
                      "randomizer's gate and key safety: keys, gates, level columns and the level-finishing groups are never touched.", 56, 102, 950, 44, Text2, 15f);
        Text2Label(p, "Seed", 28, 152, 60, 30, Text1, 17f);
        rndSeed.Text = Randomizer.Seed > 0 ? Randomizer.Seed.ToString() : "";
        rndSeed.Bounds = R(new Rectangle(92, 150, 150, 30));
        rndSeed.Font = new Font("Segoe UI", 16f * scale, GraphicsUnit.Pixel);
        rndSeed.MaxLength = 9;
        rndSeed.TextChanged += delegate
        {
            int n;
            Randomizer.Seed = int.TryParse(rndSeed.Text.Trim(), out n) && n > 0 ? n : 0;
            Picture.Save();
        };
        p.Controls.Add(rndSeed);
        tips.SetToolTip(rndSeed, "The same seed and switches always make the same game. Empty: a new seed on the next Play.");
        Action2(p, rndNewSeed, "New seed", 256, 148, 140, delegate { rndSeed.Text = Randomizer.NewSeed().ToString(); });
        Tick(p, rndEnemies, "Enemies", Randomizer.Enemies, 28, 194, 300, delegate { Randomizer.Enemies = rndEnemies.Checked; RefreshRandomizer(); Picture.Save(); });
        Tick(p, rndStructures, "Structures", Randomizer.Structures, 360, 194, 300, delegate { Randomizer.Structures = rndStructures.Checked; Picture.Save(); });
        Tick(p, rndChests, "Chests (and what is in them)", Randomizer.Chests, 660, 194, 340, delegate { Randomizer.Chests = rndChests.Checked; Picture.Save(); });
        Tick(p, rndItems, "Items", Randomizer.Items, 28, 228, 300, delegate { Randomizer.Items = rndItems.Checked; Picture.Save(); });
        Tick(p, rndSkills, "Abilities and skills", Randomizer.Skills, 360, 228, 300, delegate { Randomizer.Skills = rndSkills.Checked; Picture.Save(); });
        Tick(p, rndBossEnemies, "Bosses as normal enemies: a boss can turn up in a level (experimental)", Randomizer.BossesAsEnemies, 56, 266, 940,
             delegate { Randomizer.BossesAsEnemies = rndBossEnemies.Checked; Picture.Save(); });
        tips.SetToolTip(rndBossEnemies, "Now and then (2 a level at most) an enemy is swapped for one of the six bosses (the Spawn editor's \"Bosses as enemies\" list).");
        Tick(p, rndBossArenas, "Randomize boss fights: each boss arena gets another of the six bosses in place of its own (experimental)", Randomizer.BossArenas, 56, 300, 940,
             delegate { Randomizer.BossArenas = rndBossArenas.Checked; Picture.Save(); });
        tips.SetToolTip(rndBossArenas, "The arena's own boss is swapped for another boss (picked by the seed, from the Spawn editor's \"Boss arenas\" list); beating it ends the arena as usual.");
        Action2(p, rndSpawnEditor, "Spawn editor...", 28, 346, 220, delegate
        {
            SpawnEditorForm.ShowEditor(this, delegate { RefreshRandomizer(); });
        });
        tips.SetToolTip(rndSpawnEditor, "What can appear and how often: enemies, bosses, items, structures, abilities and skills, and what chests hold.");
        Action2(p, rndBalanced, "Back to the balanced rates", 260, 346, 300, delegate
        {
            if (MessageBox.Show(this, "Put the spawn rates back to the launcher's balanced ones?", Build.Name, MessageBoxButtons.YesNo) == DialogResult.Yes)
                Randomizer.ResetSpawnRates();
            RefreshRandomizer();
        });
        rndState.BackColor = Color.Transparent;
        rndState.ForeColor = Text2;
        rndState.Font = new Font("Segoe UI", 15f * scale, GraphicsUnit.Pixel);
        rndState.Bounds = R(new Rectangle(28, 396, 980, 80));
        p.Controls.Add(rndState);
        RefreshRandomizer();
    }

    void RefreshRandomizer()
    {
        foreach (Control c in new Control[] { rndSeed, rndNewSeed, rndEnemies, rndStructures, rndItems, rndSkills, rndChests, rndSpawnEditor, rndBalanced })
            c.Enabled = Randomizer.Enabled;
        rndBossEnemies.Enabled = rndBossArenas.Enabled = Randomizer.Enabled && Randomizer.Enemies;
        rndState.Text = !Randomizer.Enabled
            ? (Randomizer.Applied ? "Off: the original game files come back on the next Play." : "Off.")
            : (Randomizer.CustomRates ? "Spawn rates: yours (randomizer_rates.txt)." : "Spawn rates: the balanced ones.") +
              " Nothing waits at Play: each level is randomized in the game as it loads, the same way every time for the same seed.";
    }

    void BuildPages()
    {
        var tips = new ToolTip();
        Panel p = NewPage(0, "Graphics");
        Choice(p, picture, "Picture", Picture.Names, Picture.Mode, 80, delegate { Picture.Mode = picture.SelectedIndex; Picture.Save(); });
        Choice(p, scaling, "Scaling", Picture.ScalingNames, Picture.Scaling, 126, delegate { Picture.Scaling = scaling.SelectedIndex; Picture.Save(); });
        tips.SetToolTip(scaling, "How the picture is stretched to the window: sharp pixels, smooth, or AMD FSR (sharpest, on the graphics card)");
        Tick(p, borderless, "Borderless fullscreen (Alt+Enter switches in the game)", Picture.Borderless, 28, 186, 800, delegate { Picture.Borderless = borderless.Checked; Picture.Save(); });
        Tick(p, vsync, "VSync", Picture.VSync, 28, 222, 800, delegate { Picture.VSync = vsync.Checked; Picture.Save(); });
        Text2Label(p, "Stops the picture tearing. Turn it off only if the game feels delayed.", 56, 252, 800, 26, Text2);

        p = NewPage(1, "Controls");
        Text2Label(p, "Controller", 28, 74, 900, 28, Gold, 19f);
        Text2Label(p, "Xbox, PlayStation (DualSense, DualShock 4) and most other controllers work. Cross / A = Cross, " +
                      "Circle / B = Circle, and so on.", 28, 104, 980, 54, Text1, 17f);
        Text2Label(p, "Keyboard", 28, 164, 900, 28, Gold, 19f);
        Text2Label(p, "", 28, 194, 980, 84, Text1, 17f).Name = "keyboardSummary";
        Action2(p, controlsButton, "Change controls...", 28, 284, 240, delegate
        {
            using (var form = new ControlsForm()) form.ShowDialog(this);
            Text2Label(p, "Hold L1 + R1 and press Select = debug menu (free camera)", 28, 410, 980, 30, Text1, 17f);
        RefreshControls();
        });
        Text2Label(p, "Keys and controller buttons, for the keyboard and any controller.", 284, 292, 720, 28, Text2);
        Text2Label(p, "In the game", 28, 350, 900, 28, Gold, 19f);
        Text2Label(p, "F12 = screenshot for the banner     Alt+Enter = fullscreen or window     Esc = quit the game", 28, 380, 980, 30, Text1, 17f);
        Text2Label(p, "Hold L1 + R1 and press Select = debug menu (free camera)", 28, 410, 980, 30, Text1, 17f);
        RefreshControls();

        p = NewPage(2, "Enhancements");
        Choice(p, aspect, "Aspect", Picture.AspectNames, Picture.Aspect, 80, delegate { Picture.Aspect = aspect.SelectedIndex; Picture.Save(); });
        if (Picture.FrameRateNames.Length > 1) // (the USA and Japanese releases have only their own 60 Hz)
            Choice(p, frameRate, "Frame rate", Picture.FrameRateNames, Picture.FrameRate, 126, delegate { Picture.FrameRate = frameRate.SelectedIndex; Picture.Save(); });
        Tick(p, softParticles, "Smooth effects", Picture.SoftParticles, 28, 178, 900, delegate { Picture.SoftParticles = softParticles.Checked; Picture.Save(); });
        Text2Label(p, "Fire, smoke, fairies and sparkles at the full upscaled resolution (not in Original).", 56, 204, 900, 24, Text2);
        Tick(p, vu1Thread, "Faster 3D: use a second CPU core for the 3D graphics", Picture.Vu1Thread, 28, 230, 900, delegate { Picture.Vu1Thread = vu1Thread.Checked; vu1Pipeline.Enabled = vu1Thread.Checked; Picture.Save(); });
        Tick(p, vu1Pipeline, "Overlap frames: draw each frame while the game works on the next", Picture.Vu1Pipeline, 56, 262, 870, delegate { Picture.Vu1Pipeline = vu1Pipeline.Checked; Picture.Save(); });
        vu1Pipeline.Enabled = Picture.Vu1Thread;
        Tick(p, freeCamera, "Free camera: the right stick turns the camera around Maximo (experimental)", Picture.FreeCamera, 28, 296, 960,
             delegate { Picture.FreeCamera = freeCamera.Checked; RefreshCamera(); Picture.Save(); });
        Small(p, cameraSpeed, "Speed", Picture.CameraSpeedNames, Picture.CameraSpeed, 56, 332, 70, 170,
              delegate { Picture.CameraSpeed = cameraSpeed.SelectedIndex; Picture.Save(); });
        Small(p, cameraReturn, "Swing back behind Maximo", Picture.CameraReturnNames, Picture.CameraReturn, 330, 332, 230, 200,
              delegate { Picture.CameraReturn = cameraReturn.SelectedIndex; Picture.Save(); });
        tips.SetToolTip(cameraReturn, "How long after you let go of the right stick the camera turns back behind Maximo by itself");
        Tick(p, cameraInvert, "Invert up/down", Picture.CameraInvertY, 56, 366, 400, delegate { Picture.CameraInvertY = cameraInvert.Checked; Picture.Save(); });
        Text2Label(p, "R3 (click the right stick) swings it back at once. Keyboard: I J K L turn it, R swings it back (see Controls).", 56, 396, 900, 26, Text2);
        RefreshCamera();
        Tick(p, skipIntro, "Skip the intro: start at the main menu", Picture.SkipIntro, 28, 428, 900, delegate { Picture.SkipIntro = skipIntro.Checked; Picture.Save(); });
        Text2Label(p, "Leaves out the Capcom logos, the copyright notice and the title screen.", 56, 454, 950, 26, Text2);

        p = NewPage(3, "Randomizer");
        BuildRandomizerPage(p, tips);

        p = NewPage(4, "HD Textures");
        Tick(p, hdTextures, "Use HD textures", HdTextures.Enabled, 28, 80, 500, delegate { HdTextures.Enabled = hdTextures.Checked; Picture.Save(); });
        Action2(p, hdTexturesButton, "Manage texture packs...", 28, 124, 280, delegate
        {
            using (var form = new HdTexturesForm()) form.ShowDialog(this);
            hdTextures.Checked = HdTextures.Enabled;
        });
        Text2Label(p, "Download Marcus' HD texture pack, install other packs, and choose which are used. It also gives the game's HUD its HD look.", 28, 182, 960, 54, Text2, 17f);
        Text2Label(p, "HD textures by Marcus of \"Maximo Capcom Videos\".", 28, 244, 960, 30, Text1, 17f);

        p = NewPage(5, "Game Files");
        Action2(p, setupIso, "Set up from ISO...", 28, 74, 220, delegate { ChooseIso(); });
        Action2(p, setupFolder, "...or from a folder", 258, 74, 220, delegate { ChooseFolder(); });
        Action2(p, openSaves, "Saves", 488, 74, 150, delegate { Open(Paths.MemoryCard); });
        Action2(p, openLogs, "Logs", 648, 74, 150, delegate { Open(Paths.Logs); });
        Action2(p, openShots, "Screenshots", 808, 74, 180, delegate { Open(Paths.Screenshots); });
        Text2Label(p, "The banner shows your two newest screenshots. Delete a file from Screenshots to take it off the banner.", 28, 122, 980, 28, Text2);
        Text2Label(p, "Problem to report? Say where you were and what you saw. It goes into the zip.", 28, 160, 980, 28, Text1, 17f);
        notes.Multiline = true;
        notes.ScrollBars = ScrollBars.Vertical;
        notes.AcceptsReturn = true;
        notes.BackColor = Dark;
        notes.ForeColor = Text1;
        notes.BorderStyle = BorderStyle.FixedSingle;
        notes.Font = new Font("Segoe UI", 16f * scale, GraphicsUnit.Pixel);
        notes.Bounds = R(new Rectangle(28, 192, 980, 120));
        p.Controls.Add(notes);
        Action2(p, zipLogs, "Zip logs for a bug report", 28, 324, 280, delegate { ZipLogs(); });
        Action2(p, rebuild, "Update and rebuild the game", 318, 324, 300, delegate { StartBuild(); });
        Tick(p, recordAudio, "Record the game's audio too (only for sound problems; about 11 MB a minute)", false, 28, 374, 980, delegate { });
        lastRun.BackColor = Color.Transparent;
        lastRun.ForeColor = Text2;
        lastRun.Font = new Font("Segoe UI", 15f * scale, GraphicsUnit.Pixel);
        lastRun.Bounds = R(new Rectangle(28, 412, 980, 64));
        p.Controls.Add(lastRun);

        p = NewPage(BuildPage, "Building the game");
        buildStage.BackColor = Color.Transparent;
        buildStage.ForeColor = Text1;
        buildStage.Font = new Font("Segoe UI", 18f * scale, GraphicsUnit.Pixel);
        buildStage.Bounds = R(new Rectangle(28, 70, 980, 32));
        p.Controls.Add(buildStage);
        buildBar.Bounds = R(new Rectangle(28, 108, 980, 22));
        p.Controls.Add(buildBar);
        Text2Label(p, "The game is compiled here from your own disc, so this can take a while. You can use your PC meanwhile.", 28, 140, 980, 28, Text2);
        buildLog.Multiline = true;
        buildLog.ReadOnly = true;
        buildLog.WordWrap = false;
        buildLog.BackColor = Dark;
        buildLog.ForeColor = Text2;
        buildLog.BorderStyle = BorderStyle.FixedSingle;
        buildLog.Font = new Font("Consolas", 13f * scale, GraphicsUnit.Pixel);
        buildLog.Bounds = R(new Rectangle(28, 174, 980, 252));
        p.Controls.Add(buildLog);
        Action2(p, cancelBuild, "Cancel", 28, 436, 160, delegate { cancelBuild.Enabled = false; buildStage.Text = "Cancelling..."; Builder.Cancel(); });

        p = NewPage(6, "About");
        Text2Label(p, Build.Name, 28, 80, 980, 30, Text1, 19f);
        Text2Label(p, "Version: " + Build.Version, 28, 114, 980, 54, Text2);
        Text2Label(p, "A native PC port of the PAL PlayStation 2 game, made by recompiling the game from your own disc. " +
                      "This project contains no game code or data: you need your own copy of the game.", 28, 182, 980, 60, Text1, 17f);
        Text2Label(p, "This is an unofficial fan project. It is not affiliated with or endorsed by Capcom.", 28, 256, 980, 30, Text1, 17f);

        p = NewPage(7, "Credits");
        Text2Label(p, "Biorhythmic Studios", 28, 74, 460, 28, Gold, 20f);
        Text2Label(p, "Native PC port", 500, 76, 500, 28, Text1, 17f);
        Text2Label(p, "Marcus of \"Maximo Capcom Videos\"", 28, 108, 460, 28, Gold, 20f);
        Text2Label(p, "Owner and creator of the HD textures", 500, 110, 500, 28, Text1, 17f);
        Text2Label(p, "Santins", 28, 142, 460, 28, Gold, 20f);
        Text2Label(p, "Tester", 500, 144, 500, 28, Text1, 17f);
        Text2Label(p, "Built with PS2Recomp, paraLLEl-GS, Granite, raylib, FFmpeg, miniz, bc7enc and stb. Their licences are in the LICENSES folder.", 28, 192, 980, 52, Text2);
        Text2Label(p, "© CAPCOM CO., LTD. 2002, © CAPCOM U.S.A., INC. 2002. ALL RIGHTS RESERVED.\r\n" +
                      "MAXIMO developed by CAPCOM DIGITAL STUDIOS, INC.\r\n" +
                      "CHARACTERS by © 2002 SUSUMU MATSUSHITA COMPANY / MASTER LICENSOR DREAM RANCH INC.\r\n" +
                      "All rights to Maximo: Ghosts to Glory are reserved to Capcom.", 28, 256, 980, 112, Text1);
        Text2Label(p, "Unofficial fan project. Not affiliated with or endorsed by Capcom.", 28, 376, 980, 28, Text2);
    }

    void RefreshControls()
    {
        Control[] found = pages[1].Controls.Find("keyboardSummary", false);
        if (found.Length > 0) found[0].Text = Bindings.KeyboardSummary();
    }

    // ---- the game ----

    void Refresh2()
    {
        string why;
        bool ready = Setup.DiscReady(out why);
        if (BuildGame.Needed)
        {
            status.Text = BuildGame.CanUpdate ? "A newer version of the mods is here: press Update (only what changed is recompiled)."
                                              : "The game isn't built yet: press Build and choose your own Maximo disc image (.iso, .bin or .cue).";
            status.ForeColor = Gold;
            return;
        }
        if (!File.Exists(Paths.Game)) { ready = false; why = "The game isn't built yet: press Build."; }
        status.Text = ready || !File.Exists(Paths.Game) ? why : why + " Open Game Files and choose your own Maximo " + Build.DiscTitle + " disc image (.iso, .bin or .cue).";
        status.ForeColor = ready ? Good : Bad;
    }

    bool DiscUsable()
    {
        string why;
        return Setup.DiscReady(out why) && File.Exists(Paths.Game);
    }

    void EnableAll(Control parent, bool enabled)
    {
        foreach (Control c in parent.Controls)
        {
            if (c is Button || c is CheckBox || c is ComboBox) c.Enabled = enabled;
            EnableAll(c, enabled);
        }
    }

    void Busy(bool on)
    {
        busy = on;
        EnableAll(this, !on);
        cancelBuild.Enabled = building;
        if (!on) { vu1Pipeline.Enabled = vu1Thread.Checked; RefreshCamera(); Refresh2(); RefreshBanner(); }
        Invalidate();
    }

    void RunInBackground(Action work, string doneText)
    {
        Busy(true);
        var t = new Thread(() =>
        {
            string error = null;
            try { work(); }
            catch (Exception e) { error = e.Message; Log.Write("error: " + e); }
            BeginInvoke((Action)(() =>
            {
                Busy(false);
                if (error != null) MessageBox.Show(this, error, Build.Name, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                else if (doneText != null) status.Text = doneText;
            }));
        });
        t.IsBackground = true;
        t.Start();
    }

    void Progress(string text) { BeginInvoke((Action)(() => { status.ForeColor = Text1; status.Text = text; })); }

    void ChooseIso()
    {
        using (var dialog = new OpenFileDialog { Filter = "Disc images (*.iso, *.cue, *.bin)|*.iso;*.cue;*.bin|All files (*.*)|*.*", Title = "Your Maximo: Ghosts to Glory " + Build.DiscTitle + " disc image" })
            if (dialog.ShowDialog(this) == DialogResult.OK)
                RunInBackground(() => Setup.Install(dialog.FileName, Progress), null);
    }

    void ChooseFolder()
    {
        using (var dialog = new FolderBrowserDialog { Description = "The folder (or drive) with the extracted Maximo " + Build.DiscTitle + " disc, containing " + Build.ElfName })
            if (dialog.ShowDialog(this) == DialogResult.OK)
                RunInBackground(() => Setup.Install(dialog.SelectedPath, Progress), null);
    }

    // The mod package's launcher: builds the game from the player's disc, then hands over to the launcher it built.
    void BuildTheGame()
    {
        if (busy) return;
        bool built;
        using (var form = new BuildGameForm())
        {
            form.ShowDialog(this);
            built = form.Succeeded;
        }
        if (built && BuildGame.OpenBuilt()) { Close(); return; }
        Refresh2();
        Invalidate();
    }

    // ---- the Build button (Builder.cs) ----

    void StartBuild()
    {
        if (busy) return;
        string why, iso = null;
        if (!Setup.DiscReady(out why))
        {
            using (var dialog = new OpenFileDialog { Filter = "Disc images (*.iso, *.cue, *.bin)|*.iso;*.cue;*.bin|All files (*.*)|*.*", Title = "Your Maximo: Ghosts to Glory disc image" })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                iso = dialog.FileName;
            }
        }
        string message = "This compiles the game on your PC from your own disc. It downloads the source code and libraries (under 1 GB) " +
                         "and needs about 4 GB of disk space while it builds. Afterwards only the game (about 60 MB) stays. It can take a while.";
        if (Builder.NeedsCppTools)
            message += "\r\n\r\nYour PC also needs Microsoft's free C++ Build Tools, a further download of several GB that needs about 8 GB more. " +
                       "Windows will ask for your permission, and they are covered by Microsoft's licence terms.";
        if (MessageBox.Show(this, message + "\r\n\r\nStart now?", Build.Name, MessageBoxButtons.OKCancel, MessageBoxIcon.Information) != DialogResult.OK)
            return;

        var recent = new System.Collections.Generic.Queue<string>();
        Action<string> line = text =>
        {
            lock (recent)
            {
                recent.Enqueue(text);
                while (recent.Count > 200) recent.Dequeue();
            }
        };
        Action<string, int> stage = (text, percent) =>
        {
            if (!IsHandleCreated) return;
            BeginInvoke((Action)(() =>
            {
                if (cancelBuild.Enabled) buildStage.Text = text;
                buildBar.Style = percent < 0 ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
                if (percent >= 0) buildBar.Value = Math.Min(100, percent);
            }));
        };
        var view = new System.Windows.Forms.Timer { Interval = 500 };
        Action showLog = () =>
        {
            string[] shown;
            lock (recent) shown = recent.ToArray();
            string joined = string.Join("\r\n", shown);
            if (joined == buildLog.Text) return;
            buildLog.Text = joined;
            buildLog.SelectionStart = buildLog.TextLength;
            buildLog.ScrollToCaret();
        };
        view.Tick += delegate { showLog(); };

        building = true;
        buildStage.Text = "Starting...";
        buildLog.Text = "";
        ShowPage(BuildPage);
        view.Start();
        Busy(true);
        var worker = new Thread(() =>
        {
            string error = null;
            bool stopped = false;
            try
            {
                if (iso != null)
                {
                    stage("Reading your disc", -1);
                    Setup.Install(iso, text => stage(text, -1));
                }
                Builder.Run(stage, line);
            }
            catch (OperationCanceledException e) { stopped = true; error = e.Message; }
            catch (Exception e) { error = e.Message; Log.Write("build: " + e); }
            if (!IsHandleCreated) return;
            BeginInvoke((Action)(() =>
            {
                view.Stop();
                view.Dispose();
                showLog();
                building = false;
                buildBar.Style = ProgressBarStyle.Continuous;
                buildBar.Value = error == null ? 100 : 0;
                buildStage.Text = error ?? "Done. Press Play.";
                Busy(false);
                if (error != null && !stopped)
                    MessageBox.Show(this, error, Build.Name, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                else if (error == null)
                    ShowPage(-1);
            }));
        });
        worker.IsBackground = true;
        worker.Start();
    }

    void Play()
    {
        if (building) { ShowPage(BuildPage); return; }
        if (busy) return;
        if (!File.Exists(Paths.Game)) { StartBuild(); return; }
        if (!DiscUsable()) { ShowPage(5); return; }
        int dropIn;
        if (HdTextures.Enabled && HdTextures.DropInChanged(out dropIn))
        {
            var answer = MessageBox.Show(this, "textures\\replacements has " + dropIn + " PNG files that are not converted yet " +
                                               "(or changed since). Convert them first?",
                                         Build.Name, MessageBoxButtons.YesNoCancel, MessageBoxIcon.Question);
            if (answer == DialogResult.Cancel) return;
            if (answer == DialogResult.Yes)
            {
                using (var form = new HdTexturesForm()) { form.Shown += delegate { form.ConvertDropInNow(); }; form.ShowDialog(this); }
                hdTextures.Checked = HdTextures.Enabled;
                return;
            }
        }
        bool audio = recordAudio.Checked;
        status.ForeColor = Text1;
        status.Text = "Playing. Close the game window to stop.";
        string result = null;
        CrossWorld.OnProgress = text => BeginInvoke((Action)(() =>
        {
            status.ForeColor = Text1;
            status.Text = text ?? "Playing. Close the game window to stop.";
        }));
        Randomizer.OnProgress = CrossWorld.OnProgress;
        Game.OnWarning = text => BeginInvoke((Action)(() =>
            MessageBox.Show(this, text, Build.Name, MessageBoxButtons.OK, MessageBoxIcon.Warning)));
        RunInBackground(() => { result = Game.Run(86400, audio); BeginInvoke((Action)(() => lastRun.Text = result)); }, null);
    }

    void ZipLogs()
    {
        try
        {
            if (string.IsNullOrWhiteSpace(notes.Text) &&
                MessageBox.Show(this, "You haven't described what happened. Zip the logs anyway?\r\n(A sentence about where you were and what you saw helps a lot.)",
                                Build.Name, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
            {
                notes.Focus();
                return;
            }
            string zip = Game.ZipLatest(notes.Text);
            Process.Start("explorer.exe", "/select,\"" + zip + "\"");
        }
        catch (Exception e) { MessageBox.Show(this, e.Message, Build.Name, MessageBoxButtons.OK, MessageBoxIcon.Information); }
    }

    static void Open(string folder)
    {
        Directory.CreateDirectory(folder);
        Process.Start("explorer.exe", "\"" + folder + "\"");
    }
}
