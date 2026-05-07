"""
sitg — Client ArcGIS REST pour le SITG (Genève) et datasets compatibles.

Usage rapide
------------
# N'importe quel dataset ArcGIS SITG
from sitg import fetch_all

features = fetch_all("https://.../FeatureServer/0/query", fields="ID,NOM", where="COMMUNE='Genève'")
df = pl.from_dicts([f["attributes"] for f in features])

# IDC — retourne directement un pl.DataFrame
from sitg.idc import fetch_idc_data
df = fetch_idc_data(egid=123456)
df = fetch_idc_data(egid=[123456, 789012])
"""

from sitg_api._arcgis import fetch_all, get_count, stage_progress

__all__ = [
    "fetch_all",
    "get_count",
    "stage_progress",
]
