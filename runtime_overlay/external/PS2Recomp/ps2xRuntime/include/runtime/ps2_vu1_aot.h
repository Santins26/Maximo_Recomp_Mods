// Ahead-of-time compiled VU1 microprograms.
//
// The generator (tools/ps2x_vu1aot) reads a dumped VU1 microcode image and emits C++ for each entry point. The emitted
// code follows the interpreter (VU1Interpreter::run) cycle by cycle: it simulates the interpreter's pipeline metadata
// (slots, ready cycles, write sequences) at generation time, so stalls, commit points and slot allocation are constants
// in the emitted code. Only the arithmetic, memory accesses, branch conditions and XGKICK transfers run at run time.
//
// The interpreter stays the reference. Compiled code hands over to it, at the top of an instruction pair, whenever
// the timing would stop being static: XGKICK while a transfer is still active, the cycle budget, D/T halts, an unknown
// JR target, a reserved instruction or a full pipeline. A handover writes the pending pipeline entries into the
// interpreter (VU1AotRuntime::handover), which continues exactly where the compiled code stopped.
//
// The arithmetic below mirrors ps2_vu1_upper.cpp / ps2_vu1_lower.cpp / ps2_vu1_core.cpp. Keep them in step; the
// VU1 replay and PS2X_VU1_AOT_VERIFY compare compiled code against the interpreter.
#pragma once

#include "runtime/ps2_vu1.h"

#include <cmath>
#include <immintrin.h>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>

// VU1AotPending (the values of the pipeline entries in flight) is in runtime/ps2_vu1.h.

// Static pipeline metadata at the top of one instruction pair, relative to that pair's cycle.
struct VU1AotEntry
{
    enum Kind : uint8_t
    {
        Flag,     // slot, bits = writesMac 1 | writesStatus 2 | writesSticky 4 | writesClip 8
        Fdiv,     //
        Efu,      // slot
        Store,    // slot, lanes
        VfWrite,  // slot, reg, lanes, latest (lanes for which this write is the latest), seq
        ViWrite,  // slot, reg, latest, seq
        AccWrite, // slot, lanes, latest, seq
        VfReady,  // reg, lanes
        ViReady,  // reg
        AccReady, // lanes
    };
    uint8_t kind, slot, reg, lanes, latest, bits;
    uint16_t ready; // cycles after the pair's cycle
    uint32_t seq;
};

struct VU1AotHandover
{
    uint32_t pc; // kDynamicPc: the pair address is a run-time value (JR/JALR target)
    uint32_t branchTarget;
    uint8_t branchPending, branchDelay, branchTargetDynamic, ebit, haltAfterDelaySlot;
    uint8_t viBackupReg; // 0: no branch backup
    uint16_t efuResourceReady;
    uint32_t nextSequence;
    uint32_t entryCount;
    const VU1AotEntry *entries;
    static constexpr uint32_t kDynamicPc = 0xFFFFFFFFu;
};

enum class VU1AotResult : uint8_t
{
    NotCompiled, // no compiled entry for this PC: the interpreter runs the whole call
    Completed,   // the program ended; pipelines and XGKICK are flushed
    Handover,    // the interpreter continues from the materialised state
};

// Run-time values a handover needs besides the pending values.
struct VU1AotLive
{
    uint32_t pc = 0;           // for kDynamicPc
    uint32_t branchTarget = 0; // for branchTargetDynamic
    int32_t viBackupValue = 0;
    uint32_t workingClip = 0;
};

using VU1AotEntryFn = VU1AotResult (*)(VU1Interpreter &vu, uint64_t budgetEnd);

struct VU1AotImage
{
    uint64_t codeHash; // VU1AotRuntime::hashCode of the 16 KiB image
    VU1AotEntryFn entry;
};

// Friend of VU1Interpreter: the only access compiled code has to interpreter internals.
struct VU1AotRuntime
{
    static uint64_t hashCode(const uint8_t *code, uint32_t size);

    static VU1State &state(VU1Interpreter &vu) { return vu.m_state; }
    static uint64_t cycle(const VU1Interpreter &vu) { return vu.m_cycle; }
    static uint32_t workingClip(const VU1Interpreter &vu) { return vu.m_workingClip; }
    static uint8_t *data(VU1Interpreter &vu) { return vu.m_activeVuData; }
    static bool kickActive(const VU1Interpreter &vu) { return vu.m_xgkick.active; }
    static bool stopRequested(const VU1Interpreter &vu) { return vu.m_stopRequested; }

