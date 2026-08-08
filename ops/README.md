# Chaudron — Operations

Build, run and deploy Chaudron with **Podman**. There is no Docker anywhere in
this project: the container engine is Podman, images are built from
`Containerfile`s, and services run as rootless systemd **quadlets**.

Target platform: **Rocky Linux 10, SELinux Enforcing**, rootless Podman under a
dedicated unprivileged user.

---

## Contents

| File | Purpose |
| --- | --- |
| `chaudron.container` | Quadlet unit for the API |
| `chaudron-db.container` | Quadlet unit for PostgreSQL 16 |
| `chaudron-proxy.container` | Quadlet unit for Caddy — TLS, PWA hosting, API proxy |
| `chaudron-migrate.container` | One-shot Alembic runner, started by hand only |
| `Caddyfile.example` | Proxy configuration: routing, security headers, filtered logs |
| `verified-auto-update.sh` | Pull → `cosign verify` → `systemctl restart` (also `--verify-only`) |
| `chaudron-verified-update.{service,timer}` | Runs the above every 15 minutes |
| `chaudron-backup.sh` | `pg_dump` → `age` → off-machine `rsync`, with retention |
| `chaudron-backup.{service,timer}` | Runs the backup daily |
| `chaudron-restore-check.sh` | Restores the latest archive into a **disposable** PostgreSQL container |
| `chaudron-restore-check.{service,timer}` | Runs the restore drill weekly — **on the backup host**, see §8.6 |
| `chaudron-purge-credentials.container` | One-shot sweep of dead sessions and machine tokens |
| `chaudron-purge-credentials.timer` | Runs the sweep weekly — see §2.8 |
| `journald.conf.d/chaudron-retention.conf` | Journal retention — 30 days, capped |

`podman-auto-update.timer.d/override.conf` **was removed**. It made the
*unverified* update path fire every 15 minutes; that path is now masked and
replaced by `chaudron-verified-update.timer`. See §5.

`AutoUpdate=` is now absent from **every** quadlet, so `podman auto-update` has
no candidates at all. The masking is belt and braces rather than the only
control, and `verified-auto-update.sh` asserts it on every run. See §5.2.

---

## Deployment at a glance

```
                       internet
                          │  :80 / :443
                 ┌────────▼─────────┐
                 │  chaudron-proxy  │  Caddy — TLS, ACME, static /srv,
                 │   10.89.7.10     │  CSP + HSTS, 2 MiB body cap,
                 └────┬────────┬────┘  filtered access log
             /  /app/ │        │ /v1/ /caldav/ /healthz /readyz
        ┌────────────▼─┐   ┌───▼──────────┐
        │ frontend/dist│   │  chaudron    │  API, app-role DSN,
        │  (read-only) │   │              │  --no-access-log
        └──────────────┘   └───┬──────────┘
                               │ chaudron-net
                        ┌──────▼───────┐
                        │ chaudron-db  │  PostgreSQL 16, RLS in force
                        └──────┬───────┘
                               │ nightly
                    ┌──────────▼──────────┐
                    │ chaudron-backup     │  pg_dump | age → off-machine
                    └──────────┬──────────┘  (public recipient only)
                               │ rsync
                    ═══════════▼═══════════   ← a different machine
                    │  backup host          │  holds the archives AND the age
                    │  chaudron-restore-    │  PRIVATE identity; restores weekly
                    │  check.timer, weekly  │  into a disposable postgres
                    └───────────────────────┘  container (--network none)
```

Four containers on the application host, **two** timers there, and one on the
backup host. The API is not published at all — not to the internet, not on
loopback (§7.1); the database is not published either. The reverse proxy is the
only ingress.

Why the restore drill is on the other machine: it needs the age **private**
identity, and the whole point of encrypting to a public recipient is that the
application host cannot read its own backups. §8.6 has the argument and the
single-machine escape hatch.

---

## 1. Local development

### 1.1 Prerequisites

```sh
podman --version          # 5.x or later
systemctl --user status   # rootless systemd session must be available
getenforce                # expect: Enforcing
```

If a script must stay portable across engines, use
`${CONTAINER_ENGINE:-podman}` rather than hardcoding another engine.

### 1.2 Create the shared network

Both containers resolve each other by name on a user-defined network.

```sh
podman network create chaudron-net
```

### 1.3 Create the data directories

```sh
mkdir -p ~/chaudron/data/postgres
```

### 1.4 Start PostgreSQL 16

The password is passed through a Podman secret, never on the command line
(arguments are visible in `ps` and land in shell history):

```sh
read -rs -p 'Postgres password: ' PW && printf '%s' "$PW" | podman secret create chaudron-db-password - && unset PW
```

```sh
podman run -d --name chaudron-db \
  --network chaudron-net \
  --secret chaudron-db-password,type=env,target=POSTGRES_PASSWORD \
  -e POSTGRES_DB=chaudron \
  -e POSTGRES_USER=chaudron \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v ~/chaudron/data/postgres:/var/lib/postgresql/data:Z \
  --health-cmd 'pg_isready -U chaudron -d chaudron' \
  docker.io/library/postgres:16
```

> **`:Z` is not optional.** Under SELinux Enforcing, a bind mount without a
> container label is denied. `:Z` applies a **private** label (only this
> container may read the directory); `:z` applies a **shared** label. Use `:Z`
> unless two containers genuinely need the same directory.

Wait until it is healthy:

```sh
podman healthcheck run chaudron-db && podman inspect -f '{{ .State.Health.Status }}' chaudron-db
```

### 1.5 Build the API image

`HEALTHCHECK` is a Docker-format instruction. Podman's default OCI output
format drops it with a warning, so pass `--format docker` when you want the
baked-in healthcheck in the image. (Quadlet deployments declare `HealthCmd=`
themselves and do not depend on it.)

```sh
podman build --format docker -t localhost/chaudron-api:dev -f backend/Containerfile backend
```

### 1.6 Run the API

```sh
podman run -d --name chaudron \
  --network chaudron-net \
  -p 127.0.0.1:8000:8000 \
  --env-file ./.env \
  localhost/chaudron-api:dev
```

Check both endpoints — they are deliberately separate:

```sh
curl -fsS http://127.0.0.1:8000/healthz   # liveness: process is up, no dependency check
curl -fsS http://127.0.0.1:8000/readyz    # readiness: database reachable, migrations applied
```

A failing `/readyz` with a passing `/healthz` means the process is alive but a
dependency is down — do not restart the container, fix the dependency.

### 1.7 Tear down

```sh
podman rm -f chaudron chaudron-db
podman network rm chaudron-net
```

---

## 2. Deployment — rootless systemd quadlets

Quadlets are declarative container units that systemd generates services from.
They run under an unprivileged user account with lingering enabled, so services
survive logout and start at boot.

### 2.1 Prepare the service account

```sh
sudo useradd --create-home --shell /usr/sbin/nologin chaudron
sudo loginctl enable-linger chaudron
```

Everything below runs **as that user**:

```sh
sudo -u chaudron XDG_RUNTIME_DIR=/run/user/$(id -u chaudron) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u chaudron)/bus \
  systemctl --user <command>
```

Without `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`, `systemctl --user`
fails with *"Failed to connect to bus"*.

### 2.2 Install the units

```sh
install -d -m 0755 ~chaudron/.config/containers/systemd
install -m 0644 ops/chaudron.container ops/chaudron-db.container \
  ops/chaudron-proxy.container ops/chaudron-migrate.container \
  ~chaudron/.config/containers/systemd/
```

```sh
install -d -m 0755 ~chaudron/.config/systemd/user
install -m 0644 ops/chaudron-verified-update.service ops/chaudron-verified-update.timer \
  ops/chaudron-backup.service ops/chaudron-backup.timer \
  ops/chaudron-restore-check.service ops/chaudron-restore-check.timer \
  ~chaudron/.config/systemd/user/
install -d -m 0755 ~chaudron/chaudron/bin
install -m 0755 ops/verified-auto-update.sh ops/chaudron-backup.sh \
  ops/chaudron-restore-check.sh ~chaudron/chaudron/bin/
```

`chaudron-migrate.container` is installed but never enabled: it has no
`[Install]` section, so it only ever runs when somebody starts it.

Create the runtime state the units expect. **The subnet is not optional** —
`chaudron-proxy.container` pins itself to `10.89.7.10` on this network, and
`FORWARDED_ALLOW_IPS` in `chaudron.container` names that address literally:

```sh
podman network create --subnet 10.89.7.0/24 chaudron-net
mkdir -p ~/chaudron/data/postgres \
         ~/chaudron/data/caddy ~/chaudron/data/caddy-config \
         ~/chaudron/caddy ~/chaudron/www ~/chaudron/backups
chmod 0700 ~/chaudron/backups
```

> **Upgrading an existing instance?** A `chaudron-net` created without
> `--subnet` hands out addresses from a pool, so the proxy cannot be pinned.
> Stop the services, `podman network rm chaudron-net`, recreate it with the
> subnet above, and start them again. Nothing persistent lives on the network.

### 2.3 Create the secrets

Each command is a single line with masked input, transmitted over stdin, and
run **on the server as the `chaudron` user**. `printf '%s'` matters: `echo` and
`podman secret create <file>` both keep a trailing newline, which is invisible
until the value crosses an HTTP header or an HTML form and silently fails.

Nothing here is ever passed as a command-line argument: arguments are visible in
`ps` to every user on the machine and land in shell history.

**Every secret below is required.** A `Secret=` line in a quadlet is a hard
dependency — a missing one is a start-up failure, not a warning. Two of these
(`chaudron-credential-encryption-key` and the database DSNs) used to be declared
by the quadlets and created by nothing, so following this section end to end
produced a service that would not start. An operator stuck at that point
improvises, and the most natural improvisation is to put the value in
`chaudron.env` instead — which is exactly what the `Secret=` line existed to
prevent, and that file sits in `$HOME` next to the backups.

