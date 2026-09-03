#!/usr/bin/env python3
"""pc.py: local machine hands for agents. Windows backend, stdlib only.

Every command is non-interactive, takes arguments, exits 0 on success,
1 on runtime failure, 2 on usage error. Add --json for machine-readable
output. Keystroke-emitting commands accept --dry-run to print the
injection plan instead of executing it.

Third party packages are never required. PIL, pycaw and winsdk are used
automatically when they happen to be importable, and every verb has a
stdlib path that works without them.
"""

import argparse
import csv
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import math
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import wave
import zlib

if platform.system() != "Windows":
    print("error: this backend supports Windows only", file=sys.stderr)
    sys.exit(1)

import winreg

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITES_PATH = os.path.join(SKILL_DIR, "assets", "websites.json")

CREATE_NO_WINDOW = 0x08000000

UIPI_FIX = (
    "Input injection was refused by Windows. The usual cause is UIPI: a "
    "non-elevated process cannot send input to an elevated window. Fix: "
    "click a non-elevated window first, or restart the agent as "
    "administrator to drive elevated apps."
)


def run(cmd, timeout=30, **kw):
    kw.setdefault("creationflags", CREATE_NO_WINDOW)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", **kw)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def pwsh_exe():
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    fallback = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "WindowsPowerShell", "v1.0",
                            "powershell.exe")
    return fallback if os.path.exists(fallback) else None


def pwsh(script, timeout=30):
    """Run a PowerShell snippet with UTF-8 output and no profile."""
    exe = pwsh_exe()
    if not exe:
        return 1, "", "powershell was not found on this system"
    prelude = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    return run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-Command", prelude + script], timeout=timeout)


def ps_str(s):
    """Escape a value for embedding in a PowerShell single quoted literal."""
    return str(s).replace("'", "''")


def xml_str(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def fail(msg, extra=None):
    out = {"ok": False, "error": msg}
    if extra:
        out.update(extra)
    return out


def usage_fail(msg, extra=None):
    out = fail(msg, extra)
    out["_exit"] = 2
    return out


def ok(data):
    out = {"ok": True}
    out.update(data)
    return out


# ---------- win32 prelude ----------
# Every handle or pointer returning call gets an explicit restype. Without
# it ctypes assumes c_int and silently truncates 64 bit handles, which
# segfaults the process rather than failing cleanly.

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)
ole = ctypes.oledll.ole32

LRESULT = ctypes.c_ssize_t

u32.OpenClipboard.argtypes = [ctypes.c_void_p]
u32.OpenClipboard.restype = wt.BOOL
u32.GetClipboardData.argtypes = [ctypes.c_uint]
u32.GetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
u32.IsClipboardFormatAvailable.restype = wt.BOOL
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
u32.SetForegroundWindow.restype = wt.BOOL
u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.ShowWindow.restype = wt.BOOL
u32.IsIconic.argtypes = [ctypes.c_void_p]
u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
u32.IsWindowVisible.restype = wt.BOOL
u32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
u32.GetWindowTextW.argtypes = [ctypes.c_void_p, wt.LPWSTR, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(wt.DWORD)]
u32.GetWindowThreadProcessId.restype = wt.DWORD
u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.GetWindowLongW.restype = wt.DWORD
u32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
u32.BringWindowToTop.argtypes = [ctypes.c_void_p]
u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_void_p, LRESULT]
u32.PostMessageW.restype = wt.BOOL
u32.SendNotifyMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, LRESULT]
u32.SendNotifyMessageW.restype = wt.BOOL
u32.GetDC.argtypes = [ctypes.c_void_p]
u32.GetDC.restype = ctypes.c_void_p
u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u32.GetSystemMetrics.argtypes = [ctypes.c_int]
u32.GetSystemMetrics.restype = ctypes.c_int

k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalFree.argtypes = [ctypes.c_void_p]
k32.GlobalFree.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = ctypes.c_void_p
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD,
                                           wt.LPWSTR,
                                           ctypes.POINTER(wt.DWORD)]
k32.GetCurrentThreadId.restype = wt.DWORD

gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi.CreateCompatibleDC.restype = ctypes.c_void_p
gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_int]
gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi.SelectObject.restype = ctypes.c_void_p
gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                       ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi.BitBlt.restype = wt.BOOL
gdi.DeleteObject.argtypes = [ctypes.c_void_p]
gdi.DeleteDC.argtypes = [ctypes.c_void_p]

winmm.PlaySoundW.argtypes = [wt.LPCWSTR, ctypes.c_void_p, wt.DWORD]
winmm.PlaySoundW.restype = wt.BOOL

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WM_CLOSE = 0x0010
WM_APPCOMMAND = 0x0319
HWND_BROADCAST = ctypes.c_void_p(0xFFFF)
SW_RESTORE = 9
SW_SHOW = 5
SRCCOPY = 0x00CC0020
SND_SYNC = 0x0000
SND_FILENAME = 0x00020000
SND_NODEFAULT = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        ole.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(self))


def com_method(ptr, slot, restype, *argtypes):
    """Bind vtable slot `slot` on a raw COM interface pointer."""
    vtable = ctypes.cast(ptr, ctypes.POINTER(
        ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[slot])


# ---------- keystroke engine ----------

VK = {
    "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "return": 0x0D, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "forward-delete": 0x2E, "insert": 0x2D,
    "capslock": 0x14, "printscreen": 0x2C, "apps": 0x5D, "pause": 0x13,
    "volume-up": 0xAF, "volume-down": 0xAE, "volume-mute": 0xAD,
    "media-play-pause": 0xB3, "media-next": 0xB0, "media-prev": 0xB1,
    "media-stop": 0xB2,
}
for _i in range(1, 25):
    VK["f%d" % _i] = 0x6F + _i

# Punctuation goes through OEM virtual keys rather than characters so that
# combos like ctrl+plus reach browsers as the zoom shortcut.
CHAR_ALIASES = {
    "comma": 0xBC, "period": 0xBE, "dot": 0xBE, "slash": 0xBF,
    "minus": 0xBD, "plus": 0xBB, "equals": 0xBB, "backslash": 0xDC,
    "semicolon": 0xBA, "quote": 0xDE, "backtick": 0xC0,
    "lbracket": 0xDB, "rbracket": 0xDD,
}

MODS = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "option": 0x12,
    "shift": 0x10,
    "win": 0x5B, "meta": 0x5B, "super": 0x5B,
}

EXTENDED_VKS = {
    0x25, 0x27, 0x26, 0x28, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E, 0x2C,
    0x90, 0x5D, 0xAF, 0xAE, 0xAD, 0xB3, 0xB0, 0xB1, 0xB2,
}

VK_NAMES = {}
for _name, _code in (list(VK.items()) + list(CHAR_ALIASES.items())
                     + list(MODS.items())):
    # First alias wins, so 0xBB reads as PLUS rather than EQUALS.
    VK_NAMES.setdefault(_code, _name.upper())

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    class _VALUE(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("raw", ctypes.c_ubyte * 32)]

    _anonymous_ = ("value",)
    _fields_ = [("type", wt.DWORD), ("value", _VALUE)]


u32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
u32.SendInput.restype = ctypes.c_uint
u32.VkKeyScanW.argtypes = [ctypes.c_wchar]
u32.VkKeyScanW.restype = ctypes.c_short


def key_event(vk, keyup=False, scan=0, unicode_char=False):
    flags = 0
    if unicode_char:
        flags |= KEYEVENTF_UNICODE
    elif vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    if keyup:
        flags |= KEYEVENTF_KEYUP
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(0 if unicode_char else vk, scan, flags, 0, None)
    return item


def send_input(events):
    if not events:
        return 0
    buf = (INPUT * len(events))(*events)
    return u32.SendInput(len(events), ctypes.byref(buf), ctypes.sizeof(INPUT))


