// ============================================================================
//  MHW RNG Official Plugin: Generic THK Prober, Counter & Pure-RNG Engine (`rng_thk.dll`)
//  Version : 1.0.0 (SDK v2)
//  Target  : `RVA 0x1339C8C` (`cThinkMgr::updateThk` @ 0x141339640 `CALL MtRandom::nrand`)
//
//  Strict Pure-RNG Architecture:
//    - Interacts ONLY with `mhw_rng.dll` via `MhwRngHostApi::RegisterSlotHook`.
//    - ZERO extra game detours/hooks, ZERO `.thk` file patching, ZERO AI memory writes.
//    - Abstracts arbitrary THK node probing, per-monster decision/node/move counting,
//      and count-gated / ordered-sequence rules away from specific monster presets.
//    - Ships with declarative preset `thk_presets/fatalis_p1_nasty.txt` and preserves
//      the `$rng fatalis ...` shortcut facade over that preset.
// ============================================================================
#include "thk_preset_io.h"
#include "presets/fatalis_p1_nasty.h"
#include "mhw_world_api.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

namespace {

const MhwRngHostApi* g_host   = nullptr;
MhwRngPluginId       g_selfId = 0U;

// Strict leaf mutex: NEVER call `g_host->...` or `ctx->DrawDeterministic(...)` while holding `g_stateMx`!
std::mutex           g_stateMx;
thk::ThkEngine       g_engine{};
bool                 g_fatalisFarMulti    = false;
bool                 g_fatalisGuards      = true;
MhwRngHookHandle     g_worldEvQuestEnter  = kMhwRngInvalidHandle;
MhwRngHookHandle     g_worldEvQuestLeave  = kMhwRngInvalidHandle;
MhwRngHookHandle     g_worldEvTitleReturn = kMhwRngInvalidHandle;

void __fastcall OnWorldEvent(const MhwRngCustomEvent* ev, void*) {
    if (!ev || !ev->topic) return;
    std::lock_guard<std::mutex> lk(g_stateMx);
    g_engine.ResetRuntimeTrackers();
}

// ============================================================================
//  Preset Loader Helper (Filesystem I/O & `g_host` Calls Run Unlocked)
// ============================================================================

std::string GetPresetDir() {
    if (!g_host || !g_host->GetSubPluginDir) return {};
    const char* subDir = g_host->GetSubPluginDir();
    if (!subDir || !subDir[0]) return {};
    return std::string(subDir) + "\\thk_presets";
}

bool LoadPresetByName(const std::string& rawName,
                      std::string& outLoadedName,
                      std::string& outDesc,
                      std::string& outSource,
                      std::size_t& outRuleCount,
                      std::string& outErr) {
    outErr.clear();
    std::string cleanName = thk::TrimAscii(rawName);
    if (cleanName.empty()) {
        outErr = "preset name is empty";
        return false;
    }
    const std::string lowName = thk::LowerAscii(cleanName);
    if (lowName.size() > 4U && lowName.substr(lowName.size() - 4U) == ".txt") {
        cleanName.resize(cleanName.size() - 4U);
    }

    thk::ThkPresetDocument doc{};
    const std::string pDir = GetPresetDir();
    std::string sourceDesc;
    bool ok = false;

    if (!pDir.empty()) {
        const std::string filePath = pDir + "\\" + cleanName + ".txt";
        if (::GetFileAttributesA(filePath.c_str()) != INVALID_FILE_ATTRIBUTES) {
            ok = thk::LoadPresetFromFile(filePath, cleanName, doc, outErr);
            if (!ok) return false;
            sourceDesc = "file:thk_presets\\" + cleanName + ".txt";
        }
    }

    if (!ok && thk::LowerAscii(cleanName) == "fatalis_p1_nasty") {
        ok = thk::ParsePresetFromText(thk::presets::kBuiltinFatalisP1NastyPresetTxt,
                                      "fatalis_p1_nasty", doc, outErr);
        if (!ok) return false;
        sourceDesc = "builtin";
    }

    if (!ok) {
        outErr = "preset not found: " + cleanName;
        return false;
    }

    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        g_engine.activePresetName   = doc.name.empty() ? cleanName : doc.name;
        g_engine.activePresetDesc   = doc.desc;
        g_engine.activePresetDescEn = doc.descEn;
        g_engine.activePresetSource = sourceDesc;
        g_engine.presetRules        = std::move(doc.rules);
        g_engine.presetContexts     = std::move(doc.contexts);
        g_engine.disabledTags       = std::move(doc.defaultDisabledTags);

        // Apply Fatalis facade tag toggles when loading `fatalis_p1_nasty`
        if (thk::LowerAscii(g_engine.activePresetName) == "fatalis_p1_nasty") {
            g_engine.SetTagDisabled("guard", !g_fatalisGuards);
            g_engine.SetTagDisabled("far_single", g_fatalisFarMulti);
            g_engine.SetTagDisabled("far_multi", !g_fatalisFarMulti);
        }

        outLoadedName = g_engine.activePresetName;
        outDesc       = g_engine.activePresetDesc;
        outSource     = g_engine.activePresetSource;
        outRuleCount  = g_engine.presetRules.size();
    }
    return true;
}

void EnsureTakeoverOn() {
    if (g_host && !g_host->IsTakeoverEnabled()) {
        g_host->SetTakeoverEnabled(true);
    }
}

void FormatSpecText(const thk::ThkBranchSpec& sp, char* out, std::size_t cap) {
    switch (sp.kind) {
        case thk::ThkBranchSpec::Kind::kTargetFileNode:
            std::snprintf(out, cap, "%d:%d", sp.a, sp.b);
            break;
        case thk::ThkBranchSpec::Kind::kCtrlKind:
            std::snprintf(out, cap, "%s", thk::CtrlMaskName(static_cast<std::uint32_t>(sp.a)));
            break;
        case thk::ThkBranchSpec::Kind::kSegIdx:
            std::snprintf(out, cap, "seg:%d", sp.a);
            break;
        case thk::ThkBranchSpec::Kind::kCandIdx:
            std::snprintf(out, cap, "cand:%d", sp.a);
            break;
        case thk::ThkBranchSpec::Kind::kVanilla:
            std::snprintf(out, cap, "vanilla");
            break;
        default:
            std::snprintf(out, cap, "-");
            break;
    }
}

// ============================================================================
//  Pull-Mode Hook on `RVA 0x1339C8C` (`cThinkMgr::updateThk`)
// ============================================================================

void __fastcall OnThkRandomDraw(const MhwRngDrawContext* ctx,
                                MhwRngHookDecision* ioDecision,
                                void*) {
    if (!ioDecision) return;

    bool needInspect = false;
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        needInspect = g_engine.NeedsInspection();
    }

    if (!needInspect) {
        ioDecision->action = kMhwRngActionUseCondition;
        ioDecision->condId = 0U;
        return;
    }

    thk::ThkVmContext lc{};
    const bool readOk = thk::ReadThkVmContextSEH(ctx, lc);
    if (!readOk || !lc.isValid) {
        ioDecision->action = kMhwRngActionUseCondition;
        ioDecision->condId = 0U;
        std::lock_guard<std::mutex> lk(g_stateMx);
        ++g_engine.diag.enabledCalls;
        if (lc.failReason == thk::ThkReadFailReason::kBadCaller) {
            ++g_engine.diag.badCallerCount;
            g_engine.diag.lastBadCallerRva = lc.observedCallerRva;
        } else {
            ++g_engine.diag.badVftOrPtrCount;
        }
        return;
    }

    thk::ThkEvalDecision dec{};
    thk::ThkPendingWeightedPick weighted{};
    bool needObserveVanilla = false;
    std::uint32_t nodeVisitNum = 0U;
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        g_engine.EvaluateRulesLocked(lc, dec, weighted, needObserveVanilla, nodeVisitNum);
    }

    if (weighted.active) {
        // Call DrawDeterministic strictly OUTSIDE `g_stateMx`!
        const std::uint32_t rollU32 = (ctx && ctx->DrawDeterministic)
                                          ? ctx->DrawDeterministic(thk::kRvaThkUpdateRandom, weighted.condId)
                                          : 0U;
        std::lock_guard<std::mutex> lk(g_stateMx);
        g_engine.CommitWeightedPickLocked(lc, weighted, rollU32, dec, needObserveVanilla);
    }

    if (dec.shouldOverride) {
        ioDecision->action = kMhwRngActionReturnValue;
        ioDecision->condId = dec.condId;
        ioDecision->value0 = dec.value0;
        return;
    }

    if (needObserveVanilla && ctx && ctx->DrawDeterministic) {
        // Call DrawDeterministic strictly OUTSIDE `g_stateMx`!
        const std::uint32_t detU32 = ctx->DrawDeterministic(thk::kRvaThkUpdateRandom, 0U);
        const std::int32_t chosen  = thk::SimulateThkRollChoice(lc, detU32);
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_engine.RecordObservedVanillaLocked(lc, detU32, chosen, nodeVisitNum);
        }
        ioDecision->action = kMhwRngActionReturnValue;
        ioDecision->condId = 0U;
        ioDecision->value0 = detU32;
        return;
    }

    ioDecision->action = kMhwRngActionUseCondition;
    ioDecision->condId = 0U;
}

