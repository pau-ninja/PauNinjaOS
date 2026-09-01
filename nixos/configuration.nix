{ config, lib, pkgs, ... }:

let
  updateVerifier = pkgs.writers.writePython3Bin "pauninjaos-update-verify" { } (
    builtins.readFile ../scripts/update.py
  );
  updateTool = pkgs.writeShellApplication {
    name = "pauninjaos-update";
    runtimeInputs = [ pkgs.coreutils pkgs.curl pkgs.gnugrep pkgs.gnutar pkgs.openssh updateVerifier ];
    text = builtins.readFile ../scripts/pauninjaos-update.sh;
  };
  firmwareTool = pkgs.writers.writePython3Bin "pauninjaos-firmware" { } (
    builtins.readFile ../scripts/firmware.py
  );
in
{
  assertions = [
    {
      assertion = !config.services.xserver.enable;
      message = "PauNinjaOS base images must stay console-only.";
    }
  ];

  nixpkgs.hostPlatform = lib.mkDefault "aarch64-linux";
  hardware.asahi.enable = true;
  hardware.asahi.extractPeripheralFirmware = false;

  boot.loader.systemd-boot = {
    enable = true;
    configurationLimit = 10;
    bootCounting.enable = true;
  };
  boot.loader.efi.canTouchEfiVariables = false;
  boot.kernelParams = [ "firmware_class.path=/var/lib/pauninjaos-firmware/current" ];

  fileSystems."/" = {
    device = "/dev/disk/by-label/PAUNINJAOS";
    fsType = "ext4";
    autoResize = true;
  };
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/ESP";
    fsType = "vfat";
  };

  system.nixos = {
    distroId = "pauninjaos";
    distroName = "PauNinjaOS";
    vendorId = "pau";
    vendorName = "Pau";
    extraOSReleaseArgs = {
      HOME_URL = "https://pau.ninja/os";
      DOCUMENTATION_URL = "https://pau.ninja/os/docs";
      SUPPORT_URL = "https://pau.ninja/os/support";
      BUG_REPORT_URL = "https://pau.ninja/os/issues";
    };
  };
  system.stateVersion = "26.05";
  networking.hostName = "pauninjaos";
  networking.networkmanager = {
    enable = true;
    wifi.backend = "iwd";
  };

  services.xserver.enable = false;
  services.getty = {
    greetingLine = "<<< Welcome to PauNinjaOS (\\m) - \\l >>>";
    helpLine = lib.mkForce "";
  };
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = true;
      PermitRootLogin = "no";
    };
  };

  users.users.pau = {
    isNormalUser = true;
    description = "PauNinjaOS owner";
    extraGroups = [ "networkmanager" "wheel" ];
    initialHashedPassword = "!";
  };
  security.sudo.wheelNeedsPassword = true;

  systemd.services.pauninjaos-first-boot = {
    description = "Create the initial PauNinjaOS password";
    wantedBy = [ "multi-user.target" ];
    before = [ "getty@tty1.service" "sshd.service" ];
    unitConfig.ConditionPathExists = "!/var/lib/pauninjaos/provisioned";
    serviceConfig = {
      Type = "oneshot";
      StandardInput = "tty-force";
      StandardOutput = "tty";
      StandardError = "tty";
      TTYPath = "/dev/tty1";
    };
    script = ''
      echo "Set the password for the pau account."
      ${pkgs.shadow}/bin/passwd pau
      install -d -m 0700 /var/lib/pauninjaos
      touch /var/lib/pauninjaos/provisioned
    '';
  };

  systemd.services.pauninjaos-firmware = {
    description = "Load machine-specific Apple firmware copied by the installer";
    wantedBy = [ "multi-user.target" ];
    before = [ "NetworkManager.service" ];
    path = [ pkgs.coreutils firmwareTool pkgs.kmod ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      archive=/boot/vendorfw/firmware.cpio
      if [ ! -f "$archive" ]; then
        echo "Installer firmware archive is missing." >&2
        exit 1
      fi
      digest=$(sha256sum "$archive" | cut -d ' ' -f 1)
      target=/var/lib/pauninjaos-firmware/$digest
      if [ ! -e "$target/.complete" ]; then
        pauninjaos-firmware "$archive" "$target"
      fi
      manifest="$target/vendorfw/.vendorfw.manifest"
      if [ ! -f "$manifest" ]; then
        echo "Installer firmware archive lacks its integrity manifest." >&2
        exit 1
      fi
      install -d -m 0755 /var/lib/pauninjaos-firmware
      ln -sfn "$target/vendorfw" /var/lib/pauninjaos-firmware/current
      if ! modprobe brcmfmac; then
        echo "Wi-Fi firmware could not be loaded; console recovery remains available." >&2
      fi
      if ! modprobe hci_bcm4377; then
        echo "Bluetooth firmware could not be loaded; console recovery remains available." >&2
      fi
    '';
  };

  systemd.services.pauninjaos-update = {
    description = "Stage verified PauNinjaOS system updates";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${updateTool}/bin/pauninjaos-update auto";
      Nice = 10;
      IOSchedulingClass = "idle";
    };
  };

  systemd.timers.pauninjaos-update = {
    description = "Check for PauNinjaOS system updates";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "15m";
      OnUnitActiveSec = "6h";
      RandomizedDelaySec = "30m";
      Persistent = true;
    };
  };

  environment.systemPackages = with pkgs; [
    git
    iw
    updateTool
  ];
  environment.etc."motd".text = ''
    PauNinjaOS is console-first. Stage updates with pauninjaos-update.
  '';
  environment.etc."pauninjaos/ATTRIBUTION.md".source = ../ATTRIBUTION.md;
  environment.etc."pauninjaos/LICENSE".source = ../LICENSE;
  environment.etc."pauninjaos/SOURCE_OFFER.md".source = ../SOURCE_OFFER.md;
  environment.etc."pauninjaos/update-allowed-signers".source = ../release/update-allowed-signers;
  environment.etc."pauninjaos/update-serial" = {
    mode = "0444";
    text = "1\n";
  };

  nix = {
    settings.experimental-features = [ "nix-command" "flakes" ];
    gc.automatic = false;
  };
}
