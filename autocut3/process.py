"""素材级流水线处理器——transcribe∥understand 的正确并行形态（2026-09-19 落地）。

为什么不是"advance 并发调两个模块"：两步共持 packs/<pid>/.lock 整程独占（2026-09-11
覆盖事故的防锁），盲并发=understand 阻塞等锁零提速；且 understand 每条素材要吃
transcript.words+念稿指纹（M0269 修复），先理解后转写=视觉理解丢上下文。

本模块的形态：**单进程双池流水线**——ASR 池 3 线程 + 视觉池 3 线程（双端点 3+3，
2026-09-18 MiniMax 实测无 429），每条素材 ASR 一完成立即链发该素材的视觉理解
（词轨随行），素材间全并发。墙钟 ≈ max(ASR 总量, 视觉总量) 而非两者之和。

人机等价（军规1）：
  Agent : python3 autocut3/process.py <project> [--pack P] [--material M] [--force]
  人    : python3 autocut3/orchestrate.py <project> --advance（transcribe 步内部走本模块）

不变式（与 transcribe.py/understand.py 逐条对齐，改哪边都要对账）：
  · packs/<pid>/.lock 整程独占，pack.json 末尾主线程一次写
  · files[i].transcript/audit.transcript 语义 = transcribe_pack（失败标记 error:、念稿指纹、自动过审）
  · files[i].visual/audit.visual 语义 = understand_pack（音频 n/a、失败留 pending 供重试）
  · digest 不在此触发——orchestrate digest 步 / understand.py 批处理兜底
失败恢复：视觉失败留 pending → advance understand 步或 understand.py 重试只补缺。
"""
import argparse, concurrent.futures as cf, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr   # noqa: E402  retry429 退避
import vision  # noqa: E402
from transcribe import _auto_review, _dup_len, _locked, project_packs  # 单一事实源：审核/指纹/锁/包表同实现

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKERS = 3  # 每池线程数（双端点各 3，同 transcribe.py/understand.py 依据）


