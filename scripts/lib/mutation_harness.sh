# Shared helpers for scripts/mutate_cohorts.sh, mutate_slack_mirror.sh and
# mutate_system.sh. SOURCED, never executed, and it sets no shell options: the caller
# owns `set -uo pipefail`.
#
# The three harnesses used to carry byte-identical copies of these blocks, and each
# repair had to be made three times. The caller provides, before calling:
#   ROOT   physical path of the repository      PY     absolute interpreter with pytest
#   COPY   the mutated tree (mh_* functions below)
#   killed survived errors broken_inert inert_ok fail   integer counters, and the array
#   SURVIVORS; mh_score updates them. `void` stays the caller's own counter.
# Inert controls are scored apart: they never enter the "killed K/R real mutants"
# figure, which counts real mutants only.

# An operator-supplied directory is accepted only if it is a real directory (not a
# symlink, checked before anything resolves it) owned by the current user, and is then
# forced to 0700: `mkdir -p -m 0700` sets no mode on a directory that already exists, so
# a pre-made 0755 copy directory would otherwise hold a world-readable copy of `.env`.
# $1 = the variable's name (for messages), $2 = its path, $3 = "empty" to also require
# that the directory be absent or empty.
secure_override_dir() {
  local d="$2"
  while [ "$d" != "/" ] && [ "${d%/}" != "$d" ]; do d="${d%/}"; done
  if [ -L "$d" ]; then
    echo "ERROR: $1=$2 is a symlink; refusing it." >&2; return 1
  fi
  if [ "${3:-}" = "empty" ] && [ -e "$d" ] && [ -n "$(ls -A -- "$d" 2>/dev/null)" ]; then
    echo "ERROR: $1=$2 exists and is not empty; refusing to reuse it." >&2; return 1
  fi
  mkdir -p -m 0700 -- "$d" || { echo "ERROR: cannot create $1=$2" >&2; return 1; }
  if [ -L "$d" ] || [ ! -d "$d" ] || [ ! -O "$d" ]; then
    echo "ERROR: $1=$2 is not a directory owned by $(id -un); refusing it." >&2; return 1
  fi
  chmod 0700 -- "$d" || { echo "ERROR: cannot chmod 0700 $1=$2" >&2; return 1; }
}

