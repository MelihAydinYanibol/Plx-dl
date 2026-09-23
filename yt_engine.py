import os
from urllib.parse import parse_qs, urlparse

import yt_dlp

def parse_youtube_url(url):
    """Returns (video_id, playlist_id) found in a YouTube link; either can be None."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    path_parts = [part for part in parsed.path.split("/") if part]

    video_id = None
    if parsed.netloc.lower().endswith("youtu.be") and path_parts:
        video_id = path_parts[0]
    elif query.get("v"):
        video_id = query["v"][0]
    elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live", "v"):
        video_id = path_parts[1]

    playlist_id = query.get("list", [None])[0]
    # "RD..." lists are YouTube's auto-generated radio mixes, attached to lots of normal song links.
    if video_id and playlist_id and playlist_id.startswith("RD"):
        playlist_id = None
    return video_id, playlist_id

def get_playlist_items(playlist_id):
    """Returns (playlist title, list of (video URL, video title)) without downloading anything."""
    print("📃 Reading the YouTube playlist...")
    ydl_opts = {'extract_flat': 'in_playlist', 'quiet': True, 'no_warnings': True, 'js_runtimes': {'deno': {}, 'node': {}}}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/playlist?list={playlist_id}", download=False)
    items = [
        (f"https://www.youtube.com/watch?v={entry['id']}", entry.get('title') or entry['id'])
        for entry in info.get('entries') or []
        if entry and entry.get('id')
    ]
    if not items:
        raise RuntimeError("Playlist is empty or private")
    return info.get('title') or playlist_id, items

def get_playlist_videos(playlist_id):
    """Returns (playlist title, list of video URLs) without downloading anything."""
    title, items = get_playlist_items(playlist_id)
    return title, [video_url for video_url, _ in items]

def download_audio(url, download_folder="cache"):
    """Downloads a video's audio as MP3. Returns (title, mp3 path, hints).

    hints holds what YouTube knows about the song: 'artist' and 'track' (only set for videos YouTube
    recognizes as music) and 'channel'.
    """
    if download_folder == "cache":
        download_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

    if not os.path.exists(download_folder):
        os.makedirs(download_folder)
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '256'}],
        'outtmpl': f'{download_folder}/%(title)s.%(ext)s',
        # YouTube needs a JavaScript runtime to unlock all formats; use Deno or Node, whichever is installed.
        'js_runtimes': {'deno': {}, 'node': {}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        artists = info.get('artists') or ([info['artist']] if info.get('artist') else [])
        hints = {
            'artist': ", ".join(artists),
            'track': info.get('track') or "",
            'channel': info.get('channel') or info.get('uploader') or "",
        }
        return info.get('title', 'Unknown'), ydl.prepare_filename(info).rsplit(".", 1)[0] + ".mp3", hints
