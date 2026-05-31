"""
================================================================================
Music Quality Check - Modular, Scalable & In-File Tagging Edition
================================================================================
Description:
Audio file analysis tool for quality control and metadata collection.
- Detects corrupted files.
- Generates high-resolution spectrograms.
- Extracts technical metadata and audio tags via FFmpeg.
- Fallback: Fetches missing genres via MusicBrainz API.
- Automatically writes fetched/missing tags back INTO the actual audio files.
- Applies configurable bitrate-based auto-actions (Trash / Wishlist / Low-Quality)
  by reading the user's saved Preferences from the Flask app's API.
- Stores all reports in a local structured SQLite database.

Project Directory Structure (BatchQC):
- BatchQC/scripts/analyze-tracks.py  (This script)
- BatchQC/database/track-repository.db (Automatically created)
"""

import sqlite3
import shutil
import subprocess
import re
import json
import os
import urllib.request
import urllib.parse
from datetime import date
from pathlib import Path


# ==============================================================================
# 1. CENTRAL SCRIPT CONFIGURATION (Aligned with Directory Structure)
# ==============================================================================
class Config:
    """
    Holds all global paths, folder configurations, and application constants.
    Centralizing this makes it easy to maintain or expand the project later.
    """
    # Dynamically locates the "BatchQC" root directory.
    # Since this script resides in "BatchQC/scripts/", .parent.parent goes 2 levels up.
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

    # Source directory tracking incoming unverified audio files
    INPUT_FOLDER = PROJECT_ROOT / "input"

    # Core output container directory
    OUTPUT_BASE = PROJECT_ROOT / "output"

    # Sub-directories mapped inside the core output directory pool
    CORRUPTED_FOLDER    = OUTPUT_BASE / "corrupted"
    SPECTROGRAMS_FOLDER = OUTPUT_BASE / "spectrograms"
    TRASH_FOLDER        = OUTPUT_BASE / "trash"
    LOW_QUALITY_FOLDER  = OUTPUT_BASE / "low-quality"

    # Persistence relational reporting database parameters
    DATABASE_FOLDER = PROJECT_ROOT / "database"
    DB_FILE         = DATABASE_FOLDER / "track-repository.db"
    WISHLIST_DB     = DATABASE_FOLDER / "wishlist.db"

    # Flask app base URL — used to fetch Preferences at runtime
    # Change this if the app runs on a different host/port.
    FLASK_BASE_URL = "http://127.0.0.1:5000"

    # Supported file extensions for automated batch processing cycles
    SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".mp4", ".aif", ".aiff"}

    # Required User-Agent identification identifier for the MusicBrainz API queries
    USER_AGENT = 'BatchQCAudioAnalyzer/3.0 (contact-via-github-or-email)'


