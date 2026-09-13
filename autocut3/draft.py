"""故事线起草生成器——主线第二步：素材理解档案 → AI 草稿故事线。

人机等价（军规1）：
  Agent : python3 autocut3/draft.py <project> --intent "用开场钩子和空镜起一条3幕口播线" [--save]
  人    : Studio 创作台「✨ AI 起草」按钮（写一句意图 → 点一下）

纪律（AI 只起草不落定）：
  · 只从 usable=true 的素材里选段；【拍摄废片】在档案中标注为禁用
  · 台词优先取人工校对后的文本（proofread 落盘即覆盖 ASR 原文，天然生效）
  · 草稿写成新故事线（aidraft / aidraft2 / …），绝不覆盖既有故事线
  · 人接受后在创作台继续改；渲染由人触发

原料（全部来自已落盘的审计产物）：
  transcript{text,words} —— 台词与句段时间戳（vad：句边界准）
  visual{desc,ocr,usage} —— 画面描述/画面文字/用途建议（understand.py 产物）
  usable/defects         —— 拍摄层判断（人的结论优先于 AI 判断）

产物：projects/<pid>/storylines/aidraft*.json（schema 与手写故事线完全一致）
"""
import argparse, json, os, re, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # noqa: E402  复用 load_services / resolve_key

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


def kind_cn(kind):
    return {"video": "视频", "image": "图片", "audio": "音频"}.get(kind, kind)


def sentence_chunks(f, limit=8):
    """词级时间戳 → 句段（按句末标点切，短句合并）。起草引用的最小时间单位。"""
    tr = f.get("transcript") or {}
    words, text = tr.get("words") or [], tr.get("text") or ""
    if not words or not text:
        return []
    chunks, cur, start = [], [], None
    for w in words:
        if start is None:
            start = w.get("start", 0)
        cur.append(w.get("text", ""))
        end = w.get("end", start)
        if w.get("text", "") in "。！？!?；;" or len(cur) >= 40:
            chunks.append((round(start, 1), round(end, 1), "".join(cur)))
            cur, start = [], None
    if cur and start is not None:
        chunks.append((round(start, 1), round(words[-1].get("end", start), 1), "".join(cur)))
    merged = []
    for c in chunks:
        if merged and (c[1] - c[0] < 2 or len(c[2]) < 6):
            p = merged[-1]
            merged[-1] = (p[0], c[1], p[2] + c[2])
        else:
            merged.append(c)
    return merged[:limit]


def build_dossier(packs):
    """素材档案：人话版素材清单（台词+句段时间+画面+用途），供 LLM 选段。"""
    rows, mats = [], {}
    for pid in packs:
        pp = os.path.join(ROOT, "materials", "packs", pid, "pack.json")
        if not os.path.exists(pp):
            continue
        pk = json.load(open(pp, encoding="utf-8"))
        for f in pk.get("files", []):
            kind = f.get("kind", "video")
            usable = bool(f.get("usable", True))
            # mats 必须带完整档案：validate 的 trim/pad 双向钳制读 transcript.words——
            # （Claude 审查 P1#1：曾只存 {usable,duration,kind}，生产链 validate 整体短路=假绿）
            mats[f["id"]] = f
            if not usable:
                rows.append("- %s（%s）【拍摄废片——禁用】" % (f["id"], kind_cn(kind)))
                continue
            line = "- %s（%s %ss）" % (f["id"], kind_cn(kind), f.get("duration", "?"))
            tr = f.get("transcript") or {}
            if tr.get("text"):
                line += " 台词：「%s」" % tr["text"][:90]
                chunks = sentence_chunks(f)
                if chunks:
                    line += " 句段：" + "；".join("[@%ss-%ss]%s" % (a, b, t[:22]) for a, b, t in chunks)
            v = f.get("visual")
            if v:
                line += " 画面：%s" % (v.get("desc") or "")[:64]
                ct = v.get("content_type")
                # R3-3：deny 清单与 dossier.narration_eligible 同集合（broll 曾横幅判非而档案判可入=口径分裂）
                if ct in ("meta", "ambient", "broll"):
                    line += " ⛔%s类素材：禁作口播A轨" % ct
                if v.get("usage"):
                    line += "（用途：%s）" % v["usage"]
            if kind == "image":
                line += "（图片素材——不可作 A/B 轨）"
            elif kind == "audio":
                line += "（音频素材——不可作 A/B 轨）"
            if not tr.get("text") and not v:
                line += "（无理解档案）"
            rows.append(line)
    return "\n".join(rows), mats


