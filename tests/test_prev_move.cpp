#include "thk_engine.h"
#include "thk_preset_io.h"

#include <cassert>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

static thk::ThkVmContext MakeD0CtxSeg(std::int32_t nodeIdx,
                                      std::int32_t segStart,
                                      std::initializer_list<std::pair<std::int32_t, float>> targets,
                                      std::initializer_list<std::uint32_t> ctrls = {}) {
    thk::ThkVmContext lc{};
    lc.isValid    = true;
    lc.monsterPtr = 0x12340000ULL;
    lc.monsterId  = 101U;
    lc.depth      = 0U;
    lc.thkFileIdx = 0;
    lc.nodeArrIdx = nodeIdx;

    std::int32_t seg = segStart;
    for (auto [tn, w] : targets) {
        auto& c         = lc.cands[lc.candCount++];
        c.segIdx        = seg++;
        c.weight        = w;
        c.ctrlFlags     = thk::kCtrlElser;
        c.targetThkFile = 55;
        c.targetNodeId  = tn;
        lc.totalWeight += w;
    }
    for (std::uint32_t cf : ctrls) {
        auto& c         = lc.cands[lc.candCount++];
        c.segIdx        = seg++;
        c.weight        = 50.0f;
        c.ctrlFlags     = cf;
        c.targetThkFile = -1;
        c.targetNodeId  = -1;
        lc.totalWeight += 50.0f;
    }
    return lc;
}

static thk::ThkVmContext MakeD0Ctx(std::int32_t nodeIdx,
                                   std::initializer_list<std::pair<std::int32_t, float>> targets,
                                   std::initializer_list<std::uint32_t> ctrls = {}) {
    return MakeD0CtxSeg(nodeIdx, 1, targets, ctrls);
}

