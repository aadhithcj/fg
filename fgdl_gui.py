#!/usr/bin/env python3
"""
FG Downloader - web UI for fgdl.py

Run:   python fgdl_gui.py      (keep fgdl.py in the same folder)
Opens a local page (random port) in an app window, or in your browser.
"""
import json
import os
import sys
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if sys.stdout is None:  # windowed .exe has no console
    sys.stdout = sys.stderr = open(os.devnull, "w")
try:
    import fgdl
except ImportError:
    import ffdl as fgdl

PORT = 0                            # set in main()
TOKEN = secrets.token_urlsafe(16)   # the page sends it back on every API call
SETTINGS_FILE = fgdl.STATE_DIR / "gui.json"
DEFAULT_BASE = Path.home() / "Downloads"
sess = fgdl.Session()


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(**kw):
    try:
        fgdl.STATE_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps({**load_settings(), **kw}, indent=1))
    except OSError:
        pass


# --------------------------------------------------------------------------- scanning
def scan(text):
    # links pasted back to back (no line break between them) get split apart again
    text = re.sub(r"(?<=\S)(?=https?://)", "\n", text.strip())
    if fgdl.PAGE_RE.search(text) or fgdl.DN_RE.search(text):
        title = "Links"
    else:
        r = sess.s.get(text, timeout=30)
        r.raise_for_status()
        text = r.text
        t = re.search(r'<h1 class="entry-title">(.*?)</h1>', text, re.S) or fgdl.TITLE_RE.search(text)
        title = re.sub(r"<[^>]+>", "", fgdl.htmllib.unescape(t.group(1))).strip() if t else "Download"
        title = re.sub(r"\s*[-–]\s*.*$", "", title).strip() or title
    flat = fgdl.htmllib.unescape(text)
    entries, order, seen = {}, [], set()
    unnamed_ff = False

    def kind_of(name):
        low = name.lower()
        return "optional" if "optional" in low else ("selective" if "selective" in low else "main")

    for m in fgdl.PAGE_RE.findall(flat):
        it = fgdl.Item(m)
        if it.page in seen:
            continue
        seen.add(it.page)
        name = it.name or it.id
        unnamed_ff = unnamed_ff or not it.name
        key = name.lower()
        if key in entries:
            continue
        entries[key] = {"page": m, "name": name, "kind": kind_of(name), "alt": ""}
        order.append(key)

    for m in fgdl.DN_RE.findall(flat):
        it = fgdl.Item(m)
        if it.page in seen:
            continue
        seen.add(it.page)
        name = it.name or it.id
        key = name.lower()
        if key in entries:
            if not entries[key]["alt"]:
                entries[key]["alt"] = m
        elif not unnamed_ff:
            entries[key] = {"page": m, "name": name, "kind": kind_of(name), "alt": ""}
            order.append(key)

    files = [entries[k] for k in order]
    if not files:
        raise RuntimeError("No supported download links found on that page.")
    folder = re.sub(r'[<>:"/\\|?*]', "", title).strip()[:80] or "Download"
    out = Path(load_settings().get("base") or DEFAULT_BASE) / folder
    mirrors = {
        "primary": sum(1 for f in files if fgdl.is_ff(f["page"])),
        "mirror": sum(1 for f in files if fgdl.is_dn(f["page"]) or f["alt"]),
    }
    return {"title": title, "files": files, "out": str(out), "mirrors": mirrors}


# --------------------------------------------------------------------------- engine
class Progress:
    """Stands in for fgdl's tqdm bar: progress goes into the file's status dict."""
    def __init__(self, f, total, done):
        self.f = f
        f["total"], f["done"] = total or 0, done

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n):
        self.f["done"] += n


class Engine:
    def __init__(self):
        self.files = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.out = ""
        self.title = ""
        self.conns = 8
        self.mirror = True
        self.dn_sess = None
        self.msg = ""
        self.cancelling = False

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, files, out, conns, title="", mirror=True):
        if self.running or self.cancelling:
            return
        Path(out).mkdir(parents=True, exist_ok=True)
        self.out, self.conns, self.msg, self.title = out, max(1, int(conns)), "", title
        self.mirror = bool(mirror)
        save_settings(base=str(Path(out).parent), conns=self.conns, mirror=self.mirror)
        self.files = [{"page": f["page"], "alt": f.get("alt", ""), "name": f["name"], "kind": f.get("kind", "main"),
                       "status": "queued", "done": 0, "total": 0, "error": "", "speed": 0.0, "_s": []}
                      for f in files]
        self.stop.clear()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        # pick the next queued file each time, so files re-queued while running get done too
        while not self.stop.is_set():
            f = next((f for f in self.files if f["status"] == "queued"), None)
            if f is None:
                break
            for attempt in range(1, 6):
                if self.stop.is_set():
                    break
                try:
                    self.one(f)
                    break
                except fgdl.NeedBrowser:
                    if self.mirror and f.get("alt") and not f.get("_use_alt"):
                        f["_use_alt"], f["status"] = True, "retrying via mirror"
                        continue
                    f["status"] = "failed"
                    f["error"] = "Blocked by challenge - try again later, or use fgdl.py --browser"
                    break
                except Exception as e:
                    if self.stop.is_set():
                        break
                    f["error"] = str(e)
                    if self.mirror and f.get("alt") and not f.get("_use_alt"):
                        f["_use_alt"], f["status"] = True, "retrying via mirror"
                        continue
                    if "not found" in str(e) or attempt == 5:
                        f["status"] = "failed"
                        break
                    f["status"] = f"retrying ({attempt}/4)"
                    self.stop.wait(5 * attempt)
        for f in self.files:
            if self.stop.is_set() and f["status"] not in ("done", "skipped", "failed"):
                f["status"] = "paused"
        self.msg = "Paused - press Resume to continue" if self.stop.is_set() else "All done"

    def one(self, f):
        out = Path(self.out)
        use_alt = bool(f.get("_use_alt"))
        item = fgdl.Item(f["alt"] if use_alt else f["page"])
        if use_alt and f.get("name"):
            item.name = fgdl.safe_name(f["name"]) or item.name
        f["status"] = "getting link"
        s = sess
        if use_alt:
            if self.dn_sess is None:
                self.dn_sess = fgdl.Session()
            s = self.dn_sess

        def make_bar(total, done):
            f["name"], f["status"] = item.name or f["name"], "downloading"
            return Progress(f, total, done)

        status = fgdl.download_item(s, item, out, self.conns, make_bar, self.stop)
        f["name"] = item.name or f["name"]
        if status == "skipped (exists)":
            f["status"] = "skipped"
            f["done"] = f["total"] = (out / item.name).stat().st_size
        elif status == "done":
            f["status"], f["error"] = "done", ""
        # "stopped": run() marks it paused

    def cancel(self):
        if self.cancelling or not self.files:
            return
        self.cancelling, self.msg = True, ""
        self.stop.set()
        threading.Thread(target=self._cancel, daemon=True).start()

    def _cancel(self):
        """Delete this download's files (finished and partial), then the folder if nothing else is in it."""
        if self.thread:
            self.thread.join(timeout=120)
        out, deleted = Path(self.out), 0
        for f in self.files:
            name = fgdl.safe_name(f["name"])
            if not name or not name.strip(". "):
                continue
            for p in (out / name, out / (name + ".part"), out / (name + ".part.json")):
                for _ in range(10):  # a segment may still be letting go of the file
                    try:
                        if p.is_file():
                            p.unlink()
                            deleted += p.suffix != ".json"
                        break
                    except OSError:
                        time.sleep(1)
        try:
            out.rmdir()  # only works when the folder is empty
            where = " and the folder"
        except OSError:
            where = ". The folder was kept because other files are in it" if out.exists() else ""
        self.files = []
        self.msg = f"Cancelled - deleted {deleted} file{'s' * (deleted != 1)}{where}."
        self.cancelling = False

    def status(self):
        now = time.time()
        out, tot_done, tot_all, speed = [], 0, 0, 0.0
        for f in self.files:
            s = f["_s"]
            s.append((now, f["done"]))
            while len(s) > 2 and now - s[0][0] > 5:
                s.pop(0)
            f["speed"] = (s[-1][1] - s[0][1]) / max(0.5, s[-1][0] - s[0][0]) if f["status"] == "downloading" else 0
            speed += f["speed"]
            tot_done += f["done"]
            tot_all += f["total"] or f["done"]
            row = {k: v for k, v in f.items() if not k.startswith("_")}
            row["via"] = "datanodes" if f.get("_use_alt") else "fuckingfast"
            row["mirror"] = bool(f.get("alt"))
            out.append(row)
        return {"running": self.running, "cancelling": self.cancelling, "files": out, "speed": speed,
                "done": tot_done, "total": tot_all, "out": self.out, "title": self.title, "msg": self.msg}


