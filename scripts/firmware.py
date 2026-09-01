"""Validate and safely extract the installer's machine firmware archive."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys


class FirmwareError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    inode: tuple[int, int, int]
    mode: int
    links: int
    data: bytes


def align4(value: int) -> int:
    return (value + 3) & ~3


def safe_name(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise FirmwareError("unsafe firmware archive path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FirmwareError("unsafe firmware archive path")
    path = PurePosixPath(value)
    if path.parts[0] != "vendorfw":
        raise FirmwareError("firmware archive path is outside vendorfw")
    return value


def parse_archive(data: bytes) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    offset = 0
    trailer = False
    while offset < len(data):
        if len(data) - offset < 110 or data[offset:offset + 6] != b"070701":
            raise FirmwareError("firmware archive is not newc cpio")
        try:
            fields = [
                int(data[offset + 6 + index * 8:offset + 14 + index * 8], 16)
                for index in range(13)
            ]
        except ValueError as error:
            raise FirmwareError(
                "firmware archive header is invalid"
            ) from error
        (
            inode,
            mode,
            _uid,
            _gid,
            links,
            _mtime,
            size,
            dev_major,
            dev_minor,
            _rdev_major,
            _rdev_minor,
            name_size,
            check,
        ) = fields
        if not name_size or check != 0 or links < 1:
            raise FirmwareError("firmware archive header is invalid")
        offset += 110
        name_end = offset + name_size
        if (
            name_end > len(data)
            or data[name_end - 1] != 0
            or b"\0" in data[offset:name_end - 1]
        ):
            raise FirmwareError("firmware archive name is invalid")
        try:
            name = data[offset:name_end - 1].decode("ascii")
        except UnicodeDecodeError as error:
            raise FirmwareError(
                "firmware archive name is not ASCII"
            ) from error
        offset = align4(name_end)
        content_end = offset + size
        if content_end > len(data):
            raise FirmwareError("firmware archive is truncated")
        content = data[offset:content_end]
        offset = align4(content_end)
        if name == "TRAILER!!!":
            if size or stat.S_IFMT(mode) != stat.S_IFREG:
                raise FirmwareError("firmware archive trailer is invalid")
            if any(data[offset:]):
                raise FirmwareError("firmware archive has trailing content")
            trailer = True
            break
        safe_name(name)
        if name in entries:
            raise FirmwareError("firmware archive repeats a path")
        file_type = stat.S_IFMT(mode)
        if (
            file_type not in {stat.S_IFDIR, stat.S_IFREG}
            or file_type == stat.S_IFDIR
            and size
        ):
            raise FirmwareError(
                "firmware archive contains an unsupported entry type"
            )
        entries[name] = Entry(
            name, (dev_major, dev_minor, inode), mode, links, content
        )
    if not trailer or not entries:
        raise FirmwareError("firmware archive has no valid trailer")
    for name in entries:
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            entry = entries.get(str(parent))
            if entry is None or stat.S_IFMT(entry.mode) != stat.S_IFDIR:
                raise FirmwareError(
                    "firmware archive lacks a parent directory"
                )
            parent = parent.parent
    return entries


def validate_manifest(
    entries: dict[str, Entry],
) -> tuple[list[tuple[str, str]], bytes]:
    manifest_name = "vendorfw/.vendorfw.manifest"
    manifest_entry = entries.get(manifest_name)
    if (
        manifest_entry is None
        or stat.S_IFMT(manifest_entry.mode) != stat.S_IFREG
    ):
        raise FirmwareError("firmware archive lacks its integrity manifest")
    try:
        lines = manifest_entry.data.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise FirmwareError("firmware manifest is not ASCII") from error
    if not lines:
        raise FirmwareError("firmware manifest is empty")
    records: list[tuple[str, str]] = []
    declared: dict[str, tuple[str, str]] = {}
    for line in lines:
        parts = line.split(" ")
        if len(parts) == 4 and parts[0] == "FILE" and parts[2] == "SHA256":
            kind, name, _marker, value = parts
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise FirmwareError("firmware manifest digest is invalid")
        elif len(parts) == 3 and parts[0] == "LINK":
            kind, name, value = parts
        else:
            raise FirmwareError("firmware manifest entry is invalid")
        safe_name(f"vendorfw/{name}")
        if name in declared:
            raise FirmwareError("firmware manifest repeats a path")
        declared[name] = (kind, value)
        records.append((kind, name))

    archived_files = {
        name.removeprefix("vendorfw/")
        for name, entry in entries.items()
        if name != manifest_name and stat.S_IFMT(entry.mode) == stat.S_IFREG
    }
    if archived_files != set(declared):
        raise FirmwareError("firmware archive and manifest disagree")
    for name, (kind, value) in declared.items():
        entry = entries[f"vendorfw/{name}"]
        if kind == "FILE":
            if hashlib.sha256(entry.data).hexdigest() != value:
                raise FirmwareError("firmware file failed its integrity check")
        else:
            target = declared.get(value)
            target_entry = entries.get(f"vendorfw/{value}")
            if (
                target is None
                or target[0] != "FILE"
                or target_entry is None
                or entry.data
                or entry.inode != target_entry.inode
            ):
                raise FirmwareError("firmware hard link is invalid")
    return records, manifest_entry.data


def install_archive(archive: Path, target: Path) -> None:
    entries = parse_archive(archive.read_bytes())
    records, manifest = validate_manifest(entries)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    try:
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.mkdir(mode=0o755)
        directories = sorted(
            (
                entry
                for entry in entries.values()
                if stat.S_IFMT(entry.mode) == stat.S_IFDIR
            ),
            key=lambda entry: len(PurePosixPath(entry.name).parts),
        )
        for entry in directories:
            (target / entry.name).mkdir(mode=0o755, exist_ok=True)
        ordered_records = sorted(
            records, key=lambda record: record[0] != "FILE"
        )
        for kind, name in ordered_records:
            destination = target / "vendorfw" / name
            entry = entries[f"vendorfw/{name}"]
            if kind == "FILE":
                with destination.open("xb") as stream:
                    stream.write(entry.data)
                destination.chmod(0o644)
            else:
                source = target / "vendorfw" / validate_manifest_target(
                    records, entries, name
                )
                os.link(source, destination, follow_symlinks=False)
        manifest_path = target / "vendorfw" / ".vendorfw.manifest"
        with manifest_path.open("xb") as stream:
            stream.write(manifest)
        manifest_path.chmod(0o644)
        (target / ".complete").touch(mode=0o644)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def validate_manifest_target(
    records: list[tuple[str, str]],
    entries: dict[str, Entry],
    link_name: str,
) -> str:
    link_entry = entries[f"vendorfw/{link_name}"]
    for kind, candidate in records:
        if (
            kind == "FILE"
            and entries[f"vendorfw/{candidate}"].inode == link_entry.inode
        ):
            return candidate
    raise FirmwareError("firmware hard link target is missing")


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: firmware.py ARCHIVE TARGET", file=sys.stderr)
        return 2
    try:
        install_archive(Path(sys.argv[1]), Path(sys.argv[2]))
    except (FirmwareError, OSError) as error:
        print(f"Firmware validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
