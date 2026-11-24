@echo off
:: ============================================================================
:: Sync SDK headers (`mhw_rng_api.h` + companion SDK) from `mhw_rng\include\`.
:: Usage:
::   sync_sdk.bat                          (uses %MHW_RNG_SDK_DIR% or ..\..\include)
::   sync_sdk.bat D:\repos\mhw-rng         (syncs from specified framework root/include)
:: ============================================================================
setlocal

set "ROOT=%~dp0"
set "SRC_ARG=%~1"
set "CAND_DIR="

if not "%SRC_ARG%"=="" (
    if exist "%SRC_ARG%\include\mhw_rng_api.h" set "CAND_DIR=%SRC_ARG%\include"
    if exist "%SRC_ARG%\mhw_rng_api.h" set "CAND_DIR=%SRC_ARG%"
)

if "%CAND_DIR%"=="" if not "%MHW_RNG_SDK_DIR%"=="" (
    if exist "%MHW_RNG_SDK_DIR%\include\mhw_rng_api.h" set "CAND_DIR=%MHW_RNG_SDK_DIR%\include"
    if exist "%MHW_RNG_SDK_DIR%\mhw_rng_api.h" set "CAND_DIR=%MHW_RNG_SDK_DIR%"
)

if "%CAND_DIR%"=="" (
    if exist "%ROOT%..\..\include\mhw_rng_api.h" set "CAND_DIR=%ROOT%..\..\include"
)

if "%CAND_DIR%"=="" (
    echo ERROR: Could not locate mhw_rng_api.h.
    echo Pass the framework path explicitly: sync_sdk.bat ^<path-to-mhw-rng^>
    exit /b 1
)

if not exist "%ROOT%include" mkdir "%ROOT%include"
for %%H in (mhw_rng_api.h mhw_rng_math.h mhw_1523_world.h mhw_world_api.h mhw_catalog_api.h) do (
    if exist "%CAND_DIR%\%%H" (
        copy /y "%CAND_DIR%\%%H" "%ROOT%include\%%H" >nul
        if errorlevel 1 (
            echo ERROR: Failed copying "%CAND_DIR%\%%H"
            exit /b 1
        )
    )
)

echo [OK] Synced SDK headers from "%CAND_DIR%" -^> "%ROOT%include\"
exit /b 0
