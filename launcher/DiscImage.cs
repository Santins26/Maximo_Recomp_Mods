// Reading the player's disc image without Windows: a .bin/.cue (raw 2352-byte sectors, which Windows cannot mount) or
// a plain .iso. The ISO 9660 file system on its data track is copied out file by file. Nothing here knows the game.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

static class DiscImage
{
    const int Sector = 2048;

    public static bool IsImage(string path)
    {
        string ext = Path.GetExtension(path).ToLowerInvariant();
        return File.Exists(path) && (ext == ".bin" || ext == ".cue" || ext == ".img" || ext == ".iso");
    }

    // The data file, its sector size and where the 2048 data bytes sit in each sector.
    static void Layout(string path, out string file, out int sectorSize, out int dataOffset)
    {
        file = path;
        string mode = null;
        if (Path.GetExtension(path).Equals(".cue", StringComparison.OrdinalIgnoreCase))
        {
            string bin = null;
            foreach (string raw in File.ReadAllLines(path))
            {
                string line = raw.Trim(), upper = line.ToUpperInvariant();
                if (bin == null && upper.StartsWith("FILE"))
                {
                    int q1 = line.IndexOf('"'), q2 = line.LastIndexOf('"');
                    if (q1 >= 0 && q2 > q1) bin = line.Substring(q1 + 1, q2 - q1 - 1);
                    else
                    {
                        string[] parts = line.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                        if (parts.Length > 1) bin = parts[1];
                    }
                }
                if (mode == null && upper.StartsWith("TRACK"))
                {
                    if (upper.Contains("MODE1/2352")) mode = "MODE1/2352";
                    else if (upper.Contains("MODE2/2352")) mode = "MODE2/2352";
                    else if (upper.Contains("MODE1/2048")) mode = "MODE1/2048";
                }
            }
            if (bin == null) throw new Exception("The .cue file names no .bin file.");
            file = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(path)), bin);
            if (!File.Exists(file)) throw new Exception("The .cue file's disc data is missing: " + file);
        }
        sectorSize = Sector;
        dataOffset = 0;
        if (mode == "MODE1/2352") { sectorSize = 2352; dataOffset = 16; return; }
        if (mode == "MODE2/2352") { sectorSize = 2352; dataOffset = 24; return; }
        if (mode != null) return;
        // No cue sheet: a raw image starts with the sync pattern (00, ten FF, 00); byte 15 is the sector mode.
        using (var s = File.OpenRead(file))
        {
            var head = new byte[16];
            if (s.Read(head, 0, 16) == 16 && head[0] == 0 && head[11] == 0)
            {
                bool sync = true;
                for (int i = 1; i <= 10; ++i) sync &= head[i] == 0xFF;
                if (sync) { sectorSize = 2352; dataOffset = head[15] == 1 ? 16 : 24; }
            }
        }
    }

    sealed class Reader : IDisposable
    {
        readonly FileStream stream;
        readonly int sectorSize, dataOffset;
        public Reader(string file, int sectorSize, int dataOffset)
        {
            stream = File.OpenRead(file);
            this.sectorSize = sectorSize;
            this.dataOffset = dataOffset;
        }
        public void Dispose() { stream.Dispose(); }

        // size bytes of data from sector on, to a stream.
        public void Copy(long sector, long size, Stream to)
        {
            var buffer = new byte[Sector];
            for (long done = 0; done < size; ++sector)
            {
                int n = (int)Math.Min(Sector, size - done);
                stream.Position = sector * sectorSize + dataOffset;
                int got = 0;
                while (got < n)
                {
                    int r = stream.Read(buffer, got, n - got);
                    if (r <= 0) throw new Exception("The disc image ends early (is the .bin complete?).");
                    got += r;
                }
                to.Write(buffer, 0, n);
                done += n;
            }
        }
        public byte[] Read(long sector, long size)
        {
            using (var m = new MemoryStream()) { Copy(sector, size, m); return m.ToArray(); }
        }
    }

    struct Entry
    {
        public string Path;
        public long Sector, Size;
        public bool Directory;
    }

    static uint Le32(byte[] b, int at) { return (uint)(b[at] | b[at + 1] << 8 | b[at + 2] << 16 | b[at + 3] << 24); }

    // Copies every file on the image's data track into folder; progress gets a line now and then.
    public static int Extract(string image, string folder, Action<string> progress)
    {
        string file;
        int sectorSize, dataOffset;
        Layout(image, out file, out sectorSize, out dataOffset);
        using (var r = new Reader(file, sectorSize, dataOffset))
        {
            byte[] pvd = r.Read(16, Sector);
            if (pvd[0] != 1 || Encoding.ASCII.GetString(pvd, 1, 5) != "CD001")
                throw new Exception("This disc image has no readable file system (it should be a PS2 DVD/CD image).");

            var files = new List<Entry>();
            var pending = new Queue<Entry>();
            pending.Enqueue(new Entry { Path = "", Sector = Le32(pvd, 156 + 2), Size = Le32(pvd, 156 + 10), Directory = true });
            var seen = new HashSet<long>();
            while (pending.Count > 0)
            {
                Entry dir = pending.Dequeue();
                if (!seen.Add(dir.Sector)) continue;
                byte[] d = r.Read(dir.Sector, dir.Size);
                for (int at = 0; at < d.Length;)
                {
                    int length = d[at];
                    if (length == 0) { at = (at / Sector + 1) * Sector; continue; }
                    int nameLength = d[at + 32];
                    bool isDir = (d[at + 25] & 2) != 0;
                    if (nameLength == 1 && (d[at + 33] == 0 || d[at + 33] == 1)) { at += length; continue; } // . and ..
                    string name = Encoding.ASCII.GetString(d, at + 33, nameLength);
                    int semi = name.IndexOf(';');
                    if (semi >= 0) name = name.Substring(0, semi);
                    if (!isDir && name.EndsWith(".")) name = name.TrimEnd('.');
                    var e = new Entry
                    {
                        Path = dir.Path.Length == 0 ? name : dir.Path + Path.DirectorySeparatorChar + name,
                        Sector = Le32(d, at + 2),
                        Size = Le32(d, at + 10),
                        Directory = isDir,
                    };
                    if (isDir) pending.Enqueue(e); else files.Add(e);
                    at += length;
                }
            }

            long total = 0, done = 0;
            foreach (Entry e in files) total += e.Size;
            for (int i = 0; i < files.Count; ++i)
            {
                string target = Path.Combine(folder, files[i].Path);
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                using (var o = File.Create(target))
                    r.Copy(files[i].Sector, files[i].Size, o);
                done += files[i].Size;
                progress("Reading the disc image: " + (100 * done / Math.Max(1, total)) + "% (" + (i + 1) + " of " + files.Count + ")");
            }
            return files.Count;
        }
    }
}
