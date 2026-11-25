#!/usr/bin/env python3
"""Regression & equivalence suite for tools/thk_meta_tool.py + plugins/thk presets."""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
EM013_SRC = os.path.join(REPO_ROOT, "presets", "sources", "em013")
sys.path.insert(0, TOOLS_DIR)
import thk_meta_tool as tmt

parser = argparse.ArgumentParser(description="Verify thk_meta_tool.py compiler/verifier and Fatalis presets.")
parser.add_argument("--meta", default=os.path.join(EM013_SRC, "em013.thkmeta.toml"), help="Path to .thkmeta.toml")
parser.add_argument("--groups", default=os.path.join(EM013_SRC, "em013.groups.toml"), help="Path to .groups.toml")
parser.add_argument(
    "--p1-preset",
    default=os.path.join(EM013_SRC, "fatalis_p1_nasty.preset.toml"),
    help="Path to fatalis_p1_nasty .preset.toml source",
)
parser.add_argument(
    "--p1-baseline",
    default=os.path.join(REPO_ROOT, "presets", "fatalis_p1_nasty.txt"),
    help="Path to baseline hand-written fatalis_p1_nasty.txt",
)
parser.add_argument(
    "--sweet-preset",
    default=os.path.join(EM013_SRC, "fatalis_sweet.preset.toml"),
    help="Path to fatalis_sweet .preset.toml source",
)
parser.add_argument(
    "--sweet-compiled",
    default=os.path.join(REPO_ROOT, "presets", "fatalis_sweet.txt"),
    help="Path to checked-in compiled fatalis_sweet.txt",
)
parser.add_argument(
    "--main-thk",
    default=os.environ.get("MHW_THK_MAIN"),
    help="Optional path to main .thk (e.g. em013_00.thk) to enable raw bytecode replay & equivalence verify; falls back to MHW_THK_MAIN",
)
parser.add_argument(
    "--sub-thk",
    default=os.environ.get("MHW_THK_SUB"),
    help="Optional path to sub .thk (auto-discovered beside --main-thk if omitted); falls back to MHW_THK_SUB",
)
ARGS = parser.parse_args()

META_PATH = os.path.abspath(ARGS.meta)
GROUPS_PATH = os.path.abspath(ARGS.groups)
P1_PRESET_PATH = os.path.abspath(ARGS.p1_preset)
P1_BASELINE_PATH = os.path.abspath(ARGS.p1_baseline)
SWEET_PRESET_PATH = os.path.abspath(ARGS.sweet_preset)
SWEET_COMPILED_PATH = os.path.abspath(ARGS.sweet_compiled)
MAIN_THK = os.path.abspath(ARGS.main_thk) if ARGS.main_thk else None
SUB_THK = os.path.abspath(ARGS.sub_thk) if ARGS.sub_thk else None

meta = tmt.load_thkmeta_toml(META_PATH)
grp = tmt.load_groups_toml(
    GROUPS_PATH,
    meta.moves,
    subnode_moves=meta.subnode_moves,
    inline_actions=meta.inline_actions,
)
d0_by_node = {}
for p in meta.d0_points:
    d0_by_node.setdefault(p.node_idx, []).append(p)


def expect_err(fn, msg_substr: str):
    try:
        fn()
    except ValueError as e:
        if msg_substr and msg_substr not in str(e):
            raise AssertionError(f"Expected {msg_substr!r} in error {str(e)!r}")
        return
    raise AssertionError(f"Expected ValueError containing {msg_substr!r}, but call succeeded")


# 1. Bug #2: Bare multi-key `|` expressions in eval_rule_selectors
n75_s1 = next(p for p in meta.d0_points if p.node_idx == 75 and p.seg0 == 1)
n16_s8 = next(p for p in meta.d0_points if p.node_idx == 16 and p.seg0 == 8)
n36_pt = next(p for p in meta.d0_points if p.node_idx == 36)
for bare_expr in ("p1=bi|p3=bi", "em_103=0|em_103=1", "(p1=bi|p3=bi)"):
    terms = tmt.split_selector_str(bare_expr)
    ok_75, _ = tmt.eval_rule_selectors(terms, n75_s1, meta, grp, d0_by_node)
    ok_16, _ = tmt.eval_rule_selectors(terms, n16_s8, meta, grp, d0_by_node)
    assert ok_75 and not ok_16, f"Bare multi-key OR failed for {bare_expr}: 75={ok_75}, 16={ok_16}"
