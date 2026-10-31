"""Carga y catálogo de datos GTFS por concesión de transporte.

Convención de ficheros por concesión:
  gtfs_t05_map.json.gz.b64
  gtfs_t05_lookup.json.gz.b64
  gtfs_t05_timetable.json.gz.b64

Los mismos nombres se usan para las demás concesiones cambiando el código.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

CATALOGO_CONCESIONES: dict[str, dict[str, str]] = {
    "T01": {"descripcion": "Jacetania y Alto Gállego - Zaragoza"},
    "T02": {"descripcion": "Sobrarbe y Ribagorza - Barbastro"},
    "T03": {"descripcion": "Fraga - Huesca por Monzón"},
    "T04": {"descripcion": "Monegros - Huesca y Zaragoza"},
    "T05": {"descripcion": "Fraga / Castejón - Zaragoza"},
    "T06": {"descripcion": "Bajo Aragón - Zaragoza"},
    "T07": {"descripcion": "Bajo Aragón y Cuencas Mineras - Teruel"},
    "T08": {"descripcion": "Maestrazgo - Teruel"},
    "T09": {"descripcion": "Gúdar-Javalambre - Teruel"},
    "T10": {"descripcion": "Serranía de Albarracín - Teruel"},
    "T11": {"descripcion": "Teruel - Zaragoza por Calamocha"},
    "T12": {"descripcion": "Campo de Belchite - Zaragoza"},
    "T13": {"descripcion": "Calatayud y Aranda - Zaragoza"},
    "T14": {"descripcion": "Moncayo y Campo de Borja - Zaragoza"},
    "T15": {"descripcion": "Cinco Villas - Zaragoza"},
}

# El piloto de posiciones y actualizaciones en tiempo real confirmado actualmente.
# No se debe inferir disponibilidad RT de la mera existencia de un feed GTFS.
CONCESIONES_CON_TIEMPO_REAL = frozenset({"T05"})

MAPA_VACIO: dict[str, Any] = {"routes": {}, "shapes": {}, "stops": {}}
LOOKUP_VACIO: dict[str, Any] = {"stops": {}, "trips": {}}
HORARIOS_VACIOS: dict[str, Any] = {
    "routes": {}, "services": {}, "exceptions": [], "stop_times": []
}


def _leer_json_gzip_b64(ruta: Path, valor_vacio: dict[str, Any]) -> dict[str, Any]:
    """Lee un JSON comprimido con gzip y codificado en Base64."""
    if not ruta.is_file():
        return dict(valor_vacio)
    try:
        contenido = base64.b64decode(ruta.read_text(encoding="ascii"), validate=True)
        datos = json.loads(gzip.decompress(contenido).decode("utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("El documento JSON debe contener un objeto.")
        return datos
    except (OSError, UnicodeError, ValueError, gzip.BadGzipFile, EOFError) as exc:
        LOGGER.exception("No se pudo cargar el fichero GTFS %s: %s", ruta, exc)
        return dict(valor_vacio)


def cargar_datasets_gtfs(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Carga los tres ficheros normalizados de cada concesión."""
    directorio = Path(data_dir)
    datasets: dict[str, dict[str, Any]] = {}
    for codigo in CATALOGO_CONCESIONES:
        prefijo = directorio / f"gtfs_{codigo.lower()}"
        mapa = _leer_json_gzip_b64(
            Path(f"{prefijo}_map.json.gz.b64"), MAPA_VACIO
        )
        lookup = _leer_json_gzip_b64(
            Path(f"{prefijo}_lookup.json.gz.b64"), LOOKUP_VACIO
        )
        horarios = _leer_json_gzip_b64(
            Path(f"{prefijo}_timetable.json.gz.b64"), HORARIOS_VACIOS
        )

        # Un mapa solo se considera disponible si contiene rutas y paradas.
        tiene_mapa = bool(mapa.get("routes")) and any(
            isinstance(paradas, list) and paradas for paradas in mapa.get("stops", {}).values()
        )
        tiene_horarios = bool(horarios.get("stop_times")) and bool(horarios.get("routes"))
        datasets[codigo] = {
            "map": mapa,
            "lookup": lookup,
            "schedule": horarios,
            "has_map": tiene_mapa,
            "has_schedules": tiene_horarios,
            "has_realtime": codigo in CONCESIONES_CON_TIEMPO_REAL and tiene_mapa,
        }
    return datasets


def construir_catalogo_publico(
    datasets: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Devuelve al navegador los datos descriptivos y las capacidades reales."""
    catalogo = []
    for codigo, metadata in CATALOGO_CONCESIONES.items():
        dataset = datasets.get(codigo, {})
        funciones = {
            "paradas": bool(dataset.get("has_map")),
            "horarios": bool(dataset.get("has_schedules")),
            "tiempo_real": bool(dataset.get("has_realtime")),
        }
        disponible = funciones["paradas"]
        if disponible:
            estado = (
                "Paradas · horarios · seguimiento en tiempo real"
                if funciones["tiempo_real"] and funciones["horarios"]
                else "Paradas · horarios oficiales"
                if funciones["horarios"]
                else "Paradas disponibles"
            )
        else:
            estado = "Pendiente de incorporar datos GTFS"
        catalogo.append({
            "codigo": codigo,
            "descripcion": metadata["descripcion"],
            "disponible": disponible,
            "funciones": funciones,
            "estado": estado,
        })
    return catalogo
