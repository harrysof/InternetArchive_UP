# Internet Archive Uploader

A desktop GUI tool for batch-uploading YouTube channel archives to [Archive.org](https://archive.org).

Built for people who download channels with `yt-dlp` and want to preserve them on the Internet Archive — with resume support, real-time progress, and automatic metadata extraction from yt-dlp sidecar files.

---

## Features

- **Batch upload** — scans a folder for all `.mp4` files and uploads each as a separate IA item
- **Resume support** — tracks uploaded files in a local state file; restart anytime without re-uploading
- **Real-time progress** — per-file progress bar with speed (MB/s) and ETA
- **Metadata from yt-dlp** — reads `.info.json` and `.description` sidecar files for title, date, uploader, tags
- **Date parsing** — extracts `YYYYMMDD` from filenames (e.g. `VideoTitle20151024.mp4`) as fallback
- **Auto file organization** — moves uploaded files to `done/` and failed ones to `failed/`
- **Retry logic** — up to 5 attempts per file with 12-second delay between retries
- **Dry run mode** — simulate the full upload process without sending anything
- **Unicode-safe** — handles Arabic, CJK, emoji, and other non-latin-1 characters in metadata headers

---

## Requirements

- Python 3.8+
- `requests` library

```bash
pip install -r requirements.txt
```

Tkinter is included in standard Python on Windows and most Linux distros. On Ubuntu/Debian:

```bash
sudo apt install python3-tk
```

---

## Setup

### 1. Get Internet Archive S3 credentials

Go to [archive.org/account/s3.php](https://archive.org/account/s3.php) and copy your **Access Key** and **Secret Key**.

> Your IA account must exist and be in good standing. A free account is sufficient for most uploads.

### 2. Prepare your video folder

The tool expects files downloaded by `yt-dlp`, typically structured like:

```
/my-channel/
    VideoTitle [abcXYZ123].mp4
    VideoTitle [abcXYZ123].info.json      ← optional but recommended
    VideoTitle [abcXYZ123].description    ← optional
    AnotherVideo20200314 [defABC456].mp4
    ...
```

Downloaded with a command like:
```bash
yt-dlp --write-info-json --write-description -o "%(title)s [%(id)s].%(ext)s" https://www.youtube.com/@channel
```

---

## Usage

```bash
python Internet_Archive_Uploader.py
```

1. **Select folder** — point to the directory containing your `.mp4` files
2. **Enter credentials** — paste your IA Access Key and Secret Key
3. **Set channel name** — used to build the IA identifier (e.g. `mychannel-videoID`)
4. **Set collection** — defaults to `opensource_movies`; leave as-is unless you have a specific IA collection
5. **Scan folder** — previews how many videos were found and total size
6. **Start Upload** — begins uploading; you can stop and resume at any time

### Options

| Option | Description |
|--------|-------------|
| **Dry Run** | Simulates the upload loop without sending any files |
| **Move files after upload** | Moves completed files to `done/` and failed ones to `failed/` |
| **Reset state** | Clears upload history so all files are re-uploaded from scratch |

---

## State file

Upload progress is saved to `ia_upload_state.json` in the same directory as the script. This file tracks which video IDs have been uploaded, failed, or skipped. Do not delete it if you want to resume a session.

---

## IA Item structure

Each video becomes a separate Archive.org item. All associated files (`.mp4`, `.info.json`, `.description`) are uploaded to the same item.

The IA identifier is built as: `{channel_name}-{youtube_id}`, sanitized to alphanumerics and hyphens, max 80 characters.

---

## Limitations

- Credentials are not persisted between sessions — re-enter each time you launch
- Only `.mp4` files are discovered (no `.webm`, `.mkv` etc.)
- Designed for yt-dlp output; non-standard filenames may not have dates parsed correctly

---

## License

MIT
