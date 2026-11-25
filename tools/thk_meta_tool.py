#!/usr/bin/env python3
"""
MIRA Generic THK Meta Extractor, Standalone TOML Compiler & Verifier (v3.3 TOML)

100% monster-agnostic CLI tool supporting any MHW monster THK set (14 subcommands):
  Build / Compile / Verify:
    - `list-tables`    : Auto-discover all monster ActionTables inside an unpacked MHW PE dump
    - `export-actions` : Export a monster's ActionID -> MtDTI table to portable JSON
    - `extract`        : Extract `<em_id>.thkmeta.toml` (auto-discovers sibling sub-THKs, `--enum-ops`, `effects`)
    - `init-groups`    : Scaffold an editable `<em_id>.groups.toml` from `.thkmeta.toml`
    - `compile`        : Standalone compilation (`[[rules]]`, `ban`/`allow`, `fallback`, `!=` -> `.txt`)
    - `query`          : Static slot / Move->[nodes] query from TOML meta + groups
    - `verify`         : Exhaustive machine-level equivalence check between two low-level `.txt` presets
  Read-Only Agent Query Tools (`--json` & `--limit` pagination):
    - `overview`       : Compact monster cold-start summary (`em_*`, `var_*`, predicates, distance buckets)
    - `moves`          : Fuzzy search & `--detail` variant inspector across `[moves]` / `[subnodes]`
    - `inspect`        : Full candidate weights, `effects`, path `states`, and topology for points/selectors
    - `switches`       : Enumerate `RANDOM`/`ENDR`/`ELSER` probability switches with side `effects`
    - `followups`      : List `d=1` derivative trees grouped by `owner` / `entry_owners`
    - `diff`           : Minimal semantic/topology diff between two points or same-name move variants
    - `coverage`       : Static preset coverage, shadowing, `all_banned_only_rules`, and uncovered-gap audit

Runtime compilation (`compile`), scaffolding (`init-groups`), and all read-only query subcommands read ONLY
`.toml` files via stdlib `tomllib` (ZERO `.thk` or PE executable dependency).
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import math
import os
import re
import struct
import sys
import tomllib
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

FL_END = 0x1
FL_RANDOM = 0x40
FL_ENDR = 0x80
FL_ELSER = 0xC0
FL_RETURN = 0x800
FL_EXIT = 0x8000
FL_END_RANDOM = 0x10000
FL_IF = 0x20000
FL_ELIF_ELSE = 0x40000
FL_ENDIF = 0x80000
FL_STOP_THINK = 0x100000
FL_TERMINATOR_MASK = FL_RETURN | FL_EXIT | FL_STOP_THINK

CTRL_KIND_MASK = 0xC0
MAX_FILTER_ITEMS = 8    # kMaxFilterItems in thk_engine.h
MAX_STEP_CHOICES = 8    # kMaxStepChoices in thk_engine.h
MAX_RULE_STEPS = 16     # kMaxRuleSteps in thk_engine.h
MAX_RULE_COUNTERS = 1024  # kMaxRuleCounters in thk_engine.h

CMP_MAP = {0: "==", 1: ">=", 2: ">", 3: "<=", 4: "<", 5: "!="}
NEG_CMP = {"==": "!=", "!=": "==", ">=": "<", "<": ">=", ">": "<=", "<=": ">"}

# Pure state-mutating / flow-control ops (never predicate guards on edges)
SETTER_OPS: Set[int] = set(range(0x80, 0x94)) | {0x03, 0x04, 0x05, 0x5E, 0x73, 0x74}
COMPARATOR_OPS: Set[int] = {0x36, 0x38, 0x39, 0xA9, 0xAB, 0xBC, 0xCE}


def is_movement_action(name: str) -> bool:
    """Generic MHW action name filter distinguishing locomotion/turn/idle from combat actions."""
    return bool(re.search(r"(?:^|[a-z0-9_])(Walk|Turn|Search|Idle|Hover|Wait|Step)(?:[0-9]*)$", name))


def to_snake(name: str) -> str:
    """Convert PascalCase/CamelCase MHW DTI name to clean snake_case without splitting tokens like L2/L4."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"([a-z]{2,})(\d+)$", r"\1_\2", s)
    return s.lower()


def infer_em_id_and_file_idx(thk_path: str, monster_id: int) -> Tuple[str, int]:
    """Infer `(em_id, file_idx)` from a THK filename such as `emXXX_00.thk`."""
    stem = os.path.splitext(os.path.basename(thk_path))[0]
    m = re.match(r"^(ems?\d+(?:_\d+)?)_(\d+)$", stem, re.IGNORECASE)
    if m:
        return m.group(1).lower(), int(m.group(2))
    m2 = re.match(r"^(.*?)_(\d+)$", stem)
    if m2:
        return m2.group(1).lower(), int(m2.group(2))
    return f"mon{monster_id}", 0


def discover_sibling_sub_thks(main_path: str, em_id: str, main_idx: int) -> Dict[int, str]:
    """Discover sibling sub-THK files `<em_id>_<NN>.thk` alongside the main THK."""
    parent = os.path.dirname(os.path.abspath(main_path))
    if not os.path.isdir(parent):
        return {}
    pat = re.compile(rf"^{re.escape(em_id)}_(\d+)\.thk$", re.IGNORECASE)
    subs: Dict[int, str] = {}
    for fn in sorted(os.listdir(parent)):
        m = pat.match(fn)
        if m:
            fidx = int(m.group(1))
            if fidx != main_idx:
                subs[fidx] = os.path.join(parent, fn)
    return subs


class AmbiguousMoveError(ValueError):
    """Raised when Move -> [targets] matches >= 2 positive-weight candidates in the same RANDOM block."""


# =============================================================================
# THK Binary & Generic ActionTable Loaders (`extract` / `list-tables` / `verify`)
# =============================================================================


@dataclasses.dataclass(frozen=True)
class ThkSeg:
    s: int
    fl: int
    op: int
    p1: int
    p2: int
    endv: int
    tf: int
    tn: int
    lj: int
    act: int
    end_params: Tuple[Tuple[int, int], ...] = ()


@dataclasses.dataclass
class ThkNode:
    idx: int
    nid: int
    scnt: int
    segs: List[ThkSeg]


@dataclasses.dataclass
class ThkFile:
    path: str
    file_idx: int
    ver: int
    monster_id: int
    nodes: List[ThkNode]
    nid_to_idx: Dict[int, int]


def load_thk_file(path: str, file_idx: Optional[int] = None) -> ThkFile:
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, ncnt = struct.unpack_from("<4sHH", data, 0)
    if magic != b"THK\x00":
        raise ValueError(f"Bad THK magic in {path}: {magic!r}")
    _hash, mid, toff = struct.unpack_from("<I4xI4xQ", data, 8)
    if file_idx is None:
        _, file_idx = infer_em_id_and_file_idx(path, mid)
    nodes: List[ThkNode] = []
    nid_to_idx: Dict[int, int] = {}
    for idx in range(ncnt):
        doff, scnt, nid = struct.unpack_from("<QII", data, toff + idx * 16)
        segs: List[ThkSeg] = []
        for s in range(scnt):
            so = doff + s * 0x80
            raw = data[so : so + 0x80]
            if len(raw) < 0x80:
                raw = raw + b"\x00" * (0x80 - len(raw))
            fl = struct.unpack_from("<I", raw, 0x00)[0]
            op = struct.unpack_from("<I", raw, 0x08)[0]
            p1 = struct.unpack_from("<i", raw, 0x0C)[0]
            p2 = struct.unpack_from("<i", raw, 0x20)[0]
            endv = struct.unpack_from("<i", raw, 0x24)[0]
            tf, tn, lj = struct.unpack_from("<iii", raw, 0x28)
            act = struct.unpack_from("<I", raw, 0x4C)[0]
            end_types = struct.unpack_from("<4i", raw, 0x50)
            end_vals = struct.unpack_from("<4i", raw, 0x60)
            end_params = tuple((t, v) for t, v in zip(end_types, end_vals) if t != 0 or v != 0)
            segs.append(
                ThkSeg(
                    s=s,
                    fl=fl,
                    op=op,
                    p1=p1,
                    p2=p2,
                    endv=endv,
                    tf=tf,
                    tn=tn,
                    lj=lj,
                    act=act,
                    end_params=end_params,
                )
            )
        nodes.append(ThkNode(idx=idx, nid=nid, scnt=scnt, segs=segs))
        if nid != 0:
            nid_to_idx[nid] = idx
    return ThkFile(path=path, file_idx=file_idx, ver=ver, monster_id=mid, nodes=nodes, nid_to_idx=nid_to_idx)


def _open_pe_reader(dump_path: str):
    with open(dump_path, "rb") as f:
        buf = f.read()
    e_lfanew = struct.unpack_from("<I", buf, 0x3C)[0]
    _sig, _mach, nsec = struct.unpack_from("<4sHH", buf, e_lfanew)
    opt_sz = struct.unpack_from("<H", buf, e_lfanew + 20)[0]
    img_base = struct.unpack_from("<Q", buf, e_lfanew + 24 + 24)[0]
    secs: List[Tuple[str, int, int, int, int]] = []
    so = e_lfanew + 24 + opt_sz
    for i in range(nsec):
        nm_b, vsz, vaddr, rsz, rptr = struct.unpack_from("<8sIIII", buf, so + i * 40)
        secs.append((nm_b.rstrip(b"\x00").decode("latin1"), vaddr, vsz, rptr, rsz))

    def va_to_off(va: int) -> Optional[int]:
        rva = va - img_base
        for _nm, vaddr, vsz, rptr, rsz in secs:
            if vaddr <= rva < vaddr + max(vsz, rsz):
                off = rptr + (rva - vaddr)
                if off < len(buf):
                    return off
        return None

    def read_cstr(va: int) -> str:
        off = va_to_off(va)
        if off is None:
            return ""
        end = buf.find(b"\x00", off, off + 160)
        return buf[off:end].decode("utf-8", errors="replace") if end > off else ""

    return buf, img_base, secs, va_to_off, read_cstr


def scan_all_action_tables_from_dump(dump_path: str) -> List[Dict[str, Any]]:
    """Auto-discover all MHW ActionTables inside an unpacked PE dump."""
    buf, img_base, secs, va_to_off, read_cstr = _open_pe_reader(dump_path)
    max_va = img_base + 0x08000000
    tables: List[Dict[str, Any]] = []

    for sname, vaddr, _vsz, rptr, rsz in secs:
        if sname not in (".rdata", ".data"):
            continue
        end = rptr + rsz - 64
        off = rptr
        while off < end:
            a0, p0, d0 = struct.unpack_from("<IIQ", buf, off)
            if a0 == 0 and p0 == 0 and d0 == 0:
                a1, p1, d1 = struct.unpack_from("<IIQ", buf, off + 32)
                if a1 == 1 and p1 == 0 and img_base <= d1 < max_va:
                    doff = va_to_off(d1 + 8)
                    if doff is not None:
                        sva = struct.unpack_from("<Q", buf, doff)[0]
                        if img_base <= sva < max_va and read_cstr(sva).startswith("nAct"):
                            cnt = 0
                            prefixes: Dict[str, int] = defaultdict(int)
                            actions: Dict[int, str] = {}
                            cur = off
                            while cur + 32 <= rptr + rsz:
                                ai, pi, di = struct.unpack_from("<IIQ", buf, cur)
                                if ai == 0xFFFFFFFF or ai != cnt or pi != 0:
                                    break
                                sname_dti = ""
                                if di and img_base <= di < max_va:
                                    do2 = va_to_off(di + 8)
                                    if do2 is not None:
                                        sv2 = struct.unpack_from("<Q", buf, do2)[0]
                                        if img_base <= sv2 < max_va:
                                            sname_dti = read_cstr(sv2)
                                if "::" in sname_dti:
                                    pf, short_nm = sname_dti.split("::", 1)
                                    prefixes[pf] += 1
                                    actions[ai] = short_nm
                                else:
                                    actions[ai] = sname_dti
                                cnt += 1
                                cur += 32
                            if cnt >= 10:
                                tables.append(
                                    {
                                        "va": img_base + vaddr + (off - rptr),
                                        "count": cnt,
                                        "prefixes": dict(sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0]))),
                                        "actions": actions,
                                    }
                                )
                                off = cur
                                continue
            off += 8
    return tables


def discover_action_table_from_dump(
    dump_path: str,
    em_id: str,
    explicit_va: Optional[int] = None,
) -> Tuple[Dict[int, str], Dict[str, Any]]:
    """Find the best matching ActionTable in `mhw_dump.bytes` for any `emXXX` / `emsXXX` ID."""
    tables = scan_all_action_tables_from_dump(dump_path)
    if explicit_va is not None:
        for tbl in tables:
            if tbl["va"] == explicit_va:
                return tbl["actions"], tbl
        raise ValueError(
            f"No ActionTable at VA=0x{explicit_va:x} in {dump_path}. "
            f"Run `list-tables --dump ...` to inspect available VAs."
        )

    want = f"nact{em_id.lower()}"
    candidates: List[Tuple[Tuple[int, int, int, int], Dict[str, Any]]] = []
    for tbl in tables:
        plower = {k.lower(): v for k, v in tbl["prefixes"].items()}
        if want not in plower:
            continue
        has_child = any(k != want and k.startswith(want + "_") for k in plower)
        score = (1 if not has_child else 0, plower[want], -len(plower), tbl["count"])
        candidates.append((score, tbl))

    if not candidates:
        raise ValueError(
            f"No ActionTable found for '{em_id}' (prefix '{want}') in {dump_path}. "
            f"Run `list-tables --dump ...` to inspect available prefixes."
        )
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]
    if len(candidates) >= 2:
        alts = "; ".join(
            f"VA=0x{t['va']:x}(count={t['count']}, prefixes={list(t['prefixes'].items())[:3]})"
            for _, t in candidates[1:5]
        )
        print(
            f"[WARNING] Multiple ActionTable candidates matched '{em_id}' ({want}): "
            f"selected VA=0x{best['va']:x} (count={best['count']}); alternatives: {alts}. "
            f"Pass --va 0x{best['va']:x} (or another candidate) to pin explicitly.",
            file=sys.stderr,
        )
    return best["actions"], best


def load_actions_json(path: str) -> Dict[int, str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    src = raw.get("actions", raw) if isinstance(raw, dict) and "actions" in raw else raw
    out: Dict[int, str] = {}
    for k, v in src.items():
        s = str(v)
        if "::" in s:
            s = s.split("::", 1)[1]
        out[int(k)] = s
    return out


# =============================================================================
# Circular 360-degree Angle Helpers
# =============================================================================

AngleIntervals = Tuple[Tuple[int, int], ...]


def angle_from_bounds(a: int, b: int) -> AngleIntervals:
    a = a % 360
    b = b % 360
    if a == b:
        return ((0, 360),)
    if a < b:
        return ((a, b),)
    return ((0, b), (a, 360))


def angle_complement(ivs: AngleIntervals) -> AngleIntervals:
    out: List[Tuple[int, int]] = []
    cur = 0
    for s, e in ivs:
        if cur < s:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < 360:
        out.append((cur, 360))
    return tuple(out)


def angle_intersect(a: AngleIntervals, b: AngleIntervals) -> AngleIntervals:
    out: List[Tuple[int, int]] = []
    i, j = 0, 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return tuple(out)


def angle_union(a: AngleIntervals, b: AngleIntervals) -> AngleIntervals:
    pts = sorted(a + b)
    out: List[Tuple[int, int]] = []
    for s, e in pts:
        if s >= e:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return tuple(out)


def angle_subset(sub: AngleIntervals, sup: AngleIntervals) -> bool:
    return angle_intersect(sub, angle_complement(sup)) == ()


def format_angle_intervals(ivs: AngleIntervals) -> str:
    if ivs == ((0, 360),):
        return "*"
    if not ivs:
        return "EMPTY"
    if len(ivs) >= 2 and ivs[0][0] == 0 and ivs[-1][1] == 360:
        wrapped = (ivs[-1][0], ivs[0][1])
        rest = list(ivs[1:-1])
        parts = [f"{wrapped[0]}..{wrapped[1]}"] + [f"{s}..{e}" for s, e in rest]
        return "|".join(parts)
    return "|".join(f"{s}..{e}" for s, e in ivs)


def parse_angle_spec(spec: str) -> AngleIntervals:
    acc: AngleIntervals = ()
    for part in spec.split("|"):
        part = part.strip()
        if not part:
            continue
        a_s, b_s = part.split("..", 1)
        acc = angle_union(acc, angle_from_bounds(int(a_s.strip()), int(b_s.strip())))
    return acc


# =============================================================================
# Condition / Predicate Simplification (Complete Global::AI_Interpreter Map)
# =============================================================================

RawCond = Tuple[str, Tuple[int, int, int]]
INF = 10**9


@dataclasses.dataclass
class ParamStats:
    em_param_vals: Dict[int, Set[Tuple[int, int]]]
    op_param_vals: Dict[int, Set[Tuple[int, int]]]


def collect_param_stats(thks: Sequence[ThkFile]) -> ParamStats:
    em_pv: Dict[int, Set[Tuple[int, int]]] = defaultdict(set)
    op_pv: Dict[int, Set[Tuple[int, int]]] = defaultdict(set)
    for thk in thks:
        for nd in thk.nodes:
            for sg in nd.segs:
                if sg.op >= 0x101:
                    em_pv[sg.op].add((sg.p1, sg.p2))
                elif sg.op not in (0, 2):
                    op_pv[sg.op].add((sg.p1, sg.p2))
    return ParamStats(em_param_vals=em_pv, op_param_vals=op_pv)


def format_atomic_pred(pol: str, op: int, p1: int, p2: int, stats: ParamStats) -> str:
    neg = pol == "NOT"
    prefix = "!" if neg else ""
    if op == 0x06:
        return f"dist3d>={p1}" if neg else f"dist3d<{p1}"
    if op == 0x07:
        return f"dist3d<{p1}" if neg else f"dist3d>={p1}"
    if op == 0x08:
        return f"dist>={p1}" if neg else f"dist<{p1}"
    if op == 0x09:
        return f"dist<{p1}" if neg else f"dist>={p1}"
    if op == 0x0A:
        return f"home_dist>{p1}" if neg else f"home_dist<={p1}"
    if op == 0x0B:
        return f"home_dist<={p1}" if neg else f"home_dist>{p1}"
    if op == 0x0C:
        return f"target_home_dist>{p1}" if neg else f"target_home_dist<={p1}"
    if op == 0x0D:
        return f"target_home_dist<={p1}" if neg else f"target_home_dist>{p1}"
    if op == 0x0E:
        return f"abs_dy>{p1}" if neg else f"abs_dy<={p1}"
    if op == 0x0F:
        return f"abs_dy<={p1}" if neg else f"abs_dy>{p1}"
    if op == 0x10:
        return ("dy>=0" if neg else "dy<0") if p1 == 1 else ("dy<0" if neg else "dy>=0")
    if op in (0x14, 0x16):
        ivs = angle_from_bounds(p1, p2)
        if neg:
            ivs = angle_complement(ivs)
        return f"angle={format_angle_intervals(ivs)}"
    if op in (0x15, 0x17):
        ivs = angle_complement(angle_from_bounds(p1, p2))
        if neg:
            ivs = angle_complement(ivs)
        return f"angle={format_angle_intervals(ivs)}"
    if op == 0x18:
        return f"{prefix}target_angle={p1}..{p2}"
    if op == 0x19:
        return f"target_angle={p1}..{p2}" if neg else f"!target_angle={p1}..{p2}"
    if op == 0x1A:
        return f"{prefix}pitch_angle={p1}..{p2}"
    if op == 0x1B:
        return f"pitch_angle={p1}..{p2}" if neg else f"!pitch_angle={p1}..{p2}"
    if op == 0x1C:
        return f"{prefix}in_combat"
    if op == 0x1E:
        return f"{prefix}enraged"
    if op == 0x1F:
        return f"{prefix}fatigued"
    if op == 0x20:
        return f"{prefix}status_poison"
    if op == 0x21:
        return f"{prefix}status_19"
    if op == 0x22:
        return f"{prefix}status_17"
    if op == 0x2B:
        return f"op_2b({p1})>{p2}" if neg else f"op_2b({p1})<={p2}"
    if op == 0x2C:
        if p1 == 2:
            return f"hp_pct>{p2}" if neg else f"hp_pct<={p2}"
        return f"{prefix}hp_pct({p1},{p2})"
    if op == 0x2D:
        return f"target_type!={p1}" if neg else f"target_type={p1}"
    if op == 0x2E:
        return f"target_state!={p1}" if neg else f"target_state={p1}"
    if op == 0x2F:
        return f"{prefix}target_dead"
    if op == 0x37:
        sym = CMP_MAP.get(p1, f"?{p1}")
        if neg:
            sym = NEG_CMP.get(sym, sym)
        return f"quest_id{sym}{p2}"
    if op == 0x70:
        return f"{prefix}flying"
    if op == 0x75:
        return f"{prefix}part_damaged({p1},{p2})"
    if op == 0x76:
        return f"{prefix}recent_act({p1},{p2})"
    if op == 0x7F:
        if p2:
            return f"{prefix}part_clutched({p1},{p2})"
        return f"part_clutched!={p1}" if neg else f"part_clutched={p1}"
    if 0x80 <= op <= 0x93:
        var_idx = op - 0x80
        return f"var_{var_idx}={p1}"
    if 0x94 <= op <= 0xA7:
        var_idx = op - 0x94
        sym = CMP_MAP.get(p1, f"?{p1}")
        if neg:
            sym = NEG_CMP.get(sym, sym)
        return f"var_{var_idx}{sym}{p2}"
    if op == 0xB0:
        return f"parent_sub_id!={p1}" if neg else f"parent_sub_id={p1}"
    if op == 0xB2:
        return f"status_active!={p1}" if neg else f"status_active={p1}"
    if op in COMPARATOR_OPS:
        sym = CMP_MAP.get(p1, f"?{p1}")
        if neg:
            sym = NEG_CMP.get(sym, sym)
        return f"op_{op:x}{sym}{p2}"
    if (0xD2 <= op <= 0xFA) or op in (0xFC, 0xFD):
        if p1 == 0 and p2 == 0:
            return f"{prefix}clutch_mount_{op:x}"
        if p2 == 0:
            return f"clutch_mount_{op:x}!={p1}" if neg else f"clutch_mount_{op:x}={p1}"
        return f"{prefix}clutch_mount_{op:x}({p1},{p2})"
    if op == 0x100:
        return f"{prefix}current_action_flag"
    if op >= 0x101:
        pvs = stats.em_param_vals.get(op, set())
        has_nonzero_p2 = any(x[1] != 0 for x in pvs)
        has_nonzero_p1 = any(x[0] != 0 for x in pvs)
        if not has_nonzero_p1 and not has_nonzero_p2:
            return f"{prefix}em_{op:x}"
        if not has_nonzero_p2:
            return f"em_{op:x}!={p1}" if neg else f"em_{op:x}={p1}"
        return f"{prefix}em_{op:x}({p1},{p2})"
    pvs = stats.op_param_vals.get(op, set())
    if all(x == (0, 0) for x in pvs):
        return f"{prefix}op_{op:x}"
    if all(x[1] == 0 for x in pvs):
        return f"op_{op:x}!={p1}" if neg else f"op_{op:x}={p1}"
    return f"{prefix}op_{op:x}({p1},{p2})"


@dataclasses.dataclass(frozen=True)
class PathState:
    dist_lo: int
    dist_hi: Optional[int]
    dist3d_lo: int
    dist3d_hi: Optional[int]
    angles: AngleIntervals
    other_preds: Tuple[str, ...]


def simplify_conditions(
    raw_conds: Sequence[RawCond],
    stats: ParamStats,
    enum_ops: Optional[Set[int]] = None,
) -> Optional[PathState]:
    pos_raw: Set[Tuple[int, int, int]] = set()
    neg_raw: Set[Tuple[int, int, int]] = set()
    d_lo, d_hi = 0, INF
    d3_lo, d3_hi = 0, INF
    angles: AngleIntervals = ((0, 360),)
    var_bounds: Dict[str, Tuple[int, int]] = {}
    var_neq: Dict[str, Set[int]] = defaultdict(set)
    excl_lo_hi: Dict[str, List[int]] = {}
    other: Set[str] = set()
    enum_eq: Dict[int, Set[int]] = defaultdict(set)
    enum_neq: Dict[int, Set[int]] = defaultdict(set)
    active_enums = enum_ops or set()

    def _tighten_gt_le(metric: str, is_le: bool, val: int) -> bool:
        lo_ex, hi_in = excl_lo_hi.get(metric, [-INF, INF])
        if is_le:
            hi_in = min(hi_in, val)
        else:
            lo_ex = max(lo_ex, val)
        if lo_ex >= hi_in:
            return False
        excl_lo_hi[metric] = [lo_ex, hi_in]
        return True

    for pol, (op, p1, p2) in raw_conds:
        key = (op, p1, p2)
        if pol == "POS":
            if key in neg_raw:
                return None
            pos_raw.add(key)
        else:
            if key in pos_raw:
                return None
            neg_raw.add(key)

        if op == 0x08:
            if pol == "POS":
                d_hi = min(d_hi, p1)
            else:
                d_lo = max(d_lo, p1)
        elif op == 0x09:
            if pol == "POS":
                d_lo = max(d_lo, p1)
            else:
                d_hi = min(d_hi, p1)
        elif op == 0x06:
            if pol == "POS":
                d3_hi = min(d3_hi, p1)
            else:
                d3_lo = max(d3_lo, p1)
        elif op == 0x07:
            if pol == "POS":
                d3_lo = max(d3_lo, p1)
            else:
                d3_hi = min(d3_hi, p1)
        elif op in (0x14, 0x16):
            seg_iv = angle_from_bounds(p1, p2)
            if pol == "NOT":
                seg_iv = angle_complement(seg_iv)
            angles = angle_intersect(angles, seg_iv)
            if not angles:
                return None
        elif op in (0x15, 0x17):
            seg_iv = angle_complement(angle_from_bounds(p1, p2))
            if pol == "NOT":
                seg_iv = angle_complement(seg_iv)
            angles = angle_intersect(angles, seg_iv)
            if not angles:
                return None
        elif (0x94 <= op <= 0xA7) or op == 0x37:
            vname = "quest_id" if op == 0x37 else f"var_{op - 0x94}"
            sym = CMP_MAP.get(p1, "?")
            if pol == "NOT":
                sym = NEG_CMP.get(sym, sym)
            lo, hi = var_bounds.get(vname, (-INF, INF))
            if sym == "==":
                lo, hi = max(lo, p2), min(hi, p2)
            elif sym == "!=":
                var_neq[vname].add(p2)
            elif sym == ">=":
                lo = max(lo, p2)
            elif sym == ">":
                lo = max(lo, p2 + 1)
            elif sym == "<=":
                hi = min(hi, p2)
            elif sym == "<":
                hi = min(hi, p2 - 1)
            if lo > hi:
                return None
            var_bounds[vname] = (lo, hi)
        else:
            if op in (0x0A, 0x0B):
                is_le = (pol == "POS") if op == 0x0A else (pol == "NOT")
                if not _tighten_gt_le("home_dist", is_le, p1):
                    return None
            elif op in (0x0C, 0x0D):
                is_le = (pol == "POS") if op == 0x0C else (pol == "NOT")
                if not _tighten_gt_le("target_home_dist", is_le, p1):
                    return None
            elif op in (0x0E, 0x0F):
                is_le = (pol == "POS") if op == 0x0E else (pol == "NOT")
                if not _tighten_gt_le("abs_dy", is_le, p1):
                    return None
            elif op == 0x2C and p1 == 2:
                if not _tighten_gt_le("hp_pct", pol == "POS", p2):
                    return None
            if op in active_enums:
                if pol == "POS":
                    enum_eq[op].add(p1)
                else:
                    enum_neq[op].add(p1)
            other.add(format_atomic_pred(pol, op, p1, p2, stats))

    if d_lo >= d_hi or d3_lo >= d3_hi:
        return None
    for op, vs in enum_eq.items():
        if len(vs) > 1 or (vs & enum_neq[op]):
            return None

    for vname, (lo, hi) in var_bounds.items():
        for bad in var_neq.get(vname, ()):
            if lo == hi == bad:
                return None
        if lo == hi:
            other.add(f"{vname}={lo}")
        elif lo > -INF and hi < INF:
            other.add(f"{vname}={lo}..{hi}")
        elif lo > -INF:
            other.add(f"{vname}>={lo}")
        elif hi < INF:
            other.add(f"{vname}<={hi}")
    for vname, bads in var_neq.items():
        lo, hi = var_bounds.get(vname, (-INF, INF))
        for bad in sorted(bads):
            if lo <= bad <= hi and not (lo == hi == bad):
                other.add(f"{vname}!={bad}")

    return PathState(
        dist_lo=d_lo,
        dist_hi=None if d_hi >= INF else d_hi,
        dist3d_lo=d3_lo,
        dist3d_hi=None if d3_hi >= INF else d3_hi,
        angles=angles,
        other_preds=tuple(sorted(other)),
    )


def merge_adjacent_dist_states(states: List[PathState]) -> List[PathState]:
    by_rest: Dict[Tuple, List[Tuple[int, int]]] = defaultdict(list)
    for st in states:
        key = (st.dist3d_lo, st.dist3d_hi, st.angles, st.other_preds)
        by_rest[key].append((st.dist_lo, INF if st.dist_hi is None else st.dist_hi))

    merged: List[PathState] = []
    for (d3_lo, d3_hi, angles, other_preds), ivs in by_rest.items():
        ivs_sorted = sorted(ivs)
        cur_lo, cur_hi = ivs_sorted[0]
        for lo, hi in ivs_sorted[1:]:
            if lo <= cur_hi:
                cur_hi = max(cur_hi, hi)
            else:
                merged.append(PathState(cur_lo, None if cur_hi >= INF else cur_hi, d3_lo, d3_hi, angles, other_preds))
                cur_lo, cur_hi = lo, hi
        merged.append(PathState(cur_lo, None if cur_hi >= INF else cur_hi, d3_lo, d3_hi, angles, other_preds))
    return merged


# =============================================================================
# CFG Parsing, Subnode Profiling & Extraction (`.thk` -> `.thkmeta.toml`)
# =============================================================================


@dataclasses.dataclass
class RandCandidate:
    s: int
    weight: float
    fl: int
    tf: int
    tn: int
    lj: int
    act: int = 0
    body_tfs: Tuple[Tuple[int, int], ...] = ()
    body_acts: Tuple[int, ...] = ()
    body_ljs: Tuple[int, ...] = ()
    body_writes: Tuple[str, ...] = ()
    raw_key: str = ""


@dataclasses.dataclass
class RandBlock:
    node_idx: int
    seg0: int
    cands: List[RandCandidate]
    local_conds: Tuple[RawCond, ...]


@dataclasses.dataclass
class SubnodeProfile:
    tf: int
    tn: int
    idx: int
    direct_actions: Tuple[str, ...]
    direct_combat: Tuple[str, ...]
    all_actions: Tuple[str, ...]
    orig_primary: str
    snake_primary: str
    chains: Tuple[str, ...]
    internal_checks: Tuple[str, ...]
    var_writes: Tuple[str, ...]
    end_params: Tuple[Tuple[int, int], ...]
    has_rand: bool


@dataclasses.dataclass
class MetaPoint:
    depth: int
    file_label: str
    node_idx: int
    node_nid: int
    seg0: int
    cands: List[RandCandidate]
    parents: Tuple[int, ...]
    vias: Tuple[Tuple[int, ...], ...]
    states: List[PathState]
    owner_tf: int = -1
    owner_tn: int = -1
    callers: Tuple[int, ...] = ()
    entry_owners: Tuple[str, ...] = ()
    effects: Dict[str, Tuple[str, ...]] = dataclasses.field(default_factory=dict)


def parse_node_cfg(nd: ThkNode) -> Tuple[List[Tuple[int, int, Tuple[RawCond, ...]]], List[RandBlock]]:
    # Stack frame: [negated_prev_conds_list, current_cond_or_None, branch_terminated_bool]
    stack: List[List[Any]] = []
    top_terminated = False
    in_rand_block = False
    rand_if_depth = 0
    cand_terminated = False
    edges: List[Tuple[int, int, Tuple[RawCond, ...]]] = []
    rands: List[RandBlock] = []
    cur_rand_seg0: Optional[int] = None
    cur_rand_cands: List[RandCandidate] = []
    # Per-candidate body accumulators: [tfs_list, acts_list, ljs_list, writes_list]
    cur_rand_bodies: List[List[List[Any]]] = []
    cur_rand_conds: Tuple[RawCond, ...] = ()

    def flush_rand() -> None:
        nonlocal cur_rand_seg0, cur_rand_cands, cur_rand_bodies
        if cur_rand_seg0 is None:
            return
        for cand, (btfs, bacts, bljs, bwrites) in zip(cur_rand_cands, cur_rand_bodies):
            cand.body_tfs = tuple(dict.fromkeys(btfs))
            cand.body_acts = tuple(dict.fromkeys(bacts))
            cand.body_ljs = tuple(dict.fromkeys(bljs))
            cand.body_writes = tuple(dict.fromkeys(bwrites))
        rands.append(RandBlock(nd.idx, cur_rand_seg0, cur_rand_cands, cur_rand_conds))
        cur_rand_seg0 = None
        cur_rand_cands = []
        cur_rand_bodies = []

    def active() -> Tuple[RawCond, ...]:
        out: List[RawCond] = []
        for prev_neg, c, _term in stack:
            for x in prev_neg:
                out.append(("NOT", x))
            if c is not None:
                out.append(("POS", c))
        return tuple(out)

    def is_dead() -> bool:
        return top_terminated or cand_terminated or any(frame[2] for frame in stack)

    for sg in nd.segs:
        fl, op = sg.fl, sg.op
        cobj = (op, sg.p1, sg.p2) if op not in (0, 2) and op not in SETTER_OPS else None

        if fl & FL_END_RANDOM:
            flush_rand()
            in_rand_block = False
            cand_terminated = False
        elif fl & FL_IF:
            stack.append([[], cobj, False])
            if not is_dead() and sg.lj >= 0:
                edges.append((sg.lj, sg.s, active()))
        elif fl & FL_ELIF_ELSE:
            if stack:
                pn, pc, _term = stack.pop()
                nn = list(pn) + ([pc] if pc is not None else [])
                stack.append([nn, cobj, False])
            if not is_dead() and sg.lj >= 0:
                edges.append((sg.lj, sg.s, active()))
        elif fl & FL_ENDIF:
            if stack:
                stack.pop()
            if in_rand_block and len(stack) < rand_if_depth:
                flush_rand()
                in_rand_block = False
                cand_terminated = False
            if not is_dead() and sg.lj >= 0:
                edges.append((sg.lj, sg.s, active()))
        else:
            ctrl = fl & CTRL_KIND_MASK
            if ctrl in (FL_RANDOM, FL_ELSER, FL_ENDR):
                # Sibling candidate header starts a fresh branch inside the active RANDOM block
                cand_terminated = False
                if ctrl == FL_RANDOM:
                    flush_rand()
                    in_rand_block = True
                    rand_if_depth = len(stack)
            if not is_dead():
                if ctrl == FL_RANDOM:
                    cur_rand_seg0 = sg.s
                    cur_rand_conds = active()
                    cur_rand_cands = [RandCandidate(sg.s, float(sg.p1), fl, sg.tf, sg.tn, sg.lj, sg.act)]
                    cur_rand_bodies = [[[], [], [], []]]
                    if sg.lj >= 0:
                        edges.append((sg.lj, sg.s, cur_rand_conds))
                elif ctrl == FL_ELSER:
                    if cur_rand_seg0 is not None:
                        cur_rand_cands.append(RandCandidate(sg.s, float(sg.p1), fl, sg.tf, sg.tn, sg.lj, sg.act))
                        cur_rand_bodies.append([[], [], [], []])
                    if sg.lj >= 0:
                        ec = active()
                        if cobj is not None:
                            ec = ec + (("POS", cobj),)
                        edges.append((sg.lj, sg.s, ec))
                elif ctrl == FL_ENDR:
                    if cur_rand_seg0 is not None:
                        cur_rand_cands.append(RandCandidate(sg.s, float(sg.p1), fl, sg.tf, sg.tn, sg.lj, sg.act))
                        cur_rand_bodies.append([[], [], [], []])
                    if sg.lj >= 0:
                        ec = active()
                        if cobj is not None:
                            ec = ec + (("POS", cobj),)
                        edges.append((sg.lj, sg.s, ec))
                else:
                    if in_rand_block and cur_rand_bodies:
                        btfs, bacts, bljs, bwrites = cur_rand_bodies[-1]
                        if sg.tf >= 0 and sg.tn >= 0:
                            btfs.append((sg.tf, sg.tn))
                        if sg.act > 0:
                            bacts.append(sg.act)
                        if sg.lj >= 0:
                            bljs.append(sg.lj)
                        if 0x80 <= op <= 0x93:
                            bwrites.append(f"var_{op - 0x80}={sg.p1}")
                    if sg.lj >= 0:
                        ec = active()
                        if cobj is not None:
                            ec = ec + (("POS", cobj),)
                        edges.append((sg.lj, sg.s, ec))

        if fl & FL_TERMINATOR_MASK:
            if in_rand_block and len(stack) == rand_if_depth:
                cand_terminated = True
            elif stack:
                stack[-1][2] = True
            else:
                top_terminated = True
        if fl & FL_END:
            flush_rand()
            break
    flush_rand()
    return edges, rands


def is_movement_only_node(
    sub_thk: ThkFile,
    nidx: int,
    actions: Dict[int, str],
    seen: Optional[Set[int]] = None,
) -> bool:
    """True iff subnode only executes locomotion/turn/idle actions and contains no combat/RANDOM logic."""
    if nidx < 0 or nidx >= len(sub_thk.nodes):
        return False
    if seen is None:
        seen = set()
    if nidx in seen:
        return False
    seen2 = seen | {nidx}
    nd = sub_thk.nodes[nidx]
    acts: List[str] = []
    child_ljs: List[int] = []
    for sg in nd.segs:
        if (sg.fl & CTRL_KIND_MASK) == FL_RANDOM or sg.tf >= 0:
            return False
        if sg.act > 0:
            acts.append(actions.get(sg.act, f"act_{sg.act}"))
        if sg.lj >= 0:
            child_ljs.append(sg.lj)
    if acts:
        return all(is_movement_action(a) for a in acts) and all(
            is_movement_only_node(sub_thk, lj, actions, seen2) for lj in child_ljs
        )
    if child_ljs:
        return all(is_movement_only_node(sub_thk, lj, actions, seen2) for lj in child_ljs)
    return False


def resolve_subnode_actions(sub_thk: ThkFile, idx: int, depth: int = 0, seen: Optional[Set[int]] = None) -> List[int]:
    if seen is None:
        seen = set()
    if idx in seen or depth > 4 or idx < 0 or idx >= len(sub_thk.nodes):
        return []
    seen2 = seen | {idx}
    acts: List[int] = []
    for sg in sub_thk.nodes[idx].segs:
        if sg.act > 0 and sg.act not in acts:
            acts.append(sg.act)
    if not acts:
        for sg in sub_thk.nodes[idx].segs:
            if sg.lj >= 0:
                for a in resolve_subnode_actions(sub_thk, sg.lj, depth + 1, seen2):
                    if a not in acts:
                        acts.append(a)
    return acts


def collect_subnode_profile(
    tf: int,
    tn: int,
    sub_thk: Optional[ThkFile],
    actions: Dict[int, str],
    stats: ParamStats,
) -> SubnodeProfile:
    idx = sub_thk.nid_to_idx.get(tn, -1) if sub_thk is not None else -1
    if sub_thk is None or idx < 0:
        orig = f"Unresolved_{tf}_{tn}"
        return SubnodeProfile(
            tf=tf,
            tn=tn,
            idx=-1,
            direct_actions=(),
            direct_combat=(),
            all_actions=(),
            orig_primary=orig,
            snake_primary=to_snake(orig),
            chains=(),
            internal_checks=(),
            var_writes=(),
            end_params=(),
            has_rand=False,
        )

    nd = sub_thk.nodes[idx]
    direct_aids: List[int] = []
    for sg in nd.segs:
        if sg.act > 0 and sg.act not in direct_aids:
            direct_aids.append(sg.act)
    direct_actions = tuple(actions.get(a, f"act_{a}") for a in direct_aids)
    direct_combat = tuple(n for n in direct_actions if not is_movement_action(n))

    all_aids = resolve_subnode_actions(sub_thk, idx)
    all_actions = tuple(actions.get(a, f"act_{a}") for a in all_aids)

    chains: List[str] = []
    if direct_combat:
        for sg in nd.segs:
            if 0 <= sg.lj < len(sub_thk.nodes):
                lnd = sub_thk.nodes[sg.lj]
                lcombat = [
                    actions.get(s2.act, f"act_{s2.act}")
                    for s2 in lnd.segs
                    if s2.act > 0 and not is_movement_action(actions.get(s2.act, f"act_{s2.act}"))
                ]
                has_lrand = any((s2.fl & CTRL_KIND_MASK) == FL_RANDOM for s2 in lnd.segs)
                if lcombat:
                    for lc in lcombat:
                        if lc not in chains:
                            chains.append(lc)
                elif has_lrand and "d1_random" not in chains:
                    chains.append("d1_random")

    checks_set: Set[str] = set()
    var_writes: List[str] = []
    end_params_list: List[Tuple[int, int]] = []
    has_rand_ref = [False]

    def scan_node_with_polarity(
        nidx: int,
        depth: int,
        seen: Set[int],
        recurse_lj: bool,
        inherited: Tuple[RawCond, ...] = (),
    ) -> None:
        if nidx < 0 or nidx >= len(sub_thk.nodes) or nidx in seen:
            return
        seen2 = seen | {nidx}
        cur_nd = sub_thk.nodes[nidx]
        stack: List[List[Any]] = []
        top_term = False
        in_rand = False
        rand_if_depth = 0
        cand_term = False

        def cur_active() -> Tuple[RawCond, ...]:
            out: List[RawCond] = list(inherited)
            for prev_neg, c, _term in stack:
                for x in prev_neg:
                    out.append(("NOT", x))
                if c is not None:
                    out.append(("POS", c))
            return tuple(out)

        def cur_dead() -> bool:
            return top_term or cand_term or any(f[2] for f in stack)

        def record_conds(conds: Tuple[RawCond, ...]) -> None:
            for pol, (cop, cp1, cp2) in conds:
                checks_set.add(format_atomic_pred(pol, cop, cp1, cp2, stats))

        for sg in cur_nd.segs:
            fl, op = sg.fl, sg.op
            cobj = (op, sg.p1, sg.p2) if op not in (0, 2) and op not in SETTER_OPS else None

            if fl & FL_END_RANDOM:
                in_rand = False
                cand_term = False
            elif fl & FL_IF:
                stack.append([[], cobj, False])
            elif fl & FL_ELIF_ELSE:
                if stack:
                    pn, pc, _t = stack.pop()
                    nn = list(pn) + ([pc] if pc is not None else [])
                    stack.append([nn, cobj, False])
            elif fl & FL_ENDIF:
                if stack:
                    stack.pop()
                if in_rand and len(stack) < rand_if_depth:
                    in_rand = False
                    cand_term = False
            else:
                ctrl_hdr = fl & CTRL_KIND_MASK
                if ctrl_hdr in (FL_RANDOM, FL_ELSER, FL_ENDR):
                    cand_term = False
                    if ctrl_hdr == FL_RANDOM:
                        in_rand = True
                        rand_if_depth = len(stack)

            if not cur_dead():
                ctrl = fl & CTRL_KIND_MASK
                if ctrl == FL_RANDOM:
                    has_rand_ref[0] = True
                if 0x80 <= op <= 0x93:
                    vw = f"var_{op - 0x80}={sg.p1}"
                    if vw not in var_writes:
                        var_writes.append(vw)
                for ep in sg.end_params:
                    if ep not in end_params_list:
                        end_params_list.append(ep)

                is_exec = (
                    sg.act > 0
                    or sg.lj >= 0
                    or sg.tf >= 0
                    or ctrl in (FL_RANDOM, FL_ELSER, FL_ENDR)
                    or (0x80 <= op <= 0x93)
                )
                is_inline_guard = (not (fl & (FL_IF | FL_ELIF_ELSE | FL_ENDIF))) and cobj is not None
                if is_exec or is_inline_guard:
                    act_conds = cur_active()
                    if is_inline_guard and cobj is not None:
                        act_conds = act_conds + (("POS", cobj),)
                    record_conds(act_conds)
                    if (
                        recurse_lj
                        and sg.lj >= 0
                        and depth < 2
                        and sg.lj not in seen2
                        and not is_movement_only_node(sub_thk, sg.lj, actions)
                    ):
                        scan_node_with_polarity(sg.lj, depth + 1, seen2, True, act_conds)

            if fl & FL_TERMINATOR_MASK:
                if in_rand and len(stack) == rand_if_depth:
                    cand_term = True
                elif stack:
                    stack[-1][2] = True
                else:
                    top_term = True
            if fl & FL_END:
                break

    scan_node_with_polarity(idx, 0, set(), recurse_lj=(len(direct_combat) == 0))
    has_rand = has_rand_ref[0]

    if not all_actions:
        orig_primary = f"NoAction_{tf}_{tn}"
    else:
        combat = [n for n in all_actions if not is_movement_action(n)]
        orig_primary = combat[0] if combat else all_actions[0]

    return SubnodeProfile(
        tf=tf,
        tn=tn,
        idx=idx,
        direct_actions=direct_actions,
        direct_combat=direct_combat,
        all_actions=all_actions,
        orig_primary=orig_primary,
        snake_primary=to_snake(orig_primary),
        chains=tuple(chains),
        internal_checks=tuple(sorted(checks_set)),
        var_writes=tuple(var_writes),
        end_params=tuple(end_params_list),
        has_rand=has_rand,
    )


def _check_to_suffix_token(chk: str) -> str:
    """Compact deterministic suffix token for a differentiating internal check."""
    m = re.match(r"^(dist3d|dist)(<=|>=|<|>|=)(-?\d+)$", chk)
    if m:
        return f"{m.group(1)}{m.group(3)}"
    m2 = re.match(r"^angle=(.+)$", chk)
    if m2:
        clean = re.sub(r"[^0-9A-Za-z]+", "_", m2.group(1)).strip("_")
        return f"ang{clean}"
    clean = re.sub(r"[^0-9A-Za-z]+", "_", chk).strip("_").lower()
    return clean


def disambiguate_cooccurring_group(base_key: str, entries: List[SubnodeProfile]) -> List[Tuple[str, List[SubnodeProfile]]]:
    """Generally partition co-occurring entries by structural differences in their SubnodeProfile."""
    if len(entries) <= 1:
        return [(base_key, entries)]

    # 1. Distinguish chained vs plain (or distinct chain targets)
    plain = [e for e in entries if not e.chains]
    chained = [e for e in entries if e.chains]
    subgroups: List[Tuple[str, List[SubnodeProfile]]] = []
    if plain and chained:
        subgroups.append((base_key, plain))
        subgroups.append((f"{base_key}_chain", chained))
    elif len({e.chains for e in entries}) == len(entries) and all(e.chains for e in entries):
        for e in entries:
            subgroups.append((f"{base_key}_chain_{to_snake(e.chains[0])}", [e]))
    else:
        subgroups.append((base_key, entries))

    # 2. Refine any still-colliding subgroup using direct_combat -> internal_checks -> var_writes -> end_params
    out: List[Tuple[str, List[SubnodeProfile]]] = []
    for skey, grp in subgroups:
        if len(grp) <= 1:
            out.append((skey, grp))
            continue

        # 2a. Distinct direct combat signatures
        dc_sigs = [e.direct_combat for e in grp]
        if len(set(dc_sigs)) == len(grp):
            common_dc = set(grp[0].direct_combat)
            for g in grp[1:]:
                common_dc &= set(g.direct_combat)
            resolved_dc = True
            cand_out: List[Tuple[str, List[SubnodeProfile]]] = []
            for e in grp:
                extras = [to_snake(a) for a in e.direct_combat if a not in common_dc]
                if not extras:
                    extras = ["plain"]
                cand_out.append((f"{skey}_{'_'.join(extras)}", [e]))
            if len({k for k, _ in cand_out}) == len(grp) and resolved_dc:
                out.extend(cand_out)
                continue

        # 2b. Symmetric difference of ALL internal checks (`op 0x06..0x7f, 0x94..0x120`)
        common_chk = set(grp[0].internal_checks)
        for g in grp[1:]:
            common_chk &= set(g.internal_checks)
        diff_chks = [tuple(sorted(set(e.internal_checks) - common_chk)) for e in grp]
        if len(set(diff_chks)) == len(grp):
            tokens = [
                ("_".join(_check_to_suffix_token(c) for c in dc[:2]) if dc else "uncond")
                for dc in diff_chks
            ]
            if len(set(tokens)) == len(grp):
                for tok, e in zip(tokens, grp):
                    out.append((f"{skey}_{tok}", [e]))
                continue

        # 2c. Distinct var_writes (`op 0x80..0x93`)
        vw_sigs = [e.var_writes for e in grp]
        if len(set(vw_sigs)) == len(grp):
            common_vw = set(grp[0].var_writes)
            for g in grp[1:]:
                common_vw &= set(g.var_writes)
            tokens = [
                ("set_" + "_".join(_check_to_suffix_token(w) for w in sorted(set(e.var_writes) - common_vw))
                 if (set(e.var_writes) - common_vw) else "nowrite")
                for e in grp
            ]
            if len(set(tokens)) == len(grp):
                for tok, e in zip(tokens, grp):
                    out.append((f"{skey}_{tok}", [e]))
                continue

        # 2d. Distinct action end_params (`+0x50..+0x6C`)
        ep_sigs = [e.end_params for e in grp]
        if len(set(ep_sigs)) == len(grp):
            tokens = [
                ("end_" + "_".join(f"{t}_{v}" for t, v in e.end_params[:2]) if e.end_params else "noend")
                for e in grp
            ]
            if len(set(tokens)) == len(grp):
                for tok, e in zip(tokens, grp):
                    out.append((f"{skey}_{tok}", [e]))
                continue

        # Truly identical profile -> keep grouped so AmbiguousMoveError safety net catches it
        out.append((skey, grp))

    return out


def build_moves_grouped(
    thk_main: ThkFile,
    sub_thks: Dict[int, ThkFile],
    actions: Dict[int, str],
    stats: ParamStats,
) -> Tuple[Dict[str, List[SubnodeProfile]], Dict[Tuple[int, int], str], Dict[Tuple[int, int], SubnodeProfile]]:
    ref_targets = sorted({(sg.tf, sg.tn) for nd in thk_main.nodes for sg in nd.segs if sg.tf >= 0 and sg.tn >= 0})
    profiles: Dict[Tuple[int, int], SubnodeProfile] = {}

    for tf, tn in ref_targets:
        profiles[(tf, tn)] = collect_subnode_profile(tf, tn, sub_thks.get(tf), actions, stats)

    # Also profile any non-direct d=1 subnodes inside sub_thks that have RANDOM blocks or actions
    for tf, sub_thk in sorted(sub_thks.items()):
        for nd in sub_thk.nodes:
            if nd.nid > 0 and (tf, nd.nid) not in profiles:
                profiles[(tf, nd.nid)] = collect_subnode_profile(tf, nd.nid, sub_thk, actions, stats)

    # Detect moves whose primary snake_case name co-occurs >= 2 times inside the same d=0 RANDOM block
    co_occurring: Set[str] = set()
    for nd in thk_main.nodes:
        _, rands = parse_node_cfg(nd)
        for rb in rands:
            by_snake: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)
            for c in rb.cands:
                if c.weight > 0 and c.tf >= 0 and (c.tf, c.tn) in profiles:
                    by_snake[profiles[(c.tf, c.tn)].snake_primary].add((c.tf, c.tn))
            for sn, tset in by_snake.items():
                if len(tset) >= 2:
                    co_occurring.add(sn)

    by_snake_raw: Dict[str, List[SubnodeProfile]] = defaultdict(list)
    for tf, tn in ref_targets:
        prof = profiles[(tf, tn)]
        by_snake_raw[prof.snake_primary].append(prof)

    moves_grouped: Dict[str, List[SubnodeProfile]] = defaultdict(list)
    target_to_primary: Dict[Tuple[int, int], str] = {}

    for sn, entries in sorted(by_snake_raw.items()):
        if sn in co_occurring:
            groups = disambiguate_cooccurring_group(sn, entries)
        else:
            groups = [(sn, entries)]
        for gkey, gentries in groups:
            # Guard against accidental collision with another snake_primary key
            final_key = gkey
            while final_key in moves_grouped and any(p.snake_primary != gentries[0].snake_primary for p in moves_grouped[final_key]):
                final_key = f"{final_key}_{gentries[0].tf}_{gentries[0].tn}"
            for prof in gentries:
                moves_grouped[final_key].append(prof)
                target_to_primary[(prof.tf, prof.tn)] = final_key

    return dict(sorted(moves_grouped.items())), target_to_primary, profiles


