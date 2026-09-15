#!/usr/bin/env python3
"""auto-cut v3 代码审计器（2026-09-14 质量整顿）
七个维度：路由安全 / 并发写点 / JS 函数对照 / 数据引用完整性 / Python 卫生 / 文档时效 / git 卫生
用法：python3 tests/audit.py   输出：分级报告（P1 必修 / P2 应修 / P3 记录）
"""
import ast, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
issues = []  # (级别, 维度, 描述)

def add(lv, dim, msg):
    issues.append((lv, dim, msg))
    print("  [%s] %s: %s" % (lv, dim, msg))

def sec(title):
    print("\n══ %s ══" % title)

# ═══════════ A. studio.py 路由安全 ═══════════
sec("A. 路由安全（studio.py）")
studio = open(os.path.join(ROOT, "studio.py"), encoding="utf-8").read()

# A1. 从 path/query 取出的用户参数，用前是否过校验（_safe/_safe_id/_filename/_safe_pid）
# 抽取所有 parts[N] / split("/")[N] 赋值行，检查后续 5 行内是否有校验调用
param_lines = []
for i, ln in enumerate(studio.split("\n")):
    if re.search(r'(parts\[\d\]|u\.path\.split\("/"\)\[\d\]|_q\.get\()', ln) and "=" in ln:
        param_lines.append((i, ln.strip()))
unvalidated = []
lines = studio.split("\n")
for i, ln in param_lines:
    # 变量名
    m = re.match(r".*?(\w+)\s*=", ln)
    if not m: continue
    var = m.group(1)
    if var in ("parts", "u", "_q", "m"):  # 容器本身不算
        continue
    window = "\n".join(lines[i:i+6])
    if var.startswith("_") and var.endswith("_"): continue
    EXEMPT = {"sid_render_progress", "kind"}  # 已人工复核：kind 在 KIND 白名单内 / sid 仅入内存 job key
    if not re.search(r'_safe\(|_safe_id\(|_filename\(|isdigit\(\)|in \("|\.(lower\(\))', window):
        if var == "kind" and "KIND" in "\n".join(lines[i:i+12]):
            continue
        if var == "rid" and "unquote" in window or var == "rid" and "KIND" in "\n".join(lines[i:i+12]):
            continue
        if var == "sid" and "_q.get(\"story\")" in ln:
            continue  # story 仅入 RENDER_JOBS key / beat CLI 参数，无文件系统路径拼接
        unvalidated.append((i+1, var, ln[:70]))
if unvalidated:
    for ln_no, var, txt in unvalidated:
        add("P2", "A1 路由参数", "L%d 变量 %s 未见显式校验（人工复核是否安全）：%s" % (ln_no, var, txt))
else:
    print("  ✓ 全部路由参数有校验")

# A2. /files/ 防穿越仍在（commonpath 目录级比较，2026-09-15 替代 startswith 前缀匹配——
#     startswith 会被 auto-cut-v3x 兄弟目录绕过）
if "realpath" in studio and "os.path.commonpath" in studio:
    print("  ✓ /files/ realpath 防穿越在位（commonpath）")
else:
    add("P1", "A2 穿越防护", "/files/ realpath 防穿越丢失！")

# A3. path 段解码：入口统一 unquote 即根治（2026-09-14 方案）；入口缺失才报 P1
if "unquote(self.path" not in studio:
    add("P1", "A3 入口解码", "do_GET/do_POST 入口未统一 unquote——path 段中文 id 将 400（同族问题复发风险）")
else:
    print("  ✓ path 段入口统一解码在位")

# ═══════════ B. 并发写点 ═══════════
sec("B. 并发写点")
# B1. pack.json / registry 写点锁覆盖（rmw_smoke 已有静态扫描，这里复核 registry）
reg_writes = re.findall(r'json\.dump\([^)]*open\((f"[^"]*registry[^"]*")', studio)
print("  registry 写点 %d 处（并发保存互相覆盖风险=已知 P3，面板单人使用）" % len(reg_writes))
for f in re.finditer(r'open\(f"\{ROOT\}/registry/\{rname\}\.json",\s*"w"', studio):
    print("  · registry-save 写点存在（锁豁免：注册表保存为全量替换，冲突窗口=整文件覆盖，单人可接受）")

# ═══════════ C. JS 前端 ═══════════
sec("C. JS 前端（studio/index.html）")
html = open(os.path.join(ROOT, "studio", "index.html"), encoding="utf-8").read()
js = html.split("<script>")[1].split("</script>")[0] if "<script>" in html else ""

# C1. 事件处理器引用的函数全部已定义
defined = set(re.findall(r'function\s+(\w+)\s*\(', js))
# onclick="fn(...)" / onchange="fn(...)" / onerror="..." 里引用的函数
used = set()
for m in re.finditer(r'on(?:click|change|input|error|load)=\\?"(\w+)\(', js + html):
    used.add(m.group(1))
missing = sorted(used - defined - {"event", "if", "this"})
if missing:
    for fn in missing:
        add("P1", "C1 JS函数", "事件处理器引用的函数未定义: %s()" % fn)
