# ================================
# Musik-Qualitäts-Check (nur neue Tracks)
# ================================

# -------------------------------
# CONFIG
# -------------------------------
$sourceFolder = "C:\Users\thale\Music\For Rekordbox\Quality Check\QC_Input"
$outputFolder = "C:\Users\thale\Music\For Rekordbox\Quality Check\QC_Output"
$csvFile = Join-Path $outputFolder "QC_Report.csv"

# Corrupted Ordner automatisch relativ bestimmen
$baseFolder = Split-Path -Parent $sourceFolder
$corruptedFolder = Join-Path $baseFolder "Corrupted"

# Ordner erstellen falls nicht vorhanden
foreach ($folder in @($outputFolder, $corruptedFolder)) {
    if (-not (Test-Path $folder)) {
        New-Item -ItemType Directory -Path $folder | Out-Null
    }
}

# -------------------------------
# EXISTIERENDE CSV LADEN
# -------------------------------
$existingFiles = @()

if (Test-Path $csvFile) {
    $existingData = Import-Csv $csvFile ';' -Encoding UTF8
    $existingFiles = $existingData.FilePath
} else {
    $existingData = @()
    $existingFiles = @()
}

# Neue Einträge sammeln
$newEntries = @()


# Nach dem Laden der CSV, zum Testen:
Write-Host "Geladene Pfade aus CSV:"
$existingFiles | ForEach-Object { Write-Host "  [$_]" }

Write-Host "Gefundene Dateien:"
Get-ChildItem -Path $sourceFolder -Recurse -Include *.mp3,*.flac,*.wav,*.ogg,*.m4a,*.mp4 | ForEach-Object {
    Write-Host "  [$($_.FullName)]"
    Write-Host "  Match: $($existingFiles -contains $_.FullName)"
}




# -------------------------------
# DATEIEN SCANNEN
# -------------------------------
Get-ChildItem -Path $sourceFolder -Recurse -Include *.mp3, *.ogg, *.flac, *.wav, *.m4a, *.mp4 | ForEach-Object {

    $filePath = $_.FullName
    $baseName = $_.BaseName

    # -------------------------------
    # SKIP: Wenn bereits vorhanden
    # -------------------------------
    if ($existingFiles -contains $filePath) {
        Write-Host "Übersprungen (bereits analysiert): $filePath"
        return
    }

    Write-Host "Analysiere: $filePath"

    # -------------------------------
    # 1) Fehlerhafte Dateien erkennen
    # -------------------------------
    ffmpeg -v quiet -i "$filePath" -f null - 2>&1 | Out-Null
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
    	Write-Host "Beschädigt → verschiebe nach Corrupted"
    	...
    	return
    }

    # -------------------------------
    # 2) Spektrogramm erstellen
    # -------------------------------
    $spectrumFile = Join-Path $outputFolder "$baseName-spectrogram.png"

    ffmpeg -y -i "$filePath" -lavfi showspectrumpic=s=1920x1080 -update 1 "$spectrumFile" 2>&1 | Out-Null

    # -------------------------------
    # 3) Infos auslesen
    # -------------------------------
    $ffmpegOutput = ffmpeg -i "`"$filePath`"" 2>&1 | Out-String

    # Bitrate
    if ($ffmpegOutput -match "bitrate: (\d+) kb/s") {
        $bitrate = [int]$matches[1]
    } else {
        $bitrate = 0
    }

    # SampleRate
    if ($ffmpegOutput -match "(\d+) Hz") {
        $samplerate = [int]$matches[1]
    } else {
        $samplerate = 0
    }

    # Channels
    if ($ffmpegOutput -match "mono") {
        $channels = 1
    } elseif ($ffmpegOutput -match "stereo") {
        $channels = 2
    } else {
        $channels = 0
    }

    # Dauer
    if ($ffmpegOutput -match "Duration: (\d+:\d+:\d+\.\d+)") {
        $duration = $matches[1]
    } else {
        $duration = "Unknown"
    }

    # -------------------------------
    # 4) Objekt erstellen
    # -------------------------------
    $obj = [PSCustomObject]@{
        FileName        = $baseName
        FilePath        = $filePath
        Bitrate_kbps    = $bitrate
        SampleRate_Hz   = $samplerate
        Channels        = $channels
        Duration        = $duration
        SpectrumPath    = $spectrumFile
        Status          = ""
    }

    $newEntries += $obj
}

# -------------------------------
# 5) CSV SPEICHERN (anhängen)
# -------------------------------
if ($newEntries.Count -gt 0) {

    $finalData = @()

    if ($existingData) {
        $finalData += $existingData
    }

    $finalData += $newEntries

    $finalData | Export-Csv -Path $csvFile -NoTypeInformation -Encoding UTF8 -Delimiter ';'

    Write-Host ""
    Write-Host "Neue Tracks hinzugefügt: $($newEntries.Count)"
} else {
    Write-Host ""
    Write-Host "Keine neuen Tracks gefunden."
}