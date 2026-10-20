"""Rutas y fuentes de datos del proyecto."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "site_copilot.duckdb"

# Claves de entidad INEGI (2 dígitos). Empezamos con Jalisco (ZM Guadalajara).
DEFAULT_STATES = ["14"]

# INEGI responde HTML a peticiones sin User-Agent de navegador.
USER_AGENT = "Mozilla/5.0 (site-selection-copilot; portfolio project)"

INEGI = "https://www.inegi.org.mx/contenidos"

SOURCES = {
    # Directorio Estadístico Nacional de Unidades Económicas (negocios con coordenadas).
    "denue": INEGI + "/masiva/denue/denue_{cve}_csv.zip",
    # Censo de Población y Vivienda 2020, resultados por AGEB y manzana urbana.
    "censo": INEGI + "/programas/ccpv/2020/datosabiertos/ageb_manzana/"
    "ageb_mza_urbana_{cve}_cpv2020_csv.zip",
    # Marco Geoestadístico 2020 (polígonos de AGEB, manzanas, municipios...).
    "marco": INEGI + "/productos/prod_serv/contenidos/espanol/bvinegi/productos/"
    "geografia/marcogeo/889463807469/{cve}_{slug}.zip",
}

# Nombre de archivo del Marco Geoestadístico por entidad.
STATE_SLUGS = {
    "01": "aguascalientes", "02": "bajacalifornia", "03": "bajacaliforniasur",
    "04": "campeche", "05": "coahuiladezaragoza", "06": "colima", "07": "chiapas",
    "08": "chihuahua", "09": "ciudaddemexico", "10": "durango", "11": "guanajuato",
    "12": "guerrero", "13": "hidalgo", "14": "jalisco", "15": "mexico",
    "16": "michoacandeocampo", "17": "morelos", "18": "nayarit", "19": "nuevoleon",
    "20": "oaxaca", "21": "puebla", "22": "queretaro", "23": "quintanaroo",
    "24": "sanluispotosi", "25": "sinaloa", "26": "sonora", "27": "tabasco",
    "28": "tamaulipas", "29": "tlaxcala", "30": "veracruzignaciodelallave",
    "31": "yucatan", "32": "zacatecas",
}
