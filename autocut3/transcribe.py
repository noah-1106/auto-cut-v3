"""转写生成器——素材进厂管线的第一站：让素材"开口"。

人机等价（军规1）：
  Agent : python3 autocut3/transcribe.py <project> [--material M0128] [--asr minimax] [--force]
  人    : Studio 素材库卡片上的「转写」按钮 / 「全部转写」批处理按钮

产物（统一归一化契约，见 asr.py 文件头）：
  pack.json files[i].transcript = {provider, tier, text, words, duration, has_speech, at}
  files[i].audit.transcript     = "done"
供应商由 config/services.json 登记（换供应商不改代码），--asr 可临时覆盖做 A/B 对比。
"""
import argparse, json, os, re, sys  # re：_dup_len 念稿指纹检测（漏 import=NameError 雷，Claude 审查同族）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def project_packs(pid):
    """项目引用的素材包列表：project.json material_packs → library.json packs 兜底。"""
    pdir = os.path.join(ROOT, "projects", pid)
    pj = {}
    pp = os.path.join(pdir, "project.json")
    if os.path.exists(pp):
        pj = json.load(open(pp, encoding="utf-8"))
    packs = (json.load(open(os.path.join(pdir, "materials", "library.json"), encoding="utf-8")).get("packs", [])
            if os.path.exists(os.path.join(pdir, "materials", "library.json")) else []) or pj.get("material_packs") or []
    return packs



def _locked(pdir):
    """包级互斥锁：同一 pack 的读-改-写全程串行（防并发生成器互相覆盖）。
    2026-09-11 实锤：transcribe/understand 并发跑同一包，后写方整体覆盖先写方的数据。"""
    import flock as fcntl
    lf = open(os.path.join(pdir, ".lock"), "w")
    fcntl.flock(lf, fcntl.LOCK_EX)
    return lf


def _dup_len(text, min_len=12):
    """最长逐字重复子串长度（标点清洗后）。≥12 字逐字重复 = 念稿/重录指纹（自然讲话不会逐字复述）。
    O(n²) 对转写文本量级（<500 字）足够；返回最长重复字数，0=无重复。"""
    t = re.sub(r"[，。！？、,.!?\s]", "", text or "")
    best = 0
    n = len(t)
    for i in range(n - min_len + 1):
        # 从最长可能往下探，命中即记
        for L in range(n - i, best, -1):
            if L >= min_len and t.count(t[i:i + L]) >= 2:
                best = max(best, L)
                break
    return best


def transcribe_pack(pack_id, material=None, provider=None, force=False):
    """转写一个素材包内待处理的条目，返回 {id: 摘要}。"""
    pdir = os.path.join(ROOT, "materials", "packs", pack_id)
    pp = os.path.join(pdir, "pack.json")
    lockf = _locked(pdir)  # 全程独占：处理完才放锁
    pk = json.load(open(pp, encoding="utf-8"))
    out = {}
    changed = False
    for f in pk.get("files", []):
        aud = f.setdefault("audit", {})
        print("  · 转写中 %s (%dMB)…" % (f.get("id"), int((f.get("size_mb") or 0))), flush=True) if not (aud.get("transcript") == "done" and not force) else None
        if aud.get("transcript") == "done" and not force:
            continue
        if material and f.get("id") != material:
            continue
        if f.get("kind") == "image":
            continue  # 图片无音轨——转写只服务视频/音频
        src = os.path.join(pdir, f["file"])
        if not os.path.exists(src):
            out[f["id"]] = {"ok": False, "err": "文件缺失"}
            continue
        try:
            r = asr.transcribe(src, provider=provider)
        except Exception as e:
            aud["transcript"] = "error:" + str(e)[:80]  # 失败标记（Claude P3-①）：跳过≠无声吞掉
            out[f["id"]] = {"ok": False, "err": str(e)[:120]}
            continue
        has_speech = bool(r["words"])
        f["transcript"] = {"provider": r["provider"], "tier": r["tier"], "text": r["text"],
                           "words": r["words"], "duration": r["duration"],
                           "has_speech": has_speech, "at": r["at"]}
        _dup = _dup_len(r["text"])
        if _dup >= 12:
            f["transcript"]["scripted_dup"] = _dup  # 念稿/重录指纹（Noah 2026-09-14：M0269 错判根因之一——台词逐字重复两遍无人消费）
        aud["transcript"] = "done"
        changed = True
        out[f["id"]] = {"ok": True, "tier": r["tier"], "words": len(r["words"]),
                        "text": (r["text"] or "")[:40], "has_speech": has_speech}
    if changed:
        with open(pp, "w", encoding="utf-8") as fh:
            json.dump(pk, fh, ensure_ascii=False, indent=1)
    import flock as fcntl
    fcntl.flock(lockf, fcntl.LOCK_UN)
    return out


def main():
    ap = argparse.ArgumentParser(description="素材转写生成器（供应商见 config/services.json）")
    ap.add_argument("project")
    ap.add_argument("--pack", help="只处理指定素材包（默认项目引用的全部）")
    ap.add_argument("--material", help="只处理指定素材 id")
    ap.add_argument("--asr", help="临时覆盖 ASR 供应商（A/B 对比用，不写配置）")
    ap.add_argument("--force", action="store_true", help="已转写的也重跑")
    a = ap.parse_args()

    packs = [a.pack] if a.pack else project_packs(a.project)
    if not packs:
        print("TRANSCRIBE: 项目未引用任何素材包"); sys.exit(1)
    total = {"done": 0, "skip": 0, "fail": 0}
    for pid in packs:
        res = transcribe_pack(pid, material=a.material, provider=a.asr, force=a.force)
        for mid, r in res.items():
            if r.get("ok"):
                total["done"] += 1
                speech = "有声" if r.get("has_speech") else "无语音（纯画面）"
                print("  ✓ %s [%s/%s] %s | %s" % (mid, r["tier"], speech, r["words"], r["text"]))
            else:
                total["fail"] += 1
                print("  ✗ %s %s" % (mid, r.get("err", "")))
        if not res:
            total["skip"] += len(json.load(open(os.path.join(ROOT, "materials", "packs", pid, "pack.json"), encoding="utf-8")).get("files", []))
    print("TRANSCRIBE DONE: +%d (跳过已转写 %d, 失败 %d)" % (total["done"], total["skip"], total["fail"]))


if __name__ == "__main__":
    main()
