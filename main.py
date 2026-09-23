import argparse
import sys
import os
import dotenv

from file_utils import move_file, safe_name
from yt_engine import download_audio, get_playlist_videos, parse_youtube_url
from metadata_engine import metadata, write_tags
from spotify_engine import download_lyrics, download_spotify, find_spotify_match, parse_spotify_url

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def build_parser():
    parser = argparse.ArgumentParser(
        prog="plx-dl",
        description="Download YouTube or Spotify audio as MP3 files.",
    )
    parser.add_argument(
        "urls",
        nargs="+",
        metavar="URL",
        help="YouTube or Spotify URL(s) to download",
    )
    parser.add_argument(
        "-c",
        "--cache",
        default="cache",
        help="directory where MP3 files are first saved. (default: cache)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="directory where MP3 files are finally saved. (default: .env value)",
    )
    playlist_mode = parser.add_mutually_exclusive_group()
    playlist_mode.add_argument(
        "-a",
        "--all",
        dest="playlist_mode",
        action="store_const",
        const="all",
        help="if a track link is part of a playlist/album, download the whole list without asking",
    )
    playlist_mode.add_argument(
        "-s",
        "--single",
        dest="playlist_mode",
        action="store_const",
        const="single",
        help="if a track link is part of a playlist/album, download only that track without asking",
    )
    parser.add_argument(
        "--no-lyrics",
        dest="lyrics",
        action="store_false",
        help="don't download synced lyrics (.lrc files)",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="plx-dl 1.0.0",
    )
    return parser


def resolve_dir(path):
    # "cache" means the cache folder next to this script, same as the engines use.
    if path == "cache":
        return os.path.join(SCRIPT_DIR, "cache")
    return os.path.abspath(path)


def ask_playlist_choice(collection, playlist_mode):
    """Asks whether to download the whole playlist/album a track link belongs to. Returns "all" or "single"."""
    if playlist_mode:
        return playlist_mode
    article = "an" if collection == "album" else "a"
    no_prompt = f"Link is part of {article} {collection} (no terminal to ask), downloading just this track. Use --all to get the whole list."
    if not sys.stdin.isatty():
        print(no_prompt)
        return "single"
    while True:
        try:
            answer = input(f"This link is part of {article} {collection}. Download [a]ll of it or just [t]his track? [T/a]: ").strip().lower()
        except EOFError:
            print("\n" + no_prompt)
            return "single"
        if answer in ("a", "all"):
            return "all"
        if answer in ("", "t", "track"):
            return "single"


def resolve_youtube_urls(url, playlist_mode):
    """Turns a YouTube link into the list of video URLs to download."""
    video_id, playlist_id = parse_youtube_url(url)
    if playlist_id and (video_id is None or ask_playlist_choice("playlist", playlist_mode) == "all"):
        title, video_urls = get_playlist_videos(playlist_id)
        print(f"📃 Playlist '{title}': {len(video_urls)} videos")
        return video_urls
    if video_id:
        return [f"https://www.youtube.com/watch?v={video_id}"]
    return [url]


def resolve_spotify_url(url, playlist_mode):
    """Picks the Spotify URL to hand to spotdl (a track, or the playlist/album it was shared from)."""
    clean_url, context = parse_spotify_url(url)
    if context:
        context_kind, context_url = context
        if ask_playlist_choice(context_kind, playlist_mode) == "all":
            return context_url
    return clean_url


def move_to_library(album, song, artist, filepath, output_path, on_track=None):
    target_dir = os.path.join(output_path, safe_name(album, "Unknown Album"))
    print("📦 Moving to your library...")
    base_name = safe_name(f"{artist} - {song}")
    move_file(filepath, target_dir, base_name + ".mp3")

    # Plex picks up lyrics from a .lrc with the same name as the song, so move it along.
    lrc_path = os.path.splitext(filepath)[0] + ".lrc"
    has_lyrics = os.path.exists(lrc_path)
    if has_lyrics:
        move_file(lrc_path, target_dir, base_name + ".lrc")
    print(f"✅ Finished! {artist} - {song} is in: {target_dir}")
    if on_track:
        on_track({"artist": artist, "song": song, "album": album, "folder": target_dir, "lyrics": has_lyrics})


