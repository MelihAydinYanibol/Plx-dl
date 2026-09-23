import argparse
import datetime
import getpass
import hmac
import io
import itertools
import os
import queue
import re
import secrets
import sys
import threading
import time

import watcher
from flask import Flask, abort, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from main import get_dirs, process_url
from metadata_engine import DEFAULT_PICARD_PATH
from spotify_engine import parse_spotify_url
from yt_engine import parse_youtube_url

MAX_LOG_LINES = 2000
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 5 * 60
SCHEDULER_TICK_SECONDS = 30
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
)

_jobs = {}
_jobs_lock = threading.Lock()
_job_ids = itertools.count(1)
_queue = queue.Queue()
_thread_state = threading.local()
_current_job = None
_dirs = {}
_credentials = {}
_watch_interval = {"seconds": 60 * 60}
_login_failures = {}  # ip -> (failure count, locked until)
_login_lock = threading.Lock()


class Job:
    def __init__(self, url, source, playlist_mode, lyrics, kind="download", watch_id=None, title=None):
        self.id = next(_job_ids)
        self.kind = kind  # "download", or "watch" for a watched-playlist check
        self.watch_id = watch_id
        self.title = title
        self.url = url
        self.source = source
        self.playlist_mode = playlist_mode
        self.lyrics = lyrics
        self.status = "queued"
        self.tracks = []
        self.log = []
        self.created = time.time()
        self.finished = None
        self._partial = ""

    def feed(self, text):
        """Takes raw console output and turns it into log lines."""
        with _jobs_lock:
            self._partial += _ANSI.sub("", text)
            *lines, self._partial = re.split(r"[\r\n]", self._partial)
            for line in lines:
                self._add_line(line)

    def flush_partial(self):
        with _jobs_lock:
            self._add_line(self._partial)
            self._partial = ""

    def _add_line(self, line):
        line = line.rstrip()
        if not line.strip():
            return
        # yt-dlp redraws its progress line over and over; keep only the latest one.
        if line.startswith("[download]") and self.log and self.log[-1].startswith("[download]") and "%" in self.log[-1]:
            self.log[-1] = line
        else:
            self.log.append(line)
            del self.log[:-MAX_LOG_LINES]

    def add_track(self, track):
        with _jobs_lock:
            self.tracks.append(track)

    def summary(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "playlist_mode": self.playlist_mode,
            "lyrics": self.lyrics,
            "status": self.status,
            "track_count": len(self.tracks),
            "last_line": self.log[-1] if self.log else "",
            "created": self.created,
            "finished": self.finished,
        }

    def details(self):
        return {**self.summary(), "tracks": list(self.tracks), "log": list(self.log)}


class _JobStream(io.TextIOBase):
    """Stands in for stdout/stderr: while a job runs, output goes to its log (from any thread, since spotdl
    downloads on its own threads) except Flask's request threads. Everything still reaches the real console."""

    def __init__(self, console):
        self.console = console

    @property
    def encoding(self):
        return getattr(self.console, "encoding", "utf-8")

    def isatty(self):
        return False

    def writable(self):
        return True

    def write(self, text):
        try:
            self.console.write(text)
        except UnicodeEncodeError:
            # Old Windows consoles can't show emoji; the web log still gets the real text
            self.console.write(text.encode(self.encoding, errors="replace").decode(self.encoding))
        job = _current_job
        if job and not getattr(_thread_state, "is_request", False):
            job.feed(text)
        return len(text)

    def flush(self):
        self.console.flush()


def _worker():
    # Every download runs on this one thread: spotdl allows a single client per process, and
    # Picard/Ollama work best one track at a time.
    global _current_job
    while True:
        job = _jobs.get(_queue.get())
        if job is None or job.status != "queued":
            continue
        job.status = "running"
        _current_job = job
        try:
            if job.kind == "watch":
                _run_watch_check(job)
            else:
                failures = process_url(
                    job.url, _dirs["cache"], _dirs["output"], job.playlist_mode, job.lyrics, on_track=job.add_track
                )
                if not job.tracks:
                    job.status = "failed"
                elif failures:
                    job.status = "partial"
                else:
                    job.status = "done"
        except Exception as error:
            print(f"❌ Unexpected error: {error}", file=sys.stderr)
            job.status = "failed"
        finally:
            job.flush_partial()
            _current_job = None
            job.finished = time.time()


def _run_watch_check(job):
    new_songs, failures = watcher.check(job.watch_id, _dirs["cache"], _dirs["output"], on_track=job.add_track)
    watch = watcher.get_watch(job.watch_id)
    if watch and watch["title"]:
        job.title = f"New songs in {watch['title']}"
    if not new_songs:
        # Nothing happened, so keep it out of the Downloads list; the watchlist shows the result.
        job.status = "done"
        with _jobs_lock:
            _jobs.pop(job.id, None)
    elif failures == new_songs:
        job.status = "failed"
    else:
        job.status = "partial" if failures else "done"


