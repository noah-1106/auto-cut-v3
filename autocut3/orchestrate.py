#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排器——Agent 全自动管线的"司机"（2026-09-15 Claude 接管后立项，gap-analysis A/D 的落地件）。

人机票价（准则：人机同权）：
  Agent : python3 autocut3/orchestrate.py <project>            # 看状态+下一动作
          python3 autocut3/orchestrate.py <project> --advance  # 推进到下一道门
          python3 autocut3/orchestrate.py <project> --advance --intent "装修避坑口播"
  人    : Studio 顶部管线进度条（同源 /api/status，信息完全对称）

设计要点：
  · 状态全部从文件推导（零新数据库）——与人看到的必然是同一份事实
  · 素材处理段全自动（mount→转写→理解→digest→校对→档案→缺陷台账）；
    创作段以"意图"为门（--intent），draft 之后 vadwords→dubfit→dubgate→渲染→QC 继续自动
  · 每步推进都走现有模块入口（subprocess），不复制逻辑；失败即停，错误原样透出
  · aigen（AI 生成素材，video_gen.py）为预留槽位：默认 n/a，见 STATUS 表

环节判据（每步"完成"的客观定义，供 /api/status 复用）：
  mount      library.json packs 非空
  transcribe 全部非图片素材 audit.transcript=done（error:→failed）
  understand 全部视频/图片 audit.visual∈{done,n-a}
  digest     每个挂载包 pack.json.digest 存在
  proofread  全部有词轨素材 audit.proofread 落盘（auto-done/done；error→failed）
  dossier    projects/<pid>/dossier.json 存在
  disposition projects/<pid>/disposition.json 存在（M2 落地；此前该步 na）
  storyline  storylines/*.json 非空
  vadwords   当前故事线每幕 narration.words 在场（VAD 物理测量时间源）
  dubfit     无 dub 幕=na；有则每幕 narration.dubfit 在场且 dubfit-report passed
  dubgate    读 dubgate-report.json（dubfit/dubgate 产物）passed——D1-D3 同源
  render     out-<sid>.mp4 存在且新于故事线
  qc         qc-report.json 存在（verdict=blocked→failed）
  aigen      预留槽位（video_gen 口）——默认 na，接活时在此登记判据
"""
import argparse, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _jload(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def _packs(pdir):
    lib = _jload(f"{pdir}/materials/library.json", {})
    packs = lib.get("packs") or []
    if not packs:
        packs = _jload(f"{pdir}/project.json", {}).get("material_packs") or []
    return packs


def _pack_files(packs):
    """挂载包全部素材条目 [{pack, file_entry}]（文件缺失的包跳过）。"""
    out = []
    for pid in packs:
        pj = _jload(f"{ROOT}/materials/packs/{pid}/pack.json", {})
        for f in pj.get("files", []):
            out.append((pid, f))
    return out


def _latest_story(pdir):
    sd = f"{pdir}/storylines"
    if not os.path.isdir(sd):
        return None
    cands = [f for f in os.listdir(sd) if f.endswith(".json")]
    if not cands:
        return None
    return max(cands, key=lambda f: os.path.getmtime(os.path.join(sd, f)))[:-5]


def _step_mount(pdir):
    packs = _packs(pdir)
    return ("done" if packs else "pending",
            f"{len(packs)} 个包" if packs else "先挂载素材包（CLI: library.py / Studio 素材库）")


def _step_transcribe(pdir):
    files = _pack_files(_packs(pdir))
    todo = [(f.get("id"), (f.get("audit") or {}).get("transcript"))
            for _, f in files if f.get("kind") != "image"]
    if not todo:
        return ("na", "无音视频素材")
    errs = [m for m, a in todo if a and str(a).startswith("error")]
    if errs:
        return ("failed", "转写失败: %s（重跑 transcribe.py --force 或查 services.json key）" % ",".join(errs[:3]))
    left = [m for m, a in todo if a != "done"]
    return ("done" if not left else "pending",
            "全部转写完成" if not left else "待转写 %d/%d（%s…）" % (len(left), len(todo), ",".join(left[:3])))


def _step_understand(pdir):
    files = _pack_files(_packs(pdir))
    todo = [(f.get("id"), (f.get("audit") or {}).get("visual"))
            for _, f in files if f.get("kind") in ("video", "image")]
    if not todo:
        return ("na", "无视觉素材")
    left = [m for m, a in todo if a not in ("done", "n/a")]
    return ("done" if not left else "pending",
            "全部识别完成" if not left else "待识别 %d/%d（%s…）" % (len(left), len(todo), ",".join(left[:3])))


def _step_digest(pdir):
    packs = _packs(pdir)
    if not packs:
        return ("na", "未挂载包")
    left = [p for p in packs if not _jload(f"{ROOT}/materials/packs/{p}/pack.json", {}).get("digest")]
    return ("done" if not left else "pending",
            "全部包已盘点" if not left else "缺 digest: %s（understand.py 批处理自动产出，或 Studio 重新盘点）" % ",".join(left))


def _step_proofread(pdir):
    files = _pack_files(_packs(pdir))
    todo = [(f.get("id"), (f.get("audit") or {}).get("proofread"))
            for _, f in files if (f.get("transcript") or {}).get("words")]
    if not todo:
        return ("na", "无词轨素材")
    errs = [m for m, a in todo if a and str(a).startswith("error")]
    if errs:
        return ("failed", "校对失败: %s" % ",".join(errs[:3]))
    left = [m for m, a in todo if not a]
    return ("done" if not left else "pending",
            "词轨已校对" if not left else "待校对 %d/%d" % (len(left), len(todo)))


def _step_dossier(pdir):
    ok = os.path.exists(f"{pdir}/dossier.json")
    return ("done" if ok else "pending", "档案已合成" if ok else "dossier.py 合成素材档案")


def _step_disposition(pdir):
    try:
        import disposition
    except ImportError:
        return ("na", "未安装")
    return disposition.step_status(pdir)


def _step_storyline(pdir):
    sid = _latest_story(pdir)
    return ("done" if sid else "pending",
            f"当前故事线: {sid}" if sid else "需要创作故事线（--advance --intent \"…\" 触发 AI 起草）")


def _active_story(pdir):
    sid = _latest_story(pdir)
    return sid, _jload(f"{pdir}/storylines/{sid}.json", {}) if sid else (None, {})


def _step_vadwords(pdir):
    sid, sl = _active_story(pdir)
    if not sid:
        return ("pending", "先有成片故事线")
    beats = sl.get("beats") or []
    speech = [b for b in beats if (b.get("narration") or {}).get("mode", "original") != "none"]
    if not speech:
        return ("na", "故事线无语义幕")
    left = [b.get("no") for b in speech if not (b.get("narration") or {}).get("words")]
    return ("done" if not left else "pending",
            "VAD 词轨在场" if not left else f"幕 {left} 缺 VAD 词轨（渲染前自动跑 vadwords）")


def _step_dubfit(pdir):
    sid, sl = _active_story(pdir)
    if not sid:
        return ("pending", "先有成片故事线")
    dubs = [b for b in (sl.get("beats") or []) if (b.get("narration") or {}).get("mode") == "dub"]
    if not dubs:
        return ("na", "无 dub 幕")
    left = [b.get("no") for b in dubs if not (b.get("narration") or {}).get("dubfit")]
    if left:
        return ("pending", "幕 %s 待配音裁剪（渲染前自动跑 dubfit）" % left)
    rp = f"{pdir}/dubfit-report.json"
    if os.path.exists(rp):
        r = _jload(rp, {})
        if r.get("passed") is True:
            return ("done", "配音裁剪完成（报告 dubfit-report.json）")
        if r.get("passed") is False:
            return ("failed", "dubfit 未过: %s" % (r.get("fails") or "见报告"))
    return ("pending", "待跑 dubfit")


def _step_dubgate(pdir):
    sid, sl = _active_story(pdir)
    if not sid:
        return ("pending", "先有成片故事线")
    dubs = [b for b in (sl.get("beats") or []) if (b.get("narration") or {}).get("mode") == "dub"]
    if not dubs:
        return ("na", "无 dub 幕")
    rp = f"{pdir}/dubgate-report.json"
    if os.path.exists(rp):
        r = _jload(rp, {})
        if r.get("passed") is True:
            return ("done", "配音门禁全过（报告 %s）" % os.path.basename(rp))
        if r.get("passed") is False:
            return ("failed", "配音门禁拦截: %s" % (r.get("fails") or "见报告"))
    return ("pending", "%d 个 dub 幕待过门禁" % len(dubs))


def _step_render(pdir):
    sid, sl = _active_story(pdir)
    if not sid:
        return ("pending", "先有成片故事线")
    out = f"{pdir}/out-{sid}.mp4"
    sp = f"{pdir}/storylines/{sid}.json"
    if os.path.exists(out) and os.path.getmtime(out) > os.path.getmtime(sp):
        return ("done", f"out-{sid}.mp4 新于故事线")
    st = _jload(f"{pdir}/render.status", {})
    if st.get("running"):
        return ("pending", f"渲染进行中 {st.get('pct', 0)}%（{st.get('stage', '?')}）")
    return ("pending", f"待渲染（story={sid}）")


def _step_qc(pdir):
    sid = _latest_story(pdir)
    rp = f"{pdir}/qc-report.json"
    if not os.path.exists(rp):
        return ("pending", "待 QC")
    r = _jload(rp, {})
    if r.get("verdict") == "blocked":
        return ("failed", "QC 不可交付（%s blocker——按 suggested_fix 修故事线重渲）"
                % r.get("counts", {}).get("blocker", "?"))
    return ("done", "QC %s" % r.get("verdict", "?"))


def _step_aigen(pdir):
    # 预留槽位：video_gen.py（MiniMax 数字人/文生视频）接入点。接活时在此登记判据与推进动作。
    return ("na", "预留槽位（AI 生成素材，video_gen 口）")


STEPS = [
    ("mount",       "挂载素材包", _step_mount),
    ("transcribe",  "转写",       _step_transcribe),
    ("understand",  "画面识别",   _step_understand),
    ("digest",      "包级盘点",   _step_digest),
    ("proofread",   "词轨校对",   _step_proofread),
    ("dossier",     "素材档案",   _step_dossier),
    ("disposition", "缺陷台账",   _step_disposition),
    ("storyline",   "故事线",     _step_storyline),
    ("vadwords",    "VAD 词轨",   _step_vadwords),
    ("dubfit",      "配音裁剪",   _step_dubfit),
    ("dubgate",     "配音门禁",   _step_dubgate),
    ("render",      "渲染",       _step_render),
    ("qc",          "审片 QC",    _step_qc),
    ("aigen",       "AI 生成素材", _step_aigen),
]

# 全自动推进段：素材处理段无条件自动；storyline 需要 intent 门；之后自动
_ADVANCE_AUTO = {"mount", "transcribe", "understand", "digest", "proofread", "dossier", "disposition"}
_ADVANCE_INTENT = {"storyline"}   # 需要 --intent
_ADVANCE_STORY = {"vadwords", "dubfit", "dubgate", "render", "qc"}


def status(project):
    """环节清单（/api/status 与 CLI 共用同一实现——人机同一份事实）。"""
    pdir = project if os.path.isdir(project) else os.path.join(ROOT, "projects", project)
    steps = []
    for key, name, fn in STEPS:
        try:
            st, detail = fn(pdir)
        except Exception as e:
            st, detail = "pending", "判据异常: %s" % str(e)[:80]
        steps.append({"step": key, "name": name, "status": st, "detail": detail})
    nxt = next((s["step"] for s in steps if s["status"] == "pending"), None)
    return {"project": os.path.basename(pdir.rstrip("/")), "steps": steps, "next": nxt}


def _run(cmd, label):
    print(f"  ▸ {label}: {' '.join(os.path.basename(c) for c in cmd[:3])} …", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    tail = (r.stdout or "")[-300:]
    if r.returncode != 0:
        raise RuntimeError(f"{label} 失败（exit {r.returncode}）: {(r.stderr or '')[-300:]}")
    return tail


def advance(project, intent=None, story=None, only=None):
    """推进到下一道门。返回最终 status。任何一步失败即停并抛出。"""
    name = os.path.basename(project.rstrip("/"))
    pdir = project if os.path.isdir(project) else os.path.join(ROOT, "projects", project)
    py = sys.executable
    ac = os.path.join(ROOT, "autocut3")

    st = status(project)
    todo = [s["step"] for s in st["steps"] if s["status"] == "pending"]
    if not todo:
        print("全部环节完成，无可推进。")
        return st
    print("待推进: %s" % " → ".join(todo), flush=True)  # 留痕：某步被跳过时可对账 todo 快照
    for key in todo:
        if only and key != only:
            continue
        if key in _ADVANCE_INTENT and not intent:
            print(f"══ 停在故事线门：全自动素材段已就绪，创作需要意图 ══")
            print(f"   python3 autocut3/orchestrate.py {name} --advance --intent \"一句话说明这条视频讲什么\"")
            break
        print(f"══ 推进: {key} ══", flush=True)
        if key == "mount":
            print("   挂载只由人/Agent 显式决定（挂什么包是创作决策，不进全自动）——Studio 素材库或 pack-mount 完成")
            break
        if key == "transcribe":
            _run([py, os.path.join(ac, "transcribe.py"), name], "转写")
        elif key == "understand":
            _run([py, os.path.join(ac, "understand.py"), name], "画面识别")
        elif key == "digest":
            for pid in _packs(pdir):
                import understand as U
                print("  📋 digest %s …" % pid, flush=True)
                d = U.build_digest(pid)
                print("    %s（A轨候选%d/B-roll池%d）" % (
                    (d.get("theme") or "")[:40],
                    len(d.get("inventory", {}).get("a_roll_candidates", [])),
                    len(d.get("inventory", {}).get("broll_pool", []))), flush=True)
        elif key == "proofread":
            for pid in _packs(pdir):
                import proofread as PF
                PF.run(pid)
        elif key == "dossier":
            _run([py, os.path.join(ac, "dossier.py"), name], "素材档案")
        elif key == "disposition":
            import disposition
            disposition.build(name)
        elif key == "storyline":
            import draft as D
            sid, _d, beats = D.run(name, intent, save=True)
            print(f"  ✓ 故事线落盘 storylines/{sid}.json（{len(beats)} 幕）")
        elif key == "vadwords":
            sid = story or _latest_story(pdir)
            _run([py, os.path.join(ac, "vadwords.py"), name, "--story", sid], "VAD 词轨")
        elif key == "dubfit":
            sid = story or _latest_story(pdir)
            _run([py, os.path.join(ac, "dubfit.py"), name, "--story", sid], "配音裁剪")
        elif key == "dubgate":
            sid = story or _latest_story(pdir)
            _run([py, os.path.join(ac, "dubgate.py"), name, "--story", sid], "配音门禁")
        elif key == "render":
            sid = story or _latest_story(pdir)
            print(f"  ▸ render story={sid}（进度见 render.status / Studio）…", flush=True)
            r = subprocess.run([py, os.path.join(ac, "pipeline.py"), "render", name, sid],
                               capture_output=True, text=True)
            last = (r.stdout or "").strip().split("\n")[-1]
            print("  " + last)
            if r.returncode != 0:
                raise RuntimeError("渲染失败: " + (r.stdout or "")[-300:])
        elif key == "qc":
            import qc
            r = qc.run(name, story=story or _latest_story(pdir))
            print("  QC: " + r["one_line"])
            if r["verdict"] == "blocked":
                raise RuntimeError("QC blocked——按 qc-report.json 的 suggested_fix 修复后重渲")
            # 幕样张自检（2026-09-15 维护者 #9/#11：Agent 要知道可以渲幕检查——此处钉进流程）：
            # 逐幕独立渲染（build_beat_cmd 与全片渲染是两条代码路径，全片过≠单幕过），
            # 产物 previews/beat-*.mp4 供人/Agent 抽查；任一幕失败=拦截
            sid0 = story or _latest_story(pdir)
            import pipeline
            plan0 = pipeline.build_plan(pdir, sid0)
            bad = []
            for seg in plan0["segments"]:
                rb = subprocess.run([py, os.path.join(ac, "pipeline.py"), "beat", name,
                                     str(seg.get("no")), sid0], capture_output=True, text=True)
                if rb.returncode != 0:
                    bad.append("幕%s: %s" % (seg.get("no"), (rb.stdout or rb.stderr or "")[-120:]))
            if bad:
                raise RuntimeError("幕样张渲染失败（%d 幕）——\n" % len(bad) + "\n".join(bad))
            print("  幕样张: %d 幕 ✓ → previews/beat-*.mp4" % len(plan0["segments"]))
        elif key == "aigen":
            continue  # 预留槽位
        # 每步推进后重算状态；失败下一步的判据会自然反映
        st = status(project)
        cur = next(s for s in st["steps"] if s["step"] == key)
        if cur["status"] not in ("done", "na"):
            raise RuntimeError(f"推进 {key} 后状态仍={cur['status']}（{cur['detail']}）——检查上一步输出")
    return status(project)


def render_table(st):
    icon = {"done": "✓", "pending": "○", "failed": "✗", "na": "·"}
    print("══ 管线状态 · %s ══" % st["project"])
    for s in st["steps"]:
        print("  %s %-2s %-8s %s" % (icon.get(s["status"], "?"), s["name"], s["status"], s["detail"]))
    if st["next"]:
        print("── 下一动作: %s ──" % st["next"])
    else:
        print("── 全部就绪 ──")
    return st


def main():
    ap = argparse.ArgumentParser(description="管线编排器（状态推导 + 推进到下一道门）")
    ap.add_argument("project")
    ap.add_argument("--advance", action="store_true", help="推进到下一道门（失败即停）")
    ap.add_argument("--intent", help="故事线门的创作意图（触发 AI 起草）")
    ap.add_argument("--story", help="指定故事线（默认最近修改）")
    ap.add_argument("--step", help="只推进指定环节")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供 /api/status 复用）")
    a = ap.parse_args()
    if a.advance:
        st = advance(a.project, intent=a.intent, story=a.story, only=a.step)
    else:
        st = status(a.project)
    if a.json:
        print(json.dumps(st, ensure_ascii=False))
    else:
        render_table(st)


if __name__ == "__main__":
    main()
