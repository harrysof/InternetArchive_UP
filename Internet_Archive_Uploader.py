#!/usr/bin/env python3
"""
Internet Archive — YouTube Channel Uploader
• Resume support via state file
• Per-file upload progress bar (real bytes sent)
• Speed + ETA display
• Metadata from yt-dlp JSON / description files
• Date parsed from filename (e.g. VideoTitle20151024.mp4)
• All files for each video (mp4 + json + description) go to same IA item
• Successfully uploaded files moved to  <source>/done/
• Failed files moved to                 <source>/failed/
Requirements: pip install requests
"""

import os, json, time, threading, subprocess, sys, shutil, re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime
import urllib.parse
import requests

# ─── STATE ────────────────────────────────────────────────────────────────────
# State file lives next to this script, regardless of CWD at launch time.
STATE_FILE = Path(__file__).parent / "ia_upload_state.json"

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
    base_folder = Path(video["mp4"]).parent
    dest = base_folder / destination_subfolder
    dest.mkdir(exist_ok=True)

    all_paths = [video["mp4"]]
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


def discover_videos(folder):
    folder = Path(folder)
    # Also scan done/ and failed/ subdirs? No — only top-level and non-done/failed subdirs.
    mp4_files = []
    for mp4 in folder.rglob("*.mp4"):
        # Skip files already in done/ or failed/ subfolders
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

        # Try .info.json first (yt-dlp default), then plain .json
        json_file = mp4.with_suffix(".info.json")
        if not json_file.exists():
            json_file = mp4.with_suffix(".json")
        desc_file = mp4.with_suffix(".description")

        upload_date_from_filename = extract_date_from_stem(stem)

        groups[video_id] = {
            "id":                       video_id,
            "stem":                     stem,
            "mp4":                      str(mp4),
            "mp4_size":                 mp4.stat().st_size,
            "json":                     str(json_file) if json_file.exists() else None,
            "description":              str(desc_file) if desc_file.exists() else None,
            "date_from_filename":       upload_date_from_filename,
        }
    return list(groups.values())