def finish_youtube_song(album, song, artist, filepath, lyrics):
    """Fills in missing tags from Spotify and fetches lyrics. Returns the final (album, song, artist).

    One Spotify search serves both: it's only skipped when MusicBrainz already found the album and
    lyrics are off.
    """
    match = None
    if lyrics or not album:
        try:
            match = find_spotify_match(artist, song)
        except Exception as error:
            print(f"⚠️ Spotify lookup failed: {error}")

    if not album:
        if match:
            artist, song, album = match.artist, match.name, match.album_name or match.name
            print(f"🏷️ MusicBrainz had nothing, using Spotify's tags: {artist} - {song} (album: {album})")
        else:
            # No album anywhere, so treat the song as a single
            album = song
            print(f"⚠️ No album found. Filing it as a single: {artist} - {song}")
        write_tags(filepath, artist, song, album)

    if lyrics:
        if match:
            try:
                download_lyrics(match, filepath)
            except Exception as error:
                # Missing lyrics shouldn't cost us the song
                print(f"⚠️ Lyrics failed: {error}")
        else:
            print("⚠️ Skipping lyrics: couldn't find the song on Spotify.")
    return album, song, artist


def process_url(url, cache_dir, output_path, playlist_mode=None, lyrics=True, on_track=None):
    """Downloads, tags and files everything behind one link. Returns the number of failures.

    on_track, if given, is called with a dict for every song that reaches the library.
    """
    failures = 0
    if "youtube" in url or "youtu.be" in url:
        try:
            video_urls = resolve_youtube_urls(url, playlist_mode)
        except Exception as error:
            print(f"Failed to read {url}: {error}", file=sys.stderr)
            return 1

        for index, video_url in enumerate(video_urls, 1):
            prefix = f"[{index}/{len(video_urls)}] " if len(video_urls) > 1 else ""
            print(f"{prefix}Downloading from: {video_url}")
            try:
                title, filepath, hints = download_audio(video_url, cache_dir)
                album, song, artist = metadata(title, filepath, hints=hints)
                album, song, artist = finish_youtube_song(album, song, artist, filepath, lyrics)
                move_to_library(album, song, artist, filepath, output_path, on_track)
            except Exception as error:
                print(f"Failed to process {video_url}: {error}", file=sys.stderr)
                failures += 1

    elif "spotify" in url:
        print(f"Spotify URL detected: {url}")
        try:
            tracks = download_spotify(resolve_spotify_url(url, playlist_mode), cache_dir, lyrics)
        except Exception as error:
            print(f"Failed to download {url}: {error}", file=sys.stderr)
            return 1
        if not tracks:
            # spotdl skips songs it can't download instead of raising
            return 1

        for album, song, artist, filepath in tracks:
            try:
                move_to_library(album, song, artist, filepath, output_path, on_track)
            except Exception as error:
                print(f"Failed to move {filepath}: {error}", file=sys.stderr)
                failures += 1

    else:
        print(f"Unsupported URL: {url}", file=sys.stderr)
        return 1
    return failures


def get_dirs(cache="cache", output=None):
    """Returns (cache_dir, output_path). Without an output, files go to DEST_BASE from .env."""
    dotenv.load_dotenv()
    cache_dir = resolve_dir(cache)
    if output is None:
        # Auto mode is on. So we will move the file to plex folder that's written in .env file automatically.
        return cache_dir, os.getenv("DEST_BASE") or cache_dir
    return cache_dir, output


def main(argv=None):
    args = build_parser().parse_args(argv)
    cache_dir, output_path = get_dirs(args.cache, args.output)

    exit_code = 0
    for url in args.urls:
        if process_url(url, cache_dir, output_path, args.playlist_mode, args.lyrics):
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