    // One cycle of PATH1 transfer, as advanceOneCycle does after its commits.
    static void kick(VU1Interpreter &vu)
    {
        if (!vu.m_xgkick.active)
            return;
        // Every other cycle only earns the transfer its cycle (a qword moves every two): done here, inline, as
        // progressXgkick would (credit 0 -> 1, nothing moved), instead of a call per VU1 cycle.
        if (vu.m_xgkick.cycleCredit == 0u && vu.m_activeVuData && vu.m_activeVuDataSize != 0u)
        {
            vu.m_xgkick.cycleCredit = 1u;
            return;
        }
        vu.progressXgkick();
    }
    static void startKick(VU1Interpreter &vu, uint64_t cycle, uint32_t qwordAddress)
    {
        vu.m_cycle = cycle;
        vu.startXgkick(qwordAddress);
    }
    static void commitStore(VU1Interpreter &vu, const VU1AotPending::Store &store, uint8_t laneMask)
    {
        uint8_t *const target = vu.m_activeVuData + store.address;
        for (uint32_t component = 0; component < 4u; ++component)
        {
            if ((laneMask & (1u << (3u - component))) != 0u)
                std::memcpy(target + component * 4u, &store.words[component], 4u);
        }
    }

    // Program end: the static pipeline drain has been emitted; flushPipelines then keeps cycling for PATH1.
    static void finish(VU1Interpreter &vu, uint64_t cycle, uint32_t workingClip, uint32_t pc)
    {
        while (vu.m_xgkick.active)
        {
            ++cycle;
            vu.progressXgkick();
        }
        vu.m_cycle = cycle;
        vu.m_state.cycles = cycle;
        vu.m_workingClip = workingClip;
        vu.m_state.pc = pc;
        vu.m_state.ebit = false;
        vu.m_state.haltAfterDelaySlot = false;
        vu.m_state.branchPending = false;
        vu.m_pendingHaltD = false;
        vu.m_pendingHaltT = false;
        vu.m_viBranchBackupValid = false;
    }

    static VU1AotResult handover(VU1Interpreter &vu, const VU1AotHandover &h, uint64_t cycle,
                                 const VU1AotPending &pending, const VU1AotLive &live);
    // Writes the pending pipeline entries and the static metadata of `h` into the interpreter.
    static void materialise(VU1Interpreter &vu, const VU1AotHandover &h, uint64_t cycle, const VU1AotPending &pending,
                            const VU1AotLive &live);
    // The cycle budget ran out at the top of node `node`, where the interpreter would stop too. Hands over as above
    // and also remembers the compiled state, so the next call can continue in compiled code if nothing ran between.
    static VU1AotResult suspend(VU1Interpreter &vu, const VU1AotHandover &h, uint64_t cycle,
                                const VU1AotPending &pending, const VU1AotLive &live, int32_t node);
    // Called first by every compiled entry: the node to continue at (restoring the pending values and run-time
    // values saved by suspend), or -1 for a normal entry at the current PC.
    static int32_t resumeNode(VU1Interpreter &vu, VU1AotPending &pending, int32_t &viBackupValue,
                              uint32_t &branchTarget, uint32_t &workingClip)
    {
        const int32_t node = vu.m_aotResumeNode;
        if (node < 0)
            return -1;
        vu.m_aotResumeNode = -1;
        pending = vu.m_aotResume.pending;
        viBackupValue = vu.m_aotResume.viBackupValue;
        branchTarget = vu.m_aotResume.branchTarget;
        workingClip = vu.m_aotResume.workingClip;
        return node;
    }
};

namespace vu1aot
{
    using Pending = VU1AotPending;

    inline constexpr uint8_t lane(uint32_t component) { return static_cast<uint8_t>(1u << (3u - component)); }

    inline float normalizeOperand(float value)
    {
        uint32_t bits;
        std::memcpy(&bits, &value, 4);
        const uint32_t exponent = (bits >> 23) & 0xFFu;
        if (exponent == 0u)
            bits &= 0x80000000u;
        else if (exponent == 0xFFu)
            bits = (bits & 0x80000000u) | 0x7F7FFFFFu;
        std::memcpy(&value, &bits, 4);
        return value;
    }

    inline float normalizeResult(float value, uint32_t &laneFlags)
    {
        uint32_t bits;
        std::memcpy(&bits, &value, 4);
        const uint32_t sign = bits & 0x80000000u;
        const uint32_t magnitude = bits & 0x7FFFFFFFu;
        const uint32_t exponent = (bits >> 23) & 0xFFu;
        laneFlags = sign != 0u ? 0x2u : 0u;
        if (magnitude == 0u)
            laneFlags |= 0x1u;
        else if (exponent == 0u)
        {
            laneFlags |= 0x5u;
            bits = sign;
        }
        else if (exponent == 0xFFu)
        {
            laneFlags |= 0x8u;
            bits = sign | 0x7F7FFFFFu;
        }
        std::memcpy(&value, &bits, 4);
        return value;
    }

