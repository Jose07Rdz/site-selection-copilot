"""Herramientas geoespaciales sobre la base DuckDB.

Cada función devuelve un dict serializable a JSON. Las llaves que empiezan con "_"
(geometrías GeoJSON) son para el mapa de la UI y el agente no las envía al modelo.
"""

import json
import math
import re
from functools import cache

import duckdb

from site_copilot.config import DB_PATH

LCC = "EPSG:6372"  # proyección métrica de INEGI para México
WGS84 = "EPSG:4326"
MAX_RADIO_M = 10_000
SQL_ROW_LIMIT = 200
RADIO_COMERCIO_M = 500  # radio para medir si una zona tiene actividad comercial
RADIO_CLUSTER_M = 150  # radio para encontrar el punto con más comercio alrededor


class ToolError(ValueError):
    """Error de entrada que el modelo puede corregir (se le devuelve como tool_result con is_error)."""


@cache
def con() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise RuntimeError("No existe la base. Corre: uv run python -m site_copilot.data.build_db")
    c = duckdb.connect(str(DB_PATH), read_only=True)
    c.sql("LOAD spatial")
    # Bloquea lectura/escritura de archivos y red desde SQL (p. ej. read_csv('/etc/...')).
    c.sql("SET enable_external_access = false; SET lock_configuration = true;")
    return c


def rows(sql: str, params: list | None = None) -> list[dict]:
    rel = con().execute(sql, params or [])
    cols = [d[0] for d in rel.description]
    return [dict(zip(cols, r)) for r in rel.fetchall()]


def _round(d: dict, nd: int = 3) -> dict:
    return {k: (round(v, nd) if isinstance(v, float) else v) for k, v in d.items()}


def _check_point(lat: float, lon: float) -> None:
    if not (14 <= lat <= 33 and -118 <= lon <= -86):
        raise ToolError(f"Coordenadas fuera de México: lat={lat}, lon={lon}. ¿Invertiste lat y lon?")


def _check_radio(radio_m: float) -> None:
    if not (50 <= radio_m <= MAX_RADIO_M):
        raise ToolError(f"radio_m debe estar entre 50 y {MAX_RADIO_M} metros.")


def _scian_filter(scian: list[str], col: str = "scian") -> tuple[str, list[str]]:
    """Filtro SQL por prefijos SCIAN (acepta códigos de 2 a 6 dígitos)."""
    codes = [s.strip() for s in scian if s.strip()]
    if not codes or not all(re.fullmatch(r"\d{2,6}", s) for s in codes):
        raise ToolError("scian debe ser una lista de códigos numéricos de 2 a 6 dígitos (usa buscar_actividad).")
    return "(" + " OR ".join(f"{col} LIKE ? || '%'" for _ in codes) + ")", codes


def circle_sql(lat: float, lon: float, radio_m: float) -> str:
    """Expresión SQL de un círculo métrico real (buffer en LCC, devuelto a WGS84)."""
    return (
        f"ST_Transform(ST_Buffer(ST_Transform(ST_Point({lon}, {lat}), '{WGS84}', '{LCC}', always_xy := true), {radio_m}),"
        f" '{LCC}', '{WGS84}', always_xy := true)"
    )


def _haversine_sql(lat: float, lon: float, lat_col: str = "lat", lon_col: str = "lon") -> str:
    return (
        f"2 * 6371000 * ASIN(SQRT(POWER(SIN(RADIANS({lat_col} - {lat}) / 2), 2) + "
        f"COS(RADIANS({lat})) * COS(RADIANS({lat_col})) * POWER(SIN(RADIANS({lon_col} - {lon}) / 2), 2)))"
    )


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------

