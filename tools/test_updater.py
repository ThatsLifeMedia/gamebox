#!/usr/bin/env python3
"""
Proves the update checker compares versions correctly and fails silently.

Covers parse_version / is_newer plus check() against stubbed HTTP
responses: a newer release, the current release, a 403 (rate limit),
malformed JSON, and a dead network. check() must never raise.

  python tools/test_updater.py
"""

import io
import json
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import updater  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (" - " + detail if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeResp:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def stub_urlopen(resp_or_exc):
    real = updater.urllib.request.urlopen

    def fake(req, timeout=None):
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    updater.urllib.request.urlopen = fake
    return real


def release(tag, body="notes here"):
    return FakeResp(200, json.dumps({
        "tag_name": tag,
        "body": body,
        "html_url": "https://github.com/That-s-Life-Media/gamebox/releases/tag/" + tag,
    }).encode("utf-8"))


def main():
    print("parse_version")
    check("v prefix stripped", updater.parse_version("v1.2.3") == (1, 2, 3))
    check("no prefix", updater.parse_version("1.3") == (1, 3))
    check("prerelease suffix ignored", updater.parse_version("2.0.0-beta") == (2, 0, 0))
    check("empty -> ()", updater.parse_version("") == ())
    check("junk -> ()", updater.parse_version("latest") == ())

    print("\nis_newer")
    check("newer patch", updater.is_newer("v1.3.1", "1.3.0"))
    check("newer minor", updater.is_newer("v1.4.0", "1.3.0"))
    check("newer major", updater.is_newer("2.0", "1.9.9"))
    check("10 > 9 numerically", updater.is_newer("v1.10.0", "v1.9.0"))
    check("same is not newer", not updater.is_newer("1.3.0", "1.3.0"))
    check("older is not newer", not updater.is_newer("1.3.0", "1.4.0"))
    check("1.3 == 1.3.0", not updater.is_newer("1.3", "1.3.0"))
    check("unparseable latest", not updater.is_newer("", "1.3.0"))

    print("\ncheck() against stubbed HTTP")
    real = stub_urlopen(release("v1.4.0", "## New\n- stuff"))
    try:
        r = updater.check("1.3.0")
        check("update detected", r.get("ok") and r.get("update") is True)
        check("version parsed", r.get("version") == "1.4.0", repr(r.get("version")))
        check("notes passed through", r.get("notes") == "## New\n- stuff")
        check("url passed through", r.get("url", "").endswith("/tag/v1.4.0"))
    finally:
        updater.urllib.request.urlopen = real

    real = stub_urlopen(release("v1.3.0"))
    try:
        r = updater.check("1.3.0")
        check("no update when current", r.get("ok") and r.get("update") is False)
    finally:
        updater.urllib.request.urlopen = real

    real = stub_urlopen(FakeResp(403, b"rate limited"))
    try:
        r = updater.check("1.3.0")
        check("403 fails silently", r == {"ok": False}, repr(r))
    finally:
        updater.urllib.request.urlopen = real

    real = stub_urlopen(FakeResp(200, b"not json{"))
    try:
        r = updater.check("1.3.0")
        check("malformed JSON fails silently", r == {"ok": False}, repr(r))
    finally:
        updater.urllib.request.urlopen = real

    real = stub_urlopen(urllib.error.URLError("no route to host"))
    try:
        r = updater.check("1.3.0")
        check("dead network fails silently", r == {"ok": False}, repr(r))
    finally:
        updater.urllib.request.urlopen = real

    n = len([l for l in open(__file__, encoding="utf-8") if "    check(" in l])
    print("\n%d checks, %d failed" % (n, len(FAILED)))
    if FAILED:
        print("failed: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
