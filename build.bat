@echo off
:: ============================================================================
:: Standalone Build Script for `rng_thk.dll`
:: Usage:
::   build.bat             -> builds build\rng_thk.dll
::   build.bat --deploy    -> builds and deploys DLL + presets to MHW, removing stale rng_fatalis.dll
:: ============================================================================
setlocal

set "ROOT=%~dp0"
set "INCDIR=%ROOT%include"
set "OUTDIR=%ROOT%build"
set "GAME_PLUG_DIR=C:\Program Files (x86)\Steam\steamapps\common\Monster Hunter World\nativePC\plugins\mhw_rng"

if not exist "%INCDIR%\mhw_rng_api.h" (
    if exist "%ROOT%sync_sdk.bat" call "%ROOT%sync_sdk.bat"
)
if not exist "%INCDIR%\mhw_rng_api.h" (
    echo ERROR: Missing "%INCDIR%\mhw_rng_api.h". Run sync_sdk.bat ^<path-to-mhw-rng^> first.
    exit /b 1
)

where cl.exe >nul 2>&1
if not errorlevel 1 goto :have_cl

set "VSDIR="
if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" (
    for /f "usebackq tokens=*" %%I in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%I"
)
if "%VSDIR%"=="" if exist "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" (
    set "VSDIR=C:\Program Files\Microsoft Visual Studio\2022\Community"
)
if "%VSDIR%"=="" (
    echo ERROR: MSVC x64 toolchain not found.
    exit /b 1
)
set "VSCMD_SKIP_SENDTELEMETRY=1"
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1

:have_cl
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo ===== Building rng_thk.dll =====
cl.exe /nologo /std:c++17 /O2 /GL /GR- ^
    /W4 /WX- /EHsc /MT ^
    /D "WIN32" /D "NDEBUG" /D "_WINDOWS" /D "_USRDLL" /D "NOMINMAX" ^
    /utf-8 ^
    /I "%INCDIR%" /I "%ROOT%." ^
    "%ROOT%thk_plugin.cpp" ^
    /Fe:"%OUTDIR%\rng_thk.dll" ^
    /Fo:"%OUTDIR%\\" ^
    /LD ^
    /link kernel32.lib /OPT:REF /OPT:ICF ^
    /OUT:"%OUTDIR%\rng_thk.dll"
if errorlevel 1 exit /b 1

echo [OK] Built "%OUTDIR%\rng_thk.dll"

if /I "%~1"=="--deploy" (
    if not exist "%GAME_PLUG_DIR%" mkdir "%GAME_PLUG_DIR%"
    if not exist "%GAME_PLUG_DIR%\thk_presets" mkdir "%GAME_PLUG_DIR%\thk_presets"
    if exist "%GAME_PLUG_DIR%\rng_fatalis.dll" (
        del /f /q "%GAME_PLUG_DIR%\rng_fatalis.dll" >nul 2>&1
        echo [OK] Removed superseded "%GAME_PLUG_DIR%\rng_fatalis.dll"
    )
    copy /y "%OUTDIR%\rng_thk.dll" "%GAME_PLUG_DIR%\rng_thk.dll" >nul
    if errorlevel 1 (
        echo ERROR: Failed copying to "%GAME_PLUG_DIR%\rng_thk.dll"
        exit /b 1
    )
    copy /y "%ROOT%presets\fatalis_p1_nasty.txt" "%GAME_PLUG_DIR%\thk_presets\fatalis_p1_nasty.txt" >nul
    if errorlevel 1 (
        echo ERROR: Failed copying preset to "%GAME_PLUG_DIR%\thk_presets\fatalis_p1_nasty.txt"
        exit /b 1
    )
    echo [OK] Deployed to "%GAME_PLUG_DIR%\rng_thk.dll" and thk_presets\fatalis_p1_nasty.txt ^(type `$rng reload thk` in game^)
)

exit /b 0
