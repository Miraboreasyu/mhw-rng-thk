#pragma once
// ============================================================================
//  MHW Universal RNG Microkernel & Plugin SDK — Public C/C++ API (v3.5)
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
    kMhwRngApiVersion = 3U
};

using MhwRngPluginId     = std::uint16_t;
using MhwRngHookHandle   = std::uint32_t; // 0 = invalid
using MhwRngServiceLease = std::uint64_t; // 0 = invalid

constexpr MhwRngPluginId     kMhwRngPluginIdKernel = 0U;
constexpr MhwRngHookHandle   kMhwRngInvalidHandle  = 0U;
constexpr MhwRngServiceLease kMhwRngInvalidLease   = 0ULL;

enum MhwRngEngineMode : std::uint32_t {
    kMhwRngModeOff         = 0U, // Pure vanilla RNG, zero-overhead fast bypass
    kMhwRngModeObserveOnly = 1U, // Calls vanilla RNG for all draws, updates counters/lastValue & fires observers
    kMhwRngModeTakeover    = 2U, // Full deterministic microkernel takeover for all slots + pre/post/filter hooks
    kMhwRngModeSelective   = 3U  // Selective takeover: only hooked/overridden slots are intercepted; untouched sites run pure vanilla RNG
};

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
//  Conditional / Chained Pull-Mode Slot Hook & Filter Types (v3)
// ============================================================================

enum MhwRngHookAction : std::uint32_t {
    kMhwRngActionUseCondition    = 0, // Route to condId (if unchanged in Pre-chain, continues to next hook)
    kMhwRngActionReturnValue     = 1, // Override output (`value0`, and `value1` if needTwoValues), stops pre-chain
    kMhwRngActionBypass          = 2, // Bypass takeover and call vanilla RNG, stops pre-chain
    kMhwRngActionContinue        = 3, // Explicitly pass control to next hook in chain
    kMhwRngActionClaimCondition  = 4, // Commit `condId` (even 0) and stop pre-chain immediately
    kMhwRngActionFilterVanilla   = 5, // Execute vanilla RNG now, then invoke this hook's `postFn` with `CanModify`
    kMhwRngActionReturnFloatBits = 6, // For Mt19937Range: bit-cast `value0` directly as IEEE-754 float return
    kMhwRngActionFilterSlot      = 7  // Step target slot normally, then invoke this hook's `postFn` with `CanModify`
};

enum MhwRngHookFlags : std::uint32_t {
    kMhwRngHookPre           = 1U << 0,
    kMhwRngHookPost          = 1U << 1,
    kMhwRngHookObserveOnly   = 1U << 2, // Receive `postFn` notifications even in kMhwRngModeObserveOnly
    kMhwRngHookFilterVanilla = 1U << 3, // Even without preFn, run vanilla RNG and call `postFn` with `CanModify`
    kMhwRngHookFilterSlot    = 1U << 4  // Step target slot normally and call `postFn` with `CanModify`
};

enum MhwRngDrawFlags : std::uint32_t {
    kMhwRngDrawFlagHasRegs            = 1U << 0,
    kMhwRngDrawFlagHasOrigSeed        = 1U << 1,
    kMhwRngDrawFlagHasRange           = 1U << 2,
    kMhwRngDrawFlagIsPrivateInst      = 1U << 3,
    kMhwRngDrawFlagInstExplicitSeeded = 1U << 4,
    kMhwRngDrawFlagObserveOnly        = 1U << 5,
    kMhwRngDrawFlagPostDraw           = 1U << 6,
    kMhwRngDrawFlagHasVanilla         = 1U << 7,
    kMhwRngDrawFlagCanModify          = 1U << 8,
    kMhwRngDrawFlagBypassed           = 1U << 9,
    kMhwRngDrawFlagOverridden         = 1U << 10,
    kMhwRngDrawFlagHasXmmRegs         = 1U << 11
};

enum MhwRngLifecycleEvent : std::uint32_t {
    kMhwRngEventEpochReset      = 1U,
    kMhwRngEventMasterSeed      = 2U,
    kMhwRngEventEngineMode      = 3U,
    kMhwRngEventClearAllRules   = 4U,
    kMhwRngEventStateRestored   = 5U,
    kMhwRngEventLanguageChanged = 6U
};

enum MhwRngLangId : std::uint32_t {
    kMhwRngLangZh    = 0U,
    kMhwRngLangEn    = 1U,
    kMhwRngLangJa    = 2U,
    kMhwRngLangKo    = 3U,
    kMhwRngLangOther = 255U
};

enum MhwRngInstancePolicy : std::uint32_t {
    kMhwRngInstAuto             = 0U, // Default epoch-tracked heuristic
    kMhwRngInstFollowOwnState   = 1U, // Step private MtRandom::self directly when slot mode is MasterDerived
    kMhwRngInstForceSlotControl = 2U  // Always draw from CallSite slot even if instance was explicitly seeded
};

enum MhwRngRestoreFlags : std::uint32_t {
    kMhwRngRestoreState      = 1U << 0,
    kMhwRngRestoreSeqCursor  = 1U << 1,
    kMhwRngRestoreActiveCond = 1U << 2,
    kMhwRngRestoreCounters   = 1U << 3,
    kMhwRngRestoreAll        = 0xFFFFFFFFU
};

