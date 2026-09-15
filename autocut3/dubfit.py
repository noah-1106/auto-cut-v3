#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配音裁剪自动化（v2-③ dubfit）：包络定位 → 掐头掐尾 → ASR 闭环，吸收 dubgate D1-D3。

输入=故事线 dub 幕的原始录音（narration.audio），输出=干净 dub 音频 + 验证报告。
v6 人工裁剪的固化：掐头按 VAD 能量包络 0.1s 级定位（不信素材 ASR 词轨——那是素材的时间，
不是 dub 录音的时间）；闭环校验仍走 dubgate 同款的 D1-D3（首词/末词/相似度）。

裁剪策略（每幕最多 3 轮，ASR 调用 ≤3 次/幕）：
  轮0 包络：VAD 首span起点~末span终点，掐外侧静音/气息
  轮1 句首重锚：D1 不过（首词≠story 首字=开头残留上一句）→ 在原始录音词轨里找 story 首字
      锚点，回退裁点到锚点所在 VAD span 起点（锚点贴 span 头）或锚点前 0.08s
  轮2 句尾重锚：D2 不过（末词缺 story 末字=尾部截断/拖尾嘟囔）→ 找末字锚点，
      裁到锚点所在 span 终点（贴 span 尾）或锚点后 0.15s
产物：materials/dub/fit-<story>-b<no>.mp3 + 故事线 narration.audio 改指 fit 文件
      （narration.dubfit 留痕 {raw,start,end,d1,d2,d3}）+ dubfit-report.json
      + dubgate-report.json（passed——编排器 dubgate 环节读这份，D1-D3 同源）。
任一幕不过 exit 1（不过闸不渲染，硬规则 5）。

