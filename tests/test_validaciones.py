import math
import unittest

from app import app, validar_coordenadas, resolver_destino_y_parada, T05_ROUTE_NAMES


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


class CapasMapaGTFSTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_api_devuelve_las_tres_lineas_t05(self):
        respuesta = self.client.get("/api/mapa/t05")
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        nombres = {ruta["short_name"] for ruta in datos["routes"]}
        self.assertEqual(nombres, {"T05-01", "T05-20", "T05-50"})

    def test_api_devuelve_recorridos_y_paradas_con_coordenadas(self):
        datos = self.client.get("/api/mapa/T05").get_json()
        self.assertEqual(sum(len(ruta["shapes"]) for ruta in datos["routes"]), 6)
        self.assertGreaterEqual(len(datos["stops"]), 30)
        for parada in datos["stops"]:
            self.assertTrue(-90 <= parada["lat"] <= 90)
            self.assertTrue(-180 <= parada["lon"] <= 180)

    def test_otras_concesiones_no_reciben_datos_t05(self):
        datos = self.client.get("/api/mapa/t13").get_json()
        self.assertEqual(datos, {"routes": [], "stops": []})

    def test_identifica_numero_de_linea_por_route_id(self):
        self.assertEqual(len(T05_ROUTE_NAMES), 3)
        self.assertEqual(set(T05_ROUTE_NAMES.values()), {"T05-01", "T05-20", "T05-50"})


if __name__ == "__main__":
    unittest.main()