#pragma pack(push, 8)
union MhwRngXmmReg {
    float         f32[4];
    double        f64[2];
    std::uint32_t u32[4];
    std::uint64_t u64[2];
};

// Captured atomically by MASM x64 entry thunks (`src/rng_trampolines.asm`)
// DO NOT reorder fields without updating `rng_trampolines.asm`!
struct MhwRngCallerRegs {
    std::uint64_t  rbx;       // +0x00
    std::uint64_t  rbp;       // +0x08
    std::uint64_t  rsi;       // +0x10
    std::uint64_t  rdi;       // +0x18
    std::uint64_t  r12;       // +0x20
    std::uint64_t  r13;       // +0x28
    std::uint64_t  r14;       // +0x30
    std::uint64_t  r15;       // +0x38
    std::uintptr_t callerRsp; // +0x40 (Exact entry RSP == _AddressOfReturnAddress())
    std::uintptr_t callerRip; // +0x48 (Raw *returnAddrSlot)
    std::uint64_t  reserved;  // +0x50 (sizeof(MhwRngCallerRegs) == 0x80 on v3.2+)
    std::uint64_t  reserved2; // +0x58 (16B alignment pad)
    MhwRngXmmReg   xmm6;      // +0x60 (Non-volatile XMM6 at RNG entry; check kMhwRngDrawFlagHasXmmRegs)
    MhwRngXmmReg   xmm7;      // +0x70 (Non-volatile XMM7 at RNG entry; check kMhwRngDrawFlagHasXmmRegs)
};

struct MhwRngSlotSnapshot {
    std::uint32_t  callSiteRva;    // _ReturnAddress() - exeBase
    std::uint32_t  condId;         // Plugin-defined Condition Enum (0 = default base slot)
    std::uint8_t   mode;           // MhwRngSlotMode
    std::uint8_t   sourceKind;     // MhwRngSourceKind
    std::uint8_t   seqEndPolicy;   // MhwRngSeqEndPolicy
    std::uint8_t   hasPluginHook;  // Number of hooks attached on base slot (0 = none)
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
    std::uint32_t  structSize;     // sizeof(MhwRngDrawContext)
    // Deterministic PRNG helper: steps Slot[(condId << 32) | callSiteRva]'s Xorshift128 stream
    std::uint32_t (__fastcall *DrawDeterministic)(std::uint32_t callSiteRva, std::uint32_t condId);

    // ---- v3 Context Extensions ----
    const MhwRngCallerRegs* callerRegs; // Non-volatile GPR snapshot at RNG entry (nullptr if unavailable)
    std::uint64_t  origArg1;       // seed for ctor(seed)/srand/mt19937_init, outVec3 for Sphere, minVal bits for Range
    std::uint64_t  origArg2;       // maxVal bits for Range
    float          origMinVal;     // minVal when sourceKind == kMhwRngSrcMt19937Range
    float          origMaxVal;     // maxVal when sourceKind == kMhwRngSrcMt19937Range
    std::uint32_t  callFlags;      // Bitmask of MhwRngDrawFlags
    std::uint32_t  origReturnRva;  // Direct return RVA before transparent wrapper unwrapping
    std::uint32_t  vanillaValue0;  // Raw vanilla RNG result #0 (valid when kMhwRngDrawFlagHasVanilla is set)
    std::uint32_t  vanillaValue1;  // Raw vanilla RNG result #1 (for randUnitSphere)
    std::uint32_t  finalValue0;    // Final emitted result #0 (valid during PostDraw / GlobalObserver)
    std::uint32_t  finalValue1;    // Final emitted result #1
    float          vanillaFloat;   // Original float output for Mt19937Range when HasVanilla is set
    float          finalFloat;     // Final float output for Mt19937Range during PostDraw
};

struct MhwRngHookDecision {
    std::uint32_t action;          // MhwRngHookAction (default: kMhwRngActionUseCondition)
    std::uint32_t condId;          // Target Condition Enum when action == UseCondition / ClaimCondition
    std::uint32_t value0;          // Output when action == ReturnValue / ReturnFloatBits
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
    std::uint32_t targetApiVersion; // Must be kMhwRngApiVersion (3)
    const char*   shortName;        // Lowercase id, e.g. "size", "inspector" (used in `$rng reload <shortName>`)
    const char*   displayName;      // UTF-8 display name
    const char*   version;          // e.g. "1.0.0"
    const char*   description;      // UTF-8 1-line summary
};

using MhwRngSlotHookFn = void(__fastcall*)(
    const MhwRngDrawContext* ctx,
    MhwRngHookDecision*      ioDecision,
    void*                    userData);

struct MhwRngHookDesc {
    std::uint32_t    structSize; // Must be sizeof(MhwRngHookDesc)
    std::uint32_t    rva;
    std::int32_t     priority;   // Lower runs first (default 0)
    std::uint32_t    flags;      // MhwRngHookFlags (0 defaults to kMhwRngHookPre)
    MhwRngSlotHookFn preFn;
    MhwRngSlotHookFn postFn;
    void*            userData;
};

struct MhwRngSlotState {
    std::uint32_t callSiteRva;
    std::uint32_t condId;
    std::uint32_t state[4];
    std::uint32_t seqCursor;
    std::uint32_t activeCondId;
    std::uint64_t epochCalls;
    std::uint32_t lastValue;
    std::uint32_t flags;         // Bitmask of MhwRngRestoreFlags (0 treated as kMhwRngRestoreAll)
};

