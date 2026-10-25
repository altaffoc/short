
import os
import json
import re
import subprocess
import tempfile
from pathlib import Path

import feedparser
import requests
import edge_tts

# ==================================================
# F-TECH SHORTS — KONFIGURASI
# ==================================================

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

HISTORY_FILE = Path("history.json")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

RSS_FEEDS = [
    "https://blog.google/technology/ai/rss/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
]

VOICE = "id-ID-ArdiNeural"
MODEL = "gemini-2.5-flash"


def check_configuration():
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY belum diatur di GitHub Secrets."
        )

    if not PEXELS_API_KEY:
        raise RuntimeError(
            "PEXELS_API_KEY belum diatur di GitHub Secrets."
        )


def load_history():
    if not HISTORY_FILE.exists():
        return []

    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history):
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_news():
    history = load_history()
    candidates = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)

            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = re.sub(
                    r"<[^>]+>",
                    " ",
                    entry.get("summary", ""),
                ).strip()

                if title and link and link not in history:
                    candidates.append({
                        "title": title,
                        "link": link,
                        "summary": summary[:1800],
                    })
        except Exception as exc:
            print(f"Gagal membaca RSS: {exc}")

    if not candidates:
        raise RuntimeError(
            "Tidak ditemukan berita baru. Coba lagi pada jadwal berikutnya."
        )

    return candidates[0]


def generate_script(news):
    prompt = f"""
Kamu adalah penulis naskah YouTube Shorts untuk channel
teknologi Indonesia bernama F-Tech.

Gunakan berita berikut sebagai bahan:
Judul: {news['title']}
Ringkasan sumber: {news['summary']}
URL sumber: {news['link']}

Buat naskah bahasa Indonesia yang menarik dan mudah dipahami.
Jangan mengarang fakta yang tidak didukung sumber.
Jika informasi tidak cukup, gunakan bahasa yang berhati-hati.
Hindari klaim sensasional dan jangan menyalin artikel mentah.

Narasi harus cocok untuk video sekitar 30 sampai 60 detik.
Targetkan sekitar 75 sampai 110 kata.
Buat judul Shorts singkat dan relevan.
Buat visual_query berbahasa Inggris untuk mencari video stok
yang relevan di Pexels.
Hashtags maksimal 5 buah.

Balas HANYA JSON valid dengan struktur:
{{
  "title": "judul video",
  "narration": "naskah narasi",
  "visual_query": "kata pencarian video stok",
  "hashtags": "#Teknologi #AI"
}}
"""

    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent",
        params={"key": GEMINI_API_KEY},
        json={
            "contents": [
                {"parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.4,
            },
        },
        timeout=60,
    )

    response.raise_for_status()
    data = response.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    script = json.loads(text)

    narration = script["narration"].strip()
    words = narration.split()

    if not 65 <= len(words) <= 135:
        raise RuntimeError(
            f"Panjang narasi {len(words)} kata; perlu diperbaiki."
        )

    return script


def download_pexels_video(query, destination):
    response = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={
            "query": query,
            "orientation": "portrait",
            "per_page": 10,
        },
        timeout=45,
    )
    response.raise_for_status()

    videos = response.json().get("videos", [])
    if not videos:
        raise RuntimeError(
            "Video stok tidak ditemukan. Coba kata pencarian lain."
        )

    for video in videos:
        files = sorted(
            video.get("video_files", []),
            key=lambda item: item.get("width", 0),
            reverse=True,
        )

        for item in files:
            link = item.get("link")
            if not link:
                continue

            try:
                result = requests.get(link, timeout=90)
                result.raise_for_status()

                if len(result.content) < 100_000:
                    continue

                destination.write_bytes(result.content)
                return
            except requests.RequestException:
                continue

    raise RuntimeError("Tidak berhasil mengunduh video stok.")


async def create_voice(text, destination):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(str(destination))


def render_video(video_path, audio_path, destination):
    command = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "24",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        str(destination),
    ]

    subprocess.run(command, check=True)

    duration = float(subprocess.check_output([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(destination),
    ], text=True).strip())

    if not 30 <= duration <= 60:
        raise RuntimeError(
            f"Durasi video {duration:.1f} detik, di luar target 30–60 detik."
        )


def main():
    check_configuration()
    news = get_news()

    print("Berita terpilih:", news["title"])
    print("Sumber:", news["link"])

    script = generate_script(news)
    print("Judul video:", script["title"])
    print("Jumlah kata:", len(script["narration"].split()))

    import asyncio

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        stock_path = temp_dir / "stock.mp4"
        audio_path = temp_dir / "voice.mp3"

        safe_title = re.sub(
            r"[^a-zA-Z0-9_-]+", "_", script["title"]
        )[:70]
        output_path = OUTPUT_DIR / f"{safe_title}.mp4"

        download_pexels_video(
            script["visual_query"], stock_path
        )

        asyncio.run(create_voice(
            script["narration"], audio_path
        ))

        render_video(stock_path, audio_path, output_path)

        metadata = {
            "title": script["title"],
            "description": (
                f"{script['narration']}\n\n"
                f"Sumber berita: {news['link']}\n\n"
                f"{script.get('hashtags', '#Teknologi #FTech')}"
            ),
            "source_url": news["link"],
            "video_file": str(output_path),
        }

        metadata_path = output_path.with_suffix(".json")
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print("Video selesai dibuat:", output_path)
        print("Metadata:", metadata_path)

        if DRY_RUN:
            print("DRY RUN: video belum diunggah ke YouTube.")
            return

        raise RuntimeError(
            "Upload YouTube belum diaktifkan. "
            "Jangan jalankan mode upload sebelum OAuth selesai."
        )


if __name__ == "__main__":
    main()
    
