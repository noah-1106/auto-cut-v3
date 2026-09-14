# -*- coding: utf-8 -*-
"""qc.py —— 审片层（QC）：结论先行的交付闸门。

架构（auto-editor 标签制 + broadcast QC 双分法）：
  signals（词轨/ASS/成片探针）→ violations（声明式规则查询，带证据坐标）→ actions（按可修性分流）
  verdict：blocked（不可修 blocker）| fix-then-deliver（可修 blocker）| deliverable

规则（阈值外置 config/qc_rules.json，平台响度锚点=EBU R128/平台标准）：
  R1 死尾巴    出点 > 末实词尾+max        blocker auto_fix(收缩)
  R2 尾音咬字  出点 < 末实词尾+min        blocker auto_fix(延展)；素材物理顶格 → info 豁免
  R3 静音洞    窗口内词间隔 > hole         warn   suggest(重选段)
  R4 冻结帧    成片整段静止 > freeze_min   info   suggest(查素材/转场)（freezedetect 已接入）
  R5 字幕页    相邻重叠 / 闪断页<min       blocker(重叠) auto_fix=重渲 / warn(闪断) suggest(合并)
  R6 响度      integrated LUFS 偏离平台锚  warn   suggest(loudnorm 两遍)；近静音跳过
  R7 听觉     成片静音洞>hole / 相邻段响度差>jump  warn suggest（物理测量探针，不消费 ASR 时间戳）
  R8 填充词    词轨命中填充词表            info   human(标记，剪否由人/Agent 定)
  R9 重说      窗口内相邻 n-gram 重复      warn   suggest(剪重复段)
  R10 页同步   ASS 页时刻 vs plan 词轨重放页 中位偏移>shift  warn suggest（G2t 入列——确定性同函数重放，非 ASR 参照）

用法：
  python3 autocut3/qc.py <project> [--story aidraft] [--no-deep]
  产物：projects/<pid>/qc-report.json + 终端一行结论
  降级纪律：单条规则异常只记 violation: error，不中断其余检查；QC 自身崩溃不阻塞渲染流程。
"""
import json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUNCT = "，。！？、"


def load_rules():
    p = os.path.join(ROOT, "config", "qc_rules.json")
    cfg = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    plat = cfg.get("platform", "douyin")
    r = dict(cfg.get("rules", {}))
    r["_lufs_target"] = cfg.get("platforms", {}).get(plat, {}).get("lufs_target", -16)
    r["_lufs_tol"] = cfg.get("platforms", {}).get(plat, {}).get("lufs_tol", 3.0)
    r["_platform"] = plat
    return r


def _real_words(f):
    return [w for w in (f.get("transcript") or {}).get("words") or [] if w.get("text") not in PUNCT]


def _V(rule, sev, action, ev, fix=None, note=None):
    return {"rule": rule, "severity": sev, "action": action, "evidence": ev,
            "suggested_fix": fix, "note": note, "status": "open"}


