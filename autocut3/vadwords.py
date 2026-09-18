"""词轨生成（时间源演进：2026-09-15 VAD 物理测量 → 2026-09-18 实测词级时间戳直通道）。

背景勘误（2026-09-18 Noah 指路 + 探针实锤）：此前判定"ASR 词级时间戳漂移 0~3s"的真相
是 asr.py 从未向 MiniMax 传 timestamp_level——拿到句级响应后字符时间是句内均分合成的，
"漂移"是我们自己合成的锅，不是 ASR 测量的锅。实测（ruxuan-02）：timestamp_level=word
返回逐字实测时间，31 词起点 30/31 落 VAD 语音段 ±0.05s（唯一段外偏 0.08s，小于
silencedetect 0.25s 粒度）——ASR 实测与 VAD 物理测量互证。

时间源优先级（original 幕）：
  1. transcript.tier=word（MiniMax timestamp_level=word 实测）→ 窗口切片+相对化直取
  2. narration.window_local=true（手选通道）→ 窗口局部 VAD 铺 story 文本
     （场记语音混入/未转写场景；story 须为窗口内实拍台词原文）
  3. tier=sent/vad → 全素材 VAD 语音段 + 全文本比例铺轨 + 窗口切片（历史路径）
  4. 窗口无词（空镜/未转写）→ story 字符按窗口 VAD 铺
dub 幕：VAD 对 dub 音频全长铺 story 台词（dubfit 随后以 ASR 闭环词覆写）。
产物：storylines/<sid>.json 每幕 narration.words + <pid>.vad-report.json
（report.text_from 标注每幕走了哪条通道：word-ts/window-local/transcript/story）。
"""
import argparse, json, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # 平台解析链 ffmpeg_path/ffprobe_path（硬规则 8：禁裸 bin/ffmpeg 相对路径）
import flock  # write_json 原子落盘
FF = asr.ffmpeg_path()


