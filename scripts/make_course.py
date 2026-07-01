#!/usr/bin/env python3
"""
make_course.py — YouTube 听力教材流水线（非 AI 步骤）

子命令:
  download   <url>        下载视频，提取 MP3 + WAV
  transcribe <wav>        Nemotron 流式转录 → transcript.txt + segments.json
  cut-audio  <dir>        按句子 / 分组切 MP3（8 线程并行）
  build-html <dir>        将 course.md 转换为 listening_course.html
  all        <url>        依次执行全部步骤（AI 注释由外部提供 course.md）

输出目录结构:
  <out-dir>/
    video.mp4
    full.mp3
    full.wav
    transcript.txt          # [HH:MM:SS.ss] sentence
    segments.json           # 分段元信息（供 Claude Workflow 使用）
    course.md               # 【由 /make-course Claude 命令生成】
    listening_course.html   # 最终输出
    audio/
      seg1.mp3 … seg7.mp3  # 分段音频（供段落播放器）
      sentences/
        s001.mp3 … sNNN.mp3
        s002-003.mp3 …     # 合并句组

用法:
  uv run python scripts/make_course.py download "https://youtu.be/..." --out-dir ./course_out
  uv run python scripts/make_course.py transcribe ./course_out/full.wav --out-dir ./course_out
  uv run python scripts/make_course.py cut-audio ./course_out
  uv run python scripts/make_course.py build-html ./course_out
"""

import argparse, bisect, concurrent.futures, json, pathlib, re, subprocess, sys, zipfile

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def run(cmd, **kw):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        sys.exit(f"Command failed (exit {r.returncode})")
    return r


def ts_sec(ts: str) -> float:
    p = ts.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])


def fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def load_transcript(path: pathlib.Path):
    """Return [(end_sec, idx, text), ...]  sorted by end_sec."""
    entries = []
    for line in path.read_text().splitlines():
        m = re.match(r"\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s+(.*)", line.strip())
        if m:
            entries.append((ts_sec(m.group(1)), len(entries) + 1, m.group(2)))
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: download
# ─────────────────────────────────────────────────────────────────────────────

def cmd_download(args):
    url   = args.url
    outd  = pathlib.Path(args.out_dir)
    outd.mkdir(parents=True, exist_ok=True)

    video = outd / "video.mp4"
    mp3   = outd / "full.mp3"
    wav   = outd / "full.wav"

    print(f"\n=== Phase 1: Download ===")
    if not video.exists():
        run(["yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
             "--merge-output-format", "mp4",
             "-o", str(video), url])
    else:
        print(f"  ✓ {video} already exists")

    if not mp3.exists():
        run(["ffmpeg", "-y", "-i", str(video), "-vn",
             "-acodec", "libmp3lame", "-q:a", "2", str(mp3)])
    else:
        print(f"  ✓ {mp3} already exists")

    if not wav.exists():
        run(["ffmpeg", "-y", "-i", str(video), "-vn",
             "-ar", "16000", "-ac", "1", str(wav)])
    else:
        print(f"  ✓ {wav} already exists")

    # Save URL for reference
    (outd / "source.txt").write_text(url + "\n")
    print(f"\n✓ Download complete → {outd}")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: transcribe
# ─────────────────────────────────────────────────────────────────────────────

