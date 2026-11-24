#pragma once
// ============================================================================
//  MIRA Companion SDK — Verified MHW:I 15.23 World Singletons, Offsets & Readers
//  Target: Monster Hunter World: Iceborne Steam 15.23 (Build 15539686)
//          ImageBase = 0x140000000. Pure header-only, zero runtime dependencies.
// ============================================================================
#include "mhw_rng_math.h"

#include <cstdint>

namespace mhw1523 {

// ---- Verified Global Singleton Pointer RVAs (`exeBase + RVA`) ----
constexpr std::uint32_t kRvaSPlayer       = 0x0500CDA0U; // sPlayer*
constexpr std::uint32_t kRvaSAnimal       = 0x0500CE00U; // sAnimal*
constexpr std::uint32_t kRvaSChat         = 0x0500CE70U; // sChat*
constexpr std::uint32_t kRvaSQuest        = 0x0500ED30U; // sQuest*
constexpr std::uint32_t kRvaRAnimalSize   = 0x05012740U; // rAnimalSize*
constexpr std::uint32_t kRvaSEnemyTune    = 0x05012908U; // sEnemyTune* (cSizeTable holder)
constexpr std::uint32_t kRvaSTime         = 0x05013830U; // sTime*
constexpr std::uint32_t kRvaSSaveData     = 0x05013950U; // sSaveData*
constexpr std::uint32_t kRvaSPlayerRoot   = 0x050139A0U; // sPlayerRoot*
constexpr std::uint32_t kRvaSMhArea       = 0x051C42B8U; // sMhArea*
constexpr std::uint32_t kRvaSMhGui        = 0x051C4640U; // sMhGUI*

// ---- Verified Game Function RVAs ----
constexpr std::uint32_t kRvaDisplayPopup  = 0x01AE33D0U; // sMhGUI::DisplayPopup

// ---- sPlayer / sPlayerRoot / uPlayer Offsets ----
constexpr std::uintptr_t kOffSPlayerLocalUnit  = 0x0080U; // sPlayer -> uPlayer*
constexpr std::uintptr_t kOffPlayerRootEntity  = 0x0050U; // sPlayerRoot -> active entity*
constexpr std::uintptr_t kOffUnitValidMask     = 0x000CU; // (flags & 0x0E) != 0 -> valid unit
constexpr std::uintptr_t kOffPlayerWeapon      = 0x76B0U; // uPlayer -> uWeapon*
constexpr std::uintptr_t kOffPlayerStatusMgr   = 0x7D20U; // uPlayer -> statusMgr*
constexpr std::uintptr_t kOffPlayerCritCursor  = 0x8820U; // uPlayer -> crit cursor
constexpr std::uintptr_t kOffPlayerLocalFlags  = 0x8938U; // (flags & 1) != 0 -> local player

// ---- sQuest Offsets (verified vs `sQuest::isInQuest` @ 0x141B5A100 & `getGLRegionLevel` @ 0x141B545D0) ----
constexpr std::uintptr_t kOffQuestState          = 0x00038U; // uint32_t (`(state - 2) <= 10`)
constexpr std::uintptr_t kOffQuestId             = 0x0004CU; // uint32_t
constexpr std::uintptr_t kOffQuestCategory       = 0x00054U; // uint32_t (`>= 2` when in quest)
constexpr std::uintptr_t kOffQuestIsGuidingLands = 0x0AE3BU; // uint8_t
constexpr std::uintptr_t kOffQuestRemTicks       = 0x13180U; // int32_t
constexpr std::uintptr_t kOffQuestUnlimTicks     = 0x13184U; // int32_t
constexpr std::uintptr_t kOffQuestInitRemTicks   = 0x13194U; // int32_t
constexpr std::uintptr_t kOffQuestGlRegionLevels = 0x2A360U; // uint8_t[6] (Forest..Tundra)

// ---- sMhArea Offsets (verified vs `sMhArea::isAreaGuidingLands` @ 0x141AC0DE0) ----
constexpr std::uintptr_t kOffAreaIsVillage       = 0x0D2EAU; // uint8_t
constexpr std::uintptr_t kOffAreaStageId         = 0x0D328U; // uint32_t (`0x6D` = 109 Guiding Lands)
constexpr std::uint32_t  kStageIdGuidingLands    = 109U;

// ---- sTime Offsets (verified vs `sTime::isSpecialWeather` @ 0x141B831C0) ----
constexpr std::uintptr_t kOffTimeCurHour         = 0x0038U;  // float `[0.0f .. 24.0f)`
constexpr std::uintptr_t kOffTimeWeatherId       = 0x008CU;  // uint32_t (special iff `!= 0 && != 10`)

// ---- uEnemy / Monster Offsets ----
constexpr std::uintptr_t kOffEnemyMonsterId      = 0x12280U; // uint32_t (`0..101`)
constexpr std::uintptr_t kOffEnemySizeTableIdx   = 0x0773CU; // uint32_t

// ---- uAnimal / sAnimal / cAnimalGroupCtrl Offsets ----
constexpr std::uintptr_t kOffUAnimalId           = 0x1AF0U;
constexpr std::uintptr_t kOffUAnimalType         = 0x1AF4U;
constexpr std::uintptr_t kOffSAnimalRareSlotBase = 0x70A8U;
constexpr std::uintptr_t kRareSlotStride         = 0x00D0U;
constexpr std::uintptr_t kOffRareSlotJustRolled  = 0x00CBU;
constexpr std::uintptr_t kOffGroupCtrlAnimalId   = 0x0328U;
constexpr std::uintptr_t kOffGroupCtrlAnimalType = 0x032CU;
constexpr std::uintptr_t kOffGroupCtrlRegionId   = 0x0330U;

struct WorldStateSnapshot {
    bool           inScene;
    bool           inQuest;
    bool           isVillage;
    bool           isGuidingLands;
    bool           isSpecialWeather;
    bool           hasQuest;
    bool           hasArea;
    bool           hasTime;
    std::uint32_t  questId;
    std::uint32_t  questState;
    std::uint32_t  stageId;
    std::uint32_t  weatherId;
    std::int32_t   questElapsedTicks;
    float          gameHour;
    std::int32_t   gameHourInt;
    std::uint8_t   glRegionLevels[6];
    std::uintptr_t localPlayerPtr;
};

namespace detail {

inline bool ReadPlayerPresenceSEH(std::uintptr_t exeBase,
                                  std::uintptr_t& outRootEnt,
                                  std::uintptr_t& outLocalPl) noexcept {
    outRootEnt = 0ULL;
    outLocalPl = 0ULL;
    if (!exeBase) return false;
    __try {
        const std::uintptr_t root = *reinterpret_cast<const std::uintptr_t*>(exeBase + kRvaSPlayerRoot);
        if (mhw_sdk::IsValidUserPtr(root)) {
            const std::uintptr_t ent = *reinterpret_cast<const std::uintptr_t*>(root + kOffPlayerRootEntity);
            if (mhw_sdk::IsValidUserPtr(ent)) outRootEnt = ent;
        }
        const std::uintptr_t sp = *reinterpret_cast<const std::uintptr_t*>(exeBase + kRvaSPlayer);
        if (mhw_sdk::IsValidUserPtr(sp)) {
            const std::uintptr_t up = *reinterpret_cast<const std::uintptr_t*>(sp + kOffSPlayerLocalUnit);
            if (mhw_sdk::IsValidUserPtr(up)) outLocalPl = up;
        }
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

inline bool ReadQuestBlockSEH(std::uintptr_t exeBase, WorldStateSnapshot& io) noexcept {
    if (!exeBase) return false;
    __try {
        const std::uintptr_t sq = *reinterpret_cast<const std::uintptr_t*>(exeBase + kRvaSQuest);
        if (!mhw_sdk::IsValidUserPtr(sq)) return false;

        const std::uint32_t st  = *reinterpret_cast<const std::uint32_t*>(sq + kOffQuestState);
        const std::uint32_t qid = *reinterpret_cast<const std::uint32_t*>(sq + kOffQuestId);
        const std::uint32_t cat = *reinterpret_cast<const std::uint32_t*>(sq + kOffQuestCategory);
        const std::uint8_t  gl  = *reinterpret_cast<const std::uint8_t*>(sq + kOffQuestIsGuidingLands);
        const std::int32_t  rem = *reinterpret_cast<const std::int32_t*>(sq + kOffQuestRemTicks);
        const std::int32_t  unl = *reinterpret_cast<const std::int32_t*>(sq + kOffQuestUnlimTicks);
        const std::int32_t  ini = *reinterpret_cast<const std::int32_t*>(sq + kOffQuestInitRemTicks);

        std::int32_t elapsed = (ini > 0 && ini >= rem) ? (ini - rem) : unl;
        if (elapsed < 0) elapsed = 0;

        io.hasQuest          = true;
        io.questState        = st;
        io.questId           = qid;
        io.inQuest           = ((st - 2U) <= 10U && cat >= 2U);
        io.isGuidingLands    = (gl != 0U);
        io.questElapsedTicks = elapsed;
        for (int i = 0; i < 6; ++i) {
            io.glRegionLevels[i] = *reinterpret_cast<const std::uint8_t*>(
                sq + kOffQuestGlRegionLevels + static_cast<std::uintptr_t>(i));
        }
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

inline bool ReadAreaBlockSEH(std::uintptr_t exeBase, WorldStateSnapshot& io) noexcept {
    if (!exeBase) return false;
    __try {
        const std::uintptr_t area = *reinterpret_cast<const std::uintptr_t*>(exeBase + kRvaSMhArea);
        if (!mhw_sdk::IsValidUserPtr(area)) return false;
        const std::uint8_t  vil = *reinterpret_cast<const std::uint8_t*>(area + kOffAreaIsVillage);
        const std::uint32_t sid = *reinterpret_cast<const std::uint32_t*>(area + kOffAreaStageId);
        io.hasArea        = true;
        io.isVillage      = (vil != 0U);
        io.stageId        = sid;
        if (sid == kStageIdGuidingLands) io.isGuidingLands = true;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

inline bool ReadTimeBlockSEH(std::uintptr_t exeBase, WorldStateSnapshot& io) noexcept {
    if (!exeBase) return false;
    __try {
        const std::uintptr_t tm = *reinterpret_cast<const std::uintptr_t*>(exeBase + kRvaSTime);
        if (!mhw_sdk::IsValidUserPtr(tm)) return false;
        const float         hr  = *reinterpret_cast<const float*>(tm + kOffTimeCurHour);
        const std::uint32_t wid = *reinterpret_cast<const std::uint32_t*>(tm + kOffTimeWeatherId);
        io.hasTime          = true;
        io.gameHour         = hr;
        io.gameHourInt      = (hr >= 0.0f && hr < 48.0f) ? static_cast<std::int32_t>(hr) : 0;
        io.weatherId        = wid;
        io.isSpecialWeather = (wid != 0U && wid != 10U);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

} // namespace detail

inline bool ReadWorldStateSEH(std::uintptr_t exeBase, WorldStateSnapshot& out) noexcept {
    out = WorldStateSnapshot{};
    if (!exeBase) return false;

    std::uintptr_t rootEnt = 0ULL;
    std::uintptr_t localPl = 0ULL;
    detail::ReadPlayerPresenceSEH(exeBase, rootEnt, localPl);
    out.localPlayerPtr = localPl;
    out.inScene        = (rootEnt != 0ULL) || (localPl != 0ULL);

    (void)detail::ReadQuestBlockSEH(exeBase, out);
    (void)detail::ReadAreaBlockSEH(exeBase, out);
    (void)detail::ReadTimeBlockSEH(exeBase, out);
    return out.inScene || out.hasQuest || out.hasArea || out.hasTime;
}

inline bool TryGetLocalPlayerSEH(std::uintptr_t exeBase,
                                 std::uintptr_t& outUnitPtr,
                                 bool requireValidUnitMask = true) noexcept {
    outUnitPtr = 0ULL;
    if (!exeBase) return false;
    __try {
        const std::uintptr_t sp = *reinterpret_cast<const std::uintptr_t*>(exeBase + kRvaSPlayer);
        if (!mhw_sdk::IsValidUserPtr(sp)) return false;
        const std::uintptr_t up = *reinterpret_cast<const std::uintptr_t*>(sp + kOffSPlayerLocalUnit);
        if (!mhw_sdk::IsValidUserPtr(up)) return false;
        if (requireValidUnitMask) {
            const std::uint32_t mask = *reinterpret_cast<const std::uint32_t*>(up + kOffUnitValidMask);
            if ((mask & 0x0EU) == 0U) return false;
        }
        outUnitPtr = up;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

inline bool IsLocalPlayerEntitySEH(std::uintptr_t uPlayerPtr) noexcept {
    if (!mhw_sdk::IsValidUserPtr(uPlayerPtr)) return false;
    __try {
        const std::uint32_t flags = *reinterpret_cast<const std::uint32_t*>(uPlayerPtr + kOffPlayerLocalFlags);
        return (flags & 1U) != 0U;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

inline bool TryReadMonsterIdSEH(std::uintptr_t monsterPtr, std::uint32_t& outMonsterId) noexcept {
    outMonsterId = 0U;
    if (!mhw_sdk::IsValidUserPtr(monsterPtr)) return false;
    __try {
        outMonsterId = *reinterpret_cast<const std::uint32_t*>(monsterPtr + kOffEnemyMonsterId);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

} // namespace mhw1523
