"""
================================================================================
BatchQC - Advanced Flask Control Center (Unified DB Edition)
================================================================================
Description:
    An asynchronous, scalable web application designed to act as a manual
    Curation and Quality Control (QC) layer for batch audio assets.
    Integrates a non-destructive multi-step rollback (Undo) buffer.
    Supports a persistent user Preferences system with configurable
    bitrate-based auto-actions and dynamic user-defined keyboard hotkeys.

Project Directory Structure (BatchQC):
    - BatchQC/app.py                     (This script)
    - BatchQC/database/track-repository.db (Unified storage for QC and Wishlist)
"""

import os
import sqlite3
import subprocess
import threading
import shutil
from datetime import date, datetime
from pathlib import Path
from collections import deque
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, flash

app = Flask(__name__)
# Required for flash messages and session state signature verification
app.secret_key = "super_secret_session_key_for_flash_messages"


# ==============================================================================
# 1. GLOBAL ENVIRONMENT & PATH CONFIGURATION
# ==============================================================================
class WebConfig:
    """
    Centralized configuration engine mapping application directories,
    external processing scripts, and shared multithreading variables.
    """
    PROJECT_ROOT = Path(__file__).resolve().parent
    DATABASE_FOLDER = PROJECT_ROOT / "database"

    # UNIFIED DATABASE: Both 'qc_report' and 'wishlist' tables reside here
    DB_FILE = DATABASE_FOLDER / "track-repository.db"
    PREFERENCES_DB_FILE = DATABASE_FOLDER / "preferences.db"

    # Background task parameters
    ANALYZER_SCRIPT = PROJECT_ROOT / "scripts" / "analyze-tracks.py"
    TRANSFORM_SCRIPT = PROJECT_ROOT / "scripts" / "transform-tracks.py"
    QC_FOLDER = PROJECT_ROOT / "output" / "spectrograms"

    # Destination directories for asset curation states
    GOOD_FOLDER = PROJECT_ROOT / "output" / "good-quality"
    TRASH_FOLDER = PROJECT_ROOT / "output" / "trash"
    LOW_QUALITY_FOLDER = PROJECT_ROOT / "output" / "low-quality"

    # Asynchronous engine operational variables (Audio Analysis)
    is_analysis_running = False
    analysis_lock = threading.Lock()

    # Asynchronous engine operational variables (Track Transformation/Export)
    is_transform_running = False
    transform_lock = threading.Lock()


# System fallback configurations including ergonomic default hotkeys
DEFAULT_PREFERENCES = {
    "bitrate_threshold": 160,
    "action_trash": False,
    "action_wishlist": False,
    "action_low_quality": False,
    # Ergonomic primary keys mapping for single-handed workflow acceleration
    "hk_ok": "1",
    "hk_trash_wishlist": "2",
    "hk_trash_only": "3",
    "hk_low_quality": "4",
    "hk_skip": "s"
}

# Bootstrap all required storage directories on startup
for _folder in [
    WebConfig.DATABASE_FOLDER,
    WebConfig.QC_FOLDER,
    WebConfig.GOOD_FOLDER,
    WebConfig.TRASH_FOLDER,
    WebConfig.LOW_QUALITY_FOLDER,
]:
    os.makedirs(_folder, exist_ok=True)


