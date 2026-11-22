#!/usr/bin/env python3
"""Importa de una vez todas las concesiones con rutas de un ZIP GTFS multiagencia.

Uso:
  python scripts/importar_gtfs_lote.py descarga_gtfs.zip

Las agencias presentes en agency.txt pero sin rutas se omiten expresamente para
no borrar ni sustituir los datos previos de esas concesiones.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

from scripts.importar_gtfs import convertir_gtfs, leer_csv


def descubrir_concesiones(zip_path: Path) -> tuple[list[str], list[str]]:
    """Devuelve (agencias con rutas, agencias declaradas pero sin rutas)."""
    with zipfile.ZipFile(zip_path) as archivo:
        rutas = leer_csv(archivo, "routes.txt", obligatorio=True)
        agencias = leer_csv(archivo, "agency.txt")

    agencias_declaradas = sorted({
        fila.get("agency_id", "").upper()
        for fila in agencias
        if fila.get("agency_id")
    })
    agencias_con_rutas = sorted({
        fila.get("agency_id", "").upper()
        for fila in rutas
        if fila.get("agency_id")
    })

    if not agencias_con_rutas:
        if len(agencias_declaradas) == 1:
            agencias_con_rutas = agencias_declaradas
        else:
            raise ValueError(
                "No se pueden identificar las concesiones: routes.txt no "
                "incluye agency_id y agency.txt no declara una única agencia."
            )

    sin_rutas = sorted(set(agencias_declaradas) - set(agencias_con_rutas))
    return agencias_con_rutas, sin_rutas


def importar_gtfs_lote(zip_path: Path, data_dir: Path) -> dict:
    """Importa las agencias que tienen rutas y conserva las que vienen vacías."""
    if not zip_path.is_file():
        raise FileNotFoundError(f"No existe el ZIP GTFS: {zip_path}")

    agencias, agencias_sin_rutas = descubrir_concesiones(zip_path)
    resumenes = {}
    for codigo in agencias:
        resumenes[codigo] = convertir_gtfs(codigo, zip_path, data_dir)

    return {
        "importadas": resumenes,
        "sin_rutas": agencias_sin_rutas,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_gtfs", type=Path, help="ZIP GTFS multiagencia")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data",
    )
    args = parser.parse_args()

    try:
        resultado = importar_gtfs_lote(args.zip_gtfs, args.data_dir)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"Error al importar GTFS: {exc}", file=sys.stderr)
        return 1

    print("Concesiones importadas:")
    for codigo, resumen in resultado["importadas"].items():
        print(
            f"  {codigo}: {resumen['rutas']} rutas, "
            f"{resumen['paradas']} paradas, "
            f"{resumen['paradas_en_horarios']} pasos horarios"
        )
    if resultado["sin_rutas"]:
        print(
            "Sin rutas en el ZIP (se conservan los datos existentes): "
            + ", ".join(resultado["sin_rutas"])
        )
    print(f"Ficheros generados en: {args.data_dir}")
    print("La configuración de GTFS-RT no se modifica con este importador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