def buscar_actividad(texto: str, limite: int = 15) -> dict:
    """Busca actividades económicas SCIAN por texto (sin importar acentos)."""
    patron = f"%{texto.strip()}%"
    res = rows("""
        SELECT scian, actividad, COUNT(*) AS negocios
        FROM negocios
        WHERE strip_accents(lower(actividad)) LIKE strip_accents(lower(?))
        GROUP BY ALL ORDER BY negocios DESC LIMIT ?
    """, [patron, limite])
    if not res:
        raise ToolError(f"No encontré actividades con '{texto}'. Prueba con un término más general.")
    return {"actividades": res}


def buscar_municipio(texto: str) -> dict:
    """Busca municipios por nombre (sin importar acentos)."""
    res = rows("""
        SELECT cve_mun, municipio, entidad, poblacion, area_km2
        FROM municipios
        WHERE strip_accents(lower(municipio)) LIKE strip_accents(lower(?))
        ORDER BY poblacion DESC LIMIT 10
    """, [f"%{texto.strip()}%"])
    if not res:
        raise ToolError(f"No hay municipios que coincidan con '{texto}' en la base.")
    return {"municipios": res}


def _resolver_municipios(nombres: list[str]) -> list[str]:
    claves = []
    for n in nombres:
        exactos = rows("SELECT cve_mun FROM municipios WHERE strip_accents(lower(municipio)) = strip_accents(lower(?))", [n.strip()])
        if exactos:
            claves.append(exactos[0]["cve_mun"])
            continue
        candidatos = buscar_municipio(n)["municipios"]
        if len(candidatos) > 1:
            raise ToolError(f"'{n}' es ambiguo: {[c['municipio'] for c in candidatos]}. Usa el nombre exacto.")
        claves.append(candidatos[0]["cve_mun"])
    return claves


# ---------------------------------------------------------------------------
# Análisis de un área
# ---------------------------------------------------------------------------

def perfil_area(area_sql: str) -> dict:
    """Perfil demográfico y comercial de un polígono (expresión SQL en WGS84).

    La población de cada AGEB se reparte proporcional al área que cae dentro del polígono.
    """
    demo = rows(f"""
        WITH zona AS (SELECT {area_sql} AS g),
        cruce AS (
            SELECT a.*, ST_Area(ST_Intersection(a.geom, zona.g)) / NULLIF(ST_Area(a.geom), 0) AS frac
            FROM agebs a, zona
            WHERE ST_Intersects(a.geom, zona.g)
        )
        SELECT
            ROUND(ST_Area(ST_Transform((SELECT g FROM zona), '{WGS84}', '{LCC}', always_xy := true)) / 1e6, 2) AS area_km2,
            COUNT(*) AS agebs,
            ROUND(SUM(poblacion * frac)) AS poblacion_estimada,
            ROUND(SUM(viviendas_hab * frac)) AS viviendas_habitadas,
            SUM(escolaridad_prom * poblacion * frac) / NULLIF(SUM(poblacion * frac) FILTER (WHERE escolaridad_prom IS NOT NULL), 0) AS escolaridad_prom,
            SUM(viv_con_auto * frac) / NULLIF(SUM(viviendas_hab * frac) FILTER (WHERE viv_con_auto IS NOT NULL), 0) AS pct_viv_auto,
            SUM(nse_quintil_municipal * poblacion * frac) / NULLIF(SUM(poblacion * frac) FILTER (WHERE nse_quintil_municipal IS NOT NULL), 0) AS nse_quintil_municipal_prom
        FROM cruce
    """)[0]
    negocios = rows(f"""
        WITH zona AS (SELECT {area_sql} AS g)
        SELECT actividad, COUNT(*) AS n
        FROM negocios, zona WHERE ST_Within(negocios.geom, zona.g)
        GROUP BY actividad ORDER BY n DESC
    """)
    return {
        **_round(demo, 2),
        "negocios_total": sum(r["n"] for r in negocios),
        "top_actividades": negocios[:10],
    }


