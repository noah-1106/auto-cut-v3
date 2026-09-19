# auto-cut v3 · 剪辑 Agent 操作手册

> 给执行剪辑任务的 Agent 的**唯一操作入口文档**。读完再动手。
> 本文每一条命令、字段、阈值都对照代码核实（2026-09-16），与 README（系统地图）、
> CLAUDE.md（约束+事故清单）配套。三者冲突时以本文的操作细节为准、以 CLAUDE.md 的约束为纲。
>
> **零容忍纪律**（违反即事故）：
> 1. **授权闸门**：雇主已建好的 job/工作流 = 唯一执行通道。禁止自建平行 job，禁止"先跑起来再说"。
> 2. **崩溃必须留痕**：任何一步 error，续作第一件事是写事故记录（时间点/最后产物/最后动作），不允许 error+null 裸奔。
> 3. **源素材零写入**：`materials/packs/<pid>/` 里的 MP4/JPG 原片禁止任何写操作（管线自身审计字段除外）。
> 4. **说"done"必附证据**：状态声明必须引用文件/命令输出，不接受"应该好了"。

---

## 1. 核心概念模型（先建立，再动手）

```
素材包 pack          项目 project              故事线 storyline
materials/packs/     projects/<pid>/           projects/<pid>/storylines/<sid>.json
  <pid>/pack.json      materials/library.json    beats[] = 幕
  19 条 MP4+元数据     dossier.json 素材档案       每幕 = 口播词 + 画面轨 + 转场 + 效果
  （转写/视觉/digest）  disposition.json 缺陷台账    plan → ASS 字幕 → 成片 out-<sid>.mp4
```

**包与项目的归属关系**：包物理上全部在仓库级 `materials/packs/`（不存在项目级存储），
项目经 `library.json` 挂载，多对多。**命名约定 = 唯一归属信息**：给某项目导素材 →
包名 = `<项目名>`（或 `<项目名>-raw`），只挂这一个项目；通用素材（空镜/AI 生成/
共享 B-roll）才用通用名、可挂多项目。包列表是全库平铺的，起 `pack1`/`素材`/`test`
这类 generic 名 = 制造归属不明的垃圾。

**五个必须刻进脑子的认知：**

1. **幕（beat）是全轨一刀竖切的最小单元**。一条故事线 = 一串幕，每幕有：口播词（`story`）、
   A 轨画面（主画面）、可选 B 轨（画中画）、转场、贴纸/音效。
2. **`story` = 字幕文本（vadwords 铺轨）！** story 的契约是**可直接朗读的口播台词**
   （draft 契约原话），vadwords 把它按 VAD 语音段逐字铺成字幕词轨，成片字幕与 story
   一字不差（2026-09-16 对照代码与 pcln 产物实锤）：
   - **dub 幕**：story 被 TTS 逐字念出 → vadwords `text_from="story"` 直铺；
   - **original 幕**：铺**素材转写稿窗口切片**（pack.json `transcript.words`，已校对，
     内容与 story 同源一致）；窗口无词（空镜/未转写）才回退直铺 story。
   - **写成"总结腔分镜摘要"是事故**——它会被念出来/铺进字幕（vadwords 注释警告的
     正是这种失效模式，不是"story 与字幕无关"）。改 original 字幕 = 改素材转写
     （proofread 等长替换，保字符-时间对齐）；改 story/转写/窗口后必须重跑 vadwords。
3. **时间源=实测**（2026-09-18 勘误）：转写 tier=word（MiniMax timestamp_level=word 实测，
   与 VAD 物理测量互证 ±0.05s）时 vadwords 直取词时间；tier=vad/sent 是合成时间禁作锚，
   时间源只有 vadwords 的 VAD。**重转写新素材后自动走 word 直通道，别再手工修时间。**
4. **A/B 轨铁律**：A 轨=主画面，分三级——①正常口播素材随时可 A；②**空镜（ambient/broll）
   两级**：自带有效语音的（档案 `has_speech`，104030 型实拍画外音）可口播幕 A 轨用原声；
   无语音的只能进纯空镜幕（narration.mode=`none`，靠 BGM/环境音撑）或 B 轨叠画——
   **别默认只做画中画**；③meta 说戏/voiceover 读稿画面永远禁 A（穿帮）。B 轨=无叙事的
   画中画（**禁 dialogue/说话/口播/采访画面——B 轨没有音频通道，人物张嘴没声=哑口型
   穿帮**）。选段时强制（draft validate 硬分层剔除），渲染链不管内容对错。
5. **narration.mode 只有三种**：`original`（素材原声，默认）/ `dub`（AI 配音，audio 指向
   materials/dub/ 下的 mp3）/ `none`（无旁白）。**没有 "tts" 这个值**——TTS 只是 dub 的
   音频来源。
6. **幕数不设上限**（2026-09-17 Noah 决策，取消原 2-4 幕引导）：唯一标准是"用合理
   数量+质量的幕完整讲完故事"——每幕必须有独立叙事功能，凑数/重复幕宁可砍，讲不完
   就加幕（常见 2-8 幕）。validate 对任意幕数**原样全量校验、不截断**（基线曾有
   `[:4]` 静默丢弃第 5 幕起的 bug，2026-09-16 已修，T48 锚）；渲染链不限幕数。
   硬约束只剩两条：单幕 3-20 秒、总时长 20-90 秒。

---

## 2. 全流程地图：13 环节与 done 判据

编排器 `autocut3/orchestrate.py <project>` 从文件推导状态，人机同一判据。**先跑
`python3 autocut3/orchestrate.py <pid>` 看状态表，再决定干什么**；`--advance` 自动推进
（素材段 mount→disposition 自动；storyline 门二选一：`--intent` 触发 AI 起草，或手写
`storylines/<名字>.json` 落盘即过门；vadwords→qc 自动）。

