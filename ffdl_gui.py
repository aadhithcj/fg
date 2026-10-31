#!/usr/bin/env python3
"""
FF Downloader - web UI for ffdl.py

Run:   python ffdl_gui.py      (keep ffdl.py in the same folder)
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
import ffdl

PORT = 0                            # set in main()
TOKEN = secrets.token_urlsafe(16)   # the page sends it back on every API call
SETTINGS_FILE = ffdl.STATE_DIR / "gui.json"
DEFAULT_BASE = Path.home() / "Downloads"
sess = ffdl.Session()


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(**kw):
    try:
        ffdl.STATE_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps({**load_settings(), **kw}, indent=1))
    except OSError:
        pass


# --------------------------------------------------------------------------- scanning
def scan(text):
    # links pasted back to back (no line break between them) get split apart again
    text = re.sub(r"(?<=\S)(?=https?://)", "\n", text.strip())
    if ffdl.PAGE_RE.search(text):
        title = "Links"
    else:
        r = sess.s.get(text, timeout=30)
        r.raise_for_status()
        text = r.text
        t = re.search(r'<h1 class="entry-title">(.*?)</h1>', text, re.S) or ffdl.TITLE_RE.search(text)
        title = re.sub(r"<[^>]+>", "", ffdl.htmllib.unescape(t.group(1))).strip() if t else "Download"
        title = title.split(" – ")[0].split(" - FitGirl")[0].strip()
    files, seen = [], set()
    for m in ffdl.PAGE_RE.findall(ffdl.htmllib.unescape(text)):
        it = ffdl.Item(m)
        if it.page in seen:
            continue
        seen.add(it.page)
        name = it.name or it.id
        kind = "optional" if "optional" in name.lower() else ("selective" if "selective" in name.lower() else "main")
        files.append({"page": m, "name": name, "kind": kind})
    if not files:
        raise RuntimeError("No FuckingFast links found on that page.")
    folder = re.sub(r'[<>:"/\\|?*]', "", title).strip()[:80] or "Download"
    out = Path(load_settings().get("base") or DEFAULT_BASE) / folder
    return {"title": title, "files": files, "out": str(out)}


# --------------------------------------------------------------------------- engine
class Progress:
    """Stands in for ffdl's tqdm bar: progress goes into the file's status dict."""
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
        self.msg = ""
        self.cancelling = False

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, files, out, conns, title=""):
        if self.running or self.cancelling:
            return
        Path(out).mkdir(parents=True, exist_ok=True)
        self.out, self.conns, self.msg, self.title = out, max(1, int(conns)), "", title
        save_settings(base=str(Path(out).parent))
        self.files = [{"page": f["page"], "name": f["name"], "status": "queued", "done": 0, "total": 0,
                       "error": "", "speed": 0.0, "_s": []} for f in files]
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
                except ffdl.NeedBrowser:
                    f["status"], f["error"] = "failed", "Blocked by Cloudflare - try again later, or use ffdl.py --browser"
                    break
                except Exception as e:
                    if self.stop.is_set():
                        break
                    f["error"] = str(e)
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
        item = ffdl.Item(f["page"])
        f["status"] = "getting link"

        def make_bar(total, done):
            f["name"], f["status"] = item.name, "downloading"
            return Progress(f, total, done)

        status = ffdl.download_item(sess, item, out, self.conns, make_bar, self.stop)
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
            name = ffdl.safe_name(f["name"])
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
            out.append({k: v for k, v in f.items() if k != "_s"})
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
            return self.send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
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
                if not all(ffdl.PAGE_RE.fullmatch(f["page"]) for f in body["files"]):
                    raise ValueError("Only FuckingFast links can be downloaded.")
                engine.start(body["files"], body["out"], body.get("conns", 8), body.get("title", ""))
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
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FF Downloader</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0c0e12;--panel:#14171d;--raise:#1b1f27;--sunk:#111419;--line:#262b35;--line-strong:#363d4a;
--text:#e8ebf1;--mute:#9aa2b1;--faint:#8a92a2;--accent:#6d4dff;--accent-hi:#8466ff;--accent-soft:rgba(124,92,255,.16);
--ok:#34d399;--warn:#fbbf24;--bad:#f87171;--info:#38d6f0;--r:14px;--r-sm:9px;--ease:cubic-bezier(.16,1,.3,1);
--mono:'JetBrains Mono',ui-monospace,Consolas,monospace;color-scheme:dark}
*{box-sizing:border-box}
html{scrollbar-color:var(--line-strong) transparent;scrollbar-width:thin}
body{margin:0;background:radial-gradient(900px 480px at 0% -8%,rgba(109,77,255,.13),transparent 70%),var(--bg);
color:var(--text);font:14px/1.5 Inter,'Segoe UI',system-ui,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
::selection{background:rgba(124,92,255,.4);color:#fff}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line-strong);border-radius:10px;border:3px solid transparent;background-clip:padding-box}
:focus-visible{outline:2px solid var(--accent-hi);outline-offset:2px}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 72px}
header{display:flex;align-items:center;gap:12px;margin-bottom:22px}
.mark{width:36px;height:36px;border-radius:10px;background:var(--accent);display:grid;place-items:center;color:#fff;box-shadow:0 6px 18px -6px rgba(109,77,255,.7)}
.mark svg{width:20px;height:20px}
h1{font-size:18px;font-weight:650;margin:0;letter-spacing:-.01em}
header p{margin:1px 0 0;color:var(--mute);font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:20px;margin-bottom:14px}
h2{font-size:15px;font-weight:600;margin:0;letter-spacing:-.005em}
.sub{color:var(--mute);font-size:13px}
.row{display:flex;gap:10px}
input[type=text],input[type=number],textarea{flex:1;min-width:0;background:var(--raise);border:1px solid var(--line);color:var(--text);
border-radius:var(--r-sm);padding:10px 12px;font:inherit;outline:none;caret-color:var(--accent-hi);transition:border-color .15s,box-shadow .15s}
input:hover,textarea:hover{border-color:var(--line-strong)}
input:focus,textarea:focus{border-color:var(--accent-hi);box-shadow:0 0 0 3px var(--accent-soft)}
::placeholder{color:var(--faint)}
textarea{resize:vertical;min-height:44px;line-height:1.45}
.hint{color:var(--mute);font-size:12.5px;margin-top:8px}
button{display:inline-flex;align-items:center;justify-content:center;gap:7px;background:var(--raise);color:var(--text);border:1px solid var(--line);
border-radius:var(--r-sm);padding:9px 14px;font:inherit;font-weight:550;cursor:pointer;white-space:nowrap;transition:background .15s,border-color .15s,color .15s}
button:hover:not(:disabled){border-color:var(--line-strong);background:#20252e}
button:active:not(:disabled){transform:translateY(1px)}
button:disabled{opacity:.42;cursor:default}
button.primary{background:var(--accent);border-color:transparent;color:#fff}
button.primary:hover:not(:disabled){background:var(--accent-hi)}
button.danger{color:var(--bad);background:transparent;border-color:rgba(248,113,113,.35)}
button.danger:hover:not(:disabled){background:rgba(248,113,113,.1);border-color:rgba(248,113,113,.6)}
button.danger.solid{background:#c93f3f;border-color:transparent;color:#fff}
button.danger.solid:hover:not(:disabled){background:#d94c4c}
.spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:s .7s linear infinite}
@keyframes s{to{transform:rotate(1turn)}}
.err{color:var(--bad);font-size:13px;margin-top:8px}.err:empty{display:none}
.hidden{display:none!important}
.pickhead{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:14px}
.pickhead h2{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pickhead .sub{flex:none;font-variant-numeric:tabular-nums}
.groups{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.chip{padding:6px 11px 6px 8px;border-radius:999px;font-size:13px;font-weight:500}
.chip .box{width:16px;height:16px;border-radius:5px;border:1.5px solid var(--line-strong);display:grid;place-items:center;color:transparent;transition:background .15s,border-color .15s}
.chip .box svg{width:12px;height:12px}
.chip.on{border-color:rgba(124,92,255,.55);background:var(--accent-soft)}
.chip.on:hover:not(:disabled){background:rgba(124,92,255,.24);border-color:rgba(124,92,255,.7)}
.chip.on .box{background:var(--accent);border-color:var(--accent);color:#fff}
.chip .n{color:var(--mute);font-variant-numeric:tabular-nums}
.list{max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--sunk)}
.item{display:flex;align-items:center;gap:10px;padding:7px 12px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:12.5px;cursor:pointer}
.item:last-child{border-bottom:0}.item:hover{background:#161a21}
.item input{accent-color:var(--accent);width:15px;height:15px;margin:0;flex:none}
.item .fn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag{margin-left:auto;font-family:Inter,sans-serif;font-size:11px;color:var(--mute);border:1px solid var(--line);border-radius:6px;padding:1px 7px;flex:none}
.fields{display:grid;grid-template-columns:minmax(0,1fr) 160px;gap:16px;margin-top:18px}
label.lab{display:block;color:var(--mute);font-size:12.5px;font-weight:500;margin-bottom:6px}
.startrow{display:flex;align-items:center;gap:14px;margin-top:20px}
.dhead{display:flex;align-items:flex-end;justify-content:space-between;gap:16px}
.dhead h2{font-size:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.state{color:var(--mute);font-size:13px;margin-top:3px;font-variant-numeric:tabular-nums}
.pct{font-size:30px;font-weight:650;letter-spacing:-.03em;line-height:1;font-variant-numeric:tabular-nums;flex:none}
.pct small{font-size:15px;color:var(--mute);font-weight:500;margin-left:2px;letter-spacing:0}
.bar{height:6px;background:var(--raise);border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--accent-hi);border-radius:inherit;transition:width .8s var(--ease)}
.overall{margin:14px 0 16px}.overall.complete i{background:var(--ok)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding-bottom:14px;border-bottom:1px solid var(--line)}
.toolbar .grow{flex:1}
.confirm{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.confirm span{font-size:13px;margin-right:4px}
.msg{color:var(--mute);font-size:13.5px}.msg:empty{display:none}
#dls{margin-top:6px}
.dl{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:6px 14px;align-items:center;padding:11px 10px;margin:0 -10px;border-radius:var(--r-sm);border-top:1px solid var(--line)}
.dl:first-child{border-top-color:transparent}
.dl.downloading,.dl.downloading+.dl{border-top-color:transparent}
.dl.downloading{background:#181c24}
.dl .n{font-family:var(--mono);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dl .m{color:var(--mute);font-size:12.5px;white-space:nowrap;font-variant-numeric:tabular-nums}
.dl .bar{grid-column:1/-1;height:4px}
.st{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;background:var(--raise);color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.st.downloading{color:var(--info);background:rgba(56,214,240,.1)}
.st.done,.st.skipped{color:var(--ok);background:rgba(52,211,153,.1)}
.st.failed{color:var(--bad);background:rgba(248,113,113,.1)}
.st.paused,.st.retrying{color:var(--warn);background:rgba(251,191,36,.1)}
.dl.done .bar i,.dl.skipped .bar i{background:var(--ok)}.dl.failed .bar i{background:var(--bad)}.dl.paused .bar i{background:var(--warn)}
.dl .err{grid-column:1/-1;margin-top:0}
@media(max-width:640px){.fields{grid-template-columns:1fr}.pct{font-size:24px}.startrow{flex-direction:column;align-items:flex-start}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body><div class="wrap">
<header><div class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/></svg></div>
<div><h1>FF Downloader</h1><p>Paste a FitGirl page link (or FuckingFast links) and go.</p></div></header>

<section class="card">
<div class="row" style="align-items:flex-start">
<textarea id="url" rows="1" aria-label="Game page or links" placeholder="https://fitgirl-repacks.site/game-name/"></textarea>
<button class="primary" id="scanBtn" onclick="doScan()">Fetch files</button></div>
<div class="hint">A FitGirl game page, or FuckingFast links one per line. Press Enter to fetch.</div>
<div id="scanErr" class="err" role="alert"></div></section>

<section class="card hidden" id="pick">
<div class="pickhead"><h2 id="gtitle"></h2><span class="sub" id="gsub"></span></div>
<div class="groups" id="groups"></div>
<div class="list" id="list"></div>
<div class="fields">
<div><label class="lab" for="out">Save to</label><div class="row"><input type="text" id="out"><button onclick="browse()">Browse…</button></div></div>
<div><label class="lab" for="conns">Connections per file</label><input type="number" id="conns" value="8" min="1" max="32" style="width:100%"></div></div>
<div class="startrow"><button class="primary" onclick="start()" id="startBtn">Start download</button>
<span class="sub">8–16 connections is a good range. More is faster until the site starts refusing.</span></div>
<div id="startErr" class="err" role="alert"></div></section>

<section class="card hidden" id="prog" aria-live="polite">
<div class="dhead"><div style="min-width:0"><h2 id="dtitle">Downloads</h2><div class="state" id="dstate"></div></div>
<div class="pct" id="pct"></div></div>
<div class="bar overall" id="obar"><i id="tbar"></i></div>
<div class="toolbar" id="toolbar">
<button id="pauseBtn" onclick="api('pause')">Pause</button>
<button id="resumeBtn" onclick="api('resume')">Resume</button>
<button id="openBtn" onclick="api('open')">Open folder</button>
<span class="grow"></span>
<button class="danger" id="cancelBtn" onclick="askCancel(true)">Cancel download</button>
<div class="confirm hidden" id="confirm"><span id="confirmText"></span>
<button onclick="askCancel(false)">Keep downloading</button>
<button class="danger solid" onclick="doCancel()">Delete files</button></div></div>
<div id="msg" class="msg"></div>
<div id="dls"></div></section>
</div>
<script>
let files=[],sel=new Set(),title='',confirming=false,last=null,timer;
const TOKEN='__TOKEN__';
const FIN=['done','skipped'];
const $=id=>document.getElementById(id);
const fmt=b=>{if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+' '+u[i]};
const tm=s=>{if(!isFinite(s)||s<=0)return'';s=Math.round(s);const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}h ${m}m`:m?`${m}m ${s%60}s`:`${s}s`};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const CHECK='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const busy=()=>last&&(last.running||last.cancelling);
async function post(p,b){const r=await fetch('/api/'+p,{method:'POST',headers:{'X-Token':TOKEN},body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok)throw new Error(j.error||'Something went wrong. Try again.');return j}
async function api(p){try{await post(p);poll()}catch(e){$('msg').textContent=e.message}}
async function doScan(){const b=$('scanBtn');if(b.disabled)return;$('scanErr').textContent='';b.disabled=true;b.innerHTML='<span class="spin"></span>Fetching';
try{const d=await post('scan',{url:$('url').value});files=d.files;title=d.title;sel=new Set(files.filter(f=>f.kind!='optional').map(f=>f.page));
$('gtitle').textContent=d.title;$('out').value=d.out;renderPick();$('pick').classList.remove('hidden')}catch(e){$('scanErr').textContent=e.message}
b.disabled=false;b.textContent='Fetch files'}
function renderPick(){const kinds=[['main','Main parts'],['selective','Selective'],['optional','Optional / languages']].filter(k=>files.some(f=>f.kind==k[0]));
$('groups').innerHTML=kinds.map(([k,l])=>{const fs=files.filter(f=>f.kind==k),on=fs.every(f=>sel.has(f.page));
return `<button type="button" class="chip ${on?'on':''}" aria-pressed="${on}" onclick="toggleKind('${k}')"><span class="box">${CHECK}</span>${l}<span class="n">${fs.length}</span></button>`}).join('');
$('list').innerHTML=files.map((f,i)=>`<label class="item"><input type="checkbox" ${sel.has(f.page)?'checked':''} onchange="tog(${i})"><span class="fn" title="${esc(f.name)}">${esc(f.name)}</span><span class="tag">${f.kind}</span></label>`).join('');
$('gsub').textContent=`${sel.size} of ${files.length} files selected`;$('startBtn').disabled=!sel.size||busy()}
function tog(i){const p=files[i].page;sel.has(p)?sel.delete(p):sel.add(p);renderPick()}
function toggleKind(k){const fs=files.filter(f=>f.kind==k),on=fs.every(f=>sel.has(f.page));fs.forEach(f=>on?sel.delete(f.page):sel.add(f.page));renderPick()}
async function browse(){try{const d=await post('browse');if(d.path)$('out').value=d.path.replace(/\//g,'\\')}catch(e){}}
async function start(){const chosen=files.filter(f=>sel.has(f.page));if(!chosen.length)return;$('startErr').textContent='';
try{await post('start',{files:chosen,out:$('out').value,conns:+$('conns').value,title});confirming=false;await poll();$('prog').scrollIntoView({behavior:'smooth',block:'start'})}catch(e){$('startErr').textContent=e.message}}
function askCancel(on){confirming=on;if(last)render(last)}
async function doCancel(){confirming=false;try{await post('cancel')}catch(e){$('msg').textContent=e.message}poll()}
function render(s){
const n=s.files.length,show=n||s.msg||s.cancelling;$('prog').classList.toggle('hidden',!show);
$('startBtn').disabled=!sel.size||s.running||s.cancelling;if(!show)return;
const fin=s.files.filter(f=>FIN.includes(f.status)).length,failed=s.files.filter(f=>f.status=='failed').length,
paused=s.files.some(f=>f.status=='paused'),unfinished=n-fin;
const known=s.files.every(f=>f.total||FIN.includes(f.status)),eta=s.running&&known&&s.speed?tm((s.total-s.done)/s.speed):'';
let state='';
if(s.cancelling)state='Cancelling and deleting files…';
else if(n&&s.running)state=[`File ${Math.min(fin+1,n)} of ${n}`,s.speed?fmt(s.speed)+'/s':'',eta&&eta+' left',fmt(s.done)+(s.total?' of '+fmt(s.total):'')].filter(Boolean).join(' · ');
else if(n&&!unfinished)state=`All ${n} files done · ${fmt(s.total)}`;
else if(n)state=[`${fin} of ${n} files done`,failed&&`${failed} failed`,paused&&'Paused'].filter(Boolean).join(' · ');
$('dtitle').textContent=n||s.cancelling?(s.title||'Downloads'):'Downloads';$('dstate').textContent=state;
const frac=n?(fin+s.files.filter(f=>f.total&&!FIN.includes(f.status)).reduce((a,f)=>a+f.done/f.total,0))/n:0;
$('pct').innerHTML=n?`${Math.floor(frac*100)}<small>%</small>`:'';$('tbar').style.width=frac*100+'%';
$('obar').classList.toggle('hidden',!n);$('obar').classList.toggle('complete',n>0&&!unfinished);$('toolbar').classList.toggle('hidden',!n);
$('pauseBtn').disabled=!s.running||s.cancelling;
$('resumeBtn').disabled=s.cancelling||(s.running?!failed:!unfinished);
$('resumeBtn').textContent=failed&&!paused?'Retry failed':'Resume';
$('openBtn').disabled=!s.out||s.cancelling;
const canCancel=unfinished>0&&!s.cancelling;if(!canCancel)confirming=false;
$('cancelBtn').classList.toggle('hidden',!canCancel||confirming);$('confirm').classList.toggle('hidden',!confirming);
$('confirmText').textContent=`Stop and delete the ${n} file${n==1?'':'s'} from this download?`;
$('msg').textContent=n?'':s.msg;
$('dls').innerHTML=s.files.map(f=>{const st=f.status.split(' ')[0],pc=f.total?f.done/f.total*100:(FIN.includes(f.status)?100:0);
const meta=f.status=='downloading'?`${fmt(f.done)} of ${fmt(f.total)}`+(f.speed?` · ${fmt(f.speed)}/s`:''):f.total?fmt(f.total):'';
return `<div class="dl ${st}"><span class="n" title="${esc(f.name)}">${esc(f.name)}</span><span class="m">${meta}</span><span class="st ${st}">${esc(f.status)}</span><div class="bar"><i style="width:${pc}%"></i></div>${f.error&&!FIN.includes(f.status)?`<div class="err">${esc(f.error)}</div>`:''}</div>`}).join('')}
async function poll(){clearTimeout(timer);try{const s=await (await fetch('/api/status',{headers:{'X-Token':TOKEN}})).json();last=s;render(s)}catch(e){}timer=setTimeout(poll,1000)}
const grow=()=>{const t=$('url');t.style.height='auto';t.style.height=Math.min(t.scrollHeight+2,160)+'px'};
$('url').addEventListener('input',grow);
$('url').addEventListener('keydown',e=>{if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();doScan()}});poll();
</script></body></html>"""


def main():
    global PORT
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    try:
        import webview  # pywebview: real app window
        webview.create_window("FF Downloader", url, width=1080, height=860, min_size=(640, 600),
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
