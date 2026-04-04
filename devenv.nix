{ pkgs, lib, config, inputs, ... }:

{
  env.GREET = "devenv";

  packages = with pkgs; [
    git

    # Python (manual, stable)
    python312
    python312Packages.pip
    python312Packages.virtualenv

    # Video + OpenCV deps
    ffmpeg
    libglvnd
    glib
    zlib
    stdenv.cc.cc.lib

    # 👇 The stuff your error was begging for
    xorg.libxcb
    xorg.libX11
    xorg.libXext
    xorg.libSM
    xorg.libICE

    # MQTT broker
    mosquitto

    # Build tools
    gcc
    pkg-config
  ];

  env.LD_LIBRARY_PATH = lib.makeLibraryPath [
    pkgs.ffmpeg
    pkgs.libglvnd
    pkgs.glib
    pkgs.zlib
    pkgs.stdenv.cc.cc.lib

    # 👇 Also add them here (yes, both places matter in Nix land)
    pkgs.xorg.libxcb
    pkgs.xorg.libX11
    pkgs.xorg.libXext
    pkgs.xorg.libSM
    pkgs.xorg.libICE
  ];

  scripts.setup.exec = ''
    echo "Setting up Python environment..."

    if [ ! -d ".venv" ]; then
      python3 -m venv .venv
    fi

    source .venv/bin/activate

    pip install --upgrade pip

    pip install \
      flask \
      "paho-mqtt>=2.0" \
      ultralytics \
      opencv-python-headless \
      numpy

    echo "Setup complete. Run 'run' to start the node."
  '';

  scripts.run.exec = ''
    # Fix libstdc++ (because Nix enjoys making this your problem)
    cxx_lib_file="$(${pkgs.findutils}/bin/find /nix/store -path '*/lib/libstdc++.so.6' -print -quit 2>/dev/null)"
    if [ -n "$cxx_lib_file" ]; then
      cxx_lib_dir="$(${pkgs.coreutils}/bin/dirname "$cxx_lib_file")"
      case ":''${LD_LIBRARY_PATH:-}:" in
        *":$cxx_lib_dir:"*) ;;
        *) export LD_LIBRARY_PATH="$cxx_lib_dir''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
      esac
    fi

    if [ -f ".env" ]; then
      set -a
      source .env
      set +a
      echo "[devenv] Loaded .env"
    fi

    python node.py
  '';

  scripts.mtx.exec = ''
    echo "Starting MediaMTX server..."
    mediamtx mediamtx.yml
  '';

  enterShell = ''
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  Swarm Surveillance Edge Node — devenv   ║"
    echo "╚══════════════════════════════════════════╝"

    if [ -d ".venv" ]; then
      source .venv/bin/activate
      echo "  ✓  Python venv activated"
    else
      echo "  ⚠  No .venv found — run: setup"
    fi

    python --version 2>/dev/null || true
    echo ""
    echo "  Commands:"
    echo "    setup  — install Python dependencies"
    echo "    run    — start this node (reads .env if present)"
    echo "    mtx    — start the MediaMTX video server"
    echo ""
  '';
}