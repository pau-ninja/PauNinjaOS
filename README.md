# PauNinjaOS

PauNinjaOS is a console-first Apple Silicon operating-system distribution intended as the base for a completely custom visual shell. It combines a declarative Linux system with the maintained Apple Silicon boot and hardware-support stack. It does not reimplement that hardware work or erase upstream attribution.

## Current status

The repository produces a source-buildable package on an AArch64 Linux builder. It is not a hardware-tested installer release. Release signing is intentionally unconfigured, so the public macOS bootstrap remains locked until the exact package passes installation, first boot, networking, update, and rollback checks on every declared Mac model.

The initial firmware set is for supported M1/M2-era machines. M3 and newer Macs are not advertised until upstream support and model-specific hardware evidence exist.

## Build

Use an AArch64 Linux builder with Nix flakes enabled:

```sh
./build-release.sh 0.1.0 https://pau.ninja/os/releases/0.1.0
```

The build refuses uncommitted source, creates a raw PauNinjaOS disk image, extracts its EFI and root partitions, packages them in the format consumed by the Apple Silicon installer, records the exact source revision, hashes, and sizes, and labels the result `SOURCE_BUILDABLE`.

The same command can run on any trusted ARM Linux builder. Source inputs are pinned to immutable commits. The package builder is deterministic for a fixed raw image; full disk-image bit reproducibility must be demonstrated by two independent builds before making that stronger claim.

## Private hardware test

The public installer stays locked before evidence exists. On the target Mac, use the reviewed local bundle to perform the first explicitly untested installation:

```sh
scripts/hardware-test-install.sh dist/0.1.0
```

The helper verifies the complete bundle, displays the target model, and requires typing the exact package digest before delegating partitioning and recovery authorization to the pinned Apple Silicon installer. It never promotes or signs the release.

## Hardware approval

Test the generated package on every Mac model you plan to advertise using firmware compatible with that model, including the installer's default firmware path. Record the exact package SHA-256 plus one or more `PAUNINJAOS_HARDWARE_TEST_V2` cases per model. Every case must include the installer and observed system firmware, timestamps, operator, retained log name and SHA-256, and successful installation, first-boot, networking, update, and rollback checks. Then run:

```sh
python3 scripts/release.py promote dist/0.1.0 hardware-test.json
```

Promotion fails if the package, installer metadata, boot-installer identity, firmware versions, or any required check differs. Sign the resulting canonical release manifest with an offline key:

```sh
scripts/sign-release.sh dist/0.1.0 /secure/path/release-private-key.pem
```

The signing command verifies the signature and creates an upload-ready `install` entrypoint with the exact release version, release directory, and public-key digest pinned. Only a promoted, signed bundle can produce that entrypoint or pass the macOS gate.

Upstream attribution, the downstream license, and the corresponding-source offer are installed under `/etc/pauninjaos` in the running system.

## Install

After hosting a promoted bundle at the URL configured by the bootstrap:

```sh
curl -L https://pau.ninja/os/install | sh
```

The bootstrap verifies release metadata and the pinned boot installer, then delegates disk preparation and required recovery authorization to the supported Apple Silicon installer. It contains no APFS or partitioning code of its own.

Apple requires a recoveryOS authorization step. PauNinjaOS cannot and should not hide or bypass it.

## Updates

Routine development uses declarative generations, not repeated installations. This is intentionally a developer channel: an immutable revision is required, but it may be newer than the last hardware-approved installer release. On the target Mac:

```sh
PAUNINJAOS_FLAKE=github:pau-ninja/PauNinjaOS/0123456789abcdef0123456789abcdef01234567 pauninjaos-update stage
```

This accepts only an immutable 40-character source revision, stages the next generation for reboot, and keeps earlier generations available. `pauninjaos-update rollback` stages the previous generation. If a new generation cannot boot, select the previous entry in the boot menu, then run the rollback command. Use SSH from the primary Mac after completing first-boot password setup.

## Visual shell

No display manager, desktop environment, compositor, panel, or placeholder GUI is included. The system boots to a console, leaving the complete visual layer available for PauNinjaOS development.

See [ATTRIBUTION.md](ATTRIBUTION.md) and [SOURCE_OFFER.md](SOURCE_OFFER.md) for upstream work, licenses, and corresponding-source availability.
