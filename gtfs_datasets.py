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

# Grupos confirmados por el usuario para mostrar una sola parada por estación.
# Se conservan todos los stop_id originales para no perder horarios GTFS.
GRUPOS_PARADAS_FUSIONADAS = (
    {
        "clave": "huesca-estacion-autobuses",
        "nombre": "Huesca. Estación de Autobuses",
        "ids": (
            "ca2b2e6d-0e68-42bc-b3c8-0e85213c2dab",
            "daaf50c8-4c5c-4667-955d-9f0fe0e26ade",
            "2954b4ec-9b28-4e0b-8150-a8323af99a20",
        ),
        "lat": 42.1337222,
        "lon": -0.4096834,
    },
    {
        "clave": "zaragoza-delicias",
        "nombre": "Zaragoza. Estación Intermodal",
        "ids": (
            "870bbe81-0cd1-43e2-bb25-574ae29fde6c",
            "8ddaeb32-d3d4-4d85-85b9-b553093d0507",
            "a27f8e1d-8040-499c-bbdf-3d2d4d1ebdcf",
            "c419a8d4-6637-4deb-8f90-001d2a66f734",
            "69aea6a2-54e7-4e0d-85ee-504200a6de44",
            "299433fa-77ca-4396-b354-190c54442375",
            "33c439d3-2da6-4575-b5b1-4a9df9e36d8c",
            "0b291231-f435-4bf6-8cf5-1ba10cab0e78",
            "a8eb32fe-9a1f-4e51-8662-e96f944853bd",
            "d319c8cc-f5d5-4247-9642-7e9b01ddc6f5",
        ),
        "lat": 41.659593,
        "lon": -0.910664,
    },
    {
        "clave": "ejea-estacion-autobuses",
        "nombre": "Ejea de los Caballeros. Estación de Autobuses",
        "ids": (
            "5ea732ef-fe57-4895-b1cf-effed95385e6",
            "515d948f-67a6-4fcd-898a-82a6eb23da85",
        ),
        "lat": 42.125191,
        "lon": -1.143281,
    },
)

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


def _fusionar_paradas_equivalentes(mapa: dict[str, Any]) -> None:
    """Agrupa marcadores de una misma estación y conserva sus stop_id originales."""
    paradas_por_linea = mapa.get("stops", {})
    ids_presentes = {
        str(parada.get("id") or parada.get("stop_id") or "")
        for paradas in paradas_por_linea.values()
        if isinstance(paradas, list)
        for parada in paradas
    }

    grupos_activos = {}
    grupo_por_id = {}
    for grupo in GRUPOS_PARADAS_FUSIONADAS:
        ids_grupo = sorted(ids_presentes.intersection(grupo["ids"]))
        if not ids_grupo:
            continue
        grupos_activos[grupo["clave"]] = {
            **grupo,
            "ids_activos": ids_grupo,
            "id_canonico": ids_grupo[0],
        }
        for stop_id in ids_grupo:
            grupo_por_id[stop_id] = grupo["clave"]

    for nombre_linea, paradas in list(paradas_por_linea.items()):
        if not isinstance(paradas, list):
            continue
        resultado = []
        grupos_emitidos = set()
        for parada in paradas:
            stop_id = str(parada.get("id") or parada.get("stop_id") or "")
            clave_grupo = grupo_por_id.get(stop_id)
            if not clave_grupo:
                resultado.append(parada)
                continue
            if clave_grupo in grupos_emitidos:
                continue

            grupo = grupos_activos[clave_grupo]
            fusionada = dict(parada)
            fusionada.update({
                "id": grupo["id_canonico"],
                "name": grupo["nombre"],
                "lat": grupo["lat"],
                "lon": grupo["lon"],
                "stop_ids": list(grupo["ids_activos"]),
            })
            resultado.append(fusionada)
            grupos_emitidos.add(clave_grupo)

        paradas_por_linea[nombre_linea] = resultado


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

        _fusionar_paradas_equivalentes(mapa)

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
