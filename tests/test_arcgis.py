"""
Tests d'intégration pour sitg_api._arcgis.

Ces tests effectuent de vraies requêtes HTTP vers l'API SITG.
Ils nécessitent une connexion internet.
"""

import pytest

from sitg_api import fetch_all, get_count, get_layer_info

pytestmark = pytest.mark.integration

# Layer IDC — dataset public SITG, ~238k enregistrements
URL = (
    "https://vector.sitg.ge.ch/arcgis/rest/services/"
    "SCANE_INDICE_MOYENNES_3_ANS/FeatureServer/0/query"
)
BASE_URL = URL.removesuffix("/query")

# Layer SIT_AUTOR_DOSSIER — géométrie ponctuelle (point), pour valider le décodage
# PBF des points (type esriGeometryTypePoint = 0, omis par proto3).
URL_AUTOR = "https://vector.sitg.ge.ch/arcgis/rest/services/SIT_AUTOR_DOSSIER/FeatureServer/0/query"
WHERE_AUTOR_SMALL = "OBJECTID<=2500"

# Filtre sur une petite commune pour limiter le volume dans les tests de fetch
WHERE_SMALL = "COMMUNE='Avully'"
WHERE_EMPTY = "COMMUNE='COMMUNE_INEXISTANTE_XYZ'"


# ---------------------------------------------------------------------------
# get_layer_info
# ---------------------------------------------------------------------------


class TestGetLayerInfo:
    def test_returns_dict(self):
        info = get_layer_info(URL)
        assert isinstance(info, dict)

    def test_has_record_limit_fields(self):
        info = get_layer_info(URL)
        assert "maxRecordCount" in info
        assert "standardMaxRecordCount" in info
        assert "tileMaxRecordCount" in info
        assert "maxRecordCountFactor" in info

    def test_accepts_base_url_without_query_suffix(self):
        """get_layer_info doit fonctionner avec ou sans /query."""
        info_with = get_layer_info(URL)
        info_without = get_layer_info(BASE_URL)
        assert info_with["maxRecordCount"] == info_without["maxRecordCount"]

    def test_standard_max_gte_default_max(self):
        info = get_layer_info(URL)
        assert info["standardMaxRecordCount"] >= info["maxRecordCount"]

    def test_max_record_count_factor_is_positive(self):
        info = get_layer_info(URL)
        assert info["maxRecordCountFactor"] > 0

    def test_layer_name_present(self):
        info = get_layer_info(URL)
        assert "name" in info

    def test_unreachable_host_raises(self):
        import httpx

        with pytest.raises(httpx.ConnectError):
            get_layer_info(
                "https://does-not-exist.sitg.invalid/arcgis/rest/services/X/FeatureServer/0"
            )


# ---------------------------------------------------------------------------
# get_count
# ---------------------------------------------------------------------------


class TestGetCount:
    def test_total_count_is_large(self):
        count = get_count(URL)
        assert count > 10_000

    def test_filtered_count_less_than_total(self):
        total = get_count(URL)
        small = get_count(URL, where=WHERE_SMALL)
        assert 0 < small < total

    def test_impossible_where_returns_zero(self):
        assert get_count(URL, where=WHERE_EMPTY) == 0

    def test_count_is_consistent_across_calls(self):
        """Deux appels successifs doivent retourner le même résultat."""
        assert get_count(URL, where=WHERE_SMALL) == get_count(URL, where=WHERE_SMALL)


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------