# ==============================================================================
# 2. PREFERENCES KERNEL SERVICE (With Hotkey Capability)
# ==============================================================================
class PreferencesService:
    """
    Handles persistence layer operations for user configuration states.
    Utilizes key-value schema inside a dedicated preferences database.
    """

    @staticmethod
    def _init_table():
        """Ensures the directory and configuration table exist before any DB operation."""
        os.makedirs(WebConfig.PREFERENCES_DB_FILE.parent, exist_ok=True)

        with sqlite3.connect(WebConfig.PREFERENCES_DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_preferences (
                    pref_key TEXT PRIMARY KEY,
                    pref_value TEXT
                )
            """)
            conn.commit()

    @staticmethod
    def load():
        """Extracts configuration parameters or returns system defaults if file is fresh."""
        PreferencesService._init_table()
        prefs = DEFAULT_PREFERENCES.copy()

        with sqlite3.connect(WebConfig.PREFERENCES_DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT pref_key, pref_value FROM system_preferences")
            rows = cursor.fetchall()

            for row in rows:
                key, val = row[0], row[1]
                if key == "bitrate_threshold":
                    prefs[key] = int(val)
                elif key in ["action_trash", "action_wishlist", "action_low_quality"]:
                    prefs[key] = val == "True"
                elif key.startswith("hk_"):
                    prefs[key] = str(val)
        return prefs

    @staticmethod
    def save(prefs_dict):
        """Commits and forces a physical disk sync of the updated preference state."""
        PreferencesService._init_table()
        with sqlite3.connect(WebConfig.PREFERENCES_DB_FILE) as conn:
            cursor = conn.cursor()
            for key, val in prefs_dict.items():
                cursor.execute("""
                    INSERT INTO system_preferences (pref_key, pref_value)
                    VALUES (?, ?)
                    ON CONFLICT(pref_key) DO UPDATE SET pref_value = excluded.pref_value
                """, (key, str(val)))

            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL);")

    @staticmethod
    def reset():
        """Purges custom configuration records to restore default initialization state."""
        PreferencesService._init_table()
        with sqlite3.connect(WebConfig.PREFERENCES_DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM system_preferences")
            conn.commit()


# ==============================================================================
# 3. STATE MANAGEMENT & ROLLBACK MEMORY LAYER
# ==============================================================================
class ActionHistory:
    """
    Manages an in-memory execution stack storing the history of evaluation decisions.
    Utilizes a fixed-capacity deque to strictly limit resource bounds.
    """
    _history = deque(maxlen=5)

    @classmethod
    def push(cls, action_type, track_id, original_path, moved_to_path=None):
        """Appends a structured log transaction record to the history matrix tracker."""
        cls._history.append({
            "type": action_type,
            "id": track_id,
            "src": original_path,
            "dst": moved_to_path
        })

    @classmethod
    def pop(cls):
        """Extracts and returns the latest transaction ledger entry."""
        try:
            return cls._history.pop()
        except IndexError:
            return None

    @classmethod
    def get_count(cls):
        """Returns the current size of the volatile rollback index matrix."""
        return len(cls._history)


# ==============================================================================
# 4. RELATIONAL DATA ACCESS LAYER (UNIFIED STORAGE INTERFACES)
# ==============================================================================
class DatabaseService:
    """
    Handles connections and models mapped into the target persistence database.
    Manages both 'qc_report' and 'wishlist' tables within a single SQLite file context.
    """

    @staticmethod
    def _get_connection():
        """Establishes an atomic connection instance to the unified SQLite file."""
        return sqlite3.connect(WebConfig.DB_FILE)

    @classmethod
    def initialize_schemas(cls):
        """Ensures both production tables exist inside the single target database file."""
        WebConfig.DATABASE_FOLDER.mkdir(parents=True, exist_ok=True)
        with cls._get_connection() as conn:
            # Table A: Manual Curation Quality Control registry
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
            # Table B: Wishlist tracking data system repository (Moved here from wishlist.db)
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

    @classmethod
    def fetch_pending_tracks(cls):
        """Queries and returns unverified track data rows waiting for appraisal selection."""
        cls.initialize_schemas()
        if not WebConfig.DB_FILE.exists():
            return []

        with cls._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM qc_report "
                "WHERE Status IS NULL OR Status = '' OR Status = 'Skipped' "
                "ORDER BY id ASC"
            )
            return [dict(row) for row in cursor.fetchall()]

    @classmethod
    def update_track_status(cls, track_id: int, status_string: str):
        """Directly updates the structural curation status marker for the mapped ID."""
        with cls._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE qc_report SET Status = ? WHERE id = ?",
                (status_string, track_id)
            )
            conn.commit()

    @classmethod
    def insert_wishlist_track(cls, artist: str, title: str, genre: str, file_name: str):
        """Generically injects a low-quality target item into the wishlist storage table."""
        cls.initialize_schemas()
        insert_query = '''
            INSERT OR IGNORE INTO wishlist (Artist, Title, Genre, FileName, DateAdded)
            VALUES (?, ?, ?, ?, ?)
        '''
        with cls._get_connection() as conn:
            conn.execute(insert_query, (
                artist,
                title,
                genre,
                file_name,
                str(date.today())
            ))
            conn.commit()


# ==============================================================================
# 5. STORAGE & MAINTENANCE UTILITY SERVICE
# ==============================================================================
class CurationStorageService:
    """
    Provides isolated file-system operations for application directories.
    Designed generically to handle file moves, routing conflicts and cleanups.
    """

    @staticmethod
    def purge_directory_contents(target_path: Path) -> int:
        """Unlinks all files and recursively deletes directories inside a given target path."""
        deleted_count = 0
        if not target_path.exists() or not target_path.is_dir():
            return deleted_count

        for item in target_path.iterdir():
            try:
                if item.is_file() or item.is_symlink():
                    item.unlink()
                    deleted_count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    deleted_count += 1
            except Exception as e:
                print(f"[StorageService] Failed to purge node asset {item}: {e}")

        return deleted_count

    @staticmethod
    def move_file_safely(source_path: str, target_directory: Path) -> str:
        """Moves a file safely while auto-resolving filename duplicates using unique timestamps."""
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"Source audio track not found at: {source_path}")

        file_name = os.path.basename(source_path)
        destination_path = target_directory / file_name

        if destination_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_name = f"{timestamp}_{file_name}"
            destination_path = target_directory / file_name

        shutil.move(source_path, str(destination_path))
        return str(destination_path)


# ==============================================================================
# 6. ASYNCHRONOUS ENGINE WORKERS (Decoupled Background Tasks)
# ==============================================================================
def run_analysis_worker():
    """Runs the backend parsing script within a decoupled, non-blocking OS context."""
    try:
        subprocess.run(["python", str(WebConfig.ANALYZER_SCRIPT)], check=True)
    except Exception as e:
        print(f"[Flask-Core] Background analysis script exception: {e}")
    finally:
        with WebConfig.analysis_lock:
            WebConfig.is_analysis_running = False


def run_transform_worker():
    """Runs the backend track formatting script within a decoupled, non-blocking OS context."""
    try:
        subprocess.run(["python", str(WebConfig.TRANSFORM_SCRIPT)], check=True)
    except Exception as e:
        print(f"[Flask-Core] Background transformation script exception: {e}")
    finally:
        with WebConfig.transform_lock:
            WebConfig.is_transform_running = False


# ==============================================================================
# 7. CONTROLLER ACTION ROUTINGS
# ==============================================================================

@app.route('/')
def index():
    """Renders the core curation queue interface."""
    pending_tracks = DatabaseService.fetch_pending_tracks()
    remaining_count = len(pending_tracks)

    current_track = pending_tracks[0] if remaining_count > 0 else None
    spectrum_file = None

    if current_track:
        spectrum_path_str = current_track.get('SpectrumPath', "")
        if spectrum_path_str:
            spectrum_file = os.path.basename(spectrum_path_str)

    current_prefs = PreferencesService.load()

    return render_template(
        'dashboard.html',
        info=current_track,
        spectrum_file=spectrum_file,
        remaining=remaining_count,
        is_running=WebConfig.is_analysis_running,
        is_transform_running=WebConfig.is_transform_running,
        undo_count=ActionHistory.get_count(),
        prefs=current_prefs
    )


@app.route('/action', methods=['POST'])
def action():
    """Orchestrates manual curation asset routing targets via user selection matrix."""
    action_type = request.form.get('action')
    track_id = int(request.form.get('track_id'))

    with sqlite3.connect(WebConfig.DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        track = dict(conn.execute("SELECT * FROM qc_report WHERE id = ?", (track_id,)).fetchone())

    music_path = track['FilePath']
    filename = os.path.basename(music_path)

    try:
        if action_type == "ok":
            dst = CurationStorageService.move_file_safely(music_path, WebConfig.GOOD_FOLDER)
            ActionHistory.push("ok", track_id, music_path, dst)
            DatabaseService.update_track_status(track_id, "OK")
            flash(f"Approved: '{filename}' saved to Good Quality folder.", "success")

        elif action_type == "trash_wishlist":
            DatabaseService.insert_wishlist_track(
                artist=track.get("Artist", "Unknown"),
                title=track.get("Title", "Unknown"),
                genre=track.get("Genre", "Unknown"),
                file_name=filename
            )
            dst = CurationStorageService.move_file_safely(music_path, WebConfig.TRASH_FOLDER)
            ActionHistory.push("trash_wishlist", track_id, music_path, dst)
            DatabaseService.update_track_status(track_id, "Trash_Wishlist")
            flash(f"Moved to Trash & logged to Wishlist: '{filename}'", "success")

        elif action_type == "trash_only":
            dst = CurationStorageService.move_file_safely(music_path, WebConfig.TRASH_FOLDER)
            ActionHistory.push("trash_only", track_id, music_path, dst)
            DatabaseService.update_track_status(track_id, "Trash_Only")
            flash(f"Rejected: '{filename}' moved to Trash.", "success")

        elif action_type == "low_quality":
            dst = CurationStorageService.move_file_safely(music_path, WebConfig.LOW_QUALITY_FOLDER)
            ActionHistory.push("low_quality", track_id, music_path, dst)
            DatabaseService.update_track_status(track_id, "Low_Quality")
            flash(f"Downgraded: '{filename}' moved to Low-Quality archive.", "success")

        elif action_type == "skip":
            ActionHistory.push("skip", track_id, music_path)
            DatabaseService.update_track_status(track_id, "Skipped")
            flash(f"Skipped track: '{filename}'", "success")

        else:
            flash(f"Unknown curation type code configuration: '{action_type}'", "danger")

    except Exception as e:
        flash(f"Critical execution error managing track asset: {str(e)}", "danger")

    return redirect(url_for('index'))


@app.route('/undo', methods=['POST'])
def undo():
    """Extracts the latest operational event to roll back structural changes."""
    last_action = ActionHistory.pop()

    if not last_action:
        flash("No operational actions available inside rollback memory buffer.", "warning")
        return redirect(url_for('index'))

    if last_action['type'] in ['ok', 'trash_wishlist', 'trash_only', 'low_quality']:
        if os.path.exists(last_action['dst']):
            shutil.move(last_action['dst'], last_action['src'])

    DatabaseService.update_track_status(last_action['id'], None)
    flash("Last curation step successfully rolled back.", "success")
    return redirect(url_for('index'))


@app.route('/start-analysis', methods=['POST'])
def start_analysis():
    """Initializes the asynchronous audio analysis engine thread under safety locks."""
    with WebConfig.analysis_lock:
        if WebConfig.is_analysis_running:
            return jsonify({"status": "error", "message": "Analysis backend worker thread locked."}), 429

        WebConfig.is_analysis_running = True
        worker = threading.Thread(target=run_analysis_worker)
        worker.start()

    return redirect(url_for('index'))


@app.route('/start-transform', methods=['POST'])
def start_transform():
    """Initializes the asynchronous tracks transformation engine thread under safety locks."""
    with WebConfig.transform_lock:
        if WebConfig.is_transform_running:
            return jsonify({"status": "error", "message": "Transformation worker thread locked."}), 429

        WebConfig.is_transform_running = True
        worker = threading.Thread(target=run_transform_worker)
        worker.start()

    flash("Track transformation engine started in the background.", "success")
    return redirect(url_for('index'))


@app.route('/api/status')
def get_status():
    """Polling interface monitoring context status indicators for analysis pipeline."""
    return jsonify({"is_running": WebConfig.is_analysis_running})


@app.route('/api/transform-status')
def get_transform_status():
    """Polling interface monitoring status indicators for transformation pipeline."""
    return jsonify({"is_transform_running": WebConfig.is_transform_running})


@app.route('/spectrum/<filename>')
def spectrum(filename):
    """Serves visualization spectrogram assets directly from storage paths."""
    return send_from_directory(str(WebConfig.QC_FOLDER), filename)


# ==============================================================================
# 8. MAINTENANCE CONTROLLER
# ==============================================================================

@app.route('/trash/empty', methods=['POST'])
def empty_trash():
    """Triggers physical unlinking routine targeting the configured Trash folder."""
    cleared_files = CurationStorageService.purge_directory_contents(WebConfig.TRASH_FOLDER)
    flash(f"Successfully emptied trash bin. Removed {cleared_files} files.", "success")
    return redirect(url_for('index'))


# ==============================================================================
# 9. PREFERENCES CONTROLLER (With Hotkey Parsing Matrix)
# ==============================================================================

@app.route('/preferences/save', methods=['POST'])
def preferences_save():
    """Parses payload configuration updates and generic hotkey binds from modal."""
    updated_prefs = {
        "bitrate_threshold": int(request.form.get("bitrate_threshold", DEFAULT_PREFERENCES["bitrate_threshold"])),
        "action_trash": "action_trash" in request.form,
        "action_wishlist": "action_wishlist" in request.form,
        "action_low_quality": "action_low_quality" in request.form,

        # Generic hotkey processing extraction layer. Sanitized to absolute lowercase entries.
        "hk_ok": request.form.get("hk_ok", DEFAULT_PREFERENCES["hk_ok"]).strip().lower()[:1],
        "hk_trash_wishlist": request.form.get("hk_trash_wishlist",
                                              DEFAULT_PREFERENCES["hk_trash_wishlist"]).strip().lower()[:1],
        "hk_trash_only": request.form.get("hk_trash_only", DEFAULT_PREFERENCES["hk_trash_only"]).strip().lower()[:1],
        "hk_low_quality": request.form.get("hk_low_quality", DEFAULT_PREFERENCES["hk_low_quality"]).strip().lower()[:1],
        "hk_skip": request.form.get("hk_skip", DEFAULT_PREFERENCES["hk_skip"]).strip().lower()[:1]
    }
    PreferencesService.save(updated_prefs)
    flash("System settings and key-bind layout modified successfully.", "success")
    return redirect(url_for('index'))


@app.route('/preferences/reset', methods=['POST'])
def preferences_reset():
    """Wipes active customized state records to re-initialize system defaults."""
    PreferencesService.reset()
    flash("Factory settings and hotkey configurations restored.", "success")
    return redirect(url_for('index'))


# ==============================================================================
# 10. PREFERENCES REST API
# ==============================================================================

@app.route('/api/preferences', methods=['GET'])
def api_get_preferences():
    """Exposes serialized state vectors of configuration preferences."""
    return jsonify(PreferencesService.load())


if __name__ == '__main__':
    # Initialize unified database scheme layouts cleanly on server launch bindings
    DatabaseService.initialize_schemas()
    app.run(debug=True, port=5000)