# ─── METADATA ─────────────────────────────────────────────────────────────────
def parse_metadata(video, channel_name, ia_collection):
    meta = {
        "mediatype":  "movies",
        "collection": ia_collection or "opensource_movies",
        "subject":    ["YouTube", "video"],
    }
    title = video["stem"]
    description = ""
    upload_date = video.get("date_from_filename", "")   # seed from filename
    uploader = channel_name or ""
    tags = []

    if video["json"]:
        try:
            with open(video["json"], "r", encoding="utf-8") as f:
                info = json.load(f)
            title        = info.get("title", title)
            description  = info.get("description", "")
            # JSON date overrides filename date (more authoritative)
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
            d = upload_date.replace("-", "")   # normalise in case it's already YYYY-MM-DD
            meta["date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        except Exception:
            pass
    if tags: meta["subject"] = ["YouTube", "video"] + tags[:10]
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
    raw  = f"{channel_name or 'yt'}-{video['id']}"
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


# ─── VIDEO UPLOAD ─────────────────────────────────────────────────────────────
def upload_video(video, channel_name, ia_collection, access_key, secret_key,
                 log_fn, file_progress_cb, dry_run=False):
    identifier = build_identifier(video, channel_name)
    meta       = parse_metadata(video, channel_name, ia_collection)
    meta_hdrs  = meta_to_headers(meta)

    # Build ordered file list: mp4 first (carries metadata headers), then sidecar files
    files = [video["mp4"]]
    if video["json"]:        files.append(video["json"])
    if video["description"]: files.append(video["description"])

    log_fn(f"  → Identifier : {identifier}")
    log_fn(f"  → Title      : {meta.get('title','')}")
    log_fn(f"  → Date       : {meta.get('date', '(none)')}")
    log_fn(f"  → Files      : {len(files)}  (all uploading to same IA item)")

    if dry_run:
        log_fn("  [DRY RUN] skipping.")
        time.sleep(0.4)
        return "ok"

    if not access_key or not secret_key:
        log_fn("  ✗ No S3 keys — check credentials.")
        return "fail"

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
            return "fail"

    log_fn("  ✓ Done.")
    return "ok"


# ─── GUI ──────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Internet Archive — YouTube Uploader")
        self.geometry("980x740")
        self.resizable(True, True)
        self.configure(bg="#0d0d0d")
        self.app_state = load_state()
        self.videos    = []
        self.running   = False
        self.stop_flag = threading.Event()
        self._file_start = 0
        self._build_ui()
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

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        BG = "#0d0d0d"; PANEL = "#181818"; ACCENT = "#ff6b35"
        TEXT = "#e8e8e8"; MUTED = "#777"; EBG = "#242424"; BORDER = "#2a2a2a"
        SUCCESS = "#4caf50"; DANGER = "#f44336"; FILE_CLR = "#29b6f6"

        FL = ("Courier New", 9); FB = ("Courier New", 9, "bold")

        # header
        hdr = tk.Frame(self, bg=ACCENT, height=46)
        hdr.pack(fill="x")
        tk.Label(hdr, text="▶  INTERNET ARCHIVE UPLOADER", font=("Courier New", 12, "bold"),
                 bg=ACCENT, fg="#0d0d0d", pady=10).pack(side="left", padx=16)
        tk.Label(hdr, text="YouTube → Archive.org", font=FL,
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

        def entry_row(lbl, var, show=None, browse_dir=False):
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
                tk.Button(r, text="…", font=FL, bg=BORDER, fg=TEXT,
                          relief="flat", command=pick, padx=5).pack(side="left", padx=(3, 0))

        sec("SOURCE")
        self.v_folder = tk.StringVar()
        entry_row("Videos folder", self.v_folder, browse_dir=True)

        sec("IA S3 CREDENTIALS")
        tk.Label(left, text="Get keys → archive.org/account/s3.php",
                 font=("Courier New", 8), bg=PANEL, fg="#555").pack(anchor="w", padx=14, pady=(0,4))
        self.v_access = tk.StringVar()
        self.v_secret = tk.StringVar()
        entry_row("Access Key", self.v_access)
        entry_row("Secret Key", self.v_secret, show="●")

        sec("SETTINGS")
        self.v_channel    = tk.StringVar()
        self.v_collection = tk.StringVar(value="opensource_movies")
        entry_row("Channel name", self.v_channel)
        entry_row("Collection", self.v_collection)

        self.v_dry = tk.BooleanVar(value=False)
        tk.Checkbutton(left, text="Dry Run (simulate, no upload)", variable=self.v_dry,
                       font=FL, bg=PANEL, fg=TEXT, selectcolor=EBG,
                       activebackground=PANEL, activeforeground=ACCENT
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
                                    font=("Courier New", 11, "bold"),
                                    bg=ACCENT, fg="#0d0d0d", relief="flat", pady=10,
                                    command=self._start)
        self.btn_start.pack(fill="x", pady=(0, 4))

        self.btn_stop = tk.Button(btns, text="■  STOP", font=FB,
                                   bg="#252525", fg=TEXT, relief="flat", pady=9,
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(fill="x")

        tk.Button(btns, text="🗑  Reset state (re-upload all)", font=FL,
                  bg=BG, fg=MUTED, relief="flat", pady=4,
                  command=self._reset_state).pack(fill="x", pady=(7, 0))

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

        self.lbl_overall = tk.Label(s1, text="0 / 0 videos", font=FL, bg=PANEL, fg=TEXT)
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

        # log
        tk.Label(right, text="LOG", font=FB, bg=BG, fg=MUTED
                 ).pack(anchor="w", padx=14, pady=(8, 1))
        self.log_box = scrolledtext.ScrolledText(
            right, font=("Courier New", 8), bg="#080808", fg="#999",
            insertbackground=ACCENT, relief="flat", bd=0, state="disabled")
        self.log_box.pack(fill="both", expand=True)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _log(self, msg):
        def _do():
            self.log_box.config(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_box.insert("end", f"[{ts}] {msg}\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, _do)

    def _on_file_progress(self, sent, total):
        pct     = int(sent / total * 100) if total else 0
        s_mb    = sent  / 1024 / 1024
        t_mb    = total / 1024 / 1024
        elapsed = time.time() - self._file_start
        speed   = (sent / elapsed / 1024 / 1024) if elapsed > 0.1 else 0
        eta     = ((total - sent) / (sent / elapsed)) if sent > 0 and elapsed > 0.1 else 0
        eta_s   = f"ETA {int(eta//60)}m {int(eta%60)}s" if eta > 0 else "ETA …"
        sp_s    = f"{speed:.2f} MB/s" if speed > 0 else ""

        def _do():
            self.bar_file["value"] = pct
            self.lbl_pct.config(text=f"{s_mb:.1f} / {t_mb:.1f} MB  ({pct}%)")
            self.lbl_speed.config(text=sp_s)
            self.lbl_eta.config(text=eta_s)
        self.after(0, _do)

    # ── actions ───────────────────────────────────────────────────────────────
    def _scan(self):
        folder = self.v_folder.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Error", "Select a valid folder."); return
        self.videos = discover_videos(folder)
        done = set(self.app_state["uploaded"])
        pend = [v for v in self.videos if v["id"] not in done]
        total_gb = sum(v["mp4_size"] for v in self.videos) / 1024**3
        self.lbl_stats.config(
            text=f"{len(self.videos)} videos  ({total_gb:.1f} GB)\n"
                 f"{len(pend)} pending  •  {len(done)} done")
        if done:
            self.lbl_resume.config(text=f"↷ Resume: {len(done)} already uploaded")
        self._log(f"Scan: {len(self.videos)} videos, {total_gb:.1f} GB")

    def _start(self):
        if not self.v_folder.get():
            messagebox.showerror("Error", "Select a folder first."); return
        if not self.videos: self._scan()
        if not self.videos:
            messagebox.showerror("Error", "No MP4 files found."); return
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
        if messagebox.askyesno("Reset", "Clear upload history and re-upload all?"):
            self.app_state = {"uploaded": [], "failed": [], "skipped": []}
            save_state(self.app_state)
            self.lbl_resume.config(text="")
            self._log("State cleared.")

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

            result = upload_video(
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

        self._log(f"\n━━━ Session complete ━━━  ✓{ok}  ✗{fail}  ↷{skip}")
        self.after(0, lambda: (
            self.bar_file.config(value=100),
            self.lbl_file.config(text="Complete"),
            self.lbl_speed.config(text=""),
            self.lbl_eta.config(text=""),
        ))
        self.running = False
        self.after(0, lambda: self.btn_start.config(state="normal"))
        self.after(0, lambda: self.btn_stop.config(state="disabled"))

        if fail:
            self.after(0, lambda: messagebox.showwarning(
                "Done with errors",
                f"✓ {ok} uploaded\n✗ {fail} failed\n\nFailed files moved to failed/\nState saved in {STATE_FILE}"))
        else:
            self.after(0, lambda: messagebox.showinfo(
                "Complete", f"All done!\n✓ {ok} videos uploaded to Archive.org\nFiles moved to done/"))


# ─── ENTRY ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    App().mainloop()
