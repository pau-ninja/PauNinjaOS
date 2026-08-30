# PauNinjaOS

PauNinjaOS is a console-first Apple Silicon operating-system distribution intended as the base for a completely custom visual shell. It combines a declarative Linux system with the maintained Apple Silicon boot and hardware-support stack. It does not reimplement that hardware work or erase upstream attribution.

## Current status

The repository is a source prototype, not a built or hardware-tested installer release. Its source checks pass locally; the Nix image build still requires an AArch64 Linux builder. Release signing is intentionally unconfigured, so the macOS bootstrap remains locked until a trusted public key is pinned. It then still refuses installation until the exact built package passes installation, first boot, networking, update, and rollback checks on each declared Mac model.

## Build

Use an AArch64 Linux builder with Nix flakes enabled:

```sh
./build-release.sh 0.1.0 https://pau.ninja/os/releases/0.1.0
```

The build creates a raw PauNinjaOS disk image, extracts its EFI and root partitions, packages them in the format consumed by the Apple Silicon installer, generates installer metadata, records hashes and sizes, and labels the result `SOURCE_BUILDABLE`.

The included GitHub workflow performs the same build on an ARM runner. Inputs and workflow actions are pinned to immutable commits.

## Hardware approval

Test the generated package on every Mac model you plan to advertise. Record the exact package SHA-256 and successful checks in a `PAUNINJAOS_HARDWARE_TEST_V1` JSON object, then run:

```sh
python3 scripts/release.py promote dist/0.1.0 hardware-test.json
```

Promotion fails if the package, installer metadata, boot-installer identity, firmware versions, or any required check differs. Sign the resulting canonical release manifest with an offline key, host its detached signature and public key, then pin that public key's SHA-256 in the bootstrap. Only a promoted, signed bundle can pass the macOS gate.

## Install

After hosting a promoted bundle at the URL configured by the bootstrap:

```sh
curl -L https://pau.ninja/os/install | sh
```

The bootstrap verifies release metadata and the pinned boot installer, then delegates disk preparation and required recovery authorization to the supported Apple Silicon installer. It contains no APFS or partitioning code of its own.

Apple requires a recoveryOS authorization step. PauNinjaOS cannot and should not hide or bypass it.

## Updates

Routine development uses declarative generations, not repeated installations. On the target Mac:

```sh
PAUNINJAOS_FLAKE=github:pau-ninja/PauNinjaOS/0123456789abcdef0123456789abcdef01234567 pauninjaos-update stage
```

This accepts only an immutable 40-character source revision, stages the next generation for reboot, and keeps earlier generations available. `pauninjaos-update rollback` stages the previous generation. If a new generation cannot boot, select the previous entry in the boot menu, then run the rollback command. Use SSH from the primary Mac after completing first-boot password setup.

## Visual shell

No display manager, desktop environment, compositor, panel, or placeholder GUI is included. The system boots to a console, leaving the complete visual layer available for PauNinjaOS development.

See [ATTRIBUTION.md](ATTRIBUTION.md) for upstream work and licenses.