    inline uint8_t normalizeExact(float &value, long double exactResult)
    {
        // std::signbit(long double) is an out-of-line CRT call (_dsign) on MSVC; long double is a double there.
        static_assert(sizeof(long double) == sizeof(double));
        const double exactDouble = static_cast<double>(exactResult);
        uint64_t exactBits;
        std::memcpy(&exactBits, &exactDouble, 8);
        const bool negative = (exactBits >> 63) != 0u;
        const long double magnitude = std::fabs(exactDouble);
        const long double maximum = static_cast<long double>(std::numeric_limits<float>::max());
        const long double minimum = static_cast<long double>(std::numeric_limits<float>::min());
        uint8_t flags = negative ? 0x2u : 0u;
        uint32_t bits = negative ? 0x80000000u : 0u;
        if (magnitude == 0.0L)
        {
            flags |= 0x1u;
            std::memcpy(&value, &bits, 4);
        }
        else if (magnitude > maximum)
        {
            flags |= 0x8u;
            bits |= 0x7F7FFFFFu;
            std::memcpy(&value, &bits, 4);
        }
        else if (magnitude < minimum)
        {
            flags |= 0x5u;
            std::memcpy(&value, &bits, 4);
        }
        return flags;
    }

    // Upper-pipeline operation classes (op = bits 5:0, special = (instr & 3) | ((instr >> 4) & 0x7C) for op >= 0x3C).
    // Operand forms of the arithmetic: Add = vs + b, Sub = vs - b, Madd = acc + vs * b, Msub = acc - vs * b,
    // Mul = vs * b, OpMsub = acc - vs.yzx * vt.zxy, OpMul = vs.yzx * vt.zxy. b is a broadcast field, q, i, or vt.
    enum class Form : uint8_t
    {
        None,
        Add,
        Sub,
        Madd,
        Msub,
        Mul,
        OpMsub,
        OpMul,
        Max,
        Mini,
        Itof,
        Ftoi,
        Abs,
        Clip,
        Nop,
        Reserved
    };
    enum class Operand : uint8_t
    {
        None,
        Bc,
        Q,
        I,
        Vt
    };
    struct UpperOp
    {
        Form form = Form::Reserved;
        Operand operand = Operand::None;
        bool toAcc = false; // result goes to ACC, not FD
        bool toFt = false;  // result goes to FT (ITOF/FTOI/ABS)
        uint8_t scale = 0;  // ITOF/FTOI: 0, 4, 12, 15
    };

