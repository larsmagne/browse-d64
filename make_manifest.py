#!/usr/bin/env python3
"""
make_manifest.py - scan a directory of .d64 images and maintain the site data.

Usage:
    python3 make_manifest.py disks/                 # scan + merge + build launch files
    python3 make_manifest.py disks/ --rescan        # re-scan all disks (overwrites program lists!)
    python3 make_manifest.py disks/ -m manifest.json -l launch/

What it does, every run:
  1. Reads the existing manifest.json if present.
  2. Scans DIR for *.d64 files and parses their directories (pure Python, no
     tools needed). Disks already present in the manifest are left completely
     untouched (so your hand edits survive); new disks are appended.
  3. Regenerates the launch/ directory from the manifest: one small .m3u or
     .cmd file per program, which the web player hands to the EmulatorJS VICE
     core. ALWAYS rerun this script after hand-editing manifest.json so the
     launch files stay in sync.

Manifest format (hand-editable):
{
  "disks": [
    {
      "file": "disks/games1.d64",        // path relative to the site root
      "name": "GAMES 1",                 // display name (from the disk header)
      "programs": [
        // Simplest case: autostarted with the equivalent of LOAD"NAME",8,1 : RUN
        { "file": "PACMAN", "label": "Pacman", "blocks": 54 },

        // Hidden from the site (data files, broken programs). Kept in the
        // manifest so a rescan never resurrects it, unlike deleting the
        // entry. "hide": true also works on a whole disk object.
        { "file": "CHARSET.DAT", "hide": true },

        // Extra VICE command-line arguments (e.g. force joystick port 1):
        { "file": "BRUCE LEE", "label": "Bruce Lee", "command": "-j1" },

        // Custom typed startup: the disk is attached and its first program
        // is loaded (but not run); VICE then types these lines at the READY
        // prompt, from inside the emulator. No pauses are needed - the C64
        // only consumes typed-ahead input when it is back at READY, so each
        // command waits for the previous one to finish.
        { "file": "MULTIVERSE", "label": "Multiverse (Simon's BASIC)",
          "commands": [
            "LOAD \"SIMONS' BASIC\",8,1",
            "RUN",
            "LOAD \"MULTIVERSE\",8",
            "RUN"
          ]
        },

        // A program that needs the keyboard once running: "keyboard": true
        // turns on the emulator's Direct Keyboard Input so keystrokes reach
        // the C64 (off by default, since joystick-on-keyboard games need the
        // default gamepad mapping instead). "options" passes any other
        // emulator settings-menu values through per program.
        { "file": "WORDPRO", "label": "Word Pro", "keyboard": true },

        // A machine-language tool started with SYS:
        { "file": "TURBOTOOL", "label": "Turbo Tool",
          "commands": [ "LOAD \"TURBOTOOL\",8,1", "SYS 49152" ],
    "keyboard": true }
      ]
    }
  ]
}

Delete a program entry (or a whole disk) from the manifest to hide it from the
site; this script will not re-add it unless you pass --rescan.
"""

import argparse
import json
import zipfile
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- d64 parsing

# sectors per track, 1-based track numbers (standard 1541 layout, up to 40 tracks)
def sectors_in_track(track: int) -> int:
    if track <= 17:
        return 21
    if track <= 24:
        return 19
    if track <= 30:
        return 18
    return 17

def sector_offset(track: int, sector: int) -> int:
    off = 0
    for t in range(1, track):
        off += sectors_in_track(t)
    return (off + sector) * 256

DIR_TRACK = 18
FILE_TYPES = {0: "DEL", 1: "SEQ", 2: "PRG", 3: "USR", 4: "REL"}

def petscii_to_ascii(data: bytes) -> str:
    """Best-effort PETSCII -> ASCII. Unshifted letters (how filenames are
    normally stored, displayed as uppercase on the C64) become uppercase
    ASCII; shifted letters become lowercase. Unmappable characters become
    '?', which conveniently is also VICE's single-character wildcard."""
    out = []
    for b in data:
        if b in (0x00, 0xA0):          # padding
            break
        if 0x20 <= b <= 0x5F:          # digits, punctuation, unshifted letters
            out.append(chr(b))
        elif 0xC1 <= b <= 0xDA:        # shifted letters -> lowercase ASCII
            out.append(chr(b - 0x60))
        else:
            out.append("?")
    return "".join(out).rstrip()

