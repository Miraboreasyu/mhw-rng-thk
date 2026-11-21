#pragma once
// ============================================================================
//  Built-in Declarative Fallback for `fatalis_p1_nasty.txt`
//  Used when `thk_presets\fatalis_p1_nasty.txt` is not present on disk.
//  Parsed by the exact same `ParsePresetFromText` parser in `thk_preset_io.h`.
// ============================================================================

namespace thk::presets {

inline constexpr const char kBuiltinFatalisP1NastyPresetTxt[] = R"PRESET(# Built-in Fallback Preset: Fatalis Phase 1 Bipedal Nasty
name = fatalis_p1_nasty
desc = 黑龙(em013)一阶段二足全距离纯RNG控制预设
disable_tag = far_multi

rule mon=101 d=0 f=0 node=75,76 seg0=* -> 55:297 cond=1011 label="P1 <900 孕吐"
rule mon=101 d=0 f=0 node=129,130 seg0=* -> 55:297 cond=1011 tag=guard label="P1 倒后<900 孕吐"

rule mon=101 d=1 f=55 node=87 caller=75,76 seg0=* -> ENDR cond=1012 label="P1 孕吐不派生扇火"
rule mon=101 d=1 f=55 node=87 caller=129,130 seg0=* -> ENDR cond=1012 tag=guard label="P1 倒后孕吐不派生扇火"

rule mon=101 d=0 f=0 node=77 seg0=1 -> 55:184 cond=1013 label="P1 900~2000 正面地毯火"
rule mon=101 d=0 f=0 node=131 seg0=1 -> 55:184 cond=1013 tag=guard label="P1 倒后900~2000 正面地毯火"

rule mon=101 d=0 f=0 node=77 seg0=13 -> 55:175 cond=1015 label="P1 900~2000 背面甩尾"
rule mon=101 d=0 f=0 node=131 seg0=12 -> 55:175 cond=1015 tag=guard label="P1 倒后900~2000 背面甩尾"

rule mon=101 d=0 f=0 node=77 seg0=25+ -> 55:188 cond=1014 label="P1 900~2000 侧面地毯火"
rule mon=101 d=0 f=0 node=131 seg0=25+ -> 55:188 cond=1014 tag=guard label="P1 倒后900~2000 侧面地毯火"

rule mon=101 d=0 f=0 node=78 seg0=* -> 55:184|55:188 cond=1016 label="P1 2000~4500 地毯火"
rule mon=101 d=0 f=0 node=132 seg0=* -> 55:184|55:188 cond=1016 tag=guard label="P1 倒后2000~4500 地毯火"

rule mon=101 d=0 f=0 node=79 seg0=* -> 55:179 cond=1017 tag=far_single label="P1 >4500 单发火球"
rule mon=101 d=0 f=0 node=133 seg0=* -> 55:179 cond=1017 tag=guard,far_single label="P1 倒后>4500 单发火球"
rule mon=101 d=0 f=0 node=79 seg0=* -> 55:220 cond=1017 tag=far_multi label="P1 >4500 三连火球"
rule mon=101 d=0 f=0 node=133 seg0=* -> 55:220 cond=1017 tag=guard,far_multi label="P1 倒后>4500 三连火球"

rule mon=101 d=0 f=0 node=46 seg0=53+ -> 55:184|55:188 cond=1016 tag=guard label="P1 遮挡补偿地毯火"
rule mon=101 d=0 f=0 node=18 seg0=7 -> ENDR cond=1018 tag=guard label="P1 跳过15%额外龙车"
rule mon=101 d=0 f=0 node=14 seg0=22 -> ENDR cond=1018 tag=guard label="P1 维持二足态(51612)"
rule mon=101 d=0 f=0 node=15 seg0=14 -> ENDR cond=1018 tag=guard label="P1 维持二足态(51613)"
rule mon=101 d=0 f=0 node=14 seg0=49 -> RANDOM cond=1018 tag=guard label="P1 加快站立回二足(51612)"
rule mon=101 d=0 f=0 node=15 seg0=41 -> RANDOM cond=1018 tag=guard label="P1 加快站立回二足(51613)"
)PRESET";

} // namespace thk::presets
