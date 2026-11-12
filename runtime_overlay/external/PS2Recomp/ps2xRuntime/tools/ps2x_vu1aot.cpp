// ps2x_vu1aot: ahead-of-time translation of a VU1 microcode image to C++ (see runtime/ps2_vu1_aot.h).
//
//   ps2x_vu1aot <image.bin> <entry-pcs.txt> <out.cpp>
//
// image.bin is a 16 KiB VU1 micro memory dump (PS2X_VU1_AOT_DUMP writes them, named by hash, with the entry PCs
// that were called). The output defines `extern const VU1AotImage vu1aot_image_<hash>`.
//
// The generator walks every path from each entry PC. A node is an instruction pair together with the
// interpreter's pipeline metadata at the top of that pair (pending entries and their slots, ready cycles, write
// sequences, branch state), normalised to the pair's cycle. That metadata determines stalls, commit points and slot
// allocation exactly as VU1Interpreter::run does, so the emitted code only computes values. Nodes are shared when
// their normalised state is equal, which closes loops. Anything not static ends the node with a handover to the
// interpreter, which is exact at the top of any pair.
#include "runtime/ps2_vu1.h"
#include "runtime/ps2_vu1_aot.h"
#include "runtime/ps2_memory.h"

#include <algorithm>
#include <bit>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <memory>
#include <regex>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace
{
    constexpr uint32_t kCodeSize = PS2_VU1_CODE_SIZE;
    constexpr uint32_t kPcMask = 0x3FFFu;
    constexpr size_t kMaxVariantsPerPc = 24;
    constexpr size_t kMaxNodes = 60000;
    constexpr size_t kChunkNodes = 96; // nodes per emitted function (see emitChunks)

    constexpr uint8_t laneBit(uint32_t component) { return static_cast<uint8_t>(1u << (3u - component)); }

    std::string hex(uint64_t v)
    {
        char b[32];
        std::snprintf(b, sizeof b, "0x%llX", static_cast<unsigned long long>(v));
        return b;
    }
}

// Friend of VU1Interpreter: uses the interpreter's own decoder, so usage, latencies and pipelines match.
class VU1AotCompiler
{
public:
    using Pair = VU1Interpreter::DecodedInstructionPair;
    using Usage = VU1Interpreter::InstructionUsage;

    struct AFlag
    {
        bool valid = false;
        int ready = 0;
        int issue = 0;
        uint8_t bits = 0; // mac 1, status 2, sticky 4, clip 8
    };
    struct AScalar
    {
        bool valid = false;
        int ready = 0;
    };
    struct AStore
    {
        bool valid = false;
        int ready = 0;
        uint8_t lanes = 0;
    };
    struct AVf
    {
        bool valid = false;
        int ready = 0;
        uint32_t seq = 0;
        uint8_t reg = 0, lanes = 0;
    };
    struct AVi
    {
        bool valid = false;
        int ready = 0;
        uint32_t seq = 0;
        uint8_t reg = 0;
    };
    struct AAcc
    {
        bool valid = false;
        int ready = 0;
        uint32_t seq = 0;
        uint8_t lanes = 0;
    };

    // Interpreter pipeline metadata. Times are relative to the current pair's cycle (0); during the simulation of one
    // node they are relative to the node's first cycle.
    struct AState
    {
        AFlag flag[8];
        AScalar fdiv;
        AScalar efu[2];
        AStore store[8];
        AVf vf[16];
        AVi vi[8];
        AAcc acc[8];
        int vfReady[32][4]{};
        int viReady[16]{};
        int accReady[4]{};
        uint32_t vfLatest[32][4]{};
        uint32_t viLatest[16]{};
        uint32_t accLatest[4]{};
        uint32_t nextSeq = 0;
        int efuResource = 0;
        bool branchPending = false, branchDynamic = false, ebit = false, haltAfterDelaySlot = false;
        uint32_t branchTarget = 0;
        uint8_t branchDelay = 0;
        uint8_t backupReg = 0;
        bool kickMaybe = false;

        bool anyPending() const
        {
            if (fdiv.valid || efu[0].valid || efu[1].valid)
                return true;
            for (const auto &e : flag)
                if (e.valid) return true;
            for (const auto &e : store)
                if (e.valid) return true;
            for (const auto &e : vf)
                if (e.valid) return true;
            for (const auto &e : vi)
                if (e.valid) return true;
            for (const auto &e : acc)
                if (e.valid) return true;
            return false;
        }
    };

    struct Node
    {
        uint32_t pc;
        AState state;
        int id;
    };

    explicit VU1AotCompiler(std::vector<uint8_t> code) : m_code(std::move(code)), m_vu(std::make_unique<VU1Interpreter>(VU1Interpreter::Unit::VU1)) {}

    bool generate(const std::vector<uint32_t> &entries, uint64_t hash, std::ostream &out);
    void emitChunks(const std::vector<std::pair<uint32_t, int>> &entryNodes, std::ostream &out);

private:
    std::vector<uint8_t> m_code;
    std::unique_ptr<VU1Interpreter> m_vu;
    std::map<std::string, int> m_nodeByKey;
    std::vector<Node> m_nodes;
    std::vector<int> m_work;
    std::map<uint32_t, size_t> m_variants;
    std::set<uint32_t> m_jrTargets;
    std::ostringstream m_body;           // the node being simulated
    std::vector<std::string> m_nodeText; // each node's code, by id
    std::ostringstream m_tables;
    std::map<std::string, int> m_handoverByKey;
    int m_handovers = 0;
    std::vector<int> m_suspendNodes; // nodes whose top can stop at the cycle budget and continue later
    size_t m_variantOverflows = 0;

    Pair decode(uint32_t pc) const { return m_vu->decodeInstructionPair(m_code.data(), pc); }
    uint32_t word(uint32_t pc) const
    {
        uint32_t w;
        std::memcpy(&w, m_code.data() + pc, 4);
        return w;
    }

    // ---- state normalisation ----
    static void shift(AState &s, int by)
    {
        auto sh = [by](int &t) { t = t - by > 0 ? t - by : 0; };
        for (auto &e : s.flag)
            if (e.valid) { e.ready -= by; e.issue -= by; }
        if (s.fdiv.valid) s.fdiv.ready -= by;
        for (auto &e : s.efu)
            if (e.valid) e.ready -= by;
        for (auto &e : s.store)
            if (e.valid) e.ready -= by;
        for (auto &e : s.vf)
            if (e.valid) e.ready -= by;
        for (auto &e : s.vi)
            if (e.valid) e.ready -= by;
        for (auto &e : s.acc)
            if (e.valid) e.ready -= by;
        for (auto &r : s.vfReady)
            for (int &t : r) sh(t);
        for (int &t : s.viReady) sh(t);
        for (int &t : s.accReady) sh(t);
        sh(s.efuResource);
    }

    static void renumber(AState &s)
    {
        std::vector<uint32_t> seqs;
        for (const auto &e : s.vf)
            if (e.valid) seqs.push_back(e.seq);
        for (const auto &e : s.vi)
            if (e.valid) seqs.push_back(e.seq);
        for (const auto &e : s.acc)
            if (e.valid) seqs.push_back(e.seq);
        std::sort(seqs.begin(), seqs.end());
        auto map = [&](uint32_t seq) -> uint32_t
        {
            const auto it = std::lower_bound(seqs.begin(), seqs.end(), seq);
            return (it != seqs.end() && *it == seq) ? static_cast<uint32_t>(it - seqs.begin()) + 1u : 0u;
        };
        for (auto &e : s.vf)
            if (e.valid) e.seq = map(e.seq);
        for (auto &e : s.vi)
            if (e.valid) e.seq = map(e.seq);
        for (auto &e : s.acc)
            if (e.valid) e.seq = map(e.seq);
        for (auto &r : s.vfLatest)
            for (uint32_t &v : r) v = map(v);
        for (uint32_t &v : s.viLatest) v = map(v);
        for (uint32_t &v : s.accLatest) v = map(v);
        s.nextSeq = static_cast<uint32_t>(seqs.size());
        for (auto &e : s.flag)
            if (e.valid) e.issue = e.ready - 4; // every flag entry has the FMAC latency
    }