```sh
read -rs -p 'DB password (owner role): ' V && printf '%s' "$V" | podman secret create chaudron-db-password - && unset V
```

**Credential encryption key.** The most sensitive value in the system: it
decrypts every provider API key the households have entrusted to this instance.
Generate it with `openssl rand -base64 32`. Rotating it invalidates every stored
household credential. Back it up **separately from the database dumps** — see
§8.4, which is not optional reading.

```sh
read -rs -p 'Credential encryption key: ' V && printf '%s' "$V" | podman secret create chaudron-credential-encryption-key - && unset V
```

**Database DSNs — two of them, and they are different roles.** The migrator
needs the owner (a role that cannot create tables cannot run a migration); the
API must *not* be the owner, or row-level security silently enforces nothing.
§6 explains why in full. The names carry `-owner` and `-app` so the mistake has
to be typed out.

Create the owner DSN now; the app DSN comes in §2.5, once the role it names
exists.

```sh
read -rs -p 'Owner DSN (postgresql+asyncpg://chaudron:PW@chaudron-db:5432/chaudron): ' V && printf '%s' "$V" | podman secret create chaudron-database-url-owner - && unset V
```

```sh
read -rs -p 'App secret key: ' V && printf '%s' "$V" | podman secret create chaudron-secret-key - && unset V
```

```sh
read -rs -p 'Inbound email webhook key: ' V && printf '%s' "$V" | podman secret create chaudron-inbound-email-key - && unset V
```

```sh
read -rs -p 'Anthropic API key: ' V && printf '%s' "$V" | podman secret create chaudron-anthropic-api-key - && unset V
```

Check the set against what the quadlets declare — the comparison that catches a
typo before a failed start does:

```sh
diff <(podman secret ls --format '{{.Name}}' | sort) \
     <(grep -ho '^Secret=[^,]*' ops/*.container | cut -d= -f2 | sort -u)
```

### 2.4 Non-secret configuration

Copy `.env.example` to `~chaudron/chaudron.env`, fill in the non-secret keys, and
restrict its permissions. `EnvironmentFile=` in `chaudron.container` points at it.

```sh
install -m 0600 /dev/null ~chaudron/chaudron.env
```

**Nothing secret goes in this file.** It is a plain file in `$HOME`, in the same
directory tree as the backups; a leaked backup that also carried this file would
hand over the key that decrypts everything. That is the whole reason the values
in §2.3 are Podman secrets and not lines here.

#### 2.4.1 Verify the image — required, before it runs for the first time

**Do this before any command in §2.5 or §2.6.** It is not optional and it is not
a formality.

The signature gate in §5 covers **updates**. It compares the digest it pulls
against the previous one and only acts when the tag has moved — so on a fresh
host, where there is no previous digest, the first image was never checked by
anything. That first image is not a small exposure:

- §2.5 runs it with `podman run`, holding the **owner's DSN** — the role that
  owns every table and is exempt from every row-level-security policy;
- §2.6 starts it as the service;
- `chaudron-migrate.container` runs it against the schema, and it carries no
  `AutoUpdate=` and never did, so no gate has ever seen it.

Every one of those happened *before* the timer in §2.6 armed the gate. Verifying
here closes the gap, and the same script the timer runs does it — one
implementation, so the two cannot drift:

```sh
~chaudron/chaudron/bin/verified-auto-update.sh --verify-only
```

It pulls `:latest`, verifies the digest against the pinned workflow identity, and
exits without restarting anything. A non-zero exit means **stop**: do not
continue to §2.5, and read §5.2.

By hand, if the script is not installed yet:

```sh
podman pull ghcr.io/claravnk/chaudron:latest
DIGEST=$(podman image inspect ghcr.io/claravnk/chaudron:latest --format '{{ .Digest }}')
cosign verify "ghcr.io/claravnk/chaudron@${DIGEST}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity \
    'https://github.com/ClaraVnk/chaudron/.github/workflows/publish.yml@refs/heads/main' \
  | jq '.[].optional'
```

Once this passes, the local `:latest` holds verified bytes — and because every
quadlet uses Podman's default pull policy of `missing`, that is what §2.5, §2.6
and `chaudron-migrate.service` will all run. The gate keeps it that way: on a
failed verification it puts the `:latest` tag back on the last verified digest
(§5.2), so nothing can start rejected bytes later by accident.

### 2.5 Provision the application role — not optional

> **§2.4.1 first.** The `podman run` below hands this image the **owner's**
> DSN. Verify the digest before giving unverified bytes the most privileged
> credential in the system.

Row-level security is in force only when the API connects as a role that does
**not** own the tables. PostgreSQL exempts an owner from every policy, and
migration `0004` deliberately does not set `FORCE ROW LEVEL SECURITY` (§6
explains why). An instance that skips this step passes every health check,
answers every endpoint, and lets every household read every other household's
rows — with no symptom of its own.

First apply the schema, as the owner:

```sh
systemctl --user daemon-reload
systemctl --user start chaudron-db.service
systemctl --user start chaudron-migrate.service
journalctl --user -u chaudron-migrate.service -n 50
```

Then create the role. The runtime image ships `provision_app_role.py`, so this
is the same code path CI and a checkout use — there is no second copy of the
procedure to drift out of step:

```sh
read -rs -p 'App role password: ' CHAUDRON_DB_APP_PASSWORD && export CHAUDRON_DB_APP_PASSWORD
podman run --rm --network chaudron-net \
  --env-file ~/chaudron/chaudron.env \
  --env CHAUDRON_DB_APP_PASSWORD \
  --secret chaudron-database-url-owner,type=env,target=CHAUDRON_DATABASE_URL \
  ghcr.io/claravnk/chaudron:latest python /app/scripts/provision_app_role.py
unset CHAUDRON_DB_APP_PASSWORD
```

The script is idempotent, grants what is missing, sets the password when one is
supplied, and then checks its own work. Finally, give the API its own DSN —
naming `chaudron_app`, not `chaudron`:

```sh
read -rs -p 'App DSN (postgresql+asyncpg://chaudron_app:PW@chaudron-db:5432/chaudron): ' V && printf '%s' "$V" | podman secret create chaudron-database-url-app - && unset V
```

Verify before moving on. This exits non-zero when anything is wrong, and it is
the command to run after **every** migration:

```sh
podman run --rm --network chaudron-net \
  --env-file ~/chaudron/chaudron.env \
  --secret chaudron-database-url-owner,type=env,target=CHAUDRON_DATABASE_URL \
  ghcr.io/claravnk/chaudron:latest python /app/scripts/provision_app_role.py --check
```

> **Migration `0014` makes re-running this mandatory, not merely advisable.**
> Two `SECURITY DEFINER` functions cross row-level security by construction —
> `chaudron_user_memberships` and `chaudron_resolve_machine_token` — and both were
> executable by `PUBLIC`, which is PostgreSQL's default for a function nobody
> grants explicitly. Not exploitable while `chaudron_app` was the only non-owner
> login; exploitable the moment a second role exists, because it would inherit the
> right to read the whole membership map past every policy, with no ACL entry for
> a review to notice.
>
> `0014` revokes `PUBLIC`. The matching `GRANT`s live in
> `provision_app_role.py`, so the migration and this script are **one change**. A
> database upgraded without re-running it leaves the API unable to resolve a
> session or a machine token: every request answers `401`, and nothing in the logs
> says why. `--check` reports exactly that, by name.

### 2.6 Start

> **§2.4.1 first**, if you have not run it: this starts the image as a service.

```sh
systemctl --user daemon-reload
systemctl --user start chaudron-db.service
systemctl --user start chaudron.service
systemctl --user status chaudron.service
journalctl --user -u chaudron.service -f
```

The API is **not published on the host**. `chaudron.container` carries no
`PublishPort`, deliberately — see the comment in that file and §7.1. To reach it
for debugging, go through the proxy or into the network:

```sh
podman exec chaudron python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/healthz").read())'
podman run --rm --network chaudron-net docker.io/library/caddy:2-alpine wget -qO- http://chaudron:8000/readyz
```

Quadlet units are **not** enabled with `systemctl enable`. The generator reads
`[Install] WantedBy=default.target` from the `.container` file, so a
`daemon-reload` is all that is required.

The proxy comes next — see §7, which also covers the one sysctl a rootless
container needs before it can bind :80 and :443.

Then the timers:

```sh
systemctl --user mask podman-auto-update.timer          # see §5
systemctl --user enable --now chaudron-verified-update.timer
systemctl --user enable --now chaudron-backup.timer     # see §8 first
systemctl --user list-timers
```

**`chaudron-restore-check.timer` is deliberately not in that list**, and it does
not belong on this machine. It needs the age **private** identity; this host is
the one whose compromise the encrypted backups exist to survive. Install it on
the backup host — §8.6 has the argument, the topology, and the escape hatch for
a single-machine deployment. The script refuses to run here by default.

Masking `podman-auto-update.timer` above is now the second of two controls
rather than the only one — no quadlet carries `AutoUpdate=` any more — and
`verified-auto-update.sh` asserts it on every run rather than trusting that this
line was typed. Confirm:

```sh
systemctl --user is-enabled podman-auto-update.timer    # expect: masked (or not-found)
```

### 2.7 Update to a new image

Normally there is nothing to do: `chaudron-verified-update.timer` pulls,
verifies the signature and applies the update within 15 minutes (§5). To force
it:

```sh
systemctl --user start chaudron-verified-update.service
journalctl --user -u chaudron-verified-update.service -n 30
```

`podman auto-update` is no longer part of this path at all, and no quadlet
carries `AutoUpdate=` for it to act on. If you find yourself typing it, §5.2
explains why it was removed. Rolling back is §5.3 — and it is now a **manual**
step, because Podman's automatic rollback went with `podman auto-update`.

