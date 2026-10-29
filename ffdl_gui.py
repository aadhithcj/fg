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
        self.conns = 8
        self.msg = ""

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, files, out, conns):
        if self.running:
            return
        Path(out).mkdir(parents=True, exist_ok=True)
        self.out, self.conns, self.msg = out, max(1, int(conns)), ""
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
        return {"running": self.running, "files": out, "speed": speed, "done": tot_done,
                "total": tot_all, "out": self.out, "msg": self.msg}


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
                engine.start(body["files"], body["out"], body.get("conns", 8))
                return self.send(200, "{}")
            if self.path == "/api/pause":
                engine.stop.set()
                return self.send(200, "{}")
            if self.path == "/api/resume":
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
:root{--bg:#0b0d10;--panel:#13161b;--panel2:#1a1e25;--line:#252a33;--text:#e7eaf0;--mute:#8a93a3;
--accent:#7c5cff;--accent2:#22d3ee;--ok:#34d399;--warn:#fbbf24;--bad:#f87171;--r:14px}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#1d1640 0,transparent 60%),var(--bg);
color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif;min-height:100vh}
.wrap{max-width:1000px;margin:0 auto;padding:32px 16px 80px}
header{display:flex;align-items:center;gap:12px;margin-bottom:28px}
.logo{width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;font-weight:700}
h1{font-size:20px;margin:0}header p{margin:0;color:var(--mute);font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;margin-bottom:16px}
.row{display:flex;gap:10px;flex-wrap:wrap}
input[type=text],input[type=number],textarea{flex:1;min-width:0;background:var(--panel2);border:1px solid var(--line);color:var(--text);
border-radius:10px;padding:11px 13px;font:inherit;outline:none}input:focus,textarea:focus{border-color:var(--accent)}
textarea{resize:vertical;min-height:46px}
button{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px 16px;font:inherit;font-weight:600;cursor:pointer}
button:hover{border-color:#3a4150}button.primary{background:linear-gradient(135deg,var(--accent),#5b8cff);border:0}
button.primary:hover{filter:brightness(1.1)}button:disabled{opacity:.45;cursor:default}
.title{font-size:18px;font-weight:700;margin:0 0 4px}.sub{color:var(--mute);font-size:13px}
.groups{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
.chip{padding:6px 12px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);cursor:pointer;font-size:13px;user-select:none}
.chip.on{border-color:var(--accent);background:#211b45}
.list{max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.item{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--line);font-family:'JetBrains Mono',monospace;font-size:12.5px}
.item:last-child{border-bottom:0}.item input{accent-color:var(--accent)}
.tag{margin-left:auto;font-family:Inter;font-size:11px;color:var(--mute);border:1px solid var(--line);border-radius:6px;padding:1px 7px}
label.lab{display:block;color:var(--mute);font-size:12px;margin:14px 0 6px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.stat{background:var(--panel2);border-radius:10px;padding:12px}.stat b{display:block;font-size:18px}.stat span{color:var(--mute);font-size:12px}
.bar{height:8px;background:var(--panel2);border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .6s}
.big .bar{height:12px;margin-bottom:16px}
.dl{padding:11px 0;border-bottom:1px solid var(--line)}.dl:last-child{border:0}
.dlh{display:flex;gap:10px;align-items:baseline;margin-bottom:6px}
.dlh .n{font-family:'JetBrains Mono',monospace;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.dlh .m{color:var(--mute);font-size:12px;white-space:nowrap}
.st{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;background:var(--panel2);color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.st.downloading{color:var(--accent2)}.st.done,.st.skipped{color:var(--ok)}.st.failed{color:var(--bad)}.st.paused{color:var(--warn)}
.done .bar i{background:var(--ok)}.failed .bar i{background:var(--bad)}
.err{color:var(--bad);font-size:12px;margin-top:4px}.msg{color:var(--mute);margin-top:8px}
.hidden{display:none}.spin{display:inline-block;width:14px;height:14px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(1turn)}}
@media(max-width:640px){.stats{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="wrap">
<header><div class="logo">FF</div><div><h1>FF Downloader</h1><p>Paste a FitGirl page link (or FuckingFast links) and go.</p></div></header>

<div class="card"><div class="row" style="align-items:flex-start">
<textarea id="url" rows="1" placeholder="https://fitgirl-repacks.site/game-name/  (or FuckingFast links, one per line)"></textarea>
<button class="primary" id="scanBtn" onclick="doScan()">Fetch files</button></div>
<div id="scanErr" class="err"></div></div>

<div class="card hidden" id="pick">
<p class="title" id="gtitle"></p><div class="sub" id="gsub"></div>
<div class="groups" id="groups"></div>
<div class="list" id="list"></div>
<label class="lab">Save to</label>
<div class="row"><input type="text" id="out"><button onclick="browse()">Browse…</button></div>
<label class="lab">Connections per file</label>
<div class="row" style="align-items:center"><input type="number" id="conns" value="8" min="1" max="32" style="flex:0 0 90px">
<span class="sub">More = faster, until the site starts refusing. 8–16 is a good range.</span></div>
<div class="row" style="margin-top:16px"><button class="primary" onclick="start()" id="startBtn">Start download</button></div>
</div>

<div class="card hidden big" id="prog">
<div class="stats">
<div class="stat"><b id="sSpeed">–</b><span>Speed</span></div>
<div class="stat"><b id="sDone">–</b><span>Downloaded</span></div>
<div class="stat"><b id="sEta">–</b><span>Time left</span></div>
<div class="stat"><b id="sFiles">–</b><span>Files</span></div></div>
<div class="bar"><i id="tbar"></i></div>
<div class="row" style="margin-bottom:8px">
<button id="pauseBtn" onclick="api('pause')">Pause</button>
<button id="resumeBtn" onclick="api('resume')">Resume / retry failed</button>
<button onclick="api('open')">Open folder</button></div>
<div id="msg" class="msg"></div>
<div id="dls"></div></div>
</div>
<script>
let files=[], sel=new Set();
const TOKEN='__TOKEN__';
const $=id=>document.getElementById(id);
const fmt=b=>{if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+' '+u[i]};
const tm=s=>{if(!isFinite(s)||s<=0)return'–';s=Math.round(s);const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}h ${m}m`:m?`${m}m ${s%60}s`:`${s}s`};
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);
async function post(p,b){const r=await fetch('/api/'+p,{method:'POST',headers:{'X-Token':TOKEN},body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok)throw new Error(j.error||'Error');return j}
async function api(p){try{await post(p)}catch(e){}}
async function doScan(){const b=$('scanBtn');$('scanErr').textContent='';b.disabled=true;b.innerHTML='<span class="spin"></span> Fetching';
try{const d=await post('scan',{url:$('url').value});files=d.files;sel=new Set(files.filter(f=>f.kind!='optional').map(f=>f.page));
$('gtitle').textContent=d.title;$('out').value=d.out;
renderPick();$('pick').classList.remove('hidden')}catch(e){$('scanErr').textContent=e.message}
b.disabled=false;b.textContent='Fetch files'}
function renderPick(){const kinds=[['main','Main parts'],['selective','Selective'],['optional','Optional / languages']].filter(k=>files.some(f=>f.kind==k[0]));
$('groups').innerHTML=kinds.map(([k,l])=>{const fs=files.filter(f=>f.kind==k),on=fs.every(f=>sel.has(f.page));
return `<span class="chip ${on?'on':''}" onclick="toggleKind('${k}')">${on?'✓ ':''}${l} (${fs.length})</span>`}).join('');
$('list').innerHTML=files.map((f,i)=>`<label class="item"><input type="checkbox" ${sel.has(f.page)?'checked':''} onchange="tog(${i})">${esc(f.name)}<span class="tag">${f.kind}</span></label>`).join('');
$('gsub').textContent=`${sel.size} of ${files.length} files selected`}
function tog(i){const p=files[i].page;sel.has(p)?sel.delete(p):sel.add(p);renderPick()}
function toggleKind(k){const fs=files.filter(f=>f.kind==k),on=fs.every(f=>sel.has(f.page));fs.forEach(f=>on?sel.delete(f.page):sel.add(f.page));renderPick()}
async function browse(){const d=await post('browse');if(d.path)$('out').value=d.path.replace(/\//g,'\\')}
async function start(){const chosen=files.filter(f=>sel.has(f.page));if(!chosen.length)return;
try{await post('start',{files:chosen,out:$('out').value,conns:+$('conns').value});$('prog').classList.remove('hidden');$('prog').scrollIntoView({behavior:'smooth'})}catch(e){alert(e.message)}}
async function poll(){try{const s=await (await fetch('/api/status',{headers:{'X-Token':TOKEN}})).json();if(s.files.length){$('prog').classList.remove('hidden');
const left=s.files.filter(f=>!['done','skipped'].includes(f.status));
$('sSpeed').textContent=fmt(s.speed)+'/s';$('sDone').textContent=fmt(s.done)+(s.total?' / '+fmt(s.total):'');
const known=s.files.every(f=>f.total||['done','skipped'].includes(f.status));
$('sEta').textContent=s.running&&known&&s.speed?tm((s.total-s.done)/s.speed):'–';
$('sFiles').textContent=`${s.files.length-left.length} / ${s.files.length}`;
const allTotal=s.files.reduce((a,f)=>a+(f.total||0),0);
$('tbar').style.width=(s.files.length?((s.files.length-left.length)+s.files.filter(f=>f.total&&!['done','skipped'].includes(f.status)).reduce((a,f)=>a+f.done/f.total,0))/s.files.length*100:0)+'%';
$('pauseBtn').disabled=!s.running;$('resumeBtn').disabled=s.running&&!s.files.some(f=>f.status=='failed');
$('msg').textContent=s.msg;$('startBtn').disabled=s.running;
$('dls').innerHTML=s.files.map(f=>{const pc=f.total?f.done/f.total*100:(['done','skipped'].includes(f.status)?100:0);
const meta=f.status=='downloading'?`${fmt(f.done)} / ${fmt(f.total)} · ${fmt(f.speed)}/s · ${tm((f.total-f.done)/f.speed)}`:f.total?fmt(f.total):'';
return `<div class="dl ${f.status}"><div class="dlh"><span class="n">${esc(f.name)}</span><span class="m">${meta}</span><span class="st ${f.status.split(' ')[0]}">${f.status}</span></div>
<div class="bar"><i style="width:${pc}%"></i></div>${f.error&&f.status!='done'?`<div class="err">${esc(f.error)}</div>`:''}</div>`}).join('')}}catch(e){}
setTimeout(poll,1000)}
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