    static std::string key(uint32_t pc, const AState &s)
    {
        std::string k;
        auto put = [&k](const void *p, size_t n) { k.append(static_cast<const char *>(p), n); };
        put(&pc, 4);
        for (const auto &e : s.flag) { put(&e.valid, 1); if (e.valid) { put(&e.ready, 4); put(&e.bits, 1); } }
        put(&s.fdiv.valid, 1); if (s.fdiv.valid) put(&s.fdiv.ready, 4);
        for (const auto &e : s.efu) { put(&e.valid, 1); if (e.valid) put(&e.ready, 4); }
        for (const auto &e : s.store) { put(&e.valid, 1); if (e.valid) { put(&e.ready, 4); put(&e.lanes, 1); } }
        for (const auto &e : s.vf) { put(&e.valid, 1); if (e.valid) { put(&e.ready, 4); put(&e.seq, 4); put(&e.reg, 1); put(&e.lanes, 1); } }
        for (const auto &e : s.vi) { put(&e.valid, 1); if (e.valid) { put(&e.ready, 4); put(&e.seq, 4); put(&e.reg, 1); } }
        for (const auto &e : s.acc) { put(&e.valid, 1); if (e.valid) { put(&e.ready, 4); put(&e.seq, 4); put(&e.lanes, 1); } }
        put(s.vfReady, sizeof s.vfReady);
        put(s.viReady, sizeof s.viReady);
        put(s.accReady, sizeof s.accReady);
        put(s.vfLatest, sizeof s.vfLatest);
        put(s.viLatest, sizeof s.viLatest);
        put(s.accLatest, sizeof s.accLatest);
        put(&s.efuResource, 4);
        const uint8_t flags[] = {s.branchPending, s.branchDynamic, s.ebit, s.haltAfterDelaySlot, s.branchDelay,
                                 s.backupReg, s.kickMaybe};
        put(flags, sizeof flags);
        if (s.branchPending && !s.branchDynamic) put(&s.branchTarget, 4);
        return k;
    }

    // Returns the node id for (pc, state at its pair top), creating it; -1 if the variant limit is reached.
    int node(uint32_t pc, const AState &state)
    {
        AState s = state;
        renumber(s);
        const std::string k = key(pc, s);
        const auto it = m_nodeByKey.find(k);
        if (it != m_nodeByKey.end())
            return it->second;
        if (m_variants[pc] >= kMaxVariantsPerPc || m_nodes.size() >= kMaxNodes)
        {
            ++m_variantOverflows;
            return -1;
        }
        ++m_variants[pc];
        const int id = static_cast<int>(m_nodes.size());
        m_nodes.push_back({pc, s, id});
        m_nodeByKey.emplace(k, id);
        m_work.push_back(id);
        return id;
    }

    // ---- handover descriptors ----
    // Describes `s` at time `t` of the current node, as the interpreter state at the top of the pair at `pc`.
    std::string handoverRef(uint32_t pc, bool dynamicPc, AState s, int t)
    {
        shift(s, t);
        renumber(s);
        std::ostringstream e;
        int count = 0;
        auto entry = [&](int kind, int slot, int reg, int lanes, int latest, int bits, int ready, uint32_t seq)
        {
            e << "{" << kind << "," << slot << "," << reg << "," << lanes << "," << latest << "," << bits << "," << ready
              << "," << seq << "},";
            ++count;
        };
        for (int i = 0; i < 8; ++i)
            if (s.flag[i].valid) entry(VU1AotEntry::Flag, i, 0, 0, 0, s.flag[i].bits, s.flag[i].ready, 0);
        if (s.fdiv.valid) entry(VU1AotEntry::Fdiv, 0, 0, 0, 0, 0, s.fdiv.ready, 0);
        for (int i = 0; i < 2; ++i)
            if (s.efu[i].valid) entry(VU1AotEntry::Efu, i, 0, 0, 0, 0, s.efu[i].ready, 0);
        for (int i = 0; i < 8; ++i)
            if (s.store[i].valid) entry(VU1AotEntry::Store, i, 0, s.store[i].lanes, 0, 0, s.store[i].ready, 0);
        for (int i = 0; i < 16; ++i)
        {
            const AVf &w = s.vf[i];
            if (!w.valid) continue;
            int latest = 0;
            for (uint32_t c = 0; c < 4; ++c)
                if ((w.lanes & laneBit(c)) && s.vfLatest[w.reg][c] == w.seq) latest |= laneBit(c);
            entry(VU1AotEntry::VfWrite, i, w.reg, w.lanes, latest, 0, w.ready, w.seq);
        }
        for (int i = 0; i < 8; ++i)
        {
            const AVi &w = s.vi[i];
            if (!w.valid) continue;
            entry(VU1AotEntry::ViWrite, i, w.reg, 0, s.viLatest[w.reg] == w.seq ? 1 : 0, 0, w.ready, w.seq);
        }
        for (int i = 0; i < 8; ++i)
        {
            const AAcc &w = s.acc[i];
            if (!w.valid) continue;
            int latest = 0;
            for (uint32_t c = 0; c < 4; ++c)
                if ((w.lanes & laneBit(c)) && s.accLatest[c] == w.seq) latest |= laneBit(c);
            entry(VU1AotEntry::AccWrite, i, 0, w.lanes, latest, 0, w.ready, w.seq);
        }
        for (int r = 0; r < 32; ++r)
        {
            // group lanes with equal ready times
            for (uint32_t c = 0; c < 4; ++c)
            {
                const int ready = s.vfReady[r][c];
                if (ready <= 0) continue;
                bool first = true;
                for (uint32_t p = 0; p < c; ++p)
                    if (s.vfReady[r][p] == ready) first = false;
                if (!first) continue;
                int lanes = 0;
                for (uint32_t q = 0; q < 4; ++q)
                    if (s.vfReady[r][q] == ready) lanes |= laneBit(q);
                entry(VU1AotEntry::VfReady, 0, r, lanes, 0, 0, ready, 0);
            }
        }
        for (int r = 1; r < 16; ++r)
            if (s.viReady[r] > 0) entry(VU1AotEntry::ViReady, 0, r, 0, 0, 0, s.viReady[r], 0);
        for (uint32_t c = 0; c < 4; ++c)
            if (s.accReady[c] > 0) entry(VU1AotEntry::AccReady, 0, 0, laneBit(c), 0, 0, s.accReady[c], 0);

        std::ostringstream h;
        h << (dynamicPc ? "0xFFFFFFFFu" : hex(pc)) << "," << (s.branchDynamic ? 0u : s.branchTarget) << ","
          << int(s.branchPending) << "," << int(s.branchDelay) << "," << int(s.branchDynamic) << "," << int(s.ebit) << ","
          << int(s.haltAfterDelaySlot) << "," << int(s.backupReg) << "," << s.efuResource << "," << s.nextSeq << ","
          << count << ",";
        const std::string k = e.str() + "|" + h.str();
        auto it = m_handoverByKey.find(k);
        if (it == m_handoverByKey.end())
        {
            const int id = m_handovers++;
            if (count)
                m_tables << "static const VU1AotEntry E" << id << "[] = {" << e.str() << "};\n";
            m_tables << "static const VU1AotHandover H" << id << " = {" << h.str() << (count ? "E" + std::to_string(id) : "nullptr")
                     << "};\n";
            it = m_handoverByKey.emplace(k, id).first;
        }
        return "H" + std::to_string(it->second);
    }

    // ---- simulation helpers (emit into `o`) ----
    static int allocate(const auto &slots, int count)
    {
        for (int i = 0; i < count; ++i)
            if (!slots[i].valid) return i;
        return -1;
    }

    // advanceOneCycle at time t: commits, then one cycle of PATH1.
    void commitAt(AState &s, int t, std::ostream &o)
    {
        for (int i = 0; i < 8; ++i)
        {
            AFlag &e = s.flag[i];
            if (!e.valid || e.ready > t) continue;
            if (e.bits & 1) o << "s.mac = P.flag[" << i << "].mac;";
            if (e.bits & 2)
                o << "{uint32_t c_ = P.flag[" << i << "].status & 0xFu; s.status = (s.status & 0xFF0u) | c_ | ((c_ | P.flag["
                  << i << "].extraSticky) << 6);}";
            if (e.bits & 4) o << "s.status = (s.status & 0x03Fu) | (P.flag[" << i << "].status & 0xFC0u);";
            if (e.bits & 8) o << "s.clip = P.flag[" << i << "].clip;";
            e = {};
        }
        if (s.fdiv.valid && s.fdiv.ready <= t)
        {
            o << "s.q = P.q; {uint32_t d_ = P.qStatusDi & 0x30u; s.status = (s.status & 0xFCFu) | d_ | (d_ << 6);}";
            s.fdiv = {};
        }
        for (int i = 0; i < 2; ++i)
            if (s.efu[i].valid && s.efu[i].ready <= t)
            {
                o << "s.p = P.p[" << i << "];";
                s.efu[i] = {};
            }
        for (int i = 0; i < 8; ++i)
            if (s.store[i].valid && s.store[i].ready <= t)
            {
                o << "VU1AotRuntime::commitStore(vu, P.store[" << i << "], " << int(s.store[i].lanes) << ");";
                s.store[i] = {};
            }
        // Register commits are whole 16-byte stores (a blend for partial lanes): the next read of the register is a
        // 16-byte load, which store forwarding cannot serve from four 4-byte stores.
        auto commitLanes = [&o](const std::string &target, const std::string &value, int blend)
        {
            if (blend == 0xF)
                o << "std::memcpy(" << target << ", " << value << ", 16);";
            else if (blend != 0)
                o << "_mm_storeu_ps(" << target << ", _mm_blend_ps(_mm_loadu_ps(" << target << "), _mm_loadu_ps(" << value
                  << "), " << blend << "));";
        };
        for (int i = 0; i < 16; ++i)
        {
            AVf &w = s.vf[i];
            if (!w.valid || w.ready > t) continue;
            int blend = 0; // _mm_blend_ps order: bit c = component c
            for (uint32_t c = 0; c < 4; ++c)
                if ((w.lanes & laneBit(c)) && s.vfLatest[w.reg][c] == w.seq)
                    blend |= 1 << c;
            commitLanes("s.vf[" + std::to_string(w.reg) + "]", "P.vf[" + std::to_string(i) + "]", blend);
            w = {};
        }
        for (int i = 0; i < 8; ++i)
        {
            AVi &w = s.vi[i];
            if (!w.valid || w.ready > t) continue;
            if (s.viLatest[w.reg] == w.seq)
                o << "s.vi[" << int(w.reg) << "] = static_cast<int16_t>(P.vi[" << i << "]);";
            w = {};
        }
        for (int i = 0; i < 8; ++i)
        {
            AAcc &w = s.acc[i];
            if (!w.valid || w.ready > t) continue;
            int blend = 0;
            for (uint32_t c = 0; c < 4; ++c)
                if ((w.lanes & laneBit(c)) && s.accLatest[c] == w.seq)
                    blend |= 1 << c;
            commitLanes("s.acc", "P.acc[" + std::to_string(i) + "]", blend);
            w = {};
        }
        if (s.kickMaybe)
            o << "VU1AotRuntime::kick(vu);";
        o << "\n";
    }

