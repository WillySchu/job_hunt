#!/usr/bin/env bash
# Install jobfeed as a systemd service that runs every morning at 6:00 Pacific.
#
#   deploy/install.sh              install, or update after a git pull; enables the timer
#   deploy/install.sh --print      show the unit files it would install, and change nothing
#   deploy/install.sh --uninstall  stop and remove the timer and service
#                                  (the repo, .venv, .env and jobs.db stay)
#
# Run it from your normal account, not with sudo: the service runs as whoever
# runs this script, and the script asks for sudo only for the systemd steps.
set -euo pipefail

# Overridable for testing; leave these alone on the Pi.
UNIT_DIR=${JOBFEED_UNIT_DIR:-/etc/systemd/system}
SUDO=${JOBFEED_SUDO-sudo}
SYSTEMCTL=${JOBFEED_SYSTEMCTL:-systemctl}
PYTHON=${PYTHON:-python3}

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

mode=install
case "${1:-}" in
  "") ;;
  --print) mode=print ;;
  --uninstall) mode=uninstall ;;
  -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) die "unknown option '$1' (try --help)" ;;
esac

cd "$(dirname "$0")/.."
DIR=$(pwd -P)
[[ -f pyproject.toml && -d jobfeed && -f deploy/jobfeed.service ]] || die "run this from the job_hunt repo"

if [[ $EUID -eq 0 && -z ${JOBFEED_ALLOW_ROOT:-} ]]; then
  die "run this as your normal user, not root or sudo; the service runs as that user"
fi
RUN_USER=$(id -un)
RUN_GROUP=$(id -gn)

# Unit files treat spaces, % and a few other characters specially.
if [[ $DIR =~ [[:space:]%\|\&\\\"\'] ]]; then
  die "the repo path '$DIR' has a space or special character systemd can't take; move the repo"
fi

render() {
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@GROUP@|$RUN_GROUP|g" -e "s|@DIR@|$DIR|g" "deploy/$1"
}

if [[ $mode == print ]]; then
  for unit in jobfeed.service jobfeed.timer; do
    printf '### %s/%s\n' "$UNIT_DIR" "$unit"
    render "$unit"
    printf '\n'
  done
  exit 0
fi

if [[ $mode == uninstall ]]; then
  step "Removing the jobfeed timer and service"
  $SUDO "$SYSTEMCTL" disable --now jobfeed.timer 2>/dev/null || true
  $SUDO "$SYSTEMCTL" stop jobfeed.service 2>/dev/null || true
  $SUDO rm -f "$UNIT_DIR/jobfeed.service" "$UNIT_DIR/jobfeed.timer"
  $SUDO "$SYSTEMCTL" daemon-reload
  $SUDO "$SYSTEMCTL" reset-failed jobfeed.service 2>/dev/null || true
  say "Done. $DIR, its .venv, .env and jobs.db are untouched."
  exit 0
fi

step "Checking Python"
if ! command -v "$PYTHON" >/dev/null; then
  die "$PYTHON not found; install it with: sudo apt install python3 python3-venv"
fi
version=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  die "jobfeed needs Python 3.11 or newer, and $PYTHON is $version. Raspberry Pi OS Bookworm ships 3.11;
on an older release, upgrade the OS or point PYTHON= at a newer interpreter."
fi
say "$PYTHON is $version"

step "Installing jobfeed into $DIR/.venv"
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON" -m venv .venv || die "couldn't create .venv; on Raspberry Pi OS: sudo apt install python3-venv"
fi
.venv/bin/python -m pip install --quiet --disable-pip-version-check -e . \
  || die "pip install failed; see the messages above"
.venv/bin/jobfeed --version

step "Checking .env and config.yaml"
[[ -f .env ]] || die ".env is missing. Create it first:
  cp .env.example .env && chmod 600 .env
then fill in GMAIL_USER, GMAIL_APP_PASSWORD and DIGEST_TO, and check it with
  .venv/bin/jobfeed send-test"
if [[ $(stat -c '%a' .env) != 600 ]]; then
  chmod 600 .env
  say "made .env readable by $RUN_USER only (chmod 600)"
fi
.venv/bin/python - <<'PY' || die "fix the settings above, then run this again"
import sys
from jobfeed.config import ConfigError, load_config
from jobfeed.mailer import MailConfigError, load_settings

try:
    settings = load_settings()
    load_config()
except (MailConfigError, ConfigError) as exc:
    sys.exit(f"  {exc}")
print(f"email goes to {', '.join(settings.to)} from {settings.user}; config.yaml is valid")
PY

step "Installing the systemd units into $UNIT_DIR"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
render jobfeed.service >"$tmp/jobfeed.service"
render jobfeed.timer >"$tmp/jobfeed.timer"
if command -v systemd-analyze >/dev/null; then
  systemd-analyze verify "$tmp/jobfeed.service" "$tmp/jobfeed.timer" \
    || die "systemd rejected the unit files; nothing was installed"
fi
$SUDO install -m 644 "$tmp/jobfeed.service" "$tmp/jobfeed.timer" "$UNIT_DIR/"
$SUDO "$SYSTEMCTL" daemon-reload
$SUDO "$SYSTEMCTL" enable --now jobfeed.timer
say "service runs as $RUN_USER in $DIR"

step "Next run"
"$SYSTEMCTL" list-timers jobfeed.timer --no-pager || true

cat <<EOF

jobfeed now runs every morning at 6:00 Pacific. If the Pi is off then, it runs
when it next starts up.

  Run it now:        sudo systemctl start jobfeed
  See the last run:  journalctl -u jobfeed -n 50 --no-pager
  Is it scheduled:   systemctl list-timers jobfeed.timer
  Remove it:         deploy/install.sh --uninstall

After a git pull, run deploy/install.sh again to pick up the changes.
EOF
