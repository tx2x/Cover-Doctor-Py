#!/usr/bin/env python3
"""
Cover Doctor Py - find broken / missing album covers in your media files,
download replacements from the internet and sort out the rest.

* Broken cover   -> file is moved to  ./Bad_Cover   (unless a new cover is found)
* Missing cover  -> file is moved to  ./No_Cover    (unless a new cover is found)
* Errors         -> written to        cover_doctor.log (next to this script)

Requires: Python 3.8+, mutagen, Pillow   (pip install -r requirements.txt)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import shutil
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

try:
    import mutagen
    from mutagen.asf import ASF, ASFByteArrayAttribute
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from PIL import Image
except ImportError:  # pragma: no cover
    print("Missing dependencies. Please run:  pip install mutagen Pillow")
    sys.exit(1)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
APP_NAME = "Cover Doctor Py"
VERSION = "1.0.0"
USER_AGENT = f"CoverDoctor/{VERSION} (https://github.com/your-name/cover-doctor)"

BAD_DIR = "Bad_Cover"
NO_DIR = "No_Cover"
ADDED_DIR = "Added_Cover"
OUTPUT_DIRS = {BAD_DIR, NO_DIR, ADDED_DIR}

LOG_NAME = "cover_doctor.log"
TMP_MARK = ".__cd_tmp__"
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
HTTP_TIMEOUT = 15
SOURCE_MAX_FAILS = 5

# extension -> tag family
SUPPORTED = {
    ".mp3": "id3",
    ".flac": "flac",
    ".m4a": "mp4",
    ".m4b": "mp4",
    ".mp4": "mp4",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "ogg",
    ".wma": "asf",
}

ST_OK, ST_NONE, ST_BAD = "ok", "none", "bad"
ACT_REPLACE, ACT_MOVE, ACT_COPY = "replace", "move", "copy"

# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #
USE_COLOR = sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def banner(lines, code="1;97;41"):
    width = max(len(x) for x in lines) + 4
    print()
    print(c("!" * width, code))
    for line in lines:
        print(c(f"  {line.ljust(width - 4)}  ", code))
    print(c("!" * width, code))
    print()


def ask_yes_no(question: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{question} {hint}: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def ask_choice(question: str, options, default: str) -> str:
    """options: list of (key, description)."""
    print(question)
    for key, desc in options:
        mark = " (default)" if key == default else ""
        print(f"  {key}) {desc}{mark}")
    valid = {k for k, _ in options}
    while True:
        answer = input(f"Your choice [{default}]: ").strip()
        if not answer:
            return default
        if answer in valid:
            return answer
        print(f"Please enter one of: {', '.join(sorted(valid))}")


def ask_folder(preset: str | None) -> Path:
    if preset:
        p = Path(preset.strip().strip('"').strip("'")).expanduser()
        if p.is_dir():
            return p.resolve()
        print(f"Folder not found: {p}")
    default = Path.cwd()
    while True:
        answer = input(f"Folder to scan [{default}]: ").strip().strip('"').strip("'")
        p = Path(answer).expanduser() if answer else default
        if p.is_dir():
            return p.resolve()
        print("This folder does not exist. Please try again.")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logger = logging.getLogger("cover_doctor")


def setup_logging() -> Path:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    candidates = [Path(__file__).resolve().with_name(LOG_NAME), Path.cwd() / LOG_NAME]
    for path in candidates:
        try:
            handler = logging.FileHandler(path, encoding="utf-8")
        except OSError:
            continue
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
        logger.addHandler(handler)
        return path
    logger.addHandler(logging.NullHandler())
    return candidates[0]


class EmptyFileError(Exception):
    pass


class UnsupportedMediaError(Exception):
    pass


class NotFound(Exception):
    pass


def explain(exc: BaseException) -> str:
    if isinstance(exc, EmptyFileError):
        return "The file is empty (0 bytes), so there is nothing to check."
    if isinstance(exc, UnsupportedMediaError):
        return "The file is not recognised as valid media (wrong extension or corrupted data)."
    if isinstance(exc, PermissionError):
        return "Permission denied. The file may be open in another program, read-only or protected."
    if isinstance(exc, FileNotFoundError):
        return "The file or folder disappeared (moved or deleted while the script was running)."
    if isinstance(exc, urllib.error.HTTPError):
        return f"The server answered with HTTP error {exc.code}."
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return "Network problem: the server is unreachable or the connection timed out."
    if isinstance(exc, json.JSONDecodeError):
        return "The server returned an unexpected (non-JSON) answer."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "Not enough free disk space."
    if isinstance(exc, mutagen.MutagenError):
        return "The file is damaged, or its tag structure cannot be read/written by the tag library."
    if isinstance(exc, OSError):
        return "A file-system error occurred (disk problem, invalid name or path too long)."
    return "Unexpected error."


def log_problem(message, exc=None, path=None, level=logging.ERROR):
    parts = []
    if path is not None:
        parts.append(f"File: {path}")
    parts.append(f"What happened: {message}")
    if exc is not None:
        parts.append(f"Likely reason: {explain(exc)}")
        details = str(exc).replace("\n", " ").replace("\r", " ")
        parts.append(f"Technical details: {type(exc).__name__}: {details}")
    logger.log(level, " | ".join(parts))


# --------------------------------------------------------------------------- #
# Image validation / preparation
# --------------------------------------------------------------------------- #
def is_valid_image(data: bytes) -> bool:
    """True only if Pillow can fully decode the image (truncated files fail)."""
    if not data or len(data) < 32:
        return False
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return im.width > 0 and im.height > 0
    except Exception:
        return False


def prepare_image(data: bytes):
    """Return (bytes, mime, width, height, depth); convert exotic formats to JPEG."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        w, h = im.size
        fmt, mode = im.format, im.mode
        if fmt == "JPEG" and mode in ("RGB", "L"):
            return data, "image/jpeg", w, h, 24
        if fmt == "PNG":
            return data, "image/png", w, h, 32 if "A" in mode else 24
        rgb = im.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=95)
    return buf.getvalue(), "image/jpeg", w, h, 24