def ascii_to_petscii_case(s: str) -> str:
    """Strings handed to VICE (program names in launch files, -keybuf text)
    are converted ASCII->PETSCII by VICE, where LOWERCASE ASCII maps to the
    unshifted PETSCII the C64 shows as uppercase. So swap case on the way in;
    'PACMAN' in the manifest becomes 'pacman' in the launch file."""
    return s.swapcase()

def read_d64_directory(path: Path):
    """Return (disk_name, [ {file, type, blocks} ]) for a .d64 image."""
    data = path.read_bytes()
    if len(data) < 174848:
        raise ValueError(f"{path}: too small to be a d64 image ({len(data)} bytes)")

    # Disk name lives in the BAM sector, track 18 sector 0, offset 0x90
    bam = sector_offset(DIR_TRACK, 0)
    disk_name = petscii_to_ascii(data[bam + 0x90 : bam + 0xA0]) or path.stem.upper()

    entries = []
    track, sector = DIR_TRACK, 1
    seen = set()
    while track != 0:
        if (track, sector) in seen or len(seen) > 200:
            break                       # corrupt chain guard
        seen.add((track, sector))
        base = sector_offset(track, sector)
        if base + 256 > len(data):
            break
        block = data[base : base + 256]
        track, sector = block[0], block[1]
        for i in range(8):
            e = block[i * 32 : i * 32 + 32]
            ftype = e[2]
            if ftype == 0:
                continue                # scratched / empty slot
            kind = FILE_TYPES.get(ftype & 0x07, "???")
            name = petscii_to_ascii(e[5:21])
            blocks = e[30] + 256 * e[31]
            closed = bool(ftype & 0x80)
            entries.append({"file": name, "type": kind, "blocks": blocks,
                            "closed": closed, "ts": (e[3], e[4])})
    return disk_name, entries

def prg_load_address(data: bytes, track: int, sector: int):
    """The first two bytes of a PRG are its load address (lo, hi). They sit
    right after the 2-byte chain link in the file's first sector."""
    try:
        base = sector_offset(track, sector)
        if track == 0 or base + 6 > len(data):
            return None
        return data[base + 2] + 256 * data[base + 3]
    except Exception:
        return None

def enrich_load_addresses(manifest: dict, site_root: Path):
    """Fill in missing "load" fields for existing manifest entries without
    touching anything else, so hand-curated manifests gain the info on the
    next run instead of needing a destructive --rescan."""
    for disk in manifest["disks"]:
        d64 = site_root / disk["file"]
        needy = [p for p in disk["programs"] if "load" not in p]
        if not needy or not d64.exists():
            continue
        try:
            data = d64.read_bytes()
            _, entries = read_d64_directory(d64)
        except Exception:
            continue
        by_name = {}
        for e in entries:
            by_name.setdefault(e["file"], e)
        for p in needy:
            e = by_name.get(p["file"])
            addr = prg_load_address(data, *e["ts"]) if e else None
            if addr is not None:
                p["load"] = addr

# ------------------------------------------------------------- manifest logic

def default_label(c64_name: str) -> str:
    """PACMAN DELUXE -> Pacman Deluxe (just a nicer default for the menu)."""
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), c64_name)

def scan_disk(d64_path: Path, site_root: Path):
    disk_name, entries = read_d64_directory(d64_path)
    programs = []
    for e in entries:
        if e["type"] != "PRG" or not e["closed"]:
            continue                    # only closed PRG files are runnable
        entry = {
            "file": e["file"],
            "label": default_label(e["file"]),
            "blocks": e["blocks"],
        }
        addr = prg_load_address(d64_path.read_bytes(), *e["ts"])
        if addr is not None:
            entry["load"] = addr
        programs.append(entry)
    return {
        "file": d64_path.relative_to(site_root).as_posix(),
        "name": disk_name,
        "programs": programs,
    }

# --------------------------------------------------------- launch file output

def safe_prog_for_vice(name: str) -> str:
    """Program name as it goes into the launch file: case-swapped for VICE's
    ASCII->PETSCII conversion, and ':' wildcarded because it would break the
    disk:prog syntax."""
    return ascii_to_petscii_case(name).replace(":", "?")

def keybuf_escape(commands) -> str:
    """Turn the manifest's command list into one VICE -keybuf string.
    Each string is typed followed by RETURN. Numbers (the old pause syntax)
    are ignored: the C64 consumes the keyboard buffer only when it is back at
    the READY prompt, so commands naturally wait for the previous one.
    Escapes: '\\x22' is a quote (a literal '\"' would end the argument),
    '\\n' is RETURN, '\\\\' is a backslash."""
    lines = []
    for item in commands:
        if isinstance(item, (int, float)):
            continue
        s = ascii_to_petscii_case(str(item))
        s = s.replace("\\", "\\\\").replace('"', "\\x22")
        lines.append(s)
    return "\\n".join(lines) + "\\n"

