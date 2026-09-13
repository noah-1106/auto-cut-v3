# -*- coding: utf-8 -*-
"""dossier.py —— 素材档案合成器（L1 编排层的第一个消费者，零 API）。

输入：pack.json（transcript/visual/audit/几何） + storylines 取用登记 + config/lexicon.json（词表）
输出：projects/<pid>/dossier.json（判读层 assessment + timeline + 原始层带 src + 质量头）
纪律：纯确定性组装+互证；LLM 语义聚类/能量探针/抽样帧核验标 skipped 如实声明（D4/D8）；
     词表人工维护（lexicon.json 备注区），脚本只读。

用法：
  Agent : python3 autocut3/dossier.py <project>
  人    : 产物 projects/<pid>/dossier.json（Studio 素材卡第二刀接入）
"""
import json, os, re, sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUNCT = "，。！？、"


def _real_words(f):
    return [w for w in (f.get("transcript") or {}).get("words") or [] if w.get("text") not in PUNCT]


def _speech_segments(words, gap=0.6):
    segs, cur = [], None
    for w in words:
        if cur is None:
            cur = [w["start"], w["end"]]
        elif w["start"] - cur[1] <= gap:
            cur[1] = max(cur[1], w["end"])
        else:
            segs.append(cur)
            cur = [w["start"], w["end"]]
    if cur:
        segs.append(cur)
    return segs


# ---------- 互证矩阵（零 API；能量探针第二批工接入）----------
def xcheck_energy(words, dur):
    return {"check": "energy_vs_words", "status": "skipped", "reason": "能量探针未接入（第二批工）"}


def xcheck_ocr_desc(f, lex_terms=()):
    """OCR×desc：OCR 文字未进视觉叙述——词表专名漏提=suspect（品牌/楼盘名是内容一致性要害），普通文字漏提=note（不进警告）"""
    v = f.get("visual") or {}
    ocr = v.get("ocr") or []
    if ocr and isinstance(ocr[0], str):
        texts = []
        for s in ocr:
            m = re.match(r"([^（(]+)", s)
            if m:
                texts.append(m.group(1).strip())
    else:
        texts = [str(x) for x in ocr]
    narrative = (v.get("desc") or "") + "".join(
        (m.get("note") or "") for m in v.get("moments") or [])
    missed = [t for t in texts if t and t not in narrative]
    if not texts:
        return {"check": "ocr_in_desc", "status": "skipped", "reason": "无 OCR 产出"}
    hot = [t for t in missed if any(term in t for term in lex_terms if len(term) >= 2)]
    result = "suspect" if hot else ("note" if missed else "pass")
    detail = {"hot_miss": hot, "minor_miss": [t for t in missed if t not in hot]}
    return {"check": "ocr_in_desc", "status": "checked", "result": result, "detail": detail}


