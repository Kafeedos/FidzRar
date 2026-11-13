#!/usr/bin/env python3
"""
extract_span_zip.py - extract ZIP archives that were split into multiple files.

Handles BOTH split styles:

  A) "Spanned" ZIP  (WinRAR / WinZip / 7-Zip "volumes")
       My Course.z01
       My Course.z02
       My Course.zip      <- last part, holds the central directory
     The EOCD says disk != 0 and every part's offsets are relative to that part.

  B) "Byte-split" ZIP  (HJSplit, 7-Zip "split file", Telegram dumps)
       My Course.zip.part1
       My Course.zip.part2
       ... or  My Course.zip.001 / .002 ...
     The EOCD says disk == 0 and offsets are relative to the whole joined file.

Windows Explorer cannot open either style, and WinRAR only understands style A
with its own .z01 naming - that is why style B fails there.

Reads across part boundaries in memory, so no ~2x disk space is wasted on a
temporary merged copy.

Usage (command line)
--------------------
    python extract_span_zip.py "C:\\path\\My Course.zip.part1"
    python extract_span_zip.py "C:\\path\\My Course.zip"
    python extract_span_zip.py "C:\\path\\My Course.z01"
    python extract_span_zip.py "My Course.zip.part1" -o "D:\\out"    (choose output)
    python extract_span_zip.py "My Course.zip.part1" -l              (list only)
    python extract_span_zip.py "My Course.zip.part1" -t              (verify only)

Pass ANY part; the other parts are found automatically.
Exit code 0 only if every file was extracted and its CRC32 matched.

Usage (as a library)
--------------------
    import extract_span_zip as core
    arc = core.open_archive(r"D:\\x.zip.part1")     # dict: arc, info, entries, ...
    try:
        ok, fail, errors = core.run(arc, dest, mode="extract")
    finally:
        arc["arc"].close()

Features: Zip64 support, stored + deflate entries, per-file CRC verification,
chunked progress callbacks, cancellation, encrypted entries reported.
"""

import os
import re
import struct
import sys
import time
import zlib

CHUNK = 1024 * 1024


class SpanError(Exception):
    """Anything wrong with the archive set (missing parts, bad offsets, ...)."""


class Cancelled(Exception):
    """Raised when the caller requests cancellation."""


# --------------------------------------------------------------- part lookup
def find_parts(any_part):
    """Return (stem, [paths in order], style) for any part of the set."""
    path = os.path.abspath(any_part)
    folder = os.path.dirname(path)
    name = os.path.basename(path)

    if not os.path.exists(path):
        raise SpanError("File not found: " + path)

    def collect(pattern_fn):
        parts, i = [], 1
        while True:
            p = os.path.join(folder, pattern_fn(i))
            if not os.path.exists(p):
                break
            parts.append(p)
            i += 1
        return parts

    # B) name.zip.part1 / .part2 ...
    m = re.match(r"^(.*)\.part(\d+)$", name, re.IGNORECASE)
    if m:
        stem = m.group(1)
        parts = collect(lambda i: "%s.part%d" % (stem, i))
        if len(parts) < 2:
            raise SpanError("Only found one .part file - is the download complete?")
        return stem, parts, "split"

    # B) name.zip.001 / .002 ...
    m = re.match(r"^(.*)\.(\d{3})$", name)
    if m:
        stem = m.group(1)
        parts = collect(lambda i: "%s.%03d" % (stem, i))
        if len(parts) < 2:
            raise SpanError("Only found one numbered part - is the download complete?")
        return stem, parts, "split"

    # A) name.z01 / .z02 ... / name.zip
    if name.lower().endswith(".zip"):
        stem = name[:-4]
    else:
        m = re.match(r"^(.*)\.z\d+$", name, re.IGNORECASE)
        if not m:
            raise SpanError("Unrecognised part name: " + name)
        stem = m.group(1)

    parts = collect(lambda i: "%s.z%02d" % (stem, i))
    last = os.path.join(folder, stem + ".zip")
    if not os.path.exists(last):
        raise SpanError("Missing final part: " + last)
    parts.append(last)
    if len(parts) < 2:
        raise SpanError("Not split - this is a normal .zip. Use Explorer or 7-Zip.")
    return stem, parts, "spanned"