def extract_model(
    thk_main: ThkFile,
    sub_thks: Dict[int, ThkFile],
    actions: Dict[int, str],
    default_files_str: str,
    enum_ops: Optional[Set[int]] = None,
):
    stats = collect_param_stats([thk_main] + list(sub_thks.values()))
    moves_grouped, target_to_primary, subnode_profiles = build_moves_grouped(thk_main, sub_thks, actions, stats)

    # 1. d=0 full CFG path propagation from node 0
    adj0: Dict[int, List[Tuple[int, int, Tuple[RawCond, ...]]]] = {}
    rands0: Dict[int, List[RandBlock]] = {}
    all_segs_d0: Dict[int, List[int]] = {}
    for nd in thk_main.nodes:
        adj0[nd.idx], rands0[nd.idx] = parse_node_cfg(nd)
        if rands0[nd.idx]:
            all_segs_d0[nd.idx] = sorted(rb.seg0 for rb in rands0[nd.idx])

    raw_d0_by_pt: Dict[Tuple[int, int], List[Tuple[Tuple[int, ...], PathState, RandBlock]]] = defaultdict(list)

    def dfs0(u: int, via: Tuple[int, ...], conds: Tuple[RawCond, ...], seen: Set[int]):
        if u in seen:
            return
        seen2 = seen | {u}
        for rb in rands0[u]:
            st = simplify_conditions(conds + rb.local_conds, stats, enum_ops)
            if st is not None:
                raw_d0_by_pt[(u, rb.seg0)].append((via, st, rb))
        for v, _s, econds in adj0[u]:
            if simplify_conditions(conds + econds, stats, enum_ops) is not None:
                dfs0(v, via + (u,), conds + econds, seen2)

    dfs0(0, (), (), set())

    d0_points: List[MetaPoint] = []
    for (u, seg0) in sorted(raw_d0_by_pt.keys()):
        entries = raw_d0_by_pt[(u, seg0)]
        rb = entries[0][2]
        vias = tuple(sorted({e[0] for e in entries}))
        parents = tuple(sorted({e[0][-1] for e in entries if e[0]}))
        merged_states = merge_adjacent_dist_states([e[1] for e in entries])
        d0_points.append(
            MetaPoint(
                depth=0,
                file_label=default_files_str,
                node_idx=u,
                node_nid=thk_main.nodes[u].nid,
                seg0=seg0,
                cands=rb.cands,
                parents=parents,
                vias=vias,
                states=merged_states,
            )
        )

    # 2. d=1 sub-THK DFS across all discovered sub-THK files
    all_segs_d1: Dict[Tuple[int, int], List[int]] = {}
    d1_points: List[MetaPoint] = []

    for tf, sub_thk in sorted(sub_thks.items()):
        adj1: Dict[int, List[Tuple[int, int, Tuple[RawCond, ...]]]] = {}
        rands1: Dict[int, List[RandBlock]] = {}
        lparents1: Dict[int, List[int]] = defaultdict(list)
        for nd in sub_thk.nodes:
            e, r = parse_node_cfg(nd)
            adj1[nd.idx] = e
            rands1[nd.idx] = r
            if r:
                all_segs_d1[(tf, nd.idx)] = sorted(rb.seg0 for rb in r)
            for tgt, _s, _c in e:
                lparents1[tgt].append(nd.idx)

        callers_of_nid: Dict[int, Set[int]] = defaultdict(set)
        entry_nids: Set[int] = set()
        for nd in thk_main.nodes:
            for sg in nd.segs:
                if sg.tf == tf and sg.tn >= 0:
                    callers_of_nid[sg.tn].add(nd.idx)
                    entry_nids.add(sg.tn)

        def find_callers(idx_sub: int, depth: int = 0, seen: Optional[Set[int]] = None) -> Set[int]:
            if seen is None:
                seen = set()
            if idx_sub in seen or depth > 6:
                return set()
            seen2 = seen | {idx_sub}
            nid = sub_thk.nodes[idx_sub].nid
            out: Set[int] = set(callers_of_nid.get(nid, set()))
            for p_idx in lparents1[idx_sub]:
                out |= find_callers(p_idx, depth + 1, seen2)
            return out

        raw_d1_by_pt: Dict[Tuple[int, int], List[Tuple[PathState, RandBlock, Tuple[int, int]]]] = defaultdict(list)

        def dfs1(u: int, entry_key: Tuple[int, int], conds: Tuple[RawCond, ...], seen: Set[int]):
            if u in seen:
                return
            seen2 = seen | {u}
            for rb in rands1[u]:
                st = simplify_conditions(conds + rb.local_conds, stats, enum_ops)
                if st is not None:
                    raw_d1_by_pt[(u, rb.seg0)].append((st, rb, entry_key))
            for v, _s, econds in adj1[u]:
                if simplify_conditions(conds + econds, stats, enum_ops) is not None:
                    dfs1(v, entry_key, conds + econds, seen2)

        for tn in sorted(entry_nids):
            idx_e = sub_thk.nid_to_idx.get(tn, -1)
            if idx_e >= 0:
                dfs1(idx_e, (tf, tn), (), set())

        # Include any non-entry-reachable subnode RANDOM blocks via local_conds fallback
        for nd in sub_thk.nodes:
            for rb in rands1[nd.idx]:
                if (nd.idx, rb.seg0) not in raw_d1_by_pt:
                    st = simplify_conditions(rb.local_conds, stats, enum_ops)
                    if st is not None:
                        raw_d1_by_pt[(nd.idx, rb.seg0)].append((st, rb, (tf, nd.nid)))

        for (u, seg0) in sorted(raw_d1_by_pt.keys()):
            entries = raw_d1_by_pt[(u, seg0)]
            nd = sub_thk.nodes[u]
            rb = entries[0][1]
            merged_states = merge_adjacent_dist_states([e[0] for e in entries])
            entry_owners = tuple(sorted({f"{etf}:{etn}" for _, _, (etf, etn) in entries}))
            callers = find_callers(u)
            d1_points.append(
                MetaPoint(
                    depth=1,
                    file_label=str(tf),
                    node_idx=u,
                    node_nid=nd.nid,
                    seg0=seg0,
                    cands=rb.cands,
                    parents=(),
                    vias=(),
                    states=merged_states,
                    owner_tf=tf,
                    owner_tn=nd.nid,
                    callers=tuple(sorted(callers)),
                    entry_owners=entry_owners,
                )
            )

    return (
        moves_grouped,
        target_to_primary,
        subnode_profiles,
        all_segs_d0,
        all_segs_d1,
        d0_points,
        d1_points,
    )


# =============================================================================
# TOML 1.0 Renderer (`.thkmeta.toml`)
# =============================================================================


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_num(w: float) -> str:
    return str(int(w)) if w == int(w) else f"{w:g}"


def toml_state_fields(st: PathState) -> str:
    parts: List[str] = []
    if st.dist_lo > 0 or st.dist_hi is not None:
        hi_s = '"inf"' if st.dist_hi is None else str(st.dist_hi)
        parts.append(f"dist = [{st.dist_lo}, {hi_s}]")
    if st.dist3d_lo > 0 or st.dist3d_hi is not None:
        hi_s = '"inf"' if st.dist3d_hi is None else str(st.dist3d_hi)
        parts.append(f"dist3d = [{st.dist3d_lo}, {hi_s}]")
    if st.angles != ((0, 360),):
        parts.append(f"angle = {toml_str(format_angle_intervals(st.angles))}")
    if st.other_preds:
        p_s = ", ".join(toml_str(x) for x in st.other_preds)
        parts.append(f"preds = [{p_s}]")
    elif not parts:
        parts.append("preds = []")
    return ", ".join(parts)


def _cand_body_effect_tokens(
    c: RandCandidate,
    target_to_primary: Dict[Tuple[int, int], str],
    actions: Dict[int, str],
    sub_thk: Optional[ThkFile],
    subnode_profiles: Optional[Dict[Tuple[int, int], SubnodeProfile]],
) -> List[str]:
    effs: List[str] = []
    for btf, btn in c.body_tfs:
        nm = target_to_primary.get((btf, btn))
        if not nm and subnode_profiles and (btf, btn) in subnode_profiles:
            nm = subnode_profiles[(btf, btn)].snake_primary
        effs.append(f"{nm}({btf}:{btn})" if nm else f"{btf}:{btn}")
    for aid in c.body_acts:
        aname = to_snake(actions.get(aid, f"act_{aid}"))
        effs.append(f"{aname}(act:{aid})")
    for blj in c.body_ljs:
        if sub_thk is not None and 0 <= blj < len(sub_thk.nodes):
            lnid = sub_thk.nodes[blj].nid
            nm = target_to_primary.get((sub_thk.file_idx, lnid))
            if not nm and subnode_profiles and (sub_thk.file_idx, lnid) in subnode_profiles:
                nm = subnode_profiles[(sub_thk.file_idx, lnid)].snake_primary
            effs.append(f"{nm}(lj:{blj})" if nm else f"lj:{blj}")
        else:
            effs.append(f"node:{blj}")
    for vw in c.body_writes:
        effs.append(f"write:{vw}")
    return effs


def toml_cands_map(
    cands: Sequence[RandCandidate],
    target_to_primary: Dict[Tuple[int, int], str],
    actions: Dict[int, str],
    sub_thk: Optional[ThkFile] = None,
    subnode_profiles: Optional[Dict[Tuple[int, int], SubnodeProfile]] = None,
) -> Tuple[str, str, str]:
    """Return `(cands_inline_str, effects_inline_str, comment_hint_str)` preserving `@seg<N>` and body effects."""
    seen_keys: Set[str] = set()
    kv_parts: List[str] = []
    eff_parts: List[str] = []
    hints: List[str] = []
    any_body_eff = False
    for c in cands:
        if c.weight <= 0:
            continue
        w_s = toml_num(c.weight)
        preserve_seg = False
        if c.tf >= 0 and c.tn >= 0:
            raw_key = f"{c.tf}:{c.tn}"
            mname = target_to_primary.get((c.tf, c.tn), f"move_{c.tf}_{c.tn}")
            base_hint = f"{mname}({w_s})"
        else:
            kind = c.fl & CTRL_KIND_MASK
            ctrl_str = {FL_ENDR: "ENDR", FL_RANDOM: "RANDOM", FL_ELSER: "ELSER"}.get(kind, "CTRL")
            if c.act > 0:
                raw_key = f"act:{c.act}|{ctrl_str}"
                preserve_seg = True
                aname = to_snake(actions.get(c.act, f"act_{c.act}"))
                base_hint = f"{aname}({w_s})"
            elif c.lj >= 0:
                raw_key = f"lj:{c.lj}|{ctrl_str}" if kind in (FL_ENDR, FL_RANDOM, FL_ELSER) else f"lj:{c.lj}"
                preserve_seg = True
                hint_nm: Optional[str] = None
                if sub_thk is not None and 0 <= c.lj < len(sub_thk.nodes):
                    lnid = sub_thk.nodes[c.lj].nid
                    hint_nm = target_to_primary.get((sub_thk.file_idx, lnid))
                    if not hint_nm and subnode_profiles and (sub_thk.file_idx, lnid) in subnode_profiles:
                        hint_nm = subnode_profiles[(sub_thk.file_idx, lnid)].snake_primary
                base_hint = f"{hint_nm}({w_s})" if hint_nm else f"{raw_key}({w_s})"
            elif kind in (FL_ENDR, FL_RANDOM, FL_ELSER):
                raw_key = ctrl_str
                base_hint = f"{raw_key}({w_s})"
            else:
                raw_key = f"seg:{c.s}"
                base_hint = f"{raw_key}({w_s})"

        key = f"{raw_key}@seg{c.s}" if preserve_seg else raw_key
        if key in seen_keys:
            key = f"{raw_key}@seg{c.s}"
        seen_keys.add(key)
        kv_parts.append(f"{toml_str(key)} = {w_s}")

        eff_tokens = _cand_body_effect_tokens(c, target_to_primary, actions, sub_thk, subnode_profiles)
        if eff_tokens:
            any_body_eff = True
            eff_arr = "[" + ", ".join(toml_str(x) for x in eff_tokens) + "]"
            eff_parts.append(f"{toml_str(key)} = {eff_arr}")
            hints.append(f"{key}({w_s} -> {', '.join(eff_tokens)})")
        else:
            hints.append(base_hint)

    eff_inline = ("effects = { " + ", ".join(eff_parts) + " }") if any_body_eff else ""
    return "{ " + ", ".join(kv_parts) + " }", eff_inline, ", ".join(hints)