// ============================================================================
//  Telemetry Formatter (`$rng status` / `$rng top`)
// ============================================================================

bool __fastcall FormatThkSlotStatus(const MhwRngSlotSnapshot* snap,
                                    char* outBuf,
                                    std::size_t bufCap,
                                    void*) {
    if (!snap || !outBuf || bufCap == 0U) return false;
    const bool en = MhwRng_HostIsEnglish(g_host);
    switch (snap->condId) {
        case 0U: {
            bool active = false;
            bool probe  = false;
            std::string presetName;
            std::size_t customCnt = 0U;
            std::uint64_t ctrlCalls = 0ULL;
            {
                std::lock_guard<std::mutex> lk(g_stateMx);
                active     = g_engine.NeedsInspection();
                probe      = g_engine.probeEnabled;
                presetName = g_engine.activePresetName;
                customCnt  = g_engine.customRules.size();
                ctrlCalls  = g_engine.diag.controlledCalls;
            }
            if (!active) return false;
            std::snprintf(outBuf, bufCap,
                          en ? "THK[preset:%s rules:%zu probe:%s ctrl:%llu]"
                             : u8"THK[预设:%s 规则:%zu 探测:%s 已控:%llu]",
                          presetName.empty() ? "-" : presetName.c_str(),
                          customCnt,
                          probe ? (en ? "ON" : u8"开") : (en ? "OFF" : u8"关"),
                          static_cast<unsigned long long>(ctrlCalls));
            return true;
        }
        case 1011U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 bi <900 -> Cone (55:297)"              : u8"黑龙P1 2足<900 -> 孕吐(55:297)"); return true;
        case 1012U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 post-cone -> No chain (ENDR)"         : u8"黑龙P1 2足孕吐 -> 不派生扇火(ENDR)"); return true;
        case 1013U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 bi front -> Carpet (55:184)"          : u8"黑龙P1 2足正面 -> 地毯火(55:184)"); return true;
        case 1014U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 bi side -> Side carpet (55:188)"      : u8"黑龙P1 2足侧面 -> 侧地毯火(55:188)"); return true;
        case 1015U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 bi back -> Tail whip (55:175)"        : u8"黑龙P1 2足背面 -> 2足甩尾(55:175)"); return true;
        case 1016U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 bi far/occl -> Non-cone carpet"       : u8"黑龙P1 2足远程/遮挡 -> 非扇地毯火"); return true;
        case 1017U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 bi >4500 -> Fireball"                 : u8"黑龙P1 2足>4500 -> 火球"); return true;
        case 1018U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 bipedal stance guard"                 : u8"黑龙P1 二足稳态守卫"); return true;
        case 1021U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 quad <500 no-charge -> Snap (55:216)" : u8"黑龙P1 4足<500未车 -> 蛇形咬(55:216)"); return true;
        case 1022U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 quad <500 charged -> Snapback(55:277)": u8"黑龙P1 4足<500已车 -> 后撤地毯火(55:277)"); return true;
        case 1023U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 quad mid front -> Snap (55:216)"      : u8"黑龙P1 4足500~1500正面未车 -> 蛇形咬(55:216)"); return true;
        case 1024U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 quad mid front -> 50/50 Charge/Back"  : u8"黑龙P1 4足500~1500正面已车 -> 50/50蓄力火/后撤地毯"); return true;
        case 1025U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 quad back -> Tail slam (55:176)"      : u8"黑龙P1 4足后方 -> 尾砸(55:176)"); return true;
        case 1026U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 quad mid-far/occl -> Fireball"        : u8"黑龙P1 4足1500~5000/遮挡 -> 火球"); return true;
        case 1027U: std::snprintf(outBuf, bufCap, en ? "Fatalis P1 quad >5000 -> Fireball"               : u8"黑龙P1 4足>5000 -> 火球"); return true;
        default:
            if (snap->condId >= 2000U) {
                std::snprintf(outBuf, bufCap, en ? "THK custom rule/seq #%u" : u8"THK 自定义规则/序列表#%u", snap->condId - 2000U);
                return true;
            }
            return false;
    }
}

void PrintThkDiag(const char* prefix) {
    const bool en = MhwRng_HostIsEnglish(g_host);
    thk::ThkDiagCounters ds{};
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        ds = g_engine.diag;
    }
    g_host->ChatPrintf(en ? "[%s] Diag: calls=%llu ctrl=%llu probe=%llu badCaller=%llu(0x%X) badPtr=%llu"
                          : u8"[%s] 诊断: 回调=%llu 控招=%llu 探测=%llu 未知caller=%llu(0x%X) 指针异常=%llu",
                       prefix,
                       static_cast<unsigned long long>(ds.enabledCalls),
                       static_cast<unsigned long long>(ds.controlledCalls),
                       static_cast<unsigned long long>(ds.probedCalls),
                       static_cast<unsigned long long>(ds.badCallerCount),
                       ds.lastBadCallerRva,
                       static_cast<unsigned long long>(ds.badVftOrPtrCount));
    if (ds.unmatchedCount > 0ULL) {
        g_host->ChatPrintf(en ? "[%s] Unmatched=%llu, last(mon=%u d=%u f=%d node=%d seg0=%d caller=%d tgt0=%d:%d)"
                              : u8"[%s] 未命中=%llu次, 最近(mon=%u d=%u f=%d node=%d seg0=%d caller=%d tgt0=%d:%d)",
                           prefix,
                           static_cast<unsigned long long>(ds.unmatchedCount),
                           ds.lastUnmatchMon, ds.lastUnmatchDepth, ds.lastUnmatchFile,
                           ds.lastUnmatchNode, ds.lastUnmatchSeg0, ds.lastUnmatchCaller,
                           ds.lastUnmatchTFile, ds.lastUnmatchTNode);
    }
}

void PrintThkStatus() {
    const bool en = MhwRng_HostIsEnglish(g_host);
    std::string presetName, presetSource;
    std::vector<std::string> disTags;
    std::size_t customCnt = 0U, presetRuleCnt = 0U;
    bool probeOn = false;
    std::int32_t probeMon = -1;
    thk::ThkDiagCounters ds{};
    std::size_t trackerCnt = 0U;
    bool hasLatest = false;
    std::uint32_t latestMonsterId = 0U;
    std::uint64_t latestTotalDecisions = 0U;
    std::size_t latestNodeCount = 0U;
    std::size_t latestMoveCount = 0U;

    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        presetName    = g_engine.activePresetName;
        presetSource  = g_engine.activePresetSource;
        disTags       = g_engine.disabledTags;
        customCnt     = g_engine.customRules.size();
        presetRuleCnt = g_engine.presetRules.size();
        probeOn       = g_engine.probeEnabled;
        probeMon      = g_engine.probeMonsterFilter;
        ds            = g_engine.diag;
        for (const auto& t : g_engine.trackers) {
            if (t.inUse) ++trackerCnt;
        }
        if (const auto* lt = g_engine.FindLatestTracker()) {
            latestMonsterId      = lt->monsterId;
            latestTotalDecisions = lt->totalDecisions;
            latestNodeCount      = lt->nodeCount;
            latestMoveCount      = lt->moveCount;
            hasLatest            = true;
        }
    }

    g_host->ChatPrintf(en ? "[thk] Preset:%s(%zu%s%s) | Custom:%zu | Probe:%s(mon=%s) | Ctrl:%llu Probe:%llu"
                          : u8"[thk] 预设:%s(%zu条%s%s) | 自定义规则:%zu | 探测:%s(mon=%s) | 已控:%llu 探测:%llu",
                       presetName.empty() ? (en ? "none" : u8"无") : presetName.c_str(),
                       presetRuleCnt,
                       presetSource.empty() ? "" : ",",
                       presetSource.c_str(),
                       customCnt,
                       probeOn ? (en ? "ON" : u8"开") : (en ? "OFF" : u8"关"),
                       probeMon < 0 ? "*" : std::to_string(probeMon).c_str(),
                       static_cast<unsigned long long>(ds.controlledCalls),
                       static_cast<unsigned long long>(ds.probedCalls));
    if (!disTags.empty()) {
        std::string joined;
        for (std::size_t i = 0U; i < disTags.size(); ++i) {
            if (i > 0U) joined += ",";
            joined += disTags[i];
        }
        g_host->ChatPrintf(en ? "[thk] Disabled tags: %s" : u8"[thk] 已禁用标签: %s", joined.c_str());
    }
    if (hasLatest) {
        g_host->ChatPrintf(en ? "[thk] Active inst:%zu | Last mon em=%u decisions:%llu nodes:%zu moves:%zu"
                              : u8"[thk] 活跃实例:%zu | 最近怪物 em=%u 总决策:%llu 节点类:%zu 招式类:%zu",
                           trackerCnt, latestMonsterId,
                           static_cast<unsigned long long>(latestTotalDecisions),
                           latestNodeCount, latestMoveCount);
    } else {
        g_host->ChatPrint(en ? "[thk] Usage: `$rng thk probe|nodes|stats|rule|seq|preset|reset|clear|diag|help`"
                             : u8"[thk] 用法: `$rng thk probe|nodes|stats|rule|seq|preset|reset|clear|diag|help`");
    }
    char worldBuf[128]{};
    if (MhwWorld_TryFormatSummary(g_host, worldBuf, sizeof(worldBuf), en)) {
        g_host->ChatPrintf(en ? "[thk] World context: %s" : u8"[thk] 世界上下文: %s", worldBuf);
    }
}

