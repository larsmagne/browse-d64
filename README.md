# C64 disk library — a browser front-end for your .d64 collection

A small static website that presents a menu of your .d64 disk images, lists the
programs on each one, and runs the program you click in an in-page C64 emulator
(EmulatorJS with the VICE x64sc core).

## Layout

```
your-site/
  index.html          the menu page (open this)
  player.html         the emulator page (loaded in an iframe by index.html)
  style.css
  make_manifest.py    scans disks/ and (re)builds manifest.json + launch/
  manifest.json       generated, then hand-edited by you
  disks/              put your .d64 files here (input only; not needed on the server)
  launch/             generated launch bundles, one .zip per program (don't edit)
```

## Quick start

1. Copy your .d64 files into `disks/`.
2. Run the generator:

   ```
   python3 make_manifest.py disks/
   ```

   This creates `manifest.json` (every closed PRG file on every disk) and a
   `launch/` directory with one small zip per program, each containing a
   one-line VICE playlist plus the disk image.
3. Serve the directory over HTTP and open it:

   ```
   python3 -m http.server 8000
   # then browse to http://localhost:8000/
   ```

   It must be HTTP, not file:// — the pages fetch `manifest.json` and the
   emulator downloads the launch bundles. Any static host works for publishing
   (GitHub Pages, nginx, S3, ...). Only `index.html`, `player.html`,
   `style.css`, `manifest.json`, and `launch/` need to be uploaded; the raw
   `disks/` folder is input for the generator, not for the site.
4. Click a disk, click a program, press the start button in the emulator.

## Editing the manifest

`manifest.json` is meant to be hand-edited. After every edit, rerun
`python3 make_manifest.py disks/` — it leaves your edits alone (disks already
in the manifest are not re-scanned) but regenerates the `launch/` bundles,
which must stay in sync with the manifest.

Per-program options:

- **`"prod": true`** (disks only) — publication switch: on any host other
  than localhost, only disks marked `"prod": true` are listed. On localhost
  every non-hidden disk shows, so you can curate and test the whole
  collection locally and promote disks one by one as they're verified.
  `"hide": true` beats `"prod": true`. Unlisted disks remain reachable by
  direct link (`#disk=...`), so you can share a URL to something without
  putting it in the public menu; only `"hide"` makes a disk unreachable.
  A program link (`#disk=...&prog=...`) to an unlisted disk lists only that
  program on the public host — though note a disk link without the prog
  part still lists the whole disk, so the containing image is discoverable
  by editing the URL.
  Note this is menu visibility, not access control: launch bundles for
  unlisted disks still exist on the server if you upload them.
- **`"hide": true`** — keeps the entry in the manifest but off the site: no
  menu listing and no launch bundle generated. Made for curating: mark data
  files and broken programs as you test your way through a collection,
  flipping the flag back if you were wrong. Works on a whole disk object
  too, and a disk with no visible programs disappears from the menu on its
  own. (Deleting an entry outright also works, but hiding is
  self-documenting and survives your own memory better.)
- **`"label"`** — the name shown in the menu.
- **`"command"`** — extra VICE command-line arguments, e.g. `"-j1"` to put the
  joystick on port 1 for that program.
- **`"load"`** — filled in automatically: the program's load address, read
  from the first two bytes of the PRG on disk. The menu shows it (hex and
  decimal) for anything that isn't a normal BASIC program at $0801 — for
  machine code loaded with `,8,1`, the load address is usually what you
  `SYS`. Existing manifests gain the field on the next generator run; no
  rescan needed.
- **`"keyboard": true/false`** — controls the emulator's Direct Keyboard
  Input: on, keystrokes go to the C64; off, keys drive the virtual gamepad
  (how joystick games are played on a keyboard). Unset, it is decided
  automatically: on for machine-language files loading above $0801 (they
  land at READY waiting for a SYS), off for self-starting BASIC programs at
  $0801 and for files loading below it (those overwrite system vectors and
  autostart on their own). Set it explicitly to override either way — e.g.
  `"keyboard": true` for a BASIC text adventure. Visitors can also toggle it
  live under the emulator's settings menu ("Direct Keyboard Input").
- **`"options"`** — an object of other emulator settings-menu defaults to
  apply for this program, e.g. `{ "shader": "crt-mattias.glslp" }`.