### 2.8 Sweep dead credentials — weekly

`user_session` is read on **every authenticated request**, and until this timer
existed nothing had ever removed a row from it. Neither had anything removed a
revoked or expired `machine_token`. A browser that signs in and out ten times a
day leaves ten dead rows behind it; the index lookup does not care much, but
every backup, every restore drill and every `VACUUM` does — for rows that
authenticate nobody.

Look before you delete. A first run should be a count:

```sh
podman run --rm --network chaudron-net \
  --env-file ~/chaudron/chaudron.env \
  --secret chaudron-database-url-owner,type=env,target=CHAUDRON_DATABASE_URL \
  ghcr.io/claravnk/chaudron:latest \
  python /app/scripts/purge_expired_credentials.py --dry-run
```

Then install and enable the timer:

```sh
systemctl --user daemon-reload
systemctl --user enable --now chaudron-purge-credentials.timer
systemctl --user list-timers chaudron-purge-credentials.timer
```

Three things about it are decisions rather than defaults.

**It takes the owner's DSN**, like `chaudron-migrate.container` and unlike
everything else. `machine_token` carries row-level security keyed on a
transaction-local tenant (migration `0011`), and a sweep is by definition not
scoped to one household — the application role would post no tenant, match no
rows, delete nothing, and exit `0`. A cleanup job that silently does nothing is
worse than none.

**Retention is thirty days, and not zero.** These rows are the only record of
when a session ended and whose it was, which is the first thing an incident
review reads. Thirty days matches `CHAUDRON_SESSION_ABSOLUTE_TTL_HOURS`, so the
record never outlives the credential by less than the credential's own lifetime.

**Nothing live is ever selected.** The predicates match only rows the API already
refuses — revoked, past their absolute expiry, past their idle deadline — and a
machine token with no expiry at all is never swept however old it is: an
appliance polling on a credential issued years ago is the documented use, and
"old" is not "dead". `backend/tests/test_credential_purge.py` asserts each of
those negatives before it asserts that anything is deleted.

---

## 3. SELinux checklist

Do **not** run `setenforce 0`. Every problem below has a targeted fix.

| Symptom | Check | Fix |
| --- | --- | --- |
| Container cannot read/write a bind mount | `ls -Z ~/chaudron/data/postgres` | Add `:Z` to the `Volume=` line, or `restorecon -Rv <path>` |
| Denials with no obvious cause | `sudo ausearch -m AVC -ts recent` | Feed the output to `audit2why` before changing anything |
| Service binds a non-standard port | `sudo semanage port -l \| grep <port>` | `sudo semanage port -a -t http_port_t -p tcp <port>` |
| Reverse proxy cannot reach the API | `getsebool httpd_can_network_connect` | `sudo setsebool -P httpd_can_network_connect on` |

Useful one-liners:

```sh
getenforce
ls -Z ~/chaudron/data
ps -eZ | grep chaudron
sudo ausearch -m AVC -ts recent | audit2why
```

### The `:U` trap

Never add `:U` to a volume in a quadlet. It chowns the directory to the
container's **declared** user at start time, not the runtime one — the first
start works and every subsequent start breaks with permission errors. Set
ownership on the host once instead, running the `chown` *inside* the user
namespace so that the uid you name is the container-side one:

```sh
podman unshare chown -R 10001:10001 ~/chaudron/data/<directory>
```

This is written as guidance rather than a step because **`chaudron.container`
declares no volume at all** — the API container holds no host state, and the one
mount it used to carry (`data/uploads`, for receipt images) went when revision
`0012` stopped retaining the image. `10001` is the uid that image runs as, and
is the number you would need if a writable mount ever became necessary. The
database and the proxy keep their own volumes, but neither needs this: the
`postgres` and `caddy` entrypoints start as root and fix their own ownership.

---

## 4. Logging, retention, and what is deliberately not logged

Backups have moved to §8, where they are a timer rather than two commands to
remember.

### 4.1 The problem this section fixes

Podman's default log driver is **journald**, and no quadlet used to say
otherwise. So container output was already being persisted to disk,
indefinitely, by a decision nobody made.

What was in those lines is what made it matter. uvicorn's access log records the
request line, and in this application that means:

| In the log line | Where it comes from |
| --- | --- |
| A scanned product's GTIN | `/v1/products?gtin=…` — a query parameter |
| A calendar feed identifier | `/caldav/p/<feed_id>/…` — **in the path**, and it *is* the credential for that feed |
| `household_id` | `infra/logging.py` reparents `uvicorn.access` onto the application formatter, which adds it to every line |

None of it was transient, and the consequence is specific: a `household_id`
stayed correlatable in the journal **after** an erasure exercised under article
17 GDPR, because the erasure acts on the database and nothing points it at the
journal. "Retention" was "until the disk fills", which is not a period.

### 4.2 What is in place now

Three changes, at three layers:

1. **The API does not write an access log at all.** `backend/Containerfile`
   passes `--no-access-log`. No filtering to get subtly wrong, and the
   application's own structured logs are untouched.
2. **The proxy keeps a filtered one.** Turning the API's log off and leaving
   Caddy's at its default would have moved the same data one container to the
   left. `ops/Caddyfile.example` drops the identifier-bearing query parameters
   by name, masks client IPs to /24 and /48, deletes `Authorization`, `Cookie`
   and `X-Household-Id` — and skips `/caldav/*` entirely, because the feed
   identifier is in the path and no field filter can redact a path.
3. **journald has a retention policy.** 30 days, capped at 2 GB, rotating
   weekly so the age bound has a boundary to act on.

Every quadlet now states `LogDriver=journald` explicitly. That changes nothing
about behaviour — it was already the default — but it makes the sink a visible
choice rather than an inherited one, which is the difference between a policy
and an accident.

### 4.3 Installing the retention policy

This one is **root-level and system-wide**: it applies to every service on the
host, not only Chaudron's. Read it before installing, and check what else runs
here.

```sh
sudo install -d -m 0755 /etc/systemd/journald.conf.d
sudo install -m 0644 ops/journald.conf.d/chaudron-retention.conf /etc/systemd/journald.conf.d/
sudo systemctl restart systemd-journald
```

Verify:

```sh
journalctl --disk-usage
sudo journalctl --header | grep -iE 'retention|max'
```

### 4.4 What is still not covered

- **Nothing forwards logs anywhere.** No external sink is configured, which is
  the reason the journal's retention is the whole retention story. Adding one
  later means giving that sink its own retention and its own place in an
  erasure procedure — a log shipper is a second copy of everything above.
- **Application logs may still carry identifiers.** `--no-access-log` removes
  the *access* log; a `logger.info` that includes a household or a product is
  the developer's responsibility. `CHAUDRON_LOG_LEVEL=DEBUG` is refused at
  startup in production for exactly this reason — SQLAlchemy would then write
  every statement with its bound parameters, allergens and infant age bands
  included.
- **journald is not an audit log.** It is operational output with a 30-day
  ceiling. Anything that needs to survive longer, or to be tamper-evident,
  belongs in the database with a retention policy of its own.

---

## 5. Continuous deployment

Production follows `ghcr.io/claravnk/chaudron:latest`. A merge to `main` reaches
the server within 15 minutes, unattended.

### How the chain fits together

```
merge to main → CI passes → publish.yml checks the commit is on main
                                        ↓
                        builds, pushes :latest, signs the digest (cosign)
                                        ↓  (≤ 15 min)
              chaudron-verified-update.timer fires
                                        ↓
              podman pull → digest re-read from the LOCAL store
                                        ↓
              cosign verify that exact digest, PINNED identity
                                        ↓
              FAILS → :latest tag restored to the last verified digest,
                      rejected image kept as :rejected-<ts> for evidence,
                      nothing restarted, unit goes to failed
                                        ↓
              passes → systemctl --user restart chaudron.service
                       (starts the LOCAL image: pull policy is `missing`)
                                        ↓
              wait for the container to report `healthy`
                                        ↓
              never healthy → unit FAILS. No automatic rollback: §5.3
```

Only the API is in this loop, and it is in it because a **timer** names it, not
because a label marks it. No quadlet carries `AutoUpdate=` any more — see §5.2
for why it was removed from `chaudron.container` too. The database and the proxy
were never in the loop and still are not: a database restarting itself at a
moment nobody chose, with no backup taken first, is not a trade worth making for
a component that cannot roll its own state back, and a proxy that replaces itself
unattended can take the instance offline including the ACME path that would let
you notice.

**What this deployment does not have: automatic rollback.** Read §5.2 before
relying on the old sentence about it, which was true of `podman auto-update` and
is not true of what replaced it. The substitutes are `Restart=always` on the
unit, a post-restart health wait that fails the update job loudly, and §5.3 by
hand.

### 5.1 Installing the loop

```sh
# 1. Mask the stock timer. NOT optional — see below.
systemctl --user mask podman-auto-update.timer

# 2. Enable the gated one.
systemctl --user daemon-reload
systemctl --user enable --now chaudron-verified-update.timer
systemctl --user list-timers chaudron-verified-update.timer

# 3. Confirm the mask actually took.
systemctl --user is-enabled podman-auto-update.timer    # expect: masked
```

Masking is not tidiness. It used to be the *only* thing standing between the
stock timer and an ungated deploy, because `chaudron.container` carried
`AutoUpdate=registry` and both timers act on that label — so leaving the stock
one enabled meant the ungated path still existed and would win roughly half the
races, deploying exactly the digests the new timer exists to refuse.
`ops/podman-auto-update.timer.d/override.conf` was deleted from this repository
for the same reason: all it ever did was make the unverified path fire more
often.

That label is now gone from every quadlet (§5.2), so `podman auto-update` has
nothing to act on and the ungated path does not merely go unused — it stops
existing. Masking remains as the second of two independent controls, and it is
**asserted rather than trusted**: `verified-auto-update.sh` refuses to run while
`podman-auto-update.timer` is anything but masked or absent. A command buried in
a seven-item checklist is a command that gets skipped, and skipping this one used
to produce no symptom at all.

