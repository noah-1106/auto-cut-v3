# auto-cut V3 · 绝低压力快速剪辑

> 原则：**人管创作，机器管工程**。数据只有一份（JSON），人和 Agent 走同一入口改它；枚举驱动创作台，规则驱动渲染器。

## 快速上手（Mac）

```bash
cd /Volumes/Backups/auto-cut-v3
/usr/bin/python3 studio.py 8765          # 启动 Studio → http://localhost:8765
open http://localhost:8765
```

浏览器：**配置页**（项目列表 + 注册表 + 模板工程台）→ 点项目进**操作页**（创作台 / 素材库 / 审计）。

## 三条军规

1. **人机等价**：Agent 能做的每个操作，Studio 必须有人的等价操作（按钮或一句话输入）
2. **Windows / Mac 复用**：ffmpeg 解析顺序 = `$FFMPEG` env → 仓内 `bin/`（mac 专用）→ PATH；新代码纯标准库 + PATH 意识
3. **边做边写文档**：README 与实现同步，过时即修

## 仓库结构

```
config/services.json     服务供应商登记处（ASR/LLM/Vision 各自 provider + backends）
                         换供应商只改 "provider" 一行；key 走 env 或 api_key_file，不入仓
registry/*.json          全盘枚举与模板：enums / transitions / subtitles / stickers / sfx
materials/
  packs/<id>/pack.json   素材包（拍摄级资产，跨项目）。files[].tags: ["root"]=全项目可见
  packs/<id>/thumbs/     缩略帧
projects/<pid>/
  project.json           项目档案（title / active / material_packs）
  storylines/<sid>.json  故事线（一个工程多条故事线 = 同素材多个成片）
  materials/             项目自有素材（n001 源片 + cuts.json ASR + library.json）
  out-<sid>.mp4          成片（按故事线隔离）
  plan-<sid>.json / subtitle-<sid>.ass / cover/cover-<sid>.jpg / previews/beat_<sid>_<no>.mp4
autocut3/                管线（pipeline.py）
  asr.py                 ASR 适配层：归一化词轨 words[{text,start,end}] @0.1s
  transcribe.py          转写生成器
studio.py                Studio 本地服务（API + 静态页）
studio/index.html        前端（单文件，深色工作台）
```

## 核心概念

- **素材两层**：项目素材（项目内，如 n001）挂上面、跨项目素材（仓库级 packs）挂下面；
- **素材三类型**：video=ASR+视觉采样帧 / image=视觉+OCR / audio=ASR；卡片按类型渲染，理解产物落 `visual{desc,ocr,usage}`——desc 供选段、ocr 佐证、usage 辅助修剪；：`usable` 只评拍摄层（说错/卡等/重复/气口）；创作取用（A/B-roll、取舍）在 storyline 决策——档案不变、取用变
- **时间规范**：全链 0.1s 量化；词轨归一化契约 `words[{text,start,end}]`，来源对下游透明
- **时间戳三级降级**：tier=word（本地字级）→ sent（云端句级，句内按字分配）→ vad（ffmpeg 静音检测 + 文本比例插值）
- **一个工程多条故事线**：同一批素材的不同成片取法，产物按 `-<story>` 后缀完全隔离
- **封面收口**：任何来源（首帧/幕帧/上传）都落 `cover/` 槽位，工程统一拼装

## 生成器（Agent / 人同权）

| 能力 | Agent（CLI） | 人（Studio） |
|---|---|---|
| 转写 | `python3 autocut3/transcribe.py <project> [--material M0128] [--asr minimax] [--force]` | 素材库卡片「⟳ 转写」/「↻ 转写全部待审」按钮 |
| 画面识别 | `python3 autocut3/understand.py <project> [--material M0128] [--vision minimax] [--force]` | 素材卡「⟳ 画面识别」/「◉ 识别全部待审」按钮 |
| 故事线起草 | `python3 autocut3/draft.py <project> --intent "…" [--save]` | 创作台「✨ AI 起草」按钮（AI 只起草不落定，草稿=新故事线） |
| 渲染 | `python3 autocut3/pipeline.py render <project> [story]` | 创作台「▶ 保存并重渲本故事线」 |
| 幕预览 | `python3 autocut3/pipeline.py beat <project> <no> [story]` | 幕卡「▶ 幕预览」 |
| 计划 | `python3 autocut3/pipeline.py make <project> [story]` | 保存即重算 |

## 服务配置（config/services.json）

```json
{ "asr": { "provider": "minimax",                       ← 换供应商改这里
           "backends": { "minimax": {...}, "local-faster-whisper": {...} } } }
```

