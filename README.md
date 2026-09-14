# auto-cut v3 · 装修口播短视频流水线

**一句话**：把一批装修工地的手机实拍素材，变成一条带卡拉OK字幕、BGM、转场的竖版口播短视频。人（浏览器 Studio）和 Agent（CLI/HTTP）双端同权操作，全程文件传递、每个节点可独立失败。

**业务目标**：Agent 全自动从原始素材到成片；人是规则制定者（改 config 阈值/策略）+ 成片终审，不进流水线当卡点。验收：换新素材包冷启动，全程零人工急救。

## 管线全景（给 Agent 的系统地图）

| # | 环节 | 模块 | 产物 | 下游消费 | 状态 |
|---|---|---|---|---|---|
| 1 | 转写 | transcribe.py | pack.json（词轨+文本） | 理解/起草/词轨 | 自动✓ |
| 2 | 理解 | understand.py | digest（场景/资产/定位） | 起草 | 自动✓ |
| 3 | 缺陷台账 | disposition.py | disposition.json（四类缺陷+建议窗口） | 起草提示词/素材卡⚠ | 自动✓（2026-09-14 M2） |
| 4 | 起草 | draft.py | storylines/*.json | 渲染 | 自动✓（消费缺陷台账） |
| 5 | 裁剪 | dubfit.py（v2-③ 落地） | dub 音频+验证报告 | 渲染 | 自动✓（包络定位→掐头掐尾→ASR 闭环，吸收 dubgate D1-D3） |
| 6 | 词轨 | vadwords.py | VAD 词轨 | 字幕/卡拉OK | 自动✓（2026-09-14 编排器挂载） |
| 7 | 渲染 | pipeline.py + video_gen | out-*.mp4 + subtitle.ass | 门禁/交付 | 自动✓（硬解优先：mac=videotoolbox/win=nvenc·qsv，回退 libx264） |
| 8 | 门禁 | qc(R1-R10) + dubgate + G1/G2t | qc-report + 门禁报告 | 交付裁决 | 自动✓（2026-09-14 M3：听觉/冻结/页同步入列） |
| 9 | 交付 | loudnorm + 发布命名 | 发布成片 | 人终审 | 自动✓ |

**管线编排器（2026-09-14 M1 落地）**：`autocut3/orchestrate.py` 从文件推导 14 环节状态（含 aigen 预留槽位），人/Agent 同一个入口——
```bash
python3 autocut3/orchestrate.py myproj              # 看状态（14 环节 ✓/○/✗/· + 下一动作）
python3 autocut3/orchestrate.py myproj --advance    # 推进到下一道门（素材段全自动；故事线门需 --intent；mount 是创作决定门）
```
Studio 顶部管线进度条与 `/api/status/<proj>` 同源——Agent 推进，人随时看得见。

**人机契约**：缺陷处置是技术修复决策（判据量化：dup 指纹/能量包络/静音阈值），Agent 按规则全自动执行并留痕，**不设事前人工确认**；人拥有规则制定权（改 config）和成片终审权（可选）。

## 硬规则（踩坑提炼——Agent 动手前必读，违反=事故重演）

1. **ASR 词级时间戳是推测值**（素材间漂移 0~3s、同一音频内不均匀）——禁止直接作裁剪锚/字幕时间源；时间源=vadwords.py 的 VAD 物理测量（silencedetect 语音段）
2. **词轨黑区**：拍摄口令/嘟囔 ASR 会漏转写——按"能量有语音、词轨无文本"检测并掐除（事故：口令"三二一走"进成片）
3. **concat 拼接必占位**：跳过的区间（静音洞等）必须补等长静音段，否则后段整体前移（事故：片尾提前 4s 无 BGM）
4. **序列化保真**：前端落盘必须透传未知字段——白名单重建=静默丢数据（事故：手工词轨全丢，字幕退化均分）
5. **dub 裁剪闭环**：裁出音频必须 dubgate 复检（首词=story 首字 / 末词含句尾 / 相似≥0.9），不过闸不渲染
6. **门禁三域**：文本域（G1 说了什么）+ 时间域（G2t 什么时候说的）+ 听觉域（响度/静音洞）——"文本对"≠"时间对"（事故：46% 字符错位>1s 照样过文本门禁）
7. **修复闭环**：修 A 暴露 B 是常态，门禁复跑到全绿才算收敛；修复必须带回归锚进 tests/e2e.py，没锚=没修完
8. **跨平台纪律（Mac/Windows）**：文件锁一律走 `autocut3/flock.py`（POSIX=flock / Windows=msvcrt，禁直接 import fcntl）；ffmpeg 解析走 `ffmpeg_path()`（env → bin/ffmpeg(.exe) → PATH，禁裸 `"bin/ffmpeg"` 相对路径）；渲染编码走 `hw_encoder()`（平台探测，软编 libx264 只做回退）

## 当前状态与进行中

- v3 稳定运行；维护者项目 v6 成片已交付（57.1s / 门禁三域过 / -17.1 LUFS）
- **2026-09-14 接管首批落地（M1-M5 全部完成）**：管线编排器（orchestrate.py + /api/status + 前端进度条）｜缺陷处置链（disposition.py 四类缺陷→起草消费→素材卡⚠）｜QC 双域补位（R7 听觉/R4 冻结/R10 页同步）｜前端收敛（试渲去硬编码/字幕样张真兑现/渲染实时进度+系统通知）｜资产策展（12 槽位爆款语义音效 + 4 条真实 BGM 替换占位，Mixkit 免费商用，许可与来源清单随包）｜跨平台（flock.py 跨平台锁 + 平台感知 ffmpeg/硬解编码链，Mac/Windows 双端）
- **2026-09-14 新素材包冷启动验收（v2 总验收）**：全新素材包 19 条 → 成片两条（aidraft 原声 40.0s / aidraft2 本地 TTS 配音 37.6s），QC deliverable 0 blocker，全程零会话内人工急救。冷启动实锤修复两处管线缺口：①draft LLM 正文截断（finish_reason=length 旧代码当成功交付 → extract_json 炸；现升档重试 T32 锚）②dub 配音超幕视频时长旧链 atrim 掐断句子+词轨越界 bleed 成 R5 字幕页倒置（现 A 轨自动延展盖满配音+owords 幕内钳制，T12 断言改盖满语义）
- **已知遗留**：本地 TTS（Audio8-TTS，Apache 2.0）音色库待按客户扩充（当前 narrator_default 一个）；aidraft2 成片 R6 响度 -27.7 LUFS 偏轻（warn 不拦交付，平台会自行归一，终审核对混音比例）
- 代码托管：GitHub 私有库（Noah 账号），config/services.json 已永久 gitignore（历史已清除，key 走环境变量或 api_key_file）

## 快速上手

### 0. 前置（一次性）
```bash
# ffmpeg 6.0+：mac 放 bin/ffmpeg，Windows 放 bin/ffmpeg.exe，或装进 PATH（libass/libx264 编译项必需）
bin/ffmpeg -version        # 验证（Windows: bin\ffmpeg.exe -version）
# 云端服务 key：config/services.json 填 api_key_env（环境变量名，推荐）或 api_key_file（指向 {"api_key":"..."} 的文件）
#   此文件已 gitignore 永不入库。ASR 可选 local-faster-whisper 免 key 本地转写
```
平台支持：macOS 与 Windows 双端（Python 3.10+，零 pip 依赖；本地 TTS 的 onnxruntime venv 是可选组件，见下文"本地 TTS"）。

### 0.5 本地 TTS（Audio8-TTS，可选组件，2026-09-14 接入）

默认配音供应商已切到本地 [Audio8-TTS-Preview-0.6B](https://huggingface.co/Audio8/Audio8-TTS-Preview-0.6B-ONNX-INT4)（Apache 2.0 开源，ONNX INT4 CPU 推理，~1GB 内存）——零 pip 依赖的核心管线不受影响，运行时独立装在 `~/.local/share/autocut3/audio8/`（venv + 模型权重），适配层 `autocut3/tts_audio8.py` 只经 HTTP(127.0.0.1:8024) 通信、服务按需自启。

```bash
# 一次性安装（模型 ~1GB，HF 被墙时脚本走 hf-mirror）：
#   git clone Audio8_TTS 到 ~/.local/share/autocut3/audio8/runtime，
#   python3.12 -m venv ~/.local/share/autocut3/audio8/venv，
#   pip install -r onnx_runtime/requirements.txt + tokenizers（上游漏标），
#   下载 ONNX-INT4 仓库全部 *.onnx(.data) 到 ~/.local/share/autocut3/audio8/model
python3 autocut3/tts_audio8.py status                    # 运行时/服务/音色三态
# 注册参考音色（零样本克隆硬契约：参考音频 0.5-30s + 逐字稿一字不差）
python3 autocut3/tts_audio8.py register <名字> <参考音频.wav> "<逐字稿>"
python3 autocut3/tts.py synth --text "..." --out x.mp3   # provider=local-audio8 走本地
```
换回云端：config/services.json 的 `tts.provider` 改回 `"minimax"`。

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
studio/index.html    # 前端单页（管线进度条/注册表抽屉/渲染实时进度+系统通知）
autocut3/            # 流水线模块：orchestrate(编排器) transcribe(转写) understand(画面)
                     #   draft(AI起草) disposition(缺陷台账) pipeline(成片) video_gen(AI生成口)
                     #   qc(质检R1-R10) proofread(校对) vadwords(VAD词轨) dubfit(配音裁剪) dubgate(配音门禁)
                     #   dossier(素材档案) flock(跨平台锁) tts(配音合成)
registry/            # 注册表：效果系统的单一事实源（*.json 纯数据）
                     #   bgm/subtitles/transitions/sfx/stickers=效果；enums/formats=语义枚举(只读)
assets/              # 效果素材本体（registry 的 file 字段指向这里）
materials/packs/     # 用户素材仓（大文件，不入 git）
projects/<name>/     # 项目工作区：storylines/ 故事线、out-*.mp4 成片、qc-report.json
config/              # services.json(服务key) lexicon.json(校对词表) qc_rules.json
docs/                # 设计文档（pipeline-v2.md=管线改造立项）
tests/               # e2e.py(39项全量，含 T27 编排器/T28 缺陷台账/T29 QC双域/T30 声纹/T31 dubfit) rmw_smoke.py(并发) audit.py(代码审计器)
bin/                 # ffmpeg 6.0+（自备，不入 git）
```

## 注册表（效果系统）

registry/*.json 是人和 Agent 共用的"选什么效果"的唯一事实源。管理入口：首页效果卡片 → 管理抽屉（试听/看图/字幕样张/增删改/导入）。
Agent 起草时读同一张注册表选 BGM/转场；字幕样式、贴纸、音效按 id 引用。**改注册表即改下一次渲染，无需动代码。**
音效=12 槽位社交媒体爆款语义（转场嗖/强调击打/提示叮/悬疑渐强/喜剧弹弓/倒计时/快门/成功短奏/错误蜂鸣/低频轰击/弹出泡泡/尴尬蟋蟀），全部 Mixkit 免费商用，来源与许可存 `assets/sfx/viral/sources-manifest.json` + `LICENSE-mixkit.txt`，终审试听备选同清单。

## 单节点独立运行

每个模块都能单独跑（输入=文件，输出=文件）：
```bash
python3 autocut3/transcribe.py myproj --material M0211   # 只转写一条
python3 autocut3/understand.py  myproj --material M0211   # 只识别一条
python3 autocut3/vadwords.py    myproj --story main       # VAD 词轨重生成
python3 autocut3/dubfit.py      myproj --story main       # 配音裁剪自动化（dub 幕）
python3 autocut3/dubgate.py     myproj --story main       # 配音裁剪门禁
python3 autocut3/proofread.py   myproj --dry              # 校对预演不落盘
python3 tests/e2e.py                                       # 39 项全量回归
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
| 字幕与语音错位 | 词轨来源是否 VAD（narration.words）；素材词轨平移方案已废弃（时间戳漂移） |
| 配音开头有残留杂音 | dubgate 是否跑过；音频是否掐头（按能量包络 0.1s 级定位，不信 ASR 词轨） |
| 改了代码不生效 | **studio.py 是常驻进程，改完必须重启**（历史上两次"改了没生效"都是模块缓存） |
| 中文素材引用 400 | 旧版本残留——确保跑在含 `_safe_id` 的版本（git log 有"中文id 全放行"提交） |

## 测试与质量

```bash
python3 tests/e2e.py      # 39 项端到端（上传/起草/渲染/并发/安全），ALL GREEN 是交付底线
python3 tests/audit.py    # 七维审计：路由安全/数据断链/JS函数对照/文档时效/git卫生
python3 tests/rmw_smoke.py# 读-改-写并发原子性
```
