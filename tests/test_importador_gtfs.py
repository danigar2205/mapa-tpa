import tempfile
import unittest
import zipfile
from pathlib import Path

from gtfs_datasets import cargar_datasets_gtfs
from scripts.importar_gtfs import convertir_gtfs


GTFS_FILES = {
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_color\n"
        "ruta-1,agency,T01-01,Ejemplo - Zaragoza,1255CC\n"
    ),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "parada-1,Parada de prueba,41.6500,-0.8800\n"
        "parada-2,Destino de prueba,41.6600,-0.8900\n"
    ),
    "trips.txt": (
        "route_id,service_id,trip_id,trip_headsign,direction_id\n"
        "ruta-1,servicio-1,viaje-1,Zaragoza,0\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "viaje-1,08:00:00,08:00:00,parada-1,1\n"
        "viaje-1,08:30:00,08:30:00,parada-2,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "servicio-1,1,1,1,1,1,0,0,20260101,20271231\n"
    ),
    "calendar_dates.txt": (
        "service_id,date,exception_type\n"
        "servicio-1,20261225,2\n"
    ),
}


class ImportadorGTFSTests(unittest.TestCase):
    def test_importa_zip_y_deja_datos_listos_para_la_aplicacion(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            zip_path = raiz / "gtfs_t01.zip"
            data_dir = raiz / "data"

            with zipfile.ZipFile(zip_path, "w") as archivo:
                for nombre, contenido in GTFS_FILES.items():
                    archivo.writestr("feed/" + nombre, contenido)

            resumen = convertir_gtfs("T01", zip_path, data_dir)
            datasets = cargar_datasets_gtfs(data_dir)
            t01 = datasets["T01"]

            self.assertEqual(resumen["rutas"], 1)
            self.assertTrue(t01["has_map"])
            self.assertTrue(t01["has_schedules"])
            self.assertFalse(t01["has_realtime"])
            self.assertEqual(t01["map"]["routes"]["ruta-1"]["short_name"], "T01-01")
            self.assertEqual(
                {parada["id"] for parada in t01["map"]["stops"]["T01-01"]},
                {"parada-1", "parada-2"}
            )
            self.assertEqual(t01["lookup"]["trips"]["viaje-1"]["2"], "parada-2")
            self.assertEqual(len(t01["schedule"]["stop_times"]), 2)
            self.assertEqual(t01["schedule"]["exceptions"][0]["exception_type"], "2")

    def test_rechaza_codigo_de_concesion_no_valido(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            zip_path = raiz / "gtfs.zip"
            with zipfile.ZipFile(zip_path, "w") as archivo:
                for nombre, contenido in GTFS_FILES.items():
                    archivo.writestr(nombre, contenido)
            with self.assertRaises(ValueError):
                convertir_gtfs("T5", zip_path, raiz / "data")


if __name__ == "__main__":
    unittest.main()
