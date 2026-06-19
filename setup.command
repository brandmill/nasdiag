#!/bin/bash
# nasdiag — Mac setup script.
# Double-click this file in Finder, or run: bash setup.command
# Everything it does is logged to ~/nasdiag-setup.log so if something breaks
# you can just AirDrop / email that log file.

LOG="$HOME/nasdiag-setup.log"
REPO="https://github.com/brandmill/nasdiag"
INSTALL_DIR="$HOME/nasdiag"

# Send all output to both the terminal AND the log file (timestamped).
exec > >(while IFS= read -r line; do printf '%s %s\n' "$(date '+%H:%M:%S')" "$line"; done | tee -a "$LOG") 2>&1

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; }

bail() {
  err "$1"
  cat <<EOF

────────────────────────────────────────────────────────────────────
SETUP FAILED.
Send this file to Brandy:  $LOG
Open Finder → press ⌘⇧G → paste: $LOG
You can AirDrop / email / drop it in a NAS share.
────────────────────────────────────────────────────────────────────
EOF
  exit 1
}

# Reset log
: > "$LOG"
say "nasdiag setup — log: $LOG"
say "host: $(hostname)   user: $USER   $(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null)"

# ── 1. Xcode CLT (required for git) ─────────────────────────────────
say "[1/6] Xcode Command Line Tools"
if xcode-select -p &>/dev/null; then
  ok "already installed: $(xcode-select -p)"
else
  warn "missing — triggering installer (a popup will appear; click Install, ~5 min)"
  xcode-select --install 2>&1 || true
  echo "Waiting for you to finish the Xcode Command Line Tools install..."
  echo "When you see 'Software was installed', come back here and press ENTER."
  read -r _
  if ! xcode-select -p &>/dev/null; then
    bail "Xcode Command Line Tools still not installed. Re-run this script after installing."
  fi
  ok "installed"
fi

# ── 2. Homebrew ────────────────────────────────────────────────────
say "[2/6] Homebrew"
if command -v brew &>/dev/null; then
  ok "already installed: $(brew --version | head -1)"
else
  warn "missing — installing (will prompt for password, ~5 min)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || bail "Homebrew install failed"
  # Add brew to PATH for this session (Apple Silicon path)
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  command -v brew &>/dev/null || bail "brew installed but not on PATH — restart Terminal and re-run this script"
  ok "installed: $(brew --version | head -1)"
fi

# ── 3. iperf3 + fio ────────────────────────────────────────────────
say "[3/6] iperf3 + fio"
need_brew=()
command -v iperf3 &>/dev/null || need_brew+=(iperf3)
command -v fio    &>/dev/null || need_brew+=(fio)
if [ ${#need_brew[@]} -eq 0 ]; then
  ok "already installed"
else
  warn "installing: ${need_brew[*]}"
  brew install "${need_brew[@]}" || bail "brew install failed — see log above"
  ok "installed"
fi

# ── 4. psutil (optional but used for client telemetry) ─────────────
say "[4/6] psutil (Python lib for client telemetry — optional)"
if python3 -c "import psutil" &>/dev/null; then
  ok "already installed"
else
  install_psutil() {
    # Try, in order: modern pip3, legacy pip3, modern python3 -m pip, legacy python3 -m pip.
    # The first one whose pip understands --break-system-packages wins on macOS 14+;
    # the legacy forms work on older Pythons whose pip predates that flag.
    pip3 install --break-system-packages --user psutil >>"$LOG" 2>&1 \
      || pip3 install --user psutil >>"$LOG" 2>&1 \
      || python3 -m pip install --break-system-packages --user psutil >>"$LOG" 2>&1 \
      || python3 -m pip install --user psutil >>"$LOG" 2>&1
  }
  if install_psutil && python3 -c "import psutil" &>/dev/null; then
    ok "installed"
  else
    warn "psutil install failed — continuing without it. Client CPU/NIC/thermal will be blank in reports."
  fi
fi

# ── 5. Clone or update nasdiag ─────────────────────────────────────
say "[5/6] nasdiag code → $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  cd "$INSTALL_DIR" || bail "couldn't cd to $INSTALL_DIR"
  git pull --ff-only || warn "git pull failed — using existing checkout"
  ok "updated"
elif [ -d "$INSTALL_DIR" ]; then
  warn "$INSTALL_DIR exists but isn't a git checkout — moving it to ${INSTALL_DIR}.old"
  mv "$INSTALL_DIR" "${INSTALL_DIR}.old.$(date +%s)" || bail "couldn't move existing dir"
  git clone "$REPO" "$INSTALL_DIR" || bail "git clone failed"
  ok "cloned"
else
  git clone "$REPO" "$INSTALL_DIR" || bail "git clone failed"
  ok "cloned"
fi

# ── 6. Verify the install ──────────────────────────────────────────
say "[6/6] Verify install"
cd "$INSTALL_DIR" || bail "couldn't cd to $INSTALL_DIR"
python3 -c "from nasdiag import cli, network, storage, concurrent, telemetry, discover, profile, report, gui" \
  || bail "Python import test failed — see error above"
ok "all modules import cleanly"

# ── Launch ─────────────────────────────────────────────────────────
cat <<EOF


════════════════════════════════════════════════════════════════════
✓ Setup complete.

Launching nasdiag web UI in your browser…
  URL:  http://127.0.0.1:8765/
  Stop: press Ctrl-C in this window when you're done

Re-run anytime: double-click setup.command again (it'll just update).
Log file:       $LOG
════════════════════════════════════════════════════════════════════

EOF

exec python3 -m nasdiag gui
