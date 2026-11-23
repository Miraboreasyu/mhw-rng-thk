#pragma once
// ============================================================================
//  Generic THK Prober, Per-Monster Counter & Count-Gated / Sequence Engine
//  100% Monster-Agnostic: contains zero monster IDs or monster move logic.
//  Strict lock safety: NEVER calls `g_host` or `ctx->DrawDeterministic` inside!
// ============================================================================
#include "thk_vm.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace thk {

struct ThkBranchSpec {
    enum class Kind : std::uint8_t {
        kNone           = 0,
        kTargetFileNode = 1, // `a = thkFile`, `b = targetNodeId` (e.g. `55:297`)
        kCtrlKind       = 2, // `a = 0x40 (RANDOM) | 0x80 (ENDR) | 0xC0 (ELSER)`
        kSegIdx         = 3, // `a = segIdx`
        kCandIdx        = 4, // `a = candIdx`
        kVanilla        = 5  // Deliberate pass-through step inside a sequence
    };
    Kind         kind = Kind::kNone;
    std::int32_t a    = -1;
    std::int32_t b    = -1;

    static ThkBranchSpec Target(std::int32_t file, std::int32_t nodeId) noexcept {
        return {Kind::kTargetFileNode, file, nodeId};
    }
    static ThkBranchSpec Ctrl(std::uint32_t mask60) noexcept {
        return {Kind::kCtrlKind, static_cast<std::int32_t>(mask60), -1};
    }
    static ThkBranchSpec Seg(std::int32_t segIdx) noexcept {
        return {Kind::kSegIdx, segIdx, -1};
    }
    static ThkBranchSpec Cand(std::int32_t candIdx) noexcept {
        return {Kind::kCandIdx, candIdx, -1};
    }
    static ThkBranchSpec Vanilla() noexcept {
        return {Kind::kVanilla, -1, -1};
    }
};

constexpr std::size_t kMaxStepChoices = 8U;

struct ThkWeightedChoice {
    ThkBranchSpec primary{};
    ThkBranchSpec fallback{};
    float         weight = 1.0f;
};

struct ThkActionStep {
    std::uint8_t      choiceCount = 0U;
    ThkWeightedChoice choices[kMaxStepChoices]{};
};

struct ThkPendingWeightedPick {
    bool          active      = false;
    std::uint32_t ruleId      = 0U;
    std::uint32_t condId      = 0U;
    std::uint32_t stepIdx     = 0U;
    std::uint32_t nodeVisit   = 0U;
    char          ruleLabel[64]{};
    char          ruleLabelEn[64]{};
    std::uint8_t  optCount    = 0U;
    std::int32_t  candIdx[kMaxStepChoices]{}; // >=0 candidate idx, or -2 for explicit `vanilla`
    float         optWeight[kMaxStepChoices]{};
    float         totalWeight = 0.0f;
};

enum class ThkCountScope : std::uint8_t {
    kAlways       = 0,
    kRuleMatched  = 1,
    kNodeVisits   = 2,
    kMonsterTotal = 3,
    kMoveHits     = 4
};

enum class ThkSeqScope : std::uint8_t {
    kRuleFired    = 0,
    kNodeVisits   = 1,
    kMonsterTotal = 2
};

enum class ThkSeqEndMode : std::uint8_t {
    kLoop            = 0,
    kHoldLast        = 1,
    kFallbackVanilla = 2
};

struct ThkCountGate {
    ThkCountScope scope       = ThkCountScope::kAlways;
    std::uint32_t minCount    = 0U; // 0 = unbounded
    std::uint32_t maxCount    = 0U; // 0 = unbounded
    std::uint32_t everyPeriod = 0U; // 0 = disabled
    std::uint32_t everyPhase  = 0U;
    std::int32_t  refMoveFile = -1;
    std::int32_t  refMoveNode = -1;
};

constexpr std::size_t kMaxFilterItems = 8U;
constexpr std::size_t kMaxRuleSteps   = 16U;

struct ThkRuleFilter {
    std::int32_t monsterId    = -1; // -1 = any
    std::int32_t depth        = -1; // -1 = any
    std::uint8_t thkFileCount = 0U; // 0  = any file
    std::int32_t thkFiles[kMaxFilterItems]{};
    std::uint8_t nodeCount    = 0U; // 0  = any node
    std::int32_t nodes[kMaxFilterItems]{};
    std::int32_t seg0Min      = -1; // -1 = unbounded
    std::int32_t seg0Max      = -1; // -1 = unbounded
    std::uint8_t callerCount  = 0U; // 0  = any caller
    std::int32_t callers[kMaxFilterItems]{};
};

