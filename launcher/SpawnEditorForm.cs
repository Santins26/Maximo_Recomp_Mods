// The randomizer's Spawn editor: what can appear and how often, per category (enemies, bosses, items, structures,
// abilities and skills, chest contents). A window of its own, opened with Show() so the launcher keeps running;
// Save writes .\randomizer_rates.txt, which the game reads when Play is pressed.
// Written for the C# 5 compiler that ships with Windows.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

sealed class SpawnEditorForm : Form
{
    static SpawnEditorForm open;
    readonly List<Randomizer.Rate> rates;
    readonly DataGridView grid = new DataGridView();
    readonly ComboBox group = new ComboBox();
    readonly Label share = new Label();
    public event Action Saved;

    static readonly Color Back = Color.FromArgb(12, 9, 18), Dark = Color.FromArgb(20, 15, 26);

    // One editor at a time: a second click brings the open one forward.
    public static void ShowEditor(IWin32Window owner, Action saved)
    {
        if (open != null && !open.IsDisposed)
        {
            open.Activate();
            return;
        }
        open = new SpawnEditorForm();
        if (saved != null) open.Saved += saved;
        open.Show(owner);
    }

    SpawnEditorForm()
    {
        rates = Randomizer.LoadRates();
        Text = "Randomizer spawn editor";
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(720, 640);
        MinimumSize = new Size(560, 420);
        BackColor = Back;
        ForeColor = LauncherForm.Text1;
        Font = new Font("Segoe UI", 10f);
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { }

        var top = new Panel { Dock = DockStyle.Top, Height = 92, BackColor = Back };
        var help = new Label
        {
            Text = "A weight is how often a pick lands on an entry against the others in its list (0 = never). " +
                   "Enemies, items and structures are each swapped within their own list. \"Bosses as enemies\" and " +
                   "\"Boss arenas\" are used with their switches on. Changes are used from the next Play.",
            ForeColor = LauncherForm.Text2, Location = new Point(12, 8), Size = new Size(690, 40),
            Anchor = AnchorStyles.Left | AnchorStyles.Top | AnchorStyles.Right
        };
        top.Controls.Add(help);
        group.DropDownStyle = ComboBoxStyle.DropDownList;
        group.Location = new Point(12, 56);
        group.Width = 260;
        group.BackColor = Dark;
        group.ForeColor = LauncherForm.Text1;
        group.FlatStyle = FlatStyle.Flat;
        var seen = new List<string>();
        foreach (Randomizer.Rate r in rates)
            if (!seen.Contains(r.Group)) { seen.Add(r.Group); group.Items.Add(r.Group); }
        group.SelectedIndexChanged += delegate { Fill(); };
        top.Controls.Add(group);
        share.ForeColor = LauncherForm.Text2;
        share.Location = new Point(290, 59);
        share.AutoSize = true;
        top.Controls.Add(share);

        grid.Dock = DockStyle.Fill;
        grid.AllowUserToAddRows = false;
        grid.AllowUserToDeleteRows = false;
        grid.AllowUserToResizeRows = false;
        grid.RowHeadersVisible = false;
        grid.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
        grid.BackgroundColor = Back;
        grid.BorderStyle = BorderStyle.None;
        grid.EnableHeadersVisualStyles = false;
        grid.ColumnHeadersDefaultCellStyle.BackColor = Dark;
        grid.ColumnHeadersDefaultCellStyle.ForeColor = LauncherForm.Gold;
        grid.DefaultCellStyle.BackColor = Dark;
        grid.DefaultCellStyle.ForeColor = LauncherForm.Text1;
        grid.DefaultCellStyle.SelectionBackColor = Color.FromArgb(60, 45, 20);
        grid.DefaultCellStyle.SelectionForeColor = LauncherForm.Text1;
        grid.GridColor = Color.FromArgb(50, 40, 60);
        grid.Columns.Add(new DataGridViewCheckBoxColumn { HeaderText = "On", Width = 44 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { HeaderText = "What", ReadOnly = true, AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill });
        grid.Columns.Add(new DataGridViewTextBoxColumn { HeaderText = "Weight", Width = 80 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { HeaderText = "Share", ReadOnly = true, Width = 80 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { HeaderText = "Default", ReadOnly = true, Width = 80 });
        grid.CellValueChanged += GridChanged;
        grid.CurrentCellDirtyStateChanged += delegate
        {
            if (grid.IsCurrentCellDirty && grid.CurrentCell is DataGridViewCheckBoxCell)
                grid.CommitEdit(DataGridViewDataErrorContexts.Commit);
        };
        grid.DataError += delegate(object s, DataGridViewDataErrorEventArgs e) { e.ThrowException = false; };

        var bottom = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 52, FlowDirection = FlowDirection.RightToLeft, BackColor = Back, Padding = new Padding(8) };
        bottom.Controls.Add(MakeButton("Close", delegate { Close(); }));
        bottom.Controls.Add(MakeButton("Save", delegate
        {
            try
            {
                Randomizer.SaveRates(rates);
                if (Saved != null) Saved();
                share.Text = "Saved.";
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "Could not save the spawn rates: " + ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }));
        bottom.Controls.Add(MakeButton("This list's defaults", delegate
        {
            foreach (Randomizer.Rate r in rates) if (r.Group == (string)group.SelectedItem) r.Weight = r.Default;
            Fill();
        }));
        bottom.Controls.Add(MakeButton("Even weights", delegate
        {
            foreach (Randomizer.Rate r in rates) if (r.Group == (string)group.SelectedItem && r.Weight > 0) r.Weight = 10;
            Fill();
        }));

        Controls.Add(grid);
        Controls.Add(bottom);
        Controls.Add(top);
        group.SelectedIndex = 0;
    }

    Button MakeButton(string text, EventHandler click)
    {
        var b = new Button { Text = text, AutoSize = true, FlatStyle = FlatStyle.Flat, BackColor = Dark, ForeColor = LauncherForm.Text1, Height = 32, Margin = new Padding(6, 0, 0, 0) };
        b.FlatAppearance.BorderColor = LauncherForm.Gold;
        b.Click += click;
        return b;
    }

    bool filling;
    readonly List<Randomizer.Rate> shown = new List<Randomizer.Rate>();

    void Fill()
    {
        filling = true;
        grid.Rows.Clear();
        shown.Clear();
        foreach (Randomizer.Rate r in rates)
        {
            if (r.Group != (string)group.SelectedItem) continue;
            shown.Add(r);
            grid.Rows.Add(r.Weight > 0, r.Name, r.Weight.ToString(), "", r.Default.ToString());
        }
        filling = false;
        Shares();
    }

    void Shares()
    {
        int total = 0;
        foreach (Randomizer.Rate r in shown) total += r.Weight;
        bool was = filling;
        filling = true; // (writing a cell raises CellValueChanged)
        for (int i = 0; i < shown.Count; ++i)
            grid.Rows[i].Cells[3].Value = total > 0 && shown[i].Weight > 0 ? (100.0 * shown[i].Weight / total).ToString("0.0") + "%" : "-";
        filling = was;
        share.Text = total > 0 ? "" : "Nothing in this list can be picked: the originals stay.";
    }

    void GridChanged(object sender, DataGridViewCellEventArgs e)
    {
        if (filling || e.RowIndex < 0 || e.RowIndex >= shown.Count) return;
        Randomizer.Rate r = shown[e.RowIndex];
        filling = true;
        if (e.ColumnIndex == 0)
        {
            bool on = grid.Rows[e.RowIndex].Cells[0].Value is bool && (bool)grid.Rows[e.RowIndex].Cells[0].Value;
            r.Weight = on ? (r.Default > 0 ? r.Default : 5) : 0;
            grid.Rows[e.RowIndex].Cells[2].Value = r.Weight.ToString();
        }
        else if (e.ColumnIndex == 2)
        {
            int w;
            object v = grid.Rows[e.RowIndex].Cells[2].Value;
            if (v != null && int.TryParse(v.ToString().Trim(), out w)) r.Weight = Math.Max(0, Math.Min(1000, w));
            grid.Rows[e.RowIndex].Cells[2].Value = r.Weight.ToString();
            grid.Rows[e.RowIndex].Cells[0].Value = r.Weight > 0;
        }
        filling = false;
        Shares();
    }
}
