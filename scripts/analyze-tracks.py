"""
================================================================================
Music Quality Check - Modular, Scalable & In-File Tagging Edition
================================================================================
Description:
Audio file analysis tool for quality control and metadata collection.
- Detects corrupted files.
- Generates high-resolution spectrograms.
- Analyzes spectrogram images programmatically for frequency cutoffs.
- Extracts technical metadata and audio tags via FFmpeg.
- Fallback: Fetches missing genres via MusicBrainz API.
- Automatically writes fetched/missing tags back INTO the actual audio files.
- Applies unified automation rules based on user Preferences from the Flask API.
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
import argparse
from datetime import date
from pathlib import Path

# External library required for programmatic image analysis
# Install via: pip install Pillow
from PIL import Image


# ==============================================================================
# 1. CENTRAL SCRIPT CONFIGURATION (Aligned with Directory Structure)
# ==============================================================================
class Config:
    """
    Holds all global paths, folder configurations, and application constants.
    Centralizing this makes it easy to maintain or expand the project later.
    """
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    INPUT_FOLDER = PROJECT_ROOT / "input"
    OUTPUT_BASE = PROJECT_ROOT / "output"

    CORRUPTED_FOLDER = OUTPUT_BASE / "corrupted"
    SPECTROGRAMS_FOLDER = OUTPUT_BASE / "spectrograms"
    TRASH_FOLDER = OUTPUT_BASE / "trash"
    LOW_QUALITY_FOLDER = OUTPUT_BASE / "low-quality"
    GOOD_FOLDER = OUTPUT_BASE / "good-quality"

    DATABASE_FOLDER = PROJECT_ROOT / "database"
    DB_FILE = DATABASE_FOLDER / "track-repository.db"

    FLASK_BASE_URL = "http://127.0.0.1:5000"
    SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".mp4", ".aif", ".aiff"}
    USER_AGENT = 'BatchQCAudioAnalyzer/4.0 (contact-via-github-or-email)'


# ==============================================================================
# 2. DATABASE MANAGER (Encapsulates all SQL Layer Operations)
# ==============================================================================
class DatabaseManager:
    """
    Manages all SQLite database connections, schema definitions, and queries
    for both Quality Control reports and the Wishlist repository.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._initialize_database_schema()

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _initialize_database_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            # UNIQUE constraint is removed from FilePath to allow the same path
            # to exist multiple times if the file size (quality) changes.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS qc_report (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    FileName TEXT,
                    FilePath TEXT,
                    FileSize_Bytes INTEGER,
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

    def get_processed_file_paths(self) -> set:
        """
        Fetches unique combinations of FilePath and FileSize_Bytes to determine
        if a specific version of a file has already been analyzed.
        """
        query_string = "SELECT FilePath, FileSize_Bytes FROM qc_report"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query_string)
            # Returns a set of tuples: { (path1, size1), (path2, size2), ... }
            return {(row[0], row[1]) for row in cursor.fetchall()}

    def insert_qc_track(self, track_metadata: dict):
        """
        Inserts a completed track quality report into the local database ledger.
        """
        insert_query = '''
            INSERT INTO qc_report (
                FileName, FilePath, FileSize_Bytes, Artist, Title, Genre,
                Bitrate_kbps, SampleRate_Hz, Channels, Duration, SpectrumPath, Status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        with self._get_connection() as conn:
            conn.execute(insert_query, (
                track_metadata["FileName"],
                track_metadata["FilePath"],
                track_metadata["FileSize_Bytes"],  # Injected dynamic size verification step
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

    def insert_wishlist_track(self, track_metadata: dict):
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

    def get_cleanup_spectrogram_paths(self) -> list:
        """
        Fetches all spectrogram paths for tracks that have already been finalized
        (either automatically or manually approved/rejected).
        """
        # We select paths where Status is not NULL and not an empty string
        query_string = "SELECT SpectrumPath FROM qc_report WHERE Status IS NOT NULL AND Status != ''"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query_string)
            return [row[0] for row in cursor.fetchall() if row[0]]

    def get_all_wishlist_items(self) -> list:
        """
        Retrieves all entries from the wishlist table sorted by the date they were added.
        """
        query_string = "SELECT id, Artist, Title, Genre, FileName, DateAdded FROM wishlist ORDER BY id DESC"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query_string)
            # Fetch all as dictionaries for easier rendering in Flask templates
            return [
                {
                    "id": row[0],
                    "Artist": row[1],
                    "Title": row[2],
                    "Genre": row[3],
                    "FileName": row[4],
                    "DateAdded": row[5]
                }
                for row in cursor.fetchall()
            ]

    def add_manual_wishlist_item(self, artist: str, title: str, genre: str = "") -> bool:
        """
        Manually injects a new track request into the wishlist table.
        """
        insert_query = '''
            INSERT OR IGNORE INTO wishlist (Artist, Title, Genre, FileName, DateAdded)
            VALUES (?, ?, ?, ?, date('now'))
        '''
        with self._get_connection() as conn:
            cursor = conn.execute(insert_query, (artist.strip(), title.strip(), genre.strip(), "Manually Added"))
            conn.commit()
            return cursor.rowcount > 0

    def delete_wishlist_item(self, item_id: int):
        """
        Permanently drops a specific wishlist entry by its primary key ID.
        """
        delete_query = "DELETE FROM wishlist WHERE id = ?"
        with self._get_connection() as conn:
            conn.execute(delete_query, (item_id,))
            conn.commit()


# ==============================================================================
# 3. PREFERENCES LOADER
# ==============================================================================
class PreferencesLoader:
    """
    Fetches the user's saved Preferences from the running Flask app's JSON API.
    """
    FALLBACK = {
        "bitrate_threshold": 160,
        "auto_action_mode": "none",  # Options: "none", "low_quality_wishlist", "trash_wishlist", "trash_only"
        "low_quality_folder": "output/low-quality"
    }

    @classmethod
    def load(cls) -> dict:
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
# 4. AUDIO SPECTRUM ANALYZER (Programmatic Image Scan Engine)
# ==============================================================================
class AudioSpectrumAnalyzer:
    """
    Analyzes generated spectrogram images to determine the frequency presence
    and energy density above a specific vertical threshold (the 19kHz green line).
    """

    @classmethod
    def check_high_frequency_density(cls, image_path: Path, cutoff_percentage: float = 13.8) -> float:
        """
        Scans the upper band of a NAKED spectrogram image (no legends/gridlines)
        to calculate the percentage of the track's duration containing high frequencies.
        """
        if not image_path.exists():
            return 1.0

        try:
            with Image.open(image_path) as img:
                rgb_img = img.convert("RGB")
                width, height = rgb_img.size

                # Calculate the exact pixel row for 19kHz in a 22.05kHz spectrum without legends
                max_y_pixel = int(height * (cutoff_percentage / 100.0))

                active_columns_count = 0

                # Scan the entire width since there are no borders anymore
                for x in range(0, width):
                    # Scan from the very top (Y=0) down to the 19kHz mark
                    for y in range(0, max_y_pixel):
                        r, g, b = rgb_img.getpixel((x, y))

                        # Strict color threshold: Pure black/dark blue background in FFmpeg is very low.
                        # Real audio signal lights up brightly.
                        if r > 40 or g > 40 or b > 40:
                            active_columns_count += 1
                            break  # Energy confirmed, move to next X column

                density_ratio = (active_columns_count / width) * 100.0
                print(f"   -> [SpectrumScan] Dynamic High-Frequency Density calculated: {density_ratio:.2f}%")
                return density_ratio

        except Exception as e:
            print(f"   -> [SpectrumScan-Error] Failed to process image matrix array: {e}")
            return 1.0


# ==============================================================================
# 5. AUTOMATED PIPELINE ACTION HANDLER
# ==============================================================================
class AutomatedActionHandler:
    """
    Executes automated routing and wishlist logging based on unified workflow rules.
    Optimized to minimize processor workload by chaining criteria execution sequence.
    """

    @staticmethod
    def _move_file_to_destination(source_path: str, destination_folder: Path) -> str:
        destination_folder.mkdir(parents=True, exist_ok=True)
        target_path = str(destination_folder / os.path.basename(source_path))
        if os.path.exists(source_path):
            shutil.move(source_path, target_path)
        return target_path

    @classmethod
    def process_rules(cls, track: dict, prefs: dict, db_manager: DatabaseManager) -> str | None:
        """
        Executes automated quality control criteria check:
        - If bitrate is lower than threshold, trigger system preferences auto-action.
        - Calculate high-frequency density above 19kHz line.
        - If density >= 50.0%, automatically approve and route to 'good-quality'.
        - If density == 0.0%, trigger system preferences auto-action.
        - Otherwise (0.0% < density < 50.0%), leave Status as NULL for cautious manual inspection.
        """
        threshold_bitrate = int(prefs.get("bitrate_threshold", 160))
        track_bitrate = int(track.get("Bitrate_kbps") or 0)
        action_mode = prefs.get("auto_action_mode", "none")

        # --- CRITERIA STEP 1: Bitrate Threshold Verification ---
        if track_bitrate > 0 and track_bitrate < threshold_bitrate:
            print(
                f"   -> [Automation Triggered] '{track.get('FileName')}' failed Bitrate criteria ({track_bitrate} kbps < {threshold_bitrate} kbps)")
            return cls._execute_preference_action(track, action_mode, prefs, db_manager, "Low_Quality_Bitrate")

        # --- CRITERIA STEP 2: Spectrogram High-Frequency Density Scan ---
        spectrum_file = Path(track["SpectrumPath"])
        density_ratio = AudioSpectrumAnalyzer.check_high_frequency_density(spectrum_file)

        # CASE A: Genuine high-quality track crossing the visual line for more than half the runtime
        if density_ratio >= 50.0:
            cls._move_file_to_destination(track["FilePath"], Config.GOOD_FOLDER)
            print(
                f"   -> [Action Log] Auto-Approved! High quality confirmed ({density_ratio:.2f}%). Moved to Good folder.")
            return "OK"

        # CASE B: Completely dead zone above the 19kHz boundary (No high frequencies at all)
        elif density_ratio == 0.0:
            print(
                f"   -> [Automation Triggered] '{track.get('FileName')}' failed Spectrogram criteria (0.0% energy above visual guide)")
            return cls._execute_preference_action(track, action_mode, prefs, db_manager, "Dead_Spectrum")

        # CASE C: Cautious Middle-Ground (Has high frequencies but less than half the runtime)
        else:
            print(
                f"   -> [Action Log] Retained for Manual Review. Track has selective high-frequency presence ({density_ratio:.2f}%).")
            return None

    @classmethod
    def _execute_preference_action(cls, track: dict, action_mode: str, prefs: dict, db_manager: DatabaseManager,
                                   fallback_status: str) -> str | None:
        """
        Executes specific file handling and tracking routines mapped out in application settings.
        """
        if action_mode == "none":
            return None

        if action_mode == "low_quality_wishlist":
            db_manager.insert_wishlist_track(track)

            # Resolve dynamic target directory path cleanly from custom string config or default
            custom_folder_str = prefs.get("low_quality_folder", "output/low-quality")
            custom_folder_path = Path(custom_folder_str)
            if not custom_folder_path.is_absolute():
                custom_folder_path = Config.PROJECT_ROOT / custom_folder_path

            cls._move_file_to_destination(track["FilePath"], custom_folder_path)
            print(f"   -> [Action Log] Logged to Wishlist & moved to Low-Quality archive ({custom_folder_path}).")
            return "Low_Quality"

        elif action_mode == "trash_wishlist":
            db_manager.insert_wishlist_track(track)
            cls._move_file_to_destination(track["FilePath"], Config.TRASH_FOLDER)
            print(f"   -> [Action Log] Logged to Wishlist & isolated to Trash.")
            return "Trash_Wishlist"

        elif action_mode == "trash_only":
            cls._move_file_to_destination(track["FilePath"], Config.TRASH_FOLDER)
            print(f"   -> [Action Log] Isolated to Trash directly without Wishlist entry.")
            return "Trash_Only"

        return None


# ==============================================================================
# 6. EXTERNAL API SERVICES (MusicBrainz)
# ==============================================================================
class MusicBrainzService:
    @staticmethod
    def fetch_genre(artist: str, title: str) -> str:
        if not artist or not title:
            return ""
        try:
            query = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
            url = f"https://musicbrainz.org/ws/2/recording/?query={query}&fmt=json"
            req = urllib.request.Request(url, headers={'User-Agent': Config.USER_AGENT})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                if data.get("recordings"):
                    best_match = data["recordings"][0]
                    if "tags" in best_match:
                        sorted_tags = sorted(best_match["tags"], key=lambda x: x.get("count", 0), reverse=True)
                        if sorted_tags:
                            return sorted_tags[0]["name"].title()
        except Exception as e:
            print(f"   -> [API-Warning] MusicBrainz metadata retrieval bypassed: {e}")
        return ""


# ==============================================================================
# 7. AUDIO PROCESSOR (FFmpeg Engine Wrapper)
# ==============================================================================
class AudioProcessor:
    @staticmethod
    def _run_ffmpeg(command: list) -> str:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="ignore")
        return result.stdout

    @classmethod
    def check_is_corrupted(cls, file_path: Path) -> bool:
        command = ["ffmpeg", "-v", "error", "-i", str(file_path), "-f", "null", "-"]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode != 0

    @classmethod
    def generate_spectrogram(cls, audio_file: Path) -> Path:
        output_file = Config.SPECTROGRAMS_FOLDER / f"{audio_file.stem}-spectrogram.png"
        # CRITICAL: legend=0 removes all gridlines, borders, text, and axes from the image.
        command = ["ffmpeg", "-y", "-i", str(audio_file), "-lavfi", "showspectrumpic=s=1920x1080:legend=0", "-update",
                   "1",
                   str(output_file)]
        cls._run_ffmpeg(command)
        return output_file

    @classmethod
    def extract_metadata(cls, audio_file: Path) -> dict:
        command = ["ffmpeg", "-i", str(audio_file)]
        output = cls._run_ffmpeg(command)
        info = {"Bitrate_kbps": 0, "SampleRate_Hz": 0, "Channels": 0, "Duration": "Unknown", "Artist": None,
                "Title": None, "Genre": None}

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
        if not genre: return
        temp_file = audio_file.parent / f"temp_{audio_file.name}"
        command = ["ffmpeg", "-y", "-i", str(audio_file), "-metadata", f"genre={genre}", "-c:a", "copy", str(temp_file)]
        cls._run_ffmpeg(command)
        if temp_file.exists() and temp_file.stat().st_size > 0:
            os.replace(str(temp_file), str(audio_file))
        else:
            if temp_file.exists(): os.remove(str(temp_file))


# ==============================================================================
# 8. CORE EXECUTION PIPELINE
# ==============================================================================
def main():
    """
    Main execution pipeline for the BatchQC audio analysis system.
    Handles environmental checks, command-line arguments fallback vector overrides,
    folder initialization, database cleanup, metadata extraction, and rule automation.
    """
    # --- CRITICAL: Environmental Dependency Verification ---
    if not shutil.which("ffmpeg"):
        print("\n" + "!" * 80)
        print("[CRITICAL ERROR] FFmpeg was not found on your system!")
        print("This framework requires FFmpeg to extract audio data and build spectrograms.")
        print("Please install FFmpeg and append its 'bin' directory to your system's PATH.")
        print("Refer to the project's README.md for a detailed step-by-step setup guide.")
        print("!" * 80 + "\n")
        return

    # ==========================================================================
    # --- COMMAND-LINE ARGUMENT INTERCEPTOR LAYER ---
    # ==========================================================================
    parser = argparse.ArgumentParser(description="BatchQC Automated Staging Audio Quality Analyzer Engine")
    parser.add_argument("--bitrate", type=int, help="Override quality boundary threshold (kbps)")
    parser.add_argument("--mode", type=str, choices=["none", "low_quality_wishlist", "trash_wishlist", "trash_only"],
                        help="Override automated ingestion workflow routing target selection mode")
    parser.add_argument("--folder", type=str, help="Override archive destination folder path for low quality assets")
    args = parser.parse_args()

    # Base setup initialization
    prefs = PreferencesLoader.load()

    # Apply argument modifications if passed via shell execution arguments explicitly
    if args.bitrate is not None:
        print(f"[CLI Override] Updating bitrate threshold: {args.bitrate} kbps")
        prefs["bitrate_threshold"] = args.bitrate
    if args.mode is not None:
        print(f"[CLI Override] Updating auto action mode: {args.mode}")
        prefs["auto_action_mode"] = args.mode
    if args.folder is not None:
        print(f"[CLI Override] Updating low-quality folder: {args.folder}")
        prefs["low_quality_folder"] = args.folder

    # --- Directory Structure Initialization ---
    for folder in [Config.INPUT_FOLDER, Config.SPECTROGRAMS_FOLDER, Config.CORRUPTED_FOLDER,
                   Config.TRASH_FOLDER, Config.GOOD_FOLDER, Config.DATABASE_FOLDER]:
        folder.mkdir(parents=True, exist_ok=True)

    # Resolve dynamic target directory path cleanly to create low quality folder context if it doesn't exist
    custom_folder_str = prefs.get("low_quality_folder", "output/low-quality")
    custom_folder_path = Path(custom_folder_str)
    if not custom_folder_path.is_absolute():
        custom_folder_path = Config.PROJECT_ROOT / custom_folder_path
    custom_folder_path.mkdir(parents=True, exist_ok=True)

    db = DatabaseManager(Config.DB_FILE)

    print(f"Launching BatchQC Pipeline. Logging to: {Config.DB_FILE.name}")
    print("-" * 80)

    # ==========================================================================
    # --- AUTOMATED SPECTROGRAM CLEANUP ENGINE ---
    # ==========================================================================
    print("[Cleanup] Querying database for finalized track records...")
    cleanup_paths = db.get_cleanup_spectrogram_paths()
    deleted_images_counter = 0

    for path_str in cleanup_paths:
        img_path = Path(path_str)
        if img_path.exists():
            try:
                img_path.unlink()
                deleted_images_counter += 1
            except Exception as e:
                print(f"   -> [Cleanup-Warning] Failed to purge storage asset {img_path.name}: {e}")

    if deleted_images_counter > 0:
        print(
            f"   -> [Cleanup Success] Removed {deleted_images_counter} obsolete spectrogram image(s) from historical sessions.")
    else:
        print("   -> [Cleanup] Storage is optimized. No obsolete session assets found.")
    print("-" * 80)
    # ==========================================================================

    already_processed = db.get_processed_file_paths()
    audio_files = [f for f in Config.INPUT_FOLDER.rglob("*") if f.suffix.lower() in Config.SUPPORTED_EXTENSIONS]
    new_tracks_counter = 0

    # --- Main Input Inventory Traversal ---
    for audio_file in audio_files:
        file_path_str = str(audio_file.resolve())
        file_size_bytes = audio_file.stat().st_size

        if (file_path_str, file_size_bytes) in already_processed:
            continue

        print(f"\nProcessing: {audio_file.name}")

        if AudioProcessor.check_is_corrupted(audio_file):
            print("   -> [WARNING] File payload corrupted! Isolating track to 'Corrupted' folder.")
            shutil.move(str(audio_file), str(Config.CORRUPTED_FOLDER / audio_file.name))
            continue

        spectrum_img = AudioProcessor.generate_spectrogram(audio_file)
        meta = AudioProcessor.extract_metadata(audio_file)

        has_new_metadata_to_write = False
        if not meta["Genre"] and meta["Artist"] and meta["Title"]:
            fetched_genre = MusicBrainzService.fetch_genre(meta["Artist"], meta["Title"])
            if fetched_genre:
                meta["Genre"] = fetched_genre
                has_new_metadata_to_write = True

        if has_new_metadata_to_write:
            AudioProcessor.write_metadata_to_file(audio_file, meta["Genre"])

        track_data = {
            "FileName": audio_file.stem,
            "FilePath": file_path_str,
            "FileSize_Bytes": file_size_bytes,
            "Artist": meta["Artist"],
            "Title": meta["Title"],
            "Genre": meta["Genre"],
            "Bitrate_kbps": meta["Bitrate_kbps"],
            "SampleRate_Hz": meta["SampleRate_Hz"],
            "Channels": meta["Channels"],
            "Duration": meta["Duration"],
            "SpectrumPath": str(spectrum_img),
        }

        auto_status = AutomatedActionHandler.process_rules(track_data, prefs, db)
        track_data["Status"] = auto_status

        db.insert_qc_track(track_data)
        new_tracks_counter += 1

    print("\n" + "=" * 80)
    print(f"Batch Analysis Completed! Registered {new_tracks_counter} new track(s) in the DB.")
    print("=" * 80)


if __name__ == "__main__":
    main()