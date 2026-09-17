"""ASR 适配层：供应商可换（config/services.json），产物统一归一化词轨。

归一化契约（下游唯一依赖，换供应商零感知）：
  words: [{"text": str, "start": float, "end": float}]   # 源素材时间轴，0.1s 量化

时间戳三级降级（供应商给什么都能用）：
  tier=word  后端直接给字/词级（本地 faster-whisper）
  tier=sent  后端给句级（segments>=2）→ 句内按字数比例分配
  tier=vad   只有全文 → ffmpeg 静音检测出语音段 + 文本按段时长比例插值

跨平台：ffmpeg 解析顺序 = $FFMPEG env → 仓内 bin/（mac）→ PATH；纯标准库。
"""
import json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def retry429(fn, tries=3, base_s=2.0):
    """HTTP 429 限流退避（2026-09-18 素材级并发配套）：线性退避重试，其余异常直接抛。
    MiniMax 不公布限额数字（按账号 tier 浮动）——2026-09-18 实测当前账号 3并发x双端点
    无 429；退避兜底防更高负载/账号降档时被拦死。fn 需幂等（纯网络调用，无本地副作用）。"""
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(base_s * (i + 1))
                continue
            raise


def load_services():
    p = os.path.join(ROOT, "config", "services.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def resolve_key(becfg):
    """key 解析：env 优先，其次 key 文件（不回显、不入日志）。"""
    k = os.environ.get(becfg.get("api_key_env", ""), "")
    if k:
        return k
    f = becfg.get("api_key_file")
    if f:
        f = os.path.expanduser(f)
        if os.path.exists(f):
            try:
                return (json.load(open(f, encoding="utf-8")) or {}).get("api_key", "")
            except Exception:
                return ""
    return ""


def ffmpeg_path():
    for c in (os.environ.get("FFMPEG"), os.path.join(ROOT, "bin", "ffmpeg"),
              os.path.join(ROOT, "bin", "ffmpeg.exe")):
        if c and os.path.exists(c):
            return c
    return shutil.which("ffmpeg")


def ffprobe_path():
    for c in (os.environ.get("FFPROBE"), os.path.join(ROOT, "bin", "ffprobe"),
              os.path.join(ROOT, "bin", "ffprobe.exe")):
        if c and os.path.exists(c):
            return c
    return shutil.which("ffprobe")


def media_duration(path):
    fp = ffprobe_path()
    if not fp:
        return 0.0
    r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def extract_audio(src, wav, ff=None):
    ff = ff or ffmpeg_path()
    os.makedirs(os.path.dirname(wav), exist_ok=True)
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
    return wav


def vad_speech_spans(wav, ff=None, noise="-35dB", min_silence=0.25, min_span=0.15):
    """ffmpeg silencedetect → 语音段（静音的补集）。"""
    ff = ff or ffmpeg_path()
    r = subprocess.run([ff, "-i", wav, "-af", f"silencedetect=noise={noise}:d={min_silence}",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    sil, cur = [], None
    for line in (r.stderr or "").splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            cur = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1))))
            cur = None
    dur = media_duration(wav, ) if os.environ.get("FFPROBE") else _wav_dur_ffmpeg(wav, ff)
    spans, pos = [], 0.0
    for s, e in sil:
        if s - pos >= min_span:
            spans.append((pos, s))
        pos = e
    if dur - pos >= min_span:
        spans.append((pos, dur))
    return spans


def _wav_dur_ffmpeg(wav, ff):
    r = subprocess.run([ff, "-i", wav], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.findall(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr or "")
    if m:
        h, mi, s = m[0]
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def distribute(chars, spans):
    """把字符按 spans 时长比例铺进语音段 → [(ch, s, e)]。"""
    n_total, t_total = len(chars), sum(e - s for s, e in spans)
    if n_total == 0 or t_total <= 0:
        return []
    out, ci = [], 0
    for si, (s, e) in enumerate(spans):
        if ci >= n_total:
            break
        left = len(spans) - si
        n = (n_total - ci) if left == 1 else max(1, round(n_total * (e - s) / t_total))
        n = min(n, n_total - ci)
        step = (e - s) / max(n, 1)
        for j in range(n):
            out.append((chars[ci], s + j * step, s + (j + 1) * step))
            ci += 1
    return out


def transcribe_minimax(wav, becfg):
    key = resolve_key(becfg)
    if not key:
        raise RuntimeError("未找到 API key：设 env %s，或检查 api_key_file" % becfg.get("api_key_env", "?"))
    audio_path = os.path.abspath(wav)
    b = uuid.uuid4().hex
    with open(audio_path, "rb") as fh:
        audio = fh.read()
    parts = [
        ('--%s\r\nContent-Disposition: form-data; name="model"\r\n\r\n%s\r\n' % (b, becfg.get("model", "asr-1.0"))).encode(),
        ('--%s\r\nContent-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n' % b).encode(),
        ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: audio/wav\r\n\r\n' % (b, os.path.basename(audio_path))).encode() + audio + ("\r\n--%s--\r\n" % b).encode(),
    ]
    req = urllib.request.Request(
        becfg["base_url"].rstrip("/") + "/speech_to_text", data=b"".join(parts),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b, "Authorization": "Bearer " + key})
    return json.loads(urllib.request.urlopen(req, timeout=becfg.get("timeout_s", 90)).read())