Force a check without waiting:

```sh
systemctl --user start chaudron-verified-update.service
journalctl --user -u chaudron-verified-update.service -n 30
```

**Monitor this.** It is the compensating control for the rollback that no longer
happens automatically (§5.2), and it is worth nothing unless something reads it:

```sh
systemctl --user is-failed chaudron-verified-update.service   # expect: inactive
```

Keep the session alive across logouts, or the timers die with it:

```sh
loginctl enable-linger "$USER"
```

### 5.2 Image provenance — what `:latest` does and does not guarantee

Read this before deciding that continuous deployment is acceptable here.

`AutoUpdate=registry` follows a **mutable tag**. Every 15 minutes the host asks
GHCR what digest `ghcr.io/claravnk/chaudron:latest` points at. Until the gate
described below existed, it then ran whatever came back without asking who put
it there — so anyone able to push that tag (a stolen `packages: write` token, a
compromised maintainer account, a workflow tricked into building somebody else's
commit) owned the production container within a quarter of an hour, with no
human in the loop. Remote code execution with patience.

Three things narrow that, and it is worth being precise about which is which.

**What is closed.** `publish.yml` refuses to build anything that is not a commit
already reachable from `refs/heads/main` in this repository, verified against
the repository itself rather than against the event payload. A pull request from
a fork — including one whose branch is called `main`, which used to satisfy the
`workflow_run` branch filter — never reaches the build step.

**What is now provable.** Each published digest is signed with cosign in keyless
mode: the runner exchanges its GitHub OIDC token for a short-lived Fulcio
certificate, and the signature is logged in Rekor. No signing key exists to be
stolen. The certificate records *which workflow, in which repository, on which
ref* produced the image, so a signature cannot be forged by someone who merely
holds a registry token.

Verify a digest before trusting it:

```sh
podman pull ghcr.io/claravnk/chaudron:latest
DIGEST=$(podman image inspect ghcr.io/claravnk/chaudron:latest --format '{{ .Digest }}')

cosign verify "ghcr.io/claravnk/chaudron@${DIGEST}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity \
    'https://github.com/ClaraVnk/chaudron/.github/workflows/publish.yml@refs/heads/main' \
  | jq '.[].optional'
```

The `--certificate-identity` argument is the control. Dropping it, or replacing
it with `--certificate-identity-regexp '.*'`, verifies only that *somebody*
signed the image — which is worth nothing, since anybody can sign anything.

**What is now enforced — and how, given that Podman cannot do it directly.**

Podman *can* enforce sigstore signatures through `/etc/containers/policy.json`
(`"type": "sigstoreSigned"`), and for a plain key pair (`keyPath`) it works
well. For a *keyless* GitHub Actions identity it does not: the `fulcio` stanza
matches the signer by `subjectEmail`, and a GitHub Actions certificate carries
no email — its identity is a URI SAN
(`https://github.com/…/publish.yml@refs/heads/main`). There is no field in
`containers-policy.json(5)` that matches a URI SAN, so the policy cannot express
"signed by this workflow". Checked against `podman 5.6.0` on Rocky 10.

That limitation is why the gate is a host-side unit rather than a policy file.
`chaudron-verified-update.timer` runs `ops/verified-auto-update.sh`, which:

1. asserts `podman-auto-update.timer` is masked, and refuses to run otherwise;
2. `podman pull`s the tag;
3. re-resolves the digest **from the local image store**, not from the registry
   — that is the digest of the bytes on this disk, and the bytes on this disk are
   what will be deployed;
4. exits 0 and does nothing further if the digest has not moved;
5. runs `cosign verify` against the pinned `--certificate-identity` and
   `--certificate-oidc-issuer`;
6. **`systemctl --user restart chaudron.service`** — only if that succeeded;
7. waits for the container to report `healthy`, and fails the unit if it does
   not.

#### The window this used to leave open, and why step 6 is not `podman auto-update`

Step 6 used to be `podman auto-update`, under a comment in both this file and the
script asserting the pull-to-deploy window was closed because the digest had been
re-resolved locally. **That was wrong**, and `podman-auto-update(1)` on this host
says so directly:

> *registry: … Podman reaches out to the corresponding registry to check if the
> image has been updated. An image is considered updated if the digest in the
> local storage is different than the one of the remote image. If an image must
> be updated, Podman pulls it down and restarts the systemd unit.*

`podman auto-update` **queries the registry and pulls the image itself**. It
never looked at the digest the script had just verified. So:

1. the script pulls D1 and starts `cosign verify` — one to two seconds, because
   the Fulcio and Rekor round trips are on the network;
2. an attacker holding the tag pushes D2 inside that window, timed off the
   script's own pull, which is observable from the registry side;
3. verification of D1 succeeds;
4. `podman auto-update` asks the registry, sees D2 ≠ D0, pulls **D2**, restarts.

D2 was verified by nothing. The gate ran, passed, and deployed the other image —
and the verification round trip is what made the window wide enough to aim at.

**How it is closed:** by never asking the registry a second time. After
verification the bytes are already on this disk under `:latest`, and a restart
uses them:

- quadlet emits no `--pull` flag for `chaudron.service` — check it:
  `QUADLET_UNIT_DIRS=ops /usr/libexec/podman/quadlet -dryrun -user | grep ExecStart`
- `podman-run(1)`: *"--pull=policy … The default is missing"* — pull only when
  the image is absent locally. It is present.

`AutoUpdate=registry` was removed from `chaudron.container` at the same time.
Keeping it while simply not calling `auto-update` would have left the ungated
path available to a stray command or an unmasked timer; removing it means
`podman auto-update` has no candidates at all.

#### What that costs: there is no automatic rollback any more

`podman auto-update --rollback` defaults to true and restored the previous image
when the restarted unit failed. A plain `systemctl restart` has no such
behaviour, and nothing in this repository reimplements it. **State this to
whoever operates the instance**; discovering it during an incident is the
expensive way.

What stands in its place, in order:

| | |
| --- | --- |
| `Restart=always`, `RestartSec=5` | in `chaudron.container` — a container that starts and then dies is restarted rather than left down |
| Post-restart health wait | the gate waits up to `CHAUDRON_HEALTH_TIMEOUT` (180 s) for `healthy` and **fails the unit** otherwise. Without it the script would exit 0 on a deploy that never came up |
| `systemctl --user is-failed chaudron-verified-update.service` | the signal to monitor. This is the compensating control, and it only works because of the row above |
| §5.3 | rolling back, by hand |

A gate that fails silently every 15 minutes is a gate nobody reads. Wire the
`is-failed` check into whatever monitoring exists — it now catches three distinct
failures that all used to be invisible: a rejected signature, a restart that did
not happen, and an image that started and never became healthy.

#### A rejected image does not stay under `:latest`

This matters more than it did, and it is a direct consequence of deploying from
the local store. With pull policy `missing`, whatever `:latest` points at
**locally** is what the next start of `chaudron.service` runs — including a start
nobody asked for, from `Restart=always` or a reboot. Leaving a rejected image
under that tag would deploy it later, by accident.

So on a verification failure the script:

- tags the rejected digest `ghcr.io/claravnk/chaudron:rejected-<timestamp>` and
  **keeps** it — it is the evidence, and deleting it turns an incident into an
  argument about what was actually pulled;
- moves `:latest` back onto the previously verified digest, or removes the tag
  entirely if this host has never held a verified one.

Treat it as an incident:

```sh
systemctl --user is-failed chaudron-verified-update.service
journalctl --user -u chaudron-verified-update.service -n 50
podman images ghcr.io/claravnk/chaudron        # look for a :rejected-* tag
```

The `--certificate-identity` argument is the whole control. Dropping it, or
replacing it with `--certificate-identity-regexp '.*'`, verifies only that
*somebody* signed the image — worth nothing, since anybody can sign anything.
`@refs/heads/main` in particular is what stops a signature produced by the same
workflow on a branch or a tag from satisfying the check.

**cosign must be installed on the deployment host.** The script checks for it
and refuses to deploy anything when it is missing, distinguishing "the
gate is not installed" from "the image is bad" — both stop the update, and
telling them apart is the difference between a five-minute fix and an incident
review. It is not packaged in Rocky 10's default repositories; install the
released binary and verify its checksum:

```sh
COSIGN_VERSION=v2.6.1
curl -fsSLO "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-amd64"
curl -fsSLO "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign_checksums.txt"
sha256sum --check --ignore-missing cosign_checksums.txt
sudo install -m 0755 cosign-linux-amd64 /usr/local/bin/cosign
cosign version
```

(Pin `COSIGN_VERSION` deliberately and bump it deliberately, the same trade
`chaudron-db.container` documents for `postgres:16`. Check the current release
before copying the value above.)

**Two alternatives were considered and rejected**, and it is worth recording
why rather than leaving them to be rediscovered:

- **Sign with a key pair instead of keyless**, and enforce through
  `policy.json`. Architecturally cleaner — Podman refuses a wrongly-signed image
  at pull time, no host-side scripting. The cost is a long-lived private key in
  a GitHub secret, which is precisely the thing keyless signing exists to avoid,
  plus a rotation procedure nobody has written.
- **Stop following a mutable tag.** Point the quadlet at
  `ghcr.io/claravnk/chaudron@sha256:…` and drop `AutoUpdate=registry`. This is
  the only option that removes the unattended path entirely, and it is the right
  answer the day this instance has users who would notice a bad deploy. Today it
  trades the 15-minute loop for a manual step on every release.

See [ADR-0011](../docs/adr/0011-tls-termination-and-static-hosting.md) for the
full comparison.

