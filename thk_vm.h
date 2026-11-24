#pragma once
// ============================================================================
//  MHW 15.23 THK VM Reader & Pure-RNG Math (`thk_vm.h`)
//  100% Monster-Agnostic: reads `cThinkMgr::updateThk` (`0x1339C8C`) context,
//  resolves 1-hop local wrapper nodes (`seg + 0x30`), unwinds Frame 0 jump
//  stack (`mgr + 0x200` / `+0x340`), solves midpoint `uint32_t` targets, and
//  simulates vanilla weighted rolls identically to `cThinkMgr::updateThk`.
// ============================================================================
#include "mhw_rng_api.h"
#include "mhw_rng_math.h"
#include "mhw_1523_world.h"

#include <windows.h>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace thk {

// Verified MHW:I 15.23 (Steam 15539686, ImageBase 0x140000000) RVAs & Offsets:
constexpr std::uint32_t  kRvaThkUpdateRandom    = 0x1339C8CU; // `cThinkMgr::updateThk` `CALL MtRandom::nrand`
constexpr std::uintptr_t kRvaCallerEmAiTick     = 0x132B83DU; // `cThinkEm::update` @ 0x14132B7B0 (primary large-monster AI tick)
constexpr std::uintptr_t kRvaCallerAiTick       = 0x133B6F0U; // `FUN_14133b640(cThinkEm*)` (secondary AI tick)
constexpr std::uintptr_t kRvaCallerPrefetchTick = 0x133A876U; // `FUN_14133a850(cThinkMgr*)` (prefetch AI tick)
constexpr std::uintptr_t kRvaCThinkMgrVft       = 0x324F438U; // `cThinkMgr::vft`
constexpr std::uintptr_t kRvaCThinkEmVft        = 0x324F320U; // `cThinkEm::vft`

constexpr std::uintptr_t kOffMonsterId          = mhw1523::kOffEnemyMonsterId; // `uint32_t` (`em` ID: e.g. 101=Fatalis, 91=Rajang)
constexpr std::uintptr_t kOffThinkEmToMgr       = 0x20U;
constexpr std::uintptr_t kOffThinkEmMonster     = 0x08U;
constexpr std::uintptr_t kOffMgrCandidates      = 0x40U;      // `ThkCandidate[30]` (`{ int32_t segIdx; float weight; }`)
constexpr std::uintptr_t kOffMgrFrame0JumpStack = 0x200U;     // `20 * 0x10` (`{ uintptr_t nodeBase; uint64_t segIdx; }`)
constexpr std::uintptr_t kOffMgrFrame0JumpCount = 0x340U;     // `int32_t` return stack count (0..20)
constexpr std::uintptr_t kOffMgrFrameBase       = 0x180U;     // Stack frame stride (`d * 0x180`)
constexpr std::uintptr_t kOffMgrNodeBase        = 0x350U;     // `frameBase + 0x350`: `THKSegment*`
constexpr std::uintptr_t kOffMgrThkFileIdx      = 0x35CU;     // `frameBase + 0x35C`: `int32_t` (`0` = `_00`, `55` = `_55`, ...)
constexpr std::uintptr_t kOffMgrCallerNodeIdx   = 0x364U;     // Frame 0 caller node array idx when `depth == 1`
constexpr std::uintptr_t kOffMgrDepth           = 0x4F8U;     // `uint32_t` call stack depth (0 or 1)
constexpr std::uintptr_t kOffMgrMonster         = 0x508U;     // `Monster*` cached on `cThinkMgr`
constexpr std::uintptr_t kThkSegStride          = 0x80U;      // 128 bytes per `THKSegment`

constexpr std::size_t    kMaxThkCandidates      = 30U;
constexpr std::size_t    kMaxSegmentsPerNode    = 256U;
constexpr std::size_t    kMaxCallerChain        = 8U;

// Open float interval `(0.0f, 1.0f)` bounds for `cThinkMgr::updateThk` (`acc <= roll && roll < acc + weight[k]`):
using mhw_sdk::kMinSafeFloatU32;
using mhw_sdk::kMaxSafeFloatU32;

// Control-flow kind bits in `seg + 0x00` (`ctrlFlags & 0xC0`):
constexpr std::uint32_t  kCtrlKindMask          = 0xC0U;
constexpr std::uint32_t  kCtrlRandom            = 0x40U;
constexpr std::uint32_t  kCtrlEndr              = 0x80U;
constexpr std::uint32_t  kCtrlElser             = 0xC0U;

