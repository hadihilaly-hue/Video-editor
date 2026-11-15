#!/usr/bin/env python3
"""
Podcast Editor: a simple one-camera podcast auto-editor.

Give it one video of you talking. It will:
  1. Cut out dead air (long pauses, the silence before you start and after you stop).
  2. Fake three camera angles (wide, medium, close) by cropping into the frame,
     and switch angles at every cut so the cuts are hidden.
  3. Clean up the audio (rumble filter and consistent loudness).
  4. Fade in at the start and out at the end.

Usage:
    python3 edit.py my_video.mp4
    python3 edit.py my_video.mp4 -o edited.mp4 --max-pause 1.0
    python3 edit.py my_video.mp4 --dry-run        (show the plan, render nothing)

Needs: ffmpeg (brew install ffmpeg). Optional: opencv-python, for finding your face.
The render is resumable: if it stops partway, run the same command again.
"""
import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
from fractions import Fraction

ANGLES = ("wide", "medium", "close")
ANGLE_WEIGHTS = {"wide": 0.2, "medium": 0.45, "close": 0.35}


# ---------------------------------------------------------------- helpers

def die(msg):
    print("\nError: " + msg, file=sys.stderr)
    sys.exit(1)


def run(cmd, capture=True):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       errors="replace")
    return p.returncode, p.stdout, p.stderr


def fmt_time(t):
    t = max(0, int(round(t)))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def probe(path):
    code, out, err = run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", path])
    if code != 0:
        die(f"Could not read {path}\n{err.strip()}")
    info = json.loads(out)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        die("That file has no video track.")
    if a is None:
        die("That file has no audio track, so there is nothing to cut on.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if "rotate" in (v.get("tags") or {}):
        rot = int(float(v["tags"]["rotate"]))
    if abs(rot) % 180 == 90:
        w, h = h, w
    fps = Fraction(v.get("avg_frame_rate") or "0/1") if v.get("avg_frame_rate", "0/0") != "0/0" else Fraction(0)
    if fps <= 0 or fps > 120:
        fps = Fraction(v.get("r_frame_rate", "30/1"))
    if fps <= 0 or fps > 120:
        fps = Fraction(30)
    # Snap odd variable frame rates to a standard one.
    for std in (Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
                Fraction(30), Fraction(50), Fraction(60000, 1001), Fraction(60)):
        if abs(float(fps) - float(std)) < 0.02:
            fps = std
            break
    else:
        fps = Fraction(round(float(fps)))
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    if dur <= 0:
        die("Could not work out how long the video is.")
    return {"w": w, "h": h, "fps": fps, "duration": dur}


# ---------------------------------------------------------------- audio analysis

def measure_mean_volume(path):
    _, _, err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
                     "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else -25.0


def detect_silences(path, noise_db, min_len):
    _, _, err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
                     "-af", f"silencedetect=noise={noise_db}dB:d={min_len}",
                     "-f", "null", "-"])
    out, start = [], None
    for line in err.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    return out, start  # start is not None if the file ends in silence


# ---------------------------------------------------------------- planning

def build_keeps(silences, tail_silence_start, duration, max_pause, keep_pause, min_keep):
    """Turn the silence list into the stretches of video we keep."""
    half = keep_pause / 2.0
    cuts = []  # (cut_from, cut_to) stretches to remove
    for s, e in silences:
        if s <= 0.05:  # silence before you start talking
            if e > 0.4:
                cuts.append((0.0, max(0.0, e - 0.3)))
        elif e >= duration - 0.05:  # silence after you stop
            tail_silence_start = s
        elif (e - s) >= max_pause:
            cuts.append((s + half, e - half))
    if tail_silence_start is not None and duration - tail_silence_start > 0.6:
        cuts.append((tail_silence_start + 0.4, duration))

    keeps, pos = [], 0.0
    for a, b in sorted(cuts):
        if a - pos > 0.01:
            keeps.append([pos, a])
        pos = max(pos, b)
    if duration - pos > 0.01:
        keeps.append([pos, duration])

    # A very short kept stretch would make the angle flash. Put back the smaller
    # of the two pauses around it, so it joins a neighbour instead.
    while len(keeps) > 1:
        i = next((n for n, k in enumerate(keeps) if k[1] - k[0] < min_keep), None)
        if i is None:
            break
        gap_before = keeps[i][0] - keeps[i - 1][1] if i > 0 else float("inf")
        gap_after = keeps[i + 1][0] - keeps[i][1] if i < len(keeps) - 1 else float("inf")
        if gap_before <= gap_after:
            keeps[i - 1][1] = keeps[i][1]
            del keeps[i]
        else:
            keeps[i][1] = keeps[i + 1][1]
            del keeps[i + 1]
    return keeps


