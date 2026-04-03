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

    # MQTT broker (used on the designated broker laptop)
    mosquitto

    # Build tools
    gcc
    pkg-config
  ];

  scripts.setup.exec = ''
    echo "Setting up Python environment..."

    # Create venv if not exists
    if [ ! -d ".venv" ]; then
      python3 -m venv .venv
    fi

    source .venv/bin/activate

    pip install --upgrade pip

    pip install \
      flask \
      "paho-mqtt>=2.0" \
      ultralytics \
      opencv-python \
      numpy

    echo "Setup complete. Run 'run' to start the node."
  '';

  # Convenience alias: run the node with env vars from .env if present
  scripts.run.exec = ''
    if [ -f ".env" ]; then
      set -a
      source .env
      set +a
      echo "[devenv] Loaded .env"
    fi
    python node.py
  '';

  enterShell = ''
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  Swarm Surveillance Edge Node — devenv  ║"
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
    echo ""
  '';
}