struct MhwRngEventInfo {
    std::uint32_t eventKind;     // MhwRngLifecycleEvent
    std::uint32_t engineMode;    // MhwRngEngineMode
    std::uint64_t masterSeed;
    std::uint64_t epoch;
    std::uint64_t prevValue;     // Previous epoch / seed / mode / langId depending on eventKind
};

struct MhwRngLocaleText {
    const char* lang;            // Primary BCP-47 tag, e.g. "zh", "en", "ja", "ko"
    const char* text;            // UTF-8 localized text
};

// ---- v3.5 Event Bus & Hot-Reload-Safe Service Registry Types ----
struct MhwRngCustomEvent {
    std::uint32_t  structSize;   // sizeof(MhwRngCustomEvent)
    std::uint32_t  payloadSize;
    const char*    topic;        // Canonical lowercase dot-separated topic, e.g. "mhw.world.quest_enter"
    MhwRngPluginId senderId;
    std::uint16_t  reserved;
    std::uint64_t  seq;
    const void*    payload;
};

struct MhwRngServiceInfo {
    std::uint32_t  structSize;   // sizeof(MhwRngServiceInfo)
    std::uint32_t  version;
    MhwRngPluginId ownerId;
    std::uint16_t  reserved;
    const char*    name;
    const void*    apiTable;
};

struct MhwRngServiceEventPayload {
    std::uint32_t  structSize;   // sizeof(MhwRngServiceEventPayload)
    std::uint32_t  version;
    MhwRngPluginId ownerId;
    std::uint16_t  reserved;
    char           name[64];
};
#pragma pack(pop)

using MhwRngMainThreadTickFn = void(__fastcall*)(
    std::uint64_t            tickSeq,
    void*                    userData);

using MhwRngCustomEventFn = void(__fastcall*)(
    const MhwRngCustomEvent* ev,
    void*                    userData);

using MhwRngSubcommandFn = void(__fastcall*)(
    int                      argc,      // Token count AFTER `<subcmd>` (argv[0] is first arg after subcmd)
    const char* const*       argv,
    const char*              rawArgStr, // Raw trimmed string after `<subcmd>`
    void*                    userData);

using MhwRngCompletionSinkFn = void(__fastcall*)(
    const char*              candidateUtf8,
    void*                    sinkContext);