class TestFetchAll:
    def test_auto_chunk_size_fetches_correct_total(self):
        """chunk_size=None doit retourner exactement autant de features que get_count."""
        expected = get_count(URL, where=WHERE_SMALL)
        features = fetch_all(URL, where=WHERE_SMALL, chunk_size=None, progress=False)
        assert len(features) == expected

    def test_explicit_chunk_size_fetches_correct_total(self):
        expected = get_count(URL, where=WHERE_SMALL)
        # Use chunk_size small enough to force multiple pages but well below standardMaxRecordCount
        features = fetch_all(URL, where=WHERE_SMALL, chunk_size=50, progress=False)
        assert len(features) == expected

    def test_large_explicit_chunk_size_fetches_correct_total(self):
        """chunk_size plus grand que le total ne doit pas dupliquer de records."""
        expected = get_count(URL, where=WHERE_SMALL)
        features = fetch_all(URL, where=WHERE_SMALL, chunk_size=10_000, progress=False)
        assert len(features) == expected

    def test_features_have_attributes_key(self):
        features = fetch_all(URL, where=WHERE_SMALL, progress=False)
        assert all("attributes" in f for f in features)

    def test_no_geometry_by_default(self):
        features = fetch_all(URL, where=WHERE_SMALL, progress=False)
        assert all("geometry" not in f or f["geometry"] is None for f in features)

    def test_with_geometry_returns_geometry(self):
        features = fetch_all(URL, where=WHERE_SMALL, with_geometry=True, progress=False)
        assert all("geometry" in f and f["geometry"] is not None for f in features)

    def test_fields_filter_limits_attributes(self):
        features = fetch_all(URL, where=WHERE_SMALL, fields="EGID,ANNEE", progress=False)
        attrs = features[0]["attributes"]
        assert "EGID" in attrs
        assert "ANNEE" in attrs
        assert "ADRESSE" not in attrs

    def test_empty_result_returns_empty_list(self):
        features = fetch_all(URL, where=WHERE_EMPTY, progress=False)
        assert features == []

    def test_no_duplicate_records(self):
        """Vérifier l'absence de doublons par OBJECTID."""
        features = fetch_all(URL, where=WHERE_SMALL, fields="OBJECTID", progress=False)
        object_ids = [f["attributes"]["OBJECTID"] for f in features]
        assert len(object_ids) == len(set(object_ids))

    def test_auto_chunk_uses_standard_max_record_count(self):
        """fetch_all sans chunk_size doit utiliser standardMaxRecordCount du layer."""
        info = get_layer_info(URL)
        expected_chunk = int(info["standardMaxRecordCount"] * info["maxRecordCountFactor"])
        # On vérifie indirectement : si la détection fonctionne, le fetch réussit
        # et retourne le bon nombre de records (pas de troncature)
        expected_count = get_count(URL, where=WHERE_SMALL)
        features = fetch_all(URL, where=WHERE_SMALL, chunk_size=None, progress=False)
        assert len(features) == expected_count
        assert expected_chunk >= info["maxRecordCount"]

    def test_progress_callbacks_called(self):
        progress_values = []
        status_messages = []

        fetch_all(
            URL,
            where=WHERE_SMALL,
            progress=False,
            progress_cb=progress_values.append,
            status_cb=status_messages.append,
        )

        assert len(progress_values) > 0
        assert progress_values[-1] == pytest.approx(1.0)
        assert len(status_messages) > 0


# ---------------------------------------------------------------------------
# exceededTransferLimit
# ---------------------------------------------------------------------------


