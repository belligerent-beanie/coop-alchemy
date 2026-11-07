"""Back up (and restore) the live server's data: accounts, daily challenge, drafts, invites and saved games.

    python ops/backup.py                         make a backup now (and delete old ones)
    python ops/backup.py list                    show the backups
    python ops/backup.py restore NAME --yes      put a backup back (the server must be stopped)

Backups go to <live folder>\\backups\\<date-time>[-label]\\ and hold only the data files listed in DATA_FILES, never logs
or login tokens. They contain usernames and password hashes, so treat the folder like accounts.json itself.
Each file is checked after copying, and a data file that is not valid JSON stops the backup with an error instead of
quietly preserving damage. Retention: the newest KEEP_REGULAR scheduled backups and the newest KEEP_LABELLED of each
labelled kind (for example "pre-deploy") are kept.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILES = ("accounts.json", "daily.json", "drafts.json", "invites.json", "solo.json")
DATA_DIRS = ("saves",)
KEEP_REGULAR = 14
KEEP_LABELLED = 10


class BackupError(Exception):
    pass


def default_live_dir() -> Path:
    return Path(os.environ.get("ALCHEMY_LIVE_DIR") or ROOT.parent / "coop-alchemy-live")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_backup(data_dir: Path, backups_dir: Path, label: str = "", now: float | None = None) -> Path:
    """Copy the data files into a new backup folder and return it. Raises BackupError if anything looks wrong."""
    if not data_dir.is_dir():
        raise BackupError(f"the data folder {data_dir} does not exist")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    clean_label = "".join(ch for ch in label if ch.isalnum() or ch in "-_")[:30]
    target = backups_dir / (f"{stamp}-{clean_label}" if clean_label else stamp)
    if target.exists():
        raise BackupError(f"{target.name} already exists (two backups in the same second?)")
    staging = backups_dir / (target.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    manifest: dict[str, dict[str, object]] = {}
    try:
        for name in DATA_FILES:
            source = data_dir / name
            if not source.is_file():
                continue
            try:
                json.loads(source.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise BackupError(f"{name} is not valid JSON ({exc}); fix or restore it before backing up") from exc
            copy = staging / name
            shutil.copy2(source, copy)
            if _digest(copy) != _digest(source):
                raise BackupError(f"{name} changed while it was being copied; try again")
            manifest[name] = {"bytes": copy.stat().st_size}
        for name in DATA_DIRS:
            source = data_dir / name
            if source.is_dir():
                shutil.copytree(source, staging / name)
                manifest[name + "/"] = {"files": sum(1 for p in (staging / name).rglob("*") if p.is_file())}
        (staging / "backup.json").write_text(json.dumps({"made": stamp, "label": clean_label, "contents": manifest}, indent=1),
                                             encoding="utf-8")
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def list_backups(backups_dir: Path) -> list[Path]:
    """Finished backups, oldest first."""
    if not backups_dir.is_dir():
        return []
    return sorted(p for p in backups_dir.iterdir() if p.is_dir() and not p.name.endswith(".partial"))


def prune(backups_dir: Path, keep_regular: int = KEEP_REGULAR, keep_labelled: int = KEEP_LABELLED) -> list[str]:
    """Delete old backups; returns the names removed."""
    groups: dict[str, list[Path]] = {}
    for path in list_backups(backups_dir):
        parts = path.name.split("-", 2)
        groups.setdefault(parts[2] if len(parts) == 3 else "", []).append(path)
    removed = []
    for label, paths in groups.items():
        for path in paths[: max(0, len(paths) - (keep_labelled if label else keep_regular))]:
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


def server_is_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def restore(backups_dir: Path, name: str, data_dir: Path, port: int = 8000, force: bool = False) -> Path:
    """Put backup `name` back into the data folder, after taking a "pre-restore" backup of what is there now."""
    source = backups_dir / name
    if not source.is_dir() or not (source / "backup.json").is_file():
        raise BackupError(f"there is no backup called {name!r}")
    if not force and server_is_running(port):
        raise BackupError(f"a server is running on port {port}: stop it first, or its next save would overwrite the restore")
    if data_dir.is_dir():
        make_backup(data_dir, backups_dir, "pre-restore")
    data_dir.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name == "backup.json":
            continue
        destination = data_dir / entry.name
        if entry.is_dir():
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(entry, destination)
        else:
            shutil.copy2(entry, destination)
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up and restore the live server's data")
    parser.add_argument("command", nargs="?", default="backup", choices=("backup", "list", "restore"))
    parser.add_argument("name", nargs="?", help="the backup to restore (see: list)")
    parser.add_argument("--live-dir", default=str(default_live_dir()))
    parser.add_argument("--data-dir", help="default: <live-dir>\\data")
    parser.add_argument("--backups-dir", help="default: <live-dir>\\backups")
    parser.add_argument("--label", default="", help="a tag for this backup (kept separately from the scheduled ones)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--yes", action="store_true", help="confirm a restore")
    parser.add_argument("--force", action="store_true", help="restore even though a server answers on the port")
    args = parser.parse_args(argv)
    live = Path(args.live_dir)
    data_dir = Path(args.data_dir) if args.data_dir else live / "data"
    backups_dir = Path(args.backups_dir) if args.backups_dir else live / "backups"
    try:
        if args.command == "list":
            for path in list_backups(backups_dir):
                print(path.name)
            return 0
        if args.command == "restore":
            if not args.name:
                print("say which backup to restore (python ops/backup.py list)", file=sys.stderr)
                return 2
            if not args.yes:
                print(f"This replaces the data in {data_dir} with backup {args.name}. Run again with --yes to do it.")
                return 2
            restore(backups_dir, args.name, data_dir, args.port, args.force)
            print(f"restored {args.name} (what was there before is in the newest 'pre-restore' backup)")
            return 0
        made = make_backup(data_dir, backups_dir, args.label)
        removed = prune(backups_dir)
        print(f"backed up to {made}" + (f"; removed {len(removed)} old backup(s)" if removed else ""))
        return 0
    except BackupError as exc:
        print(f"backup problem: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
