"""Construye la base DuckDB (con extensión spatial) a partir de los datos crudos de INEGI.

Tablas resultantes (geometrías en EPSG:4326):
    municipios  polígonos municipales con nombre
    agebs       AGEB urbanas con indicadores del Censo 2020 y un índice NSE aproximado
    negocios    unidades económicas del DENUE como puntos, con su sector SCIAN

Uso:
    uv run python -m site_copilot.data.build_db
"""

from pathlib import Path

import duckdb

from site_copilot.config import DB_PATH, RAW_DIR

# Marco Geoestadístico 2020: MEXICO_ITRF_2008_LCC.
SRC_CRS = "EPSG:6372"


def states_available() -> list[str]:
    return sorted(p.name for p in (RAW_DIR / "marco").iterdir() if p.is_dir())


def union_sql(selects: list[str]) -> str:
    return "\nUNION ALL\n".join(selects)


def to_wgs84(col: str = "geom") -> str:
    return f"ST_Transform({col}, '{SRC_CRS}', 'EPSG:4326', always_xy := true)"


def census_csv(cve: str) -> Path:
    return next((RAW_DIR / "censo" / cve).glob("*/conjunto_de_datos/conjunto_de_datos_ageb_urbana_*.csv"))


def shp(cve: str, layer: str) -> Path:
    return RAW_DIR / "marco" / cve / "conjunto_de_datos" / f"{cve}{layer}.shp"


def denue_csv(cve: str) -> Path:
    return next((RAW_DIR / "denue" / cve / "conjunto_de_datos").glob("denue_inegi_*.csv"))


