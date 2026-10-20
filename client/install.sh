#!/bin/sh
# SideCode device installer.
#
# The gateway serves this file at GET /install.sh with the placeholder on the GATEWAY line
# replaced by its PUBLIC_ORIGIN, so the usual invocations are:
#
#   curl -fsSL https://rc.example.com/install.sh | sh
#   curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-7K42-QX9M
#
# It installs uv and a private Python 3.12 virtual environment under
# ~/.rc-client, installs the rc_client wheel from the gateway, enrolls the
# device and registers the background service. Without --pair the host prints a
# QR code and waits for an app to scan it. Re-running upgrades in place.
#
# Every step is one line: a spinner while it runs, a green tick and the result
# when it is done, a red cross and the step's captured output when it fails.
# Colour, the Unicode marks and the spinner are for a real terminal only; piped
# or dumb output is the same lines in plain ASCII.
set -eu

GATEWAY="__GATEWAY_ORIGIN__"
PAIR=""
NAME=""
PROXY=""
MANUAL=0
UNINSTALL=0
SHELL_RC=1
CODEX=1
GROK=1

RC_HOME="${RC_CLIENT_HOME:-$HOME/.rc-client}"
VENV="$RC_HOME/venv"
UV_BIN=""
DOWNLOAD=""
LOG_FILE=""
SPINNER_PID=""
STEP_LABEL=""

log() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- terminal ---------------------------------------------------------------
# Escape codes go to a terminal and nowhere else: `| cat`, a log file, TERM=dumb
# and NO_COLOR all get the same lines with ASCII marks and no spinner. Unicode
# follows the locale, the way pi's own installer decides it.
ANSI=0
UNICODE=0
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    ANSI=1
    case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
        *UTF-8*|*utf-8*|*UTF8*|*utf8*) UNICODE=1 ;;
        *)
            case "${TERM_PROGRAM:-}" in
                Apple_Terminal|iTerm.app|vscode|WezTerm) UNICODE=1 ;;
            esac
            ;;
    esac
fi

C_RESET=""
C_BOLD=""
C_DIM=""
C_GREEN=""
C_RED=""
C_CYAN=""
if [ "$ANSI" -eq 1 ]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_CYAN="$(printf '\033[36m')"
fi

if [ "$UNICODE" -eq 1 ]; then
    MARK_OK="✓"
    MARK_BAD="✗"
    MARK_NOTE="–"
    ELLIPSIS="…"
else
    MARK_OK="ok"
    MARK_BAD="error"
    MARK_NOTE="-"
    ELLIPSIS="..."
fi

# The first argument is the indent, so the summary can nest its lines under a
# heading while the steps stay at the left edge.
mark_ok() { printf '%s%s%s%s %s\n' "$1" "$C_GREEN" "$MARK_OK" "$C_RESET" "$2"; }
mark_bad() { printf '%s%s%s %s%s\n' "$1" "$C_RED" "$MARK_BAD" "$2" "$C_RESET"; }
mark_note() { printf '%s%s%s %s%s\n' "$1" "$C_DIM" "$MARK_NOTE" "$2" "$C_RESET"; }

spinner_frame() {
    if [ "$UNICODE" -eq 1 ]; then
        case $(($1 % 10)) in
            0) printf '⠋' ;;
            1) printf '⠙' ;;
            2) printf '⠹' ;;
            3) printf '⠸' ;;
            4) printf '⠼' ;;
            5) printf '⠴' ;;
            6) printf '⠦' ;;
            7) printf '⠧' ;;
            8) printf '⠇' ;;
            *) printf '⠏' ;;
        esac
    else
        case $(($1 % 4)) in
            0) printf '-' ;;
            # \134 is a backslash; a bare one in quotes reads as an escape to linters.
            1) printf '\134' ;;
            2) printf '|' ;;
            *) printf '/' ;;
        esac
    fi
}

spin_loop() {
    spin_step=0
    while :; do
        printf '\r\033[K  %s%s%s %s%s' \
            "$C_CYAN" "$(spinner_frame "$spin_step")" "$C_RESET" "$1" "$ELLIPSIS"
        spin_step=$((spin_step + 1))
        sleep 0.1
    done
}

spin_stop() {
    if [ -n "$SPINNER_PID" ]; then
        kill "$SPINNER_PID" 2>/dev/null || true
        wait "$SPINNER_PID" 2>/dev/null || true
        SPINNER_PID=""
        printf '\r\033[K'
    fi
}

