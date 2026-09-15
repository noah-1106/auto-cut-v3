#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASR 校对层（audit.proofread 的实现）——同音字纠错的唯一合法动作是【等长替换】。

为什么等长：words 是字/词级时间轴，字幕卡拉OK按字符均分词时长。等长替换 → 时间戳、
字符对齐、词轨消费方（字幕/校对/EDL）全部零破坏。非等长修正（增删字）一律拒绝并记录。
错误主要来源：同音字（轮骨→龙骨）、专名（潭溪工馆→檀溪公馆）。
v2（2026-09-12）：①词表注入——PROMPT 读 config/lexicon.json（行业术语+专名，参照系外置）；
②confidence 分级——replace 命中词表术语(len>=2)=auto 直接改；否则=needs-human 进复核队列
（projects/<pid>/review-queue.json），不落词轨（"放可以了→方可以了"这类二次猜错有人把关）；
③find/replace 超过 6 字一律 needs-human（词级修正才可机判）。
用法: python3 proofread.py <pid> [--dry]   ；复核：编辑 review-queue.json 的 status 字段"""
import argparse, json, os, sys, urllib.request
try:
    import flock as fcntl  # 跨平台锁：POSIX=flock，Windows=msvcrt（autocut3/flock.py）
except ImportError:
    import fcntl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from draft import chat_llm  # 复用起草的 LLM 客户端（key/端点一套配置）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROMPT_T = """你是装修行业口播视频的字幕校对员。下面是一段 ASR 转写的连续文本。
找出其中的错别字，重点：同音/近音错字。行业词表（正确写法以此为准，命中词表的修正优先）：
{terms}
铁律：只做【等长替换】——find 和 replace 字数必须相同；不改标点；不增删任何字；
find/replace 都不超过 6 个字（句级改写不做，标 needs-human 由人处理）。
输出 JSON 数组，每项 {{"find": "...", "replace": "..."}}；没有错误输出 []。只输出 JSON，不要解释。"""


def _load_terms():
    p = os.path.join(ROOT, "config", "lexicon.json")
    try:
        return json.load(open(p, encoding="utf-8")).get("terms", [])
    except Exception:
        return []


def _queue_load(pid):
    p = os.path.join(ROOT, "projects", pid, "review-queue.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"project": pid, "items": []}


def _queue_add(pid, item):
    q = _queue_load(pid)
    q["items"].append(item)
    d = os.path.join(ROOT, "projects", pid)
    if not os.path.isdir(d):
        os.makedirs(d)
    p = os.path.join(d, "review-queue.json")
    json.dump(q, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _confidence(replace, terms):
    """确定性分级：replace 命中词表术语（子串, len>=2）→ auto；否则 needs-human。
    依据：词表修正可机判（轮骨→龙骨/潭溪工馆→檀溪公馆）；非词表修正语义存疑（放可以了→方可以了案例）。"""
    if len(replace) < 2:
        return "needs-human"  # 首轮 P2#9：单字替换上下文弱（股→骨 类），必须人审——词表子串命中也不豁免
    for t in terms:
        if len(t) >= 2 and (t in replace or replace in t):
            return "auto"
    return "needs-human"


def apply_eqsub(words, find, replace):
    """在词序列上做等长替换（保持每词字数与时间戳不动）。返回替换的词下标列表。"""
    joined = "".join(w["text"] for w in words)
    if len(find) != len(replace):
        return None
    hit = []
    p = joined.find(find)
    while p >= 0:
        # 定位覆盖 [p, p+len) 的词段，逐字符替换
        acc = 0
        for w in words:
            L = len(w["text"])
            s, e = acc, acc + L
            if e > p and s < p + len(find):  # 与目标区间相交
                cs = list(w["text"])
                for k in range(L):
                    gp = s + k  # 该字符在 joined 中的全局位置
                    if p <= gp < p + len(find) and cs[k] != replace[gp - p]:
                        cs[k] = replace[gp - p]
                        if w not in hit:
                            hit.append(w)
                w["text"] = "".join(cs)
            acc = e
        if joined.find(find, p + 1) == -1:
            break
        p = joined.find(find, p + 1)
        joined = "".join(w["text"] for w in words)
    return hit


def _pack_locked(fn):
    """pack.json 写互斥：与 understand/transcribe 同锁文件（.lock），防 Studio 并发写互踩。"""
    def wrapper(pid, *a, **kw):
        lock_path = os.path.join(ROOT, "materials", "packs", pid, ".lock")
        lockf = open(lock_path, "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            return fn(pid, *a, **kw)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()
    return wrapper


@_pack_locked
def run(pid, dry=False, material=None):
    pk_path = os.path.join(ROOT, "materials", "packs", pid, "pack.json")
    pk = json.load(open(pk_path, encoding="utf-8"))
    terms = _load_terms()
    total_fix, total_review = 0, 0
    pending_queue = []
    for f in pk.get("files", []):
        if material and f.get("id") != material:
            continue
        words = (f.get("transcript") or {}).get("words") or []
        if not words:
            continue
        text = "".join(w["text"] for w in words)
        try:
            raw = chat_llm([{"role": "system", "content": PROMPT_T.format(terms="、".join(terms))},
                            {"role": "user", "content": text}])
            fixes = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
        except Exception as e:
            print(f"  {f['id']}: LLM 校对失败 {str(e)[:80]}")
            continue
        applied, queued = [], []
        for c in fixes:
            if not isinstance(c, dict) or not c.get("find") or not c.get("replace"):
                continue
            if len(c["find"]) != len(c["replace"]):
                print(f"  {f['id']}: 拒绝非等长替换 {c['find']}→{c['replace']}（会破坏词轨对齐）")
                continue
            if len(c["find"]) > 6:
                queued.append(c)
                print(f"  {f['id']}: 超长修正转人工复核 {c['find']}→{c['replace']}")
                continue
            conf = _confidence(c["replace"], terms)
            if conf == "needs-human":
                queued.append(c)
                print(f"  {f['id']}: 非词表修正转人工复核 {c['find']}→{c['replace']}")
                continue
            hit = apply_eqsub(words, c["find"], c["replace"])
            if hit:
                applied.append({"find": c["find"], "replace": c["replace"], "n": len(hit)})
                total_fix += len(hit)
        f.setdefault("audit", {})["proofread"] = "auto-done"  # P2#13：外部 pack 可能无 audit 键
        if applied:
            f.setdefault("proofread_log", [])
            f["proofread_log"] += applied
        for c in queued:
            total_review += 1
            item = {"material": f["id"], "find": c["find"], "replace": c["replace"],
                    "context": text[:60], "status": "pending",
                    "reason": "非词表/超长修正——语义存疑，人审后定",
                    "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
            if dry:
                print(f"  {f['id']}: [dry] 拟入复核队列 {c['find']}→{c['replace']}")
            else:
                pending_queue.append(item)
        print(f"  {f['id']}: auto {len(applied)} 组 {[a['find']+'→'+a['replace'] for a in applied] or '无'} | 转人工 {len(queued)} 组")
    if not dry:
        for item in pending_queue:
            _queue_add(pid, item)
        json.dump(pk, open(pk_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"PROOFREAD DONE: auto {total_fix} 处已写回（等长，时间戳未动）；needs-human {total_review} 组进 review-queue.json")
    else:
        print(f"PROOFREAD DRY: auto {total_fix} 处 / 转人工 {total_review} 组（未写回）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pid")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--material", help="只校对指定素材（试点用）")
    a = ap.parse_args()
    run(a.pid, a.dry, a.material)
