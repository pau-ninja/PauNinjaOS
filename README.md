# PauNinjaOS

PauNinjaOS is a console-first Apple Silicon operating-system distribution intended as the base for a completely custom visual shell. It combines a declarative Linux system with the maintained Apple Silicon boot and hardware-support stack. It does not reimplement that hardware work or erase upstream attribution.

## Current status

The repository produces a source-buildable package on an AArch64 Linux builder. It is not a hardware-tested installer release. Release signing is intentionally unconfigured, so the public macOS bootstrap remains locked until the exact package passes installation, first boot, networking, update, and rollback checks on every declared Mac model.

The initial firmware set is for supported M1/M2-era machines. M3 and newer Macs are not advertised until upstream support and model-specific hardware evidence exist.

## Build

Use an AArch64 Linux builder with Nix flakes enabled:

```sh
./build-release.sh VERSION https://vps-308188fb.vps.ovh.us/current
```

The build refuses uncommitted source, creates a raw PauNinjaOS disk image, extracts its EFI and root partitions, packages them in the format consumed by the Apple Silicon installer, records the exact source revision, hashes, and sizes, and labels the result `SOURCE_BUILDABLE`.

The same command can run on any trusted ARM Linux builder. Source inputs are pinned to immutable commits. The package builder is deterministic for a fixed raw image; full disk-image bit reproducibility must be demonstrated by two independent builds before making that stronger claim.

## Private hardware test

The public installer stays locked before evidence exists. On the target Mac, use the reviewed local bundle to perform the first explicitly untested installation:

```sh
scripts/hardware-test-install.sh dist/VERSION
```

The helper verifies the complete bundle, displays the target model, and requires typing the exact package digest before delegating partitioning and recovery authorization to the pinned Apple Silicon installer. It never promotes or signs the release.

After uploading that exact source-buildable directory, download the generated candidate entrypoint. Verify its SHA-256 against the separately published reviewed digest before running it; never pipe the download directly into a shell:

```sh
curl -fL --proto '=https' --proto-redir '=https' -o /tmp/pauninjaos-install-candidate https://pau.ninja/os/releases/VERSION/install-candidate
printf '%s  %s\n' REVIEWED_INSTALL_CANDIDATE_SHA256 /tmp/pauninjaos-install-candidate | shasum -a 256 -c -
/bin/sh /tmp/pauninjaos-install-candidate
```

The maintained test route can provide the same verification and execution as one terminal line. Its pinned digest must be updated for each release:

```sh
curl -fL --proto '=https' --proto-redir '=https' -o /tmp/pauninjaos-install https://pau.ninja/instalar && printf '%s  %s\n' REVIEWED_INSTALL_CANDIDATE_SHA256 /tmp/pauninjaos-install | shasum -a 256 -c - && /bin/sh /tmp/pauninjaos-install
```

The build prints the candidate entrypoint digest. Publish it with the immutable tagged source release, then use that independently published value in the command above; never take it from the download host. The candidate entrypoint pins the release metadata, package, and boot installer, rejects Macs outside the M1/M2 MacBook test allowlist, requires an interactive terminal and exact package-digest confirmation, and refuses any release marked production-installable. This path is for the first hardware test only; it never replaces the signed production installer.

## Hardware approval

Test the generated package on every Mac model you plan to advertise using firmware compatible with that model, including the installer's default firmware path. Record the exact package SHA-256 plus one or more `PAUNINJAOS_HARDWARE_TEST_V2` cases per model. Every case must include the installer and observed system firmware, timestamps, operator, retained log name and SHA-256, and successful installation, first-boot, networking, update, and rollback checks. Then run:

```sh
python3 scripts/release.py promote dist/VERSION hardware-test.json
```

Promotion fails if the package, installer metadata, boot-installer identity, firmware versions, or any required check differs. Sign the resulting canonical release manifest with an offline key:

Promotion removes the local candidate entrypoint. Remove the hosted candidate object when uploading the promoted directory; any forgotten copy is hash-pinned to the old manifest and fails closed.

```sh
scripts/sign-release.sh dist/VERSION /secure/path/release-private-key.pem
```

The signing command verifies the signature and creates an upload-ready `install` entrypoint with the exact release version, release directory, and public-key digest pinned. Only a promoted, signed bundle can produce that entrypoint or pass the macOS gate.

Upstream attribution, the downstream license, and the corresponding-source offer are installed under `/etc/pauninjaos` in the running system.

## Install

After hosting a promoted bundle at the URL configured by the bootstrap:

```sh
curl -fL --proto '=https' --proto-redir '=https' https://pau.ninja/os/install | sh
```

The bootstrap verifies release metadata and the pinned boot installer, then delegates disk preparation and required recovery authorization to the supported Apple Silicon installer. It contains no APFS or partitioning code of its own.

Apple requires a recoveryOS authorization step. PauNinjaOS cannot and should not hide or bypass it.

## Automatic updates

PauNinjaOS checks its signed current channel after networking starts and every six hours. It verifies the signed source archive, builds it, and stages the result as the next boot generation without replacing the running system. The previous generations remain available, and boot counting returns to a working generation if the new one cannot start. The update trust key is part of the installed image and is never downloaded from the update server. Metadata uses OpenSSH signatures and a monotonic serial.

Manual development still uses declarative generations, not repeated installations. An immutable revision is required:

```sh
PAUNINJAOS_FLAKE=github:pau-ninja/PauNinjaOS/0123456789abcdef0123456789abcdef01234567 pauninjaos-update stage
```

This accepts only an immutable 40-character source revision. `pauninjaos-update auto` performs the same staging only after the channel signature and monotonic serial pass verification. `pauninjaos-update rollback` stages the previous generation. If a new generation cannot boot, select the previous entry in the boot menu, then run the rollback command. Use SSH from the primary Mac after completing first-boot password setup.

## Visual shell

No display manager, desktop environment, compositor, panel, or placeholder GUI is included. The system boots to a console, leaving the complete visual layer available for PauNinjaOS development.

See [ATTRIBUTION.md](ATTRIBUTION.md) and [SOURCE_OFFER.md](SOURCE_OFFER.md) for upstream work, licenses, and corresponding-source availability.
