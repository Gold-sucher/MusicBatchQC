from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from flask import send_file
import pandas as pd
import os
import shutil

app = Flask(__name__)

PROJECT_FOLDER = os.path.abspath(os.path.dirname(__file__))
QC_FOLDER = os.path.join(PROJECT_FOLDER, "QC_Output")
CSV_FILE = os.path.join(QC_FOLDER, "QC_Report.csv")
GOOD_FOLDER = os.path.join(PROJECT_FOLDER, "Good")
FIND_BETTER_CSV = os.path.join(PROJECT_FOLDER, "Find_Better_Quality.csv")

os.makedirs(GOOD_FOLDER, exist_ok=True)

# CSV sicher laden
if not os.path.exists(CSV_FILE):
    raise FileNotFoundError(f"QC_Report.csv nicht gefunden unter: {CSV_FILE}")

df = pd.read_csv(CSV_FILE, sep=';', encoding='utf-8')

# Status-Spalte sicherstellen
if 'Status' not in df.columns:
    df['Status'] = ""

# Status bereinigen (wichtig!)
df['Status'] = df['Status'].fillna("").astype(str).str.strip()

# Alle ungeprüften Tracks sammeln
pending_indices_list = df[df['Status'] == ""].index.tolist()

current_pending_pointer = 0

# ---- Session-Workflow ----
pending_indices_list = df[df['Status'].isna() | (df['Status'] == "")].index.tolist()
current_pending_pointer = 0


@app.route('/')
def index():
    global current_pending_pointer, pending_indices_list

    if not pending_indices_list:
        return "<h2>Alle Dateien geprüft!</h2>"

    current_index = pending_indices_list[current_pending_pointer]
    row = df.loc[current_index]
    spectrum_path = row.get('SpectrumPath', "")

    if pd.isna(spectrum_path) or spectrum_path == "":
    	spectrum_file = None
    else:
    	spectrum_file = os.path.basename(str(spectrum_path))

    info = row.to_dict()
    remaining = len(pending_indices_list)

    print("RAW PATH:", spectrum_path)
    print("FILENAME:", spectrum_file)

    if spectrum_file:
    	full_path = os.path.join(QC_FOLDER, spectrum_file)
    	print("FULL PATH:", full_path)
    	print("EXISTS:", os.path.exists(full_path))

    return render_template(
        'qc_view.html',
        spectrum_file=spectrum_file,
        info=info,
        remaining=remaining
    )
    

@app.route('/action', methods=['POST'])
def action():
    global current_pending_pointer, pending_indices_list

    action_type = request.form['action']

    if not pending_indices_list:
        return "<h2>Alle Dateien geprüft!</h2>"

    current_index = pending_indices_list[current_pending_pointer]
    row = df.loc[current_index]

    music_path = row.get('FilePath', "")
    spectrum_path = row.get('Spectrum', "")

    # Spektrogramm löschen bei OK oder Bad
    if action_type in ["ok", "bad"]:
        if pd.notna(spectrum_path) and spectrum_path != "" and os.path.exists(spectrum_path):
            os.remove(spectrum_path)

    # =======================
    # OK → verschieben
    # =======================
    if action_type == "ok":

        if os.path.exists(music_path):
            destination = os.path.join(GOOD_FOLDER, os.path.basename(music_path))

            try:
                shutil.move(music_path, destination)

            except Exception as e:
                source_dir = os.path.dirname(music_path)
                filename = os.path.basename(music_path)

                if os.path.exists(source_dir):
                    for file in os.listdir(source_dir):
                        cleaned_file = file.replace('?', '').replace('™', '').replace("'", "")
                        cleaned_name = filename.replace('?', '').replace('™', '').replace("'", "")

                        if cleaned_file == cleaned_name:
                            actual_path = os.path.join(source_dir, file)
                            shutil.move(actual_path, os.path.join(GOOD_FOLDER, file))
                            break
                    else:
                        return f"<h2>Fehler: Datei nicht gefunden - {str(e)}</h2>"
                else:
                    return f"<h2>Fehler beim Verschieben: {str(e)}</h2>"

        else:
            return f"<h2>Warnung: Datei nicht gefunden: {music_path}</h2>"

        df.at[current_index, 'Status'] = "OK"
        pending_indices_list.pop(current_pending_pointer)

    # =======================
    # BAD → löschen + CSV
    # =======================
    elif action_type == "bad":

        if os.path.exists(music_path):
            os.remove(music_path)

        df.at[current_index, 'Status'] = "Bad"

        columns_to_keep = [
            'FileName',
            'FilePath',
            'Artist',
            'Title',
            'Bitrate',
            'SampleRate',
            'BitDepth',
            'Channels',
            'Duration',
            'Spectrum',
            'DJ_Tauglich',
            'Genre'
        ]

        row_to_save = row.reindex(columns_to_keep)

        try:
            if os.path.exists(FIND_BETTER_CSV):
                df_bad = pd.read_csv(
                    FIND_BETTER_CSV,
                    delimiter=';',
                    encoding='utf-8-sig'
                )

                df_bad = df_bad.reindex(columns=columns_to_keep)

                df_bad = pd.concat(
                    [df_bad, pd.DataFrame([row_to_save])],
                    ignore_index=True
                )
            else:
                df_bad = pd.DataFrame([row_to_save], columns=columns_to_keep)

            df_bad.to_csv(
                FIND_BETTER_CSV,
                sep=';',
                index=False,
                encoding='utf-8-sig'
            )

        except PermissionError:
            return "<h2>Bitte Find_Better_Quality.csv schließen.</h2>"

        except Exception as e:
            return f"<h2>Fehler beim Speichern: {str(e)}</h2>"

        pending_indices_list.pop(current_pending_pointer)

    # =======================
    # SKIP → nächster Track
    # =======================
    elif action_type == "skip":
        current_pending_pointer += 1

        if current_pending_pointer >= len(pending_indices_list):
            current_pending_pointer = 0

    # CSV speichern
    df.to_csv(CSV_FILE, sep=';', index=False, encoding='utf-8-sig')

    # Pointer absichern
    if current_pending_pointer >= len(pending_indices_list):
        current_pending_pointer = 0

    return redirect(url_for('index'))


@app.route('/spectrum/<filename>')
def spectrum(filename):
    return send_from_directory(QC_FOLDER, filename)


if __name__ == '__main__':
    app.run(debug=True)