- key 解析：env（api_key_env）优先 → api_key_file 指向的 JSON 的 api_key 字段。**不入仓、不回显**
- 本地 faster-whisper（本版本不内置模型，保留下载通路）：
  `pip install faster-whisper`，模型首跑自动下载（small ≈ 460MB）；`--asr local-faster-whisper` 切换
- MiniMax 限额：≤50MB / ≤500s；中国站 base_url `api.minimaxi.com`，国际站 `api.minimax.io`

## 外部 Agent 上手（ExFlower / Codex / 任意 CLI Agent）

项目 = 文件夹 + CLI + 本地 Studio。Agent 不需要浏览器：

```bash
cat README.md && ls projects/ materials/packs/        # 读盘入职
python3 autocut3/transcribe.py demo-ningbo       # 素材转写
python3 autocut3/understand.py demo-ningbo       # 画面理解
python3 autocut3/draft.py demo-ningbo --intent "..." --save   # 起草故事线
python3 autocut3/pipeline.py make demo-ningbo aidraft    # plan+字幕
python3 autocut3/pipeline.py render demo-ningbo aidraft  # 渲染成片
```

**双向同步（先落库，后读取）**：人和 Agent 的所有修改都直接写 JSON 文件。Studio 每 5 秒轮询 `/api/sync/<project>` 比对 mtime：干净状态自动载入对方修改；有未保存改动先询问再覆盖。Agent 落库无需通知，人最多 5 秒可见。

**默认故事线**：`/api/project/<name>` 不带 story 参数时，服务端返回「最近修改」的故事线——首页进项目不再空屏。

## 路线图（2026-09-11 定稿）

| 期 | 主题 | 内容 | 规模 | 验收 |
|---|---|---|---|---|
| R0 | 验证债清偿 ✅ 2026-09-11 | 贴纸：帧差法实测（ON窗/基线 2.1×，词点+fallback 双路径）；转场：filmBurn(koubo)/crossDissolve(jilu) 均有真实成片背书；画面识别 21/22 全量完成（AUD001 音频无视觉轨，正确跳过） | 小 ✅ | 已达 |
| R1 | 第二条产品线开通 ✅ 2026-09-11 | 验收剧本全走通（舟山横版 1920×1080）。尾巴已清：方版实测 1080×1080（三种画幅全部有真实成片）；包删除（归档制 + 全项目摘挂载）| 中 ✅ | 已达 |
| R2 | 起草体验闭环 ✅ 2026-09-11 | ①「草稿对比」tab（项目页）：现存 vs 草稿双列、逐幕勾选、采纳合并进现存线（替换同序号/追加新幕）；②渲染任务模型：/api/render-start + /api/render-progress，pipeline 以 STAGE/PCT 行协议输出进度，saveAndRender 改启动+轮询（实时百分比），同线防重入 409；③顺带修掉 /api/project 显式 story 静默换线的同族 bug（显式指定→404） | 小-中 ✅ | 已达（T11 入回归） |
| R3 | AI 配音 dub ✅ 主体 2026-09-11 | TTS 选型落定 MiniMax speech-2.8（turbo 默认/hd 可切，同域同 key）；draft --dub 逐幕配音→narration.mode=dub；渲染链配音轨替换原声（apad 补齐）；**dub 幕字幕词轨=配音文案按实测时长均分**（画面字幕跟配音不跟原声）；音画同步修复（音频链 acrossfade 与视频 xfade dt 同源，直切 concat 1:1）。等包删除尾巴：voiceclone 代码全通等平台权限（2038）；H3 视频生成代码就绪等套餐（v2 协议已适配） | 大 | 双版本成片 ✅（aidraft4=19.5s dub 版；koubo 原声版回归绿） |

**R3 前置能力（2026-09-11 已落地，先于 dub 主线）**：
- `autocut3/tts.py`：synth（speech-2.8-turbo/hd 配置化、speed 语速=时长约束杠杆）+ voiceclone 子命令。**实测 6/6 通**；克隆代码全通但账号权限未开通（平台 2038），开通后 voice_id 直填即用
- `autocut3/video_gen.py`：AI 视频生成（Hailuo-2.3 实测通，**两条 5.9s 素材已入 ai-generated 包走审计流程**——产物=素材不是成片，与拍摄素材同权）。H3 代码就绪但 **Token Plan/Credit 套餐暂不支持**（v2 协议已适配，content schema + 根级端点），套餐开通即用
- 哲学：生成式产物一律走素材审计流程，禁止直接进渲染管线（先审阅落盘，后取用）