def cmd_transcribe(args):
    wav  = pathlib.Path(args.wav)
    outd = pathlib.Path(args.out_dir)
    outd.mkdir(parents=True, exist_ok=True)

    transcript_path = outd / "transcript.txt"
    segments_path   = outd / "segments.json"

    print(f"\n=== Phase 2: Transcribe ({wav.name}) ===")

    if not transcript_path.exists():
        # Use mlx_whisper for standalone accuracy (no watch_transcribe dependency)
        print("  Running Whisper large-v3-turbo…")
        import mlx_whisper
        result = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
            language="en",
            word_timestamps=True,
            verbose=False,
        )

        # Write transcript.txt: one line per Whisper segment
        lines = []
        for seg in result["segments"]:
            end = seg["end"]
            h   = int(end // 3600)
            m   = int((end % 3600) // 60)
            s   = end % 60
            ts  = f"{h:02d}:{m:02d}:{s:06.3f}"
            text = seg["text"].strip()
            if text:
                lines.append(f"[{ts}] {text}")
        transcript_path.write_text("\n".join(lines))
        print(f"  ✓ {len(lines)} sentences → {transcript_path}")
    else:
        print(f"  ✓ {transcript_path} already exists")

    # Build segments.json: split transcript into ~5-min segments for AI agents
    entries = load_transcript(transcript_path)
    if not entries:
        sys.exit("Transcript is empty or couldn't be parsed.")

    total_dur = entries[-1][0]
    seg_dur   = args.segment_minutes * 60
    seg_count = max(1, round(total_dur / seg_dur))
    boundary  = total_dur / seg_count

    segments = []
    bucket   = []
    seg_idx  = 1
    for end_sec, idx, text in entries:
        bucket.append({"idx": idx, "end_sec": end_sec, "text": text})
        if end_sec >= seg_idx * boundary and seg_idx < seg_count:
            segments.append({
                "seg": seg_idx,
                "start_sec": (segments[-1]["end_sec"] if segments else 0),
                "end_sec":   end_sec,
                "start_fmt": fmt_ts(segments[-1]["end_sec"] if segments else 0),
                "end_fmt":   fmt_ts(end_sec),
                "sentences": bucket[:],
            })
            bucket = []
            seg_idx += 1
    if bucket:
        segments.append({
            "seg": seg_idx,
            "start_sec": (segments[-1]["end_sec"] if segments else 0),
            "end_sec":   entries[-1][0],
            "start_fmt": fmt_ts(segments[-1]["end_sec"] if segments else 0),
            "end_fmt":   fmt_ts(entries[-1][0]),
            "sentences": bucket,
        })

    segments_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2))
    print(f"  ✓ {len(segments)} segments → {segments_path}")
    for s in segments:
        print(f"    Seg {s['seg']:2d}: {s['start_fmt']}–{s['end_fmt']} ({len(s['sentences'])} sentences)")

    print(f"\n✓ Transcription complete → {outd}")
    return segments


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: cut audio
# ─────────────────────────────────────────────────────────────────────────────

def cmd_cut_audio(args):
    outd = pathlib.Path(args.out_dir)
    wav  = outd / "full.wav"
    md   = outd / "course.md"

    transcript_path = outd / "transcript.txt"
    sent_dir = outd / "audio" / "sentences"
    seg_dir  = outd / "audio"
    sent_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Phase 3: Cut Audio ===")

    entries = load_transcript(transcript_path)
    ts_secs = [e[0] for e in entries]

    def nearest(sec):
        pos  = bisect.bisect_left(ts_secs, sec)
        best = None
        for p in [max(0, pos - 1), min(len(ts_secs) - 1, pos)]:
            d = abs(ts_secs[p] - sec)
            if best is None or d < best[0]:
                best = (d, entries[p])
        return best[1] if best and best[0] <= 4.0 else None

    LEAD, TAIL = 0.2, 0.4

    def ffcut(src, start, end, out):
        return subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-ss", f"{max(0.0, start):.4f}", "-to", f"{end:.4f}",
             "-acodec", "libmp3lame", "-q:a", "5", "-loglevel", "error", str(out)],
            capture_output=True,
        ).returncode == 0

    # --- Individual sentence clips ---
    tasks = []
    for i, (ts, idx, text) in enumerate(entries, 1):
        prev_ts = entries[i - 2][0] if i > 1 else 0.0
        out     = sent_dir / f"s{idx:03d}.mp3"
        tasks.append((prev_ts - LEAD, ts + TAIL, out))

    print(f"  Cutting {len(tasks)} sentence clips…")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(ffcut, wav, s, e, o) for s, e, o in tasks]
        ok = sum(f.result() for f in concurrent.futures.as_completed(futs))
    print(f"  ✓ {ok}/{len(tasks)} individual clips")

    # --- Merged group clips (from course.md consecutive timestamp blocks) ---
    if md.exists():
        _cut_merged_groups(md, wav, sent_dir, entries, nearest, LEAD, TAIL)

    # --- Segment clips (for section players) ---
    segments_path = outd / getattr(args, "segments_file", "segments.json")
    if segments_path.exists():
        segs = json.loads(segments_path.read_text())
        seg_tasks = []
        for s in segs:
            start = (segs[s["seg"] - 2]["end_sec"] if s["seg"] > 1 else 0.0)
            end   = s["end_sec"]
            out   = seg_dir / f"seg{s['seg']}.mp3"
            seg_tasks.append((start - LEAD, end + TAIL, out))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(ffcut, wav, s, e, o) for s, e, o in seg_tasks]
            ok = sum(f.result() for f in concurrent.futures.as_completed(futs))
        print(f"  ✓ {ok}/{len(seg_tasks)} segment clips")

    print(f"\n✓ Audio cut complete → {sent_dir}")


