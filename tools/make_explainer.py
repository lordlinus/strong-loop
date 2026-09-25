"""make_explainer.py — build the ~90-second narrated explainer on page 1, from the committed page.

    python tools/make_explainer.py --speech-endpoint https://<account>.cognitiveservices.azure.com
        -> docs/explainer.mp4 (+ .webm), docs/explainer.vtt, docs/explainer.jpg

Concept cards first, then the real recorded run on the same wheel as docs/index.html, so the
video can never show a different loop from the page. Every frame is a screenshot of a state
(no screen recording), and each scene lasts exactly as long as its narration, so re-running it
after the run or the page changes gives a video that still lines up.

Needs: a local docs/index.html (build it with make_showcase.py first), Playwright's Chromium,
ffmpeg, and an Entra identity with "Cognitive Services Speech User" on the Speech account
(key auth is off on ours). SPEECH_ENDPOINT may stand in for --speech-endpoint.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import wave
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
W, H = 1920, 1080
VOICE = "en-US-AndrewMultilingualNeural"
PAD = 0.5          # silence after each scene's narration, seconds
SETTLE_MS = 450    # let the wheel's CSS transitions finish before a screenshot

# Title cards share the page's palette and fonts; they are laid over the page, not a separate site.
CARD_CSS = """
#xp{position:fixed;inset:0;z-index:99;background:var(--paper);display:flex;align-items:center;justify-content:center;font-family:var(--body);color:var(--ink)}
#xp .in{width:1280px}
#xp .k{font:600 22px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--graphite);margin:0 0 22px}
#xp h2{font:800 96px/1.02 var(--display);letter-spacing:-.035em;margin:0 0 36px} #xp h2 .c{color:var(--code)}
#xp p{font-size:36px;line-height:1.4;color:var(--graphite);margin:0 0 14px;max-width:30em} #xp p b{color:var(--ink);font-weight:600}
#xp .who{font-size:20px;padding:4px 10px;vertical-align:middle;margin-right:10px}
#xp .split{display:grid;grid-template-columns:1fr 460px;gap:70px;align-items:center}
#xp .ring{width:460px;height:460px;border:5px solid var(--code);border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:60px}
#xp .ring p{font-size:30px;margin:14px 0 0}
#xp .foot{font:600 22px var(--mono);color:var(--code);margin-top:40px;letter-spacing:.04em}
.site-nav,.controls,#watch,footer{visibility:hidden}
"""


def cards(run: dict) -> dict[str, str]:
    return {
        "title": f"""<p class="k">strong-loop · an autonomous loop</p>
            <h2>The agent proposes.<br><span class="c">Code disposes.</span></h2>
            <p>An AI analyst that runs on its own, <b>and that you can still trust.</b></p>""",
        "problem": """<p class="k">the problem</p><h2>Fluent is not the same as true.</h2>
            <p>Ask a model to analyse data and you get confident findings.</p>
            <p><b>Some are real. Some are noise.</b> From the text alone, you can't tell which.</p>""",
        "split": """<p class="k">the idea</p><div class="split"><div><h2>Split the job.</h2>
            <p><span class="who who-model">model</span><b>proposes</b> · outside the ring. Reads, picks a question, states a claim.</p>
            <p><span class="who who-human">human</span><b>signs the charter</b> · what the role owns, may do, and what counts as proof.</p></div>
            <div class="ring"><span class="who who-code">code</span><p><b>decides</b> · inside the ring. Runs the statistics, gives the verdict, approves actions.</p></div></div>""",
        "loop": """<p class="k">the loop</p><h2>Then let it loop.</h2>
            <p>Every round starts from <b>an empty context.</b></p>
            <p>The only memory is <b>an append-only ledger, written by code.</b></p>
            <p>It stops when the ledger says so — <b>never because the model says it's done.</b></p>""",
        "close": f"""<p class="k">one engine · any role</p><h2>The role lives in a charter,<br><span class="c">not in code.</span></h2>
            <p>Nothing in the engine mentions {run['role'].split('_')[0]}s. A new role is a new charter — never new code.</p>
            <p class="foot">write a charter → run it</p>""",
    }


