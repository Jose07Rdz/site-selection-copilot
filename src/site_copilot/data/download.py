"""Descarga y descomprime las fuentes públicas de INEGI por entidad.

Uso:
    uv run python -m site_copilot.data.download            # Jalisco
    uv run python -m site_copilot.data.download 14 09 19   # Jalisco, CDMX, Nuevo León
"""

import sys
import zipfile
from pathlib import Path

import httpx
from tqdm import tqdm

from site_copilot.config import DEFAULT_STATES, RAW_DIR, SOURCES, STATE_SLUGS, USER_AGENT


def download(url: str, dest: Path) -> Path:
    """Descarga `url` a `dest` si no existe ya. Escribe a un .part y renombra al final."""
    if dest.exists():
        print(f"  ya existe: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": USER_AGENT}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        if "zip" not in r.headers.get("content-type", ""):
            raise RuntimeError(f"Respuesta inesperada ({r.headers.get('content-type')}) de {url}")
        total = int(r.headers.get("content-length", 0)) or None
        with open(tmp, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=dest.name) as bar:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                bar.update(len(chunk))
    tmp.rename(dest)
    return dest


def extract(zip_path: Path, out_dir: Path) -> Path:
    if out_dir.exists() and any(out_dir.iterdir()):
        return out_dir
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out_dir)
    return out_dir


def fetch_state(cve: str) -> None:
    slug = STATE_SLUGS[cve]
    print(f"Entidad {cve} ({slug})")
    for name, template in SOURCES.items():
        url = template.format(cve=cve, slug=slug)
        zip_path = download(url, RAW_DIR / "zips" / f"{name}_{cve}.zip")
        extract(zip_path, RAW_DIR / name / cve)


def main(argv: list[str]) -> None:
    states = argv or DEFAULT_STATES
    unknown = [s for s in states if s not in STATE_SLUGS]
    if unknown:
        sys.exit(f"Claves de entidad no válidas: {unknown} (usa 01-32)")
    for cve in states:
        fetch_state(cve)


if __name__ == "__main__":
    main(sys.argv[1:])
