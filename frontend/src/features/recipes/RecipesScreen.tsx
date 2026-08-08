import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { getBalance, getSuggestionQuality, suggestRecipes } from '../../api/endpoints';
import type {
  AppliedConstraints,
  HouseholdMember,
  MealTemperature,
  RecipeSuggestion,
  StorageLocation,
  SuggestionQuality,
  WeeklyBalance,
} from '../../api/types';
import { Badge, Button, Callout, Chip, ChipRow, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { useCapabilities } from '../../context/capabilitiesContext';
import type { MembersState } from '../../hooks/useMembers';
import { ALLERGEN_LABELS, DIET_LABELS, TEXTURE_LABELS, unionConstraints } from '../../lib/dietary';
import { readMealTemperature, writeMealTemperature } from '../../lib/preferences';
import { BalancePanel } from './BalancePanel';
import { CookingForPanel } from './CookingForPanel';
import { FeedbackControls } from './FeedbackControls';
import { QualityPanel } from './QualityPanel';
import { AllergenAssessmentPanel, ExpiryPressurePanel, PreparationPanel } from './RecipeSafety';
import { ProviderSetup } from './ProviderSetup';
import styles from './Recipes.module.css';

interface Props {
  locations: StorageLocation[];
  members: MembersState;
}

function RecipeCard({
  recipe,
  requestedTemperature,
  onFeedback,
}: {
  recipe: RecipeSuggestion;
  requestedTemperature: MealTemperature;
  onFeedback: () => void;
}) {
  const missing = recipe.ingredients.filter((ingredient) => !ingredient.in_stock);

  return (
    <article className={styles.card} aria-labelledby={`recipe-${recipe.id}`}>
      <header className={styles.cardHeader}>
        {/* h2: the screen heading is the only h1, so levels must not skip. */}
        <h2 className={styles.title} id={`recipe-${recipe.id}`}>
          {recipe.title}
        </h2>
        {recipe.summary ? <p className={styles.summary}>{recipe.summary}</p> : null}
        <div className={styles.facts}>
          {recipe.duration_minutes !== null ? (
            <Badge tone="neutral">{recipe.duration_minutes} min</Badge>
          ) : null}
          {recipe.servings !== null ? (
            <Badge tone="neutral">
              {recipe.servings} portion{recipe.servings > 1 ? 's' : ''}
            </Badge>
          ) : null}
          {recipe.uses_expiring_soon ? <Badge tone="warn">utilise du périssable</Badge> : null}
          {missing.length === 0 ? (
            <Badge tone="ok">tout est en stock</Badge>
          ) : (
            <Badge tone="warn">
              {missing.length} ingrédient{missing.length > 1 ? 's' : ''} manquant
              {missing.length > 1 ? 's' : ''}
            </Badge>
          )}
        </div>
      </header>

      <AllergenAssessmentPanel assessment={recipe.allergen_assessment} />
      <PreparationPanel preparation={recipe.preparation} requested={requestedTemperature} />
      <ExpiryPressurePanel pressure={recipe.expiry_pressure} />

      <div>
        <p className={styles.sectionTitle}>Ingrédients</p>
        <ul className={styles.ingredients}>
          {recipe.ingredients.map((ingredient, index) => (
            <li
              key={`${recipe.id}-${String(index)}-${ingredient.name}`}
              className={[styles.ingredient, ingredient.in_stock ? '' : styles.ingredientMissing]
                .filter(Boolean)
                .join(' ')}
            >
              <span>
                {ingredient.name}
                {ingredient.in_stock ? null : (
                  <>
                    {' '}
                    <Badge tone="warn">à acheter</Badge>
                  </>
                )}
              </span>
              <span className={styles.ingredientAmount}>
                {[ingredient.amount, ingredient.unit].filter(Boolean).join(' ')}
              </span>
            </li>
          ))}
        </ul>
      </div>

      <div>
        <p className={styles.sectionTitle}>Préparation</p>
        <ol className={styles.steps}>
          {recipe.steps.map((step, index) => (
            <li key={`${recipe.id}-step-${String(index)}`}>{step}</li>
          ))}
        </ol>
      </div>

      {/* Last on the card, and deliberately below the allergen statement rather
          than above it: nothing may push a safety sentence further from the title
          it qualifies. A verdict also reads more naturally once the steps have
          been seen. */}
      <FeedbackControls suggestionId={recipe.id} title={recipe.title} onRecorded={onFeedback} />
    </article>
  );
}

/** What the server actually applied, once it has answered. */
function AppliedConstraintsSummary({ applied }: { applied: AppliedConstraints }) {
  return (
    <div className={styles.applied}>
      <p className={styles.blockHead}>
        <span className={styles.blockTitle}>Contraintes appliquées par le serveur</span>
      </p>
      <ul className={styles.blockList}>
        <li>
          <span className={styles.blockLabel}>Membres</span>
          {applied.members.length === 0
            ? 'tout le foyer'
            : applied.members.map((member) => member.display_name).join(', ')}
        </li>
        <li>
          <span className={styles.blockLabel}>Allergènes exclus</span>
          {applied.excluded_allergens.length === 0
            ? 'aucun'
            : applied.excluded_allergens.map((code) => ALLERGEN_LABELS[code]).join(', ')}
        </li>
        <li>
          <span className={styles.blockLabel}>Régime</span>
          {applied.diet === null ? 'aucun' : DIET_LABELS[applied.diet]}
        </li>
        {applied.infant_texture !== null ? (
          <li>
            <span className={styles.blockLabel}>Texture</span>
            {TEXTURE_LABELS[applied.infant_texture]}
          </li>
        ) : null}
        <li>
          <span className={styles.blockLabel}>Produits écartés</span>
          {applied.products_withheld} retiré{applied.products_withheld > 1 ? 's' : ''} par les
          filtres, dont {applied.products_unverified} sans données allergènes.
        </li>
      </ul>
    </div>
  );
}

export function RecipesScreen({ locations, members }: Props) {
  const { capabilities } = useCapabilities();
  const [notes, setNotes] = useState('');
  const [maxSuggestions, setMaxSuggestions] = useState(3);
  const [selectedLocations, setSelectedLocations] = useState<string[]>([]);
  // Member selection is health-adjacent and stays in memory: never a query
  // string, never localStorage.
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [mealTemperature, setMealTemperature] = useState<MealTemperature>(readMealTemperature);
  const [suggestions, setSuggestions] = useState<RecipeSuggestion[] | null>(null);
  const [applied, setApplied] = useState<AppliedConstraints | null>(null);
  const [balance, setBalance] = useState<WeeklyBalance | null>(null);
  const [quality, setQuality] = useState<SuggestionQuality | null>(null);
  const [askedTemperature, setAskedTemperature] = useState<MealTemperature>('any');
  const [model, setModel] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const [notConfigured, setNotConfigured] = useState(false);

  // Dashboard balance, before any suggestion is asked for. A server without the
  // route simply shows nothing.
  useEffect(() => {
    const controller = new AbortController();
    getBalance(controller.signal)
      .then(setBalance)
      .catch(() => {
        /* absent or unreachable: the panel is optional */
      });
    return () => {
      controller.abort();
    };
  }, []);

  // What the household's own answers have measured so far. Refreshed after every
  // verdict so the panel and the card can never disagree about the last tap.
  const refreshQuality = useCallback((signal?: AbortSignal) => {
    getSuggestionQuality(signal)
      .then(setQuality)
      .catch(() => {
        /* absent or unreachable: the panel is optional, like the balance one */
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    refreshQuality(controller.signal);
    return () => {
      controller.abort();
    };
  }, [refreshQuality]);

  const effectiveMembers: HouseholdMember[] = useMemo(
    () =>
      selectedMembers.length === 0
        ? members.members
        : members.members.filter((member) => selectedMembers.includes(member.id)),
    [members.members, selectedMembers],
  );

  const constraints = useMemo(() => unionConstraints(effectiveMembers), [effectiveMembers]);

  const toggleLocation = (id: string) => {
    setSelectedLocations((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );
  };

  const chooseTemperature = (value: MealTemperature) => {
    setMealTemperature(value);
    writeMealTemperature(value);
  };

  const run = () => {
    setLoading(true);
    setError(null);
    setBlocked(false);
    setNotConfigured(false);
    setAskedTemperature(mealTemperature);

    suggestRecipes({
      location_ids: selectedLocations,
      max_suggestions: maxSuggestions,
      notes: notes.trim(),
      member_ids: selectedMembers,
      balance_mode: 'weekly',
      meal_temperature: mealTemperature,
    })
      .then((response) => {
        setSuggestions(response.suggestions);
        setApplied(response.applied_constraints ?? null);
        if (response.balance) setBalance(response.balance);
        setModel(response.model);
      })
      .catch((cause: unknown) => {
        // 409 provider-not-configured is a configuration state, not a failure —
        // the contract requires a setup screen rather than a raw error.
        if (
          cause instanceof ApiError &&
          cause.status === 409 &&
          (cause.problemType === 'provider-not-configured' || cause.problemType === null)
        ) {
          setNotConfigured(true);
          return;
        }
        // The filtered stock allowed nothing. Not an execution failure: the
        // interface explains which constraints emptied the inventory.
        if (cause instanceof ApiError && cause.problemType === 'no-suggestion-within-constraints') {
          setSuggestions([]);
          setBlocked(true);
          return;
        }
        setError(describeError(cause));
      })
      .finally(() => {
        setLoading(false);
      });
  };

  const providerMissing = notConfigured || capabilities?.configured === false;

  return (
    <section className={styles.screen} aria-labelledby="recipes-heading">
      <h1 className={styles.heading} id="recipes-heading">
        Que cuisiner ?
      </h1>
      <p className={styles.lead}>
        Les propositions sont construites à partir de ce qui est réellement en stock, en
        privilégiant ce qui périme bientôt.
      </p>

      {providerMissing ? (
        <ProviderSetup capabilities={capabilities} />
      ) : (
        <>
          <div className={styles.controls}>
            <ChipRow label="Limiter à certains emplacements">
              {locations.map((location) => (
                <Chip
                  key={location.id}
                  active={selectedLocations.includes(location.id)}
                  onClick={() => {
                    toggleLocation(location.id);
                  }}
                >
                  {location.name}
                </Chip>
              ))}
            </ChipRow>

            {members.unsupported ? (
              <Callout tone="info" title="Contraintes alimentaires indisponibles">
                <p>
                  Ce serveur ne gère pas encore les membres du foyer : les suggestions ne tiennent
                  compte d’aucune allergie ni d’aucun régime.
                </p>
              </Callout>
            ) : (
              <CookingForPanel
                members={members.members}
                selectedIds={selectedMembers}
                effective={effectiveMembers}
                constraints={constraints}
                mealTemperature={mealTemperature}
                onToggleMember={(id, checked) => {
                  setSelectedMembers((current) =>
                    checked ? [...current, id] : current.filter((value) => value !== id),
                  );
                }}
                onMealTemperature={chooseTemperature}
              />
            )}

            <Field
              label="Contraintes"
              hint="Par exemple : rapide, sans four, pour deux. Transmis au modèle comme préférence."
            >
              {({ id, describedBy }) => (
                <input
                  id={id}
                  aria-describedby={describedBy}
                  className={controlClass()}
                  type="text"
                  value={notes}
                  onChange={(event) => {
                    setNotes(event.target.value);
                  }}
                />
              )}
            </Field>

            <Field label="Nombre de propositions">
              {({ id, describedBy }) => (
                <select
                  id={id}
                  aria-describedby={describedBy}
                  className={controlClass()}
                  value={maxSuggestions}
                  onChange={(event) => {
                    setMaxSuggestions(Number(event.target.value));
                  }}
                >
                  {[1, 2, 3, 4, 5].map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              )}
            </Field>

            <Button variant="primary" block loading={loading} onClick={run}>
              Proposer des recettes
            </Button>
          </div>

          <div aria-live="polite" aria-busy={loading}>
            {loading ? (
              <p className={styles.lead}>Recherche de recettes à partir de votre stock…</p>
            ) : error ? (
              <Callout tone="danger" title="Suggestions indisponibles">
                <p>{error}</p>
                <Button variant="secondary" onClick={run}>
                  Réessayer
                </Button>
              </Callout>
            ) : blocked ? (
              <Callout tone="info" title="Aucune recette ne respecte ces contraintes">
                <p>
                  Le stock filtré ne permet aucune suggestion. Les contraintes dures ont retiré trop
                  de produits :{' '}
                  {constraints.allergens.length > 0
                    ? `allergènes exclus (${constraints.allergens.map((code) => ALLERGEN_LABELS[code]).join(', ')})`
                    : 'aucun allergène exclu'}
                  {constraints.diet === null ? '' : `, régime ${DIET_LABELS[constraints.diet]}`}
                  {constraints.infantTexture === null
                    ? ''
                    : `, texture ${TEXTURE_LABELS[constraints.infantTexture]}`}
                  . Sélectionnez moins de membres, ou ajoutez des articles au stock.
                </p>
              </Callout>
            ) : suggestions === null ? (
              <p className={styles.lead}>Aucune suggestion demandée pour l’instant.</p>
            ) : suggestions.length === 0 ? (
              <Callout tone="info" title="Aucune recette proposée">
                <p>
                  Le stock actuel n’a pas permis de composer une recette. Ajoutez quelques articles
                  ou assouplissez les contraintes.
                </p>
              </Callout>
            ) : (
              <p className={styles.lead}>
                {suggestions.length} proposition{suggestions.length > 1 ? 's' : ''}
                {model ? ` — modèle ${model}` : ''}.
              </p>
            )}
          </div>

          {applied ? <AppliedConstraintsSummary applied={applied} /> : null}
          {balance ? <BalancePanel balance={balance} /> : null}

          {suggestions && suggestions.length > 0 ? (
            <div className={styles.results}>
              {suggestions.map((recipe) => (
                <RecipeCard
                  key={recipe.id}
                  recipe={recipe}
                  requestedTemperature={askedTemperature}
                  onFeedback={refreshQuality}
                />
              ))}
            </div>
          ) : null}

          {quality ? <QualityPanel quality={quality} /> : null}
        </>
      )}
    </section>
  );
}
