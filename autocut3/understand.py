"""理解生成器——素材进厂管线的第二站：让素材"被看懂"。

人机等价（军规1）：
  Agent : python3 autocut3/understand.py <project> [--material M0128] [--vision minimax] [--force]
  人    : Studio 素材卡「⟳ 画面识别」按钮

按素材类型分派（卡片上有类型标签）：
  视频 → 采样帧 + 视觉模型（desc/ocr/usage） + ASR（transcribe.py 负责）
  图片 → 视觉模型（desc/ocr/usage）
  音频 → ASR（transcribe.py 负责），无视觉轨

产物（归一化契约见 vision.py 文件头）：
  pack.json files[i].visual     = {desc, ocr, usage, frames, provider, at}
  files[i].audit.visual         = "done"
供应商由 config/services.json vision 段登记，--vision 可临时覆盖。
"""
import argparse, concurrent.futures as cf, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # noqa: E402  digest 时间戳用（datetime_iso）+ retry429 退避
import vision  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 素材级并发数：与 transcribe.py 同值同依据（2026-09-18 实测 3xVLM 并发 26-37s 无 429 无减速）
WORKERS = 3


def project_packs(pid):
    pdir = os.path.join(ROOT, "projects", pid)
    pj = {}
    pp = os.path.join(pdir, "project.json")
    if os.path.exists(pp):
        pj = json.load(open(pp, encoding="utf-8"))
    packs = (json.load(open(os.path.join(pdir, "materials", "library.json"), encoding="utf-8")).get("packs", [])
            if os.path.exists(os.path.join(pdir, "materials", "library.json")) else []) or pj.get("material_packs") or []
    return packs


def _audits_done(f, aud):
    """与 transcribe.py 同规则：该 kind 所需审计全部 done/n/a = 已过审。"""
    need = {"video": ("transcript", "visual"), "audio": ("transcript",), "image": ("visual",)}.get(
        f.get("kind", "video"), ())
    return all(aud.get(k) in ("done", "n/a") for k in need)


def _auto_review(f):
    if f.get("review") == "pending-review" and _audits_done(f, f.get("audit") or {}):
        f["review"] = "reviewed"  # 审计齐=自动过审（维护者 2026-09-15：审核的实质内容 Agent 自己能做）


def _one(f, src, kind, provider):
    """单条素材的网络调用（线程池内执行）。只发请求不碰 pack.json——结果由主线程
    单写者合并；429 走 asr.retry429 线性退避。"""
    try:
        r = asr.retry429(lambda: vision.understand(src, kind, provider=provider,
                                                   words=(f.get("transcript") or {}).get("words"),
                                                   dup=int((f.get("transcript") or {}).get("scripted_dup") or 0)))  # 念稿指纹随词轨入视觉（M0269 错判修复）
        return {"ok": True, "r": r}
    except Exception as e:
        return {"ok": False, "err": str(e)[:120]}


def understand_pack(pack_id, material=None, provider=None, force=False):
    pdir = os.path.join(ROOT, "materials", "packs", pack_id)
    pp = os.path.join(pdir, "pack.json")
    import flock as fcntl
    lockf = open(os.path.join(pdir, ".lock"), "w")  # 与 transcribe 同锁：读-改-写全程独占（并发在锁内，写者仍单一）
    fcntl.flock(lockf, fcntl.LOCK_EX)
    pk = json.load(open(pp, encoding="utf-8"))
    out = {}
    changed = False
    todo = []
    for f in pk.get("files", []):
        aud = f.setdefault("audit", {})
        if aud.get("visual") == "done" and not force:
            continue
        if material and f.get("id") != material:
            continue
        kind = f.get("kind", "video")
        if kind not in ("image", "video"):
            aud["visual"] = "n/a"  # 音频无视觉轨——如实标注，不留永久 pending
            _auto_review(f)
            changed = True  # P1#2：audit 变更也要落盘（原只在理解成功分支置位，n/a 改动被吞）
            out[f["id"]] = {"ok": False, "err": "音频无视觉轨（理解走 ASR）"}
            print("  ✗ %s 音频无视觉轨（理解走 ASR）" % f["id"], flush=True)
            continue
        src = os.path.join(pdir, f["file"])
        if not os.path.exists(src):
            out[f["id"]] = {"ok": False, "err": "文件缺失"}
            print("  ✗ %s 文件缺失" % f["id"], flush=True)
            continue
        todo.append((f, src, kind))
    if todo:
        print("  · 并发理解 %d 条（%d 线程）…" % (len(todo), WORKERS), flush=True)
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_one, f, src, kind, provider): f for f, src, kind in todo}
        for fu in cf.as_completed(futs):
            f = futs[fu]
            aud = f.setdefault("audit", {})
            mid = f["id"]
            res = fu.result()
            if not res["ok"]:
                out[mid] = {"ok": False, "err": res["err"]}
                print("  ✗ %s %s" % (mid, res["err"]), flush=True)
                continue
            r = res["r"]
            f["visual"] = r
            aud["visual"] = "done"
            _auto_review(f)
            changed = True
            out[mid] = {"ok": True, "desc": (r.get("desc") or "")[:44], "ocr_n": len(r.get("ocr") or []),
                        "content_type": r.get("content_type") or "", "schema": r.get("schema", 1)}
            # 完成即报（flush）：后台跑日志实时可见——结果行从 main() 移到此处（2026-09-18 nohup 盲区整改）
            print("  ✓ %s | %s | OCR %d 条 | %s" % (mid, r.get("content_type") or "—",
                  len(r.get("ocr") or []), (r.get("desc") or "")[:44]), flush=True)
    if changed:
        with open(pp, "w", encoding="utf-8") as fh:
            json.dump(pk, fh, ensure_ascii=False, indent=1)
    fcntl.flock(lockf, fcntl.LOCK_UN)
    return out