def split_long(keep, silences, min_shot, max_shot, rng):
    """Split a long stretch into several shots, only at natural short pauses."""
    a, b = keep
    points = sorted((s + e) / 2.0 for s, e in silences if a + min_shot < (s + e) / 2.0 < b - min_shot)
    shots, start = [], a
    while b - start > max_shot:
        target = start + rng.uniform(max(min_shot * 2, max_shot * 0.5), max_shot)
        cands = [p for p in points if start + min_shot <= p <= start + max_shot * 1.5 and b - p >= min_shot]
        if not cands:
            break
        cut = min(cands, key=lambda p: abs(p - target))
        shots.append((start, cut))
        start = cut
    shots.append((start, b))
    return shots


def pick_angle(prev, rng):
    choices = [x for x in ANGLES if x != prev]
    return rng.choices(choices, weights=[ANGLE_WEIGHTS[c] for c in choices])[0]


def make_plan(info, silences, tail, args):
    rng = random.Random(args.seed)
    keeps = build_keeps(silences, tail, info["duration"], args.max_pause, args.keep_pause, args.min_keep)
    if not keeps:
        die("Nothing left after cutting silence. Try a lower --noise value (for example --noise -45).")
    fps = info["fps"]
    frame = 1.0 / float(fps)
    shots, prev = [], None
    for k in keeps:
        for a, b in split_long(k, silences, args.min_shot, args.max_shot, rng):
            fa, fb = round(a / frame), round(b / frame)
            if fb - fa < 2:
                continue
            angle = "wide" if args.no_angles else pick_angle(prev, rng)
            prev = angle
            shots.append({"start_frame": fa, "frames": fb - fa, "angle": angle})
    return shots, keeps


# ---------------------------------------------------------------- framing

def find_face(path, duration, workdir):
    """Return (cx, cy, face_height) as fractions of the frame, or None."""
    try:
        import cv2  # type: ignore
    except Exception:
        return None
    cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    if not os.path.exists(cascade_path):
        return None
    det = cv2.CascadeClassifier(cascade_path)
    found = []
    n = 14
    for i in range(n):
        t = duration * (i + 0.5) / n
        img_path = os.path.join(workdir, f"probe_{i:02d}.jpg")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", path,
             "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", img_path])
        img = cv2.imread(img_path)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40))
        if len(faces) == 0:
            continue
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        H, W = gray.shape
        found.append(((x + w / 2) / W, (y + h / 2) / H, h / H))
    if len(found) < 3:
        return None
    return tuple(statistics.median(f[i] for f in found) for i in range(3))


def crop_boxes(info, out_h, face):
    """Work out the crop rectangle for each fake camera angle."""
    W, H = info["w"], info["h"]
    headroom = H / out_h  # how far we can zoom before it goes soft
    z_close = min(2.0, max(1.4, headroom))
    z_medium = 1.0 + (z_close - 1.0) * 0.45
    cx, cy = (face[0], face[1]) if face else (0.5, 0.40)
    boxes = {}
    for name, z in (("wide", 1.0), ("medium", z_medium), ("close", z_close)):
        cw, ch = int(W / z) // 2 * 2, int(H / z) // 2 * 2
        x = cx * W - cw / 2
        y = cy * H - ch * 0.42  # keep the face a little above the middle
        x = int(min(max(0, x), W - cw)) // 2 * 2
        y = int(min(max(0, y), H - ch)) // 2 * 2
        boxes[name] = (cw, ch, x, y)
    return boxes, z_close


