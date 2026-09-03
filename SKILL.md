---
name: pc-control
description: Hands on the local Windows PC for any agent - system state (battery, volume, brightness, IP), app launch/quit, open websites and YouTube, media and browser control via keystrokes, screenshots, clipboard, notifications, TTS, and honest mic/speaker hardware tests. Use whenever the user asks to control the computer, change volume or brightness, open an app or site, play or pause media, check battery/IP/running apps, take a screenshot, or test the mic or speakers. The agent is the intent brain; this skill is only the hands.
---

# pc-control

One stdlib-only CLI gives the agent verified hands on this Windows PC. No
third party Python packages are required, no daemon, no interactive prompts.
Every verb takes arguments, returns useful text (or `--json`), and exits
nonzero on failure with a fix suggestion.

```bash
python %USERPROFILE%\.claude\skills\pc-control\scripts\pc.py <command> [args] [--json]
```

## Start here

1. `pc.py doctor` - capability report. Run this first in a new environment;
   it tells you exactly which verbs are live and which are blocked, with the
   fix for each. Optional accelerator packages are reported separately from
   real failures.
2. `pc.py <family> <action> --dry-run` - keystroke and URL verbs print what
   they would do without doing it. Use this when unsure.

## Verbs

### System state
| Command | Does |
|---|---|
| `battery` | percent, charging state, source, time remaining |
| `volume get\|set N\|up [N]\|down [N]\|mute\|unmute` | system output volume, 0-100, through the audio endpoint API |
| `brightness get\|set V\|up [steps]\|down [steps]` | absolute 0-100. WMI drives built in panels, DDC/CI drives external monitors. Each step is 10 points, default 3 steps |
| `ip [--public]` | local address and adapter name; `--public` adds the WAN address |
| `apps [--all]` | apps that own a visible window, or every process with `--all` |
| `doctor` | environment and capability audit |

### Apps
| Command | Does |
|---|---|
| `app open NAME` | launch by name. Resolution chain: alias table, PATH, the App Paths registry key, the Start Menu, then the UWP app list. The reply says which link resolved it |
| `app quit NAME` | graceful `WM_CLOSE` to every window, `taskkill` (unforced) as a backstop; no-op if not running |
| `app activate NAME` | bring to front, working around the foreground lock |
| `app front` | name, exe and window title of the foreground app |
| `app running NAME` | true/false |

### Web and media sources
| Command | Does |
|---|---|
| `web NAME_OR_URL` | resolve and open: URL > cached name (assets/websites.json, 185 seeded) > https://name.com reachability guess (cached on success) > Google search page. Never scrapes search engines. |
| `youtube play QUERY` | fetches YouTube results, extracts the first videoId, opens the watch page directly; falls back to the results page offline |
| `youtube search QUERY` | opens the results page |

### Keystroke families (see Platform limits)
| Command | Does |
|---|---|
| `browser ACTION` | 20 actions: new-tab, close-tab, reopen-tab, next-tab, prev-tab, new-window, private-window, zoom-in/out/reset, refresh, hard-refresh, back, forward, history, bookmarks, address-bar, find, devtools, fullscreen. Sent to the frontmost app; mappings are Chrome-family and work in Edge, Chrome, Firefox and Brave. |
| `yt ACTION` | 22 YouTube transport actions: play-pause, mute, fullscreen, theater, miniplayer, captions, vol-up/down, back-5/fwd-5, back-10/fwd-10, prev-frame/next-frame, speed-up/down, start, end, prev-chapter/next-chapter, next-video/prev-video. Focus a browser with YouTube first. |
| `media status\|play-pause\|play\|pause\|next\|prev` | play-pause, next and prev use the media keys, which the shell routes to the active player even when it is not focused. Absolute play and pause use `WM_APPCOMMAND`. `status` reads the real track and artist from the system media session when winsdk is installed, otherwise from the player window title |
| `keys COMBO` | raw hand, e.g. `keys ctrl+shift+t`, `keys f11`, `keys win+d` |
| `type TEXT` | types literal text into the frontmost app as Unicode, so any character and any keyboard layout works |

### Desktop utilities
| Command | Does |
|---|---|
| `screenshot [PATH]` | full virtual-screen capture (all monitors). Defaults to the real Desktop folder, which is resolved through the shell so OneDrive redirection is handled |
| `clipboard get\|set TEXT` | read or write the clipboard, preserving exact whitespace and Unicode |
| `notify MSG [--title T] [--subtitle S]` | Windows toast notification |
| `say TEXT [--voice V]` | speak through the speakers using SAPI. An unknown voice is rejected and the installed voices are listed |

### Hardware tests
| Command | Does |
|---|---|
| `mic-test [--seconds N]` | records via ffmpeg, reports peak, clipping %, noise floor, SNR, and a verdict (detects silent/dead/blocked mic) |
| `speaker-test [--measure] [--duration S]` | plays 4 tones plus a log sweep. Without `--measure` it says so honestly: playback is not proof of sound. With `--measure` it records through the mic during playback and reports which tones were actually heard. |

## Platform limits (Windows has no permission prompts for these)

`doctor` detects all of these and prints the same fixes.

1. **UIPI and elevation** (keystroke families): a non-elevated process
   cannot send input to an elevated window. Nothing to grant; either click a
   non-elevated window first, or run the agent as administrator to drive
   elevated apps. Blocked injection fails with that fix printed.
2. **Microphone** (mic-test, speaker-test --measure): Settings > Privacy &
   security > Microphone > "Let desktop apps access your microphone".
3. **Focus Assist** (notify): Do Not Disturb suppresses the toast banner
   with no error. The verb still reports success because the toast was
   accepted by the system.
4. **DDC/CI** (brightness on external monitors): must be enabled in the
   monitor's own on screen menu. Built in laptop panels answer on WMI
   instead and need nothing.
5. **Interactive session**: input injection, screenshots and the clipboard
   need a real desktop session, so none of it works from a service or a
   session 0 context.

## Verification

`scripts/gate.py` is the measured gate: 65 checks including negative tests
and regression locks, with volume, brightness and the clipboard saved and
restored. `gate.py --live` adds a real keystroke round trip through Notepad
(type > select > copy > compare clipboard). The gate prints a NOT COVERED
list; treat anything on that list as unverified.

## Design notes and limits

- The calling agent is the intent recognizer. This skill deliberately has no
  phrase dictionary, no fuzzy matcher and no natural language parsing: the
  agent thinks, the skill acts.
- Windows backend only, and stdlib only by default. Everything works through
  ctypes against the Win32, WASAPI, WMI and GDI APIs plus a few PowerShell
  one-liners. PIL, pycaw and winsdk are used automatically when they happen
  to be importable (faster screenshots, an alternative volume backend, exact
  media metadata) and are never required. ffmpeg is needed only for the two
  hardware tests.
- Keystroke verbs act on the **frontmost app**, blind. Activate the right
  app first (`app activate NAME`) and prefer `--dry-run` when exploring. A
  dry run returns `input_plan`, a readable account of the key down and key
  up events that would be injected.
- `cmd+...` combos are rejected rather than silently remapped: use `ctrl`
  for shortcuts, or `win` for the Windows key.
- Brightness is an integer percent. A value between 0 and 1 is rejected with
  a pointer to the percent scale rather than being applied as 1%.
- `browser history/bookmarks/devtools` are Chrome-family shortcuts; Firefox
  differs for a few.
- `clipboard` deliberately avoids `clip.exe` and `Get-Clipboard`, both of
  which corrupt trailing whitespace.
