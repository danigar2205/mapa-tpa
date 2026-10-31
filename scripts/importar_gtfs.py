#!/usr/bin/env python3
"""Convierte un ZIP GTFS estático al formato que consume Mapa TPA.

Uso:
  python scripts/importar_gtfs.py T01 descarga_gtfs.zip

Genera en data/:
  gtfs_t01_map.json.gz.b64
  gtfs_t01_lookup.json.gz.b64
  gtfs_t01_timetable.json.gz.b64
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any


def localizar_fichero(zip_file: zipfile.ZipFile, nombre: str) -> str | None:
    """Encuentra un fichero GTFS incluso si el ZIP contiene una carpeta raíz."""
    coincidencias = [
        item for item in zip_file.namelist()
        if not item.endswith("/") and item.rsplit("/", 1)[-1].lower() == nombre.lower()
    ]
    if not coincidencias:
        return None
    # Priorizar ficheros en la raíz y después el camino más corto.
    coincidencias.sort(key=lambda item: (item.count("/"), len(item)))
    return coincidencias[0]


def leer_csv(zip_file: zipfile.ZipFile, nombre: str, obligatorio: bool = False) -> list[dict[str, str]]:
    ruta = localizar_fichero(zip_file, nombre)
    if not ruta:
        if obligatorio:
            raise ValueError(f"El ZIP GTFS no contiene {nombre}.")
        return []
    with zip_file.open(ruta) as fichero:
        texto = io.TextIOWrapper(fichero, encoding="utf-8-sig", newline="")
        return [
            {str(k or "").strip(): str(v or "").strip() for k, v in fila.items()}
            for fila in csv.DictReader(texto)
        ]


def numero(valor: str, defecto: float | None = None) -> float | None:
    try:
        resultado = float(valor)
        return resultado if resultado == resultado and abs(resultado) != float("inf") else defecto
    except (TypeError, ValueError):
        return defecto


def color_gtfs(valor: str) -> str:
    color = re.sub(r"[^0-9A-Fa-f]", "", valor or "")
    return color.upper() if len(color) == 6 else "2563EB"


def escribir_json_comprimido(ruta: Path, datos: dict[str, Any]) -> None:
    contenido = json.dumps(datos, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    comprimido = gzip.compress(contenido, mtime=0)
    ruta.write_text(base64.b64encode(comprimido).decode("ascii"), encoding="ascii")


def convertir_gtfs(codigo: str, zip_path: Path, data_dir: Path) -> dict[str, int]:
    codigo = codigo.upper()
    if not re.fullmatch(r"T[0-9]{2}", codigo):
        raise ValueError("El código de concesión debe tener formato T01, T02, ...")

    with zipfile.ZipFile(zip_path) as archivo:
        rutas_csv = leer_csv(archivo, "routes.txt", obligatorio=True)
        paradas_csv = leer_csv(archivo, "stops.txt", obligatorio=True)
        viajes_csv = leer_csv(archivo, "trips.txt", obligatorio=True)
        tiempos_csv = leer_csv(archivo, "stop_times.txt", obligatorio=True)
        calendario_csv = leer_csv(archivo, "calendar.txt")
        excepciones_csv = leer_csv(archivo, "calendar_dates.txt")

    rutas: dict[str, dict[str, str]] = {}
    for fila in rutas_csv:
        route_id = fila.get("route_id")
        if not route_id:
            continue
        rutas[route_id] = {
            "short_name": fila.get("route_short_name") or f"{codigo}-{route_id}",
            "long_name": fila.get("route_long_name") or fila.get("route_short_name") or codigo,
            "color": color_gtfs(fila.get("route_color", "")),
        }

    paradas: dict[str, dict[str, Any]] = {}
    for fila in paradas_csv:
        stop_id = fila.get("stop_id")
        lat, lon = numero(fila.get("stop_lat", "")), numero(fila.get("stop_lon", ""))
        if stop_id and lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            paradas[stop_id] = {
                "id": stop_id,
                "name": fila.get("stop_name") or stop_id,
                "lat": lat,
                "lon": lon,
            }

    viajes: dict[str, dict[str, str]] = {}
    for fila in viajes_csv:
        trip_id = fila.get("trip_id")
        if trip_id:
            viajes[trip_id] = fila

    paradas_por_linea: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    lookup_trips: dict[str, dict[str, str]] = defaultdict(dict)
    stop_times: list[dict[str, str]] = []
    for fila in tiempos_csv:
        trip_id = fila.get("trip_id")
        stop_id = fila.get("stop_id")
        viaje = viajes.get(trip_id or "")
        if not viaje or not stop_id:
            continue

        route_id = viaje.get("route_id", "")
        ruta = rutas.get(route_id)
        if not ruta:
            continue

        if stop_id in paradas:
            paradas_por_linea[ruta["short_name"]][stop_id] = paradas[stop_id]

        secuencia = fila.get("stop_sequence", "")
        if secuencia:
            lookup_trips[trip_id][secuencia] = stop_id

        salida = fila.get("departure_time") or fila.get("arrival_time") or ""
        llegada = fila.get("arrival_time") or salida
        stop_times.append({
            "stop_id": stop_id,
            "route_id": route_id,
            "service_id": viaje.get("service_id", ""),
            "trip_id": trip_id,
            "direction_id": viaje.get("direction_id", ""),
            "headsign": viaje.get("trip_headsign") or ruta["long_name"],
            "arrival_time": llegada,
            "departure_time": salida,
        })

    mapa = {
        "routes": rutas,
        "shapes": {},
        "stops": {
            nombre: sorted(lista.values(), key=lambda parada: parada["name"].casefold())
            for nombre, lista in paradas_por_linea.items()
        },
    }
    lookup = {
        "stops": {stop_id: parada["name"] for stop_id, parada in paradas.items()},
        "trips": dict(lookup_trips),
    }
    horarios = {
        "routes": rutas,
        "services": {
            fila["service_id"]: {
                clave: fila.get(clave, "0")
                for clave in (
                    "monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday", "start_date", "end_date"
                )
            }
            for fila in calendario_csv if fila.get("service_id")
        },
        "exceptions": [
            {
                "service_id": fila.get("service_id", ""),
                "date": fila.get("date", ""),
                "exception_type": fila.get("exception_type", ""),
            }
            for fila in excepciones_csv
        ],
        "stop_times": stop_times,
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    prefijo = f"gtfs_{codigo.lower()}"
    escribir_json_comprimido(data_dir / f"{prefijo}_map.json.gz.b64", mapa)
    escribir_json_comprimido(data_dir / f"{prefijo}_lookup.json.gz.b64", lookup)
    escribir_json_comprimido(data_dir / f"{prefijo}_timetable.json.gz.b64", horarios)
    return {
        "rutas": len(rutas),
        "paradas": len(paradas),
        "viajes": len(viajes),
        "paradas_en_horarios": len(stop_times),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("concesion", help="Código, por ejemplo T01 o T13")
    parser.add_argument("zip_gtfs", type=Path, help="ZIP GTFS estático descargado")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    args = parser.parse_args()

    if not args.zip_gtfs.is_file():
        parser.error(f"No existe el ZIP indicado: {args.zip_gtfs}")
    try:
        resumen = convertir_gtfs(args.concesion, args.zip_gtfs, args.data_dir)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"Error al importar GTFS: {exc}", file=sys.stderr)
        return 1

    print(f"GTFS {args.concesion.upper()} importado correctamente:")
    for clave, valor in resumen.items():
        print(f"  {clave}: {valor}")
    print(f"Ficheros generados en: {args.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
