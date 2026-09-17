"""故事线起草生成器——主线第二步：素材理解档案 → AI 草稿故事线。

人机等价（军规1）：
  Agent : python3 autocut3/draft.py <project> --intent "用开场钩子和空镜起一条3幕口播线" [--save]
  人    : Studio 创作台「✨ AI 起草」按钮（写一句意图 → 点一下）

纪律（AI 只起草不落定）：
  · 只从 usable=true 且已过审的素材里选段；【拍摄废片】【未过审】在档案中标注为禁用
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
        dg = pk.get("digest")
        if dg:
            rows.append("【素材包盘点（导演视角，digest 生成于 %s）】题材：%s｜可作A轨：%s｜B-roll池：%s｜旁白音轨源：%s｜空镜：%s%s"
                        % (dg.get("at", "?")[:10], dg.get("theme", ""),
                           ",".join(dg.get("inventory", {}).get("a_roll_candidates", [])) or "无",
                           ",".join(dg.get("inventory", {}).get("broll_pool", [])) or "无",
                           ",".join(dg.get("inventory", {}).get("voiceover_sources", [])) or "无",
                           ",".join(dg.get("inventory", {}).get("ambient", [])) or "无",
                           ("｜缺口：" + dg["inventory"]["gaps"] if dg.get("inventory", {}).get("gaps") else "")))
            for r_ in dg.get("roles", []):
                rows.append("  · 定位 %s：%s" % (r_.get("id"), r_.get("suggest", "")))
            rows.append("——以下为逐条明细——")
        for f in pk.get("files", []):
            kind = f.get("kind", "video")
            usable = bool(f.get("usable", True))
            # mats 必须带完整档案：validate 的 trim/pad 双向钳制读 transcript.words——
            # （Claude 审查 P1#1：曾只存 {usable,duration,kind}，生产链 validate 整体短路=假绿）
            mats[f["id"]] = f
            if not usable:
                rows.append("- %s（%s）【拍摄废片——禁用】" % (f["id"], kind_cn(kind)))
                continue
            if not _reviewed(f):
                rows.append("- %s（%s）【未过审——禁用：转写/画面识别未完成（Agent 跑 transcribe+understand 后自动过审）】" % (f["id"], kind_cn(kind)))
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
                # R3-3：deny 清单与 dossier.narration_eligible 同集合（broll 曾横幅判非而档案判可入=口径分裂）。
                # 2026-09-18 空镜两级细化（agent 抓的口径分裂复发）：broll/ambient 带 has_speech
                # （104030 型实拍画外音）→ 可口播 A 轨，横幅如实标"旁白空镜"；无语音才 ⛔。
                # 规则区 6c 与 validate 同判据——三处必须同步改，横幅是喂 LLM 的逐条明细，
                # 与规则区矛盾时 LLM 大概率服从 ⛔，新规则会被起草层架空。
                _sp = bool(tr.get("words"))
                if ct == "meta":
                    line += " ⛔meta类素材：禁作口播A轨"
                elif ct in ("ambient", "broll"):
                    line += (" ✅旁白空镜（%s类自带语音）：可口播幕A轨原声" % ct if _sp
                             else " ⛔%s类素材无语音：仅纯空镜幕（mode=none）或B轨" % ct)
                if ct == "voiceover":
                    line += " 🎙voiceover：念稿/配音录制——词轨=旁白音轨源；画面禁作A轨主体（validate 硬剔除，维护者 2026-09-14/16：M0269 错判修复）也禁作B轨（读稿画面哑口型）"
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


def _reviewed(f):
    """审核有实义化（2026-09-15 维护者 #3）：review=pending-review 且该 kind 所需审计未齐 = 未过审。
    transcribe/understand 跑完会自动置 reviewed——pending-review 存续即"Agent 还没审过"，
    draft 提示词标注禁用 + validate 剔除。人保留最终否决权（toggle 拍摄废片）。"""
    if f.get("review") != "pending-review":
        return True
    aud = f.get("audit") or {}
    need = {"video": ("transcript", "visual"), "audio": ("transcript",), "image": ("visual",)}.get(
        f.get("kind", "video"), ())
    return all(aud.get(k) in ("done", "n/a") for k in need)


def _reg(name):
    import os
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "registry", name)
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _effect_catalog():
    """效果注册表 → 提示词段落（2026-09-15 维护者 #11：字幕样式/贴纸/音效此前未进提示词，
    draft 输出恒为空——Agent 无从得知可选效果。id+中文名+语义描述，LLM 按幕选用）。"""
    def _lines(rn, fmt):
        reg = _reg(rn)
        return [fmt(k, v) for k, v in reg.items() if not k.startswith("_")]
    _fmt = lambda k, v: "%s(%s%s)" % (k, (v.get("name", "") + "：") if v.get("name") else "",
                                      (v.get("desc") or v.get("name") or "")[:30])
    subs = _lines("subtitles.json", _fmt)
    stks = _lines("stickers.json", _fmt)
    sfxs = _lines("sfx.json", _fmt)
    return ("[效果注册表]（按幕选用；拿不准就留空，禁止编造表外 id）\n"
            "字幕样式 subtitle.style：%s\n"
            "贴纸 effects.stickers（词点触发；text=从本幕口播蒸馏的强调短语，≤6 字必给；at_word=该幕台词里的触发词）：%s\n"
            "音效 effects.sfx（时刻触发，at=幕内秒）：%s"
            % ("、".join(subs) or "（无）", "、".join(stks) or "（无）", "、".join(sfxs) or "（无）"))


def build_prompt(dossier, intent, transitions):
    return """你是装修口播短视频的故事线起草师。基于素材档案起草一条新故事线。

