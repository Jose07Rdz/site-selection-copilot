# Site Selection Copilot

Agente con LLM que responde preguntas de ubicación comercial en lenguaje natural
(*"¿dónde abrirías una farmacia en Guadalajara, lejos de la competencia y en una zona de nivel socioeconómico medio-alto?"*)
consultando datos geoespaciales públicos de INEGI con SQL espacial.

> 🚧 En construcción. Ya funcionan el pipeline de datos, las herramientas y el agente por línea de comandos.

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
| `rankear_zonas` | Ordena AGEB candidatas por habitantes por competidor en un radio, con filtro de NSE |
| `perfil_zona` | Población, viviendas, escolaridad, % con auto, NSE y mezcla de negocios en un radio |
| `competencia_cercana` | Competidores en un radio y los más cercanos, con distancia |
| `isocrona` | Área alcanzable en N minutos a pie o en auto, con su perfil y competencia |
| `consulta_sql` | SQL de solo lectura para preguntas que las demás no cubren (sin acceso a archivos ni red) |

El agente no depende del proveedor: las mismas herramientas y el mismo prompt funcionan con
**OpenAI** (Responses API con function calling, `gpt-5-mini` por defecto) o con **Anthropic**
(Tool Runner del SDK, `claude-opus-5-5`). Se elige con `LLM_PROVIDER` en `.env`.
Las isócronas en auto usan las velocidades de OpenStreetMap al 60 % para aproximar el tráfico urbano.

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
```

> En macOS con el Escritorio en iCloud, `.venv` y `data` son enlaces a carpetas `*.nosync` para que iCloud no los sincronice
> (iCloud duplica y oculta archivos del entorno virtual y los datos pesan ~1 GB):
> `mkdir .venv.nosync data.nosync && ln -s .venv.nosync .venv && ln -s data.nosync data`

## Roadmap

- [x] Pipeline de datos: DENUE + Censo 2020 + Marco Geoestadístico → DuckDB spatial
- [x] Herramientas del agente: competencia, perfil de zona, ranking de zonas, isócronas (OSMnx), SQL de solo lectura
- [x] Agente con tool calling (Claude API) y CLI
- [ ] Set de evaluación (~30 preguntas con respuesta esperada)
- [ ] UI con mapa interactivo (Streamlit) y despliegue

## Licencia de datos

Información de INEGI, de uso libre con atribución. Fuente: INEGI, DENUE; Censo de Población y Vivienda 2020; Marco Geoestadístico 2020.