def launch_stem(disk: dict, prog_index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(disk["file"]).stem)
    return f"{stem}__{prog_index:03d}"

def build_launch_files(manifest: dict, launch_dir: Path, site_root: Path):
    """One self-contained .zip per program: a one-line start.m3u plus the disk
    image itself. EmulatorJS extracts every file in the archive into the
    emulator's file system root, so the m3u can reference the disk by name
    (or by '/name' on a #COMMAND line). The m3u is written FIRST so
    EmulatorJS picks it as the content file.

    Three m3u forms, all verified against the VICE libretro core:
      plain autostart:  /disk.d64:progname
      extra VICE args:  #COMMAND:<args> "/disk.d64:progname"
      typed startup:    #COMMAND:-autoload "/disk.d64" -keybuf "<commands>"
    Note that a #COMMAND line REPLACES normal autostart handling, so the
    image must be part of the command itself. -autoload attaches the disk and
    loads (but does not run) its first program, which is what arms VICE's
    keyboard buffer; the -keybuf text is then typed at the READY prompt."""
    launch_dir.mkdir(parents=True, exist_ok=True)
    for old in list(launch_dir.glob("*.zip")) + list(launch_dir.glob("*.m3u")) \
            + list(launch_dir.glob("*.cmd")):
        old.unlink()                    # wipe stale files so manifest
                                        # deletions take effect
    count = 0
    for disk in manifest["disks"]:
        if disk.get("hide"):
            continue
        disk_path = site_root / disk["file"]
        disk_fs_name = disk_path.name   # filename inside the emulator FS
        if '"' in disk_fs_name:
            raise ValueError(f'{disk_fs_name}: rename this image; a quote in '
                             'the filename cannot be passed to VICE')
        disk_bytes = disk_path.read_bytes()
        for i, prog in enumerate(disk["programs"]):
            if prog.get("hide"):
                continue                # keeps index numbering stable
            stem = launch_stem(disk, i)
            extra = prog.get("command", "").strip()
            if "commands" in prog:
                line = (f'#COMMAND:{extra + " " if extra else ""}'
                        f'-autoload "/{disk_fs_name}" '
                        f'-keybuf "{keybuf_escape(prog["commands"])}"')
            elif extra:
                line = (f'#COMMAND:{extra} '
                        f'"/{disk_fs_name}:{safe_prog_for_vice(prog["file"])}"')
            else:
                line = f"/{disk_fs_name}:{safe_prog_for_vice(prog['file'])}"
            with zipfile.ZipFile(launch_dir / f"{stem}.zip", "w",
                                 zipfile.ZIP_DEFLATED) as z:
                z.writestr("start.m3u", line + "\n")  # must be the first entry
                z.writestr(disk_fs_name, disk_bytes)
            count += 1
    return count

# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("disk_dir", help="directory containing .d64 files (inside the site root)")
    ap.add_argument("-m", "--manifest", default="manifest.json")
    ap.add_argument("-l", "--launch-dir", default="launch")
    ap.add_argument("--rescan", action="store_true",
                    help="re-scan disks already in the manifest (OVERWRITES their program lists)")
    args = ap.parse_args()

    site_root = Path(args.manifest).resolve().parent
    disk_dir = Path(args.disk_dir).resolve()
    if not disk_dir.is_dir():
        sys.exit(f"error: {disk_dir} is not a directory")

    manifest_path = Path(args.manifest)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"disks": []}
    known = {d["file"]: d for d in manifest["disks"]}

    added, skipped, failed = 0, 0, 0
    for d64 in sorted(disk_dir.glob("*.d64")):
        rel = d64.relative_to(site_root).as_posix()
        if rel in known and not args.rescan:
            skipped += 1
            continue
        try:
            entry = scan_disk(d64, site_root)
        except Exception as exc:
            print(f"  warning: could not read {d64.name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if rel in known:                # --rescan: replace in place
            known[rel].update(entry)
        else:
            manifest["disks"].append(entry)
            known[rel] = entry
        added += 1

    enrich_load_addresses(manifest, site_root)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    n = build_launch_files(manifest, Path(args.launch_dir), site_root)

    print(f"scanned: {added} disk(s), kept untouched: {skipped}, failed: {failed}")
    print(f"wrote {manifest_path} and {n} launch file(s) in {args.launch_dir}/")
    print("Reminder: rerun this script whenever you hand-edit the manifest.")

if __name__ == "__main__":
    main()
