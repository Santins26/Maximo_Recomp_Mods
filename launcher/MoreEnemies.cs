// Extra enemies (experimental): the launcher writes copies of some of a level's own walking enemies into the level
// files in .\disc before the game starts. This is the same algorithm as tools\more_enemies.py (same checks, same
// random numbers, so both write identical files); see that file for the reasoning.
//
//  - Only the *_SUB*.PSX level files are touched. Boss arenas, hubs, sky and menu files never are.
//  - Copies are appended after the last record, so every existing entity keeps its number; the record count and the
//    records-section size are raised to match. A file whose fields do not match what was parsed is left alone.
//  - No new enemy types: every copy is a type the level already loads.
//  - The first time a level file is changed, the original is saved in .\disc_levels_original. Every later run starts
//    from that original, and choosing "Original" puts the originals back. Setting up the game files again deletes
//    the backup (the new files are the originals).
//
// The game stops running enemies past a per-level weight limit, so the launcher also raises that limit
// (MAXIMO_OBJECT_BUDGET=more) whenever extra enemies are on.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

static class MoreEnemies
{
    // ---- launcher settings ----
    public static readonly string[] Names =
    {
        "Original",
        "+25% enemies (experimental)",
        "+50% enemies (experimental)",
        "+100% enemies (experimental)",
        "Triple enemies (experimental)",
        "Quadruple enemies (experimental)",
        "10x enemies (experimental)",
    };
    public static readonly string[] Keys = { "original", "25", "50", "100", "200", "300", "900" };
    public static readonly double[] Multipliers = { 1.0, 1.25, 1.5, 2.0, 3.0, 4.0, 10.0 };

    // ---- the algorithm ----
    const int NameBufferSize = 0x100;
    const int RecordHeaderSize = 0x200;
    const int EntityHeaderSize = 0x28;
    const int EntrySize = 0x20;
    const int EntityTable = 384;   // the engine's per-level entity flag table
    const int MaxRecords = 380;    // never more records than this in one file, for a game without the fix below
    // maximo_boot.exe keeps the flags of entities past 384 itself (maximo_boot.cpp, "entity flag table past 384")
    // when the object budget is raised; the launcher always raises it with extra enemies. It logs this marker, so
    // the text is in any executable that has the fix.
    const int MaxRecordsExtended = 1000;
    const string ExtendedMarker = "entity flags: extended past 384 entities";
    const double Spread = 0.5;     // copy distance as a fraction of the typical enemy spacing
    const int UpAxis = 1;          // the vertical axis of the position floats (assumed Y)
    const int Seed = 1;
    const string CopySuffix = "_more";

    static readonly Encoding Latin1 = Encoding.GetEncoding("iso-8859-1");
    static readonly Regex LevelFile = new Regex(@"(_SUB\d*|G_INTRO)\.PSX$", RegexOptions.IgnoreCase);
    // Names like Skeleton_12345 look generated; a hand-named instance (gate1_torch) is probably referenced by a script.
    static readonly Regex GeneratedName = new Regex(@"^[A-Za-z][A-Za-z0-9_]*_\d+$");
    static readonly Regex CopyName = new Regex(@"_more\d+x*$");

    // Walking enemies (the randomizer's spawn_config.ENEMY_TYPES). Not bosses, not Monster Generators, not items.
    static readonly HashSet<int> EnemyTypes = new HashSet<int>
    {
        0x47, 0x2F, 0x39, 0x54, 0x49, 0x3A, 0x48, 0x6A, 0x10, 0x19, 0x0D, 0x6B, 0x37, 0x1D, 0x13, 0x27, 0x5D, 0x08,
        0x53, 0x58, 0x16,
    };

    sealed class PsxRecord
    {
        public long Offset;
        public string InstanceName;
        public int TypeId;
        public uint InstanceId;
        public uint PropCount;
        public double[] Pos;
        public byte[] Raw;
        public int Size { get { return Raw.Length; } }
    }

    struct Layout
    {
        public int CountOffset, PtrOffset, RecordsStart;
    }