cleanup() {
    spin_stop
    if [ "$ANSI" -eq 1 ]; then
        printf '\033[?25h'
    fi
    if [ -n "$DOWNLOAD" ] && [ -d "$DOWNLOAD" ]; then
        rm -rf "$DOWNLOAD" 2>/dev/null || true
    fi
    if [ -n "$LOG_FILE" ] && [ -f "$LOG_FILE" ]; then
        rm -f "$LOG_FILE" 2>/dev/null || true
    fi
    return 0
}

# A step names what it is doing now; it ends as a result in the past tense.
step_begin() {
    STEP_LABEL="$1"
    : >"$LOG_FILE"
    if [ "$ANSI" -eq 1 ]; then
        spin_loop "$STEP_LABEL" &
        SPINNER_PID=$!
    fi
}

step_ok() {
    spin_stop
    mark_ok '  ' "$1"
}

step_note() {
    spin_stop
    mark_note '  ' "$1"
}

# Only the failing step's own output is worth showing, so it is captured and
# printed here and nowhere else.
step_fail() {
    spin_stop
    mark_bad '  ' "${1:-$STEP_LABEL}"
    if [ -n "$LOG_FILE" ] && [ -s "$LOG_FILE" ]; then
        sed 's/^/    /' "$LOG_FILE"
    fi
    exit 1
}

# Runs the step's command with its output captured for step_fail.
run_step() {
    "$@" >"$LOG_FILE" 2>&1
}

short_path() {
    case "$1" in
        "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;;
        *) printf '%s' "$1" ;;
    esac
}

usage() {
    cat <<'USAGE'
Usage: install.sh [--pair RC-XXXX-XXXX] [options]

Without --pair this host prints a QR code and waits for you to scan it with the
SideCode app, or to open its link in a signed-in browser.

  --pair CODE        pairing code minted in an app; omit it to pair by scanning
  --name NAME        device name shown in the apps (default: this hostname)
  --proxy env|URL    reach the gateway through an http or https proxy: "env" takes the
                     one this host's proxy settings name, a URL names one and is used
                     by this script too (default: dial directly)
  --gateway ORIGIN   override the gateway origin baked into this script
  --no-shell-rc      do not add the shim directory to your shell startup file
  --no-codex         skip the shared Codex app-server daemon setup
  --no-grok          leave Grok's leader flag alone in ~/.grok/config.toml
  --manual           print the steps instead of running them
  --uninstall        stop and remove the service, keep ~/.rc-client
  -h, --help         show this message
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --pair) PAIR="${2:-}"; shift 2 ;;
        --pair=*) PAIR="${1#*=}"; shift ;;
        --name) NAME="${2:-}"; shift 2 ;;
        --name=*) NAME="${1#*=}"; shift ;;
        --proxy) PROXY="${2:-}"; shift 2 ;;
        --proxy=*) PROXY="${1#*=}"; shift ;;
        --gateway) GATEWAY="${2:-}"; shift 2 ;;
        --gateway=*) GATEWAY="${1#*=}"; shift ;;
        --no-shell-rc) SHELL_RC=0; shift ;;
        --no-codex) CODEX=0; shift ;;
        --no-grok) GROK=0; shift ;;
        --manual) MANUAL=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $1" ;;
    esac
done

# A --proxy URL is what this host has to use for everything, not just what
# rc-client dials: curl, uv and the wheel download all read the environment and
# would otherwise time out before enrollment is ever reached. "env" needs
# nothing, since it means the variables are here already.
case "$PROXY" in
    ""|[Ee][Nn][Vv]) ;;
    *) export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" ;;
esac

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Darwin)
        PLATFORM="macos"
        [ "$(id -u)" -eq 0 ] && fail "do not run this installer as root on macOS; run it as the logged-in user"
        ;;
    Linux) PLATFORM="linux" ;;
    *) fail "unsupported operating system: $OS" ;;
esac
case "$ARCH" in
    arm64|aarch64|x86_64|amd64) : ;;
    *) fail "unsupported architecture: $ARCH" ;;
esac

if [ "$UNINSTALL" -eq 1 ]; then
    if [ -x "$VENV/bin/rc-client" ]; then
        [ "$SHELL_RC" -eq 1 ] || set -- --no-shell-rc
        "$VENV/bin/rc-client" uninstall "$@" || true
    fi
    log "SideCode service removed. Delete $RC_HOME to remove the data as well."
    exit 0
fi

# Only the assignment above may carry the placeholder: the gateway replaces every occurrence, so
# a literal here would turn into the configured origin and reject it. An unsubstituted script has
# no scheme and falls through to the same error.
case "$GATEWAY" in
    http://*|https://*) : ;;
    *) fail "no gateway origin; pass --gateway https://your-gateway" ;;
