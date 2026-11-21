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
    std::string              name{};
    std::string              desc{};
    std::vector<std::string> defaultDisabledTags{};
    std::vector<ThkRule>     rules{};
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

inline bool ParseActionStep(const std::string& raw, ThkActionStep& out) {
    out = ThkActionStep{};
    const std::size_t bar = raw.find('|');
    if (bar == std::string::npos) {
        return ParseBranchSpec(raw, out.primary);
    }
    return ParseBranchSpec(raw.substr(0U, bar), out.primary) &&
           ParseBranchSpec(raw.substr(bar + 1U), out.fallback);
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
        if (lowerHead.rfind("rule ", 0U) != 0U && lowerHead.rfind("rule\t", 0U) != 0U && eq != std::string::npos) {
            const std::string k = LowerAscii(TrimAscii(trimmed.substr(0U, eq)));
            const std::string v = TrimAscii(trimmed.substr(eq + 1U));
            if (k == "name") { outDoc.name = v; continue; }
            if (k == "desc" || k == "description") { outDoc.desc = v; continue; }
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
        if (LowerAscii(toks[0]) != "rule") {
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
                if (vLow == "*" || vLow == "any") r.filter.thkFileIdx = -1;
                else if (!ParseSignedIntStrict(vLow, r.filter.thkFileIdx)) {
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

inline bool LoadPresetFromFile(const std::string& path,
                               const std::string& fallbackName,
                               ThkPresetDocument& outDoc,
                               std::string& outErr) {
    std::ifstream ifs(path, std::ios::binary);
    if (!ifs.is_open()) {
        outErr = "cannot open file: " + path;
        return false;
    }
    std::ostringstream ss;
    ss << ifs.rdbuf();
    return ParsePresetFromText(ss.str(), fallbackName, outDoc, outErr);
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