# ---------------------------------------------------------------- rendering

def pick_encoder(fast):
    if fast:
        _, out, _ = run(["ffmpeg", "-hide_banner", "-encoders"])
        if "h264_videotoolbox" in out:
            return ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p"]


def render(path, info, shots, boxes, out_w, out_h, out_path, workdir, args):
    fps = info["fps"]
    frame = 1.0 / float(fps)
    enc = pick_encoder(args.fast)
    total = len(shots)
    seg_paths = []
    for i, s in enumerate(shots):
        seg = os.path.join(workdir, f"seg_{i:05d}.mkv")
        seg_paths.append(seg)
        if os.path.exists(seg) and os.path.getsize(seg) > 0:
            continue
        start, dur = s["start_frame"] * frame, s["frames"] * frame
        cw, ch, x, y = boxes[s["angle"]]
        vf = [f"crop={cw}:{ch}:{x}:{y}", f"scale={out_w}:{out_h}:flags=lanczos",
              f"fps={fps.numerator}/{fps.denominator}", "setsar=1"]
        af = ["afade=t=in:st=0:d=0.012", f"afade=t=out:st={max(0, dur - 0.012):.4f}:d=0.012"]
        if i == 0:
            vf.append("fade=t=in:st=0:d=0.5")
            af[0] = "afade=t=in:st=0:d=0.3"
        if i == total - 1:
            fd = min(0.6, dur / 2)
            vf.append(f"fade=t=out:st={dur - fd:.4f}:d={fd:.4f}")
            af[1] = f"afade=t=out:st={dur - fd:.4f}:d={fd:.4f}"
        tmp = seg + ".part.mkv"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.6f}", "-i", path, "-t", f"{dur:.6f}",
               "-map", "0:v:0", "-map", "0:a:0",
               "-vf", ",".join(vf), "-af", ",".join(af),
               "-frames:v", str(s["frames"]),
               *enc, "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", tmp]
        code, _, err = run(cmd)
        if code != 0 or not os.path.exists(tmp):
            die(f"ffmpeg failed on shot {i + 1}:\n{err.strip()[-800:]}")
        os.replace(tmp, seg)
        done = i + 1
        if done == total or done % 5 == 0:
            print(f"\r  rendering shot {done}/{total}", end="", flush=True)
    print()

    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        for p in seg_paths:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    print("  joining shots and cleaning up the audio")
    tmp_out = out_path + ".part.mp4"
    code, _, err = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-i", lst,
                        "-c:v", "copy",
                        "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                        "-movflags", "+faststart", tmp_out])
    if code != 0:
        die("ffmpeg failed while joining the shots:\n" + err.strip()[-800:])
    os.replace(tmp_out, out_path)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Simple one-camera podcast auto-editor.")
    ap.add_argument("input", nargs="?", help="the video to edit")
    ap.add_argument("-o", "--output", help="where to save (default: <name>_edited.mp4 next to the original)")
    ap.add_argument("--max-pause", type=float, default=0.9, help="pauses longer than this get shortened (seconds, default 0.9)")
    ap.add_argument("--keep-pause", type=float, default=0.35, help="how much of a shortened pause to keep (default 0.35)")
    ap.add_argument("--noise", type=float, default=None, help="silence threshold in dB (default: measured from your audio)")
    ap.add_argument("--min-shot", type=float, default=4.0, help="shortest time to hold an angle (default 4)")
    ap.add_argument("--max-shot", type=float, default=22.0, help="longest time to hold an angle when there is a pause to cut on (default 22)")
    ap.add_argument("--min-keep", type=float, default=2.0, help=argparse.SUPPRESS)
    ap.add_argument("--height", type=int, default=1080, help="output height (default 1080)")
    ap.add_argument("--no-angles", action="store_true", help="only trim, keep the original framing")
    ap.add_argument("--fast", action="store_true", help="use the Mac hardware encoder (faster, larger files)")
    ap.add_argument("--seed", type=int, default=7, help="change this to get a different pattern of angles")
    ap.add_argument("--dry-run", action="store_true", help="show the plan without rendering")
    ap.add_argument("--workdir", help=argparse.SUPPRESS)
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} is not installed. On a Mac, install it with:  brew install ffmpeg")

    if not args.input:
        raw = input("Drag your video into this window and press Enter: ").strip()
        parts = shlex.split(raw) if raw else []
        if not parts:
            die("No video given.")
        args.input = parts[0]
    src = os.path.abspath(os.path.expanduser(args.input))
    if not os.path.isfile(src):
        die(f"File not found: {src}")
    out_path = os.path.abspath(os.path.expanduser(args.output)) if args.output else \
        os.path.splitext(src)[0] + "_edited.mp4"
    if os.path.abspath(out_path) == src:
        die("The output would overwrite the original. Pick a different -o name.")

    print(f"Reading {os.path.basename(src)}")
    info = probe(src)
    print(f"  {info['w']}x{info['h']}, {float(info['fps']):.2f} fps, {fmt_time(info['duration'])} long")

    print("Listening for pauses")
    noise = args.noise
    if noise is None:
        mean = measure_mean_volume(src)
        noise = min(-28.0, max(-50.0, mean - 14.0))
    silences, tail = detect_silences(src, noise, 0.25)
    shots, keeps = make_plan(info, silences, tail, args)

    out_h = min(args.height, info["h"]) // 2 * 2
    out_w = int(round(info["w"] * out_h / info["h"])) // 2 * 2
    new_len = sum(s["frames"] for s in shots) / float(info["fps"])

    key = hashlib.sha1(json.dumps([src, os.path.getmtime(src), shots, out_h, args.fast],
                                  sort_keys=True).encode()).hexdigest()[:12]
    workdir = args.workdir or os.path.join(tempfile.gettempdir(), f"podcast_edit_{key}")
    os.makedirs(workdir, exist_ok=True)

    face = None
    if not args.no_angles:
        print("Finding your face for the close-ups")
        face = find_face(src, info["duration"], workdir)
        if face is None:
            print("  (no face found, or opencv not installed: framing on the upper middle of the frame)")
    boxes, z_close = crop_boxes(info, out_h, face)

    counts = {a: sum(1 for s in shots if s["angle"] == a) for a in ANGLES}
    print("\nPlan")
    print(f"  silence threshold   {noise:.0f} dB")
    print(f"  length              {fmt_time(info['duration'])} -> {fmt_time(new_len)} "
          f"({fmt_time(info['duration'] - new_len)} removed in {max(0, len(keeps) - 1)} cuts)")
    print(f"  shots               {len(shots)}  (wide {counts['wide']}, medium {counts['medium']}, close {counts['close']})")
    print(f"  output              {out_w}x{out_h} -> {out_path}")
    if not args.no_angles and info["h"] / out_h < z_close - 0.05:
        print("  note                the source is not much bigger than the output, so close-ups will look a little soft.\n"
              "                      Recording in 4K fixes this.")
    if args.dry_run:
        shutil.rmtree(workdir, ignore_errors=True)
        print("\nDry run: nothing rendered.")
        return

    print("\nRendering (safe to stop and rerun, it picks up where it left off)")
    render(src, info, shots, boxes, out_w, out_h, out_path, workdir, args)
    shutil.rmtree(workdir, ignore_errors=True)
    print(f"\nDone: {out_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue.")
        sys.exit(130)
