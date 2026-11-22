# Duplikill

Thin out a big photo archive, fast. Duplikill finds duplicate and similar pictures, shows every group
**on one screen**, you cross out what should go, fire, next group.

Black and white, pixel font, keyboard and mouse driven. Windows. By **metamountain**.

![logo](assets/duplikill.png)

## How it works

1. **Open a folder.** Big folders get a warning with a time estimate and a list of subfolders to pick instead.
2. **Search.** The [Czkawka](https://github.com/qarmin/czkawka) image engine (gradient hash, 8 px) finds
   candidates; a second check (structure, colour layout, capture date, no chaining) throws out look-alikes
   and joins series that Czkawka split.
3. **Overview.** Few groups: a floating net of real thumbnails. Many groups: one pixel clump per folder,
   one square per image. The similarity slider at the bottom searches again live.
4. **Review.** A whole group fits on the screen, edge to edge.

## Controls (review)

| Input | Action |
|---|---|
| Left click on an image | toggle delete cross |
| Skull (top left of the image under the mouse) | remove this one image right away |
| Del / Backspace / the `n X DELETE` button | remove the crossed-out images, next group |
| Mouse wheel | synchronous zoom, same spot in every image, pixel-exact from 100 % |
| Middle drag | pan |
| Hold right button | blink: every cell shows the next image |
| Tab / mouse side buttons | single image / switch |
| Space, → / ← | next / previous group |
| 0 | fit |
| Ctrl+Z | undo |
| Esc | one level back (double Esc on the start screen quits) |
| S | settings (start screen and overview) |
| Del (overview) | "at the end" mode: remove every crossed-out image at once |
| F11 | fullscreen / window |

At least one image of a group always stays. Nothing is ever removed automatically.

**Removing** means *moving* by default: the files go to a `Duplikill` folder on the same drive
(`D:\Pictures\2019\a.jpg` -> `D:\Duplikill\Pictures\2019\a.jpg`, instant, no copying), XMP sidecars included,
and can be undone (Ctrl+Z). In the settings (`S`) the target can be the drive's `Duplikill` folder, a `Duplikill`
folder inside the scanned folder, or a folder of your own; removing can also use the recycle bin or delete
forever (asked once per session). Deleting can happen per group or all at the end from the overview. The recycle bin is the other option.
Settings live in `%LOCALAPPDATA%\Duplikill\settings.json`.

## Build

Needs Python 3.14 and Windows.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Download `windows_czkawka_cli.exe` 12.0.2 from the
[Czkawka releases](https://github.com/qarmin/czkawka/releases) and put it into `tools\`.

```
.venv\Scripts\python -m duplikill --dry-run      # run without touching files
.venv\Scripts\python -m pytest -q tests          # tests
.venv\Scripts\python tools\build_exe.py          # dist\Duplikill\Duplikill.exe
```

## Credits

- [Czkawka](https://github.com/qarmin/czkawka) by Rafał Mikrut (MIT) does the image hashing.
- [C64 font](https://github.com/zeiche/c64font) (CC0), see `tools/fonts/LICENSE-c64font.md`.

## License

MIT, see [LICENSE](LICENSE).