def render_thkmeta_toml(
    thk_main: ThkFile,
    sub_thks: Dict[int, ThkFile],
    em_id: str,
    default_files: Sequence[int],
    actions: Dict[int, str],
    moves_grouped: Dict[str, List[SubnodeProfile]],
    target_to_primary: Dict[Tuple[int, int], str],
    subnode_profiles: Dict[Tuple[int, int], SubnodeProfile],
    all_segs_d0: Dict[int, List[int]],
    all_segs_d1: Dict[Tuple[int, int], List[int]],
    d0_points: List[MetaPoint],
    d1_points: List[MetaPoint],
    enum_ops: Optional[Set[int]] = None,
) -> str:
    d0_counts: Dict[Tuple[int, int], int] = defaultdict(int)
    for pt in d0_points:
        seen_t: Set[Tuple[int, int]] = set()
        for c in pt.cands:
            if c.weight > 0 and c.tf >= 0 and c.tn >= 0 and (c.tf, c.tn) not in seen_t:
                seen_t.add((c.tf, c.tn))
                d0_counts[(c.tf, c.tn)] += 1

    sub_ids_sorted = sorted(sub_thks.keys())
    src_names = [os.path.basename(thk_main.path)] + [os.path.basename(sub_thks[k].path) for k in sub_ids_sorted]

    lines: List[str] = []
    lines.append("# ============================================================================")
    lines.append("# MIRA Read-Only THK Static Meta Index (.thkmeta.toml, TOML 1.0)")
    lines.append("# 100% auto-extracted from .thk bytecode + MHW ActionTable.")
    lines.append("# Put user-editable groups/ranges/moves/move_groups in `<em_id>.groups.toml`.")
    lines.append("# ============================================================================")
    lines.append("")
    lines.append("[header]")
    lines.append("meta_version = 3")
    lines.append(f"mon = {thk_main.monster_id}")
    lines.append(f"em_id = {toml_str(em_id)}")
    lines.append(f"default_files = [{', '.join(str(x) for x in default_files)}]")
    lines.append(f"sub_files = [{', '.join(str(x) for x in sub_ids_sorted)}]")
    lines.append(f"sources = [{', '.join(toml_str(s) for s in src_names)}]")
    if enum_ops:
        enum_names = [f"em_{op:x}" if op >= 0x101 else f"op_{op:x}" for op in sorted(enum_ops)]
        lines.append(f"enum_ops = [{', '.join(toml_str(s) for s in enum_names)}]")
    lines.append("")

    lines.append("[moves]")
    lines.append(
        f"# snake_case Move -> [list of <file>:<nid>] ({len(moves_grouped)} unique moves, {len(target_to_primary)} nodes)"
    )
    max_name = max((len(k) for k in moves_grouped.keys()), default=16)
    for mname, entries in moves_grouped.items():
        targets = "[" + ", ".join(toml_str(f"{p.tf}:{p.tn}") for p in entries) + "]"
        orig_dti = entries[0].orig_primary
        notes = []
        for p in entries:
            cnt = d0_counts.get((p.tf, p.tn), 0)
            tags = [f"{cnt} pts"]
            if p.has_rand:
                tags.append("d=1")
            if p.internal_checks:
                tags.append("chk:" + ",".join(p.internal_checks[:4]) + ("..." if len(p.internal_checks) > 4 else ""))
            if p.chains:
                tags.append("chain->" + "/".join(p.chains))
            if p.var_writes:
                tags.append("writes:" + ",".join(p.var_writes))
            notes.append(f"{p.tf}:{p.tn}({', '.join(tags)})")
        lines.append(f"{mname:<{max_name}} = {targets:<28s} # {orig_dti} | {'; '.join(notes)}")
    lines.append("")

    # Complete read-only subnode behavioral & internal-check profile table
    lines.append("[subnodes]")
    for key in sorted(subnode_profiles.keys()):
        p = subnode_profiles[key]
        m_label = target_to_primary.get(key, p.snake_primary)
        fparts = [f"idx = {p.idx}", f"move = {toml_str(m_label)}"]
        if p.has_rand:
            fparts.append("has_rand = true")
        if p.internal_checks:
            fparts.append("checks = [" + ", ".join(toml_str(x) for x in p.internal_checks) + "]")
        if p.chains:
            fparts.append("chains = [" + ", ".join(toml_str(x) for x in p.chains) + "]")
        if p.var_writes:
            fparts.append("var_writes = [" + ", ".join(toml_str(x) for x in p.var_writes) + "]")
        if p.end_params:
            fparts.append("end_params = [" + ", ".join(f"[{t}, {v}]" for t, v in p.end_params) + "]")
        lines.append(f"{toml_str(f'{p.tf}:{p.tn}'):<10s} = {{ {', '.join(fparts)} }}")
    lines.append("")

    inline_aids = sorted(
        {c.act for pt in d0_points + d1_points for c in pt.cands if c.weight > 0 and c.tf < 0 and c.act > 0}
    )
    if inline_aids:
        lines.append("[inline_actions]")
        for aid in inline_aids:
            orig_a = actions.get(aid, f"act_{aid}")
            lines.append(f"{aid} = {toml_str(to_snake(orig_a))}  # {orig_a}")
        lines.append("")

    lines.append("[node_segs.d0]")
    for nidx in sorted(all_segs_d0.keys()):
        lines.append(f"{nidx} = [{', '.join(str(x) for x in all_segs_d0[nidx])}]")
    lines.append("")
    lines.append("[node_segs.d1]")
    single_sub = len(sub_ids_sorted) <= 1
    for (tf, nidx) in sorted(all_segs_d1.keys()):
        k_str = str(nidx) if single_sub else toml_str(f"{tf}:{nidx}")
        lines.append(f"{k_str} = [{', '.join(str(x) for x in all_segs_d1[(tf, nidx)])}]")
    lines.append("")

    lines.append(f"# Reachable feasible RANDOM points: d=0 -> {len(d0_points)}, d=1 -> {len(d1_points)}")
    by_n0: Dict[int, List[MetaPoint]] = defaultdict(list)
    for pt in d0_points:
        by_n0[pt.node_idx].append(pt)

    for nidx in sorted(by_n0.keys()):
        pts = by_n0[nidx]
        lines.append(f"[points.d0.n{nidx}]  # nid={pts[0].node_nid}")
        lines.append(f"nid = {pts[0].node_nid}")
        for pt in pts:
            p_arr = "[" + ", ".join(str(x) for x in pt.parents) + "]"
            v_arr = "[" + ", ".join(toml_str(">".join(str(x) for x in v)) for v in pt.vias) + "]"
            c_tbl, eff_tbl, hints = toml_cands_map(pt.cands, target_to_primary, actions, subnode_profiles=subnode_profiles)
            eff_part = f", {eff_tbl}" if eff_tbl else ""
            if len(pt.states) == 1:
                st_str = toml_state_fields(pt.states[0])
                lines.append(
                    f"s{pt.seg0} = {{ parents = {p_arr}, via = {v_arr}, {st_str}, cands = {c_tbl}{eff_part} }}  # {hints}"
                )
            else:
                p_list = "[" + ", ".join("{ " + toml_state_fields(st) + " }" for st in pt.states) + "]"
                lines.append(
                    f"s{pt.seg0} = {{ parents = {p_arr}, via = {v_arr}, paths = {p_list}, cands = {c_tbl}{eff_part} }}  # {hints}"
                )
        lines.append("")

    by_n1: Dict[Tuple[int, int], List[MetaPoint]] = defaultdict(list)
    for pt in d1_points:
        by_n1[(pt.owner_tf, pt.node_idx)].append(pt)

    for (tf, nidx) in sorted(by_n1.keys()):
        pts = by_n1[(tf, nidx)]
        sec_key = f"n{nidx}" if single_sub else f"f{tf}_n{nidx}"
        lines.append(f"[points.d1.{sec_key}]  # nid={pts[0].node_nid}, owner={pts[0].owner_tf}:{pts[0].owner_tn}")
        lines.append(f"file = {tf}")
        lines.append(f"nid = {pts[0].node_nid}")
        lines.append(f"owner = {toml_str(f'{pts[0].owner_tf}:{pts[0].owner_tn}')}")
        for pt in pts:
            c_arr = "[" + ", ".join(str(x) for x in pt.callers) + "]"
            eo_arr = "[" + ", ".join(toml_str(x) for x in pt.entry_owners) + "]"
            c_tbl, eff_tbl, hints = toml_cands_map(
                pt.cands,
                target_to_primary,
                actions,
                sub_thk=sub_thks.get(pt.owner_tf),
                subnode_profiles=subnode_profiles,
            )
            eff_part = f", {eff_tbl}" if eff_tbl else ""
            if len(pt.states) == 1:
                st_part = toml_state_fields(pt.states[0])
            else:
                st_part = "paths = [" + ", ".join("{ " + toml_state_fields(st) + " }" for st in pt.states) + "]"
            eo_part = f", entry_owners = {eo_arr}" if pt.entry_owners and pt.entry_owners != (f"{pt.owner_tf}:{pt.owner_tn}",) else ""
            lines.append(f"s{pt.seg0} = {{ callers = {c_arr}{eo_part}, {st_part}, cands = {c_tbl}{eff_part} }}  # {hints}")
        lines.append("")

    text = "\n".join(lines)
    tomllib.loads(text)
    return text


# =============================================================================
# Standalone TOML Loaders (`tomllib` — Zero `.thk` or PE Dependency!)
# =============================================================================


@dataclasses.dataclass
class LoadedThkMeta:
    mon: int
    em_id: str
    default_files: str
    sub_files: List[int]
    moves: Dict[str, List[Tuple[int, int]]]
    node_segs_d0: Dict[int, List[int]]
    node_segs_d1: Dict[Tuple[int, int], List[int]]
    d0_points: List[MetaPoint]
    d1_points: List[MetaPoint]
    inline_actions: Dict[int, str] = dataclasses.field(default_factory=dict)
    subnode_by_idx: Dict[Tuple[int, int], Tuple[int, str]] = dataclasses.field(default_factory=dict)
    subnode_moves: Dict[str, List[Tuple[int, int]]] = dataclasses.field(default_factory=dict)
    sources: List[str] = dataclasses.field(default_factory=list)
    subnode_info: Dict[Tuple[int, int], Dict[str, Any]] = dataclasses.field(default_factory=dict)
    target_to_move: Dict[Tuple[int, int], str] = dataclasses.field(default_factory=dict)
    enum_ops: Set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class LoadedGroups:
    conditions: Dict[str, str]
    groups: Dict[str, str]
    move_groups: Dict[str, List[Tuple[int, int]]]
    alias_items: Dict[str, List[str]] = dataclasses.field(default_factory=dict)


def state_from_toml_dict(d: Dict[str, Any]) -> PathState:
    d_lo, d_hi = 0, None
    if "dist" in d:
        lo_v, hi_v = d["dist"]
        d_lo = int(lo_v)
        d_hi = None if hi_v == "inf" else int(hi_v)
    d3_lo, d3_hi = 0, None
    if "dist3d" in d:
        lo_v, hi_v = d["dist3d"]
        d3_lo = int(lo_v)
        d3_hi = None if hi_v == "inf" else int(hi_v)
    angles = parse_angle_spec(d["angle"]) if ("angle" in d and str(d["angle"]).strip() != "*") else ((0, 360),)
    preds = tuple(sorted(str(x) for x in d.get("preds", [])))
    return PathState(d_lo, d_hi, d3_lo, d3_hi, angles, preds)


def cand_from_toml_kv(k: str, w: Any) -> RandCandidate:
    raw = k.strip()
    m_seg = re.search(r"@seg(\d+)$", raw)
    seg_idx = int(m_seg.group(1)) if m_seg else -1
    base_k = raw[: m_seg.start()] if m_seg else raw
    weight = float(w)
    if base_k.startswith("act:"):
        rest = base_k[4:]
        aid_s, _, ctrl_s = rest.partition("|")
        upper = ctrl_s.upper()
        fl = FL_ENDR if "ENDR" in upper else (FL_RANDOM if "RANDOM" in upper else (FL_ELSER if "ELSER" in upper else 0))
        return RandCandidate(s=seg_idx, weight=weight, fl=fl, tf=-1, tn=-1, lj=-1, act=int(aid_s), raw_key=raw)
    if ":" in base_k and not base_k.startswith("lj:") and not base_k.startswith("seg:"):
        tf_s, tn_s = base_k.split(":", 1)
        return RandCandidate(s=seg_idx, weight=weight, fl=0, tf=int(tf_s), tn=int(tn_s), lj=-1, raw_key=raw)
    upper = base_k.upper()
    fl = 0
    if "ENDR" in upper:
        fl = FL_ENDR
    elif "RANDOM" in upper:
        fl = FL_RANDOM
    elif "ELSER" in upper:
        fl = FL_ELSER
    lj = -1
    if base_k.startswith("lj:"):
        lj = int(base_k[3:].split("|", 1)[0])
    s = seg_idx
    if base_k.startswith("seg:"):
        s = int(base_k[4:])
    return RandCandidate(s=s, weight=weight, fl=fl, tf=-1, tn=-1, lj=lj, raw_key=raw)


def load_thkmeta_toml(path: str) -> LoadedThkMeta:
    with open(path, "rb") as f:
        doc = tomllib.load(f)

    hdr = doc["header"]
    mon = int(hdr["mon"])
    em_id = str(hdr.get("em_id", f"mon{mon}"))
    default_files = ",".join(str(x) for x in hdr.get("default_files", [0]))
    sub_files = [int(x) for x in hdr.get("sub_files", [55])]
    default_sub = sub_files[0] if sub_files else 55
    sources = [str(x) for x in hdr.get("sources", [])]
    enum_ops = {str(x) for x in hdr.get("enum_ops", [])}

    moves: Dict[str, List[Tuple[int, int]]] = {}
    for mname, items in doc.get("moves", {}).items():
        tlist: List[Tuple[int, int]] = []
        for it in items:
            tf_s, tn_s = str(it).split(":", 1)
            tlist.append((int(tf_s), int(tn_s)))
        moves[mname] = tlist

    inline_actions: Dict[int, str] = {int(k): str(v) for k, v in doc.get("inline_actions", {}).items()}
    subnode_by_idx: Dict[Tuple[int, int], Tuple[int, str]] = {}
    subnode_moves_map: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
    subnode_info: Dict[Tuple[int, int], Dict[str, Any]] = {}
    target_to_move: Dict[Tuple[int, int], str] = {}
    for sk, sv in doc.get("subnodes", {}).items():
        tf_s, tn_s = str(sk).split(":", 1)
        tf_i, tn_i = int(tf_s), int(tn_s)
        s_idx = int(sv["idx"])
        s_move = str(sv["move"])
        subnode_by_idx[(tf_i, s_idx)] = (tn_i, s_move)
        if (tf_i, tn_i) not in subnode_moves_map[s_move]:
            subnode_moves_map[s_move].append((tf_i, tn_i))
        subnode_info[(tf_i, tn_i)] = {
            "idx": s_idx,
            "move": s_move,
            "has_rand": bool(sv.get("has_rand", False)),
            "checks": [str(x) for x in sv.get("checks", [])],
            "chains": [str(x) for x in sv.get("chains", [])],
            "var_writes": [str(x) for x in sv.get("var_writes", [])],
            "end_params": [list(x) for x in sv.get("end_params", [])],
        }
        target_to_move[(tf_i, tn_i)] = s_move
    for mname, tlist in moves.items():
        for pair in tlist:
            target_to_move[pair] = mname

    ns = doc.get("node_segs", {})
    node_segs_d0 = {int(k): [int(x) for x in v] for k, v in ns.get("d0", {}).items()}
    node_segs_d1: Dict[Tuple[int, int], List[int]] = {}
    for k, v in ns.get("d1", {}).items():
        ks = str(k)
        if ":" in ks:
            tf_s, ni_s = ks.split(":", 1)
            node_segs_d1[(int(tf_s), int(ni_s))] = [int(x) for x in v]
        else:
            node_segs_d1[(default_sub, int(ks))] = [int(x) for x in v]

    pts = doc.get("points", {})
    d0_points: List[MetaPoint] = []
    for nkey, ntbl in pts.get("d0", {}).items():
        nidx = int(nkey[1:])
        nid = int(ntbl.get("nid", 0))
        for skey, stbl in ntbl.items():
            if not skey.startswith("s"):
                continue
            seg0 = int(skey[1:])
            parents = tuple(int(x) for x in stbl.get("parents", []))
            vias = tuple(tuple(int(x) for x in str(v).split(">")) for v in stbl.get("via", []) if v)
            if "paths" in stbl:
                states = [state_from_toml_dict(p) for p in stbl["paths"]]
            else:
                states = [state_from_toml_dict(stbl)]
            cands = [cand_from_toml_kv(ck, cw) for ck, cw in stbl.get("cands", {}).items()]
            effects = {str(ek): tuple(str(x) for x in ev) for ek, ev in stbl.get("effects", {}).items()}
            d0_points.append(
                MetaPoint(
                    depth=0,
                    file_label=default_files,
                    node_idx=nidx,
                    node_nid=nid,
                    seg0=seg0,
                    cands=cands,
                    parents=parents,
                    vias=vias,
                    states=states,
                    effects=effects,
                )
            )

    d1_points: List[MetaPoint] = []
    for nkey, ntbl in pts.get("d1", {}).items():
        if nkey.startswith("f") and "_n" in nkey:
            f_s, ni_s = nkey[1:].split("_n", 1)
            nidx = int(ni_s)
            f_num = int(f_s)
        else:
            nidx = int(nkey[1:])
            f_num = int(ntbl.get("file", default_sub))
        nid = int(ntbl.get("nid", 0))
        otf_s, otn_s = str(ntbl["owner"]).split(":", 1)
        for skey, stbl in ntbl.items():
            if not skey.startswith("s"):
                continue
            seg0 = int(skey[1:])
            callers = tuple(int(x) for x in stbl.get("callers", []))
            entry_owners = tuple(str(x) for x in stbl.get("entry_owners", []))
            if "paths" in stbl:
                states = [state_from_toml_dict(p) for p in stbl["paths"]]
            else:
                states = [state_from_toml_dict(stbl)]
            cands = [cand_from_toml_kv(ck, cw) for ck, cw in stbl.get("cands", {}).items()]
            effects = {str(ek): tuple(str(x) for x in ev) for ek, ev in stbl.get("effects", {}).items()}
            d1_points.append(
                MetaPoint(
                    depth=1,
                    file_label=str(f_num),
                    node_idx=nidx,
                    node_nid=nid,
                    seg0=seg0,
                    cands=cands,
                    parents=(),
                    vias=(),
                    states=states,
                    owner_tf=int(otf_s),
                    owner_tn=int(otn_s),
                    callers=callers,
                    entry_owners=entry_owners,
                    effects=effects,
                )
            )

    d0_points.sort(key=lambda p: (p.node_idx, p.seg0))
    d1_points.sort(key=lambda p: (p.owner_tf, p.node_idx, p.seg0))
    return LoadedThkMeta(
        mon=mon,
        em_id=em_id,
        default_files=default_files,
        sub_files=sub_files,
        moves=moves,
        node_segs_d0=node_segs_d0,
        node_segs_d1=node_segs_d1,
        d0_points=d0_points,
        d1_points=d1_points,
        inline_actions=inline_actions,
        subnode_by_idx=subnode_by_idx,
        subnode_moves=dict(subnode_moves_map),
        sources=sources,
        subnode_info=subnode_info,
        target_to_move=target_to_move,
        enum_ops=enum_ops,
    )


def resolve_move_spec_list(
    spec: str,
    base_moves: Dict[str, List[Tuple[int, int]]],
    raw_move_groups: Dict[str, List[str]],
    seen: Optional[Set[str]] = None,
    inline_names: Optional[Set[str]] = None,
) -> List[Tuple[int, int]]:
    if seen is None:
        seen = set()
    spec = spec.strip()
    if "@" in spec:
        base_name, ref_s = spec.split("@", 1)
        if ":" in ref_s:
            tf_s, tn_s = ref_s.split(":", 1)
            want_tf, want_tn = int(tf_s), int(tn_s)
        else:
            want_tf, want_tn = None, int(ref_s)
        cand_list = resolve_move_spec_list(base_name.strip(), base_moves, raw_move_groups, seen, inline_names)
        filtered = [(tf, tn) for tf, tn in cand_list if tn == want_tn and (want_tf is None or tf == want_tf)]
        if not filtered:
            raise ValueError(f"Disambiguator @{ref_s} not found inside move '{base_name}'")
        return filtered
    if "#" in spec:
        base_name, nid_s = spec.split("#", 1)
        want_nid = int(nid_s)
        cand_list = resolve_move_spec_list(base_name.strip(), base_moves, raw_move_groups, seen, inline_names)
        filtered = [(tf, tn) for tf, tn in cand_list if tn == want_nid]
        if not filtered:
            raise ValueError(f"Disambiguator #{want_nid} not found inside move '{base_name}'")
        return filtered
    if ":" in spec:
        tf_s, tn_s = spec.split(":", 1)
        return [(int(tf_s), int(tn_s))]
    if spec in raw_move_groups:
        if spec in seen:
            raise ValueError(f"Cycle in [move_groups] involving '{spec}'")
        seen2 = seen | {spec}
        out: List[Tuple[int, int]] = []
        for item in raw_move_groups[spec]:
            for pair in resolve_move_spec_list(item, base_moves, raw_move_groups, seen2, inline_names):
                if pair not in out:
                    out.append(pair)
        return out
    if spec in base_moves:
        return list(base_moves[spec])
    if inline_names and spec in inline_names:
        return []
    raise ValueError(f"Unknown move or move_group: {spec!r}")


def flatten_groups_table(tbl: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in tbl.items():
        full_k = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten_groups_table(v, full_k))
        else:
            out[full_k] = str(v)
    return out


def load_groups_toml(
    path: Optional[str],
    base_moves: Dict[str, List[Tuple[int, int]]],
    subnode_moves: Optional[Dict[str, List[Tuple[int, int]]]] = None,
    inline_actions: Optional[Dict[int, str]] = None,
) -> LoadedGroups:
    if not path or not os.path.exists(path):
        return LoadedGroups({}, {}, {}, {})
    with open(path, "rb") as f:
        doc = tomllib.load(f)

    combined_moves: Dict[str, List[Tuple[int, int]]] = {k: list(v) for k, v in base_moves.items()}
    if subnode_moves:
        for k, v in subnode_moves.items():
            if k not in combined_moves:
                combined_moves[k] = list(v)
    inline_names: Set[str] = set(inline_actions.values()) if inline_actions else set()

    conditions = {str(k): str(v) for k, v in doc.get("conditions", {}).items()}
    groups = flatten_groups_table(doc.get("groups", {}))

    raw_mg: Dict[str, List[str]] = {}
    identity_moves: Set[str] = set()

    for k, v in doc.get("moves", {}).items():
        if isinstance(v, list):
            raw_mg[k] = [str(x).strip() for x in v if str(x).strip()]
        else:
            v_s = str(v).strip()
            if k == v_s:
                if k not in combined_moves and k not in inline_names:
                    raise ValueError(f"Unknown base move in [moves]: {k!r}")
                identity_moves.add(k)
            else:
                raw_mg[k] = [v_s]

    for k, v in doc.get("move_groups", {}).items():
        if k in raw_mg or k in identity_moves:
            raise ValueError(f"Duplicate move alias/group key '{k}' in [moves] and [move_groups]")
        if isinstance(v, list):
            raw_mg[k] = [str(x).strip() for x in v if str(x).strip()]
        else:
            raw_mg[k] = [x.strip() for x in str(v).split(",") if x.strip()]

    resolved_mg: Dict[str, List[Tuple[int, int]]] = {}
    for mg_name in raw_mg:
        resolved_mg[mg_name] = resolve_move_spec_list(
            mg_name, combined_moves, raw_mg, inline_names=inline_names
        )
    return LoadedGroups(conditions=conditions, groups=groups, move_groups=resolved_mg, alias_items=raw_mg)


# =============================================================================
# Boolean Selector Evaluator & `Move -> [list]` Ambiguity-Safe Compiler
# =============================================================================


def eval_dist_atom(metric: str, cmp_op: str, rhs: str, st: PathState) -> bool:
    s_lo, s_hi = 0, INF
    if cmp_op in ("<=", "<"):
        s_hi = int(rhs)
    elif cmp_op in (">=", ">"):
        s_lo = int(rhs)
    elif cmp_op == "=":
        if ".." in rhs:
            a, b = rhs.split("..", 1)
            s_lo = int(a) if a else 0
            s_hi = int(b) if b else INF
        else:
            s_lo = s_hi = int(rhs)
    else:
        raise ValueError(f"Unsupported distance comparator {cmp_op!r} in {metric}{cmp_op}{rhs}")
    if metric == "dist":
        p_lo, p_hi = st.dist_lo, st.dist_hi
    else:
        p_lo, p_hi = st.dist3d_lo, st.dist3d_hi
    st_hi = INF if p_hi is None else p_hi
    return p_lo >= s_lo and st_hi <= s_hi


_NUM_METRIC_RE = re.compile(
    r"^(var_\d+|quest_id|hp_pct|abs_dy|home_dist|target_home_dist|op_[0-9a-fA-F]+)(<=|>=|<|>|=|!=)(-?\d+(?:\.\.-?\d*)?|\.\.-?\d+)$"
)


def _pred_implies_num_cmp(preds: Sequence[str], metric: str, cmp_op: str, rhs_str: str) -> bool:
    """Sound interval entailment check for numeric predicates in PathState.other_preds."""
    lo, hi = -INF, INF
    neq_set: Set[int] = set()
    constrained = False
    for p in preds:
        mp = _NUM_METRIC_RE.match(p)
        if not mp or mp.group(1) != metric:
            continue
        pop, val_s = mp.group(2), mp.group(3)
        constrained = True
        if pop == "=" and ".." in val_s:
            a_s, b_s = val_s.split("..", 1)
            if a_s:
                lo = max(lo, int(a_s))
            if b_s:
                hi = min(hi, int(b_s))
        else:
            v = int(val_s)
            if pop == "=":
                lo, hi = max(lo, v), min(hi, v)
            elif pop == "!=":
                neq_set.add(v)
            elif pop == ">=":
                lo = max(lo, v)
            elif pop == ">":
                lo = max(lo, v + 1)
            elif pop == "<=":
                hi = min(hi, v)
            elif pop == "<":
                hi = min(hi, v - 1)
    if not constrained or lo > hi:
        return False
    if cmp_op == "=" and ".." in rhs_str:
        a_s, b_s = rhs_str.split("..", 1)
        req_lo = int(a_s) if a_s else -INF
        req_hi = int(b_s) if b_s else INF
        return lo >= req_lo and hi <= req_hi
    rhs = int(rhs_str)
    if cmp_op == "=":
        return lo == hi == rhs
    if cmp_op == "!=":
        return rhs < lo or rhs > hi or rhs in neq_set
    if cmp_op == ">=":
        return lo >= rhs
    if cmp_op == ">":
        return lo > rhs or (lo == rhs and rhs in neq_set)
    if cmp_op == "<=":
        return hi <= rhs
    if cmp_op == "<":
        return hi < rhs or (hi == rhs and rhs in neq_set)
    return False


def expand_move_names_and_targets(
    spec: str,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
) -> Tuple[Set[Tuple[int, int]], Set[str]]:
    """Resolve a move / move_group / alias into `(target_pairs, canonical_move_names)`."""
    sp = spec.strip()
    if ":" in sp and "@" not in sp and "#" not in sp:
        tf_s, tn_s = sp.split(":", 1)
        return {(int(tf_s), int(tn_s))}, set()

    combined_moves: Dict[str, List[Tuple[int, int]]] = {k: list(v) for k, v in meta.moves.items()}
    for k, v in meta.subnode_moves.items():
        if k not in combined_moves:
            combined_moves[k] = list(v)
    inline_names: Set[str] = set(meta.inline_actions.values())

    if "@" in sp or "#" in sp:
        targets = resolve_move_spec_list(sp, combined_moves, grp.alias_items, inline_names=inline_names)
        return set(targets), set()

    target_pairs: Set[Tuple[int, int]] = set()
    move_names: Set[str] = set()

    def walk(name: str, seen: Set[str]) -> None:
        if name in grp.alias_items:
            if name in seen:
                raise ValueError(f"Cycle in [move_groups] involving '{name}'")
            seen2 = seen | {name}
            for item in grp.alias_items[name]:
                walk(item, seen2)
            return
        if ":" in name and "@" not in name and "#" not in name:
            tf_s, tn_s = name.split(":", 1)
            target_pairs.add((int(tf_s), int(tn_s)))
            return
        if "@" in name or "#" in name:
            for pair in resolve_move_spec_list(name, combined_moves, grp.alias_items, inline_names=inline_names):
                target_pairs.add(pair)
            return
        if name not in combined_moves and name not in inline_names:
            raise ValueError(f"Unknown move or move_group: {name!r}")
        move_names.add(name)
        for pair in combined_moves.get(name, ()):
            target_pairs.add(pair)

    walk(sp, set())
    return target_pairs, move_names


def match_move_specs_at_point(
    sp: str,
    pt: MetaPoint,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
) -> List[str]:
    """Return distinct positive-weight low-level specs (`<tf>:<tn>` or `seg:<s_idx>`) matching `sp` at `pt`."""
    target_pairs, move_names = expand_move_names_and_targets(sp, meta, grp)
    hits: List[str] = []

    def add_hit(val: str) -> None:
        if val not in hits:
            hits.append(val)

    for c in pt.cands:
        if c.weight <= 0:
            continue
        if c.tf >= 0 and c.tn >= 0:
            if (c.tf, c.tn) in target_pairs:
                add_hit(f"{c.tf}:{c.tn}")
        elif c.act > 0:
            aname = meta.inline_actions.get(c.act)
            if aname and aname in move_names and c.s >= 0:
                add_hit(f"seg:{c.s}")
        elif pt.depth == 1 and c.lj >= 0:
            nid, mname = meta.subnode_by_idx.get((pt.owner_tf, c.lj), (-1, ""))
            if (((pt.owner_tf, nid) in target_pairs) or (mname and mname in move_names)) and c.s >= 0:
                add_hit(f"seg:{c.s}")
    return hits


def normalize_expr_syntax(expr: str) -> str:
    """Normalize intuitive `angle in -45..45`, `dist in 900..2000`, and spaced comparisons."""
    out = re.sub(
        r"\bangle\s*(?:in\s+|<=|=)\s*(-?\d+\.\.-?\d+(?:\s*\|\s*-?\d+\.\.-?\d+)*)",
        lambda m: "ANGLE:" + re.sub(r"\s*\|\s*", ";", m.group(1).strip()),
        expr,
    )
    out = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(-?\d*\.\.-?\d*)",
        lambda m: f"{m.group(1)}={m.group(2)}",
        out,
    )
    out = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*==\s*",
        r"\1=",
        out,
    )
    out = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(<=|>=|!=|<|>|=)\s*(-?\d+(?:\.\.-?\d*)?|\.\.-?\d+)",
        r"\1\2\3",
        out,
    )
    out = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(true|false)\b",
        lambda m: f"{m.group(1)}={m.group(2).lower()}",
        out,
        flags=re.IGNORECASE,
    )
    return out


def split_selector_str(expr: str) -> List[str]:
    """Split a top-level whitespace-delimited selector string while preserving boolean groups."""
    s = normalize_expr_syntax(expr.strip())
    s = re.sub(r"\s*([&|])\s*", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"!\s+", "!", s)
    terms: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch.isspace() and depth == 0:
            if buf:
                terms.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        terms.append("".join(buf))
    return terms


