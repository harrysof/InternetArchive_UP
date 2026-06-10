#!/usr/bin/env python3
"""
Internet Archive Uploader
• Resume support via state file
• Per-file upload progress bar (real bytes sent)
• Speed + ETA display
• Metadata from yt-dlp JSON / description files
• Date parsed from filename (e.g. VideoTitle20151024.mp4)
• All files for each video (mp4 + json + description) go to same IA item
• Successfully uploaded files moved to  <source>/done/
• Failed files moved to                 <source>/failed/
• Credentials saved/loaded from config file
• Clickable Archive.org links after each successful upload
• Log auto-saved to file + Save Log button
• Retry failed only (without resetting successful uploads)
• Test credentials button
• Session summary stats at end
Requirements: pip install requests
"""

import os, json, time, threading, subprocess, sys, shutil, re, webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime
import urllib.parse
import requests

# ─── PATHS ────────────────────────────────────────────────────────────────────
# All persistent files live next to this script, regardless of CWD at launch.
STATE_FILE  = Path(__file__).parent / "ia_upload_state.json"
CONFIG_FILE = Path(__file__).parent / "ia_config.json"
LOG_FILE    = Path(__file__).parent / "ia_upload.log"

# ─── CONFIG (credentials + settings) ─────────────────────────────────────────
def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"uploaded": [], "failed": [], "skipped": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── FILE MOVING ──────────────────────────────────────────────────────────────
def move_video_files(video, destination_subfolder, log_fn):
    """Move all files belonging to this video (mp4, json, description) into a subfolder."""
    base_folder = Path(video["file"]).parent
    dest = base_folder / destination_subfolder
    dest.mkdir(exist_ok=True)

    all_paths = [video["file"]]
    if video["json"]:        all_paths.append(video["json"])
    if video["description"]: all_paths.append(video["description"])

    for src in all_paths:
        src_path = Path(src)
        if src_path.exists():
            dst_path = dest / src_path.name
            try:
                shutil.move(str(src_path), str(dst_path))
                log_fn(f"    ↦ Moved to {destination_subfolder}/: {src_path.name}")
            except Exception as e:
                log_fn(f"    ⚠ Could not move {src_path.name}: {e}")


# ─── FILE DISCOVERY ───────────────────────────────────────────────────────────
def extract_date_from_stem(stem):
    """
    Try to find a YYYYMMDD date at or near the end of the filename stem.
    yt-dlp typically appends it like: VideoTitle20151024
    or inside brackets: VideoTitle [abcXYZ123] — in which case we strip the ID first.
    Returns YYYYMMDD string or "" if not found.
    """
    # Remove trailing [youtube-id] if present
    clean = re.sub(r'\[[\w-]{6,12}\]$', '', stem).strip()
    # Look for 8-digit date (YYYYMMDD) — greedy from the right
    m = re.search(r'((?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))', clean)
    if m:
        return m.group(1)
    return ""


def discover_videos(folder, generic_mode=False):
    path = Path(folder)

    if path.is_file():
        if not generic_mode:
            return []

        f = path
        stem = f.stem
        file_id = stem
        if "[" in stem and stem.endswith("]"):
            file_id = stem.split("[")[-1].rstrip("]")

        json_file = f.with_suffix(".info.json")
        if not json_file.exists():
            json_file = f.with_suffix(".json")
        desc_file = f.with_suffix(".description")

        upload_date_from_filename = extract_date_from_stem(stem)
        mtime = f.stat().st_mtime
        date_from_mtime = datetime.fromtimestamp(mtime).strftime("%Y%m%d") if mtime else ""

        return [{
            "id":                       file_id,
            "stem":                     stem,
            "file":                     str(f),
            "file_size":                f.stat().st_size,
            "extension":                f.suffix.lower(),
            "json":                     str(json_file) if json_file.exists() else None,
            "description":              str(desc_file) if desc_file.exists() else None,
            "date_from_filename":       upload_date_from_filename,
            "date_from_mtime":          date_from_mtime,
            "generic_mode":             generic_mode,
        }]

    folder = path
    if generic_mode:
        all_files = []
        for f in folder.rglob("*"):
            if f.is_file():
                parts = f.relative_to(folder).parts
                if len(parts) > 1 and parts[0].lower() in ("done", "failed"):
                    continue
                all_files.append(f)
        all_files = sorted(all_files)

        groups = {}
        for f in all_files:
            stem = f.stem
            file_id = stem
            if "[" in stem and stem.endswith("]"):
                file_id = stem.split("[")[-1].rstrip("]")

            json_file = f.with_suffix(".info.json")
            if not json_file.exists():
                json_file = f.with_suffix(".json")
            desc_file = f.with_suffix(".description")

            upload_date_from_filename = extract_date_from_stem(stem)

            mtime = f.stat().st_mtime
            date_from_mtime = datetime.fromtimestamp(mtime).strftime("%Y%m%d") if mtime else ""

            groups[file_id] = {
                "id":                       file_id,
                "stem":                     stem,
                "file":                     str(f),
                "file_size":                f.stat().st_size,
                "extension":                f.suffix.lower(),
                "json":                     str(json_file) if json_file.exists() else None,
                "description":              str(desc_file) if desc_file.exists() else None,
                "date_from_filename":       upload_date_from_filename,
                "date_from_mtime":          date_from_mtime,
                "generic_mode":             True,
            }
        return list(groups.values())
    else:
        mp4_files = []
        for mp4 in folder.rglob("*.mp4"):
            parts = mp4.relative_to(folder).parts
            if len(parts) > 1 and parts[0].lower() in ("done", "failed"):
                continue
            mp4_files.append(mp4)
        mp4_files = sorted(mp4_files)

        groups = {}
        for mp4 in mp4_files:
            stem = mp4.stem
            video_id = stem
            if "[" in stem and stem.endswith("]"):
                video_id = stem.split("[")[-1].rstrip("]")

            json_file = mp4.with_suffix(".info.json")
            if not json_file.exists():
                json_file = mp4.with_suffix(".json")
            desc_file = mp4.with_suffix(".description")

            upload_date_from_filename = extract_date_from_stem(stem)

            groups[video_id] = {
                "id":                       video_id,
                "stem":                     stem,
                "file":                     str(mp4),
                "file_size":                mp4.stat().st_size,
                "extension":                ".mp4",
                "json":                     str(json_file) if json_file.exists() else None,
                "description":              str(desc_file) if desc_file.exists() else None,
                "date_from_filename":       upload_date_from_filename,
                "generic_mode":             False,
            }
        return list(groups.values())