# ---------- R1/R2/R3/R8/R9：storyline 窗口 × 词轨（纯 Python，零探针）----------
def check_windows(sl, files, rules):
    out = []
    for b in sl.get("beats", []):
        for t in b.get("tracks", []):
            mid = t.get("source_id")
            f = files.get(mid)
            if not f:
                out.append(_V("R0", "warn", "human",
                              {"material": mid, "beat": b.get("no")},
                              note="引用了档案中不存在的素材"))
                continue
            if t.get("role") == "B":
                # B 轨=画中画，无叙事语音语义——R1/R2/R3 词轨类检查不适用（镜像 draft.validate 的 role==B 跳过）
                continue
            if ((b.get("narration") or {}).get("mode") or "original") == "dub":
                # 配音幕：原声整段被配音轨替换（pipeline 音频链 dub 分支），A 轨素材自带词轨
                # 对成片无语义——R1 死尾/R2 咬字/R3 静音洞按原声检查=误报（2026-09-14 维护者项目实锤）
                continue
            words = _real_words(f)
            dur_m = float(f.get("duration") or 0)
            si, du = float(t.get("src_in") or 0), float(t.get("duration") or 0)
            we = si + du
            inwin = [w for w in words if w["end"] <= we + 0.05 and w["start"] >= si - 0.05]
            if not inwin:
                continue
            # D1 完整含义：同一函数+同一份输入（原始词表含标点词）。_real_words 滤掉标点会让尾点漂移回 7.0
            tail = asr.speech_tail((f.get("transcript") or {}).get("words") or [], we)
            if tail <= 0:
                tail = max(w["end"] for w in inwin)  # 兜底（理论不达）
            # R1 死尾巴
            if we > tail + rules["R1_dead_tail_max"] + 1e-6:
                out.append(_V("R1", "blocker", "auto_fix",
                              {"material": mid, "beat": b.get("no"), "cut_out": round(we, 2),
                               "speech_tail": round(tail, 2), "tail_len": round(we - tail, 2)},
                              fix="出点收缩至 %.1fs（词尾+%.1fs）" % (tail + rules.get("R1_fix_target", 0.35), rules.get("R1_fix_target", 0.35))))
            # R2 咬字（含素材物理极限豁免）
            elif we < tail + rules["R2_min_tail"] - 1e-6:
                # 句中豁免：出点后紧贴下一词开头（<0.15s）=句子中间抽段，延展反而会包进下一个词——非咬字问题
                after = [w for w in words if w["start"] >= we - 0.05 and w["end"] > we]
                mid_cut = bool(after) and (after[0]["start"] - we) < 0.15
                at_edge = we >= dur_m - 0.05
                if mid_cut:
                    out.append(_V("R2", "info", "suggest",
                                  {"material": mid, "beat": b.get("no"), "cut_out": round(we, 2),
                                   "speech_tail": round(tail, 2),
                                   "next_word": (after[0]["text"] if after else "")},
                                  fix="句中抽段——建议调整窗口到自然停顿处（逗号/句号后），机械延展会切进下一词"))
                    # 首轮 P2#10：此处原 continue 会连带跳过 R3/R8/R9——句中抽段只豁免 R2 出点档位，其余检查照跑
                elif at_edge:
                    out.append(_V("R2", "info", "unfixable",
                                  {"material": mid, "beat": b.get("no"), "cut_out": round(we, 2),
                                   "speech_tail": round(tail, 2), "material_end": dur_m},
                                  note="素材物理极限（出点已顶素材尾），非剪辑错误"))
                else:
                    out.append(_V("R2", "blocker", "auto_fix",
                                  {"material": mid, "beat": b.get("no"), "cut_out": round(we, 2),
                                   "speech_tail": round(tail, 2)},
                                  fix="出点延展至 %.1fs（词尾+%.1fs，受素材边界 %.1fs 限制）"
                                      % (min(tail + rules.get("R2_fix_target", 0.35), dur_m), rules.get("R2_fix_target", 0.35), dur_m)))
            # R3 静音洞
            for k in range(1, len(inwin)):
                gap = inwin[k]["start"] - inwin[k - 1]["end"]
                if gap > rules["R3_silence_hole"]:
                    out.append(_V("R3", "warn", "suggest",
                                  {"material": mid, "beat": b.get("no"),
                                   "gap": [round(inwin[k - 1]["end"], 2), round(inwin[k]["start"], 2)],
                                   "gap_len": round(gap, 2)},
                                  fix="窗口内 %.1fs 静音洞——建议绕选或收紧窗口" % gap))
            # R8 填充词
            fills = []
            for w in inwin:
                for fw in rules["R8_filler_words"]:
                    if fw in w["text"]:
                        fills.append({"t": [round(w["start"], 1), round(w["end"], 1)], "w": w["text"]})
                        break
                else:
                    if w["text"] in rules["R8_single_fillers"]:
                        fills.append({"t": [round(w["start"], 1), round(w["end"], 1)], "w": w["text"]})
            if fills:
                out.append(_V("R8", "info", "human",
                              {"material": mid, "beat": b.get("no"), "fillers": fills[:8],
                               "n": len(fills)},
                              note="填充词标记——剪否由人/Agent 决定（全自动剪会机器人化）"))
            # R9 重说（窗口内相邻 n-gram）
            n = rules["R9_repeat_ngram"]
            texts = [w["text"] for w in inwin]
            for i in range(len(texts) - n):
                gram = "".join(texts[i:i + n])
                if len(gram) < n:
                    continue
                for j in range(i + n, min(i + n + 8, len(texts) - n + 1)):
                    if "".join(texts[j:j + n]) == gram and (j - i) * 1 <= rules["R9_repeat_gap"]:
                        out.append(_V("R9", "warn", "suggest",
                                      {"material": mid, "beat": b.get("no"),
                                       "gram": gram,
                                       "t": [round(inwin[i]["start"], 1), round(inwin[j + n - 1]["end"], 1)]},
                                      fix="检测到重说段——剪取后半段（重说一般后半句更顺）"))
                        break
                else:
                    continue
                break
    return out


