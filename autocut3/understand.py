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
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vision  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def project_packs(pid):
    pdir = os.path.join(ROOT, "projects", pid)
    pj = {}
    pp = os.path.join(pdir, "project.json")
    if os.path.exists(pp):
        pj = json.load(open(pp, encoding="utf-8"))
    packs = (json.load(open(os.path.join(pdir, "materials", "library.json"), encoding="utf-8")).get("packs", [])
            if os.path.exists(os.path.join(pdir, "materials", "library.json")) else []) or pj.get("material_packs") or []
    return packs


def understand_pack(pack_id, material=None, provider=None, force=False):
    pdir = os.path.join(ROOT, "materials", "packs", pack_id)
    pp = os.path.join(pdir, "pack.json")
    import fcntl
    lockf = open(os.path.join(pdir, ".lock"), "w")  # 与 transcribe 同锁：读-改-写全程独占
    fcntl.flock(lockf, fcntl.LOCK_EX)
    pk = json.load(open(pp, encoding="utf-8"))
    out = {}
    changed = False
    for f in pk.get("files", []):
        aud = f.setdefault("audit", {})
        if aud.get("visual") == "done" and not force:
            continue
        if material and f.get("id") != material:
            continue
        kind = f.get("kind", "video")
        if kind not in ("image", "video"):
            aud["visual"] = "n/a"  # 音频无视觉轨——如实标注，不留永久 pending
            changed = True  # P1#2：audit 变更也要落盘（原只在理解成功分支置位，n/a 改动被吞）
            out[f["id"]] = {"ok": False, "err": "音频无视觉轨（理解走 ASR）"}
            continue
        src = os.path.join(pdir, f["file"])
        if not os.path.exists(src):
            out[f["id"]] = {"ok": False, "err": "文件缺失"}
            continue
        try:
            r = vision.understand(src, kind, provider=provider,
                                  words=(f.get("transcript") or {}).get("words"))
        except Exception as e:
            out[f["id"]] = {"ok": False, "err": str(e)[:120]}
            continue
        f["visual"] = r
        aud["visual"] = "done"
        changed = True
        out[f["id"]] = {"ok": True, "desc": (r.get("desc") or "")[:44], "ocr_n": len(r.get("ocr") or []),
                       "content_type": r.get("content_type") or "", "schema": r.get("schema", 1)}
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
                print("  ✓ %s | %s | OCR %d 条 | %s" % (mid, r.get("content_type") or "—", r["ocr_n"], r["desc"]))
            else:
                total["fail"] += 1
                print("  ✗ %s %s" % (mid, r.get("err", "")))
        if not res:
            total["skip"] += len(json.load(open(os.path.join(ROOT, "materials", "packs", pid, "pack.json"), encoding="utf-8")).get("files", []))
    print("UNDERSTAND DONE: +%d (跳过 %d, 失败 %d)" % (total["done"], total["skip"], total["fail"]))


if __name__ == "__main__":
    main()
