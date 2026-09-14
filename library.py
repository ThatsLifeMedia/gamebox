#!/usr/bin/env python3
"""
GameBox library store
=====================

The library used to be a `const G=[...]` array spliced into GameBox.html by a
regex, keyed by scan-order integers. That meant three things, all bad:

  * a rescan renumbered every game, so the favourites and hidden lists in
    localStorage silently re-pointed at *different* games;
  * a rescan overwrote the file, so there was nowhere to keep anything the user
    had done - a tag, a note, a playtime;
  * every cover rode along as a base64 data: URI, so the page was 3.7 MB and
    could still be parsing when the bridge announced itself.

So the library now lives in library.json next to the exe, keyed by a *derived*
id that survives a rescan, and the page is just code again.

  library.json     {"version": 2, "scanned": "...", "games": [...]}
  art/<id>.jpg     covers, heroes and screenshots, as files
  library-backups/ a timestamped copy taken before every merge

A game record splits into three parts, and the split is the whole point:

  scanner-owned   title, store, path, bytes, installed, launch, art, meta, files
                  - refreshed on every scan
  "user"          favorite, hidden, tags, categories, completion, score, notes,
                  and title/cover overrides - the scanner never touches these
  "play"          seconds, count, last, sessions - written by the play tracker
"""

import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

VERSION = 2
KEEP_BACKUPS = 10
MAX_SESSIONS = 200

# Fields the scanner owns. A merge overwrites these and nothing else.
SCANNED_FIELDS = ("title", "store", "path", "bytes", "installed", "updated",
                  "played", "launch", "art", "meta", "files", "via", "state",
                  # which file inside the folder is the game, and which console
                  # it is for - both needed to offer a different emulator later
                  "rom", "platform",
                  # the folder's mtime when its size was last measured, so the
                  # next scan can skip re-walking a folder nothing has touched
                  "mtime")


