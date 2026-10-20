# Site Selection Copilot

Agente con LLM que responde preguntas de ubicación comercial en lenguaje natural
(*"¿dónde abrirías una farmacia en Guadalajara, lejos de la competencia y en una zona de nivel socioeconómico medio-alto?"*)
consultando datos geoespaciales públicos de INEGI con SQL espacial.

**▶ Demo en vivo: [site-selection-copilot.streamlit.app](https://site-selection-copilot.streamlit.app/)**
(10 análisis reales generados por el agente, con su mapa y las herramientas que usó).

## Datos

Todo sale de fuentes públicas de INEGI, por entidad federativa:

| Fuente | Qué aporta | Tabla |
|---|---|---|
| DENUE (directorio de unidades económicas) | ~400 k negocios en Jalisco con coordenadas, actividad SCIAN y tamaño | `negocios` |
| Censo de Población y Vivienda 2020 (AGEB urbana) | Población, escolaridad y equipamiento de la vivienda | `agebs` |
| Marco Geoestadístico 2020 | Polígonos de AGEB urbanas y municipios | `agebs`, `municipios` |

Todas las geometrías quedan en **EPSG:4326**, con índices R-tree, en una base [DuckDB](https://duckdb.org) con la extensión `spatial`.

### Índice socioeconómico (aproximado)

`agebs.nse_score` es el promedio de los z-scores de cuatro indicadores: escolaridad promedio y % de viviendas con auto, internet y computadora.
`nse_quintil_municipal` (1 a 5) ordena las AGEB dentro de su municipio y `nse_quintil_estatal` las ordena dentro del estado.
Es un indicador **relativo**, no el NSE de AMAI. Las AGEB sin población o con datos protegidos por confidencialidad quedan en `NULL`.

## Cómo funciona

```
pregunta ──► Claude (tool calling) ──► herramientas ──► DuckDB spatial (INEGI) / OSMnx (OpenStreetMap)
                   ▲                        │
                   └──── resultados JSON ◄──┘
```

| Herramienta | Qué hace |
|---|---|
| `buscar_actividad` | Traduce un giro ("farmacia", "gimnasio") a códigos SCIAN |
| `buscar_municipio` | Resuelve nombres de municipios (tolera acentos) |
| `rankear_zonas` | Ordena AGEB candidatas por habitantes por competidor, con filtro de NSE y de actividad comercial, y sugiere el punto con más comercio |
| `perfil_zona` | Población, viviendas, escolaridad, % con auto, NSE y mezcla de negocios en un radio |
| `competencia_cercana` | Competidores en un radio y los más cercanos, con distancia |
| `isocrona` | Área alcanzable en N minutos a pie o en auto, con su perfil y competencia |
| `consulta_sql` | SQL de solo lectura para preguntas que las demás no cubren (sin acceso a archivos ni red) |

El agente no depende del proveedor: las mismas herramientas y el mismo prompt funcionan con
**OpenAI** (Responses API con function calling, `gpt-5-mini` por defecto) o con **Anthropic**
(Tool Runner del SDK, `claude-opus-5-5`). Se elige con `LLM_PROVIDER` en `.env`.
Detalles de método:
- **Ranking:** descarta zonas con menos de 25 comercios al por menor a 500 m (el ~10 % más residencial de Guadalajara y Zapopan).
  Sin ese filtro, las "mejores" zonas eran fraccionamientos cerrados sin locales: cero competencia porque no hay dónde abrir.
- **Isócronas:** la red vial de la ZM Guadalajara se precarga una vez, así que no dependen del servidor público de Overpass.
  En auto se usan las velocidades de OSM al 60 % para aproximar el tráfico; a pie, la misma red sin sentido único ni autopistas, a 4.8 km/h.

## Uso

Requiere [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m site_copilot.data.download        # Jalisco por defecto; acepta claves: 14 09 19
uv run python -m site_copilot.data.build_db        # crea data/site_copilot.duckdb
uv run python -m site_copilot.tools.isochrones     # precarga la red vial de auto de la ZM Guadalajara
uv run pytest                                      # pruebas (no requieren API key)

cp .env.example .env                               # y pon tu OPENAI_API_KEY (o ANTHROPIC_API_KEY)
uv run site-copilot "¿Dónde abrirías una farmacia en Guadalajara o Zapopan, en zona de NSE medio-alto y con poca competencia?"
uv run site-copilot                                # modo conversación
uv run streamlit run src/site_copilot/app.py       # app con chat y mapa
```

**Modos de la app:**
- **Público** (`SITE_COPILOT_PUBLICO=1`): solo la galería de 10 análisis grabados en `demos/`, sin chat ni campo de key.
  Visitarla no cuesta nada y no necesita la base de datos.
- **Local con key en `.env`:** galería + preguntas en vivo.
- **Local sin key:** galería + campo opcional para usar una key propia.

Para grabar un ejemplo nuevo:
`uv run python -m site_copilot.demos --titulo "Farmacia en Zapopan" --categoria Ranking "tu pregunta"`

### Despliegue en Streamlit Community Cloud

1. En [share.streamlit.io](https://share.streamlit.io), *Create app* → este repositorio, rama `main`,
   archivo principal `src/site_copilot/app.py`, Python 3.12.
2. En *Advanced settings → Secrets*: `SITE_COPILOT_PUBLICO = "1"`.

`requirements.txt` se genera con `uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt`.

El diseño sigue un sistema "Data-Dense Dashboard" (azul marino + acento ámbar, Fira Sans / Fira Code) definido en
`.streamlit/config.toml`, con contraste AA, foco visible y respeto a `prefers-reduced-motion`.

> En macOS con el Escritorio en iCloud, `.venv` y `data` son enlaces a carpetas `*.nosync` para que iCloud no los sincronice
> (iCloud duplica y oculta archivos del entorno virtual y los datos pesan ~1 GB):
> `mkdir .venv.nosync data.nosync && ln -s .venv.nosync .venv && ln -s data.nosync data`

## Roadmap

- [x] Pipeline de datos: DENUE + Censo 2020 + Marco Geoestadístico → DuckDB spatial
- [x] Herramientas del agente: competencia, perfil de zona, ranking de zonas, isócronas (OSMnx), SQL de solo lectura
- [x] Agente con tool calling (Claude API) y CLI
- [ ] Set de evaluación (~30 preguntas con respuesta esperada)
- [x] UI con chat y mapa interactivo (Streamlit + Folium) con modo demo
- [x] Galería pública de 10 análisis (sin costo por visita)
- [x] Despliegue público en Streamlit Community Cloud

## Licencia de datos

Información de INEGI, de uso libre con atribución. Fuente: INEGI, DENUE; Censo de Población y Vivienda 2020; Marco Geoestadístico 2020.
