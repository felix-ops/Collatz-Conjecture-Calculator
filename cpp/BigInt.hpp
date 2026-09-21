#pragma once

#include <iostream>
#include <vector>
#include <string>
#include <string_view>
#include <cstdint>
#include <bit>
#include <algorithm>
#include <stdexcept>

#if defined(__GNUC__) || defined(__clang__)
__extension__ using uint128_t = unsigned __int128;
#define BIGINT_HAS_NATIVE_128 1
#elif defined(_MSC_VER) && (defined(_M_X64) || defined(_M_ARM64))
#include <intrin.h>
#define BIGINT_HAS_NATIVE_128 0
#else
#define BIGINT_HAS_NATIVE_128 0
#endif

class BigInt {
public:
    std::vector<uint64_t> limbs; // Little-endian: limbs[0] is the least significant 64 bits

    BigInt() : limbs{0} {}
    BigInt(uint64_t v) : limbs{v} {}

    static BigInt from_string(std::string_view s) {
        // Trim leading and trailing whitespace
        while (!s.empty() && (s.front() == ' ' || s.front() == '\t' || s.front() == '\r' || s.front() == '\n')) {
            s.remove_prefix(1);
        }
        while (!s.empty() && (s.back() == ' ' || s.back() == '\t' || s.back() == '\r' || s.back() == '\n')) {
            s.remove_suffix(1);
        }

        if (s.empty()) {
            throw std::invalid_argument("Input string is empty.");
        }

        if (s.front() == '+') {
            s.remove_prefix(1);
        } else if (s.front() == '-') {
            throw std::invalid_argument("Collatz Conjecture requires positive integers (negative number provided).");
        }

        if (s.empty()) {
            throw std::invalid_argument("Input string contains no digits.");
        }

        for (char c : s) {
            if (c < '0' || c > '9') {
                throw std::invalid_argument(std::string("Invalid non-digit character '") + c + "' in input.");
            }
        }

        // Skip leading zeros
        while (s.size() > 1 && s.front() == '0') {
            s.remove_prefix(1);
        }

        BigInt res;
        res.limbs.clear(); // start empty for construction

        // Parse chunks of up to 18 digits (10^18 < 2^64 - 1)
        size_t idx = 0;
        size_t len = s.size();
        size_t first_chunk = len % 18;
        if (first_chunk == 0 && len > 0) first_chunk = 18;

        auto parse_u64 = [](std::string_view chunk) -> uint64_t {
            uint64_t v = 0;
            for (char c : chunk) {
                v = v * 10ULL + static_cast<uint64_t>(c - '0');
            }
            return v;
        };

        static const uint64_t POW10_18 = 1'000'000'000'000'000'000ULL;

        if (first_chunk > 0) {
            res.limbs.push_back(parse_u64(s.substr(0, first_chunk)));
            idx += first_chunk;
        }

        while (idx < len) {
            uint64_t chunk_val = parse_u64(s.substr(idx, 18));
            res.multiply_add(POW10_18, chunk_val);
            idx += 18;
        }

        res.trim();
        if (res.limbs.empty()) res.limbs.push_back(0);
        return res;
    }

    [[nodiscard]] bool is_zero() const noexcept {
        return limbs.empty() || (limbs.size() == 1 && limbs[0] == 0);
    }

    [[nodiscard]] bool is_one() const noexcept {
        return limbs.size() == 1 && limbs[0] == 1;
    }

    [[nodiscard]] bool is_even() const noexcept {
        return !is_zero() && ((limbs[0] & 1ULL) == 0ULL);
    }

    [[nodiscard]] size_t count_trailing_zeros() const noexcept {
        if (is_zero()) return 0;
        size_t zeros = 0;
        for (uint64_t limb : limbs) {
            if (limb != 0) {
                zeros += std::countr_zero(limb);
                break;
            }
            zeros += 64;
        }
        return zeros;
    }

    // In-place bitwise right shift: n >>= shift
    void shift_right(size_t shift) noexcept {
        if (shift == 0 || is_zero()) return;
        size_t limb_shift = shift / 64;
        size_t bit_shift = shift % 64;

        if (limb_shift >= limbs.size()) {
            limbs.assign(1, 0);
            return;
        }

        size_t new_size = limbs.size() - limb_shift;
        if (bit_shift == 0) {
            for (size_t i = 0; i < new_size; ++i) {
                limbs[i] = limbs[i + limb_shift];
            }
        } else {
            for (size_t i = 0; i < new_size; ++i) {
                uint64_t low = limbs[i + limb_shift] >> bit_shift;
                uint64_t high = (i + limb_shift + 1 < limbs.size())
                    ? (limbs[i + limb_shift + 1] << (64 - bit_shift))
                    : 0ULL;
                limbs[i] = low | high;
            }
        }
        limbs.resize(new_size);
        trim();
    }

