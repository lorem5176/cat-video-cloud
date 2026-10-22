#!/usr/bin/env python3
"""
Cloud-friendly port of AutoCompoBuilder.jsx (After Effects) using FFmpeg.

Expected folder layout (next to this script, or pass --root):
    Audio/            -> random audio track (sets the video length)
    Assets/Hooks/     -> 1 random clip (always first)
    Assets/Cats/      -> N random clips (right after Hook)
    Assets/Eye/       -> 1 random clip (right after Cats)
    Assets/Sad/       -> fills the remaining time until the audio ends

Same rules as the AE script:
  - Clips in Cats/Sad longer than MAX_CLIP_DURATION are split at the midpoint,
    second half mirrored. Hook and Eye are never split.
  - 1-2 random whole clips (not the auto-mirrored halves) are mirrored.
  - Output is cut to the audio length.
  - Frame 0 is replaced by a random frame from a random Cats clip (thumbnail).

Usage:
    python cat_video_maker.py --count 10
    python cat_video_maker.py --count 50 --workers 4 --out Compo
"""
import argparse
import json
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ================= CONFIGURABLE VARIABLES =================
MIN_CATS_CLIPS = 3
MAX_CATS_CLIPS = 6
MIN_MIRRORED_CLIPS = 1
MAX_MIRRORED_CLIPS = 2
MAX_CLIP_DURATION = 3        # seconds - split clips longer than this
COMP_WIDTH = 1080
COMP_HEIGHT = 1920
COMP_FRAMERATE = 30
MAX_SAD_ITERATIONS = 200     # safety cap
CRF = 20                     # lower = better quality / bigger file
PRESET = "veryfast"          # slower presets = smaller files, more CPU
# ==========================================================

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

_dur_cache = {}


def list_files(folder, exts):
    files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in exts)
    if not files:
        raise SystemExit(f"No usable files found in '{folder}'")
    return files


def duration(path):
    if path not in _dur_cache:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True).stdout
        _dur_cache[path] = float(json.loads(out)["format"]["duration"])
    return _dur_cache[path]


def add_clip(segments, path, kind, allow_split):
    """Append a clip as one segment, or two if it needs the split+mirror."""
    d = duration(path)
    if allow_split and d > MAX_CLIP_DURATION:
        half = d / 2
        segments.append(dict(path=path, start=0.0, length=half,
                             mirror=False, auto=False, kind=kind))
        segments.append(dict(path=path, start=half, length=d - half,
                             mirror=True, auto=True, kind=kind))
    else:
        segments.append(dict(path=path, start=0.0, length=d,
                             mirror=False, auto=False, kind=kind))
    return d


def plan_video(rng, audio_files, hooks, cats, eyes, sads):
    audio = rng.choice(audio_files)
    target = duration(audio)
    segments, t = [], 0.0

    t += add_clip(segments, rng.choice(hooks), "hook", allow_split=False)

    for _ in range(rng.randint(MIN_CATS_CLIPS, MAX_CATS_CLIPS)):
        t += add_clip(segments, rng.choice(cats), "cats", allow_split=True)

    t += add_clip(segments, rng.choice(eyes), "eye", allow_split=False)

    i = 0
    while t < target and i < MAX_SAD_ITERATIONS:
        t += add_clip(segments, rng.choice(sads), "sad", allow_split=True)
        i += 1

    # Random mirror of 1-2 whole clips (never the auto-mirrored halves)
    pool = [s for s in segments if not s["auto"]]
    n = min(rng.randint(MIN_MIRRORED_CLIPS, MAX_MIRRORED_CLIPS), len(pool))
    for s in rng.sample(pool, n):
        s["mirror"] = True

    # Thumbnail: one random frame from a random Cats clip
    thumb_clip = rng.choice(cats)
    max_t = max(duration(thumb_clip) - 1 / COMP_FRAMERATE, 0)
    thumb_time = rng.random() * max_t

    return dict(audio=audio, target=target, segments=segments,
                thumb_clip=thumb_clip, thumb_time=thumb_time)


COVER = (f"fps={COMP_FRAMERATE},"
         f"scale={COMP_WIDTH}:{COMP_HEIGHT}:force_original_aspect_ratio=increase,"
         f"crop={COMP_WIDTH}:{COMP_HEIGHT},setsar=1")


def render(plan, out_path):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    segs = plan["segments"]

    for s in segs:
        cmd += ["-ss", f"{s['start']:.4f}", "-t", f"{s['length']:.4f}",
                "-i", str(s["path"])]
    n = len(segs)
    cmd += ["-ss", f"{plan['thumb_time']:.4f}", "-i", str(plan["thumb_clip"])]
    cmd += ["-i", str(plan["audio"])]
    thumb_idx, audio_idx = n, n + 1

    parts = []
    for i, s in enumerate(segs):
        flip = ",hflip" if s["mirror"] else ""
        parts.append(f"[{i}:v]{COVER}{flip},setpts=PTS-STARTPTS[v{i}]")
    concat_in = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{concat_in}concat=n={n}:v=1:a=0[base]")
    parts.append(f"[{thumb_idx}:v]{COVER},trim=end_frame=1,setpts=PTS-STARTPTS[thumb]")
    parts.append(f"[base][thumb]overlay=enable='lt(t,{0.5 / COMP_FRAMERATE:.5f})'[vout]")

    cmd += ["-filter_complex", ";".join(parts),
            "-map", "[vout]", "-map", f"{audio_idx}:a",
            "-t", f"{plan['target']:.4f}", "-shortest",
            "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
            "-pix_fmt", "yuv420p", "-r", str(COMP_FRAMERATE),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(out_path)]
    subprocess.run(cmd, check=True)


def next_number(out_dir):
    nums = [int(p.stem) for p in out_dir.glob("*.mp4") if p.stem.isdigit()]
    return max(nums, default=0) + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="folder containing Audio/ and Assets/")
    ap.add_argument("--out", default="Compo", help="output folder")
    ap.add_argument("--count", type=int, default=1, help="how many videos to make")
    ap.add_argument("--workers", type=int, default=1, help="videos rendered in parallel")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    root = Path(args.root)
    audio = list_files(root / "Audio", AUDIO_EXT)
    hooks = list_files(root / "Assets" / "Hooks", VIDEO_EXT)
    cats = list_files(root / "Assets" / "Cats", VIDEO_EXT)
    eyes = list_files(root / "Assets" / "Eye", VIDEO_EXT)
    sads = list_files(root / "Assets" / "Sad", VIDEO_EXT)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    # Plan everything up front (cheap), then render in parallel (heavy)
    start = next_number(out_dir)
    jobs = []
    for k in range(args.count):
        plan = plan_video(rng, audio, hooks, cats, eyes, sads)
        jobs.append((plan, out_dir / f"{start + k}.mp4"))

    def run(job):
        plan, path = job
        render(plan, path)
        print(f"done: {path}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(run, jobs))
    print(f"Created {len(jobs)} video(s) in {out_dir}/")


if __name__ == "__main__":
    main()