# ---------- R5：ASS 字幕页（渲染产物）----------
def _ass_t(s):
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def check_ass(ass_path, rules):
    out = []
    if not os.path.exists(ass_path):
        return [_V("R5", "warn", "human", {"ass": ass_path}, note="渲染产物 ASS 不存在，跳过字幕检查")]
    pages = []
    for ln in open(ass_path, encoding="utf-8"):
        m = re.match(r"Dialogue: 0,([\d:.]+),([\d:.]+)", ln.strip())
        if m:
            pages.append((_ass_t(m.group(1)), _ass_t(m.group(2)), ln.strip()[:80]))
    pages.sort()
    for i in range(1, len(pages)):
        if pages[i][0] < pages[i - 1][1] - 0.01:
            out.append(_V("R5", "blocker", "suggest",
                          {"overlap": [round(pages[i - 1][1], 2), round(pages[i][0], 2)],
                           "prev": pages[i - 1][2][:60], "cur": pages[i][2][:60]},
                          fix="字幕页重叠——重渲即修；若复现说明 build_ass 页间钳制回归"))
    for a, b, ln in pages:
        if 0 < b - a < rules["R5_flash_page_min"]:
            out.append(_V("R5", "warn", "suggest",
                          {"page": [round(a, 2), round(b, 2)], "dur": round(b - a, 2), "line": ln[:60]},
                          fix="闪断页 <%.1fs——建议合并进相邻页（先向前合并，超宽向后）" % rules["R5_flash_page_min"]))
    return out


