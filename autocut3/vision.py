"""视觉理解适配层 v2：供应商可换（config/services.json vision），产物统一归一化。

归一化契约 v2（下游唯一依赖；desc/ocr/usage 三键与 v1 兼容，draft.py 零改动）：
  visual = {
    "desc": str,          # 一句话：拍了什么、谁在做什么（=summary）
    "ocr": [str],         # 兼容形态："文字（位置）@秒"
    "usage": str,         # 多条以；连接
    "content_type": str,  # narration|meta|dialogue|ambient|broll
    "moments": [{"t":[a,b],"note":str}],
    "defects": [{"t":[a,b],"type":str,"note":str}],
    "ocr_detail": [{"t":..,"text":..,"where":..}],
    "frames": str|int,    # "video-native@720p" 或降级路径帧数
    "provider": str, "at": iso, "schema": 2|1, "flags": [...]
  }

v2 变更（2026-09-12，A/B 实测背书 docs/ab-vision-20260912/）：
  视频：3帧采样 → 原生视频理解（video_url；720p 转码前置 crf23 防细节色偏，≤100MB）
  提示词：缺陷清单式 → 结构化契约（summary/content_type/moments/ocr坐标/defects/usage[]）
  词轨先验：说话区间+要点注入（A/B 实测：延迟 -39%，content_type 判对 3/3，OCR 召回↑）
  铁律入提示词：只描述确实看到的/不确定写不确定/时间不越界
  降级：v2 失败 → 自动回退 3帧旧路径（schema=1，frames=帧数，如实标注）
"""
import base64, json, os, re, shutil, subprocess, sys, tempfile, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # noqa: E402  复用：load_services/resolve_key/media_duration/ffmpeg_path/datetime_iso

MAX_VIDEO_BYTES = 100 * 1024 * 1024


def _ff():
    ff = asr.ffmpeg_path()
    if not ff:
        raise RuntimeError("找不到 ffmpeg：装入 PATH、设 FFMPEG 环境变量，或用仓内 bin/（mac）")
    return ff


def downscale_image(src, out):
    """压图：长边 ≤1280；仍 >2MB 再压到 880（MiniMax 建议 ≤2MB/图）。"""
    ff = _ff()
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", src,
                    "-vf", "scale='min(1280,iw)':-2", "-q:v", "5", out], check=True)
    if os.path.exists(out) and os.path.getsize(out) > 2 * 1024 * 1024:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", src,
                        "-vf", "scale='min(880,iw)':-2", "-q:v", "7", out], check=True)
    return out


def _datauri(path):
    return "data:image/jpeg;base64," + base64.b64encode(open(path, "rb").read()).decode()


def _transcode_720(src, td):
    """原生视频前置：720p/crf23/-an/faststart（A/B：crf26 致深蓝误判黑，23 起步）。"""
    out = os.path.join(td, "v720.mp4")
    subprocess.run([_ff(), "-y", "-loglevel", "error", "-i", src,
                    "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-an", "-movflags", "+faststart", out], check=True)
    if os.path.getsize(out) > MAX_VIDEO_BYTES:
        raise RuntimeError("720p 转码后仍超 100MB（异常素材，需人工核查）")
    return out


def _speech_prior(words, dur):
    """词轨 → 说话区间+要点先验（A/B：先验使延迟 -39%、content_type 判对率 100%）。"""
    real = [w for w in (words or []) if w.get("text") not in "，。！？、"]
    if not real:
        return "这段素材全程无有效语音（纯画面）。"
    segs, cur = [], None
    for w in real:
        s, e = float(w.get("start", 0)), float(w.get("end", 0))
        if cur and s - cur[1] <= 0.6:
            cur[1] = max(cur[1], e)
        else:
            if cur:
                segs.append(cur)
            cur = [s, e]
    if cur:
        segs.append(cur)
    segs = [[max(0.0, a), min(dur, b) if dur else b] for a, b in segs]
    parts = ["%.1f-%.1fs 有人讲话" % (a, b) for a, b in segs]
    gist = "".join(w.get("text", "") for w in real)[:40]
    return "；".join(parts) + "（要点：%s）。" % gist