| # | 环节 | done 判据（文件事实） | 通常谁跑 |
|---|------|----------------------|---------|
| 1 | mount | `materials/library.json` 的 packs 非空 | 人决策（挂哪个包），Agent 提议 |
| 2 | transcribe | 全部非 image 素材 `audit.transcript=="done"`（有 `error:*`=failed） | `transcribe.py` |
| 3 | understand | 全部 video/image 素材 `audit.visual ∈ {done, n/a}` | `understand.py` |
| 4 | digest | 每挂载包 `pack.json.digest` 存在 | understand 批处理自动 |
| 5 | proofread | 全部有词轨素材 `audit.proofread ∈ {done, auto-done}` + `review-queue.json` 无 `status=="pending"` 项 | `proofread.py` + 人复核队列 |
| 6 | dossier | `projects/<pid>/dossier.json` 存在 | `dossier.py` |
| 7 | disposition | `disposition.json` 存在且新于其消费的 pack.json | `disposition.py` |
| 8 | storyline | `storylines/*.json` 非空 | `draft.py --intent`（LLM）或人/Agent 手写落盘 |
| 9 | vadwords | 当前故事线每幕 `narration.words` 在场（mode=none 除外） | `vadwords.py` |
| 10 | dubfit | 无 dub 幕=na；有则每幕 `narration.dubfit` 在场且 `dubfit-report.json passed==true` | `dubfit.py` |
| 11 | dubgate | `dubgate-report.json passed==true`（D1 首词/D2 末词/D3 相似≥0.9） | `dubgate.py` |
| 12 | render | `out-<sid>.mp4` 存在且新于故事线（或 render.status 在跑） | `pipeline.py render` |
| 13 | qc | `qc-report.json` 存在且 `verdict != "blocked"` | `qc.py`（渲染成功自动跑） |

---

## 3. 标准作业流程（SOP）：六阶段，每阶段有放行判据

### 阶段 1 · 接料立项（mount）
- 确认项目存在：`projects/<pid>/project.json`；没有则先建（Studio `POST /api/project-create/`）。
- 确认挂载：`projects/<pid>/materials/library.json` 的 `packs[]` 就是**唯一事实源**。
  挂包/卸包走 Studio `POST /api/pack-mount/<proj>/<pid>/mount|unmount`——**不要手编 library.json**。
- **核对任务书指定的包名**（挂错包=白转写一轮，还得两包逐字节对账）：挂载前确认包 id
  与任务书一致，发现预建项目挂错包先卸载重挂再开工。
- 清点：`pack.json.files[]` 数量与源目录一致；每条 MP4 大小与源一致；**源目录 mtime 前后各记一次**（零写入签字）。
- **竖拍方向核对（2026-09-18 补，手机素材普遍"编码横放 + rotation=-90"）**：逐条 MP4 读
  ffprobe `side_data_list[].rotation` 核对真实方向；**rotation≠0 的素材 `pack.json` 该条
  `geometry._rotation` 必须在场**（mount 走 video_meta.probe 自动落；手工挂载/复挂后人工核对）。
  缺 _rotation 又不横竖对账 = 后续画幅/裁切全错。
- **放行判据**：挂载 done + 清点一致 + 兜底假设（画幅/平台/时长/风格）已登记并注明依据。

### 阶段 2 · 素材审核（transcribe → understand → digest → proofread → dossier/disposition）
依次跑，每步核对 done 判据：
```bash
python3 autocut3/transcribe.py <pid>            # ASR 词轨+文本 → pack.json transcript
python3 autocut3/understand.py <pid>            # 视觉理解 → pack.json visual + digest
python3 autocut3/proofread.py <pid>            # 参数=项目名（与 transcribe 同构）；队列必落 projects/<pid>/
python3 autocut3/dossier.py <pid>               # 素材档案（LLM 读全量转写+视觉合成）
python3 autocut3/disposition.py <pid>           # 缺陷台账（重说/黑区/音量跳变等）
```
- 手工跑 ASR（绕过 transcribe.py，仅排障时）须**先抽 16k 单声道 wav** 再送：
  `asr.extract_audio(src, wav)` 现成（管线内转写自动做，asr.py；手工直送原始 MP4 = ASR 服务端拒/慢）。
- **素材"过审"的自动规则**：该素材 kind 所需审计全 done → `files[].review` 自动
  `"pending-review"→"reviewed"`。draft 只会用过审素材；人保留废片否决权
  （Studio `POST /api/pack/<pid>/<fid>/usable`，body 可置 `usable:false`/`review`）。
- **proofread 两道门**：① `audit.proofread` 只有 `done`/`auto-done` 算过，
  `pending`（上传初值）一律是**没校对**，编排器现在严格判定（T47）；② 语义存疑的修正进
  `projects/<pid>/review-queue.json`（`status:"pending"`），**有人复核改状态前环节不推进**（T43）。
  → **跑完必须读 review-queue.json**；非空=有人工活在等你汇报雇主，不许静默继续。
- **放行判据**：orchestrate 状态表素材段全绿 + review-queue 已清 + dossier/disposition 已产出。

### 阶段 3 · 故事线供纲（draft）
```bash
python3 autocut3/draft.py <pid> --intent "<一句话意图>" --save
# 配音版（整线 TTS）：python3 autocut3/draft.py <pid> --intent "..." --save --dub
```
- `--intent` 必填（AI 起草路线）。不加 `--save` 只打印不落盘。
- **手写路线**：故事线门也接受人/Agent 手写——落盘 `storylines/<名字>.json`
  （Studio 创作台 / `POST /api/storyline` / 直接写文件）即过门，编排器不问出处；
  手写线同样吃 vadwords→dubfit→渲染全链。大纲/意图想结构化传给 LLM 的目前只有
  `--intent` 一句话自由文本一条通道（outline 是 LLM 输出字段，不是输入）。
