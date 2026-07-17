# /make-course

将 YouTube 视频自动制作为英语听力精讲课程（含逐句音频播放器与中文注释）。

## Input

`$ARGUMENTS` 格式：`<youtube-url> [--out-dir <path>] [--level <描述>] [--title <课程标题>] [--mode theme|meeting]`

示例：
```
/make-course https://www.youtube.com/watch?v=lBVtvOpU80Q
/make-course https://youtu.be/xxx --out-dir ~/Desktop/my_course --title "产品营销周会" --mode meeting
```

## Instructions

当用户运行此命令时，执行以下完整流水线（从仓库根目录运行所有命令）：

### 第 0 步：解析参数

从 `$ARGUMENTS` 中提取：
- `URL`：YouTube 链接（必填）
- `OUT_DIR`：输出目录（默认 `./course_$(date +%Y%m%d_%H%M%S)`，或使用视频 ID）
- `LEVEL`：目标学员描述（默认 `"B1 水平，目标是学完后重听能听懂所有内容"`）
- `TITLE`：课程标题（默认从视频元数据获取）
- `MODE`：素材模式，决定内容筛选强度（第 1.7 步用）：
  - `theme` —— 主题类素材（演讲、纪录片、解释类视频、评测、布道、讲座）：**只保留主体内容**，删广告、赞助口播、频道推广、片头片尾过场、离题闲聊
  - `meeting` —— 实战类素材（会议、访谈、panel、播客）：**全部保留**，包括填充词、口误、false start——这些本身就是实战听力的训练价值
  - 未指定时根据视频类型推断，并在开跑前把推断结果报告给用户

### 第 1 步：下载 + 转录 + 拆段

使用 Bash 运行：
```bash
VIDEO_ID=$(yt-dlp --get-id "$URL" 2>/dev/null)
OUT_DIR="${OUT_DIR:-./course_$VIDEO_ID}"

# download 只下音轨（含 --remote-components ejs:github 破解 YouTube 限速）
uv run python scripts/make_course.py download "$URL" --out-dir "$OUT_DIR"
uv run python scripts/make_course.py transcribe "$OUT_DIR/full.wav" --out-dir "$OUT_DIR"
uv run python scripts/make_course.py split-segs "$OUT_DIR"   # 拆出 segs/segN.txt + segs_meta.json
```

`transcribe` 除 transcript.txt / segments.json 外还会保存 **words.json（词级时间戳）**——这是第 1.7 步重断句对齐的依据，勿删。

**长素材必读**：`transcribe` 对长视频（如 80 分钟）要跑 15–30 分钟（GPU 串行，拆分并不能加速）。用 Bash `run_in_background: true` 跑这些长命令，并挂一个监视器等 segments.json 生成——**监视器命令里绝不能再加 `&`**（否则外壳提前返回、真任务脱钩、完成通知变误报）：
```bash
# 正确：直接写阻塞循环，让工具后台化整个循环
i=0; while [ ! -f "$OUT_DIR/segments.json" ] && [ $i -lt 200 ]; do sleep 20; i=$((i+1)); done
[ -f "$OUT_DIR/segments.json" ] && echo READY || echo TIMEOUT
```

读 `$OUT_DIR/segments.json` 了解分段；读 `$OUT_DIR/transcript.txt` 抽查转录质量（尤其多人重叠段——本转录不做说话人分离，`[发言人切换]` 只能据语气推测）。视频标题：`yt-dlp --get-title "$URL"`。`segs_meta.json` 的内容（很小）作为后续 Workflow 的 args。

### 第 1.5 步：评估体量与成本（超阈值必须先确认）

在启动付费的 AI 步骤（第 1.7 步整理 + 第 2 步注释）**之前**，先跑成本估算：

```bash
uv run python scripts/make_course.py estimate "$OUT_DIR"
```

把预估的 input/output token 和美元成本（Opus 4.8 与 Sonnet 5 两档）报给用户；另加一句说明：第 1.7 步整理重断句约增加 output ≈ transcript 字符数 ÷ 4 的 token（相对注释步很小）。**当输出带 `⚠ 超阈值`（时长 ≥30 分钟或预估 > $4）时，必须等用户明确确认再进第 1.7/2 步**；低于阈值可直接继续。theme 模式删减内容后实际注释成本会低于此估算（估算基于未删减的原始转录，偏保守）。