def process_pack(pack_id, material=None, asr_provider=None, vision_provider=None, force=False):
    """流水线处理一个包：返回 {"asr": {id: 摘要}, "visual": {id: 摘要}}。"""
    pdir = os.path.join(ROOT, "materials", "packs", pack_id)
    pp = os.path.join(pdir, "pack.json")
    lockf = _locked(pdir)  # 同一把包锁，整程独占（读-改-写全程串行）
    pk = json.load(open(pp, encoding="utf-8"))
    out = {"asr": {}, "visual": {}}
    st = {"changed": False}
    futs = []  # 双池全部在飞 future（链发的视觉任务由 ASR 线程 append——list.append 线程安全）

    def visual_job(f, src, kind):
        """视觉理解（视觉池线程）：merge 进本素材对象——每素材只此一线程写 f，链式
        happens-before（ASR 完成后才提交），跨素材对象不相交。"""
        try:
            r = asr.retry429(lambda: vision.understand(
                src, kind, provider=vision_provider,
                words=(f.get("transcript") or {}).get("words"),
                dup=int((f.get("transcript") or {}).get("scripted_dup") or 0)))  # 念稿指纹随词轨入视觉（M0269）
            f["visual"] = r
            f.setdefault("audit", {})["visual"] = "done"
            _auto_review(f)
            st["changed"] = True
            out["visual"][f["id"]] = {"ok": True, "desc": (r.get("desc") or "")[:44],
                                      "ocr_n": len(r.get("ocr") or []),
                                      "content_type": r.get("content_type") or ""}
            print("  ✓ %s 视觉 | %s | OCR %d 条 | %s" % (f["id"], r.get("content_type") or "—",
                  len(r.get("ocr") or []), (r.get("desc") or "")[:44]), flush=True)
        except Exception as e:
            out["visual"][f["id"]] = {"ok": False, "err": str(e)[:120]}
            print("  ✗ %s 视觉 %s" % (f["id"], str(e)[:120]), flush=True)  # audit 留 pending 供重试

    def asr_job(f, src, kind, need_visual):
        """转写（ASR 池线程）：merge 后链发本素材视觉（词轨已就位）。"""
        try:
            r = asr.retry429(lambda: asr.transcribe(src, provider=asr_provider))
            has_speech = bool(r["words"])
            f["transcript"] = {"provider": r["provider"], "tier": r["tier"], "text": r["text"],
                               "words": r["words"], "duration": r["duration"],
                               "has_speech": has_speech, "at": r["at"]}
            _dup = _dup_len(r["text"])
            if _dup >= 12:
                f["transcript"]["scripted_dup"] = _dup  # 念稿/重录指纹
            f.setdefault("audit", {})["transcript"] = "done"
            _auto_review(f)
            st["changed"] = True
            out["asr"][f["id"]] = {"ok": True, "tier": r["tier"], "words": len(r["words"]),
                                   "text": (r["text"] or "")[:40], "has_speech": has_speech}
            print("  ✓ %s [%s/%s] %s | %s" % (f["id"], r["tier"],
                  "有声" if has_speech else "无语音", len(r["words"]), (r["text"] or "")[:40]), flush=True)
        except Exception as e:
            f.setdefault("audit", {})["transcript"] = "error:" + str(e)[:80]  # 失败标记（跳过≠无声吞掉）
            st["changed"] = True
            out["asr"][f["id"]] = {"ok": False, "err": str(e)[:120]}
            print("  ✗ %s 转写 %s" % (f["id"], str(e)[:120]), flush=True)
        if need_visual:  # ASR 失败也发视觉（words=None）——与顺序跑 understand 的行为一致
            futs.append(vi_ex.submit(visual_job, f, src, kind))

    asr_ex = cf.ThreadPoolExecutor(max_workers=WORKERS)
    vi_ex = cf.ThreadPoolExecutor(max_workers=WORKERS)
    try:
        n_asr = n_vi = 0
        for f in pk.get("files", []):
            aud = f.setdefault("audit", {})
            if material and f.get("id") != material:
                continue
            kind = f.get("kind", "video")
            need_asr = kind != "image" and (force or aud.get("transcript") != "done")
            need_vi = kind in ("video", "image") and (force or aud.get("visual") != "done")
            if kind not in ("video", "image") and aud.get("visual") != "n/a":
                aud["visual"] = "n/a"  # 音频无视觉轨——如实标注，不留永久 pending
                _auto_review(f)
                st["changed"] = True
                out["visual"][f["id"]] = {"ok": False, "err": "音频无视觉轨（理解走 ASR）"}
                print("  ✗ %s 音频无视觉轨（理解走 ASR）" % f["id"], flush=True)
            if not (need_asr or need_vi):
                continue
            src = os.path.join(pdir, f["file"])
            if not os.path.exists(src):
                out["asr" if need_asr else "visual"][f["id"]] = {"ok": False, "err": "文件缺失"}
                print("  ✗ %s 文件缺失" % f["id"], flush=True)
                continue
            if need_asr:
                n_asr += 1
                futs.append(asr_ex.submit(asr_job, f, src, kind, need_vi))
            elif need_vi:  # 图片 / 已转写的视频：无 ASR 依赖，直接进视觉池
                n_vi += 1
                futs.append(vi_ex.submit(visual_job, f, src, kind))
        if n_asr or n_vi:
            print("  · 流水线 %d 条转写 + %d 条视觉（双端点 %d+%d 并发）…" % (n_asr, n_vi, WORKERS, WORKERS), flush=True)
        for fu in cf.as_completed(futs):
            fu.result()  # job 内部已自吞异常，这里只等全部落地（防御性：线程池回调异常不静默）
        if st["changed"]:
            with open(pp, "w", encoding="utf-8") as fh:
                json.dump(pk, fh, ensure_ascii=False, indent=1)
    finally:
        asr_ex.shutdown(wait=True)
        vi_ex.shutdown(wait=True)
        import flock as fcntl
        fcntl.flock(lockf, fcntl.LOCK_UN)
    return out


def main():
    ap = argparse.ArgumentParser(description="素材级流水线：每条素材 ASR 完立即链发画面识别（双端点 3+3 并发）")
    ap.add_argument("project")
    ap.add_argument("--pack", help="只处理指定素材包（默认项目引用的全部）")
    ap.add_argument("--material", help="只处理指定素材 id")
    ap.add_argument("--asr", help="临时覆盖 ASR 供应商")
    ap.add_argument("--vision", help="临时覆盖视觉供应商")
    ap.add_argument("--force", action="store_true", help="已完成的也重跑（两段同 force）")
    a = ap.parse_args()

    packs = [a.pack] if a.pack else project_packs(a.project)
    if not packs:
        print("PROCESS: 项目未引用任何素材包"); sys.exit(1)
    total = {"asr": 0, "vi": 0, "skip": 0, "fail": 0}
    for pid in packs:
        res = process_pack(pid, material=a.material, asr_provider=a.asr, vision_provider=a.vision, force=a.force)
        for stg in ("asr", "visual"):
            for r in res[stg].values():
                if r.get("ok"):
                    total["asr" if stg == "asr" else "vi"] += 1
                else:
                    total["fail"] += 1
        if not res["asr"] and not res["visual"]:
            total["skip"] += len(json.load(open(os.path.join(ROOT, "materials", "packs", pid, "pack.json"), encoding="utf-8")).get("files", []))
    print("PROCESS DONE: 转写 +%d / 视觉 +%d（跳过 %d, 失败 %d）" % (total["asr"], total["vi"], total["skip"], total["fail"]), flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台兜底（同 transcribe.py）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
