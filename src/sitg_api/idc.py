"""
Module IDC — Indice de Dépense de Chaleur.

Fournit :
  - EXPECTED_SCHEMA, NULLABLE_COLUMNS : référence du schéma
  - validate_schema()                  : vérification des types Polars
  - fetch_idc_data()                   : fetch + cast des types + déduplication
"""

import logging
from collections.abc import Callable

import polars as pl

from ._arcgis import fetch_all

logger = logging.getLogger(__name__)

# URL par défaut — surchargeable à l'appel
URL_IDC = (
    "https://vector.sitg.ge.ch/arcgis/rest/services/"
    "SCANE_INDICE_MOYENNES_3_ANS/FeatureServer/0/query"
)

# Colonnes telles que retournées par l'API (majuscules)
_API_COLUMNS = [
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
    "ID_CONCESSIONNAIRE",
    "NBRE_PRENEUR",
]

# Colonnes en sortie (minuscules) — source de vérité pour le schéma
RESULT_COLUMNS = [c.lower() for c in _API_COLUMNS]

# Mapping renommage API → sortie
_RENAME = {c: c.lower() for c in _API_COLUMNS}

# Schéma attendu après transformation — source de vérité
EXPECTED_SCHEMA: dict[str, pl.DataType] = {
    "egid": pl.Int64,
    "annee": pl.Int64,
    "indice": pl.Int64,
    "sre": pl.Int64,
    "adresse": pl.String,
    "npa": pl.Int64,
    "commune": pl.String,
    "destination": pl.String,
    "agent_energetique_1": pl.String,
    "quantite_agent_energetique_1": pl.Float64,
    "unite_agent_energetique_1": pl.String,
    "agent_energetique_2": pl.String,
    "quantite_agent_energetique_2": pl.Float64,
    "unite_agent_energetique_2": pl.String,
    "agent_energetique_3": pl.String,
    "quantite_agent_energetique_3": pl.Float64,
    "unite_agent_energetique_3": pl.String,
    "date_debut_periode": pl.Datetime("ms"),
    "date_fin_periode": pl.Datetime("ms"),
    "date_saisie": pl.Datetime("ms"),
    "indice_moy2": pl.Int64,
    "annees_concernees_moy_2": pl.String,
    "indice_moy3": pl.Int64,
    "annees_concernees_moy_3": pl.String,
    "id_concessionnaire": pl.Int64,
    "nbre_preneur": pl.Int64,
}

# Colonnes pouvant être null sans déclencher d'erreur de schéma
NULLABLE_COLUMNS: frozenset[str] = frozenset({
    "agent_energetique_2",
    "quantite_agent_energetique_2",
    "unite_agent_energetique_2",
    "agent_energetique_3",
    "quantite_agent_energetique_3",
    "unite_agent_energetique_3",
    "indice_moy2",
    "annees_concernees_moy_2",
    "indice_moy3",
    "annees_concernees_moy_3",
    "id_concessionnaire",
    "nbre_preneur",
})


def validate_schema(df: pl.DataFrame) -> list[str]:
    """
    Vérifie que le DataFrame respecte EXPECTED_SCHEMA.
    Retourne une liste d'erreurs (vide = OK).
    """
    errors: list[str] = []
    for col, expected_dtype in EXPECTED_SCHEMA.items():
        if col not in df.columns:
            errors.append(f"{col}: colonne absente")
            continue
        actual = df[col].dtype
        if actual == pl.Null and col in NULLABLE_COLUMNS:
            continue
        # Datetime : ms et us sont équivalents ici
        if isinstance(expected_dtype, pl.Datetime) and isinstance(actual, pl.Datetime):
            continue
        if actual != expected_dtype:
            errors.append(f"{col}: attendu {expected_dtype}, obtenu {actual}")
    return errors


def fetch_idc_data(
    egid: int | list[int],
    *,
    url: str = URL_IDC,
    chunk_size: int = 1000,
    egid_chunk_size: int = 50,  # nb max d'EGIDs par clause IN
    progress_cb: Callable[[float], None] | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> pl.DataFrame | None:
    """
    Récupère les données IDC pour un ou plusieurs EGIDs.

    Retourne un DataFrame Polars nettoyé et dédupliqué,
    ou None si aucune donnée ou erreur réseau.

    Paramètres
    ----------
    egid        : EGID unique ou liste d'EGIDs
    url         : endpoint ArcGIS (surchargeable pour tests/staging)
    chunk_size  : taille des pages API
    progress_cb : callback(float 0→1)
    status_cb   : callback(str) pour messages
    """
    # Normalisation en liste unique (dédoublonnage avant envoi)
    egid_list = list(set(egid)) if isinstance(egid, list) else [egid]

    # Découpage en chunks pour éviter les URLs trop longues → erreur 406
    chunks = [
        egid_list[i : i + egid_chunk_size]
        for i in range(0, len(egid_list), egid_chunk_size)
    ]

    try:
        features = []
        for chunk in chunks:
            where = (
                f"EGID IN ({','.join(map(str, chunk))})"
                if len(chunk) > 1
                else f"EGID={chunk[0]}"
            )
            chunk_features = fetch_all(
                url,
                fields=",".join(_API_COLUMNS),
                where=where,
                chunk_size=chunk_size,
                progress_cb=progress_cb,
                status_cb=status_cb,
            )
            if chunk_features:
                features.extend(chunk_features)

        if not features:
            return None

        df = (
            pl
            .from_dicts([f["attributes"] for f in features], schema_infer_length=None)
            .rename(_RENAME)
            .select(RESULT_COLUMNS)
            .with_columns(
                # Int64
                pl.col(
                    "egid",
                    "annee",
                    "indice",
                    "sre",
                    "npa",
                    "indice_moy2",
                    "indice_moy3",
                    "id_concessionnaire",
                    "nbre_preneur",
                ).cast(pl.Int64),
                # Floats
                pl.col(
                    "quantite_agent_energetique_1",
                    "quantite_agent_energetique_2",
                    "quantite_agent_energetique_3",
                ).cast(pl.Float64),
                # Timestamps
                pl.col("date_debut_periode", "date_fin_periode", "date_saisie").cast(
                    pl.Datetime("ms")
                ),
            )
            # Dedup par (egid, annee)
            .sort(["egid", "annee", "date_saisie"], descending=[False, False, True])
            .unique(subset=["egid", "annee"], keep="first")
            .sort(["egid", "annee"])
        )

        schema_errors = validate_schema(df)
        for err in schema_errors:
            logger.warning("IDC schema mismatch: %s", err)

        return df

    except Exception as e:
        logger.error("fetch_idc_data error: %s", e)
        return None
