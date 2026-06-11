"""Configuration loguru pour sitg_api, compatible avec les barres tqdm.

Par convention « librairie », sitg_api désactive son propre logger au moment de
l'import (cf. ``__init__``). L'application appelle :func:`configure_logging` pour
activer la sortie — horodatage en **heure locale** et sink compatible ``tqdm``.

Pourquoi un sink tqdm : ``fetch_all`` affiche une barre de progression ``tqdm``.
Un ``print``/handler classique écrirait par-dessus la barre et la casserait.
``tqdm.write`` insère proprement la ligne de log au-dessus de la barre active.
"""

import sys

from loguru import logger
from tqdm.auto import tqdm

# {time} est en heure locale par défaut dans loguru → date + heure locales.
DEFAULT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _tqdm_sink(message: str) -> None:
    """Écrit le log via tqdm.write pour préserver les barres de progression."""
    tqdm.write(message, end="", file=sys.stderr)


def configure_logging(
    level: str | int = "INFO",
    *,
    fmt: str = DEFAULT_FORMAT,
    colorize: bool | None = None,
) -> None:
    """Active et configure le logging loguru de sitg_api.

    Paramètres
    ----------
    level : niveau minimum affiché (``"DEBUG"``, ``"INFO"``, ...).
    fmt : format loguru ; par défaut horodatage en heure locale.
    colorize : forcer/désactiver la couleur. ``None`` (défaut) = auto :
        couleurs si la sortie est un terminal, texte brut sinon (logs cloud,
        fichiers) pour éviter des codes ANSI parasites.

    Remplace les handlers loguru existants par un sink unique écrivant via
    ``tqdm.write``. À appeler une fois au démarrage de l'application.
    """
    if colorize is None:
        colorize = sys.stderr.isatty()

    logger.remove()
    logger.add(
        _tqdm_sink,
        level=level,
        format=fmt,
        colorize=colorize,
        backtrace=False,
        diagnose=False,
    )
    logger.enable("sitg_api")