# ---------- R6：响度（EBU R128，ffmpeg ebur128）----------
def check_lufs(video, rules):
    if not video or not os.path.exists(video):
        return [_V("R6", "warn", "human", {"video": video}, note="成片不存在，跳过响度检查")]
    ff = asr.ffmpeg_path()
    if not ff:
        return [_V("R6", "warn", "human", {}, note="无 ffmpeg，跳过响度检查")]
    try:
        p = subprocess.run([ff, "-hide_banner", "-nostats", "-i", video,
                            "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", p.stderr)
        if not m:
            return [_V("R6", "warn", "human", {}, note="ebur128 无输出，跳过")]
        i_lufs = float(m.group(1))
        if i_lufs <= rules["R6_lufs_skip_below"]:
            return [_V("R6", "info", "human", {"integrated": i_lufs},
                       note="近静音（gate floor 陷阱），跳过响度判定")]
        tgt, tol = rules["_lufs_target"], rules["_lufs_tol"]
        if abs(i_lufs - tgt) > tol:
            return [_V("R6", "warn", "suggest",
                       {"integrated": i_lufs, "target": tgt, "platform": rules["_platform"],
                        "delta": round(i_lufs - tgt, 1)},
                       fix="loudnorm 两遍法至 %.0f LUFS（linear=true 保动态）" % tgt)]
        return [_V("R6", "info", "human", {"integrated": i_lufs, "target": tgt},
                   note="响度达标")]
    except Exception as e:
        return [_V("R6", "warn", "human", {}, note="响度探测异常: %s" % str(e)[:80])]


# ---------- R7：听觉域（成片物理测量——不消费 ASR 时间戳，v2-⑥）----------
def check_audio_output(video, rules):
    """成片音频：静音洞（silencedetect 补集）+ 分段响度骤变（volumedetect）。"""
    if not video or not os.path.exists(video):
        return [_V("R7", "warn", "human", {"video": video}, note="成片不存在，跳过听觉检查")]
    ff = asr.ffmpeg_path()
    if not ff:
        return [_V("R7", "warn", "human", {}, note="无 ffmpeg，跳过听觉检查")]
    out = []
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", video, "-vn",
                            "-af", "silencedetect=noise=-35dB:d=0.5", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        sil = []
        cur = None
        for line in (p.stderr or "").splitlines():
            m = re.search(r"silence_start: ([\d.]+)", line)
            if m:
                cur = float(m.group(1))
            m = re.search(r"silence_end: ([\d.]+)", line)
            if m and cur is not None:
                sil.append((cur, float(m.group(1))))
                cur = None
        hole_max = rules.get("R7_hole", 1.5)
        holes = [(s, e) for s, e in sil if e - s > hole_max]
        if holes:
            out.append(_V("R7", "warn", "suggest",
                          {"holes": [[round(s, 1), round(e, 1)] for s, e in holes[:6]],
                           "n": len(holes), "max": round(max(e - s for s, e in holes), 2)},
                          fix="成片存在 >%.1fs 静音洞 %d 处——检查该时段是否缺 BGM/音效或误裁人声" % (hole_max, len(holes))))
    except Exception as e:
        out.append(_V("R7", "warn", "human", {}, note="静音探针异常: %s" % str(e)[:80]))
    try:
        # 分段 mean_volume 对账：相邻段差 > jump → 音量骤变（离麦/设备变化在成片上的残留）
        seg = float(rules.get("R7_seg", 5.0))
        dur = asr.media_duration(video) or 0
        means = []
        t = 0.0
        while t + 1 < dur:
            r = subprocess.run([ff, "-hide_banner", "-nostats", "-ss", str(t), "-t", str(seg),
                                "-i", video, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=60)
            m = re.search(r"mean_volume: (-?[\d.]+)", r.stderr or "")
            if m:
                means.append((t, float(m.group(1))))
            t += seg
        jump = rules.get("R7_jump", 8.0)
        jumps = [(means[k][0], round(means[k][1] - means[k + 1][1], 1))
                 for k in range(len(means) - 1) if abs(means[k][1] - means[k + 1][1]) > jump]
        if jumps:
            out.append(_V("R7", "warn", "suggest",
                          {"jumps": [[round(a, 1), d] for a, d in jumps[:6]], "seg": seg},
                          fix="相邻 %.0fs 段响度差 >%.0fdB %d 处——音量骤变（离麦/素材来源切换），建议增益归一" % (seg, jump, len(jumps))))
    except Exception as e:
        out.append(_V("R7", "warn", "human", {}, note="响度分段探针异常: %s" % str(e)[:80]))
    return out


# ---------- R4：冻结帧（freezedetect 接入，替换 skipped 声明）----------
def check_freeze(video, rules):
    if not video or not os.path.exists(video):
        return [_V("R4", "info", "human", {"video": video}, note="成片不存在，冻结帧检查 skipped")]
    ff = asr.ffmpeg_path()
    if not ff:
        return [_V("R4", "info", "human", {}, note="无 ffmpeg，冻结帧检查 skipped")]
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", video, "-vf",
                            "freezedetect=noise=0.001:duration=%.1f" % rules.get("R4_freeze_min", 2.0),
                            "-map", "0:v", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=180)
        freezes = []
        start = None
        for line in (p.stderr or "").splitlines():
            m = re.search(r"freeze_start: ([\d.]+)", line)
            if m:
                start = float(m.group(1))
            m = re.search(r"freeze_duration: ([\d.]+)", line)
            if m and start is not None:
                freezes.append([round(start, 1), round(start + float(m.group(1)), 1)])
                start = None
        if start is not None:
            # EOF 截断：冻结持续到片尾时本构建不吐 freeze_end/duration——补到素材时长
            freezes.append([round(start, 1), round(asr.media_duration(video) or start, 1)])
        if freezes:
            return [_V("R4", "info", "suggest",
                       {"freezes": freezes[:6], "n": len(freezes)},
                       fix="检测到冻结帧（静止画面）——若是空镜素材属正常，否则检查素材/转场")]
        return [_V("R4", "info", "human", {}, note="冻结帧检查通过（无 ≥%.1fs 静止段）" % rules.get("R4_freeze_min", 2.0))]
    except Exception as e:
        return [_V("R4", "info", "human", {}, note="冻结帧探针异常，skipped: %s" % str(e)[:80])]


# ---------- R10：页同步（G2t 入列——plan 词轨重放页 vs ASS 实际页，确定性零 ASR 参照）----------
def check_page_sync(pdir, sid, rules):
    """用 pipeline.build_pages 对 plan 词轨做确定性重放，与 ASS 实际页逐页对时。
    中位偏移 > shift → warn。字幕页时间错的 detector（错幕/错行/时轴错位全都会在此显形）。"""
    import statistics
    plan = None
    for cand in (f"{pdir}/plan-{sid}.json" if sid else None,
                 f"{pdir}/plan.json",
                 f"{pdir}/plan-{sid}.json"):
        if cand and os.path.exists(cand):
            plan = json.load(open(cand, encoding="utf-8"))
            break
    ass_path = os.path.join(pdir, "subtitle-%s.ass" % sid)
    if not plan or not os.path.exists(ass_path):
        return [_V("R10", "info", "human", {},
                   note="plan/ASS 不齐，页同步检查 skipped（先渲染再检）")]
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pipeline
        style = (plan.get("meta") or {}).get("style", {}) or {}
        regs = json.load(open(os.path.join(ROOT, "registry", "subtitles.json"), encoding="utf-8"))
        st = regs.get(plan.get("subtitle_style", ""), {})
        pages = pipeline.build_pages(
            [(w["t"], w["s"], w["e"]) for w in plan.get("words") or []],
            int(st.get("max_chars", 12)))
        expected = [pg[0][1] for pg in pages]
        actual = []
        for ln in open(ass_path, encoding="utf-8"):
            m = re.match(r"Dialogue: 0,([\d:.]+),", ln.strip())
            if m:
                actual.append(_ass_t(m.group(1)))
        n = min(len(expected), len(actual))
        if n == 0:
            return [_V("R10", "info", "human", {}, note="页数为零，页同步 skipped")]
        offs = sorted(actual[k] - expected[k] for k in range(n))
        med = statistics.median(offs)
        shift = rules.get("R10_shift", 0.5)
        ev = {"pages": n, "median_offset": round(med, 2),
              "p90_abs": round(sorted(abs(o) for o in offs)[int(n * 0.9) - 1 if n >= 10 else n - 1], 2)}
        if abs(med) > shift:
            return [_V("R10", "warn", "suggest", ev,
                       fix="字幕页中位偏移 %.2fs（阈 %.1fs）——词轨与 ASS 时轴错位，查 vadwords 词轨或重渲" % (med, shift))]
        return [_V("R10", "info", "human", ev, note="页同步通过（中位偏移 %.2fs）" % round(med, 2))]
    except Exception as e:
        return [_V("R10", "warn", "human", {}, note="页同步检查异常: %s" % str(e)[:80])]


# ---------- 主入口 ----------
def run(pid, story=None, deep=True, write=True):
    pdir = os.path.join(ROOT, "projects", pid)
    rules = load_rules()
    sid = story
    sl_path = None
    if sid:
        sl_path = os.path.join(pdir, "storylines", sid + ".json")
    if not sl_path or not os.path.exists(sl_path):
        cands = sorted([f for f in os.listdir(os.path.join(pdir, "storylines"))
                        if f.endswith(".json")],
                       key=lambda x: -os.path.getmtime(os.path.join(pdir, "storylines", x))) \
            if os.path.exists(os.path.join(pdir, "storylines")) else []
        if not cands:
            return {"verdict": "blocked", "violations": [_V("R0", "blocker", "human",
                    {}, note="项目无故事线")]}
        sid = cands[0][:-5]
        sl_path = os.path.join(pdir, "storylines", cands[0])
    sl = json.load(open(sl_path, encoding="utf-8"))

    files = {}
    lib = os.path.join(pdir, "materials", "library.json")
    if os.path.exists(lib):
        for p in json.load(open(lib, encoding="utf-8")).get("packs", []):
            pid2 = p["id"] if isinstance(p, dict) else p
            pp = os.path.join(ROOT, "materials", "packs", pid2, "pack.json")
            if os.path.exists(pp):
                for f in json.load(open(pp, encoding="utf-8")).get("files", []):
                    files[f["id"]] = f

    violations = []
    try:
        violations += check_windows(sl, files, rules)
    except Exception as e:
        violations.append(_V("R1-R9", "warn", "human", {}, note="词轨检查异常: %s" % str(e)[:80]))
    try:
        violations += check_ass(os.path.join(pdir, "subtitle-%s.ass" % sid), rules)
    except Exception as e:
        violations.append(_V("R5", "warn", "human", {}, note="ASS 检查异常: %s" % str(e)[:80]))
    try:
        violations += check_page_sync(pdir, sid, rules)
    except Exception as e:
        violations.append(_V("R10", "warn", "human", {}, note="页同步检查异常: %s" % str(e)[:80]))
    out_mp4 = os.path.join(pdir, "out-%s.mp4" % sid)
    if deep:
        try:
            violations += check_lufs(out_mp4, rules)
        except Exception as e:
            violations.append(_V("R6", "warn", "human", {}, note="响度检查异常: %s" % str(e)[:80]))
        try:
            violations += check_audio_output(out_mp4, rules)
        except Exception as e:
            violations.append(_V("R7", "warn", "human", {}, note="听觉检查异常: %s" % str(e)[:80]))
        try:
            violations += check_freeze(out_mp4, rules)
        except Exception as e:
            violations.append(_V("R4", "info", "human", {}, note="冻结帧检查异常，skipped: %s" % str(e)[:80]))
    else:
        violations.append({"rule": "R4/R6/R7", "severity": "info", "action": "human",
                           "evidence": {}, "note": "深检关闭（--no-deep），听觉/响度/冻结帧 skipped",
                           "suggested_fix": None, "status": "skipped"})

    def sev_order(v):
        return {"blocker": 0, "warn": 1, "info": 2}.get(v.get("severity"), 3)
    violations.sort(key=sev_order)
    blockers = [v for v in violations if v["severity"] == "blocker"]
    unfixable_blockers = [v for v in blockers if v["action"] in ("human", "unfixable")]
    verdict = ("blocked" if unfixable_blockers else
               "fix-then-deliver" if blockers else "deliverable")
    report = {
        "project": pid, "story": sid, "verdict": verdict,
        "one_line": ("%s | %d blocker（可修 %d）/ %d warn / %d info" % (
            {"deliverable": "可交付", "fix-then-deliver": "修后交付", "blocked": "不可交付"}[verdict],
            len(blockers), len(blockers) - len(unfixable_blockers),
            sum(1 for v in violations if v["severity"] == "warn"),
            sum(1 for v in violations if v["severity"] == "info"))),
        "counts": {"blocker": len(blockers), "warn": sum(1 for v in violations if v["severity"] == "warn"),
                   "info": sum(1 for v in violations if v["severity"] == "info")},
        "platform": rules["_platform"], "violations": violations,
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    if write:
        json.dump(report, open(os.path.join(pdir, "qc-report.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="审片层 QC——结论先行的交付闸门")
    ap.add_argument("project")
    ap.add_argument("--story", help="故事线 id（默认最近修改）")
    ap.add_argument("--no-deep", action="store_true", help="跳过响度探针")
    a = ap.parse_args()
    r = run(a.project, story=a.story, deep=not a.no_deep)
    print("QC 结论：%s" % r["one_line"])
    for v in r["violations"]:
        if v["severity"] in ("blocker", "warn"):
            print("  [%s/%s] %s %s %s" % (v["severity"], v["action"], v["rule"],
                                          json.dumps(v["evidence"], ensure_ascii=False)[:100],
                                          ("→ " + v["suggested_fix"]) if v.get("suggested_fix") else (v.get("note") or "")))