def scenes(run: dict, events: list[dict]) -> list[dict]:
    """Narration + what is on screen. Wheel positions are found in the run, never hard-coded."""
    def at(pred, start=0):
        return [k + 1 for k, e in enumerate(events) if k >= start and pred(e)]
    tool = lambda name: (lambda e: e.get("tool") == name)
    gate = lambda e: (e.get("args") or {}).get("kind")
    iters = at(lambda e: e.get("kind") == "iteration_start")
    r2 = iters[1] - 1 if len(iters) > 1 else len(events)
    first_test = at(tool("test_hypothesis"))[0]
    tests = at(lambda e: e.get("tool") == "test_hypothesis" and gate(e) != "driver_effect")
    tests = [k for k in tests if k < r2]
    challenges = [k for k in at(lambda e: gate(e) == "driver_effect") if k < r2]
    records = [k for k in at(tool("record_finding")) if k < r2]
    acts = [k for k in at(tool("propose_action")) if k < r2]
    thin = lambda ks, n=4: ks if len(ks) <= n else [ks[round(i * (len(ks) - 1) / (n - 1))] for i in range(n)]
    later = [k for k in range(r2 + 1, len(events) + 1)]
    rows = f"{run['dataset']['rows']:,}"
    return [
        {"card": "title", "say": "This is strong-loop: an AI analyst that runs on its own, and that you can still trust."},
        {"card": "problem", "say": "Ask a model to analyse data and you get confident findings. Some are real. Some are noise. From the text alone, you can't tell which."},
        {"card": "split", "say": "So we split the job. The model works outside the ring: it reads, and proposes claims. Only fixed code works inside: it runs the statistics, gives the verdict, and approves any action. A person signs the charter that sets the rules."},
        {"card": "loop", "say": "Then we let it loop. Every round starts from an empty context. The only memory is a ledger, written by code. And it stops when the ledger says so, not when the model says it's done."},
        {"wheel": [0, 3, first_test - 1], "say": f"Here is a real run: a {run['role'].replace('_', ' ')} charter over {rows} claims. The model reads its brief, and picks a question the charter allows."},
        {"wheel": thin(tests), "say": "It commits to each claim before seeing any result. Code tests it against the rows, and applies the charter's standard of proof."},
        {"wheel": thin(challenges + records, 5), "say": "A pass isn't enough. The same group is re-tested with a likely confound held constant, before a finding may be recorded."},
        {"wheel": acts, "say": "Actions need a right the charter grants, evidence that clears its bar, and a blast radius inside the cap."},
        {"wheel": [later[0], later[len(later) // 2], len(events)], "say": "Round two starts fresh, from the ledger alone. At the end, every p-value is corrected together, and lucky results are demoted."},
        {"card": "close", "say": "The role lives in a charter, not in code. Write one for your own role, and run it."},
    ]


def tts(endpoint: str, token: str, text: str, out: Path) -> float:
    ssml = f'<speak version="1.0" xml:lang="en-US"><voice name="{VOICE}">{escape(text)}</voice></speak>'
    req = urllib.request.Request(endpoint.rstrip("/") + "/tts/cognitiveservices/v1", data=ssml.encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm", "User-Agent": "strong-loop-explainer"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out.write_bytes(r.read())
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


def narration_track(clips: list[tuple[Path, float]], out: Path) -> None:
    """One WAV: each clip followed by silence up to its scene's length."""
    with wave.open(str(clips[0][0])) as w0:
        params = w0.getparams()
    with wave.open(str(out), "wb") as dst:
        dst.setparams(params)
        for path, length in clips:
            with wave.open(str(path)) as src:
                frames = src.readframes(src.getnframes())
            need = int(length * params.framerate) * params.sampwidth - len(frames)
            dst.writeframes(frames + b"\0" * max(0, need))


def vtt_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def captions(plan: list[dict]) -> str:
    """One cue per sentence, timed by its share of the scene's spoken length."""
    cues, t = ["WEBVTT", ""], 0.0
    for sc in plan:
        parts = [s for s in re.split(r"(?<=[.!?])\s+", sc["say"]) if s]
        total = sum(len(p) for p in parts)
        start = t
        for p in parts:
            end = start + sc["spoken"] * len(p) / total
            cues += [f"{vtt_time(start)} --> {vtt_time(end)}", p, ""]
            start = end
        t += sc["length"]
    return "\n".join(cues)


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--speech-endpoint", default=os.environ.get("SPEECH_ENDPOINT"))
    ap.add_argument("--out", type=Path, default=DOCS / "explainer.mp4")
    args = ap.parse_args()
    if not args.speech_endpoint:
        ap.error("pass --speech-endpoint or set SPEECH_ENDPOINT (https://<account>.cognitiveservices.azure.com)")
    if not shutil.which("ffmpeg"):
        ap.error("ffmpeg is not on PATH")

    from azure.identity import DefaultAzureCredential
    from playwright.sync_api import sync_playwright

    token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    work = Path(tempfile.mkdtemp(prefix="explainer-"))
    httpd, base = serve(DOCS)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # reduced motion stops the page's own autoplay; every state is set explicitly below
            page = browser.new_page(viewport={"width": W, "height": H}, reduced_motion="reduce")
            page.goto(base)
            page.evaluate("document.fonts.ready")
            page.add_style_tag(content=CARD_CSS)
            run = page.evaluate("({role: RUN.role, dataset: RUN.dataset})")
            events = page.evaluate("EVENTS.map(e => ({kind: e.kind, tool: e.tool, args: e.args && {kind: e.args.kind}}))")
            plan, html = scenes(run, events), cards(run)

            frames: list[tuple[Path, float]] = []
            clips: list[tuple[Path, float]] = []
            for i, sc in enumerate(plan):
                wav = work / f"s{i:02d}.wav"
                sc["spoken"] = tts(args.speech_endpoint, token, sc["say"], wav)
                sc["length"] = round(sc["spoken"] + PAD, 3)
                clips.append((wav, sc["length"]))
                states = [("card", sc["card"])] if "card" in sc else [("wheel", k) for k in sc["wheel"]]
                for j, (kind, value) in enumerate(states):
                    if kind == "card":
                        page.evaluate("h => { let x = document.getElementById('xp'); if (!x) { x = document.createElement('div'); x.id = 'xp'; document.body.append(x); } x.innerHTML = `<div class=in>${h}</div>`; }", html[value])
                    else:
                        page.evaluate("k => { document.getElementById('xp')?.remove(); goTo(k); }", value)
                    page.wait_for_timeout(SETTLE_MS)
                    png = work / f"f{i:02d}_{j:02d}.png"
                    page.screenshot(path=str(png))
                    frames.append((png, sc["length"] / len(states)))
                print(f"scene {i + 1}/{len(plan)}  {sc['length']:5.1f}s  {len(states)} frame(s)", file=sys.stderr)
            browser.close()
    finally:
        httpd.shutdown()

    narration_track(clips, work / "narration.wav")
    listing = "".join(f"file '{f}'\nduration {d:.3f}\n" for f, d in frames) + f"file '{frames[-1][0]}'\n"
    (work / "frames.txt").write_text(listing)
    total = sum(sc["length"] for sc in plan)
    frames_in = ["-f", "concat", "-safe", "0", "-i", str(work / "frames.txt"), "-i", str(work / "narration.wav"),
                 "-vf", f"fps=24,format=yuv420p,fade=t=in:d=0.4,fade=t=out:st={total - 0.6:.2f}:d=0.6", "-t", f"{total:.3f}"]
    # MP4/H.264 for every mainstream browser; WebM/VP9 for Chromium builds shipped without H.264
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *frames_in,
                    "-c:v", "libx264", "-preset", "slow", "-tune", "stillimage", "-crf", "24", "-profile:v", "high", "-level", "4.0",
                    "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(args.out)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *frames_in,
                    "-c:v", "libvpx-vp9", "-crf", "38", "-b:v", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
                    "-c:a", "libopus", "-b:a", "64k", str(args.out.with_suffix(".webm"))], check=True)
    args.out.with_suffix(".vtt").write_text(captions(plan))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(frames[0][0]), "-vf", "scale=1280:-1", "-q:v", "4",
                    str(args.out.with_suffix(".jpg"))], check=True)
    shutil.rmtree(work)
    print(f"wrote {args.out.relative_to(ROOT)} ({total:.0f}s, {args.out.stat().st_size / 1e6:.1f} MB) + .webm .vtt .jpg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