// `subcmd`  : lowercase registered subcommand name (e.g. "size", "steam", "thk", "fix")
// `argc`    : number of completed tokens AFTER `<subcmd>` (`0` = completing 1st arg after `<subcmd>`)
// `argv`    : `argc` completed tokens after `<subcmd>`
// `prefix`  : current partial token under cursor (never null; may be `""`)
// `addCand` : callback to emit candidate tokens (host performs case-insensitive prefix filtering, dedup & sort)
using MhwRngSubcommandCompleteFn = void(__fastcall*)(
    const char*              subcmd,
    int                      argc,
    const char* const*       argv,
    const char*              prefix,
    MhwRngCompletionSinkFn   addCand,
    void*                    sinkCtx,
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

using MhwRngObserverFn = void(__fastcall*)(
    const MhwRngDrawContext* ctx,
    void*                    userData);

using MhwRngLifecycleFn = void(__fastcall*)(
    const MhwRngEventInfo*   info,
    void*                    userData);

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

    // Pull-Mode Synchronous Slot Callback Registration (Simple Form; chains on shared RVAs in v3)
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

    // ---- v3.0 Microkernel Capabilities ----
    // Main-Thread Safe Async Chat Delivery (safe from worker-thread OnSlotDraw callbacks)
    void          (__fastcall *ChatPrintAsync)(const char* utf8Text);
    void          (__cdecl    *ChatPrintfAsync)(const char* fmt, ...);

    // 4-State Engine Mode (Off / ObserveOnly / Takeover / Selective)
    void          (__fastcall *SetEngineMode)(std::uint32_t mode);
    std::uint32_t (__fastcall *GetEngineMode)();

    // Chained Prioritized Pre/Post/FilterVanilla Slot Hooks, Global Wildcard Observers & Lifecycle Events
    MhwRngHookHandle (__fastcall *RegisterSlotHookEx)(MhwRngPluginId selfId, const MhwRngHookDesc* desc);
    void             (__fastcall *UnregisterSlotHookHandle)(MhwRngPluginId selfId, MhwRngHookHandle handle);

    MhwRngHookHandle (__fastcall *RegisterGlobalObserver)(MhwRngPluginId selfId,
                                                          std::uint32_t rvaFilterOrZero,
                                                          std::uint32_t sourceKindMask,
                                                          MhwRngObserverFn fn,
                                                          void* userData);
    void             (__fastcall *UnregisterGlobalObserver)(MhwRngPluginId selfId, MhwRngHookHandle handle);

    MhwRngHookHandle (__fastcall *RegisterLifecycleHook)(MhwRngPluginId selfId, MhwRngLifecycleFn fn, void* userData);
    void             (__fastcall *UnregisterLifecycleHook)(MhwRngPluginId selfId, MhwRngHookHandle handle);

    // RNG Snapshot & Restore + Dynamic Condition Slot Reclamation
    bool          (__fastcall *RestoreSlotState)(const MhwRngSlotState* state);
    std::uint32_t (__fastcall *CaptureStateBlob)(void* outBuf, std::uint32_t bufCap);
    bool          (__fastcall *RestoreStateBlob)(const void* buf, std::uint32_t bufSize);
    bool          (__fastcall *ReleaseCondSlot)(MhwRngPluginId selfId, std::uint32_t rva, std::uint32_t condId);

    // Private MtRandom Instance Policy & Dynamic Transparent Wrapper Registration
    bool          (__fastcall *SetInstancePolicy)(std::uintptr_t instancePtr, std::uint32_t policy);
    std::uint32_t (__fastcall *GetInstancePolicy)(std::uintptr_t instancePtr);
    bool          (__fastcall *RegisterTransparentWrapper)(MhwRngPluginId selfId,
                                                             std::uint32_t wrapperRetRva,
                                                             std::uint16_t frameQwords);
    void          (__fastcall *UnregisterTransparentWrapper)(MhwRngPluginId selfId, std::uint32_t wrapperRetRva);

    // ---- v3.1 Extensions (check `structSize` before calling on older hosts) ----
    const char*   (__fastcall *GetLanguage)();                          // Returns canonical tag ("zh", "en", "ja", ...)
    bool          (__fastcall *ExecuteCommand)(const char* commandLine); // Executes single command (up to 1024B)

    // ---- v3.2 Extensions (check `structSize` or use `MhwRng_HostCaptureCallerRvas`) ----
    // Unwinds caller return RVAs via Windows x64 `.pdata` (`RtlLookupFunctionEntry` + `RtlVirtualUnwind`)
    // with SEH protection and zero heap allocation.
    // `outRvas[0]` is the innermost direct in-module return RVA, `outRvas[1]` is its caller, etc.
    // Returns the number of RVAs written (up to `maxDepth`, capped at 32).
    std::uint32_t (__fastcall *CaptureCallerRvas)(const MhwRngDrawContext* ctx,
                                                  std::uint32_t* outRvas,
                                                  std::uint32_t maxDepth);

    // ---- v3.3 Standardized i18n Extensions (check `structSize` or use `MhwRng_Tr*` / `MhwRng_Register*Bi`) ----
    // Resolves localized text from `entries[0..count-1]` using deterministic fallback:
    //   active language -> "en" -> "zh" -> first non-empty entry -> fallbackText -> "" (never null).
    const char*   (__fastcall *SelectText)(const MhwRngLocaleText* entries,
                                           std::size_t count,
                                           const char* fallbackText);
    bool          (__fastcall *RegisterPluginInfoI18n)(MhwRngPluginId selfId,
                                                       const MhwRngLocaleText* displayNames,
                                                       std::size_t displayCount,
                                                       const MhwRngLocaleText* descriptions,
                                                       std::size_t descCount);
    bool          (__fastcall *RegisterSubcommandI18n)(MhwRngPluginId selfId,
                                                       const char* subcmd,
                                                       const char* usageExample,
                                                       const MhwRngLocaleText* helpTexts,
                                                       std::size_t helpCount,
                                                       MhwRngSubcommandFn fn,
                                                       void* userData);
    bool          (__fastcall *RegisterSiteMetaI18n)(MhwRngPluginId selfId,
                                                     std::uint32_t rva,
                                                     const char* shortAlias,
                                                     const MhwRngLocaleText* descriptions,
                                                     std::size_t descCount,
                                                     MhwRngValueFormatterFn formatterOrNull,
                                                     void* userData);

    // ---- v3.4 Subcommand Tab-Completion Extension (check `structSize` or use `MhwRng_RegisterSubcommandCompleter`) ----
    bool          (__fastcall *RegisterSubcommandCompleter)(MhwRngPluginId selfId,
                                                            const char* subcmd,
                                                            MhwRngSubcommandCompleteFn completer,
                                                            void* userData);

    // ---- v3.5 Main-Thread Tick, Event Bus & Hot-Reload-Safe Service Registry ----
    MhwRngHookHandle   (__fastcall *RegisterMainThreadTick)(MhwRngPluginId selfId,
                                                            MhwRngMainThreadTickFn fn,
                                                            void* userData);
    void               (__fastcall *UnregisterMainThreadTick)(MhwRngPluginId selfId,
                                                              MhwRngHookHandle handle);
    MhwRngHookHandle   (__fastcall *SubscribeEvent)(MhwRngPluginId selfId,
                                                    const char* topicPattern,
                                                    MhwRngCustomEventFn fn,
                                                    void* userData);
    void               (__fastcall *UnsubscribeEvent)(MhwRngPluginId selfId,
                                                      MhwRngHookHandle handle);
    std::uint32_t      (__fastcall *EmitEvent)(MhwRngPluginId selfId,
                                               const char* topic,
                                               const void* payload,
                                               std::uint32_t payloadSize);
    bool               (__fastcall *RegisterService)(MhwRngPluginId selfId,
                                                     const char* serviceName,
                                                     std::uint32_t version,
                                                     const void* apiTable);
    void               (__fastcall *UnregisterService)(MhwRngPluginId selfId,
                                                       const char* serviceName);
    MhwRngServiceLease (__fastcall *AcquireService)(const char* serviceName,
                                                    std::uint32_t minVersion,
                                                    MhwRngServiceInfo* outInfo);
    void               (__fastcall *ReleaseService)(MhwRngServiceLease lease);
};
#pragma pack(pop)