def build_prompt(dossier, intent, transitions):
    return """你是装修口播短视频的故事线起草师。基于素材档案起草一条新故事线。

[素材档案]（台词以人工校对为准；标【拍摄废片——禁用】的素材绝对不可使用）
%s

[用户意图]
%s

[硬约束]
1. 只能使用上面列出的素材 id；使用禁用素材即违规
2. 每幕恰好一条 A 轨主画（role="A"）；可选 0-1 条 B 轨（role="B"，空镜叠画，pos 从 top-right/top-left/bottom-right/bottom-left 选）
3. narration.mode 一律 "original"（用素材原声，台词来自素材自身）
4. 幕数 2-4；单幕时长 3-20 秒；总时长 20-90 秒
5. transition_out 只能取：%s，或 null
6. 第一幕优先用带开场钩子台词的素材；空镜/环境素材适合做 B 轨叠画或转场幕
7. audio.bgm_id 只能取：%s，或 null（全片不配乐才 null；按内容情绪选）

[输出] 只输出合法 JSON（无 markdown 代码块、无解释）：
{"title":"故事线标题","outline":"这条线在讲什么（2-3句）",
"beats":[{"story":"这一幕讲什么（1-2句）",
"tracks":[{"role":"A","source_id":"素材id","src_in":起点秒,"duration":时长秒,"requirement":"选用理由一句话"},
{"role":"B","source_id":"素材id","src_in":0,"duration":秒,"pos":"top-right","scale":0.3,"requirement":"叠画理由"}],
"narration":{"mode":"original"},"transition_out":null}],
"audio":{"bgm_id":"BGM id 或 null（全片不配乐就 null）"}}""" % (dossier, intent, ", ".join(transitions) or "（无转场注册）",
    ", ".join("%s(%s：%s)" % (k, v.get("name", ""), (v.get("desc") or "")[:24]) for k, v in _bgm_reg().items() if not k.startswith("_")) or "（无 BGM 注册，bgm_id 填 null）")