    constexpr UpperOp decodeUpper(uint32_t instr)
    {
        const uint8_t op = static_cast<uint8_t>(instr & 0x3Fu);
        UpperOp u{};
        if (op < 0x3Cu)
        {
            if (op <= 0x03u)
                return {Form::Add, Operand::Bc};
            if (op <= 0x07u)
                return {Form::Sub, Operand::Bc};
            if (op <= 0x0Bu)
                return {Form::Madd, Operand::Bc};
            if (op <= 0x0Fu)
                return {Form::Msub, Operand::Bc};
            if (op <= 0x13u)
                return {Form::Max, Operand::Bc};
            if (op <= 0x17u)
                return {Form::Mini, Operand::Bc};
            if (op <= 0x1Bu)
                return {Form::Mul, Operand::Bc};
            switch (op)
            {
            case 0x1C: return {Form::Mul, Operand::Q};
            case 0x1D: return {Form::Max, Operand::I};
            case 0x1E: return {Form::Mul, Operand::I};
            case 0x1F: return {Form::Mini, Operand::I};
            case 0x20: return {Form::Add, Operand::Q};
            case 0x21: return {Form::Madd, Operand::Q};
            case 0x22: return {Form::Add, Operand::I};
            case 0x23: return {Form::Madd, Operand::I};
            case 0x24: return {Form::Sub, Operand::Q};
            case 0x25: return {Form::Msub, Operand::Q};
            case 0x26: return {Form::Sub, Operand::I};
            case 0x27: return {Form::Msub, Operand::I};
            case 0x28: return {Form::Add, Operand::Vt};
            case 0x29: return {Form::Madd, Operand::Vt};
            case 0x2A: return {Form::Mul, Operand::Vt};
            case 0x2B: return {Form::Max, Operand::Vt};
            case 0x2C: return {Form::Sub, Operand::Vt};
            case 0x2D: return {Form::Msub, Operand::Vt};
            case 0x2E: return {Form::OpMsub, Operand::Vt};
            case 0x2F: return {Form::Mini, Operand::Vt};
            default: return u;
            }
        }
        const uint8_t special = static_cast<uint8_t>((instr & 3u) | ((instr >> 4) & 0x7Cu));
        UpperOp a{};
        a.toAcc = true;
        if (special <= 0x03u) { a.form = Form::Add; a.operand = Operand::Bc; return a; }
        if (special <= 0x07u) { a.form = Form::Sub; a.operand = Operand::Bc; return a; }
        if (special <= 0x0Bu) { a.form = Form::Madd; a.operand = Operand::Bc; return a; }
        if (special <= 0x0Fu) { a.form = Form::Msub; a.operand = Operand::Bc; return a; }
        if (special <= 0x13u)
        {
            UpperOp t{Form::Itof};
            t.toFt = true;
            t.scale = special == 0x10u ? 0 : special == 0x11u ? 4 : special == 0x12u ? 12 : 15;
            return t;
        }
        if (special <= 0x17u)
        {
            UpperOp t{Form::Ftoi};
            t.toFt = true;
            t.scale = special == 0x14u ? 0 : special == 0x15u ? 4 : special == 0x16u ? 12 : 15;
            return t;
        }
        if (special <= 0x1Bu) { a.form = Form::Mul; a.operand = Operand::Bc; return a; }
        switch (special)
        {
        case 0x1C: a.form = Form::Mul; a.operand = Operand::Q; return a;
        case 0x1D: { UpperOp t{Form::Abs}; t.toFt = true; return t; }
        case 0x1E: a.form = Form::Mul; a.operand = Operand::I; return a;
        case 0x1F: return {Form::Clip};
        case 0x20: a.form = Form::Add; a.operand = Operand::Q; return a;
        case 0x21: a.form = Form::Madd; a.operand = Operand::Q; return a;
        case 0x22: a.form = Form::Add; a.operand = Operand::I; return a;
        case 0x23: a.form = Form::Madd; a.operand = Operand::I; return a;
        case 0x24: a.form = Form::Sub; a.operand = Operand::Q; return a;
        case 0x25: a.form = Form::Msub; a.operand = Operand::Q; return a;
        case 0x26: a.form = Form::Sub; a.operand = Operand::I; return a;
        case 0x27: a.form = Form::Msub; a.operand = Operand::I; return a;
        case 0x28: a.form = Form::Add; a.operand = Operand::Vt; return a;
        case 0x29: a.form = Form::Madd; a.operand = Operand::Vt; return a;
        case 0x2A: a.form = Form::Mul; a.operand = Operand::Vt; return a;
        case 0x2C: a.form = Form::Sub; a.operand = Operand::Vt; return a;
        case 0x2D: a.form = Form::Msub; a.operand = Operand::Vt; return a;
        case 0x2E: a.form = Form::OpMul; a.operand = Operand::Vt; return a;
        case 0x2F:
        case 0x30: return {Form::Nop};
        default: return u;
        }
    }

    constexpr bool isFmac(Form form)
    {
        return form == Form::Add || form == Form::Sub || form == Form::Madd || form == Form::Msub ||
               form == Form::Mul || form == Form::OpMsub || form == Form::OpMul;
    }

    inline int32_t floatToInt(float value, float scale)
    {
        const double scaled = static_cast<double>(value) * static_cast<double>(scale);
        if (scaled >= static_cast<double>(std::numeric_limits<int32_t>::max()))
            return std::numeric_limits<int32_t>::max();
        if (scaled <= static_cast<double>(std::numeric_limits<int32_t>::min()))
            return std::numeric_limits<int32_t>::min();
        return static_cast<int32_t>(scaled);
    }

    // Four-lane operand normalisation (normalizeOperand): denormals become signed zero, Inf/NaN signed FLT_MAX.
    inline __m128 normalizeOperands(__m128 value)
    {
        const __m128i bits = _mm_castps_si128(value);
        const __m128i exponent = _mm_and_si128(bits, _mm_set1_epi32(0x7F800000));
        const __m128i sign = _mm_and_si128(bits, _mm_set1_epi32(static_cast<int>(0x80000000u)));
        __m128i result = _mm_blendv_epi8(bits, sign, _mm_cmpeq_epi32(exponent, _mm_setzero_si128()));
        result = _mm_blendv_epi8(result, _mm_or_si128(sign, _mm_set1_epi32(0x7F7FFFFF)),
                                 _mm_cmpeq_epi32(exponent, _mm_set1_epi32(0x7F800000)));
        return _mm_castsi128_ps(result);
    }

