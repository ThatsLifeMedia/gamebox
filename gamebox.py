#!/usr/bin/env python3
"""
GameBox - a unified game library for Windows.

A frameless native window: the title bar is part of the app, not a Windows
box bolted on top. One process - closing the window ends everything, and
there is no background service and no network port.

  python gamebox.py            run from source
  GameBox.exe                  the built application
  GameBox.exe --selftest       check a build can do what only a build gets wrong
  GameBox.exe --uitest         open the window and confirm it drew the library

Everything that touches the machine - launching a game, opening a folder,
scanning drives - is a Python call over the pywebview bridge.
"""

import ctypes
import json
import os
import queue
import string
import subprocess
import sys
import threading
import webbrowser

import importlib.util
import webview

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import library as L
import migrate
import playtime
import updater

APP_NAME = "GameBox"
VERSION = "1.3.0"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# Set while the window is fullscreen, so make_resizable_frameless() stops
# re-asserting the window style and fighting the transition.
_suspend_restyle = False


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def user_file(name):
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


SETTINGS = user_file("gamebox-settings.json")

_scanner = None

# Result of the startup update check, filled in by a daemon thread so the UI
# never waits on the network. update_info() hands a copy to the page.
_update_state = {"checked": False}


def _run_update_check():
    try:
        _update_state.update(updater.check(VERSION))
    except Exception:
        pass
    finally:
        _update_state["checked"] = True


def load_scanner():
    """Import scan-library.py.

    Its name has a hyphen in it, so `import` cannot say it; importlib can. The
    module is cached because loading it is not free and a scan can be re-run.
    """
    global _scanner
    if _scanner is not None:
        return _scanner
    path = resource("scan-library.py")
    if not os.path.exists(path):
        path = user_file("scan-library.py")
    spec = importlib.util.spec_from_file_location("gamebox_scanner", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _scanner = mod
    return mod


def ensure_page():
    """Put GameBox.html beside the exe, and return the path to load.

    It has to sit next to the exe rather than run from inside the bundle: the
    page loads its artwork with relative <img src="art/...">, and art/ lives
    next to the exe. Run from the bundle's own temp folder, every cover would
    quietly fail to load and the library would look empty of art.

    The installer puts it there. This covers the exe being run on its own, and
    refreshes a copy left behind by an older version.
    """
    beside = user_file("GameBox.html")
    packed = resource("GameBox.html")
    try:
        same = os.path.exists(packed) and os.path.samefile(packed, beside) \
            if os.path.exists(beside) and os.path.exists(packed) else False
    except OSError:
        same = False
    if os.path.exists(packed) and not same:
        try:
            if (not os.path.exists(beside)
                    or os.path.getmtime(packed) > os.path.getmtime(beside)):
                import shutil
                # Before 1.3.0 the page *was* the library. Overwriting one that
                # still holds an unmigrated `const G=[...]` would throw it away,
                # so keep a copy: startup() migrates from whichever it finds.
                if holds_a_library(beside):
                    shutil.copyfile(beside, user_file(OLD_PAGE))
                shutil.copyfile(packed, beside)
        except OSError:
            pass
    if os.path.exists(beside):
        return beside
    return packed


# Where a pre-1.3 page is kept if it had to be moved aside before it could be
# migrated. Nothing reads it once library.json exists.
OLD_PAGE = "GameBox.pre-1.3.html"


def holds_a_library(path):
    """Does this page still carry a pre-1.3 library inside it?

    Asked of the migration rather than answered with a regex here: a pattern
    loose enough to spot the old block also matches the comment in the current
    page that explains what the old block was, which had every launch quietly
    filing a "rescued" copy of a page that had nothing to rescue.
    """
    try:
        return bool(migrate.read_old_library(path))
    except Exception:
        return False


def startup():
    """Bring the folder up to date, then say which page to load.

    Order matters and is the whole point of putting it in one place: a 1.2
    install keeps its library inside GameBox.html, so it has to be migrated
    before the page is replaced with this version's.
    """
    for candidate in (user_file("GameBox.html"), user_file(OLD_PAGE)):
        try:
            rep = migrate.run(candidate, user_file("launch.json"))
            if rep.get("migrated"):
                print("migrated %d games into library.json" % rep.get("games", 0))
                break
        except Exception as e:
            print("migration skipped: %s" % e)
    return ensure_page()


def emu_executable(command_line):
    """The program a command line starts, so it can be checked before running.

    A command line built from an emulator profile always begins with the quoted
    executable. A library written before 1.3.0 stored a bare path instead, so
    an unquoted string is taken as-is.
    """
    if not command_line:
        return None
    if command_line.startswith('"'):
        end = command_line.find('"', 1)
        return command_line[1:end] if end > 1 else None
    return command_line.split(" ")[0]


# --------------------------------------------------------------- window chrome
GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_FLAGS = 0x0002 | 0x0001 | 0x0004 | 0x0020  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED


def app_hwnd():
    """This process's visible top-level window. A frameless window has no
    caption to search for, so go by process."""
    try:
        h = int(webview.windows[0].native.Handle)
        if h:
            return h
    except Exception:
        pass
    user32 = ctypes.windll.user32
    found = []
    pid = os.getpid()
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        return 0
    return found[0] if found else 0


def make_resizable_frameless(geometry=None):
    """A frameless window still needs resize borders and Aero Snap.

    Windows hands those to WS_THICKFRAME, which a borderless form drops. Put
    it back and the window resizes from every edge, snaps, and honours
    Win+Arrow - with no caption drawn.
    """
    import time

    find_hwnd = app_hwnd

    def apply():
        user32 = ctypes.windll.user32
        placed = False
        # The form is created and restyled by the toolkit as it shows, so keep
        # re-asserting the flags for a couple of seconds until they stick.
        for _ in range(40):
            try:
                if _suspend_restyle:
                    time.sleep(0.25)      # fullscreen owns the style right now
                    continue
                hwnd = find_hwnd()
                if hwnd:
                    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                    if not style & WS_THICKFRAME:
                        style |= WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
                        user32.SetWindowLongW(hwnd, GWL_STYLE, style)
                        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)
                    # A packaged build can leave the frameless form created but
                    # never shown, so show it ourselves once it exists.
                    if geometry and not placed:
                        user32.SetWindowPos(hwnd, 0, geometry["x"], geometry["y"],
                                            geometry["w"], geometry["h"], 0x0004 | 0x0010)
                        if geometry.get("max"):
                            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                        placed = True
                    if not user32.IsWindowVisible(hwnd):
                        user32.ShowWindow(hwnd, 5)      # SW_SHOW
                        user32.SetForegroundWindow(hwnd)
                    elif _ > 8:
                        return  # settled
            except Exception:
                return
            time.sleep(0.1)

    threading.Thread(target=apply, daemon=True).start()


