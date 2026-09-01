from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit


SCHEMA = "PAUNINJAOS_UPDATE_V1"
FIELDS = {
    "schema", "serial", "version", "system", "source_revision",
    "source_url", "source_sha256", "source_size",
}
REVISION = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHA256 = re.compile(r"[0-9a-f]{64}")


class UpdateError(Exception):
    pass


def reject_duplicate(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise UpdateError("Update manifest contains a duplicate field")
        value[key] = item
    return value


def canonical(value: object) -> bytes:
    rendered = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return (rendered + "\n").encode()


def load_manifest(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=reject_duplicate)
    except (
        OSError, UnicodeDecodeError, json.JSONDecodeError, UpdateError
    ) as error:
        raise UpdateError("Update manifest is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or raw != canonical(value)
    ):
        raise UpdateError("Update manifest is not canonical")
    if value["schema"] != SCHEMA or value["system"] != "aarch64-linux":
        raise UpdateError("Update manifest targets an unsupported system")
    if (
        not isinstance(value["serial"], int)
        or isinstance(value["serial"], bool)
        or value["serial"] < 1
    ):
        raise UpdateError("Update serial is invalid")
    if (
        not isinstance(value["version"], str)
        or VERSION.fullmatch(value["version"]) is None
    ):
        raise UpdateError("Update version is invalid")
    revision = value["source_revision"]
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise UpdateError("Update source revision is invalid")
    source_url = value["source_url"]
    if not isinstance(source_url, str):
        raise UpdateError("Update source address is invalid")
    parsed = urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/source.tar.gz")
    ):
        raise UpdateError("Update source address is invalid")
    if (
        not isinstance(value["source_sha256"], str)
        or SHA256.fullmatch(value["source_sha256"]) is None
    ):
        raise UpdateError("Update source digest is invalid")
    if (
        not isinstance(value["source_size"], int)
        or isinstance(value["source_size"], bool)
        or not 1 <= value["source_size"] <= 16 * 1024 * 1024
    ):
        raise UpdateError("Update source size is invalid")
    return value


def read_serial(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink() or not path.is_file():
        raise UpdateError("Update serial state is unsafe")
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError as error:
        raise UpdateError("Update serial state cannot be read") from error
    if not value.isascii() or not value.isdigit() or int(value) < 0:
        raise UpdateError("Update serial state is invalid")
    return int(value)


def verify(
    manifest: Path,
    signature: Path,
    allowed_signers: Path,
    base_serial: Path,
    state: Path,
) -> dict:
    value = load_manifest(manifest)
    checked = subprocess.run(
        [
            "ssh-keygen", "-Y", "verify", "-f", str(allowed_signers),
            "-I", "pauninjaos-update", "-n", "pauninjaos-update",
            "-s", str(signature),
        ],
        input=manifest.read_bytes(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if checked.returncode != 0:
        raise UpdateError("Update signature is invalid")
    floor = max(read_serial(base_serial), read_serial(state))
    if value["serial"] <= floor:
        raise UpdateError("No newer PauNinjaOS update is available")
    return value


def commit(serial: int, state: Path) -> None:
    if serial < 1:
        raise UpdateError("Update serial is invalid")
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.exists() and read_serial(state) >= serial:
        return
    with tempfile.NamedTemporaryFile(
        "w", encoding="ascii", dir=state.parent,
        prefix=".update-serial.", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(f"{serial}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, state)
    directory = os.open(state.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verifier = subparsers.add_parser("verify")
    names = ("manifest", "signature", "allowed_signers", "base_serial", "state")
    for name in names:
        verifier.add_argument(name, type=Path)
    committer = subparsers.add_parser("commit")
    committer.add_argument("serial", type=int)
    committer.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            value = verify(
                args.manifest, args.signature, args.allowed_signers,
                args.base_serial, args.state,
            )
            print(value["source_url"])
            print(value["source_sha256"])
            print(value["source_size"])
            print(value["serial"])
        else:
            commit(args.serial, args.state)
    except UpdateError as error:
        print(str(error), file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