void PrintProbeLog() {
    const bool en = MhwRng_HostIsEnglish(g_host);
    thk::ThkEventRecord snap[thk::kRecentEventCap]{};
    std::size_t cnt = 0U;
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        cnt = g_engine.recentEventCount;
        for (std::size_t i = 0U; i < cnt; ++i) snap[i] = g_engine.recentEvents[i];
    }
    if (cnt == 0U) {
        g_host->ChatPrint(en ? "[thk] No probe/control events yet; enable via `$rng thk probe on [monId]`"
                             : u8"[thk] 尚无探测或控招记录；先输入 `$rng thk probe on [怪ID]`");
        return;
    }
    const std::size_t showN = std::min<std::size_t>(cnt, 10U);
    for (std::size_t i = 0U; i < showN; ++i) {
        const auto& e = snap[i];
        char chosenBuf[32]{};
        if (e.targetFile >= 0 && e.targetNodeId >= 0) {
            std::snprintf(chosenBuf, sizeof(chosenBuf), "%d:%d", e.targetFile, e.targetNodeId);
        } else {
            std::snprintf(chosenBuf, sizeof(chosenBuf), "%s", thk::CtrlMaskName(e.ctrlFlags));
        }
        g_host->ChatPrintf("[thk] #%zu[%s] em=%u #%llu d=%u f=%d node=%d(n#%u) seg0=%d cal=%d -> %s | [%s]",
                           i + 1U,
                           e.wasControlled ? (en ? "CTL" : u8"控") : (en ? "VAN" : u8"原"),
                           e.monsterId,
                           static_cast<unsigned long long>(e.monsterDecisions),
                           e.depth, e.file, e.node, e.nodeVisitNum, e.seg0, e.unwoundCaller,
                           chosenBuf, e.candSummary);
    }
}

void PrintNodesSummary(std::int32_t monFilter) {
    const bool en = MhwRng_HostIsEnglish(g_host);
    bool found = false;
    std::uint32_t monsterId = 0U;
    std::uint64_t totalDecisions = 0U;
    std::vector<thk::NodeVisitEntry> items;
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        if (const auto* p = g_engine.FindLatestTracker(monFilter)) {
            monsterId      = p->monsterId;
            totalDecisions = p->totalDecisions;
            items.assign(p->nodeVisits, p->nodeVisits + p->nodeCount);
            found = true;
        }
    }
    if (!found || items.empty()) {
        g_host->ChatPrint(en ? "[thk] No node stats yet; enable `$rng thk probe on` or load a preset"
                             : u8"[thk] 暂无节点访问统计；开启 `$rng thk probe on` 或加载预设并进入战斗后查看");
        return;
    }
    std::sort(items.begin(), items.end(), [](const thk::NodeVisitEntry& a, const thk::NodeVisitEntry& b) {
        return a.count > b.count;
    });
    g_host->ChatPrintf(en ? "[thk] em=%u total=%llu, top nodes (%zu unique):"
                          : u8"[thk] em=%u 总决策=%llu, 访问最多的节点(共%zu种):",
                       monsterId, static_cast<unsigned long long>(totalDecisions), items.size());
    const std::size_t showN = std::min<std::size_t>(items.size(), 10U);
    for (std::size_t i = 0U; i < showN; ++i) {
        const auto& n = items[i];
        g_host->ChatPrintf(en ? "[thk]   d=%u f=%d node=%d seg0=%d -> %u"
                              : u8"[thk]   d=%u f=%d node=%d seg0=%d -> %u次",
                           n.depth, n.file, n.node, n.seg0, n.count);
    }
}

void PrintMoveStats(std::int32_t monFilter) {
    const bool en = MhwRng_HostIsEnglish(g_host);
    bool found = false;
    std::uint32_t monsterId = 0U;
    std::uint64_t totalDecisions = 0U;
    std::vector<thk::MoveHitEntry> items;
    thk::LastMoveContext lastMove{};
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        if (const auto* p = g_engine.FindLatestTracker(monFilter)) {
            monsterId      = p->monsterId;
            totalDecisions = p->totalDecisions;
            items.assign(p->moveHits, p->moveHits + p->moveCount);
            lastMove       = p->lastMove;
            found = true;
        }
    }
    if (!found) {
        g_host->ChatPrint(en ? "[thk] No move stats yet; enable `$rng thk probe on` or a rule first"
                             : u8"[thk] 暂无怪物出招计数；开启 `$rng thk probe on` 或规则后自动累计");
        return;
    }
    if (lastMove.valid) {
        g_host->ChatPrintf(en ? "[thk] Last move: %d:%d (from d=%u f=%d node=%d seg0=%d)"
                              : u8"[thk] 上一招: %d:%d (来自 d=%u f=%d node=%d seg0=%d)",
                           lastMove.moveFile, lastMove.moveNode, lastMove.depth,
                           lastMove.file, lastMove.node, lastMove.seg0);
    }
    std::sort(items.begin(), items.end(), [](const thk::MoveHitEntry& a, const thk::MoveHitEntry& b) {
        return (a.controlledCount + a.naturalCount) > (b.controlledCount + b.naturalCount);
    });
    g_host->ChatPrintf(en ? "[thk] em=%u total=%llu, recorded moves (%zu unique):"
                          : u8"[thk] em=%u 总决策=%llu, 已记录出招(%zu种):",
                       monsterId, static_cast<unsigned long long>(totalDecisions), items.size());
    const std::size_t showN = std::min<std::size_t>(items.size(), 12U);
    for (std::size_t i = 0U; i < showN; ++i) {
        const auto& m = items[i];
        const std::uint32_t tot = m.controlledCount + m.naturalCount;
        g_host->ChatPrintf(en ? "[thk]   %d:%d -> %u (ctrl %u / van %u)"
                              : u8"[thk]   %d:%d -> 共%u次 (控%u / 原%u)",
                           m.file, m.nodeId, tot, m.controlledCount, m.naturalCount);
    }
}