    sealed class Outcome
    {
        public byte[] Data;          // the rebuilt file, or null when the file should stay as it is
        public string Message;
        public bool AlreadyCopied;   // the file already holds copies from an earlier run
        public int Added;
    }

    // splitmix64 seeded from text by FNV-1a; identical to the Rng class in tools\more_enemies.py.
    sealed class Rng
    {
        ulong state;

        public Rng(string text)
        {
            ulong h = 0xCBF29CE484222325UL;
            foreach (char c in text) h = (h ^ (byte)c) * 0x100000001B3UL;
            state = h;
        }

        public uint NextU32()
        {
            state += 0x9E3779B97F4A7C15UL;
            ulong z = state;
            z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
            z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
            z ^= z >> 31;
            return (uint)(z >> 32);
        }

        public double Unit() { return NextU32() / 4294967296.0; }

        public void Shuffle<T>(List<T> items)
        {
            for (int i = items.Count - 1; i > 0; i--)
            {
                int j = (int)(NextU32() % (uint)(i + 1));
                T t = items[i];
                items[i] = items[j];
                items[j] = t;
            }
        }
    }

    // ---- reading and writing little-endian fields ----
    static uint U32(byte[] d, long o)
    {
        return (uint)d[o] | (uint)d[o + 1] << 8 | (uint)d[o + 2] << 16 | (uint)d[o + 3] << 24;
    }

    static double F32(byte[] d, long o) { return BitConverter.ToSingle(d, (int)o); }

    static void PutU32(byte[] d, long o, uint v)
    {
        d[o] = (byte)v;
        d[o + 1] = (byte)(v >> 8);
        d[o + 2] = (byte)(v >> 16);
        d[o + 3] = (byte)(v >> 24);
    }

    static void PutF32(byte[] d, long o, double v)
    {
        Buffer.BlockCopy(BitConverter.GetBytes((float)v), 0, d, (int)o, 4);
    }

    // ---- PSX parsing (the same layout detection as the randomizer's psx.py) ----
    static string ReadString(byte[] data, long offset, int maxLen)
    {
        long end = offset;
        while (end < offset + maxLen && end < data.Length && data[end] != 0) end++;
        return Latin1.GetString(data, (int)offset, (int)(end - offset)); // one char per byte: names round-trip
    }

    static bool IsClassBuffer(byte[] data, long offset)
    {
        if (offset < 0 || offset >= data.Length) return false;
        if (data[offset] < 0x41 || data[offset] > 0x5A) return false;
        long end = offset;
        while (end < offset + 64 && end < data.Length && data[end] >= 32 && data[end] < 127) end++;
        if (end - offset < 3 || end >= data.Length || data[end] != 0) return false;
        int cd = 0;
        long stop = Math.Min(offset + NameBufferSize, (long)data.Length);
        for (long j = end + 1; j < stop; j++)
        {
            if (data[j] != 0xCD) break;
            cd++;
        }
        return cd >= 200;
    }

    static byte Lower(byte b) { return (b >= 0x41 && b <= 0x5A) ? (byte)(b + 32) : b; }

    static int FindBefPath(byte[] data)
    {
        int limit = Math.Min(0x1200, data.Length - 5);
        for (int off = 0; off < limit; off++)
        {
            if (Lower(data[off]) == (byte)'.' && Lower(data[off + 1]) == (byte)'b' &&
                Lower(data[off + 2]) == (byte)'e' && Lower(data[off + 3]) == (byte)'f')
            {
                int start = off;
                while (start > 0 && start > off - 256)
                {
                    byte c = data[start - 1];
                    if (c == 0 || !(c >= 0x20 && c < 127)) break;
                    start--;
                }
                return start;
            }
        }
        return 0;
    }