def _cut_merged_groups(md_path, wav, sent_dir, entries, nearest, LEAD, TAIL):
    TS_ANY = re.compile(r"^\*\*\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s*(.*?)\*\*\s*$")
    tasks  = []
    cur_ts = []

    def flush_group():
        if len(cur_ts) < 2:
            cur_ts.clear()
            return
        resolved = [nearest(sec) for sec, _ in cur_ts]
        resolved = [e for e in resolved if e]
        if len(resolved) < 2:
            cur_ts.clear()
            return
        first, last = resolved[0], resolved[-1]
        first_idx, last_idx = first[1], last[1]
        prev_ts = entries[first_idx - 2][0] if first_idx > 1 else 0.0
        out = sent_dir / f"s{first_idx:03d}-{last_idx:03d}.mp3"
        tasks.append((prev_ts - LEAD, last[0] + TAIL, out))
        cur_ts.clear()

    in_ann = False
    for line in md_path.read_text().splitlines():
        stripped = line.strip()
        m = TS_ANY.match(stripped)
        if stripped == "---":
            flush_group()
            in_ann = False
        elif m:
            if in_ann:
                flush_group()
                in_ann = False
            cur_ts.append((ts_sec(m.group(1)), m.group(2)))
        else:
            if cur_ts:
                in_ann = True
    flush_group()

    def ffcut(start, end, out):
        return subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav),
             "-ss", f"{max(0.0, start):.4f}", "-to", f"{end:.4f}",
             "-acodec", "libmp3lame", "-q:a", "5", "-loglevel", "error", str(out)],
            capture_output=True,
        ).returncode == 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(ffcut, *t) for t in tasks]
        ok = sum(f.result() for f in concurrent.futures.as_completed(futs))
    print(f"  ✓ {ok}/{len(tasks)} merged group clips")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: build HTML
# ─────────────────────────────────────────────────────────────────────────────