struct ThkRule {
    std::uint32_t ruleId    = 0U;
    std::uint32_t condId    = 2000U;
    bool          enabled   = true;
    ThkRuleFilter filter{};
    ThkCountGate  gate{};
    ThkSeqScope   seqScope  = ThkSeqScope::kRuleFired;
    ThkSeqEndMode seqEnd    = ThkSeqEndMode::kLoop;
    std::uint8_t  stepCount = 0U;
    ThkActionStep steps[kMaxRuleSteps]{};
    char          tags[64]{};   // Comma-separated lowercase tags (e.g. "guard,far_single")
    char          label[64]{};  // Optional human-readable description
    char          labelEn[64]{};// Optional English human-readable description
};

struct NodeVisitEntry {
    std::uint32_t depth = 0U;
    std::int32_t  file  = -1;
    std::int32_t  node  = -1;
    std::int32_t  seg0  = -1;
    std::uint32_t count = 0U;
};

struct MoveHitEntry {
    std::int32_t  file            = -1;
    std::int32_t  nodeId          = -1;
    std::uint32_t controlledCount = 0U;
    std::uint32_t naturalCount    = 0U;
};

struct RuleCounterEntry {
    std::uint32_t ruleId       = 0U;
    std::uint32_t matchedCount = 0U;
    std::uint32_t firedCount   = 0U;
};

constexpr std::size_t kMaxTrackers      = 8U;
constexpr std::size_t kMaxNodeEntries   = 64U;
constexpr std::size_t kMaxMoveEntries   = 64U;
constexpr std::size_t kMaxRuleCounters  = 64U;
constexpr std::size_t kRecentEventCap   = 16U;
constexpr std::size_t kControlledCap    = 8U;

struct MonsterRuntimeTracker {
    bool             inUse          = false;
    std::uintptr_t   monsterPtr     = 0U;
    std::uint32_t    monsterId      = 0U;
    std::uint64_t    lastTouchSeq   = 0ULL;
    std::uint64_t    totalDecisions = 0ULL;
    std::size_t      nodeCount      = 0U;
    NodeVisitEntry   nodeVisits[kMaxNodeEntries]{};
    std::size_t      moveCount      = 0U;
    MoveHitEntry     moveHits[kMaxMoveEntries]{};
    std::size_t      ruleCntCount   = 0U;
    RuleCounterEntry ruleCounters[kMaxRuleCounters]{};

    std::uint32_t IncrementNodeVisit(std::uint32_t d, std::int32_t f, std::int32_t n, std::int32_t seg0) noexcept {
        for (std::size_t i = 0U; i < nodeCount; ++i) {
            if (nodeVisits[i].depth == d && nodeVisits[i].file == f &&
                nodeVisits[i].node == n && nodeVisits[i].seg0 == seg0) {
                return ++nodeVisits[i].count;
            }
        }
        if (nodeCount < kMaxNodeEntries) {
            auto& e = nodeVisits[nodeCount++];
            e.depth = d;
            e.file  = f;
            e.node  = n;
            e.seg0  = seg0;
            e.count = 1U;
            return 1U;
        }
        // Replace entry with smallest count if saturated
        std::size_t minIdx = 0U;
        for (std::size_t i = 1U; i < kMaxNodeEntries; ++i) {
            if (nodeVisits[i].count < nodeVisits[minIdx].count) minIdx = i;
        }
        nodeVisits[minIdx] = {d, f, n, seg0, 1U};
        return 1U;
    }

    std::uint32_t GetMoveHits(std::int32_t file, std::int32_t nodeId) const noexcept {
        for (std::size_t i = 0U; i < moveCount; ++i) {
            if (moveHits[i].file == file && moveHits[i].nodeId == nodeId) {
                return moveHits[i].controlledCount + moveHits[i].naturalCount;
            }
        }
        return 0U;
    }

    void IncrementMoveHit(std::int32_t file, std::int32_t nodeId, bool controlled) noexcept {
        if (file < 0 || nodeId < 0) return;
        for (std::size_t i = 0U; i < moveCount; ++i) {
            if (moveHits[i].file == file && moveHits[i].nodeId == nodeId) {
                if (controlled) ++moveHits[i].controlledCount;
                else ++moveHits[i].naturalCount;
                return;
            }
        }
        if (moveCount < kMaxMoveEntries) {
            auto& e = moveHits[moveCount++];
            e.file            = file;
            e.nodeId          = nodeId;
            e.controlledCount = controlled ? 1U : 0U;
            e.naturalCount    = controlled ? 0U : 1U;
        }
    }

