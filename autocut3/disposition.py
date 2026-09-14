#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""disposition.py —— 缺陷台账聚合器（v2-①落地件：检测→落库→下游消费，治"假环节"）。

输入（全部已落盘，零新检测发明）：
  · dossier.detect_takes      词级重说段定位（素材词轨）
  · transcribe._dup_len 产物   transcript.scripted_dup ≥12 = 念稿/重录指纹
  · vision defects            visual.defects（blur/shake/exposure/framing/still）
  · 黑区检测                   asr.vad_speech_spans（物理测量）vs 词轨覆盖——能量有语音、
                              词轨无文本 = 口令/嘟囔黑区（词窗±0.8s 漂移容差，v5 实锤漂移 0~3s
                              的教训：容差不够会误报，容差过了会漏报——0.8s 是 VAD 页级精度的保守折中）

产物：projects/<pid>/disposition.json
  {materials: {mid: {verdict: clean/flag/avoid, items: [{type, t, evidence, suggestion}]}}, ...}
只检测+建议区间，**不做破坏性处置**（剪辑决策归 draft/人）；留痕可审计。
消费方：draft 提示词（避开区间）、Studio 素材档案面板（人可见）、编排器 disposition 环节。

用法：
  Agent : python3 autocut3/disposition.py <project>
  自动  : orchestrate.py --advance 到 disposition 环节
  人    : Studio 素材卡 ⚠ 标记（读取本文件）
