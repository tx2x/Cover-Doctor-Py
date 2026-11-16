<div align="center">

# Cover Doctor Py

**Find broken & missing album covers, fetch the right artwork from the internet, and keep your library tidy.**

![Python](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![Formats](https://img.shields.io/badge/formats-MP3%20%7C%20FLAC%20%7C%20M4A%20%7C%20OGG%20%7C%20Opus%20%7C%20WMA-success)
![Dependencies](https://img.shields.io/badge/deps-mutagen%20%2B%20Pillow-orange)

</div>

---

## Features

- **Real validation, not just "tag exists".** The embedded image is fully decoded — truncated, corrupted or non-image data is reported as a *broken* cover.
- **Automatic sorting**
  - broken cover → `Bad_Cover/`
  - no cover → `No_Cover/`
- **Automatic cover download** (enabled by default) from several sources, with fuzzy artist/album matching to avoid wrong artwork:
  1. iTunes Search API
  2. Deezer API
  3. MusicBrainz + Cover Art Archive
- **Broken covers are replaced** — the new image never gets added as a second picture.
- **You choose what happens to fixed files:**
  1. replace the original in place,
  2. move the updated file to `Added_Cover/`,
  3. keep the original and save a copy with the `_1` suffix.
- **Unicode-safe.** Cyrillic, CJK, accented and other non-English tags are never rewritten, so they cannot be damaged. ID3v2.3/2.4 tags keep their original version.
- **Offline-aware.** If there is no internet, a highly visible warning is shown and you decide whether to continue without downloading.
- **Everything is logged.** Every problem is written to `cover_doctor.log` with a plain-English explanation.
- **Interactive and friendly** — sensible defaults, optional recursive scan, "start over" at the end.

## Supported formats

| Format | Extensions | Cover storage |
|---|---|---|
| MP3 | `.mp3` | ID3 `APIC` frame |
| FLAC | `.flac` | `PICTURE` block |
| MPEG-4 audio/video | `.m4a` `.m4b` `.mp4` | `covr` atom |
| Ogg Vorbis / Opus | `.ogg` `.oga` `.opus` | `METADATA_BLOCK_PICTURE` |
| Windows Media Audio | `.wma` | `WM/Picture` |

## Quick start

```bash
git clone https://github.com/your-name/cover-doctor.git
cd cover-doctor
pip install -r requirements.txt
python cover_doctor.py
```

You can also pass the folder directly (or drag & drop it onto the script on Windows):

```bash
python cover_doctor.py "D:\Music\Rock"
```

## What you will be asked

| # | Question | Default |
|---|---|---|
| 1 | Folder to scan | current folder |
| 2 | Scan subdirectories as well? | **No** |
| 3 | Download missing/broken covers from the internet? | **Yes** |
| 4 | *(if no internet)* Continue WITHOUT downloading covers? | **No** |
| 5 | What to do with files that received a new cover? (replace / `Added_Cover` / `_1` copy) | **Replace** |
| 6 | Start over or exit? | **Exit** |

### Example session

```text
=== Cover Doctor Py 1.0.0 ===
Folder to scan [/home/me/Music]:
Scan subdirectories as well? [y/N]: y
Download missing/broken covers from the internet? [Y/n]:
Checking internet connection...
Connection OK.

What should be done with files that received a new cover?
  1) Replace the original file (cover is written in place) (default)
  2) Move the updated file to 'Added_Cover'
  3) Keep the original and save a copy with the '_1' suffix
Your choice [1]:

Found 4 media file(s). Working...

[1/4] Queen/Bohemian Rhapsody.mp3: OK
[2/4] Кино/Группа крови.mp3: no cover -> downloaded -> cover replaced in place
[3/4] Unknown/track07.flac: BAD cover -> nothing found online -> moved to Bad_Cover
[4/4] Demo/song.mp3: ERROR (see log) - file left untouched

==============================================
  Files scanned        : 4
  Cover OK             : 1
  No cover found       : 1
  Broken cover found   : 2
  New covers added     : 1
  Moved to No_Cover    : 0
  Moved to Bad_Cover   : 1
  Errors               : 1
==============================================
```

## How it works

1. **Collect** supported files (output folders `Bad_Cover`, `No_Cover`, `Added_Cover` in the root are skipped).
2. **Inspect** each file:
   - no embedded picture → *no cover*;
   - pictures exist but none can be decoded → *broken cover*;
   - at least one picture decodes correctly → *OK*.
3. **Search** online using the artist / album / title tags (falls back to `Artist - Title` file names). Results are cached per album, so an album is searched only once.
4. **Write** the artwork as the front cover. Existing (broken) pictures are removed first. Non-JPEG/PNG images are converted to JPEG.
5. **Sort** whatever is still unfixed into `No_Cover` / `Bad_Cover`.

All new files are built from a temporary copy, so a failed write never damages the original.

## Log file

Errors and warnings go to `cover_doctor.log` next to the script. Each entry says *which file*, *what happened*, the *likely reason* and the *technical details*:

```text
2026-01-15 18:20:11 | ERROR   | File: D:\Music\a.mp3 | What happened: The file could not be checked and was left untouched | Likely reason: The file is damaged, or its tag structure cannot be read/written by the tag library. | Technical details: HeaderNotFoundError: can't sync to MPEG frame
```

## Good to know

- **Back up your library first.** Option 1 (replace) modifies files in place — that is the point of the tool, but backups are always wise.
- In recursive mode, moved files are placed **flat** into `Bad_Cover` / `No_Cover` / `Added_Cover` in the scanned root. Name collisions are resolved by appending `_1`, `_2`, …
- With option 3 the original file stays where it is, so running the tool again will create another copy (`_2`, …) for it.
- A file is considered *OK* if at least one embedded picture is valid.
- Cover matching relies on tags. Files without an artist tag (and without `Artist - Title` in the name) cannot be searched online and end up in the problem folders.
- Online sources can be wrong or rate-limited; a source that fails 5 times in a row is skipped for the rest of the run.
- Text tags are only read, never written. For MP3, tags are saved in their original ID3 version (v2.2 is upgraded to v2.3/2.4 by the tag library).

## Troubleshooting

| Problem | Solution |
|---|---|
| `Missing dependencies` | `pip install -r requirements.txt` |
| "No internet" banner although you are online | Check proxy/firewall — the script needs HTTPS access to `itunes.apple.com`, `api.deezer.com`, `musicbrainz.org`, `coverartarchive.org` |
| Files stay in place with `ERROR` | Open `cover_doctor.log` — the reason is explained there (locked file, damaged file, permissions, …) |

## Credits
The code was written using web version of Claude Sonnet 5.5 😁.

## License

[MIT](LICENSE)
