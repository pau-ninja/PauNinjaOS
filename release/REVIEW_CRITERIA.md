# PauNinjaOS release criteria

Review the supplied source as a console-first Apple Silicon distribution and installer-release pipeline.

The source must:

1. Build an AArch64 disk image from immutable inputs, bind every package to its exact source revision, and avoid claiming bit reproducibility until two independent image builds match.
2. Produce the exact package and metadata format consumed by the pinned Apple Silicon installer.
3. Verify the installer, signed release metadata, package, sizes, boot chain, filesystems, firmware payload, and target Mac model before destructive installation begins.
4. Keep the public installer locked until real installation, first boot, networking, update, and rollback evidence exists for every advertised Mac model and the promoted release is signed.
5. Preserve upstream licenses and attribution while presenting PauNinjaOS as the user-facing product.
6. Boot without a graphical desktop and leave a safe first-login path for building a custom visual shell.
7. Support immutable remote system updates and rollback without routine reinstallation.
8. Fail closed on malformed metadata, unsafe paths, corrupted images, missing firmware, stale evidence, unsigned releases, and unsupported hardware.
9. Include runnable checks that detect material regressions in the release and installation gates.
10. Provide an explicit maintainer-only path for gathering first-install evidence without weakening the locked public installer.

A source-only build may pass while remaining deliberately non-installable. No reviewer may infer successful real-hardware installation from source checks or synthetic fixtures.
