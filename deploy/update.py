#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
REMOTE_REF = "origin/main"
SERVICES = ("voiceover-studio-worker", "voiceover-studio-web")
ENV_FILE = Path("/etc/voiceover-studio.env")


def load_environment():
    if not ENV_FILE.is_file():
        raise RuntimeError(f"Production environment file is missing: {ENV_FILE}")
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def validate_production_environment():
    if os.environ.get("DJANGO_DEBUG") != "0":
        raise RuntimeError(
            "Refusing to deploy with DJANGO_DEBUG enabled. "
            "Set DJANGO_DEBUG=0 in /etc/voiceover-studio.env."
        )


def run(args, *, capture=False, dry_run=False):
    printable = " ".join(map(str, args))
    print(f"$ {printable}", flush=True)
    if dry_run:
        return ""
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def backup_database(dry_run=False):
    database = Path(os.environ.get("DJANGO_DB_PATH", "/var/lib/voiceover-studio/db.sqlite3"))
    if not database.is_file() or dry_run:
        return
    backup_dir = Path("/var/lib/voiceover-studio/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"db-{stamp}.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source:
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination)
    for obsolete in sorted(backup_dir.glob("db-*.sqlite3"), reverse=True)[5:]:
        obsolete.unlink()


def main():
    parser = argparse.ArgumentParser(description="Safely deploy Voiceover Studio")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    load_environment()
    validate_production_environment()
    if not (ROOT / ".git").exists() or not VENV.exists():
        raise RuntimeError("Run the first-time server bootstrap before deployment")

    lock_path = Path("/tmp/voiceover-studio-deploy.lock")
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Voiceover Studio deployment is already running")

        dirty = run(["git", "status", "--porcelain"], capture=True)
        if dirty:
            raise RuntimeError("Server working tree has local changes; deployment aborted")
        run(["git", "fetch", "--prune"], dry_run=args.dry_run)
        local = run(["git", "rev-parse", "HEAD"], capture=True)
        remote = run(["git", "rev-parse", REMOTE_REF], capture=True)
        print("Up to date" if local == remote else f"Update: {local[:8]} -> {remote[:8]}")
        if args.status or local == remote:
            return
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", local, remote], cwd=ROOT)
        if ancestor.returncode:
            raise RuntimeError(f"{REMOTE_REF} cannot be applied as a fast-forward")

        run(["git", "merge", "--ff-only", REMOTE_REF], dry_run=args.dry_run)
        backup_database(args.dry_run)
        python = str(VENV / "bin" / "python")
        pip = str(VENV / "bin" / "pip")
        run([pip, "install", "-r", "requirements.txt"], dry_run=args.dry_run)
        run([python, "manage.py", "check", "--deploy"], dry_run=args.dry_run)
        run([python, "manage.py", "migrate", "--noinput"], dry_run=args.dry_run)
        run([python, "manage.py", "compilemessages", "-l", "ru"], dry_run=args.dry_run)
        run([python, "manage.py", "collectstatic", "--noinput"], dry_run=args.dry_run)
        for service in SERVICES:
            run(["sudo", "systemctl", "restart", service], dry_run=args.dry_run)
        for service in SERVICES:
            run(["sudo", "systemctl", "is-active", "--quiet", service], dry_run=args.dry_run)
        print("Voiceover Studio deployed successfully")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
