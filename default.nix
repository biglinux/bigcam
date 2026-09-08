{
  lib,
  stdenv,
  python3,
  wrapGAppsHook4,
  gobject-introspection,
  makeWrapper,
  gtk4,
  libadwaita,
  gst_all_1,
  ffmpeg,
  v4l-utils,
  gphoto2,
  pipewire,
  libcamera,
  zbar,
  polkit,
  kmod,
  linuxPackages,
  openssl,
  pulseaudio,
}:

let
  pythonEnv = python3.withPackages (ps:
    with ps; [
      pygobject3
      pycairo
      numpy
      opencv4
      qrcode
      aiohttp
      pillow
    ]
  );
in
stdenv.mkDerivation rec {
  pname = "bigcam";
  version = "4.5.0";

  src = ./.;

  nativeBuildInputs = [
    wrapGAppsHook4
    gobject-introspection
    makeWrapper
  ];

  buildInputs = [
    gtk4
    libadwaita
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
    gst_all_1.gst-plugins-good
    gst_all_1.gst-plugins-bad
    gst_all_1.gst-plugins-ugly
    gst_all_1.gst-plugin-gtk4
    ffmpeg
    v4l-utils
    gphoto2
    pipewire
    libcamera
    zbar
    polkit
  ];

  dontBuild = true;
  dontConfigure = true;
  dontWrapGApps = true;

  installPhase = ''
    runHook preInstall

    # Application files
    mkdir -p $out/share/biglinux/bigcam
    cp -r usr/share/biglinux/bigcam/* $out/share/biglinux/bigcam/

    # Desktop file
    mkdir -p $out/share/applications
    cp usr/share/applications/*.desktop $out/share/applications/

    # System icons
    mkdir -p $out/share/icons
    cp -r usr/share/icons/* $out/share/icons/

    # Locale / translations
    if [ -d usr/share/locale ]; then
      mkdir -p $out/share/locale
      cp -r usr/share/locale/* $out/share/locale/
    fi

    # Module configuration
    if [ -d etc ]; then
      mkdir -p $out/etc
      cp -r etc/* $out/etc/
    fi

    install -Dm755 usr/lib/bigcam/virtual-camera-helper $out/lib/bigcam/virtual-camera-helper
    install -Dm644 usr/share/polkit-1/actions/br.com.biglinux.bigcam.policy \
      $out/share/polkit-1/actions/br.com.biglinux.bigcam.policy
    substituteInPlace $out/share/biglinux/bigcam/core/virtual_camera.py \
      $out/share/polkit-1/actions/br.com.biglinux.bigcam.policy \
      --replace-fail /usr/lib/bigcam/virtual-camera-helper $out/lib/bigcam/virtual-camera-helper
    substituteInPlace $out/lib/bigcam/virtual-camera-helper \
      --replace-fail '#!/usr/bin/python3 -I' '#!${python3}/bin/python3 -I' \
      --replace-fail 'SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"' \
        'SAFE_PATH = "${lib.makeBinPath [ kmod linuxPackages.v4l2loopback.bin ]}"'

    # Launcher script
    mkdir -p $out/bin
    cat > $out/bin/bigcam <<'LAUNCHER'
    #!/bin/bash
    exec python3 @out@/share/biglinux/bigcam/main.py "$@"
    LAUNCHER
    chmod +x $out/bin/bigcam
    substituteInPlace $out/bin/bigcam --replace-fail "@out@" "$out"

    # Fix desktop file paths
    substituteInPlace $out/share/applications/*.desktop \
      --replace-quiet "/usr/share/biglinux/bigcam" "$out/share/biglinux/bigcam" \
      --replace-quiet "/usr/bin/bigcam" "$out/bin/bigcam"

    runHook postInstall
  '';

  postFixup = ''
    wrapProgram $out/bin/bigcam \
      "''${gappsWrapperArgs[@]}" \
      --prefix PATH : "${lib.makeBinPath [ pythonEnv ffmpeg v4l-utils gphoto2 pipewire polkit openssl pulseaudio linuxPackages.v4l2loopback.bin ]}" \
      --prefix PYTHONPATH : "$out/share/biglinux/bigcam" \
      --prefix PYTHONPATH : "${pythonEnv}/${pythonEnv.sitePackages}" \
      --prefix GI_TYPELIB_PATH : "${lib.makeSearchPath "lib/girepository-1.0" buildInputs}" \
      --prefix GST_PLUGIN_PATH : "${lib.makeSearchPath "lib/gstreamer-1.0" [
        gst_all_1.gst-plugins-base
        gst_all_1.gst-plugins-good
        gst_all_1.gst-plugins-bad
        gst_all_1.gst-plugins-ugly
        gst_all_1.gst-plugin-gtk4
      ]}"
  '';

  meta = with lib; {
    description = "Universal webcam control center for Linux";
    homepage = "https://github.com/biglinux/bigcam";
    license = licenses.gpl3;
    platforms = platforms.linux;
    mainProgram = "bigcam";
  };
}