    RuleCounterEntry* GetOrCreateRuleCounter(std::uint32_t ruleId) noexcept {
        for (std::size_t i = 0U; i < ruleCntCount; ++i) {
            if (ruleCounters[i].ruleId == ruleId) return &ruleCounters[i];
        }
        if (ruleCntCount < kMaxRuleCounters) {
            auto& e = ruleCounters[ruleCntCount++];
            e.ruleId       = ruleId;
            e.matchedCount = 0U;
            e.firedCount   = 0U;
            return &e;
        }
        return nullptr;
    }
};

struct ThkEventRecord {
    bool          valid            = false;
    bool          wasControlled    = false;
    std::uint32_t monsterId        = 0U;
    std::uint32_t depth            = 0U;
    std::int32_t  file             = -1;
    std::int32_t  node             = -1;
    std::int32_t  seg0             = -1;
    std::int32_t  callerNode       = -1;
    std::int32_t  unwoundCaller    = -1;
    std::uint64_t monsterDecisions = 0ULL;
    std::uint32_t nodeVisitNum     = 0U;
    std::uint32_t ruleId           = 0U;
    std::uint32_t ruleStepIdx      = 0U;
    std::uint32_t condId           = 0U;
    std::int32_t  chosenSegIdx     = -1;
    std::int32_t  targetFile       = -1;
    std::int32_t  targetNodeId     = -1;
    std::uint32_t ctrlFlags        = 0U;
    float         chosenWeight     = 0.0f;
    float         totalWeight      = 0.0f;
    std::uint32_t injectedU32      = 0U;
    char          ruleLabel[64]{};
    char          ruleLabelEn[64]{};
    char          candSummary[96]{};
};

struct ThkDiagCounters {
    std::uint64_t enabledCalls      = 0ULL;
    std::uint64_t controlledCalls   = 0ULL;
    std::uint64_t probedCalls       = 0ULL;
    std::uint64_t badCallerCount    = 0ULL;
    std::uint64_t badVftOrPtrCount  = 0ULL;
    std::uint64_t unmatchedCount    = 0ULL;
    std::uint32_t lastBadCallerRva  = 0U;
    std::uint32_t lastUnmatchMon    = 0U;
    std::uint32_t lastUnmatchDepth  = 0U;
    std::int32_t  lastUnmatchFile   = -1;
    std::int32_t  lastUnmatchNode   = -1;
    std::int32_t  lastUnmatchSeg0   = -1;
    std::int32_t  lastUnmatchCaller = -1;
    std::int32_t  lastUnmatchTFile  = -1;
    std::int32_t  lastUnmatchTNode  = -1;
};

struct ThkEvalDecision {
    bool          shouldOverride = false;
    std::uint32_t condId         = 0U;
    std::uint32_t value0         = 0U;
};

inline void CopyTrunc(char* dst, std::size_t cap, const char* src) noexcept {
    if (!dst || cap == 0U) return;
    if (!src) { dst[0] = '\0'; return; }
    std::size_t i = 0U;
    while (i + 1U < cap && src[i] != '\0') {
        dst[i] = src[i];
        ++i;
    }
    dst[i] = '\0';
}

inline std::string LowerAscii(std::string s) {
    for (char& c : s) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
    }
    return s;
}

inline const char* CtrlMaskName(std::uint32_t ctrlFlags) noexcept {
    switch (ctrlFlags & kCtrlKindMask) {
        case kCtrlRandom: return "RANDOM";
        case kCtrlEndr:   return "ENDR";
        case kCtrlElser:  return "ELSER";
        default:          return "CTRL";
    }
}

inline void BuildCandSummary(const ThkVmContext& lc, char* out, std::size_t cap) noexcept {
    if (!out || cap == 0U) return;
    out[0] = '\0';
    std::size_t pos = 0U;
    for (std::size_t i = 0U; i < lc.candCount && pos + 1U < cap; ++i) {
        const auto& c = lc.cands[i];
        char piece[32]{};
        if (c.targetThkFile >= 0 && c.targetNodeId >= 0) {
            std::snprintf(piece, sizeof(piece), "%s%d:%d(%.0f)",
                          (i > 0U ? "," : ""), c.targetThkFile, c.targetNodeId, c.weight);
        } else {
            std::snprintf(piece, sizeof(piece), "%s%s(%.0f)",
                          (i > 0U ? "," : ""), CtrlMaskName(c.ctrlFlags), c.weight);
        }
        const std::size_t len = std::strlen(piece);
        if (pos + len + 4U >= cap) {
            if (pos + 4U < cap) std::memcpy(out + pos, "...", 4U);
            break;
        }
        std::memcpy(out + pos, piece, len + 1U);
        pos += len;
    }
}

struct ThkEngine {
    std::vector<ThkRule>     customRules{};
    std::uint32_t            nextCustomRuleId = 1U;