bool ParseCommonModifiers(int startIdx,
                          int argc,
                          const char* const* argv,
                          thk::ThkRule& rule,
                          std::string& outErr) {
    for (int i = startIdx; i < argc; ++i) {
        const std::string tok  = argv[i];
        const std::string low  = thk::LowerAscii(tok);
        if (low == "loop") { rule.seqEnd = thk::ThkSeqEndMode::kLoop; continue; }
        if (low == "hold") { rule.seqEnd = thk::ThkSeqEndMode::kHoldLast; continue; }
        if (low == "once" || low == "vanilla") { rule.seqEnd = thk::ThkSeqEndMode::kFallbackVanilla; continue; }

        const bool en = MhwRng_HostIsEnglish(g_host);
        const std::size_t eq = tok.find('=');
        if (eq == std::string::npos) {
            outErr = (en ? "Unknown arg: " : u8"未知参数: ") + tok;
            return false;
        }
        const std::string kLow = thk::LowerAscii(tok.substr(0U, eq));
        const std::string val  = tok.substr(eq + 1U);
        const std::string vLow = thk::LowerAscii(val);

        if (kLow == "d" || kLow == "depth") {
            if (vLow == "*" || vLow == "any") rule.filter.depth = -1;
            else if (!thk::ParseSignedIntStrict(vLow, rule.filter.depth)) { outErr = en ? "Invalid depth" : u8"无效 depth"; return false; }
        } else if (kLow == "f" || kLow == "file") {
            if (!thk::ParseIntList(vLow, rule.filter.thkFiles, thk::kMaxFilterItems, rule.filter.thkFileCount)) {
                outErr = en ? "Invalid file" : u8"无效 file";
                return false;
            }
        } else if (kLow == "caller" || kLow == "callers") {
            if (!thk::ParseIntList(vLow, rule.filter.callers, thk::kMaxFilterItems, rule.filter.callerCount)) {
                outErr = en ? "Invalid caller" : u8"无效 caller";
                return false;
            }
        } else if (kLow == "prev" || kLow == "prev_move") {
            if (!thk::ParseMoveList(vLow, rule.filter.prevMoveFiles, rule.filter.prevMoveNodes,
                                    thk::kMaxFilterItems, rule.filter.prevMoveCount)) {
                outErr = en ? "Invalid prev move (use file:node)" : u8"无效 prev 招式(格式 file:node)";
                return false;
            }
        } else if (kLow == "prev_d" || kLow == "prev_depth") {
            if (vLow == "*" || vLow == "any") rule.filter.prevDepth = -1;
            else if (!thk::ParseSignedIntStrict(vLow, rule.filter.prevDepth) || rule.filter.prevDepth < 0) {
                outErr = en ? "Invalid prev_d" : u8"无效 prev_d";
                return false;
            }
        } else if (kLow == "prev_f" || kLow == "prev_file") {
            if (!thk::ParseIntList(vLow, rule.filter.prevFiles, thk::kMaxFilterItems, rule.filter.prevFileCount)) {
                outErr = en ? "Invalid prev_f" : u8"无效 prev_f";
                return false;
            }
        } else if (kLow == "prev_node" || kLow == "prev_nodes") {
            if (!thk::ParseIntList(vLow, rule.filter.prevNodes, thk::kMaxFilterItems, rule.filter.prevNodeCount)) {
                outErr = en ? "Invalid prev_node" : u8"无效 prev_node";
                return false;
            }
        } else if (kLow == "prev_seg0") {
            if (!thk::ParseSeg0Range(vLow, rule.filter.prevSeg0Min, rule.filter.prevSeg0Max)) {
                outErr = en ? "Invalid prev_seg0" : u8"无效 prev_seg0";
                return false;
            }
        } else if (kLow == "prev_caller" || kLow == "prev_callers") {
            if (!thk::ParseIntList(vLow, rule.filter.prevCallers, thk::kMaxFilterItems, rule.filter.prevCallerCount)) {
                outErr = en ? "Invalid prev_caller" : u8"无效 prev_caller";
                return false;
            }
        } else if (kLow == "scope") {
            if (vLow == "fired" || vLow == "rule") rule.seqScope = thk::ThkSeqScope::kRuleFired;
            else if (vLow == "node") rule.seqScope = thk::ThkSeqScope::kNodeVisits;
            else if (vLow == "total") rule.seqScope = thk::ThkSeqScope::kMonsterTotal;
            else { outErr = en ? "Invalid scope (fired|node|total)" : u8"无效 scope (fired|node|total)"; return false; }
        } else if (kLow == "end") {
            if (vLow == "loop") rule.seqEnd = thk::ThkSeqEndMode::kLoop;
            else if (vLow == "hold") rule.seqEnd = thk::ThkSeqEndMode::kHoldLast;
            else if (vLow == "once" || vLow == "vanilla") rule.seqEnd = thk::ThkSeqEndMode::kFallbackVanilla;
            else { outErr = en ? "Invalid end (loop|hold|once)" : u8"无效 end (loop|hold|once)"; return false; }
        } else if (kLow == "label") {
            thk::CopyTrunc(rule.label, sizeof(rule.label), val.c_str());
        } else if (kLow == "label_en") {
            thk::CopyTrunc(rule.labelEn, sizeof(rule.labelEn), val.c_str());
        } else if (!thk::ParseGateKeyVal(kLow, val, rule.gate)) {
            outErr = (en ? "Unknown modifier: " : u8"未知修饰符: ") + tok;
            return false;
        }
    }
    return true;
}

bool ParseRuleTargetBase(const char* monStr,
                         const char* nodeStr,
                         const char* seg0Str,
                         const char* tgtStr,
                         thk::ThkRule& rule,
                         std::string& outErr) {
    const bool en = MhwRng_HostIsEnglish(g_host);
    const std::string mLow = thk::LowerAscii(monStr);
    if (mLow == "*" || mLow == "any") {
        rule.filter.monsterId = -1;
    } else if (!thk::ParseSignedIntStrict(mLow, rule.filter.monsterId)) {
        outErr = en ? "Invalid monster ID" : u8"无效怪物ID";
        return false;
    }
    if (!thk::ParseIntList(nodeStr, rule.filter.nodes, thk::kMaxFilterItems, rule.filter.nodeCount)) {
        outErr = en ? "Invalid node list" : u8"无效节点列表";
        return false;
    }
    if (!thk::ParseSeg0Range(seg0Str, rule.filter.seg0Min, rule.filter.seg0Max)) {
        outErr = en ? "Invalid seg0 range" : u8"无效 seg0 范围";
        return false;
    }
    if (!thk::ParseSequenceSteps(tgtStr, rule)) {
        outErr = en ? "Invalid target or sequence" : u8"无效目标或序列";
        return false;
    }
    return true;
}

void HandleRuleCmd(int argc, const char* const* argv) {
    const bool en = MhwRng_HostIsEnglish(g_host);
    if (argc < 2) {
        g_host->ChatPrint(en ? "[thk] Usage: `$rng thk rule add <mon|*> <node> <seg0|*> <tgt> [after=N until=M]` | `list` | `del <id>` | `clear`"
                             : u8"[thk] 用法: `$rng thk rule add <mon|*> <node> <seg0|*> <目标> [after=N until=M every=P:Ph d=0 f=0]` | `list` | `del <id>` | `clear`");
        return;
    }
    const std::string sub = thk::LowerAscii(argv[1]);
    if (sub == "list" || sub == "ls") {
        std::vector<thk::ThkRule> copy;
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            copy = g_engine.customRules;
        }
        if (copy.empty()) {
            g_host->ChatPrint(en ? "[thk] No custom rules" : u8"[thk] 当前无自定义规则");
            return;
        }
        for (const auto& r : copy) {
            char firstTgt[32]{};
            if (r.steps[0].choiceCount > 0U) {
                FormatSpecText(r.steps[0].choices[0].primary, firstTgt, sizeof(firstTgt));
            } else {
                std::snprintf(firstTgt, sizeof(firstTgt), "-");
            }
            g_host->ChatPrintf("[thk] #%u mon=%d node=%s seg0=[%d..%d] steps=%u(%s%s) min=%u max=%u",
                               r.ruleId, r.filter.monsterId,
                               (r.filter.nodeCount > 0U ? std::to_string(r.filter.nodes[0]).c_str() : "*"),
                               r.filter.seg0Min, r.filter.seg0Max,
                               static_cast<unsigned>(r.stepCount), firstTgt,
                               (r.steps[0].choiceCount > 1U ? "+..." : ""),
                               r.gate.minCount, r.gate.maxCount);
        }
        return;
    }
    if (sub == "clear") {
        bool stayActive = false;
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_engine.customRules.clear();
            stayActive = g_engine.NeedsInspection();
        }
        if (!stayActive) g_host->ClearSlotAllConditions(thk::kRvaThkUpdateRandom);
        g_host->ChatPrint(en ? "[thk] Cleared all custom THK rules" : u8"[thk] 已清空所有自定义 THK 规则");
        return;
    }
    if (sub == "del" || sub == "rm" || sub == "remove") {
        if (argc < 3) {
            g_host->ChatPrint(en ? "[thk] Usage: `$rng thk rule del <ruleId>`" : u8"[thk] 用法: `$rng thk rule del <ruleId>`");
            return;
        }
        std::uint32_t id = 0U;
        if (!thk::ParseUnsignedIntStrict(argv[2], id)) {
            g_host->ChatPrint(en ? "[thk] Invalid ruleId" : u8"[thk] 无效 ruleId");
            return;
        }
        bool erased = false;
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            auto& v = g_engine.customRules;
            const auto oldSize = v.size();
            v.erase(std::remove_if(v.begin(), v.end(), [id](const thk::ThkRule& r) { return r.ruleId == id; }), v.end());
            erased = (v.size() != oldSize);
        }
        g_host->ChatPrintf(en ? "[thk] Rule #%u %s" : u8"[thk] 规则 #%u %s",
                           id, erased ? (en ? "deleted" : u8"已删除") : (en ? "not found" : u8"未找到"));
        return;
    }
    if (sub == "add") {
        if (argc < 6) {
            g_host->ChatPrint(en ? "[thk] Usage: `$rng thk rule add <mon|*> <node> <seg0|*> <tgt> [after=N] [until=M] [every=P:Ph] [when=...] [d=0] [f=0|55]`"
                                 : u8"[thk] 用法: `$rng thk rule add <mon|*> <node> <seg0|*> <目标> [after=N] [until=M] [every=P:Ph] [when=rule|node|total|move:F:N] [d=0|1] [f=0|55]`");
            return;
        }
        thk::ThkRule r{};
        std::string err;
        if (!ParseRuleTargetBase(argv[2], argv[3], argv[4], argv[5], r, err) ||
            !ParseCommonModifiers(6, argc, argv, r, err)) {
            g_host->ChatPrintf(en ? "[thk] Failed adding rule: %s" : u8"[thk] 添加规则失败: %s", err.c_str());
            return;
        }
        EnsureTakeoverOn();
        std::uint32_t assignedId = 0U;
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            r.ruleId   = g_engine.nextCustomRuleId++;
            r.condId   = 2000U + r.ruleId;
            if (r.label[0] == '\0') std::snprintf(r.label, sizeof(r.label), "rule#%u", r.ruleId);
            assignedId = r.ruleId;
            g_engine.customRules.push_back(r);
        }
        g_host->ChatPrintf(en ? "[thk] Added rule #%u (cond=%u, steps=%u)" : u8"[thk] 已添加规则 #%u (cond=%u, steps=%u)",
                           assignedId, 2000U + assignedId, static_cast<unsigned>(r.stepCount));
        return;
    }

    g_host->ChatPrintf(en ? "[thk] Unknown rule subcmd `%s`" : u8"[thk] 未知 rule 子命令 `%s`", argv[1]);
}