esac
GATEWAY="${GATEWAY%/}"

# The wheel is downloaded and then executed as a service, so plain HTTP is only
# acceptable where nobody can sit on the path. This mirrors the daemon's own
# rule in rc_client/config.py, but has to hold *before* the first download.
case "$GATEWAY" in
    http://*)
        GATEWAY_HOST="${GATEWAY#http://}"
        GATEWAY_HOST="${GATEWAY_HOST%%/*}"
        GATEWAY_HOST="${GATEWAY_HOST%%:*}"
        GATEWAY_HOST="${GATEWAY_HOST#[}"
        GATEWAY_HOST="${GATEWAY_HOST%]}"
        # A name like 192.168.evil.com resolves wherever its owner points it, so
        # only literal loopback and RFC1918 addresses count as local.
        if [ "$GATEWAY_HOST" != "localhost" ] && [ "$GATEWAY_HOST" != "::1" ]; then
            case "$GATEWAY_HOST" in
                *[!0-9.]*) fail "refusing plain http:// to $GATEWAY_HOST; use https://" ;;
            esac
            case "$GATEWAY_HOST" in
                127.*|10.*|192.168.*|169.254.*|0.0.0.0) : ;;
                172.1[6-9].*|172.2[0-9].*|172.3[01].*) : ;;
                *) fail "refusing plain http:// to $GATEWAY_HOST; use https:// for a public gateway" ;;
            esac
        fi
        ;;
esac

if [ "$MANUAL" -eq 1 ]; then
    cat <<MANUAL
Manual installation on $PLATFORM/$ARCH:

  1. curl -LsSf https://astral.sh/uv/install.sh | sh
  2. uv python install 3.12
  3. uv venv --python 3.12 "$VENV"
  4. curl -fsSL -O -J "$GATEWAY/dist/rc_client-latest.whl"   # saves the real wheel name
  5. uv pip install --python "$VENV/bin/python" --upgrade ./rc_client-*.whl
  6. mkdir -p "$RC_HOME/state" && shasum -a 256 ./rc_client-*.whl | cut -d' ' -f1 \
       > "$RC_HOME/state/client-build"
  7. "$VENV/bin/rc-client" enroll --gateway "$GATEWAY" --pair <your pairing code>
     or "$VENV/bin/rc-client" enroll --gateway "$GATEWAY" --scan   # prints a QR code
     add --proxy env (or --proxy http://proxy.example:3128) on a host that only
     reaches the gateway through a proxy; export HTTPS_PROXY yourself for the
     curl and uv steps above, which read it on their own
  8. "$VENV/bin/rc-client" service install && "$VENV/bin/rc-client" service start
  9. "$VENV/bin/rc-client" shim install   # lets the apps drive terminal Claude sessions
 10. "$VENV/bin/rc-client" codex setup    # lets the apps drive terminal Codex sessions
 11. "$VENV/bin/rc-client" pi setup       # lets the apps drive terminal pi sessions
 12. "$VENV/bin/rc-client" grok setup     # lets the apps drive terminal Grok sessions

The pairing code is single use and expires after 10 minutes. Step 6 is what lets
an app update this device later; skip it and the apps report no build for it.
MANUAL
    exit 0
fi

mkdir -p "$RC_HOME"
chmod 700 "$RC_HOME"

LOG_FILE="$(mktemp)"
trap 'cleanup' EXIT
trap 'cleanup; exit 130' INT TERM
if [ "$ANSI" -eq 1 ]; then
    printf '\033[?25l'
fi

printf '\n  %sSideCode installer%s\n' "$C_BOLD" "$C_RESET"
printf '  %sYour terminal agents, from your phone and browser.%s\n\n' "$C_DIM" "$C_RESET"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
    elif [ -x "$HOME/.local/bin/uv" ]; then
        printf '%s\n' "$HOME/.local/bin/uv"
    fi
}

UV_BIN="$(find_uv || true)"
if [ -n "$UV_BIN" ]; then
    step_ok "Using uv at $UV_BIN"
else
    step_begin "Installing uv"
    if run_step sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'; then
        UV_BIN="$(find_uv || true)"
        if [ -z "$UV_BIN" ]; then
            printf 'uv was installed but is not on PATH; open a new shell and re-run\n' >"$LOG_FILE"
            step_fail
        fi
        step_ok "Installed uv into $(short_path "$HOME/.local/bin")"
    else
        step_fail "Installing uv (install it manually and re-run)"
    fi
fi

step_begin "Preparing Python 3.12"
if run_step "$UV_BIN" python install 3.12; then
    step_ok "Python 3.12 ready"