def app_dir():
    """The folder the app writes to: next to the exe, or next to the sources."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def path_in_app(*parts):
    return os.path.join(app_dir(), *parts)


def library_file():
    return path_in_app("library.json")


def art_dir():
    return path_in_app("art")


def backup_dir():
    return path_in_app("library-backups")


# ------------------------------------------------------------------------ ids
def norm_path(path):
    """One spelling for an install path: backslashes, no trailing one, lower."""
    return (path or "").replace("/", chr(92)).rstrip(chr(92)).lower()


def game_id(store, key=None, path=None):
    """A stable id for a game, so a rescan recognises it as the same game.

    Store entries key off the store's own identifier, which never changes.
    Everything else keys off the install path, hashed so the id stays short and
    safe to use as a filename.
    """
    if key:
        return "%s:%s" % (store, str(key).strip())
    norm = norm_path(path)
    return "path:" + hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:12]


def art_name(gid, suffix=""):
    """A game id as a filename. ':' is legal in an id and illegal in a path,
    and store-derived ids can carry other separators too, so keep only the
    safe set."""
    return re.sub(r"[^a-z0-9_-]", "-", gid, flags=re.I) + suffix


# -------------------------------------------------------------------- records
def blank_user():
    return {"favorite": False, "hidden": False, "tags": [], "categories": [],
            "completion": None, "score": None, "notes": "",
            "title": None, "cover": None, "emulator": None}


def blank_play():
    return {"seconds": 0, "count": 0, "last": None, "sessions": []}


def new_record(scanned):
    """A fresh record: everything the scanner found, plus empty user state."""
    rec = {"id": scanned["id"], "source": scanned.get("source", "scan"),
           "added": time.strftime("%b %d, %Y"),
           "user": blank_user(), "play": blank_play()}
    for f in SCANNED_FIELDS:
        if f in scanned:
            rec[f] = scanned[f]
    rec.setdefault("installed", True)
    return rec


# ------------------------------------------------------------------ load/save
def load():
    """The library on disk, or an empty one. Never raises."""
    try:
        # a UTF-8 BOM breaks json.load, and Windows editors write them
        with io.open(library_file(), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("games"), list):
            for g in data["games"]:
                g.setdefault("user", blank_user())
                g.setdefault("play", blank_play())
                # a library written by an older build can be missing new keys
                for k, v in blank_user().items():
                    g["user"].setdefault(k, v)
                for k, v in blank_play().items():
                    g["play"].setdefault(k, v)
            data.setdefault("exclusions", [])
            return data
    except Exception:
        pass
    return {"version": VERSION, "scanned": "", "games": [], "exclusions": []}


def backup(reason="merge"):
    """A timestamped copy of the library, so a bad scan is one file to undo."""
    src = library_file()
    if not os.path.exists(src):
        return None
    try:
        os.makedirs(backup_dir(), exist_ok=True)
        dst = os.path.join(backup_dir(), "library-%s-%s.json" %
                           (time.strftime("%Y%m%d-%H%M%S"), reason))
        shutil.copyfile(src, dst)
        old = sorted(f for f in os.listdir(backup_dir())
                     if f.startswith("library-") and f.endswith(".json"))
        for f in old[:-KEEP_BACKUPS]:
            try:
                os.remove(os.path.join(backup_dir(), f))
            except OSError:
                pass
        return dst
    except Exception:
        return None


def save(data):
    """Write the library atomically - a half-written library is a lost one."""
    data["version"] = VERSION
    tmp = library_file() + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, library_file())
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ------------------------------------------------------------------ the merge
def merge(scanned, data=None, prune=True):
    """Fold a scan into the library without disturbing anything the user owns.

    scanned  records carrying an "id" and the scanner-owned fields
    prune    mark games the scan did not find as not installed, rather than
             deleting them - an uninstalled game keeps its tags and playtime

    Returns (data, added, updated, missing, rekeyed).
    """
    data = data if data is not None else load()
    excluded = set(data.get("exclusions") or [])
    by_id = {g["id"]: g for g in data["games"]}
    # A secondary index on the install path, because an id can legitimately
    # improve. A game migrated before its launch target was known is keyed off
    # its path; the scanner, which knows the appid, will offer "steam:620" for
    # the same folder. Matching on the path lets us re-key it instead of
    # importing a second copy of a game the user has already tagged.
    by_path = {}
    for g in data["games"]:
        p = norm_path(g.get("path"))
        if p:
            by_path.setdefault(p, g)
    seen = set()
    claimed = set()          # id() of records this pass has already matched
    added = updated = rekeyed = 0

    for s in scanned:
        gid = s["id"]
        if gid in excluded:
            continue
        cur = by_id.get(gid)
        if cur is None:
            p = norm_path(s.get("path"))
            match = by_path.get(p) if p else None
            # only adopt a record this pass has not already claimed
            if match is not None and id(match) not in claimed and match["id"] not in excluded:
                by_id.pop(match["id"], None)
                match["id"] = gid
                by_id[gid] = match
                cur = match
                rekeyed += 1
        seen.add(gid)
        if cur is not None:
            claimed.add(id(cur))
        if cur is None:
            data["games"].append(new_record(s))
            by_id[gid] = data["games"][-1]
            claimed.add(id(data["games"][-1]))
            added += 1
            continue
        for f in SCANNED_FIELDS:
            if f in s:
                cur[f] = s[f]
        cur["installed"] = True
        updated += 1

    missing = 0
    if prune:
        for g in data["games"]:
            # a hand-added game is not the scanner's to disown
            if (g["id"] not in seen and g.get("source") != "manual"
                    and g.get("installed", True)):
                g["installed"] = False
                missing += 1

    data["scanned"] = time.strftime("%b %d, %Y")
    return data, added, updated, missing, rekeyed


# -------------------------------------------------------------- play sessions
def record_session(gid, seconds, data=None):
    """Add a finished play session to a game. Returns the updated record."""
    data = data if data is not None else load()
    g = next((x for x in data["games"] if x["id"] == gid), None)
    if g is None or seconds <= 0:
        return None
    p = g["play"]
    p["seconds"] = int(p.get("seconds", 0)) + int(seconds)
    p["count"] = int(p.get("count", 0)) + 1
    p["last"] = time.strftime("%b %d, %Y")
    p["sessions"] = (p.get("sessions") or [])[-(MAX_SESSIONS - 1):]
    p["sessions"].append({"at": int(time.time()), "seconds": int(seconds)})
    save(data)
    return g


# -------------------------------------------------------------- the page shape
def to_page(data):
    """The library in the compact shape the page renders.

    The page has always spoken short keys - t, s, gb, art.c - and there is no
    reason to churn every template to rename them. This is the only place that
    mapping lives.
    """
    out = []
    for g in data.get("games", []):
        u = g.get("user") or {}
        p = g.get("play") or {}
        art = dict(g.get("art") or {})
        if u.get("cover"):
            art["c"] = u["cover"]
        gb = round((g.get("bytes") or 0) / (1024 ** 3), 1)
        title = u.get("title") or g.get("title") or "?"
        out.append({
            "id": g["id"], "t": title, "short": title[:34],
            "s": g.get("store", "local"), "gb": gb,
            "upd": g.get("updated") or "-",
            "played": p.get("last") or g.get("played"),
            "state": g.get("state") or "",
            "rate": "PC", "path": g.get("path", ""), "via": g.get("via", ""),
            "note": u.get("notes", ""), "art": art, "meta": g.get("meta"),
            "files": g.get("files"), "added": g.get("added", ""),
            "installed": g.get("installed", True),
            "fav": bool(u.get("favorite")), "hide": bool(u.get("hidden")),
            "tags": u.get("tags") or [], "cats": u.get("categories") or [],
            "done": u.get("completion"), "score": u.get("score"),
            "secs": int(p.get("seconds") or 0), "plays": int(p.get("count") or 0),
        })
    return out


def launch_target(gid, data=None):
    """(kind, target, folder) for a game, or None."""
    data = data if data is not None else load()
    g = next((x for x in data["games"] if x["id"] == gid), None)
    if not g:
        return None
    la = g.get("launch")
    if not la:
        return None
    kind = la[0]
    target = la[1] if len(la) > 1 else ""
    folder = la[2] if len(la) > 2 else os.path.dirname(target)
    return kind, target, folder
