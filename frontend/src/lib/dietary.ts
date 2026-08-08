/**
 * Labels and constraint arithmetic for the dietary vocabularies of contract
 * v1.1. The codes themselves live in `api/types.ts` because they are wire
 * values; only their French presentation and the union rules live here.
 */

import type {
  AgeBand,
  AllergenCode,
  Diet,
  HouseholdMember,
  InfantTexture,
  MealTemperature,
  ServingTemperature,
} from '../api/types';
import { ALLERGEN_CODES } from '../api/types';

export const ALLERGEN_LABELS: Record<AllergenCode, string> = {
  gluten: 'Céréales contenant du gluten',
  crustaceans: 'Crustacés',
  eggs: 'Œufs',
  fish: 'Poissons',
  peanuts: 'Arachides',
  soybeans: 'Soja',
  milk: 'Lait',
  nuts: 'Fruits à coque',
  celery: 'Céleri',
  mustard: 'Moutarde',
  sesame: 'Sésame',
  sulphites: 'Anhydride sulfureux et sulfites',
  lupin: 'Lupin',
  molluscs: 'Mollusques',
};

export const DIET_LABELS: Record<Diet, string> = {
  omnivore: 'Omnivore',
  pescatarian: 'Pescétarien',
  vegetarian: 'Végétarien',
  vegan: 'Végétalien',
};

export const AGE_BAND_LABELS: Record<AgeBand, string> = {
  adult: 'Adulte',
  child: 'Enfant',
  infant_4_6m: 'Nourrisson — 4 à 6 mois',
  infant_6_9m: 'Nourrisson — 6 à 9 mois',
  infant_9_12m: 'Nourrisson — 9 à 12 mois',
  infant_12_36m: 'Nourrisson — 12 à 36 mois',
};

export const TEXTURE_LABELS: Record<InfantTexture, string> = {
  smooth: 'Lisse — texture mixée',
  soft_pieces: 'Petits morceaux fondants',
  pieces: 'Morceaux',
};

export const MEAL_TEMPERATURE_LABELS: Record<MealTemperature, string> = {
  any: 'Indifférent',
  hot: 'Plutôt chaud',
  cold: 'Plutôt froid',
};

export const SERVING_TEMPERATURE_LABELS: Record<ServingTemperature, string> = {
  hot: 'se sert chaud',
  cold: 'se sert froid',
  either: 'chaud ou froid',
};

export const AGE_BANDS: AgeBand[] = [
  'adult',
  'child',
  'infant_4_6m',
  'infant_6_9m',
  'infant_9_12m',
  'infant_12_36m',
];

export const DIETS: Diet[] = ['omnivore', 'pescatarian', 'vegetarian', 'vegan'];

export const TEXTURES: InfantTexture[] = ['smooth', 'soft_pieces', 'pieces'];

export function isInfantBand(band: AgeBand): boolean {
  return band.startsWith('infant_');
}

/** Strictest wins: an omnivore eating with a vegan eats vegan for that meal. */
const DIET_RANK: Record<Diet, number> = {
  omnivore: 0,
  pescatarian: 1,
  vegetarian: 2,
  vegan: 3,
};

/** Strictest wins here too: smooth is the safest texture of the three. */
const TEXTURE_RANK: Record<InfantTexture, number> = {
  smooth: 0,
  soft_pieces: 1,
  pieces: 2,
};

export interface FreeTextPreference {
  memberId: string;
  memberName: string;
  text: string;
}

/**
 * What applies when cooking for these members. Hard constraints (allergens,
 * diet, texture) are filters; `preferences` are not — they only reach the
 * prompt, and the interface must say so.
 */
export interface UnionConstraints {
  allergens: AllergenCode[];
  diet: Diet | null;
  infantTexture: InfantTexture | null;
  ageBands: AgeBand[];
  hasInfant: boolean;
  preferences: FreeTextPreference[];
}