def default_dest(any_part, stem):
    """Sensible output folder: next to the archive, named after it."""
    folder = os.path.dirname(os.path.abspath(any_part))
    base = stem[:-4] if stem.lower().endswith(".zip") else stem
    return os.path.join(folder, base)


# ------------------------------------------------------------ virtual reader
class Parts:
    """Presents all parts as one continuous, seekable file."""

    def __init__(self, paths):
        self.paths = paths
        self.sizes = [os.path.getsize(p) for p in paths]
        self.files = [open(p, "rb") for p in paths]
        self.starts = []
        run = 0
        for s in self.sizes:
            self.starts.append(run)
            run += s
        self.total = run

    def read(self, off, n):
        out, got, d = [], 0, 0
        if off >= self.total:
            return b""
        while d < len(self.sizes) - 1 and off >= self.starts[d] + self.sizes[d]:
            d += 1
        o = off - self.starts[d]
        while got < n and d < len(self.files):
            avail = self.sizes[d] - o
            if avail <= 0:
                d, o = d + 1, 0
                continue
            take = min(n - got, avail)
            self.files[d].seek(o)
            buf = self.files[d].read(take)
            if not buf:
                break
            out.append(buf)
            got += len(buf)
            d, o = d + 1, 0
        return b"".join(out)

    def close(self):
        for f in self.files:
            f.close()


# ------------------------------------------------------------------- metadata
def read_eocd(arc, style):
    """Return dict(cd_off, cd_disk, cd_size, count, spanned); cd_off is absolute."""
    tail_len = min(arc.total, 66000)
    tail = arc.read(arc.total - tail_len, tail_len)
    idx = tail.rfind(b"PK\x05\x06")
    if idx < 0:
        raise SpanError("No end-of-central-directory record. Not a ZIP "
                        "(or a part is missing).")

    (sig, disk, cd_disk, this_disk, total, cd_size, cd_off,
     clen) = struct.unpack("<IHHHHIIH", tail[idx:idx + 22])

    zip64 = (cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or total == 0xFFFF)
    if zip64:
        loc = tail.rfind(b"PK\x06\x07", 0, idx)
        if loc < 0:
            raise SpanError("Zip64 locator missing.")
        z64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        rec = arc.read(z64_off, 56)
        if rec[:4] != b"PK\x06\x06":
            # some writers store it relative to the last part
            rec = arc.read(arc.starts[-1] + z64_off, 56)
        if rec[:4] != b"PK\x06\x06":
            raise SpanError("Zip64 end record not found.")
        total, cd_size, cd_off = struct.unpack("<QQL", rec[32:56])
        disk = cd_disk = 0
    elif style == "split":
        disk = cd_disk = 0

    cd_abs = arc.starts[cd_disk] + cd_off if cd_disk < len(arc.starts) else cd_off
    return dict(cd_off=cd_abs, cd_disk=cd_disk, cd_size=cd_size,
                count=total,
                spanned=(disk != 0 or cd_disk != 0 or style == "spanned"))


