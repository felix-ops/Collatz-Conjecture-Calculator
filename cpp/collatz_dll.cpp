#include <cstdint>
#include <bit>
#include <string_view>
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

inline uint64_t collatz_steps_u64(uint64_t n) noexcept {
    if (n <= 1) return 0;
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
    }
    return steps;
}

inline uint64_t collatz_steps_bigint_impl(BigInt n) {
    if (n.is_one() || n.is_zero()) return 0;
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
    }
    return steps;
}

// Single uint64 calculation
COLLATZ_API uint64_t collatz_steps(uint64_t n) {
    return collatz_steps_u64(n);
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
    #pragma omp parallel for schedule(static)
    for (int64_t i = 0; i < static_cast<int64_t>(count); ++i) {
        out_steps[i] = collatz_steps_u64(start + static_cast<uint64_t>(i));
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
