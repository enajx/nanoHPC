#!/bin/bash
# Run inside a Slurm interactive shell. The second argument is the cluster's login address.
# From that shell: bash interactive-notebook.sh jupyter|vscode LOGIN_HOST
# Keep the printed SSH tunnel open on your laptop. Exit the Slurm shell to stop the server.
set -euo pipefail

mode=${1:-}
login_host=${2:-}
if [[ $mode != jupyter && $mode != vscode ]] || [[ ! $login_host =~ ^[A-Za-z0-9_.@-]+$ ]]; then
  echo 'Usage: bash interactive-notebook.sh jupyter|vscode LOGIN_HOST' >&2
  exit 2
fi
if [[ -z ${SLURM_JOB_ID:-} ]]; then
  echo 'Start an interactive Slurm job before running this helper.' >&2
  exit 2
fi

port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')
node=$(hostname)
echo "On your laptop, keep this open: ssh -N -L $port:$node:$port $login_host"

if [[ $mode == jupyter ]]; then
  umask 077
  token_file=$(mktemp)
  trap 'rm -f "$token_file"' EXIT
  python3 -c 'import secrets; print(secrets.token_hex(24))' > "$token_file"
  echo 'Then open the 127.0.0.1 link with the token that Jupyter prints below.'
  echo 'In desktop VS Code, choose an existing Jupyter server and paste that link.'
  JUPYTER_TOKEN_FILE="$token_file" uv run --with jupyterlab jupyter lab --no-browser --ip=0.0.0.0 --port="$port"
  exit
fi

case $(uname -m) in
  x86_64) arch=x64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo 'Browser-based VS Code needs an x86-64 or ARM64 Linux machine.' >&2; exit 2 ;;
esac
if [[ ! -x $HOME/.local/bin/code ]]; then
  mkdir -p "$HOME/.local/bin"
  archive=$(mktemp)
  trap 'rm -f "$archive"' EXIT
  curl --fail --silent --show-error --location "https://code.visualstudio.com/sha/download?build=stable&os=cli-alpine-$arch" --output "$archive"
  tar -xzf "$archive" -C "$HOME/.local/bin" code
  rm -f "$archive"
  trap - EXIT
fi

umask 077
token_file=$(mktemp)
trap 'rm -f "$token_file"' EXIT
python3 -c 'import secrets; print(secrets.token_hex(24))' > "$token_file"
echo "Then open in your browser: http://127.0.0.1:$port/?tkn=$(cat "$token_file")"
"$HOME/.local/bin/code" serve-web --accept-server-license-terms --host 0.0.0.0 --port "$port" --connection-token-file "$token_file"
