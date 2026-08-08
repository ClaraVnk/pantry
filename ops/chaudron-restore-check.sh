#!/usr/bin/env bash
#
# Chaudron — scheduled restore verification.
#
# Install to:  ~chaudron/chaudron/bin/chaudron-restore-check.sh   (mode 0755)
# Run by:      chaudron-restore-check.service, weekly
# Run WHERE:   the BACKUP host, not the application host. See ops/README.md §8.6
#              and the co-location guard below, which enforces it.
#
# ---------------------------------------------------------------------------
# WHY THIS RUNS ON A TIMER AND NOT "WHEN SOMEBODY REMEMBERS"
# ---------------------------------------------------------------------------
# An untested backup is a belief, not a control. Every way a backup silently
# stops working — a rotated age recipient nobody re-encrypted for, a pg_dump
# that started failing after a PostgreSQL minor bump, a remote that has been
# quietly full for a month — produces files of a plausible size that restore
# into nothing. All of them are invisible until the day the restore is not a
# drill.
#
# So this runs weekly, restores the most recent archive into a DISPOSABLE
# PostgreSQL container, checks that the schema and the reference data came back,
# and destroys the container.
#
# ---------------------------------------------------------------------------
# WHY A DISPOSABLE CONTAINER AND NOT THE PRODUCTION CLUSTER
# ---------------------------------------------------------------------------
# This used to `podman exec` into `chaudron-db` and restore into a scratch
# DATABASE inside the production cluster, as the owner role. Three things were
# wrong with that, in increasing order of seriousness:
#
#   * The owner role is exempt from every RLS policy — migration 0004 does not
#     set FORCE ROW LEVEL SECURITY, deliberately (ops/README.md §6). So the drill
#     used the one connection in the system that can read and write across every
#     household, weekly, unattended.
#   * `age` encrypts TO a recipient; it does not authenticate the SENDER, and
#     the recipient's public key sits in clear text in `backup.env`. Anyone able
#     to write a file into the archive directory can therefore produce an archive
#     that decrypts perfectly and contains whatever SQL they like — and
#     `pg_restore` executes SQL. That input was being fed to the production
#     cluster through its most privileged role.
#   * A restore that half-succeeds leaves objects behind in a cluster that is
#     serving requests.
#
# A disposable container removes all three: the SQL executes against a database
# that exists for a couple of minutes, holds nothing, is reachable by nothing
# (`--network none`), and is destroyed either way. It also means this script
# needs no production database at all — which is what lets it run on the backup
# host, and that is the point of the guard below.
#
# ---------------------------------------------------------------------------
# WHAT IT CANNOT CHECK, AND THIS IS THE IMPORTANT PART
# ---------------------------------------------------------------------------
# It cannot tell you that `api_key_ciphertext` is still decryptable, because
# decrypting it requires CHAUDRON_CREDENTIAL_ENCRYPTION_KEY — and that key is
# deliberately not available to the backup chain (ops/README.md §8.4). A restore
# that passes every check here still yields an instance where every stored
# provider credential is dead, if the key was not backed up separately.
#
# The key's own restore drill is a separate, manual exercise. It is in
# ops/README.md §8.5, and it is the one to actually run once a year.

set -euo pipefail

LOCAL_DIR="${CHAUDRON_BACKUP_LOCAL_DIR:-$HOME/chaudron/backups}"

# The disposable cluster. Nothing here is persisted: no volume, no published
# port, no network, a random password that exists only in this process.
SCRATCH_IMAGE="${CHAUDRON_RESTORE_CHECK_IMAGE:-docker.io/library/postgres:16}"
SCRATCH_NAME="chaudron-restore-check-$$"
SCRATCH_DB="${CHAUDRON_RESTORE_CHECK_DB:-chaudron_restore_check}"
SCRATCH_USER=postgres

# The PRIVATE age identity. It is needed to decrypt, which is exactly why the
# host that holds it must not also be the host that runs the application — see
# the co-location guard below and ops/README.md §8.6.
IDENTITY="${CHAUDRON_BACKUP_AGE_IDENTITY:-$HOME/.config/chaudron/backup.age-identity}"