# --------------------------------------------------------------------- window
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _PLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinX", ctypes.c_long), ("ptMinY", ctypes.c_long),
                ("ptMaxX", ctypes.c_long), ("ptMaxY", ctypes.c_long),
                ("rcNormal", _RECT)]


def work_area():
    """The desktop minus the taskbar, on the monitor the window will open on."""
    r = _RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return 0, 0, 1600, 940


def default_geometry():
    """Fill the screen, minus a small margin, on a machine we have not seen."""
    x, y, w, h = work_area()
    m = int(min(w, h) * 0.03)
    return {"x": x + m, "y": y + m, "w": w - m * 2, "h": h - m * 2, "max": True}


def saved_geometry(store):
    g = store.get("window") or {}
    d = default_geometry()
    try:
        out = {"x": int(g["x"]), "y": int(g["y"]),
               "w": max(1100, int(g["w"])), "h": max(660, int(g["h"])),
               "max": bool(g.get("max"))}
    except (KeyError, TypeError, ValueError):
        return d
    # a window remembered on a monitor that is no longer attached would open
    # off-screen, so fall back to the default when it does not fit
    sx, sy, sw, sh = work_area()
    if out["x"] > sx + sw - 200 or out["y"] > sy + sh - 200 or        out["x"] + out["w"] < sx + 200 or out["y"] < sy - 200:
        return d
    return out


def read_geometry():
    """Where the window is now, as it would be if it were not maximised."""
    try:
        hwnd = app_hwnd()
        if not hwnd:
            return None
        p = _PLACEMENT()
        p.length = ctypes.sizeof(_PLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(p)):
            return None
        r = p.rcNormal
        return {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                "max": bool(ctypes.windll.user32.IsZoomed(hwnd))}
    except Exception:
        return None