engine = Engine()


def browse_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose download folder")
        root.destroy()
        return path or ""
    except Exception:
        return ""


def page_html():
    st = load_settings()
    theme = "light" if st.get("theme") == "light" else "dark"
    theme_btn = "Dark" if theme == "light" else "Light"
    return (PAGE.replace("__TOKEN__", TOKEN)
                .replace("__THEME__", theme)
                .replace("__THEME_BTN__", theme_btn)
                .replace("__MIRROR__", " checked" if st.get("mirror", True) else "")
                .replace("__CONNS__", str(st.get("conns", 8))))


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        return self.headers.get("Host") == f"127.0.0.1:{PORT}"

    def token_ok(self):
        return self.host_ok() and secrets.compare_digest(self.headers.get("X-Token") or "", TOKEN)

    def do_GET(self):
        if self.path == "/" and self.host_ok():
            return self.send(200, page_html(), "text/html; charset=utf-8")
        if not self.token_ok():
            return self.send(403, "{}")
        if self.path == "/api/status":
            return self.send(200, json.dumps(engine.status()))
        self.send(404, "{}")

    def do_POST(self):
        if not self.token_ok():
            return self.send(403, "{}")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/scan":
                return self.send(200, json.dumps(scan(body["url"])))
            if self.path == "/api/start":
                if not all(fgdl.is_supported(f["page"]) and (not f.get("alt") or fgdl.is_dn(f["alt"]))
                           for f in body["files"]):
                    raise ValueError("Only supported host links can be downloaded.")
                engine.start(body["files"], body["out"], body.get("conns", 8), body.get("title", ""),
                             body.get("mirror", True))
                return self.send(200, "{}")
            if self.path == "/api/clear":
                if not engine.running and not engine.cancelling:
                    engine.files, engine.msg = [], ""
                return self.send(200, "{}")
            if self.path == "/api/theme":
                save_settings(theme="light" if body.get("theme") == "light" else "dark")
                return self.send(200, "{}")
            if self.path == "/api/mirror":
                save_settings(mirror=bool(body.get("on")))
                return self.send(200, "{}")
            if self.path == "/api/cancel":
                engine.cancel()
                return self.send(200, "{}")
            if self.path == "/api/pause":
                engine.stop.set()
                return self.send(200, "{}")
            if self.path == "/api/resume":
                if engine.cancelling:
                    return self.send(200, "{}")
                running = engine.running
                for f in engine.files:
                    if f["status"] == "failed" or (not running and f["status"] not in ("done", "skipped")):
                        f["status"], f["error"] = "queued", ""
                if not running:
                    engine.stop.clear()
                    engine.msg = ""
                    engine.thread = threading.Thread(target=engine.run, daemon=True)
                    engine.thread.start()
                return self.send(200, "{}")
            if self.path == "/api/browse":
                return self.send(200, json.dumps({"path": browse_folder()}))
            if self.path == "/api/open":
                if engine.out and Path(engine.out).is_dir() and hasattr(os, "startfile"):
                    os.startfile(engine.out)
                return self.send(200, "{}")
        except Exception as e:
            return self.send(400, json.dumps({"error": str(e)}))
        self.send(404, "{}")