# Tables the restored schema must carry, by name.
#
# The previous check was a bare `[ "$TABLES" -ge 13 ]`, written against migration
# 0004 and never moved since. The schema is now at 23 tables plus
# `alembic_version`, so a dump missing TEN of them passed and was reported as a
# good backup. A floor that trails the schema by ten tables is not a floor.
#
# Naming the tables rather than raising the number is what keeps this honest: a
# count has to be edited on every migration and will therefore drift again,
# whereas a missing named table is a specific, readable failure. The list is the
# tables whose absence makes a restore worthless. It does not need to be every
# table, and it must never name one a future migration might legitimately drop —
# if a migration removes something listed here, remove it here in the same
# commit.
REQUIRED_TABLES="${CHAUDRON_RESTORE_CHECK_TABLES:-alembic_version household household_member user_account storage_location inventory_lot product stock_movement unit shopping_list shopping_list_item recipe_suggestion llm_provider_config}"

log() { printf '%s %s\n' "$(date -Is)" "$*" >&2; }
die() {
  log "ERROR: $*"
  exit 1
}

command -v age >/dev/null 2>&1 || die "age not found"
command -v podman >/dev/null 2>&1 || die "podman not found"
[ -r "$IDENTITY" ] || die "age identity not readable at $IDENTITY — the restore check cannot decrypt anything, which means the backups are UNVERIFIED. See ops/README.md §8.3."

# ---------------------------------------------------------------------------
# The co-location guard, which is the reason this runs where it runs
# ---------------------------------------------------------------------------
# `chaudron-backup.sh` encrypts to a public recipient so that the machine taking
# the backups cannot read them back. This script needs the PRIVATE identity. Run
# both on the same host, under the same user, from the same `backup.env`, and
# that property is gone: one compromise of the `chaudron` account yields the
# archives and the key that opens them.
#
# The old guard tested whether the identity file lived inside the archive
# DIRECTORY. That catches the direct case and the symlink, and misses the one
# that actually happens — same home, same UID, different subdirectory — which is
# precisely what ops/README.md §8.2 told operators to configure. It blocked the
# arrangement nobody chooses and passed the one everybody does.
#
# This is the real test: is the application running here? If the quadlet, the
# credential secret, or the container is on this machine, this is the
# application host and the private identity does not belong on it.
#
# The escape hatch is deliberate and deliberately noisy. A single-machine
# deployment is a legitimate choice; silently continuing to claim the property
# is not.
ACK="${CHAUDRON_RESTORE_CHECK_ACKNOWLEDGE_COLOCATION:-no}"
COLOCATED=""

if [ -e "${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd/chaudron.container" ]; then
  COLOCATED="a chaudron.container quadlet is installed for this user"
fi

if [ -z "$COLOCATED" ] && podman secret inspect chaudron-credential-encryption-key >/dev/null 2>&1; then
  COLOCATED="the chaudron-credential-encryption-key podman secret exists here"
fi

if [ -z "$COLOCATED" ] && podman container exists chaudron >/dev/null 2>&1; then
  COLOCATED="the 'chaudron' application container exists here"
fi

if [ -n "$COLOCATED" ]; then
  if [ "$ACK" != "yes" ]; then
    die "refusing to run: this looks like the APPLICATION host (${COLOCATED}), and this check needs the age PRIVATE identity. Holding both here means one compromise of this account yields the archives AND the key that opens them — the property chaudron-backup.sh exists to provide. Move this timer to the backup host (ops/README.md §8.6), or set CHAUDRON_RESTORE_CHECK_ACKNOWLEDGE_COLOCATION=yes to accept the downgrade knowingly."
  fi
  log "WARNING: running on the application host (${COLOCATED}) with the age private identity present."
  log "WARNING: this deployment does NOT have the property 'the backed-up host cannot decrypt its own backups'. Acknowledged via CHAUDRON_RESTORE_CHECK_ACKNOWLEDGE_COLOCATION — see ops/README.md §8.6."
fi