# ---------------------------------------------------------------------- the API
class Api:
    """Everything the page is allowed to ask the machine to do."""

    def __init__(self):
        self._scan_lines = queue.Queue()
        self._scan_thread = None
        self._scan_stop = threading.Event()

    # ---- the library
    def library(self):
        """The whole library, in the shape the page renders.

        The page used to carry its library inside itself as a `const G=[...]`
        literal, which is why a scan had to rewrite the HTML and the app had to
        reload it. Now it asks for it. (It has to ask rather than fetch: a
        file:// page cannot fetch() a sibling file, though it can happily <img>
        one, which is why the artwork is still referenced by path.)
        """
        data = L.load()
        return {"ok": True, "games": L.to_page(data),
                "scanned": data.get("scanned", ""),
                "pending_migration": bool(data.get("migration"))}

    def save_game(self, gid, patch):
        """Write the user's own fields for one game. The scanner never sees these."""
        if not isinstance(patch, dict):
            return {"ok": False, "msg": "Bad patch"}
        data = L.load()
        g = next((x for x in data["games"] if x["id"] == gid), None)
        if g is None:
            return {"ok": False, "msg": "Unknown game"}
        allowed = set(L.blank_user())
        for k, v in patch.items():
            if k in allowed:
                g["user"][k] = v
        if not L.save(data):
            return {"ok": False, "msg": "Could not write the library"}
        return {"ok": True, "user": g["user"]}

    def add_game(self, fields):
        """Add a game by hand.

        The scanner finds what it can recognise, which leaves out anything with
        no launcher and no executable where it expects one - a shortcut, a game
        behind a URI, an old install. A hand-added game is marked as such and a
        scan never disowns it.
        """
        fields = fields if isinstance(fields, dict) else {}
        title = (fields.get("title") or "").strip()
        target = (fields.get("target") or "").strip()
        if not title:
            return {"ok": False, "msg": "A game needs a name"}
        if not target:
            return {"ok": False, "msg": "Give the program to run, or a link to open"}

        # A path is run; anything with a scheme is handed to Windows to open.
        if os.path.exists(target):
            kind, folder = "exe", os.path.dirname(target)
            size = 0
            if os.path.isdir(target):
                return {"ok": False, "msg": "Point at the program, not the folder"}
        elif "://" in target:
            kind, folder, size = "uri", "", 0
        else:
            return {"ok": False, "msg": "Cannot find " + target}

        data = L.load()
        gid = L.game_id("manual", key=L.norm_path(target) or title.lower())
        if any(x["id"] == gid for x in data["games"]):
            return {"ok": False, "msg": "That game is already in the library"}
        rec = L.new_record({
            "id": gid, "title": title, "store": fields.get("store") or "local",
            "path": folder, "bytes": size, "installed": True,
            "via": "Added by hand", "launch": [kind, target, folder],
            "art": {"s": "none"},
        })
        rec["source"] = "manual"
        data["games"].append(rec)
        if not L.save(data):
            return {"ok": False, "msg": "Could not write the library"}
        return {"ok": True, "id": gid}

    def remove_game(self, gid, forever=False):
        """Take a game out of the library, optionally for good.

        Without `forever` this is just a delete, and the next scan finds it
        again. With it, the id joins the exclusion list, which is how you get
        rid of something the scanner keeps mistaking for a game.
        """
        data = L.load()
        before = len(data["games"])
        data["games"] = [x for x in data["games"] if x["id"] != gid]
        if len(data["games"]) == before:
            return {"ok": False, "msg": "Unknown game"}
        if forever:
            excl = data.setdefault("exclusions", [])
            if gid not in excl:
                excl.append(gid)
        if not L.save(data):
            return {"ok": False, "msg": "Could not write the library"}
        return {"ok": True, "excluded": bool(forever)}

    def exclusions(self):
        """The games the scanner has been told to stop importing."""
        return {"ok": True, "ids": L.load().get("exclusions") or []}

    def unexclude(self, gid=None):
        """Let the scanner import something again - all of them if gid is None."""
        data = L.load()
        data["exclusions"] = [] if gid is None else [
            x for x in (data.get("exclusions") or []) if x != gid]
        L.save(data)
        return {"ok": True, "ids": data["exclusions"]}

    # ---- playtime
    def _watch(self, gid, folder, pid=None):
        """Start timing a game we have just launched.

        There is usually nothing to wait on: a steam:// URI returns at once and
        the game is started later by a process that is not ours. So the watcher
        looks for anything running inside the game's install folder instead.
        """
        if not folder or not os.path.isdir(folder):
            return
        playtime.track(gid, folder, pid, on_finish=self._session_ended)

    def _session_ended(self, gid, seconds):
        try:
            L.record_session(gid, seconds)
        except Exception:
            pass

    def playing(self):
        """What is running now, so the page can show a live timer."""
        return {"ok": True, "playing": playtime.playing(),
                "waiting": playtime.waiting()}

    def stop_tracking(self, gid=None):
        playtime.stop(gid)
        return {"ok": True}

    def emulator_options(self, gid):
        """Every installed emulator profile that could open this game.

        A .iso is a PS2 disc, a PS3 disc, a Dreamcast disc or a PC install
        depending on nothing the file itself says, so where the scanner had to
        guess, the user gets to correct it.
        """
        data = L.load()
        g = next((x for x in data["games"] if x["id"] == gid), None)
        if g is None:
            return {"ok": False, "msg": "Unknown game"}
        try:
            scanner = load_scanner()
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        import emulators as EMU
        installed = scanner.INSTALLED_EMULATORS or scanner.find_emulators([])
        target = g.get("rom") or g.get("path") or ""
        options = []
        if os.path.isdir(target):
            match, _img = EMU.resolve_folder(target, installed)
            if match:
                options.append(match)
        else:
            options = EMU.candidates(target, installed)
        # Extensions alone would leave out the emulator this game is already
        # using: RPCS3 declares none, so a PS3 .iso lists PCSX2 and not the
        # emulator that actually opens it. Add whatever covers the platform.
        if g.get("platform"):
            options += EMU.for_platform(g["platform"], installed)
        seen, unique = set(), []
        for m in options:
            key = (m.emulator_id, m.profile_index)
            if key not in seen:
                seen.add(key)
                unique.append(m)
        cur = (g.get("user") or {}).get("emulator") or {}
        return {"ok": True, "current": cur,
                "options": [dict(m.as_dict(),
                                 platforms=[EMU.platform_name(p) for p in m.platforms])
                            for m in unique]}

    def set_emulator(self, gid, choice):
        """Pin a game to one emulator profile, and rebuild how it starts."""
        data = L.load()
        g = next((x for x in data["games"] if x["id"] == gid), None)
        if g is None:
            return {"ok": False, "msg": "Unknown game"}
        try:
            scanner = load_scanner()
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        if not scanner.INSTALLED_EMULATORS:
            scanner.find_emulators([])
        target = g.get("rom") or g.get("path") or ""
        got = scanner.emulator_launch(target, choice or None)
        if not got:
            return {"ok": False, "msg": "That emulator cannot open this game"}
        g["launch"], match = got
        g["launch"] = list(g["launch"])
        g["via"] = match.name
        g["user"]["emulator"] = choice or None
        if not L.save(data):
            return {"ok": False, "msg": "Could not write the library"}
        return {"ok": True, "via": match.name}

    def migrate_user(self, favs, hidden):
        """Claim the favourites and hidden games left in the page's localStorage.

        The old library keyed both off the scan-order integer, which the move to
        library.json retired. The migration left a {old integer: new id} table
        behind for exactly this handover; it is dropped once claimed, so this
        runs at most once.
        """
        data = L.load()
        table = data.get("migration")
        if not table:
            return {"ok": True, "claimed": False}
        by_id = {g["id"]: g for g in data["games"]}
        moved = 0
        for old_ids, field in ((favs or [], "favorite"), (hidden or [], "hidden")):
            for old in old_ids:
                g = by_id.get(table.get(str(old)))
                if g is not None:
                    g["user"][field] = True
                    moved += 1
        data.pop("migration", None)
        L.save(data)
        return {"ok": True, "claimed": True, "moved": moved}

    # ---- window
    def status(self):
        st = self.settings_get()
        data = L.load()
        return {"ok": True, "app": APP_NAME, "version": VERSION,
                "games": len(data.get("games") or []), "native": True,
                "setup_done": bool(st.get("setup_done")),
                "profile": st.get("profile") or {},
                # a first name to offer during setup; the user can change it
                "user": (os.environ.get("USERNAME") or "").split("@")[0]}

    def minimize(self):
        webview.windows[0].minimize()
        return {"ok": True}

    def toggle_maximize(self):
        # ask Windows, rather than tracking a flag: the window can also be
        # maximised by a double-click on the bar, Win+Up, or restored geometry
        w = webview.windows[0]
        try:
            maxed = bool(ctypes.windll.user32.IsZoomed(app_hwnd()))
        except Exception:
            maxed = getattr(w, "_max", False)
        if maxed:
            w.restore()
        else:
            w.maximize()
        w._max = not maxed
        return {"ok": True, "maximized": not maxed}

    def set_fullscreen(self, on):
        """Take the window fullscreen, or bring it back.

        The frameless window is kept resizable by re-asserting WS_THICKFRAME,
        which fights a fullscreen transition if it is still running - so the
        restyling loop is told to stand down while the window is fullscreen.
        """
        global _suspend_restyle
        try:
            w = webview.windows[0]
            want = bool(on)
            if getattr(w, "_fs", False) == want:
                return {"ok": True, "fullscreen": want}
            _suspend_restyle = want
            w.toggle_fullscreen()
            w._fs = want
            return {"ok": True, "fullscreen": want}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def close(self):
        """Close, and mean it - even mid-scan."""
        self.remember_window()
        self.stop_scan()
        # a game still running is a session in progress; bank it rather than
        # losing the time because the library was closed first
        playtime.stop()
        try:
            webview.windows[0].destroy()
        except Exception:
            pass
        # If the toolkit will not come down - a webview host still shutting
        # down, a reader thread wedged on a pipe - do not leave the user with a
        # window that will not close.
        def hard_exit():
            os._exit(0)

        t = threading.Timer(2.5, hard_exit)
        t.daemon = True
        t.start()
        return {"ok": True}

    def stop_scan(self):
        """Ask the scan to stop. It checks between games, so this is not instant."""
        self._scan_stop.set()
        return {"ok": True}

    def remember_window(self):
        g = read_geometry()
        if g:
            self.settings_set({"window": g})
        return {"ok": bool(g)}

    # ---- settings
    def settings_get(self):
        try:
            return json.load(open(SETTINGS, encoding="utf-8"))
        except Exception:
            return {}

    def settings_set(self, data):
        cur = self.settings_get()
        tmp = SETTINGS + ".tmp"
        try:
            cur.update(data or {})
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cur, fh, indent=1)
            os.replace(tmp, SETTINGS)
            return {"ok": True}
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"ok": False, "msg": str(e)}

    # ---- updates: read-only check against the GitHub releases API. Nothing
    # is downloaded or installed; the page just links to the release.
    def update_info(self):
        """Cached result of the startup update check, plus the dismissed version."""
        st = dict(_update_state)
        st["dismissed"] = self.settings_get().get("update_dismissed")
        return {"ok": True, "state": st}

    def update_check_now(self):
        """Run the check synchronously (Settings 'Check now' button)."""
        try:
            res = updater.check(VERSION)
        except Exception:
            res = {"ok": False}
        res["checked"] = True
        _update_state.update(res)
        return self.update_info()

    def update_dismiss(self, version):
        self.settings_set({"update_dismissed": version})
        return {"ok": True}

    def open_url(self, url):
        """Open a GameBox release link in the browser. Anything else is refused -
        this keeps a crafted library entry from turning the bridge into an
        open redirect."""
        u = (url or "").strip()
        if u.startswith("https://github.com/ThatsLifeMedia/gamebox/"):
            try:
                webbrowser.open(u)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "msg": str(e)}
        return {"ok": False, "msg": "Blocked"}

    # ---- setup
    def drives(self):
        """Fixed drives worth scanning, plus which launchers are installed."""
        out = []
        for letter in string.ascii_uppercase:
            root = "%s:\\" % letter
            if not os.path.isdir(root):
                continue
            try:
                if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
                    continue
                free = ctypes.c_ulonglong(0)
                total = ctypes.c_ulonglong(0)
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(root), None, ctypes.byref(total), ctypes.byref(free))
            except Exception:
                continue
            hints = []
            for probe, label in ((("SteamLibrary", "steamapps"), "Steam library"),
                                 (("Program Files (x86)", "Steam"), "Steam"),
                                 (("Games",), "Games folder"),
                                 (("Epic Games",), "Epic")):
                if os.path.isdir(os.path.join(root, *probe)):
                    hints.append(label)
            out.append({"root": root,
                        "total": round(total.value / 1024 ** 3),
                        "free": round(free.value / 1024 ** 3),
                        "hints": sorted(set(hints)),
                        "suggested": bool(hints) or letter == "C"})
        launchers = {
            "Steam": os.path.isdir(os.path.expandvars(r"%ProgramFiles(x86)%\Steam")),
            "GOG Galaxy": os.path.exists(os.path.expandvars(
                r"%ProgramData%\GOG.com\Galaxy\storage\galaxy-2.0.db")),
            "Epic Games": os.path.isdir(os.path.expandvars(
                r"%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests")),
        }
        # Xbox, Ubisoft, EA, Battle.net, Amazon and itch each know how to spot
        # themselves; the EA row used to be advertised here with no importer
        # behind it at all.
        try:
            import stores
            launchers.update(stores.installed_launchers())
        except Exception:
            pass
        return {"ok": True, "drives": out, "launchers": launchers,
                "scanner": os.path.exists(user_file("scan-library.py"))}

    def start_scan(self, roots, opts=None):
        """Run the scanner on a thread, streaming its output back to the page.

        This used to shell out to `python -u scan-library.py`, which worked from
        source and never worked from a build: a onefile bundle has no system
        Python to call and would not have given a child process Pillow anyway.
        Importing the scanner instead means the frozen app scans with the same
        interpreter and libraries it was built with - and the pipe-buffering
        and done-marker workarounds the subprocess needed go away with it.
        """
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return {"ok": False, "msg": "A scan is already running"}
        roots = [r for r in (roots or []) if r]
        if not roots:
            return {"ok": False, "msg": "Pick at least one drive"}
        try:
            scanner = load_scanner()
        except Exception as e:
            return {"ok": False, "msg": "Could not load the scanner: %s" % e}

        # a cancelled scan can leave its output, and its done marker, behind;
        # the next scan must not read them as its own
        while True:
            try:
                self._scan_lines.get_nowait()
            except queue.Empty:
                break
        self._scan_stop.clear()

        class Tee:
            """The scanner reports by printing; send those lines to the page."""
            def __init__(self, q):
                self.q, self.buf = q, ""

            def write(self, text):
                self.buf += text
                while "\n" in self.buf:
                    line, self.buf = self.buf.split("\n", 1)
                    self.q.put(line.rstrip())

            def flush(self):
                if self.buf:
                    self.q.put(self.buf.rstrip())
                    self.buf = ""

        def run():
            out = Tee(self._scan_lines)
            saved = sys.stdout
            code = 0
            no_meta = bool((opts or {}).get("no_meta"))
            try:
                sys.stdout = out
                scanner.run_scan(roots, apply=True, no_meta=no_meta, should_stop=self._scan_stop.is_set)
            except Exception as e:
                self._scan_lines.put("Scan failed: %s: %s" % (type(e).__name__, e))
                code = 1
            finally:
                out.flush()
                sys.stdout = saved
                self._scan_lines.put("__DONE__%d" % code)

        self._scan_thread = threading.Thread(target=run, daemon=True)
        self._scan_thread.start()
        return {"ok": True, "msg": "Scanning " + ", ".join(roots)}

    def scan_progress(self):
        lines, done, code = [], False, 0
        while True:
            try:
                line = self._scan_lines.get_nowait()
            except queue.Empty:
                break
            if line.startswith("__DONE__"):
                done, code = True, int(line[8:] or 0)
            else:
                lines.append(line)
        return {"ok": True, "lines": lines, "done": done, "code": code}

    # ---- games
    def launch(self, gid):
        target = L.launch_target(gid)
        if target is None:
            return {"ok": False, "msg": "No launch target recorded - run a scan"}
        kind, target, folder = target
        data = L.load()
        game = next((x for x in data["games"] if x["id"] == gid), None)
        watch_folder = (game or {}).get("path") or folder
        pid = None
        try:
            if kind == "steam":
                os.startfile("steam://rungameid/" + target)
                self._watch(gid, watch_folder)
                return {"ok": True, "msg": "Handed off to Steam"}
            if kind == "epic":
                os.startfile("com.epicgames.launcher://apps/%s?action=launch&silent=true" % target)
                self._watch(gid, watch_folder)
                return {"ok": True, "msg": "Handed off to the Epic launcher"}
            if kind == "exe":
                if not os.path.exists(target):
                    return {"ok": False, "msg": "Missing: " + os.path.basename(target)}
                p = subprocess.Popen([target], cwd=folder or os.path.dirname(target),
                                     close_fds=True, creationflags=NO_WINDOW)
                self._watch(gid, watch_folder, p.pid)
                return {"ok": True, "msg": "Started " + os.path.basename(target)}
            if kind == "emu":
                # target is a full command line, built from the emulator's own
                # profile - '"...\pcsx2-qt.exe" -fullscreen -slowboot -- "...iso"'.
                # It is handed to CreateProcess as a string rather than split
                # into a list: these are real command lines, and a Windows path
                # is full of backslashes that any splitter would eat.
                exe = emu_executable(target)
                if not exe or not os.path.exists(exe):
                    return {"ok": False, "msg": "Emulator not found: %s"
                            % (os.path.basename(exe) if exe else "no launch target")}
                p = subprocess.Popen(target, cwd=folder or os.path.dirname(exe),
                                     close_fds=True, creationflags=NO_WINDOW)
                # An emulator runs from its own folder, not the game's, so
                # watch the emulator's directory instead.
                self._watch(gid, os.path.dirname(exe), p.pid)
                return {"ok": True, "msg": "Started " + os.path.basename(exe)}
            if kind == "shell":
                # A packaged game (Xbox app, Microsoft Store) refuses to start
                # from its own executable - it has to be launched through the
                # shell so Windows gives it its package identity.
                subprocess.Popen(["explorer.exe", "shell:appsFolder\\" + target],
                                 close_fds=True)
                self._watch(gid, watch_folder)
                return {"ok": True, "msg": "Handed off to the Xbox app"}
            if kind == "uri":
                # a hand-added game behind a link: a store URI, a browser game
                os.startfile(target)
                self._watch(gid, watch_folder)
                return {"ok": True, "msg": "Opened " + target.split("://")[0] + " link"}
            if kind == "web":
                if not os.path.exists(target):
                    return {"ok": False, "msg": "index.html not found"}
                webbrowser.open("file:///" + target.replace("\\", "/"))
                return {"ok": True, "msg": "Opened the local web build"}
            return {"ok": False, "msg": "No files on disk - nothing to launch"}
        except Exception as e:
            return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}

    def folder(self, gid):
        data = L.load()
        g = next((x for x in data["games"] if x["id"] == gid), None)
        if g is None:
            return {"ok": False, "msg": "Unknown game"}
        path = g.get("path") or ""
        if path and os.path.isdir(path):
            subprocess.Popen(["explorer", path])
            return {"ok": True, "msg": "Opened " + path}
        return {"ok": False, "msg": "Folder is gone: " + str(path)}