def tokenize_expr(expr: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    n = len(expr)
    while i < n:
        if expr[i].isspace():
            i += 1
            continue
        if expr[i] in "()&|!":
            tokens.append(expr[i])
            i += 1
        else:
            j = i
            while j < n and (not expr[j].isspace()) and expr[j] not in "()&|!":
                j += 1
            tokens.append(expr[i:j])
            i = j
    return tokens


def eval_group_expr(
    expr: str,
    pt: MetaPoint,
    st: PathState,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
    depth: int = 0,
) -> bool:
    if depth > 16:
        raise ValueError(f"Group expansion exceeded max depth (cycle?): {expr!r}")

    norm = normalize_expr_syntax(expr)
    norm = norm.replace("!=", "__NEQ__").replace("<=", "__LE__").replace(">=", "__GE__")
    toks = tokenize_expr(norm)
    pos = [0]

    def peek() -> Optional[str]:
        return toks[pos[0]] if pos[0] < len(toks) else None

    def consume() -> str:
        t = toks[pos[0]]
        pos[0] += 1
        return t

    def parse_or() -> bool:
        val = parse_and()
        while peek() == "|":
            consume()
            rhs = parse_and()
            val = val or rhs
        return val

    def parse_and() -> bool:
        val = parse_unary()
        while peek() == "&":
            consume()
            rhs = parse_unary()
            val = val and rhs
        return val

    def parse_unary() -> bool:
        if peek() == "!":
            consume()
            return not parse_unary()
        if peek() == "(":
            consume()
            val = parse_or()
            if peek() != ")":
                raise ValueError(f"Unmatched '(' in {expr!r}")
            consume()
            return val
        tok = consume().replace("__NEQ__", "!=").replace("__LE__", "<=").replace("__GE__", ">=")
        return eval_atom(tok)

    def eval_atom(atom: str) -> bool:
        if atom.startswith("ANGLE:"):
            req = parse_angle_spec(atom[6:].replace(";", "|"))
            return angle_subset(st.angles, req)
        if atom.startswith("parent="):
            allowed = {int(x) for x in atom[7:].split(",")}
            return any(p in allowed for p in pt.parents)
        if atom.startswith("via="):
            allowed = {int(x) for x in atom[4:].split(",")}
            return any(any(v in allowed for v in via) for via in pt.vias)
        if atom.startswith("node="):
            allowed = {int(x) for x in atom[5:].split(",")}
            return pt.node_idx in allowed
        if atom.startswith("in="):
            target_pairs, move_names = expand_move_names_and_targets(atom[3:], meta, grp)
            if (pt.owner_tf, pt.owner_tn) in target_pairs:
                return True
            if pt.depth == 1:
                _, owner_mname = meta.subnode_by_idx.get((pt.owner_tf, pt.node_idx), (-1, ""))
                if owner_mname and owner_mname in move_names:
                    return True
            return False
        if atom.startswith("has="):
            return bool(match_move_specs_at_point(atom[4:], pt, meta, grp))

        for cname, raw_op in grp.conditions.items():
            if atom == cname:
                atom = raw_op
                break
            if any(atom.startswith(cname + pfx) for pfx in ("=", ">=", "<=", "<", ">", "!=")):
                atom = raw_op + atom[len(cname) :]
                break

        if atom.startswith("dist"):
            m_dist = re.match(r"^(dist3d|dist)(<=|>=|<|>|=)(.+)$", atom)
            if not m_dist:
                raise ValueError(f"Invalid distance selector atom: {atom!r}")
            return eval_dist_atom(m_dist.group(1), m_dist.group(2), m_dist.group(3), st)

        if "!=" in atom:
            if atom in st.other_preds:
                return True
            lhs, rhs = atom.split("!=", 1)
            # Backward compatibility with legacy `.thkmeta.toml` storing `!k=v`
            if f"!{lhs}={rhs}" in st.other_preds:
                return True
            m = _NUM_METRIC_RE.match(atom)
            if m and _pred_implies_num_cmp(st.other_preds, m.group(1), m.group(2), m.group(3)):
                return True
            # Exclusive enum opcodes (e.g. em_103=1 implies em_103!=0)
            if lhs in meta.enum_ops and any(
                p.startswith(lhs + "=") and p[len(lhs) + 1 :] != rhs for p in st.other_preds
            ):
                return True
            return False

        if "=" in atom and not any(op in atom for op in ("<=", ">=", "!=")):
            k, v = atom.split("=", 1)
            gk = f"{k}.{v}"
            if gk in grp.groups:
                return eval_group_expr(grp.groups[gk], pt, st, meta, grp, depth + 1)
            vl = v.lower()
            if vl == "false":
                return f"!{k}" in st.other_preds
            if vl == "true":
                return k in st.other_preds

        if atom in st.other_preds:
            return True

        m = _NUM_METRIC_RE.match(atom)
        if m:
            return _pred_implies_num_cmp(st.other_preds, m.group(1), m.group(2), m.group(3))
        return False

    res = parse_or()
    if pos[0] != len(toks):
        raise ValueError(f"Unexpected trailing token {toks[pos[0]:]} in {expr!r}")
    return res


def eval_rule_selectors(
    terms: List[str],
    pt: MetaPoint,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
    d0_by_node: Dict[int, List[MetaPoint]],
) -> Tuple[bool, Optional[List[int]]]:
    depth_req: Optional[int] = None
    from_terms: List[str] = []
    normal_terms: List[str] = []

    for t in terms:
        if t.startswith("d="):
            depth_req = int(t[2:])
        elif t.startswith("from=(") and t.endswith(")"):
            from_terms.extend(t[6:-1].strip().split())
        else:
            normal_terms.append(t)

    if depth_req is not None and pt.depth != depth_req:
        return False, None
    if pt.depth == 1 and depth_req is None and not any(t.startswith("in=") or t.startswith("node=") for t in normal_terms):
        if not from_terms:
            return False, None

    def match_terms_on_pt(p: MetaPoint, tlist: List[str]) -> bool:
        for term in tlist:
            if term.startswith("!has="):
                alts = [x.strip() for x in term[5:].split("|") if x.strip()]
                st0 = p.states[0] if p.states else PathState(0, None, 0, None, ((0, 360),), ())
                if any(eval_group_expr(f"has={alt}", p, st0, meta, grp) for alt in alts):
                    return False
                continue
            if term.startswith("!in="):
                alts = [x.strip() for x in term[4:].split("|") if x.strip()]
                st0 = p.states[0] if p.states else PathState(0, None, 0, None, ((0, 360),), ())
                if any(eval_group_expr(f"in={alt}", p, st0, meta, grp) for alt in alts):
                    return False
                continue
            if (
                "&" in term
                or "(" in term
                or ")" in term
                or term.startswith("ANGLE:")
                or ("|" in term and "=" not in term.split("|", 1)[0])
            ):
                if not p.states or not all(eval_group_expr(term, p, st, meta, grp) for st in p.states):
                    return False
                continue
            if term.startswith("!") and "=" not in term:
                inner = term[1:]
                if not p.states or not all(not eval_group_expr(inner, p, st, meta, grp) for st in p.states):
                    return False
                continue
            if term.startswith("dist") or term.startswith("angle") or term.startswith("from="):
                if not p.states or not all(eval_group_expr(term, p, st, meta, grp) for st in p.states):
                    return False
                continue
            if "=" in term and not any(op in term for op in ("<=", ">=", "!=")):
                k, v = term.split("=", 1)
                if re.fullmatch(r"[A-Za-z0-9_.]+", k) and not any(
                    ch in v for ch in ("&", "(", ")", "!", "=", "<", ">")
                ):
                    alts = [x.strip() for x in v.split("|") if x.strip()]
                    if k in ("in", "has", "node", "parent", "via"):
                        if not any(eval_group_expr(f"{k}={alt}", p, p.states[0], meta, grp) for alt in alts):
                            return False
                    else:
                        if not p.states or not all(
                            any(eval_group_expr(f"{k}={alt}", p, st, meta, grp) for alt in alts) for st in p.states
                        ):
                            return False
                else:
                    if not p.states or not all(eval_group_expr(term, p, st, meta, grp) for st in p.states):
                        return False
            else:
                if not p.states or not all(eval_group_expr(term, p, st, meta, grp) for st in p.states):
                    return False
        return True

    if not match_terms_on_pt(pt, normal_terms):
        return False, None

    if pt.depth == 1:
        if not from_terms:
            return True, list(pt.callers)
        matched_callers: List[int] = []
        for cid in pt.callers:
            cpts = d0_by_node.get(cid, [])
            if cpts and any(match_terms_on_pt(cpt, from_terms) for cpt in cpts):
                matched_callers.append(cid)
        return (len(matched_callers) > 0), matched_callers

    return True, None


def match_cand_indices_at_point(
    specs: Sequence[str],
    pt: MetaPoint,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
) -> Set[int]:
    """Return every positive-weight candidate index in `pt.cands` matched by any item in `specs`."""
    ctrl_alias = {
        "ENDR": FL_ENDR,
        "CANCEL": FL_ENDR,
        "STOP": FL_ENDR,
        "NO_BRANCH": FL_ENDR,
        "DENY": FL_ENDR,
        "RANDOM": FL_RANDOM,
        "TRIGGER": FL_RANDOM,
        "TAKE": FL_RANDOM,
        "PROCEED": FL_RANDOM,
        "ELSER": FL_ELSER,
    }
    matched: Set[int] = set()
    for raw_sp in specs:
        sp = raw_sp.strip()
        if not sp:
            continue
        upper = sp.upper()
        if upper in ctrl_alias:
            want = ctrl_alias[upper]
            for idx, c in enumerate(pt.cands):
                if c.weight > 0 and (c.fl & CTRL_KIND_MASK) == want:
                    matched.add(idx)
            continue
        if sp.startswith("seg:"):
            want_s = int(sp[4:])
            for idx, c in enumerate(pt.cands):
                if c.weight > 0 and c.s == want_s:
                    matched.add(idx)
            continue
        if sp.startswith("cand:"):
            c_idx = int(sp[5:])
            if 0 <= c_idx < len(pt.cands) and pt.cands[c_idx].weight > 0:
                matched.add(c_idx)
            continue
        if ":" in sp and "@" not in sp and "#" not in sp:
            tf_s, tn_s = sp.split(":", 1)
            tf, tn = int(tf_s), int(tn_s)
            for idx, c in enumerate(pt.cands):
                if c.weight > 0 and c.tf == tf and c.tn == tn:
                    matched.add(idx)
            continue

        target_pairs, move_names = expand_move_names_and_targets(sp, meta, grp)
        for idx, c in enumerate(pt.cands):
            if c.weight <= 0:
                continue
            if c.tf >= 0 and c.tn >= 0:
                if (c.tf, c.tn) in target_pairs:
                    matched.add(idx)
            elif c.act > 0:
                aname = meta.inline_actions.get(c.act)
                if aname and aname in move_names:
                    matched.add(idx)
            elif pt.depth == 1 and c.lj >= 0:
                nid, mname = meta.subnode_by_idx.get((pt.owner_tf, c.lj), (-1, ""))
                if (pt.owner_tf, nid) in target_pairs or (mname and mname in move_names):
                    matched.add(idx)
    return matched


def cand_to_low_spec(idx: int, pt: MetaPoint) -> str:
    """Format candidate `pt.cands[idx]` into an unambiguous low-level `rng_thk.dll` RHS token."""
    c = pt.cands[idx]
    if c.tf >= 0 and c.tn >= 0:
        same = sum(1 for c2 in pt.cands if c2.weight > 0 and c2.tf == c.tf and c2.tn == c.tn)
        if same == 1:
            return f"{c.tf}:{c.tn}"
        return f"seg:{c.s}" if c.s >= 0 else f"cand:{idx}"
    kind = c.fl & CTRL_KIND_MASK
    if kind != 0:
        kname = {FL_RANDOM: "RANDOM", FL_ENDR: "ENDR", FL_ELSER: "ELSER"}[kind]
        same = sum(1 for c2 in pt.cands if c2.weight > 0 and c2.tf < 0 and (c2.fl & CTRL_KIND_MASK) == kind)
        if c.act == 0 and c.lj < 0 and same == 1:
            return kname
        if c.s >= 0:
            return f"seg:{c.s}"
        return kname if same == 1 else f"cand:{idx}"
    return f"seg:{c.s}" if c.s >= 0 else f"cand:{idx}"


def resolve_single_spec_at_point(
    sp: str,
    pt: MetaPoint,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
    rule_desc: str,
) -> Optional[str]:
    upper = sp.upper()
    if upper in ("VANILLA", "PASS", "SKIP", "-"):
        return sp
    ctrl_alias = {
        "ENDR": "ENDR",
        "CANCEL": "ENDR",
        "STOP": "ENDR",
        "NO_BRANCH": "ENDR",
        "DENY": "ENDR",
        "RANDOM": "RANDOM",
        "TRIGGER": "RANDOM",
        "TAKE": "RANDOM",
        "PROCEED": "RANDOM",
        "ELSER": "ELSER",
    }
    if upper in ctrl_alias:
        canon = ctrl_alias[upper]
        want = {"RANDOM": FL_RANDOM, "ENDR": FL_ENDR, "ELSER": FL_ELSER}[canon]
        if any(c.weight > 0 and (c.fl & CTRL_KIND_MASK) == want for c in pt.cands):
            return canon
        return None
    if ":" in sp and "@" not in sp and "#" not in sp and not sp.startswith("seg:") and not sp.startswith("cand:"):
        tf_s, tn_s = sp.split(":", 1)
        tf, tn = int(tf_s), int(tn_s)
        if any(c.weight > 0 and c.tf == tf and c.tn == tn for c in pt.cands):
            return f"{tf}:{tn}"
        return None
    if sp.startswith("seg:") or sp.startswith("cand:"):
        return sp

    hits = match_move_specs_at_point(sp, pt, meta, grp)
    if not hits:
        return None
    if len(hits) >= 2:
        hstr = ", ".join(hits)
        first = hits[0]
        ex = f"{sp}@{first}" if (":" in first and not first.startswith("seg:") and "@" not in sp and "#" not in sp) else first
        raise AmbiguousMoveError(
            f"Ambiguous move '{sp}' at d={pt.depth} node={pt.node_idx} seg0={pt.seg0}: "
            f"matches multiple positive-weight candidates [{hstr}]. Disambiguate with '{ex}' or explicit target/seg:<n>.\n  Rule: {rule_desc}"
        )
    return hits[0]


def resolve_rhs_at_point(
    rhs_sem: str,
    pt: MetaPoint,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
    rule_desc: str,
) -> Optional[str]:
    out_steps: List[str] = []
    for step in rhs_sem.split(","):
        step = step.strip()
        out_choices: List[str] = []
        for choice in step.split("+"):
            choice = choice.strip()
            w_suf = ""
            if choice.endswith(")") and "(" in choice:
                lp = choice.rfind("(")
                w_suf = choice[lp:]
                choice = choice[:lp].strip()
            alts = [p.strip() for p in choice.split("|") if p.strip()]
            picked: Optional[str] = None
            for alt in alts:
                picked = resolve_single_spec_at_point(alt, pt, meta, grp, rule_desc)
                if picked is not None:
                    break
            if picked is None:
                return None
            out_choices.append(picked + w_suf)
        out_steps.append("+".join(out_choices))
    return ",".join(out_steps)


def format_seg0_ranges(matched_segs: List[int], all_node_segs: List[int]) -> List[str]:
    mset = set(matched_segs)
    if mset == set(all_node_segs):
        return ["*"]
    sorted_all = sorted(all_node_segs)
    out: List[str] = []
    i = 0
    while i < len(sorted_all):
        if sorted_all[i] not in mset:
            i += 1
            continue
        start = i
        while i + 1 < len(sorted_all) and sorted_all[i + 1] in mset:
            i += 1
        end = i
        if start == end:
            out.append(str(sorted_all[start]))
        elif end == len(sorted_all) - 1:
            out.append(f"{sorted_all[start]}+")
        else:
            out.append(f"{sorted_all[start]}..{sorted_all[end]}")
        i += 1
    return out


def coalesce_fallback_per_node(node_pts: List[Tuple[MetaPoint, str]]) -> List[Tuple[str, int]]:
    unique_rhs = list(dict.fromkeys(r for _, r in node_pts))
    if len(unique_rhs) == 2 and all(re.fullmatch(r"\d+:\d+", r) for r in unique_rhs):
        t1, t2 = unique_rhs[0], unique_rhs[1]
        p_t1 = [pt for pt, r in node_pts if r == t1]
        p_t2 = [pt for pt, r in node_pts if r == t2]
        tf1, tn1 = (int(x) for x in t1.split(":"))
        tf2, tn2 = (int(x) for x in t2.split(":"))
        t1_in_t2 = any(any(c.weight > 0 and c.tf == tf1 and c.tn == tn1 for c in pt.cands) for pt in p_t2)
        t2_in_t1 = any(any(c.weight > 0 and c.tf == tf2 and c.tn == tn2 for c in pt.cands) for pt in p_t1)
        if not t1_in_t2 and not t2_in_t1:
            return [(f"{t1}|{t2}", pt.seg0) for pt, _ in node_pts]
    return [(r, pt.seg0) for pt, r in node_pts]


def parse_positive_weight(raw: Any, context: str) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{context}: weight cannot be boolean {raw!r}")
    try:
        w = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}: invalid weight {raw!r}") from exc
    if not math.isfinite(w) or w <= 0.0:
        raise ValueError(f"{context}: weight must be finite and > 0 (got {raw!r})")
    return w


def _format_kv_atom(key: str, val: Any, strip_spaces: bool = False) -> str:
    if isinstance(val, bool):
        return f"{key}={'true' if val else 'false'}"
    sv = str(val).strip()
    if sv.startswith("in "):
        sv = sv[3:].strip()
        return f"{key}={sv.replace(' ', '') if strip_spaces else sv}"
    sv_cmp = sv.replace(" ", "")
    if sv_cmp.startswith("=="):
        return f"{key}={sv_cmp[2:]}"
    if sv_cmp.startswith("="):
        return f"{key}={sv_cmp[1:]}"
    if sv_cmp.startswith(("<=", ">=", "!=", "<", ">")):
        return f"{key}{sv_cmp}"
    return f"{key}={sv_cmp if strip_spaces else sv}"


def dict_to_selector_terms(d: Dict[str, Any]) -> List[str]:
    terms: List[str] = []
    for k, v in d.items():
        if k == "expr":
            terms.extend(split_selector_str(str(v)))
        elif k == "not_has":
            if isinstance(v, list):
                terms.extend(f"!has={x}" for x in v)
            else:
                terms.append(f"!has={v}")
        elif k == "not_in":
            if isinstance(v, list):
                terms.extend(f"!in={x}" for x in v)
            else:
                terms.append(f"!in={v}")
        elif k == "angle":
            if isinstance(v, list):
                if not v:
                    raise ValueError("Selector list for `angle` cannot be empty")
                parts = [re.sub(r"^(?:in\s+|<=|==|=)", "", str(x).strip()).strip() for x in v]
                sv = "|".join(parts)
            else:
                sv = re.sub(r"^(?:in\s+|<=|==|=)", "", str(v).strip()).strip()
            sv = re.sub(r"\s*\|\s*", "|", sv)
            terms.append(f"angle={sv}")
        elif k in ("dist", "dist3d"):
            if isinstance(v, list):
                if not v:
                    raise ValueError(f"Selector list for `{k}` cannot be empty")
                if len(v) == 1:
                    terms.append(_format_kv_atom(k, v[0], strip_spaces=True))
                else:
                    terms.append("(" + "|".join(_format_kv_atom(k, x, strip_spaces=True) for x in v) + ")")
            else:
                terms.append(_format_kv_atom(k, v, strip_spaces=True))
        elif k in ("node", "via", "parent"):
            if isinstance(v, list):
                if not v:
                    raise ValueError(f"Selector list for `{k}` cannot be empty")
                terms.append(f"{k}={','.join(str(x) for x in v)}")
            else:
                terms.append(f"{k}={v}")
        elif isinstance(v, list):
            if not v:
                raise ValueError(f"Selector list for `{k}` cannot be empty")
            if len(v) == 1:
                terms.append(_format_kv_atom(k, v[0]))
            else:
                terms.append("(" + "|".join(_format_kv_atom(k, x) for x in v) + ")")
        else:
            terms.append(_format_kv_atom(k, v))
    return terms


def do_spec_to_rhs_str(do_val: Any) -> str:
    def conv_step(step: Any) -> str:
        if isinstance(step, str):
            st_s = step.strip()
            if not st_s:
                raise ValueError("Empty step in `do`/`seq`")
            choices = [c.strip() for c in st_s.split("+")]
            if len(choices) > MAX_STEP_CHOICES:
                raise ValueError(
                    f"Rule step '{st_s}' has {len(choices)} choices > kMaxStepChoices={MAX_STEP_CHOICES}"
                )
            for ch in choices:
                if not ch:
                    raise ValueError(f"Empty choice in step '{st_s}'")
                if ch.endswith(")") and "(" in ch:
                    lp = ch.rfind("(")
                    parse_positive_weight(ch[lp + 1 : -1].strip(), f"Step choice '{ch}'")
            return st_s
        if isinstance(step, dict):
            if not step:
                raise ValueError("Empty weighted dict step in `do`/`seq`")
            if len(step) > MAX_STEP_CHOICES:
                raise ValueError(
                    f"Rule step dict has {len(step)} choices > kMaxStepChoices={MAX_STEP_CHOICES}"
                )
            return "+".join(
                f"{k}({toml_num(parse_positive_weight(w, f'Weighted choice {k!r}'))})"
                for k, w in step.items()
            )
        raise ValueError(f"Unsupported `do` step type: {step!r}")

    if isinstance(do_val, list):
        if not do_val:
            raise ValueError("`seq` list cannot be empty")
        steps = [conv_step(s) for s in do_val]
    else:
        steps = [conv_step(s) for s in str(do_val).split(",")] if isinstance(do_val, str) else [conv_step(do_val)]
    if len(steps) > MAX_RULE_STEPS:
        raise ValueError(f"Rule has {len(steps)} steps > kMaxRuleSteps={MAX_RULE_STEPS}")
    return ",".join(steps)


@dataclasses.dataclass
class ParsedPresetRule:
    idx: int
    sel_terms: List[str]
    rhs_sem: str
    tags: Set[str]
    tag_list: List[str]
    has_gate: bool
    short_lbl: str
    rule_desc: str
    extra_str: str
    group_id: Optional[int]
    warnings: List[str]
    action_kind: str = "do"
    action_specs: List[str] = dataclasses.field(default_factory=list)
    fallback: bool = False
    skip_if_all_banned: bool = False
    on_overflow: str = "trim"
    needs_rule_counter: bool = False
    prev_move_pairs: Tuple[Tuple[int, int], ...] = ()
    prev_move_items: List[str] = dataclasses.field(default_factory=list)
    prev_sel_terms: List[str] = dataclasses.field(default_factory=list)


ALLOWED_RULE_KEYS: Set[str] = {
    "d",
    "when",
    "in",
    "from",
    "followup",
    "sub_when",
    "do",
    "seq",
    "ban",
    "allow",
    "target",
    "fallback",
    "skip_if_all_banned",
    "on_overflow",
    "cond",
    "group",
    "stat",
    "tag",
    "tags",
    "label",
    "label_en",
    "after",
    "until",
    "every",
    "end",
    "scope",
    "when_gate",
    "prev_move",
    "prev_when",
    "prev",
}


def _extract_selector_block(
    obj: Dict[str, Any],
    idx: int,
    allow_sub_when_warning: bool = True,
) -> Tuple[List[str], List[str]]:
    """Extract normalized selector terms from a rule or `prev` context table."""
    terms: List[str] = []
    warns: List[str] = []
    if "followup" in obj:
        terms.append("d=1")
        f_move = obj["followup"]
        f_str = "|".join(str(x) for x in f_move) if isinstance(f_move, list) else str(f_move)
        terms.append(f"in={f_str}")
        if "from" in obj:
            fval = obj["from"]
            fterms = dict_to_selector_terms(fval) if isinstance(fval, dict) else split_selector_str(str(fval))
            terms.append("from=(" + " ".join(fterms) + ")")
        if "when" in obj:
            wval = obj["when"]
            terms.extend(dict_to_selector_terms(wval) if isinstance(wval, dict) else split_selector_str(str(wval)))
        if "sub_when" in obj:
            if allow_sub_when_warning:
                warns.append(
                    f"[WARNING] rule#{idx}: `sub_when` in followup rules is deprecated; put current-point conditions in `when` and caller conditions in `from`."
                )
            swval = obj["sub_when"]
            terms.extend(dict_to_selector_terms(swval) if isinstance(swval, dict) else split_selector_str(str(swval)))
    else:
        if "d" in obj:
            terms.append(f"d={int(obj['d'])}")
        elif "in" in obj or "from" in obj:
            terms.append("d=1")
        if "when" in obj:
            wval = obj["when"]
            if isinstance(wval, dict):
                terms.extend(dict_to_selector_terms(wval))
            else:
                terms.extend(split_selector_str(str(wval)))
        if "in" in obj:
            ival = obj["in"]
            istr = "|".join(str(x) for x in ival) if isinstance(ival, list) else str(ival)
            terms.append(f"in={istr}")
        if "from" in obj:
            fval = obj["from"]
            fterms = dict_to_selector_terms(fval) if isinstance(fval, dict) else split_selector_str(str(fval))
            terms.append("from=(" + " ".join(fterms) + ")")
    return terms, warns


def parse_preset_rule(
    rule: Dict[str, Any],
    idx: int,
    group_id_table: Dict[str, int],
    auto_group_ids: Dict[str, int],
    next_auto_group_box: List[int],
    meta: Optional[LoadedThkMeta] = None,
    grp: Optional[LoadedGroups] = None,
) -> ParsedPresetRule:
    """Parse a single `[[rules]]` entry into normalized selector terms, RHS/action, tags, and metadata."""
    if not isinstance(rule, dict):
        raise ValueError(f"Rule #{idx} must be a TOML table/dict, got {type(rule).__name__}")
    unknown_keys = sorted(set(rule.keys()) - ALLOWED_RULE_KEYS)
    if unknown_keys:
        raise ValueError(f"Rule #{idx}: unknown key(s) {unknown_keys}; allowed keys: {sorted(ALLOWED_RULE_KEYS)}")
    sel_terms, warnings = _extract_selector_block(rule, idx, allow_sub_when_warning=True)

    action_keys = [k for k in ("do", "seq", "ban", "allow", "target") if k in rule]
    if not action_keys:
        raise ValueError(f"Rule #{idx} missing action (`do`, `seq`, `ban`, or `allow`)")
    if len(action_keys) > 1:
        raise ValueError(f"Rule #{idx} cannot specify multiple action keys: {action_keys}")

    akey = action_keys[0]
    action_kind = "do"
    action_specs: List[str] = []
    if akey == "do":
        dval = rule["do"]
        if isinstance(dval, list) or (isinstance(dval, str) and "," in dval):
            raise ValueError(f"Rule #{idx}: use `seq = [...]` instead of `do` for multi-decision sequences")
        rhs_sem = do_spec_to_rhs_str(dval)
        action_kind = "do"
    elif akey in ("seq", "target"):
        rhs_sem = do_spec_to_rhs_str(rule[akey])
        action_kind = "seq" if akey == "seq" else "do"
    else:
        action_kind = akey
        raw_v = rule[akey]
        if isinstance(raw_v, list):
            action_specs = [str(x).strip() for x in raw_v if str(x).strip()]
        elif isinstance(raw_v, str):
            action_specs = [x.strip() for x in re.split(r"[|,]", raw_v) if x.strip()]
        else:
            raise ValueError(f"Rule #{idx}: `{akey}` must be a string or list of move/control names, got {type(raw_v).__name__}")
        if not action_specs:
            raise ValueError(f"Rule #{idx}: `{akey}` cannot be empty")
        rhs_sem = f"{action_kind}({','.join(action_specs)})"

    fallback_val = rule.get("fallback", False)
    if not isinstance(fallback_val, bool):
        raise ValueError(f"Rule #{idx}: `fallback` must be a boolean (`true`/`false`), got {fallback_val!r}")
    skip_if_all_banned_val = rule.get("skip_if_all_banned", False)
    if not isinstance(skip_if_all_banned_val, bool):
        raise ValueError(f"Rule #{idx}: `skip_if_all_banned` must be a boolean, got {skip_if_all_banned_val!r}")
    if skip_if_all_banned_val and action_kind != "ban":
        raise ValueError(f"Rule #{idx}: `skip_if_all_banned` is only valid with `ban`")
    if "on_overflow" in rule and action_kind != "ban":
        raise ValueError(f"Rule #{idx}: `on_overflow` is only valid with `ban`")
    on_overflow_val = str(rule.get("on_overflow", "trim")).strip().lower()
    if on_overflow_val not in ("trim", "error"):
        raise ValueError(f"Rule #{idx}: `on_overflow` must be 'trim' or 'error', got {on_overflow_val!r}")

    extra_parts: List[str] = []
    group_id: Optional[int] = None
    if "cond" in rule:
        group_id = int(rule["cond"])
        extra_parts.append(f"cond={group_id}")
    elif "group" in rule or "stat" in rule:
        gname = str(rule.get("group", rule.get("stat"))).strip()
        if gname in group_id_table:
            cid = group_id_table[gname]
        else:
            if gname not in auto_group_ids:
                auto_group_ids[gname] = next_auto_group_box[0]
                next_auto_group_box[0] += 1
            cid = auto_group_ids[gname]
        group_id = cid
        extra_parts.append(f"cond={cid}")
    tag_val = rule.get("tag", rule.get("tags"))
    cur_tags: Set[str] = set()
    tag_list: List[str] = []
    if tag_val:
        t_list = [str(x).strip() for x in tag_val] if isinstance(tag_val, list) else [x.strip() for x in str(tag_val).split(",")]
        t_list = [x for x in t_list if x]
        tag_list = t_list
        cur_tags = {x.lower() for x in t_list}
        if t_list:
            extra_parts.append(f"tag={','.join(t_list)}")
    if "label" in rule:
        extra_parts.append(f'label="{rule["label"]}"')
    if "label_en" in rule:
        extra_parts.append(f'label_en="{rule["label_en"]}"')
    for opt_k in ("after", "until", "every", "end", "scope"):
        if opt_k in rule:
            extra_parts.append(f"{opt_k}={rule[opt_k]}")
    if "when_gate" in rule:
        extra_parts.append(f"when={rule['when_gate']}")
    if "prev" in rule and ("prev_move" in rule or "prev_when" in rule):
        raise ValueError(f"Rule #{idx}: cannot combine `prev` with `prev_move` or `prev_when`")

    pm_raw: Any = None
    prev_sel_terms: List[str] = []
    if "prev" in rule:
        pv = rule["prev"]
        if isinstance(pv, (str, list)):
            pm_raw = pv
        elif isinstance(pv, dict):
            if not pv:
                raise ValueError(f"Rule #{idx}: `prev` table cannot be empty")
            pm_raw = pv.get("move")
            p_terms, p_warns = _extract_selector_block(pv, idx, allow_sub_when_warning=False)
            warnings.extend(p_warns)
            prev_sel_terms.extend(p_terms)
            flat_ctx = {
                k: v
                for k, v in pv.items()
                if k not in ("move", "when", "d", "in", "from", "followup", "sub_when")
            }
            if flat_ctx:
                prev_sel_terms.extend(dict_to_selector_terms(flat_ctx))
            if pm_raw is None and not prev_sel_terms:
                raise ValueError(f"Rule #{idx}: `prev` must specify `move` and/or at least one context condition")
        else:
            raise ValueError(f"Rule #{idx}: `prev` must be a string, list, or table, got {type(pv).__name__}")
    else:
        if "prev_move" in rule:
            pm_raw = rule["prev_move"]
        if "prev_when" in rule:
            pw = rule["prev_when"]
            if isinstance(pw, dict):
                pw_terms = dict_to_selector_terms(pw)
            elif isinstance(pw, str):
                pw_terms = split_selector_str(pw)
            else:
                raise ValueError(f"Rule #{idx}: `prev_when` must be a string or dict, got {type(pw).__name__}")
            if not pw_terms:
                raise ValueError(f"Rule #{idx}: `prev_when` cannot be empty")
            prev_sel_terms.extend(pw_terms)

    if prev_sel_terms and not any(t.startswith(("d=", "in=", "from=")) for t in prev_sel_terms):
        prev_sel_terms.insert(0, "d=0")

    prev_move_pairs: Tuple[Tuple[int, int], ...] = ()
    prev_move_items: List[str] = []
    if pm_raw is not None:
        if isinstance(pm_raw, list):
            pm_items = [str(x).strip() for x in pm_raw if str(x).strip()]
        elif isinstance(pm_raw, str):
            pm_items = [x.strip() for x in re.split(r"[|,]", pm_raw) if x.strip()]
        else:
            raise ValueError(f"Rule #{idx}: `prev_move` / `prev.move` must be a string or list, got {type(pm_raw).__name__}")
        if not pm_items:
            raise ValueError(f"Rule #{idx}: `prev_move` / `prev.move` cannot be empty")
        if meta is not None and grp is not None:
            pm_pairs, _ = expand_move_names_and_targets("|".join(pm_items), meta, grp)
        else:
            pm_pairs = set()
            for it in pm_items:
                if not re.fullmatch(r"\d+:\d+", it):
                    raise ValueError(
                        f"Rule #{idx}: `prev_move` entry {it!r} requires loaded meta/groups or literal file:node"
                    )
                tf_s, tn_s = it.split(":", 1)
                pm_pairs.add((int(tf_s), int(tn_s)))
        if not pm_pairs:
            raise ValueError(f"Rule #{idx}: `prev_move` matched 0 target nodes ({pm_items})")
        if len(pm_pairs) > MAX_FILTER_ITEMS:
            raise ValueError(
                f"Rule #{idx}: `prev_move` resolved to {len(pm_pairs)} targets > kMaxFilterItems={MAX_FILTER_ITEMS}"
            )
        prev_move_pairs = tuple(sorted(pm_pairs))
        prev_move_items = pm_items
        pm_str = ",".join(f"{tf}:{tn}" for tf, tn in prev_move_pairs)
        extra_parts.append(f"prev={pm_str}")
    extra_str = (" " + " ".join(extra_parts)) if extra_parts else ""
    short_lbl = str(rule.get("label_en", rule.get("label", rhs_sem)))
    rule_desc = f"rule#{idx} ({short_lbl})"
    has_gate = any(k in rule for k in ("after", "until", "every", "when_gate", "prev_move", "prev_when", "prev"))
    steps_cnt = len(rhs_sem.split(",")) if action_kind in ("do", "seq") else 1
    wg = str(rule.get("when_gate", "")).strip().lower()
    seq_sc = str(rule.get("scope", "fired")).strip().lower()
    seq_en = str(rule.get("end", "loop")).strip().lower()
    gate_is_rule = (wg in ("rule", "matched")) or (not wg and any(k in rule for k in ("after", "until", "every")))
    seq_is_stateful = (seq_sc in ("fired", "rule")) and (steps_cnt > 1 or seq_en in ("vanilla", "once"))
    return ParsedPresetRule(
        idx=idx,
        sel_terms=sel_terms,
        rhs_sem=rhs_sem,
        tags=cur_tags,
        tag_list=tag_list,
        has_gate=has_gate,
        short_lbl=short_lbl,
        rule_desc=rule_desc,
        extra_str=extra_str,
        group_id=group_id,
        warnings=warnings,
        action_kind=action_kind,
        action_specs=action_specs,
        fallback=fallback_val,
        skip_if_all_banned=skip_if_all_banned_val,
        on_overflow=on_overflow_val,
        needs_rule_counter=(gate_is_rule or seq_is_stateful),
        prev_move_pairs=prev_move_pairs,
        prev_move_items=prev_move_items,
        prev_sel_terms=prev_sel_terms,
    )


def resolve_action_at_point(
    pr: ParsedPresetRule,
    pt: MetaPoint,
    meta: LoadedThkMeta,
    grp: LoadedGroups,
    all_banned_out: Optional[List[str]] = None,
    overflow_out: Optional[List[str]] = None,
    multi_sub: bool = False,
    spec_hits_out: Optional[Dict[str, int]] = None,
) -> Optional[str]:
    """Resolve `do` / `seq` / `ban` / `allow` action at a single meta point."""
    if pr.action_kind in ("do", "seq"):
        res = resolve_rhs_at_point(pr.rhs_sem, pt, meta, grp, pr.rule_desc)
        if res is not None:
            steps_list = res.split(",")
            if len(steps_list) > MAX_RULE_STEPS:
                raise ValueError(
                    f"{pr.rule_desc} at d={pt.depth} n{pt.node_idx}.s{pt.seg0} has "
                    f"{len(steps_list)} steps > kMaxRuleSteps={MAX_RULE_STEPS}"
                )
            for st_idx, step_s in enumerate(steps_list, start=1):
                n_ch = len(step_s.split("+"))
                if n_ch > MAX_STEP_CHOICES:
                    raise ValueError(
                        f"{pr.rule_desc} step#{st_idx} at d={pt.depth} n{pt.node_idx}.s{pt.seg0} has "
                        f"{n_ch} choices > kMaxStepChoices={MAX_STEP_CHOICES}"
                    )
        return res

    pos = [i for i, c in enumerate(pt.cands) if c.weight > 0]
    if not pos:
        return None
    if spec_hits_out is not None:
        matched: Set[int] = set()
        for sp in pr.action_specs:
            hits_sp = match_cand_indices_at_point([sp], pt, meta, grp)
            if hits_sp:
                spec_hits_out[sp] = spec_hits_out.get(sp, 0) + 1
                matched |= hits_sp
    else:
        matched = match_cand_indices_at_point(pr.action_specs, pt, meta, grp)

    if pr.action_kind == "ban":
        if not matched:
            return None
        kept = [i for i in pos if i not in matched]
        pid_str = f"d{pt.depth}.f{pt.owner_tf}.n{pt.node_idx}.s{pt.seg0}" if (pt.depth == 1 and multi_sub) else f"d{pt.depth}.n{pt.node_idx}.s{pt.seg0}"
        if not kept:
            if all_banned_out is not None:
                all_banned_out.append(pid_str)
            return None
        if len(kept) > MAX_STEP_CHOICES:
            if pr.on_overflow == "error":
                raise ValueError(
                    f"{pr.rule_desc} `ban` left {len(kept)} > kMaxStepChoices={MAX_STEP_CHOICES} candidates at {pid_str}"
                )
            tot_rem_w = sum(pt.cands[i].weight for i in kept)
            top8 = sorted(kept, key=lambda i: (-pt.cands[i].weight, i))[:MAX_STEP_CHOICES]
            top8_set = set(top8)
            dropped = [i for i in kept if i not in top8_set]
            kept = [i for i in kept if i in top8_set]
            kept_w = sum(pt.cands[i].weight for i in kept)
            drop_desc = ", ".join(f"{cand_to_low_spec(i, pt)}({toml_num(pt.cands[i].weight)})" for i in dropped)
            if overflow_out is not None:
                overflow_out.append(
                    f"{pid_str}: kept {toml_num(kept_w)}/{toml_num(tot_rem_w)} weight, trimmed [{drop_desc}]"
                )
        if len(kept) == 1:
            return cand_to_low_spec(kept[0], pt)
        return "+".join(f"{cand_to_low_spec(i, pt)}({toml_num(pt.cands[i].weight)})" for i in kept)

    if pr.action_kind == "allow":
        kept = [i for i in pos if i in matched]
        if not kept:
            return None
        pid_str = f"d{pt.depth}.f{pt.owner_tf}.n{pt.node_idx}.s{pt.seg0}" if (pt.depth == 1 and multi_sub) else f"d{pt.depth}.n{pt.node_idx}.s{pt.seg0}"
        if len(kept) > MAX_STEP_CHOICES:
            raise ValueError(
                f"{pr.rule_desc} `allow` matched {len(kept)} > kMaxStepChoices={MAX_STEP_CHOICES} candidates at {pid_str}"
            )
        if len(kept) == 1:
            return cand_to_low_spec(kept[0], pt)
        return "+".join(f"{cand_to_low_spec(i, pt)}({toml_num(pt.cands[i].weight)})" for i in kept)

    raise ValueError(f"Unsupported action_kind: {pr.action_kind!r}")


