#pragma once
// ============================================================================
//  MIRA Optional THK AI Controller Service ABI (`rng_thk.dll`)
//  Service Name : "mhw.thk" (v1)
//
//  Strict Pure-POD C ABI (zero `<windows.h>` dependency).
//  Allows peer plugins (e.g. deterministic TAS recorders/replayers) to query
//  cumulative THK decision counters, export full active THK preset text +
//  SHA-256 + disabled tags + canonical custom rule/seq commands, and apply
//  configurations atomically in-memory without touching disk preset files.
// ============================================================================
#include "mhw_rng_api.h"

#include <cstddef>
#include <cstdint>

constexpr const char*   kMhwThkServiceName    = "mhw.thk";
constexpr std::uint32_t kMhwThkApiVersion     = 1U;
constexpr std::uint32_t kMhwThkServiceVersion = kMhwThkApiVersion;

#pragma pack(push, 8)
struct MhwThkServiceApi {
    std::uint32_t structSize; // sizeof(MhwThkServiceApi)
    std::uint32_t apiVersion; // == kMhwThkApiVersion (1)

    // Returns cumulative `totalDecisions` across all active monster trackers.
    std::uint64_t (__fastcall *GetTotalDecisions)();

    // Exports active preset name (empty if none), 64-hex lowercase SHA-256 of
    // the active preset raw text, comma-separated sorted disabled tags,
    // verbatim raw preset text body, and newline-separated canonical replayable
    // custom `rule add ...` / `seq ...` commands.
    //
    // Buffer-size probing & truncation semantics:
    //   - `outBodyNeed` / `outCmdsNeed` (when non-null) always receive the
    //     required byte capacity including NUL terminator (`len + 1`), or `0`
    //     when the corresponding string is empty.
    //   - Passing `nullptr` for any output buffer probes/skips that field
    //     without failing.
    //   - Any non-null output buffer with `cap > 0` is always NUL-terminated.
    //   - Returns `false` if any non-null output buffer is too small (still
    //     populating `*outBodyNeed` / `*outCmdsNeed`).
    bool (__fastcall *GetConfigSnapshot)(
        char*        outPresetName,
        std::size_t  nameCap,
        char*        outPresetSha256,
        std::size_t  shaCap,
        char*        outDisabledTags,
        std::size_t  tagsCap,
        char*        outPresetBody,
        std::size_t  bodyCap,
        std::size_t* outBodyNeed,
        char*        outCustomCmds,
        std::size_t  cmdsCap,
        std::size_t* outCmdsNeed);

    // Atomically clears active preset, disabled tags & custom rules, then:
    //   1. If `presetBodyOrNull` is non-empty: parses in-memory without writing
    //      to disk `thk_presets\*.txt`, storing preset name, raw body & SHA-256.
    //   2. Else if `presetName` is non-empty: loads preset via disk/builtin.
    //   3. Applies `disabledTagsCsvOrNull` (comma-separated; non-null replaces
    //      preset default disabled tags, `nullptr` preserves preset defaults).
    //   4. Replays each non-empty line in `customCmdsOrNull` (`rule add ...` /
    //      `seq ...`, ignoring `#` / `;` comment lines) quietly (no chat spam).
    //   5. Resets runtime trackers and syncs MIRA slot/mode cleanly without
    //      forcing `OFF` when `TAKEOVER` is active.
    // On parse/load error, live THK state is NOT modified and `outErr` is set.
    bool (__fastcall *ApplyConfig)(
        const char* presetName,
        const char* presetBodyOrNull,
        const char* disabledTagsCsvOrNull,
        const char* customCmdsOrNull,
        char*       outErr,
        std::size_t errCap);
};
#pragma pack(pop)

// ---- Inline Service Convenience Helpers (Graceful No-Op when `rng_thk.dll` is unloaded) ----

inline bool MhwThk_TryGetTotalDecisions(const MhwRngHostApi* host,
                                        std::uint64_t* outDecisions) noexcept {
    if (!host || !outDecisions) return false;
    MhwRngServiceGuard<MhwThkServiceApi> thk(host, kMhwThkServiceName, kMhwThkApiVersion);
    if (!thk || !thk->GetTotalDecisions) return false;
    *outDecisions = thk->GetTotalDecisions();
    return true;
}

inline bool MhwThk_TryGetConfigSnapshot(const MhwRngHostApi* host,
                                        char*        outPresetName,
                                        std::size_t  nameCap,
                                        char*        outPresetSha256,
                                        std::size_t  shaCap,
                                        char*        outDisabledTags,
                                        std::size_t  tagsCap,
                                        char*        outPresetBody,
                                        std::size_t  bodyCap,
                                        std::size_t* outBodyNeed,
                                        char*        outCustomCmds,
                                        std::size_t  cmdsCap,
                                        std::size_t* outCmdsNeed) noexcept {
    if (!host) return false;
    MhwRngServiceGuard<MhwThkServiceApi> thk(host, kMhwThkServiceName, kMhwThkApiVersion);
    if (!thk || !thk->GetConfigSnapshot) return false;
    return thk->GetConfigSnapshot(outPresetName, nameCap,
                                  outPresetSha256, shaCap,
                                  outDisabledTags, tagsCap,
                                  outPresetBody, bodyCap, outBodyNeed,
                                  outCustomCmds, cmdsCap, outCmdsNeed);
}

inline bool MhwThk_TryApplyConfig(const MhwRngHostApi* host,
                                  const char* presetName,
                                  const char* presetBodyOrNull,
                                  const char* disabledTagsCsvOrNull,
                                  const char* customCmdsOrNull,
                                  char*       outErr,
                                  std::size_t errCap) noexcept {
    if (!host) return false;
    MhwRngServiceGuard<MhwThkServiceApi> thk(host, kMhwThkServiceName, kMhwThkApiVersion);
    if (!thk || !thk->ApplyConfig) return false;
    return thk->ApplyConfig(presetName, presetBodyOrNull,
                            disabledTagsCsvOrNull, customCmdsOrNull,
                            outErr, errCap);
}