# --------------------------------------------------------------------------- #
# Reading covers
# --------------------------------------------------------------------------- #
def parse_wm_picture(blob: bytes) -> bytes:
    try:
        size = struct.unpack("<I", blob[1:5])[0]
        pos = 5
        for _ in range(2):  # skip MIME and description (UTF-16LE, null-terminated)
            while blob[pos:pos + 2] != b"\x00\x00":
                if pos >= len(blob):
                    raise ValueError("truncated")
                pos += 2
            pos += 2
        return bytes(blob[pos:pos + size])
    except Exception:
        return b""


def read_pictures(path: Path):
    """Return a list with the raw bytes of every embedded picture."""
    kind = SUPPORTED[path.suffix.lower()]
    if kind == "id3":
        try:
            tags = ID3(path, translate=False)
        except ID3NoHeaderError:
            return []
        return [bytes(f.data) for f in tags.getall("APIC") + tags.getall("PIC")]
    if kind == "flac":
        return [bytes(p.data) for p in FLAC(path).pictures]
    if kind == "mp4":
        audio = MP4(path)
        return [bytes(x) for x in (audio.tags or {}).get("covr", [])]
    if kind == "asf":
        audio = ASF(path)
        return [parse_wm_picture(a.value) for a in (audio.tags or {}).get("WM/Picture", [])]
    if kind == "ogg":
        audio = mutagen.File(path)
        if audio is None:
            raise UnsupportedMediaError()
        if audio.tags is None:
            return []
        pics = []
        for b64 in audio.tags.get("metadata_block_picture", []):
            try:
                pics.append(bytes(Picture(base64.b64decode(b64)).data))
            except Exception:
                pics.append(b"")
        for b64 in audio.tags.get("coverart", []):  # legacy style
            try:
                pics.append(base64.b64decode(b64))
            except Exception:
                pics.append(b"")
        return pics
    return []


def inspect_file(path: Path) -> str:
    if path.stat().st_size == 0:
        raise EmptyFileError()
    if mutagen.File(path) is None:
        raise UnsupportedMediaError()
    pics = read_pictures(path)
    if not pics:
        return ST_NONE
    if any(is_valid_image(p) for p in pics):
        return ST_OK
    return ST_BAD


