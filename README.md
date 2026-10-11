# Chhaya

Chhaya is a Python-based geospatial analytics pipeline for identifying and prioritizing urban heat and environmental vulnerability hotspots across Indian cities. The project combines city boundaries, H3 hex grids, remote sensing inputs, and local context data to build a consistent, explainable city-level priority score from multiple signals such as land surface temperature, vegetation health, population pressure, and dust exposure.

This repository is designed to support a repeatable workflow:

- define a city configuration
- build a hex-tile fixture for that city
- collect or synthesize source features
- merge outputs into a single cell-level dataset
- validate schema compliance
- publish the result locally or to S3 for downstream mapping and analysis

## Why Chhaya

Urban heat and environmental stress are not evenly distributed. High temperatures, low vegetation cover, dense settlement patterns, and particulate exposure can all compound in localized hotspots. Chhaya aims to surface those cells in a way that is transparent and operationally useful for city planning, ward-level prioritization, and public-health-oriented interventions.

The pipeline is intentionally modular:

- source modules can be swapped or extended
- each city has its own configuration
- schema validation prevents malformed output
- previous results can be merged without overwriting good data from successful modules

## Key capabilities

- H3-based hex-grid generation for city boundaries
- Sentinel-2 NDVI processing and caching
- multi-source cell feature assembly for heat and urban stress indicators
- ward-aware outputs for local aggregation
- schema-based validation for pipeline artifacts
- S3 export and city index generation
- support for demo and production modes

## Project structure

```text
chhaya/
├── config/
│   ├── base.yaml
│   ├── dev.yaml
│   ├── prod.yaml
│   └── cities/
│       ├── delhi.yaml
│       ├── jaipur.yaml
│       └── ...
├── contract/
│   ├── cell.schema.json
│   ├── pipeline_status.schema.json
│   └── mock_cells.geojson
├── scripts/
│   ├── assign_wards.py
│   ├── build_city.py
│   └── upload_fixtures.py
├── src/
│   ├── __init__.py
│   ├── bottom_pipeline.py
│   ├── export.py
│   └── sources/
│       ├── __init__.py
│       ├── base.py
│       ├── mock.py
│       ├── ndvi.py
│       ├── stubs.py
│       └── wards.py
├── utils/
│   ├── __init__.py
│   └── logger.py
├── .gitignore
├── config.py
├── LICENSE
├── README.md
└── ...
```

## Data and modeling approach

### City configuration

Each city gets a YAML configuration file under `config/cities/`. These files define:

- city name and slug
- data mode (`real` or `demo`)
- H3 resolution
- raw geodata paths
- boundary and fixture information
- Sentinel-2 and geospatial metadata such as bounding box and EPSG code

The base config in `config/base.yaml` provides shared defaults for:

- local filesystem paths
- S3 bucket settings
- fixture and artifact prefixes
- source configuration

### Grid fixture generation

The project works over H3 hexagonal cells. A city boundary is used to generate a grid fixture via `scripts/build_city.py`, producing a parquet file such as:

- `data/cities/delhi_hex_res8.parquet`

This grid becomes the foundation for all source enrichment and final cell-level outputs.

### Source processing

The pipeline assembles a cell-level table with values such as:

- `ndvi`
- `lst_c`
- `population`
- `built_frac`
- `water_prox`
- `dust_idx`
- `school_count`
- `ward`
- `ward_id`
- `priority`
- `priority_rank`

A weighted priority score is computed from heat, population, greenery deficit, and dust. The logic is defined in `src/bottom_pipeline.py` and is intended to be transparent and easy to adjust.

### Validation and output

The pipeline validates generated cell rows against the schema in `contract/cell.schema.json`. Output is written locally as parquet and GeoJSON artifacts and can also be uploaded to S3 via the exporter layer (`src/export.py`).

## Prerequisites

Chhaya is a Python project. Typical requirements include:

- Python 3.10+
- geopandas
- pandas
- numpy
- shapely
- h3
- pyYAML
- python-dotenv
- boto3
- jsonschema
- pystac-client
- stackstac
- xarray
- rioxarray
- rasterstats

The repository does not currently include a pinned `requirements.txt` or `pyproject.toml`, so package installation is typically done directly with `pip` or via a project-specific environment manager.

Example package installation:

```bash
pip install geopandas pandas numpy shapely h3 pyyaml python-dotenv boto3 jsonschema pystac-client stackstac xarray rioxarray rasterstats
```

## Configuration

Create a `.env` file in the repository root if you want environment-specific configuration or local overrides. A minimal example:

```bash
CHHAYA_ENV=dev
```

The active environment is selected by `config.py` using:

```python
ENV = os.getenv("CHHAYA_ENV", "dev")
```

This loads the corresponding configuration file in `config/` (for example, `config/dev.yaml` or `config/prod.yaml`).

## Quick start

### 1. Build a city grid fixture

```bash
python -m scripts.build_city delhi
```

You can also build all configured cities:

```bash
python -m scripts.build_city --all
```

### 2. Run the city pipeline

Single city:

```bash
python -m src.bottom_pipeline --city delhi
```

All configured cities:

```bash
python -m src.bottom_pipeline --all
```

### 3. Inspect generated outputs

Outputs are written to the configured local directories, such as:

- `data/cities/`
- `data/derived/`

Typical artifacts include:

- `delhi_cells.parquet`
- `delhi_cells.geojson`
- `delhi_status.json`
- ward-related outputs and city summaries

## Source modules

### Sentinel-2 NDVI

The `src/sources/ndvi.py` module searches for Sentinel-2 scenes in the configured date range, filters by cloud cover, composites scenes, computes NDVI, and zonal-averages it onto the H3 hex grid. It caches the processed raster for repeat runs.

### Stubs and mock data

The repository includes stubbed and mock sources in `src/sources/stubs.py` and `src/sources/mock.py` for testing and demo runs when live external data is unavailable.

### Wards

Ward-based information can be merged into the cell dataset when a wards fixture exists or when a city mapping is defined.

## Scripts

### `scripts/build_city.py`

Builds H3 cell fixtures from a city boundary file.

### `scripts/assign_wards.py`

Used to map or assign ward boundaries to cells or fixtures.

### `scripts/upload_fixtures.py`

Helps upload generated fixture data to the configured storage backend.

## Production notes

Some settings are intentionally guarded for safety:

- production uploads require confirmation in config
- when S3 upload is enabled, helper logic writes a status document and city index
- the pipeline treats a failed source as non-fatal and preserves previously good values where appropriate

This is useful for incremental or resilient production runs, especially when data quality or provider availability varies.

## Development conventions

- keep city-specific logic in `config/cities/*.yaml`
- keep shared defaults in `config/base.yaml`
- keep data contracts in `contract/`
- prefer schema validation before publishing outputs
- maintain source modules as independent units with a consistent `run()` interface

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## Contributing

Contributions are welcome. To contribute:

1. fork the repository
2. create a feature branch
3. make your changes
4. validate the relevant pipeline or script
5. submit a pull request with a clear summary

## Maintainer note

Chhaya is intended as a practical, extensible foundation for urban environmental analytics. It is suitable for experimenting with city-scale heat risk scoring, calling remote sensing sources, and building transparent municipal planning outputs for neighborhood prioritization.