def parse_central_directory(arc, info):
    blob = arc.read(info["cd_off"], info["cd_size"])
    if blob[:4] != b"PK\x01\x02":
        raise SpanError("Central directory not found where expected "
                        "(a part may be missing or corrupt).")
    entries, i = [], 0
    while blob[i:i + 4] == b"PK\x01\x02":
        (sig, verm, verneed, flag, meth, tm, dt, crc, csz, usz,
         nlen, elen, clen, dstart, iattr, eattr, off) = struct.unpack(
            "<IHHHHHHIIIHHHHHII", blob[i:i + 46])
        raw_name = blob[i + 46:i + 46 + nlen]
        extra = blob[i + 46 + nlen:i + 46 + nlen + elen]

        if 0xFFFFFFFF in (csz, usz, off) or dstart == 0xFFFF:
            j = 0
            while j + 4 <= len(extra):
                hid, hlen = struct.unpack("<HH", extra[j:j + 4])
                if hid == 0x0001:
                    vals, k = extra[j + 4:j + 4 + hlen], 0
                    if usz == 0xFFFFFFFF:
                        usz = struct.unpack("<Q", vals[k:k + 8])[0]; k += 8
                    if csz == 0xFFFFFFFF:
                        csz = struct.unpack("<Q", vals[k:k + 8])[0]; k += 8
                    if off == 0xFFFFFFFF:
                        off = struct.unpack("<Q", vals[k:k + 8])[0]; k += 8
                    if dstart == 0xFFFF:
                        dstart = struct.unpack("<I", vals[k:k + 4])[0]
                    break
                j += 4 + hlen

        entries.append(dict(
            name=raw_name.decode("utf-8" if flag & 0x800 else "cp437", "replace"),
            flag=flag, meth=meth, crc=crc, csz=csz, usz=usz,
            disk=dstart, off=off))
        i += 46 + nlen + elen + clen
    return entries


def open_archive(any_part):
    """Open a split set. Caller must call result['arc'].close() when done."""
    stem, paths, style = find_parts(any_part)
    arc = Parts(paths)
    try:
        info = read_eocd(arc, style)
        entries = parse_central_directory(arc, info)
    except Exception:
        arc.close()
        raise
    return dict(stem=stem, paths=paths, style=style, arc=arc,
                info=info, entries=entries, part=any_part)


def safe_join(dest, name):
    p = os.path.normpath(os.path.join(dest, name))
    root = os.path.normpath(dest)
    if p != root and not p.startswith(root + os.sep):
        raise SpanError("Refusing unsafe path inside archive: " + name)
    return p


def data_offset(arc, e, spanned):
    """Absolute offset of the entry's compressed bytes."""
    base = arc.starts[e["disk"]] if spanned else 0
    for delta in (0, 4, -4):
        off = base + e["off"] + delta
        if off < 0:
            continue
        lh = arc.read(off, 30)
        if lh[:4] == b"PK\x03\x04":
            lnlen, lelen = struct.unpack("<HH", lh[26:30])
            return off + 30 + lnlen + lelen
    raise SpanError("Bad local header for entry: " + e["name"])


# ------------------------------------------------------------------ extraction
def run(opened, dest, mode="extract", on_progress=None, on_file=None, cancel=None):
    """Extract (or verify) everything.

    mode        'extract' writes files, 'test' only verifies CRCs.
    on_progress callable(done_bytes, total_bytes)
    on_file     callable(name, size_bytes, ok, error_or_None)
    cancel      threading.Event-like with .is_set()

    Returns (ok_count, fail_count, errors list of (name, message)).
    """
    arc = opened["arc"]
    entries = opened["entries"]
    spanned = opened["info"]["spanned"]
    total = sum(e["usz"] for e in entries) or 1
    done = 0
    ok = fail = 0
    errors = []

    if mode == "extract":
        os.makedirs(dest, exist_ok=True)

    for e in entries:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

        name = e["name"]
        if name.endswith("/"):
            if mode == "extract":
                os.makedirs(safe_join(dest, name), exist_ok=True)
            continue

        if e["flag"] & 1:
            errors.append((name, "encrypted - password not supported"))
            fail += 1
            if on_file:
                on_file(name, e["usz"], False, "encrypted")
            continue

        target = safe_join(dest, name) if mode == "extract" else None
        tmp = None
        fh = None
        entry_base = done
        try:
            if mode == "extract":
                os.makedirs(os.path.dirname(target), exist_ok=True)
                tmp = target + ".part_extract"
                fh = open(tmp, "wb")

            pos = data_offset(arc, e, spanned)
            remaining = e["csz"]
            crc = 0
            size = 0
            inflater = zlib.decompressobj(-15) if e["meth"] == 8 else None
            if e["meth"] not in (0, 8):
                raise SpanError("unsupported compression method %d" % e["meth"])

            while remaining > 0:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                n = min(CHUNK, remaining)
                chunk = arc.read(pos, n)
                if not chunk:
                    raise SpanError("unexpected end of data (missing part?)")
                pos += len(chunk)
                remaining -= len(chunk)
                data = inflater.decompress(chunk) if inflater else chunk
                if data:
                    crc = zlib.crc32(data, crc)
                    size += len(data)
                    if fh:
                        fh.write(data)
                done += len(chunk)
                if on_progress:
                    on_progress(min(done, total), total)

            if inflater:
                tail = inflater.flush()
                if tail:
                    crc = zlib.crc32(tail, crc)
                    size += len(tail)
                    if fh:
                        fh.write(tail)

            if size != e["usz"]:
                raise SpanError("size %d != expected %d" % (size, e["usz"]))
            if crc & 0xFFFFFFFF != e["crc"]:
                raise SpanError("CRC mismatch - a part is probably corrupt")

            if fh:
                fh.close()
                fh = None
                os.replace(tmp, target)
                tmp = None
            ok += 1
            if on_file:
                on_file(name, e["usz"], True, None)

        except Cancelled:
            if fh:
                fh.close()
                fh = None
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        except Exception as ex:
            if fh:
                fh.close()
                fh = None
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            fail += 1
            errors.append((name, str(ex)))
            if on_file:
                on_file(name, e["usz"], False, str(ex))
            done = entry_base + e["usz"]

    if on_progress:
        on_progress(total, total)
    return ok, fail, errors


