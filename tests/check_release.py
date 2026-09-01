#!/usr/bin/env python3

from __future__ import annotations

import argparse
import binascii
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TEST_REVISION = "1" * 40
SPEC = importlib.util.spec_from_file_location("pauninjaos_release", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release)

FIRMWARE_SPEC = importlib.util.spec_from_file_location("pauninjaos_firmware", ROOT / "scripts/firmware.py")
firmware = importlib.util.module_from_spec(FIRMWARE_SPEC)
assert FIRMWARE_SPEC.loader is not None
sys.modules[FIRMWARE_SPEC.name] = firmware
FIRMWARE_SPEC.loader.exec_module(firmware)

UPDATE_SPEC = importlib.util.spec_from_file_location("pauninjaos_update", ROOT / "scripts/update.py")
update = importlib.util.module_from_spec(UPDATE_SPEC)
assert UPDATE_SPEC.loader is not None
UPDATE_SPEC.loader.exec_module(update)


def newc(entries: list[tuple[str, int, bytes]]) -> bytes:
    output = bytearray()
    for inode, (name, mode, data) in enumerate(entries + [("TRAILER!!!", 0o100644, b"")], 1):
        encoded_name = name.encode("ascii") + b"\0"
        fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded_name), 0)
        output.extend(b"070701" + b"".join(f"{value:08x}".encode("ascii") for value in fields))
        output.extend(encoded_name)
        output.extend(b"\0" * (-len(output) % 4))
        output.extend(data)
        output.extend(b"\0" * (-len(output) % 4))
    return bytes(output)


def check_firmware_parser(directory: Path) -> None:
    payload = b"machine firmware\n"
    manifest = f"FILE brcm/device.bin SHA256 {hashlib.sha256(payload).hexdigest()}\n".encode("ascii")
    archive = directory / "firmware.cpio"
    archive.write_bytes(newc([
        ("vendorfw", 0o040755, b""),
        ("vendorfw/brcm", 0o040755, b""),
        ("vendorfw/brcm/device.bin", 0o100644, payload),
        ("vendorfw/.vendorfw.manifest", 0o100644, manifest),
    ]))
    target = directory / "missing-parent" / "digest"
    firmware.install_archive(archive, target)
    assert (target / "vendorfw/brcm/device.bin").read_bytes() == payload
    assert (target / ".complete").is_file()
    replacement_target = directory / "replacement-target"
    replacement_target.mkdir()
    replacement = directory / "replacement"
    replacement.symlink_to(replacement_target, target_is_directory=True)
    firmware.install_archive(archive, replacement)
    assert not replacement.is_symlink()
    assert (replacement / "vendorfw/brcm/device.bin").read_bytes() == payload

    missing_parent = directory / "missing-parent.cpio"
    missing_parent.write_bytes(newc([
        ("vendorfw", 0o040755, b""),
        ("vendorfw/brcm/device.bin", 0o100644, payload),
        ("vendorfw/.vendorfw.manifest", 0o100644, manifest),
    ]))
    try:
        firmware.parse_archive(missing_parent.read_bytes())
    except firmware.FirmwareError:
        pass
    else:
        raise AssertionError("firmware archive without explicit parents was accepted")


