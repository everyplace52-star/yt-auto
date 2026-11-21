"""
Free YouTube history-documentary pipeline.
Topic -> script (Gemini) -> voice (edge-tts) -> images (Wikimedia Commons) -> video (FFmpeg) -> scheduled upload.
"""
import os, re, sys, csv, json, math, time, random, asyncio, argparse, subprocess, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import edge_tts

KEYS = [k for k in [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
] if k]
if not KEYS:
    raise SystemExit("No GEMINI_API_KEY found")
KEY_I = [0]
MODELS = [m for m in [
    os.getenv("GEMINI_MODEL"),
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-3.1-pro-preview",
] if m]
CUR = [0]
VOICE = os.getenv("VOICE", "en-US-AndrewNeural")
SECTIONS = 12
SLOT_HOURS_UTC = [15, 20, 24]
WORK, OUT, DONE, CACHE = Path("work"), Path("out"), Path("done.json"), Path("cache")


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def gemini(prompt, as_json=False):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    last = ""
    for rnd in range(3):
        for step in range(len(MODELS)):
            midx = (CUR[0] + step) % len(MODELS)
            model = MODELS[midx]
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            model_dead = False
            for kstep in range(len(KEYS)):
                if model_dead:
                    break
                kidx = (KEY_I[0] + kstep) % len(KEYS)
                key = KEYS[kidx]
                for attempt in range(2):
                    try:
                        r = requests.post(url, params={"key": key}, json=body, timeout=300)
                    except requests.exceptions.RequestException as e:
                        last = f"{model}: network/timeout {e}"
                        print(f"[{model}|key{kidx+1}] attempt {attempt+1} network/timeout", flush=True)
                        time.sleep(10 * (attempt + 1))
                        continue
                    if r.status_code == 200:
                        try:
                            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                        except Exception:
                            last = f"{model}: empty reply"
                            print(f"[{model}|key{kidx+1}] empty reply", flush=True)
                            break
                        if CUR[0] != midx or KEY_I[0] != kidx:
                            print(f"Now using model: {model} with key {kidx+1}", flush=True)
                        CUR[0], KEY_I[0] = midx, kidx
                        time.sleep(4)
                        return text
                    last = f"{model}: HTTP {r.status_code} {r.text[:200]}"
                    print(f"[{model}|key{kidx+1}] attempt {attempt+1}: HTTP {r.status_code}", flush=True)
                    if r.status_code == 404:
                        model_dead = True
                        break
                    if r.status_code in (400, 401, 403, 429):
                        break
                    time.sleep(10 * (attempt + 1))
        print(f"All models and keys busy, waiting 60s (round {rnd+1})", flush=True)
        time.sleep(60)
    raise RuntimeError(f"Gemini failed on all models and keys: {last}")


def make_script(topic):
    outline = json.loads(gemini(f"""
You are a documentary writer for a US YouTube history channel.
Topic: "{topic}"
Return JSON with keys:
  "title": a click-worthy but honest title under 70 characters,
  "description": 120-word YouTube description with 3 relevant hashtags,
  "tags": list of 10 search tags,
  "sections": list of exactly {SECTIONS} section titles that tell the story in a
              compelling order (strong hook first, strong conclusion last).
""", as_json=True))

    scenes, prev_end = [], ""
    for i, sec in enumerate(outline["sections"]):
        data = json.loads(gemini(f"""
Documentary topic: "{topic}"
Full outline: {outline["sections"]}
Write section {i+1} of {SECTIONS}: "{sec}".
Previous section ended with: "{prev_end}"
Rules:
- About 280 words of spoken narration, plain prose, no headings or stage directions.
- {"Open with a gripping hook in the first two sentences." if i == 0 else "Continue smoothly from the previous section."}
- Only state facts you are confident are true. Do NOT invent quotes, numbers or dates.
- Return JSON: {{"narration": "...", "keywords": ["3 specific search phrases for Wikimedia Commons historical photos, paintings or maps, using names of people, places or events"]}}
""", as_json=True))
        scenes.append(data)
        prev_end = data["narration"][-150:]
    return outline, scenes


def load_or_make_script(topic):
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:60]
    f = CACHE / f"{slug}.json"
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        print("Using saved script (no Gemini needed)", flush=True)
        return d["outline"], d["scenes"]
    outline, scenes = make_script(topic)
    CACHE.mkdir(exist_ok=True)
    f.write_text(json.dumps({"outline": outline, "scenes": scenes}), encoding="utf-8")
    print("Script saved", flush=True)
    return outline, scenes


def clean_text(t):
    t = re.sub(r"[*#`]+", "", str(t))
    return re.sub(r"\s+", " ", t).strip()


def tts(text, path):
    text = clean_text(text)
    if len(text) < 20:
        raise RuntimeError("Narration text is empty or too short")
    last = None
    for attempt in range(6):
        try:
            asyncio.run(edge_tts.Communicate(text, VOICE).save(str(path)))
            if Path(path).exists() and Path(path).stat().st_size > 1000:
                return
            last = "audio file too small"
        except Exception as e:
            last = e
        print(f"voice attempt {attempt+1} failed: {type(last)._name_}", flush=True)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Voice failed after 6 tries: {last}")