    // normalizeExact's classes on four lanes. Masks are in movemask order (bit c = component c).
    struct ExactClass
    {
        uint32_t sign, zero, under, over; // zero excludes underflow
    };

    // Movemask order (bit c = component c) to MAC lane order (bit 3 - c).
    inline constexpr uint8_t kReverse4[16] = {0x0, 0x8, 0x4, 0xC, 0x2, 0xA, 0x6, 0xE, 0x1, 0x9, 0x5, 0xD, 0x3, 0xB, 0x7, 0xF};

    // FMAC result and flags of upper instruction I, from the committed state (execUpper + applyFmacDest).
    // r receives all four lanes (lanes outside DEST are unspecified). Returns false when no flag entry is queued
    // (DEST = 0).
    //
    // Computed in double on four lanes: a product of two floats is exact in double, so the exact result (and the
    // flags taken from it, as calculateFmacExactResult) has one rounding, like the interpreter's long double (= double
    // on MSVC). The float result is the exact result rounded to float under the VU's round-toward-zero mode; for a
    // directed rounding mode the double-then-float rounding equals a single rounding, which is what the interpreter's
    // contracted (FMA) float arithmetic produces.
    // kFlags = false: the result only (fmacResult): for an instruction whose flags the compiler found are never
    // observed (every path overwrites them before any FMAND/FSAND or exit reads them).
    template <uint32_t I, bool kFlags>
    inline bool fmacCore(const VU1State &s, float r[4], VU1AotPending::Flag &flag)
    {
        constexpr UpperOp u = decodeUpper(I);
        constexpr uint8_t dest = static_cast<uint8_t>((I >> 21) & 0xFu);
        constexpr uint8_t fs = static_cast<uint8_t>((I >> 11) & 0x1Fu);
        constexpr uint8_t ft = static_cast<uint8_t>((I >> 16) & 0x1Fu);
        constexpr uint8_t bc = static_cast<uint8_t>(I & 3u);
        constexpr uint32_t destMask = kReverse4[dest]; // movemask order
        static_assert(isFmac(u.form));

        const __m128 vs = normalizeOperands(_mm_loadu_ps(s.vf[fs]));
        const __m128 vt = normalizeOperands(_mm_loadu_ps(s.vf[ft]));
        __m128 left = vs, right;
        if constexpr (u.form == Form::OpMsub || u.form == Form::OpMul)
        {
            left = _mm_shuffle_ps(vs, vs, _MM_SHUFFLE(3, 0, 2, 1));  // vs.yzx(w)
            right = _mm_shuffle_ps(vt, vt, _MM_SHUFFLE(3, 1, 0, 2)); // vt.zxy(w)
        }
        else if constexpr (u.operand == Operand::Bc)
            right = _mm_shuffle_ps(vt, vt, _MM_SHUFFLE(bc, bc, bc, bc));
        else if constexpr (u.operand == Operand::Q)
            right = normalizeOperands(_mm_set1_ps(s.q));
        else if constexpr (u.operand == Operand::I)
            right = normalizeOperands(_mm_set1_ps(s.i));
        else
            right = vt;

        const __m256d l = _mm256_cvtps_pd(left);
        const __m256d rr = _mm256_cvtps_pd(right);
        __m256d exact, product = _mm256_setzero_pd();
        if constexpr (u.form == Form::Add)
            exact = _mm256_add_pd(l, rr);
        else if constexpr (u.form == Form::Sub)
            exact = _mm256_sub_pd(l, rr);
        else
        {
            product = _mm256_mul_pd(l, rr); // exact
            if constexpr (u.form == Form::Madd)
                exact = _mm256_add_pd(_mm256_cvtps_pd(normalizeOperands(_mm_loadu_ps(s.acc))), product);
            else if constexpr (u.form == Form::Msub || u.form == Form::OpMsub)
                exact = _mm256_sub_pd(_mm256_cvtps_pd(normalizeOperands(_mm_loadu_ps(s.acc))), product);
            else
                exact = product;
            if constexpr (u.form == Form::OpMsub || u.form == Form::OpMul)
                exact = _mm256_blend_pd(exact, _mm256_setzero_pd(), 0x8); // w = 0
        }

        // Result: zero/underflow lanes become signed zero, overflow lanes signed FLT_MAX. Rounding toward zero (the
        // VU mode, set while VU1 runs) already gives signed FLT_MAX for an overflow and signed zero for an exact zero,
        // so only underflow lanes, which round to a denormal, need their magnitude cleared.
        const __m128 rounded = _mm256_cvtpd_ps(exact);
        const __m256d magnitude = _mm256_and_pd(exact, _mm256_castsi256_pd(_mm256_set1_epi64x(0x7FFFFFFFFFFFFFFFll)));
        const __m256d tinyLanes = _mm256_cmp_pd(magnitude, _mm256_set1_pd(static_cast<double>(std::numeric_limits<float>::min())),
                                                _CMP_LT_OQ); // zero or underflow
        const uint32_t tiny = static_cast<uint32_t>(_mm256_movemask_pd(tinyLanes));
        __m128 out = rounded;
        if ((tiny & destMask) != 0u)
        {
            const __m128 tiny32 = _mm_shuffle_ps(_mm_castpd_ps(_mm256_castpd256_pd128(tinyLanes)),
                                                 _mm_castpd_ps(_mm256_extractf128_pd(tinyLanes, 1)), _MM_SHUFFLE(2, 0, 2, 0));
            out = _mm_andnot_ps(_mm_and_ps(tiny32, _mm_castsi128_ps(_mm_set1_epi32(0x7FFFFFFF))), rounded);
        }
        _mm_storeu_ps(r, out);
        if constexpr (dest == 0u)
            return false;
        if constexpr (!kFlags)
        {
            flag.mac = 0u;
            flag.status = 0u;
            flag.extraSticky = 0u;
            flag.clip = 0u;
            return true;
        }
        ExactClass k;
        k.sign = static_cast<uint32_t>(_mm256_movemask_pd(exact));
        k.over = static_cast<uint32_t>(_mm256_movemask_pd(
            _mm256_cmp_pd(magnitude, _mm256_set1_pd(static_cast<double>(std::numeric_limits<float>::max())), _CMP_GT_OQ)));
        k.zero = tiny;
        k.under = 0u;
        if ((tiny & destMask) != 0u)
        {
            k.zero = static_cast<uint32_t>(_mm256_movemask_pd(_mm256_cmp_pd(magnitude, _mm256_setzero_pd(), _CMP_EQ_OQ)));
            k.under = tiny & ~k.zero;
        }

        // calculateFmacProductSticky: product conditions of the product-sum forms (all lanes, w included).
        uint32_t extraSticky = 0u;
        if constexpr (u.form == Form::Madd || u.form == Form::Msub || u.form == Form::OpMsub)
        {
            // As for the result: the zero test is needed only when a DEST lane is below FLT_MIN.
            const __m256d pMagnitude =
                _mm256_and_pd(product, _mm256_castsi256_pd(_mm256_set1_epi64x(0x7FFFFFFFFFFFFFFFll)));
            const uint32_t pTiny = static_cast<uint32_t>(_mm256_movemask_pd(_mm256_cmp_pd(
                                       pMagnitude, _mm256_set1_pd(static_cast<double>(std::numeric_limits<float>::min())),
                                       _CMP_LT_OQ))) &
                                   destMask;
            const uint32_t pSign = static_cast<uint32_t>(_mm256_movemask_pd(product)) & destMask;
            const uint32_t pOver = static_cast<uint32_t>(_mm256_movemask_pd(_mm256_cmp_pd(
                                       pMagnitude, _mm256_set1_pd(static_cast<double>(std::numeric_limits<float>::max())),
                                       _CMP_GT_OQ))) &
                                   destMask;
            uint32_t pUnder = 0u;
            if (pTiny != 0u)
                pUnder = pTiny & ~static_cast<uint32_t>(
                                     _mm256_movemask_pd(_mm256_cmp_pd(pMagnitude, _mm256_setzero_pd(), _CMP_EQ_OQ)));
            extraSticky = (pTiny ? 0x1u : 0u) | (pSign ? 0x2u : 0u) | (pUnder ? 0x4u : 0u) | (pOver ? 0x8u : 0u);
        }

        // updateFmacFlags (underflow also sets the zero flag).
        const uint32_t zero = (k.zero | k.under) & destMask, sign = k.sign & destMask, under = k.under & destMask,
                       over = k.over & destMask;
        flag.mac = static_cast<uint32_t>(kReverse4[zero]) | (static_cast<uint32_t>(kReverse4[sign]) << 4) |
                   (static_cast<uint32_t>(kReverse4[under]) << 8) | (static_cast<uint32_t>(kReverse4[over]) << 12);
        flag.status = (zero ? 0x1u : 0u) | (sign ? 0x2u : 0u) | (under ? 0x4u : 0u) | (over ? 0x8u : 0u);
        flag.extraSticky = extraSticky;
        flag.clip = 0u;
        return true;
    }