def main():
    ap = argparse.ArgumentParser(description="素材理解生成器（视觉/OCR/用途，供应商见 config/services.json）")
    ap.add_argument("project")
    ap.add_argument("--pack", help="只处理指定素材包")
    ap.add_argument("--material", help="只处理指定素材 id")
    ap.add_argument("--vision", help="临时覆盖视觉供应商")
    ap.add_argument("--force", action="store_true", help="已识别的也重跑")
    a = ap.parse_args()

    packs = [a.pack] if a.pack else project_packs(a.project)
    if not packs:
        print("UNDERSTAND: 项目未引用任何素材包"); sys.exit(1)
    total = {"done": 0, "skip": 0, "fail": 0}
    for pid in packs:
        res = understand_pack(pid, material=a.material, provider=a.vision, force=a.force)
        for mid, r in res.items():
            if r.get("ok"):
                total["done"] += 1
            else:
                total["fail"] += 1
        if not res:
            total["skip"] += len(json.load(open(os.path.join(ROOT, "materials", "packs", pid, "pack.json"), encoding="utf-8")).get("files", []))
    print("UNDERSTAND DONE: +%d (跳过 %d, 失败 %d)" % (total["done"], total["skip"], total["fail"]), flush=True)
    # 编排闭环（维护者 2026-09-14）：单素材识别全部完成后 → 包级统一理解 digest。
    # 没有这层，起草 LLM 面对的是扁平列表，悟不出"M0269 是旁白音轨源"这类整体定位——
    # 这是编排缺口，不是识别提示词的错。单素材模式（--material）不触发（盘点需要全量视图）。
    if not a.material and total["done"] > 0:
        for pid in packs:
            try:
                print("  📋 DIGEST %s LLM 盘点中（1-3 分钟静默属正常）…" % pid, flush=True)
                d = build_digest(pid)
                print("  📋 DIGEST %s：%s（A轨候选%d/B-roll池%d/配音源%d）"
                      % (pid, d.get("theme", "")[:40],
                         len(d.get("inventory", {}).get("a_roll_candidates", [])),
                         len(d.get("inventory", {}).get("broll_pool", [])),
                         len(d.get("inventory", {}).get("voiceover_sources", []))), flush=True)
            except Exception as e:
                print("  ⚠ DIGEST %s 失败（不影响素材识别结果）：%s" % (pid, str(e)[:100]), flush=True)


def build_digest(pid):
    """包级统一理解：汇总全部单素材识别结果 → LLM 一次调用产出导演视角盘点。
    产物 pack.json.digest = {theme, inventory{a_roll_candidates,broll_pool,voiceover_sources,ambient,gaps},
    roles[{id,suggest}], narrative_assets}。draft.build_dossier 读它做档案头。"""
    import flock as fcntl
    pp = os.path.join(ROOT, "materials", "packs", pid, "pack.json")
    pk = json.load(open(pp, encoding="utf-8"))
    lines = []
    for f in pk.get("files", []):
        if not f.get("usable", True):
            lines.append("- %s：拍摄废片（禁用）" % f["id"]); continue
        tr = f.get("transcript") or {}
        v = f.get("visual") or {}
        parts = ["- %s（%ss，%s）" % (f["id"], f.get("duration", "?"), (v.get("content_type") or "未识别"))]
        if tr.get("text"):
            parts.append("台词：%s" % tr["text"][:60])
        if tr.get("scripted_dup"):
            parts.append("【台词≥%d字逐字重复=念稿/重录指纹】" % tr["scripted_dup"])
        if v.get("desc"):
            parts.append("画面：%s" % v["desc"][:60])
        if v.get("usage"):
            parts.append("用途：%s" % str(v["usage"])[:50])
        lines.append(" ".join(parts))
    if not lines:
        raise RuntimeError("素材包为空（无可用素材），无可盘点内容")  # 空包护栏：不调 LLM（曾把模型的"错误说明"当成 digest 落库）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import draft as D
    prompt = (
        "以下是同一个素材包内全部素材的识别结果。请做导演视角的统一盘点，只输出合法 JSON：\n"
        '{"theme":"一句话：这包素材整体拍的是什么（题材/场景/叙事价值）",\n'
        '"inventory":{"a_roll_candidates":["适合作出镜口播主轴的素材id——自然讲话、内容成段"],'
        '"broll_pool":["适合垫画面的素材id"],'
        '"voiceover_sources":["念稿/配音录制素材id——词轨是主资产，画面仅辅助"],'
        '"ambient":["空镜/环境素材id"],'
        '"gaps":"这包素材缺什么（如缺全景/缺成果镜头/缺收尾）——没有就空字符串"},\n'
        '"roles":[{"id":"素材id","suggest":"一句话：这条在整体中的最佳定位与用法"}],\n'
        '"narrative_assets":"这包素材能支撑的叙事主题，一句话"}\n'
        "铁律：分类要尊重识别层给出的 content_type 和念稿指纹，不要重新臆测；"
        "素材id 必须来自清单，禁止编造。\n\n素材清单：\n" + "\n".join(lines))
    d = D.extract_json(D.chat_llm([{"role": "user", "content": prompt}]))
    if not isinstance(d, dict):
        raise RuntimeError("digest LLM 未返回合法 JSON")
    d["at"] = asr.datetime_iso()
    with open(pp + ".lock", "w") as _lf:
        import flock as _f
        _f.flock(_lf, _f.LOCK_EX)
        pk2 = json.load(open(pp, encoding="utf-8"))
        pk2["digest"] = d
        json.dump(pk2, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return d


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
