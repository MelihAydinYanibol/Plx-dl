import os
import re
import subprocess
import ollama
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB

MODEL_NAME = "llama3"
DEFAULT_PICARD_PATH = r'C:\Program Files\MusicBrainz Picard\picard.exe'
UNKNOWN_ARTIST = "Unknown Artist"

# Bracketed bits in video titles that aren't part of the song name, e.g. "(Official Video)", "[4K Remaster]"
_TITLE_JUNK = re.compile(
    r"\s*[\(\[][^\)\]]*\b(official|video|audio|lyrics?|hd|hq|4k|remaster(ed)?|visuali[sz]er|mv|m/v)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_ARTIST_SEPARATOR = re.compile(r"\s+[-–—|]\s+")
_CHANNEL_JUNK = re.compile(r"\s*(- topic|vevo|official)\s*$", re.IGNORECASE)

def ask_local_ai_for_metadata(raw_title):
    """Uses local Ollama model to extract Artist and Song."""
    prompt = (
        f"Extract the artist and song name from this YouTube title: '{raw_title}'. "
        "Ignore '(Official Video)', 'Lyrics', 'HD', or years. "
        "Respond ONLY in this format: Artist | Song Name"
    )
    print(f"🤖 Asking the local AI ({MODEL_NAME}) to clean up the title...")
    try:
        response = ollama.chat(model=MODEL_NAME, messages=[{'role': 'user', 'content': prompt}])
        result = response['message']['content']
        # Models sometimes add chatter around the answer, so use the last line that has the separator.
        answer_lines = [line for line in result.splitlines() if "|" in line]
        if answer_lines:
            artist, song = answer_lines[-1].split("|", 1)
            artist, song = artist.strip(" '\"*"), song.strip(" '\"*")
            if artist and song:
                return artist, song
    except Exception as e:
        print(f"❌ AI Error: {e}")
    return None

def guess_from_title(raw_title, channel=""):
    """Best-effort (artist, song) from a video title like "Artist - Song (Official Video)", for when the AI can't help."""
    title = " ".join(_TITLE_JUNK.sub("", raw_title).split())
    parts = _ARTIST_SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2 and all(part.strip() for part in parts):
        return parts[0].strip(), parts[1].strip()
    # No "Artist - Song" in the title, so the channel is the best guess for the artist ("Måneskin - Topic", "AdeleVEVO")
    artist = _CHANNEL_JUNK.sub("", channel).strip()
    return artist or UNKNOWN_ARTIST, title or raw_title

def write_tags(filepath_mp3, artist=None, song=None, album=None):
    """Writes whichever of artist/song/album are given into the MP3's ID3 tags."""
    audio = MP3(filepath_mp3, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    for frame, value in ((TPE1, artist), (TIT2, song), (TALB, album)):
        if value:
            audio.tags.add(frame(encoding=3, text=value))
    audio.save()

def run_picard(filepath_mp3):
    """Lets MusicBrainz Picard tag the file. Skipped with a warning if Picard isn't installed."""
    picard_path = os.getenv("PICARD_PATH", DEFAULT_PICARD_PATH)
    if not os.path.isfile(picard_path):
        print(f"⚠️ Picard not found at {picard_path}, skipping MusicBrainz lookup (set PICARD_PATH in .env).")
        return

    print("🔍 Looking the song up on MusicBrainz with Picard...")
    subprocess.run([
        picard_path,
        filepath_mp3, '-e', 'SCAN', '-e', 'SAVE_MATCHED', '-e', 'QUIT'
    ], check=False)

    if not os.path.exists(filepath_mp3):
        raise FileNotFoundError(
            f"Picard moved or renamed {filepath_mp3}. Turn off 'Rename files' and 'Move files' in Picard's options."
        )

def _tag_text(tags, key):
    frame = tags.get(key)
    if frame and frame.text:
        return str(frame.text[0]).strip()
    return ""

def metadata(title, filepath_mp3, ai=True, picard=True, hints=None):
    """Tags the MP3 and returns (album, song, artist). album is None when no album was found.

    Where the artist and song come from, best first: YouTube's own music info (hints), the local AI,
    then a guess from the title. A Picard match then wins over all of those.
    """
    hints = hints or {}
    if hints.get("artist") and hints.get("track"):
        # 1. YouTube already knows the song, so there's nothing to guess
        artist, song = hints["artist"], hints["track"]
        print(f"🎼 YouTube lists this as: {artist} - {song}")
    else:
        # 1. Get AI Cleaned Metadata, or guess from the title when the AI isn't available
        guess = ask_local_ai_for_metadata(title) if ai else None
        if guess:
            artist, song = guess
            print(f"✨ AI parsing complete: {artist} - {song}")
        else:
            artist, song = guess_from_title(title, hints.get("channel", ""))
            print(f"🔤 Guessed from the video title: {artist} - {song}")

    # 2. Initial Tagging (So Picard has something to work with)
    write_tags(filepath_mp3, artist, song)

    if picard:
        # 3. Run MusicBrainz Picard
        run_picard(filepath_mp3)

    # 4. Read back the final tags, falling back to what we already know
    audio = MP3(filepath_mp3, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    artist = _tag_text(audio.tags, 'TPE1') or artist
    song = _tag_text(audio.tags, 'TIT2') or song
    album = _tag_text(audio.tags, 'TALB') or None
    if album:
        print(f"🏷️ MusicBrainz match: {artist} - {song} (album: {album})")
    return album, song, artist
