#include <cstdint>
#include <bit>
#include <string_view>
#include <atomic>
#include "BigInt.hpp"

#if defined(_WIN32)
#define COLLATZ_API extern "C" __declspec(dllexport)
#else
#define COLLATZ_API extern "C" __attribute__((visibility("default")))
#endif

#if defined(__GNUC__) || defined(__clang__)
__extension__ using uint128 = unsigned __int128;
#else
using uint128 = uint64_t;
#endif

// Safe fast-path boundary: for numbers below 10^16 (10 Quadrillion),
// intermediate Collatz values are mathematically guaranteed never to exceed 2^64-1.
constexpr uint64_t FAST_PATH_LIMIT = 10000000000000000ULL;

// Watchdog sentinel returned when step count exceeds the configured limit
constexpr uint64_t WATCHDOG_TRIGGERED = 0xFFFFFFFFFFFFFFFFULL; // 2^64 - 1
inline std::atomic<uint64_t> g_watchdog_limit{10000000ULL};     // Default: 10 Million steps

COLLATZ_API void collatz_set_watchdog_limit(uint64_t limit) {
    g_watchdog_limit.store(limit > 0 ? limit : 10000000ULL, std::memory_order_relaxed);
}

COLLATZ_API uint64_t collatz_get_watchdog_limit() {
    return g_watchdog_limit.load(std::memory_order_relaxed);
}

// 1. Ultra-fast Branchless Odd Pipeline (Zero branch mispredictions, native BMI tzcnt)
inline uint64_t collatz_steps_branchless_odd(uint64_t n) noexcept {
    if (n <= 1) return 0;
    const uint64_t max_steps = g_watchdog_limit.load(std::memory_order_relaxed);
    int tz = std::countr_zero(n);
    n >>= tz;
    uint64_t steps = tz;
    while (n > 1) {
        uint64_t next_n = 3ULL * n + 1ULL;
        int z = std::countr_zero(next_n);
        n = next_n >> z;
        steps += 1 + z;
        if (steps >= max_steps) [[unlikely]] {
            return WATCHDOG_TRIGGERED;
        }
    }
    return steps;
}

// 2. Guarded 64-bit/128-bit Safe Loop (For high ranges n >= 10^16 up to 2^64-1)
inline uint64_t collatz_steps_safe_u64(uint64_t n) noexcept {
    if (n <= 1) return 0;
    const uint64_t max_steps = g_watchdog_limit.load(std::memory_order_relaxed);
    uint64_t steps = 0;
    while (n > 1) {
        if ((n & 1ULL) == 0ULL) {
            int zeros = std::countr_zero(n);
            n >>= zeros;
            steps += zeros;
        } else {
            if (n > (~0ULL - 1ULL) / 3ULL) {
                uint128 big_next = (static_cast<uint128>(3) * n) + 1;
                n = static_cast<uint64_t>(big_next >> 1);
            } else {
                n = 3ULL * n + 1ULL;
            }
            steps++;
        }
        if (steps >= max_steps) [[unlikely]] {
            return WATCHDOG_TRIGGERED;
        }
    }
    return steps;
}

inline uint64_t collatz_steps_bigint_impl(BigInt n) {
    if (n.is_one() || n.is_zero()) return 0;
    const uint64_t max_steps = g_watchdog_limit.load(std::memory_order_relaxed);
    uint64_t steps = 0;
    while (!n.is_one() && !n.is_zero()) {
        if (n.is_even()) {
            size_t zeros = n.count_trailing_zeros();
            n.shift_right(zeros);
            steps += zeros;
        } else {
            n.multiply_by_3_add_1();
            steps++;
        }
        if (steps >= max_steps) [[unlikely]] {
            return WATCHDOG_TRIGGERED;
        }
    }
    return steps;
}

// Single uint64 calculation with dual-path dispatch
COLLATZ_API uint64_t collatz_steps(uint64_t n) {
    if (n < FAST_PATH_LIMIT) {
        return collatz_steps_branchless_odd(n);
    }
    return collatz_steps_safe_u64(n);
}

// Arbitrary-precision BigInt calculation from decimal string
COLLATZ_API uint64_t collatz_steps_bigint(const char* str) {
    if (!str) return 0;
    try {
        BigInt val = BigInt::from_string(str);
        if (val.is_zero()) return 0;
        return collatz_steps_bigint_impl(val);
    } catch (...) {
        return 0;
    }
}

// Helper to increment BigInt by 1
inline void bigint_increment(BigInt& val) {
    uint64_t carry = 1;
    for (size_t i = 0; i < val.limbs.size(); ++i) {
        val.limbs[i] += carry;
        if (val.limbs[i] == 0) {
            carry = 1;
        } else {
            carry = 0;
            break;
        }
    }
    if (carry > 0) {
        val.limbs.push_back(1);
    }
}

