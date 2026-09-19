#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autocut3 管线核心 v0.1 —— 最小闭环
故事线(SSOT) + 素材选段 → 解说轨(词级0.1s) → 字幕轨(ASS卡拉OK·字级插值) → 画面轨(选段+转场) → 音乐轨(BGM)
→ EDL(plan.json) → ffmpeg 渲染

设计依据：《auto-cut V3 萃取范围界定》v2
  - 双视图：本模块只生产 JSON/命令（AI 操作台），渲染产物交 Studio 监视器（人眼/视觉模型）
  - 组件四件套：输入契约 / 生成器 / 校验器(最小) / 后续加缓存
"""
import json, os, re, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # noqa: E402  speech_tail（转场预留的词尾判定，D1 单一事实源）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _resolve_ff():
    # 跨平台 ffmpeg 定位：环境变量 → 仓内二进制（mac 无后缀 / win .exe）→ 系统 PATH
    for c in (os.environ.get("FFMPEG"),
              os.path.join(ROOT, "bin", "ffmpeg"),
              os.path.join(ROOT, "bin", "ffmpeg.exe")):
        if c and os.path.exists(c):
            return c
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"

FF = _resolve_ff()


def _media_duration(path):
    if not path or not os.path.exists(path):
        return 0.0
    for c in (os.environ.get("FFPROBE"), os.path.join(ROOT, "bin", "ffprobe"),
              os.path.join(ROOT, "bin", "ffprobe.exe")):
        if c and os.path.exists(c):
            r = subprocess.run([c, "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
            try:
                return float(r.stdout.strip())
            except ValueError:
                return 0.0
    import shutil
    fp = shutil.which("ffprobe")
    if not fp:
        return 0.0
    r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


_ENC_CACHE = {}
def _encoder_usable(enc):
    if enc not in _ENC_CACHE:
        try:
            r = subprocess.run([FF, "-hide_banner", "-h", f"encoder={enc}"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            _ENC_CACHE[enc] = r.returncode == 0
        except Exception:
            _ENC_CACHE[enc] = False
    return _ENC_CACHE[enc]

def hw_encoder():
    """平台感知硬件编码器（2026-09-14 跨平台分发要求）：mac=videotoolbox，
    win=nvenc→qsv 探测择一，皆不可用=libx264（软编回退铁律不变）。"""
    if sys.platform == "darwin":
        return "h264_videotoolbox"
    if sys.platform == "win32":
        for enc in ("h264_nvenc", "h264_qsv"):
            if _encoder_usable(enc):
                return enc
    return "libx264"

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def _ass_spec(ass_path):
    """subtitles 滤镜规格（ffmpeg 7+ 新解析器兼容，2026-09-15 Windows CI 实证）：
    绝对路径含盘符冒号（D:），冒号在选项值里无论引号/转义都被新解析器劈开
    （av_get_token 剥引号后 ff_filter_opt_parse 按裸冒号切选项）。
    解法=相对路径+无引号（值里只剩 / - _ . 全非特殊字符），subprocess 统一 cwd=ROOT。
    ffmpeg 6/9、mac/win 全兼容。"""
    rel = os.path.relpath(ass_path, ROOT).replace("\\", "/")
    return f"subtitles=filename={rel}:fontsdir=assets/fonts"

# ---------------------------------------------------------------- 解说轨
def remap_words(cuts, acts, segs, total, packpool=None):
    """词级时间戳 → 成片时间轴（0.1s 量化规范）。源: cuts.json（n001）或素材包转写词轨（source_id 优先）。
    dub 幕：字幕词轨 = 配音文案按配音音频实测时长均分（画面字幕必须跟配音走，不跟原声）。"""
    packpool = packpool or {}
    out, drops = [], []  # drops=转场重叠钳没的词（ruxuan-02 实锤：静默丢词→字幕页合并零告警）
    for i, act in enumerate(acts):
        if act is None:
            continue  # narration=none 的幕：占位对齐用，无词
        s = segs[i]
        tl0 = float(s["tl_in"])
        tl1 = float(segs[i + 1]["tl_in"]) if i + 1 < len(segs) else float(total) + 0.3
        seg_words = []
        # 显式词轨最优先（2026-09-15）：narration.words（VAD 物理测量/音频本体 ASR），
        # dub/original 统一通道——素材 ASR 词轨绝对时间漂移 0~3s 实锤后，物理测量是唯一可信时间源
        ow = s.get("owords")
        if ow:
            for w in ow:
                _ws, _we = round(tl0 + float(w["s"]), 1), round(tl0 + float(w["e"]), 1)
                # 幕内钳制与 dub 通道同源（真实项目实锤：owords 直通无钳制 → 词越界 bleed → R5 页倒置）。
                # 全窗内天然短词不截不丢（同下方通用钳制块 2026-09-18 修正）
                if _ws >= tl0 and _we <= tl1:
                    seg_words.append({"t": w["t"], "s": _ws, "e": _we})
                    continue
                _ws2, _we2 = max(_ws, tl0), min(_we, tl1)
                if _we2 - _ws2 < 0.08:
                    drops.append({"beat": s.get("no"), "word": str(w.get("t") or ""),
                                  "at": round(float(w["s"]), 1), "reason": "overlap-clamp"})
                    continue
                seg_words.append({"t": w["t"], "s": _ws2, "e": _we2})
            out.extend(seg_words)
            continue
        dubp = s.get("dub")  # {path,text,dur,words|words_from}
        if dubp:
            dur = float(dubp.get("dur") or 0)
            if dur <= 0:
                dur = float(s["dur"])
            seg_words = []
            # 词轨三级优先（2026-09-15 15s 错位根因复盘）：
            # ① narration.words 显式词轨=配音音频本体 ASR——与音频同源零偏移（首选）
            # ② words_from 素材词轨平移——已实锤素材 ASR 绝对时间偏移 0.15~3s 不等，只作旧线兼容
            # ③ 均分——最后兜底
            rw = dubp.get("words") or []
            if rw:
                for w in rw:
                    seg_words.append({"t": w["t"], "s": round(tl0 + float(w["s"]), 1), "e": round(tl0 + float(w["e"]), 1)})
            wf = dubp.get("words_from") or {}
            src_words = (packpool.get(wf.get("source_id")) or {}).get("words", []) if (wf.get("source_id") and not seg_words) else []
            t0 = float(wf.get("src_in") or 0)
            if src_words:
                for w in src_words:
                    if w["start"] >= t0 - 0.05 and w["end"] <= t0 + dur + 0.05:
                        ws = round(w["start"] - t0 + tl0, 1)
                        seg_words.append({"t": w["text"], "s": ws, "e": round(w["end"] - t0 + tl0, 1)})
            if not seg_words:
                words_dub = (dubp.get("text") or "").strip()
                if words_dub:
                    chars = list(words_dub.replace(" ", ""))
                    step = dur / max(1, len(chars))
                    for k, ch in enumerate(chars):
                        ws = round(tl0 + k * step, 1)
                        seg_words.append({"t": ch, "s": ws, "e": round(ws + step, 1)})
        else:
            sid = act.get("source_id")
            if sid and sid in packpool:
                cut = packpool[sid]
            else:
                cut = cuts[act.get("cut_index") or 0] if cuts else {"words": []}
            for w in cut.get("words", []):
                if w["start"] >= act["src_in"] - 0.05 and w["end"] <= act["src_in"] + act["duration"] + 0.05:
                    ws = round(w["start"] - act["src_in"] + tl0, 1)
                    we = round(w["end"] - act["src_in"] + tl0, 1)
                    if 0 <= ws and we <= total + 0.3:
                        seg_words.append({"t": w["text"], "s": ws, "e": min(we, total)})
        # 幕内钳制：词不越出本幕时间窗——转场重叠区本幕尾词不侵占下一幕开头，
        # 下一幕头词不从上一幕中间冒出来（"一个字断在前一句后面"的根因）。
        # 全窗内的天然短词（0.04s 单字，vadwords 切片产物）照常上屏——0.08 下限只作用于
        # 被边界截断的残余（2026-09-18 实锤：8 条"转场钳词"实为短词误杀=字幕漏字）
        for w in seg_words:
            if w["s"] >= tl0 and w["e"] <= tl1:  # 全在窗内：不截不丢
                out.append({"t": w["t"], "s": w["s"], "e": w["e"]})
                continue
            ws2, we2 = max(w["s"], tl0), min(w["e"], tl1)
            if we2 - ws2 < 0.08:
                drops.append({"beat": s.get("no"), "word": str(w.get("t") or ""),
                              "at": round(w["s"], 1), "reason": "overlap-clamp"})
                continue  # 残余<0.08s 的跨界词丢弃（画面在转场，字幕留白）
            out.append({"t": w["t"], "s": round(ws2, 1), "e": round(we2, 1)})
    # 注意：不做全局时间排序——词按幕归属输出（字幕跟幕走），
    # 转场重叠区按时间排序会把两幕词轨洗成交错（历史病灶：'对着查。接'）
    return out, drops

# ---------------------------------------------------------------- 字幕轨
def char_level(words):
    """词级 → 字级插值（中文按字符均分词时长）。ASS 卡拉OK的最小时间单元。"""
    chars = []
    for w in words:
        cs = list(w["t"])
        if not cs:
            continue
        d = max(0.1, (w["e"] - w["s"])) / len(cs)
        for i, ch in enumerate(cs):
            chars.append((ch, round(w["s"] + i * d, 1), round(w["s"] + (i + 1) * d, 1)))
    return chars

PUNCT = "。！？；，、,:;!?）)】》\"\"''"
SENT_END = "。！？；!?"

def build_pages(chars, max_chars=12, hard_max=15, gap=0.35, seek_gap=0.12):
    """字流 → 字幕页。断句规则（2026-09-13 修复"一个字断在前一句后面"）：
    1) 标点字符永远粘住页尾：既不触发断页，也绝不落页首（逗号甩下页的病灶）；
    2) 句末标点（。！？；）后必断页：下句开头不再粘进前句页尾的病灶；
    3) 满 12 字进入"找断点"模式：小停顿(>seek_gap)/句末才断，15 字硬上限兜底——
       避免在词语正中间硬切；
    4) 孤字页回吞：断页产物 ≤2 字且并回上页不超 hard_max+2 时，并回上页
       （"还"字单独一页的病灶）。"""
    pages, cur = [], []
    for it in chars:
        ch = it[0]
        if not cur and pages and ch in PUNCT:
            pages[-1].append(it)
            continue
        if cur and ch not in PUNCT:
            clen = sum(len(c[0]) for c in cur)
            last = cur[-1][0]
            g = it[1] - cur[-1][2]
            punct_break = last in SENT_END
            gap_break = g > gap
            seek_break = clen >= max_chars and (punct_break or g > seek_gap or last in "，、,")
            if punct_break or gap_break or seek_break or clen >= hard_max:
                # 孤字回吞三守卫：新页自身非句末碎片 + 上页不以句末标点结尾（句号后
                # 刚分出的下句开头绝不容许回吞——"对着查。|接"被并回的病灶）+ 容量
                if (len(cur) <= 2 and pages
                        and cur[-1][0] not in SENT_END
                        and pages[-1][-1][0] not in SENT_END
                        and sum(len(c[0]) for c in pages[-1]) + len(cur) <= hard_max + 2):
                    pages[-1].extend(cur)
                else:
                    pages.append(cur)
                cur = []
        cur.append(it)
    if cur:
        pages.append(cur)
    # 后处理：孤字页（≤2字）按身份定向吸收——
    #   以句末标点结尾（"弹。"）＝ 上句的尾巴 → 向前合并回上页（完整句尾）；
    #   不含句末标点（"接"）   ＝ 下句的开头 → 向后合并进下页（"接下来"），
    #     绝不向前（前页以句号结尾时向前合并=把下句开头粘回前句，即原始病灶）。
    merged = True
    while merged and len(pages) > 1:
        merged = False
        for k in range(len(pages)):
            if len(pages[k]) > 2:
                continue
            klen = sum(len(c[0]) for c in pages[k])
            if k > 0 and pages[k][-1][0] in SENT_END \
                    and sum(len(c[0]) for c in pages[k-1]) + klen <= hard_max + 2:
                pages[k-1].extend(pages[k])
                del pages[k]
                merged = True
                break
            if k + 1 < len(pages) and sum(len(c[0]) for c in pages[k+1]) + klen <= hard_max + 2:
                pages[k + 1] = pages[k] + pages[k + 1]
                del pages[k]
                merged = True
                break
    return pages

def fmt_ts(sec):
    cs = max(0, int(round(sec * 100)))
    return f"{cs//360000}:{cs%360000//6000:02d}:{cs%6000//100:02d}.{cs%100:02d}"

def build_ass(plan, project_dir, ass_name="subtitle.ass"):
    regs = load(f"{ROOT}/registry/subtitles.json")
    if plan["subtitle_style"] not in regs:
        print(f"ASS FAIL: 字幕风格 '{plan['subtitle_style']}' 未注册，可用: {list(regs)}"); sys.exit(1)
    style = regs[plan["subtitle_style"]]
    pages = build_pages(char_level(plan["words"]), style.get("max_chars", 12))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {plan['width']}
PlayResY: {plan['height']}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{style['font']},{style['size']},{style['primary']},{style['secondary']},{style['outline_col']},&H00000000,0,0,0,0,100,100,1,0,1,{style['border']},2,2,80,80,{style['marginv']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    for i, pg in enumerate(pages):
        start = pg[0][1]
        end = min(pg[-1][2] + 0.25, plan["duration"])
        if i + 1 < len(pages):
            end = min(end, pages[i + 1][0][1])  # 页间钳制（2026-09-11 回看：尾缓冲压过下页起点=上下两行同屏）
        if style.get("karaoke", True):
            text = ""
            for ch, s, e in pg:  # \kf 厘秒；已读=Primary(金) 未读=Secondary(白)
                text += r"{\kf" + str(max(1, round((e - s) * 100))) + "}" + ch
        else:  # 无卡拉OK：整页白字
            text = "".join(ch for ch, _s, _e in pg)
        lines.append(f"Dialogue: 0,{fmt_ts(start)},{fmt_ts(end)},Karaoke,,0,0,0,,{text}\n")
    ass = os.path.join(project_dir, ass_name)
    with open(ass, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return ass, len(pages)

# ---------------------------------------------------------------- EDL
def story_file(project_dir, sid):
    """故事线文件解析：storylines/<sid>.json 优先，旧单故事线 storyline.json 兼容。"""
    if sid:
        f = os.path.join(project_dir, "storylines", f"{sid}.json")
        if os.path.exists(f):
            return f, sid
        raise FileNotFoundError(f"无故事线: {project_dir} story={sid}")  # 指定即严格——静默回退旧线是空屏 bug 的同族
    legacy = os.path.join(project_dir, "storyline.json")
    if os.path.exists(legacy):
        return legacy, None
    raise FileNotFoundError(f"无故事线: {project_dir}")

def art_suffix(project_dir, sid):
    """产物后缀：多故事线 -<sid>，旧模式空串。"""
    _f, real_sid = story_file(project_dir, sid)
    return (f"-{real_sid}" if real_sid else ""), real_sid


# ------------------------------------------------ 转场预留（2026-09-18 Noah 裁定）
# 不变式：幕边界落在静音里，转场整体吃预留区不吃词——词头词尾在结构上不可能被转场压。
# dt_eff = min(注册 dt, 尾侧可用静音, 头侧可用静音)，随边缘静音伸缩；
# 两侧静音不足 0.2s → 降级直切（素材够长就留预留，不够就直切）。
def _first_word_start(words, lo, hi):
    """窗口 [lo,hi] 内首个词起点（素材绝对坐标，start/end 词格式）。无词 None。"""
    c = [float(w.get("start") or 0) for w in words or []
         if float(w.get("end") or 0) > lo + 0.05 and float(w.get("start") or 0) < hi]
    return min(c) if c else None


def _prev_word_end(words, pos):
    """pos 前最后一个词的终点（无则 0）——头预留的回退上限（不许吃到上一词）。"""
    c = [float(w.get("end") or 0) for w in words or [] if float(w.get("end") or 0) <= pos + 0.05]
    return max(c) if c else 0.0


def _next_word_start(words, pos):
    """pos 后第一个词的起点（无则 None）——尾预留的推进上限（不许吃到下一词）。"""
    c = [float(w.get("start") or 0) for w in words or [] if float(w.get("start") or 0) > pos + 0.05]
    return min(c) if c else None


def _peek_src(nb):
    """下一幕（预留 peek 用）A 轨 (source_id, cut_index, 入点)。"""
    trks = (nb or {}).get("tracks")
    if trks:
        A = next((x for x in trks if x.get("role", "A") == "A"), trks[0])
        return A.get("source_id"), A.get("cut_index"), float(A.get("src_in") or 0)
    mm = (nb.get("materials") or [{}])[0]
    return mm.get("source_id"), mm.get("cut_index"), float(mm.get("src_in") or 0)


def _track_words(sid, ci, packpool, cuts):
    """素材词轨（绝对坐标）：包转写词轨优先，无则 cuts 词轨（n001 老项目）。"""
    w = (packpool.get(sid) or {}).get("words")
    if w:
        return w
    try:
        return (cuts[ci].get("words") if ci is not None and cuts else []) or []
    except (TypeError, IndexError):
        return []


def _edge_silences(b, m, sid, dub_f, nb, packpool, cuts, mat_end):
    """转场边界两侧可用静音秒数（尾=本幕，头=下一幕）。返回 (tail, head, 下一幕 mode)。
    original 用素材词轨（绝对坐标，speech_tail 单一事实源）；dub 用配音词轨（幕相对）；
    下一幕 none=音轨静音无约束。flash/none 本幕不走本函数（音轨无重叠/静音，天然安全）。
    dub 音频缺失回退原声的幕按 original 算（实际播音轨是素材）。"""
    mode = (b.get("narration") or {}).get("mode") or "original"
    if mode == "dub" and not dub_f:
        mode = "original"
    if mode == "dub":
        sp_end = float((dub_f or {}).get("dur") or 0)
        tail_avail = max(0.0, mat_end - sp_end)
    else:
        words = _track_words(sid, m.get("cut_index"), packpool, cuts)
        out = float(m.get("src_in") or 0) + float(m.get("duration") or 0)
        tail = asr.speech_tail(words, out)
        anchor = tail if tail > 0 else out  # 窗内无语音：预留区只需窗后静音
        nxt = _next_word_start(words, anchor)
        tail_avail = (min(nxt, mat_end) if nxt is not None else mat_end) - anchor
    nmode = ((nb or {}).get("narration") or {}).get("mode") or "original"
    if nmode == "none":
        head_avail = 99.0  # 静音幕：头侧无语音约束
    elif nmode == "dub":
        nw = ((nb.get("narration") or {}).get("words") or [])
        head_avail = float(nw[0]["s"]) if nw else 0.0  # 配音从幕首播：首个配音词前即头侧静音
    else:
        nsid, nci, nin = _peek_src(nb)
        nwords = _track_words(nsid, nci, packpool, cuts)
        first = _first_word_start(nwords, nin, nin + 99)
        head_avail = (first - _prev_word_end(nwords, nin)) if first is not None else 99.0
    return max(0.0, tail_avail), max(0.0, head_avail), nmode

def build_plan(project_dir, sid=None):
    spath, real_sid = story_file(project_dir, sid)
    story = load(spath)
    cpath = f"{project_dir}/materials/cuts.json"
    cuts = load(cpath).get("cuts", []) if os.path.exists(cpath) else []  # 无 ASR 源的项目（如纯画面叙事）合法
    # 素材发声（转写生成器产物）→ 跨包词轨池：key = 素材 id（归一化契约同 v1 词格式）
    packpool = {}
    lp = f"{project_dir}/materials/library.json"
    if os.path.exists(lp):
        for pkid in load(lp).get("packs", []):
            try:
                for mf in load(f"{ROOT}/materials/packs/{pkid}/pack.json").get("files", []):
                    t = mf.get("transcript") or {}
                    if t.get("words"):
                        packpool[mf["id"]] = {"start": 0.0, "end": float(mf.get("duration") or 0), "words": t["words"]}
            except FileNotFoundError:
                pass
    trans = load(f"{ROOT}/registry/transitions.json")

    # Studio 参数回流（人监视器 → JSON 操作台）：params.json 覆写模板参数
    pfile = f"{project_dir}/params.json"
    ov = load(pfile) if os.path.exists(pfile) else {}
    if ov:
        tids = {b.get("transition_out") for b in (story.get("beats") or [])} | {a.get("transition") for a in story.get("acts", [])}
        for tid in tids:
            if tid and tid in trans:
                if "duration" in ov: trans[tid]["duration"] = float(ov["duration"])
                if "hold" in ov: trans[tid]["hold"] = float(ov["hold"])
                if "grain" in ov: trans[tid]["noise"] = int(ov["grain"])

    # 素材解析表：项目自有 sources + 引用的素材包（两层：包=拍摄级，项目=取用级）
    lp = f"{project_dir}/materials/library.json"
    lib = load(lp) if os.path.exists(lp) else {}
    srcmap = {}
    for s in (lib or {}).get("sources", []):
        if s.get("id"): srcmap[s["id"]] = os.path.join(project_dir, s["file"])
    for pid in (lib or {}).get("packs", []):
        try:
            pj = load(f"{ROOT}/materials/packs/{pid}/pack.json")
            for f in pj.get("files", []):
                if f.get("id"): srcmap[f["id"]] = os.path.join(ROOT, "materials", "packs", pid, f["file"])
        except FileNotFoundError:
            pass

    beats = story.get("beats") or story.get("acts") or []  # v4 编号幕（v3/v2/v1 兼容）
    segs = []
    t = 0.0
    pending_head = 0.0  # 上一边界 dt_eff：本幕入点需向静音回退的预留量（仅 original 幕）
    _mdur_cache = {}
    for i, b in enumerate(beats):
        trks = b.get("tracks")
        if trks:  # v4 多轨：A 轨定时长
            A = next((x for x in trks if x.get("role", "A") == "A"), trks[0])
            m = {"src_in": A.get("src_in"), "duration": A.get("duration"),
                 "cut_index": A.get("cut_index", 0), "requirement": A.get("requirement", "")}
            sid = A.get("source_id")
            seg_tracks = [{"role": x.get("role", "A"), "src_in": x.get("src_in"),
                           "dur": x.get("duration"), "pos": x.get("pos"), "scale": x.get("scale"),
                           "op": x.get("op", "overlay-pip"), "requirement": x.get("requirement", ""),
                           "media": srcmap.get(x.get("source_id"),
                                               os.path.join(project_dir, "materials", "src.mp4"))}
                          for x in trks]
        else:      # v3/v1 单素材
            m = (b.get("materials") or [{}])[0]
            sid = m.get("source_id")
            seg_tracks = [{"role": "A", "src_in": m.get("src_in"), "dur": m.get("duration"),
                           "requirement": m.get("requirement", ""),
                           "media": os.path.join(project_dir, "materials", "src.mp4")}]
        dub_f = None
        if (b.get("narration") or {}).get("mode") == "dub":
            df = (b.get("narration") or {}).get("audio")
            if df and not os.path.isabs(df):
                df = os.path.join(project_dir, df)
            if df and not os.path.exists(df):
                print("警告: 幕%s mode=dub 但配音音频不存在（%s）——回退原声" % (b.get("no", 0), df))
                df = None
            if df:
                # 字幕需要文案+实测时长（词轨均分用）；渲染链只要路径
                dub_f = {"path": df, "text": (b.get("story") or "").strip(),
                         "dur": round(_media_duration(df), 1),
                         "words": (b.get("narration") or {}).get("words"),
                         "words_from": (b.get("narration") or {}).get("words_from")}
                # dub 配音实测时长盖过幕视频时长 → 延展 A 轨窗口盖满配音（新素材包冷启动实锤：
                # seg dub 7.6s vs 幕 5.3s → atrim 掐断句子 + 词轨越界 bleed 进下一幕 → R5 字幕页倒置）。
                # 素材余量不足则如实保留（配音截断，remap 钳制 + QC 终审兜底）。
                if dub_f["dur"] > float(m.get("duration") or 0):
                    _a = seg_tracks[0]
                    _mdur = _media_duration(_a.get("media"))
                    _src_in = float(_a.get("src_in") or 0)
                    _reach = round(min(dub_f["dur"], _mdur - _src_in), 1)  # 盖到配音长或素材尽头
                    if _reach >= float(m.get("duration") or 0) + 0.3:
                        _a["dur"] = _reach
                        m["duration"] = _reach
                    else:
                        print(f"警告: 幕{i+1} 配音 {dub_f['dur']}s 超幕 {m.get('duration')}s "
                              f"且素材余量仅 {_mdur - _src_in:.1f}s——配音将截断，QC 会 flagged",
                              flush=True)
                # 词轨↔文本一致性（2026-09-15 15s 错位防线）：显式词轨拼接必须与 story 实义字符全等
                _rw = dub_f.get("words") or []
                if _rw:
                    _wt = "".join(w.get("t", "") for w in _rw)
                    _strip = lambda x: "".join(ch for ch in x if ch not in "，。！？、,.!?;；:： \"'")
                    if _strip(_wt) != _strip(dub_f["text"]):
                        raise SystemExit("故事线幕%d 词轨与文本不一致（词轨=%s… story=%s…）——字幕错位风险，先修词轨再渲染"
                                         % (b.get("no", 0), _wt[:20], dub_f["text"][:20]))
            else:
                print(f"警告: 幕{i+1} 配音文件缺失（{df}），本幕回退原声", flush=True)
        # 转场预留·头侧：上一边界已定 dt_eff，本幕入点向素材静音回退，让淡入区落在首词之前。
        # 仅 original 幕需要平移（dub 音轨从幕首播不受入点影响；none 无语音）。dub/none 的头侧
        # 静音约束已在 _edge_silences 算 dt_eff 时吃掉，这里只做窗口回退 + 幕相对数据同步。
        if pending_head > 0 and (b.get("narration") or {}).get("mode", "original") == "original":
            _win = _track_words(sid, m.get("cut_index"), packpool, cuts)
            _nin = float(m.get("src_in") or 0)
            _first = _first_word_start(_win, _nin, _nin + float(m.get("duration") or 0))
            if _first is not None and _first - _nin < pending_head:
                _ext = round(_nin - (_first - pending_head), 1)
                if _ext > 0:
                    m["src_in"] = round(_nin - _ext, 1)
                    m["duration"] = round(float(m.get("duration") or 0) + _ext, 1)
                    seg_tracks[0]["src_in"] = m["src_in"]
                    seg_tracks[0]["dur"] = m["duration"]
                    # 幕相对数据同步平移：owords 词轨 + 音效 at（贴纸 at_word 走 plan words 自动跟）
                    ow = (b.get("narration") or {}).get("words") or []
                    if ow:
                        b.setdefault("narration", {})["words"] = [
                            {"t": w.get("t"), "s": round(float(w["s"]) + _ext, 2),
                             "e": round(float(w["e"]) + _ext, 2)} for w in ow]
                    for _sx in (b.get("effects") or {}).get("sfx") or []:
                        if _sx.get("at") is not None:
                            _sx["at"] = round(float(_sx["at"]) + _ext, 2)
                    print("  · 幕%s 头预留 %.1fs（入点回退至 %.1fs，淡入区不压首词）"
                          % (b.get("no", i + 1), pending_head, m["src_in"]), flush=True)
        seg = {"id": b.get("id", f"b{i+1}"), "no": b.get("no", i + 1),
               "src_in": m.get("src_in"), "dur": m.get("duration"), "tl_in": round(t, 1),
               "source_id": sid, "cut_index": m.get("cut_index"),
               "src_file": (seg_tracks[0].get("media") if seg_tracks else None),
               "dub": dub_f,
               "owords": (b.get("narration") or {}).get("words"),  # VAD/显式词轨（original 幕通道，2026-09-15）
               "tracks": seg_tracks,
               "effects": b.get("effects", {}),
               "music": {"inherit": (b.get("music") or {}).get("inherit", True),
                         "bgm": (b.get("music") or {}).get("bgm"),
                         "segment": (b.get("music") or {}).get("segment"),
                         "loop": (b.get("music") or {}).get("loop")},  # 幕级 BGM 声明（2026-09-14 接通前端音乐轨开关；segment/loop 2026-09-15）
               "narration": (b.get("narration") or {}).get("mode") or "original"}
        tr_id = b.get("transition_out")
        pending_head = 0.0  # 本幕边界算出后传给下一幕的头预留
        if tr_id and i < len(beats) - 1 and m.get("duration"):
            tr = trans.get(tr_id)  # P2#15：手编故事线引用了不存在的转场 → 明确报错而非 KeyError
            if not tr:
                raise SystemExit("TRANSITION ?? %s（registry 无此转场）——故事线 transition_out 拼写错误" % tr_id)
            if tr["type"] == "flash":
                # flash：插帧自占时长、音轨 concat 不重叠——词头尾天然安全，注册表 dt 原样
                seg["transition"] = tr_id
                t += m["duration"]
            else:
                # 转场预留（2026-09-18 Noah 裁定）：dt 随边缘静音伸缩，不足 0.2s 降级直切
                _mend = float((packpool.get(sid) or {}).get("end") or 0)
                if not _mend:
                    _mp = seg_tracks[0].get("media")
                    if _mp not in _mdur_cache:
                        _mdur_cache[_mp] = _media_duration(_mp) or 0.0
                    _mend = _mdur_cache[_mp]
                tail_avail, head_avail, nmode = _edge_silences(b, m, sid, dub_f, beats[i + 1],
                                                               packpool, cuts, _mend)
                dt_eff = int(min(float(tr["duration"]), tail_avail, head_avail) * 10) / 10.0
                if dt_eff < 0.2:
                    print("  · 幕%s 转场 %s 降级直切（边缘静音 尾%.2fs/头%.2fs 不足 0.2s）"
                          % (b.get("no", i + 1), tr_id, tail_avail, head_avail), flush=True)
                    t += m["duration"]
                else:
                    seg["transition"] = tr_id
                    seg["trans_d"] = dt_eff
                    if dt_eff < float(tr["duration"]):
                        print("  · 幕%s 转场 %s %.1f→%.1fs（边缘静音伸缩）"
                              % (b.get("no", i + 1), tr_id, float(tr["duration"]), dt_eff), flush=True)
                    # 尾预留：出点延到语音尾+dt_eff（窗内已含尾后下一词则不动——语义窗，QC 域）
                    _out0 = float(m.get("src_in") or 0) + float(m.get("duration") or 0)
                    if (b.get("narration") or {}).get("mode") == "dub" and dub_f:
                        _need = max(_out0, float((dub_f or {}).get("dur") or 0) + dt_eff)
                    else:
                        _wtr = _track_words(sid, m.get("cut_index"), packpool, cuts)
                        _tl = asr.speech_tail(_wtr, _out0)
                        if _tl <= 0:
                            _need = _out0  # 窗内无语音：现有窗尾即静音，无需延
                        else:
                            _nxt = _next_word_start(_wtr, _tl)
                            _need = _out0 if (_nxt is not None and _out0 > _nxt + 0.05) else _tl + dt_eff
                    if _need > _out0 + 0.05:
                        m["duration"] = round(_need - float(m.get("src_in") or 0), 1)
                        seg["dur"] = m["duration"]
                        seg_tracks[0]["dur"] = m["duration"]
                        print("  · 幕%s 尾预留 %.1fs（出点延至 %.1fs，淡出区不压尾词）"
                              % (b.get("no", i + 1), dt_eff, _need), flush=True)
                    pending_head = dt_eff if nmode == "original" else 0.0
                    t += m["duration"] - dt_eff
        else:
            t += m.get("duration", 0)
        segs.append(seg)
    total = round(t, 1)

    # 词重映射（narration=none 的幕不产词）——窗口以 segs 为准：转场预留的头尾调整
    # 已并入 seg（原实现从故事线重取窗口，预留调整会被绕过）
    srcs = [None if s.get("narration") == "none" else
            {"cut_index": s.get("cut_index"), "src_in": s["src_in"],
             "duration": s["dur"], "source_id": s.get("source_id")}
            for s in segs]
    words, word_drops = remap_words(cuts, srcs, segs, total, packpool)
    for _d in word_drops:  # 决策点有声化：钳制丢词不再静默（对照 QC 建议避让，比 QC 兜底早一整轮）
        print("警告: 幕%s 词「%s」(%.1fs 起) 落入转场重叠区被钳没（残余<0.08s）——词数减少，字幕页结构可能变化" % (
            _d["beat"], _d["word"], _d["at"]), flush=True)

    audio = story.get("meta", {}).get("audio", {})
    # 画幅：项目级属性（project.json.format 打底，故事线 meta.format 可覆盖单线实验）
    fmt_id = None
    pjf = f"{project_dir}/project.json"
    if os.path.exists(pjf):
        fmt_id = (load(pjf) or {}).get("format")
    fmt_id = (story.get("meta", {}) or {}).get("format") or fmt_id or "vertical"
    fmt = (load(f"{ROOT}/registry/formats.json").get(fmt_id)
           or {"width": 1080, "height": 1920})
    plan = {
        "version": "0.5", "story": real_sid, "fps": 30,
        "width": int(fmt["width"]), "height": int(fmt["height"]),
        "format": fmt_id, "sticker_w": int(fmt.get("sticker_w", 500)), "sticker_h": int(fmt.get("sticker_h", 140)),
        "sticker_top": int(fmt.get("sticker_top", 30)),   # 顶行贴纸 y（2026-09-19 Noah 定：留白140；角位仍 margin 30）
        "sub_clear": int(fmt.get("sub_clear", 360)),
        "duration": total, "title": story["title"],
        "outline": story.get("outline", ""),
        "media": f"{project_dir}/materials/src.mp4",
        "segments": segs, "words": words, "word_drops": word_drops,
        "subtitle_style": story.get("meta", {}).get("style", {}).get("subtitle",
                                story.get("subtitle_style", "karaoke-gold")),
        "transitions": {s["id"]: s.get("transition") for s in segs if s.get("transition")},
        "flash_peak": ov.get("flash"),
        "tconf": trans,
    }
    # BGM 三态解析（2026-09-14 注册表化）：bgm_id（新，查注册表）> bgm_default/bgm（旧，路径直用）
    bgm_reg = load(f"{ROOT}/registry/bgm.json") if os.path.exists(f"{ROOT}/registry/bgm.json") else {}
    bid = audio.get("bgm_id")
    if bid:
        conf = bgm_reg.get(bid)
        if not conf:
            raise SystemExit("BGM ?? %s（bgm.json 无此 id）——故事线 audio.bgm_id 拼写错误" % bid)
        plan["bgm"] = os.path.join(ROOT, conf["file"])
        plan["bgm_id"] = bid
    elif audio.get("bgm") or audio.get("bgm_default"):
        plan["bgm"] = os.path.join(ROOT, audio.get("bgm_default") or audio.get("bgm"))
    plan["bgm_segment"] = audio.get("bgm_segment") or None   # 全局贯穿曲段落（2026-09-15）
    plan["bgm_loop"] = audio.get("bgm_loop")                 # None=按曲长自动
    if audio.get("bgm_volume") is not None:
        plan["bgm_volume"] = float(audio["bgm_volume"])
    # 全局响度归一位（2026-09-16 agent-037 实锤：R6 曾恒 warn"管线无 loudnorm 位"）：
    # True=EBU R128 锚 -16 LUFS（单遍 loudnorm）；数字=自定义 I；false=关（特殊混音需求）
    plan["loudnorm"] = audio.get("loudnorm", True)
    plan["meta"] = story.get("meta", {})
    sfx = f"-{real_sid}" if real_sid else ""
    with open(f"{project_dir}/plan{sfx}.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    return plan, real_sid

def pos_xy(pos, w, h, sw, sh, margin=30, sub_clear=360, top_y=None):
    ty = margin if top_y is None else top_y   # top_y=顶中独享下沉位（sticker_top），角位保持 margin
    table = {"top-left": (margin, margin), "top-center": ((w - sw) // 2, ty),
             "top-right": (w - sw - margin, margin), "center": ((w - sw) // 2, (h - sh) // 2),
             "bottom-left": (margin, h - sh - sub_clear), "bottom-right": (w - sw - margin, h - sh - sub_clear)}
    return table.get(pos, (margin, margin))

def pos_expr(pos, margin=30, sub_clear=360, top_y=None):
    """overlay 表达式版 pos_xy（v2 贴纸宽随文字自适应，须按实际 overlay_w/h 定位），
    margin/sub_clear/top_y 语义与 pos_xy 完全一致。"""
    ty = margin if top_y is None else top_y
    table = {"top-left": ("%d" % margin, "%d" % margin),
             "top-center": ("(main_w-overlay_w)/2", "%d" % ty),
             "top-right": ("main_w-overlay_w-%d" % margin, "%d" % margin),
             "center": ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2"),
             "bottom-left": ("%d" % margin, "main_h-overlay_h-%d" % sub_clear),
             "bottom-right": ("main_w-overlay_w-%d" % margin, "main_h-overlay_h-%d" % sub_clear)}
    return table.get(pos, ("%d" % margin, "%d" % margin))

def _sticker_v2_style(conf):
    """注册表样式是否 v2 词根（overlay 几何分流用，与 sticker_file 分派同判据）。"""
    return any(k in (conf.get("text_style") or {}) for k in _STICKER_V2_KEYS)

def word_time(words, at_word, seg):
    """贴纸词点触发：在该幕时间窗内找含关键词的词的起始时刻。
    匹配两级（2026-09-18 agent 实锤：vadwords 词轨单字粒度，多字 at_word「低价」substring
    永不命中 → 曾静默回退 0.4s 零告警）：①词项 substring；②字符序列——窗口内连续词项
    逐字拼出 at_word，命中取首词起点。两级全空才回退，且回退必打渲染日志 warn
    （word_drops 同族，commit 62a6429）。"""
    if not at_word:
        return None
    win = [wd for wd in words
           if seg["tl_in"] - 0.05 <= wd["s"] < seg["tl_in"] + float(seg["dur"])]
    for wd in win:
        if at_word in wd["t"]:
            return wd["s"]
    n = len(at_word)
    for i in range(len(win) - n + 1):
        if "".join(wd["t"] for wd in win[i:i + n]) == at_word:
            return win[i]["s"]
    print("⚠ 贴纸 at_word「%s」未命中幕%s 词轨，回退 t0=0.4s" % (at_word, seg.get("no")), flush=True)
    return None


def _ff_escape_path(p):
    r"""drawtext 滤镜内路径：正斜杠化 + 冒号转义（Windows 盘符 C\:/），单引号包住。"""
    return "'" + p.replace("\\", "/").replace(":", "\\:") + "'"


_STICKER_FONTS = {  # 贴纸可选字体（assets/fonts/ 下文件名）
    "wenkai": "LXGWWenKai-Regular.ttf",   # 文楷·楷体手写（默认，存量样式零改动）
    "smiley": "SmileySans-Oblique.ttf",    # 得意黑·现代斜体标题
    "kuaile": "ZCOOLKuaiLe-Regular.ttf",  # 站酷快乐体·圆润活泼
}
_STICKER_V2_KEYS = ("font", "radius", "shadow", "ring")  # v2 样式词根：任一在场走紧裁路径


def _sticker_bbox(png):
    """alpha 包围盒（alphaextract→cropdetect 两段式），返回 (w,h,x,y) 或 None。"""
    r = subprocess.run([FF, "-loglevel", "info", "-i", png, "-vf",
                        "format=rgba,alphaextract,cropdetect=limit=16:round=2:skip=0",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"crop=(\d+):(\d+):(-?\d+):(-?\d+)", r.stderr or "")
    return tuple(map(int, m[-1])) if m else None


def _sticker_v2(out, tf, st, fs):
    """v2 贴纸现画（2026-09-18 现代 · 活泼样式批）：紧裁画布 + 圆角掩膜 +
    分层绘制（硬投影→外框环/白环→主体）。字体可选，尺寸随文字自适应。
    词根：font/radius/shadow(+shadow_c)/ring(+ring_c)/border_w=盒外描边宽。"""
    font = os.path.join(ROOT, "assets", "fonts",
                        _STICKER_FONTS.get(st.get("font"), _STICKER_FONTS["wenkai"]))
    pad = int(st.get("box_pad", 30)); bw = int(st.get("stroke_w", 0))
    ring = int(st.get("ring", 0)); sh = int(st.get("shadow", 0))
    box = st.get("bg"); fg = st.get("fg", "0xFFFFFF")
    txt = open(tf, encoding="utf-8").read()
    cw = fs * (len(txt) + 2) + 2 * (pad + bw + ring + sh) + 160
    ch = int(fs * 2.6) + 2 * (pad + bw + ring + sh)

    def _dt(color, borderw=0, bordercolor="0x000000", boxcolor=None, boxpad=0, dx=0, dy=0):
        p = ("drawtext=fontfile=%s:textfile=%s:fontcolor=%s:fontsize=%d"
             ":x=(w-text_w)/2+%d:y=(h-text_h)/2+%d"
             % (_ff_escape_path(font), _ff_escape_path(tf), color, fs, dx, dy))
        if borderw:
            p += ":borderw=%d:bordercolor=%s" % (borderw, bordercolor)
        if boxcolor:
            p += ":box=1:boxcolor=%s:boxborderw=%d" % (boxcolor, boxpad)
        return p

    passes = []
    if box:  # 盒式：投影(盒) → 外框环(大一号盒垫底) → 主体盒
        if sh:
            passes.append(_dt("0x000000@0", boxcolor=st.get("shadow_c", "0x111111"),
                              boxpad=pad + bw, dx=sh, dy=sh))
        if bw:
            passes.append(_dt("0x000000@0", boxcolor=st.get("stroke", "0x111111"), boxpad=pad + bw))
        passes.append(_dt(fg, boxcolor=box, boxpad=pad))
    else:    # 裸字式：投影(带描边剪影) → 白环/粗描边 → 主体
        if sh:
            passes.append(_dt(fg, borderw=max(bw, ring), bordercolor=st.get("shadow_c", "0x111111"),
                              dx=sh, dy=sh))
        if ring:
            passes.append(_dt(fg, borderw=ring, bordercolor=st.get("ring_c", "0xFFFFFF")))
        passes.append(_dt(fg, borderw=bw, bordercolor=st.get("stroke", "0x111111")))

    raw = "%s.raw%d.png" % (out, os.getpid())  # 并发同 key：中间帧带 pid 防互踩
    r = subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "color=c=black@0.0:s=%dx%d,format=rgba" % (cw, ch),
                        "-vf", ",".join(passes), "-frames:v", "1", raw],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(raw):
        raise RuntimeError("贴纸现画失败 %r: %s" % (txt, (r.stderr or "")[-160:]))
    bb = _sticker_bbox(raw)
    if not bb or bb[0] < 4:
        raise RuntimeError("贴纸包围盒探测失败 %r" % txt)
    bw2, bh2, bx, by = bb
    chain = ["crop=%d:%d:%d:%d" % (bw2, bh2, bx, by)]
    R = int(st.get("radius") or 0)
    if R:
        R = min(R, bh2 // 2)  # ≥半高即胶囊
        corner = ("hypot(max(max(%d-X,X-%d),0),max(max(%d-Y,Y-%d),0))"
                  % (R, bw2 - 1 - R, R, bh2 - 1 - R))
        chain.append("format=rgba")
        chain.append("geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"
                     ":a='if(lt(%s,%d),alpha(X,Y),0)'" % (corner, R + 1))
    tmp = "%s.tmp%d.png" % (out, os.getpid())
    r = subprocess.run([FF, "-y", "-loglevel", "error", "-i", raw, "-vf",
                        ",".join(chain), "-frames:v", "1", tmp],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("贴纸圆角失败 %r: %s" % (txt, (r.stderr or "")[-160:]))
    os.replace(tmp, out)
    try:
        os.remove(raw)
    except OSError:
        pass
    return out


def sticker_file(conf, stk):
    """贴纸文件解析（2026-09-18 文字模板制，v1 规矩回归：贴纸文字跟内容走，≤6 字短语）。
    - conf 带 text_style → drawtext 现画 PNG（缓存 materials/.sticker_cache/，文字+样式哈希键，
      同短语不重画）；文字取 stk.text，缺省回退注册表 default_text（老故事线无 text 不断链）。
    - 否则按 conf/stk 的 file 走静态 PNG（图标类兼容路径）。
    返回 ""（调用方跳过）当：无样式无文件 / 文字模板无文字可用。"""
    st = conf.get("text_style")
    if not st:
        fp = conf.get("file") or stk.get("file")
        return os.path.join(ROOT, fp) if fp else ""   # 已删贴纸 id：无样式无文件 → 返回 "" 调用方跳过（2026-09-19 CI 实锤：原 os.path.join(ROOT,"")=仓库根目录，exists 为真不跳过，ffmpeg 拿目录当输入整渲染炸——与音效"引用已删自动跳过不崩"同纪律）
    txt = str(stk.get("text") or conf.get("default_text") or "").strip()
    if not txt:
        return ""
    import hashlib
    key = hashlib.md5((json.dumps([conf.get("id", ""), txt, st], ensure_ascii=False,
                                  sort_keys=True)).encode("utf-8")).hexdigest()[:16]
    cache = os.path.join(ROOT, "materials", ".sticker_cache")
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, key + ".png")
    if os.path.exists(out):
        return out
    tf = os.path.join(cache, key + ".txt")   # textfile 传参：绕开 drawtext 转义地狱
    ttmp = "%s.tmp%d" % (tf, os.getpid())    # 并发同 key 双写者：txt/PNG 全 tmp(pid)+原子换名，读者不见半截
    open(ttmp, "w", encoding="utf-8").write(txt)
    os.replace(ttmp, tf)
    if any(k in st for k in _STICKER_V2_KEYS):   # v2 现代样式批：紧裁+圆角+分层
        return _sticker_v2(out, tf, st, int(st.get("font_size", 96)))
    font = os.path.join(ROOT, "assets", "fonts", "LXGWWenKai-Regular.ttf")
    vf = ("drawtext=fontfile=%s:textfile=%s:fontcolor=%s:fontsize=%d:"
          "borderw=%d:bordercolor=%s:box=1:boxcolor=%s@1.0:boxborderw=%d:"
          "x=(w-text_w)/2:y=(h-text_h)/2"
          % (_ff_escape_path(font), _ff_escape_path(tf),
             st.get("fg", "0xFFFFFF"), int(st.get("font_size", 96)),
             int(st.get("stroke_w", 5)), st.get("stroke", "0x000000"),
             st.get("bg", "0xFFD400"), int(st.get("box_pad", 24))))
    tmp_png = "%s.tmp%d.png" % (out, os.getpid())
    r = subprocess.run([FF, "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=black@0.0:s=1200x300,format=rgba",
                        "-frames:v", "1", "-vf", vf, tmp_png], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp_png):
        raise RuntimeError("贴纸现画失败 %r: %s" % (txt, (r.stderr or "")[-160:]))
    os.replace(tmp_png, out)
    return out

def build_cmd(plan, ass_path, out_path):
    w, h, fps = plan["width"], plan["height"], plan["fps"]
    total = plan["duration"]
    trans = plan.get("tconf") or load(f"{ROOT}/registry/transitions.json")
    reg_stk = load(f"{ROOT}/registry/stickers.json")
    reg_sfx = load(f"{ROOT}/registry/sfx.json")
    inputs, fc = [], []
    ni = 0
    beat_v = []   # (video_label, a_audio_input_idx, seg)
    sfx_in = []   # (input_idx, abs_ms)
    win = None
    for bi, seg in enumerate(plan["segments"]):
        # 轨输入
        a_idx = None; bs = []
        for tr in seg.get("tracks") or []:
            inputs += ["-ss", str(tr.get("src_in", seg["src_in"])), "-t", str(tr.get("dur", seg["dur"])), "-i", tr.get("media") or plan["media"]]
            if tr.get("role", "A") == "A" and a_idx is None:
                a_idx = ni
            else:
                bs.append((ni, tr))
            ni += 1
        fc.append(f"[{a_idx}:v]scale={w}:{h},settb=AVTB,fps={fps},format=yuv420p[bv{bi}]")
        cur = f"bv{bi}"
        for k, (idx, tr) in enumerate(bs):  # B 轨：画中画叠加
            sc = float(tr.get("scale") or 0.3); pw = int(w * sc)
            x, y = pos_xy(tr.get("pos", "top-right"), w, h, pw, int(pw * h / w), sub_clear=plan.get("sub_clear", 360))
            fc.append(f"[{idx}:v]scale={pw}:-2,settb=AVTB,fps={fps}[bp{bi}{k}]")
            fc.append(f"[{cur}][bp{bi}{k}]overlay={x}:{y}[bo{bi}{k}]")
            cur = f"bo{bi}{k}"
        for k, stk in enumerate((seg.get("effects") or {}).get("stickers") or []):  # 贴纸：词点触发
            conf = reg_stk.get(stk.get("asset") or "") or {}
            fpath = sticker_file(conf, stk)
            if not fpath or not os.path.exists(fpath):
                continue
            inputs += ["-i", fpath]; sidx = ni; ni += 1
            _wt = word_time(plan["words"], stk.get("at_word"), seg)
            # 贴纸 overlay 作用于 -ss 裁剪后的单幕局部时间轴（t 从 0 计）——绝对时刻必须重定基，
            # 否则第 2 幕起的贴纸 enable 时刻超出本幕长度，永远不出现
            t0 = round(_wt - float(seg["tl_in"]), 2) if _wt is not None else 0.4
            sd = float(stk.get("duration") or conf.get("duration") or 1.2)
            pos_k = stk.get("pos") or conf.get("pos", "top-center")
            if _sticker_v2_style(conf):
                # v2 贴纸：紧裁画布宽随文字——高度锁框、overlay 表达式按实际宽高定位
                xe, ye = pos_expr(pos_k, sub_clear=plan.get("sub_clear", 360), top_y=plan.get("sticker_top"))
                fc.append(f"[{sidx}:v]scale=-1:{plan.get('sticker_h', 140)},settb=AVTB,fps={fps}[st{bi}{k}]")
                fc.append(f"[{cur}][st{bi}{k}]overlay={xe}:{ye}:enable='between(t,{t0:.2f},{t0+sd:.2f})'[bs{bi}{k}]")
            else:
                x, y = pos_xy(pos_k, w, h, plan.get("sticker_w", 500), plan.get("sticker_h", 140), sub_clear=plan.get("sub_clear", 360), top_y=plan.get("sticker_top"))
                # 贴纸统一缩放进 500x140 贴纸框（文字模板画布 1200x300 也归一到同框，
                # 与静态 PNG 时代的几何语义一致——否则按原尺寸叠，文字块会溢出屏幕）
                fc.append(f"[{sidx}:v]scale={plan.get('sticker_w', 500)}:{plan.get('sticker_h', 140)},settb=AVTB,fps={fps}[st{bi}{k}]")
                fc.append(f"[{cur}][st{bi}{k}]overlay={x}:{y}:enable='between(t,{t0:.2f},{t0+sd:.2f})'[bs{bi}{k}]")
            cur = f"bs{bi}{k}"
        beat_v.append((cur, a_idx, seg))
        for sfx in (seg.get("effects") or {}).get("sfx") or []:  # 音效：时刻混入
            conf = reg_sfx.get(sfx.get("asset") or "") or {}
            fpath = os.path.join(ROOT, conf.get("file") or sfx.get("file", ""))
            if not os.path.exists(fpath):
                continue
            inputs += ["-i", fpath]; sidx = ni; ni += 1
            sfx_in.append((sidx, seg, float(sfx.get("at", 0))))
    # 转场链
    if not beat_v:
        raise SystemExit("RENDER ABORT：无有效幕（全部素材被剔除？）")
    cur = beat_v[0][0]
    concat_v = None  # 无转场幕序列（None 直切），攒齐后 concat 进链
    for bi in range(len(beat_v) - 1):
        s = plan["segments"][bi]
        if not s.get("transition"):
            if concat_v is None:
                concat_v = [cur]
            concat_v.append(beat_v[bi + 1][0])
            cur = beat_v[bi + 1][0]
            continue
        if concat_v is not None:  # 直切段收口：concat 后作为单一源继续
            if len(concat_v) > 1:
                # concat 输出时基与流不同（1/1000000 vs 1/30），必须归一，否则后续 xfade 报
                # "main timebase do not match ... xfade timebase"（2026-09-11 全装饰版实锤）
                fc.append("%sconcat=n=%d:v=1:a=0,settb=AVTB,fps=%s[cc]"
                          % ("".join(f"[{x}]" for x in concat_v), len(concat_v), fps))
                cur = "cc"
            concat_v = None
        tr = trans[s["transition"]]
        dt = float(s.get("trans_d") or tr["duration"])  # 转场预留伸缩后的实际时长（无预留=注册表值）
        off = s["tl_in"] + s["dur"] - dt
        nxt = beat_v[bi + 1][0]
        if tr["type"] == "flash":
            fname = f"fl{bi}"
            fc.append(f"color=c={tr['color']}:s={w}x{h}:r={fps}:d={tr['hold']},settb=AVTB,fps={fps},"
                      f"noise=alls={tr['noise']}:allf=t,format=yuv420p[{fname}]")
            fc.append(f"[{cur}][{fname}]xfade=transition=fade:duration={dt}:offset={off:.2f}[x{bi}]")
            cur = f"x{bi}"
            off2 = round(off + dt, 2)
            fc.append(f"[{cur}][{nxt}]xfade=transition=fade:duration={dt}:offset={off2:.2f}[x{bi}b]")
            cur = f"x{bi}b"
            win = (off, round(off + 2 * dt, 2))
        else:
            fc.append(f"[{cur}][{nxt}]xfade=transition={tr['preset']}:duration={dt}:offset={off:.2f}[x{bi}]")
            cur = f"x{bi}"
    if concat_v is not None and len(concat_v) > 1:  # 尾部直切段收口（同样归一时基）
        fc.append("%sconcat=n=%d:v=1:a=0,settb=AVTB,fps=%s[cc]"
                  % ("".join(f"[{x}]" for x in concat_v), len(concat_v), fps))
        cur = "cc"
    if plan.get("flash_peak") is not None and win:
        o, e = win; pk = float(plan["flash_peak"])
        fc.append(f"[{cur}]eq=eval=frame:brightness='if(between(t,{o:.2f},{e:.2f}),{pk:.2f}*sin((t-{o:.2f})/{e-o:.2f}*PI),0)'[fx]")
        cur = "fx"
    fc.append(f"[{cur}]{_ass_spec(ass_path)}[vout]")
    # 音频：原声链（dub 幕换配音轨）+ BGM + 音效
    for j, (lbl, a_idx, seg) in enumerate(beat_v):
        if seg.get("dub"):  # 配音幕：配音轨进链，原声弃用；配音短于画面则尾部静音补齐
            dpath = seg["dub"]["path"]  # build_plan 存 {path,text,dur}
            inputs += ["-i", dpath]; d_idx = ni; ni += 1
            fc.append(f"[{d_idx}:a]atrim=0:{seg['dur']},asetpts=PTS-STARTPTS,"
                      f"apad=whole_dur={seg['dur']}[vc{j}]")
        elif seg.get("narration") == "none":  # 三态之静音：人声弃用，只留 BGM/音效（空镜幕语义）
            fc.append(f"aevalsrc=0:d={seg['dur']}:s=32000[vc{j}]")
        else:
            fc.append(f"[{a_idx}:a]atrim=0:{seg['dur']},asetpts=PTS-STARTPTS[vc{j}]")
    vp = "vc0"
    for j in range(1, len(beat_v)):
        d = _overlap_d(plan, plan["segments"][j - 1])  # R2-5：音频重叠时长唯一实现（原与 sfx 循环双副本）
        if d > 0:
            fc.append(f"[{vp}][vc{j}]acrossfade=d={d}[v{j}]")
        else:  # 直切：音频无重叠串联（apad 已把各幕配齐，直接 concat 保持时轴 1:1）
            fc.append(f"[{vp}][vc{j}]concat=n=2:v=0:a=1[v{j}]")
        vp = f"v{j}"
    mix = f"[{vp}]"; n_in = 1
    # 音频链时轴（幕起点累计）——BGM 分组与音效落点共用同一时轴（_overlap_d 同源，R2-5）
    a_starts, _aa = [], 0.0
    for j, (_l, _a, sg) in enumerate(beat_v):
        a_starts.append(round(_aa, 2))
        _aa += float(sg["dur"]) - (_overlap_d(plan, plan["segments"][j - 1]) if j else 0.0)
    # BGM 区间化（真实项目问答）：前端每幕"继承贯穿 BGM"开关接通渲染——
    # inherit=true → 贯穿曲（meta.audio.bgm_id）；inherit=false+bgm=<id> → 幕级独立换曲；
    # inherit=false+bgm=null → 该幕无 BGM（人声/旁白裸奔）。连续同源幕合为一组共享 input，
    # 组段按音频链时轴（a_starts 同源逻辑）切齐后 concat——组间硬切，组首尾各自淡入淡出。
    # 曲长 < 段长自动循环（-stream_loop），afade 收尾由 atrim 精确截断。
    # 段落化（2026-09-15 维护者 问答 #7）：music.segment / meta.audio.bgm_segment 引用注册表
    # segments 段落名——组渲染改为「取段落窗口 + aloop 段落循环 / apad 不足补静音」，
    # 不再用曲位=时间轴位语义；loop 标志（幕级 music.loop / 全局 bgm_loop）覆盖注册表默认。
    _reg_bgm = load(f"{ROOT}/registry/bgm.json") if os.path.exists(f"{ROOT}/registry/bgm.json") else {}
    def _bgm_src(seg):
        mu = seg.get("music") or {}
        if mu.get("inherit", True):
            if not plan.get("bgm"):
                return (None, None, None)
            _seg = mu.get("segment") or plan.get("bgm_segment")   # 幕级覆盖贯穿曲段落（维护者 #7）
            _lp = mu.get("loop") if mu.get("loop") is not None else plan.get("bgm_loop")
            return (("global", _seg, _lp), plan.get("bgm"), (plan.get("bgm_id"), _seg, _lp))
        bid = mu.get("bgm")
        conf = _reg_bgm.get(bid) if bid else None
        if conf and os.path.exists(os.path.join(ROOT, conf["file"])):
            return ((bid, mu.get("segment"), mu.get("loop")),
                    os.path.join(ROOT, conf["file"]), (bid, mu.get("segment"), mu.get("loop")))
        return (None, None, None)  # 独立换曲 id 无效 → 如实静音（不打断渲染，QC 可查）
    def _seg_window(conf_id, segname, loop_ovr):
        """注册表段落 → {in, out, loop}；段落名无效如实告警回退整条。"""
        conf = _reg_bgm.get(conf_id) or {}
        in0, out0 = 0.0, None
        if segname:
            s = next((x for x in (conf.get("segments") or []) if x.get("name") == segname), None)
            if s:
                in0, out0 = float(s.get("in") or 0), s.get("out")
            else:
                print("警告: BGM %s 无段落「%s」——回退整条" % (conf_id, segname))
        loop = conf.get("loop", True) if loop_ovr is None else bool(loop_ovr)
        return in0, (float(out0) if out0 is not None else None), loop
    _bgm_groups = []  # [key, file, src3, [seg_idx,...]]
    for _si, _seg in enumerate(plan["segments"]):
        _k, _f, _src = _bgm_src(_seg)
        if _bgm_groups and _bgm_groups[-1][0] == _k:
            _bgm_groups[-1][3].append(_si)
        else:
            _bgm_groups.append([_k, _f, _src, [_si]])
    _bgm_chain = []
    for _gi, (_k, _fp, _src, _sidx) in enumerate(_bgm_groups):
        _g0 = a_starts[_sidx[0]]
        _last = plan["segments"][_sidx[-1]]
        _g1 = a_starts[_sidx[-1]] + float(_last["dur"]) - (_overlap_d(plan, plan["segments"][_sidx[-1] - 1]) if _sidx[-1] else 0.0)
        _glen = max(0.5, _g1 - _g0)
        if not _fp:
            # 静音组占位（2026-09-14 实测实锤）：concat 是顺序拼接不是时间对齐——
            # 静音区间若无等长占位段，后段曲子整体前移（幕4 静音洞被填 + 片尾提前无 BGM 双重错位）
            fc.append(f"aevalsrc=0:d={_glen:.2f}:s=32000[bg{_gi}]")
            _bgm_chain.append(f"[bg{_gi}]")
            continue
        _r = subprocess.run([FF, "-hide_banner", "-i", _fp], capture_output=True, text=True, encoding="utf-8", errors="replace")
        import re as _re
        _mm = _re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", _r.stderr or "")
        _m_dur = (int(_mm.group(1)) * 3600 + int(_mm.group(2)) * 60 + float(_mm.group(3))) if _mm else 0
        _vol = plan.get('bgm_volume', 0.22)
        _in0, _out0, _loop = _seg_window(*_src)
        if _in0 > 0 or _out0 is not None:
            if not _m_dur:  # probe 失败拿不到曲长 → 段落窗口无从定位，如实回退整条语义
                print("警告: BGM %s 时长探测失败——段落「%s」回退整条" % (_src[0], _src[1]))
                _in0, _out0 = 0.0, None
        if _in0 > 0 or _out0 is not None:
            # 段落窗口：取 [in0,out0) 段，loop=true 且段短于组 → aloop 循环盖满；否则 apad 补静音
            _end = _out0 if (_out0 is not None and _out0 > _in0) else _m_dur
            _seglen = max(0.3, _end - _in0)
            inputs += ["-i", _fp]
            bgm_idx = ni; ni += 1
            _mid = (f"aloop=loop=-1:size=2147483647,atrim=0:{_glen:.2f}," if (_loop and _seglen < _glen)
                    else f"apad,atrim=0:{_glen:.2f},")
            fc.append(f"[{bgm_idx}:a]atrim=start={_in0:.2f}:end={_end:.2f},asetpts=PTS-STARTPTS,"
                      + _mid + f"volume={_vol},"
                      f"afade=t=in:st=0:d=0.5,afade=t=out:st={max(0, _glen - 1):.2f}:d=1.0[bg{_gi}]")
            _bgm_chain.append(f"[bg{_gi}]")
            continue
        if _m_dur and _m_dur < _glen:
            inputs += ["-stream_loop", "-1", "-i", _fp]
        else:
            inputs += ["-i", _fp]
        bgm_idx = ni; ni += 1
        fc.append(f"[{bgm_idx}:a]atrim=start={_g0:.2f}:end={_g1:.2f},asetpts=PTS-STARTPTS,volume={_vol},"
                  f"afade=t=in:st=0:d=0.5,afade=t=out:st={max(0, _glen - 1):.2f}:d=1.0[bg{_gi}]")
        _bgm_chain.append(f"[bg{_gi}]")
    if _bgm_chain:
        if len(_bgm_chain) == 1:
            mix += _bgm_chain[0]
        else:
            fc.append("".join(_bgm_chain) + f"concat=n={len(_bgm_chain)}:v=0:a=1[bgcat]")
            mix += "[bgcat]"
        n_in += 1
    for k, (sidx, sg, at) in enumerate(sfx_in):
        j0 = next(i for i, tup in enumerate(beat_v) if tup[2] is sg)
        fc.append(f"[{sidx}:a]adelay={int(round((a_starts[j0] + at) * 1000))}:all=1[sx{k}]")
        mix += f"[sx{k}]"; n_in += 1
    _ln = plan.get("loudnorm", True)
    _lnf = "" if _ln is False else ",loudnorm=I=%s:TP=-1.5:LRA=11" % (-16 if _ln is True else float(_ln))
    fc.append(f"{mix}amix=inputs={n_in}:duration=first:normalize=0{_lnf}[amix]")
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", "[amix]", "-t", f"{total}",
        "-c:v", hw_encoder(), "-b:v", "6M",
        "-c:a", "aac", "-b:a", "128k", out_path]
    return cmd

def _cover_frame(plan, c, out):
    """代表帧抽取（素材侧——渲染前即可出封面，2026-09-18 Noah 封面前置裁定的地基）：
    指定幕(beat_no)+幕内偏移(at)，缺省首幕入点；拼接源(src.mp4)优先，回退该幕源文件。"""
    segs = plan.get("segments") or []
    seg = next((s for s in segs if s.get("no") == c.get("beat_no")), None) if c.get("beat_no") else None
    seg = seg or (segs[0] if segs else None)
    if not seg:
        return None
    csrc = plan["media"] if os.path.exists(plan.get("media") or "") else seg.get("src_file")
    if not (csrc and os.path.exists(csrc)):
        return None
    subprocess.run([FF, "-y", "-loglevel", "error", "-ss", str(float(seg["src_in"]) + float(c.get("at", 0))),
                    "-i", csrc, "-frames:v", "1", "-q:v", "2", out], capture_output=True)
    return out if os.path.exists(out) else None


_COVER_FONT = os.path.join(ROOT, "assets", "fonts", "LXGWWenKai-Regular.ttf")


def _compose_cover_title(base, title, out, w=1080, h=1920):
    """核心标题合成（确定性 drawtext——AI 画中文字=假字高危，标题一律本地渲染叠加）。
    ≤10 字/行自动折行，排版画面上部（y=8% 起、行距 120px/1080 宽基准），白字黑描边任何底图可读，
    统一成片画幅收口；tmp+os.replace 原子换名，合成中断不留半截封面。"""
    lines = [title[i:i + 10] for i in range(0, len(title), 10)]
    fd, ext = os.path.splitext(out)
    tfd = fd + ".title"
    os.makedirs(tfd, exist_ok=True)
    vf = ["scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1" % (w, h, w, h)]
    for i, ln in enumerate(lines):
        tf = os.path.join(tfd, "l%d.txt" % i)
        open(tf, "w", encoding="utf-8").write(ln)  # textfile 传参：绕开 drawtext 转义地狱
        vf.append("drawtext=fontfile=%s:textfile=%s:fontcolor=0xFFFFFF:fontsize=88:"
                  "borderw=7:bordercolor=0x101010@0.85:x=(w-text_w)/2:y=h*0.08+%d"
                  % (_ff_escape_path(_COVER_FONT), _ff_escape_path(tf), i * 120))
    tmp = fd + ".t" + ext
    r = subprocess.run([FF, "-y", "-loglevel", "error", "-i", base, "-frames:v", "1",
                        "-vf", ",".join(vf), "-q:v", "2", tmp], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("封面标题合成失败 %r: %s" % (title[:16], (r.stderr or "")[-200:]))
    os.replace(tmp, out)


def build_cover(plan, project_dir, sfx=""):
    """封面收口（2026-09-18 新流程——生成+检查在渲染前，Noah 裁定）：
    · 非 AI = 素材代表帧（first-frame/beat-frame）或已上传图；ai-generated = 代表帧底图
      + prompt 走 image-01 subject_reference 保主体生成（⚠ 底图含真人脸会被平台审核拦 1026，
      含人封面用帧截图策略 + title_text——真脸保真唯一路线）
    · output-frame = 渲染后成片抽帧（唯一后置特例：封面=发布产物本体，含字幕全要素）
    · title_text 非空 → 一律本地 drawtext 合成核心标题
    · 任何产物归一 cover{sfx}.jpg 槽（前端 files.cover 只认这个名，2026-09-15 实锤）"""
    c = (plan.get("meta") or {}).get("cover", {}) or {}
    strategy = c.get("strategy", "first-frame")
    cover_dir = f"{project_dir}/cover"
    os.makedirs(cover_dir, exist_ok=True)
    out = f"{cover_dir}/cover{sfx}.jpg"
    title = str(c.get("title_text") or "").strip()
    base = None
    if strategy == "output-frame":
        op = os.path.join(project_dir, f"out{sfx}.mp4")
        if os.path.exists(op):
            subprocess.run([FF, "-y", "-loglevel", "error", "-ss", str(float(c.get("at", 0.4))),
                            "-i", op, "-frames:v", "1", "-q:v", "2", out], capture_output=True)
            base = out if os.path.exists(out) else None
    elif strategy == "upload":
        for cand in ("upload.jpg", "upload.png", "generated.png", "generated.jpg"):
            p = f"{cover_dir}/{cand}"
            if os.path.exists(p):
                base = p
                break
    else:  # first-frame / beat-frame / ai-generated：底图一律素材代表帧
        base = _cover_frame(plan, c, f"{cover_dir}/.frame{sfx}.jpg")
        if strategy == "ai-generated":
            prompt = str(c.get("prompt") or "").strip()
            if not base:
                return None
            if not prompt:
                raise RuntimeError("AI 封面缺 prompt（meta.cover.prompt）——写作建议见操作手册封面章节")
            import image_gen
            try:
                image_gen.gen_image(prompt, out, aspect_ratio="9:16", base_image=base)
            except RuntimeError as e:
                raise RuntimeError("AI 封面生成失败——底图含真人脸会被平台审核拦(1026)，"
                                   "含人封面改 first-frame/beat-frame + title_text: %s" % e)
            base = out if os.path.exists(out) else None
    if not base:
        return None
    w, h = int(plan.get("width") or 1080), int(plan.get("height") or 1920)
    if title:
        _compose_cover_title(base, title, out, w, h)
    else:
        # 归一到成片画幅（2026-09-18 e2e 实锤：素材横拍竖裁时原始帧方向≠成片——封面=发布
        # 产物形态，一律画幅收口；base==out 原位换名，!=out 兼掉旧 copyfile）
        fd, ext = os.path.splitext(out)
        tmp = fd + ".t" + ext
        r = subprocess.run([FF, "-y", "-loglevel", "error", "-i", base, "-frames:v", "1",
                            "-vf", "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1"
                                   % (w, h, w, h), "-q:v", "2", tmp], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise RuntimeError("封面画幅归一失败: %s" % (r.stderr or "")[-200:])
        os.replace(tmp, out)
    return out if os.path.exists(out) else None


def cover_check(img, plan_wh=None):
    """封面体检（渲染前闸门判据）：在盘/非损坏/方向与成片画幅一致/非空白。返回 (ok, 说明)。
    plan_wh=(w,h) 传入=按成片画幅判方向（横版项目横版封面合法，2026-09-18 e2e 实锤：
    曾写死竖版把横版夹具全拦）；缺省仍按竖版（主业务竖版短视频）。"""
    if not (img and os.path.exists(img)):
        return False, "封面文件缺失"
    kb = os.path.getsize(img) // 1024
    if kb < 20:
        return False, "封面过小 %dKB（疑似损坏）" % kb
    fp = next((c for c in (os.environ.get("FFPROBE"), os.path.join(ROOT, "bin", "ffprobe"),
                           os.path.join(ROOT, "bin", "ffprobe.exe")) if c and os.path.exists(c)),
              None) or shutil.which("ffprobe")
    orient = "竖版" if not plan_wh or plan_wh[1] >= plan_wh[0] else "横版"
    if fp:
        r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", img],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            w, h = (int(x) for x in r.stdout.strip().split(","))
            if plan_wh:
                if (h >= w) != (plan_wh[1] >= plan_wh[0]):
                    return False, "封面方向与画幅不符（封面 %dx%d，画幅 %dx%d）" % (w, h, plan_wh[0], plan_wh[1])
            elif w >= h:
                return False, "封面非竖版（%dx%d）" % (w, h)
        except Exception:
            return False, "封面尺寸探测失败（损坏）"
    r = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", img,
                        "-vf", "scale=1:1,format=gray", "-frames:v", "1",
                        "-f", "rawvideo", "-"], capture_output=True)
    if not r.stdout:
        return False, "封面解码失败（损坏）"
    luma = r.stdout[0]
    if luma < 8 or luma > 247:
        return False, "封面疑似空白（平均亮度 %d）" % luma
    return True, "%s %dKB 亮度%d" % (orient, kb, luma)

def build_beat_cmd(plan, seg, ass_path, out_path):
    """单幕独立渲染：多轨叠加 + 贴纸 + 音效 + 本幕字幕（时间轴已重定基），秒级反馈。"""
    w, h, fps = plan["width"], plan["height"], plan["fps"]
    dur = float(seg["dur"])
    reg_stk = load(f"{ROOT}/registry/stickers.json")
    reg_sfx = load(f"{ROOT}/registry/sfx.json")
    inputs, fc = [], []
    ni = 0; a_idx = None; bs = []
    for tr in seg.get("tracks") or []:
        inputs += ["-ss", str(tr.get("src_in", seg["src_in"])), "-t", str(tr.get("dur", dur)), "-i", tr.get("media") or plan["media"]]
        if tr.get("role", "A") == "A" and a_idx is None: a_idx = ni
        else: bs.append((ni, tr))
        ni += 1
    fc.append(f"[{a_idx}:v]scale={w}:{h},settb=AVTB,fps={fps},format=yuv420p[bv]")
    cur = "bv"
    for k, (idx, tr) in enumerate(bs):
        sc = float(tr.get("scale") or 0.3); pw = int(w * sc)
        x, y = pos_xy(tr.get("pos", "top-right"), w, h, pw, int(pw * h / w), sub_clear=plan.get("sub_clear", 360))
        fc.append(f"[{idx}:v]scale={pw}:-2,settb=AVTB,fps={fps}[bp{k}]")
        fc.append(f"[{cur}][bp{k}]overlay={x}:{y}[bo{k}]"); cur = f"bo{k}"
    for k, stk in enumerate((seg.get("effects") or {}).get("stickers") or []):
        conf = reg_stk.get(stk.get("asset") or "") or {}
        fpath = sticker_file(conf, stk)
        if not fpath or not os.path.exists(fpath): continue
        inputs += ["-i", fpath]; sidx = ni; ni += 1
        t0 = word_time(plan["words"], stk.get("at_word"), seg)
        t0 = round(t0 - float(seg["tl_in"]), 2) if t0 is not None else 0.4
        sd = float(stk.get("duration") or conf.get("duration") or 1.2)
        pos_k = stk.get("pos") or conf.get("pos", "top-center")
        if _sticker_v2_style(conf):
            xe, ye = pos_expr(pos_k, sub_clear=plan.get("sub_clear", 360), top_y=plan.get("sticker_top"))
            fc.append(f"[{sidx}:v]scale=-1:{plan.get('sticker_h', 140)},settb=AVTB,fps={fps}[st{k}]")
            fc.append(f"[{cur}][st{k}]overlay={xe}:{ye}:enable='between(t,{t0:.2f},{t0+sd:.2f})'[bs{k}]")
        else:
            x, y = pos_xy(pos_k, w, h, plan.get("sticker_w", 500), plan.get("sticker_h", 140), sub_clear=plan.get("sub_clear", 360), top_y=plan.get("sticker_top"))
            fc.append(f"[{sidx}:v]scale={plan.get('sticker_w', 500)}:{plan.get('sticker_h', 140)},settb=AVTB,fps={fps}[st{k}]")
            fc.append(f"[{cur}][st{k}]overlay={x}:{y}:enable='between(t,{t0:.2f},{t0+sd:.2f})'[bs{k}]")
        cur = f"bs{k}"
    fc.append(f"[{cur}]{_ass_spec(ass_path)}[vout]")
    fc.append(f"[vout]scale=540:960[voutp]")  # 幕预览降质（2026-09-15 体验提速）：参考样张无需全分辨率，编码+传输双加速
    fc.append(f"[{a_idx}:a]atrim=0:{dur},asetpts=PTS-STARTPTS[vc]")
    mix = "[vc]"; n_in = 1
    for k, sfx in enumerate((seg.get("effects") or {}).get("sfx") or []):
        conf = reg_sfx.get(sfx.get("asset") or "") or {}
        fpath = os.path.join(ROOT, conf.get("file") or sfx.get("file", ""))
        if not os.path.exists(fpath): continue
        inputs += ["-i", fpath]; sidx = ni; ni += 1
        ms = int(float(sfx.get("at", 0)) * 1000); sd = float(sfx.get("duration", 0.4))
        fc.append(f"[{sidx}:a]atrim=0:{sd},adelay={ms}:all=1,volume=1.4[sx{k}]")
        mix += f"[sx{k}]"; n_in += 1
    fc.append(f"{mix}amix=inputs={n_in}:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[amix]")
    return [FF, "-y", "-hide_banner", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(fc), "-map", "[voutp]", "-map", "[amix]", "-t", f"{dur}",
        "-c:v", hw_encoder(), "-b:v", "1.5M", "-c:a", "aac", "-b:a", "96k", out_path]

def render_beat(plan, project_dir, beat_no, sid=None):
    sfx, _rs = art_suffix(project_dir, sid)
    seg = next((s for s in plan["segments"] if s.get("no") == int(beat_no)), None)
    if not seg:
        print("BEAT FAIL: no such beat"); sys.exit(1)
    pv = f"{project_dir}/previews"; os.makedirs(pv, exist_ok=True)
    tl0 = float(seg["tl_in"]); dur = float(seg["dur"])
    mini = dict(plan)
    mini["words"] = [{"t": wd["t"], "s": round(wd["s"] - tl0, 1), "e": round(wd["e"] - tl0, 1)}
                     for wd in plan["words"] if tl0 - 0.05 <= wd["s"] < tl0 + dur]
    mini["duration"] = dur
    tag = f"{sfx.lstrip('-')}_" if sfx else ""
    ass, _np = build_ass(mini, pv, f"beat_{tag}{beat_no}.ass")  # 解包元组(路径,页数)
    out = f"{pv}/beat_{tag}{beat_no}.mp4"
    out_tmp = "%s.tmp%d.mp4" % (out, os.getpid())  # 双 studio 同幕预览互踩归零（2026-09-18 并发审计）
    cmd = build_beat_cmd(plan, seg, ass, out_tmp)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    if r.returncode != 0 and cmd[cmd.index("-c:v") + 1] != "libx264":
        # 硬编码失败回退软编（跨平台降级铁律，不限定 videotoolbox——win 的 nvenc/qsv 同理）
        # 改 -b:v 的"值位"而非按码率字面量找（幕预览是 1.5M，按 4M 找=ValueError，2026-09-15 Windows CI 实锤）
        cmd[cmd.index("-c:v") + 1] = "libx264"
        bi = cmd.index("-b:v"); cmd[bi] = "-crf"; cmd[bi + 1] = "20"
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    if r.returncode != 0:
        try:
            os.remove(out_tmp)
        except OSError:
            pass
        print("BEAT FAIL:", r.stderr[-600:]); sys.exit(1)
    os.replace(out_tmp, out)
    print(f"BEAT OK: {out} ({os.path.getsize(out)//1024}KB)")

def _overlap_d(plan, prev_seg):
    """音频链相邻两幕的重叠秒数——唯一实现（R2-5）：
    dissolve 类重叠 dt；flash/直切 0（flash 插 hold 帧视频轴不缩 → 音频也不重叠）。
    trans_d 优先（2026-09-18 转场预留：dt 已随边缘静音伸缩，音频重叠必须同值）。"""
    tr = prev_seg.get("transition")
    if not tr:
        return 0.0
    if prev_seg.get("trans_d") is not None:
        return float(prev_seg["trans_d"])
    tcfg = (plan.get("tconf") or {}).get(tr) or load(f"{ROOT}/registry/transitions.json").get(tr) or {}
    return 0.0 if tcfg.get("type") == "flash" else float(tcfg.get("duration") or 0.4)


def render(plan, project_dir, sid=None):
    """整线渲染。进度协议（Studio 的渲染任务线程逐行消费）：
       STAGE <名> —— 阶段切换；PCT <0-99> —— ffmpeg out_time/总时长；RENDER OK/FAIL 收尾。
       同步落盘 render.status（2026-09-11：CLI/Agent 渲染也让界面看见——人机同权不只在数据，
       还在过程状态。文件是唯一跨进程通道，stdout 只有一条消费者能读到）。"""
    import threading, json as _json

    status_path = os.path.join(project_dir, "render.status")
    # 跨进程渲染互斥（2026-09-18 并发审计）：running 且 <300s 新鲜（与 Studio 显示窗同口径）即拒——
    # CLI/Agent/双 studio 实例同权，out/ass/status 互踩归零；崩溃残留 running 由 300s 窗口自愈。
    try:
        import time
        if json.load(open(status_path, encoding="utf-8")).get("running") \
                and time.time() - os.path.getmtime(status_path) < 300:
            print("RENDER FAIL: 已有渲染进行中（render.status running 且 <300s 新鲜）——跨进程互斥", flush=True)
            sys.exit(1)
    except (OSError, ValueError):
        pass
    def _status(**kv):
        try:
            st = {"running": True, "stage": "plan", "pct": 0, "ok": None, "tail": [], "source": "cli"}
            st.update(kv)
            _json.dump(st, open(status_path, "w", encoding="utf-8"))
        except Exception:
            pass

    _status(stage="plan", pct=2)
    sfx, real_sid = art_suffix(project_dir, sid)  # P2#7：原引用未定义的 real_sid，直接 import render() 会 NameError
    # 封面闸门（2026-09-18 Noah 裁定：封面生成+检查在渲染前——人先验封面再耗渲染；
    # output-frame 例外：成片抽帧天然后置，渲染后收口）
    _cov_strategy = ((plan.get("meta") or {}).get("cover") or {}).get("strategy", "first-frame")
    print("STAGE cover", flush=True)
    _status(stage="cover", pct=1)
    cover = None
    if _cov_strategy != "output-frame":
        try:
            cover = build_cover(plan, project_dir, sfx)
            _ok, _why = cover_check(cover, (plan.get("width"), plan.get("height")))
        except Exception as _e:
            _ok, _why = False, str(_e)[:200]
        if not _ok:
            _status(running=False, ok=False, tail=["封面不过: " + _why])
            print("RENDER FAIL: 封面闸门不过——" + _why, flush=True)
            sys.exit(1)
    ass, npages = build_ass(plan, project_dir, f"subtitle{sfx}.ass")
    out = os.path.join(project_dir, f"out{sfx}.mp4")
    out_tmp = "%s.tmp%d.mp4" % (out, os.getpid())  # 原子换名：读者见到的 out 永远是完整成片
    cmd = build_cmd(plan, ass, out_tmp) + ["-progress", "pipe:1", "-nostats"]
    print("STAGE ffmpeg", flush=True)
    _status(stage="ffmpeg", pct=0)
    r = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=ROOT)  # cwd=ROOT：滤镜串内嵌相对路径（_ass_spec）的解析锚点
    errbuf = []
    threading.Thread(target=lambda: [errbuf.append(l) for l in r.stderr], daemon=True).start()
    total = float(plan.get("duration") or 0)
    for line in r.stdout:
        line = line.strip()
        if line.startswith("out_time="):
            try:
                hh, mm, ss = line.split("=", 1)[1].split(":")
                sec = int(hh) * 3600 + int(mm) * 60 + float(ss)
                if total > 0:
                    pct = min(99, max(0, int(sec / total * 100)))  # 钳位：ffmpeg 起步瞬间的未知时间(-577014:..)不许漏进来
                    print(f"PCT {pct}", flush=True)
                    if pct % 5 == 0:  # 每 5% 落一次盘（足够实时，不刷盘过频）
                        _status(stage="ffmpeg", pct=pct)
            except Exception:
                pass
        elif line.startswith("progress=end"):
            print("PCT 99", flush=True)
            _status(stage="ffmpeg", pct=99)
    r.wait()
    if r.returncode != 0 and cmd[cmd.index("-c:v") + 1] != "libx264":
        # 硬编码不可用则回退软编（跨平台降级铁律）
        # 改 -b:v 的值位而非按码率字面量找（与 render_beat 同款——改码率即炸的雷，2026-09-15 实锤）
        cmd[cmd.index("-c:v") + 1] = "libx264"
        bi = cmd.index("-b:v"); cmd[bi] = "-crf"; cmd[bi + 1] = "20"
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT)  # 软编回退同锚点
        errbuf = list(r.stderr or "")
    if r.returncode != 0:
        try:
            os.remove(out_tmp)
        except OSError:
            pass
        _status(running=False, ok=False, tail=["".join(errbuf)[-400:]])
        print("RENDER FAIL:", "".join(errbuf)[-800:], flush=True)
        sys.exit(1)
    os.replace(out_tmp, out)
    if _cov_strategy == "output-frame":
        cover = build_cover(plan, project_dir, sfx)  # 成片抽帧特例：渲染后才存在 out{sfx}.mp4
    # QC 审片层（delivery gate）：渲染后自动体检，结论并入渲染尾行与 status.tail——QC 自身异常不阻塞渲染结果
    try:
        import qc
        qc_r = qc.run(os.path.basename(project_dir), story=real_sid)
        qc_line = "QC:" + qc_r["one_line"]
        _status(tail=[qc_line])
        print(qc_line, flush=True)
    except Exception as _qce:
        print("QC SKIP: %s" % str(_qce)[:80], flush=True)
    _status(running=False, ok=True, stage="done", pct=100)
    print(f"RENDER OK: {out} ({os.path.getsize(out)//1024}KB)  字幕页:{npages}  时长:{plan['duration']}s  封面:{os.path.basename(cover) if cover else '缺'}", flush=True)

# ---------------------------------------------------------------- 入口
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1]
    arg2 = sys.argv[2]
    # 项目参数：绝对/相对目录路径，或裸项目名（与其他生成器同语义，README 示例即此）
    project = os.path.abspath(arg2 if os.path.isdir(arg2) else os.path.join(ROOT, "projects", arg2))
    if not os.path.isdir(project):
        raise SystemExit(f"无项目目录: {arg2}")
    sid = None
    batch = None
    resume = False
    if cmd == "beat":
        a3 = sys.argv[3]
        if a3 == "--batch":
            batch = sys.argv[4]; resume = True
            sid = sys.argv[5] if len(sys.argv) > 5 else None
        elif a3.startswith("--batch="):
            batch = a3.split("=", 1)[1]; resume = True
            sid = sys.argv[4] if len(sys.argv) > 4 else None
        else:
            batch = a3  # 单幕旧形态 "beat <proj> 3"：明确重渲，不跳已有预览（迭代单幕场景）
            sid = sys.argv[4] if len(sys.argv) > 4 else None
    else:
        sid = sys.argv[3] if len(sys.argv) > 3 else None
    plan, real_sid = build_plan(project, sid)
    sfx = f"-{real_sid}" if real_sid else ""
    print(f"PLAN: {project}/plan{sfx}.json [{real_sid or 'main'}]  时长 {plan['duration']}s  词 {len(plan['words'])}  幕 {len(plan['segments'])}")
    if cmd == "make":
        ass, npages = build_ass(plan, project, f"subtitle{sfx}.ass")
        print(f"ASS: {ass}  字幕页 {npages}")
    elif cmd == "cover":
        # 封面单独出（渲染前闸门的独立入口——人/Agent 可迭代封面而不耗渲染）
        cover = build_cover(plan, project, sfx)
        ok, why = cover_check(cover, (plan.get("width"), plan.get("height"))) if cover else (False, "封面缺失")
        print(("COVER OK: %s（%s）" % (os.path.basename(cover), why)) if ok else "COVER FAIL: " + why)
        sys.exit(0 if ok else 1)
    elif cmd == "render":
        render(plan, project, real_sid)
    elif cmd == "beat":
        # 断点续渲（2026-09-18 agent 建议 P4）：--batch 1-6 顺序逐幕渲，已完成幕（预览在场
        # 且 >0 字节）跳过——幕预览重渲迭代时不必从头来。单幕旧形态 "beat <proj> 3" 不变。
        rng = str(batch)
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", rng)
        if not m:
            raise SystemExit("幕号形态应如 3 或 1-6，收到: %r" % rng)
        nos = [int(m.group(1))] if not m.group(2) else list(range(int(m.group(1)), int(m.group(2)) + 1))
        _sfx, _rs = art_suffix(project, real_sid)  # 与 render_beat 同参——预览文件名必须同口径
        _tag = f"{_sfx.lstrip('-')}_" if _sfx else ""
        for _n in nos:
            if not any(s.get("no") == _n for s in plan["segments"]):
                print("BEAT SKIP: 幕%s 不在故事线" % _n)
                continue
            _out = f"{project}/previews/beat_{_tag}{_n}.mp4"
            if resume and os.path.exists(_out) and os.path.getsize(_out) > 0:
                print(f"BEAT SKIP: 幕{_n} 预览已在（断点续渲跳过） {_out}")
                continue
            render_beat(plan, project, _n, real_sid)
        if len(nos) > 1:
            print("BATCH DONE: 幕 %d-%d" % (nos[0], nos[-1]))