def _enqueue(job):
    with _jobs_lock:
        _jobs[job.id] = job
    _queue.put(job.id)
    return job


def _pending_check(watch_id):
    with _jobs_lock:
        return next(
            (job for job in _jobs.values() if job.watch_id == watch_id and job.status in ("queued", "running")), None
        )


def enqueue_check(watch_id):
    """Queues a check of a watched playlist, unless one is already waiting or running."""
    pending = _pending_check(watch_id)
    if pending:
        return pending
    watch = watcher.get_watch(watch_id)
    title = f"Checking {watch['title'] or 'new playlist'} for new songs"
    return _enqueue(Job(watch["url"], watch["source"], "single", watch["lyrics"], "watch", watch_id, title))


def _scheduler():
    while True:
        try:
            for watch_id in watcher.due_watches(_watch_interval["seconds"]):
                enqueue_check(watch_id)
        except Exception as error:
            print(f"⚠️ Watchlist scheduler error: {error}")
        time.sleep(SCHEDULER_TICK_SECONDS)


def inspect_url(url):
    """Works out what a link points to without touching the network."""
    if "youtube" in url or "youtu.be" in url:
        video_id, playlist_id = parse_youtube_url(url)
        if video_id and playlist_id:
            return {"source": "youtube", "kind": "video", "choice": "playlist"}
        if playlist_id:
            return {"source": "youtube", "kind": "playlist", "choice": None}
        return {"source": "youtube", "kind": "video" if video_id else "link", "choice": None}

    if "spotify" in url:
        clean_url, context = parse_spotify_url(url)
        kind = clean_url.rstrip("/").split("/")[-2] if "open.spotify.com/" in clean_url else "link"
        return {"source": "spotify", "kind": kind, "choice": context[0] if context else None}

    return {"source": None, "kind": None, "choice": None}


def _check_credentials(username, password):
    # Compare both fields every time so the response time doesn't hint at which one was wrong.
    user_ok = hmac.compare_digest(username.encode(), _credentials["username"].encode())
    stored = _credentials["password"]
    if stored.startswith(("scrypt:", "pbkdf2:")):
        password_ok = check_password_hash(stored, password)
    else:
        password_ok = hmac.compare_digest(password.encode(), stored.encode())
    return user_ok and password_ok


def _locked_out(ip):
    with _login_lock:
        _, locked_until = _login_failures.get(ip, (0, 0))
        return locked_until > time.time()


def _record_login(ip, success):
    with _login_lock:
        if success:
            _login_failures.pop(ip, None)
            return
        failures = _login_failures.get(ip, (0, 0))[0] + 1
        if failures >= MAX_LOGIN_FAILURES:
            print(f"🔒 Too many failed logins from {ip}, locking it out for {LOCKOUT_SECONDS // 60} minutes.")
            _login_failures[ip] = (0, time.time() + LOCKOUT_SECONDS)
        else:
            _login_failures[ip] = (failures, 0)


@app.before_request
def mark_request_thread():
    # Keeps request logging out of the job logs (see _JobStream). Must run before require_login.
    _thread_state.is_request = True


@app.before_request
def require_login():
    # Checking the name too means changing WEB_USERNAME logs everyone out.
    if request.endpoint == "login" or session.get("user") == _credentials["username"]:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not logged in."}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return redirect("/") if session.get("user") == _credentials["username"] else app.send_static_file("login.html")

    ip = request.remote_addr
    if _locked_out(ip):
        return redirect("/login?error=locked")
    username = request.form.get("username", "")
    if not _check_credentials(username, request.form.get("password", "")):
        _record_login(ip, False)
        return redirect("/login?error=invalid")

    _record_login(ip, True)
    session.clear()
    session.permanent = True
    session["user"] = username
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/config")
def config():
    picard_path = os.getenv("PICARD_PATH", DEFAULT_PICARD_PATH)
    return jsonify({"output": _dirs["output"], "picard": os.path.isfile(picard_path)})


@app.post("/api/inspect")
def inspect():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    return jsonify(inspect_url(url))


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        return jsonify([job.summary() for job in sorted(_jobs.values(), key=lambda j: j.id, reverse=True)])


@app.post("/api/jobs")
def create_job():
    body = request.get_json(silent=True) or {}
    url = str(body.get("url", "")).strip()
    info = inspect_url(url)
    if not url or info["source"] is None:
        return jsonify({"error": "That doesn't look like a YouTube or Spotify link."}), 400

    playlist_mode = body.get("playlist_mode")
    if playlist_mode not in ("all", "single"):
        playlist_mode = "single"
    job = _enqueue(Job(url, info["source"], playlist_mode, bool(body.get("lyrics", True))))
    return jsonify(job.summary()), 201


