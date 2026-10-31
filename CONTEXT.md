# FF Downloader - project context

Download manager + app for fuckingfast.co links (FitGirl repacks). Built with Claude, Oct 2026.

## Files
- `ffdl.py` - core downloader / command-line tool. Also imported by the GUI, so both must sit together to rebuild.
- `ffdl_gui.py` - local web UI served from a small Python server, shown in a native window via pywebview (falls back to the default browser). Downloads go through `ffdl.download_item()`, the same code the CLI uses. API calls need the per-launch token baked into the page. The last download folder is remembered in `~/.ffdl/gui.json` (default `~/Downloads`).
- `requirements.txt` - pinned versions the build installs.
- `Build FF Downloader.bat` - installs deps, builds `FF Downloader.exe` with PyInstaller (excludes torch/scipy/pandas/etc. so it stays small), then `FF Downloader Setup.exe` with Inno Setup 6 if installed (`winget install JRSoftware.InnoSetup`).
- `installer.iss` - Inno Setup script: per-user install (no admin) to `%LOCALAPPDATA%\Programs\FF Downloader`, Start menu + optional desktop shortcut, uninstaller in Windows Settings. Bump `AppVersion` for new releases; keep `AppId` unchanged so updates replace the old install.
- `make_icon.py` / `icon.ico` - the app icon (violet download arrow), used by the exe and the setup.
- Share `FF Downloader Setup.exe` (or the self-contained `FF Downloader.exe`). Friends may need Microsoft WebView2 (built into Windows 11) and must click "More info > Run anyway" in SmartScreen since it's unsigned.
- GUI Cancel stops downloads and deletes this download's files (finished parts and `.part`/`.part.json`), then removes the folder only if it is empty; unrelated files are never touched.

## How it works
1. FitGirl page is scanned for `https://fuckingfast.co/<id>#<filename>` links (title from `<h1 class="entry-title">`). Files named `*optional*` / `*selective*` are grouped separately and unticked by default.
2. Each fuckingfast page no longer contains a direct link. The DOWNLOAD button does `hx-post /f/<id>/go` (htmx). `resolve_go()` replays that POST (headers `HX-Request`, `HX-Current-URL`, `Referer`, `Origin`) up to 3 times (first click = ad, then the file link) and reads the URL from HX-* headers or the body.
   - If that fails, `ffdl.py` falls back to driving a real Chrome window with Playwright and clicking DOWNLOAD (closing ad popups). The GUI does not have this fallback. Without Playwright (e.g. the .exe), a file that needs a browser is marked failed instead of hanging.
3. Cloudflare: `curl_cffi` with `impersonate="chrome"` plus cookies/user-agent saved from a real browser pass in `~/.ffdl/session.json` (profile in `~/.ffdl/browser-profile`). Plain `requests` was recognised and blocked even with valid cookies.
4. Speed: single connection was slow (0.5-2 MB/s vs ~10 MB/s in Chrome). Files are now fetched in N Range segments in parallel (default 8, `-c` in CLI, "Connections per file" in the GUI). Resume state: `<file>.part` plus `<file>.part.json` (segment offsets). Keep both; point the app at the same folder to resume. A `.part` with a `.json` is preallocated to full size, so it always resumes segmented (also with 1 connection) and is never renamed as complete early. A single-stream `.part` is only accepted on HTTP 416 if the server's size matches.
5. Finished parts are skipped on re-run. Extract `part01.rar` only (7-Zip / WinRAR picks up the rest).

## CLI
`python ffdl.py -i links.txt -o <folder> [-c 8] [-j 1] [--browser]`
`links.txt` may contain a FitGirl page URL or the fuckingfast links.

## Status / notes
- Verified: link parsing, segmented download with dropped connections and resume, GUI end to end against a fake local site.
- Real-site behaviour confirmed by the user: downloads worked in CLI mode (parts 1-5 of CoD MW4 finished), the built `.exe` opens and works.
- If the site changes its markup, the CLI writes `debug_<id>.txt` (the `/go` response) or `debug_<id>.png` (browser mode screenshot) next to the downloads; use those to update `resolve_go()`.
- The built exe bundles this exact code. To change behaviour, edit the `.py` files and re-run the build `.bat`.
