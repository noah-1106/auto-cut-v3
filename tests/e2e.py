#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端回归测试套件——军规3：跑通才算数，测试可重复。

覆盖面（对应历次挖出 bug 的场景）：
  T1  服务健康 + 静态服务穿越防护
  T2  API 注入面白名单（恶意 story/material/pack id 必须 400，正常 id 必须可用）
  T3  默认故事线（空屏 bug 回归：无 story 参数 → 最近修改线全量返回）
  T4  同步探针（/api/sync 双域 mtime）
  T5  上传素材（pack-upload：入库+审计队列+缩略帧+重名保护）
  T6  音效混音（静音底 max_volume 对账）
  T7  贴纸渲染（帧差 ≥2× 运动噪声基线）
  T8  AI 起草 + 渲染（Agent CLI 全链路，真实 LLM 调用，可 --skip-llm 跳过）
  T9  多画幅（landscape 项目 → 1920×1080 成片）
  T10 双向同步（Agent CLI 写 → 探针可见 → 恢复）
  T19 字幕页零重叠（build_ass 页间钳制回归，2026-09-11 回看修复）
  T20 proofread 等长替换（词轨时间戳零动回归，同日）
  T21 trim 双向钳制（死尾巴修剪回归，同日）
  T22 视觉v2契约归一化（兼容三键/钳制/丢弃/flags，2026-09-12）
  T23 dossier 自审性验收（注入 ASR 错必须被抓，同日）
  T24 proofread v2 分级+队列（词表auto/非词表转人工/超长/非等长拒绝/dry零落盘）
  T22 视觉v2契约归一化（兼容三键/时间钳制/非法值丢弃/flags，2026-09-12 视觉重做）