print("[PASS] Bug #2: bare multi-key OR (`p1=bi|p3=bi`, `em_103=0|em_103=1`)")

# 2. Bug #3: Leading `=` / `==` & comparison lists + normalize_expr_syntax `==`
assert tmt.normalize_expr_syntax("phase == p1 & em_103 == 0") == "phase=p1 & em_103=0"
terms_dict = tmt.dict_to_selector_terms(
    {
        "stage": "= 0",
        "posture": "== 0",
        "em_101": ["= 0", "!= 1"],
        "dist": ["<= 500", ">= 1500"],
        "dist3d": "== 800",
    }
)
assert "stage=0" in terms_dict
assert "posture=0" in terms_dict
assert "(em_101=0|em_101!=1)" in terms_dict
assert "(dist<=500|dist>=1500)" in terms_dict
assert "dist3d=800" in terms_dict
assert tmt.dict_to_selector_terms({"angle": ["in -45..45", "in 135..225"]}) == ["angle=-45..45|135..225"]

terms_75_list = tmt.dict_to_selector_terms({"stage": "== 0", "posture": "= 0", "dist": ["< 500", ">= 900"]})
ok_list_75, _ = tmt.eval_rule_selectors(terms_75_list, n75_s1, meta, grp, d0_by_node)
assert ok_list_75, f"Expected d0.n75.s1 (dist=[0,500)) to match {terms_75_list}"
print("[PASS] Bug #3: leading `=`/`==`, comparison lists, `normalize_expr_syntax` `==`")

# 3. Bug #4: Positive finite weights & MAX_RULE_STEPS=16
for bad_do in (
    {"l2_under_breath": 0},
    {"l2_under_breath": -10},
    {"l2_under_breath": float("nan")},
    {"l2_under_breath": float("inf")},
    {"l2_under_breath": True},
    "l2_under_breath(0)+ENDR(1)",
    "l2_under_breath(-2)",
    [],
    {},
    ["l2_under_breath"] * 17,
):
    expect_err(lambda b=bad_do: tmt.do_spec_to_rhs_str(b), "")

assert tmt.do_spec_to_rhs_str(["l2_under_breath"] * 16).count(",") == 15
assert tmt.do_spec_to_rhs_str({"a": 50, "b": 25.5}) == "a(50)+b(25.5)"

with tempfile.TemporaryDirectory() as td:
    for bad_line in (
        "rule mon=101 d=0 f=0 node=75 seg0=* -> 55:297(0)+ENDR(1)",
        "rule mon=101 d=0 f=0 node=75 seg0=* -> 55:297(-1)+ENDR(1)",
        "rule mon=101 d=0 f=0 node=75 seg0=* -> 55:297(nan)+ENDR(1)",
        "rule mon=101 d=0 f=0 node=75 seg0=* -> " + ",".join(["ENDR"] * 17),
    ):
        lp = os.path.join(td, "bad.txt")
        with open(lp, "w", encoding="utf-8") as f:
            f.write(bad_line + "\n")
        expect_err(lambda p=lp: tmt.parse_low_preset(p), "")
print("[PASS] Bug #4: positive finite weights & MAX_RULE_STEPS=16 enforced cleanly")

