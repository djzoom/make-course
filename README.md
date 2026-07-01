# make-course

将 YouTube 视频自动制作为**英语听力精讲课程**：逐句 MP3 播放器 + AI 中文注释，输出为可离线分享的 HTML 文件。

> **运行环境**：macOS + Apple Silicon（M 系列芯片）  
> **AI 工具**：[Claude Code](https://claude.ai/code)（需安装）

---

## 效果示例

生成的 HTML 课程包含：
- 按段落组织的时间轴
- 每句话可单独播放的音频按钮
- AI 生成的词汇注释（B2/C1/C2）、语法点、言外之意、文化背景
- 可打包为 ZIP 随时带走（离线可用）

---

## 安装

```bash
# 1. 克隆本仓库
git clone https://github.com/djzoom/make-course.git
cd make-course

# 2. 安装系统依赖
brew install ffmpeg

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
/make-course <url> --title "我的课程" --level "B2→C1" --out-dir ~/Desktop/my_course
```

### 流水线步骤

| 步骤 | 工具 | 时间（60分钟视频） |
|------|------|------------------|
| 下载视频 | yt-dlp | ~1 分钟 |
| 语音转文字 | Whisper large-v3-turbo | ~5 分钟 |
| AI 逐句注释 | Claude（并行） | ~10 分钟 |
| 剪辑音频片段 | ffmpeg（8线程） | ~5 分钟 |
| 生成 HTML | Python | <1 分钟 |
| 打包 ZIP | Python | <1 分钟 |

---

## 输出目录结构

```
course_VIDEO_ID/
├── video.mp4
├── full.mp3
├── full.wav
├── transcript.txt          # [HH:MM:SS.ss] sentence 格式
├── segments.json           # 分段元信息
├── course.md               # AI 生成的注释 Markdown
├── listening_course.html   # 最终课程（可直接用浏览器打开）
├── *_portable.zip          # 便携包（含 HTML + 音频，约 50MB/小时）
└── audio/
    ├── seg1.mp3 … segN.mp3
    └── sentences/
        ├── s001.mp3
        ├── s002-003.mp3    # 跨行合并句组
        └── …
```

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
# 下载
uv run python scripts/make_course.py download "https://youtu.be/..." --out-dir ./course_out

# 转录（Apple Silicon 必需）
uv run python scripts/make_course.py transcribe ./course_out/full.wav --out-dir ./course_out

# 剪辑音频（需先有 course.md）
uv run python scripts/make_course.py cut-audio ./course_out

# 生成 HTML
uv run python scripts/make_course.py build-html ./course_out --source-url "https://youtu.be/..."

# 打包 ZIP
uv run python scripts/make_course.py package ./course_out
```

---

## License

MIT © djzoom
