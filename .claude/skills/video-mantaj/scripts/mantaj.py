#!/usr/bin/env python3
"""mantaj.py: build a video montage from photos and clips with ffmpeg.

Photos get a gentle Ken Burns move; clips are trimmed. Everything is fitted to one
frame size, joined with transitions (xfade) or hard cuts, and put under an optional
music track. Cuts can be snapped to the music's beat. Needs ffmpeg/ffprobe; numpy is
needed only for --beat-sync, and Pillow (optional) fixes EXIF photo rotation.

Example:
  python3 mantaj.py photos/ clip.mp4 -o out.mp4 --music song.mp3 --aspect 9:16 \
      --transition random --beat-sync --title "Yozgi sayohat" --sheet sheet.jpg
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp", ".mts"}
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
RANDOM_TRANSITIONS = ["fade", "dissolve", "smoothleft", "smoothright", "slideleft", "slideup",
                      "circleopen", "radial", "wipeleft", "zoomin", "fadeblack", "hblur"]
MIN_HOLD = 0.25  # seconds a shot is shown on its own between two transitions


def die(msg: str) -> None:
    sys.exit(f"mantaj: error: {msg}")


def run(cmd: list[str], quiet: bool = True) -> None:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-25:])
        die(f"ffmpeg failed ({res.returncode}):\n{' '.join(cmd)}\n{tail}")
    if not quiet:
        sys.stderr.write(res.stderr)


def probe(path: str) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type", "-of", "json", path],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        die(f"cannot read {path}: {res.stderr.strip()}")
    info = json.loads(res.stdout or "{}")
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    dur = float(info.get("format", {}).get("duration") or 0)
    return {"duration": dur, "has_audio": "audio" in kinds, "has_video": "video" in kinds}


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def collect_inputs(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files = [os.path.join(p, f) for f in os.listdir(p)
                     if os.path.splitext(f)[1].lower() in PHOTO_EXT | VIDEO_EXT]
            out.extend(sorted(files, key=natural_key))
        elif os.path.isfile(p):
            out.append(p)
        else:
            die(f"not found: {p}")
    if not out:
        die("no photos or videos found in the inputs")
    return out


# ---------------------------------------------------------------- beat detection
def detect_beats(music: str, max_seconds: float) -> tuple[float, list[float]]:
    """Return (bpm, beat_times) using spectral-flux onsets + autocorrelation tempo."""
    try:
        import numpy as np
    except ImportError:
        die("--beat-sync needs numpy (pip install numpy)")
    sr, hop, win = 22050, 512, 2048
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", f"{max_seconds + 5:.2f}", "-i", music,
                          "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    y = np.frombuffer(raw, dtype=np.float32)
    if y.size < win * 4:
        die("music is too short or unreadable for beat detection")
    n = 1 + (y.size - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    frames = y[idx] * np.hanning(win)[None, :].astype(np.float32)
    mag = np.log1p(10.0 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")  # remove slow trend
    env = np.maximum(flux, 0.0)
    env /= env.max() or 1.0
    fps = sr / hop
    ac = np.correlate(env, env, mode="full")[env.size - 1:]
    bpms = np.arange(70.0, 181.0, 0.5)
    lags = 60.0 * fps / bpms
    weight = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)  # prefer ~120 BPM
    score = np.interp(lags, np.arange(ac.size), ac) * weight
    bpm = float(bpms[int(np.argmax(score))])
    period = 60.0 * fps / bpm
    phases = np.arange(0.0, period, 0.5)
    k = np.arange(0, int((env.size - period) / period))
    best = max(phases, key=lambda ph: env[np.clip((ph + k * period).astype(int), 0, env.size - 1)].sum())
    beats = [float((best + i * period) / fps) for i in k]
    return bpm, beats


# ---------------------------------------------------------------- planning
def plan_timeline(items: list[dict], tdur: float, beats: list[float] | None,
                  target_total: float | None) -> list[float]:
    """Fill item['len'] (segment length incl. overlap) and return transition offsets."""
    n = len(items)
    if target_total and n > 1:
        fixed = sum(it["want"] for it in items if it["kind"] == "video")
        photos = [it for it in items if it["kind"] == "photo"]
        if photos:
            per = (target_total - fixed + (n - 1) * tdur) / len(photos)
            per = max(per, 2 * tdur + MIN_HOLD + 0.1)
            for it in photos:
                it["want"] = per
    offsets: list[float] = []
    seg_start = 0.0
    for i in range(n - 1):
        it = items[i]
        lo = seg_start + (tdur if i > 0 else 0.0) + MIN_HOLD
        hi = seg_start + it["max"] - tdur
        ideal = max(lo, min(hi, seg_start + it["want"] - tdur))
        o = ideal
        if beats:
            cands = [b - tdur / 2 for b in beats if lo <= b - tdur / 2 <= hi]
            if cands:
                o = min(cands, key=lambda c: abs(c - ideal))
        it["len"] = o + tdur - seg_start
        offsets.append(o)
        seg_start = o
    last = items[-1]
    want_last = last["want"]
    if target_total:
        want_last = max(want_last if n == 1 else tdur + MIN_HOLD, target_total - seg_start)
    last["len"] = max(min(want_last, last["max"]), (tdur if n > 1 else 0.0) + MIN_HOLD)
    return offsets


# ---------------------------------------------------------------- filters
def fit_filter(fit: str, w: int, h: int, tag_in: str, tag_out: str, blur: int) -> str:
    cover = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    contain = f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if fit == "cover":
        return f"[{tag_in}]{cover},setsar=1[{tag_out}]"
    if fit == "pad":
        return f"[{tag_in}]{contain},pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[{tag_out}]"
    return (f"[{tag_in}]split[bga][fga];[bga]{cover},boxblur={blur}:3,eq=brightness=-0.07[bg];"
            f"[fga]{contain}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{tag_out}]")


def kenburns(i: int, frames: int, w: int, h: int, fps: int, zoom: float) -> str:
    z_in = f"1+{zoom - 1:.4f}*on/{frames}"
    z_out = f"{zoom:.4f}-{zoom - 1:.4f}*on/{frames}"
    cx, cy = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    moves = [(z_in, cx, cy), (z_out, cx, cy),
             (z_in, f"(iw-iw/zoom)*on/{frames}", cy), (z_in, cx, f"(ih-ih/zoom)*(1-on/{frames})")]
    z, x, y = moves[i % len(moves)]
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps}"


def prepare_photo(src: str, tmp: str, i: int) -> str:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return src
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            dst = os.path.join(tmp, f"photo_{i:03d}.jpg")
            im.save(dst, quality=95)
            return dst
    except Exception:
        return src


def render_segment(i: int, it: dict, a: argparse.Namespace, w: int, h: int, tmp: str, enc: list[str]) -> str:
    out = os.path.join(tmp, f"seg_{i:03d}.mkv")
    L = it["len"]
    frames = max(1, round(L * a.fps))
    if it["kind"] == "photo":
        src = prepare_photo(it["path"], tmp, i)
        fc = fit_filter(a.fit, 2 * w, 2 * h, "0:v", "fit", 60)
        if a.kenburns:
            fc += f";[fit]{kenburns(i, frames, w, h, a.fps, a.zoom)},format=yuv420p[v]"
        else:
            fc += f";[fit]scale={w}:{h},loop=loop=-1:size=1,fps={a.fps},format=yuv420p[v]"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a"
    else:
        fc = fit_filter(a.fit, w, h, "0:v", "fit", 30)
        fc += (f";[fit]fps={a.fps},tpad=stop_mode=clone:stop_duration={L:.3f},"
               f"format=yuv420p[v]")
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{it['start']:.3f}", "-t", f"{L + 0.5:.3f}",
               "-i", it["path"], "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        if a.clip_audio and it["has_audio"]:
            fc += (f";[0:a]aresample=48000,aformat=channel_layouts=stereo,"
                   f"volume={a.clip_volume},apad[a]")
            amap = "[a]"
        else:
            amap = "1:a"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", amap, "-frames:v", str(frames),
            "-t", f"{frames / a.fps:.4f}", *enc, "-c:a", "pcm_s16le", out]
    run(cmd)
    it["len"] = frames / a.fps
    return out


def find_font(tmp: str) -> str | None:
    path = None
    if shutil.which("fc-match"):
        res = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"],
                             stdout=subprocess.PIPE, text=True)
        path = res.stdout.strip() or None
    for cand in [path, "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]:
        if cand and os.path.isfile(cand):
            dst = os.path.join(tmp, "font" + os.path.splitext(cand)[1])
            shutil.copy(cand, dst)
            return dst
    return None


def wrap_text(text: str, max_chars: int) -> list[str]:
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        cur = ""
        for word in para.split():
            if cur and len(cur) + 1 + len(word) > max_chars:
                lines.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        lines.append(cur)
    return [ln for ln in lines if ln] or [text]


def title_filter(text: str, font: str, tmp: str, w: int, h: int, start: float, dur: float,
                 anchor: str) -> list[str]:
    """One drawtext per line so each line is centred; wraps to ~88% of the frame width."""
    size = int(min(w, h) * 0.075)
    lines = wrap_text(text, max(8, int(w * 0.88 / (size * 0.58))))
    longest = max(len(ln) for ln in lines)
    size = min(size, int(w * 0.88 / (max(longest, 1) * 0.58)))
    lh = int(size * 1.25)
    block = lh * len(lines)
    top = int(h * 0.14) if anchor == "top" else (h - block) // 2
    s, e = start, start + dur
    alpha = (f"if(lt(t,{s + 0.6:.2f}),(t-{s:.2f})/0.6,"
             f"if(lt(t,{e - 0.6:.2f}),1,({e:.2f}-t)/0.6))")
    out = []
    for k, line in enumerate(lines):
        tf = os.path.join(tmp, f"text_{int(start * 1000)}_{k}.txt")
        with open(tf, "w", encoding="utf-8") as f:
            f.write(line)
        out.append(f"drawtext=fontfile={font}:textfile={tf}:fontsize={size}:fontcolor=white:"
                   f"borderw={max(2, size // 18)}:bordercolor=black@0.55:shadowx=2:shadowy=2:"
                   f"shadowcolor=black@0.35:x=(w-text_w)/2:y={top + k * lh}:"
                   f"alpha='{alpha}':enable='between(t,{s:.2f},{e:.2f})'")
    return out


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Video montage from photos and clips (ffmpeg).")
    ap.add_argument("inputs", nargs="+", help="photos, videos, or folders (sorted by name)")
    ap.add_argument("-o", "--output", default="mantaj.mp4")
    ap.add_argument("--music", help="background music file")
    ap.add_argument("--aspect", default="9:16", choices=sorted(ASPECTS))
    ap.add_argument("--size", help="explicit WxH, overrides --aspect (e.g. 1280x720)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--fit", default="blur", choices=["blur", "cover", "pad"],
                    help="blur: blurred fill behind (default); cover: crop to fill; pad: black bars")
    ap.add_argument("--photo-dur", type=float, default=3.0, help="seconds per photo")
    ap.add_argument("--clip-max", type=float, default=6.0, help="max seconds taken from each clip")
    ap.add_argument("--clip-start", type=float, default=0.0, help="start offset inside clips")
    ap.add_argument("--clip-audio", action="store_true", help="keep the clips' own sound")
    ap.add_argument("--clip-volume", type=float, default=1.0)
    ap.add_argument("--transition", default="fade",
                    help="none, random, or an ffmpeg xfade name (fade, dissolve, slideleft, ...)")
    ap.add_argument("--tdur", type=float, default=0.6, help="transition seconds")
    ap.add_argument("--no-kenburns", dest="kenburns", action="store_false")
    ap.add_argument("--zoom", type=float, default=1.10, help="Ken Burns zoom amount")
    ap.add_argument("--beat-sync", action="store_true", help="snap cuts to the music beat")
    ap.add_argument("--duration", help="target length in seconds, or 'music' for the song length")
    ap.add_argument("--music-volume", type=float, default=1.0)
    ap.add_argument("--title", help="title text shown at the start")
    ap.add_argument("--title-dur", type=float, default=3.0)
    ap.add_argument("--end-text", help="text shown over the last seconds")
    ap.add_argument("--no-fade", dest="fade", action="store_false", help="no fade in/out")
    ap.add_argument("--preview", action="store_true", help="half size, fast encode")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7, help="seed for --transition random")
    ap.add_argument("--sheet", help="also write a contact-sheet JPG for visual QC")
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
    ap.add_argument("--keep-temp", action="store_true", help="keep intermediate segments (debug)")
    a = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not found; install ffmpeg first")
    if a.size:
        m = re.fullmatch(r"(\d+)[xX](\d+)", a.size)
        if not m:
            die("--size must look like 1280x720")
        w, h = int(m.group(1)), int(m.group(2))
    else:
        w, h = ASPECTS[a.aspect]
    if a.preview:
        w, h = w // 2, h // 2
    w, h = w - w % 2, h - h % 2
    tdur = 0.0 if a.transition == "none" else max(0.05, a.tdur)

    items = []
    for p in collect_inputs(a.inputs):
        ext = os.path.splitext(p)[1].lower()
        if ext in PHOTO_EXT:
            items.append({"path": p, "kind": "photo", "want": a.photo_dur, "max": 1e9, "start": 0.0})
        else:
            info = probe(p)
            if not info["has_video"]:
                print(f"mantaj: skipping {p} (no video stream)", file=sys.stderr)
                continue
            avail = max(0.3, info["duration"] - a.clip_start)
            items.append({"path": p, "kind": "video", "want": min(a.clip_max, avail), "max": avail,
                          "start": a.clip_start, "has_audio": info["has_audio"]})
    if not items:
        die("nothing to montage")
    if len(items) > 1:
        for it in items:  # every shot must outlast two transitions
            it["want"] = max(it["want"], 2 * tdur + MIN_HOLD)
            it["max"] = max(it["max"], 2 * tdur + MIN_HOLD) if it["kind"] == "photo" else it["max"]

    music_len = probe(a.music)["duration"] if a.music else None
    target = None
    if a.duration:
        if a.duration == "music":
            if not music_len:
                die("--duration music needs --music")
            target = music_len
        else:
            target = float(a.duration)

    beats, bpm = None, None
    if a.beat_sync:
        if not a.music:
            die("--beat-sync needs --music")
        est = target or sum(it["want"] for it in items) + 10
        bpm, beats = detect_beats(a.music, est * 1.5)

    offsets = plan_timeline(items, tdur, beats, target)
    total = (offsets[-1] if offsets else 0.0) + items[-1]["len"]
    rng = random.Random(a.seed)
    trans = [(rng.choice(RANDOM_TRANSITIONS) if a.transition == "random" else a.transition)
             for _ in offsets]

    plan = {"size": f"{w}x{h}", "fps": a.fps, "total_sec": round(total, 2), "bpm": bpm,
            "segments": [{"file": os.path.basename(it["path"]), "kind": it["kind"],
                          "sec": round(it["len"], 2)} for it in items],
            "cuts_at": [round(o + tdur / 2, 2) for o in offsets], "transitions": trans}
    if a.dry_run:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return

    enc = ["-c:v", "libx264", "-preset", "ultrafast" if a.preview else "veryfast",
           "-crf", "18" if not a.preview else "26", "-pix_fmt", "yuv420p"]
    tmp = tempfile.mkdtemp(prefix="mantaj_")
    try:
        segs = []
        for i, it in enumerate(items):
            print(f"mantaj: segment {i + 1}/{len(items)} {os.path.basename(it['path'])}",
                  file=sys.stderr)
            segs.append(render_segment(i, it, a, w, h, tmp, enc))
        # rendering rounds to whole frames; recompute offsets from the real lengths
        offsets, start = [], 0.0
        for it in items[:-1]:
            o = start + it["len"] - tdur
            offsets.append(o)
            start = o
        total = (offsets[-1] if offsets else 0.0) + items[-1]["len"]

        n = len(segs)
        seg_inputs = [x for sg in segs for x in ("-i", sg)]

        # Pass 1: audio. Kept apart from the video graph because ffmpeg drops the
        # tail of acrossfade chains when both live in one filtergraph.
        apart = [f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]" for i in range(n)]
        alast = "a0"
        if n > 1 and tdur == 0:
            apart.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ac]")
            alast = "ac"
        elif n > 1:
            for i in range(1, n):
                apart.append(f"[{alast}][a{i}]acrossfade=d={tdur:.3f}[ax{i}]")
                alast = f"ax{i}"
        acmd = ["ffmpeg", "-y", "-v", "error", *seg_inputs]
        if a.music:
            acmd += ["-stream_loop", "-1", "-i", a.music]
            fade_out = min(2.0, total / 3)
            apart.append(f"[{n}:a]atrim=0:{total + 1:.3f},asetpts=N/SR/TB,aresample=48000,"
                         f"aformat=channel_layouts=stereo,volume={a.music_volume},"
                         f"afade=t=in:st=0:d=0.3,afade=t=out:st={total - fade_out:.3f}:"
                         f"d={fade_out:.3f}[mus]")
            apart.append(f"[{alast}][mus]amix=inputs=2:duration=first:normalize=0,"
                         f"alimiter=limit=0.95,apad,atrim=0:{total:.3f}[aout]")
        else:
            apart.append(f"[{alast}]apad,atrim=0:{total:.3f}[aout]")
        mix = os.path.join(tmp, "mix.wav")
        acmd += ["-filter_complex", ";".join(apart), "-map", "[aout]", "-c:a", "pcm_s16le", mix]
        print("mantaj: audio mix", file=sys.stderr)
        run(acmd)

        # Pass 2: video, joined with transitions, then muxed with the mixed audio.
        vpart = [f"[{i}:v]settb=AVTB,fps={a.fps},format=yuv420p,setsar=1[v{i}]" for i in range(n)]
        vlast = "v0"
        if n > 1 and tdur == 0:
            vpart.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
            vlast = "vc"
        elif n > 1:
            for i in range(1, n):
                vpart.append(f"[{vlast}][v{i}]xfade=transition={trans[i - 1]}:duration={tdur:.3f}:"
                             f"offset={offsets[i - 1]:.3f}[vx{i}]")
                vlast = f"vx{i}"
        post = []
        if a.title or a.end_text:
            font = find_font(tmp)
            if not font:
                print("mantaj: no font found, skipping text", file=sys.stderr)
            else:
                if a.title:
                    post += title_filter(a.title, font, tmp, w, h, 0.3,
                                         min(a.title_dur, max(1.5, total - 0.6)), "top")
                if a.end_text:
                    d = min(3.0, total)
                    post += title_filter(a.end_text, font, tmp, w, h, total - d, d, "middle")
        if a.fade:
            post += ["fade=t=in:st=0:d=0.5", f"fade=t=out:st={max(0.0, total - 1.0):.3f}:d=1"]
        post.append("format=yuv420p")
        vpart.append(f"[{vlast}]" + ",".join(post) + "[vout]")
        vcmd = ["ffmpeg", "-y", "-v", "error", *[x for sg in segs for x in ("-an", "-i", sg)],
                "-i", mix, "-filter_complex", ";".join(vpart), "-map", "[vout]", "-map", f"{n}:a",
                "-t", f"{total:.3f}", "-c:v", "libx264",
                "-preset", "ultrafast" if a.preview else "medium",
                "-crf", str(28 if a.preview else a.crf), "-pix_fmt", "yuv420p",
                "-r", str(a.fps), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                a.output]
        print("mantaj: video render", file=sys.stderr)
        run(vcmd)

        if a.sheet:
            cols = 4 if h > w else 3
            tiles = cols * 3
            run(["ffmpeg", "-y", "-v", "error", "-i", a.output, "-vf",
                 f"fps={tiles / max(total, 0.1):.5f},scale=-2:{360 if h > w else 240},"
                 f"tile={cols}x3:padding=6:color=white", "-frames:v", "1", a.sheet])
        plan["total_sec"] = round(total, 2)
        plan["segments"] = [{"file": os.path.basename(it["path"]), "kind": it["kind"],
                             "sec": round(it["len"], 2)} for it in items]
        plan["cuts_at"] = [round(o + tdur / 2, 2) for o in offsets]
        plan["output"] = a.output
        if a.sheet:
            plan["sheet"] = a.sheet
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    finally:
        if a.keep_temp:
            print(f"mantaj: temp files kept in {tmp}", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
