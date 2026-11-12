// Key and controller bindings, saved in controls.ini beside the launcher. The game reads that file itself
// (ps2_pad.cpp in the runtime: PS2X_CONTROLS, or controls.ini in the folder above game\maximo_boot.exe), so a game
// updated with "Update and rebuild" uses it even with an older launcher.
//
// Keys are stored by position, as raylib/GLFW key names (US layout), because that is how the game reads them; the
// launcher records the key's scan code and shows the label of the player's own layout (GetKeyNameText).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

static class Bindings
{
    public static readonly string File_ = Path.Combine(Paths.Root, "controls.ini");

    // Same order and names as kActions in ps2_pad.cpp.
    public static readonly string[] Actions =
    {
        "up", "down", "left", "right", "cross", "circle", "square", "triangle",
        "l1", "r1", "l2", "r2", "l3", "r3", "start", "select",
        "stick_up", "stick_down", "stick_left", "stick_right",
        "camera_up", "camera_down", "camera_left", "camera_right"
    };
    public static readonly string[] Labels =
    {
        "D-pad up", "D-pad down", "D-pad left", "D-pad right",
        "Cross  ✕", "Circle  ○", "Square  □", "Triangle  △",
        "L1", "R1", "L2", "R2", "L3 (press left stick)", "R3 (press right stick)", "Start", "Select",
        "Left stick up", "Left stick down", "Left stick left", "Left stick right",
        "Camera up", "Camera down", "Camera left", "Camera right"
    };
    public const int StickFirst = 16, CameraFirst = 20;

    // Defaults: the game's own (kDefaultKeys / kDefaultPad in ps2_pad.cpp).
    static readonly string[][] DefaultKeys =
    {
        new[] { "UP", "W" }, new[] { "DOWN", "S" }, new[] { "LEFT", "A" }, new[] { "RIGHT", "D" },
        new[] { "X", "SPACE" }, new[] { "C" }, new[] { "Z", "KP_0" }, new[] { "V", "KP_1" },
        new[] { "Q" }, new[] { "E" }, new[] { "LEFT_SHIFT" }, new[] { "RIGHT_SHIFT" }, new string[0], new[] { "R" },
        new[] { "ENTER" }, new[] { "TAB" },
        new string[0], new string[0], new string[0], new string[0],
        new[] { "I" }, new[] { "K" }, new[] { "J" }, new[] { "L" }
    };
    static readonly string[] DefaultPad =
    {
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "SOUTH", "EAST", "WEST", "NORTH",
        "L1", "R1", "L2", "R2", "L3", "R3", "START", "SELECT", "", "", "", "", "", "", "", ""
    };

    // Controller choices: game name, then what the player sees.
    public static readonly string[,] PadChoices =
    {
        { "", "(none)" },
        { "SOUTH", "Cross / A  (bottom)" }, { "EAST", "Circle / B  (right)" },
        { "WEST", "Square / X  (left)" }, { "NORTH", "Triangle / Y  (top)" },
        { "DPAD_UP", "D-pad up" }, { "DPAD_DOWN", "D-pad down" }, { "DPAD_LEFT", "D-pad left" }, { "DPAD_RIGHT", "D-pad right" },
        { "L1", "L1 / LB" }, { "R1", "R1 / RB" }, { "L2", "L2 / LT" }, { "R2", "R2 / RT" },
        { "L3", "L3  (press left stick)" }, { "R3", "R3  (press right stick)" },
        { "START", "Options / Start / Menu" }, { "SELECT", "Create / Share / Back / View" }
    };

    public const int KeysPerAction = 2;
    public static string[][] Keys = Copy(DefaultKeys);
    public static string[] Pad = (string[])DefaultPad.Clone();

    static string[][] Copy(string[][] k) { return k.Select(a => (string[])a.Clone()).ToArray(); }

    public static string[][] DefaultKeysCopy() { return Copy(DefaultKeys); }
    public static string[] DefaultPadCopy() { return (string[])DefaultPad.Clone(); }

    public static bool IsDefault()
    {
        for (int a = 0; a < Actions.Length; a++)
            if (!Keys[a].SequenceEqual(DefaultKeys[a]) || Pad[a] != DefaultPad[a]) return false;
        return true;
    }

