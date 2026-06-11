from loguru import logger

from sitg_api._arcgis import fetch_all, get_count, get_layer_info, stage_progress
from sitg_api._logging import configure_logging

# Convention loguru pour les librairies : silencieux par défaut. L'application
# active la sortie en appelant configure_logging() (qui ré-active "sitg_api").
logger.disable("sitg_api")

__all__ = [
    "configure_logging",
    "fetch_all",
    "get_count",
    "get_layer_info",
    "stage_progress",
]
