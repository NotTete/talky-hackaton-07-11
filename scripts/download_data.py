#!/usr/bin/env python3
"""Download the hackathon data and lay it out so the pipeline can find it.

Why this script exists
----------------------
The event serves five archives with overlapping content: the docs archive carries the
masters, the one-month sample and the spec; the phase archives carry a whole phase each.
Unpacked naively they all land at the top level, mixed with the downloads, and you can
no longer tell what is *original*, what is *derived*, or which phase a directory belongs
to. This script keeps those concerns apart:

    data/
    ├── archives/                  exactly what the server served (never edited)
    │   ├── bankrec-docs-and-masters.zip
    │   ├── bankrec-train-lite.tar.gz
    │   ├── bankrec-train.tar.gz
    │   ├── bankrec-live.tar.gz
    │   └── bankrec-final.tar.gz
    ├── manifest.json              url, bytes, sha256, extraction log
    ├── masters/                   shared master data (snapshot 2025-12-31)
    ├── sample/                    one labelled month, from the docs archive
    ├── train/                     four labelled years
    ├── train-lite/                four labelled years, raw files only from 2024-07
    ├── live/                      2025-07..12, no golden
    └── final/                     2026-01..06, released late, ships masters_final/

Design rules
------------
1. **Archives are kept by default.** They are the provenance of every number you
   report, and re-extracting must never require 1.2 GB of downloads again. Delete them
   with `--no-keep-archives` if disk is tight.
2. **Extraction is idempotent and atomic.** Downloads go to `<file>.part` and are
   renamed only when complete (Range requests resume them), extraction happens in
   `data/.tmp/` and is then merged into place, so an interrupted run never leaves a
   half-populated phase directory that silently scores wrong.
3. **`manifest.json` is the truth about what you have.** It stores the sha256 of each
   archive and where it was extracted, so `--verify` can answer "is this the data the
   server served?" without walking 200 000 files.
4. **Nothing is guessed.** Phase directories are named after the phases in the docs;
   `train-lite` stays separate from `train` because it is a *subset of raw files*, not a
   subset of tables — renaming it to `train/` would silently change what a TRAIN score
   means.

Usage
-----
    python scripts/download_data.py --list                 # sizes + what is already here
    python scripts/download_data.py                        # docs + sample (~10 MB)
    python scripts/download_data.py --phases train-lite     # + 460 MB
    python scripts/download_data.py --phases live,final     # + 87 MB (+ final when released)
    python scripts/download_data.py --all                   # everything published
    python scripts/download_data.py --verify                # re-hash the archives
    python scripts/download_data.py --force --phases live   # re-download and re-extract
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SOURCE = "https://hackathon.usetalky.com/data/v1"
DEFAULT_DEST = Path(__file__).resolve().parents[1] / "data"
CHUNK = 1024 * 1024


# --------------------------------------------------------------------- model


@dataclass(frozen=True)
class Archive:
    filename: str
    #: phases this archive provides (`sample` comes from the docs archive)
    phases: tuple[str, ...]
    #: human note for `--list`
    note: str
    #: expected size in MB, only used when the server cannot be reached
    approx_mb: int
    #: final phase data may not be published yet
    optional: bool = False

    @property
    def url_name(self) -> str:
        return self.filename


ARCHIVES: tuple[Archive, ...] = (
    Archive("bankrec-docs-and-masters.zip", ("sample",), "masters + 1-month sample + spec", 10),
    Archive("bankrec-train-lite.tar.gz", ("train-lite",), "4 labelled years, raw files from 2024-07", 460),
    Archive("bankrec-train.tar.gz", ("train",), "4 labelled years, full raw statements + PDFs", 657),
    Archive("bankrec-live.tar.gz", ("live",), "2025-H2, no golden, leaderboard phase", 87),
    Archive("bankrec-final.tar.gz", ("final",), "2026-H1, released late, one scored submission", 220, optional=True),
)

#: phase -> archive that provides it (sample is inside the docs archive)
PHASE_SOURCE = {phase: archive for archive in ARCHIVES for phase in archive.phases}

#: top-level directories an archive may contain -> where they belong
PHASE_DIRS = {"masters", "sample", "train", "live", "final"}
DOC_FILES = {"README.md", "README_EN.md", "POLICIES.md", "FORMAT.md", "DATA_DICTIONARY.md"}


@dataclass
class Manifest:
    path: Path
    source: str = DEFAULT_SOURCE
    archives: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, source: str) -> Manifest:
        if path.exists():
            raw = json.loads(path.read_text())
            return cls(path=path, source=raw.get("source", source), archives=raw.get("archives", {}))
        return cls(path=path, source=source)

    def save(self) -> None:
        payload = {
            "source": self.source,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "archives": dict(sorted(self.archives.items())),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.path)


# ------------------------------------------------------------------ helpers


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def remote_size(url: str, timeout: int = 30) -> int | None:
    """Content-Length of the archive, or None when it is not published yet."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            return int(length) if length else None
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404, 410):
            return None
        raise


