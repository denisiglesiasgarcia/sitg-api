"""sitg_api.idc
=================

Indice de Dépense de Chaleur (IDC) — module d'accès et de validation.

Ce module fournit deux composants principaux :

- IDCSchema (hérite de :class:`dataframely.Schema`)
    - Déclare le schéma attendu des données (types, nullabilité, règles
        métier). Sert de source de vérité pour la validation des jeux de
        données IDC.

- IDCFetcher
    - Gère le cycle complet d'ingestion des données IDC : récupération
        paginée via l'API ArcGIS, vérification du contrat API, transformation
        et cast des champs, déduplication et validation finale avec
        :class:`IDCSchema`.

Conventions importantes
- Les règles métier (plages valides, contraintes) sont définies au
    niveau du module et réutilisées par les règles de :class:`IDCSchema`.
- La validation est effectuée via dataframely ; les erreurs de
    validation lèvent des exceptions explicites provenant de la librairie.
"""

import datetime
import logging
from collections.abc import Callable

import dataframely as dy
import polars as pl

from ._arcgis import fetch_all

logger = logging.getLogger(__name__)

# Plages valides — invariants métier IDC (module-level pour les règles dy.rule)
_ANNEE_MIN: int = 2000
_ANNEE_MAX: int = datetime.date.today().year + 1  # marge d'un an dans le futur
_NPA_MIN: int = 1000  # NPA suisse : 4 chiffres, Lausanne = 1000
_NPA_MAX: int = 9999  # NPA suisse : 4 chiffres max

# ---------------------------------------------------------------------------
# Schéma dataframely
# ---------------------------------------------------------------------------


class IDCSchema(dy.Schema):
    """
    Schéma déclaratif pour les données IDC retournées et transformées depuis l'API SITG.

    Chaque colonne déclare son type Polars et sa nullabilité.
    Les règles métier (@dy.rule) expriment des invariants physiques ou géographiques
    qui, s'ils sont violés, signalent une anomalie de saisie côté SITG.
    Ces règles sont évaluées via IDCSchema.filter() (soft validation) :
    les lignes invalides sont isolées, pas supprimées silencieusement.
    """

    # --- Identifiants ---
    egid = dy.Int64(nullable=False)
    annee = dy.Int64(nullable=False)

    # --- Indicateurs énergétiques ---
    indice = dy.Int64(nullable=False)
    sre = dy.Int64(nullable=False)

    # --- Adresse ---
    adresse = dy.String(nullable=False)
    npa = dy.Int64(nullable=False)
    commune = dy.String(nullable=False)
    destination = dy.String(nullable=False)

    # --- Agent énergétique 1 — obligatoire pour tout calcul IDC ---
    agent_energetique_1 = dy.String(nullable=False)
    quantite_agent_energetique_1 = dy.Float64(nullable=False)
    unite_agent_energetique_1 = dy.String(nullable=False)

    # --- Agents énergétiques 2 et 3 — absents si bâtiment mono-énergie ---
    agent_energetique_2 = dy.String(nullable=True)
    quantite_agent_energetique_2 = dy.Float64(nullable=True)
    unite_agent_energetique_2 = dy.String(nullable=True)
    agent_energetique_3 = dy.String(nullable=True)
    quantite_agent_energetique_3 = dy.Float64(nullable=True)
    unite_agent_energetique_3 = dy.String(nullable=True)

    # --- Dates de période et de saisie ---
    date_debut_periode = dy.Datetime(time_unit="ms", nullable=False)
    date_fin_periode = dy.Datetime(time_unit="ms", nullable=False)
    date_saisie = dy.Datetime(time_unit="ms", nullable=False)

    # --- Moyennes 2 et 3 ans — absentes si historique insuffisant ---
    indice_moy2 = dy.Int64(nullable=True)
    annees_concernees_moy_2 = dy.String(nullable=True)
    indice_moy3 = dy.Int64(nullable=True)
    annees_concernees_moy_3 = dy.String(nullable=True)

    # --- Métadonnées optionnelles ---
    nbre_preneur = dy.Int64(nullable=True)

    # --- Règles métier ---

    @dy.rule()
    def indice_positif(cls) -> pl.Expr:
        # IDC en kWh/m²/an — toujours strictement positif physiquement
        return pl.col("indice") > 0

    @dy.rule()
    def sre_positive(cls) -> pl.Expr:
        # SRE nulle ou négative rend le calcul IDC mathématiquement invalide
        return pl.col("sre") > 0

    @dy.rule()
    def annee_valide(cls) -> pl.Expr:
        # Hors plage = erreur de millésime ou donnée hors périmètre temporel attendu
        return pl.col("annee").is_between(_ANNEE_MIN, _ANNEE_MAX)

    @dy.rule()
    def npa_suisse(cls) -> pl.Expr:
        # NPA hors plage = adresse mal géocodée ou bâtiment hors Suisse
        return pl.col("npa").is_between(_NPA_MIN, _NPA_MAX)

    @dy.rule()
    def periode_coherente(cls) -> pl.Expr:
        # Période incohérente = les calculs d'IDC sur cet intervalle seraient faux
        return pl.col("date_debut_periode") < pl.col("date_fin_periode")


