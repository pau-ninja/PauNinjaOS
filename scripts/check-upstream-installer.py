#!/usr/bin/env python3
"""Verify the exact upstream installer handoff PauNinjaOS relies on."""

from __future__ import annotations

import hashlib
import io
from pathlib import PurePosixPath
import tarfile
import urllib.request


VERSION = "v0.9.0"
SHA256 = "1dc51ec2cce25392e1eae2601c9dc1244e04cb51dbc207b51c815ead6ceeab33"
SIZE = 22211382
URL = f"https://cdn.asahilinux.org/installer/installer-{VERSION}.tar.gz"


def main() -> int:
    request = urllib.request.Request(URL, headers={"User-Agent": "PauNinjaOS release verifier"})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive_bytes = response.read()
    if len(archive_bytes) != SIZE or hashlib.sha256(archive_bytes).hexdigest() != SHA256:
        raise SystemExit("Pinned Apple Silicon installer digest changed")

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit("Pinned installer contains an unsafe archive path")

        def text(name: str) -> str:
            member = archive.extractfile(name)
            if member is None:
                raise SystemExit(f"Pinned installer lacks {name}")
            return member.read().decode("utf-8")

        launcher = text("./install.sh")
        main_source = text("./main.py")
        osinstall = text("./osinstall.py")

    required = (
        (launcher, 'exec $python main.py "$@"'),
        (main_source, 'json.load(open("installer_data.json"))'),
        (osinstall, 'os.environ.get("REPO_BASE", ".") + "/os/" + package'),
        (osinstall, 'zipfile.ZipFile(open(package, "rb"))'),
    )
    if any(needle not in source for source, needle in required):
        raise SystemExit("Pinned installer no longer consumes verified local metadata and package bytes")
    print("Pinned installer contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