def _prompt_v2(prior, dur):
    return ("这是一段竖版短视频素材，时长 %.1f 秒。已知信息（来自音频转写，可信）：\n%s\n"
            "请观看整段视频，只输出合法 JSON（不要 markdown 代码块、不要任何多余文字）：\n"
            '{"summary":"一句话：这条视频拍了什么、谁在做什么",\n'
            '"content_type":"narration|meta|dialogue|ambient|broll 五选一。narration=对镜头讲内容；'
            'meta=拍摄指挥/说戏（谈补镜头、后期、拍摄安排等事务）；dialogue=多人与事项相关的对话；'
            'ambient=纯环境画面；broll=纯画面展示",\n'
            '"moments":[{"t":[起,止],"note":"该时段画面发生了什么"}],\n'
            '"ocr":[{"t":出现的大致秒,"text":"画面文字","where":"画面位置"}],\n'
            '"defects":[{"t":[起,止],"type":"blur|shake|exposure|framing|still|other",'
            '"note":"画面质量问题；没有则空数组"}],\n'
            '"usage":["剪辑用途建议，可多个（开场钩子/B-roll/特写佐证/过程记录等）"]}\n'
            "铁律：只描述确实看到的；不确定就写'不确定'，禁止推断补全；"
            "moments/ocr/defects 里的时间必须以秒为单位且不得超出 0-%.1f 秒；"
            "画面无文字则 ocr 为空数组。") % (dur, prior, dur)


def _parse_json(text):
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            d = json.loads(t[i:j + 1])
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return None