def compile_preset_toml(
    preset_path: str,
    meta_path: Optional[str],
    groups_path: Optional[str],
    explain: bool = False,
) -> str:
    """Standalone TOML compiler: reads ONLY `.preset.toml`, `.thkmeta.toml`, `.groups.toml`."""
    preset_dir = os.path.dirname(os.path.abspath(preset_path))
    with open(preset_path, "rb") as f:
        doc = tomllib.load(f)

    if meta_path is None:
        m_ref = doc.get("meta")
        if not m_ref:
            raise ValueError("Preset missing `meta` key and --meta not specified")
        meta_path = os.path.join(preset_dir, m_ref if str(m_ref).endswith(".toml") else f"{m_ref}.thkmeta.toml")
    if groups_path is None and doc.get("groups"):
        g_ref = doc["groups"]
        groups_path = os.path.join(preset_dir, g_ref if str(g_ref).endswith(".toml") else f"{g_ref}.groups.toml")

    meta = load_thkmeta_toml(meta_path)
    grp = load_groups_toml(
        groups_path,
        meta.moves,
        subnode_moves=meta.subnode_moves,
        inline_actions=meta.inline_actions,
    )
    d0_files_str = meta.default_files
    if "files" in doc:
        fv = doc["files"]
        d0_files_str = ",".join(str(x) for x in fv) if isinstance(fv, list) else str(fv)
    group_id_table: Dict[str, int] = {
        str(k): int(v) for k, v in doc.get("group_ids", doc.get("stats", {})).items()
    }
    auto_group_ids: Dict[str, int] = {}
    next_auto_group_box = [2100]

    d0_by_node: Dict[int, List[MetaPoint]] = defaultdict(list)
    for pt in meta.d0_points:
        d0_by_node[pt.node_idx].append(pt)

    header_lines: List[str] = [
        "# ============================================================================",
        "# Auto-Compiled Low-Level THK Preset (standalone from TOML meta + groups)",
        "# Compatible directly with `rng_thk.dll` (`thk_preset_io.h`).",
        "# ============================================================================",
    ]
    if "name" in doc:
        header_lines.append(f"name = {doc['name']}")
    if "desc" in doc:
        header_lines.append(f"desc = {doc['desc']}")
    if "desc_en" in doc:
        header_lines.append(f"desc_en = {doc['desc_en']}")
    if "disable_tag" in doc:
        dt = doc["disable_tag"]
        dt_str = ",".join(str(x) for x in dt) if isinstance(dt, list) else str(dt)
        header_lines.append(f"disable_tag = {dt_str}")
    header_lines.append("")

    ctx_registry: Dict[Tuple[str, ...], str] = {}
    ctx_lines: List[str] = []
    body_lines: List[str] = []

    def _rhs_allows_any_move(rhs_str: str, pt: MetaPoint, move_set: Set[Tuple[int, int]]) -> bool:
        for step_s in rhs_str.split(","):
            for ch_s in step_s.split("+"):
                for alt_s in ch_s.split("|"):
                    tok = alt_s.strip()
                    if tok.endswith(")") and "(" in tok:
                        tok = tok[: tok.rfind("(")].strip()
                    tok_u = tok.upper()
                    if tok_u in ("VANILLA", "PASS", "SKIP", "-"):
                        return True
                    if tok_u.startswith("SEG:"):
                        ws = int(tok_u[4:])
                        for c in pt.cands:
                            if c.weight > 0 and c.s == ws and (c.tf, c.tn) in move_set:
                                return True
                    elif tok_u.startswith("CAND:"):
                        ci = int(tok_u[5:])
                        if 0 <= ci < len(pt.cands):
                            c = pt.cands[ci]
                            if c.weight > 0 and (c.tf, c.tn) in move_set:
                                return True
                    elif ":" in tok_u:
                        tf_s, tn_s = tok_u.split(":", 1)
                        if tf_s.isdigit() and tn_s.isdigit() and (int(tf_s), int(tn_s)) in move_set:
                            return True
        return False

    multi_sub = len(meta.sub_files) > 1

    # Track prior ungated rule coverages for first-match shadowing & `fallback = true` cascade
    d0_cov: Dict[Tuple[int, int], List[Tuple[int, str, Set[str]]]] = defaultdict(list)
    d1_cov: Dict[Tuple[int, int, int], List[Tuple[int, str, Set[str], Set[int]]]] = defaultdict(list)
    d0_uncond_winners: Dict[Tuple[int, int], List[Tuple[Set[str], str]]] = defaultdict(list)
    d1_uncond_winners: Dict[Tuple[int, int, int, int], List[Tuple[Set[str], str]]] = defaultdict(list)
    prev_reach_audit: List[Tuple[ParsedPresetRule, Optional[List[MetaPoint]], Optional[List[Tuple[MetaPoint, List[int]]]]]] = []

    def _d0_blockers(nidx: int, seg0: int, tags: Set[str]) -> List[str]:
        return [f"rule#{pi}({pl})" for pi, pl, ptags in d0_cov.get((nidx, seg0), ()) if ptags <= tags]

    def _d1_covered_and_blockers(
        tf: int, nidx: int, seg0: int, callers_col: Sequence[int], tags: Set[str]
    ) -> Tuple[Set[int], List[str]]:
        cset = set(callers_col)
        cov_c: Set[int] = set()
        blockers_here: List[str] = []
        for pi, pl, ptags, pcallers in d1_cov.get((tf, nidx, seg0), ()):
            if ptags <= tags and (pcallers & cset):
                cov_c |= (pcallers & cset)
                b = f"rule#{pi}({pl})"
                if b not in blockers_here:
                    blockers_here.append(b)
        return cov_c, blockers_here

    stateful_rule_lines = 0
    for idx, rule in enumerate(doc.get("rules", []), start=1):
        parsed = parse_preset_rule(
            rule, idx, group_id_table, auto_group_ids, next_auto_group_box, meta=meta, grp=grp
        )
        for w_msg in parsed.warnings:
            print(w_msg, file=sys.stderr)
        sel_terms = parsed.sel_terms
        rhs_sem = parsed.rhs_sem
        cur_tags = parsed.tags
        extra_str = parsed.extra_str
        short_lbl = parsed.short_lbl
        rule_desc = parsed.rule_desc
        has_gate = parsed.has_gate

        prev_narrowed_d0: Optional[List[MetaPoint]] = None
        prev_narrowed_d1: Optional[List[Tuple[MetaPoint, List[int]]]] = None
        if parsed.prev_sel_terms:
            prev_raw_d0: List[MetaPoint] = []
            prev_raw_d1: List[Tuple[MetaPoint, List[int]]] = []
            for ppt in meta.d0_points:
                p_ok, _ = eval_rule_selectors(parsed.prev_sel_terms, ppt, meta, grp, d0_by_node)
                if p_ok:
                    prev_raw_d0.append(ppt)
            for ppt in meta.d1_points:
                p_ok, p_callers = eval_rule_selectors(parsed.prev_sel_terms, ppt, meta, grp, d0_by_node)
                if p_ok and p_callers:
                    prev_raw_d1.append((ppt, sorted(p_callers)))
            if not prev_raw_d0 and not prev_raw_d1:
                raise ValueError(
                    f"{rule_desc}: `prev` context matched 0 THK points! selectors={parsed.prev_sel_terms}"
                )
            if parsed.prev_move_pairs:
                pm_set = set(parsed.prev_move_pairs)
                prev_narrowed_d0 = [
                    ppt for ppt in prev_raw_d0 if any(c.weight > 0 and (c.tf, c.tn) in pm_set for c in ppt.cands)
                ]
                prev_narrowed_d1 = [
                    (ppt, pcs)
                    for ppt, pcs in prev_raw_d1
                    if any(c.weight > 0 and (c.tf, c.tn) in pm_set for c in ppt.cands)
                ]
                if not prev_narrowed_d0 and not prev_narrowed_d1:
                    raise ValueError(
                        f"{rule_desc}: `prev` move {parsed.prev_move_items} not found in any of the "
                        f"{len(prev_raw_d0) + len(prev_raw_d1)} matched prev-context points"
                    )
            else:
                prev_narrowed_d0 = prev_raw_d0
                prev_narrowed_d1 = prev_raw_d1

            # Compress narrowed prev-context points into canonical slot dicts
            slot_dicts: List[Dict[str, str]] = []
            if prev_narrowed_d0:
                p_segs_by_node: Dict[int, List[int]] = defaultdict(list)
                for ppt in prev_narrowed_d0:
                    p_segs_by_node[ppt.node_idx].append(ppt.seg0)
                p_nodes_by_seg: Dict[str, List[int]] = defaultdict(list)
                for pnidx, ps_list in sorted(p_segs_by_node.items()):
                    for s_ex in format_seg0_ranges(ps_list, meta.node_segs_d0[pnidx]):
                        p_nodes_by_seg[s_ex].append(pnidx)
                for s_ex, nlist in sorted(p_nodes_by_seg.items()):
                    n_sorted = sorted(nlist)
                    for ci in range(0, len(n_sorted), MAX_FILTER_ITEMS):
                        chunk = n_sorted[ci : ci + MAX_FILTER_ITEMS]
                        slot_dicts.append(
                            {
                                "d": "0",
                                "f": d0_files_str,
                                "node": ",".join(str(x) for x in chunk),
                                "seg0": s_ex,
                            }
                        )
            if prev_narrowed_d1:
                p_d1_by_fnc: Dict[Tuple[int, int, Tuple[int, ...]], List[int]] = defaultdict(list)
                for ppt, pcs in prev_narrowed_d1:
                    p_d1_by_fnc[(ppt.owner_tf, ppt.node_idx, tuple(sorted(pcs)))].append(ppt.seg0)
                for (ptf, pnidx, pcs_t), ps_list in sorted(p_d1_by_fnc.items()):
                    for s_ex in format_seg0_ranges(ps_list, meta.node_segs_d1[(ptf, pnidx)]):
                        for ci in range(0, len(pcs_t), MAX_FILTER_ITEMS):
                            cchunk = pcs_t[ci : ci + MAX_FILTER_ITEMS]
                            slot_dicts.append(
                                {
                                    "d": "1",
                                    "f": str(ptf),
                                    "node": str(pnidx),
                                    "caller": ",".join(str(x) for x in cchunk),
                                    "seg0": s_ex,
                                }
                            )

            if len(slot_dicts) == 1:
                sd = slot_dicts[0]
                inl = [f"prev_d={sd['d']}", f"prev_f={sd['f']}", f"prev_node={sd['node']}"]
                if "caller" in sd:
                    inl.append(f"prev_caller={sd['caller']}")
                inl.append(f"prev_seg0={sd['seg0']}")
                extra_str = (extra_str + " " + " ".join(inl)) if extra_str else (" " + " ".join(inl))
            else:
                slot_bodies: List[str] = []
                for sd in slot_dicts:
                    parts = [f"mon={meta.mon}", f"d={sd['d']}", f"f={sd['f']}", f"node={sd['node']}"]
                    if "caller" in sd:
                        parts.append(f"caller={sd['caller']}")
                    parts.append(f"seg0={sd['seg0']}")
                    slot_bodies.append(" ".join(parts))
                sig = tuple(slot_bodies)
                if sig in ctx_registry:
                    ctx_name = ctx_registry[sig]
                else:
                    ctx_name = f"prev_ctx_{len(ctx_registry) + 1}"
                    ctx_registry[sig] = ctx_name
                    for sb in slot_bodies:
                        ctx_lines.append(f"ctx {ctx_name} {sb}")
                extra_str = (extra_str + f" prev_ctx={ctx_name}") if extra_str else f" prev_ctx={ctx_name}"

        if parsed.prev_move_pairs:
            prev_reach_audit.append((parsed, prev_narrowed_d0, prev_narrowed_d1))

        matched_d0_by_node: Dict[int, List[Tuple[MetaPoint, str]]] = defaultdict(list)
        matched_d1: List[Tuple[int, int, int, Tuple[int, ...], str]] = []
        all_banned_pts: List[str] = []
        overflow_notes: List[str] = []
        raw_hits = 0
        selector_hits = 0
        kept_selector_hits = 0
        noop_hits = 0
        kept_noop_hits = 0
        track_specs = parsed.action_kind in ("ban", "allow")
        spec_hits: Optional[Dict[str, int]] = ({sp: 0 for sp in parsed.action_specs} if track_specs else None)
        kept_d0_evals: List[Tuple[MetaPoint, bool]] = []

        for pt in meta.d0_points:
            ok, _ = eval_rule_selectors(sel_terms, pt, meta, grp, d0_by_node)
            if not ok:
                continue
            selector_hits += 1
            covered_here = parsed.fallback and bool(_d0_blockers(pt.node_idx, pt.seg0, cur_tags))
            if not covered_here:
                kept_selector_hits += 1
            prev_all_banned = len(all_banned_pts)
            res_rhs = resolve_action_at_point(
                parsed,
                pt,
                meta,
                grp,
                all_banned_out=None if covered_here else all_banned_pts,
                overflow_out=None if covered_here else overflow_notes,
                multi_sub=multi_sub,
                spec_hits_out=spec_hits,
            )
            if res_rhs is not None:
                raw_hits += 1
                if not covered_here:
                    matched_d0_by_node[pt.node_idx].append((pt, res_rhs))
                    kept_d0_evals.append((pt, True))
            else:
                if len(all_banned_pts) == prev_all_banned:
                    noop_hits += 1
                    if not covered_here:
                        kept_noop_hits += 1
                        kept_d0_evals.append((pt, False))

        for pt in meta.d1_points:
            ok, callers = eval_rule_selectors(sel_terms, pt, meta, grp, d0_by_node)
            if not (ok and callers):
                continue
            selector_hits += 1
            cov_c, _ = _d1_covered_and_blockers(pt.owner_tf, pt.node_idx, pt.seg0, callers, cur_tags)
            rem_callers = [c for c in callers if c not in cov_c] if parsed.fallback else list(callers)
            need_warnings = (not parsed.fallback) or bool(rem_callers)
            if rem_callers:
                kept_selector_hits += 1
            prev_all_banned = len(all_banned_pts)
            res_rhs = resolve_action_at_point(
                parsed,
                pt,
                meta,
                grp,
                all_banned_out=all_banned_pts if need_warnings else None,
                overflow_out=overflow_notes if need_warnings else None,
                multi_sub=multi_sub,
                spec_hits_out=spec_hits,
            )
            if res_rhs is not None:
                raw_hits += 1
                if rem_callers:
                    matched_d1.append((pt.owner_tf, pt.node_idx, pt.seg0, tuple(sorted(rem_callers)), res_rhs))
            else:
                if len(all_banned_pts) == prev_all_banned:
                    noop_hits += 1
                    if rem_callers:
                        kept_noop_hits += 1

        if raw_hits == 0:
            if all_banned_pts:
                if not parsed.skip_if_all_banned:
                    print(
                        f"[WARNING] {rule_desc} (`ban`) eliminated ALL positive-weight candidates at {len(all_banned_pts)} pt(s): {', '.join(all_banned_pts[:8])}"
                        + (" ..." if len(all_banned_pts) > 8 else ""),
                        file=sys.stderr,
                    )
                continue
            raise ValueError(f"{rule_desc} matched 0 THK points! selectors={sel_terms}")

        if all_banned_pts and not parsed.skip_if_all_banned:
            print(
                f"[WARNING] {rule_desc} (`ban`) skipped {len(all_banned_pts)} all-banned solo pt(s) (left vanilla): {', '.join(all_banned_pts[:8])}"
                + (" ..." if len(all_banned_pts) > 8 else ""),
                file=sys.stderr,
            )

        if overflow_notes:
            print(
                f"[WARNING] {rule_desc} (`ban`) trimmed >{MAX_STEP_CHOICES} candidates at {len(overflow_notes)} pt(s): {overflow_notes[0]}"
                + (f" (+{len(overflow_notes) - 1} more)" if len(overflow_notes) > 1 else ""),
                file=sys.stderr,
            )

        kept_hits = sum(len(v) for v in matched_d0_by_node.values()) + len(matched_d1)
        if spec_hits is not None and raw_hits > 0:
            unused_specs = [sp for sp in parsed.action_specs if spec_hits.get(sp, 0) == 0]
            if unused_specs:
                print(
                    f"[WARNING] {rule_desc} (`{parsed.action_kind}`) spec(s) {unused_specs} matched 0 candidates "
                    f"across all {selector_hits} selector-matched pts.",
                    file=sys.stderr,
                )

        emitted_branch_warn = False
        if parsed.action_kind == "ban" and raw_hits > 0 and isinstance(rule.get("when"), dict) and kept_d0_evals:
            for wk, wv in rule["when"].items():
                if isinstance(wv, list) and len(wv) >= 2 and wk not in ("node", "via", "parent", "not_has", "not_in", "expr"):
                    for bv in wv:
                        if wk == "angle":
                            sv = re.sub(r"^(?:in\s+|<=|==|=)", "", str(bv).strip()).strip()
                            sv = re.sub(r"\s*\|\s*", "|", sv)
                            bterm = f"angle={sv}"
                        else:
                            bterm = _format_kv_atom(wk, bv, strip_spaces=(wk in ("dist", "dist3d")))
                        b_sel = 0
                        b_acted = 0
                        for bpt, b_ok in kept_d0_evals:
                            m_b, _ = eval_rule_selectors([bterm], bpt, meta, grp, d0_by_node)
                            if m_b:
                                b_sel += 1
                                if b_ok:
                                    b_acted += 1
                        if b_sel >= 4 and (b_acted / b_sel) < 0.20:
                            emitted_branch_warn = True
                            print(
                                f"[WARNING] {rule_desc} (`ban`) branch `{wk}={bv}` acted on only "
                                f"{b_acted}/{b_sel} remaining pts ({b_sel - b_acted} noop left vanilla).",
                                file=sys.stderr,
                            )

        if (
            parsed.action_kind == "ban"
            and raw_hits > 0
            and not emitted_branch_warn
            and kept_selector_hits >= 4
            and kept_hits > 0
            and (kept_hits / kept_selector_hits) < 0.25
        ):
            print(
                f"[WARNING] {rule_desc} (`ban`) acted on only {kept_hits}/{kept_selector_hits} "
                f"remaining pts ({kept_noop_hits} noop left vanilla).",
                file=sys.stderr,
            )

        if parsed.fallback and kept_hits == 0:
            print(
                f"[WARNING] {rule_desc} (fallback) had 0 remaining uncovered points after earlier rules (raw matched {raw_hits} pts); emitted 0 lines.",
                file=sys.stderr,
            )
            continue

        # First-match shadowing check & coverage tracking
        total_matched_pts = 0
        shadowed_pts = 0
        shadow_sources: List[str] = []
        for nidx, pairs in matched_d0_by_node.items():
            for pt, rhs_val in pairs:
                total_matched_pts += 1
                if not parsed.fallback:
                    blockers = _d0_blockers(nidx, pt.seg0, cur_tags)
                    if blockers:
                        shadowed_pts += 1
                        for b in blockers:
                            if b not in shadow_sources:
                                shadow_sources.append(b)
                if not has_gate:
                    d0_cov[(nidx, pt.seg0)].append((idx, short_lbl, set(cur_tags)))
                    d0_uncond_winners[(nidx, pt.seg0)].append((set(cur_tags), rhs_val))
        for tf, nidx, s0, callers_tup, rhs_val in matched_d1:
            total_matched_pts += 1
            if not parsed.fallback:
                covered_callers, blockers_here = _d1_covered_and_blockers(tf, nidx, s0, callers_tup, cur_tags)
                if set(callers_tup) <= covered_callers and blockers_here:
                    shadowed_pts += 1
                    for b in blockers_here:
                        if b not in shadow_sources:
                            shadow_sources.append(b)
            if not has_gate:
                d1_cov[(tf, nidx, s0)].append((idx, short_lbl, set(cur_tags), set(callers_tup)))
                for cid in callers_tup:
                    d1_uncond_winners[(tf, nidx, s0, cid)].append((set(cur_tags), rhs_val))

        if not parsed.fallback:
            if shadowed_pts == total_matched_pts and total_matched_pts > 0:
                print(
                    f"[WARNING] {rule_desc} is completely shadowed ({shadowed_pts}/{total_matched_pts} pts) by earlier {', '.join(shadow_sources)}",
                    file=sys.stderr,
                )
            elif shadowed_pts > 0:
                print(
                    f"[WARNING] {rule_desc} is partially shadowed ({shadowed_pts}/{total_matched_pts} pts) by earlier {', '.join(shadow_sources)}",
                    file=sys.stderr,
                )

        if explain:
            d0_cnt = sum(len(v) for v in matched_d0_by_node.values())
            d1_cnt = len(matched_d1)
            resolved_targets: List[str] = []
            for _n, pairs in sorted(matched_d0_by_node.items()):
                for _pt, r_s in pairs:
                    if r_s not in resolved_targets:
                        resolved_targets.append(r_s)
            for _tf, _n, _s, _c, r_s in matched_d1:
                if r_s not in resolved_targets:
                    resolved_targets.append(r_s)
            pool_notes: List[str] = []
            if parsed.action_kind in ("do", "seq") and (
                rhs_sem in meta.moves or rhs_sem in grp.move_groups or rhs_sem in meta.subnode_moves
            ):
                t_pool, _ = expand_move_names_and_targets(rhs_sem, meta, grp)
                if len(t_pool) >= 2:
                    p_str = ",".join(f"{a}:{b}" for a, b in sorted(t_pool))
                    r_str_note = ",".join(resolved_targets)
                    pool_notes.append(f"{rhs_sem} [{p_str}] => {r_str_note}")
            elif ("followup" in rule and str(rule["followup"]) in (set(meta.moves) | set(meta.subnode_moves))) or (
                "in" in rule and str(rule["in"]) in (set(meta.moves) | set(meta.subnode_moves))
            ):
                in_m = str(rule.get("followup", rule.get("in")))
                t_pool, _ = expand_move_names_and_targets(in_m, meta, grp)
                if len(t_pool) >= 2:
                    p_str = ",".join(f"{a}:{b}" for a, b in sorted(t_pool))
                    owners = ",".join(
                        sorted(
                            {
                                f"{pt.owner_tf}:{pt.owner_tn}"
                                for pt in meta.d1_points
                                if any(
                                    pt.owner_tf == tf and pt.node_idx == n and pt.seg0 == s
                                    for tf, n, s, _, _ in matched_d1
                                )
                            }
                        )
                    )
                    tag_k = "followup" if "followup" in rule else "in"
                    pool_notes.append(f"{tag_k}={in_m} [{p_str}] => {owners}")
            fb_note = f" fallback({kept_hits}/{raw_hits})" if parsed.fallback else ""
            if parsed.action_kind in ("ban", "allow"):
                act_note = f" acted={raw_hits}/{selector_hits}" + (f",noop={noop_hits}" if noop_hits > 0 else "")
            else:
                act_note = ""
            prev_note = ""
            if parsed.prev_sel_terms or parsed.prev_move_items:
                p_bits: List[str] = []
                if parsed.prev_move_items:
                    p_bits.append("move=" + "|".join(parsed.prev_move_items))
                if parsed.prev_sel_terms:
                    p_bits.append(" ".join(parsed.prev_sel_terms))
                prev_note = f" prev=[{' '.join(p_bits)}]"
            pt_summary = (f"{d0_cnt} d=0 pts" if d0_cnt else f"{d1_cnt} d=1 pts") + act_note
            note_suf = (" | " + "; ".join(pool_notes)) if pool_notes else f" -> {','.join(resolved_targets)}"
            body_lines.append(
                f"# [#{idx} {rule.get('label_en', rule.get('label', ''))}] "
                f"sel=[{' '.join(sel_terms)}]{prev_note}{fb_note} {parsed.action_kind}={rhs_sem} | {pt_summary}{note_suf}"
            )

        by_rhs_seg0: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for node_idx in sorted(matched_d0_by_node.keys()):
            pairs = coalesce_fallback_per_node(matched_d0_by_node[node_idx])
            by_rhs_seg: Dict[str, List[int]] = defaultdict(list)
            for r_str, s0 in pairs:
                by_rhs_seg[r_str].append(s0)
            for r_str, s_list in by_rhs_seg.items():
                for s_expr in format_seg0_ranges(s_list, meta.node_segs_d0[node_idx]):
                    by_rhs_seg0[(r_str, s_expr)].append(node_idx)

        rule_lines_start = len(body_lines)
        for (r_str, s_expr), nlist in by_rhs_seg0.items():
            nlist_sorted = sorted(nlist)
            for ci in range(0, len(nlist_sorted), MAX_FILTER_ITEMS):
                chunk = nlist_sorted[ci : ci + MAX_FILTER_ITEMS]
                nstr = ",".join(str(x) for x in chunk)
                body_lines.append(
                    f"rule mon={meta.mon} d=0 f={d0_files_str} node={nstr} seg0={s_expr} -> {r_str}{extra_str}"
                )

        by_sub_node_callers_rhs: Dict[Tuple[int, int, Tuple[int, ...], str], List[int]] = defaultdict(list)
        for tf, node_idx, seg0, callers, r_str in matched_d1:
            by_sub_node_callers_rhs[(tf, node_idx, callers, r_str)].append(seg0)
        for (tf, node_idx, callers, r_str), segs in sorted(by_sub_node_callers_rhs.items()):
            for s_expr in format_seg0_ranges(segs, meta.node_segs_d1[(tf, node_idx)]):
                for ci in range(0, len(callers), MAX_FILTER_ITEMS):
                    cchunk = callers[ci : ci + MAX_FILTER_ITEMS]
                    cstr = ",".join(str(x) for x in cchunk)
                    body_lines.append(
                        f"rule mon={meta.mon} d=1 f={tf} node={node_idx} caller={cstr} seg0={s_expr} -> {r_str}{extra_str}"
                    )
        emitted_here = sum(1 for ln in body_lines[rule_lines_start:] if ln.startswith("rule "))
        if parsed.needs_rule_counter:
            stateful_rule_lines += emitted_here

    for p_rule, p_d0_pts, p_d1_pts in prev_reach_audit:
        pm_set = set(p_rule.prev_move_pairs)
        if p_d0_pts is not None or p_d1_pts is not None:
            src_d0 = p_d0_pts or []
            src_d1 = p_d1_pts or []
        else:
            src_d0 = [
                pt for pt in meta.d0_points if any(c.weight > 0 and (c.tf, c.tn) in pm_set for c in pt.cands)
            ]
            src_d1 = [
                (pt, list(pt.callers))
                for pt in meta.d1_points
                if any(c.weight > 0 and (c.tf, c.tn) in pm_set for c in pt.cands)
            ]
        total_src = len(src_d0) + sum(len(cs) for _, cs in src_d1)
        if total_src == 0:
            continue
        any_reachable = False
        for pt in src_d0:
            winners = [w_rhs for w_tags, w_rhs in d0_uncond_winners.get((pt.node_idx, pt.seg0), ()) if w_tags <= p_rule.tags]
            if not winners:
                any_reachable = True
                break
            if _rhs_allows_any_move(winners[0], pt, pm_set):
                any_reachable = True
                break
        if not any_reachable:
            for pt, cs in src_d1:
                for cid in cs:
                    winners = [
                        w_rhs
                        for w_tags, w_rhs in d1_uncond_winners.get((pt.owner_tf, pt.node_idx, pt.seg0, cid), ())
                        if w_tags <= p_rule.tags
                    ]
                    if not winners or _rhs_allows_any_move(winners[0], pt, pm_set):
                        any_reachable = True
                        break
                if any_reachable:
                    break
        if not any_reachable:
            print(
                f"[WARNING] {p_rule.rule_desc}: `prev` move {p_rule.prev_move_items} cannot occur at any of its "
                f"{total_src} matched prev-context point(s) because unconditional rules override all of them.",
                file=sys.stderr,
            )

    if stateful_rule_lines > MAX_RULE_COUNTERS:
        print(
            f"[WARNING] Compiled {stateful_rule_lines} stateful low-level rules exceeding kMaxRuleCounters={MAX_RULE_COUNTERS}; "
            f"rules past the first {MAX_RULE_COUNTERS} stateful rules per monster instance cannot persist per-rule counters.",
            file=sys.stderr,
        )

    ctx_block = (ctx_lines + [""]) if ctx_lines else []
    return "\n".join(header_lines + ctx_block + body_lines) + "\n"


# =============================================================================
# Faithful Generic `rng_thk.dll` Verifier
# =============================================================================


@dataclasses.dataclass
class CompiledContextSlot:
    mon: int = -1
    depth: int = -1
    files: List[int] = dataclasses.field(default_factory=list)
    nodes: List[int] = dataclasses.field(default_factory=list)
    seg0_min: int = -1
    seg0_max: int = -1
    callers: List[int] = dataclasses.field(default_factory=list)
    moves: Tuple[Tuple[int, int], ...] = ()


@dataclasses.dataclass
class CompiledRule:
    mon: int
    depth: int
    files: List[int]
    nodes: List[int]
    seg0_min: int
    seg0_max: int
    callers: List[int]
    steps: List[List[Tuple[List[str], float]]]
    cond_id: int
    tags: Set[str]
    line: str
    gate_sig: Tuple[str, int, int, str] = ("always", 0, 0, "")
    seq_end: str = "loop"
    seq_scope: str = "fired"
    prev_moves: Tuple[Tuple[int, int], ...] = ()
    prev_depth: int = -1
    prev_files: List[int] = dataclasses.field(default_factory=list)
    prev_nodes: List[int] = dataclasses.field(default_factory=list)
    prev_seg0_min: int = -1
    prev_seg0_max: int = -1
    prev_callers: List[int] = dataclasses.field(default_factory=list)
    prev_ctx_name: str = ""
    prev_ctx_slots: Tuple[CompiledContextSlot, ...] = ()


def _parse_seg0_bounds(vl: str) -> Tuple[int, int]:
    if vl in ("*", "any"):
        return -1, -1
    if ".." in vl:
        a, b = vl.split("..", 1)
        return (int(a) if a else -1), (int(b) if b else -1)
    if vl.endswith("+"):
        return int(vl[:-1]), -1
    v = int(vl)
    return v, v


def _parse_move_pairs_list(vl: str, ctx_lbl: str) -> Tuple[Tuple[int, int], ...]:
    pm_list: List[Tuple[int, int]] = []
    for item in vl.split(","):
        item = item.strip()
        if not item or item in ("*", "any"):
            continue
        tf_s, tn_s = item.split(":", 1)
        pm_list.append((int(tf_s), int(tn_s)))
    if len(pm_list) > MAX_FILTER_ITEMS:
        raise ValueError(f"{ctx_lbl} exceeds kMaxFilterItems={MAX_FILTER_ITEMS}")
    return tuple(sorted(set(pm_list)))


