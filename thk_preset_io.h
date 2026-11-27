#pragma once
// ============================================================================
//  Declarative Plain-Text THK Preset Parser & File Scanner (`thk_preset_io.h`)
//  Shared token parsers are reused by both `.txt` presets and `$rng thk` cmds.
// ============================================================================
#include "thk_engine.h"

#include <windows.h>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace thk {

struct ThkPresetDocument {
    std::string                  name{};
    std::string                  desc{};
    std::string                  descEn{};
    std::vector<std::string>     defaultDisabledTags{};
    std::vector<ThkNamedContext> contexts{};
    std::vector<ThkRule>         rules{};
};

inline std::string TrimAscii(const std::string& s) {
    std::size_t a = 0U;
    while (a < s.size() && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) ++a;
    std::size_t b = s.size();
    while (b > a && (s[b - 1U] == ' ' || s[b - 1U] == '\t' || s[b - 1U] == '\r' || s[b - 1U] == '\n')) --b;
    return s.substr(a, b - a);
}

inline bool ParseSignedIntStrict(const std::string& s, std::int32_t& out) noexcept {
    if (s.empty()) return false;
    char* endp = nullptr;
    const long v = std::strtol(s.c_str(), &endp, 10);
    if (endp == s.c_str() || *endp != '\0') return false;
    out = static_cast<std::int32_t>(v);
    return true;
}

inline bool ParseUnsignedIntStrict(const std::string& s, std::uint32_t& out) noexcept {
    if (s.empty() || s[0] == '-') return false;
    char* endp = nullptr;
    const unsigned long v = std::strtoul(s.c_str(), &endp, 10);
    if (endp == s.c_str() || *endp != '\0') return false;
    out = static_cast<std::uint32_t>(v);
    return true;
}

inline bool ParseIntList(const std::string& s, std::int32_t* outArr, std::size_t maxCap, std::uint8_t& outCount) {
    outCount = 0U;
    const std::string t = TrimAscii(s);
    if (t.empty() || t == "*" || LowerAscii(t) == "any") return true;
    std::size_t pos = 0U;
    while (pos <= t.size()) {
        const std::size_t comma = t.find(',', pos);
        const std::string item = TrimAscii(comma == std::string::npos ? t.substr(pos) : t.substr(pos, comma - pos));
        if (!item.empty()) {
            std::int32_t v = 0;
            if (!ParseSignedIntStrict(item, v)) return false;
            if (outCount < maxCap) outArr[outCount++] = v;
        }
        if (comma == std::string::npos) break;
        pos = comma + 1U;
    }
    return outCount > 0U;
}

inline bool ParseMoveList(const std::string& s,
                          std::int32_t* outFiles,
                          std::int32_t* outNodes,
                          std::size_t maxCap,
                          std::uint8_t& outCount) {
    outCount = 0U;
    const std::string t = TrimAscii(s);
    if (t.empty() || t == "*" || LowerAscii(t) == "any") return true;
    std::size_t pos = 0U;
    while (pos <= t.size()) {
        const std::size_t comma = t.find(',', pos);
        const std::string item = TrimAscii(comma == std::string::npos ? t.substr(pos) : t.substr(pos, comma - pos));
        if (!item.empty()) {
            const std::size_t colon = item.find(':');
            if (colon == std::string::npos) return false;
            std::int32_t tf = -1, tn = -1;
            if (!ParseSignedIntStrict(TrimAscii(item.substr(0U, colon)), tf) || tf < 0) return false;
            if (!ParseSignedIntStrict(TrimAscii(item.substr(colon + 1U)), tn) || tn < 0) return false;
            if (outCount >= maxCap) return false;
            outFiles[outCount] = tf;
            outNodes[outCount] = tn;
            ++outCount;
        }
        if (comma == std::string::npos) break;
        pos = comma + 1U;
    }
    return outCount > 0U;
}