> 大/小文件都靠同一套"切分+并行"机制覆盖：`transcribe` 已把整段切成约 5 分钟一片，Workflow 对每片并行处理，`build-html` 组装。**长文件**可调大 `transcribe --segment-minutes` 减少并行 agent 数与总输出量；**注意转录本身是 GPU 串行**（mlx_whisper 单 GPU 无法靠拆分并行加速），长文件耗时主要在转录，如实告知用户。

### 第 1.7 步：整理重断句 + 内容筛选（AI，新增的关键步骤）

> **为什么需要这一步**：Whisper 按自然语音停顿切割，切出来的行经常是"半句 + 下半句"、甚至"A 的后半句 + B 的前半句"。直接拿去注释和剪音频，句播放器听到的就不是完整句子，无益于长句听力训练。必须先整理成完整句/意群，再让后续所有步骤（注释、剪辑、建 HTML）都基于精整稿。

调用 Workflow 工具，每段一个 agent 整理，各自把结果**直接写入文件**：

```javascript
export const meta = {
  name: 'refine-transcript',
  description: 'Re-segment whisper transcript into complete sentences, filter by mode',
  phases: [{ title: 'Refine', detail: 'Parallel per-segment re-segmentation' }],
}

// args: { dir, segs:[{seg,start_fmt,end_fmt,n}], mode }  — segs 用 segs_meta.json
const A = typeof args === 'string' ? JSON.parse(args) : args

const MODE_RULES = A.mode === 'meeting'
  ? `本素材是【会议/实战】模式：**全部内容保留**，包括填充词（uh/um/you know）、口误、
false start、自我修正——这些是实战听力的一部分，一个都不要删。只做重新断句。`
  : `本素材是【主题】模式：**只保留主体内容**。整段删除以下内容（整句、整段地删，不要只删词）：
  - 广告与赞助口播（"this video is sponsored by…"、优惠码等）
  - 频道推广（like & subscribe、Patreon、周边、社群）
  - 片头片尾过场、与主题无关的闲聊和离题
  保留主体中出现的填充词与口语特征（这些照常保留，仅删除上述"整块无关内容"）。`

const STATS_SCHEMA = {
  type: 'object',
  properties: {
    lines: { type: 'number', description: '输出的行数' },
    dropped: { type: 'string', description: '删除了什么内容（一句话，无删除则写"无"）' },
  },
  required: ['lines', 'dropped'],
}

phase('Refine')
const results = await parallel(
  A.segs.map((seg) => () => agent(
    `你是听力教材的文本整理编辑。

第一步：用 Read 工具读取 ${A.dir}/segs/seg${seg.seg}.txt。
每行格式为 "[HH:MM:SS.ss] 英文原文"，是 Whisper 按自然停顿切割的原始转录（切割点常在句子中间）。

第二步：重新断句 + 内容筛选，规则（硬约束）：
1. 每行必须是一个**完整句子**，或一个可独立理解的**完整意群**；禁止半句结尾、禁止两句拼一行。
2. **说话人切换必须另起一行**——绝不允许两个人的话出现在同一行（据语义/称呼/问答判断切换点）。
3. 行长目标 8–22 个英文词。超长句在从句边界、并列连接词（and/but/so/because/which/that）处切分，
   每个分片自身仍须是完整意群。
4. **只重组、不改写**：词序和用词必须与原转录一致（后续按词对齐时间戳，改写会导致对齐失败）。
   仅允许修正明显的 ASR 误识（如 sink→sync）和标点、大小写。**缩写与展开不得互换**
   （don't 不改 do not，gonna 不改 going to）；**数字保持原写法**（20 不改 twenty）；
   **不改英美拼写**（color/colour 保持原样）。
5. ${A.mode === 'meeting' ? '内容全保留（见下）' : '删除无关内容（见下）'}：
${MODE_RULES}
6. 保留内容严格按原顺序输出，不得重排、不得增删句内词语。**删除整块后，删除点前的
   最后一句和删除点后的第一句必须仍是两行**——禁止把删除点两侧的内容拼成一行。
