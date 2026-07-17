# make-course

将 YouTube 视频自动制作为**英语听力精讲课程**：逐句 MP3 播放器 + AI 中文注释，输出为可离线分享的 HTML 文件。

> **运行环境**：macOS + Apple Silicon（M 系列芯片）  
> **AI 工具**：[Claude Code](https://claude.ai/code)（需安装）

---

## 效果示例

生成的 HTML 课程包含：
- 按段落组织的时间轴
- 每句话可单独播放的音频按钮（按 AI 精整后的**真实句边界**剪辑）
- AI 生成的词汇注释（B2/C1/C2）、语法点、言外之意、文化背景
- 可内嵌音频输出**自包含单文件 HTML**（双击即开，手机/Telegram 可播），或打包为 ZIP 随时带走（离线可用）

---

## 安装

```bash
# 1. 克隆本仓库
git clone https://github.com/djzoom/make-course.git
cd make-course

# 2. 安装系统依赖
brew install ffmpeg
# yt-dlp 破解 YouTube 限速需要 JS 运行时（deno 或 node 任一即可）
brew install deno

# 3. 安装 Python 依赖（需要 uv）
pip install uv   # 或 brew install uv
uv sync

# 4. 安装 Claude Code CLI
npm install -g @anthropic-ai/claude-code
```

---

## 使用方法

在 Claude Code 中打开本仓库目录，然后运行 `/make-course` 命令：

```
/make-course https://www.youtube.com/watch?v=VIDEO_ID
```

可选参数：
```
/make-course <url> --title "我的课程" --level "B2→C1" --out-dir ~/Desktop/my_course --mode theme|meeting
```

`--mode` 决定内容筛选强度：`theme`（演讲/纪录片/讲座等）只保留主体内容，删广告、赞助口播、片头片尾；`meeting`（会议/访谈/播客等）全部保留，填充词和口误本身就是实战听力的训练价值。未指定时自动推断并报告。

### 流水线步骤

| 步骤 | 工具 | 时间（60分钟视频） |
|------|------|------------------|
| 下载音轨（audio-only） | yt-dlp | ~1 分钟 |
| 语音转文字（含词级时间戳） | Whisper large-v3-turbo | ~5–15 分钟 |
| 成本预估（超阈值先确认） | Python | <1 分钟 |
| 整理重断句 + 内容筛选 | Claude（并行） | ~5 分钟 |
| 词级时间戳对齐（resegment） | Python | <1 分钟 |
| AI 逐句注释 | Claude（并行） | ~10 分钟 |
| 剪辑音频片段（真实句边界、间隙感知） | ffmpeg（8线程） | ~5 分钟 |
| 生成 HTML + 内嵌音频单文件 | Python | ~1 分钟 |

> **为什么要整理重断句**：Whisper 按语音停顿切割，切出的行常是半句甚至两人各半句。先由 AI 整理成完整句/意群（只重组不改写），再按词级时间戳对齐回真实句边界，句播放器听到的才是完整句子。

---

## 输出目录结构

```
course_VIDEO_ID/
├── full.mp3
├── full.wav
├── transcript.txt          # [HH:MM:SS.ss] sentence（resegment 后为精整稿）
├── transcript_raw.txt      # 原始 Whisper 转录备份
├── words.json              # 词级时间戳（resegment 的对齐依据）
├── refined.txt             # AI 整理重断句稿（每行一个完整句/意群）
├── sentences.json          # 每句真实 start/end（resegment 产出）
├── segments.json           # 分段元信息
├── segs/                   # 按段拆分的转录（供并行注释 agent 各读各段）
├── course.md               # AI 生成的注释 Markdown
├── listening_course.html   # 课程 HTML（引用 audio/ 目录）
├── *_单文件.html            # 自包含单文件（音频已内嵌，双击即开）
└── audio/
    ├── seg1.mp3 … segN.mp3
    └── sentences/
        ├── s001.mp3
        ├── s002-003.mp3    # 跨行合并句组
        └── …
```

成品单文件默认归集到 `课程/` 目录；中间产物可用 `clean` 子命令清理（默认保留 course.md 等重建源）。

---

## 版权说明

本工具生成的课程**仅供个人学习使用**。  
商业发布需要原视频版权方授权。建议优先使用：
- 公有领域内容（1928年前的录音）
- CC BY / CC0 授权内容
- 美国联邦政府官方视频（公有领域）
- 经版权方书面授权的内容

---

## 命令行直接使用（不通过 Claude Code）

如果只需要非 AI 步骤（下载、剪音频、建 HTML），可以直接调用：

```bash
# 下载（只下音轨，更快更小）
uv run python scripts/make_course.py download "https://youtu.be/..." --out-dir ./course_out

# 转录（Apple Silicon 必需；产出词级时间戳 words.json）
uv run python scripts/make_course.py transcribe ./course_out/full.wav --out-dir ./course_out

# 预估 AI 注释 token/成本（注释之前跑）
uv run python scripts/make_course.py estimate ./course_out

# AI 整理稿对齐词级时间戳（需先有 refined.txt，由 /make-course 的 AI 步骤生成）
uv run python scripts/make_course.py resegment ./course_out

# 剪辑音频（需先有 course.md；有 sentences.json 时按真实句边界、间隙感知剪辑）
uv run python scripts/make_course.py cut-audio ./course_out

# 生成 HTML
uv run python scripts/make_course.py build-html ./course_out --source-url "https://youtu.be/..."

# 内嵌音频 → 自包含单文件 HTML（双击即开）
uv run python scripts/make_course.py inline ./course_out

# 或打包 ZIP（HTML + audio/ 目录）
uv run python scripts/make_course.py package ./course_out

# 归集成品单文件到 课程/ 目录；清理中间产物
uv run python scripts/make_course.py collect ./course_out
uv run python scripts/make_course.py clean ./course_out --dry-run
```

其余辅助子命令：`split-segs`（按段拆分转录供并行 agent）、`fix-audio`（给旧单文件注入移动端播放修复）、`all`（download + transcribe 一步跑完）。

---

## License

MIT © djzoom