inline const char* MhwRng_HostGetLanguage(const MhwRngHostApi* host) noexcept {
    if (!host) return "zh";
    if (host->structSize < offsetof(MhwRngHostApi, GetLanguage) + sizeof(host->GetLanguage)) return "zh";
    if (!host->GetLanguage) return "zh";
    const char* lang = host->GetLanguage();
    return (lang && lang[0] != '\0') ? lang : "zh";
}

inline void MhwRng_ExtractCanonicalPrimary(const char* raw, char outPrimary[12]) noexcept {
    outPrimary[0] = '\0';
    if (!raw) return;
    while (*raw == ' ' || *raw == '\t') ++raw;
    std::size_t len = 0U;
    while (len < 11U && raw[len] != '\0' && raw[len] != '-' && raw[len] != '_' &&
           raw[len] != ' ' && raw[len] != '\t') {
        char c = raw[len];
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
        outPrimary[len++] = c;
    }
    outPrimary[len] = '\0';
    auto eq = [&](const char* lit) noexcept {
        std::size_t i = 0U;
        while (outPrimary[i] != '\0' && lit[i] != '\0') {
            if (outPrimary[i] != lit[i]) return false;
            ++i;
        }
        return outPrimary[i] == '\0' && lit[i] == '\0';
    };
    if (eq("cn") || eq("chs") || eq("cht") || eq("chinese")) {
        outPrimary[0] = 'z'; outPrimary[1] = 'h'; outPrimary[2] = '\0';
    } else if (eq("eng") || eq("english")) {
        outPrimary[0] = 'e'; outPrimary[1] = 'n'; outPrimary[2] = '\0';
    } else if (eq("jp") || eq("jpn") || eq("japanese")) {
        outPrimary[0] = 'j'; outPrimary[1] = 'a'; outPrimary[2] = '\0';
    } else if (eq("kr") || eq("kor") || eq("korean")) {
        outPrimary[0] = 'k'; outPrimary[1] = 'o'; outPrimary[2] = '\0';
    }
}

inline bool MhwRng_LangMatchesPrimary(const char* a, const char* b) noexcept {
    char pa[12]{}, pb[12]{};
    MhwRng_ExtractCanonicalPrimary(a, pa);
    MhwRng_ExtractCanonicalPrimary(b, pb);
    if (pa[0] == '\0' || pb[0] == '\0') return false;
    std::size_t i = 0U;
    while (pa[i] != '\0' && pb[i] != '\0') {
        if (pa[i] != pb[i]) return false;
        ++i;
    }
    return pa[i] == '\0' && pb[i] == '\0';
}

inline bool MhwRng_HostIsEnglish(const MhwRngHostApi* host) noexcept {
    const char* lang = MhwRng_HostGetLanguage(host);
    return !MhwRng_LangMatchesPrimary(lang, "zh");
}

inline const char* MhwRng_SelectTextFallback(const char* activeLang,
                                             const MhwRngLocaleText* entries,
                                             std::size_t count,
                                             const char* fallbackText) noexcept {
    if (entries && count > 0U) {
        const char* cur = (activeLang && activeLang[0] != '\0') ? activeLang : "zh";
        for (std::size_t i = 0U; i < count; ++i) {
            if (entries[i].text && entries[i].text[0] != '\0' &&
                MhwRng_LangMatchesPrimary(entries[i].lang, cur)) {
                return entries[i].text;
            }
        }
        if (!MhwRng_LangMatchesPrimary(cur, "en")) {
            for (std::size_t i = 0U; i < count; ++i) {
                if (entries[i].text && entries[i].text[0] != '\0' &&
                    MhwRng_LangMatchesPrimary(entries[i].lang, "en")) {
                    return entries[i].text;
                }
            }
        }
        if (!MhwRng_LangMatchesPrimary(cur, "zh")) {
            for (std::size_t i = 0U; i < count; ++i) {
                if (entries[i].text && entries[i].text[0] != '\0' &&
                    MhwRng_LangMatchesPrimary(entries[i].lang, "zh")) {
                    return entries[i].text;
                }
            }
        }
        for (std::size_t i = 0U; i < count; ++i) {
            if (entries[i].text && entries[i].text[0] != '\0') {
                return entries[i].text;
            }
        }
    }
    return fallbackText ? fallbackText : "";
}

inline const char* MhwRng_TrTable(const MhwRngHostApi* host,
                                  const MhwRngLocaleText* entries,
                                  std::size_t count,
                                  const char* fallbackText = "") noexcept {
    if (host &&
        host->structSize >= offsetof(MhwRngHostApi, SelectText) + sizeof(host->SelectText) &&
        host->SelectText) {
        const char* res = host->SelectText(entries, count, fallbackText);
        if (res) return res;
    }
    return MhwRng_SelectTextFallback(MhwRng_HostGetLanguage(host), entries, count, fallbackText);
}

inline const char* MhwRng_Tr(const MhwRngHostApi* host,
                             const char* zhUtf8,
                             const char* enUtf8) noexcept {
    const MhwRngLocaleText pair[2] = {
        { "zh", zhUtf8 },
        { "en", enUtf8 }
    };
    const char* fb = (zhUtf8 && zhUtf8[0] != '\0') ? zhUtf8 : (enUtf8 ? enUtf8 : "");
    return MhwRng_TrTable(host, pair, 2U, fb);
}

