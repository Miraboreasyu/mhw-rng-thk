#pragma once
// ============================================================================
//  MIRA Companion SDK — Pure Header-Only Math, PRNG & SEH Memory Primitives
//  Zero runtime dependencies; compatible with MSVC x64 `/MT` `/EHsc`.
// ============================================================================
#include <windows.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <type_traits>

namespace mhw_sdk {

// ---- User-Mode Pointer Bounds & SEH-Safe Memory Readers ----
// Keep SEH functions strictly free of C++ objects requiring stack unwinding (MSVC C2712).
inline bool IsValidUserPtr(std::uintptr_t p, std::uintptr_t alignMask = 0x7ULL) noexcept {
    return p >= 0x100000ULL && p <= 0x00007FFFFFFF0000ULL && (p & alignMask) == 0ULL;
}

template <typename T>
inline bool TryReadPodSEH(std::uintptr_t addr, T& out, std::uintptr_t alignMask = 0ULL) noexcept {
    static_assert(std::is_trivially_copyable_v<T>, "TryReadPodSEH requires trivially copyable T");
    if (!IsValidUserPtr(addr, alignMask)) return false;
    __try {
        out = *reinterpret_cast<const T*>(addr);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

inline bool TryReadPtrSEH(std::uintptr_t addr,
                          std::uintptr_t& outPtr,
                          std::uintptr_t ptrAlignMask = 0x7ULL) noexcept {
    outPtr = 0ULL;
    std::uintptr_t val = 0ULL;
    if (!TryReadPodSEH<std::uintptr_t>(addr, val, 0x7ULL)) return false;
    if (!IsValidUserPtr(val, ptrAlignMask)) return false;
    outPtr = val;
    return true;
}

// ---- MHW:I 15.23 .rdata Float Constants & IEEE-754 Open-Interval Helpers ----
constexpr float         kInv2Pow32         = 1.0f / 4294967296.0f;          // 2^-32 (`0x142F12798`)
constexpr float         kInv2Pow32Times100 = 100.0f / 4294967296.0f;        // 100 * 2^-32 (`0x142F173C4`)
constexpr float         kProbEpsilon       = 1.192092896e-7f;             // FLT_EPSILON (`0x142E4FE48`)
constexpr std::uint32_t kMinSafeFloatU32   = 0x00000001U;                   // > 0.0f (skips weight == 0 rows)
constexpr std::uint32_t kMaxSafeFloatU32   = 0xFFFFFF00U;                   // < 1.0f / < 100.0f (no rounding overflow)

inline float U32ToFloat01(std::uint32_t u32) noexcept {
    return static_cast<float>(static_cast<std::int64_t>(static_cast<std::uint64_t>(u32))) * kInv2Pow32;
}

inline float U32ToFloat100(std::uint32_t u32) noexcept {
    return static_cast<float>(static_cast<std::int64_t>(static_cast<std::uint64_t>(u32))) * kInv2Pow32Times100;
}

// Maps `[0.0, 1.0]` strictly into open safe interval `[0x00000001U, 0xFFFFFF00U]`.
inline std::uint32_t FractionToOpenSafeU32(double frac0To1) noexcept {
    if (!(frac0To1 > 0.0)) return kMinSafeFloatU32;
    if (frac0To1 >= 1.0) return kMaxSafeFloatU32;
    const auto u = static_cast<std::uint64_t>(frac0To1 * 4294967296.0 + 0.5);
    if (u <= kMinSafeFloatU32) return kMinSafeFloatU32;
    if (u >= kMaxSafeFloatU32) return kMaxSafeFloatU32;
    return static_cast<std::uint32_t>(u);
}

// Maps `[0.0, 1.0]` into `[0U, allowExactOne ? 0xFFFFFFFFU : 0xFFFFFF00U]` for intra-tier `[min, max]` scaling.
inline std::uint32_t FractionToTierU32(double frac0To1, bool allowExactOne) noexcept {
    if (!(frac0To1 > 0.0)) return 0U;
    if (frac0To1 >= 1.0) return allowExactOne ? 0xFFFFFFFFU : kMaxSafeFloatU32;
    const auto u = static_cast<std::uint64_t>(frac0To1 * 4294967296.0 + 0.5);
    if (u >= kMaxSafeFloatU32) return allowExactOne ? static_cast<std::uint32_t>((std::min<std::uint64_t>)(u, 0xFFFFFFFFULL)) : kMaxSafeFloatU32;
    return static_cast<std::uint32_t>(u);
}

// Maps `[0.0, 100.0]` strictly into `[kMinSafeFloatU32, kMaxSafeFloatU32]` (`[1U, 0xFFFFFF00U]`).
inline std::uint32_t PercentToOpenSafeU32(double pct0To100) noexcept {
    if (!(pct0To100 > 0.0)) return kMinSafeFloatU32;
    if (pct0To100 >= 100.0) return kMaxSafeFloatU32;
    const double scaled = (pct0To100 / 100.0) * static_cast<double>(kMaxSafeFloatU32);
    const auto u = static_cast<std::uint64_t>(scaled + 0.5);
    if (u <= kMinSafeFloatU32) return kMinSafeFloatU32;
    if (u >= kMaxSafeFloatU32) return kMaxSafeFloatU32;
    return static_cast<std::uint32_t>(u);
}

// Adapts a `[0.0, 100.0]` roll to cumulative weight tables whose positive weights sum != 100.
inline std::uint32_t AdaptPercentToWeightSum(double pct0To100, std::uint32_t totalWeight) noexcept {
    if (totalWeight == 0U || totalWeight == 100U) {
        return PercentToOpenSafeU32(pct0To100);
    }
    const double maxSafeRoll = (std::min)(100.0, static_cast<double>(totalWeight) * 0.99999);
    const double targetRoll  = (pct0To100 / 100.0) * maxSafeRoll;
    return PercentToOpenSafeU32(targetRoll);
}

// Inverse solver for `FUN_140672f90` (`minVal + float(u32) * 2^-32 * (maxVal - minVal)`).
inline std::uint32_t RandRangeToSafeU32(float target, float minVal, float maxVal) noexcept {
    const double span = static_cast<double>(maxVal) - static_cast<double>(minVal);
    if (!(span > 0.0)) return kMinSafeFloatU32;
    const double frac = (static_cast<double>(target) - static_cast<double>(minVal)) / span;
    return FractionToOpenSafeU32(frac);
}

// ---- MHW Xorshift128 + SplitMix64 Deterministic Substream Helpers ----
inline std::uint64_t SplitMix64(std::uint64_t x) noexcept {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

inline void DeriveMhwXorshift128(std::uint64_t baseSeed,
                                 std::uint32_t rva,
                                 std::uint32_t condId,
                                 std::uint32_t outState[4]) noexcept {
    const std::uint64_t comp = (static_cast<std::uint64_t>(condId) << 32) | rva;
    const std::uint64_t mix1 = SplitMix64(baseSeed ^ (comp * 0xD6E8FEB86659FD93ULL));
    const std::uint64_t mix2 = SplitMix64(mix1 ^ (comp + 0x9E3779B97F4A7C15ULL));
    outState[0] = static_cast<std::uint32_t>(mix1);
    outState[1] = static_cast<std::uint32_t>(mix1 >> 32);
    outState[2] = static_cast<std::uint32_t>(mix2);
    outState[3] = static_cast<std::uint32_t>(mix2 >> 32);
    if ((outState[0] | outState[1] | outState[2] | outState[3]) == 0U) {
        outState[0] = 0x13579BDFU;
        outState[3] = 0x2468ACE1U;
    }
}

inline std::uint32_t StepMhwXorshift128(std::uint32_t s[4]) noexcept {
    std::uint32_t t = (s[0] << 15) ^ s[0];
    s[0] = s[1];
    s[1] = s[2];
    const std::uint32_t w = s[3];
    s[2] = w;
    t = ((w >> 17) ^ t) >> 4 ^ w ^ t;
    s[3] = t;
    return t;
}

// Lightweight local Xorshift128 for Selective-mode filtered draws (avoids repeating across boots).
struct LocalXorshift128 {
    std::uint32_t s[4]{0x13572468U, 0x24681357U, 0x89ABCDEFU, 0xFEDCBA98U};

    void Seed(std::uint64_t seed) noexcept {
        std::uint64_t z = seed ^ 0x9E3779B97F4A7C15ULL;
        for (int i = 0; i < 4; ++i) {
            z += 0x9E3779B97F4A7C15ULL;
            s[i] = static_cast<std::uint32_t>(SplitMix64(z) | 1ULL);
        }
    }

    void SeedFromClock() noexcept {
        LARGE_INTEGER qpc{};
        ::QueryPerformanceCounter(&qpc);
        const std::uint64_t mix = static_cast<std::uint64_t>(qpc.QuadPart) ^
                                  ::GetTickCount64() ^
                                  (static_cast<std::uint64_t>(::GetCurrentProcessId()) << 32);
        Seed(mix);
    }

    std::uint32_t Next() noexcept {
        std::uint32_t t = s[0] ^ (s[0] << 11);
        s[0] = s[1];
        s[1] = s[2];
        s[2] = s[3];
        s[3] = (s[3] ^ (s[3] >> 19)) ^ (t ^ (t >> 8));
        return s[3];
    }
};

} // namespace mhw_sdk