# 4. Bug #5: ALLOWED_RULE_KEYS, on_overflow only on ban, all_banned_only_rules, >1024 stateful advisory
expect_err(
    lambda: tmt.parse_preset_rule({"whne": {"p1": "bi"}, "do": "l2_under_breath"}, 1, {}, {}, [2100]),
    "unknown key",
)
expect_err(
    lambda: tmt.parse_preset_rule(
        {"when": {"p1": "bi"}, "do": "l2_under_breath", "on_overflow": "error"}, 1, {}, {}, [2100]
    ),
    "on_overflow",
)
expect_err(
    lambda: tmt.parse_preset_rule(
        {"when": {"p1": "bi"}, "allow": ["l2_under_breath"], "on_overflow": "error"}, 1, {}, {}, [2100]
    ),
    "on_overflow",
)
pr_ok = tmt.parse_preset_rule(
    {"when": {"p1": "bi"}, "ban": ["l2_under_breath"], "on_overflow": "error"}, 1, {}, {}, [2100]
)
assert pr_ok.on_overflow == "error"

with tempfile.TemporaryDirectory() as td:
    p_cov = os.path.join(td, "cov_ban.preset.toml")
    with open(p_cov, "w", encoding="utf-8") as f:
        f.write(
            'meta = "../em013.thkmeta.toml"\n'
            'groups = "../em013.groups.toml"\n'
            'name = "cov_test"\n\n'
            "[[rules]]\n"
            'label_en = "Ban solo random"\n'
            'when = { node = 21, em_110 = 2, dist = "< 900", em_10d = true }\n'
            'ban = "rush"\n'
        )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = tmt.run_coverage(
            p_cov,
            meta_path=META_PATH,
            groups_path=GROUPS_PATH,
            as_json=True,
        )
    assert rc == 0
    cov_payload = json.loads(buf.getvalue())
    assert len(cov_payload["all_banned_only_rules"]) == 1, f"Got {cov_payload}"
    assert cov_payload["zero_match_rules"] == []

    # Test >1024 stateful vs stateless compile warning
    p_stateless = os.path.join(td, "many_stateless.preset.toml")
    p_stateful = os.path.join(td, "many_stateful.preset.toml")
    # node=75 has 10 points (2 seg groups if split), repeated rules x 120 with d=0 points hitting ~10 segs -> >1024 lines
    rules_less = "\n".join(
        f'[[rules]]\nwhen = {{ node = 75 }}\ndo = "l2_under_breath"\nlabel_en = "r{i}"\n'
        for i in range(210)
    )
    rules_ful = "\n".join(
        f'[[rules]]\nwhen = {{ node = 75 }}\ndo = "l2_under_breath"\nuntil = 1\nlabel_en = "r{i}"\n'
        for i in range(210)
    )
    # node=75 has 5 seg0 points -> 210 * 5 = 1050? Wait format_seg0_ranges merges same seg0=*.
    # Use individual node/seg0: meta has 601 d0 points. Repeating rules across distinct segs or duplicating rules:
    # Let's build 1050 rules each matching 1 distinct seg via expr `dist` or repeating non-coalesced segs -> simpler: 180 rules hitting node=14 (Wait n14 has multiple seg0)
    # Check how many rule lines node=75 produces: 1 line (`seg0=*`). Use `fallback` off + repeat rule 1030 times -> 1030 `rule ...` lines!
    many_less = "\n".join(
        f'[[rules]]\nwhen = {{ node = 75, dist = "< 500" }}\ndo = "l2_under_breath"\nlabel_en = "s{i}"\n'
        for i in range(1030)
    )
    many_ful = "\n".join(
        f'[[rules]]\nwhen = {{ node = 75, dist = "< 500" }}\ndo = "l2_under_breath"\nuntil = 1\nlabel_en = "f{i}"\n'
        for i in range(1030)
    )
    for path_w, body in ((p_stateless, many_less), (p_stateful, many_ful)):
        with open(path_w, "w", encoding="utf-8") as f:
            f.write(f'meta = "{META_PATH.replace(os.sep, "/")}"\ngroups = "{GROUPS_PATH.replace(os.sep, "/")}"\n{body}\n')
    err_less, err_ful = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err_less):
        out_less = tmt.compile_preset_toml(p_stateless, META_PATH, GROUPS_PATH)
    assert "kMaxRuleCounters" not in err_less.getvalue(), err_less.getvalue()
    assert sum(1 for ln in out_less.splitlines() if ln.startswith("rule ")) == 1030
    with contextlib.redirect_stderr(err_ful):
        out_ful = tmt.compile_preset_toml(p_stateful, META_PATH, GROUPS_PATH)
    assert "kMaxRuleCounters=1024" in err_ful.getvalue(), f"Expected >1024 warning, got: {err_ful.getvalue()}"

    # Test prev_move resolution & parse_low_preset round-trip
    p_prev = os.path.join(td, "prev_test.preset.toml")
    with open(p_prev, "w", encoding="utf-8") as f:
        f.write(
            f'meta = "{META_PATH.replace(os.sep, "/")}"\n'
            f'groups = "{GROUPS_PATH.replace(os.sep, "/")}"\n\n'
            "[[rules]]\n"
            'when = { phase = ["p1", "p2"], stance = "bi", has = "l2_snake_fire_shoot" }\n'
            'prev_move = "l2_breath_single"\n'
            'do = "l2_snake_fire_shoot"\n'
            'label_en = "After SingleFB"\n'
        )
    err_prev = io.StringIO()
    with contextlib.redirect_stderr(err_prev):
        out_prev = tmt.compile_preset_toml(p_prev, META_PATH, GROUPS_PATH)
    assert err_prev.getvalue() == "", err_prev.getvalue()
    assert "prev=55:179" in out_prev, out_prev
    prev_txt = os.path.join(td, "prev_test.compiled.txt")
    with open(prev_txt, "w", encoding="utf-8") as f:
        f.write(out_prev)
    _, prev_rules = tmt.parse_low_preset(prev_txt)
    assert prev_rules and all(r.prev_moves == ((55, 179),) for r in prev_rules)

    # Test dead ban spec warning, silent-noop branch warning, and coverage breakdown/ban_noop
    p_diag = os.path.join(td, "diag_ban.preset.toml")
    with open(p_diag, "w", encoding="utf-8") as f:
        f.write(
            f'meta = "{META_PATH.replace(os.sep, "/")}"\n'
            f'groups = "{GROUPS_PATH.replace(os.sep, "/")}"\n\n'
            "[[rules]]\n"
            'when = { phase = ["p1", "p2"], stance = "bi" }\n'
            'ban = ["rush", "l2_switch_l4_body_press", "fly_zigzag_breath"]\n'
            'label_en = "Diag MultiBranch Ban"\n'
        )
    err_diag = io.StringIO()
    with contextlib.redirect_stderr(err_diag):
        tmt.compile_preset_toml(p_diag, META_PATH, GROUPS_PATH, explain=True)
    diag_warn = err_diag.getvalue()
    assert "spec(s) ['fly_zigzag_breath'] matched 0 candidates" in diag_warn, diag_warn
    assert "branch `phase=p1` acted on only" in diag_warn, diag_warn

    buf_diag = io.StringIO()
    with contextlib.redirect_stdout(buf_diag):
        rc_d = tmt.run_coverage(
            p_diag,
            meta_path=META_PATH,
            groups_path=GROUPS_PATH,
            when_str="phase=p1 stance=bi",
            limit=0,
            as_json=True,
        )
    assert rc_d == 0
    diag_cov = json.loads(buf_diag.getvalue())
    assert any("fly_zigzag_breath" in x for x in diag_cov["unused_action_specs"]), diag_cov["unused_action_specs"]
    assert diag_cov["ban_noop_uncovered_count"] > 0, diag_cov
    assert any(u.get("ban_noop_by") for u in diag_cov["uncovered"]), diag_cov["uncovered"][:3]
    p1_rows = diag_cov["group_breakdown"].get("p1", [])
    assert any(r["value"] == "bi" and r["ban_noop"] > 0 for r in p1_rows), p1_rows