class IDCFetcher:
    """
    Fetcher pour les données IDC de l'API SITG (SCANE_INDICE_MOYENNES_3_ANS).

    Responsabilités
    ---------------
    - Fetch paginé par chunks d'EGIDs (évite les URLs trop longues → erreur 406)
    - Contrôle du contrat API (colonnes retournées vs attendues)
    - Cast et transformation des types Polars
    - Déduplication par (egid, annee)
    - Validation dataframely via IDCSchema.filter() (soft — isole les invalides)
    - Logging structuré à chaque étape
    """

    # Colonnes attendues dans le retour brut de l'API (majuscules) — contrat API
    _API_COLUMNS: list[str] = [
        "EGID",
        "ANNEE",
        "INDICE",
        "SRE",
        "ADRESSE",
        "NPA",
        "COMMUNE",
        "DESTINATION",
        "AGENT_ENERGETIQUE_1",
        "QUANTITE_AGENT_ENERGETIQUE_1",
        "UNITE_AGENT_ENERGETIQUE_1",
        "AGENT_ENERGETIQUE_2",
        "QUANTITE_AGENT_ENERGETIQUE_2",
        "UNITE_AGENT_ENERGETIQUE_2",
        "AGENT_ENERGETIQUE_3",
        "QUANTITE_AGENT_ENERGETIQUE_3",
        "UNITE_AGENT_ENERGETIQUE_3",
        "DATE_DEBUT_PERIODE",
        "DATE_FIN_PERIODE",
        "DATE_SAISIE",
        "INDICE_MOY2",
        "ANNEES_CONCERNEES_MOY_2",
        "INDICE_MOY3",
        "ANNEES_CONCERNEES_MOY_3",
        "NBRE_PRENEUR",
    ]

    # Colonnes en sortie (minuscules) — doit correspondre aux champs d'IDCSchema
    _RESULT_COLUMNS: list[str] = [c.lower() for c in _API_COLUMNS]

    # Mapping renommage API → sortie
    _RENAME: dict[str, str] = {c: c.lower() for c in _API_COLUMNS}

    URL_DEFAULT: str = (
        "https://vector.sitg.ge.ch/arcgis/rest/services/"
        "SCANE_INDICE_MOYENNES_3_ANS/FeatureServer/0/query"
    )

    def __init__(
        self,
        url: str = URL_DEFAULT,
        chunk_size: int = 1000,
        egid_chunk_size: int = 50,
    ) -> None:
        """
        Paramètres
        ----------
        url             : endpoint ArcGIS (surchargeable pour tests/staging)
        chunk_size      : taille des pages API (pagination interne ArcGIS)
        egid_chunk_size : nb max d'EGIDs par clause WHERE IN
        """
        self.url = url
        self.chunk_size = chunk_size
        self.egid_chunk_size = egid_chunk_size

    def fetch(
        self,
        egid: int | list[int],
        *,
        progress_cb: Callable[[float], None] | None = None,
        status_cb: Callable[[str], None] | None = None,
    ) -> tuple[dy.DataFrame[IDCSchema], dy.FailureInfo]:
        """
        Récupère, transforme et valide les données IDC pour un ou plusieurs EGIDs.

        Retourne un tuple (df_valide, failures).
        Lève RuntimeError si aucune donnée, ValueError si le contrat API est rompu.
        """
        egid_list = list(set(egid)) if isinstance(egid, list) else [egid]
        logger.debug("IDCFetcher.fetch : %d EGIDs uniques demandés", len(egid_list))

        features = self._fetch_raw(
            egid_list, progress_cb=progress_cb, status_cb=status_cb
        )

        # features may be None or contain None entries; guard against that
        safe_features = features or []
        df_raw = pl.from_dicts(
            data=[f["attributes"] for f in safe_features if f and "attributes" in f],
            infer_schema_length=None,
        )

        # Niveau 1 : contrat API — bloquant
        self._check_api_columns(df_raw)

        df = self._transform(df_raw)
        df, n_dedup = self._dedup(df)

        if n_dedup:
            logger.info(
                "IDCFetcher.fetch : %d doublon(s) supprimé(s) "
                "(même EGID + même année, entrée la plus récente conservée)",
                n_dedup,
            )

        self._log_egid_coverage(df, set(egid_list))

        # Niveau 2 : validation dataframely — retour direct du FilterResult sans déballer
        df_valid, failures = IDCSchema.filter(df, cast=False)

        if len(failures):
            logger.warning(
                "IDCFetcher.fetch : %d ligne(s) invalide(s) selon IDCSchema — %s",
                len(failures),
                failures.counts(),
            )

        logger.info(
            "IDCFetcher.fetch : %d lignes valides / %d EGIDs couverts / %d demandés",
            len(df_valid),
            df_valid["egid"].n_unique(),
            len(egid_list),
        )

        return df_valid, failures

    # --- Méthodes privées ---

    def _fetch_raw(
        self,
        egid_list: list[int],
        *,
        progress_cb: Callable[[float], None] | None,
        status_cb: Callable[[str], None] | None,
    ) -> list[dict]:
        """
        Appels API paginés, découpés en chunks d'EGIDs.
        Lève RuntimeError si aucun résultat retourné.
        """
        chunks = [
            egid_list[i : i + self.egid_chunk_size]
            for i in range(0, len(egid_list), self.egid_chunk_size)
        ]

        features: list[dict] = []
        for chunk in chunks:
            where = (
                f"EGID IN ({','.join(map(str, chunk))})"
                if len(chunk) > 1
                else f"EGID={chunk[0]}"
            )
            chunk_features = fetch_all(
                self.url,
                fields=",".join(self._API_COLUMNS),
                where=where,
                chunk_size=self.chunk_size,
                progress_cb=progress_cb,
                status_cb=status_cb,
            )
            if chunk_features:
                features.extend(chunk_features)

        if not features:
            raise RuntimeError(
                f"Aucune donnée IDC pour les {len(egid_list)} EGIDs demandés — "
                "bâtiments non assujettis à l'IDC ou hors périmètre Genève ?"
            )

        return features

    def _check_api_columns(self, df: pl.DataFrame) -> None:
        """
        Vérifie que les colonnes retournées correspondent exactement à _API_COLUMNS.

        Lève ValueError avec le détail des colonnes manquantes / inattendues.
        Toute divergence indique un changement de contrat côté SITG non répercuté
        dans _API_COLUMNS et IDCSchema — correction manuelle requise.
        """
        returned = set(df.columns)
        expected = set(self._API_COLUMNS)

        missing = expected - returned
        extra = returned - expected

        messages: list[str] = []
        if missing:
            messages.append(f"colonnes manquantes : {sorted(missing)}")
        if extra:
            # Colonne inconnue = schéma étendu côté SITG, à évaluer pour intégration
            messages.append(f"colonnes inattendues : {sorted(extra)}")

        if messages:
            raise ValueError(f"Contrat API rompu — {' | '.join(messages)}")

    def _transform(self, df_raw: pl.DataFrame) -> pl.DataFrame:
        """
        Renommage majuscules → minuscules, sélection et cast des types Polars.
        Les timestamps ArcGIS arrivent en ms epoch (Int64) → Datetime("ms").
        """
        return (
            df_raw
            .rename(self._RENAME)
            .select(self._RESULT_COLUMNS)
            .with_columns(
                pl.col(
                    "egid",
                    "annee",
                    "indice",
                    "sre",
                    "npa",
                    "indice_moy2",
                    "indice_moy3",
                    "nbre_preneur",
                ).cast(pl.Int64),
                pl.col(
                    "quantite_agent_energetique_1",
                    "quantite_agent_energetique_2",
                    "quantite_agent_energetique_3",
                ).cast(pl.Float64),
                pl.col(
                    "date_debut_periode",
                    "date_fin_periode",
                    "date_saisie",
                ).cast(pl.Datetime("ms")),
            )
        )

    def _dedup(self, df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        """
        Déduplique par (egid, annee), conserve l'entrée la plus récente (date_saisie).
        Retourne le DataFrame dédupliqué et le nombre de lignes supprimées.
        """
        n_before = len(df)
        df_dedup = (
            df
            .sort(["egid", "annee", "date_saisie"], descending=[False, False, True])
            .unique(subset=["egid", "annee"], keep="first")
            .sort(["egid", "annee"])
        )
        return df_dedup, n_before - len(df_dedup)

    def _log_egid_coverage(
        self, df: pl.DataFrame, egid_set_requested: set[int]
    ) -> None:
        """
        Log les EGIDs demandés sans données IDC retournées.
        Causes légitimes : bâtiment exempté, non assujetti, hors périmètre IDC.
        """
        egids_returned = set(df["egid"].to_list())
        egids_missing = egid_set_requested - egids_returned
        if egids_missing:
            logger.warning(
                "IDCFetcher : %d EGID(s) sans données IDC : %s",
                len(egids_missing),
                sorted(egids_missing),
            )


def fetch_idc_data(
    egid: int | list[int],
    *,
    url: str = IDCFetcher.URL_DEFAULT,
    chunk_size: int = 1000,
    egid_chunk_size: int = 50,
    progress_cb: Callable[[float], None] | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> tuple[dy.DataFrame[IDCSchema], dy.FailureInfo] | None:

    fetcher = IDCFetcher(
        url=url,
        chunk_size=chunk_size,
        egid_chunk_size=egid_chunk_size,
    )
    return fetcher.fetch(egid, progress_cb=progress_cb, status_cb=status_cb)
