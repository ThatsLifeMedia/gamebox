#!/usr/bin/env python3
"""Lightweight update checker for GameBox.

Asks the GitHub releases API whether a newer GameBox exists. Read-only:
no downloads, no installs, no signature handling. Every failure mode
(no network, rate limit, malformed JSON) returns {"ok": False} and this
module never raises - the check runs on a background thread at startup
and a failed check is simply invisible to the user.
"""

import json
import re
import urllib.request

RELEASES_URL = "https://api.github.com/ThatsLifeMedia/gamebox/releases/latest"
USER_AGENT = "GameBox-Updater/1.0"
TIMEOUT = 10


def parse_version(s):
    """'v1.2.3' -> (1, 2, 3). Tolerant: ignores a leading v and trailing junk."""
    if not s:
        return ()
    core = str(s).split("-")[0].split("+")[0]
    return tuple(int(p) for p in re.findall(r"\d+", core)[:4])


def is_newer(latest, current):
    """True when latest is a strictly newer version than current."""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return (a + (0,) * (n - len(a))) > (b + (0,) * (n - len(b)))


def check(current_version, timeout=TIMEOUT):
    """Ask GitHub for the latest release. Never raises.

    Returns {"ok": True, "update": bool} and, when an update exists,
    {"version", "notes", "url"} for the UI. {"ok": False} on any failure.
    """
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return {"ok": False}
            data = json.loads(resp.read().decode("utf-8", "replace"))
        tag = (data.get("tag_name") or "").strip()
        if not tag or not is_newer(tag, current_version):
            return {"ok": True, "update": False}
        return {
            "ok": True,
            "update": True,
            "version": tag.lstrip("v"),
            "notes": (data.get("body") or "").strip(),
            "url": data.get("html_url")
                   or "https://github.com/ThatsLifeMedia/gamebox/releases",
        }
    except Exception:
        return {"ok": False}
