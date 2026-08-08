#!/usr/bin/env bash
#
# Chaudron — encrypted, off-machine PostgreSQL backup.
#
# Install to:  ~chaudron/chaudron/bin/chaudron-backup.sh   (mode 0755)
# Run by:      chaudron-backup.service, daily (chaudron-backup.timer)
# Restore:     ops/chaudron-restore-check.sh, and ops/README.md §8.3
#
# ---------------------------------------------------------------------------
# THE THING THIS SCRIPT EXISTS TO SAY OUT LOUD
# ---------------------------------------------------------------------------
# A Chaudron dump is NOT a complete backup on its own.
#
# `api_key_ciphertext` — every provider key a household has entrusted to this
# instance — is encrypted with CHAUDRON_CREDENTIAL_ENCRYPTION_KEY, which lives
# in a Podman secret and NOT in the database. That is the right design: a stolen
# dump must not be enough to decrypt third-party secrets.
#
# It also means two mistakes sit one step apart, and the documentation used to
# point at neither exit:
#
#   * Restore the dump WITHOUT the key, and you get a database where every
#     stored credential is permanently undecryptable. Nothing errors at restore
#     time; the failure surfaces the first time a household runs a recipe
#     suggestion, as an authentication error against a provider.
#   * Store the key ALONGSIDE the dump, and you have undone the encryption
#     entirely: one stolen archive now yields both halves.
#
# So: this script backs up the DUMP. The KEY is a separate artefact, backed up
# separately, to a different place, on a different schedule — see
# ops/README.md §8.4. This script refuses to be the thing that puts them
# together: it will not read the credential key, and it fails if it finds one in
# its own destination directory.
#
# ---------------------------------------------------------------------------
# ENCRYPTION — and the exact property it does and does not give you
# ---------------------------------------------------------------------------
# `age`, with a PUBLIC recipient key. THIS script holds only the encryption
# half: it cannot decrypt yesterday's backups, and neither can anything that
# compromises it.
#
# That is a narrower statement than the one this comment used to make. It said
# "this host holds only the encryption half", and that was false in the very
# deployment ops/README.md recommends. `chaudron-restore-check.sh` needs the
# PRIVATE identity to decrypt anything, and the install sequence used to put its
# timer on this same machine, reading it from this same `backup.env`, under this
# same UID. Compromise the `chaudron` user and you get the archives AND the key —
# the two halves the design exists to keep apart, in one place.
#
# THE RESOLUTION, and it is a topology decision rather than a code one:
# the restore drill does not run here. `chaudron-restore-check.sh` refuses to run
# on the application host unless the operator explicitly acknowledges the
# downgrade, and the recommended home for it is the backup host, which already
# holds the archives and does not run anything facing the internet. See
# ops/README.md §8.6 for the argument, the two topologies, and what each costs.
#
# So the property this host actually has, stated so it can be checked:
#   this machine can encrypt backups and cannot read them back.
# Verify it the blunt way — expect no output:
#   grep -rl 'AGE-SECRET-KEY-1' ~chaudron/ 2>/dev/null
#
# Generate the pair on a MACHINE THAT IS NOT THIS SERVER:
#   age-keygen -o chaudron-backup.age-key    # keep this private, offline
#   grep 'public key' chaudron-backup.age-key
# then put the public half in CHAUDRON_BACKUP_AGE_RECIPIENT below.
#
# ---------------------------------------------------------------------------
# CONFIGURATION — ~chaudron/chaudron/backup.env, mode 0600
# ---------------------------------------------------------------------------
#   CHAUDRON_BACKUP_AGE_RECIPIENT   age public key (age1…). REQUIRED.
#   CHAUDRON_BACKUP_LOCAL_DIR       staging directory (default ~/chaudron/backups)
#   CHAUDRON_BACKUP_REMOTE          rsync destination, e.g. backup@host:/srv/chaudron
#                                   REQUIRED — a backup that never leaves the
#                                   machine does not survive the machine.
#   CHAUDRON_BACKUP_SSH_KEY         identity used for the remote copy
#   CHAUDRON_BACKUP_KEEP_DAYS       local retention in days (default 7)
#   CHAUDRON_BACKUP_KEEP_WEEKLY     weekly copies kept locally (default 4)

set -euo pipefail

