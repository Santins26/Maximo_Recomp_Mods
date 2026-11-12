// maximo_kit: turns the player's own disc into the inputs of the build. Nothing it writes may ever enter git.
//
//   maximo_kit prepare <disc .iso / .bin / .cue or extracted folder> <repository root> <generated folder>
//
// It opens the disc read-only, checks the game executable against the three releases it knows (PAL SLES-50703,
// USA SLUS-20017, Japan SLPM-62127), and writes:
//   <generated>/<executable>         the executable, for the recompiler (SLES_507.03, SLUS_200.17 or SLPM_621.27)
//   <generated>/region.txt           PAL, USA or JPN: which release it is, for build.cmd
//   <generated>/recomp.toml          the recompiler's settings (data/funcs.csv, funcs_usa.csv or funcs_jpn.csv)
//   <generated>/vu1/vu1_<hash>.bin   VU1 microcode images, assembled from the executable by data/vu1/recipe*.txt
//   <generated>/vu1/vu1_<hash>.pcs   their entry points (data/vu1)
//   <generated>/vu1aot/vu1aot_registry.cpp
//   <generated>/vu1_images.txt       the image names, for build.cmd
//
//   maximo_kit region <disc>         prints PAL, USA or JPN for the disc (checked like prepare)
//   maximo_kit sha256 <file>         prints the file's SHA-256
#include <windows.h>
#include <bcrypt.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using Bytes = std::vector<uint8_t>;

namespace
{
    // The releases this build supports. The USA and Japanese function lists and VU1 recipes were made by matching
    // their executables against the PAL one (offsets and hashes only, like the PAL files).
    struct Release
    {
        const char *region, *elfName, *sha256, *title, *funcs, *recipe;
    };
    constexpr Release kReleases[] = {
        {"PAL", "SLES_507.03", "03615557E6291B882A54165E068F653C8613856852E4250F7DE4B075ABFC3C99",
         "Europe (SLES-50703)", "funcs.csv", "recipe.txt"},
        {"USA", "SLUS_200.17", "12049EC55891ADFF413B85BCAAAC106567419146AE76CEEE88BC45E13E1B195F",
         "USA (SLUS-20017)", "funcs_usa.csv", "recipe_usa.txt"},
        {"JPN", "SLPM_621.27", "9F58B67B5029A93B30506B670D6C54D7C326A05A3A03780E796EA03C94A17F57",
         "Japan (SLPM-62127)", "funcs_jpn.csv", "recipe_jpn.txt"},
    };
    constexpr size_t kVu1ImageSize = 16 * 1024;
    constexpr uint64_t kSector = 2048;

    std::string sha256(const uint8_t *data, size_t size)
    {
        uint8_t digest[32];
        if (BCryptHash(BCRYPT_SHA256_ALG_HANDLE, nullptr, 0, const_cast<PUCHAR>(data), static_cast<ULONG>(size), digest,
                       sizeof digest) != 0)
            throw std::runtime_error("Windows could not compute a SHA-256 hash");
        std::string text;
        char hex[3];
        for (uint8_t b : digest)
        {
            std::snprintf(hex, sizeof hex, "%02X", b);
            text += hex;
        }
        return text;
    }

    Bytes readFile(const fs::path &path)
    {
        std::ifstream in(path, std::ios::binary);
        if (!in)
            throw std::runtime_error("cannot open " + path.string());
        return Bytes(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    }

    void writeFile(const fs::path &path, const void *data, size_t size)
    {
        std::ofstream out(path, std::ios::binary);
        out.write(static_cast<const char *>(data), static_cast<std::streamsize>(size));
        if (!out)
            throw std::runtime_error("cannot write " + path.string());
    }

    uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | static_cast<uint32_t>(p[3]) << 24; }

    // A disc image: a plain .iso (2048-byte sectors), a raw .bin (2352-byte sectors: MODE1 with the data at +16 of
    // each sector, MODE2 form 1 at +24), or a .cue sheet naming its .bin. Only the data track is read.
    struct DiscImage
    {
        std::ifstream in;
        uint64_t sectorSize = kSector, dataOffset = 0;

        static std::string upper(std::string t)
        {
            std::transform(t.begin(), t.end(), t.begin(), [](unsigned char c) { return std::toupper(c); });
            return t;
        }