[素材档案]（台词以人工校对为准；标【拍摄废片——禁用】的素材绝对不可使用）
%s

[用户意图]
%s

%s

[硬约束]
1. 只能使用上面列出的素材 id；使用禁用素材即违规
2. 每幕恰好一条 A 轨主画（role="A"）；可选 0-1 条 B 轨（role="B"，空镜叠画，pos 从 top-right/top-left/bottom-right/bottom-left 选）
3. narration.mode："original"（口播幕，用素材原声）或 "none"（纯空镜幕——无旁白，
   靠 BGM/环境音撑）；不要用其他值（dub 是配音流程专属，起草阶段不用）
4. 幕数不设上限（2026-09-17 Noah 决策：取消 2-4 幕引导，质量优先）——以"完整讲完故事"为
   唯一标准：每幕必须有独立叙事功能，凑数幕/重复信息幕宁可砍；讲不完就加幕，常见 2-8 幕。
   单幕时长 3-20 秒；总时长 20-90 秒（这两条仍是硬约束）
5. transition_out 只能取：%s，或 null
6. 第一幕优先用带开场钩子台词的素材；空镜/环境素材两种用法：B 轨叠画，**或整幅 A 轨
   纯空镜幕（narration.mode="none" 的环境描述幕）**——别默认只做画中画
6b. B 轨铁律（validate 硬剔除，2026-09-16 agent-037 实锤 M0275/M0243 哑口型）：画面里有人在
   说话/对话/朗读的素材（dialogue/voiceover 类，或描述含对话/沟通/讲解等）禁作 B 轨——
   B 轨无音频通道，嘴动无声必然穿帮。B 轨只用纯空镜/工艺画面
6c. ⛔ meta 说戏 / voiceover 读稿画面永远禁作 A 轨（validate 硬剔除）。ambient/broll 空镜两级：
   自带有效语音的（档案标 has_speech，实拍画外音）可作口播幕 A 轨（original 原声）；
   无语音的只在 narration.mode="none" 纯空镜幕可作 A 轨整幅，口播幕（original）禁。
   voiceover 素材的词轨价值=旁白音，画面不配
