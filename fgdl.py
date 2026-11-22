#!/usr/bin/env python3
"""
fgdl - multi-threaded segmented file downloader

Features
  * Accepts direct host links, a text/HTML file containing them, or a page URL
    that lists them
  * Parallel downloads with progress bars, resume (.part files), retries
  * Session and challenge management
  * Multi-part archives: checks for missing parts and reports completion status

Setup
  pip install requests tqdm playwright curl_cffi
  python -m playwright install chromium

Usage
  python fgdl.py -i links.txt -o .
  python fgdl.py -i links.txt -o D:/Downloads
  python fgdl.py --browser -i links.txt -o .       # skip fast mode, use the browser
"""
import argparse
import html as htmllib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from tqdm import tqdm

try:
    from curl_cffi import requests as http
    HAVE_CFFI = True
except ImportError:
    import requests as http
    HAVE_CFFI = False

BASE = "https://fuckingfast.co/"
DL_RE = re.compile(r"https?://fuckingfast\.co/dl/[^\s\"'<>\\)]+")
PAGE_RE = re.compile(r"https?://fuckingfast\.co/(?!dl/)[A-Za-z0-9]+(?:#[^\s\"'<>]+)?")
DN_RE = re.compile(r"https?://(?:www\.)?datanodes\.to/(?!download(?:/|$))[A-Za-z0-9]+(?:/[^\s\"'<>]*)?")
OTHER_URL_RE = re.compile(r"https?://(?!(?:www\.)?(?:fuckingfast\.co|datanodes\.to))[^\s\"'<>]+")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
STATE_DIR = Path.home() / ".fgdl"
_LEGACY_DIR = Path.home() / ".ffdl"
if not STATE_DIR.exists() and _LEGACY_DIR.exists():
    try:
        import shutil
        shutil.copytree(_LEGACY_DIR, STATE_DIR)
    except Exception:
        pass
SESSION_FILE = STATE_DIR / "session.json"
PROFILE_DIR = STATE_DIR / "browser-profile"
CHUNK = 1 << 20
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class NeedBrowser(Exception):
    """Cloudflare keeps blocking the script; this file must go through the browser."""


class NoPlaywright(RuntimeError):
    """A browser window is needed but playwright isn't installed (e.g. in the GUI .exe)."""


# --------------------------------------------------------------------------- helpers
def _looks_like_challenge(title, body):
    t = (title or "").lower()
    return "just a moment" in t or "attention required" in t or "cf-chl" in (body or "")[:20000]


def is_challenge(resp):
    if resp.status_code not in (403, 429, 503):
        return False
    if "text/html" not in (resp.headers.get("Content-Type") or ""):
        return False
    body = resp.text
    t = TITLE_RE.search(body)
    return resp.headers.get("cf-mitigated") == "challenge" or _looks_like_challenge(t.group(1) if t else "", body)


def safe_name(n):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n or "").strip() or None


def launch_browser(p, channel, **extra):
    kw = dict(user_data_dir=str(PROFILE_DIR), headless=False, viewport=None, **extra)
    try:
        return p.chromium.launch_persistent_context(channel=channel or "chrome", **kw)
    except Exception:
        return p.chromium.launch_persistent_context(**kw)


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise NoPlaywright("Needs playwright: pip install playwright && python -m playwright install chromium")