        explicit DiscImage(const fs::path &path)
        {
            fs::path file = path;
            std::string mode;
            if (upper(path.extension().string()) == ".CUE")
            {
                // FILE "name.bin" BINARY / TRACK 01 MODE2/2352: the first FILE holds the data track.
                std::ifstream cue(path);
                if (!cue)
                    throw std::runtime_error("cannot open " + path.string());
                std::string line, bin;
                while (std::getline(cue, line))
                {
                    const std::string u = upper(line);
                    const size_t f = u.find("FILE");
                    if (bin.empty() && f != std::string::npos)
                    {
                        const size_t q1 = line.find('"'), q2 = line.rfind('"');
                        if (q1 != std::string::npos && q2 > q1)
                            bin = line.substr(q1 + 1, q2 - q1 - 1);
                        else
                        {
                            std::istringstream w(line.substr(f + 4));
                            w >> bin;
                        }
                    }
                    if (mode.empty() && u.find("TRACK") != std::string::npos)
                    {
                        if (u.find("MODE1/2352") != std::string::npos) mode = "MODE1/2352";
                        else if (u.find("MODE2/2352") != std::string::npos) mode = "MODE2/2352";
                        else if (u.find("MODE1/2048") != std::string::npos) mode = "MODE1/2048";
                    }
                }
                if (bin.empty())
                    throw std::runtime_error("the .cue sheet names no .bin file");
                file = path.parent_path() / fs::u8path(bin);
                if (!fs::exists(file))
                    throw std::runtime_error("the .cue sheet's file is missing: " + file.string());
            }
            in.open(file, std::ios::binary);
            if (!in)
                throw std::runtime_error("cannot open " + file.string());
            if (mode == "MODE1/2352") { sectorSize = 2352; dataOffset = 16; }
            else if (mode == "MODE2/2352") { sectorSize = 2352; dataOffset = 24; }
            else if (mode.empty())
            {
                // No cue: a raw image starts with the sector sync pattern (00, ten FF, 00); byte 15 is the mode.
                uint8_t head[16] = {};
                in.read(reinterpret_cast<char *>(head), sizeof head);
                in.clear();
                static const uint8_t sync[12] = {0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0};
                if (std::equal(sync, sync + 12, head))
                {
                    sectorSize = 2352;
                    dataOffset = head[15] == 1 ? 16 : 24;
                }
            }
        }

        // size bytes of user data starting at a sector.
        Bytes read(uint64_t sector, size_t size)
        {
            Bytes data(size);
            for (size_t done = 0; done < size; ++sector)
            {
                const size_t n = std::min<size_t>(kSector, size - done);
                in.seekg(static_cast<std::streamoff>(sector * sectorSize + dataOffset));
                in.read(reinterpret_cast<char *>(data.data() + done), static_cast<std::streamsize>(n));
                if (!in)
                    throw std::runtime_error("the disc image ended early");
                done += n;
            }
            return data;
        }
    };