int main(int argc, char** argv) {
    const char* presetPath = (argc >= 2 && argv[1] && argv[1][0] != '\0') ? argv[1] : "presets/fatalis_sweet.txt";
    std::ifstream ifs(presetPath, std::ios::binary);
    assert(ifs.good());
    std::ostringstream oss;
    oss << ifs.rdbuf();

    thk::ThkPresetDocument doc{};
    std::string err;
    const bool parsed = thk::ParsePresetFromText(oss.str(), "fatalis_sweet", doc, err);
    if (!parsed) {
        std::fprintf(stderr, "ParsePresetFromText failed: %s\n", err.c_str());
        return 1;
    }
    std::printf("[OK] ParsePresetFromText(`%s`): %zu rules parsed cleanly\n", presetPath, doc.rules.size());

    thk::ThkEngine eng;
    eng.activePresetName = doc.name;
    eng.presetRules      = doc.rules;
    assert(eng.AnyActiveRuleNeedsNaturalMoves());

    // Synthetic unit test for prev=55:179 semantics
    thk::ThkEngine unit;
    thk::ThkRule prevRule{};
    prevRule.ruleId              = 1U;
    prevRule.condId              = 2001U;
    prevRule.filter.monsterId    = 101;
    prevRule.filter.depth        = 0;
    prevRule.filter.nodes[0]     = 77;
    prevRule.filter.nodeCount    = 1U;
    prevRule.filter.prevMoveFiles[0] = 55;
    prevRule.filter.prevMoveNodes[0] = 179;
    prevRule.filter.prevMoveCount    = 1U;
    prevRule.stepCount               = 1U;
    prevRule.steps[0].choiceCount    = 1U;
    prevRule.steps[0].choices[0].primary = thk::ThkBranchSpec::Target(55, 274);
    prevRule.steps[0].choices[0].weight  = 100.0f;

    thk::ThkRule stanceEndr{};
    stanceEndr.ruleId             = 2U;
    stanceEndr.condId             = 2002U;
    stanceEndr.filter.monsterId   = 101;
    stanceEndr.filter.depth       = 0;
    stanceEndr.filter.nodes[0]    = 14;
    stanceEndr.filter.nodeCount   = 1U;
    stanceEndr.stepCount          = 1U;
    stanceEndr.steps[0].choiceCount = 1U;
    stanceEndr.steps[0].choices[0].primary = thk::ThkBranchSpec::Ctrl(thk::kCtrlEndr);
    stanceEndr.steps[0].choices[0].weight  = 100.0f;

    unit.customRules.push_back(prevRule);
    unit.customRules.push_back(stanceEndr);
    assert(unit.AnyActiveRuleNeedsNaturalMoves());

    thk::ThkEvalDecision dec{};
    thk::ThkPendingWeightedPick wp{};
    bool needObs = false;
    std::uint32_t nv = 0U;

    const auto n77 = MakeD0Ctx(77, {{179, 50.0f}, {274, 50.0f}});
    const auto n14 = MakeD0Ctx(14, {}, {thk::kCtrlRandom, thk::kCtrlEndr});

    // 1. Before 55:179: prevRule must NOT match
    unit.EvaluateRulesLocked(n77, dec, wp, needObs, nv);
    assert(!dec.shouldOverride && needObs);

    // 2. Vanilla roll lands on 55:179 (cand 0) -> sets lastMove = 55:179 at n77.s1
    unit.RecordObservedVanillaLocked(n77, 0U, 0, nv);
    const auto* trk = unit.FindLatestTracker(101);
    assert(trk && trk->lastMove.valid && trk->lastMove.moveFile == 55 && trk->lastMove.moveNode == 179);
    assert(trk->lastMove.depth == 0U && trk->lastMove.file == 0 && trk->lastMove.node == 77 && trk->lastMove.seg0 == 1);

    // 3. Intervening control-only ENDR (n14) has file=-1 -> must NOT clobber lastMove or its origin context
    unit.EvaluateRulesLocked(n14, dec, wp, needObs, nv);
    assert(dec.shouldOverride);
    assert(trk->lastMove.moveFile == 55 && trk->lastMove.moveNode == 179);
    assert(trk->lastMove.node == 77 && trk->lastMove.seg0 == 1);

    // 4. Next n77 call MUST match prev=55:179 and force 55:274 (cand 1)
    unit.EvaluateRulesLocked(n77, dec, wp, needObs, nv);
    assert(dec.shouldOverride);
    assert(trk->lastMove.moveFile == 55 && trk->lastMove.moveNode == 274);
    assert(trk->ruleCntCount == 0U); // Stateless prev= consumes 0 ruleCounters slots

    // 5. Subsequent n77 call after 55:274 must NOT match prev=55:179
    unit.EvaluateRulesLocked(n77, dec, wp, needObs, nv);
    assert(!dec.shouldOverride);

    std::printf("[OK] prev=55:179 state machine & 0-counter-slot check PASSED!\n");

    // 6. Inline prev_node/prev_seg0 & named multi-slot ctx (`prev_ctx`) tests
    {
        const std::string ctxPreset =
            "name = prev_ctx_test\n"
            "ctx bi_far_tail mon=101 d=0 f=0 node=77 seg0=13..38\n"
            "ctx bi_far_tail mon=101 d=0 f=0 node=83 seg0=13\n"
            "rule mon=101 d=0 f=0 node=77 seg0=* -> 55:179 prev=55:175 prev_ctx=bi_far_tail\n"
            "rule mon=101 d=0 f=0 node=78 seg0=* -> 55:220 prev_ctx=bi_far_tail\n"
            "rule mon=101 d=0 f=0 node=75 seg0=* -> 55:297 prev=55:175 prev_node=75 prev_seg0=36\n";
        thk::ThkPresetDocument ctxDoc{};
        std::string ctxErr;
        assert(thk::ParsePresetFromText(ctxPreset, "prev_ctx_test", ctxDoc, ctxErr));
        assert(ctxDoc.contexts.size() == 1U && ctxDoc.contexts[0].slots.size() == 2U);
        assert(ctxDoc.rules.size() == 3U);

        thk::ThkEngine ce;
        ce.activePresetName = ctxDoc.name;
        ce.presetContexts   = ctxDoc.contexts;
        ce.presetRules      = ctxDoc.rules;
        assert(ce.AnyActiveRuleNeedsNaturalMoves());

        const auto p_n77_s13 = MakeD0CtxSeg(77, 13, {{175, 50.0f}, {179, 50.0f}, {220, 50.0f}});
        const auto p_n77_s50 = MakeD0CtxSeg(77, 50, {{175, 50.0f}, {179, 50.0f}, {220, 50.0f}});
        const auto p_n75_s36 = MakeD0CtxSeg(75, 36, {{175, 50.0f}, {297, 50.0f}});
        const auto p_n78_s1  = MakeD0CtxSeg(78, 1,  {{220, 50.0f}, {179, 50.0f}});

        // 55:175 from n75.s36 should NOT trigger n77 (requires bi_far_tail: n77.s13..38 or n83.s13),
        // and SHOULD trigger n75 (`prev_node=75 prev_seg0=36`).
        ce.RecordObservedVanillaLocked(p_n75_s36, 0U, 0, nv);
        ce.EvaluateRulesLocked(p_n77_s13, dec, wp, needObs, nv);
        assert(!dec.shouldOverride);
        ce.EvaluateRulesLocked(p_n75_s36, dec, wp, needObs, nv);
        assert(dec.shouldOverride && ce.recentControlled[0].targetNodeId == 297);

        // 55:175 from n77.s50 (wrong seg0) should NOT trigger bi_far_tail
        ce.RecordObservedVanillaLocked(p_n77_s50, 0U, 0, nv);
        ce.EvaluateRulesLocked(p_n77_s13, dec, wp, needObs, nv);
        assert(!dec.shouldOverride);

        // 55:175 from n77.s13 SHOULD trigger prev=55:175 prev_ctx=bi_far_tail -> 55:179
        ce.RecordObservedVanillaLocked(p_n77_s13, 0U, 0, nv);
        ce.EvaluateRulesLocked(p_n77_s13, dec, wp, needObs, nv);
        assert(dec.shouldOverride && ce.recentControlled[0].targetNodeId == 179);

        // Context-only `prev_ctx=bi_far_tail` (no `prev=`): 55:220 from n77.s13 matches slot 0 on n78 -> 55:220
        ce.RecordObservedVanillaLocked(p_n77_s13, 0U, 2, nv);
        ce.EvaluateRulesLocked(p_n78_s1, dec, wp, needObs, nv);
        assert(dec.shouldOverride && ce.recentControlled[0].targetNodeId == 220);

        const auto* ctrk = ce.FindLatestTracker(101);
        assert(ctrk && ctrk->ruleCntCount == 0U);

        // Undefined prev_ctx must fail cleanly
        thk::ThkPresetDocument badDoc{};
        std::string badErr;
        assert(!thk::ParsePresetFromText("rule mon=101 d=0 f=0 node=77 seg0=* -> 55:179 prev_ctx=missing\n",
                                         "bad", badDoc, badErr));
        assert(badErr.find("unknown prev_ctx") != std::string::npos);

        std::printf("[OK] Context-aware prev_node/prev_seg0/ctx/prev_ctx checks PASSED!\n");
    }

    // 7. Pure-C++ FIPS 180-4 SHA-256 test vectors & LoadPresetFromFile raw-text capture
    {
        assert(thk::Sha256HexLower(std::string("")) ==
               "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
        assert(thk::Sha256HexLower(std::string("abc")) ==
               "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        assert(thk::Sha256HexLower(std::string("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")) ==
               "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
        // Verify exact 55-byte and 64-byte block padding boundaries
        assert(thk::Sha256HexLower(std::string(55U, 'a')) ==
               "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318");
        assert(thk::Sha256HexLower(std::string(64U, 'a')) ==
               "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb");

        thk::ThkPresetDocument rawDoc{};
        std::string rawText, rawErr;
        assert(thk::LoadPresetFromFile(presetPath, "fatalis_sweet", rawDoc, rawErr, &rawText));
        assert(rawText == oss.str());
        assert(thk::Sha256HexLower(rawText).size() == 64U);
        std::printf("[OK] Pure-C++ SHA-256 vectors & LoadPresetFromFile raw text capture PASSED!\n");
    }
    return 0;
}
