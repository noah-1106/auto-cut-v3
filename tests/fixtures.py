#!/usr/bin/env python3
"""E2E 夹具：测试底盘自给自足（2026-09-14 清场后立——E2E 曾硬编码 40 处真实素材引用，
素材一删测试即崩。夹具合成 + 词轨注入 + 项目重建，与用户素材彻底解耦。）

用法：tests/e2e.py main() 开头调 ensure()。夹具包带 "fixture": true 标记，teardown 不删（下次秒过）。
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FF = os.path.join(ROOT, "bin", "ffmpeg")
BASE = "http://127.0.0.1:8765"
PID = "demo-project"  # E2E 的 PROJ（40 处引用的包名）

# id → (时长s, 尺寸, 词尾锚点或None, 台词)  —— 锚点对齐 T21 钳制断言（7.2/3.95/10.2）
FIXTURES = [
    ("M0124", 12, "1280x720", 7.2, "我们在檀溪公馆做的窗帘盒和龙骨检查，全部按标准来。"),
    ("M0089", 10, "1280x720", 8.5, "第二遍我们再看一遍，检查所有细节有没有遗漏的地方。"),
    ("M0095", 8,  "1280x720", 3.95, "这里注意找平的细节处理。"),
    ("M0099", 12, "1280x720", 10.2, "最后一遍检查完成之后我们再统一收尾交付。"),
    ("M0109", 6,  "1920x1080", None, "横版画面检查窗帘盒和踢脚线的收口。"),
    ("M0097", 6,  "1280x720", None, "第二段口播内容。"),
    ("M0104", 6,  "1280x720", None, "第三段口播内容。"),
    ("M0101", 6,  "1280x720", None, "方版素材测试。"),
]


def _synth(mid, dur, size, tmpdir):
    """合成带音轨的测试视频（testsrc2+sine；ultrafast 压速度）。rotation 件另用 display_rotation。"""
    out = os.path.join(tmpdir, mid + ".MP4")
    cmd = [FF, "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc2=size=%s:rate=30:duration=%g" % (size, dur),
           "-f", "lavfi", "-i", "sine=frequency=440:duration=%g" % dur,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
           "-c:a", "aac", "-b:a", "64k", "-shortest", out]
    subprocess.run(cmd, check=True)
    return out


def _synth_rotated(tmpdir):
    """带 display-orientation SEI 的手机素材仿件（1920x1080 编码 + SEI 声明 -90°旋转）——t5/T18 几何链夹具。
    历史教训：合成 testsrc 无 rotation 恰好漏掉人侧上传的几何盲区（2026-09-11 rotation 必检升级）。
    ffmpeg6 实测：只有 h264_metadata bsf 的 display_orientation=insert 能写 SEI；
    局限：ffprobe 6.0 读不出该 SEI 的角度值（rotation=None），故 _rotated 审计字段不落库、
    T18 覆盖『竖版内容登记=显示尺寸』主链；角度审计链由真实素材时代验证（git 历史 T18 PASS）。"""
    out = os.path.join(tmpdir, "ROT01.MP4")
    r = subprocess.run(
        [FF, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         "-bsf:v", "h264_metadata=display_orientation=insert:rotate=-90", out],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return out


def _words_to(t_end, text):
    """台词逐字铺 0.25s→t_end，末词词尾精确钉在锚点（T21 钳制断言读词尾：7.2/10.2 必须精确）。"""
    chars = [c for c in text if not c.isspace()]
    n = len(chars)
    step = (t_end - 0.25) / max(n - 1, 1) if n > 1 else 0.25
    ws = [{"text": c, "start": round(0.25 + i * step, 2), "end": round(0.25 + (i + 1) * step, 2)}
          for i, c in enumerate(chars)]
    ws[-1]["end"] = round(t_end, 2)  # 锚点精确（0.25+i*step 的累积舍入会漂 0.3-0.5s）
    return ws


def _api(method, path, body=None, raw=None):
    import urllib.request
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else raw
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/octet-stream" if raw else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        return {"err": str(e)[:120]}


def ensure():
    """夹具就位检查：有 fixture 标记即跳过（幂等）；没有则全量合成。"""
    pp = os.path.join(ROOT, "materials", "packs", PID, "pack.json")
    if os.path.exists(pp):
        try:
            if json.load(open(pp, encoding="utf-8")).get("fixture"):
                return  # 已就位
        except Exception:
            pass
    print("  [fixtures] 合成 E2E 夹具包（一次性，~30s）…")
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="e2efix_")
    try:
        # ① 建包
        _api("POST", "/api/pack-create/", {"id": PID, "name": "E2E 夹具包（自给自足，可随时删）"})
        # ② 合成 + 上传（filename=id.MP4 → id 提取命中 M 编号规则）
        for mid, dur, size, t_end, text in FIXTURES:
            fp = _synth(mid, dur, size, tmpdir)
            raw = open(fp, "rb").read()
            _api("POST", "/api/pack-upload/%s?filename=%s.MP4" % (PID, mid), raw=raw)
        rot = _synth_rotated(tmpdir)
        if rot:
            _api("POST", "/api/pack-upload/%s?filename=ROT01.MP4" % PID, raw=open(rot, "rb").read())
        # ③ 词轨/视觉注入（pack.json 直写；合成素材没有 ASR——测试锚点直供）
        pk = json.load(open(pp, encoding="utf-8"))
        byid = {f["id"]: f for f in pk["files"]}
        for mid, dur, size, t_end, text in FIXTURES:
            f = byid.get(mid)
            if not f:
                continue
            if t_end:
                f["transcript"] = {"provider": "fixture", "tier": "char", "text": text,
                                   "duration": dur, "has_speech": True, "words": _words_to(t_end, text)}
            else:
                f["transcript"] = {"provider": "fixture", "tier": "char", "text": text,
                                   "duration": dur, "has_speech": True, "words": _words_to(dur * 0.6, text)}
            f["visual"] = {"desc": "合成测试素材：" + text[:20], "content_type": "narration",
                           "ocr": [], "usage": "E2E 夹具", "schema": 2, "frames": "fixture", "flags": []}
            f.setdefault("audit", {})["transcript"] = "done"
            f["audit"]["visual"] = "done"
        pk["fixture"] = True  # 幂等标记（teardown 不删，下次 ensure 秒过）
        json.dump(pk, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        # ④ 项目重建 + 挂载（E2E 的 PROJ 依赖）
        _api("POST", "/api/project-create/", {"name": PID, "title": "E2E 测试项目", "format": "vertical"})
        _api("POST", "/api/pack-mount/%s/%s/mount" % (PID, PID))
        # ⑤ 种子故事线（T3 断言项目有默认故事线——历史上靠 T8 起草遗留，夹具必须自给）
        _api("POST", "/api/storyline/%s?story=seed" % PID, body={
            "title": "夹具种子线", "outline": "t", "meta": {"format": "vertical"},
            "beats": [{"no": 1, "id": "b1", "story": "种子故事线（夹具）", "narration": {"mode": "original"},
                       "tracks": [{"role": "A", "source_id": "M0124", "cut_index": None, "src_in": 0,
                                   "duration": 6.0, "requirement": "t"}],
                       "music": {"inherit": True, "bgm": None},
                       "effects": {"stickers": [], "sfx": []}, "subtitle": {}, "transition_out": None}]})
        print("  [fixtures] 就位：%d 条合成素材 + 词轨注入 + 项目挂载 + 种子故事线" % len(pk["files"]))
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    ensure()
    print("fixtures OK")