def vk_for_key(key):
    """Resolve one key token to a virtual key code, or raise ValueError."""
    if key in VK:
        return VK[key]
    if key in CHAR_ALIASES:
        return CHAR_ALIASES[key]
    if key in ("brightness-up", "brightness-down"):
        raise ValueError(
            "brightness keys cannot be injected on Windows; use "
            "'brightness up [steps]' or 'brightness down [steps]' instead")
    if len(key) == 1:
        scan = u32.VkKeyScanW(key)
        if scan != -1:
            return scan & 0xFF
        if key.upper().isalnum():
            return ord(key.upper())
    raise ValueError("unknown key: %s" % key)


def build_key_plan(combo):
    """Return (events, human readable plan) for a combo like ctrl+shift+t."""
    if not combo.strip():
        raise ValueError("empty key combo")
    parts = [p.strip().lower() for p in combo.split("+")]
    if any(p == "" for p in parts):
        raise ValueError("malformed combo: empty segment (use 'plus' for the "
                         "+ key, e.g. ctrl+plus)")
    mods, key = parts[:-1], parts[-1]
    for mod in mods:
        if mod in ("cmd", "command"):
            raise ValueError(
                "cmd is a macOS modifier; use ctrl for shortcuts or win for "
                "the Windows key")
    bad = [m for m in mods if m not in MODS]
    if bad:
        raise ValueError("unknown modifier: %s" % "+".join(bad))
    key_vk = vk_for_key(key)
    mod_vks = [MODS[m] for m in mods]
    events, plan = [], []
    for vk in mod_vks:
        events.append(key_event(vk))
        plan.append("%s down" % VK_NAMES.get(vk, "0x%02X" % vk))
    label = VK_NAMES.get(key_vk) or (key.upper() if len(key) == 1
                                     else "0x%02X" % key_vk)
    events.append(key_event(key_vk))
    plan.append("%s down" % label)
    events.append(key_event(key_vk, keyup=True))
    plan.append("%s up" % label)
    for vk in reversed(mod_vks):
        events.append(key_event(vk, keyup=True))
        plan.append("%s up" % VK_NAMES.get(vk, "0x%02X" % vk))
    return events, ", ".join(plan)


def send_keys(combo, dry_run=False):
    try:
        events, plan = build_key_plan(combo)
    except ValueError as e:
        return fail(str(e))
    if dry_run:
        return ok({"dry_run": True, "combo": combo, "input_plan": plan,
                   "events": len(events)})
    sent = send_input(events)
    if sent != len(events):
        return fail(UIPI_FIX, {"combo": combo, "events_sent": sent})
    return ok({"sent": combo})


def type_text(text, dry_run=False):
    if text == "":
        return fail("nothing to type (empty text)")
    events = []
    for ch in text:
        if ch == "\n":
            events += [key_event(VK["return"]), key_event(VK["return"], True)]
        elif ch == "\t":
            events += [key_event(VK["tab"]), key_event(VK["tab"], True)]
        else:
            # Feed UTF-16 code units so characters outside the BMP work.
            encoded = ch.encode("utf-16-le")
            units = struct.unpack("<%dH" % (len(encoded) // 2), encoded)
            for unit in units:
                events.append(key_event(0, scan=unit, unicode_char=True))
                events.append(key_event(0, keyup=True, scan=unit,
                                        unicode_char=True))
    if dry_run:
        return ok({"dry_run": True, "input_plan": "unicode: %d chars, %d "
                   "input events" % (len(text), len(events)),
                   "chars": len(text)})
    # Chunk the batch: very long strings can exceed the input queue.
    sent = 0
    for start in range(0, len(events), 200):
        block = events[start:start + 200]
        got = send_input(block)
        sent += got
        if got != len(block):
            return fail(UIPI_FIX, {"events_sent": sent})
    return ok({"typed_chars": len(text)})


# ---------- system ----------

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD),
                ("BatteryFullLifeTime", wt.DWORD)]


k32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
k32.GetSystemPowerStatus.restype = wt.BOOL


def battery():
    status = SYSTEM_POWER_STATUS()
    if not k32.GetSystemPowerStatus(ctypes.byref(status)):
        return fail("GetSystemPowerStatus failed (error %d)"
                    % ctypes.get_last_error())
    ac = status.ACLineStatus
    flag = status.BatteryFlag
    source = {0: "Battery", 1: "AC"}.get(ac, "unknown")
    if flag & 128:
        state = "no battery"
    elif flag & 8:
        state = "charging"
    elif flag & 4:
        state = "critical"
    elif flag & 2:
        state = "low"
    else:
        state = "charged" if ac == 1 else "discharging"
    data = {"source": source, "state": state}
    if status.BatteryLifePercent != 255:
        data["percent"] = int(status.BatteryLifePercent)
    secs = status.BatteryLifeTime
    if secs != 0xFFFFFFFF:
        data["time_remaining"] = "%d:%02d" % (secs // 3600, (secs % 3600) // 60)
    data["raw"] = ("ACLineStatus=%d BatteryFlag=%d percent=%d life=%s"
                   % (ac, flag, status.BatteryLifePercent,
                      "unknown" if secs == 0xFFFFFFFF else secs))
    return ok(data)


CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
# Verified vtable slots. IMMDeviceEnumerator: 3 EnumAudioEndpoints,
# 4 GetDefaultAudioEndpoint. IMMDevice: 3 Activate. IAudioEndpointVolume:
# 7 SetMasterVolumeLevelScalar, 9 GetMasterVolumeLevelScalar, 14 SetMute,
# 15 GetMute.
AEV_SET_SCALAR, AEV_GET_SCALAR, AEV_SET_MUTE, AEV_GET_MUTE = 7, 9, 14, 15


class ComVolume:
    """Default output device volume through IAudioEndpointVolume."""

    method = "wasapi"

    def __init__(self):
        ole.CoInitialize(None)
        enumerator = ctypes.c_void_p()
        ole.CoCreateInstance(
            ctypes.byref(GUID(CLSID_MMDEVICE_ENUMERATOR)), None, 1,
            ctypes.byref(GUID(IID_IMMDEVICE_ENUMERATOR)),
            ctypes.byref(enumerator))
        device = ctypes.c_void_p()
        com_method(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                   ctypes.POINTER(ctypes.c_void_p))(
            enumerator, 0, 1, ctypes.byref(device))
        if not device.value:
            raise OSError("no default audio output device")
        endpoint = ctypes.c_void_p()
        com_method(device, 3, ctypes.c_long, ctypes.POINTER(GUID),
                   ctypes.c_ulong, ctypes.c_void_p,
                   ctypes.POINTER(ctypes.c_void_p))(
            device, ctypes.byref(GUID(IID_IAUDIO_ENDPOINT_VOLUME)), 23, None,
            ctypes.byref(endpoint))
        if not endpoint.value:
            raise OSError("could not activate IAudioEndpointVolume")
        self.p = endpoint

    def get(self):
        level = ctypes.c_float()
        com_method(self.p, AEV_GET_SCALAR, ctypes.c_long,
                   ctypes.POINTER(ctypes.c_float))(self.p, ctypes.byref(level))
        return level.value

    def set(self, scalar):
        com_method(self.p, AEV_SET_SCALAR, ctypes.c_long, ctypes.c_float,
                   ctypes.POINTER(GUID))(self.p, scalar, None)

    def get_mute(self):
        muted = ctypes.c_int()
        com_method(self.p, AEV_GET_MUTE, ctypes.c_long,
                   ctypes.POINTER(ctypes.c_int))(self.p, ctypes.byref(muted))
        return bool(muted.value)

    def set_mute(self, state):
        com_method(self.p, AEV_SET_MUTE, ctypes.c_long, ctypes.c_int,
                   ctypes.POINTER(GUID))(self.p, 1 if state else 0, None)


class PycawVolume:
    """Same contract as ComVolume, used only if pycaw is installed."""

    method = "pycaw"

    def __init__(self):
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL,
                                      None)
        self.iface = ctypes.cast(interface,
                                 ctypes.POINTER(IAudioEndpointVolume))

    def get(self):
        return self.iface.GetMasterVolumeLevelScalar()

    def set(self, scalar):
        self.iface.SetMasterVolumeLevelScalar(scalar, None)

    def get_mute(self):
        return bool(self.iface.GetMute())

    def set_mute(self, state):
        self.iface.SetMute(1 if state else 0, None)


