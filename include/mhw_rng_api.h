#pragma once
// ============================================================================
//  MHW Universal RNG Microkernel & Plugin SDK — Public C/C++ API (v2)
//  Core Module : nativePC\plugins\mhw_rng.dll
//  Plugin Dir  : nativePC\plugins\mhw_rng\*.dll (Shadow-Copy Hot-Reloadable!)
//  Target Game : Monster Hunter World: Iceborne 15.23 (Steam build 15539686)
// ============================================================================
#include <cstddef>
#include <cstdint>

#ifdef MHW_RNG_EXPORTS
#define MHW_RNG_API extern "C" __declspec(dllexport)
#else
#define MHW_RNG_API extern "C" __declspec(dllimport)
#endif

#define MHW_RNG_PLUGIN_EXPORT extern "C" __declspec(dllexport)

enum MhwRngApiVersion : std::uint32_t {
    kMhwRngApiVersion = 2U
};

using MhwRngPluginId = std::uint16_t;
constexpr MhwRngPluginId kMhwRngPluginIdKernel = 0U;

enum MhwRngSlotMode : std::uint8_t {
    kMhwRngSlotMasterDerived = 0, // Deterministic Xorshift128 derived from (MasterSeed, CallSiteRVA, CondID)
    kMhwRngSlotCustomSeed    = 1, // Deterministic Xorshift128 derived from (SlotCustomSeed, CallSiteRVA, CondID)
    kMhwRngSlotFixedValue    = 2, // Always returns raw uint32_t fixedValue
    kMhwRngSlotSequence      = 3, // Plays user-defined raw uint32_t sequence [v0, v1, ...]
    kMhwRngSlotBypass        = 4  // Uses MHW's original unhooked RNG implementation
};

enum MhwRngSeqEndPolicy : std::uint8_t {
    kMhwRngSeqLoop            = 0, // Wrap around to index 0 when sequence ends
    kMhwRngSeqHoldLast        = 1, // Keep returning last element when sequence ends
    kMhwRngSeqFallbackDerived = 2  // Switch seamlessly to deterministic Xorshift128 stream after sequence ends
};

enum MhwRngSourceKind : std::uint8_t {
    kMhwRngSrcUnknown         = 0,
    kMhwRngSrcMtNrandGlobal   = 1, // MtRandom::nrand(&MtRandom::sInstance) @ 0x142174E30
    kMhwRngSrcMtNrandPrivate  = 2, // MtRandom::nrand(privateThis)          @ 0x142174E30
    kMhwRngSrcMtCtorSeed      = 3, // MtRandom::ctor(this, seed)            @ 0x142174CE0
    kMhwRngSrcMtCtorDefault   = 4, // MtRandom::ctor_default(this)          @ 0x142174D50
    kMhwRngSrcMtSrand         = 5, // MtRandom::srand / xorshift_init       @ 0x142174DC0
    kMhwRngSrcMtSphere        = 6, // MtRandom::randUnitSphere(this, out3)  @ 0x142174E60
    kMhwRngSrcStdRandomDevice = 7, // std::_Random_device()                 @ 0x14273D420
    kMhwRngSrcMt19937Range    = 8, // Static mt19937 float range            @ 0x1427D3E60
    kMhwRngSrcMt19937HeapInit = 9, // Heap mt19937 init                     @ 0x142A74400
    kMhwRngSrcMt19937HeapDraw = 10,// Heap mt19937 draw                     @ 0x142A744B0
    kMhwRngSrcCrtRand         = 11 // MSVC CRT rand()                       @ 0x142C58034
};

// ============================================================================
//  Conditional / Dynamic Pull-Mode Slot Hook Types
// ============================================================================

enum MhwRngHookAction : std::uint32_t {
    kMhwRngActionUseCondition = 0, // Route draw to composite Slot[(decision.condId << 32) | callSiteRva]
    kMhwRngActionReturnValue  = 1, // Immediately return `decision.value0` (and `value1` if needTwoValues)
    kMhwRngActionBypass       = 2  // Bypass takeover and call vanilla RNG for this single invocation
};