// Supports `*`, `any`, single int (`13`), range (`25..999`), or lower-bound (`25+`).
inline bool ParseSeg0Range(const std::string& raw, std::int32_t& outMin, std::int32_t& outMax) {
    const std::string t = LowerAscii(TrimAscii(raw));
    if (t.empty() || t == "*" || t == "any") {
        outMin = -1;
        outMax = -1;
        return true;
    }
    const std::size_t dots = t.find("..");
    if (dots != std::string::npos) {
        const std::string lo = t.substr(0U, dots);
        const std::string hi = t.substr(dots + 2U);
        outMin = -1;
        outMax = -1;
        if (!lo.empty() && !ParseSignedIntStrict(lo, outMin)) return false;
        if (!hi.empty() && !ParseSignedIntStrict(hi, outMax)) return false;
        return true;
    }
    if (!t.empty() && t.back() == '+') {
        if (!ParseSignedIntStrict(t.substr(0U, t.size() - 1U), outMin)) return false;
        outMax = -1;
        return true;
    }
    std::int32_t exact = -1;
    if (!ParseSignedIntStrict(t, exact)) return false;
    outMin = exact;
    outMax = exact;
    return true;
}

// Supports `55:297`, `ENDR`, `RANDOM`, `ELSER`, `seg:10`, `cand:0`, `vanilla`/`pass`/`-`.
inline bool ParseBranchSpec(const std::string& raw, ThkBranchSpec& out) {
    out = ThkBranchSpec{};
    const std::string t = TrimAscii(raw);
    const std::string low = LowerAscii(t);
    if (low.empty()) return false;
    if (low == "vanilla" || low == "pass" || low == "skip" || low == "-") {
        out = ThkBranchSpec::Vanilla();
        return true;
    }
    if (low == "endr")   { out = ThkBranchSpec::Ctrl(kCtrlEndr);   return true; }
    if (low == "random") { out = ThkBranchSpec::Ctrl(kCtrlRandom); return true; }
    if (low == "elser")  { out = ThkBranchSpec::Ctrl(kCtrlElser);  return true; }
    if (low.rfind("seg:", 0U) == 0U) {
        std::int32_t s = -1;
        if (!ParseSignedIntStrict(low.substr(4U), s) || s < 0) return false;
        out = ThkBranchSpec::Seg(s);
        return true;
    }
    if (low.rfind("cand:", 0U) == 0U) {
        std::int32_t c = -1;
        if (!ParseSignedIntStrict(low.substr(5U), c) || c < 0) return false;
        out = ThkBranchSpec::Cand(c);
        return true;
    }
    const std::size_t colon = low.find(':');
    if (colon != std::string::npos) {
        std::int32_t tf = -1, tn = -1;
        if (!ParseSignedIntStrict(low.substr(0U, colon), tf) || tf < 0) return false;
        if (!ParseSignedIntStrict(low.substr(colon + 1U), tn) || tn < 0) return false;
        out = ThkBranchSpec::Target(tf, tn);
        return true;
    }
    return false;
}

inline bool ParseWeightedChoiceItem(const std::string& rawItem, ThkWeightedChoice& outChoice) {
    outChoice = ThkWeightedChoice{};
    std::string item = TrimAscii(rawItem);
    if (item.empty()) return false;

    float w = 1.0f;
    if (item.back() == ')') {
        const std::size_t lp = item.rfind('(');
        if (lp != std::string::npos && lp > 0U && lp + 1U < item.size() - 1U) {
            const std::string wStr = TrimAscii(item.substr(lp + 1U, item.size() - lp - 2U));
            char* endp = nullptr;
            const double parsed = std::strtod(wStr.c_str(), &endp);
            if (endp == wStr.c_str() || *endp != '\0' || !std::isfinite(parsed) || !(parsed > 0.0)) {
                return false;
            }
            w = static_cast<float>(parsed);
            item = TrimAscii(item.substr(0U, lp));
        }
    }

    outChoice.weight = w;
    const std::size_t bar = item.find('|');
    if (bar == std::string::npos) {
        return ParseBranchSpec(item, outChoice.primary);
    }
    return ParseBranchSpec(item.substr(0U, bar), outChoice.primary) &&
           ParseBranchSpec(item.substr(bar + 1U), outChoice.fallback);
}