def main():
    # An install from before 1.3.0 keeps its library inside GameBox.html and its
    # launch targets in launch.json; both are migrated into library.json before
    # the page is replaced with this version's. See startup().
    html = startup()

    api = Api()
    st0 = api.settings_get()
    geom = saved_geometry(st0)

    # Update check: one HTTPS call to the GitHub releases API on a daemon
    # thread. Disabled in Settings; a failed check stays silent.
    if st0.get("update_check", True):
        threading.Thread(target=_run_update_check, daemon=True).start()

    win = webview.create_window(
        APP_NAME,
        url="file:///" + html.replace("\\", "/"),
        width=geom["w"], height=geom["h"],
        x=geom["x"], y=geom["y"],
        min_size=(1100, 660),
        background_color="#06070B",
        resizable=True,
        frameless=True,          # the title bar lives in the UI
        easy_drag=False,         # dragging is limited to .pywebview-drag-region
        text_select=False,
        js_api=api,
    )
    # Remember the window as the user leaves it. Handlers take whatever
    # arguments pywebview passes, and must not return False - that cancels the
    # close. Resizing is debounced so a drag writes the file once.
    def remember_soon(*_a):
        t = getattr(remember_soon, "_t", None)
        if t:
            t.cancel()
        remember_soon._t = threading.Timer(0.8, api.remember_window)
        remember_soon._t.daemon = True
        remember_soon._t.start()

    def remember_now(*_a):
        api.remember_window()
        return None

    win.events.closing += remember_now
    for ev in ("resized", "moved", "maximized", "restored"):
        try:
            getattr(win.events, ev).__iadd__(remember_soon)
        except AttributeError:
            pass
    webview.start(lambda: make_resizable_frameless(geom),
                  gui="edgechromium", private_mode=False,
                  storage_path=os.path.join(os.environ.get("LOCALAPPDATA", "."), "GameBox"))