    std::string              activePresetName{};
    std::string              activePresetDesc{};
    std::string              activePresetDescEn{};
    std::string              activePresetSource{};
    std::vector<ThkRule>     presetRules{};
    std::vector<std::string> disabledTags{};

    bool                     probeEnabled       = false;
    std::int32_t             probeMonsterFilter = -1; // -1 = any monster

    MonsterRuntimeTracker    trackers[kMaxTrackers]{};
    std::uint64_t            globalTickSeq      = 0ULL;

    ThkEventRecord           recentEvents[kRecentEventCap]{};
    std::size_t              recentEventCount   = 0U;
    ThkEventRecord           recentControlled[kControlledCap]{};
    std::size_t              recentCtrlCount    = 0U;

    ThkDiagCounters          diag{};

    bool IsTagDisabled(const std::string& tag) const {
        for (const auto& t : disabledTags) {
            if (t == tag) return true;
        }
        return false;
    }

    void SetTagDisabled(const std::string& rawTag, bool disable) {
        const std::string tag = LowerAscii(rawTag);
        if (tag.empty()) return;
        const auto it = std::find(disabledTags.begin(), disabledTags.end(), tag);
        if (disable) {
            if (it == disabledTags.end()) disabledTags.push_back(tag);
        } else {
            if (it != disabledTags.end()) disabledTags.erase(it);
        }
    }

    bool IsRuleActive(const ThkRule& r) const {
        if (!r.enabled) return false;
        if (r.tags[0] == '\0') return true;
        std::size_t i = 0U;
        while (r.tags[i] != '\0') {
            while (r.tags[i] == ',' || r.tags[i] == ' ') ++i;
            if (r.tags[i] == '\0') break;
            std::size_t start = i;
            while (r.tags[i] != '\0' && r.tags[i] != ',' && r.tags[i] != ' ') ++i;
            std::string t(r.tags + start, i - start);
            t = LowerAscii(t);
            if (IsTagDisabled(t)) return false;
        }
        return true;
    }

    bool NeedsInspection() const {
        if (probeEnabled) return true;
        for (const auto& r : customRules) {
            if (IsRuleActive(r)) return true;
        }
        if (!activePresetName.empty()) {
            for (const auto& r : presetRules) {
                if (IsRuleActive(r)) return true;
            }
        }
        return false;
    }

    bool AnyActiveRuleNeedsNaturalMoves() const {
        auto scan = [&](const std::vector<ThkRule>& list) {
            for (const auto& r : list) {
                if (IsRuleActive(r) && r.gate.scope == ThkCountScope::kMoveHits) return true;
            }
            return false;
        };
        return scan(customRules) || (!activePresetName.empty() && scan(presetRules));
    }

    MonsterRuntimeTracker& TouchTracker(std::uintptr_t monPtr, std::uint32_t monId) noexcept {
        for (auto& t : trackers) {
            if (t.inUse && t.monsterPtr == monPtr && t.monsterId == monId) {
                t.lastTouchSeq = ++globalTickSeq;
                return t;
            }
        }
        std::size_t pick = 0U;
        for (std::size_t i = 0U; i < kMaxTrackers; ++i) {
            if (!trackers[i].inUse) {
                pick = i;
                break;
            }
            if (trackers[i].lastTouchSeq < trackers[pick].lastTouchSeq) pick = i;
        }
        trackers[pick]                = MonsterRuntimeTracker{};
        trackers[pick].inUse        = true;
        trackers[pick].monsterPtr   = monPtr;
        trackers[pick].monsterId    = monId;
        trackers[pick].lastTouchSeq = ++globalTickSeq;
        return trackers[pick];
    }

    const MonsterRuntimeTracker* FindLatestTracker(std::int32_t filterMonId = -1) const noexcept {
        const MonsterRuntimeTracker* best = nullptr;
        for (const auto& t : trackers) {
            if (!t.inUse) continue;
            if (filterMonId >= 0 && t.monsterId != static_cast<std::uint32_t>(filterMonId)) continue;
            if (!best || t.lastTouchSeq > best->lastTouchSeq) best = &t;
        }
        return best;
    }

    void ResetRuntimeTrackers() noexcept {
        for (auto& t : trackers) t = MonsterRuntimeTracker{};
        globalTickSeq    = 0ULL;
        recentEventCount = 0U;
        recentCtrlCount  = 0U;
        for (auto& e : recentEvents) e = ThkEventRecord{};
        for (auto& e : recentControlled) e = ThkEventRecord{};
        diag = ThkDiagCounters{};
    }