# ─── METADATA ─────────────────────────────────────────────────────────────────
def parse_metadata(video, channel_name, ia_collection):
    is_generic = video.get("generic_mode", False)

    mediatype = "movies"
    if is_generic:
        ext = video.get("extension", "").lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"):
            mediatype = "image"
        elif ext in (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"):
            mediatype = "audio"
        elif ext in (".mp4", ".mkv", ".avi", ".mov", ".webm"):
            mediatype = "movies"
        elif ext in (".pdf", ".djvu"):
            mediatype = "texts"
        else:
            mediatype = "data"

    meta = {
        "mediatype":  mediatype,
        "collection": ia_collection or "opensource_movies" if not is_generic else "opensource",
    }

    title = video["stem"]
    description = ""
    upload_date = video.get("date_from_filename", "") or video.get("date_from_mtime", "")
    uploader = channel_name or ""
    tags = []

    if video["json"]:
        try:
            with open(video["json"], "r", encoding="utf-8") as f:
                info = json.load(f)
            title        = info.get("title", title)
            description  = info.get("description", "")
            upload_date  = info.get("upload_date", upload_date) or upload_date
            uploader     = info.get("uploader", uploader) or uploader
            tags         = info.get("tags", []) or []
        except Exception:
            pass

    if video["description"] and not description:
        try:
            with open(video["description"], "r", encoding="utf-8") as f:
                description = f.read()
        except Exception:
            pass

    meta["title"] = title
    if description:  meta["description"] = description[:5000]
    if uploader:     meta["creator"]     = uploader
    if upload_date:
        try:
            d = upload_date.replace("-", "")
            meta["date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        except Exception:
            pass
    if tags: meta["subject"] = tags[:10]
    elif not is_generic: meta["subject"] = ["YouTube", "video"]
    return meta


def safe_header_value(val):
    """
    HTTP headers must be latin-1 safe (requests encodes them as latin-1 internally).
    For any string that contains characters outside latin-1 (e.g. emoji, CJK, Arabic),
    we UTF-8 encode and then percent-encode it so it survives the transport layer.
    Internet Archive decodes these correctly on their end.
    """
    s = str(val)
    try:
        s.encode("latin-1")   # already safe — pass through unchanged
        return s
    except UnicodeEncodeError:
        # Percent-encode the full string as UTF-8 bytes
        return urllib.parse.quote(s, safe=" ,.-_:()/")


def meta_to_headers(meta):
    hdrs = {}
    for key, val in meta.items():
        if isinstance(val, list):
            for i, v in enumerate(val):
                hdrs[f"x-archive-meta{i:02d}-{key}"] = safe_header_value(v)
        else:
            hdrs[f"x-archive-meta-{key}"] = safe_header_value(val)
    return hdrs


# ─── IDENTIFIER ───────────────────────────────────────────────────────────────
def build_identifier(video, channel_name):
    is_generic = video.get("generic_mode", False)

    if is_generic:
        prefix = channel_name or "file"
        ext = video.get("extension", "")
        raw = f"{prefix}-{video['id']}{ext}"
    else:
        raw = f"{channel_name or 'yt'}-{video['id']}"

    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in raw)
    return safe[:80].strip("-")


# ─── STREAMING FILE WRAPPER ───────────────────────────────────────────────────
class ProgressReader:
    """Wraps a file to report upload progress via callback(bytes_sent, total)."""
    def __init__(self, path, progress_cb):
        self._f    = open(path, "rb")
        self._size = os.path.getsize(path)
        self._sent = 0
        self._cb   = progress_cb

    def read(self, size=-1):
        chunk = self._f.read(size)
        if chunk:
            self._sent += len(chunk)
            self._cb(self._sent, self._size)
        return chunk

    def __len__(self):          return self._size
    def close(self):            self._f.close()
    def __enter__(self):        return self
    def __exit__(self, *a):     self.close()


# ─── SINGLE FILE UPLOAD ───────────────────────────────────────────────────────
def upload_one_file(path, identifier, access_key, secret_key,
                    extra_headers, log_fn, progress_cb):
    filename  = os.path.basename(path)
    url       = f"https://s3.us.archive.org/{identifier}/{urllib.parse.quote(filename)}"
    file_size = os.path.getsize(path)

    headers = {
        "Authorization":           f"LOW {access_key}:{secret_key}",
        "x-amz-auto-make-bucket":  "1",
        "x-archive-queue-derive":  "0",
        "Content-Length":          str(file_size),
    }
    headers.update(extra_headers)
    # Final safety pass: requests encodes headers as latin-1 internally.
    # safe_header_value() already percent-encoded non-latin-1 chars in metadata,
    # but as a hard backstop we replace anything that slipped through.
    headers = {k: v.encode("latin-1", errors="replace").decode("latin-1")
               for k, v in headers.items()}

    log_fn(f"    → {filename}  ({file_size/1024/1024:.1f} MB)")
    reader = ProgressReader(path, progress_cb)
    try:
        resp = requests.put(url, data=reader, headers=headers, timeout=900)
        reader.close()
        if resp.status_code in (200, 201):
            return True
        log_fn(f"    HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as e:
        reader.close()
        log_fn(f"    Error: {e}")
        return False


# ─── CREDENTIAL TEST ──────────────────────────────────────────────────────────
def test_credentials(access_key, secret_key):
    """
    Ping the IA S3 API with a lightweight HEAD request on a known public item.
    Returns (ok: bool, message: str).
    """
    if not access_key or not secret_key:
        return False, "Access key and secret key are both required."
    try:
        url = "https://s3.us.archive.org/"
        headers = {"Authorization": f"LOW {access_key}:{secret_key}"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code in (200, 403):
            # 200 = valid, 403 = valid keys but no bucket access (still authenticated)
            return True, f"Credentials valid (HTTP {resp.status_code})."
        return False, f"Unexpected response: HTTP {resp.status_code}."
    except requests.exceptions.ConnectionError:
        return False, "Connection failed — check your internet connection."
    except Exception as e:
        return False, f"Error: {e}"


# ─── VIDEO UPLOAD ─────────────────────────────────────────────────────────────
def upload_video(video, channel_name, ia_collection, access_key, secret_key,
                 log_fn, file_progress_cb, dry_run=False):
    """Returns ("ok", identifier) | ("fail", None) | ("skip", None)."""
    identifier = build_identifier(video, channel_name)
    meta       = parse_metadata(video, channel_name, ia_collection)
    meta_hdrs  = meta_to_headers(meta)

    is_generic = video.get("generic_mode", False)
    files = [video["file"]]
    if video["json"]:        files.append(video["json"])
    if video["description"]: files.append(video["description"])

    log_fn(f"  → Identifier : {identifier}")
    log_fn(f"  → Title      : {meta.get('title','')}")
    log_fn(f"  → Date       : {meta.get('date', '(none)')}")
    log_fn(f"  → Mediatype  : {meta.get('mediatype', 'unknown')}")
    log_fn(f"  → Files      : {len(files)}  (all uploading to same IA item)")

    if dry_run:
        log_fn("  [DRY RUN] skipping.")
        time.sleep(0.4)
        return "ok", identifier

    if not access_key or not secret_key:
        log_fn("  ✗ No S3 keys — check credentials.")
        return "fail", None

    for i, fpath in enumerate(files):
        # Metadata headers only on the FIRST file (the mp4) — this creates/updates the IA item.
        # Subsequent files (json, description) go to the same identifier with no extra meta headers.
        hdrs = meta_hdrs if i == 0 else {}
        ok   = False
        for attempt in range(1, 6):
            if attempt > 1:
                log_fn(f"    Retry {attempt}/5…"); time.sleep(12)
            ok = upload_one_file(fpath, identifier, access_key, secret_key,
                                 hdrs, log_fn, file_progress_cb)
            if ok: break
        if not ok:
            log_fn(f"  ✗ Failed: {os.path.basename(fpath)}")
            return "fail", None

    log_fn(f"  ✓ Done → https://archive.org/details/{identifier}")
    return "ok", identifier


# ─── GUI ──────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Internet Archive — File Uploader")
        self.geometry("980x760")
        self.resizable(True, True)
        self.configure(bg="#0d0d0d")
        self.app_state   = load_state()
        self.app_config  = load_config()
        self.videos      = []
        self.running     = False
        self.stop_flag   = threading.Event()
        self._file_start = 0
        self._session_start = 0
        self._session_bytes = 0          # total bytes uploaded this session
        self._uploaded_identifiers = []  # [(stem, identifier), …] for link panel
        self._build_ui()
        self._load_config_into_ui()
        self._check_deps()

    # ── dependency check ──────────────────────────────────────────────────────
    def _check_deps(self):
        missing = []
        for pkg in ("requests",):
            try: __import__(pkg)
            except ImportError: missing.append(pkg)
        if missing:
            if messagebox.askyesno("Missing packages",
                                   f"Missing: {', '.join(missing)}\nInstall now?"):
                subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)
                self._log(f"✓ Installed {', '.join(missing)}.")

    # ── config helpers ────────────────────────────────────────────────────────
    def _load_config_into_ui(self):
        cfg = self.app_config
        if cfg.get("access_key"): self.v_access.set(cfg["access_key"])
        if cfg.get("secret_key"): self.v_secret.set(cfg["secret_key"])
        if cfg.get("channel"):    self.v_channel.set(cfg["channel"])
        if cfg.get("collection"): self.v_collection.set(cfg["collection"])
        if cfg.get("folder"):     self.v_folder.set(cfg["folder"])

    def _save_credentials(self):
        cfg = self.app_config
        cfg["access_key"] = self.v_access.get().strip()
        cfg["secret_key"] = self.v_secret.get().strip()
        cfg["channel"]    = self.v_channel.get().strip()
        cfg["collection"] = self.v_collection.get().strip()
        cfg["folder"]     = self.v_folder.get().strip()
        save_config(cfg)
        self._log(f"✓ Settings saved to {CONFIG_FILE.name}")

    def _toggle_generic_mode(self):
        is_generic = self.v_generic.get()
        if is_generic:
            self.v_collection.set("opensource")
        else:
            self.v_collection.set("opensource_movies")

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        BG = "#0d0d0d"; PANEL = "#181818"; ACCENT = "#ff6b35"
        TEXT = "#e8e8e8"; MUTED = "#777"; EBG = "#242424"; BORDER = "#2a2a2a"
        SUCCESS = "#4caf50"; DANGER = "#f44336"; FILE_CLR = "#29b6f6"

        FL = ("Segoe UI", 9); FB = ("Segoe UI", 9, "bold")

        # header
        hdr = tk.Frame(self, bg=ACCENT, height=46)
        hdr.pack(fill="x")
        tk.Label(hdr, text="▶  INTERNET ARCHIVE UPLOADER", font=("Segoe UI", 12, "bold"),
                 bg=ACCENT, fg="#0d0d0d", pady=10).pack(side="left", padx=16)
        tk.Label(hdr, text="Items → Archive.org", font=FL,
                 bg=ACCENT, fg="#0d0d0d").pack(side="right", padx=16)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        # ── LEFT ─────────────────────────────────────────────────────────────
        left = tk.Frame(body, bg=PANEL, width=315)
        left.pack(side="left", fill="y", padx=(0, 1))
        left.pack_propagate(False)

        def sec(lbl):
            tk.Label(left, text=lbl, font=FB, bg=PANEL, fg=ACCENT
                     ).pack(anchor="w", padx=14, pady=(12, 1))
            tk.Frame(left, bg=BORDER, height=1).pack(fill="x", padx=14, pady=(0, 5))

        def entry_row(lbl, var, show=None, browse_dir=False, browse_file=False):
            tk.Label(left, text=lbl, font=FL, bg=PANEL, fg=MUTED).pack(anchor="w", padx=14)
            r = tk.Frame(left, bg=PANEL)
            r.pack(fill="x", padx=14, pady=(1, 6))
            e = tk.Entry(r, textvariable=var, font=FL, bg=EBG, fg=TEXT,
                         insertbackground=ACCENT, relief="flat", show=show or "")
            e.pack(side="left", fill="x", expand=True, ipady=5, ipadx=4)
            if browse_dir:
                def pick():
                    p = filedialog.askdirectory()
                    if p: var.set(p)
                tk.Button(r, text="📁", font=FL, bg=BORDER, fg=TEXT,
                          relief="flat", command=pick, padx=5).pack(side="left", padx=(3, 0))
            if browse_file:
                def pick():
                    p = filedialog.askopenfilename()
                    if p: var.set(p)
                tk.Button(r, text="📄", font=FL, bg=BORDER, fg=TEXT,
                          relief="flat", command=pick, padx=5).pack(side="left", padx=(3, 0))

        sec("SOURCE")
        self.v_folder = tk.StringVar()
        entry_row("file", self.v_folder, browse_dir=True, browse_file=True)

        sec("IA S3 CREDENTIALS")
        tk.Label(left, text="Get keys → archive.org/account/s3.php",
                 font=("Segoe UI", 8), bg=PANEL, fg="#555").pack(anchor="w", padx=14, pady=(0,4))
        self.v_access = tk.StringVar()
        self.v_secret = tk.StringVar()
        entry_row("Access Key", self.v_access)
        entry_row("Secret Key", self.v_secret, show="●")

        # credential action buttons row
        cred_row = tk.Frame(left, bg=PANEL)
        cred_row.pack(fill="x", padx=14, pady=(0, 6))
        tk.Button(cred_row, text="💾  Save", font=FL, bg=BORDER, fg=TEXT,
                  relief="flat", padx=8, pady=4,
                  command=self._save_credentials).pack(side="left", padx=(0, 4))
        self.btn_test_creds = tk.Button(cred_row, text="⚡  Test", font=FL, bg=BORDER, fg=TEXT,
                  relief="flat", padx=8, pady=4,
                  command=self._test_credentials)
        self.btn_test_creds.pack(side="left")
        self.lbl_cred_status = tk.Label(cred_row, text="", font=("Segoe UI", 8),
                                         bg=PANEL, fg=MUTED)
        self.lbl_cred_status.pack(side="left", padx=(8, 0))

        sec("SETTINGS")
        self.v_channel    = tk.StringVar()
        self.v_collection = tk.StringVar(value=" ")
        entry_row("Creator", self.v_channel)
        entry_row("Collection", self.v_collection)

        self.v_dry = tk.BooleanVar(value=False)
        tk.Checkbutton(left, text="Dry Run (simulate, no upload)", variable=self.v_dry,
                       font=FL, bg=PANEL, fg=TEXT, selectcolor=EBG,
                       activebackground=PANEL, activeforeground=ACCENT
                       ).pack(anchor="w", padx=14, pady=(0, 5))

        self.v_generic = tk.BooleanVar(value=False)
        tk.Checkbutton(left, text="Generic files (any type, not just MP4)", variable=self.v_generic,
                       font=FL, bg=PANEL, fg=TEXT, selectcolor=EBG,
                       activebackground=PANEL, activeforeground=ACCENT,
                       command=self._toggle_generic_mode
                       ).pack(anchor="w", padx=14, pady=(0, 5))

        self.v_move = tk.BooleanVar(value=True)
        tk.Checkbutton(left, text="Move files after upload (done/failed)", variable=self.v_move,
                       font=FL, bg=PANEL, fg=TEXT, selectcolor=EBG,
                       activebackground=PANEL, activeforeground=ACCENT
                       ).pack(anchor="w", padx=14, pady=(0, 8))

        tk.Button(left, text="⟳  SCAN FOLDER", font=FB,
                  bg=BORDER, fg=TEXT, relief="flat", pady=8,
                  command=self._scan).pack(fill="x", padx=14, pady=(4, 3))

        self.lbl_stats  = tk.Label(left, text="No folder scanned.", font=FL,
                                    bg=PANEL, fg=MUTED, wraplength=275, justify="left")
        self.lbl_stats.pack(padx=14, anchor="w")
        self.lbl_resume = tk.Label(left, text="", font=FL, bg=PANEL, fg=SUCCESS,
                                    wraplength=275, justify="left")
        self.lbl_resume.pack(padx=14, anchor="w", pady=(2, 0))

        # bottom buttons
        btns = tk.Frame(left, bg=PANEL)
        btns.pack(fill="x", padx=14, pady=12, side="bottom")

        self.btn_start = tk.Button(btns, text="▶  START UPLOAD",
                                    font=("Segoe UI", 11, "bold"),
                                    bg=ACCENT, fg="#0d0d0d", relief="flat", pady=10,
                                    command=self._start)
        self.btn_start.pack(fill="x", pady=(0, 4))

        self.btn_stop = tk.Button(btns, text="■  STOP", font=FB,
                                   bg="#252525", fg=TEXT, relief="flat", pady=9,
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(fill="x")

        self.btn_retry = tk.Button(btns, text="↺  Retry failed only", font=FL,
                                    bg="#252525", fg="#f9a825", relief="flat", pady=6,
                                    command=self._retry_failed)
        self.btn_retry.pack(fill="x", pady=(4, 0))

        util_row = tk.Frame(btns, bg=PANEL)
        util_row.pack(fill="x", pady=(6, 0))
        tk.Button(util_row, text="💾 Save Log", font=FL,
                  bg=BG, fg=MUTED, relief="flat", pady=4,
                  command=self._save_log).pack(side="left", fill="x", expand=True, padx=(0, 2))
        tk.Button(util_row, text="🗑 Reset all", font=FL,
                  bg=BG, fg=MUTED, relief="flat", pady=4,
                  command=self._reset_state).pack(side="left", fill="x", expand=True)

        # ── RIGHT ─────────────────────────────────────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        # overall progress
        s1 = tk.Frame(right, bg=PANEL)
        s1.pack(fill="x", pady=(0, 1))
        tk.Label(s1, text="OVERALL", font=FB, bg=PANEL, fg=ACCENT
                 ).pack(anchor="w", padx=14, pady=(10, 2))

        ttk.Style().configure("Ov.Horizontal.TProgressbar",
                              troughcolor=EBG, background=ACCENT,
                              bordercolor=PANEL, thickness=16)
        self.bar_overall = ttk.Progressbar(s1, style="Ov.Horizontal.TProgressbar",
                                            mode="determinate")
        self.bar_overall.pack(fill="x", padx=14, pady=(0, 3))

        self.lbl_overall = tk.Label(s1, text="0 / 0 files", font=FL, bg=PANEL, fg=TEXT)
        self.lbl_overall.pack(anchor="w", padx=14)

        row_st = tk.Frame(s1, bg=PANEL)
        row_st.pack(fill="x", padx=14, pady=(2, 10))
        self.lbl_ok   = tk.Label(row_st, text="✓ 0 done",    font=FL, bg=PANEL, fg=SUCCESS)
        self.lbl_fail = tk.Label(row_st, text="✗ 0 failed",  font=FL, bg=PANEL, fg=DANGER)
        self.lbl_skip = tk.Label(row_st, text="↷ 0 skipped", font=FL, bg=PANEL, fg=MUTED)
        self.lbl_ok.pack(side="left", padx=(0, 14))
        self.lbl_fail.pack(side="left", padx=(0, 14))
        self.lbl_skip.pack(side="left")

        # current file progress
        s2 = tk.Frame(right, bg="#131313")
        s2.pack(fill="x", pady=(0, 1))
        tk.Label(s2, text="CURRENT FILE", font=FB, bg="#131313", fg=FILE_CLR
                 ).pack(anchor="w", padx=14, pady=(10, 1))

        self.lbl_file = tk.Label(s2, text="—", font=FL, bg="#131313", fg=TEXT,
                                  anchor="w", wraplength=640)
        self.lbl_file.pack(anchor="w", padx=14)

        ttk.Style().configure("Fi.Horizontal.TProgressbar",
                              troughcolor=EBG, background=FILE_CLR,
                              bordercolor="#131313", thickness=12)
        self.bar_file = ttk.Progressbar(s2, style="Fi.Horizontal.TProgressbar",
                                         mode="determinate")
        self.bar_file.pack(fill="x", padx=14, pady=(4, 2))

        info_row = tk.Frame(s2, bg="#131313")
        info_row.pack(fill="x", padx=14, pady=(0, 8))
        self.lbl_pct   = tk.Label(info_row, text="", font=FL, bg="#131313", fg=MUTED)
        self.lbl_speed = tk.Label(info_row, text="", font=FL, bg="#131313", fg=MUTED)
        self.lbl_eta   = tk.Label(info_row, text="", font=FL, bg="#131313", fg=MUTED)
        self.lbl_pct.pack(side="left", padx=(0, 16))
        self.lbl_speed.pack(side="left", padx=(0, 16))
        self.lbl_eta.pack(side="left")

        # right panel uses a PanedWindow so log and links can resize
        right_pane = tk.PanedWindow(right, orient="vertical", bg=BG,
                                    sashwidth=4, sashrelief="flat")
        right_pane.pack(fill="both", expand=True)

        # log frame
        log_frame = tk.Frame(right_pane, bg=BG)
        tk.Label(log_frame, text="LOG", font=FB, bg=BG, fg=MUTED
                 ).pack(anchor="w", padx=14, pady=(8, 1))
        self.log_box = scrolledtext.ScrolledText(
            log_frame, font=("Segoe UI", 8), bg="#080808", fg="#999",
            insertbackground=ACCENT, relief="flat", bd=0, state="disabled")
        self.log_box.pack(fill="both", expand=True)
        right_pane.add(log_frame, minsize=120)

        # uploaded links frame
        links_frame = tk.Frame(right_pane, bg="#0a0a0a")
        lf_hdr = tk.Frame(links_frame, bg="#0a0a0a")
        lf_hdr.pack(fill="x", padx=14, pady=(6, 2))
        tk.Label(lf_hdr, text="UPLOADED ITEMS", font=FB, bg="#0a0a0a", fg=MUTED
                 ).pack(side="left")
        self.lbl_session_summary = tk.Label(lf_hdr, text="", font=("Segoe UI", 8),
                                             bg="#0a0a0a", fg="#555")
        self.lbl_session_summary.pack(side="right")

        self.links_box = scrolledtext.ScrolledText(
            links_frame, font=("Segoe UI", 8), bg="#080808", fg="#29b6f6",
            insertbackground=ACCENT, relief="flat", bd=0, state="disabled",
            cursor="arrow", height=6)
        self.links_box.tag_config("link", foreground="#29b6f6", underline=True)
        self.links_box.tag_bind("link", "<Button-1>", self._on_link_click)
        self.links_box.tag_bind("link", "<Enter>",
                                lambda e: self.links_box.config(cursor="hand2"))
        self.links_box.tag_bind("link", "<Leave>",
                                lambda e: self.links_box.config(cursor="arrow"))
        self.links_box.pack(fill="both", expand=True)
        right_pane.add(links_frame, minsize=80)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _log(self, msg):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {msg}\n"
            self.log_box.config(state="normal")
            self.log_box.insert("end", line)
            self.log_box.see("end")
            self.log_box.config(state="disabled")
            # auto-write to log file
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as lf:
                    lf.write(line)
            except Exception:
                pass
        self.after(0, _do)

    def _add_link(self, stem, identifier):
        """Add a clickable IA link to the links panel."""
        url = f"https://archive.org/details/{identifier}"
        def _do():
            self.links_box.config(state="normal")
            self.links_box.insert("end", f"  {stem[:55]}\n  ", "")
            self.links_box.insert("end", url, ("link", f"url:{url}"))
            self.links_box.insert("end", "\n\n")
            self.links_box.see("end")
            self.links_box.config(state="disabled")
        self.after(0, _do)

    def _on_link_click(self, event):
        idx = self.links_box.index(f"@{event.x},{event.y}")
        for tag in self.links_box.tag_names(idx):
            if tag.startswith("url:"):
                webbrowser.open(tag[4:])
                break

    def _on_file_progress(self, sent, total):
        pct     = int(sent / total * 100) if total else 0
        s_mb    = sent  / 1024 / 1024
        t_mb    = total / 1024 / 1024
        elapsed = time.time() - self._file_start
        speed   = (sent / elapsed / 1024 / 1024) if elapsed > 0.1 else 0
        eta     = ((total - sent) / (sent / elapsed)) if sent > 0 and elapsed > 0.1 else 0
        eta_s   = f"ETA {int(eta//60)}m {int(eta%60)}s" if eta > 0 else "ETA …"
        sp_s    = f"{speed:.2f} MB/s" if speed > 0 else ""
        self._session_bytes = max(self._session_bytes,
                                  (getattr(self, '_prev_file_bytes', 0) + sent))

        def _do():
            self.bar_file["value"] = pct
            self.lbl_pct.config(text=f"{s_mb:.1f} / {t_mb:.1f} MB  ({pct}%)")
            self.lbl_speed.config(text=sp_s)
            self.lbl_eta.config(text=eta_s)
        self.after(0, _do)

    # ── actions ───────────────────────────────────────────────────────────────
    def _scan(self):
        folder = self.v_folder.get()
        generic_mode = self.v_generic.get()
        is_file = os.path.isfile(folder)
        is_dir = os.path.isdir(folder)

        if not folder:
            messagebox.showerror("Error", "Select a folder or file."); return
        if not is_dir and not is_file:
            messagebox.showerror("Error", "Invalid path."); return
        if is_file and not generic_mode:
            messagebox.showerror("Error", "Single files only in Generic mode. Select a folder for files."); return

        self.videos = discover_videos(folder, generic_mode)
        done = set(self.app_state["uploaded"])
        pend = [v for v in self.videos if v["id"] not in done]
        total_gb = sum(v["file_size"] for v in self.videos) / 1024**3
        mode_label = "files" if generic_mode else "videos"
        self.lbl_stats.config(
            text=f"{len(self.videos)} {mode_label}  ({total_gb:.1f} GB)\n"
                 f"{len(pend)} pending  •  {len(done)} done")
        if done:
            self.lbl_resume.config(text=f"↷ Resume: {len(done)} already uploaded")
        self._log(f"Scan: {len(self.videos)} {mode_label}, {total_gb:.1f} GB")

    def _start(self):
        if not self.v_folder.get():
            messagebox.showerror("Error", "Select a folder or file first."); return
        if not self.videos: self._scan()
        if not self.videos:
            messagebox.showerror("Error", "No files found."); return
        self._session_start = time.time()
        self._session_bytes = 0
        self._prev_file_bytes = 0
        self._uploaded_identifiers = []
        self.running = True
        self.stop_flag.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        threading.Thread(target=self._run, daemon=True).start()

    def _stop(self):
        self.stop_flag.set()
        self._log("⚠ Stop requested — finishing current file first.")
        self.btn_stop.config(state="disabled")

    def _reset_state(self):
        if messagebox.askyesno("Reset", "Clear ALL upload history and re-upload everything?"):
            self.app_state = {"uploaded": [], "failed": [], "skipped": []}
            save_state(self.app_state)
            self.lbl_resume.config(text="")
            self._log("State cleared — all items will be re-uploaded.")

    def _retry_failed(self):
        """Clear only the failed list so those items get picked up on next run."""
        failed = self.app_state.get("failed", [])
        if not failed:
            messagebox.showinfo("Nothing to retry", "No failed items in state."); return
        if messagebox.askyesno("Retry failed",
                               f"Re-queue {len(failed)} failed item(s) for upload?\n"
                               "(Successful uploads are kept.)"):
            self.app_state["failed"] = []
            save_state(self.app_state)
            self._log(f"↺ {len(failed)} failed item(s) cleared — will retry on next Start.")
            self.lbl_fail.config(text="✗ 0 failed")

    def _test_credentials(self):
        access = self.v_access.get().strip()
        secret = self.v_secret.get().strip()
        self.btn_test_creds.config(state="disabled", text="Testing…")
        self.lbl_cred_status.config(text="", fg="#777")

        def _do():
            ok, msg = test_credentials(access, secret)
            def _ui():
                self.btn_test_creds.config(state="normal", text="⚡  Test")
                color = "#4caf50" if ok else "#f44336"
                icon  = "✓" if ok else "✗"
                self.lbl_cred_status.config(text=f"{icon} {msg}", fg=color)
                self._log(f"Credential test: {msg}")
            self.after(0, _ui)
        threading.Thread(target=_do, daemon=True).start()

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All", "*.*")],
            initialfile=f"ia_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
            title="Save log as…")
        if not path: return
        content = self.log_box.get("1.0", "end")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._log(f"✓ Log saved to {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # ── upload loop ───────────────────────────────────────────────────────────
    def _run(self):
        done_ids = set(self.app_state["uploaded"])
        pending  = [v for v in self.videos if v["id"] not in done_ids]
        total    = len(self.videos)
        base     = len(done_ids)
        ok   = len(self.app_state["uploaded"])
        fail = len(self.app_state["failed"])
        skip = len(self.app_state["skipped"])
        do_move  = self.v_move.get()

        self._log(f"Starting: {len(pending)} to upload, {base} already done.")
        if do_move:
            self._log("File moving enabled: done/ and failed/ subfolders will be used.")

        for i, video in enumerate(pending):
            if self.stop_flag.is_set():
                self._log("⚠ Stopped by user."); break

            num = base + i + 1
            self._log(f"\n[{num}/{total}] {video['stem']}")
            self.after(0, lambda n=num, t=total, s=video["stem"][:60]: (
                self.bar_overall.config(value=int(n / t * 100)),
                self.lbl_overall.config(text=f"{n} / {t} videos"),
                self.lbl_file.config(text=s),
            ))
            self.after(0, lambda: (
                self.bar_file.config(value=0),
                self.lbl_pct.config(text=""),
                self.lbl_speed.config(text=""),
                self.lbl_eta.config(text=""),
            ))
            self._file_start = time.time()
            self._prev_file_bytes = self._session_bytes

            result, identifier = upload_video(
                video,
                channel_name     = self.v_channel.get(),
                ia_collection    = self.v_collection.get(),
                access_key       = self.v_access.get(),
                secret_key       = self.v_secret.get(),
                log_fn           = self._log,
                file_progress_cb = self._on_file_progress,
                dry_run          = self.v_dry.get(),
            )

            if result == "ok":
                self.app_state["uploaded"].append(video["id"])
                ok += 1
                if identifier:
                    self._uploaded_identifiers.append((video["stem"], identifier))
                    self._add_link(video["stem"], identifier)
                if do_move:
                    move_video_files(video, "done", self._log)
            elif result == "fail":
                self.app_state["failed"].append(video["id"])
                fail += 1
                if do_move:
                    move_video_files(video, "failed", self._log)
            else:
                self.app_state["skipped"].append(video["id"])
                skip += 1

            save_state(self.app_state)
            self.after(0, lambda o=ok, f=fail, s=skip: (
                self.lbl_ok.config(text=f"✓ {o} done"),
                self.lbl_fail.config(text=f"✗ {f} failed"),
                self.lbl_skip.config(text=f"↷ {s} skipped"),
            ))

        # ── session summary ───────────────────────────────────────────────────
        elapsed_s  = time.time() - self._session_start
        elapsed_m  = int(elapsed_s // 60)
        elapsed_ss = int(elapsed_s % 60)
        mb_total   = self._session_bytes / 1024 / 1024
        avg_speed  = (mb_total / elapsed_s) if elapsed_s > 0 else 0
        summary    = (f"Session: {elapsed_m}m {elapsed_ss}s  •  "
                      f"{mb_total:.1f} MB transferred  •  "
                      f"avg {avg_speed:.2f} MB/s")

        self._log(f"\n━━━ Session complete ━━━  ✓{ok}  ✗{fail}  ↷{skip}")
        self._log(summary)

        self.after(0, lambda: (
            self.bar_file.config(value=100),
            self.lbl_file.config(text="Complete"),
            self.lbl_speed.config(text=""),
            self.lbl_eta.config(text=summary),
        ))
        self.after(0, lambda s=summary: self.lbl_session_summary.config(text=s))

        self.running = False
        self.after(0, lambda: self.btn_start.config(state="normal"))
        self.after(0, lambda: self.btn_stop.config(state="disabled"))

        if fail:
            self.after(0, lambda: messagebox.showwarning(
                "Done with errors",
                f"✓ {ok} uploaded\n✗ {fail} failed\n\n{summary}\n\n"
                f"Use '↺ Retry failed only' to re-queue them.\nState saved in {STATE_FILE}"))
        else:
            self.after(0, lambda: messagebox.showinfo(
                "Complete",
                f"All done!\n✓ {ok} videos uploaded to Archive.org\n\n{summary}"))


# ─── ENTRY ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    App().mainloop()
