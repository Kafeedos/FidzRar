#!/usr/bin/env python3
"""
archive_engine.py - one extraction backend for FidzRar.

Picks the best available engine for whatever the user drops in:

  * single-stream & container formats  -> 7-Zip (7z, zip, rar/rar5, cab, iso,
    udf, wim, tar, gz, bz2, xz, zstd, lzma, Z, ar/deb, rpm, cpio, arj, lzh,
    chm, msi, nsis, dmg, squashfs, vhd, qcow, xar, ...)
  * .tar / .tar.gz / .tgz / .tar.bz2 / .tar.xz  -> Python tarfile (one step,
    no temp copy, real per-file progress)
  * ZIP split as .z01+.zip / .part1+.part2 / .001+.002  -> the built-in
    extract_span_zip.py engine (handles cross-part files exactly)
  * .tar.zst and other nested "compressor around a tar"  -> two passes

Public API
----------
    probe(path, password=None) -> info dict
    extract(info, dest, password=None, mode="extract", on_progress=..., on_file=..., cancel=...)
    formats_summary() -> str

Both raise EngineError on failure and Cancelled when the caller cancels.
"""

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib

try:
    import extract_span_zip as core
except Exception:                                    # pragma: no cover
    core = None

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK = 1024 * 1024


class EngineError(Exception):
    pass


class Cancelled(Exception):
    pass


TAR_EXTS = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz",
            ".tar.xz", ".txz", ".tar.zst", ".tar.lzma")


# --------------------------------------------------------------------- utils
def app_dir():
    """Folder holding bundled data (works inside a PyInstaller onefile exe)."""
    return getattr(sys, "_MEIPASS", HERE)


