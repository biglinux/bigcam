{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        packages = {
          default = self.packages.${system}.bigcam;
          bigcam = pkgs.callPackage ./default.nix { };
        };
        devShells.default = pkgs.mkShell {
          inputsFrom = [ self.packages.${system}.default ];
          packages = with pkgs; [
            python3
            ruff
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/usr/share/biglinux/bigcam:$PYTHONPATH"
          '';
        };
      }
    );
}
