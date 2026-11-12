// The Randomizer page: the game randomizes itself while it runs (maximo_boot's randomizer, built on the debug menu's
// spawn tool): when a level has loaded, its enemies, items and structures are swapped for picks from the spawn rates,
// ability pickups and wooden chests get new contents, and boss arenas get a second boss. The disc files are not
// changed (the cross-world tool only adds every enemy and structure to every world, so anything can be picked
// anywhere). The same seed and switches always give the same levels.
// While it is on, the cross-world enemies and structures, Expanded memory, the raised object budget and the larger
// sound RAM are always on with it. The game keeps the randomizer's gate and progression safety (keys, gates, level
// columns, the level-finisher groups and the hub's phase groups are never touched).
// The spawn rates are .\randomizer_rates.txt ("category id weight" lines), written by the Spawn editor
// (SpawnEditorForm.cs); without it the game's balanced rates are used.
// Written for the C# 5 compiler that ships with Windows.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;

static class Randomizer
{
    public static bool Enabled = false;
    public static int Seed = 0;            // 0: a new one is drawn the first time Play is pressed
    public static bool Enemies = true, Structures = true, Items = true, Skills = true, Chests = true;
    public static bool BossesAsEnemies = false, BossArenas = false;

    public static readonly string RatesFile = Path.Combine(Paths.Root, "randomizer_rates.txt");
    // Left by the launcher's earlier randomizer, which rewrote the disc files: put back once, then forgotten.
    static readonly string OldBackupRoot = Path.Combine(Paths.Root, "disc_randomizer_original");
    public static Action<string> OnProgress;

    // Object budget while randomizing (per type; the original is 100 and 1000 is the port's ceiling).
    public const int ObjectBudget = 400;

    public static void Load(string key, string value)
    {
        int n;
        switch (key)
        {
            case "rnd": Enabled = value == "1"; break;
            case "rndseed": if (int.TryParse(value, out n)) Seed = Math.Max(0, n); break;
            case "rndenemies": Enemies = value == "1"; break;
            case "rndstructures": Structures = value == "1"; break;
            case "rnditems": Items = value == "1"; break;
            case "rndskills": Skills = value == "1"; break;
            case "rndchests": Chests = value == "1"; break;
            case "rndbossenemies": BossesAsEnemies = value == "1"; break;
            case "rndbossarenas": BossArenas = value == "1"; break;
        }
    }

    public static string SaveLines()
    {
        return "rnd=" + (Enabled ? 1 : 0) + "\r\nrndseed=" + Seed + "\r\nrndenemies=" + (Enemies ? 1 : 0) +
               "\r\nrndstructures=" + (Structures ? 1 : 0) + "\r\nrnditems=" + (Items ? 1 : 0) +
               "\r\nrndskills=" + (Skills ? 1 : 0) + "\r\nrndchests=" + (Chests ? 1 : 0) +
               "\r\nrndbossenemies=" + (BossesAsEnemies ? 1 : 0) + "\r\nrndbossarenas=" + (BossArenas ? 1 : 0) + "\r\n";
    }

    public static string Describe()
    {
        if (!Enabled) return "";
        return ", randomizer (seed " + Seed + (Enemies ? ", enemies" : "") + (Structures ? ", structures" : "") +
               (Items ? ", items" : "") + (Skills ? ", skills" : "") + (Chests ? ", chests" : "") +
               (BossesAsEnemies ? ", bosses as enemies" : "") + (BossArenas ? ", boss arenas" : "") + ")";
    }

    public static int NewSeed()
    {
        var bytes = new byte[4];
        using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(bytes);
        return 1 + (int)(BitConverter.ToUInt32(bytes, 0) % 999999u);
    }

    public static string Options()
    {
        var o = new List<string>();
        if (Enemies) o.Add("enemies");
        if (Structures) o.Add("structures");
        if (Items) o.Add("items");
        if (Skills) o.Add("skills");
        if (Chests) o.Add("chests");
        if (BossesAsEnemies && Enemies) o.Add("bossenemies");
        if (BossArenas) o.Add("bossarenas");
        return string.Join(",", o.ToArray());
    }