def find_7z():
    """Locate a usable 7z.exe: bundled first, then the machine."""
    candidates = [
        os.path.join(app_dir(), "bin", "7z.exe"),
        os.path.join(HERE, "bin", "7z.exe"),
        shutil.which("7z"),
        shutil.which("7za"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        r"C:\Program Files\NVIDIA Corporation\NVIDIA App\7z.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0


def _cancel_check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _snapshot_tree(dest, limit=50000):
    """Record what is already in dest so a failed run can remove its own junk.

    Returns (files, dirs, truncated).  Huge folders are skipped rather than
    risking a long pause or deleting something we did not create.
    """
    files, dirs, count = set(), set(), 0
    if not os.path.isdir(dest):
        return files, dirs, False
    for root, dnames, fnames in os.walk(dest):
        relroot = os.path.relpath(root, dest)
        for d in dnames:
            dirs.add(os.path.normpath(os.path.join(relroot, d)))
        for f in fnames:
            files.add(os.path.normpath(os.path.join(relroot, f)))
            count += 1
            if count > limit:
                return files, dirs, True
    return files, dirs, False


def _remove_new_tree(dest, before_files, before_dirs):
    """Delete files/dirs that appeared in dest after the snapshot."""
    if not os.path.isdir(dest):
        return
    for root, dnames, fnames in os.walk(dest, topdown=False):
        relroot = os.path.relpath(root, dest)
        for f in fnames:
            rel = os.path.normpath(os.path.join(relroot, f))
            if rel not in before_files:
                try:
                    os.remove(os.path.join(dest, rel))
                except OSError:
                    pass
        for d in dnames:
            rel = os.path.normpath(os.path.join(relroot, d))
            if rel not in before_dirs:
                try:
                    os.rmdir(os.path.join(dest, rel))
                except OSError:
                    pass


def _split_parts(path):
    """Return (stem, parts, style) if this is a split set, else None."""
    if core is None:
        return None
    try:
        return core.find_parts(path)
    except Exception:
        return None


def is_tar_like(path):
    low = path.lower()
    return any(low.endswith(e) for e in TAR_EXTS)


# ---------------------------------------------------------------- 7-Zip layer
def _run7z(args, cancel=None, on_progress=None, on_line=None, total_hint=0):
    sevenz = find_7z()
    if not sevenz:
        raise EngineError("7z.exe not found")

    cmd = [sevenz] + args
    creation = 0
    if sys.platform == "win32":
        creation = 0x08000000                          # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1, creationflags=creation)

    tail = []
    last_pct = -1
    try:
        for raw in proc.stdout:
            _cancel_check(cancel)
            line = raw.rstrip("\r\n")
            if not line:
                continue
            tail.append(line)
            if len(tail) > 400:
                del tail[:200]

            m = re.search(r"(\d{1,3})%", line)
            if m:
                pct = int(m.group(1))
                if pct != last_pct:
                    last_pct = pct
                    if on_progress:
                        on_progress(pct * 10, 1000)
                continue

            if on_line:
                on_line(line)
    finally:
        if cancel is not None and cancel.is_set() and proc.poll() is None:
            proc.kill()
        proc.wait()

    if cancel is not None and cancel.is_set():
        raise Cancelled()

    if proc.returncode != 0:
        errs = [l for l in tail if l.lower().startswith("error")
                or "cannot open" in l.lower() or "wrong password" in l.lower()
                or "data error" in l.lower()]
        msg = errs[-1] if errs else (tail[-1] if tail else "7-Zip failed")
        raise EngineError("7-Zip: %s" % msg.strip())
    return tail


def _probe_7z(path, password=None):
    sevenz = find_7z()
    args = ["l", "-slt", "-y"]
    args.append("-p" + (password or ""))
    args.append(path)
    out = _run7z(args)

    # The listing is: header lines, one archive property block, "----------",
    # then one "Path = ..." record per entry (separators vary by version, so
    # we key off the Path field instead of trusting blank lines).
    sep = None
    for i, line in enumerate(out):
        if line.strip() == "----------":
            sep = i
            break
    head = out[:sep] if sep is not None else out
    body = out[sep + 1:] if sep is not None else []

    def kv(lines):
        d = {}
        for line in lines:
            if " = " in line:
                k, v = line.split(" = ", 1)
                d[k.strip()] = v
        return d

    arch = kv(head)
    raw_entries, cur = [], None
    for line in body:
        if " = " not in line:
            continue
        k, v = line.split(" = ", 1)
        k = k.strip()
        if k == "Path":
            if cur:
                raw_entries.append(cur)
            cur = {"Path": v}
        elif cur is not None:
            cur[k] = v
    if cur:
        raw_entries.append(cur)

    entries = []
    for d in raw_entries:
        name = d.get("Path")
        if not name:
            continue
        is_dir = d.get("Folder") == "+"
        try:
            size = int(d.get("Size") or 0)
        except ValueError:
            size = 0
        entries.append(dict(
            name=name.replace("\\", "/"),
            size=size,
            is_dir=is_dir,
            encrypted=(d.get("Encrypted") == "+"),
        ))

    try:
        physical = int(arch.get("Physical Size") or 0)
    except ValueError:
        physical = 0

    return dict(
        engine="sevenzip", path=path, type=arch.get("Type", "?"),
        entries=entries,
        total=physical,
        unpacked=sum(e["size"] for e in entries),
        encrypted=(arch.get("Encrypted") == "+" or any(e["encrypted"] for e in entries)),
        multivolume=(arch.get("Multivolume") == "+"),
        parts=1, style="single",
        nested=False,
    )


def entries_size(entries):
    return [e["size"] for e in entries]


def _list_tar(path):
    """Entries of a tar (optionally compressed) via tarfile."""
    entries = []
    try:
        with tarfile.open(path, "r:*") as tf:
            for m in tf.getmembers():
                entries.append(dict(name=m.name.replace("\\", "/"),
                                    size=m.size if m.isfile() else 0,
                                    is_dir=m.isdir(),
                                    encrypted=False))
    except tarfile.ReadError as ex:
        raise EngineError("Not a readable tar archive: %s" % ex)
    except Exception as ex:
        raise EngineError("Not a readable tar archive: %s" % ex)
    return dict(engine="tar", path=path, type="tar",
                entries=entries,
                total=0, unpacked=sum(e["size"] for e in entries),
                encrypted=False, multivolume=False, parts=1, style="single",
                nested=False)


# -------------------------------------------------------------------- probing
def probe(path, password=None):
    """Inspect any archive / split set. Returns a dict describing it."""
    if not os.path.exists(path):
        raise EngineError("File not found: %s" % path)

    split = _split_parts(path)

    # 1) zip split into raw byte chunks: our engine joins them exactly
    if split and split[2] == "split" and core is not None:
        try:
            opened = core.open_archive(path)
            try:
                entries = [dict(name=e["name"].rstrip("/"),
                                size=e["usz"],
                                is_dir=e["name"].endswith("/"),
                                encrypted=bool(e["flag"] & 1))
                           for e in opened["entries"]]
            finally:
                opened["arc"].close()
            return dict(engine="python_zip", path=path, type="zip",
                        entries=entries,
                        total=sum(os.path.getsize(p) for p in split[1]),
                        unpacked=sum(e["size"] for e in entries),
                        encrypted=any(e["encrypted"] for e in entries),
                        multivolume=True, parts=len(split[1]),
                        style="split", nested=False)
        except Exception:
            pass                                     # fall back to join + 7z

    # 2) plain tar family: tarfile does it in one pass
    if is_tar_like(path) and not split:
        try:
            return _list_tar(path)
        except EngineError:
            pass

    # 3) 7-Zip for everything else (spanned .z01 sets included)
    if find_7z():
        try:
            info = _probe_7z(path, password)
        except EngineError as ex:
            raise EngineError(str(ex))

        if split:
            info["parts"] = len(split[1])
            info["style"] = split[2]
        # .00N sets are seen by 7-Zip as a "Split" container: join, then look again
        if info["type"] == "Split" and split:
            joined = _join_parts(split[1])
            inner = _probe_7z(joined, password)
            inner["engine"] = "sevenzip_joined"
            inner["joined_path"] = joined
            inner["original"] = path
            inner["parts"] = len(split[1])
            inner["style"] = split[2]
            return inner

        # single-stream compressor wrapping a tar (e.g. .tar.zst)
        if len(info["entries"]) == 1 and info["entries"][0]["name"].lower().endswith(".tar"):
            info["nested"] = True
        return info

    # 4) no 7-Zip at all: stdlib fallbacks
    if path.lower().endswith(".zip") and not split:
        import zipfile
        try:
            with zipfile.ZipFile(path) as zf:
                entries = [dict(name=i.filename.rstrip("/"),
                                size=i.file_size,
                                is_dir=i.is_dir(),
                                encrypted=bool(i.flag_bits & 1))
                           for i in zf.infolist()]
        except Exception as ex:
            raise EngineError("Cannot read zip: %s" % ex)
        return dict(engine="python_zip_single", path=path, type="zip",
                    entries=entries, total=os.path.getsize(path),
                    unpacked=sum(e["size"] for e in entries),
                    encrypted=any(e["encrypted"] for e in entries),
                    multivolume=False, parts=1, style="single", nested=False)

    raise EngineError("Unsupported archive (install 7-Zip for this format).")


def _join_parts(parts, cache_dir=None):
    """Concatenate split parts into one temp file (only used for rare formats)."""
    base = os.path.basename(parts[0])
    stem = re.sub(r"(\.part\d+|\.\d{3})$", "", base, flags=re.IGNORECASE)
    outdir = cache_dir or os.path.join(tempfile.gettempdir(), "FidzRar")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, stem)
    if os.path.exists(out) and os.path.getsize(out) == sum(os.path.getsize(p) for p in parts):
        return out
    with open(out, "wb") as w:
        for p in parts:
            with open(p, "rb") as r:
                shutil.copyfileobj(r, w, CHUNK)
    return out


# ------------------------------------------------------------------ extracting
def extract(info, dest, password=None, mode="extract",
            on_progress=None, on_file=None, cancel=None):
    """mode: 'extract' writes files, 'test' only verifies.

    Returns (ok, fail, errors[(name, message)]).
    """
    engine = info["engine"]
    if engine == "python_zip":
        return _extract_python_split(info, dest, mode, on_progress, on_file, cancel)
    if engine == "python_zip_single":
        return _extract_zip_single(info, dest, mode, on_progress, on_file, cancel)
    if engine == "tar":
        return _extract_tar(info, dest, mode, on_progress, on_file, cancel)
    if engine in ("sevenzip", "sevenzip_joined"):
        if info.get("nested"):
            return _extract_nested(info, dest, password, mode, on_progress, on_file, cancel)
        return _extract_7z(info, dest, password, mode, on_progress, on_file, cancel)
    raise EngineError("Unknown engine: %s" % engine)


def _extract_python_split(info, dest, mode, on_progress, on_file, cancel):
    opened = core.open_archive(info["path"])
    try:
        return core.run(opened, dest, mode=mode, on_progress=on_progress,
                        on_file=on_file, cancel=cancel)
    finally:
        opened["arc"].close()


def _extract_zip_single(info, dest, mode, on_progress, on_file, cancel):
    import zipfile
    ok = fail = 0
    errors = []
    entries = info["entries"]
    total = sum(e["size"] for e in entries) or 1
    done = 0
    with zipfile.ZipFile(info["path"]) as zf:
        for e in entries:
            _cancel_check(cancel)
            name = e["name"]
            if e["is_dir"]:
                if mode == "extract":
                    os.makedirs(core.safe_join(dest, name), exist_ok=True)
                continue
            if e["encrypted"]:
                fail += 1
                errors.append((name, "encrypted (install 7-Zip for AES zips)"))
                if on_file:
                    on_file(name, e["size"], False, "encrypted")
                continue
            try:
                with zf.open(e["name"]) as src:
                    fh = None
                    if mode == "extract":
                        target = core.safe_join(dest, name)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        fh = open(target, "wb")
                    try:
                        while True:
                            _cancel_check(cancel)
                            buf = src.read(CHUNK)
                            if not buf:
                                break
                            if fh:
                                fh.write(buf)
                            done += len(buf)
                            if on_progress:
                                on_progress(min(done, total), total)
                    finally:
                        if fh:
                            fh.close()
                ok += 1
                if on_file:
                    on_file(name, e["size"], True, None)
            except Cancelled:
                raise
            except Exception as ex:
                fail += 1
                errors.append((name, str(ex)))
                if on_file:
                    on_file(name, e["size"], False, str(ex))
    if on_progress:
        on_progress(total, total)
    return ok, fail, errors


def _extract_tar(info, dest, mode, on_progress, on_file, cancel):
    ok = fail = 0
    errors = []
    total = info["unpacked"] or 1
    done = 0
    with tarfile.open(info["path"], "r:*") as tf:
        for m in tf.getmembers():
            _cancel_check(cancel)
            name = m.name.replace("\\", "/")
            if m.isdir():
                if mode == "extract":
                    os.makedirs(core.safe_join(dest, name), exist_ok=True)
                continue
            if not m.isfile():
                continue                                   # links/devices skipped
            try:
                src = tf.extractfile(m)
                if src is None:
                    raise EngineError("could not read member")
                fh = None
                if mode == "extract":
                    target = core.safe_join(dest, name)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    fh = open(target, "wb")
                try:
                    while True:
                        _cancel_check(cancel)
                        buf = src.read(CHUNK)
                        if not buf:
                            break
                        if fh:
                            fh.write(buf)
                        done += len(buf)
                        if on_progress:
                            on_progress(min(done, total), total)
                finally:
                    src.close()
                    if fh:
                        fh.close()
                ok += 1
                if on_file:
                    on_file(name, m.size, True, None)
            except Cancelled:
                raise
            except Exception as ex:
                fail += 1
                errors.append((name, str(ex)))
                if on_file:
                    on_file(name, m.size, False, str(ex))
    if on_progress:
        on_progress(total, total)
    return ok, fail, errors


def _extract_7z(info, dest, password, mode, on_progress, on_file, cancel):
    src = info.get("joined_path") or info["path"]
    sevenz = find_7z()
    if not sevenz:
        raise EngineError("7z.exe not found")

    if mode == "test":
        _run7z(["t", "-y", "-p" + (password or ""), src],
               cancel=cancel, on_progress=on_progress)
        if on_progress:
            on_progress(1000, 1000)
        return len(info["entries"]), 0, []

    os.makedirs(dest, exist_ok=True)
    before_files, before_dirs, too_big = _snapshot_tree(dest)
    seen = []

    def on_line(line):
        s = line.strip()
        if s.startswith("- ") or s.startswith("-"):
            name = s.lstrip("- ").strip()
            if name and name not in seen:
                seen.append(name)
                if on_file:
                    on_file(name, 0, True, None)

    try:
        _run7z(["x", "-y", "-bb1", "-bsp1", "-bso1", "-bse1",
                "-p" + (password or ""), "-o" + dest, src],
               cancel=cancel, on_progress=on_progress, on_line=on_line)
    except (EngineError, Cancelled):
        # 7-Zip can leave zero-byte or partial files when it fails (for
        # example on a wrong password) or when we kill it. Remove only the
        # files this run created; anything that was already there stays.
        if not too_big:
            _remove_new_tree(dest, before_files, before_dirs)
        raise

    # 7-Zip exits 0 only when everything was written; count what landed
    ok = len([e for e in info["entries"] if not e["is_dir"]])
    if on_progress:
        on_progress(1000, 1000)
    return ok, 0, []


def _extract_nested(info, dest, password, mode, on_progress, on_file, cancel):
    """Compressor (gz/bz2/xz/zstd...) wrapping a single .tar: two passes."""
    tmp = tempfile.mkdtemp(prefix="fidzrar_")
    try:
        _run7z(["x", "-y", "-p" + (password or ""), "-o" + tmp, info["path"]],
               cancel=cancel)
        inner = None
        for f in os.listdir(tmp):
            if f.lower().endswith(".tar"):
                inner = os.path.join(tmp, f)
        if not inner:
            raise EngineError("Expected a .tar inside the archive, found none.")
        sub = _list_tar(inner)
        sub["path"] = inner
        return _extract_tar(sub, dest, mode, on_progress, on_file, cancel)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- reporting
def formats_summary():
    sevenz = find_7z()
    if sevenz:
        return ("7-Zip engine: zip, 7z, rar/rar5, tar, gz, bz2, xz, zst, lzma, "
                "cab, iso, udf, wim, arj, lzh, chm, msi, rpm, deb, cpio, dmg, "
                "squashfs, split volumes")
    return ("Limited engine: zip + tar/gz/bz2/xz only "
            "(7z.exe was not found next to the app)")