    static bool MatchesFilter(const ThkRuleFilter& f, const ThkVmContext& lc) noexcept {
        if (f.monsterId >= 0 && lc.monsterId != static_cast<std::uint32_t>(f.monsterId)) return false;
        if (f.depth >= 0 && lc.depth != static_cast<std::uint32_t>(f.depth)) return false;
        if (f.thkFileCount > 0U) {
            bool hit = false;
            for (std::uint8_t i = 0U; i < f.thkFileCount; ++i) {
                if (f.thkFiles[i] == lc.thkFileIdx) { hit = true; break; }
            }
            if (!hit) return false;
        }
        if (f.nodeCount > 0U) {
            bool hit = false;
            for (std::uint8_t i = 0U; i < f.nodeCount; ++i) {
                if (f.nodes[i] == lc.nodeArrIdx) { hit = true; break; }
            }
            if (!hit) return false;
        }
        const std::int32_t seg0 = (lc.candCount > 0U) ? lc.cands[0].segIdx : -1;
        if (f.seg0Min >= 0 && seg0 < f.seg0Min) return false;
        if (f.seg0Max >= 0 && seg0 > f.seg0Max) return false;
        if (f.callerCount > 0U) {
            bool hit = false;
            for (std::uint8_t i = 0U; i < f.callerCount && !hit; ++i) {
                for (std::size_t c = 0U; c < lc.callerChainCount; ++c) {
                    if (f.callers[i] == lc.callerChain[c]) { hit = true; break; }
                }
            }
            if (!hit) return false;
        }
        return true;
    }

    static bool CheckGate(const ThkCountGate& g,
                          std::uint32_t matchedCnt,
                          std::uint32_t nodeVisitCnt,
                          std::uint64_t monTotalCnt,
                          const MonsterRuntimeTracker& trk) noexcept {
        if (g.scope == ThkCountScope::kAlways) return true;
        std::uint64_t cur = 0ULL;
        switch (g.scope) {
            case ThkCountScope::kRuleMatched:  cur = matchedCnt; break;
            case ThkCountScope::kNodeVisits:   cur = nodeVisitCnt; break;
            case ThkCountScope::kMonsterTotal: cur = monTotalCnt; break;
            case ThkCountScope::kMoveHits:     cur = trk.GetMoveHits(g.refMoveFile, g.refMoveNode); break;
            default: return true;
        }
        if (g.minCount > 0U && cur < g.minCount) return false;
        if (g.maxCount > 0U && cur > g.maxCount) return false;
        if (g.everyPeriod > 0U) {
            const std::uint64_t phase = g.everyPhase % g.everyPeriod;
            if (g.scope == ThkCountScope::kMoveHits) {
                if ((cur % g.everyPeriod) != phase) return false;
            } else {
                if (cur == 0ULL || ((cur - 1ULL) % g.everyPeriod) != phase) return false;
            }
        }
        return true;
    }

    // Returns: >=0 = candidate index; -1 = not found; -2 = explicit `kVanilla` passthrough step!
    static std::int32_t ResolveSpec(const ThkBranchSpec& spec, const ThkVmContext& lc) noexcept {
        switch (spec.kind) {
            case ThkBranchSpec::Kind::kNone:
                return -1;
            case ThkBranchSpec::Kind::kVanilla:
                return -2;
            case ThkBranchSpec::Kind::kTargetFileNode:
                for (std::size_t i = 0U; i < lc.candCount; ++i) {
                    if (lc.cands[i].weight > 0.0f &&
                        lc.cands[i].targetThkFile == spec.a &&
                        lc.cands[i].targetNodeId == spec.b) {
                        return static_cast<std::int32_t>(i);
                    }
                }
                return -1;
            case ThkBranchSpec::Kind::kCtrlKind:
                for (std::size_t i = 0U; i < lc.candCount; ++i) {
                    if (lc.cands[i].weight > 0.0f &&
                        (lc.cands[i].ctrlFlags & kCtrlKindMask) == static_cast<std::uint32_t>(spec.a)) {
                        return static_cast<std::int32_t>(i);
                    }
                }
                return -1;
            case ThkBranchSpec::Kind::kSegIdx:
                for (std::size_t i = 0U; i < lc.candCount; ++i) {
                    if (lc.cands[i].weight > 0.0f && lc.cands[i].segIdx == spec.a) {
                        return static_cast<std::int32_t>(i);
                    }
                }
                return -1;
            case ThkBranchSpec::Kind::kCandIdx:
                if (spec.a >= 0 && static_cast<std::size_t>(spec.a) < lc.candCount &&
                    lc.cands[spec.a].weight > 0.0f) {
                    return spec.a;
                }
                return -1;
            default:
                return -1;
        }
    }