def volume_backend():
    for cls in (ComVolume, PycawVolume):
        try:
            return cls()
        except Exception:
            continue
    return None


NO_VOLUME_API = (
    "The audio endpoint API is unavailable, so absolute volume cannot be "
    "read or set. Check that an output device is enabled in Settings > "
    "System > Sound, or install pycaw as an alternative backend."
)


def volume_get():
    backend = volume_backend()
    if not backend:
        return fail(NO_VOLUME_API)
    return ok({"volume": int(round(backend.get() * 100)),
               "muted": backend.get_mute(), "method": backend.method})


def volume_set(n):
    if not 0 <= n <= 100:
        return fail("volume must be 0-100")
    backend = volume_backend()
    if not backend:
        return fail(NO_VOLUME_API)
    backend.set(n / 100.0)
    return ok({"volume": n, "method": backend.method})


def volume_step(delta):
    backend = volume_backend()
    if backend:
        previous = int(round(backend.get() * 100))
        new = max(0, min(100, previous + delta))
        backend.set(new / 100.0)
        return ok({"volume": new, "previous": previous,
                   "method": backend.method})
    # Last resort: the media keys step in fixed increments of about 2.
    presses = max(1, abs(delta) // 2)
    for _ in range(presses):
        r = send_keys("volume-up" if delta > 0 else "volume-down")
        if not r["ok"]:
            return r
    return ok({"method": "media-keys", "presses": presses,
               "note": "stepped with the volume keys; the endpoint API was "
                       "unavailable so the resulting level is unknown"})


def volume_mute(state):
    backend = volume_backend()
    if backend:
        backend.set_mute(state)
        return ok({"muted": state, "method": backend.method})
    r = send_keys("volume-mute")
    if not r["ok"]:
        return r
    return ok({"method": "media-keys",
               "note": "the mute key toggles; absolute mute state could not "
                       "be set because the endpoint API was unavailable"})


def brightness_wmi_get():
    code, out, _ = pwsh(
        "$b=@(Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightness -ErrorAction Stop); $b[0].CurrentBrightness")
    if code == 0 and out.strip().isdigit():
        return int(out.strip())
    return None


def brightness_wmi_set(value):
    code, _, err = pwsh(
        "$m=@(Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightnessMethods -ErrorAction Stop); "
        "Invoke-CimMethod -InputObject $m[0] -MethodName WmiSetBrightness "
        "-Arguments @{Timeout=[uint32]1;Brightness=[byte]%d} | Out-Null"
        % value)
    return code == 0, err


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_void_p),
                ("description", ctypes.c_wchar * 128)]


def physical_monitors():
    """DDC/CI handles for attached monitors. Empty list if unsupported."""
    try:
        dxva2 = ctypes.WinDLL("dxva2")
    except OSError:
        return None, []
    handles = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                               ctypes.c_void_p,
                               ctypes.POINTER(wt.RECT), LRESULT)

    @proto
    def collect(hmonitor, _hdc, _rect, _data):
        handles.append(hmonitor)
        return 1

    u32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        proto, LRESULT]
    u32.EnumDisplayMonitors(None, None, collect, 0)
    dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    monitors = []
    for hmonitor in handles:
        count = wt.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                hmonitor, ctypes.byref(count)) or not count.value:
            continue
        array = (PHYSICAL_MONITOR * count.value)()
        dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value,
                                                     array):
            continue
        monitors.extend(array[i] for i in range(count.value))
    return dxva2, monitors


def brightness_ddc_get():
    dxva2, monitors = physical_monitors()
    if not monitors:
        return None
    dxva2.GetMonitorBrightness.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]
    low, cur, high = wt.DWORD(), wt.DWORD(), wt.DWORD()
    for monitor in monitors:
        if dxva2.GetMonitorBrightness(monitor.handle, ctypes.byref(low),
                                      ctypes.byref(cur), ctypes.byref(high)):
            span = max(1, high.value - low.value)
            return int(round((cur.value - low.value) * 100.0 / span))
    return None


def brightness_ddc_set(value):
    dxva2, monitors = physical_monitors()
    if not monitors:
        return 0
    dxva2.GetMonitorBrightness.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]
    dxva2.SetMonitorBrightness.argtypes = [ctypes.c_void_p, wt.DWORD]
    changed = 0
    low, cur, high = wt.DWORD(), wt.DWORD(), wt.DWORD()
    for monitor in monitors:
        if not dxva2.GetMonitorBrightness(monitor.handle, ctypes.byref(low),
                                          ctypes.byref(cur),
                                          ctypes.byref(high)):
            continue
        span = high.value - low.value
        target = low.value + int(round(span * value / 100.0))
        if dxva2.SetMonitorBrightness(monitor.handle, target):
            changed += 1
    return changed


NO_BRIGHTNESS_API = (
    "Brightness control is unavailable on this display. WMI brightness "
    "works on built in laptop panels; external monitors need DDC/CI enabled "
    "in their on screen menu. Some desktop GPUs expose neither."
)


def brightness_get():
    value = brightness_wmi_get()
    if value is not None:
        return ok({"brightness": value, "scale": "0-100", "method": "wmi"})
    value = brightness_ddc_get()
    if value is not None:
        return ok({"brightness": value, "scale": "0-100", "method": "ddcci"})
    return fail(NO_BRIGHTNESS_API)


def brightness_set(value):
    if value is None:
        return usage_fail("brightness set needs a value 0-100")
    if 0 < value < 1:
        return fail("brightness is an integer percent 0-100 on Windows, so "
                    "%g would set 0%%. Use e.g. 'brightness set %d'."
                    % (value, int(round(value * 100))))
    if not 0 <= value <= 100:
        return fail("brightness must be 0-100")
    value = int(round(value))
    done, err = brightness_wmi_set(value)
    if done:
        return ok({"brightness": value, "method": "wmi"})
    changed = brightness_ddc_set(value)
    if changed:
        return ok({"brightness": value, "method": "ddcci",
                   "monitors_changed": changed})
    return fail(NO_BRIGHTNESS_API, {"detail": err.splitlines()[-1] if err
                                    else None})


BRIGHTNESS_STEP = 10


def brightness_step(direction, steps, dry_run=False):
    if steps < 1:
        return fail("steps must be a positive integer (1 or more)")
    current = brightness_get()
    if not current["ok"]:
        return current
    delta = BRIGHTNESS_STEP * steps * (1 if direction == "up" else -1)
    target = max(0, min(100, current["brightness"] + delta))
    if dry_run:
        return ok({"dry_run": True, "direction": direction, "steps": steps,
                   "from": current["brightness"], "would_set": target,
                   "method": current["method"]})
    r = brightness_set(target)
    if not r["ok"]:
        return r
    return ok({"direction": direction, "steps": steps,
               "from": current["brightness"], "brightness": target,
               "method": r["method"]})


def ip_info(public=False):
    data = {"local": None, "interface": None}
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        data["local"] = probe.getsockname()[0]
    except OSError:
        try:
            data["local"] = socket.gethostbyname(socket.gethostname())
        except OSError:
            pass
    finally:
        probe.close()
    if data["local"]:
        code, out, _ = pwsh(
            "(Get-NetIPAddress -IPAddress '%s' -ErrorAction SilentlyContinue"
            ").InterfaceAlias" % ps_str(data["local"]), timeout=20)
        if code == 0 and out.strip():
            data["interface"] = out.splitlines()[0].strip()
    if public:
        try:
            req = urllib.request.Request(
                "https://checkip.amazonaws.com",
                headers={"User-Agent": "Mozilla/5.0"})
            data["public"] = urllib.request.urlopen(
                req, timeout=6).read().decode().strip()
        except Exception as e:
            data["public"] = None
            data["public_error"] = str(e)
    return ok(data)