def wait_past_challenge(page, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not _looks_like_challenge(page.title(), page.content()):
                return
        except Exception:
            pass
        time.sleep(1.5)
    raise RuntimeError("timed out waiting for the Cloudflare check")


# --------------------------------------------------------------------------- session
class Session:
    def __init__(self, channel=None, challenge_solver=None):
        self.s = http.Session(impersonate="chrome") if HAVE_CFFI else http.Session()
        self.ua = CHROME_UA
        self.s.headers.update({"User-Agent": self.ua, "Accept-Language": "en-US,en;q=0.9"})
        self.lock = threading.Lock()
        self.gen = 0
        self.blocked = False   # True once a fresh browser session still doesn't work
        self.channel = channel
        self.challenge_solver = challenge_solver
        if SESSION_FILE.exists():
            try:
                self._apply(json.loads(SESSION_FILE.read_text()))
            except Exception:
                pass

    def _apply(self, data):
        self.ua = data["user_agent"]
        self.s.headers["User-Agent"] = self.ua
        self.s.cookies.clear()
        for c in data["cookies"]:
            self.s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))

    def request(self, method, url, **kw):
        if self.blocked:
            raise NeedBrowser()
        gen = self.gen
        r = self.s.request(method, url, **kw)
        if is_challenge(r):
            r.close()
            self.refresh(kw.get("headers", {}).get("Referer") or url, gen)
            if self.blocked:
                raise NeedBrowser()
            r = self.s.request(method, url, **kw)
            if is_challenge(r):
                r.close()
                self.blocked = True
                raise NeedBrowser()
        return r

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def refresh(self, url, seen_gen):
        with self.lock:
            if seen_gen != self.gen or self.blocked:
                return  # another thread already handled it
            data = None
            if self.challenge_solver:
                data = self.challenge_solver(url)
                if not data:
                    raise NeedBrowser("Cloudflare check was cancelled or timed out")
            else:
                try:
                    sync_playwright = require_playwright()
                except NoPlaywright:
                    tqdm.write("\n>> Cloudflare check needed, but playwright isn't installed to open a browser.\n")
                    raise NeedBrowser("Blocked by Cloudflare challenge - try again later, or use fgdl.py --browser")
                tqdm.write("\n>> Cloudflare check: a browser window is opening. Complete the check there; "
                           "it closes by itself.\n")
                with sync_playwright() as p:
                    ctx = launch_browser(p, self.channel)
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded")
                    wait_past_challenge(page)
                    ua = page.evaluate("navigator.userAgent")
                    cookies = [{k: c[k] for k in ("name", "value", "domain", "path")}
                               for c in ctx.cookies() if "fuckingfast" in c["domain"] or "datanodes" in c["domain"]]
                    ctx.close()
                data = {"user_agent": ua, "cookies": cookies}
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps(data, indent=1))
            self._apply(data)
            self.gen += 1
            # check right away whether the script itself now gets through
            test = self.s.get(url, timeout=30)
            if is_challenge(test):
                self.blocked = True
                tqdm.write(">> Cloudflare still blocks the script.\n")
            else:
                tqdm.write(">> Passed. Continuing.\n")
            test.close()


# --------------------------------------------------------------------------- links
class Item:
    def __init__(self, page_url):
        self.page = page_url.split("#")[0]
        frag = urlparse(page_url).fragment
        self.host = "datanodes" if is_dn(self.page) else "fuckingfast"
        name = safe_name(unquote(frag)) if frag else None
        if not name and self.host == "datanodes":
            tail = unquote(urlparse(self.page).path.rstrip("/").rsplit("/", 1)[-1])
            name = safe_name(tail) if tail and not tail.isdigit() else None
        self.name = name
        self.id = self.page.rstrip("/").rsplit("/", 1)[-1]


def collect_links(args, sess):
    text = " ".join(args.links)
    if args.input:
        text += "\n" + Path(args.input).read_text(encoding="utf-8", errors="ignore")
    if not (PAGE_RE.search(text) or DN_RE.search(text)):
        for url in dict.fromkeys(OTHER_URL_RE.findall(text)):
            try:
                r = sess.s.get(url, timeout=30)
                r.raise_for_status()
                text += "\n" + r.text
                print(f"Scanned {url} for download links")
            except Exception as e:
                print(f"Could not open {url}: {e}\n  Save that page with Ctrl+S and pass the .html file with -i.")
    flat = htmllib.unescape(text)
    seen, out = set(), []
    for regex in (PAGE_RE, DN_RE):
        for m in regex.findall(flat):
            base = m.split("#")[0]
            if base.rstrip("/") == BASE.rstrip("/") or base in seen:
                continue
            seen.add(base)
            out.append(Item(m))
    return out


DEBUG_DIR = None  # set in main()
REL_DL_RE = re.compile(r"""["'](/dl/[^"'\s<>]+)["']""")
OPEN_RE = re.compile(r"""window\.open\(\s*["']([^"']+)["']""")
GO_RE = re.compile(r"""hx-(?:post|get)=["']([^"']+)["']""")
URL_RE = re.compile(r"""https?://[^\s"'<>\\]+""")


