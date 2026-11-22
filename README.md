# Duplikill

Thin out a big photo archive, fast. Duplikill finds duplicate and similar pictures, shows every group **on one screen**, you cross out what should go, fire, next group.

Black and white, integer-scaled 8×8 C64 pixel font, keyboard and mouse driven. Windows. By **metamountain**.

---

## Screenshots & Workflow

### 1. Overview (Floating Net & Folder Clumps)
Few groups float as a live physics net of real thumbnails joined by folder lines; large archives switch automatically to organic pixel clumps (one square per photo, well above 30 fps). The similarity slider (`STRICT <######......> LOOSE`) re-queries the cache in a fraction of a second.

![Overview](assets/overview.svg)

### 2. Review (Edge-to-Edge Compare with Central Seam Switches)
Every group fills the screen edge to edge (`2 px` seam). All action switches cluster right around the **central seam `(W/2, H/2)`** so mouse travel stays minimal (~45 px) and photo centers (faces/subjects) are never covered:
- **2-image pairs:** Both `[☠]` instant-remove switches sit left and right of the vertical center seam directly above the `< i/N >` hub. Clicking one photo sets an `X`; clicking the other swaps the `X` in 1 click.
- **Multi-image groups & bursts (e.g. 6 photos):** Each cell has `[ONLY] [☠]` at its inner seam corner. Click any subset of photos to cross them out (e.g. keep 3 of 6), or click `[ONLY]` / `[KEEP BEST]` to cross out all others in 1 click, then fire `[🗑 n X DEL]` right at the center.

![Review](assets/review.svg)

### 3. Settings (`S`)
Fixed-grid C64 menu with explanatory help lines under every setting, configurable mouse pan (`MIDDLE BUTTON` vs `RIGHT BUTTON`), live journal statistics, and JSON configuration (`%LOCALAPPDATA%\Duplikill\settings.json`).

![Settings](assets/settings.png)

---

## How It Works

1. **Open a folder.** Scans estimated over 90 seconds show a warning with the cached percentage and a 1–9 shortcut list of the largest subfolders.
2. **Two-stage search:**
   - **Stage 1 ([Czkawka](https://github.com/qarmin/czkawka) CLI engine):** 8×8 gradient hash finds candidate groups at ~75–400 images/s (cached runs take sub-seconds).
   - **Stage 2 (`verify.py` refinement):** Re-verifies every candidate pair via 24×24 greyscale structure correlation (`≥ 0.6`), 4×4 colour layout distance (`≤ 0.12`), and EXIF capture date (`DateTimeOriginal`), rebuilds groups with average linkage + floor (no transitive chaining of unrelated photos), and merges burst series split by Stage 1 (`≤ 15 min`). Signatures are cached in `%LOCALAPPDATA%\Duplikill\signatures.sqlite`.
3. **Safe removal & undo:**
   - By default, **removing means moving**: files (plus `.xmp` sidecars) move instantaneously to `<Drive>:\Duplikill\<original path>` on the same drive (no byte copying), logged in `journal.jsonl`, and can be undone anytime with `Ctrl+Z`.
   - In Settings (`S`), you can switch the move target to `<Scanned Folder>\Duplikill\<path>` or a custom folder, or switch removal to the Windows Recycle Bin or permanent deletion (asked once per session).

---

## Controls (Review)

| Input | Action |
|---|---|
| Left click on an image | Toggle delete cross `X` (on 2-image pairs: clicking the other image swaps the cross in 1 click) |
| Skull `[☠]` (inner seam corner of each image) | Remove this one image immediately |
| `[ONLY]` (inner seam corner, 3+ images) | Cross out all other images in the group (click another photo to keep 2 or 3) |
| `[KEEP BEST]` (central hub, 3+ images) | Cross out all except the sharpest / highest-resolution `BEST` image |
| `Del` / `Backspace` / central `[🗑 n X DEL]` | Remove crossed-out images and advance to the next group |
| Central `<` / `>` or `Space`, `→` / `←` | Previous / next group |
| Mouse wheel | Synchronous zoom into the cursor spot across all images (pixel-exact from 100 %) |
| Middle drag (or Right drag, configurable in `S`) | Synchronous pan across all images |
| Hold Right button (or Middle button) | Blink compare: every cell shows the next image at the exact same zoom & spot |
| `Tab` / mouse side buttons | Solo single image / cycle to next |
| `0` / `Home` | Reset zoom to fit |
| `Ctrl+Z` | Undo last move (restores files + XMP sidecars and reopens the group) |
| `Esc` | One level back (double `Esc` on the start screen quits) |
| `S` | Open Settings (from start screen or overview) |
| `F1` | Open / close Help overlay from any screen |
| `Del` (in overview, `AT THE END` mode) | Remove all crossed-out images across all groups at once |
| `F11` | Toggle borderless fullscreen / dark window |

At least one image of a group always stays. Nothing is ever removed automatically.

---

## Build & Run

Requires Python 3.14 and Windows 10/11.

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Download `windows_czkawka_cli.exe` 12.0.2 from the [Czkawka releases](https://github.com/qarmin/czkawka/releases) and place it into `tools\`.

```powershell
.venv\Scripts\python -I -m duplikill --dry-run      # run without touching files
.venv\Scripts\python -I -m pytest -q tests          # run test suite
.venv\Scripts\python -I tools\build_exe.py          # build dist\Duplikill\Duplikill.exe
```

---

## Credits & License

- [Czkawka](https://github.com/qarmin/czkawka) by Rafał Mikrut (MIT) for Stage-1 image hashing.
- [C64 font](https://github.com/zeiche/c64font) (CC0), see `tools/fonts/LICENSE-c64font.md`.
- Licensed under the [MIT License](LICENSE).