inline bool ParseActionStep(const std::string& raw, ThkActionStep& out) {
    out = ThkActionStep{};
    std::size_t pos = 0U;
    while (pos <= raw.size()) {
        const std::size_t plus = raw.find('+', pos);
        const std::string item = TrimAscii(plus == std::string::npos ? raw.substr(pos) : raw.substr(pos, plus - pos));
        if (item.empty() || out.choiceCount >= kMaxStepChoices) return false;
        if (!ParseWeightedChoiceItem(item, out.choices[out.choiceCount])) return false;
        ++out.choiceCount;
        if (plus == std::string::npos) break;
        pos = plus + 1U;
    }
    return out.choiceCount > 0U;
}

inline bool ParseSequenceSteps(const std::string& raw, ThkRule& rule) {
    rule.stepCount = 0U;
    std::size_t pos = 0U;
    while (pos <= raw.size()) {
        const std::size_t comma = raw.find(',', pos);
        const std::string item = TrimAscii(comma == std::string::npos ? raw.substr(pos) : raw.substr(pos, comma - pos));
        if (!item.empty()) {
            if (rule.stepCount >= kMaxRuleSteps) return false;
            ThkActionStep st{};
            if (!ParseActionStep(item, st)) return false;
            rule.steps[rule.stepCount++] = st;
        }
        if (comma == std::string::npos) break;
        pos = comma + 1U;
    }
    return rule.stepCount > 0U;
}

inline bool ParseGateKeyVal(const std::string& kLow, const std::string& v, ThkCountGate& gate) {
    const std::string vLow = LowerAscii(v);
    if (kLow == "when") {
        if (vLow == "always") { gate.scope = ThkCountScope::kAlways; return true; }
        if (vLow == "rule" || vLow == "matched") { gate.scope = ThkCountScope::kRuleMatched; return true; }
        if (vLow == "node") { gate.scope = ThkCountScope::kNodeVisits; return true; }
        if (vLow == "total") { gate.scope = ThkCountScope::kMonsterTotal; return true; }
        if (vLow.rfind("move:", 0U) == 0U) {
            const std::size_t c = vLow.find(':', 5U);
            if (c == std::string::npos) return false;
            if (!ParseSignedIntStrict(vLow.substr(5U, c - 5U), gate.refMoveFile)) return false;
            if (!ParseSignedIntStrict(vLow.substr(c + 1U), gate.refMoveNode)) return false;
            gate.scope = ThkCountScope::kMoveHits;
            return true;
        }
        return false;
    }
    if (kLow == "after" || kLow == "min") {
        std::uint32_t u = 0U;
        if (!ParseUnsignedIntStrict(vLow, u)) return false;
        gate.minCount = u;
        if (gate.scope == ThkCountScope::kAlways && u > 0U) gate.scope = ThkCountScope::kRuleMatched;
        return true;
    }
    if (kLow == "until" || kLow == "max") {
        std::uint32_t u = 0U;
        if (!ParseUnsignedIntStrict(vLow, u)) return false;
        gate.maxCount = u;
        if (gate.scope == ThkCountScope::kAlways && u > 0U) gate.scope = ThkCountScope::kRuleMatched;
        return true;
    }
    if (kLow == "every") {
        const std::size_t col = vLow.find(':');
        if (col == std::string::npos) {
            if (!ParseUnsignedIntStrict(vLow, gate.everyPeriod) || gate.everyPeriod == 0U) return false;
            gate.everyPhase = 0U;
        } else {
            if (!ParseUnsignedIntStrict(vLow.substr(0U, col), gate.everyPeriod) || gate.everyPeriod == 0U) return false;
            if (!ParseUnsignedIntStrict(vLow.substr(col + 1U), gate.everyPhase)) return false;
        }
        if (gate.scope == ThkCountScope::kAlways) gate.scope = ThkCountScope::kRuleMatched;
        return true;
    }
    return false;
}

