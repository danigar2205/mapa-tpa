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
import unicodedata
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

# Coordenadas verificadas por el usuario para las dos paradas de la N-II.
# Se aplican al cargar el mapa para que también sobrevivan a futuras
# reimportaciones del GTFS mientras el proveedor mantenga mal sus coordenadas.
COORDENADAS_CORREGIDAS_T05 = {
    "lugarico": (41.649452, -0.790304),
    "condado": (41.59113888888889, -0.66819444444444),
}

# Coordenadas verificadas por el usuario para dos paradas de la T11.
# Se aplican al cargar el mapa, sin alterar el GTFS original ni otras concesiones.
COORDENADAS_CORREGIDAS_T11 = {
    "c/ violante de hungria (escuela de idiomas)": (41.642076, -0.905206),
    "cutanda. marquesina c/ mayor no 2": (40.934940, -1.184232),
}

MAPA_VACIO: dict[str, Any] = {"routes": {}, "shapes": {}, "stops": {}}
LOOKUP_VACIO: dict[str, Any] = {"stops": {}, "trips": {}}
HORARIOS_VACIOS: dict[str, Any] = {
    "routes": {}, "services": {}, "exceptions": [], "stop_times": []
}


def _normalizar_nombre_parada(nombre: Any) -> str:
    """Normaliza un nombre para localizar paradas sin depender de tildes."""
    texto = unicodedata.normalize("NFKD", str(nombre or "").casefold())
    return "".join(caracter for caracter in texto if not unicodedata.combining(caracter))


def _corregir_coordenadas_t05(mapa: dict[str, Any]) -> None:
    """Corrige las coordenadas de Lugarico y El Condado en todas las líneas T05."""
    for paradas in mapa.get("stops", {}).values():
        if not isinstance(paradas, list):
            continue
        for parada in paradas:
            nombre = _normalizar_nombre_parada(parada.get("name"))
            coordenadas = None
            if "lugarico" in nombre:
                coordenadas = COORDENADAS_CORREGIDAS_T05["lugarico"]
            elif "condado" in nombre:
                coordenadas = COORDENADAS_CORREGIDAS_T05["condado"]
            if coordenadas is not None:
                parada["lat"], parada["lon"] = coordenadas


def _corregir_coordenadas_t11(mapa: dict[str, Any]) -> None:
    """Corrige dos paradas de la T11 con las coordenadas confirmadas por el usuario."""
    for paradas in mapa.get("stops", {}).values():
        if not isinstance(paradas, list):
            continue
        for parada in paradas:
            nombre = " ".join(_normalizar_nombre_parada(parada.get("name")).split())
            coordenadas = COORDENADAS_CORREGIDAS_T11.get(nombre)
            if coordenadas is not None:
                parada["lat"], parada["lon"] = coordenadas


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

        if codigo == "T05":
            _corregir_coordenadas_t05(mapa)
        elif codigo == "T11":
            _corregir_coordenadas_t11(mapa)

        # Un mapa solo se considera disponible si contiene rutas y paradas.
        tiene_mapa = bool(mapa.get("routes")) and any(
            isinstance(paradas, list) and paradas for paradas in mapa.get("stops", {}).values()
        )
        tiene_calendario = bool(horarios.get("services")) or bool(horarios.get("exceptions"))
        tiene_horarios = (
            bool(horarios.get("stop_times")) and
            bool(horarios.get("routes")) and
            tiene_calendario
        )
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
