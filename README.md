# FF Downloader

A high-performance, resumable, multi-threaded parallel download manager with a modern desktop UI.

FF Downloader accelerates large file downloads using concurrent HTTP Range segments, automatic mirror failover, and persistent resume state.

---

## Features

- **Segmented Parallel Downloads**: Splits files into multiple concurrent byte-range streams (configurable connections per file) to maximize network throughput.
- **Resumable Transfers**: Stores chunk progress (`.part` and `.part.json` offset states) to safely resume interrupted transfers without re-downloading completed blocks.
- **Desktop Interface**: Clean, lightweight desktop UI built with `pywebview` featuring a brutalist manifest ledger, dark/light theme switching, and live per-file transfer progress.
- **Automated Fallback Mirrors**: Automatically retries failed segments using alternative host mirrors when available.
- **Selective Batch Processing**: Pre-scans multi-part file manifests before starting, allowing you to select/unselect groups (main, optional, selective).
- **Headless CLI Mode**: Fully functional command-line interface for scripts and automated workflows.

---

## Installation

### Prerequisites
- Python 3.10+
- Windows 10/11 (with Microsoft WebView2 runtime installed)

### Setup
```bash
git clone https://github.com/your-username/ff-downloader.git
cd ff-downloader
pip install -r requirements.txt
```

---

## Usage

### Desktop GUI
Launch the application window:
```bash
python ffdl_gui.py
```
1. Paste a download page URL or direct host links into the source bar.
2. Click **Fetch Files** to inspect the manifest and verify the file list.
3. Select your destination directory and desired connection concurrency (default: 8).
4. Click **Start Download**.

### Command Line Interface (CLI)
Download from a list of links or a supported page:
```bash
python ffdl.py -i links.txt -o ./downloads -c 8
```

Options:
- `-i, --input`: Input URL or path to text/HTML file with links.
- `-o, --output`: Target directory for downloaded files.
- `-c, --conns`: Number of parallel connections per file (default: 8).
- `-j, --jobs`: Number of files to download concurrently (default: 1).
- `--browser`: Enable interactive browser fallback for challenge resolution.

---

## Building Executables

To build a standalone Windows executable (`FF Downloader.exe`):
```bat
Build FF Downloader.bat
```
This packages the Python application with PyInstaller. If [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed, it also generates `FF Downloader Setup.exe`.

---

## License

MIT License.
