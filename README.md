# sitg-api

Client Python pour les APIs ArcGIS REST du SITG (Genève).

## Installation

```bash
# Installer uv https://docs.astral.sh/uv/getting-started/installation/

# Mac/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# Depuis le dépôt git
uv add git+https://github.com/denisiglesiasgarcia/sitg-api

# Créer venv
uv sync
```

## Usage

```python
# https://sitg.ge.ch/search?category=data
# Ajouter "/0/query" au "FeatureServer"
URL = "https://vector.sitg.ge.ch/arcgis/rest/services/SCANE_INDICE_MOYENNES_3_ANS/FeatureServer/0/query"
```

### Nombre de features

```python
from sitg_api import get_count

print(f"Total: {get_count(URL)}")
print(f"Genève: {get_count(URL, where="COMMUNE='Genève'")}")
```

### Télécharger couche complète

#### Sans géométrie

```python
import polars as pl

from sitg_api import fetch_all

features = fetch_all(URL, with_geometry=False)
df = pl.from_dicts([f["attributes"] for f in features], infer_schema_length=None)
display(df)
```

#### Avec géométrie

```python
import geopandas as gpd
from shapely.geometry import Polygon

features_geom = fetch_all(URL, with_geometry=True)
gdf = gpd.GeoDataFrame(
    [f["attributes"] for f in features_geom],
    geometry=[Polygon(f["geometry"]["rings"][0]) for f in features_geom],
    crs="EPSG:2056",
)
print(gdf)
```

#### Avec filtre

```python
import polars as pl

from sitg_api import fetch_all

features = fetch_all(
    URL,
    where="COMMUNE='Avully'",
)
df = pl.from_dicts([f["attributes"] for f in features], infer_schema_length=None)

print(df.head(2))
```

### IDC

Retourne directement un `pl.DataFrame` avec colonnes en minuscules, types castés et dédupliqué par `(egid, annee)` :

```python
from sitg_api.idc import fetch_idc_data

df = fetch_idc_data(egid=[1015054, 1015052])

print(df)
```

## Paramètres de `fetch_all`

| Paramètre      | Défaut  | Description                                      |
|----------------|---------|--------------------------------------------------|
| `fields`       | `"*"`   | Champs à retourner, ex. `"ID,NOM"`               |
| `where`        | `"1=1"` | Filtre SQL, ex. `"COMMUNE='Genève'"`             |
| `with_geometry`| `False` | Inclure la géométrie brute dans chaque feature   |
| `chunk_size`   | `1000`  | Features par requête (max serveur SITG)          |
| `max_workers`  | `4`     | Parallélisme des requêtes HTTP                   |
| `timeout`      | `120`   | Timeout HTTP en secondes                         |
| `max_retries`  | `4`     | Tentatives max par page avant exception          |