def check_update_script(directory: Path) -> None:
    tools = directory / "update-tools"
    tools.mkdir()
    log = directory / "update-command.log"
    downloads = directory / "update-downloads"
    downloads.mkdir()
    (downloads / "update.json").write_text("test\n", encoding="ascii")
    (downloads / "update.json.sig").write_text("test\n", encoding="ascii")
    source_payload = b"signed source archive"
    (downloads / "source.tar.gz").write_bytes(source_payload)
    source_digest = hashlib.sha256(source_payload).hexdigest()
    for name, body in {
        "id": "#!/bin/sh\necho 0\n",
        "nixos-rebuild": "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$PAUNINJAOS_TEST_LOG\"\n",
        "nix-env": "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$PAUNINJAOS_TEST_LOG\"\n",
        "curl": "#!/bin/sh\nwhile [ $# -gt 0 ]; do if [ \"$1\" = -o ]; then output=$2; shift 2; else url=$1; shift; fi; done\ncp \"$PAUNINJAOS_TEST_DOWNLOADS/${url##*/}\" \"$output\"\n",
        "pauninjaos-update-verify": f"#!/bin/sh\nif [ \"$1\" = verify ]; then printf '%s\\n%s\\n%s\\n%s\\n' 'https://updates.example.test/current/source.tar.gz' '{source_digest}' {len(source_payload)} 7; else printf 'commit %s %s\\n' \"$2\" \"$3\" >> \"$PAUNINJAOS_TEST_LOG\"; fi\n",
        "sha256sum": "#!/bin/sh\npython3 -c 'import hashlib,sys; p=sys.argv[1]; print(hashlib.sha256(open(p, \"rb\").read()).hexdigest(), p)' \"$1\"\n",
        "stat": "#!/bin/sh\nwc -c < \"$3\" | tr -d ' '\n",
        "tar": "#!/bin/sh\ncase \"$1\" in tzf) printf 'flake.nix\\n';; xzf) while [ $# -gt 0 ]; do if [ \"$1\" = -C ]; then mkdir -p \"$2\"; printf '{}\\n' > \"$2/flake.nix\"; break; fi; shift; done;; esac\n",
    }.items():
        target = tools / name
        target.write_text(body, encoding="utf-8")
        target.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "PAUNINJAOS_TEST_LOG": str(log),
        "PAUNINJAOS_TEST_DOWNLOADS": str(downloads),
        "PAUNINJAOS_UPDATE_BASE": "https://updates.example.test/current",
        "PAUNINJAOS_UPDATE_HELPER": str(tools / "pauninjaos-update-verify"),
        "PAUNINJAOS_UPDATE_ALLOWED_SIGNERS": str(directory / "allowed-signers"),
        "PAUNINJAOS_UPDATE_BASE_SERIAL": str(directory / "base-serial"),
        "PAUNINJAOS_UPDATE_STATE": str(directory / "state-serial"),
    }

    def run(action: str, source: str | None = None) -> subprocess.CompletedProcess:
        if log.exists():
            log.unlink()
        current = dict(environment)
        if source is None:
            current.pop("PAUNINJAOS_FLAKE", None)
        else:
            current["PAUNINJAOS_FLAKE"] = source
        return subprocess.run(
            ["/bin/sh", ROOT / "scripts/pauninjaos-update.sh", action],
            env=current,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    revision = "2" * 40
    staged = run("stage", f"github:pau-ninja/PauNinjaOS/{revision}")
    assert staged.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        "boot", "--flake", f"github:pau-ninja/PauNinjaOS/{revision}#pauninjaos",
    ]
    for unsafe in (
        "github:pau-ninja/PauNinjaOS/main",
        f"github:pau-ninja/PauNinjaOS/{revision};touch",
        f"github:other/PauNinjaOS/{revision}",
    ):
        rejected = run("stage", unsafe)
        assert rejected.returncode == 2 and not log.exists()
    assert run("rollback").returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["boot", "--rollback"]
    assert run("status").returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["--list-generations", "-p", "/nix/var/nix/profiles/system"]
    automatic = run("auto")
    assert automatic.returncode == 0, automatic.stderr
    automatic_log = log.read_text(encoding="utf-8").splitlines()
    assert automatic_log[:2] == ["boot", "--flake"], automatic_log
    assert automatic_log[2].startswith("path:/tmp/pauninjaos-update.") and automatic_log[2].endswith("/source#pauninjaos"), automatic_log
    assert automatic_log[3:] == [f"commit 7 {directory / 'state-serial'}"], automatic_log
    environment["PAUNINJAOS_UPDATE_BASE"] = "http://updates.example.test/current"
    assert run("auto").returncode == 2 and not log.exists()


