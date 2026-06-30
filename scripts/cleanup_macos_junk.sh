#!/usr/bin/env zsh
set -euo pipefail

DRY_RUN=1
USE_TRASH=1
EMPTY_TRASH=0
CLEAN_DEV=0
CLEAN_BROWSER=0
CLEAN_DOWNLOADS=0
DOWNLOADS_DAYS=30

SCRIPT_NAME="${0:t}"
TRASH_DIR="$HOME/.Trash"

usage() {
  cat <<'EOF'
Clean common macOS junk files safely.

Default mode is a dry run. Nothing is removed unless --execute is passed.
By default, files are moved to ~/.Trash. Use --delete to remove immediately.

Usage:
  ./scripts/cleanup_macos_junk.sh [options]

Options:
  --execute              Actually clean files. Without this, only prints actions.
  --delete               Delete immediately instead of moving to Trash.
  --empty-trash          Empty ~/.Trash after cleaning. Requires --execute.
  --dev                  Include developer caches: Xcode, npm, yarn, pnpm, pip, uv, Gradle.
  --browser              Include browser caches for Safari, Chrome, Edge, Firefox.
  --downloads-old-days N Move files in ~/Downloads older than N days. Default: 30.
  -h, --help             Show this help.

Examples:
  ./scripts/cleanup_macos_junk.sh
  ./scripts/cleanup_macos_junk.sh --execute --dev
  ./scripts/cleanup_macos_junk.sh --execute --browser --downloads-old-days 60
  ./scripts/cleanup_macos_junk.sh --execute --delete --dev --browser
EOF
}

