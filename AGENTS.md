# AGENTS.md — `rng_thk` (`mhw-rng-thk`) Cold-Start Guide

This file is the cold-start manual for any AI agent or maintainer working in the `plugins/thk/` (`rng_thk.dll`) repository.

---

## 1. Repository Map

| Path | Role |
| :--- | :--- |
| `thk_vm.h`, `thk_engine.h`, `thk_preset_io.h`, `thk_plugin.cpp` | Generic monster-agnostic THK VM reader (`0x1339C8C`), `LastMoveContext` + `ctx`/`prev_*` + `CountGate` + sequence engine, plain-text preset parser, `$rng thk` / `$rng fatalis` commands (`rng_thk.dll` v`1.4.0`). |
| `tools/thk_meta_tool.py` | Offline THK extractor (`extract`, `init-groups`, `export-actions`, `list-tables`), zero-bloat read-only query CLI (`overview`, `moves`, `inspect`, `switches`, `followups`, `diff`, `query`, `coverage`), standalone TOML 1.0 preset compiler (`compile`), and machine-code equivalence verifier (`verify`). |
| `presets/*.txt` & `presets/fatalis_p1_nasty.h` | Compiled runtime presets deployed to `thk_presets/*.txt` (`fatalis_p1_nasty.txt` is also embedded in `fatalis_p1_nasty.h` and serves as the zero-mismatch verification baseline). |
| `presets/sources/em013/` | Source layer for Fatalis (`em013`, built from clean vanilla 15.23 baseline THKs): `em013.actions.json`, read-only `em013.thkmeta.toml`, user dictionary `em013.groups.toml`, and editable `*.preset.toml` sources. |
| `tests/run_thk_meta_tests.py` & `tests/test_prev_move.cpp` | Python compiler/verifier regression suite (`run_thk_meta_tests.py`) and standalone C++ `ThkEngine` `prev` / `ctx` / `prev_ctx` unit test (`test_prev_move.cpp`). |
| `docs/TOML_LANGUAGE_MANUAL.md` | Full TOML 1.0 preset language & `thk_meta_tool.py` CLI reference. Read on demand when writing or debugging complex TOML presets. |
| `PRESET_GUIDE.md` & `README.md` | Low-level `.txt` preset syntax manual & runtime troubleshooting (`PRESET_GUIDE.md`) and plugin overview (`README.md`). |

---

## 2. Five Non-Negotiable Engineering Invariants

1. **Zero-Bloat Read-Only Query Discipline**:
   - **Never whole-read `*.thkmeta.toml`** (`em013.thkmeta.toml` is ~373 KB / 1,600+ lines). Always query on demand with `tools/thk_meta_tool.py` (`overview`, `moves`, `inspect`, `switches`, `followups`, `diff`, `query`, `coverage`) using `--limit` / `--when` / `--json`.
2. **Source-First Preset Workflow**:
   - Edit `presets/sources/<em_id>/*.preset.toml` (or `.groups.toml`) and recompile to `presets/<name>.txt` via `compile --explain` (**must produce 0 stderr warnings**).
   - Never hand-edit generated `.txt` outputs such as `presets/fatalis_sweet.txt`. Preserve `presets/fatalis_p1_nasty.txt` intact as the equivalence baseline for `fatalis_p1_nasty.preset.toml`.
3. **C++ Kernel & ABI Safety**:
   - **Leaf-Mutex Discipline**: Never call `g_host->...` or host services while holding `g_stateMx`; snapshot under `std::lock_guard`, release lock, then call out.
   - **SEH Safety**: Memory dereferences in `ReadThkVmContextSEH` must check user-mode bounds and keep zero C++ unwindable objects inside `__try/__except`.
   - **Bilingual Hygiene (MIRA Test 20)**: All English chat/error strings gated on `MhwRng_HostIsEnglish(g_host)` must contain zero CJK characters.
4. **Engine Static Limits & Encoding**:
   - Compiled preset `.txt` files must be **UTF-8 (no BOM)** and respect runtime bounds: `kMaxFilterItems = 8` (`f`, `node`, `caller`, `prev`), `kMaxStepChoices = 8`, `kMaxRuleSteps = 16`, `char[64]` (`<= 63` UTF-8 bytes) for `tags`, `label`, `label_en`, and `ctx.name`.
   - `kMaxRuleCounters = 1024U` per monster instance via **lazy allocation** (`RuleNeedsCounter()`: stateless `do`/`weighted`/`ban`/`allow`/`prev`/`prev_ctx` rules consume `0` slots).
5. **Repo & Submodule Hygiene**:
   - Keep zero file-scheme URIs, local drive paths outside default build targets, build outputs (`build/`, `*.exe`, `*.obj`), or `__pycache__/` in tracked files.
   - When modifying `plugins/thk/` inside `mhw_rng`, commit & push the **`plugins/thk` submodule first** using its configured anonymous local author, then update the parent repo's `plugins/thk` gitlink.

---

## 3. Standard Commands (Run from `plugins/thk/`)

### Edit & Recompile an Existing Preset (`fatalis_sweet`)
```powershell
python tools/thk_meta_tool.py query --meta presets/sources/em013/em013.thkmeta.toml --groups presets/sources/em013/em013.groups.toml --when "stance=bi & dist>=900"
python tools/thk_meta_tool.py compile presets/sources/em013/fatalis_sweet.preset.toml --explain -o presets/fatalis_sweet.txt
python tools/thk_meta_tool.py coverage presets/sources/em013/fatalis_sweet.preset.toml --uncovered-only --limit 10
```

### Onboard a New Monster
```powershell
python tools/thk_meta_tool.py export-actions --dump <dump.bytes> --em-id <emXXX> -o presets/sources/<emXXX>/<emXXX>.actions.json
python tools/thk_meta_tool.py extract --main <emXXX_00.thk> --actions presets/sources/<emXXX>/<emXXX>.actions.json -o presets/sources/<emXXX>/<emXXX>.thkmeta.toml
python tools/thk_meta_tool.py init-groups --meta presets/sources/<emXXX>/<emXXX>.thkmeta.toml -o presets/sources/<emXXX>/<emXXX>.groups.toml
python tools/thk_meta_tool.py overview --meta presets/sources/<emXXX>/<emXXX>.thkmeta.toml --groups presets/sources/<emXXX>/<emXXX>.groups.toml
```

### Verification & Build
```powershell
:: 1. Python compiler/verifier + equivalence suite (omit --main-thk to skip raw bytecode replay)
python tests/run_thk_meta_tests.py --main-thk <path-to-em013_00.thk>

:: 2. Standalone C++ unit test (compile into gitignored build\ from x64 VS prompt)
cl /nologo /EHsc /std:c++17 /W4 /utf-8 /I include /I . tests\test_prev_move.cpp /Fe:build\test_prev_move.exe /Fo:build\ && build\test_prev_move.exe presets\fatalis_sweet.txt

:: 3. Standalone DLL build + local deployment to nativePC\plugins\mhw_rng\
sync_sdk.bat && build.bat --deploy
```

When invoked from parent repo `mhw_rng/`, finish with `sync_plugin_sdks.bat && build.bat && run_tests.bat` and `python tools/package_release.py --check`.