7. 文件第一行/最后一行若是被段边界切断的半句，**保持原样单独成行**，不要试图补全或跨段拼接。

第三步：用 Write 工具把结果写入 ${A.dir}/segs/refined_seg${seg.seg}.txt。
格式：纯文本，每行一句，**不带时间戳、不带序号、不带任何标题或说明**。

最后返回统计信息。`,
    { label: `refine ${seg.seg}/${A.segs.length}`, phase: 'Refine',
      schema: STATS_SCHEMA, model: 'sonnet', agentType: 'general-purpose' }
  ))
)
return { segments: results.filter(Boolean).length, stats: results }
```

**Workflow 完成后**，按段号顺序合并、对齐、重拆段：

```bash
# 数字序合并（不能用 glob，seg10 会排到 seg2 前面）
N=$(ls "$OUT_DIR"/segs/refined_seg*.txt | wc -l)
: > "$OUT_DIR/refined.txt"
for i in $(seq 1 $N); do cat "$OUT_DIR/segs/refined_seg$i.txt" >> "$OUT_DIR/refined.txt"; echo >> "$OUT_DIR/refined.txt"; done

# 对齐词级时间戳 → sentences.json；重写 transcript.txt（原稿备份为 transcript_raw.txt）；重建 segments.json
uv run python scripts/make_course.py resegment "$OUT_DIR"
# 用精整稿重新拆段（供注释 agent 使用）
uv run python scripts/make_course.py split-segs "$OUT_DIR"
```

**检查 resegment 输出**：对齐覆盖率应 ≥ 90%（低于 85% 会告警，说明整理稿改写过多，须检查 refined.txt）；"✂ 丢弃"列表应与 MODE 预期一致（meeting 模式不应出现大段丢弃）；**任何 `⚠ 行 N span 异常` 告警都必须处理**——说明该行时间戳跨越了被删内容或锚到了错误的重复句，核对 refined.txt 对应行后重跑。有问题就修 refined.txt 后重跑 resegment。

### 第 2 步：AI 课程注释生成（核心步骤）

调用 Workflow 工具，传入以下脚本，用并行 Agent 为每个分段生成注释：

```javascript
export const meta = {
  name: 'make-course-content',
  description: 'Generate annotated English listening course from refined transcript',
  phases: [
    { title: 'Intro', detail: 'Generate course introduction' },
    { title: 'Annotate', detail: 'Parallel segment annotation' },
    { title: 'Compile', detail: 'Merge and finalize course markdown' },
  ],
}

// args: { dir, segs:[{seg,start_fmt,end_fmt,n}], title, level, total_duration }
//   segs 直接用 split-segs 产出的 segs_meta.json（很小，精整稿版）；每个 agent 自读
//   dir/segs/segN.txt，避免把 100–200KB 的 segments.json 塞进 args（沙箱也读不了文件）。
// 防御：args 可能以 JSON 字符串传入，而非解析好的对象。
const A = typeof args === 'string' ? JSON.parse(args) : args

const SEG_SCHEMA = {
  type: 'object',
  properties: {
    markdown: {
      type: 'string',
      description: 'Complete markdown content for this segment, starting with ## heading'
    }
  },
  required: ['markdown']
}

const INTRO_SCHEMA = {
  type: 'object',
  properties: { markdown: { type: 'string' } },
  required: ['markdown']
}

const SYSTEM_PROMPT = `你是英语听力课程编写专家，专注于为中文母语者制作职场英语听力教材。
目标学员：${A.level}
课程素材：${A.title}（时长 ${A.total_duration}）

【结构模式 —— 必须严格遵守，HTML 构建器按此解析】
每段输出必须符合以下骨架：

## 第N段：MM:SS–MM:SS        ← 段标题独立一行
                              ← 空一行
**[HH:MM:SS.ss] 句子**        ← 时间戳行（同一句组多行相邻、之间不空行）
- **[标签]** 中文解释          ← 注释列表紧跟其后
---                           ← 每个句块以单独一行 --- 结束