def page_name(item, body):
    if not item.name:
        t = TITLE_RE.search(body)
        item.name = safe_name(htmllib.unescape(t.group(1))) if t else None
        if item.host == "datanodes" and item.name:
            item.name = re.sub(r"^(?:datanodes|download)\s*[|–-]\s*", "", item.name, flags=re.I)
            item.name = re.sub(r"\s*[|–-]\s*(?:datanodes|download|free file).*$", "", item.name, flags=re.I)
            item.name = safe_name(item.name)
        item.name = item.name or f"{item.id}.bin"


SIZE_RE = re.compile(r"Size:\s*([\d.]+)\s*([KMGT]?B)", re.I)


def page_size(body):
    m = SIZE_RE.search(body)
    if not m:
        return None
    mult = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def dump_debug(item, text):
    if DEBUG_DIR:
        f = DEBUG_DIR / f"debug_{item.id}.txt"
        f.write_text(text, encoding="utf-8", errors="ignore")
        return f"saved details to {f} (send this file)"
    return "site layout may have changed"


def static_dl(item, body):
    """Old-style pages: the direct link is in the HTML itself."""
    clean = htmllib.unescape(body.replace("\\/", "/"))
    m = DL_RE.search(clean)
    if m:
        return m.group(0)
    m = REL_DL_RE.search(clean)
    if m:
        return "https://fuckingfast.co" + m.group(1)
    for u in OPEN_RE.findall(clean):
        if u.startswith("/") or is_ff(u):
            return u if u.startswith("http") else "https://fuckingfast.co" + u
    return None


def check_gone(body):
    low = body.lower()
    if "file not found" in low or "has been deleted" in low or "no longer available" in low:
        raise RuntimeError("file not found / deleted")


def urls_from_response(r):
    """Collect candidate URLs from an htmx response (headers first, then body)."""
    found = []
    for h in ("HX-Redirect", "HX-Location", "Location", "HX-Trigger", "HX-Trigger-After-Settle"):
        v = r.headers.get(h)
        if not v:
            continue
        if v.startswith("{"):
            try:
                j = json.loads(v)
                found += URL_RE.findall(json.dumps(j).replace("\\/", "/"))
                if isinstance(j.get("path"), str):
                    found.append(j["path"])
            except Exception:
                found += URL_RE.findall(v)
        else:
            found.append(v)
    body = r.text if "text" in (r.headers.get("Content-Type") or "") or not r.headers.get("Content-Type") else ""
    found += URL_RE.findall(htmllib.unescape(body.replace("\\/", "/")))
    out = []
    for u in found:
        u = u.strip()
        if u.startswith("/"):
            u = "https://fuckingfast.co" + u
        if u.startswith("http") and u not in out:
            out.append(u)
    return out


def is_ff(u):
    host = urlparse(u).hostname or ""
    return host == "fuckingfast.co" or host.endswith(".fuckingfast.co")


def is_dn(u):
    host = urlparse(u).hostname or ""
    return host == "datanodes.to" or host.endswith(".datanodes.to")


def is_supported(u):
    return is_ff(u) or is_dn(u)


def host_label(u):
    return "datanodes" if is_dn(u) else "fuckingfast" if is_ff(u) else "unknown"


def resolve_go(sess, item, body):
    """New-style pages: DOWNLOAD button does hx-post /f/<id>/go.
    First click returns an ad, second returns the file link."""
    m = GO_RE.search(body)
    go = m.group(1) if m else f"/f/{item.id}/go"
    go = go if go.startswith("http") else "https://fuckingfast.co" + go
    headers = {"HX-Request": "true", "HX-Current-URL": item.page, "HX-Target": "",
               "Referer": item.page, "Origin": "https://fuckingfast.co", "Accept": "*/*"}
    log = []
    for click in range(1, 4):
        r = sess.post(go, headers=headers, allow_redirects=False, timeout=30)
        cands = urls_from_response(r)
        log.append(f"--- click {click}: HTTP {r.status_code}\n" +
                   "\n".join(f"{k}: {v}" for k, v in r.headers.items()) +
                   "\n\n" + (r.text[:3000] if "text" in (r.headers.get("Content-Type") or "") else "<binary>"))
        r.close()
        if r.status_code == 404:
            raise RuntimeError("file not found (404) - link is dead")
        good = [u for u in cands if is_ff(u) and "/f/" not in urlparse(u).path
                and u.split("#")[0].rstrip("/") != item.page.rstrip("/")]
        if good:
            return good[0]
        time.sleep(1)
    raise RuntimeError("download button gave no file link - " + dump_debug(item, "\n\n".join(log)))