# ---------- apps ----------

APP_ALIASES = {
    "notepad": "notepad.exe", "wordpad": "write.exe",
    "calc": "calc.exe", "calculator": "calc.exe",
    "paint": "mspaint.exe", "mspaint": "mspaint.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe",
    "powershell": "powershell.exe", "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "task manager": "taskmgr.exe", "taskmgr": "taskmgr.exe",
    "registry editor": "regedit.exe", "regedit": "regedit.exe",
    "control panel": "control.exe",
    "device manager": "devmgmt.msc", "services": "services.msc",
    "settings": "ms-settings:", "camera": "microsoft.windows.camera:",
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "brave": "brave.exe", "opera": "opera.exe",
    "spotify": "spotify.exe", "vlc": "vlc.exe",
    "code": "code.exe", "vscode": "code.exe", "vs code": "code.exe",
    "visual studio code": "code.exe",
    "word": "winword.exe", "excel": "excel.exe",
    "powerpoint": "powerpnt.exe", "outlook": "outlook.exe",
    "teams": "ms-teams.exe", "discord": "discord.exe",
    "slack": "slack.exe", "zoom": "zoom.exe", "steam": "steam.exe",
    "notepad++": "notepad++.exe", "sublime": "sublime_text.exe",
    "snipping tool": "snippingtool.exe", "magnifier": "magnify.exe",
    "on screen keyboard": "osk.exe", "character map": "charmap.exe",
    "resource monitor": "resmon.exe", "system information": "msinfo32.exe",
    "disk cleanup": "cleanmgr.exe", "event viewer": "eventvwr.msc",
}

APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


def exe_of_pid(pid):
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        k32.CloseHandle(handle)
    return None


def visible_windows():
    """[(hwnd, pid, exe, title)] for visible, titled, top level windows."""
    found = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, ctypes.c_void_p, LRESULT)

    @proto
    def collect(hwnd, _param):
        if u32.IsWindowVisible(hwnd):
            length = u32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                pid = wt.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((hwnd, pid.value, exe_of_pid(pid.value),
                              buf.value))
        return True

    u32.EnumWindows.argtypes = [proto, LRESULT]
    u32.EnumWindows(collect, 0)
    return found


def process_table():
    """[(pid, exe_lower)] for every process we can see."""
    try:
        import psutil
        return [(p.pid, (p.info.get("name") or "").lower())
                for p in psutil.process_iter(["name"])]
    except Exception:
        pass
    code, out, _ = run(["tasklist", "/fo", "csv", "/nh"], timeout=30)
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        fields = next(csv.reader([line]), [])
        if len(fields) >= 2 and fields[1].strip().isdigit():
            rows.append((int(fields[1]), fields[0].strip().lower()))
    return rows


def app_stem(name):
    """Normalize an app or exe name for comparison: 'Chrome.exe' -> 'chrome'."""
    base = os.path.basename(str(name)).strip().lower()
    for suffix in (".exe", ".msc", ".com"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    return base


def app_candidates(name):
    """Every exe stem that could satisfy this app name."""
    lowered = name.strip().lower()
    stems = {app_stem(lowered)}
    alias = APP_ALIASES.get(lowered)
    if alias:
        stems.add(app_stem(alias))
    return {s for s in stems if s}


def apps_list(show_all=False):
    if show_all:
        names = {app_stem(exe) for _pid, exe in process_table() if exe}
    else:
        names = {app_stem(exe) for _h, _p, exe, _t in visible_windows() if exe}
    names.discard("")
    ordered = sorted(names)
    return ok({"count": len(ordered), "apps": ordered})


def app_front():
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return fail("no foreground window (the session may be locked)")
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    exe = exe_of_pid(pid.value)
    length = u32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
    buf = ctypes.create_unicode_buffer(length + 1)
    u32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, length + 1)
    return ok({"frontmost": app_stem(exe) if exe else None, "exe": exe,
               "title": buf.value, "pid": pid.value})


def app_pids(name):
    wanted = app_candidates(name)
    return [pid for pid, exe in process_table() if app_stem(exe) in wanted]


def app_running(name):
    return ok({"app": name, "running": bool(app_pids(name))})


def resolve_app_target(name):
    """(target, how) for launching, or (None, None) if unresolvable."""
    raw = name.strip()
    lowered = raw.lower()
    if os.path.exists(raw):
        return raw, "path"
    candidate = APP_ALIASES.get(lowered, raw)
    if candidate.endswith(":"):
        return candidate, "protocol"
    if candidate.lower().endswith(".msc"):
        return candidate, "mmc"
    named = candidate if os.path.splitext(candidate)[1] else candidate + ".exe"
    found = shutil.which(named) or shutil.which(candidate)
    if found:
        return found, "path"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, APP_PATHS_KEY + "\\" + named) as key:
                value = winreg.QueryValue(key, None)
                if value:
                    return value.strip('"'), "registry"
        except OSError:
            continue
    link = find_start_menu_link(raw)
    if link:
        return link, "start-menu"
    appid = find_uwp_appid(raw)
    if appid:
        return appid, "appsfolder"
    return None, None


def start_menu_dirs():
    dirs = []
    for var in ("ProgramData", "APPDATA"):
        base = os.environ.get(var)
        if base:
            path = os.path.join(base, "Microsoft", "Windows", "Start Menu",
                                "Programs")
            if os.path.isdir(path):
                dirs.append(path)
    return dirs


def find_start_menu_link(name):
    wanted = name.strip().lower()
    partial = None
    for base in start_menu_dirs():
        for root, _dirs, files in os.walk(base):
            for filename in files:
                if not filename.lower().endswith((".lnk", ".url")):
                    continue
                stem = os.path.splitext(filename)[0].lower()
                if stem == wanted:
                    return os.path.join(root, filename)
                if partial is None and wanted in stem:
                    partial = os.path.join(root, filename)
    return partial


def find_uwp_appid(name):
    code, out, _ = pwsh(
        "Get-StartApps | Where-Object { $_.Name -like '*%s*' } | "
        "Select-Object -First 1 -ExpandProperty AppID" % ps_str(name),
        timeout=40)
    if code == 0 and out.strip():
        return out.splitlines()[0].strip()
    return None


def app_open(name):
    target, how = resolve_app_target(name)
    if not target:
        return fail(
            "could not find '%s'. Tried the alias table, PATH, the App Paths "
            "registry key, the Start Menu and the UWP app list. Pass a full "
            "path to the executable instead." % name)
    try:
        if how == "appsfolder":
            code, _, err = run(["explorer.exe",
                                "shell:AppsFolder\\" + target], timeout=20)
            # explorer.exe returns 1 even when the launch succeeds.
            if code not in (0, 1):
                return fail(err or "explorer could not launch " + target)
        elif how == "mmc":
            subprocess.Popen(["cmd", "/c", "start", "", target],
                             creationflags=CREATE_NO_WINDOW)
        else:
            os.startfile(target)
    except OSError as e:
        return fail("could not open '%s': %s" % (name, e))
    return ok({"opened": name, "target": target, "resolved_via": how})


def window_handles_for(pids):
    return [(hwnd, pid) for hwnd, pid, _exe, _title in visible_windows()
            if pid in pids]


def app_quit(name):
    pids = set(app_pids(name))
    if not pids:
        return ok({"app": name, "was_running": False})
    windows = window_handles_for(pids)
    method = None
    if windows:
        for hwnd, _pid in windows:
            u32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, None, 0)
        method = "wm_close"
        deadline = time.time() + 2.5
        while time.time() < deadline:
            time.sleep(0.2)
            if not app_pids(name):
                break
    if app_pids(name):
        for pid in pids:
            run(["taskkill", "/PID", str(pid)], timeout=15)
        method = "taskkill" if method is None else method + "+taskkill"
        deadline = time.time() + 2.0
        while time.time() < deadline:
            time.sleep(0.2)
            if not app_pids(name):
                break
    still = bool(app_pids(name))
    data = {"quit": name, "was_running": True, "method": method,
            "still_running": still}
    if still:
        data["note"] = ("the app was asked to close but is still running; it "
                        "may be showing a save prompt")
    return ok(data)


