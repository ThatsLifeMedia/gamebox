#!/usr/bin/env python3
"""
Playtime: watch a launched game, and record how long it ran.

GameBox did no process tracking at all. The only "last played" it had was the
date Steam wrote into its own appmanifest, so a GOG game, an Epic game, a ROM
or a folder game had none - and "Never played" meant "not a Steam game" as
often as it meant never played.

Watching is harder than it sounds, because for most of the library there is no
child process to wait on. Launching a Steam game means firing a steam:// URI,
which returns immediately; the game is started minutes later by a Steam process
that is not ours. So instead of following a process, this follows a *folder*:
any process running from inside the game's install directory is that game.
That is the approach Playnite takes, and it works for every kind of game here.

  track(gid, folder, pid=None, on_finish=...)   start watching
  playing()                                     what is running right now
  stop(gid)                                     forget one game

Pure ctypes against psapi, so this adds no dependency.
"""

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

POLL_SECONDS = 2.0
# How long to wait for a game to appear before giving up. A cold Steam start,
# a shader pre-cache or an anti-cheat service can take minutes.
STARTUP_GRACE = 5 * 60
# A game that swaps between a launcher and its own executable can leave nothing
# running for a moment; do not end the session on the first empty poll.
MISSES_BEFORE_END = 2
# Below this, it was a bounce - a crash on startup, or the wrong executable.
MIN_SESSION_SECONDS = 20

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_lock = threading.Lock()
_watches = {}          # gid -> dict(started, folder, seen, thread)


# ------------------------------------------------------------------ processes
def _process_paths():
    """Every running process's executable path that we are allowed to read."""
    psapi = ctypes.windll.psapi
    kernel32 = ctypes.windll.kernel32
    count = 1024
    while True:
        arr = (wt.DWORD * count)()
        needed = wt.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr),
                                   ctypes.byref(needed)):
            return {}
        if needed.value < ctypes.sizeof(arr):
            break
        count *= 2                     # the array was full; there may be more

    out = {}
    n = needed.value // ctypes.sizeof(wt.DWORD)
    buf = ctypes.create_unicode_buffer(32768)
    for i in range(n):
        pid = arr[i]
        if not pid:
            continue
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue                   # a system process, or one already gone
        try:
            size = wt.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                out[pid] = buf.value
        finally:
            kernel32.CloseHandle(h)
    return out


def _under(path, folder):
    """Is this executable inside the game's install folder?"""
    if not path or not folder:
        return False
    p = os.path.normcase(os.path.normpath(path))
    f = os.path.normcase(os.path.normpath(folder))
    return p.startswith(f + os.sep)


def running_in(folder):
    """The executables currently running from inside a folder."""
    return [p for p in _process_paths().values() if _under(p, folder)]


# -------------------------------------------------------------------- watching
def track(gid, folder, pid=None, on_finish=None, on_start=None):
    """Watch for a game to start in `folder`, then time it until it stops.

    pid  the process we launched, if we launched one. It is watched as well as
         the folder, because a game can re-exec into a child elsewhere, and
         because a launcher hands off and exits.
    """
    stop_flag = threading.Event()
    with _lock:
        old = _watches.get(gid)
        if old:
            old["stop"].set()
        _watches[gid] = {"folder": folder, "stop": stop_flag, "started": None,
                         "last_seen": None, "seen": False, "exe": None}

    def alive(paths):
        if any(_under(p, folder) for p in paths.values()):
            return True
        return bool(pid and pid in paths)

    def run():
        began = time.time()
        misses = 0
        while not stop_flag.is_set():
            paths = _process_paths()
            here = alive(paths)
            with _lock:
                w = _watches.get(gid)
                if w is None or w["stop"] is not stop_flag:
                    return                       # superseded by a newer launch
                if here and not w["seen"]:
                    w["seen"] = True
                    w["started"] = w["last_seen"] = time.time()
                    w["exe"] = next((p for p in paths.values() if _under(p, folder)), None)
                    if on_start:
                        on_start(gid)
                elif here:
                    w["last_seen"] = time.time()
                    misses = 0
                elif w["seen"]:
                    misses += 1
                    if misses >= MISSES_BEFORE_END:
                        # Time to the last poll that saw it, not to now: the
                        # grace polls that confirm it really stopped are not
                        # part of the session.
                        seconds = int(w["last_seen"] - w["started"])
                        cur = _watches.get(gid)
                        if cur is not None and cur["stop"] is stop_flag:
                            _watches.pop(gid, None)
                        if on_finish and seconds >= MIN_SESSION_SECONDS:
                            on_finish(gid, seconds)
                        return
                elif time.time() - began > STARTUP_GRACE:
                    # it never started - a failed launch, or the user cancelled
                    cur = _watches.get(gid)
                    if cur is not None and cur["stop"] is stop_flag:
                        _watches.pop(gid, None)
                    return
            stop_flag.wait(POLL_SECONDS)
        with _lock:
            w = _watches.get(gid)
            if w and w["stop"] is stop_flag:
                _watches.pop(gid, None)
                # a session interrupted by the app closing still counts
                if w["seen"] and on_finish:
                    seconds = int((w["last_seen"] or w["started"]) - w["started"])
                    if seconds >= MIN_SESSION_SECONDS:
                        on_finish(gid, seconds)

    t = threading.Thread(target=run, daemon=True, name="playtime-" + str(gid))
    t.start()
    return t


def playing():
    """{game id: seconds so far} for the games running right now."""
    now = time.time()
    with _lock:
        return {gid: int(now - w["started"])
                for gid, w in _watches.items() if w["seen"]}


def waiting():
    """Games that have been launched but have not appeared yet."""
    with _lock:
        return [gid for gid, w in _watches.items() if not w["seen"]]


def stop(gid=None):
    """Stop watching one game, or all of them."""
    with _lock:
        targets = [gid] if gid else list(_watches)
        for g in targets:
            w = _watches.get(g)
            if w:
                w["stop"].set()
