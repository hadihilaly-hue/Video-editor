I'm building a simple auto-editor for my one-camera, one-person podcast, styled after the Dwarkesh Podcast: restrained, hard cuts only, no flashy transitions or effects.

edit.py in this folder is a working first version. Read it before doing anything. It currently:
- detects pauses with ffmpeg silencedetect and shortens long ones
- fakes three camera angles (wide, medium, close) by cropping, and switches angle at every cut to hide jump cuts
- finds my face with OpenCV to center the crops
- cleans audio (highpass + loudnorm), fades in/out at the start and end
- renders shot by shot into a temp folder so it can resume, then joins the shots

First, set it up and verify it on my machine (macOS, Apple Silicon):
1. Check ffmpeg and opencv-python are installed; install what's missing.
2. Ask me for a short real test clip, run edit.py on it, and extract a few frames from each angle so we can check the face framing. Fix the framing if it's off.

Then add these features, one at a time, testing each on the clip before moving on:
1. Transcription with word timestamps, run locally (faster-whisper or mlx-whisper). Save the transcript next to the output. Cache it so re-runs don't re-transcribe.
2. A review step: before rendering, write the planned cuts to a readable file (what's removed, with transcript text and timestamps) that I can edit to undo any cut. Add a flag to stop after writing this file, and a way to render from my edited version. Later features should add their cuts to this file too.
3. Retake removal: when I restart a sentence, keep only the last take.
4. Cold open: pick the 2 to 3 strongest moments (30 to 60 seconds total) and play them before the main video, followed by a simple title card.
5. Chapter title cards: split the episode into topics, show a plain card between sections, and write a chapters.txt with YouTube timestamps.

Use the Anthropic API for the judgment calls in 3, 4, and 5 (key in ANTHROPIC_API_KEY). Put the model name in one config constant. Cache API responses per input so re-runs don't re-bill. If the key is missing, skip those features and say so.

Constraints:
- Never modify or overwrite the source video.
- Keep it a command-line tool: python3 edit.py video.mp4. No web UI.
- Must handle 10 to 45 minute 4K files without loading the video into memory.
- Keep rendering resumable.
- Keep the look minimal: hard cuts, plain title cards, no wipes, zooms, or animated captions.
- Use Apple's hardware encoder (h264_videotoolbox / hevc_videotoolbox) if it keeps quality acceptable; otherwise tell me the tradeoff.
- Show me your plan before making big changes, and tell me what you changed after each step.
