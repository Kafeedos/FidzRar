#!/usr/bin/env python3
"""Explorer right-click integration for FidzRar.

Adds an "Extract with FidzRar" verb to the context menu of the archive
types the app can open.  Everything lives under HKEY_CURRENT_USER, so no
administrator rights are needed and other Windows accounts are untouched.

The verb is registered under SystemFileAssociations, which is the
documented place for per-file-type shell verbs and does not disturb any
existing file association.

Public API
----------
    install()        -> int   number of file types registered
    uninstall()      -> int   number of file types cleaned up
    is_installed()   -> bool
    installed_count()-> int
    launch_command() -> str   the exact command the menu runs

Command line (handy for packaging tests)
----------------------------------------
    python shell_menu.py --status
    python shell_menu.py --install
    python shell_menu.py --remove
"""

import os
import sys

try:
    import winreg
except Exception:                                    # pragma: no cover
    winreg = None

VERB_KEY = "FidzRar"
VERB_LABEL = "Extract with FidzRar"
ROOT = r"Software\Classes\SystemFileAssociations"

# Keep this list in step with FILE_TYPES in FidzRar.pyw.  Split sets are
# represented by their first part, which is what people normally click.
ARCHIVE_EXTS = (
    ".zip", ".zipx", ".7z", ".rar", ".r00", ".tar", ".gz", ".tgz",
    ".bz2", ".tbz", ".tbz2", ".xz", ".txz", ".zst", ".tzst", ".lzma",
    ".cab", ".iso", ".img", ".udf", ".wim", ".swm", ".esd", ".arj",
    ".lzh", ".lha", ".chm", ".msi", ".rpm", ".deb", ".cpio", ".dmg",
    ".xar", ".z", ".apk", ".jar", ".epub", ".docx", ".xlsx", ".odt",
    ".z01", ".001", ".part1",
)


def available():
    """True when the Explorer menu can be written on this machine."""
    return winreg is not None and sys.platform == "win32"


def exe_path():
    """The program Explorer should launch."""
    return os.path.abspath(sys.executable)


def launch_command():
    """Quoted command line that starts this app (source run or frozen exe)."""
    if getattr(sys, "frozen", False):
        return '"%s"' % exe_path()

    # Development run: point at the GUI source itself, and prefer pythonw
    # so Explorer does not flash a console window.
    exe = exe_path()
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FidzRar.pyw")
    return '"%s" "%s"' % (exe, script)


def menu_command():
    """Full context-menu command: open the app, load this file, extract."""
    return '%s --extract "%%1"' % launch_command()


def _verb_path(ext, *more):
    return "\\".join((ROOT, ext, "shell", VERB_KEY) + more)


def _exists(path):
    if not available():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
            return True
    except OSError:
        return False


def install():
    """Create the verb for every known archive extension. Returns a count."""
    if not available():
        raise OSError("The right-click menu is only available on Windows.")

    command = menu_command()
    icon = '"%s",0' % exe_path()
    count = 0

    for ext in ARCHIVE_EXTS:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _verb_path(ext),
                                0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, VERB_LABEL)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ, "Single")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                _verb_path(ext, "command"),
                                0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
        count += 1

    return count


def uninstall():
    """Remove the verb again. Returns how many file types were cleaned up."""
    if not available():
        return 0

    count = 0
    for ext in ARCHIVE_EXTS:
        found = False
        for path in (_verb_path(ext, "command"), _verb_path(ext)):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                found = True
            except FileNotFoundError:
                pass
            except OSError:
                pass
        # Remove the now-empty shell / file-type keys when nothing else uses them.
        for path in (ROOT + "\\" + ext + "\\shell", ROOT + "\\" + ext):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except OSError:
                pass
        if found:
            count += 1

    return count


def installed_count():
    """How many of the known file types currently have the verb."""
    if not available():
        return 0
    return sum(1 for ext in ARCHIVE_EXTS if _exists(_verb_path(ext)))


def is_installed():
    """True if at least one file type is registered (normally the .zip one)."""
    return installed_count() > 0


def status():
    """Small dictionary used by the GUI and the CLI."""
    return dict(
        available=available(),
        installed=installed_count(),
        total=len(ARCHIVE_EXTS),
        command=menu_command(),
    )


def main(argv):
    if "--install" in argv:
        try:
            n = install()
        except Exception as ex:
            if sys.stdout is not None:
                print("ERROR: %s" % ex)
            return 1
        if sys.stdout is not None:
            print("Added the right-click menu for %d file types." % n)
        return 0

    if "--remove" in argv or "--uninstall" in argv:
        n = uninstall()
        if sys.stdout is not None:
            print("Removed the right-click menu from %d file types." % n)
        return 0

    st = status()
    text = ("Right-click menu: %s (%d/%d file types)\nCommand: %s"
            % ("installed" if st["installed"] else "not installed",
               st["installed"], st["total"], st["command"]))
    if sys.stdout is not None:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
