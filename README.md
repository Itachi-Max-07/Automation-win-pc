# pc-control: Give an AI Agent Real Hands on Your Windows PC

> A stdlib-only Python CLI that lets any AI agent control Windows: volume, brightness, apps, browser tabs, media playback, screenshots, clipboard, notifications and hardware tests. No third party packages required, no daemon, no interactive prompts.

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero-success?style=for-the-badge)](scripts/pc.py)
[![Verified](https://img.shields.io/badge/Gate-65%20checks-brightgreen?style=for-the-badge)](scripts/gate.py)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

## What it does

Most desktop automation projects hardcode a list of phrases and guess at intent. This one does not. It exposes a clean, predictable verb catalog and lets the AI agent do the thinking. The agent decides *what* to do, this CLI is simply the hands that do it.

Every command is a single subprocess call, returns structured output, and never blocks waiting for input, which is exactly what an autonomous agent needs.

## Quick start

```bat
python scripts\pc.py doctor
python scripts\pc.py volume set 40
python scripts\pc.py brightness set 60
python scripts\pc.py app open notepad
python scripts\pc.py youtube play "lo-fi beats"
python scripts\pc.py browser new-tab
python scripts\pc.py mic-test
```

`doctor` is the one to run first. It reports which capabilities are live, which are blocked and why, and prints the fix for each.

## Features

- **System state**: battery, volume, brightness, IP address, running apps
- **App control**: launch and quit any application by name, including Start Menu and Store apps
- **Web and media**: open URLs, search YouTube, play, pause, skip tracks
- **Browser control**: new tabs, navigation, window management
- **Capture**: multi-monitor screenshots and clipboard read/write
- **Output**: toast notifications and text to speech
- **Hardware**: honest microphone and speaker tests that actually verify signal

The full verb catalog, platform limits and design notes live in [SKILL.md](SKILL.md).

## Install as a Claude Code skill

```bat
git clone https://github.com/Itachi-Max-07/Automation-win-pc.git %USERPROFILE%\.claude\skills\pc-control
```

Then invoke `/pc-control`, or just ask your agent to control the computer.

## How it works

Everything runs through `ctypes` against the Win32, WASAPI, WMI and GDI APIs, with a few PowerShell one-liners where no C API exists:

| Capability | Mechanism |
|---|---|
| Volume and mute | `IAudioEndpointVolume` (WASAPI) |
| Brightness | WMI for built in panels, DDC/CI for external monitors |
| Keystrokes and typing | `SendInput`, with Unicode injection so any layout works |
| Battery | `GetSystemPowerStatus` |
| Apps and windows | `EnumWindows`, `WM_CLOSE`, `SetForegroundWindow` |
| Screenshots | GDI `BitBlt` plus a stdlib PNG encoder, or PIL when installed |
| Clipboard | `CF_UNICODETEXT`, preserving exact whitespace |
| Notifications | Windows toast via WinRT |
| Speech | SAPI |

Optional packages are used automatically when present and are never required: **PIL** for faster screenshots, **pycaw** as an alternative volume backend, **winsdk** for exact media track metadata. **ffmpeg** is needed only for the two hardware tests.

## Platform limits

Windows does not gate these behind permission prompts, but they still decide what works:

- **UIPI**: a non-elevated process cannot send keystrokes to an elevated window. Run the agent as administrator only if you need to drive elevated apps.
- **Microphone**: Settings > Privacy & security > Microphone > "Let desktop apps access your microphone" for the mic tests.
- **Focus Assist**: Do Not Disturb silently suppresses toast banners.
- **DDC/CI**: external monitors need it enabled in their own on screen menu for brightness control.

Run `doctor` to see which of these currently apply.

## Verification

```bat
python scripts\gate.py
```

65 measured checks, including negative tests and regression locks. Volume, brightness and the clipboard are saved and restored, and the gate prints an explicit NOT COVERED list so a green run is proof of exactly what ran and nothing more. `gate.py --live` adds a real keystroke round trip through Notepad.

## Requirements

Windows 10 or 11. Python 3.9 or newer. Nothing to `pip install`.

## Contributing

Issues and pull requests welcome. New verbs should stay stdlib only and non-interactive.

## License

MIT. See [LICENSE](LICENSE).

## Author

Built and maintained by **Nuovance AI**.
