#!/usr/bin/env bash
#
# Chaudron — gated image update.
#
# Install to:  ~chaudron/chaudron/bin/verified-auto-update.sh   (mode 0755)
# Run by:      chaudron-verified-update.service, every 15 minutes
# Never run:   podman-auto-update.timer — it is masked in this deployment, and
#              this script refuses to run while it is not.
#
# Usage:
#   verified-auto-update.sh                pull, verify, deploy if the digest moved
#   verified-auto-update.sh --verify-only  pull and verify; never restart anything
#
# ---------------------------------------------------------------------------
# WHAT THIS REPLACES, AND WHY
# ---------------------------------------------------------------------------
# `publish.yml` signs every published digest with cosign in keyless mode. That
# signature was produced and never checked: `grep -rn cosign` over the whole
# repository found no `cosign verify` anywhere outside a documentation example.
#
# Meanwhile `chaudron.container` declared `Image=…:latest` with
# `AutoUpdate=registry`, and the stock timer polled every 15 minutes. So the
# real chain of trust ended at the registry token: anyone holding
# `packages: write` on GHCR could land an image in production inside a quarter
# of an hour, with no human in the loop. That is remote code execution with
# patience.
#
# ops/README.md described that gap precisely and listed four ways out. This
# script is option 2 — gate the update — chosen because it keeps keyless
# signing (no long-lived private key to steal or rotate) while turning the
# signature from after-the-fact evidence into an actual precondition.
#
# ---------------------------------------------------------------------------
# THE TOCTOU THIS USED TO LEAVE OPEN
# ---------------------------------------------------------------------------
# The first version of this script ended with `podman auto-update`, under a
# comment asserting the pull-to-deploy window was closed because the digest had
# been re-resolved from the local store. That was wrong, and podman-auto-update(1)
# on this host states the opposite outright:
#
#   "registry: ... Podman reaches out to the corresponding registry to check if
#    the image has been updated. An image is considered updated if the digest in
#    the local storage is different than the one of the remote image. If an image
#    must be updated, Podman pulls it down and restarts the systemd unit."
#
# `podman auto-update` queries the REGISTRY and pulls the image ITSELF. It never
# looked at the digest this script had verified. The attack that opens:
#
#   1. this script pulls D1 and starts `cosign verify` — 1-2 s, because the
#      Rekor and Fulcio round trips are on the network;
#   2. an attacker holding the tag pushes D2 inside that window, timed off the
#      script's own pull, which is observable from the registry side;
#   3. verification of D1 succeeds;
#   4. `podman auto-update` asks the registry, sees D2 != D0, pulls D2, restarts.
#
# D2 was verified by nothing. The gate ran, passed, and deployed the other image.
#
# ---------------------------------------------------------------------------
# HOW IT IS CLOSED NOW
# ---------------------------------------------------------------------------
# By never asking the registry a second time. After verification the bytes are
# already on this disk, tagged `:latest` in the local store, and:
#
#   * quadlet generates no `--pull` flag for chaudron.service — check with
#     `QUADLET_UNIT_DIRS=ops /usr/libexec/podman/quadlet -dryrun -user`;
#   * podman-run(1) documents the default `--pull` policy as `missing`: pull only
#     when the image is absent locally. It is present.
#
# So `systemctl --user restart chaudron.service` starts exactly the verified
# bytes. There is no window because there is no second resolution.
#
# `AutoUpdate=registry` has been removed from chaudron.container accordingly, so
# `podman auto-update` now has no candidates at all — the ungated path does not
# merely go unused, it stops existing.
#
# WHAT THAT COSTS: podman's automatic rollback (`--rollback`, default true),
# which restored the previous image when a restarted unit failed. A plain
# `systemctl restart` has no such behaviour. Compensated by, in order:
#
#   * `Restart=always` / `RestartSec=5` in chaudron.container;
#   * the post-restart health wait below, which fails this unit when the new
#     image never becomes healthy — otherwise the unit would exit 0 on a broken
#     deploy and nothing downstream would ever know;
#   * `systemctl --user is-failed chaudron-verified-update.service` as a
#     monitored signal (ops/README.md §5.1). This is the compensating control.
#     A gate that fails silently every 15 minutes is a gate nobody reads;
#   * a documented manual rollback, ops/README.md §5.3.
#
# On a verification failure the local `:latest` tag is put BACK on the previously
# verified digest before exiting. That is not tidiness: with pull policy
# `missing`, whatever `:latest` points at locally is what the next restart of
# chaudron.service runs — including a restart nobody asked for, from
# `Restart=always` or a reboot. Leaving a rejected image under that tag would
# deploy it later, by accident, which is a strictly worse outcome than the one
# this script exists to prevent.
#
# ---------------------------------------------------------------------------
# WHAT THIS STILL DOES NOT CLOSE
# ---------------------------------------------------------------------------
# A legitimately signed image whose CONTENT is malicious — a compromise of the
# repository itself. Signing proves origin, never intent. Branch protection on
# `main` is the control for that one, and it lives on GitHub, not here.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IMAGE="${CHAUDRON_IMAGE:-ghcr.io/claravnk/chaudron:latest}"

