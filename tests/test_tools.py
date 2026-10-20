"""Pruebas de las herramientas geoespaciales sobre la base real (Jalisco)."""

import pytest

from site_copilot.config import DB_PATH
from site_copilot.tools import geo

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="Corre primero site_copilot.data.build_db")

PROVIDENCIA = (20.695, -103.387)
FARMACIAS = ["464111", "464112"]


def test_buscar_actividad_ignora_acentos():
    con_acento = geo.buscar_actividad("café")["actividades"]
    sin_acento = geo.buscar_actividad("cafe")["actividades"]
    assert con_acento == sin_acento and con_acento


def test_municipio_ambiguo_pide_nombre_exacto():
    with pytest.raises(geo.ToolError, match="ambiguo"):
        geo._resolver_municipios(["Zapotl"])
    assert geo._resolver_municipios(["tonala"]) == ["14101"]


def test_competencia_crece_con_el_radio():
    chico = geo.competencia_cercana(*PROVIDENCIA, FARMACIAS, radio_m=500)
    grande = geo.competencia_cercana(*PROVIDENCIA, FARMACIAS, radio_m=1500)
    assert 0 < chico["competidores_total"] < grande["competidores_total"]
    distancias = [c["distancia_m"] for c in grande["mas_cercanos"]]
    assert distancias == sorted(distancias) and max(distancias) <= 1500


def test_competencia_coincide_con_conteo_por_area():
    por_radio = geo.competencia_cercana(*PROVIDENCIA, FARMACIAS, radio_m=1000)["competidores_total"]
    por_area = geo.competidores_en_area(geo.circle_sql(*PROVIDENCIA, 1000), FARMACIAS)["competidores_total"]
    assert abs(por_radio - por_area) <= 1  # bordes del círculo poligonizado


def test_perfil_zona_razonable():
    p = geo.perfil_zona(*PROVIDENCIA, radio_m=1000)
    assert 2.9 < p["area_km2"] < 3.3  # pi * 1 km²
    assert p["poblacion_estimada"] > 5000
    assert p["nse_quintil_municipal_prom"] >= 4  # Providencia es zona de NSE alto


def test_ranking_respeta_filtros_y_orden():
    res = geo.rankear_zonas(["Guadalajara"], FARMACIAS, nse_min=4, radio_m=1000, top_n=10)["candidatas"]
    assert len(res) == 10
    assert all(c["municipio"] == "Guadalajara" and c["nse_quintil_municipal"] >= 4 for c in res)
    scores = [c["hab_por_competidor"] for c in res]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("lat, lon", [(-103.387, 20.695), (40.0, -3.7)])
def test_rechaza_coordenadas_fuera_de_mexico(lat, lon):
    with pytest.raises(geo.ToolError):
        geo.perfil_zona(lat, lon)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/passwd')",
    "COPY negocios TO '/tmp/x.csv'",
    "DROP TABLE negocios",
    "SELECT 1; SELECT 2",
    "ATTACH '/tmp/otra.db'",
])
def test_sql_bloquea_escritura_y_archivos(sql):
    with pytest.raises(geo.ToolError):
        geo.consulta_sql(sql)


def test_sql_limita_filas():
    r = geo.consulta_sql("SELECT id FROM negocios")
    assert len(r["filas"]) == geo.SQL_ROW_LIMIT and r["truncado"]