    void PushEventRecord(const ThkEventRecord& rec, bool alsoControlled) noexcept {
        for (std::size_t i = kRecentEventCap - 1U; i > 0U; --i) {
            recentEvents[i] = recentEvents[i - 1U];
        }
        recentEvents[0] = rec;
        if (recentEventCount < kRecentEventCap) ++recentEventCount;

        if (alsoControlled) {
            for (std::size_t i = kControlledCap - 1U; i > 0U; --i) {
                recentControlled[i] = recentControlled[i - 1U];
            }
            recentControlled[0] = rec;
            if (recentCtrlCount < kControlledCap) ++recentCtrlCount;
        }
    }

    bool ShouldProbeMonster(std::uint32_t monId) const noexcept {
        return probeEnabled &&
               (probeMonsterFilter < 0 || monId == static_cast<std::uint32_t>(probeMonsterFilter));
    }

    void RecordControlledDecisionLocked(MonsterRuntimeTracker& trk,
                                        const ThkVmContext& lc,
                                        std::uint32_t ruleId,
                                        std::uint32_t condId,
                                        std::uint32_t stepIdx,
                                        std::uint32_t nodeVisitNum,
                                        const char* ruleLabel,
                                        const char* ruleLabelEn,
                                        std::size_t candIdx,
                                        ThkEvalDecision& outDec) noexcept {
        const std::uint32_t solvedU32 = SolveCandidateMidpointU32(lc, candIdx);
        const std::int32_t  seg0      = (lc.candCount > 0U) ? lc.cands[0].segIdx : -1;

        outDec.shouldOverride = true;
        outDec.condId         = condId;
        outDec.value0         = solvedU32;
        ++diag.controlledCalls;

        const auto& chosen = lc.cands[candIdx];
        if (chosen.targetThkFile >= 0 && chosen.targetNodeId >= 0) {
            trk.IncrementMoveHit(chosen.targetThkFile, chosen.targetNodeId, true);
        }

        ThkEventRecord rec{};
        rec.valid            = true;
        rec.wasControlled    = true;
        rec.monsterId        = lc.monsterId;
        rec.depth            = lc.depth;
        rec.file             = lc.thkFileIdx;
        rec.node             = lc.nodeArrIdx;
        rec.seg0             = seg0;
        rec.callerNode       = lc.callerNodeIdx;
        rec.unwoundCaller    = lc.unwoundCallerNodeIdx;
        rec.monsterDecisions = trk.totalDecisions;
        rec.nodeVisitNum     = nodeVisitNum;
        rec.ruleId           = ruleId;
        rec.ruleStepIdx      = stepIdx;
        rec.condId           = condId;
        rec.chosenSegIdx     = chosen.segIdx;
        rec.targetFile       = chosen.targetThkFile;
        rec.targetNodeId     = chosen.targetNodeId;
        rec.ctrlFlags        = chosen.ctrlFlags;
        rec.chosenWeight     = chosen.weight;
        rec.totalWeight      = lc.totalWeight;
        rec.injectedU32      = solvedU32;
        CopyTrunc(rec.ruleLabel, sizeof(rec.ruleLabel), ruleLabel);
        CopyTrunc(rec.ruleLabelEn, sizeof(rec.ruleLabelEn), ruleLabelEn);
        BuildCandSummary(lc, rec.candSummary, sizeof(rec.candSummary));
        PushEventRecord(rec, true);
    }

