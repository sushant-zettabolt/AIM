#!/bin/bash
# Installs Docker Engine (rootless mode — no sudo used or required anywhere
# in this script) from the official static binary tarballs, entirely under
# $HOME. Companion to run.sh, which builds/runs the actual llama.cpp+ZenDNN
# image once this is done.
#
# Two things this CANNOT fix without root, checked up front rather than
# failing halfway through a download:
#   1. newuidmap/newgidmap (the `uidmap` package) must already be installed.
#   2. Your user needs an /etc/subuid and /etc/subgid range assigned.
# Many distros set both up automatically for every user at account creation
# — if that already happened here, this script proceeds with zero root
# involvement. If not, it prints the exact one-time commands to hand to
# whoever has root, and stops rather than guessing.
set -euo pipefail

DOCKER_VERSION="${DOCKER_VERSION:-29.7.2}"
INSTALL_DIR="$HOME/.local/bin"
ARCH="x86_64"

echo "==> Checking prerequisites (these need root/admin to fix, one-time, if missing)"
missing=0

if ! command -v newuidmap >/dev/null 2>&1 || ! command -v newgidmap >/dev/null 2>&1; then
  echo "MISSING: newuidmap/newgidmap not found on PATH (the 'uidmap' package)."
  echo "  Ask an admin to run ONE of:"
  echo "    sudo apt-get install -y uidmap          # Debian/Ubuntu"
  echo "    sudo dnf install -y shadow-utils         # Fedora/RHEL/CentOS"
  missing=1
fi

if ! grep -q "^$(whoami):" /etc/subuid 2>/dev/null; then
  echo "MISSING: no /etc/subuid entry for $(whoami)."
  echo "  Ask an admin to run:"
  echo "    sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $(whoami)"
  missing=1
elif ! grep -q "^$(whoami):" /etc/subgid 2>/dev/null; then
  echo "MISSING: no /etc/subgid entry for $(whoami) (subuid entry exists, subgid doesn't — unusual, but check both)."
  missing=1
fi

if [ "$missing" -eq 1 ]; then
  echo
  echo "Fix the above (needs root, one-time, on this machine only), then re-run this script."
  exit 1
fi
echo "prerequisites OK — proceeding with zero root involvement from here on"

if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
  echo "==> docker already installed and working ($(docker --version)) — skipping install, jumping to daemon check"
else
  echo "==> Downloading Docker ${DOCKER_VERSION} static binaries + rootless extras"
  mkdir -p "$INSTALL_DIR"
  TMP="$(mktemp -d)"
  curl -fL -o "$TMP/docker.tgz" \
    "https://download.docker.com/linux/static/stable/${ARCH}/docker-${DOCKER_VERSION}.tgz"
  curl -fL -o "$TMP/docker-rootless-extras.tgz" \
    "https://download.docker.com/linux/static/stable/${ARCH}/docker-rootless-extras-${DOCKER_VERSION}.tgz"
  tar xzf "$TMP/docker.tgz" -C "$TMP"
  tar xzf "$TMP/docker-rootless-extras.tgz" -C "$TMP"
  cp "$TMP"/docker/* "$INSTALL_DIR/"
  cp "$TMP"/docker-rootless-extras/* "$INSTALL_DIR/"
  rm -rf "$TMP"
  echo "installed to $INSTALL_DIR"
fi

export PATH="$INSTALL_DIR:$PATH"

echo "==> Persisting PATH + DOCKER_HOST in ~/.bashrc (only if not already there)"
grep -qF "$INSTALL_DIR" "$HOME/.bashrc" 2>/dev/null || \
  echo "export PATH=\"$INSTALL_DIR:\$PATH\"" >> "$HOME/.bashrc"
UID_NUM="$(id -u)"
DOCKER_SOCK="unix:///run/user/${UID_NUM}/docker.sock"
grep -qF "DOCKER_HOST" "$HOME/.bashrc" 2>/dev/null || \
  echo "export DOCKER_HOST=$DOCKER_SOCK" >> "$HOME/.bashrc"
export DOCKER_HOST="$DOCKER_SOCK"

if ! docker version >/dev/null 2>&1; then
  echo "==> Running dockerd-rootless-setuptool.sh install"
  "$INSTALL_DIR/dockerd-rootless-setuptool.sh" install --skip-iptables

  echo "==> Starting the rootless daemon"
  if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    systemctl --user enable --now docker
    echo "started via systemd --user. Note: without 'loginctl enable-linger' (needs root)"
    echo "this won't survive a full logout — ask an admin for that if you need persistence"
    echo "across sessions; it'll work fine for the rest of this login either way."
  else
    echo "no usable systemd --user session — starting dockerd-rootless.sh directly with nohup"
    nohup "$INSTALL_DIR/dockerd-rootless.sh" > "$HOME/dockerd-rootless.log" 2>&1 &
    disown
    sleep 3
  fi
fi

echo "==> Verifying"
docker version
docker run --rm hello-world

echo
echo "==> Done. Open a NEW shell (or 'source ~/.bashrc') so PATH/DOCKER_HOST are picked up,"
echo "    then run: ./deploy/epyc-standalone/run.sh"