print("[PASS] Bug #5: ALLOWED_RULE_KEYS, on_overflow guard, coverage all_banned_only_rules, >1024 stateful warning, prev_move, ban diagnostics")

# 5b. Context-aware prev tracking: prev={move,when}, prev_when, inline single-slot, ctx dedup, context-only, errors & reachability
with tempfile.TemporaryDirectory() as td:
    meta_q = META_PATH.replace(os.sep, "/")
    grp_q = GROUPS_PATH.replace(os.sep, "/")

    # (a) Multi-slot prev + ctx deduplication + compile/parse_low_preset/eval_low_rule_set round-trip
    p_ctx = os.path.join(td, "ctx_prev.preset.toml")
    with open(p_ctx, "w", encoding="utf-8") as f:
        f.write(
            f'meta = "{meta_q}"\n'
            f'groups = "{grp_q}"\n\n'
            "[[rules]]\n"
            'when = { stance = "bi", dist = ">= 900", expr = "has=l2_breath_single | has=l2_high_power_three_breath_multi_start" }\n'
            'prev = { move = "l2_tail_swing", when = { stance = "bi", dist = ">= 900" } }\n'
            'allow = ["l2_breath_single", "l2_high_power_three_breath_multi_start"]\n'
            'label_en = "Bi TailSwing Far -> 1FB/3FB"\n\n'
            "[[rules]]\n"
            'when = { node = 78, dist = ">= 2000" }\n'
            'prev = { move = "l2_tail_swing", when = { stance = "bi", dist = ">= 900" } }\n'
            'do = "l2_breath_single"\n'
            'label_en = "Dup ctx reuse"\n\n'
            "[[rules]]\n"
            'when = { p1 = "bi", dist = "< 500" }\n'
            'prev = { move = "l2_tail_swing", when = { node = 75, dist = "< 500", angle = "in 150..210" } }\n'
            'do = "l2_under_breath"\n'
            'label_en = "Single-slot inline prev"\n\n'
            "[[rules]]\n"
            'when = { p1 = "bi", dist = ">= 900", has = "l2_breath_single" }\n'
            'prev = { when = { p1 = "quad" } }\n'
            'do = "l2_breath_single"\n'
            'label_en = "Context-only prev"\n\n'
            "[[rules]]\n"
            'when = { p1 = "bi", dist = "< 500" }\n'
            'prev_move = "l2_tail_swing"\n'
            'prev_when = { node = 76, dist = "in 500..900", angle = "in 150..210" }\n'
            'do = "l2_line_fire_shoot"\n'
            'label_en = "prev_move + prev_when"\n'
        )
    err_c = io.StringIO()
    with contextlib.redirect_stderr(err_c):
        comp_txt = tmt.compile_preset_toml(p_ctx, META_PATH, GROUPS_PATH, explain=True)
    assert err_c.getvalue() == "", f"Unexpected stderr warnings: {err_c.getvalue()}"
    ctx_lines_emitted = [ln for ln in comp_txt.splitlines() if ln.startswith("ctx ")]
    assert any(ln.startswith("ctx prev_ctx_1 ") for ln in ctx_lines_emitted), comp_txt
    # Check deduplication: rule #1 and rule #2 share prev_ctx_1 (only one ctx name for bi_far_tail)
    assert comp_txt.count("prev_ctx=prev_ctx_1") >= 2, comp_txt
    assert "prev=55:175 prev_d=0 prev_f=0,4 prev_node=75 prev_seg0=36" in comp_txt, comp_txt
    assert "prev=55:175 prev_d=0 prev_f=0,4 prev_node=76 prev_seg0=36" in comp_txt, comp_txt
    # Context-only rule emits prev_ctx without prev=
    assert any(" prev_ctx=" in ln and " prev=" not in ln for ln in comp_txt.splitlines() if ln.startswith("rule ")), comp_txt

    ctx_compiled = os.path.join(td, "ctx_prev.compiled.txt")
    with open(ctx_compiled, "w", encoding="utf-8") as f:
        f.write(comp_txt)
    dis_c, rules_c = tmt.parse_low_preset(ctx_compiled)
    n77_s1 = next(p for p in meta.d0_points if p.node_idx == 77 and p.seg0 == 1)
    # Emulate vanilla cands on n77.s1
    rb_77 = [
        tmt.RandCandidate(c.s, c.weight, c.fl, c.tf, c.tn, 0) for c in n77_s1.cands
    ]
    res_match = tmt.eval_low_rule_set(
        rules_c, dis_c, 101, 0, 0, 77, 1, (), rb_77,
        prev_ctx={"move": (55, 175), "depth": 0, "file": 0, "node": 77, "seg0": 13, "callers": ()},
    )
    assert res_match[0], f"Expected multi-slot prev_ctx match, got {res_match}"
    res_wrong_node = tmt.eval_low_rule_set(
        rules_c, dis_c, 101, 0, 0, 77, 1, (), rb_77,
        prev_ctx={"move": (55, 175), "depth": 0, "file": 0, "node": 75, "seg0": 36, "callers": ()},
    )
    assert not res_wrong_node[0], f"Expected n75.s36 NOT to match far bi_tail prev_ctx, got {res_wrong_node}"

    # Flat-key `prev` syntax
    pr_flat = tmt.parse_preset_rule(
        {
            "when": {"p1": "bi", "dist": ">= 900"},
            "prev": {"move": "l2_tail_swing", "stance": "bi", "dist": "< 500"},
            "do": "l2_breath_single",
        },
        1, {}, {}, [2100], meta=meta, grp=grp,
    )
    assert pr_flat.prev_move_pairs == ((55, 175),) and "d=0" in pr_flat.prev_sel_terms and "dist<500" in pr_flat.prev_sel_terms

    # Error rejections
    expect_err(lambda: tmt.parse_preset_rule({"when": {"node": 75}, "prev": {}, "do": "l2_under_breath"}, 1, {}, {}, [2100], meta=meta, grp=grp), "cannot be empty")
    expect_err(lambda: tmt.parse_preset_rule({"when": {"node": 75}, "prev": "l2_tail_swing", "prev_move": "l2_tail_swing", "do": "l2_under_breath"}, 1, {}, {}, [2100], meta=meta, grp=grp), "cannot combine")
    expect_err(lambda: tmt.parse_preset_rule({"when": {"node": 75}, "prev": "l2_tail_swing", "prev_when": {"node": 75}, "do": "l2_under_breath"}, 1, {}, {}, [2100], meta=meta, grp=grp), "cannot combine")

    p_bad_zero = os.path.join(td, "bad_zero.preset.toml")
    with open(p_bad_zero, "w", encoding="utf-8") as f:
        f.write(f'meta = "{meta_q}"\ngroups = "{grp_q}"\n[[rules]]\nwhen = {{ node = 75 }}\nprev = {{ when = {{ node = 99999 }} }}\ndo = "l2_under_breath"\n')
    expect_err(lambda: tmt.compile_preset_toml(p_bad_zero, META_PATH, GROUPS_PATH), "matched 0 THK points")

    p_bad_nomove = os.path.join(td, "bad_nomove.preset.toml")
    with open(p_bad_nomove, "w", encoding="utf-8") as f:
        f.write(f'meta = "{meta_q}"\ngroups = "{grp_q}"\n[[rules]]\nwhen = {{ node = 77 }}\nprev = {{ move = "l2_tail_swing", when = {{ dist = ">= 2000" }} }}\ndo = "l2_breath_single"\n')
    expect_err(lambda: tmt.compile_preset_toml(p_bad_nomove, META_PATH, GROUPS_PATH), "not found in any")

    # Reachability warning when unconditional rule eliminates prev.move at all prev-context points
    p_unreach = os.path.join(td, "unreach.preset.toml")
    with open(p_unreach, "w", encoding="utf-8") as f:
        f.write(
            f'meta = "{meta_q}"\ngroups = "{grp_q}"\n'
            '[[rules]]\nwhen = { stance = "bi", dist = ">= 900", has = "l2_special_wide_range_fire_shoot" }\ndo = "l2_special_wide_range_fire_shoot"\n'
            '[[rules]]\nwhen = { stance = "bi", dist = ">= 900", has = "l2_breath_single" }\nprev = { move = "l2_tail_swing", when = { stance = "bi", dist = ">= 900" } }\ndo = "l2_breath_single"\n'
        )
    err_unr = io.StringIO()
    with contextlib.redirect_stderr(err_unr):
        tmt.compile_preset_toml(p_unreach, META_PATH, GROUPS_PATH)
    assert "cannot occur at any of its" in err_unr.getvalue(), err_unr.getvalue()
