"""record the household's consent before a model provider may be used

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-04 00:00:00.000000+00:00

``docs/security-model.md`` section 12 states the legal basis for every processing
this application performs, and one row of that table had no implementation:

    | **Sending to an external model provider** | **Consent** (art. 6(1)(a)),
    explicit, per household, revocable | This is a transmission to a third party,
    often outside the EU. It must be **opt-in**, never a default. The ``ollama``
    mode must remain fully functional without this consent. |

``llm_provider_config`` carried no consent column at all. The only gate on sending
a household's data to Anthropic, OpenAI, Google or Mistral was whether somebody had
configured a key -- which is a statement about capability, not about agreement.

What actually leaves, and why art. 9 applies
--------------------------------------------

Not the allergens: ``services/dietary.py`` filters forbidden products out of the
inventory *before* the call and ``services/recipes.py`` re-checks the answer after,
so the structured health fields never reach a prompt. That design is sound and this
revision does not disturb it.

What does leave is the part no filter can generalise:

* ``infra/llm/prompts.py`` renders "A young child eats this meal. Required texture:
  ..." -- an age and developmental-stage signal about a minor, by design, because a
  recipe for an infant is unsafe without it;
* the same module interpolates each member's ``free_text_restrictions`` verbatim
  (sanitised of control characters, not of meaning) -- prose a household writes
  about a person's health, in their own words;
* ``services/receipts.py`` sends the **whole** receipt photograph to a vision model,
  uncropped and unredacted, with whatever else happens to be on the paper.

Consent per configuration, not per household
---------------------------------------------

The security model says "per household", and a single household-wide flag would
satisfy that sentence. It would also be the wrong shape, for a reason the same
document supplies two paragraphs later: **Mistral (EU) and Ollama (local) are the
two configurations with no transfer**, and that distinction is already shown in the
interface as a selection criterion. Agreeing to a French company processing in the
EU is not the same act as agreeing to a Chapter V transfer to the United States, and
a consent that cannot tell them apart is not the specific, informed one art. 4(11)
asks for. So the columns land on ``llm_provider_config`` -- which is household-scoped
already, so the consent *is* per household -- and a household that revokes Anthropic
keeps Mistral.

``ollama`` is exempt, and the exemption is the point
-----------------------------------------------------

Local inference transmits nothing to anybody. Requiring an agreement to send data to
one's own machine would train people to click through consent screens, which is how
consent stops meaning anything. The gate in ``services/providers.py`` therefore asks
for consent only where a third party receives the data, and ADR-0007's whole premise
-- that a household unwilling to send anything anywhere still gets the feature --
survives untouched.

Why the columns are nullable, and what is deferred
----------------------------------------------------

``shopping_export_target.consented_at`` is ``NOT NULL`` because a destination row
only ever comes into being when somebody consents; the constraint and the row are
born together. This table is the other case: rows may already exist.

Backfilling ``consented_at = now()`` on them was rejected outright. Revision 0014
refused to invent a plausible-looking registrant on exactly this reasoning -- it
"would have made an unaudited consent look audited" -- and the argument is stronger
here, because the fabricated value would not merely mislabel a record, it would
manufacture the legal basis for a transfer that nobody agreed to.

So: nullable columns, and the enforcement is **fail-closed at the gate**. An
existing off-device configuration stops serving at the next request and the
household is told why, with a remedy, through the degradation banner that already
exists (``services/providers.py``). Nothing is deleted, nothing is disabled, and no
agreement is invented; the household re-grants when it chooses to, and the date is
then true.

The mode-tied CHECK -- "``byok`` and ``instance_owner`` rows must carry a consent",
the constraint that would make the wrong state unrepresentable rather than merely
unusable -- is deliberately **not** added here. Per the expand/migrate/contract rule,
it belongs with the registration route that can guarantee the invariant on insert,
and that route does not exist yet: nothing in ``api/`` creates an
``LlmProviderConfig`` today, and ``ProviderCredentialService.store_api_key`` is
written, unit-tested and unreachable. Adding the CHECK now would abort this
migration on any deployment holding a seeded configuration, to buy an invariant the
gate already enforces.

What *is* constrained here is the ordering, which is safe on any data: a revocation
cannot precede -- or exist without -- the consent it withdraws. Same name and same
reason as ``shopping_export_target``, but **not** the same predicate, and the
difference is the nullability above.

That table spells it ``consent_revoked_at IS NULL OR consent_revoked_at >=
consented_at``, which is sound there only because ``consented_at`` is ``NOT NULL``.
Copied here verbatim it would admit exactly the row it looks like it forbids: with
``consented_at`` NULL, ``consent_revoked_at >= NULL`` evaluates to NULL, and a CHECK
rejects only FALSE. "Withdrawn, having never agreed" would have been representable
under a constraint named for preventing it. The ``IS NOT NULL`` conjunct is what
makes the name true.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "llm_provider_config",
        sa.Column(
            "consented_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When this household agreed that its data may be sent to this "
                "third-party provider (RGPD art. 6(1)(a), art. 9(2)(a) for the "
                "health signals in a recipe prompt). NULL means no agreement on "
                "record: the provider is refused before any credential is "
                "decrypted. Never backfilled -- an invented date would manufacture "
                "the legal basis for a transfer nobody consented to. Not required "
                "for mode 'ollama', which transmits to nobody."
            ),
        ),
    )
    op.add_column(
        "llm_provider_config",
        sa.Column(
            "consent_revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the household withdrew that agreement. The row survives "
                "withdrawal so the household can still answer 'who did you send "
                "this to, and when did we agree?'. Takes effect at the next "
                "request, because consent is re-read on every provider load."
            ),
        ),
    )
    # The component, not the rendered name: `create_check_constraint` prepends the
    # template's `ck_llm_provider_config_` prefix.
    op.create_check_constraint(
        "revocation_follows_consent",
        "llm_provider_config",
        "consent_revoked_at IS NULL"
        " OR (consented_at IS NOT NULL AND consent_revoked_at >= consented_at)",
    )
    # Withdrawal has to be as cheap as the banner that reports it: the provider load
    # runs on every recipe suggestion and every receipt import, and it now reads
    # these two columns on the household's active rows.
    op.create_index(
        "ix_llm_provider_config_unconsented",
        "llm_provider_config",
        ["household_id"],
        postgresql_where=sa.text(
            "archived_at IS NULL AND (consented_at IS NULL OR consent_revoked_at IS NOT NULL)"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_llm_provider_config_unconsented", table_name="llm_provider_config")
    # The component, not the rendered name -- `drop_constraint` puts the template's
    # `ck_llm_provider_config_` prefix back on, exactly as `create_check_constraint`
    # did. Getting this wrong fails only when the downgrade is actually run, which is
    # the worst moment to discover it (revision 0013 learned this by running it).
    op.drop_constraint("revocation_follows_consent", "llm_provider_config", type_="check")
    op.drop_column("llm_provider_config", "consent_revoked_at")
    op.drop_column("llm_provider_config", "consented_at")
