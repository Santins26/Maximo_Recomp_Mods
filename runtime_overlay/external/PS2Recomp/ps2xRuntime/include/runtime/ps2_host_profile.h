#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>

// Host-time attribution for performance measurements. Each thread charges elapsed time to whichever category
// it is running, so nested work (a GS packet kicked from inside a VU1 program) is counted once, under the
// innermost category. Time outside any scope belongs to kEe.
namespace ps2_host_profile
{
    enum Category : uint32_t
    {
        kEe = 0,
        kVu1,
        kGs,
        kPresent, // latching and converting the displayed frame (main thread, or the GS worker when asynchronous)
        kGsWait,  // game thread blocked on the GS worker (queue full, or a synchronous GS entry point)
        kVu1Wait, // game thread blocked on the VU1 back-end worker (PS2X_VU1_THREAD=1)
        kCategoryCount
    };

    inline std::atomic<uint64_t> g_nanoseconds[kCategoryCount]{};

    inline uint64_t nowNs()
    {
        return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                                         std::chrono::steady_clock::now().time_since_epoch())
                                         .count());
    }

    struct ThreadState
    {
        Category current = kEe;
        uint64_t since = 0;
    };
    inline thread_local ThreadState t_state;

    inline void switchTo(Category next)
    {
        const uint64_t now = nowNs();
        if (t_state.since != 0 && t_state.current != kEe)
            g_nanoseconds[t_state.current].fetch_add(now - t_state.since, std::memory_order_relaxed);
        t_state.current = next;
        t_state.since = now;
    }

    class Scope
    {
    public:
        explicit Scope(Category category) : m_previous(t_state.current) { switchTo(category); }
        ~Scope() { switchTo(m_previous); }
        Scope(const Scope &) = delete;
        Scope &operator=(const Scope &) = delete;

    private:
        Category m_previous;
    };

    inline uint64_t total(Category category)
    {
        return g_nanoseconds[category].load(std::memory_order_relaxed);
    }

    // VBlank wait measurement: a caller marks when the guest starts waiting for VBlank, and the first VBlank
    // after that mark records when the wait was released.
    inline std::atomic<uint64_t> g_waitMarkNs{0};
    inline std::atomic<uint64_t> g_waitReleaseNs{0};

    // Fast forward while the game is not producing frames (loading): the game marks every finished frame with the
    // scheduler's field count (noteFrame); once that mark is g_fastForwardAfter fields old, the scheduler stops
    // pacing guest time to the host clock until the next frame. 0 = never (the default; the host program sets it).
    inline std::atomic<uint64_t> g_lastFrameTick{0};
    inline std::atomic<uint32_t> g_fastForwardAfter{0};
    inline std::atomic<uint64_t> g_fastForwardFields{0}; // fields that went by fast-forwarded (statistics)

    inline void noteFrame(uint64_t vsyncTick)
    {
        g_lastFrameTick.store(vsyncTick, std::memory_order_relaxed);
    }

    inline bool fastForward(uint64_t vsyncTick)
    {
        const uint32_t after = g_fastForwardAfter.load(std::memory_order_relaxed);
        if (after == 0u)
            return false;
        const uint64_t last = g_lastFrameTick.load(std::memory_order_relaxed);
        return vsyncTick > last && vsyncTick - last >= after;
    }

    inline void noteVBlank()
    {
        const uint64_t mark = g_waitMarkNs.load(std::memory_order_acquire);
        if (mark != 0 && g_waitReleaseNs.load(std::memory_order_relaxed) < mark)
            g_waitReleaseNs.store(nowNs(), std::memory_order_release);
    }
}