# The unit this script deploys into, and the container it inspects afterwards.
SERVICE="${CHAUDRON_SERVICE:-chaudron.service}"
CONTAINER="${CHAUDRON_CONTAINER:-chaudron}"

# How long to wait for the restarted container to report `healthy`. The quadlet
# sets HealthStartPeriod=20s and HealthInterval=30s, so the first real verdict
# cannot arrive before ~20 s and a three-retry failure takes ~110 s.
HEALTH_TIMEOUT="${CHAUDRON_HEALTH_TIMEOUT:-180}"

# The control. Everything else in this script is plumbing.
#
# This is the exact identity a keyless GitHub Actions certificate carries in its
# URI SAN, and it pins four things at once: the repository, the workflow file,
# the ref that ran it, and the fact that it was a workflow at all.
#
# Do NOT relax it. `--certificate-identity-regexp '.*'` verifies that *somebody*
# signed the image, which is worth nothing because anybody can sign anything.
# `@refs/heads/main` in particular is what stops a signature produced by the
# same workflow running on a branch or a tag from satisfying this check —
# including one produced by `workflow_dispatch` on a branch, which publish.yml
# now refuses outright rather than leaving it to fail here.
CERT_IDENTITY="${CHAUDRON_CERT_IDENTITY:-https://github.com/ClaraVnk/chaudron/.github/workflows/publish.yml@refs/heads/main}"

# The issuer that minted the certificate. Pinning the identity without pinning
# the issuer would accept the same string asserted by any other OIDC provider.
CERT_OIDC_ISSUER="${CHAUDRON_CERT_OIDC_ISSUER:-https://token.actions.githubusercontent.com}"

COSIGN="${COSIGN:-cosign}"

VERIFY_ONLY=0
case "${1:-}" in
  --verify-only) VERIFY_ONLY=1 ;;
  "") ;;
  *) printf 'usage: %s [--verify-only]\n' "$0" >&2; exit 2 ;;
esac

log() { printf '%s %s\n' "$(date -Is)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
command -v podman >/dev/null 2>&1 || die "podman not found in PATH"

# Checked explicitly rather than left to fail mid-run: a missing cosign must
# read as "the gate is not installed", not as "the image is bad". Both stop the
# update, and telling them apart is the difference between a five-minute fix and
# an incident review. Install: see ops/README.md §5.2.
command -v "$COSIGN" >/dev/null 2>&1 \
  || die "cosign not found (looked for '$COSIGN'). The update gate is NOT installed; refusing to run. See ops/README.md §5.2."

# ---------------------------------------------------------------------------
# The stock timer must be masked, and this is where that gets checked
# ---------------------------------------------------------------------------
# `systemctl --user mask podman-auto-update.timer` is step 2 of a seven-command
# list in ops/README.md §2.6. A step in a list is a step that gets skipped, and
# skipping this one used to leave an ungated deploy path enabled with no symptom
# whatsoever — the gate keeps passing, and the other timer keeps winning races.
#
# Removing `AutoUpdate=` from every quadlet already leaves that timer with
# nothing to act on, so this is the second of two independent controls rather
# than the only one. It is asserted here because a script that checks its own
# preconditions is self-carrying, where a checklist is only as good as the last
# person to read it.
#
# `is-enabled` prints `masked` for a masked unit and `not-found` when podman's
# systemd units are not installed for this user at all — also fine.
if command -v systemctl >/dev/null 2>&1; then
  TIMER_STATE="$(systemctl --user is-enabled podman-auto-update.timer 2>/dev/null || true)"
  case "$TIMER_STATE" in
    masked | masked-runtime | not-found | "")
      log "podman-auto-update.timer: ${TIMER_STATE:-absent} (ok)" ;;
    *)
      die "podman-auto-update.timer is '${TIMER_STATE}', not masked. The ungated update path is still armed. Fix with: systemctl --user mask podman-auto-update.timer  (ops/README.md §5.1)" ;;
  esac
