#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 视频生成器（MiniMax H3 / Hailuo 系）——独立生成器，不接入渲染管线。

产物语义（工程哲学）：生成的片段是【素材】不是【成片】——落进 materials/packs/ai-generated/，
与拍摄素材走完全相同的审计流程（转写/识别/usable），将来由起草器像普通素材一样取用。

异步任务模型（官方建议轮询间隔 10s）：
  ① POST /v1/video_generation {model, prompt, duration, resolution[, first_frame_image]} → task_id
  ② GET  /v1/query/video_generation?task_id= → status: Queueing/Processing/Success/Fail
  ③ GET  /v1/files/retrieve?file_id= → file.download_url → 下载 mp4

用法：
  python3 autocut3/video_gen.py gen <project> --prompt "..." [--image f.jpg] [--duration 6] [--res 768P] [--model MiniMax-H3]
  python3 autocut3/video_gen.py poll <project> [--task-id xxx] [--latest] [--wait]

图生视频：--image 本地图片自动转 base64 data URL（支持 jpg/png）。
"""
import argparse, base64, json, os, subprocess, sys, time, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asr import load_services, resolve_key  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FF = os.path.join(ROOT, "bin", "ffmpeg")
GEN_PACK = "ai-generated"


def _backend():
    sv = load_services()
    vs = sv.get("video") or {}
    be = (vs.get("backends") or {}).get(vs.get("provider") or "minimax")
    if not be:
        raise RuntimeError("services.json 缺 video 段")
    return be


def _call(be, path, body=None, method="GET"):
    key = resolve_key(be)
    req = urllib.request.Request(
        be["base_url"].rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=be.get("timeout_s", 60)).read()), None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {}, e.code


def submit(project, prompt, image=None, duration=None, res=None, model=None):
    be = _backend()
    model = model or be.get("model", "MiniMax-H3")
    body = {"model": model, "prompt": prompt,
            "duration": int(duration or be.get("duration", 6)),
            "resolution": res or be.get("resolution", "768P")}
    if image:
        if not os.path.exists(image):
            raise RuntimeError("无图片: %s" % image)
        ext = image.rsplit(".", 1)[-1].lower()
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        b64 = base64.b64encode(open(image, "rb").read()).decode()
        body["first_frame_image"] = "data:image/%s;base64,%s" % (mime, b64)
    def _vpath(m):
        # H3 系走根级 /v2（多模态 content schema）；Hailuo 系走 /v1。base_url 以 /v1 结尾，v2 需剥掉
        if "h3" in m.lower():
            return be["base_url"].rstrip("/").rsplit("/v1", 1)[0] + "/v2/video_generation"
        return "/video_generation"
    if "h3" in model.lower():  # v2 统一上下文 schema：prompt 包进 content 数组
        body["content"] = [{"type": "text", "text": prompt}]
        body.pop("prompt", None)
    resp, code = _call(be, _vpath(model), body, "POST")
    def _errmsg(r):
        if r.get("error"):
            return (r["error"] or {}).get("message", "v2 error")
        st = (r.get("base_resp") or {})
        return "[%s] %s" % (st.get("status_code"), st.get("status_msg")) if st.get("status_code") not in (None, 0) else ""
    st = (resp.get("base_resp") or {})
    bad = bool(resp.get("error")) or st.get("status_code", -1) not in (0, None)
    if bad:
        fb = be.get("fallback_model")
        if fb and model != fb:  # 模型名/参数/套餐不合法 → 回退档重试一次
            print("模型 %s 被拒（%s），回退 %s" % (model, _errmsg(resp), fb))
            body["model"] = fb
            if "content" in body:  # v2 schema（content 数组）必须还原成 v1 的 prompt 字段
                body["prompt"] = body["content"][0]["text"]
                body.pop("content", None)
            resp, code = _call(be, _vpath(fb), body, "POST")
            st = (resp.get("base_resp") or {})
        if bool(resp.get("error")) or st.get("status_code", -1) not in (0, None):
            raise RuntimeError("提交失败: " + _errmsg(resp))
    task_id = resp.get("task_id") or ((resp.get("data") or {}).get("task_id"))
    if not task_id:
        raise RuntimeError("无 task_id: %s" % json.dumps(resp, ensure_ascii=False)[:200])
    # 任务登记落盘（project 根 gen/ 目录，人机同权可查）
    gdir = os.path.join(project, "gen")
    os.makedirs(gdir, exist_ok=True)
    rec = {"task_id": str(task_id), "model": body["model"], "api": _vpath(body["model"]), "prompt": prompt,
           "duration": body["duration"], "resolution": body["resolution"],
           "image": image or None, "t": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "submitted"}
    json.dump(rec, open(os.path.join(gdir, "task-%s.json" % task_id), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return rec


def poll_once(project, task_id):
    be = _backend()
    api_query = "/query/video_generation"  # v1 默认（老档案无 api 字段）
    tf = os.path.join(project, "gen", "task-%s.json" % task_id)
    if os.path.exists(tf):
        genp = json.load(open(tf, encoding="utf-8")).get("api")
        if genp:  # 从生成端点推导查询端点（注意：默认路径本身含 video_generation，绝不能再过 replace）
            api_query = genp.replace("/video_generation", "/query/video_generation")
    resp, _ = _call(be, api_query + "?task_id=%s" % task_id)
    st = (resp.get("base_resp") or {})
    if st.get("status_code", -1) != 0:
        return {"status": "Fail", "err": "[%s] %s" % (st.get("status_code"), st.get("status_msg"))}
    return {"status": resp.get("status") or "Unknown", "file_id": resp.get("file_id")}


def retrieve(project, task_id, file_id):
    """file_id → 下载 mp4 → 登记进 ai-generated 素材包（审计流程入口）。"""
    be = _backend()
    resp, _ = _call(be, "/files/retrieve?file_id=%s" % file_id)
    url = ((resp.get("file") or {}).get("download_url")) or ((resp.get("data") or {}).get("download_url"))
    if not url:
        raise RuntimeError("无下载地址: %s" % json.dumps(resp, ensure_ascii=False)[:200])
    gdir = os.path.join(project, "gen")
    os.makedirs(gdir, exist_ok=True)
    mp4 = os.path.join(gdir, "gen_%s.mp4" % task_id)
    with urllib.request.urlopen(url, timeout=60) as resp, open(mp4, "wb") as fo:  # 60s 超时：urlretrieve 无超时，远程生成拉取可挂死线程（Claude P2-⑤，video_gen 属数字人生成模块非渲染主链）
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fo.write(chunk)
    # 入包：与拍摄素材同权（转写/识别/usable 审计后才能被起草取用）
    pk_dir = os.path.join(ROOT, "materials", "packs", GEN_PACK)
    os.makedirs(os.path.join(pk_dir, "thumbs"), exist_ok=True)
    pp = os.path.join(pk_dir, "pack.json")
    pk = json.load(open(pp, encoding="utf-8")) if os.path.exists(pp) else {
        "id": GEN_PACK, "name": "AI 生成素材（video_gen 产物）",
        "shot_at": "", "time_rule": "0.1s 量化（与词级时间戳规范一致）",
        "separation": "拍摄层 usable/defects 归素材包；创作取用归各项目 storyline", "files": []}
    mid = "G%s" % task_id.replace("-", "")[-7:]
    fname = os.path.basename(mp4)
    dest = os.path.join(pk_dir, fname)
    os.replace(mp4, dest)
    r = subprocess.run([os.path.join(ROOT, "bin", "ffprobe"), "-v", "error",
                        "-show_entries", "format=duration:stream=width,height", "-of", "json", dest],  # probe 目的地（先移后探=永远探空）
                       capture_output=True, text=True)
    meta = json.loads(r.stdout or "{}")
    dur = round(float((meta.get("format") or {}).get("duration") or 0), 1)
    wh = next(({"width": s["width"], "height": s["height"]} for s in meta.get("streams", []) if s.get("width")), {})
    entry = {"id": mid, "file": fname, "kind": "video", "duration": dur, **wh,
             "size_mb": round(os.path.getsize(os.path.join(pk_dir, fname)) / 1e6, 2),
             "tags": ["ai-gen"], "usable": True, "review": "pending-review", "defects": [],
             "audit": {"transcript": "pending", "proofread": "pending", "visual": "pending"},
             "source_title": "AI 生成 %s" % task_id}
    # R2-2 同族：写 pack 前锁内重读+append（转写/理解可能已并发更新包），与全仓写者同锁
    import flock as fcntl
    _lf = open(os.path.join(pk_dir, ".lock"), "w")
    fcntl.flock(_lf, fcntl.LOCK_EX)
    try:
        pk2 = json.load(open(pp, encoding="utf-8")) if os.path.exists(pp) else pk
        pk2["files"].append(entry)
        json.dump(pk2, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    finally:
        fcntl.flock(_lf, fcntl.LOCK_UN); _lf.close()
    subprocess.run([FF, "-y", "-loglevel", "error", "-ss", "1", "-i", os.path.join(pk_dir, fname),
                    "-vframes", "1", "-vf", "scale=480:-2", os.path.join(pk_dir, "thumbs", "%s.jpg" % mid)],
                   capture_output=True)
    # 任务档案收尾
    tf = os.path.join(gdir, "task-%s.json" % task_id)
    if os.path.exists(tf):
        rec = json.load(open(tf, encoding="utf-8"))
        rec.update({"status": "done", "file": fname, "mid": mid, "dur": dur})
        json.dump(rec, open(tf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return entry


def main():
    ap = argparse.ArgumentParser(description="AI 视频生成器（产物=素材，入包走审计）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("project")
    g.add_argument("--prompt", required=True)
    g.add_argument("--image")
    g.add_argument("--duration", type=int)
    g.add_argument("--res")
    g.add_argument("--model")
    p = sub.add_parser("poll")
    p.add_argument("project")
    p.add_argument("--task-id")
    p.add_argument("--wait", action="store_true", help="阻塞轮询直到 Success/Fail（10s 间隔，官方建议）")
    a = ap.parse_args()
    pdir = os.path.abspath(a.project if os.path.isdir(a.project) else os.path.join(ROOT, "projects", a.project))
    if a.cmd == "gen":
        rec = submit(pdir, a.prompt, image=a.image, duration=a.duration, res=a.res, model=a.model)
        print("GEN SUBMITTED: task=%s model=%s → poll: python3 autocut3/video_gen.py poll %s --latest --wait"
              % (rec["task_id"], rec["model"], os.path.basename(pdir)))
    else:
        gdir = os.path.join(pdir, "gen")
        tid = a.task_id
        if not tid:
            tls = sorted((f for f in os.listdir(gdir) if f.startswith("task-")), key=lambda f: os.path.getmtime(os.path.join(gdir, f)))
            if not tls:
                raise SystemExit("无任务记录")
            tid = json.load(open(os.path.join(gdir, tls[-1]), encoding="utf-8"))["task_id"]
        t0 = time.time()
        while True:
            r = poll_once(pdir, tid)
            print("  [%s] status=%s" % (int(time.time() - t0), r.get("status")))
            if r.get("status") == "Success":
                e = retrieve(pdir, tid, r["file_id"])
                print("VIDEO GEN OK: %s (%ss, %sx%s) → 素材包 ai-generated/%s" %
                      (e["file"], e["duration"], e.get("width", "?"), e.get("height", "?"), e["id"]))
                break
            if r.get("status") == "Fail":
                print("VIDEO GEN FAIL:", r.get("err") or "平台返回失败")
                sys.exit(1)
            if not a.wait:
                print("  仍在生成中——稍后重跑同命令继续轮询")
                return
            if time.time() - t0 > 900:
                print("超时（15 分钟）——任务可能仍在队列，稍后重跑 poll")
                sys.exit(2)
            time.sleep(10)


if __name__ == "__main__":
    main()