def competidores_en_area(area_sql: str, scian: list[str]) -> dict:
    filtro, params = _scian_filter(scian)
    res = rows(f"""
        WITH zona AS (SELECT {area_sql} AS g)
        SELECT actividad, COUNT(*) AS n FROM negocios, zona
        WHERE ST_Within(negocios.geom, zona.g) AND {filtro}
        GROUP BY actividad ORDER BY n DESC
    """, params)
    return {"competidores_total": sum(r["n"] for r in res), "por_actividad": res}


def perfil_zona(lat: float, lon: float, radio_m: float = 1000) -> dict:
    """Perfil demográfico, socioeconómico y comercial en un radio alrededor de un punto."""
    _check_point(lat, lon)
    _check_radio(radio_m)
    circulo = circle_sql(lat, lon, radio_m)
    out = {"centro": {"lat": lat, "lon": lon}, "radio_m": radio_m, **perfil_area(circulo)}
    out["_geojson"] = json.loads(rows(f"SELECT ST_AsGeoJSON({circulo}) AS g")[0]["g"])
    return out


def competencia_cercana(lat: float, lon: float, scian: list[str], radio_m: float = 1000, max_listado: int = 10) -> dict:
    """Competidores (por códigos SCIAN) dentro de un radio, con los más cercanos y su distancia."""
    _check_point(lat, lon)
    _check_radio(radio_m)
    filtro, params = _scian_filter(scian)
    dist = _haversine_sql(lat, lon)
    cerca = rows(f"""
        WITH zona AS (SELECT {circle_sql(lat, lon, radio_m)} AS g)
        SELECT id, nombre, actividad, personal_ocupado, lat, lon, ROUND({dist}) AS distancia_m
        FROM negocios, zona
        WHERE ST_Intersects(negocios.geom, zona.g) AND {filtro}
        ORDER BY distancia_m
    """, params)
    conteo: dict[str, int] = {}
    for r in cerca:
        conteo[r["actividad"]] = conteo.get(r["actividad"], 0) + 1
    return {
        "radio_m": radio_m,
        "competidores_total": len(cerca),
        "por_actividad": [{"actividad": k, "n": v} for k, v in sorted(conteo.items(), key=lambda kv: -kv[1])],
        "mas_cercanos": cerca[:max_listado],
        "_puntos": cerca,
    }


# ---------------------------------------------------------------------------
# Ranking de zonas candidatas
# ---------------------------------------------------------------------------

def _punto_comercial(lat: float, lon: float) -> dict:
    """Dentro de RADIO_COMERCIO_M, el comercio al por menor con más comercios a RADIO_CLUSTER_M.

    Aproxima "dónde están los locales" de la zona, en vez del centroide de la AGEB,
    que puede caer en medio de un fraccionamiento sin uso comercial.
    """
    d = RADIO_COMERCIO_M / 111_000
    res = rows(f"""
        WITH r AS (
            SELECT id, lat, lon FROM negocios
            WHERE scian_sector = '46'
              AND lat BETWEEN {lat - d} AND {lat + d} AND lon BETWEEN {lon - d * 1.1} AND {lon + d * 1.1}
              AND {_haversine_sql(lat, lon)} <= {RADIO_COMERCIO_M}
        )
        -- Agrupar por id (no por coordenada): varios locales de una plaza comparten el mismo punto.
        SELECT a.lat, a.lon, COUNT(*) AS comercios_150m
        FROM r a JOIN r b ON {_haversine_sql('a.lat', 'a.lon', 'b.lat', 'b.lon')} <= {RADIO_CLUSTER_M}
        GROUP BY a.id, a.lat, a.lon
        ORDER BY comercios_150m DESC, {_haversine_sql(lat, lon, 'a.lat', 'a.lon')}
        LIMIT 1
    """)
    return res[0] if res else {"lat": lat, "lon": lon, "comercios_150m": 0}