#pragma pack(push, 1)
struct RawThkCandidate {
    std::int32_t segIdx;
    float        weight;
};
#pragma pack(pop)
static_assert(sizeof(RawThkCandidate) == 8U, "RawThkCandidate must be 8 bytes");

struct ThkCandidateOption {
    std::int32_t  segIdx;
    float         weight;
    std::uint32_t ctrlFlags;
    std::int32_t  targetThkFile;
    std::int32_t  targetNodeId;
    bool          resolvedViaWrapper;
};

enum class ThkReadFailReason : std::uint8_t {
    kNone           = 0,
    kBadCaller      = 1,
    kBadMgrVft      = 2,
    kBadMonsterPtr  = 3,
    kBadDepthOrNode = 4,
    kBadCandidates  = 5
};

// Strictly POD/aggregate so MSVC `/EHsc` allows SEH `__try/__except` in `ReadThkVmContextSEH`.
struct ThkVmContext {
    bool               isValid              = false;
    ThkReadFailReason  failReason           = ThkReadFailReason::kNone;
    std::uint32_t      observedCallerRva    = 0U;
    std::uintptr_t     monsterPtr           = 0U;
    std::uint32_t      monsterId            = 0U;
    std::uint32_t      depth                = 0U;
    std::int32_t       thkFileIdx           = -1;
    std::int32_t       nodeArrIdx           = -1;
    std::int32_t       callerNodeIdx        = -1; // Direct `mgr + 0x364` when `depth == 1`
    std::int32_t       unwoundCallerNodeIdx = -1; // Furthest non-null Frame 0 return-stack node (or `callerNodeIdx`)
    std::size_t        callerChainCount     = 0U;
    std::int32_t       callerChain[kMaxCallerChain]{};
    std::size_t        candCount            = 0U;
    float              totalWeight          = 0.0f;
    ThkCandidateOption cands[kMaxThkCandidates]{};
};

using mhw_sdk::IsValidUserPtr;
using mhw_sdk::FractionToOpenSafeU32;

// Locate node END sentinel (`seg[0] & 1`) to recover exact `nodeArrIdx` (`endVal - 10000`).
inline std::int32_t ResolveNodeArrIdx(std::uintptr_t nodeBase) noexcept {
    if (!IsValidUserPtr(nodeBase)) return -1;
    for (std::size_t s = 0U; s < kMaxSegmentsPerNode; ++s) {
        const std::uintptr_t segAddr = nodeBase + s * kThkSegStride;
        if ((*reinterpret_cast<const std::uint8_t*>(segAddr) & 0x01U) != 0U) {
            return *reinterpret_cast<const std::int32_t*>(segAddr + 0x24U) - 10000;
        }
    }
    return -1;
}

inline void AppendUniqueCaller(ThkVmContext& out, std::int32_t idx) noexcept {
    if (idx < 0 || out.callerChainCount >= kMaxCallerChain) return;
    for (std::size_t i = 0U; i < out.callerChainCount; ++i) {
        if (out.callerChain[i] == idx) return;
    }
    out.callerChain[out.callerChainCount++] = idx;
}