fi

# ---------------------------------------------------------------------------
# 1. Pull, and find out whether anything actually moved
# ---------------------------------------------------------------------------
BEFORE="$(podman image inspect "$IMAGE" --format '{{ .Digest }}' 2>/dev/null || true)"

# The local image ID as well as the digest, captured BEFORE the pull moves the
# tag. The ID is what the rollback below tags with, and that is not a stylistic
# choice: once `podman pull` moves `:latest` onto a new digest, the previous
# image is no longer tagged in this repository, and `podman tag repo@<digest>`
# can fail to resolve it — which is the one moment the restore must not fail,
# because failing leaves `:latest` pointing at the REJECTED image. An image ID
# always resolves locally regardless of which tags exist.
BEFORE_ID="$(podman image inspect "$IMAGE" --format '{{ .Id }}' 2>/dev/null || true)"

log "pulling $IMAGE"
podman pull --quiet "$IMAGE" >/dev/null || die "pull failed for $IMAGE"

# Re-resolved from the LOCAL store: this is the digest of the bytes on this disk,
# which is what will be verified and what a restart will run. Asking the registry
# again would name a digest that need not be the one we hold — that mistake is
# the whole subject of the TOCTOU note at the top of this file.
DIGEST="$(podman image inspect "$IMAGE" --format '{{ .Digest }}')"
[ -n "$DIGEST" ] || die "could not resolve a local digest for $IMAGE"

# Same reasoning as BEFORE_ID: the quarantine tag below is applied by ID so it
# cannot fail on reference resolution at the moment it is most needed.
DIGEST_ID="$(podman image inspect "$IMAGE" --format '{{ .Id }}')"

if [ "$BEFORE" = "$DIGEST" ] && [ "$VERIFY_ONLY" -eq 0 ]; then
  log "digest unchanged ($DIGEST); nothing to do"
  exit 0
fi

log "digest: ${BEFORE:-<none>} -> $DIGEST"

# ---------------------------------------------------------------------------
# 2. Verify — by digest, never by tag
# ---------------------------------------------------------------------------
# A signature bound to `:latest` would say nothing about the bytes actually
# pulled, because the tag can move between the signature and the pull. The
# repository name is the tag stripped of its `:tag` suffix.
REPO="${IMAGE%:*}"

# mktemp, not a fixed /tmp path. `/tmp` is sticky, so another user cannot replace
# a file this script owns — but nothing stops them CREATING the fixed path first,
# as a symlink to something this user can write. The `2>` redirection below would
# then follow it and truncate the target, and a quadlet under ~/.config is a
# plausible target. The contents are also logged a few lines down, so a fixed
# name is an attacker-chosen input to the journal as well.
COSIGN_ERR="$(mktemp -t chaudron-cosign.XXXXXXXXXX)"
cleanup() { rm -f "$COSIGN_ERR"; }
trap cleanup EXIT

