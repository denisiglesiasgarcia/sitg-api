"""
Client générique pour les FeatureServers ArcGIS REST (SITG et compatibles).

Point d'entrée principal : fetch_all()
"""

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)


def get_count(url: str, where: str = "1=1", timeout: int = 30) -> int:
    """Retourne le nombre total de features pour un where clause donné."""
    resp = requests.get(
        url,
        params={"where": where, "returnCountOnly": "true", "f": "json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("count", 0)


def _fetch_page(
    url: str,
    offset: int,
    chunk_size: int,
    fields: str,
    where: str,
    with_geometry: bool,
    timeout: int,
    max_retries: int,
) -> list[dict]:
    """Récupère une page de features avec retry exponentiel."""
    for attempt in range(max_retries):
        try:
            r = requests.get(
                url,
                params={
                    "where": where,
                    "outFields": fields,
                    "returnGeometry": "true" if with_geometry else "false",
                    "f": "json",
                    "resultOffset": offset,
                    "resultRecordCount": chunk_size,
                },
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json().get("features", [])
        except requests.exceptions.RequestException:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            logger.warning(
                "offset=%d attempt %d/%d, retry in %ds",
                offset,
                attempt + 1,
                max_retries,
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
    Compose un progress_cb pour une étape partielle [start, end].

    Permet d'enchaîner plusieurs appels fetch_all() avec une barre de progression unique.

    Exemple:
        fetch_all(..., progress_cb=stage_progress(cb, 0.0, 0.5))
        fetch_all(..., progress_cb=stage_progress(cb, 0.5, 1.0))
    """
    if cb is None:
        return None
    return lambda frac: cb(start + frac * (end - start))


def fetch_all(
    url: str,
    *,
    fields: str = "*",
    where: str = "1=1",
    with_geometry: bool = False,
    chunk_size: int = 1000,
    max_workers: int = 3,
    timeout: int = 120,
    max_retries: int = 4,
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
    chunk_size   : nombre de features par requête (max serveur: souvent 1000 ou 2000)
    max_workers  : parallélisme des requêtes HTTP
    timeout      : timeout HTTP en secondes
    max_retries  : tentatives max par page avant exception
    progress_cb  : callback(float 0→1) — compatible Streamlit, tqdm, etc.
    status_cb    : callback(str) pour messages de progression

    Retourne
    --------
    Liste de dicts, chacun avec :
      - "attributes" : dict des valeurs de champs
      - "geometry"   : dict brut ArcGIS (seulement si with_geometry=True)
    """
    total = get_count(url, where, timeout=30)

    if status_cb:
        status_cb(f"{total:,} enregistrements trouvés — téléchargement...")

    if total == 0:
        logger.warning("Aucun feature retourné pour url=%s where=%s", url, where)
        return []

    offsets = list(range(0, total, chunk_size))
    all_features: list[dict] = []
    completed = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _fetch_page,
                url,
                off,
                chunk_size,
                fields,
                where,
                with_geometry,
                timeout,
                max_retries,
            ): off
            for off in offsets
        }
        for future in as_completed(futures):
            off = futures[future]
            try:
                features = future.result()
            except Exception as e:
                raise RuntimeError(f"Échec fetch offset={off}: {e}") from e

            with lock:
                all_features.extend(features)
                completed += 1
                if progress_cb:
                    progress_cb(completed / len(offsets))
                if status_cb:
                    status_cb(f"Téléchargé {len(all_features):,} / ~{total:,}")

    return all_features