    int readyCycle(const AState &s, const Pair &d) const
    {
        int ready = 0;
        for (const Usage *u : {&d.upperUsage, &d.lowerUsage})
        {
            for (uint32_t i = 0; i < u->vfReadCount; ++i)
                for (uint32_t c = 0; c < 4; ++c)
                    if (u->vfRead[i].lanes & laneBit(c)) ready = std::max(ready, s.vfReady[u->vfRead[i].reg][c]);
            for (uint32_t r = 1; r < 16; ++r)
                if (u->viRead & (1u << r)) ready = std::max(ready, s.viReady[r]);
            for (uint32_t c = 0; c < 4; ++c)
                if (u->accRead & laneBit(c)) ready = std::max(ready, s.accReady[c]);
        }
        using P = VU1Interpreter::Pipeline;
        if (d.lowerUsage.pipeline == P::PipelineFdiv && s.fdiv.valid) ready = std::max(ready, s.fdiv.ready);
        if (d.lowerUsage.pipeline == P::PipelineEfu) ready = std::max(ready, s.efuResource);
        if (d.lowerUsage.waitQ && s.fdiv.valid) ready = std::max(ready, s.fdiv.ready);
        if (d.lowerUsage.waitP)
            for (const auto &e : s.efu)
                if (e.valid) ready = std::max(ready, e.ready);
        return ready;
    }

    void simulate(const Node &n);
    bool emitUpper(const Pair &d, AState &s, int t, std::ostream &o, int &flagSlot, bool &directVf0);
    bool emitLower(const Pair &d, uint32_t pc, AState &s, int t, std::ostream &o, int &lowerVfSlotOut, int &viSlotOut,
                   int &branchKind);
};

namespace
{
    // Lower-instruction formats that the generator emits.
    enum BranchKind
    {
        kNoBranch,
        kStatic,      // B, BAL
        kConditional, // IBxx (cond)
        kDynamic      // JR, JALR (jt)
    };
}

// Upper instruction: computes the result into u_[4] and queues the flag entry. Returns false on a full flag pipeline.
bool VU1AotCompiler::emitUpper(const Pair &d, AState &s, int t, std::ostream &o, int &flagSlot, bool &directVf0)
{
    using namespace vu1aot;
    const uint32_t I = d.upper;
    const UpperOp u = decodeUpper(I);
    const uint8_t dest = static_cast<uint8_t>((I >> 21) & 0xFu);
    flagSlot = -1;
    directVf0 = false;
    if (u.form == Form::Nop)
        return true;
    if (u.form == Form::Clip)
    {
        const int k = allocate(s.flag, 8);
        if (k < 0) return false;
        s.flag[k] = {true, t + 4, t, 8};
        o << "{uint32_t cf_ = vu1aot::clip<" << hex(I) << "u>(s); wclip = ((wclip << 6) | (cf_ & 0x3Fu)) & 0xFFFFFFu; P.flag["
          << k << "].clip = wclip;}\n";
        flagSlot = k;
        return true;
    }
    if (dest == 0u)
        return true; // no lanes written and no flags queued
    const uint8_t destReg = u.toAcc ? 0xFF : u.toFt ? static_cast<uint8_t>((I >> 16) & 0x1F) : static_cast<uint8_t>((I >> 6) & 0x1F);
    if (isFmac(u.form))
    {
        const int k = allocate(s.flag, 8);
        if (k < 0) return false;
        s.flag[k] = {true, t + 4, t, 3};
        o << "vu1aot::fmac<" << hex(I) << "u>(s, u_, P.flag[" << k << "]);\n";
        flagSlot = k;
    }
    else
        o << "vu1aot::upperPlain<" << hex(I) << "u>(s, u_);\n";
    if (destReg == 0u)
    {
        // VF0 is not tracked: the interpreter writes it directly and restores it after the pair.
        for (uint32_t c = 0; c < 4; ++c)
            if (dest & laneBit(c)) o << "s.vf[0][" << c << "] = u_[" << c << "];";
        o << "\n";
        directVf0 = true;
    }
    return true;
}

