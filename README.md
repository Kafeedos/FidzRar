# FidzRar

A small, friendly archive extractor for Windows. Pick almost any archive —
including **one part of a split set** — and FidzRar finds the other parts,
lists the contents, and extracts them with checksum verification.

It ships as one portable `.exe`: no installer, no admin rights, nothing to
configure.

## Features

- **Wide format support** — zip/zipx, 7z, rar/rar5, tar, tar.gz/tgz,
  tar.bz2, tar.xz, tar.zst, gz, bz2, xz, zst, lzma, Z, cab, iso/udf,
  wim/swm/esd, arj, lzh/lha, chm, msi, rpm, deb, cpio, dmg, xar, squashfs.
- **Split archives** — open *any* part and the rest are found automatically:
  - `Name.z01 + … + Name.zip` (WinRAR / WinZip spanning)
  - `Name.zip.part1 + Name.zip.part2` (byte split)
  - `Name.zip.001 + Name.zip.002` (7-Zip / HJSplit style)
- **Test archive** — verifies CRCs without writing anything.
- **Password box** for encrypted archives.
- **Drag & drop** an archive onto the window.
- **Explorer right-click menu** — “Extract with FidzRar”.
- **Cancel** at any time; no half-written files are left behind.
- Per-file log, determinate progress bar, optional “open folder when finished”.

## Download

Download `FidzRar.exe` from the **Releases** page (or from `dist\` if you
built it yourself). It is a single file, about 12 MB.

Windows SmartScreen may warn about an “unknown publisher” the first time on
another PC. That is normal for an unsigned homemade exe; choose
**More info → Run anyway**.

## Use

1. Press **Browse…** and pick an archive, or drag one onto the window.
2. Check the contents list and the **Extract to** folder.
3. Press **Extract**. Use **Test archive** to only verify it.
4. For an encrypted archive, type the password first.

For a split set, pick any part. FidzRar shows the format, part count, file
count, unpacked size, and which engine is doing the work.

## Right-click menu

Open **Tools → Add to right-click menu**. Explorer will then offer
**Extract with FidzRar** when you right-click an archive (or the first part of
a split set). It loads the file and starts extracting straight away.

- Stored under `HKEY_CURRENT_USER`, so no administrator rights are needed.
- Only affects your Windows user.
- Points at the exe in its current folder — re-add it if you move the exe.
- Remove it with **Tools → Remove from right-click menu**.
- On Windows 11 the entry may appear under **Show more options**.

## Command line

The same exe can run headless, which is handy for scripts and regression
tests:

```powershell
FidzRar.exe --cli "D:\file.rar" -o "D:\out" [-p password] [-t] [--log run.txt]
```

Exit code `0` means every file was extracted (or verified) successfully.
`--extract "file.zip"` opens the window and starts extracting immediately;
that is what the Explorer menu uses.

## Build from source

Requirements: Python 3.13 (x64), PyInstaller, and `tkinterdnd2` for drag &
drop. The 7-Zip binaries are already in `bin\`.

```powershell
python -m pip install tkinterdnd2 pyinstaller

python -m PyInstaller --noconfirm --clean --onefile --windowed --name FidzRar ^
  --add-binary "bin\7z.exe;bin" --add-binary "bin\7z.dll;bin" ^
  --add-data "bin\License.txt;bin" FidzRar.pyw
```

The tkinterdnd2 PyInstaller hook is discovered automatically. Drag & drop is
optional at runtime: without the package the app still works, it just cannot
accept dropped files.

Output: `dist\FidzRar.exe`. Delete `build\`, `FidzRar.spec`, and
`__pycache__\` afterwards.

## How it works

The GUI is plain `tkinter`. Long work runs in a background thread and talks to
the window through a `queue.Queue`; the UI polls it with `after()`. That keeps
the window responsive and makes Cancel reliable.

`archive_engine.py` is the only place that knows about formats. It probes the
input and picks an engine:

| Input | Engine | Why |
|---|---|---|
| split zip as `.partN` / `.00N` | built-in Python reader | 7-Zip cannot open `.partN`, and treats `.00N` as a generic split |
| spanned `.z01 … .zip` | 7-Zip | native volume support |
| `tar`, `tar.gz`, `tar.bz2`, `tar.xz` | Python `tarfile` | one pass, real per-file progress |
| compressor wrapping a `.tar` (e.g. `.tar.zst`) | 7-Zip + two passes | stdlib has no zstd |
| everything else | 7-Zip | widest coverage |

The split-ZIP engine (`extract_span_zip.py`) presents all parts as one
continuous virtual file, so a file that straddles a part boundary still reads
correctly — and no temporary merged copy is needed. Every extracted entry is
CRC-checked before it replaces the temporary `.part_extract` file, and unsafe
paths are rejected.

`shell_menu.py` writes the Explorer verb under `SystemFileAssociations` in
`HKEY_CURRENT_USER`.

## Project layout

```
FidzRar.pyw           the window, CLI and self-test entry points
archive_engine.py     probe()/extract() and engine routing
extract_span_zip.py   split-ZIP reader (virtual Parts file)
shell_menu.py         Explorer right-click integration
bin\7z.exe, 7z.dll    bundled 7-Zip engine
bin\License.txt       7-Zip licence (must ship with the app)
requirements.txt      optional packages for a source run
README.txt            short end-user instructions
```

## Notes and limitations

- Extraction only; FidzRar does not create archives.
- RAR creation is not supported. WinRAR’s console tools are RAR-only, so
  7-Zip is used as the engine.
- Progress inside huge solid 7z/rar archives is coarse: 7-Zip reports a
  percentage, not per-file bytes.
- The exe is unsigned, hence the SmartScreen warning on other PCs.

## Licence

The bundled 7-Zip binaries (`bin\7z.exe`, `bin\7z.dll`) are © Igor Pavlov and
licensed under **LGPL-2.1+**; see `bin\License.txt`. That notice must stay with
the app when it is shared.

The FidzRar source in this repository is shared as-is for personal use.