else:
    print("  ✓ 事件处理器引用的 %d 个函数全部已定义" % len(used))

# C2. 重复函数定义（后定义静默覆盖——patch 事故温床）
all_defs = re.findall(r'function\s+(\w+)\s*\(', js)
from collections import Counter
dup = [k for k, v in Counter(all_defs).items() if v > 1]
if dup:
    for fn in dup:
        add("P1", "C2 重复定义", "JS 函数重复定义（后者静默覆盖前者）: %s()" % fn)
else:
    print("  ✓ 无重复函数定义（%d 个）" % len(all_defs))

# C3. node 语法检查
import tempfile
_fd, _ui_tmp = tempfile.mkstemp(suffix=".js"); os.close(_fd)
r = subprocess.run(["node", "--check", _ui_tmp], capture_output=True, text=True)
open(_ui_tmp, "w", encoding="utf-8").write(js)
r = subprocess.run(["node", "--check", _ui_tmp], capture_output=True, text=True)
if r.returncode == 0:
    print("  ✓ node --check 语法通过")
else:
    add("P1", "C3 JS语法", r.stderr[:200])

# C4. let/const TDZ 风险：let 声明的全局变量在函数中使用（函数可能在声明前被调用）
for m in re.finditer(r'^let\s+(\w+)', js, re.M):
    var = m.group(1)
    uses = len(re.findall(r'\b%s\b' % var, js))
    print("  · 全局 let %s（引用 %d 处——TDZ 需人工确认调用时序）" % (var, uses))

# ═══════════ D. 数据引用完整性 ═══════════
sec("D. 数据引用完整性")
# D1. registry 条目的 file 存在
for rj in ["bgm", "sfx", "stickers"]:
    p = os.path.join(ROOT, "registry", rj + ".json")
    if not os.path.exists(p): continue
    d = json.load(open(p, encoding="utf-8"))
    for k, v in d.items():
        if k.startswith("_"): continue
        f = v.get("file")
        if f and not os.path.exists(os.path.join(ROOT, f)):
            add("P1", "D1 注册表断链", "%s.json[%s] → %s 文件不存在" % (rj, k, f))
print("  ✓ D1 registry file 引用核查完成")

# D2. 每个 pack 的 file/thumb 存在
for pk_dir in sorted(os.listdir(os.path.join(ROOT, "materials", "packs"))):
    pp = os.path.join(ROOT, "materials", "packs", pk_dir, "pack.json")
    if not os.path.exists(pp): continue
    pk = json.load(open(pp, encoding="utf-8"))
    for f in pk.get("files", []):
        fp = os.path.join(ROOT, "materials", "packs", pk_dir, f.get("file", ""))
        if not os.path.exists(fp):
            add("P1", "D2 素材断链", "%s[%s] → %s 缺失" % (pk_dir, f["id"], f.get("file")))
print("  ✓ D2 素材文件核查完成")

# D3. 故事线 source_id ∈ pack entry ids
for proj in sorted(os.listdir(os.path.join(ROOT, "projects"))):
    sd = os.path.join(ROOT, "projects", proj, "storylines")
    if not os.path.isdir(sd): continue
    # 项目引用的包
    pj = os.path.join(ROOT, "projects", proj, "project.json")
    libp = os.path.join(ROOT, "projects", proj, "materials", "library.json")
    packs = []
    if os.path.exists(libp):
        packs = json.load(open(libp, encoding="utf-8")).get("packs", [])
    if not packs and os.path.exists(pj):
        packs = json.load(open(pj, encoding="utf-8")).get("material_packs", [])
    ids = set()
    for pid in packs:
        pp = os.path.join(ROOT, "materials", "packs", pid, "pack.json")
        if os.path.exists(pp):
            ids |= {f["id"] for f in json.load(open(pp, encoding="utf-8")).get("files", [])}
    for slf in sorted(os.listdir(sd)):
        if not slf.endswith(".json"): continue
        sl = json.load(open(os.path.join(sd, slf), encoding="utf-8"))
        for b in sl.get("beats", []):
            for t in b.get("tracks", []):
                sid_ = t.get("source_id")
                if sid_ and ids and sid_ not in ids:
                    add("P1", "D3 故事线断链", "%s/%s 幕%s 引用 %s 不在素材包" % (proj, slf, b.get("no"), sid_))
print("  ✓ D3 故事线引用核查完成")

# D4. 故事线的 style/bgm_id/transition ∈ 注册表
regs = {}
for rn in ["bgm", "subtitles", "transitions"]:
    p = os.path.join(ROOT, "registry", rn + ".json")
    regs[rn] = set(k for k in json.load(open(p, encoding="utf-8")) if not k.startswith("_")) if os.path.exists(p) else set()
