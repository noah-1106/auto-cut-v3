#!/usr/bin/env python3
"""RMW 并发竞争冒烟测试（R3-1/R3-2 的运行时自测证据，2026-09-13 立）

三轮审查的锁族 P1 全部同根：写者上了锁，读点却不在临界区——RMW（读-改-写）不原子。
本测试直接构造竞争窗口：慢写者持 flock 提交变更的同时 POST usable/upload，
若读点在锁外，慢写者变更会被陈旧快照覆盖，断言红。

用法：python3 tests/rmw_smoke.py（要求 Studio 在 8765 端口运行）
产物：自建 RMWTEST 包，结束自动清理。
"""
import fcntl, json, os, shutil, sys, threading, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID = "RMWTEST"
PKD = os.path.join(ROOT, "materials", "packs", PID)
PP = os.path.join(PKD, "pack.json")
LOCK = os.path.join(PKD, ".lock")
BASE = "http://127.0.0.1:8765"

def _post(path, body, timeout=20):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def _mkpack():
    os.makedirs(PKD, exist_ok=True)
    json.dump({"id": PID, "files": [
        {"id": "TESTA", "file": "a.mp4", "kind": "video", "duration": 3.0,
         "usable": True, "tags": ["root"], "audit": {}}]}, open(PP, "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

def _slow_writer(marker, hold_s=1.5):
    """模拟 transcribe/understand：持 flock 读-改-写 pack.json（hold 秒竞争窗口）"""
    def run():
        lf = open(LOCK, "w")
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            pk = json.load(open(PP, encoding="utf-8"))
            pk["files"].append({"id": marker, "file": marker + ".mp4", "kind": "video",
                                "duration": 5.0, "usable": True})
            time.sleep(hold_s)  # 持锁窗口：此时并发写者必须被挡在读点之外
            json.dump(pk, open(PP, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN); lf.close()
    t = threading.Thread(target=run); t.start()
    time.sleep(0.3)  # 确保持锁完成
    return t

def t_rmw_usable():
    """R3-1：usable 切换 vs 慢写者——两方变更都必须存活"""
    _mkpack()
    t = _slow_writer("M-SLOW1")
    r = _post(f"/api/pack/{PID}/TESTA/usable", {"usable": False})
    t.join()
    pk = json.load(open(PP, encoding="utf-8"))
    ids = {f["id"] for f in pk["files"]}
    usable_off = bool(r.get("ok")) and not next(f for f in pk["files"] if f["id"] == "TESTA")["usable"]
    slow_kept = "M-SLOW1" in ids
    ok = usable_off and slow_kept
    print("  usable 切换生效=%s 慢写者变更未丢=%s → %s" % (usable_off, slow_kept, "PASS" if ok else "FAIL"))
    return ok

def t_rmw_upload():
    """R3-2：upload 登记 vs 慢写者——新 entry 与慢写者变更都必须存活"""
    _mkpack()
    t = _slow_writer("M-SLOW2")
    req = urllib.request.Request(f"{BASE}/api/pack-upload/{PID}?filename=rmw.mp4",
                                 data=b"fake-bytes-rmw-test", method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    t.join()
    pk = json.load(open(PP, encoding="utf-8"))
    ids = {f["id"] for f in pk["files"]}
    up_kept = bool(r.get("ok")) and "RMW" in ids
    slow_kept = "M-SLOW2" in ids
    ok = up_kept and slow_kept
    print("  upload 登记存活=%s 慢写者变更未丢=%s → %s" % (up_kept, slow_kept, "PASS" if ok else "FAIL"))
    return ok

def t_rmw_lockmap():
    """静态守卫：studio.py + video_gen.py 的每个 pack.json【写点】往前 60 行内必须有 flock。
    （窗口 20→60：2026-09-14 词轨同步约 40 行合法代码插入锁与 dump 之间触发误报——
      启发式窗口必须大于最大合法临界区；锁的"在位性"靠窗口，原子性靠 rmw_smoke 动态测试双保险）
    （只读者不加锁是设计——半截 JSON 属可用性风险已 deferred；写者才是 RMW 原子性的责任人）"""
    bad = []
    for fname in ("studio.py", os.path.join("autocut3", "video_gen.py")):
        lines = open(os.path.join(ROOT, fname), encoding="utf-8").read().split("\n")
        for i, ln in enumerate(lines):
            is_write = (("pack.json" in ln and '"w"' in ln)      # 直接写开
                        or ("json.dump" in ln and ("pk2" in ln or "pj2" in ln))  # dump 持久句柄
                        or ("open(pp" in ln and '"w"' in ln))
            if is_write:
                win = "\n".join(lines[max(0, i - 60):i])
                if "flock" not in win and "锁豁免注记" not in win:  # 初建写（files=[] 无并发对象）显式豁免
                    bad.append("%s:%d" % (fname, i + 1))
    ok = not bad
    print("  写点锁覆盖扫描：异常=%s → %s" % (bad or "无", "PASS" if ok else "FAIL"))
    return ok

if __name__ == "__main__":
    print("══ RMW 并发竞争冒烟（R3-1/R3-2 运行时自测）══")
    try:
        r1 = t_rmw_usable()
        r2 = t_rmw_upload()
    finally:
        shutil.rmtree(PKD, ignore_errors=True)  # 清理 scratch 包
    r3 = t_rmw_lockmap()
    print("══ 结果：%s ══" % ("ALL PASS" if (r1 and r2 and r3) else "FAIL"))
    sys.exit(0 if (r1 and r2 and r3) else 1)