- **配音音色**：`--dub` 逐幕调 `tts.synth(story)`，音色取 services.json `tts` 段配置
  （本地 audio8 默认注册音色 `narrator_default`）。换音色=先注册参考音色再改配置：
  `python3 autocut3/tts_audio8.py register <名字> <参考音频.wav> "<逐字稿>"`——
  **硬契约：参考音频 0.5–30s 且逐字稿一字不差**，否则克隆出来的是哑嗓子；
  注册后 `tts.py synth --text "..." --voice <名字>` 验证音色可用再跑 draft --dub。
  云端 minimax 同理走 `tts` 段 provider 切换。
- 产物：`storylines/aidraft.json`（beats[]）。**LLM 可能截断**（finish_reason=length）——
  落盘后必须逐幕核对结构完整（每幕有 no/story/tracks/narration），缺=重跑，不许带伤前进。
- **后台跑（nohup/超时后台执行）判完成看文件，不看进程、不看日志空满**：
  判据=`storylines/aidraft.json` 存在且逐幕结构完整（2026-09-18 起日志已行级 flush，
  起草开始/`DRAFT DONE` 会实时落日志；但完成信号以文件为准——进程残留≠还在干活，
  日志 0 字节=缓冲未刷≠死进程）。**不许 sleep 轮询进程存活**，文件到了就进阶段 4。
- draft 提示词已内置硬规则（content_type 选段门、效果注册表白名单、旁白词汇表），但
  **LLM 输出必须人工/Agent 全面检查**（阶段 4）。
- **放行判据**：故事线落盘 + 结构完整 + 你已完成阶段 4 的逐幕审核。

### 阶段 4 · 故事线全面检查 + 逐幕审核（最容易被跳过，雇主的核心里程碑）
**不许"故事线写完直接渲染"**。按此清单逐幕过：
1. **逐幕读 story**：是可直接朗读的口语吗？≤120 字？有没有总结腔/书面腔？
   **审 aidraft 只认这三个字段的层次**：`beats[].story`=口播台词/分镜（你要审的）；
   `beats[].tracks`=画面取段；`narration.words`=字幕词轨（vadwords 产物，只看不编）。
   故事线里**没有 "text" 字段**——"text" 是素材层 `pack.json transcript.text`
   （原始转写全文），别在故事线里找它，也别把三层混着改（改台词=dub 改 story /
   original 改转写，改时间=重跑 vadwords，见阶段 4.5）。
2. **逐幕读 tracks**：A 轨 source_id 在 pack.json 里存在吗？`src_in`+`duration` 在素材时长内吗？
   B 轨有没有说话画面（desc 含对话/口播/采访…）？content_type 合规吗？
3. **narration.mode 与意图一致吗**（原声线全 original；配音线是 dub 且 audio 文件存在）？
4. **效果与转场**：effects.stickers/sfx ≤2 个且 asset id 在注册表里？贴纸带 `text` 短语吗
   （**≤6 字、不写无意义词、不复述原字幕里的词**——是从本幕口播里蒸馏的强调短语，
   照抄整句或超 6 字渲染时被剔除=白加）？transition_out 在 transitions.json 里？
5. **幕试渲（beat preview）——单幕独立验证，全片过≠单幕过**：
   ```bash
   python3 autocut3/pipeline.py beat <pid> <幕号> [sid]
   # 产物 projects/<pid>/previews/beat_<sid>_<幕号>.mp4（540p 降质提速版）
   ```
   或 Studio `GET /api/beat/<proj>/<幕号>?story=<sid>`。逐幕看：画面对吗？字幕跟人声同步吗？
   静音洞/截半句吗？**有问题改 storylines/<sid>.json 再试渲该幕**，不要直接全片渲染。
- **放行判据**：每幕审过 + 重点幕试渲看过 + 修改已落盘。

### 阶段 4.5 · 改稿 SOP（手改 storylines/<sid>.json 之后必走）

```
手改 JSON → 按需重跑 vadwords → make 预检 → beat 试渲改过的幕 → render
```

1. **先判改动类型，决定要不要重跑 vadwords**（字幕词轨是派生数据，源变了不重跑=旧字幕）：

   | 改了什么 | 重跑 vadwords？ | 理由 |
   |---|---|---|
   | 素材转写文本（proofread 修正） | **必须** | original 幕字幕文本=转写拼接（vadwords.py 选词源） |
   | `story`（dub 幕台词） | **必须** | dub 幕字幕=story 字符铺 VAD 段 |
   | A 轨 `src_in` / `duration`（窗口） | **必须** | 窗口决定哪些词/哪些语音段进幕 |
   | A 轨 `source_id`（换素材） | **必须** | 词轨来源整个换掉 |
   | `narration.mode` original↔dub↔none | **必须** | 词轨生成路径不同 |
   | 转场 / 贴纸 / 音效 / 字幕样式 / BGM / 封面 / title/outline | **不用** | 不消费 narration.words |
2. `python3 autocut3/pipeline.py make <pid> <sid>`——plan+ASS 能生成=结构没坏（字段名错/
   素材缺在这里报错，比渲染到一半炸便宜得多）。
3. 改过的幕**逐个** `pipeline.py beat <pid> <幕号> <sid>` 试渲确认；没改动的幕不用重看。
4. 全绿 → render。只改了转场/样式这类，跳过 vadwords，make 过即可直接 render。

### 阶段 5 · 渲染（render）
前置（全满足才渲）：素材段全绿 → review-queue 空 → 故事线审核完 → vadwords 已跑
（词轨在场）→ dub 线 dubfit/dubgate 全过。
```bash
python3 autocut3/pipeline.py render <pid> [sid]     # 全片；产物 out-<sid>.mp4
python3 autocut3/pipeline.py make  <pid> [sid]      # 只产 plan+ASS 不渲（预检）
```
- 渲染成功尾部**自动跑 QC**；进度看 `projects/<pid>/render.status`
  （running/stage/pct/tail）。Studio `GET /api/render-progress/<proj>?story=<sid>` 同源。