void HandleSeqCmd(int argc, const char* const* argv) {
    const bool en = MhwRng_HostIsEnglish(g_host);
    if (argc < 5) {
        g_host->ChatPrint(en ? "[thk] Usage: `$rng thk seq <mon|*> <node> <seg0|*> <t1,t2,...> [loop|hold|once]`"
                             : u8"[thk] 用法: `$rng thk seq <mon|*> <node> <seg0|*> <t1,t2,...> [loop|hold|once] [scope=fired|node|total] [after=N] [until=M] [d=0] [f=0]`");
        return;
    }
    thk::ThkRule r{};
    std::string err;
    if (!ParseRuleTargetBase(argv[1], argv[2], argv[3], argv[4], r, err) ||
        !ParseCommonModifiers(5, argc, argv, r, err)) {
        g_host->ChatPrintf(en ? "[thk] Failed adding seq: %s" : u8"[thk] 添加序列失败: %s", err.c_str());
        return;
    }
    EnsureTakeoverOn();
    std::uint32_t assignedId = 0U;
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        r.ruleId   = g_engine.nextCustomRuleId++;
        r.condId   = 2000U + r.ruleId;
        if (r.label[0] == '\0') std::snprintf(r.label, sizeof(r.label), "seq#%u", r.ruleId);
        assignedId = r.ruleId;
        g_engine.customRules.push_back(r);
    }
    g_host->ChatPrintf(en ? "[thk] Added sequence #%u (steps=%u, end=%s)" : u8"[thk] 已添加出招序列 #%u (步数=%u, 模式=%s)",
                       assignedId,
                       static_cast<unsigned>(r.stepCount),
                       r.seqEnd == thk::ThkSeqEndMode::kLoop ? "loop" :
                       (r.seqEnd == thk::ThkSeqEndMode::kHoldLast ? "hold" : "once"));
}

void HandlePresetCmd(int argc, const char* const* argv) {
    const bool en = MhwRng_HostIsEnglish(g_host);
    if (argc < 2) {
        g_host->ChatPrint(en ? "[thk] Usage: `$rng thk preset list` | `load <name>` | `reload` | `off` | `tag <tag> on|off`"
                             : u8"[thk] 用法: `$rng thk preset list` | `load <名称>` | `reload` | `off` | `tag <tag> on|off`");
        return;
    }
    const std::string sub = thk::LowerAscii(argv[1]);
    if (sub == "list" || sub == "ls") {
        const auto files = thk::ListPresetFiles(GetPresetDir());
        std::string listStr = en ? "fatalis_p1_nasty(builtin)" : u8"fatalis_p1_nasty(内置)";
        for (const auto& f : files) {
            if (thk::LowerAscii(f) == "fatalis_p1_nasty") {
                listStr += en ? " [disk]" : u8" [磁盘覆盖]";
            } else {
                listStr += ", " + f;
            }
        }
        g_host->ChatPrintf(en ? "[thk] Presets: %s" : u8"[thk] 可用预设: %s", listStr.c_str());
        return;
    }
    if (sub == "off" || sub == "unload") {
        bool stayActive = false;
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_engine.activePresetName.clear();
            g_engine.activePresetDesc.clear();
            g_engine.activePresetDescEn.clear();
            g_engine.activePresetSource.clear();
            g_engine.presetRules.clear();
            g_engine.presetContexts.clear();
            g_engine.disabledTags.clear();
            stayActive = g_engine.NeedsInspection();
        }
        if (!stayActive) g_host->ClearSlotAllConditions(thk::kRvaThkUpdateRandom);
        g_host->ChatPrint(en ? "[thk] Disabled THK preset" : u8"[thk] 已停用 THK 预设");
        return;
    }
    if (sub == "load" || sub == "reload") {
        std::string targetName;
        if (sub == "load") {
            if (argc < 3) {
                g_host->ChatPrint(en ? "[thk] Usage: `$rng thk preset load <name>`" : u8"[thk] 用法: `$rng thk preset load <名称>`");
                return;
            }
            targetName = argv[2];
        } else {
            {
                std::lock_guard<std::mutex> lk(g_stateMx);
                targetName = g_engine.activePresetName;
            }
            if (targetName.empty()) {
                g_host->ChatPrint(en ? "[thk] No active preset to reload" : u8"[thk] 当前未加载任何预设，无法 reload");
                return;
            }
        }
        std::string loadedName, desc, src, err;
        std::size_t ruleCnt = 0U;
        if (!LoadPresetByName(targetName, loadedName, desc, src, ruleCnt, err)) {
            g_host->ChatPrintf(en ? "[thk] Failed loading preset: %s" : u8"[thk] 加载预设失败: %s", err.c_str());
            return;
        }
        EnsureTakeoverOn();
        g_host->ChatPrintf(en ? "[thk] Loaded preset `%s` (%zu rules, src=%s)" : u8"[thk] 已加载预设 `%s` (%zu条规则, 来源=%s)",
                           loadedName.c_str(), ruleCnt, src.c_str());
        return;
    }
    if (sub == "tag" && argc >= 4) {
        const std::string tag = thk::LowerAscii(argv[2]);
        const std::string st  = thk::LowerAscii(argv[3]);
        const bool enable     = (st == "on" || st == "1" || st == "true");
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_engine.SetTagDisabled(tag, !enable);
        }
        g_host->ChatPrintf(en ? "[thk] Preset tag `%s` %s" : u8"[thk] 预设标签 `%s` 已%s",
                           tag.c_str(), enable ? (en ? "enabled" : u8"启用") : (en ? "disabled" : u8"禁用"));
        return;
    }

    g_host->ChatPrintf(en ? "[thk] Unknown preset subcmd `%s`" : u8"[thk] 未知 preset 子命令 `%s`", argv[1]);
}