# ==============================================================================
# 2. DATABASE MANAGER (Encapsulates all SQL Layer Operations)
# ==============================================================================
class DatabaseManager:
    """
    Manages all SQLite database connections, schema definitions, and queries
    for both Quality Control reports and the Wishlist repository.

    This unified approach encapsulates operations into a single database file
    while organizing data logically across distinct, specialized tables.
    """

    def __init__(self, db_path: Path):
        """
        Initializes the database manager with a target persistence file path.
        Automatically triggers schema verification and table creation routines.
        """
        self.db_path = db_path
        self._initialize_database_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Creates and returns a fresh isolated SQLite connection context.
        Enforces foreign key constraints if expanded in future updates.
        """
        return sqlite3.connect(self.db_path)

    def _initialize_database_schema(self):
        """
        Executes internal database DDL routines to safely establish necessary
        tables if they do not already exist within the storage engine.
        """
        # Ensure the parent directory structure physically exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._get_connection() as conn:
            # Table 1: Core Quality Control track analysis tracking registry
            conn.execute('''
                CREATE TABLE IF NOT EXISTS qc_report (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    FileName TEXT,
                    FilePath TEXT UNIQUE,
                    Artist TEXT,
                    Title TEXT,
                    Genre TEXT,
                    Bitrate_kbps INTEGER,
                    SampleRate_Hz INTEGER,
                    Channels INTEGER,
                    Duration TEXT,
                    SpectrumPath TEXT,
                    Status TEXT,
                    Timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Table 2: Curation target wishlist repository (Unified into the same DB)
            # UNIQUE constraint prevents duplicate track configurations
            conn.execute('''
                CREATE TABLE IF NOT EXISTS wishlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    Artist TEXT,
                    Title TEXT,
                    Genre TEXT,
                    FileName TEXT,
                    DateAdded TEXT,
                    UNIQUE(Artist, Title)
                )
            ''')
            conn.commit()

    # --- QC_REPORT TABLE METHODS ---

    def get_processed_file_paths(self) -> set:
        """
        Queries and compiles a lookup set of all previously analyzed files
        to efficiently skip duplicate ingestion pipelines.
        """
        query_string = "SELECT FilePath FROM qc_report"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query_string)
            # Returns a set structure for optimized O(1) compliance checks
            return {row[0] for row in cursor.fetchall()}

    def insert_qc_track(self, track_metadata: dict):
        """
        Persists a newly audited audio file tracking entry into the qc_report table.
        """
        insert_query = '''
            INSERT INTO qc_report (
                FileName, FilePath, Artist, Title, Genre,
                Bitrate_kbps, SampleRate_Hz, Channels, Duration, SpectrumPath, Status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        with self._get_connection() as conn:
            conn.execute(insert_query, (
                track_metadata["FileName"],
                track_metadata["FilePath"],
                track_metadata["Artist"],
                track_metadata["Title"],
                track_metadata["Genre"],
                track_metadata["Bitrate_kbps"],
                track_metadata["SampleRate_Hz"],
                track_metadata["Channels"],
                track_metadata["Duration"],
                track_metadata["SpectrumPath"],
                track_metadata["Status"]
            ))
            conn.commit()

    # --- WISHLIST TABLE METHODS ---

    def insert_wishlist_track(self, track_metadata: dict):
        """
        Safely inserts a track into the wishlist table repository.
        Uses INSERT OR IGNORE to automatically bypass duplicate entities.
        """
        insert_query = '''
            INSERT OR IGNORE INTO wishlist (Artist, Title, Genre, FileName, DateAdded)
            VALUES (?, ?, ?, ?, ?)
        '''
        with self._get_connection() as conn:
            conn.execute(insert_query, (
                track_metadata.get("Artist"),
                track_metadata.get("Title"),
                track_metadata.get("Genre"),
                track_metadata.get("FileName"),
                str(date.today())
            ))
            conn.commit()


# ==============================================================================
# 3. PREFERENCES LOADER
# ==============================================================================
class PreferencesLoader:
    """
    Fetches the user's saved Preferences from the running Flask app's JSON API.
    Falls back to safe defaults if the Flask app is not reachable (e.g. standalone runs).

    Why fetch from Flask instead of reading the DB directly?
    - Single source of truth: Preferences logic (defaults, validation) lives in app.py.
    - This script stays decoupled from Flask internals.
    """

    # These mirrors DEFAULT_PREFERENCES in app.py.
    # Update both places if you add new preference keys.
    FALLBACK = {
        "bitrate_threshold":  128,
        "action_trash":       False,
        "action_wishlist":    False,
        "action_low_quality": False,
    }

    @classmethod
    def load(cls) -> dict:
        """
        Attempts to GET preferences from the Flask API endpoint.
        Returns the fallback defaults if the request fails.
        """
        url = f"{Config.FLASK_BASE_URL}/api/preferences"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as response:
                prefs = json.loads(response.read().decode())
                print(f"[Preferences] Loaded from Flask API: {prefs}")
                return prefs
        except Exception as e:
            print(f"[Preferences] Flask API unreachable ({e}). Using defaults.")
            return dict(cls.FALLBACK)


# ==============================================================================
# 4. BITRATE ACTION HANDLER
# ==============================================================================
class BitrateActionHandler:
    """
    Evaluates tracks against quality criteria constraints and routes files
    or tracking entry updates based on user preferences.
    """

    @staticmethod
    def _move_file_to_destination(source_path: str, destination_folder: Path) -> str:
        """
        Moves a file to a specific destination folder. Creates directories if needed.
        """
        destination_folder.mkdir(parents=True, exist_ok=True)
        target_path = str(destination_folder / os.path.basename(source_path))
        if os.path.exists(source_path):
            shutil.move(source_path, target_path)
        return target_path

    @classmethod
    def apply(cls, track: dict, prefs: dict, db_manager: DatabaseManager) -> str | None:
        """
        Processes bitrate checks and executes requested automated lifecycle actions.
        """
        threshold = int(prefs.get("bitrate_threshold", 128))
        bitrate   = int(track.get("Bitrate_kbps") or 0)

        any_action_enabled = any([
            prefs.get("action_trash"),
            prefs.get("action_wishlist"),
            prefs.get("action_low_quality"),
        ])

        if not any_action_enabled or bitrate == 0 or bitrate >= threshold:
            return None

        print(f"   -> [BitrateRule] '{track.get('FileName')}' is {bitrate} kbps "
              f"(below threshold of {threshold} kbps). Processing actions...")

        # --- Wishlist Action via Unified Database Architecture ---
        if prefs.get("action_wishlist"):
            # We call the unified manager directly to interact with the wishlist table
            db_manager.insert_wishlist_track(track)
            print(f"   -> [BitrateAction] Added metadata to database wishlist registry table.")

        # --- File System Execution Pipeline ---
        if prefs.get("action_trash"):
            cls._move_file_to_destination(track["FilePath"], Config.TRASH_FOLDER)
            print(f"   -> [BitrateAction] Moved file to Trash.")
            return "Trash"

        if prefs.get("action_low_quality"):
            cls._move_file_to_destination(track["FilePath"], Config.LOW_QUALITY_FOLDER)
            print(f"   -> [BitrateAction] Moved file to Low-Quality.")
            return "Low-Quality"

        return "Wishlist"


# ==============================================================================
# 5. EXTERNAL API SERVICES (MusicBrainz)
# ==============================================================================
class MusicBrainzService:
    """
    Handles remote API networking queries to fetch missing metadata tags.
    """

    @staticmethod
    def fetch_genre(artist: str, title: str) -> str:
        """Queries the MusicBrainz Web API for the most popular tag/genre of a track."""
        if not artist or not title:
            return ""

        try:
            query = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
            url   = f"https://musicbrainz.org/ws/2/recording/?query={query}&fmt=json"
            req   = urllib.request.Request(url, headers={'User-Agent': Config.USER_AGENT})

            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())

                if data.get("recordings"):
                    best_match = data["recordings"][0]
                    if "tags" in best_match:
                        sorted_tags = sorted(best_match["tags"], key=lambda x: x.get("count", 0), reverse=True)
                        if sorted_tags:
                            found_genre = sorted_tags[0]["name"].title()
                            print(f"   -> [API] Genre found via MusicBrainz: {found_genre}")
                            return found_genre
        except Exception as e:
            print(f"   -> [API-Warning] MusicBrainz unavailable or no matching tags: {e}")

        return ""


# ==============================================================================
# 6. AUDIO PROCESSOR (FFmpeg Engine Wrapper)
# ==============================================================================
class AudioProcessor:
    """
    Encapsulates all technical audio demuxing, metadata extraction, and tagging routines.
    """

    @staticmethod
    def _run_ffmpeg(command: list) -> str:
        """Executes a given FFmpeg subprocess command natively and returns console output."""
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="ignore"
        )
        return result.stdout

    @classmethod
    def check_is_corrupted(cls, file_path: Path) -> bool:
        """Asks FFmpeg to scan the stream packet containers to detect file corruption."""
        command = ["ffmpeg", "-v", "error", "-i", str(file_path), "-f", "null", "-"]
        result  = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode != 0

    @classmethod
    def generate_spectrogram(cls, audio_file: Path) -> Path:
        """Generates a high-definition 1080p visual spectrogram picture of the audio file."""
        output_file = Config.SPECTROGRAMS_FOLDER / f"{audio_file.stem}-spectrogram.png"
        command = [
            "ffmpeg", "-y", "-i", str(audio_file), "-lavfi",
            "showspectrumpic=s=1920x1080", "-update", "1", str(output_file)
        ]
        cls._run_ffmpeg(command)
        return output_file

    @classmethod
    def extract_metadata(cls, audio_file: Path) -> dict:
        """Parses FFmpeg's technical stream console printout to collect tags and stats."""
        command = ["ffmpeg", "-i", str(audio_file)]
        output  = cls._run_ffmpeg(command)

        info = {
            "Bitrate_kbps": 0, "SampleRate_Hz": 0, "Channels": 0,
            "Duration": "Unknown", "Artist": None, "Title": None, "Genre": None
        }

        bitrate = re.search(r"bitrate: (\d+) kb/s", output)
        if bitrate: info["Bitrate_kbps"] = int(bitrate.group(1))

        samplerate = re.search(r"(\d+) Hz", output)
        if samplerate: info["SampleRate_Hz"] = int(samplerate.group(1))

        if "stereo" in output.lower():
            info["Channels"] = 2
        elif "mono" in output.lower():
            info["Channels"] = 1

        duration = re.search(r"Duration: (\d+:\d+:\d+\.\d+)", output)
        if duration: info["Duration"] = duration.group(1)

        artist = re.search(r"artist\s*:\s*(.+)", output, re.IGNORECASE)
        if artist: info["Artist"] = artist.group(1).strip()

        title = re.search(r"title\s*:\s*(.+)", output, re.IGNORECASE)
        if title: info["Title"] = title.group(1).strip()

        genre = re.search(r"genre\s*:\s*(.+)", output, re.IGNORECASE)
        if genre: info["Genre"] = genre.group(1).strip()

        return info

    @classmethod
    def write_metadata_to_file(cls, audio_file: Path, genre: str):
        """
        Writes newly fetched metadata (like Genre) directly back into the physical audio file.
        Uses FFmpeg stream-copying (-c:a copy) to preserve original quality without re-encoding.
        """
        if not genre:
            return

        print(f"   -> [File-Tagging] Writing updated genre '{genre}' into the audio file...")
        temp_file = audio_file.parent / f"temp_{audio_file.name}"

        command = [
            "ffmpeg", "-y", "-i", str(audio_file),
            "-metadata", f"genre={genre}",
            "-c:a", "copy", str(temp_file)
        ]
        cls._run_ffmpeg(command)

        if temp_file.exists() and temp_file.stat().st_size > 0:
            os.replace(str(temp_file), str(audio_file))
        else:
            if temp_file.exists():
                os.remove(str(temp_file))
            print(f"   -> [Error] Failed to write metadata back to {audio_file.name}")