log "verifying ${REPO}@${DIGEST} against ${CERT_IDENTITY}"
if ! "$COSIGN" verify \
  --certificate-identity "$CERT_IDENTITY" \
  --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
  "${REPO}@${DIGEST}" >/dev/null 2>"$COSIGN_ERR"; then
  log "$(cat "$COSIGN_ERR")"

  # Quarantine the rejected bytes and restore the tag. Both halves matter.
  #
  # The image is KEPT, under an explicit tag, because it is the evidence: a
  # rejected digest is an incident, and deleting the artefact is how an incident
  # becomes an argument about what was actually pulled.
  #
  # The tag is MOVED BACK because `:latest` in the local store is what the next
  # `systemctl restart chaudron.service` will run under pull policy `missing` —
  # and that restart can happen without anybody deciding to deploy, from
  # `Restart=always` or a reboot. A rejected image left under `:latest` is a
  # deferred deployment of exactly the thing this check refused.
  REJECTED="${REPO}:rejected-$(date -u +%Y%m%dT%H%M%SZ)"
  if podman tag "$DIGEST_ID" "$REJECTED" 2>/dev/null; then
    log "rejected image kept as $REJECTED (evidence — do not delete before investigating)"
  fi

  if [ -n "$BEFORE_ID" ]; then
    if podman tag "$BEFORE_ID" "$IMAGE" 2>/dev/null; then
      log "restored $IMAGE to the last verified image (${BEFORE_ID:0:19}, digest ${BEFORE:-<unknown>})"
    else
      log "CRITICAL: could not restore $IMAGE to $BEFORE_ID — the tag still points at the REJECTED image. Do NOT restart $SERVICE. Fix with: podman tag $BEFORE_ID $IMAGE"
    fi
  else
    # No previously verified image exists, so there is nothing to restore to.
    # Untag rather than leave the rejected digest reachable as `:latest`.
    if podman untag "$IMAGE" 2>/dev/null; then
      log "no previously verified image existed; removed the $IMAGE tag so nothing can start it by accident"
    fi
  fi

  die "cosign verification FAILED for ${REPO}@${DIGEST}. Refusing to deploy. Nothing was restarted; investigate before removing $REJECTED."
fi

log "signature verified"

if [ "$VERIFY_ONLY" -eq 1 ]; then
  # Used by ops/README.md §2.4, before the first `podman run` of this image on a
  # new host. The gate only ever covered UPDATES; the first image a deployment
  # runs — to provision the application role with the OWNER's DSN, and to start
  # the service — went through no check at all.
  log "--verify-only: ${REPO}@${DIGEST} is signed by ${CERT_IDENTITY}. Nothing restarted."
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. Deploy the bytes that were verified — no registry involved
# ---------------------------------------------------------------------------
# NOT `podman auto-update`: that re-queries the registry and pulls again, which
# is the TOCTOU documented at the top of this file. `systemctl restart` starts
# the local `:latest`, which is what was just verified, because the generated
# ExecStart carries no `--pull` and podman's default policy is `missing`.
log "restarting $SERVICE onto ${REPO}@${DIGEST}"
systemctl --user restart "$SERVICE" \
  || die "restart of $SERVICE FAILED. The verified image is in the local store; check: journalctl --user -u $SERVICE -n 50"

# ---------------------------------------------------------------------------
# 4. Confirm it came back healthy
# ---------------------------------------------------------------------------
# This is what stands in for podman's automatic rollback. It does NOT roll back —
# it makes the failure visible, which is the thing rollback was actually buying
# here. Without it the script would exit 0 on a deploy that never came up, and
# `systemctl --user is-failed chaudron-verified-update.service` — the signal
# ops/README.md §5.1 asks you to monitor — would stay clean while the API was
# down. A gate that reports success on a broken deploy is worse than no gate.
log "waiting up to ${HEALTH_TIMEOUT}s for $CONTAINER to report healthy"

DEADLINE=$(($(date +%s) + HEALTH_TIMEOUT))
STATUS="unknown"
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS="$(podman inspect "$CONTAINER" --format '{{ .State.Health.Status }}' 2>/dev/null || echo unknown)"
  case "$STATUS" in
    healthy | unhealthy) break ;;
  esac
  sleep 5
done

if [ "$STATUS" != "healthy" ]; then
  die "deployed ${REPO}@${DIGEST} but $CONTAINER is '${STATUS}' after ${HEALTH_TIMEOUT}s. There is NO automatic rollback in this deployment — roll back by hand, ops/README.md §5.3. Previous verified digest: ${BEFORE:-<none>}"
fi

log "deployed ${REPO}@${DIGEST}; $CONTAINER is healthy"
log "done"