**What the gate still does not answer.** It proves an image came from this
repository's `main` workflow. It says nothing about whether the change is
correct: signing proves origin, never intent, and a legitimately signed image
built from a compromised `main` verifies perfectly. Branch protection on `main`
is the control for that one, and it lives on GitHub, not in `ops/`.

To check a digest by hand at any time:

```sh
DIGEST=$(podman image inspect ghcr.io/claravnk/chaudron:latest --format '{{ .Digest }}')
cosign verify "ghcr.io/claravnk/chaudron@${DIGEST}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity \
    'https://github.com/ClaraVnk/chaudron/.github/workflows/publish.yml@refs/heads/main' \
  | jq '.[].optional'
```

### 5.3 Rolling back

**Rolling back is manual in this deployment.** Nothing reverts an image on its
own — see §5.2 for what was traded away and what replaced it. What the gate does
give you is that a bad deploy is *reported*: the update unit fails when the new
container never becomes healthy, so the trigger for this section is
`systemctl --user is-failed chaudron-verified-update.service`, not a user
complaint.

Republish a known-good commit as `:latest`:

```sh
gh workflow run publish.yml --ref main -f ref=<good-sha>
```

`--ref main` is not optional. `workflow_dispatch` lets you choose the branch the
workflow *runs from*, and keyless signing records that branch in the certificate
(`…/publish.yml@refs/heads/<branch>`). The deployment host pins
`@refs/heads/main`, so a dispatch from any other branch produces a valid
signature this host is built to **refuse** — after `:latest` has already moved,
leaving production stuck on a digest it will not deploy and failing every 15
minutes. `publish.yml` now fails the job outright in that case rather than
publishing first and letting the host discover it.

The `ref` **input** is a different thing: a commit SHA (7–40 hex characters) that
is **already reachable from `main`**; the workflow verifies this and refuses
anything else, tags included. A tag can be moved, so it is not an acceptable
description of "the code I reviewed".

Then either wait for the timer or start `chaudron-verified-update.service`. To
stop the bleeding first, pin the service to the previous image and stop the
timer — note the immutable tag is the **first 12 characters** of the commit SHA,
which is what `publish.yml` tags and pushes:

```sh
systemctl --user stop chaudron-verified-update.timer
podman pull ghcr.io/claravnk/chaudron:<good-sha-12>          # if not already local
podman tag ghcr.io/claravnk/chaudron:<good-sha-12> ghcr.io/claravnk/chaudron:latest
systemctl --user restart chaudron.service
```

That retag-then-restart sequence is exactly what the gate itself does on a
successful verification, and it works for the same reason: the generated
`ExecStart` carries no `--pull`, so a restart runs the local `:latest` and never
consults the registry.

Verify that known-good digest before pinning to it — a rollback is not a reason
to skip the check, and `podman tag` does not run one:

```sh
cosign verify "ghcr.io/claravnk/chaudron:<good-sha-12>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity \
    'https://github.com/ClaraVnk/chaudron/.github/workflows/publish.yml@refs/heads/main'
```

Remember to start the timer again once the incident is closed. A stopped update
timer is a machine that stops receiving security fixes, and nothing will remind
you.

### 5.4 Migrations are deliberately outside this loop

`chaudron-migrate.container` has no `AutoUpdate=` and no `[Install]` section: it
never runs on its own. That is the point.

If migrations ran in the API entrypoint, this pipeline would apply schema changes
to production with nobody watching — and Podman's rollback would become actively
misleading, because it restores the previous *image* and cannot restore the
previous *schema*. Old code against a new database is usually worse than the
failure it was undoing.

`chaudron-migrate.container` never carried `AutoUpdate=`, which used to mean it
sat outside the signature gate entirely: it runs the same `:latest` image, against
the schema, as the **owner** role. It is covered now, and not by a second check —
by the fact that the gate deploys from the local store. `:latest` in local storage
is either a verified digest or, after a rejection, has been put back onto the last
verified one (§5.2). Pull policy `missing` means this unit runs whatever that tag
holds, so it inherits the gate's guarantee rather than needing its own.

The exception is a **fresh host**, where the gate has never run. That is what
§2.4.1 exists for, and it is why it comes before §2.5.

For a release carrying a migration:

```sh
systemctl --user start chaudron-migrate.service
journalctl --user -u chaudron-migrate.service -n 50     # read it before continuing
systemctl --user start chaudron-verified-update.service # then let the API roll
```

Then re-check the application role. A migration creates tables the app role was
never granted on, and `ALTER DEFAULT PRIVILEGES` only covers tables created
*after* provisioning:

```sh
podman run --rm --network chaudron-net \
  --env-file ~/chaudron/chaudron.env \
  --secret chaudron-database-url-owner,type=env,target=CHAUDRON_DATABASE_URL \
  ghcr.io/claravnk/chaudron:latest python /app/scripts/provision_app_role.py --check
```

Because both versions coexist for a few minutes, **every migration must be
backward compatible with the code currently running**. Expand, migrate, contract:
add columns in one release, remove them in a later one, never both at once.

### 5.5 What this cadence costs

- 96 registry polls per day per image. GHCR absorbs that without complaint.
  `cosign verify` only runs on the days a digest actually moved, so a quiet day
  costs nothing beyond the pulls.
- **No human between a merged pull request and production.** The signature gate
  answers "is this ours?"; the health check answers "does it start?". Nothing
  answers "is it right?". If that trade stops being acceptable — a second
  maintainer, real users — put a GitHub Environment with a required reviewer in
  front of `publish.yml` rather than lengthening the timer, which only makes the
  same unreviewed change arrive later.
- **`cosign` is now a hard dependency of the host.** A missing binary freezes
  deployments until somebody reads the unit status. That is the safe direction,
  and it is still a thing that has to be noticed.
- **No automatic rollback.** `podman auto-update --rollback` left the path along
  with `podman auto-update` (§5.2). A deploy that starts and never becomes
  healthy now fails the update unit and stays up-front, rather than being
  reverted while nobody looks. That is a deliberate trade of *silent recovery*
  for *loud failure*, and it is only the right trade if the `is-failed` check is
  actually monitored. If it is not, this is worse than what it replaced.
- **Up to ~3 minutes per deploy waiting on the health check.** The gate blocks
  until the container reports `healthy` (`CHAUDRON_HEALTH_TIMEOUT`, 180 s). That
  is what makes the failure visible; it also means a stuck deploy occupies the
  timer's slot rather than returning immediately.

---

## 6. Row-level security

Since migration `0004`, PostgreSQL itself refuses to return one household's rows
to another. Thirteen tables carry a policy; a connection that has not named a
household reads **nothing** from any of them. This closes the engine half of
SEC-001 / AUD-002 — until then, isolation rested entirely on every query
remembering its `WHERE household_id`, which is a property that degrades in
silence.

**The whole guarantee reduces to one sentence: the API must not connect as the
role that owns the tables.** PostgreSQL exempts a table's owner from its own
policies unless `FORCE ROW LEVEL SECURITY` is set, and this schema deliberately
does not set it — the owner is Alembic, `scripts/seed.py` and your psql prompt,
and forcing policies on them would mean every maintenance statement had to post
a tenant first. So an instance whose `CHAUDRON_DATABASE_URL` still names the
owner passes every health check, answers every endpoint, and enforces nothing.

### 6.1 Provisioning the role

**For a new deployment this is §2.5, and it is a required step of the install.**
The quadlet's default is the application DSN
(`Secret=chaudron-database-url-app`), so an instance that has not done this does
not start — which is the point. It used to be the other way round: both the API
and the migrator received the same `chaudron-database-url` secret, naming the
role `POSTGRES_USER=chaudron` that owns the database, and the separate
application role existed only at the end of a four-step manual procedure that
asked the operator to **hand-edit the installed quadlet**. The shipped default
disabled RLS, and enabling it was opt-in. That is now inverted.

This section is for an instance that is **already running** on the owner DSN.

```sh
# 1. Apply the migration (as the owner, from the migration runner)
systemctl --user start chaudron-migrate.service
journalctl --user -u chaudron-migrate.service | tail -20
```

```sh
# 2. Rename the existing secret so the migrator keeps the owner DSN under its
#    new name. Podman secrets are immutable, so this is re-create, not rename.
podman secret inspect chaudron-database-url --showsecret --format '{{.SecretData}}' \
  | podman secret create chaudron-database-url-owner -
```

```sh
# 3. Create the application role. The runtime image ships this one script, so
#    it is the same code path a checkout and CI use — there is no second copy
#    of the procedure to drift.
read -rs -p 'App role password: ' CHAUDRON_DB_APP_PASSWORD && export CHAUDRON_DB_APP_PASSWORD
podman run --rm --network chaudron-net \
  --env-file ~/chaudron/chaudron.env \
  --env CHAUDRON_DB_APP_PASSWORD \
  --secret chaudron-database-url-owner,type=env,target=CHAUDRON_DATABASE_URL \
  ghcr.io/claravnk/chaudron:latest python /app/scripts/provision_app_role.py
unset CHAUDRON_DB_APP_PASSWORD
```

> This block used to be twenty lines of SQL pasted into `psql`, because the
> runtime image shipped the wheel and the migrations but **not** `scripts/`. The
> README said so itself and flagged the duplication as something to keep in
> step — two copies of a security procedure, one of which was going to drift,
> and the silent failure mode of the wrong one is "every household reads every
> other household's rows". `backend/Containerfile` now ships that one script,
> and the SQL block is gone rather than maintained.
>
> The script also does things the SQL could not: it validates the role name
> against an identifier pattern rather than escaping it, builds each statement
> server-side with PostgreSQL's own `format('%I','%L')` quoting, sets the
> password through a path where the plaintext never reaches `argv` or the server
> log, and then checks its own work.
>
> **Only `provision_app_role.py` is in the image**, named file by file rather
> than by directory. `seed.py` in particular must never be one `podman exec`
> away from a production database: it carries a household UUID that is public in
> this repository, it writes to whatever `CHAUDRON_DATABASE_URL` names without
> consulting `CHAUDRON_ENV` or asking for confirmation, and it leaves a second
> real household behind. The owner DSN it would need is present in the
> deployment, held by `chaudron-migrate.container` — so keeping it out of the
> image is the control, not an oversight.

