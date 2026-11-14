---
name: video-mantaj
description: Video montage / editing with ffmpeg. Turns photos and video clips into one finished video (Reels, Shorts, TikTok 9:16, YouTube 16:9, square) with transitions, Ken Burns motion, background music, beat-synced cuts, title and end text; also trims, joins, speeds up, subtitles, watermarks, compresses or converts existing videos. Use when the user asks for a video montage or edit, in any language, e.g. "video montaj qil", "mantaj", "rasmlardan video", "klip yasab ber", "slideshow", "montage", "reels", "edit these clips together", "add music to video".
---

# video-mantaj

Builds videos from the user's own photos, clips and music using ffmpeg. The main tool is
`scripts/mantaj.py` (next to this file). For one-off edits on a single video, use the ffmpeg
recipes below instead.

Reply in the user's language (often Uzbek). Keep technical flags out of the reply unless asked.

## Requirements

- `ffmpeg` + `ffprobe` (check with `ffmpeg -version`). If missing: `apt-get install -y ffmpeg`
  (Linux), `brew install ffmpeg` (macOS), `winget install Gyan.FFmpeg` (Windows).
- Python 3.9+. `numpy` only for `--beat-sync`; `Pillow` (optional) fixes rotated phone photos.

## Workflow for a montage

1. **Collect inputs.** Find the photos/clips and music the user means (a folder is fine; files
   are sorted by name, so rename or list them explicitly to control order). If the user gave
   nothing, ask for the media. Never download copyrighted music for them.
2. **Decide the format** from the request; only ask if it truly matters and is unclear:
   - Reels / Shorts / TikTok / Stories → `--aspect 9:16` (default)
   - YouTube / TV → `--aspect 16:9`; Instagram feed → `--aspect 4:5` or `1:1`
   - Calm / wedding / memories → `--transition fade --tdur 0.8 --photo-dur 3.5`
   - Energetic / sport / party → `--transition random --tdur 0.4 --photo-dur 1.5 --beat-sync`
   - "Fit the whole song" → `--duration music`; "30 seconds" → `--duration 30`
3. **Plan first:** add `--dry-run` to print segment lengths, cut times, BPM and transitions.
4. **Preview:** run with `--preview --sheet sheet.jpg` (half size, fast), then open
   `sheet.jpg` with the Read tool and look at it: framing, text legibility, black frames.
5. **Final render** without `--preview`, keep `--sheet`, check the JSON summary
   (`total_sec`, `output`) and that video and audio lengths match:
   `ffprobe -v error -show_entries stream=codec_type,duration -of csv=p=0 out.mp4`
6. **Deliver** the file path (send the file if a send-file tool exists) plus a one-line
   description: length, format, music, number of shots.

## mantaj.py reference

```bash
python3 <skill-dir>/scripts/mantaj.py INPUTS... -o out.mp4 [options]
```

