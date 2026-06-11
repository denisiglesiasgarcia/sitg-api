"""
Décodeur minimal du format Protocol Buffer (PBF) d'ArcGIS FeatureServer.

ArcGIS sert les résultats de requête en ``f=pbf`` selon le schéma
``esriPBuffer.FeatureCollectionPBuffer`` (cf. ``FeatureCollection.proto`` dans
ce dossier, copié depuis https://github.com/Esri/arcgis-pbf). Le PBF est
nettement plus compact et rapide à transférer que ``f=json`` — Esri recommande
explicitement PBF plutôt que JSON/geoJSON pour les performances.

Plutôt que d'ajouter une dépendance ``protobuf`` + code généré, ce module
décode directement le sous-ensemble du wire-format dont nous avons besoin
(lecture seule). La sortie est volontairement **identique** à celle du chemin
JSON de ``fetch_all`` : une liste de dicts ``{"attributes": {...}, "geometry":
{...} | None}`` où ``geometry`` suit la convention ArcGIS JSON
(``rings`` / ``paths`` / ``x,y`` / ``points``).

Point d'entrée : :func:`decode_feature_collection`.
"""

import struct

# Numéros de champs (cf. FeatureCollection.proto)
_FC_QUERY_RESULT = 2
_QR_FEATURE_RESULT = 1
_FR_GEOMETRY_TYPE = 7
_FR_SPATIAL_REF = 8
_FR_EXCEEDED = 9
_FR_TRANSFORM = 12
_FR_FIELDS = 13
_FR_FEATURES = 15

_FEAT_ATTRIBUTES = 1
_FEAT_GEOMETRY = 2  # oneof compressed_geometry → Geometry

_GEOM_TYPE = 1
_GEOM_LENGTHS = 2
_GEOM_COORDS = 3

# GeometryType enum
_GT_POINT = 0
_GT_MULTIPOINT = 1
_GT_POLYLINE = 2
_GT_POLYGON = 3
_GT_ENVELOPE = 5

# QuantizeOriginPosition enum
_ORIGIN_UPPER_LEFT = 0
_ORIGIN_LOWER_LEFT = 1


# ---------------------------------------------------------------------------
# Lecture bas niveau du wire-format protobuf
# ---------------------------------------------------------------------------


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Lit un varint non signé à partir de ``pos``. Retourne (valeur, nouvelle_pos)."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _zigzag(n: int) -> int:
    """Décode un entier zigzag (sint32/sint64) → entier signé."""
    return (n >> 1) ^ -(n & 1)


def _to_signed64(n: int) -> int:
    """Interprète un varint int64 (complément à deux) en entier signé Python."""
    return n - (1 << 64) if n >= (1 << 63) else n


def _parse(buf: bytes) -> dict[int, list]:
    """Parse un message protobuf en dict ``field_number -> [(wire_type, raw), ...]``.

    ``raw`` est :
      - un ``int`` pour wire-type 0 (varint),
      - ``bytes`` (8 octets) pour wire-type 1 (64-bit),
      - ``bytes`` (sous-buffer) pour wire-type 2 (length-delimited),
      - ``bytes`` (4 octets) pour wire-type 5 (32-bit).
    """
    fields: dict[int, list] = {}
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        fnum = key >> 3
        wt = key & 0x07
        if wt == 0:
            val, pos = _read_varint(buf, pos)
        elif wt == 1:
            val = buf[pos : pos + 8]
            pos += 8
        elif wt == 2:
            length, pos = _read_varint(buf, pos)
            val = buf[pos : pos + length]
            pos += length
        elif wt == 5:
            val = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"PBF: wire-type non supporté {wt} (field {fnum})")
        fields.setdefault(fnum, []).append((wt, val))
    return fields


def _last(fields: dict[int, list], fnum: int):
    """Retourne la valeur brute de la dernière occurrence d'un champ, ou None."""
    entries = fields.get(fnum)
    return entries[-1][1] if entries else None


def _unpack_varints(fields: dict[int, list], fnum: int) -> list[int]:
    """Déballe un champ ``repeated [packed=true]`` de varints (toutes occurrences)."""
    out: list[int] = []
    for _wt, raw in fields.get(fnum, []):
        pos = 0
        n = len(raw)
        while pos < n:
            v, pos = _read_varint(raw, pos)
            out.append(v)
    return out


def _double(raw: bytes | None) -> float:
    return struct.unpack("<d", raw)[0] if raw else 0.0


# ---------------------------------------------------------------------------
# Décodage des messages métier
# ---------------------------------------------------------------------------


def _decode_value(buf: bytes) -> tuple[object, int | None]:
    """Décode un message ``Value`` → (valeur Python, index de champ optionnel)."""
    f = _parse(buf)
    index = _last(f, 11)  # optional uint32 index
    if 1 in f:  # string_value
        return _last(f, 1).decode("utf-8"), index
    if 2 in f:  # float_value
        return struct.unpack("<f", _last(f, 2))[0], index
    if 3 in f:  # double_value
        return _double(_last(f, 3)), index
    if 4 in f:  # sint32
        return _zigzag(_last(f, 4)), index
    if 5 in f:  # uint32
        return _last(f, 5), index
    if 6 in f:  # int64
        return _to_signed64(_last(f, 6)), index
    if 7 in f:  # uint64
        return _last(f, 7), index
    if 8 in f:  # sint64
        return _zigzag(_last(f, 8)), index
    if 9 in f:  # bool
        return bool(_last(f, 9)), index
    if 10 in f:  # null_value
        return None, index
    return None, index


