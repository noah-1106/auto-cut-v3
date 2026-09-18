#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TTS 适配层：供应商可换（config/services.json tts 段），产物统一 mp3。

契约（下游 R3 dub 唯一依赖）：
  synth(text, model=None, voice_id=None, speed=None, out=None) → out 路径
  voice_id 可以是系统音色，也可以是声纹克隆音色（voiceclone 子命令产物，7 天内用一次即永久）。

供应商事实（2026-09-11 冒烟实测，minimax t2a_v2）：
  - 与 ASR/LLM 同域同 key（api.minimaxi.com/v1）
  - base_resp.status_code=0 ≠ 完成：必须校验 data.status==2，否则拿到伪成功短载荷
  - data.audio 为 hex 编码 mp3（ID3 头实测验证）
  - speed [0.5-2]：speech-2.8 1.0≈4.1字/s、1.25≈5.3字/s（时长约束的标定基准，按模型实测）

用法：
  python3 autocut3/tts.py synth --text "..." [--out x.mp3] [--model m] [--voice v] [--speed 1.15]
  python3 autocut3/tts.py voiceclone --audio 10s+人声.mp3 --voice-id demo_v1
"""
import argparse, binascii, json, os, sys, tempfile, time, uuid, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asr import load_services, resolve_key  # noqa: E402 key 解析与供应商登记同源，DRY

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _backend():
    sv = load_services()
    ts = sv.get("tts") or {}
    be = (ts.get("backends") or {}).get(ts.get("provider") or "minimax")
    if not be:
        raise RuntimeError("services.json 缺 tts 段（供应商登记处）")
    return be


def _provider():
    return (load_services().get("tts") or {}).get("provider") or "minimax"


def _ensure_parent(out):
    d = os.path.dirname(os.path.abspath(out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)  # 调用方常传深层路径（materials/dub/...）


def _wav_to_mp3(wav, out):
    """Audio8 产物 44.1kHz wav → 契约 mp3（128k）。ffmpeg 走平台解析链（asr.ffmpeg_path）。"""
    import subprocess
    from asr import ffmpeg_path
    out = out or os.path.join(tempfile.gettempdir(), "audio8_%d.mp3" % int(time.time()))
    _ensure_parent(out)
    r = subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-i", wav,
                        "-c:a", "libmp3lame", "-b:a", "128k", out], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("wav→mp3 转码失败: %s" % (r.stderr or "")[-200:])
    return out


def synth(text, model=None, voice_id=None, speed=None, out=None):
    """文本 → mp3 路径。所有参数缺省回落 services.json tts 段配置。"""
    if not (text or "").strip():
        raise RuntimeError("空文本")
    if _provider() == "local-audio8":
        # 本地 Audio8-TTS（Apache 2.0，ONNX INT4）——零样本克隆音色，speed 参数不适用
        import tempfile
        import tts_audio8
        wav = os.path.join(tempfile.mkdtemp(prefix="audio8_"), "synth.wav")
        tts_audio8.synth(text, wav, voice=voice_id)
        return _wav_to_mp3(wav, out)
    be = _backend()
    key = resolve_key(be)
    body = {
        "model": model or be.get("model", "speech-2.8-turbo"),
        "text": text,
        "voice_setting": {"voice_id": voice_id or be.get("voice_id", "male-qn-qingse"),
                          "speed": float(speed or be.get("speed", 1.0)), "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
    }
    req = urllib.request.Request(
        be["base_url"].rstrip("/") + "/t2a_v2",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=be.get("timeout_s", 60)).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("TTS HTTP%s: %s" % (e.code, e.read()[:200]))
    st = resp.get("base_resp") or {}
    if st.get("status_code", -1) != 0:
        raise RuntimeError("TTS 失败 [%s] %s" % (st.get("status_code"), st.get("status_msg")))
    d = resp.get("data") or {}
    if d.get("status") != 2:
        raise RuntimeError("TTS 未完成态 data.status=%s（伪成功防护）" % d.get("status"))
    audio = d.get("audio") or ""
    if not audio:
        raise RuntimeError("TTS 无音频载荷")
    out = out or os.path.join(tempfile.gettempdir(), "t2a_%d.mp3" % int(time.time()))
    _ensure_parent(out)
    open(out, "wb").write(binascii.unhexlify(audio))
    return out


def voice_clone(audio_path, voice_id, transcript=None):
    """声纹克隆 → voice_id。
    minimax：本地音频(10s-5min, ≤20MB)上传克隆（transcript 不需要）。
    local-audio8：零样本克隆，transcript=参考音频逐字稿（必须与 spoken 内容一字不差，模型契约）。"""
    if _provider() == "local-audio8":
        if not transcript:
            raise RuntimeError("local-audio8 克隆必须给 --transcript（参考音频的逐字稿，一字不差是模型硬契约）")
        import tts_audio8
        return tts_audio8.register_voice(voice_id, audio_path, transcript,
                                         overwrite=True).get("voice", {}).get("name", voice_id)
    be = _backend()
    key = resolve_key(be)
    if not os.path.exists(audio_path):
        raise RuntimeError("无音频: %s" % audio_path)
    if os.path.getsize(audio_path) > 20 << 20:
        raise RuntimeError("音频超 20MB")
    # ① 上传（multipart，purpose=voice_clone）
    boundary = "----autocut" + uuid.uuid4().hex
    parts = [
        ('--%s\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nvoice_clone\r\n' % boundary).encode(),
        ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: audio/mpeg\r\n\r\n'
         % (boundary, os.path.basename(audio_path))).encode(),
        open(audio_path, "rb").read(), b"\r\n",
        ("--%s--\r\n" % boundary).encode(),
    ]
    req = urllib.request.Request(
        be["base_url"].rstrip("/") + "/files/upload",  # 国内站上传端点（/files 是 404）
        data=b"".join(parts), method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                 "Authorization": "Bearer " + key})
    try:
        up = json.loads(urllib.request.urlopen(req, timeout=120).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("上传 HTTP%s: %s" % (e.code, e.read()[:200]))
    st = up.get("base_resp") or {}
    if st.get("status_code", -1) != 0:
        raise RuntimeError("上传失败 [%s] %s" % (st.get("status_code"), st.get("status_msg")))
    file_id = (up.get("file") or {}).get("file_id") or up.get("file_id")
    if not file_id:
        raise RuntimeError("上传无 file_id: %s" % json.dumps(up, ensure_ascii=False)[:200])
    # ② 克隆
    req = urllib.request.Request(
        be["base_url"].rstrip("/") + "/voice_clone",
        data=json.dumps({"file_id": file_id, "voice_id": voice_id}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        cl = json.loads(urllib.request.urlopen(req, timeout=300).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("克隆 HTTP%s: %s" % (e.code, e.read()[:300]))
    st = cl.get("base_resp") or {}
    if st.get("status_code", -1) != 0:
        raise RuntimeError("克隆失败 [%s] %s" % (st.get("status_code"), st.get("status_msg")))
    return voice_id


def main():
    ap = argparse.ArgumentParser(description="TTS 生成器（synth / voiceclone）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("synth")
    s1.add_argument("--text", required=True)
    s1.add_argument("--out")
    s1.add_argument("--model")
    s1.add_argument("--voice")
    s1.add_argument("--speed", type=float)
    s2 = sub.add_parser("voiceclone")
    s2.add_argument("--audio", required=True)
    s2.add_argument("--voice-id", required=True)
    s2.add_argument("--transcript", help="local-audio8 必填：参考音频的逐字稿（一字不差）")
    a = ap.parse_args()
    if a.cmd == "synth":
        out = synth(a.text, model=a.model, voice_id=a.voice, speed=a.speed, out=a.out)
        print("TTS OK:", out, "(%dB)" % os.path.getsize(out), flush=True)
    else:
        vid = voice_clone(a.audio, a.voice_id, transcript=a.transcript)
        print("VOICE CLONE OK:", vid, "→ 可直接用于 synth --voice", vid, flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