def _bgm_reg():
    import os
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "registry", "bgm.json")
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def chat_llm(messages):
    svc = asr.load_services()
    be = svc["llm"]["backends"][svc["llm"]["provider"]]
    key = asr.resolve_key(be)
    if not key:
        raise RuntimeError("未找到 llm key：env %s 或 api_key_file" % be.get("api_key_env", "?"))
    # M3 是思考型模型：think 可能吃掉全部 token 导致正文为空——大 max_tokens + 空回答重试一次
    last_err = None
    for attempt, cap in enumerate((8192, 12288, 16384)):  # M3 think 额度阶梯（minimax-av 教训：think 吃满=空正文，加档不封顶思维）
        payload = {"model": be["model"], "messages": messages, "max_tokens": cap}
        req = urllib.request.Request(
            be["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        resp = json.loads(urllib.request.urlopen(req, timeout=be.get("timeout_s", 300)).read())
        msg = ((resp.get("choices") or [{}])[0].get("message", {}) or {})
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        if text:
            return text
        last_err = "空回答（think 吃满 %d token，usage=%s）" % (
            cap, (resp.get("usage") or {}).get("completion_tokens"))
    raise RuntimeError("起草失败：" + str(last_err) + "——重试仍空，请缩短意图或稍后再试")


def extract_json(text):
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(t[i:j + 1])
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    raise RuntimeError("LLM 未返回合法 JSON：" + text[:120])


def validate(draft, mats, transitions):
    """修复式校验：越界钳制、废片剔除、保证每幕有 A 轨、转场白名单。"""
    beats = []
    for b in (draft.get("beats") or [])[:4]:
        tracks = []
        for t in (b.get("tracks") or []):
            m = mats.get(t.get("source_id"))
            if not m or not m.get("usable", True):  # R2-4：mats=f 后外部手造 pack 可能缺 usable 键
                continue
            src_in = max(0.0, float(t.get("src_in") or 0))
            dur = float(t.get("duration") or 0)
            mdur = float(m.get("duration") or 0)
            if dur <= 0:
                dur = min(6.0, max(1.0, mdur or 6.0))
            if mdur:
                src_in = min(src_in, max(0.0, mdur - 1))
                dur = min(dur, max(1.0, mdur - src_in))
            if m.get("kind") != "video":
                continue  # 图片/音频不可作轨道（图片无音轨、音频无画面）——起草硬约束
            role = "B" if t.get("role") == "B" else "A"
            # 截字修复（2026-09-11 苏炜回看）：ASR 词尾时间戳偏紧，出点压着词尾会咬掉最后一字的尾音。
            # A 轨出点向后留 0.35s 气口（不越素材物理边界）；B 轨是画中画无语音，不处理。
            # 死尾巴修剪（2026-09-11 二次回看）：出点也不晚于末实词尾+0.8s——话说完人杵着 2s 是废段。
            # 句中截断天然不触发（末词≈窗口边缘，+0.8 必大于窗口尾），只有"词全说完素材还长"才裁。
            if role != "B" and mdur and m.get("transcript", {}).get("words"):
                last_end = asr.speech_tail(m["transcript"]["words"], src_in + dur)
                if last_end > 0:
                    tail = last_end - src_in
                    dur = min(mdur - src_in, max(dur, tail + 0.35))   # 下限：词尾气口（防咬字）
                    dur = min(dur, tail + 0.8)                        # 上限：死尾巴修剪（防废段）
            tr = {"role": role, "source_id": t["source_id"], "src_in": round(src_in, 1),
                  "duration": round(dur, 1), "requirement": str(t.get("requirement") or "")[:80]}
            if role == "B":
                tr.update({"op": "overlay-pip", "pos": t.get("pos") or "top-right",
                           "scale": min(0.6, max(0.15, float(t.get("scale") or 0.3)))})
            tracks.append(tr)
        if not tracks:
            continue
        if not any(t["role"] == "A" for t in tracks):
            tracks[0]["role"] = "A"
        to = b.get("transition_out")
        beats.append({"story": str(b.get("story") or "")[:120], "tracks": tracks,
                      "narration": {"mode": "original"}, "music": {"inherit": True},
                      "effects": {"stickers": [], "sfx": []}, "subtitle": {},
                      "transition_out": to if to in transitions else None})
    return beats


def save_storyline(pid, draft, beats):
    pdir = os.path.join(ROOT, "projects", pid)
    sl = {}
    sp = os.path.join(pdir, "storylines")
    os.makedirs(sp, exist_ok=True)
    # meta 继承当前故事线（保持类型/平台/字幕风格一致——AI 只起草叙事，不改工程设定）
    sids = sorted(f[:-5] for f in os.listdir(sp) if f.endswith(".json"))
    for s in sids:
        cur = json.load(open(os.path.join(sp, s + ".json"), encoding="utf-8"))
        if cur.get("meta"):
            sl["meta"] = cur["meta"]
            break
    sid = "aidraft"
    n = 2
    while os.path.exists(os.path.join(sp, sid + ".json")):
        sid = "aidraft%d" % n
        n += 1
    sl.update({"title": "AI起草 · " + str(draft.get("title") or "未命名"),
               "outline": str(draft.get("outline") or ""),
               "origin": "ai-draft",
               "beats": [dict(b, no=i + 1, id="b%d" % (i + 1)) for i, b in enumerate(beats)]})
    _au = draft.get("audio") or {}
    if isinstance(_au, dict) and _au.get("bgm_id"):
        sl.setdefault("meta", {}).setdefault("audio", {})["bgm_id"] = str(_au["bgm_id"])
    with open(os.path.join(sp, sid + ".json"), "w", encoding="utf-8") as fh:
        json.dump(sl, fh, ensure_ascii=False, indent=1)
    return sid


def run(project, intent, packs=None, save=True):
    """主入口（Studio 与 CLI 共用）。返回 (sid or None, draft, beats)。"""
    packs = packs or project_packs(project)
    if not packs:
        raise RuntimeError("项目未挂载任何素材包")
    dossier, mats = build_dossier(packs)
    if len(dossier) < 40:
        raise RuntimeError("素材档案为空——先完成转写/画面识别")
    tr = os.path.join(ROOT, "registry", "transitions.json")
    transitions = list(json.load(open(tr, encoding="utf-8")).keys()) if os.path.exists(tr) else []
    messages = [{"role": "user", "content": build_prompt(dossier, intent, transitions)}]
    text = None
    draft = None
    for attempt in (1, 2):  # M3 偶发 JSON 截断（实测不稳）——重试一次，末次失败如实抛
        text = chat_llm(messages)
        try:
            draft = extract_json(text)
            break
        except RuntimeError as e:
            if attempt == 2:
                raise
            print("  LLM JSON 解析失败（%s），重试一次…" % str(e)[:60], flush=True)
    beats = validate(draft, mats, transitions)
    if not beats:
        raise RuntimeError("草稿校验后无有效幕（素材 id 都对不上？）")
    sid = save_storyline(project, draft, beats) if save else None
    return sid, draft, beats


def main():
    ap = argparse.ArgumentParser(description="故事线起草生成器（素材理解档案 → AI 草稿）")
    ap.add_argument("project")
    ap.add_argument("--intent", required=True, help="一句话起草意图")
    ap.add_argument("--packs", help="逗号分隔素材包范围（默认全部挂载包）")
    ap.add_argument("--save", action="store_true", help="落盘为新故事线（不加则只打印草稿）")
    ap.add_argument("--dub", action="store_true", help="逐幕 AI 配音（TTS，narration.mode=dub）")
    a = ap.parse_args()
    sid, draft, beats = run(a.project, a.intent, packs=a.packs.split(",") if a.packs else None, save=a.save)
    if a.dub and sid and beats:
        # R3：逐幕配音（story=口播词）→ narration.mode=dub + audio 路径写回故事线
        import tts
        pdir = a.project if os.path.isdir(a.project) else os.path.join(ROOT, "projects", a.project)
        ddir = os.path.join(pdir, "materials", "dub")
        os.makedirs(ddir, exist_ok=True)
        ok_n = 0
        for idx, bt in enumerate(beats):  # beats 元素无 no 字段——按列表序=幕序对齐
            words = (bt.get("story") or "").strip()
            if not words:
                continue
            af = "materials/dub/%s-b%d.mp3" % (sid, idx + 1)
            try:
                tts.synth(words, out=os.path.join(ddir, os.path.basename(af)))
                bt["narration"] = {"mode": "dub", "audio": af}
                bt["no"] = idx + 1
                ok_n += 1
            except Exception as e:
                print("  幕%d 配音失败，保留原声: %s" % (idx + 1, e), flush=True)
        if ok_n and a.save:  # 幕数一致才写回（防中途增删幕错位）
            sp = os.path.join(pdir, "storylines", sid + ".json")
            sl = json.load(open(sp, encoding="utf-8"))
            slb = sl.get("beats", [])
            if len(slb) == len(beats):
                for k, b2 in enumerate(slb):
                    if (beats[k].get("narration") or {}).get("mode") == "dub":
                        b2["narration"] = beats[k]["narration"]
                json.dump(sl, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            else:
                print("  幕数不一致（草稿 %d vs 落盘 %d），配音信息不写回" % (len(beats), len(slb)), flush=True)
        print("DUB: %d/%d 幕配音完成" % (ok_n, len(beats)), flush=True)
    for i, b in enumerate(beats, 1):
        ts = ", ".join("%s:%s@%ss+%ss" % (t["role"], t["source_id"], t["src_in"], t["duration"]) for t in b["tracks"])
        print("  幕%d [%s] %s" % (i, b["transition_out"] or "无转场", ts))
        print("      %s" % b["story"])
    print("DRAFT DONE: %d 幕%s" % (len(beats), ("，已落盘 → " + sid) if sid else "（未落盘，加 --save）"))


if __name__ == "__main__":
    main()