#pragma pack(push, 8)
struct MhwRngSlotSnapshot {
    std::uint32_t  callSiteRva;    // _ReturnAddress() - exeBase
    std::uint32_t  condId;         // Plugin-defined Condition Enum (0 = default base slot)
    std::uint8_t   mode;           // MhwRngSlotMode
    std::uint8_t   sourceKind;     // MhwRngSourceKind
    std::uint8_t   seqEndPolicy;   // MhwRngSeqEndPolicy
    std::uint8_t   hasPluginHook;  // 1 if a Pull-mode callback is attached on this RVA
    MhwRngPluginId ownerPluginId;  // 0 = Kernel/Manual, >0 = Plugin ID owning this rule/slot
    std::uint16_t  reserved0;
    std::uint32_t  activeCondId;   // Current Push-mode active condition on base slot (condId == 0)
    std::uint32_t  fixedValue;     // Raw uint32_t when mode == kMhwRngSlotFixedValue
    std::uint32_t  lastValue;      // Most recent uint32_t produced by this (callSiteRva, condId) slot
    std::uint32_t  reserved1;
    std::uint64_t  customSeed;     // Seed when mode == kMhwRngSlotCustomSeed
    std::uint64_t  totalCalls;     // Cumulative calls since game boot
    std::uint64_t  epochCalls;     // Calls since last SetMasterSeed / ResetEpoch
    std::uint32_t  seqLength;
    std::uint32_t  seqCursor;
    std::uint32_t  state[4];       // Current 128-bit Xorshift128 state
};

struct MhwRngDrawContext {
    std::uint32_t  callSiteRva;
    std::uint8_t   sourceKind;     // MhwRngSourceKind
    std::uint8_t   needTwoValues;  // 1 only for MtRandom::randUnitSphere, 0 otherwise
    std::uint16_t  reserved;
    std::uintptr_t exeBase;
    std::uintptr_t exeEnd;
    std::uintptr_t rngInstance;    // RCX (`MtRandom* self` or generator state pointer)
    const void*    returnAddrSlot; // `_AddressOfReturnAddress()` on caller stack
    std::uint64_t  masterSeed;
    std::uint64_t  epoch;
    std::uint32_t  activeCondId;   // Current Push-mode condition set on this RVA
    std::uint32_t  pad0;
    // Deterministic PRNG helper: steps Slot[(condId << 32) | callSiteRva]'s Xorshift128 stream
    // so plugins doing custom probability shaping remain 100% deterministic under MasterSeed/Epoch!
    std::uint32_t (__fastcall *DrawDeterministic)(std::uint32_t callSiteRva, std::uint32_t condId);
};

struct MhwRngHookDecision {
    std::uint32_t action;          // MhwRngHookAction (default: kMhwRngActionUseCondition)
    std::uint32_t condId;          // Target Condition Enum when action == kMhwRngActionUseCondition
    std::uint32_t value0;          // Output when action == kMhwRngActionReturnValue
    std::uint32_t value1;          // Second output when needTwoValues != 0
};

struct MhwRngSiteMeta {
    std::uint32_t rva;
    std::uint8_t  category;        // Optional plugin-defined category
    std::uint8_t  scaleKind;       // Optional plugin-defined scale hint
    std::uint8_t  isCoreGameplay;
    std::uint8_t  found;
    char          alias[48];       // e.g. "em.size"
    char          description[128];// UTF-8 human description
    char          techSymbol[96];  // Optional C++ symbol / vtable hint
};

struct MhwRngPluginDescriptor {
    std::uint32_t targetApiVersion; // Must be kMhwRngApiVersion (2)
    const char*   shortName;        // Lowercase id, e.g. "size", "inspector" (used in `$rng reload <shortName>`)
    const char*   displayName;      // UTF-8 display name
    const char*   version;          // e.g. "1.0.0"
    const char*   description;      // UTF-8 1-line summary
};
#pragma pack(pop)

using MhwRngSlotHookFn = void(__fastcall*)(
    const MhwRngDrawContext* ctx,
    MhwRngHookDecision*      ioDecision,
    void*                    userData);

