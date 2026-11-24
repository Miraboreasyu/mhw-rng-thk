#pragma once
// ============================================================================
//  MIRA Optional Static RNG Catalog Service ABI (`rng_inspector.dll`)
//  Service Name : "mhw.catalog" (v1)
// ============================================================================
#include "mhw_rng_api.h"

#include <cstdint>

constexpr const char*   kMhwCatalogServiceName    = "mhw.catalog";
constexpr std::uint32_t kMhwCatalogServiceVersion = 1U;

#pragma pack(push, 8)
struct MhwCatalogApi {
    std::uint32_t apiVersion; // == 1
    std::uint32_t structSize; // sizeof(MhwCatalogApi)
    bool (__fastcall *ResolveAliasToRva)(const char* aliasOrShortcut, std::uint32_t* outRva);
    bool (__fastcall *QueryCatalogSite)(std::uint32_t rva, MhwRngSiteMeta* outMeta);
};
#pragma pack(pop)

inline bool MhwCatalog_TryResolveAlias(const MhwRngHostApi* host,
                                       const char* aliasOrShortcut,
                                       std::uint32_t* outRva) noexcept {
    if (!host || !aliasOrShortcut || aliasOrShortcut[0] == '\0' || !outRva) return false;
    MhwRngServiceGuard<MhwCatalogApi> cat(host, kMhwCatalogServiceName, kMhwCatalogServiceVersion);
    if (!cat || !cat->ResolveAliasToRva) return false;
    return cat->ResolveAliasToRva(aliasOrShortcut, outRva);
}
