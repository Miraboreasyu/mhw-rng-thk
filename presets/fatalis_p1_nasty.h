#pragma once
// ============================================================================
//  Built-in Declarative Fallback for `fatalis_p1_nasty.txt`
//  Used when `thk_presets\fatalis_p1_nasty.txt` is not present on disk.
//  Parsed by the exact same `ParsePresetFromText` parser in `thk_preset_io.h`.
// ============================================================================

namespace thk::presets {

inline constexpr const char kBuiltinFatalisP1NastyPresetTxt[] = R"PRESET(# Built-in Fallback Preset: Fatalis Phase 1 Full Nasty (Bipedal + Quadruped)
name = fatalis_p1_nasty
desc = 黑龙(em013)一阶段二足+四足全距离纯RNG控制预设
desc_en = Fatalis (em013) P1 full-range pure-RNG AI preset (bipedal + quadruped)
disable_tag = far_multi,bipedal_lock

# Bipedal
rule mon=101 d=0 f=0,4 node=75,76 seg0=* -> 55:297 cond=1011 label="P1 2足<900 孕吐" label_en="P1 Bi <900 Cone"
rule mon=101 d=0 f=0,4 node=129,130 seg0=* -> 55:297 cond=1011 tag=guard label="P1 2足倒后<900 孕吐" label_en="P1 Bi Knock <900 Cone"
rule mon=101 d=1 f=55 node=87 caller=75,76 seg0=* -> ENDR cond=1012 label="P1 2足孕吐不派生扇火" label_en="P1 Bi No Cone Chain"
rule mon=101 d=1 f=55 node=87 caller=129,130 seg0=* -> ENDR cond=1012 tag=guard label="P1 2足倒后孕吐不派生扇火" label_en="P1 Bi Knock No Cone Chain"
rule mon=101 d=0 f=0,4 node=77 seg0=1 -> 55:184 cond=1013 label="P1 2足900~2000 正面地毯火" label_en="P1 Bi 900-2000 Front Carpet"
rule mon=101 d=0 f=0,4 node=131 seg0=1 -> 55:184 cond=1013 tag=guard label="P1 2足倒后900~2000 正面地毯火" label_en="P1 Bi Knock 900-2000 Front Carpet"
rule mon=101 d=0 f=0,4 node=77 seg0=13 -> 55:175 cond=1015 label="P1 2足900~2000 背面甩尾" label_en="P1 Bi 900-2000 Back TailWhip"
rule mon=101 d=0 f=0,4 node=131 seg0=12 -> 55:175 cond=1015 tag=guard label="P1 2足倒后900~2000 背面甩尾" label_en="P1 Bi Knock 900-2000 Back TailWhip"
rule mon=101 d=0 f=0,4 node=77 seg0=25+ -> 55:188 cond=1014 label="P1 2足900~2000 侧面地毯火" label_en="P1 Bi 900-2000 Side Carpet"
rule mon=101 d=0 f=0,4 node=131 seg0=25+ -> 55:188 cond=1014 tag=guard label="P1 2足倒后900~2000 侧面地毯火" label_en="P1 Bi Knock 900-2000 Side Carpet"
rule mon=101 d=0 f=0,4 node=78 seg0=* -> 55:184|55:188 cond=1016 label="P1 2足2000~4500 地毯火" label_en="P1 Bi 2000-4500 Carpet"
rule mon=101 d=0 f=0,4 node=132 seg0=* -> 55:184|55:188 cond=1016 tag=guard label="P1 2足倒后2000~4500 地毯火" label_en="P1 Bi Knock 2000-4500 Carpet"
rule mon=101 d=0 f=0,4 node=79 seg0=* -> 55:179 cond=1017 tag=far_single label="P1 2足>4500 单发火球" label_en="P1 Bi >4500 Single Fireball"
rule mon=101 d=0 f=0,4 node=133 seg0=* -> 55:179 cond=1017 tag=guard,far_single label="P1 2足倒后>4500 单发火球" label_en="P1 Bi Knock >4500 Single Fireball"
rule mon=101 d=0 f=0,4 node=79 seg0=* -> 55:220 cond=1017 tag=far_multi label="P1 2足>4500 三连火球" label_en="P1 Bi >4500 Triple Fireball"
rule mon=101 d=0 f=0,4 node=133 seg0=* -> 55:220 cond=1017 tag=guard,far_multi label="P1 2足倒后>4500 三连火球" label_en="P1 Bi Knock >4500 Triple Fireball"
rule mon=101 d=0 f=0,4 node=46 seg0=53+ -> 55:184|55:188 cond=1016 tag=guard label="P1 2足遮挡补偿地毯火" label_en="P1 Bi Occluded Carpet"
rule mon=101 d=0 f=0,4 node=18 seg0=7 -> ENDR cond=1018 tag=guard label="P1 跳过15%额外龙车" label_en="P1 Skip 15% Extra Charge"
rule mon=101 d=0 f=0,4 node=14 seg0=22 -> ENDR cond=1018 tag=bipedal_lock label="P1 维持二足态(51612)" label_en="P1 Hold Bipedal (51612)"
rule mon=101 d=0 f=0,4 node=15 seg0=14 -> ENDR cond=1018 tag=bipedal_lock label="P1 维持二足态(51613)" label_en="P1 Hold Bipedal (51613)"
rule mon=101 d=0 f=0,4 node=14 seg0=49 -> RANDOM cond=1018 tag=bipedal_lock label="P1 加快站立回二足(51612)" label_en="P1 Stand Bipedal Fast (51612)"
rule mon=101 d=0 f=0,4 node=15 seg0=41 -> RANDOM cond=1018 tag=bipedal_lock label="P1 加快站立回二足(51613)" label_en="P1 Stand Bipedal Fast (51613)"

# Quadruped
rule mon=101 d=0 f=0,4 node=104 seg0=* -> 55:216 cond=1021 label="P1 4足(0~500未车) 蛇形咬" label_en="P1 Quad 0-500 NoChg Snap"
rule mon=101 d=0 f=0,4 node=158 seg0=* -> 55:216 cond=1021 tag=guard label="P1 4足倒后(0~500未车) 蛇形咬" label_en="P1 Quad Knock 0-500 NoChg Snap"
rule mon=101 d=0 f=0,4 node=109 seg0=* -> 55:277 cond=1022 label="P1 4足(0~500已车) 后撤地毯火" label_en="P1 Quad 0-500 Chgd Snapback"
rule mon=101 d=0 f=0,4 node=163 seg0=* -> 55:277 cond=1022 tag=guard label="P1 4足倒后(0~500已车) 后撤地毯火" label_en="P1 Quad Knock 0-500 Chgd Snapback"
rule mon=101 d=0 f=0,4 node=105,106,110,111 seg0=* -> 55:176 cond=1025 label="P1 4足后方 尾砸" label_en="P1 Quad Back TailSlam"
rule mon=101 d=0 f=0,4 node=159,160,164,165 seg0=* -> 55:176 cond=1025 tag=guard label="P1 4足倒后后方 尾砸" label_en="P1 Quad Knock Back TailSlam"
rule mon=101 d=0 f=0,4 node=105,106 seg0=* -> 55:216 cond=1023 label="P1 4足(500~1500正面未车) 蛇形咬" label_en="P1 Quad 500-1500 Front NoChg Snap"
rule mon=101 d=0 f=0,4 node=159,160 seg0=* -> 55:216 cond=1023 tag=guard label="P1 4足倒后(500~1500未车) 蛇形咬" label_en="P1 Quad Knock 500-1500 NoChg Snap"
rule mon=101 d=0 f=0,4 node=110,111 seg0=* -> 55:217(50)+55:277(50) cond=1024 label="P1 4足(500~1500正面已车) 蓄力火/后撤地毯" label_en="P1 Quad 500-1500 Chgd 50/50"
rule mon=101 d=0 f=0,4 node=164,165 seg0=* -> 55:217(50)+55:277(50) cond=1024 tag=guard label="P1 4足倒后(500~1500已车) 蓄力火/后撤地毯" label_en="P1 Quad Knock 500-1500 Chgd 50/50"
rule mon=101 d=0 f=0,4 node=107,112 seg0=* -> 55:180 cond=1026 tag=far_single label="P1 4足(1500~5000) 单发火球" label_en="P1 Quad 1500-5000 Single Fireball"
rule mon=101 d=0 f=0,4 node=161,166 seg0=* -> 55:180 cond=1026 tag=guard,far_single label="P1 4足倒后(1500~5000) 单发火球" label_en="P1 Quad Knock 1500-5000 Single"
rule mon=101 d=0 f=0,4 node=107,112 seg0=* -> 55:221 cond=1026 tag=far_multi label="P1 4足(1500~5000) 三连火球" label_en="P1 Quad 1500-5000 Triple Fireball"
rule mon=101 d=0 f=0,4 node=161,166 seg0=* -> 55:221 cond=1026 tag=guard,far_multi label="P1 4足倒后(1500~5000) 三连火球" label_en="P1 Quad Knock 1500-5000 Triple"
rule mon=101 d=0 f=0,4 node=108,113 seg0=* -> 55:180 cond=1027 tag=far_single label="P1 4足(>5000) 单发火球" label_en="P1 Quad >5000 Single Fireball"
rule mon=101 d=0 f=0,4 node=162,167 seg0=* -> 55:180 cond=1027 tag=guard,far_single label="P1 4足倒后(>5000) 单发火球" label_en="P1 Quad Knock >5000 Single Fireball"
rule mon=101 d=0 f=0,4 node=108,113 seg0=* -> 55:221 cond=1027 tag=far_multi label="P1 4足(>5000) 三连火球" label_en="P1 Quad >5000 Triple Fireball"
rule mon=101 d=0 f=0,4 node=162,167 seg0=* -> 55:221 cond=1027 tag=guard,far_multi label="P1 4足倒后(>5000) 三连火球" label_en="P1 Quad Knock >5000 Triple Fireball"
rule mon=101 d=0 f=0,4 node=47 seg0=2..23 -> 55:180 cond=1026 tag=guard,far_single label="P1 4足遮挡补偿 单发火球" label_en="P1 Quad Occluded Single Fireball"
rule mon=101 d=0 f=0,4 node=47 seg0=2..23 -> 55:221 cond=1026 tag=guard,far_multi label="P1 4足遮挡补偿 三连火球" label_en="P1 Quad Occluded Triple Fireball"
)PRESET";

} // namespace thk::presets