inline bool ReadThkVmContextSEH(const MhwRngDrawContext* ctx, ThkVmContext& out) noexcept {
    out = ThkVmContext{};
    if (!ctx || !ctx->returnAddrSlot || !ctx->exeBase) {
        out.failReason = ThkReadFailReason::kBadCaller;
        return false;
    }
    __try {
        const auto* slotPtr = static_cast<const std::uintptr_t*>(ctx->returnAddrSlot);
        const std::uintptr_t callerRet = slotPtr[20];
        if (callerRet >= ctx->exeBase && callerRet < ctx->exeEnd) {
            out.observedCallerRva = static_cast<std::uint32_t>(callerRet - ctx->exeBase);
        }

        std::uintptr_t mgr     = 0U;
        std::uintptr_t thinkEm = 0U;
        std::uintptr_t monster = 0U;

        if (callerRet == (ctx->exeBase + kRvaCallerEmAiTick)) {
            thinkEm = slotPtr[23]; // `updateThk` saved caller `RSI` (`MOV [RSP+0xB0], RSI`)
            mgr     = thinkEm + kOffThinkEmToMgr;
            monster = slotPtr[19]; // `updateThk` pushed caller `RDI` (`PUSH RDI`)
        } else if (callerRet == (ctx->exeBase + kRvaCallerAiTick)) {
            thinkEm = slotPtr[19]; // caller `RDI`
            mgr     = thinkEm + kOffThinkEmToMgr;
            monster = slotPtr[21]; // caller `RBX`
        } else if (callerRet == (ctx->exeBase + kRvaCallerPrefetchTick)) {
            mgr     = slotPtr[21]; // caller `RBX`
            thinkEm = (mgr >= kOffThinkEmToMgr) ? (mgr - kOffThinkEmToMgr) : 0U;
        } else {
            out.failReason = ThkReadFailReason::kBadCaller;
            return false;
        }

        if (!IsValidUserPtr(mgr) ||
            *reinterpret_cast<const std::uintptr_t*>(mgr) != (ctx->exeBase + kRvaCThinkMgrVft)) {
            out.failReason = ThkReadFailReason::kBadMgrVft;
            return false;
        }

        if (!IsValidUserPtr(monster)) {
            monster = *reinterpret_cast<const std::uintptr_t*>(mgr + kOffMgrMonster);
        }
        if (!IsValidUserPtr(monster) && IsValidUserPtr(thinkEm) &&
            *reinterpret_cast<const std::uintptr_t*>(thinkEm) == (ctx->exeBase + kRvaCThinkEmVft)) {
            monster = *reinterpret_cast<const std::uintptr_t*>(thinkEm + kOffThinkEmMonster);
        }
        if (!IsValidUserPtr(monster)) {
            out.failReason = ThkReadFailReason::kBadMonsterPtr;
            return false;
        }

        out.monsterPtr = monster;
        out.monsterId  = *reinterpret_cast<const std::uint32_t*>(monster + kOffMonsterId);

        const std::uint32_t depth = *reinterpret_cast<const std::uint32_t*>(mgr + kOffMgrDepth);
        if (depth > 1U) {
            out.failReason = ThkReadFailReason::kBadDepthOrNode;
            return false;
        }
        out.depth = depth;

        const std::uintptr_t frameBase = mgr + static_cast<std::uintptr_t>(depth) * kOffMgrFrameBase;
        const std::uintptr_t nodeBase  = *reinterpret_cast<const std::uintptr_t*>(frameBase + kOffMgrNodeBase);
        if (!IsValidUserPtr(nodeBase)) {
            out.failReason = ThkReadFailReason::kBadDepthOrNode;
            return false;
        }

        out.thkFileIdx = *reinterpret_cast<const std::int32_t*>(frameBase + kOffMgrThkFileIdx);
        if (depth == 1U) {
            const std::int32_t directCaller = *reinterpret_cast<const std::int32_t*>(mgr + kOffMgrCallerNodeIdx);
            out.callerNodeIdx        = directCaller;
            out.unwoundCallerNodeIdx = directCaller;
            AppendUniqueCaller(out, directCaller);

            // Monster-agnostic unwind of Frame 0 local-jump return stack (`mgr + 0x200`, count at `mgr + 0x340`)
            const std::int32_t jCnt = *reinterpret_cast<const std::int32_t*>(mgr + kOffMgrFrame0JumpCount);
            if (jCnt >= 1 && jCnt <= 20) {
                for (std::int32_t k = jCnt - 1; k >= 0; --k) {
                    const std::uintptr_t parentBase = *reinterpret_cast<const std::uintptr_t*>(
                        mgr + kOffMgrFrame0JumpStack + static_cast<std::uintptr_t>(k) * 0x10U);
                    const std::int32_t pIdx = ResolveNodeArrIdx(parentBase);
                    if (pIdx >= 0) {
                        out.unwoundCallerNodeIdx = pIdx;
                        AppendUniqueCaller(out, pIdx);
                    }
                }
            }
        }

        const std::int32_t resolvedArrIdx = ResolveNodeArrIdx(nodeBase);
        if (resolvedArrIdx < 0) {
            out.failReason = ThkReadFailReason::kBadDepthOrNode;
            return false;
        }
        out.nodeArrIdx = resolvedArrIdx;

        // Read populated candidate table at `mgr + 0x40` (`Candidate[30]`)
        const auto* cands = reinterpret_cast<const RawThkCandidate*>(mgr + kOffMgrCandidates);
        float sumW = 0.0f;
        std::size_t n = 0U;
        for (std::size_t i = 0U; i < kMaxThkCandidates; ++i) {
            if (cands[i].segIdx < 0 || cands[i].segIdx >= static_cast<std::int32_t>(kMaxSegmentsPerNode)) {
                break;
            }
            const float w = cands[i].weight;
            if (!std::isfinite(w) || w < 0.0f) {
                out.failReason = ThkReadFailReason::kBadCandidates;
                return false;
            }

            const std::uintptr_t seg = nodeBase + static_cast<std::uintptr_t>(cands[i].segIdx) * kThkSegStride;
            ThkCandidateOption& dst = out.cands[n++];
            dst.segIdx             = cands[i].segIdx;
            dst.weight             = w;
            dst.ctrlFlags          = *reinterpret_cast<const std::uint32_t*>(seg + 0x00U);
            dst.resolvedViaWrapper = false;

            std::int32_t tf = *reinterpret_cast<const std::int32_t*>(seg + 0x28U);
            std::int32_t tn = *reinterpret_cast<const std::int32_t*>(seg + 0x2CU);
            if (tf < 0) {
                // Follow 1-hop local wrapper nodes (`seg + 0x30` -> trampoline node containing `file, node`)
                const std::uintptr_t locPtr = *reinterpret_cast<const std::uintptr_t*>(seg + 0x30U);
                if (IsValidUserPtr(locPtr)) {
                    for (std::size_t ws = 0U; ws < 8U; ++ws) {
                        const std::uintptr_t wseg = locPtr + ws * kThkSegStride;
                        if ((*reinterpret_cast<const std::uint8_t*>(wseg) & 0x01U) != 0U) break;
                        const std::int32_t wtf = *reinterpret_cast<const std::int32_t*>(wseg + 0x28U);
                        if (wtf >= 0) {
                            tf = wtf;
                            tn = *reinterpret_cast<const std::int32_t*>(wseg + 0x2CU);
                            dst.resolvedViaWrapper = true;
                            break;
                        }
                    }
                }
            }
            dst.targetThkFile = tf;
            dst.targetNodeId  = tn;
            sumW += w;
        }

        if (n == 0U || !(sumW > 0.0f)) {
            out.failReason = ThkReadFailReason::kBadCandidates;
            return false;
        }
        out.candCount   = n;
        out.totalWeight = sumW;
        out.isValid     = true;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        out.failReason = ThkReadFailReason::kBadMgrVft;
        return false;
    }
}

