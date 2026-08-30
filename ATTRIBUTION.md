# Upstream attribution

PauNinjaOS is a downstream integration, not a clean-room operating system. It uses NixOS and nixpkgs for the declarative system, and the nixos-apple-silicon project for Apple Silicon integration. That work relies on the Asahi Linux project, including its Linux kernel work, m1n1, U-Boot integration, firmware tooling, Mesa driver work, and installer.

Source and license information:

- NixOS and nixpkgs: https://github.com/NixOS/nixpkgs
- NixOS Apple Silicon support: https://github.com/nix-community/nixos-apple-silicon
- Asahi Linux: https://asahilinux.org and https://github.com/AsahiLinux
- Linux kernel: https://kernel.org
- U-Boot: https://source.denx.de/u-boot/u-boot

No upstream project endorses PauNinjaOS. Their names remain in source references, diagnostics, package metadata, and legal notices. PauNinjaOS branding applies only to the downstream product experience.
