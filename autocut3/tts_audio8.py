#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 TTS 适配层：Audio8-TTS-Preview-0.6B（Apache 2.0，ONNX INT4 CPU）。

运行时（python3.12 venv + 模型权重）装在 ~/.local/share/autocut3/audio8/——与仓内 bin/ffmpeg
同级的"可选外部组件"，核心管线维持零 pip 依赖；本层只通过 HTTP 与本机服务通信（127.0.0.1:8024），
服务按需自启。模型/代码来源与许可：huggingface.co/Audio8/Audio8-TTS-Preview-0.6B-ONNX-INT4（HF 被墙时
走 hf-mirror.com），推理代码 github.com/Audio8-AI/Audio8_TTS，均 Apache 2.0。

参考音色=零样本克隆：register_voice(name, 参考音频, 参考逐字稿)——参考音频 0.5-30s，
逐字稿必须与 spoken 内容完全一致（模型契约，错字直接降相似度）。
"""
import json, os, sys, time, urllib.request

ARK = os.path.expanduser(os.environ.get("AUTOCUT_AUDIO8_HOME", "~/.local/share/autocut3/audio8"))
_VENV_BIN = os.path.join(ARK, "venv", "Scripts" if os.name == "nt" else "bin")
VENV_PY = os.path.join(_VENV_BIN, "python.exe" if os.name == "nt" else "python")
RT_DIR = os.path.join(ARK, "runtime", "onnx_runtime")
MODEL_DIR = os.path.join(ARK, "model")
VOICES_DIR = os.path.join(ARK, "voices")
PORT = int(os.environ.get("ARKTTS_PORT", "8024"))
BASE = "http://127.0.0.1:%d" % PORT
LAST_USE = os.path.join(ARK, "last-use")  # 闲置看门狗的续命标记（synth/register 成功即刷新）
IDLE_S = int(os.environ.get("ARKTTS_IDLE_S", "3600"))  # 空闲自动退出窗（Noah 2026-09-19：60 分钟）


def _touch_use():
    """刷新 last-use（看门狗读它的 mtime 判闲置）。"""
    try:
        with open(LAST_USE, "a"):
            os.utime(LAST_USE)
    except OSError:
        pass


def installed():
    """运行时三件套是否齐备（venv 解释器/推理代码/模型权重）。"""
    return (os.path.exists(VENV_PY) and os.path.isdir(RT_DIR)
            and os.path.exists(os.path.join(MODEL_DIR, "slow_ar_int4.onnx"))
            and os.path.exists(os.path.join(MODEL_DIR, "slow_ar_int4.onnx.data")))


def _get(path, timeout=5):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=timeout).read())


def _health():
    try:
        return bool(_get("/api/health").get("ok"))
    except Exception:
        return False


def ensure_server(timeout_s=180):
    """服务不在就自启（nohup 脱离），健康检查通过才返回。"""
    if _health():
        return
    if not installed():
        raise RuntimeError(
            "Audio8 本地 TTS 未安装（期望 %s 下有 venv/ runtime/ model/）——安装步骤见 README「本地 TTS」" % ARK)
    import subprocess
    os.makedirs(VOICES_DIR, exist_ok=True)
    env = dict(os.environ,
               ARKTTS_MODEL_DIR=MODEL_DIR, ARKTTS_VOICES_DIR=VOICES_DIR,
               ARKTTS_REGISTRATION_DIR=os.path.join(MODEL_DIR, "registration"),
               ARKTTS_PRECISION="int4", ARKTTS_CODEC_PRECISION="fp16",
               PORT=str(PORT), ARKTTS_THREADS=os.environ.get(
                   "ARKTTS_THREADS", str(min(8, (os.cpu_count() or 6)))),  # 默认线程按核数取（2026-09-15 试听提速）
               PATH=_VENV_BIN + os.pathsep + os.environ.get("PATH", ""))
    log = open(os.path.join(ARK, "service.log"), "ab")
    # 启动方式对齐上游 run_server.sh：service.py 只定义 app，必须由 uvicorn 拉起
    proc = subprocess.Popen([VENV_PY, "-m", "uvicorn", "arktts_runtime.service:app",
                             "--app-dir", RT_DIR, "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=RT_DIR, env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if _health():
            break
        time.sleep(1.0)
    else:
        raise RuntimeError("Audio8 服务 %ds 未就绪——见 %s/service.log" % (timeout_s, ARK))
    _touch_use()
    # 闲置看门狗（2026-09-19）：空闲 IDLE_S 自动回收服务（下次调用自启，零感知）；
    # pid 只有自启路径知道——外部 run_server.sh 拉起的不在覆盖内
    subprocess.Popen([sys.executable,
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_idlekill.py"),
                      str(proc.pid), str(IDLE_S)],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def voices():
    """已注册参考音色清单（服务未起时返回空表，不副作用启动）。"""
    if not _health():
        return []
    try:
        r = _get("/api/voices")
        return r.get("voices", []) if isinstance(r, dict) else r
    except Exception:
        return []


def synth(text, out_wav, voice=None, max_new_tokens=1024, timeout_s=600):
    """文本 → wav（44.1kHz mono fp32）。voice=None 用服务端默认/首个已注册音色。"""
    ensure_server()
    if not voice:
        vs = voices()
        if not vs:
            raise RuntimeError("Audio8 无已注册参考音色——先 register_voice()（零样本克隆需要参考音频+逐字稿）")
        voice = vs[0].get("name") if isinstance(vs[0], dict) else vs[0]
    body = json.dumps({"text": text, "voice_name": voice, "max_new_tokens": max_new_tokens}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/tts", data=body,
                                 headers={"Content-Type": "application/json"})
    wav = urllib.request.urlopen(req, timeout=timeout_s).read()
    if not wav.startswith(b"RIFF"):
        raise RuntimeError("Audio8 返回非 WAV（可能 4xx/5xx 页面）——前 80 字节: %r" % wav[:80])
    with open(out_wav, "wb") as fh:
        fh.write(wav)
    _touch_use()  # 真实使用过模型 → 闲置计时归零（voices 查询不算）
    return out_wav


def register_voice(name, audio_path, transcript, overwrite=False, timeout_s=300):
    """注册/更新参考音色。audio_path 0.5-30s wav/mp3（服务自动转 44.1k mono）；
    transcript=参考音频的逐字稿，必须与 spoken 内容一字不差。"""
    ensure_server()
    import uuid
    b = uuid.uuid4().hex
    fname = os.path.basename(audio_path)
    head = ("--%s\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n") % (b, fname)
    parts = [
        ("--%s\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n%s\r\n" % (b, transcript)).encode("utf-8"),
        ("--%s\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n%s\r\n" % (b, name)).encode("utf-8"),
        ("--%s\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\n%s\r\n" % (b, "true" if overwrite else "false")).encode(),
        head.encode("utf-8") + open(audio_path, "rb").read() + ("\r\n--%s--\r\n" % b).encode(),
    ]
    req = urllib.request.Request(
        BASE + "/api/voices/register", data=b"".join(parts),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout_s).read())
        _touch_use()  # 注册走模型处理参考音频 → 算使用
        return r
    except urllib.error.HTTPError as e:
        raise RuntimeError("Audio8 注册音色失败 [%s]: %s" % (e.code, e.read()[:200]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print("installed:", installed(), "| server:", _health(), "| voices:", voices())
    elif cmd == "synth":
        print(synth(sys.argv[2], sys.argv[3], voice=sys.argv[4] if len(sys.argv) > 4 else None))
    elif cmd == "register":
        print(register_voice(sys.argv[2], sys.argv[3], sys.argv[4],
                             overwrite=("--force" in sys.argv)))