- **`"commands"`** — replaces the default load-and-run with a typed startup
  sequence. The disk is attached, its first program is loaded (but not run),
  and then VICE itself types your commands at the READY prompt, exactly as if
  someone sat at the keyboard. Each string is typed followed by RETURN. No
  pauses are needed: the C64 only consumes typed-ahead input when it returns
  to READY, so each command naturally waits for the previous one — including
  slow disk loads. (Numbers in the list, the old pause syntax, are ignored.)

  ```json
  { "file": "MULTIVERSE", "label": "Multiverse (needs Simon's BASIC)",
    "commands": [
      "LOAD \"SIMONS' BASIC\",8,1",
      "RUN",
      "LOAD \"MULTIVERSE\",8",
      "RUN"
    ] }
  ```

  A SYS-started machine language tool looks like:

  ```json
  { "file": "TURBOTOOL",
    "commands": [ "LOAD \"TURBOTOOL\",8,1", "SYS 49152" ] }
  ```

## How it works

- Each launch zip contains the disk image and a one-line `start.m3u` playlist
  that the VICE libretro core understands. EmulatorJS downloads the zip and
  extracts both files into the emulator's file system root. Three playlist
  forms are generated, all tested against the core:
  - `/disk.d64:progname` — autostart that specific program (the equivalent of
    `LOAD"PROGNAME",8,1` + `RUN`).
  - `#COMMAND:<args> "/disk.d64:progname"` — same, with extra VICE arguments.
    (A `#COMMAND:` line replaces the normal autostart, so the image has to be
    part of the command itself.)
  - `#COMMAND:-autoload "/disk.d64" -keybuf "..."` — for `"commands"`
    programs: attach and arm the keyboard buffer, then VICE types the startup
    lines itself.
- Program names and typed commands are case-swapped on the way into launch
  files (`PACMAN` → `pacman`): VICE converts ASCII to PETSCII, where lowercase
  ASCII is what matches the unshifted filenames on a normal disk. The
  generator handles this; write commands in the manifest the way they'd look
  on the C64 screen.
- Load warp is enabled by default (`vice_autoloadwarp` in `player.html`), so
  even multi-step typed startups with real 1541 loads finish quickly.
- EmulatorJS is loaded from `cdn.emulatorjs.org`. To self-host it instead
  (recommended for a site you want to keep working long-term), download a
  release from https://github.com/EmulatorJS/EmulatorJS, put its `data/`
  folder next to `index.html`, and change `EJS_pathtodata` and the loader
  `<script src=...>` in `player.html` to `data/`.

## Fair warning about content

The emulator core has the Commodore system ROMs built in, and the programs on
your disks have their own copyright status. For homebrew, your own software,
and freely distributable titles this is a non-issue; think twice before
putting commercial games on a public URL.

## Troubleshooting

- **Changes don't show up (site behaves like the old version)** — browsers
  cache the HTML and JS files aggressively. Hard-refresh once after deploying
  (Ctrl+Shift+R / Cmd+Shift+R); the pages themselves cache-bust the manifest
  and the player iframe from then on. EmulatorJS additionally caches game
  bundles in the browser (IndexedDB) — its in-emulator cache manager can
  clear those.
- **The emulator area turns black and keeps growing taller** — this was a
  resize feedback loop fixed in the current player.html (the emulator is now
  pinned to the iframe viewport, so its size can't depend on its own
  content). If you see it, you are running a cached old player.html; see the
  previous item.
- **Menu says it can't load manifest.json** — you opened the page via file://
  or haven't run the generator yet.
- **Wrong program starts, or "file not found"** — you edited `manifest.json`
  and forgot to rerun `make_manifest.py`, so `launch/` is stale. Rerun it.
- **A program with odd characters in its PETSCII name won't load** — the
  generator turns unmappable characters into `?`, which VICE treats as a
  single-character wildcard; if that still doesn't match, shorten the name in
  the manifest and end it with `*`.
- **The emulator boots to READY and nothing happens** — the launch bundle's
  disk reference didn't resolve. Rerun the generator (don't edit files in
  `launch/` by hand), and hard-refresh the page: EmulatorJS caches game files
  in the browser, so a stale cached bundle can outlive your fix. The cache
  manager in the emulator's menu can clear it.
- **Keyboard input goes to the page instead of the C64** — click inside the
  emulator screen first.
- **Loads are slow** — the site defaults load warp to its strongest setting
  ("Automatic Load Warp: mute", which fast-forwards all disk access and
  silences audio meanwhile). If a program still loads at real 1541 speed,
  check the emulator's settings menu under Media: per-game settings persist
  in the browser's localStorage, so a value saved during earlier testing
  overrides the site default. To hear a title's loading music at authentic
  speed instead, set `"options": { "vice_autoloadwarp": "disabled" }` on that
  program in the manifest.
