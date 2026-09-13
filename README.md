# auto-cut v3 · 装修口播短视频流水线

**一句话**：把一批装修工地的手机实拍素材，变成一条带卡拉OK字幕、BGM、贴纸、转场的竖版口播短视频。人（浏览器 Studio）和 Agent（CLI/HTTP）双端同权操作，全程文件传递、每个节点可独立失败。

## 快速上手

### 0. 前置（一次性）
```bash
# ffmpeg 6.0+ 放进 bin/（或改 PATH；libass/libx264 编译项必需）
bin/ffmpeg -version        # 验证
# 云端服务 key 填入 config/services.json（ASR/视觉/LLM 目前用 minimax；也可 local-faster-whisper，见 requirements.lock.txt）
```

### 1. 启动 Studio（人侧）
```bash
python3 studio.py                 # http://127.0.0.1:8765
```

### 2. 全链路走一遍（Agent 侧，与上面等价）
```bash
# ① 建包 + 上传素材（中文文件名 OK；id 自动提取 M 编号）
curl -X POST localhost:8765/api/pack-create/ -d '{"id":"mypack","name":"我的素材"}' -H 'Content-Type: application/json'
curl -X POST "localhost:8765/api/pack-upload/mypack?filename=20260827_M0211.MP4" --data-binary @素材.MP4

# ② 建项目 + 挂载
curl -X POST localhost:8765/api/project-create/ -d '{"name":"myproj","title":"我的项目","format":"vertical"}' -H 'Content-Type: application/json'
curl -X POST localhost:8765/api/pack-mount/myproj/mypack/mount

# ③ 转写 + 画面识别（单素材走 HTTP ?material=M0211；全量走 CLI）
python3 autocut3/transcribe.py myproj
python3 autocut3/understand.py myproj

# ④ AI 起草故事线（读 dossier，产出 storylines/aidraft.json）
curl -X POST localhost:8765/api/draft/myproj -d '{"intent":"装修避坑干货口播"}' -H 'Content-Type: application/json'

# ⑤ 渲染成片
curl -X POST "localhost:8765/api/render-start/myproj?story=aidraft"
# 产物：projects/myproj/out-aidraft.mp4 + subtitle-aidraft.ass + qc-report.json
```

## 目录结构

```
studio.py            # 后端全部路由（人/Agent 同权 HTTP）
studio/index.html    # 前端单页
autocut3/            # 流水线模块：transcribe(转写) understand(画面) draft(AI起草)
                     #   pipeline(成片) video_gen(渲染) qc(质检) proofread(校对) ...
registry/            # 注册表：效果系统的单一事实源（*.json 纯数据）
                     #   bgm/subtitles/transitions/sfx/stickers=效果；enums/formats=语义枚举(只读)
assets/              # 效果素材本体（registry 的 file 字段指向这里）
                     #   music/ sfx/ stickers/ fonts/（三款中文字体）
materials/packs/     # 用户素材仓（大文件，不入 git）
projects/<name>/     # 项目工作区：storylines/ 故事线、out-*.mp4 成片、qc-report.json
config/              # services.json(服务key) lexicon.json(校对词表) qc_rules.json
tests/               # e2e.py(30项全量) rmw_smoke.py(并发) audit.py(代码审计器)
bin/                 # ffmpeg 6.0+（自备，不入 git）
```

## 注册表（效果系统）

registry/*.json 是人和 Agent 共用的"选什么效果"的唯一事实源。管理入口：首页效果卡片 → 管理抽屉（试听/看图/字幕样张/增删改/导入）。
Agent 起草时读同一张注册表选 BGM/转场；字幕样式、贴纸、音效按 id 引用。**改注册表即改下一次渲染，无需动代码。**

## 单节点独立运行

每个模块都能单独跑（输入=文件，输出=文件）：
```bash
python3 autocut3/transcribe.py myproj --material M0211   # 只转写一条
python3 autocut3/understand.py  myproj --material M0211   # 只识别一条
python3 autocut3/proofread.py   myproj --dry              # 校对预演不落盘
python3 tests/e2e.py                                       # 30 项全量回归
python3 tests/audit.py                                     # 代码审计器
```

## 常见失败排查

| 症状 | 先看什么 |
|---|---|
| 上传 Broken pipe / 000 | materials/packs/<包>/ 目录是否存在（v3 已自动兜底建）；文件是否 >500MB |
| "bad filename" | 文件名含 `/` `\` `..` 或以 `.` 开头（中文名是允许的） |
| 转写 401/超时 | config/services.json 的 key；minimax 云端限制 ≤50MB/≤500s |
| 渲染卡在 ffmpeg | projects/<proj>/render.status 的 tail 字段有最后 8 行 stderr |
| 字幕不换页/叠字 | subtitle-*.ass 是否生成；words 词轨是否为空（转写失败会让字幕静默消失） |
| 改了代码不生效 | **studio.py 是常驻进程，改完必须重启**（历史上两次"改了没生效"都是模块缓存） |
| 中文素材引用 400 | 旧版本残留——确保跑在含 `_safe_id` 的版本（git log 有"中文id 全放行"提交） |

## 测试与质量

```bash
python3 tests/e2e.py      # 30 项端到端（上传/起草/渲染/并发/安全），ALL GREEN 是交付底线
python3 tests/audit.py    # 七维审计：路由安全/数据断链/JS函数对照/文档时效/git卫生
python3 tests/rmw_smoke.py# 读-改-写并发原子性
```