PAGE = r"""<!doctype html>
<html lang="en" data-theme="__THEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FG Downloader</title>
<style>
:root{
--ui:'Bahnschrift','DIN Alternate','Franklin Gothic Medium','Segoe UI',system-ui,sans-serif;
--mono:'Cascadia Mono','Cascadia Code','Consolas',ui-monospace,monospace;
--ease:cubic-bezier(.2,.9,.2,1)
}
:root[data-theme=light]{
color-scheme:light;
--bg:#e7e4dc;--panel:#f7f5ef;--panel-2:#efebe2;--ink:#0c0c0b;--mute:#555147;--faint:#6d685e;
--line:#0c0c0b;--rule:#c8c3b7;--inv:#0c0c0b;--inv-ink:#f7f5ef;--hatch:rgba(12,12,11,.16);
--grain:rgba(12,12,11,.045);
--accent:#2ecc5b;--accent-hi:#23b44c;--accent-ink:#04120a;
--danger:#ff6e6c;--danger-hi:#e85a58;--danger-ink:#1a0505;--danger-text:#a8241d
}
:root[data-theme=dark]{
color-scheme:dark;
--bg:#0b0b0a;--panel:#131312;--panel-2:#1b1b19;--ink:#f1eee5;--mute:#a8a398;--faint:#8b8679;
--line:#f1eee5;--rule:#2c2b28;--inv:#f1eee5;--inv-ink:#0b0b0a;--hatch:rgba(241,238,229,.18);
--grain:rgba(241,238,229,.03);
--accent:#3ddc6a;--accent-hi:#2fc45a;--accent-ink:#04120a;
--danger:#ff7b78;--danger-hi:#ea6663;--danger-ink:#1a0505;--danger-text:#ff9b98
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 var(--ui);
-webkit-font-smoothing:antialiased;overflow:hidden;
background-image:radial-gradient(var(--grain) .5px,transparent .5px);background-size:4px 4px}
::selection{background:var(--ink);color:var(--inv-ink)}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:var(--panel-2)}
::-webkit-scrollbar-thumb{background:var(--ink);border:3px solid var(--panel-2)}
:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
button,input,textarea{font:inherit;color:inherit}
.hidden{display:none!important}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

.app{height:100vh;height:100dvh;display:grid;grid-template-rows:auto auto minmax(0,1fr) auto}

/* ---------------------------------------------------------------- top bar */
.topbar{display:flex;align-items:center;gap:16px;height:54px;padding:0 20px;border-bottom:2px solid var(--line);background:var(--panel)}
.brand{display:flex;align-items:center;gap:7px;min-width:0}
.brand-block{background:var(--inv);color:var(--inv-ink);padding:4px 9px 5px;font-weight:700;font-size:18px;letter-spacing:.08em;line-height:1;border-bottom:3px solid var(--accent)}
.brand-note{font:600 9px var(--ui);letter-spacing:.16em;text-transform:uppercase;color:var(--mute);margin-left:6px;white-space:nowrap}
.top-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.hostnote{font:600 9px var(--ui);letter-spacing:.16em;text-transform:uppercase;color:var(--mute)}

/* ---------------------------------------------------------------- buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:34px;padding:0 12px;background:transparent;
color:var(--ink);border:1px solid var(--line);border-radius:0;font:600 10.5px var(--ui);letter-spacing:.11em;text-transform:uppercase;
cursor:pointer;white-space:nowrap;transition:background-color .1s,color .1s,border-color .1s}
.btn:hover:not(:disabled){background:var(--ink);color:var(--inv-ink)}
.btn:disabled{opacity:.32;cursor:not-allowed}
.btn-primary{background:var(--accent);color:var(--accent-ink);border-color:var(--line)}
.btn-primary:hover:not(:disabled){background:var(--accent-hi);color:var(--accent-ink)}
.btn-primary:active:not(:disabled){background:var(--accent-hi);color:var(--accent-ink)}
.btn-danger{background:var(--danger);color:var(--danger-ink);border-color:var(--line);background-image:none}
.btn-danger:hover:not(:disabled){background:var(--danger-hi);color:var(--danger-ink)}
.btn-danger-solid{background:var(--danger);color:var(--danger-ink);border-color:var(--line)}
.btn-danger-solid:hover:not(:disabled){background:var(--danger-hi);color:var(--danger-ink)}
.btn-block{display:flex;width:100%;height:44px}
.btn-square{width:34px;padding:0;flex:none;font-size:14px;letter-spacing:0}
.btn-tiny{height:26px;padding:0 8px;font-size:9px;letter-spacing:.1em}
.btn.busy{pointer-events:none}
.btn.busy .btn-label::after{content:"";display:inline-block;width:7px;height:7px;margin-left:8px;background:var(--accent-ink);animation:pulse .9s steps(2) infinite}

/* ---------------------------------------------------------------- source bar */
.sourcebar{display:grid;grid-template-columns:72px minmax(0,1fr) auto;gap:14px;align-items:start;padding:12px 20px 13px;
border-bottom:1px solid var(--line);background:var(--bg)}
.source-label{font:700 9px var(--ui);letter-spacing:.18em;text-transform:uppercase;color:var(--mute);padding-top:15px}
.source-field{min-width:0}
textarea#url{display:block;width:100%;min-height:44px;max-height:128px;resize:none;overflow:auto;background:var(--panel);
border:1px solid var(--line);border-radius:0;padding:12px 12px;font:400 12px/1.45 var(--mono);caret-color:var(--ink);outline:none}
textarea#url::placeholder{color:var(--faint)}
textarea#url:focus{outline:2px solid var(--ink);outline-offset:-1px}
.source-err{margin-top:7px;border:1px solid var(--line);padding:6px 8px;font:600 9.5px var(--mono);
letter-spacing:.04em;text-transform:uppercase;background:repeating-linear-gradient(135deg,transparent 0 5px,var(--hatch) 5px 6px)}
.source-err:empty{display:none}
.btn-fetch{height:44px;min-width:142px}

/* ---------------------------------------------------------------- workspace */
.workspace{display:grid;grid-template-columns:300px minmax(0,1fr);min-height:0;background:var(--bg)}
.rail{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line);background:var(--panel)}
.rail-scroll{flex:1;overflow-y:auto;overscroll-behavior:contain;display:flex;flex-direction:column}
.block{padding:16px 18px;border-bottom:1px solid var(--rule)}
#noteBlock{margin-top:auto;border-top:1px solid var(--rule);border-bottom:0}
.micro{font:700 9.5px var(--ui);letter-spacing:.17em;text-transform:uppercase;color:var(--mute)}
.micro.mt{margin-top:16px}
.rail h1{margin:9px 0 5px;font:600 19px/1.1 var(--ui);letter-spacing:-.01em;text-transform:uppercase;overflow-wrap:anywhere}
.rail-line{font:600 10px var(--mono);letter-spacing:.03em;text-transform:uppercase;color:var(--mute);line-height:1.5}
.kv{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--rule);
font:600 10.5px var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--mute)}
.kv:first-of-type{margin-top:8px}
.kv b{color:var(--ink);font-weight:600;text-align:right;max-width:62%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.checkline{display:grid;grid-template-columns:16px minmax(0,1fr);gap:9px;align-items:start;margin-top:11px;cursor:pointer;
font:600 9.5px/1.35 var(--ui);letter-spacing:.07em;text-transform:uppercase}
.checkline input{position:absolute;opacity:0;width:0;height:0}
.checkline .box{width:15px;height:15px;margin-top:1px;border:1px solid var(--line);background:var(--panel);display:grid;place-items:center}
.checkline input:checked+.box{background:var(--ink)}
.checkline input:checked+.box::after{content:"";width:7px;height:4px;border-left:2px solid var(--inv-ink);border-bottom:2px solid var(--inv-ink);transform:rotate(-45deg) translateY(-1px)}
.checkline input:focus-visible+.box{outline:2px solid var(--ink);outline-offset:2px}
.pathrow{display:flex;gap:0;margin-top:9px}
.pathrow input{flex:1;min-width:0;height:34px;background:var(--panel-2);border:1px solid var(--line);border-right:0;border-radius:0;
padding:0 9px;font:400 10.5px var(--mono);outline:none}
.pathrow input:focus{outline:2px solid var(--ink);outline-offset:-2px}
.stepper{display:grid;grid-template-columns:34px minmax(0,1fr) 34px;margin-top:9px}
.stepper button{height:34px;background:transparent;border:1px solid var(--line);border-radius:0;cursor:pointer;font:600 15px var(--mono);line-height:1}
.stepper button:hover{background:var(--ink);color:var(--inv-ink)}
.stepper input{height:34px;min-width:0;text-align:center;background:var(--panel-2);border:1px solid var(--line);border-left:0;border-right:0;
border-radius:0;font:600 13px var(--mono);outline:none;-moz-appearance:textfield}
.stepper input::-webkit-outer-spin-button,.stepper input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.note{margin:10px 0 0;font:400 10.5px/1.55 var(--ui);color:var(--mute)}
.note b{color:var(--ink);font-weight:600}
.bigpct{margin:10px 0 4px;font:600 46px/.9 var(--mono);letter-spacing:-.06em;font-variant-numeric:tabular-nums}
.bigpct small{font-size:16px;letter-spacing:0;color:var(--mute);margin-left:2px}
.rail-actions{flex:none;padding:14px 18px 16px;border-top:2px solid var(--line);background:var(--panel)}
.btn-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.btn-row+.btn,.btn+.btn-block,.btn-block+.btn-block{margin-top:8px}
.confirm{margin-top:9px;border:1px solid var(--line);padding:10px;background:var(--panel-2)}
.confirm p{margin:0 0 9px;font:600 9.5px/1.45 var(--ui);letter-spacing:.05em;text-transform:uppercase}
.err{margin-top:8px;font:600 10px/1.45 var(--mono);text-transform:uppercase;color:var(--ink);
border:1px solid var(--line);padding:6px 8px;background:repeating-linear-gradient(135deg,transparent 0 5px,var(--hatch) 5px 6px)}
.err:empty{display:none}

/* ---------------------------------------------------------------- manifest pane */
.pane{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--bg)}
.pane-head{flex:none;display:flex;align-items:center;gap:14px;height:46px;padding:0 18px;border-bottom:1px solid var(--line);background:var(--panel)}
.pane-title{display:flex;align-items:baseline;gap:10px;min-width:0}
.pane-count{font:600 10.5px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--mute);font-variant-numeric:tabular-nums;white-space:nowrap}
.pane-tools{margin-left:auto;display:flex;align-items:center;gap:6px}
.liveword{font:700 9px var(--ui);letter-spacing:.16em;color:var(--mute)}
.liveword::before{content:"";display:inline-block;width:6px;height:6px;background:var(--accent);margin-right:6px;animation:pulse 1s steps(2) infinite}
.tablewrap{position:relative;flex:1;min-height:0;overflow:auto;overscroll-behavior:contain}
table.manifest{width:100%;table-layout:fixed;border-collapse:collapse}
.manifest th{position:sticky;top:0;z-index:2;background:var(--panel);text-align:left;padding:8px 10px;
border-bottom:1px solid var(--line);font:700 9px var(--ui);letter-spacing:.15em;text-transform:uppercase;color:var(--mute)}
.c-ck{width:42px}.c-idx{width:52px}.c-kind{width:96px}.c-mirror{width:74px}.c-state{width:124px}
.manifest td{padding:8px 10px;border-bottom:1px solid var(--rule);vertical-align:middle}
.frow{cursor:pointer;background:var(--bg);transition:background .1s}
.frow:hover{background:var(--panel-2)}
.frow.on{background:var(--panel)}
.frow.intro{animation:rowin .26s var(--ease) both;animation-delay:calc(var(--i,0)*10ms)}
.frow.via-dn .c-mirror{font-weight:700}
.c-idx{font:400 10px var(--mono);color:var(--faint);font-variant-numeric:tabular-nums}
.c-name{min-width:0}
.nm{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:400 12px var(--mono)}
.c-kind{font:600 9.5px var(--ui);letter-spacing:.08em;text-transform:uppercase;color:var(--mute)}
.c-mirror{font:600 9.5px var(--mono);letter-spacing:.04em;color:var(--mute)}
.c-state{text-align:right}
.minibar{display:none;height:3px;margin-top:5px;background:var(--rule)}
.frow.live .minibar{display:block}
.minibar i{display:block;height:100%;width:0;background:var(--accent);transition:width .4s var(--ease)}
.rowerr{margin-top:4px;font:600 9px/1.4 var(--mono);text-transform:uppercase;color:var(--danger-text);
white-space:normal;overflow-wrap:anywhere}
.ckwrap{display:inline-flex;cursor:pointer}
.ckwrap input{position:absolute;opacity:0;width:0;height:0}
.ckwrap span{width:15px;height:15px;border:1px solid var(--line);background:var(--panel);display:grid;place-items:center}
.ckwrap input:checked+span{background:var(--ink)}
.ckwrap input:checked+span::after{content:"";width:7px;height:4px;border-left:2px solid var(--inv-ink);border-bottom:2px solid var(--inv-ink);transform:rotate(-45deg) translateY(-1px)}
.ckwrap input:focus-visible+span{outline:2px solid var(--ink);outline-offset:2px}
.ckwrap input:disabled+span{opacity:.35;cursor:not-allowed}
.tick{display:block;width:9px;height:9px;border:1px solid var(--line);background:transparent;margin:0 auto}
.tick.done{background:var(--ink)}
.tick.active{background:var(--accent);animation:pulse 1s steps(2) infinite}
.tick.failed{background:var(--danger)}
.stamp{display:inline-flex;align-items:center;gap:5px;padding:2px 6px;border:1px solid transparent;
font:600 9.5px var(--ui);letter-spacing:.1em;text-transform:uppercase;white-space:nowrap}
.stamp.queued{color:var(--mute)}
.stamp.link{border-color:var(--rule);color:var(--ink)}
.stamp.active{background:var(--accent);color:var(--accent-ink)}
.stamp.active::before{content:"";width:4px;height:4px;background:currentColor;animation:pulse .9s steps(2) infinite}
.stamp.done{background:var(--inv);color:var(--inv-ink)}
.stamp.failed{background:var(--danger);color:var(--danger-ink);border-color:var(--line)}
.stamp.paused,.stamp.retry{border:1px dashed var(--line)}
.band td{padding:0;border-bottom:1px solid var(--line)}
.band-in{display:flex;align-items:center;gap:10px;padding:6px 10px;background:var(--inv);color:var(--inv-ink);
font:700 9.5px var(--ui);letter-spacing:.15em;text-transform:uppercase}
.band-in span:nth-child(2){margin-left:auto;font:600 9.5px var(--mono);letter-spacing:.04em;opacity:.7}
.band-btn{background:transparent;color:inherit;border:1px solid currentColor;border-radius:0;padding:2px 6px;
font:700 9px var(--ui);letter-spacing:.1em;text-transform:uppercase;cursor:pointer;margin-left:4px}
.band-btn:hover{background:var(--inv-ink);color:var(--inv)}
.empty{display:flex;flex-direction:column;align-items:flex-start;justify-content:center;min-height:100%;margin:0 auto;width:100%;max-width:600px;padding:44px 40px}
.empty-mark{font:700 40px/.95 var(--ui);letter-spacing:-.03em;text-transform:uppercase;color:var(--ink);
padding-bottom:8px;border-bottom:6px solid var(--accent)}
.empty p{max-width:48ch;margin:16px 0 22px;font:400 12px/1.6 var(--ui);color:var(--mute)}
.empty p+.empty-note{margin-top:-10px;font:600 9.5px/1.5 var(--mono);letter-spacing:.06em;text-transform:uppercase}
.empty dl{margin:0;border-top:1px solid var(--line)}
.empty dl>div{display:grid;grid-template-columns:64px minmax(0,1fr);gap:12px;padding:8px 0;border-bottom:1px solid var(--rule)}
.empty dt{font:700 9px var(--ui);letter-spacing:.14em;text-transform:uppercase;color:var(--mute);padding-top:2px}
.empty dd{margin:0;font:400 11px var(--mono);overflow-wrap:anywhere}
.pane-foot{flex:none;display:flex;align-items:center;justify-content:space-between;gap:14px;height:32px;padding:0 18px;
border-top:1px solid var(--line);background:var(--panel);font:600 9.5px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--mute);font-variant-numeric:tabular-nums}

/* ---------------------------------------------------------------- status bar */
.statusbar{display:grid;grid-template-columns:auto minmax(90px,1fr) auto;align-items:center;gap:14px;height:32px;padding:0 16px;
border-top:2px solid var(--line);background:var(--panel)}
.status-word{font:700 9.5px var(--ui);letter-spacing:.18em;text-transform:uppercase;white-space:nowrap}
.status-num{font:600 9.5px var(--mono);letter-spacing:.04em;color:var(--mute);white-space:nowrap;font-variant-numeric:tabular-nums}
.meter{height:9px;border:1px solid var(--line);background:var(--bg);overflow:hidden}
.meter i{display:block;height:100%;width:0;background:repeating-linear-gradient(90deg,var(--accent) 0 5px,transparent 5px 7px);transition:width .4s var(--ease)}

.toast{position:fixed;right:16px;bottom:44px;z-index:50;max-width:400px;padding:10px 12px;background:var(--inv);color:var(--inv-ink);
border:1px solid var(--line);font:600 9.5px/1.5 var(--mono);letter-spacing:.04em;text-transform:uppercase}

@keyframes rowin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes pulse{50%{opacity:.15}}

@media(max-width:920px) and (min-width:781px){
  .c-kind{display:none}
  .c-state{width:112px}
  .c-mirror{width:66px}
}
@media(max-width:780px){
  body{overflow:auto}
  .app{height:auto;min-height:100vh;min-height:100dvh}
  .workspace{grid-template-columns:1fr}
  .rail{border-right:0;border-bottom:1px solid var(--line)}
  .rail-scroll{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));overflow:visible}
  .block{border-right:1px solid var(--rule)}
  .rail-actions{position:static}
  .pane{min-height:0}
  .tablewrap{overflow:visible}
  .empty{min-height:360px}
  .pane-head{position:sticky;top:0;z-index:7}
}
@media(max-width:660px){
  .topbar{padding:0 14px}
  .brand-note,.hostnote{display:none}
  .sourcebar{grid-template-columns:minmax(0,1fr) auto;padding:10px 14px 12px;gap:8px}
  .source-label{display:none}
  .btn-fetch{min-width:0;padding:0 12px;font-size:9.5px}
  .rail-scroll{grid-template-columns:1fr}
  .block{border-right:0}
  .c-kind,.c-mirror{display:none}
  .c-state{width:104px}
  .empty{padding:32px 20px}
  .empty-mark{font-size:32px}
  .pane-head,.pane-foot,.statusbar{padding-left:12px;padding-right:12px}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}
</style>
</head>
<body>
<div class="app">

<header class="topbar">
  <div class="brand">
    <span class="brand-block">FG</span>
    <span class="brand-note">FG Downloader</span>
  </div>
  <div class="top-right">
    <span class="hostnote">Desktop app</span>
    <button class="btn btn-tiny" id="themeBtn" onclick="toggleTheme()" aria-label="Switch colour theme">__THEME_BTN__</button>
  </div>
</header>

<section class="sourcebar">
  <label class="source-label" for="url">Source</label>
  <div class="source-field">
    <textarea id="url" rows="1" spellcheck="false" autocomplete="off" autocapitalize="off"
      aria-label="Game page or download links"
      placeholder="Paste a page URL or host links"></textarea>
    <div class="source-err" id="scanErr" role="alert"></div>
  </div>
  <button class="btn btn-primary btn-fetch" id="scanBtn" onclick="doScan()"><span class="btn-label">Fetch files</span></button>
</section>

<main class="workspace">
  <aside class="rail" aria-label="Download controls">
    <div class="rail-scroll">
      <section class="block">
        <div class="micro">Title</div>
        <h1 id="gtitle">No source</h1>
        <div class="rail-line" id="gsub">Paste a link to load a manifest</div>
      </section>

      <section class="block" id="hostBlock">
        <div class="micro">Hosts</div>
        <div class="kv"><span>Primary Host</span><b id="mPrimary">—</b></div>
        <div class="kv"><span>Mirror Host</span><b id="mMirror">—</b></div>
        <label class="checkline" for="mirror">
          <input type="checkbox" id="mirror" onchange="setMirror()"__MIRROR__>
          <span class="box" aria-hidden="true"></span>
          <span>Retry failed parts on mirror</span>
        </label>
      </section>

      <section class="block" id="setupBlock">
        <div class="micro">Destination</div>
        <div class="pathrow">
          <input type="text" id="out" spellcheck="false" aria-label="Download folder">
          <button class="btn btn-square" onclick="browse()" title="Choose folder" aria-label="Choose folder">…</button>
        </div>
        <div class="micro mt">Connections per file</div>
        <div class="stepper">
          <button type="button" onclick="bump(-1)" aria-label="Fewer connections">−</button>
          <input type="number" id="conns" value="__CONNS__" min="1" max="32" aria-label="Connections per file">
          <button type="button" onclick="bump(1)" aria-label="More connections">+</button>
        </div>
        <p class="note">8–16 is the useful range. More is faster until the host starts refusing.</p>
      </section>

      <section class="block hidden" id="readout">
        <div class="micro">Transfer</div>
        <div class="bigpct" id="pct">0<small>%</small></div>
        <div class="kv"><span>State</span><b id="rState">—</b></div>
        <div class="kv"><span>Speed</span><b id="rSpeed">—</b></div>
        <div class="kv"><span>Left</span><b id="rEta">—</b></div>
        <div class="kv"><span>Bytes</span><b id="rBytes">—</b></div>
        <div class="kv"><span>Folder</span><b id="rFolder" title="">—</b></div>
      </section>

      <section class="block" id="noteBlock">
        <p class="note" style="margin:0"><b>Extract part01.rar only.</b> 7-Zip / WinRAR picks up the remaining parts from the same folder.</p>
      </section>
    </div>

    <div class="rail-actions">
      <div id="startWrap">
        <button class="btn btn-primary btn-block" id="startBtn" onclick="start()">Start download</button>
        <div class="err" id="startErr" role="alert"></div>
      </div>
      <div id="runWrap" class="hidden">
        <div class="btn-row">
          <button class="btn" id="pauseBtn" onclick="api('pause')">Pause</button>
          <button class="btn" id="resumeBtn" onclick="api('resume')">Resume</button>
        </div>
        <button class="btn btn-block" id="openBtn" onclick="api('open')">Open folder</button>
        <button class="btn btn-danger btn-block" id="cancelBtn" onclick="askCancel(true)">Cancel &amp; delete files</button>
        <div class="confirm hidden" id="confirm">
          <p id="confirmText">Stop and delete every file from this download?</p>
          <div class="btn-row">
            <button class="btn" onclick="askCancel(false)">Keep</button>
            <button class="btn btn-danger-solid" onclick="doCancel()">Delete</button>
          </div>
        </div>
      </div>
    </div>
  </aside>

  <section class="pane" aria-label="Manifest">
    <div class="pane-head">
      <div class="pane-title">
        <span class="micro" id="paneLabel">Manifest</span>
        <span class="pane-count" id="paneCount">0 files</span>
      </div>
      <div class="pane-tools" id="pickTools">
        <button class="btn btn-tiny" onclick="selectAll(true)">All</button>
        <button class="btn btn-tiny" onclick="selectAll(false)">None</button>
      </div>
      <div class="pane-tools hidden" id="runTools">
        <span class="liveword" id="liveWord">Live</span>
        <button class="btn btn-tiny hidden" id="newBtn" onclick="clearAll()">New source</button>
      </div>
    </div>

    <div class="tablewrap" id="tablewrap">
      <table class="manifest">
        <thead id="thead">
          <tr>
            <th class="c-ck" scope="col"><span class="sr">Select</span></th>
            <th class="c-idx" scope="col">#</th>
            <th class="c-name" scope="col">File</th>
            <th class="c-kind" scope="col">Group</th>
            <th class="c-mirror" scope="col">Mirror</th>
            <th class="c-state" scope="col">State</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="empty" id="empty">
        <div class="empty-mark">No manifest</div>
        <p>Paste a page URL to inspect parts, or paste host links one per line. Every part is verified and listed before transfer starts.</p>
        <p class="empty-note">Accepts supported web pages, multi-part packages, or direct host links.</p>
      </div>
    </div>

    <div class="pane-foot">
      <span id="footLeft">Nothing selected</span>
      <span id="footRight">0 / 0</span>
    </div>
  </section>
</main>

<footer class="statusbar">
  <span class="status-word" id="statusWord">Ready</span>
  <div class="meter" aria-hidden="true"><i id="tbar"></i></div>
  <span class="status-num" id="statusNum">0 B</span>
</footer>

<div class="toast hidden" id="toast" role="status" aria-live="polite"></div>
</div>

<script>
const TOKEN='__TOKEN__';
const FIN=['done','skipped'];
const $=id=>document.getElementById(id);
let files=[],sel=new Set(),title='',last=null,confirming=false,timer=null,toastTimer=null,intro=false,scanning=false;
let theme=document.documentElement.dataset.theme;

const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const fmt=b=>{if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+' '+u[i]};
const tm=s=>{if(!isFinite(s)||s<=0)return'';s=Math.round(s);const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}h ${m}m`:m?`${m}m ${s%60}s`:`${s}s`};
const pad=n=>String(n).padStart(3,'0');
const busy=()=>!!last&&(last.running||last.cancelling);
const kindName=k=>k==='main'?'Main':k==='selective'?'Selective':'Optional';

async function post(p,b){
  const r=await fetch('/api/'+p,{method:'POST',headers:{'X-Token':TOKEN},body:JSON.stringify(b||{})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.error||'Something went wrong. Try again.');
  return j;
}
async function api(p){try{await post(p);poll()}catch(e){toast(e.message)}}
function toast(msg){
  const t=$('toast');t.textContent=msg;t.classList.remove('hidden');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.add('hidden'),7000);
}
function toggleTheme(){
  theme=theme==='light'?'dark':'light';
  document.documentElement.dataset.theme=theme;
  $('themeBtn').textContent=theme==='light'?'Dark':'Light';
  post('theme',{theme}).catch(()=>{});
}
function setMirror(){post('mirror',{on:$('mirror').checked}).catch(()=>{})}
function bump(n){
  const i=$('conns');i.value=Math.max(1,Math.min(32,(+i.value||8)+n));
}

/* ------------------------------------------------------------------ scan */
async function doScan(){
  const b=$('scanBtn');if(b.disabled)return;
  if(busy()){toast('A transfer is running. Finish or cancel it first.');return}
  scanning=true;
  $('scanErr').textContent='';b.disabled=true;b.classList.add('busy');
  b.querySelector('.btn-label').textContent='Fetching';
  try{
    const d=await post('scan',{url:$('url').value});
    if(last&&!busy()){try{await post('clear')}catch(e){}}
    files=d.files||[];title=d.title||'';
    sel=new Set(files.filter(f=>f.kind!=='optional').map(f=>f.page));
    $('gtitle').textContent=d.title||'Links';
    $('out').value=d.out||'';
    $('mPrimary').textContent=d.mirrors?d.mirrors.primary:0;
    $('mMirror').textContent=d.mirrors?d.mirrors.mirror:0;
    $('gsub').textContent=`${files.length} part${files.length===1?'':'s'} · ${sel.size} selected`;
    intro=true;
    await poll();
  }catch(e){
    $('scanErr').textContent=e.message;
  }
  b.disabled=false;b.classList.remove('busy');
  b.querySelector('.btn-label').textContent='Fetch files';
  scanning=false;
}

/* ------------------------------------------------------------------ manifest */
function mirrorLabel(f){
  if(f.alt)return'PRI+MIR';
  return /datanodes\.to/i.test(f.page)?'MIR':'PRI';
}
function rowHtml(f,i,n,anim){
  return `<tr class="frow ${anim?'intro':''} ${sel.has(f.page)?'on':''}" style="--i:${i}" onclick="rowTog(event,${i})">
    <td class="c-ck"><label class="ckwrap" onclick="event.stopPropagation()"><input type="checkbox" ${sel.has(f.page)?'checked':''} onchange="tog(${i})" aria-label="Select ${esc(f.name)}"><span></span></label></td>
    <td class="c-idx">${pad(n)}</td>
    <td class="c-name"><span class="nm" title="${esc(f.name)}">${esc(f.name)}</span></td>
    <td class="c-kind">${kindName(f.kind)}</td>
    <td class="c-mirror">${mirrorLabel(f)}</td>
    <td class="c-state"><span class="stamp queued">Ready</span></td>
  </tr>`;
}
const cols=()=>window.innerWidth<=660?4:(window.innerWidth<=920?5:6);
function renderPick(){
  const kinds=[['main','Main parts'],['selective','Selective'],['optional','Optional / languages']]
    .filter(([k])=>files.some(f=>f.kind===k));
  let html='',shown=0;
  const c=cols();
  for(const [k,label] of kinds){
    const group=files.map((f,i)=>[f,i]).filter(([f])=>f.kind===k);
    const on=group.filter(([f])=>sel.has(f.page)).length;
    html+=`<tr class="band"><td colspan="${c}"><div class="band-in"><span>${label}</span><span>${on}/${group.length} selected</span>
      <button type="button" class="band-btn" onclick="toggleKind('${k}')">${on===group.length?'Clear':'All'}</button></div></td></tr>`;
    for(const [f,i] of group)html+=rowHtml(f,i,++shown,intro);
  }
  intro=false;
  $('rows').innerHTML=html;
  $('thead').classList.toggle('hidden',!files.length);
  $('empty').classList.toggle('hidden',!!files.length);
  $('paneLabel').textContent='Manifest';
  $('paneCount').textContent=`${files.length} file${files.length===1?'':'s'}`;
  $('footLeft').textContent=files.length?`${sel.size} of ${files.length} selected`:'Nothing selected';
  $('footRight').textContent=`${files.length} part${files.length===1?'':'s'}`;
  $('startBtn').disabled=!sel.size||busy();
  $('gsub').textContent=files.length?`${files.length} part${files.length===1?'':'s'} · ${sel.size} selected`:'Paste a link to load a manifest';
  $('pickTools').classList.toggle('hidden',!files.length);
  $('runTools').classList.add('hidden');
  $('setupBlock').classList.remove('hidden');
  $('noteBlock').classList.remove('hidden');
  $('hostBlock').classList.remove('hidden');
  $('scanBtn').disabled=scanning;
}
function tog(i){
  const p=files[i].page;
  if(sel.has(p))sel.delete(p);else sel.add(p);
  renderPick();
}
function rowTog(e,i){
  if(e.target.closest('input,label,button'))return;
  tog(i);
}
function toggleKind(k){
  const group=files.filter(f=>f.kind===k);
  const all=group.every(f=>sel.has(f.page));
  group.forEach(f=>all?sel.delete(f.page):sel.add(f.page));
  renderPick();
}
function selectAll(on){
  if(on)files.forEach(f=>sel.add(f.page));else sel.clear();
  renderPick();
}
async function clearAll(){
  try{await post('clear')}catch(e){}
  files=[];sel.clear();title='';
  $('gtitle').textContent='No source';$('gsub').textContent='Paste a link to load a manifest';
  $('mPrimary').textContent='—';$('mMirror').textContent='—';$('startErr').textContent='';
  await poll();
}
async function browse(){try{const d=await post('browse');if(d.path)$('out').value=d.path.replace(/\//g,'\\')}catch(e){}}
async function start(){
  const chosen=files.filter(f=>sel.has(f.page));
  if(!chosen.length)return;
  $('startErr').textContent='';
  try{
    await post('start',{files:chosen,out:$('out').value,conns:+$('conns').value,title,mirror:$('mirror').checked});
    confirming=false;await poll();
  }catch(e){$('startErr').textContent=e.message}
}
function askCancel(on){confirming=on;if(last)render(last)}
async function doCancel(){confirming=false;try{await post('cancel')}catch(e){toast(e.message)}poll()}

/* ------------------------------------------------------------------ transfer */
function statusInfo(f){
  const key=f.status.split(' ')[0];
  const pc=f.total?Math.min(100,f.done/f.total*100):(FIN.includes(f.status)?100:0);
  let cls='queued',label='Queued';
  if(key==='downloading'){cls='active';label=`Active ${Math.floor(pc)}%`}
  else if(key==='getting'){cls='link';label='Link'}
  else if(key==='retrying'){cls='retry';label=f.status.includes('mirror')||f.status.includes('datanodes')?'Via Mirror':'Retry'}
  else if(key==='paused'){cls='paused';label='Paused'}
  else if(key==='failed'){cls='failed';label='Failed'}
  else if(key==='done'){cls='done';label='Done'}
  else if(key==='skipped'){cls='done';label='Skip'}
  return {key,cls,label,pc,live:key==='downloading'||key==='getting'};
}
function renderTransfer(s){
  const n=s.files.length;
  const fin=s.files.filter(f=>FIN.includes(f.status)).length;
  const failed=s.files.filter(f=>f.status==='failed').length;
  const paused=s.files.some(f=>f.status==='paused');
  const unfinished=n-fin;
  let rows='';
  s.files.forEach((f,i)=>{
    const st=statusInfo(f);
    const via=f.via==='datanodes';
    rows+=`<tr class="frow ${st.live?'live':''} ${via?'via-dn':''}">
      <td class="c-ck"><span class="tick ${st.cls}" aria-hidden="true"></span></td>
      <td class="c-idx">${pad(i+1)}</td>
      <td class="c-name"><span class="nm" title="${esc(f.name)}">${esc(f.name)}</span>
        <div class="minibar"><i style="width:${st.pc}%"></i></div>
        ${f.error&&f.status==='failed'?`<div class="rowerr">${esc(f.error)}</div>`:''}</td>
      <td class="c-kind">${kindName(f.kind||'main')}</td>
      <td class="c-mirror">${via?'MIR':(f.mirror?'PRI+MIR':'PRI')}</td>
      <td class="c-state"><span class="stamp ${st.cls}">${st.label}</span></td>
    </tr>`;
  });
  $('rows').innerHTML=rows;
  $('thead').classList.remove('hidden');
  $('empty').classList.add('hidden');
  $('paneLabel').textContent='Transfer';
  $('paneCount').textContent=`${fin}/${n} done`;
  $('footLeft').textContent=`${fin} done${failed?` · ${failed} failed`:''}${paused?' · paused':''}`;
  $('footRight').textContent=`${fmt(s.done)} / ${fmt(s.total)}`;
  $('pickTools').classList.add('hidden');
  $('runTools').classList.remove('hidden');
  $('setupBlock').classList.add('hidden');
  $('noteBlock').classList.add('hidden');
  $('hostBlock').classList.add('hidden');
  $('scanBtn').disabled=true;
  $('liveWord').classList.toggle('hidden',!s.running);
  $('newBtn').classList.toggle('hidden',s.running||s.cancelling);
  if(s.title)$('gtitle').textContent=s.title;
  if(s.out)$('out').value=s.out;
  $('rFolder').textContent=s.out?(s.out.split(/[\\/]/).filter(Boolean).pop()||s.out):'—';
  $('rFolder').title=s.out||'';

  const frac=n?(fin+s.files.filter(f=>f.total&&!FIN.includes(f.status)).reduce((a,f)=>a+f.done/f.total,0))/n:0;
  const pct=Math.max(0,Math.min(100,Math.floor(frac*100)));
  $('pct').innerHTML=`${pct}<small>%</small>`;
  $('rState').textContent=s.cancelling?'Deleting':s.running?'Running':unfinished?'Halted':'Complete';
  $('rSpeed').textContent=s.running&&s.speed?fmt(s.speed)+'/s':'—';
  $('rEta').textContent=s.running&&s.speed&&s.files.every(f=>f.total||FIN.includes(f.status))?tm((s.total-s.done)/s.speed)||'—':'—';
  $('rBytes').textContent=`${fmt(s.done)} / ${fmt(s.total)}`;

  $('statusWord').textContent=s.cancelling?'Deleting':s.running?'Transferring':unfinished?'Halted':'Complete';
  $('statusNum').textContent=`${fmt(s.done)} / ${fmt(s.total)}`;
  $('tbar').style.width=(frac*100)+'%';

  $('pauseBtn').disabled=!s.running||s.cancelling;
  $('resumeBtn').disabled=s.cancelling||(s.running?!failed:!unfinished);
  $('resumeBtn').textContent=failed&&!paused?'Retry failed':'Resume';
  $('openBtn').disabled=!s.out||s.cancelling;
  const canCancel=unfinished>0&&!s.cancelling;
  if(!canCancel)confirming=false;
  $('cancelBtn').classList.toggle('hidden',!canCancel||confirming);
  $('confirm').classList.toggle('hidden',!confirming);
  $('confirmText').textContent=`Stop and delete the ${n} file${n===1?'':'s'} from this download?`;
  $('startBtn').disabled=true;
}
function render(s){
  last=s;
  const has=s.files.length;
  $('readout').classList.toggle('hidden',!has);
  $('startWrap').classList.toggle('hidden',!!has);
  $('runWrap').classList.toggle('hidden',!has);
  if(has){
    renderTransfer(s);
  }else{
    renderPick();
    $('statusWord').textContent='Ready';
    $('statusNum').textContent='0 B';
    $('tbar').style.width='0%';
    $('startBtn').disabled=!sel.size;
    if(s.msg)toast(s.msg);
  }
}
async function poll(){
  clearTimeout(timer);
  try{
    const s=await (await fetch('/api/status',{headers:{'X-Token':TOKEN}})).json();
    render(s);
  }catch(e){}
  timer=setTimeout(poll,busy()?1000:2500);
}

/* ------------------------------------------------------------------ boot */
const grow=()=>{const t=$('url');t.style.height='auto';t.style.height=Math.min(t.scrollHeight+2,128)+'px'};
$('url').addEventListener('input',grow);
$('url').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();doScan()}});
$('themeBtn').textContent=theme==='light'?'Dark':'Light';
$('conns').addEventListener('change',()=>bump(0));
window.addEventListener('resize',()=>{const c=cols();document.querySelectorAll('.band td').forEach(td=>{td.colSpan=c})});
poll();
</script>
</body>
</html>
"""


def main():
    global PORT
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    try:
        import webview  # pywebview: real app window
        webview.create_window("FG Downloader", url, width=1080, height=860, min_size=(640, 600),
                              background_color="#0b0d10")
        webview.start()
    except Exception:
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    engine.stop.set()
    time.sleep(1.5)  # let downloads save their resume state


if __name__ == "__main__":
    main()
