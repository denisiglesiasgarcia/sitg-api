"""
Client générique pour les FeatureServers ArcGIS REST (SITG et compatibles).

Point d'entrée principal : fetch_all()
"""

import random
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from loguru import logger
from tqdm.auto import tqdm

from ._pbf import decode_feature_collection


def _looks_like_json(content: bytes) -> bool:
    """True if the response body is JSON rather than PBF.

    ArcGIS sometimes returns JSON even when ``f=pbf`` is requested — notably
    query errors served as HTTP 200 with a ``{"error": ...}`` body. A PBF
    FeatureCollectionPBuffer never starts with ``{``/``[``, so sniffing the
    first non-whitespace byte cleanly distinguishes the two.
    """
    stripped = content.lstrip()[:1]
    return stripped in (b"{", b"[")


def _layer_desc(url: str) -> str:
    """Extrait un label court depuis l'URL ArcGIS pour la barre de progression.

    Exemple :
        ``.../services/SCANE_INDICE_MOYENNES_3_ANS/FeatureServer/0/query``
        → ``"SCANE_INDICE_MOYENNES_3_ANS"``
    """
    match = re.search(r"/services/([^/]+)/", url)
    return match.group(1) if match else "ArcGIS"


def get_layer_info(url: str, timeout: int = 30) -> dict:
    """Retourne les métadonnées du layer ArcGIS (GET /FeatureServer/0).

    Paramètres
    ----------
    url : str
        Endpoint du layer — avec ou sans suffixe ``/query``.
    timeout : int
        Timeout HTTP en secondes.

    Retourne
    --------
    dict
        Métadonnées brutes, incluant ``maxRecordCount``,
        ``standardMaxRecordCount``, ``tileMaxRecordCount`` et
        ``maxRecordCountFactor``.
    """
    base_url = url.removesuffix("/query")
    resp = httpx.get(base_url, params={"f": "json"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_count(url: str, where: str = "1=1", timeout: int = 30) -> int:
    """Retourne le nombre total de features pour une requête ArcGIS.

    Paramètres
    ----------
    url : str
        Endpoint /query du FeatureServer.
    where : str
        Clause SQL ArcGIS utilisée pour filtrer les enregistrements.
    timeout : int
        Timeout HTTP en secondes.

    Retourne
    --------
    int
        Nombre total d'enregistrements correspondants.
    """
    resp = httpx.get(
        url,
        params={"where": where, "returnCountOnly": "true", "f": "json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("count", 0)


def _fetch_page(
    client: httpx.Client,
    url: str,
    offset: int,
    chunk_size: int,
    fields: str,
    where: str,
    with_geometry: bool,
    timeout: int,
    max_retries: int,
    response_format: str = "json",
) -> list[dict]:
    """Récupère une page de features ArcGIS avec stratégie de retry exponentiel.

    Paramètres
    ----------
    client : httpx.Client
        Client HTTP partagé (connection pool, thread-safe).
    url : str
        Endpoint ``/query`` du FeatureServer.
    offset : int
        Décalage de pagination (``resultOffset``).
    chunk_size : int
        Taille de page demandée (``resultRecordCount``).
    fields : str
        Champs à retourner via ``outFields`` (ex. ``"*"``).
    where : str
        Clause SQL ArcGIS appliquée à la requête.
    with_geometry : bool
        Si ``True``, inclut la géométrie (``returnGeometry=true``).
    timeout : int
        Timeout HTTP en secondes.
    max_retries : int
        Nombre maximal de tentatives en cas d'erreur réseau/HTTP.

    Retourne
    --------
    list[dict]
        Liste des features (format ArcGIS JSON, clé ``features``).

    Notes
    -----
    En cas d'échec, la temporisation suit ``2^(attempt+1)`` secondes (+/- 20% de
    jitter, pour éviter que les workers en échec ne retentent tous en même
    temps) avant nouvelle tentative. La dernière erreur est propagée.
    """
    for attempt in range(max_retries):
        try:
            r = client.get(
                url,
                params={
                    "where": where,
                    "outFields": fields,
                    "returnGeometry": "true" if with_geometry else "false",
                    "f": response_format,
                    "resultOffset": offset,
                    "resultRecordCount": chunk_size,
                    "resultType": "standard",
                },
                timeout=timeout,
            )
            r.raise_for_status()
            if response_format == "pbf" and not _looks_like_json(r.content):
                features, exceeded = decode_feature_collection(r.content)
            else:
                # JSON path — also reached when f=pbf was requested but ArcGIS
                # returned JSON anyway (it does this for query errors served as
                # HTTP 200, and for layers/params that don't honour pbf).
                data = r.json()
                features = data.get("features", [])
                exceeded = data.get("exceededTransferLimit")
            if exceeded and not features:
                # Zéro feature + exceededTransferLimit => la page demandée dépasse
                # la limite serveur : erreur réelle de configuration.
                raise RuntimeError(
                    f"exceededTransferLimit at offset={offset}: server returned zero features "
                    f"(chunk_size={chunk_size} exceeds server limit). "
                    "Pass a smaller chunk_size or leave it as None for auto-detection."
                )
            # NB : avec la pagination par resultOffset, ArcGIS positionne
            # exceededTransferLimit=true sur CHAQUE page pleine (il reste des
            # lignes au-delà). Ce n'est pas une anomalie — la pagination est
            # pilotée par get_count(), donc on logue en DEBUG seulement pour
            # éviter ~1 warning par page.
            if exceeded:
                logger.debug(
                    "exceededTransferLimit at offset={} with {} features (page pleine, normal)",
                    offset,
                    len(features),
                )
            return features
        except httpx.HTTPError as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** (attempt + 1) * random.uniform(0.8, 1.2)  # nosec B311 - retry jitter, not crypto
            logger.warning(
                "offset={} tentative {}/{} échouée ({}), nouvel essai dans {}s",
                offset,
                attempt + 1,
                max_retries,
                type(exc).__name__,
                wait,
            )
            time.sleep(wait)
    return []


def stage_progress(
    cb: Callable[[float], None] | None,
    start: float,
    end: float,
) -> Callable[[float], None] | None:
    """
    Compose un ``progress_cb`` pour une étape partielle ``[start, end]``.

    Le callback retourné convertit une progression locale ``frac`` (de 0 à 1)
    en progression globale entre ``start`` et ``end``.

    Permet d'enchaîner plusieurs appels ``fetch_all()`` avec une barre de
    progression unique.

    Exemple:
        fetch_all(..., progress_cb=stage_progress(cb, 0.0, 0.5))
        fetch_all(..., progress_cb=stage_progress(cb, 0.5, 1.0))
    """
    if cb is None:
        return None

    def _stage_cb(frac: float) -> None:
        cb(start + frac * (end - start))

    return _stage_cb


def fetch_all(
    url: str,
    *,
    fields: str = "*",
    where: str = "1=1",
    with_geometry: bool = False,
    chunk_size: int | None = None,
    max_workers: int = 4,
    timeout: int = 120,
    max_retries: int = 4,
    response_format: str = "json",
    progress: bool = True,
    progress_cb: Callable[[float], None] | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> list[dict]:
    """
    Récupère la totalité des features d'un layer ArcGIS FeatureServer (pagination parallèle).

    Paramètres
    ----------
    url          : endpoint /query du FeatureServer
    fields       : champs à retourner, ex. "ID,NOM" ou "*"
    where        : filtre SQL, ex. "COMMUNE='Genève'" (défaut: tout)
    with_geometry: inclure la géométrie brute dans chaque feature
    chunk_size   : features par requête. Si None (défaut), lu automatiquement depuis
                   les métadonnées du layer (standardMaxRecordCount x maxRecordCountFactor),
                   ce qui correspond au maximum autorisé par le serveur avec resultType=standard.
    max_workers  : parallélisme des requêtes HTTP
    timeout      : timeout HTTP en secondes
    max_retries  : tentatives max par page avant exception
    response_format: format de transport, "json" (défaut) ou "pbf". Le PBF
                   (Protocol Buffer) est plus compact et rapide à transférer et
                   décoder ; Esri le recommande plutôt que JSON/geoJSON pour les
                   performances. La sortie est identique quel que soit le format.
                   Le layer doit lister "PBF" dans supportedQueryFormats.
    progress     : afficher une barre tqdm.auto en records/s (défaut: True); fonctionne
                   en terminal et en Jupyter (widget HTML automatique via tqdm.auto)
    progress_cb  : callback(float 0→1) — pour usage programmatique (ex. Streamlit)
    status_cb    : callback(str) pour messages de progression

    Retourne
    --------
    Liste de dicts, chacun avec :
      - "attributes" : dict des valeurs de champs
      - "geometry"   : dict brut ArcGIS (seulement si with_geometry=True)

    Note : les pages sont récupérées en parallèle, donc l'ordre des features
    dans la liste retournée reflète l'ordre de complétion des requêtes, pas
    l'ordre de pagination ArcGIS (resultOffset). Trier explicitement si un
    ordre stable est nécessaire.
    """
    if response_format not in ("json", "pbf"):
        raise ValueError(f"response_format invalide: {response_format!r} (attendu 'json' ou 'pbf')")

    layer = _layer_desc(url)

    if chunk_size is None:
        info = get_layer_info(url, timeout=30)
        factor = info.get("maxRecordCountFactor", 1.0)
        chunk_size = int(info.get("standardMaxRecordCount", 2000) * factor)
        logger.debug(
            "{}: chunk_size auto-détecté {} (standardMaxRecordCount x maxRecordCountFactor)",
            layer,
            chunk_size,
        )

    total = get_count(url, where, timeout=30)

    if status_cb:
        status_cb(f"{total:,} enregistrements trouvés — téléchargement...")

    if total == 0:
        logger.warning("{}: aucun feature pour where={!r}", layer, where)
        return []

    started = time.monotonic()
    offsets = list(range(0, total, chunk_size))
    logger.info(
        "{}: téléchargement de {:,} features sur {} page(s) "
        "(chunk={}, workers={}, geometry={}, format={})",
        layer,
        total,
        len(offsets),
        chunk_size,
        max_workers,
        with_geometry,
        response_format,
    )
    all_features: list[dict] = []
    lock = threading.Lock()

    # One shared httpx.Client — thread-safe, connection pool sized to max_workers
    limits = httpx.Limits(
        max_connections=max_workers,
        max_keepalive_connections=max_workers,
    )
    completed = 0
    with (
        httpx.Client(limits=limits) as client,
        ThreadPoolExecutor(max_workers=max_workers) as executor,
    ):
        futures = {
            executor.submit(
                _fetch_page,
                client,
                url,
                off,
                chunk_size,
                fields,
                where,
                with_geometry,
                timeout,
                max_retries,
                response_format,
            ): off
            for off in offsets
        }
        bar = tqdm(
            total=total,
            unit="rec",
            unit_scale=True,
            desc=_layer_desc(url),
            colour="green",
            dynamic_ncols=True,
            disable=not progress,
        )
        for future in as_completed(futures):
            off = futures[future]
            try:
                features = future.result()
            except Exception as e:
                bar.close()
                raise RuntimeError(f"Échec fetch offset={off}: {e}") from e

            with lock:
                all_features.extend(features)
                completed += 1
                bar.update(len(features))
                bar.set_postfix(pages=f"{completed}/{len(offsets)}", refresh=False)
                if progress_cb:
                    progress_cb(completed / len(offsets))
                if status_cb:
                    status_cb(f"Téléchargé {len(all_features):,} / ~{total:,}")
        bar.close()

    elapsed = time.monotonic() - started
    rate = len(all_features) / elapsed if elapsed > 0 else 0.0
    logger.info(
        "{}: {:,} features téléchargées en {:.1f}s ({:,.0f} rec/s)",
        layer,
        len(all_features),
        elapsed,
        rate,
    )
    return all_features