    void EvaluateRulesLocked(const ThkVmContext& lc,
                             ThkEvalDecision& outDec,
                             ThkPendingWeightedPick& outWeighted,
                             bool& outNeedObserveVanilla,
                             std::uint32_t& outNodeVisitNum) noexcept {
        outDec                = ThkEvalDecision{};
        outWeighted           = ThkPendingWeightedPick{};
        outNeedObserveVanilla = false;
        outNodeVisitNum       = 0U;
        ++diag.enabledCalls;

        MonsterRuntimeTracker& trk = TouchTracker(lc.monsterPtr, lc.monsterId);
        ++trk.totalDecisions;
        const std::int32_t seg0 = (lc.candCount > 0U) ? lc.cands[0].segIdx : -1;
        outNodeVisitNum = trk.IncrementNodeVisit(lc.depth, lc.thkFileIdx, lc.nodeArrIdx, seg0);

        auto tryRuleList = [&](const std::vector<ThkRule>& rules, bool& handledByVanillaRule) -> bool {
            for (const auto& rule : rules) {
                if (!IsRuleActive(rule) || rule.stepCount == 0U) continue;
                if (!MatchesFilter(rule.filter, lc)) continue;

                RuleCounterEntry* rc = trk.GetOrCreateRuleCounter(rule.ruleId);
                const std::uint32_t matchedNow = rc ? ++rc->matchedCount : 1U;

                if (!CheckGate(rule.gate, matchedNow, outNodeVisitNum, trk.totalDecisions, trk)) {
                    continue;
                }

                std::uint64_t rawIdx = 0ULL;
                switch (rule.seqScope) {
                    case ThkSeqScope::kRuleFired:    rawIdx = rc ? rc->firedCount : 0ULL; break;
                    case ThkSeqScope::kNodeVisits:   rawIdx = (outNodeVisitNum > 0U) ? (outNodeVisitNum - 1ULL) : 0ULL; break;
                    case ThkSeqScope::kMonsterTotal: rawIdx = (trk.totalDecisions > 0ULL) ? (trk.totalDecisions - 1ULL) : 0ULL; break;
                }

                std::uint32_t stepIdx = 0U;
                if (rawIdx >= rule.stepCount) {
                    if (rule.seqEnd == ThkSeqEndMode::kLoop) {
                        stepIdx = static_cast<std::uint32_t>(rawIdx % rule.stepCount);
                    } else if (rule.seqEnd == ThkSeqEndMode::kHoldLast) {
                        stepIdx = static_cast<std::uint32_t>(rule.stepCount - 1U);
                    } else {
                        // `kFallbackVanilla` once sequence finishes
                        handledByVanillaRule = true;
                        return false;
                    }
                } else {
                    stepIdx = static_cast<std::uint32_t>(rawIdx);
                }

                const ThkActionStep& step = rule.steps[stepIdx];
                std::int32_t availCand[kMaxStepChoices]{};
                float        availWt[kMaxStepChoices]{};
                std::uint8_t availCnt = 0U;
                float        sumWt    = 0.0f;

                for (std::uint8_t c = 0U; c < step.choiceCount && c < kMaxStepChoices; ++c) {
                    const ThkWeightedChoice& wc = step.choices[c];
                    if (!(wc.weight > 0.0f)) continue;
                    std::int32_t resolved = ResolveSpec(wc.primary, lc);
                    if (resolved == -1 && wc.fallback.kind != ThkBranchSpec::Kind::kNone) {
                        resolved = ResolveSpec(wc.fallback, lc);
                    }
                    if (resolved >= 0 || resolved == -2) {
                        availCand[availCnt] = resolved;
                        availWt[availCnt]   = wc.weight;
                        sumWt += wc.weight;
                        ++availCnt;
                    }
                }

                if (availCnt == 0U || !(sumWt > 0.0f)) continue;

                if (availCnt == 1U) {
                    if (availCand[0] == -2) {
                        // Explicit `vanilla` step: advance sequence counter, let draw remain vanilla.
                        if (rc) ++rc->firedCount;
                        handledByVanillaRule = true;
                        return false;
                    }
                    if (rc) ++rc->firedCount;
                    RecordControlledDecisionLocked(trk, lc, rule.ruleId, rule.condId, stepIdx,
                                                   outNodeVisitNum, rule.label, rule.labelEn,
                                                   static_cast<std::size_t>(availCand[0]), outDec);
                    return true;
                }

                // Multi-choice weighted branch (`A(w1)+B(w2)`): defer deterministic roll until lock is released.
                outWeighted.active      = true;
                outWeighted.ruleId      = rule.ruleId;
                outWeighted.condId      = rule.condId;
                outWeighted.stepIdx     = stepIdx;
                outWeighted.nodeVisit   = outNodeVisitNum;
                outWeighted.optCount    = availCnt;
                outWeighted.totalWeight = sumWt;
                CopyTrunc(outWeighted.ruleLabel, sizeof(outWeighted.ruleLabel), rule.label);
                CopyTrunc(outWeighted.ruleLabelEn, sizeof(outWeighted.ruleLabelEn), rule.labelEn);
                for (std::uint8_t k = 0U; k < availCnt; ++k) {
                    outWeighted.candIdx[k]   = availCand[k];
                    outWeighted.optWeight[k] = availWt[k];
                }
                return true;
            }
            return false;
        };

        bool explicitVanilla = false;
        if (tryRuleList(customRules, explicitVanilla)) return;
        if (!explicitVanilla && !activePresetName.empty()) {
            if (tryRuleList(presetRules, explicitVanilla)) return;
        }

        // Neither rule overrode: check whether we should observe vanilla roll for probe/move tracking.
        const bool needObs = ShouldProbeMonster(lc.monsterId) || AnyActiveRuleNeedsNaturalMoves();
        if (needObs) {
            outNeedObserveVanilla = true;
            return;
        }

        ++diag.unmatchedCount;
        diag.lastUnmatchMon    = lc.monsterId;
        diag.lastUnmatchDepth  = lc.depth;
        diag.lastUnmatchFile   = lc.thkFileIdx;
        diag.lastUnmatchNode   = lc.nodeArrIdx;
        diag.lastUnmatchSeg0   = seg0;
        diag.lastUnmatchCaller = lc.unwoundCallerNodeIdx;
        diag.lastUnmatchTFile  = (lc.candCount > 0U) ? lc.cands[0].targetThkFile : -1;
        diag.lastUnmatchTNode  = (lc.candCount > 0U) ? lc.cands[0].targetNodeId : -1;
    }