inline bool MhwRng_RegisterPluginInfoI18n(const MhwRngHostApi* host,
                                          MhwRngPluginId selfId,
                                          const MhwRngLocaleText* displayNames,
                                          std::size_t displayCount,
                                          const MhwRngLocaleText* descriptions,
                                          std::size_t descCount) noexcept {
    if (!host) return false;
    if (host->structSize < offsetof(MhwRngHostApi, RegisterPluginInfoI18n) + sizeof(host->RegisterPluginInfoI18n)) {
        return false;
    }
    if (!host->RegisterPluginInfoI18n) return false;
    return host->RegisterPluginInfoI18n(selfId, displayNames, displayCount, descriptions, descCount);
}

inline bool MhwRng_RegisterPluginInfoBi(const MhwRngHostApi* host,
                                        MhwRngPluginId selfId,
                                        const char* zhDisplayName,
                                        const char* enDisplayName,
                                        const char* zhDescription,
                                        const char* enDescription) noexcept {
    const MhwRngLocaleText names[2] = { { "zh", zhDisplayName }, { "en", enDisplayName } };
    const MhwRngLocaleText descs[2] = { { "zh", zhDescription }, { "en", enDescription } };
    return MhwRng_RegisterPluginInfoI18n(host, selfId, names, 2U, descs, 2U);
}

inline bool MhwRng_RegisterSubcommandI18n(const MhwRngHostApi* host,
                                          MhwRngPluginId selfId,
                                          const char* subcmd,
                                          const char* usageExample,
                                          const MhwRngLocaleText* helpTexts,
                                          std::size_t helpCount,
                                          MhwRngSubcommandFn fn,
                                          void* userData) noexcept {
    if (!host) return false;
    if (host->structSize >= offsetof(MhwRngHostApi, RegisterSubcommandI18n) + sizeof(host->RegisterSubcommandI18n) &&
        host->RegisterSubcommandI18n) {
        return host->RegisterSubcommandI18n(selfId, subcmd, usageExample, helpTexts, helpCount, fn, userData);
    }
    if (!host->RegisterSubcommand) return false;
    const char* singleHelp = MhwRng_SelectTextFallback(MhwRng_HostGetLanguage(host), helpTexts, helpCount, "");
    return host->RegisterSubcommand(selfId, subcmd, usageExample, singleHelp, fn, userData);
}

inline bool MhwRng_RegisterSubcommandBi(const MhwRngHostApi* host,
                                        MhwRngPluginId selfId,
                                        const char* subcmd,
                                        const char* usageExample,
                                        const char* zhHelp,
                                        const char* enHelp,
                                        MhwRngSubcommandFn fn,
                                        void* userData) noexcept {
    const MhwRngLocaleText helps[2] = { { "zh", zhHelp }, { "en", enHelp } };
    return MhwRng_RegisterSubcommandI18n(host, selfId, subcmd, usageExample, helps, 2U, fn, userData);
}

inline bool MhwRng_RegisterSubcommandCompleter(const MhwRngHostApi* host,
                                               MhwRngPluginId selfId,
                                               const char* subcmd,
                                               MhwRngSubcommandCompleteFn completer,
                                               void* userData = nullptr) noexcept {
    if (!host || !subcmd || !completer) return false;
    if (host->structSize < offsetof(MhwRngHostApi, RegisterSubcommandCompleter) + sizeof(host->RegisterSubcommandCompleter)) {
        return false;
    }
    if (!host->RegisterSubcommandCompleter) return false;
    return host->RegisterSubcommandCompleter(selfId, subcmd, completer, userData);
}

inline bool MhwRng_RegisterSiteMetaI18n(const MhwRngHostApi* host,
                                        MhwRngPluginId selfId,
                                        std::uint32_t rva,
                                        const char* shortAlias,
                                        const MhwRngLocaleText* descriptions,
                                        std::size_t descCount,
                                        MhwRngValueFormatterFn formatterOrNull,
                                        void* userData) noexcept {
    if (!host) return false;
    if (host->structSize >= offsetof(MhwRngHostApi, RegisterSiteMetaI18n) + sizeof(host->RegisterSiteMetaI18n) &&
        host->RegisterSiteMetaI18n) {
        return host->RegisterSiteMetaI18n(selfId, rva, shortAlias, descriptions, descCount, formatterOrNull, userData);
    }
    if (!host->RegisterSiteMeta) return false;
    const char* singleDesc = MhwRng_SelectTextFallback(MhwRng_HostGetLanguage(host), descriptions, descCount, "");
    return host->RegisterSiteMeta(selfId, rva, shortAlias, singleDesc, formatterOrNull, userData);
}

inline bool MhwRng_RegisterSiteMetaBi(const MhwRngHostApi* host,
                                      MhwRngPluginId selfId,
                                      std::uint32_t rva,
                                      const char* shortAlias,
                                      const char* zhDescription,
                                      const char* enDescription,
                                      MhwRngValueFormatterFn formatterOrNull,
                                      void* userData) noexcept {
    const MhwRngLocaleText descs[2] = { { "zh", zhDescription }, { "en", enDescription } };
    return MhwRng_RegisterSiteMetaI18n(host, selfId, rva, shortAlias, descs, 2U, formatterOrNull, userData);
}