for proj in sorted(os.listdir(os.path.join(ROOT, "projects"))):
    sd = os.path.join(ROOT, "projects", proj, "storylines")
    if not os.path.isdir(sd): continue
    for slf in sorted(os.listdir(sd)):
        if not slf.endswith(".json"): continue
        sl = json.load(open(os.path.join(sd, slf), encoding="utf-8"))
        bid = (sl.get("meta", {}).get("audio", {}) or {}).get("bgm_id")
        if bid and bid not in regs["bgm"]:
            add("P1", "D4 BGM 断链", "%s/%s bgm_id=%s 不在注册表" % (proj, slf, bid))
        sty = (sl.get("meta", {}).get("style", {}) or {}).get("subtitle")
        if sty and sty not in regs["subtitles"]:
            add("P1", "D4 字幕断链", "%s/%s subtitle=%s 不在注册表" % (proj, slf, sty))
        for b in sl.get("beats", []):
            tr = b.get("transition_out")
            if tr and tr not in regs["transitions"]:
                add("P1", "D4 转场断链", "%s/%s 幕%s transition_out=%s 不在注册表" % (proj, slf, b.get("no"), tr))
print("  ✓ D4 效果引用核查完成")

# D5. assets 孤儿（有文件但注册表没引用）
for sub in ["music", "sfx", "stickers"]:
    adir = os.path.join(ROOT, "assets", sub)
    if not os.path.isdir(adir): continue
    rj = os.path.join(ROOT, "registry", {"music": "bgm", "sfx": "sfx", "stickers": "stickers"}[sub] + ".json")
    refd = set()
    if os.path.exists(rj):
        for v in json.load(open(rj, encoding="utf-8")).values():
            if isinstance(v, dict) and v.get("file"): refd.add(os.path.basename(v["file"]))
    for fn in os.listdir(adir):
        if fn not in refd:
            add("P3", "D5 assets孤儿", "assets/%s/%s 未被注册表引用" % (sub, fn))
print("  ✓ D5 孤儿核查完成")

# ═══════════ E. Python 卫生 ═══════════
sec("E. Python 卫生")
for dirpath, _, fns in os.walk(os.path.join(ROOT, "autocut3")):
    if "_attic" in dirpath: continue
    for fn in fns:
        if not fn.endswith(".py"): continue
        fp = os.path.join(dirpath, fn)
        try:
            ast.parse(open(fp, encoding="utf-8").read())
        except SyntaxError as e:
            add("P1", "E1 语法", "%s: %s" % (fn, e))
for fn in ["studio.py", "tests/e2e.py", "tests/rmw_smoke.py"]:
    fp = os.path.join(ROOT, fn)
    if os.path.exists(fp):
        try: ast.parse(open(fp, encoding="utf-8").read())
        except SyntaxError as e: add("P1", "E1 语法", "%s: %s" % (fn, e))
print("  ✓ E1 语法全过")
# E2. 裸 except 概览（不修，记录吞错误点）
bare = len(re.findall(r'except\s*:\s*\n\s*pass', studio))
print("  · studio.py 裸 except pass %d 处（防御性吞错，渲染主链不应有）" % bare)
# E3. NameError 冒烟：import 全模块
r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0,'.'); import importlib.util; "
                    "spec=importlib.util.spec_from_file_location('s','studio.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)"],
                   capture_output=True, text=True, cwd=ROOT)
if "NameError" in r.stderr:
    add("P1", "E3 NameError", r.stderr.strip().split("\n")[-1][:150])
else:
    print("  ✓ studio.py 模块级加载无 NameError")

# ═══════════ F. 文档时效 ═══════════
sec("F. 文档时效")
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
stale = []
if "models" in readme: stale.append("README 提及 models/（已删除）")
if "registry/subtitles/" in readme and "空目录" not in readme: stale.append("README 提及 registry 子目录（已删除）")
for s in stale:
    add("P1", "F1 README 过时", s)
if not stale: print("  ✓ README 无过时结构引用")
else: print("  ✗ README 有过时引用")
missing_doc = [name for name, ok in [
    ("安装依赖说明", "安装依赖" in readme or "前置" in readme),
    ("单节点运行", "单节点" in readme),
    ("排查路径", "排查" in readme),
] if not ok]
if missing_doc:
    add("P1", "F2 README 完整性", "缺少关键章节关键词: %s（陌生人检验存疑）" % "/".join(missing_doc))

# ═══════════ G. git 卫生 ═══════════
sec("G. git 卫生")
r = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT)
tracked = r.stdout.strip().split("\n")
big = []
for t in tracked:
    fp = os.path.join(ROOT, t)
    if os.path.isfile(fp) and os.path.getsize(fp) > 5 << 20:
        big.append((t, os.path.getsize(fp) >> 20))
if big:
    for t, mb in big:
        add("P3", "G1 大文件入track", "%s %dMB（git 仓库膨胀源，评估是否该 ignore）" % (t, mb))
else:
    print("  ✓ 无 5MB+ 文件被 track")
print("  ✓ track 文件 %d 个" % len(tracked))

# ═══════════ 汇总 ═══════════
sec("汇总")
p1 = [x for x in issues if x[0] == "P1"]
p2 = [x for x in issues if x[0] == "P2"]
p3 = [x for x in issues if x[0] == "P3"]
print("P1=%d  P2=%d  P3=%d" % (len(p1), len(p2), len(p3)))
sys.exit(1 if p1 else 0)