用法：python3 autocut3/dubfit.py <project> [--story sid]
"""
import argparse, difflib, json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr

PUNCT = r"[，。！？、,.!?;；:：\s]"


def _clean(s):
    return re.sub(PUNCT, "", s or "")


def gate_check(wav, story, cache):
    """D1-D3 闭环（与 dubgate.py 同判据）。返回 (ok, d1, d2, d3, asr_text, words)。"""
    r = asr.transcribe(wav, cache_dir=cache)
    txt = _clean(r.get("text") or "")
    ws = r.get("words") or []
    st = _clean(story)
    d1 = bool(ws) and bool(st) and ws[0]["text"] == st[0]
    d2 = bool(ws) and bool(st) and st[-1] in "".join(w["text"] for w in ws[-4:])
    d3 = difflib.SequenceMatcher(None, txt, st).ratio() >= 0.9 if st else False
    return (d1 and d2 and d3, d1, d2, d3, r.get("text") or "", ws)


def _cut(src, out, start, end):
    ff = asr.ffmpeg_path()
    subprocess.run([ff, "-y", "-loglevel", "error", "-ss", "%.3f" % start,
                    "-t", "%.3f" % (end - start), "-i", src,
                    "-c:a", "libmp3lame", "-b:a", "128k", out],
                   capture_output=True, check=True)
    return out


def _span_of(spans, t):
    for s, e in spans:
        if s - 0.05 <= t <= e + 0.05:
            return (s, e)
    return None


def fit_beat(raw, story, workdir, cache):
    """单幕裁剪。返回 dict(ok, start, end, rounds, d1, d2, d3, asr_text, fitted)。"""
    wav = os.path.join(workdir, "src.wav")
    asr.extract_audio(raw, wav)
    spans = asr.vad_speech_spans(wav)
    dur = asr.media_duration(wav)
    if not spans:
        return {"ok": False, "start": 0.0, "end": 0.0, "rounds": ["envelope"],
                "d1": False, "d2": False, "d3": False, "asr_text": "",
                "fitted": None, "reason": "VAD 无语音段"}
    start = max(0.0, spans[0][0] - 0.06)          # 包络轮：掐头（保 0.06s 前导防切辅音）
    end = min(dur, spans[-1][1] + 0.25)           # 掐尾（保 0.25s 句尾余韵）
    rounds = ["envelope"]
    fitted = os.path.join(workdir, "fit.mp3")
    _cut(wav, fitted, start, end)
    ok, d1, d2, d3, txt, _ws = gate_check(fitted, story, cache)

    if not ok and not (d1 and d2):
        # 重锚轮：对原始录音词轨找锚点（词轨时间=本文件内相对时间，仅作锚点提示，
        # 裁点落 VAD 能量边界——ASR 时间戳漂移由包络兜底，硬规则 1）
        rw = asr.transcribe(wav, cache_dir=cache)
        rwords = rw.get("words") or []
        st = _clean(story)
        if not d1 and st:
            anchor = next((w for w in rwords if w["text"] == st[0]), None)
            if anchor is not None:
                sp = _span_of(spans, anchor["start"])
                start = max(0.0, (sp[0] - 0.06) if (sp and anchor["start"] - sp[0] <= 0.45)
                            else anchor["start"] - 0.08)
                rounds.append("head-anchor@%s" % anchor["text"])
        if not d2 and st:
            tail = [w for w in rwords if st[-1] in w["text"]]
            if tail:
                anchor = tail[-1]
                sp = _span_of(spans, anchor["end"])
                end = min(dur, (sp[1] + 0.12) if (sp and sp[1] - anchor["end"] <= 0.45)
                          else anchor["end"] + 0.15)
                rounds.append("tail-anchor@%s" % anchor["text"])
        if start < end and rounds[-1] != "envelope":
            _cut(wav, fitted, start, end)
            ok, d1, d2, d3, txt, _ws = gate_check(fitted, story, cache)

    return {"ok": ok, "start": round(start, 3), "end": round(end, 3),
            "rounds": rounds, "d1": d1, "d2": d2, "d3": round(
                difflib.SequenceMatcher(None, _clean(txt), _clean(story)).ratio(), 3)
            if story else 0.0, "asr_text": txt[:60], "fitted": fitted if ok else None}


def main():
    ap = argparse.ArgumentParser(description="配音裁剪自动化（包络定位→掐头掐尾→ASR 闭环）")
    ap.add_argument("project")
    ap.add_argument("--story", default=None)
    a = ap.parse_args()
    pdir = os.path.join("projects", a.project)
    sfile = os.path.join(pdir, "storylines", "%s.json" % a.story) if a.story \
        else os.path.join(pdir, "storyline.json")
    sl = json.load(open(sfile, encoding="utf-8"))
    sid = a.story or os.path.splitext(os.path.basename(sfile))[0]
    cache = os.path.join(pdir, "materials", "dub", ".asrcache")
    os.makedirs(cache, exist_ok=True)
    workroot = os.path.join(pdir, "materials", "dub", ".fitcache")
    os.makedirs(workroot, exist_ok=True)

    beats = sl.get("beats") or []
    report, fails, changed = [], [], False
    for b in beats:
        nar = b.get("narration") or {}
        if nar.get("mode") != "dub":
            continue
        no = b.get("no")
        meta = nar.get("dubfit") or {}
        raw_rel = meta.get("raw") or nar.get("audio")
        if not raw_rel:
            fails.append(no); report.append({"no": no, "ok": False, "reason": "无 audio"})
            print("幕%s ✗ FAIL 无 audio" % no)
            continue
        raw = raw_rel if os.path.isabs(raw_rel) else os.path.join(pdir, raw_rel)
        if not os.path.exists(raw):
            fails.append(no); report.append({"no": no, "ok": False, "reason": "raw 缺失 " + raw_rel})
            print("幕%s ✗ FAIL raw 缺失 %s" % (no, raw_rel))
            continue
        story = b.get("story") or ""
        wd = os.path.join(workroot, "b%s" % no)
        os.makedirs(wd, exist_ok=True)
        r = fit_beat(raw, story, wd, cache)
        if r["ok"]:
            dubdir = os.path.join(pdir, "materials", "dub")
            fit_rel = "materials/dub/fit-%s-b%s.mp3" % (sid, no)
            fit_path = os.path.join(pdir, fit_rel)
            if os.path.exists(fit_path):
                os.unlink(fit_path)
            os.rename(r["fitted"], fit_path)
            nar["audio"] = fit_rel                       # 等长替换铁律不涉及（只动 audio/dubfit 两键）
            nar["dubfit"] = {"raw": raw_rel, "start": r["start"], "end": r["end"],
                             "rounds": r["rounds"], "d1": r["d1"], "d2": r["d2"], "d3": r["d3"]}
            changed = True
            print("幕%s ✓ %s | %s | D1%s D2%s D3%.2f | %s" % (
                no, "→" + fit_rel, "+".join(r["rounds"]),
                "✓" if r["d1"] else "✗", "✓" if r["d2"] else "✗", r["d3"], r["asr_text"][:24]))
        else:
            fails.append(no)
            nar["dubfit"] = {"raw": raw_rel, "start": r["start"], "end": r["end"],
                             "rounds": r["rounds"], "d1": r["d1"], "d2": r["d2"], "d3": r["d3"],
                             "ok": False}
            changed = True
            print("幕%s ✗ FAIL | %s | D1%s D2%s D3%.2f | %s" % (
                no, "+".join(r["rounds"]),
                "✓" if r["d1"] else "✗", "✓" if r["d2"] else "✗", r["d3"], r["asr_text"][:24]))
        report.append(dict({"no": no}, **{k: r[k] for k in
                                          ("ok", "start", "end", "rounds", "d1", "d2", "d3")}))

    if changed:  # 序列化保真：全量回写（json 透传所有未知字段，硬规则 4）
        json.dump(sl, open(sfile, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    passed = not fails
    json.dump({"story": sid, "passed": passed, "fails": fails, "beats": report},
              open(os.path.join(pdir, "dubfit-report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump({"passed": passed, "fails": fails, "source": "dubfit", "story": sid,
               "beats": [{"no": x["no"], "ok": x["ok"], "d1": x.get("d1"),
                          "d2": x.get("d2"), "d3": x.get("d3")} for x in report]},
              open(os.path.join(pdir, "dubgate-report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    if fails:
        print("dubfit ✗ 门禁不过：幕 %s 配音残留/吞字——重裁后复检" % fails)
        sys.exit(1)
    print("dubfit ✓ 全部配音干净（报告 dubfit-report.json）")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