def transcribe_local(wav, becfg):
    """本地后端（字级，tier=word）。本版本不内置模型——保留下载通路。"""
    try:
        from faster_whisper import WhisperModel  # 下载通路：pip install faster-whisper
    except ImportError:
        print("本地后端依赖未安装（本版本不内置模型，保留下载通路）：")
        print("  pip install faster-whisper")
        print("  模型首次运行自动下载（%s ≈ 460MB）" % becfg.get("model", "small"))
        sys.exit(1)
    model = WhisperModel(becfg.get("model", "small"), device=becfg.get("device", "cpu"),
                         compute_type=becfg.get("compute_type", "int8"))
    segs, info = model.transcribe(wav, word_timestamps=True, language="zh")
    words = []
    for s in segs:
        for w in (s.words or []):
            t = (w.word or "").strip()
            if t:
                words.append({"text": t, "start": float(w.start), "end": float(w.end)})
    return {"text": "".join(w["text"] for w in words), "segments": [], "words": words,
            "duration": float(getattr(info, "duration", 0.0))}


def transcribe(src, provider=None, cache_dir=None):
    """主入口：素材文件 → 归一化词轨（契约见文件头）。"""
    svc = load_services()
    acfg = svc.get("asr", {})
    provider = provider or acfg.get("provider", "minimax")
    becfg = acfg.get("backends", {}).get(provider, {})
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("找不到 ffmpeg：装入 PATH、设 FFMPEG 环境变量，或用仓内 bin/（mac）")
    cache_dir = cache_dir or os.path.join(ROOT, "materials", ".audio_cache")
    os.makedirs(cache_dir, exist_ok=True)
    import hashlib
    _dh = hashlib.md5(os.path.dirname(os.path.abspath(src)).encode("utf-8")).hexdigest()[:8]
    wav = os.path.join(cache_dir, _dh + "_" + os.path.basename(src).rsplit(".", 1)[0] + ".wav")  # P2#14：同名素材跨目录防缓存串写
    extract_audio(src, wav, ff)
    dur = media_duration(src)
    max_s = float(becfg.get("max_audio_s", 480))
    if dur > max_s:
        raise RuntimeError("素材 %.1fs 超过后端上限 %.0fs（先切片再转）" % (dur, max_s))

    if provider == "local-faster-whisper":
        r = transcribe_local(wav, becfg)
        text, words, tier = r["text"], r["words"], "word"
    else:
        resp = transcribe_minimax(wav, becfg)
        text = (resp.get("text") or "").strip()
        segments = resp.get("segments") or []
        words, tier = None, ("sent" if len(segments) >= 2 else "vad")
        chars = [c for c in text if not c.isspace()]
        if not chars:
            words = []
        elif tier == "sent":
            spans = [(float(s.get("start", 0)), float(s.get("end", 0))) for s in segments
                     if float(s.get("end", 0)) > float(s.get("start", 0))]
            words = [quant(p) for p in distribute(chars, spans)]
        else:
            spans = vad_speech_spans(wav, ff)
            if not spans and chars:
                spans = [(0.0, min(dur or len(chars) * 0.28, len(chars) * 0.28))]
            words = [quant(p) for p in distribute(chars, spans)] if spans else []

    words = [w for w in (words or []) if w.get("text")]
    words.sort(key=lambda w: w["start"])
    return {"provider": provider, "tier": tier, "text": text,
            "duration": round(dur, 1), "words": words,
            "at": datetime_iso()}


def quant(p):
    return {"text": p[0], "start": round(p[1], 1), "end": round(p[2], 1)}


def speech_tail(words, upto):
    """词轨有效语音末尾（含标点词——ASR 标点时间戳也是它给的）。

    单一事实源（D1）：draft.validate 与 qc 的 R1/R2 必须共用本函数，
    否则同一出点两个模块两种判定（qc 实词尾 vs draft 含标点尾的漂移，2026-09-12 QC 首跑实物命中）。
    upto=窗口右沿（src_in+duration），0.05s 容差吸收浮点噪声。返回 0.0 表示窗口内无词。
    """
    last = 0.0
    for w in words or []:
        try:
            w_end = float(w.get("end") or (float(w.get("start") or 0)) + 0.2)
        except (TypeError, ValueError):
            continue
        if w_end <= upto + 0.05:
            last = max(last, w_end)
    return last


def datetime_iso():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
