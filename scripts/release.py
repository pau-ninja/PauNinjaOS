#!/usr/bin/env python3
"""Build and validate PauNinjaOS installer bundles using only Python stdlib."""

from __future__ import annotations

import argparse
import binascii
from datetime import datetime, timedelta, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.parse
import uuid
import zipfile


EFI_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
LINUX_TYPE = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^[0-9][0-9A-Za-z._-]*$")
PACKAGE_NAME = re.compile(r"^pauninjaos-[0-9][0-9A-Za-z._-]*-apple-silicon\.zip$")
PINNED_GITHUB = re.compile(r'github:[^/\"]+/[^/\"]+/[0-9a-f]{40}')
BOOT_INSTALLER_VERSION = "v0.9.0"
BOOT_INSTALLER_SHA256 = "1dc51ec2cce25392e1eae2601c9dc1244e04cb51dbc207b51c815ead6ceeab33"
SUPPORTED_FIRMWARE = ["12.3", "12.3.1", "13.5"]
DEFAULT_INSTALLER_FIRMWARE = "13.5"
MAX_HARDWARE_TEST_AGE = timedelta(days=30)
MAX_CLOCK_SKEW = timedelta(minutes=5)


class ReleaseError(ValueError):
    pass


def validate_https_url(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.isascii() or any(character.isspace() for character in value):
        raise ReleaseError(f"{label} must be one ASCII HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ReleaseError(f"{label} is invalid") from error
    if (
        not value.startswith("https://")
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
        or ".." in PurePosixPath(parsed.path).parts
        or re.fullmatch(r"[A-Za-z0-9._~+:/-]+", value) is None
    ):
        raise ReleaseError(f"{label} must be a plain HTTPS URL without credentials, query, or fragment")


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"Expected one JSON object: {path}")
    return value


def read_gpt(path: Path) -> list[dict]:
    sector = 512
    with path.open("rb") as source:
        source.seek(sector)
        header = source.read(512)
        if header[:8] != b"EFI PART":
            raise ReleaseError("Disk image has no GPT header")
        header_size, expected_header_crc = struct.unpack_from("<II", header, 12)
        if not 92 <= header_size <= 512:
            raise ReleaseError("GPT header size is invalid")
        checked_header = bytearray(header[:header_size])
        struct.pack_into("<I", checked_header, 16, 0)
        if binascii.crc32(checked_header) & 0xFFFFFFFF != expected_header_crc:
            raise ReleaseError("GPT header checksum mismatch")
        first_usable, last_usable = struct.unpack_from("<QQ", header, 40)
        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        entry_count, entry_size, expected_entries_crc = struct.unpack_from("<III", header, 80)
        if not 128 <= entry_size <= 4096 or not 1 <= entry_count <= 1024:
            raise ReleaseError("GPT entry table is unreasonable")
        source.seek(entries_lba * sector)
        table = source.read(entry_count * entry_size)
        if len(table) != entry_count * entry_size:
            raise ReleaseError("GPT entry table is truncated")
        if binascii.crc32(table) & 0xFFFFFFFF != expected_entries_crc:
            raise ReleaseError("GPT entry table checksum mismatch")

    partitions = []
    image_size = path.stat().st_size
    for index in range(entry_count):
        entry = table[index * entry_size : (index + 1) * entry_size]
        if entry[:16] == b"\0" * 16:
            continue
        type_id = uuid.UUID(bytes_le=entry[:16])
        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
        if last_lba < first_lba:
            raise ReleaseError("GPT partition range is reversed")
        offset = first_lba * sector
        size = (last_lba - first_lba + 1) * sector
        if offset + size > image_size:
            raise ReleaseError("GPT partition exceeds the disk image")
        if first_lba < first_usable or last_lba > last_usable:
            raise ReleaseError("GPT partition exceeds the usable LBA range")
        name = entry[56:128].decode("utf-16-le", errors="strict").rstrip("\0")
        partitions.append({"number": index + 1, "type": type_id, "offset": offset, "size": size, "name": name})
    ranges = sorted((partition["offset"], partition["offset"] + partition["size"]) for partition in partitions)
    if any(previous_end > current_start for (_, previous_end), (current_start, _) in zip(ranges, ranges[1:])):
        raise ReleaseError("GPT partitions overlap")
    return partitions


def copy_range(source: Path, target: Path, offset: int, size: int) -> None:
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        incoming.seek(offset)
        remaining = size
        while remaining:
            block = incoming.read(min(1024 * 1024, remaining))
            if not block:
                raise ReleaseError("Disk image ended during partition extraction")
            outgoing.write(block)
            remaining -= len(block)


def add_archive_file(archive: zipfile.ZipFile, source: Path, name: str, mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = mode << 16
    with source.open("rb") as incoming, archive.open(info, "w", force_zip64=True) as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)


def validate_esp(stream, size: int) -> None:
    def read_at(offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > size:
            raise ReleaseError("FAT structure exceeds the EFI image")
        stream.seek(offset)
        value = stream.read(length)
        if len(value) != length:
            raise ReleaseError("EFI image is truncated")
        return value

    boot = read_at(0, 512)
    if boot[510:512] != b"\x55\xaa":
        raise ReleaseError("EFI image has no FAT boot signature")
    bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
    sectors_per_cluster = boot[13]
    reserved_sectors = struct.unpack_from("<H", boot, 14)[0]
    fat_count = boot[16]
    root_entry_count = struct.unpack_from("<H", boot, 17)[0]
    total_sectors = struct.unpack_from("<H", boot, 19)[0] or struct.unpack_from("<I", boot, 32)[0]
    fat_sectors = struct.unpack_from("<H", boot, 22)[0] or struct.unpack_from("<I", boot, 36)[0]
    if bytes_per_sector not in {512, 1024, 2048, 4096} or not sectors_per_cluster or sectors_per_cluster & (sectors_per_cluster - 1):
        raise ReleaseError("EFI image has invalid FAT geometry")
    if not reserved_sectors or not fat_count or not fat_sectors or not total_sectors:
        raise ReleaseError("EFI image has incomplete FAT geometry")
    root_sectors = (root_entry_count * 32 + bytes_per_sector - 1) // bytes_per_sector
    data_sector = reserved_sectors + fat_count * fat_sectors + root_sectors
    if data_sector >= total_sectors or total_sectors * bytes_per_sector > size:
        raise ReleaseError("EFI FAT geometry exceeds the image")
    cluster_count = (total_sectors - data_sector) // sectors_per_cluster
    fat_bits = 12 if cluster_count < 4085 else 16 if cluster_count < 65525 else 32
    max_cluster = cluster_count + 1
    required_fat_bytes = max_cluster + max_cluster // 2 + 2 if fat_bits == 12 else max_cluster * (fat_bits // 8) + fat_bits // 8
    if required_fat_bytes > fat_sectors * bytes_per_sector:
        raise ReleaseError("EFI FAT is too small for its declared data area")
    root_cluster = struct.unpack_from("<I", boot, 44)[0] if fat_bits == 32 else None
    if fat_bits == 32 and (root_entry_count or root_cluster is None or root_cluster < 2):
        raise ReleaseError("EFI image has an invalid FAT32 root")
    if fat_bits != 32 and not root_entry_count:
        raise ReleaseError("EFI image has an invalid FAT root")
    cluster_size = bytes_per_sector * sectors_per_cluster
    fat_offset = reserved_sectors * bytes_per_sector
    root_offset = (reserved_sectors + fat_count * fat_sectors) * bytes_per_sector
    data_offset = data_sector * bytes_per_sector

    def next_cluster(cluster: int) -> int:
        if fat_bits == 12:
            value = struct.unpack("<H", read_at(fat_offset + cluster + cluster // 2, 2))[0]
            return value >> 4 if cluster & 1 else value & 0x0FFF
        if fat_bits == 16:
            return struct.unpack("<H", read_at(fat_offset + cluster * 2, 2))[0]
        return struct.unpack("<I", read_at(fat_offset + cluster * 4, 4))[0] & 0x0FFFFFFF

    end_cluster = {12: 0x0FF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[fat_bits]

    def cluster_chain(cluster: int):
        seen = set()
        while 2 <= cluster <= max_cluster:
            if cluster in seen or len(seen) >= cluster_count:
                raise ReleaseError("FAT cluster chain loops")
            seen.add(cluster)
            yield cluster
            cluster = next_cluster(cluster)
            if cluster >= end_cluster:
                return
        raise ReleaseError("FAT cluster chain is invalid")

    def short_checksum(name: bytes) -> int:
        value = 0
        for byte in name:
            value = (((value & 1) << 7) | (value >> 1)) + byte & 0xFF
        return value

    def short_text(name: bytes) -> str:
        base = name[:8].rstrip(b" ").decode("ascii", errors="strict")
        extension = name[8:].rstrip(b" ").decode("ascii", errors="strict")
        return f"{base}.{extension}".casefold() if extension else base.casefold()

    def directory(cluster: int | None) -> dict[bytes | str, tuple[int, int, int]]:
        entries = {}
        if cluster is None:
            blocks = [read_at(root_offset, root_entry_count * 32)]
        else:
            blocks = [read_at(data_offset + (item - 2) * cluster_size, cluster_size) for item in cluster_chain(cluster)]
        long_parts: dict[int, str] = {}
        long_checksum = None
        long_last = None
        long_expected = None
        for block in blocks:
            for position in range(0, len(block), 32):
                entry = block[position : position + 32]
                if entry[0] == 0:
                    return entries
                if entry[0] == 0xE5:
                    long_parts = {}
                    long_checksum = long_last = None
                    long_expected = None
                    continue
                if entry[11] == 0x0F:
                    sequence = entry[0] & 0x1F
                    checksum = entry[13]
                    if not sequence or entry[12] != 0 or entry[26:28] != b"\0\0":
                        long_parts = {}
                        long_checksum = long_last = None
                        long_expected = None
                        continue
                    if entry[0] & 0x40:
                        long_parts = {}
                        long_checksum = checksum
                        long_last = sequence
                        long_expected = sequence
                    elif long_checksum != checksum or long_expected is None or sequence != long_expected - 1:
                        long_parts = {}
                        long_checksum = long_last = None
                        long_expected = None
                        continue
                    encoded = entry[1:11] + entry[14:26] + entry[28:32]
                    long_parts[sequence] = encoded.decode("utf-16-le", errors="strict").rstrip("\uffff\0")
                    long_expected = sequence
                    continue
                if entry[11] & 0x08:
                    long_parts = {}
                    long_checksum = long_last = None
                    long_expected = None
                    continue
                entry_cluster = (struct.unpack_from("<H", entry, 20)[0] << 16) | struct.unpack_from("<H", entry, 26)[0]
                value = (entry[11], entry_cluster, struct.unpack_from("<I", entry, 28)[0])
                entries[entry[:11]] = value
                entries[short_text(entry[:11])] = value
                if long_parts and long_expected == 1 and long_checksum == short_checksum(entry[:11]) and set(long_parts) == set(range(1, (long_last or 0) + 1)):
                    entries["".join(long_parts[index] for index in sorted(long_parts)).casefold()] = value
                long_parts = {}
                long_checksum = long_last = None
                long_expected = None
        return entries

    def file_data(entry: tuple[int, int, int], path: str, limit: int = 1024 * 1024 * 1024, read_content: bool = True) -> bytes:
        size = entry[2]
        if size <= 0 or size > limit:
            raise ReleaseError(f"EFI file has an invalid size: {path}")
        chain = list(cluster_chain(entry[1]))
        required_clusters = (size + cluster_size - 1) // cluster_size
        if len(chain) != required_clusters:
            raise ReleaseError(f"EFI file cluster chain length is invalid: {path}")
        if not read_content:
            return b""
        blocks = []
        remaining = size
        for cluster in chain:
            blocks.append(read_at(data_offset + (cluster - 2) * cluster_size, min(cluster_size, remaining)))
            remaining -= min(cluster_size, remaining)
        return b"".join(blocks)

    def child(entries: dict, name: bytes | str, path: str, want_directory: bool) -> tuple[int, int, int]:
        entry = entries.get(name)
        is_directory = entry is not None and bool(entry[0] & 0x10)
        if entry is None or is_directory is not want_directory or entry[1] < 2 or (not want_directory and entry[2] <= 0):
            raise ReleaseError(f"EFI image lacks {path}")
        return entry

    root = directory(root_cluster)
    m1n1 = child(root, b"M1N1       ", "the m1n1 directory", True)
    m1n1_boot = child(directory(m1n1[1]), b"BOOT    BIN", "m1n1/boot.bin", False)
    file_data(m1n1_boot, "m1n1/boot.bin")
    efi = child(root, b"EFI        ", "the EFI directory", True)
    efi_entries = directory(efi[1])
    boot_directory = child(efi_entries, b"BOOT       ", "EFI/BOOT", True)
    boot_file = child(directory(boot_directory[1]), b"BOOTAA64EFI", "EFI/BOOT/BOOTAA64.EFI", False)
    boot_data = file_data(boot_file, "EFI/BOOT/BOOTAA64.EFI")
    if len(boot_data) < 132 or boot_data[:2] != b"MZ":
        raise ReleaseError("EFI/BOOT/BOOTAA64.EFI is not a PE executable")
    pe_offset = struct.unpack_from("<I", boot_data, 0x3C)[0]
    if pe_offset + 6 > len(boot_data) or boot_data[pe_offset : pe_offset + 4] != b"PE\0\0" or struct.unpack_from("<H", boot_data, pe_offset + 4)[0] != 0xAA64:
        raise ReleaseError("EFI/BOOT/BOOTAA64.EFI is not an AArch64 PE executable")
    loader = child(root, b"LOADER     ", "the loader directory", True)
    loader_entries = directory(loader[1])
    loader_config = child(loader_entries, "loader.conf", "loader/loader.conf", False)
    loader_text = file_data(loader_config, "loader/loader.conf", 1024 * 1024).decode("utf-8", errors="strict")
    entries_directory = child(loader_entries, b"ENTRIES    ", "loader/entries", True)
    boot_entries = directory(entries_directory[1])
    configs = [(name, entry) for name, entry in boot_entries.items() if isinstance(name, str) and name.endswith(".conf")]
    if not configs:
        raise ReleaseError("EFI image has no loader entry")
    defaults = [
        fields[1].strip().casefold()
        for line in loader_text.splitlines()
        if len(fields := line.split(None, 1)) == 2 and fields[0].casefold() == "default"
    ]
    if len(defaults) != 1 or not defaults[0] or "/" in defaults[0] or "\\" in defaults[0] or ".." in defaults[0]:
        raise ReleaseError("EFI loader default is invalid")
    if not any(fnmatch.fnmatchcase(name, defaults[0]) for name, _entry in configs):
        raise ReleaseError("EFI loader default does not match a loader entry")

    def resolve(path: str) -> tuple[int, int, int]:
        parts = [part for part in path.strip().lstrip("/").split("/") if part]
        current = root
        result = None
        for index, part in enumerate(parts):
            result = current.get(part.casefold())
            if result is None:
                raise ReleaseError(f"Loader entry references a missing EFI path: {path}")
            if index + 1 < len(parts):
                if not result[0] & 0x10:
                    raise ReleaseError(f"Loader entry path is not a directory: {path}")
                current = directory(result[1])
        if result is None or result[0] & 0x10:
            raise ReleaseError(f"Loader entry does not reference a file: {path}")
        return result

    for name, entry in configs:
        config = file_data(entry, f"loader/entries/{name}", 1024 * 1024).decode("utf-8", errors="strict")
        references = [line.split(None, 1) for line in config.splitlines() if line.startswith(("linux ", "initrd "))]
        kernels = [path for key, path in references if key == "linux"]
        initrds = [path for key, path in references if key == "initrd"]
        if len(kernels) != 1 or not initrds:
            raise ReleaseError(f"Loader entry lacks kernel or initrd: {name}")
        for path in kernels + initrds:
            file_data(resolve(path), path, read_content=False)


def validate_root(stream, size: int) -> None:
    if size < 2048:
        raise ReleaseError("Root image is too small")
    stream.seek(1024)
    superblock = stream.read(1024)
    if len(superblock) != 1024 or superblock[56:58] != b"\x53\xef":
        raise ReleaseError("Root image is not ext4")
    if superblock[120:136].rstrip(b"\0") != b"PAUNINJAOS":
        raise ReleaseError("Root filesystem label is not PAUNINJAOS")


def validate_full_filesystems(esp_image: Path, root_image: Path) -> None:
    fat_check = shutil.which("fsck.fat") or shutil.which("fsck_msdos")
    ext_check = shutil.which("e2fsck")
    debugfs = shutil.which("debugfs")
    if not fat_check or not ext_check or not debugfs:
        raise ReleaseError("Full image validation requires fsck.fat, e2fsck, and debugfs")
    fat = subprocess.run([fat_check, "-n", esp_image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if fat.returncode != 0:
        raise ReleaseError("EFI filesystem integrity check failed")
    ext = subprocess.run([ext_check, "-fn", root_image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if ext.returncode != 0:
        raise ReleaseError("Root filesystem integrity check failed")
    for path in ("/etc/os-release", "/nix/var/nix/profiles/system"):
        inspected = subprocess.run([debugfs, "-R", f"stat {path}", root_image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if inspected.returncode != 0 or "Inode:" not in inspected.stdout or "File not found" in inspected.stdout:
            raise ReleaseError(f"Root filesystem lacks required system path: {path}")


def choose_partition(partitions: list[dict], type_id: uuid.UUID, label: str) -> dict:
    matches = [partition for partition in partitions if partition["type"] == type_id]
    if len(matches) != 1:
        raise ReleaseError(f"Expected exactly one {label} partition, found {len(matches)}")
    if matches[0]["size"] % 4096:
        raise ReleaseError(f"{label} partition size must be a multiple of 4096 bytes")
    return matches[0]


def validate_source(root: Path) -> None:
    required = [
        "flake.nix",
        "nixos/configuration.nix",
        "nixos/image.nix",
        "bootstrap/install.sh",
        "scripts/check-upstream-installer.py",
        "scripts/firmware.py",
        "scripts/sign-release.sh",
        "scripts/hardware-test-install.sh",
        "ATTRIBUTION.md",
        "LICENSE",
        "SOURCE_OFFER.md",
        "release/status.json",
    ]
    missing = [name for name in required if not (root / name).is_file() or not (root / name).stat().st_size]
    if missing:
        raise ReleaseError(f"Missing required source files: {', '.join(missing)}")

    flake = (root / "flake.nix").read_text(encoding="utf-8")
    input_urls = re.findall(r'\burl\s*=\s*"([^\"]+)"', flake)
    if not input_urls or any(PINNED_GITHUB.fullmatch(url) is None for url in input_urls):
        raise ReleaseError("Every source input must use an immutable GitHub commit")
    config = (root / "nixos/configuration.nix").read_text(encoding="utf-8")
    for required_text in (
        'services.xserver.enable = false;',
        'initialHashedPassword = "!";',
        "configurationLimit = 10;",
        "bootCounting.enable = true;",
        'pauninjaos-firmware "$archive" "$target"',
        'echo "Installer firmware archive is missing."',
        'echo "Wi-Fi firmware could not be loaded; console recovery remains available."',
        'echo "Bluetooth firmware could not be loaded; console recovery remains available."',
    ):
        if required_text not in config:
            raise ReleaseError(f"System safety policy is missing: {required_text}")
    image = (root / "nixos/image.nix").read_text(encoding="utf-8")
    if 'bootSize = "2048M";' not in image:
        raise ReleaseError("EFI capacity policy is missing")
    bootstrap = (root / "bootstrap/install.sh").read_text(encoding="utf-8").lower()
    for forbidden in ("diskutil", "newfs", " resizecontainer", " apfs", " gpt "):
        if forbidden in bootstrap:
            raise ReleaseError(f"Bootstrap contains forbidden disk handling: {forbidden.strip()}")
    if "installer_sha256=" not in bootstrap or "export distro=pauninjaos" not in bootstrap:
        raise ReleaseError("Bootstrap is not pinned and branded")
    for required_text in (
        "curl --fail --proto '=https' --proto-redir '=https'",
        "openssl dgst -sha256 -verify",
        "release_public_key_sha256=",
        "release_version=unconfigured",
        "installer_data.size",
        "package.sha256",
        "package.size",
        'export repo_base="$pwd"',
        'export installer_data="$pwd/installer_data.json"',
        'tar tf "$archive" > installer.members',
    ):
        if required_text not in bootstrap:
            raise ReleaseError(f"Bootstrap verification policy is missing: {required_text}")
    status = load_json(root / "release/status.json")
    if status != {
        "schema": "PAUNINJAOS_RELEASE_STATUS_V1",
        "status": "SOURCE_ONLY",
        "installable": False,
        "hardware_tests": [],
    }:
        raise ReleaseError("Source tree must default to a non-installable release status")


def find_raw_image(value: Path) -> Path:
    if value.is_file():
        return value
    candidates = sorted(path for path in value.rglob("*.img") if path.is_file())
    if len(candidates) != 1:
        raise ReleaseError(f"Expected one raw image, found {len(candidates)}")
    return candidates[0]


def build_bundle(
    raw_value: Path,
    version: str,
    base_url: str,
    output: Path,
    attribution: Path,
    source_revision: str,
    full_filesystem_check: bool = True,
) -> Path:
    if not VERSION.fullmatch(version) or ".." in version:
        raise ReleaseError("Release version is unsafe")
    validate_https_url(base_url, "Release base URL")
    if not REVISION.fullmatch(source_revision):
        raise ReleaseError("Source revision must be one immutable Git commit")
    raw = find_raw_image(raw_value)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pauninjaos-release-") as temporary_name:
        temporary = Path(temporary_name)
        partitions = read_gpt(raw)
        esp = choose_partition(partitions, EFI_TYPE, "EFI")
        root = choose_partition(partitions, LINUX_TYPE, "Linux root")
        esp_image = temporary / "esp.img"
        root_image = temporary / "root.img"
        copy_range(raw, esp_image, esp["offset"], esp["size"])
        copy_range(raw, root_image, root["offset"], root["size"])
        with esp_image.open("rb") as stream:
            validate_esp(stream, esp_image.stat().st_size)
        with root_image.open("rb") as stream:
            validate_root(stream, root_image.stat().st_size)
        if full_filesystem_check:
            validate_full_filesystems(esp_image, root_image)

        package_name = f"pauninjaos-{version}-apple-silicon.zip"
        package = temporary / package_name
        license_path = attribution.parent / "LICENSE"
        source_offer_path = attribution.parent / "SOURCE_OFFER.md"
        if not license_path.is_file() or not license_path.stat().st_size:
            raise ReleaseError("Project license is missing")
        if not source_offer_path.is_file() or not source_offer_path.stat().st_size:
            raise ReleaseError("Corresponding-source offer is missing")
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            add_archive_file(archive, esp_image, "esp.img")
            add_archive_file(archive, root_image, "root.img")
            add_archive_file(archive, attribution, "ATTRIBUTION.md")
            add_archive_file(archive, license_path, "LICENSE")
            add_archive_file(archive, source_offer_path, "SOURCE_OFFER.md")

        package_url = f"{base_url.rstrip('/')}/{package_name}"
        installer_data = {
            "os_list": [
                {
                    "name": "PauNinjaOS",
                    "default_os_name": "PauNinjaOS",
                    "boot_object": "m1n1.bin",
                    "next_object": "m1n1/boot.bin",
                    "package": package_name,
                    "supported_fw": SUPPORTED_FIRMWARE,
                    "partitions": [
                        {
                            "name": "EFI",
                            "type": "EFI",
                            "size": f"{esp_image.stat().st_size}B",
                            "image": "esp.img",
                            "copy_firmware": True,
                            "copy_installer_data": True,
                        },
                        {
                            "name": "Root",
                            "type": "Linux",
                            "size": f"{root_image.stat().st_size}B",
                            "expand": True,
                            "image": "root.img",
                        },
                    ],
                }
            ]
        }
        installer_bytes = canonical(installer_data)
        installer_path = temporary / "installer_data.json"
        installer_path.write_bytes(installer_bytes)
        release = {
            "schema": "PAUNINJAOS_RELEASE_V1",
            "version": version,
            "source_revision": source_revision,
            "status": "SOURCE_BUILDABLE",
            "installable": False,
            "supported_models": [],
            "boot_installer": {"version": BOOT_INSTALLER_VERSION, "sha256": BOOT_INSTALLER_SHA256},
            "package": {"url": package_url, "size": package.stat().st_size, "sha256": digest(package)},
            "installer_data": {"size": len(installer_bytes), "sha256": hashlib.sha256(installer_bytes).hexdigest()},
            "hardware_tests": [],
        }
        release_path = temporary / "release.json"
        release_path.write_bytes(canonical(release))
        validate_bundle(temporary)
        for artifact in (package, installer_path, release_path):
            shutil.copy2(artifact, output / artifact.name)
    validate_bundle(output)
    return output


def validate_bundle(directory: Path, candidate_release=None) -> None:
    release = candidate_release if candidate_release is not None else load_json(directory / "release.json")
    installer_path = directory / "installer_data.json"
    installer = load_json(installer_path)
    expected_release_fields = {
        "schema", "version", "source_revision", "status", "installable", "supported_models", "boot_installer",
        "package", "installer_data", "hardware_tests",
    }
    if set(release) != expected_release_fields:
        raise ReleaseError("Release manifest fields are invalid")
    if release.get("schema") != "PAUNINJAOS_RELEASE_V1":
        raise ReleaseError("Release schema is invalid")
    if release.get("status") not in {"SOURCE_BUILDABLE", "HARDWARE_TESTED"}:
        raise ReleaseError("Release status is invalid")
    installable = release["status"] == "HARDWARE_TESTED"
    if release.get("installable") is not installable:
        raise ReleaseError("Installable flag disagrees with release status")
    if not isinstance(release.get("version"), str) or not VERSION.fullmatch(release["version"]) or ".." in release["version"]:
        raise ReleaseError("Release version is invalid")
    if not isinstance(release.get("source_revision"), str) or not REVISION.fullmatch(release["source_revision"]):
        raise ReleaseError("Release source revision is invalid")
    if release.get("boot_installer") != {"version": BOOT_INSTALLER_VERSION, "sha256": BOOT_INSTALLER_SHA256}:
        raise ReleaseError("Boot installer identity is invalid")
    if set(release.get("package", {})) != {"url", "size", "sha256"} or set(release.get("installer_data", {})) != {"size", "sha256"}:
        raise ReleaseError("Release digest records are invalid")
    metadata = release.get("installer_data", {})
    if metadata.get("size") != installer_path.stat().st_size or metadata.get("sha256") != digest(installer_path):
        raise ReleaseError("Installer metadata digest or size mismatch")
    if set(installer) != {"os_list"} or not isinstance(installer.get("os_list"), list) or len(installer["os_list"]) != 1:
        raise ReleaseError("Installer metadata root is invalid")
    try:
        os_entry = installer["os_list"][0]
        package_name = os_entry["package"]
        partitions = os_entry["partitions"]
    except (KeyError, IndexError, TypeError) as error:
        raise ReleaseError("Installer metadata is incomplete") from error
    expected_os_fields = {
        "name", "default_os_name", "boot_object", "next_object", "package", "supported_fw", "partitions",
    }
    if set(os_entry) != expected_os_fields:
        raise ReleaseError("Installer OS entry fields are invalid")
    if os_entry.get("name") != "PauNinjaOS" or os_entry.get("default_os_name") != "PauNinjaOS":
        raise ReleaseError("Installer product branding is invalid")
    if os_entry.get("boot_object") != "m1n1.bin" or os_entry.get("next_object") != "m1n1/boot.bin":
        raise ReleaseError("Installer boot object contract is invalid")
    supported_fw = os_entry.get("supported_fw")
    if supported_fw != SUPPORTED_FIRMWARE:
        raise ReleaseError("Installer firmware compatibility is invalid")
    if not isinstance(package_name, str) or not PACKAGE_NAME.fullmatch(package_name):
        raise ReleaseError("Installer package name is unsafe")
    if "/" in package_name or ".." in package_name:
        raise ReleaseError("Installer package name is unsafe")
    if not isinstance(partitions, list) or len(partitions) != 2 or any(not isinstance(partition, dict) for partition in partitions):
        raise ReleaseError("Installer partition layout is invalid")
    if partitions[0].get("type") != "EFI" or partitions[1].get("type") != "Linux":
        raise ReleaseError("Installer partition layout is invalid")
    if set(partitions[0]) != {"name", "type", "size", "image", "copy_firmware", "copy_installer_data"}:
        raise ReleaseError("EFI partition metadata fields are invalid")
    if set(partitions[1]) != {"name", "type", "size", "image", "expand"}:
        raise ReleaseError("Root partition metadata fields are invalid")
    if partitions[0].get("name") != "EFI" or partitions[0].get("image") != "esp.img" or partitions[0].get("copy_firmware") is not True or partitions[0].get("copy_installer_data") is not True:
        raise ReleaseError("EFI partition metadata is invalid")
    if partitions[1].get("name") != "Root" or partitions[1].get("image") != "root.img" or partitions[1].get("expand") is not True:
        raise ReleaseError("Root partition metadata is invalid")
    package = directory / package_name
    package_record = release.get("package", {})
    package_url = package_record.get("url")
    if not isinstance(package_url, str):
        raise ReleaseError("Release package URL is invalid")
    validate_https_url(package_url, "Release package URL")
    if package_url.rsplit("/", 1)[-1] != package_name:
        raise ReleaseError("Release package URL is unsafe or disagrees with installer metadata")
    if not package.is_file() or package.stat().st_size != package_record.get("size") or digest(package) != package_record.get("sha256"):
        raise ReleaseError("Release package digest or size mismatch")
    if not SHA256.fullmatch(str(package_record.get("sha256", ""))):
        raise ReleaseError("Release package SHA-256 is invalid")
    with zipfile.ZipFile(package) as archive:
        expected_names = ["esp.img", "root.img", "ATTRIBUTION.md", "LICENSE", "SOURCE_OFFER.md"]
        if archive.namelist() != expected_names:
            raise ReleaseError("Release package content list is invalid")
        if any(not archive.getinfo(name).file_size for name in ("ATTRIBUTION.md", "LICENSE", "SOURCE_OFFER.md")):
            raise ReleaseError("Release legal notices are empty")
        expected_sizes = {"esp.img": partitions[0]["size"], "root.img": partitions[1]["size"]}
        for name, size_text in expected_sizes.items():
            if not isinstance(size_text, str) or not size_text.endswith("B"):
                raise ReleaseError("Partition size is invalid")
            if archive.getinfo(name).file_size != int(size_text[:-1]) or archive.getinfo(name).file_size % 4096:
                raise ReleaseError(f"{name} size does not match installer metadata")
        with tempfile.TemporaryDirectory(prefix="pauninjaos-verify-") as temporary_name:
            extracted = Path(temporary_name)
            for name in ("esp.img", "root.img"):
                with archive.open(name) as incoming, (extracted / name).open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            with (extracted / "esp.img").open("rb") as stream:
                validate_esp(stream, archive.getinfo("esp.img").file_size)
            with (extracted / "root.img").open("rb") as stream:
                validate_root(stream, archive.getinfo("root.img").file_size)
    tests = release.get("hardware_tests")
    supported_models = release.get("supported_models")
    if installable and (not isinstance(tests, list) or not tests):
        raise ReleaseError("Installable release lacks hardware tests")
    if installable and (not isinstance(supported_models, list) or not supported_models):
        raise ReleaseError("Installable release names no hardware-tested model")
    if not installable and tests != []:
        raise ReleaseError("Source-buildable release must not claim hardware tests")
    if not installable and supported_models != []:
        raise ReleaseError("Source-buildable release must not claim supported models")
    if installable:
        tested_models = set()
        for attestation in tests:
            tested_models.update(validate_attestation(release, attestation, supported_fw, directory))
        if tested_models != set(supported_models):
            raise ReleaseError("Supported models disagree with hardware attestations")


def validate_attestation(release: dict, attestation: object, supported_fw: list[str], evidence_directory: Path | None = None) -> list[str]:
    if not isinstance(attestation, dict):
        raise ReleaseError("Hardware attestation is not an object")
    required = {
        "schema", "version", "package_sha256", "installer_data_sha256", "installer_version",
        "installer_sha256", "source_revision", "cases",
    }
    if set(attestation) != required or attestation.get("schema") != "PAUNINJAOS_HARDWARE_TEST_V2":
        raise ReleaseError("Hardware attestation schema is invalid")
    if attestation.get("package_sha256") != release.get("package", {}).get("sha256"):
        raise ReleaseError("Hardware attestation targets a different package")
    if attestation.get("version") != release.get("version"):
        raise ReleaseError("Hardware attestation targets a different release version")
    if attestation.get("source_revision") != release.get("source_revision"):
        raise ReleaseError("Hardware attestation targets a different source revision")
    if attestation.get("installer_data_sha256") != release.get("installer_data", {}).get("sha256"):
        raise ReleaseError("Hardware attestation targets different installer metadata")
    if attestation.get("installer_version") != BOOT_INSTALLER_VERSION or attestation.get("installer_sha256") != BOOT_INSTALLER_SHA256:
        raise ReleaseError("Hardware attestation targets a different boot installer")
    model_name = re.compile(r"(?:Mac(?:BookAir|BookPro|mini|Studio|Pro)?|iMac)[0-9]+,[0-9]+")
    required_checks = {"install", "first_boot", "network", "update", "rollback"}
    required_case = {"model", "installer_firmware", "system_firmware", "checks", "evidence"}
    required_evidence = {"started_at", "completed_at", "operator", "log", "log_sha256"}
    cases = attestation.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ReleaseError("Hardware attestation contains no test cases")
    models = []
    identities = set()
    coverage: dict[str, set[str]] = {}
    for case in cases:
        if not isinstance(case, dict) or set(case) != required_case:
            raise ReleaseError("Hardware test case schema is invalid")
        model = case.get("model")
        installer_firmware = case.get("installer_firmware")
        system_firmware = case.get("system_firmware")
        if not isinstance(model, str) or not model_name.fullmatch(model):
            raise ReleaseError("Hardware test case contains an invalid model identifier")
        if installer_firmware not in supported_fw:
            raise ReleaseError("Hardware test case uses unsupported installer firmware")
        if not isinstance(system_firmware, str) or not system_firmware.strip() or len(system_firmware) > 200:
            raise ReleaseError("Hardware test case lacks the observed system firmware")
        identity = (model, installer_firmware, system_firmware)
        if identity in identities:
            raise ReleaseError("Hardware attestation repeats a test case")
        identities.add(identity)
        coverage.setdefault(model, set()).add(installer_firmware)
        checks = case.get("checks")
        if not isinstance(checks, dict) or set(checks) != required_checks or any(value is not True for value in checks.values()):
            raise ReleaseError("Hardware test case does not pass every required check")
        evidence = case.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != required_evidence:
            raise ReleaseError("Hardware test evidence schema is invalid")
        try:
            started = datetime.strptime(evidence.get("started_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            completed = datetime.strptime(evidence.get("completed_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError) as error:
            raise ReleaseError("Hardware test evidence timestamps are invalid") from error
        if completed <= started:
            raise ReleaseError("Hardware test evidence time range is invalid")
        now = datetime.now(timezone.utc)
        if completed > now + MAX_CLOCK_SKEW or started < now - MAX_HARDWARE_TEST_AGE:
            raise ReleaseError("Hardware test evidence is stale or future-dated")
        log_name = evidence.get("log")
        if not isinstance(log_name, str) or not re.fullmatch(r"hardware-test-[0-9A-Za-z._-]+\.log", log_name) or ".." in log_name:
            raise ReleaseError("Hardware test evidence log name is invalid")
        if evidence_directory is not None:
            log_path = evidence_directory / log_name
            if not log_path.is_file() or digest(log_path) != evidence.get("log_sha256"):
                raise ReleaseError("Hardware test evidence log is missing or has the wrong digest")
        if not isinstance(evidence.get("operator"), str) or not evidence["operator"].strip() or len(evidence["operator"]) > 100:
            raise ReleaseError("Hardware test evidence operator is invalid")
        if not isinstance(evidence.get("log_sha256"), str) or not SHA256.fullmatch(evidence["log_sha256"]):
            raise ReleaseError("Hardware test evidence log digest is invalid")
        models.append(model)
    if any(DEFAULT_INSTALLER_FIRMWARE not in firmware for firmware in coverage.values()):
        raise ReleaseError("Hardware attestation does not cover the default installer firmware for every model")
    return sorted(set(models))


def promote(directory: Path, attestation_path: Path) -> None:
    validate_bundle(directory)
    release_path = directory / "release.json"
    release = load_json(release_path)
    if release.get("status") != "SOURCE_BUILDABLE":
        raise ReleaseError("Only a source-buildable release can be promoted")
    attestation = load_json(attestation_path)
    installer = load_json(directory / "installer_data.json")
    models = validate_attestation(release, attestation, installer["os_list"][0]["supported_fw"], directory)
    release["status"] = "HARDWARE_TESTED"
    release["installable"] = True
    release["supported_models"] = models
    release["hardware_tests"] = [attestation]
    promoted = canonical(release)
    validate_bundle(directory, release)
    atomic_write(release_path, promoted)


def prepare_signing(directory: Path, output: Path) -> None:
    validate_bundle(directory)
    release = load_json(directory / "release.json")
    if release.get("status") != "HARDWARE_TESTED" or release.get("installable") is not True:
        raise ReleaseError("Only a hardware-tested release can produce an installer entrypoint")
    atomic_write(output, canonical(release))


def render_bootstrap(
    directory: Path,
    manifest: Path,
    public_key: Path,
    signature: Path,
    template: Path,
    output: Path,
) -> None:
    validate_bundle(directory)
    release = load_json(directory / "release.json")
    if release.get("status") != "HARDWARE_TESTED" or release.get("installable") is not True:
        raise ReleaseError("Only a hardware-tested release can produce an installer entrypoint")
    if manifest.read_bytes() != canonical(release):
        raise ReleaseError("Signed release manifest differs from the validated release")
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", public_key, "-signature", signature, manifest],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if verified.returncode != 0:
        raise ReleaseError("Release signature cannot be verified")
    base_url = release["package"]["url"].rsplit("/", 1)[0]
    source = template.read_text(encoding="utf-8")
    replacements = {
        "PAUNINJAOS_BASE=${PAUNINJAOS_BASE:-https://pau.ninja/os/releases/current}": f"PAUNINJAOS_BASE=${{PAUNINJAOS_BASE:-'{base_url}'}}",
        "RELEASE_PUBLIC_KEY_SHA256=UNCONFIGURED": f"RELEASE_PUBLIC_KEY_SHA256={digest(public_key)}",
        "RELEASE_VERSION=UNCONFIGURED": f"RELEASE_VERSION={release['version']}",
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise ReleaseError(f"Bootstrap template marker is missing or repeated: {old}")
        source = source.replace(old, new)
    atomic_write(output, source.encode())
    output.chmod(0o755)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("source-check")
    source.add_argument("root", type=Path)
    build = subparsers.add_parser("build")
    build.add_argument("raw", type=Path)
    build.add_argument("version")
    build.add_argument("base_url")
    build.add_argument("output", type=Path)
    build.add_argument("--attribution", type=Path, required=True)
    build.add_argument("--source-revision", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("directory", type=Path)
    approved = subparsers.add_parser("promote")
    approved.add_argument("directory", type=Path)
    approved.add_argument("attestation", type=Path)
    signing = subparsers.add_parser("prepare-signing")
    signing.add_argument("directory", type=Path)
    signing.add_argument("output", type=Path)
    bootstrap = subparsers.add_parser("render-bootstrap")
    bootstrap.add_argument("directory", type=Path)
    bootstrap.add_argument("manifest", type=Path)
    bootstrap.add_argument("public_key", type=Path)
    bootstrap.add_argument("signature", type=Path)
    bootstrap.add_argument("template", type=Path)
    bootstrap.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "source-check":
            validate_source(args.root.resolve())
        elif args.command == "build":
            build_bundle(
                args.raw.resolve(),
                args.version,
                args.base_url,
                args.output.resolve(),
                args.attribution.resolve(),
                args.source_revision,
            )
        elif args.command == "verify":
            validate_bundle(args.directory.resolve())
        elif args.command == "promote":
            promote(args.directory.resolve(), args.attestation.resolve())
        elif args.command == "prepare-signing":
            prepare_signing(args.directory.resolve(), args.output.resolve())
        else:
            render_bootstrap(
                args.directory.resolve(),
                args.manifest.resolve(),
                args.public_key.resolve(),
                args.signature.resolve(),
                args.template.resolve(),
                args.output.resolve(),
            )
    except ValueError as error:
        print(f"release check failed: {error}")
        return 1
    print("release check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