- 渲染中途失败：读 render.status 的 tail 找 ffmpeg 报错；**改完输入重渲，不要手动拼视频**。
- **转场微调旋钮**（Studio 渲染 URL 的 query 参数，写入 `projects/<pid>/params.json`，
  渲染时覆写本故事线用到的转场模板参数，不动注册表）：
  | 旋钮 | 类型 | 作用（pipeline.py 消费点） |
  |---|---|---|
  | `duration` | 秒(float) | 转场过渡时长（xfade duration；注册表模板默认值可被它临时覆盖） |
  | `hold` | 秒(float) | flash 型转场的白闪帧时长 |
  | `grain` | 强度(int) | flash 型转场白帧的噪点强度（ffmpeg noise alls 值） |
  | `flash` | 亮度(float) | flash 转场窗口内的亮度增益峰值（flash_peak，正弦包络） |

  例：`POST /api/render-start/<proj>?story=<sid>&duration=0.6&grain=40`。只对**本故事线
  实际用到**的转场 id 生效；params.json 长期存在，想恢复模板默认就删掉该文件或重发空参数。

- **转场预留区（2026-09-18 起，build_plan 自动做，Agent 不用手工留边）**：非 flash 转场的
  时长按边界两侧可用静音自动伸缩（`dt_eff = min(注册 dt, 尾侧静音, 头侧静音)`），出点/入点
  自动延到词尾/首词外；两侧静音不足 0.2s 的边界自动降级直切（渲染日志可见 `降级直切`/`伸缩`/
  `头预留`/`尾预留` 行）。推论：①`word_drops` 应恒空——再出现"落入转场重叠区被钳没"警告=
  窗口数据有 bleed（owords 超窗），修故事线窗口而不是调转场；②params.json 的 `duration` 只是
  上限，实际转场时长仍受边缘静音钳制；③时长预算：渲染总长 ≈ 素材合计 + Σ转场时长（每边界
  +0.2~0.6s），90s 帽下素材窗口合计控制在 ~86s 内。

### 阶段 6 · 质检与交付（qc）
```bash
python3 autocut3/qc.py <pid> --story <sid>
```
- 产物 `qc-report.json`，verdict 三档：`deliverable`（可交付）/ `fix-then-deliver`
  （有 blocker 但可修——改故事线重渲）/ `blocked`（人/unfixable，必须人介入）。
- R1–R10 规则速查（config/qc_rules.json）：R1 死尾巴≤0.8s / R2 尾音≥0.25s /
  R3 静音洞≤0.8s / R4 冻结帧<2s / R5 字幕页≥0.5s / R6 响度锚 -16±3 LUFS /
  R7 听觉（洞>1.5s、相邻段差>8dB）/ R8 填充词 7 个+呃嗯 / R9 重说（4 字 ngram×12s 窗）/
  R10 页同步偏移≤0.5s。**warn 不拦交付但要在交付说明里如实列出**。
- **suggested_fix 的定位＝参考答案**（默认起点，非指令）：可偏离，但偏离要在交付说明记一句原因；
  最终裁决是复检 QC verdict，不是"是否采纳了建议"。R1/R2 的建议值已含转场重叠避让
  （末词存活下限=末词起点+转场时长+0.08s，ruxuan-02 实锤：机械收缩会钳没尾词→字幕页跨幕合并），
  采纳前仍建议跑 vadwords 重铺词轨并**比对词数/页数**——改窗口后词分配会整体重排。
  渲染日志出现「落入转场重叠区被钳没」警告 = 有词被丢，先查再交（plan-<sid>.json `word_drops` 有明细）。
- **交付判据**：QC deliverable（或 fix-then-deliver 已修重跑到 deliverable）+ 成片存在 +
  交付说明含：成片路径/时长/响度、QC verdict、warn 清单、对雇主兜底假设的确认。

### 阶段 6.5 · 封面收口（cover）——渲染前闸门
**位置（2026-09-18 Noah 裁定）：封面生成+检查在渲染之前。** 封面槽
`projects/<pid>/cover/cover-<sid>.jpg` 在盘、新于故事线、体检过（竖版/非空白）才放行渲染
（编排器 cover 环节 / render 内建闸门，pipeline.cover_check）。唯一例外：output-frame
（成片抽帧天然后置）。单独出封面不耗渲染：`python3 autocut3/pipeline.py cover <pid> <sid>`
（Studio「出封面」按钮同一入口）。

**策略矩阵**（enums.json `cover_strategies`，故事线 `meta.cover` 选）：

| strategy | 怎么出图 | 适用 |
|---|---|---|
| `first-frame`（默认） | 首幕素材入点帧 + 标题合成 | **含真人脸封面的唯一保真路线** |
| `beat-frame` | 指定幕指定时刻（`beat_no`+`at`）+ 标题合成 | 代表帧选人脸正脸/表情最好的时刻 |
| `ai-generated` | **代表帧底图**（`beat_no`+`at`，缺省首幕）+ `prompt` 走 image-01 保主体生成 + 标题合成 | **空镜/B-roll 场景封面**（⚠ 底图含真人脸会被平台审核拦 1026，实测与提示词无关） |
| `output-frame` | 渲染后成片抽帧（`at` 秒） | 封面=发布本体，含字幕全要素（唯一后置） |
| `upload` | 外部图放 `cover/upload.jpg`（或 .png），可选标题 | 设计稿 |