def download(url: str, target: Path, *, resume: bool = True, quiet: bool = False) -> None:
    """Download with resume support and an atomic rename at the end."""
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if (resume and part.exists()) else 0

    headers = {"User-Agent": "talky-bankrec-downloader/1"}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # already complete
            part.replace(target)
            return
        raise

    total = int(response.headers.get("Content-Length") or 0) + have
    mode = "ab" if have and response.status == 206 else "wb"
    if mode == "wb":
        have = 0
    last = 0.0
    with part.open(mode) as fh:
        while True:
            block = response.read(CHUNK)
            if not block:
                break
            fh.write(block)
            have += len(block)
            if not quiet and time.time() - last > 0.4:
                pct = f"{100 * have / total:5.1f} %" if total else "   ?   "
                line = f"  {target.name}: {pct}  {human(have)}"
                if sys.stderr.isatty():
                    print(f"\r{line}", end="", file=sys.stderr, flush=True)
                elif have - getattr(download, "_last_printed", 0) > max(total // 10, CHUNK):
                    download._last_printed = have
                    print(line, file=sys.stderr, flush=True)
                last = time.time()
    if not quiet:
        print(f"  {target.name}: done  {human(have)}", file=sys.stderr)
    part.replace(target)


# ------------------------------------------------------------------ extract


def dest_relative_root(dest: Path) -> Path:
    """Manifest paths are stored relative to the repo root (``data/…``, ``docs/…``)."""
    return dest.parent if dest.name == "data" else dest


def normalise_manifest(manifest: Manifest, dest: Path) -> bool:
    """Repair paths recorded by older versions (relative to data/ instead of the repo)."""
    root = dest_relative_root(dest)
    changed = False
    for record in manifest.archives.values():
        fixed = []
        for stored in record.get("extracted", []):
            if (root / stored).exists():
                fixed.append(stored)
            elif (dest / stored).exists():
                fixed.append(str((dest / stored).relative_to(root)))
                changed = True
            else:
                fixed.append(stored)
        record["extracted"] = sorted(set(fixed))
    return changed


def _safe_tar_members(archive: tarfile.TarFile, dest: Path):
    for member in archive.getmembers():
        resolved = (dest / member.name).resolve()
        if not str(resolved).startswith(str(dest.resolve())):
            raise RuntimeError(f"unsafe path in {archive.name}: {member.name}")
        yield member


def extract(archive_path: Path, tmp: Path) -> None:
    """Extract into a scratch directory (no path traversal, no surprises)."""
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.namelist():
                resolved = (tmp / member).resolve()
                if not str(resolved).startswith(str(tmp.resolve())):
                    raise RuntimeError(f"unsafe path in {archive_path.name}: {member}")
            zf.extractall(tmp)
    elif archive_path.name.endswith(".tar.gz"):
        with tarfile.open(archive_path) as tf:
            tf.extractall(tmp, members=_safe_tar_members(tf, tmp), filter="data")
    else:
        raise RuntimeError(f"unsupported archive type: {archive_path.name}")


def place(
    extracted: Path,
    dest: Path,
    docs_dest: Path,
    *,
    phase_override: str | None = None,
) -> list[str]:
    """Move the known top-level entries into the documented layout.

    Phase data goes to ``data/<phase>/``; the spec and the official starter go to the
    repo's ``docs/`` (they are text, they are versioned, and they belong next to the
    code that implements them — not next to the 1 GB of CSV).

    Returns the destinations relative to the repo root, for the manifest.
    """
    placed: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    docs_dest.mkdir(parents=True, exist_ok=True)
    root = dest_relative_root(dest)

    for entry in sorted(extracted.iterdir()):
        name = entry.name
        if name.startswith("._"):  # macOS resource forks, 188k of them in TRAIN
            continue
        target: Path | None = None

        if entry.is_dir() and name in PHASE_DIRS:
            # train-lite ships as `train/` inside its own archive
            target = dest / (phase_override or name)
        elif entry.is_dir() and name == "starter":
            target = docs_dest / "official-starter"
        elif entry.is_file() and name in DOC_FILES:
            target = docs_dest / name
        elif entry.is_dir() and name == "docs":
            target = docs_dest

        if target is None:
            print(f"  (skipping unexpected entry: {name})", file=sys.stderr)
            continue

        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("._*"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
        placed.append(str(target.relative_to(root)))
    return sorted(set(placed))


# --------------------------------------------------------------------- main


def phases_requested(args: argparse.Namespace) -> list[str]:
    if args.all:
        return [a for archive in ARCHIVES for a in archive.phases]
    if not args.phases:
        return ["sample"]
    requested = [p.strip() for p in args.phases.split(",") if p.strip()]
    unknown = [p for p in requested if p not in PHASE_SOURCE]
    if unknown:
        raise SystemExit(f"unknown phase(s): {', '.join(unknown)} (choose from {', '.join(sorted(PHASE_SOURCE))})")
    return requested


def archives_for(phases: list[str]) -> list[Archive]:
    wanted = {PHASE_SOURCE[p] for p in phases}
    # sample lives inside the docs archive, which also brings masters + the spec
    wanted.add(ARCHIVES[0])
    return [a for a in ARCHIVES if a in wanted]


def cmd_list(dest: Path, manifest: Manifest) -> int:
    print(f"source {manifest.source}")
    print(f"dest   {dest}\n")
    print(f"{'archive':<34}{'phase':<12}{'size':>11}  {'local':<8} status")
    for archive in ARCHIVES:
        record = manifest.archives.get(archive.filename, {})
        local = (dest / "archives" / archive.filename)
        if record.get("bytes") or local.exists():
            size = human(record.get("bytes") or local.stat().st_size)
        elif archive.optional:
            size = "—"
        else:
            size = f"~{human(archive.approx_mb * 1_000_000)}"

        state = "kept" if local.exists() else "—"
        if record.get("extracted"):
            status = "extracted -> " + ", ".join(record["extracted"][:2]) + ("…" if len(record["extracted"]) > 2 else "")
        elif local.exists():
            status = "downloaded, not extracted"
        elif archive.optional:
            status = "published late (see event page)"
        else:
            status = "not downloaded"
        print(f"{archive.filename:<34}{archive.phases[0]:<12}{size:>11}  {state:<8} {status}")
    return 0


def cmd_verify(dest: Path, manifest: Manifest) -> int:
    bad = 0
    for archive in ARCHIVES:
        record = manifest.archives.get(archive.filename)
        path = dest / "archives" / archive.filename
        if not record:
            continue
        if not path.exists():
            print(f"MISSING  {archive.filename}")
            bad += 1
            continue
        actual = sha256(path)
        ok = actual == record.get("sha256")
        print(f"{'OK      ' if ok else 'CORRUPT '}{archive.filename}  {human(path.stat().st_size)}")
        bad += 0 if ok else 1
    if not manifest.archives:
        print("nothing recorded yet: run the script without --verify first")
        return 1
    print("\nall archives match the manifest" if not bad else f"\n{bad} archive(s) need re-downloading (--force)")
    return 0 if not bad else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="download_data.py",
        description="Download the hackathon archives into data/{archives,masters,sample,train,live,final}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----")[-1],
    )
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="data root (default: <repo>/data)")
    parser.add_argument("--source", default=os.environ.get("BANKREC_BASE_URL", DEFAULT_SOURCE), help="archives base URL")
    parser.add_argument("--phases", help="comma separated: sample, train-lite, train, live, final")
    parser.add_argument("--all", action="store_true", help="every published archive")
    parser.add_argument("--list", action="store_true", help="show sizes and what is already present")
    parser.add_argument("--verify", action="store_true", help="re-hash the kept archives against the manifest")
    parser.add_argument("--force", action="store_true", help="re-download and re-extract even when present")
    parser.add_argument("--no-keep-archives", action="store_true", help="delete the archives after extraction")
    parser.add_argument("--docs-dir", type=Path, help="where the spec + official starter go (default: <repo>/docs)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    dest = args.dest.resolve()
    manifest = Manifest.load(dest / "manifest.json", args.source)
    if normalise_manifest(manifest, dest):
        manifest.save()

    if args.list:
        return cmd_list(dest, manifest)
    if args.verify:
        return cmd_verify(dest, manifest)

    archives = archives_for(phases_requested(args))
    docs_dest = (args.docs_dir or dest_relative_root(dest) / "docs").resolve()
    tmp_root = dest / ".tmp"
    failed: list[str] = []

    for archive in archives:
        url = f"{args.source.rstrip('/')}/{archive.filename}"
        path = dest / "archives" / archive.filename
        record = manifest.archives.get(archive.filename, {})

        if not args.quiet:
            print(f"\n== {archive.filename}  ({archive.note})")

        # ------------------------------------------------------ download
        if path.exists() and not args.force:
            if not args.quiet:
                print(f"  archive present ({human(path.stat().st_size)})")
        else:
            available = remote_size(url)
            if available is None:
                if archive.optional:
                    print(f"  not published yet: {url}")
                else:
                    print(f"  unavailable: {url}", file=sys.stderr)
                    failed.append(archive.filename)
                continue
            try:
                download(url, path, quiet=args.quiet)
            except (urllib.error.URLError, OSError) as exc:
                print(f"  download failed: {exc}", file=sys.stderr)
                failed.append(archive.filename)
                continue

        digest = sha256(path)

        # ------------------------------------------------------- extract
        already = (
            record.get("sha256") == digest
            and record.get("extracted")
            and all((dest_relative_root(dest) / p).exists() for p in record["extracted"])
        )
        if already and not args.force:
            if not args.quiet:
                print("  already extracted")
            continue

        if not args.quiet:
            print(f"  extracting {human(path.stat().st_size)} …")
        tmp = tmp_root / re.sub(r"[^A-Za-z0-9._-]", "_", archive.filename)
        try:
            extract(path, tmp)
            override = "train-lite" if archive.phases == ("train-lite",) else None
            placed = place(tmp, dest, docs_dest, phase_override=override)
        except Exception as exc:  # corrupt archive: keep the file, report, continue
            print(f"  extraction failed: {exc}", file=sys.stderr)
            failed.append(archive.filename)
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        manifest.archives[archive.filename] = {
            "url": url,
            "bytes": path.stat().st_size,
            "sha256": digest,
            "downloaded_at": record.get("downloaded_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "extracted": placed,
        }
        manifest.save()
        if not args.quiet:
            print(f"  -> {', '.join(placed)}")

        if args.no_keep_archives:
            path.unlink(missing_ok=True)
            if not args.quiet:
                print("  archive deleted (--no-keep-archives)")

    shutil.rmtree(tmp_root, ignore_errors=True)
    manifest.save()

    # --------------------------------------------------------- summary
    if not args.quiet:
        print("\nlayout:")
        for name in ("archives", "masters", "sample", "train", "train-lite", "live", "final"):
            target = dest / name
            if target.exists():
                size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
                print(f"  {name:<12} {human(size):>9}  {target}")
        print(f"\nmanifest {dest / 'manifest.json'}")

    if failed:
        print(f"\n{len(failed)} archive(s) could not be fetched: {', '.join(failed)}", file=sys.stderr)
        print("re-run later, or check the event page for the release time", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