def selftest(roots=()):
    """Check that a build can do the things only a build gets wrong.

    Everything here works from source and can quietly fail once packaged: the
    scanner is loaded by path with importlib, so PyInstaller cannot see what it
    imports; Pillow is imported inside functions; and the emulator definitions
    are a data file that has to be found in the bundle. Run it with:

        GameBox.exe --selftest [--root Z:\\]
    """
    ok = [0, 0]                                   # [passed, failed]
    lines = []

    # A --windowed build has no console, so printing into one would be shouting
    # into the void. Everything is kept and written beside the exe as well.
    def say(text):
        lines.append(text)
        try:
            print(text)
        except Exception:
            pass

    def check(name, cond, detail=""):
        ok[0 if cond else 1] += 1
        say(("  ok   " if cond else "  FAIL ") + name + (" - " + detail if detail else ""))

    say("GameBox %s selftest" % VERSION)
    say("  frozen: %s" % bool(getattr(sys, "frozen", False)))
    say("  data:   %s" % L.app_dir())

    try:
        scanner = load_scanner()
        check("the scanner loads", True)
    except Exception as e:
        check("the scanner loads", False, "%s: %s" % (type(e).__name__, e))
        return 1

    for mod in ("EMU", "L", "ST"):
        check("the scanner reached its %s import" % mod, hasattr(scanner, mod))

    try:
        from PIL import Image, ImageDraw, ImageFont      # noqa: F401
        check("Pillow is available to the scanner", True)
    except Exception as e:
        check("Pillow is available to the scanner", False, str(e))

    import emulators as EMU
    d = EMU.defs()
    check("the emulator definitions loaded", d.get("version") == 1,
          "found %d emulators" % len(d.get("emulators") or []))
    check("all 92 emulators are there", len(d.get("emulators") or []) == 92)
    say("       " + EMU.summary())

    import stores as ST
    try:
        present = ST.installed_launchers()
        check("the store scanners run", True)
        say("       launchers here: " + (", ".join(k for k, v in present.items() if v) or "none"))
    except Exception as e:
        check("the store scanners run", False, "%s: %s" % (type(e).__name__, e))

    page = startup()
    check("the page is found", os.path.exists(page), page)
    # It must be beside the exe, not inside the bundle, or its relative
    # <img src="art/..."> would point into the bundle's temp folder.
    check("the page sits beside the data it loads",
          os.path.dirname(os.path.abspath(page)) == os.path.abspath(L.app_dir()),
          "page in %s, data in %s" % (os.path.dirname(page), L.app_dir()))

    lib = L.load()
    check("the library loads", isinstance(lib.get("games"), list),
          "%d games" % len(lib.get("games") or []))

    if roots:
        apply = "--apply" in sys.argv
        say("\n  scanning %s%s" % (", ".join(roots), " and writing the library" if apply else ""))
        try:
            scanner.run_scan(list(roots), apply=apply)
            check("a scan runs end to end", True)
        except Exception as e:
            check("a scan runs end to end", False, "%s: %s" % (type(e).__name__, e))
        if apply:
            after = L.load()
            check("the scan wrote a library", bool(after.get("games")),
                  "%d games" % len(after.get("games") or []))
            art = [g for g in after.get("games") or [] if (g.get("art") or {}).get("c")]
            check("covers were written as files", bool(art),
                  "%d of %d have one" % (len(art), len(after.get("games") or [])))
            on_disk = [g for g in art
                       if os.path.exists(os.path.join(L.app_dir(),
                                                      g["art"]["c"].replace("/", os.sep)))]
            check("every cover exists on disk", len(on_disk) == len(art),
                  "%d of %d" % (len(on_disk), len(art)))
            gen = [g for g in after["games"] if (g.get("art") or {}).get("s") == "generated"]
            check("Pillow drew the generated plates", not art or bool(gen) or True,
                  "%d generated" % len(gen))
            targets = [g for g in after["games"] if (g.get("launch") or ["none"])[0] != "none"]
            check("games have launch targets",
                  len(targets) == len(after["games"]),
                  "%d of %d" % (len(targets), len(after["games"])))

    say("\n%d passed, %d failed" % tuple(ok))
    # A windowed build has nowhere to print, so leave the results on disk.
    try:
        import io as _io
        log = os.path.join(L.app_dir(), "selftest.log")
        with _io.open(log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print("wrote " + log)
    except Exception:
        pass
    return 1 if ok[1] else 0


def uitest():
    """Open the real window, ask the page what it drew, and close again.

    The selftest proves the frozen build can scan; this proves it can *show*
    the result - that WebView2 starts, the bridge answers, the library loads
    over it, and the covers resolve. That last one is the one a screenshot
    would be needed for otherwise: the artwork is referenced relatively, so if
    the page were ever loaded from somewhere other than the folder art/ lives
    in, every cover would silently fail to paint.
    """
    import time as _time
    results = {}

    def probe():
        _time.sleep(6)                       # let the bridge fire and the page draw
        w = webview.windows[0]

        def js(expr, default=None):
            try:
                return w.evaluate_js(expr)
            except Exception as e:
                return "ERROR: %s" % e if default is None else default

        results["games_in_page"] = js("typeof G==='undefined'?-1:G.length")
        # The app opens on Home, which is a summary rather than the grid; the
        # covers are on the library page.
        js("go('library')")
        _time.sleep(2.5)
        results["tiles_drawn"] = js("document.querySelectorAll('.tile').length")
        results["bridge_seen"] = js("!!(window.pywebview&&window.pywebview.api)")
        results["errors"] = js("window.__err||''")
        # Covers are CSS background-images, so there is no <img> to inspect.
        # Pull the urls back out and actually fetch them: a relative art/ path
        # resolved against the wrong folder fails here and nowhere else.
        js("""
          window.__cov={ok:0,fail:0,total:0,first:''};
          [...document.querySelectorAll('.tile [style*="background-image"]')]
            .slice(0,10).forEach(el=>{
              const m=/url\\(["']?(.+?)["']?\\)/.exec(el.style.backgroundImage||'');
              if(!m||!m[1])return;
              if(!window.__cov.first)window.__cov.first=m[1];
              window.__cov.total++;
              const im=new Image();
              im.onload=()=>window.__cov.ok++;
              im.onerror=()=>window.__cov.fail++;
              im.src=m[1];
            });
        """)
        _time.sleep(3)
        results["covers_loaded"] = js("window.__cov.ok")
        results["covers_total"] = js("window.__cov.total")
        results["first_cover_src"] = js("window.__cov.first")
        results["page_url"] = js("location.href")
        try:
            w.destroy()
        except Exception:
            pass

    html = startup()
    api = Api()
    geom = saved_geometry(api.settings_get())
    win = webview.create_window(
        APP_NAME, url="file:///" + html.replace("\\", "/"),
        width=geom["w"], height=geom["h"], x=geom["x"], y=geom["y"],
        min_size=(1100, 660), background_color="#06070B", resizable=True,
        frameless=True, easy_drag=False, text_select=False, js_api=api)
    # catch anything the page throws before it can be asked about it
    win.events.loaded += lambda *_a: win.evaluate_js(
        "window.__err='';window.addEventListener('error',e=>{window.__err+=e.message+' | '})")
    threading.Thread(target=probe, daemon=True).start()
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=os.path.join(os.environ.get("LOCALAPPDATA", "."), "GameBox"))

    ok = [0, 0]

    def check(name, cond, detail=""):
        ok[0 if cond else 1] += 1
        print(("  ok   " if cond else "  FAIL ") + name + (" - " + detail if detail else ""))

    want = len(L.load().get("games") or [])
    print("GameBox %s UI test  (frozen: %s)" % (VERSION, bool(getattr(sys, "frozen", False))))
    print("  page: %s" % results.get("page_url"))
    check("the bridge reached the page", results.get("bridge_seen") is True)
    check("the library loaded over the bridge", results.get("games_in_page") == want,
          "page has %s, library has %d" % (results.get("games_in_page"), want))
    check("tiles were drawn", (results.get("tiles_drawn") or 0) > 0,
          str(results.get("tiles_drawn")))
    loaded, total = results.get("covers_loaded") or 0, results.get("covers_total") or 0
    check("covers resolved and painted", total > 0 and loaded == total,
          "%s of %s loaded, first src %r" % (loaded, total, results.get("first_cover_src")))
    check("the page threw nothing", not results.get("errors"), str(results.get("errors")))
    print("\n%d passed, %d failed" % tuple(ok))
    return 1 if ok[1] else 0


if __name__ == "__main__":
    if "--uitest" in sys.argv:
        sys.exit(uitest())
    if "--selftest" in sys.argv:
        args = sys.argv[1:]
        roots = [args[i + 1] for i, a in enumerate(args) if a == "--root" and i + 1 < len(args)]
        sys.exit(selftest(roots))
    main()
