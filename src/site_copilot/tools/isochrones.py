"""Isócronas sobre la red vial de OpenStreetMap (OSMnx) y su perfil con datos de INEGI.

La red para auto de cada zona metropolitana se precarga una vez con
`python -m site_copilot.tools.isochrones` (el servidor público de Overpass suele saturarse).
Las redes peatonales, más pequeñas, se descargan bajo demanda con reintentos.

Uso:
    uv run python -m site_copilot.tools.isochrones   # precarga ZM Guadalajara
"""

import json
import math
import pickle
import time
from functools import cache, lru_cache

import networkx as nx
import numpy as np
import osmnx as ox
import requests
import shapely

from site_copilot.config import DATA_DIR
from site_copilot.tools.geo import ToolError, _check_point, competidores_en_area, perfil_area

ox.settings.use_cache = True
ox.settings.cache_folder = str(DATA_DIR / "osm_cache")
ox.settings.log_console = False
ox.settings.requests_timeout = 300

OSM_DIR = DATA_DIR / "osm"
# Zonas con red de auto precargada: (norte, sur, este, oeste).
ZONAS = {
    "zm_guadalajara": (20.82, 20.45, -103.15, -103.55),
}

MODOS = {
    # network_type de OSMnx, minutos máximos, velocidad para dimensionar la descarga (km/h)
    "auto": ("drive", 20, 40),
    "pie": ("walk", 30, 5),
}
VELOCIDAD_PIE_KMH = 4.8
# OSM da velocidades de flujo libre; en ciudad el tráfico real es bastante menor.
# Escalamos el tiempo disponible como si el auto circulara al 60 % de esa velocidad.
FACTOR_TRAFICO = 0.6


def _con_reintentos(fn, intentos: int = 4, espera_s: float = 5):
    for i in range(intentos):
        try:
            return fn()
        except (ox._errors.ResponseStatusCodeError, ox._errors.InsufficientResponseError, requests.RequestException) as e:
            if i == intentos - 1:
                raise ToolError(f"OpenStreetMap (Overpass) no respondió; intenta más tarde. Detalle: {str(e)[:120]}") from e
            time.sleep(espera_s * 2**i)


def _con_tiempos(G: nx.MultiDiGraph, modo: str) -> nx.MultiDiGraph:
    if modo == "auto":
        G = ox.routing.add_edge_speeds(G)  # usa maxspeed de OSM o imputa por tipo de vía
        return ox.routing.add_edge_travel_times(G)
    mps = VELOCIDAD_PIE_KMH * 1000 / 3600
    for _, _, d in G.edges(data=True):
        d["travel_time"] = d["length"] / mps
    return G


@cache
def _grafo_zona(zona: str) -> nx.MultiDiGraph | None:
    graphml = OSM_DIR / f"{zona}_drive.graphml"
    pkl = graphml.with_suffix(".pkl")  # copia binaria propia: carga ~10x más rápido que GraphML
    if pkl.exists() and pkl.stat().st_mtime >= graphml.stat().st_mtime:
        with open(pkl, "rb") as f:
            return pickle.load(f)
    if not graphml.exists():
        return None
    G = ox.load_graphml(graphml)
    with open(pkl, "wb") as f:
        pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
    return G


# Vías por donde no se camina; el resto de la red de auto sirve como aproximación peatonal.
NO_PEATONAL = {"motorway", "motorway_link", "trunk", "trunk_link"}


@cache
def _grafo_pie_zona(zona: str) -> nx.MultiGraph | None:
    """Red peatonal aproximada a partir de la red de auto precargada: sin sentido único ni autopistas.

    No incluye andadores ni calles peatonales, así que tiende a subestimar ligeramente el área.
    """
    G = _grafo_zona(zona)
    if G is None:
        return None
    mps = VELOCIDAD_PIE_KMH * 1000 / 3600

    def caminable(u, v, k) -> bool:
        hw = G.edges[u, v, k].get("highway")
        tipos = set(hw) if isinstance(hw, list) else {hw}
        return not tipos <= NO_PEATONAL

    H = nx.MultiGraph(nx.subgraph_view(G, filter_edge=caminable))
    for _, _, d in H.edges(data=True):
        d["travel_time"] = d["length"] / mps
    return H


def _zona_que_contiene(lat: float, lon: float, margen: float) -> str | None:
    for nombre, (n, s, e, w) in ZONAS.items():
        if s + margen <= lat <= n - margen and w + margen <= lon <= e - margen:
            return nombre
    return None


