import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dotenv
from rapidfuzz import fuzz
from spotdl import Spotdl
from spotdl.types.song import Song
from spotdl.utils.lrc import generate_lrc
from spotdl.utils.config import DEFAULT_CONFIG

_client = None

_SPOTIFY_LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(track|album|playlist|artist)[/:]([A-Za-z0-9]+)")
_CONTEXT = re.compile(r"spotify:(playlist|album):([A-Za-z0-9]+)")

def parse_spotify_url(url):
    """Returns (clean_url, context).

    context is (kind, url) of the playlist/album a track link was shared from, when the
    link carries one (?context=spotify:playlist:...), otherwise None.
    """
    match = _SPOTIFY_LINK.search(url)
    if not match:
        return url, None
    kind, item_id = match.groups()
    clean_url = f"https://open.spotify.com/{kind}/{item_id}"

    context = None
    context_match = _CONTEXT.fullmatch(parse_qs(urlparse(url).query).get("context", [""])[0])
    if kind == "track" and context_match:
        context_kind, context_id = context_match.groups()
        context = (context_kind, f"https://open.spotify.com/{context_kind}/{context_id}")
    return clean_url, context

def _get_client():
    """Spotdl can only be initialized once per process, so reuse a single client."""
    global _client
    if _client is None:
        dotenv.load_dotenv()
        _client = Spotdl(
            client_id=os.getenv("SPOTIFY_CLIENT_ID", DEFAULT_CONFIG["client_id"]),
            client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", DEFAULT_CONFIG["client_secret"]),
            downloader_settings={
                "format": "mp3",
                "bitrate": "256k",
                "overwrite": "skip",
            },
        )
    return _client

def download_spotify(url, download_folder="cache", lyrics=True):
    """Downloads a Spotify track/album/playlist URL.

    Returns a list of (album, song, artist, filepath) for every successfully downloaded track.
    spotdl already embeds full metadata from Spotify, so no AI/Picard pass is needed.
    With lyrics on, spotdl also saves synced lyrics next to each MP3 as a .lrc file.
    """
    if download_folder == "cache":
        download_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

    if not os.path.exists(download_folder):
        os.makedirs(download_folder)

    client = _get_client()
    # The client is shared, so apply the per-download settings every time.
    client.downloader.settings["output"] = os.path.join(download_folder, "{artists} - {title}.{output-ext}")
    client.downloader.settings["generate_lrc"] = lyrics
    songs = client.search([url])
    if not songs:
        raise RuntimeError("No songs found for this Spotify URL")

    results = []
    for song, path in client.download_songs(songs):
        if path is None:
            print(f"❌ Failed to download: {song.display_name}")
            continue
        results.append((song.album_name or song.name, song.name, song.artist, str(path)))
    return results

def download_lyrics(artist, song_name, filepath_mp3):
    """Finds the song on Spotify and saves its synced lyrics next to the MP3 as a .lrc file.

    Returns the .lrc path, or None when there's no confident Spotify match or no lyrics.
    """
    _get_client()  # Spotify search needs the client set up
    try:
        match = Song.from_search_term(f"{artist} - {song_name}")
    except Exception as error:
        print(f"⚠️ Couldn't find {artist} - {song_name} on Spotify, skipping lyrics: {error}")
        return None

    # Spotify returns its top result no matter how bad it is, so make sure it's actually the same song.
    title_score = fuzz.token_set_ratio(song_name.lower(), match.name.lower())
    artist_score = max(fuzz.token_set_ratio(artist.lower(), name.lower()) for name in match.artists)
    if title_score < 70 or artist_score < 70:
        print(f"⚠️ Spotify's best match was {match.display_name}, which doesn't look right. Skipping lyrics.")
        return None

    print(f"🎤 Spotify match: {match.display_name}. Looking for lyrics...")
    generate_lrc(match, Path(filepath_mp3))
    lrc_path = os.path.splitext(filepath_mp3)[0] + ".lrc"
    if not os.path.exists(lrc_path):
        print("⚠️ No synced lyrics found.")
        return None
    return lrc_path