    // ISO 9660: the executable sits in the root directory.
    Bytes readElfFromImage(const fs::path &image, const Release *&found)
    {
        DiscImage disc(image);
        const Bytes descriptor = disc.read(16, kSector);
        if (descriptor[0] != 1 || std::string(descriptor.begin() + 1, descriptor.begin() + 6) != "CD001")
            throw std::runtime_error("this is not a disc image this build can read (.iso, .bin or .cue)");

        const uint8_t *root = &descriptor[156];
        const Bytes directory = disc.read(le32(root + 2), le32(root + 10));

        for (size_t at = 0; at < directory.size();)
        {
            const uint8_t length = directory[at];
            if (length == 0)
            {
                at = (at / kSector + 1) * kSector; // records do not cross sectors
                continue;
            }
            const uint8_t nameLength = directory[at + 32];
            std::string name(reinterpret_cast<const char *>(&directory[at + 33]), nameLength);
            std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::toupper(c); });
            for (const Release &r : kReleases)
                if (name == std::string(r.elfName) + ";1")
                {
                    found = &r;
                    return disc.read(le32(&directory[at + 2]), le32(&directory[at + 10]));
                }
            at += length;
        }
        throw std::runtime_error("this is not a Maximo disc: none of SLES_507.03, SLUS_200.17 or SLPM_621.27 is on it");
    }

    Bytes readElf(const fs::path &disc, const Release *&release)
    {
        release = nullptr;
        Bytes elf;
        if (fs::is_directory(disc))
        {
            for (const Release &r : kReleases)
                if (fs::exists(disc / r.elfName))
                {
                    release = &r;
                    elf = readFile(disc / r.elfName);
                    break;
                }
            if (!release)
                throw std::runtime_error("this is not a Maximo disc folder: none of SLES_507.03, SLUS_200.17 or "
                                         "SLPM_621.27 is in it");
        }
        else
            elf = readElfFromImage(disc, release);
        if (sha256(elf.data(), elf.size()) != release->sha256)
            throw std::runtime_error(std::string("this disc's game executable does not match the ") + release->title +
                                     " release (v1.00) this build knows");
        return elf;
    }

    std::string forwardSlashes(const fs::path &path) { return path.generic_string(); }

    void writeRecompilerSettings(const fs::path &repo, const fs::path &generated, const Release &release)
    {
        std::ofstream out(generated / "recomp.toml", std::ios::binary);
        out << "[general]\n"
            << "input = \"" << forwardSlashes(generated / release.elfName) << "\"\n"
            << "ghidra_output = \"" << forwardSlashes(repo / "data" / release.funcs) << "\"\n"
            << "output = \"" << forwardSlashes(generated / "game") << "\"\n"
            << "single_file_output = false\npatch_syscalls = false\npatch_cop0 = true\npatch_cache = true\n"
            << "stubs = []\nskip = []\n";
        if (!out)
            throw std::runtime_error("cannot write recomp.toml");
    }

    // recipe.txt: "image <name> <sha256>", then "copy <image offset> <executable offset> <bytes>" in hex.
    std::vector<std::string> buildVu1Images(const Bytes &elf, const fs::path &repo, const fs::path &generated,
                                            const Release &release)
    {
        const fs::path data = repo / "data" / "vu1";
        const fs::path out = generated / "vu1";
        fs::create_directories(out);

        std::ifstream recipe(data / release.recipe);
        if (!recipe)
            throw std::runtime_error(std::string("data/vu1/") + release.recipe + " is missing");

        std::vector<std::string> names;
        Bytes image;
        std::string expected;

        const auto finish = [&] {
            if (names.empty())
                return;
            if (sha256(image.data(), image.size()) != expected)
                throw std::runtime_error("VU1 image " + names.back() + " does not match its recipe");
            writeFile(out / (names.back() + ".bin"), image.data(), image.size());
            fs::copy_file(data / (names.back() + ".pcs"), out / (names.back() + ".pcs"),
                          fs::copy_options::overwrite_existing);
        };

        std::string line;
        while (std::getline(recipe, line))
        {
            std::istringstream words(line);
            std::string word;
            if (!(words >> word) || word[0] == '#')
                continue;
            if (word == "image")
            {
                finish();
                std::string name;
                words >> name >> expected;
                names.push_back(name);
                image.assign(kVu1ImageSize, 0);
            }
            else if (word == "copy")
            {
                uint64_t to = 0, from = 0, size = 0;
                words >> std::hex >> to >> from >> size;
                if (names.empty() || to + size > image.size() || from + size > elf.size())
                    throw std::runtime_error("bad line in data/vu1/recipe.txt: " + line);
                std::copy_n(elf.begin() + static_cast<std::ptrdiff_t>(from), size,
                            image.begin() + static_cast<std::ptrdiff_t>(to));
            }
            else
                throw std::runtime_error("bad line in data/vu1/recipe.txt: " + line);
        }
        finish();
        return names;
    }

    void writeRegistry(const std::vector<std::string> &names, const fs::path &generated)
    {
        fs::create_directories(generated / "vu1aot");
        std::ofstream out(generated / "vu1aot" / "vu1aot_registry.cpp", std::ios::binary);
        out << "// Written by maximo_kit: registers the VU1 microcode compiled from the player's disc.\n"
            << "#include \"runtime/ps2_vu1_aot.h\"\n\n";
        for (const std::string &name : names)
            out << "extern const VU1AotImage vu1aot_image_" << name.substr(4) << ";\n";
        out << "\nvoid registerMaximoVu1Aot()\n{\n    static const VU1AotImage images[] = {\n";
        for (const std::string &name : names)
            out << "        vu1aot_image_" << name.substr(4) << ",\n";
        out << "    };\n    VU1Interpreter::registerAotImages(images, sizeof(images) / sizeof(images[0]));\n}\n";
        if (!out)
            throw std::runtime_error("cannot write vu1aot_registry.cpp");

        std::ofstream list(generated / "vu1_images.txt", std::ios::binary);
        for (const std::string &name : names)
            list << name << "\n";
    }

    const Release &prepare(const fs::path &disc, const fs::path &repoArg, const fs::path &generatedArg)
    {
        const fs::path repo = fs::absolute(repoArg);
        const fs::path generated = fs::absolute(generatedArg);
        fs::create_directories(generated);

        const Release *release = nullptr;
        const Bytes elf = readElf(disc, release);
        writeFile(generated / release->elfName, elf.data(), elf.size());
        writeFile(generated / "region.txt", release->region, std::strlen(release->region));
        writeRecompilerSettings(repo, generated, *release);
        writeRegistry(buildVu1Images(elf, repo, generated, *release), generated);
        return *release;
    }
}

int main(int argc, char **argv)
{
    try
    {
        if (argc == 5 && std::string(argv[1]) == "prepare")
        {
            const Release &release = prepare(argv[2], argv[3], argv[4]);
            std::printf("Disc checked: Maximo, %s. Build inputs written.\n", release.title);
            return 0;
        }
        if (argc == 3 && std::string(argv[1]) == "region")
        {
            const Release *release = nullptr;
            readElf(argv[2], release);
            std::puts(release->region);
            return 0;
        }
        if (argc == 3 && std::string(argv[1]) == "sha256")
        {
            const Bytes file = readFile(argv[2]);
            std::puts(sha256(file.data(), file.size()).c_str());
            return 0;
        }
        std::fputs("usage: maximo_kit prepare <disc.iso or folder> <repository> <generated folder>\n"
                   "       maximo_kit region <disc.iso or folder>\n"
                   "       maximo_kit sha256 <file>\n", stderr);
        return 2;
    }
    catch (const std::exception &e)
    {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
}
