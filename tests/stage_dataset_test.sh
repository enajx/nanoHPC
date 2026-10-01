#!/usr/bin/env bash
# Tests for stage-dataset, in temporary folders. Needs GNU tools (flock, stat -c, sha256sum), so the
# end-to-end test runs it on a simulated Linux machine: stage_dataset_test.sh PATH_TO_STAGE_DATASET
set -euo pipefail

stage=$1
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT

export STAGE_SCRATCH_ROOT="$test_root/scratch/staged"
export STAGE_LOCK_ROOT="$test_root/scratch/locks"
export STAGE_CONFIG_FILE=/dev/null
export STAGE_HOME_ROOT="$test_root/home/stageuser"
mkdir -p "$STAGE_HOME_ROOT/private-data" "$STAGE_HOME_ROOT/broken"
printf 'private\n' > "$STAGE_HOME_ROOT/private-data/data.txt"
printf 'broken\n' > "$STAGE_HOME_ROOT/broken/data.txt"

# Staged once, reused while unchanged, private to the user.
private=$("$stage" --private private-data)
test -f "$private/.ready"
test "$(cat "$private/data.txt")" = private
test "$(stat -c '%a' "$private")" = 700
test "$("$stage" --private private-data)" = "$private"

# Jobs that use the copy are recorded, so cleanup keeps it while they run.
SLURM_JOB_ID=101 "$stage" --private private-data >/dev/null
SLURM_JOB_ID=102 "$stage" --private private-data >/dev/null
test -f "$private/.jobs/101"
test -f "$private/.jobs/102"

# A changed folder gets a new copy; two jobs staging it at once get the same one.
printf 'changed\n' > "$STAGE_HOME_ROOT/private-data/data.txt"
"$stage" --private private-data > "$test_root/first-path" &
first_pid=$!
"$stage" --private private-data > "$test_root/second-path" &
second_pid=$!
wait "$first_pid"
wait "$second_pid"
cmp "$test_root/first-path" "$test_root/second-path"
test "$(cat "$test_root/first-path")" != "$private"

# A failed copy and a full scratch never leave a copy marked ready.
STAGE_RSYNC_BIN=false "$stage" --private broken >/dev/null 2>&1 && exit 1
test -z "$(find "$STAGE_SCRATCH_ROOT" -path '*' -name .ready -newer "$test_root/second-path" -print)"
if STAGE_MIN_FREE_BYTES=9223372036854775807 "$stage" --private broken 2>/dev/null; then
  echo 'space reserve did not refuse the copy' >&2
  exit 1
fi

# Paths outside the home folder are refused.
"$stage" --private ../outside >/dev/null 2>&1 && exit 1
"$stage" --private /etc >/dev/null 2>&1 && exit 1
echo 'PASS: private staging, reuse, job tracking, concurrency, failed-copy and reserve guards, path checks'
