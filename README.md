# GameBox by That's Life Media

One library for every game on your PC — Steam, GOG, Epic, EA App, emulated discs and
plain folders — and one button that launches them.

GameBox is a small native Windows app. It scans what is actually installed on your
drives, pulls the cover art your launchers already cached, and gives you a single
library to browse and launch from. No account, no telemetry, no background service.

One honest exception: scans fetch cover art and store info (titles, descriptions) from
Steam's public endpoints, so Valve sees your IP alongside game titles. There is a
Settings toggle ("Fetch artwork & store info online") to turn that off, or run
`scan-library.py --no-meta` to skip it.

On launch, GameBox also asks GitHub's releases API whether a newer version exists, so it
can show an "Update available" notice. That check can be turned off in Settings
("Check for updates on startup").

## Built with AI, and I am not hiding it

Yes — this was built with AI. Claude Code wrote the code. AI is a tool, the same as
a compiler or an IDE, and I used it like one. What I brought is the creative vision,
the testing, and the error-hunting: I decided what this should be, I ran it until it
broke, and I refined and fixed the bugs until it worked.

This is a free project, made for fun. No complaining, no whining. Use it or don't —
doesn't matter to me.

## Why

Most people's games are scattered across four launchers and a folder of disc images.
Every launcher wants to be the front end, none of them shows the others, and none of
them touches the ISO sitting on your D: drive. GameBox is the shelf that holds all of it.

## What it does

- **Finds your games.** Reads Steam `appmanifest` files, GOG Galaxy's database, Epic's
  manifests, and sizes every game folder on the drives you point it at.
- **Launches them properly.** Steam and Epic titles are handed to their own launchers
  through `steam://` and `com.epicgames.launcher://`. GOG, EA and folder installs run
  their executable. PS2/PS3 disc images open in PCSX2 or RPCS3.
- **Finds the artwork.** Covers come from your Steam and GOG caches first, then from
  Steam's public CDN by title match, and finally a generated plate for console discs
  and ROMs that no store carries.
- **Shows real information.** Size on disk, last played, last updated, install path,
  what's inside the install folder, cloud-save state, developer, publisher, genre.
- **A 3D case for every game.** Click a game and its case flies out of the tile; drag
  to rotate it, press `F` to flip it over. The back is a real box back — screenshots,
  blurb, feature list, credits, rating box, spec panel, barcode.
- **It knows who you are.** Setup asks for a name, and you can change it (and your
  avatar) any time in Settings. It is kept in `gamebox-settings.json` beside the app
  and never leaves the machine.

## Views

| View | For |
|---|---|
| **Grid** | Browsing by cover |
| **List** | Sorting by size, date, store, artwork source; seeing install paths |
| **Details** | One game at a time: Overview, Files, Saves, Activity |

Plus Home, Continue playing, Installed, Never played, Favorites, Collections,
an Artwork manager and Settings.

## Install