// Lower instruction. Values written to VF/VI go to P slots chosen here; stores/FDIV/EFU/flags are queued.
// Returns false when a pipeline is full (the node then hands over before executing).
bool VU1AotCompiler::emitLower(const Pair &d, uint32_t pc, AState &s, int t, std::ostream &o, int &lowerVfSlot, int &viSlot,
                               int &branchKind)
{
    const uint32_t I = d.lower;
    lowerVfSlot = -1;
    viSlot = -1;
    branchKind = kNoBranch;
    if (d.iBit || I == 0u || I == 0x8000033Cu)
        return true;

    const Usage &lu = d.lowerUsage;
    const uint8_t opHi = (I >> 25) & 0x7F;
    const uint8_t vfT = (I >> 16) & 0x1F, vfS = (I >> 11) & 0x1F;
    const uint8_t viT = (I >> 16) & 0xF, viS = (I >> 11) & 0xF, viD = (I >> 6) & 0xF;
    const uint8_t dest = (I >> 21) & 0xF;
    const int imm11 = static_cast<int16_t>(static_cast<int32_t>(I << 21) >> 21);
    auto comp = [dest]() { return (dest & 8) ? 0 : (dest & 4) ? 1 : (dest & 2) ? 2 : 3; };
    // Value slots are allocated now, but the entries are queued after execution (run() order).
    const uint8_t viWritten = (lu.viWrite & 0xFFFEu) ? static_cast<uint8_t>(std::countr_zero(static_cast<uint32_t>(lu.viWrite & 0xFFFEu))) : 0;
    const bool lowerVf = lu.vfWrite.reg != 0u && d.suppressedLowerVf != lu.vfWrite.reg;
    // Slot numbers are predicted: upper VF write first (if any), then lower, then ACC, then VI (allocate order).
    AState probe = s;
    int upperVf = -1;
    if (d.upperUsage.vfWrite.reg != 0u)
    {
        upperVf = allocate(probe.vf, 16);
        if (upperVf < 0) return false;
        probe.vf[upperVf].valid = true;
    }
    if (lowerVf && (d.upperUsage.vfWrite.reg == 0u || lu.vfWrite.reg != d.upperUsage.vfWrite.reg))
    {
        lowerVfSlot = allocate(probe.vf, 16);
        if (lowerVfSlot < 0) return false;
        probe.vf[lowerVfSlot].valid = true;
    }
    if (viWritten)
    {
        viSlot = allocate(probe.vi, 8);
        if (viSlot < 0) return false;
    }
    const std::string VF = lowerVfSlot >= 0 ? "P.vf[" + std::to_string(lowerVfSlot) + "]" : "dummy_";
    const std::string VI = viSlot >= 0 ? "P.vi[" + std::to_string(viSlot) + "]" : "dummyVi_";
    auto rb = [&s](uint8_t reg) -> std::string
    {
        if (reg == 0) return "0";
        if (reg == s.backupReg) return "bk";
        return "s.vi[" + std::to_string(reg) + "]";
    };
    auto store = [&](const std::string &call) -> bool
    {
        const int k = allocate(s.store, 8);
        if (k < 0) return false;
        s.store[k] = {true, t + 1, dest};
        o << call << "(P.store[" << k << "], ";
        return true;
    };
    auto branchTo = [&](uint32_t target)
    {
        s.branchPending = true;
        s.branchDynamic = false;
        s.branchTarget = target & kPcMask;
        s.branchDelay = 1;
        o << "s.branchTarget = " << hex(s.branchTarget) << "u;\n"; // the interpreter keeps the last issued target
    };
    const uint32_t staticTarget = (pc + 8 + imm11 * 8) & kPcMask;

    switch (opHi)
    {
    case 0x00: // LQ
        o << "vu1aot::loadQ(data, vu1aot::qwordAddress(s.vi[" << int(viS) << "], " << imm11 << "), " << VF << ");\n";
        return true;
    case 0x01: // SQ
        if (!store("vu1aot::storeVf")) return false;
        o << "vu1aot::qwordAddress(s.vi[" << int(viT) << "], " << imm11 << "), s.vf[" << int(vfS) << "]);\n";
        return true;
    case 0x04: // ILW
        o << VI << " = vu1aot::loadWord(data, vu1aot::qwordAddress(s.vi[" << int(viS) << "], " << imm11 << "), " << comp()
          << ");\n";
        return true;
    case 0x05: // ISW
        if (!store("vu1aot::storeVi")) return false;
        o << "vu1aot::qwordAddress(s.vi[" << int(viS) << "], " << imm11 << "), s.vi[" << int(viT) << "]);\n";
        return true;
    case 0x08:
    case 0x09:
    {
        const int imm = static_cast<int16_t>(I & 0x7FF) | ((I >> 10) & 0x7800);
        o << VI << " = static_cast<int16_t>(s.vi[" << int(viS) << "] " << (opHi == 0x08 ? "+" : "-") << " " << imm << ");\n";
        return true;
    }
    case 0x10: o << VI << " = ((s.clip & 0xFFFFFFu) == " << hex(I & 0xFFFFFF) << "u) ? 1 : 0;\n"; return true;
    case 0x11:
    {
        // FCSET: disables same-cycle clip writes, then queues the new clip flag.
        for (auto &e : s.flag)
            if (e.valid && e.issue == t) e.bits &= ~8;
        const int k = allocate(s.flag, 8);
        if (k < 0) return false;
        s.flag[k] = {true, t + 4, t, 8};
        o << "wclip = " << hex(I & 0xFFFFFF) << "u; P.flag[" << k << "].clip = wclip;\n";
        return true;
    }
    case 0x12: o << VI << " = ((s.clip & " << hex(I & 0xFFFFFF) << "u) != 0) ? 1 : 0;\n"; return true;
    case 0x13: o << VI << " = ((s.clip | " << hex(I & 0xFFFFFF) << "u) == 0xFFFFFFu) ? 1 : 0;\n"; return true;
    case 0x14:
    case 0x15:
    case 0x16:
    case 0x17:
    {
        const uint32_t imm12 = (((I >> 21) & 1u) << 11) | (I & 0x7FFu);
        if (opHi == 0x15)
        {
            for (auto &e : s.flag)
                if (e.valid && e.issue == t) e.bits &= ~2;
            const int k = allocate(s.flag, 8);
            if (k < 0) return false;
            s.flag[k] = {true, t + 4, t, 4};
            o << "P.flag[" << k << "].status = " << hex(imm12 & 0xFC0u) << "u;\n";
            return true;
        }
        if (opHi == 0x14) o << VI << " = ((s.status & 0xFFFu) == " << hex(imm12) << "u) ? 1 : 0;\n";
        if (opHi == 0x16) o << VI << " = static_cast<int32_t>((s.status & 0xFFFu) & " << hex(imm12) << "u);\n";
        if (opHi == 0x17) o << VI << " = static_cast<int32_t>((s.status & 0xFFFu) | " << hex(imm12) << "u);\n";
        return true;
    }
    case 0x18:
        o << VI << " = ((s.mac & 0xFFFFu) == static_cast<uint32_t>(static_cast<uint16_t>(s.vi[" << int(viS) << "]))) ? 1 : 0;\n";
        return true;
    case 0x1A:
        o << VI << " = static_cast<int32_t>(s.mac & static_cast<uint32_t>(static_cast<uint16_t>(s.vi[" << int(viS) << "])));\n";
        return true;
    case 0x1B:
        o << VI << " = static_cast<int32_t>(s.mac | static_cast<uint32_t>(static_cast<uint16_t>(s.vi[" << int(viS) << "])));\n";
        return true;
    case 0x1C: o << VI << " = static_cast<int32_t>(s.clip & 0x0FFFu);\n"; return true;
    case 0x20: branchKind = kStatic; branchTo(staticTarget); return true;
    case 0x21:
        o << VI << " = " << ((pc + 16) / 8) << ";\n";
        branchKind = kStatic;
        branchTo(staticTarget);
        return true;
    case 0x24:
    case 0x25:
        o << "jt = (static_cast<uint32_t>(static_cast<uint16_t>(" << rb(viS) << ")) * 8u) & 0x3FFFu;\n";
        if (opHi == 0x25) o << VI << " = " << ((pc + 16) / 8) << ";\n";
        o << "s.branchTarget = jt;\n";
        branchKind = kDynamic;
        s.branchPending = true;
        s.branchDynamic = true;
        s.branchTarget = 0;
        s.branchDelay = 1;
        return true;
    case 0x28:
    case 0x29:
        o << "cond = static_cast<int16_t>(" << rb(viS) << ") " << (opHi == 0x28 ? "==" : "!=") << " static_cast<int16_t>("
          << rb(viT) << ");\n";
        branchKind = kConditional;
        return true;
    case 0x2C:
    case 0x2D:
    case 0x2E:
    case 0x2F:
    {
        static const char *ops[] = {"<", ">", "<=", ">="};
        o << "cond = static_cast<int16_t>(" << rb(viS) << ") " << ops[opHi - 0x2C] << " 0;\n";
        branchKind = kConditional;
        return true;
    }
    case 0x40:
        break;
    default:
        return false;
    }

    const uint8_t funct = I & 0x3F;
    switch (funct)
    {
    case 0x30: o << VI << " = static_cast<int16_t>(s.vi[" << int(viS) << "] + s.vi[" << int(viT) << "]);\n"; return true;
    case 0x31: o << VI << " = static_cast<int16_t>(s.vi[" << int(viS) << "] - s.vi[" << int(viT) << "]);\n"; return true;
    case 0x32:
    {
        const int imm5 = static_cast<int16_t>(static_cast<int32_t>((I >> 6) & 0x1F) << 27 >> 27);
        o << VI << " = static_cast<int16_t>(s.vi[" << int(viS) << "] + " << imm5 << ");\n";
        return true;
    }
    case 0x34: o << VI << " = s.vi[" << int(viS) << "] & s.vi[" << int(viT) << "];\n"; return true;
    case 0x35: o << VI << " = s.vi[" << int(viS) << "] | s.vi[" << int(viT) << "];\n"; return true;
    default:
        if (funct < 0x3C) return false;
    }

    const uint8_t special = static_cast<uint8_t>((I & 3u) | ((I >> 4) & 0x7Cu));
    const uint32_t fsf = (I >> 21) & 3u, ftf = (I >> 23) & 3u;
    switch (special)
    {
    case 0x30: o << "std::memcpy(" << VF << ", s.vf[" << int(vfS) << "], 16);\n"; return true;
    case 0x31:
        o << VF << "[0] = s.vf[" << int(vfS) << "][1]; " << VF << "[1] = s.vf[" << int(vfS) << "][2]; " << VF << "[2] = s.vf["
          << int(vfS) << "][3]; " << VF << "[3] = s.vf[" << int(vfS) << "][0];\n";
        return true;
    case 0x34: // LQI
        o << "vu1aot::loadQ(data, vu1aot::qwordAddress(s.vi[" << int(viS) << "]), " << VF << ");";
        if (viS) o << VI << " = static_cast<int16_t>(s.vi[" << int(viS) << "] + 1);";
        o << "\n";
        return true;
    case 0x35: // SQI
        if (!store("vu1aot::storeVf")) return false;
        o << "vu1aot::qwordAddress(s.vi[" << int(viT) << "]), s.vf[" << int(vfS) << "]);";
        if (viT) o << VI << " = static_cast<int16_t>(s.vi[" << int(viT) << "] + 1);";
        o << "\n";
        return true;
    case 0x36: // LQD
        if (viS)
            o << VI << " = static_cast<int16_t>(s.vi[" << int(viS) << "] - 1); vu1aot::loadQ(data, vu1aot::qwordAddress(" << VI
              << "), " << VF << ");\n";
        else
            o << "vu1aot::loadQ(data, vu1aot::qwordAddress(0), " << VF << ");\n";
        return true;
    case 0x37: // SQD
        if (viT)
        {
            o << VI << " = static_cast<int16_t>(s.vi[" << int(viT) << "] - 1);";
            if (!store("vu1aot::storeVf")) return false;
            o << "vu1aot::qwordAddress(" << VI << "), s.vf[" << int(vfS) << "]);\n";
        }
        else
        {
            if (!store("vu1aot::storeVf")) return false;
            o << "vu1aot::qwordAddress(0), s.vf[" << int(vfS) << "]);\n";
        }
        return true;
    case 0x38:
    case 0x39:
    case 0x3A:
    {
        s.fdiv = {true, t + (special == 0x3A ? 13 : 7)};
        o << "{uint32_t di_; P.q = ";
        if (special == 0x38)
            o << "vu1aot::divide(s.vf[" << int(vfS) << "][" << fsf << "], s.vf[" << int(vfT) << "][" << ftf << "], di_);";
        else if (special == 0x39)
            o << "vu1aot::squareRoot(s.vf[" << int(vfT) << "][" << ftf << "], di_);";
        else
            o << "vu1aot::reciprocalSquareRoot(s.vf[" << int(vfS) << "][" << fsf << "], s.vf[" << int(vfT) << "][" << ftf
              << "], di_);";
        o << " P.qStatusDi = di_ & 0x30u;}\n";
        return true;
    }
    case 0x3B: return true; // WAITQ
    case 0x3C:
        o << "{uint32_t f_; std::memcpy(&f_, &s.vf[" << int(vfS) << "][" << fsf << "], 4); " << VI
          << " = static_cast<int32_t>(static_cast<int16_t>(f_ & 0xFFFFu));}\n";
        return true;
    case 0x3D:
        o << "{int32_t v_ = static_cast<int32_t>(static_cast<int16_t>(s.vi[" << int(viS) << "] & 0xFFFF)); for (int c_ = 0; c_ < 4; ++c_) std::memcpy(&"
          << VF << "[c_], &v_, 4);}\n";
        return true;
    case 0x3E:
        o << VI << " = vu1aot::loadWord(data, vu1aot::qwordAddress(s.vi[" << int(viS) << "]), " << comp() << ");\n";
        return true;
    case 0x3F:
        if (!store("vu1aot::storeVi")) return false;
        o << "vu1aot::qwordAddress(s.vi[" << int(viS) << "]), s.vi[" << int(viT) << "]);\n";
        return true;
    case 0x40:
        o << "{uint32_t x_ = (s.r >> 4) & 1u, y_ = (s.r >> 22) & 1u; s.r = ((s.r << 1) ^ x_ ^ y_) & 0x007FFFFFu; s.r |= 0x3F800000u; "
             "float v_; std::memcpy(&v_, &s.r, 4); for (int c_ = 0; c_ < 4; ++c_) "
          << VF << "[c_] = v_;}\n";
        return true;
    case 0x41:
        o << "{float v_; std::memcpy(&v_, &s.r, 4); for (int c_ = 0; c_ < 4; ++c_) " << VF << "[c_] = v_;}\n";
        return true;
    case 0x42:
        o << "{uint32_t b_; std::memcpy(&b_, &s.vf[" << int(vfS) << "][" << fsf << "], 4); s.r = 0x3F800000u | (b_ & 0x007FFFFFu);}\n";
        return true;
    case 0x43:
        o << "{uint32_t b_; std::memcpy(&b_, &s.vf[" << int(vfS) << "][" << fsf
          << "], 4); s.r = 0x3F800000u | ((s.r ^ b_) & 0x007FFFFFu);}\n";
        return true;
    case 0x64: o << "for (int c_ = 0; c_ < 4; ++c_) " << VF << "[c_] = s.p;\n"; return true;
    case 0x68: o << VI << " = static_cast<int32_t>(s.top & 0x3FFu);\n"; return true;
    case 0x69: o << VI << " = static_cast<int32_t>(s.itop & 0x3FFu);\n"; return true;
    case 0x6C:
        o << "VU1AotRuntime::startKick(vu, cyc + " << t << ", static_cast<uint32_t>(static_cast<uint16_t>(s.vi[" << int(viS)
          << "])));\n";
        s.kickMaybe = true;
        return true;
    case 0x7B: return true; // WAITP
    default:
        break;
    }
    if ((special >= 0x70 && special <= 0x7A) || special == 0x7C || special == 0x7D)
    {
        const int k = allocate(s.efu, 2);
        if (k < 0) return false;
        const int latency = lu.latency;
        s.efu[k] = {true, t + latency};
        s.efuResource = t + (latency > 0 ? latency - 1 : 0);
        o << "P.p[" << k << "] = vu1aot::efu<" << hex(special) << ">(s.vf[" << int(vfS) << "], " << fsf << ");\n";
        return true;
    }
    return false;
}

