#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
素材库：原始素材的权威解读落盘（素材事实的 SSOT）。

原则（维护者，2026-09-10）：
- library.json 只记录【素材本身的事实】与【拍摄层质量决策】（usable：说错/卡等/重复/气口）；
- "这段素材在某部片里怎么用"（A-roll/B-roll、取哪一段）是创作决策，住在 storyline.json。
  同一批素材，功能介绍片里当 A-roll，纪录片里可能压旁白当 B-roll——档案不变，取用变。

自动解读管线（本模块是落盘 sink，各环节逐步接入）：
  语音提取（faster-whisper → cuts.json，已有）→ 文字校对（Agent）→ 画面识别（minimax understand）
  → 拍摄层废片决策 → library.json（权威解读，后续一切创作只认这里）
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asr import ffmpeg_path as _ffmpeg_path, ffprobe_path as _ffprobe_path  # 平台解析链（硬规则 8）
FFP = _ffprobe_path()
FF = _ffmpeg_path()

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def r01(x):
    """时间量化规则：0.1s 一位小数（与词级时间戳规范一致），禁止浮点尾数落盘。"""
    return None if x is None else round(float(x) * 10) / 10

def ensure_thumbs(project_dir):
    """为每条 cut 抽缩略帧（素材审阅必须看得到素材本身）。缺哪张补哪张。"""
    lib = load(f"{project_dir}/materials/library.json")
    src = f"{project_dir}/materials/src.mp4"
    if not os.path.exists(src):
        return 0
    tdir = f"{project_dir}/materials/thumbs"
    os.makedirs(tdir, exist_ok=True)
    made = 0
    for c in lib.get("cuts", []):
        out = f"{tdir}/cut_{c['cut_index']}.jpg"
        if os.path.exists(out):
            continue
        at = min(float(c.get("in") or 0) + 0.1, max(0, float(c.get("duration") or 3) - 0.2))
        subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(at),
                        "-i", src, "-frames:v", "1", "-vf", "scale=270:-2", "-q:v", "3", out],
                       capture_output=True)
        made += 1
    return made

def seed(project_dir):
    """从既有 ASR 成果（cuts.json）种子化素材档案，之后由 Agent 审计增量更新。"""
    cuts = load(f"{project_dir}/materials/cuts.json")["cuts"]
    src = "materials/src.mp4"
    abspath = f"{project_dir}/{src}"
    dur = None
    if os.path.exists(FFP) and os.path.exists(abspath):
        r = subprocess.run([FFP, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", abspath], capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            dur = round(float(r.stdout.strip()), 2)
        except ValueError:
            pass
    entries = []
    for i, c in enumerate(cuts):
        sin = c.get("start", c.get("in"))
        eout = c.get("end", c.get("out"))
        entries.append({
            "cut_index": i, "src": src,
            "in": r01(sin), "out": r01(eout),
            "duration": r01(eout - sin) if (sin is not None and eout is not None) else None,
            "text": (c.get("text") or "")[:80],
            "usable": True,          # 拍摄层质量决策（唯一下沉到档案的"决策"）
            "defects": [],           # [{at, type, note}] type: 说错/卡等/重复/气口/…
            "audit": {
                "transcript": "faster-whisper 权威转写（v1 迁移）",  # 已落盘
                "proofread": "pending",   # 文字校对（Agent）
                "visual": "pending"       # 画面识别（minimax understand）
            }
        })
    lib = {
        "version": 1,
        "sources": [{"file": src, "duration": r01(dur), "note": "原始素材"}],
        "time_rule": "0.1s 量化（与词级时间戳规范一致）；展示格式 分:秒.十分",
        "cuts": entries,
        "principle": ("usable 只评拍摄层（说错/卡等/重复/气口）；"
                      "创作取用（A/B-roll、取舍）在 storyline.json 决策——两层不混。")
    }
    # 合并保护：已有档案中的人工决策（usable/defects/audit）不被种子化覆盖
    if os.path.exists(f"{project_dir}/materials/library.json"):
        oldmap = {c.get("cut_index"): c for c in load(f"{project_dir}/materials/library.json").get("cuts", [])}
        for e in lib["cuts"]:
            o = oldmap.get(e["cut_index"])
            if o:
                e["usable"] = o.get("usable", True)
                e["defects"] = o.get("defects", [])
                e["audit"] = o.get("audit", e["audit"])
    out = f"{project_dir}/materials/library.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=1)
    ensure_thumbs(project_dir)
    print(f"LIBRARY: {out}  cuts={len(entries)}  源时长={dur}s")

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    seed(sys.argv[2])