// High-speed parallel batch calculation for 64-bit contiguous ranges [start, start + count - 1]
COLLATZ_API void collatz_compute_batch(uint64_t start, uint64_t count, uint64_t* out_steps) {
    if (!out_steps || count == 0) return;
    if (start + count < FAST_PATH_LIMIT) {
        #pragma omp parallel for schedule(static)
        for (int64_t i = 0; i < static_cast<int64_t>(count); ++i) {
            out_steps[i] = collatz_steps_branchless_odd(start + static_cast<uint64_t>(i));
        }
    } else {
        #pragma omp parallel for schedule(static)
        for (int64_t i = 0; i < static_cast<int64_t>(count); ++i) {
            out_steps[i] = collatz_steps_safe_u64(start + static_cast<uint64_t>(i));
        }
    }
}

// Generic OpenMP parallel batch reduction template
template <typename StepFn>
inline void collatz_compute_batch_fast_impl(
    uint64_t start,
    uint64_t count,
    uint64_t threshold_steps,
    uint64_t* out_qual_nums,
    uint64_t* out_qual_steps,
    uint64_t max_qual_capacity,
    uint64_t* out_qual_count,
    uint64_t* out_sum,
    uint64_t* out_max_steps,
    uint64_t* out_max_num,
    uint64_t* out_last_steps,
    StepFn&& step_fn
) {
    uint64_t total_sum = 0;
    uint64_t global_max_steps = 0;
    uint64_t global_max_num = start;
    uint64_t qual_count = 0;

    #pragma omp parallel
    {
        uint64_t local_sum = 0;
        uint64_t local_max_steps = 0;
        uint64_t local_max_num = start;

        #pragma omp for schedule(static) nowait
        for (int64_t i = 0; i < static_cast<int64_t>(count); ++i) {
            uint64_t cur_n = start + static_cast<uint64_t>(i);
            uint64_t s = step_fn(cur_n);
            if (s == WATCHDOG_TRIGGERED) [[unlikely]] {
                #pragma omp critical
                {
                    global_max_steps = WATCHDOG_TRIGGERED;
                    global_max_num = cur_n;
                }
                continue;
            }
            local_sum += s;
            if (s > local_max_steps) {
                local_max_steps = s;
                local_max_num = cur_n;
            }
            if (s >= threshold_steps && out_qual_nums && out_qual_steps) {
                #pragma omp critical
                {
                    if (qual_count < max_qual_capacity) {
                        out_qual_nums[qual_count] = cur_n;
                        out_qual_steps[qual_count] = s;
                        qual_count++;
                    }
                }
            }
        }

        #pragma omp critical
        {
            total_sum += local_sum;
            if (local_max_steps > global_max_steps) {
                global_max_steps = local_max_steps;
                global_max_num = local_max_num;
            }
        }
    }

    if (out_sum) *out_sum = total_sum;
    if (out_max_steps) *out_max_steps = global_max_steps;
    if (out_max_num) *out_max_num = global_max_num;
    if (out_qual_count) *out_qual_count = qual_count;
    if (out_last_steps) *out_last_steps = step_fn(start + count - 1);
}

// Ultra-fast parallel batch calculation with in-C++ sum reduction, max detection, and threshold filtering.
// Automatically routes to Branchless Odd loop (n < 10^16) or Safe 128-bit loop (n >= 10^16).
COLLATZ_API void collatz_compute_batch_fast(
    uint64_t start,
    uint64_t count,
    uint64_t threshold_steps,
    uint64_t* out_qual_nums,
    uint64_t* out_qual_steps,
    uint64_t max_qual_capacity,
    uint64_t* out_qual_count,
    uint64_t* out_sum,
    uint64_t* out_max_steps,
    uint64_t* out_max_num,
    uint64_t* out_last_steps
) {
    if (count == 0) return;

    if (start + count < FAST_PATH_LIMIT) {
        collatz_compute_batch_fast_impl(
            start, count, threshold_steps,
            out_qual_nums, out_qual_steps, max_qual_capacity,
            out_qual_count, out_sum, out_max_steps, out_max_num, out_last_steps,
            collatz_steps_branchless_odd
        );
    } else {
        collatz_compute_batch_fast_impl(
            start, count, threshold_steps,
            out_qual_nums, out_qual_steps, max_qual_capacity,
            out_qual_count, out_sum, out_max_steps, out_max_num, out_last_steps,
            collatz_steps_safe_u64
        );
    }
}

// Arbitrary-precision parallel batch calculation starting from any super-big decimal string
COLLATZ_API void collatz_compute_batch_bigint(const char* start_str, uint64_t count, uint64_t* out_steps) {
    if (!out_steps || count == 0 || !start_str) return;
    try {
        BigInt base = BigInt::from_string(start_str);
        // Pre-compute starting BigInt values for each slice
        std::vector<BigInt> slice_starts;
        slice_starts.reserve(count);
        BigInt cur = base;
        for (uint64_t i = 0; i < count; ++i) {
            slice_starts.push_back(cur);
            bigint_increment(cur);
        }

        #pragma omp parallel for schedule(dynamic)
        for (int64_t i = 0; i < static_cast<int64_t>(count); ++i) {
            out_steps[i] = collatz_steps_bigint_impl(slice_starts[i]);
        }
    } catch (...) {
        for (uint64_t i = 0; i < count; ++i) out_steps[i] = 0;
    }
}