void VU1AotCompiler::simulate(const Node &n)
{
    std::ostringstream &o = m_body;
    const uint32_t pc = n.pc;
    const Pair d = decode(pc);
    AState s = n.state;
    o << "N" << n.id << ": // pc " << hex(pc) << "\n";

    auto handoverAt = [&](const AState &st, int t, const char *why)
    {
        o << "HO(" << handoverRef(pc, false, st, t) << ", " << t << ", " << hex(pc) << "u); // " << why << "\n";
    };
    if (d.upperUsage.reserved || d.lowerUsage.reserved)
    {
        handoverAt(s, 0, "reserved instruction");
        return;
    }

    // Static stall (calculatePairReadyCycle without the XGKICK term). The interpreter runs the pair if its stall ends
    // before the budget does. At the budget it stops here without running anything, so the compiled code suspends
    // (the next call continues at this node); a budget that ends inside the stall is left to the interpreter.
    const int stall = readyCycle(s, d);
    if (stall > 0)
    {
        o << "if (cyc + " << stall << " >= budgetEnd && cyc < budgetEnd) ";
        handoverAt(s, 0, "cycle budget inside a stall");
    }
    o << "if (cyc >= budgetEnd) SUS(" << handoverRef(pc, false, s, 0) << ", " << n.id << ", " << hex(pc)
      << "u); // cycle budget\n";
    m_suspendNodes.push_back(n.id);
    if (s.kickMaybe)
    {
        // A malformed GIF tag stops the interpreter at the next pair top.
        o << "if (VU1AotRuntime::stopRequested(vu)) ";
        handoverAt(s, 0, "stop requested");
    }
    if (d.dBit || d.tBit)
    {
        o << "if (" << (d.dBit ? "s.dBitEnabled" : "false") << " || " << (d.tBit ? "s.tBitEnabled" : "false") << ") ";
        handoverAt(s, 0, "D/T halt");
    }

    // Pipelines full: the interpreter reports it, so hand over before the pair.
    {
        AState trial = s;
        for (int t = 1; t <= stall; ++t)
        {
            std::ostringstream discard;
            commitAt(trial, t, discard);
        }
        std::ostringstream discard;
        int fs, lv, vs, bk;
        bool v0;
        AState probe = trial;
        if (!emitUpper(d, probe, stall, discard, fs, v0) || !emitLower(d, pc, probe, stall, discard, lv, vs, bk) ||
            (d.upperUsage.accWrite != 0u && allocate(probe.acc, 8) < 0))
        {
            handoverAt(s, 0, "pipeline full");
            return;
        }
    }

    for (int t = 1; t <= stall; ++t)
        commitAt(s, t, o);
    const int t = stall;
    if (d.lowerUsage.pipeline == VU1Interpreter::PipelineXgkick && s.kickMaybe)
    {
        o << "if (VU1AotRuntime::kickActive(vu)) ";
        handoverAt(s, t, "XGKICK while PATH1 busy");
    }

    // Execute the pair.
    o << "{\n";
    const uint8_t viWritten = (d.lowerUsage.viWrite & 0xFFFEu)
                                  ? static_cast<uint8_t>(std::countr_zero(static_cast<uint32_t>(d.lowerUsage.viWrite & 0xFFFEu)))
                                  : 0;
    if (viWritten && d.lowerUsage.delaysNextBranchRead)
        o << "const int32_t bkNew_ = s.vi[" << int(viWritten) << "];\n";
    int flagSlot = -1, lowerVfSlot = -1, viSlot = -1, branchKind = kNoBranch;
    bool directVf0 = false;
    emitUpper(d, s, t, o, flagSlot, directVf0);
    if (d.iBit)
    {
        float imm;
        std::memcpy(&imm, &d.lower, 4);
        uint32_t bits;
        std::memcpy(&bits, &d.lower, 4);
        o << "{uint32_t b_ = " << hex(bits) << "u; float f_; std::memcpy(&f_, &b_, 4); s.i = vu1aot::normalizeOperand(f_);}\n";
    }
    else
        emitLower(d, pc, s, t, o, lowerVfSlot, viSlot, branchKind);

    // Queue the register writes in run() order: upper VF, lower VF, ACC, VI.
    const Usage &uu = d.upperUsage, &lu = d.lowerUsage;
    auto queueVf = [&](uint8_t reg, uint8_t lanes, int latency, const std::string &value)
    {
        const int k = allocate(s.vf, 16);
        AVf &w = s.vf[k];
        w = {true, t + latency, ++s.nextSeq, reg, lanes};
        for (uint32_t c = 0; c < 4; ++c)
            if (lanes & laneBit(c)) s.vfLatest[reg][c] = w.seq;
        if (!value.empty()) o << "std::memcpy(P.vf[" << k << "], " << value << ", 16);\n";
        return k;
    };
    const bool hasUpperWrite = uu.vfWrite.reg != 0u;
    const bool hasLowerWrite = lu.vfWrite.reg != 0u && d.suppressedLowerVf != lu.vfWrite.reg;
    const bool distinctLower = hasLowerWrite && (!hasUpperWrite || lu.vfWrite.reg != uu.vfWrite.reg);
    if (hasUpperWrite)
        queueVf(uu.vfWrite.reg, uu.vfWrite.lanes, uu.vfLatency ? uu.vfLatency : uu.latency, "u_");
    if (distinctLower)
    {
        const int k = queueVf(lu.vfWrite.reg, lu.vfWrite.lanes, lu.vfLatency ? lu.vfLatency : lu.latency, "");
        if (k != lowerVfSlot)
        {
            std::fprintf(stderr, "slot prediction failed at pc 0x%X\n", pc);
            std::exit(3);
        }
    }
    if (uu.accWrite)
    {
        const int k = allocate(s.acc, 8);
        AAcc &w = s.acc[k];
        w = {true, t + 1, ++s.nextSeq, uu.accWrite};
        for (uint32_t c = 0; c < 4; ++c)
            if (uu.accWrite & laneBit(c)) s.accLatest[c] = w.seq;
        o << "std::memcpy(P.acc[" << k << "], u_, 16);\n";
    }
    if (viWritten)
    {
        const int k = allocate(s.vi, 8);
        if (k != viSlot)
        {
            std::fprintf(stderr, "VI slot prediction failed at pc 0x%X\n", pc);
            std::exit(3);
        }
        s.vi[k] = {true, t + (lu.viLatency ? lu.viLatency : lu.latency), ++s.nextSeq, viWritten};
        s.viLatest[viWritten] = s.vi[k].seq;
    }

    // markPairWrites.
    if (lu.vfWrite.reg != 0u && d.suppressedLowerVf != lu.vfWrite.reg)
        for (uint32_t c = 0; c < 4; ++c)
            if (lu.vfWrite.lanes & laneBit(c)) s.vfReady[lu.vfWrite.reg][c] = t + (lu.vfLatency ? lu.vfLatency : lu.latency);
    if (uu.vfWrite.reg != 0u)
        for (uint32_t c = 0; c < 4; ++c)
            if (uu.vfWrite.lanes & laneBit(c)) s.vfReady[uu.vfWrite.reg][c] = t + (uu.vfLatency ? uu.vfLatency : uu.latency);
    for (uint32_t r = 1; r < 16; ++r)
        if (lu.viWrite & (1u << r)) s.viReady[r] = t + (lu.viLatency ? lu.viLatency : lu.latency);
    for (uint32_t c = 0; c < 4; ++c)
        if (uu.accWrite & laneBit(c)) s.accReady[c] = t + 1;

    // Branch backup for the next pair.
    if (viWritten && lu.delaysNextBranchRead)
    {
        o << "bk = bkNew_;\n";
        s.backupReg = viWritten;
    }
    else
        s.backupReg = 0;
    if (directVf0)
        o << "s.vf[0][0] = 0.0f; s.vf[0][1] = 0.0f; s.vf[0][2] = 0.0f; s.vf[0][3] = 1.0f;\n";
    o << "}\n";

    // PC, branch and program end, for the taken and not-taken outcomes.
    auto successor = [&](AState st, bool taken, std::ostream &out)
    {
        if (branchKind == kConditional && taken)
        {
            st.branchPending = true;
            st.branchDynamic = false;
            st.branchTarget = ((pc + 8 + static_cast<int16_t>(static_cast<int32_t>(d.lower << 21) >> 21) * 8)) & kPcMask;
            st.branchDelay = 1;
            out << "s.branchTarget = " << hex(st.branchTarget) << "u;\n";
        }
        uint32_t nextPc = pc + 8u;
        if (nextPc >= kCodeSize) nextPc = 0u;
        bool dynamicPc = false;
        if (st.branchPending)
        {
            if (st.branchDelay == 0)
            {
                dynamicPc = st.branchDynamic;
                nextPc = st.branchTarget;
                st.branchPending = false;
                st.branchDynamic = false;
                st.branchTarget = 0;
            }
            else
                --st.branchDelay;
        }
        bool ended = false;
        if (st.haltAfterDelaySlot || st.ebit)
            ended = true;
        else if (d.eBit)
            st.ebit = true;

        // advanceOneCycle.
        commitAt(st, t + 1, out);
        const int used = t + 1;
        if (ended)
        {
            int u = used;
            while (st.anyPending())
                commitAt(st, ++u, out);
            if (dynamicPc)
                out << "VU1AotRuntime::finish(vu, cyc + " << u << ", wclip, jt); RET(VU1AotResult::Completed);\n";
            else
                out << "VU1AotRuntime::finish(vu, cyc + " << u << ", wclip, " << hex(nextPc) << "u); RET(VU1AotResult::Completed);\n";
            return;
        }
        shift(st, used);
        out << "cyc += " << used << ";\n";
        if (dynamicPc)
        {
            out << "switch (jt) {\n";
            for (uint32_t target : m_jrTargets)
            {
                const int id = node(target, st);
                if (id >= 0) out << "case " << hex(target) << "u: goto N" << id << ";\n";
            }
            out << "default: HO(" << handoverRef(0, true, st, 0) << ", 0, jt);\n}\n";
            return;
        }
        const int id = node(nextPc, st);
        if (id >= 0)
            out << "goto N" << id << ";\n";
        else
            out << "HO(" << handoverRef(nextPc, false, st, 0) << ", 0, " << hex(nextPc) << "u); // variant limit\n";
    };

    if (branchKind == kConditional)
    {
        o << "if (cond) {\n";
        successor(s, true, o);
        o << "}\n";
        successor(s, false, o);
    }
    else
        successor(s, false, o);
}