    template <uint32_t I>
    inline bool fmac(const VU1State &s, float r[4], VU1AotPending::Flag &flag)
    {
        return fmacCore<I, true>(s, r, flag);
    }

    // The same result as fmac<I>, without its MAC and status flags (see fmacCore).
    template <uint32_t I>
    inline bool fmacResult(const VU1State &s, float r[4], VU1AotPending::Flag &flag)
    {
        return fmacCore<I, false>(s, r, flag);
    }

    // MAX/MINI/ITOF/FTOI/ABS of upper instruction I (no flags).
    template <uint32_t I>
    inline void upperPlain(const VU1State &s, float r[4])
    {
        constexpr UpperOp u = decodeUpper(I);
        constexpr uint8_t fs = static_cast<uint8_t>((I >> 11) & 0x1Fu);
        constexpr uint8_t ft = static_cast<uint8_t>((I >> 16) & 0x1Fu);
        constexpr uint8_t bc = static_cast<uint8_t>(I & 3u);
        float vs[4], vt[4];
        for (uint32_t c = 0; c < 4u; ++c)
        {
            vs[c] = normalizeOperand(s.vf[fs][c]);
            vt[c] = normalizeOperand(s.vf[ft][c]);
        }
        auto b = [&](uint32_t c) -> float
        {
            if constexpr (u.operand == Operand::Bc)
                return normalizeOperand(vt[bc]);
            else if constexpr (u.operand == Operand::I)
                return normalizeOperand(s.i);
            else
                return vt[c];
        };
        for (uint32_t c = 0; c < 4u; ++c)
        {
            if constexpr (u.form == Form::Max)
                r[c] = (vs[c] > b(c)) ? vs[c] : b(c);
            else if constexpr (u.form == Form::Mini)
                r[c] = (vs[c] < b(c)) ? vs[c] : b(c);
            else if constexpr (u.form == Form::Abs)
                r[c] = std::fabs(vs[c]);
            else if constexpr (u.form == Form::Itof)
            {
                int32_t iv;
                std::memcpy(&iv, &s.vf[fs][c], 4);
                if constexpr (u.scale == 0)
                    r[c] = static_cast<float>(iv);
                else if constexpr (u.scale == 4)
                    r[c] = static_cast<float>(iv) / 16.0f;
                else if constexpr (u.scale == 12)
                    r[c] = static_cast<float>(iv) / 4096.0f;
                else
                    r[c] = static_cast<float>(iv) / 32768.0f;
            }
            else if constexpr (u.form == Form::Ftoi)
            {
                constexpr float scale = u.scale == 0 ? 1.0f : u.scale == 4 ? 16.0f : u.scale == 12 ? 4096.0f : 32768.0f;
                const int32_t iv = floatToInt(vs[c], scale);
                std::memcpy(&r[c], &iv, 4);
            }
        }
    }

