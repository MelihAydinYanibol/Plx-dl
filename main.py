import argparse
import sys
import os
import dotenv

from file_utils import move_file, safe_name
from yt_engine import download_audio, get_playlist_videos, parse_youtube_url
from metadata_engine import metadata
from spotify_engine import download_lyrics, download_spotify, parse_spotify_url

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


def move_to_library(album, song, artist, filepath, output_path):
    target_dir = os.path.join(output_path, safe_name(album, "Unknown Album"))
    base_name = safe_name(f"{artist} - {song}")
    move_file(filepath, target_dir, base_name + ".mp3")

    # Plex picks up lyrics from a .lrc with the same name as the song, so move it along.
    lrc_path = os.path.splitext(filepath)[0] + ".lrc"
    if os.path.exists(lrc_path):
        move_file(lrc_path, target_dir, base_name + ".lrc")
    print(f"✅ Finished! {artist} - {song} is in: {target_dir}")


def main(argv=None):
    args = build_parser().parse_args(argv)
    dotenv.load_dotenv()

    cache_dir = resolve_dir(args.cache)
    if args.output is None:
        # Auto mode is on. So we will move the file to plex folder that's written in .env file automatically.
        output_path = os.getenv("DEST_BASE") or cache_dir
    else:
        output_path = args.output

    exit_code = 0
    for url in args.urls:
        if "youtube" in url or "youtu.be" in url:
            try:
                video_urls = resolve_youtube_urls(url, args.playlist_mode)
            except Exception as error:
                print(f"Failed to read {url}: {error}", file=sys.stderr)
                exit_code = 1
                continue

            for index, video_url in enumerate(video_urls, 1):
                prefix = f"[{index}/{len(video_urls)}] " if len(video_urls) > 1 else ""
                print(f"{prefix}Downloading from: {video_url}")
                try:
                    title, filepath = download_audio(video_url, cache_dir)
                    album, song, artist = metadata(title, filepath)
                    if args.lyrics:
                        try:
                            download_lyrics(artist, song, filepath)
                        except Exception as error:
                            # Missing lyrics shouldn't cost us the song
                            print(f"⚠️ Lyrics failed: {error}")
                    move_to_library(album, song, artist, filepath, output_path)
                except Exception as error:
                    print(f"Failed to process {video_url}: {error}", file=sys.stderr)
                    exit_code = 1

        elif "spotify" in url:
            print(f"Spotify URL detected: {url}")
            try:
                tracks = download_spotify(resolve_spotify_url(url, args.playlist_mode), cache_dir, args.lyrics)
            except Exception as error:
                print(f"Failed to download {url}: {error}", file=sys.stderr)
                exit_code = 1
                continue

            for album, song, artist, filepath in tracks:
                try:
                    move_to_library(album, song, artist, filepath, output_path)
                except Exception as error:
                    print(f"Failed to move {filepath}: {error}", file=sys.stderr)
                    exit_code = 1

        else:
            print(f"Unsupported URL: {url}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
