#!/usr/bin/env python3

from __future__ import annotations

import argparse
import binascii
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pauninjaos_release", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release)


def fake_disk(path: Path) -> None:
    sector = 512
    sectors = 32768
    entries_lba = 2
    entry_count = 128
    entry_size = 128
    entries = bytearray(entry_count * entry_size)

    def add(index: int, type_id: uuid.UUID, first: int, last: int, name: str) -> None:
        offset = index * entry_size
        entries[offset : offset + 16] = type_id.bytes_le
        entries[offset + 16 : offset + 32] = uuid.uuid4().bytes_le
        struct.pack_into("<QQQ", entries, offset + 32, first, last, 0)
        encoded = name.encode("utf-16-le")
        entries[offset + 56 : offset + 56 + len(encoded)] = encoded

    add(0, release.EFI_TYPE, 2048, 6143, "ESP")
    add(1, release.LINUX_TYPE, 6144, 22527, "PAUNINJAOS")
    entries_crc = binascii.crc32(entries) & 0xFFFFFFFF
    header = bytearray(512)
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        header,
        0,
        b"EFI PART",
        0x00010000,
        92,
        0,
        0,
        1,
        sectors - 1,
        34,
        sectors - 34,
        uuid.uuid4().bytes_le,
        entries_lba,
        entry_count,
        entry_size,
        entries_crc,
    )
    header_crc = binascii.crc32(header[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, header_crc)
    with path.open("wb") as image:
        image.truncate(sectors * sector)
        image.seek(sector)
        image.write(header)
        image.seek(entries_lba * sector)
        image.write(entries)
        image.seek(2048 * sector)
        fat = bytearray((6143 - 2048 + 1) * sector)
        struct.pack_into("<3s8sHBHBHHBHHHII", fat, 0, b"\xebX\x90", b"PAUNINJ ", 512, 1, 32, 2, 0, 0, 0xF8, 0, 63, 255, 0, 4096)
        struct.pack_into("<IHHIHH12sHBBI11s8s", fat, 36, 32, 0, 0, 2, 1, 6, b"\0" * 12, 0x80, 0, 0x29, 0x12345678, b"ESP        ", b"FAT32   ")
        fat[510:512] = b"\x55\xaa"
        for fat_start in (32 * sector, 64 * sector):
            struct.pack_into("<IIIII", fat, fat_start, 0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF, 0x0FFFFFFF, 0x0FFFFFFF)
        data_start = 96 * sector
        root_entry = bytearray(32)
        root_entry[:11] = b"M1N1       "
        root_entry[11] = 0x10
        struct.pack_into("<H", root_entry, 26, 3)
        fat[data_start : data_start + 32] = root_entry
        boot_entry = bytearray(32)
        boot_entry[:11] = b"BOOT    BIN"
        boot_entry[11] = 0x20
        struct.pack_into("<H", boot_entry, 26, 4)
        struct.pack_into("<I", boot_entry, 28, 16)
        fat[data_start + sector : data_start + sector + 32] = boot_entry
        fat[data_start + 2 * sector : data_start + 2 * sector + 16] = b"PAUNINJAOS-BOOT!"
        image.write(fat)
        image.seek(6144 * sector + 1024 + 56)
        image.write(b"\x53\xef")
        image.seek(6144 * sector + 1024 + 120)
        image.write(b"PAUNINJAOS\0\0\0\0\0\0")


def checks(source_only: bool) -> None:
    release.validate_source(ROOT)
    update = (ROOT / "scripts/pauninjaos-update.sh").read_text(encoding="utf-8")
    assert "github:pau-ninja/PauNinjaOS/" in update
    assert "github:pau/PauNinjaOS/" not in update
    with tempfile.TemporaryDirectory(prefix="pauninjaos-source-check-") as source_directory_name:
        source_copy = Path(source_directory_name) / "source"
        shutil.copytree(ROOT, source_copy)
        (source_copy / "flake.nix").chmod(0o644)
        with (source_copy / "flake.nix").open("a", encoding="utf-8") as flake:
            flake.write('\n# mutable.url = "github:example/unsafe";\n')
        try:
            release.validate_source(source_copy)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("mutable source input was accepted")
    if source_only:
        return
    with tempfile.TemporaryDirectory(prefix="pauninjaos-check-") as directory_name:
        directory = Path(directory_name)
        raw = directory / "disk.img"
        output = directory / "release"
        fake_disk(raw)
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", False)
        release.validate_bundle(output)
        assert next(output.glob("*.zip")).stat().st_size < raw.stat().st_size, "release package is not compressed"

        invalid_version = directory / "invalid-version"
        try:
            release.build_bundle(raw, "0..1", "https://example.test/releases", invalid_version, ROOT / "ATTRIBUTION.md", False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("unsafe release version was accepted")
        assert not invalid_version.exists(), "unsafe version left partial release artifacts"

        invalid_partitions = json.loads((output / "installer_data.json").read_text(encoding="utf-8"))
        invalid_partitions["os_list"][0]["partitions"] = ["EFI", "Root"]
        (output / "installer_data.json").write_bytes(release.canonical(invalid_partitions))
        manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        manifest["installer_data"] = {
            "size": (output / "installer_data.json").stat().st_size,
            "sha256": release.digest(output / "installer_data.json"),
        }
        (output / "release.json").write_bytes(release.canonical(manifest))
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("non-object partition metadata was accepted")
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", False)

        invalid_firmware = json.loads((output / "installer_data.json").read_text(encoding="utf-8"))
        invalid_firmware["os_list"][0]["supported_fw"] = ["14.8.3"]
        (output / "installer_data.json").write_bytes(release.canonical(invalid_firmware))
        manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        manifest["installer_data"] = {
            "size": (output / "installer_data.json").stat().st_size,
            "sha256": release.digest(output / "installer_data.json"),
        }
        (output / "release.json").write_bytes(release.canonical(manifest))
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("unsupported firmware compatibility was accepted")
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", False)
        first_hash = release.digest(next(output.glob("*.zip")))
        second_output = directory / "release-again"
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", second_output, ROOT / "ATTRIBUTION.md", False)
        assert first_hash == release.digest(next(second_output.glob("*.zip"))), "release package is not deterministic"

        forged = json.loads((output / "release.json").read_text(encoding="utf-8"))
        forged.update({"status": "HARDWARE_TESTED", "installable": True, "supported_models": ["Mac14,2"], "hardware_tests": [{}]})
        (output / "release.json").write_text(json.dumps(forged), encoding="utf-8")
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("forged hardware-tested release was accepted")
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", False)

        broken_boot = directory / "broken-boot.img"
        shutil.copy2(raw, broken_boot)
        with broken_boot.open("r+b") as image:
            image.seek((2048 + 97) * 512)
            image.write(b"\0" * 32)
        try:
            release.build_bundle(broken_boot, "0.1.0", "https://example.test/releases", directory / "broken-release", ROOT / "ATTRIBUTION.md", False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("EFI image without m1n1/boot.bin was accepted")

        package = next(output.glob("*.zip"))
        with package.open("ab") as changed:
            changed.write(b"corruption")
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("corrupted package was accepted")

        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", False)
        manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        bad = directory / "bad-attestation.json"
        bad.write_text(json.dumps({
            "schema": "PAUNINJAOS_HARDWARE_TEST_V1",
            "version": manifest["version"],
            "package_sha256": "0" * 64,
            "installer_data_sha256": manifest["installer_data"]["sha256"],
            "installer_version": release.BOOT_INSTALLER_VERSION,
            "installer_sha256": release.BOOT_INSTALLER_SHA256,
            "models": ["MacBookAir10,1"],
            "firmware_versions": release.SUPPORTED_FIRMWARE,
            "checks": {"install": True, "first_boot": True, "network": True, "update": True, "rollback": True},
        }), encoding="utf-8")
        release_before_failed_promotion = (output / "release.json").read_bytes()
        try:
            release.promote(output, bad)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("wrong hardware attestation was accepted")
        assert (output / "release.json").read_bytes() == release_before_failed_promotion, "failed promotion changed release metadata"

        good = directory / "good-attestation.json"
        good.write_text(json.dumps({
            "schema": "PAUNINJAOS_HARDWARE_TEST_V1",
            "version": manifest["version"],
            "package_sha256": manifest["package"]["sha256"],
            "installer_data_sha256": manifest["installer_data"]["sha256"],
            "installer_version": release.BOOT_INSTALLER_VERSION,
            "installer_sha256": release.BOOT_INSTALLER_SHA256,
            "models": ["MacBookAir10,1"],
            "firmware_versions": release.SUPPORTED_FIRMWARE,
            "checks": {"install": True, "first_boot": True, "network": True, "update": True, "rollback": True},
        }), encoding="utf-8")
        release.promote(output, good)
        release.validate_bundle(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    checks(args.source)
    print("PauNinjaOS checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