def parse_low_preset(path: str) -> Tuple[Set[str], List[CompiledRule]]:
    disabled: Set[str] = set()
    contexts: Dict[str, List[CompiledContextSlot]] = {}
    rules: List[CompiledRule] = []
    next_id = 10001

    def tok_line(line: str) -> List[str]:
        toks = []
        i = 0
        n = len(line)
        while i < n:
            while i < n and line[i] in " \t":
                i += 1
            if i >= n or line[i] == "#":
                break
            in_q = False
            b = []
            while i < n:
                c = line[i]
                if c == '"':
                    in_q = not in_q
                    i += 1
                    continue
                if not in_q and c in " \t#":
                    break
                b.append(c)
                i += 1
            if b:
                toks.append("".join(b))
        return toks

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("#") or s.startswith(";"):
                continue
            low_s = s.lower()
            if (
                not (low_s.startswith("rule ") or low_s.startswith("rule\t"))
                and not (low_s.startswith("ctx ") or low_s.startswith("ctx\t"))
                and "=" in s
            ):
                k, v = [x.strip() for x in s.split("=", 1)]
                if k.lower() in ("disable_tag", "disable_tags"):
                    disabled.update(x.strip().lower() for x in v.split(",") if x.strip())
                continue
            toks = tok_line(s)
            if not toks:
                continue
            head_l = toks[0].lower()
            if head_l == "ctx":
                if len(toks) < 3 or "=" in toks[1]:
                    raise ValueError(f"Invalid ctx directive: {s}")
                cname = toks[1].strip().lower()
                slot = CompiledContextSlot()
                for tok in toks[2:]:
                    if "=" not in tok:
                        raise ValueError(f"Invalid ctx token {tok!r} in: {s}")
                    k, v = tok.split("=", 1)
                    kl, vl = k.lower(), v.lower()
                    if kl in ("mon", "monster"):
                        slot.mon = -1 if vl in ("*", "any") else int(vl)
                    elif kl in ("d", "depth"):
                        slot.depth = -1 if vl in ("*", "any") else int(vl)
                    elif kl in ("f", "file"):
                        vals = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                        if len(vals) > MAX_FILTER_ITEMS:
                            raise ValueError(f"ctx file exceeds kMaxFilterItems={MAX_FILTER_ITEMS}: {s}")
                        slot.files = vals
                    elif kl in ("node", "nodes"):
                        vals = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                        if len(vals) > MAX_FILTER_ITEMS:
                            raise ValueError(f"ctx node exceeds kMaxFilterItems={MAX_FILTER_ITEMS}: {s}")
                        slot.nodes = vals
                    elif kl == "seg0":
                        slot.seg0_min, slot.seg0_max = _parse_seg0_bounds(vl)
                    elif kl in ("caller", "callers"):
                        vals = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                        if len(vals) > MAX_FILTER_ITEMS:
                            raise ValueError(f"ctx caller exceeds kMaxFilterItems={MAX_FILTER_ITEMS}: {s}")
                        slot.callers = vals
                    elif kl in ("move", "moves"):
                        slot.moves = _parse_move_pairs_list(vl, f"ctx move in '{s}'")
                    else:
                        raise ValueError(f"Unknown ctx key {kl!r} in: {s}")
                contexts.setdefault(cname, []).append(slot)
                continue
            if head_l != "rule":
                raise ValueError(f"Unknown directive {toks[0]!r} in: {s}")
            r = CompiledRule(-1, -1, [], [], -1, -1, [], [], 2000 + (next_id - 10000), set(), s)
            g_scope, g_min, g_max, g_every = "always", 0, 0, ""
            next_id += 1
            def parse_low_steps(rhs_expr: str) -> None:
                for st in rhs_expr.split(","):
                    choices = []
                    for ch in st.split("+"):
                        w = 1.0
                        if ch.endswith(")") and "(" in ch:
                            lp = ch.rfind("(")
                            w = parse_positive_weight(ch[lp + 1 : -1].strip(), f"Low preset line '{s}'")
                            ch = ch[:lp]
                        choices.append(([p.strip().upper() for p in ch.split("|")], w))
                    if len(choices) > MAX_STEP_CHOICES:
                        raise ValueError(
                            f"Rule step exceeds kMaxStepChoices={MAX_STEP_CHOICES} ({len(choices)} choices): {s}"
                        )
                    r.steps.append(choices)
                if len(r.steps) > MAX_RULE_STEPS:
                    raise ValueError(
                        f"Rule exceeds kMaxRuleSteps={MAX_RULE_STEPS} ({len(r.steps)} steps): {s}"
                    )

            i = 1
            while i < len(toks):
                t = toks[i]
                if t == "->":
                    parse_low_steps(toks[i + 1])
                    i += 2
                    continue
                k, v = t.split("=", 1)
                kl = k.lower()
                vl = v.lower()
                if kl in ("mon", "monster"):
                    r.mon = -1 if vl in ("*", "any") else int(vl)
                elif kl in ("d", "depth"):
                    r.depth = -1 if vl in ("*", "any") else int(vl)
                elif kl in ("f", "file"):
                    r.files = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                elif kl in ("node", "nodes"):
                    r.nodes = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                elif kl == "seg0":
                    r.seg0_min, r.seg0_max = _parse_seg0_bounds(vl)
                elif kl in ("caller", "callers"):
                    r.callers = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                elif kl in ("prev", "prev_move"):
                    r.prev_moves = _parse_move_pairs_list(vl, f"Rule prev in '{s}'")
                elif kl in ("prev_d", "prev_depth"):
                    r.prev_depth = -1 if vl in ("*", "any") else int(vl)
                elif kl in ("prev_f", "prev_file"):
                    r.prev_files = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                elif kl in ("prev_node", "prev_nodes"):
                    r.prev_nodes = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                elif kl == "prev_seg0":
                    r.prev_seg0_min, r.prev_seg0_max = _parse_seg0_bounds(vl)
                elif kl in ("prev_caller", "prev_callers"):
                    r.prev_callers = [] if vl in ("*", "any") else [int(x) for x in vl.split(",")]
                elif kl == "prev_ctx":
                    if vl not in contexts:
                        raise ValueError(f"Unknown prev_ctx {v!r} in: {s}")
                    r.prev_ctx_name = vl
                    r.prev_ctx_slots = tuple(contexts[vl])
                elif kl == "cond":
                    r.cond_id = int(vl)
                elif kl in ("tag", "tags"):
                    r.tags = {x.strip().lower() for x in vl.split(",") if x.strip()}
                elif kl in ("seq", "target"):
                    parse_low_steps(v)
                elif kl == "end":
                    r.seq_end = {"once": "vanilla"}.get(vl, vl)
                elif kl == "scope":
                    r.seq_scope = {"rule": "fired"}.get(vl, vl)
                elif kl == "when":
                    g_scope = {"matched": "rule"}.get(vl, vl)
                elif kl in ("after", "min"):
                    g_min = int(vl)
                    if g_scope == "always" and g_min > 0:
                        g_scope = "rule"
                elif kl in ("until", "max"):
                    g_max = int(vl)
                    if g_scope == "always" and g_max > 0:
                        g_scope = "rule"
                elif kl == "every":
                    g_every = vl
                    if g_scope == "always":
                        g_scope = "rule"
                i += 1
            r.gate_sig = (g_scope, g_min, g_max, g_every)
            rules.append(r)
    return disabled, rules


def _matches_compiled_ctx_slot(slot: CompiledContextSlot, mon: int, pc: Dict[str, Any]) -> bool:
    if slot.mon >= 0 and slot.mon != mon:
        return False
    if slot.depth >= 0 and pc.get("depth", -1) != slot.depth:
        return False
    if slot.files and pc.get("file", -1) not in slot.files:
        return False
    if slot.nodes and pc.get("node", -1) not in slot.nodes:
        return False
    s0 = pc.get("seg0", -1)
    if slot.seg0_min >= 0 and s0 < slot.seg0_min:
        return False
    if slot.seg0_max >= 0 and s0 > slot.seg0_max:
        return False
    p_callers = pc.get("callers", ())
    if slot.callers and not any(c in slot.callers for c in p_callers):
        return False
    pm = pc.get("move")
    if slot.moves and (pm is None or pm not in slot.moves):
        return False
    return True


def eval_low_rule_set(
    rules: Sequence[CompiledRule],
    disabled_tags: Set[str],
    mon: int,
    depth: int,
    file_idx: int,
    node_idx: int,
    seg0: int,
    callers: Sequence[int],
    cands: Sequence[RandCandidate],
    prev_move: Optional[Tuple[int, int]] = None,
    prev_ctx: Optional[Dict[str, Any]] = None,
):
    def resolve_spec(sp: str) -> int:
        if sp in ("VANILLA", "PASS", "SKIP", "-"):
            return -2
        if sp in ("ENDR", "RANDOM", "ELSER"):
            want = {"RANDOM": FL_RANDOM, "ENDR": FL_ENDR, "ELSER": FL_ELSER}[sp]
            for idx, c in enumerate(cands):
                if c.weight > 0 and (c.fl & CTRL_KIND_MASK) == want:
                    return idx
            return -1
        if sp.startswith("SEG:"):
            want_s = int(sp[4:])
            for idx, c in enumerate(cands):
                if c.weight > 0 and c.s == want_s:
                    return idx
            return -1
        if sp.startswith("CAND:"):
            c_idx = int(sp[5:])
            if 0 <= c_idx < len(cands) and cands[c_idx].weight > 0:
                return c_idx
            return -1
        if ":" in sp:
            tf_s, tn_s = sp.split(":", 1)
            tf, tn = int(tf_s), int(tn_s)
            for idx, c in enumerate(cands):
                if c.weight > 0 and c.tf == tf and c.tn == tn:
                    return idx
            return -1
        return -1

    def encode_step(step_choices: Sequence[Tuple[List[str], float]]) -> Tuple:
        resolved: List[Tuple[int, float]] = []
        for alts, w in step_choices:
            picked = -1
            for sp in alts:
                picked = resolve_spec(sp)
                if picked != -1:
                    break
            if picked != -1:
                resolved.append((picked, round(w, 6)))
        if not resolved:
            return ()
        tot = sum(w for _, w in resolved)
        return tuple(
            (
                cands[idx].tf if idx >= 0 else -1,
                cands[idx].tn if idx >= 0 else -1,
                cands[idx].fl & CTRL_KIND_MASK if idx >= 0 else -1,
                cands[idx].s if (idx >= 0 and cands[idx].tf < 0) else -1,
                round(w / tot, 6),
            )
            for idx, w in resolved
        )

    for r in rules:
        if r.tags & disabled_tags:
            continue
        if r.mon >= 0 and r.mon != mon:
            continue
        if r.depth >= 0 and r.depth != depth:
            continue
        if r.files and file_idx not in r.files:
            continue
        if r.nodes and node_idx not in r.nodes:
            continue
        if r.seg0_min >= 0 and seg0 < r.seg0_min:
            continue
        if r.seg0_max >= 0 and seg0 > r.seg0_max:
            continue
        if r.callers and not any(c in r.callers for c in callers):
            continue
        has_prev_req = bool(
            r.prev_moves
            or r.prev_depth >= 0
            or r.prev_files
            or r.prev_nodes
            or r.prev_seg0_min >= 0
            or r.prev_seg0_max >= 0
            or r.prev_callers
            or r.prev_ctx_slots
        )
        if has_prev_req:
            pc = dict(prev_ctx) if prev_ctx is not None else {}
            if "move" not in pc and prev_move is not None:
                pc["move"] = prev_move
            pm = pc.get("move") if pc else None
            if not pc:
                continue
            if r.prev_moves and (pm is None or pm not in r.prev_moves):
                continue
            if r.prev_depth >= 0 and pc.get("depth", -1) != r.prev_depth:
                continue
            if r.prev_files and pc.get("file", -1) not in r.prev_files:
                continue
            if r.prev_nodes and pc.get("node", -1) not in r.prev_nodes:
                continue
            ps0 = pc.get("seg0", -1)
            if r.prev_seg0_min >= 0 and ps0 < r.prev_seg0_min:
                continue
            if r.prev_seg0_max >= 0 and ps0 > r.prev_seg0_max:
                continue
            p_cals = pc.get("callers", ())
            if r.prev_callers and not any(c in r.prev_callers for c in p_cals):
                continue
            if r.prev_ctx_slots and not any(_matches_compiled_ctx_slot(sl, mon, pc) for sl in r.prev_ctx_slots):
                continue

        sig0 = encode_step(r.steps[0])
        if sig0:
            all_sigs = tuple(
                encode_step(st) or ("UNRESOLVED", tuple((tuple(a), w) for a, w in st))
                for st in r.steps
            )
            return (True, r.cond_id, all_sigs, r.gate_sig, r.seq_end, r.seq_scope)
    return (False, 0, (), ("always", 0, 0, ""), "loop", "fired")


def verify_equivalence(
    orig_path: str,
    compiled_path: str,
    main_path: str,
    sub_thks: Dict[int, ThkFile],
    main_files: Sequence[int],
    explicit_tag_modes: Optional[Dict[str, Set[str]]] = None,
) -> int:
    thk_main = load_thk_file(main_path)
    mon_id = thk_main.monster_id
    orig_dis, orig_rules = parse_low_preset(orig_path)
    comp_dis, comp_rules = parse_low_preset(compiled_path)

    d0_blocks: List[Tuple[int, RandBlock]] = []
    for nd in thk_main.nodes:
        _, rands = parse_node_cfg(nd)
        for rb in rands:
            d0_blocks.append((nd.idx, rb))

    d1_blocks: List[Tuple[int, int, RandBlock]] = []
    for tf, sub_thk in sorted(sub_thks.items()):
        for nd in sub_thk.nodes:
            _, rands = parse_node_cfg(nd)
            for rb in rands:
                d1_blocks.append((tf, nd.idx, rb))

    if explicit_tag_modes:
        tag_modes = explicit_tag_modes
    else:
        all_tags: Set[str] = set(orig_dis) | set(comp_dis)
        for r in list(orig_rules) + list(comp_rules):
            all_tags |= r.tags
        tag_modes = {"default": set(orig_dis)}
        for tg in sorted(all_tags):
            tag_modes[f"only_{tg}"] = all_tags - {tg}
            if tg in orig_dis:
                tag_modes[f"enable_{tg}"] = set(orig_dis) - {tg}
            else:
                tag_modes[f"disable_{tg}"] = set(orig_dis) | {tg}
        tag_modes["all_enabled"] = set()

    total_mismatches = 0
    for mode_name, dis_tags in tag_modes.items():
        mismatches = 0
        controlled_orig = 0
        controlled_comp = 0
        checks = 0

        for f_idx in main_files:
            for n_idx, rb in d0_blocks:
                checks += 1
                res_o = eval_low_rule_set(orig_rules, dis_tags, mon_id, 0, f_idx, n_idx, rb.seg0, (), rb.cands)
                res_c = eval_low_rule_set(comp_rules, dis_tags, mon_id, 0, f_idx, n_idx, rb.seg0, (), rb.cands)
                if res_o[0]:
                    controlled_orig += 1
                if res_c[0]:
                    controlled_comp += 1
                if res_o != res_c:
                    mismatches += 1
                    print(f"MISMATCH [{mode_name}] d=0 f={f_idx} node={n_idx} seg0={rb.seg0}: orig={res_o} comp={res_c}")

        all_caller_sets: List[Tuple[int, ...]] = [()] + [(c,) for c in range(len(thk_main.nodes))]
        for tf, n_idx, rb in d1_blocks:
            for cset in all_caller_sets:
                checks += 1
                res_o = eval_low_rule_set(orig_rules, dis_tags, mon_id, 1, tf, n_idx, rb.seg0, cset, rb.cands)
                res_c = eval_low_rule_set(comp_rules, dis_tags, mon_id, 1, tf, n_idx, rb.seg0, cset, rb.cands)
                if res_o[0]:
                    controlled_orig += 1
                if res_c[0]:
                    controlled_comp += 1
                if res_o != res_c:
                    mismatches += 1
                    print(
                        f"MISMATCH [{mode_name}] d=1 f={tf} node={n_idx} seg0={rb.seg0} caller={cset}: orig={res_o} comp={res_c}"
                    )

        total_mismatches += mismatches
        print(
            f"[{mode_name:14s}] checks={checks:6d}, controlled orig={controlled_orig:3d} comp={controlled_comp:3d}, mismatches={mismatches}"
        )

    print(f"Rules count: original={len(orig_rules)}, compiled={len(comp_rules)}, total_mismatches={total_mismatches}")
    return 0 if total_mismatches == 0 else 1


# =============================================================================
# Groups Scaffold Generator (`init-groups`) & Interactive Query (`query`)
# =============================================================================


def render_groups_scaffold_toml(meta_path: str) -> str:
    """Generate a complete editable `.groups.toml` scaffold from `.thkmeta.toml` alone."""
    meta = load_thkmeta_toml(meta_path)

    em_names: Set[str] = set()
    var_names: Set[str] = set()
    dist_ivs: Set[Tuple[int, Optional[int]]] = set()
    for pt in meta.d0_points + meta.d1_points:
        for st in pt.states:
            if pt.depth == 0 and (st.dist_lo > 0 or st.dist_hi is not None):
                dist_ivs.add((st.dist_lo, st.dist_hi))
            for pr in st.other_preds:
                m_em = re.match(r"^!?(em_[0-9a-fA-F]+)", pr)
                if m_em:
                    em_names.add(m_em.group(1))
                m_var = re.match(r"^(var_\d+)", pr)
                if m_var:
                    var_names.add(m_var.group(1))

    lines: List[str] = [
        "# ============================================================================",
        f"# Auto-Generated Editable Semantic Overlay Scaffold: {meta.em_id} (MonsterID = {meta.mon})",
        f"# Paired with read-only `{os.path.basename(meta_path)}`.",
        "# Zero raw `<file>:<nid>` node IDs — rename/alias any snake_case move or define condition groups here.",
        "# ============================================================================",
        "",
        "[conditions]",
        "# Friendly aliases for monster-private opcodes (`em_*`) and THK registers (`var_*`).",
    ]
    for name in sorted(em_names, key=lambda x: int(x.split("_")[1], 16)) + sorted(
        var_names, key=lambda x: int(x.split("_")[1])
    ):
        lines.append(f"{name:<10s} = {toml_str(name)}")

    lines.extend(
        [
            "",
            "[groups.dir]",
            'front      = "angle in -45..45"',
            'back       = "angle in 135..225"',
            'side       = "angle in 45..135 | 225..315"',
            'front_hemi = "angle in -90..90"',
            'back_hemi  = "angle in 90..270"',
            "",
            "[groups.range]",
            "# Discovered distance intervals in d=0 THK (name the ranges you need):",
        ]
    )
    for idx_d, (lo, hi) in enumerate(sorted(dist_ivs, key=lambda x: (x[0], INF if x[1] is None else x[1])), start=1):
        d_expr = f"dist >= {lo}" if hi is None else (f"dist <= {hi}" if lo == 0 else f"dist in {lo}..{hi}")
        lines.append(f'# r_{idx_d:<2d} = "{d_expr}"')

    lines.extend(
        [
            "",
            "[moves]",
            f"# Full auto-converted snake_case move roster ({len(meta.moves)} moves; ZERO node IDs).",
            "# Rename keys on the left or reference the snake_case names directly in `.preset.toml`.",
        ]
    )
    max_m = max((len(m) for m in meta.moves), default=16)
    for mname in sorted(meta.moves.keys()):
        tlist = meta.moves[mname]
        lines.append(f"{mname:<{max_m}} = {toml_str(mname):<{max_m + 3}} # {len(tlist)} node(s)")

    lines.extend(
        [
            "",
            "[move_groups]",
            "# Combine multiple distinct moves under one fallback/union name (zero node IDs), e.g.:",
            '# combo_group = ["move_a", "move_b"]',
            "",
        ]
    )
    text = "\n".join(lines)
    tomllib.loads(text)
    return text


def format_point_id(pt: MetaPoint, multi_sub: bool = False) -> str:
    if pt.depth == 0:
        return f"d0.n{pt.node_idx}.s{pt.seg0}"
    if multi_sub:
        return f"d1.f{pt.owner_tf}_n{pt.node_idx}.s{pt.seg0}"
    return f"d1.n{pt.node_idx}.s{pt.seg0}"


def parse_point_id_spec(spec: str) -> Optional[Tuple[Optional[int], Optional[int], int, Optional[int]]]:
    """Parse point selectors like `d0.n14.s22`, `d0.n14`, `d1.f55_n87.s9`, `n14.s22`."""
    m = re.fullmatch(
        r"(?:d([01])\.)?(?:f(\d+)_)?n(\d+)(?:\.s(\d+))?",
        spec.strip(),
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    d_opt = int(m.group(1)) if m.group(1) is not None else None
    f_opt = int(m.group(2)) if m.group(2) is not None else None
    n_idx = int(m.group(3))
    s_opt = int(m.group(4)) if m.group(4) is not None else None
    return d_opt, f_opt, n_idx, s_opt


def state_to_dict(st: PathState) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "dist": [st.dist_lo, "inf" if st.dist_hi is None else st.dist_hi],
        "angle": format_angle_intervals(st.angles),
        "preds": list(st.other_preds),
    }
    if st.dist3d_lo > 0 or st.dist3d_hi is not None:
        out["dist3d"] = [st.dist3d_lo, "inf" if st.dist3d_hi is None else st.dist3d_hi]
    return out


def format_state_brief(st: PathState) -> str:
    parts = [f"dist=[{st.dist_lo}, {'inf' if st.dist_hi is None else st.dist_hi}]"]
    if st.dist3d_lo > 0 or st.dist3d_hi is not None:
        parts.append(f"dist3d=[{st.dist3d_lo}, {'inf' if st.dist3d_hi is None else st.dist3d_hi}]")
    parts.append(f"angle={format_angle_intervals(st.angles)}")
    if st.other_preds:
        parts.append(f"preds=[{', '.join(st.other_preds)}]")
    else:
        parts.append("preds=[]")
    return " ".join(parts)


def is_switch_point(pt: MetaPoint) -> bool:
    if pt.effects:
        return True
    return any(c.weight > 0 and c.tf < 0 and c.act == 0 and c.lj < 0 for c in pt.cands)


def describe_cand(c: RandCandidate, pt: MetaPoint, meta: LoadedThkMeta) -> Dict[str, Any]:
    weight_val: Any = int(c.weight) if c.weight == int(c.weight) else c.weight
    effects = list(pt.effects.get(c.raw_key, ()))
    kind = c.fl & CTRL_KIND_MASK
    ctrl_name = {FL_RANDOM: "RANDOM", FL_ENDR: "ENDR", FL_ELSER: "ELSER"}.get(kind, "")
    if c.tf >= 0 and c.tn >= 0:
        move_name = meta.target_to_move.get((c.tf, c.tn), f"move_{c.tf}_{c.tn}")
        return {
            "key": c.raw_key or f"{c.tf}:{c.tn}",
            "kind": "target",
            "target": f"{c.tf}:{c.tn}",
            "move": move_name,
            "weight": weight_val,
            "effects": effects,
        }
    if c.act > 0:
        aname = meta.inline_actions.get(c.act, f"act_{c.act}")
        return {
            "key": c.raw_key,
            "kind": "inline_act",
            "target": f"seg:{c.s}" if c.s >= 0 else f"act:{c.act}",
            "move": aname,
            "ctrl": ctrl_name,
            "weight": weight_val,
            "effects": effects,
        }
    if c.lj >= 0:
        nid, mname = meta.subnode_by_idx.get((pt.owner_tf, c.lj), (-1, ""))
        return {
            "key": c.raw_key,
            "kind": "local_jump",
            "target": f"seg:{c.s}" if c.s >= 0 else f"lj:{c.lj}",
            "lj": c.lj,
            "subnode": f"{pt.owner_tf}:{nid}" if nid >= 0 else "",
            "move": mname or f"lj:{c.lj}",
            "ctrl": ctrl_name,
            "weight": weight_val,
            "effects": effects,
        }
    if ctrl_name:
        return {
            "key": c.raw_key or ctrl_name,
            "kind": "control",
            "target": ctrl_name,
            "move": ctrl_name,
            "ctrl": ctrl_name,
            "weight": weight_val,
            "effects": effects,
        }
    return {
        "key": c.raw_key or f"seg:{c.s}",
        "kind": "seg",
        "target": f"seg:{c.s}",
        "move": f"seg:{c.s}",
        "weight": weight_val,
        "effects": effects,
    }


def format_cand_compact(cd: Dict[str, Any]) -> str:
    w = cd["weight"]
    kind = cd["kind"]
    if kind == "target":
        base = f"{cd['move']}({cd['target']})={w}"
    elif kind == "inline_act":
        ctrl_s = f"|{cd['ctrl']}" if cd.get("ctrl") else ""
        base = f"{cd['move']}({cd['target']}{ctrl_s})={w}"
    elif kind == "local_jump":
        sub_s = cd.get("subnode") or cd.get("target")
        ctrl_s = f"|{cd['ctrl']}" if cd.get("ctrl") else ""
        base = f"{cd['move']}({cd['target']}:{sub_s}{ctrl_s})={w}"
    else:
        base = f"{cd['move']}={w}"
    if cd["effects"]:
        base += f" -> [{', '.join(cd['effects'])}]"
    return base


def emit_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _load_meta_and_groups(meta_path: str, groups_path: Optional[str]) -> Tuple[LoadedThkMeta, LoadedGroups, Dict[int, List[MetaPoint]]]:
    meta = load_thkmeta_toml(meta_path)
    grp = load_groups_toml(
        groups_path,
        meta.moves,
        subnode_moves=meta.subnode_moves,
        inline_actions=meta.inline_actions,
    )
    d0_by_node: Dict[int, List[MetaPoint]] = defaultdict(list)
    for pt in meta.d0_points:
        d0_by_node[pt.node_idx].append(pt)
    return meta, grp, d0_by_node


def _point_to_json_dict(pt: MetaPoint, meta: LoadedThkMeta, multi_sub: bool = False, callers_override: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "point": format_point_id(pt, multi_sub=multi_sub),
        "depth": pt.depth,
        "node": pt.node_idx,
        "nid": pt.node_nid,
        "seg0": pt.seg0,
    }
    if pt.depth == 0:
        out["parents"] = list(pt.parents)
        out["via"] = [">".join(str(x) for x in v) for v in pt.vias]
    else:
        out["file"] = pt.owner_tf
        out["owner"] = f"{pt.owner_tf}:{pt.owner_tn}"
        out["owner_move"] = meta.target_to_move.get((pt.owner_tf, pt.owner_tn), "")
        out["entry_owners"] = list(pt.entry_owners)
        out["callers"] = list(callers_override if callers_override is not None else pt.callers)
    out["states"] = [state_to_dict(st) for st in pt.states]
    out["cands"] = [describe_cand(c, pt, meta) for c in pt.cands if c.weight > 0]
    if pt.effects:
        out["effects"] = {k: list(v) for k, v in pt.effects.items()}
    return out


def run_query(
    meta_path: str,
    groups_path: Optional[str],
    depth: Optional[int],
    when_str: Optional[str],
    move_name: Optional[str],
    in_move: Optional[str],
    from_str: Optional[str],
    followup_move: Optional[str] = None,
) -> int:
    """Interactive standalone query showing how multiple conditions + move name lock onto exact points."""
    meta = load_thkmeta_toml(meta_path)
    grp = load_groups_toml(
        groups_path,
        meta.moves,
        subnode_moves=meta.subnode_moves,
        inline_actions=meta.inline_actions,
    )

    d0_by_node: Dict[int, List[MetaPoint]] = defaultdict(list)
    for pt in meta.d0_points:
        d0_by_node[pt.node_idx].append(pt)

    if followup_move:
        depth = 1
        in_move = followup_move

    sel_terms: List[str] = []
    if depth is not None:
        sel_terms.append(f"d={depth}")
    elif in_move or from_str:
        sel_terms.append("d=1")
    if when_str:
        sel_terms.extend(split_selector_str(when_str))
    if in_move:
        sel_terms.append(f"in={in_move}")
    if from_str:
        sel_terms.append("from=(" + " ".join(split_selector_str(from_str)) + ")")
    if move_name and depth != 1 and not in_move and not any(t.startswith(("has=", "!has=")) for t in sel_terms):
        sel_terms.append(f"has={move_name}")

    if move_name:
        pool, mnames = expand_move_names_and_targets(move_name, meta, grp)
        parts = [f"{a}:{b}" for a, b in sorted(pool)] + [f"name:{n}" for n in sorted(mnames) if not pool]
        pstr = ", ".join(parts)
        base_d0_cnt = sum(1 for pt in meta.d0_points if match_move_specs_at_point(move_name, pt, meta, grp))
        print(f"Move '{move_name}' -> candidate pool [{pstr}] ({base_d0_cnt} d=0 points total in THK)")
    if in_move:
        in_pool, _ = expand_move_names_and_targets(in_move, meta, grp)
        pstr = ", ".join(f"{a}:{b}" for a, b in sorted(in_pool))
        print(f"Caller target 'in={in_move}' -> pool [{pstr}]")

    print(f"Selectors: {sel_terms}")
    matched_pts: List[Tuple[MetaPoint, Optional[List[int]], str]] = []
    partial_multipath: List[str] = []
    target_pts = meta.d1_points if (depth == 1 or in_move or from_str) else meta.d0_points
    for pt in target_pts:
        ok, callers = eval_rule_selectors(sel_terms, pt, meta, grp, d0_by_node)
        if not ok:
            if len(pt.states) >= 2:
                any_path_ok = any(
                    eval_rule_selectors(sel_terms, dataclasses.replace(pt, states=[st]), meta, grp, d0_by_node)[0]
                    for st in pt.states
                )
                if any_path_ok:
                    partial_multipath.append(f"d{pt.depth}.n{pt.node_idx}.s{pt.seg0}")
            continue
        resolved = ""
        if move_name:
            try:
                res = resolve_single_spec_at_point(move_name, pt, meta, grp, f"query({move_name})")
                if res is None:
                    continue
                resolved = res
            except AmbiguousMoveError as e:
                print(f"AMBIGUOUS: {e}")
                return 2
        matched_pts.append((pt, callers, resolved))

    print(f"Matched points: {len(matched_pts)}")
    if partial_multipath:
        shown = ", ".join(partial_multipath[:8]) + (" ..." if len(partial_multipath) > 8 else "")
        print(f"Note: {len(partial_multipath)} multi-path point(s) partially matched (skipped for safety): {shown}")
    resolved_set: List[str] = []
    nodes_by_seg0: Dict[int, List[int]] = defaultdict(list)
    for pt, callers, resolved in matched_pts:
        if resolved and resolved not in resolved_set:
            resolved_set.append(resolved)
        nodes_by_seg0[pt.node_idx].append(pt.seg0)
        st0 = pt.states[0]
        dist_s = f"[{st0.dist_lo}, {'inf' if st0.dist_hi is None else st0.dist_hi}]"
        ang_s = format_angle_intervals(st0.angles)
        w_s = ""
        if resolved and resolved.startswith("seg:"):
            rseg = int(resolved[4:])
            ws = [c.weight for c in pt.cands if c.weight > 0 and c.s == rseg]
            if ws:
                w_s = f" (vanilla w={toml_num(ws[0])})"
        elif resolved and ":" in resolved:
            rtf, rtn = (int(x) for x in resolved.split(":"))
            ws = [c.weight for c in pt.cands if c.weight > 0 and c.tf == rtf and c.tn == rtn]
            if ws:
                w_s = f" (vanilla w={toml_num(ws[0])})"
        cal_s = f" caller={callers}" if callers is not None else ""
        res_s = f" -> {resolved}{w_s}" if resolved else ""
        print(f"  d={pt.depth} n{pt.node_idx:<3d} s{pt.seg0:<3d} dist={dist_s:<11s} angle={ang_s:<18s}{cal_s}{res_s}")

    if matched_pts and matched_pts[0][0].depth == 0:
        seg_exprs: Dict[str, List[int]] = defaultdict(list)
        for nidx, segs in sorted(nodes_by_seg0.items()):
            for se in format_seg0_ranges(segs, meta.node_segs_d0[nidx]):
                seg_exprs[se].append(nidx)
        summary = "; ".join(f"node={','.join(str(x) for x in ns)} seg0={se}" for se, ns in seg_exprs.items())
        print(f"Summary slots: {summary}")
        if resolved_set:
            print(f"Resolved targets: {', '.join(resolved_set)}")
    return 0


def run_overview(meta_path: str, groups_path: Optional[str], as_json: bool = False) -> int:
    """Compact schema, predicate vocabulary, distance buckets, and group inventory summary."""
    meta, grp, _ = _load_meta_and_groups(meta_path, groups_path)
    all_pts = meta.d0_points + meta.d1_points
    em_ops: Dict[str, Set[str]] = defaultdict(set)
    var_ops: Dict[str, Set[str]] = defaultdict(set)
    global_preds: Set[str] = set()
    d0_dists: Set[Tuple[int, str]] = set()

    for pt in all_pts:
        for st in pt.states:
            if pt.depth == 0 and (st.dist_lo > 0 or st.dist_hi is not None):
                hi_s = "inf" if st.dist_hi is None else str(st.dist_hi)
                d0_dists.add((st.dist_lo, hi_s))
            for raw_p in st.other_preds:
                p = raw_p[1:] if raw_p.startswith("!") else raw_p
                m_em = re.match(r"^(em_[0-9a-fA-F]+)(.*)$", p)
                if m_em:
                    suf = m_em.group(2).strip() or "bool"
                    em_ops[m_em.group(1)].add(suf)
                    continue
                m_var = re.match(r"^(var_\d+)(.*)$", p)
                if m_var:
                    suf = m_var.group(2).strip() or "set"
                    var_ops[m_var.group(1)].add(suf)
                    continue
                global_preds.add(p)

    # Sort var_<n> numerically by register index
    sorted_em = {k: sorted(em_ops[k]) for k in sorted(em_ops)}
    sorted_var = {
        k: sorted(var_ops[k])
        for k in sorted(var_ops, key=lambda s: int(s.split("_", 1)[1]))
    }
    dist_buckets = [
        f"[{lo}, {hi})" for lo, hi in sorted(d0_dists, key=lambda t: (t[0], INF if t[1] == "inf" else int(t[1])))
    ]

    dims: Dict[str, List[str]] = defaultdict(list)
    for gk in grp.groups:
        dim, _, key = gk.partition(".")
        if key:
            dims[dim].append(key)
    custom_aliases = {k: v for k, v in grp.alias_items.items() if k not in (grp.move_groups and set()) and k not in dims}
    mg_keys = sorted(k for k in grp.alias_items if k in grp.move_groups and (k in meta.moves or len(grp.alias_items[k]) != 1 or grp.alias_items[k][0] != k))

    payload: Dict[str, Any] = {
        "em_id": meta.em_id,
        "mon": meta.mon,
        "default_files": [int(x) for x in meta.default_files.split(",") if x.strip()],
        "sub_files": meta.sub_files,
        "sources": meta.sources,
        "counts": {
            "unique_moves": len(meta.moves),
            "subnodes": len(meta.subnode_info),
            "inline_actions": len(meta.inline_actions),
            "d0_points": len(meta.d0_points),
            "d0_nodes": len({p.node_idx for p in meta.d0_points}),
            "d1_points": len(meta.d1_points),
            "d1_nodes": len({(p.owner_tf, p.node_idx) for p in meta.d1_points}),
            "switch_points_d0": sum(1 for p in meta.d0_points if is_switch_point(p)),
            "switch_points_d1": sum(1 for p in meta.d1_points if is_switch_point(p)),
            "effects_points_d0": sum(1 for p in meta.d0_points if p.effects),
            "effects_points_d1": sum(1 for p in meta.d1_points if p.effects),
            "multipath_points": sum(1 for p in all_pts if len(p.states) >= 2),
        },
        "em_ops": sorted_em,
        "var_ops": sorted_var,
        "global_preds": sorted(global_preds),
        "d0_dist_intervals": dist_buckets,
    }
    if groups_path and os.path.exists(groups_path):
        payload["groups_summary"] = {
            "conditions": grp.conditions,
            "dims": {k: sorted(v) for k, v in sorted(dims.items())},
            "move_aliases_and_groups": mg_keys,
        }

    if as_json:
        emit_json(payload)
        return 0

    c = payload["counts"]
    print(f"THK Meta Overview: {meta.em_id} (mon={meta.mon}) | default_files={meta.default_files} | sub_files={meta.sub_files}")
    if meta.sources:
        print(f"Sources: {', '.join(meta.sources)}")
    print(
        f"Counts: moves={c['unique_moves']} subnodes={c['subnodes']} inline_acts={c['inline_actions']} | "
        f"d0={c['d0_points']} pts ({c['d0_nodes']} nodes) d1={c['d1_points']} pts ({c['d1_nodes']} nodes) | "
        f"switches=d0:{c['switch_points_d0']},d1:{c['switch_points_d1']} | "
        f"effects=d0:{c['effects_points_d0']},d1:{c['effects_points_d1']} | multipath={c['multipath_points']}"
    )
    print(f"em_* predicates ({len(sorted_em)}):")
    for k, vals in sorted_em.items():
        alias = [ak for ak, av in grp.conditions.items() if av == k]
        al_s = f" (alias: {', '.join(alias)})" if alias else ""
        print(f"  {k}{al_s}: {', '.join(vals[:8])}" + (f" ...(+{len(vals)-8})" if len(vals) > 8 else ""))
    print(f"var_* predicates ({len(sorted_var)}):")
    for k, vals in sorted_var.items():
        alias = [ak for ak, av in grp.conditions.items() if av == k]
        al_s = f" (alias: {', '.join(alias)})" if alias else ""
        print(f"  {k}{al_s}: {', '.join(vals[:8])}" + (f" ...(+{len(vals)-8})" if len(vals) > 8 else ""))
    print(f"Global predicates ({len(global_preds)}): {', '.join(sorted(global_preds)[:20])}" + (" ..." if len(global_preds) > 20 else ""))
    print(f"d=0 dist intervals ({len(dist_buckets)}): {', '.join(dist_buckets[:14])}" + (f" ...(+{len(dist_buckets)-14})" if len(dist_buckets) > 14 else ""))
    if "groups_summary" in payload:
        gs = payload["groups_summary"]
        print(f"Loaded groups dims ({len(gs['dims'])}):")
        for dim, keys in gs["dims"].items():
            print(f"  groups.{dim}: {', '.join(keys)}")
        if mg_keys:
            print(f"Move aliases/groups ({len(mg_keys)}): {', '.join(mg_keys)}")
    return 0


