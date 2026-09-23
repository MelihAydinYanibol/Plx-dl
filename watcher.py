import json
import os
import threading
import time
import uuid

from main import SCRIPT_DIR, process_url
from spotify_engine import get_playlist_tracks, parse_spotify_url
from yt_engine import get_playlist_items, parse_youtube_url

WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "watchlist.json")
# A new song that keeps failing is given up on after this many checks, so it isn't retried forever.
MAX_ATTEMPTS = 3

_lock = threading.RLock()
_watches = {}


def normalize_playlist_url(url):
    """Returns (source, clean playlist URL) for a link we can watch, or raises ValueError."""
    if "youtube" in url or "youtu.be" in url:
        _, playlist_id = parse_youtube_url(url)
        if not playlist_id:
            raise ValueError("That YouTube link isn't a playlist.")
        if playlist_id.startswith("RD"):
            raise ValueError("YouTube mixes change on their own and can't be watched. Use a normal playlist.")
        return "youtube", f"https://www.youtube.com/playlist?list={playlist_id}"

    if "spotify" in url:
        clean_url, context = parse_spotify_url(url)
        if context:
            return "spotify", context[1]
        if "/playlist/" in clean_url or "/album/" in clean_url:
            return "spotify", clean_url
        raise ValueError("That Spotify link isn't a playlist or album.")

    raise ValueError("That doesn't look like a YouTube or Spotify link.")


def load():
    with _lock:
        _watches.clear()
        if not os.path.exists(WATCHLIST_PATH):
            return
        with open(WATCHLIST_PATH, encoding="utf-8") as file:
            content = file.read()
        if not content.strip():
            return
        try:
            for watch in json.loads(content):
                _watches[watch["id"]] = watch
        except (ValueError, TypeError, KeyError) as error:
            # Keep the broken file instead of overwriting it on the next save, so nothing is lost.
            backup_path = f"{WATCHLIST_PATH}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
            counter = 1
            while os.path.exists(backup_path):
                counter += 1
                backup_path = f"{WATCHLIST_PATH}.broken-{time.strftime('%Y%m%d-%H%M%S')}-{counter}"
            os.replace(WATCHLIST_PATH, backup_path)
            _watches.clear()
            print(f"⚠️ {WATCHLIST_PATH} couldn't be read ({error}). Moved it to {backup_path} and started with an empty watchlist.")


def _save():
    # Write to a temp file first so a crash mid-write can't wipe the watchlist.
    temp_path = WATCHLIST_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(list(_watches.values()), file, indent=2, ensure_ascii=False)
    os.replace(temp_path, WATCHLIST_PATH)


def _summary(watch):
    return {key: value for key, value in watch.items() if key not in ("seen", "attempts")} | {
        "song_count": len(watch["seen"]),
    }


def list_watches():
    with _lock:
        return [_summary(watch) for watch in sorted(_watches.values(), key=lambda w: w["added"])]


def get_watch(watch_id):
    with _lock:
        watch = _watches.get(watch_id)
        return _summary(watch) if watch else None


def add_watch(url, lyrics=True, download_existing=False):
    source, clean_url = normalize_playlist_url(url)
    with _lock:
        if any(watch["url"] == clean_url for watch in _watches.values()):
            raise ValueError("You're already watching that playlist.")
        watch = {
            "id": uuid.uuid4().hex[:12],
            "url": clean_url,
            "source": source,
            "title": None,
            "lyrics": lyrics,
            "download_existing": download_existing,
            "initialized": False,
            "paused": False,
            "added": time.time(),
            "last_checked": None,
            "last_result": None,
            "last_error": None,
            "seen": [],
            "attempts": {},
        }
        _watches[watch["id"]] = watch
        _save()
        return _summary(watch)


def remove_watch(watch_id):
    with _lock:
        if _watches.pop(watch_id, None) is None:
            return False
        _save()
        return True


def set_paused(watch_id, paused):
    with _lock:
        watch = _watches.get(watch_id)
        if watch is None:
            return None
        watch["paused"] = bool(paused)
        _save()
        return _summary(watch)


def due_watches(interval_seconds):
    """IDs of watches that should be checked now."""
    now = time.time()
    with _lock:
        return [
            watch["id"]
            for watch in _watches.values()
            if not watch["paused"] and (watch["last_checked"] is None or now - watch["last_checked"] >= interval_seconds)
        ]


def _list_items(watch):
    if watch["source"] == "youtube":
        return get_playlist_items(parse_youtube_url(watch["url"])[1])
    return get_playlist_tracks(watch["url"])


def check(watch_id, cache_dir, output_path, on_track=None):
    """Looks for songs added to a watched playlist and downloads them. Returns (new songs, failures).

    The first check only remembers what's already there, unless the watch was added with download_existing.
    """
    with _lock:
        watch = _watches.get(watch_id)
        if watch is None:
            raise KeyError("This playlist isn't being watched anymore.")
        watch = dict(watch, seen=set(watch["seen"]))

    print(f"🔁 Checking {watch['title'] or watch['url']} for new songs...")
    try:
        title, items = _list_items(watch)
    except Exception as error:
        with _lock:
            if watch_id in _watches:
                _watches[watch_id].update(last_checked=time.time(), last_error=str(error))
                _save()
        raise

    if not watch["initialized"] and not watch["download_existing"]:
        with _lock:
            if watch_id in _watches:
                _watches[watch_id].update(
                    title=title,
                    initialized=True,
                    seen=[item_url for item_url, _ in items],
                    last_checked=time.time(),
                    last_result=f"Started watching ({len(items)} songs)",
                    last_error=None,
                )
                _save()
        print(f"👀 Now watching '{title}' ({len(items)} songs). Songs added from now on will be downloaded.")
        return 0, 0

    new_items = [(item_url, label) for item_url, label in items if item_url not in watch["seen"]]
    with _lock:
        if watch_id in _watches:
            _watches[watch_id].update(title=title, initialized=True)
            _save()
    if new_items:
        print(f"🆕 {len(new_items)} new song{'s' if len(new_items) != 1 else ''} in '{title}'.")
    else:
        print(f"No new songs in '{title}'.")

    failures = 0
    for index, (item_url, label) in enumerate(new_items, 1):
        if watch_id not in _watches:
            print("Stopping: the playlist was removed from the watchlist.")
            break
        print(f"[{index}/{len(new_items)}] ⬇️ {label}")
        succeeded = process_url(item_url, cache_dir, output_path, "single", watch["lyrics"], on_track) == 0

        with _lock:
            stored = _watches.get(watch_id)
            if stored is None:
                continue
            if succeeded:
                stored["seen"].append(item_url)
                stored["attempts"].pop(item_url, None)
            else:
                failures += 1
                attempts = stored["attempts"].get(item_url, 0) + 1
                if attempts >= MAX_ATTEMPTS:
                    print(f"⚠️ Giving up on {label} after {attempts} failed tries.")
                    stored["seen"].append(item_url)
                    stored["attempts"].pop(item_url, None)
                else:
                    stored["attempts"][item_url] = attempts
            _save()

    with _lock:
        if watch_id in _watches:
            downloaded = len(new_items) - failures
            result = f"Downloaded {downloaded} new song{'s' if downloaded != 1 else ''}" if new_items else "No new songs"
            if failures:
                result += f", {failures} failed"
            _watches[watch_id].update(last_checked=time.time(), last_result=result, last_error=None)
            _save()
    return len(new_items), failures