# The copy directory is guarded before the tar and before every `rm -rf`. If mktemp
# failed, COPY would be empty, bash's `cd ""` would succeed as a no-op, `pwd -P` would
# return the repo root, the mutants would land in the live tree and the EXIT trap would
# delete it. Physical paths are compared with physical paths.
copy_is_safe() {
  case "$COPY" in
    ""|/) echo "ERROR: unsafe copy directory '${COPY}'" >&2; return 1 ;;
    /*) ;;
    *) echo "ERROR: copy directory '$COPY' is not absolute" >&2; return 1 ;;
  esac
  case "$COPY/" in
    "$ROOT"/*) echo "ERROR: copy directory $COPY is the repository or inside it ($ROOT)" >&2; return 1 ;;
  esac
  case "$ROOT/" in
    "$COPY"/*) echo "ERROR: copy directory $COPY contains the repository ($ROOT)" >&2; return 1 ;;
  esac
  return 0
}

# Copy the tree into $COPY, outside the repository.
# backups/ holds the production dumps (RCA S1). .env stays in, for parity with ci.sh:
# Settings reads a cwd-relative .env, and the copy is a 0700 directory removed on EXIT,
# INT, TERM or HUP.
mh_make_copy() {
  if ! tar -C "$ROOT" \
        --exclude=./.git --exclude=./.venv-test --exclude=./backups --exclude=./logs \
        --exclude=./mutants --exclude=./build --exclude=./.hypothesis --exclude=./.pytest_cache \
        --exclude=./.ruff_cache --exclude=./.playwright-mcp --exclude=__pycache__ \
        -cf - . | tar -C "$COPY" -xf -; then
    echo "ERROR: could not copy $ROOT into $COPY." >&2
    return 1
  fi
}

# Guard 1: `import src` from the copy must resolve inside the copy. Otherwise a run
# would exercise unmutated code and report every mutant as SURVIVED.
mh_check_provenance() {
  local prov
  prov=$(cd "$COPY" && "$PY" -c 'import src; print(src.__file__)' 2>/dev/null)
  case "$prov" in
    "$COPY"/src/__init__.py) echo "provenance OK: pytest will import $prov" ;;
    *)
      echo "ERROR: from $COPY, 'import src' resolves to '${prov:-<nothing>}', not" >&2
      echo "$COPY/src/__init__.py. The mutants would not be under test. Refusing to run." >&2
      return 1 ;;
  esac
}

# Apply one mutant to the COPY: FILE (repo-relative), FROM, TO (`\n` is a newline).
# Returns 1, with the reason on stderr, if FROM is missing or not unique.
mh_apply_mutant() {
  local file="$1" from="$2" to="$3"
  FROM="$from" TO="$to" "$PY" - "$COPY/$file" <<'PY' 2>&1
import os, pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
frm = os.environ["FROM"].replace("\\n", "\n")
to = os.environ["TO"].replace("\\n", "\n")
if frm not in s:
    sys.stderr.write(f"mutation target not found in {p}:\n{frm!r}\n"); sys.exit(1)
if s.count(frm) != 1:
    sys.stderr.write(f"target occurs {s.count(frm)} times in {p}; it must be unique\n")
    sys.exit(1)
p.write_text(s.replace(frm, to, 1))
PY
}

# Guard 2: the mutant must still IMPORT, or a SyntaxError fakes a kill. Reported by the
# caller as VOID, never as killed. Derived from the path so a new mutant in a new file is
# covered without editing the harness.
mh_check_imports() {
  local file="$1" mod
  mod=$(printf '%s' "${file%.py}" | tr '/' '.')
  (cd "$COPY" && "$PY" -c "import $mod") >/dev/null 2>&1
}

# Score one pytest exit. $1 = exit code, $2 = log, $3 = label, $4 = 1 for an inert control.
mh_score() {
  local rc="$1" log="$2" label="$3" inert="$4" killer
  case "$rc" in
    0)
      if [ "$inert" -eq 1 ]; then
        echo "survived (expected)  $label"
        inert_ok=$((inert_ok + 1))
      else
        echo "SURVIVED  $label"
        SURVIVORS+=("$label")
        survived=$((survived + 1)); fail=1
      fi ;;
    1)
      # Exit 1 is a kill only if a test FAILED. It also comes from a setup or fixture
      # ERROR under -x (testcontainers/Docker in the DB-backed files), a failed `cd`, or
      # a failed `>"$log"` redirect — none of which names a failing test.
      killer=$(grep -m1 '^FAILED ' "$log" 2>/dev/null | sed 's/^FAILED //')
      if [ -z "$killer" ]; then
        echo "ERROR     $label — pytest exit 1 with no FAILED line (setup/collection ERROR," >&2
        echo "          failed cd, or failed log redirect) names no failing test; see $log" >&2
        fail=1; errors=$((errors + 1))
      elif [ "$inert" -eq 1 ]; then
        echo "KILLED AN INERT MUTANT  $label" >&2
        echo "          -> $killer" >&2
        echo "          The selection is failing for a reason that is NOT the mutation, so" >&2
        echo "          every other number in this run is meaningless." >&2
        broken_inert=$((broken_inert + 1)); fail=1
      else
        echo "killed    $label"
        echo "          by $killer"
        killed=$((killed + 1))
      fi ;;
    124|137)
      # Never a kill, inert or real: a kill must name a failing test.
      echo "ERROR     $label — TIMEOUT after ${MUT_TIMEOUT:-?}s (exit $rc); see $log" >&2
      fail=1; errors=$((errors + 1)) ;;
    *)
      # 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected: none of
      # them names a failing test, so none is a kill.
      echo "ERROR     $label — pytest exit $rc names no failing test; see $log" >&2
      fail=1; errors=$((errors + 1)) ;;
  esac
}

# Put the file back from the repository and verify it. Returns 1 when the copy is
# polluted; the caller must stop, because every later result would be unattributable.
mh_restore() {
  local file="$1"
  cp -- "$file" "$COPY/$file" >/dev/null 2>&1
  if ! cmp -s -- "$file" "$COPY/$file"; then
    echo "ERROR: $COPY/$file no longer matches $file; the copy is polluted and" >&2
    echo "every result after this point is unattributable. Stopping." >&2
    return 1
  fi
}

# Summary. Real mutants only in the denominator; inert controls are a separate line.
mh_report() {
  echo
  echo "killed ${killed}/$((killed + survived)) real mutants"
  echo "${errors} errors (target moved, timeout, or no failing test named)"
  echo "inert controls: ${inert_ok}/$((inert_ok + broken_inert)) survived (all of them must)"
  if [ "$broken_inert" -gt 0 ]; then
    echo "AN INERT MUTANT WAS KILLED. Read nothing else in this run as a score." >&2
  fi
  if [ "${#SURVIVORS[@]}" -gt 0 ]; then
    echo "SURVIVING MUTANTS (${1}) — each is a behaviour the suite does not protect:" >&2
    for s in "${SURVIVORS[@]}"; do echo "  - $s" >&2; done
  fi
}
