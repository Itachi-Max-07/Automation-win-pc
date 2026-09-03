#!/usr/bin/env python3
"""gate.py: measured verification gate for the pc-control skill.

Safe by default: no keystrokes are injected and nothing destructive runs.
System volume, brightness and the clipboard are saved and restored. Pass
--live to add a real keystroke round trip through Notepad. Pass --quiet to
skip the audible speaker tones.

Exit 0 only if zero FAILs. Prints an honest NOT COVERED list: a green gate
is proof of exactly what ran, nothing more.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.join(HERE, "pc.py")
SKILL = os.path.dirname(HERE)

results = []
not_covered = []


def record(status, name, detail=""):
    results.append((status, name, detail))
    mark = {"PASS": "ok", "FAIL": "FAIL", "SKIP": "skip"}[status]
    print("[%s] %s%s" % (mark, name, "  (%s)" % detail if detail else ""))


def pc(*args, **kw):
    timeout = kw.pop("timeout", 90)
    p = subprocess.run([sys.executable, PC, "--json", *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    try:
        data = json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
    return p.returncode, data, p.stderr


def check(name, cond, detail=""):
    record("PASS" if cond else "FAIL", name, detail)


def plan_ok(data):
    """A dry run must describe a balanced, fully resolved key sequence."""
    plan, events = data.get("input_plan", ""), data.get("events", 0)
    if not plan or not events:
        return False
    steps = plan.split(", ")
    if len(steps) != events:
        return False
    downs = [s for s in steps if s.endswith(" down")]
    ups = [s for s in steps if s.endswith(" up")]
    return len(downs) == len(ups) == events // 2


def clip_get():
    return pc("clipboard", "get")[1].get("text", "")


def clip_set(text):
    return pc("clipboard", "set", text)[0] == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="include the Notepad keystroke round trip")
    ap.add_argument("--quiet", action="store_true",
                    help="skip audible speaker tones")
    a = ap.parse_args()

    # -- doctor and structure
    code, d, _ = pc("doctor")
    check("doctor runs and returns json", code == 0 and d.get("ok"))
    checks = d.get("checks", {})
    injection_granted = checks.get("input_injection", False)
    check("doctor separates optional packages from blocked capabilities",
          isinstance(d.get("optional"), dict)
          and "pycaw" not in d.get("blocked", []))

    # -- battery
    code, d, _ = pc("battery")
    check("battery reports percent 0-100",
          code == 0 and 0 <= d.get("percent", -1) <= 100)

    # -- volume (state saved and restored)
    code, d, _ = pc("volume", "get")
    vol0, muted0 = d.get("volume"), d.get("muted")
    check("volume get returns 0-100", code == 0 and 0 <= (vol0 or -1) <= 100)
    code, d, _ = pc("volume", "set", str(vol0))
    check("volume set (same value) succeeds", code == 0)
    code, d, _ = pc("volume", "set", "150")
    check("volume set 150 is rejected", code == 1)
    code, d, _ = pc("volume", "get")
    check("volume unchanged after gate", d.get("volume") == vol0)
    check("mute state unchanged after gate", d.get("muted") == muted0)

    # -- brightness (state saved and restored: setting it really works here)
    code, d, err = pc("brightness", "get")
    bright0 = d.get("brightness")
    check("brightness get: works or fails with guidance",
          code == 0 or "brightness" in (d.get("error", "") + err).lower())
    if code == 0:
        code2, d2, _ = pc("brightness", "set", str(bright0))
        check("brightness set (same value) succeeds", code2 == 0)
        code3, d3, _ = pc("brightness", "up", "2", "--dry-run")
        check("brightness up dry-run reports a target without changing it",
              code3 == 0 and d3.get("would_set") is not None
              and pc("brightness", "get")[1].get("brightness") == bright0)
        code4, d4, _ = pc("brightness", "set", "0.5")
        check("fractional brightness rejected, not silently applied as 0%",
              code4 == 1)
        pc("brightness", "set", str(bright0))
        check("brightness restored after gate",
              pc("brightness", "get")[1].get("brightness") == bright0)
    else:
        record("SKIP", "brightness set/restore", "no brightness API here")
        not_covered.append("brightness set (no WMI or DDC/CI on this display)")
    code, d, _ = pc("brightness", "up", "-3", "--dry-run")
    check("brightness up -3 rejected cleanly (no traceback)",
          code in (1, 2) and isinstance(d, dict) and d.get("ok") is False)
    code, d, _ = pc("brightness", "up", "0", "--dry-run")
    check("brightness up 0 rejected, not defaulted to 3",
          code in (1, 2) and d.get("ok") is False)

    # -- ip
    code, d, _ = pc("ip")
    check("ip reports a local address", code == 0 and bool(d.get("local")))
    code, d, _ = pc("ip", "--public")
    if d.get("public"):
        check("ip --public resolves", True)
    else:
        record("SKIP", "ip --public", "network unavailable")

    # -- apps
    code, d, _ = pc("apps")
    check("apps lists visible apps", code == 0 and d.get("count", 0) > 0)
    code, d, _ = pc("app", "front")
    check("app front returns a name", code == 0 and bool(d.get("frontmost")))
    code, d, _ = pc("app", "running", "explorer")
    check("app running explorer is true",
          code == 0 and d.get("running") is True)
    code, d, _ = pc("app", "running", "NoSuchAppQzx")
    check("app running (absent) is false",
          code == 0 and d.get("running") is False)
    code, d, _ = pc("app", "open")
    check("app open without name is a usage error (exit 2)", code == 2)
    code, d, _ = pc("app", "running", 'x" & "y')
    check("quoted app name treated as literal data",
          code == 0 and d.get("running") is False)
    code, d, _ = pc("app", "open", "NoSuchAppQzx")
    check("app open (absent) fails with the resolution chain explained",
          code == 1 and "Start Menu" in d.get("error", ""))

    # -- clipboard (saved and restored). pc.py is its own tool here: the
    # Windows alternatives (clip.exe, Get-Clipboard) corrupt whitespace.
    clip0 = clip_get()
    token = "pc-gate-3f9a"
    ok_set = clip_set(token)
    code2, d2, _ = pc("clipboard", "get")
    check("clipboard set/get round trip",
          ok_set and code2 == 0 and d2.get("text") == token)
    clip_set("line with newline\n")
    check("clipboard get preserves trailing newline",
          clip_get() == "line with newline\n")
    # Regression lock: a 32 bit restype truncates the clipboard handle on
    # win64 and segfaults, and cp1252 stdout raises on non ASCII text.
    unicode_probe = "café ◑ 日本語"
    clip_set(unicode_probe)
    check("clipboard survives non-ASCII (handle and encoding regression)",
          clip_get() == unicode_probe)
    plain = subprocess.run([sys.executable, PC, "clipboard", "get"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    check("plaintext output of non-ASCII does not crash",
          plain.returncode == 0 and "Traceback" not in plain.stderr)
    plain = subprocess.run([sys.executable, PC, "apps"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    check("apps plaintext output does not crash on window titles",
          plain.returncode == 0 and "Traceback" not in plain.stderr)
    clip_set(clip0)
    check("clipboard restored after gate", clip_get() == clip0)

    # -- screenshot, both the PIL path and the stdlib GDI fallback
    tmp = os.path.join(tempfile.gettempdir(), "pc-gate-%d.png" % os.getpid())
    code, d, _ = pc("screenshot", tmp)
    if code == 0:
        check("screenshot writes a nonzero png", os.path.getsize(tmp) > 0)
        check("screenshot png has a valid signature",
              open(tmp, "rb").read(8) == b"\x89PNG\r\n\x1a\n")
        os.unlink(tmp)
    else:
        record("FAIL", "screenshot", d.get("error", "?"))
    gdi = os.path.join(tempfile.gettempdir(), "pc-gate-gdi-%d.png" % os.getpid())
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import pc; "
         "print(pc.screenshot_gdi(r'%s'))" % (HERE, gdi)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if probe.returncode == 0 and os.path.exists(gdi):
        check("stdlib GDI screenshot path writes a valid png",
              os.path.getsize(gdi) > 0
              and open(gdi, "rb").read(8) == b"\x89PNG\r\n\x1a\n")
        os.unlink(gdi)
    else:
        record("FAIL", "stdlib GDI screenshot path",
               (probe.stderr or "").strip().splitlines()[-1:] or "?")

    # -- keystroke engine: dry-run every mapped combo
    combos = ["ctrl+t", "ctrl+shift+t", "ctrl+tab", "ctrl+plus", "space",
              "shift+n", "ctrl+left", "f11", "win+d", "k"]
    all_ok = True
    for c in combos:
        code, d, _ = pc("keys", c, "--dry-run")
        if code != 0 or not plan_ok(d):
            all_ok = False
            record("FAIL", "keys dry-run %s" % c, str(d.get("input_plan")))
    check("keys: %d combos resolve to balanced key plans" % len(combos),
          all_ok)
    code, d, _ = pc("keys", "ctrl+nosuchkey", "--dry-run")
    check("keys: unknown key rejected", code == 1)
    code, d, _ = pc("keys", "badmod+t", "--dry-run")
    check("keys: unknown modifier rejected", code == 1)
    code, d, _ = pc("keys", "cmd+t", "--dry-run")
    check("keys: macOS cmd rejected with a pointer to ctrl",
          code == 1 and "ctrl" in d.get("error", ""))
    code, d, _ = pc("keys", "brightness-up", "--dry-run")
    check("keys: brightness keys rejected with a pointer to the verb",
          code == 1 and "brightness up" in d.get("error", ""))
    code, d, _ = pc("keys", "ctrl++", "--dry-run")
    check("keys: malformed combo rejected", code == 1)

    code, d, _ = pc("type", 'he said "hi" \\ done', "--dry-run")
    check("type: quotes and backslashes plan cleanly",
          code == 0 and d.get("chars") == 19)
    code, d, _ = pc("type", "café 日本", "--dry-run")
    check("type: non-ASCII text plans as unicode input",
          code == 0 and d.get("chars") == 7)
    code, d, _ = pc("type", "", "--dry-run")
    check("type empty string rejected", code == 1)

    # -- browser and yt maps: every action resolves
    for family, actions in [
        ("browser", ["new-tab", "close-tab", "reopen-tab", "next-tab",
                     "prev-tab", "new-window", "private-window", "zoom-in",
                     "zoom-out", "zoom-reset", "refresh", "hard-refresh",
                     "back", "forward", "history", "bookmarks", "address-bar",
                     "find", "devtools", "fullscreen"]),
        ("yt", ["play-pause", "mute", "fullscreen", "theater", "miniplayer",
                "captions", "vol-up", "vol-down", "back-5", "fwd-5",
                "back-10", "fwd-10", "prev-frame", "next-frame", "speed-up",
                "speed-down", "start", "end", "prev-chapter", "next-chapter",
                "next-video", "prev-video"]),
    ]:
        all_ok = True
        for act in actions:
            code, d, _ = pc(family, act, "--dry-run")
            if code != 0 or not plan_ok(d):
                all_ok = False
                record("FAIL", "%s %s" % (family, act),
                       str(d.get("input_plan")))
        check("%s: all %d actions resolve" % (family, len(actions)), all_ok)
        code, d, _ = pc(family, "no-such-action", "--dry-run")
        check("%s: unknown action rejected" % family, code == 1)

    # -- media
    code, d, _ = pc("media", "status")
    check("media status runs", code == 0)
    code, d, _ = pc("media", "play-pause", "--dry-run")
    check("media play-pause dry-run uses a media key",
          code == 0 and d.get("method") == "media-key" and plan_ok(d))
    code, d, _ = pc("media", "pause", "--dry-run")
    check("media pause dry-run uses an absolute appcommand",
          code == 0 and d.get("method") == "appcommand"
          and "WM_APPCOMMAND" in d.get("input_plan", ""))

    # -- web resolution
    code, d, _ = pc("web", "youtube", "--dry-run")
    check("web: cache hit resolves youtube",
          code == 0 and "youtube.com" in d.get("url", ""))
    code, d, _ = pc("web", "example.com", "--dry-run")
    check("web: bare domain becomes https url",
          code == 0 and d.get("url") == "https://example.com")
    code, d, _ = pc("web", "zzqx qkjw vvnn", "--dry-run")
    check("web: gibberish falls back to a search url",
          code == 0 and "google.com/search" in d.get("url", ""))
    code, d, _ = pc("web", "HTTP://EXAMPLE.ORG", "--dry-run")
    check("uppercase scheme not double-prefixed",
          code == 0 and d.get("url", "").lower().count("http") == 1)
    with open(os.path.join(SKILL, "assets", "websites.json"), "rb") as f:
        cache0 = f.read()
    pc("web", "example", "--dry-run")
    with open(os.path.join(SKILL, "assets", "websites.json"), "rb") as f:
        check("web dry-run does not mutate the site cache", f.read() == cache0)

    # -- youtube resolution (network)
    code, d, _ = pc("youtube", "play", "lo-fi beats", "--dry-run")
    if code == 0 and "youtube.com" in d.get("url", ""):
        check("youtube play resolves a url", True, d.get("resolved_via", ""))
    else:
        record("SKIP", "youtube play", "network unavailable")

    # -- speaker test
    if a.quiet:
        record("SKIP", "speaker-test", "--quiet")
        not_covered.append("speaker tone playback (--quiet)")
    else:
        pc("volume", "set", "15")
        code, d, _ = pc("speaker-test", "--duration", "0.15", timeout=90)
        pc("volume", "set", str(vol0))
        played = all(t.get("played") for t in d.get("tones", []))
        check("speaker-test plays all 5 tones",
              code == 0 and played and len(d.get("tones", [])) == 5)
        check("speaker-test admits it is unmeasured",
              "playback only" in d.get("verdict", ""))

    # -- mic test (privacy setting dependent)
    code, d, _ = pc("mic-test", "--seconds", "2", timeout=90)
    if code == 0:
        check("mic-test returns metrics", "verdict" in d)
    else:
        record("SKIP", "mic-test", "no input device or privacy setting off")
        not_covered.append("mic capture (device or privacy setting)")

    # -- negative and contract checks
    p = subprocess.run([sys.executable, PC, "no-such-cmd"],
                       capture_output=True, text=True)
    check("unknown command exits 2", p.returncode == 2)
    code, d, _ = pc("volume", "set")
    check("volume set without value is a usage error (exit 2)", code == 2)
    code, d, _ = pc("clipboard", "set")
    check("clipboard set without text is a usage error (exit 2)", code == 2)
    for c in ["battery", "doctor"]:
        code, d, _ = pc(c)
        check("--json %s parses" % c, isinstance(d, dict) and "ok" in d)

    p = subprocess.run([sys.executable, PC, "battery", "--json"],
                       capture_output=True, text=True)
    try:
        trailing = json.loads(p.stdout).get("ok") is True
    except json.JSONDecodeError:
        trailing = False
    check("trailing --json accepted (documented syntax)",
          p.returncode == 0 and trailing)

    # -- no em or en dashes anywhere in the skill
    dirty = []
    for root, _, files in os.walk(SKILL):
        if ".git" in root:
            continue
        for f in files:
            path = os.path.join(root, f)
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            # Escapes, not literals: this file must not trip its own check.
            if "\u2014" in text or "\u2013" in text:
                dirty.append(f)
    check("no em/en dashes in any skill file", not dirty, ", ".join(dirty))

    # -- live keystroke round trip
    if a.live and injection_granted:
        clip0 = clip_get()
        target = os.path.join(tempfile.gettempdir(), "pc-gate-live.txt")
        token = "pc-gate-live-7x"
        was_running = pc("app", "running", "notepad")[1].get("running")
        try:
            open(target, "w", encoding="utf-8").close()
            subprocess.Popen(["notepad.exe", target])
            time.sleep(2.0)
            pc("app", "activate", "notepad")
            time.sleep(1.0)
            front = pc("app", "front")[1].get("frontmost")
            if front != "notepad":
                record("FAIL", "LIVE: could not focus Notepad", str(front))
            else:
                pc("type", token)
                time.sleep(0.6)
                pc("keys", "ctrl+a")
                pc("keys", "ctrl+c")
                time.sleep(0.6)
                check("LIVE: keystrokes reached Notepad",
                      clip_get().strip() == token)
                # leave no unsaved buffer behind, so nothing prompts
                pc("keys", "ctrl+a")
                pc("type", " ")
                pc("keys", "ctrl+s")
                time.sleep(0.8)
        finally:
            if not was_running:
                pc("app", "quit", "notepad")
            clip_set(clip0)
            if os.path.exists(target):
                os.unlink(target)
    elif a.live:
        record("SKIP", "live keystroke round trip", "input injection blocked")
        not_covered.append("live keystroke injection (UIPI)")
    else:
        not_covered.append("live keystroke injection (run gate.py --live)")

    if not injection_granted:
        not_covered.append("browser/yt/keys/type live execution (input "
                           "injection is blocked in this session)")
    not_covered.append("screenshot image content (only validity is checked)")
    not_covered.append("toast visibility (Focus Assist suppresses silently)")
    if not checks.get("brightness_wmi"):
        not_covered.append("WMI brightness (this display uses another path)")
    else:
        not_covered.append("external monitor DDC/CI brightness (this machine "
                           "answered on WMI)")
    not_covered.append("elevated windows (input injection is refused across "
                       "the UIPI boundary unless the agent is elevated)")

    # -- summary
    npass = sum(1 for s, _, _ in results if s == "PASS")
    nfail = sum(1 for s, _, _ in results if s == "FAIL")
    nskip = sum(1 for s, _, _ in results if s == "SKIP")
    print("\nGATE: %d passed, %d failed, %d skipped" % (npass, nfail, nskip))
    print("NOT COVERED by this run:")
    for item in not_covered:
        print("  - %s" % item)
    sys.exit(1 if nfail else 0)


if __name__ == "__main__":
    main()
