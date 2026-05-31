import os
import shutil
import subprocess
from pathlib import Path

# --- PFADE ---
# Stelle sicher, dass ffmpeg.exe im Projektordner liegt
FFMPEG_EXE = r"C:\Program Files\FFmpeg\ffmpeg-2025-12-22-git-c50e5c7778-essentials_build\bin\ffmpeg.exe"

PROJECT_FOLDER = Path(__file__).resolve().parent.parent
SOURCE_FOLDER = PROJECT_FOLDER / "output" / "good-quality"
TARGET_FOLDER = PROJECT_FOLDER / "output" / "good-quality-formatted"

def transform_audio():
    os.makedirs(TARGET_FOLDER, exist_ok=True)

    if not os.path.exists(SOURCE_FOLDER):
        print(f"Quellordner nicht gefunden: {SOURCE_FOLDER}")
        return

    for filename in os.listdir(SOURCE_FOLDER):
        source_path = os.path.join(SOURCE_FOLDER, filename)
        if not os.path.isfile(source_path):
            continue

        base_name, ext = os.path.splitext(filename)
        ext = ext.lower()

        # 1. MP3 und AIFF verschieben
        if ext in ['.mp3', '.aiff', '.aif']:
            print(f"Verschiebe: {filename}")
            shutil.move(source_path, os.path.join(TARGET_FOLDER, filename))

        # 2. Andere Formate konvertieren (16-bit AIFF)
        else:
            target_path = os.path.join(TARGET_FOLDER, base_name + ".aif")
            print(f"Konvertiere mit FFmpeg: {filename}...")
            
            # Direkter FFmpeg Aufruf: -sample_fmt s16 = 16 bit
            cmd = [
                FFMPEG_EXE, "-y", "-i", source_path,
                "-sample_fmt", "s16", "-ar", "44100", 
                target_path
            ]
            
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                os.remove(source_path)
                print(f"Erfolgreich: {base_name}.aif")
            except subprocess.CalledProcessError as e:
                print(f"Fehler bei {filename}: {e.stderr.decode()}")

if __name__ == "__main__":
    transform_audio()