    void CommitWeightedPickLocked(const ThkVmContext& lc,
                                  const ThkPendingWeightedPick& pick,
                                  std::uint32_t rollU32,
                                  ThkEvalDecision& outDec,
                                  bool& outNeedObserveVanilla) noexcept {
        outDec                = ThkEvalDecision{};
        outNeedObserveVanilla = false;
        if (!pick.active || pick.optCount == 0U || !(pick.totalWeight > 0.0f)) return;

        const double target = (static_cast<double>(rollU32) / 4294967296.0) * static_cast<double>(pick.totalWeight);
        double acc = 0.0;
        std::size_t chosenOpt = static_cast<std::size_t>(pick.optCount - 1U);
        for (std::uint8_t i = 0U; i < pick.optCount; ++i) {
            const double w = static_cast<double>(pick.optWeight[i]);
            if (acc <= target && target < acc + w) {
                chosenOpt = i;
                break;
            }
            acc += w;
        }

        MonsterRuntimeTracker& trk = TouchTracker(lc.monsterPtr, lc.monsterId);
        if (RuleCounterEntry* rc = trk.GetOrCreateRuleCounter(pick.ruleId)) {
            ++rc->firedCount;
        }

        const std::int32_t resolved = pick.candIdx[chosenOpt];
        if (resolved == -2) {
            outNeedObserveVanilla = ShouldProbeMonster(lc.monsterId) || AnyActiveRuleNeedsNaturalMoves();
            return;
        }
        if (resolved >= 0) {
            RecordControlledDecisionLocked(trk, lc, pick.ruleId, pick.condId, pick.stepIdx,
                                           pick.nodeVisit, pick.ruleLabel, pick.ruleLabelEn,
                                           static_cast<std::size_t>(resolved), outDec);
        }
    }

    void RecordObservedVanillaLocked(const ThkVmContext& lc,
                                     std::uint32_t detU32,
                                     std::int32_t chosenCandIdx,
                                     std::uint32_t nodeVisitNum) noexcept {
        MonsterRuntimeTracker& trk = TouchTracker(lc.monsterPtr, lc.monsterId);
        if (chosenCandIdx >= 0 && static_cast<std::size_t>(chosenCandIdx) < lc.candCount) {
            const auto& chosen = lc.cands[chosenCandIdx];
            if (chosen.targetThkFile >= 0 && chosen.targetNodeId >= 0) {
                trk.IncrementMoveHit(chosen.targetThkFile, chosen.targetNodeId, false);
            }
        }
        if (ShouldProbeMonster(lc.monsterId)) {
            ++diag.probedCalls;
            ThkEventRecord rec{};
            rec.valid            = true;
            rec.wasControlled    = false;
            rec.monsterId        = lc.monsterId;
            rec.depth            = lc.depth;
            rec.file             = lc.thkFileIdx;
            rec.node             = lc.nodeArrIdx;
            rec.seg0             = (lc.candCount > 0U) ? lc.cands[0].segIdx : -1;
            rec.callerNode       = lc.callerNodeIdx;
            rec.unwoundCaller    = lc.unwoundCallerNodeIdx;
            rec.monsterDecisions = trk.totalDecisions;
            rec.nodeVisitNum     = nodeVisitNum;
            rec.injectedU32      = detU32;
            rec.totalWeight      = lc.totalWeight;
            if (chosenCandIdx >= 0 && static_cast<std::size_t>(chosenCandIdx) < lc.candCount) {
                const auto& ch   = lc.cands[chosenCandIdx];
                rec.chosenSegIdx = ch.segIdx;
                rec.targetFile   = ch.targetThkFile;
                rec.targetNodeId = ch.targetNodeId;
                rec.ctrlFlags    = ch.ctrlFlags;
                rec.chosenWeight = ch.weight;
            }
            CopyTrunc(rec.ruleLabel, sizeof(rec.ruleLabel), "probe");
            BuildCandSummary(lc, rec.candSummary, sizeof(rec.candSummary));
            PushEventRecord(rec, false);
        }
    }
};

} // namespace thk