// Code layout. A single function holding every node is far beyond the size MSVC optimises: it silently compiles such
// a function without optimisation (every value lives in memory, every helper is a call). The nodes are therefore
// grouped into chunk functions of at most kChunkNodes nodes, keeping each loop (strongly connected component of the
// goto graph) in one chunk where it fits. A goto inside a chunk stays a goto; a goto to another chunk saves the values
// that live across nodes (cyc, wclip, bk, jt; P and L stay in the shared context) and returns the target node to run's
// dispatch loop. Node bodies are unchanged, so the computation and its order are those of the single-function form.
void VU1AotCompiler::emitChunks(const std::vector<std::pair<uint32_t, int>> &entryNodes, std::ostream &out)
{
    size_t chunkNodes = kChunkNodes;
    if (const char *v = std::getenv("PS2X_VU1AOT_CHUNK_NODES")) // layout experiments only
        chunkNodes = std::max<size_t>(1, std::strtoul(v, nullptr, 10));
    const int count = static_cast<int>(m_nodes.size());
    const std::regex gotoRe("goto N([0-9]+);");

    std::vector<std::vector<int>> succ(count);
    for (int id = 0; id < count; ++id)
        for (std::sregex_iterator it(m_nodeText[id].begin(), m_nodeText[id].end(), gotoRe), e; it != e; ++it)
            succ[id].push_back(std::stoi((*it)[1].str()));

    // Tarjan's strongly connected components, iterative.
    std::vector<int> index(count, -1), low(count, 0), stack;
    std::vector<bool> onStack(count, false);
    std::vector<std::vector<int>> comps;
    int next = 0;
    for (int root = 0; root < count; ++root)
    {
        if (index[root] >= 0) continue;
        std::vector<std::pair<int, size_t>> call{{root, 0}};
        index[root] = low[root] = next++;
        stack.push_back(root);
        onStack[root] = true;
        while (!call.empty())
        {
            const int v = call.back().first;
            if (call.back().second < succ[v].size())
            {
                const int w = succ[v][call.back().second++];
                if (index[w] < 0)
                {
                    index[w] = low[w] = next++;
                    stack.push_back(w);
                    onStack[w] = true;
                    call.push_back({w, 0});
                }
                else if (onStack[w])
                    low[v] = std::min(low[v], index[w]);
                continue;
            }
            if (low[v] == index[v])
            {
                std::vector<int> c;
                int w;
                do
                {
                    w = stack.back();
                    stack.pop_back();
                    onStack[w] = false;
                    c.push_back(w);
                } while (w != v);
                std::sort(c.begin(), c.end());
                comps.push_back(std::move(c));
            }
            call.pop_back();
            if (!call.empty())
                low[call.back().first] = std::min(low[call.back().first], low[v]);
        }
    }

    // Pack components in order of their first node; split a component only when it alone exceeds the limit.
    std::sort(comps.begin(), comps.end(), [](const auto &a, const auto &b) { return a.front() < b.front(); });
    std::vector<std::vector<int>> chunks(1);
    for (const auto &c : comps)
    {
        if (!chunks.back().empty() && chunks.back().size() + c.size() > chunkNodes)
            chunks.emplace_back();
        for (int id : c)
        {
            if (chunks.back().size() >= chunkNodes)
                chunks.emplace_back();
            chunks.back().push_back(id);
        }
    }
    std::vector<int> chunkOf(count, -1);
    for (size_t k = 0; k < chunks.size(); ++k)
        for (int id : chunks[k]) chunkOf[id] = static_cast<int>(k);

    // Nodes entered from outside their chunk: entry points, resume points and cross-chunk goto targets.
    std::vector<bool> external(count, false), resumable(count, false);
    for (const auto &[pc, id] : entryNodes)
        if (id >= 0) external[id] = true;
    for (int id : m_suspendNodes) external[id] = resumable[id] = true;
    for (int id = 0; id < count; ++id)
        for (int w : succ[id])
            if (chunkOf[w] != chunkOf[id]) external[w] = true;

    out << "struct Ctx\n{\n"
        << "VU1Interpreter &vu; VU1State &s; uint8_t *const data; const uint64_t budgetEnd;\n"
        << "uint64_t cyc; uint32_t wclip; int32_t bk = 0; uint32_t jt = 0;\n"
        << "VU1AotPending P; VU1AotLive L; VU1AotResult result = VU1AotResult::Completed;\n};\n\n"
        << "#define RET(R) do { c.result = (R); return -1; } while (0)\n"
        << "#define HO(H, T, PCV) do { L.pc = (PCV); L.branchTarget = jt; L.viBackupValue = bk; L.workingClip = wclip; "
           "RET(VU1AotRuntime::handover(vu, H, cyc + (T), P, L)); } while (0)\n"
        << "#define SUS(H, NODE, PCV) do { L.pc = (PCV); L.branchTarget = jt; L.viBackupValue = bk; L.workingClip = wclip; "
           "RET(VU1AotRuntime::suspend(vu, H, cyc, P, L, NODE)); } while (0)\n"
        << "#define LEAVE(NODE) do { c.cyc = cyc; c.wclip = wclip; c.bk = bk; c.jt = jt; return (NODE); } while (0)\n\n";
    for (size_t k = 0; k < chunks.size(); ++k)
    {
        out << "int chunk" << k << "(Ctx &c, int node)\n{\n"
            << "VU1Interpreter &vu = c.vu; VU1State &s = c.s; uint8_t *const data = c.data; "
               "const uint64_t budgetEnd = c.budgetEnd;\n"
            << "uint64_t cyc = c.cyc; uint32_t wclip = c.wclip; int32_t bk = c.bk; uint32_t jt = c.jt; bool cond = false;\n"
            << "float u_[4]; float dummy_[4]; int32_t dummyVi_ = 0;\n"
            << "VU1AotPending &P = c.P; VU1AotLive &L = c.L;\n"
            << "(void)vu; (void)dummy_; (void)dummyVi_; (void)cond; (void)data; (void)budgetEnd;\n"
            << "switch (node) {\n";
        for (int id : chunks[k])
            if (external[id]) out << "case " << id << ": goto N" << id << ";\n";
        out << "default: std::abort();\n}\n";
        for (int id : chunks[k])
        {
            const std::string &text = m_nodeText[id];
            size_t last = 0;
            for (std::sregex_iterator it(text.begin(), text.end(), gotoRe), e; it != e; ++it)
            {
                const int w = std::stoi((*it)[1].str());
                out << text.substr(last, static_cast<size_t>(it->position()) - last);
                if (chunkOf[w] == static_cast<int>(k))
                    out << it->str();
                else
                    out << "LEAVE(" << w << ");";
                last = static_cast<size_t>(it->position() + it->length());
            }
            out << text.substr(last);
        }
        out << "}\n\n";
    }
    out << "using Chunk = int (*)(Ctx &, int);\nconst Chunk kChunks[] = {";
    for (size_t k = 0; k < chunks.size(); ++k)
        out << (k % 16 == 0 ? "\n" : " ") << "&chunk" << k << ",";
    out << "\n};\nconst uint16_t kChunkOf[] = {";
    for (int id = 0; id < count; ++id)
        out << (id % 32 == 0 ? "\n" : " ") << chunkOf[id] << ",";
    out << "\n};\nconst bool kResumable[] = {";
    for (int id = 0; id < count; ++id)
        out << (id % 32 == 0 ? "\n" : " ") << (resumable[id] ? 1 : 0) << ",";
    out << "\n};\n\n"
        << "VU1AotResult run(VU1Interpreter &vu, uint64_t budgetEnd)\n{\n"
        << "Ctx c{vu, VU1AotRuntime::state(vu), VU1AotRuntime::data(vu), budgetEnd, VU1AotRuntime::cycle(vu), "
           "VU1AotRuntime::workingClip(vu)};\n"
        << "int node = VU1AotRuntime::resumeNode(vu, c.P, c.bk, c.jt, c.wclip);\n"
        << "if (node >= 0) { if (node >= " << count << " || !kResumable[node]) std::abort(); }\n"
        << "else switch (c.s.pc) {\n";
    for (const auto &[pc, id] : entryNodes)
        if (id >= 0) out << "case " << hex(pc) << "u: node = " << id << "; break;\n";
    out << "default: return VU1AotResult::NotCompiled;\n}\n"
        << "while (node >= 0) node = kChunks[kChunkOf[node]](c, node);\n"
        << "return c.result;\n}\n"
        << "#undef RET\n#undef HO\n#undef SUS\n#undef LEAVE\n";
    std::fprintf(stderr, "ps2x_vu1aot: %d nodes in %zu chunks of at most %zu nodes, %zu loop components\n", count,
                 chunks.size(), chunkNodes, comps.size());
}