def check_update_manifest(directory: Path) -> None:
    private_key = directory / "update-test-private"
    allowed_signers = directory / "update-test-allowed-signers"
    manifest = directory / "update.json"
    signature = directory / "update.json.sig"
    base_serial = directory / "base-serial"
    state = directory / "state-serial"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", private_key], check=True)
    public_parts = private_key.with_suffix(".pub").read_text(encoding="ascii").split()
    allowed_signers.write_text(
        f"pauninjaos-update {public_parts[0]} {public_parts[1]}\n", encoding="ascii"
    )
    revision = "4" * 40
    value = {
        "schema": update.SCHEMA,
        "serial": 2,
        "version": "0.1.3",
        "system": "aarch64-linux",
        "source_revision": revision,
        "source_url": "https://updates.example.test/current/source.tar.gz",
        "source_sha256": "5" * 64,
        "source_size": 1024,
    }
    manifest.write_bytes(update.canonical(value))
    base_serial.write_text("1\n", encoding="ascii")
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", private_key, "-n", "pauninjaos-update", manifest],
        check=True, stdout=subprocess.DEVNULL,
    )
    assert update.verify(manifest, signature, allowed_signers, base_serial, state) == value
    update.commit(2, state)
    assert state.read_text(encoding="ascii") == "2\n"
    try:
        update.verify(manifest, signature, allowed_signers, base_serial, state)
    except update.UpdateError as error:
        assert "No newer" in str(error)
    else:
        raise AssertionError("replayed update was accepted")
    value["serial"] = 3
    manifest.write_bytes(update.canonical(value))
    try:
        update.verify(manifest, signature, allowed_signers, base_serial, state)
    except update.UpdateError as error:
        assert "signature" in str(error)
    else:
        raise AssertionError("tampered update was accepted")


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

    add(0, release.EFI_TYPE, 2048, 10239, "ESP")
    add(1, release.LINUX_TYPE, 10240, 26623, "PAUNINJAOS")
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
        fat = bytearray((10239 - 2048 + 1) * sector)
        struct.pack_into("<3s8sHBHBHHBHHHII", fat, 0, b"\xebX\x90", b"PAUNINJ ", 512, 1, 32, 2, 512, 8192, 0xF8, 32, 63, 255, 0, 0)
        fat[54:62] = b"FAT16   "
        fat[510:512] = b"\x55\xaa"
        for fat_start in (32 * sector, 64 * sector):
            struct.pack_into("<14H", fat, fat_start, 0xFFF8, *(0xFFFF for _ in range(13)))
        data_start = 128 * sector

        def directory_entry(name: bytes, cluster: int) -> bytes:
            entry = bytearray(32)
            entry[:11] = name
            entry[11] = 0x10
            struct.pack_into("<H", entry, 26, cluster)
            return bytes(entry)

        def file_entry(name: bytes, cluster: int, size: int) -> bytes:
            entry = bytearray(32)
            entry[:11] = name
            entry[11] = 0x20
            struct.pack_into("<H", entry, 26, cluster)
            struct.pack_into("<I", entry, 28, size)
            return bytes(entry)

        def checksum(name: bytes) -> int:
            value = 0
            for byte in name:
                value = (((value & 1) << 7) | (value >> 1)) + byte & 0xFF
            return value

        def long_entry(name: str, short_name: bytes) -> bytes:
            encoded = (name + "\0").encode("utf-16-le").ljust(26, b"\xff")
            entry = bytearray(32)
            entry[0] = 0x41
            entry[11] = 0x0F
            entry[13] = checksum(short_name)
            entry[1:11] = encoded[:10]
            entry[14:26] = encoded[10:22]
            entry[28:32] = encoded[22:26]
            return bytes(entry)

        root_offset = 96 * sector
        fat[root_offset : root_offset + 32] = directory_entry(b"M1N1       ", 3)
        fat[root_offset + 32 : root_offset + 64] = directory_entry(b"EFI        ", 4)
        fat[root_offset + 64 : root_offset + 96] = directory_entry(b"LOADER     ", 7)
        fat[data_start + sector : data_start + sector + 32] = file_entry(b"BOOT    BIN", 2, 16)
        fat[data_start + 2 * sector : data_start + 2 * sector + 32] = directory_entry(b"BOOT       ", 5)
        fat[data_start + 2 * sector + 32 : data_start + 2 * sector + 64] = directory_entry(b"NIXOS      ", 11)
        fat[data_start + 3 * sector : data_start + 3 * sector + 32] = file_entry(b"BOOTAA64EFI", 6, 512)
        fat[data_start + 5 * sector : data_start + 5 * sector + 32] = long_entry("loader.conf", b"LOADER~1CON")
        loader_config = b"default entry.conf\n"
        fat[data_start + 5 * sector + 32 : data_start + 5 * sector + 64] = file_entry(b"LOADER~1CON", 8, len(loader_config))
        fat[data_start + 5 * sector + 64 : data_start + 5 * sector + 96] = directory_entry(b"ENTRIES    ", 9)
        fat[data_start + 7 * sector : data_start + 7 * sector + 32] = long_entry("entry+3.conf", b"ENTRY~1 CON")
        entry_config = b"linux /EFI/NIXOS/KERNEL.EFI\ninitrd /EFI/NIXOS/INITRD.EFI\n"
        fat[data_start + 7 * sector + 32 : data_start + 7 * sector + 64] = file_entry(b"ENTRY~1 CON", 10, len(entry_config))
        fat[data_start + 9 * sector : data_start + 9 * sector + 32] = file_entry(b"KERNEL  EFI", 12, 1)
        fat[data_start + 9 * sector + 32 : data_start + 9 * sector + 64] = file_entry(b"INITRD  EFI", 13, 1)
        fat[data_start : data_start + 16] = b"PAUNINJAOS-BOOT!"
        pe = bytearray(512)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 0x3C, 0x80)
        pe[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", pe, 0x84, 0xAA64)
        fat[data_start + 4 * sector : data_start + 5 * sector] = pe
        fat[data_start + 6 * sector : data_start + 6 * sector + len(loader_config)] = loader_config
        fat[data_start + 8 * sector : data_start + 8 * sector + len(entry_config)] = entry_config
        fat[data_start + 10 * sector] = 1
        fat[data_start + 11 * sector] = 1
        image.write(fat)
        image.seek(10240 * sector + 1024 + 56)
        image.write(b"\x53\xef")
        image.seek(10240 * sector + 1024 + 120)
        image.write(b"PAUNINJAOS\0\0\0\0\0\0")


def checks(source_only: bool) -> None:
    release.validate_source(ROOT)
    for model in release.SUPPORTED_TEST_MODELS:
        release.check_test_model(model)
    for model in ("", "MacBookAir", "Mac15,3", "MacBookPro19,1", "Mac14,2 "):
        try:
            release.check_test_model(model)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError(f"unsupported hardware-test model was accepted: {model!r}")
    for script in (
        ROOT / "bootstrap/install.sh",
        ROOT / "bootstrap/install-candidate.sh",
        ROOT / "scripts/hardware-test-install.sh",
        ROOT / "scripts/sign-release.sh",
        ROOT / "scripts/deploy-current.sh",
    ):
        assert "/bin/rm" not in script.read_text(encoding="utf-8")
        subprocess.run(["/bin/sh", "-n", script], check=True)
    for installer in (
        ROOT / "bootstrap/install.sh",
        ROOT / "bootstrap/install-candidate.sh",
        ROOT / "scripts/hardware-test-install.sh",
    ):
        installer_text = installer.read_text(encoding="utf-8")
        assert "22211382" in installer_text
        assert "stat -f %z" in installer_text
    assert "sudo -E" not in (ROOT / "bootstrap/install.sh").read_text(encoding="utf-8")
    assert "sudo -E" not in (ROOT / "bootstrap/install-candidate.sh").read_text(encoding="utf-8")
    assert "sudo -E" not in (ROOT / "scripts/hardware-test-install.sh").read_text(encoding="utf-8")
    assert 'false|0)' in (ROOT / "scripts/hardware-test-install.sh").read_text(encoding="utf-8")
    public_installer = (ROOT / "bootstrap/install.sh").read_text(encoding="utf-8")
    assert "metadata_package_name" in public_installer
    assert 'fetch --max-filesize "$RELEASE_PUBLIC_KEY_SIZE"' in public_installer
    assert 'fetch --max-filesize "$RELEASE_MANIFEST_SIZE"' in public_installer
    assert 'fetch --max-filesize "$RELEASE_SIGNATURE_SIZE"' in public_installer
    assert public_installer.index('export PATH=') < public_installer.index('for tool in')
    build_script = (ROOT / "build-release.sh").read_text(encoding="utf-8")
    assert build_script.count("--no-write-lock-file") == 3
    update = (ROOT / "scripts/pauninjaos-update.sh").read_text(encoding="utf-8")
    assert "github:pau-ninja/PauNinjaOS/" in update
    assert "github:pau/PauNinjaOS/" not in update
    signing = (ROOT / "scripts/sign-release.sh").read_text(encoding="utf-8")
    assert '"$script_dir/release.py" prepare-signing' in signing
    assert '"$script_dir/release.py" render-bootstrap' in signing
    deployment = (ROOT / "scripts/deploy-current.sh").read_text(encoding="utf-8")
    assert "UPDATE_SIGNATURE SOURCE_ARCHIVE" in deployment
    assert 'cp "$source_archive" "$temporary/source.tar.gz"' in deployment
    assert 'manifest["source_revision"] != release["source_revision"]' in deployment
    assert 'hashlib.sha256(source.read_bytes()).hexdigest()' in deployment
    assert 'manifest["source_url"] != f"{public_base}/source.tar.gz"' in deployment
    assert 'release["package"]["url"] != f"{public_base}/{package}"' in deployment
    assert "renameat2" in deployment
    assert "AI-NOTE.txt" in deployment
    configuration = (ROOT / "nixos/configuration.nix").read_text(encoding="utf-8")
    assert 'environment.etc."pauninjaos/update-serial" = {' in configuration
    assert 'mode = "0444";' in configuration
    assert 'Type = "oneshot";' in configuration
    assert 'RemainAfterExit = true;' in configuration
    assert "boot.blacklistedKernelModules" not in configuration
    with tempfile.TemporaryDirectory(prefix="pauninjaos-source-check-") as source_directory_name:
        source_copy = Path(source_directory_name) / "source"
        shutil.copytree(ROOT, source_copy, ignore=shutil.ignore_patterns(".git", "dist", "result"))
        (source_copy / "flake.nix").chmod(0o644)
        with (source_copy / "flake.nix").open("a", encoding="utf-8") as flake:
            flake.write('\n# mutable.url = "github:example/unsafe";\n')
        try:
            release.validate_source(source_copy)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("mutable source input was accepted")
        (source_copy / "flake.nix").write_text((ROOT / "flake.nix").read_text(encoding="utf-8"), encoding="utf-8")
        bootstrap = source_copy / "bootstrap/install.sh"
        bootstrap.chmod(0o644)
        bootstrap.write_text(bootstrap.read_text(encoding="utf-8").replace(" --proto '=https' --proto-redir '=https'", ""), encoding="utf-8")
        try:
            release.validate_source(source_copy)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("bootstrap without HTTPS-only policy was accepted")
        check_firmware_parser(Path(source_directory_name))
        check_update_script(Path(source_directory_name))
        check_update_manifest(Path(source_directory_name))
    if source_only:
        return
    with tempfile.TemporaryDirectory(prefix="pauninjaos-check-") as directory_name:
        directory = Path(directory_name)
        raw = directory / "disk.img"
        output = directory / "release"
        fake_disk(raw)
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        release.validate_bundle(output)
        candidate = output / "install-candidate"
        release.render_candidate_bootstrap(output, ROOT / "bootstrap/install-candidate.sh", candidate)
        candidate_text = candidate.read_text(encoding="utf-8")
        manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        assert candidate.stat().st_mode & 0o111
        for marker in (
            "PAUNINJAOS_BASE=UNCONFIGURED",
            "RELEASE_VERSION=UNCONFIGURED",
            "RELEASE_SHA256=UNCONFIGURED",
            "RELEASE_SIZE=UNCONFIGURED",
            "SUPPORTED_TEST_MODELS=UNCONFIGURED",
        ):
            assert marker not in candidate_text
        assert "PAUNINJAOS_BASE='https://example.test/releases'" in candidate_text
        assert f"RELEASE_VERSION={manifest['version']}" in candidate_text
        assert f"RELEASE_SHA256={release.digest(output / 'release.json')}" in candidate_text
        assert f"RELEASE_SIZE={(output / 'release.json').stat().st_size}" in candidate_text
        assert f"SUPPORTED_TEST_MODELS='{' '.join(release.SUPPORTED_TEST_MODELS)}'" in candidate_text
        assert 'case " $SUPPORTED_TEST_MODELS " in' in candidate_text
        subprocess.run(["/bin/sh", "-n", candidate], check=True)
        real_disk_usage = release.shutil.disk_usage
        release.shutil.disk_usage = lambda _path: type("FullDisk", (), {"free": 0})()
        try:
            release.validate_bundle(output)
        except release.ReleaseError as error:
            assert "temporary free space" in str(error)
        else:
            raise AssertionError("release verification ignored insufficient temporary space")
        finally:
            release.shutil.disk_usage = real_disk_usage
        original_zip_seek = zipfile.ZipExtFile.seek
        zipfile.ZipExtFile.seek = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("compressed image was random-seeked"))
        try:
            release.validate_bundle(output)
        finally:
            zipfile.ZipExtFile.seek = original_zip_seek
        package = next(output.glob("*.zip"))
        with zipfile.ZipFile(package) as archive:
            for name in ("esp.img", "root.img"):
                info = archive.getinfo(name)
                assert info.compress_type == zipfile.ZIP_DEFLATED, f"{name} is not deflated"
                assert info.compress_size < info.file_size, f"{name} is not compressed"
            assert archive.getinfo("SOURCE_OFFER.md").file_size > 0

        invalid_revision = directory / "invalid-revision"
        try:
            release.build_bundle(raw, "0.1.0", "https://example.test/releases", invalid_revision, ROOT / "ATTRIBUTION.md", "dirty", False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("non-immutable source revision was accepted")

        invalid_version = directory / "invalid-version"
        try:
            release.build_bundle(raw, "0..1", "https://example.test/releases", invalid_version, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("unsafe release version was accepted")
        assert not invalid_version.exists(), "unsafe version left partial release artifacts"

        for unsafe_url in (
            "https://example.test/releases/$(touch injected)",
            "https://example.test/releases/`touch injected`",
            "HTTPS://example.test/releases",
            "https://user@example.test/releases",
            "https://example.test/releases?channel=test",
            "https://example.test/releases/../other",
            "https://example.test/releases/%2e%2e/other",
            "https://example.test/releases/%ZZ",
            "https://example.test:bad/releases",
            "https://example.test/releases\\other",
            "https://example.test/réleases",
        ):
            try:
                release.build_bundle(raw, "0.1.0", unsafe_url, directory / "unsafe-url", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
            except release.ReleaseError:
                pass
            else:
                raise AssertionError(f"unsafe release URL was accepted: {unsafe_url}")
        assert not (directory / "injected").exists(), "release URL executed shell content"

        mismatched_package = output / "pauninjaos-9.9.9-apple-silicon.zip"
        shutil.copy2(next(output.glob("*.zip")), mismatched_package)
        mismatched_installer = json.loads((output / "installer_data.json").read_text(encoding="utf-8"))
        mismatched_installer["os_list"][0]["package"] = mismatched_package.name
        (output / "installer_data.json").write_bytes(release.canonical(mismatched_installer))
        mismatched_manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        mismatched_manifest["package"] = {
            "url": f"https://example.test/releases/{mismatched_package.name}",
            "size": mismatched_package.stat().st_size,
            "sha256": release.digest(mismatched_package),
        }
        mismatched_manifest["installer_data"] = {
            "size": (output / "installer_data.json").stat().st_size,
            "sha256": release.digest(output / "installer_data.json"),
        }
        (output / "release.json").write_bytes(release.canonical(mismatched_manifest))
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("package filename disagreed with the release version")
        mismatched_package.unlink()
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)

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
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)

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
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        first_hash = release.digest(next(output.glob("*.zip")))
        second_output = directory / "release-again"
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", second_output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        assert first_hash == release.digest(next(second_output.glob("*.zip"))), "release package is not deterministic"

        package = next(output.glob("*.zip"))
        with zipfile.ZipFile(package, "a") as archive:
            archive.writestr("../unexpected", b"unexpected")
        manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        manifest["package"] = {
            **manifest["package"],
            "size": package.stat().st_size,
            "sha256": release.digest(package),
        }
        (output / "release.json").write_bytes(release.canonical(manifest))
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("release package with unexpected content was accepted")
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)

        forged = json.loads((output / "release.json").read_text(encoding="utf-8"))
        forged.update({"status": "HARDWARE_TESTED", "installable": True, "supported_models": ["Mac14,2"], "hardware_tests": [{}]})
        (output / "release.json").write_text(json.dumps(forged), encoding="utf-8")
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("forged hardware-tested release was accepted")
        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)

        broken_boot = directory / "broken-boot.img"
        shutil.copy2(raw, broken_boot)
        with broken_boot.open("r+b") as image:
            image.seek((2048 + 96) * 512)
            image.write(b"\0" * 32)
        try:
            release.build_bundle(broken_boot, "0.1.0", "https://example.test/releases", directory / "broken-release", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("EFI image without m1n1/boot.bin was accepted")

        broken_efi = directory / "broken-efi.img"
        shutil.copy2(raw, broken_efi)
        with broken_efi.open("r+b") as image:
            image.seek((2048 + 128 + 3) * 512)
            image.write(b"\0" * 32)
        try:
            release.build_bundle(broken_efi, "0.1.0", "https://example.test/releases", directory / "broken-efi-release", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("EFI image without EFI/BOOT/BOOTAA64.EFI was accepted")

        broken_lfn = directory / "broken-lfn.img"
        shutil.copy2(raw, broken_lfn)
        with broken_lfn.open("r+b") as image:
            image.seek((2048 + 128 + 5) * 512 + 13)
            image.write(b"\0")
        try:
            release.build_bundle(broken_lfn, "0.1.0", "https://example.test/releases", directory / "broken-lfn-release", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("loader.conf with an invalid FAT long-name checksum was accepted")

        broken_default = directory / "broken-default.img"
        shutil.copy2(raw, broken_default)
        invalid_default = b"default missing*.conf\n"
        with broken_default.open("r+b") as image:
            image.seek((2048 + 128 + 5) * 512 + 32 + 28)
            image.write(struct.pack("<I", len(invalid_default)))
            image.seek((2048 + 128 + 6) * 512)
            image.write(invalid_default)
        try:
            release.build_bundle(broken_default, "0.1.0", "https://example.test/releases", directory / "broken-default-release", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("loader default without a matching boot entry was accepted")

        broken_pe = directory / "broken-pe.img"
        shutil.copy2(raw, broken_pe)
        with broken_pe.open("r+b") as image:
            image.seek((2048 + 128 + 4) * 512 + 0x84)
            image.write(b"\0\0")
        try:
            release.build_bundle(broken_pe, "0.1.0", "https://example.test/releases", directory / "broken-pe-release", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("non-AArch64 EFI executable was accepted")

        broken_initrds = directory / "broken-initrds.img"
        shutil.copy2(raw, broken_initrds)
        invalid_entry = b"linux /EFI/NIXOS/KERNEL.EFI\ninitrd /EFI/NIXOS/MISSING.EFI\ninitrd /EFI/NIXOS/INITRD.EFI\n"
        with broken_initrds.open("r+b") as image:
            image.seek((2048 + 128 + 7) * 512 + 32 + 28)
            image.write(struct.pack("<I", len(invalid_entry)))
            image.seek((2048 + 128 + 8) * 512)
            image.write(invalid_entry)
        try:
            release.build_bundle(broken_initrds, "0.1.0", "https://example.test/releases", directory / "broken-initrds-release", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("loader entry with a missing early initrd was accepted")

        undersized_fat = directory / "undersized-fat.img"
        shutil.copy2(raw, undersized_fat)
        with undersized_fat.open("r+b") as image:
            image.seek(2048 * 512 + 22)
            image.write(struct.pack("<H", 1))
        try:
            release.build_bundle(undersized_fat, "0.1.0", "https://example.test/releases", directory / "undersized-fat-release", ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("undersized FAT was accepted")

        package = next(output.glob("*.zip"))
        with package.open("ab") as changed:
            changed.write(b"corruption")
        try:
            release.validate_bundle(output)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("corrupted package was accepted")

        release.build_bundle(raw, "0.1.0", "https://example.test/releases", output, ROOT / "ATTRIBUTION.md", TEST_REVISION, False)
        manifest = json.loads((output / "release.json").read_text(encoding="utf-8"))
        try:
            release.prepare_signing(output, directory / "unapproved-release.json")
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("source-buildable release was prepared for signing")
        try:
            release.render_bootstrap(
                output,
                output / "release.json",
                directory / "missing-public-key.pem",
                directory / "missing-signature",
                ROOT / "bootstrap/install.sh",
                directory / "unapproved-install",
            )
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("source-buildable release produced an installer entrypoint")
        evidence_log = output / "hardware-test-MacBookAir10_1.log"
        evidence_log.write_text("synthetic test evidence\n", encoding="utf-8")
        completed = datetime.now(timezone.utc) - timedelta(minutes=1)
        started = completed - timedelta(minutes=30)
        cases = [{
            "model": "MacBookAir10,1",
            "installer_firmware": release.DEFAULT_INSTALLER_FIRMWARE,
            "system_firmware": "iBoot-test",
            "checks": {"install": True, "first_boot": True, "network": True, "update": True, "rollback": True},
            "evidence": {
                "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "completed_at": completed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "operator": "Pau",
                "log": evidence_log.name,
                "log_sha256": release.digest(evidence_log),
            },
        }]
        bad = directory / "bad-attestation.json"
        bad.write_text(json.dumps({
            "schema": "PAUNINJAOS_HARDWARE_TEST_V2",
            "version": manifest["version"],
            "package_sha256": "0" * 64,
            "installer_data_sha256": manifest["installer_data"]["sha256"],
            "installer_version": release.BOOT_INSTALLER_VERSION,
            "installer_sha256": release.BOOT_INSTALLER_SHA256,
            "source_revision": manifest["source_revision"],
            "cases": cases,
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
            "schema": "PAUNINJAOS_HARDWARE_TEST_V2",
            "version": manifest["version"],
            "package_sha256": manifest["package"]["sha256"],
            "installer_data_sha256": manifest["installer_data"]["sha256"],
            "installer_version": release.BOOT_INSTALLER_VERSION,
            "installer_sha256": release.BOOT_INSTALLER_SHA256,
            "source_revision": manifest["source_revision"],
            "cases": cases,
        }), encoding="utf-8")
        invalid_evidence = json.loads(good.read_text(encoding="utf-8"))
        invalid_evidence["cases"][0]["evidence"]["log_sha256"] = "not-a-digest"
        try:
            release.validate_attestation(manifest, invalid_evidence, release.SUPPORTED_FIRMWARE)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("hardware test case without valid evidence was accepted")
        unsupported_firmware = json.loads(good.read_text(encoding="utf-8"))
        unsupported_firmware["cases"][0]["installer_firmware"] = "99.9"
        try:
            release.validate_attestation(manifest, unsupported_firmware, release.SUPPORTED_FIRMWARE)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("unsupported installer firmware was accepted")
        unsupported_model = json.loads(good.read_text(encoding="utf-8"))
        unsupported_model["cases"][0]["model"] = "Mac16,1"
        try:
            release.validate_attestation(manifest, unsupported_model, release.SUPPORTED_FIRMWARE)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("unsupported hardware model was accepted for promotion")
        missing_default = json.loads(good.read_text(encoding="utf-8"))
        missing_default["cases"][0]["installer_firmware"] = "12.3"
        try:
            release.validate_attestation(manifest, missing_default, release.SUPPORTED_FIRMWARE)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("attestation without the default installer firmware was accepted")
        stale = json.loads(good.read_text(encoding="utf-8"))
        stale_started = datetime.now(timezone.utc) - release.MAX_HARDWARE_TEST_AGE - timedelta(days=1)
        stale["cases"][0]["evidence"]["started_at"] = stale_started.strftime("%Y-%m-%dT%H:%M:%SZ")
        stale["cases"][0]["evidence"]["completed_at"] = (stale_started + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            release.validate_attestation(manifest, stale, release.SUPPORTED_FIRMWARE)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("stale evidence was accepted for promotion")
        release.validate_attestation(manifest, stale, release.SUPPORTED_FIRMWARE, require_fresh=False)
        impossible_time = json.loads(good.read_text(encoding="utf-8"))
        impossible_time["cases"][0]["evidence"]["started_at"] = "2026-99-99T99:99:99Z"
        try:
            release.validate_attestation(manifest, impossible_time, release.SUPPORTED_FIRMWARE)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("impossible hardware-test timestamp was accepted")
        release.promote(output, good)
        release.validate_bundle(output)
        assert not (output / "install-candidate").exists(), "promotion retained the untested candidate entrypoint"
        try:
            release.render_candidate_bootstrap(output, ROOT / "bootstrap/install-candidate.sh", directory / "promoted-candidate")
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("promoted release produced an untested candidate installer")
        prepared = directory / "prepared-release.json"
        release.prepare_signing(output, prepared)
        assert prepared.read_bytes() == (output / "release.json").read_bytes(), "signing did not preserve the validated manifest"
        private_key = directory / "disposable-test-key.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", private_key],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run([ROOT / "scripts/sign-release.sh", output, private_key], cwd=directory, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", output / "release-public-key.pem", "-signature", output / "release.json.sig", output / "release.json"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        published_installer = (output / "install").read_text(encoding="utf-8")
        for marker in (
            "RELEASE_PUBLIC_KEY_SHA256=UNCONFIGURED",
            "RELEASE_PUBLIC_KEY_SIZE=UNCONFIGURED",
            "RELEASE_MANIFEST_SIZE=UNCONFIGURED",
            "RELEASE_SIGNATURE_SIZE=UNCONFIGURED",
            "RELEASE_VERSION=UNCONFIGURED",
        ):
            assert marker not in published_installer
        assert f"RELEASE_VERSION={manifest['version']}" in published_installer
        assert "PAUNINJAOS_BASE=${PAUNINJAOS_BASE:-'https://example.test/releases'}" in published_installer
        assert release.digest(output / "release-public-key.pem") in published_installer
        assert f"RELEASE_PUBLIC_KEY_SIZE={(output / 'release-public-key.pem').stat().st_size}" in published_installer
        assert f"RELEASE_MANIFEST_SIZE={(output / 'release.json').stat().st_size}" in published_installer
        assert f"RELEASE_SIGNATURE_SIZE={(output / 'release.json.sig').stat().st_size}" in published_installer
        assert manifest["package"]["url"].rsplit("/", 1)[0] in published_installer
        promoted = (output / "release.json").read_bytes()
        try:
            release.promote(output, good)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("already promoted release was promoted again")
        assert (output / "release.json").read_bytes() == promoted, "repeated promotion changed release metadata"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    checks(args.source)
    print("PauNinjaOS checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
