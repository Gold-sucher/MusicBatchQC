## Prerequisites

This project requires **FFmpeg** to be installed on your system, as it handles audio metadata extraction and spectrogram generation.

### How to install FFmpeg:

* **Windows:**
  1. Download the latest build from [Gyan.dev](https://www.gyan.dev/ffmpeg/builds/).
  2. Extract the folder and add the `bin` directory to your system's **PATH Environmental Variables**.
  3. Verify installation in your terminal using: `ffmpeg -version`

* **Mac (via Homebrew):**
  ```bash
  brew install ffmpeg
  
* **Linux (Ubunutu/Debian):**
  ```bash
  sudo apt update && sudo apt install ffmpeg