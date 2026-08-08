"""Application settings, read from the environment once and validated at startup.

Every key documented in ``.env.example`` is declared here, under the
``CHAUDRON_`` prefix except for the four provider keys that keep their vendor
name (``ANTHROPIC_API_KEY`` and friends) because that is what the SDKs read.

Two behaviours are deliberate and load-bearing.

*Empty means unset.* ``.env.example`` ships every key with an empty value, and a
copied-then-partially-filled file is the normal case. A blank value therefore
falls back to the declared default instead of failing on ``int("")`` -- but a
blank value on a **required** field still fails, with "field required" rather
than a type error nobody can act on.

*Fail-fast.* :func:`get_settings` raises on the first invalid value. Nothing in
this module degrades gracefully: an instance that cannot decrypt household
credentials or cannot reach its database must refuse to start rather than serve
half of its endpoints (``docs/architecture.md`` section 5).
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Annotated, Any, Final, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: AES-256-GCM, as documented on ``LlmProviderConfig.api_key_ciphertext``.
CREDENTIAL_KEY_BYTES: Final = 32

#: Short enough to be brute-forced offline if it ever signs a token.
MIN_SECRET_KEY_LENGTH: Final = 32

Environment = Literal["local", "ci", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

#: The only environments allowed to run with row-level security bypassed, i.e. to
#: connect as the owner of the tables. Both do it on purpose: ``local`` runs the
#: migrations and the API from one DSN, and ``ci`` needs the owner *and* the
#: provisioned role side by side to prove the policies apply to one and not the
#: other (``tests/tenancy/conftest.py``). Everything else -- ``staging``
#: included -- is a deployment serving somebody's data. See
#: :attr:`Settings.requires_row_level_security`.
_ROW_LEVEL_SECURITY_OPTIONAL: Final[frozenset[Environment]] = frozenset({"local", "ci"})

#: Comma-separated env values, decoded by a validator instead of by
#: pydantic-settings' JSON decoder -- which would reject ``a,b`` outright.
CommaSeparated = Annotated[list[str], NoDecode]


class ConfigurationError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


class Settings(BaseSettings):
    """The whole configuration surface of the backend."""

    model_config = SettingsConfigDict(
        env_prefix="CHAUDRON_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Unknown CHAUDRON_* variables are a typo far more often than a feature.
        extra="ignore",
        frozen=True,
    )

    # -- Application ------------------------------------------------------- #

    #: **Required, with no default.** It used to default to ``local``, and
    #: ``local`` is the value that opens ``/docs`` and ``/openapi.json`` and turns
    #: HSTS off. So an operator who deployed a copy of ``.env.example`` -- which
    #: ships every key empty, and which this class reads as *absent* -- got a
    #: production instance publishing its complete route and schema inventory
    #: without authentication, and doing it silently. Audit AUD-018 fixed the
    #: symptom on ``CHAUDRON_ENABLE_DOCS`` alone and left the cause here.
    #:
    #: A default that fails towards "exposed" is not an acceptable default, and
    #: there is no safe one to pick: guessing ``production`` on a developer's
    #: laptop would break the local loop instead. So the environment is declared,
    #: once, or the process refuses to start with "env: field required".
    env: Environment
    log_level: LogLevel = "INFO"
    port: int = Field(default=8000, ge=1, le=65535)
    base_url: str = "http://localhost:8000"

    # -- Database ---------------------------------------------------------- #
    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)

    # -- Security ---------------------------------------------------------- #
    # No JWT settings here, and that is deliberate. Authentication is server-side
    # sessions (`services/auth.py`) and hashed machine tokens (`services/tokens.py`);
    # `jwt_ttl_minutes` and `jwt_algorithm` were scaffolding from before that was
    # decided, read by nothing. A free-form `jwt_algorithm` accepting `none` is an
    # algorithm-confusion vector with no upside (audit AUD-031, baseline SEC-007),
    # so it was removed rather than typed: the safest configurable is the one that
    # does not exist.
    secret_key: SecretStr

    credential_encryption_key: SecretStr

    # -- Sessions ----------------------------------------------------------- #
    # Two deadlines, because they answer different questions. The absolute one
    # bounds how long a stolen cookie is worth stealing; the idle one bounds how
    # long a forgotten tab on a shared machine stays signed in. Neither is a
    # substitute for the other, and both are enforced in the same SQL predicate
    # (``services/auth.py``).

    #: Thirty days. Long enough that a household appliance is not a login screen,
    #: short enough that an old backup of a browser profile stops being a key.
    session_absolute_ttl_hours: int = Field(default=30 * 24, ge=1)

    #: Seven days of inactivity. Slides forward on use, at one-minute granularity.
    session_idle_ttl_hours: int = Field(default=7 * 24, ge=1)

    #: Sign-in attempts per source address per hour. Bounds a spray across many
    #: accounts from one host -- which the per-account limit below cannot see.
    login_attempts_per_ip_per_hour: int = Field(default=30, ge=1)

    #: Sign-in attempts per email address per hour, counted whether or not the
    #: address exists. Bounds a guess at one account from a botnet -- which the
    #: per-address limit above cannot see. Both are needed; neither is sufficient.
    login_attempts_per_account_per_hour: int = Field(default=10, ge=1)

    #: Account creations per source address per hour.
    registrations_per_ip_per_hour: int = Field(default=5, ge=1)

    # -- Machine access tokens (contract v1.1 section 10) -------------------- #

    #: How many live tokens one household may hold. Not a contract field and
    #: deliberately generous: it exists so a loop in a misconfigured client cannot
    #: fill a table, not to ration a household that runs several integrations.
    machine_tokens_per_household: int = Field(default=20, ge=1)

    #: **Rejected** token presentations per source address per hour. Only failures
    #: are counted -- a running integration presents the same valid token on a
    #: timer, and charging it would throttle the legitimate use while leaving a
    #: guesser the same budget (``api/deps.py``).
    machine_token_attempts_per_ip_per_hour: int = Field(default=60, ge=1)

    # -- instance_owner mode ----------------------------------------------- #
    instance_owner_household_id: str | None = None
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    mistral_api_key: SecretStr | None = Field(default=None, validation_alias="MISTRAL_API_KEY")

    llm_default_model: str = "claude-opus-5"
    llm_max_tokens: int = Field(default=4096, ge=1)
    llm_monthly_budget_usd: float = Field(default=0.0, ge=0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)

    # -- Ollama ------------------------------------------------------------- #
    ollama_allowed_hosts: CommaSeparated = Field(default_factory=list)
    ollama_timeout_seconds: float = Field(default=300.0, gt=0)

    # -- Open Food Facts ---------------------------------------------------- #
    # Their policy asks for an honest caller identity; the repository URL is the
    # contact point, so the default is usable rather than merely polite.
    off_user_agent: str = "Chaudron/0.1.0 (+https://github.com/ClaraVnk/chaudron)"
    off_base_url: str = "https://world.openfoodfacts.org"
    off_cache_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=0)

    # -- Inbound email ------------------------------------------------------ #
    inbound_email_webhook_key: SecretStr | None = None
    inbound_email_domain: str | None = None
    inbound_email_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)

    # -- Abuse and availability limits -------------------------------------- #
    # See ``api/throttling.py`` for what these bound and, just as importantly,
    # what they do not: the counters are per process.

    #: 256 KiB. The largest legitimate v1 request is an inventory creation with an
    #: inline product, which is under a kilobyte; a hundredfold margin absorbs any
    #: reasonable growth of the JSON contract. It is *not* sized for the receipt
    #: photo import: ``CHAUDRON_INBOUND_EMAIL_MAX_BYTES`` (10 MiB by default) is the
    #: order of magnitude that route will need, and raising it globally to reach it
    #: would hand every JSON endpoint a forty-times larger memory footprint. That
    #: route must carry its own, higher, per-route bound when it is written.
    max_request_body_bytes: int = Field(default=256 * 1024, ge=1024)

    #: Recipe suggestions per household per hour. Each one is a billed inference.
    recipe_suggestions_per_hour: int = Field(default=10, ge=1)
    #: In-flight suggestions per household. Two open browser tabs must not double
    #: the bill; one is unusably strict when a request takes half a minute.
    recipe_max_concurrent_per_household: int = Field(default=2, ge=1)
    #: In-flight suggestions for the whole process. The ceiling that keeps a
    #: colocated Ollama on a small machine from being asked for six generations at
    #: once (ADR-0007, and the OOM-kill documented in ``infra/llm/settings.py``).
    recipe_max_concurrent_total: int = Field(default=4, ge=1)

    # -- Shopping-list import (api contract 7.3) ---------------------------- #
    # These four bounds are deliberately *separate* from
    # ``max_request_body_bytes``: that one sizes a JSON body, and a file is not a
    # JSON body. Each is checked before any parsing, and each overrun answers 413
    # rather than reading part of the document (contract 7.3).

    #: 1 MiB, and the number is not arbitrary. Starlette spools an uploaded part to
    #: a temporary file on disk the moment it passes ``spool_max_size``, which is
    #: 1 MiB (``starlette/formparsers.py``). At or below this value the document
    #: never touches the filesystem, which is what "the file is not kept" (contract
    #: 7.3) should mean in practice rather than "it is deleted afterwards". It is
    #: also ample: a text-bearing shopping list is a few kilobytes, and a scanned
    #: one has no extractable text at any size because nothing here does OCR.
    #: Raising this past 1 MiB is allowed and starts spooling to disk; the upload is
    #: still closed -- and therefore unlinked -- on every path out of the handler.
    shopping_import_max_bytes: int = Field(default=1024 * 1024, ge=1024)

    #: Pages read from an uploaded PDF. A household shopping list is one or two
    #: pages; twenty is generous and still bounds the work a single request buys.
    shopping_import_max_pdf_pages: int = Field(default=20, ge=1, le=500)

    #: Characters of extracted text, across the whole document. Sized for roughly
    #: two thousand lines of shopping list, and the only bound standing between a
    #: highly compressible PDF content stream and this process's memory.
    shopping_import_max_text_chars: int = Field(default=200_000, ge=1000)

    #: Candidate lines kept in a proposal. Unlike the three bounds above this one
    #: does **not** answer 413: the extra lines are dropped and the response says
    #: ``truncated: true``, because a list one line too long is still worth
    #: importing whereas a document too big to read is not.
    shopping_import_max_lines: int = Field(default=500, ge=1, le=5000)

    #: Shopping-list imports per household per hour. This endpoint calls no model,
    #: which is exactly why it had no cap and why that was the finding: a PDF parse
    #: is CPU a caller does not own, and fifteen concurrent uploads of a one-megabyte
    #: document were answered ``200`` fifteen times while the instance was unusable.
    #: Thirty is far above importing a weekly list and low enough that a loop is not
    #: free. It bounds the *pasted text* path too, deliberately: whichever entrance
    #: is throttled less becomes the one an attacker uses.
    shopping_imports_per_hour: int = Field(default=30, ge=1)

    #: In-flight document reads per household. One: two browser tabs importing the
    #: same list at once is a double submit, not a use case.
    shopping_import_max_concurrent_per_household: int = Field(default=1, ge=1)

    #: In-flight document reads for the whole process. This is the cap that bounds
    #: total parsing CPU, and it is the one that matters: the per-document limits in
    #: ``infra/documents/sandbox.py`` bound one document, never a thousand.
    shopping_import_max_concurrent_total: int = Field(default=2, ge=1)

    # -- The process that reads a PDF (infra/documents/sandbox.py) ---------- #
    # A hostile PDF spends two things without bound: memory, because a Flate stream
    # inflates without a budget, and CPU, because nothing stops a content stream
    # from being long. Neither can be bounded in the process serving requests, so
    # the parse runs in a worker under RLIMIT_AS and RLIMIT_CPU and these are the
    # numbers those limits are set from.

    #: ``RLIMIT_AS`` for one document. Has to clear the worker interpreter's own
    #: virtual size -- about 310 MiB with ``pypdf`` imported -- plus the expansion
    #: budget below, or every document fails rather than only the hostile ones.
    document_sandbox_address_space_bytes: int = Field(
        default=512 * 1024 * 1024, ge=128 * 1024 * 1024
    )

    #: ``RLIMIT_CPU`` for one document, in seconds. The measured attack spent 120
    #: seconds of CPU on a one-megabyte upload before any bound refused it; an
    #: ordinary shopping list is read in about ten milliseconds.
    document_sandbox_cpu_seconds: int = Field(default=10, ge=1, le=300)

    #: Resident growth above which a document is refused whatever it returned. The
    #: bound that catches the decompression bomb: it produces no characters, so
    #: every output bound passes and only what it *allocated* gives it away. An
    #: ordinary list grows the worker by about 1 MiB, the measured bombs by 145.
    document_sandbox_expansion_bytes: int = Field(default=64 * 1024 * 1024, ge=8 * 1024 * 1024)

    # -- Receipt import (api contract 6ter and 7) --------------------------- #
    # Separate from the shopping-list bounds above rather than shared, because the
    # documents are not the same shape: a shopping list is a few kilobytes of text
    # and a till receipt is a photograph. Sharing one ceiling would mean either
    # refusing real photos or letting a 6 MB JPEG through the JSON path.

    #: 6 MiB. A phone photograph of a receipt is 1.5 to 4 MB at default quality, so
    #: this accepts one without inviting a burst-mode upload. Above 1 MiB Starlette
    #: spools the part to a temporary file (``starlette/formparsers.py``); the
    #: upload is closed -- and therefore unlinked -- on every path out of the
    #: handler, and nothing else writes the bytes anywhere (contract 6ter: the
    #: image is not retained).
    receipt_import_max_bytes: int = Field(default=6 * 1024 * 1024, ge=1024)

    #: Pages read from a drive order recap. A recap is one or two pages; five
    #: covers a very large order and still bounds the work one request buys.
    receipt_import_max_pdf_pages: int = Field(default=5, ge=1, le=100)

    #: Characters of text extracted from a recap PDF, across the whole document.
    #: The only bound between a highly compressible PDF content stream and this
    #: process's memory (see ``infra/documents/pdf.py`` on unbounded inflation).
    receipt_import_max_text_chars: int = Field(default=100_000, ge=1000)

    #: Lines kept from one receipt. A weekly family shop is 30 to 60 lines; 200 is a
    #: month of them. Overrun drops the extra lines and says ``truncated``, the way
    #: the shopping-list import does: a receipt one line too long is still worth
    #: importing, and the total -- which is what the budget counts -- is unaffected.
    receipt_import_max_lines: int = Field(default=200, ge=1, le=1000)

    #: Receipt imports per household per hour. A photographed receipt is a billed
    #: inference *and* a multi-megabyte upload, so this endpoint spends both the
    #: household's money and the instance's CPU. Ten is well above a weekly shop
    #: and low enough that a loop stops being free.
    receipt_imports_per_hour: int = Field(default=10, ge=1)

    #: In-flight receipt reads per household. Same reasoning as the recipe cap: two
    #: browser tabs must not double the bill.
    receipt_max_concurrent_per_household: int = Field(default=1, ge=1)

    #: In-flight receipt reads for the whole process. Lower than the recipe cap: a
    #: receipt call carries an image, so several at once is several megabytes of
    #: base64 in memory at the same time as the inference.
    receipt_max_concurrent_total: int = Field(default=2, ge=1)

    #: Barcode lookups per household per minute. Must stay **below** the ten calls
    #: per minute that ``infra/openfoodfacts.py`` allows the whole instance, or one
    #: household can still drain the shared budget on its own (ADR-0008).
    product_lookups_per_minute: int = Field(default=6, ge=1)

    # -- Calendar feed (CalDAV) --------------------------------------------- #
    #: Publishing expiry alerts over CalDAV means publishing one household's
    #: inventory -- asset A3 -- to any device holding the feed credentials. Off
    #: unless an operator says otherwise, and turning it off again is the
    #: instance-wide revocation lever (``infra/calendar/credentials.py``).
    calendar_feed_enabled: bool = False

    #: Mixed into the key every feed credential is derived from. Bumping it
    #: invalidates every credential this instance ever issued -- the rotation to
    #: reach for after a leak -- without touching ``CHAUDRON_SECRET_KEY`` and
    #: therefore without invalidating anything else.
    calendar_feed_epoch: str = "1"

    # -- CORS --------------------------------------------------------------- #
    cors_origins: CommaSeparated = Field(default_factory=list)
    cors_allow_credentials: bool = False

    # -- Documentation ------------------------------------------------------ #
    #: ``None`` means "local only". An explicit value is a decision, which is what
    #: exposing every route and schema without authentication deserves.
    enable_docs: bool | None = None

    # -- Normalisation and cross-field rules -------------------------------- #

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_values(cls, data: Any) -> Any:
        """Treat a blank environment variable as an absent one.

        Applied before anything else so that defaults still apply and required
        fields still report themselves as missing.
        """
        if not isinstance(data, dict):
            return data
        return {
            key: value
            for key, value in data.items()
            if not (isinstance(value, str) and not value.strip())
        }

    @field_validator("ollama_allowed_hosts", "cors_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_postgresql(cls, value: SecretStr) -> SecretStr:
        """PostgreSQL with the async driver, and nothing else (ADR-0003).

        Named explicitly rather than left to a connection error at first query:
        "no SQLite mode" is a decision, and a decision deserves a message.
        """
        url = value.get_secret_value()
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "must be a postgresql+asyncpg:// DSN; Chaudron runs on PostgreSQL only "
                "and the application layer is async (ADR-0003)"
            )
        return value

    @field_validator("secret_key")
    @classmethod
    def _require_long_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"must be at least {MIN_SECRET_KEY_LENGTH} characters "
                "(generate one with `openssl rand -hex 32`)"
            )
        return value

    @field_validator("credential_encryption_key")
    @classmethod
    def _require_aes256_key(cls, value: SecretStr) -> SecretStr:
        """Reject at startup what would otherwise fail on the first key rotation.

        A key of the wrong size does not break anything until a household stores
        a provider credential -- days later, in a code path nobody is watching.
        """
        try:
            raw = base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "must be base64-encoded (generate one with `openssl rand -base64 32`)"
            ) from exc
        if len(raw) != CREDENTIAL_KEY_BYTES:
            raise ValueError(
                f"must decode to exactly {CREDENTIAL_KEY_BYTES} bytes for AES-256-GCM, "
                f"got {len(raw)}"
            )
        return value

    @model_validator(mode="after")
    def _reject_wildcard_origin(self) -> Settings:
        """``Access-Control-Allow-Origin: *`` is never right for this API.

        Since authentication became a cookie, the pairing with credentials is not
        merely wrong, it is refused by browsers outright -- so configuring it
        produces a CORS failure nobody can explain from the server logs. It was
        wrong before that too: a wildcard would let any page on the internet read
        the answers to requests the browser attaches a session to. Origins are
        listed, one by one.
        """
        if "*" in self.cors_origins:
            raise ValueError(
                "CHAUDRON_CORS_ORIGINS cannot contain '*': this API authenticates with a "
                "cookie, so a wildcard origin is both refused by browsers and a way for "
                "any site to read a household's data. List the origins explicitly"
            )
        return self

    @model_validator(mode="after")
    def _require_credentials_for_cross_origin_clients(self) -> Settings:
        """A listed origin that may not send credentials can never sign in.

        The session travels in a cookie, and a cross-origin ``fetch`` only sends
        one when the request sets ``credentials: 'include'`` *and* the response
        carries ``Access-Control-Allow-Credentials: true``. Listing origins while
        leaving that off yields a frontend where every call answers ``401`` and
        nothing in the logs says why. Failing here is the difference between a
        five-minute fix and an afternoon.

        An empty ``cors_origins`` is untouched: it means the interface is served
        same-origin, where CORS does not apply at all.
        """
        if self.cors_origins and not self.cors_allow_credentials:
            raise ValueError(
                "CHAUDRON_CORS_ALLOW_CREDENTIALS must be true when CHAUDRON_CORS_ORIGINS "
                "lists an origin: the session is a cookie, and a cross-origin browser "
                "client cannot send one without it"
            )
        return self

    @model_validator(mode="after")
    def _refuse_debug_logging_in_production(self) -> Settings:
        """``DEBUG`` in production publishes the data this application exists to protect.

        The level is applied to the **root** logger (``infra/logging.py``), so it
        is not only this package that starts talking: at ``DEBUG``,
        ``sqlalchemy.engine`` logs every statement *together with its bound
        parameters*. On this schema those parameters are an infant's age band, a
        person's allergens and a household's spending -- health data under GDPR
        article 9 for two of the three -- written in clear text to wherever the
        container's stdout goes, and to every log shipper downstream of it.

        Refused rather than silently clamped: an operator who asked for ``DEBUG``
        wants to see something, and quietly giving them ``INFO`` would send them
        looking for the missing lines. The message says what to do instead.
        """
        if self.is_production and self.log_level == "DEBUG":
            raise ValueError(
                "CHAUDRON_LOG_LEVEL cannot be DEBUG when CHAUDRON_ENV is production: the "
                "level applies to the root logger, and sqlalchemy.engine then writes every "
                "statement with its bound parameters -- allergens, infant age bands and "
                "amounts -- in clear text to the logs. Use INFO, and reproduce the problem "
                "on a staging instance"
            )
        return self

    @model_validator(mode="after")
    def _require_https_in_production(self) -> Settings:
        """The session cookie carries ``Secure``; over plain HTTP it is never set.

        There is no switch for this and deliberately none: a "development mode"
        that drops ``Secure`` is a flag that eventually ships, and the failure it
        produces in production -- authentication that silently never persists --
        looks like an application bug rather than a configuration one. Refusing to
        start is the honest answer.

        ``localhost`` and ``127.0.0.1`` are exempt from the rule everywhere,
        because browsers already treat them as trustworthy origins and will store
        a ``Secure`` cookie sent over ``http://`` there.
        """
        if not self.is_production:
            return self
        url = self.base_url.strip().lower()
        if not url.startswith("https://"):
            raise ValueError(
                "CHAUDRON_BASE_URL must be an https:// URL in production: the session "
                "cookie is Secure and __Host-prefixed, so a browser refuses to store it "
                "over plain HTTP and nobody can stay signed in"
            )
        return self

    @model_validator(mode="after")
    def _keep_concurrency_caps_consistent(self) -> Settings:
        """A per-household cap above the process-wide one is a contradiction."""
        if self.recipe_max_concurrent_total < self.recipe_max_concurrent_per_household:
            raise ValueError(
                "CHAUDRON_RECIPE_MAX_CONCURRENT_TOTAL cannot be below "
                "CHAUDRON_RECIPE_MAX_CONCURRENT_PER_HOUSEHOLD"
            )
        if self.receipt_max_concurrent_total < self.receipt_max_concurrent_per_household:
            raise ValueError(
                "CHAUDRON_RECEIPT_MAX_CONCURRENT_TOTAL cannot be below "
                "CHAUDRON_RECEIPT_MAX_CONCURRENT_PER_HOUSEHOLD"
            )
        if (
            self.shopping_import_max_concurrent_total
            < self.shopping_import_max_concurrent_per_household
        ):
            raise ValueError(
                "CHAUDRON_SHOPPING_IMPORT_MAX_CONCURRENT_TOTAL cannot be below "
                "CHAUDRON_SHOPPING_IMPORT_MAX_CONCURRENT_PER_HOUSEHOLD"
            )
        if self.document_sandbox_address_space_bytes <= self.document_sandbox_expansion_bytes:
            raise ValueError(
                "CHAUDRON_DOCUMENT_SANDBOX_ADDRESS_SPACE_BYTES must leave room above "
                "CHAUDRON_DOCUMENT_SANDBOX_EXPANSION_BYTES for the worker interpreter "
                "itself, or no document can be read at all"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def requires_row_level_security(self) -> bool:
        """Whether this instance must refuse to serve when the policies do not apply.

        **Not** :attr:`is_production`, and the difference is the whole point
        (audit AUD-029). A ``staging`` instance pointed at the owner DSN isolates
        nothing between households, has no symptom a request could reveal, and
        used to answer ``/readyz`` with ``200`` -- so a load balancer put it into
        service and a probe kept it there. The argument is the one
        :attr:`docs_enabled` already makes two properties above: *a staging
        instance carries real data far more often than anyone admits*, and a
        default that fails towards "exposed" is not an acceptable default.

        Exactly two environments are exempt, and each is exempt because
        connecting as the table owner there is a **deliberate** act rather than a
        misconfiguration: ``local``, where a developer runs migrations and the API
        from one DSN, and ``ci``, where ``tests/tenancy/conftest.py`` needs the
        owner to prove that the provisioned role behaves differently. In both,
        ``/readyz`` still *reports* the bypass -- it simply does not refuse
        readiness over it, because a permanent red light is one people learn to
        ignore, including on the deployment where it means something.
        """
        return self.env not in _ROW_LEVEL_SECURITY_OPTIONAL

    @property
    def docs_enabled(self) -> bool:
        """Whether ``/docs`` and ``/openapi.json`` are served.

        Defaults closed everywhere but ``local``. A ``staging`` instance carries
        real data far more often than anyone admits, and the default value of
        ``env`` is ``local`` -- so a variable forgotten at deployment used to open
        the documentation rather than close it (audit AUD-018). A default that
        fails towards "exposed" is not an acceptable default.
        """
        if self.enable_docs is not None:
            return self.enable_docs
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read and validate the configuration once per process.

    The ``ValidationError`` is re-raised as a :class:`ConfigurationError` so the
    startup path has one exception type to report, and so the message never
    carries the offending *values* -- an invalid secret is still a secret.
    """
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigurationError(f"invalid Chaudron configuration: {problems}") from None