# ------------------------------------------------------------------------ CLI
def main(argv):
    mode, out, part, args, i = "extract", None, None, argv[1:], 0
    while i < len(args):
        a = args[i]
        if a in ("-l", "--list"):
            mode = "list"
        elif a in ("-t", "--test"):
            mode = "test"
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        elif a in ("-o", "--out"):
            i += 1
            out = args[i]
        else:
            part = a
        i += 1

    if not part:
        print(__doc__)
        return 2

    try:
        opened = open_archive(part)
    except SpanError as ex:
        print("ERROR: %s" % ex)
        return 1

    arc = opened["arc"]
    try:
        entries = opened["entries"]
        style = opened["style"]
        total_bytes = sum(arc.sizes)
        print("Archive : %s" % opened["stem"])
        print("Style   : %s (%d parts, %.2f GB)" % (
            "spanned .z01+.zip" if style == "spanned" else "byte-split .partN",
            len(arc.paths), total_bytes / 1024 ** 3))
        print("Entries : %d   Uncompressed: %.2f GB" % (
            len(entries), sum(e["usz"] for e in entries) / 1024 ** 3))

        if mode == "list":
            for e in entries:
                mark = "D" if e["name"].endswith("/") else ("E" if e["flag"] & 1 else " ")
                print("%s %10.2f MB  %s" % (mark, e["usz"] / 1024 ** 2, e["name"]))
            return 0

        dest = os.path.abspath(out) if out else default_dest(part, opened["stem"])
        print("Output  : %s\n" % dest)

        t0 = time.time()
        last = [0.0]

        def on_file(name, size, good, err):
            if good:
                print("  ok  %-78s %8.1f MB" % (name.split("/")[-1][:76], size / 1024 ** 2))

        def on_progress(done, total):
            now = time.time()
            if now - last[0] > 0.25:
                last[0] = now
                sys.stdout.write("\r  ... %5.1f%%  (%d/%d MB)" % (
                    done * 100.0 / total, done // 1024 ** 2, total // 1024 ** 2))
                sys.stdout.flush()

        ok, fail, errors = run(opened, dest, mode=mode,
                               on_progress=on_progress, on_file=on_file)
        sys.stdout.write("\r" + " " * 60 + "\r")

        print("%s in %.1fs - %d ok, %d failed" % (
            "Extracted" if mode == "extract" else "Verified",
            time.time() - t0, ok, fail))
        if errors:
            print("\nERRORS:")
            for n, m in errors:
                print("  %s -> %s" % (n, m))
            return 1
        print("All CRCs verified OK.")
        return 0
    finally:
        arc.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