```sh
# 4. Point the API — and only the API — at the new role.
read -rs -p 'App DSN: ' V && printf '%s' "$V" | podman secret create chaudron-database-url-app - && unset V
```

```sh
# 5. Reinstall the units — the shipped chaudron.container already names
#    chaudron-database-url-app, so there is nothing to hand-edit any more.
install -m 0644 ops/chaudron.container ops/chaudron-migrate.container \
  ~chaudron/.config/containers/systemd/
systemctl --user daemon-reload && systemctl --user restart chaudron.service
```

Then verify (§6.2). Podman secrets keep whatever trailing newline the shell gave
them, so use `printf '%s'` and never `echo`.

Once `chaudron-database-url-app` and `chaudron-database-url-owner` are both in
place and the service is healthy, remove the old ambiguous secret so nothing can
fall back to it:

```sh
podman secret rm chaudron-database-url
```

### 6.2 Verifying that it is actually on

Three questions, three answers. All of them run against the production database;
none of them writes.

```sh
# Which tables are protected, and by what? Expect 13 tables and 16 policies.
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "select tablename, policyname, cmd from pg_policies where schemaname='public' order by 1,2;"

# Any table carrying household_id that the migration missed? Expect zero rows.
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
   join pg_attribute a on a.attrelid=c.oid
   where n.nspname='public' and c.relkind='r' and a.attname='household_id'
     and not a.attisdropped and not c.relrowsecurity;"

# Is the application's own role exempt? Expect f | f, and no tables owned.
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "select rolsuper, rolbypassrls from pg_roles where rolname='chaudron_app';" -c \
  "select relname from pg_class where relowner='chaudron_app'::regrole;"
```

`provision_app_role.py --check` answers all three at once and exits non-zero
when any of them is wrong — that is the form to put in a deployment pipeline,
after every migration. It runs from the deployed image, no checkout required:

```sh
podman run --rm --network chaudron-net \
  --env-file ~/chaudron/chaudron.env \
  --secret chaudron-database-url-owner,type=env,target=CHAUDRON_DATABASE_URL \
  ghcr.io/claravnk/chaudron:latest python /app/scripts/provision_app_role.py --check
```

The end-to-end check, which no catalogue query can replace: connect **as the
application role** and read a tenant table without naming a household.

```sh
psql "$APP_DSN" -c "select count(*) from inventory_lot;"   # expect 0
psql "$APP_DSN" -c "set local chaudron.household_id = '<a real household uuid>';
                    select count(*) from inventory_lot;"   # expect that household's rows
```

### 6.3 What breaks when the role is wrong, and how it looks

| Symptom | Cause |
| --- | --- |
| Every endpoint returns empty lists; no errors anywhere | The API connects as a non-owner but nothing posts the household. Check that `api.deps` resolves the tenant and that the session is the one from `infra/db.py`. |
| Writes fail with `new row violates row-level security policy` | Same cause, seen from the write side. RLS filters reads silently and refuses writes loudly, so this is the *useful* symptom of the two. |
| Everything works and `pg_policies` is full — but two households see each other | The API is still connecting as the owner, or as a superuser. Run `provision_app_role.py --check`. This is the failure mode with no symptom of its own. |
| `permission denied for table …` after a new migration | A table created by a later revision that the app role was never granted. `ALTER DEFAULT PRIVILEGES` covers tables created *after* provisioning; re-run the script once. |
| `alembic upgrade` fails with `must be owner of table` | The migrator is using the app DSN. It needs the owner. |
| `invalid input syntax for type uuid: ""` | Something is posting an empty household. `chaudron_current_household()` treats `''` as "no tenant" precisely to prevent this; a caller writing raw SQL against `current_setting` will not. |

### 6.4 Rolling back

The migration is reversible and the switch is not one-way:

```sh
# Point the API back at the owner DSN. Policies stay in place but stop applying,
# because the owner is exempt from them.
#
# This is a rollback of last resort, not a debugging step: for the duration,
# every household can read every other household's rows and nothing says so.
# Prefer 6.3 — the symptom table almost always identifies the real cause in less
# time than this takes to undo.
podman secret inspect chaudron-database-url-owner --showsecret --format '{{.SecretData}}' \
  | podman secret create chaudron-database-url-app-rollback -
# then edit the installed chaudron.container to name that secret, and:
systemctl --user daemon-reload && systemctl --user restart chaudron.service

# Or remove the policies entirely
podman exec chaudron-migrate alembic downgrade 0003
```

`downgrade 0003` drops the sixteen policies, disables row-level security on the
thirteen tables and drops `chaudron_current_household()`. It does **not** drop
the `chaudron_app` role — roles are cluster-wide and may be shared. Remove it by
hand if you mean to:

```sh
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "reassign owned by chaudron_app to chaudron; drop owned by chaudron_app; drop role chaudron_app;"
```

### 6.5 What is deliberately not protected

* `household`, `user_account`, `unit`, `llm_provider` carry no `household_id`
  and get no policy. The first two are read *before* a household is known — the
  tenant resolution itself queries `household` — and the last two are shared
  reference data. Guarding them would mean guarding the lookup that decides what
  to guard.
* The **public product catalogue** (`product.household_id IS NULL`) is readable
  and writable by any household that has posted one: it is a shared cache of
  Open Food Facts answers (ADR-0008), and scoping it per tenant would multiply
  the outbound calls by the number of households for byte-identical content. It
  is still invisible to a connection with no tenant, and no household can delete
  from it.
* **Background jobs** run outside any HTTP request and therefore outside the
  tenant resolution. They must open their session with
  `Database.session(household_id=...)`, taking the household from the row being
  processed. `docs/data-model.md` section 5.4 expects them to be the first thing
  to leak; under RLS they now read nothing instead, which is a much better first
  symptom.

---

## 7. Reverse proxy, TLS and the PWA

Design and alternatives: [ADR-0011](../docs/adr/0011-tls-termination-and-static-hosting.md).
Configuration: `ops/Caddyfile.example`.

### 7.1 Why this is not optional

Before `chaudron-proxy.container` existed, the deployment had no TLS anywhere
and `frontend/dist/` had no host. Neither is cosmetic:

- **A service worker only registers in a secure context.** Over plain HTTP, off
  `localhost`, the Workbox setup in `frontend/vite.config.ts` is inert — no
  offline mode, no install prompt, no precache. `getUserMedia` is
  secure-context only too, so the barcode scanner never gets a camera. Without
  HTTPS the application does not work; this is not hardening.
- **`Strict-Transport-Security` is ignored over HTTP, by specification.** The
  API was emitting a header that could never engage.
- **The frontend origin had no security headers at all**, because nothing served
  it. That origin is where the service worker lives — so a cross-site script
  there becomes *persistent*, poisoned into the Workbox precache and re-served
  offline — and where `localStorage` keeps a household's preferences. It now has
  a strict CSP, `nosniff`, HSTS, `Referrer-Policy` and `X-Robots-Tag`.

The proxy also puts a **2 MiB ceiling on request bodies**, enforced before a
byte reaches Python, and keeps a **filtered access log** (§4).

**Everything above is true only if the proxy is the only way in.** It was not:
`chaudron.container` published the API on `127.0.0.1:8000`, "for `curl` from the
host". That was a second front door and the unhardened one — no body cap, no
filtered log, none of the headers, no `X-Real-IP`/`Forwarded` scrubbing. Worse
for rate limiting: `FORWARDED_ALLOW_IPS` names the proxy's address, so a request
arriving over loopback is not a trusted peer and uvicorn attributes it to
`127.0.0.1` — every caller on that port shared **one bucket** across all three
limiters, which any local account could exhaust for everyone or hide inside.
The `PublishPort` line is gone; §2.6 lists the two ways to reach the API for
debugging without reopening it.

Three of the proxy's controls are worth naming because they are recent and each
closes something measured:

- **Error responses carry the full header set.** `handle_errors` is a separate
  route tree, so nothing declared in the site block reaches it. A 404 used to go
  out with its CSP and nothing else — no HSTS, no `nosniff`, no
  `Referrer-Policy`, and `Server: Caddy` still advertised. A 404 is the easiest
  response to make a victim load, and it was the one that did not pin HTTPS.
  `encode` was missing there too: ~22 kB uncompressed on every error.
- **`X-Real-IP` and `Forwarded` are deleted from every request.** Removing
  `trusted_proxies` makes Caddy rewrite `X-Forwarded-For` and
  `X-Forwarded-Proto`; it does nothing to those two, which reached the API byte
  for byte. Nothing reads them today — which is exactly when it is cheap to fix.
- **The CSPs carry `report-to` and `report-uri`.** A CSP regression is otherwise
  the quietest failure on this origin. The endpoint is a 204 served by Caddy, so
  a violation becomes one log line; it records *that* a policy was violated, not
  which directive. See the `@cspReport` block in `ops/Caddyfile.example`.

### 7.2 One sysctl: unprivileged ports

Rootless Podman binds published host ports as the unprivileged service user, and
80 and 443 are below the default privileged-port boundary.

```sh
echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/99-chaudron-rootless-ports.conf
sudo sysctl --system
sysctl net.ipv4.ip_unprivileged_port_start   # expect 80
```

> **This is a system-wide sysctl, and it is the loosest of the three options.**
> It lowers the privileged-port boundary for *every* process of *every*
> unprivileged user on the host — not just Podman's, not just this account's.
> Any local account can then bind 80–1023.
>
> The sharp edge is the restart window. `chaudron-proxy.service` releases :80
> and :443 while it restarts — a configuration change (§7.5), a `podman` update,
> a reboot. Anything already waiting can take them in that gap and answer as
> this site, on a host where nothing else is expected to want those ports. HSTS
> limits what an attacker gains on :443 (they have no certificate), but :80 is
> where the ACME HTTP-01 challenge is answered, so holding it also means holding
> renewal.