# --------------------------------------------------------------------------- #
# Writing covers
# --------------------------------------------------------------------------- #
def embed_cover(path: Path, image) -> None:
    data, mime, w, h, depth = image
    kind = SUPPORTED[path.suffix.lower()]

    if kind == "id3":
        try:
            tags = ID3(path, translate=False)  # keep the original tag version/text untouched
        except ID3NoHeaderError:
            tags = ID3()
        if tags.version[1] == 2:  # ID3v2.2 - let mutagen upgrade the frames
            tags = ID3(path)
        v2 = 3 if tags.version[1] == 3 else 4
        tags.delall("APIC")
        tags.delall("PIC")
        tags.add(APIC(encoding=0, mime=mime, type=3, desc="Cover", data=data))
        tags.save(path, v2_version=v2)

    elif kind == "flac":
        audio = FLAC(path)
        audio.clear_pictures()
        audio.add_picture(_make_picture(data, mime, w, h, depth))
        audio.save()

    elif kind == "mp4":
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(data, imageformat=fmt)]
        audio.save()

    elif kind == "ogg":
        audio = mutagen.File(path)
        if audio is None:
            raise UnsupportedMediaError()
        if audio.tags is None:
            audio.add_tags()
        pic = _make_picture(data, mime, w, h, depth)
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        for key in ("coverart", "coverartmime"):
            if key in audio:
                del audio[key]
        audio.save()

    elif kind == "asf":
        audio = ASF(path)
        blob = (
            struct.pack("<bi", 3, len(data))
            + mime.encode("utf-16-le") + b"\x00\x00"
            + "Cover".encode("utf-16-le") + b"\x00\x00"
            + data
        )
        audio.tags["WM/Picture"] = [ASFByteArrayAttribute(blob)]
        audio.save()


def _make_picture(data, mime, w, h, depth) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = mime
    pic.desc = "Cover"
    pic.width, pic.height, pic.depth = w, h, depth
    pic.data = data
    return pic


# --------------------------------------------------------------------------- #
# File operations
# --------------------------------------------------------------------------- #
def numbered_path(path: Path) -> Path:
    n = 1
    while True:
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def build_with_cover(src: Path, dst: Path, image) -> None:
    """Copy src -> dst and embed the cover in dst. The source is never touched."""
    shutil.copy2(src, dst)
    try:
        embed_cover(dst, image)
    except BaseException:
        try:
            dst.unlink()
        except OSError:
            pass
        raise


def deliver(path: Path, image, action: str, root: Path) -> str:
    if action == ACT_REPLACE:
        tmp = path.with_name(f"{path.stem}{TMP_MARK}{path.suffix}")
        build_with_cover(path, tmp, image)
        try:
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return "cover replaced in place"

    if action == ACT_MOVE:
        dest_dir = root / ADDED_DIR
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / path.name
        if dest.exists():
            dest = numbered_path(dest)
        build_with_cover(path, dest, image)
        try:
            path.unlink()
        except BaseException:
            try:
                dest.unlink()
            except OSError:
                pass
            raise
        return f"updated file moved to {ADDED_DIR}"

    dest = numbered_path(path)  # ACT_COPY
    build_with_cover(path, dest, image)
    return f"copy saved as {dest.name}"


def move_to_folder(path: Path, root: Path, folder: str) -> Path:
    dest_dir = root / folder
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = numbered_path(dest)
    shutil.move(str(path), str(dest))
    return dest


def collect_files(root: Path, recursive: bool):
    files = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            if here == root:
                dirnames[:] = [d for d in dirnames if d not in OUTPUT_DIRS]
            dirnames.sort()
            for name in sorted(filenames):
                files.append(here / name)
    else:
        files = sorted(p for p in root.iterdir() if p.is_file())
    return [p for p in files if p.suffix.lower() in SUPPORTED and TMP_MARK not in p.name]


# --------------------------------------------------------------------------- #
# Reading text tags (never written back, so non-English text stays intact)
# --------------------------------------------------------------------------- #
def _first(values) -> str:
    if not values:
        return ""
    v = values[0] if isinstance(values, (list, tuple)) else values
    v = getattr(v, "value", v)
    return str(v).strip()