def app_activate(name):
    pids = set(app_pids(name))
    if not pids:
        return fail("'%s' is not running; use 'app open %s' first"
                    % (name, name))
    windows = window_handles_for(pids)
    if not windows:
        return fail("'%s' is running but has no visible window to activate"
                    % name)
    hwnd = ctypes.c_void_p(windows[0][0])
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, SW_RESTORE)
    else:
        u32.ShowWindow(hwnd, SW_SHOW)
    # Windows refuses SetForegroundWindow from a background process unless
    # the calling thread shares input state with the target thread.
    target_thread = u32.GetWindowThreadProcessId(hwnd, None)
    our_thread = k32.GetCurrentThreadId()
    attached = u32.AttachThreadInput(our_thread, target_thread, True)
    try:
        u32.BringWindowToTop(hwnd)
        raised = bool(u32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            u32.AttachThreadInput(our_thread, target_thread, False)
    front = app_front()
    now = front.get("frontmost") if front["ok"] else None
    if not raised and now not in app_candidates(name):
        return fail("Windows refused to change the foreground window. Fix: "
                    "click the desktop or the target window once, then retry.",
                    {"app": name})
    return ok({"activated": name, "frontmost": now})


# ---------- web ----------

def load_sites():
    try:
        with open(SITES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sites(sites):
    with open(SITES_PATH, "w", encoding="utf-8") as f:
        json.dump(sites, f, indent=2, sort_keys=True)


def normalize_url(u):
    if not re.match(r"^[a-z]+://", u, re.I):
        u = "https://" + u
    return u


def url_reachable(url):
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def open_url(url):
    """Hand a URL to the default browser."""
    try:
        os.startfile(url)
        return None
    except OSError as e:
        return str(e)


def web_open(name, dry_run=False):
    name = name.strip()
    sites = load_sites()
    key = name.lower()
    if re.match(r"^[a-z]+://", key, re.I) or ("." in key and " " not in key):
        url, source = normalize_url(name), "url"
    elif key in sites:
        url, source = normalize_url(sites[key]), "cache"
    elif key.replace(" ", "") in sites:
        url, source = normalize_url(sites[key.replace(" ", "")]), "cache"
    else:
        guess = "https://" + key.replace(" ", "") + ".com"
        if url_reachable(guess):
            url, source = guess, "guess"
            if not dry_run:  # a dry-run must not mutate the cache
                sites[key] = guess
                save_sites(sites)
        else:
            url = ("https://www.google.com/search?q="
                   + urllib.parse.quote(name))
            source = "search-fallback"
    if dry_run:
        return ok({"dry_run": True, "url": url, "resolved_via": source})
    err = open_url(url)
    if err:
        return fail(err)
    return ok({"opened": url, "resolved_via": source})


def youtube_play(query, dry_run=False):
    search_url = ("https://www.youtube.com/results?search_query="
                  + urllib.parse.quote(query))
    url, source = search_url, "search-page"
    try:
        req = urllib.request.Request(
            search_url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=8).read().decode(
            "utf-8", "ignore")
        m = re.search(r'"videoId":"([\w-]{11})"', html)
        if m:
            url = "https://www.youtube.com/watch?v=" + m.group(1)
            source = "first-result"
    except Exception:
        pass
    if dry_run:
        return ok({"dry_run": True, "url": url, "resolved_via": source})
    err = open_url(url)
    if err:
        return fail(err)
    return ok({"opened": url, "resolved_via": source})


# ---------- browser / youtube-transport / media ----------

BROWSER_KEYS = {
    "new-tab": "ctrl+t", "close-tab": "ctrl+w", "reopen-tab": "ctrl+shift+t",
    "next-tab": "ctrl+tab", "prev-tab": "ctrl+shift+tab",
    "new-window": "ctrl+n", "private-window": "ctrl+shift+n",
    "zoom-in": "ctrl+plus", "zoom-out": "ctrl+minus", "zoom-reset": "ctrl+0",
    "refresh": "f5", "hard-refresh": "ctrl+f5",
    "back": "alt+left", "forward": "alt+right",
    "history": "ctrl+h", "bookmarks": "ctrl+shift+o",
    "address-bar": "ctrl+l", "find": "ctrl+f",
    "devtools": "f12", "fullscreen": "f11",
}

YT_KEYS = {
    "play-pause": "k", "mute": "m", "fullscreen": "f", "theater": "t",
    "miniplayer": "i", "captions": "c",
    "vol-up": "up", "vol-down": "down",
    "back-5": "left", "fwd-5": "right",
    "back-10": "j", "fwd-10": "l",
    "prev-frame": "comma", "next-frame": "period",
    "speed-up": "shift+period", "speed-down": "shift+comma",
    "start": "home", "end": "end",
    "prev-chapter": "ctrl+left", "next-chapter": "ctrl+right",
    "next-video": "shift+n", "prev-video": "shift+p",
}


def browser_action(action, dry_run=False):
    if action not in BROWSER_KEYS:
        return fail("unknown browser action '%s'. Options: %s"
                    % (action, ", ".join(sorted(BROWSER_KEYS))))
    r = send_keys(BROWSER_KEYS[action], dry_run=dry_run)
    if r["ok"]:
        r["action"] = action
    return r


def yt_action(action, dry_run=False):
    if action not in YT_KEYS:
        return fail("unknown yt action '%s'. Options: %s"
                    % (action, ", ".join(sorted(YT_KEYS))))
    r = send_keys(YT_KEYS[action], dry_run=dry_run)
    if r["ok"]:
        r["action"] = action
        r["note"] = ("sent to the frontmost app; focus a browser with "
                     "YouTube first")
    return r


PLAYERS = ["spotify", "vlc", "wmplayer", "musicbee", "foobar2000", "itunes",
           "aimp", "winamp"]

# APPCOMMAND codes. play-pause, next and prev also exist as media virtual
# keys, which the shell routes to the active session even unfocused; play
# and pause are only addressable as APPCOMMANDs.
APPCOMMANDS = {"play-pause": 14, "next": 11, "prev": 12, "play": 46,
               "pause": 47}
MEDIA_KEYS = {"play-pause": "media-play-pause", "next": "media-next",
              "prev": "media-prev"}


def player_windows():
    return [(hwnd, exe, title) for hwnd, _pid, exe, title in visible_windows()
            if exe and app_stem(exe) in PLAYERS]


def smtc_status():
    """Real track metadata from the system media session, if winsdk is here."""
    try:
        import asyncio
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager)

        async def read():
            manager = await Manager.request_async()
            session = manager.get_current_session()
            if not session:
                return None
            props = await session.try_get_media_properties_async()
            info = session.get_playback_info()
            states = {0: "closed", 1: "opened", 2: "changing",
                      3: "stopped", 4: "playing", 5: "paused"}
            return {"state": states.get(int(info.playback_status), "unknown"),
                    "track": props.title, "artist": props.artist}

        return asyncio.run(read())
    except Exception:
        return None


def media_status():
    info = smtc_status()
    if info:
        data = {"player": "system-session", "method": "smtc"}
        data.update({k: v for k, v in info.items() if v})
        return ok(data)
    for _hwnd, exe, title in player_windows():
        name = app_stem(exe)
        data = {"player": name, "method": "window-title"}
        if name == "spotify":
            # Spotify puts "Artist - Track" in its title while playing and
            # its own name there when nothing is playing.
            if title.lower().startswith("spotify") or " - " not in title:
                data["state"] = "paused"
            else:
                artist, track = title.split(" - ", 1)
                data.update({"state": "playing", "artist": artist.strip(),
                             "track": track.strip()})
        else:
            data.update({"state": "unknown", "title": title})
        data["note"] = ("read from the window title; install winsdk for exact "
                        "player state")
        return ok(data)
    front = app_front()
    return ok({"player": None,
               "frontmost": front.get("frontmost") if front["ok"] else None,
               "note": "no known media player running (Spotify, VLC, ...)"})


def media_action(action, dry_run=False):
    if action == "status":
        return media_status()
    if action not in APPCOMMANDS:
        return fail("unknown media action '%s'" % action)
    targets = player_windows()
    if action in MEDIA_KEYS:
        r = send_keys(MEDIA_KEYS[action], dry_run=dry_run)
        if r["ok"]:
            r["action"] = action
            r["method"] = "media-key"
            if targets:
                r["player"] = app_stem(targets[0][1])
        return r
    command = APPCOMMANDS[action] << 16
    if dry_run:
        return ok({"dry_run": True, "action": action,
                   "input_plan": "WM_APPCOMMAND %d to %s"
                   % (APPCOMMANDS[action],
                      app_stem(targets[0][1]) if targets else "all windows"),
                   "method": "appcommand"})
    if targets:
        hwnd = ctypes.c_void_p(targets[0][0])
        u32.SendNotifyMessageW(hwnd, WM_APPCOMMAND, hwnd, command)
        return ok({"action": action, "method": "appcommand",
                   "player": app_stem(targets[0][1])})
    u32.SendNotifyMessageW(HWND_BROADCAST, WM_APPCOMMAND, None, command)
    return ok({"action": action, "method": "appcommand-broadcast",
               "note": "no known player window; broadcast instead. Absolute "
                       "play/pause depends on player support, so prefer "
                       "'media play-pause'."})


# ---------- desktop utilities ----------

def desktop_dir():
    """The real Desktop, which OneDrive often redirects away from ~."""
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\Shell Folders") as key:
            path = winreg.QueryValueEx(key, "Desktop")[0]
            if os.path.isdir(path):
                return path
    except OSError:
        pass
    for candidate in (os.path.join(os.environ.get("USERPROFILE", ""),
                                   "Desktop"),
                      os.path.expanduser("~/Desktop")):
        if candidate and os.path.isdir(candidate):
            return candidate
    return os.getcwd()


def write_png(path, width, height, bgra):
    """Minimal RGB PNG writer, so screenshots need no image library."""
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        line = bytearray(bgra[row * stride:(row + 1) * stride])
        rgb = bytearray(width * 3)
        rgb[0::3] = line[2::4]
        rgb[1::3] = line[1::4]
        rgb[2::3] = line[0::4]
        raw.append(0)  # filter type: none
        raw += rgb

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                        0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def screenshot_gdi(path):
    u32.SetProcessDPIAware()
    x = u32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = u32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = u32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:
        raise OSError("no display to capture")
    screen_dc = u32.GetDC(None)
    memory_dc = gdi.CreateCompatibleDC(screen_dc)
    bitmap = gdi.CreateCompatibleBitmap(screen_dc, width, height)
    try:
        gdi.SelectObject(memory_dc, bitmap)
        if not gdi.BitBlt(memory_dc, 0, 0, width, height, screen_dc, x, y,
                          SRCCOPY):
            raise OSError("BitBlt failed (error %d)"
                          % ctypes.get_last_error())

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD),
                        ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD),
                        ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long),
                        ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                        ("bmiColors", wt.DWORD * 3)]

        gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_uint, ctypes.c_uint,
                                  ctypes.c_void_p,
                                  ctypes.POINTER(BITMAPINFO), ctypes.c_uint]
        info = BITMAPINFO()
        # A negative height asks for top down rows, so no flip is needed.
        info.bmiHeader = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER),
                                          width, -height, 1, 32, 0, 0, 0, 0,
                                          0, 0)
        buf = ctypes.create_string_buffer(width * height * 4)
        if not gdi.GetDIBits(memory_dc, bitmap, 0, height, buf,
                             ctypes.byref(info), 0):
            raise OSError("GetDIBits failed")
        write_png(path, width, height, memoryview(buf).cast("B"))
    finally:
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory_dc)
        u32.ReleaseDC(None, screen_dc)
    return {"width": width, "height": height}