UA = {"User-Agent": "yt-auto-history-bot/1.0 (personal educational project)"}
FREE_LICENSES = ("public domain", "cc0", "pd", "no restrictions")


def commons_search(query, limit=30):
    r = requests.get(
        "https://commons.wikimedia.org/w/api.php", headers=UA, timeout=30,
        params={"action": "query", "format": "json", "generator": "search",
                "gsrnamespace": 6, "gsrsearch": query, "gsrlimit": limit,
                "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata",
                "iiurlwidth": 1920})
    pages = r.json().get("query", {}).get("pages", {}).values()
    out = []
    for pg in pages:
        info = (pg.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if info.get("width", 0) < 1000:
            continue
        lic = info.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "").lower()
        if not any(k in lic for k in FREE_LICENSES):
            continue
        out.append(info.get("thumburl") or info.get("url"))
    return out


def fetch_images(keywords, n, dest):
    urls = []
    for kw in keywords + ["historical map", "historical illustration"]:
        try:
            urls += commons_search(kw)
        except Exception:
            pass
        if len(set(urls)) >= n:
            break
        time.sleep(1)
    urls = list(dict.fromkeys(urls))
    random.shuffle(urls)
    if not urls:
        raise RuntimeError("No images found")
    paths, i = [], 0
    while len(paths) < n and i < n + len(urls) * 2:
        u = urls[i % len(urls)]
        i += 1
        try:
            resp = requests.get(u, headers=UA, timeout=60)
            resp.raise_for_status()
        except Exception:
            continue
        pth = dest / f"img{len(paths)}.jpg"
        pth.write_bytes(resp.content)
        paths.append(pth)
        time.sleep(0.5)
    if not paths:
        raise RuntimeError("Image download failed")
    while len(paths) < n:
        paths.append(paths[len(paths) % len(paths)])
    return paths


def build_scene(i, scene, d):
    print(f"Scene {i+1}/{SECTIONS}: voice", flush=True)
    audio = d / f"a{i}.mp3"
    tts(scene["narration"], audio)
    dur = duration(audio)
    n = max(1, math.ceil(dur / 8))
    each = dur / n + 0.05
    print(f"Scene {i+1}/{SECTIONS}: images", flush=True)
    imgs = fetch_images(scene["keywords"], n, d)

    clips = []
    for j, img in enumerate(imgs):
        clip = d / f"s{i}_c{j}.mp4"
        frames = int(each * 24)
        vf = ("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
              f"zoompan=z='min(zoom+0.0006,1.15)':d={frames}:s=1280x720:fps=24")
        run(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-vf", vf,
             "-t", f"{each:.2f}", "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", str(clip)])
        clips.append(clip)

    lst = d / f"s{i}.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    silent = d / f"s{i}_v.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent)])
    scene_mp4 = d / f"scene{i}.mp4"
    run(["ffmpeg", "-y", "-i", str(silent), "-i", str(audio), "-c:v", "copy",
         "-c:a", "aac", "-shortest", str(scene_mp4)])
    return scene_mp4


def render(scenes, d, final):
    parts = [build_scene(i, s, d) for i, s in enumerate(scenes)]
    lst = d / "all.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(final)])


def upload(path, outline, publish_at):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials.from_authorized_user_file(
        "token.json", ["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": outline["title"][:100], "description": outline["description"],
                    "tags": outline["tags"][:15], "categoryId": "27"},
        "status": {"privacyStatus": "private",
                   "publishAt": publish_at, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def publish_time(k):
    base = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (base + timedelta(hours=SLOT_HOURS_UTC[k % len(SLOT_HOURS_UTC)])).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    print(f"Gemini keys loaded: {len(KEYS)}", flush=True)
    OUT.mkdir(exist_ok=True)
    done = json.loads(DONE.read_text()) if DONE.exists() else []
    with open("topics.csv", newline="", encoding="utf-8") as f:
        topics = [r["topic"] for r in csv.DictReader(f) if r["topic"] not in done]

    made, failed = 0, 0
    for k, topic in enumerate(topics[:args.count]):
        print(f"=== {topic}", flush=True)
        try:
            d = WORK / f"v{len(done)+1}"
            d.mkdir(parents=True, exist_ok=True)
            outline, scenes = load_or_make_script(topic)
            final = OUT / f"{len(done)+1:03d}.mp4"
            render(scenes, d, final)
            if not args.no_upload:
                vid = upload(final, outline, publish_time(k))
                print("uploaded:", vid)
            done.append(topic)
            DONE.write_text(json.dumps(done, indent=2))
            subprocess.run(["rm", "-rf", str(d)])
            made += 1
        except Exception:
            traceback.print_exc()
            failed += 1

    print(f"RESULT: {made} video(s) made, {failed} failed", flush=True)
    if made == 0 and failed > 0:
        sys.exit(1)


main()