Two narrower alternatives. Neither is applied by default — pick one deliberately
if this host is shared, or if the reasoning above is not acceptable:

**a. Grant the capability to Podman's port forwarder only.** This is the same
privilege, scoped to the one binary that needs it, instead of to every process
on the machine:

```sh
sudo setcap cap_net_bind_service=+ep /usr/libexec/podman/rootlessport
getcap /usr/libexec/podman/rootlessport
```

The catch, and it is the reason this is not the default: **an RPM upgrade of
`podman` replaces that binary and drops the capability**, silently, and the proxy
then fails to start on the next restart rather than at upgrade time. If you take
this route, reapply it from a `%posttrans`-style hook or a configuration
management run, and assert `getcap` output in monitoring. Verify it works on this
host before removing the sysctl — the rootless network backend here is `pasta`
with `netavark`, and the forwarding path for published ports on a user-defined
network is `rootlessport`.

**b. Socket activation.** systemd binds :80 and :443 as root at boot and passes
the listening descriptors to the container; nothing unprivileged ever binds a low
port, and the descriptors cannot be taken during a restart because systemd never
releases them. Caddy supports this directly — `bind fd/3` and `bind fd/4` in the
site address, with a `.socket` unit alongside the quadlet. It is the strongest
option and the most configuration, and it changes how the site block is written,
so it is a deliberate migration rather than a drop-in.

On a single-purpose machine the sysctl is an acceptable trade, and it is what the
rest of this document assumes. On a shared one it is not.

SELinux: publishing a port through rootless Podman needs no `semanage port`
entry — the bind happens in the rootless port forwarder, not in a confined
domain. The `httpd_can_network_connect` boolean in §3 applies to a *host* nginx
or Apache reaching the API, not to this containerised proxy.

### 7.3 Deploy

```sh
# 1. Configuration
install -d -m 0755 ~chaudron/chaudron/caddy
install -m 0644 ops/Caddyfile.example ~chaudron/chaudron/caddy/Caddyfile
install -m 0600 /dev/null ~chaudron/chaudron/caddy.env
```

`caddy.env` holds three non-secret values:

```sh
CHAUDRON_SITE_HOST=chaudron.example.tld
CHAUDRON_ACME_EMAIL=ops@example.tld
CHAUDRON_API_UPSTREAM=chaudron:8000
```

```sh
# 2. Build and publish the PWA. VITE_API_BASE_URL is the SAME origin as the
#    site: the proxy serves both, which is what keeps CORS out of the picture
#    entirely (CHAUDRON_CORS_ORIGINS stays empty).
VITE_API_BASE_URL=https://chaudron.example.tld \
VITE_SITE_URL=https://chaudron.example.tld \
  npm --prefix frontend ci && npm --prefix frontend run build
rsync -a --delete frontend/dist/ ~chaudron/chaudron/www/
```

```sh
# 3. Start
systemctl --user daemon-reload
systemctl --user start chaudron-proxy.service
journalctl --user -u chaudron-proxy.service -f    # watch the ACME order
```

The hostname must already resolve to this machine and :80 must be reachable from
the internet before the first start: Caddy answers the ACME HTTP-01 challenge
there. A failed order retries with backoff; Let's Encrypt rate-limits repeated
failures, so fix DNS before restarting in a loop.

### 7.4 Verify — in a browser, not only with curl

```sh
curl -sSI https://chaudron.example.tld/ | grep -iE 'strict-transport|content-security|x-content-type|referrer'
curl -sSI https://chaudron.example.tld/app/ | grep -i x-robots-tag
curl -sS  https://chaudron.example.tld/healthz
curl -sSI http://chaudron.example.tld/ | head -3        # expect 308 to https
```

**Check an error response too, not only a 200.** This is the one that was
broken, and a 200 says nothing about it:

```sh
# Expect all eight, and NO `Server:` line.
curl -sSI https://chaudron.example.tld/nope | grep -icE \
  'strict-transport|x-content-type|referrer-policy|x-frame|permissions-policy|cross-origin-opener|cross-origin-resource|content-security'
curl -sSI https://chaudron.example.tld/nope | grep -i '^server:'   # expect nothing

# Expect Content-Encoding on the error body as well.
curl -sSI -H 'Accept-Encoding: gzip, zstd' https://chaudron.example.tld/nope | grep -i content-encoding
```

**Check the forwarded headers are scrubbed.** Send all four and read what the
API believes:

```sh
curl -sS https://chaudron.example.tld/healthz \
  -H 'X-Forwarded-For: 6.6.6.6' -H 'X-Real-IP: 6.6.6.6' -H 'Forwarded: for=6.6.6.6'
journalctl --user -u chaudron.service -n 5   # the client address must not be 6.6.6.6
```

Then open the site and check the console. `curl` cannot tell you the two things
most likely to be wrong:

- **The service worker registered.** DevTools → Application → Service Workers.
  If it did not, the page is not in a secure context or `/sw.js` is not being
  served.
- **Nothing is blocked by CSP.** The scanner in particular: the barcode reader
  is a WebAssembly module and needs `'wasm-unsafe-eval'`, which is in the policy
  — but any frontend change that adds an external font, an analytics script or a
  third-party image host will be blocked, and the console is where that shows
  up. A CSP violation is silent everywhere else.

> **Not verified on this machine.** The CSP in `Caddyfile.example` was derived
> by reading the built `frontend/dist/` — inline scripts, inline styles,
> external origins, WebAssembly and worker usage — and the configuration was
> validated with `caddy validate` and `caddy adapt` against Caddy v2.11.4. It
> has **not** been exercised by a browser against a running instance. Do that
> before calling the deployment done.

### 7.5 Configuration changes need a restart

The admin API is off, so `caddy reload` has nothing to talk to:

```sh
systemctl --user restart chaudron-proxy.service
```

Validate before restarting, so a typo does not take the site down:

```sh
podman run --rm -v ~chaudron/chaudron/caddy/Caddyfile:/etc/caddy/Caddyfile:Z,ro \
  -e CHAUDRON_SITE_HOST -e CHAUDRON_ACME_EMAIL \
  docker.io/library/caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```

---

## 8. Backups, encryption, and the restore drill

This section replaces the two hand-typed `pg_dump` / `pg_restore` commands that
used to be all of it — no timer, no retention, no encryption, no off-machine
copy, and no tested restore.

### 8.1 Prerequisites

```sh
sudo dnf install -y age rsync          # age is in EPEL on Rocky 10
```

Generate the age key pair **on a machine that is not this server**. The server
gets only the public half, so a compromise of the backup job cannot decrypt
yesterday's backups:

```sh
age-keygen -o chaudron-backup.age-key       # keep this file offline and private
grep 'public key' chaudron-backup.age-key   # the age1… value goes on the server
```

Set up a dedicated SSH key for the off-machine copy, restricted on the remote
side to rsync in one directory (`command="rrsync -wo /srv/chaudron"` in
`authorized_keys`). The backup job deliberately has **no** ability to delete
remotely: a job that can destroy the backups it makes is one compromise away
from doing so.

### 8.2 Configure and enable

```sh
install -m 0600 /dev/null ~chaudron/chaudron/backup.env
```

```sh
CHAUDRON_BACKUP_AGE_RECIPIENT=age1…
CHAUDRON_BACKUP_REMOTE=backup@offsite.example.tld:/srv/chaudron
CHAUDRON_BACKUP_SSH_KEY=/home/chaudron/.ssh/chaudron-backup
CHAUDRON_BACKUP_LOCAL_DIR=/home/chaudron/chaudron/backups
CHAUDRON_BACKUP_KEEP_DAYS=7
CHAUDRON_BACKUP_KEEP_WEEKLY=4
```

`CHAUDRON_BACKUP_AGE_IDENTITY` used to be listed here, with the comment "used
ONLY by the restore drill". That was the whole problem: it put the private key on
the application host. It belongs in the backup host's own `backup.env` — §8.6.

Note what is **not** in that file on this host: `CHAUDRON_BACKUP_AGE_IDENTITY`.
The private half belongs on the machine that runs the restore drill, which is not
this one — §8.6.

```sh
systemctl --user enable --now chaudron-backup.timer
systemctl --user start chaudron-backup.service      # run one now
journalctl --user -u chaudron-backup.service -n 30
```

`chaudron-restore-check.timer` is installed on the **backup host**, not here. See
§8.6 for the argument and the configuration.

**Retention.** Daily locally for 7 days, plus the last 4 Monday copies, so a
corruption noticed three weeks late still has something to go back to. The
remote's retention is the remote's business, enforced by its own timer — the
backup job cannot delete there.

**What the dump contains.** `pg_dump --format=custom`, streamed straight into
`age`: the plaintext never touches the filesystem. A plaintext dump written then
encrypted would leave a window — and, after a crash, a permanent copy — of the
whole database in the clear on disk.

### 8.3 Restoring for real

```sh
age --decrypt --identity ~/.config/chaudron/backup.age-identity \
      chaudron-2026-08-04T032000Z.dump.age \
  | podman exec -i chaudron-db pg_restore -U chaudron -d chaudron --clean --if-exists
```

On a **new machine**, the order matters:

1. Bring up `chaudron-db.service` with an empty data directory.
2. Restore the dump as above.
3. Re-provision the application role (§2.5) — a dump restored with `--no-owner`
   carries no `chaudron_app` grants, and the API cannot read a table it has not
   been granted on.
4. Restore `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY` from its own, separate backup
   (§8.4). **Without this step every stored provider credential is dead**, and
   nothing at restore time says so.