def resolve(sess, item):
    if item.host == "datanodes":
        return resolve_datanodes(sess, item)
    return resolve_fuckingfast(sess, item)


def resolve_fuckingfast(sess, item):
    r = sess.get(item.page, timeout=30)
    if r.status_code == 404:
        raise RuntimeError("file not found (404) - link is dead")
    r.raise_for_status()
    body = r.text
    r.close()
    page_name(item, body)
    check_gone(body)
    return static_dl(item, body) or resolve_go(sess, item, body)


# --------------------------------------------------------------------------- datanodes
FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
INPUT_RE = re.compile(r"<input\b([^>]*)>", re.I)
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
DN_BAD_HOSTS = ("challenges.cloudflare.com", "cloudflare.com", "google.com", "google-analytics.com",
                "googletagmanager.com", "doubleclick.net", "gstatic.com", "recaptcha.net")
DN_NEEDS_BROWSER = ("datanodes is asking for a browser check (Turnstile) - open this part in a browser, "
                    "or run the CLI with --browser")


def _attrs(tag):
    return {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None)
            for m in ATTR_RE.finditer(tag)}


def _dn_form(body):
    """First form carrying a download op, with its hidden fields already collected."""
    for m in FORM_RE.finditer(body):
        attrs = _attrs(m.group(1))
        fields = {}
        for im in INPUT_RE.finditer(m.group(2)):
            ia = _attrs(im.group(1))
            name = ia.get("name")
            if name and ia.get("type", "").lower() not in ("submit", "button", "image", "file", "checkbox", "radio"):
                fields[name] = ia.get("value", "")
        if "op" in fields or "method_free" in fields:
            return attrs.get("action") or "", fields
    return None