    public static bool CustomRates { get { return File.Exists(RatesFile); } }
    public static bool Applied { get { return Directory.Exists(OldBackupRoot); } }
    public static void ForgetBackup()
    {
        try { if (Directory.Exists(OldBackupRoot)) Directory.Delete(OldBackupRoot, true); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    public static void ResetSpawnRates()
    {
        if (File.Exists(RatesFile)) File.Delete(RatesFile); // the game's balanced rates again
    }

    // Before the game starts. Only the earlier launcher's disc changes are undone here (once); the randomizing itself
    // is the game's. One line for the launcher's log.
    public static string Prepare()
    {
        string note = "";
        if (Directory.Exists(OldBackupRoot) && Directory.Exists(Path.Combine(Paths.Disc, "PSXDATA")))
        {
            int n = 0;
            foreach (string src in Directory.GetFiles(OldBackupRoot, "*", SearchOption.AllDirectories))
            {
                string rel = src.Substring(OldBackupRoot.Length).TrimStart('\\', '/');
                if (rel.IndexOfAny(new[] { '\\', '/' }) < 0) continue; // the old stamp and lock
                string dst = Path.Combine(Paths.Disc, rel);
                Directory.CreateDirectory(Path.GetDirectoryName(dst));
                if (File.Exists(dst)) File.SetAttributes(dst, FileAttributes.Normal);
                File.Copy(src, dst, true);
                n++;
            }
            ForgetBackup();
            note = "randomizer: the earlier randomizer's disc changes undone (" + n + " files); ";
        }
        if (!Enabled) return note.TrimEnd(' ', ';');
        if (Seed == 0) { Seed = NewSeed(); Picture.Save(); }
        return note + "randomizer: seed " + Seed + " (" + Options() + "), " + (CustomRates ? "your spawn rates" : "balanced spawn rates");
    }

    public static void Apply(ProcessStartInfo info)
    {
        if (!Enabled) return;
        info.EnvironmentVariables["MAXIMO_RANDOMIZER"] = "1";
        info.EnvironmentVariables["MAXIMO_RND_SEED"] = Seed.ToString(System.Globalization.CultureInfo.InvariantCulture);
        info.EnvironmentVariables["MAXIMO_RND_OPTIONS"] = Options();
        if (CustomRates) info.EnvironmentVariables["MAXIMO_RND_RATES"] = RatesFile;
    }

    // ---- the spawn rates (the game's defaults, maximo_boot.cpp randomizerDefaults: keep the two the same) ----
    public sealed class Rate
    {
        public string Category, Id, Name, Group;
        public int Weight, Default;
    }

    static Rate R(string cat, string id, string name, string group, int w)
    {
        return new Rate { Category = cat, Id = id, Name = name, Group = group, Weight = w, Default = w };
    }

    public static List<Rate> DefaultRates()
    {
        var l = new List<Rate>();
        // (the axe guard, which cannot be killed, and the raven are never put in a level-finisher, key or hub phase group)
        string[][] enemies = {
            new[] { "0x47", "Basic Skeleton", "15" }, new[] { "0x2F", "Axe Skeleton", "15" },
            new[] { "0x39", "Sword Skeleton", "15" }, new[] { "0x54", "Guard Skeleton", "6" },
            new[] { "0x49", "Bomb Skeleton", "15" }, new[] { "0x3A", "Pirate Skeleton", "15" },
            new[] { "0x48", "Basic Zombie", "15" }, new[] { "0x6A", "Torso Zombie", "6" },
            new[] { "0x10", "Frozen Zombie", "15" }, new[] { "0x19", "Swamp Zombie", "15" },
            new[] { "0x0D", "Zombie Crocodile", "15" }, new[] { "0x6B", "Ghost", "9" },
            new[] { "0x37", "Raven", "6" },
            new[] { "0x1D", "Snowman", "15" },
            new[] { "0x13", "Goat Devil", "15" }, new[] { "0x27", "Hammer Devil", "15" },
            new[] { "0x5D", "Doomed Soul", "15" }, new[] { "0x08", "Plant Monster", "15" },
            new[] { "0x53", "Dark Knight", "15" }, new[] { "0x58", "Crazed Prisoner", "15" },
            new[] { "0x16", "Axe Guard (cannot be killed)", "10" },
        };
        foreach (string[] e in enemies) l.Add(R("enemy", e[0], e[1], "Enemies", int.Parse(e[2])));
        string[][] bosses = {
            new[] { "0x29", "GraveDigger", "2" }, new[] { "0x2E", "Bokor La Bas", "2" },
            new[] { "0x82", "Captain Cadaver", "2" }, new[] { "0x8E", "Lord Glutterscum", "1" },
            new[] { "0x9C", "King Achille", "2" }, new[] { "0x9D", "Queen Sophia (Achille's queen)", "1" },
        };
        foreach (string[] b in bosses) l.Add(R("boss", b[0], b[1], "Bosses as enemies", int.Parse(b[2])));
        foreach (string[] b in bosses) l.Add(R("arena", b[0], b[1], "Boss arenas (who replaces the boss)", 10));
        l.Add(R("item", "0x21", "Gold coin", "Items", 45));
        l.Add(R("item", "0x46", "Bag of gold", "Items", 12));
        l.Add(R("item", "0x52", "Gem", "Items", 9));
        l.Add(R("item", "0x51", "Full health", "Items", 8));
        l.Add(R("item", "0x45", "Armor power-up (2 a level at most)", "Items", 4));
        l.Add(R("item", "0x4E", "Extra life (2 a level at most)", "Items", 2));
        l.Add(R("item", "0x20", "Ability pickup (3 a level at most)", "Items", 5));
        l.Add(R("item", "0x25", "Wooden chest (4 extra a level at most)", "Items", 6));
        // every structure of the game (maximo_boot kStructures); the ones in the way, the monster generator and the
        // scripted props are rarer
        string[][] structures = {
            new[] { "0x01", "Bone tower", "4" }, new[] { "0x02", "Bonus grave", "8" },
            new[] { "0x0A", "Popup headstone", "3" }, new[] { "0x17", "Smashable snow pirate", "8" },
            new[] { "0x18", "Ice torch", "8" }, new[] { "0x1B", "Popup ice", "3" },
            new[] { "0x30", "Grave smashable glass", "8" }, new[] { "0x4A", "Globe", "4" },
            new[] { "0x50", "Standing coffin", "8" }, new[] { "0x5F", "Monster generator", "2" },
            new[] { "0x6C", "Coin container", "10" }, new[] { "0x7C", "Ice plant", "8" },
            new[] { "0xA0", "Coffin lid", "8" }, new[] { "0xAA", "Smashable door", "3" },
            new[] { "0xAD", "Spirit torch", "8" }, new[] { "0xAE", "Underworld lantern", "5" },
            new[] { "0xB0", "Smashable torch", "8" }, new[] { "0xB1", "Smashable ice wall", "3" },
            new[] { "0xB5", "Swamp coffin lid", "5" }, new[] { "0xBB", "Thorn vine", "3" },
            new[] { "0xBC", "Smashable coffin", "8" }, new[] { "0xC8", "Spirit statue", "5" },
            new[] { "0xC9", "Breakable rockwall", "3" }, new[] { "0xD3", "Breakable tiki torch", "5" },
        };
        foreach (string[] s in structures) l.Add(R("structure", s[0], s[1], "Structures", int.Parse(s[2])));
        string[] skills = { "Increase sword power", "Sword 720 spin", "Double slash", "Mighty blow", "Magic bolt",
                            "Masquerade", "Return shield", "Wide shockwave", "Damage shockwave", "Find treasure",
                            "Smart bomb", "Hover shield", "Shield", "Shield stamina +5", "Increase armor" };
        for (int i = 0; i < skills.Length; ++i) l.Add(R("skill", i.ToString(), skills[i], "Abilities and skills", 5));
        string[] swords = { "Fire sword", "Ice sword", "Sun sword", "Armageddon sword" };
        for (int i = 0; i < swords.Length; ++i) l.Add(R("skill", (15 + i).ToString(), swords[i], "Abilities and skills", 3));
        string[] more = { "Wind shield", "Magnetic shield", "Lightning shield", "Full health (heart)", "Increase throw", "Sword stamina +5" };
        for (int i = 0; i < more.Length; ++i) l.Add(R("skill", (19 + i).ToString(), more[i], "Abilities and skills", i == 3 ? 3 : 5));
        l.Add(R("chest", "gold", "Gold only", "Chest contents", 30));
        l.Add(R("chest", "health", "Full health", "Chest contents", 15));
        l.Add(R("chest", "life", "Extra life", "Chest contents", 5));
        l.Add(R("chest", "armor", "Armor", "Chest contents", 10));
        l.Add(R("chest", "fire", "Fire sword", "Chest contents", 7));
        l.Add(R("chest", "ice", "Ice sword", "Chest contents", 7));
        l.Add(R("chest", "sun", "Sun sword", "Chest contents", 7));
        l.Add(R("chest", "armageddon", "Armageddon sword", "Chest contents", 7));
        l.Add(R("chest", "mimic", "Mimic (the chest bites)", "Chest contents", 12));
        return l;
    }

    // The rates in use: the defaults with the file's weights over them.
    public static List<Rate> LoadRates()
    {
        List<Rate> l = DefaultRates();
        if (!File.Exists(RatesFile)) return l;
        foreach (string raw in File.ReadAllLines(RatesFile))
        {
            string line = raw;
            int hash = line.IndexOf('#');
            if (hash >= 0) line = line.Substring(0, hash);
            string[] parts = line.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
            int w;
            if (parts.Length != 3 || !int.TryParse(parts[2], out w)) continue;
            foreach (Rate r in l)
                if (r.Category == parts[0] && string.Equals(r.Id, parts[1], StringComparison.OrdinalIgnoreCase))
                    r.Weight = Math.Max(0, Math.Min(1000, w));
        }
        return l;
    }

    public static void SaveRates(List<Rate> rates)
    {
        var sb = new StringBuilder();
        sb.Append("# Maximo randomizer spawn rates (written by the launcher's Spawn editor): category id weight\r\n");
        sb.Append("# A weight is how often a pick lands on that entry, against the others of its category; 0 = never.\r\n");
        foreach (Rate r in rates)
            sb.Append(r.Category).Append(' ').Append(r.Id).Append(' ').Append(r.Weight).Append("   # ").Append(r.Name).Append("\r\n");
        File.WriteAllText(RatesFile, sb.ToString());
    }
}