def _fuzzy_move_candidates(pattern: str, candidates: Sequence[str], max_hits: int = 10) -> List[str]:
    low = pattern.lower()
    stripped = low.replace("_", "")
    sub_hits = [c for c in candidates if low in c.lower() or stripped in c.lower().replace("_", "")]
    if sub_hits:
        return sub_hits[:max_hits]
    return difflib.get_close_matches(pattern, list(candidates), n=max_hits, cutoff=0.45)


def run_moves(
    meta_path: str,
    groups_path: Optional[str],
    search: Optional[str] = None,
    detail: Optional[str] = None,
    has_d1: bool = False,
    multi_variant: bool = False,
    limit: int = 60,
    as_json: bool = False,
) -> int:
    """Search moves/aliases or inspect deep variant/d0/d1 details for one move."""
    meta, grp, _ = _load_meta_and_groups(meta_path, groups_path)
    multi_sub = len(meta.sub_files) > 1

    # Reverse index from target pair -> aliases in .groups.toml
    aliases_for_pair: Dict[Tuple[int, int], List[str]] = defaultdict(list)
    for mg_name, pairs in grp.move_groups.items():
        if mg_name in meta.moves and grp.alias_items.get(mg_name) == [mg_name]:
            continue
        for pair in pairs:
            if mg_name not in aliases_for_pair[pair]:
                aliases_for_pair[pair].append(mg_name)

    d1_owner_pts: Dict[Tuple[int, int], List[MetaPoint]] = defaultdict(list)
    d1_entry_pts: Dict[Tuple[int, int], List[MetaPoint]] = defaultdict(list)
    for pt in meta.d1_points:
        d1_owner_pts[(pt.owner_tf, pt.owner_tn)].append(pt)
        for eo in pt.entry_owners:
            if ":" in eo:
                etf_s, etn_s = eo.split(":", 1)
                d1_entry_pts[(int(etf_s), int(etn_s))].append(pt)

    if detail:
        sp = detail.strip()
        resolved_pairs: Set[Tuple[int, int]] = set()
        resolved_inline_ids: List[int] = []
        if re.fullmatch(r"act:\d+", sp, flags=re.IGNORECASE):
            aid = int(sp.split(":", 1)[1])
            if aid in meta.inline_actions:
                resolved_inline_ids.append(aid)
        else:
            try:
                pairs, mnames = expand_move_names_and_targets(sp, meta, grp)
                resolved_pairs = set(pairs)
                for aid, aname in sorted(meta.inline_actions.items()):
                    if aname in mnames or aname == sp:
                        resolved_inline_ids.append(aid)
            except ValueError:
                pass

        if not resolved_pairs and not resolved_inline_ids:
            all_names = sorted(
                set(meta.moves) | set(meta.subnode_moves) | set(meta.inline_actions.values()) | set(grp.alias_items)
            )
            sugs = _fuzzy_move_candidates(sp, all_names)
            if as_json:
                emit_json({"error": f"Unknown move '{sp}'", "suggestions": sugs})
            else:
                print(f"Move '{sp}' not found.", file=sys.stderr)
                if sugs:
                    print(f"Did you mean: {', '.join(sugs)}", file=sys.stderr)
            return 2

        variant_details: List[Dict[str, Any]] = []
        for tf, tn in sorted(resolved_pairs):
            info = dict(meta.subnode_info.get((tf, tn), {}))
            canon = meta.target_to_move.get((tf, tn), info.get("move", f"{tf}:{tn}"))
            d0_hits: List[Tuple[MetaPoint, float]] = []
            for pt in meta.d0_points:
                for c in pt.cands:
                    if c.weight > 0 and c.tf == tf and c.tn == tn:
                        d0_hits.append((pt, c.weight))
                        break
            weights = [w for _, w in d0_hits]
            nodes_hit = sorted({pt.node_idx for pt, _ in d0_hits})
            st_all = [st for pt, _ in d0_hits for st in pt.states]
            common_preds: List[str] = []
            varying_preds: List[str] = []
            if st_all:
                pred_sets = [set(st.other_preds) for st in st_all]
                inter = set.intersection(*pred_sets)
                union = set.union(*pred_sets)
                common_preds = sorted(inter)
                varying_preds = sorted(union - inter)

            d1_blocks: List[Dict[str, Any]] = []
            seen_d1_keys: Set[Tuple[str, int, int, int]] = set()
            for role, pt_list in (("owner", d1_owner_pts.get((tf, tn), ())), ("entry_owner", d1_entry_pts.get((tf, tn), ()))):
                for pt in pt_list:
                    k_tup = (role, pt.owner_tf, pt.node_idx, pt.seg0)
                    if k_tup in seen_d1_keys:
                        continue
                    seen_d1_keys.add(k_tup)
                    d1_blocks.append(
                        {
                            "role": role,
                            "point": format_point_id(pt, multi_sub=multi_sub),
                            "owner": f"{pt.owner_tf}:{pt.owner_tn}",
                            "callers": list(pt.callers),
                            "states": [state_to_dict(st) for st in pt.states],
                            "cands": [describe_cand(c, pt, meta) for c in pt.cands if c.weight > 0],
                        }
                    )
            variant_details.append(
                {
                    "target": f"{tf}:{tn}",
                    "canonical_move": canon,
                    "aliases": aliases_for_pair.get((tf, tn), []),
                    "subnode": info,
                    "d0_usage": {
                        "points": len(d0_hits),
                        "nodes": nodes_hit,
                        "weight_min": min(weights) if weights else None,
                        "weight_max": max(weights) if weights else None,
                        "common_preds": common_preds,
                        "varying_preds": varying_preds[:12],
                    },
                    "d1_blocks": d1_blocks,
                }
            )

        inline_details: List[Dict[str, Any]] = []
        for aid in sorted(set(resolved_inline_ids)):
            aname = meta.inline_actions.get(aid, f"act_{aid}")
            hits_inline: List[Dict[str, Any]] = []
            for pt in meta.d0_points + meta.d1_points:
                for c in pt.cands:
                    if c.weight > 0 and c.act == aid:
                        hits_inline.append(
                            {
                                "point": format_point_id(pt, multi_sub=multi_sub),
                                "seg": f"seg:{c.s}",
                                "weight": int(c.weight) if c.weight == int(c.weight) else c.weight,
                                "state": format_state_brief(pt.states[0]) if pt.states else "",
                            }
                        )
            inline_details.append({"act_id": aid, "move": aname, "occurrences": hits_inline})

        payload = {"query": sp, "variants": variant_details, "inline_actions": inline_details}
        if as_json:
            emit_json(payload)
            return 0

        print(f"Move Detail: '{sp}' -> targets={len(variant_details)}, inline_acts={len(inline_details)}")
        for vd in variant_details:
            sn = vd["subnode"]
            d0u = vd["d0_usage"]
            w_str = f"{toml_num(d0u['weight_min'])}..{toml_num(d0u['weight_max'])}" if d0u["weight_min"] is not None else "-"
            al_s = f" aliases={vd['aliases']}" if vd["aliases"] else ""
            print(f"\n[{vd['target']}] canonical={vd['canonical_move']} (idx={sn.get('idx', '?')}, has_rand={sn.get('has_rand', False)}){al_s}")
            if sn.get("checks"):
                print(f"  subnode checks: {', '.join(sn['checks'])}")
            if sn.get("chains"):
                print(f"  subnode chains: {', '.join(sn['chains'])}")
            if sn.get("var_writes"):
                print(f"  subnode writes: {', '.join(sn['var_writes'])}")
            print(f"  d=0 usage: {d0u['points']} pts across nodes={d0u['nodes']} vanilla_w={w_str}")
            if d0u["common_preds"]:
                print(f"  d=0 common_preds: {', '.join(d0u['common_preds'])}")
            if d0u["varying_preds"]:
                print(f"  d=0 varying_preds: {', '.join(d0u['varying_preds'])}")
            if vd["d1_blocks"]:
                print(f"  d=1 blocks ({len(vd['d1_blocks'])}):")
                for db in vd["d1_blocks"]:
                    cands_s = ", ".join(format_cand_compact(cd) for cd in db["cands"])
                    print(f"    {db['point']} ({db['role']}, owner={db['owner']}) callers={db['callers']} -> {cands_s}")
        for inl in inline_details:
            print(f"\n[act:{inl['act_id']}] {inl['move']} ({len(inl['occurrences'])} points)")
            for occ in inl["occurrences"]:
                print(f"  {occ['point']} -> {occ['seg']} (w={occ['weight']}) {occ['state']}")
        return 0

    # List / Search mode
    entries: List[Dict[str, Any]] = []
    d0_count_by_pair: Dict[Tuple[int, int], int] = Counter()
    for pt in meta.d0_points:
        for pair in {(c.tf, c.tn) for c in pt.cands if c.weight > 0 and c.tf >= 0 and c.tn >= 0}:
            d0_count_by_pair[pair] += 1

    all_move_names = sorted(set(meta.moves) | set(meta.subnode_moves))
    inline_names_seen: Set[str] = set()
    for mname in all_move_names:
        targets = meta.moves.get(mname) or meta.subnode_moves.get(mname, [])
        t_strs = [f"{tf}:{tn}" for tf, tn in targets]
        d0_pts_cnt = sum(d0_count_by_pair.get(pair, 0) for pair in targets)
        d1_cnt = sum(len(d1_owner_pts.get(pair, ())) + len(d1_entry_pts.get(pair, ())) for pair in targets)
        m_aliases = sorted({al for pair in targets for al in aliases_for_pair.get(pair, [])})
        if mname in meta.inline_actions.values():
            inline_names_seen.add(mname)
        entries.append(
            {
                "name": mname,
                "kind": "move",
                "targets": t_strs,
                "variants": len(targets),
                "d0_points": d0_pts_cnt,
                "has_d1": d1_cnt > 0,
                "d1_refs": d1_cnt,
                "aliases": m_aliases,
            }
        )
    for aid, aname in sorted(meta.inline_actions.items()):
        if aname in inline_names_seen:
            continue
        inline_names_seen.add(aname)
        cnt = sum(1 for pt in meta.d0_points + meta.d1_points if any(c.weight > 0 and c.act == aid for c in pt.cands))
        entries.append(
            {
                "name": aname,
                "kind": "inline_act",
                "targets": [f"act:{aid}"],
                "variants": 1,
                "d0_points": cnt,
                "has_d1": False,
                "d1_refs": 0,
                "aliases": [],
            }
        )
    for mg_name, pairs in sorted(grp.move_groups.items()):
        if mg_name in meta.moves or mg_name in meta.subnode_moves or mg_name in inline_names_seen:
            continue
        t_strs = [f"{tf}:{tn}" for tf, tn in pairs]
        d0_pts_cnt = sum(d0_count_by_pair.get(pair, 0) for pair in pairs)
        d1_cnt = sum(len(d1_owner_pts.get(pair, ())) + len(d1_entry_pts.get(pair, ())) for pair in pairs)
        entries.append(
            {
                "name": mg_name,
                "kind": "alias_or_group",
                "targets": t_strs,
                "variants": len(pairs),
                "d0_points": d0_pts_cnt,
                "has_d1": d1_cnt > 0,
                "d1_refs": d1_cnt,
                "aliases": list(grp.alias_items.get(mg_name, [])),
            }
        )

    filtered: List[Dict[str, Any]] = []
    q_low = search.lower() if search else None
    q_stripped = q_low.replace("_", "") if q_low else None
    for ent in entries:
        if has_d1 and not ent["has_d1"]:
            continue
        if multi_variant and ent["variants"] < 2:
            continue
        if q_low:
            hay = [ent["name"]] + ent["targets"] + ent["aliases"]
            if not any(q_low in h.lower() or (q_stripped and q_stripped in h.lower().replace("_", "")) for h in hay):
                continue
        filtered.append(ent)

    total = len(filtered)
    shown = filtered if limit <= 0 else filtered[:limit]
    if as_json:
        emit_json({"total": total, "shown": len(shown), "moves": shown})
        return 0

    print(f"Moves ({len(shown)}/{total}):")
    for ent in shown:
        t_s = ",".join(ent["targets"])
        d1_s = f" d1={ent['d1_refs']}" if ent["has_d1"] else ""
        al_s = f" aliases={','.join(ent['aliases'])}" if ent["aliases"] else ""
        print(f"  {ent['name']:<48s} [{t_s}] d0={ent['d0_points']:3d}{d1_s}{al_s}")
    if len(shown) < total:
        print(f"... showing {len(shown)}/{total} moves (use --limit 0 or refine --search)")
    return 0


def run_inspect(
    meta_path: str,
    groups_path: Optional[str],
    points: Optional[List[str]] = None,
    depth: Optional[int] = None,
    when_str: Optional[str] = None,
    move_name: Optional[str] = None,
    followup_move: Optional[str] = None,
    from_str: Optional[str] = None,
    compact: bool = False,
    limit: int = 20,
    as_json: bool = False,
) -> int:
    """Inspect full candidates, effects, states, and topology for selected points."""
    meta, grp, d0_by_node = _load_meta_and_groups(meta_path, groups_path)
    multi_sub = len(meta.sub_files) > 1

    if followup_move:
        depth = 1
    sel_terms: List[str] = []
    if depth is not None:
        sel_terms.append(f"d={depth}")
    elif followup_move or from_str:
        sel_terms.append("d=1")
    if followup_move:
        sel_terms.append(f"in={followup_move}")
    if from_str:
        sel_terms.append("from=(" + " ".join(split_selector_str(from_str)) + ")")
    if when_str:
        sel_terms.extend(split_selector_str(when_str))
    if move_name and depth != 1 and not followup_move and not any(t.startswith(("has=", "!has=")) for t in sel_terms):
        sel_terms.append(f"has={move_name}")

    cand_pts: List[MetaPoint] = []
    if points:
        parsed_specs: List[Tuple[Optional[int], Optional[int], int, Optional[int]]] = []
        for pspec in points:
            parsed = parse_point_id_spec(pspec)
            if parsed is None:
                raise ValueError(f"Invalid --point selector {pspec!r} (expected e.g. d0.n14.s22, d0.n14, d1.n87.s9)")
            parsed_specs.append(parsed)
        for pt in meta.d0_points + meta.d1_points:
            if any(
                (d_o is None or pt.depth == d_o)
                and (f_o is None or pt.owner_tf == f_o)
                and pt.node_idx == n_i
                and (s_o is None or pt.seg0 == s_o)
                for d_o, f_o, n_i, s_o in parsed_specs
            ):
                cand_pts.append(pt)
    else:
        cand_pts = list(meta.d1_points if (depth == 1 or followup_move or from_str) else meta.d0_points)

    matched: List[Tuple[MetaPoint, Optional[List[int]]]] = []
    partial_multipath: List[str] = []
    use_sel = bool(sel_terms)
    for pt in cand_pts:
        callers_out: Optional[List[int]] = list(pt.callers) if pt.depth == 1 else None
        if use_sel:
            pt_terms = list(sel_terms)
            if pt.depth == 1 and depth is None and not any(t.startswith(("d=", "in=", "node=", "from=")) for t in pt_terms):
                pt_terms = ["d=1"] + pt_terms
            ok, callers = eval_rule_selectors(pt_terms, pt, meta, grp, d0_by_node)
            if not ok:
                if len(pt.states) >= 2 and any(
                    eval_rule_selectors(pt_terms, dataclasses.replace(pt, states=[st]), meta, grp, d0_by_node)[0]
                    for st in pt.states
                ):
                    partial_multipath.append(format_point_id(pt, multi_sub=multi_sub))
                continue
            callers_out = callers
        elif move_name and not match_move_specs_at_point(move_name, pt, meta, grp):
            continue
        matched.append((pt, callers_out))

    total = len(matched)
    shown = matched if limit <= 0 else matched[:limit]
    if as_json:
        emit_json(
            {
                "total": total,
                "shown": len(shown),
                "partial_multipath": partial_multipath,
                "points": [_point_to_json_dict(pt, meta, multi_sub=multi_sub, callers_override=cs) for pt, cs in shown],
            }
        )
        return 0

    print(f"Inspect ({len(shown)}/{total} points):")
    if partial_multipath:
        pm_s = ", ".join(partial_multipath[:8]) + (" ..." if len(partial_multipath) > 8 else "")
        print(f"Note: {len(partial_multipath)} multi-path point(s) partially matched (skipped for safety): {pm_s}")
    for pt, cs in shown:
        pid = format_point_id(pt, multi_sub=multi_sub)
        if pt.depth == 0:
            v_s = "[" + ", ".join(">".join(str(x) for x in v) for v in pt.vias) + "]"
            hdr_extra = f"parents={list(pt.parents)} via={v_s}"
        else:
            om = meta.target_to_move.get((pt.owner_tf, pt.owner_tn), "")
            om_s = f"({om})" if om else ""
            eo_s = f" entry_owners={list(pt.entry_owners)}" if pt.entry_owners else ""
            c_list = cs if cs is not None else list(pt.callers)
            hdr_extra = f"owner={pt.owner_tf}:{pt.owner_tn}{om_s}{eo_s} callers={c_list}"
        cands_desc = [describe_cand(c, pt, meta) for c in pt.cands if c.weight > 0]
        if compact:
            st0 = format_state_brief(pt.states[0]) if pt.states else ""
            mp_s = f" (paths={len(pt.states)})" if len(pt.states) >= 2 else ""
            print(f"  {pid} {hdr_extra} | {st0}{mp_s}")
            print(f"    cands: {', '.join(format_cand_compact(cd) for cd in cands_desc)}")
        else:
            print(f"\n[{pid}] nid={pt.node_nid} {hdr_extra}")
            for si, st in enumerate(pt.states):
                p_lbl = f"path[{si}]: " if len(pt.states) >= 2 else "state:   "
                print(f"  {p_lbl}{format_state_brief(st)}")
            print("  cands:")
            for cd in cands_desc:
                print(f"    - {format_cand_compact(cd)}")
    if len(shown) < total:
        print(f"\n... showing {len(shown)}/{total} points (use --limit 0 or narrow --when)")
    return 0


def run_switches(
    meta_path: str,
    groups_path: Optional[str],
    depth: Optional[int] = None,
    when_str: Optional[str] = None,
    limit: int = 50,
    as_json: bool = False,
) -> int:
    """List decision switches (RANDOM/ENDR/ELSER or multi-segment body effects) cleanly."""
    meta, grp, d0_by_node = _load_meta_and_groups(meta_path, groups_path)
    multi_sub = len(meta.sub_files) > 1
    when_terms = split_selector_str(when_str) if when_str else []

    pool: List[MetaPoint] = []
    if depth != 1:
        pool.extend(pt for pt in meta.d0_points if is_switch_point(pt))
    if depth != 0:
        pool.extend(pt for pt in meta.d1_points if is_switch_point(pt))

    matched: List[Tuple[MetaPoint, Optional[List[int]]]] = []
    for pt in pool:
        if when_terms:
            has_from = any(t.startswith("from=") for t in when_terms)
            if pt.depth == 0:
                if has_from:
                    continue
                ok, _ = eval_rule_selectors(["d=0"] + when_terms, pt, meta, grp, d0_by_node)
                if not ok:
                    continue
                matched.append((pt, None))
            else:
                pt_terms = list(when_terms)
                if not any(t.startswith("d=") for t in pt_terms):
                    pt_terms = ["d=1"] + pt_terms
                ok, cs = eval_rule_selectors(pt_terms, pt, meta, grp, d0_by_node)
                if not ok and not has_from and not any(t.startswith(("in=", "d=")) for t in when_terms):
                    ok, cs = eval_rule_selectors(["d=1", "from=(" + " ".join(when_terms) + ")"], pt, meta, grp, d0_by_node)
                if not ok:
                    continue
                matched.append((pt, cs))
        else:
            matched.append((pt, list(pt.callers) if pt.depth == 1 else None))

    total = len(matched)
    shown = matched if limit <= 0 else matched[:limit]
    if as_json:
        emit_json(
            {
                "total": total,
                "shown": len(shown),
                "switches": [_point_to_json_dict(pt, meta, multi_sub=multi_sub, callers_override=cs) for pt, cs in shown],
            }
        )
        return 0

    print(f"Switch Points ({len(shown)}/{total}):")
    for pt, cs in shown:
        pid = format_point_id(pt, multi_sub=multi_sub)
        st0 = format_state_brief(pt.states[0]) if pt.states else ""
        if pt.depth == 0:
            v_s = "[" + ", ".join(">".join(str(x) for x in v) for v in pt.vias) + "]"
            ctx = f"parents={list(pt.parents)} via={v_s}"
        else:
            om = meta.target_to_move.get((pt.owner_tf, pt.owner_tn), "")
            ctx = f"owner={pt.owner_tf}:{pt.owner_tn}({om}) callers={cs if cs is not None else list(pt.callers)}"
        cands_s = " | ".join(format_cand_compact(describe_cand(c, pt, meta)) for c in pt.cands if c.weight > 0)
        print(f"  {pid:<14s} {ctx} | {st0}")
        print(f"    {cands_s}")
    if len(shown) < total:
        print(f"... showing {len(shown)}/{total} switches (use --limit 0 or narrow --when)")
    return 0


def run_followups(
    meta_path: str,
    groups_path: Optional[str],
    owner_move: Optional[str] = None,
    from_str: Optional[str] = None,
    when_str: Optional[str] = None,
    limit: int = 50,
    as_json: bool = False,
) -> int:
    """List all d=1 sub-THK blocks grouped by owner, supporting owner/entry_owner/from/when filters."""
    meta, grp, d0_by_node = _load_meta_and_groups(meta_path, groups_path)
    multi_sub = len(meta.sub_files) > 1
    owner_pool: Optional[Set[Tuple[int, int]]] = None
    if owner_move:
        owner_pool, _ = expand_move_names_and_targets(owner_move, meta, grp)

    sel_terms: List[str] = ["d=1"]
    if from_str:
        sel_terms.append("from=(" + " ".join(split_selector_str(from_str)) + ")")
    if when_str:
        sel_terms.extend(split_selector_str(when_str))
    use_sel = bool(from_str or when_str)

    hits: List[Tuple[MetaPoint, str, List[int]]] = []
    for pt in meta.d1_points:
        role = "owner"
        if owner_pool is not None:
            is_owner = (pt.owner_tf, pt.owner_tn) in owner_pool
            is_entry = any(
                (int(eo.split(":", 1)[0]), int(eo.split(":", 1)[1])) in owner_pool
                for eo in pt.entry_owners
                if ":" in eo
            )
            if not is_owner and not is_entry:
                continue
            role = "owner" if is_owner else "entry_owner"
        callers = list(pt.callers)
        if use_sel:
            ok, cs = eval_rule_selectors(sel_terms, pt, meta, grp, d0_by_node)
            if not ok:
                continue
            if cs is not None:
                callers = cs
        hits.append((pt, role, callers))

    total = len(hits)
    shown = hits if limit <= 0 else hits[:limit]
    if as_json:
        items = []
        for pt, role, callers in shown:
            d = _point_to_json_dict(pt, meta, multi_sub=multi_sub, callers_override=callers)
            d["match_role"] = role
            d["all_callers"] = list(pt.callers)
            items.append(d)
        emit_json({"total": total, "shown": len(shown), "followups": items})
        return 0

    print(f"Followups d=1 ({len(shown)}/{total}):")
    by_owner: Dict[Tuple[int, int, str], List[Tuple[MetaPoint, str, List[int]]]] = defaultdict(list)
    for pt, role, callers in shown:
        om = meta.target_to_move.get((pt.owner_tf, pt.owner_tn), "")
        by_owner[(pt.owner_tf, pt.owner_tn, om)].append((pt, role, callers))
    for (otf, otn, om), items in by_owner.items():
        eo_set = sorted({eo for pt, _, _ in items for eo in pt.entry_owners})
        eo_disp = [f"{eo}({meta.target_to_move.get((int(eo.split(':')[0]), int(eo.split(':')[1])), '')})" for eo in eo_set if ":" in eo]
        eo_s = f" entry_owners=[{', '.join(eo_disp)}]" if eo_disp else ""
        print(f"\n[owner {otf}:{otn} {om}]{eo_s}")
        for pt, role, callers in items:
            pid = format_point_id(pt, multi_sub=multi_sub)
            st0 = format_state_brief(pt.states[0]) if pt.states else ""
            cands_s = ", ".join(format_cand_compact(describe_cand(c, pt, meta)) for c in pt.cands if c.weight > 0)
            role_s = f" match={role}" if owner_move else ""
            cal_s = f"matched_callers={callers} (all={list(pt.callers)})" if from_str else f"callers={callers}"
            print(f"  {pid:<12s}{role_s} {cal_s} | {st0}")
            print(f"    -> {cands_s}")
    if len(shown) < total:
        print(f"\n... showing {len(shown)}/{total} followups (use --limit 0)")
    return 0


def run_diff(
    meta_path: str,
    groups_path: Optional[str],
    a_spec: Optional[str] = None,
    b_spec: Optional[str] = None,
    move_name: Optional[str] = None,
    depth: Optional[int] = None,
    as_json: bool = False,
) -> int:
    """Find minimal discriminating predicates/topology between two point sets or move variants."""
    meta, grp, d0_by_node = _load_meta_and_groups(meta_path, groups_path)
    multi_sub = len(meta.sub_files) > 1

    if bool(move_name) == bool(a_spec or b_spec):
        raise ValueError("Specify either `--move <MOVE>` OR both `--a <SPEC>` and `--b <SPEC>`")
    if (a_spec and not b_spec) or (b_spec and not a_spec):
        raise ValueError("Both `--a` and `--b` are required when comparing selectors")

    if move_name:
        targets, mnames = expand_move_names_and_targets(move_name, meta, grp)
        t_sorted = sorted(targets)
        variant_rows: List[Dict[str, Any]] = []
        for tf, tn in t_sorted:
            info = dict(meta.subnode_info.get((tf, tn), {}))
            canon = meta.target_to_move.get((tf, tn), info.get("move", ""))
            pts = [pt for pt in meta.d0_points if any(c.weight > 0 and c.tf == tf and c.tn == tn for c in pt.cands)]
            sts = [st for pt in pts for st in pt.states]
            if sts:
                psets = [set(st.other_preds) for st in sts]
                common = set.intersection(*psets)
                union = set.union(*psets)
            else:
                common, union = set(), set()
            variant_rows.append(
                {
                    "target": f"{tf}:{tn}",
                    "canonical_move": canon,
                    "subnode": info,
                    "d0_point_ids": [format_point_id(pt, multi_sub=multi_sub) for pt in pts],
                    "d0_nodes": sorted({pt.node_idx for pt in pts}),
                    "common_preds": common,
                    "any_preds": union,
                }
            )
        co_pts: List[str] = []
        if len(t_sorted) >= 2:
            t_set = set(t_sorted)
            for pt in meta.d0_points:
                present = {(c.tf, c.tn) for c in pt.cands if c.weight > 0 and (c.tf, c.tn) in t_set}
                if len(present) >= 2:
                    co_pts.append(format_point_id(pt, multi_sub=multi_sub))
        for i, row in enumerate(variant_rows):
            other_any = set().union(*(r["any_preds"] for j, r in enumerate(variant_rows) if j != i)) if len(variant_rows) > 1 else set()
            row["strict_unique_preds"] = sorted(row["common_preds"] - other_any)
            row["common_preds_sorted"] = sorted(row["common_preds"])
            row["any_preds_sorted"] = sorted(row["any_preds"])

        payload = {
            "mode": "move_variants",
            "move": move_name,
            "cooccurring_d0_points": co_pts,
            "variants": [
                {
                    "target": r["target"],
                    "canonical_move": r["canonical_move"],
                    "d0_points": len(r["d0_point_ids"]),
                    "d0_nodes": r["d0_nodes"],
                    "subnode_checks": r["subnode"].get("checks", []),
                    "subnode_chains": r["subnode"].get("chains", []),
                    "subnode_var_writes": r["subnode"].get("var_writes", []),
                    "common_preds": r["common_preds_sorted"],
                    "strict_unique_preds": r["strict_unique_preds"],
                }
                for r in variant_rows
            ],
        }
        if as_json:
            emit_json(payload)
            return 0
        print(f"Diff Move Variants: '{move_name}' ({len(variant_rows)} targets, co-occurring d0 points={len(co_pts)})")
        if co_pts:
            print(f"  Co-occurring d=0 points (both in same block): {', '.join(co_pts[:10])}" + (" ..." if len(co_pts) > 10 else ""))
        for r in payload["variants"]:
            print(f"\n  [{r['target']}] canonical={r['canonical_move']} d0_pts={r['d0_points']} nodes={r['d0_nodes']}")
            print(f"    strict_unique_d0_preds: {r['strict_unique_preds']}")
            print(f"    common_d0_preds:        {r['common_preds']}")
            if r["subnode_checks"]:
                print(f"    subnode_checks:         {r['subnode_checks']}")
            if r["subnode_chains"]:
                print(f"    subnode_chains:         {r['subnode_chains']}")
            if r["subnode_var_writes"]:
                print(f"    subnode_var_writes:     {r['subnode_var_writes']}")
        return 0

    def _select_pts(spec: str) -> List[MetaPoint]:
        pid = parse_point_id_spec(spec)
        if pid is not None:
            d_o, f_o, n_i, s_o = pid
            pts = [
                pt
                for pt in (meta.d0_points + meta.d1_points)
                if (d_o is None or pt.depth == d_o)
                and (f_o is None or pt.owner_tf == f_o)
                and pt.node_idx == n_i
                and (s_o is None or pt.seg0 == s_o)
            ]
            if d_o is None and depth is not None:
                pts = [pt for pt in pts if pt.depth == depth]
            return pts
        terms = split_selector_str(spec)
        is_d1 = depth == 1 or any(t.startswith(("d=1", "in=", "from=")) for t in terms)
        if depth is not None and not any(t.startswith("d=") for t in terms):
            terms = [f"d={depth}"] + terms
        elif is_d1 and not any(t.startswith("d=") for t in terms):
            terms = ["d=1"] + terms
        target_pool = meta.d1_points if is_d1 else meta.d0_points
        return [pt for pt in target_pool if eval_rule_selectors(terms, pt, meta, grp, d0_by_node)[0]]

    pts_a = _select_pts(a_spec or "")
    pts_b = _select_pts(b_spec or "")
    sts_a = [st for pt in pts_a for st in pt.states]
    sts_b = [st for pt in pts_b for st in pt.states]

    a_all = set.intersection(*(set(st.other_preds) for st in sts_a)) if sts_a else set()
    b_all = set.intersection(*(set(st.other_preds) for st in sts_b)) if sts_b else set()
    a_any = set.union(*(set(st.other_preds) for st in sts_a)) if sts_a else set()
    b_any = set.union(*(set(st.other_preds) for st in sts_b)) if sts_b else set()

    common_preds = sorted(a_all & b_all)
    a_strict = sorted(a_all - b_any)
    b_strict = sorted(b_all - a_any)
    a_partial = sorted((a_all - b_all) - set(a_strict))
    b_partial = sorted((b_all - a_all) - set(b_strict))

    def _dist_strs(sts: Sequence[PathState]) -> List[str]:
        raw = {(st.dist_lo, "inf" if st.dist_hi is None else str(st.dist_hi)) for st in sts}
        return [f"[{lo}, {hi})" for lo, hi in sorted(raw, key=lambda t: (t[0], INF if t[1] == "inf" else int(t[1])))]

    def _angle_union_str(sts: Sequence[PathState]) -> str:
        acc: AngleIntervals = ()
        for st in sts:
            acc = angle_union(acc, st.angles)
        return format_angle_intervals(acc)

    def _cand_moves(pts: Sequence[MetaPoint]) -> Set[str]:
        out: Set[str] = set()
        for pt in pts:
            for c in pt.cands:
                if c.weight > 0:
                    out.add(describe_cand(c, pt, meta)["move"])
        return out

    a_dists = _dist_strs(sts_a)
    b_dists = _dist_strs(sts_b)
    a_ang = _angle_union_str(sts_a)
    b_ang = _angle_union_str(sts_b)
    a_parents = sorted({p for pt in pts_a for p in pt.parents})
    b_parents = sorted({p for pt in pts_b for p in pt.parents})
    a_vias = sorted({">".join(str(x) for x in v) for pt in pts_a for v in pt.vias})
    b_vias = sorted({">".join(str(x) for x in v) for pt in pts_b for v in pt.vias})
    a_moves = _cand_moves(pts_a)
    b_moves = _cand_moves(pts_b)
    has_semantic_sep = bool(a_strict or b_strict or a_dists != b_dists or a_ang != b_ang)
    verdict = "semantic_separators" if has_semantic_sep else "topology_only"

    payload = {
        "mode": "point_sets",
        "verdict": verdict,
        "a": {
            "spec": a_spec,
            "points": len(pts_a),
            "nodes": sorted({pt.node_idx for pt in pts_a}),
            "dists": a_dists,
            "angles": a_ang,
            "parents": a_parents,
            "vias": a_vias,
            "strict_only_preds": a_strict,
            "partial_only_preds": a_partial,
            "unique_moves": sorted(a_moves - b_moves),
        },
        "b": {
            "spec": b_spec,
            "points": len(pts_b),
            "nodes": sorted({pt.node_idx for pt in pts_b}),
            "dists": b_dists,
            "angles": b_ang,
            "parents": b_parents,
            "vias": b_vias,
            "strict_only_preds": b_strict,
            "partial_only_preds": b_partial,
            "unique_moves": sorted(b_moves - a_moves),
        },
        "common_preds": common_preds,
    }
    if as_json:
        emit_json(payload)
        return 0

    print(f"Diff: A=({a_spec}) [{len(pts_a)} pts] vs B=({b_spec}) [{len(pts_b)} pts] | verdict={verdict}")
    print(f"  common_preds ({len(common_preds)}): {common_preds}")
    print(f"  A nodes={payload['a']['nodes']} dists={a_dists} angle={a_ang} parents={a_parents} via={a_vias}")
    print(f"    A strict_only_preds:  {a_strict}")
    if a_partial:
        print(f"    A partial_only_preds: {a_partial}")
    if payload["a"]["unique_moves"]:
        print(f"    A unique_moves:       {payload['a']['unique_moves']}")
    print(f"  B nodes={payload['b']['nodes']} dists={b_dists} angle={b_ang} parents={b_parents} via={b_vias}")
    print(f"    B strict_only_preds:  {b_strict}")
    if b_partial:
        print(f"    B partial_only_preds: {b_partial}")
    if payload["b"]["unique_moves"]:
        print(f"    B unique_moves:       {payload['b']['unique_moves']}")
    return 0