    static Layout DetectLayout(byte[] data)
    {
        Layout l;
        int bef = FindBefPath(data);
        if (bef == 0)
        {
            l.CountOffset = 0x530;
            l.PtrOffset = 0x534;
            l.RecordsStart = 0x570;
            return l;
        }
        l.CountOffset = bef + 0x100;
        l.PtrOffset = bef + 0x104;
        l.RecordsStart = bef + 0x140;
        if (!IsClassBuffer(data, l.RecordsStart))
        {
            int[] deltas = { 1, -1, 2, -2, 3, -3, 4, -4 };
            foreach (int delta in deltas)
            {
                if (IsClassBuffer(data, l.RecordsStart + delta))
                {
                    l.CountOffset += delta;
                    l.PtrOffset += delta;
                    l.RecordsStart += delta;
                    break;
                }
            }
        }
        return l;
    }

    static List<PsxRecord> ParseRecords(byte[] data, int recordsStart)
    {
        var records = new List<PsxRecord>();
        long i = recordsStart;
        while (i < (long)data.Length - RecordHeaderSize - EntityHeaderSize)
        {
            if (!IsClassBuffer(data, i))
            {
                i += 4;
                continue;
            }
            long prop = i + RecordHeaderSize;
            var r = new PsxRecord();
            r.Offset = i;
            r.InstanceName = ReadString(data, i + NameBufferSize, 256);
            r.TypeId = data[prop + 0x04];
            r.InstanceId = U32(data, prop + 0x08);
            r.PropCount = U32(data, prop + 0x0C);
            r.Pos = new double[] { F32(data, prop + 0x10), F32(data, prop + 0x14), F32(data, prop + 0x18) };
            long size = RecordHeaderSize + EntityHeaderSize + (long)r.PropCount * EntrySize;
            if (!IsClassBuffer(data, i + size))
            {
                long testI = i + Math.Max(size, 0x100L);
                long found = -1;
                long limit = Math.Min((long)data.Length - 0x100, i + 0x10000L);
                while (testI < limit)
                {
                    if (IsClassBuffer(data, testI))
                    {
                        found = testI;
                        break;
                    }
                    testI += 4;
                }
                if (found >= 0) size = found - i;
            }
            long end = Math.Min(i + size, (long)data.Length);
            r.Raw = new byte[end - i];
            Buffer.BlockCopy(data, (int)i, r.Raw, 0, (int)(end - i));
            records.Add(r);
            i += size;
        }
        return records;
    }

    // ---- cloning ----
    static double Dist(double[] a, double[] b)
    {
        double dx = a[0] - b[0], dy = a[1] - b[1], dz = a[2] - b[2];
        return Math.Sqrt(dx * dx + dy * dy + dz * dz);
    }

    static double NearestDistance(double[] pos, List<double[]> others)
    {
        double best = double.PositiveInfinity;
        foreach (double[] q in others)
        {
            double d = Dist(pos, q);
            if (0 < d && d < best) best = d;
        }
        return best;
    }

    static byte[] Clone(PsxRecord src, uint newId, string newName, double[] newPos)
    {
        byte[] raw = (byte[])src.Raw.Clone();
        PutU32(raw, RecordHeaderSize + 0x08, newId);
        PutF32(raw, RecordHeaderSize + 0x10, newPos[0]);
        PutF32(raw, RecordHeaderSize + 0x14, newPos[1]);
        PutF32(raw, RecordHeaderSize + 0x18, newPos[2]);
        // instance name: new text, then the original padding is left as it was
        byte[] name = Latin1.GetBytes(newName);
        int nameLen = Math.Min(name.Length, NameBufferSize - 1);
        Buffer.BlockCopy(name, 0, raw, NameBufferSize, nameLen);
        raw[NameBufferSize + nameLen] = 0;
        // trigger volumes (property type 0x0C) are in world coordinates: move them with the copy
        double[] delta = { newPos[0] - src.Pos[0], newPos[1] - src.Pos[1], newPos[2] - src.Pos[2] };
        for (long k = 0; k < src.PropCount; k++)
        {
            long eo = RecordHeaderSize + EntityHeaderSize + k * EntrySize;
            if (eo + EntrySize > raw.Length) break;
            if (raw[eo] == 0x0C)
                for (int a = 0; a < 3; a++)
                    PutF32(raw, eo + 8 + 4 * a, F32(raw, eo + 8 + 4 * a) + delta[a]);
        }
        return raw;
    }

