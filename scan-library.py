#!/usr/bin/env python3
"""
GameBox library scanner
=======================

Finds the games installed on this PC, resolves cover art and store metadata,
and writes the result straight into the app.

  python scan-library.py                     scan and print a report
  python scan-library.py --json library.json write the raw scan out as JSON
  python scan-library.py --apply             merge the scan into library.json
  python scan-library.py --root C:\\ --root D:\\Games

What it reads
-------------
  Steam       <drive>\\SteamLibrary\\steamapps\\appmanifest_*.acf
              real title, appid, size on disk, last played, last updated
  GOG         %ProgramData%\\GOG.com\\Galaxy\\storage\\galaxy-2.0.db
              owned and installed releases, plus each game's cover hashes
  Epic        %ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item
              display name, app id and launch executable
  Xbox        <drive>\\XboxGames\\*\\Content\\MicrosoftGame.config
  Ubisoft     HKLM\\SOFTWARE\\Ubisoft\\Launcher\\Installs
  EA          HKLM\\SOFTWARE\\Electronic Arts, plus each install's installerdata.xml
  Battle.net  each install's .build.info, and the uninstall entries
  Amazon      %LOCALAPPDATA%\\Amazon Games\\...\\GameInstallInfo.sqlite
  itch.io     %APPDATA%\\itch\\db\\butler.db
  Folders     one game per top-level folder under each --root
  ROMs        any file a known emulator can open, and PlayStation 3 game
              folders, which are directories rather than disc images

Emulators
---------
  92 emulators and 561 profiles, from Playnite's definitions in emulation/
  (MIT - see emulation/NOTICE). A profile says which platforms it covers, which
  extensions it accepts, how to recognise its executable, and the command line
  to start a game with - so a game is launched the way its emulator expects
  rather than with a bare path and no arguments.

  Where a file cannot say which console it is for - a .iso is a PS2 disc or a
  PS3 disc - the folder it sits in decides, and the app can override that per
  game.

Cover art, in priority order
----------------------------
  1. Steam's own cache      %ProgramFiles(x86)%\\Steam\\appcache\\librarycache
  2. GOG Galaxy's webcache  %ProgramData%\\GOG.com\\Galaxy\\webcache
  3. Steam's public CDN     matched by title through Steam's app search
  4. Generated              a platform plate for disc images and ROMs

With --apply it merges the scan into the library:
  library.json     the library the app reads, keyed by a derived id so that a
                   rescan refreshes a game rather than replacing it - anything
                   you have done to a game (favourite, tags, notes, playtime)
                   is left alone
  art/             covers, heroes and screenshots, as files
  library-backups/ a timestamped copy taken before every merge

A game the scan no longer finds is marked not installed rather than deleted,
so uninstalling a game does not throw away what you recorded about it.

It only ever reads your games. It never moves, deletes or modifies them.
"""

import argparse, io, json, os, re, sqlite3, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emulators as EMU
import library as L
import stores as ST

HERE = os.path.dirname(os.path.abspath(__file__))
STEAM_CACHE = os.path.expandvars(r"%ProgramFiles(x86)%\Steam\appcache\librarycache")
GOG_DB = os.path.expandvars(r"%ProgramData%\GOG.com\Galaxy\storage\galaxy-2.0.db")
GOG_CACHE = os.path.expandvars(r"%ProgramData%\GOG.com\Galaxy\webcache")
EPIC_MANIFESTS = os.path.expandvars(r"%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests")
UA = {"User-Agent": "GameBox-Scanner/1.0"}
GB = 1024 ** 3

# Which emulators are installed here. Filled in once per scan by find_emulators()
# from the definitions in emulation/ - 92 emulators, 561 profiles, 326 ROM
# extensions - rather than the four extensions and six executable names this
# used to hardcode. A user emulators.json next to the app still wins.
INSTALLED_EMULATORS = {}


def find_emulators(roots=()):
    """Look for installed emulators once, and remember what was found."""
    global INSTALLED_EMULATORS
    override = None
    path = os.path.join(HERE, "emulators.json.user")
    legacy = os.path.join(HERE, "emulators.json")
    for p in (path, legacy):
        # emulators.json is the compiled definitions now, so only treat it as a
        # user override if it looks like the old {".iso": [...]} shape.
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8-sig"))
                if isinstance(d, dict) and "emulators" not in d:
                    override = d
                    break
            except Exception:
                pass
    INSTALLED_EMULATORS = EMU.find_installed(roots, override)
    return INSTALLED_EMULATORS


