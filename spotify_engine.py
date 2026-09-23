import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dotenv
from rapidfuzz import fuzz
from spotdl import Spotdl
from spotdl.types.album import Album
from spotdl.types.playlist import Playlist
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
        print("🔌 Connecting to Spotify...")
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
    client.downloader.progress_handler.update_callback = _progress_printer()

    print("🔎 Getting the track list from Spotify...")
    songs = client.search([url])
    if not songs:
        raise RuntimeError("No songs found for this Spotify URL")
    print(f"🎧 Found {len(songs)} song{'s' if len(songs) != 1 else ''}. Downloading audio from YouTube Music with Spotify's tags...")

    results = []
    for song, path in client.download_songs(songs):
        if path is None:
            print(f"❌ Failed to download: {song.display_name}")
            continue
        results.append((song.album_name or song.name, song.name, song.artist, str(path)))
    return results

def get_playlist_tracks(url):
    """Returns (title, list of (track URL, "Artist - Song")) for a Spotify playlist or album, without downloading."""
    _get_client()  # Spotify API calls need the client set up
    print("🔎 Reading the Spotify playlist...")
    list_class = Album if "/album/" in url else Playlist
    metadata, songs = list_class.get_metadata(url)
    return metadata["name"], [(song.url, song.display_name) for song in songs if song.url]

# spotdl's status messages, reworded for our log
_STATUS_TEXT = {
    "Searching for song": "finding the audio on YouTube Music",
    "Getting audio meta": "reading audio info",
    "Downloading": "downloading audio",
    "Converting": "converting to MP3",
    "Embedding metadata": "adding Spotify tags and cover art",
    "Done": "done ✅",
    "Skipped": "already downloaded, skipped",
    "Error": "failed ❌",
}

def _progress_printer():
    """Makes a spotdl progress callback that prints each song's steps once."""
    last_status = {}

    def on_update(tracker, message):
        if not message or last_status.get(tracker.song.url) == message:
            return
        last_status[tracker.song.url] = message
        # spotdl downloads several songs at once; a single write keeps their lines from mixing together
        sys.stdout.write(f"🎵 {tracker.song_name}: {_STATUS_TEXT.get(message, message.lower())}\n")

    return on_update

def _core_title(text):
    """Song title without the extras that differ between sites: "GOSSIP (feat. Tom Morello)" -> "gossip"."""
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text.lower())
    text = re.split(r"\s(?:feat\.?|ft\.?|featuring|with)\s", text)[0]
    return " ".join(text.split())

def find_spotify_match(artist, song_name):
    """Searches Spotify for the song. Returns spotdl's Song, or None if there's no confident match."""
    _get_client()  # Spotify search needs the client set up
    known_artist = artist and artist != "Unknown Artist"
    query = f"{artist} - {song_name}" if known_artist else song_name
    print(f"🔎 Searching Spotify for {query}...")
    try:
        match = Song.from_search_term(query)
    except Exception as error:
        print(f"⚠️ Couldn't find {query} on Spotify: {error}")
        return None

    # Spotify returns its top result no matter how bad it is, so make sure it's actually the same song.
    title_score = fuzz.token_set_ratio(_core_title(song_name), _core_title(match.name))
    if known_artist:
        artist_score = max(fuzz.token_set_ratio(artist.lower(), name.lower()) for name in match.artists)
    else:
        # Without an artist, at least one of Spotify's artists should show up somewhere in the title
        artist_score = max(fuzz.partial_ratio(name.lower(), song_name.lower()) for name in match.artists)
    if title_score < 80 or artist_score < 70:
        print(f"⚠️ Spotify's best match was {match.display_name}, which doesn't look like the same song.")
        return None

    print(f"🎧 Spotify match: {match.display_name} (album: {match.album_name})")
    return match

def download_lyrics(match, filepath_mp3):
    """Saves the synced lyrics of a Spotify match (from find_spotify_match) next to the MP3 as a .lrc file.

    Returns the .lrc path, or None when no synced lyrics were found.
    """
    print("🎤 Downloading synced lyrics...")
    generate_lrc(match, Path(filepath_mp3))
    lrc_path = os.path.splitext(filepath_mp3)[0] + ".lrc"
    if not os.path.exists(lrc_path):
        print("⚠️ No synced lyrics found.")
        return None
    print("📝 Saved synced lyrics.")
    return lrc_path