inline std::vector<std::string> TokenizeRuleLine(const std::string& line) {
    std::vector<std::string> tokens;
    std::size_t i = 0U;
    while (i < line.size()) {
        while (i < line.size() && (line[i] == ' ' || line[i] == '\t')) ++i;
        if (i >= line.size() || line[i] == '#') break;
        std::string tok;
        bool inQuote = false;
        while (i < line.size()) {
            const char c = line[i];
            if (c == '"') {
                inQuote = !inQuote;
                ++i;
                continue;
            }
            if (!inQuote && (c == ' ' || c == '\t' || c == '#')) break;
            tok.push_back(c);
            ++i;
        }
        if (!tok.empty()) tokens.push_back(std::move(tok));
    }
    return tokens;
}

inline bool ParsePresetFromText(const std::string& content,
                                const std::string& fallbackName,
                                ThkPresetDocument& outDoc,
                                std::string& outErr) {
    outDoc      = ThkPresetDocument{};
    outDoc.name = fallbackName;
    outErr.clear();

    std::istringstream iss(content);
    std::string rawLine;
    std::size_t lineNo = 0U;
    std::uint32_t nextRuleId = 10001U;

    while (std::getline(iss, rawLine)) {
        ++lineNo;
        // Strip inline `#` comments outside quotes by truncating header/meta cleanly
        const std::string trimmed = TrimAscii(rawLine);
        if (trimmed.empty() || trimmed[0] == '#' || trimmed[0] == ';') continue;

        if (trimmed[0] == '[') continue; // Section headers like `[meta]` / `[rules]`

        const std::size_t eq = trimmed.find('=');
        const std::string lowerHead = LowerAscii(trimmed);
        if (lowerHead.rfind("rule ", 0U) != 0U && lowerHead.rfind("rule\t", 0U) != 0U &&
            lowerHead.rfind("ctx ", 0U) != 0U && lowerHead.rfind("ctx\t", 0U) != 0U &&
            eq != std::string::npos) {
            const std::string k = LowerAscii(TrimAscii(trimmed.substr(0U, eq)));
            const std::string v = TrimAscii(trimmed.substr(eq + 1U));
            if (k == "name") { outDoc.name = v; continue; }
            if (k == "desc" || k == "description") { outDoc.desc = v; continue; }
            if (k == "desc_en" || k == "description_en") { outDoc.descEn = v; continue; }
            if (k == "disable_tag" || k == "disable_tags") {
                std::size_t p = 0U;
                while (p <= v.size()) {
                    const std::size_t c = v.find(',', p);
                    const std::string item = LowerAscii(TrimAscii(c == std::string::npos ? v.substr(p) : v.substr(p, c - p)));
                    if (!item.empty()) outDoc.defaultDisabledTags.push_back(item);
                    if (c == std::string::npos) break;
                    p = c + 1U;
                }
                continue;
            }
        }

        const auto toks = TokenizeRuleLine(trimmed);
        if (toks.empty()) continue;
        const std::string headLow = LowerAscii(toks[0]);
        if (headLow == "ctx") {
            if (toks.size() < 3U || toks[1].find('=') != std::string::npos) {
                outErr = "line " + std::to_string(lineNo) + ": expected `ctx <name> <key=val>...`";
                return false;
            }
            const std::string ctxNameLow = LowerAscii(TrimAscii(toks[1]));
            if (ctxNameLow.empty()) {
                outErr = "line " + std::to_string(lineNo) + ": empty ctx name";
                return false;
            }
            ThkContextSlot slot{};
            for (std::size_t t = 2U; t < toks.size(); ++t) {
                const std::string& tok = toks[t];
                const std::size_t posEq = tok.find('=');
                if (posEq == std::string::npos) {
                    outErr = "line " + std::to_string(lineNo) + ": ctx expected key=value near '" + tok + "'";
                    return false;
                }
                const std::string kLow = LowerAscii(tok.substr(0U, posEq));
                const std::string val  = tok.substr(posEq + 1U);
                const std::string vLow = LowerAscii(val);
                if (kLow == "mon" || kLow == "monster") {
                    if (vLow == "*" || vLow == "any") slot.monsterId = -1;
                    else if (!ParseSignedIntStrict(vLow, slot.monsterId)) {
                        outErr = "line " + std::to_string(lineNo) + ": ctx bad mon=" + val;
                        return false;
                    }
                } else if (kLow == "d" || kLow == "depth") {
                    if (vLow == "*" || vLow == "any") slot.depth = -1;
                    else if (!ParseSignedIntStrict(vLow, slot.depth)) {
                        outErr = "line " + std::to_string(lineNo) + ": ctx bad depth=" + val;
                        return false;
                    }
                } else if (kLow == "f" || kLow == "file") {
                    if (!ParseIntList(vLow, slot.files, kMaxFilterItems, slot.fileCount)) {
                        outErr = "line " + std::to_string(lineNo) + ": ctx bad file=" + val;
                        return false;
                    }
                } else if (kLow == "node" || kLow == "nodes") {
                    if (!ParseIntList(vLow, slot.nodes, kMaxFilterItems, slot.nodeCount)) {
                        outErr = "line " + std::to_string(lineNo) + ": ctx bad node=" + val;
                        return false;
                    }
                } else if (kLow == "seg0") {
                    if (!ParseSeg0Range(vLow, slot.seg0Min, slot.seg0Max)) {
                        outErr = "line " + std::to_string(lineNo) + ": ctx bad seg0=" + val;
                        return false;
                    }
                } else if (kLow == "caller" || kLow == "callers") {
                    if (!ParseIntList(vLow, slot.callers, kMaxFilterItems, slot.callerCount)) {
                        outErr = "line " + std::to_string(lineNo) + ": ctx bad caller=" + val;
                        return false;
                    }
                } else if (kLow == "move" || kLow == "moves") {
                    if (!ParseMoveList(vLow, slot.moveFiles, slot.moveNodes, kMaxFilterItems, slot.moveCount)) {
                        outErr = "line " + std::to_string(lineNo) + ": ctx bad move=" + val;
                        return false;
                    }
                } else {
                    outErr = "line " + std::to_string(lineNo) + ": unknown ctx key '" + kLow + "'";
                    return false;
                }
            }
            std::int32_t foundCtx = -1;
            for (std::size_t ci = 0U; ci < outDoc.contexts.size(); ++ci) {
                if (ctxNameLow == outDoc.contexts[ci].name) {
                    foundCtx = static_cast<std::int32_t>(ci);
                    break;
                }
            }
            if (foundCtx < 0) {
                ThkNamedContext nc{};
                CopyTrunc(nc.name, sizeof(nc.name), ctxNameLow.c_str());
                nc.slots.push_back(slot);
                outDoc.contexts.push_back(std::move(nc));
            } else {
                outDoc.contexts[static_cast<std::size_t>(foundCtx)].slots.push_back(slot);
            }
            continue;
        }
        if (headLow != "rule") {
            outErr = "line " + std::to_string(lineNo) + ": unknown directive '" + toks[0] + "'";
            return false;
        }

        ThkRule r{};
        r.ruleId = nextRuleId++;
        r.condId = 2000U + (r.ruleId - 10000U);

        for (std::size_t t = 1U; t < toks.size(); ++t) {
            const std::string& tok = toks[t];
            if (tok == "->") {
                if (t + 1U >= toks.size() || !ParseSequenceSteps(toks[t + 1U], r)) {
                    outErr = "line " + std::to_string(lineNo) + ": invalid target after '->'";
                    return false;
                }
                ++t;
                continue;
            }
            const std::size_t posEq = tok.find('=');
            if (posEq == std::string::npos) {
                outErr = "line " + std::to_string(lineNo) + ": expected key=value near '" + tok + "'";
                return false;
            }
            const std::string kLow = LowerAscii(tok.substr(0U, posEq));
            const std::string val  = tok.substr(posEq + 1U);
            const std::string vLow = LowerAscii(val);

            if (kLow == "mon" || kLow == "monster") {
                if (vLow == "*" || vLow == "any") r.filter.monsterId = -1;
                else if (!ParseSignedIntStrict(vLow, r.filter.monsterId)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad mon=" + val;
                    return false;
                }
            } else if (kLow == "d" || kLow == "depth") {
                if (vLow == "*" || vLow == "any") r.filter.depth = -1;
                else if (!ParseSignedIntStrict(vLow, r.filter.depth)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad depth=" + val;
                    return false;
                }
            } else if (kLow == "f" || kLow == "file") {
                if (!ParseIntList(vLow, r.filter.thkFiles, kMaxFilterItems, r.filter.thkFileCount)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad file=" + val;
                    return false;
                }
            } else if (kLow == "node" || kLow == "nodes") {
                if (!ParseIntList(vLow, r.filter.nodes, kMaxFilterItems, r.filter.nodeCount)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad node=" + val;
                    return false;
                }
            } else if (kLow == "seg0") {
                if (!ParseSeg0Range(vLow, r.filter.seg0Min, r.filter.seg0Max)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad seg0=" + val;
                    return false;
                }
            } else if (kLow == "caller" || kLow == "callers") {
                if (!ParseIntList(vLow, r.filter.callers, kMaxFilterItems, r.filter.callerCount)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad caller=" + val;
                    return false;
                }
            } else if (kLow == "prev" || kLow == "prev_move") {
                if (!ParseMoveList(vLow, r.filter.prevMoveFiles, r.filter.prevMoveNodes,
                                   kMaxFilterItems, r.filter.prevMoveCount)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad prev=" + val;
                    return false;
                }
            } else if (kLow == "prev_d" || kLow == "prev_depth") {
                if (vLow == "*" || vLow == "any") r.filter.prevDepth = -1;
                else if (!ParseSignedIntStrict(vLow, r.filter.prevDepth) || r.filter.prevDepth < 0) {
                    outErr = "line " + std::to_string(lineNo) + ": bad prev_d=" + val;
                    return false;
                }
            } else if (kLow == "prev_f" || kLow == "prev_file") {
                if (!ParseIntList(vLow, r.filter.prevFiles, kMaxFilterItems, r.filter.prevFileCount)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad prev_f=" + val;
                    return false;
                }
            } else if (kLow == "prev_node" || kLow == "prev_nodes") {
                if (!ParseIntList(vLow, r.filter.prevNodes, kMaxFilterItems, r.filter.prevNodeCount)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad prev_node=" + val;
                    return false;
                }
            } else if (kLow == "prev_seg0") {
                if (!ParseSeg0Range(vLow, r.filter.prevSeg0Min, r.filter.prevSeg0Max)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad prev_seg0=" + val;
                    return false;
                }
            } else if (kLow == "prev_caller" || kLow == "prev_callers") {
                if (!ParseIntList(vLow, r.filter.prevCallers, kMaxFilterItems, r.filter.prevCallerCount)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad prev_caller=" + val;
                    return false;
                }
            } else if (kLow == "prev_ctx") {
                std::int32_t foundCtx = -1;
                for (std::size_t ci = 0U; ci < outDoc.contexts.size(); ++ci) {
                    if (vLow == outDoc.contexts[ci].name) {
                        foundCtx = static_cast<std::int32_t>(ci);
                        break;
                    }
                }
                if (foundCtx < 0) {
                    outErr = "line " + std::to_string(lineNo) + ": unknown prev_ctx '" + val + "'";
                    return false;
                }
                r.filter.prevCtxId = foundCtx;
            } else if (kLow == "seq" || kLow == "target") {
                if (!ParseSequenceSteps(val, r)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad seq/target=" + val;
                    return false;
                }
            } else if (kLow == "end") {
                if (vLow == "loop") r.seqEnd = ThkSeqEndMode::kLoop;
                else if (vLow == "hold") r.seqEnd = ThkSeqEndMode::kHoldLast;
                else if (vLow == "once" || vLow == "vanilla") r.seqEnd = ThkSeqEndMode::kFallbackVanilla;
                else { outErr = "line " + std::to_string(lineNo) + ": bad end=" + val; return false; }
            } else if (kLow == "scope") {
                if (vLow == "fired" || vLow == "rule") r.seqScope = ThkSeqScope::kRuleFired;
                else if (vLow == "node") r.seqScope = ThkSeqScope::kNodeVisits;
                else if (vLow == "total") r.seqScope = ThkSeqScope::kMonsterTotal;
                else { outErr = "line " + std::to_string(lineNo) + ": bad scope=" + val; return false; }
            } else if (kLow == "cond") {
                if (!ParseUnsignedIntStrict(vLow, r.condId)) {
                    outErr = "line " + std::to_string(lineNo) + ": bad cond=" + val;
                    return false;
                }
            } else if (kLow == "tag" || kLow == "tags") {
                CopyTrunc(r.tags, sizeof(r.tags), vLow.c_str());
            } else if (kLow == "label") {
                CopyTrunc(r.label, sizeof(r.label), val.c_str());
            } else if (kLow == "label_en") {
                CopyTrunc(r.labelEn, sizeof(r.labelEn), val.c_str());
            } else if (!ParseGateKeyVal(kLow, val, r.gate)) {
                outErr = "line " + std::to_string(lineNo) + ": unknown key '" + kLow + "'";
                return false;
            }
        }

        if (r.stepCount == 0U) {
            outErr = "line " + std::to_string(lineNo) + ": rule missing target (`-> ...` or `seq=...`)";
            return false;
        }
        if (r.label[0] == '\0') {
            std::snprintf(r.label, sizeof(r.label), "preset#%u", static_cast<unsigned>(outDoc.rules.size() + 1U));
        }
        outDoc.rules.push_back(r);
    }
    return true;
}

