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
import argparse, binascii, json, os, sys, uuid, urllib.request, urllib.error

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


def synth(text, model=None, voice_id=None, speed=None, out=None):
    """文本 → mp3 路径。所有参数缺省回落 services.json tts 段配置。"""
    if not (text or "").strip():
        raise RuntimeError("空文本")
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
    out = out or os.path.join("/tmp", "t2a_%d.mp3" % int(time.time()))
    d = os.path.dirname(os.path.abspath(out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)  # 调用方常传深层路径（materials/dub/...）
    open(out, "wb").write(binascii.unhexlify(audio))
    return out


def voice_clone(audio_path, voice_id):
    """声纹克隆：本地音频(10s-5min, mp3/m4a/wav, ≤20MB, 单人声) → voice_id。
    产物语义与系统音色完全一致（T2A voice_setting.voice_id 直填）。
    注意：克隆音色需在 7 天内用 synth() 用一次，否则平台删除（用过即永久）。"""
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
    a = ap.parse_args()
    if a.cmd == "synth":
        out = synth(a.text, model=a.model, voice_id=a.voice, speed=a.speed, out=a.out)
        print("TTS OK:", out, "(%dB)" % os.path.getsize(out))
    else:
        vid = voice_clone(a.audio, a.voice_id)
        print("VOICE CLONE OK:", vid, "→ 可直接用于 synth --voice", vid)


if __name__ == "__main__":
    import time  # noqa: E402 synth 默认文件名用
    main()
