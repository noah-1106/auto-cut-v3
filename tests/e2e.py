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
  T39 dossier LLM digest 契约（digest 落盘 pack.json / 空包抛错不白调 LLM）
  T40 幕样张渲染（render_beat 逐幕独立验证：preview 产物存在且时长 >0）
  T41 注册表读写闭环（save 回读 / enums 拒写 / delete 幂等 / upload 入库+落盘）
  T42 编排器故事线门（audit+review+digest 齐后 --advance 仍停在故事线门）
  T43 proofread 人工复核门（review-queue 有 pending=环节 pending，复核后放行）
  T44 vadwords 时间源双锚（合成词时间禁作锚→VAD 铺轨；T44b tier=word 实测词时间直取，2026-09-18 勘误）
  T45 content_type 强制门（meta/voiceover 禁A、说话画面禁B、空镜两级：无语音分层禁/纯空镜幕 mode=none 放行/旁白空镜【broll+语音】放行）
  T46 loudnorm 渲染链（默认开/可关，实测 integrated≈-16 LUFS，2026-09-15）
  T47 proofread 严格判定（audit=pending≠done，只有 done/auto-done 算过，2026-09-16 n006 复发实锤）
  T48 validate 不截断幕数（6 幕全留，第 5 幕起同样吃钳制，2026-09-16 Noah 实锤 [:4] 静默丢弃 bug）
  T49 proofread 队列落点=项目目录（包名≠项目名也不同步错位，2026-09-16 n004/n006/wangyalun 三连发修根）
  T50 VLM 转码预设阶梯（ultrafast 首选，超限回退 veryfast 再抛错，2026-09-17 批量素材提速）
  T51 transcribe 素材级并发（3 线程真并行峰值≥3 + 写回全量，2026-09-18 实测 3+3 并发无 429 后落地）
  T52 retry429 退避（429 线性退避重试，非 429 直接抛，2026-09-18 并发配套）
  T53 贴纸文字模板（drawtext 现画+缓存+default 回退；validate ≤6字硬门，v1 规矩代码化）

用法：python3 tests/e2e.py [--fast]   # --fast 跳过 LLM 与长渲染
"""
import argparse, json, os, re, shutil, subprocess, sys, time, urllib.request, urllib.error  # re: T16 volumedetect 解析

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool(name):
    """跨平台工具解析：仓内 bin/（mac 无后缀/win .exe）→ PATH（硬规则 8 同链）。"""
    for c in (os.path.join(ROOT, "bin", name), os.path.join(ROOT, "bin", name + ".exe")):
        if os.path.exists(c):
            return c
    import shutil
    return shutil.which(name) or name


FF = _tool("ffprobe")     # 历史变量名=ffprobe（T12/T16 探时长用）
FFMPEG = _tool("ffmpeg")
BASE = os.environ.get("E2E_BASE", "http://localhost:8765")  # 独立 studio 回归：E2E_BASE=http://localhost:8799（2026-09-18 并行互踩实锤，studio 支持端口参数）
import fixtures  # PID 进程唯一（并行 e2e 各持各的包/项目——T17 假红根治，2026-09-18）
PROJ = fixtures.PID
PASS, FAIL = [], []


def api(path, method="GET", body=None, raw=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None),
                                 headers={"Content-Type": "application/json", "X-AutoCut": "1"})  # X-AutoCut=CSRF 闸硬契约（2026-10-23）
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
    api("/api/pack-create/", method="POST", body={"id": "e2e-upload", "name": "E2E 上传夹具"})  # 幂等：已存在 409 忽略
    # 上传源：夹具 ROT01（display_matrix=-90 合成件）——曾引用已删的真实素材 M0085，
    # ffmpeg 裁剪静默失败后用 /tmp 残留上传=假绿（2026-09-14 根治：源必须出自当次夹具）
    src_rot = os.path.join(ROOT, "materials", "packs", PROJ, "ROT01.MP4")
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".mp4"); os.close(fd)
    rc = subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                         "-ss", "0", "-t", "1.5",
                         "-i", src_rot, "-c:v", "copy", "-an", tmp],
                        capture_output=True, text=True)
    if rc.returncode != 0 or not os.path.exists(tmp):
        check("T5 上传素材（入库/元数据/缩略帧/审计队列/rotation 转正）", False,
              ("夹具源缺失/裁剪失败：%s" % (rc.stderr or "no src"))[-90:])
        return
    raw = open(tmp, "rb").read()
    st, d = api("/api/pack-upload/e2e-upload?filename=e2e_test_src.mp4", method="POST", raw=raw)
    entry = d.get("entry") or {}
    ok = (st == 200 and entry.get("kind") == "video"
          and abs((entry.get("duration") or 0) - 1.5) < 0.5  # 裁剪 1.5s（与上方 -t 1.5 一致；曾写 2.0 撞边界=0.5<0.5 False）
          and bool(entry.get("thumb"))
          and ((entry.get("audit") or {}).get("transcript") == "pending"))
    # rotation 必检（2026-09-11）：登记宽高必须是转正后的显示尺寸，病因角度必须落盘
    _rot = entry.get("_rotation")
    geo_ok = (entry.get("width"), entry.get("height")) == (1080, 1920) and \
        _rot is not None and abs(abs(_rot) - 90) < 0.001  # 夹具注入 +90（tkhd matrix 符号约定），等价旋转均可
    check("T5 上传素材（入库/元数据/缩略帧/审计队列/rotation 转正）", ok and geo_ok,
          "id=%s dur=%s geo=%sx%s rot=%s" % (entry.get("id"), entry.get("duration"),
                                             entry.get("width"), entry.get("height"), entry.get("_rotation")))
    # 重名保护
    st2, d2 = api("/api/pack-upload/e2e-upload?filename=e2e_test_src.mp4", method="POST", raw=raw)
    fn2 = (d2.get("entry") or {}).get("file", "")
    check("T5 重名不覆盖", st2 == 200 and fn2 != "e2e_test_src.mp4")
    # 非法文件名拒绝（小载荷探针：服务器在拒绝路径不读请求体，大 body 会让客户端 BrokenPipe）
    st3, _ = api("/api/pack-upload/e2e-upload?filename=..%2Fx.mp4", method="POST", raw=b"\x00" * 1024)
    check("T5 恶意文件名拒绝", st3 == 400)
    # 清理：把测试素材从包里移除（文件一并删）
    pk_path = os.path.join(ROOT, "materials", "packs", "e2e-upload", "pack.json")
    pk = json.load(open(pk_path, encoding="utf-8"))
    pk["files"] = [f for f in pk["files"] if not str(f.get("file", "")).startswith("e2e_test_src")]
    json.dump(pk, open(pk_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    updir = os.path.join(ROOT, "materials", "packs", "e2e-upload")
    for f in os.listdir(updir):
        if f.startswith("e2e_test_src"):
            os.remove(os.path.join(updir, f))
    if not (json.load(open(os.path.join(updir, "pack.json"), encoding="utf-8")).get("files") or []):
        import shutil as _sh
        _sh.rmtree(updir, ignore_errors=True)  # 测试包不留壳（曾只删素材文件，目录残留=磁盘脏）


# ---------------------------------------------------------------- T6 音效混音
def t6_sfx():
    aid = json.load(open(os.path.join(ROOT, "materials", "packs", PROJ, "pack.json"), encoding="utf-8"))
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
            r = subprocess.run([FFMPEG, "-hide_banner", "-ss", str(t0), "-t", str(dur),
                                "-i", outp, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True)
            mm = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
            return float(mm.group(1)) if mm else -99
        on, base = vmax(1.0), vmax(4.0)
        ding = -14.0  # ding.wav 素材自身 max
        ok = (on - base) > 3 or abs(on - ding) < 3  # 抬升显著 或 贴着素材原响度
        check("T6 音效混音", ok, "ding窗 %.1fdB / 基线窗 %.1fdB" % (on, base))
    else:
        check("T6 音效混音", False, str(d))  # 全量 log 不截断：CI 排障要完整 ffmpeg 错误（Windows 截断曾致盲排）
    api("/api/storyline-delete/" + PROJ + "?story=e2esfx", method="POST")


# ---------------------------------------------------------------- T7 贴纸渲染
def t7_sticker():
    # 2026-09-19 策展解耦：不再硬编码贴纸 id（Noah 删 zhuyi/tuijian 后 CI 双端红）——
    # 动态取首个「顶区 + 带默认短语」的在册文字模板贴纸，注册表怎么删测都不空转
    _reg = json.load(open(os.path.join(ROOT, "registry", "stickers.json"), encoding="utf-8"))
    sid = next(k for k, v in _reg.items() if not k.startswith("_")
               and v.get("text_style") and v.get("default_text")
               and "top" in (v.get("pos") or ""))
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
    stick["beats"][0]["effects"]["stickers"] = [{"asset": sid, "duration": 1.2}]
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
            subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                            "-ss", str(t), "-i", mp, "-frames:v", "1", png], capture_output=True)
            return png
        import tempfile
        _snapdir = tempfile.mkdtemp(prefix="e2e_snap_")
        def cross(t):
            a = snap(outs[1], t, os.path.join(_snapdir, "e2e_a.png"))
            b = snap(outs[0], t, os.path.join(_snapdir, "e2e_b.png"))
            r = subprocess.run([FFMPEG, "-hide_banner",
                                "-i", a, "-i", b,
                                "-filter_complex", "[0:v]crop=900:320:90:30[x];[1:v]crop=900:320:90:30[y];"
                                                   "[x][y]blend=all_mode=difference,signalstats,metadata=print:file=-",
                                "-f", "null", "-"], capture_output=True, text=True)
            return float((re.search(r"YAVG=([\d.]+)", r.stdout or r.stderr) or [None, 0])[1])
        on = cross(0.8)   # 贴纸 ON（fallback 0.4s 起 1.2s 窗）
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
        r = subprocess.run([FF, "-v", "error",
                            "-show_entries", "format=duration", "-of", "csv=p=0", out],
                           capture_output=True, text=True)
        dur = float(r.stdout.strip() or 0)
    # 2026-09-14 dub 盖满语义（冷启动修复）：配音 > 幕视频时长 → A 轨延展盖满配音，
    # 成片 ≈ 配音实测长 + 幕2 4s——不再 atrim 掐断句子（旧断言 8s=掐断语义，已废）
    rd = subprocess.run([FF, "-v", "error",
                         "-show_entries", "format=duration", "-of", "csv=p=0", dub_src],
                        capture_output=True, text=True)
    dub_dur = float(rd.stdout.strip() or 0)
    check("T12 dub 渲染（幕1配音延展盖满+幕2原声混合）",
          ok and abs(dur - (dub_dur + 4.0)) < 1.5, "%.1fs（配音%.1fs+4s）" % (dur, dub_dur))
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
            check("T16 narration 三态", False, json.dumps(d, ensure_ascii=False))
            return

        def seg_mv(ss, tt):
            r = subprocess.run([FFMPEG, "-ss", str(ss), "-t", str(tt),
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
        # 转场预留（T59 语义）：dt_eff 随边缘静音伸缩（0.2~注册值），本夹具实测钳到 0.2 → 8.8s。
        # 上界 9.0 严格排除：降级直切=9.0 必须红（那是夹具漂移/预留逻辑失效的信号）
        ok = 8.0 < dur < 9.0  # 9s 素材 − 1×dt_eff（软叠仍在，时基归一目的不变）
    check("T17 混合转场链（直切→xfade 时基归一）", ok, "%.1fs" % dur)
    api("/api/storyline-delete/" + PROJ + "?story=e2emix", method="POST")


# ---------------------------------------------------------------- T18 显示几何纯净性（rotation 陷阱回归）
def t18_sar_purity():
    # 苏炜实锤：编码 1920x1080 + rotation=-90 的素材，登记/渲染若不转正，成片会被写上
    # SAR 81:256 / DAR 9:16 的补偿标记 → 播放器横向压扁画面（字幕贴纸画中画全变形）。
    # 回归：入库登记必须写显示尺寸；成片 SAR 必须干净 1:1。
    pk = json.load(open(os.path.join(ROOT, "materials", "packs", PROJ, "pack.json"), encoding="utf-8"))
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
        mats = draftmod.build_dossier([PROJ])[1]
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
        pk = json.load(open(os.path.join(ROOT, "materials", "packs", PROJ, "pack.json"), encoding="utf-8"))
        files = {f["id"]: f for f in pk["files"]}
        TERMS = ["檀溪公馆", "窗帘盒", "龙骨", "石膏板", "可耐福"]
        # 注入1：檀溪公馆 → 潭溪工馆（连续四词 中 三字同位）
        inj1 = json.loads(json.dumps(files["M0124"]))
        ws = inj1["transcript"]["words"]
        for i in range(len(ws) - 3):
            if [w["text"] for w in ws[i:i + 4]] == ["檀", "溪", "公", "馆"]:
                ws[i]["text"], ws[i + 2]["text"] = "潭", "工"
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
    """M0269 错判修复的三缺口回归锚（维护者："改完测试了吗"）——
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
                           "roles": [{"id": "M0269", "suggest": "旁白音轨源，词轨铺broll"}],
                           "narrative_assets": "老房翻新避坑"},
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
            c2 = "🎙voiceover" in dos and "词轨=旁白音轨源" in dos and "validate 硬剔除" in dos
            c3 = "逐条明细" in dos and "M0269" in mats and mats["M0269"].get("transcript", {}).get("scripted_dup") == 23
            c4 = "叙事支撑：老房翻新避坑" in dos  # narrative_assets 消费（2026-10-22 接通，此前 LLM 产出零读取）
            check("T26c digest 消费链（盘点头/voiceover表述/明细降级无digest也可用/narrative_assets）", c1 and c2 and c3 and c4,
                  "盘点头=%s voiceover标注=%s mats穿透=%s 叙事支撑=%s" % (c1, c2, c3, c4))
        finally:
            D.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T26 编排回归", False, str(e)[:120])


# ---------------------------------------------------------------- T27 管线编排器（M1）
def t27_orchestrator():
    # 回归：orchestrate.status 文件态推导（与 /api/status/CLI 同源）——
    # ①结构完整（13 环节+合法状态）②全链产物齐备的合成树必须全 done/na 且无 next
    # ③空项目的下一动作=mount ④advance 不越创作门（mount 是创作决策，全自动只到门）
    # 注：全绿断言用补丁 ROOT 的合成文件树——live 项目在套件中途被各用例改故事线/渲染，
    #     freshness（out 新于故事线）必然被破坏，断言 live 全绿=测试设计缺陷（2026-09-14 实锤）
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import orchestrate as ORCH
        ok_struct = ok_done = ok_gate = False
        old_root = ORCH.ROOT
        tmp = tempfile.mkdtemp(prefix="t27_")
        try:
            ORCH.ROOT = tmp
            pk = os.path.join(tmp, "materials", "packs", "p1")
            os.makedirs(pk)
            words = [{"text": "测", "start": 0.0, "end": 0.3}]
            json.dump({"files": [{"id": "M1", "kind": "video", "audit": {"transcript": "done", "visual": "done", "proofread": "auto-done"},
                                  "transcript": {"words": words}}], "digest": {"theme": "t"}},
                      open(os.path.join(pk, "pack.json"), "w", encoding="utf-8"), ensure_ascii=False)
            pd = os.path.join(tmp, "projects", "full")
            os.makedirs(os.path.join(pd, "materials"))
            os.makedirs(os.path.join(pd, "storylines"))
            os.makedirs(os.path.join(pd, "cover"))
            json.dump({"packs": ["p1"]}, open(os.path.join(pd, "materials", "library.json"), "w"))
            json.dump({}, open(os.path.join(pd, "project.json"), "w"))
            json.dump({}, open(os.path.join(pd, "dossier.json"), "w"))
            json.dump({"materials": {}, "summary": {}}, open(os.path.join(pd, "disposition.json"), "w"))
            json.dump({"meta": {"cover": {"strategy": "first-frame"}},
                       "beats": [{"no": 1, "narration": {"mode": "original", "words": words}}]},
                      open(os.path.join(pd, "storylines", "s.json"), "w", encoding="utf-8"), ensure_ascii=False)
            json.dump({"verdict": "pass"}, open(os.path.join(pd, "qc-report.json"), "w"))
            open(os.path.join(pd, "out-s.mp4"), "wb").write(b"x")  # mtime 最新=out 新于故事线
            # 封面夹具：竖版非空白帧（cover 环节体检判据——2026-09-18 封面前置收口）
            rc = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                                 "-i", "testsrc=size=1080x1920:rate=10:duration=1", "-frames:v", "1",
                                 "-q:v", "2", os.path.join(pd, "cover", "cover-s.jpg")],
                                capture_output=True, text=True)
            assert rc.returncode == 0, "封面夹具合成失败"
            # 显式钉 mtime 而非依赖"先写后写"：同 tick 内两者 mtime 可相等（Windows CI
            # 2026-09-15 实锤 T27 偶发 done=False——render  freshness 判据是严格大于）
            t0 = time.time()
            os.utime(os.path.join(pd, "storylines", "s.json"), (t0, t0))
            os.utime(os.path.join(pd, "cover", "cover-s.jpg"), (t0 + 1, t0 + 1))
            os.utime(os.path.join(pd, "out-s.mp4"), (t0 + 2, t0 + 2))
            st = ORCH.status(pd)
            ok_struct = (st["project"] == "full" and len(st["steps"]) == 15
                         and all(s["status"] in ("done", "pending", "failed", "na") for s in st["steps"]))
            ok_done = all(s["status"] in ("done", "na") for s in st["steps"]) and st["next"] is None
        finally:
            ORCH.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
        api("/api/project-create/", method="POST", body={"name": "e2eorch", "title": "编排测试", "format": "vertical"})
        try:
            st2 = ORCH.status("e2eorch")
            st2_after = ORCH.advance("e2eorch")  # mount 是创作决策门——advance 必须停在这里（无副作用）
            ok_gate = (st2["next"] == "mount" and st2["steps"][0]["status"] == "pending"
                       and st2_after["next"] == "mount")
        finally:
            api("/api/project-delete/e2eorch", method="POST")
        check("T27 管线编排器（状态推导/全链 done/mount 创作门）",
              ok_struct and ok_done and ok_gate,
              "struct=%s done=%s gate=%s" % (ok_struct, ok_done, ok_gate))
    except Exception as e:
        check("T27 管线编排器（状态推导/全链 done/mount 创作门）", False, "异常: %s" % str(e)[:120])


# ---------------------------------------------------------------- T28 缺陷台账（M2）
def t28_disposition():
    # 回归：disposition 聚合器四类缺陷（重说/念稿指纹/视觉缺陷/黑区）注入必抓必留痕，
    # 且 draft 提示词必须消费台账（识别层的问题起草层听得见——治"假环节"）。
    # 黑区用合成 wav：0-2s 静音 + 2-3s 正弦（VAD 物理测量），词轨只覆盖 0-0.5s → 2-3s 无词覆盖=黑区。
    import tempfile, shutil
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import disposition as DSP
        import draft as D
        tmp = tempfile.mkdtemp(prefix="t28_")
        old_root, old_droot = DSP.ROOT, D.ROOT
        try:
            DSP.ROOT = D.ROOT = tmp
            pk = os.path.join(tmp, "materials", "packs", "tp")
            os.makedirs(os.path.join(pk, "thumbs"))
            os.makedirs(os.path.join(tmp, "materials", ".audio_cache"))
            os.makedirs(os.path.join(tmp, "projects", "tp", "materials"))
            os.makedirs(os.path.join(tmp, "registry"))
            json.dump({}, open(os.path.join(tmp, "registry", "transitions.json"), "w"))
            json.dump({"packs": ["tp"]}, open(os.path.join(tmp, "projects", "tp", "materials", "library.json"), "w"))
            # 词轨：0-0.5s 六个词（含 3-gram 重说）+ 念稿指纹 + 视觉缺陷
            ws = [{"text": c, "start": round(i * 0.08, 2), "end": round(i * 0.08 + 0.08, 2)}
                  for i, c in enumerate("甲乙丙甲乙丙")]
            json.dump({"id": "tp", "files": [{"id": "TPM", "file": "tp.mp4", "kind": "video",
                     "duration": 3.0, "usable": True,
                     "transcript": {"text": "甲乙丙甲乙丙", "scripted_dup": 23, "words": ws},
                     "visual": {"desc": "t", "content_type": "narration",
                                "defects": [{"t": [0.2, 0.6], "type": "blur", "note": "手抖"}]}}]},
                      open(os.path.join(pk, "pack.json"), "w"), ensure_ascii=False)
            # 合成 wav：0-2s 静音，2-3s 正弦——对应黑区 [2,3]
            wav = os.path.join(tmp, "materials", ".audio_cache", "x_tp.wav")
            # 缓存路径规则 = md5(素材所在目录)[:8]_<basename>——素材在 packs/tp 下，直接复用 _wav_cache 生成
            import hashlib
            dh = hashlib.md5(pk.encode("utf-8")).hexdigest()[:8]
            wav = os.path.join(tmp, "materials", ".audio_cache", dh + "_tp.wav")
            r = subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                                "-f", "lavfi", "-i", "aevalsrc='if(gte(t,2),sin(440*t),0)':s=16000:d=3",
                                "-ac", "1", wav], capture_output=True, text=True)
            assert r.returncode == 0 and os.path.exists(wav), "黑区夹具 wav 合成失败"
            rep = DSP.build("tp")
            m = rep["materials"]["TPM"]
            types = [it["type"] for it in m["items"]]
            ok_detect = (m["verdict"] == "avoid"
                         and "retake-suspect" in types and "scripted-dup" in types
                         and "visual-blur" in types
                         and any(it["type"] == "black-zone" and it["t"] == [2.0, 3.0] for it in m["items"]))
            ok_report = rep["summary"]["avoid"] == 1 and os.path.exists(os.path.join(tmp, "projects", "tp", "disposition.json"))
            # 消费链：draft 提示词必须含台账（假 LLM 捕获 messages 断言）
            cap = {}
            old_llm = D.chat_llm
            def fake_llm(msgs):
                cap["content"] = msgs[0]["content"]
                return json.dumps({"title": "t", "outline": "o", "audio": {},
                                   "beats": [{"story": "s", "narration": {"mode": "original"},
                                              "tracks": [{"role": "A", "source_id": "TPM", "src_in": 0, "duration": 2}],
                                              "transition_out": None}]}, ensure_ascii=False)
            D.chat_llm = fake_llm
            try:
                sid, _d, beats = D.run("tp", "测试意图", save=False)
            finally:
                D.chat_llm = old_llm
            ok_prompt = ("缺陷台账" in cap.get("content", "") and "TPM" in cap["content"]
                         and "retake-suspect" in cap["content"])
            ok_run = bool(sid is None and beats)  # save=False → sid None，beats 有效
            check("T28 缺陷台账（四类注入必抓/留痕/起草消费）",
                  ok_detect and ok_report and ok_prompt and ok_run,
                  "types=%s prompt含台账=%s" % (types, ok_prompt))
        finally:
            DSP.ROOT, D.ROOT = old_root, old_droot
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T28 缺陷台账（四类注入必抓/留痕/起草消费）", False, "异常: %s" % str(e)[:140])