def emulator_launch(target, prefer=None):
    """('emu', command line, working dir) for a ROM or game folder, or None."""
    hint = platform_hint(target)
    if os.path.isdir(target):
        match, image = EMU.resolve_folder(target, INSTALLED_EMULATORS)
        # A folder is matched on its shape rather than an extension, but an
        # explicit choice still wins - the folder is what gets handed over.
        if prefer:
            chosen = EMU.resolve(target, INSTALLED_EMULATORS, prefer, hint)
            if chosen:
                match, image = chosen, (image or target)
    else:
        match = EMU.resolve(target, INSTALLED_EMULATORS, prefer, hint)
        image = target
    if not match:
        return None
    return ("emu", EMU.command_line(match, image), EMU.working_dir(match, image)), match


SKIP_FOLDERS = {"system volume information", "$recycle.bin", "windows", "windows.old",
                "program files", "program files (x86)", "programdata", "users", "temp", "tmp",
                "downloads", "documents", "pictures", "music", "videos", "desktop", "onedrive",
                "backup", "tools", "code", "files", "camera", "camera01", "drivers", "intel",
                "nvidia", "amd", "perflogs", "inetpub", "recovery", "msocache", "config.msi",
                "media & captures", "projects & code", "archived office temps", "installers & builds",
                "ai", "antigravity", "watson code", "new folder", "$windows.~bt", "$windows.~ws"}
SKIP_PATTERNS = (re.compile(r"^backup[_\- ]", re.I), re.compile(r"^\$", re.I),
                 re.compile(r"^windows", re.I), re.compile(r"^python\d", re.I),
                 re.compile(r"redist|directx|vcredist|dotnet|\.net|visual c\+\+", re.I))
# Folders that host other games rather than being one, and emulator/tool homes.
CONTAINER_NAMES = {"steamlibrary", "steamapps", "gog galaxy", "epic games", "origin games",
                   "ea games", "ubisoft", "battle.net", "ps3 games", "ps2", "ps1", "roms",
                   "emulators", "emulation"}
EMU_HOMES = re.compile(r"^(rpcs3|pcsx2|dolphin|cemu|yuzu|ryujinx|ppsspp|retroarch|mgba|"
                       r"visualboyadvance|duckstation|xenia|citra|melonds)", re.I)
SKIP_TITLES = {"unreal engine", "quixel bridge", "fab ue plugin", "epic online services",
               "steamworks common redistributables", "steamvr", "steam linux runtime",
               "proton", "steam controller configs", "spacewar", "half-life 2 demo"}
# Steam publishes tooling under the same manifest format as games; these are not games.
SKIP_APPIDS = {"228980", "250820", "1070560", "1391110", "1493710", "1628350", "1826330"}


def human(n):
    return "%.1f GB" % (n / GB)


# Sizing every folder is most of a scan's runtime, and almost nothing changes
# between two scans. The last scan's answer is kept with the folder's mtime, so
# an unchanged folder costs one stat instead of walking a hundred thousand
# files. Filled in by load_size_cache() at the start of a scan.
SIZE_CACHE = {}
_size_hits = [0, 0]      # [reused, measured]


def load_size_cache():
    """Remember what the last scan measured, so a rescan can skip most of it."""
    SIZE_CACHE.clear()
    _size_hits[0] = _size_hits[1] = 0
    for g in L.load().get("games", []):
        p, size, mtime = g.get("path"), g.get("bytes"), g.get("mtime")
        if p and size and mtime:
            SIZE_CACHE[L.norm_path(p)] = (size, mtime)
    return SIZE_CACHE


def folder_mtime(path):
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def folder_size(path, budget=400_000):
    """How much this folder holds, reusing the last scan's answer when it can.

    A folder's own mtime changes when anything is added or removed directly in
    it, which is what an install, an update or a delete does. It does not catch
    a file quietly growing three levels down - but a size that is a few hundred
    megabytes stale is a fair trade for a rescan that takes seconds.
    """
    key = L.norm_path(path)
    mtime = folder_mtime(path)
    cached = SIZE_CACHE.get(key)
    if cached and mtime and cached[1] == mtime:
        _size_hits[0] += 1
        return cached[0]

    total = seen = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            seen += 1
            if seen > budget:
                _size_hits[1] += 1
                SIZE_CACHE[key] = (total, mtime)
                return total
    _size_hits[1] += 1
    SIZE_CACHE[key] = (total, mtime)
    return total


def ts(v, fmt="%b %d, %Y"):
    v = int(v or 0)
    return time.strftime(fmt, time.localtime(v)) if v > 0 else None


def get(url, timeout=25):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
    except Exception:
        return None


norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ------------------------------------------------------------------ sources
def scan_steam(roots):
    out, seen = [], set()
    dirs = []
    for r in roots:
        dirs += [os.path.join(r, "SteamLibrary", "steamapps"), os.path.join(r, "steamapps"),
                 os.path.join(r, "Steam", "steamapps")]
    dirs.append(os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steamapps"))
    for d in dirs:
        if not os.path.isdir(d) or d.lower() in seen:
            continue
        seen.add(d.lower())
        for f in os.listdir(d):
            if not (f.startswith("appmanifest_") and f.endswith(".acf")):
                continue
            try:
                t = open(os.path.join(d, f), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            g = lambda k: (re.search(r'"%s"\s+"([^"]*)"' % k, t) or [None, None])[1]
            if not g("name"):
                continue
            if g("appid") in SKIP_APPIDS or norm(g("name")) in {norm(x) for x in SKIP_TITLES}:
                continue
            out.append({"title": g("name"), "store": "steam", "appid": g("appid"),
                        "bytes": int(g("SizeOnDisk") or 0), "updated": ts(g("LastUpdated")),
                        "played": ts(g("LastPlayed")),
                        "path": os.path.join(d, "common", g("installdir") or g("name")),
                        "via": "Steam runtime",
                        "launch": ("steam", g("appid"))})
    return out


def scan_gog():
    if not os.path.exists(GOG_DB):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % GOG_DB.replace("\\", "/"), uri=True)
        cur = con.cursor()
        cur.execute("select releaseKey from LibraryReleases")
        keys = [r[0] for r in cur.fetchall()]
        installed, paths = set(), {}
        try:
            cur.execute("select productId, installationPath from InstalledBaseProducts")
            for pid, ipath in cur.fetchall():
                installed.add("gog_%s" % pid)
                if ipath:
                    paths["gog_%s" % pid] = ipath
        except sqlite3.Error:
            try:
                cur.execute("select productId from InstalledBaseProducts")
                installed = {"gog_%s" % r[0] for r in cur.fetchall()}
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        return []

    def piece(key, tid):
        cur.execute("select value from GamePieces where releaseKey=? and gamePieceTypeId=?", (key, tid))
        row = cur.fetchone()
        try:
            return json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            return None

    out = []
    for k in keys:
        if k not in installed:
            continue
        title = (piece(k, 236) or {}).get("title")
        if not title:
            continue
        img = piece(k, 145) or piece(k, 209) or {}
        grab = lambda u: (re.search(r"/([0-9a-f]{64})_", u or "") or [None, None])[1] if isinstance(u, str) else None
        entry = {"title": title, "store": "gog", "via": "GOG Galaxy",
                 "product_id": k.split("_", 1)[-1],   # releaseKey is "gog_<id>"
                 "cover_hash": grab(img.get("verticalCover") if isinstance(img, dict) else ""),
                 "bg_hash": grab(img.get("background") if isinstance(img, dict) else "")}
        ipath = paths.get(k)
        if ipath and os.path.isdir(ipath):
            entry["path"] = os.path.normpath(ipath)   # GOG records forward slashes
            entry["bytes"] = folder_size(ipath)
            exe = best_exe(ipath)
            if exe:
                entry["launch"] = ("exe", exe, ipath)
        out.append(entry)
    return out


def scan_epic():
    out = []
    if not os.path.isdir(EPIC_MANIFESTS):
        return out
    for f in os.listdir(EPIC_MANIFESTS):
        if not f.endswith(".item"):
            continue
        try:
            m = json.load(open(os.path.join(EPIC_MANIFESTS, f), encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        name = m.get("DisplayName", "?")
        if norm(name) in {norm(x) for x in SKIP_TITLES}:
            continue
        out.append({"title": name, "store": "epic", "via": "Epic launcher",
                    "app_name": m.get("AppName", ""),
                    "path": m.get("InstallLocation", ""), "bytes": int(m.get("InstallSize") or 0),
                    "launch": ("epic", m.get("AppName", ""))})
    return out


TITLE_NOISE = re.compile(r"\s*\((?:usa|eu|jp|europe|japan|en|ja|fr|es|de|it|"
                         r"en,?\s*\w+(?:,\s*\w+)*|v[\d.]+|collectors? edition|disc \d)\)", re.I)


def pretty_title(name):
    """Folder names are not titles: drop region/version tags and tidy separators."""
    t = TITLE_NOISE.sub("", name)
    t = re.sub(r"\s{2,}", " ", t).strip(" -_")
    return t or name


def looks_like_a_game(name, path=""):
    low = name.lower()
    if low in SKIP_FOLDERS or low in CONTAINER_NAMES or name.startswith("."):
        return False
    # A console folder holds a "bios" and a "saves" beside its games, and those
    # are full of .bin files that any emulator would cheerfully be handed.
    if NOT_A_GAME.search(name):
        return False
    if EMU_HOMES.match(name):
        return False
    if any(p.search(name) for p in SKIP_PATTERNS):
        return False
    # a folder that hosts a Steam library is a container, not a game
    if path and os.path.isdir(os.path.join(path, "steamapps")):
        return False
    return True


def scan_folders(root, min_gb=0.5):
    """One game per top-level folder - but only if it actually looks like a game.

    A folder qualifies when it holds a launchable executable or a disc image.
    Size alone is not evidence: backups, camera dumps and toolchains are big too.
    """
    out = []
    if not os.path.isdir(root):
        return out
    entries = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        # "PS3 Games", "roms" and friends hold games rather than being one, so
        # step inside and treat each child as a candidate. A ROM collection is
        # usually one *file* per game rather than one folder, so take those too.
        if name.lower() in CONTAINER_NAMES and not os.path.isdir(os.path.join(p, "steamapps")):
            try:
                for sub in sorted(os.listdir(p)):
                    full = os.path.join(p, sub)
                    if os.path.isdir(full):
                        entries.append((sub, full))
                    elif os.path.splitext(sub)[1].lower().lstrip(".") in ROM_EXTS                             and not NOT_A_GAME.search(sub):
                        entries.append((os.path.splitext(sub)[0], full))
            except OSError:
                pass
            continue
        entries.append((name, p))

    for name, p in entries:
        if not os.path.isdir(p):
            # a loose ROM in a collection folder: the file is the game
            out.append(rom_entry(name, p))
            continue
        if not looks_like_a_game(name, p):
            continue
        exe = best_exe(p)
        # An executable still wins: a PC game folder can easily hold a .bin or
        # a .zip, and those are ROM extensions too. Only a folder with nothing
        # to run is considered for emulation.
        emu_target = None
        if not exe:
            emu_target = first_rom(p) or (p if EMU.resolve_folder(p, INSTALLED_EMULATORS)[0] else None)
        web = None if (exe or emu_target) else (os.path.join(p, "index.html")
                                                if os.path.exists(os.path.join(p, "index.html")) else None)
        if not exe and not emu_target and not web:
            continue                      # no launchable payload: not a game
        size = folder_size(p)
        if size < min_gb * GB and not emu_target and not web:
            continue
        entry = {"title": pretty_title(name), "store": "local", "path": p, "bytes": size,
                 "updated": ts(os.path.getmtime(p)), "via": "Direct executable"}
        if web:
            entry["launch"] = ("web", web, p)
            entry["via"] = "Local web build"
        elif exe:
            entry["launch"] = ("exe", exe, p)
        elif emu_target:
            # A ROM, or a folder an emulator opens - a PlayStation 3 game is a
            # directory tree, not an image, which is why RPCS3 declares no
            # extensions at all and is matched on the shape of the folder.
            got = emulator_launch(emu_target)
            entry["store"] = "emu"
            # Remember which file inside the folder is the game, so the app can
            # offer the other emulators that could open it without re-scanning.
            if os.path.isfile(emu_target):
                entry["rom"] = emu_target
            if got:
                entry["launch"], match = got
                entry["via"] = match.name
                entry["platform"] = (match.platforms or [None])[0]
            else:
                entry["launch"] = ("none", "")
                entry["via"] = "No emulator installed for " + (
                    os.path.splitext(emu_target)[1].lstrip(".").upper() or "this game")
        out.append(entry)
    return out


# Every extension any of the 92 known emulators can open - 326 of them, where
# this used to be a hardcoded four. Loaded once; empty if the definitions were
# not shipped, in which case nothing is treated as a ROM.
ROM_EXTS = EMU.extensions()

# Breadth has a cost. RetroArch will happily be pointed at a .exe, .bat or .dll,
# DuckStation at a .exe, Kega Fusion at a .md - which is also every README ever
# written. Left alone, a README.md inside a web build made it a Mega Drive game.
# These extensions are only treated as ROMs when the file sits directly in a
# folder that exists to hold ROMs, where the intent is not in doubt.
AMBIGUOUS_EXTS = {"md", "bin", "exe", "bat", "cmd", "com", "dll", "app", "dat",
                  "cfg", "xml", "img", "gz", "tar", "rar", "txt", "log", "ini"}
SCAN_ROM_EXTS = ROM_EXTS - AMBIGUOUS_EXTS

# A ROM collection is usually sorted by console, and the folder name is the only
# thing that says which - an .iso is a PS2 disc or a PS3 disc depending entirely
# on where it lives. RPCS3 declares no extensions at all, so without this hint a
# PS3 image is handed to PCSX2, which cannot boot it.
PLATFORM_HINTS = (
    (re.compile(r"\bps3\b|playstation ?3", re.I), "sony_playstation3"),
    (re.compile(r"\bps2\b|playstation ?2", re.I), "sony_playstation2"),
    (re.compile(r"\bps1\b|\bpsx\b|playstation ?1", re.I), "sony_playstation"),
    (re.compile(r"\bpsp\b", re.I), "sony_psp"),
    (re.compile(r"gamecube|\bngc\b", re.I), "nintendo_gamecube"),
    (re.compile(r"\bwii ?u\b", re.I), "nintendo_wiiu"),
    (re.compile(r"\bwii\b", re.I), "nintendo_wii"),
    (re.compile(r"\bnds\b|nintendo ?ds", re.I), "nintendo_ds"),
    (re.compile(r"\b3ds\b", re.I), "nintendo_3ds"),
    (re.compile(r"\bn64\b|nintendo ?64", re.I), "nintendo_64"),
    (re.compile(r"\bsnes\b|super ?nintendo", re.I), "nintendo_super_nes"),
    (re.compile(r"\bnes\b|famicom", re.I), "nintendo_nes"),
    (re.compile(r"\bgba\b|game ?boy ?advance", re.I), "nintendo_gameboyadvance"),
    (re.compile(r"\bgbc\b|game ?boy ?color", re.I), "nintendo_gameboycolor"),
    (re.compile(r"game ?boy", re.I), "nintendo_gameboy"),
    (re.compile(r"dreamcast", re.I), "sega_dreamcast"),
    (re.compile(r"saturn", re.I), "sega_saturn"),
    (re.compile(r"mega ?drive|genesis", re.I), "sega_genesis"),
    (re.compile(r"xbox ?360", re.I), "microsoft_xbox360"),
)
# Folders inside a console's directory that hold system files, not games.
NOT_A_GAME = re.compile(r"\b(bios|firmware|saves?|savestates?|memcards?|"
                        r"screenshots?|cheats?|shaders?|covers?|themes?)\b", re.I)


def platform_hint(path):
    """The console a path implies, from the folder names above it."""
    for part in reversed(os.path.normpath(path).split(os.sep)):
        for rx, pid in PLATFORM_HINTS:
            if rx.search(part):
                return pid
    return None

# A multi-disc game names its discs in an .m3u; the playlist is the game, and
# the discs beside it are not three more games.
PLAYLIST_EXTS = ("m3u", "m3u8")


def first_rom(folder, depth=2):
    """The ROM in a folder, preferring a playlist over the discs it lists."""
    best = None
    for root, _d, files in os.walk(folder):
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower().lstrip(".")
            if ext in PLAYLIST_EXTS:
                return os.path.join(root, f)      # a playlist settles it
            if ext in SCAN_ROM_EXTS and best is None:
                best = os.path.join(root, f)
        if root.count(os.sep) - folder.count(os.sep) > depth:
            break
    return best


def rom_entry(name, path):
    """A loose ROM file in a collection folder is one game."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    entry = {"title": pretty_title(name), "store": "emu", "path": os.path.dirname(path),
             "rom": path, "bytes": size, "updated": ts(os.path.getmtime(path))
             if os.path.exists(path) else None}
    got = emulator_launch(path)
    if got:
        entry["launch"], match = got
        entry["via"] = match.name
        entry["platform"] = (match.platforms or [None])[0]
    else:
        entry["launch"] = ("none", "")
        entry["via"] = "No emulator installed for " + (
            os.path.splitext(path)[1].lstrip(".").upper() or "this file")
    return entry


BAD_EXE = re.compile(r"unins|crash|redist|vcredist|setup|touchup|updater|helper|dxsetup|"
                     r"activation|installer|cleanup|trial|launcher_", re.I)


def best_exe(folder):
    best = None
    for root, _d, files in os.walk(folder):
        for f in files:
            if not f.lower().endswith(".exe") or BAD_EXE.search(f):
                continue
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if best is None or sz > best[0]:
                best = (sz, p)
        if root.count(os.sep) - folder.count(os.sep) > 2:
            break
    return best[1] if best else None


# ----------------------------------------------------------------- artwork
def enc(im, w, h, q, stem):
    """Resize, encode, and write into art/. Returns the path the page uses.

    Artwork used to be inlined as base64 data: URIs, which is what made a
    scanned page 3.7 MB and slow enough to parse that the app could miss its
    own bridge-ready event. Files load lazily and cost the library nothing.
    """
    from PIL import Image
    im = im.convert("RGB").resize((w, h), Image.LANCZOS)
    os.makedirs(L.art_dir(), exist_ok=True)
    name = stem + ".jpg"
    im.save(os.path.join(L.art_dir(), name), "JPEG",
            quality=q, optimize=True, progressive=True)
    return "art/" + name


def steam_appid(title):
    d = get("https://steamcommunity.com/actions/SearchApps/" + urllib.parse.quote(title))
    if not d:
        return None
    try:
        arr = json.loads(d.decode("utf-8", "ignore"))
    except ValueError:
        return None
    target, best = norm(title), None
    for a in arr[:8]:
        n = norm(a.get("name", ""))
        score = 2 if n == target else 1 if target in n or n in target else 0
        if best is None or score > best[0]:
            best = (score, a["appid"])
    return best[1] if best and best[0] else None


def store_meta(appid, stem="meta"):
    d = get("https://store.steampowered.com/api/appdetails?appids=%s&l=english" % appid)
    if not d:
        return None
    try:
        j = json.loads(d.decode("utf-8", "ignore"))[str(appid)]
    except Exception:
        return None
    if not j.get("success"):
        return None
    a = j["data"]
    keep = ("Single-player", "Multi-player", "Co-op", "Online Co-op", "Shared/Split Screen",
            "Full controller support", "Partial Controller Support", "Steam Cloud",
            "Steam Achievements", "VR Supported", "VR Only", "PvP")
    req = a.get("pc_requirements") or {}
    txt = re.sub(r"<[^>]+>", " ", req.get("minimum", "") if isinstance(req, dict) else "")
    m = re.search(r"OS[^:]*:\s*(.*?)(?:Processor|Memory|$)", txt)
    meta = {"desc": (a.get("short_description") or "")[:230],
            "dev": ", ".join(a.get("developers", [])[:2]),
            "pub": ", ".join(a.get("publishers", [])[:2]),
            "genres": [g["description"] for g in a.get("genres", [])][:3],
            "rel": (a.get("release_date") or {}).get("date", ""),
            "cats": [c["description"] for c in a.get("categories", []) if c["description"] in keep][:6],
            "os": (m.group(1)[:40].strip(" *:") if m else "Windows"), "shots": []}
    from PIL import Image
    for i, sc in enumerate((a.get("screenshots") or [])[:3]):
        raw = get(sc.get("path_thumbnail") or sc.get("path_full"), 20)
        if raw:
            try:
                im = Image.open(io.BytesIO(raw))
                meta["shots"].append(enc(im, 340, int(im.height * 340 / im.width), 68,
                                         stem + "-s%d" % i))
            except Exception:
                pass
    return meta


def generated_plate(title, platform=""):
    from PIL import Image, ImageDraw, ImageFont
    tints = {"PS3": ((24, 52, 74), (8, 16, 26)), "PS2": ((60, 26, 40), (16, 10, 18)),
             "GB": ((176, 150, 40), (40, 34, 14)), "": ((40, 44, 54), (14, 16, 22))}
    c1, c2 = tints.get(platform, tints[""])
    W, H = 300, 450
    im = Image.new("RGB", (W, H), c1)
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    d.rectangle([0, 0, W, 46], fill=(12, 14, 20))
    fdir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

    def font(sz):
        for n in ("segoeuib.ttf", "arialbd.ttf"):
            p = os.path.join(fdir, n)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, sz)
                except OSError:
                    pass
        return ImageFont.load_default()

    d.text((14, 15), platform or "NO STORE ARTWORK", font=font(17), fill=(232, 238, 248))
    d.rectangle([0, 46, W, 49], fill=(255, 122, 26))
    f = font(29)
    lines, cur = [], ""
    for w in title.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= W - 36:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    y = H - 40 - len(lines[:4]) * 33
    for ln in lines[:4]:
        d.text((18, y), ln, font=f, fill=(255, 255, 255))
        y += 33
    d.rectangle([0, 0, W - 1, H - 1], outline=(255, 255, 255), width=1)
    return im


def artwork(game, gog_index, stem, want_meta=True):
    """-> (cover, ambient, source, meta|None), written into art/ as <stem>*.jpg"""
    from PIL import Image
    appid = game.get("appid")
    if appid:
        d = os.path.join(STEAM_CACHE, str(appid))
        cov = os.path.join(d, "library_600x900.jpg")
        if os.path.exists(cov):
            hero = os.path.join(d, "library_hero.jpg")
            return (enc(Image.open(cov), 300, 450, 80, stem),
                    enc(Image.open(hero), 640, 300, 70, stem + "-h") if os.path.exists(hero) else None,
                    "steam-cache", store_meta(appid, stem) if want_meta else None)
    if game.get("cover_hash"):
        fc = gog_index.get(game["cover_hash"] + "_glx_vertical_cover.webp")
        if fc:
            fb = gog_index.get((game.get("bg_hash") or "") + "_glx_bg_top_padding_7.webp")
            aid = appid or (steam_appid(game["title"]) if want_meta else None)
            return (enc(Image.open(fc), 300, 450, 80, stem),
                    enc(Image.open(fb), 640, 300, 70, stem + "-h") if fb else None,
                    "gog-cache", store_meta(aid, stem) if (aid and want_meta) else None)
    aid = appid or (steam_appid(game["title"]) if want_meta else None)
    if aid and want_meta:
        raw = get("https://cdn.akamai.steamstatic.com/steam/apps/%s/library_600x900.jpg" % aid)
        if raw:
            hero = get("https://cdn.akamai.steamstatic.com/steam/apps/%s/library_hero.jpg" % aid)
            return (enc(Image.open(io.BytesIO(raw)), 300, 450, 80, stem),
                    enc(Image.open(io.BytesIO(hero)), 640, 300, 70, stem + "-h") if hero else None,
                    "steam-cdn", store_meta(aid, stem) if want_meta else None)
    plat = "PS3" if "ps3" in (game.get("path") or "").lower() else \
           "PS2" if re.search(r"\(usa\)|\(eu\)|iso", game.get("path") or "", re.I) else ""
    im = generated_plate(game["title"], plat)
    return (enc(im, 300, 450, 80, stem), enc(im, 640, 300, 70, stem + "-h"),
            "generated", None)


# ---------------------------------------------------------------------- ids
def stable_id(g):
    """The id a game keeps across rescans.

    Prefer the store's own key - an appid, an Epic AppName, a GOG product id -
    because that is the one thing about a game that does not move. A folder
    game has no such key, so it falls back to its install path.
    """
    store = g.get("store")
    if store == "steam" and g.get("appid"):
        return L.game_id("steam", g["appid"])
    if store == "epic" and g.get("app_name"):
        return L.game_id("epic", g["app_name"])
    if store == "gog" and g.get("product_id"):
        return L.game_id("gog", g["product_id"])
    key = ST.ID_KEYS.get(store)
    if key and g.get(key):
        return L.game_id(store, g[key])
    if g.get("path"):
        return L.game_id(store or "local", path=g["path"])
    return L.game_id(store or "local",
                     key="title-" + norm(g.get("title", "")))


# ------------------------------------------------------------------- write
def apply_to_library(games, prune=True):
    """Fold the scan into library.json, keeping everything the user owns.

    This used to splice a `const G=[...]` array into GameBox.html with a regex
    and write a parallel launch.json, both keyed by scan order - so a rescan
    renumbered every game and threw away anything the user had done. Now the
    scan is merged into a library keyed by a derived id, and the page is code
    again.
    """
    L.backup("scan")
    data, added, updated, missing, rekeyed = L.merge(games, data=L.load(), prune=prune)
    if not L.save(data):
        print("! could not write library.json")
        return False
    bits = ["%d new" % added, "%d updated" % updated]
    if rekeyed:
        bits.append("%d re-keyed" % rekeyed)
    if missing:
        bits.append("%d no longer installed" % missing)
    print("+ library.json - %d games (%s), %.0f KB"
          % (len(data["games"]), ", ".join(bits),
             os.path.getsize(L.library_file()) / 1e3))
    return True


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Scan this PC for installed games.")
    ap.add_argument("--root", action="append", default=[], help="drive or folder to scan (repeatable)")
    ap.add_argument("--json", metavar="FILE", help="write the raw scan as JSON")
    ap.add_argument("--apply", action="store_true",
                    help="merge the scan into library.json and write art/")
    ap.add_argument("--no-meta", action="store_true", help="covers only, skip store metadata")
    a = ap.parse_args()
    roots = a.root or [r for r in (os.environ.get("GAMEBOX_ROOT", "").split(";")) if r]
    if not roots:
        ap.error("give at least one --root, e.g. --root C:\\Games --root D:\\")
    return run_scan(roots, apply=a.apply, json_out=a.json, no_meta=a.no_meta)


def run_scan(roots, apply=False, json_out=None, no_meta=False, should_stop=None):
    """Scan the given roots. The app calls this directly, on a thread.

    should_stop  a callable checked between games, so a cancelled scan stops
                 without leaving the library half-merged
    """
    class a:                       # what the body below still reads
        pass
    a.json, a.apply, a.no_meta = json_out, apply, no_meta
    stop = should_stop or (lambda: False)

    t0 = time.time()
    print("GameBox scanner - roots: %s\n" % ", ".join(roots), flush=True)
    inst = find_emulators(roots)
    if inst:
        names = sorted(EMU.emulator(e)["name"] for e in inst)
        print("  emulators   %3d installed - %s" % (len(inst), ", ".join(names)[:70]), flush=True)
    found = []
    steam = scan_steam(roots)
    print("  Steam       %3d titles" % len(steam), flush=True)
    found += steam
    gog = scan_gog()
    print("  GOG         %3d installed" % len(gog), flush=True)
    found += gog
    epic = scan_epic()
    print("  Epic        %3d titles" % len(epic), flush=True)
    found += epic
    for label, _key, fn in ST.SCANNERS:
        try:
            got = fn(roots)
        except Exception as e:
            print("  %-11s failed: %s" % (label, e), flush=True)
            continue
        if got:
            print("  %-11s %3d installed" % (label, len(got)), flush=True)
            found += got

    for r in roots:
        f = scan_folders(r)
        print("  %-11s %3d folders" % (r, len(f)), flush=True)
        found += f

    # Merge duplicates. A store entry always wins over a bare folder, and the
    # match is on the install path as well as the title, because a folder is
    # frequently named nothing like the game it holds - launchers use their own
    # slugs, and some add a random suffix.
    def keys(g):
        out = [("t", norm(g["title"]))]
        p = (g.get("path") or "").replace("/", "\\").rstrip("\\").lower()
        if p:
            out.append(("p", p))
        return out

    merged, index = [], {}
    for g in sorted(found, key=lambda x: x["store"] == "local"):   # store entries first
        at = next((index[k] for k in keys(g) if k in index), None)
        if at is not None:
            cur = merged[at]
            for field in ("bytes", "path", "updated", "played", "appid",
                          "app_name", "product_id", "rom", "platform",
                          "xbox_pfn", "ubi_id", "ea_id", "bnet_code",
                          "amazon_id", "itch_id",
                          "cover_hash", "bg_hash", "launch", "via"):
                if not cur.get(field) and g.get(field):
                    cur[field] = g[field]
            for k in keys(cur):
                index[k] = at
            continue
        for k in keys(g):
            index[k] = len(merged)
        merged.append(dict(g))

    total = sum(g.get("bytes", 0) for g in merged)
    print("\n  %d games - %s - %.1f s" % (len(merged), human(total), time.time() - t0))
    if _size_hits[0]:
        print("  %d folder sizes reused from the last scan, %d measured" % tuple(_size_hits))
    for g in merged:
        if g.get("bytes", 1) == 0:
            print("  ! %s: install folder is empty" % g["title"])

    if a.json:
        json.dump(merged, open(a.json, "w", encoding="utf-8"), indent=1, default=str)
        print("+ wrote %s" % a.json)

    if not a.apply:
        return 0

    gog_index = {}
    for root, _d, fs in os.walk(GOG_CACHE):
        for f in fs:
            gog_index.setdefault(f, os.path.join(root, f))

    # What the last scan already resolved. Reaching Steam's CDN and its store
    # API for every game is most of a rescan's runtime, and a cover already on
    # disk is the same cover.
    have_art, have_meta = {}, {}
    for pg in L.load().get("games", []):
        art = pg.get("art") or {}
        cov = art.get("c")
        if cov and os.path.exists(os.path.join(L.app_dir(), cov.replace("/", os.sep))):
            have_art[pg["id"]] = art
            have_meta[pg["id"]] = pg.get("meta")
    games, counts, stopped, reused = [], {}, False, 0
    for g in merged:
        if stop():
            print("\n  stopped - merging the %d games resolved so far" % len(games))
            stopped = True
            break
        gid = stable_id(g)
        stem = L.art_name(gid)
        # A cover the last scan already resolved is kept, unless it is one of
        # the generated plates - those are worth another try, in case the game
        # can be matched properly now.
        keep = have_art.get(gid)
        if keep and keep.get("s") not in (None, "", "none", "generated"):
            cover, ambient, src = keep.get("c"), keep.get("a"), keep.get("s")
            meta = have_meta.get(gid)
            reused += 1
        else:
            cover, ambient, src, meta = artwork(g, gog_index, stem, want_meta=not a.no_meta)
        counts[src] = counts.get(src, 0) + 1
        by = g.get("bytes", 0)
        entry = {"id": gid, "title": g["title"], "store": g["store"], "bytes": by,
                 "installed": True,
                 "updated": g.get("updated"), "played": g.get("played"),
                 "state": "err" if by == 0 else
                          ("new" if (g["store"] == "steam" and not g.get("played")) else ""),
                 "path": g.get("path", ""), "via": g.get("via", ""),
                 "rom": g.get("rom"), "platform": g.get("platform"),
                 "launch": list(g.get("launch") or ("none", "")),
                 "art": {"c": cover, "s": src}}
        if ambient:
            entry["art"]["a"] = ambient
        if meta:
            entry["meta"] = meta
        p = g.get("path")
        if p and os.path.isdir(p):
            try:
                entry["files"] = [{"n": e.name, "d": e.is_dir(),
                                   "sz": 0 if e.is_dir() else e.stat().st_size}
                                  for e in sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name))[:10]]
            except OSError:
                pass
        games.append(entry)
        print("    %-12s %s" % (src, g["title"][:52]), flush=True)

    print("\n  artwork: " + ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items()))
          + (" (%d kept from the last scan)" % reused if reused else ""))
    # A cancelled scan has only seen part of the machine, so it is in no
    # position to declare the rest of the library uninstalled.
    apply_to_library(games, prune=not stopped)
    return 0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