print("[PASS] Prev-context compiler, ctx deduplication, error guards & reachability warning verified")

# 5. Standalone low-level syntax/limit checks (+ optional raw THK bytecode replay)
def resolve_cand_in_raw(alts, cands):
    for sp in alts:
        if sp in ("VANILLA", "PASS", "SKIP", "-"):
            return -2
        if sp in ("ENDR", "RANDOM", "ELSER"):
            want = {"RANDOM": tmt.FL_RANDOM, "ENDR": tmt.FL_ENDR, "ELSER": tmt.FL_ELSER}[sp]
            for idx, c in enumerate(cands):
                if c.weight > 0 and (c.fl & tmt.CTRL_KIND_MASK) == want:
                    return idx
        elif sp.startswith("SEG:"):
            ws = int(sp[4:])
            for idx, c in enumerate(cands):
                if c.weight > 0 and c.s == ws:
                    return idx
        elif ":" in sp:
            tf, tn = (int(x) for x in sp.split(":", 1))
            for idx, c in enumerate(cands):
                if c.weight > 0 and c.tf == tf and c.tn == tn:
                    return idx
    return -1


with tempfile.TemporaryDirectory() as td_comp:
    p1_comp_path = os.path.join(td_comp, "fatalis_p1_nasty.compiled.txt")
    sw_comp_path = os.path.join(td_comp, "fatalis_sweet.compiled.txt")

    err_p1, err_sw = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err_p1):
        p1_comp_txt = tmt.compile_preset_toml(P1_PRESET_PATH, META_PATH, GROUPS_PATH, explain=True)
    assert err_p1.getvalue() == "", f"fatalis_p1_nasty compile warnings:\n{err_p1.getvalue()}"
    with contextlib.redirect_stderr(err_sw):
        sw_comp_txt = tmt.compile_preset_toml(SWEET_PRESET_PATH, META_PATH, GROUPS_PATH, explain=True)
    assert err_sw.getvalue() == "", f"fatalis_sweet compile warnings:\n{err_sw.getvalue()}"

    with open(SWEET_COMPILED_PATH, "r", encoding="utf-8") as sf:
        checked_sw = sf.read()
    assert sw_comp_txt == checked_sw, "Checked-in presets/fatalis_sweet.txt is out of sync with fatalis_sweet.preset.toml"
    print("[PASS] Clean compile (0 warnings) & fatalis_sweet.txt sync check passed")

    for p_w, txt_w in ((p1_comp_path, p1_comp_txt), (sw_comp_path, sw_comp_txt)):
        with open(p_w, "w", encoding="utf-8") as wf:
            wf.write(txt_w)

    compiled_targets = (
        ("fatalis_p1_nasty.compiled.txt", p1_comp_path),
        ("fatalis_sweet.txt", sw_comp_path),
    )

    parsed_presets = {}
    for label, fname in compiled_targets:
        disabled, compiled_rules = tmt.parse_low_preset(fname)
        overlong_meta = 0
        with open(fname, "r", encoding="utf-8") as rf:
            for ln in rf:
                for kw in ("tag=", 'label="', 'label_en="'):
                    if kw in ln:
                        val = ln.split(kw, 1)[1]
                        val = val.split('"', 1)[0] if kw.endswith('"') else val.split()[0]
                        if len(val.encode("utf-8")) > 63:
                            overlong_meta += 1
        assert overlong_meta == 0, f"{label}: overlong_meta={overlong_meta}"
        parsed_presets[label] = (disabled, compiled_rules)
        print(f"[PASS] Compiled preset lint `{label}`: rules={len(compiled_rules)}, overlong_meta=0")

    if MAIN_THK:
        thk0 = tmt.load_thk_file(MAIN_THK)
        if SUB_THK:
            _, sub_idx = tmt.infer_em_id_and_file_idx(SUB_THK, thk0.monster_id)
            sub_paths = {sub_idx: SUB_THK}
        else:
            em_id, main_idx = tmt.infer_em_id_and_file_idx(MAIN_THK, thk0.monster_id)
            sub_paths = tmt.discover_sibling_sub_thks(MAIN_THK, em_id, main_idx)

        sub_loaded = {sf: tmt.load_thk_file(spath) for sf, spath in sub_paths.items()}
        d0_blocks = {(nidx, rb.seg0): rb for nidx, nd in enumerate(thk0.nodes) for rb in tmt.parse_node_cfg(nd)[1]}
        d1_blocks = {}
        for sf, sthk in sub_loaded.items():
            for nidx, nd in enumerate(sthk.nodes):
                for rb in tmt.parse_node_cfg(nd)[1]:
                    d1_blocks[(sf, nidx, rb.seg0)] = rb

        for label, (disabled, compiled_rules) in parsed_presets.items():
            unresolved_steps = 0
            partial_drops = 0
            checks = 0
            for fidx in meta.default_files:
                for (nidx, seg0), rb in d0_blocks.items():
                    checks += 1
                    for r in compiled_rules:
                        if r.tags & disabled:
                            continue
                        if r.depth == 0 and fidx in r.files and nidx in r.nodes:
                            if (r.seg0_min < 0 or seg0 >= r.seg0_min) and (r.seg0_max < 0 or seg0 <= r.seg0_max):
                                for step in r.steps:
                                    hits = [resolve_cand_in_raw(alts, rb.cands) for alts, _ in step]
                                    if all(h == -1 for h in hits):
                                        unresolved_steps += 1
                                    elif any(h == -1 for h in hits):
                                        partial_drops += 1
                                break
            for pt in meta.d1_points:
                rb = d1_blocks[(pt.owner_tf, pt.node_idx, pt.seg0)]
                for cid in pt.callers:
                    checks += 1
                    for r in compiled_rules:
                        if r.tags & disabled:
                            continue
                        if r.depth == 1 and pt.owner_tf in r.files and pt.node_idx in r.nodes and cid in r.callers:
                            if (r.seg0_min < 0 or pt.seg0 >= r.seg0_min) and (r.seg0_max < 0 or pt.seg0 <= r.seg0_max):
                                for step in r.steps:
                                    hits = [resolve_cand_in_raw(alts, rb.cands) for alts, _ in step]
                                    if all(h == -1 for h in hits):
                                        unresolved_steps += 1
                                    elif any(h == -1 for h in hits):
                                        partial_drops += 1
                                break
            assert unresolved_steps == 0, f"{label}: unresolved_steps={unresolved_steps}"
            assert partial_drops == 0, f"{label}: partial_drops={partial_drops}"
            print(f"[PASS] Bytecode replay `{label}`: rules={len(compiled_rules)}, checks={checks}, unresolved=0, partial_drops=0")

        mismatches = tmt.verify_equivalence(P1_BASELINE_PATH, p1_comp_path, MAIN_THK, sub_loaded, meta.default_files)
        assert mismatches == 0, f"fatalis_p1_nasty equivalence verification failed: mismatches={mismatches}"
        print("[PASS] Equivalence verify `fatalis_p1_nasty`: mismatches=0")
    else:
        print("[SKIP] Bytecode replay & equivalence verify skipped (pass --main-thk <path/to/em013_00.thk> or set MHW_THK_MAIN to enable)")

print("[ALL THK META & PRESET TESTS PASSED]")