    static bool SameBytes(byte[] a, byte[] b, int bStart, int length)
    {
        if (a.Length != length) return false;
        for (int i = 0; i < length; i++)
            if (a[i] != b[bStart + i]) return false;
        return true;
    }

    // Returns the rebuilt file in Data, or null Data (with a reason) when nothing should change.
    static Outcome Build(byte[] data, double multiplier, int maxRecords, double spread, int upAxis, bool anyName, Rng rng)
    {
        var o = new Outcome();
        Layout layout = DetectLayout(data);
        List<PsxRecord> records = ParseRecords(data, layout.RecordsStart);
        if (records.Count == 0) { o.Message = "no records parsed"; return o; }
        if (layout.CountOffset < 0 || layout.CountOffset + 4 > data.Length ||
            layout.PtrOffset < 0 || layout.PtrOffset + 4 > data.Length)
        { o.Message = "header fields out of range; skipped"; return o; }
        uint countField = U32(data, layout.CountOffset);
        uint sizeField = U32(data, layout.PtrOffset);
        PsxRecord last = records[records.Count - 1];
        long sectionEnd = last.Offset + last.Size;
        long sum = 0;
        foreach (PsxRecord r in records) sum += r.Size;
        if (countField != (uint)records.Count)
        { o.Message = "count field " + countField + " != " + records.Count + " parsed records; skipped"; return o; }
        if (sizeField != sum || sectionEnd - records[0].Offset != sizeField)
        { o.Message = "records-section size field does not match the parsed records; skipped"; return o; }

        foreach (PsxRecord r in records)
            if (CopyName.IsMatch(r.InstanceName))
            { o.Message = "already has copies from an earlier run; skipped"; o.AlreadyCopied = true; return o; }
        var enemies = new List<PsxRecord>();
        foreach (PsxRecord r in records)
            if (EnemyTypes.Contains(r.TypeId)) enemies.Add(r);
        if (enemies.Count == 0) { o.Message = "no walking enemies"; return o; }
        var candidates = new List<PsxRecord>();
        foreach (PsxRecord r in enemies)
            if (anyName || GeneratedName.IsMatch(r.InstanceName)) candidates.Add(r);
        int want = (int)Math.Round(enemies.Count * (multiplier - 1.0));
        int room = maxRecords - records.Count;
        // Up to 9 copies per original (1 + 9 = 10x, the highest multiplier offered).
        int extra = Math.Max(0, Math.Min(want, Math.Min(room, candidates.Count * 9)));
        if (extra == 0)
        {
            o.Message = "nothing to add (" + enemies.Count + " enemies, " + candidates.Count + " cloneable, room for " +
                        Math.Max(room, 0) + ")";
            return o;
        }

        // typical spacing between enemies decides how far a copy stands from its original
        var positions = new List<double[]>();
        foreach (PsxRecord r in enemies) positions.Add(r.Pos);
        var spacings = new List<double>();
        foreach (double[] p in positions)
        {
            double d = NearestDistance(p, positions);
            if (!double.IsPositiveInfinity(d)) spacings.Add(d);
        }
        spacings.Sort();
        double medianGap = spacings.Count > 0 ? spacings[spacings.Count / 2] : 1.0;
        double radius = Math.Max(medianGap * spread, 1e-3);
        var horiz = new List<int>();
        for (int a = 0; a < 3; a++) if (a != upAxis) horiz.Add(a);

        uint maxId = 0;
        foreach (PsxRecord r in records) if (r.InstanceId > maxId) maxId = r.InstanceId;
        long nextId = (long)maxId + 1;
        if (nextId + extra > 0xFFFFFFFFL)
        { o.Message = "instance ids are too close to the 32-bit limit; skipped"; return o; }
        var usedNames = new HashSet<string>();
        foreach (PsxRecord r in records) usedNames.Add(r.InstanceName);
        var picks = new List<PsxRecord>();
        for (int i = 0; i < extra; i++) picks.Add(candidates[i % candidates.Count]);
        rng.Shuffle(picks);

        var added = new List<byte>();
        for (int n = 0; n < picks.Count; n++)
        {
            PsxRecord src = picks[n];
            double angle = rng.Unit() * (2.0 * Math.PI);
            double[] pos = { src.Pos[0], src.Pos[1], src.Pos[2] };
            pos[horiz[0]] += radius * Math.Cos(angle);
            pos[horiz[1]] += radius * Math.Sin(angle);
            string name = src.InstanceName + CopySuffix + (n + 1);
            while (usedNames.Contains(name)) name += "x";
            usedNames.Add(name);
            added.AddRange(Clone(src, (uint)(nextId + n), name, pos));
        }

        byte[] addedBytes = added.ToArray();
        byte[] result = new byte[data.Length + addedBytes.Length];
        Buffer.BlockCopy(data, 0, result, 0, (int)sectionEnd);
        Buffer.BlockCopy(addedBytes, 0, result, (int)sectionEnd, addedBytes.Length);
        Buffer.BlockCopy(data, (int)sectionEnd, result, (int)sectionEnd + addedBytes.Length, data.Length - (int)sectionEnd);
        PutU32(result, layout.CountOffset, (uint)(records.Count + extra));
        PutU32(result, layout.PtrOffset, (uint)(sizeField + addedBytes.Length));

        // verify by parsing what was just built
        List<PsxRecord> again = ParseRecords(result, layout.RecordsStart);
        bool ok = again.Count == records.Count + extra;
        for (int i = 0; ok && i < records.Count; i++)
            ok = SameBytes(records[i].Raw, again[i].Raw, 0, again[i].Raw.Length);
        if (ok)
        {
            var ids = new HashSet<uint>();
            var oldIds = new HashSet<uint>();
            long againSum = 0;
            foreach (PsxRecord r in again) { ids.Add(r.InstanceId); againSum += r.Size; }
            foreach (PsxRecord r in records) oldIds.Add(r.InstanceId);
            ok = ids.Count == oldIds.Count + extra && U32(result, layout.PtrOffset) == againSum;
        }
        if (ok)
        {
            int tail = data.Length - (int)sectionEnd;
            for (int i = 0; ok && i < tail; i++)
                ok = result[(int)sectionEnd + addedBytes.Length + i] == data[(int)sectionEnd + i];
        }
        if (!ok) { o.Message = "self-check failed on the rebuilt file; skipped"; return o; }

        o.Data = result;
        o.Added = extra;
        o.Message = enemies.Count + " enemies -> +" + extra + " copies (records " + records.Count + " -> " +
                    (records.Count + extra) + ", +" + addedBytes.Length + " bytes, spacing " +
                    medianGap.ToString("0.0", CultureInfo.InvariantCulture) + ", radius " +
                    radius.ToString("0.0", CultureInfo.InvariantCulture) + ")";
        return o;
    }