bool VU1AotCompiler::generate(const std::vector<uint32_t> &entries, uint64_t hash, std::ostream &out)
{
    // JR/JALR targets: return addresses of every BAL/JALR, plus the entry points.
    for (uint32_t pc = 0; pc + 8 <= kCodeSize; pc += 8)
    {
        const Pair d = decode(pc);
        if (d.iBit) continue;
        const uint8_t opHi = (d.lower >> 25) & 0x7F;
        if (opHi == 0x21 || opHi == 0x25) m_jrTargets.insert((pc + 16) & kPcMask);
    }
    for (uint32_t e : entries) m_jrTargets.insert(e);

    std::vector<std::pair<uint32_t, int>> entryNodes;
    for (uint32_t e : entries)
        entryNodes.push_back({e, node(e, AState{})});
    while (!m_work.empty())
    {
        const int id = m_work.back();
        m_work.pop_back();
        m_body.str("");
        m_body.clear();
        simulate(m_nodes[id]);
        if (m_nodeText.size() <= static_cast<size_t>(id))
            m_nodeText.resize(id + 1);
        m_nodeText[id] = m_body.str();
    }

    char name[64];
    std::snprintf(name, sizeof name, "%016llX", static_cast<unsigned long long>(hash));
    out << "// Generated by ps2x_vu1aot from VU1 microcode image " << name << ". Do not edit.\n"
        << "// " << m_nodes.size() << " nodes, " << m_handovers << " handover descriptors, " << entries.size() << " entries, "
        << m_variantOverflows << " variant-limit exits.\n"
        << "#include \"runtime/ps2_vu1_aot.h\"\n\n#pragma warning(disable: 4102 4700 4701 4189)\n\n"
        << "namespace {\n"
        << m_tables.str()
        << "\n";
    emitChunks(entryNodes, out);
    out << "}\n\nextern const VU1AotImage vu1aot_image_" << name << " = {0x" << name << "ull, &run};\n";
    std::fprintf(stderr, "ps2x_vu1aot %s: %zu nodes, %d handover descriptors, %zu variant-limit exits\n", name,
                 m_nodes.size(), m_handovers, m_variantOverflows);
    return true;
}