def screenshot(path=None):
    if not path:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(desktop_dir(), "pc-screenshot-%s.png" % stamp)
    path = os.path.expanduser(path)
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        return fail("destination directory does not exist: %s" % parent)
    method, extra = "gdi", {}
    try:
        from PIL import ImageGrab
        image = ImageGrab.grab(all_screens=True)
        image.save(path)
        method = "pil"
        extra = {"width": image.width, "height": image.height}
    except Exception:
        try:
            extra = screenshot_gdi(path)
        except OSError as e:
            return fail("screen capture failed: %s" % e)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return fail("capture produced no file at %s" % path)
    data = {"path": path, "bytes": os.path.getsize(path), "method": method}
    data.update(extra)
    return ok(data)


def with_clipboard(action):
    """Open the clipboard with retries: other apps hold it briefly."""
    last = None
    for _ in range(10):
        if u32.OpenClipboard(None):
            try:
                return action()
            finally:
                u32.CloseClipboard()
        last = ctypes.get_last_error()
        time.sleep(0.05)
    raise OSError("could not open the clipboard, another app is holding it "
                  "(error %s)" % last)


def clipboard_get():
    # Deliberately not clip.exe or Get-Clipboard: both mangle trailing
    # whitespace, which would break exact round trips.
    def read():
        if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = k32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            k32.GlobalUnlock(handle)

    try:
        return ok({"text": with_clipboard(read)})
    except OSError as e:
        return fail(str(e))


def clipboard_set(text):
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)

    def write():
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise OSError("GlobalAlloc failed")
        pointer = k32.GlobalLock(handle)
        if not pointer:
            k32.GlobalFree(handle)
            raise OSError("GlobalLock failed")
        ctypes.memmove(pointer, buf, size)
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):
            k32.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
        return True

    try:
        with_clipboard(write)
    except OSError as e:
        return fail(str(e))
    return ok({"copied_chars": len(text)})


TOAST_AUMID = (r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
               r"\WindowsPowerShell\v1.0\powershell.exe")


def notify(message, title="pc-control", subtitle=None):
    lines = "<text>%s</text><text>%s</text>" % (xml_str(title),
                                                xml_str(message))
    if subtitle:
        lines = ("<text>%s</text><text>%s</text><text>%s</text>"
                 % (xml_str(title), xml_str(subtitle), xml_str(message)))
    toast_xml = ("<toast><visual><binding template='ToastGeneric'>%s"
                 "</binding></visual></toast>" % lines)
    script = (
        "$null=[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$null=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,"
        "ContentType=WindowsRuntime];"
        "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$doc.LoadXml('%s');"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($doc);"
        "[Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('%s').Show($toast)"
        % (ps_str(toast_xml), ps_str(TOAST_AUMID)))
    code, _, err = pwsh(script, timeout=40)
    if code != 0:
        return fail("could not post a notification: %s"
                    % (err.splitlines()[-1] if err else "unknown error"))
    return ok({"notified": message, "method": "toast",
               "note": "Focus Assist and Do Not Disturb can suppress the "
                       "banner without any error"})