// Compact pure-C++ FIPS 180-4 SHA-256 (lowercase 64-hex, zero external DLL deps).
inline std::string Sha256HexLower(const void* data, std::size_t len) {
    static constexpr std::uint32_t kK[64] = {
        0x428A2F98U, 0x71374491U, 0xB5C0FBCFU, 0xE9B5DBA5U,
        0x3956C25BU, 0x59F111F1U, 0x923F82A4U, 0xAB1C5ED5U,
        0xD807AA98U, 0x12835B01U, 0x243185BEU, 0x550C7DC3U,
        0x72BE5D74U, 0x80DEB1FEU, 0x9BDC06A7U, 0xC19BF174U,
        0xE49B69C1U, 0xEFBE4786U, 0x0FC19DC6U, 0x240CA1CCU,
        0x2DE92C6FU, 0x4A7484AAU, 0x5CB0A9DCU, 0x76F988DAU,
        0x983E5152U, 0xA831C66DU, 0xB00327C8U, 0xBF597FC7U,
        0xC6E00BF3U, 0xD5A79147U, 0x06CA6351U, 0x14292967U,
        0x27B70A85U, 0x2E1B2138U, 0x4D2C6DFCU, 0x53380D13U,
        0x650A7354U, 0x766A0ABBU, 0x81C2C92EU, 0x92722C85U,
        0xA2BFE8A1U, 0xA81A664BU, 0xC24B8B70U, 0xC76C51A3U,
        0xD192E819U, 0xD6990624U, 0xF40E3585U, 0x106AA070U,
        0x19A4C116U, 0x1E376C08U, 0x2748774CU, 0x34B0BCB5U,
        0x391C0CB3U, 0x4ED8AA4AU, 0x5B9CCA4FU, 0x682E6FF3U,
        0x748F82EEU, 0x78A5636FU, 0x84C87814U, 0x8CC70208U,
        0x90BEFFFAU, 0xA4506CEBU, 0xBEF9A3F7U, 0xC67178F2U
    };
    std::uint32_t h[8] = {
        0x6A09E667U, 0xBB67AE85U, 0x3C6EF372U, 0xA54FF53AU,
        0x510E527FU, 0x9B05688CU, 0x1F83D9ABU, 0x5BE0CD19U
    };

    auto rotr = [](std::uint32_t x, std::uint32_t n) noexcept -> std::uint32_t {
        return (x >> n) | (x << (32U - n));
    };

    auto compress = [&](const std::uint8_t blk[64]) noexcept {
        std::uint32_t w[64]{};
        for (std::size_t i = 0U; i < 16U; ++i) {
            w[i] = (static_cast<std::uint32_t>(blk[i * 4U + 0U]) << 24) |
                   (static_cast<std::uint32_t>(blk[i * 4U + 1U]) << 16) |
                   (static_cast<std::uint32_t>(blk[i * 4U + 2U]) << 8)  |
                   (static_cast<std::uint32_t>(blk[i * 4U + 3U]));
        }
        for (std::size_t i = 16U; i < 64U; ++i) {
            const std::uint32_t s0 = rotr(w[i - 15U], 7U) ^ rotr(w[i - 15U], 18U) ^ (w[i - 15U] >> 3U);
            const std::uint32_t s1 = rotr(w[i - 2U], 17U) ^ rotr(w[i - 2U], 19U) ^ (w[i - 2U] >> 10U);
            w[i] = w[i - 16U] + s0 + w[i - 7U] + s1;
        }
        std::uint32_t a = h[0], b = h[1], c = h[2], d = h[3];
        std::uint32_t e = h[4], f = h[5], g = h[6], k = h[7];
        for (std::size_t i = 0U; i < 64U; ++i) {
            const std::uint32_t S1  = rotr(e, 6U) ^ rotr(e, 11U) ^ rotr(e, 25U);
            const std::uint32_t ch  = (e & f) ^ ((~e) & g);
            const std::uint32_t t1  = k + S1 + ch + kK[i] + w[i];
            const std::uint32_t S0  = rotr(a, 2U) ^ rotr(a, 13U) ^ rotr(a, 22U);
            const std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            const std::uint32_t t2  = S0 + maj;
            k = g;
            g = f;
            f = e;
            e = d + t1;
            d = c;
            c = b;
            b = a;
            a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d;
        h[4] += e; h[5] += f; h[6] += g; h[7] += k;
    };

    const auto* bytes = reinterpret_cast<const std::uint8_t*>(data);
    std::size_t off = 0U;
    while (off + 64U <= len) {
        compress(bytes + off);
        off += 64U;
    }

    std::uint8_t tail[64]{};
    const std::size_t rem = len - off;
    if (rem > 0U && bytes) {
        std::memcpy(tail, bytes + off, rem);
    }
    tail[rem] = 0x80U;
    if (rem >= 56U) {
        compress(tail);
        std::memset(tail, 0, sizeof(tail));
    }
    const std::uint64_t bitLen = static_cast<std::uint64_t>(len) * 8ULL;
    for (std::size_t i = 0U; i < 8U; ++i) {
        tail[56U + i] = static_cast<std::uint8_t>((bitLen >> (56U - i * 8U)) & 0xFFULL);
    }
    compress(tail);

    static constexpr char kHex[16] = {
        '0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f'
    };
    std::string out(64U, '0');
    for (std::size_t i = 0U; i < 8U; ++i) {
        for (std::size_t j = 0U; j < 8U; ++j) {
            out[i * 8U + j] = kHex[(h[i] >> (28U - j * 4U)) & 0xFU];
        }
    }
    return out;
}

inline std::string Sha256HexLower(const std::string& s) {
    return Sha256HexLower(s.data(), s.size());
}

inline bool LoadPresetFromFile(const std::string& path,
                               const std::string& fallbackName,
                               ThkPresetDocument& outDoc,
                               std::string& outErr,
                               std::string* outRawText = nullptr) {
    std::ifstream ifs(path, std::ios::binary);
    if (!ifs.is_open()) {
        outErr = "cannot open file: " + path;
        return false;
    }
    std::ostringstream ss;
    ss << ifs.rdbuf();
    std::string raw = ss.str();
    if (!ParsePresetFromText(raw, fallbackName, outDoc, outErr)) {
        return false;
    }
    if (outRawText) {
        *outRawText = std::move(raw);
    }
    return true;
}

inline std::vector<std::string> ListPresetFiles(const std::string& dir) {
    std::vector<std::string> names;
    if (dir.empty()) return names;
    const std::string pat = dir + "\\*.txt";
    WIN32_FIND_DATAA fd{};
    HANDLE h = ::FindFirstFileA(pat.c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return names;
    do {
        if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) continue;
        std::string fn(fd.cFileName);
        if (fn.size() > 4U) {
            fn.resize(fn.size() - 4U);
            names.push_back(std::move(fn));
        }
    } while (::FindNextFileA(h, &fd));
    ::FindClose(h);
    return names;
}

} // namespace thk