using MhwRngSubcommandFn = void(__fastcall*)(
    int                      argc,      // Token count AFTER `<subcmd>` (argv[0] is first arg after subcmd)
    const char* const*       argv,
    const char*              rawArgStr, // Raw trimmed string after `<subcmd>`
    void*                    userData);

using MhwRngMetaResolverFn = bool(__fastcall*)(
    std::uint32_t            rva,
    MhwRngSiteMeta*          outMeta,
    void*                    userData);

using MhwRngValueFormatterFn = bool(__fastcall*)(
    const MhwRngSlotSnapshot* snap,
    char*                     outBuf,
    std::size_t               bufCap,
    void*                     userData);

// ============================================================================
//  Host API Table Passed Directly to Every Plugin (`MhwRngPlugin_Init`)
// ============================================================================
#pragma pack(push, 8)
struct MhwRngHostApi {
    std::uint32_t  apiVersion;
    std::uint32_t  structSize;
    std::uintptr_t exeBase;
    std::uintptr_t exeEnd;

    // Logging, Paths & Unified Chat Output
    const char*   (__fastcall *GetRootPluginDir)();   // `nativePC\plugins`
    const char*   (__fastcall *GetSubPluginDir)();    // `nativePC\plugins\mhw_rng`
    void          (__cdecl    *Log)(MhwRngPluginId selfId, const char* fmt, ...);
    void          (__fastcall *ChatPrint)(const char* utf8Text);
    void          (__cdecl    *ChatPrintf)(const char* fmt, ...);

    // Global Engine State
    void          (__fastcall *SetTakeoverEnabled)(bool enabled);
    bool          (__fastcall *IsTakeoverEnabled)();
    void          (__fastcall *SetMasterSeed)(std::uint64_t seed, bool resetEpoch);
    std::uint64_t (__fastcall *GetMasterSeed)();
    std::uint64_t (__fastcall *ResetEpoch)();
    std::uint64_t (__fastcall *GetEpoch)();
    std::uint32_t (__fastcall *NormalizeSiteAddress)(std::uint64_t rawAddr, bool* outWasCallInsn);

    // Push-Mode Condition Control & Per-(RVA, CondID) Slot Rules
    bool          (__fastcall *SetActiveCondition)(MhwRngPluginId selfId, std::uint32_t rva, std::uint32_t condId);
    std::uint32_t (__fastcall *GetActiveCondition)(std::uint32_t rva);

    bool          (__fastcall *SetCondMasterDerived)(MhwRngPluginId selfId, std::uint32_t rva, std::uint32_t condId);
    bool          (__fastcall *SetCondCustomSeed)(MhwRngPluginId selfId, std::uint32_t rva, std::uint32_t condId, std::uint64_t seed);
    bool          (__fastcall *SetCondFixedValue)(MhwRngPluginId selfId, std::uint32_t rva, std::uint32_t condId, std::uint32_t rawU32);
    bool          (__fastcall *SetCondSequence)(MhwRngPluginId selfId, std::uint32_t rva, std::uint32_t condId,
                                                const std::uint32_t* values, std::uint32_t count, std::uint8_t endPolicy);
    bool          (__fastcall *SetCondBypass)(MhwRngPluginId selfId, std::uint32_t rva, std::uint32_t condId);
    void          (__fastcall *ClearSlotAllConditions)(std::uint32_t rva);
    void          (__fastcall *ClearPluginRules)(MhwRngPluginId selfId);
    void          (__fastcall *ClearAllRules)();

    // Pull-Mode Synchronous Slot Callback Registration
    bool          (__fastcall *RegisterSlotHook)(MhwRngPluginId selfId, std::uint32_t rva, MhwRngSlotHookFn fn, void* userData);
    void          (__fastcall *UnregisterSlotHook)(MhwRngPluginId selfId, std::uint32_t rva);