def _dn_direct(u):
    p = urlparse(u)
    host = p.hostname or ""
    if p.scheme not in ("http", "https") or not host:
        return False
    if host in ("datanodes.to", "www.datanodes.to"):
        return False
    if any(host == b or host.endswith("." + b) for b in DN_BAD_HOSTS):
        return False
    return not p.path.lower().endswith((".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                                        ".ico", ".webp", ".woff", ".woff2", ".json", ".xml"))


def _dn_links(text):
    clean = htmllib.unescape((text or "").replace("\\/", "/"))
    out = []
    for u in URL_RE.findall(clean):
        u = u.rstrip(").,;")
        if _dn_direct(u) and u not in out:
            out.append(u)
    return out


def resolve_datanodes(sess, item):
    """Best-effort HTTP resolution of a datanodes.to share page.

    datanodes is an XFileSharing-style host: one POST to the free-download form,
    then the step-2 form returns the direct CDN URL in a Location header or body.
    Turnstile-protected pages cannot be solved without a real browser, so they
    fail with a message that says exactly that instead of hanging.
    """
    r = sess.get(item.page, timeout=30)
    if r.status_code == 404:
        raise RuntimeError("file not found (404) - link is dead")
    r.raise_for_status()
    body = r.text
    r.close()
    page_name(item, body)
    check_gone(body)

    low = body.lower()
    if "cf-turnstile" in low or "turnstile" in low or "verify you are human" in low:
        raise NeedBrowser(DN_NEEDS_BROWSER)

    form = _dn_form(body)
    if not form:
        raise RuntimeError("datanodes gave no download form - the page layout may have changed")

    action, fields = form
    for step in range(2):
        data = dict(fields)
        data.setdefault("method_free", "Free Download >>")
        if data.get("op") == "download2":
            data.setdefault("down_direct", "1")
        target = urljoin(item.page, action) if action else item.page
        if step:
            time.sleep(2)  # let the free-download countdown settle
        r = sess.post(target, data=data, headers={"Referer": item.page, "Origin": "https://datanodes.to",
                                                  "Accept": "text/html,application/xhtml+xml,*/*"},
                      allow_redirects=False, timeout=30)
        loc = r.headers.get("Location") or ""
        ctype = r.headers.get("Content-Type") or ""
        text = r.text if "text" in ctype else ""
        r.close()
        if loc:
            loc = urljoin(item.page, loc)
            if _dn_direct(loc):
                return loc
        links = _dn_links(text)
        if links:
            return links[0]
        if "cf-turnstile" in text.lower():
            raise NeedBrowser(DN_NEEDS_BROWSER)
        nxt = _dn_form(text)
        if not nxt:
            break
        action, fields = nxt
    raise RuntimeError("datanodes gave no file link - try this part in a browser")


# --------------------------------------------------------------------------- segmented download
MIN_SEGMENTED = 8 << 20


def probe_size(sess, dl, referer):
    """Return total size if the server supports Range requests, else None."""
    r = sess.get(dl, headers={"Referer": referer, "Range": "bytes=0-0"}, stream=True, timeout=(30, 60))
    try:
        if r.status_code == 206:
            m = re.search(r"/(\d+)$", r.headers.get("Content-Range") or "")
            if m:
                return int(m.group(1))
        return None
    finally:
        r.close()


def fetch_segment(sess, dl, referer, path, seg, bar, lock, stop):
    """seg = [start, end, done]; keeps going (and re-requesting) until the range is complete."""
    stalls, last = 0, "no data received"
    while seg[0] + seg[2] <= seg[1] and not stop.is_set():
        pos = seg[0] + seg[2]
        before = seg[2]
        try:
            r = sess.get(dl, headers={"Referer": referer, "Range": f"bytes={pos}-{seg[1]}"},
                         stream=True, timeout=(30, 60))
            try:
                if r.status_code != 206:
                    raise IOError(f"segment got HTTP {r.status_code}")
                with open(path, "r+b") as f:
                    f.seek(pos)
                    for chunk in r.iter_content(CHUNK):
                        if stop.is_set():
                            return
                        if not chunk:
                            continue
                        room = seg[1] + 1 - (seg[0] + seg[2])
                        chunk = chunk[:room]
                        f.write(chunk)
                        f.flush()
                        with lock:
                            seg[2] += len(chunk)
                            bar.update(len(chunk))
            finally:
                r.close()
        except NeedBrowser:
            raise
        except Exception as e:
            last = e
        if seg[2] == before:
            stalls += 1
            if stalls >= 6:
                raise IOError(f"segment kept failing: {last}")
            stop.wait(min(30, 3 * stalls))
        else:
            stalls = 0


def download_segmented(sess, item, dl, tmp, total, conns, make_bar, cancel):
    """Returns True when complete, False if cancelled (state is saved for resume)."""
    state_file = tmp.with_name(tmp.name + ".json")
    segs = None
    if tmp.exists() and state_file.exists():
        try:
            st = json.loads(state_file.read_text())
            if st["total"] == total and tmp.stat().st_size == total:
                segs = st["segs"]
        except Exception:
            segs = None
    if segs is None:
        with open(tmp, "wb") as f:
            f.truncate(total)
        step = -(-total // conns)
        segs = [[i * step, min(total, (i + 1) * step) - 1, 0] for i in range(conns) if i * step < total]
    done0 = sum(sg[2] for sg in segs)
    lock = threading.Lock()
    stop = threading.Event()

    def save():
        with lock:
            data = {"total": total, "segs": [list(sg) for sg in segs]}
        tmp_state = state_file.with_name(state_file.name + ".tmp")
        tmp_state.write_text(json.dumps(data))
        os.replace(tmp_state, state_file)

    with make_bar(total, done0) as bar:
        # fewer connections than saved segments: the extra segments wait their turn
        with ThreadPoolExecutor(max_workers=max(1, min(conns, len(segs)))) as ex:
            futs = [ex.submit(fetch_segment, sess, dl, item.page, tmp, sg, bar, lock, stop) for sg in segs]
            try:
                while not all(f.done() for f in futs):
                    time.sleep(1)
                    save()
                    if cancel.is_set() or any(f.done() and f.exception() for f in futs):
                        stop.set()
                        break
                for f in futs:
                    f.result()
            except BaseException:
                stop.set()
                save()
                raise
    if cancel.is_set():
        save()
        return False
    if any(sg[2] != sg[1] - sg[0] + 1 for sg in segs):
        save()
        raise IOError("incomplete download")
    state_file.unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------- one file
def download_item(sess, item, outdir, conns, make_bar, cancel):
    """Resolve and download one file; used by the CLI and the GUI.
    make_bar(total, initial) returns a context manager with update(n), e.g. a tqdm bar.
    Returns "done", "skipped (exists)" or "stopped" (cancel was set; rerun to resume)."""
    if item.name and (outdir / item.name).exists():
        return "skipped (exists)"
    dl = resolve(sess, item)
    dest = outdir / item.name
    tmp = dest.with_name(dest.name + ".part")
    state_file = tmp.with_name(tmp.name + ".json")
    if dest.exists():
        return "skipped (exists)"
    # a .part with a .json is preallocated to full size, so it must stay segmented
    if state_file.exists() or (conns > 1 and not tmp.exists()):
        total = probe_size(sess, dl, item.page)
        if state_file.exists() and not total:
            raise IOError("server did not answer the range check")
        if total and (state_file.exists() or total >= MIN_SEGMENTED):
            if not download_segmented(sess, item, dl, tmp, total, conns, make_bar, cancel):
                return "stopped"
            tmp.rename(dest)
            return "done"
    have = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Referer": item.page}
    if have:
        headers["Range"] = f"bytes={have}-"
    r = sess.get(dl, headers=headers, stream=True, timeout=(30, 90))
    try:
        if r.status_code == 416:
            # only trust "nothing left to send" if the server's size matches ours
            m = re.search(r"/(\d+)$", r.headers.get("Content-Range") or "")
            if m and int(m.group(1)) == have:
                tmp.rename(dest)
                return "done"
            tmp.unlink(missing_ok=True)
            raise IOError("partial file did not match the server's copy - starting it over")
        r.raise_for_status()
        if "text/html" in (r.headers.get("Content-Type") or ""):
            raise RuntimeError("got an HTML page instead of the file")
        if have and r.status_code != 206:
            have = 0
        length = int(r.headers.get("Content-Length") or 0)
        total = have + length if length else None
        with open(tmp, "ab" if have else "wb") as f, make_bar(total, have) as bar:
            for chunk in r.iter_content(CHUNK):
                if cancel.is_set():
                    return "stopped"
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
    finally:
        r.close()
    size = tmp.stat().st_size
    if total and size < total:
        raise IOError(f"incomplete ({size}/{total} bytes)")
    tmp.rename(dest)
    return "done"


# --------------------------------------------------------------------------- fast mode
def download_fast(sess, item, outdir, slots, retries, conns, cancel):
    slot = slots.get()

    def make_bar(total, initial):
        return tqdm(total=total, initial=initial, unit="B", unit_scale=True, unit_divisor=1024,
                    desc=item.name[-40:].ljust(40), position=slot, leave=False)
    try:
        for attempt in range(1, retries + 1):
            if cancel.is_set():
                return item, "stopped"
            try:
                return item, download_item(sess, item, outdir, conns, make_bar, cancel)
            except NeedBrowser:
                return item, "BROWSER"
            except RuntimeError as e:
                if "gave no file link" in str(e):
                    tqdm.write(f"   {item.name or item.id}: {e}\n      -> will try it via the browser")
                    return item, "BROWSER"
                if "not found" in str(e) or attempt == retries:
                    return item, f"FAILED: {e}"
                cancel.wait(min(60, 5 * attempt))
            except Exception as e:
                if "not found" in str(e) or attempt == retries:
                    return item, f"FAILED: {e}"
                delay = min(60, 5 * attempt)
                tqdm.write(f"   {item.name or item.id}: {e} - retry {attempt}/{retries - 1} in {delay}s")
                cancel.wait(delay)
    finally:
        slots.put(slot)


def run_fast(sess, items, outdir, jobs, retries, conns=1):
    slots = queue.Queue()
    for i in range(jobs):
        slots.put(i)
    results = []
    cancel = threading.Event()
    overall = tqdm(total=len(items), desc="Total".ljust(40), position=jobs, unit="file")
    ex = ThreadPoolExecutor(max_workers=jobs)
    try:
        pending = {ex.submit(download_fast, sess, it, outdir, slots, retries, conns, cancel) for it in items}
        while pending:
            # short timeout so Ctrl+C is noticed (a plain blocking wait can miss it on Windows)
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for f in done:
                it, status = f.result()
                results.append((it, status))
                if status != "BROWSER":
                    tag = "ERR" if status.startswith("FAILED") else "OK "
                    tqdm.write(f"{tag} {it.name or it.page}: {status}")
                    overall.update(1)
    except KeyboardInterrupt:
        cancel.set()
        tqdm.write("\nStopping - saving progress of the current downloads...")
        raise
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
        overall.close()
    return results


# --------------------------------------------------------------------------- browser mode
def run_browser(items, outdir, channel, retries):
    """Download each file through a real Chrome window, one at a time."""
    sync_playwright = require_playwright()
    print(f"\nBrowser mode: {len(items)} file(s). A Chrome window will do the downloads - "
          "leave it open. If a Cloudflare check shows up there, complete it.\n")
    tmpdir = outdir / ".ffdl_tmp"
    tmpdir.mkdir(exist_ok=True)
    results = []
    with sync_playwright() as p:
        ctx = launch_browser(p, channel, accept_downloads=True, downloads_path=str(tmpdir))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        got = []
        page.on("download", got.append)

        def close_popups():
            for pg in ctx.pages:
                if pg is not page:
                    try:
                        pg.close()
                    except Exception:
                        pass
        ctx.on("page", lambda pg: pg.close() if pg is not page else None)
        for n, item in enumerate(items, 1):
            status = None
            for attempt in range(1, retries + 1):
                try:
                    if item.name and (outdir / item.name).exists():
                        status = "skipped (exists)"
                        break
                    page.goto(item.page, wait_until="domcontentloaded", timeout=60000)
                    wait_past_challenge(page)
                    body = page.content()
                    page_name(item, body)
                    check_gone(body)
                    dest = outdir / item.name
                    if dest.exists():
                        status = "skipped (exists)"
                        break
                    before = set(os.listdir(tmpdir))
                    got.clear()
                    dl = static_dl(item, body)
                    if dl:
                        page.evaluate("u => { window.location.href = u }", dl)
                    else:
                        btn = page.locator("a:has-text('DOWNLOAD'), button:has-text('DOWNLOAD')").first
                        for click in range(4):
                            if got:
                                break
                            btn.click(timeout=15000)
                            for _ in range(20):          # wait up to 10s for the download to start
                                if got:
                                    break
                                page.wait_for_timeout(500)
                            close_popups()
                    end = time.time() + 60
                    while not got and time.time() < end:
                        page.wait_for_timeout(500)
                    if not got:
                        page.screenshot(path=str(outdir / f"debug_{item.id}.png"))
                        raise RuntimeError(f"clicking DOWNLOAD didn't start a download "
                                           f"(screenshot: {outdir / f'debug_{item.id}.png'})")
                    download = got[0]
                    total = page_size(body)
                    stop = threading.Event()
                    bar = tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                               desc=f"[{n}/{len(items)}] {item.name[-34:]}".ljust(40), leave=False)

                    def watch():
                        last = 0
                        while not stop.is_set():
                            try:
                                new = [f for f in os.listdir(tmpdir) if f not in before]
                                size = max((os.path.getsize(tmpdir / f) for f in new), default=0)
                                if size > last:
                                    bar.update(size - last)
                                    last = size
                            except OSError:
                                pass
                            stop.wait(1)

                    t = threading.Thread(target=watch, daemon=True)
                    t.start()
                    try:
                        path = download.path()        # blocks until the download finishes
                        err = download.failure()
                    finally:
                        stop.set()
                        t.join()
                        bar.close()
                    if err or not path:
                        raise RuntimeError(f"browser download failed: {err}")
                    shutil.move(str(path), str(dest))
                    status = "done"
                    break
                except Exception as e:
                    if "not found" in str(e) or "screenshot:" in str(e) or attempt == retries:
                        status = f"FAILED: {e}"
                        break
                    wait = min(60, 5 * attempt)
                    print(f"   {item.name or item.id}: {e} - retry {attempt}/{retries - 1} in {wait}s")
                    time.sleep(wait)
            tag = "ERR" if status.startswith("FAILED") else "OK "
            print(f"{tag} {item.name or item.page}: {status}")
            results.append((item, status))
        ctx.close()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return results


# --------------------------------------------------------------------------- multipart
PART_RE = re.compile(r"^(?P<base>.+?)\.(?:part0*(?P<n1>\d+)\.rar|7z\.0*(?P<n2>\d+)|zip\.0*(?P<n3>\d+)|r(?P<n4>\d{2}))$", re.I)


def multipart_report(items, outdir):
    groups = {}
    for it in items:
        m = PART_RE.match(it.name or "")
        if m:
            n = int(next(g for g in m.group("n1", "n2", "n3", "n4") if g))
            groups.setdefault(m.group("base"), []).append((n, it.name))
    for base, parts in groups.items():
        parts.sort()
        nums = [n for n, _ in parts]
        missing = sorted(set(range(1, nums[-1] + 1)) - set(nums))
        present = all((outdir / name).exists() for _, name in parts)
        print(f"\n[{base}] {len(parts)} parts" + (f" - MISSING part(s) {missing}" if missing else ""))
        if present and not missing:
            print(f"  All parts here. Extract only the first one: {parts[0][1]}")
            print("  (7-Zip / WinRAR picks up the rest automatically - keep them in the same folder)")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Multi-threaded segmented download manager")
    ap.add_argument("links", nargs="*", help="links, or any text containing them")
    ap.add_argument("-i", "--input", help="text/HTML file containing links")
    ap.add_argument("-o", "--out", default="downloads", help="output folder")
    ap.add_argument("-j", "--jobs", type=int, default=1, help="files downloaded at once in fast mode (default 1)")
    ap.add_argument("-c", "--connections", type=int, default=8,
                    help="connections per file (default 8; 1 = single stream)")
    ap.add_argument("-r", "--retries", type=int, default=6)
    ap.add_argument("--browser", action="store_true", help="download everything through the browser")
    ap.add_argument("--channel", help="browser for playwright: chrome (default) or msedge")
    args = ap.parse_args()
    args.jobs, args.retries = max(1, args.jobs), max(1, args.retries)

    if not HAVE_CFFI and not args.browser:
        print("Tip: pip install curl_cffi  - it gets past Cloudflare far more often.\n")

    sess = Session(args.channel)
    items = collect_links(args, sess)
    if not items:
        sys.exit("No supported download links found. Put the part links in the file "
                 "(one per line), or a page URL that lists them.")
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    global DEBUG_DIR
    DEBUG_DIR = outdir
    print(f"{len(items)} file(s) -> {outdir.resolve()}\n")

    if args.browser:
        results = run_browser(items, outdir, args.channel, args.retries)
    else:
        results = run_fast(sess, items, outdir, args.jobs, args.retries, max(1, args.connections))
        todo = [it for it, s in results if s == "BROWSER"]
        if todo:
            results = [(it, s) for it, s in results if s != "BROWSER"]
            try:
                results += run_browser(todo, outdir, args.channel, args.retries)
            except NoPlaywright as e:
                print(f"\n{e}")
                results += [(it, "FAILED: blocked by Cloudflare (browser mode needs playwright)") for it in todo]

    failed = [(it, s) for it, s in results if s.startswith("FAILED")]
    print(f"\nFinished: {len(results) - len(failed)} ok, {len(failed)} failed")
    if failed:
        (outdir / "failed_links.txt").write_text(
            "\n".join(f"{it.page}#{it.name or ''}" for it, _ in failed))
        print(f"Failed links saved to {outdir / 'failed_links.txt'} - rerun with -i to retry them.")
    multipart_report(items, outdir)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Rerun the same command to resume.")
    except NoPlaywright as e:
        sys.exit(str(e))