**封面标题**：`meta.cover.title_text` = 核心故事标题（一句话钩子）。任何策略下非空即由管线
**本地 drawtext 确定性合成**到画面上部（白字黑描边、≤10 字/行自动折行、1080×1920 收口）——
**禁让 AI 在图里画文字**（image-01 中文字形=假字高危，2026-09-18 封面曾中招同类）。

**AI 封面提示词写作建议**（`meta.cover.prompt`，样张接口 `POST /api/cover-gen/<proj>`
body `{prompt, beat_no, at}` 出 cover/sample.jpg 预览，满意再保存正式出）：
1. 开头固定写「**保持参考图主体/场景不变**」——这是保主体生成的锚，缺了会面目全非
2. 只写**风格方向**：色调 / 光线 / 氛围 / 构图（如「暖色自然光，背景轻微虚化」）
3. 注明「**上半部分留白**」——标题由管线合成在上部，别让 AI 把画面填满
4. **禁写**「画面中出现文字 / 标题 / 水印」——AI 画中文=假字，标题走 title_text
5. **禁写**改变人物身份的词（换人/变老/变性别）——且真脸底图本就会被平台拦截，AI 策略只用于空镜
6. 中文直写即可（image-01 中文理解强），≤1500 字，短句列表优于长段落

示例（空镜底图）：`保持参考图场景不变，室内装修工地，暖色自然光，画面干净通透，背景轻微虚化，上半部分留白`

任何途径最终归一到 `cover-<sid>.jpg`（pipeline.build_cover 收口）；验收封面就看这张。

---

## 4. 工具卡全集（命令 / 参数 / 产出 / 何时用）

> Windows 上 `python3` 一律换 `python`（本文按 macOS/Linux 书写）；ffmpeg 同理用 `bin\ffmpeg.exe`。

### 4.1 素材段
> **首选 `process.py`（素材级流水线）**：每条素材 ASR 一完成立即链发该素材的画面识别（词轨随行），
> ASR 池 3 线程 + 视觉池 3 线程双端点并发（2026-09-18 实测 MiniMax 3+3 无 429）——墙钟
> ≈max(转写总量,视觉总量) 而非两段求和。`orchestrate --advance` 的转写步内部就走它。
> **同一包两步别各起进程同时跑**：两步共持 `packs/<pid>/.lock` 整程独占，同起=后起方
> 阻塞在锁上干等，零提速白挂进程。**"同时开跑"只对不同包有意义**（不同包不同锁）。
> transcribe.py / understand.py 保留作分步重试（--force / --material 单条），限流自动退避
> （429 等 2/4s 重试，不是卡死）。

| 命令 | 参数 | 产出 | 何时用 |
|---|---|---|---|
| `process.py <pid>` | `--pack --material --asr --vision --force` | transcript + visual 一次落（**流水线：ASR 完一条立即链发视觉**） | **新素材入库首选**；advance 转写步内部走它 |
| `transcribe.py <pid>` | `--pack --material --asr --force` | pack.json transcript + audit.transcript | 只重跑转写段；--force 重跑已转写的 |
| `understand.py <pid>` | `--pack --material --vision --force` | pack.json visual + digest | 只重跑视觉段/补 digest；批处理自动 build_digest |
| `proofread.py <项目> [--pack X]` | `--dry --material --pack` | 词轨修正写回 pack.json + 复核队列（**固定落 `projects/<项目>/review-queue.json`**，与门禁同目录） | 转写后必跑；--dry 预演不落盘；--pack 限定项目内某包 |
| `dossier.py <pid>` | 无 | `projects/<pid>/dossier.json` 素材档案 | 理解后；draft 的提示词原料 |
| `disposition.py <pid>` | 无 | `projects/<pid>/disposition.json` 缺陷台账 | dossier 后；draft 档案行会带⚠ |

### 4.2 创作段
| 命令 | 参数 | 产出 | 何时用 |
|---|---|---|---|
| `draft.py <pid>` | `--intent（必填）--packs --save --dub` | storylines/aidraft*.json | 素材段全绿后；--dub 逐幕 TTS→materials/dub/<sid>-b<n>.mp3 |
| `vadwords.py <pid>` | `--story <sid>` | 每幕 narration.words 写回故事线 + vad-report.json | 故事线定稿/修改后必跑（字幕时间源） |
| `dubfit.py <pid>` | `--story` | materials/dub/fit-*.mp3 + dubfit-report.json | dub 线配音裁剪闭环；不过 exit 1 |
| `dubgate.py <pid>` | `--story` | dubgate-report.json | dub 线门禁 D1-D3；不过 exit 1 |

### 4.3 渲染段
| 命令 | 参数 | 产出 | 何时用 |
|---|---|---|---|
| `pipeline.py make <pid> [sid]` | — | plan-<sid>.json + subtitle-<sid>.ass | 渲前预检（不耗渲染） |
| `pipeline.py render <pid> [sid]` | — | out-<sid>.mp4（+自动 QC） | 阶段 5 前置全满足后。**跨进程互斥**：报"已有渲染进行中"=另一进程（CLI/Agent/另一 Studio 实例）在渲同项目——等它完成或 `ps` 对账，别强行重试（2026-09-18 并发加固） |
| `pipeline.py beat <pid> <幕号> [sid]` | — | previews/beat_<sid>_<幕号>.mp4 | 阶段 4 逐幕审核 |
| `qc.py <pid>` | `--story --no-deep` | qc-report.json | 渲后必跑（渲染已自动跑，人复核读报告） |

### 4.4 编排器
```bash
python3 autocut3/orchestrate.py <pid>                 # 状态表（13 环节 done/pending/failed/na）
python3 autocut3/orchestrate.py <pid> --json          # JSON 版（程序消费）
python3 autocut3/orchestrate.py <pid> --advance       # 自动推进（storyline 门：--intent AI 起草 / 手写落盘过门）
python3 autocut3/orchestrate.py <pid> --advance --intent "..." --story <sid>
python3 autocut3/orchestrate.py <pid> --step vadwords --story <sid>   # 单步
```
- `--advance` 语义：素材段自动；storyline 无 intent 停在门；vadwords→qc 自动；
  qc 通过后自动逐幕样张（beat preview）自检，任一幕失败=拦截。
