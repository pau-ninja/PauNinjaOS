{ config, lib, modulesPath, pkgs, ... }:

{
  system.build.diskImage = import "${modulesPath}/../lib/make-disk-image.nix" {
    inherit config lib pkgs;
    name = "pauninjaos-disk-image";
    baseName = "pauninjaos";
    format = "raw";
    partitionTableType = "efi";
    bootSize = "2048M";
    additionalSpace = "2G";
    label = "PAUNINJAOS";
    deterministic = true;
    copyChannel = false;
    installBootLoader = true;
  };
}