export function unionConstraints(members: HouseholdMember[]): UnionConstraints {
  const allergens = new Set<AllergenCode>();
  const ageBands = new Set<AgeBand>();
  const preferences: FreeTextPreference[] = [];
  let diet: Diet | null = null;
  let texture: InfantTexture | null = null;

  for (const member of members) {
    for (const code of member.allergens) allergens.add(code);
    ageBands.add(member.age_band);
    if (diet === null || DIET_RANK[member.diet] > DIET_RANK[diet]) diet = member.diet;
    if (
      member.infant_texture !== null &&
      (texture === null || TEXTURE_RANK[member.infant_texture] < TEXTURE_RANK[texture])
    ) {
      texture = member.infant_texture;
    }
    const text = member.free_text_restrictions.trim();
    if (text !== '') {
      preferences.push({ memberId: member.id, memberName: member.display_name, text });
    }
  }

  return {
    // Contract order, so two households never read the same list differently.
    allergens: ALLERGEN_CODES.filter((code) => allergens.has(code)),
    diet,
    infantTexture: texture,
    ageBands: AGE_BANDS.filter((band) => ageBands.has(band)),
    hasInfant: [...ageBands].some(isInfantBand),
    preferences,
  };
}

/**
 * French terms that name a regulated allergen, normalised (lower case, no
 * diacritics). Matching is by whole word: "laitue" must not trigger "lait".
 *
 * `lactose` is deliberately absent from the milk terms — a lactose intolerance
 * is not the milk allergen, and the contract sends it to the free-text field on
 * purpose. Telling that user to tick "Lait" would be wrong advice.
 */
const ALLERGEN_TERMS: Record<AllergenCode, string[]> = {
  gluten: ['gluten', 'ble', 'froment', 'seigle', 'orge', 'epeautre', 'kamut', 'avoine'],
  crustaceans: ['crustace', 'crevette', 'crabe', 'homard', 'langoustine', 'ecrevisse', 'gambas'],
  eggs: ['oeuf', 'oeufs', 'ovoproduit'],
  fish: ['poisson', 'saumon', 'thon', 'cabillaud', 'morue', 'anchois', 'hareng', 'maquereau'],
  peanuts: ['arachide', 'cacahuete', 'cacahouete', 'peanut'],
  soybeans: ['soja', 'soya', 'tofu', 'edamame'],
  milk: ['lait', 'laitier', 'laitiere', 'caseine', 'lactoserum', 'fromage', 'beurre'],
  nuts: ['noix', 'noisette', 'amande', 'pistache', 'cajou', 'macadamia', 'pecan', 'coque'],
  celery: ['celeri'],
  mustard: ['moutarde'],
  sesame: ['sesame', 'tahini'],
  sulphites: ['sulfite', 'sulfureux', 'anhydride'],
  lupin: ['lupin'],
  molluscs: [
    'mollusque',
    'moule',
    'huitre',
    'calamar',
    'encornet',
    'seiche',
    'poulpe',
    'palourde',
    'coquillage',
    'bulot',
  ],
};

function normalise(value: string): string {
  return (
    value
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .toLowerCase()
      // NFD leaves the ligatures intact, and the tokeniser would eat them.
      .replace(/\u0153/g, 'oe')
      .replace(/\u00e6/g, 'ae')
  );
}

/**
 * Allergens named in free text. The field cannot filter anything — the system
 * has no idea which products contain coriander — so a regulated allergen typed
 * here is silently ineffective. The interface warns and offers the checkbox.
 */
export function detectAllergenTerms(text: string): AllergenCode[] {
  const words = normalise(text)
    .split(/[^a-z0-9]+/)
    .filter((word) => word.length > 2);
  if (words.length === 0) return [];

  const candidates = new Set<string>();
  for (const word of words) {
    candidates.add(word);
    // Plurals: "arachides" -> "arachide". "noix" is invariant and listed as is.
    if (word.endsWith('s')) candidates.add(word.slice(0, -1));
  }

  return ALLERGEN_CODES.filter((code) => ALLERGEN_TERMS[code].some((term) => candidates.has(term)));
}

export function formatAllergenList(codes: AllergenCode[]): string {
  return codes.map((code) => ALLERGEN_LABELS[code].toLowerCase()).join(', ');
}