    // CLIP judgement bits of upper instruction I (queueClip shifts them into the working clip flag).
    template <uint32_t I>
    inline uint32_t clip(const VU1State &s)
    {
        constexpr uint8_t fs = static_cast<uint8_t>((I >> 11) & 0x1Fu);
        constexpr uint8_t ft = static_cast<uint8_t>((I >> 16) & 0x1Fu);
        uint32_t wBits;
        std::memcpy(&wBits, &s.vf[ft][3], 4);
        const int32_t limit = (wBits & 0x7F800000u) != 0u ? static_cast<int32_t>(wBits & 0x7FFFFFFFu) : 0x007FFFFF;
        auto exceeds = [limit](float value, uint32_t signMask)
        {
            uint32_t bits;
            std::memcpy(&bits, &value, 4);
            bits ^= signMask;
            int32_t ordered;
            std::memcpy(&ordered, &bits, 4);
            return ordered > limit;
        };
        uint32_t flags = 0u;
        if (exceeds(s.vf[fs][0], 0x00000000u)) flags |= 0x01u;
        if (exceeds(s.vf[fs][0], 0x80000000u)) flags |= 0x02u;
        if (exceeds(s.vf[fs][1], 0x00000000u)) flags |= 0x04u;
        if (exceeds(s.vf[fs][1], 0x80000000u)) flags |= 0x08u;
        if (exceeds(s.vf[fs][2], 0x00000000u)) flags |= 0x10u;
        if (exceeds(s.vf[fs][2], 0x80000000u)) flags |= 0x20u;
        return flags;
    }

