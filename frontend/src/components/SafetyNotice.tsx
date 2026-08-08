import styles from './SafetyNotice.module.css';

/**
 * The two standing warnings of ADR-0009.
 *
 * Neither can be dismissed, and neither remembers anything: there is no close
 * button, no "do not show again", no persisted flag. A warning that can be
 * turned off is read once, by the person who set the app up, and never by the
 * grandparent cooking on a Sunday. They are rendered wherever the matching
 * information is entered or displayed, every time.
 */

export function AllergenDisclaimer() {
  return (
    <div className={styles.notice} role="note">
      <p className={styles.title}>
        <span className={styles.glyph} aria-hidden="true">
          !
        </span>
        Les données allergènes ne sont pas une garantie médicale
      </p>
      <p>
        Elles viennent d’Open Food Facts — un wiki alimenté par la communauté — et de ce que vous
        saisissez ici. Un produit peut n’avoir aucune donnée, ou une donnée fausse. En cas
        d’allergie sévère, lisez l’emballage : Chaudron ne remplace pas cette lecture.
      </p>
    </div>
  );
}

export function InfantDisclaimer() {
  return (
    <div className={styles.notice} role="note">
      <p className={styles.title}>
        <span className={styles.glyph} aria-hidden="true">
          !
        </span>
        Mode nourrisson : ces règles ne remplacent pas un pédiatre
      </p>
      <p>
        La diversification alimentaire se discute avec un professionnel de santé. Chaudron applique
        une table d’aliments déconseillés par tranche d’âge ; elle ne connaît ni votre enfant, ni
        son histoire médicale.
      </p>
    </div>
  );
}