def rankear_zonas(
    municipios: list[str],
    scian: list[str],
    nse_min: int = 1,
    radio_m: float = 1000,
    top_n: int = 10,
    min_comercio: int = 25,
) -> dict:
    """Ordena AGEB urbanas como candidatas para abrir un negocio.

    Para el centroide de cada AGEB calcula, en un radio dado, la población (demanda) y el
    número de competidores. El score es habitantes por competidor: poblacion_radio / (competidores + 1).
    Descarta zonas con menos de `min_comercio` comercios al por menor a 500 m: sin comercio
    suele tratarse de fraccionamientos residenciales donde no hay locales disponibles.
    """
    _check_radio(radio_m)
    if not 1 <= nse_min <= 5:
        raise ToolError("nse_min va de 1 (más bajo) a 5 (más alto).")
    if not 0 <= min_comercio <= 500:
        raise ToolError("min_comercio va de 0 a 500.")
    top_n = max(1, min(top_n, 30))
    claves = _resolver_municipios(municipios)
    filtro, params = _scian_filter(scian, "n.scian")
    filtro_punto, _ = _scian_filter(scian)
    # Prefiltro rectangular en grados antes del cálculo de distancia (latitudes de México ~15-33°).
    dlat = radio_m / 111_000
    dlon = radio_m / (111_000 * math.cos(math.radians(33)))
    mun_ph = ", ".join("?" for _ in claves)
    res = rows(f"""
        WITH cand AS (
            SELECT cvegeo, cve_mun, localidad, poblacion, nse_quintil_municipal, escolaridad_prom, pct_viv_auto,
                   comercio_500m, ST_Y(ST_Centroid(geom)) AS lat, ST_X(ST_Centroid(geom)) AS lon
            FROM agebs
            WHERE cve_mun IN ({mun_ph}) AND poblacion > 0 AND nse_quintil_municipal >= ? AND comercio_500m >= ?
        ),
        cen AS (
            SELECT poblacion, ST_Y(ST_Centroid(geom)) AS lat, ST_X(ST_Centroid(geom)) AS lon
            FROM agebs WHERE poblacion > 0
        ),
        demanda AS (
            SELECT c.cvegeo, SUM(x.poblacion) AS poblacion_radio
            FROM cand c JOIN cen x
              ON x.lat BETWEEN c.lat - {dlat} AND c.lat + {dlat}
             AND x.lon BETWEEN c.lon - {dlon} AND c.lon + {dlon}
            WHERE {_haversine_sql('c.lat', 'c.lon', 'x.lat', 'x.lon')} <= {radio_m}
            GROUP BY c.cvegeo
        ),
        comp AS (
            SELECT c.cvegeo, COUNT(n.id) AS competidores,
                   MIN({_haversine_sql('c.lat', 'c.lon', 'n.lat', 'n.lon')}) AS dist_competidor_m
            FROM cand c JOIN negocios n
              ON n.lat BETWEEN c.lat - {dlat} AND c.lat + {dlat}
             AND n.lon BETWEEN c.lon - {dlon} AND c.lon + {dlon}
            WHERE {filtro} AND {_haversine_sql('c.lat', 'c.lon', 'n.lat', 'n.lon')} <= {radio_m}
            GROUP BY c.cvegeo
        )
        SELECT c.cvegeo, m.municipio, c.localidad, c.lat, c.lon, c.nse_quintil_municipal,
               c.escolaridad_prom, c.pct_viv_auto,
               d.poblacion_radio,
               COALESCE(k.competidores, 0) AS competidores,
               k.dist_competidor_m,
               c.comercio_500m,
               d.poblacion_radio / (COALESCE(k.competidores, 0) + 1) AS hab_por_competidor
        FROM cand c
        JOIN demanda d USING (cvegeo)
        JOIN municipios m USING (cve_mun)
        LEFT JOIN comp k USING (cvegeo)
        ORDER BY hab_por_competidor DESC
        LIMIT ?
    """, [*claves, nse_min, min_comercio, *params, top_n])

    candidatas = []
    for r in res:
        r = _round(r, 4)
        centro_lat, centro_lon = r.pop("lat"), r.pop("lon")
        punto = _punto_comercial(centro_lat, centro_lon)
        plat, plon = punto["lat"], punto["lon"]
        # Competencia medida también en el punto sugerido, que es lo que el usuario visitaría.
        comp_punto = rows(f"""
            SELECT COUNT(*) AS n, MIN({_haversine_sql(plat, plon)}) AS d FROM negocios
            WHERE {filtro_punto}
              AND lat BETWEEN {plat - dlat} AND {plat + dlat} AND lon BETWEEN {plon - dlon} AND {plon + dlon}
              AND {_haversine_sql(plat, plon)} <= {radio_m}
        """, params)[0]
        r["punto_sugerido"] = {
            "lat": round(plat, 6), "lon": round(plon, 6), "comercios_150m": punto["comercios_150m"],
            "competidores_radio": comp_punto["n"],
            "dist_competidor_m": round(comp_punto["d"]) if comp_punto["d"] is not None else None,
        }
        candidatas.append(r)
    return {
        "metodo": (
            f"Por AGEB (desde su centroide): población en {radio_m:.0f} m / (competidores en {radio_m:.0f} m + 1), "
            f"solo zonas con al menos {min_comercio} comercios al por menor a {RADIO_COMERCIO_M} m. "
            f"punto_sugerido = el comercio con más comercios a {RADIO_CLUSTER_M} m dentro de esos {RADIO_COMERCIO_M} m, "
            f"con sus propios competidores en {radio_m:.0f} m (reporta estos al usuario); "
            "valida ahí con perfil_zona / competencia_cercana. Población del Censo 2020."
        ),
        "candidatas": candidatas,
        "_puntos": [{"cvegeo": c["cvegeo"], **c["punto_sugerido"]} for c in candidatas],
    }