def build(con: duckdb.DuckDBPyConnection, states: list[str]) -> None:
    con.sql("INSTALL spatial; LOAD spatial;")

    # --- Censo 2020: todas las filas (entidad, municipio, localidad, AGEB, manzana) ---
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE censo_raw AS
        {union_sql([f"SELECT * FROM read_csv('{census_csv(c)}', encoding = 'latin-1', all_varchar = true)" for c in states])}
    """)

    # --- Municipios: geometría del Marco Geoestadístico + nombre desde el censo ---
    # Los nombres del .dbf vienen en Latin-1 y GDAL en DuckDB no los decodifica bien,
    # así que solo leemos claves (ASCII) y geometría.
    con.sql(f"""
        CREATE OR REPLACE TABLE municipios AS
        WITH geo AS (
            {union_sql([f"SELECT CVEGEO, CVE_ENT, CVE_MUN, geom FROM ST_Read('{shp(c, 'mun')}')" for c in states])}
        )
        SELECT
            g.CVEGEO AS cve_mun,
            g.CVE_ENT AS cve_ent,
            c.NOM_ENT AS entidad,
            c.NOM_MUN AS municipio,
            TRY_CAST(c.POBTOT AS INTEGER) AS poblacion,
            ROUND(ST_Area(g.geom) / 1e6, 2) AS area_km2,
            {to_wgs84('g.geom')} AS geom
        FROM geo g
        LEFT JOIN censo_raw c
            ON c.ENTIDAD = g.CVE_ENT AND c.MUN = g.CVE_MUN
           AND c.LOC = '0000' AND c.AGEB = '0000' AND c.MZA = '000'
    """)

    # --- AGEB urbanas: geometría + indicadores del censo ---
    # Las celdas protegidas por confidencialidad vienen como '*' o 'N/D' -> NULL con TRY_CAST.
    con.sql(f"""
        CREATE OR REPLACE TABLE agebs AS
        WITH geo AS (
            {union_sql([f"SELECT CVEGEO, CVE_ENT, CVE_MUN, CVE_LOC, CVE_AGEB, geom FROM ST_Read('{shp(c, 'a')}')" for c in states])}
        ),
        censo AS (
            SELECT
                ENTIDAD || MUN || LOC || AGEB AS cvegeo,
                NOM_LOC AS localidad,
                TRY_CAST(POBTOT AS INTEGER) AS poblacion,
                TRY_CAST(P_18YMAS AS INTEGER) AS pob_18_mas,
                TRY_CAST(GRAPROES AS DOUBLE) AS escolaridad_prom,
                TRY_CAST(PEA AS INTEGER) AS pea,
                TRY_CAST(PDER_SS AS INTEGER) AS pob_derechohabiente,
                TRY_CAST(TVIVHAB AS INTEGER) AS viviendas_hab,
                TRY_CAST(PROM_OCUP AS DOUBLE) AS ocupantes_prom,
                TRY_CAST(VPH_AUTOM AS INTEGER) AS viv_con_auto,
                TRY_CAST(VPH_INTER AS INTEGER) AS viv_con_internet,
                TRY_CAST(VPH_PC AS INTEGER) AS viv_con_pc
            FROM censo_raw
            WHERE AGEB <> '0000' AND MZA = '000'
        ),
        base AS (
            SELECT
                g.CVEGEO AS cvegeo,
                g.CVE_ENT || g.CVE_MUN AS cve_mun,
                c.* EXCLUDE (cvegeo),
                ST_Area(g.geom) / 1e6 AS area_km2,
                {to_wgs84('g.geom')} AS geom
            FROM geo g
            LEFT JOIN censo c ON c.cvegeo = g.CVEGEO
        ),
        ratios AS (
            SELECT *,
                poblacion / NULLIF(area_km2, 0) AS densidad_hab_km2,
                viv_con_auto / NULLIF(viviendas_hab, 0) AS pct_viv_auto,
                viv_con_internet / NULLIF(viviendas_hab, 0) AS pct_viv_internet,
                viv_con_pc / NULLIF(viviendas_hab, 0) AS pct_viv_pc
            FROM base
        ),
        score AS (
            -- Proxy de nivel socioeconómico: promedio de z-scores de escolaridad y equipamiento
            -- de la vivienda, calculado por entidad. No es el NSE AMAI; es un indicador relativo.
            SELECT *,
                (  (escolaridad_prom - AVG(escolaridad_prom) OVER w) / STDDEV(escolaridad_prom) OVER w
                 + (pct_viv_auto     - AVG(pct_viv_auto)     OVER w) / STDDEV(pct_viv_auto)     OVER w
                 + (pct_viv_internet - AVG(pct_viv_internet) OVER w) / STDDEV(pct_viv_internet) OVER w
                 + (pct_viv_pc       - AVG(pct_viv_pc)       OVER w) / STDDEV(pct_viv_pc)       OVER w
                ) / 4 AS nse_score
            FROM ratios
            WINDOW w AS (PARTITION BY LEFT(cvegeo, 2))
        )
        SELECT
            * EXCLUDE (geom, area_km2, nse_score),
            ROUND(area_km2, 3) AS area_km2,
            ROUND(nse_score, 3) AS nse_score,
            -- Quintiles: 1 = más bajo, 5 = más alto. El estatal compara contra todo el estado
            -- (incluye cabeceras pequeñas); el municipal compara dentro de la misma ciudad/municipio.
            CASE WHEN nse_score IS NOT NULL
                 THEN NTILE(5) OVER (PARTITION BY LEFT(cvegeo, 2), nse_score IS NULL ORDER BY nse_score)
            END AS nse_quintil_estatal,
            CASE WHEN nse_score IS NOT NULL
                 THEN NTILE(5) OVER (PARTITION BY cve_mun, nse_score IS NULL ORDER BY nse_score)
            END AS nse_quintil_municipal,
            geom
        FROM score
    """)

    # --- DENUE: negocios como puntos ---
    con.sql(f"""
        CREATE OR REPLACE TABLE negocios AS
        WITH raw AS (
            {union_sql([f"SELECT * FROM read_csv('{denue_csv(c)}', encoding = 'latin-1', all_varchar = true)" for c in states])}
        )
        SELECT
            TRY_CAST(id AS BIGINT) AS id,
            TRIM(nom_estab) AS nombre,
            TRIM(raz_social) AS razon_social,
            codigo_act AS scian,
            LEFT(codigo_act, 2) AS scian_sector,
            TRIM(nombre_act) AS actividad,
            per_ocu AS personal_ocupado,
            tipoCenCom AS tipo_centro_comercial,
            TRIM(nom_CenCom) AS centro_comercial,
            cod_postal,
            cve_ent || cve_mun AS cve_mun,
            TRIM(municipio) AS municipio,
            TRIM(localidad) AS localidad,
            cve_ent || cve_mun || cve_loc || ageb AS cvegeo_ageb,
            TRY_CAST(latitud AS DOUBLE) AS lat,
            TRY_CAST(longitud AS DOUBLE) AS lon,
            fecha_alta,
            ST_Point(TRY_CAST(longitud AS DOUBLE), TRY_CAST(latitud AS DOUBLE)) AS geom
        FROM raw
        WHERE TRY_CAST(latitud AS DOUBLE) IS NOT NULL
    """)

    for table in ("municipios", "agebs", "negocios"):
        con.sql(f"CREATE INDEX {table}_geom_idx ON {table} USING RTREE (geom)")


def summary(con: duckdb.DuckDBPyConnection) -> None:
    print(con.sql("""
        SELECT 'municipios' AS tabla, COUNT(*) AS filas FROM municipios
        UNION ALL SELECT 'agebs', COUNT(*) FROM agebs
        UNION ALL SELECT 'agebs con censo', COUNT(poblacion) FROM agebs
        UNION ALL SELECT 'agebs con nse', COUNT(nse_score) FROM agebs
        UNION ALL SELECT 'negocios', COUNT(*) FROM negocios
    """))


def main() -> None:
    states = states_available()
    if not states:
        raise SystemExit("No hay datos en data/raw. Corre primero: uv run python -m site_copilot.data.download")
    print(f"Construyendo {DB_PATH.name} para entidades {states}...")
    DB_PATH.unlink(missing_ok=True)
    with duckdb.connect(str(DB_PATH)) as con:
        build(con, states)
        summary(con)


if __name__ == "__main__":
    main()
