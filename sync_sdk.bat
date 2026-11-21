@echo off
:: ============================================================================
:: Sync `include\mhw_rng_api.h` from the `mhw_rng` framework repository.
:: Usage:
::   sync_sdk.bat                          (uses %MHW_RNG_SDK_DIR% or ..\..\include)
::   sync_sdk.bat D:\repos\mhw-rng         (syncs from specified framework root/include)
:: ============================================================================
setlocal

set "ROOT=%~dp0"
set "SRC_ARG=%~1"
set "CAND_HDR="

if not "%SRC_ARG%"=="" (
    if exist "%SRC_ARG%\include\mhw_rng_api.h" set "CAND_HDR=%SRC_ARG%\include\mhw_rng_api.h"
    if exist "%SRC_ARG%\mhw_rng_api.h" set "CAND_HDR=%SRC_ARG%\mhw_rng_api.h"
)

if "%CAND_HDR%"=="" if not "%MHW_RNG_SDK_DIR%"=="" (
    if exist "%MHW_RNG_SDK_DIR%\include\mhw_rng_api.h" set "CAND_HDR=%MHW_RNG_SDK_DIR%\include\mhw_rng_api.h"
    if exist "%MHW_RNG_SDK_DIR%\mhw_rng_api.h" set "CAND_HDR=%MHW_RNG_SDK_DIR%\mhw_rng_api.h"
)

if "%CAND_HDR%"=="" (
    if exist "%ROOT%..\..\include\mhw_rng_api.h" set "CAND_HDR=%ROOT%..\..\include\mhw_rng_api.h"
)

if "%CAND_HDR%"=="" (
    echo ERROR: Could not locate mhw_rng_api.h.
    echo Pass the framework path explicitly: sync_sdk.bat ^<path-to-mhw-rng^>
    exit /b 1
)

if not exist "%ROOT%include" mkdir "%ROOT%include"
copy /y "%CAND_HDR%" "%ROOT%include\mhw_rng_api.h" >nul
if errorlevel 1 (
    echo ERROR: Failed copying "%CAND_HDR%"
    exit /b 1
)

echo [OK] Synced "%CAND_HDR%" -^> "%ROOT%include\mhw_rng_api.h"
exit /b 0
