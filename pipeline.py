"""
Free YouTube history-documentary pipeline.
Topic -> script (Gemini) -> voice (edge-tts) -> images (Pexels) -> video (FFmpeg) -> scheduled upload.

Run:  python pipeline.py --count 3            (make + upload 3 videos)
      python pipeline.py --count 1 --no-upload (test without uploading)
"""
import os, csv, json, math, time, random, asyncio, argparse, subprocess, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import edge_tts

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
VOICE = os.getenv("VOICE", "en-US-AndrewNeural")
SECTIONS = 12                       # 12 x ~280 words = ~22 min
SLOT_HOURS_UTC = [15, 20, 24]       # ~10am, 3pm, 7pm US Eastern (next-day schedule)
WORK, OUT, DONE = Path("work"), Path("out"), Path("done.json")


# ---------- helpers ----------
def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def gemini(prompt, as_json=False):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    last = ""
    for attempt in range(6):
        r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=180)
        if r.status_code == 200:
            time.sleep(4)  # stay under free-tier rate limits
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        last = r.text
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"Gemini failed: {last}")


# ---------- 1. script ----------
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


# ---------- 2. voice ----------
def tts(text, path):
    asyncio.run(edge_tts.Communicate(text, VOICE).save(str(path)))


# ---------- 3. visuals (Wikimedia Commons, no API key) ----------
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
            continue  # keep only public-domain / CC0 images
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
    while len(paths) < n:           # reuse images if fewer than needed
        paths.append(paths[len(paths) % len(paths)])
    return paths


# ---------- 4. render ----------
def build_scene(i, scene, d):
    audio = d / f"a{i}.mp3"
    tts(scene["narration"], audio)
    dur = duration(audio)
    n = max(1, math.ceil(dur / 8))
    each = dur / n + 0.05
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


# ---------- 5. upload ----------
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
        "status": {"privacyStatus": "private",  # required for scheduled publishing
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


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    done = json.loads(DONE.read_text()) if DONE.exists() else []
    with open("topics.csv", newline="", encoding="utf-8") as f:
        topics = [r["topic"] for r in csv.DictReader(f) if r["topic"] not in done]

    for k, topic in enumerate(topics[:args.count]):
        print(f"=== {topic}")
        try:
            d = WORK / f"v{len(done)+1}"
            d.mkdir(parents=True, exist_ok=True)
            outline, scenes = make_script(topic)
            final = OUT / f"{len(done)+1:03d}.mp4"
            render(scenes, d, final)
            if not args.no_upload:
                vid = upload(final, outline, publish_time(k))
                print("uploaded:", vid)
            done.append(topic)
            DONE.write_text(json.dumps(done, indent=2))
            subprocess.run(["rm", "-rf", str(d)])  # free disk space
        except Exception:
            traceback.print_exc()  # skip this topic, continue with the next


if __name__ == "__main__":
    main()
