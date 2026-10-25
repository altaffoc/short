
import os
import re
import json
import time
import random
import hashlib
import logging
import subprocess
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
import edge_tts

# ============================================================
# F-TECH SHORTS — CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
HISTORY_FILE = ROOT / "history.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-3.8-flash"
).strip()

TTS_VOICE = os.getenv("TTS_VOICE", "id-ID-ArdiNeural")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

MAX_GEMINI_ATTEMPTS = 5
MAX_HISTORY_ITEMS = 500
MIN_DURATION = 30
MAX_DURATION = 60

RSS_FEEDS = [
    "https://blog.google/technology/ai/rss/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ftech")


# ============================================================
# UTILITIES
# ============================================================

def fingerprint(value):
    normalized = re.sub(
        r"\W+", " ", value.casefold()
    ).strip()

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def load_history():
    if not HISTORY_FILE.exists():
        return {
            "used_articles": [],
            "used_scripts": [],
            "used_titles": [],
        }

    try:
        data = json.loads(
            HISTORY_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(data, dict):
            raise ValueError("Format history bukan object.")

        for key in (
            "used_articles",
            "used_scripts",
            "used_titles",
        ):
            if not isinstance(data.get(key), list):
                data[key] = []

        return data

    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            "history.json tidak valid. Periksa file sebelum melanjutkan."
        ) from exc


def save_history(history):
    temp_path = HISTORY_FILE.with_suffix(".tmp")

    temp_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temp_path.replace(HISTORY_FILE)


def remember(history, field, value):
    items = history.setdefault(field, [])

    if value not in items:
        items.append(value)

    history[field] = items[-MAX_HISTORY_ITEMS:]


def safe_filename(title):
    result = re.sub(r"[^\w-]+", "_", title, flags=re.UNICODE)
    result = result.strip("_")[:65]

    return result or "ftech_short"


# ============================================================
# NEWS RESEARCH
# ============================================================

def get_news(history):
    used_articles = set(history["used_articles"])
    candidates = []

    for feed_url in RSS_FEEDS:
        try:
            log.info("Membaca RSS: %s", feed_url)

            response = requests.get(
                feed_url,
                headers={"User-Agent": "F-Tech-Shorts/1.0"},
                timeout=30,
            )
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            for entry in feed.entries[:30]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()

                summary = re.sub(
                    r"<[^>]+>",
                    " ",
                    entry.get("summary", ""),
                )
                summary = re.sub(r"\s+", " ", summary).strip()

                if not title or not link.startswith("https://"):
                    continue

                article_key = fingerprint(link)

                if article_key in used_articles:
                    continue

                candidates.append({
                    "title": title,
                    "link": link,
                    "summary": summary[:3000],
                    "key": article_key,
                })

        except requests.RequestException as exc:
            log.warning("RSS gagal dibaca: %s", exc)

    if not candidates:
        raise RuntimeError(
            "Tidak ada artikel baru yang tersedia. "
            "Periksa RSS atau tunggu artikel baru."
        )

    # Ambil kandidat pertama yang belum pernah dipakai.
    return candidates[0]


# ============================================================
# GEMINI API WITH RETRY
# ============================================================

