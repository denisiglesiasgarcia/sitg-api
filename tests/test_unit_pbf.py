"""
Tests unitaires pour sitg_api._pbf — décodage bas niveau, sans accès réseau.

Construit des messages protobuf synthétiques minimaux (via de petits encodeurs
locaux) pour exercer decode_feature_collection() sans dépendre du serveur SITG.
"""

import struct

from sitg_api._arcgis import _looks_like_json
from sitg_api._pbf import _parse, _read_varint, _to_signed64, _zigzag, decode_feature_collection

# ---------------------------------------------------------------------------
# Petits encodeurs protobuf, pour construire des messages de test uniquement
# (le package lui-même ne fait que décoder, cf. _pbf/__init__.py).
# ---------------------------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _tag(field: int, wire_type: int) -> bytes:
    return _varint((field << 3) | wire_type)


def _len_delim(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _double_field(field: int, value: float) -> bytes:
    return _tag(field, 1) + struct.pack("<d", value)


def _string_field(field: int, s: str) -> bytes:
    return _len_delim(field, s.encode("utf-8"))


# ---------------------------------------------------------------------------
# _read_varint / _zigzag / _to_signed64 / _parse
# ---------------------------------------------------------------------------


class TestReadVarint:
    def test_single_byte(self):
        assert _read_varint(b"\x01", 0) == (1, 1)

    def test_multi_byte(self):
        # 300 = 0b1_0010_1100 -> varint bytes: 0xAC 0x02
        assert _read_varint(bytes([0xAC, 0x02]), 0) == (300, 2)

    def test_starts_at_offset(self):
        buf = b"\xff\x01\x05"  # garbage byte, then varint 1, then varint 5
        assert _read_varint(buf, 1) == (1, 2)


class TestZigzag:
    def test_zero(self):
        assert _zigzag(0) == 0

    def test_positive_roundtrip(self):
        for n in (1, 2, 10, 1000):
            encoded = n * 2
            assert _zigzag(encoded) == n

    def test_negative_roundtrip(self):
        for n in (-1, -2, -10, -1000):
            encoded = -n * 2 - 1
            assert _zigzag(encoded) == n


class TestToSigned64:
    def test_small_positive_unchanged(self):
        assert _to_signed64(42) == 42

    def test_top_bit_set_becomes_negative(self):
        assert _to_signed64((1 << 64) - 1) == -1


class TestParse:
    def test_varint_field(self):
        buf = _varint_field(7, 42)
        fields = _parse(buf)
        assert fields[7] == [(0, 42)]

    def test_length_delimited_field(self):
        buf = _len_delim(3, b"hello")
        fields = _parse(buf)
        assert fields[3] == [(2, b"hello")]

    def test_last_occurrence_wins_on_repeat(self):
        buf = _varint_field(1, 1) + _varint_field(1, 2)
        fields = _parse(buf)
        assert [v for _wt, v in fields[1]] == [1, 2]


# ---------------------------------------------------------------------------
# decode_feature_collection — messages synthétiques
# ---------------------------------------------------------------------------


def _wrap_feature_result(feature_result_msg: bytes) -> bytes:
    """FeatureCollectionPBuffer -> queryResult(2) -> featureResult(1)."""
    fr_field = _len_delim(1, feature_result_msg)
    qr_field = _len_delim(2, fr_field)
    return qr_field


class TestDecodeFeatureCollectionPoint:
    """Point (type 0, souvent omis en proto3) avec un attribut string indexé."""

    def _build(self) -> bytes:
        # Value{string_value="PORTE", index=0}
        value_msg = _string_field(1, "PORTE") + _varint_field(11, 0)
        attributes_field = _len_delim(1, value_msg)  # Feature.attributes (field 1)

        # coords: un seul point, delta (10, 20) depuis l'origine, zigzag-encodé
        coords_zz = _varint(20) + _varint(40)  # zigzag(10)=20, zigzag(20)=40
        geometry_msg = _len_delim(3, coords_zz)  # Geometry.coords (field 3)
        geometry_field = _len_delim(2, geometry_msg)  # Feature.geometry (field 2)

        feature_msg = attributes_field + geometry_field
        feature_field = _len_delim(15, feature_msg)  # FeatureResult.features (field 15)

        field_def = _len_delim(13, _string_field(1, "ID_DOSSIER"))  # FeatureResult.fields
        geom_type_field = _varint_field(7, 0)  # FeatureResult.geometryType = point

        # origin = lower_left (1) pour éviter l'inversion de signe en Y, scale=1, translate=0
        scale_msg = _double_field(1, 1.0) + _double_field(2, 1.0)
        translate_msg = _double_field(1, 0.0) + _double_field(2, 0.0)
        transform_msg = (
            _varint_field(1, 1) + _len_delim(2, scale_msg) + _len_delim(3, translate_msg)
        )
        transform_field = _len_delim(12, transform_msg)

        sr_field = _len_delim(8, _varint_field(1, 2056))  # spatialReference.wkid

        feature_result_msg = (
            geom_type_field + sr_field + transform_field + field_def + feature_field
        )
        return _wrap_feature_result(feature_result_msg)

    def test_decodes_single_point_feature(self):
        features, exceeded = decode_feature_collection(self._build())

        assert exceeded is False
        assert len(features) == 1
        assert features[0]["attributes"] == {"ID_DOSSIER": "PORTE"}
        geom = features[0]["geometry"]
        assert geom["x"] == 10.0
        assert geom["y"] == 20.0
        assert geom["spatialReference"] == {"wkid": 2056}


class TestDecodeFeatureCollectionPolygon:
    """Polygon multi-anneaux (lengths) avec origine upper_left (inversion Y)."""

    def _build(self) -> bytes:
        # 4 sommets, 2 anneaux de 2 sommets. Deltas cumulés bruts : (1,1)(1,1)(3,3)(1,1)
        # -> zigzag(n>=0) = n*2
        raw_deltas = [1, 1, 1, 1, 3, 3, 1, 1]
        coords_zz = b"".join(_varint(d * 2) for d in raw_deltas)
        lengths = _varint(2) + _varint(2)

        geometry_msg = (
            _varint_field(1, 3)  # GeometryType.polygon
            + _len_delim(2, lengths)
            + _len_delim(3, coords_zz)
        )
        geometry_field = _len_delim(2, geometry_msg)
        feature_field = _len_delim(15, geometry_field)  # feature with no attributes
        # No transform field -> decoder defaults to scale=1, translate=0, origin=upper_left
        return _wrap_feature_result(feature_field)

    def test_decodes_rings_with_y_sign_flip(self):
        features, exceeded = decode_feature_collection(self._build())

        assert exceeded is False
        assert len(features) == 1
        assert features[0]["attributes"] == {}
        geom = features[0]["geometry"]
        assert "spatialReference" not in geom
        assert geom["rings"] == [
            [[1.0, -1.0], [2.0, -2.0]],
            [[5.0, -5.0], [6.0, -6.0]],
        ]


class TestDecodeFeatureCollectionEdgeCases:
    def test_exceeded_transfer_limit_flag(self):
        feature_result_msg = _varint_field(9, 1)  # exceededTransferLimit=true, no features
        data = _wrap_feature_result(feature_result_msg)
        features, exceeded = decode_feature_collection(data)
        assert features == []
        assert exceeded is True

    def test_no_query_result_returns_empty(self):
        assert decode_feature_collection(b"") == ([], False)

    def test_count_only_result_returns_empty(self):
        # queryResult present but without a featureResult (e.g. countResult) -> not handled here
        qr_field = _len_delim(2, _varint_field(3, 42))
        assert decode_feature_collection(qr_field) == ([], False)


class TestLooksLikeJson:
    def test_detects_json_object(self):
        assert _looks_like_json(b'{"error": {"code": 400}}') is True

    def test_detects_json_array(self):
        assert _looks_like_json(b"[1, 2, 3]") is True

    def test_detects_leading_whitespace(self):
        assert _looks_like_json(b'  \n {"a": 1}') is True

    def test_pbf_bytes_not_json(self):
        # Un FeatureCollectionPBuffer commence par le tag du champ 1 (0x0A), pas '{'
        assert _looks_like_json(b"\x0a\x03foo") is False

    def test_empty_not_json(self):
        assert _looks_like_json(b"") is False