| Option | Default | Meaning |
|---|---|---|
| `INPUTS` | | photos, clips and/or folders (jpg png webp bmp tif / mp4 mov mkv webm avi m4v 3gp mts) |
| `--music FILE` | none | background track; looped if shorter, faded out at the end |
| `--aspect` | `9:16` | `9:16` 1080x1920, `16:9` 1920x1080, `1:1`, `4:5`; or `--size WxH` |
| `--fit` | `blur` | `blur` = blurred copy fills empty space; `cover` = crop to fill; `pad` = black bars |
| `--photo-dur` | `3.0` | seconds per photo |
| `--clip-max` / `--clip-start` | `6` / `0` | how much of each clip to use, and from where |
| `--clip-audio`, `--clip-volume` | off, `1.0` | keep the clips' own sound (mixed under the music) |
| `--transition` | `fade` | `none` (hard cuts), `random`, or any ffmpeg xfade name: fade, dissolve, fadeblack, fadewhite, wipeleft, slideleft, slideup, smoothleft, circleopen, radial, zoomin, hblur, pixelize ... |
| `--tdur` | `0.6` | transition length in seconds |
| `--beat-sync` | off | detect the music's tempo and put cuts on beats (needs `--music`) |
| `--duration N\|music` | auto | stretch photo durations so the film lasts N seconds / the whole song |
| `--no-kenburns`, `--zoom` | on, `1.10` | slow zoom/pan on photos |
| `--title TEXT`, `--title-dur` | none, `3` | title at the top of the opening (auto-wrapped, Unicode ok) |
| `--end-text TEXT` | none | centred text over the last 3 seconds |
| `--music-volume` | `1.0` | music gain |
| `--no-fade` | fades on | disable fade in from / out to black |
| `--preview` | off | half resolution, fast encode |
| `--crf` | `20` | quality (lower = better/bigger; 18–23 is sensible) |
| `--fps` | `30` | frame rate |
| `--sheet FILE.jpg` | none | contact sheet of 12 frames for visual QC |
| `--dry-run` | off | print the plan JSON only |
| `--seed` | `7` | changes the `random` transition sequence |

Output: H.264 + AAC MP4 with `+faststart` (plays everywhere, uploads to Telegram/Instagram/YouTube).
Rendering takes roughly 2 seconds per second of 1080p output on a 4-core machine.

## ffmpeg recipes for single-video edits

```bash
# Trim (re-encode, frame-accurate): 00:12 to 00:27
ffmpeg -ss 12 -to 27 -i in.mp4 -c:v libx264 -crf 20 -c:a aac out.mp4
# Join clips that share codec/size, no re-encode
printf "file '%s'\n" a.mp4 b.mp4 > list.txt && ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4
# Speed x2 (video + audio) / slow motion x0.5
ffmpeg -i in.mp4 -vf "setpts=PTS/2" -af "atempo=2" out.mp4
ffmpeg -i in.mp4 -vf "setpts=PTS*2" -af "atempo=0.5" out.mp4
# Landscape -> vertical 9:16 (centre crop)
ffmpeg -i in.mp4 -vf "crop=ih*9/16:ih,scale=1080:1920" -c:a copy out.mp4
# Replace audio with music (cut to video length)
ffmpeg -i in.mp4 -i song.mp3 -map 0:v -map 1:a -c:v copy -shortest out.mp4
# Burn subtitles (.srt)
ffmpeg -i in.mp4 -vf "subtitles=subs.srt:force_style='FontSize=22,Outline=2'" out.mp4
# Logo / watermark in the bottom-right corner
ffmpeg -i in.mp4 -i logo.png -filter_complex "[1]scale=iw*0.15:-1[l];[0][l]overlay=W-w-30:H-h-30" out.mp4
# Compress for messengers (~720p)
ffmpeg -i in.mp4 -vf "scale=-2:720" -c:v libx264 -crf 26 -preset slow -c:a aac -b:a 128k out.mp4
# Extract audio / make a GIF
ffmpeg -i in.mp4 -vn -c:a libmp3lame -q:a 2 out.mp3
ffmpeg -i in.mp4 -vf "fps=12,scale=480:-1:flags=lanczos" out.gif
```

## Pitfalls

- Photos appear in **file-name order**; pass files explicitly to set a custom order.
- `--beat-sync` only moves cuts by up to about half a shot; very short `--photo-dur` with long
  `--tdur` leaves little room. Keep `--tdur` below a third of the photo duration.
- `--duration` stretches **photos** only; clips keep their length (`--clip-max`).
- HEIC/HEIF photos: convert first (`heif-convert` or `pillow-heif`), ffmpeg may not read them.
- Long songs with `--duration music` and few photos give long, slow shots; suggest a shorter
  `--duration` or more photos.
- If a render fails, the script prints the failing ffmpeg command and its last error lines;
  `--keep-temp` keeps the intermediate segments for debugging.
