
import os
import re
import json
import hashlib
import subprocess
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import feedparser
import requests
import edge_tts

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
HISTORY_FILE = ROOT / "history.json"

OUTPUT_DIR.mkdir(exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TTS_VOICE = os.getenv("TTS_VOICE", "id-ID-ArdiNeural")

RSS_FEEDS = [
    "https://blog.google/technology/ai/rss/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
]

def load_history():
    if not HISTORY_FILE.exists():
        return {"used_articles": [], "used_scripts": []}

    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"used_articles": data, "used_scripts": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"used_articles": [], "used_scripts": []}

def save_history(history):
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def fingerprint(text):
    normalized = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def get_news(history):
    used = set(history.get("used_articles", []))
    candidates = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)

            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = re.sub(
                    r"<[^>]+>", " ", entry.get("summary", "")
                ).strip()

                if not title or not link.startswith("https://"):
                    continue

                key = fingerprint(link)

                if key not in used:
                    candidates.append({
                        "title": title,
                        "link": link,
                        "summary": summary[:2500],
                        "key": key,
                    })
        except Exception as exc:
            print(f"RSS tidak bisa dibaca: {exc}")

    if not candidates:
        raise RuntimeError(
            "Tidak ditemukan artikel baru yang belum digunakan."
        )

    # Pilih artikel terbaru berdasarkan urutan feed.
    return candidates[0]

def generate_script(news, history):
    prompt = f"""
Kamu adalah editor teknologi untuk channel YouTube Shorts F-Tech.
Buat naskah orisinal dalam Bahasa Indonesia, sekitar 75-110 kata,
untuk video 30-60 detik.

Gunakan informasi yang didukung oleh bahan sumber di bawah.
Jangan mengarang angka, tanggal, hasil riset, atau kutipan.
Jika bahan tidak cukup untuk memastikan suatu klaim, jangan
menyatakannya sebagai fakta. Bedakan pengumuman perusahaan dari
hasil yang telah diverifikasi secara independen.

Judul sumber: {news['title']}
Ringkasan sumber: {news['summary']}
URL sumber: {news['link']}

Buat visual_query bahasa Inggris yang secara spesifik menggambarkan
objek atau teknologi dalam cerita. Hindari pencarian umum seperti
"technology" jika topiknya sebenarnya perangkat tertentu.

Buat maksimal 5 hashtag yang relevan.
Balas hanya JSON valid:
{{
  "title": "judul Shorts",
  "narration": "naskah narasi",
  "visual_query": "specific visual search terms in English",
  "hashtags": ["#Teknologi", "#FTech"]
}}
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent"
    )

    response = requests.post(
        url,
        params={"key": GEMINI_API_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
            },
        },
        timeout=90,
    )
    response.raise_for_status()

    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    script = json.loads(text)

    for field in ("title", "narration", "visual_query", "hashtags"):
        if field not in script:
            raise RuntimeError(f"Kolom AI tidak tersedia: {field}")

    words = script["narration"].split()
    if not 65 <= len(words) <= 135:
        raise RuntimeError(
            f"Naskah berisi {len(words)} kata; target 65-135 kata."
        )

    script_key = fingerprint(script["narration"])
    if script_key in set(history.get("used_scripts", [])):
        raise RuntimeError("Narasi terdeteksi duplikat.")

    return script, script_key

def find_pixabay_video(query, destination):
    if not PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY belum diatur.")

    response = requests.get(
        "https://pixabay.com/api/videos/",
        params={
            "key": PIXABAY_API_KEY,
            "q": query,
            "video_type": "film",
            "orientation": "vertical",
            "safesearch": "true",
            "per_page": 20,
        },
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()

    hits = data.get("hits", [])
    if not hits:
        # Coba lagi tanpa batasan orientasi jika tidak ada hasil vertikal.
        response = requests.get(
            "https://pixabay.com/api/videos/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "safesearch": "true",
                "per_page": 20,
            },
            timeout=45,
        )
        response.raise_for_status()
        hits = response.json().get("hits", [])

    for hit in hits:
        videos = hit.get("videos", {})

        # Pilih ukuran yang cukup baik, dengan fallback.
        for size in ("large", "medium", "small", "tiny"):
            item = videos.get(size, {})
            video_url = item.get("url")

            if not video_url:
                continue

            try:
                with requests.get(
                    video_url, stream=True, timeout=90
                ) as download:
                    download.raise_for_status()

                    total = 0
                    with destination.open("wb") as output:
                        for chunk in download.iter_content(1024 * 1024):
                            if chunk:
                                output.write(chunk)
                                total += len(chunk)

                if total >= 50_000:
                    return {
                        "video_id": hit.get("id"),
                        "page_url": hit.get("pageURL", ""),
                        "tags": hit.get("tags", ""),
                    }

                destination.unlink(missing_ok=True)
            except requests.RequestException:
                destination.unlink(missing_ok=True)
                continue

    raise RuntimeError(
        "Video Pixabay tidak berhasil ditemukan atau diunduh."
    )

async def create_voice(text, destination):
    speech = edge_tts.Communicate(text, TTS_VOICE)
    await speech.save(str(destination))

def get_audio_duration(audio_path):
    result = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ], text=True)

    return float(result.strip())

def render_video(video_path, audio_path, destination):
    duration = get_audio_duration(audio_path)

    if not 30 <= duration <= 60:
        raise RuntimeError(
            f"Durasi narasi {duration:.1f} detik, di luar target 30-60."
        )

    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(video_path),
        "-i", str(audio_path),
        "-t", str(duration),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(destination),
    ], check=True)

    return duration

def main():
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY belum diatur.")

    if not PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY belum diatur.")

    history = load_history()
    news = get_news(history)

    print("Artikel:", news["title"])
    print("Sumber berita:", news["link"])

    script, script_key = generate_script(news, history)

    print("Judul video:", script["title"])
    print("Kata narasi:", len(script["narration"].split()))

    safe_title = re.sub(
        r"[^a-zA-Z0-9_-]+", "_", script["title"]
    ).strip("_")[:65] or "ftech_short"

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        stock_path = temp_dir / "stock.mp4"
        audio_path = temp_dir / "voice.mp3"
        final_path = OUTPUT_DIR / f"{safe_title}.mp4"
        metadata_path = OUTPUT_DIR / f"{safe_title}.json"

        footage = find_pixabay_video(
            script["visual_query"], stock_path
        )

        asyncio.run(create_voice(
            script["narration"], audio_path
        ))

        duration = render_video(
            stock_path, audio_path, final_path
        )

        metadata = {
            "title": script["title"],
            "narration": script["narration"],
            "hashtags": script["hashtags"],
            "description": (
                script["narration"] + "\n\n"
                + "Sumber berita: " + news["link"] + "\n"
                + "Video stok: " + footage["page_url"] + "\n\n"
                + " ".join(script["hashtags"])
            ),
            "source_url": news["link"],
            "footage_page": footage["page_url"],
            "duration_seconds": round(duration, 2),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "video_file": str(final_path),
        }

        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print("Video selesai:", final_path)
        print("Metadata selesai:", metadata_path)
        print("Durasi:", round(duration, 2), "detik")
        print("Halaman footage:", footage["page_url"])

    if DRY_RUN:
        print("MODE UJI: belum mengunggah video ke YouTube.")
        return

    raise RuntimeError(
        "Upload belum diaktifkan. OAuth YouTube perlu disiapkan dahulu."
    )

if __name__ == "__main__":
    main()
    