# ---------------------------------------------------------------------------
# SQL libre (solo lectura)
# ---------------------------------------------------------------------------

SCHEMA_DOC = """\
Tablas (DuckDB + extensión spatial, geometrías EPSG:4326):
- municipios(cve_mun, cve_ent, entidad, municipio, poblacion, area_km2, geom)
- agebs(cvegeo, cve_mun, localidad, poblacion, pob_18_mas, escolaridad_prom, pea, pob_derechohabiente,
        viviendas_hab, ocupantes_prom, viv_con_auto, viv_con_internet, viv_con_pc, densidad_hab_km2,
        pct_viv_auto, pct_viv_internet, pct_viv_pc, area_km2, nse_score, nse_quintil_estatal,
        nse_quintil_municipal, comercio_500m, geom)
        -- quintiles 1 = más bajo, 5 = más alto; comercio_500m = comercios al por menor a 500 m del centroide
- negocios(id, nombre, razon_social, scian, scian_sector, actividad, personal_ocupado,
        tipo_centro_comercial, centro_comercial, cod_postal, cve_mun, municipio, localidad,
        cvegeo_ageb, lat, lon, fecha_alta, geom)
Para distancias en metros transforma a EPSG:6372: ST_Transform(geom, 'EPSG:4326', 'EPSG:6372', always_xy := true)."""


def consulta_sql(sql: str) -> dict:
    """Ejecuta una consulta SELECT de solo lectura (máximo 200 filas)."""
    q = sql.strip().rstrip(";").strip()
    if ";" in q:
        raise ToolError("Solo se permite una sentencia.")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise ToolError("Solo se permiten consultas SELECT / WITH.")
    try:
        rel = con().execute(f"SELECT * FROM ({q}) LIMIT {SQL_ROW_LIMIT + 1}")
    except duckdb.Error as e:
        raise ToolError(f"Error de SQL: {e}") from e
    cols = [d[0] for d in rel.description]
    data = []
    for r in rel.fetchall():
        data.append({c: ("<geometría: usa ST_AsText o ST_X/ST_Y>" if isinstance(v, (bytes, bytearray)) else v) for c, v in zip(cols, r)})
    return {"filas": data[:SQL_ROW_LIMIT], "truncado": len(data) > SQL_ROW_LIMIT}
