{
  description = "PauNinjaOS Apple Silicon system and release image";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/83199d0d373dd3ac2b9a1996b1d0263f76ab7a4c";
    apple-silicon.url = "github:nix-community/nixos-apple-silicon/cae818c72b2138510334850a3ff435831703bd23";
    apple-silicon.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    { self, nixpkgs, apple-silicon }:
    let
      system = "aarch64-linux";
      pauninjaos = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          apple-silicon.nixosModules.apple-silicon-support
          ./nixos/configuration.nix
          ./nixos/image.nix
        ];
      };
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      nixosConfigurations.pauninjaos = pauninjaos;

      packages.${system} = {
        default = pauninjaos.config.system.build.diskImage;
        diskImage = pauninjaos.config.system.build.diskImage;
      };

      checks.${system}.source-policy = pkgs.runCommand "pauninjaos-source-policy" {
        nativeBuildInputs = [ pkgs.python3 ];
      } ''
        python3 ${self}/tests/check_release.py --source
        touch $out
      '';

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          dosfstools
          e2fsprogs
          python3
        ];
      };

      formatter.${system} = pkgs.nixfmt-tree;
    };
}
