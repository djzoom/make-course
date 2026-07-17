#!/usr/bin/env python3
"""
make_course.py — YouTube 听力教材流水线（非 AI 步骤）

子命令:
  download   <url>        下载音轨（audio-only），提取 MP3 + WAV
  transcribe <wav>        Whisper 转录 → transcript.txt + words.json + segments.json
  resegment  <dir>        AI 整理稿 refined.txt 对齐词级时间戳 → sentences.json，
                          重写 transcript.txt / segments.json（真实句边界）
  cut-audio  <dir>        按句子 / 分组切 MP3（8 线程并行；有 sentences.json 时间隙感知）
  build-html <dir>        将 course.md 转换为 listening_course.html
  all        <url>        依次执行全部步骤（AI 注释由外部提供 course.md）

输出目录结构:
  <out-dir>/
    full.mp3
    full.wav
    transcript.txt          # [HH:MM:SS.ss] sentence（resegment 后为精整稿）
    transcript_raw.txt      # 原始 Whisper 转录备份（resegment 时生成）
    words.json              # 词级时间戳（resegment 的对齐依据）
    refined.txt             # 【AI 整理重断句稿：每行一个完整句/意群】
    sentences.json          # 每句真实 start/end（resegment 产出）
    segments.json           # 分段元信息（供 Claude Workflow 使用）
    course.md               # 【由 /make-course Claude 命令生成】
    listening_course.html   # 最终输出
    audio/
      seg1.mp3 … seg7.mp3  # 分段音频（供段落播放器；间隙感知，不含被删内容）
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

    mp3   = outd / "full.mp3"
    wav   = outd / "full.wav"

    print(f"\n=== Phase 1: Download (audio only) ===")
    # The course never uses video → download audio only (far smaller/faster).
    # `--remote-components ejs:github` lets yt-dlp's deno-based solver clear
    # YouTube's `n` challenge; without it, downloads throttle to a near-halt on
    # long videos (requires a JS runtime — deno or node — on PATH).
    if not mp3.exists():
        run(["yt-dlp", "--remote-components", "ejs:github",
             "-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "2",
             "-o", str(outd / "full.%(ext)s"), url])
    else:
        print(f"  ✓ {mp3} already exists")

    if not wav.exists():
        run(["ffmpeg", "-y", "-i", str(mp3), "-vn",
             "-ar", "16000", "-ac", "1", str(wav)])
    else:
        print(f"  ✓ {wav} already exists")

    # Save source URL for reproducibility (lets the course be rebuilt later).
    (outd / "source.txt").write_text(url + "\n")
    print(f"\n✓ Download complete → {outd}")


def _build_segments(entries, segment_minutes):
    """Bucket [(end_sec, idx, text), ...] into ~segment_minutes chunks."""
    total_dur = entries[-1][0]
    seg_dur   = segment_minutes * 60
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
    return segments


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: transcribe
# ─────────────────────────────────────────────────────────────────────────────

def cmd_transcribe(args):
    wav  = pathlib.Path(args.wav)
    outd = pathlib.Path(args.out_dir)
    outd.mkdir(parents=True, exist_ok=True)

    transcript_path = outd / "transcript.txt"
    segments_path   = outd / "segments.json"
    words_path      = outd / "words.json"

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

        # Word-level timestamps — the raw material `resegment` aligns against.
        words = []
        for seg in result["segments"]:
            for w in seg.get("words", []):
                token = str(w["word"]).strip()
                if token:
                    words.append({"w": token,
                                  "start": round(float(w["start"]), 3),
                                  "end":   round(float(w["end"]), 3)})
        words_path.write_text(json.dumps(words, ensure_ascii=False))
        print(f"  ✓ {len(words)} word timestamps → {words_path}")
    else:
        print(f"  ✓ {transcript_path} already exists")
        if not words_path.exists():
            print("  ⚠ words.json 缺失（旧版转录）——如需 resegment，请删除 transcript.txt 重跑 transcribe")

    # Build segments.json: split transcript into ~5-min segments for AI agents
    entries = load_transcript(transcript_path)
    if not entries:
        sys.exit("Transcript is empty or couldn't be parsed.")

    segments = _build_segments(entries, args.segment_minutes)

    segments_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2))
    print(f"  ✓ {len(segments)} segments → {segments_path}")
    for s in segments:
        print(f"    Seg {s['seg']:2d}: {s['start_fmt']}–{s['end_fmt']} ({len(s['sentences'])} sentences)")

    print(f"\n✓ Transcription complete → {outd}")
    return segments


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.5: resegment — align AI-refined sentences back to word timestamps
# ─────────────────────────────────────────────────────────────────────────────

def _norm_token(t: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", t.lower())


def _align_lines(words, lines):
    """Per-line monotonic alignment of refined lines onto the word stream.

    NOT one global SequenceMatcher: with repeated phrases (sermons, meetings:
    "we need more…" said three times) a global match can anchor a line's head
    to an EARLIER occurrence and its tail after a dropped ad — min/max then
    swallows the ad into a single sentence span, at 100% token coverage, so
    no warning fires. Instead walk a cursor through the word stream, align
    each line inside a lookahead window, cluster its matching blocks by
    contiguity (small gaps = in-line ASR corrections) and keep only the best
    cluster; the cursor then advances past it.

    When a line has multiple equally-good anchors (typical: short "Thank you." /
    "OK." after theme dropped an earlier copy), a small DP look-ahead picks the
    candidate that maximises matched tokens on this + later lines; ties prefer
    the later anchor (deleted copies tend to be earlier in the stream).

    Returns (starts, ends, coverage, suspicious): starts/ends hold None for
    unmatched lines; suspicious = [(line_no, span_sec, expected_sec)] for
    spans that are implausibly long for their word count, or (line_no, -matched,
    -total) when only a partial token match was kept (incomplete line span).
    """
    import difflib

    orig_tokens, orig_map = [], []
    for i, w in enumerate(words):
        n = _norm_token(w["w"])
        if n:
            orig_tokens.append(n)
            orig_map.append(i)
    n_orig = len(orig_tokens)

    line_tokens = [[n for n in (_norm_token(t) for t in ln.split()) if n]
                   for ln in lines]
    n_lines = len(line_tokens)

    def _need(L):
        # Short lines: require a full token match so a lone early "yes" cannot
        # claim the line "yes sir". Longer lines: ≥ half is enough for ASR gaps.
        return len(L) if len(L) <= 3 else max(1, len(L) // 2)

    def _window_hi(cursor, L):
        return min(n_orig, cursor + len(L) + max(80, len(L) * 8))

    def _clusters(lo, hi, L):
        """All contiguous clusters in orig[lo:hi] as (score, a0, a1) abs idx."""
        seg = orig_tokens[lo:hi]
        if not seg or not L:
            return []
        sm = difflib.SequenceMatcher(a=seg, b=L, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size]
        raw = []
        for blk in blocks:
            last = raw[-1][-1] if raw else None
            # near-contiguous in BOTH sequences → same utterance (the gap is
            # an ASR-corrected word), merge; otherwise a separate candidate.
            if (last is not None
                    and blk.a - (last.a + last.size) <= 3
                    and blk.b - (last.b + last.size) <= 3):
                raw[-1].append(blk)
            else:
                raw.append([blk])
        out = []
        for cl in raw:
            score = sum(b.size for b in cl)
            a0 = lo + cl[0].a
            a1 = lo + cl[-1].a + cl[-1].size - 1
            out.append((score, a0, a1))
        return out

    def _exact_matches(lo, hi, L):
        """All non-overlapping exact occurrences of L in orig[lo:hi].

        SequenceMatcher only assigns each b-token once, so a 2-word line like
        "thank you" yields a single (earliest) block even when the phrase
        repeats later — missing the copy theme kept after dropping an earlier
        one. Sliding-window exact search finds every full hit.
        """
        n, out, i = len(L), [], lo
        if n == 0:
            return out
        while i + n <= hi:
            if orig_tokens[i:i + n] == L:
                out.append((n, i, i + n - 1))
                i += n
            else:
                i += 1
        return out

    def _candidates(cursor, L):
        """Match candidates for L at/after cursor, highest-score pool."""
        need = _need(L)
        hi = _window_hi(cursor, L)

        def _gather(lo, hi_):
            found = _exact_matches(lo, hi_, L)
            for c in _clusters(lo, hi_, L):
                if c[0] >= need:
                    found.append(c)
            # Dedupe by span, keep best score.
            best_at = {}
            for score, a0, a1 in found:
                k = (a0, a1)
                if k not in best_at or score > best_at[k][0]:
                    best_at[k] = (score, a0, a1)
            return list(best_at.values())

        found = _gather(cursor, hi)
        if not found and hi < n_orig:
            found = _gather(cursor, n_orig)   # dropped span > window
        if not found:
            return []
        best = max(c[0] for c in found)
        # Every top-score hit is a live option (exact repeats of short or long
        # lines). DP look-ahead picks among them; ties prefer later anchors so
        # a theme-dropped earlier copy is skipped when totals are equal.
        pool = [c for c in found if c[0] == best]
        pool.sort(key=lambda c: c[1])  # earliest first
        # Cap branching for pathological "OK."×N streams (DP is exponential in
        # candidates-per-line); keep earliest + latest options.
        if len(pool) > 8:
            pool = pool[:4] + pool[-4:]
        return pool

    # DP: maximise remaining matched tokens; tie → later anchor (see docstring).
    memo = {}

    def _solve(li, cursor):
        key = (li, cursor)
        if key in memo:
            return memo[key]
        if li >= n_lines:
            memo[key] = (0, None)
            return memo[key]
        L = line_tokens[li]
        if not L:
            memo[key] = _solve(li + 1, cursor)
            return memo[key]
        cands = _candidates(cursor, L)
        if not cands:
            # Skip unmatched line; neighbour fallback later. Cursor stays put.
            total, _ = _solve(li + 1, cursor)
            memo[key] = (total, None)
            return memo[key]
        best_total = -1
        best_choice = None
        for score, a0, a1 in cands:
            rest, _ = _solve(li + 1, a1 + 1)
            total = score + rest
            if total > best_total:
                best_total = total
                best_choice = (score, a0, a1)
            elif total == best_total and best_choice is not None:
                # Equal futures: keep the earlier hit for tight re-speech
                # ("we need…" said twice back-to-back). Jump to the later hit
                # only when a multi-second gap sits between them — that gap is
                # almost always theme-deleted material, and the early hit is
                # the copy refined already dropped.
                gap_sec = (words[orig_map[a0]]["start"]
                           - words[orig_map[best_choice[2]]]["end"])
                if gap_sec > 3.0:
                    best_choice = (score, a0, a1)
        memo[key] = (best_total, best_choice)
        return memo[key]

    starts = [None] * n_lines
    ends   = [None] * n_lines
    cursor = 0
    matched_total = 0
    suspicious = []
    partial = []  # (line_no, matched_tokens, line_tokens)

    for li, L in enumerate(line_tokens):
        if not L:
            continue
        _, choice = _solve(li, cursor)
        if choice is None:
            continue
        score, a0, a1 = choice
        starts[li] = words[orig_map[a0]]["start"]
        ends[li]   = words[orig_map[a1]]["end"]
        cursor     = a1 + 1
        matched_total += score
        span     = ends[li] - starts[li]
        expected = len(L) / 2.0 + 2.0        # ~2 words/sec + slack, conservative
        if span > max(6.0, 3.0 * expected):
            suspicious.append((li + 1, span, expected))
        # Half-line match (e.g. >3-token ASR gap split clusters): not long, but incomplete.
        if score < len(L) * 0.75:
            partial.append((li + 1, score, len(L)))

    coverage = matched_total / max(1, sum(len(L) for L in line_tokens))
    # Encode partials into suspicious as negative expected so the printer can tell them apart
    # if we only pass one list — actually keep separate and merge in caller. Return both via
    # suspicious entries with a tag: use span < 0 as sentinel for partials.
    for line_no, got, total in partial:
        suspicious.append((line_no, -float(got), -float(total)))
    return starts, ends, coverage, suspicious


def cmd_resegment(args):
    """Align refined.txt (AI 整理重断句稿：每行一个完整句/意群，无时间戳) against
    words.json (word-level timestamps) → sentences.json with REAL per-sentence
    start/end. Real starts matter: after dropping ads/filler, the next sentence
    must not inherit "previous line's end" as its start, or the dropped audio
    leaks back in. Also rewrites transcript.txt (backing up the raw one) and
    rebuilds segments.json so every downstream step sees the refined text."""
    import shutil

    outd         = pathlib.Path(args.out_dir)
    words_path   = outd / "words.json"
    refined_path = pathlib.Path(args.refined) if args.refined else outd / "refined.txt"
    if not words_path.exists():
        sys.exit("words.json 不存在——旧版转录没有词级时间戳。删除 transcript.txt 后重跑 transcribe。")
    if not refined_path.exists():
        sys.exit(f"{refined_path} 不存在——先运行整理重断句（/make-course 第 1.7 步）生成。")

    words = json.loads(words_path.read_text())

    # Refined lines (one complete sentence / thought-group per line).
    lines = [ln.strip() for ln in refined_path.read_text().splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        sys.exit(f"{refined_path} 为空。")

    print(f"\n=== Phase 2.5: Resegment ===")
    print(f"  原始 {len(words)} 词 · 整理稿 {len(lines)} 行")

    starts, ends, coverage, suspicious = _align_lines(words, lines)

    print(f"  对齐覆盖率 {coverage:.1%}")
    if coverage < 0.85:
        print("  ⚠ 覆盖率偏低：整理稿改写过多（应只重组不改写），时间戳可能不准，建议检查 refined.txt")
    for line_no, span, expected in suspicious:
        # Partial token match encoded as negative span/expected by _align_lines.
        if span < 0:
            print(f"  ⚠ 行 {line_no} 仅对齐 {-span:.0f}/{ -expected:.0f} 词"
                  f"（行内匹配被拆散，时间戳可能只覆盖半句）——请核对该行")
        else:
            print(f"  ⚠ 行 {line_no} span 异常：{span:.1f}s（该行词数预期 ≈{expected:.0f}s）——请核对该行时间戳")

    # Lines with no matched word: fall back to neighbours, warn.
    unmatched = [li for li in range(len(lines)) if starts[li] is None]
    for li in unmatched:
        prev_end   = next((ends[j] for j in range(li - 1, -1, -1) if ends[j] is not None), 0.0)
        next_start = next((starts[j] for j in range(li + 1, len(lines)) if starts[j] is not None), None)
        starts[li] = prev_end
        ends[li]   = next_start if next_start is not None else prev_end + 2.0
    if unmatched:
        print(f"  ⚠ {len(unmatched)} 行无词级对齐（时间戳按相邻行估算）: 行 {[i + 1 for i in unmatched[:8]]}")

    sentences = [{"idx": i + 1, "start": round(starts[i], 3), "end": round(ends[i], 3),
                  "text": lines[i]} for i in range(len(lines))]
    (outd / "sentences.json").write_text(json.dumps(sentences, ensure_ascii=False, indent=1))
    print(f"  ✓ {len(sentences)} 句 → sentences.json")

    # Report dropped spans (>3s gap between consecutive kept sentences).
    gaps = [(sentences[i]["end"], sentences[i + 1]["start"])
            for i in range(len(sentences) - 1)
            if sentences[i + 1]["start"] - sentences[i]["end"] > 3.0]
    if gaps:
        total_gap = sum(b - a for a, b in gaps)
        print(f"  ✂ 丢弃 {len(gaps)} 处内容（共 {total_gap:.0f}s）: "
              + "、".join(f"{fmt_ts(a)}→{fmt_ts(b)}" for a, b in gaps[:6])
              + ("…" if len(gaps) > 6 else ""))

    # Rewrite transcript.txt (annotation agents & build-html read this),
    # keeping the raw whisper transcript for reference / re-runs.
    transcript_path = outd / "transcript.txt"
    raw_backup      = outd / "transcript_raw.txt"
    if transcript_path.exists() and not raw_backup.exists():
        shutil.copy2(transcript_path, raw_backup)
        print(f"  ✓ 原始转录备份 → {raw_backup.name}")

    def fmt_full(sec):
        h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    transcript_path.write_text("\n".join(f"[{fmt_full(s['end'])}] {s['text']}" for s in sentences))
    print(f"  ✓ transcript.txt 重写（{len(sentences)} 行，精整稿）")

    entries  = [(s["end"], s["idx"], s["text"]) for s in sentences]
    segments = _build_segments(entries, args.segment_minutes)
    (outd / "segments.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2))
    print(f"  ✓ segments.json 重建（{len(segments)} 段）——记得重跑 split-segs")

    print(f"\n✓ Resegment complete → {outd}")


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

    # Refined pipeline: sentences.json carries REAL per-sentence start/end.
    # Without it (legacy courses) fall back to "previous end = my start".
    sent_meta = None
    sentences_path = outd / "sentences.json"
    if sentences_path.exists():
        sent_meta = {s["idx"]: (s["start"], s["end"])
                     for s in json.loads(sentences_path.read_text())}
        print(f"  ✓ sentences.json：按真实句边界剪辑（间隙感知）")

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
             "-acodec", "libmp3lame", "-q:a", "5", "-ar", "16000", "-ac", "1",
             "-loglevel", "error", str(out)],
            capture_output=True,
        ).returncode == 0

    # --- Individual sentence clips ---
    tasks = []
    for i, (ts, idx, text) in enumerate(entries, 1):
        out = sent_dir / f"s{idx:03d}.mp3"
        if sent_meta and idx in sent_meta:
            s0, e0 = sent_meta[idx]
            tasks.append((s0 - LEAD, e0 + TAIL, out))
        else:
            prev_ts = entries[i - 2][0] if i > 1 else 0.0
            tasks.append((prev_ts - LEAD, ts + TAIL, out))

    print(f"  Cutting {len(tasks)} sentence clips…")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(ffcut, wav, s, e, o) for s, e, o in tasks]
        ok = sum(f.result() for f in concurrent.futures.as_completed(futs))
    print(f"  ✓ {ok}/{len(tasks)} individual clips")

    # --- Merged group clips (from course.md consecutive timestamp blocks) ---
    if md.exists():
        _cut_merged_groups(md, wav, sent_dir, entries, nearest, LEAD, TAIL, sent_meta)

    # --- Segment clips (for section players) ---
    segments_path = outd / getattr(args, "segments_file", "segments.json")
    if segments_path.exists():
        segs = json.loads(segments_path.read_text())
        if sent_meta:
            # Gap-aware: concatenate only the KEPT sentence spans, so dropped
            # ads / off-topic content never plays inside the section player.
            ok = 0
            for s in segs:
                spans = [sent_meta[x["idx"]] for x in s["sentences"] if x["idx"] in sent_meta]
                out   = seg_dir / f"seg{s['seg']}.mp3"
                if spans and _cut_regions(wav, spans, out):
                    ok += 1
            print(f"  ✓ {ok}/{len(segs)} segment clips (gap-aware)")
        else:
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


def _cut_regions(wav, spans, out, max_gap=1.5,
                 edge_pad=(0.2, 0.4), joint_pad=(0.05, 0.2)):
    """Cut a section/group clip that skips dropped content: merge sentence
    spans whose gap ≤ max_gap into contiguous regions, cut each region, then
    concat. Single region → plain cut (the common no-drop case).

    edge_pad  — outer lead/tail (matches single-sentence LEAD/TAIL) so group
                and section players don't clip onsets harder than s{idx}.mp3.
    joint_pad — pads at internal cut points next to a dropped span; kept tiny
                so ads/filler don't bleed back in.
    """
    import shutil

    regions = []
    for s0, e0 in spans:
        if regions and s0 - regions[-1][1] <= max_gap:
            regions[-1][1] = max(regions[-1][1], e0)
        else:
            regions.append([s0, e0])

    def _pads(j, n):
        lead = edge_pad[0] if j == 0 else joint_pad[0]
        tail = edge_pad[1] if j == n - 1 else joint_pad[1]
        return lead, tail

    def ffcut(start, end, path):
        return subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav),
             "-ss", f"{max(0.0, start):.4f}", "-to", f"{end:.4f}",
             "-acodec", "libmp3lame", "-q:a", "5", "-ar", "16000", "-ac", "1",
             "-loglevel", "error", str(path)],
            capture_output=True,
        ).returncode == 0

    n = len(regions)
    if n == 1:
        s0, e0 = regions[0]
        lead, tail = _pads(0, 1)
        return ffcut(s0 - lead, e0 + tail, out)

    tmpdir = out.parent / f".{out.stem}_parts"
    tmpdir.mkdir(exist_ok=True)
    try:
        parts = []
        for j, (s0, e0) in enumerate(regions):
            p = tmpdir / f"p{j:03d}.mp3"
            lead, tail = _pads(j, n)
            if not ffcut(s0 - lead, e0 + tail, p):
                return False
            parts.append(p)
        # Relative names (resolved against the list file's directory) — no
        # quoting pitfalls from parent paths. Re-encode instead of -c copy:
        # bit-joined MP3s can click at frame boundaries and drift in duration.
        lst = tmpdir / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        return subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-acodec", "libmp3lame", "-q:a", "5", "-ar", "16000", "-ac", "1",
             "-loglevel", "error", str(out)],
            capture_output=True,
        ).returncode == 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cut_merged_groups(md_path, wav, sent_dir, entries, nearest, LEAD, TAIL,
                       sent_meta=None):
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
        out = sent_dir / f"s{first_idx:03d}-{last_idx:03d}.mp3"
        if sent_meta and first_idx in sent_meta and last_idx in sent_meta:
            # Gap-aware like the section players: a group whose sentences
            # straddle a dropped span (ad) must not cut straight across it.
            spans = [sent_meta[i] for i in range(first_idx, last_idx + 1)
                     if i in sent_meta]
            tasks.append(("regions", spans, out))
        else:
            prev_ts = entries[first_idx - 2][0] if first_idx > 1 else 0.0
            tasks.append(("cut", prev_ts - LEAD, last[0] + TAIL, out))
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
             "-acodec", "libmp3lame", "-q:a", "5", "-ar", "16000", "-ac", "1",
             "-loglevel", "error", str(out)],
            capture_output=True,
        ).returncode == 0

    def run_task(t):
        if t[0] == "regions":
            # edge_pad matches LEAD/TAIL so merged groups feel like stitched
            # sentence clips; joint_pad stays tight at deletion cuts.
            return _cut_regions(wav, t[1], t[2],
                                edge_pad=(LEAD, TAIL), joint_pad=(0.05, 0.2))
        return ffcut(t[1], t[2], t[3])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(run_task, t) for t in tasks]
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

    # Single-word fillers / discourse particles with no teaching value on their own.
    FILLER = {
        "uh", "uhh", "uhm", "um", "umm", "er", "erm", "err", "ah", "ahh",
        "oh", "ohh", "hmm", "hm", "mm", "mmm", "mhm", "huh", "eh",
        "well", "so", "right", "okay", "ok", "alright", "yeah", "yea",
        "yep", "yup", "yes", "no", "nah", "like", "now", "anyway", "anyways",
        "u", "v",
    }
    # Multi-word fillers stripped before the single-word check.
    MULTIWORD_FILLERS = [
        "you know what i mean", "you know", "i mean", "sort of", "kind of",
        "you see", "i guess",
    ]

    def is_pure_filler(text):
        """True when the whole sentence is nothing but filler/discourse
        particles — dropped even if the AI attached an annotation, because a
        bare 'Um.' / 'Well.' / 'You know.' has no listening value."""
        raw = text.strip()
        if not raw:
            return False
        t = raw.lower()
        for mw in MULTIWORD_FILLERS:
            t = t.replace(mw, " ")
        t = re.sub(r"[^\w\s]", " ", t)            # drop punctuation
        words = [w for w in t.split() if w]
        return all(w in FILLER for w in words)     # empty (all multiword) → True

    def is_short_bare(text):
        # Very short (catches dramatic-pause fragments) — only dropped when unannotated.
        return len(text.split()) <= 3

    def has_ann(lines):
        return len("".join(lines).strip()) > 20

    # Annotations that themselves declare the line has no value. Kept tight
    # on purpose: generic words (跳过/无关紧要/不值得) also appear inside real
    # vocabulary glosses (irrelevant→无关紧要, not worth→不值得), so only
    # unambiguous meta-notes belong here.
    SKIP_PHRASES = [
        "无注释价值", "无学习价值", "无教学价值", "无需注释",
        "无实质内容", "无实质意义", "无实质教学", "意义不大", "意義不大",
        "纯填充，跳过", "纯填充,跳过", "跳过不讲", "此句跳过", "跳过此句",
        "纯内容，无", "no annotation", "not worth annotating",
    ]

    def ann_says_skip(lines):
        text = "".join(lines)
        if not any(p in text.lower() for p in SKIP_PHRASES):
            return False
        # Guard: only drop when the annotation is essentially just the meta-note.
        # A long annotation that merely mentions such a word inside a real gloss
        # is kept.
        return len(re.sub(r"\s+", "", text)) <= 60

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
            # Prose accumulated before any timestamp (title, intro, section
            # heading) must flush as its own intro block — otherwise it becomes
            # this sentence's "annotation" and the player (emitted before the
            # annotation) floats above the heading/title.
            if in_ann or (cur_ann and not cur_ts):
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
        elif all(is_pure_filler(t) for _, t in b["ts"]):
            b["keep"] = False  # pure filler → drop even if annotated
        elif ann_says_skip(b["annot"]):
            b["keep"] = False  # annotation itself flags it as no-value
        elif not has_ann(b["annot"]) and all(is_short_bare(t) for _, t in b["ts"]):
            b["keep"] = False  # very short & unannotated
        else:
            b["keep"] = True
            b["type"] = "merged" if len(b["ts"]) > 1 else "single"

    # Dropping is by design (fragments / pure filler), but never silent —
    # a legitimate short sentence the AI forgot to annotate would vanish here.
    dropped = [b for b in blocks if not b["keep"]]
    if dropped:
        print(f"  ⚠ 丢弃 {len(dropped)} 个块（纯填充/超短无注释/标记跳过）：")
        for b in dropped[:8]:
            print(f"      · {' / '.join(t for _, t in b['ts'])[:48]}")
        if len(dropped) > 8:
            print(f"      … 共 {len(dropped)} 个")

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
    unresolved = []
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
            # No transcript line within ±4s — annotation renders without a
            # player. Usually the AI mangled a timestamp; must be visible.
            unresolved.append(f"[{fmt_ts(ts_list[0][0])}] {ts_list[0][1][:40]}")
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

    if unresolved:
        print(f"  ⚠ {len(unresolved)} 个时间戳未匹配到转录行（±4s 内无对应，块内无播放器）：")
        for u in unresolved[:8]:
            print(f"      · {u}")

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
# Inline: single self-contained HTML (audio as base64 data URIs)
# ─────────────────────────────────────────────────────────────────────────────

# Mobile browsers and in-app WebViews (Telegram, iOS Safari, Android WebView)
# cannot actually play audio from a large `data:` URI: the <audio> element enters
# the playing state (the ▶ button flips to ⏸ and the `play` event fires) but the
# resource never decodes, so currentTime stays frozen at 0:00. Desktop browsers
# tolerate data-URI media; phones do not. Fix: on the first `play`, convert the
# inlined base64 into a Blob URL — a real, seekable object URL that every mobile
# WebView plays natively — then reassign src and resume. Lazy (per-clip, on
# demand) so we never decode all ~60 MB of audio up front. Idempotent-guarded by
# the marker comment below so re-running fix-audio never injects twice.
_AUDIO_FIX_MARKER = "<!-- audio-blob-fix -->"
_AUDIO_FIX_JS = _AUDIO_FIX_MARKER + """
<script>
(function () {
  function toBlobURL(uri) {
    var c = uri.indexOf(',');
    var mime = (uri.substring(0, c).match(/data:([^;]+)/) || [0, 'audio/mpeg'])[1];
    var bin = atob(uri.substring(c + 1)), n = bin.length, u8 = new Uint8Array(n);
    for (var i = 0; i < n; i++) u8[i] = bin.charCodeAt(i);
    return URL.createObjectURL(new Blob([u8], { type: mime }));
  }
  function upgrade(audio) {
    var src = audio.querySelector('source');
    var uri = src ? src.getAttribute('src') : audio.getAttribute('src');
    if (!uri || uri.lastIndexOf('data:', 0) !== 0) return;  // only inlined clips
    var done = false;
    audio.addEventListener('play', function () {
      if (done) return;
      done = true;
      var url;
      try { url = toBlobURL(uri); } catch (e) { return; }
      if (src) src.removeAttribute('src');
      audio.src = url;   // seekable resource — plays on iOS / Android / Telegram
      audio.load();
      var p = audio.play();
      if (p && p.catch) p.catch(function () {});
    });
  }
  function boot() {
    var list = document.getElementsByTagName('audio');
    for (var i = 0; i < list.length; i++) upgrade(list[i]);
  }
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>
"""


def _inject_audio_fix(html):
    """Insert the Blob-URL playback shim before </body> (idempotent)."""
    if _AUDIO_FIX_MARKER in html:
        return html, False
    if "</body>" in html:
        return html.replace("</body>", _AUDIO_FIX_JS + "\n</body>", 1), True
    return html + _AUDIO_FIX_JS, True


def cmd_fix_audio(args):
    """Patch already-built single-file HTML so its inlined audio plays on mobile.

    Adds the Blob-URL playback shim to each given HTML in place, without needing
    the original audio/ folder (which `clean` may have removed). Idempotent."""
    paths = [pathlib.Path(p) for p in args.files]
    print(f"\n=== Fix audio: {len(paths)} 个文件 ===")
    for p in paths:
        if not p.exists():
            print(f"  ⚠ 跳过（不存在）：{p}"); continue
        html = p.read_text(encoding="utf-8")
        html, changed = _inject_audio_fix(html)
        if not changed:
            print(f"  · 已含修复，跳过：{p.name}"); continue
        p.write_text(html, encoding="utf-8")
        print(f"  ✓ 已注入 Blob 播放修复：{p.name}")


def cmd_inline(args):
    """Inline referenced audio as base64 data URIs → one self-contained HTML.

    Works on any already-built listening_course.html (manual or scripted flow).
    Only the audio the page actually references is embedded, so the thousands of
    unused per-sentence clips on disk are dropped automatically. The result is a
    single file: no unzip, no audio/ folder next to it, safe to move or send."""
    import base64
    outd = pathlib.Path(args.out_dir)
    html_in = outd / "listening_course.html"
    if not html_in.exists():
        sys.exit(f"listening_course.html not found in {outd}. Run build-html first.")

    raw = html_in.read_text(encoding="utf-8")
    MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
            ".ogg": "audio/ogg", ".opus": "audio/ogg"}
    cache, missing = {}, []
    embedded_bytes = 0

    def encode(rel):
        nonlocal embedded_bytes
        if rel in cache:
            return cache[rel]
        p = outd / rel
        if not p.exists():
            missing.append(rel); cache[rel] = None; return None
        data = p.read_bytes()
        embedded_bytes += len(data)
        uri = (f"data:{MIME.get(p.suffix.lower(), 'audio/mpeg')};base64,"
               + base64.b64encode(data).decode("ascii"))
        cache[rel] = uri
        return uri

    refs = sorted(set(re.findall(r'src="(audio/[^"]+)"', raw)))
    print(f"\n=== Inline: 内嵌 {len(refs)} 个被引用音频 → 单文件 HTML ===")

    def repl(m):
        uri = encode(m.group(1))
        return f'src="{uri}"' if uri else m.group(0)

    html = re.sub(r'src="(audio/[^"]+)"', repl, raw)

    if missing:
        print(f"  ⚠ {len(missing)} 个引用文件缺失，保留原相对路径（如 {missing[0]}）")

    # Blob-URL shim so the inlined data-URI audio actually plays on mobile/Telegram.
    html, _ = _inject_audio_fix(html)

    # Output name: reuse the <title> slug convention from cmd_package.
    title_line = ""
    for line in raw.splitlines():
        mt = re.search(r"<title>(.*?)</title>", line)
        if mt:
            title_line = mt.group(1); break
    slug = re.sub(r"[^\w一-鿿]+", "_", title_line).strip("_")[:40] or outd.name
    out_path = pathlib.Path(getattr(args, "output", None) or (outd / f"{slug}_单文件.html"))
    out_path.write_text(html, encoding="utf-8")

    size_mb = out_path.stat().st_size / 1048576
    print(f"  ✓ 内嵌音频 {embedded_bytes/1048576:.1f} MB → 单文件 HTML {size_mb:.1f} MB")
    print(f"\n✓ 双击即开，无需解压、无 audio 目录依赖 → {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Collect / Clean: gather finished courses, drop workflow intermediates
# ─────────────────────────────────────────────────────────────────────────────

# The three artifact classes this workflow produces:
#   成品   (deliverable) : *_单文件.html          → collected into 课程/
#   重建源 (source)       : course.md / listening_course.md, transcript.txt,
#                           segments.json          → small, kept for re-builds
#   中间产物 (intermediate): everything below       → safe to delete post-build
_CLEAN_DIRS  = ["audio", "segs"]
_CLEAN_GLOBS = ["*_portable.zip", "listening_course.html", "full.mp3",
                "full.wav", "full_transcript.txt", "segments_sermon.json"]
_SOURCE_FILES = ["course.md", "listening_course.md", "transcript.txt",
                 "transcript_raw.txt", "refined.txt", "words.json",
                 "sentences.json", "segments.json", "segs_meta.json"]


def cmd_collect(args):
    """Move (default) or copy the standalone single-file HTML into a central 课程/ folder."""
    import shutil
    outd = pathlib.Path(args.out_dir)
    files = sorted(outd.glob("*_单文件.html"))
    if not files:
        sys.exit(f"未找到 *_单文件.html（先运行 inline）：{outd}")
    dest = pathlib.Path(args.dest) if args.dest else (outd.resolve().parent.parent / "课程")
    dest.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Collect → {dest} ===")
    for f in files:
        target = dest / (args.name if args.name else f.name)
        if args.copy:
            shutil.copy2(f, target)
        else:
            shutil.move(str(f), str(target))
        print(f"  ✓ {'复制' if args.copy else '移动'} {f.name} → {target.name}")


def cmd_clean(args):
    """Delete workflow intermediates. Keeps the small re-build sources unless --all;
    keeps *_单文件.html unless --drop-standalone (use once collected into 课程/)."""
    import shutil
    outd = pathlib.Path(args.out_dir)
    if not outd.exists():
        sys.exit(f"目录不存在：{outd}")

    targets = []
    for d in _CLEAN_DIRS:
        p = outd / d
        if p.is_symlink() or p.is_dir():
            targets.append(p)
    for pat in _CLEAN_GLOBS:
        targets += list(outd.glob(pat))
    if args.drop_standalone:
        targets += list(outd.glob("*_单文件.html"))
    if args.all:
        targets += [outd / f for f in _SOURCE_FILES if (outd / f).exists()]
    targets = [p for p in dict.fromkeys(targets) if p.is_symlink() or p.exists()]

    def size(p):
        if p.is_symlink():
            return 0
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size

    print(f"\n=== Clean: {outd} ===")
    if not targets:
        print("  无可清理的中间产物。")
        return
    total = 0
    for p in sorted(targets):
        s = size(p); total += s
        print(f"  🗑 {p.relative_to(outd)}{'/' if p.is_dir() else ''}  ({s/1048576:.1f} MB)")
    print(f"  合计 {total/1048576:.1f} MB")

    if args.dry_run:
        print("  (dry-run，未删除)")
        return
    for p in targets:
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
    print(f"  ✓ 已清理，回收 {total/1048576:.1f} MB")
    if not args.all:
        kept = [f for f in _SOURCE_FILES if (outd / f).exists()]
        if kept:
            print(f"  ↳ 保留重建源：{'、'.join(kept)}")


# ─────────────────────────────────────────────────────────────────────────────
# Estimate: token / cost of the (paid) AI annotation step
# ─────────────────────────────────────────────────────────────────────────────

# Warn-and-confirm threshold: runs longer than this (or over the cost floor
# below) should prompt the user before the Workflow annotation is launched.
_COST_WARN_MINUTES = 30
_COST_WARN_USD = 4.0


def cmd_split_segs(args):
    """Split segments.json into per-segment text files (segs/segN.txt) + a tiny
    segs_meta.json. Lets Workflow annotation agents each read ONLY their own
    segment file, so the 100–200KB segments.json never has to pass through the
    Workflow args (which can't carry that much, and the sandbox can't read files)."""
    outd  = pathlib.Path(args.out_dir)
    spath = outd / getattr(args, "segments_file", "segments.json")
    if not spath.exists():
        sys.exit(f"{spath} 不存在，先运行 transcribe")
    segs = json.loads(spath.read_text())
    sd = outd / "segs"; sd.mkdir(exist_ok=True)

    def fmt(sec):
        h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    meta = []
    for seg in segs:
        lines = [f"[{fmt(x['end_sec'])}] {x['text']}" for x in seg["sentences"]]
        (sd / f"seg{seg['seg']}.txt").write_text("\n".join(lines))
        meta.append({"seg": seg["seg"], "start_fmt": seg["start_fmt"],
                     "end_fmt": seg["end_fmt"], "n": len(seg["sentences"])})
    (outd / "segs_meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    print(f"  ✓ 拆出 {len(meta)} 个段文件 → {sd}")
    print(f"  ✓ segs_meta.json（{len(json.dumps(meta))} 字符）— 作为 Workflow args 传入")
    print(json.dumps(meta, ensure_ascii=False))


def cmd_estimate(args):
    """Estimate annotation token/cost before the paid Workflow step.
    Reads transcript.txt + segments.json produced by `transcribe`.
    Heuristics are calibrated against past episodes:
      output_tokens ≈ 1.75 × transcript_chars (course.md ≈ 2.5× transcript, ~0.7 tok/char);
      input_tokens ≈ transcript_chars/4 (English) + one ~600-tok system-prompt copy per agent.
    """
    outd = pathlib.Path(args.out_dir)
    tpath = outd / "transcript.txt"
    spath = outd / getattr(args, "segments_file", "segments.json")
    if not tpath.exists() or not spath.exists():
        sys.exit("需要先运行 transcribe（缺 transcript.txt / segments.json）")

    chars = len(tpath.read_text())
    segs = json.loads(spath.read_text())
    n_seg = len(segs)
    n_sent = sum(len(s["sentences"]) for s in segs)
    dur_min = max((s["end_sec"] for s in segs), default=0) / 60

    in_tok = int(chars / 4 + (n_seg + 1) * 600)
    out_lo, out_mid, out_hi = (int(chars * k) for k in (1.4, 1.75, 2.2))

    def cost(o, pin, pout):
        return in_tok / 1e6 * pin + o / 1e6 * pout

    over = dur_min >= _COST_WARN_MINUTES or cost(out_mid, 5, 25) >= _COST_WARN_USD

    print(f"\n=== 成本预估: {outd} ===")
    print(f"  时长 {dur_min:.0f} 分钟 · {n_seg} 段 · {n_sent} 句 · transcript {chars:,} 字符")
    print(f"  预估 input ≈ {in_tok/1000:.0f}k tok · output ≈ {out_lo/1000:.0f}–{out_hi/1000:.0f}k tok")
    print(f"  Opus 4.8  ($5/$25):  约 ${cost(out_lo,5,25):.1f}–${cost(out_hi,5,25):.1f}")
    print(f"  Sonnet 5  ($3/$15):  约 ${cost(out_lo,3,15):.1f}–${cost(out_hi,3,15):.1f}")
    print(f"  注：未含 thinking token（adaptive 思考另计，实际可再高 20–60%）")
    if over:
        print(f"  ⚠ 超阈值（≥{_COST_WARN_MINUTES}分钟 或 >${_COST_WARN_USD:.0f}）——注释前须向用户确认")
    else:
        print(f"  ✓ 低于阈值，可直接进注释")


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

    p_rs = sub.add_parser("resegment", help="AI 整理稿对齐词级时间戳 → sentences.json + 重写 transcript/segments")
    p_rs.add_argument("out_dir")
    p_rs.add_argument("--refined", default=None,
                      help="整理稿路径（默认 <out-dir>/refined.txt）")
    p_rs.add_argument("--segment-minutes", type=float, default=5.0, dest="segment_minutes")

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

    p_in = sub.add_parser("inline", help="把被引用音频内嵌进 HTML，输出自包含单文件（无需解压/无 audio 目录）")
    p_in.add_argument("out_dir")
    p_in.add_argument("--output", default=None, dest="output",
                      help="输出 HTML 路径（默认：<title>_单文件.html）")

    p_fa = sub.add_parser("fix-audio", help="给已生成的单文件 HTML 注入 Blob 播放修复（手机/Telegram 上可播）")
    p_fa.add_argument("files", nargs="+", help="要修复的 HTML 文件（可多个）")

    p_co = sub.add_parser("collect", help="把成品单文件 *_单文件.html 归集到 课程/ 目录")
    p_co.add_argument("out_dir")
    p_co.add_argument("--dest", default=None, help="目标目录（默认 <项目根>/课程）")
    p_co.add_argument("--name", default=None, help="归集后的文件名（默认沿用原名）")
    p_co.add_argument("--copy", action="store_true", help="复制而非移动（保留 docs 原件）")

    p_ss = sub.add_parser("split-segs", help="把 segments.json 拆成 segs/segN.txt + segs_meta.json（供 Workflow agent 各读各段）")
    p_ss.add_argument("out_dir")
    p_ss.add_argument("--segments-file", default="segments.json", dest="segments_file")

    p_es = sub.add_parser("estimate", help="预估 AI 注释 token/成本（transcribe 之后、注释之前跑）")
    p_es.add_argument("out_dir")
    p_es.add_argument("--segments-file", default="segments.json", dest="segments_file")

    p_cl = sub.add_parser("clean", help="清理中间产物（默认保留 course.md/transcript/segments 重建源）")
    p_cl.add_argument("out_dir")
    p_cl.add_argument("--all", action="store_true", help="连重建源(course.md/transcript/segments)也删")
    p_cl.add_argument("--drop-standalone", action="store_true", dest="drop_standalone",
                      help="连 *_单文件.html 也删（确认已归集到 课程/ 后再用）")
    p_cl.add_argument("--dry-run", action="store_true", dest="dry_run", help="只预览不删除")

    p_al = sub.add_parser("all", help="下载 + 转录（AI 注释需单独运行 /make-course）")
    p_al.add_argument("url")
    p_al.add_argument("--out-dir", default="./course_out", dest="out_dir")
    p_al.add_argument("--segment-minutes", type=float, default=5.0, dest="segment_minutes")

    args = parser.parse_args()

    if args.cmd == "download":
        cmd_download(args)
    elif args.cmd == "transcribe":
        cmd_transcribe(args)
    elif args.cmd == "resegment":
        cmd_resegment(args)
    elif args.cmd == "cut-audio":
        cmd_cut_audio(args)
    elif args.cmd == "build-html":
        cmd_build_html(args)
    elif args.cmd == "package":
        cmd_package(args)
    elif args.cmd == "inline":
        cmd_inline(args)
    elif args.cmd == "fix-audio":
        cmd_fix_audio(args)
    elif args.cmd == "collect":
        cmd_collect(args)
    elif args.cmd == "split-segs":
        cmd_split_segs(args)
    elif args.cmd == "estimate":
        cmd_estimate(args)
    elif args.cmd == "clean":
        cmd_clean(args)
    elif args.cmd == "all":
        cmd_download(args)
        args.wav = str(pathlib.Path(args.out_dir) / "full.wav")
        cmd_transcribe(args)
        print("\n⏸  Paused — run /make-course in Claude Code to generate course.md")
        print(f"   segments.json is ready at {args.out_dir}/segments.json")


if __name__ == "__main__":
    main()
