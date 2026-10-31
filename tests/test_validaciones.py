import math
import unittest
from datetime import date
from unittest.mock import patch, Mock

from app import (
    app,
    validar_coordenadas,
    resolver_destino_y_parada,
    T05_ROUTE_NAMES,
    GTFS_T05_SCHEDULE,
    servicio_activo_gtfs,
    segundos_horario_gtfs,
    obtener_respuesta_cacheada,
    CACHE_RESPUESTAS_GTFSRT,
)


class ValidarCoordenadasTests(unittest.TestCase):
    def test_acepta_coordenadas_cero(self):
        self.assertEqual(validar_coordenadas(0, 0), (0.0, 0.0))

    def test_acepta_limites_geograficos(self):
        self.assertEqual(validar_coordenadas(90, 180), (90.0, 180.0))
        self.assertEqual(validar_coordenadas(-90, -180), (-90.0, -180.0))

    def test_rechaza_valores_ausentes(self):
        self.assertIsNone(validar_coordenadas(None, 0))
        self.assertIsNone(validar_coordenadas(0, None))

    def test_rechaza_coordenadas_fuera_de_rango(self):
        self.assertIsNone(validar_coordenadas(91, 0))
        self.assertIsNone(validar_coordenadas(0, 181))
        self.assertIsNone(validar_coordenadas(-91, 0))
        self.assertIsNone(validar_coordenadas(0, -181))

    def test_rechaza_nan_e_infinito(self):
        self.assertIsNone(validar_coordenadas(math.nan, 0))
        self.assertIsNone(validar_coordenadas(0, math.inf))

    def test_rechaza_texto_no_numerico(self):
        self.assertIsNone(validar_coordenadas("norte", "oeste"))


class ProximaParadaGTFSTests(unittest.TestCase):
    ROUTE_T05_01 = "d3efc296-1916-41c3-90c2-166ff0e8e4fb"

    def test_resuelve_parada_por_viaje_y_secuencia(self):
        destino, parada = resolver_destino_y_parada(
            "T05", self.ROUTE_T05_01, 0, 0,
            trip_id="0a56e2eb-d7a1-408a-8840-8852d372babc"
        )
        self.assertEqual(destino, "Zaragoza")
        self.assertEqual(parada, "Fraga. Estación")

    def test_resuelve_parada_por_stop_id(self):
        destino, parada = resolver_destino_y_parada(
            "T05", self.ROUTE_T05_01, 0, None,
            stop_id="51d66e5a-5e4f-45e5-96b2-da375c2ff9ee"
        )
        self.assertEqual(parada, "Fraga. Estación")

    def test_no_inventa_nombre_si_no_hay_coincidencia(self):
        _, parada = resolver_destino_y_parada(
            "T05", self.ROUTE_T05_01, 0, 999,
            trip_id="viaje-inexistente"
        )
        self.assertEqual(parada, "Parada sin identificar")


class HorariosGTFSTests(unittest.TestCase):
    def test_se_carga_el_horario_de_las_tres_lineas(self):
        self.assertEqual(set(GTFS_T05_SCHEDULE["routes"].values()), {
            "T05-01", "T05-20", "T05-50"
        })
        self.assertGreaterEqual(len(GTFS_T05_SCHEDULE["stop_times"]), 300)

    def test_respeta_excepciones_de_calendario(self):
        service_id = "bde18d92-f44d-45fb-8970-e4d3fbc899be_22146502-c35d-4e38-a720-41155b8c4aff"
        self.assertTrue(servicio_activo_gtfs(service_id, date(2026, 11, 7)))
        self.assertFalse(servicio_activo_gtfs(service_id, date(2026, 11, 8)))

    def test_admite_horas_gtfs_superiores_a_24(self):
        self.assertEqual(segundos_horario_gtfs("25:15:00"), 90900)
        self.assertIsNone(segundos_horario_gtfs("12:75:00"))
        self.assertIsNone(segundos_horario_gtfs("hora"))

    def test_rechaza_parada_inexistente_en_api_de_horarios(self):
        respuesta = app.test_client().get("/api/paradas/inexistente/horarios")
        self.assertEqual(respuesta.status_code, 404)


class CacheGTFSRTTests(unittest.TestCase):
    def test_reutiliza_respuesta_durante_ttl(self):
        url = "https://test.local/gtfsrt-cache-unittest"
        clave = ("unit-test", url)
        CACHE_RESPUESTAS_GTFSRT.pop(clave, None)
        respuesta_falsa = Mock(status_code=200, content=b"feed")

        with patch("app.session.get", return_value=respuesta_falsa) as get:
            primera = obtener_respuesta_cacheada(url, "unit-test", 60, {}, 1)
            segunda = obtener_respuesta_cacheada(url, "unit-test", 60, {}, 1)

        self.assertEqual(primera.content, b"feed")
        self.assertEqual(segunda.content, b"feed")
        self.assertEqual(get.call_count, 1)
        CACHE_RESPUESTAS_GTFSRT.pop(clave, None)


class CapasMapaGTFSTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_api_devuelve_las_tres_lineas_t05(self):
        respuesta = self.client.get("/api/mapa/t05")
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        nombres = {ruta["short_name"] for ruta in datos["routes"]}
        self.assertEqual(nombres, {"T05-01", "T05-20", "T05-50"})

    def test_api_devuelve_paradas_con_coordenadas_y_lineas(self):
        datos = self.client.get("/api/mapa/T05").get_json()
        self.assertGreaterEqual(len(datos["stops"]), 30)
        for parada in datos["stops"]:
            self.assertTrue(-90 <= parada["lat"] <= 90)
            self.assertTrue(-180 <= parada["lon"] <= 180)
            self.assertTrue(parada["lineas"])
            self.assertTrue(set(parada["lineas"]).issubset({"T05-01", "T05-20", "T05-50"}))
        self.assertTrue(all("shapes" not in ruta for ruta in datos["routes"]))

    def test_lugarico_indica_que_lineas_paran(self):
        datos = self.client.get("/api/mapa/T05").get_json()
        lugarico = next(parada for parada in datos["stops"] if parada["name"] == "Lugarico de Cerdán")
        self.assertEqual(set(lugarico["lineas"]), {"T05-01", "T05-20"})

    def test_otras_concesiones_no_reciben_datos_t05(self):
        datos = self.client.get("/api/mapa/t13").get_json()
        self.assertEqual(datos, {"routes": [], "stops": []})

    def test_identifica_numero_de_linea_por_route_id(self):
        self.assertEqual(len(T05_ROUTE_NAMES), 3)
        self.assertEqual(set(T05_ROUTE_NAMES.values()), {"T05-01", "T05-20", "T05-50"})


if __name__ == "__main__":
    unittest.main()
