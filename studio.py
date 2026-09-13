#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto-cut V3 Studio · 人监视器 v0.1
铁律：AI 以 JSON 为操作台，人以 Studio 为监视器。
纯标准库实现（零 pip 依赖，自包含）。用法: python3 studio.py [port]  默认 8765
"""
import json, os, re, shutil, subprocess, sys, time, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))


def _safe(s):
    """URL 拼路径的成分必须过这个白名单：字母数字、下划线、中划线、点（防 ../ 穿越）。"""
    return bool(s) and re.fullmatch(r"[A-Za-z0-9_\-\.]{1,80}", s) and ".." not in s


def _filename(s):
    """用户上传文件名校验（2026-09-14 立规：素材名是用户资产，系统不剥夺——中文全放行）。
    只禁真正危险的：路径分隔符、空字节、.. 穿越、隐藏文件。"""
    return bool(s) and len(s) <= 120 and s not in (".", "..") and not s.startswith((".", "-")) \
        and "/" not in s and "\\" not in s and "\x00" not in s and ".." not in s  # 拒 - 开头：防 ffmpeg 参数解析吃掉文件名


def _safe_id(s):
    """素材 id：ASCII 白名单 + 中文（CJK）——id 可能从中文文件名生成，query 传参 urlencode 后合法。"""
    return bool(s) and re.fullmatch(r"[A-Za-z0-9_\-\.\u4e00-\u9fff]{1,40}", s) and ".." not in s
sys.path.insert(0, os.path.join(ROOT, "autocut3"))
from video_meta import display_geometry as _display_geometry  # 入库几何探针：人机同权共享（rotation 必检）
PIPE = os.path.join(ROOT, "autocut3", "pipeline.py")
RENDER_JOBS = {}  # "name:sid" → {running,stage,pct,tail,ok}（R2 渲染进度：pipeline 以 PCT/STAGE 行协议输出）
FF = os.path.join(ROOT, "bin", "ffmpeg")
MIME = {"mp4": "video/mp4", "png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
        "mp3": "audio/mpeg", "json": "application/json", "ass": "text/plain; charset=utf-8",
        "srt": "text/plain; charset=utf-8", "mlt": "application/xml",
        "html": "text/html; charset=utf-8"}

def jload(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        d = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        u = urlparse(unquote(self.path, encoding="utf-8"))  # path 段统一解码：中文 id 的 percent 编码态在此还原（同族问题根治点）
        if u.path in ("/", "/index.html"):
            fp = os.path.join(ROOT, "studio", "index.html")
            try:
                body = open(fp, "rb").read()
            except FileNotFoundError:
                return self._json({"err": "studio/index.html 缺失"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path.startswith("/api/library/"):
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            pd = f"{ROOT}/projects/{name}"
            try:  # 素材审阅必须看得到素材：缺哪张缩略帧补哪张
                sys.path.insert(0, f"{ROOT}/autocut3")
                import library as LIB
                if LIB.ensure_thumbs(pd):
                    pass
            except Exception:
                pass
            self._json(jload(f"{pd}/materials/library.json", {}))
        elif u.path == "/api/enums":
            self._json(jload(f"{ROOT}/registry/enums.json", {}))
        elif u.path.startswith("/api/registry/"):
            # 注册表读接口（2026-09-14）：/api/registry/bgm → registry/bgm.json。人/Agent 同权。
            rname = u.path[len("/api/registry/"):]
            if not _safe(rname):
                return self._json({"err": "bad registry name"}, 400)
            return self._json(jload(f"{ROOT}/registry/{rname}.json", {}))
        elif u.path.startswith("/api/cover/"):
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            cover_dir = f"{ROOT}/projects/{name}/cover"
            os.makedirs(cover_dir, exist_ok=True)
            fp = f"{cover_dir}/cover.jpg"
            if os.path.isfile(fp):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(os.path.getsize(fp)))
                self.end_headers()
                with open(fp, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._json({"err": "cover 未生成"}, 404)
        if u.path.startswith("/api/sync/"):
            # 双向同步探针：先落库，后读取——Studio 每 5s 轮询这里，看见 Agent 的落库
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            p = f"{ROOT}/projects/{name}"
            if not os.path.isdir(p):
                return self._json({"err": "no such project"}, 404)
            _q = parse_qs(urlparse(self.path).query)
            sid = (_q.get("story") or [None])[0]
            if sid and not _safe(sid):
                return self._json({"err": "bad story id"}, 400)
            sdir = f"{p}/storylines"
            stories = {f[:-5]: int(os.path.getmtime(os.path.join(sdir, f)))
                       for f in (os.listdir(sdir) if os.path.isdir(sdir) else []) if f.endswith(".json")}
            return self._json({"ok": True,
                               "stories_mtime": int(os.path.getmtime(sdir)) if os.path.isdir(sdir) else 0,
                               "story_mtime": stories.get(sid, 0), "stories": stories})
        if u.path.startswith("/api/render-progress/"):
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            _q = parse_qs(u.query)
            sid = (_q.get("story") or [None])[0]
            job = RENDER_JOBS.get(f"{name}:{sid or 'main'}")
            if not job:  # CLI/Agent 渲染不在内存任务表——读渲染器落盘的状态文件（人机同权看过程）
                rs = jload(f"{ROOT}/projects/{name}/render.status", None)
                if rs and time.time() - os.path.getmtime(f"{ROOT}/projects/{name}/render.status") < 300:
                    job = rs  # 5 分钟内的状态才可信（更旧的是历史残留）
            job = job or {"running": False, "ok": None, "pct": 0, "stage": "", "tail": []}
            return self._json({"ok": True, "job": job})
        elif u.path == "/api/pulse":
            # 脉搏：全项目数据版本 + 全部渲染任务。前端 2.5s 探一次，变了就静默刷新对应视图
            # （2026-09-11：CLI/Agent 的变更界面看不见、必须手动刷新——人机同权的最后一公里）
            projs = []
            pdir = f"{ROOT}/projects"
            for d in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
                dp = f"{pdir}/{d}"
                if not os.path.isdir(dp) or d.startswith("_"):
                    continue
                sdir = f"{dp}/storylines"
                smt = max([os.path.getmtime(f"{sdir}/{f}") for f in os.listdir(sdir) if f.endswith(".json")],
                          default=0) if os.path.isdir(sdir) else 0
                pmt = os.path.getmtime(f"{dp}/project.json") if os.path.exists(f"{dp}/project.json") else 0
                lmt = os.path.getmtime(f"{dp}/materials/library.json") if os.path.exists(f"{dp}/materials/library.json") else 0
                omt = max([os.path.getmtime(f"{dp}/{f}") for f in os.listdir(dp) if f.startswith("out-")],
                          default=0) if os.path.isdir(dp) else 0
                projs.append({"name": d, "sig": round(max(smt, pmt, lmt, omt), 2)})
            jobs = {}
            for k, j in RENDER_JOBS.items():
                if j.get("running") or j.get("ok") is False:
                    jobs[k] = {"running": j.get("running"), "ok": j.get("ok"), "pct": j.get("pct"), "stage": j.get("stage")}
            for d in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:  # CLI 渲染的落盘状态也进脉搏
                rs = jload(f"{pdir}/{d}/render.status", None)
                if rs and time.time() - os.path.getmtime(f"{pdir}/{d}/render.status") < 300:
                    jobs[f"{d}:{'main'}"] = rs if d not in {k.split(':')[0] for k in jobs} else jobs.get(f"{d}:main", rs)
            packs = []
            pk_root = f"{ROOT}/materials/packs"
            for d in sorted(os.listdir(pk_root)) if os.path.isdir(pk_root) else []:
                pp = f"{pk_root}/{d}/pack.json"
                if os.path.exists(pp):
                    packs.append({"id": d, "sig": round(os.path.getmtime(pp), 2)})
            return self._json({"ok": True, "projs": projs, "jobs": jobs, "packs": packs})
        elif u.path == "/api/packs":
            pk_root = f"{ROOT}/materials/packs"
            out = []
            for d in sorted(os.listdir(pk_root)) if os.path.isdir(pk_root) else []:
                pj = jload(f"{pk_root}/{d}/pack.json", {})
                if pj:
                    out.append({"id": d, "name": pj.get("name", d), "n": len(pj.get("files", []))})
            return self._json(out)
        elif u.path == "/api/formats":
            formats = jload(f"{ROOT}/registry/formats.json", {})
            return self._json({k: v for k, v in formats.items() if not k.startswith("_")})
        elif u.path == "/api/projects":
            out = []
            pd = os.path.join(ROOT, "projects")
            if os.path.isdir(pd):
                for name in sorted(os.listdir(pd)):
                    p = os.path.join(pd, name)
                    if not os.path.isdir(p):
                        continue
                    pj = jload(f"{p}/project.json", {})
                    sids = [f[:-5] for f in sorted(os.listdir(f"{p}/storylines")) if f.endswith(".json")] \
                        if os.path.isdir(f"{p}/storylines") else []
                    if pj:  # 新档案项目：一个工程多条故事线
                        out.append({"name": name, "title": pj.get("title", name),
                                    "active": pj.get("active"), "stories": sids,
                                    "has_out": any(os.path.exists(f"{p}/out-{s}.mp4") for s in sids),
                                    "outs": [s for s in sids if os.path.exists(f"{p}/out-{s}.mp4")]})
                    elif os.path.exists(f"{p}/storyline.json"):  # 旧项目兼容
                        st = jload(f"{p}/storyline.json", {})
                        out.append({"name": name, "title": st.get("title", name),
                                    "active": None, "stories": [],
                                    "has_out": os.path.exists(f"{p}/out.mp4"), "outs": []})
            self._json(out)
        elif u.path.startswith("/api/project/"):
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            p = f"{ROOT}/projects/{name}"
            if not os.path.isdir(p):
                return self._json({"err": "no such project"}, 404)
            _q = parse_qs(urlparse(self.path).query)
            sid = (_q.get("story") or [None])[0]
            if sid and not _safe(sid):
                return self._json({"err": "bad story id"}, 400)
            sdir = f"{p}/storylines"
            sids_all = sorted(f[:-5] for f in (os.listdir(sdir) if os.path.isdir(sdir) else []) if f.endswith(".json"))
            if sid and sid not in sids_all:
                return self._json({"err": "no such story: %s" % sid}, 404)  # 显式指定即严格——静默换成别的线是错幕事故源
            if not sid and sids_all:
                sid = max(sids_all, key=lambda s: os.path.getmtime(f"{sdir}/{s}.json"))
            sfx = f"-{sid}" if sid else ""
            sp = (f"{sdir}/{sid}.json" if sid and os.path.exists(f"{sdir}/{sid}.json")
                  else (f"{p}/storyline.json" if not sids_all else f"{sdir}/{sids_all[0]}.json"))
            grid_dir = f"{ROOT}/docs/poc/studio-grid"
            gvs = sorted(f"/files/docs/poc/studio-grid/{f}" for f in os.listdir(grid_dir)
                         if f.endswith(".mp4")) if os.path.isdir(grid_dir) else []
            imgs = [f"/files/{rel}" for rel in
                    ("docs/poc/studio-grid.png", "docs/poc/v3-loop-v2.png", "docs/poc/v3-loop-verify.png")
                    if os.path.exists(os.path.join(ROOT, rel))]
            mlt = f"/files/projects/{name}/manual/{name}.mlt"
            enums = jload(f"{ROOT}/registry/enums.json", {})
            transition_ids = list((jload(f"{ROOT}/registry/transitions.json", {}) or {}).keys())
            enums["stickers"] = [{"id": k, **{kk: vv for kk, vv in v.items() if kk in ("desc", "pos", "duration")}}
                                 for k, v in (jload(f"{ROOT}/registry/stickers.json", {}) or {}).items()]
            enums["sfx"] = [{"id": k, "desc": v.get("desc", "")} for k, v in (jload(f"{ROOT}/registry/sfx.json", {}) or {}).items()]
            library = jload(f"{p}/materials/library.json", {})
            pack_files = []
            for pid in library.get("packs", []):
                pj = jload(f"{ROOT}/materials/packs/{pid}/pack.json", {})
                for f in pj.get("files", []):
                    f2 = dict(f); f2["pack"] = pid
                    f2["url"] = f"/files/materials/packs/{pid}/{f['file']}"
                    f2["thumb"] = f"/files/materials/packs/{pid}/thumbs/{f['id']}.jpg"
                    pack_files.append(f2)
            self._json({
                "story": sid, "stories": [f[:-5] for f in sorted(os.listdir(f"{p}/storylines")) if f.endswith(".json")] if os.path.isdir(f"{p}/storylines") else [],
                "outs": [s for s in ([f[:-5] for f in sorted(os.listdir(f"{p}/storylines")) if f.endswith(".json")] if os.path.isdir(f"{p}/storylines") else []) if os.path.exists(f"{p}/out-{s}.mp4")],
                "storyline": jload(sp, {}),
                "plan": jload(f"{p}/plan{sfx}.json", {}),
                "ass": open(f"{p}/subtitle{sfx}.ass", encoding="utf-8").read() if os.path.exists(f"{p}/subtitle{sfx}.ass") else "",
                "params": jload(f"{p}/params.json", {}),
                "enums": enums, "library": library, "pack_files": pack_files,
                "project": jload(f"{p}/project.json", {}),
                "transition_ids": transition_ids,
                "files": {"out": f"/files/projects/{name}/out{sfx}.mp4",
                          "grid_videos": gvs, "images": imgs, "mlt": mlt,
                          "cover": f"/files/projects/{name}/cover/cover{sfx}.jpg"},
            })
        elif u.path.startswith("/api/sub-preview/"):
            # 字幕样式预览：用该样式的 ASS 参数烧一句样例台词 → jpg（缓存到 assets/preview/）
            sid = u.path.split("/")[3]
            try:
                sid = urllib.parse.unquote(sid, encoding="utf-8")
            except Exception:
                pass
            if not _safe_id(sid):
                return self._json({"err": "bad id"}, 400)
            st = jload(f"{ROOT}/registry/subtitles.json", {}).get(sid)
            if not st:
                return self._json({"err": "no such style"}, 404)
            pv_dir = f"{ROOT}/assets/preview"
            os.makedirs(pv_dir, exist_ok=True)
            jpg = f"{pv_dir}/sub-{sid}.jpg"
            if not os.path.exists(jpg):
                # 样例 ASS：1280x720 深色底（模拟实拍画面）+ 该样式一行卡拉OK台词
                ass = "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n\n" \
                      "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n" \
                      "Style: PV,%s,%s,%s,%s,%s,&H00000000,0,0,0,0,100,100,0,0,1,%s,0,2,60,60,%s,1\n\n" \
                      "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" \
                      "Dialogue: 0,0:00:00.00,0:00:02.00,PV,,0,0,0,,{\\k30}装修{\\k30}避坑{\\k30}样例{\\k30}字幕\n" % (
                          st.get("font", "Smiley Sans"), int(st.get("size", 64) * 720 / 1920),
                          st.get("primary", "&H00FFFFFF"), st.get("secondary", "&H00F0F0F0"),
                          st.get("outline_col", "&H00101010"), st.get("border", 3), st.get("marginv", 180) * 720 // 1920)
                open(f"{pv_dir}/_pv_{sid}.ass", "w", encoding="utf-8").write(ass)
                subprocess.run([os.path.join(ROOT, "bin", "ffmpeg"), "-y", "-loglevel", "error",
                                "-f", "lavfi", "-i", "color=c=0x1a1e24:s=1280x720:d=0.1",
                                "-vf", "drawtext=text='':fontcolor=white",
                                "-i", f"{pv_dir}/_pv_{sid}.ass", "-map", "0:v", "-map", "1:s",
                                "-frames:v", "1", "-q:v", "3", jpg],
                               capture_output=True, timeout=30)
            return self._json({"ok": True, "url": f"/files/assets/preview/sub-{sid}.jpg"})
        elif u.path.startswith("/files/"):
            fp = os.path.realpath(os.path.join(ROOT, urllib.parse.unquote(u.path[len("/files/"):], encoding="utf-8")))  # path 段解码：中文名素材可达（试听/看图依赖）
            if not fp.startswith(os.path.realpath(ROOT)) or not os.path.isfile(fp):
                return self._json({"err": "not found"}, 404)
            ctype = MIME.get(fp.rsplit(".", 1)[-1].lower(), "application/octet-stream")
            size = os.path.getsize(fp)
            rng = self.headers.get("Range")
            if rng:  # 视频拖动进度条必需
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                start = int(m.group(1) or 0)
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(fp, "rb") as f:
                    f.seek(start); remain = end - start + 1
                    while remain > 0:
                        chunk = f.read(min(1 << 20, remain))
                        if not chunk: break
                        self.wfile.write(chunk); remain -= len(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(fp, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk: break
                        self.wfile.write(chunk)
        else:
            self._json({"err": "not found"}, 404)

    def do_POST(self):
        u = urlparse(unquote(self.path, encoding="utf-8"))  # 同 do_GET
        if u.path.startswith("/api/library/") and "/clip/" in u.path:
            parts = u.path.split("/")
            name, ci = parts[3], parts[-1]  # 修复：/clip/0 的编号在末段（此前误取 parts[4]="clip"）
            if not (_safe(name) and ci.isdigit()):
                return self._json({"err": "bad id"}, 400)
            pd = f"{ROOT}/projects/{name}"
            lib = jload(f"{pd}/materials/library.json", {})
            cut = next((c for c in lib.get("cuts", []) if str(c.get("cut_index")) == str(ci)), None)
            if not cut:
                return self._json({"err": "no such cut"}, 404)
            pv = f"{pd}/materials/previews"; os.makedirs(pv, exist_ok=True)
            out = f"{pv}/cut_{ci}.mp4"
            if not os.path.exists(out):
                subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                                "-ss", str(cut.get("in")), "-t", str(cut.get("duration") or 5),
                                "-i", f"{pd}/materials/src.mp4",
                                "-vf", "scale=360:-2", "-c:v", "libx264", "-crf", "26",
                                "-c:a", "aac", "-b:a", "96k", out],
                               capture_output=True, timeout=120)
            if not os.path.exists(out):
                return self._json({"err": "clip 生成失败"}, 500)
            return self._json({"ok": True, "video": f"/files/projects/{name}/materials/previews/cut_{ci}.mp4?t={int(os.path.getmtime(out))}",
                               "dur": cut.get("duration")})
        if u.path.startswith("/api/pack-mount/"):
            # 挂载/卸载：library.json packs 是唯一事实源
            parts = u.path.split("/")
            name, pid, op = parts[3], parts[4], parts[5]  # mount / unmount
            if not (_safe(name) and _safe(pid)):
                return self._json({"err": "bad id"}, 400)
            lp = f"{ROOT}/projects/{name}/materials/library.json"
            if not os.path.isfile(lp):
                return self._json({"err": "no such project"}, 404)
            lib = jload(lp, {})
            packs = lib.setdefault("packs", [])
            if op == "mount":
                if not os.path.isfile(f"{ROOT}/materials/packs/{pid}/pack.json"):
                    return self._json({"err": "包不存在"}, 404)
                if pid not in packs:
                    packs.append(pid)
            elif op == "unmount":
                lib["packs"] = [x for x in packs if x != pid]
            else:
                return self._json({"err": "op 仅 mount/unmount"}, 400)
            json.dump(lib, open(lp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return self._json({"ok": True, "packs": lib["packs"]})
        if u.path.startswith("/api/pack-create/"):
            # 建包：空壳 pack.json + 目录（素材文件入仓仍走文件系统/CLI，人机同权）
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            pid = str(body.get("id") or "").strip()
            if not _safe(pid):
                return self._json({"err": "包 id 只允许字母数字下划线中划线"}, 400)
            pk_dir = f"{ROOT}/materials/packs/{pid}"
            if os.path.exists(pk_dir):
                return self._json({"err": "包已存在"}, 409)
            os.makedirs(f"{pk_dir}/thumbs", exist_ok=True)
            # 初建写，锁豁免注记（rmw_smoke 守卫）：目录刚建、files=[]、无并发对象——锁无竞争可解
            json.dump({"id": pid, "name": str(body.get("name") or pid),
                       "shot_at": str(body.get("shot_at") or ""),
                       "time_rule": "0.1s 量化（与词级时间戳规范一致）",
                       "separation": "拍摄层 usable/defects 归素材包；创作取用归各项目 storyline",
                       "files": []},
                      open(f"{pk_dir}/pack.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return self._json({"ok": True, "id": pid})
        if u.path.startswith("/api/project-create/"):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            name = str(body.get("name") or "").strip()
            title = str(body.get("title") or "").strip() or name
            fmt_id = str(body.get("format") or "vertical")
            if not _safe(name):
                return self._json({"err": "项目名只允许字母数字下划线中划线"}, 400)
            fmts = jload(f"{ROOT}/registry/formats.json", {})
            if fmt_id not in fmts:
                return self._json({"err": "未知画幅: %s" % fmt_id}, 400)
            pd = f"{ROOT}/projects/{name}"
            if os.path.exists(pd):
                return self._json({"err": "项目已存在"}, 409)
            os.makedirs(f"{pd}/materials", exist_ok=True)
            os.makedirs(f"{pd}/storylines", exist_ok=True)
            os.makedirs(f"{pd}/cover", exist_ok=True)
            json.dump({"id": name, "title": title, "format": fmt_id,
                       "note": str(body.get("note") or "")},
                      open(f"{pd}/project.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            json.dump({"version": 2, "packs": [], "sources": [], "cuts": []},
                      open(f"{pd}/materials/library.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return self._json({"ok": True, "name": name, "format": fmt_id})
        if u.path.startswith("/api/render-start/"):
            # R2：渲染改为任务模型——启动后轮询 /api/render-progress，前端实时百分比
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            _q = parse_qs(u.query)
            sid = (_q.get("story") or [None])[0]
            if sid and not _safe(sid):
                return self._json({"err": "bad story id"}, 400)
            p = f"{ROOT}/projects/{name}"
            if not os.path.isdir(p):
                return self._json({"err": "no such project"}, 404)
            params = {}
            for k in ("flash", "hold", "duration"):
                if k in _q:
                    try: params[k] = float(_q[k][0])
                    except ValueError: pass
            if "grain" in _q:
                try: params["grain"] = int(_q["grain"][0])
                except ValueError: pass
            with open(f"{p}/params.json", "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False, indent=1)
            key = f"{name}:{sid or 'main'}"
            job = RENDER_JOBS.get(key)
            if job and job.get("running"):
                return self._json({"ok": False, "err": "该故事线已有渲染在进行"}, 409)
            RENDER_JOBS[key] = {"running": True, "stage": "plan", "pct": 0, "tail": [], "ok": None, "t0": time.time()}
            def _run(job=RENDER_JOBS[key]):
                try:
                    r = subprocess.Popen([sys.executable, "-u", PIPE, "render", p] + ([sid] if sid else []),
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                    for line in r.stdout:
                        line = line.strip()
                        if line.startswith("PCT "):
                            try: job["pct"] = int(line.split()[1])
                            except Exception: pass
                        elif line.startswith("STAGE "):
                            job["stage"] = line.split(" ", 1)[1]
                        elif line:
                            job["tail"].append(line)
                            del job["tail"][:-6]
                    r.wait()
                    job["ok"] = r.returncode == 0
                    job["running"] = False
                except Exception as e:
                    job["running"] = False
                    job["ok"] = False
                    job["tail"].append(str(e)[:200])
            threading.Thread(target=_run, daemon=True).start()
            return self._json({"ok": True, "job": key})
        if u.path.startswith("/api/pack-delete/"):
            # R1 尾巴：删包 = 归档到 materials/_attic/ + 从所有项目 library.json 摘除挂载
            pid = u.path.split("/")[3]
            if not _safe(pid):
                return self._json({"err": "bad id"}, 400)
            pk_dir = f"{ROOT}/materials/packs/{pid}"
            if not os.path.isdir(pk_dir):
                return self._json({"err": "no such pack"}, 404)
            touched = []
            pd_root = f"{ROOT}/projects"
            for nm in sorted(os.listdir(pd_root)) if os.path.isdir(pd_root) else []:
                lp = f"{pd_root}/{nm}/materials/library.json"
                lib = jload(lp, {})
                if lib and pid in (lib.get("packs") or []):
                    lib["packs"] = [x for x in lib["packs"] if x != pid]
                    json.dump(lib, open(lp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    touched.append(nm)
            attic = f"{ROOT}/materials/_attic/{time.strftime('%Y%m%d-%H%M%S')}-{pid}"
            os.makedirs(os.path.dirname(attic), exist_ok=True)
            shutil.move(pk_dir, attic)
            return self._json({"ok": True, "attic": attic, "unmounted": touched})
        if u.path.startswith("/api/pack-upload/"):
            # 上传素材：raw body = 文件字节；人（浏览器 file input）与 Agent（HTTP 语义）同权
            pid = u.path.split("/")[3]
            if not _safe(pid):
                return self._json({"err": "bad id"}, 400)
            _q = parse_qs(u.query)
            fname = (_q.get("filename") or [""])[0]
            if not _filename(fname) or "." not in fname:
                return self._json({"err": "bad filename（禁路径分隔符/隐藏文件/..；中文名允许）"}, 400)
            pk_dir = f"{ROOT}/materials/packs/{pid}"
            if not os.path.isdir(pk_dir):
                os.makedirs(pk_dir, exist_ok=True)  # 目录兜底：忘先 pack-create 时自动建包（2026-09-14 彭程林实战踩坑）
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 500 << 20:
                return self._json({"err": "bad size %d（上限 500MB）" % n}, 400)
            ext = fname.rsplit(".", 1)[-1].lower()
            kind = {"mp4": "video", "mov": "video", "m4v": "video", "webm": "video",
                    "jpg": "image", "jpeg": "image", "png": "image", "webp": "image", "gif": "image",
                    "mp3": "audio", "wav": "audio", "m4a": "audio", "aac": "audio", "flac": "audio"}.get(ext)
            if not kind:
                return self._json({"err": "不支持的格式: .%s" % ext}, 400)
            dest = f"{pk_dir}/{fname}"
            if os.path.exists(dest):  # 同名不覆盖——加时间戳后缀
                stem, e2 = fname.rsplit(".", 1)
                dest = f"{pk_dir}/{stem}_{time.strftime('%H%M%S')}.{e2}"
                fname = os.path.basename(dest)
            # R2-2（Claude 复核）：upload 全程无锁=9-11 事故同族——读-改-写窗口以分钟计
            # （流式收文件最长达 500MB），必须与 transcribe/understand/proofread/usable 同锁
            import fcntl
            _lf = open(f"{pk_dir}/.lock", "w")
            fcntl.flock(_lf, fcntl.LOCK_EX)
            try:
                pk = jload(f"{pk_dir}/pack.json", {})
                if not pk:  # 目录兜底的伴生逻辑：pack.json 缺失时初始化空壳（与 pack-create 同构）
                    pk = {"id": pid, "name": pid, "files": []}
                    json.dump(pk, open(f"{pk_dir}/pack.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            finally:
                fcntl.flock(_lf, fcntl.LOCK_UN); _lf.close()
            remain = n
            with open(dest, "wb") as f:
                while remain > 0:
                    chunk = self.rfile.read(min(1 << 20, remain))
                    if not chunk:
                        break
                    f.write(chunk)
                    remain -= len(chunk)
            duration = 0.0
            width = height = 0
            rotation = None
            try:
                g = _display_geometry(dest)  # 显示尺寸（rotation 转正）——手机素材必检
                duration = g["duration"]
                width, height = g["w"], g["h"]
                rotation = g["rotation"]
            except Exception:
                pass  # 探测失败回落 0（与旧行为一致，不阻塞上传）
            _stem = fname.rsplit(".", 1)[0]
            _mnum = re.search(r"[Mm]\d{3,}", _stem)  # 手机素材命名 20260827_M0269 → 提取 M0269（短·唯一·可辨识）
            mid = _mnum.group(0).upper() if _mnum else (re.sub(r"[\s/\\\x00]+", "_", _stem)[:24].upper() or "MAT")  # id 统一大写惯例（rmw_smoke R3-2 回归锚）
            base = mid
            # id 去重改到写锁内进行（R2-2 残留自查）：预检读只是存在性检查，
            # 并发上传各自基于陈旧快照算 id 仍会撞号——最终以锁内重读的最新 pack 为准
            thumb = None
            if kind != "audio":
                try:
                    os.makedirs(f"{pk_dir}/thumbs", exist_ok=True)
                    subprocess.run([FF, "-y", "-loglevel", "error",
                                    "-ss", ("1" if kind == "video" else "0"), "-i", dest,
                                    "-vframes", "1", "-vf", "scale=480:-2",
                                    f"{pk_dir}/thumbs/_t{__import__('hashlib').md5((pid + fname).encode()).hexdigest()[:10]}.jpg"], capture_output=True, timeout=30)
                    thumb = f"/files/materials/packs/{pid}/thumbs/_t{__import__('hashlib').md5((pid + fname).encode()).hexdigest()[:10]}.jpg"
                except Exception:
                    pass
            entry = {"id": mid, "file": fname, "kind": kind,
                     **({"duration": duration} if kind != "image" else {}),
                     **({"width": width, "height": height} if width else {}),
                     **({"_rotated": True, "_rotation": rotation}
                        if (width and height and width < height and rotation is not None) else {}),
                     "size_mb": round(n / 1e6, 2), "tags": ["root"], "usable": True,
                     "review": "pending-review", "defects": [],
                     "audit": {"transcript": "none" if kind == "image" else "pending",
                               "proofread": "none" if kind == "image" else "pending",
                               "visual": "none" if kind == "audio" else "pending"},
                     "source_title": "上传素材"}
            # append+dump 在同一把锁内完成。R3-2：重读也必须进锁（原读在锁外=写锁没护住读点，
            # upload vs 转写的丢更新仍在）；读失败经 finally 释放锁后 500 放弃登记
            import fcntl
            _lf = open(f"{pk_dir}/.lock", "w")
            fcntl.flock(_lf, fcntl.LOCK_EX)
            try:
                pk2 = jload(f"{pk_dir}/pack.json", {})
                if not pk2:
                    return self._json({"err": "pack 重读失败，放弃登记——素材文件已存，请重传或手动登记"}, 500)
                ids = {f.get("id") for f in pk2.get("files", [])}
                k = 2
                while mid in ids:
                    mid = f"{base}{k}"
                    k += 1
                if mid != base:
                    entry["id"] = mid
                    if thumb:  # 缩略图按预检 mid 命名——id 撞号改名后同步改名，防错位
                        try:
                            os.rename(f"{pk_dir}/thumbs/{base}.jpg", f"{pk_dir}/thumbs/{mid}.jpg")
                        except Exception:
                            pass
                        thumb = f"/files/materials/packs/{pid}/thumbs/{mid}.jpg"
                pk2.setdefault("files", []).append(entry)
                json.dump(pk2, open(f"{pk_dir}/pack.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            finally:
                fcntl.flock(_lf, fcntl.LOCK_UN); _lf.close()
            entry["url"] = f"/files/materials/packs/{pid}/{fname}"
            entry["thumb"] = thumb
            return self._json({"ok": True, "entry": entry})
        if u.path.startswith("/api/storyline-delete/"):
            # 删除故事线 = 归档到 projects/_attic/<pid>/<时间戳>/（可手工找回），至少保留一条
            parts = u.path.split("/")
            name = parts[3]
            _q = parse_qs(urlparse(self.path).query)
            sid = (_q.get("story") or [None])[0]
            if sid and not _safe(sid):
                return self._json({"err": "bad story id"}, 400)
            p = f"{ROOT}/projects/{name}"
            sdir = f"{p}/storylines"
            sids = [f[:-5] for f in os.listdir(sdir) if f.endswith(".json")] if os.path.isdir(sdir) else []
            if not sid or sid not in sids:
                return self._json({"err": "no such story"}, 404)
            if len(sids) <= 1:
                return self._json({"err": "至少保留一条故事线（整项目请用项目删除）"}, 400)
            attic = f"{ROOT}/projects/_attic/{name}/{time.strftime('%Y%m%d-%H%M%S')}-{sid}"
            os.makedirs(attic, exist_ok=True)
            moved = []
            def _mv(srcf):
                if os.path.exists(srcf):
                    shutil.move(srcf, os.path.join(attic, os.path.basename(srcf)))
                    moved.append(os.path.basename(srcf))
            _mv(f"{sdir}/{sid}.json")
            for suf in (f"plan-{sid}.json", f"subtitle-{sid}.ass", f"out-{sid}.mp4", f"cover/cover-{sid}.jpg"):
                _mv(os.path.join(p, suf))
            if os.path.isdir(f"{p}/previews"):
                for f in os.listdir(f"{p}/previews"):
                    if f.startswith(f"beat_{sid}_"):
                        _mv(os.path.join(f"{p}/previews", f))
            pj = jload(f"{p}/project.json", {})
            if pj.get("active") == sid and sids:
                rest = [s for s in sids if s != sid]
                pj["active"] = rest[0]
                json.dump(pj, open(f"{p}/project.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return self._json({"ok": True, "attic": attic, "moved": moved})
        if u.path.startswith("/api/project-delete/"):
            # 删除项目 = 整目录归档到 projects/_attic/（素材包是根级资产，不受影响）
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            p = f"{ROOT}/projects/{name}"
            if not os.path.isdir(p):
                return self._json({"err": "no such project"}, 404)
            attic = f"{ROOT}/projects/_attic/{name}-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.move(p, attic)
            return self._json({"ok": True, "attic": attic})
        if u.path.startswith("/api/proofread-pack/"):
            # 人工校对：转写文本以人的修正为准（素材权威解读的最后一道）
            parts = u.path.split("/")
            pid_, fid = parts[3], parts[4]
            if not (_safe(pid_) and _safe_id(fid)):
                return self._json({"err": "bad id"}, 400)
            pp = f"{ROOT}/materials/packs/{pid_}/pack.json"
            pj = jload(pp, {})
            if not pj:
                return self._json({"err": "no such pack"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"err": "bad json"}, 400)
            import fcntl
            # RMW 守卫战果（rmw_smoke 扫到）：此端点读在锁外、写在锁外——第五个漏网写者，四轮都没点到
            with open(os.path.join(os.path.dirname(pp), ".lock"), "w") as _lf:
                fcntl.flock(_lf, fcntl.LOCK_EX)
                pj2 = jload(pp, {})
                if not pj2:
                    return self._json({"err": "pack 重读失败，已放弃——请重试"}, 500)
                for f in pj2.get("files", []):
                    if f.get("id") == fid:
                        if f.get("transcript") is not None:
                            f["transcript"]["text"] = str(body.get("text", ""))
                        f.setdefault("audit", {})["proofread"] = "done"
                        json.dump(pj2, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                        return self._json({"ok": True, "file": f})
                return self._json({"err": "no such material"}, 404)
        if u.path.startswith("/api/proofread-cut/"):
            # n001 切片台词的人工校对
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            lp = f"{ROOT}/projects/{name}/materials/library.json"
            lib = jload(lp, {})
            if not lib:
                return self._json({"err": "library 未建"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"err": "bad json"}, 400)
            try:
                ci = int(body.get("cut_index", -1))
            except (TypeError, ValueError):
                return self._json({"err": "bad cut_index"}, 400)
            for c in lib.get("cuts", []):
                if c.get("cut_index") == ci:
                    c["text"] = str(body.get("text", ""))
                    c.setdefault("audit", {})["proofread"] = "done"
                    json.dump(lib, open(lp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    return self._json({"ok": True})
            return self._json({"err": "no such cut"}, 404)
        if u.path.startswith("/api/draft/"):
            # 人机等价入口：Agent 走 CLI，人点「✨ AI 起草」——同一落盘 storylines/aidraft*.json
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"err": "bad json"}, 400)
            intent = str(body.get("intent") or "").strip()
            if not intent:
                return self._json({"err": "缺少起草意图"}, 400)
            try:
                sys.path.insert(0, os.path.join(ROOT, "autocut3"))
                import draft as D
                sid, draft, beats = D.run(name, intent, save=True)
                return self._json({"ok": True, "sid": sid, "beats": len(beats),
                                   "title": draft.get("title"), "outline": draft.get("outline")})
            except Exception as e:
                return self._json({"err": str(e)[:250]}, 500)
        if u.path.startswith("/api/understand/"):
            # 人机等价入口：Agent 走 CLI，人点素材卡「⟳ 画面识别」——同一落盘 pack.json
            pid_ = u.path.split("/")[3]
            if not _safe(pid_):
                return self._json({"err": "bad id"}, 400)
            _q = parse_qs(urlparse(self.path).query)
            mid = (_q.get("material") or [None])[0]
            if not _safe_id(mid):
                return self._json({"err": "bad id"}, 400)
            prov = (_q.get("vision") or [None])[0]
            force = (_q.get("force") or ["0"])[0] in ("1", "true")
            try:
                sys.path.insert(0, os.path.join(ROOT, "autocut3"))
                import understand as U
                res = U.understand_pack(pid_, material=mid, provider=prov, force=force)
                r = res.get(mid, {})
                if not r.get("ok"):
                    return self._json({"ok": False, "err": r.get("err", "未找到该素材或无需识别")}, 404)
                pk = jload(f"{ROOT}/materials/packs/{pid_}/pack.json", {})
                f = next((x for x in pk.get("files", []) if x.get("id") == mid), None)
                return self._json({"ok": True, "file": f})
            except Exception as e:
                return self._json({"ok": False, "err": str(e)[:200]}, 500)
        if u.path.startswith("/api/transcribe/"):
            # 人机等价入口：Agent 走 CLI，人点素材卡「转写」按钮——同一落盘 pack.json
            pid_ = u.path.split("/")[3]
            if not _safe(pid_):
                return self._json({"err": "bad id"}, 400)
            _q = parse_qs(urlparse(self.path).query)
            mid = (_q.get("material") or [None])[0]
            if not _safe_id(mid):
                return self._json({"err": "bad id"}, 400)
            prov = (_q.get("asr") or [None])[0]
            force = (_q.get("force") or ["0"])[0] in ("1", "true")
            try:
                sys.path.insert(0, os.path.join(ROOT, "autocut3"))
                import transcribe as T
                res = T.transcribe_pack(pid_, material=mid, provider=prov, force=force)
                r = res.get(mid, {})
                if not r.get("ok"):
                    return self._json({"ok": False, "err": r.get("err", "未找到该素材或无需转写")}, 404)
                pk = jload(f"{ROOT}/materials/packs/{pid_}/pack.json", {})
                f = next((x for x in pk.get("files", []) if x.get("id") == mid), None)
                return self._json({"ok": True, "file": f})
            except Exception as e:
                return self._json({"ok": False, "err": str(e)[:200]}, 500)
        if u.path.startswith("/api/pack/") and u.path.endswith("/usable"):
            parts = u.path.split("/")
            pid, fid = parts[3], parts[4]
            if not (_safe(pid) and _safe_id(fid)):
                return self._json({"err": "bad id"}, 400)
            pp = f"{ROOT}/materials/packs/{pid}/pack.json"
            pj = jload(pp, {})
            if not pj:
                return self._json({"err": "no such pack"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"err": "bad json"}, 400)
            for f in pj.get("files", []):
                if f.get("id") == fid:
                    f["usable"] = bool(body.get("usable", True))
                    f["defects"] = body.get("defects", [])
                    if body.get("review"):
                        f["review"] = body["review"]
            # P1#5 完整版：锁覆盖读-改-写全程。R2-1（Claude 复核）：原 `or pj` 陈旧回退是反向保险——
            # 重读失败时会把锁前旧快照落盘、覆盖并发已提交变更；重读失败必须放弃本次切换（幂等重试）
            import fcntl
            # R3-1：重读移入锁内——原读在锁外，锁只保住写点，读点仍拿陈旧快照（RMW 未原子）
            with open(f"{ROOT}/materials/packs/{pid}/.lock", "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                pj2 = jload(pp, {})
                if not pj2:  # with 语义保证 return 时释放锁——读失败放弃变更，不落任何盘
                    return self._json({"err": "pack 重读失败（IO 异常），已放弃本次变更——请重试"}, 500)
                for f in pj2.get("files", []):
                    if f.get("id") == fid:
                        f["usable"] = bool(body.get("usable", True))
                        f["defects"] = body.get("defects", [])
                        if body.get("review"):
                            f["review"] = body["review"]
                with open(pp, "w", encoding="utf-8") as f:
                    json.dump(pj2, f, ensure_ascii=False, indent=1)
            tgt = next((f for f in pj2["files"] if f.get("id") == fid), None)
            return self._json({"ok": True, "file": tgt})
        if u.path.startswith("/api/library/") and u.path.endswith("/usable"):
            # 拍摄层废片决策：usable 只评拍摄层（说错/卡等/重复/气口），不评创作取用
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            lp = f"{ROOT}/projects/{name}/materials/library.json"
            lib = jload(lp, {})
            if not lib:
                return self._json({"err": "library 未建"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"err": "bad json"}, 400)
            try:
                ci = int(body.get("cut_index", -1))
            except (TypeError, ValueError):
                return self._json({"err": "bad cut_index"}, 400)
            for c in lib.get("cuts", []):
                if c.get("cut_index") == ci:
                    c["usable"] = bool(body.get("usable", True))
                    c["defects"] = body.get("defects", [])
            with open(lp, "w", encoding="utf-8") as f:
                json.dump(lib, f, ensure_ascii=False, indent=1)
            return self._json({"ok": True, "cut": next((c for c in lib["cuts"] if c["cut_index"] == ci), None)})
        if u.path.startswith("/api/beat/"):
            # 幕级预览渲染：反馈半径缩小到一幕（秒级），绝低压力快速剪辑的核心
            parts = u.path.split("/")
            name, beat_no = parts[3], parts[4]
            if not beat_no.isdigit():
                return self._json({"err": "bad beat no"}, 400)
            _q = parse_qs(urlparse(self.path).query)
            sid = (_q.get("story") or [None])[0]
            if sid and not _safe(sid):
                return self._json({"err": "bad story id"}, 400)
            tag = (sid + "_") if sid else ""
            p = f"{ROOT}/projects/{name}"
            try:
                r = subprocess.run([sys.executable, PIPE, "beat", p, beat_no] + ([sid] if sid else []),
                                   capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return self._json({"ok": False, "log": "幕渲染超时"})
            ok = r.returncode == 0 and "BEAT OK" in r.stdout
            out = f"/files/projects/{name}/previews/beat_{tag}{beat_no}.mp4?t={int(time.time())}" if ok else None
            return self._json({"ok": ok, "video": out, "log": (r.stdout + r.stderr)[-400:]})
        if u.path.startswith("/api/storyline/"):
            # 人和 Agent 共用的故事线写入口（等价机制：所有编辑落为同一 JSON 变更）
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            _q = parse_qs(urlparse(self.path).query)
            sid = (_q.get("story") or [None])[0]
            if sid and not _safe(sid):
                return self._json({"err": "bad story id"}, 400)
            sp = f"{ROOT}/projects/{name}/storylines/{sid}.json" if sid else f"{ROOT}/projects/{name}/storyline.json"
            is_new = bool(sid) and not os.path.isfile(sp)
            if not os.path.isfile(sp) and not is_new:
                return self._json({"err": "no such story"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"err": "bad json"}, 400)
            if is_new:
                os.makedirs(os.path.dirname(sp), exist_ok=True)
            story = jload(sp, {})
            if "title" in body:
                story["title"] = str(body["title"])
            if "outline" in body:
                story["outline"] = str(body["outline"])
            meta = body.get("meta") or {}
            if meta:
                story.setdefault("meta", {})
                for k, v in meta.items():
                    if isinstance(v, dict) and isinstance(story["meta"].get(k), dict):
                        story["meta"][k].update(v)
                    else:
                        story["meta"][k] = v
            if isinstance(body.get("beats"), list):
                story["beats"] = body["beats"]  # 幕序列整体替换（编号幕，编辑器持有全文）
            if sid:  # 多故事线：写入 storylines/<sid>.json
                json.dump(story, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                return self._json({"ok": True, "storyline": story})
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(story, f, ensure_ascii=False, indent=1)
            return self._json({"ok": True, "storyline": story})
        if u.path.startswith("/api/render/"):
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            p = f"{ROOT}/projects/{name}"
            if not os.path.isdir(p):
                return self._json({"err": "no such project"}, 404)
        if u.path.startswith("/api/registry-save/"):
            # 注册表整体保存（前端管理面板编辑后 PUT）：白名单五件套，enums/formats 语义枚举不开放
            rname = u.path.split("/")[3]
            if rname not in ("bgm", "subtitles", "transitions", "sfx", "stickers"):
                return self._json({"err": "registry 不可编辑: " + rname}, 400)
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 2 << 20:
                return self._json({"err": f"bad size {n}"}, 400)
            try:
                obj = json.loads(self.rfile.read(n))
            except Exception:
                return self._json({"err": "bad json"}, 400)
            if not isinstance(obj, dict):
                return self._json({"err": "registry 必须是对象"}, 400)
            with open(f"{ROOT}/registry/{rname}.json", "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
            return self._json({"ok": True, "count": len([k for k in obj if not k.startswith("_")])})
        if u.path.startswith("/api/registry-delete/"):
            # 删注册表条目：不动 assets 文件（可能被历史故事线引用），返回 file 供人工清理
            parts = u.path.split("/")
            rname = parts[3]
            rid = parts[4] if len(parts) > 4 else ""
            if rname not in ("bgm", "subtitles", "transitions", "sfx", "stickers") or not _safe_id(rid):
                return self._json({"err": "bad registry/id（id 支持中文）"}, 400)
            regp = f"{ROOT}/registry/{rname}.json"
            reg = jload(regp, {})
            if rid not in reg:
                return self._json({"err": "no such entry"}, 404)
            gone = reg.pop(rid)
            with open(regp, "w", encoding="utf-8") as f:
                json.dump(reg, f, ensure_ascii=False, indent=1)
            return self._json({"ok": True, "removed_file": gone.get("file")})
        if u.path.startswith("/api/registry-upload/"):
            # 效果素材导入（2026-09-14）：/api/registry-upload/bgm/myid → assets/music/myid.mp3
            # + registry/bgm.json 自动加条目。白名单按类别：音频(bgm,sfx) mp3/wav/m4a/flac，图片(stickers) png。
            # 文件名按真实扩展名落盘；同时写注册表条目（name=文件名，desc 标注导入来源）。
            parts = u.path.split("/")
            kind = parts[3]
            rid = parts[4] if len(parts) > 4 else ""
            KIND = {"bgm": ("assets/music", "bgm.json", "music",
                            {"mp3": "audio/mpeg", "wav": "audio/x-wav", "m4a": "audio/mp4", "flac": "audio/flac"}),
                    "sfx": ("assets/sfx", "sfx.json", "audio",
                            {"mp3": "audio/mpeg", "wav": "audio/x-wav", "m4a": "audio/mp4", "flac": "audio/flac"}),
                    "stickers": ("assets/stickers", "stickers.json", "image", {"png": "image/png"})}
            if kind not in KIND or not _safe_id(rid):
                return self._json({"err": "bad kind/id（id 支持中文；禁路径分隔符/..）"}, 400)
            _q = parse_qs(u.query)
            fname = (_q.get("filename") or [""])[0]
            if not _filename(fname) or "." not in fname:
                return self._json({"err": "bad filename（中文名允许；禁路径分隔符/隐藏文件/..）"}, 400)
            ext = fname.rsplit(".", 1)[-1].lower()
            subdir, regf, _, mime = KIND[kind]
            if ext not in mime:
                return self._json({"err": "不支持的格式: .%s（%s 类接受 %s）" % (ext, kind, "/".join(sorted(mime)))}, 400)
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 50 << 20:
                return self._json({"err": f"bad size {n}"}, 400)
            dest_dir = f"{ROOT}/{subdir}"
            os.makedirs(dest_dir, exist_ok=True)
            fp = f"{dest_dir}/{rid}.{ext}"
            with open(fp, "wb") as f:
                f.write(self.rfile.read(n))
            regp = f"{ROOT}/registry/{regf}"
            reg = jload(regp, {})
            reg[rid] = {"file": f"{subdir}/{rid}.{ext}", "name": fname.rsplit(".", 1)[0],
                        "desc": f"导入于 {time.strftime('%Y-%m-%d %H:%M')} · {n}B"}
            with open(regp, "w", encoding="utf-8") as f:
                json.dump(reg, f, ensure_ascii=False, indent=1)
            return self._json({"ok": True, "id": rid, "registry": regf, "file": fp[len(ROOT)+1:]})
        if u.path.startswith("/api/cover-upload/"):
            name = u.path.split("/")[3]
            if not _safe(name):
                return self._json({"err": "bad id"}, 400)
            cover_dir = f"{ROOT}/projects/{name}/cover"
            os.makedirs(cover_dir, exist_ok=True)
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 10 << 20:
                return self._json({"err": f"bad size {n}"}, 400)
            with open(f"{cover_dir}/upload.jpg", "wb") as f:
                f.write(self.rfile.read(n))
            return self._json({"ok": True, "path": f"cover/upload.jpg ({n}B)"})
        if u.path.startswith("/api/render/"):
            qs = parse_qs(u.query)
            sid = (qs.get("story") or [None])[0]
            params = {}
            for k in ("flash", "hold", "duration"):
                if k in qs:
                    try: params[k] = float(qs[k][0])
                    except ValueError: pass
            if "grain" in qs:
                try: params["grain"] = int(qs["grain"][0])
                except ValueError: pass
            with open(f"{p}/params.json", "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False, indent=1)
            try:
                r = subprocess.run([sys.executable, PIPE, "render", p] + ([sid] if sid else []),
                                   capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                return self._json({"ok": False, "log": "渲染超时(600s)"})
            sfx = f"-{sid}" if sid else ""
            ok = r.returncode == 0 and "RENDER OK" in r.stdout
            vid = f"/files/projects/{name}/out{sfx}.mp4?t={int(os.path.getmtime(f'{p}/out{sfx}.mp4'))}" if ok else None
            self._json({"ok": ok, "log": (r.stdout + r.stderr)[-600:], "video": vid})
        else:
            self._json({"err": "not found"}, 404)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)  # 只绑本机：素材不出本机
    print(f"Studio: http://localhost:{port}")
    srv.serve_forever()