def gemini_request(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY belum diatur.")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1200,
        },
    }

    last_error = None

    for attempt in range(MAX_GEMINI_ATTEMPTS):
        try:
            response = requests.post(
                url,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=(15, 120),
            )

            if response.status_code in (
                408, 429, 500, 502, 503, 504
            ):
                last_error = (
                    f"Gemini HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

                if attempt < MAX_GEMINI_ATTEMPTS - 1:
                    retry_after = response.headers.get("Retry-After")
                    delay = min(60, 2 ** (attempt + 1))
                    delay += random.uniform(0, 1.5)

                    if retry_after:
                        try:
                            delay = max(
                                delay, float(retry_after)
                            )
                        except ValueError:
                            try:
                                retry_date = parsedate_to_datetime(
                                    retry_after
                                )
                                delay = max(
                                    delay,
                                    (
                                        retry_date
                                        - datetime.now(
                                            retry_date.tzinfo
                                        )
                                    ).total_seconds(),
                                )
                            except (TypeError, ValueError):
                                pass

                    delay = min(90, max(1, delay))

                    log.warning(
                        "%s. Percobaan %s/%s; tunggu %.1f detik.",
                        last_error,
                        attempt + 1,
                        MAX_GEMINI_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue

            response.raise_for_status()

            data = response.json()
            candidates = data.get("candidates", [])

            if not candidates:
                feedback = data.get("promptFeedback", {})
                raise RuntimeError(
                    "Gemini tidak mengembalikan kandidat. "
                    f"Feedback: {feedback}"
                )

            parts = candidates[0].get("content", {}).get(
                "parts", []
            )

            result = "".join(
                part.get("text", "")
                for part in parts
                if "text" in part
            ).strip()

            if not result:
                raise RuntimeError(
                    "Respons Gemini kosong atau tidak berisi teks."
                )

            return result

        except (
            requests.Timeout,
            requests.ConnectionError,
        ) as exc:
            last_error = str(exc)

            if attempt >= MAX_GEMINI_ATTEMPTS - 1:
                break

            delay = min(60, 2 ** (attempt + 1))
            delay += random.uniform(0, 1.5)

            log.warning(
                "Gangguan jaringan Gemini. Percobaan %s/%s; "
                "menunggu %.1f detik.",
                attempt + 1,
                MAX_GEMINI_ATTEMPTS,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError(
        "Gemini gagal setelah beberapa percobaan. "
        f"Detail terakhir: {last_error}"
    )


def generate_script(news, history):
    prompt = f"""
Kamu adalah editor teknologi untuk channel YouTube Shorts F-Tech.

TUGAS:
Buat satu naskah orisinal berbahasa Indonesia untuk video
teknologi berdurasi sekitar 30-60 detik.

BAHAN SUMBER:
Judul: {news['title']}
Ringkasan: {news['summary']}
URL: {news['link']}

ATURAN FAKTA:
- Gunakan hanya fakta yang didukung bahan sumber.
- Jangan mengarang angka, tanggal, hasil riset, atau kutipan.
- Bedakan pengumuman perusahaan dengan fakta independen.
- Jika ringkasan tidak cukup, gunakan bahasa berhati-hati.
- Jangan menyatakan telah memverifikasi halaman penuh jika
  hanya ringkasan RSS yang tersedia.
- Jangan menyalin kalimat artikel secara panjang.
- Hindari klaim sensasional dan judul menyesatkan.

ATURAN VIDEO:
- Narasi 75-110 kata.
- Bahasa alami untuk penonton Indonesia.
- Pembuka menarik, isi jelas, penutup singkat.
- Judul menarik tetapi tidak menyesatkan.
- visual_query harus spesifik dan relevan dengan berita.
- Hashtag maksimal 5.
- Hindari mengulang narasi atau judul yang pernah digunakan.

Balas hanya JSON valid dengan struktur:
{{
  "title": "Judul Shorts",
  "narration": "Narasi lengkap",
  "visual_query": "specific English stock footage search",
  "hashtags": ["#Teknologi", "#FTech"]
}}
"""

    raw = gemini_request(prompt)

    try:
        script = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gemini tidak mengembalikan JSON valid: {raw[:300]}"
        ) from exc

    required = (
        "title",
        "narration",
        "visual_query",
        "hashtags",
    )

    for field in required:
        if field not in script:
            raise RuntimeError(
                f"Kolom {field} tidak ditemukan pada respons Gemini."
            )

    script["title"] = str(script["title"]).strip()
    script["narration"] = str(script["narration"]).strip()
    script["visual_query"] = str(script["visual_query"]).strip()

    if isinstance(script["hashtags"], str):
        script["hashtags"] = script["hashtags"].split()

    script["hashtags"] = [
        str(tag).strip()
        for tag in script["hashtags"]
        if str(tag).strip()
    ][:5]

    words = script["narration"].split()

    if not 65 <= len(words) <= 135:
        raise RuntimeError(
            f"Narasi berisi {len(words)} kata; target 65-135 kata."
        )

    if not script["title"] or not script["visual_query"]:
        raise RuntimeError(
            "Judul atau visual_query dari Gemini kosong."
        )

    script_key = fingerprint(script["narration"])
    title_key = fingerprint(script["title"])

    if script_key in set(history["used_scripts"]):
        raise RuntimeError(
            "Narasi duplikat terdeteksi. Proses dihentikan."
        )

    if title_key in set(history["used_titles"]):
        raise RuntimeError(
            "Judul duplikat terdeteksi. Proses dihentikan."
        )

    return script, script_key, title_key


# ============================================================
# PIXABAY STOCK VIDEO
# ============================================================

def download_pixabay_video(query, destination):
    if not PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY belum diatur.")

    url = "https://pixabay.com/api/videos/"

    search_variants = [
        query,
        "technology " + query,
    ]

    seen_ids = set()

    for search_query in search_variants:
        response = requests.get(
            url,
            params={
                "key": PIXABAY_API_KEY,
                "q": search_query,
                "safesearch": "true",
                "per_page": 20,
            },
            timeout=45,
        )
        response.raise_for_status()

        for hit in response.json().get("hits", []):
            video_id = hit.get("id")

            if video_id in seen_ids:
                continue

            seen_ids.add(video_id)
            video_files = hit.get("videos", {})

            for size in ("large", "medium", "small", "tiny"):
                video_info = video_files.get(size, {})
                video_url = video_info.get("url")

                if not video_url:
                    continue

                try:
                    total = 0

                    with requests.get(
                        video_url,
                        stream=True,
                        timeout=(15, 90),
                    ) as download:
                        download.raise_for_status()

                        with destination.open("wb") as output:
                            for chunk in download.iter_content(
                                chunk_size=1024 * 1024
                            ):
                                if chunk:
                                    output.write(chunk)
                                    total += len(chunk)

                    if total >= 50_000:
                        log.info(
                            "Footage Pixabay ID=%s, tags=%s",
                            video_id,
                            hit.get("tags", ""),
                        )

                        return {
                            "video_id": video_id,
                            "page_url": hit.get("pageURL", ""),
                            "tags": hit.get("tags", ""),
                        }

                    destination.unlink(missing_ok=True)

                except requests.RequestException as exc:
                    log.warning(
                        "Gagal mengunduh footage: %s", exc
                    )
                    destination.unlink(missing_ok=True)

    raise RuntimeError(
        "Pixabay tidak menemukan video yang berhasil diunduh. "
        "Periksa query, API key, atau coba lagi."
    )


# ============================================================
# INDONESIAN TTS
# ============================================================

async def create_voice(text, destination):
    communicator = edge_tts.Communicate(
        text,
        TTS_VOICE,
    )
    await communicator.save(str(destination))


def audio_duration(audio_path):
    result = subprocess.check_output(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        text=True,
    )

    return float(result.strip())


# ============================================================
# VIDEO RENDERING
# ============================================================

def render_video(video_path, audio_path, destination):
    duration = audio_duration(audio_path)

    if not MIN_DURATION <= duration <= MAX_DURATION:
        raise RuntimeError(
            f"Durasi suara {duration:.1f} detik. "
            "Target video adalah 30-60 detik. "
            "Naskah perlu disesuaikan sebelum rendering."
        )

    command = [
        "ffmpeg",
        "-y",
        "-stream_loop", "-1",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-t", str(duration),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,fps=30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "24",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        str(destination),
    ]

    subprocess.run(command, check=True)

    if not destination.exists() or destination.stat().st_size < 50_000:
        raise RuntimeError(
            "FFmpeg tidak menghasilkan video yang valid."
        )

    check_duration = subprocess.check_output(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(destination),
        ],
        text=True,
    )

    actual_duration = float(check_duration.strip())

    if not MIN_DURATION <= actual_duration <= MAX_DURATION:
        raise RuntimeError(
            f"Durasi video hasil render {actual_duration:.1f} detik."
        )

    return actual_duration


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY belum tersedia.")

    if not PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY belum tersedia.")

    history = load_history()
    news = get_news(history)

    log.info("Artikel: %s", news["title"])
    log.info("Sumber: %s", news["link"])

    script, script_key, title_key = generate_script(
        news, history
    )

    log.info("Judul video: %s", script["title"])
    log.info(
        "Jumlah kata narasi: %s",
        len(script["narration"].split()),
    )

    base_name = safe_filename(script["title"])
    suffix = news["key"][:8]
    filename = f"{base_name}_{suffix}"

    final_path = OUTPUT_DIR / f"{filename}.mp4"
    metadata_path = OUTPUT_DIR / f"{filename}.json"

    if final_path.exists() or metadata_path.exists():
        raise RuntimeError(
            "Nama file sudah ada. Proses dihentikan agar "
            "hasil sebelumnya tidak tertimpa."
        )

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        stock_path = temp_dir / "stock.mp4"
        voice_path = temp_dir / "voice.mp3"

        footage = download_pixabay_video(
            script["visual_query"],
            stock_path,
        )

        asyncio.run(
            create_voice(
                script["narration"],
                voice_path,
            )
        )

        duration = render_video(
            stock_path,
            voice_path,
            final_path,
        )

    metadata = {
        "title": script["title"],
        "narration": script["narration"],
        "hashtags": script["hashtags"],
        "description": (
            script["narration"]
            + "\n\nSumber berita: "
            + news["link"]
            + "\nSumber footage: "
            + footage["page_url"]
            + "\n\n"
            + " ".join(script["hashtags"])
        ),
        "source_url": news["link"],
        "footage_page": footage["page_url"],
        "duration_seconds": round(duration, 2),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "video_file": str(final_path),
        "upload_status": "not_uploaded",
    }

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Riwayat baru dicatat setelah video berhasil dibuat.
    remember(history, "used_articles", news["key"])
    remember(history, "used_scripts", script_key)
    remember(history, "used_titles", title_key)
    save_history(history)

    log.info("Video selesa