inline std::uint32_t MhwRng_HostCaptureCallerRvas(const MhwRngHostApi* host,
                                                  const MhwRngDrawContext* ctx,
                                                  std::uint32_t* outRvas,
                                                  std::uint32_t maxDepth) noexcept {
    if (!host || !ctx || !outRvas || maxDepth == 0U) return 0U;
    if (host->structSize < offsetof(MhwRngHostApi, CaptureCallerRvas) + sizeof(host->CaptureCallerRvas)) return 0U;
    if (!host->CaptureCallerRvas) return 0U;
    return host->CaptureCallerRvas(ctx, outRvas, maxDepth);
}

inline MhwRngHookHandle MhwRng_HostRegisterMainThreadTick(const MhwRngHostApi* host,
                                                          MhwRngPluginId selfId,
                                                          MhwRngMainThreadTickFn fn,
                                                          void* userData = nullptr) noexcept {
    if (!host || !fn) return kMhwRngInvalidHandle;
    if (host->structSize < offsetof(MhwRngHostApi, RegisterMainThreadTick) + sizeof(host->RegisterMainThreadTick)) {
        return kMhwRngInvalidHandle;
    }
    if (!host->RegisterMainThreadTick) return kMhwRngInvalidHandle;
    return host->RegisterMainThreadTick(selfId, fn, userData);
}

inline void MhwRng_HostUnregisterMainThreadTick(const MhwRngHostApi* host,
                                                 MhwRngPluginId selfId,
                                                 MhwRngHookHandle handle) noexcept {
    if (!host || handle == kMhwRngInvalidHandle) return;
    if (host->structSize < offsetof(MhwRngHostApi, UnregisterMainThreadTick) + sizeof(host->UnregisterMainThreadTick)) {
        return;
    }
    if (host->UnregisterMainThreadTick) host->UnregisterMainThreadTick(selfId, handle);
}

inline MhwRngHookHandle MhwRng_HostSubscribeEvent(const MhwRngHostApi* host,
                                                  MhwRngPluginId selfId,
                                                  const char* topicPattern,
                                                  MhwRngCustomEventFn fn,
                                                  void* userData = nullptr) noexcept {
    if (!host || !topicPattern || !fn) return kMhwRngInvalidHandle;
    if (host->structSize < offsetof(MhwRngHostApi, SubscribeEvent) + sizeof(host->SubscribeEvent)) {
        return kMhwRngInvalidHandle;
    }
    if (!host->SubscribeEvent) return kMhwRngInvalidHandle;
    return host->SubscribeEvent(selfId, topicPattern, fn, userData);
}

inline void MhwRng_HostUnsubscribeEvent(const MhwRngHostApi* host,
                                          MhwRngPluginId selfId,
                                          MhwRngHookHandle handle) noexcept {
    if (!host || handle == kMhwRngInvalidHandle) return;
    if (host->structSize < offsetof(MhwRngHostApi, UnsubscribeEvent) + sizeof(host->UnsubscribeEvent)) {
        return;
    }
    if (host->UnsubscribeEvent) host->UnsubscribeEvent(selfId, handle);
}

inline std::uint32_t MhwRng_HostEmitEvent(const MhwRngHostApi* host,
                                          MhwRngPluginId selfId,
                                          const char* topic,
                                          const void* payload = nullptr,
                                          std::uint32_t payloadSize = 0U) noexcept {
    if (!host || !topic || topic[0] == '\0') return 0U;
    if (host->structSize < offsetof(MhwRngHostApi, EmitEvent) + sizeof(host->EmitEvent)) {
        return 0U;
    }
    if (!host->EmitEvent) return 0U;
    return host->EmitEvent(selfId, topic, payload, payloadSize);
}

inline bool MhwRng_HostRegisterService(const MhwRngHostApi* host,
                                       MhwRngPluginId selfId,
                                       const char* serviceName,
                                       std::uint32_t version,
                                       const void* apiTable) noexcept {
    if (!host || !serviceName || !apiTable || version == 0U) return false;
    if (host->structSize < offsetof(MhwRngHostApi, RegisterService) + sizeof(host->RegisterService)) {
        return false;
    }
    if (!host->RegisterService) return false;
    return host->RegisterService(selfId, serviceName, version, apiTable);
}

inline void MhwRng_HostUnregisterService(const MhwRngHostApi* host,
                                         MhwRngPluginId selfId,
                                         const char* serviceName) noexcept {
    if (!host || !serviceName) return;
    if (host->structSize < offsetof(MhwRngHostApi, UnregisterService) + sizeof(host->UnregisterService)) {
        return;
    }
    if (host->UnregisterService) host->UnregisterService(selfId, serviceName);
}

inline MhwRngServiceLease MhwRng_HostAcquireService(const MhwRngHostApi* host,
                                                    const char* serviceName,
                                                    std::uint32_t minVersion,
                                                    MhwRngServiceInfo* outInfo) noexcept {
    if (!host || !serviceName || !outInfo) return kMhwRngInvalidLease;
    if (host->structSize < offsetof(MhwRngHostApi, AcquireService) + sizeof(host->AcquireService)) {
        return kMhwRngInvalidLease;
    }
    if (!host->AcquireService) return kMhwRngInvalidLease;
    return host->AcquireService(serviceName, minVersion, outInfo);
}

inline void MhwRng_HostReleaseService(const MhwRngHostApi* host, MhwRngServiceLease lease) noexcept {
    if (!host || lease == kMhwRngInvalidLease) return;
    if (host->structSize < offsetof(MhwRngHostApi, ReleaseService) + sizeof(host->ReleaseService)) {
        return;
    }
    if (host->ReleaseService) host->ReleaseService(lease);
}