    // ---- lower-instruction helpers (execLower) ----
    inline uint32_t qwordAddress(int32_t vi, int32_t imm) { return (static_cast<uint32_t>(vi + imm) * 16u) & 0x3FFFu; }
    inline uint32_t qwordAddress(int32_t vi) { return (static_cast<uint32_t>(static_cast<uint16_t>(vi)) * 16u) & 0x3FFFu; }
    inline void loadQ(const uint8_t *data, uint32_t address, float out[4]) { std::memcpy(out, data + address, 16); }
    inline int32_t loadWord(const uint8_t *data, uint32_t address, uint32_t component)
    {
        uint32_t v;
        std::memcpy(&v, data + address + component * 4u, 4);
        return static_cast<int32_t>(static_cast<int16_t>(v & 0xFFFFu));
    }
    inline void storeVf(VU1AotPending::Store &store, uint32_t address, const float vf[4])
    {
        store.address = address;
        std::memcpy(store.words, vf, 16);
    }
    inline void storeVi(VU1AotPending::Store &store, uint32_t address, int32_t vi)
    {
        const uint32_t v = static_cast<uint32_t>(static_cast<uint16_t>(vi & 0xFFFF));
        store.address = address;
        store.words[0] = store.words[1] = store.words[2] = store.words[3] = v;
    }

    inline float divide(float num, float den, uint32_t &statusDi)
    {
        num = normalizeOperand(num);
        den = normalizeOperand(den);
        statusDi = 0u;
        float result;
        if (den == 0.0f)
        {
            statusDi = num == 0.0f ? 0x10u : 0x20u;
            result = std::signbit(num) != std::signbit(den) ? -std::numeric_limits<float>::max()
                                                            : std::numeric_limits<float>::max();
        }
        else
            result = num / den;
        uint32_t ignored;
        result = normalizeResult(result, ignored);
        return normalizeResult(result, ignored); // queueQ normalises again
    }
    inline float squareRoot(float value, uint32_t &statusDi)
    {
        value = normalizeOperand(value);
        statusDi = value < 0.0f ? 0x10u : 0u;
        uint32_t ignored;
        return normalizeResult(std::sqrt(std::fabs(value)), ignored);
    }
    inline float reciprocalSquareRoot(float num, float radicand, uint32_t &statusDi)
    {
        num = normalizeOperand(num);
        radicand = normalizeOperand(radicand);
        const float den = std::sqrt(std::fabs(radicand));
        statusDi = radicand < 0.0f ? 0x10u : 0u;
        float result;
        if (den != 0.0f)
            result = num / den;
        else
        {
            statusDi = num == 0.0f ? 0x10u : 0x20u;
            result = std::signbit(num) ? -std::numeric_limits<float>::max() : std::numeric_limits<float>::max();
        }
        uint32_t ignored;
        result = normalizeResult(result, ignored);
        return normalizeResult(result, ignored);
    }

    float efuAtan(float value);
    float efuSin(float value);
    float efuExp(float value);

    // EFU result of lower special op (0x70..0x7D), normalised as queueP does.
    template <uint8_t Special>
    inline float efu(const float vs[4], uint32_t component)
    {
        float result;
        const float x = normalizeOperand(vs[0]), y = normalizeOperand(vs[1]), z = normalizeOperand(vs[2]);
        if constexpr (Special == 0x70)
            result = x * x + y * y + z * z;
        else if constexpr (Special == 0x71)
        {
            const float sum = x * x + y * y + z * z;
            result = sum != 0.0f ? 1.0f / sum : sum;
        }
        else if constexpr (Special == 0x72)
            result = std::sqrt(x * x + y * y + z * z);
        else if constexpr (Special == 0x73)
        {
            const float len = std::sqrt(x * x + y * y + z * z);
            result = len != 0.0f ? 1.0f / len : len;
        }
        else if constexpr (Special == 0x74)
            result = x != 0.0f ? efuAtan(y / x) : 0.0f;
        else if constexpr (Special == 0x75)
            result = x != 0.0f ? efuAtan(z / x) : 0.0f;
        else if constexpr (Special == 0x76)
        {
            float sum = 0.0f;
            for (uint32_t c = 0; c < 4u; ++c)
                sum += normalizeOperand(vs[c]);
            result = sum;
        }
        else
        {
            const float value = normalizeOperand(vs[component]);
            if constexpr (Special == 0x77)
            {
                result = value;
                if (result >= 0.0f)
                {
                    result = std::sqrt(result);
                    if (result != 0.0f)
                        result = 1.0f / result;
                }
            }
            else if constexpr (Special == 0x78)
                result = value >= 0.0f ? std::sqrt(value) : value;
            else if constexpr (Special == 0x79)
                result = efuSin(value);
            else if constexpr (Special == 0x7A)
                result = value != 0.0f ? 1.0f / value : value;
            else if constexpr (Special == 0x7C)
                result = efuAtan(value);
            else
                result = efuExp(value);
        }
        uint32_t ignored;
        return normalizeResult(result, ignored);
    }
}
