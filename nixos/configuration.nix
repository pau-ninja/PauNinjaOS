{ config, lib, pkgs, ... }:

let
  updateTool = pkgs.writeShellApplication {
    name = "pauninjaos-update";
    runtimeInputs = [ pkgs.gnugrep ];
    text = builtins.readFile ../scripts/pauninjaos-update.sh;
  };
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
    unitConfig.ConditionPathExists = "/boot/vendorfw/firmware.cpio";
    path = [ pkgs.coreutils pkgs.cpio pkgs.kmod ];
    script = ''
      archive=/boot/vendorfw/firmware.cpio
      digest=$(sha256sum "$archive" | cut -d ' ' -f 1)
      target=/var/lib/pauninjaos-firmware/$digest
      if [ ! -e "$target/.complete" ]; then
        if [ -e "$target" ]; then
          mv "$target" "$target-incomplete-$(date +%s)"
        fi
        install -d -m 0755 "$target"
        cd "$target"
        cpio -id --quiet --no-absolute-filenames < "$archive"
        touch "$target/.complete"
      fi
      install -d -m 0755 /var/lib/pauninjaos-firmware
      ln -sfn "$target/vendorfw" /var/lib/pauninjaos-firmware/current
      modprobe brcmfmac
      modprobe hci_bcm4377
    '';
  };

  boot.blacklistedKernelModules = [ "brcmfmac" "hci_bcm4377" ];

  environment.systemPackages = with pkgs; [
    git
    iw
    updateTool
  ];
  environment.etc."motd".text = ''
    PauNinjaOS is console-first. Stage updates with pauninjaos-update.
  '';

  nix = {
    settings.experimental-features = [ "nix-command" "flakes" ];
    gc.automatic = false;
  };
}