inline std::uint32_t SolveCandidateMidpointU32(const ThkVmContext& lc, std::size_t chosenCand) noexcept {
    if (chosenCand >= lc.candCount || !(lc.totalWeight > 0.0f)) return kMinSafeFloatU32;
    double cumBefore = 0.0;
    for (std::size_t i = 0U; i < chosenCand; ++i) {
        cumBefore += static_cast<double>(lc.cands[i].weight);
    }
    const double mid  = cumBefore + 0.5 * static_cast<double>(lc.cands[chosenCand].weight);
    const double frac = mid / static_cast<double>(lc.totalWeight);
    return FractionToOpenSafeU32(frac);
}

// Replicate `cThinkMgr::updateThk`'s exact float threshold selection for vanilla observation.
inline std::int32_t SimulateThkRollChoice(const ThkVmContext& lc, std::uint32_t u32) noexcept {
    if (lc.candCount == 0U || !(lc.totalWeight > 0.0f)) return -1;
    const float roll = (static_cast<float>(static_cast<std::uint64_t>(u32)) * (100.0f / 4294967296.0f)) *
                       (lc.totalWeight * 0.01f);
    float acc = 0.0f;
    std::int32_t lastPositive = -1;
    for (std::size_t i = 0U; i < lc.candCount; ++i) {
        const float w = lc.cands[i].weight;
        if (w > 0.0f) {
            lastPositive = static_cast<std::int32_t>(i);
            if (acc <= roll && roll < acc + w) {
                return static_cast<std::int32_t>(i);
            }
        }
        acc += w;
    }
    return lastPositive;
}

} // namespace thk