def xcheck_repeat(words, ngram=4):
    """相邻 n-gram 重叠 = 重说/口误检测（R9 素材级，首个命中即报）"""
    text = "".join(w["text"] for w in words)
    if len(text) < ngram * 2:
        return {"check": "repeat_ngram", "status": "skipped", "reason": "文本过短"}
    hits = []
    step = max(1, ngram // 2)
    for i in range(0, len(text) - ngram * 2, step):
        a = text[i:i + ngram]
        j = text.find(a, i + ngram)
        if j != -1 and j - i <= ngram * 3:
            hits.append({"gram": a, "at_chars": [i, j]})
            break
    return {"check": "repeat_ngram", "status": "checked",
            "result": "suspect" if hits else "pass",
            "detail": hits or "无相邻重复"}


# 宽容档锚点停用字：代词/助词/介词等高频虚词——专名首尾锚落在虚词上=大概率是普通语句碎片
# 正确构造：逐字符进集合（原写法 .split()[0] 把整串塞成单元素集合，恒真=停用词完全失效）
_ANCHOR_STOP = set("的了呢吧啊这那是和与或在被把从向对让给吗么呀哦嘛着过们之其此该各每另我你他她它咱")

def propernoun_vote(files, lex_terms):
    """跨素材同名词模糊窗口扫描：n>=3 的词表专名，位置级 >=n-1 字符相同且不等于专名 = ASR 变体嫌疑。
    （潭溪工馆 vs 檀溪公馆：4 字中 3 字同位 → 必中。2 字术语留给 proofread LLM，此处不扫防误报如"排骨~龙骨"）"""
    suspects, seen = [], set()
    # P1#3 修正：explained 的语义=窗口内含【完整术语】（"们的龙骨"含"龙骨"=正常语句）。
    # 不能用 2-gram 片段共享判定——变体天然与原词共享片段（窗帘箱~窗帘盒共享"窗帘"），会误杀真嫌疑。
    full_terms = {t for t in lex_terms if len(t) >= 2}
    for term in lex_terms:
        n = len(term)
        if n < 3:
            continue
        for f in files:
            text = "".join(w.get("text") or "" for w in _real_words(f))
            for i in range(0, max(0, len(text) - n + 1)):
                win = text[i:i + n]
                if win == term:
                    continue
                same = sum(1 for a, b in zip(win, term) if a == b)
                # 严格档：位置级 >=n-1 相同；宽容档（n>=4）：>=n-2 且锚点字为实字（首/尾锚不在虚词表）。
                # 宽容档依据=真实事故 潭溪工馆 vs 檀溪公馆（4 字错 2，同音替换常一次改多字）
                # 虚词锚排除："们的龙骨"vs"轻钢龙骨"（3/4 同+尾锚"骨"）为正常语句，必须排除
                anchor_ok = (win[0] == term[0] and win[0] not in _ANCHOR_STOP) or \
                            (win[-1] == term[-1] and win[-1] not in _ANCHOR_STOP)
                # 子词解释性排除（P1#3：原只在 loose 分支生效，strict 真嫌疑如"潭溪工馆"含子词"梅合"被误杀）：
                # "们的龙骨"含连续子词"龙骨"→相似性被更短术语解释=正常语句；strict 档同样适用
                explained = any(t in win for t in full_terms)  # 窗口内含完整术语=正常语句（strict/loose 双档适用）
                loose = n >= 4 and same >= n - 2 and anchor_ok and not explained
                if (same >= n - 1 and not explained) or loose:
                    key = (term, win)
                    if key not in seen:
                        seen.add(key)
                        suspects.append({"canonical": term, "variant": win,
                                         "material": f["id"], "at_char": i,
                                         "match": "strict" if same >= n - 1 else "loose"})
    return {"check": "propernoun_vote", "status": "checked",
            "result": "suspect" if suspects else "pass",
            "detail": suspects or "词表专名（n>=3）在各素材中拼写一致"}


def detect_takes(words, ngram=3):
    """词级重说段定位（供 Agent 剪取决策），重叠区间合并"""
    takes = []
    if len(words) < ngram * 2:
        return takes
    texts = [w["text"] for w in words]
    for i in range(len(words) - ngram):
        gram = "".join(texts[i:i + ngram])
        if len(gram) < ngram:
            continue
        for j in range(i + ngram, min(i + ngram * 4, len(words) - ngram + 1)):
            if "".join(texts[j:j + ngram]) == gram:
                takes.append({"t": [round(words[i]["start"], 1), round(words[j + ngram - 1]["end"], 1)],
                              "type": "retake-suspect",
                              "note": "「%s」在 %.1fs 附近重复出现" % (gram, words[j]["start"])})
                break
    merged = []
    for t in sorted(takes, key=lambda x: x["t"][0]):
        if merged and t["t"][0] <= merged[-1]["t"][1]:
            merged[-1]["t"][1] = max(merged[-1]["t"][1], t["t"][1])
        else:
            merged.append(t)
    return merged


def assess_usable(defects):
    hard = [d for d in defects if d.get("type") in ("blur", "exposure")]
    if hard:
        return "reshoot-suspect", "存在 %d 处硬缺陷（%s），建议人工确认是否重拍" % (
            len(hard), "、".join(d["type"] for d in hard))
    if defects:
        return "trim", "存在 %d 处轻微缺陷（%s），取净段可用" % (
            len(defects), "、".join(d["type"] for d in defects))
    return "yes", "无已知缺陷"


def _pack_files(pid):
    pdir = os.path.join(ROOT, "projects", pid)
    packs = []
    lib = os.path.join(pdir, "materials", "library.json")
    if os.path.exists(lib):
        packs = json.load(open(lib, encoding="utf-8")).get("packs", [])
    if not packs:
        pj = os.path.join(pdir, "project.json")
        if os.path.exists(pj):
            packs = json.load(open(pj, encoding="utf-8")).get("material_packs", [])
    files = {}
    for p in packs:
        pid2 = p["id"] if isinstance(p, dict) else p
        pp = os.path.join(ROOT, "materials", "packs", pid2, "pack.json")
        if os.path.exists(pp):
            for f in json.load(open(pp, encoding="utf-8")).get("files", []):
                files[f["id"]] = f
    return files


def build(pid):
    pdir = os.path.join(ROOT, "projects", pid)
    lex_path = os.path.join(ROOT, "config", "lexicon.json")
    lex = json.load(open(lex_path, encoding="utf-8")) if os.path.exists(lex_path) else {"terms": []}
    terms = lex.get("terms", [])
    pack_files = _pack_files(pid)

    used = {}
    sl_dir = os.path.join(pdir, "storylines")
    if os.path.exists(sl_dir):
        for fn in sorted(os.listdir(sl_dir)):
            if not fn.endswith(".json"):
                continue
            try:
                sl = json.load(open(os.path.join(sl_dir, fn), encoding="utf-8"))
            except Exception:
                continue
            for b in sl.get("beats", []):
                for t in b.get("tracks", []):
                    used.setdefault(t.get("source_id"), []).append(
                        {"story": fn[:-5], "beat": b.get("no"), "role": t.get("role"),
                         "src_in": t.get("src_in"), "duration": t.get("duration")})

    pack_xcheck = [propernoun_vote(list(pack_files.values()), terms)]

    dossiers = {}
    for mid, f in sorted(pack_files.items()):
        words = _real_words(f)
        dur = float(f.get("duration") or 0)
        v = f.get("visual") or {}
        segs = _speech_segments(words)
        defects = v.get("defects") or []
        usable, why = assess_usable(defects)
        takes = detect_takes(words)
        xc = [c for c in (xcheck_ocr_desc(f, terms), xcheck_repeat(words)) if c]
        xc.append(xcheck_energy(words, dur))
        suspicious = [c["check"] for c in xc if c.get("result") == "suspect"]

        dossiers[mid] = {
            "id": mid,
            "duration": dur,
            "geometry": {k: f.get(k) for k in ("width", "height", "_rotated", "_rotation") if k in f},
            "assessment": {
                "summary": v.get("summary") or (v.get("desc") or "")[:60],
                "content_type": v.get("content_type") or "unknown",
                # P2#11+R3-3：unknown（v2 降级/图片音频）≠ 禁用——v1 时代全部素材即"unknown"级理解，
                # 默认可入口播；显式禁口播=deny 清单 {meta, ambient, broll}，与 draft 闸门同一集合
                "narration_eligible": (v.get("content_type") or "unknown") not in ("meta", "ambient", "broll"),
                "content_type_note": {
                    "meta": "拍摄说戏——禁入叙事线（A 轨禁用，B 轨画面可用）",
                    "ambient": "纯环境画面——不可作口播 A 轨，仅 B-roll/转场",
                    "broll": "纯画面无语音——不可作口播 A 轨（无声幕），仅 B-roll/转场",
                }.get(v.get("content_type"), ""),
                "usable": usable,
                "usable_why": why,
                "defects": defects,
                "takes": takes,
                "action": ("避开缺陷段取净段" if usable == "trim" else
                           "建议人工确认重拍" if usable == "reshoot-suspect" else
                           "可直接取用" if usable == "yes" else "按簇归档待用"),
            },
            "timeline": {
                "speech": [{"t": [round(a, 1), round(b, 1)]} for a, b in segs],
                "visual_moments": [{"t": m.get("t"), "note": m.get("note")} for m in v.get("moments") or []],
                "defects": [{"t": d.get("t"), "type": d.get("type"), "note": d.get("note")} for d in defects],
            },
            "raw": {
                "transcript": {"words": words, "n_real": len(words),
                               "audit": (f.get("audit") or {}).get("transcript")},
                "visual": {"desc": v.get("desc"),
                           "ocr_detail": v.get("ocr_detail") or [],
                           "usage_list": v.get("usage_list") or ([v.get("usage")] if v.get("usage") else []),
                           "provider": v.get("provider"), "schema": v.get("schema", 1),
                           "flags": v.get("flags") or []},
                "lexicon_terms": len(terms),
            },
            "usage_in_project": used.get(mid) or [],
            "quality": {
                "xchecks": xc,
                "suspects": suspicious,
                "coverage": {"energy_probe": False, "ocr_desc": True,
                             "repeat_ngram": True, "propernoun_vote": True},
            },
        }

    return {
        "project": pid,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "schema": 1,
        "counts": {"materials": len(dossiers),
                   "usable": sum(1 for d in dossiers.values() if d["assessment"]["usable"] == "yes"),
                   "trim": sum(1 for d in dossiers.values() if d["assessment"]["usable"] == "trim"),
                   "reshoot_suspect": sum(1 for d in dossiers.values() if d["assessment"]["usable"] == "reshoot-suspect")},
        "pack_xchecks": pack_xcheck,
        "lexicon_terms": len(terms),
        "materials": dossiers,
        "skipped_layers": ["LLM 语义聚类（topic_cluster 精判）", "能量探针互证", "抽样帧核验 desc"],
    }


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else "demo-project"
    d = build(pid)
    outp = os.path.join(ROOT, "projects", pid, "dossier.json")
    json.dump(d, open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    c = d["counts"]
    print("dossier ✓ %s | %d 条：可取 %d / 需修剪 %d / 重拍嫌疑 %d" % (
        outp, c["materials"], c["usable"], c["trim"], c["reshoot_suspect"]))
    for mid, dd in d["materials"].items():
        a = dd["assessment"]
        marks = ("  ⚠ " + "|".join(dd["quality"]["suspects"])) if dd["quality"]["suspects"] else ""
        print("  %s %-9s %-16s %s%s" % (mid, a["content_type"], a["usable"], a["action"][:26], marks))