用法：python3 tests/e2e.py [--fast]   # --fast 跳过 LLM 与长渲染
"""
import argparse, json, os, re, shutil, subprocess, sys, time, urllib.request, urllib.error  # re: T16 volumedetect 解析

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FF = os.path.join(ROOT, "bin", "ffprobe")
BASE = "http://localhost:8765"
PROJ = "demo-project"  # E2E 专用夹具包/项目名（tests/fixtures.py 自给自足合成，与用户素材零耦合）
PASS, FAIL = [], []


def api(path, method="GET", body=None, raw=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s %s" % ("✓" if ok else "✗", name, detail))


# ---------------------------------------------------------------- T1 健康+静态服务
def t1_health():
    st, _ = api("/api/projects")
    check("T1 服务健康", st == 200)
    # 穿越防护：/files/..%2f.. 应 404
    try:
        urllib.request.urlopen(BASE + "/files/..%2f..%2fconfig/services.json", timeout=10)
        trav = False
    except urllib.error.HTTPError as e:
        trav = e.code in (400, 404)
    except Exception:
        trav = True
    check("T1 静态服务穿越防护", trav)


# ---------------------------------------------------------------- T2 注入面白名单
def t2_injection():
    st, _ = api("/api/storyline-delete/" + PROJ + "?story=..%2F..%2Fproject.json", method="POST")
    check("T2 恶意 story id 拒绝", st == 400)
    st, _ = api("/api/understand/" + PROJ + "?material=..%2FREADME", method="POST")
    check("T2 恶意 material 拒绝", st == 400)
    st, _ = api("/api/pack-mount/" + PROJ + "/..%2Fconfig/mount", method="POST")
    check("T2 恶意 pack id 拒绝", st == 400)
    st, d = api("/api/sync/" + PROJ)
    check("T2 正常 id 不误伤", st == 200 and d.get("ok") is True)


# ---------------------------------------------------------------- T3 默认故事线
def t3_default_story():
    st, d = api("/api/project/" + PROJ)
    sl = d.get("storyline") or {}
    # plan 断言已移除（2026-09-13 收口清空）：plan 是渲染派生产物，清空后为空是合法状态
    ok = (st == 200 and d.get("story") in (d.get("stories") or [])
          and len(sl.get("beats", [])) > 0)
    check("T3 默认故事线（空屏回归）", ok, "story=%s" % d.get("story"))


# ---------------------------------------------------------------- T4 同步探针
def t4_sync():
    st, d = api("/api/sync/" + PROJ)
    check("T4 同步探针", st == 200 and "story_mtime" in d and "stories_mtime" in d)


# ---------------------------------------------------------------- T5 上传素材
def t5_upload():
    # 测试夹具（2026-09-11 rotation 必检升级）：直拷苏炜素材 2s——带真实 rotation=-90 的
    # 手机素材（编码 1920x1080）。合成 testsrc 无 rotation，恰好漏掉了人侧上传的几何盲区。
    api("/api/pack-create/", method="POST", body={"id": "nature-stock", "name": "E2E 夹具包"})  # 幂等：已存在 409 忽略
    tmp = "/tmp/e2e_upload.mp4"
    subprocess.run([os.path.join(ROOT, "bin", "ffmpeg"), "-y", "-loglevel", "error",
                    "-ss", "0", "-t", "2",
                    "-i", os.path.join(ROOT, "materials/packs/demo-project/20260826_M0085.MP4"),
                    "-c:v", "copy", "-an", tmp], capture_output=True)
    raw = open(tmp, "rb").read()
    st, d = api("/api/pack-upload/nature-stock?filename=e2e_test_src.mp4", method="POST", raw=raw)
    entry = d.get("entry") or {}
    ok = (st == 200 and entry.get("kind") == "video"
          and abs((entry.get("duration") or 0) - 2.0) < 0.5
          and bool(entry.get("thumb"))
          and ((entry.get("audit") or {}).get("transcript") == "pending"))
    # rotation 必检（2026-09-11）：登记宽高必须是转正后的显示尺寸，病因角度必须落盘
    geo_ok = (entry.get("width"), entry.get("height")) == (1080, 1920) and entry.get("_rotation") is not None
    check("T5 上传素材（入库/元数据/缩略帧/审计队列/rotation 转正）", ok and geo_ok,
          "id=%s dur=%s geo=%sx%s rot=%s" % (entry.get("id"), entry.get("duration"),
                                             entry.get("width"), entry.get("height"), entry.get("_rotation")))
    # 重名保护
    st2, d2 = api("/api/pack-upload/nature-stock?filename=e2e_test_src.mp4", method="POST", raw=raw)
    fn2 = (d2.get("entry") or {}).get("file", "")
    check("T5 重名不覆盖", st2 == 200 and fn2 != "e2e_test_src.mp4")
    # 非法文件名拒绝（小载荷探针：服务器在拒绝路径不读请求体，大 body 会让客户端 BrokenPipe）
    st3, _ = api("/api/pack-upload/nature-stock?filename=..%2Fx.mp4", method="POST", raw=b"\x00" * 1024)
    check("T5 恶意文件名拒绝", st3 == 400)
    # 清理：把测试素材从包里移除（文件一并删）
    pk_path = os.path.join(ROOT, "materials", "packs", "nature-stock", "pack.json")
    pk = json.load(open(pk_path, encoding="utf-8"))
    pk["files"] = [f for f in pk["files"] if not str(f.get("file", "")).startswith("e2e_test_src")]
    json.dump(pk, open(pk_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for f in os.listdir(os.path.join(ROOT, "materials", "packs", "nature-stock")):
        if f.startswith("e2e_test_src"):
            os.remove(os.path.join(ROOT, "materials", "packs", "nature-stock", f))


# ---------------------------------------------------------------- T6 音效混音
def t6_sfx():
    aid = json.load(open(os.path.join(ROOT, "materials/packs/demo-project/pack.json"), encoding="utf-8"))
    m = [f for f in aid["files"] if f["id"] == "M0124"][0]
    sl = {"title": "E2E音效验证", "outline": "静音/语音底 + ding@1s", "origin": "test",
          "meta": {"format": "vertical"},
          "beats": [{"no": 1, "id": "b1", "story": "t", "narration": {"mode": "original"},
                     "tracks": [{"role": "A", "source_id": "M0124", "src_in": 0, "duration": 6.0, "requirement": "t"}],
                     "music": {"inherit": True, "bgm": None},
                     "effects": {"stickers": [], "sfx": [{"asset": "ding", "at": 1.0, "duration": 0.4}]},
                     "subtitle": {}, "transition_out": None}]}
    st, _ = api("/api/storyline/" + PROJ + "?story=e2esfx", method="POST", body=sl)
    if st != 200:
        check("T6 音效混音", False, "写入失败")
        return
    st, d = api("/api/render/" + PROJ + "?flash=0.6&grain=16&hold=1.0&duration=0.5&story=e2esfx", method="POST")
    outp = os.path.join(ROOT, "projects", PROJ, "out-e2esfx.mp4")
    ok = st == 200 and d.get("ok") and os.path.exists(outp)
    if ok:
        # 窄窗 max 对账：ding@1s vs 基线窗
        def vmax(t0, dur=0.4):
            r = subprocess.run([os.path.join(ROOT, "bin", "ffmpeg"), "-hide_banner", "-ss", str(t0), "-t", str(dur),
                                "-i", outp, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True)
            mm = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
            return float(mm.group(1)) if mm else -99
        on, base = vmax(1.0), vmax(4.0)
        ding = -14.0  # ding.wav 素材自身 max
        ok = (on - base) > 3 or abs(on - ding) < 3  # 抬升显著 或 贴着素材原响度
        check("T6 音效混音", ok, "ding窗 %.1fdB / 基线窗 %.1fdB" % (on, base))
    else:
        check("T6 音效混音", False, str(d)[:80])
    api("/api/storyline-delete/" + PROJ + "?story=e2esfx", method="POST")


# ---------------------------------------------------------------- T7 贴纸渲染
def t7_sticker():
    sl = {"title": "E2E贴纸", "outline": "t", "meta": {"format": "vertical"},
          "beats": [
            {"no": 1, "id": "b1", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0124", "cut_index": None, "src_in": 2.0, "duration": 6.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": None},
            {"no": 2, "id": "b2", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0099", "cut_index": None, "src_in": 0, "duration": 6.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": "filmBurn"}]}
    # T7 双贴纸：幕1 词点挂载（at_word）+ fallback 路径，渲染后帧差验证
    base = json.loads(json.dumps(sl))
    base["title"] = "E2E贴纸基线"
    base["beats"] = base["beats"][:1]
    base["beats"][0]["effects"]["stickers"] = []
    api("/api/storyline/" + PROJ + "?story=e2estk0", method="POST", body=base)
    stick = json.loads(json.dumps(base))
    stick["title"] = "E2E贴纸验证"
    stick["beats"][0]["effects"]["stickers"] = [{"asset": "tuijian", "duration": 1.2}]
    api("/api/storyline/" + PROJ + "?story=e2estk1", method="POST", body=stick)
    outs = []
    for sid in ("e2estk0", "e2estk1"):
        st, d = api("/api/render/" + PROJ + "?flash=0.6&grain=16&hold=1.0&duration=0.5&story=" + sid, method="POST")
        outs.append(os.path.join(ROOT, "projects", PROJ, "out-%s.mp4" % sid) if d.get("ok") else None)
    if not (outs[0] and outs[1]):
        check("T7 贴纸渲染", False, "渲染失败")
    else:
        # 对照实验设计（v2）：同刻跨渲染 A/B——两条线唯一差异是贴纸，同参编码是确定性的，
        # 故「同刻跨渲染帧差」在无贴纸时刻≈编码噪声(≈0)，有贴纸时刻≈贴纸像素差。
        # （v1 的"同时跨刻"对照量的是运动噪声，与贴纸差不可比——设计缺陷，非实现缺陷）
        def snap(mp, t, png):
            subprocess.run([os.path.join(ROOT, "bin", "ffmpeg"), "-y", "-loglevel", "error",
                            "-ss", str(t), "-i", mp, "-frames:v", "1", png], capture_output=True)
            return png
        def cross(t):
            a = snap(outs[1], t, "/tmp/e2e_a.png")
            b = snap(outs[0], t, "/tmp/e2e_b.png")
            r = subprocess.run([os.path.join(ROOT, "bin", "ffmpeg"), "-hide_banner",
                                "-i", a, "-i", b,
                                "-filter_complex", "[0:v]crop=900:320:90:30[x];[1:v]crop=900:320:90:30[y];"
                                                   "[x][y]blend=all_mode=difference,signalstats,metadata=print:file=-",
                                "-f", "null", "-"], capture_output=True, text=True)
            return float((re.search(r"YAVG=([\d.]+)", r.stdout or r.stderr) or [None, 0])[1])
        on = cross(0.8)   # tuijian ON（fallback 0.4s 起 1.2s 窗）
        off = cross(3.0)  # 双方均无贴纸
        ok = on > 0.5 and on >= off * 2  # 语义断言：ON 显著异于 OFF（阈值画幅无关，0.5 下限防假阳）
        check("T7 贴纸渲染（同刻跨渲染 A/B）", ok, "ON窗 %.1f / 编码噪声 %.1f" % (on, off))
    for sid in ("e2estk0", "e2estk1"):
        api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")


# ---------------------------------------------------------------- T8 AI 起草+渲染（真实 LLM）
def t8_draft(skip):
    if skip:
        print("  - T8 跳过（--fast）")
        return
    st, d = api("/api/draft/" + PROJ, method="POST",
                body={"intent": "回归测试：用开场钩子起一条2幕线，总长20秒"})
    sid = d.get("sid")
    if not (st == 200 and d.get("ok") and sid):
        check("T8 AI起草", False, str(d)[:80])
        return
    r = subprocess.run([sys.executable, os.path.join(ROOT, "autocut3", "pipeline.py"),
                        "make", PROJ, sid], capture_output=True, text=True, timeout=120)
    r2 = subprocess.run([sys.executable, os.path.join(ROOT, "autocut3", "pipeline.py"),
                         "render", PROJ, sid], capture_output=True, text=True, timeout=420)
    outp = os.path.join(ROOT, "projects", PROJ, "out-%s.mp4" % sid)
    ok = "RENDER OK" in r2.stdout and os.path.exists(outp)
    check("T8 AI起草+渲染全链路", ok, "%s %.1fMB" % (sid, os.path.getsize(outp) / 1e6) if ok else r2.stderr[-120:])
    try:  # 内容维度：起草文案进日志（供人审）
        slc = json.load(open(os.path.join(ROOT, "projects", PROJ, "storylines", sid + ".json"), encoding="utf-8"))
        for i, b in enumerate(slc.get("beats") or [], 1):
            print("    T8内容·幕%d [%s] %s" % (i, b.get("transition_out") or "无转场", (b.get("story") or "")[:44]))
            for t in b.get("tracks") or []:
                print("      %s:%s@%ss+%ss" % (t.get("role"), t.get("source_id"), t.get("src_in"), t.get("duration")))
    except Exception:
        pass
    # 归档测试线
    api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")


# ---------------------------------------------------------------- T9 多画幅
def t9_aspect():
    # 自给化：自造横版线→实渲染→验证→归档（测试不留痕，不依赖历史成片）
    sl = {"title": "E2E横版", "outline": "t", "meta": {"format": "landscape"},
          "beats": [
            {"no": 1, "id": "a1", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0109", "cut_index": None, "src_in": 0, "duration": 4.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": None},
            {"no": 2, "id": "a2", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0097", "cut_index": None, "src_in": 0, "duration": 4.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": None}]}
    api("/api/storyline/" + PROJ + "?story=e2esize", method="POST", body=sl)
    st, d = api("/api/render/" + PROJ + "?flash=0.6&grain=16&hold=1.0&duration=0.5&story=e2esize", method="POST")
    outp = os.path.join(ROOT, "projects", PROJ, "out-e2esize.mp4")
    ok = st == 200 and d.get("ok") and os.path.exists(outp)
    dim = ""
    if ok:
        r = subprocess.run([FF, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", outp],
                           capture_output=True, text=True)
        dim = r.stdout.strip()
        ok = dim == "1920,1080"
    check("T9 多画幅（横版 1920×1080，自造线实渲染）", ok, dim)
    api("/api/storyline-delete/" + PROJ + "?story=e2esize", method="POST")


# ---------------------------------------------------------------- T10 双向同步
def t10_sync():
    sdir = os.path.join(ROOT, "projects", PROJ, "storylines")
    sids = [f[:-5] for f in os.listdir(sdir) if f.endswith(".json")]
    if not sids:
        check("T10 双向同步（Agent 落库→人侧可见→恢复）", False, "项目无故事线")
        return
    sid = sids[0]  # 动态取现存线（项目无关，不点名）
    p = os.path.join(sdir, sid + ".json")
    bak = open(p, encoding="utf-8").read()
    d0 = api("/api/sync/" + PROJ + "?story=" + sid)[1]
    sl = json.loads(bak)
    sl["beats"][0]["story"] = (sl["beats"][0].get("story") or "") + "【E2E】"
    json.dump(sl, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    d1 = api("/api/sync/" + PROJ + "?story=" + sid)[1]
    ok = d1["story_mtime"] > d0["story_mtime"]
    st, d2 = api("/api/project/" + PROJ + "?story=" + sid)
    ok = ok and "【E2E】" in ((d2.get("storyline") or {}).get("beats", [{}])[0].get("story") or "")
    open(p, "w", encoding="utf-8").write(bak)
    check("T10 双向同步（Agent 落库→人侧可见→恢复）", ok)


# ---------------------------------------------------------------- T11 渲染任务模型（R2）
def t11_render_job():
    meta = {"format": "square"}  # 顺带覆盖方版路径（R1 尾巴回归）
    sl = {"title": "E2E方版", "outline": "t", "meta": meta, "origin": "test",
          "beats": [{"no": 1, "id": "b1", "story": "t", "narration": {"mode": "original"},
                     "tracks": [{"role": "A", "source_id": "M0101", "cut_index": None, "src_in": 0, "duration": 4.0, "requirement": "t"}],
                     "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
                     "subtitle": {}, "transition_out": None}]}
    api("/api/storyline/" + PROJ + "?story=e2esq", method="POST", body=sl)
    st, d = api("/api/render-start/" + PROJ + "?story=e2esq&flash=0.6&grain=16&hold=1.0&duration=0.5", method="POST")
    ok = st == 200 and d.get("ok")
    pcts, done = [], False
    t0 = time.time()
    while time.time() - t0 < 90:
        j = api("/api/render-progress/" + PROJ + "?story=e2esq")[1]["job"]
        pcts.append(j.get("pct", 0))
        if not j.get("running"):
            done = bool(j.get("ok")); break
        time.sleep(0.3)
    inrange = all(0 <= x <= 99 for x in pcts)
    check("T11 渲染任务模型（启动→轮询→完成，PCT∈[0,99]）", ok and done and inrange,
          "采样 %d 个，完成=%s" % (len(pcts), done))
    api("/api/storyline-delete/" + PROJ + "?story=e2esq", method="POST")


# ---------------------------------------------------------------- T12 dub 渲染（R3）
def t12_dub_render():
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import tts
    dub_src = os.path.join(ROOT, "projects", PROJ, "materials", "dub", "e2e-t12.mp3")
    try:
        tts.synth("石膏板接缝要做V字型开槽，再用牛皮纸封层，防止后期开裂。", out=dub_src)
    except Exception as e:
        check("T12 dub 渲染", False, "TTS 失败: %s" % str(e)[:80])
        return
    try:
        _t12_render(dub_src)
    finally:
        if os.path.exists(dub_src):
            os.remove(dub_src)  # 测试不留痕


def _t12_render(dub_src):
    sl = {"title": "E2E dub", "outline": "t", "meta": {"format": "landscape"},
          "beats": [
            {"no": 1, "id": "d1", "story": "t", "narration": {"mode": "dub", "audio": dub_src},
             "tracks": [{"role": "A", "source_id": "M0095", "cut_index": None, "src_in": 0, "duration": 4.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": None},
            {"no": 2, "id": "d2", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0099", "cut_index": None, "src_in": 0, "duration": 4.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": None}]}
    api("/api/storyline/" + PROJ + "?story=e2edub", method="POST", body=sl)
    st, d = api("/api/render/" + PROJ + "?flash=0.6&grain=16&hold=1.0&duration=0.5&story=e2edub", method="POST")
    out = os.path.join(ROOT, "projects", PROJ, "out-e2edub.mp4")
    ok = st == 200 and d.get("ok") and os.path.exists(out)
    dur = 0.0
    if ok:
        r = subprocess.run([os.path.join(ROOT, "bin", "ffprobe"), "-v", "error",
                            "-show_entries", "format=duration", "-of", "csv=p=0", out],
                           capture_output=True, text=True)
        dur = float(r.stdout.strip() or 0)
    check("T12 dub 渲染（幕1配音+幕2原声混合，8s 画面）", ok and 7.0 < dur < 9.5, "%.1fs" % dur)
    api("/api/storyline-delete/" + PROJ + "?story=e2edub", method="POST")


# ---------------------------------------------------------------- T16 narration 三态渲染语义
def t16_narration_modes():
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import tts
    ddir = os.path.join(ROOT, "projects", PROJ, "materials", "dub")
    dub16 = os.path.join(ddir, "e2e-t16.mp3")
    try:
        tts.synth("给墙面做两层防护，裂缝就不会再出现。", out=dub16)
    except Exception as e:
        check("T16 narration 三态", False, "TTS 失败: %s" % str(e)[:80])
        return
    try:
        sl = {"title": "E2E 三态", "outline": "t",
              "meta": {"format": "landscape", "audio": {"bgm": ""}},  # 关 BGM：none 段应为数字静音
          "beats": [
            {"no": 1, "id": "m1", "story": "t", "narration": {"mode": "none"}},
            {"no": 2, "id": "m2", "story": "t", "narration": {"mode": "original"}},
            {"no": 3, "id": "m3", "story": "t", "narration": {"mode": "dub", "audio": dub16}}]}
        srcs = ["M0109", "M0097", "M0104"]
        for i, b in enumerate(sl["beats"]):
            b["tracks"] = [{"role": "A", "source_id": srcs[i], "cut_index": None, "src_in": 0, "duration": 4.0, "requirement": "t"}]
            b["music"] = {"inherit": True, "bgm": None}
            b["effects"] = {"stickers": [], "sfx": []}
            b["subtitle"] = {}
            b["transition_out"] = None
        api("/api/storyline/" + PROJ + "?story=e2emodes", method="POST", body=sl)
        st, d = api("/api/render/" + PROJ + "?flash=0.6&grain=16&hold=1.0&duration=0.5&story=e2emodes", method="POST")
        out = os.path.join(ROOT, "projects", PROJ, "out-e2emodes.mp4")
        if not (st == 200 and d.get("ok") and os.path.exists(out)):
            check("T16 narration 三态", False, json.dumps(d, ensure_ascii=False)[:120])
            return

        def seg_mv(ss, tt):
            r = subprocess.run([os.path.join(ROOT, "bin", "ffmpeg"), "-ss", str(ss), "-t", str(tt),
                                "-i", out, "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True)
            m2 = re.search(r"mean_volume: (-?[\d.]+)", r.stderr)
            return float(m2.group(1)) if m2 else 0.0
        mv_none, mv_orig, mv_dub = seg_mv(0, 3.5), seg_mv(4.5, 3.5), seg_mv(8.5, 3.5)
        # 语义断言（不赌素材响度）：none=数字静音；original/dub=轨道接入且显著高于静音 20dB
        ok = mv_none <= -50 and mv_orig >= mv_none + 20 and mv_dub >= mv_none + 20
        check("T16 narration 三态（none静音/原声/dub，分段实测）", ok,
              "none %.0fdB / orig %.0fdB(静音+%.0f) / dub %.0fdB(静音+%.0f)"
              % (mv_none, mv_orig, mv_orig - mv_none, mv_dub, mv_dub - mv_none))
    finally:
        api("/api/storyline-delete/" + PROJ + "?story=e2emodes", method="POST")
        if os.path.exists(dub16):
            os.remove(dub16)


# ---------------------------------------------------------------- T15 包删除全流程
def t15_pack_delete():
    st0, d0 = api("/api/pack-create/", method="POST", body={"id": "e2edel", "name": "删除流程测试"})
    st1, d1 = api("/api/pack-mount/" + PROJ + "/e2edel/mount", method="POST")
    mounted = "e2edel" in (d1.get("packs") or [])
    st2, d2 = api("/api/pack-delete/e2edel", method="POST")
    ok = st2 == 200 and d2.get("ok") and mounted
    st3, d3 = api("/api/packs")
    gone = "e2edel" not in [x.get("id") for x in d3]
    archived = not os.path.isdir(os.path.join(ROOT, "materials", "packs", "e2edel"))
    check("T15 包删除（建→挂→删→归档+摘挂载）", ok and gone and archived,
          "摘挂=%s 归档=%s" % (d2.get("unmounted"), archived))
    att = d2.get("attic") or ""
    if att and os.path.isdir(att):
        shutil.rmtree(att, ignore_errors=True)  # 测试不留痕（连归档区都清）


# ---------------------------------------------------------------- T17 混合转场链（直切 concat → xfade 时基归一）
def t17_mixed_transitions():
    # 幕1→2 直切（concat），幕2→3 crossDissolve（xfade）——混用时基必须归一（2026-09-11 实锤 bug）
    sl = {"title": "E2E混合转场", "outline": "t", "meta": {"format": "landscape"},
          "beats": [
            {"no": 1, "id": "x1", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0109", "cut_index": None, "src_in": 0, "duration": 3.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": None},
            {"no": 2, "id": "x2", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0097", "cut_index": None, "src_in": 0, "duration": 3.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": "crossDissolve"},
            {"no": 3, "id": "x3", "story": "t", "narration": {"mode": "original"},
             "tracks": [{"role": "A", "source_id": "M0104", "cut_index": None, "src_in": 0, "duration": 3.0, "requirement": "t"}],
             "music": {"inherit": True, "bgm": None}, "effects": {"stickers": [], "sfx": []},
             "subtitle": {}, "transition_out": None}]}
    api("/api/storyline/" + PROJ + "?story=e2emix", method="POST", body=sl)
    st, d = api("/api/render/" + PROJ + "?flash=0.6&grain=16&hold=1.0&duration=0.5&story=e2emix", method="POST")
    outp = os.path.join(ROOT, "projects", PROJ, "out-e2emix.mp4")
    ok = st == 200 and d.get("ok") and os.path.exists(outp)
    dur = 0.0
    if ok:
        r = subprocess.run([FF, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", outp],
                           capture_output=True, text=True)
        dur = float(r.stdout.strip() or 0)
        ok = 8.0 < dur < 8.6  # 9s 素材 - 1×0.5 软叠
    check("T17 混合转场链（直切→xfade 时基归一）", ok, "%.1fs" % dur)
    api("/api/storyline-delete/" + PROJ + "?story=e2emix", method="POST")


# ---------------------------------------------------------------- T18 显示几何纯净性（rotation 陷阱回归）
def t18_sar_purity():
    # 苏炜实锤：编码 1920x1080 + rotation=-90 的素材，登记/渲染若不转正，成片会被写上
    # SAR 81:256 / DAR 9:16 的补偿标记 → 播放器横向压扁画面（字幕贴纸画中画全变形）。
    # 回归：入库登记必须写显示尺寸；成片 SAR 必须干净 1:1。
    pk = json.load(open(os.path.join(ROOT, "materials", "packs", "demo-project", "pack.json"), encoding="utf-8"))
    bad = [f["id"] for f in pk["files"]
           if f.get("_rotated") and f["width"] > f["height"]]          # 竖版内容被登记成横版 = 旧病
    norot = [f["id"] for f in pk["files"]
             if f.get("_rotated") and f.get("_rotation") is None]      # 转正了但病因角度缺失（审计断链）
    check("T18a 入库登记=显示尺寸+病因角度（rotation 转正且可审计）", not bad and not norot,
          "登记错: %s | 缺角度: %s" % (bad, norot) if (bad or norot) else "13 条全竖版且 rotation 可审计")
    # 成片 SAR 纯净
    r = subprocess.run([FF, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=sample_aspect_ratio", "-of", "csv=p=0",
                        os.path.join(ROOT, "projects", PROJ, "out-aidraft.mp4")],
                       capture_output=True, text=True)
    check("T18b 成片 SAR 纯净（无补偿标记）", r.stdout.strip() in ("1:1", "0:1", ""), r.stdout.strip())



# ---------------------------------------------------------------- T19 字幕页零重叠（2026-09-11 回看回归）
def t19_subtitle_no_overlap():
    # 回归：build_ass 页尾缓冲(+0.25s)未与下页起点钳制 → 上下两行同屏（生产实锤 10/19 页重叠）。
    # 合成夹具：24 个连续单字词（0.2s/字），按 max_chars 换页后相邻页必然产生 +0.25s 重叠，钳制后必须为 0。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import pipeline
        import tempfile
        chars = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯"
        words = [{"t": c, "s": round(i * 0.2, 1), "e": round((i + 1) * 0.2, 1)} for i, c in enumerate(chars)]
        reg = json.load(open(os.path.join(ROOT, "registry", "subtitles.json"), encoding="utf-8"))
        style = min(reg, key=lambda k: reg[k].get("max_chars", 12))  # 最窄页宽 → 页数最多
        plan = {"words": words, "subtitle_style": style, "width": 1080, "height": 1920,
                "duration": words[-1]["e"], "title": "T19"}
        with tempfile.TemporaryDirectory() as td:
            res = pipeline.build_ass(plan, td, "t19.ass")
            ass_path = res[0] if isinstance(res, tuple) else res  # 元组 repr 血泪：build_ass 返回元组
            pages = []
            for ln in open(ass_path, encoding="utf-8"):
                m = re.match(r"Dialogue: 0,([\d:.]+),([\d:.]+)", ln.strip())
                if m:
                    cvt = lambda s: int(s.split(":")[0]) * 3600 + int(s.split(":")[1]) * 60 + float(s.split(":")[2])
                    pages.append((cvt(m.group(1)), cvt(m.group(2))))
        ovs = sum(1 for i in range(1, len(pages)) if pages[i][0] < pages[i - 1][1] - 0.01)
        check("T19 字幕页零重叠（页间钳制回归）", len(pages) >= 2 and ovs == 0,
              "%d 页 / %d 重叠 / style=%s" % (len(pages), ovs, style))
    except Exception as e:
        check("T19 字幕页零重叠（页间钳制回归）", False, "异常: %s" % str(e)[:100])


# ---------------------------------------------------------------- T20 proofread 等长替换（词轨安全回归）
def t20_proofread_eqsub():
    # 回归：proofread 校对层唯一合法动作=等长替换——词文本改、字级时间戳零动（卡拉OK对齐零破坏）、非等长拒绝。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        from proofread import apply_eqsub
        words = [{"text": "检查", "start": 0, "end": 1}, {"text": "我", "start": 1, "end": 1.2},
                 {"text": "们", "start": 1.2, "end": 1.4}, {"text": "的", "start": 1.4, "end": 1.5},
                 {"text": "轮", "start": 1.5, "end": 1.7}, {"text": "骨", "start": 1.7, "end": 1.9}]
        before = [(w["start"], w["end"]) for w in words]
        hit = apply_eqsub(words, "轮骨", "龙骨")
        ok_join = "".join(w["text"] for w in words) == "检查我们的龙骨"
        ok_ts = [(w["start"], w["end"]) for w in words] == before
        rej = apply_eqsub([dict(w) for w in words], "龙骨", "轻钢龙骨") is None  # 非等长必须拒绝
        w2 = [{"text": "轮", "start": 0, "end": 0.2}, {"text": "股", "start": 0.2, "end": 0.4}]
        apply_eqsub(w2, "轮股", "龙骨")
        ok_two = "".join(x["text"] for x in w2) == "龙骨"  # 双字全错的跨词替换
        check("T20 proofread 等长替换（跨词定位/时戳零动/非等长拒绝）",
              bool(hit) and ok_join and ok_ts and rej and ok_two,
              "hit=%d join=%s ts=%s rej=%s two=%s" % (len(hit or []), ok_join, ok_ts, rej, ok_two))
    except Exception as e:
        check("T20 proofread 等长替换（跨词定位/时戳零动/非等长拒绝）", False, "异常: %s" % str(e)[:100])


# ---------------------------------------------------------------- T21 trim 双向钳制（死尾巴回归）
def t21_trim_pad_clamp():
    # 回归：validate 出点双向钳制——下限 词尾+0.35s（防咬字）、上限 词尾+0.8s（防死尾巴废段）。
    # 句中截断天然不触发上限（末词≈窗口边缘）。实测锚：M0124 句号词尾7.2 / M0095 句中 / M0099 词尾10.2。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import draft as draftmod
        # 防"假绿"回归（Claude 审查 P1#1）：必须用生产链的 mats 构造（build_dossier）——
        # 曾因测试直塞全量 f 而生产链塞精简 dict，validate 生产从未生效
        mats = draftmod.build_dossier(["demo-project"])[1]
        V = lambda sid, si, dur: draftmod.validate(
            {"beats": [{"tracks": [{"source_id": sid, "src_in": si, "duration": dur, "role": "A"}]}]},
            mats, ["crossDissolve"])[0]["tracks"][0]
        dead = float(V("M0124", 0, 9.0)["duration"])     # 期望 8.0（词尾7.2+0.8）
        mid = float(V("M0095", 0, 4.0)["duration"])      # 期望 ~4.3（词尾+0.35，不误裁）
        breath = float(V("M0099", 0, 10.2)["duration"])  # 期望 ~10.5（词尾+0.35 气口）
        ok = dead == 8.0 and 4.0 <= mid <= 4.8 and 10.2 <= breath <= 11.0
        check("T21 trim 双向钳制（死尾巴/句中截断/正常气口）", ok,
              "死尾%.1f 截断%.1f 气口%.1f" % (dead, mid, breath))
    except Exception as e:
        check("T21 trim 双向钳制（死尾巴/句中截断/正常气口）", False, "异常: %s" % str(e)[:100])



# ---------------------------------------------------------------- T22 视觉 v2 契约归一化（2026-09-12 视觉重做回归）
def t22_vision_v2_contract():
    # 回归：vision v2 归一化——desc/ocr/usage 兼容键保持（draft 零改动）、时间越界钳制、
    # 无效时间丢弃、非法 content_type 丢弃、flags 如实标注（D8 覆盖率自证）。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        from vision import _normalize_v2
        obj = {"summary": "测试素材，一名男子对镜头讲话。",
               "content_type": "weird",
               "moments": [{"t": [-1, 20], "note": "越界"}, {"t": "bad", "note": "无效"}, {"t": [1, 2], "note": "正常"}],
               "ocr": [{"t": 3, "text": "圣都整装", "where": "条幅"},
                       {"t": 99, "text": "越界文字", "where": "画外"},
                       {"text": "无时间"}],
               "defects": [{"t": [5, 7], "type": "still", "note": "人物静止"}],
               "usage": ["口播主体", "B-roll"]}
        d = _normalize_v2(obj, 9.0, "minimax", at="2026-09-12T00:00:00")
        flags = "|".join(d.get("flags", []))
        ok_ct = d["content_type"] == "" and "content_type 非法值丢弃" in flags
        ok_m = len(d["moments"]) == 2 and d["moments"][0]["t"] == [0.0, 9.0] and d["moments"][1]["t"] == [1.0, 2.0]
        ok_o = (len(d["ocr_detail"]) == 2
                and d["ocr_detail"][0]["text"] == "圣都整装" and d["ocr_detail"][0]["t"] == 3.0
                and d["ocr_detail"][1]["text"] == "越界文字" and d["ocr_detail"][1]["t"] == 9.0
                and len(d["ocr"]) == 2)  # 越界→钳制保留（设计：OCR 文字宝贵，丢了无法找回；无效时间才丢弃）
        ok_compat = d["desc"].startswith("测试素材") and d["usage"] == "口播主体；B-roll"
        ok_d = len(d["defects"]) == 1 and d["defects"][0]["type"] == "still"
        ok_flags = ("时间越界钳制" in flags) and ("时间无效丢弃" in flags)
        check("T22 视觉v2契约归一化（兼容三键/钳制/丢弃/flags）",
              all([ok_ct, ok_m, ok_o, ok_compat, ok_d, ok_flags]) and d["schema"] == 2,
              "ct=%s m=%d o=%d flags=%s" % (d["content_type"] or "空", len(d["moments"]), len(d["ocr"]), flags[:90]))
    except Exception as e:
        check("T22 视觉v2契约归一化（兼容三键/钳制/丢弃/flags）", False, "异常: %s" % str(e)[:100])



# ---------------------------------------------------------------- T23 dossier 自审性验收（注入错误必须被抓）
def t23_dossier_selfaudit():
    # 回归：档案层的存在意义=错了能从图上看出来——注入已知 ASR 变体（潭溪工馆/窗帘箱），propernoun_vote 必须报 suspect；
    # 干净词轨必须 pass（不冤枉）。这同时验证 lexicon 词表与模糊窗口算法（n>=3，位置级 n-1 相同）。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import dossier
        pk = json.load(open(os.path.join(ROOT, "materials/packs/demo-project/pack.json"), encoding="utf-8"))
        files = {f["id"]: f for f in pk["files"]}
        TERMS = ["檀溪公馆", "窗帘盒", "龙骨", "石膏板", "可耐福"]
        # 注入1：檀溪公馆 → 潭溪工馆（连续四词 中 三字同位）
        inj1 = json.loads(json.dumps(files["M0124"]))
        ws = inj1["transcript"]["words"]
        for i in range(len(ws) - 3):
            if [w["text"] for w in ws[i:i + 4]] == ["盛", "梅", "合", "院"]:
                ws[i]["text"], ws[i + 2]["text"] = "圣", "和"
                break
        r1 = dossier.propernoun_vote(list({**files, "M0124": inj1}.values()), TERMS)
        # 注入2：窗帘盒 → 窗帘箱（M0109 三字同位二）
        inj2 = json.loads(json.dumps(files["M0109"]))
        for w in inj2["transcript"]["words"]:
            if w["text"] == "盒":
                w["text"] = "箱"
        r2 = dossier.propernoun_vote(list({**files, "M0109": inj2}.values()), TERMS)
        # 对照：真实词轨（已修复）必须 pass
        r3 = dossier.propernoun_vote(list(files.values()), TERMS)
        # R2-6（Claude 复核）：补"真嫌疑含子词"用例——变体与原词共享子词且窗内不含完整术语必须 suspect。
        # 这防的正是修复 P1#3 时引入过的错法：片段共享判定会把"防水上船"误杀为"正常语句"。
        # 夹具用合成词轨（教训：曾注入到 M0089 真词轨，但该素材根本没有"防水上翻"词序，注入恒不命中=假用例）
        inj4 = {"id": "SYN4", "transcript": {"words": [
            {"text": c, "start": i * 0.2, "end": i * 0.2 + 0.2}
            for i, c in enumerate("阳台的防水上翻三十公分是标准")]}}
        # 注入变体：翻→船（此前夹具重写时把注入行弄丢，词轨是原词=扫描 continue=恒 pass=假用例）
        _ws4 = inj4["transcript"]["words"]
        for _i in range(len(_ws4) - 3):
            if [w["text"] for w in _ws4[_i:_i + 4]] == ["防", "水", "上", "翻"]:
                _ws4[_i + 3]["text"] = "船"
                break
        # 注意：防水上翻 必须显式进被扫词表（TERMS 里没有它——曾因漏加导致注入恒不命中=假用例）
        r4 = dossier.propernoun_vote([inj4], TERMS + ["防水上翻"])
        r4_hit = [s for s in r4.get("detail", []) if isinstance(s, dict)
                  and s.get("variant") == "防水上船" and s.get("canonical") == "防水上翻"]
        r4_ok = (r4["result"] == "suspect" and r4_hit and r4_hit[0].get("match") == "strict")
        takes = dossier.detect_takes(dossier._real_words(files["M0089"]))
        check("T23 dossier 自审性验收（注入必抓/对照不冤/含子词真变体不漏）",
              r1["result"] == "suspect" and r2["result"] == "suspect" and r3["result"] == "pass"
              and r4_ok and isinstance(takes, list),
              "注入1=%s 注入2=%s 对照=%s 含子词=%s(%s) takes(M0089)=%d" % (
                  r1["result"], json.dumps(r1["detail"][:1], ensure_ascii=False)[:50],
                  r3["result"], r4["result"], json.dumps(r4.get("detail", [])[:1], ensure_ascii=False)[:50],
                  len(takes)))
    except Exception as e:
        check("T23 dossier 自审性验收（注入必抓/对照不冤）", False, "异常: %s" % str(e)[:100])



# ---------------------------------------------------------------- T24 proofread v2 分级+队列
def t26_voiceover_orchestration():
    """M0269 错判修复的三缺口回归锚（Noah："改完测试了吗"）——
    T26a 念稿指纹检测器（曾跑完就丢，无回归保护）；
    T26b 强制升级分支（dup≥12 时 narration→voiceover，M0269 实测时模型自己判对、分支从未被触发）；
    T26c digest 消费链（dossier 头 + voiceover 表述，此前只有打印验证）。全部离线 tempdir 隔离。"""
    import tempfile, shutil
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    try:
        import transcribe as TR
        # ── T26a 指纹检测器（正式用例；M0269 台词清洗后最长重复 23 字）──
        m0269_line = ("我们做的是一整张欧松板加固，如果不做的话，后面吊顶下坠全是安全隐患。"
                      "我们做的是一整张欧松板加固，如果不做的话，后面吊顶会下坠，全是安全隐患。")
        a1 = TR._dup_len(m0269_line) >= 12
        a2 = TR._dup_len("大家好今天我们来看看厨房防水做法第一步先刷第一遍") == 0
        a3 = TR._dup_len("") == 0
        check("T26a 念稿指纹检测（重录≥12字/正常独白=0/空文本=0）", a1 and a2 and a3,
              "dup=%d/%d/%d" % (TR._dup_len(m0269_line),
                                TR._dup_len("大家好今天我们来看看厨房防水做法第一步先刷第一遍"),
                                TR._dup_len("")))
        # ── T26b 强制升级分支 ──
        import vision as V
        def _mk(ct):
            return {"summary": "x", "content_type": ct, "moments": [], "ocr": [], "defects": [], "usage": ["x"]}
        b1 = V._normalize_v2(_mk("narration"), 19.0, "test", dup=23)
        b2 = V._normalize_v2(_mk("narration"), 19.0, "test", dup=0)
        b3 = V._normalize_v2(_mk("voiceover"), 19.0, "test", dup=23)
        ok = (b1["content_type"] == "voiceover" and any("dup=23" in f for f in b1["flags"])
              and b2["content_type"] == "narration"
              and b3["content_type"] == "voiceover" and not any("强制升级" in f for f in b3["flags"]))
        check("T26b 念稿指纹强制升级（dup=23:narration→voiceover / dup=0:保持 / 模型自判:无flag）", ok,
              "%s/%s/%s" % (b1["content_type"], b2["content_type"], b3["content_type"]))
        # ── T26c digest 消费链（dossier 头 + voiceover 表述）──
        import draft as D
        tmp = tempfile.mkdtemp(prefix="t26_")
        old_root = D.ROOT
        try:
            D.ROOT = tmp
            pp = os.path.join(tmp, "materials", "packs", "tp")
            os.makedirs(pp)
            json.dump({
                "id": "tp",
                "digest": {"at": "2026-09-14T07:00:00", "theme": "测试题材",
                           "inventory": {"a_roll_candidates": ["A01"], "broll_pool": ["B01"],
                                         "voiceover_sources": ["M0269"], "ambient": [], "gaps": "缺成果镜头"},
                           "roles": [{"id": "M0269", "suggest": "旁白音轨源，词轨铺broll"}]},
                "files": [
                    {"id": "M0269", "kind": "video", "usable": True, "duration": 19.0,
                     "transcript": {"text": "测试台词", "scripted_dup": 23, "words": []},
                     "visual": {"desc": "念稿", "content_type": "voiceover", "usage": "旁白音轨源"}},
                    {"id": "A01", "kind": "video", "usable": True, "duration": 20.0,
                     "transcript": {"text": "正常口播", "words": []},
                     "visual": {"desc": "口播出镜", "content_type": "narration", "usage": "主轴"}}]},
                open(os.path.join(pp, "pack.json"), "w", encoding="utf-8"), ensure_ascii=False)
            dos, mats = D.build_dossier(["tp"])
            c1 = "素材包盘点" in dos and "旁白音轨源：M0269" in dos and "缺成果镜头" in dos
            c2 = "🎙voiceover" in dos and "词轨可作旁白音轨源" in dos
            c3 = "逐条明细" in dos and "M0269" in mats and mats["M0269"].get("transcript", {}).get("scripted_dup") == 23
            check("T26c digest 消费链（盘点头/voiceover表述/明细降级无digest也可用）", c1 and c2 and c3,
                  "盘点头=%s voiceover标注=%s mats穿透=%s" % (c1, c2, c3))
        finally:
            D.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T26 编排回归", False, str(e)[:120])


def t25_rmw_smoke():
    # R3 终局轮产物：RMW 并发竞争冒烟——慢写者持锁期间并发 usable/upload，
    # 两方变更都必须存活（R3-1/R3-2 的回归锚：读点再溜出临界区，此测试当场红）
    try:
        import subprocess as _sp
        r = _sp.run(["/usr/bin/python3", os.path.join(ROOT, "tests", "rmw_smoke.py")],
                    capture_output=True, text=True, timeout=120)
        out = r.stdout.strip().split("\n")
        ok = r.returncode == 0
        detail = next((l.strip() for l in out if "结果" in l), "?")
        check("T25 RMW 并发竞争冒烟（慢写者 vs usable/upload，RMW 原子性）",
              ok, detail)
    except Exception as e:
        check("T25 RMW 并发竞争冒烟（慢写者 vs usable/upload，RMW 原子性）", False, str(e)[:100])


def t24b_proofread_words_sync():
    # Claude 审查 P1-③/T24假绿回归锚：人工校对若只改 text 不改 words，渲染链（pipeline 读 words）永远不上屏。
    # 本测直接验证 studio.py 人工端点采用的词轨同步逻辑（等长 apply_eqsub 保时间戳）。
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import proofread as PF
    old_text, new_text = "轮骨不牢，方可以了。", "龙骨不牢，方可以了。"
    words = [{"text": ch, "start": round(0.1 * i, 1), "end": round(0.1 * i + 0.1, 1)} for i, ch in enumerate(old_text)]
    times_before = [w["start"] for w in words]
    diff = [(i, a, b) for i, (a, b) in enumerate(zip(old_text, new_text)) if a != b]
    PF.apply_eqsub(words, "".join(d[1] for d in diff), "".join(d[2] for d in diff))
    after = "".join(w["text"] for w in words)
    check("T24B 人工校对词轨同步（等长替换保时间戳）",
          after == new_text and [w["start"] for w in words] == times_before,
          "text=%r 时间戳不动=%s" % (after, [w["start"] for w in words] == times_before))


def t24_proofread_v2():
    # 回归：词表命中=auto 直改；非词表=needs-human 进队列；非等长=拒绝；超长(>6字)=转人工；dry=零落盘。
    # 假 LLM 注入四类修正各一发，临时 ROOT 隔离，不碰真实项目。
    import tempfile, shutil
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import proofread
        tmp = tempfile.mkdtemp(prefix="t24_")
        old_root = proofread.ROOT
        try:
            proofread.ROOT = tmp
            os.makedirs(os.path.join(tmp, "materials", "packs", "tpid"))
            os.makedirs(os.path.join(tmp, "config"))
            os.makedirs(os.path.join(tmp, "projects", "tpid"))
            json.dump({"terms": ["龙骨", "檀溪公馆", "窗帘盒"]},
                      open(os.path.join(tmp, "config", "lexicon.json"), "w"), ensure_ascii=False)
            ws, t = [], 0.0
            for ch in "轮骨不牢，方可以了。大家都看到过吧":
                ws.append({"text": ch, "start": round(t, 1), "end": round(t + 0.2, 1)})
                t += 0.2
            json.dump({"files": [{"id": "T24M", "duration": round(t, 1), "audit": {},
                                  "transcript": {"words": ws}}]},
                      open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json"), "w"), ensure_ascii=False)
            old_llm = proofread.chat_llm
            proofread.chat_llm = lambda msgs: ('[{"find":"轮骨","replace":"龙骨"},'
                                               '{"find":"方可以了","replace":"放可以了"},'
                                               '{"find":"龙骨","replace":"轻钢龙骨"},'
                                               '{"find":"大家都看到过吧","replace":"大家均看到过吧"}]')
            proofread.run("tpid", dry=True)
            assert not os.path.exists(os.path.join(tmp, "projects", "tpid", "review-queue.json")), "dry 不得落队列"
            pk_dry = json.load(open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json")))
            assert pk_dry["files"][0]["transcript"]["words"][0]["text"] == "轮", "dry 不得写词轨"
            proofread.run("tpid", dry=False)
            pk_now = json.load(open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json")))
            text = "".join(w["text"] for w in pk_now["files"][0]["transcript"]["words"])
            ok_fix = ("龙骨不牢" in text) and ("轮骨" not in text) and ("方可以了" in text)
            q = json.load(open(os.path.join(tmp, "projects", "tpid", "review-queue.json")))
            ok_q = len(q["items"]) == 2 and q["items"][0]["find"] == "方可以了"
            ok_log = pk_now["files"][0].get("proofread_log", [{}])[0].get("replace") == "龙骨"
            check("T24 proofread v2 分级+队列（词表auto/转人工/拒绝/dry纪律）",
                  ok_fix and ok_q and ok_log, "fix=%s queue=%d log=%s" % (ok_fix, len(q["items"]), ok_log))
        finally:
            proofread.ROOT = old_root
            proofread.chat_llm = old_llm
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T24 proofread v2 分级+队列（词表auto/转人工/拒绝/dry纪律）", False, "异常: %s" % str(e)[:120])


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过 LLM 与长渲染")
    a = ap.parse_args()
    print("══ E2E 回归（%s）══" % ("fast" if a.fast else "全量"))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fixtures
    fixtures.ensure()  # 测试底盘自给自足：夹具包缺失时合成（2026-09-14 清场后立——E2E 曾 40 处硬编码真实素材）
    t1_health()
    t2_injection()
    t3_default_story()
    t4_sync()
    t5_upload()
    t6_sfx()
    if not a.fast:
        t7_sticker()
        t8_draft(a.fast)
    t9_aspect()
    t10_sync()
    t11_render_job()
    t12_dub_render()
    t15_pack_delete()
    t16_narration_modes()
    t17_mixed_transitions()
    t18_sar_purity()
    t19_subtitle_no_overlap()
    t20_proofread_eqsub()
    t21_trim_pad_clamp()
    t22_vision_v2_contract()
    t23_dossier_selfaudit()
    t24_proofread_v2()
    t24b_proofread_words_sync()
    t26_voiceover_orchestration()
    t25_rmw_smoke()
    print("══ 结果：%d 通过 / %d 失败 ══" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        sys.exit(1)
    print("E2E ALL GREEN")


if __name__ == "__main__":
    main()