template <typename ApiT>
class MhwRngServiceGuard {
public:
    MhwRngServiceGuard(const MhwRngHostApi* host,
                       const char* serviceName,
                       std::uint32_t minVersion = 1U,
                       std::size_t minStructSize = sizeof(ApiT)) noexcept
        : host_(host) {
        if (!host || !serviceName) return;
        MhwRngServiceInfo info{};
        info.structSize = sizeof(info);
        const MhwRngServiceLease lease = MhwRng_HostAcquireService(host, serviceName, minVersion, &info);
        if (lease == kMhwRngInvalidLease || !info.apiTable) {
            if (lease != kMhwRngInvalidLease) MhwRng_HostReleaseService(host, lease);
            return;
        }
        const auto* tbl = static_cast<const ApiT*>(info.apiTable);
        if (tbl->structSize < minStructSize || tbl->apiVersion < minVersion) {
            MhwRng_HostReleaseService(host, lease);
            return;
        }
        lease_   = lease;
        api_     = tbl;
        version_ = info.version;
    }

    ~MhwRngServiceGuard() noexcept { Reset(); }

    MhwRngServiceGuard(const MhwRngServiceGuard&)            = delete;
    MhwRngServiceGuard& operator=(const MhwRngServiceGuard&) = delete;

    MhwRngServiceGuard(MhwRngServiceGuard&& other) noexcept
        : host_(other.host_), lease_(other.lease_), api_(other.api_), version_(other.version_) {
        other.lease_   = kMhwRngInvalidLease;
        other.api_     = nullptr;
        other.version_ = 0U;
    }

    MhwRngServiceGuard& operator=(MhwRngServiceGuard&& other) noexcept {
        if (this != &other) {
            Reset();
            host_          = other.host_;
            lease_         = other.lease_;
            api_           = other.api_;
            version_       = other.version_;
            other.lease_   = kMhwRngInvalidLease;
            other.api_     = nullptr;
            other.version_ = 0U;
        }
        return *this;
    }

    void Reset() noexcept {
        if (lease_ != kMhwRngInvalidLease) {
            MhwRng_HostReleaseService(host_, lease_);
            lease_ = kMhwRngInvalidLease;
        }
        api_     = nullptr;
        version_ = 0U;
    }

    explicit operator bool() const noexcept { return api_ != nullptr; }
    const ApiT* operator->() const noexcept { return api_; }
    const ApiT* Get() const noexcept { return api_; }
    std::uint32_t Version() const noexcept { return version_; }

private:
    const MhwRngHostApi* host_    = nullptr;
    MhwRngServiceLease   lease_   = kMhwRngInvalidLease;
    const ApiT*          api_     = nullptr;
    std::uint32_t        version_ = 0U;
};

// Every DLL in `nativePC\plugins\mhw_rng\*.dll` exports these 3 functions:
using MhwRngPluginGetInfoFn  = const MhwRngPluginDescriptor*(__cdecl*)();
using MhwRngPluginInitFn     = bool(__cdecl*)(const MhwRngHostApi* host, MhwRngPluginId selfId);
using MhwRngPluginShutdownFn = void(__cdecl*)();

// ============================================================================
//  Exported C Functions on `mhw_rng.dll` (for external DLLs like TAS / HFI)
// ============================================================================
MHW_RNG_API std::uint32_t        MhwRng_GetApiVersion();
MHW_RNG_API const MhwRngHostApi* MhwRng_GetHostApi();
// IsTakeoverEnabled() returns true for both kMhwRngModeTakeover and kMhwRngModeSelective;
// SetTakeoverEnabled(true) keeps Takeover/Selective if active or enters kMhwRngModeSelective.
MHW_RNG_API void                 MhwRng_SetTakeoverEnabled(bool enabled);
MHW_RNG_API bool                 MhwRng_IsTakeoverEnabled();
MHW_RNG_API void                 MhwRng_SetEngineMode(std::uint32_t mode);
MHW_RNG_API std::uint32_t        MhwRng_GetEngineMode();
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

MHW_RNG_API bool                 MhwRng_RestoreSlotState(const MhwRngSlotState* state);
MHW_RNG_API std::uint32_t        MhwRng_CaptureStateBlob(void* outBuf, std::uint32_t bufCap);
MHW_RNG_API bool                 MhwRng_RestoreStateBlob(const void* buf, std::uint32_t bufSize);
MHW_RNG_API bool                 MhwRng_SetInstancePolicy(std::uintptr_t instancePtr, std::uint32_t policy);
MHW_RNG_API std::uint32_t        MhwRng_GetInstancePolicy(std::uintptr_t instancePtr);
MHW_RNG_API const char*          MhwRng_GetLanguage();
MHW_RNG_API std::uint32_t        MhwRng_GetLanguageId();
MHW_RNG_API void                 MhwRng_SetLanguage(const char* lang);
MHW_RNG_API const char*          MhwRng_SelectText(const MhwRngLocaleText* entries,
                                                   std::size_t count,
                                                   const char* fallbackText);
MHW_RNG_API bool                 MhwRng_ExecuteCommand(const char* commandLine);
MHW_RNG_API std::uint32_t        MhwRng_CaptureCallerRvas(const MhwRngDrawContext* ctx,
                                                          std::uint32_t* outRvas,
                                                          std::uint32_t maxDepth);
MHW_RNG_API void                 MhwRng_PumpMainThreadTicks();
