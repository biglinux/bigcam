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
    ]
  );
in
stdenv.mkDerivation {
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

    # System config (polkit, modprobe, sudoers)
    if [ -d etc ]; then
      mkdir -p $out/etc
      cp -r etc/* $out/etc/
    fi

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
      --prefix PATH : "${lib.makeBinPath [ pythonEnv ffmpeg v4l-utils gphoto2 ]}" \
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
