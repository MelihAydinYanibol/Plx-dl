import os
import subprocess
import ollama
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB

MODEL_NAME = "llama3"
DEFAULT_PICARD_PATH = r'C:\Program Files\MusicBrainz Picard\picard.exe'

def ask_local_ai_for_metadata(raw_title):
    """Uses local Ollama model to extract Artist and Song."""
    prompt = (
        f"Extract the artist and song name from this YouTube title: '{raw_title}'. "
        "Ignore '(Official Video)', 'Lyrics', 'HD', or years. "
        "Respond ONLY in this format: Artist | Song Name"
    )
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
    return "Unknown Artist", raw_title

def run_picard(filepath_mp3):
    """Lets MusicBrainz Picard tag the file. Skipped with a warning if Picard isn't installed."""
    picard_path = os.getenv("PICARD_PATH", DEFAULT_PICARD_PATH)
    if not os.path.isfile(picard_path):
        print(f"⚠️ Picard not found at {picard_path}, skipping MusicBrainz lookup (set PICARD_PATH in .env).")
        return

    print("🔍 Checking MusicBrainz...")
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

def metadata(title, filepath_mp3, ai=True, picard=True):
    """Tags the MP3 and returns (album, song, artist). Tags from a Picard match win over the AI guess."""
    artist, song = "Unknown Artist", title

    if ai:
        # 1. Get AI Cleaned Metadata
        artist, song = ask_local_ai_for_metadata(title)
        print(f"✨ AI parsing complete: {artist} - {song}")

        # 2. Initial Tagging (So Picard has something to work with)
        audio = MP3(filepath_mp3, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TIT2(encoding=3, text=song))
        audio.save()

    if picard:
        # 3. Run MusicBrainz Picard
        run_picard(filepath_mp3)

    # 4. Read back the final tags, falling back to what we already know
    audio = MP3(filepath_mp3, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    artist = _tag_text(audio.tags, 'TPE1') or artist
    song = _tag_text(audio.tags, 'TIT2') or song
    album = _tag_text(audio.tags, 'TALB')

    if not album:
        # No album found, so treat the song as a single
        print(f"⚠️ No album found. Setting album to: {song}")
        album = song
        audio.tags.add(TALB(encoding=3, text=album))
        audio.save()

    return album, song, artist