**砍掉**：MLT 导出（2026-09-11 Noah 拍板）、注册表可视化编辑、发布分发、任务队列。
**等信号再做**：字级词轨精修（等卡拉OK字幕被抱怨）。
**依赖链**：R0 的识别档案是 R1 新项目复用素材的原料；画幅是项目级属性所以并入 R1 向导；R3 的时长约束依赖 R1 的画幅参数。
**R0 顺手修复**：素材挂载源分裂——CLI 生成器读 project.json 旧字段、Studio 写 library.json，Agent 看不见 Studio 挂的包；已统一为 library.json packs 唯一事实源（三生成器同修）。

## 测试（军规3 的执行器）

```bash
python3 tests/e2e.py          # 全量（含真实 LLM 起草与渲染，约 3-5 分钟）
python3 tests/e2e.py --fast   # 快速轮（跳过 LLM/长渲染，约 90 秒）
```

16 项回归覆盖历次挖出的每类 bug：注入面白名单（T2）/ 空屏默认线（T3）/ 同步探针（T4）/
上传入库与重名保护（T5）/ 音效 max_volume 对账（T6）/ 贴纸同刻跨渲染 A/B（T7）/
起草渲染全链路（T8）/ 多画幅（T9）/ 双向同步（T10）/ 渲染任务模型与 PCT 钳位（T11，含方版画幅顺带回归）/ dub 混合渲染（T12）/ 包删除全流程（T15）/ **narration 三态同线共存（T16：none静音+原声+dub，分段实测）** / **混合转场链（T17：直切concat→xfade 时基归一，2026-09-11 实锤 bug 回归）**。**2026-09-11 苏炜真实案例轮 21/21**：套件随活跃项目迁移（PROJ+素材 ID 映射，王亚伦旧 ID 零残留）、断言全面语义化（T7 画幅无关阈值、T10 动态选线不点名）。全装饰版成片（BGM+音效×2+贴纸×2+三态转场+词轨字幕+画中画）已出。**2026-09-11 回看修复轮**：苏炜回看揪出【rotation 陷阱】——素材编码 1920x1080 + rotation=-90（实为竖拍），入库只读编码宽高导致横版误判，全片被压扁（字幕/贴纸/画中画连带变形，成片还被 ffmpeg 写入 SAR 81:256 补偿标记）。修复三件套：① video_meta.display_geometry() 显示尺寸探测（入库登记=转正后真实方向）；② 项目画幅改回 vertical；③ validate 出点自动留尾 0.35s（ASR 词尾偏紧咬字，幕出点=词尾+气口不越物理边界）。回归 T18（SAR 纯净性+登记方向）。**rotation 升格为全管线必选项（2026-09-11）**：人侧 Studio 上传路径接入共享 display_geometry 探针（人机同权一个事实源，登记宽高=转正显示尺寸，_rotation 病因落盘）；T5 夹具升级为真实带 rotation=-90 的手机素材（合成 testsrc 无 rotation，恰好造就了上传路径的测试盲区），断言含转正尺寸+病因角度；T18a 同步断言审计链完整。E2E 23/23。**实时性补全（2026-09-11 Noah 反馈：CLI 操作界面看不见、必须刷新）**：① 渲染器落盘 render.status（CLI/Agent 渲染在文件系统留下状态，每 5% 刷一次）；② /api/render-progress 加文件回退（内存任务表查不到就读文件，5 分钟新鲜度）；③ 新增 /api/pulse 脉搏接口（项目数据签名 + 全部渲染任务 + 素材包签名）；④ 前端 pulsePoll 2.5s 轮询：渲染状态条全视图可见（无论谁启动的渲染）、配置页项目卡自动刷新、素材库变更静默同步，DIRTY 守卫不打断编辑。**清场全流程重跑验证（2026-09-11 深夜，Noah 指令'清理掉了吧，重新完整跑一下'）**：归档制清场（项目+包全进 _attic 可找回）→ 空项目重建 → 13 条素材从原始拍摄目录（贝壳圣都/宁波/苏炜）重新入库 → 转写 13/13 + 视觉识别 13/13（**全新真实 API 调用，零跳过**——上次跑法复用旧登记，这次是真全量）→ AI 起草 → 装饰层（BGM+音效×2+贴纸×2+画中画+三态转场）→ Studio API 渲染 38.9s → E2E 23/23。**回看修复轮②（2026-09-11 深夜，Noah 三问）**：① 字幕页重叠（10/19 页压 0.25s）——build_ass 尾缓冲未与下页起点钳制，已修；② **proofread.py 校对层落地**（audit.proofread 从 pending 到实现）：LLM 同音字校对，铁律=等长替换（词轨时间戳/卡拉OK对齐零破坏），非等长拒绝；首战抓 3 组：轮骨→龙骨、潭溪工馆→檀溪公馆（楼盘名）、放可以了→方可以了；③ 死尾巴修剪：validate 出点上限=末实词尾+0.8s（pad 下限 0.35 的镜像），幕1 词尾 7.2 剪到 9.0 的 1.0s 近静音段（-52dB）裁掉。E2E 23/23。
**任何接口改动后必跑；新 bug 修复必须先在 e2e.py 里立对应测试项再修。**