def read_text_tags(path: Path) -> dict:
    meta = {"artist": "", "album_artist": "", "album": "", "title": ""}
    try:
        if SUPPORTED[path.suffix.lower()] == "asf":
            tags = ASF(path).tags or {}
            meta["artist"] = _first(tags.get("Author"))
            meta["album_artist"] = _first(tags.get("WM/AlbumArtist"))
            meta["album"] = _first(tags.get("WM/AlbumTitle"))
            meta["title"] = _first(tags.get("Title"))
        else:
            audio = mutagen.File(path, easy=True)
            tags = audio.tags if audio is not None and audio.tags is not None else {}
            meta["artist"] = _first(tags.get("artist"))
            meta["album_artist"] = _first(tags.get("albumartist"))
            meta["album"] = _first(tags.get("album"))
            meta["title"] = _first(tags.get("title"))
    except Exception as exc:
        log_problem("Could not read text tags to build the search query; "
                    "falling back to the file name", exc, path, logging.WARNING)

    if not meta["artist"] and not meta["title"] and " - " in path.stem:
        artist, title = path.stem.split(" - ", 1)
        meta["artist"], meta["title"] = artist.strip(), title.strip()
    return meta


# --------------------------------------------------------------------------- #
# Online cover search
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFound(url) from exc
        raise
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("Downloaded file is unexpectedly large")
    return data


