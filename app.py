
import os
import re
import json
import hashlib
import subprocess
import asyncio
from pathlib import Path

import requests
import feedparser

ROOT = Path(__file__).parent
OUT = ROOT / "output"
HISTORY_FILE = ROOT / "history.json"

OUT.mkdir(exist_ok=True)

RSS_FEEDS = [
    "https://blog.google/technology/ai/rss/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
]

def load_history():
    if not HISTORY_FILE.exists():
        return {"used": []}

    return json.loads(
        HISTORY_FILE.read_text(encoding="utf-8")
    )

def save_history(history):
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def fingerprint(text):
    normalized = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()

def get_latest_article(history):
    used = set(history.get("used", []))
    articles = []

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries[:10]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            summary = re.sub(
                r"<[^>]+>", " ",
                getattr(entry, "summary", "")
            ).strip()

            if not title or not link.startswith("https://"):
                continue

            key = fingerprint(title)

            if key not in used:
                articles.append({
                    "title": title,
                    "url": link,
                    "summary": summary[:1800],
                    "key": key
                })

    if not articles:
        raise RuntimeError(
            "Tidak ada artikel baru yang lolos pemeriksaan duplikasi."
        )

    return articles[0]

def make_script(article):
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    prompt = f"""
Buat naskah YouTube Shorts teknologi dalam Bahasa Indonesia.

Gunakan hanya fakta yang didukung judul dan ringkasan berikut.
Jangan mengarang angka atau klaim. Buat narasi orisinal
sekitar 75-110 kata untuk durasi 30-60 detik.

Judul sumber: {article['title']}
Ringkasan: {article['summary']}
URL sumber: {article['url']}

Balas JSON dengan format:
{{
  "title": "judul video",
  "narration": "naskah narator",
  "visual_query": "kata kunci footage dalam bahasa Inggris",
  "hashtags": ["#Teknologi", "#FTech"]
}}
"""

    response = requests.post(
        url,
        params={"key": api_key},
        json={
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.6,
                "responseMimeType": "application/json"
            }
        },
        timeout=90
    )

    response.raise_for_status()

    result = response.json()
    text = result["candidates"][0]["content"]["parts"][0]["text"]
    script = json.loads(text)

    if not 65 <= len(script["narration"].split()) <= 135:
        raise RuntimeError("Panjang naskah tidak sesuai.")

    return script

def find_footage(query):
    api_key = os.environ["PEXELS_API_KEY"]

    response = requests.get(
        "https://api.pexels.com/v1/videos/search",
        headers={"Authorization": api_key},
        params={
            "query": query,
            "per_page": 8,
            "orientation": "portrait"
        },
        timeout=45
    )

    response.raise_for_status()

    for video in response.json().get("videos", []):
        files = sorted(
            video.get("video_files", []),
            key=lambda item: item.get("width", 0),
            reverse=True
        )

        for item in files:
            if (
                item.get("link")
                and item.get("width", 0) >= 720
                and item.get("height", 0) >= 1000
            ):
                return item["link"], video.get("url", "")

    raise RuntimeError("Footage vertikal yang sesuai tidak ditemukan.")

def download_file(url, destination):
    with requests.get(
        url, stream=True, timeout=90
    ) as response:
        response.raise_for_status()

        with open(destination, "wb") as file:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    file.write(chunk)

async def make_voice(text, destination):
    import edge_tts

    voice = os.getenv("TTS_VOICE", "id-ID-ArdiNeural")
    speech = edge_tts.Communicate(text, voice)
    await speech.save(str(destination))

def render_video(footage_url, narration):
    footage = OUT / "footage.mp4"
    voice = OUT / "voice.mp3"
    final = OUT / "ftech-short.mp4"

    download_file(footage_url, footage)

    asyncio.run(make_voice(narration, voice))

    duration = float(subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(voice)
    ]).decode().strip())

    if not 30 <= duration <= 60:
        raise RuntimeError(
            f"Durasi suara {duration:.1f} detik; target 30-60 detik."
        )

    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(footage),
        "-i", str(voice),
        "-t", str(duration),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(final)
    ], check=True)

    return final

def main():
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY belum disiapkan.")

    if not os.getenv("PEXELS_API_KEY"):
        raise RuntimeError("PEXELS_API_KEY belum disiapkan.")

    history = load_history()
    article = get_latest_article(history)

    print("Artikel:", article["title"])
    print("Sumber:", article["url"])

    script = make_script(article)

    footage_url, footage_page = find_footage(
        script["visual_query"]
    )

    video = render_video(
        footage_url,
        script["narration"]
    )

    print("Video selesai:", video)
    print("Judul:", script["title"])
    print("Hashtag:", " ".join(script["hashtags"]))
    print("Sumber footage:", footage_page)

    if os.getenv("DRY_RUN", "true").lower() != "false":
        print("MODE UJI: video belum diunggah.")
        return

    # Upload YouTube akan diaktifkan setelah OAuth disiapkan.
    raise RuntimeError(
        "Upload belum diaktifkan. Selesaikan konfigurasi OAuth terlebih dahulu."
    )

if __name__ == "__main__":
    main()
      
