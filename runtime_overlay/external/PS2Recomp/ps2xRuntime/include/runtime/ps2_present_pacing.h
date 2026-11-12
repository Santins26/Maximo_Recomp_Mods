#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <algorithm>

// PS2X_PRESENT_PACING: how the window paces its presents. Unset (the default), raylib's own 60 Hz timer.
//   vblank   : once per guest VBlank, woken by the EE scheduler when it processes the VBlank.
//   deadline : once per guest VBlank, at the VBlank's host deadline on a high-resolution timer, so the window is
//              not delayed by the game thread's own wake-up latency. The frame for that VBlank is already published
//              (the guest publishes before it waits for the VBlank), so this adds no latency.
namespace ps2_present_pacing
{
    struct State
    {
        std::atomic<bool> enabled{false};
        std::atomic<int64_t> nextDeadlineNs{0}; // steady_clock time of the next VBlank's host deadline
        std::mutex mutex;
        std::condition_variable cv;
        uint64_t vblanks = 0;
    };

    inline State &state()
    {
        static State s;
        return s;
    }

    inline int64_t toNs(std::chrono::steady_clock::time_point t)
    {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(t.time_since_epoch()).count();
    }

    // Set by the presenter before the game thread starts.
    inline void enable() { state().enabled.store(true, std::memory_order_release); }

    // EE scheduler, at each VBlank start, with the host deadline it scheduled for the next one.
    inline void notifyVBlank(std::chrono::steady_clock::time_point nextDeadline)
    {
        State &s = state();
        if (!s.enabled.load(std::memory_order_acquire))
            return;
        {
            std::lock_guard<std::mutex> lock(s.mutex);
            ++s.vblanks;
            s.nextDeadlineNs.store(toNs(nextDeadline), std::memory_order_release);
        }
        s.cv.notify_one();
    }

    // Presenter: waits until a VBlank after `seen` (or the timeout), then records the latest one in `seen`.
    // Returns false on a timeout.
    inline bool waitVBlank(uint64_t &seen, std::chrono::milliseconds timeout)
    {
        State &s = state();
        std::unique_lock<std::mutex> lock(s.mutex);
        const bool woke = s.cv.wait_for(lock, timeout, [&]() { return s.vblanks != seen; });
        seen = s.vblanks;
        return woke;
    }

    // Sleeps until the steady_clock time `ns` on the platform's most precise sleeping timer (no busy wait).
    void sleepUntilNs(int64_t ns);

    // DISPLAY LOCK (deadline pacing with VSync): the guest's VBlank clock and the monitor's refresh are two clocks.
    // Even at the same nominal 60 Hz they drift (a monitor at 59.95 Hz, the field at 60.00), and each time the VBlank
    // deadline crosses the monitor's refresh a frame is shown twice or not at all; near that point it flips back and
    // forth for a while, which is uneven pacing. So while the field rate is within 2% of a whole multiple of the
    // monitor's refresh, the VBlanks follow the monitor: their period is the measured refresh (times that multiple),
    // and their phase is pulled gently (at most 2% of a refresh per field) to land a little before a refresh - early
    // enough for the window's upload and draw to make that refresh every time.
    struct Display
    {
        std::atomic<bool> vsync{false};         // the presenter swaps on the monitor's refresh (deadline pacing)
        std::atomic<int64_t> periodNs{0};       // measured refresh period
        std::atomic<int64_t> lastSwapNs{0};     // when the last swap returned (a refresh)
        std::atomic<int64_t> workNs{3000000};   // the presenter's upload + draw, smoothed
        std::atomic<bool> locked{false};
        std::atomic<uint64_t> lockedFields{0};
    };

    inline Display &display()
    {
        static Display d;
        return d;
    }

    // Presenter, once, with the monitor's nominal rate (0: unknown, measured from the swaps).
    inline void setDisplay(bool vsync, int refreshHz)
    {
        Display &d = display();
        d.vsync.store(vsync, std::memory_order_release);
        if (refreshHz > 0)
            d.periodNs.store(1000000000ll / refreshHz, std::memory_order_release);
    }

    // Presenter, after each swap: when it returned, and how long the upload and draw before it took.
    inline void noteSwap(int64_t ns, int64_t workNs)
    {
        Display &d = display();
        const int64_t last = d.lastSwapNs.exchange(ns, std::memory_order_acq_rel);
        int64_t period = d.periodNs.load(std::memory_order_relaxed);
        if (last != 0)
        {
            const int64_t interval = ns - last;
            if (period == 0 && interval > 4000000 && interval < 50000000)
                period = interval;
            else if (period != 0 && interval > period - period / 16 && interval < period + period / 16)
                period += (interval - period) / 64; // a single refresh: refine the estimate
            d.periodNs.store(period, std::memory_order_release);
        }
        if (workNs > 0 && workNs < 50000000)
        {
            const int64_t w = d.workNs.load(std::memory_order_relaxed);
            d.workNs.store(w + (workNs - w) / 16, std::memory_order_relaxed);
        }
    }

    // EE scheduler: the next VBlank's host deadline. `previous` is this VBlank's deadline, `fieldPeriod` the field
    // period; `unlocked` is the deadline the scheduler would use without the lock.
    inline std::chrono::steady_clock::time_point lockToDisplay(std::chrono::steady_clock::time_point previous,
                                                              std::chrono::nanoseconds fieldPeriod,
                                                              std::chrono::steady_clock::time_point unlocked)
    {
        Display &d = display();
        const int64_t refresh = d.periodNs.load(std::memory_order_acquire);
        const int64_t swap = d.lastSwapNs.load(std::memory_order_acquire);
        const int64_t field = fieldPeriod.count();
        bool lock = state().enabled.load(std::memory_order_relaxed) && d.vsync.load(std::memory_order_relaxed) &&
                    refresh > 0 && swap != 0 && field > 0;
        int64_t multiple = 0;
        if (lock)
        {
            multiple = (field + refresh / 2) / refresh;
            const int64_t diff = field - multiple * refresh;
            lock = multiple >= 1 && multiple <= 4 && (diff < 0 ? -diff : diff) * 50 < field;
        }
        d.locked.store(lock, std::memory_order_relaxed);
        if (!lock)
            return unlocked;
        d.lockedFields.fetch_add(1, std::memory_order_relaxed);
        int64_t next = toNs(previous) + multiple * refresh;
        // Land `margin` before a refresh: the presenter's work plus room for its timer.
        int64_t margin = d.workNs.load(std::memory_order_relaxed) * 3 / 2 + 1000000;
        margin = std::max<int64_t>(2000000, std::min<int64_t>(margin, refresh / 2));
        int64_t error = (next + margin - swap) % refresh; // 0: exactly `margin` before a refresh
        if (error < 0)
            error += refresh;
        if (error > refresh / 2)
            error -= refresh;
        const int64_t maxStep = refresh / 50;
        next -= std::max<int64_t>(-maxStep, std::min<int64_t>(maxStep, error / 8));
        return std::chrono::steady_clock::time_point(
            std::chrono::duration_cast<std::chrono::steady_clock::duration>(std::chrono::nanoseconds(next)));
    }
}