def cmd_build_html(args):
    import markdown as md_lib

    outd = pathlib.Path(args.out_dir)
    md   = outd / "course.md"
    html_out = outd / "listening_course.html"
    transcript_path = outd / "transcript.txt"

    print(f"\n=== Phase 4: Build HTML ===")
    if not md.exists():
        sys.exit(f"course.md not found in {outd}. Run AI generation first.")

    entries = load_transcript(transcript_path)
    ts_secs = [e[0] for e in entries]

    def nearest(sec):
        pos  = bisect.bisect_left(ts_secs, sec)
        best = None
        for p in [max(0, pos - 1), min(len(ts_secs) - 1, pos)]:
            d = abs(ts_secs[p] - sec)
            if best is None or d < best[0]:
                best = (d, entries[p])
        return best[1] if best and best[0] <= 4.0 else None

    # --- Parse markdown blocks ---
    TS_ANY = re.compile(r"^\*\*\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s*(.*?)\*\*\s*$")
    TAG_SUBS = [
        (r"\*\*\[填充语[^\]]*\]\*\*",        "tag-filler",  "填充语"),
        (r"\*\*\[缓和语[^\]]*\]\*\*",        "tag-filler",  "缓和语"),
        (r"\*\*\[填充语\s*/\s*缓和语[^\]]*\]\*\*", "tag-filler", "填充语/缓和语"),
        (r"\*\*\[B2词汇[^\]]*\]\*\*",        "tag-b2",      "B2"),
        (r"\*\*\[C1词汇[^\]]*\]\*\*",        "tag-c1",      "C1"),
        (r"\*\*\[C1/C2词汇[^\]]*\]\*\*",     "tag-c2",      "C1/C2"),
        (r"\*\*\[C2词汇/习语[^\]]*\]\*\*",   "tag-c2",      "C2/习语"),
        (r"\*\*\[C2词汇[^\]]*\]\*\*",        "tag-c2",      "C2"),
        (r"\*\*\[职场[^\]]*\]\*\*",          "tag-work",    "职场表达"),
        (r"\*\*\[言外之意[^\]]*\]\*\*",      "tag-intent",  "言外之意"),
        (r"\*\*\[语法点[^\]]*\]\*\*",        "tag-grammar", "语法点"),
        (r"\*\*\[发言人切换[^\]]*\]\*\*",    "tag-spk",     "发言人切换"),
        (r"\*\*\[文化背景[^\]]*\]\*\*",      "tag-culture", "文化背景"),
        (r"\*\*\[演讲[^\]]*\]\*\*",          "tag-work",    "演讲表达"),
        (r"\*\*\[ASR错误[^\]]*\]\*\*",       "tag-asr",     "ASR错误"),
    ]

    FRAGS = {"uh.", "um.", "u.", "v.", "yeah.", "okay.", "ok.", "mm.", "hmm."}

    def is_frag(text):
        # Pure filler words
        if len(text.split()) <= 2 and text.lower().strip(" .,?!") in {x.strip(".") for x in FRAGS}:
            return True
        # Very short with no annotation (catches dramatic-pause sermon fragments)
        return len(text.split()) <= 3

    def has_ann(lines):
        return len("".join(lines).strip()) > 20

    # Annotations that explicitly flag a block as no-value → auto-remove
    SKIP_PHRASES = [
        "跳过", "无注释价值", "无学习价值", "纯填充，跳过", "纯内容，无",
        "无需注释", "无教学价值", "skip", "no annotation", "not worth annotating",
    ]

    def ann_says_skip(lines):
        joined = "".join(lines).lower()
        return any(p in joined for p in SKIP_PHRASES)

    blocks, cur_ts, cur_ann, in_ann = [], [], [], False

    def flush():
        nonlocal cur_ts, cur_ann, in_ann
        if cur_ts or cur_ann:
            blocks.append({"ts": cur_ts[:], "annot": cur_ann[:]})
        cur_ts, cur_ann, in_ann = [], [], False

    for line in md.read_text().splitlines():
        stripped = line.strip()
        m = TS_ANY.match(stripped)
        if stripped == "---":
            flush()
        elif m:
            if in_ann:
                flush()
            cur_ts.append((ts_sec(m.group(1)), m.group(2).strip()))
        else:
            if cur_ts:
                in_ann = True
            cur_ann.append(line)
    flush()

    # Mark kept/removed
    for b in blocks:
        if not b["ts"]:
            b["keep"] = True; b["type"] = "intro"
        elif not has_ann(b["annot"]) and all(is_frag(t) for _, t in b["ts"]):
            b["keep"] = False  # pure filler, no annotation
        elif ann_says_skip(b["annot"]):
            b["keep"] = False  # AI explicitly flagged as no-value
        else:
            b["keep"] = True
            b["type"] = "merged" if len(b["ts"]) > 1 else "single"

    # Build pre-processed content
    TMPL = ('<p class="sent-line">'
            '<audio class="sent-audio" controls preload="none">'
            '<source src="{audio}" type="audio/mpeg"></audio>'
            '<span class="ts">[{ts}]</span> '
            '<span class="sent-en">{text}</span>'
            '</p>')

    def apply_tags(text):
        for pat, cls, lbl in TAG_SUBS:
            text = re.sub(pat, f'<span class="tag {cls}">{lbl}</span>', text)
        return text

    seg_info = []
    _segs_file = outd / getattr(args, "segments_file", "segments.json")
    if _segs_file.exists():
        segs = json.loads(_segs_file.read_text())
        seg_info = [(f"第{s['seg']}段", f"audio/seg{s['seg']}.mp3",
                     f"{s['start_fmt']}–{s['end_fmt']}") for s in segs]

    out_parts = []
    for b in blocks:
        if not b["keep"]:
            continue
        if b["type"] == "intro":
            out_parts.append(apply_tags("\n".join(b["annot"])))
            continue

        ts_list = b["ts"]
        resolved = [nearest(sec) for sec, _ in ts_list]
        resolved = [e for e in resolved if e]
        if not resolved:
            out_parts.append(apply_tags("\n".join(b["annot"])))
            continue

        first_idx = resolved[0][1]
        last_idx  = resolved[-1][1]
        if len(resolved) == 1:
            audio = f"audio/sentences/s{first_idx:03d}.mp3"
        else:
            audio = f"audio/sentences/s{first_idx:03d}-{last_idx:03d}.mp3"

        ts_s = ts_list[0][0]
        ts_display = fmt_ts(ts_s)
        combined   = " / ".join(t for _, t in ts_list)

        html_block = "\n" + TMPL.format(audio=audio, ts=ts_display, text=combined) + "\n"
        out_parts.append(html_block)
        if b["annot"]:
            out_parts.append(apply_tags("\n".join(b["annot"])))
        out_parts.append("\n---\n")

    processed = "\n".join(out_parts)

    # Add segment players after matching h2 headings
    body = md_lib.markdown(processed, extensions=["tables", "extra"])
    for label, src, time_range in seg_info:
        player = (f'<div class="seg-player">'
                  f'<span class="seg-label">🎧 {label} · {time_range}</span>'
                  f'<audio controls preload="none"><source src="{src}" type="audio/mpeg"></audio>'
                  f'</div>')
        body = re.sub(rf'(<h2[^>]*>[^<]*{re.escape(label)}[^<]*</h2>)', rf'\1\n{player}', body)

    # Add heading IDs and build TOC
    def add_id(m):
        lvl, content = m.group(1), m.group(2)
        text   = re.sub(r"<[^>]+>", "", content).strip()
        anchor = re.sub(r"[^\w一-鿿]", "-", text.lower()).strip("-")
        return f"<h{lvl} id=\"{anchor}\">{content}</h{lvl}>"
    body = re.sub(r"<h([1-4])>(.*?)</h\1>", add_id, body, flags=re.DOTALL)

    toc = '<nav class="sidebar"><h2>目录</h2>\n'
    for m in re.finditer(r'<h([1-4])[^>]*id="([^"]*)"[^>]*>(.*?)</h\1>', body, re.DOTALL):
        lvl, anchor, content = m.group(1), m.group(2), m.group(3)
        text = re.sub(r"<[^>]+>", "", content).strip()[:40]
        toc += f'<a href="#{anchor}" class="toc-h{lvl}">{text}</a>\n'
    toc += "</nav>"

    CSS = _css()
    title = md.read_text().splitlines()[0].lstrip("# ").strip() or "英语听力精讲课程"

    # Source video URL: from --source-url flag, else source.txt, else None
    source_url = getattr(args, "source_url", None) or None
    if not source_url:
        src_file = outd / "source.txt"
        if src_file.exists():
            source_url = src_file.read_text().strip()

    source_link = ""
    if source_url:
        source_link = (f'\n<div class="source-link">📺 原视频：'
                       f'<a href="{source_url}" target="_blank" rel="noopener">{source_url}</a></div>')

    final = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
{toc}
<main>
{source_link}
{body}
</main>
</body>
</html>"""

    html_out.write_text(final, encoding="utf-8")
    kb = html_out.stat().st_size // 1024
    print(f"  ✓ {html_out}  ({kb} KB, {final.count('sent-line')} sentence blocks)")
    print(f"\n✓ HTML built → {html_out}")


def _css():
    return """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:#f8f7f4; --surface:#fff; --sidebar:#16213e;
  --txt:#1c1c2e; --txt-soft:#4a4a6a;
  --accent:#2563eb; --accent2:#059669; --border:#e2e8f0; --r:8px;
  --mono:"JetBrains Mono","Fira Code","Courier New",monospace;
  --sans:-apple-system,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
}
body { font-family:var(--sans); font-size:15px; line-height:1.85;
       color:var(--txt); background:var(--bg); font-weight:400; }
