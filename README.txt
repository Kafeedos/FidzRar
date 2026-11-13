FidzRar  -  extract almost any archive, with two clicks
=======================================================

TO USE IT
  Double-click  FidzRar.exe  (in the "dist" folder, or use the FidzRar shortcut).

  1. Press "Browse..." and pick an archive.
     For a split archive, pick ANY one part - the rest are found automatically.
  2. Check the file list and the "Extract to" folder.
  3. Press Extract.  (Or "Test archive" to only check it for damage.)
  4. If the archive is encrypted, type the password in the Password box first.
  5. You can also drag an archive from Explorer and drop it anywhere on the
     window, or right-click an archive and choose "Extract with FidzRar"
     (see RIGHT-CLICK MENU below).

WHAT IT OPENS
  zip, zipx, 7z, rar, rar5, tar, tar.gz/tgz, tar.bz2, tar.xz, tar.zst,
  gz, bz2, xz, zst, lzma, Z, cab, iso, udf, wim/swm/esd, arj, lzh/lha,
  chm, msi, rpm, deb, cpio, dmg, xar, squashfs, split volumes.

  Split archives handled in all common naming styles:
      Name.z01 + Name.z02 + ... + Name.zip     (WinRAR / WinZip spanning)
      Name.zip.part1 + Name.zip.part2          (byte split)
      Name.zip.001 + Name.zip.002              (7-Zip / HJSplit style)

  Every extracted file is checked (CRC where the format stores one), so you
  find out immediately if a part was truncated by a bad download.

SHARING IT
  FidzRar.exe is a single self-contained file (~11 MB). Nothing to install.
  Copy it to a USB stick, email it, whatever. Windows SmartScreen may warn
  about an "unknown publisher" the first time on another PC - that is normal
  for a homemade exe; choose "More info" -> "Run anyway".

RIGHT-CLICK MENU
  Open Tools -> "Add to right-click menu" and Explorer will offer
  "Extract with FidzRar" when you right-click an archive (or the first part
  of a split set). It loads the file and starts extracting to the usual
  folder next to it.

  Tools -> "Remove from right-click menu" undoes it. The entry is stored
  for your Windows user only (HKEY_CURRENT_USER), needs no administrator
  rights, and points at the exe in its current folder - re-add it if you
  move the exe. On Windows 11 it may sit under "Show more options".

  Drag & drop needs the tkinterdnd2 package when running from source. The
  built exe already includes it; if it is missing, everything else still
  works.

FILES
  dist\FidzRar.exe        the app (this is the one to share)
  FidzRar.pyw             source - the window
  archive_engine.py       source - chooses the right engine per format
  extract_span_zip.py     source - the split-ZIP engine
  shell_menu.py           source - the Explorer right-click menu
  requirements.txt        optional Python packages for a source run
  bin\7z.exe, bin\7z.dll  the 7-Zip engine it drives

  Rebuild the exe with:
      pyinstaller --noconfirm --clean --onefile --windowed --name FidzRar ^
        --collect-all tkinterdnd2 ^
        --add-binary "bin\7z.exe;bin" --add-binary "bin\7z.dll;bin" ^
        --add-data "bin\License.txt;bin" FidzRar.pyw

LICENCE NOTE
  7-Zip (bin\7z.exe, bin\7z.dll) is (c) Igor Pavlov, LGPL-2.1+.
   Its licence is in bin\License.txt and must stay with the app when shared.

Command line (optional, same exe):
  FidzRar.exe --cli "D:\file.rar" -o "D:\out" [-p password] [-t] [--log run.txt]
