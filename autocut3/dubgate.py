"""配音裁剪门禁（2026-09-15 维护者项目实锤：按素材词轨裁 dub 边界 → 上一句尾音残留 / 本句句首被吞）。
素材 ASR 词轨绝对时间偏移 0.15~3s 不等——裁剪边界不能只信词轨，必须对裁出音频本体做 ASR 闭环：
  D1 句首完整  首词 == story 首字（开头残留上一句 → 首词是别字）
  D2 句尾完整  末词窗口含 story 末字（尾部截断吞字 → 末词缺失）
  D3 内容完整  去标点相似度 ≥ 0.9（漏句/错段）
任一不过 exit 1。渲染前跑：python3 autocut3/dubgate.py <project> [--story sid]
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project"); ap.add_argument("--story", default=None)
    a = ap.parse_args()
    pdir = os.path.join("projects", a.project)
    sfile = os.path.join(pdir, "storylines", "%s.json" % a.story) if a.story else os.path.join(pdir, "storyline.json")
    sl = json.load(open(sfile))
    cache = os.path.join(pdir, "materials", "dub", ".asrcache")
    os.makedirs(cache, exist_ok=True)
    fails = []
    for b in sl.get("beats", []):
        nar = b.get("narration") or {}
        if nar.get("mode") != "dub":
            continue
        df = nar.get("audio")
        if not os.path.isabs(df): df = os.path.join(pdir, df)
        story = re.sub(r"\s", "", b.get("story") or "")
        r = asr.transcribe(df, cache_dir=cache)
        txt = re.sub(r"[，。！？、,.!?;；\s]", "", r.get("text") or "")
        ws = r.get("words") or []
        d1 = bool(ws) and ws[0]["text"] == story[0] if story else False
        d2 = story[-1] in "".join(w["text"] for w in ws[-4:]) if ws and story else False
        import difflib
        d3 = difflib.SequenceMatcher(None, txt, re.sub(r"[，。！？、,.!?;；\s]", "", story)).ratio() >= 0.9
        ok = d1 and d2 and d3
        f0 = "%s@%.2f" % (ws[0]["text"], ws[0]["start"]) if ws else "—"
        print("幕%s %s | D1句首[%s]%s D2句尾%s D3相似%.2f | %s" % (
            b.get("no"), "✓" if ok else "✗ FAIL", f0, "✓" if d1 else "✗",
            "✓" if d2 else "✗", d3, (r.get("text") or "")[:30]))
        if not ok:
            fails.append(b.get("no"))
    if fails:
        print("门禁不过：幕 %s 配音残留/吞字——重裁后复检" % fails); sys.exit(1)
    print("dubgate ✓ 全部配音干净")

if __name__ == "__main__":
    main()