.sidebar { position:fixed; top:0; left:0; width:240px; height:100vh;
           background:var(--sidebar); overflow-y:auto; padding:20px 12px;
           font-size:12px; z-index:100; }
.sidebar h2 { color:#60a5fa; font-size:11px; font-weight:700;
              letter-spacing:.12em; text-transform:uppercase; margin-bottom:12px; }
.sidebar a { color:#94a3b8; text-decoration:none; display:block;
             padding:4px 8px; border-radius:4px; line-height:1.5; }
.sidebar a:hover { background:#1e293b; color:#e2e8f0; }
.toc-h1 { font-weight:700; color:#e2e8f0; margin-top:12px; }
.toc-h2 { font-weight:600; color:#cbd5e1; margin-top:8px; padding-left:4px; }
.toc-h3 { padding-left:14px; }
.toc-h4 { padding-left:24px; font-size:11px; }
main { margin-left:240px; max-width:880px; padding:48px 52px;
       background:var(--surface); min-height:100vh; }
h1 { font-size:24px; font-weight:700; color:var(--txt);
     border-bottom:2px solid var(--accent); padding-bottom:12px; margin:40px 0 18px; }
h2 { font-size:17px; font-weight:700; color:#fff; background:var(--accent);
     padding:9px 15px; border-radius:var(--r); margin:44px 0 16px; }
h3 { font-size:14px; font-weight:600; color:var(--accent);
     margin:28px 0 8px; border-bottom:1px solid var(--border); padding-bottom:5px; }
h4 { font-size:13px; font-weight:600; color:var(--txt-soft); margin:20px 0 6px; }
p  { margin:8px 0; }
li { margin:5px 0; }
ul,ol { padding-left:22px; margin:8px 0; }
strong { font-weight:600; }
a  { color:var(--accent); }
hr { border:none; border-top:1px solid var(--border); margin:32px 0; }
p.sent-line { display:flex; align-items:flex-start; gap:10px;
  background:#f0f6ff; border-left:3px solid var(--accent);
  border-radius:0 var(--r) var(--r) 0; padding:10px 14px; margin:22px 0 4px; }
.ts { font-family:var(--mono); font-size:11px; font-weight:500;
      color:#94a3b8; white-space:nowrap; padding-top:4px; flex-shrink:0; }
.sent-en { font-size:17px; font-weight:600; color:#1e3a5f;
           font-family:Georgia,serif; line-height:1.5; flex:1; min-width:0; }
.sent-audio { height:28px; width:180px; flex-shrink:0; margin-top:3px; }
.tag { display:inline-block; font-size:11px; font-weight:700;
       padding:2px 9px; border-radius:20px; margin-right:4px;
       vertical-align:middle; white-space:nowrap; }
.tag-filler  { background:#fff3cd; color:#92400e; border:1px solid #fcd34d; }
.tag-b2      { background:#dbeafe; color:#1d4ed8; border:1px solid #93c5fd; }
.tag-c1      { background:#ede9fe; color:#6d28d9; border:1px solid #c4b5fd; }
.tag-c2      { background:#fef3c7; color:#b45309; border:1px solid #fbbf24; }
.tag-work    { background:#d1fae5; color:#065f46; border:1px solid #6ee7b7; }
.tag-intent  { background:#cffafe; color:#0e7490; border:1px solid #67e8f9; }
.tag-grammar { background:#fce7f3; color:#9d174d; border:1px solid #f9a8d4; }
.tag-spk     { background:#e0e7ff; color:#3730a3; border:1px solid #a5b4fc; }
.tag-culture { background:#fdf4ff; color:#7e22ce; border:1px solid #d8b4fe; }
.tag-asr     { background:#fee2e2; color:#991b1b; border:1px solid #fca5a5; }
code { font-family:var(--mono); font-size:13px; background:#f1f5f9; color:#0f172a;
       border:1px solid #e2e8f0; padding:1px 6px; border-radius:4px; }
pre  { background:#0f172a; color:#e2e8f0; padding:16px 20px;
       border-radius:var(--r); overflow-x:auto; margin:12px 0; font-size:13px; }
pre code { background:none; border:none; padding:0; color:inherit; }
blockquote { border-left:3px solid var(--accent2); background:#f0fdf4;
             padding:10px 18px; margin:10px 0;
             border-radius:0 var(--r) var(--r) 0; color:#065f46; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:14px; }
th { background:#1e3a5f; color:#fff; padding:8px 13px; font-weight:600;
     text-align:left; font-size:13px; }
td { padding:7px 13px; border:1px solid var(--border); vertical-align:top; }
tr:nth-child(even) td { background:#f8faff; }
.seg-player { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
              background:#1e3a5f; border-radius:var(--r);
              padding:12px 16px; margin:10px 0 26px; }
.seg-label { color:#93c5fd; font-size:12px; font-weight:600; flex-shrink:0; }
.seg-player audio { height:32px; flex:1; min-width:200px; }
.source-link { background:#eff6ff; border:1px solid #bfdbfe; border-radius:var(--r);
               padding:10px 16px; margin:0 0 24px; font-size:13px; color:#1d4ed8; }
.source-link a { color:#1d4ed8; word-break:break-all; }
@media (max-width:800px) {
  .sidebar { display:none; }
  main { margin-left:0; padding:20px 16px; }
  p.sent-line { flex-wrap:wrap; }
  .sent-audio { width:100%; }
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Package: portable ZIP (HTML + audio)
# ─────────────────────────────────────────────────────────────────────────────

def cmd_package(args):
    outd     = pathlib.Path(args.out_dir)
    html_src = outd / "listening_course.html"
    audio_dir = outd / "audio"

    if not html_src.exists():
        sys.exit(f"listening_course.html not found in {outd}. Run build-html first.")

    # ZIP name: slug from HTML title, fallback to dir name
    title_line = ""
    for line in html_src.read_text(encoding="utf-8").splitlines():
        m = re.search(r"<title>(.*?)</title>", line)
        if m:
            title_line = m.group(1)
            break
    slug = re.sub(r"[^\w一-鿿]+", "_", title_line).strip("_")[:40] or outd.name
    zip_path = pathlib.Path(getattr(args, "output", None) or (outd / f"{slug}_portable.zip"))

    print(f"\n=== Package: Creating portable ZIP ===")
    print(f"  Source HTML: {html_src}")
    print(f"  Audio dir:   {audio_dir}")
    print(f"  Output ZIP:  {zip_path}")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # HTML at top level of ZIP
        zf.write(html_src, "listening_course.html")

        # Audio directory (preserve relative structure; exclude full.mp3 source file)
        if audio_dir.exists():
            for f in sorted(audio_dir.rglob("*.mp3")):
                if f.name == "full.mp3":
                    continue
                arc_name = "audio/" + f.relative_to(audio_dir).as_posix()
                zf.write(f, arc_name)

        # README
        readme = (
            "英语听力精讲课程 - 使用说明\n"
            "================================\n\n"
            "1. 解压此 ZIP 到任意文件夹\n"
            "2. 双击 listening_course.html 用浏览器打开\n"
            "3. 确保 audio/ 文件夹与 HTML 文件在同一目录下\n\n"
            "兼容性：Chrome / Firefox / Edge / Safari（Windows / macOS / Linux）\n"
            "无需联网，无需安装任何软件。\n"
        )
        zf.writestr("README.txt", readme.encode("utf-8"))

    size_mb = zip_path.stat().st_size / 1024 / 1024
    mp3_count = sum(1 for _ in audio_dir.rglob("*.mp3")) if audio_dir.exists() else 0
    print(f"  ✓ {mp3_count} MP3 files packaged")
    print(f"  ✓ ZIP size: {size_mb:.1f} MB → {zip_path}")
    print(f"\n✓ Package complete → {zip_path}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="YouTube 听力教材流水线（非 AI 步骤）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dl = sub.add_parser("download", help="下载视频 + 提取 MP3/WAV")
    p_dl.add_argument("url")
    p_dl.add_argument("--out-dir", default="./course_out", dest="out_dir")

    p_tr = sub.add_parser("transcribe", help="转录 WAV → transcript.txt + segments.json")
    p_tr.add_argument("wav")
    p_tr.add_argument("--out-dir", default="./course_out", dest="out_dir")
    p_tr.add_argument("--segment-minutes", type=float, default=5.0, dest="segment_minutes")

    p_ca = sub.add_parser("cut-audio", help="剪辑句子 MP3")
    p_ca.add_argument("out_dir")
    p_ca.add_argument("--segments-file", default="segments.json", dest="segments_file",
                      help="指定段落文件（默认 segments.json，可用于布道课等过滤后文件）")

    p_bh = sub.add_parser("build-html", help="Markdown → HTML")
    p_bh.add_argument("out_dir")
    p_bh.add_argument("--segments-file", default="segments.json", dest="segments_file",
                      help="指定段落文件（默认 segments.json）")
    p_bh.add_argument("--source-url", default=None, dest="source_url",
                      help="原视频 URL（可选），嵌入到课程 HTML 顶部")

    p_pk = sub.add_parser("package", help="打包 HTML + 音频为可迁移 ZIP")
    p_pk.add_argument("out_dir")
    p_pk.add_argument("--output", default=None, dest="output",
                      help="ZIP 输出路径（默认：<title>_portable.zip）")

    p_al = sub.add_parser("all", help="下载 + 转录（AI 注释需单独运行 /make-course）")
    p_al.add_argument("url")
    p_al.add_argument("--out-dir", default="./course_out", dest="out_dir")
    p_al.add_argument("--segment-minutes", type=float, default=5.0, dest="segment_minutes")

    args = parser.parse_args()

    if args.cmd == "download":
        cmd_download(args)
    elif args.cmd == "transcribe":
        cmd_transcribe(args)
    elif args.cmd == "cut-audio":
        cmd_cut_audio(args)
    elif args.cmd == "build-html":
        cmd_build_html(args)
    elif args.cmd == "package":
        cmd_package(args)
    elif args.cmd == "all":
        cmd_download(args)
        args.wav = str(pathlib.Path(args.out_dir) / "full.wav")
        cmd_transcribe(args)
        print("\n⏸  Paused — run /make-course in Claude Code to generate course.md")
        print(f"   segments.json is ready at {args.out_dir}/segments.json")


if __name__ == "__main__":
    main()
