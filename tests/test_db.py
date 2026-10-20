"""Chequeos de integridad sobre la base construida (requieren correr build_db antes)."""

import duckdb
import pytest

from site_copilot.config import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="Corre primero site_copilot.data.build_db")


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(str(DB_PATH), read_only=True)
    c.sql("LOAD spatial")
    yield c
    c.close()


def scalar(con, sql):
    return con.sql(sql).fetchone()[0]


def test_tablas_no_vacias(con):
    for table in ("municipios", "agebs", "negocios"):
        assert scalar(con, f"SELECT COUNT(*) FROM {table}") > 0


def test_nombres_con_acentos(con):
    assert scalar(con, "SELECT COUNT(*) FROM municipios WHERE municipio = 'Tlajomulco de Zúñiga'") == 1


def test_coordenadas_en_wgs84(con):
    lon, lat = con.sql("SELECT ST_X(ST_Centroid(geom)), ST_Y(ST_Centroid(geom)) FROM municipios LIMIT 1").fetchone()
    assert -118 < lon < -86 and 14 < lat < 33


def test_poblacion_agebs_cuadra_con_municipio(con):
    # En municipios casi 100 % urbanos la suma de AGEB urbanas debe aproximar el total municipal.
    pob_mun, pob_agebs = con.sql("""
        SELECT m.poblacion, SUM(a.poblacion)
        FROM municipios m JOIN agebs a USING (cve_mun)
        WHERE m.cve_mun = '14039'
        GROUP BY m.poblacion
    """).fetchone()
    assert abs(pob_mun - pob_agebs) / pob_mun < 0.01


def test_negocios_caen_en_su_ageb(con):
    pct = scalar(con, """
        WITH s AS (SELECT * FROM negocios USING SAMPLE 5000 ROWS (reservoir, 42))
        SELECT AVG(ST_Within(s.geom, a.geom)::INT) FROM s JOIN agebs a ON a.cvegeo = s.cvegeo_ageb
    """)
    assert pct > 0.98


def test_nse_quintiles_en_rango(con):
    lo, hi = con.sql("SELECT MIN(nse_quintil_municipal), MAX(nse_quintil_municipal) FROM agebs").fetchone()
    assert (lo, hi) == (1, 5)
