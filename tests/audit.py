#!/usr/bin/env python3
"""auto-cut v3 代码审计器（2026-09-14 质量整顿）
九个维度：路由安全 / 并发写点 / JS 函数对照 / 数据引用完整性 / Python 卫生 / 文档时效 / git 卫生 / i18n 一致性 / 人机等价对账
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

# A1. 从 path/query 取出的用户参数，用前是否过校验（_safe/_safe_name/_safe_id/_filename/_safe_pid）
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
    EXEMPT = {"sid_render_progress", "kind",  # 已人工复核：kind 在 KIND 白名单内 / sid 仅入内存 job key
              "text"}  # 已人工复核（2026-10-14）：/api/voice-test 的 text 只作 TTS 文本进 JSON body，输出路径由 _safe_id 校验过的 name 派生，无注入面
    if var in EXEMPT: continue  # 2026-10-14 修：EXEMPT 集合定义后从未被消费（装饰性豁免），接上
    if not re.search(r'_safe\(|_safe_name\(|_safe_id\(|_filename\(|isdigit\(\)|in \("|\.(lower\(\))', window):
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

# A3. path 段解码：正确次序=urlparse(self.path) 后仅解 path 段（2026-09-19 井号素材根修后的不变式）；
# 旧写法 urlparse(unquote(self.path)) 会把 query 的 %23 提前还原成 # 再被锚点截断——正确 encode 的
# 客户端也必炸 bad filename。两种偏离（缺解码 / 次序反）都算 P1 复发风险。
if "unquote(u0.path" not in studio or "urlparse(unquote(self.path" in studio:
    add("P1", "A3 入口解码", "入口解码次序偏离不变式——应为 urlparse(self.path) 后仅解 path 段（先解后 parse 会截断 query 的 %23/#，井号素材根修回归风险）")
else:
    print("  ✓ path 段入口解码次序正确（先 parse 后解 path 段）")

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

# D5. assets 孤儿（有文件但注册表没引用）——递归查文件（2026-09-18 修：原 listdir 不递归，
# assets/sfx/viral/ 这层目录名本身被当孤儿=恒误报）；许可/来源清单是合规存档，不算孤儿；
# .DS_Store 是 Finder 元垃圾（已 gitignore，碰过目录即再生），跳过否则恒误报（2026-10-14）
for sub in ["music", "sfx", "stickers"]:
    adir = os.path.join(ROOT, "assets", sub)
    if not os.path.isdir(adir): continue
    rj = os.path.join(ROOT, "registry", {"music": "bgm", "sfx": "sfx", "stickers": "stickers"}[sub] + ".json")
    refd = set()
    if os.path.exists(rj):
        for v in json.load(open(rj, encoding="utf-8")).values():
            if isinstance(v, dict) and v.get("file"): refd.add(os.path.basename(v["file"]))
    for dirpath, _, fns in os.walk(adir):
        for fn in fns:
            if fn.startswith("LICENSE") or "manifest" in fn.lower() or fn == ".DS_Store":
                continue
            if fn not in refd:
                add("P3", "D5 assets孤儿", "%s 未被注册表引用"
                    % os.path.relpath(os.path.join(dirpath, fn), ROOT))
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

# F3. 中英 README 结构同步（2026-10-15 英文版落地后加）：手工翻译必然漂移——
# 章节标题数量不一致即提醒同步（标题文字因语言不同不可比，只比结构）
_enp = os.path.join(ROOT, "README.en.md")
if not os.path.exists(_enp):
    add("P2", "F3 英文README", "README.en.md 缺失（README.md 顶部有链接指向它）")
else:
    _enr = open(_enp, encoding="utf-8").read()
    for _lv in ("## ", "### "):
        _zc, _ec = len(re.findall(r"^%s" % re.escape(_lv), readme, re.M)), len(re.findall(r"^%s" % re.escape(_lv), _enr, re.M))
        if _zc != _ec:
            add("P2", "F3 英文README", "README.en.md 与中文版 %s级标题数不一致（%d vs %d）——中文版改过，英文版需同步" % (_lv.strip(), _zc, _ec))
            break
    else:
        print("  ✓ README.en.md 章节结构与中文版一致")


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

# ═══════════ H. i18n 一致性 ═══════════
# 由来（2026-10-15 双语化后）：词典 parity / 引用完整性此前靠手工脚本抽查，
# 推送前终审曾实锤抓到 t('view.beats2') 拼接错误——此类漂移必须自动化拦截。
sec("H. i18n 一致性（studio/index.html）")
_i18n_lines = html.split("\n")
_zh0 = next((i for i, l in enumerate(_i18n_lines) if l.strip() == "zh:{"), None)
_en0 = next((i for i, l in enumerate(_i18n_lines) if l.strip() == "en:{"), None)
_en1 = next((i for i, l in enumerate(_i18n_lines) if l.strip() == "}};"), None)
if _zh0 is None or _en0 is None or _en1 is None:
    add("P1", "H1 i18n结构", "I18N 词典块结构无法定位（zh:{/en:{/}};）——词典被重构后本检查需跟随更新")
else:
    # 键提取不锚行首（词典多个键挤一行是常态）；理论误报=值内出现转义引号+'xx':形态，
    # 当前词典实测 347/347 与人工计数一致，可接受
    _key_re = re.compile(r"'([\w.]+)':")
    zh_keys = [k for l in _i18n_lines[_zh0:_en0] for k in _key_re.findall(l)]
    en_keys = [k for l in _i18n_lines[_en0:_en1] for k in _key_re.findall(l)]
    # H1. zh/en 键集合 parity
    if set(zh_keys) != set(en_keys):
        add("P2", "H1 词典parity", "zh/en 键不一致——zh 独有: %s；en 独有: %s"
            % (sorted(set(zh_keys) - set(en_keys))[:5], sorted(set(en_keys) - set(zh_keys))[:5]))
    elif len(zh_keys) != len(set(zh_keys)):
        add("P2", "H1 词典重复键", "词典内存在重复键（后者静默覆盖前者）")
    else:
        print("  ✓ 词典 parity：zh/en 各 %d 键完全一致" % len(zh_keys))
    # H2. 引用完整性：data-i18n* 属性 + t()/t_() 调用 + 键形态字符串（REGS/REGFILE 等间接引用）
    #     全命中词典；'pack.kind.' 是动态前缀（t('pack.kind.'+kind)），单独核对三个后缀
    _ref_re = re.compile(r"'((?:chip|tab|st|pipe|cfg|reg|dlg|voice|cr|meta|cov|beat|eff|lane|sub|nar|mus|lib|pk|pack|cut|view|pulse|msg|def|src)\.[\w.]+)'")
    refs = {m.group(1) for m in _ref_re.finditer(html)}
    refs |= set(re.findall(r'data-i18n(?:-html|-ph|-t)?=\\?"([\w.]+)', html))
    dyn = {r for r in refs if r.endswith(".")}
    missing = sorted(r for r in refs - set(zh_keys) if r not in dyn)
    if missing:
        add("P2", "H2 引用缺失", "%d 个 i18n 引用不在词典: %s" % (len(missing), missing[:8]))
    else:
        print("  ✓ 引用完整性：%d 个静态引用全部命中词典" % len(refs))
    for sfx in ("image", "audio", "video"):
        if "pack.kind." + sfx not in zh_keys:
            add("P2", "H2 动态键", "pack.kind.%s 缺失（packCard 动态拼接 t('pack.kind.'+kind) 依赖）" % sfx)
    # H3. en 词典残留中文（漏翻）——值为中文句子的 key 列出来人工判
    _val_re = re.compile(r"'([\w.]+)':\s*'((?:[^'\\]|\\.)*)'")
    _cjk_left = []
    for l in _i18n_lines[_en0:_en1]:
        for m in _val_re.finditer(l):
            if re.search(r"[一-鿿]", m.group(2)):
                _cjk_left.append(m.group(1))
    if _cjk_left:
        add("P3", "H3 en残留中文", "en 词典 %d 个值含中文（漏翻或有意保留？）: %s" % (len(_cjk_left), _cjk_left[:6]))
    else:
        print("  ✓ en 词典无中文残留")


# ═══════════ I. 人机等价三向对账 ═══════════
sec("I. 人机等价三向对账（路由 ↔ 前端 ↔ 手册）")
# 缘起（2026-10-19）：等价审查靠人肉三向映射，功能一多就漂移。钉死规则——
# 前端调不存在的路由=P1；路由两端都无影=P2 孤儿；路由没进手册=P2 Agent 侧等价缺口。
_st = studio  # studio.py 全文（A 节已读）
_idx = open(os.path.join(ROOT, "studio", "index.html"), encoding="utf-8").read()
_man = open(os.path.join(ROOT, "剪辑Agent操作手册.md"), encoding="utf-8").read()
_routes = set(re.findall(r'u\.path\.startswith\("(/api/[a-z0-9_-]+)', _st)) | \
            set(re.findall(r'u\.path == "(/api/[^"]+)"', _st))
_routes = {r.strip("/").split("/")[1] for r in _routes}
_fe = set(re.findall(r"fetch\(\s*['\"`]/api/([a-z0-9_-]+)", _idx))
_doc = set(re.findall(r"/api/([a-z0-9_-]+)", _man))
for r in sorted(_fe - _routes):
    add("P1", "I1 等价", "前端 fetch /api/%s 但 studio.py 无此路由" % r)
for r in sorted(_routes - _fe - _doc):
    add("P2", "I1 等价", "路由 /api/%s 前端无入口、手册未载——孤儿或隐藏 Agent-only 面" % r)
for r in sorted(_routes - _doc):
    add("P2", "I1 等价", "路由 /api/%s 未进操作手册——Agent 侧等价缺口" % r)
if not (_fe - _routes) and not (_routes - _fe - _doc) and not (_routes - _doc):
    print("  ✓ 三向对账闭合：路由 %d / 前端 %d / 手册 %d" % (len(_routes), len(_fe), len(_doc)))


# ═══════════ 汇总 ═══════════
sec("汇总")
p1 = [x for x in issues if x[0] == "P1"]
p2 = [x for x in issues if x[0] == "P2"]
p3 = [x for x in issues if x[0] == "P3"]
print("P1=%d  P2=%d  P3=%d" % (len(p1), len(p2), len(p3)))
sys.exit(1 if p1 else 0)