7. audio.bgm_id 只能取：%s，或 null（全片不配乐才 null；按内容情绪选）
8. story 必须是可直接朗读的口播台词（第一人称口语，1-2 句）——同字段会被 TTS 逐字念出/作配音幕字幕；
   禁止画面调度描述（"右下角叠""长镜""logo入镜"这类词念出来就是总结腔，违规）
9. 效果按幕选用（字幕风格/贴纸/音效，见效果注册表）：每幕至多 1 个字幕样式、2 个贴纸、2 个音效；
   只选与幕内容语义匹配的，宁缺毋滥。贴纸 text 铁律：**从本幕口播词蒸馏的强调短语，
   不超过 6 个字**（"横厅布局""得房率高"这种；照抄整句/超过 6 字=稀释成字幕，validate 硬剔除）

[输出] 只输出合法 JSON（无 markdown 代码块、无解释）：
{"title":"故事线标题","outline":"这条线在讲什么（2-3句）",
"beats":[{"story":"该幕口播台词（可直接念的第一人称口语）",
"tracks":[{"role":"A","source_id":"素材id","src_in":起点秒,"duration":时长秒,"requirement":"选用理由一句话"},
{"role":"B","source_id":"素材id","src_in":0,"duration":秒,"pos":"top-right","scale":0.3,"requirement":"叠画理由"}],
"narration":{"mode":"original"},"transition_out":null,
"subtitle":{"style":"字幕样式id 或省略"},
"effects":{"stickers":[{"asset":"贴纸样式id","text":"≤6字强调短语","at_word":"触发词","duration":1.2,"pos":"top-center"}],
"sfx":[{"asset":"音效id","at":0.0,"duration":0.4}]}}],
"audio":{"bgm_id":"BGM id 或 null（全片不配乐就 null）"}}""" % (dossier, intent, _effect_catalog(), ", ".join(transitions) or "（无转场注册）",
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
    for attempt, cap in enumerate((8192, 12288, 16384, 24576)):  # M3 think 额度阶梯（minimax-av 教训：think 吃满=空正文，加档不封顶思维）
        payload = {"model": be["model"], "messages": messages, "max_tokens": cap}
        req = urllib.request.Request(
            be["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        resp = json.loads(urllib.request.urlopen(req, timeout=be.get("timeout_s", 300)).read())
        ch = (resp.get("choices") or [{}])[0]
        msg = ch.get("message", {}) or {}
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        if text and ch.get("finish_reason") != "length":
            return text
        # finish_reason=length=正文被截断（JSON 半成品，extract_json 必炸）——与空回答同 ladder 升档
        last_err = "%s（finish_reason=%s cap=%d，usage=%s）" % (
            "空回答" if not text else "正文截断", ch.get("finish_reason"), cap,
            (resp.get("usage") or {}).get("completion_tokens"))
    raise RuntimeError("起草失败：" + str(last_err) + "——重试仍空/截断，请缩短意图或稍后再试")


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
    # 全量校验不截断（2026-09-16 Noah 实锤 bug：基线起的 [:4] 把第 5 幕起静默丢弃——
    # C 类静默数据丢失，同 T32"截断当成功"一族）。幕数不设上限（2026-09-17 提示词同步取消
    # 2-4 幕引导），LLM 给几幕就校几幕，多幕原样保留（下游渲染/QC 均不限幕数）。
    for b in (draft.get("beats") or []):
        tracks = []
        # 旁白模式透传（2026-09-18）：validate 原把每幕硬编码 original——LLM 选 none（纯
        # 空镜幕）被静默改写，空镜只能挤 B 轨画中画。现在 original/none 照传；dub 仍是
        # CLI --dub 专属（需要 TTS 产物路径，起草阶段给不出）——非法值回退 original。
        nmode = (b.get("narration") or {}).get("mode")
        nmode = nmode if nmode in ("original", "none") else "original"
        for t in (b.get("tracks") or []):
            m = mats.get(t.get("source_id"))
            if not m or not m.get("usable", True) or not _reviewed(m):  # R2-4：mats=f 后外部手造 pack 可能缺 usable 键；未过审素材同废片剔除
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
            # content_type 强制门（2026-09-16 agent-037 实锤：提示词 advisory 拦不住，
            # M0269 voiceover 读稿画面照样 A 轨 original 裸奔成片）——标记→消费断链收口：
            # meta 说戏 / voiceover 读稿画面永远禁 A（穿帮类）；
            # ambient/broll 分层（2026-09-18 两级细化，agent"旁白空镜"缺口）：
            #   · 素材自带有效语音（transcript.words 非空，104030 型实拍画外音）→ 可作
            #     口播幕 A 轨（original 原声撑得起）；也可 mode=none 静音用画面（人定）
            #   · 无语音 → 仅纯空镜幕（mode=none）放行，口播幕禁——无声画面撑不起口播
            # B 轨哑口型铁律：管线 B 轨无音频通道，画面含说话人却无声 = 必然穿帮——
            # dialogue/voiceover 类或画面描述含说话类关键词的素材禁入 B。
            vis = m.get("visual") or {}
            ct = vis.get("content_type")
            _has_sp = bool((m.get("transcript") or {}).get("words"))
            if role == "A" and (ct in ("meta", "voiceover")
                                or (ct in ("ambient", "broll") and nmode != "none" and not _has_sp)):
                continue
            if role == "B" and (ct in ("dialogue", "voiceover") or
                                any(k in (vis.get("desc") or "")
                                    for k in ("对话", "说话", "沟通", "交谈", "朗读", "讲解", "口播", "采访", "讨论"))):
                continue
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
        # 效果选择透传（2026-09-15 维护者 #11）：注册表白名单校验后保留 LLM 选择——
        # 曾恒空丢弃（假消费）：提示词没喂注册表 + validate 重建 beats 时空 effects/subtitle
        _subs = _reg("subtitles.json")
        _stks = _reg("stickers.json")
        _sfxs = _reg("sfx.json")
        _eff = b.get("effects") or {}
        # 贴纸文字门（2026-09-18 文字模板制，v1 规矩代码化）：text=从本幕口播蒸馏的强调
        # 短语，≤6 字；>6 字=稀释成字幕，硬剔除；缺失允许（回退注册表 default_text，
        # 老故事线无 text 不断链）
        _stickers = []
        for s in (_eff.get("stickers") or [])[:2]:
            if s.get("asset") not in _stks:
                continue
            t = str(s.get("text") or "").strip()
            if len(t) > 6:
                continue
            _stickers.append({"asset": s.get("asset"), "text": t,
                              "at_word": str(s.get("at_word") or "")[:12],
                              "duration": min(6.0, max(0.3, float(s.get("duration") or 1.2))),
                              "pos": s.get("pos") if s.get("pos") in
                              ("top-left", "top-center", "top-right", "center", "bottom-left", "bottom-right") else "top-center"})
        _sfxl = [{"asset": s.get("asset"), "at": max(0.0, float(s.get("at") or 0)),
                  "duration": min(3.0, max(0.1, float(s.get("duration") or 0.4)))}
                 for s in (_eff.get("sfx") or [])[:2] if s.get("asset") in _sfxs]
        _sub = b.get("subtitle") or {}
        _substyle = _sub.get("style") if _sub.get("style") in _subs else None
        beats.append({"story": str(b.get("story") or "")[:120], "tracks": tracks,
                      "narration": {"mode": nmode}, "music": {"inherit": True},
                      "effects": {"stickers": _stickers, "sfx": _sfxl},
                      "subtitle": ({"style": _substyle} if _substyle else {}),
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
    # 缺陷台账消费（v2-①）：选段必须避开处置窗口——识别层喊过的问题，起草层必须听得见
    try:
        import disposition
        disp = disposition.lines_for_prompt(project)
    except ImportError:
        disp = ""
    if disp:
        dossier = dossier + "\n\n" + disp
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
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