else
    step_fail
fi

if [ -x "$VENV/bin/python" ]; then
    step_ok "Virtual environment at $(short_path "$VENV") (reused)"
else
    step_begin "Creating the virtual environment"
    if run_step "$UV_BIN" venv --python 3.12 "$VENV"; then
        step_ok "Virtual environment at $(short_path "$VENV") (created)"
    else
        step_fail
    fi
fi

# `pip install <url>` reads the wheel's Python, ABI and platform tags out of the filename, and the
# stable alias carries none of them, so save the download under the name the gateway reports and
# install that file instead of the alias URL.
step_begin "Downloading rc-client from $GATEWAY"
DOWNLOAD="$(mktemp -d)"
if ! ( cd "$DOWNLOAD" && curl -fsSL -O -J "$GATEWAY/dist/rc_client-latest.whl" ) \
        >"$LOG_FILE" 2>&1; then
    step_fail "Downloading $GATEWAY/dist/rc_client-latest.whl"
fi
WHEEL=""
for candidate in "$DOWNLOAD"/*.whl; do
    if [ -f "$candidate" ]; then
        WHEEL="$candidate"
    fi
done
if [ -z "$WHEEL" ]; then
    printf 'the gateway did not name the wheel it served; upgrade the gateway\n' >"$LOG_FILE"
    step_fail
fi
step_ok "Downloaded rc-client $(basename "$WHEEL") from $GATEWAY"

step_begin "Installing rc-client"
if run_step "$UV_BIN" pip install --python "$VENV/bin/python" --upgrade --quiet "$WHEEL"; then
    step_ok "Installed rc-client"
else
    step_fail
fi

RC="$VENV/bin/rc-client"
if [ ! -x "$RC" ]; then
    printf 'rc-client was not installed into %s\n' "$VENV" >"$LOG_FILE"
    STEP_LABEL="Installing rc-client"
    step_fail
fi

# Record which wheel this is, so an app can see whether the device runs the one
# the gateway serves and can ask it to install that one instead.
BUILD="$(sha256_of "$WHEEL" || true)"
if [ -n "$BUILD" ]; then
    mkdir -p "$RC_HOME/state"
    chmod 700 "$RC_HOME/state"
    printf '%s\n' "$BUILD" > "$RC_HOME/state/client-build"
    chmod 600 "$RC_HOME/state/client-build"
    step_ok "Recorded build $(printf '%s' "$BUILD" | cut -c1-12)"
else
    step_note "No sha256 tool on this host; the apps cannot update this device"
fi

# Enrolment owns the terminal: --scan draws a QR code and waits for it to be
# scanned, --pair prints where the device landed. Its output passes through.
printf '  Enrolling this device\n'
if [ -n "$PAIR" ]; then
    set -- enroll --gateway "$GATEWAY" --pair "$PAIR"
    ENROLL_HINT="mint a fresh pairing code and try again"
else
    set -- enroll --gateway "$GATEWAY" --scan
    ENROLL_HINT="run this installer again for a fresh code"
fi
[ -n "$NAME" ] && set -- "$@" --name "$NAME"
[ -n "$PROXY" ] && set -- "$@" --proxy "$PROXY"
if "$RC" "$@"; then
    step_ok "Enrolled"
else
    printf 'enrollment failed; %s\n' "$ENROLL_HINT" >"$LOG_FILE"
    STEP_LABEL="Enrolling this device"
    step_fail
fi

step_begin "Registering the background service"
if run_step "$RC" service install && run_step "$RC" service start; then
    step_ok "Background service registered and started"
else
    step_fail
fi

# The shim lets the apps attach to a Claude session started in a terminal. It is
# inert for every non-interactive invocation, so installing it is safe even for
# someone who never uses that feature.
step_begin "Installing the claude shim"
if [ "$SHELL_RC" -eq 1 ]; then
    run_step "$RC" shim install && SHIM=0 || SHIM=1
else
    run_step "$RC" shim install --no-shell-rc && SHIM=0 || SHIM=1
fi
if [ "$SHIM" -eq 0 ]; then
    step_ok "claude shim installed"
else
    step_note "Could not install the claude shim; run 'rc-client shim status'"
fi

# The shared Codex app-server daemon is what lets the apps see and drive a Codex
# session started in a terminal. Only the standalone build can bootstrap it, so
# `codex setup` installs that build when it is missing, whatever other `codex`
# happens to be on PATH. It is idempotent and never enables OpenAI remote
# control. It never removes another Codex install either: an npm or Homebrew
# TUI joins the shared daemon just as well, it simply cannot start it.
if [ "$CODEX" -eq 1 ]; then
    step_begin "Setting up the shared Codex daemon"
    if run_step "$RC" codex setup; then
        step_ok "Shared Codex daemon ready"
    else
        step_note "The shared Codex daemon is not ready; run 'rc-client codex status'"
    fi
else
    step_note "Codex daemon setup skipped"
fi

# The pi extension is what lets the apps see and drive a pi session started in a
# terminal. `pi setup` prints one line about what it found and is never fatal:
# a device without pi installed is a normal outcome.
step_begin "Installing the pi extension"
if run_step "$RC" pi setup; then
    PI_LINE="$(sed -n '1p' "$LOG_FILE")"
    case "$PI_LINE" in
        "") step_ok "pi extension installed" ;;
        "pi is not installed"*) step_note "$PI_LINE" ;;
        *) step_ok "$PI_LINE" ;;
    esac
else
    step_note "Could not install the pi extension; run 'rc-client pi setup'"
fi

# Grok Build attaches through its leader: one backend per machine that every
# `grok` joins once `[cli] use_leader` is on. `grok setup` sets that flag in the
# person's own config, in place, and is never fatal: a device without Grok
# installed is a normal outcome, and so is a sandbox profile that refuses it.
if [ "$GROK" -eq 1 ]; then
    step_begin "Enabling the Grok leader"
    if run_step "$RC" grok setup; then
        step_ok "Grok leader enabled"
    else
        GROK_LINE="$(grep -m1 'not installed\|sandbox' "$LOG_FILE" || true)"
        case "$GROK_LINE" in
            "") step_note "Grok is not ready to attach; run 'rc-client grok status'" ;;
            *) step_note "$GROK_LINE" ;;
        esac
    fi
else
    step_note "Grok leader setup skipped"
fi

# --- summary ----------------------------------------------------------------
# `rc-client agents` prints one JSON object per agent, pretty-printed one key
# per line, so the top-level keys are the ones indented by exactly four spaces.
agent_name() {
    case "$1" in
        claude) printf 'Claude Code' ;;
        codex) printf 'Codex' ;;
        grok) printf 'Grok Build' ;;
        pi) printf 'pi' ;;
        *) printf '%s' "$1" ;;
    esac
}

tip() { printf '    %-24s%s%s%s\n' "$1" "$C_DIM" "$2" "$C_RESET"; }

TAB="$(printf '\t')"
AGENT_LINES="$("$RC" agents 2>/dev/null | awk '
/^    "agent": / {
    id = $0
    sub(/^ *"agent": "/, "", id)
    sub(/",?$/, "", id)
    avail = 0
    next
}
/^    "available": / { if ($0 ~ /true/) { avail = 1 } ; next }
/^    "version": / {
    if (id == "") { next }
    ver = $0
    sub(/^ *"version": /, "", ver)
    sub(/,$/, "", ver)
    if (ver == "null") { ver = "" } else { sub(/^"/, "", ver); sub(/"$/, "", ver) }
    printf "%d\t%s\t%s\n", avail, id, ver
    id = ""
}
' || true)"

printf '\n  %sDetected agents%s\n' "$C_BOLD" "$C_RESET"
if [ -n "$AGENT_LINES" ]; then
    printf '%s\n' "$AGENT_LINES" | while IFS="$TAB" read -r a_avail a_id a_ver; do
        [ -n "$a_id" ] || continue
        a_name="$(agent_name "$a_id")"
        if [ "$a_avail" = "1" ]; then
            if [ -n "$a_ver" ]; then
                mark_ok '    ' "$a_name $a_ver"
            else
                mark_ok '    ' "$a_name"
            fi
        else
            mark_note '    ' "$a_name (not installed)"
        fi
    done
else
    mark_note '    ' "Could not read the agent list; run 'rc-client agents'"
fi

printf '\n  %sSideCode is running.%s\n' "$C_BOLD" "$C_RESET"
tip "rc-client status" "device and service status"
tip "rc-client service stop" "stop the daemon"
tip "rc-client uninstall" "remove the service (--purge also deletes $RC_HOME)"
tip "rc-client shim status" "the claude shim behind terminal Claude sessions"
tip "rc-client codex status" "the shared daemon behind terminal Codex sessions"
tip "rc-client grok status" "the leader behind terminal Grok sessions"
log ""
log "  Start Codex as a bare \`codex\` with no -c, --enable or --disable flags:"
log "  those launch a private app-server the apps cannot see."
log ""
log "  Restart any running grok so it joins the leader the apps attach to."
log ""
log "  Add $VENV/bin to your PATH to call rc-client directly."
log ""