# Kept from the old guard: still true, still cheap, and it catches the archive
# directory being used as a key store whichever host this is.
case "$(readlink -f "$IDENTITY")" in
  "$(readlink -f "$LOCAL_DIR")"/*)
    die "the age identity is inside $LOCAL_DIR. Move it out: an archive and the key that opens it must not share a directory." ;;
esac

# ---------------------------------------------------------------------------
# Find something to restore
# ---------------------------------------------------------------------------
LATEST="$(find "$LOCAL_DIR" -maxdepth 1 -name 'chaudron-*.dump.age' -printf '%T@ %p\n' \
  | sort -rn | head -1 | cut -d' ' -f2-)"
[ -n "$LATEST" ] || die "no backup archive found in $LOCAL_DIR"

AGE_HOURS=$((($(date +%s) - $(stat -c %Y "$LATEST")) / 3600))
log "most recent archive: $LATEST (${AGE_HOURS}h old)"

# A backup job that stopped running is the most common backup failure of all,
# and it produces no error anywhere — just an archive that quietly gets older.
[ "$AGE_HOURS" -lt 48 ] || die "the most recent backup is ${AGE_HOURS}h old; chaudron-backup.timer is not doing its job"

# ---------------------------------------------------------------------------
# Stand up the disposable cluster
# ---------------------------------------------------------------------------
cleanup() { podman rm -f "$SCRATCH_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# The password never leaves this process: it goes to a container that lives for
# the duration of the check, is published on no port, and is on no network.
# `--network none` is what makes "reachable by nothing" true rather than merely
# likely — restoring an archive this script cannot authenticate must not be able
# to reach anything even if the SQL inside it tries.
SCRATCH_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '\n=/+')"

log "starting disposable PostgreSQL ($SCRATCH_IMAGE) as $SCRATCH_NAME"
podman run -d --rm --name "$SCRATCH_NAME" \
  --network none \
  --env POSTGRES_PASSWORD="$SCRATCH_PASSWORD" \
  --env POSTGRES_DB="$SCRATCH_DB" \
  --env PGDATA=/var/lib/postgresql/data/pgdata \
  --tmpfs /var/lib/postgresql/data:rw,size=2g \
  --memory=768m --pids-limit=512 \
  --security-opt=no-new-privileges \
  "$SCRATCH_IMAGE" >/dev/null \
  || die "could not start the disposable PostgreSQL container"

log "waiting for it to accept connections"
READY=0
for _ in $(seq 1 60); do
  if podman exec "$SCRATCH_NAME" pg_isready -U "$SCRATCH_USER" -d "$SCRATCH_DB" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
[ "$READY" -eq 1 ] || die "the disposable PostgreSQL never became ready"

# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
# `--no-owner` and `--no-privileges`: the disposable cluster has no chaudron_app
# role to grant to, and the point of this drill is that the DATA came back, not
# that the ACLs did. The privilege side is exercised by provision_app_role.py on
# a real restore (ops/README.md §8.3).
log "restoring into $SCRATCH_NAME"
age --decrypt --identity "$IDENTITY" "$LATEST" \
  | podman exec -i "$SCRATCH_NAME" \
    pg_restore -U "$SCRATCH_USER" -d "$SCRATCH_DB" --no-owner --no-privileges \
  || die "pg_restore FAILED on $LATEST — this backup is not restorable"

psql_scratch() {
  podman exec "$SCRATCH_NAME" psql -tAX -U "$SCRATCH_USER" -d "$SCRATCH_DB" "$@"
}

# ---------------------------------------------------------------------------
# Did anything actually come back?
# ---------------------------------------------------------------------------
# pg_restore exits 0 on an archive that contained a schema and no rows, which is
# precisely what a subtly broken dump produces. So: check that every table that
# matters is present BY NAME, that the migration head survived, and that
# reference data is in a table that is never legitimately empty.
RESTORED="$(psql_scratch -c "select table_name from information_schema.tables where table_schema='public';")"
TABLES="$(printf '%s\n' "$RESTORED" | grep -c . || true)"
log "restored ${TABLES} tables"

MISSING=""
for t in $REQUIRED_TABLES; do
  printf '%s\n' "$RESTORED" | grep -qx -- "$t" || MISSING="${MISSING} ${t}"
done
[ -z "$MISSING" ] || die "the restore is missing required tables:${MISSING} — only ${TABLES} tables came back. This dump is incomplete; do not treat it as a backup."

# `alembic_version` carries exactly one row naming the schema revision. A dump
# that lost it restores into a database no migration can ever be applied to
# again, and nothing else in this check would notice.
REVISIONS="$(psql_scratch -c "select version_num from alembic_version;" | grep -c . || true)"
[ "$REVISIONS" = "1" ] || die "alembic_version holds ${REVISIONS} rows, expected exactly 1 — the restored schema has no usable migration head"

UNITS="$(psql_scratch -c "select count(*) from unit;" 2>/dev/null || echo 0)"
log "reference data: ${UNITS} rows in unit"
[ "$UNITS" -gt 0 ] || die "the 'unit' reference table is empty in the restore; migration 0002 seeds it, so an empty one means the dump is incomplete"

log "RESTORE CHECK PASSED for $LATEST (${TABLES} tables, migration head present, reference data present)"
log "NOT verified by this check: that CHAUDRON_CREDENTIAL_ENCRYPTION_KEY still opens api_key_ciphertext. See ops/README.md §8.5."
