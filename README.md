# Duplikill

Thin out a big photo archive fast. Duplikill finds duplicates and near-identical series, shows each group
on one screen, you cross out what should go, and move on to the next group.

**[⬇ Duplikill-Setup.exe](https://github.com/metamountain/duplikill/releases/latest/download/Duplikill-Setup.exe)** (installer) ·
**[⬇ Portable ZIP](https://github.com/metamountain/duplikill/releases/latest/download/Duplikill-Portable-win64.zip)** ·
Windows 10/11, 64-bit, no Python needed.

![Overview](assets/overview.svg)

## Fast scan

- **Stage 1: [Czkawka](https://github.com/qarmin/czkawka)** by Rafał Mikrut ([@qarmin](https://github.com/qarmin)), the Rust
  engine behind Krokiet. It hashes about 80 new photos per second (4,108 photos from 2011: 53 s). Images it has
  already seen come from its cache: the same folder again takes about 4 s. The cache is shared with an installed
  Krokiet.
- **Stage 2: verification.** Fast hashes sometimes put unrelated images together, or split one long series. Duplikill
  checks every pair for structure, colour and capture date. A group is kept only if every image matches every other
  one (no A→B→C chains), and series split by stage 1 are joined again. 400 groups / 2,400 photos: 0.14 s.
- **Results are saved.** Reopen a folder and the groups, your crosses and your position are back at once. Duplikill
  only rescans if images were added, changed or removed in the meantime.
- **Big folders:** scans longer than about 90 s are announced first. You can pick a subfolder (click or 1–9), or exclude
  one (right click / Shift+1–9).
- **Similarity slider** (`STRICT … LOOSE`) in the overview: changing it searches again in a fraction of a second.

## Fast comparison

![Review](assets/review.svg)

- **The whole group fits on one screen**, edge to edge.
- **Synchronous zoom:** the mouse wheel zooms every image of the group into the same spot (e.g. the eyes of a
  portrait). From 100 % on, the originals are shown **pixel for pixel**, so you can see blur and noise.
- **Blink:** hold the right mouse button and every image shows its neighbour at the same zoom and spot.
- **BEST** blinks on the sharpest full-size image. `KEEP BEST` crosses out all the others.
- **Click = cross, Del = remove crossed and go to the next group.** The skull removes a single image at once.
  `Ctrl+Z` brings back the last group.

## Removing is safe

- By default files are **moved**, not deleted, into one bin per drive: `<drive>:\Duplikill-Bin\<original path>`
  (instant, no copying; XMP sidecars go along). It is a normal folder: look at it in Explorer, delete it when you are
  sure. Windows' recycle bin and permanent delete can be chosen in the settings, but the recycle bin has a size limit
  (beyond it Windows deletes for good) and none on network drives.
- **Get single images back:** `B` opens the BIN view, all removed images as thumbnails, newest first. Click the ones
  you want and press `R`: they go back to where they were, also days later. `Ctrl+Z` undoes the last delete.
- At least one image of every group always stays. Nothing is removed without your click.

## Best-of folders and exclusions

Do you collect your favourites as copies in a "best of" folder? Duplikill notices folders whose images mostly exist
elsewhere and asks once: exclude it (those duplicates are on purpose) or keep scanning it. Excluded folders can also
be managed in the settings (`S`).

## Controls

| Input | Action |
|---|---|
| Click image | cross / uncross |
| `Del` / `Backspace` | remove crossed, next group |
| Skull on an image | remove just this image now |
| `KEEP BEST` / `ONLY` | cross out all but the best / all but this one |
| Mouse wheel · `0` | synchronous zoom at the cursor · fit |
| Middle drag · hold right | pan · blink compare (swappable in settings) |
| `Space` `→` / `←` | next / previous group |
| `Tab` | one image alone, again = next one |
| `Ctrl+Z` | undo |
| `Esc` | one step back |
| `S` · `F1` · `F11` | settings · help · fullscreen |
| `B` | bin: see removed images, restore single ones (`R`) |
| `U` | install an update (shown when GitHub has a newer version) |
| `F8` | report a bug |

## Privacy

Duplikill sends nothing by itself. At startup it asks GitHub whether there is a newer release. `F8` (bug report) and
"share numbers" in the settings open a prefilled GitHub issue in your browser with folder paths removed. You read it
and decide whether to submit it. Statistics (images removed, space freed) stay on your computer.

## Build from source

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -I tools\build_exe.py          # exe + portable ZIP + Setup.exe
.venv\Scripts\python -I -m duplikill --dry-run      # run without touching any file
.venv\Scripts\python -I -m pytest -q tests
```

## Credits & license

- [Czkawka / Krokiet](https://github.com/qarmin/czkawka) by Rafał Mikrut (MIT): the scan engine.
- Pixel font: [c64font](https://github.com/zeiche/c64font) (CC0).
- Duplikill: [MIT License](LICENSE).
