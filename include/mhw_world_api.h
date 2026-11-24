#pragma once
// ============================================================================
//  MIRA Built-in World Context & Event Service ABI (`mhw_world.dll`)
//  Service Name : "mhw.world" (v1)
//  Event Topics : "mhw.world.*"
// ============================================================================
#include "mhw_rng_api.h"

#include <cstddef>
#include <cstdint>

constexpr const char*   kMhwWorldServiceName    = "mhw.world";
constexpr std::uint32_t kMhwWorldServiceVersion = 1U;

// Canonical Event Bus topics emitted by `mhw_world.dll` (payload = `MhwWorldEventPayload`)
constexpr const char* kMhwWorldEvSceneEnter     = "mhw.world.scene_enter";
constexpr const char* kMhwWorldEvTitleReturn    = "mhw.world.title_return";
constexpr const char* kMhwWorldEvQuestEnter     = "mhw.world.quest_enter";
constexpr const char* kMhwWorldEvQuestLeave     = "mhw.world.quest_leave";
constexpr const char* kMhwWorldEvStageChanged   = "mhw.world.stage_changed";
constexpr const char* kMhwWorldEvPlayerChanged  = "mhw.world.player_changed";
constexpr const char* kMhwWorldEvHourChanged    = "mhw.world.hour_changed";
constexpr const char* kMhwWorldEvWeatherChanged = "mhw.world.weather_changed";

enum MhwWorldTransitionFlags : std::uint32_t {
    kMhwWorldTransSceneEnter    = 1U << 0,
    kMhwWorldTransTitleReturn   = 1U << 1,
    kMhwWorldTransQuestEnter    = 1U << 2,
    kMhwWorldTransQuestLeave    = 1U << 3,
    kMhwWorldTransStageChanged  = 1U << 4,
    kMhwWorldTransPlayerChanged = 1U << 5,
    kMhwWorldTransHourChanged   = 1U << 6,
    kMhwWorldTransWeatherChange = 1U << 7
};

#pragma pack(push, 8)
struct MhwWorldState {
    std::uint32_t  structSize;       // sizeof(MhwWorldState)
    std::uint8_t   inScene;          // 1 when player entity is active in Hub or Quest
    std::uint8_t   inQuest;          // 1 when `sQuest::isInQuest` conditions hold
    std::uint8_t   isVillage;        // 1 when `sMhArea+0xD2EA != 0`
    std::uint8_t   isGuidingLands;   // 1 when `sQuest+0xAE3B != 0` or `stageId == 109`
    std::uint8_t   isSpecialWeather; // 1 when `weatherId != 0 && weatherId != 10`
    std::uint8_t   hasTime;
    std::uint8_t   hasQuest;
    std::uint8_t   hasArea;
    std::uint32_t  questId;
    std::uint32_t  questState;
    std::uint32_t  stageId;
    std::uint32_t  weatherId;
    std::int32_t   questElapsedTicks;
    float          gameHour;
    std::int32_t   gameHourInt;
    std::uint8_t   glRegionLevels[6];
    std::uint8_t   pad0[2];
    std::uintptr_t localPlayerPtr;
    std::uint64_t  sessionGen;       // Monotonic counter incremented on scene/quest transitions
    std::uint64_t  tickCount;
};

struct MhwWorldEventPayload {
    std::uint32_t structSize;        // sizeof(MhwWorldEventPayload)
    std::uint32_t transitionFlags;   // Bitmask of `MhwWorldTransitionFlags`
    std::uint64_t sessionGen;
    MhwWorldState prev;
    MhwWorldState curr;
};

struct MhwWorldApi {
    std::uint32_t  apiVersion;       // == 1
    std::uint32_t  structSize;       // sizeof(MhwWorldApi)
    bool           (__fastcall *GetState)(MhwWorldState* outState);
    bool           (__fastcall *RefreshStateNow)(MhwWorldState* outState);
    std::uint64_t  (__fastcall *GetSessionGen)();
    bool           (__fastcall *DisplayPopupAsync)(const char* utf8Msg,
                                                   float durationSec,
                                                   float delaySec,
                                                   std::uint8_t flags);
    bool           (__fastcall *GetGuidingLandsLevel)(std::uint32_t regionIdx,
                                                      std::uint8_t* outLevel);
    bool           (__fastcall *FormatWorldSummary)(char* outBuf,
                                                    std::size_t bufCap,
                                                    bool english);
};
#pragma pack(pop)

// ---- Inline Service Convenience Helpers (Graceful No-Op when `mhw_world.dll` is unloaded) ----

inline bool MhwWorld_TryGetState(const MhwRngHostApi* host, MhwWorldState* outState) noexcept {
    if (!host || !outState) return false;
    MhwRngServiceGuard<MhwWorldApi> world(host, kMhwWorldServiceName, kMhwWorldServiceVersion);
    if (!world || !world->GetState) return false;
    return world->GetState(outState);
}

inline bool MhwWorld_TryRefreshState(const MhwRngHostApi* host, MhwWorldState* outState) noexcept {
    if (!host || !outState) return false;
    MhwRngServiceGuard<MhwWorldApi> world(host, kMhwWorldServiceName, kMhwWorldServiceVersion);
    if (!world || !world->RefreshStateNow) return false;
    return world->RefreshStateNow(outState);
}

inline bool MhwWorld_TryDisplayPopup(const MhwRngHostApi* host,
                                     const char* utf8Msg,
                                     float durationSec = 3.0f,
                                     float delaySec = 0.0f,
                                     std::uint8_t flags = 0U) noexcept {
    if (!host || !utf8Msg || utf8Msg[0] == '\0') return false;
    MhwRngServiceGuard<MhwWorldApi> world(host, kMhwWorldServiceName, kMhwWorldServiceVersion);
    if (!world || !world->DisplayPopupAsync) return false;
    return world->DisplayPopupAsync(utf8Msg, durationSec, delaySec, flags);
}

inline bool MhwWorld_TryFormatSummary(const MhwRngHostApi* host,
                                      char* outBuf,
                                      std::size_t bufCap,
                                      bool english) noexcept {
    if (!host || !outBuf || bufCap == 0U) return false;
    MhwRngServiceGuard<MhwWorldApi> world(host, kMhwWorldServiceName, kMhwWorldServiceVersion);
    if (!world || !world->FormatWorldSummary) return false;
    return world->FormatWorldSummary(outBuf, bufCap, english);
}