log() {
  printf '%s\n' "$*"
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

is_safe_target() {
  local target="$1"
  [[ -n "$target" ]] || return 1
  [[ "$target" != "/" ]] || return 1
  [[ "$target" != "$HOME" ]] || return 1
  [[ "$target" == "$HOME/"* || "$target" == /private/var/folders/* || "$target" == /var/folders/* ]] || return 1
  return 0
}

human_size() {
  local target="$1"
  [[ -e "$target" ]] || {
    printf '0B'
    return
  }
  du -sh "$target" 2>/dev/null | awk '{print $1}'
}

move_or_delete() {
  local target="$1"
  local label="${2:-$target}"

  [[ -e "$target" || -L "$target" ]] || return 0
  is_safe_target "$target" || die "refusing unsafe target: $target"

  local size
  size="$(human_size "$target")"

  if (( DRY_RUN )); then
    log "[dry-run] would clean $label ($size)"
    return 0
  fi

  if (( USE_TRASH )); then
    mkdir -p "$TRASH_DIR"
    local base="${target:t}"
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    local destination="$TRASH_DIR/${base}.${stamp}"
    log "Moving to Trash: $label ($size)"
    mv "$target" "$destination"
  else
    log "Deleting: $label ($size)"
    rm -rf "$target"
  fi
}

clean_glob() {
  local label="$1"
  shift
  local pattern
  for pattern in "$@"; do
    local targets=(${~pattern}(N))
    local target
    for target in "${targets[@]}"; do
      move_or_delete "$target" "$label: $target"
    done
  done
}

clean_contents() {
  local dir="$1"
  local label="$2"
  [[ -d "$dir" ]] || return 0

  local entries=("$dir"/*(N) "$dir"/.[!.]*(N) "$dir"/..?*(N))
  local entry
  for entry in "${entries[@]}"; do
    move_or_delete "$entry" "$label: $entry"
  done
}

clean_downloads_old() {
  local dir="$HOME/Downloads"
  [[ -d "$dir" ]] || return 0
  (( DOWNLOADS_DAYS > 0 )) || die "--downloads-old-days must be greater than 0"

  log "Scanning Downloads for files older than ${DOWNLOADS_DAYS} days..."
  local targets
  targets=("${(@f)$(find "$dir" -mindepth 1 -maxdepth 1 -mtime +"$DOWNLOADS_DAYS" -print 2>/dev/null)}")
  local target
  for target in "${targets[@]}"; do
    move_or_delete "$target" "old Downloads item: $target"
  done
}

parse_args() {
  while (( $# > 0 )); do
    case "$1" in
      --execute)
        DRY_RUN=0
        ;;
      --delete)
        USE_TRASH=0
        ;;
      --empty-trash)
        EMPTY_TRASH=1
        ;;
      --dev)
        CLEAN_DEV=1
        ;;
      --browser)
        CLEAN_BROWSER=1
        ;;
      --downloads-old-days)
        shift
        [[ $# -gt 0 ]] || die "--downloads-old-days requires a number"
        DOWNLOADS_DAYS="$1"
        CLEAN_DOWNLOADS=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
    shift
  done
}

main() {
  parse_args "$@"

  log "macOS junk cleanup"
  if (( DRY_RUN )); then
    log "Mode: dry run"
  elif (( USE_TRASH )); then
    log "Mode: execute, move to Trash"
  else
    log "Mode: execute, delete immediately"
  fi
  log ""

  clean_contents "$HOME/Library/Caches" "user cache"
  clean_contents "$HOME/Library/Logs" "user log"
  clean_contents "$HOME/Library/Application Support/CrashReporter" "crash report"
  clean_contents "$HOME/Library/DiagnosticReports" "diagnostic report"
  clean_glob "temporary file" "$HOME"/.Trash/.DS_Store "$HOME"/Library/Saved\ Application\ State/* "$HOME"/Library/Containers/*/Data/Library/Caches/*
  clean_glob "system temp owned by user" /private/var/folders/*/*/*/com.apple.DeletedUsersCache/* /private/var/folders/*/*/*/TemporaryItems/*

  if (( CLEAN_BROWSER )); then
    clean_contents "$HOME/Library/Safari/LocalStorage" "Safari local storage"
    clean_contents "$HOME/Library/Caches/com.apple.Safari" "Safari cache"
    clean_contents "$HOME/Library/Caches/Google/Chrome" "Chrome cache"
    clean_contents "$HOME/Library/Application Support/Google/Chrome/Default/Cache" "Chrome default cache"
    clean_contents "$HOME/Library/Caches/Microsoft Edge" "Edge cache"
    clean_contents "$HOME/Library/Caches/Firefox" "Firefox cache"
    clean_glob "Firefox profile cache" "$HOME"/Library/Application\ Support/Firefox/Profiles/*/cache2
  fi

  if (( CLEAN_DEV )); then
    clean_contents "$HOME/Library/Developer/Xcode/DerivedData" "Xcode DerivedData"
    clean_contents "$HOME/Library/Developer/Xcode/iOS DeviceSupport" "Xcode iOS DeviceSupport"
    clean_contents "$HOME/Library/Developer/CoreSimulator/Caches" "CoreSimulator cache"
    clean_contents "$HOME/.npm/_cacache" "npm cache"
    clean_contents "$HOME/Library/Caches/Yarn" "Yarn cache"
    clean_contents "$HOME/Library/pnpm/store" "pnpm store"
    clean_contents "$HOME/Library/Caches/pip" "pip cache"
    clean_contents "$HOME/Library/Caches/uv" "uv cache"
    clean_contents "$HOME/.gradle/caches" "Gradle cache"
  fi

  if (( CLEAN_DOWNLOADS )); then
    clean_downloads_old
  fi

  if (( EMPTY_TRASH )); then
    (( DRY_RUN )) && die "--empty-trash requires --execute"
    is_safe_target "$TRASH_DIR" || die "refusing unsafe Trash path: $TRASH_DIR"
    log "Emptying Trash..."
    rm -rf "$TRASH_DIR"/*(N) "$TRASH_DIR"/.[!.]*(N) "$TRASH_DIR"/..?*(N)
  fi

  log ""
  if (( DRY_RUN )); then
    log "Dry run complete. Re-run with --execute to clean."
  else
    log "Cleanup complete."
    (( USE_TRASH )) && log "Files moved to Trash. Empty Trash later to reclaim disk space."
  fi
}

main "$@"