硬性规则：
- 段标题（## 开头）之后如需导读文字，导读段落后必须紧跟一行 ---，再开始第一个句块。
- 时间戳行与其注释之间不得插入任何其它文字。
- 时间戳必须与转录文件逐字符一致（含小数秒），构建器靠它匹配音频。
- 每个句块必须以 --- 结尾，包括段内最后一个。

【转录已精整】输入的转录已经过重断句整理：每行是一个完整句子或完整意群，说话人不混行。
因此：
- 一行 = 一个句块 = 一个播放器，**不要**再合并或拆分行。
- 仅当发现极少数残留半句（段边界切断）时，才用相邻无空行 + 理顺 blockquote 标注：
  > （理顺：完整句子...）

【注释规范】
- 对每个句子用以下标签注释（中文解释）：
  **[填充语]** uh/um/like/you know 等口头停顿
  **[B2词汇]** CEFR B2 级词汇
  **[C1词汇]** CEFR C1 级词汇
  **[C2词汇/习语]** C2 级词汇或习语
  **[职场表达]** 职场/商务常用说法
  **[言外之意]** 说话人的隐含意图
  **[语法点]** 值得关注的语法现象
  **[发言人切换]** 新说话人开始（如有说话人标签）
  **[文化背景]** 理解所需的文化/背景知识

- **整句只是填充语/语气词时，直接省略整块**：若某行英文去掉标点后只剩 um/uh/uhm/er/ah/oh/hmm/mm/well/so/right/okay/yeah/yep/no/like/now/anyway/you know/i mean/sort of 这类词（不含任何实义词），**不要**为它生成时间戳块和注释——跳过该行，或把它并入相邻的实义句一起讲。只有当这些词出现在一个**有实义内容的句子里**时，才用 [填充语] 标签顺带点出其功能。
- **禁止元注释**：任何句子都不要写"这句意义不大 / 无实质内容 / 无教学价值 / 纯填充，跳过"之类评价。若判断没有教学价值 → 直接整块省略；若判断有价值 → 给出具体注释。二者必居其一，不要保留一个只说"没价值"的空壳块。`

// Phase: intro
phase('Intro')
const intro = await agent(
  `${SYSTEM_PROMPT}

为这门课程写一个引言（约 600 字中文），包含：
1. 课程介绍（这段素材是什么、来自哪里）
2. 如何使用本课程（三步学习法）
3. 背景知识（说话人是谁、讨论什么主题）
4. 听力难点预警（口音、语速、口语特征）
5. 词汇级别说明（B2/C1/C2）

用 # 和 ## 分级标题组织内容。引言中**不得出现 **[时间戳]** 格式的行**（构建器会把它误认为句块）。`,
  { label: 'intro', phase: 'Intro', schema: INTRO_SCHEMA }
)

// Phase: annotate — 每段一个 agent，各自读自己那段的小文件（不把原文塞进 args）
// 若用户在第 1.5 步选了 Sonnet 5 省成本 → 给每个 agent 加 model: 'sonnet'；
// 否则删掉 model 用默认。agentType: 'general-purpose' 保证 agent 有 Read/Bash 能读文件。
phase('Annotate')
const segment_results = await parallel(
  A.segs.map((seg) => () => agent(
    `${SYSTEM_PROMPT}

请先读取文件：${A.dir}/segs/seg${seg.seg}.txt（用 Read 工具或 bash \`cat\`）。
文件每行格式为 [HH:MM:SS.ss] 英文原文（精整稿：每行一个完整句/意群），
是第 ${seg.seg} 段（${seg.start_fmt}–${seg.end_fmt}）共 ${seg.n} 句。

读到内容后按【结构模式】输出（严格遵守）：
## 第${seg.seg}段：${seg.start_fmt}–${seg.end_fmt}

对每一保留的行（时间戳与文件完全一致，含小数秒）：
**[HH:MM:SS.ss] 原文句子**
- **[标签]** 中文解释
...

每个句子块后加一行 ---

注意：
- 纯填充语行按规范省略（不输出其时间戳块）
- 输入已是精整稿，不要合并或拆分行
- 输出只含 Markdown，不含任何解释性前缀（不要写"我读取了文件"之类）`,
    { label: `Seg ${seg.seg}/${A.segs.length}`, phase: 'Annotate', schema: SEG_SCHEMA,
      model: 'sonnet', agentType: 'general-purpose' }
  ))
)