    // The tool's entry point with every setting spelled out (used by the test harness).
    public static byte[] Transform(byte[] data, string fileName, double multiplier, int maxRecords, double spread,
                                   int upAxis, bool anyName, int seed, out string message)
    {
        Outcome o = Build(data, multiplier, maxRecords, spread, upAxis, anyName,
                          new Rng(seed.ToString(CultureInfo.InvariantCulture) + ":" + fileName.ToUpperInvariant()));
        message = o.Message;
        return o.Data;
    }

    // ---- the launcher side: saves the originals and writes the changed level files into .\disc ----
    static readonly string BackupRoot = Path.Combine(Paths.Root, "disc_levels_original");

    // The new files are the originals now (Set up from ISO / folder replaced .\disc).
    public static void ForgetBackup()
    {
        try { if (Directory.Exists(BackupRoot)) Directory.Delete(BackupRoot, true); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    static void WriteFile(string path, byte[] bytes)
    {
        string temp = path + ".tmp";
        File.WriteAllBytes(temp, bytes);
        if (File.Exists(path)) File.SetAttributes(path, FileAttributes.Normal);
        File.Copy(temp, path, true);
        File.Delete(temp);
    }

    static bool FileHolds(string path, byte[] bytes)
    {
        if (!File.Exists(path) || new FileInfo(path).Length != bytes.Length) return false;
        return SameBytes(File.ReadAllBytes(path), bytes, 0, bytes.Length);
    }

    static bool GameHasExtendedTable()
    {
        try
        {
            byte[] exe = File.ReadAllBytes(Paths.Game);
            byte[] marker = Encoding.ASCII.GetBytes(ExtendedMarker);
            for (int i = 0; i + marker.Length <= exe.Length; i++)
            {
                if (exe[i] != marker[0]) continue;
                int j = 1;
                while (j < marker.Length && exe[i + j] == marker[j]) j++;
                if (j == marker.Length) return true;
            }
        }
        catch (Exception e)
        {
            Log.Write("enemies: could not read " + Paths.Game + ": " + e.Message);
        }
        return false;
    }

    // multiplier 1.0 puts the original level files back. Returns one line for the launcher's log ("" = nothing to do).
    public static string Prepare(double multiplier)
    {
        string root = Path.Combine(Paths.Disc, "PSXDATA");
        if (!Directory.Exists(root)) return "";
        int maxRecords = multiplier > 1.0 && GameHasExtendedTable() ? MaxRecordsExtended : MaxRecords;
        if (multiplier > 1.0)
            Log.Write(maxRecords == MaxRecords
                          ? "enemies: this maximo_boot.exe has the original 384-entity table; levels stop at " + MaxRecords + " entities"
                          : "enemies: maximo_boot.exe keeps entities past 384; levels may grow to " + maxRecords + " entities");
        string[] files = Directory.GetFiles(root, "*.PSX", SearchOption.AllDirectories);
        Array.Sort(files, StringComparer.OrdinalIgnoreCase);
        int changed = 0, kept = 0, restored = 0, copies = 0;
        foreach (string file in files)
        {
            string name = Path.GetFileName(file);
            if (!LevelFile.IsMatch(name)) continue;
            string backup = Path.Combine(BackupRoot, file.Substring(Paths.Disc.Length).TrimStart('\\', '/'));
            bool haveBackup = File.Exists(backup);
            if (multiplier <= 1.0)
            {
                if (haveBackup && !FileHolds(file, File.ReadAllBytes(backup)))
                {
                    WriteFile(file, File.ReadAllBytes(backup));
                    restored++;
                }
                continue;
            }
            byte[] source = File.ReadAllBytes(haveBackup ? backup : file);
            // Instance names are designer area labels shared by coins, torches and enemies alike (gate1 is a skeleton, a
            // grave and a chest in G_INTRO), not script references, so every walking enemy can be copied.
            Outcome o = Build(source, multiplier, maxRecords, Spread, UpAxis, true,
                              new Rng(Seed.ToString(CultureInfo.InvariantCulture) + ":" + name.ToUpperInvariant()));
            Log.Write("enemies: " + name + ": " + o.Message);
            if (o.AlreadyCopied && !haveBackup) { kept++; continue; } // not ours: leave it, and do not back it up
            if (o.Data == null)
            {
                // nothing to add: the disc file must be the original
                if (haveBackup && !FileHolds(file, source)) WriteFile(file, source);
                kept++;
                continue;
            }
            if (!haveBackup)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(backup));
                WriteFile(backup, source); // the original is saved before anything is changed
            }
            if (!FileHolds(file, o.Data)) WriteFile(file, o.Data);
            changed++;
            copies += o.Added;
        }
        if (multiplier <= 1.0)
            return restored > 0 ? "enemies: original level files restored (" + restored + ")" : "";
        return "enemies +" + Math.Round((multiplier - 1.0) * 100) + "%: " + changed + " level file(s) changed (" + copies +
               " copies), " + kept + " left as they were";
    }
}