@lru_cache(maxsize=8)
def _grafo(lat: float, lon: float, modo: str, dist_m: int) -> nx.MultiDiGraph:
    zona = _zona_que_contiene(lat, lon, margen=dist_m / 111_000)
    if zona:
        G = _grafo_zona(zona) if modo == "auto" else _grafo_pie_zona(zona)
        if G is not None:
            return G
    network_type = MODOS[modo][0]
    G = _con_reintentos(lambda: ox.graph_from_point((lat, lon), dist=dist_m, network_type=network_type, simplify=True))
    return _con_tiempos(G, modo)


def precargar(zona: str = "zm_guadalajara") -> None:
    n, s, e, w = ZONAS[zona]
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Descargando red vial de auto para {zona} (puede tardar varios minutos)...")
    G = _con_reintentos(lambda: ox.graph_from_bbox((w, s, e, n), network_type="drive", simplify=True), intentos=6)
    G = _con_tiempos(G, "auto")
    path = OSM_DIR / f"{zona}_drive.graphml"
    ox.save_graphml(G, path)
    print(f"Guardado {path.name}: {len(G.nodes):,} nodos, {len(G.edges):,} aristas")


_NODOS_PRINCIPALES: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def _nodos_red_principal(G: nx.MultiGraph | nx.MultiDiGraph) -> tuple[np.ndarray, np.ndarray]:
    """Ids y coordenadas de los nodos de la componente conexa más grande (calculado una vez por grafo).

    Evita arrancar la isócrona en un tramo aislado (estacionamiento, privada) desde donde no se llega a ningún lado.
    """
    if id(G) not in _NODOS_PRINCIPALES:
        comps = nx.strongly_connected_components(G) if G.is_directed() else nx.connected_components(G)
        ids = np.array(list(max(comps, key=len)))
        xy = np.array([(G.nodes[n]["x"], G.nodes[n]["y"]) for n in ids])
        _NODOS_PRINCIPALES[id(G)] = (ids, xy)
    return _NODOS_PRINCIPALES[id(G)]


def _nodo_mas_cercano(G: nx.MultiGraph | nx.MultiDiGraph, lat: float, lon: float) -> int:
    ids, xy = _nodos_red_principal(G)
    d2 = ((xy[:, 0] - lon) * math.cos(math.radians(lat))) ** 2 + (xy[:, 1] - lat) ** 2
    return int(ids[int(np.argmin(d2))])


def poligono_isocrona(lat: float, lon: float, minutos: float, modo: str = "auto") -> shapely.Polygon:
    _check_point(lat, lon)
    if modo not in MODOS:
        raise ToolError(f"modo debe ser uno de {list(MODOS)}.")
    _, max_min, kmh = MODOS[modo]
    if not 1 <= minutos <= max_min:
        raise ToolError(f"Para modo '{modo}' los minutos van de 1 a {max_min}.")
    # Radio de descarga: lo máximo que se podría recorrer, redondeado para reutilizar caché.
    dist_m = int(np.ceil(minutos / 60 * kmh * 1000 / 500) * 500) + 500
    G = _grafo(round(lat, 3), round(lon, 3), modo, dist_m)
    origen = _nodo_mas_cercano(G, lat, lon)
    segundos = minutos * 60 * (FACTOR_TRAFICO if modo == "auto" else 1)
    alcanzable = nx.ego_graph(G, origen, radius=segundos, distance="travel_time")
    puntos = shapely.MultiPoint([(d["x"], d["y"]) for _, d in alcanzable.nodes(data=True)])
    if len(puntos.geoms) < 4:
        raise ToolError("La red vial alrededor del punto es demasiado pequeña para calcular la isócrona.")
    # Envolvente cóncava de los nodos alcanzables + pequeño margen (~60 m) para incluir frentes de calle.
    return shapely.concave_hull(puntos, ratio=0.3).buffer(0.0006).simplify(0.0002)


def isocrona(lat: float, lon: float, minutos: float, modo: str = "auto", scian: list[str] | None = None) -> dict:
    """Área alcanzable en N minutos y su perfil demográfico/comercial (y competencia si se dan códigos SCIAN)."""
    poly = poligono_isocrona(lat, lon, minutos, modo)
    area_sql = f"ST_GeomFromText('{poly.wkt}')"
    out = {"centro": {"lat": lat, "lon": lon}, "minutos": minutos, "modo": modo, **perfil_area(area_sql)}
    if scian:
        out["competencia"] = competidores_en_area(area_sql, scian)
        out["_puntos"] = out["competencia"].pop("_puntos")
    out["_geojson"] = json.loads(shapely.to_geojson(poly))
    return out


if __name__ == "__main__":
    precargar()