// Phase: compile
phase('Compile')
const valid = segment_results.filter(Boolean)
const course_markdown = [
  `# ${A.title} — 英语听力精讲课程`,
  `> **目标学员**：${A.level}`,
  '',
  intro ? intro.markdown : '',
  '',
  ...valid.map(r => r.markdown),
].join('\n\n')

return { markdown: course_markdown, segments_annotated: valid.length }
```

**调用方式**：Workflow 的 `args` 传 `{ dir: "<OUT_DIR 绝对路径>", segs: <segs_meta.json 内容>, title, level, total_duration }`（segs 很小，直接内联；原文由各 agent 自读文件）。

**取回结果**：返回的 `result.markdown` 很大（长课程 20–30 万字符），**不要**把它读进上下文。用 python 从 Workflow 的任务输出文件里提取 `result.markdown` 写入 `$OUT_DIR/course.md`：
```python
import json, pathlib
d = json.loads(pathlib.Path("<workflow task .output 路径>").read_text())
pathlib.Path("$OUT_DIR/course.md").write_text(d["result"]["markdown"])
```

### 第 3 步：剪辑音频 + 构建 HTML + 打包单文件 + 归集

```bash
# cut-audio 检测到 sentences.json 时自动按真实句边界剪辑（间隙感知：
# 句音频从本句真实起点开始，段音频跳过被删的广告/离题区间）
uv run python scripts/make_course.py cut-audio "$OUT_DIR"
uv run python scripts/make_course.py build-html "$OUT_DIR"
# 把被引用音频内嵌进 HTML → 自包含单文件（双击即开，无需解压/无 audio 目录）
uv run python scripts/make_course.py inline "$OUT_DIR"
# 归集成品单文件到 <项目根>/课程/（默认移动）
uv run python scripts/make_course.py collect "$OUT_DIR"
```

**构建后自检**（一条 python 快查，防结构回归）：
- `<main>` 内第一个 `sent-line` 必须出现在第一个 `<h1` 之后（播放器不得在标题之前）；
- 每个 `<h2>第N段` 之后应紧跟 `seg-player`。

### 第 4 步：报告结果

完成后，向用户报告：
- 成品单文件路径（已归集到 `课程/`，双击即开）
- 总句子数、分段数、丢弃的内容（resegment 的 ✂ 列表）
- 单文件大小

### 第 5 步：清理中间产物（询问后执行）

本工作流的产物分三类：**成品**（`*_单文件.html`，已归集）／**重建源**（`course.md`、`transcript.txt`、`transcript_raw.txt`、`refined.txt`、`sentences.json`、`words.json`、`segments.json`，小、保留以便日后改注释重跑）／**中间产物**（`audio/`、`*_portable.zip`、非内联 `listening_course.html`、`segs/`、`full.*` 等，可删）。

提示用户可运行（**保守**，保留重建源）：
```bash
uv run python scripts/make_course.py clean "$OUT_DIR" --drop-standalone   # 单文件已归集，docs 副本可删
# 预览用 --dry-run；连重建源也清用 --all
```

---

## 版权提醒

每次运行完成后，提醒用户：
- 本工具生成的课程仅供个人学习使用
- 商业发布需要原视频版权方授权
- 如视频描述中有 CC BY 等声明，在报告中注明

## 错误处理

- `yt-dlp` 失败（地区限制、需登录）→ 提示用户手动下载音频放入 `$OUT_DIR/full.mp3`（WAV 会由 download/transcribe 步骤从它提取）
- 转录为空 → 检查 WAV 格式，提示用 `ffprobe` 验证
- resegment 覆盖率 < 85% → refined.txt 改写过多，检查后重跑（只重组不改写）
- Workflow 超时 → 建议将 `--segment-minutes` 调大（减少分段数量）
- 旧课程目录无 words.json → 删除 transcript.txt 重跑 transcribe 才能走重断句流程
