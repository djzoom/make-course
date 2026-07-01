# /make-course

将 YouTube 视频自动制作为英语听力精讲课程（含逐句音频播放器与中文注释）。

## Input

`$ARGUMENTS` 格式：`<youtube-url> [--out-dir <path>] [--level <描述>] [--title <课程标题>]`

示例：
```
/make-course https://www.youtube.com/watch?v=lBVtvOpU80Q
/make-course https://youtu.be/xxx --out-dir ~/Desktop/my_course --title "产品营销周会"
```

## Instructions

当用户运行此命令时，执行以下完整流水线（从仓库根目录运行所有命令）：

### 第 0 步：解析参数

从 `$ARGUMENTS` 中提取：
- `URL`：YouTube 链接（必填）
- `OUT_DIR`：输出目录（默认 `./course_$(date +%Y%m%d_%H%M%S)`，或使用视频 ID）
- `LEVEL`：目标学员描述（默认 `"B2→C1"`）
- `TITLE`：课程标题（默认从视频元数据获取）

### 第 1 步：下载 + 转录

使用 Bash 运行：
```bash
# 获取视频 ID 作为默认目录名
VIDEO_ID=$(yt-dlp --get-id "$URL" 2>/dev/null)
OUT_DIR="${OUT_DIR:-./course_$VIDEO_ID}"

uv run python scripts/make_course.py download "$URL" --out-dir "$OUT_DIR"
uv run python scripts/make_course.py transcribe "$OUT_DIR/full.wav" --out-dir "$OUT_DIR"
```

读取生成的 `$OUT_DIR/segments.json` 以了解分段数量和时间范围。
读取 `$OUT_DIR/transcript.txt` 获取完整转录内容。
用 yt-dlp 获取视频标题：`yt-dlp --get-title "$URL"`

### 第 2 步：AI 课程内容生成（核心步骤）

调用 Workflow 工具，传入以下脚本，用并行 Agent 为每个分段生成注释：

```javascript
export const meta = {
  name: 'make-course-content',
  description: 'Generate annotated English listening course from YouTube transcript',
  phases: [
    { title: 'Intro', detail: 'Generate course introduction' },
    { title: 'Annotate', detail: 'Parallel segment annotation' },
    { title: 'Compile', detail: 'Merge and finalize course markdown' },
  ],
}

// args: { segments, title, level, subject }

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

const SYSTEM_PROMPT = `你是英语听力课程编写专家，专注于为中文母语者制作英语听力精讲教材。
目标学员：${args.level}
课程素材：${args.title}

注释规范：
- 每个句子块格式：
  **[HH:MM:SS.ss] 原文句子**（保持原始时间戳完全一致）
  - **[标签]** 中文解释

- 使用以下标签（必须用双星号粗体格式）：
  **[填充语]** uh/um/like/you know/I mean 等口头停顿词
  **[B2词汇]** CEFR B2 级词汇
  **[C1词汇]** CEFR C1 级词汇
  **[C2词汇/习语]** C2 级词汇或固定习语
  **[职场表达]** 职场/商务/演讲常用说法
  **[言外之意]** 说话人的隐含意图或言外之意
  **[语法点]** 值得关注的语法结构或用法
  **[发言人切换]** 新说话人出现时标注
  **[文化背景]** 理解所需的文化/背景知识
  **[ASR错误]** 明显的语音识别错误，格式：~~错误词~~ → 正确词

- 连续相邻的时间戳行属于同一句话时：不加空行，注释前加：
  > （理顺：完整句子...）

## 重要：以下类型的句子直接跳过
- 纯停顿/填充词单独出现（Uh. / Um. / Yeah. / So. / OK.）
- 单独出现的短残句（So uh. / I mean. / Well.）
- 明显的 ASR 识别错误（乱码）

- 每个句子块注释完后加 ---（三短横分隔线）
- 输出只含 Markdown，不含任何解释前缀或总结`

phase('Intro')
const intro = await agent(
  `${SYSTEM_PROMPT}

为这门课程写一个引言（约 800 字中文），包含：

# ${args.title} — 英语听力精讲课程

> **目标学员**：${args.level}  **素材**：${args.subject || args.title}

## 1. 课程介绍
## 2. 如何使用本课程（三步学习法：盲听 → 精学 → 再听）
## 3. 背景知识
## 4. 听力难点预警
## 5. 词汇级别说明（B2 / C1 / C2）

---`,
  { label: 'intro', phase: 'Intro', schema: INTRO_SCHEMA }
)

phase('Annotate')
const segment_results = await parallel(
  args.segments.map((seg, i) => () => {
    const sentences = seg.sentences
      .map(s => {
        const h = Math.floor(s.end_sec / 3600).toString().padStart(2, '0')
        const m = Math.floor((s.end_sec % 3600) / 60).toString().padStart(2, '0')
        const sc = (s.end_sec % 60).toFixed(2).padStart(5, '0')
        return `[${h}:${m}:${sc}] ${s.text}`
      })
      .join('\n')

    return agent(
      `${SYSTEM_PROMPT}

这是第 ${seg.seg}/${args.segments.length} 段（${seg.start_fmt}–${seg.end_fmt}），共 ${seg.sentences.length} 句：

${sentences}

输出格式（严格遵守）：

## 第${seg.seg}段：${seg.start_fmt}–${seg.end_fmt}

[对每一行逐句注释，保持时间戳格式与输入完全一致]

---`,
      {
        label: `Seg ${seg.seg}/${args.segments.length}`,
        phase: 'Annotate',
        schema: SEG_SCHEMA
      }
    )
  })
)

phase('Compile')
const valid = segment_results.filter(Boolean)
const course_markdown = [
  intro ? intro.markdown : `# ${args.title} — 英语听力精讲课程`,
  '',
  ...valid.map(r => r.markdown),
].join('\n\n')

return { markdown: course_markdown, segments_annotated: valid.length }
```

将 Workflow 返回的 `result.markdown` 写入 `$OUT_DIR/course.md`。

### 第 3 步：剪辑音频 + 构建 HTML + 打包

```bash
uv run python scripts/make_course.py cut-audio "$OUT_DIR"
uv run python scripts/make_course.py build-html "$OUT_DIR" --source-url "$URL"
uv run python scripts/make_course.py package "$OUT_DIR"
```

### 第 4 步：报告结果

完成后，向用户报告：
- 输出目录路径
- HTML 文件路径（可直接用浏览器打开）
- ZIP 文件路径（可带走分享的便携包）
- 总句子数、分段数
- 文件大小

---

## 版权提醒

每次运行完成后，提醒用户：
- 本工具生成的课程仅供个人学习使用
- 商业发布需要原视频版权方授权
- 如视频描述中有 CC BY 等声明，在报告中注明

## 错误处理

- `yt-dlp` 失败（地区限制、需登录）→ 提示用户手动下载后放入 `$OUT_DIR/video.mp4`，再手动提取 WAV：
  `ffmpeg -i video.mp4 -vn -ar 16000 -ac 1 full.wav`
- 转录为空 → 检查 WAV 格式，提示用 `ffprobe full.wav` 验证
- 分段句子数 > 150 → 建议将 `--segment-minutes` 调小（如 `--segment-minutes 4`）
