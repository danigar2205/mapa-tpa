import tempfile
import unittest
import zipfile
from pathlib import Path

from gtfs_datasets import cargar_datasets_gtfs
from scripts.importar_gtfs import convertir_gtfs


GTFS_FILES = {
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_color\n"
        "ruta-1,T01,T01-01,Ejemplo - Zaragoza,1255CC\n"
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


def escribir_zip(ruta, contenidos):
    with zipfile.ZipFile(ruta, "w") as archivo:
        for nombre, contenido in contenidos.items():
            archivo.writestr("feed/" + nombre, contenido)


class ImportadorGTFSTests(unittest.TestCase):
    def test_importa_zip_y_deja_datos_listos_para_la_aplicacion(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            zip_path = raiz / "gtfs_t01.zip"
            data_dir = raiz / "data"
            escribir_zip(zip_path, GTFS_FILES)

            resumen = convertir_gtfs("T01", zip_path, data_dir)
            t01 = cargar_datasets_gtfs(data_dir)["T01"]

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

    def test_separa_concesiones_de_un_gtfs_multiactor_por_agency_id(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            zip_path = raiz / "gtfs_multiagencia.zip"
            data_dir = raiz / "data"
            multi = dict(GTFS_FILES)
            multi["routes.txt"] += "ruta-2,T02,T02-01,Ejemplo T02,0088CC\n"
            multi["stops.txt"] += "parada-3,Parada T02,41.6700,-0.9000\n"
            multi["trips.txt"] += "ruta-2,servicio-2,viaje-2,Barbastro,1\n"
            multi["stop_times.txt"] += "viaje-2,09:00:00,09:00:00,parada-3,1\n"
            multi["calendar.txt"] += (
                "servicio-2,0,0,0,0,0,1,1,20260101,20271231\n"
            )
            multi["calendar_dates.txt"] += "servicio-2,20261226,1\n"
            escribir_zip(zip_path, multi)

            convertir_gtfs("T01", zip_path, data_dir)
            convertir_gtfs("T02", zip_path, data_dir)
            datasets = cargar_datasets_gtfs(data_dir)
            t01 = datasets["T01"]
            t02 = datasets["T02"]

            self.assertEqual(set(t01["map"]["routes"]), {"ruta-1"})
            self.assertEqual(set(t02["map"]["routes"]), {"ruta-2"})
            self.assertEqual(
                {p["id"] for lista in t01["map"]["stops"].values() for p in lista},
                {"parada-1", "parada-2"}
            )
            self.assertEqual(
                {p["id"] for lista in t02["map"]["stops"].values() for p in lista},
                {"parada-3"}
            )
            self.assertEqual({x["service_id"] for x in t01["schedule"]["stop_times"]}, {"servicio-1"})
            self.assertEqual({x["service_id"] for x in t02["schedule"]["stop_times"]}, {"servicio-2"})
            self.assertEqual(set(t02["schedule"]["services"]), {"servicio-2"})
            self.assertEqual(
                {x["service_id"] for x in t02["schedule"]["exceptions"]},
                {"servicio-2"}
            )

    def test_rechaza_agencia_que_no_esta_en_el_gtfs(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            zip_path = raiz / "gtfs.zip"
            escribir_zip(zip_path, GTFS_FILES)
            with self.assertRaisesRegex(ValueError, "no contiene rutas para T02"):
                convertir_gtfs("T02", zip_path, raiz / "data")

    def test_corrige_las_dos_paradas_t05_en_todas_las_lineas(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            zip_path = raiz / "gtfs_t05.zip"
            data_dir = raiz / "data"
            archivos_gtfs = dict(GTFS_FILES)
            archivos_gtfs["routes.txt"] = (
                archivos_gtfs["routes.txt"]
                .replace(",T01,", ",T05,")
                .replace("T01-01", "T05-01")
            )
            archivos_gtfs["stops.txt"] = (
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "lugarico,Lugarico de Cerdán,40.0000,-1.0000\n"
                "condado,Condado (El),40.1000,-1.1000\n"
            )
            archivos_gtfs["stop_times.txt"] = (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "viaje-1,08:00:00,08:00:00,lugarico,1\n"
                "viaje-1,08:30:00,08:30:00,condado,2\n"
            )
            escribir_zip(zip_path, archivos_gtfs)

            convertir_gtfs("T05", zip_path, data_dir)
            t05 = cargar_datasets_gtfs(data_dir)["T05"]
            paradas = {
                parada["id"]: parada
                for lista in t05["map"]["stops"].values()
                for parada in lista
            }

            self.assertEqual((paradas["lugarico"]["lat"], paradas["lugarico"]["lon"]),
                             (41.649452, -0.790304))
            self.assertAlmostEqual(paradas["condado"]["lat"], 41.59113888888889)
            self.assertAlmostEqual(paradas["condado"]["lon"], -0.66819444444444)

    def test_rechaza_codigo_de_concesion_no_valido(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            zip_path = raiz / "gtfs.zip"
            escribir_zip(zip_path, GTFS_FILES)
            with self.assertRaises(ValueError):
                convertir_gtfs("T5", zip_path, raiz / "data")


if __name__ == "__main__":
    unittest.main()