def network_available(timeout: int = 5) -> bool:
    for host in ("itunes.apple.com", "api.deezer.com", "musicbrainz.org"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", (text or "").casefold(), flags=re.UNICODE).strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def rank(cands, want_artist: str, want_name: str):
    """cands: [(name, artist, payload)] -> payloads, best match first."""
    various = norm(want_artist) in ("various artists", "various", "va")
    scored = []
    for name, artist, payload in cands:
        name_score = sim(name, want_name)
        if name_score < 0.75:
            continue
        artist_score = 1.0 if various else sim(artist, want_artist)
        if artist_score < 0.6:
            continue
        scored.append((name_score + artist_score, payload))
    scored.sort(key=lambda item: -item[0])
    return [payload for _, payload in scored]


def queries(meta: dict):
    artist = meta.get("album_artist") or meta.get("artist") or ""
    if not artist:
        return []
    out = []
    if meta.get("album"):
        out.append(("album", artist, meta["album"]))
    if meta.get("title"):
        out.append(("track", meta.get("artist") or artist, meta["title"]))
    return out


def src_itunes(meta: dict):
    for kind, artist, name in queries(meta):
        params = {
            "term": f"{artist} {name}", "media": "music", "limit": 10,
            "entity": "album" if kind == "album" else "song",
        }
        data = json.loads(http_get("https://itunes.apple.com/search?" + urllib.parse.urlencode(params)))
        cands = []
        for r in data.get("results", []):
            nm = r.get("collectionName") if kind == "album" else r.get("trackName")
            url = r.get("artworkUrl100")
            if nm and url:
                cands.append((nm, r.get("artistName", ""), url))
        for url in rank(cands, artist, name)[:2]:
            for size in ("1000x1000bb", "600x600bb"):
                try:
                    return http_get(re.sub(r"\d+x\d+bb", size, url))
                except NotFound:
                    continue
    return None


def src_deezer(meta: dict):
    for kind, artist, name in queries(meta):
        endpoint = "album" if kind == "album" else "track"
        params = {"q": f"{artist} {name}", "limit": 10}
        data = json.loads(http_get(f"https://api.deezer.com/search/{endpoint}?" + urllib.parse.urlencode(params)))
        cands = []
        for r in data.get("data", []):
            src = r if kind == "album" else (r.get("album") or {})
            url = src.get("cover_xl") or src.get("cover_big")
            if not url or "/cover//" in url:  # Deezer placeholder
                continue
            cands.append((r.get("title", ""), (r.get("artist") or {}).get("name", ""), url))
        for url in rank(cands, artist, name)[:2]:
            try:
                return http_get(url)
            except NotFound:
                continue
    return None


_mb_last = [0.0]


def src_musicbrainz(meta: dict):
    for kind, artist, name in queries(meta):
        if kind != "album":
            continue
        clean = lambda s: re.sub(r'["\\]', " ", s)
        query = f'release:"{clean(name)}" AND artist:"{clean(artist)}"'
        wait = 1.1 - (time.time() - _mb_last[0])  # MusicBrainz: max 1 request/second
        if wait > 0:
            time.sleep(wait)
        _mb_last[0] = time.time()
        params = {"query": query, "fmt": "json", "limit": 5}
        data = json.loads(http_get("https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode(params)))
        cands = []
        for r in data.get("releases", []):
            credit = r.get("artist-credit") or []
            art = "".join(x.get("name", "") + x.get("joinphrase", "") for x in credit if isinstance(x, dict))
            if r.get("id"):
                cands.append((r.get("title", ""), art, r["id"]))
        for mbid in rank(cands, artist, name)[:3]:
            try:
                return http_get(f"https://coverartarchive.org/release/{mbid}/front-500")
            except NotFound:
                continue
    return None


class Source:
    def __init__(self, name, func):
        self.name, self.func = name, func
        self.fails, self.disabled = 0, False

    def failed(self, exc, path):
        self.fails += 1
        log_problem(f"Cover source '{self.name}' failed", exc, path)
        if self.fails >= SOURCE_MAX_FAILS and not self.disabled:
            self.disabled = True
            msg = f"Source '{self.name}' failed {SOURCE_MAX_FAILS} times in a row and is disabled for this run."
            print(c(f"\n  ! {msg}", "33"))
            logger.warning(msg)


def make_sources():
    return [Source("iTunes", src_itunes), Source("Deezer", src_deezer),
            Source("MusicBrainz/CoverArtArchive", src_musicbrainz)]


# --------------------------------------------------------------------------- #
# Session logic
# --------------------------------------------------------------------------- #
@dataclass
class Context:
    root: Path
    download: bool
    action: str
    sources: list = field(default_factory=list)
    cache: dict = field(default_factory=dict)


@dataclass
class Stats:
    scanned: int = 0
    ok: int = 0
    no_cover: int = 0
    bad_cover: int = 0
    added: int = 0
    moved_no: int = 0
    moved_bad: int = 0
    errors: int = 0


def find_cover(path: Path, ctx: Context):
    meta = read_text_tags(path)
    if not queries(meta):
        log_problem("Not enough tag information (artist plus album or title) to search "
                    "for a cover online", None, path, logging.WARNING)
        return None
    key = (norm(meta["album_artist"] or meta["artist"]), norm(meta["album"])) if meta["album"] \
        else ("track", norm(meta["artist"]), norm(meta["title"]))
    if key in ctx.cache:
        return ctx.cache[key]

    result = None
    for src in ctx.sources:
        if src.disabled:
            continue
        try:
            data = src.func(meta)
        except NotFound:
            continue
        except Exception as exc:
            src.failed(exc, path)
            continue
        src.fails = 0
        if data and is_valid_image(data):
            result = data
            break
    ctx.cache[key] = result
    return result


def process_file(path: Path, ctx: Context, stats: Stats) -> str:
    stats.scanned += 1
    try:
        status = inspect_file(path)
    except Exception as exc:
        stats.errors += 1
        log_problem("The file could not be checked and was left untouched", exc, path)
        return c("ERROR (see log) - file left untouched", "31")

    if status == ST_OK:
        stats.ok += 1
        return c("OK", "32")

    if status == ST_NONE:
        stats.no_cover += 1
        label, folder = "no cover", NO_DIR
    else:
        stats.bad_cover += 1
        label, folder = "BAD cover", BAD_DIR
    steps = [label]

    if ctx.download:
        image = None
        try:
            raw = find_cover(path, ctx)
            if raw:
                image = prepare_image(raw)
        except Exception as exc:
            stats.errors += 1
            log_problem("Unexpected error while searching for / preparing an online cover", exc, path)
        if image:
            try:
                note = deliver(path, image, ctx.action, ctx.root)
                stats.added += 1
                return c(f"{label} -> downloaded -> {note}", "36")
            except Exception as exc:
                stats.errors += 1
                log_problem("The downloaded cover could not be written; "
                            "the original file was left unchanged", exc, path)
                steps.append("could not write cover")
        else:
            steps.append("nothing found online")

    try:
        move_to_folder(path, ctx.root, folder)
        if status == ST_NONE:
            stats.moved_no += 1
        else:
            stats.moved_bad += 1
        steps.append(f"moved to {folder}")
        return c(" -> ".join(steps), "33")
    except Exception as exc:
        stats.errors += 1
        log_problem(f"The file could not be moved to '{folder}'", exc, path)
        steps.append("MOVE FAILED (see log)")
        return c(" -> ".join(steps), "31")


def run_once(preset_folder: str | None, log_path: Path) -> None:
    print(c(f"\n=== {APP_NAME} {VERSION} ===", "1"))
    root = ask_folder(preset_folder)
    recursive = ask_yes_no("Scan subdirectories as well?", False)
    download = ask_yes_no("Download missing/broken covers from the internet?", True)
    action = ACT_REPLACE

    if download:
        print("Checking internet connection...")
        if not network_available():
            banner(["NO INTERNET CONNECTION DETECTED",
                    "Covers cannot be downloaded right now."])
            if not ask_yes_no("Continue WITHOUT downloading covers?", False):
                print("Cancelled.")
                return
            download = False
        else:
            print(c("Connection OK.", "32"))

    if download:
        choice = ask_choice(
            "\nWhat should be done with files that received a new cover?",
            [("1", "Replace the original file (cover is written in place)"),
             ("2", f"Move the updated file to '{ADDED_DIR}'"),
             ("3", "Keep the original and save a copy with the '_1' suffix")],
            "1")
        action = {"1": ACT_REPLACE, "2": ACT_MOVE, "3": ACT_COPY}[choice]

    files = collect_files(root, recursive)
    if not files:
        print("\nNo supported media files found.")
        print("Supported: " + ", ".join(sorted(SUPPORTED)))
        return

    ctx = Context(root=root, download=download, action=action, sources=make_sources() if download else [])
    stats = Stats()
    logger.info("Session started | folder=%s | recursive=%s | download=%s | action=%s | files=%d",
                root, recursive, download, action, len(files))
    print(f"\nFound {len(files)} media file(s). Working...\n")

    total = len(files)
    for i, path in enumerate(files, 1):
        try:
            shown = path.relative_to(root)
        except ValueError:
            shown = path
        print(f"[{i}/{total}] {shown}: ", end="", flush=True)
        print(process_file(path, ctx, stats))

    summary = (f"scanned={stats.scanned} ok={stats.ok} no_cover={stats.no_cover} "
               f"bad_cover={stats.bad_cover} covers_added={stats.added} "
               f"moved_to_No_Cover={stats.moved_no} moved_to_Bad_Cover={stats.moved_bad} "
               f"errors={stats.errors}")
    logger.info("Session finished | %s", summary)

    print("\n" + "=" * 46)
    print(f"  Files scanned        : {stats.scanned}")
    print(f"  Cover OK             : {stats.ok}")
    print(f"  No cover found       : {stats.no_cover}")
    print(f"  Broken cover found   : {stats.bad_cover}")
    print(f"  New covers added     : {stats.added}")
    print(f"  Moved to {NO_DIR:<12}: {stats.moved_no}")
    print(f"  Moved to {BAD_DIR:<12}: {stats.moved_bad}")
    print(f"  Errors               : {stats.errors}")
    print("=" * 46)
    if stats.errors:
        print(c(f"Some problems occurred. Details: {log_path}", "33"))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash on exotic file names
        except Exception:
            pass
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console

    parser = argparse.ArgumentParser(description="Find and fix broken/missing media covers.")
    parser.add_argument("folder", nargs="?", help="folder to scan (asked interactively if omitted)")
    args = parser.parse_args()

    log_path = setup_logging()
    preset = args.folder
    try:
        while True:
            try:
                run_once(preset, log_path)
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception as exc:
                logger.exception("Unexpected crash during a session")
                print(c(f"\nUnexpected error: {exc}\nDetails were written to {log_path}", "31"))
            preset = None
            print()
            again = ask_choice("Start over or exit?",
                               [("s", "Start over"), ("e", "Exit")], "e")
            if again != "s":
                break
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted by user.")
        logger.warning("Session interrupted by user")
    print("Goodbye!")


if __name__ == "__main__":
    main()