class TestFetchAllPbf:
    """Le format PBF doit produire exactement la même sortie que JSON."""

    def test_pbf_count_matches_json(self):
        json_features = fetch_all(URL, where=WHERE_SMALL, progress=False, response_format="json")
        pbf_features = fetch_all(URL, where=WHERE_SMALL, progress=False, response_format="pbf")
        # Le dataset SITG est vivant : des lignes peuvent apparaître/disparaître entre les
        # deux appels séquentiels. On tolère un écart de 2 % (min 10 records) pour
        # distinguer une dérive normale d'un bug de décodage PBF.
        tolerance = max(10, int(len(json_features) * 0.02))
        assert abs(len(pbf_features) - len(json_features)) <= tolerance

    def test_pbf_attributes_match_json(self):
        fields = "EGID,ANNEE,COMMUNE"
        j = fetch_all(URL, where=WHERE_SMALL, fields=fields, progress=False, response_format="json")
        p = fetch_all(URL, where=WHERE_SMALL, fields=fields, progress=False, response_format="pbf")

        def key(f):
            return (f["attributes"]["EGID"], f["attributes"]["ANNEE"])

        jd = {key(f): f["attributes"] for f in j}
        pd = {key(f): f["attributes"] for f in p}

        # Le dataset SITG est vivant : des lignes peuvent apparaître entre les
        # deux appels HTTP séquentiels (json puis pbf). On ne compare que les
        # clés communes aux deux snapshots pour isoler les vraies divergences
        # de décodage PBF des écarts dus à des écritures concurrentes côté serveur.
        common = jd.keys() & pd.keys()
        assert len(common) > 0
        for k in common:
            assert jd[k] == pd[k]

    def test_pbf_geometry_matches_json(self):
        fields = "EGID,ANNEE"
        j = fetch_all(
            URL,
            where=WHERE_SMALL,
            fields=fields,
            with_geometry=True,
            progress=False,
            response_format="json",
        )
        p = fetch_all(
            URL,
            where=WHERE_SMALL,
            fields=fields,
            with_geometry=True,
            progress=False,
            response_format="pbf",
        )

        def key(f):
            return (f["attributes"]["EGID"], f["attributes"]["ANNEE"])

        jd = {key(f): f["geometry"] for f in j}
        pd = {key(f): f["geometry"] for f in p}

        # Comme pour les attributs, le dataset peut évoluer entre les deux
        # appels séquentiels : on ne compare que les clés communes.
        common = jd.keys() & pd.keys()
        assert len(common) > 0

        max_diff = 0.0
        compared_rings = 0
        for k in common:
            gj = jd[k]
            gp = pd[k]
            if not gj or "rings" not in gj:
                continue
            assert len(gj["rings"]) == len(gp["rings"])
            for ring_j, ring_p in zip(gj["rings"], gp["rings"], strict=True):
                assert len(ring_j) == len(ring_p)
                for (xj, yj), (xp, yp) in zip(ring_j, ring_p, strict=True):
                    max_diff = max(max_diff, abs(xj - xp), abs(yj - yp))
                compared_rings += 1
        assert compared_rings > 0
        # Quantification au mm près : tout écart > 1cm signalerait un bug de décodage
        assert max_diff < 0.01

    def test_pbf_no_geometry_when_not_requested(self):
        features = fetch_all(URL, where=WHERE_SMALL, progress=False, response_format="pbf")
        assert all(f["geometry"] is None for f in features)

    def test_invalid_response_format_raises(self):
        with pytest.raises(ValueError):
            fetch_all(URL, where=WHERE_SMALL, progress=False, response_format="xml")

    def test_pbf_point_geometry_decoded_as_xy(self):
        """Les points (type 0, omis en proto3) doivent décoder en {x, y}, pas en rings."""
        j = fetch_all(
            URL_AUTOR,
            where=WHERE_AUTOR_SMALL,
            fields="ID_DOSSIER",
            with_geometry=True,
            progress=False,
            response_format="json",
        )
        p = fetch_all(
            URL_AUTOR,
            where=WHERE_AUTOR_SMALL,
            fields="ID_DOSSIER",
            with_geometry=True,
            progress=False,
            response_format="pbf",
        )

        def key(f):
            return f["attributes"]["ID_DOSSIER"]

        jd = {key(f): f["geometry"] for f in j}
        pd = {key(f): f["geometry"] for f in p}
        assert set(jd) == set(pd)

        max_diff = 0.0
        compared = 0
        for k, gj in jd.items():
            gp = pd[k]
            if not gj:
                continue
            assert "x" in gp and "y" in gp, f"point décodé sans x/y: {gp}"
            assert "rings" not in gp
            max_diff = max(max_diff, abs(gj["x"] - gp["x"]), abs(gj["y"] - gp["y"]))
            compared += 1
        assert compared > 0
        # quantification au cm près — un signe/accumulation erroné donnerait des km
        assert max_diff < 0.01


class TestExceededTransferLimit:
    def test_false_positive_does_not_lose_data(self):
        """
        ArcGIS peut retourner exceededTransferLimit=true sur la dernière page
        même quand tous les records ont été retournés (artefact de l'index spatial).
        fetch_all doit logguer un warning mais NE PAS perdre de données.
        """
        expected = get_count(URL, where=WHERE_SMALL)
        # chunk_size petit → plusieurs pages, augmente la probabilité du faux positif
        features = fetch_all(URL, where=WHERE_SMALL, chunk_size=50, progress=False)
        assert len(features) == expected

    def test_large_chunk_on_full_dataset_gets_all_records(self):
        """
        fetch_all avec chunk_size=None auto-détecte le max serveur.
        Le résultat doit contenir exactement get_count() enregistrements.
        """
        expected = get_count(URL, where=WHERE_SMALL)
        features = fetch_all(URL, where=WHERE_SMALL, chunk_size=None, progress=False)
        assert len(features) == expected
