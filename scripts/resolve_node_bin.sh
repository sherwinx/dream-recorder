#!/usr/bin/env bash
# Print the directory holding a usable `node` binary, or fail loudly.
#
# The launchd agents run without a login shell, so nvm is never sourced and node
# is not on PATH. Hardcoding one install path is what broke this before: node@20
# was uninstalled from Homebrew and the Plaud importer silently died every day.
# Instead we follow whatever `nvm alias default` points at, so upgrading node
# through nvm keeps working with no edit here.
set -euo pipefail

NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
NODE_VERSIONS_DIR="$NVM_DIR/versions/node"
NODE_FALLBACK_DIRS="${NODE_FALLBACK_DIRS-/opt/homebrew/bin:/usr/local/bin}"

# Installed nvm versions, newest first. `sort -V` is required so v22.16.0 beats
# v22.9.0 -- a lexical sort gets this backwards.
installed_versions_newest_first() {
  [ -d "$NODE_VERSIONS_DIR" ] || return 0
  find "$NODE_VERSIONS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'v*' \
    -exec basename {} \; | sort -Vr
}

# Follow nvm's alias files (default -> "22", or default -> lts/iron -> v20.x)
# until we hit a value that is not itself an alias.
resolve_alias() {
  local name="$1" hops=0 value
  while [ "$hops" -lt 5 ] && [ -f "$NVM_DIR/alias/$name" ]; do
    value="$(tr -d '[:space:]' <"$NVM_DIR/alias/$name")"
    [ -n "$value" ] || break
    name="$value"
    hops=$((hops + 1))
  done
  printf '%s' "$name"
}

# Match a possibly-partial version ("22", "v22.16", "22.16.0") to an install.
match_version() {
  local want="${1#v}" version
  [ -n "$want" ] || return 1
  while IFS= read -r version; do
    case "${version#v}" in
      "$want" | "$want".*) printf '%s' "$version"; return 0 ;;
    esac
  done < <(installed_versions_newest_first)
  return 1
}

selected=""
if [ -f "$NVM_DIR/alias/default" ]; then
  selected="$(match_version "$(resolve_alias default)" || true)"
fi
if [ -z "$selected" ]; then
  selected="$(installed_versions_newest_first | head -n 1)"
fi
if [ -n "$selected" ] && [ -x "$NODE_VERSIONS_DIR/$selected/bin/node" ]; then
  printf '%s\n' "$NODE_VERSIONS_DIR/$selected/bin"
  exit 0
fi

IFS=':' read -r -a fallback_dirs <<<"$NODE_FALLBACK_DIRS"
for dir in "${fallback_dirs[@]}"; do
  if [ -n "$dir" ] && [ -x "$dir/node" ]; then
    printf '%s\n' "$dir"
    exit 0
  fi
done

echo "resolve_node_bin: no usable node found (searched $NODE_VERSIONS_DIR and $NODE_FALLBACK_DIRS)" >&2
exit 1