void __fastcall Cmd_Thk(int argc, const char* const* argv, const char*, void*) {
    if (!g_host) return;
    const bool en = MhwRng_HostIsEnglish(g_host);
    if (argc == 0) { PrintThkStatus(); return; }

    const std::string a0 = thk::LowerAscii(argv[0]);
    if (a0 == "status" || a0 == "info") { PrintThkStatus(); return; }
    if (a0 == "diag" || a0 == "debug")  { PrintThkDiag("thk"); return; }
    if (a0 == "help" || a0 == "?") {
        if (en) {
            g_host->ChatPrint("[thk] Probe:  `$rng thk probe on [monId]|off|log` | `nodes [monId]` | `stats [monId]` | `reset`");
            g_host->ChatPrint("[thk] Rules:  `$rng thk rule add <mon|*> <node> <seg0|*> <tgt>` | `seq ... [loop|hold|once]`");
            g_host->ChatPrint("[thk] Preset: `$rng thk preset list|load <name>|reload|off|tag <t> on|off` | `clear` | `diag`");
        } else {
            g_host->ChatPrint(u8"[thk] 探测计数: `$rng thk probe on [怪ID]|off|log` | `nodes [怪ID]` | `stats [怪ID]` | `reset`");
            g_host->ChatPrint(u8"[thk] 规则序列: `$rng thk rule add <怪|*> <node> <seg0|*> <目标> [after=N until=M]` | `seq <怪|*> <node> <seg0|*> <t1,t2,...> [loop|hold|once]`");
            g_host->ChatPrint(u8"[thk] 预设管理: `$rng thk preset list|load <名>|reload|off|tag <t> on|off` | `clear` | `diag`");
        }
        return;
    }
    if (a0 == "probe") {
        if (argc >= 2) {
            const std::string a1 = thk::LowerAscii(argv[1]);
            if (a1 == "log" || a1 == "history") { PrintProbeLog(); return; }
            if (a1 == "off" || a1 == "0" || a1 == "stop") {
                {
                    std::lock_guard<std::mutex> lk(g_stateMx);
                    g_engine.probeEnabled       = false;
                    g_engine.probeMonsterFilter = -1;
                }
                g_host->ChatPrint(en ? "[thk] Live node probe OFF" : u8"[thk] 已关闭实时节点探测");
                return;
            }
            if (a1 == "on" || a1 == "1" || a1 == "start") {
                std::int32_t mon = -1;
                if (argc >= 3 && !thk::ParseSignedIntStrict(argv[2], mon)) {
                    g_host->ChatPrint(en ? "[thk] Invalid monster ID" : u8"[thk] 无效怪ID");
                    return;
                }
                EnsureTakeoverOn();
                {
                    std::lock_guard<std::mutex> lk(g_stateMx);
                    g_engine.probeEnabled       = true;
                    g_engine.probeMonsterFilter = mon;
                }
                g_host->ChatPrintf(en ? "[thk] Live THK node probe ON (mon=%s)"
                                      : u8"[thk] 已开启 THK 节点实时探测 (过滤怪ID=%s)",
                                   mon < 0 ? "*" : std::to_string(mon).c_str());
                return;
            }
        }
        g_host->ChatPrint(en ? "[thk] Usage: `$rng thk probe on [monId]` | `off` | `log`"
                             : u8"[thk] 用法: `$rng thk probe on [怪ID]` | `off` | `log`");
        return;
    }
    if (a0 == "nodes" || a0 == "node") {
        std::int32_t mon = -1;
        if (argc >= 2) thk::ParseSignedIntStrict(argv[1], mon);
        PrintNodesSummary(mon);
        return;
    }
    if (a0 == "stats" || a0 == "moves" || a0 == "count") {
        std::int32_t mon = -1;
        if (argc >= 2) thk::ParseSignedIntStrict(argv[1], mon);
        PrintMoveStats(mon);
        return;
    }
    if (a0 == "reset") {
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_engine.ResetRuntimeTrackers();
        }
        g_host->ChatPrint(en ? "[thk] Reset all runtime node/move counters and sequence cursors"
                             : u8"[thk] 已清零所有怪物运行时节点/出招计数与序列游标");
        return;
    }
    if (a0 == "clear" || a0 == "off" || a0 == "stop" || a0 == "disable") {
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_engine.customRules.clear();
            g_engine.activePresetName.clear();
            g_engine.activePresetDesc.clear();
            g_engine.activePresetDescEn.clear();
            g_engine.activePresetSource.clear();
            g_engine.presetRules.clear();
            g_engine.presetContexts.clear();
            g_engine.disabledTags.clear();
            g_engine.probeEnabled       = false;
            g_engine.probeMonsterFilter = -1;
        }
        g_host->ClearSlotAllConditions(thk::kRvaThkUpdateRandom);
        g_host->ChatPrint(en ? "[thk] Cleared all THK rules/presets & probe -> vanilla RNG"
                             : u8"[thk] 已清空全部规则/预设并关闭探测 -> 恢复原版随机");
        return;
    }
    if (a0 == "rule" || a0 == "rules") { HandleRuleCmd(argc, argv); return; }
    if (a0 == "seq" || a0 == "sequence") { HandleSeqCmd(argc, argv); return; }
    if (a0 == "preset" || a0 == "presets") { HandlePresetCmd(argc, argv); return; }

    g_host->ChatPrintf(en ? "[thk] Unknown subcmd `%s`; type `$rng thk help`" : u8"[thk] 未知子命令 `%s`，输 `$rng thk help` 查看用法", argv[0]);
}

bool IsFatalisPresetActiveLocked() {
    return thk::LowerAscii(g_engine.activePresetName) == "fatalis_p1_nasty";
}

void PrintFatalisStatus() {
    const bool en = MhwRng_HostIsEnglish(g_host);
    bool enabled = false, guards = true, farMulti = false;
    std::uint64_t rolls = 0ULL, calls = 0ULL;
    thk::ThkEventRecord latest{};
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        enabled  = IsFatalisPresetActiveLocked();
        guards   = g_fatalisGuards;
        farMulti = g_fatalisFarMulti;
        rolls    = g_engine.diag.controlledCalls;
        calls    = g_engine.diag.enabledCalls;
        for (std::size_t i = 0U; i < g_engine.recentCtrlCount; ++i) {
            if (g_engine.recentControlled[i].monsterId == 101U) {
                latest = g_engine.recentControlled[i];
                break;
            }
        }
    }
    g_host->ChatPrintf(en ? "[fatalis] State:%s | Far:%s | Guards:%s | Controlled:%llu"
                          : u8"[fatalis] 状态:%s | 远程火球:%s | 倒地/遮挡守卫:%s | 累计控制:%llu次",
                       enabled ? (en ? "ON (P1 bi+quad presets/fatalis_p1_nasty)" : u8"已开启(P1二足+四足 presets/fatalis_p1_nasty)")
                               : (en ? "OFF (vanilla)" : u8"已关闭(原版)"),
                       farMulti ? (en ? "Triple fireball (55:220/221)" : u8"三连火球(55:220/221)")
                                : (en ? "Single fireball (55:179/180)" : u8"单发火球(55:179/180)"),
                       guards ? (en ? "ON" : u8"开(倒地衍生/遮挡/额外车)") : (en ? "OFF (main nodes only)" : u8"关(仅主节点)"),
                       static_cast<unsigned long long>(rolls));
    if (enabled && rolls == 0ULL && calls > 0ULL) {
        PrintThkDiag("fatalis");
    }
    if (latest.valid) {
        char tgtBuf[32]{};
        if (latest.targetFile >= 0 && latest.targetNodeId >= 0) {
            std::snprintf(tgtBuf, sizeof(tgtBuf), "%d:%d", latest.targetFile, latest.targetNodeId);
        } else {
            std::snprintf(tgtBuf, sizeof(tgtBuf), "%s", thk::CtrlMaskName(latest.ctrlFlags));
        }
        g_host->ChatPrintf(en ? "[fatalis] Last: [%s] -> %s (node=%d seg=%d w=%.0f/%.0f u32=0x%08X)"
                              : u8"[fatalis] 最近一次: [%s] -> %s (node=%d seg=%d w=%.0f/%.0f u32=0x%08X)",
                           MhwRng_Tr(g_host, latest.ruleLabel, latest.ruleLabelEn), tgtBuf,
                           latest.node, latest.chosenSegIdx,
                           latest.chosenWeight, latest.totalWeight, latest.injectedU32);
    } else {
        g_host->ChatPrint(en ? "[fatalis] Usage: `$rng fatalis on|off|status|history|diag|far single|multi|guards on|off`"
                             : u8"[fatalis] 用法: `$rng fatalis on|off|status|history|diag|far single|multi|guards on|off`");
    }
}