## 全量代码审查记录（2026-09-11）

范围：autocut3 全部 7 模块（asr/transcribe/understand/draft/vision/pipeline/library）+ studio.py（622 行）+ index.html JS（999 行）逐行通读；mlt_export.py 判定为死代码（Noah 拍板砍掉的功能，无人调用）→ 归档 autocut3/_attic/。
**修复 12 项**，按严重度：

| 级别 | 问题 | 后果（若未修） |
|---|---|---|
| 数据损坏 | storyline-delete 路由头被补丁吞掉（回归，E2E 抓住） | 删除线功能整体失效 |
| 渲染错误 | 多幕贴纸用绝对时刻挂局部时间轴 | 第 2 幕起贴纸永不出现 |
| 渲染错误 | 音效 adelay 用视频绝对时轴（acrossfade 重叠未计入） | 多幕音效逐渐偏移 |
| 潜伏 | narration=none 幕的 srcs/segs 错位 | dub 落地时词轨整体错幕 |
| 安全 | pack usable / proofread-pack / library clip 3 入口无白名单；clip ci 非数字可写任意路径 | 路径注入 |
| 健壮性 | cut_index 垃圾输入 500；story_file 显式 sid 静默回退旧线 | 500 / 渲错对象 |
| 兼容 | beat-frame 封面仍依赖 src.mp4；pos_xy 竖版 sub_clear 写死 | R1 新项目封面缺/贴纸位置错 |
| 交付债 | 素材上传只说不做（上轮总结虚报） | R1 验收缺口 |

**记账不修（低危）**：asr 缓存同名冲突（后传覆盖前传）；
**补审追加（第一次总结漏报，实审 9 模块后修正）**：library.py 为活代码（缩略帧保障+seed 命令，无新问题）；mlt_export.py 死代码归档。
**方法论教训**：单幕测试验不出多幕时轴 bug（贴纸/音效均中招）——测试必须覆盖多幕场景（e2e T6/T7 已用真实多幕线）；对照实验要用同刻跨渲染（编码确定性），不能用跨刻（运动噪声混淆）。

## 常见排查

| 症状 | 看哪 |
|---|---|
| Studio 起不来 | `/tmp/studio.log`；端口占用 `lsof -ti :8765` |
| 渲染失败 | CLI 直跑 pipeline.py 看完整 stderr；硬编失败会自动回退软编 |
| 转写 404 "无需转写" | 已 done 会跳过，加 `&force=1`（UI 上点已完成卡片的 ⟳ 前先改 audit） |
| 素材卡没有视频 | thumb/url 路径 = pack.json file 字段；404 先查文件名大小写 |
| 页面空白 | 提取 `<script>` 跑 `node --check`——语法错炸全页（历史教训） |

## 已知边界（诚实清单，2026-09-11 全面刷新）

**功能缺口**
- 新建故事线按钮未做（只能 AI 起草或复制文件；新建项目已由 R1 向导解决）
- sync 探针只盯故事线域：Agent 新建素材包/改素材库时，人的下拉与卡片不自动刷新（要切一次项目）（R2 未动，仍挂账）

**精度与观感**
- 词轨 vad/sent tier 为近似时间戳（句边界准、句内插值）；字级精修需本地 faster-whisper（等信号）
- at_word 词点未命中时静默 fallback 到幕内 0.4s（贴纸出现但不在词上，无提示）
- ~~音画漂移~~（2026-09-11 闭环）：音频重叠已与视频转场语义对齐——dissolve 重叠 dt、**flash 类重叠 0**（视频轴不缩音频也不缩）、直切 concat 1:1；全转场类型回归实测音轨=视频轨（koubo 21.0=21.0）
- 多个 filmBurn 连用时 flash_peak 只作用最后一个

**低危记账**
- ASR 音频缓存按文件名（跨包同名互相覆盖缓存，仅浪费重算不出错）
- pack-upload 同名文件自动加时间戳后缀（不覆盖）
- 配置页注册表只读；增删直接改 registry/*.json（枚举实时生效）