    public static void Load()
    {
        try
        {
            if (!File.Exists(File_)) return;
            int section = -1;
            foreach (string raw in File.ReadAllLines(File_))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line[0] == ';' || line[0] == '#') continue;
                if (line.Equals("[keyboard]", StringComparison.OrdinalIgnoreCase)) { section = 0; continue; }
                if (line.Equals("[controller]", StringComparison.OrdinalIgnoreCase)) { section = 1; continue; }
                string[] kv = line.Split(new[] { '=' }, 2);
                int a = Array.IndexOf(Actions, kv[0].Trim().ToLowerInvariant());
                if (kv.Length != 2 || a < 0 || section < 0) continue;
                string[] names = kv[1].Split(',').Select(n => n.Trim().ToUpperInvariant()).Where(n => n.Length > 0).ToArray();
                if (section == 0) Keys[a] = names.Where(n => KeyNames.Contains(n)).Take(KeysPerAction).ToArray();
                else Pad[a] = names.FirstOrDefault(n => PadIndex(n) > 0) ?? "";
            }
        }
        catch (IOException) { }
    }

    public static void Save()
    {
        var s = new StringBuilder();
        s.Append("; Maximo controls, written by the launcher (Controls page). The game reads this file itself.\r\n");
        s.Append("; Key names are by position on a US keyboard; controller names: SOUTH EAST WEST NORTH, DPAD_UP...,\r\n");
        s.Append("; L1 R1 L2 R2 L3 R3, START, SELECT. stick_ lines push the left stick fully,\r\n");
        s.Append("; camera_ lines the right stick (turns the free camera). Delete this file for the defaults.\r\n");
        s.Append("[keyboard]\r\n");
        for (int a = 0; a < Actions.Length; a++) s.Append(Actions[a] + " = " + string.Join(", ", Keys[a]) + "\r\n");
        s.Append("[controller]\r\n");
        for (int a = 0; a < Actions.Length; a++) s.Append(Actions[a] + " = " + Pad[a] + "\r\n");
        try
        {
            if (IsDefault()) { if (File.Exists(File_)) File.Delete(File_); }
            else File.WriteAllText(File_, s.ToString());
        }
        catch (IOException e) { Log.Write("controls: could not save: " + e.Message); }
    }

    // Tells the game where the file is, so a launcher-started game never depends on the folder layout.
    public static void Apply(ProcessStartInfo info)
    {
        if (File.Exists(File_) && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PS2X_CONTROLS")))
            info.EnvironmentVariables["PS2X_CONTROLS"] = File_;
    }

    public static string Describe() { return IsDefault() ? "" : ", custom controls"; }

    // ---- names ----

    public static int PadIndex(string name)
    {
        for (int i = 1; i < PadChoices.GetLength(0); i++) if (PadChoices[i, 0] == name) return i;
        return 0;
    }

    // GLFW's Windows scan-code table (win32_init.c), limited to the names the game accepts (kKeyNames in
    // ps2_pad.cpp). Extended keys carry 0x100. Esc and F12 are absent on purpose: the game window keeps them.
    static readonly Dictionary<int, string> ByScanCode = new Dictionary<int, string>
    {
        { 0x00B, "0" }, { 0x002, "1" }, { 0x003, "2" }, { 0x004, "3" }, { 0x005, "4" }, { 0x006, "5" }, { 0x007, "6" },
        { 0x008, "7" }, { 0x009, "8" }, { 0x00A, "9" },
        { 0x01E, "A" }, { 0x030, "B" }, { 0x02E, "C" }, { 0x020, "D" }, { 0x012, "E" }, { 0x021, "F" }, { 0x022, "G" },
        { 0x023, "H" }, { 0x017, "I" }, { 0x024, "J" }, { 0x025, "K" }, { 0x026, "L" }, { 0x032, "M" }, { 0x031, "N" },
        { 0x018, "O" }, { 0x019, "P" }, { 0x010, "Q" }, { 0x013, "R" }, { 0x01F, "S" }, { 0x014, "T" }, { 0x016, "U" },
        { 0x02F, "V" }, { 0x011, "W" }, { 0x02D, "X" }, { 0x015, "Y" }, { 0x02C, "Z" },
        { 0x028, "APOSTROPHE" }, { 0x02B, "BACKSLASH" }, { 0x033, "COMMA" }, { 0x00D, "EQUAL" }, { 0x029, "GRAVE" },
        { 0x01A, "LEFT_BRACKET" }, { 0x00C, "MINUS" }, { 0x034, "PERIOD" }, { 0x01B, "RIGHT_BRACKET" },
        { 0x027, "SEMICOLON" }, { 0x035, "SLASH" },
        { 0x153, "DELETE" }, { 0x14F, "END" }, { 0x01C, "ENTER" }, { 0x147, "HOME" }, { 0x152, "INSERT" },
        { 0x151, "PAGE_DOWN" }, { 0x149, "PAGE_UP" }, { 0x039, "SPACE" }, { 0x00F, "TAB" }, { 0x03A, "CAPS_LOCK" },
        { 0x03B, "F1" }, { 0x03C, "F2" }, { 0x03D, "F3" }, { 0x03E, "F4" }, { 0x03F, "F5" }, { 0x040, "F6" },
        { 0x041, "F7" }, { 0x042, "F8" }, { 0x043, "F9" }, { 0x044, "F10" }, { 0x057, "F11" },
        { 0x038, "LEFT_ALT" }, { 0x01D, "LEFT_CONTROL" }, { 0x02A, "LEFT_SHIFT" },
        { 0x138, "RIGHT_ALT" }, { 0x11D, "RIGHT_CONTROL" }, { 0x036, "RIGHT_SHIFT" },
        { 0x150, "DOWN" }, { 0x14B, "LEFT" }, { 0x14D, "RIGHT" }, { 0x148, "UP" },
        { 0x052, "KP_0" }, { 0x04F, "KP_1" }, { 0x050, "KP_2" }, { 0x051, "KP_3" }, { 0x04B, "KP_4" },
        { 0x04C, "KP_5" }, { 0x04D, "KP_6" }, { 0x047, "KP_7" }, { 0x048, "KP_8" }, { 0x049, "KP_9" },
        { 0x04E, "KP_ADD" }, { 0x053, "KP_DECIMAL" }, { 0x135, "KP_DIVIDE" }, { 0x11C, "KP_ENTER" },
        { 0x037, "KP_MULTIPLY" }, { 0x04A, "KP_SUBTRACT" }
    };
    static readonly HashSet<string> KeyNames = new HashSet<string>(ByScanCode.Values);

    // The game's name for a key message's scan code (lParam bits 16-24), or null if the game can't use the key.
    public static string KeyFromMessage(IntPtr lParam)
    {
        long l = lParam.ToInt64();
        int scan = (int)((l >> 16) & 0xFF) | (((l >> 24) & 1) != 0 ? 0x100 : 0);
        string name;
        return ByScanCode.TryGetValue(scan, out name) ? name : null;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetKeyNameText(int lParam, StringBuilder text, int size);

    // The label on the player's own keyboard for a stored name, e.g. "A" for Q on an AZERTY layout.
    public static string KeyLabel(string name)
    {
        switch (name)
        {
            case "UP": return "Up arrow";
            case "DOWN": return "Down arrow";
            case "LEFT": return "Left arrow";
            case "RIGHT": return "Right arrow";
            case "LEFT_SHIFT": return "Left Shift";
            case "RIGHT_SHIFT": return "Right Shift";
            case "LEFT_CONTROL": return "Left Ctrl";
            case "RIGHT_CONTROL": return "Right Ctrl";
            case "LEFT_ALT": return "Left Alt";
            case "RIGHT_ALT": return "Right Alt";
        }
        foreach (var pair in ByScanCode)
        {
            if (pair.Value != name) continue;
            var text = new StringBuilder(64);
            int lParam = ((pair.Key & 0xFF) << 16) | ((pair.Key & 0x100) != 0 ? 1 << 24 : 0);
            if (GetKeyNameText(lParam, text, text.Capacity) > 0)
            {
                string label = text.ToString();
                return label.Length == 1 ? label.ToUpperInvariant() : char.ToUpperInvariant(label[0]) + label.Substring(1).ToLowerInvariant();
            }
            break;
        }
        return name.Replace("KP_", "Num ").Replace('_', ' ');
    }

    public static string KeysLabel(int action)
    {
        return Keys[action].Length == 0 ? "-" : string.Join(" / ", Keys[action].Select(KeyLabel));
    }

    // For the Controls page.
    public static string KeyboardSummary()
    {
        var parts = new List<string>();
        int[] shown = { 0, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15 };
        parts.Add("Move: " + string.Join(" ", new[] { 0, 2, 1, 3 }.Select(a => Keys[a].Length > 0 ? KeyLabel(Keys[a].Last()) : "-")));
        foreach (int a in shown.Skip(1))
            parts.Add(Labels[a].Split(' ')[0] + ": " + KeysLabel(a));
        parts.Add("Camera: " + string.Join(" ", new[] { CameraFirst, CameraFirst + 2, CameraFirst + 1, CameraFirst + 3 }
                                                   .Select(a => Keys[a].Length > 0 ? KeyLabel(Keys[a][0]) : "-")) +
                  ", back: " + KeysLabel(13));
        return string.Join("     ", parts);
    }
}

// The "Change controls" dialog: two key boxes and a controller button per action.
class ControlsForm : Form
{
    readonly Button[,] keyBoxes = new Button[Bindings.Actions.Length, Bindings.KeysPerAction];
    readonly ComboBox[] padBoxes = new ComboBox[Bindings.Actions.Length];
    readonly Label hint = new Label();
    readonly string[][] keys;
    readonly string[] pad;
    int capAction = -1, capSlot = -1;

    public ControlsForm()
    {
        Text = "Controls";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        Font = new Font("Segoe UI", 9.5f);
        keys = Bindings.Keys.Select(k => (string[])k.Clone()).ToArray();
        pad = (string[])Bindings.Pad.Clone();

        const int RowH = 27, LabelX = 18, Key1X = 220, KeyW = 128, PadX = 492, PadW = 210;
        int y = 12;
        Controls.Add(new Label
        {
            Text = "Click a key box, then press the key. Esc cancels, Backspace clears the box. Esc, F12 and Alt+Enter stay " +
                   "with the game window. The controller's sticks always work as the PS2 sticks.",
            ForeColor = LauncherForm.Text2, Location = new Point(LabelX, y), Size = new Size(PadX + PadW - LabelX, 36)
        });
        y += 44;
        Controls.Add(new Label { Text = "Keyboard", Font = new Font(Font, FontStyle.Bold), Location = new Point(Key1X, y), AutoSize = true });
        Controls.Add(new Label { Text = "Controller", Font = new Font(Font, FontStyle.Bold), Location = new Point(PadX, y), AutoSize = true });
        y += 24;

        for (int a = 0; a < Bindings.Actions.Length; a++)
        {
            if (a == Bindings.StickFirst)
            {
                y += 6;
                Controls.Add(new Label { Text = "Left stick, pushed fully (for running with keys or buttons)", ForeColor = LauncherForm.Gold,
                                      Location = new Point(LabelX, y), AutoSize = true });
                y += 24;
            }
            if (a == Bindings.CameraFirst)
            {
                y += 6;
                Controls.Add(new Label { Text = "Right stick, pushed fully (turns the free camera; R3 swings it back behind Maximo)",
                                      ForeColor = LauncherForm.Gold, Location = new Point(LabelX, y), AutoSize = true });
                y += 24;
            }
            Controls.Add(new Label { Text = Bindings.Labels[a], Location = new Point(LabelX, y + 4), Size = new Size(Key1X - LabelX - 8, 22) });
            for (int s = 0; s < Bindings.KeysPerAction; s++)
            {
                int action = a, slot = s;
                var b = new Button { Bounds = new Rectangle(Key1X + s * (KeyW + 8), y, KeyW, RowH - 3), TabStop = false };
                b.Click += delegate { StartCapture(action, slot); };
                keyBoxes[a, s] = b;
                Controls.Add(b);
            }
            var c = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Bounds = new Rectangle(PadX, y, PadW, RowH - 3) };
            for (int i = 0; i < Bindings.PadChoices.GetLength(0); i++) c.Items.Add(Bindings.PadChoices[i, 1]);
            int at = a;
            c.SelectedIndex = Bindings.PadIndex(pad[a]);
            c.SelectedIndexChanged += delegate { pad[at] = Bindings.PadChoices[c.SelectedIndex, 0]; };
            padBoxes[a] = c;
            Controls.Add(c);
            y += RowH;
        }

        y += 8;
        hint.Location = new Point(LabelX, y);
        hint.Size = new Size(PadX + PadW - LabelX, 22);
        hint.ForeColor = LauncherForm.Gold;
        Controls.Add(hint);
        y += 28;
        var reset = new Button { Text = "Reset to defaults", Bounds = new Rectangle(LabelX, y, 150, 32) };
        reset.Click += delegate { ResetAll(); };
        var save = new Button { Text = "Save", Bounds = new Rectangle(PadX + PadW - 220, y, 104, 32) };
        save.Click += delegate
        {
            Bindings.Keys = keys;
            Bindings.Pad = pad;
            Bindings.Save();
            DialogResult = DialogResult.OK;
        };
        var cancel = new Button { Text = "Cancel", Bounds = new Rectangle(PadX + PadW - 104, y, 104, 32), DialogResult = DialogResult.Cancel };
        Controls.Add(reset);
        Controls.Add(save);
        Controls.Add(cancel);
        CancelButton = null; // Esc belongs to key capture; the Cancel button and the window's X close it
        ClientSize = new Size(PadX + PadW + 18, y + 44);
        LauncherForm.Theme(this);
        RefreshBoxes();
    }

    void ResetAll()
    {
        string[][] k = Bindings.DefaultKeysCopy();
        string[] p = Bindings.DefaultPadCopy();
        for (int a = 0; a < keys.Length; a++) { keys[a] = k[a]; pad[a] = p[a]; padBoxes[a].SelectedIndex = Bindings.PadIndex(p[a]); }
        EndCapture("Defaults restored. Press Save to keep them.");
    }

    void StartCapture(int action, int slot)
    {
        capAction = action;
        capSlot = slot;
        RefreshBoxes();
        hint.Text = "Press a key for " + Bindings.Labels[action].Trim() + "...";
    }

    void EndCapture(string message)
    {
        capAction = capSlot = -1;
        RefreshBoxes();
        hint.Text = message;
    }

    void RefreshBoxes()
    {
        for (int a = 0; a < keys.Length; a++)
            for (int s = 0; s < Bindings.KeysPerAction; s++)
            {
                bool capturing = a == capAction && s == capSlot;
                keyBoxes[a, s].Text = capturing ? "Press a key..." : s < keys[a].Length ? Bindings.KeyLabel(keys[a][s]) : "";
                keyBoxes[a, s].ForeColor = capturing ? LauncherForm.Gold : LauncherForm.Text1;
            }
    }

    protected override bool ProcessCmdKey(ref Message message, Keys keyData)
    {
        if (capAction < 0)
        {
            if (keyData == System.Windows.Forms.Keys.Escape) { Close(); return true; }
            return base.ProcessCmdKey(ref message, keyData);
        }
        Keys code = keyData & System.Windows.Forms.Keys.KeyCode;
        if (code == System.Windows.Forms.Keys.Escape) { EndCapture(""); return true; }
        var list = keys[capAction].ToList();
        if (code == System.Windows.Forms.Keys.Back)
        {
            if (capSlot < list.Count) list.RemoveAt(capSlot);
            keys[capAction] = list.ToArray();
            EndCapture("Cleared.");
            return true;
        }
        string name = Bindings.KeyFromMessage(message.LParam);
        if (name == null) { hint.Text = "The game can't use that key. Try another, or Esc to cancel."; return true; }

        string moved = "";
        for (int a = 0; a < keys.Length; a++)
            if (a != capAction && keys[a].Contains(name))
            {
                keys[a] = keys[a].Where(n => n != name).ToArray();
                moved = " (taken off " + Bindings.Labels[a].Trim() + ")";
            }
        list.Remove(name);
        if (capSlot < list.Count) list[capSlot] = name;
        else list.Add(name);
        keys[capAction] = list.Take(Bindings.KeysPerAction).ToArray();
        EndCapture(Bindings.KeyLabel(name) + " = " + Bindings.Labels[capAction].Trim() + moved);
        return true;
    }
}