LOCAL_DIR="${CHAUDRON_BACKUP_LOCAL_DIR:-$HOME/chaudron/backups}"
KEEP_DAYS="${CHAUDRON_BACKUP_KEEP_DAYS:-7}"
KEEP_WEEKLY="${CHAUDRON_BACKUP_KEEP_WEEKLY:-4}"
SSH_KEY="${CHAUDRON_BACKUP_SSH_KEY:-$HOME/.ssh/chaudron-backup}"
DB_CONTAINER="${CHAUDRON_DB_CONTAINER:-chaudron-db}"
DB_NAME="${CHAUDRON_DB_NAME:-chaudron}"
DB_USER="${CHAUDRON_DB_USER:-chaudron}"

log() { printf '%s %s\n' "$(date -Is)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

: "${CHAUDRON_BACKUP_AGE_RECIPIENT:?set CHAUDRON_BACKUP_AGE_RECIPIENT (age1…) in backup.env}"
: "${CHAUDRON_BACKUP_REMOTE:?set CHAUDRON_BACKUP_REMOTE in backup.env — a local-only backup does not survive the machine}"

command -v age >/dev/null 2>&1 || die "age not found. dnf install age (EPEL), or see ops/README.md §8.1"
command -v rsync >/dev/null 2>&1 || die "rsync not found. dnf install rsync"
command -v podman >/dev/null 2>&1 || die "podman not found"

[ "${CHAUDRON_BACKUP_AGE_RECIPIENT#age1}" != "$CHAUDRON_BACKUP_AGE_RECIPIENT" ] \
  || die "CHAUDRON_BACKUP_AGE_RECIPIENT does not look like an age PUBLIC key (age1…). Never put the private identity (AGE-SECRET-KEY-…) on this host."

install -d -m 0700 "$LOCAL_DIR"

# ---------------------------------------------------------------------------
# Refuse to co-locate the two artefacts
# ---------------------------------------------------------------------------
# The failure this guards against is entirely human: somebody restores an
# instance, needs the credential key, drops a copy next to the dumps "for a
# minute", and it stays. At that point one stolen archive carries both the
# ciphertext and the key that opens it, and every backup taken since is
# retroactively worthless as a protection.
#
# THIS USED TO BE A FILENAME TEST, and it missed both names anything in this
# repository actually produces:
#
#   file name                 old patterns          matches?
#   ------------------------- --------------------- --------
#   backup.age-identity       *.age-key             no  <- chaudron-restore-check.sh's default
#   key.txt                   (none)                no  <- age-keygen's default output name
#   chaudron-backup.age-key   *.age-key             yes
#   AGE-SECRET-KEY-1ABC…      AGE-SECRET*           yes
#
# The two it missed are precisely the two an operator ends up with by following
# the defaults: `CHAUDRON_BACKUP_AGE_IDENTITY` defaults to `backup.age-identity`
# in the sibling script, and `age-keygen` with no `-o` writes `key.txt`. A guard
# that passes on the only names the system generates is decoration.
#
# So the test is on CONTENT. An age private identity file always contains the
# literal `AGE-SECRET-KEY-1` — it is the Bech32 HRP of the private key format,
# so it cannot be renamed away and cannot be absent from a usable identity.
# Whatever the file is called, this finds it.
#
# `-I` skips binary files, so the archives themselves are never scanned; they are
# excluded by name as well, because a `.dump.age` is megabytes and there is no
# reason to read them. `-l` stops at the first match in each file.
KEYLIKE="$(
  find "$LOCAL_DIR" -maxdepth 1 -type f ! -name 'chaudron-*.dump.age' -print0 \
    | xargs -0 --no-run-if-empty grep -lI -- 'AGE-SECRET-KEY-1' 2>/dev/null || true
)"
if [ -n "$KEYLIKE" ]; then
  die "an age PRIVATE identity is sitting in $LOCAL_DIR: ${KEYLIKE}. The archives and the key that opens them must NOT share a directory — that is the whole point of encrypting to a recipient. Move it out and re-run. See ops/README.md §8.4."
fi

# The credential encryption key has no content signature to test for: it is
# `openssl rand -base64 32`, which is indistinguishable from any other base64.
# So for that one artefact the filename heuristic is all there is, and it is
# kept as a heuristic rather than as a control — the real protection is that
# this script never reads that secret and never writes it anywhere.
if find "$LOCAL_DIR" -maxdepth 1 -type f \
  \( -name '*credential*' -o -name '*encryption-key*' -o -name '*.age-key' -o -name '*age-identity*' \) \
  -print -quit | grep -q .; then
  die "a key-shaped filename is sitting in $LOCAL_DIR. The dump and the credential encryption key must NOT live in the same place — that is the whole point of encrypting the credentials. Move it out and re-run. See ops/README.md §8.4."
