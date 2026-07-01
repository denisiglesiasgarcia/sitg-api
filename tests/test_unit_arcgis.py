"""
Tests unitaires pour sitg_api._arcgis — httpx.MockTransport / monkeypatch,
aucun accès réseau. Complète tests/test_arcgis.py (intégration, réseau réel).
"""

import httpx
import pytest

from sitg_api._arcgis import _fetch_page, _layer_desc, fetch_all, stage_progress

# ---------------------------------------------------------------------------
# _layer_desc / stage_progress — fonctions pures
# ---------------------------------------------------------------------------


class TestLayerDesc:
    def test_extracts_service_name(self):
        url = (
            "https://vector.sitg.ge.ch/arcgis/rest/services/"
            "SCANE_INDICE_MOYENNES_3_ANS/FeatureServer/0/query"
        )
        assert _layer_desc(url) == "SCANE_INDICE_MOYENNES_3_ANS"

    def test_falls_back_when_no_match(self):
        assert _layer_desc("https://example.com/not-arcgis") == "ArcGIS"


class TestStageProgress:
    def test_none_callback_returns_none(self):
        assert stage_progress(None, 0.0, 1.0) is None

    def test_rescales_into_subrange(self):
        seen = []
        cb = stage_progress(seen.append, 0.5, 1.0)
        cb(0.0)
        cb(1.0)
        assert seen == [0.5, 1.0]


# ---------------------------------------------------------------------------
# _fetch_page — retry/backoff et exceededTransferLimit, via MockTransport
# ---------------------------------------------------------------------------


class TestFetchPage:
    def test_succeeds_first_try(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"features": [{"attributes": {"A": 1}}]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        features = _fetch_page(
            client, "https://x/query", 0, 100, "*", "1=1", False, 10, max_retries=3
        )
        assert features == [{"attributes": {"A": 1}}]
        assert len(calls) == 1

    def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("sitg_api._arcgis.time.sleep", lambda _: None)
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json={"features": [{"attributes": {"A": 1}}]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        features = _fetch_page(
            client, "https://x/query", 0, 100, "*", "1=1", False, 10, max_retries=5
        )
        assert features == [{"attributes": {"A": 1}}]
        assert attempts["n"] == 3

    def test_exhausts_retries_and_raises(self, monkeypatch):
        monkeypatch.setattr("sitg_api._arcgis.time.sleep", lambda _: None)

        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.ConnectError):
            _fetch_page(client, "https://x/query", 0, 100, "*", "1=1", False, 10, max_retries=2)

    def test_zero_features_with_exceeded_limit_raises(self):
        def handler(request):
            return httpx.Response(200, json={"features": [], "exceededTransferLimit": True})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(RuntimeError, match="exceededTransferLimit"):
            _fetch_page(client, "https://x/query", 0, 100, "*", "1=1", False, 10, max_retries=1)

    def test_full_page_exceeded_limit_does_not_raise(self):
        """exceededTransferLimit=true avec des features est normal (page pleine)."""

        def handler(request):
            return httpx.Response(
                200,
                json={"features": [{"attributes": {"A": 1}}], "exceededTransferLimit": True},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        features = _fetch_page(
            client, "https://x/query", 0, 100, "*", "1=1", False, 10, max_retries=1
        )
        assert features == [{"attributes": {"A": 1}}]


# ---------------------------------------------------------------------------
# fetch_all — orchestration bout-en-bout, httpx.get/httpx.Client monkeypatchés
# ---------------------------------------------------------------------------


class TestFetchAllOrchestration:
    def _install_fake_arcgis(self, monkeypatch, *, total, standard_max=2):
        def fake_get(url, params=None, timeout=None):
            request = httpx.Request("GET", url, params=params)
            if params and params.get("returnCountOnly"):
                return httpx.Response(200, json={"count": total}, request=request)
            return httpx.Response(
                200,
                json={
                    "standardMaxRecordCount": standard_max,
                    "maxRecordCountFactor": 1.0,
                    "maxRecordCount": standard_max,
                },
                request=request,
            )

        monkeypatch.setattr("sitg_api._arcgis.httpx.get", fake_get)

        def handler(request):
            offset = int(dict(request.url.params).get("resultOffset", 0))
            n = min(standard_max, max(0, total - offset))
            feats = [{"attributes": {"OBJECTID": offset + i}} for i in range(n)]
            return httpx.Response(200, json={"features": feats, "exceededTransferLimit": False})

        real_client_cls = httpx.Client

        def fake_client(*args, **kwargs):
            kwargs.pop("limits", None)
            return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("sitg_api._arcgis.httpx.Client", fake_client)

    def test_auto_chunk_size_fetches_all_pages(self, monkeypatch):
        self._install_fake_arcgis(monkeypatch, total=5, standard_max=2)
        features = fetch_all("https://x/query", progress=False)
        object_ids = sorted(f["attributes"]["OBJECTID"] for f in features)
        assert object_ids == [0, 1, 2, 3, 4]

    def test_empty_result_short_circuits(self, monkeypatch):
        self._install_fake_arcgis(monkeypatch, total=0)
        features = fetch_all("https://x/query", progress=False)
        assert features == []

    def test_invalid_response_format_raises_before_any_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "sitg_api._arcgis.httpx.get",
            lambda *a, **k: called.append(1) or httpx.Response(200, json={}),
        )
        with pytest.raises(ValueError, match="response_format"):
            fetch_all("https://x/query", progress=False, response_format="xml")
        assert called == []
