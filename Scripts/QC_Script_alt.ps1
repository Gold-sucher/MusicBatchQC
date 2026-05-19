# ==============================================================================
# PowerShell Skript: Musik-Qualitäts-Check mit DJ-Kennwerten (FFmpeg basiert)
# ==============================================================================

# ------------------------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------------------------
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$PSDefaultParameterValues['*:Encoding'] = 'utf8'
$sourceFolder = "C:\Users\thale\Music\For Rekordbox\Quality Check\QC_Input"
$outputFolder = "C:\Users\thale\Music\For Rekordbox\Quality Check\QC_Output"
$csvFile      = Join-Path $outputFolder "QC_Report.csv"
$baseFolder = Split-Path -Parent $MyInvocation.MyCommand.Path
$corruptedFolder = Join-Path $baseFolder "Corrupted"


# Zielordner erstellen, falls nicht vorhanden
if (-not (Test-Path $outputFolder)) {
    New-Item -ItemType Directory -Path $outputFolder | Out-Null
}

# Corrupted Ordner erstellen, falls nicht vorhanden
if (-not (Test-Path $corruptedFolder)) {
    New-Item -ItemType Directory -Path $corruptedFolder | Out-Null
}

# Array für CSV-Export vorbereiten
$report = @()

# ------------------------------------------------------------------------------
# AUDIO DATEIEN SCANNEN UND VERARBEITEN
# ------------------------------------------------------------------------------
$files = Get-ChildItem -Path $sourceFolder -Recurse -Include *.mp3, *.ogg, *.flac, *.wav, *.m4a, *.mp4, *.aiff

foreach ($file in $files) {
    $filePath = $file.FullName
    $baseName = $file.BaseName

    Write-Host "Verarbeite: $($file.Name)..." -ForegroundColor Cyan

    # Spektrogramm-Dateiname
    $spectrumFile = Join-Path $outputFolder "$baseName-spectrogram.png"

    # 1) Spektrogramm erstellen
    # --------------------------------------------------------------------------
    ffmpeg -y -i "$filePath" -lavfi showspectrumpic=s=1920x1080 "$spectrumFile" -loglevel error

    # 2) Technische Infos mit FFmpeg auslesen
    # --------------------------------------------------------------------------
    $ffmpegOutput = ffmpeg -v error -i "`"$filePath`"" -f null - 2>&1 | Out-String

if ($ffmpegOutput -match "Header missing|Invalid data|Error submitting packet") {
    Write-Host "Beschädigte Datei erkannt: $filePath"

    $destination = Join-Path $corruptedFolder $_.Name

    try {
        Move-Item -Path $filePath -Destination $destination -Force
        Write-Host "Nach Corrupted verschoben."
    }
    catch {
        Write-Host "Fehler beim Verschieben: $filePath"
    }

    return
}

    # Bitrate extrahieren
    if ($ffmpegOutput -match "bitrate: (\d+) kb/s") {
        $bitrate = [int]$matches[1]
    } else {
        $bitrate = 0
    }

    # SampleRate extrahieren
    if ($ffmpegOutput -match "(\d+) Hz") {
        $samplerate = [int]$matches[1]
    } else {
        $samplerate = 0
    }

    # Kanalanzahl extrahieren
    if ($ffmpegOutput -match "Audio:.*?(?:(\d+) channels|mono|stereo)") {
        if ($matches[1]) {
            $channels = [int]$matches[1]
        } elseif ($matches[0] -match "stereo") {
            $channels = 2
        } elseif ($matches[0] -match "mono") {
            $channels = 1
        } else {
            $channels = 0
        }
    } else {
        $channels = 0
    }

    # Bit Depth extrahieren
    if ($ffmpegOutput -match "(\d+)-bit") {
        $bitDepth = [int]$matches[1]
    } else {
        $bitDepth = 16 # Standard-Annahme für MP3
    }

    # Dauer extrahieren
    if ($ffmpegOutput -match "Duration: (\d+:\d+:\d+\.\d+)") {
        $duration = $matches[1]
    } else {
        $duration = "Unknown"
    }

    # 3) Metadaten (Artist & Title) extrahieren
    # --------------------------------------------------------------------------
    $artist = "Unknown"
    $title  = "Unknown"

    # Split an Zeilenumbrüchen
    foreach ($line in ($ffmpegOutput -split "`n")) {
        $line = $line.Trim()

        # Artist auslesen (ignoriere Cover-Art-Metadaten)
        if ($line -match "^artist\s*[:=]\s*(.+)$") {
            if ($line -notmatch "cover") {
                $artist = $matches[1].Trim()
            }
        }

        # Title auslesen
        if ($line -match "^title\s*[:=]\s*(.+)$") {
            if ($line -notmatch "cover") {
                $title = $matches[1].Trim()
            }
        }
    }

    # 4) DJ-Tauglichkeit prüfen
    # --------------------------------------------------------------------------
    # Kriterien: Min. 310 kbps (für MP3), 44.1kHz, Stereo
    $djReady = $true
    if ($bitrate -lt 310 -or $samplerate -lt 44100 -or $channels -lt 2) {
        $djReady = $false
    }

    # Objekt für CSV erstellen
    $obj = [PSCustomObject]@{
        FileName       = $baseName
        Artist         = $artist
        Title          = $title
        Bitrate_kbps   = $bitrate
        SampleRate_Hz  = $samplerate
        BitDepth       = $bitDepth
        Channels       = $channels
        Duration       = $duration
        DJ_Tauglich    = if ($djReady) {"Ja"} else {"Nein"}
        SpectrumPath   = $spectrumFile
        FilePath       = $filePath
    }
    $report += $obj
}

# ------------------------------------------------------------------------------
# 5) CSV EXPORTIEREN
# ------------------------------------------------------------------------------
$report | Export-Csv -Path $csvFile -NoTypeInformation -Delimiter ';' -Encoding UTF8
Write-Host "`n---------------------------------------------------" -ForegroundColor Green
Write-Host "Batch-Analyse abgeschlossen!" -ForegroundColor Green
Write-Host "Bericht gespeichert unter: $csvFile"
Write-Host "Spektrogramme liegen in: $outputFolder"