    // High-speed in-place Collatz odd step: n = 3n + 1
    void multiply_by_3_add_1() {
#if BIGINT_HAS_NATIVE_128
        uint128_t carry = 1;
        for (size_t i = 0; i < limbs.size(); ++i) {
            uint128_t prod = static_cast<uint128_t>(limbs[i]) * 3 + carry;
            limbs[i] = static_cast<uint64_t>(prod);
            carry = prod >> 64;
        }
        if (carry > 0) {
            limbs.push_back(static_cast<uint64_t>(carry));
        }
#else
        uint64_t carry = 1;
        for (size_t i = 0; i < limbs.size(); ++i) {
            uint64_t low, high;
#if defined(_MSC_VER) && (defined(_M_X64) || defined(_M_ARM64))
            low = _umul128(limbs[i], 3, &high);
#else
            uint64_t a = limbs[i] & 0xFFFFFFFFULL;
            uint64_t b = limbs[i] >> 32;
            uint64_t p0 = a * 3;
            uint64_t p1 = b * 3 + (p0 >> 32);
            low = (p0 & 0xFFFFFFFFULL) | (p1 << 32);
            high = p1 >> 32;
#endif
            low += carry;
            if (low < carry) {
                high++;
            }
            limbs[i] = low;
            carry = high;
        }
        if (carry > 0) {
            limbs.push_back(carry);
        }
#endif
    }

    // Multiply this BigInt by uint64_t mult and add uint64_t add
    void multiply_add(uint64_t mult, uint64_t add) {
        if (limbs.empty()) {
            if (add > 0) limbs.push_back(add);
            return;
        }
#if BIGINT_HAS_NATIVE_128
        uint128_t carry = add;
        for (size_t i = 0; i < limbs.size(); ++i) {
            uint128_t prod = static_cast<uint128_t>(limbs[i]) * mult + carry;
            limbs[i] = static_cast<uint64_t>(prod);
            carry = prod >> 64;
        }
        while (carry > 0) {
            limbs.push_back(static_cast<uint64_t>(carry));
            carry >>= 64;
        }
#else
        uint64_t carry = add;
        for (size_t i = 0; i < limbs.size(); ++i) {
            uint64_t low, high;
#if defined(_MSC_VER) && (defined(_M_X64) || defined(_M_ARM64))
            low = _umul128(limbs[i], mult, &high);
#else
            uint64_t a_lo = limbs[i] & 0xFFFFFFFFULL;
            uint64_t a_hi = limbs[i] >> 32;
            uint64_t b_lo = mult & 0xFFFFFFFFULL;
            uint64_t b_hi = mult >> 32;

            uint64_t p0 = a_lo * b_lo;
            uint64_t p1 = a_lo * b_hi;
            uint64_t p2 = a_hi * b_lo;
            uint64_t p3 = a_hi * b_hi;

            uint64_t mid = p1 + (p0 >> 32);
            mid += p2;
            if (mid < p2) p3 += (1ULL << 32);

            low = (p0 & 0xFFFFFFFFULL) | (mid << 32);
            high = p3 + (mid >> 32);
#endif
            low += carry;
            if (low < carry) {
                high++;
            }
            limbs[i] = low;
            carry = high;
        }
        if (carry > 0) {
            limbs.push_back(carry);
        }
#endif
    }

    // In-place division by a 64-bit divisor, returns remainder
    uint64_t divide_scalar(uint64_t divisor) {
        if (divisor == 0) throw std::domain_error("Division by zero");
        uint64_t remainder = 0;
#if BIGINT_HAS_NATIVE_128
        for (size_t i = limbs.size(); i-- > 0; ) {
            uint128_t current = (static_cast<uint128_t>(remainder) << 64) | limbs[i];
            limbs[i] = static_cast<uint64_t>(current / divisor);
            remainder = static_cast<uint64_t>(current % divisor);
        }
#else
        for (size_t i = limbs.size(); i-- > 0; ) {
            uint64_t high = remainder;
            uint64_t low = limbs[i];
#if defined(_MSC_VER) && (defined(_M_X64) || defined(_M_ARM64))
            uint64_t rem;
            limbs[i] = _udiv128(high, low, divisor, &rem);
            remainder = rem;
#else
            uint64_t cur_hi = (high << 32) | (low >> 32);
            uint64_t q_hi = cur_hi / divisor;
            high = cur_hi % divisor;

            uint64_t cur_lo = (high << 32) | (low & 0xFFFFFFFFULL);
            uint64_t q_lo = cur_lo / divisor;
            remainder = cur_lo % divisor;
            limbs[i] = (q_hi << 32) | q_lo;
#endif
        }
#endif
        trim();
        return remainder;
    }

    [[nodiscard]] std::string to_string() const {
        if (is_zero()) return "0";
        BigInt temp = *this;
        std::vector<uint64_t> chunks;
        static const uint64_t BASE = 1'000'000'000'000'000'000ULL; // 10^18

        while (!temp.is_zero()) {
            uint64_t rem = temp.divide_scalar(BASE);
            chunks.push_back(rem);
        }

        std::string result = std::to_string(chunks.back());
        for (size_t i = chunks.size() - 1; i-- > 0; ) {
            std::string s = std::to_string(chunks[i]);
            result.append(18 - s.length(), '0');
            result.append(s);
        }
        return result;
    }

    bool operator<(const BigInt& other) const noexcept {
        if (limbs.size() != other.limbs.size()) {
            return limbs.size() < other.limbs.size();
        }
        for (size_t i = limbs.size(); i-- > 0; ) {
            if (limbs[i] != other.limbs[i]) {
                return limbs[i] < other.limbs[i];
            }
        }
        return false;
    }

    bool operator>(const BigInt& other) const noexcept {
        return other < *this;
    }

    bool operator==(const BigInt& other) const noexcept {
        return limbs == other.limbs;
    }

    bool operator!=(const BigInt& other) const noexcept {
        return !(*this == other);
    }

private:
    void trim() noexcept {
        while (limbs.size() > 1 && limbs.back() == 0) {
            limbs.pop_back();
        }
    }
};