fi

STAMP="$(date -u +%Y-%m-%dT%H%M%SZ)"
DUMP="${LOCAL_DIR}/chaudron-${STAMP}.dump.age"
TMP="${DUMP}.partial"

cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Dump → encrypt, in one pipe
# ---------------------------------------------------------------------------
# The plaintext dump never touches the filesystem: `pg_dump` writes to stdout,
# `age` reads it and writes ciphertext. Writing a plaintext dump and encrypting
# it afterwards would leave a window — and, on a crash, a permanent copy — of
# the whole database in the clear on disk.
#
# `--format=custom` because it is what `pg_restore` needs for selective restore
# and parallel restore; a plain SQL dump can only be replayed whole.
#
# `set -o pipefail` (top of file) is what makes a pg_dump failure fail the
# script instead of producing a valid encryption of a truncated dump.
log "dumping ${DB_NAME} from ${DB_CONTAINER}"
podman exec "$DB_CONTAINER" \
    pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom --compress=9 \
  | age --recipient "$CHAUDRON_BACKUP_AGE_RECIPIENT" --output "$TMP"

# An empty or absurdly small archive means pg_dump wrote nothing useful. Catch
# it here rather than discovering it during a restore.
SIZE="$(stat -c %s "$TMP")"
[ "$SIZE" -gt 4096 ] || die "dump is only ${SIZE} bytes — refusing to keep it"

chmod 0600 "$TMP"
mv "$TMP" "$DUMP"
trap - EXIT
log "wrote $DUMP (${SIZE} bytes, encrypted)"

# ---------------------------------------------------------------------------
# Off-machine copy
# ---------------------------------------------------------------------------
# The backup is already encrypted, so the remote end never sees plaintext and
# does not need to be trusted with the data — only with its availability.
#
# Use a dedicated SSH key restricted on the remote side to rsync in one
# directory (`command="rrsync -wo /srv/chaudron"` in authorized_keys). A backup
# job that can also DELETE remotely is one compromise away from destroying the
# backups it exists to make, which is why there is no `--delete` here: remote
# retention is the remote's business, enforced by its own timer.
log "copying to ${CHAUDRON_BACKUP_REMOTE}"
rsync --archive --partial \
      -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=yes -o BatchMode=yes" \
      "$DUMP" "${CHAUDRON_BACKUP_REMOTE}/" \
  || die "off-machine copy FAILED. The local dump was kept, but this backup does not yet survive the loss of this host."

log "off-machine copy complete"

# ---------------------------------------------------------------------------
# Local retention
# ---------------------------------------------------------------------------
# Local copies are a convenience for a fast restore, not the backup itself.
# Daily for KEEP_DAYS, plus the most recent KEEP_WEEKLY Monday copies, so a
# corruption noticed three weeks late still has something to go back to.
log "pruning local copies older than ${KEEP_DAYS} days (keeping ${KEEP_WEEKLY} weekly)"

mapfile -t WEEKLY < <(
  find "$LOCAL_DIR" -maxdepth 1 -name 'chaudron-*.dump.age' -printf '%f\n' \
    | while read -r f; do
        d="${f#chaudron-}"; d="${d%%T*}"
        [ "$(date -d "$d" +%u 2>/dev/null || echo 0)" = "1" ] && printf '%s\n' "$f"
      done | sort -r | head -n "$KEEP_WEEKLY"
)

find "$LOCAL_DIR" -maxdepth 1 -name 'chaudron-*.dump.age' -mtime "+${KEEP_DAYS}" -print0 \
  | while IFS= read -r -d '' path; do
      base="$(basename "$path")"
      for keep in ${WEEKLY[@]+"${WEEKLY[@]}"}; do
        [ "$base" = "$keep" ] && continue 2
      done
      log "pruning $base"
      rm -f -- "$path"
    done

# ---------------------------------------------------------------------------
# The reminder that no script can enforce
# ---------------------------------------------------------------------------
log "REMINDER: this dump is useless without CHAUDRON_CREDENTIAL_ENCRYPTION_KEY, which is NOT in it and must NOT be stored with it. See ops/README.md §8.4."
log "done"