@app.get("/api/jobs/<int:job_id>")
def get_job(job_id):
    job = _jobs.get(job_id) or abort(404)
    with _jobs_lock:
        return jsonify(job.details())


@app.delete("/api/jobs/<int:job_id>")
def delete_job(job_id):
    """Cancels a queued job, or removes a finished one from the list."""
    job = _jobs.get(job_id) or abort(404)
    with _jobs_lock:
        if job.status == "running":
            return jsonify({"error": "Can't stop a job while it's running."}), 409
        if job.status == "queued":
            job.status = "cancelled"
            job.finished = time.time()
        else:
            del _jobs[job_id]
    return "", 204


@app.post("/api/jobs/clear")
def clear_jobs():
    with _jobs_lock:
        for job_id in [i for i, job in _jobs.items() if job.status not in ("queued", "running")]:
            del _jobs[job_id]
    return "", 204


def _watch_state(watch):
    pending = _pending_check(watch["id"])
    return {**watch, "state": pending.status if pending else ("paused" if watch["paused"] else "idle")}


@app.get("/api/watches")
def list_watches():
    return jsonify({
        "interval_minutes": _watch_interval["seconds"] // 60,
        "watches": [_watch_state(watch) for watch in watcher.list_watches()],
    })


@app.post("/api/watches")
def add_watch():
    body = request.get_json(silent=True) or {}
    try:
        watch = watcher.add_watch(
            str(body.get("url", "")).strip(),
            lyrics=bool(body.get("lyrics", True)),
            download_existing=bool(body.get("download_existing", False)),
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    enqueue_check(watch["id"])
    return jsonify(_watch_state(watch)), 201


@app.post("/api/watches/<watch_id>/check")
def check_watch(watch_id):
    if not watcher.get_watch(watch_id):
        abort(404)
    enqueue_check(watch_id)
    return "", 204


@app.patch("/api/watches/<watch_id>")
def update_watch(watch_id):
    body = request.get_json(silent=True) or {}
    watch = watcher.set_paused(watch_id, body.get("paused", False)) or abort(404)
    return jsonify(_watch_state(watch))


@app.delete("/api/watches/<watch_id>")
def delete_watch(watch_id):
    if not watcher.remove_watch(watch_id):
        abort(404)
    # Drop a check that's still waiting in the queue
    pending = _pending_check(watch_id)
    if pending and pending.status == "queued":
        pending.status = "cancelled"
    return "", 204


def build_parser():
    parser = argparse.ArgumentParser(prog="plx-dl-web", description="Web interface for plx-dl.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on. Use 0.0.0.0 to open it to your network (there's no login!). (default: 127.0.0.1)",
    )
    parser.add_argument("-p", "--port", type=int, default=8080, help="port to listen on. (default: 8080)")
    parser.add_argument("-c", "--cache", default="cache", help="directory where MP3 files are first saved. (default: cache)")
    parser.add_argument("-o", "--output", default=None, help="directory where MP3 files are finally saved. (default: .env value)")
    parser.add_argument(
        "--hash-password",
        action="store_true",
        help="print a hashed version of a password to put in WEB_PASSWORD, then exit",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.hash_password:
        print(generate_password_hash(getpass.getpass("Password to hash: ")))
        return

    _dirs["cache"], _dirs["output"] = get_dirs(args.cache, args.output)
    _credentials["username"] = os.getenv("WEB_USERNAME", "")
    _credentials["password"] = os.getenv("WEB_PASSWORD", "")
    if not _credentials["username"] or not _credentials["password"]:
        raise SystemExit("Set WEB_USERNAME and WEB_PASSWORD in .env before starting the web app.")
    # Without a fixed key, a new one is made on every start, which just means logging in again after restarts.
    app.secret_key = os.getenv("WEB_SECRET_KEY") or secrets.token_hex(32)
    _watch_interval["seconds"] = max(5, int(os.getenv("WATCH_INTERVAL_MINUTES") or 60)) * 60
    watcher.load()

    sys.stdout = _JobStream(sys.stdout)
    sys.stderr = _JobStream(sys.stderr)
    threading.Thread(target=_worker, name="plx-dl-worker", daemon=True).start()
    threading.Thread(target=_scheduler, name="plx-dl-scheduler", daemon=True).start()

    print(f"🎵 plx-dl web is running at http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
    print(f"   Music goes to: {_dirs['output']}")
    print(f"   Watching {len(watcher.list_watches())} playlist(s), checking every {_watch_interval['seconds'] // 60} min")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