def _decode_transform(buf: bytes) -> tuple[float, float, float, float, int]:
    """Décode ``Transform`` → (xScale, yScale, xTranslate, yTranslate, originPosition)."""
    f = _parse(buf)
    origin = _last(f, 1) or _ORIGIN_UPPER_LEFT
    x_scale = y_scale = 1.0
    x_tr = y_tr = 0.0
    if 2 in f:  # Scale
        scale = _parse(_last(f, 2))
        x_scale = _double(_last(scale, 1))
        y_scale = _double(_last(scale, 2))
    if 3 in f:  # Translate
        tr = _parse(_last(f, 3))
        x_tr = _double(_last(tr, 1))
        y_tr = _double(_last(tr, 2))
    return x_scale, y_scale, x_tr, y_tr, origin


def _decode_geometry(
    buf: bytes,
    transform: tuple[float, float, float, float, int],
    wkid: int | None,
    default_geom_type: int | None,
) -> dict | None:
    """Décode un message ``Geometry`` quantifié en géométrie ArcGIS JSON."""
    f = _parse(buf)
    geom_type = _last(f, _GEOM_TYPE)
    if geom_type is None:
        geom_type = default_geom_type
    lengths = _unpack_varints(f, _GEOM_LENGTHS)
    coords_zz = _unpack_varints(f, _GEOM_COORDS)
    if not coords_zz:
        return None

    x_scale, y_scale, x_tr, y_tr, origin = transform

    # coords : sint64 zigzag, deltas cumulés, entrelacés x, y, x, y, ...
    # Origine upperLeft (défaut ArcGIS) : y décroît vers le bas → signe -1.
    y_sign = 1 if origin == _ORIGIN_LOWER_LEFT else -1
    deltas = [_zigzag(v) for v in coords_zz]
    vertices: list[list[float]] = []
    cum_x = 0
    cum_y = 0
    for i in range(0, len(deltas) - 1, 2):
        cum_x += deltas[i]
        cum_y += deltas[i + 1]
        x = x_tr + cum_x * x_scale
        y = y_tr + y_sign * cum_y * y_scale
        vertices.append([x, y])

    sr = {"wkid": wkid} if wkid else None

    if geom_type == _GT_POINT:
        x, y = vertices[0]
        out = {"x": x, "y": y}
        if sr:
            out["spatialReference"] = sr
        return out

    # Découpe les sommets en parties selon `lengths` (nb de sommets par anneau/chemin)
    parts: list[list[list[float]]] = []
    if lengths:
        idx = 0
        for ln in lengths:
            parts.append(vertices[idx : idx + ln])
            idx += ln
    else:
        parts = [vertices]

    if geom_type == _GT_MULTIPOINT:
        out = {"points": [v for part in parts for v in part]}
    elif geom_type == _GT_POLYLINE:
        out = {"paths": parts}
    else:  # polygon (et fallback)
        out = {"rings": parts}
    if sr:
        out["spatialReference"] = sr
    return out


def decode_feature_collection(data: bytes) -> tuple[list[dict], bool]:
    """Décode une réponse ArcGIS ``f=pbf`` (FeatureCollectionPBuffer).

    Retourne ``(features, exceeded_transfer_limit)`` où ``features`` est une
    liste de ``{"attributes": {...}, "geometry": {...} | None}`` — même forme
    que le chemin ``f=json`` de :func:`sitg_api.fetch_all`.
    """
    root = _parse(data)
    qr_buf = _last(root, _FC_QUERY_RESULT)
    if qr_buf is None:
        return [], False
    qr = _parse(qr_buf)
    fr_buf = _last(qr, _QR_FEATURE_RESULT)
    if fr_buf is None:
        # countResult / idsResult / extentCountResult — pas géré ici
        return [], False
    fr = _parse(fr_buf)

    exceeded = bool(_last(fr, _FR_EXCEEDED) or 0)
    default_geom_type = _last(fr, _FR_GEOMETRY_TYPE)

    # Référence spatiale (wkid préféré, sinon latestWkid)
    wkid = None
    if _FR_SPATIAL_REF in fr:
        sr = _parse(_last(fr, _FR_SPATIAL_REF))
        wkid = _last(sr, 1) or _last(sr, 2)

    # Transform de quantification
    transform = (1.0, 1.0, 0.0, 0.0, _ORIGIN_UPPER_LEFT)
    if _FR_TRANSFORM in fr:
        transform = _decode_transform(_last(fr, _FR_TRANSFORM))

    # Noms de champs, dans l'ordre des attributs
    field_names: list[str] = []
    for _wt, raw in fr.get(_FR_FIELDS, []):
        field = _parse(raw)
        name_raw = _last(field, 1)
        field_names.append(name_raw.decode("utf-8") if name_raw else "")

    features: list[dict] = []
    for _wt, raw in fr.get(_FR_FEATURES, []):
        feat = _parse(raw)

        attributes: dict[str, object] = {}
        for pos, (_w, vraw) in enumerate(feat.get(_FEAT_ATTRIBUTES, [])):
            value, vindex = _decode_value(vraw)
            i = vindex if vindex is not None else pos
            name = field_names[i] if i < len(field_names) else str(i)
            attributes[name] = value

        geometry = None
        geom_raw = _last(feat, _FEAT_GEOMETRY)
        if geom_raw is not None:
            geometry = _decode_geometry(geom_raw, transform, wkid, default_geom_type)

        features.append({"attributes": attributes, "geometry": geometry})

    return features, exceeded


__all__ = ["decode_feature_collection"]