"""
import argparse, datetime, hashlib, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr
import dossier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIFT_TOL = 0.8  # 词轨漂移容差（v5 实锤素材 ASR 偏移 0.15~3s；黑区判定只要求"无词覆盖"且能量为语音）


def _jload(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def _pack_files(pid):
    pdir = os.path.join(ROOT, "projects", pid)
    lib = _jload(f"{pdir}/materials/library.json", {})
    packs = lib.get("packs") or _jload(f"{pdir}/project.json", {}).get("material_packs") or []
    out = []
    for pk in packs:
        pj = _jload(f"{ROOT}/materials/packs/{pk}/pack.json", {})
        for f in pj.get("files", []):
            out.append((pk, f))
    return out


def _wav_cache(pack_id, fname):
    """转写缓存 wav 路径（与 asr.transcribe 同规则——缓存缺失返回 None，黑区跳过不硬算）。"""
    src_dir = os.path.join(ROOT, "materials", "packs", pack_id)
    dh = hashlib.md5(os.path.dirname(os.path.join(src_dir, fname)).encode("utf-8")).hexdigest()[:8]
    stem = os.path.basename(fname).rsplit(".", 1)[0]
    wav = os.path.join(ROOT, "materials", ".audio_cache", f"{dh}_{stem}.wav")
    return wav if os.path.exists(wav) else None


def black_zones(pack_id, f):
    """VAD 语音段中无词轨覆盖的区间（口令/嘟囔）。返回 [{t:[a,b]}] 或 []（无缓存/无词轨→[]）。"""
    tr = f.get("transcript") or {}
    words = tr.get("words") or []
    wav = _wav_cache(pack_id, f.get("file", ""))
    if not words or not wav:
        return []
    try:
        spans = asr.vad_speech_spans(wav)
    except Exception:
        return []
    zones = []
    for s, e in spans:
        if e - s < 0.3:
            continue
        covered = any(w["end"] + DRIFT_TOL >= s and w["start"] - DRIFT_TOL <= e
                      for w in words if w.get("start") is not None and w.get("end") is not None)
        if not covered:
            zones.append([round(s, 1), round(e, 1)])
    return zones


def assess_material(pack_id, f):
    """单素材缺陷项清单。每项 {type, t, evidence, suggestion}（t 可为 null=无窗口的标记）。"""
    mid = f.get("id")
    items = []
    tr = f.get("transcript") or {}
    v = f.get("visual") or {}
    words = tr.get("words") or []

    # 0) 人工标记缺陷（素材卡「＋缺陷」一手记录——2026-09-15 展示收敛：原始 defects 并进台账聚合，
    #    前端只读一个视图；此处消费使人工标记与自动检测同级进 draft 提示词）
    for d in f.get("defects") or []:
        try:
            _at = float(d.get("at"))
            _t = [round(_at, 1), round(_at + 0.5, 1)]
        except (TypeError, ValueError):
            _t = None
        items.append({"type": "manual-" + str(d.get("type", "note")), "t": _t,
                      "evidence": str(d.get("note", ""))[:80],
                      "suggestion": "人工标记缺陷：选段避开该时刻或掐剪"})

    # 1) 重说段（词级定位，可直接避开）
    for t in dossier.detect_takes([w for w in words if w.get("text") not in "，。！？、"]):
        items.append({"type": "retake-suspect", "t": t["t"],
                      "evidence": t.get("note", "相邻 n-gram 重复"),
                      "suggestion": "避开该窗口选净段（重说一般后半句更顺）"})
    # 2) 念稿/重录指纹
    dup = tr.get("scripted_dup") or 0
    if dup >= 12:
        items.append({"type": "scripted-dup", "t": None,
                      "evidence": "台词 ≥%d 字逐字重复（念稿/重录指纹）" % dup,
                      "suggestion": "词轨是主资产——按 voiceover 用（旁白音轨源），画面不作 A 轨主体"})
    # 3) 视觉缺陷
    for d in v.get("defects") or []:
        items.append({"type": "visual-" + str(d.get("type", "other")), "t": d.get("t"),
                      "evidence": str(d.get("note", ""))[:80],
                      "suggestion": "避开该时段或只取无缺陷净段"})
    # 4) 黑区（拍摄口令/嘟囔）
    for z in black_zones(pack_id, f):
        items.append({"type": "black-zone", "t": z,
                      "evidence": "VAD 语音段 %.1f-%.1fs 无词轨覆盖（口令/嘟囔/漏转写）" % (z[0], z[1]),
                      "suggestion": "选段避开该区间的 src_in；已在窗口内则掐头尾重裁"})
    return mid, items


def build(pid):
    """聚合全部挂载素材 → projects/<pid>/disposition.json。返回报告 dict。"""
    pdir = os.path.join(ROOT, "projects", pid)
    materials = {}
    n_flag = n_avoid = 0
    for pack_id, f in _pack_files(pid):
        mid, items = assess_material(pack_id, f)
        if mid is None:
            continue
        if not items:
            materials[mid] = {"verdict": "clean", "items": []}
            continue
        has_window = any(it.get("t") for it in items)
        materials[mid] = {"verdict": "avoid" if has_window else "flag", "items": items}
        n_avoid += has_window
        n_flag += 1
    report = {
        "project": pid, "schema": 1,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "drift_tol": DRIFT_TOL,
        "summary": {"materials": len(materials), "clean": sum(1 for m in materials.values() if m["verdict"] == "clean"),
                    "flag": n_flag, "avoid": n_avoid},
        "materials": materials,
        "note": "只检测+建议区间，不做破坏性处置；剪辑决策归 draft/人。",
    }
    os.makedirs(pdir, exist_ok=True)
    with open(f"{pdir}/disposition.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    return report


def step_status(pdir):
    """编排器环节判据：disposition.json 存在且新于其消费的 pack.json。"""
    rp = f"{pdir}/disposition.json"
    if not os.path.exists(rp):
        return ("pending", "待生成缺陷台账（orchestrate --advance 或 disposition.py）")
    try:
        r = _jload(rp, {})
        s = r.get("summary", {})
        fresh = True
        lib = _jload(f"{pdir}/materials/library.json", {})
        for pk in (lib.get("packs") or []):
            pp = f"{ROOT}/materials/packs/{pk}/pack.json"
            if os.path.exists(pp) and os.path.getmtime(pp) > os.path.getmtime(rp):
                fresh = False
        detail = "缺陷台账: %d 素材 / %d 有建议 / %d 有避开窗口%s" % (
            s.get("materials", 0), s.get("flag", 0), s.get("avoid", 0),
            "" if fresh else "（⚠ 素材已更新，建议重新生成）")
        return ("done", detail)
    except Exception as e:
        return ("failed", "台账解析失败: %s" % str(e)[:80])


def lines_for_prompt(pid, limit=12):
    """draft 提示词消费：有避开窗/标记的素材一行摘要。"""
    rp = f"{ROOT}/projects/{pid}/disposition.json"
    if not os.path.exists(rp):
        return ""
    r = _jload(rp, {})
    out = []
    for mid, m in (r.get("materials") or {}).items():
        if m.get("verdict") == "clean":
            continue
        bits = []
        for it in (m.get("items") or [])[:3]:
            t = it.get("t")
            bits.append(("[%s-%ss]%s" % (t[0], t[1], it["type"])) if t else it["type"])
        out.append("- %s：%s —— %s" % (mid, "、".join(bits), (m["items"][0].get("suggestion") or "")[:50]))
    if not out:
        return ""
    return ("【缺陷台账——选段必须避开下列窗口，标记类素材按其建议用法】\n" + "\n".join(out[:limit]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="缺陷台账聚合器（检测→落库→留痕，不破坏性处置）")
    ap.add_argument("project")
    a = ap.parse_args()
    r = build(a.project)
    s = r["summary"]
    print("DISPOSITION ✓ %s | %d 素材：干净 %d / 有标记 %d / 有避开窗口 %d" % (
        f"projects/{a.project}/disposition.json", s["materials"], s["clean"], s["flag"], s["avoid"]))
    for mid, m in r["materials"].items():
        if m["verdict"] != "clean":
            marks = "、".join((("[%s-%ss]" % (it["t"][0], it["t"][1]) if it.get("t") else "") + it["type"])
                              for it in m["items"][:4])
            print("  ⚠ %s [%s] %s" % (mid, m["verdict"], marks))