5. Restore `~/chaudron/data/caddy` or let Caddy re-issue the certificate.

`chaudron-restore-check.timer` exercises steps 1–2 weekly, on the backup host,
into a **disposable PostgreSQL container**, and checks that the schema and the
reference data came back. It cannot exercise step 4 — see §8.5.

### 8.4 The dump and the key are two artefacts. Keep them apart.

**This is the part that is easy to get wrong in either direction, and the two
mistakes are one step apart.**

Every household's provider API keys are stored as `api_key_ciphertext`,
encrypted with `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`. That key lives in a Podman
secret and **is not in the database**, which is deliberate: a stolen dump must
not be enough to decrypt third-party secrets.

So:

| Mistake | What happens |
| --- | --- |
| Back up the dump, **not** the key | A restore produces a database where every `api_key_ciphertext` is permanently undecryptable. Nothing errors during the restore. The failure surfaces later, as authentication errors against every provider, and by then the key is gone. |
| Store the key **with** the dumps | The encryption is undone. One stolen archive now carries both the ciphertext and the key that opens it, and every backup ever taken is retroactively worthless as a protection. |

**The rule: both are required, in different places, with different access.**

- The **dump** goes to `CHAUDRON_BACKUP_REMOTE`, nightly, encrypted to the age
  recipient.
- The **key** goes somewhere else entirely — a password manager, a sealed
  envelope, an offline device. It changes only when it is rotated, so it does
  not need a timer; it needs to exist, and to be findable by whoever restores
  the machine at 3 a.m.
- The **age private identity** is a third artefact, and it must not live in the
  backup directory either. The restore drill needs it; the backup job does not.

`chaudron-backup.sh` enforces the half it can: it refuses to run if it finds a
key-shaped file in its own destination directory, and it will not read the
credential key at all. `chaudron-restore-check.sh` refuses to run if the age
identity is inside the archive directory. Neither can check where you put the
copy in the password manager.

Export the key for its separate backup:

```sh
podman secret inspect chaudron-credential-encryption-key --showsecret --format '{{.SecretData}}'
```

Restore it on a new machine:

```sh
read -rs -p 'Credential encryption key: ' V && printf '%s' "$V" | podman secret create chaudron-credential-encryption-key - && unset V
```

Rotating the key invalidates every stored household credential — every household
must re-enter its provider key. See `docs/security-model.md`.

### 8.5 The drill the timer cannot run

`chaudron-restore-check.service` proves the dump restores. It cannot prove the
credential key still opens what is in it, because it deliberately does not have
that key.

Once a year, do it by hand on a scratch machine: restore a dump, install the
credential key from its separate backup, start the API, and open a household
that has a stored provider key. If a recipe suggestion works, the pair is
intact. That is the only test that covers both artefacts at once, and it is the
one that matters on the day the machine is gone.

### 8.6 Where the restore drill runs, and why it is not here

**This section exists because the arrangement it replaces made a claim that was
false in the exact deployment this document recommended.**

`chaudron-backup.sh` encrypts to an age **public** recipient, and said so:
*"this host holds only the encryption half, so a compromise of the backup job
cannot decrypt yesterday's backups."* Meanwhile §8.2 told you to put
`CHAUDRON_BACKUP_AGE_IDENTITY` — the **private** half — in the same `backup.env`,
on the same machine, read by a timer running as the same user. Follow this
document end to end and the application host held the archives *and* the key that
opens them. One compromise of the `chaudron` account yielded both.

The old guard did not catch it. It tested whether the identity file lived inside
the archive *directory*, which blocks the direct case and the symlink and misses
the one that actually happens: same home, same UID, different subdirectory —
precisely what §8.2 instructed. It refused the arrangement nobody chooses and
passed the one everybody gets.

**The choice made here: the drill moves off the application host.**

The property worth protecting is not "no machine anywhere holds both" — some
machine has to, or the backups can never be restored. It is:

> **the internet-facing machine that runs the application cannot read its own
> backups.**

That is the machine with the attack surface, the one whose loss the backups exist
to survive, and the one an attacker reaches first. The backup host already holds
every archive; giving it the key adds nothing an attacker who owns it did not
already have. Giving the *application* host the key hands over both halves to
whoever gets a shell in the API container's blast radius.

The rewritten `chaudron-restore-check.sh` is what makes this practical: it starts
its own **disposable PostgreSQL container** (`--network none`, tmpfs data
directory, random password, `--rm`) rather than exec'ing into `chaudron-db`, so it
needs no production database and can run anywhere Podman and `age` exist. It also
refuses to run on the application host — it looks for the quadlet, the credential
secret and the container — rather than trusting that this section was read.

That container change closes a second problem in its own right. The old drill
replayed the archive into the **production cluster**, as the **owner** role, which
is exempt from every RLS policy. `age` encrypts *to* a recipient; it does not
authenticate the *sender*, and the recipient's public key sits in clear text in
`backup.env`. So anyone able to drop a file into the archive directory could
produce an archive that decrypts perfectly and contains arbitrary SQL — and
`pg_restore` executes SQL, weekly, unattended, through the most privileged role
in the system.

#### Installing it on the backup host

Podman, `age` and the archives are all it needs.

```sh
install -d -m 0755 ~/.config/systemd/user ~/chaudron/bin
install -m 0644 ops/chaudron-restore-check.service ops/chaudron-restore-check.timer \
  ~/.config/systemd/user/
install -m 0755 ops/chaudron-restore-check.sh ~/chaudron/bin/
```

`~/chaudron/backup.env` on that host, mode 0600 — note it carries the identity
and **not** the recipient, remote or SSH key, which are the application host's
business:

```sh
CHAUDRON_BACKUP_LOCAL_DIR=/srv/chaudron
CHAUDRON_BACKUP_AGE_IDENTITY=/home/backup/.config/chaudron/backup.age-identity
```

```sh
install -d -m 0700 ~/.config/chaudron
install -m 0400 /path/to/backup.age-identity ~/.config/chaudron/backup.age-identity
systemctl --user enable --now chaudron-restore-check.timer
systemctl --user start chaudron-restore-check.service     # run one now
journalctl --user -u chaudron-restore-check.service -n 40
```

Verify the property on the **application** host — expect no output:

```sh
grep -rl 'AGE-SECRET-KEY-1' ~chaudron/ 2>/dev/null
```

#### If you only have one machine

That is a legitimate deployment, and it is a real downgrade rather than a
formality. Say so out loud instead of letting the comment claim otherwise:

```sh
# in ~chaudron/chaudron/backup.env, on the application host
CHAUDRON_BACKUP_AGE_IDENTITY=/home/chaudron/.config/chaudron/backup.age-identity
CHAUDRON_RESTORE_CHECK_ACKNOWLEDGE_COLOCATION=yes
```

The script then runs, logs two warnings on every execution, and the deployment
**does not have** the property in the quotation at the top of this section. What
you still keep: the disposable container, so the drill no longer replays
unauthenticated SQL into production as the owner role. What you lose: an attacker
who reaches the `chaudron` account gets the archives and the key together, and
every backup taken since is retroactively readable.

The mitigations that remain worth taking in that case:

- `chmod 0400` the identity and keep it out of `$HOME/chaudron/` so it is not in
  the tree an over-broad `rsync` or a misconfigured backup would sweep up;
- keep the off-machine copies on a host the application host cannot delete from
  (`rrsync -wo`, already the case — §8.1), so the archives survive even when the
  key does not;
- treat this as the first thing to fix when a second machine exists.

---

## 9. Resource limits

Every quadlet carries memory, CPU and process bounds. Before they existed,
`grep 'Memory\|CPU\|PidsLimit\|Ulimit' ops/*.container` returned nothing: one
pathological PDF import, or a colocated Ollama inference, could take the whole
machine — PostgreSQL included, which turns a slow request into a database
outage.

| Unit | Container bound | systemd cgroup bound |
| --- | --- | --- |
| `chaudron` | `--memory=1g --pids-limit=1024` | `MemoryHigh=768M MemoryMax=1200M CPUQuota=150%` |
| `chaudron-db` | `--memory=768m --memory-reservation=512m` | `MemoryHigh=640M MemoryMax=896M CPUQuota=150%` |
| `chaudron-proxy` | `--memory=256m --pids-limit=512` | `MemoryHigh=192M MemoryMax=320M CPUQuota=100%` |
| `chaudron-migrate` | `--memory=512m --pids-limit=256` | `MemoryHigh=384M MemoryMax=640M CPUQuota=100%` |

Two layers, on purpose. `PodmanArgs=` bounds the container payload;
`MemoryMax=` in `[Service]` bounds the whole unit cgroup, which also contains
conmon and the rootless port forwarder. `MemoryHigh` throttles and reclaims
before `MemoryMax` kills, so the failure mode is "slow" before it is
"OOM-killed with a request in flight".

**Sized for a 2 GB host.** These are floors to start from, not a tuning
recommendation:

- Raise `chaudron-db` **and** `shared_buffers` together, never one alone.
- `chaudron`'s tmpfs is 256 MiB and is charged against its `MemoryMax`, because
  a tmpfs is memory. It was 64 MiB, which a handful of concurrent 10 MiB receipt
  imports filled — and a full `/tmp` on a `ReadOnly=true` container is an ENOSPC
  mid-request rather than a clean 413.
- If inference runs on a colocated Ollama, lower
  `CHAUDRON_RECIPE_MAX_CONCURRENT_TOTAL` before raising anything here (ADR-0007).

Check what is actually being used before changing a number:

```sh
systemd-cgtop --order=memory
podman stats --no-stream
systemctl --user show chaudron.service -p MemoryHigh -p MemoryMax -p MemoryCurrent
```

A container being OOM-killed shows up as a restart loop with exit code 137:

```sh
journalctl --user -u chaudron.service | grep -iE 'oom|killed|137'
```