void PrintFatalisHistory() {
    const bool en = MhwRng_HostIsEnglish(g_host);
    thk::ThkEventRecord snap[thk::kControlledCap]{};
    std::size_t count = 0U;
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        count = g_engine.recentCtrlCount;
        for (std::size_t i = 0U; i < count; ++i) snap[i] = g_engine.recentControlled[i];
    }
    std::size_t printed = 0U;
    for (std::size_t i = 0U; i < count; ++i) {
        const auto& h = snap[i];
        if (h.monsterId != 101U) continue;
        char tgtBuf[32]{};
        if (h.targetFile >= 0 && h.targetNodeId >= 0) {
            std::snprintf(tgtBuf, sizeof(tgtBuf), "%d:%d", h.targetFile, h.targetNodeId);
        } else {
            std::snprintf(tgtBuf, sizeof(tgtBuf), "%s", thk::CtrlMaskName(h.ctrlFlags));
        }
        g_host->ChatPrintf("[fatalis] #%zu [%s] -> %s | node=%d seg=%d (%.0f/%.0f) u32=0x%08X",
                           printed + 1U, MhwRng_Tr(g_host, h.ruleLabel, h.ruleLabelEn), tgtBuf,
                           h.node, h.chosenSegIdx,
                           h.chosenWeight, h.totalWeight, h.injectedU32);
        ++printed;
    }
    if (printed == 0U) {
        g_host->ChatPrint(en ? "[fatalis] No Fatalis P1 control records yet; run `$rng fatalis on` in Fatalis quest"
                             : u8"[fatalis] 尚无黑龙P1二足控招记录；开启 `$rng fatalis on` 并进入黑龙任务后自动记录");
    }
}

void __fastcall Cmd_Fatalis(int argc, const char* const* argv, const char*, void*) {
    if (!g_host) return;
    const bool en = MhwRng_HostIsEnglish(g_host);
    if (argc == 0) { PrintFatalisStatus(); return; }

    const std::string a0 = thk::LowerAscii(argv[0]);
    if (a0 == "status" || a0 == "info") { PrintFatalisStatus(); return; }
    if (a0 == "history" || a0 == "recent" || a0 == "log") { PrintFatalisHistory(); return; }
    if (a0 == "diag" || a0 == "debug") { PrintThkDiag("fatalis"); return; }
    if (a0 == "help" || a0 == "?") {
        if (en) {
            g_host->ChatPrint("[fatalis] Toggle:  `$rng fatalis on|off|status|history|diag` (preset: fatalis_p1_nasty)");
            g_host->ChatPrint("[fatalis] Options: `far single` (55:179) | `far multi` (55:220) | `guards on|off`");
        } else {
            g_host->ChatPrint(u8"[fatalis] 开关: `$rng fatalis on|off|status|history|diag` (基于 `fatalis_p1_nasty` 预设)");
            g_host->ChatPrint(u8"[fatalis] 选项: `far single`(55:179) | `far multi`(55:220) | `guards on|off`");
        }
        return;
    }
    if (a0 == "on" || a0 == "1" || a0 == "start" || a0 == "enable") {
        std::string loadedName, desc, src, err;
        std::size_t cnt = 0U;
        if (!LoadPresetByName("fatalis_p1_nasty", loadedName, desc, src, cnt, err)) {
            g_host->ChatPrintf(en ? "[fatalis] Enable failed: %s" : u8"[fatalis] 启动失败: %s", err.c_str());
            return;
        }
        EnsureTakeoverOn();
        g_host->ChatPrintf(en ? "[fatalis] Fatalis P1 full preset ON (%zu rules, src=%s)"
                              : u8"[fatalis] 已启用黑龙P1全姿态预设 (%zu条规则, 来源=%s)",
                           cnt, src.c_str());
        return;
    }
    if (a0 == "off" || a0 == "0" || a0 == "stop" || a0 == "disable" || a0 == "clear" || a0 == "reset") {
        bool stayActive = false;
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            if (IsFatalisPresetActiveLocked()) {
                g_engine.activePresetName.clear();
                g_engine.activePresetDesc.clear();
                g_engine.activePresetDescEn.clear();
                g_engine.activePresetSource.clear();
                g_engine.presetRules.clear();
                g_engine.presetContexts.clear();
            }
            stayActive = g_engine.NeedsInspection();
        }
        if (!stayActive) g_host->ClearSlotAllConditions(thk::kRvaThkUpdateRandom);
        g_host->ChatPrint(en ? "[fatalis] Fatalis P1 preset OFF -> vanilla THK RNG"
                             : u8"[fatalis] 已关闭黑龙P1预设 -> 恢复原版THK随机");
        return;
    }
    if (a0 == "far") {
        if (argc < 2) {
            g_host->ChatPrint(en ? "[fatalis] Usage: `$rng fatalis far single` (55:179) or `far multi` (55:220)"
                                 : u8"[fatalis] 用法: `$rng fatalis far single` (55:179) 或 `far multi` (55:220)");
            return;
        }
        const std::string a1 = thk::LowerAscii(argv[1]);
        bool multi = false;
        if (a1 == "multi" || a1 == "triple" || a1 == "3" || a1 == "220") multi = true;
        else if (a1 != "single" && a1 != "179" && a1 != "one") {
            g_host->ChatPrintf(en ? "[fatalis] Unknown far mode `%s`" : u8"[fatalis] 未知 far 模式 `%s`", argv[1]);
            return;
        }
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_fatalisFarMulti = multi;
            g_engine.SetTagDisabled("far_single", multi);
            g_engine.SetTagDisabled("far_multi", !multi);
        }
        if (en) {
            g_host->ChatPrint(multi ? "[fatalis] Far fireball -> Triple (bi 55:220 / quad 55:221)"
                                    : "[fatalis] Far fireball -> Single (bi 55:179 / quad 55:180)");
        } else {
            g_host->ChatPrint(multi ? u8"[fatalis] 远程火球已设为: 三连火球(2足55:220 / 4足55:221)"
                                    : u8"[fatalis] 远程火球已设为: 单发火球(2足55:179 / 4足55:180)");
        }
        return;
    }
    if (a0 == "guards" || a0 == "guard") {
        if (argc < 2) {
            g_host->ChatPrint(en ? "[fatalis] Usage: `$rng fatalis guards on|off`" : u8"[fatalis] 用法: `$rng fatalis guards on|off`");
            return;
        }
        const std::string a1 = thk::LowerAscii(argv[1]);
        const bool on = (a1 == "on" || a1 == "1" || a1 == "true");
        if (!on && a1 != "off" && a1 != "0" && a1 != "false") {
            g_host->ChatPrintf(en ? "[fatalis] Unknown guards arg `%s`" : u8"[fatalis] 未知 guards 参数 `%s`", argv[1]);
            return;
        }
        {
            std::lock_guard<std::mutex> lk(g_stateMx);
            g_fatalisGuards = on;
            g_engine.SetTagDisabled("guard", !on);
        }
        if (en) {
            g_host->ChatPrint(on ? "[fatalis] P1 knockdown/occluded/snap guards ON"
                                 : "[fatalis] P1 knockdown/occluded/snap guards OFF (main nodes only)");
        } else {
            g_host->ChatPrint(on ? u8"[fatalis] P1倒地/遮挡/额外车守卫已开启 (46/47/18/129..167)"
                                 : u8"[fatalis] P1倒地/遮挡/额外车守卫已关闭 (仅控制主节点)");
        }
        return;
    }

    g_host->ChatPrintf(en ? "[fatalis] Unknown subcmd `%s`; type `$rng fatalis help`" : u8"[fatalis] 未知子命令 `%s`，输 `$rng fatalis help` 查看用法", argv[0]);
}

void __fastcall Complete_Fatalis(const char*,
                                 int argc,
                                 const char* const* argv,
                                 const char*,
                                 MhwRngCompletionSinkFn addCand,
                                 void* sinkCtx,
                                 void*) {
    if (!addCand) return;
    if (argc == 0) {
        static const char* const kTop[] = {
            "on", "off", "status", "history", "diag", "help", "far", "guards"
        };
        for (const char* t : kTop) addCand(t, sinkCtx);
        return;
    }
    if (argc == 1 && argv && argv[0]) {
        const std::string a0 = thk::LowerAscii(argv[0]);
        if (a0 == "far") {
            addCand("single", sinkCtx);
            addCand("multi", sinkCtx);
        } else if (a0 == "guards" || a0 == "guard") {
            addCand("on", sinkCtx);
            addCand("off", sinkCtx);
        }
    }
}