- mount 只提示不自动（挂包是创作决策）。
- **项目锁**：advance 持 `projects/<pid>/.advance.lock` 非阻塞互斥——报"被占"=另一 Agent
  正在推进同项目（ASR/LLM 会烧两遍钱），等它完成再推（2026-09-18 并发加固）。

### 4.5 生成类（按需，非每单必用）
| 命令 | 说明 |
|---|---|
| `tts.py synth --text "..." [--out --voice --speed]` | 单条 TTS；provider 见 config。本地 audio8 服务**空闲 60 分钟自动退出**（看门狗 tts_idlekill，下次调用自启） |
| `tts.py voiceclone --audio <f> --voice-id <n>` | 音色克隆（local-audio8） |
| `image_gen.py "<prompt>" <out> [--ar 9:16]` | AI 封面/贴图 |
| `video_gen.py gen <pid> --prompt "..." [--image --duration --res]` + `poll <pid>` | AI 生成片段（预留口） |

### 4.6 切片子系统（cuts，老项目形态，见到再碰）

部分老项目（v1 形态）不用 packs，而是**单条长素材** `projects/<pid>/materials/src.mp4`
切成 cuts：`projects/<pid>/materials/cuts.json`（ASR 切分产物，cuts[].{cut_index,in,out,
duration,text}）→ `library.py seed <项目目录>` 种子化成 `materials/library.json` 的
`cuts[]` 档案（含 usable 拍摄层决策/audit，时间 0.1s 量化）。原则：usable 只评拍摄层
（说错/卡等/重复/气口），创作取用仍在 storyline。

相关路由（新 packs 项目用不到，别混）：`GET /api/library/<proj>/clip/<cut_index>`
（cut 预览 mp4）、`POST /api/proofread-cut/<proj>`（cut 台词人工校对，等长替换）、
`POST /api/library/<proj>/usable`（cut 级废片，body `{"cut_index":0,"usable":false,
"defects":[...]}`）。**packs 与 cuts 是两条素材轨，一个项目里通常只有一套在役**——
先看 `materials/library.json` 有 `packs` 还是有 `cuts`。

---

## 5. 故事线数据契约（直接读写 storylines/<sid>.json 前必读）

```json
{
  "title": "...", "outline": "...", "origin": "ai-draft",
  "meta": {"audio": {"bgm_id": "...", "bgm_volume": 0.3, "loudnorm": true}},
  "beats": [{
    "no": 1, "id": "b1",
    "story": "可直接朗读的口播台词（≤120 字）",
    "tracks": [{"role": "A", "source_id": "M0167", "src_in": 3.2, "duration": 4.0},
               {"role": "B", "source_id": "M0181", "src_in": 0, "duration": 4.0,
                "op": "overlay-pip", "pos": "br", "scale": 0.3}],
    "narration": {"mode": "original", "words": [{"t": "字", "s": 0.12, "e": 0.35}]},
    "transition_out": "fade-black",
    "effects": {"stickers": [{"asset": "candy-sky", "text": "得房率高", "at_word": "得房率", "duration": 1.0, "pos": "top-center"}],
                "sfx": [{"asset": "x_xxx", "at": 1.2, "duration": 0.8}]},
    "subtitle": {"style": "default"},
    "music": {"inherit": true}
  }]
}
```
- `narration.words` **由 vadwords 生成，不要手写**（手写字时间不准=字幕错位）。
  改字幕文本=改素材转写（original 幕）或 story（dub 幕）后重跑 vadwords。
- `words[].s/e` 是相对**幕起点**的秒。
- 兼容顶层键（手写也可用）：`subtitle_style`、`audio.{bgm_id,bgm,bgm_segment,bgm_loop,bgm_volume,loudnorm}`。
  **BGM 用法约定**：不用 BGM=**不写 `bgm_id` 键**（写 null 虽同效，统一删键别两式混用）；
  用时 id 必须已在 `registry/bgm.json` 登记——拼错不存在的 id 渲染直接 SystemExit 拦。
- B 轨仅视频素材、无音频通道、禁说话画面；`op:"overlay-pip"` + pos（tl/tr/bl/br）+ scale。
- **"当前故事线"= `storylines/*.json` 里 mtime 最新的那条**（qc.py `--story` 省略、
  编排器取当前线、Studio 默认展示，全按这个规则）。副作用：你改了一条旧线，
  它就变成"当前线"——多线并存时**渲染/QC 永远显式带 `--story`**，别靠默认值。

---

## 6. 注册表：怎么用、怎么维护

六个注册表在 `registry/*.json`，是**素材/风格/转场的唯一白名单**。draft 只从这里选，
渲染只认这里登记的 id。Agent 可以维护（新增爆款音效/BGM/字幕风格），但必须走
Studio API（带校验+落盘一致），**禁止手编 JSON**：

