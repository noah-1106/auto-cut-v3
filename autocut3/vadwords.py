"""VAD 词轨生成（2026-09-15 管线缺口修复：字幕时间源从"ASR 推测"切到"物理测量"）。

背景实锤：ASR 词级时间戳是对齐模型的输出（推测值），漂移 0~3s 不等且同一音频内不均匀
（静音段/弱语音段/说话人变化处漂移更大）。它被全链当事实消费——裁剪锚、字幕词轨、卡拉OK
时序——是"能力都在却对不上"的根因。静音检测（VAD）是物理测量：语音段起止即真值。

本脚本对每幕音频跑 silencedetect 得真实语音段，story 文本按字符比例铺进去：
  - original 幕：VAD 对素材窗口 [src_in, src_in+dur]
  - dub 幕：VAD 对 dub 音频全长
产物：storylines/<sid>.json 每幕 narration.words（VAD 词轨）+ <pid>.vad-report.json
页面级精度（±0.2s），页内 karaoke 均分——"字幕在人说话时出现"这个本质需求由物理测量保证。
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


def allocate(chars, spans):
    """字符按序线性铺进语音段（比例分配），返回 [(ch, s, e)]。"""
    n = len(chars)
    total = sum(e - s for s, e in spans)
    def at(p):  # 语音进度 p∈[0,total] → 时间轴
        acc = 0.0
        for s, e in spans:
            d = e - s
            if acc + d >= p: return s + (p - acc)
            acc += d
        return spans[-1][1]
    return [(c, at(i * total / n), at((i + 1) * total / n)) for i, (c, _a, _b) in enumerate(chars)]


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
                print("幕%s: 素材未找到，跳过" % b.get("no")); continue
            ss, t = float(A.get("src_in") or 0), float(A.get("duration") or 0)
            audio_desc = "%s[%s+%s]" % (os.path.basename(src), ss, t)
            # 字幕文本=素材实拍台词（proofread 校对后 ASR），story 摘要不进字幕——
            # VAD 只管时间（物理测量），文本管"人物实际说了什么"（2026-09-14 维护者 实锤：
            # story 是"这一幕讲什么"的分镜摘要，被铺进字幕=总结腔字幕）。
            # 窗口无词（空镜/未转写）→ 回退 story 字符，不留空白字幕。
            # 选词铁律（2026-09-16 agent-037 实锤 M0269「我」标 7.2 实际 11.4 / M0222 偏早 ~3s）：
            # ASR 词时间漂移 0.5-3s，按它过滤窗口会吃掉句首句尾字（字幕漏字/半句根因）。
            # 弃用 ASR 时间——全文本按字符序铺进"全素材 VAD 语音段"（物理测量唯一时间源），
            # 再按窗口切片、时间轴相对化到窗口起点。
            text_all = "".join(w.get("text", "") for w in (ftrans.get("words") or [])).strip()
            win = []
            spans = []
            if text_all:
                spans_all, _dur_all = vad_spans(src)  # 全素材绝对时间轴
                chars_all = allocate([(ch, 0, 0) for ch in text_all], spans_all)
                win = [(ch, s, e) for ch, s, e in chars_all if e > ss and s < ss + t]
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
                words = allocate(chars, spans)
                text_from = "story"
            speech = sum(e - s for s, e in spans)
        nar["words"] = [{"t": ch, "s": round(s, 2), "e": round(e, 2)} for ch, s, e in words]
        report.append({"no": b.get("no"), "audio": audio_desc, "spans": [[round(s,2), round(e,2)] for s, e in spans],
                       "speech": round(speech, 2), "chars": len(story), "density": round(len(story)/speech, 1) if speech else 0,
                       "text_from": text_from})
        print("幕%s %s 语音段=%s 字密=%.1f字/s" % (b.get("no"), audio_desc, report[-1]["spans"], report[-1]["density"]))
    flock.write_json(sfile, sl)  # 原子落盘（与 draft 双跑同险：读者永不读半截故事线）
    flock.write_json(os.path.join(pdir, "vad-report.json"), report)
    print("VAD 词轨已写入 %s ✓" % sfile)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