Download the latest `GameBox-Setup-*.exe` from
[Releases](https://github.com/zmaya13/gamebox/releases) and run it. It installs per
user, needs no administrator rights, and uninstalls from Add/Remove Programs.
Upgrading over an existing install keeps the library you scanned; uninstalling
asks whether to remove it, along with your settings and the browser profile that
holds favourites and hidden games. Either way your games are never touched.

Setup scans for you on first run. To scan by hand instead:

```powershell
cd "$env:LOCALAPPDATA\Programs\GameBox"
python scan-library.py --root C:\ --root D:\ --apply
```

Reopen GameBox and your library is there.

## Run from source

```powershell
git clone https://github.com/zmaya13/gamebox
cd gamebox
pip install -r requirements.txt
python scan-library.py --root C:\ --apply
python gamebox.py
```

## Build

```powershell
./build.ps1     # needs pyinstaller and NSIS
```

Produces `GameBox.exe` and the matching `GameBox-Setup-<version>.exe`.

## How it is put together

```
gamebox.py         native window (pywebview + WebView2) and the launch API
GameBox.html       the whole UI: one file, no framework, no build step
scan-library.py    the scanner: finds games, resolves artwork, merges the library
library.py         the library store: stable ids, the merge, backups
emulators.py       which emulators are installed, and how each starts a game
stores.py          Xbox, Ubisoft, EA, Battle.net, Amazon and itch.io
playtime.py        watches a launched game and records how long it ran
migrate.py         one-way upgrade from the pre-1.3 in-page library
emulation/         Playnite's emulator definitions (MIT - see its NOTICE)
installer.nsi      NSIS installer
```

The UI is plain HTML, CSS and JavaScript in a single file. Anything that touches the
machine — launching a game, opening a folder, rescanning — is a Python call over the
pywebview bridge. There is no local web server and no listening port, and closing the
window ends the process.

The library lives in `library.json` beside the app, with its artwork in `art/`.
Each game is keyed by something that does not move - a Steam appid, an Epic app
name, a GOG product id, or a hash of its install path - so a rescan *refreshes* a
game rather than replacing it. Anything you record about a game (favourite, tags,
notes, completion, score, playtime) is kept in a separate part of the record that
the scanner never touches, and a game you uninstall is marked as such rather than
deleted, so it keeps what you wrote about it. Every scan takes a timestamped
backup into `library-backups/` first.

Before 1.3 the library was a `const G=[...]` array spliced into `GameBox.html`
and keyed by scan order, with launch targets in a parallel `launch.json`. An
existing install is migrated to `library.json` the first time 1.3 runs.

## Stores

Everything is read from what each launcher has already written to this PC — a
registry key, a manifest, a small database. Nothing signs in or contacts a
server, so GameBox reports the games **installed** here rather than the ones you
own.

| Store | Read from | Started with |
|---|---|---|
| Steam | `appmanifest_*.acf` | `steam://rungameid/<id>` |
| GOG Galaxy | `galaxy-2.0.db` | the game's executable |
| Epic | `.item` manifests | `com.epicgames.launcher://` |
| Xbox / Game Pass | `XboxGames\*\Content\MicrosoftGame.config` | `shell:appsFolder\<pfn>` |
| Ubisoft Connect | `Ubisoft\Launcher\Installs` in the registry | `uplay://launch/<id>/0` |
| EA app | `Electronic Arts` / `EA Games` in the registry, plus `installerdata.xml` | `origin2://game/launch` |
| Battle.net | each install's `.build.info` | `battlenet://<code>` |
| Amazon Games | `GameInstallInfo.sqlite` | `amazon-games://play/<id>` |
| itch.io | `butler.db` | the game's executable |
| Folders and ROMs | walking the drives you pick | see Emulators |

A packaged Xbox game will not start from its own executable — it has to go
through the shell so Windows gives it its package identity.

## Rescanning

A rescan is around **ten times faster** than the first scan. Sizing every folder
and re-fetching every cover was almost all of the runtime, and neither changes
between two scans: a folder's size is kept with its mtime and only re-measured
when something has touched it, and a cover already on disk is kept. Generated
placeholder plates are always retried, in case a game can be matched properly
now.

## Making the library yours

Anything you record about a game lives in `library.json` in its own section, which
the scanner never touches — so a rescan refreshes what it found and leaves the
rest alone, and an uninstalled game keeps everything you wrote about it.

- **Completion status** — Not played, Plan to play, Playing, On hold, Beaten,
  Completed, Abandoned. Each one in use becomes a filter chip.
- **Tags and categories**, free-form, with the ones you already use offered so
  they don't get retyped slightly differently. Both become sidebar rows.
- **A score** out of 100, and **a note**.
- **Saved views** — a search, a set of filters and a sort, kept under a name.
- **Add a game by hand**, for anything the scanner cannot see: a shortcut, a
  launcher link, an old install. A scan never removes it.
- **Never import again** — remove something the scanner keeps mistaking for a
  game, and it stays gone.

Search covers your own words too: tags, categories, statuses and notes, not just
titles and paths.

## Fullscreen and a controller

**F11**, the button in the title bar, or **Start** on a controller switches to
fullscreen: the chrome goes away, the covers roughly double, and the selected
one is outlined clearly enough to read from a sofa. It is the same page in a
different mode rather than a second application — the layout was already sized
in container units against the window, so it scales rather than breaking.

A gamepad drives the whole library:

| | |
|---|---|
| D-pad / left stick | move, with repeat while held |
| **A** | play |
| **X** | details |
| **Y** | favourite |
| **B** | back, then out of fullscreen |
| **LB** / **RB** | All games · Continue · Installed · Never played · Favorites |
| **Start** | fullscreen on and off |
| **Back** | search |

Grid navigation measures the real number of columns rather than assuming one,
so arrow keys and the d-pad both work at any window size and in either mode.

## Playtime

Every game is timed, not just the ones a store keeps a record for. Launching a
Steam or Epic game hands off to the launcher and leaves no process to wait on,
so GameBox watches the game's **install folder** instead — anything running from
inside it is the game. A session is recorded when it exits, with its length,
and a game running right now shows a live badge on its tile.

Sessions live in the library beside your tags and favourites, so they survive a
rescan, and an uninstalled game keeps the hours you put into it.

## Emulators

GameBox reads Playnite's emulator definitions: **92 emulators, 561 profiles and
326 ROM extensions**, vendored in `emulation/` and compiled into `emulators.json`
at build time. Each profile carries the platforms it covers, the extensions it
accepts, a pattern matching its executable, and — the part that matters — the
command line to start a game with, so PCSX2-Qt gets `-fullscreen -slowboot --`
rather than a bare path.

Where a file cannot say which console it belongs to (a `.iso` is a PS2 disc or a
PS3 disc), the folder it sits in decides; you can override that per game from
the inspector. PlayStation 3 games are folders rather than images, and are
recognised by shape and handed to RPCS3.

Those definitions are Playnite's and are used under the MIT licence — the full
notice is in [emulation/NOTICE](emulation/NOTICE).

## Checking a build

A build gets things wrong that running from source cannot: the scanner is loaded
by path, so PyInstaller cannot see what it imports; Pillow is imported inside
functions; the emulator definitions are packed inside the exe; and the page has
to end up beside the artwork it references. The exe can check itself:

```bash
GameBox.exe --selftest                    # imports, definitions, page placement
GameBox.exe --selftest --root Z:\ --apply # ...and a real scan
GameBox.exe --uitest                      # open the window, confirm it drew the library
```

Results also go to `selftest.log` beside the exe, since a windowed build has no
console to print to. `python tools/test_frozen.py` drives all of that against a
built exe in throwaway folders, in the two states a real machine is in: a fresh
install, and an upgrade from 1.2.x with the library still inside `GameBox.html`.

## Requirements

- Windows 10 or 11 (the WebView2 runtime ships with Windows 11)
- Python 3.9+ and Pillow, for the scanner
- PyYAML, only to rebuild `emulators.json` from `emulation/`

## A note on artwork

GameBox displays covers, screenshots and descriptions for games you already own. That
material is fetched on your machine at scan time — from your own launcher caches and
from Steam's public endpoints — and belongs to its publishers. **None of it is
distributed with this repository**, and both `library.json` and the `art/` folder
it refers to are deliberately git-ignored.

## Known limits

- Windows only.
- The scanner matches non-Steam titles by name, so an oddly named folder can miss.
- Stores are read locally, so GameBox lists the games **installed** on this PC,
  not everything you own.
- Playtime history only exists for Steam games; other stores expose no play record,
  which the UI reports as "no play record" rather than "never played".
- Xbox / Game Pass and Ubisoft Connect are not implemented yet.

## Licence

MIT — see [LICENSE](LICENSE).