| 注册表 | 条目字段 | 用途 |
|---|---|---|
| `sfx.json` | `{name, desc, file}` | 音效：draft 选→渲染混音（现存仅 ding/success；引用已删 id 渲染自动跳过，别选表外 id） |
| `bgm.json` | `{file, name, desc, loop, segments:[{name,in,out,desc}]}` | BGM：整曲或段落（segments 供幕级选段） |
| `subtitles.json` | `{font, size, primary, secondary, outline_col, border, marginv, max_chars, karaoke}` | 字幕样式（style id） |
| `transitions.json` | `{type: xfade\|flash, preset, duration, ...}` | 幕间转场（transition_out） |
| `stickers.json` | `{type:"text", text_style{bg,fg,stroke,font_size…}, default_text, pos, duration}` | 贴纸=**样式模板**（文字模板制）：渲染时 drawtext 现画，短语来自故事线 `effects.stickers[].text`（**≤6 字硬门**，draft 从本幕口播蒸馏；缺省回退 default_text；缓存 materials/.sticker_cache/）。无 text_style 的条目按老路径读 file（静态 PNG 兼容）。**v2 词根**（2026-09-18 现代·活泼批 12 款：胶囊/多巴胺四色/荧光夜光/荧光笔/白环贴纸/漫画爆点/玻璃拟态/极简墨条）：`font`=wenkai/smiley/kuaile、`radius` 圆角(≥半高=胶囊)、`shadow`+`shadow_c` 硬投影(可带@alpha)、`ring`+`ring_c` 白边环；带任一 v2 词根走紧裁画布（宽随文字自适应）。样式样张：`GET /api/sticker-preview/<id>`（走渲染同一实现，改样式换哈希自动重画） |
| `enums.json` | 五个枚举数组（video_types/cover_strategies/narration_modes/platforms/positions） | 全局词汇表（前端/后端同源；不开放编辑） |

Studio 路由（http://127.0.0.1:8765）：
- 读：`GET /api/registry/<name>`、`GET /api/enums`
- 写：`POST /api/registry-save/<name>`（整体替换，白名单=上表前五）、
  `POST /api/registry-delete/<name>/<id>`
- 上传素材入册：`POST /api/registry-upload/<bgm|sfx|stickers>/<id>?filename=`
  （自动存 assets/music|sfx|stickers 并加条目）
- 字幕样张：`GET /api/sub-preview/<style_id>` 出 jpg——**改字幕样式前先看样张**。

维护纪律：新增音效/BGM 须免费商用许可（许可文件随包放 assets/ 下并在条目注明来源）；
删除前 grep 确认没有故事线引用该 id。

---

## 7. Studio API 速查（Agent 经 HTTP 驱动时的主通道）

启动：`python3 studio.py [port]`（默认 8765，只绑 127.0.0.1）。**改 studio.py 后必须重启进程**。

- 状态：`GET /api/status/<proj>`（13 环节，与 orchestrate 同源）、`GET /api/pulse`（全项目签名）
- 创作台数据：`GET /api/project/<proj>?story=<sid>`（故事线/plan/素材/参数全量）
- 写故事线：`POST /api/storyline/<proj>?story=<sid>`（title/outline/meta/beats 整体替换）
- 渲染：`POST /api/render-start/<proj>?story=<sid>`（异步）或 `POST /api/render/<proj>?story=<sid>`（同步 600s）
- 幕试渲：`GET /api/beat/<proj>/<beat_no>?story=<sid>`（同步 300s）
- 素材：`POST /api/transcribe/<pid>?material=&force=`、`POST /api/understand/<pid>?...`、
  `POST /api/digest/<pid>`、`POST /api/usable...`（废片/复核切换）、
  `POST /api/proofread-pack/<pid>/<fid>`（人工校对单条转写）
- AI 起草：`POST /api/draft/<proj>` body `{"intent": "..."}`
- 封面：`POST /api/cover-gen/<proj>`（AI）、`POST /api/cover-upload/<proj>`
- 项目/包：`POST /api/project-create/`、`POST /api/pack-mount/<proj>/<pid>/mount|unmount`、
  `POST /api/pack-upload/<pid>?filename=`、`POST /api/pack-create/`
- 文件：`GET /files/projects/<proj>/out-<sid>.mp4` 等（Range 流，可拖进度）

---

## 8. 操作红线清单（每条背后都有真实事故，背景见仓库 CLAUDE.md 失败模式清单——此处只留规则）

1. **proofread 的参数是项目名，不是包 id**——`proofread.py <项目> [--pack X]`，队列固定落
   `projects/<项目>/review-queue.json`（与编排器门禁同目录，不存在错位可能）。
   ⚠ 旧文档/旧会话里可能见过 `proofread.py <包id>` 的写法，那是过时用法，别照抄。
   手动迁过队列后的验证：跑 `orchestrate.py <pid>`——proofread 环节显示
   "待人工复核 N 组"=门禁已看见；还显示"待校对 N/N"=文件没放对目录。
2. **`audit.proofread: "pending"` ≠ 已校对**：上传初值就是 "pending"，只有
   `done`/`auto-done` 算过。看状态用 orchestrate 严格判据，不要自己数非空。
3. **review-queue 非空不许静默继续**：有 pending 组=有人工活，汇报雇主。
4. **original 幕改字幕 ≠ 改 story**：改素材转写（等长替换铁律：长度必须相等，否则破坏
   字符-时间对齐），然后重跑 vadwords。
5. **B 轨禁说话画面**（哑口型）；**voiceover/meta 画面禁 A 轨**（读稿画面播原声=穿帮）。
   draft validate 会硬剔除，但 LLM 选段仍要人工复核。
6. **故事线写完必须全面检查+逐幕审核再渲染**（阶段 4）。LLM 可能截断
   （finish_reason=length）产出缺字段的幕。
7. **渲染失败/任务崩溃必须留痕**（时间点+最后产物+最后动作），续作从记录接着走。
8. **合成词时间禁作锚**：tier=vad/sent 的词时间是均分合成值，"按词时间过滤/对齐"停手；
   tier=word 实测词时间是 vadwords 直通道输入（重转写自动获得），人工别碰时间轴。
9. **concat 跳过必占位**：改渲染链时跳过区间必须补等长静音段，否则后段前移。
10. **改代码/配置后**：跑 `python3 tests/e2e.py --fast` + `python3 tests/audit.py`
    （P1 必须 0）；改 studio.py 后重启进程再用行为探针验证。