def vad_spans(src, ss=0.0, t=None, noise="-32dB", min_speech=0.30):
    """窗口内语音段（相对秒）。silencedetect 取补集；段短于 min_speech 丢弃（呼吸级噪声）。"""
    cmd = [FF, "-hide_banner"]
    if ss: cmd += ["-ss", str(ss)]
    if t: cmd += ["-t", str(t)]
    cmd += ["-i", src, "-vn", "-af", "silencedetect=n=%s:d=0.25" % noise, "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ev = []
    for line in (r.stderr or "").splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m: ev.append(["s", float(m.group(1))]); continue
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m: ev.append(["e", float(m.group(1))])
    # 事件流 → 静音区间
    sil, cur = [], None
    for kind, v in ev:
        if kind == "s" and cur is None: cur = v
        elif kind == "e" and cur is not None: sil.append((cur, v)); cur = None
    if cur is not None: sil.append((cur, 10**9))
    # 语音段 = 补集
    dur = t
    if dur is None:
        q = subprocess.run([asr.ffprobe_path(), "-v", "error",
                            "-show_entries", "format=duration", "-of", "csv=p=0", src],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        dur = float(q.stdout.strip())
    spans, pos = [], 0.0
    for s, e in sil:
        if s - pos >= min_speech: spans.append((pos, min(s, dur)))
        pos = max(pos, e)
    if dur - pos >= min_speech: spans.append((pos, dur))
    return spans, dur


def allocate(chars, spans, cross_out=None):
    """字符按序线性铺进语音段（比例分配），返回 [(ch, s, e)]。
    跨段字符按段尾截断（2026-09-18 agent 实锤 b4「流」0.42s 静音全程高亮：卡拉OK fill
    时长=e-s，字符跨段界时高亮窗盖住段间静音+下段头部）——截到起始段尾；被截字符的
    索引经 cross_out（可选 set/list）登记，供调用方映射回窗口做 vad-report 台账
    （事后按"e==段尾"猜会误报段尾自然对齐的字符，只能在截断现场记）。"""
    n = len(chars)
    total = sum(e - s for s, e in spans)
    def at(p):  # 语音进度 p∈[0,total] → 时间轴
        acc = 0.0
        for s, e in spans:
            d = e - s
            if acc + d >= p: return s + (p - acc)
            acc += d
        return spans[-1][1]
    out = []
    for i, (c, _a, _b) in enumerate(chars):
        s = at(i * total / n)
        e = at((i + 1) * total / n)
        sp_end = next((e0 for s0, e0 in spans if s0 - 1e-9 <= s <= e0 + 1e-9), None)
        if sp_end is not None and e > sp_end:  # 跨段界：截到起始段尾
            e = sp_end
            if cross_out is not None:
                cross_out.append(i)
        out.append((c, s, e))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project"); ap.add_argument("--story", default=None)
    a = ap.parse_args()
    pdir = os.path.join("projects", a.project)
    sfile = os.path.join(pdir, "storylines", "%s.json" % a.story) if a.story else os.path.join(pdir, "storyline.json")
    sl = json.load(open(sfile, encoding="utf-8"))
    report = []
    for b in sl.get("beats", []):
        nar = b.get("narration") or {}
        if nar.get("mode") == "none": continue
        story = re.sub(r"\s", "", b.get("story") or "")
        if not story: continue
        text_from = "story"  # dub 幕：story=口播台词（draft --dub 用它 TTS，天然同文本）
        if nar.get("mode") == "dub":
            src = nar["audio"]
            src = src if os.path.isabs(src) else os.path.join(pdir, src)
            spans, dur = vad_spans(src)
            # 2026-09-18 补齐（既有隐患：此分支原不给 words 赋值——首幕 dub 直接 NameError，
            # 非首幕静默沿用上一幕陈词；cross 上台账后两况都必炸，按 D 类纪律修根）：
            # dub 台词=story（draft --dub 用它 TTS，天然同文本），照常铺 VAD 段。
            # 注：dubfit 在 vadwords 之后跑会用 ASR 闭环词覆写得更准（环节顺序 vadwords→dubfit）。
            _ci = []
            words = allocate([(ch, 0, 0) for ch in story], spans, cross_out=_ci)
            cross = [story[i] for i in _ci]
            audio_desc = os.path.basename(src)
        else:
            A = next((x for x in (b.get("tracks") or [])), None)
            if not A: continue
            src = None
            ftrans = {}
            lib = json.load(open(os.path.join(pdir, "materials", "library.json"), encoding="utf-8"))
            for pk in lib.get("packs", []):
                pj = json.load(open(os.path.join("materials", "packs", pk, "pack.json"), encoding="utf-8"))
                for f in pj.get("files", []):
                    if f["id"] == A.get("source_id"):
                        src = os.path.join("materials", "packs", pk, f["file"])
                        ftrans = f.get("transcript") or {}
            if not src:
                print("幕%s: 素材未找到，跳过" % b.get("no"), flush=True); continue
            ss, t = float(A.get("src_in") or 0), float(A.get("duration") or 0)
            audio_desc = "%s[%s+%s]" % (os.path.basename(src), ss, t)
            # 字幕文本=素材实拍台词（proofread 校对后 ASR），story 摘要不进字幕——
            # VAD 只管时间（物理测量），文本管"人物实际说了什么"（2026-09-14 维护者 实锤：
            # story 是"这一幕讲什么"的分镜摘要，被铺进字幕=总结腔字幕）。
            # 窗口无词（空镜/未转写）→ 回退 story 字符，不留空白字幕。
            # 历史注（2026-09-16 agent-037 实锤 M0269/M0222 词时间偏 0.5-3s）：当时词时间
            # 是句内均分合成值（asr.py 未传 timestamp_level），按它过滤窗口吃掉句首句尾字
            # =字幕漏字/半句。2026-09-18 勘误：tier=word 实测时间无此问题（上方直通道）；
            # 本段以下路径仅服务 tier=sent/vad 老包与 window_local 手选——合成时间仍禁作
            # 窗口过滤锚，全文本铺全素材 VAD 段再按窗口切片的铁律在这些路径继续成立。
            text_all = "".join(w.get("text", "") for w in (ftrans.get("words") or [])).strip()
            win = []
            spans = []
            cross = []
            # 2026-09-18 实测词级时间戳直通道（Noah 指路 + 探针实锤）：tier=word 的词时间
            # 是 MiniMax timestamp_level=word 实测值（ruxuan-02 实测 31 词起点 30/31 落
            # VAD 语音段 ±0.05s，唯一段外偏 0.08s < silencedetect 0.25s 粒度——ASR 实测
            # 与 VAD 物理测量互证）——窗口切片+相对化即词轨，不进均分机器。M0038 类伪峰
            # 错位（silencedetect 把 -35dB 段均瞬态伪峰判成语音段）在此路不存在：ASR 语义
            # 判定不认伪峰。window_local 手选优先（zhanglingxiang-02 通道，见下）。
            word_ts = False
            if str(ftrans.get("tier") or "") == "word" and (ftrans.get("words") or []) \
                    and not nar.get("window_local"):
                _w = [(str(x.get("text") or ""), float(x.get("start", 0)), float(x.get("end", 0)))
                      for x in ftrans["words"]
                      if float(x.get("end", 0)) > ss and float(x.get("start", 0)) < ss + t]
                if _w:
                    words = [(ch, round(max(s, ss) - ss, 2), round(min(e, ss + t) - ss, 2))
                             for ch, s, e in _w]
                    _sp = []
                    for _ch, _s, _e in _w:  # 报告语音段=实测词时间合并（gap≤0.3s 并段）
                        if _sp and _s - _sp[-1][1] <= 0.3:
                            _sp[-1] = (_sp[-1][0], _e)
                        else:
                            _sp.append((_s, _e))
                    spans = [(round(s - ss, 2), round(e - ss, 2)) for s, e in _sp]
                    story = "".join(ch for ch, _s, _e in _w).strip()
                    text_from = "word-ts"
                    word_ts = True
            if not word_ts:
                # 2026-09-18 窗口局部 VAD 通道（zhanglingxiang-02 实锤，剪辑 agent 提案、
                # 裁定 A 保留）：素材原生音轨带高频瞬态伪峰（峰值 -1.4~-2.1dB / 段均
                # -35dB）或未转写场记语音时，全素材比例铺轨会把文本整体前移（M0038 前移
                # 3-5s → "字幕在走、语音没有"）。narration.window_local=true 时改走下方
                # else 分支：窗口局部 VAD 铺 story 文本（story 须为窗口内实拍台词原文）。
                # 默认关闭 → 既有项目行为零改动。重转写后 tier=word 通常不再需要此通道。
                if text_all and not nar.get("window_local"):
                    spans_all, _dur_all = vad_spans(src)  # 全素材绝对时间轴
                    cross_all = []
                    chars_all = allocate([(ch, 0, 0) for ch in text_all], spans_all, cross_out=cross_all)
                    win_i = [(i, ch, s, e) for i, (ch, s, e) in enumerate(chars_all) if e > ss and s < ss + t]
                    win = [(ch, s, e) for _i, ch, s, e in win_i]
                    cross = [ch for i, ch, _s, _e in win_i if i in cross_all]  # 窗口内实际被截断的
                wtext = "".join(ch for ch, _s, _e in win).strip()
                if wtext:
                    story = wtext
                    text_from = "transcript"
                    words = [(ch, round(s - ss, 2), round(e - ss, 2)) for ch, s, e in win]
                    spans = [(max(s, ss) - ss, min(e, ss + t) - ss) for s, e in spans_all if e > ss and s < ss + t]
                else:
                    # 窗口无词（空镜/未转写）→ 回退 story 字符按窗口 VAD 铺
                    spans, dur = vad_spans(src, ss=ss, t=t)
                    chars = [(ch, 0, 0) for ch in story]
                    _ci = []
                    words = allocate(chars, spans, cross_out=_ci)
                    cross = [story[i] for i in _ci]
                    text_from = "story"
        speech = sum(e - s for s, e in spans)  # 共位（dub/original 两路都算——原仅在 else 内，dub 路未绑定）
        nar["words"] = [{"t": ch, "s": round(s, 2), "e": round(e, 2)} for ch, s, e in words]
        report.append({"no": b.get("no"), "audio": audio_desc, "spans": [[round(s,2), round(e,2)] for s, e in spans],
                       "speech": round(speech, 2), "chars": len(story), "density": round(len(story)/speech, 1) if speech else 0,
                       "text_from": text_from, "cross_span": cross})
        print("幕%s %s 语音段=%s 字密=%.1f字/s" % (b.get("no"), audio_desc, report[-1]["spans"], report[-1]["density"]), flush=True)
    flock.write_json(sfile, sl)  # 原子落盘（与 draft 双跑同险：读者永不读半截故事线）
    flock.write_json(os.path.join(pdir, "vad-report.json"), report)
    print("VAD 词轨已写入 %s ✓" % sfile, flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
