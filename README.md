# Site Selection Copilot

Agente con LLM que responde preguntas de ubicación comercial en lenguaje natural
(*"¿dónde abrirías una farmacia en Guadalajara, lejos de la competencia y en una zona de nivel socioeconómico medio-alto?"*)
consultando datos geoespaciales públicos de INEGI con SQL espacial.

> 🚧 En construcción. Fase actual: **pipeline de datos**.

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

## Uso

Requiere [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m site_copilot.data.download        # Jalisco por defecto; acepta claves: 14 09 19
uv run python -m site_copilot.data.build_db        # crea data/site_copilot.duckdb
uv run pytest                                      # chequeos de integridad de la base
```

## Roadmap

- [x] Pipeline de datos: DENUE + Censo 2020 + Marco Geoestadístico → DuckDB spatial
- [ ] Herramientas del agente: búsqueda de competencia, perfil de zona, isócronas (OSMnx)
- [ ] Agente con tool calling (Claude API)
- [ ] Set de evaluación (~30 preguntas con respuesta esperada)
- [ ] UI con mapa interactivo (Streamlit) y despliegue

## Licencia de datos

Información de INEGI, de uso libre con atribución. Fuente: INEGI, DENUE; Censo de Población y Vivienda 2020; Marco Geoestadístico 2020.