11. **气口 +0.35s 卷词头**：A 轨出点自动留词尾+0.35s 气口（防咬字），词尾与下一句
    **零间隙**时会把下句词头卷进本幕——观众听到下一句的第一个字，转场/切幕都救不了。
    识别：beat 试渲听幕尾有没有不属于本幕台词的字。修法：**切点后移**——增大该幕
    A 轨 `src_in`（或缩 `duration`），让 +0.35 落在句间静音里；
    **不要手编 narration.words**（词轨是 vadwords 的重跑产物，手改时间必错位）。
12. **并发互斥报错不是故障**：渲染报"已有渲染进行中"/advance 报"被占"= 另一进程正在
    干同一件事（多 Agent/双 Studio 同机是常态）——等待或 `ps` 对账后接力，**不要删
    render.status / .advance.lock 强行绕过**（半截产物互踩才是真事故）。

---

## 9. 交付前最终清单（逐项打勾，附证据路径）

- [ ] `orchestrate.py <pid>` 13 环节全 done/na
- [ ] review-queue.json 无 pending；proofread 19/19（或 N/N）done/auto-done
- [ ] 故事线逐幕审核记录（重点幕 beat preview 看过）
- [ ] vadwords 已重跑（改稿后）
- [ ] dub 线：dubfit-report + dubgate-report passed
- [ ] `out-<sid>.mp4` 存在；qc-report.json verdict=deliverable
- [ ] warn 清单已抄录进交付说明
- [ ] 兜底假设（时长/平台/风格）已请雇主确认
- [ ] 交付说明：成片路径+时长+响度、verdict、warn、改动历史、遗留问题

---

## 附录 A · Studio POST 请求体形参（经 HTTP 驱动时照抄）

| 路由 | body / query | 说明 |
|---|---|---|
| `POST /api/project-create/` | `{"name":"myproj","title":"...","format":"vertical","note":"..."}` | format 取值 = formats.json 键：`vertical`(9:16) / `landscape`(16:9) / `square`(1:1)；name 只允许字母数字下划线中划线；**`projects/<name>/` 目录已存在（含手工残件）=409「项目已存在」**——确认是残件后删目录再走本 API 重建，不要手工 mkdir 冒充创建 |
| `POST /api/pack-create/` | `{"id":"mypack","name":"我的素材"}` | 建空包 |
| `POST /api/pack-upload/<pid>?filename=xx.MP4` | 原始字节（--data-binary @文件） | 入包+审计+缩略图；文件名禁 `/` `\` `..` 和点前缀，中文 OK；**>1GiB（1,073,741,824 字节）硬拒 400**，不是"易断"——超限素材用 `ffmpeg -i 源 -c copy -segment_time 60 -f segment 前缀_%03d.MP4` 按时长无损拆段（`-c copy` 不重编码），逐段上传 |
| `POST /api/pack-mount/<proj>/<pid>/mount|unmount` | 无 body | 挂载=创作决策，只走这个路由 |
| `POST /api/draft/<proj>` | `{"intent":"..."}` | AI 起草（等价 draft.py --intent --save） |
| `POST /api/storyline/<proj>?story=<sid>` | `{"title":"...","outline":"...","meta":{...},"beats":[...]}` | **整体替换**这些键（缺省键不动）；sid 不存在=新建故事线。改 beats 后记得重跑 vadwords |
| `POST /api/storyline-delete/<proj>?story=<sid>` | 无 body | 删线 |
| `POST /api/render-start/<proj>?story=&duration=&hold=&grain=&flash=` | 无 body | 异步渲染；四个旋钮见阶段 5；同线渲染中=409；**另一进程在渲同项目也 409**（render.status 磁盘双检，2026-09-18） |
| `POST /api/render/<proj>?story=&...` | 无 body | 同步渲染（600s 超时），长片用 render-start |
| `GET  /api/beat/<proj>/<幕号>?story=` | 无 body | 幕试渲（同步 300s），产物 previews/beat_<sid>_<幕号>.mp4 |
| `POST /api/pack/<pid>/<fid>/usable` | `{"usable":false,"defects":[{"at":3.2,"type":"说错","note":"..."}],"review":"reviewed"}` | 素材级废片/复核切换（锁内 RMW，安全） |
| `POST /api/library/<proj>/usable` | `{"cut_index":0,"usable":false,"defects":[...]}` | cut 级废片（cuts 形态项目） |
| `POST /api/proofread-pack/<pid>/<fid>` | 词轨等长替换结构（走 Studio 界面更稳） | 人工校对单条转写 |
| `POST /api/voice-register` | 音色注册（audio8 硬契约见阶段 3） | dub 换音色前置 |
| `POST /api/registry-save/<name>` | 整个注册表 JSON（≤2MB） | 白名单=bgm/subtitles/transitions/sfx/stickers |
| `POST /api/registry-upload/<bgm|sfx|stickers>/<id>?filename=` | 原始字节 | 上传素材入册+自动建条目 |
| `POST /api/pack-upload/<pid>?filename=<名>` | 原始字节（文件字节） | **导入素材**（文件名中文/`#`/空格均允许）。⚠ `filename` 必须 percent-encode（`#`→`%23`、空格→%20）——裸 `#` 在 URL 里是锚点分隔符，客户端会把 `#` 后面整段截掉，服务端只见半截名报 400 bad filename（2026-09-19 井号素材实锤）。Python 用 `urllib.parse.quote(name)`、JS 用 `encodeURIComponent` |
| `GET  /api/sticker-preview/<id>` | 无 body | 贴纸样式真渲染样张（走渲染同一实现，PNG URL） |

**pack.json 字段口径**：`files[].size_mb` 是十进制显示值（字节数 ÷ 1e6，只用于人看）——
**逐字节比对/完整性校验不要用 size_mb**，用 `os.path.getsize(文件)` 对字节数。