# ==============================================================================
# 7. CORE EXECUTION PIPELINE
# ==============================================================================
def main():
    """Orchestrates directories, scans input audio files, applies bitrate rules, and logs results."""

    # 1. Initialize all project directory components
    for folder in [
        Config.INPUT_FOLDER,
        Config.SPECTROGRAMS_FOLDER,
        Config.CORRUPTED_FOLDER,
        Config.TRASH_FOLDER,
        Config.LOW_QUALITY_FOLDER,
        Config.DATABASE_FOLDER,
    ]:
        folder.mkdir(parents=True, exist_ok=True)

    # 2. Load user preferences once for the entire batch run
    prefs = PreferencesLoader.load()

    # 3. Fire up the relational DB engine layer
    db = DatabaseManager(Config.DB_FILE)
    already_processed = db.get_processed_file_paths()

    # 4. Scan the source folder recursively for valid music items
    audio_files       = [f for f in Config.INPUT_FOLDER.rglob("*") if f.suffix.lower() in Config.SUPPORTED_EXTENSIONS]
    new_tracks_counter = 0

    print(f"Launching BatchQC Pipeline. Logging to: {Config.DB_FILE.name}")
    print("-" * 80)

    for audio_file in audio_files:
        file_path_str = str(audio_file.resolve())

        # Skip track if it was already logged during a previous session
        if file_path_str in already_processed:
            continue

        print(f"\nProcessing: {audio_file.name}")

        # Integrity check: Filter out and isolate corrupted audio tracks
        if AudioProcessor.check_is_corrupted(audio_file):
            print("   -> [WARNING] File corrupted! Isolating track to 'Corrupted' folder.")
            shutil.move(str(audio_file), str(Config.CORRUPTED_FOLDER / audio_file.name))
            continue

        # Render spectral signature diagnostics
        spectrum_img = AudioProcessor.generate_spectrogram(audio_file)

        # Parse local internal audio container streams
        meta = AudioProcessor.extract_metadata(audio_file)

        # Fallback API check: If local genre is missing BUT Artist & Title exist, fetch via MusicBrainz
        has_new_metadata_to_write = False
        if not meta["Genre"] and meta["Artist"] and meta["Title"]:
            fetched_genre = MusicBrainzService.fetch_genre(meta["Artist"], meta["Title"])
            if fetched_genre:
                meta["Genre"] = fetched_genre
                has_new_metadata_to_write = True

        # Write metadata tags back directly into the file if updated
        if has_new_metadata_to_write:
            AudioProcessor.write_metadata_to_file(audio_file, meta["Genre"])

        # Build the track dict that BitrateActionHandler and the DB both consume
        track_data = {
            "FileName":     audio_file.stem,
            "FilePath":     file_path_str,
            "Artist":       meta["Artist"],
            "Title":        meta["Title"],
            "Genre":        meta["Genre"],
            "Bitrate_kbps": meta["Bitrate_kbps"],
            "SampleRate_Hz":meta["SampleRate_Hz"],
            "Channels":     meta["Channels"],
            "Duration":     meta["Duration"],
            "SpectrumPath": str(spectrum_img),
        }

        # --- Apply bitrate rule from Preferences ---
        # Returns a status string ("Trash", "Low-Quality", "Wishlist") if a rule fired,
        # or None if the track is within acceptable quality and should be reviewed manually.
        auto_status = BitrateActionHandler.apply(track_data, prefs, db)

        # Tracks that pass the bitrate check are queued for manual review (Status = None / pending)
        # Tracks that triggered an auto-action receive the corresponding status label.
        track_data["Status"] = auto_status  # None = pending in the Flask queue

        # Save record to report database
        db.insert_qc_track(track_data)
        new_tracks_counter += 1

    print("\n" + "=" * 80)
    print(f"Batch Analysis Completed! Registered {new_tracks_counter} new track(s) in the DB.")
    print("=" * 80)


if __name__ == "__main__":
    main()