def installed_voices():
    code, out, _ = pwsh(
        "Add-Type -AssemblyName System.Speech;"
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
        ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }",
        timeout=40)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def say_text(text, voice=None):
    if text == "":
        return fail("nothing to say (empty text)")
    select = ""
    if voice:
        # SAPI silently keeps the default voice for an unknown name, so
        # validate against the installed list first.
        names = installed_voices()
        match = [n for n in names if n.lower() == voice.lower()]
        if not match:
            return fail("unknown voice '%s'. Installed: %s"
                        % (voice, ", ".join(names) if names else "none found"))
        voice = match[0]
        select = "$s.SelectVoice('%s');" % ps_str(voice)
    script = ("Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "%s$s.Speak('%s')" % (select, ps_str(text)))
    code, _, err = pwsh(script, timeout=180)
    if code != 0:
        return fail(err.splitlines()[-1] if err else "speech synthesis failed")
    data = {"spoke_chars": len(text)}
    if voice:
        data["voice"] = voice
    return ok(data)


# ---------- hardware tests ----------

FFMPEG_FIX = ("ffmpeg is required for this test. Install it with: "
              "winget install Gyan.FFmpeg")


def read_wav_frames(path, frame_ms=20):
    with wave.open(path, "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
    per = max(1, int(rate * frame_ms / 1000))
    frames = [samples[i:i + per] for i in range(0, len(samples), per)]
    rms = [math.sqrt(sum(s * s for s in f) / len(f)) for f in frames if f]
    peak = max((abs(s) for s in samples), default=0)
    clip = sum(1 for s in samples if abs(s) >= 32700) / max(1, len(samples))
    return rms, peak, clip


def dshow_audio_devices():
    """Parse ffmpeg's DirectShow device list. Handles old and new formats."""
    if not shutil.which("ffmpeg"):
        return []
    _code, out, err = run(["ffmpeg", "-hide_banner", "-list_devices", "true",
                           "-f", "dshow", "-i", "dummy"], timeout=30)
    lines = (out + "\n" + err).splitlines()
    devices = []
    for i, line in enumerate(lines):
        m = re.search(r'"([^"]+)"\s*\((audio|video)\)', line)
        if not m or m.group(2) != "audio":
            continue
        entry = {"name": m.group(1), "alt": None}
        if i + 1 < len(lines):
            alt = re.search(r'Alternative name\s+"([^"]+)"', lines[i + 1])
            if alt:
                entry["alt"] = alt.group(1)
        devices.append(entry)
    return devices


def record_wav(path, seconds, device):
    """Capture mono 44.1k audio from a DirectShow device."""
    spec = "audio=" + (device["alt"] or device["name"])
    return run(["ffmpeg", "-y", "-f", "dshow", "-i", spec, "-t",
                str(seconds), "-ar", "44100", "-ac", "1", path],
               timeout=seconds + 25)


def mic_test(seconds=5):
    if not shutil.which("ffmpeg"):
        return fail(FFMPEG_FIX)
    devices = dshow_audio_devices()
    if not devices:
        return fail(
            "no DirectShow audio input device was found. Check Settings > "
            "Privacy & security > Microphone and enable 'Let desktop apps "
            "access your microphone', then confirm an input device is "
            "enabled in Settings > System > Sound.")
    device = devices[0]
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        code, _, err = record_wav(tmp.name, seconds, device)
        if code != 0 or not os.path.getsize(tmp.name):
            detail = err.splitlines()[-1] if err else "no output"
            return fail("mic capture failed (%s). Check the microphone "
                        "privacy setting for desktop apps." % detail,
                        {"device": device["name"]})
        rms, peak, clip = read_wav_frames(tmp.name)
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
    if not rms:
        return fail("no audio frames captured", {"device": device["name"]})
    srt = sorted(rms)
    noise = srt[int(len(srt) * 0.10)]
    signal = srt[int(len(srt) * 0.95) - 1]
    snr = 20 * math.log10(signal / noise) if noise > 0 else float("inf")
    verdict = "ok"
    if peak < 100:
        verdict = "silent: mic dead, muted, or blocked by privacy settings"
    elif clip > 0.05:
        verdict = "clipping: input gain too high"
    elif snr < 6:
        verdict = "noisy: little dynamic range between noise floor and signal"
    return ok({"seconds": seconds, "device": device["name"], "peak": peak,
               "clipping_pct": round(clip * 100, 2),
               "noise_floor_rms": round(noise, 1),
               "signal_rms": round(signal, 1),
               "snr_db": round(snr, 1) if snr != float("inf") else None,
               "verdict": verdict})


def write_tone(path, freq, seconds, rate=44100, amp=0.4):
    n = int(rate * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            if freq == "sweep":
                f = 200 * (10 ** (i / n * 1.7))
                v = math.sin(2 * math.pi * f * i / rate)
            else:
                v = math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(v * amp * 32767))
        w.writeframes(bytes(frames))


def play_wav(path):
    return bool(winmm.PlaySoundW(path, None,
                                 SND_FILENAME | SND_SYNC | SND_NODEFAULT))


def speaker_test(measure=False, duration=1.0):
    tones = [100, 1000, 5000, 10000, "sweep"]
    devices = dshow_audio_devices() if measure else []
    results = []
    for freq in tones:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        write_tone(tmp.name, freq, duration)
        label = "%sHz" % freq if freq != "sweep" else "sweep-200-10k"
        if measure and devices:
            rec = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            rec.close()
            spec = "audio=" + (devices[0]["alt"] or devices[0]["name"])
            recorder = subprocess.Popen(
                ["ffmpeg", "-y", "-f", "dshow", "-i", spec, "-t",
                 str(duration + 0.5), "-ar", "44100", "-ac", "1", rec.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
            played = play_wav(tmp.name)
            try:
                recorder.wait(timeout=duration + 20)
            except subprocess.TimeoutExpired:
                recorder.kill()
            heard = None
            if os.path.exists(rec.name) and os.path.getsize(rec.name):
                try:
                    _rms, peak, _clip = read_wav_frames(rec.name)
                    heard = peak > 500
                except (wave.Error, EOFError):
                    heard = None
                os.unlink(rec.name)
            results.append({"tone": label, "played": played, "heard": heard})
        else:
            results.append({"tone": label, "played": play_wav(tmp.name)})
        os.unlink(tmp.name)
    data = {"tones": results, "measured": bool(measure and devices)}
    if measure and not devices:
        data["measure_error"] = ("no input device available, so playback "
                                 "could not be verified")
    if data["measured"]:
        heard = [r for r in results if r.get("heard")]
        data["verdict"] = ("%d/%d tones confirmed through the microphone"
                           % (len(heard), len(results)))
    else:
        data["verdict"] = ("playback only: tones were sent to the speaker "
                           "but not verified. Use --measure to confirm "
                           "through the microphone.")
    return ok(data)


# ---------- doctor ----------

def module_available(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def doctor():
    checks = {}
    version = sys.getwindowsversion()
    # platform.release() still says "10" on Windows 11; the build number is
    # the only reliable discriminator.
    release = "11" if version.build >= 22000 else platform.release()
    checks["platform"] = "Windows %s build %s" % (release, version.build)
    checks["powershell"] = bool(pwsh_exe())
    # A shift key-up is inert on its own and proves the input path works.
    checks["input_injection"] = send_input(
        [key_event(MODS["shift"], keyup=True)]) == 1
    if not checks["input_injection"]:
        checks["input_injection_fix"] = UIPI_FIX
    checks["elevated"] = bool(shell32.IsUserAnAdmin())
    backend = volume_backend()
    checks["volume_api"] = backend is not None
    if backend:
        checks["volume_backend"] = backend.method
    else:
        checks["volume_api_fix"] = NO_VOLUME_API
    combined = pwsh(
        "$out=@();"
        "try{$b=@(Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightness -ErrorAction Stop);"
        "$out+='brightness='+$b[0].CurrentBrightness}"
        "catch{$out+='brightness='};"
        "try{$null=[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$out+='toast=1'}catch{$out+='toast=0'};"
        "try{Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$out+='voices='+((($s.GetInstalledVoices()|ForEach-Object "
        "{$_.VoiceInfo.Name}) -join '|'))}catch{$out+='voices='};"
        "$out -join [Environment]::NewLine", timeout=60)
    fields = {}
    for line in combined[1].splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    checks["brightness_wmi"] = bool(fields.get("brightness", "").isdigit())
    if not checks["brightness_wmi"]:
        checks["brightness_ddcci"] = brightness_ddc_get() is not None
        if not checks["brightness_ddcci"]:
            checks["brightness_fix"] = NO_BRIGHTNESS_API
    checks["notifications"] = fields.get("toast") == "1"
    if not checks["notifications"]:
        checks["notifications_fix"] = ("the WinRT toast API did not load; "
                                       "notify will report the failure")
    voices = [v for v in fields.get("voices", "").split("|") if v]
    checks["text_to_speech"] = bool(voices)
    if voices:
        checks["voices"] = ", ".join(voices)
    checks["ffmpeg"] = bool(shutil.which("ffmpeg"))
    if not checks["ffmpeg"]:
        checks["ffmpeg_fix"] = FFMPEG_FIX
    devices = dshow_audio_devices()
    checks["mic_device"] = bool(devices)
    if devices:
        checks["mic_device_name"] = devices[0]["name"]
    elif checks["ffmpeg"]:
        checks["mic_device_fix"] = (
            "no DirectShow input found. Settings > Privacy & security > "
            "Microphone > 'Let desktop apps access your microphone'")
    try:
        screen_dc = u32.GetDC(None)
        checks["screen_capture"] = bool(screen_dc)
        if screen_dc:
            u32.ReleaseDC(None, screen_dc)
    except Exception:
        checks["screen_capture"] = False
    checks["clipboard"] = clipboard_get()["ok"]
    checks["console_encoding"] = (sys.stdout.encoding or "").lower()
    checks["pil"] = module_available("PIL")
    checks["pycaw"] = module_available("pycaw")
    checks["winsdk"] = module_available("winsdk")
    checks["psutil"] = module_available("psutil")
    try:
        req = urllib.request.Request(
            "https://checkip.amazonaws.com",
            headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=5)
        checks["network"] = True
    except Exception:
        checks["network"] = False
    # These say something about the environment rather than about a verb, so
    # they must not show up as blocked capabilities.
    informational = ("elevated", "pil", "pycaw", "winsdk", "psutil")
    caps_ok = [k for k, v in checks.items()
               if v is True and k not in informational]
    caps_bad = [k for k, v in checks.items()
                if v is False and k not in informational]
    return ok({"checks": checks, "working": caps_ok, "blocked": caps_bad,
               "optional": {k: checks[k] for k in informational
                            if k in checks}})


# ---------- CLI ----------

def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            # Window titles, clipboard text and track names are not ASCII,
            # and the Windows console default (cp1252) raises on them.
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(prog="pc", description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("battery")
    sub.add_parser("doctor")

    p = sub.add_parser("volume")
    p.add_argument("action", choices=["get", "set", "up", "down", "mute",
                                      "unmute"])
    p.add_argument("value", nargs="?", type=int)

    p = sub.add_parser("brightness")
    p.add_argument("action", choices=["get", "set", "up", "down"])
    p.add_argument("value", nargs="?", type=float)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("ip")
    p.add_argument("--public", action="store_true")

    p = sub.add_parser("apps")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("app")
    p.add_argument("action", choices=["open", "quit", "front", "running",
                                      "activate"])
    p.add_argument("name", nargs="?")

    p = sub.add_parser("web")
    p.add_argument("name", nargs="+")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("youtube")
    p.add_argument("action", choices=["play", "search"])
    p.add_argument("query", nargs="+")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("browser")
    p.add_argument("action")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("yt")
    p.add_argument("action")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("media")
    p.add_argument("action", choices=["status", "play-pause", "play", "pause",
                                      "next", "prev"])
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("keys")
    p.add_argument("combo")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("type")
    p.add_argument("text")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("screenshot")
    p.add_argument("path", nargs="?")

    p = sub.add_parser("clipboard")
    p.add_argument("action", choices=["get", "set"])
    p.add_argument("text", nargs="?")

    p = sub.add_parser("notify")
    p.add_argument("message")
    p.add_argument("--title", default="pc-control")
    p.add_argument("--subtitle")

    p = sub.add_parser("say")
    p.add_argument("text")
    p.add_argument("--voice")

    p = sub.add_parser("mic-test")
    p.add_argument("--seconds", type=int, default=5)

    p = sub.add_parser("speaker-test")
    p.add_argument("--measure", action="store_true")
    p.add_argument("--duration", type=float, default=1.0)

    # accept --json in trailing position too, as the docs show
    for sp in sub.choices.values():
        sp.add_argument("--json", action="store_true", dest="json",
                        default=argparse.SUPPRESS)

    a = ap.parse_args()

    if a.cmd == "battery":
        r = battery()
    elif a.cmd == "doctor":
        r = doctor()
    elif a.cmd == "volume":
        if a.action == "get":
            r = volume_get()
        elif a.action == "set":
            r = volume_set(a.value) if a.value is not None else usage_fail(
                "volume set needs a value 0-100")
        elif a.action == "up":
            r = volume_step(a.value if a.value is not None else 10)
        elif a.action == "down":
            r = volume_step(-(a.value if a.value is not None else 10))
        else:
            r = volume_mute(a.action == "mute")
    elif a.cmd == "brightness":
        if a.action == "get":
            r = brightness_get()
        elif a.action == "set":
            r = brightness_set(a.value)
        elif a.value is not None and not float(a.value).is_integer():
            r = usage_fail("steps must be a whole number")
        else:
            steps = 3 if a.value is None else int(a.value)
            r = brightness_step(a.action, steps, dry_run=a.dry_run)
    elif a.cmd == "ip":
        r = ip_info(public=a.public)
    elif a.cmd == "apps":
        r = apps_list(show_all=a.all)
    elif a.cmd == "app":
        if a.action == "front":
            r = app_front()
        elif not a.name:
            r = usage_fail("app %s needs an app name" % a.action)
        elif a.action == "open":
            r = app_open(a.name)
        elif a.action == "quit":
            r = app_quit(a.name)
        elif a.action == "running":
            r = app_running(a.name)
        else:
            r = app_activate(a.name)
    elif a.cmd == "web":
        r = web_open(" ".join(a.name), dry_run=a.dry_run)
    elif a.cmd == "youtube":
        q = " ".join(a.query)
        if a.action == "play":
            r = youtube_play(q, dry_run=a.dry_run)
        else:
            url = ("https://www.youtube.com/results?search_query="
                   + urllib.parse.quote(q))
            if a.dry_run:
                r = ok({"dry_run": True, "url": url,
                        "resolved_via": "search-page"})
            else:
                err = open_url(url)
                r = (ok({"opened": url, "resolved_via": "search-page"})
                     if not err else fail(err))
    elif a.cmd == "browser":
        r = browser_action(a.action, dry_run=a.dry_run)
    elif a.cmd == "yt":
        r = yt_action(a.action, dry_run=a.dry_run)
    elif a.cmd == "media":
        r = media_action(a.action, dry_run=a.dry_run)
    elif a.cmd == "keys":
        r = send_keys(a.combo, dry_run=a.dry_run)
    elif a.cmd == "type":
        r = type_text(a.text, dry_run=a.dry_run)
    elif a.cmd == "screenshot":
        r = screenshot(a.path)
    elif a.cmd == "clipboard":
        if a.action == "get":
            r = clipboard_get()
        else:
            r = clipboard_set(a.text) if a.text is not None else usage_fail(
                "clipboard set needs text")
    elif a.cmd == "notify":
        r = notify(a.message, title=a.title, subtitle=a.subtitle)
    elif a.cmd == "say":
        r = say_text(a.text, voice=a.voice)
    elif a.cmd == "mic-test":
        r = mic_test(seconds=a.seconds)
    elif a.cmd == "speaker-test":
        r = speaker_test(measure=a.measure, duration=a.duration)
    else:
        r = fail("unknown command")

    exit_code = 0 if r["ok"] else r.pop("_exit", 1)
    if getattr(a, "json", False):
        print(json.dumps(r, indent=2))
    else:
        if r["ok"]:
            for k, v in r.items():
                if k != "ok":
                    print("%s: %s" % (k, v))
        else:
            print("error: %s" % r["error"], file=sys.stderr)
            for k, v in r.items():
                if k not in ("ok", "error"):
                    print("%s: %s" % (k, v), file=sys.stderr)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