def run_coverage(
    preset_path: str,
    meta_path: Optional[str],
    groups_path: Optional[str],
    when_str: Optional[str] = None,
    depth: Optional[int] = None,
    tags_str: Optional[str] = None,
    all_tags: bool = False,
    uncovered_only: bool = False,
    limit: int = 50,
    as_json: bool = False,
) -> int:
    """Audit static coverage, uncovered gaps, and shadowing of a `.preset.toml`."""
    preset_dir = os.path.dirname(os.path.abspath(preset_path))
    with open(preset_path, "rb") as f:
        doc = tomllib.load(f)

    if meta_path is None:
        m_ref = doc.get("meta")
        if not m_ref:
            raise ValueError("Preset missing `meta` key and --meta not specified")
        meta_path = os.path.join(preset_dir, m_ref if str(m_ref).endswith(".toml") else f"{m_ref}.thkmeta.toml")
    if groups_path is None and doc.get("groups"):
        g_ref = doc["groups"]
        groups_path = os.path.join(preset_dir, g_ref if str(g_ref).endswith(".toml") else f"{g_ref}.groups.toml")

    meta, grp, d0_by_node = _load_meta_and_groups(meta_path, groups_path)
    multi_sub = len(meta.sub_files) > 1
    dt = doc.get("disable_tag", [])
    disabled_default = {str(x).strip().lower() for x in (dt if isinstance(dt, list) else str(dt).split(",")) if str(x).strip()}
    explicit_enabled = (
        {x.strip().lower() for x in tags_str.split(",") if x.strip()}
        if tags_str is not None
        else None
    )

    group_id_table: Dict[str, int] = {str(k): int(v) for k, v in doc.get("group_ids", doc.get("stats", {})).items()}
    auto_group_ids: Dict[str, int] = {}
    next_auto_group_box = [2100]

    when_terms = split_selector_str(when_str) if when_str else []
    has_from_in_when = any(t.startswith(("from=", "in=")) for t in when_terms)

    d0_scope: List[MetaPoint] = []
    if depth != 1 and not any(t.startswith(("d=1", "in=", "from=")) for t in when_terms):
        for pt in meta.d0_points:
            if when_terms:
                ok, _ = eval_rule_selectors(["d=0"] + when_terms, pt, meta, grp, d0_by_node)
                if not ok:
                    continue
            d0_scope.append(pt)

    d1_scope: List[Tuple[MetaPoint, Set[int]]] = []
    if depth != 0 and not any(t == "d=0" for t in when_terms):
        for pt in meta.d1_points:
            if not when_terms:
                d1_scope.append((pt, set(pt.callers)))
                continue
            pt_terms = list(when_terms)
            if not any(t.startswith("d=") for t in pt_terms):
                pt_terms = ["d=1"] + pt_terms
            ok, cs = eval_rule_selectors(pt_terms, pt, meta, grp, d0_by_node)
            if ok:
                d1_scope.append((pt, set(cs) if cs is not None else set(pt.callers)))
            elif not has_from_in_when and not any(t.startswith("d=") for t in when_terms):
                ok_f, cs_f = eval_rule_selectors(["d=1", "from=(" + " ".join(when_terms) + ")"], pt, meta, grp, d0_by_node)
                if ok_f and cs_f:
                    d1_scope.append((pt, set(cs_f)))

    active_rules: List[ParsedPresetRule] = []
    skipped_rules = 0
    for idx, rule in enumerate(doc.get("rules", []), start=1):
        parsed = parse_preset_rule(
            rule, idx, group_id_table, auto_group_ids, next_auto_group_box, meta=meta, grp=grp
        )
        if all_tags:
            is_active = True
        elif explicit_enabled is not None:
            is_active = (not parsed.tags) or (parsed.tags <= explicit_enabled)
        else:
            is_active = not bool(parsed.tags & disabled_default)
        if is_active:
            active_rules.append(parsed)
        else:
            skipped_rules += 1

    d0_winner: Dict[Tuple[int, int], Tuple[ParsedPresetRule, str]] = {}
    d0_all_hits: Dict[Tuple[int, int], List[Tuple[ParsedPresetRule, str]]] = defaultdict(list)
    d1_caller_winner: Dict[Tuple[int, int, int], Dict[int, Tuple[ParsedPresetRule, str]]] = defaultdict(dict)
    d1_all_hits: Dict[Tuple[int, int, int, int], List[ParsedPresetRule]] = defaultdict(list)
    zero_match_rules: List[str] = []
    all_banned_only_rules: List[str] = []
    empty_fallback_rules: List[str] = []
    unused_action_specs: List[str] = []
    shadowed_hits: List[str] = []
    d0_ban_noop: Dict[Tuple[int, int], List[str]] = defaultdict(list)
    d1_ban_noop: Dict[Tuple[int, int, int, int], List[str]] = defaultdict(list)

    for pr in active_rules:
        raw_hits = 0
        kept_hits = 0
        all_banned_pts: List[str] = []
        track_sp = pr.action_kind in ("ban", "allow")
        spec_hits_cov: Optional[Dict[str, int]] = ({sp: 0 for sp in pr.action_specs} if track_sp else None)
        for pt in meta.d0_points:
            ok, _ = eval_rule_selectors(pr.sel_terms, pt, meta, grp, d0_by_node)
            if ok:
                prev_ab = len(all_banned_pts)
                rhs = resolve_action_at_point(
                    pr,
                    pt,
                    meta,
                    grp,
                    all_banned_out=all_banned_pts,
                    multi_sub=multi_sub,
                    spec_hits_out=spec_hits_cov,
                )
                key0 = (pt.node_idx, pt.seg0)
                prior = d0_all_hits[key0]
                is_cov = any((not p_r.has_gate) and (p_r.tags <= pr.tags) for p_r, _ in prior)
                if rhs is not None:
                    raw_hits += 1
                    if pr.fallback and is_cov:
                        continue
                    kept_hits += 1
                    if not pr.fallback and key0 in d0_winner and is_cov:
                        shadowed_hits.append(f"{format_point_id(pt)}:{pr.rule_desc}")
                    prior.append((pr, rhs))
                    if key0 not in d0_winner or (d0_winner[key0][0].has_gate and not pr.has_gate):
                        d0_winner[key0] = (pr, rhs)
                elif pr.action_kind == "ban" and len(all_banned_pts) == prev_ab:
                    if not (pr.fallback and is_cov):
                        if pr.rule_desc not in d0_ban_noop[key0]:
                            d0_ban_noop[key0].append(pr.rule_desc)
        for pt in meta.d1_points:
            ok, callers = eval_rule_selectors(pr.sel_terms, pt, meta, grp, d0_by_node)
            if ok and callers:
                prev_ab = len(all_banned_pts)
                rhs = resolve_action_at_point(
                    pr,
                    pt,
                    meta,
                    grp,
                    all_banned_out=all_banned_pts,
                    multi_sub=multi_sub,
                    spec_hits_out=spec_hits_cov,
                )
                key1 = (pt.owner_tf, pt.node_idx, pt.seg0)
                if rhs is not None:
                    raw_hits += 1
                    cmap = d1_caller_winner[key1]
                    rem_here = 0
                    for cid in callers:
                        prior1 = d1_all_hits[(pt.owner_tf, pt.node_idx, pt.seg0, cid)]
                        is_cov1 = any((not p_r.has_gate) and (p_r.tags <= pr.tags) for p_r in prior1)
                        if pr.fallback and is_cov1:
                            continue
                        rem_here += 1
                        if not pr.fallback and cid in cmap and is_cov1:
                            shadowed_hits.append(f"{format_point_id(pt, multi_sub=multi_sub)}(caller={cid}):{pr.rule_desc}")
                        prior1.append(pr)
                        if cid not in cmap or (cmap[cid][0].has_gate and not pr.has_gate):
                            cmap[cid] = (pr, rhs)
                    if rem_here > 0:
                        kept_hits += 1
                elif pr.action_kind == "ban" and len(all_banned_pts) == prev_ab:
                    for cid in callers:
                        prior1 = d1_all_hits[(pt.owner_tf, pt.node_idx, pt.seg0, cid)]
                        is_cov1 = any((not p_r.has_gate) and (p_r.tags <= pr.tags) for p_r in prior1)
                        if not (pr.fallback and is_cov1):
                            bn_list = d1_ban_noop[(pt.owner_tf, pt.node_idx, pt.seg0, cid)]
                            if pr.rule_desc not in bn_list:
                                bn_list.append(pr.rule_desc)
        if spec_hits_cov is not None and raw_hits > 0:
            for sp in pr.action_specs:
                if spec_hits_cov.get(sp, 0) == 0:
                    unused_action_specs.append(f"{pr.rule_desc}:{sp}")
        if raw_hits == 0:
            if all_banned_pts:
                all_banned_only_rules.append(pr.rule_desc)
            else:
                zero_match_rules.append(pr.rule_desc)
        elif pr.fallback and kept_hits == 0:
            empty_fallback_rules.append(pr.rule_desc)

    d0_covered_entries: List[Dict[str, Any]] = []
    d0_uncovered_entries: List[Dict[str, Any]] = []
    for pt in d0_scope:
        key0 = (pt.node_idx, pt.seg0)
        if key0 in d0_winner:
            w_r, w_rhs = d0_winner[key0]
            d0_covered_entries.append(
                {
                    "point": format_point_id(pt),
                    "node": pt.node_idx,
                    "seg0": pt.seg0,
                    "rule": w_r.idx,
                    "label": w_r.short_lbl,
                    "rhs": w_rhs,
                    "gated": w_r.has_gate,
                }
            )
        else:
            ent0 = _point_to_json_dict(pt, meta)
            bn0 = d0_ban_noop.get(key0, [])
            if bn0:
                ent0["ban_noop_by"] = list(bn0)
            d0_uncovered_entries.append(ent0)

    d1_total_units = 0
    d1_covered_units = 0
    d1_covered_entries: List[Dict[str, Any]] = []
    d1_uncovered_entries: List[Dict[str, Any]] = []
    for pt, sc_callers in d1_scope:
        key1 = (pt.owner_tf, pt.node_idx, pt.seg0)
        cmap = d1_caller_winner.get(key1, {})
        sc_sorted = sorted(sc_callers)
        cov_c = [cid for cid in sc_sorted if cid in cmap]
        uncov_c = [cid for cid in sc_sorted if cid not in cmap]
        d1_total_units += len(sc_sorted)
        d1_covered_units += len(cov_c)
        if cov_c:
            first_r, first_rhs = cmap[cov_c[0]]
            d1_covered_entries.append(
                {
                    "point": format_point_id(pt, multi_sub=multi_sub),
                    "owner": f"{pt.owner_tf}:{pt.owner_tn}",
                    "covered_callers": cov_c,
                    "rule": first_r.idx,
                    "label": first_r.short_lbl,
                    "rhs": first_rhs,
                }
            )
        if uncov_c or not sc_sorted:
            ent = _point_to_json_dict(pt, meta, multi_sub=multi_sub, callers_override=uncov_c)
            ent["uncovered_callers"] = uncov_c
            bn1: List[str] = []
            for cid in uncov_c:
                for desc in d1_ban_noop.get((pt.owner_tf, pt.node_idx, pt.seg0, cid), ()):
                    if desc not in bn1:
                        bn1.append(desc)
            if bn1:
                ent["ban_noop_by"] = bn1
            d1_uncovered_entries.append(ent)

    all_uncovered = d0_uncovered_entries + d1_uncovered_entries
    shown_uncov = all_uncovered if limit <= 0 else all_uncovered[:limit]
    ban_noop_uncovered_count = sum(1 for e in all_uncovered if e.get("ban_noop_by"))

    dim_values: Dict[str, List[str]] = {}
    for gk in grp.groups:
        if "." not in gk:
            continue
        dim, val = gk.split(".", 1)
        if dim in ("range", "dir", "quest"):
            continue
        dim_values.setdefault(dim, []).append(val)

    group_breakdown: Dict[str, List[Dict[str, Any]]] = {}
    for dim, vals in dim_values.items():
        if len(vals) < 2:
            continue
        rows: List[Dict[str, Any]] = []
        for val in vals:
            tot = 0
            cov = 0
            bn = 0
            for pt in d0_scope:
                m_g, _ = eval_rule_selectors([f"{dim}={val}"], pt, meta, grp, d0_by_node)
                if not m_g:
                    continue
                tot += 1
                key0 = (pt.node_idx, pt.seg0)
                if key0 in d0_winner:
                    cov += 1
                elif d0_ban_noop.get(key0):
                    bn += 1
            if tot > 0:
                rows.append(
                    {
                        "value": val,
                        "covered": cov,
                        "total": tot,
                        "pct": round(100.0 * cov / tot, 1),
                        "ban_noop": bn,
                    }
                )
        if rows:
            group_breakdown[dim] = rows

    payload = {
        "preset": os.path.basename(preset_path),
        "mode": "all_tags" if all_tags else (f"tags={tags_str}" if tags_str is not None else f"default(disabled={sorted(disabled_default)})"),
        "when": when_str or "*",
        "active_rules": len(active_rules),
        "skipped_rules": skipped_rules,
        "d0_covered": len(d0_covered_entries),
        "d0_total": len(d0_scope),
        "d0_uncovered": len(d0_uncovered_entries),
        "d1_covered_units": d1_covered_units,
        "d1_total_units": d1_total_units,
        "d1_uncovered_points": len(d1_uncovered_entries),
        "shadowed_hits": shadowed_hits,
        "zero_match_rules": zero_match_rules,
        "all_banned_only_rules": all_banned_only_rules,
        "empty_fallback_rules": empty_fallback_rules,
        "unused_action_specs": unused_action_specs,
        "ban_noop_uncovered_count": ban_noop_uncovered_count,
        "group_breakdown": group_breakdown,
        "covered_summary": [] if uncovered_only else d0_covered_entries + d1_covered_entries,
        "uncovered_total": len(all_uncovered),
        "uncovered_shown": len(shown_uncov),
        "uncovered": shown_uncov,
    }
    if as_json:
        emit_json(payload)
        return 0

    print(
        f"Coverage [{payload['preset']}] mode={payload['mode']} when=({payload['when']}) | "
        f"rules={len(active_rules)} active ({skipped_rules} skipped)"
    )
    print(
        f"  d=0: {len(d0_covered_entries)}/{len(d0_scope)} covered ({len(d0_uncovered_entries)} uncovered, "
        f"{ban_noop_uncovered_count} ban-noop) | "
        f"d=1 (pt,caller): {d1_covered_units}/{d1_total_units} covered ({len(d1_uncovered_entries)} pts w/ uncovered callers)"
    )
    if group_breakdown:
        print("  Group coverage (d=0):")
        for dim, rows in group_breakdown.items():
            row_strs = [
                f"{r['value']} {r['covered']}/{r['total']} ({r['pct']:.0f}%"
                + (f", {r['ban_noop']} ban-noop" if r["ban_noop"] else "")
                + ")"
                for r in rows
            ]
            print(f"    {dim:<8s}: " + " | ".join(row_strs))
    if shadowed_hits:
        print(f"  Shadowed hits ({len(shadowed_hits)}): {', '.join(shadowed_hits[:8])}" + (" ..." if len(shadowed_hits) > 8 else ""))
    if zero_match_rules:
        print(f"  Zero-match active rules ({len(zero_match_rules)}): {', '.join(zero_match_rules)}")
    if all_banned_only_rules:
        print(f"  All-banned-only active rules ({len(all_banned_only_rules)}): {', '.join(all_banned_only_rules)}")
    if empty_fallback_rules:
        print(f"  Empty fallback rules ({len(empty_fallback_rules)}): {', '.join(empty_fallback_rules)}")
    if unused_action_specs:
        print(f"  Unused action specs ({len(unused_action_specs)}): {', '.join(unused_action_specs)}")

    if not uncovered_only and (d0_covered_entries or d1_covered_entries):
        print("Covered summary:")
        by_node_rule: Dict[Tuple[int, int, str, str], List[int]] = defaultdict(list)
        for ce in d0_covered_entries:
            by_node_rule[(ce["node"], ce["rule"], ce["label"], ce["rhs"] + (" [gated]" if ce["gated"] else ""))].append(ce["seg0"])
        for (nidx, ridx, rlbl, r_rhs), segs in sorted(by_node_rule.items()):
            se = ",".join(format_seg0_ranges(segs, meta.node_segs_d0.get(nidx, segs)))
            print(f"  d0.n{nidx} seg0={se} <- rule#{ridx} ({rlbl}) -> {r_rhs}")
        for ce in d1_covered_entries:
            print(f"  {ce['point']} callers={ce['covered_callers']} <- rule#{ce['rule']} ({ce['label']}) -> {ce['rhs']}")

    print(f"Uncovered points ({len(shown_uncov)}/{len(all_uncovered)}):")
    for ue in shown_uncov:
        st_raw = ue["states"][0] if ue["states"] else None
        if st_raw:
            d_s = f"[{st_raw['dist'][0]}, {st_raw['dist'][1]})"
            d3_s = f" dist3d=[{st_raw['dist3d'][0]}, {st_raw['dist3d'][1]})" if "dist3d" in st_raw else ""
            p_s = ", ".join(st_raw.get("preds", []))
            st_str = f"dist={d_s}{d3_s} angle={st_raw['angle']} preds=[{p_s}]"
        else:
            st_str = ""
        top_cands = ", ".join(format_cand_compact(c) for c in ue["cands"][:3])
        if len(ue["cands"]) > 3:
            top_cands += f", ...(+{len(ue['cands']) - 3})"
        if ue["depth"] == 0:
            ctx = f"parents={ue['parents']} via={ue['via']}"
        else:
            ctx = f"owner={ue['owner']}({ue['owner_move']}) uncov_callers={ue.get('uncovered_callers', ue['callers'])}"
        print(f"  {ue['point']:<13s} {ctx} | {st_str}")
        if ue.get("ban_noop_by"):
            print(f"    ban-noop: {', '.join(ue['ban_noop_by'])}")
        print(f"    cands: {top_cands}")
    if len(shown_uncov) < len(all_uncovered):
        print(f"... showing {len(shown_uncov)}/{len(all_uncovered)} uncovered points (use --limit 0 or narrow --when)")
    return 0


# =============================================================================
# CLI Entry Point
# =============================================================================


def _parse_sub_thk_args(
    sub_args: Optional[List[str]],
    main_path: str,
    em_id: str,
    main_idx: int,
    thk_main: ThkFile,
) -> Dict[int, ThkFile]:
    if sub_args:
        out: Dict[int, ThkFile] = {}
        for item in sub_args:
            if "=" in item:
                idx_s, p = item.split("=", 1)
                idx = int(idx_s)
                out[idx] = load_thk_file(p, file_idx=idx)
            else:
                tf = load_thk_file(item)
                out[tf.file_idx] = tf
        return out
    referenced_tfs = {
        sg.tf for nd in thk_main.nodes for sg in nd.segs if sg.tf >= 0 and sg.tf != main_idx
    }
    discovered = discover_sibling_sub_thks(main_path, em_id, main_idx)
    out: Dict[int, ThkFile] = {}
    for fidx in sorted(referenced_tfs):
        if fidx in discovered:
            out[fidx] = load_thk_file(discovered[fidx], file_idx=fidx)
        else:
            print(
                f"[WARNING] Referenced sub-THK file index {fidx} ({em_id}_{fidx:02d}.thk) not found beside {main_path}",
                file=sys.stderr,
            )
    return out


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="MIRA Generic Multi-Monster THK Meta CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_lt = sub.add_parser("list-tables", help="List all auto-discovered monster ActionTables in a PE dump")
    p_lt.add_argument("--dump", required=True)

    p_ea = sub.add_parser("export-actions", help="Export a monster ActionTable from PE dump to JSON")
    p_ea.add_argument("--dump", required=True)
    p_ea.add_argument("--em-id", required=True, help="Monster ID prefix such as em001, em001_01, ems001, ...")
    p_ea.add_argument("--va", default=None, help="Pin exact ActionTable VA (hex or dec) to resolve multi-table matches")
    p_ea.add_argument("-o", "--out", required=True)

    p_ext = sub.add_parser("extract", help="Extract <em_id>.thkmeta.toml from main + sub THK files")
    p_ext.add_argument("--main", required=True, help="Path to main THK file (e.g. *_00.thk)")
    p_ext.add_argument("--sub", action="append", default=None, help="Sub-THK path or <idx>=<path> (repeatable; referenced siblings auto-discovered if omitted)")
    p_ext.add_argument("--em-id", default=None, help="Override inferred monster string ID")
    p_ext.add_argument("--default-files", default=None, help="Comma-separated d=0 file indices (default: <main_idx>)")
    p_ext.add_argument("--dump", default=None, help="Unpacked PE dump for ActionTable auto-discovery")
    p_ext.add_argument("--va", default=None, help="Pin exact ActionTable VA (hex or dec) when using --dump")
    p_ext.add_argument("--actions", default=None, help="Portable actions JSON file from export-actions")
    p_ext.add_argument("--no-actions", action="store_true", help="Proceed without action names (use act_<id>)")
    p_ext.add_argument("--enum-ops", default=None, help="Comma-separated exclusive enum opcodes (e.g. 0x101,0x103)")
    p_ext.add_argument("-o", "--out", required=True)

    p_init = sub.add_parser("init-groups", help="Generate editable .groups.toml scaffold from .thkmeta.toml")
    p_init.add_argument("--meta", required=True)
    p_init.add_argument("-o", "--out", required=True)

    p_cmp = sub.add_parser("compile", help="Standalone compile .preset.toml + .thkmeta.toml -> rng_thk .txt")
    p_cmp.add_argument("preset")
    p_cmp.add_argument("--meta", default=None)
    p_cmp.add_argument("--groups", default=None)
    p_cmp.add_argument("--explain", action="store_true")
    p_cmp.add_argument("-o", "--out", required=True)

    p_qry = sub.add_parser("query", help="Interactive static selector / Move->[nodes] query")
    p_qry.add_argument("--meta", required=True)
    p_qry.add_argument("--groups", default=None)
    p_qry.add_argument("--d", type=int, default=None)
    p_qry.add_argument("--when", default=None)
    p_qry.add_argument("--move", default=None)
    p_qry.add_argument("--followup", default=None)
    p_qry.add_argument("--in", dest="in_move", default=None)
    p_qry.add_argument("--from", dest="from_when", default=None)

    p_ov = sub.add_parser("overview", help="Compact schema, predicate vocabulary, and distance/group summary")
    p_ov.add_argument("--meta", required=True)
    p_ov.add_argument("--groups", default=None)
    p_ov.add_argument("--json", action="store_true")

    p_mv = sub.add_parser("moves", help="Search moves/aliases or inspect deep variant/d0/d1 usage for one move")
    p_mv.add_argument("--meta", required=True)
    p_mv.add_argument("--groups", default=None)
    p_mv.add_argument("--search", default=None)
    p_mv.add_argument("--detail", default=None)
    p_mv.add_argument("--has-d1", action="store_true")
    p_mv.add_argument("--multi-variant", action="store_true")
    p_mv.add_argument("--limit", type=int, default=60)
    p_mv.add_argument("--json", action="store_true")

    p_ins = sub.add_parser("inspect", help="Inspect full candidates, effects, states, and topology for selected points")
    p_ins.add_argument("--meta", required=True)
    p_ins.add_argument("--groups", default=None)
    p_ins.add_argument("--point", action="append", default=None, help="Point ID selector like d0.n<idx>.s<seg> (repeatable)")
    p_ins.add_argument("--d", type=int, default=None)
    p_ins.add_argument("--when", default=None)
    p_ins.add_argument("--move", default=None)
    p_ins.add_argument("--followup", default=None)
    p_ins.add_argument("--from", dest="from_when", default=None)
    p_ins.add_argument("--compact", action="store_true")
    p_ins.add_argument("--limit", type=int, default=20)
    p_ins.add_argument("--json", action="store_true")

    p_sw = sub.add_parser("switches", help="List optional RANDOM/ENDR/ELSER switches and their body effects")
    p_sw.add_argument("--meta", required=True)
    p_sw.add_argument("--groups", default=None)
    p_sw.add_argument("--d", type=int, default=None)
    p_sw.add_argument("--when", default=None)
    p_sw.add_argument("--limit", type=int, default=50)
    p_sw.add_argument("--json", action="store_true")

    p_fu = sub.add_parser("followups", help="Inspect d=1 sub-THK blocks grouped by owner and entry_owners")
    p_fu.add_argument("--meta", required=True)
    p_fu.add_argument("--groups", default=None)
    p_fu.add_argument("--owner", default=None)
    p_fu.add_argument("--from", dest="from_when", default=None)
    p_fu.add_argument("--when", default=None)
    p_fu.add_argument("--limit", type=int, default=50)
    p_fu.add_argument("--json", action="store_true")

    p_df = sub.add_parser("diff", help="Compare two point sets/selectors or variants of a move to find minimal separators")
    p_df.add_argument("--meta", required=True)
    p_df.add_argument("--groups", default=None)
    p_df.add_argument("--a", dest="a_spec", default=None)
    p_df.add_argument("--b", dest="b_spec", default=None)
    p_df.add_argument("--move", default=None)
    p_df.add_argument("--d", type=int, default=None)
    p_df.add_argument("--json", action="store_true")

    p_cov = sub.add_parser("coverage", help="Audit preset static coverage, uncovered points, and shadowing")
    p_cov.add_argument("preset")
    p_cov.add_argument("--meta", default=None)
    p_cov.add_argument("--groups", default=None)
    p_cov.add_argument("--when", default=None)
    p_cov.add_argument("--d", type=int, default=None)
    p_cov.add_argument("--tags", default=None)
    p_cov.add_argument("--all-tags", action="store_true")
    p_cov.add_argument("--uncovered-only", action="store_true")
    p_cov.add_argument("--limit", type=int, default=50)
    p_cov.add_argument("--json", action="store_true")

    p_ver = sub.add_parser("verify", help="Verify runtime equivalence of two compiled/low-level presets")
    p_ver.add_argument("orig")
    p_ver.add_argument("compiled")
    p_ver.add_argument("--main", required=True, help="Path to main THK file (e.g. *_00.thk)")
    p_ver.add_argument("--sub", action="append", default=None, help="Sub-THK path or <idx>=<path> (repeatable; referenced siblings auto-discovered if omitted)")
    p_ver.add_argument("--files", default=None, help="Comma-separated d=0 file indices to test (default: <main_idx>)")
    p_ver.add_argument("--tag-mode", action="append", default=None, help="Optional explicit tag mode name=tag1,tag2 (repeatable)")

    args = ap.parse_args()

    if args.cmd == "list-tables":
        tables = scan_all_action_tables_from_dump(args.dump)
        print(f"Discovered {len(tables)} ActionTables in {args.dump}:")
        for tbl in tables:
            top = ", ".join(f"{k}({v})" for k, v in list(tbl["prefixes"].items())[:4])
            print(f"  VA=0x{tbl['va']:x} count={tbl['count']:3d} prefixes=[{top}]")
        return 0

    if args.cmd == "export-actions":
        pin_va = int(args.va, 0) if args.va else None
        acts, tbl = discover_action_table_from_dump(args.dump, args.em_id, explicit_va=pin_va)
        payload = {
            "em_id": args.em_id,
            "va": f"0x{tbl['va']:x}",
            "count": tbl["count"],
            "prefixes": tbl["prefixes"],
            "actions": {str(k): v for k, v in sorted(acts.items()) if v},
        }
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Exported {len(payload['actions'])} named actions (table VA=0x{tbl['va']:x}) -> {args.out}")
        return 0

    if args.cmd == "extract":
        thk_main = load_thk_file(args.main)
        inferred_em, main_idx = infer_em_id_and_file_idx(args.main, thk_main.monster_id)
        em_id = args.em_id or inferred_em
        sub_thks = _parse_sub_thk_args(args.sub, args.main, em_id, main_idx, thk_main)

        if args.default_files:
            default_files = [int(x) for x in args.default_files.split(",") if x.strip()]
        else:
            default_files = [main_idx]
        default_files_str = ",".join(str(x) for x in default_files)

        if args.actions:
            actions = load_actions_json(args.actions)
        elif args.dump:
            pin_va = int(args.va, 0) if args.va else None
            actions, tbl = discover_action_table_from_dump(args.dump, em_id, explicit_va=pin_va)
            print(f"Auto-discovered ActionTable for {em_id}: VA=0x{tbl['va']:x}, entries={tbl['count']}")
        elif args.no_actions:
            actions = {}
        else:
            raise ValueError("Specify one of --actions <json>, --dump <pe>, or --no-actions")

        enum_ops: Set[int] = set()
        if args.enum_ops:
            for item in args.enum_ops.split(","):
                item = item.strip()
                if item:
                    enum_ops.add(int(item, 0))

        mg, t2p, sprof, s0, s1, d0, d1 = extract_model(thk_main, sub_thks, actions, default_files_str, enum_ops)
        text = render_thkmeta_toml(
            thk_main, sub_thks, em_id, default_files, actions, mg, t2p, sprof, s0, s1, d0, d1, enum_ops=enum_ops
        )
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(
            f"Extracted TOML {args.out}: em={em_id}(mon={thk_main.monster_id}) subs={sorted(sub_thks.keys())} "
            f"unique_moves={len(mg)}, targets={len(t2p)}, d0_points={len(d0)}, d1_points={len(d1)}"
        )
        return 0

    if args.cmd == "init-groups":
        text = render_groups_scaffold_toml(args.meta)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"Scaffolded groups TOML {args.out}")
        return 0

    if args.cmd == "compile":
        text = compile_preset_toml(args.preset, args.meta, args.groups, explain=args.explain)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"Compiled {args.preset} -> {args.out}")
        return 0

    if args.cmd == "query":
        return run_query(
            args.meta, args.groups, args.d, args.when, args.move, args.in_move, args.from_when, args.followup
        )

    if args.cmd == "overview":
        return run_overview(args.meta, args.groups, as_json=args.json)

    if args.cmd == "moves":
        return run_moves(
            args.meta,
            args.groups,
            search=args.search,
            detail=args.detail,
            has_d1=args.has_d1,
            multi_variant=args.multi_variant,
            limit=args.limit,
            as_json=args.json,
        )

    if args.cmd == "inspect":
        return run_inspect(
            args.meta,
            args.groups,
            points=args.point,
            depth=args.d,
            when_str=args.when,
            move_name=args.move,
            followup_move=args.followup,
            from_str=args.from_when,
            compact=args.compact,
            limit=args.limit,
            as_json=args.json,
        )

    if args.cmd == "switches":
        return run_switches(
            args.meta,
            args.groups,
            depth=args.d,
            when_str=args.when,
            limit=args.limit,
            as_json=args.json,
        )

    if args.cmd == "followups":
        return run_followups(
            args.meta,
            args.groups,
            owner_move=args.owner,
            from_str=args.from_when,
            when_str=args.when,
            limit=args.limit,
            as_json=args.json,
        )

    if args.cmd == "diff":
        return run_diff(
            args.meta,
            args.groups,
            a_spec=args.a_spec,
            b_spec=args.b_spec,
            move_name=args.move,
            depth=args.d,
            as_json=args.json,
        )

    if args.cmd == "coverage":
        return run_coverage(
            args.preset,
            args.meta,
            args.groups,
            when_str=args.when,
            depth=args.d,
            tags_str=args.tags,
            all_tags=args.all_tags,
            uncovered_only=args.uncovered_only,
            limit=args.limit,
            as_json=args.json,
        )

    if args.cmd == "verify":
        thk_m = load_thk_file(args.main)
        em_id, main_idx = infer_em_id_and_file_idx(args.main, thk_m.monster_id)
        sub_thks = _parse_sub_thk_args(args.sub, args.main, em_id, main_idx, thk_m)
        main_files = (
            [int(x) for x in args.files.split(",") if x.strip()]
            if args.files
            else [main_idx]
        )
        explicit_modes: Optional[Dict[str, Set[str]]] = None
        if args.tag_mode:
            explicit_modes = {}
            for tm in args.tag_mode:
                mname, _, tags_s = tm.partition("=")
                explicit_modes[mname.strip()] = {x.strip().lower() for x in tags_s.split(",") if x.strip()}
        return verify_equivalence(args.orig, args.compiled, args.main, sub_thks, main_files, explicit_modes)

    return 1


if __name__ == "__main__":
    sys.exit(main())