# ---------------------------------------------------------------- T29 QC 听觉/冻结/页同步（M3）
def t29_qc_av_sync():
    # 回归：R7 静音洞+音量骤变（silencedetect/volumedetect 物理探针）、R4 冻结帧
    # （含 EOF 截断冻结补到片尾）、R10 页同步（plan 词轨重放 vs ASS，注入偏移必报）。
    # 全合成媒体，离线。探针判据曾被"输入类型错（dict 喂给 build_pages）"整段打折——锚必须打真输入。
    import tempfile, shutil
    FF = FFMPEG
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import qc
        rules = qc.load_rules()
        tmp = tempfile.mkdtemp(prefix="t29_")
        try:
            def synth(name, args):
                p = os.path.join(tmp, name)
                r = subprocess.run([FF, "-y", "-loglevel", "error"] + args + [p],
                                   capture_output=True, text=True)
                assert r.returncode == 0 and os.path.exists(p), "夹具合成失败: %s" % name
                return p
            hole = synth("hole.mp4", ["-f", "lavfi", "-i",
                          "aevalsrc='if(lt(t,1),sin(440*t),if(lt(t,3.5),0,sin(440*t)))':s=16000:d=6", "-c:a", "aac"])
            jump = synth("jump.mp4", ["-f", "lavfi", "-i",
                          "aevalsrc='if(lt(t,5),0.08*sin(440*t),sin(440*t))':s=16000:d=10", "-c:a", "aac"])
            freeze = synth("freeze.mp4", ["-f", "lavfi", "-i", "color=c=red:s=320x240:r=30:d=4",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest"])
            v = qc.check_audio_output(hole, rules)
            ok_hole = any(x["evidence"].get("holes") and x["evidence"]["holes"][0] == [1.0, 3.5] for x in v)
            v2 = qc.check_audio_output(jump, rules)
            ok_jump = any(x["evidence"].get("jumps") for x in v2)
            v4 = qc.check_freeze(freeze, rules)
            ok_freeze = v4[0]["evidence"].get("freezes") == [[0.0, 4.0]]  # EOF 截断补到片尾
            # R10：24 字词轨重放分页；ASS 整体 +0.8s → 中位偏移超阈必报；对齐版必过
            sys.path.insert(0, os.path.join(ROOT, "autocut3"))
            import pipeline
            chars = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯"
            words = [{"t": c, "s": round(i * 0.2, 1), "e": round(i * 0.2 + 0.2, 1)}
                     for i, c in enumerate(chars)]
            pages = pipeline.build_pages([(w["t"], w["s"], w["e"]) for w in words], 12)
            starts = [pg[0][1] for pg in pages]
            pp = os.path.join(tmp, "proj")
            os.makedirs(pp)
            json.dump({"subtitle_style": "karaoke-gold", "meta": {"style": {}}, "words": words},
                      open(os.path.join(pp, "plan-t.json"), "w"), ensure_ascii=False)
            def ass_with(shift):
                def ts(s):
                    cs = int(round(s * 100))
                    return "%d:%02d:%02d.%02d" % (cs // 360000, cs % 360000 // 6000, cs % 6000 // 100, cs % 100)
                lines = ["[Events]", "Format: Start, End, Text"]
                for st_ in starts:
                    lines.append("Dialogue: 0,%s,%s,Karaoke,,0,0,0,,x" % (ts(st_ + shift), ts(st_ + shift + 1)))
                return "\n".join(lines)
            open(os.path.join(pp, "subtitle-t.ass"), "w", encoding="utf-8").write(ass_with(0.8))
            bad = qc.check_page_sync(pp, "t", rules)
            open(os.path.join(pp, "subtitle-t.ass"), "w", encoding="utf-8").write(ass_with(0.0))
            good = qc.check_page_sync(pp, "t", rules)
            ok_r10 = (bad[0]["severity"] == "warn" and abs(bad[0]["evidence"].get("median_offset", 0) - 0.8) < 0.05
                      and good[0]["severity"] == "info")
            # R10 多字单元锚（2026-09-18 word-tier 勘误配套）：tier=word 词轨含多字单元
            #（「售，」「年。」标点合并）——R10 重放曾裸用 plan words 而 build_ass 走
            # char_level 预处理，两侧断页分叉→假 warn（ruxuan-02 aidraft 实锤中位 -2.5s）。
            # 锚：多字词轨经真 build_ass 出 ASS，R10 复检必须 info。
            mc_words = [{"t": "做装修", "s": 0.18, "e": 0.86}, {"t": "销售，", "s": 0.86, "e": 1.30},
                        {"t": "我从不拿低价吸引客户", "s": 1.30, "e": 4.00},
                        {"t": "，", "s": 4.00, "e": 4.10}, {"t": "我只拿结果说话。", "s": 4.10, "e": 6.90},
                        {"t": "我是如轩，", "s": 7.00, "e": 8.60}, {"t": "做装修六年。", "s": 8.60, "e": 10.40}]
            mc_plan = {"subtitle_style": "karaoke-gold", "meta": {"style": {}}, "words": mc_words,
                       "width": 1080, "height": 1920, "duration": 11.0}
            json.dump(mc_plan, open(os.path.join(pp, "plan-m.json"), "w"), ensure_ascii=False)
            _ass, _np = pipeline.build_ass(mc_plan, pp, "subtitle-m.ass")
            mc = qc.check_page_sync(pp, "m", rules)
            ok_mc = (mc[0]["severity"] == "info" and _np >= 2)
            check("T29 QC 听觉/冻结/页同步（洞/骤变/EOF冻结/页偏移双向+多字单元重放对齐）",
                  ok_hole and ok_jump and ok_freeze and ok_r10 and ok_mc,
                  "hole=%s jump=%s freeze=%s r10=%s mc=%s(%s页)" % (ok_hole, ok_jump, ok_freeze, ok_r10, ok_mc, _np))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T29 QC 听觉/冻结/页同步（洞/骤变/EOF冻结/页偏移双向+多字单元重放对齐）", False, "异常: %s" % str(e)[:140])


# ---------------------------------------------------------------- T30 声纹管理双通道（Studio API 回归锚）
def t30_voice_api():
    # 人工通道（Studio 界面）四端点契约：voices 结构 / 注册缺逐字稿必拒（零样本硬契约）/
    # 坏名必拒 / 删不存在 404。AI 通道=tts.py voiceclone，同一注册实现（tts_audio8.register_voice）。
    st, r = api("/api/voices")
    ok_list = (st == 200 and r.get("ok") is True and "installed" in r
               and isinstance(r.get("voices"), list) and r.get("provider") == "local-audio8")
    st1, r1 = api("/api/voice-register?name=t30x&text=", "POST", raw=b"x")
    ok_notext = st1 == 400 and "逐字稿" in (r1.get("err") or "")
    st2, r2 = api("/api/voice-register?name=..&text=y", "POST", raw=b"x")
    ok_badname = st2 == 400 and "voice name" in (r2.get("err") or "")
    st3, r3 = api("/api/voice-delete/t30_no_such_voice", "POST")
    ok_del404 = st3 == 404
    check("T30 声纹管理 API（voices 结构/缺逐字稿拒/坏名拒/删 404）",
          ok_list and ok_notext and ok_badname and ok_del404,
          "list=%s notext=%s badname=%s del404=%s" % (ok_list, ok_notext, ok_badname, ok_del404))


# ---------------------------------------------------------------- T31 配音裁剪自动化（v2-③ dubfit）
def t31_dubfit():
    # 回归锚：包络掐头掐尾 / D1 句首残留→重锚回退裁点（VAD 能量边界）/ 序列化保真
    #（故事线未知字段透传，硬规则 4）/ 不过 exit 1 + dubgate-report 落盘（编排器 dubgate 环节读它）。
    # ASR 全程打桩（离线）：fit.mp3 复检词轨 vs src.wav 原始词轨 按文件名分派。
    import tempfile, shutil
    FF = FFMPEG
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import dubfit, asr
        tmp = tempfile.mkdtemp(prefix="t31_")
        old_cwd, old_tr = os.getcwd(), asr.transcribe
        MODE = {"m": "clean"}
        state = {"dirty_used": False}
        CLEAN = {"text": "你好世界", "words": [
            {"text": "你", "start": 0.0, "end": 0.3}, {"text": "好", "start": 0.4, "end": 0.7},
            {"text": "世", "start": 0.9, "end": 1.2}, {"text": "界", "start": 1.3, "end": 1.7}]}
        DIRTY = {"text": "残你好世界", "words": [
            {"text": "残", "start": 0.0, "end": 0.3}] + CLEAN["words"]}
        RAW = {"text": "残你好世界", "words": [
            {"text": "残", "start": 0.3, "end": 0.7}, {"text": "你", "start": 1.3, "end": 1.5},
            {"text": "好", "start": 1.6, "end": 1.8}, {"text": "世", "start": 2.0, "end": 2.2},
            {"text": "界", "start": 2.3, "end": 2.6}]}

        def fake_transcribe(src, provider=None, cache_dir=None):
            if src.endswith("src.wav"):
                return RAW
            if MODE["m"] == "always_dirty":
                return DIRTY
            if MODE["m"] == "dirty_first" and not state["dirty_used"]:
                state["dirty_used"] = True
                return DIRTY
            return CLEAN

        try:
            asr.transcribe = fake_transcribe
            dubfit.asr.transcribe = fake_transcribe  # 同源模块双保险（import 绑定早于 patch）
            os.chdir(tmp)
            # 夹具 dub 录音：0-1s 静音 + 1-3s 语音 + 3-4s 静音（头尾垃圾 1s each）
            os.makedirs("projects/t31/materials/dub")
            os.makedirs("projects/t31/storylines")
            raw = "projects/t31/materials/dub/raw.mp3"
            r = subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                                "aevalsrc='if(lt(t,1),0,if(lt(t,3),sin(440*t),0))':s=16000:d=4",
                                "-c:a", "libmp3lame", raw], capture_output=True, text=True)
            assert r.returncode == 0, "夹具合成失败"
            sl = {"beats": [
                {"no": 1, "custom_keep": "beat级透传", "story": "你好世界。",
                 "narration": {"mode": "dub", "audio": "materials/dub/raw.mp3",
                               "custom_nar": "narration级透传"}},
                {"no": 2, "story": "你好世界。",
                 "narration": {"mode": "dub", "audio": "materials/dub/raw.mp3"}}]}
            json.dump(sl, open("projects/t31/storylines/t31.json", "w", encoding="utf-8"),
                      ensure_ascii=False)
            old_argv = sys.argv
            # 幕1 干净 take（包络轮即过）；幕2 首检残留"残"→重锚回退到 1s 语音span头
            MODE["m"] = "dirty_first"
            sys.argv = ["dubfit.py", "t31", "--story", "t31"]
            code = None
            try:
                dubfit.main()
            except SystemExit as e:
                code = e.code
            finally:
                sys.argv = old_argv
            rep = json.load(open("projects/t31/dubfit-report.json"))
            sl2 = json.load(open("projects/t31/storylines/t31.json"))
            b1, b2 = sl2["beats"][0], sl2["beats"][1]
            n1, n2 = b1["narration"], b2["narration"]
            anchored = n1 if any(r.startswith("head-anchor") for r in n1["dubfit"]["rounds"]) else n2
            fit1 = os.path.join("projects/t31", b1["narration"]["audio"])
            ok_clean = (code in (None, 0) and rep["passed"] is True
                        and n1["dubfit"]["rounds"][0] == "envelope"
                        and n2["dubfit"]["rounds"][0] == "envelope"
                        and os.path.exists(fit1))
            ok_anchor = (any(r.startswith("head-anchor@你") for r in anchored["dubfit"]["rounds"])
                         and anchored["dubfit"]["start"] > 0.85  # 残留段被掐掉（mp3 编码容差）
                         and anchored["audio"].startswith("materials/dub/fit-t31-b"))
            ok_keep = (b1.get("custom_keep") == "beat级透传"
                       and b1["narration"].get("custom_nar") == "narration级透传"
                       and b1["narration"]["dubfit"]["raw"] == "materials/dub/raw.mp3")
            dg = json.load(open("projects/t31/dubgate-report.json"))
            ok_dg = dg["passed"] is True and dg["source"] == "dubfit"
            # 负例：门禁永不过 → exit 1 + 报告 passed=False
            MODE["m"] = "always_dirty"
            json.dump({"beats": [{"no": 9, "story": "你好世界。",
                                  "narration": {"mode": "dub", "audio": "materials/dub/raw.mp3"}}]},
                      open("projects/t31/storylines/bad.json", "w", encoding="utf-8"))
            sys.argv = ["dubfit.py", "t31", "--story", "bad"]
            bad_code = None
            try:
                dubfit.main()
            except SystemExit as e:
                bad_code = e.code
            finally:
                sys.argv = old_argv
            rep_bad = json.load(open("projects/t31/dubfit-report.json"))
            ok_fail = bad_code == 1 and rep_bad["passed"] is False and rep_bad["fails"] == [9]
            check("T31 dubfit 配音裁剪（包络/句首重锚/保真/不过 exit1+dubgate报告）",
                  ok_clean and ok_anchor and ok_keep and ok_dg and ok_fail,
                  "clean=%s anchor=%s keep=%s dg=%s fail=%s" % (ok_clean, ok_anchor, ok_keep, ok_dg, ok_fail))
        finally:
            asr.transcribe = old_tr
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T31 dubfit 配音裁剪（包络/句首重锚/保真/不过 exit1+dubgate报告）",
              False, "异常: %s" % str(e)[:140])


# ---------------------------------------------------------------- T32 起草截断升档重试（冷启动实锤缺口）
def t32_draft_truncation_retry():
    # 回归锚：新素材包冷启动实锤——M3 正文超 cap 时 finish_reason=length 返回 JSON 半成品，
    # 旧代码非空即 return → extract_json 必炸。锚：length 必须升档重试，2 次调用拿到干净正文。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import draft
        calls = {"n": 0}

        class _R:
            def __init__(self, d):
                self._d = d

            def read(self):
                return json.dumps(self._d).encode()

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _R({"choices": [{"message": {"content": '{"title":"x","outline":"未写完'},
                                        "finish_reason": "length"}],
                           "usage": {"completion_tokens": 8192}})
            return _R({"choices": [{"message": {"content": '{"title":"x","outline":"完整"}'},
                                    "finish_reason": "stop"}],
                       "usage": {"completion_tokens": 100}})

        old = draft.urllib.request.urlopen
        draft.urllib.request.urlopen = fake_urlopen
        try:
            text = draft.chat_llm([{"role": "user", "content": "hi"}])
            ok = calls["n"] == 2 and "完整" in text
        finally:
            draft.urllib.request.urlopen = old
        check("T32 起草截断升档重试（finish_reason=length 不交付半成品 JSON）",
              ok, "calls=%s" % calls["n"])
    except Exception as e:
        check("T32 起草截断升档重试（finish_reason=length 不交付半成品 JSON）",
              False, "异常: %s" % str(e)[:140])


# ---------------------------------------------------------------- T33 original 幕字幕文本=素材台词（非 story 摘要）
def t33_vadwords_transcript_text():
    # 回归锚：维护者 实锤——story 是"这一幕讲什么"的分镜摘要，被 vadwords 铺进 VAD 段后
    # 字幕=总结腔。锚：original 幕词轨文本=素材窗口 transcript 文本（时间仍 VAD 物理测量），
    # 窗口无词回退 story；dub 幕保持 story 文本（=TTS 念稿）。
    import tempfile, shutil
    FF = FFMPEG
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import vadwords
        tmp = tempfile.mkdtemp(prefix="t33_")
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            os.makedirs("materials/packs/p1")
            os.makedirs("projects/t33/materials")
            os.makedirs("projects/t33/storylines")
            # M1 有转写词（0.5-3.5s 三词）；M2 无 transcript（回退通道）
            files = []
            for mid, words in (("M1", [{"text": "找", "start": 0.5, "end": 1.5},
                                       {"text": "平", "start": 1.5, "end": 2.5},
                                       {"text": "。", "start": 2.5, "end": 3.5}]),
                               ("M2", None)):
                mp4 = os.path.join("materials/packs/p1", mid + ".mp4")
                r = subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi",
                                    "-i", "sine=frequency=440:duration=4", "-c:a", "aac", mp4],
                                   capture_output=True, text=True)
                assert r.returncode == 0, "夹具合成失败"
                f = {"id": mid, "file": mid + ".mp4", "kind": "video", "transcript": {}}
                if words:
                    f["transcript"] = {"text": "找平。", "words": words}
                files.append(f)
            json.dump({"files": files}, open("materials/packs/p1/pack.json", "w", encoding="utf-8"),
                      ensure_ascii=False)
            json.dump({"packs": ["p1"]}, open("projects/t33/materials/library.json", "w"))
            sl = {"beats": [
                {"no": 1, "story": "分镜摘要：这一幕讲找平工艺", "narration": {"mode": "original"},
                 "tracks": [{"role": "A", "source_id": "M1", "src_in": 0, "duration": 4}]},
                {"no": 2, "story": "回退通道摘要", "narration": {"mode": "original"},
                 "tracks": [{"role": "A", "source_id": "M2", "src_in": 0, "duration": 4}]}]}
            json.dump(sl, open("projects/t33/storylines/t33.json", "w", encoding="utf-8"),
                      ensure_ascii=False)
            old_argv = sys.argv
            sys.argv = ["vadwords.py", "t33", "--story", "t33"]
            try:
                vadwords.main()
            finally:
                sys.argv = old_argv
            sl2 = json.load(open("projects/t33/storylines/t33.json"))
            rep = json.load(open("projects/t33/vad-report.json"))
            w1 = "".join(w["t"] for w in sl2["beats"][0]["narration"]["words"])
            w2 = "".join(w["t"] for w in sl2["beats"][1]["narration"]["words"])
            tf = {r["no"]: r.get("text_from") for r in rep}
            ok_tr = w1 == "找平。" and tf.get(1) == "transcript"   # 素材台词进字幕，摘要被逐出
            ok_fb = w2 == "回退通道摘要" and tf.get(2) == "story"  # 无词窗口回退 story
            # 时间必须仍由 VAD 物理测量产出（语音段非空且词落在段内）
            ws1 = sl2["beats"][0]["narration"]["words"]
            ok_vad = bool(rep[0]["spans"]) and all(any(s - 0.01 <= w_["s"] and w_["e"] <= e + 0.01
                                                     for s, e in rep[0]["spans"]) for w_ in ws1[:3])
            check("T33 original 幕字幕文本=素材台词（VAD 管时间/transcript 管文本/空窗回退 story）",
                  ok_tr and ok_fb and ok_vad, "tr=%s fb=%s vad=%s" % (ok_tr, ok_fb, ok_vad))
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T33 original 幕字幕文本=素材台词（VAD 管时间/transcript 管文本/空窗回退 story）",
              False, "异常: %s" % str(e)[:140])


