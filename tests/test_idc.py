"""
Tests d'intégration pour sitg_api.idc (IDCFetcher).

Ces tests effectuent de vraies requêtes HTTP vers l'API SITG.
Ils nécessitent une connexion internet.
"""

import polars as pl
import pytest

from sitg_api.idc import _ANNEE_MAX, IDCFetcher

# EGIDs connus dans le dataset IDC SITG (immeuble Avenue Giuseppe-Motta 30, Genève)
EGID_KNOWN = [1015052, 1015054]
EGID_SINGLE = 1015052
EGID_UNKNOWN = [999999999]


@pytest.fixture(scope="module")
def fetcher():
    return IDCFetcher()


@pytest.fixture(scope="module")
def idc_result(fetcher):
    """Résultat partagé pour éviter des appels réseau redondants."""
    df, failures = fetcher.fetch(EGID_KNOWN)
    return df, failures


# ---------------------------------------------------------------------------
# IDCFetcher.fetch — structure du résultat
# ---------------------------------------------------------------------------


class TestIDCFetcherStructure:
    def test_returns_tuple(self, fetcher):
        result = fetcher.fetch(EGID_SINGLE)
        assert isinstance(result, tuple) and len(result) == 2

    def test_df_is_polars_dataframe(self, idc_result):
        df, _ = idc_result
        assert isinstance(df, pl.DataFrame)

    def test_df_not_empty(self, idc_result):
        df, _ = idc_result
        assert len(df) > 0

    def test_df_has_expected_columns(self, idc_result):
        df, _ = idc_result
        expected = {c.lower() for c in IDCFetcher._API_COLUMNS}
        assert expected.issubset(set(df.columns))

    def test_egid_column_contains_requested_egids(self, idc_result):
        df, _ = idc_result
        returned_egids = set(df["egid"].to_list())
        assert set(EGID_KNOWN).issubset(returned_egids)

    def test_annee_column_is_integer(self, idc_result):
        df, _ = idc_result
        assert df["annee"].dtype in (pl.Int32, pl.Int64)

    def test_indice_column_is_numeric(self, idc_result):
        df, _ = idc_result
        assert df["indice"].dtype in (pl.Int32, pl.Int64, pl.Float32, pl.Float64)

    def test_date_columns_are_datetime(self, idc_result):
        df, _ = idc_result
        for col in ("date_debut_periode", "date_fin_periode", "date_saisie"):
            assert df[col].dtype == pl.Datetime("ms"), f"{col} should be Datetime"


# ---------------------------------------------------------------------------
# IDCFetcher.fetch — validité des données
# ---------------------------------------------------------------------------


class TestIDCFetcherDataQuality:
    def test_no_duplicate_egid_annee(self, idc_result):
        """La déduplication doit garantir une seule ligne par (egid, annee)."""
        df, _ = idc_result
        n_unique = df.select(["egid", "annee"]).n_unique()
        assert n_unique == len(df)

    def test_annee_in_valid_range(self, idc_result):
        df, _ = idc_result
        assert df["annee"].min() >= 2000
        assert df["annee"].max() <= _ANNEE_MAX

    def test_egid_positive(self, idc_result):
        df, _ = idc_result
        assert (df["egid"] > 0).all()

    def test_indice_positive_where_not_null(self, idc_result):
        df, _ = idc_result
        non_null = df["indice"].drop_nulls()
        if len(non_null) > 0:
            assert (non_null > 0).all()

    def test_date_debut_before_date_fin(self, idc_result):
        df, _ = idc_result
        valid_rows = df.filter(
            pl.col("date_debut_periode").is_not_null() & pl.col("date_fin_periode").is_not_null()
        )
        assert (valid_rows["date_debut_periode"] < valid_rows["date_fin_periode"]).all()


# ---------------------------------------------------------------------------
# IDCFetcher.fetch — cas limites
# ---------------------------------------------------------------------------


class TestIDCFetcherEdgeCases:
    def test_single_egid_as_int(self, fetcher):
        df, _ = fetcher.fetch(EGID_SINGLE)
        assert len(df) > 0
        assert EGID_SINGLE in df["egid"].to_list()

    def test_single_egid_as_list(self, fetcher):
        df, _ = fetcher.fetch([EGID_SINGLE])
        assert len(df) > 0

    def test_unknown_egid_raises(self, fetcher):
        with pytest.raises(RuntimeError, match="Aucune donnée IDC"):
            fetcher.fetch(EGID_UNKNOWN)

    def test_invalid_type_raises(self, fetcher):
        with pytest.raises(ValueError, match="Aucun EGID valide"):
            fetcher.fetch(["not_an_int"])

    def test_duplicate_egids_deduplicated_in_input(self, fetcher):
        """Passer le même EGID deux fois ne doit pas doubler les résultats."""
        df_once, _ = fetcher.fetch([EGID_SINGLE])
        df_twice, _ = fetcher.fetch([EGID_SINGLE, EGID_SINGLE])
        assert len(df_once) == len(df_twice)

    def test_failures_is_failure_info(self, idc_result):
        import dataframely as dy

        _, failures = idc_result
        assert isinstance(failures, dy.FailureInfo)