def _call_video(vp, prompt, becfg):
    key = asr.resolve_key(becfg)
    if not key:
        raise RuntimeError("未找到 vision key：设 env，或检查 api_key_file")
    b64 = base64.b64encode(open(vp, "rb").read()).decode()
    content = [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + b64}},
               {"type": "text", "text": prompt}]
    # R2-3：与 chat_llm 同款空回答升档阶梯（M3 think 吃满=空正文，同 cap 重试无意义）
    base = int(becfg.get("max_tokens", 8192))
    raw = ""
    for cap in (base, int(base * 1.5), base * 2):
        payload = {"model": becfg.get("model", "MiniMax-M3"),
                   "messages": [{"role": "user", "content": content}],
                   "max_tokens": cap}
        req = urllib.request.Request(
            becfg["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        resp = json.loads(urllib.request.urlopen(req, timeout=float(becfg.get("timeout_s", 300))).read())
        raw = re.sub(r"<think>.*?</think>", "", ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or "", flags=re.S).strip()
        if raw:
            break
    return raw


def _clamp_t(x, dur):
    """t 归一：[a,b] 或单值 → (a,b,是否越界被钳)；无效返回 None。"""
    a, b = (x if isinstance(x, list) and len(x) == 2 else [x, x])
    try:
        a, b = float(a), float(b)
    except Exception:
        return None
    oa, ob = a, b
    if dur:
        a = max(0.0, min(a, dur))
        b = max(0.0, min(b, dur))
    if b < a:
        a, b = b, a
    return (round(a, 1), round(b, 1), not (abs(oa - a) < 1e-6 and abs(ob - b) < 1e-6))


def _normalize_v2(obj, dur, provider, at=None):
    """M3 v2 输出 → 归一化契约（兼容三键 + 结构化新键；越界钳制并如实标注 flags）。"""
    flags = []
    desc = str(obj.get("summary") or obj.get("desc") or "").strip()
    ct = str(obj.get("content_type") or "").strip().lower()
    if ct not in ("narration", "meta", "dialogue", "ambient", "broll"):
        if ct:
            flags.append("content_type 非法值丢弃:%s" % ct[:20])
        ct = ""

    def tlist(key):
        out, clamped, dropped = [], 0, 0
        for m in obj.get(key) or []:
            if not isinstance(m, dict):
                continue
            r = _clamp_t(m.get("t"), dur)
            if r is None:
                dropped += 1
                continue
            a, b, ch = r
            clamped += 1 if ch else 0
            out.append({"t": [a, b], "note": str(m.get("note", ""))[:120],
                        **({"type": str(m.get("type", "other"))[:20]} if key == "defects" else {})})
        if dropped:
            flags.append("%s 时间无效丢弃:%d" % (key, dropped))
        if clamped:
            flags.append("%s 时间越界钳制:%d" % (key, clamped))
        return out

    moments = tlist("moments")
    defects = tlist("defects")
    ocr_detail, ocr = [], []
    for m in obj.get("ocr") or []:
        if not isinstance(m, dict) or not str(m.get("text", "")).strip():
            continue
        r = _clamp_t(m.get("t"), dur)
        if r is None:
            flags.append("ocr 时间无效丢弃:1")
            continue
        t = r[0]
        ocr_detail.append({"t": t, "text": str(m.get("text", ""))[:80], "where": str(m.get("where", ""))[:60]})
        ocr.append("%s（%s）@%gs" % (m.get("text", ""), m.get("where", "") or "画面", t))
    u = obj.get("usage")
    usage = "；".join(str(x) for x in u) if isinstance(u, list) else str(u or "")
    return {"desc": desc, "ocr": ocr, "usage": usage, "content_type": ct,
            "moments": moments, "defects": defects, "ocr_detail": ocr_detail,
            "frames": "video-native@720p", "provider": provider,
            "at": at or asr.datetime_iso(), "schema": 2, "flags": flags}


def _legacy_video(src, becfg, td, provider):
    """v1 降级路径：3 帧采样+旧契约（v2 失败时自动回退；schema 如实标 1）。"""
    dur = asr.media_duration(src) or 0
    pts = [max(0.05, round(dur * p, 1)) for p in (0.1, 0.5, 0.9)] if dur > 0.5 else [0.0]
    imgs = []
    for i, t in enumerate(pts):
        fp = os.path.join(td, "f%d.jpg" % i)
        subprocess.run([_ff(), "-y", "-loglevel", "error", "-ss", str(t), "-i", src,
                        "-frames:v", "1", "-vf", "scale='min(960,iw)':-2", "-q:v", "5", fp], check=True)
        imgs.append(("第%s秒采样帧" % (("%.1f" % t).rstrip("0").rstrip(".") or "0"), _datauri(fp)))
    content = [{"type": "image_url", "image_url": {"url": u}} for _, u in imgs]
    head = "以下是同一条视频按时间顺序的 %d 个采样帧。帧清单：\n%s" % (
        len(imgs), "\n".join("- " + l for l, _ in imgs))
    text = (head + '''

请只输出合法 JSON（不要 markdown 代码块、不要任何多余文字）：
{"desc":"一段话描述画面内容：场景、人物/物体、动作、构图与光线",
"ocr":["画面中出现的所有文字（招牌/字幕/标签/水印），按出现顺序；没有则空数组"],
"usage":"这条素材在短视频剪辑中的用途建议（如：开场钩子/空镜B-roll/过程记录/特写佐证/工艺展示，一句话）"}''')
    content.append({"type": "text", "text": text})
    key = asr.resolve_key(becfg)
    payload = {"model": becfg.get("model", "MiniMax-M3"),
               "messages": [{"role": "user", "content": content}], "max_tokens": 4096}  # P1#4：2048 对 M3 think 过紧，曾致空正文
    req = urllib.request.Request(
        becfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    resp = json.loads(urllib.request.urlopen(req, timeout=float(becfg.get("timeout_s", 300))).read())
    raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    t = re.sub(r"```(?:json)?", "", raw).strip()
    i, j = t.find("{"), t.rfind("}")
    d = {}
    if 0 <= i < j:
        try:
            d = json.loads(t[i:j + 1])
        except Exception:
            d = {}
    ocr = d.get("ocr") if isinstance(d.get("ocr"), list) else []
    return {"desc": str(d.get("desc") or text[:200] or ""),
            "ocr": [str(x) for x in ocr][:8],
            "usage": str(d.get("usage") or ""),
            "frames": len(imgs), "provider": provider,
            "at": asr.datetime_iso(), "schema": 1,
            "flags": ["v2 失败降级 3 帧路径"]}


def understand(src, kind, provider=None, words=None):
    """主入口：素材文件 → 归一化视觉档案（契约见文件头）。kind: image|video"""
    if kind == "audio":
        raise ValueError("音频无视觉轨——理解走 asr.py")
    svc = asr.load_services()
    vcfg = svc.get("vision", {})
    provider = provider or vcfg.get("provider", "minimax")
    becfg = vcfg.get("backends", {}).get(provider, {})
    asr.resolve_key(becfg)  # 早失败：无 key 直接抛
    tmpdir = tempfile.mkdtemp(prefix="vis_")
    try:
        if kind == "image":
            p = os.path.join(tmpdir, "img.jpg")
            downscale_image(src, p)
            imgs = [("单张图片", _datauri(p))]
            content = [{"type": "image_url", "image_url": {"url": u}} for _, u in imgs]
            content.append({"type": "text", "text": "这是一张图片。\n" + _IMAGE_PROMPT_TAIL})
            key = asr.resolve_key(becfg)
            payload = {"model": becfg.get("model", "MiniMax-M3"),
                       "messages": [{"role": "user", "content": content}], "max_tokens": 4096}  # P1#4：2048 对 M3 think 过紧，曾致空正文
            req = urllib.request.Request(
                becfg["base_url"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            resp = json.loads(urllib.request.urlopen(req, timeout=float(becfg.get("timeout_s", 300))).read())
            raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
            d = _parse_json(raw) or {}
            ocr = d.get("ocr") if isinstance(d.get("ocr"), list) else []
            return {"desc": str(d.get("desc") or raw[:200] or ""),
                    "ocr": [str(x) for x in ocr][:8],
                    "usage": str(d.get("usage") or ""),
                    "frames": 1, "provider": provider,
                    "at": asr.datetime_iso(), "schema": 1, "flags": []}
        # ---- 视频：v2 原生理解，失败降级 3 帧 ----
        dur = asr.media_duration(src) or 0
        if dur > 0.5:
            try:
                v720 = _transcode_720(src, tmpdir)
                pr = _prompt_v2(_speech_prior(words, dur), dur)
                obj = None
                # R3-5 成本注记：attempt(2) × 阶梯(3) = 持续空回答时最多 6 次计费调用才降级 3 帧。
                # 接受此上限（真实素材 think 持续吃满极罕见），不做跨层去重
                for attempt in (1, 2):  # 解析失败重试一次
                    try:
                        raw = _call_video(v720, pr, becfg)
                        obj = _parse_json(raw)
                        if obj and obj.get("summary"):
                            break
                        obj = None
                    except Exception:
                        if attempt == 2:
                            obj = None
                if obj:
                    return _normalize_v2(obj, dur, provider)
            except Exception:
                pass  # 降级路径接管，flags 如实标注
        with tempfile.TemporaryDirectory() as td2:
            return _legacy_video(src, becfg, td2, provider)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


_IMAGE_PROMPT_TAIL = '''请只输出合法 JSON（不要 markdown 代码块、不要任何多余文字）：
{"desc":"一段话描述画面内容：场景、人物/物体、动作、构图与光线",
"ocr":["画面中出现的所有文字（招牌/字幕/标签/水印），按出现顺序；没有则空数组"],
"usage":"这条素材在短视频剪辑中的用途建议（如：开场钩子/空镜B-roll/过程记录/特写佐证/工艺展示，一句话）"}'''


if __name__ == "__main__":
    print(__doc__)