def t38_cover_sink():
    # 回归锚：2026-09-15 实锤——build_cover 的 ai-generated/upload 分支直返槽位路径
    # （cover/generated.jpg），前端 files.cover 只认 cover{sid}.jpg → 渲染用了 AI 图
    # 但 Studio 显示陈旧抽帧封面（aidraft3 哈希不匹配抓获）。锚：五策略全部归一到 cover{sid}.jpg。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import pipeline
        tmp = tempfile.mkdtemp(prefix="t38_")
        src = os.path.join(tmp, "src.mp4")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=1080x1920:rate=25:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", src], check=True)
        pd = os.path.join(tmp, "proj")
        os.makedirs(os.path.join(pd, "cover"))
        shutil.copyfile(src, os.path.join(pd, "out-demo.mp4"))
        jpg = os.path.join(tmp, "a.jpg")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src,
                        "-frames:v", "1", "-q:v", "2", jpg], check=True)
        shutil.copyfile(jpg, os.path.join(pd, "cover", "generated.jpg"))
        seg = {"no": 1, "src_in": 0.0, "src_file": src}
        plan = {"media": src, "segments": [seg], "meta": {"cover": {}}}

        def run(strategy, **kw):
            c = {"strategy": strategy}
            c.update(kw)
            plan["meta"]["cover"] = c
            out = pipeline.build_cover(plan, pd, "-demo")
            return out if out and os.path.exists(out) else None

        r_out = run("output-frame", at=0.4)
        r_first = run("first-frame")
        r_beat = run("beat-frame", beat_no=1, at=0.2)
        want = os.path.normpath(os.path.join(pd, "cover", "cover-demo.jpg"))
        # ai-generated 新契约（2026-09-18 Noah 封面改版）：代表帧底图 + 提示词走 image-01
        # 保主体生成。锚：①缺 prompt fail-fast（不再静默采纳外部图）；②有 prompt 真把
        # 底图传给 gen_image（base_image=代表帧）且产物归一槽位。gen_image 桩掉（离线）。
        try:
            run("ai-generated")
            ai_gate = False
        except RuntimeError as e:
            ai_gate = "prompt" in str(e)
        import types
        _calls = {}
        def _gi(prompt, out, aspect_ratio="9:16", base_image=None, **kw):
            _calls["base"], _calls["prompt"] = base_image, prompt
            shutil.copyfile(jpg, out)
            return out
        sys.modules["image_gen"] = types.SimpleNamespace(gen_image=_gi)
        try:
            r_ai = run("ai-generated", prompt="保持参考图场景不变，暖色自然光")
        finally:
            sys.modules.pop("image_gen", None)
        ai_ok = bool(r_ai) and os.path.normpath(r_ai) == want and _calls.get("base") \
            and os.path.exists(_calls["base"]) and "参考图" in (_calls.get("prompt") or "")
        shutil.copyfile(jpg, os.path.join(pd, "cover", "generated.jpg"))
        r_up = run("upload")
        up_ok = bool(r_up) and os.path.normpath(r_up) == want
        check("T38 封面收口五策略归一 cover{sid}.jpg（AI/上传不直返槽位路径）",
              all([r_out, r_first, r_beat, ai_ok, up_ok]),
              "out=%s first=%s beat=%s ai=%s upload=%s" % (
                  bool(r_out), bool(r_first), bool(r_beat), ai_ok, up_ok))
    except Exception as e:
        check("T38 封面收口五策略归一 cover{sid}.jpg", False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t39_digest():
    # 锚：/api/digest 同款 build_digest——包级盘点（汇总单素材识别 → LLM 一次调用出导演视角
    # digest 落 pack.json，draft 档案头消费）。①空包护栏：无素材不调 LLM 直接 RuntimeError
    #（曾把模型的"错误说明"当 digest 落库）②正常包：digest 结构落盘可回读。
    import tempfile
    calls = {"n": 0}

    class _R:
        def __init__(self, d):
            self._d = d

        def read(self):
            return json.dumps(self._d).encode()

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _R({"choices": [{"message": {"content": json.dumps({
            "theme": "t39 主题",
            "inventory": {"a_roll_candidates": ["M1"], "broll_pool": [],
                          "voiceover_sources": [], "ambient": [], "gaps": []},
            "roles": [], "narrative_assets": []})}, "finish_reason": "stop"}]})

    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import understand as U
        import draft
        tmp = tempfile.mkdtemp(prefix="t39_")
        old_root = U.ROOT
        U.ROOT = tmp
        pd_ = os.path.join(tmp, "materials", "packs", "t39p")
        os.makedirs(pd_)
        pk = {"files": [
            {"id": "M1", "usable": True, "duration": 8,
             "transcript": {"text": "台词一", "scripted_dup": 0},
             "visual": {"desc": "画面一", "content_type": "speech", "usage": "口播A轨"}},
            {"id": "M2", "usable": False, "duration": 6,
             "transcript": {"text": "x"}, "visual": {}}]}
        json.dump(pk, open(os.path.join(pd_, "pack.json"), "w", encoding="utf-8"), ensure_ascii=False)
        old_uo = draft.urllib.request.urlopen
        draft.urllib.request.urlopen = fake_urlopen
        try:
            d = U.build_digest("t39p")
            saved = json.load(open(os.path.join(pd_, "pack.json"), encoding="utf-8")).get("digest") or {}
            n_before = calls["n"]
            json.dump({"files": []}, open(os.path.join(pd_, "pack.json"), "w", encoding="utf-8"))
            try:
                U.build_digest("t39p")
                guard = False
            except RuntimeError:
                guard = True
        finally:
            draft.urllib.request.urlopen = old_uo
            U.ROOT = old_root
        ok = (d.get("theme") == "t39 主题"
              and saved.get("theme") == "t39 主题"
              and (saved.get("inventory") or {}).get("a_roll_candidates") == ["M1"]
              and guard and calls["n"] == n_before)
        check("T39 包级盘点 digest（结构落盘/空包不调 LLM 护栏）", ok,
              "theme=%s calls=%s guard=%s" % (saved.get("theme"), calls["n"], guard))
    except Exception as e:
        check("T39 包级盘点 digest", False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t40_beat_preview():
    # 锚：/api/beat 幕级预览——绝低压力快速剪辑核心（秒级 540p 单幕样张，
    # build_beat_cmd 与全片渲染两条代码路径，全片过≠单幕过）。手造故事线 → 幕渲染 → 产物有效视频。
    try:
        import fixtures
        def beat(no, mid, line, dur):
            return {"no": no, "id": "b%d" % no, "story": line,
                    "narration": {"mode": "original",
                                  "words": [{"t": c, "s": round(i * 0.2, 2), "e": round((i + 1) * 0.2, 2)}
                                            for i, c in enumerate(line)]},
                    "tracks": [{"role": "A", "source_id": mid, "cut_index": 0, "src_in": 0,
                                "duration": dur, "scale": 0.3, "op": "overlay-pip",
                                "requirement": None}],
                    "music": {"inherit": True, "bgm": None, "segment": None, "loop": None},
                    "effects": {}, "subtitle": {}, "transition_out": None}
        sl = {"title": "T40", "outline": "t40",
              "meta": {"audio": {}, "style": {}, "cover": {"strategy": "output-frame"}},
              "beats": [beat(1, "M0124", "第一幕测试台词。", 3), beat(2, "M0089", "第二幕测试台词。", 3)]}
        pdir = os.path.join(ROOT, "projects", fixtures.PID)
        sp = os.path.join(pdir, "storylines", "t40beat.json")
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        json.dump(sl, open(sp, "w", encoding="utf-8"), ensure_ascii=False)
        code, r = api("/api/beat/%s/1?story=t40beat" % fixtures.PID, method="POST")
        pv = os.path.join(pdir, "previews", "beat_t40beat_1.mp4")
        okv = False
        if os.path.exists(pv):
            pr = subprocess.run([FF, "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", pv], capture_output=True, text=True)
            try:
                okv = float((pr.stdout or "0").strip() or 0) > 0
            except ValueError:
                okv = False
        check("T40 幕预览渲染（/api/beat 产物存在且为有效视频）",
              code == 200 and r.get("ok") and okv,
              "code=%s ok=%s valid=%s log=…%s" % (code, r.get("ok"), okv, str(r.get("log"))[-300:]))
    except Exception as e:
        check("T40 幕预览渲染", False, "异常: %s" % str(e)[:140])


def t41_registry():
    # 锚：注册表管理写接口闭环（改注册表=改下一次渲染，曾零覆盖）。
    # save 整体保存→读回→白名单拒 enums（400）→delete→404→upload 音频入 assets+条目→清理。
    import tempfile
    sfxp = os.path.join(ROOT, "registry", "sfx.json")
    old = open(sfxp, encoding="utf-8").read()
    try:
        reg = json.loads(old)
        reg["_t41"] = {"file": "assets/sfx/_t41.mp3", "desc": "t41"}
        code1, _ = api("/api/registry-save/sfx", method="POST", body=reg)
        saved = "_t41" in json.load(open(sfxp, encoding="utf-8"))
        code2, _ = api("/api/registry-save/enums", method="POST", body={})
        code3, r3 = api("/api/registry-delete/sfx/_t41", method="POST")
        code4, _ = api("/api/registry-delete/sfx/_t41", method="POST")  # 再删=404
        gone = "_t41" not in json.load(open(sfxp, encoding="utf-8"))
        wav = os.path.join(tempfile.mkdtemp(prefix="t41_"), "a.wav")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1", wav], check=True)
        req = urllib.request.Request(BASE + "/api/registry-upload/sfx/_t41up?filename=a.wav",
                                     method="POST", data=open(wav, "rb").read(),
                                     headers={"Content-Type": "audio/x-wav", "X-AutoCut": "1"})
        with urllib.request.urlopen(req, timeout=60) as rr:
            up = json.loads(rr.read() or b"{}")
        up_in = "_t41up" in json.load(open(sfxp, encoding="utf-8"))
        asset = os.path.join(ROOT, "assets", "sfx", "_t41up.wav")
        up_file = os.path.exists(asset)
        api("/api/registry-delete/sfx/_t41up", method="POST")
        if os.path.exists(asset):
            os.remove(asset)
        check("T41 注册表管理写闭环（save/白名单拒/delete 404/upload+清理）",
              code1 == 200 and saved and code2 == 400 and code3 == 200 and code4 == 404
              and gone and up.get("ok") and up_in and up_file,
              "save=%s/%s enums=%s del=%s/redel=%s gone=%s upload=%s/%s/%s" % (
                  code1, saved, code2, code3, code4, gone, up.get("ok"), up_in, up_file))
    except Exception as e:
        check("T41 注册表管理写闭环", False, "异常: %s" % str(e)[:140])
    finally:
        open(sfxp, "w", encoding="utf-8").write(old)  # 防御：无论成败还原注册表快照


def t42_advance_gate():
    # 锚：orchestrate --advance 故事线门语义——素材段全绿时不带 --intent 必须停在故事线门
    # （打印门提示而非误跑 LLM 起草/后续渲染环节），状态零污染。门语义=人机契约的自动化边界。
    try:
        import fixtures
        pj = os.path.join(ROOT, "materials", "packs", fixtures.PID, "pack.json")
        pk = json.load(open(pj, encoding="utf-8"))
        for f in pk.get("files", []):
            f.setdefault("audit", {})
            if f.get("kind") != "image":
                f["audit"]["transcript"] = "done"
            if f.get("kind") in ("video", "image"):
                f["audit"]["visual"] = "done"
            if (f.get("transcript") or {}).get("words"):
                f["audit"]["proofread"] = "done"
            f["review"] = "reviewed"
        pk["digest"] = {"theme": "t42", "inventory": {}, "roles": [], "narrative_assets": []}
        json.dump(pk, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        pdir = os.path.join(ROOT, "projects", fixtures.PID)
        # 夹具自带 seed 故事线会让 storyline 判 done、advance 真去渲染——移到一边使
        # storyline 成为首个 pending 步（门语义才有触发条件），测后恢复
        sdir = os.path.join(pdir, "storylines")
        bak = sdir + "_t42bak"
        os.replace(sdir, bak)
        json.dump({"version": 1}, open(os.path.join(pdir, "dossier.json"), "w", encoding="utf-8"))
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import io
        import contextlib
        import orchestrate
        import disposition
        disposition.build(fixtures.PID)  # 离线缺陷台账（T28 同款路径）
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            st = orchestrate.advance(fixtures.PID)
        nxt = (st or {}).get("next")
        gate = "停在故事线门" in buf.getvalue()
        untouched = not os.path.exists(os.path.join(pdir, "out-main.mp4"))
        check("T42 --advance 故事线门（素材段全绿无 intent 必停门、零污染）",
              nxt == "storyline" and gate and untouched,
              "next=%s gate=%s untouched=%s" % (nxt, gate, untouched))
        os.replace(bak, sdir)  # 恢复 seed 故事线（后续测试依赖）
    except Exception as e:
        check("T42 --advance 故事线门", False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t43_proofread_review_gate():
    # 回归锚（2026-09-16 agent-037 实锤）：proofread 把语义存疑组进 review-queue.json
    # （status=pending）后环节照样 done——「违科」类未校错字直达成片。锚：queue 有 pending
    # = 门不开（_step_proofread 返回 pending）；清空/复核后回 done。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import orchestrate as ORCH
        tmp = tempfile.mkdtemp(prefix="t43_")
        old_root = ORCH.ROOT
        try:
            ORCH.ROOT = tmp
            os.makedirs(os.path.join(tmp, "materials/packs/tp"), exist_ok=True)
            json.dump({"files": [{"id": "M1", "kind": "video", "usable": True,
                                  "transcript": {"text": "x", "words": [{"text": "x", "start": 0, "end": 1}]},
                                  "audit": {"proofread": "auto-done"}}]},
                      open(os.path.join(tmp, "materials/packs/tp/pack.json"), "w", encoding="utf-8"))
            pdir = os.path.join(tmp, "projects/t43")
            os.makedirs(os.path.join(pdir, "materials"), exist_ok=True)
            json.dump({"packs": ["tp"]}, open(os.path.join(pdir, "materials/library.json"), "w"))
            st1, d1 = ORCH._step_proofread(pdir)  # 空 queue → done
            json.dump({"project": "t43", "items": [
                {"material": "M1", "find": "违科", "replace": "贝壳",
                 "status": "pending", "reason": "语义存疑"}]},
                      open(os.path.join(pdir, "review-queue.json"), "w", encoding="utf-8"))
            st2, d2 = ORCH._step_proofread(pdir)  # 有 pending → 门不开
            ok = (st1 == "done") and (st2 == "pending" and "待人工复核" in d2 and "违科" in d2)
            check("T43 proofread 人工复核门（review-queue 有 pending=环节 pending，复核后放行）",
                  ok, "empty=%s:%s queued=%s:%s" % (st1, d1[:20], st2, d2[:40]))
        finally:
            ORCH.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T43 proofread 人工复核门（review-queue 有 pending=环节 pending，复核后放行）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t44_vadwords_no_asr_time():
    # 回归锚（2026-09-16 agent-037 实锤，2026-09-18 勘误）：vadwords 曾用 ASR 词时间过滤
    # 窗口内词——当时词时间是句内均分合成值（asr.py 未传 timestamp_level），偏 0.5-3s
    # （M0269「我」标 7.2 实际 11.4），按合成时间过滤=字幕漏字/半句。
    # 锚①：tier 缺失（合成时间）词时间全部离谱时，选词仍按文本顺序铺满 VAD 段。
    # 锚②（T44b）：tier=word 实测词时间直取（timestamp_level=word，与 VAD 互证 ±0.05s）。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import vadwords
        tmp = tempfile.mkdtemp(prefix="t44_")
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            os.makedirs("materials/packs/p1"); os.makedirs("projects/t44/materials")
            os.makedirs("projects/t44/storylines")
            mp4 = os.path.join("materials/packs/p1", "M1.mp4")
            r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                                "-i", "sine=frequency=440:duration=4", "-c:a", "aac", mp4],
                               capture_output=True, text=True)
            assert r.returncode == 0, "夹具合成失败"
            json.dump({"files": [{"id": "M1", "file": "M1.mp4", "kind": "video",
                                  # ASR 词时间全部离谱（真实 1000s 处）——旧逻辑会选不到词回退 story
                                  "transcript": {"text": "甲乙丙丁",
                                                 "words": [{"text": "甲", "start": 1000, "end": 1001},
                                                           {"text": "乙", "start": 1001, "end": 1002},
                                                           {"text": "丙", "start": 1002, "end": 1003},
                                                           {"text": "丁", "start": 1003, "end": 1004}]}}]},
                      open("materials/packs/p1/pack.json", "w", encoding="utf-8"))
            json.dump({"packs": ["p1"]}, open("projects/t44/materials/library.json", "w"))
            json.dump({"beats": [{"no": 1, "story": "回退摘要不应出现",
                                  "narration": {"mode": "original"},
                                  "tracks": [{"role": "A", "source_id": "M1", "src_in": 0, "duration": 4}]}]},
                      open("projects/t44/storylines/t44.json", "w", encoding="utf-8"))
            old_argv = sys.argv
            sys.argv = ["vadwords.py", "t44", "--story", "t44"]
            try:
                vadwords.main()
            finally:
                sys.argv = old_argv
            sl2 = json.load(open("projects/t44/storylines/t44.json"))
            rep = json.load(open("projects/t44/vad-report.json"))
            w1 = "".join(w["t"] for w in sl2["beats"][0]["narration"]["words"])
            ok = (w1 == "甲乙丙丁" and rep[0].get("text_from") == "transcript")
            check("T44 vadwords 合成时间禁作锚（tier 缺失词时间离谱仍按文本顺序铺满 VAD 段）",
                  ok, "text=%s from=%s" % (w1, rep[0].get("text_from")))
            # 勘误锚（2026-09-18 Noah 指路）：asr.py 从未传 timestamp_level，历史"ASR 词时间
            # 漂移 0.5-3s"实为句内均分合成值。tier=word（MiniMax 实测，与 VAD 互证 ±0.05s）
            # 时词轨直取实测时间，不进均分机器——夹具：词时间只盖前半窗，均分机器会把
            # 4 字铺满全窗（末字 e=4.0），直通道末字 e 必须原样 2.5。
            mp4b = os.path.join("materials/packs/p1", "M2.mp4")
            r2 = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                                 "-i", "sine=frequency=440:duration=4", "-c:a", "aac", mp4b],
                                capture_output=True, text=True)
            assert r2.returncode == 0, "夹具M2合成失败"
            pk = json.load(open("materials/packs/p1/pack.json", encoding="utf-8"))
            pk["files"].append({"id": "M2", "file": "M2.mp4", "kind": "video",
                                "transcript": {"tier": "word", "text": "甲乙丙丁",
                                               "words": [{"text": "甲", "start": 0.5, "end": 1.0},
                                                         {"text": "乙", "start": 1.0, "end": 1.5},
                                                         {"text": "丙", "start": 1.5, "end": 2.0},
                                                         {"text": "丁", "start": 2.0, "end": 2.5}]}})
            json.dump(pk, open("materials/packs/p1/pack.json", "w", encoding="utf-8"))
            json.dump({"beats": [{"no": 1, "story": "不进字幕",
                                  "narration": {"mode": "original"},
                                  "tracks": [{"role": "A", "source_id": "M2", "src_in": 0, "duration": 4}]}]},
                      open("projects/t44/storylines/t44b.json", "w", encoding="utf-8"))
            sys.argv = ["vadwords.py", "t44", "--story", "t44b"]
            try:
                vadwords.main()
            finally:
                sys.argv = old_argv
            sl3 = json.load(open("projects/t44/storylines/t44b.json"))
            rep3 = json.load(open("projects/t44/vad-report.json"))
            w3 = sl3["beats"][0]["narration"]["words"]
            ok3 = ("".join(w["t"] for w in w3) == "甲乙丙丁"
                   and rep3[0].get("text_from") == "word-ts"
                   and w3[0]["s"] == 0.5 and abs(w3[-1]["e"] - 2.5) < 0.01
                   and not rep3[0].get("cross_span"))
            check("T44b vadwords 实测词级时间戳直取（tier=word 直通道不均分，2026-09-18 勘误）",
                  ok3, "w0.s=%s wN.e=%s from=%s" % (w3[0]["s"], w3[-1]["e"], rep3[0].get("text_from")))
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T44 vadwords 选词弃 ASR 时间（词时间离谱仍按文本顺序铺满 VAD 段）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t45_content_type_gates():
    # 回归锚（2026-09-16 agent-037 实锤双断链）：提示词 advisory 拦不住——
    # ① M0269 voiceover 读稿画面照样 A 轨 original 裸奔；② B 轨选了 M0275 对话画面（管线
    # B 轨无音频通道）→ 嘴动无声穿帮。锚：validate 硬剔除——deny 类/voiceover 禁 A，
    # 说话类画面禁 B。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import draft as D
        mats = {
            "V": {"id": "V", "kind": "video", "usable": True, "duration": 10,
                  "visual": {"content_type": "voiceover", "desc": "手持文件低头朗读"}},
            "ME": {"id": "ME", "kind": "video", "usable": True, "duration": 10,
                   "visual": {"content_type": "meta", "desc": "导演说戏"}},
            "DG": {"id": "DG", "kind": "video", "usable": True, "duration": 10,
                   "visual": {"content_type": "dialogue", "desc": "两人面对面沟通细节"},
                   "transcript": {"words": []}},
            "CL": {"id": "CL", "kind": "video", "usable": True, "duration": 10,
                   "visual": {"content_type": "narration", "desc": "空镜施工工艺"},
                   "transcript": {"words": []}},
            "AM": {"id": "AM", "kind": "video", "usable": True, "duration": 10,
                   "visual": {"content_type": "ambient", "desc": "小区园林环境"}},
            "BV": {"id": "BV", "kind": "video", "usable": True, "duration": 10,
                   "visual": {"content_type": "broll", "desc": "住宅实拍画面"},
                   "transcript": {"words": [{"text": "整体", "start": 0.0, "end": 0.6}]}},  # 旁白空镜（104030 型）
        }
        draft = {"beats": [
            {"story": "s", "tracks": [{"role": "A", "source_id": "V", "src_in": 0, "duration": 4}]},
            {"story": "s", "tracks": [{"role": "A", "source_id": "ME", "src_in": 0, "duration": 4}]},
            {"story": "s", "tracks": [{"role": "A", "source_id": "DG", "src_in": 0, "duration": 4},
                                      {"role": "B", "source_id": "DG", "src_in": 0, "duration": 3}]},
            {"story": "s", "tracks": [{"role": "A", "source_id": "CL", "src_in": 0, "duration": 4},
                                      {"role": "B", "source_id": "CL", "src_in": 0, "duration": 3}]},
            # 空镜分层（2026-09-18）：口播幕（默认 original）ambient 无语音禁 A→整幕剔除；
            # 纯空镜幕（mode=none）ambient 作 A 轨整幅→保留且 mode 透传
            {"story": "", "tracks": [{"role": "A", "source_id": "AM", "src_in": 0, "duration": 4}]},
            {"story": "", "narration": {"mode": "none"},
             "tracks": [{"role": "A", "source_id": "AM", "src_in": 0, "duration": 4}]},
            # 旁白空镜（2026-09-18 二级细化）：broll+自带语音 original 口播→放行（104030 实拍画外音）
            {"story": "整体逛下来", "tracks": [{"role": "A", "source_id": "BV", "src_in": 0, "duration": 4}]},
        ]}
        beats = D.validate(draft, mats, [])
        ok_struct = len(beats) == 4  # voiceover/meta/无语音ambient口播幕 三幕被剔除，剩 DG-A、CL 双轨、AM 纯空镜幕、BV 旁白空镜幕
        ok_dg = ([t["role"] for t in beats[0]["tracks"]] == ["A"]
                 and beats[0]["tracks"][0]["source_id"] == "DG")          # B 哑口型被剔
        ok_cl = [t["role"] for t in beats[1]["tracks"]] == ["A", "B"]    # 干净素材双轨保留
        ok_am = (len(beats[2]["tracks"]) == 1 and beats[2]["tracks"][0]["source_id"] == "AM"
                 and beats[2]["narration"]["mode"] == "none"
                 and all(b["narration"]["mode"] == "original" for b in beats[:2]))  # mode 透传不串
        ok_bv = (beats[3]["tracks"][0]["source_id"] == "BV"
                 and beats[3]["narration"]["mode"] == "original")         # 旁白空镜口播放行
        check("T45 content_type 强制门（meta/voiceover 禁A、说话画面禁B、空镜两级：无语音分层/旁白空镜放行）",
              ok_struct and ok_dg and ok_cl and ok_am and ok_bv,
              "beats=%d dg=%s cl=%s am=%s bv=%s" % (len(beats), ok_dg, ok_cl, ok_am, ok_bv))
    except Exception as e:
        check("T45 content_type 强制门（meta/voiceover 禁A、说话画面禁B、空镜两级：无语音分层/旁白空镜放行）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t46_loudnorm_chain():
    # 回归锚（2026-09-16 agent-037 实锤）：管线无全局响度位，R6 恒 warn"渲染后修"。
    # 锚：渲染混音链默认带 loudnorm=I=-16（EBU R128 平台锚）；audio.loudnorm=false 可关；
    # 真渲 2s 正弦实测 integrated ≈ -16 LUFS（±1）。
    import tempfile
    ass_dir = os.path.join(ROOT, "assets", "preview")
    ass_path = os.path.join(ass_dir, "_t46.ass")
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import pipeline
        tmp = tempfile.mkdtemp(prefix="t46_")
        src = os.path.join(tmp, "src.mp4")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=480x854:rate=25:duration=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", src], check=True)
        os.makedirs(ass_dir, exist_ok=True)
        open(ass_path, "w", encoding="utf-8").write(
            "[Script Info]\nTitle: t\nScriptType: v4.00+\nPlayResX: 480\nPlayResY: 854\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,t\n")
        seg = {"no": 1, "src_in": 0, "dur": 2.0, "tl_in": 0.0, "transition": None,
               "tracks": [{"role": "A", "media": src, "src_in": 0, "dur": 2.0}],
               "effects": {"stickers": [], "sfx": []}}
        base = {"width": 480, "height": 854, "fps": 25, "duration": 2.0,
                "media": src, "words": [], "segments": [seg]}
        p_on = dict(base)          # 缺省=开（2026-09-16：修复前无此键）
        p_true = dict(base, loudnorm=True)
        p_off = dict(base, loudnorm=False)
        f_on = " ".join(pipeline.build_cmd(p_on, ass_path, os.path.join(tmp, "a.mp4")))
        f_true = " ".join(pipeline.build_cmd(p_true, ass_path, os.path.join(tmp, "b.mp4")))
        f_off = " ".join(pipeline.build_cmd(p_off, ass_path, os.path.join(tmp, "c.mp4")))
        ok_filter = ("loudnorm=I=-16" in f_on) and ("loudnorm=I=-16" in f_true) \
                    and ("loudnorm" not in f_off)
        out = os.path.join(tmp, "out.mp4")
        cmd = pipeline.build_cmd(p_on, ass_path, out)
        # 编码器钉 libx264：本锚测的是 loudnorm 滤镜链，不是编码器。CI Windows 无 GPU，
        # nvenc 探测（-h encoder= 只查存在性）会选中但运行时载不了 nvcuda.dll——渲染
        # 入口 pipeline.py:907 有软编运行时回退兜住（T8/T9 实证），测试直调 build_cmd
        # 没有那层回退，必须自钉确定性编码器。
        cmd[cmd.index("-c:v") + 1] = "libx264"
        rr = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if rr.returncode != 0:
            # 失败诊断全量上日志（ffmpeg 9 新 CLI 报错风格不同，截 160 字不够定位）：
            # 打印完整 stderr + 实际使用的二进制与编码器选择
            print("T46 DIAG ffmpeg=%s" % (cmd[0],))
            print("T46 DIAG stderr:\n%s" % (rr.stderr or "")[-2000:])
            check("T46 loudnorm 渲染链（默认开/可关，实测 integrated≈-16 LUFS）", False,
                  "渲染失败 rc=%s" % rr.returncode)
            return
        pv = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", out,
                             "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                            capture_output=True, text=True)
        m = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", pv.stderr)
        lufs = float(m.group(1)) if m else None
        ok_loud = lufs is not None and abs(lufs - (-16)) <= 1.0
        # 采样率锚（2026-10-16 用户实锤：loudnorm 内部升采样泄漏 → 96kHz AAC，ffmpeg/afplay
        # 宽容能放但浏览器解不动=成片/预览一开声就停。主渲染与幕预览两出口都钉 -ar 48000）
        sr = subprocess.run([FFMPEG, "-hide_banner", "-i", out], capture_output=True, text=True)
        ok_rate = re.search(r"Audio: aac.*?48000 Hz", sr.stderr or "") is not None
        ok_beat_rate = "-ar 48000" in " ".join(pipeline.build_beat_cmd(p_on, seg, ass_path, os.path.join(tmp, "b.mp4")))
        check("T46 loudnorm 渲染链（默认开/可关，实测 integrated≈-16 LUFS；出口采样率 48k）",
              ok_filter and ok_loud and ok_rate and ok_beat_rate,
              "filter=%s lufs=%s 采样率48k=%s 幕预览-ar=%s" % (ok_filter, lufs, ok_rate, ok_beat_rate))
    except Exception as e:
        check("T46 loudnorm 渲染链（默认开/可关，实测 integrated≈-16 LUFS）",
              False, "异常: %s" % str(e)[:140])
    finally:
        if os.path.exists(ass_path):
            os.remove(ass_path)
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t47_proofread_pending_not_done():
    # 回归锚（2026-09-16 n006 实锤复发，n004 曾人肉拦截未根治）：上传初值
    # audit.proofread="pending" 是非空串，旧 not a 判定放过=校对从未跑也报 done
    # （19/19 pending 虚报"词轨校对 done"，校对轮被迫返工）。锚：只有 done/auto-done
    # 算过；pending 一律待校对。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import orchestrate as ORCH
        tmp = tempfile.mkdtemp(prefix="t47_")
        old_root = ORCH.ROOT
        try:
            ORCH.ROOT = tmp
            os.makedirs(os.path.join(tmp, "materials/packs/tp"), exist_ok=True)
            json.dump({"files": [
                {"id": "M1", "kind": "video", "usable": True,
                 "transcript": {"text": "x", "words": [{"text": "x", "start": 0, "end": 1}]},
                 "audit": {"proofread": "pending"}},        # 上传初值——旧逻辑误判 done
                {"id": "M2", "kind": "video", "usable": True,
                 "transcript": {"text": "y", "words": [{"text": "y", "start": 0, "end": 1}]},
                 "audit": {"proofread": "auto-done"}}]},     # 已过
                      open(os.path.join(tmp, "materials/packs/tp/pack.json"), "w", encoding="utf-8"))
            pdir = os.path.join(tmp, "projects/t47")
            os.makedirs(os.path.join(pdir, "materials"), exist_ok=True)
            json.dump({"packs": ["tp"]}, open(os.path.join(pdir, "materials/library.json"), "w"))
            st1, d1 = ORCH._step_proofread(pdir)  # 有 pending → 环节 pending
            # 全部校对完成 → done
            pj = json.load(open(os.path.join(tmp, "materials/packs/tp/pack.json"), encoding="utf-8"))
            pj["files"][0]["audit"]["proofread"] = "done"
            json.dump(pj, open(os.path.join(tmp, "materials/packs/tp/pack.json"), "w", encoding="utf-8"))
            st2, d2 = ORCH._step_proofread(pdir)
            ok = (st1 == "pending" and "1/2" in d1) and (st2 == "done")
            check("T47 proofread 严格判定（audit=pending≠done，只有 done/auto-done 算过）",
                  ok, "pending=%s:%s all-done=%s:%s" % (st1, d1[:24], st2, d2[:16]))
        finally:
            ORCH.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T47 proofread 严格判定（audit=pending≠done，只有 done/auto-done 算过）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t48_validate_no_beat_truncation():
    # 回归锚（2026-09-16 Noah 实锤）：draft.validate 自基线起 `[:4]` 截断——LLM 给 6 幕
    # 只落 4 幕，第 5 幕起静默丢弃（C 类静默数据丢失，同 T32 截断当成功一族）。
    # 锚：6 幕全过 validate = 6 幕全留，且第 5/6 幕同样吃钳制（不是旁路放行）。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import draft as D
        mats = {"M%d" % i: {"id": "M%d" % i, "kind": "video", "usable": True, "duration": 30,
                            "visual": {"content_type": "narration", "desc": "空镜"},
                            "transcript": {"words": []}}
                for i in range(1, 7)}
        draft = {"beats": [
            {"story": "s%d" % i,
             "tracks": [{"role": "A", "source_id": "M%d" % i, "src_in": 0, "duration": 4}]}
            for i in range(1, 7)]}
        beats = D.validate(draft, mats, [])
        # 第 6 幕 duration 故意越界（40s > 素材 30s）→ 钳到 30s，证明第 5 幕起也在校验不在旁路
        draft["beats"][5]["tracks"][0]["duration"] = 40
        beats2 = D.validate(draft, mats, [])
        ok = (len(beats) == 6) and (len(beats2) == 6) and (beats2[5]["tracks"][0]["duration"] <= 30)
        check("T48 validate 不截断幕数（6 幕全留，第 5 幕起同样吃钳制）",
              ok, "beats=%d clamp=%s" % (len(beats2), beats2[5]["tracks"][0]["duration"] if len(beats2) == 6 else "-"))
    except Exception as e:
        check("T48 validate 不截断幕数（6 幕全留，第 5 幕起同样吃钳制）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t49_proofread_queue_lands_in_project():
    # 回归锚（2026-09-16 修根：n004 人肉/n006/wangyalun 三连发）：proofread 原收包 id、
    # 队列写 projects/<包id>/，包名≠项目名时门禁永远等不到复核。锚：入口=项目名，
    # 包叫 tp49、项目叫 t49proj（故意不同名），队列必须落 projects/t49proj/ 且
    # projects/tp49/ 下不得出现队列。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import proofread
        tmp = tempfile.mkdtemp(prefix="t49_")
        old_root = proofread.ROOT
        try:
            proofread.ROOT = tmp
            os.makedirs(os.path.join(tmp, "materials", "packs", "tp49"))
            os.makedirs(os.path.join(tmp, "config"))
            os.makedirs(os.path.join(tmp, "projects", "t49proj", "materials"))
            json.dump({"terms": ["龙骨"]}, open(os.path.join(tmp, "config", "lexicon.json"), "w"))
            ws = [{"text": ch, "start": round(0.1 * i, 1), "end": round(0.1 * i + 0.1, 1)}
                  for i, ch in enumerate("轮骨不牢")]
            json.dump({"files": [{"id": "M1", "audit": {}, "transcript": {"words": ws}}]},
                      open(os.path.join(tmp, "materials", "packs", "tp49", "pack.json"), "w"))
            json.dump({"packs": ["tp49"]},
                      open(os.path.join(tmp, "projects", "t49proj", "materials", "library.json"), "w"))
            old_llm = proofread.chat_llm
            proofread.chat_llm = lambda msgs: '[{"find":"轮骨","replace":"龙骨"}]'
            try:
                proofread.run("t49proj")   # 词表命中=auto 直改，不产生队列
            finally:
                proofread.chat_llm = old_llm
            # 第二轮：非词表修正→必入队（入队才产生队列文件）
            old_llm = proofread.chat_llm
            proofread.chat_llm = lambda msgs: '[{"find":"龙骨","replace":"轻钢"}]'
            try:
                proofread.run("t49proj")
            finally:
                proofread.chat_llm = old_llm
            in_proj = os.path.exists(os.path.join(tmp, "projects", "t49proj", "review-queue.json"))
            in_pack_dir = os.path.exists(os.path.join(tmp, "projects", "tp49", "review-queue.json"))
            q = json.load(open(os.path.join(tmp, "projects", "t49proj", "review-queue.json")))
            ok = in_proj and not in_pack_dir and len(q["items"]) == 1 and q["project"] == "t49proj"
            check("T49 proofread 队列落点=项目目录（包名≠项目名也不同步错位）",
                  ok, "in_proj=%s in_packdir=%s items=%d" % (in_proj, in_pack_dir, len(q["items"])))
        finally:
            proofread.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T49 proofread 队列落点=项目目录（包名≠项目名也不同步错位）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t50_transcode_preset_ladder():
    # 回归锚（2026-09-17）：VLM 送片 720p 转码提速档——首选 ultrafast（crf 不变=质量
    # 语义不变，只增体积）；超限回退 veryfast 重编一次，再超才抛错。锚：预设阶梯次序
    # + 回退发生 + 产物可被 ffprobe 读出时长。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import vision
        tmp = tempfile.mkdtemp(prefix="t50_")
        src = os.path.join(tmp, "s.mp4")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=2",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", src],
                       check=True)
        calls = []
        orig_run = vision.subprocess.run
        orig_max = vision.MAX_VIDEO_BYTES

        def _spy(cmd, **kw):
            calls.append(list(cmd))
            return orig_run(cmd, **kw)

        vision.subprocess.run = _spy
        try:
            out = vision._transcode_720(src, tmp)   # 正常路径：ultrafast 一轮即过
            ok_normal = ("ultrafast" in calls[0]) and os.path.exists(out) \
                        and os.path.getsize(out) <= orig_max
            q = subprocess.run([FF, "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", out], capture_output=True, text=True)
            ok_probe = float((q.stdout or "0").strip() or 0) > 1.0
            # 强制体积上限=1B：两轮预设都试过后必须抛错（证明 veryfast 回退真发生）
            vision.MAX_VIDEO_BYTES = 1
            calls.clear()
            try:
                vision._transcode_720(src, tmp)
                ok_fallback = False
            except RuntimeError:
                presets = [c[c.index("-preset") + 1] for c in calls if "-preset" in c]
                ok_fallback = presets == ["ultrafast", "veryfast"]
        finally:
            vision.subprocess.run = orig_run
            vision.MAX_VIDEO_BYTES = orig_max
        check("T50 VLM 转码预设阶梯（ultrafast 首选，超限回退 veryfast 再抛错）",
              ok_normal and ok_probe and ok_fallback,
              "normal=%s probe=%s fallback=%s" % (ok_normal, ok_probe, ok_fallback))
    except Exception as e:
        check("T50 VLM 转码预设阶梯（ultrafast 首选，超限回退 veryfast 再抛错）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t51_transcribe_concurrency():
    # 回归锚（2026-09-18 Noah 实测 3xASR+3xVLM 并发无 429 后落地）：transcribe_pack 素材级
    # 3 线程池——并发峰值必须真到 3（防退化成串行），且写回仍单写者全量（防并发改造
    # 把 [:4] 截断那类"静默丢数据"带进多线程版）。
    import tempfile, threading, time
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import transcribe, asr
        tmp = tempfile.mkdtemp(prefix="t51_")
        old_root = transcribe.ROOT
        try:
            transcribe.ROOT = tmp
            pk_dir = os.path.join(tmp, "materials", "packs", "tp51")
            os.makedirs(pk_dir)
            files = []
            for i in range(6):
                mid = "M%d" % (i + 1)
                open(os.path.join(pk_dir, mid + ".mp4"), "wb").write(b"\x00")
                files.append({"id": mid, "file": mid + ".mp4", "kind": "video", "audit": {}})
            json.dump({"files": files}, open(os.path.join(pk_dir, "pack.json"), "w"))
            active = {"n": 0, "peak": 0}
            alock = threading.Lock()

            def _fake(src, provider=None):
                with alock:
                    active["n"] += 1
                    active["peak"] = max(active["peak"], active["n"])
                time.sleep(0.3)  # 网络等待模拟：串行=1.8s+，3并发≈0.6s+
                with alock:
                    active["n"] -= 1
                return {"provider": "minimax", "tier": "sent", "text": "测试文本",
                        "words": [{"text": "测", "start": 0.0, "end": 0.5}],
                        "duration": 5.0, "at": "now"}
            old_tr = asr.transcribe
            asr.transcribe = _fake   # transcribe._one 经模块引用调用，补丁生效
            try:
                t0 = time.time()
                out = transcribe.transcribe_pack("tp51")
                wall = time.time() - t0
            finally:
                asr.transcribe = old_tr
            pk = json.load(open(os.path.join(pk_dir, "pack.json")))
            all_done = all(f["audit"].get("transcript") == "done" and f.get("transcript", {}).get("text")
                           for f in pk["files"])
            ok = (len(out) == 6 and all_done and active["peak"] >= 3 and wall < 1.5)
            check("T51 transcribe 素材级并发（峰值≥3 真并行，写回全量不丢）",
                  ok, "peak=%d wall=%.2fs done=%s" % (active["peak"], wall, all_done))
        finally:
            transcribe.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T51 transcribe 素材级并发（峰值≥3 真并行，写回全量不丢）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t52_retry429_backoff():
    # 回归锚（2026-09-18 并发配套）：asr.retry429 只对 HTTP 429 退避重试，其余异常直接抛
    # （防把"素材超长"这类业务错也裹进重试，白等 12 秒再失败）。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import asr
        import urllib.error
        calls = {"n": 0}
        old_time = asr.time
        asr.time = type("ft", (), {"sleep": staticmethod(lambda s: None)})()  # 只换 asr 命名空间里的引用，不碰真 time 模块
        try:
            def _flaky():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise urllib.error.HTTPError("u", 429, "Too Many", {}, None)
                return "ok"
            r_ok = asr.retry429(_flaky)
            n_429 = calls["n"]
            calls["n"] = 0

            def _boom():
                calls["n"] += 1
                raise urllib.error.HTTPError("u", 500, "ISE", {}, None)
            try:
                asr.retry429(_boom)
                raise_500 = "no-raise"   # 不该走到
            except urllib.error.HTTPError:
                raise_500 = "raised"
            n_500 = calls["n"]
        finally:
            asr.time = old_time
        check("T52 retry429 退避（429 重试成功，非 429 直接抛）",
              r_ok == "ok" and n_429 == 2 and raise_500 == "raised" and n_500 == 1,
              "429: %d 次, 500: %d 次" % (n_429, n_500))
    except Exception as e:
        check("T52 retry429 退避（429 重试成功，非 429 直接抛）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t53_sticker_text_templates():
    # 回归锚（2026-09-18 文字模板制）：贴纸=样式模板+≤6字短语（v1 规矩回归——文字跟内容走，
    # 不再是固定 PNG 三选一）。锚三件事：①sticker_file drawtext 现画真 PNG（透明底）+缓存命中
    # +无 text 回退 default_text；②validate text 门：>6 字剔除、≤6 字透传、缺失保留（老线兼容）。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import pipeline, draft as D
        reg = json.load(open(os.path.join(ROOT, "registry", "stickers.json"), encoding="utf-8"))
        # 2026-09-19 策展解耦：动态取首个「文字模板+默认短语」在册条目（硬编码 zhuyi 随注册表删减变 KeyError）
        _sid = next(k for k, v in reg.items() if not k.startswith("_")
                    and v.get("text_style") and v.get("default_text"))
        conf = dict(reg[_sid]); conf["id"] = _sid
        p1 = pipeline.sticker_file(conf, {"text": "横厅布局"})
        p1b = pipeline.sticker_file(conf, {"text": "横厅布局"})          # 缓存命中（同路径）
        p2 = pipeline.sticker_file(conf, {})                              # 无 text → default_text
        ok_png = os.path.exists(p1) and os.path.getsize(p1) > 1000 and (p1 == p1b) \
                 and os.path.exists(p2) and p2 != p1
        q = subprocess.run([FF, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=pix_fmt", "-of", "csv=p=0", p1], capture_output=True, text=True)
        ok_alpha = "rgba" in (q.stdout or "")

        mats = {"CL": {"id": "CL", "kind": "video", "usable": True, "duration": 10,
                       "visual": {"content_type": "narration", "desc": "干净"}}}
        def _st(text):
            # at_word 置空：T53 只锚 text 门（at_word 词轨成员门是 T56 的锚，不在此混测）
            return {"asset": _sid, "text": text, "at_word": "", "duration": 1.0, "pos": "top-center"}
        _tr = [{"role": "A", "source_id": "CL", "src_in": 0, "duration": 4}]
        dr = {"beats": [
            {"story": "a", "tracks": _tr, "effects": {"stickers": [_st("超过六个字的长短语")]}},   # 8字 → 剔除
            {"story": "b", "tracks": _tr, "effects": {"stickers": [_st("得房率高")]}},             # 4字 → 透传
            {"story": "c", "tracks": _tr, "effects": {"stickers": [_st("")] }},                    # 缺 text → 保留
            {"story": "d", "tracks": _tr, "effects": {"stickers": [dict(_st("好"), asset="表外id")]}},  # 表外 id → 剔除
        ]}
        beats = D.validate(dr, mats, [])
        sk = [b["effects"]["stickers"] for b in beats]
        ok_gate = (len(beats) == 4 and len(sk[0]) == 0
                   and sk[1][0]["text"] == "得房率高"
                   and len(sk[2]) == 1 and sk[2][0]["text"] == ""
                   and len(sk[3]) == 0)
        check("T53 贴纸文字模板（drawtext 现画+缓存+回退；validate ≤6字硬门）",
              ok_png and ok_alpha and ok_gate,
              "png=%s alpha=%s gate=%s" % (ok_png, ok_alpha, ok_gate))
    except Exception as e:
        check("T53 贴纸文字模板（drawtext 现画+缓存+回退；validate ≤6字硬门）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t54_overlap_aware_fix():
    # 回归锚（2026-09-18 ruxuan-02 实锤）：R1 建议曾是纯死尾算术（词尾+0.35），不知转场重叠——
    # 建议值 7.2 令尾词「。」(6.70 起) 被幕内钳制整词静默丢弃（残余<0.08s）→ 字幕页跨幕合并照样过 QC。
    # 两修对锚：①QC R1/R2 建议感知 dt（存活下限=末词start+dt+0.08；floor 超死尾上限→拒给数字指路改转场）
    # ②remap_words 钳制丢词落 plan.word_drops 有声（决策点不再静默降级）。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import qc, pipeline
        rules = qc.load_rules()
        words = [{"text": t, "start": s, "end": e} for t, s, e in
                 [("做", 0.0, 0.2), ("好", 6.0, 6.4), ("年", 6.4, 6.72), ("。", 7.0, 7.2)]]
        f = {"id": "M", "duration": 10, "transcript": {"words": words}}
        ow = [{"t": w["text"], "s": w["start"], "e": w["end"]} for w in words]
        tr = {"role": "A", "source_id": "M", "src_in": 0, "duration": 8.5}
        sl = {"beats": [
            {"no": 1, "tracks": [tr], "transition_out": "crossDissolve",
             "narration": {"mode": "original", "words": ow}},
            {"no": 2, "tracks": [dict(tr, duration=3)], "transition_out": None}]}
        tmap = qc._eff_trans_map(sl, os.path.join(ROOT, "projects"))  # 真注册表 crossDissolve=0.5
        # ① R1：词尾 7.2，owords 末词「。」7.0 起 → floor 7.0+0.5+0.08=7.58 → 建议 7.6 含避让说明
        r1 = next(v for v in qc.check_windows(sl, {"M": f}, rules, tmap) if v["rule"] == "R1")
        ok_r1 = "7.6" in (r1["suggested_fix"] or "") and "重叠" in r1["suggested_fix"]
        # 冲突档：末词起点 7.8 → floor 8.38 > 词尾 7.2+死尾上限 0.8 → 拒给数字，指路改短转场/砍尾词
        sl2 = json.loads(json.dumps(sl))
        sl2["beats"][0]["narration"]["words"][-1] = {"t": "。", "s": 7.8, "e": 8.0}
        r1c = next(v for v in qc.check_windows(sl2, {"M": f}, rules, tmap) if v["rule"] == "R1")
        ok_conflict = "改短转场" in (r1c["suggested_fix"] or "") and "7.6" not in r1c["suggested_fix"]
        # R2 同族：出点 6.5 咬「年」(6.4-6.72)，基础延展 6.75 但 floor=6.4+0.5+0.08=6.98 → 建议 7.0
        sl3 = json.loads(json.dumps(sl))
        sl3["beats"][0]["tracks"][0]["duration"] = 6.5
        r2 = next(v for v in qc.check_windows(sl3, {"M": f}, rules, tmap) if v["rule"] == "R2")
        ok_r2 = "7.0" in (r2["suggested_fix"] or "") and "避让" in r2["suggested_fix"]
        # ② pipeline：owords 末词「。」7.0 起，幕 dur 7.2 → tl1=7.2-0.5=6.7 → 残余<0 → 必丢且有声
        segs = [{"no": 1, "tl_in": 0, "dur": 7.2, "owords": ow}, {"no": 2, "tl_in": 6.7, "dur": 3.0}]
        ws, drops = pipeline.remap_words([], [{"source_id": "M", "src_in": 0, "duration": 7.2}],
                                         segs, 9.7, {"M": f})
        ok_drop = (len(drops) == 1 and drops[0]["word"] == "。" and drops[0]["beat"] == 1
                   and abs(drops[0]["at"] - 7.0) < 0.01
                   and all(w["t"] != "。" for w in ws))
        check("T54 R1/R2 建议转场重叠避让 + 钳制丢词有声（ruxuan-02）",
              ok_r1 and ok_conflict and ok_r2 and ok_drop,
              "r1=%s conflict=%s r2=%s drop=%s" % (ok_r1, ok_conflict, ok_r2, ok_drop))
    except Exception as e:
        check("T54 R1/R2 建议转场重叠避让 + 钳制丢词有声（ruxuan-02）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t55_cover_prerender_flow():
    # 回归锚（2026-09-18 Noah 裁定封面改版）：封面生成+体检前置于渲染。锚四件：
    # ①beat-frame=指定幕帧+title_text 本地合成（1080×1920 收口，上部两行标题真实落像素）；
    # ②cover_check 缺失文件如实 False；③orchestrate cover 环节推导（封面在场且新于故事线=done，
    #   output-frame 策略=na——唯一渲染后收口）；④pipeline.py cover 子命令 COVER OK 闭环。
    import tempfile
    pl_py = os.path.join(ROOT, "autocut3", "pipeline.py")
    tmp = tempfile.mkdtemp(prefix="t55_")
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        os.makedirs("projects/t55/materials"); os.makedirs("projects/t55/storylines")
        mp4 = "projects/t55/materials/M1.mp4"   # 走 library.sources（按项目目录解析）——pack 解析锚定仓库 ROOT，tmp 夹具进不去
        r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc=size=1080x1920:rate=10:duration=5",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", mp4],
                           capture_output=True, text=True)
        assert r.returncode == 0, "夹具合成失败: %s" % (r.stderr or "")[-120:]
        json.dump({"sources": [{"id": "M1", "file": "materials/M1.mp4"}],
                   "files": []},
                  open("projects/t55/materials/library.json", "w", encoding="utf-8"))
        sl = {"title": "t55",
              "meta": {"cover": {"strategy": "beat-frame", "beat_no": 1, "at": 2.0,
                                 "title_text": "做装修销售我从不拿低价吸引客户"}},
              "beats": [{"no": 1, "story": "甲乙", "narration": {"mode": "original"},
                         "tracks": [{"role": "A", "source_id": "M1", "src_in": 0, "duration": 4}]}]}
        json.dump(sl, open("projects/t55/storylines/t55.json", "w", encoding="utf-8"))
        r = subprocess.run([sys.executable, pl_py, "cover", os.path.abspath("projects/t55"), "t55"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        cover = "projects/t55/cover/cover-t55.jpg"
        ok_cli = r.returncode == 0 and "COVER OK" in (r.stdout or "") and os.path.exists(cover)
        ffprobe = FFMPEG.replace("ffmpeg", "ffprobe")
        q = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", cover],
                           capture_output=True, text=True)
        try:
            w, h = (int(x) for x in (q.stdout or "").strip().split(","))
            ok_portrait = w == 1080 and h == 1920
        except Exception:
            ok_portrait = False
        # ①标题真实落像素：上部标题带必含高亮像素（纯 testsrc 顶部是暗色块）
        s = subprocess.run([FFMPEG, "-loglevel", "error", "-i", cover,
                            "-vf", "crop=1080:320:0:100,signalstats,metadata=print:key=lavfi.signalstats.YMAX:file=-",
                            "-f", "null", "-"], capture_output=True, text=True)
        ymax = max((int(m) for m in re.findall(r"YMAX=(\d+)", s.stdout or "")), default=0)
        ok_title = ymax > 200
        try:
            import importlib
            sys.path.insert(0, os.path.join(ROOT, "autocut3"))
            import orchestrate
            importlib.reload(orchestrate)
            st = orchestrate.status(os.path.abspath("projects/t55"))
            cov = next(x for x in st["steps"] if x["step"] == "cover")
            ok_step = cov["status"] == "done"
            sl["meta"]["cover"]["strategy"] = "output-frame"
            json.dump(sl, open("projects/t55/storylines/t55.json", "w", encoding="utf-8"))
            st2 = orchestrate.status(os.path.abspath("projects/t55"))
            cov2 = next(x for x in st2["steps"] if x["step"] == "cover")
            ok_na = cov2["status"] == "na"
            import pipeline as _PL
            ok_chk = _PL.cover_check("projects/t55/cover/nope.jpg") == (False, "封面文件缺失")
        finally:
            sys.path = [p for p in sys.path if "autocut3" not in p]
        check("T55 封面前置收口（beat-frame+标题合成+cover_check+环节推导）",
              ok_cli and ok_portrait and ok_title and ok_step and ok_na and ok_chk,
              "cli=%s portrait=%s title_YMAX=%d step=%s na=%s chk=%s"
              % (ok_cli, ok_portrait, ymax, ok_step, ok_na, ok_chk))
    except Exception as e:
        check("T55 封面前置收口（beat-frame+标题合成+cover_check+环节推导）",
              False, "异常: %s" % str(e)[:140])
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def t56_at_word_gate():
    # 回归锚（2026-09-18 agent P1 实锤）：LLM 产出多字 at_word（「低价」「评论区」），词轨
    # 单字粒度 substring 永不匹配 → word_time 静默回退 0.4s 零告警。双锚：
    # ①draft validate 门——at_word 不在本幕词轨文本（A 轨转写窗口+story）= 剔除贴纸；
    # ②word_time 两级匹配——多字序列拼出命中首字起点，不命中 None。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import pipeline, draft as D
        # 2026-09-19 策展解耦：动态取在册贴纸 id（validate 白名单按注册表实时判，硬编码已删 id 会被整条剔除）
        _reg = json.load(open(os.path.join(ROOT, "registry", "stickers.json"), encoding="utf-8"))
        _sid = next(k for k in _reg if not k.startswith("_"))
        words = [{"t": c, "s": 1.0 + i * 0.4, "e": 1.4 + i * 0.4}
                 for i, c in enumerate("我从不拿低价吸引客户")]
        seg = {"tl_in": 0.0, "dur": 10.0, "no": 1}
        ok_wt = (pipeline.word_time(words, "低", seg) == 2.6
                 and pipeline.word_time(words, "低价", seg) == 2.6   # 多字序列命中（修复前 None）
                 and pipeline.word_time(words, "评论区", seg) is None)
        mats = {"M1": {"id": "M1", "kind": "video", "usable": True, "duration": 10,
                       "visual": {"content_type": "narration", "desc": "干净"},
                       "transcript": {"words": [{"text": c, "start": 0.1 * i, "end": 0.1 * i + 0.1}
                                                for i, c in enumerate("我从不拿低价吸引客户")]}}}
        def _st(aw):
            return {"asset": _sid, "text": "要点", "at_word": aw, "duration": 1.0, "pos": "top-center"}
        _tr = [{"role": "A", "source_id": "M1", "src_in": 0, "duration": 10}]
        dr = {"beats": [
            {"story": "s", "tracks": _tr, "effects": {"stickers": [_st("低价")]}},      # 在词轨文本 → 保留
            {"story": "s", "tracks": _tr, "effects": {"stickers": [_st("评论区")]}},    # 不在 → 剔除
            {"story": "s", "tracks": _tr, "effects": {"stickers": [_st("")] }},         # 空 at_word → 保留（老线兼容）
        ]}
        beats = D.validate(dr, mats, [])
        sk = [b["effects"]["stickers"] for b in beats]
        ok_gate = (len(beats) == 3 and len(sk[0]) == 1 and len(sk[1]) == 0 and len(sk[2]) == 1)
        check("T56 at_word 触发词门（draft 词轨成员校验 + word_time 序列匹配）",
              ok_wt and ok_gate, "wt=%s gate=%s" % (ok_wt, ok_gate))
    except Exception as e:
        check("T56 at_word 触发词门（draft 词轨成员校验 + word_time 序列匹配）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t57_vadwords_cross_span():
    # 回归锚（2026-09-18 agent P2 实锤 b4「流」0.42s 静音全程高亮）：字符区间跨语音段界
    # → 卡拉OK fill 窗盖住段间静音+下段头部。锚：①allocate 跨段字符截到起始段尾且登记 cross；
    # ②dub 首幕不再 words/cross 未绑定（原：NameError 或静默沿用上一幕陈词）。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import vadwords
        spans = [(0.0, 1.0), (3.0, 4.0)]
        text = "零一二三四五六七八九"
        ci = []
        out = vadwords.allocate([(c, 0, 0) for c in text], spans, cross_out=ci)
        crossing = [c for c, s, e in out if s < 1.0 < e]          # 修复前「五」[1.0,3.2] 盖满静音
        ok_alloc = (not crossing) and [text[i] for i in ci] == ["五"]
        tmp = tempfile.mkdtemp(prefix="t57_")
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            os.makedirs("projects/t57/storylines")
            wav = "projects/t57/dub.wav"
            r = subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                                "-f", "lavfi", "-i", "sine=frequency=440:duration=4", wav],
                               capture_output=True, text=True)
            assert r.returncode == 0, "夹具合成失败"
            json.dump({"beats": [{"no": 1, "story": "甲乙丙丁",
                                  "narration": {"mode": "dub", "audio": "dub.wav"},
                                  "tracks": []}]},
                      open("projects/t57/storylines/t57.json", "w", encoding="utf-8"))
            old_argv = sys.argv
            sys.argv = ["vadwords.py", "t57", "--story", "t57"]
            vadwords.main()   # dub 首幕：修复前此处 NameError
            sys.argv = old_argv
            sl2 = json.load(open("projects/t57/storylines/t57.json"))
            rep = json.load(open("projects/t57/vad-report.json"))
            w1 = "".join(w["t"] for w in sl2["beats"][0]["narration"]["words"])
            ok_dub = (w1 == "甲乙丙丁" and isinstance(rep[0].get("cross_span"), list))
            check("T57 vadwords 跨段字符段尾截断 + dub 首幕词轨补齐",
                  ok_alloc and ok_dub, "alloc=%s dub=%s words=%s" % (ok_alloc, ok_dub, w1))
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T57 vadwords 跨段字符段尾截断 + dub 首幕词轨补齐",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t58_proofread_degenerate_guard():
    # 回归锚（2026-09-18 agent P3 实锤 3/3）：M3 偶把结论写进 think 块、正文只剩孤立「[」
    # → 切片表达式 ValueError 报"substring not found"误导归因。锚：退化正文单独归因
    # "引擎退化输出"（不炸、不改词轨）；空清单 [] 合法放行。
    import tempfile, io, contextlib
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import proofread
        tmp = tempfile.mkdtemp(prefix="t58_")
        old_root = proofread.ROOT
        try:
            proofread.ROOT = tmp
            os.makedirs(os.path.join(tmp, "materials", "packs", "tpid"))
            os.makedirs(os.path.join(tmp, "config"))
            os.makedirs(os.path.join(tmp, "projects", "tproj", "materials"))
            json.dump({"terms": ["龙骨"]}, open(os.path.join(tmp, "config", "lexicon.json"), "w"),
                      ensure_ascii=False)
            ws = [{"text": c, "start": 0.2 * i, "end": 0.2 * i + 0.2}
                  for i, c in enumerate("轮骨不牢")]
            json.dump({"files": [{"id": "T58M", "duration": 1.0, "audit": {},
                                  "transcript": {"words": ws}}]},
                      open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json"), "w"),
                      ensure_ascii=False)
            json.dump({"packs": ["tpid"]},
                      open(os.path.join(tmp, "projects", "tproj", "materials", "library.json"), "w"),
                      ensure_ascii=False)
            old_llm = proofread.chat_llm
            buf = io.StringIO()
            proofread.chat_llm = lambda msgs: "["     # 退化正文（think 块吞了结论）
            with contextlib.redirect_stdout(buf):
                proofread.run("tproj", dry=True)      # 修复前：ValueError("substring not found")
            pk = json.load(open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json")))
            ok_guard = ("".join(w["text"] for w in pk["files"][0]["transcript"]["words"]) == "轮骨不牢"
                        and "引擎退化输出" in buf.getvalue())
            proofread.chat_llm = lambda msgs: "[]"    # 合法空清单：零修正，不误报退化
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                proofread.run("tproj", dry=True)
            ok_empty = "引擎退化输出" not in buf2.getvalue()
            check("T58 proofread 引擎退化正文前置拦截（孤立「[」不再误导归因）",
                  ok_guard and ok_empty, "guard=%s empty=%s" % (ok_guard, ok_empty))
        finally:
            proofread.ROOT = old_root
            proofread.chat_llm = old_llm
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T58 proofread 引擎退化正文前置拦截（孤立「[」不再误导归因）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t59_transition_reserve():
    # 回归锚（2026-09-18 Noah 裁定·转场预留区）：幕边界只落静音——dt_eff=min(注册dt,尾侧静音,头侧静音)
    # 随素材伸缩，两侧不足 0.2s 降级直切；出点延至词尾+dt_eff、入点回退 dt_eff（owords/sfx at 平移）。
    # 另锚短词不再误杀：全窗内 0.04s 单字照常上屏（zhanglingxiang-02 实锤 8 条"转场钳词"实为短词漏字）。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import pipeline
        tmp = tempfile.mkdtemp(prefix="t59_")
        old_root = pipeline.ROOT
        try:
            pipeline.ROOT = tmp
            os.makedirs(os.path.join(tmp, "materials", "packs", "t59pack"))
            os.makedirs(os.path.join(tmp, "projects", "t59", "storylines"))
            os.makedirs(os.path.join(tmp, "projects", "t59", "materials"))
            os.makedirs(os.path.join(tmp, "registry"))
            W = lambda *tse: [{"text": t, "start": s, "end": e} for t, s, e in tse]
            json.dump({"files": [
                {"id": "M-A", "file": "a.mp4", "kind": "video", "duration": 10.0,
                 "transcript": {"words": W(("今", 1.0, 1.4), ("的", 2.0, 2.04), ("查", 3.0, 3.4), ("好", 5.0, 5.4))}},
                {"id": "M-B", "file": "b.mp4", "kind": "video", "duration": 10.0,
                 "transcript": {"words": W(("吗", 1.0, 1.4), ("呢", 1.6, 1.9), ("吧", 2.1, 2.4))}},
                {"id": "M-C", "file": "c.mp4", "kind": "video", "duration": 10.0,
                 "transcript": {"words": W(("啊", 0.1, 0.4), ("好", 0.6, 1.0))}}]},
                open(os.path.join(tmp, "materials", "packs", "t59pack", "pack.json"), "w"),
                ensure_ascii=False)
            json.dump({"packs": ["t59pack"]},
                      open(os.path.join(tmp, "projects", "t59", "materials", "library.json"), "w"))
            json.dump({"crossDissolve": {"type": "xfade", "preset": "fade", "duration": 0.5}},
                      open(os.path.join(tmp, "registry", "transitions.json"), "w"))
            json.dump({"vertical": {"width": 1080, "height": 1920}},
                      open(os.path.join(tmp, "registry", "formats.json"), "w"))
            # 三幕：幕1(M-A 尾词查@3.4，窗出 3.6→预留延至 3.9)；幕2(M-B 头词吗@1.0，窗入 0.6
            # →回退至 0.5；其转场边界因 M-B 尾侧 0.2s + M-C 头侧 0.1s 不足 → 降级直切)；幕3 不动
            sl = {"title": "t59", "meta": {"audio": {}}, "beats": [
                {"no": 1, "transition_out": "crossDissolve",
                 "tracks": [{"role": "A", "source_id": "M-A", "src_in": 1.0, "duration": 2.6}],
                 "narration": {"mode": "original", "words": [
                     {"t": "今", "s": 0.0, "e": 0.4}, {"t": "的", "s": 1.0, "e": 1.04},
                     {"t": "查", "s": 2.0, "e": 2.4}]}},
                {"no": 2, "transition_out": "crossDissolve",
                 "tracks": [{"role": "A", "source_id": "M-B", "src_in": 0.6, "duration": 1.4}],
                 "narration": {"mode": "original", "words": [
                     {"t": "吗", "s": 0.4, "e": 0.8}, {"t": "呢", "s": 1.0, "e": 1.3}]},
                 "effects": {"sfx": [{"asset": "success", "at": 0.4}]}},
                {"no": 3, "transition_out": None,
                 "tracks": [{"role": "A", "source_id": "M-C", "src_in": 0.2, "duration": 1.5}],
                 "narration": {"mode": "original", "words": [
                     {"t": "啊", "s": -0.1, "e": 0.2}, {"t": "好", "s": 0.4, "e": 0.8}]}}]}
            json.dump(sl, open(os.path.join(tmp, "projects", "t59", "storylines", "t59.json"), "w"),
                      ensure_ascii=False)
            plan, _ = pipeline.build_plan(os.path.join(tmp, "projects", "t59"), "t59")
            s1, s2, s3 = plan["segments"]
            ok_tail = (s1["trans_d"] == 0.5 and abs(s1["dur"] - 2.9) < 0.01)      # 出点 3.6→词尾3.4+0.5
            ok_head = (abs(s2["src_in"] - 0.5) < 0.01 and abs(s2["dur"] - 1.5) < 0.01
                       and abs(s2["owords"][0]["s"] - 0.5) < 0.01                 # owords 平移 0.4→0.5
                       and abs(s2["effects"]["sfx"][0]["at"] - 0.5) < 0.01)       # sfx at 平移
            ok_degrade = ("transition" not in s2 and "trans_d" not in s2
                          and "b2" not in (plan.get("transitions") or {}))        # 0.1s<0.2 → 直切
            ok_no_shift = abs(s3["src_in"] - 0.2) < 0.01                          # 降级后头预留不传播
            ok_words = (not (plan.get("word_drops") or [])
                        and any(w["t"] == "的" for w in plan["words"])            # 0.04s 短词上屏
                        and abs(s2["tl_in"] - 2.4) < 0.01)                        # tl1=0+2.9-0.5
            check("T59 转场预留区（dt_eff 伸缩/降级直切/短词上屏/零钳词）",
                  ok_tail and ok_head and ok_degrade and ok_no_shift and ok_words,
                  "tail=%s head=%s degrade=%s noshift=%s words=%s" % (
                      ok_tail, ok_head, ok_degrade, ok_no_shift, ok_words))
        finally:
            pipeline.ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T59 转场预留区（dt_eff 伸缩/降级直切/短词上屏/零钳词）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t60_material_pipeline():
    # 回归锚（2026-09-19 素材级流水线 process.py）：每条素材 ASR 完立即链发视觉（词轨随行），
    # 双端点双池、单写者末尾一次写。全离线零计费（asr.transcribe/vision.understand 打桩）。
    # 锚四义：①链路=视觉拿到该素材 ASR 词轨 ②ASR 失败视觉仍发（words=None，与顺序跑一致）
    # ③图片跳 ASR 直接视觉、音频 visual=n/a ④幂等重跑全跳 + 锁释放（防死锁残留）。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import asr, vision, process
        tmp = tempfile.mkdtemp(prefix="t60_")
        old_root = process.ROOT
        old_tr, old_vi = asr.transcribe, vision.understand
        W = [{"text": "你", "start": 0.1, "end": 0.4}, {"text": "好", "start": 0.5, "end": 0.8}]
        calls = {"asr": [], "vi": []}
        try:
            process.ROOT = tmp
            pdir = os.path.join(tmp, "materials", "packs", "t60pack")
            os.makedirs(pdir)
            for n in ("v1.mp4", "v2.mp4", "a1.mp3", "i1.jpg"):
                open(os.path.join(pdir, n), "wb").write(b"x")
            json.dump({"files": [
                {"id": "M-V1", "file": "v1.mp4", "kind": "video", "review": "pending-review"},
                {"id": "M-V2", "file": "v2.mp4", "kind": "video", "review": "pending-review"},
                {"id": "M-A1", "file": "a1.mp3", "kind": "audio", "review": "pending-review"},
                {"id": "M-I1", "file": "i1.jpg", "kind": "image", "review": "pending-review"}]},
                open(os.path.join(pdir, "pack.json"), "w"), ensure_ascii=False)

            def fake_asr(src, provider=None, cache_dir=None):
                calls["asr"].append(os.path.basename(src))
                if src.endswith("v2.mp4"):
                    raise RuntimeError("ASR 桩失败")
                return {"provider": "stub", "tier": "word", "text": "你好", "words": W,
                        "duration": 1.0, "at": "2026-09-19T00:00:00"}

            def fake_vi(src, kind, provider=None, words=None, dup=0):
                calls["vi"].append({"src": os.path.basename(src), "kind": kind,
                                    "words": words, "dup": dup})
                return {"desc": "桩描述", "ocr": [], "usage": "broll", "frames": 3,
                        "content_type": "broll", "provider": "stub", "at": "2026-09-19T00:00:00"}

            asr.transcribe = fake_asr
            vision.understand = fake_vi
            res = process.process_pack("t60pack")

            v1_vi = next(c for c in calls["vi"] if c["src"] == "v1.mp4")
            v2_vi = next(c for c in calls["vi"] if c["src"] == "v2.mp4")
            ok_chain = v1_vi["words"] == W                       # ①视觉吃到本素材词轨
            ok_fail_dispatch = (v2_vi["words"] is None           # ②ASR 失败视觉仍发
                                and not res["asr"]["M-V2"]["ok"])
            ok_kinds = (calls["asr"].count("i1.jpg") == 0         # ③图片不进 ASR
                        and next(c for c in calls["vi"] if c["src"] == "i1.jpg")["kind"] == "image"
                        and "M-A1" not in [c["src"] for c in calls["vi"]])
            pk = json.load(open(os.path.join(pdir, "pack.json"), encoding="utf-8"))
            f = {x["id"]: x for x in pk["files"]}
            ok_pack = (f["M-V1"]["audit"]["transcript"] == "done" and f["M-V1"]["audit"]["visual"] == "done"
                       and f["M-V1"]["review"] == "reviewed"     # 双审计齐=自动过审
                       and f["M-V2"]["audit"]["transcript"].startswith("error:")
                       and f["M-V2"]["audit"]["visual"] == "done"  # 失败视觉照发照落
                       and f["M-A1"]["audit"]["visual"] == "n/a" and f["M-A1"]["audit"]["transcript"] == "done"
                       and f["M-I1"]["audit"]["visual"] == "done" and "transcript" not in f["M-I1"]["audit"])
            # ④幂等=done 项不重跑（error 项理应重试——恢复语义）；锁没放第二次调用会死锁挂死
            res2 = process.process_pack("t60pack")
            ok_idem = (calls["asr"].count("v1.mp4") == 1 and calls["asr"].count("a1.mp3") == 1
                       and len([c for c in calls["vi"] if c["src"] == "v1.mp4"]) == 1
                       and len([c for c in calls["vi"] if c["src"] == "i1.jpg"]) == 1
                       and set(res2["asr"]) == {"M-V2"} and not res2["visual"])
            check("T60 素材级流水线（词轨链发/失败仍发/分派/幂等+锁）",
                  ok_chain and ok_fail_dispatch and ok_kinds and ok_pack and ok_idem,
                  "chain=%s faildisp=%s kinds=%s pack=%s idem=%s" % (
                      ok_chain, ok_fail_dispatch, ok_kinds, ok_pack, ok_idem))
        finally:
            process.ROOT = old_root
            asr.transcribe, vision.understand = old_tr, old_vi
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T60 素材级流水线（词轨链发/失败仍发/分派/幂等+锁）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t61_advance_dynamic_reestimate():
    # 回归锚（2026-09-19 todo 快照冻结修根，ExFlower agent 实锤）：na→pending 翻转环节
    # （proofread 判据=词轨存在）在快照瞬间是 na 不入 todo，transcribe 落轨后翻 pending——
    # 旧 for-快照 永不回头漏跑。全打桩零计费。锚三义：①proofread 被自动补跑 ②先于 dossier/
    # disposition（设计序保真——disposition 消费校对后词轨）③单次执行守卫无重跑 + 门保持。
    import tempfile
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import orchestrate as ORCH
        import proofread, disposition
        tmp = tempfile.mkdtemp(prefix="t61_")
        old_root = ORCH.ROOT
        old_status, old_run = ORCH.status, ORCH._run
        old_pf, old_disp = proofread.run, disposition.build
        EXEC = []   # 执行顺序（可含重复=违例）
        try:
            ORCH.ROOT = tmp
            pd = os.path.join(tmp, "projects", "t61")
            os.makedirs(os.path.join(pd, "materials"))
            json.dump({"packs": []}, open(os.path.join(pd, "materials", "library.json"), "w"))

            def fake_run(cmd, label):   # 子进程环节全打桩：process/understand/dossier/vadwords…
                k = os.path.basename(cmd[1]).replace(".py", "")
                EXEC.append("transcribe" if k == "process" else k)  # 流水线步=转写环节的执行体

            def fake_status(project):
                tr = "transcribe" in EXEC          # process.py 流水线步=转写+视觉一起落
                vi = tr                            # 流水线一并完成
                def s(step, done_cond, na_cond=None):
                    if done_cond:
                        return "done"
                    if na_cond:
                        return "na"
                    return "pending"
                steps = [
                    ("mount", "done", True),
                    ("transcribe", "transcribe" in EXEC, None),
                    ("understand", vi, None),
                    ("digest", True, True),        # 桩语义：无包可盘点 na
                    ("proofread", "proofread" in EXEC, not tr),   # 无词轨 na→落轨后 pending（翻转源）
                    ("dossier", "dossier" in EXEC, None),
                    ("disposition", "disposition" in EXEC, None),
                    ("storyline", False, None),    # 恒 pending：门必停
                    ("vadwords", False, None), ("dubfit", False, None),
                    ("dubgate", False, None), ("cover", False, None),
                    ("render", False, None), ("qc", False, None),
                    ("aigen", False, True),
                ]
                sts = []
                for k, dc, nc in steps:
                    if dc:
                        v = "done"
                    elif nc:
                        v = "na"
                    else:
                        v = "pending"
                    sts.append({"step": k, "name": k, "status": v, "detail": "桩"})
                nxt = next((x["step"] for x in sts if x["status"] == "pending"), None)
                return {"project": "t61", "steps": sts, "next": nxt}

            def fake_pf(name):
                EXEC.append("proofread")

            def fake_disp(name):
                EXEC.append("disposition")

            ORCH._run, ORCH.status = fake_run, fake_status
            proofread.run, disposition.build = fake_pf, fake_disp
            ret = ORCH.advance("t61")   # 无 intent：故事线门必停

            ok_flip = "proofread" in EXEC                                   # ①补跑（旧代码必缺）
            ok_order = (EXEC.index("proofread") < EXEC.index("dossier") < EXEC.index("disposition"))  # ②设计序
            ok_once = len(EXEC) == len(set(EXEC))                           # ③单次守卫
            ok_gate = ret["next"] == "storyline" and "storyline" not in EXEC  # 门保持（无 draft 调用）
            check("T61 advance 动态重估（proofread 翻转补跑/设计序/单次+门）",
                  ok_flip and ok_order and ok_once and ok_gate,
                  "flip=%s order=%s once=%s gate=%s exec=%s" % (
                      ok_flip, ok_order, ok_once, ok_gate, EXEC))
        finally:
            ORCH.ROOT = old_root
            ORCH.status, ORCH._run = old_status, old_run
            proofread.run, disposition.build = old_pf, old_disp
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("T61 advance 动态重估（proofread 翻转补跑/设计序/单次+门）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


# ---------------------------------------------------------------- T62 字幕分页结构信号（2026-09-20 根修）
def t62_structural_pagination():
    # 回归锚（2026-09-20 王亚伦片 35 页 13 违规页根修）：断页从韵律信号（gap 停顿，
    # 快说慢说切点漂移）换轨到文本结构信号（句界/句内标点/禁起字）。全离线合成词轨。
    # 锚六义：①句内标点优先切点（「目的|地」劈词病灶）②整句不劈（「做|的」/13 字句
    # 在 14 字档整句保住）③短句合并不独立闪页（「会查。」）④时序不变性：×10 慢说/
    # 逐字前 5s 气口轰炸，页文本必须一字不变（韵律不动结构=换轨本身的锚）
    # ⑤不变式：页首非标点/页宽不超/片长≥3/页首非禁起字 ⑥确定性（R10 重放根基）。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import pipeline

        def mk(text, per=0.3):
            return [(ch, round(i * per, 2), round((i + 1) * per, 2)) for i, ch in enumerate(text)]

        def txt(pages):
            return ["".join(c[0] for c in pg) for pg in pages]

        # ①句内标点优先：旧逻辑实锤劈「目的|地」
        ok1 = txt(pipeline.build_pages(mk("开船的时候航向偏一度，目的地相差十万八千里。"), 14)) \
            == ["开船的时候航向偏一度，", "目的地相差十万八千里。"]
        # ②整句不劈：「说明咱们做|的」病灶 / 13 字句 14 字档整句
        ok2 = txt(pipeline.build_pages(mk("说明咱们做的没问题。"), 14)) == ["说明咱们做的没问题。"]
        ok3 = txt(pipeline.build_pages(mk("人这一辈子最想靠的岸是家。"), 14)) == ["人这一辈子最想靠的岸是家。"]
        # ③短句合并：「会查。」不再独立闪页；句末标点后接续新句合法（两句同页形态）
        t3 = txt(pipeline.build_pages(mk("所以每个工地我都会跑，都会查。在海上我学了一件事，"), 14))
        ok4 = t3 == ["所以每个工地我都会跑，", "都会查。在海上我学了一件事，"]
        # ④时序不变性：同一文本三种韵律 → 页文本一字不变
        base = mk("几年后我在舟山装了几百套房子。其实我在海上漂了几年，想明白了一件事。", 0.25)
        slow = [(c, round(s * 10, 2), round(e * 10, 2)) for c, s, e in base]
        gappy, tt = [], 0.0
        for c, _s, _e in base:
            gappy.append((c, round(tt + 5, 2), round(tt + 5.2, 2)))   # 每字前塞 5s 气口
            tt += 5.3
        ok5 = (txt(pipeline.build_pages(base, 14)) == txt(pipeline.build_pages(slow, 14))
               == txt(pipeline.build_pages(gappy, 14)))
        # ⑤不变式：页首非标点/非禁起字、页宽不超、片长≥3（无标点 15 字句）
        t5 = txt(pipeline.build_pages(mk("我想住在大房子的院子里每天看书"), 14))
        ok6 = (len(t5) == 2 and all(x and x[0] not in pipeline.PUNCT
                                    and x[0] not in pipeline.NO_START
                                    and 3 <= len(x) <= 14 for x in t5))
        # ⑤b 禁起字软约束定向：填满切点撞「的」必须让位
        t6 = txt(pipeline.build_pages(mk("九个字凑一凑凑一的字的后八字凑凑"), 14))
        ok7 = len(t6) == 2 and not any(x.startswith("的") for x in t6)
        # ⑥确定性
        ok8 = pipeline.build_pages(base, 14) == pipeline.build_pages(base, 14)
        check("T62 字幕分页结构信号（句界/整句/合并/时序不变/禁起字/确定性）",
              ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8,
              "句内标点=%s 整句=%s/%s 合并=%s 时序不变=%s 不变式=%s 禁起=%s 确定性=%s" % (
                  ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8))
    except Exception as e:
        check("T62 字幕分页结构信号（句界/整句/合并/时序不变/禁起字/确定性）",
              False, "异常: %s" % str(e)[:140])
    finally:
        sys.path = [p for p in sys.path if "autocut3" not in p]


def t34_narration_vocab_guard():
    # 回归锚（2026-09-15 维护者 实锤）：前端 enums.json narration_modes 词汇表曾与后端脱钩——
    # 前端用 tts、后端全家（draft/vadwords/dubfit/dubgate/pipeline）用 dub。脱钩双向错：
    # ① storyline mode=dub 时前端 select 无匹配项→回退显示"视频原声"（维护者 看到原声却渲出 TTS）；
    # ② 用户手选"AI 旁白"存成 tts→pipeline 不认→静默渲原声。此处钉死两端同一词汇表。
    en = json.load(open(os.path.join(ROOT, "registry", "enums.json"), encoding="utf-8"))
    ids = sorted(m["id"] for m in en.get("narration_modes") or [])
    check("T34 旁白词汇表两端一致（enums ≡ pipeline 消费集 original/dub/none）",
          ids == ["dub", "none", "original"], "enums=%s" % ids)


def t35_bgm_segment_render():
    # 回归锚（2026-09-15 维护者 #7）：BGM 段落选择+段落 loop 真渲染。
    # 判别设计：2s 曲 = [0-0.2 静音 | 0.2-0.9 蜂鸣 | 0.9-2 静音]，段落 hook=0.2-0.9，loop=false，幕长 4s——
    # 正确路径=aloop 不启用/apad 补静音 → 1.5s 后恒静；若错回整条语义（stream_loop 整曲循环）→ 蜂鸣每 2s 重复出现。
    reg_p = os.path.join(ROOT, "registry", "bgm.json")
    mp3 = os.path.join(ROOT, "assets", "music", "_t35.mp3")
    old_reg = open(reg_p, encoding="utf-8").read()
    try:
        reg = json.loads(old_reg)
        reg["bgm_t35seg"] = {"file": "assets/music/_t35.mp3", "name": "T35", "loop": True,
                             "segments": [{"name": "hook", "in": 0.2, "out": 0.9, "desc": "蜂鸣段"},
                                          {"name": "整条", "in": 0, "out": None}]}
        json.dump(reg, open(reg_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        r = subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.7",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",  # 2s 底
                            "-filter_complex",
                            "[0:a]adelay=200:all=1[beep];[1:a]atrim=0:2[base];[base][beep]amix=inputs=2:normalize=0,volume=8[a]",
                            "-map", "[a]", "-c:a", "libmp3lame", mp3], capture_output=True, text=True)
        assert os.path.exists(mp3), "夹具合成失败: %s" % r.stderr[-200:]
        sl = {"title": "T35", "outline": "t",
              "meta": {"format": "portrait", "audio": {"bgm": "", "bgm_volume": 0.9}},
              "beats": [{"no": 1, "id": "m1", "story": "t",
                         "narration": {"mode": "none"},
                         "music": {"inherit": False, "bgm": "bgm_t35seg", "segment": "hook", "loop": False},
                         "tracks": [{"role": "A", "source_id": "M0109", "cut_index": None,
                                     "src_in": 0, "duration": 4.0, "requirement": "t"}],
                         "effects": {"stickers": [], "sfx": []}, "subtitle": {}, "transition_out": None}]}
        api("/api/storyline/" + PROJ + "?story=e2ebgm35", method="POST", body=sl)
        st, d = api("/api/render/" + PROJ + "?flash=0.6&grain=16&hold=1.0&duration=0.5&story=e2ebgm35", method="POST")
        out = os.path.join(ROOT, "projects", PROJ, "out-e2ebgm35.mp4")
        if not (st == 200 and d.get("ok") and os.path.exists(out)):
            check("T35 BGM 段落渲染（段落窗口+不循环→apad 补静音）", False, json.dumps(d, ensure_ascii=False))
            return
        r2 = subprocess.run([FFMPEG, "-ss", "1.5", "-t", "2.5", "-i", out,
                             "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        m2 = re.search(r"mean_volume: (-?[\d.]+)", r2.stderr)
        mv = float(m2.group(1)) if m2 else 0.0
        # 1.5s 后必须近静音：蜂鸣只响一次（0.2-0.9s）→ 若整曲循环 bug 会再次蜂鸣（mean 显著抬升）
        check("T35 BGM 段落渲染（段落窗口+不循环→apad 补静音）", mv <= -45,
              "1.5-4s 均值 %.1fdB（≤-45 为段）" % mv)
    finally:
        open(reg_p, "w", encoding="utf-8").write(old_reg)
        api("/api/storyline-delete/" + PROJ + "?story=e2ebgm35", method="POST")
        if os.path.exists(mp3):
            os.remove(mp3)


def t36_draft_effect_registry():
    # 回归锚（2026-09-15 维护者 #11）：效果注册表进提示词 + LLM 选择经 validate 白名单透传。
    # 曾双重假消费：提示词没喂注册表 + validate 重建 beats 时恒空 effects/subtitle。
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import draft
    p = draft.build_prompt("素材档案x", "意图y", ["cut"])
    _k = lambda reg: next(k for k in reg if not k.startswith("_"))
    subs = draft._reg("subtitles.json"); sfxs = draft._reg("sfx.json"); stks = draft._reg("stickers.json")
    ok_prompt = ("效果注册表" in p and _k(subs) in p and _k(sfxs) in p and _k(stks) in p)
    beats = draft.validate({"beats": [{"story": "s", "transition_out": None,
                                      "tracks": [{"role": "A", "source_id": "M1", "src_in": 0, "duration": 3}],
                                      "subtitle": {"style": _k(subs)},
                                      "effects": {"stickers": [{"asset": _k(stks), "at_word": "",
                                                                "duration": 1.2, "pos": "top-center"},
                                                               {"asset": "编造贴纸", "at_word": "x"}],
                                                  "sfx": [{"asset": _k(sfxs), "at": 0.1, "duration": 0.4},
                                                          {"asset": "编造音效"}]}}]},
                           {"M1": {"usable": True, "duration": 10, "kind": "video"}}, ["cut"])
    b = beats[0]
    ok_pass = (b["subtitle"].get("style") in subs and len(b["effects"]["stickers"]) == 1
               and len(b["effects"]["sfx"]) == 1 and b["effects"]["sfx"][0]["asset"] in sfxs)
    check("T36 效果注册表进提示词+LLM 选择白名单透传", ok_prompt and ok_pass,
          "prompt=%s 透传 sub=%s stk=%d sfx=%d" % (ok_prompt, bool(b["subtitle"].get("style")),
                                                   len(b["effects"]["stickers"]), len(b["effects"]["sfx"])))


def t37_review_gate():
    # 回归锚（2026-09-15 维护者 #3）：素材审核有实义化——"待审"曾是纯标签，validate 只看 usable，
    # 待审素材照进成片（名存实亡）。现三层：①审计齐自动过审 ②档案标【未过审——禁用】 ③validate 剔除。
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import draft, transcribe, understand
    v_done = {"kind": "video", "review": "pending-review",
              "audit": {"transcript": "done", "visual": "done"}}
    v_half = {"kind": "video", "review": "pending-review", "audit": {"transcript": "done"}}
    a_done = {"kind": "audio", "review": "pending-review",
              "audit": {"transcript": "done", "visual": "n/a"}}
    for f in (v_done, a_done):
        transcribe._auto_review(f)
    understand._auto_review(v_half)
    ok_auto = (v_done["review"] == "reviewed" and a_done["review"] == "reviewed"
               and v_half["review"] == "pending-review")  # 半审（只转写）不过审
    import tempfile
    tmp = tempfile.mkdtemp(prefix="t37_")
    old_root = draft.ROOT
    ok_rows = ok_val = False
    try:
        draft.ROOT = tmp
        os.makedirs(os.path.join(tmp, "materials", "packs", "t37p"))
        _w = {"words": [{"text": "x", "start": 0.0, "end": 0.5}]}
        files = [
            {"id": "M_UNREV", "kind": "video", "duration": 8.0, "usable": True,
             "review": "pending-review", "audit": {}, "transcript": dict(_w)},
            {"id": "M_LEGOK", "kind": "video", "duration": 8.0, "usable": True,
             "review": "pending-review", "audit": {"transcript": "done", "visual": "done"},
             "transcript": dict(_w)},
            {"id": "M_OK", "kind": "video", "duration": 8.0, "usable": True,
             "review": "reviewed", "audit": {"transcript": "done", "visual": "done"},
             "transcript": dict(_w)},
        ]
        json.dump({"files": files},
                  open(os.path.join(tmp, "materials", "packs", "t37p", "pack.json"), "w"),
                  ensure_ascii=False)
        dossier, mats = draft.build_dossier(["t37p"])

        def row_of(mid):
            return next((l for l in dossier.split("\n") if l.startswith("- %s（" % mid)), "")
        ok_rows = ("【未过审——禁用" in row_of("M_UNREV")
                   and "【未过审" not in row_of("M_LEGOK")  # 审计齐的旧数据=视同过审（不回写也放行）
                   and "【未过审" not in row_of("M_OK"))
        beats = draft.validate({"beats": [
            {"story": "s", "transition_out": None,
             "tracks": [{"role": "A", "source_id": "M_OK", "src_in": 0, "duration": 3},
                        {"role": "B", "source_id": "M_UNREV", "src_in": 0, "duration": 2}]},
            {"story": "s", "transition_out": None,
             "tracks": [{"role": "A", "source_id": "M_UNREV", "src_in": 0, "duration": 3}]}]},
            mats, ["cut"])
        ok_val = (len(beats) == 1 and len(beats[0]["tracks"]) == 1
                  and beats[0]["tracks"][0]["source_id"] == "M_OK")  # 未过审轨被剔+纯未过审幕整幕丢弃
    finally:
        draft.ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)
    check("T37 审核有实义化（审计齐自动过审/档案标禁用/validate 剔除未过审）",
          ok_auto and ok_rows and ok_val,
          "auto=%s rows=%s val=%s" % (ok_auto, ok_rows, ok_val))


def t25_rmw_smoke():
    # R3 终局轮产物：RMW 并发竞争冒烟——慢写者持锁期间并发 usable/upload，
    # 两方变更都必须存活（R3-1/R3-2 的回归锚：读点再溜出临界区，此测试当场红）
    try:
        import subprocess as _sp
        r = _sp.run([sys.executable, os.path.join(ROOT, "tests", "rmw_smoke.py")],
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
            os.makedirs(os.path.join(tmp, "projects", "tproj", "materials"))
            json.dump({"terms": ["龙骨", "檀溪公馆", "窗帘盒"]},
                      open(os.path.join(tmp, "config", "lexicon.json"), "w"), ensure_ascii=False)
            ws, t = [], 0.0
            for ch in "轮骨不牢，方可以了。大家都看到过吧":
                ws.append({"text": ch, "start": round(t, 1), "end": round(t + 0.2, 1)})
                t += 0.2
            json.dump({"files": [{"id": "T24M", "duration": round(t, 1), "audit": {},
                                  "transcript": {"words": ws}}]},
                      open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json"), "w"), ensure_ascii=False)
            json.dump({"packs": ["tpid"]},
                      open(os.path.join(tmp, "projects", "tproj", "materials", "library.json"), "w"), ensure_ascii=False)
            old_llm = proofread.chat_llm
            proofread.chat_llm = lambda msgs: ('[{"find":"轮骨","replace":"龙骨"},'
                                               '{"find":"方可以了","replace":"放可以了"},'
                                               '{"find":"龙骨","replace":"轻钢龙骨"},'
                                               '{"find":"大家都看到过吧","replace":"大家均看到过吧"}]')
            proofread.run("tproj", dry=True)
            assert not os.path.exists(os.path.join(tmp, "projects", "tproj", "review-queue.json")), "dry 不得落队列"
            pk_dry = json.load(open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json")))
            assert pk_dry["files"][0]["transcript"]["words"][0]["text"] == "轮", "dry 不得写词轨"
            proofread.run("tproj", dry=False)
            pk_now = json.load(open(os.path.join(tmp, "materials", "packs", "tpid", "pack.json")))
            text = "".join(w["text"] for w in pk_now["files"][0]["transcript"]["words"])
            ok_fix = ("龙骨不牢" in text) and ("轮骨" not in text) and ("方可以了" in text)
            q = json.load(open(os.path.join(tmp, "projects", "tproj", "review-queue.json")))
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
    # 新鲜度门（2026-10-19）：进程必须比代码新，否则下游所有结果不可信——旧实例占端口 /
    # mtime 倒挂三次实锤。无 /api/health = 服务太旧，同样中止。宁可不跑，不要假绿。
    st, h = api("/api/health")
    if st != 200 or not h.get("fresh"):
        print("✗ studio 进程不是最新代码（%s）——重启 studio 后再测，否则结果不可信" %
              ("无 /api/health 路由" if st != 200
               else "pid=%s started=%s newest_py=%s" % (h.get("pid"), h.get("started"), h.get("newest_py"))))
        sys.exit(2)
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
    t27_orchestrator()
    t28_disposition()
    t29_qc_av_sync()
    t30_voice_api()
    t31_dubfit()
    t32_draft_truncation_retry()
    t33_vadwords_transcript_text()
    t34_narration_vocab_guard()
    t35_bgm_segment_render()
    t36_draft_effect_registry()
    t37_review_gate()
    t38_cover_sink()
    t39_digest()
    t40_beat_preview()
    t41_registry()
    t42_advance_gate()
    t43_proofread_review_gate()
    t44_vadwords_no_asr_time()
    t45_content_type_gates()
    t46_loudnorm_chain()
    t47_proofread_pending_not_done()
    t48_validate_no_beat_truncation()
    t49_proofread_queue_lands_in_project()
    t50_transcode_preset_ladder()
    t51_transcribe_concurrency()
    t52_retry429_backoff()
    t53_sticker_text_templates()
    t54_overlap_aware_fix()
    t55_cover_prerender_flow()
    t56_at_word_gate()
    t57_vadwords_cross_span()
    t58_proofread_degenerate_guard()
    t59_transition_reserve()
    t60_material_pipeline()
    t61_advance_dynamic_reestimate()
    t62_structural_pagination()
    t63_type_platform_profiles()
    t64_dom_execution_sim()
    t65_human_agent_equivalence()
    t66_chinese_naming()
    t67_browser_smoke()
    t68_persona_prompt()
    t69_voice_format_transcode()
    t70_dub_voice_invalidation()
    t71_beat_subtitle_style()
    t72_active_storyline_write()
    t73_security_batch()
    t74_review_batch()
    t75_acceptance_gaps()
    t25_rmw_smoke()
    print("══ 结果：%d 通过 / %d 失败 ══" % (len(PASS), len(FAIL)))
    fixtures.remove()  # 夹具即用即删（无论成败——曾只挂在 GREEN 分支，失败路径残留测试数据）
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        sys.exit(1)
    print("E2E ALL GREEN")


# ---------------------------------------------------------------- T63 视频类型/平台档案化（2026-10-15）
def t63_type_platform_profiles():
    # 锚：类型/平台从"换标签"升为"剪辑方法参数包"——draft 提示词注入、渲染默认值回退、
    # qc R11 时长域、平台锚跟随故事线、/api/enums 派生、档案注册表写闭环。全离线。
    try:
        sys.path.insert(0, os.path.join(ROOT, "autocut3"))
        import draft, qc, pipeline

        # ① draft 提示词注入：类型档案的 rhythm/draft_prompt/默认转场进提示词；无类型零漂移
        p = draft.vtype_profile("knowledge-koubo")
        ok1a = bool(p) and p.get("rhythm") == "钩子先行，信息密度高"
        pr = draft.build_prompt("[档案]", "测", ["crossDissolve"], p)
        ok1b = "[视频类型档案]" in pr and "首幕必须开场钩子" in pr and "默认转场倾向 fadewhite" in pr
        pr2 = draft.build_prompt("[档案]", "测", ["crossDissolve"], None)
        ok1c = "[视频类型档案]" not in pr2
        ok1d = draft.vtype_profile("no-such") is None  # 未知 id 退化通用不炸

        # ② pipeline 渲染默认值：档案 defaults 只补未显式设置字段（显式选择赢）
        vd = pipeline._vtype_defaults({"type": "vlog"})
        ok2a = vd.get("subtitle") == "plain-quiet" and vd.get("cover_strategy") == "first-frame"
        ok2b = pipeline._vtype_defaults({}) == {} and pipeline._vtype_defaults({"type": "no-such"}) == {}

        # ③ 平台锚跟随：qc.load_rules(platform) 查 registry/platforms.json（旧 qc_rules 段兜底）
        r1 = qc.load_rules("shipinhao"); r2 = qc.load_rules("broadcast"); r3 = qc.load_rules("no-such")
        ok3 = (r1["_lufs_target"] == -14 and r1["_platform"] == "shipinhao"
               and r2["_lufs_target"] == -23 and r3["_lufs_target"] == -16)

        # ④ R11 时长域：口播类型（20-120s）超界 warn / 在界静默 / 无类型跳过
        sl = {"meta": {"type": "knowledge-koubo"}, "beats": [
            {"tracks": [{"role": "A", "src_in": 0, "duration": 80.0}]},
            {"tracks": [{"role": "A", "src_in": 0, "duration": 70.0}]}]}
        v = qc.check_duration_domain(sl, {})
        ok4a = len(v) == 1 and v[0]["rule"] == "R11" and "超类型" in v[0]["note"]
        ok4b = qc.check_duration_domain({"meta": {"type": "vlog"},
                                         "beats": [{"tracks": [{"role": "A", "src_in": 0, "duration": 60.0}]}]}, {}) == []
        ok4c = qc.check_duration_domain({"meta": {}, "beats": sl["beats"]}, {}) == []

        # ⑤ /api/enums 派生：video_types/platforms 从档案注册表来（含 rhythm），enums.json 不再持有
        code5, e5 = api("/api/enums")
        ok5 = (code5 == 200 and len(e5.get("video_types") or []) == 6
               and any(x.get("id") == "knowledge-koubo" and x.get("rhythm") for x in e5["video_types"])
               and len(e5.get("platforms") or []) == 5
               and not json.load(open(os.path.join(ROOT, "registry", "enums.json"), encoding="utf-8")).get("video_types"))

        # ⑥ 档案注册表写闭环：save video_types（白名单已放行）→ 读回 → 还原
        vtp = os.path.join(ROOT, "registry", "video_types.json")
        old = open(vtp, encoding="utf-8").read()
        try:
            reg = json.loads(old)
            reg["knowledge-koubo"]["rhythm"] = "T63-临时改"
            code6a, _ = api("/api/registry-save/video_types", method="POST", body=reg)
            back = json.load(open(vtp, encoding="utf-8"))["knowledge-koubo"]["rhythm"] == "T63-临时改"
        finally:
            open(vtp, "w", encoding="utf-8").write(old)  # 原样还原（不动格式细节）

        check("T63 类型/平台档案化（draft注入/渲染默认值/平台锚/R11/enums派生/写闭环）",
              ok1a and ok1b and ok1c and ok1d and ok2a and ok2b and ok3
              and ok4a and ok4b and ok4c and ok5 and code6a == 200 and back,
              "draft=%s/%s/%s/%s defaults=%s/%s 锚=%s R11=%s/%s/%s enums=%s save=%s读回=%s"
              % (ok1a, ok1b, ok1c, ok1d, ok2a, ok2b, ok3, ok4a, ok4b, ok4c, ok5, code6a, back))
    except Exception as e:
        check("T63 类型/平台档案化（draft注入/渲染默认值/平台锚/R11/enums派生/写闭环）", False, str(e)[:160])


# ---------------------------------------------------------------- T65 人与 Agent 等价补齐（2026-10-17）
def t65_human_agent_equivalence():
    # 锚：等价审查修复的三条服务端契约——
    # ① /api/voices sleeping：未安装 ≠ 休眠（_health() 对未安装也 False，曾致前端
    #    "未安装"与"休眠中+唤醒"双横幅同挂）——sleeping 为真的前提是 installed
    # ② 故事线 title 人侧可写：POST 带 title → 落盘读回（创作台 metabar 标题字段的 API 锚）
    # ③ dub 台词变更失效：台词改了，旧配音 audio/synth_text/words 必须摘除（防旧音唱新词
    #    静默回退）；台词不变则按文本配对迁移保留（插幕/调序不误伤）
    st, v = api("/api/voices")
    ok = st == 200 and (not v.get("sleeping") or bool(v.get("installed")))
    check("T65a voices sleeping 契约（休眠⇒已安装）", ok,
          "installed=%s sleeping=%s" % (v.get("installed"), v.get("sleeping")))

    sid = "e2eeq"
    sfx = os.path.join(ROOT, "projects", PROJ, "storylines", sid + ".json")
    api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")  # 清残
    nar0 = {"mode": "dub", "audio": "materials/dub/%s-b1.mp3" % sid,
            "synth_text": "旧台词", "words": [{"text": "旧", "start": 0, "end": 1}]}
    mk = lambda text, nar: {"no": 1, "id": "b1", "story": text, "narration": nar,
                            "tracks": [], "music": {"inherit": True},
                            "effects": {"stickers": [], "sfx": []}, "subtitle": {}, "transition_out": None}
    try:
        st, _ = api("/api/storyline/" + PROJ + "?story=" + sid, method="POST",
                    body={"title": "等价锚线", "outline": "", "meta": {}, "beats": [mk("旧台词", dict(nar0))]})
        sl = json.load(open(sfx, encoding="utf-8")) if st == 200 else {}
        check("T65b 故事线 title 写读闭环", st == 200 and sl.get("title") == "等价锚线",
              "st=%s title=%r" % (st, sl.get("title")))
        # 台词不变（客户端未透传 audio 也按文本配对从旧线迁移保留）
        api("/api/storyline/" + PROJ + "?story=" + sid, method="POST",
            body={"beats": [mk("旧台词", {"mode": "dub"})]})
        nar = (json.load(open(sfx, encoding="utf-8")).get("beats") or [{}])[0].get("narration") or {}
        check("T65c dub 台词不变→音频迁移保留", nar.get("audio") == nar0["audio"] and nar.get("words") == nar0["words"],
              "narration=%r" % nar)
        # 台词改了→旧配音三键摘除（render-start 前置会重合成）
        api("/api/storyline/" + PROJ + "?story=" + sid, method="POST",
            body={"beats": [mk("新台词", dict(nar0))]})
        nar = (json.load(open(sfx, encoding="utf-8")).get("beats") or [{}])[0].get("narration") or {}
        check("T65d dub 台词变更→旧音频失效",
              nar.get("mode") == "dub" and "audio" not in nar and "synth_text" not in nar and "words" not in nar,
              "narration=%r" % nar)
    finally:
        api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")


def t66_chinese_naming():
    # 锚：2026-10-17 用户裁定项目名/故事线 id 支持中文（_safe_name = ASCII 白名单 + CJK，
    # 安全不变量不变：禁分隔符 + 禁 ..）。钉全链路：建项目→GET→建中文故事线→落盘读回→删除清理。
    from urllib.parse import quote
    name, sid = "e2e中文项目", "口播一版"
    api("/api/project-delete/" + quote(name), method="POST")  # 清残
    try:
        st, d = api("/api/project-create/x", method="POST",
                    body={"name": name, "title": "中文命名锚", "format": "vertical"})
        ok = st == 200 and d.get("name") == name and os.path.isdir(os.path.join(ROOT, "projects", name))
        check("T66a 中文项目名建库", ok, "st=%s name=%r" % (st, d.get("name")))
        st, d = api("/api/project/" + quote(name))
        check("T66b 中文项目名读回", st == 200 and name in json.dumps(d, ensure_ascii=False),
              "st=%s" % st)
        beats = [{"no": 1, "id": "b1", "story": "第一句", "narration": {"mode": "inherit"},
                  "tracks": [], "music": {"inherit": True},
                  "effects": {"stickers": [], "sfx": []}, "subtitle": {}, "transition_out": None}]
        st, _ = api("/api/storyline/" + quote(name) + "?story=" + quote(sid), method="POST",
                    body={"title": "中文线", "outline": "", "meta": {}, "beats": beats})
        sfx = os.path.join(ROOT, "projects", name, "storylines", sid + ".json")
        sl = json.load(open(sfx, encoding="utf-8")) if st == 200 and os.path.exists(sfx) else {}
        check("T66c 中文故事线 id 写读闭环", st == 200 and sl.get("title") == "中文线",
              "st=%s title=%r" % (st, sl.get("title")))
        # 安全不变量不随中文放开：.. 穿越 / 路径分隔符照旧 400
        st1, _ = api("/api/project-create/x", method="POST", body={"name": "..%2Fetc", "format": "vertical"})
        st2, _ = api("/api/project/" + quote("a/b"))
        check("T66d 穿越/分隔符仍拒", st1 == 400 and st2 in (400, 404), "create=%s get=%s" % (st1, st2))
    finally:
        api("/api/storyline-delete/" + quote(name) + "?story=" + quote(sid), method="POST")
        api("/api/project-delete/" + quote(name), method="POST")



def t67_browser_smoke():
    # 锚：真浏览器冒烟（2026-10-19）。domsim 桩 DOM 不解析 HTML——pfField 断串裸显文本、
    # loadConfig/openProj 竞态（await 期间全局 P 被换）这类【渲染事故+异步竞态】只有真浏览器
    # 能拦。执行体 = tests/browser_smoke.html（iframe 同域驱动真实页面），--dump-dom 读回 JSON。
    # Chrome 跨平台发现（2026-10-22 CI 红根修：曾写死 mac 路径，ubuntu/windows runner 必红——
    # T67 自 2026-10-19 落地起 CI 连红三轮）。GitHub runner 预装 Chrome：
    # ubuntu=/usr/bin/google-chrome、windows=C:\Program Files\Google\Chrome\...；可用 CHROME_PATH 覆盖。
    import shutil as _sh
    _cands = [os.environ.get("CHROME_PATH"),
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              _sh.which("google-chrome"), _sh.which("google-chrome-stable"),
              _sh.which("chromium"), _sh.which("chromium-browser"), _sh.which("chrome"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    chrome = next((c for c in _cands if c and os.path.exists(c)), None)
    if not chrome:
        check("T67 真浏览器冒烟", False, "Chrome 未找到（mac/which/win 九路全空）——本层门禁无法降级，请安装")
        return
    url = "%s/files/tests/browser_smoke.html?proj=%s" % (BASE, PROJ)
    try:
        # 浏览器冒烟必须离线自闭环：页面外链字体不可成为 dump-dom 的 150s 网络等待
        # （2026-10-24 Ubuntu CI 同点两连红；禁外网后本地同命令 17.4s→3.3s）。
        r = subprocess.run([chrome, "--headless", "--disable-gpu",
                            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
                            "--virtual-time-budget=30000", "--dump-dom", url],
                           capture_output=True, text=True, timeout=150)
    except Exception as e:
        check("T67 真浏览器冒烟", False, "chrome 执行失败: %s" % e)
        return
    m = re.search(r'<pre id="result">(.*?)</pre>', r.stdout or "", re.S)
    if not m or m.group(1) == "RUNNING":
        check("T67 真浏览器冒烟", False, "harness 未出结果（rc=%s）" % r.returncode)
        return
    import html as _html
    R = json.loads(_html.unescape(m.group(1)))
    check("T67a 全程无 JS 异常", R.get("errors") == [], "errors=%r" % (R.get("errors") or [])[:200])
    steps = R.get("steps") or {}
    bad = {k: v for k, v in steps.items()
           if not (all(v.values()) if isinstance(v, dict) else v)}
    check("T67b 关键路径渲染（主页/开项目/切换器/tab×2/抽屉×7）", not bad, "bad=%r" % bad)
    check("T67c 无断串泄漏（处理器属性不成可见文本）", R.get("leak") == [], "leak=%r" % R.get("leak"))


# ---------------------------------------------------------------- T68 人设注入起草提示词（2026-10-20）
def t68_persona_prompt():
    """meta.persona 曾是零消费假字段（UI 可设可落盘，draft/渲染全不读）——Noah 裁定接进
    起草提示词。锚：注入生效 / 无人设零漂移 / run 链路参数在。"""
    import inspect
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import draft as D
    p_with = D.build_prompt("档" * 60, "起一条口播线", [], None, persona="三代同堂宝妈")
    check("T68a 人设注入提示词", "讲述者人设：三代同堂宝妈" in p_with and "[人设]" in p_with,
          "hit=%s" % ("[人设]" in p_with))
    p_none = D.build_prompt("档" * 60, "起一条口播线", [], None)
    check("T68b 无人设零漂移（提示词无 [人设] 段）", "[人设]" not in p_none, "")
    check("T68c run 链路接受 persona（Studio/CLI 共用入口）",
          "persona" in inspect.signature(D.run).parameters,
          "params=%s" % list(inspect.signature(D.run).parameters))


# ---------------------------------------------------------------- T69 声纹注册格式归一（2026-10-22）
def t69_voice_format_transcode():
    """锚：studio 曾把上传的参考音频恒命名 .wav——m4a 字节顶 .wav 后缀发给 Audio8，
    解码必炸（用户实锤 400 Format not recognised）。根修在 register_voice 入口层
    （CLI/Studio 同路）：wav/mp3 直传，其余 ffmpeg 转 44.1k mono wav。离线锚不打 Audio8。"""
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import asr, tts_audio8, subprocess, tempfile
    ff = asr.ffmpeg_path()
    if not ff:
        check("T69 声纹注册格式归一", False, "无 ffmpeg——本锚无法降级")
        return
    # a) wav/mp3 直通不转码
    p, tmp = tts_audio8._reg_wav("/x/参考.wav")
    a = (p == "/x/参考.wav" and tmp is None)
    p2, tmp2 = tts_audio8._reg_wav("/x/voice.MP3")  # 大小写不敏感
    a = a and p2 == "/x/voice.MP3" and tmp2 is None
    check("T69a wav/mp3 直通（不转码、无临时文件）", a, "")
    # b) m4a → 真转码出合法 wav
    src = os.path.join(tempfile.mkdtemp(prefix="t69_"), "probe.m4a")
    subprocess.run([ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", src], capture_output=True, check=True)
    out, tmp3 = tts_audio8._reg_wav(src)
    head = open(out, "rb").read(4)
    ok = out.endswith(".wav") and head == b"RIFF" and tmp3 == out
    if tmp3:
        os.unlink(tmp3)
    check("T69b m4a 转码为合法 WAV（RIFF 头+临时文件交还调用方）", ok,
          "out=%s head=%r" % (out, head))
    # c) 坏输入如实报错不静默
    bad = os.path.join(os.path.dirname(src), "bad.m4a")
    open(bad, "wb").write(b"not audio")
    try:
        tts_audio8._reg_wav(bad)
        c = False
    except RuntimeError:
        c = True
    check("T69c 坏音频转码失败如实抛错（不静默注册空音色）", c, "")


# ---------------------------------------------------------------- T70 配音音色失效链（2026-10-22）
def t70_dub_voice_invalidation():
    # 锚：故事线级配音音色（meta.voice）落地后的失效语义——音色变更与台词变更同套机制：
    # 旧配音 audio/synth_text/synth_voice/words 摘除（防旧音色音频静默残留），渲染前 _dub_ensure 重配。
    # 四态：a) 客户端透传旧键+音色变更→摘  b) 音色不变→迁移保留（含 synth_voice）
    #       c) 客户端不透传+音色变更→迁移拒绝（不留旧音）  d) 存量线无 synth_voice→不溯及
    sid = "e2evoice"
    sfx = os.path.join(ROOT, "projects", PROJ, "storylines", sid + ".json")
    api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")  # 清残
    nar0 = {"mode": "dub", "audio": "materials/dub/%s-b1.mp3" % sid,
            "synth_text": "台词甲", "synth_voice": "xiaoxiao",
            "words": [{"text": "台", "start": 0, "end": 1}]}
    mk = lambda text, nar: {"no": 1, "id": "b1", "story": text, "narration": nar,
                            "tracks": [], "music": {"inherit": True},
                            "effects": {"stickers": [], "sfx": []}, "subtitle": {}, "transition_out": None}
    KEYS = ("audio", "synth_text", "synth_voice", "words")
    post = lambda meta, beats: api("/api/storyline/" + PROJ + "?story=" + sid, method="POST",
                                   body={"meta": meta, "beats": beats})
    read_nar = lambda: (json.load(open(sfx, encoding="utf-8")).get("beats") or [{}])[0].get("narration") or {}
    try:
        # 建线：voice=xiaoxiao + 已合成 dub 幕
        st, _ = post({"voice": "xiaoxiao"}, [mk("台词甲", dict(nar0))])
        assert st == 200
        # a) 音色改 yunxi，客户端透传旧 narration → 四键摘除
        post({"voice": "yunxi"}, [mk("台词甲", dict(nar0))])
        nar = read_nar()
        check("T70a dub 音色变更→旧音频失效（透传旧键路径）",
              nar.get("mode") == "dub" and all(k not in nar for k in KEYS), "narration=%r" % nar)
        # b) 重建后音色不变（客户端不透传音频）→ 按文本配对迁移保留，synth_voice 随行
        post({"voice": "xiaoxiao"}, [mk("台词甲", dict(nar0))])
        post({"voice": "xiaoxiao"}, [mk("台词甲", {"mode": "dub"})])
        nar = read_nar()
        check("T70b dub 音色不变→迁移保留（synth_voice 随行）",
              nar.get("audio") == nar0["audio"] and nar.get("synth_voice") == "xiaoxiao"
              and nar.get("words") == nar0["words"], "narration=%r" % nar)
        # c) 音色改 yunxi，客户端不透传旧键 → 音色不符的旧配音迁移拒绝（渲染前必重配，不留旧音）
        post({"voice": "yunxi"}, [mk("台词甲", {"mode": "dub"})])
        nar = read_nar()
        check("T70c dub 音色变更→迁移拒绝（不透传路径同样不留旧音）",
              nar.get("mode") == "dub" and all(k not in nar for k in KEYS), "narration=%r" % nar)
        # d) 存量线（synth_voice 缺失的老产物）音色变更 → POST 不溯及（渲染链 _dub_ensure 兜底重配）
        nar_old = {k: v for k, v in nar0.items() if k != "synth_voice"}
        post({"voice": "xiaoxiao"}, [mk("台词甲", dict(nar_old))])
        post({"voice": "yunxi"}, [mk("台词甲", dict(nar_old))])
        nar = read_nar()
        check("T70d 存量线无 synth_voice→不溯及（audio 保留，渲染链兜底）",
              nar.get("audio") == nar0["audio"] and nar.get("synth_text") == "台词甲",
              "narration=%r" % nar)
    finally:
        api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")


# ---------------------------------------------------------------- T71 幕级字幕样式渲染链（2026-10-22）
def t71_beat_subtitle_style():
    """锚：幕级字幕样式（beat.subtitle.style）曾四通一断——提示词 schema/validate/UI/落盘全通，
    渲染链零消费（voice_id 同型半成品）。接通后钉四态：无覆盖零漂移 / 覆盖→按幕分页+多样式表 /
    分页与出片同源（QC R10 也吃 plan_pages）/ 幕级未注册硬失败。离线不打 ffmpeg。"""
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import pipeline, tempfile, shutil
    regs = json.load(open(os.path.join(ROOT, "registry", "subtitles.json"), encoding="utf-8"))
    gids = sorted(regs)
    g1 = "karaoke-gold" if "karaoke-gold" in regs else gids[0]
    g2 = next((k for k in gids if k != g1), None)
    if not g2:
        check("T71 幕级字幕样式渲染链", False, "注册表字幕样式不足两款，无法锚幕级覆盖")
        return
    mk_plan = lambda seg2style: {
        "subtitle_style": g1, "width": 1080, "height": 1920, "duration": 10.0,
        "segments": [{"tl_in": 0.0, "dur": 5.0, "narration": "original"},
                     {"tl_in": 5.0, "dur": 5.0, "narration": "original", "subtitle_style": seg2style}],
        "words": [{"t": "完了。", "s": 0.5, "e": 2.0}, {"t": "开始。", "s": 5.5, "e": 7.0}]}
    tmp = tempfile.mkdtemp(prefix="t71_")
    try:
        # a) 无覆盖：与全局 build_pages 逐页一致（存量零漂移），ASS 单样式 Karaoke
        p1 = mk_plan(None)
        p1["segments"][1].pop("subtitle_style")
        pages1 = pipeline.plan_pages(p1)
        ref = pipeline.build_pages(pipeline.char_level(p1["words"]),
                                   (regs.get(g1) or {}).get("max_chars", 12))
        a_same = [pg for pg, _ in pages1] == ref and all(st == g1 for _, st in pages1)
        ass1, n1 = pipeline.build_ass(p1, tmp, "a1.ass")
        ass1t = open(ass1, encoding="utf-8").read()
        check("T71a 无覆盖零漂移（全局面分页+单 Karaoke 样式行）",
              a_same and "Style: Karaoke," in ass1t and ("Style: " + g1) not in ass1t,
              "pages=%d ref=%d" % (len(pages1), len(ref)))
        # b) 幕级覆盖：按幕分页（全局会并成 1 页的两短句拆成 2 页）+ 多样式表 + Dialogue 用注册表 id
        p2 = mk_plan(g2)
        pages2 = pipeline.plan_pages(p2)
        ass2, n2 = pipeline.build_ass(p2, tmp, "a2.ass")
        ass2t = open(ass2, encoding="utf-8").read()
        b_ok = (len(pages2) == 2 and [st for _, st in pages2] == [g1, g2]
                and ("Style: " + g1 + ",") in ass2t and ("Style: " + g2 + ",") in ass2t
                and ass2t.count("Dialogue: 0,") == 2
                and (",%s,," % g2) in ass2t.split("Dialogue: 0,")[2])
        check("T71b 幕级覆盖→按幕分页+ASS 多样式表", b_ok,
              "pages=%d styles=%s" % (len(pages2), [st for _, st in pages2]))
        # c) 分页与出片同源：Dialogue 行数 == plan_pages 页数（R10 重放同吃 plan_pages，不会假 warn）
        check("T71c 分页单一事实源（plan_pages==出片页数）", n1 == len(pages1) and n2 == len(pages2),
              "n1=%s/%s n2=%s/%s" % (n1, len(pages1), n2, len(pages2)))
        # d) 幕级未注册样式 → SystemExit 硬失败（与全局同纪律）
        p3 = mk_plan("no-such-style")
        try:
            pipeline.build_ass(p3, tmp, "a3.ass")
            d_ok = False
        except SystemExit:
            d_ok = True
        check("T71d 幕级未注册样式硬失败", d_ok, "")
    except Exception as e:
        check("T71 幕级字幕样式渲染链", False, str(e)[:160])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- T72 当前线 active 写端（2026-10-22）
def t72_active_storyline_write():
    """锚：海报墙封面偏好「当前线→首线→main」的读端早在（studio /api/projects cover 解析），
    写端此前只有删线兜底——切线从不写 active，读端恒落空。接通后：GET /api/project?story=<sid>
    即回写 project.json active。"""
    sid = "e2eactive"
    sfx = os.path.join(ROOT, "projects", PROJ, "storylines", sid + ".json")
    pjf = os.path.join(ROOT, "projects", PROJ, "project.json")
    api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")  # 清残
    mk = {"no": 1, "id": "b1", "story": "锚", "narration": {"mode": "inherit"},
          "tracks": [], "music": {"inherit": True},
          "effects": {"stickers": [], "sfx": []}, "subtitle": {}, "transition_out": None}
    try:
        st, _ = api("/api/storyline/" + PROJ + "?story=" + sid, method="POST",
                    body={"title": "active 锚线", "beats": [mk]})
        assert st == 200 and os.path.exists(sfx)
        st2, d = api("/api/project/%s?story=%s" % (PROJ, sid))
        pj = json.load(open(pjf, encoding="utf-8"))
        check("T72 切线回写 active（海报墙封面跟当前线）",
              st2 == 200 and d.get("story") == sid and pj.get("active") == sid,
              "st=%s active=%r" % (st2, pj.get("active")))
    finally:
        api("/api/storyline-delete/" + PROJ + "?story=" + sid, method="POST")


# ---------------------------------------------------------------- T73 安全批（2026-10-22）
def t73_security_batch():
    """锚：安全审查五件的回归钉——
    a) 路径校验漏网：/api/beat/ 漏校 name、/api/render/ 漏校 sid（曾 `..%2F` 逃逸项目目录）
    b) GET 不再分发状态变更声纹路由（CSRF 面：任意网页 <img> 可删声纹），POST 通道完好
    c) registry-save 值校验：filtergraph 危险字符拒绝且不落盘 / 合法原样放行
    d) Host 白名单（DNS 重绑定）+ Content-Length 非法 400（原 int() 裸断/负值挂线程）
    e) build_digest 锁=全仓纪律 <packdir>/.lock（曾用 pack.json.lock 互斥失效，离线源码锚）"""
    # a) 穿越拒绝
    st1, _ = api("/api/beat/..%2F..%2Ftmp%2Fx/3", method="POST", body={})
    st2, _ = api("/api/render/%s?story=..%%2Fx" % PROJ, method="POST", body={})
    check("T73a 路径穿越拒绝（beat name / render sid）", st1 == 400 and st2 == 400,
          "st=%s/%s" % (st1, st2))
    # b) GET 摘除状态变更：GET voice-delete/voice-test 不再分发；POST 通道完好；GET /api/voices 只读保留
    stg1, dg1 = api("/api/voice-delete/__no_such__")
    stg2, _ = api("/api/voice-test/__no_such__")
    stp, dp = api("/api/voice-delete/__no_such__", method="POST")
    stv, _ = api("/api/voices")
    ok_b = (stv == 200
            and "no such voice" not in json.dumps(dg1)  # GET 落到通用 404，不进 _handle_voice
            and stg2 in (404, 501)
            and stp == 404 and "no such voice" in json.dumps(dp))  # POST 通道完好（不存在的音色=业务 404）
    check("T73b 状态变更声纹路由撤出 GET（POST 完好）", ok_b,
          "get_del=%s get_test=%s post_del=%s voices=%s" % (stg1, stg2, stp, stv))
    # c) registry 值校验：危险 preset 拒且文件不动；原样值放行
    tp = os.path.join(ROOT, "registry", "transitions.json")
    old = open(tp, encoding="utf-8").read()
    try:
        reg = json.loads(old)
        first = next(k for k in reg if not k.startswith("_"))
        reg[first]["preset"] = "fade,movie=/etc/passwd"
        stb, _ = api("/api/registry-save/transitions", method="POST", body=reg)
        unchanged = open(tp, encoding="utf-8").read() == old
        stok, _ = api("/api/registry-save/transitions", method="POST", body=json.loads(old))
        check("T73c registry-save 值校验（危险字符拒+合法放行+拒绝不落盘）",
              stb == 400 and unchanged and stok == 200, "st=%s/%s unchanged=%s" % (stb, stok, unchanged))
    finally:
        open(tp, "w", encoding="utf-8").write(old)
    # d) Host 白名单 + Content-Length 非法
    import http.client
    port = int(urllib.parse.urlparse(BASE).port or 8765)
    c1 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c1.request("GET", "/api/projects", headers={"Host": "evil.example.com"})
    st_h = c1.getresponse().status
    c1.close()
    c2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c2.putrequest("POST", "/api/draft/" + PROJ)
    c2.putheader("Content-Length", "abc")
    c2.endheaders(b"{}")
    st_cl = c2.getresponse().status
    c2.close()
    check("T73d Host 白名单 403 + 非法 Content-Length 400", st_h == 403 and st_cl == 400,
          "host=%s cl=%s" % (st_h, st_cl))
    # e) build_digest 锁纪律（离线源码锚）
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import understand, inspect
    src = inspect.getsource(understand.build_digest)
    check("T73e build_digest 锁=<packdir>/.lock 纪律",
          'os.path.join(os.path.dirname(pp), ".lock")' in src and 'pp + ".lock"' not in src, "")


# ---------------------------------------------------------------- T74 安全+资源审查修复批（2026-10-23）
def t74_review_batch():
    """锚：2026-10-23 三路审查（安全注入/资源溢出/文档）修复的回归钉——
    a) CSRF 闸：POST 无 X-AutoCut 头 403（text/plain 简单请求形态实证），带头通畅
    b) 注册表数值类型闸：subtitles.size 字符串（ASS 换行注入载荷）/bgm.segments.in 字符串/platforms
       lufs_target 字符串全 400 不落盘；合法值放行
    c) /files/：config 黑名单 404（services.json 密钥出口封死）+ NUL 400 + 畸形/倒挂 Range 416
    d) 短路径 400 不崩：beat 缺幕号/pack-mount 缺段/proofread-pack 缺 fid
    e) cover-upload/storyline 幽灵项目 404 且不落目录
    f) build_ass 词文本净化：\n 不出孤行、{} 转全角不进覆盖标签位
    g) 资源修复源码锚：渲染看门狗/start_new_session 进程组/上传截断校验/uuid 同名后缀/wake POST"""
    import http.client
    port = int(urllib.parse.urlparse(BASE).port or 8765)
    # a) CSRF 闸
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/storyline-delete/%s?story=__none__" % PROJ,
              body="x=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    st_no = c.getresponse().status
    c.close()
    st_yes, _ = api("/api/storyline-delete/%s?story=__none__" % PROJ, method="POST")
    check("T74a CSRF 闸（无 X-AutoCut 头 403 / 带头通）",
          st_no == 403 and st_yes != 403, "no=%s yes=%s" % (st_no, st_yes))
    # b) 注册表数值类型闸（备份/还原纪律同 T73c）
    def _try_save(rname, mutate):
        p = os.path.join(ROOT, "registry", rname + ".json")
        old = open(p, encoding="utf-8").read()
        try:
            reg = json.loads(old)
            mutate(reg)
            st, _ = api("/api/registry-save/" + rname, method="POST", body=reg)
            unchanged = open(p, encoding="utf-8").read() == old
            return st, unchanged
        finally:
            open(p, "w", encoding="utf-8").write(old)
    st1, u1 = _try_save("subtitles", lambda r: r.__setitem__(next(k for k in r if not k.startswith("_")),
                        {**r[next(k for k in r if not k.startswith("_"))], "size": "64\nDialogue: EVIL"}))
    def _bgm_mut(r):
        k = next(k for k in r if not k.startswith("_"))
        r[k]["segments"] = [{"name": "x", "in": "0;rm", "out": None}]
    st2, u2 = _try_save("bgm", _bgm_mut)
    def _plat_mut(r):
        k = next(k for k in r if not k.startswith("_"))
        r[k]["lufs_target"] = "-16;evil"
    st3, u3 = _try_save("platforms", _plat_mut)
    st4, _ = api("/api/registry-save/subtitles", method="POST",
                 body=json.load(open(os.path.join(ROOT, "registry", "subtitles.json"), encoding="utf-8")))
    check("T74b 注册表数值类型闸（注入载荷 400 不落盘/合法放行）",
          st1 == 400 and u1 and st2 == 400 and u2 and st3 == 400 and u3 and st4 == 200,
          "st=%s/%s/%s/%s unchanged=%s/%s/%s" % (st1, st2, st3, st4, u1, u2, u3))
    # c) /files/ 三面
    stc1, _ = api("/files/config/services.json")
    stc2, _ = api("/files/%00studio.py")
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/files/studio.py", headers={"Range": "bytes=abc-"})
    stc3 = c.getresponse().status
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/files/studio.py", headers={"Range": "bytes=100-5"})
    stc4 = c.getresponse().status
    c.close()
    check("T74c /files/ config 黑名单+NUL+Range 双畸形",
          stc1 == 404 and stc2 in (400, 404) and stc3 == 416 and stc4 == 416,
          "cfg=%s nul=%s range=%s/%s" % (stc1, stc2, stc3, stc4))
    # d) 短路径 400 不崩（原 IndexError 连接重置）
    std1, _ = api("/api/beat/" + PROJ, method="POST", body={})
    std2, _ = api("/api/pack-mount/" + PROJ, method="POST", body={})
    std3, _ = api("/api/proofread-pack/x", method="POST", body={})
    check("T74d 短路径 400（beat/pack-mount/proofread-pack）",
          std1 == 400 and std2 == 400 and std3 == 400, "st=%s/%s/%s" % (std1, std2, std3))
    # e) cover-upload / storyline 幽灵项目
    ghost = "__ghost_%d__" % os.getpid()
    ste, _ = api("/api/cover-upload/" + ghost, method="POST", raw=b"\xff\xd8\xff")
    ste2, _ = api("/api/storyline/" + ghost + "?story=s1", method="POST", body={"title": "t"})
    check("T74e cover-upload/storyline 幽灵项目 404 不落目录",
          ste == 404 and ste2 == 404 and not os.path.exists(os.path.join(ROOT, "projects", ghost)),
          "st=%s/%s" % (ste, ste2))
    # f) build_ass 净化（离线）
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    import pipeline, tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="t74_")
    try:
        plan = {"subtitle_style": "karaoke-gold", "width": 1080, "height": 1920, "duration": 5.0,
                "segments": [{"tl_in": 0.0, "dur": 5.0, "narration": "original"}],
                "words": [{"t": "正{\n}常", "s": 0.5, "e": 2.0}]}
        if "karaoke-gold" not in json.load(open(os.path.join(ROOT, "registry", "subtitles.json"), encoding="utf-8")):
            plan["subtitle_style"] = sorted(json.load(open(os.path.join(ROOT, "registry", "subtitles.json"), encoding="utf-8")))[0]
        ass, _n = pipeline.build_ass(plan, tmp, "f.ass")
        at = open(ass, encoding="utf-8").read()
        ev = at.split("[Events]", 1)[1]
        bad_lines = [l for l in ev.splitlines()[2:] if l.strip() and not l.startswith("Dialogue: 0,")]
        check("T74f build_ass 词文本净化（无孤行/{} 转全角）",
              not bad_lines and "（" in at and "）" in at and "\n}正常" not in at,
              "bad=%r" % bad_lines[:2])
    except Exception as e:
        check("T74f build_ass 词文本净化", False, str(e)[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # g) 资源修复源码锚
    src = open(os.path.join(ROOT, "studio.py"), encoding="utf-8").read()
    check("T74g 资源修复在场（看门狗/进程组/截断校验/uuid 后缀/wake POST）",
          all(x in src for x in ("WATCHDOG", "start_new_session", "上传截断", "uuid4().hex",
                                 'self.command == "POST"', "_run_pg")), "")


def t75_acceptance_gaps():
    """2026-10-23 wangyalun 真实素材全流程验收抓出的三个真缺口的回归锚（全离线打桩，零计费）：
    a) ASR 422「No clear speaker」=后端语义拒识（纯音乐/汽笛无说话人）——曾落 error 标记让编排器
       transcribe 环节对天然无声素材永远 pending；根修=归一为空词轨无语音（对齐空文本路）。
    b) draft --persona 曾只进提示词不落 meta.persona（save_storyline 有 vtype/voice 持久化独缺
       persona，实测 aidraft 起草后 meta 无人设）；根修=三处签名/持久化/传参补齐。
    c) vadwords dub 分支 nar["audio"] KeyError——音色/台词变更失效链摘除 audio 后 CLI/编排链
       无人补（_dub_ensure 只挂 studio 渲染入口）；根修=vadwords 就地按 meta.voice 合成补齐。"""
    import tempfile, shutil as _sh
    sys.path.insert(0, os.path.join(ROOT, "autocut3"))
    # a) 422 归一
    try:
        import asr, urllib.error, io
        _orig_t, _orig_e, _orig_d = asr.transcribe_minimax, asr.extract_audio, asr.media_duration
        asr.extract_audio = lambda src, wav, ff=None: open(wav, "wb").write(b"RIFF")  # 桩：不真解码
        asr.media_duration = lambda src: 10.0
        def _boom(wav, becfg):
            body = b'{"error":{"message":"No clear speaker was detected. Please check the audio. (711)"}}'
            raise urllib.error.HTTPError("http://x/speech_to_text", 422, "Unprocessable Entity", {}, io.BytesIO(body))
        asr.transcribe_minimax = _boom
        r = asr.transcribe(os.path.join(ROOT, "fake-src.mp4"))
        check("T75a ASR 422 No-clear-speech 归一无语音（不落 error）",
              r["words"] == [] and r["tier"] == "vad" and not r["text"], "r=%r" % str(r)[:80])
        asr.transcribe_minimax, asr.extract_audio, asr.media_duration = _orig_t, _orig_e, _orig_d
    except Exception as e:
        check("T75a ASR 422 归一", False, str(e)[:120])
    # b) persona 落盘
    tmpd = os.path.join(ROOT, "projects", "t75-tmp-%d" % os.getpid())
    try:
        import draft
        sid = draft.save_storyline("t75-tmp-%d" % os.getpid(), {"title": "t", "outline": "o"},
                                   [{"story": "台词"}], persona="测试人设", vtype="vlog", voice="v1")
        s = json.load(open(os.path.join(tmpd, "storylines", sid + ".json"), encoding="utf-8"))
        m = s.get("meta") or {}
        check("T75b draft persona/voice/type 三键落盘",
              m.get("persona") == "测试人设" and m.get("voice") == "v1" and m.get("type") == "vlog",
              "meta=%r" % str(m)[:80])
    except Exception as e:
        check("T75b persona 落盘", False, str(e)[:120])
    finally:
        _sh.rmtree(tmpd, ignore_errors=True)
    # c) vadwords dub 缺音频就地补齐（桩 tts.synth 出真音——VAD 要吃真实波形）
    tmpd2 = os.path.join(ROOT, "projects", "t75-dub-%d" % os.getpid())
    try:
        os.makedirs(os.path.join(tmpd2, "storylines"))
        sl = {"meta": {"voice": "桩音色"}, "beats": [
            {"no": 1, "story": "测试台词四个字", "narration": {"mode": "dub"},
             "tracks": [{"role": "A", "source_id": "X", "src_in": 0, "duration": 3.0}]}]}
        json.dump(sl, open(os.path.join(tmpd2, "storylines", "s1.json"), "w", encoding='utf-8'), ensure_ascii=False)
        import tts, vadwords
        _orig_synth, _orig_argv = tts.synth, sys.argv
        def _fake_synth(text, voice_id=None, out=None, **kw):
            subprocess.run([os.path.join(ROOT, "bin", "ffmpeg"), "-y", "-loglevel", "error",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=3", out], check=True)
        tts.synth = _fake_synth  # vadwords 分支内 import tts 后按模块属性调用，进程内补丁生效
        sys.argv = ["vadwords.py", "t75-dub-%d" % os.getpid(), "--story", "s1"]
        try:
            vadwords.main()
        finally:
            tts.synth, sys.argv = _orig_synth, _orig_argv
        s2 = json.load(open(os.path.join(tmpd2, "storylines", "s1.json"), encoding="utf-8"))
        n2 = s2["beats"][0]["narration"]
        check("T75c vadwords dub 缺音频就地补齐（audio+留痕+词轨）",
              bool(n2.get("audio")) and n2.get("synth_voice") == "桩音色"
              and n2.get("synth_text") == "测试台词四个字" and bool(n2.get("words")),
              "nar=%r" % str(n2)[:100])
    except Exception as e:
        check("T75c vadwords dub 补齐", False, str(e)[:120])
    finally:
        _sh.rmtree(tmpd2, ignore_errors=True)



def t64_dom_execution_sim():
    # 锚：t 遮蔽 i18n t() 一族前端运行时崩溃（切页崩/抽屉空白/转写框空白）曾穿过
    # node --check + audit + e2e 全部门禁——因为没有任何测试真正【执行】过 index.html 的 JS。
    # 本测用 node + DOM 桩（tests/domsim.js 模板）真实执行关键路径：
    # boot → 开项目 → 渲染幕/包/切片 → 搜索命中 → 7 个注册表抽屉。MOCK 从运行中的
    # studio 实时抓取（夹具项目自给，不依赖任何真实项目），前端/后端数据契约漂移同样在此现形。
    try:
        import tempfile
        node = shutil.which("node")
        if not node:
            check("T64 前端 DOM 执行模拟", False, "node 不可用（执行模拟需要 node，runner/开发机均预装）")
            return
        # ① 选夹具项目的第一条故事线（前面测试可能增删线，以 /api/projects 现报为准）
        st, projs = api("/api/projects")
        me = next((p for p in (projs or []) if p.get("name") == PROJ), None)
        story = (me and me.get("stories") or [""])[0]
        # ② 实时抓前端 boot→项目→抽屉 会碰的全部端点 → MOCK
        paths = ["/api/projects", "/api/enums", "/api/voices", "/api/formats",
                 "/api/packs", "/api/pulse", "/api/library/" + PROJ,
                 "/api/project/%s?story=%s" % (PROJ, story)] + \
                ["/api/registry/" + r for r in ("bgm", "subtitles", "transitions", "sfx",
                                                "stickers", "video_types", "platforms")]
        mock = {}
        for p in paths:
            st, d = api(p)
            mock[p.split("?")[0]] = d
        # ③ 拼装：模板 + index.html 脚本体，替换占位
        tpl = open(os.path.join(ROOT, "tests", "domsim.js"), encoding="utf-8").read()
        html = open(os.path.join(ROOT, "studio", "index.html"), encoding="utf-8").read()
        app = re.search(r"<script>\n(.*?)\n</script>", html, re.S).group(1)
        sim = (tpl.replace("__MOCK__", "const MOCK=" + json.dumps(mock, ensure_ascii=False))
                  .replace("__PROJ__", PROJ).replace("__STORY__", story)) + "\n" + app + "\n"
        # ④ 执行并解析断言行
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(sim)
            tmp = f.name
        try:
            r = subprocess.run([node, tmp], capture_output=True, text=True, timeout=120)
        finally:
            os.unlink(tmp)
        out = (r.stdout or "") + (r.stderr or "")
        open_ok = "OPEN: OK" in out and "active= s-create" in out
        render_ok = re.search(r"metabar= (\d+) beatcards= (\d+) packgroups= (\d+)", out or "")  # console.log 参数间有空格
        search_ok = "textarea-shows-transcript= true" in out
        drawers = dict(re.findall(r"DRAWER (\w+) len= (\d+)", out))
        broken = re.findall(r"DRAWER (\w+) .*no-broken-tag= false", out)  # pfField 断串锚（2026-10-17 用户实锤）
        drawer_errs = [l for l in (out or "").splitlines() if "ERR" in l]
        ok = (r.returncode == 0 and open_ok and render_ok
              and all(int(v) > 0 for v in render_ok.groups())
              and search_ok and len(drawers) == 7 and all(int(v) > 0 for v in drawers.values())
              and not drawer_errs and not broken)
        open_line = next((l for l in (out or "").splitlines() if l.startswith("OPEN:")), "?")
        if not ok:  # 取证落盘：完整模拟输出（OPEN 行含异常栈首帧）
            open("/tmp/t64_dom_out.txt", "w", encoding="utf-8").write(out or "(无输出) rc=%s" % r.returncode)
        check("T64 前端 DOM 执行模拟（开项目/渲染/搜索命中/7抽屉/无断串）", ok,
              "story=%s open=%s 渲染=%s 搜索=%s 抽屉=%d %s%s" % (story or "?", open_line[:120],
              ("OK" if render_ok else "无输出"), search_ok, len(drawers),
              ("；" + " | ".join(drawer_errs[:2]) if drawer_errs else ""),
              ("；断串抽屉: " + ",".join(broken) if broken else "")))
    except Exception as e:
        check("T64 前端 DOM 执行模拟（开项目/渲染/搜索命中/7抽屉/无断串）", False, str(e)[:160])



if __name__ == "__main__":
    main()