void __fastcall Complete_Thk(const char*,
                             int argc,
                             const char* const* argv,
                             const char*,
                             MhwRngCompletionSinkFn addCand,
                             void* sinkCtx,
                             void*) {
    if (!addCand) return;
    if (argc == 0) {
        static const char* const kTop[] = {
            "status", "probe", "nodes", "stats", "rule", "seq", "preset", "reset", "clear", "diag", "help"
        };
        for (const char* t : kTop) addCand(t, sinkCtx);
        return;
    }

    const std::string a0 = (argv && argv[0]) ? thk::LowerAscii(argv[0]) : std::string();
    if (argc == 1) {
        if (a0 == "probe") {
            addCand("on", sinkCtx);
            addCand("off", sinkCtx);
            addCand("log", sinkCtx);
        } else if (a0 == "rule" || a0 == "rules") {
            static const char* const kRuleSubs[] = { "add", "list", "del", "clear" };
            for (const char* r : kRuleSubs) addCand(r, sinkCtx);
        } else if (a0 == "preset" || a0 == "presets") {
            static const char* const kPresetSubs[] = { "list", "load", "reload", "off", "tag" };
            for (const char* p : kPresetSubs) addCand(p, sinkCtx);
        } else if (a0 == "seq" || a0 == "sequence") {
            addCand("*", sinkCtx);
        }
        return;
    }

    const std::string a1 = (argv && argv[1]) ? thk::LowerAscii(argv[1]) : std::string();
    if (argc == 2) {
        if ((a0 == "preset" || a0 == "presets") && a1 == "load") {
            addCand("fatalis_p1_nasty", sinkCtx);
            const auto files = thk::ListPresetFiles(GetPresetDir());
            for (const auto& f : files) addCand(f.c_str(), sinkCtx);
        } else if ((a0 == "preset" || a0 == "presets") && a1 == "tag") {
            std::vector<std::string> tags = { "far_single", "far_multi", "guard", "bipedal_lock" };
            {
                std::lock_guard<std::mutex> lk(g_stateMx);
                for (const auto& t : g_engine.disabledTags) tags.push_back(t);
                for (const auto& r : g_engine.presetRules) {
                    if (r.tags[0] == '\0') continue;
                    const std::string s(r.tags);
                    std::size_t pos = 0U;
                    while (pos < s.size()) {
                        const std::size_t comma = s.find(',', pos);
                        const std::string tok = thk::TrimAscii(
                            s.substr(pos, comma == std::string::npos ? std::string::npos : (comma - pos)));
                        if (!tok.empty()) tags.push_back(tok);
                        if (comma == std::string::npos) break;
                        pos = comma + 1U;
                    }
                }
            }
            for (const auto& t : tags) addCand(t.c_str(), sinkCtx);
        } else if ((a0 == "rule" || a0 == "rules") && (a1 == "del" || a1 == "rm" || a1 == "remove")) {
            std::vector<std::string> ids;
            {
                std::lock_guard<std::mutex> lk(g_stateMx);
                for (const auto& r : g_engine.customRules) {
                    ids.push_back(std::to_string(r.ruleId));
                }
            }
            for (const auto& id : ids) addCand(id.c_str(), sinkCtx);
        } else if ((a0 == "rule" || a0 == "rules") && a1 == "add") {
            addCand("*", sinkCtx);
        }
        return;
    }

    if (argc == 3) {
        if ((a0 == "preset" || a0 == "presets") && a1 == "tag") {
            addCand("on", sinkCtx);
            addCand("off", sinkCtx);
        }
        return;
    }

    if ((a0 == "seq" || a0 == "sequence") && argc == 5) {
        static const char* const kSeqMods[] = {
            "loop", "hold", "once", "scope=fired", "scope=node", "scope=total", "after=", "until=",
            "prev=", "prev_d=", "prev_f=", "prev_node=", "prev_seg0=", "prev_caller="
        };
        for (const char* m : kSeqMods) addCand(m, sinkCtx);
        return;
    }
    if ((a0 == "rule" || a0 == "rules") && a1 == "add" && argc >= 6) {
        static const char* const kRuleMods[] = {
            "after=", "until=", "every=", "when=rule", "when=node", "when=total",
            "prev=", "prev_d=", "prev_f=", "prev_node=", "prev_seg0=", "prev_caller="
        };
        for (const char* m : kRuleMods) addCand(m, sinkCtx);
    }
}

const MhwRngPluginDescriptor kPluginDesc = {
    kMhwRngApiVersion,
    "thk",
    u8"通用THK随机节点探测与纯RNG控招引擎",
    "1.4.0",
    u8"探测任意怪物THK节点、记录出招上下文，并支持门控/权重/序列/上下文化prev与声明式预设"
};

} // namespace

MHW_RNG_PLUGIN_EXPORT const MhwRngPluginDescriptor* MhwRngPlugin_GetInfo() {
    return &kPluginDesc;
}

MHW_RNG_PLUGIN_EXPORT bool MhwRngPlugin_Init(const MhwRngHostApi* host, MhwRngPluginId selfId) {
    if (!host || host->apiVersion != kMhwRngApiVersion) return false;
    g_host   = host;
    g_selfId = selfId;

    MhwRng_RegisterPluginInfoBi(
        host, selfId,
        u8"通用THK随机节点探测与纯RNG控招引擎",
        "Universal THK Prober & Pure-RNG AI Controller",
        u8"探测任意怪物THK节点、出招计数，并支持门控/权重/序列/预设控招",
        "Probe any monster THK nodes, count moves & drive count-gated/weighted/seq AI");

    MhwRng_RegisterSiteMetaBi(
        host, selfId,
        thk::kRvaThkUpdateRandom,
        "ai.thk_rand",
        u8"怪物THK行为树随机分支抽取 (由 thk 插件提供探测/计数/序列/预设控招)",
        "Monster AI think-tree (.thk) branch RNG (managed by thk plugin)",
        &FormatThkSlotStatus,
        nullptr);

    host->RegisterSlotHook(selfId, thk::kRvaThkUpdateRandom, &OnThkRandomDraw, nullptr);

    MhwRng_RegisterSubcommandBi(
        host, selfId,
        "thk",
        "$rng thk status|probe|nodes|stats|rule|seq|preset|reset|clear|diag",
        u8"通用THK节点探测、出招计数、序列表与预设控招",
        "Generic THK node probe, move stats, sequences & presets",
        &Cmd_Thk,
        nullptr);
    MhwRng_RegisterSubcommandCompleter(host, selfId, "thk", &Complete_Thk, nullptr);

    MhwRng_RegisterSubcommandBi(
        host, selfId,
        "fatalis",
        "$rng fatalis on|off|status|history|diag|far single|multi|guards on|off",
        u8"黑龙(em013)一阶段二足纯RNG预设快捷入口",
        "Shortcut facade for Fatalis (em013) P1 pure-RNG preset",
        &Cmd_Fatalis,
        nullptr);
    MhwRng_RegisterSubcommandCompleter(host, selfId, "fatalis", &Complete_Fatalis, nullptr);
    g_worldEvQuestEnter  = MhwRng_HostSubscribeEvent(host, selfId, kMhwWorldEvQuestEnter,  &OnWorldEvent, nullptr);
    g_worldEvQuestLeave  = MhwRng_HostSubscribeEvent(host, selfId, kMhwWorldEvQuestLeave,  &OnWorldEvent, nullptr);
    g_worldEvTitleReturn = MhwRng_HostSubscribeEvent(host, selfId, kMhwWorldEvTitleReturn, &OnWorldEvent, nullptr);
    return true;
}

MHW_RNG_PLUGIN_EXPORT void MhwRngPlugin_Shutdown() {
    const MhwRngHostApi* hostCopy = nullptr;
    MhwRngPluginId       selfCopy = 0U;
    MhwRngHookHandle     ev1      = kMhwRngInvalidHandle;
    MhwRngHookHandle     ev2      = kMhwRngInvalidHandle;
    MhwRngHookHandle     ev3      = kMhwRngInvalidHandle;
    {
        std::lock_guard<std::mutex> lk(g_stateMx);
        hostCopy             = g_host;
        selfCopy             = g_selfId;
        ev1                  = g_worldEvQuestEnter;
        ev2                  = g_worldEvQuestLeave;
        ev3                  = g_worldEvTitleReturn;
        g_worldEvQuestEnter  = kMhwRngInvalidHandle;
        g_worldEvQuestLeave  = kMhwRngInvalidHandle;
        g_worldEvTitleReturn = kMhwRngInvalidHandle;
        g_engine             = thk::ThkEngine{};
        g_fatalisFarMulti    = false;
        g_fatalisGuards      = true;
        g_host               = nullptr;
        g_selfId             = 0U;
    }
    MhwRng_HostUnsubscribeEvent(hostCopy, selfCopy, ev1);
    MhwRng_HostUnsubscribeEvent(hostCopy, selfCopy, ev2);
    MhwRng_HostUnsubscribeEvent(hostCopy, selfCopy, ev3);
}