    // Unified `$rng <subcmd>` Chat Command Registration
    bool          (__fastcall *RegisterSubcommand)(MhwRngPluginId selfId,
                                                   const char* subcmd,
                                                   const char* usageExample,
                                                   const char* helpCn,
                                                   MhwRngSubcommandFn fn,
                                                   void* userData);

    // Site Metadata & Custom Value Formatter Registration (for `status`, `top`, `dump`)
    bool          (__fastcall *RegisterSiteMeta)(MhwRngPluginId selfId,
                                                 std::uint32_t rva,
                                                 const char* shortAlias,
                                                 const char* descriptionUtf8,
                                                 MhwRngValueFormatterFn formatterOrNull,
                                                 void* userData);
    bool          (__fastcall *SetFallbackMetaResolver)(MhwRngPluginId selfId,
                                                        MhwRngMetaResolverFn fn,
                                                        void* userData);
    bool          (__fastcall *QuerySiteMeta)(std::uint32_t rva, MhwRngSiteMeta* outMeta);
    bool          (__fastcall *FormatSlotCustom)(const MhwRngSlotSnapshot* snap, char* outBuf, std::size_t bufCap);
    const char*   (__fastcall *GetPluginShortName)(MhwRngPluginId pluginId);

    // Telemetry & Introspection
    bool          (__fastcall *GetCondSlotInfo)(std::uint32_t rva, std::uint32_t condId, MhwRngSlotSnapshot* outSnap);
    std::uint32_t (__fastcall *GetActiveSlotCount)();
    std::uint32_t (__fastcall *SnapshotActiveSlots)(MhwRngSlotSnapshot* outArray, std::uint32_t maxCount);
};
#pragma pack(pop)

// Every DLL in `nativePC\plugins\mhw_rng\*.dll` exports these 3 functions:
using MhwRngPluginGetInfoFn  = const MhwRngPluginDescriptor*(__cdecl*)();
using MhwRngPluginInitFn     = bool(__cdecl*)(const MhwRngHostApi* host, MhwRngPluginId selfId);
using MhwRngPluginShutdownFn = void(__cdecl*)();

// ============================================================================
//  Exported C Functions on `mhw_rng.dll` (for external DLLs like TAS / HFI)
// ============================================================================
MHW_RNG_API std::uint32_t        MhwRng_GetApiVersion();
MHW_RNG_API const MhwRngHostApi* MhwRng_GetHostApi();
MHW_RNG_API void                 MhwRng_SetTakeoverEnabled(bool enabled);
MHW_RNG_API bool                 MhwRng_IsTakeoverEnabled();
MHW_RNG_API void                 MhwRng_SetMasterSeed(std::uint64_t seed, bool resetEpoch);
MHW_RNG_API std::uint64_t        MhwRng_GetMasterSeed();
MHW_RNG_API std::uint64_t        MhwRng_ResetEpoch();
MHW_RNG_API std::uint64_t        MhwRng_GetEpoch();

MHW_RNG_API bool                 MhwRng_SetSlotMasterDerived(std::uint32_t callSiteRva);
MHW_RNG_API bool                 MhwRng_SetSlotCustomSeed(std::uint32_t callSiteRva, std::uint64_t seed);
MHW_RNG_API bool                 MhwRng_SetSlotFixedValue(std::uint32_t callSiteRva, std::uint32_t value);
MHW_RNG_API bool                 MhwRng_SetSlotSequence(std::uint32_t callSiteRva,
                                                         const std::uint32_t* values,
                                                         std::uint32_t count,
                                                         std::uint8_t endPolicy);
MHW_RNG_API bool                 MhwRng_SetSlotBypass(std::uint32_t callSiteRva);
MHW_RNG_API void                 MhwRng_ClearAllSlotOverrides();

MHW_RNG_API bool                 MhwRng_GetSlotInfo(std::uint32_t callSiteRva, MhwRngSlotSnapshot* outSnapshot);
MHW_RNG_API std::uint32_t        MhwRng_GetActiveSlotCount();
MHW_RNG_API std::uint32_t        MhwRng_SnapshotActiveSlots(MhwRngSlotSnapshot* outArray, std::uint32_t maxCount);