// ---- flag liveness (a pass over the finished output) ----------------------------------------------------------------
// An FMAC instruction computes its MAC and status flags into a flag pipeline slot (P.flag[k]), which a later cycle
// commits to s.mac / s.status. Most of those values are overwritten by the next commit before anything reads them
// (FMAND/FMEQ/FMOR read s.mac, FSAND/FSEQ/FSOR s.status): computing them was about a quarter of VU1's time. This
// pass finds, by backward data flow over the nodes, the FMACs whose flags no path can observe and has them call
// vu1aot::fmacResult (the same result, no flags). Conservative: a handover to the interpreter or a program end makes
// everything live; a cycle-budget stop continues at its node (compiled or interpreted, the same program runs). The
// sticky status bits are taken as never read, which the pass checks: an image that reads any status bit other than
// the Z/S/U/O ones FMACs write, or reads s.status in any form it does not know, is left as it was.
// PS2X_VU1AOT_FLAG_LIVENESS=0: off.
static int applyFlagLiveness(std::string &text)
{
    if (const char *v = std::getenv("PS2X_VU1AOT_FLAG_LIVENESS"); v && v[0] == '0')
        return 0;
    struct Node
    {
        size_t begin = 0, end = 0;
        struct Event
        {
            char kind; // W fmac write, M mac commit, S status commit, T status set (FSSET), m mac read, s status read, X exit
            int slot = 0;
            size_t pos = 0, len = 0;
        };
        std::vector<Event> events;
        std::vector<int> succ;
    };
    std::map<int, Node> nodes;
    std::vector<int> order;
    {
        const std::regex label("(^|\n)N([0-9]+): // pc");
        std::vector<std::pair<size_t, int>> labels;
        for (std::sregex_iterator it(text.begin(), text.end(), label), e; it != e; ++it)
            labels.push_back({static_cast<size_t>(it->position(0) + it->length(1)), std::stoi((*it)[2].str())});
        std::vector<size_t> chunkStarts;
        for (size_t p = text.find("\nint chunk"); p != std::string::npos; p = text.find("\nint chunk", p + 1))
            chunkStarts.push_back(p);
        chunkStarts.push_back(text.size());
        for (size_t i = 0; i < labels.size(); ++i)
        {
            size_t end = i + 1 < labels.size() ? labels[i + 1].first : text.size();
            for (size_t cs : chunkStarts)
                if (cs > labels[i].first)
                {
                    end = std::min(end, cs);
                    break;
                }
            Node &n = nodes[labels[i].second];
            n.begin = labels[i].first;
            n.end = end;
            order.push_back(labels[i].second);
        }
    }
    const std::regex ev("vu1aot::fmac<0x[0-9A-Fa-f]+u>\\(s, u_, P\\.flag\\[([0-7])\\]\\)"
                        "|s\\.mac = P\\.flag\\[([0-7])\\]\\.mac;"
                        "|uint32_t c_ = P\\.flag\\[([0-7])\\]\\.status"
                        "|P\\.flag\\[([0-7])\\]\\.status = "
                        "|(s\\.status = \\(s\\.status & 0x(?:FF0|FCF|03F)u\\))"
                        "|\\(s\\.status & 0xFFFu\\) & (0x[0-9A-Fa-f]+)u"
                        "|(s\\.mac)|(s\\.status)"
                        "|SUS\\(H[0-9]+, ([0-9]+), |(RET\\(|HO\\()"
                        "|goto N([0-9]+);|LEAVE\\(([0-9]+)\\)");
    for (auto &[id, n] : nodes)
        for (std::sregex_iterator it(text.begin() + n.begin, text.begin() + n.end, ev), e; it != e; ++it)
        {
            const std::smatch &m = *it;
            const size_t pos = n.begin + static_cast<size_t>(m.position(0));
            if (m[1].matched) n.events.push_back({'W', m[1].str()[0] - '0', pos, static_cast<size_t>(m.length(0))});
            else if (m[2].matched) n.events.push_back({'M', m[2].str()[0] - '0'});
            else if (m[3].matched) n.events.push_back({'S', m[3].str()[0] - '0'});
            else if (m[4].matched) n.events.push_back({'T', m[4].str()[0] - '0'});
            else if (m[5].matched) {} // a commit's own merge of the bits it keeps
            else if (m[6].matched)
            {
                if ((std::stoul(m[6].str(), nullptr, 16) & ~0xFul) != 0)
                    return -1; // a sticky (or D/I) bit is read: leave the image as it is
                n.events.push_back({'s'});
            }
            else if (m[7].matched) n.events.push_back({'m'});
            else if (m[8].matched) return -1; // s.status read in a form this pass does not know
            else if (m[9].matched) n.succ.push_back(std::stoi(m[9].str()));
            else if (m[10].matched) n.events.push_back({'X'});
            else if (m[11].matched) n.succ.push_back(std::stoi(m[11].str()));
            else if (m[12].matched) n.succ.push_back(std::stoi(m[12].str()));
        }
    struct Live
    {
        bool mac = false, status = false;
        bool needMac[8] = {}, needStatus[8] = {};
        bool operator==(const Live &o) const
        {
            return mac == o.mac && status == o.status && std::equal(needMac, needMac + 8, o.needMac) &&
                   std::equal(needStatus, needStatus + 8, o.needStatus);
        }
    };
    Live all;
    all.mac = all.status = true;
    std::fill(all.needMac, all.needMac + 8, true);
    std::fill(all.needStatus, all.needStatus + 8, true);
    std::map<int, Live> in;
    auto outOf = [&](const Node &n)
    {
        if (n.succ.empty())
            return all;
        Live o;
        for (int s : n.succ)
        {
            const auto it = in.find(s);
            if (it == in.end() && !nodes.count(s))
                return all;
            const Live l = it == in.end() ? Live{} : it->second;
            o.mac |= l.mac;
            o.status |= l.status;
            for (int k = 0; k < 8; ++k)
            {
                o.needMac[k] |= l.needMac[k];
                o.needStatus[k] |= l.needStatus[k];
            }
        }
        return o;
    };
    std::vector<std::pair<size_t, size_t>> dead;
    auto transfer = [&](const Node &n, Live l, bool record)
    {
        for (auto e = n.events.rbegin(); e != n.events.rend(); ++e)
            switch (e->kind)
            {
            case 'X': l = all; break;
            case 'm': l.mac = true; break;
            case 's': l.status = true; break;
            case 'M': l.needMac[e->slot] |= l.mac; l.mac = false; break;
            case 'S': l.needStatus[e->slot] |= l.status; l.status = false; break;
            case 'T': l.needStatus[e->slot] = false; break;
            case 'W':
                if (record && !l.needMac[e->slot] && !l.needStatus[e->slot])
                    dead.push_back({e->pos, e->len});
                l.needMac[e->slot] = l.needStatus[e->slot] = false;
                break;
            }
        return l;
    };
    for (bool changed = true; changed;)
    {
        changed = false;
        for (auto it = order.rbegin(); it != order.rend(); ++it)
        {
            const Live l = transfer(nodes[*it], outOf(nodes[*it]), false);
            Live &cur = in[*it];
            if (!(l == cur))
            {
                cur = l;
                changed = true;
            }
        }
    }
    for (int id : order)
        transfer(nodes[id], outOf(nodes[id]), true);
    std::sort(dead.begin(), dead.end());
    std::string out;
    out.reserve(text.size() + dead.size() * 8);
    size_t last = 0;
    for (const auto &[pos, len] : dead)
    {
        out.append(text, last, pos - last);
        out += "vu1aot::fmacResult<";
        out.append(text, pos + std::strlen("vu1aot::fmac<"), len - std::strlen("vu1aot::fmac<"));
        last = pos + len;
    }
    out.append(text, last, std::string::npos);
    text.swap(out);
    return static_cast<int>(dead.size());
}

int main(int argc, char **argv)
{
    if (argc != 4)
    {
        std::fprintf(stderr, "usage: ps2x_vu1aot <image.bin> <entry-pcs.txt> <out.cpp>\n");
        return 2;
    }
    std::ifstream in(argv[1], std::ios::binary);
    std::vector<uint8_t> code((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (code.size() != kCodeSize)
    {
        std::fprintf(stderr, "%s: expected %u bytes\n", argv[1], kCodeSize);
        return 2;
    }
    std::vector<uint32_t> entries;
    std::ifstream pcs(argv[2]);
    for (std::string line; std::getline(pcs, line);)
    {
        if (line.empty() || line[0] == '#') continue;
        entries.push_back(static_cast<uint32_t>(std::stoul(line, nullptr, 0)) & kPcMask);
    }
    std::sort(entries.begin(), entries.end());
    entries.erase(std::unique(entries.begin(), entries.end()), entries.end());
    const uint64_t hash = VU1AotRuntime::hashCode(code.data(), kCodeSize);
    std::ostringstream text;
    VU1AotCompiler compiler(code);
    if (!compiler.generate(entries, hash, text))
        return 1;
    std::string generated = text.str();
    const int resultOnly = applyFlagLiveness(generated);
    if (resultOnly >= 0)
        std::fprintf(stderr, "ps2x_vu1aot: %d FMAC instructions whose flags are never observed compute their result only\n", resultOnly);
    else
        std::fprintf(stderr, "ps2x_vu1aot: flag liveness skipped (the image reads status bits it does not cover)\n");
    std::ofstream out(argv[3], std::ios::binary);
    out << generated;
    return out ? 0 : 1;
}
