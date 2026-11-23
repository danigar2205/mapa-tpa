import unittest

import app as app_module


class AvanzaScanLifecycleTests(unittest.TestCase):
    def test_new_scan_starts_without_buses_from_previous_scan(self):
        estado_anterior = {
            "buses_parciales": [
                {"coche": "101", "linea": "1"},
                {"coche": "257", "linea": "2"},
            ]
        }

        buses = app_module._buses_parciales_del_mismo_escaneo(
            estado_anterior,
            reanudar_escaneo=False,
        )

        self.assertEqual(buses, [])

    def test_resume_keeps_only_partial_buses_from_the_same_scan(self):
        estado_actual = {
            "buses_parciales": [
                {"coche": "101", "linea": "1"},
                {"coche": "257", "linea": "2"},
                None,
                {"linea": "3"},
            ]
        }

        buses = app_module._buses_parciales_del_mismo_escaneo(
            estado_actual,
            reanudar_escaneo=True,
        )

        self.assertEqual(
            [bus["coche"] for bus in buses],
            ["101", "257"],
        )

    def test_cache_from_previous_scan_logic_version_is_invalid(self):
        self.assertFalse(
            app_module._cache_avanza_es_compatible({
                "scan_version": app_module.AVANZA_SCAN_VERSION - 1,
                "buses": [{"coche": "257"}],
            })
        )

    def test_cache_from_current_scan_logic_version_is_valid(self):
        self.assertTrue(
            app_module._cache_avanza_es_compatible({
                "scan_version": app_module.AVANZA_SCAN_VERSION,
                "buses": [{"coche": "101"}],
            })
        )




class AvanzaStopResponseTests(unittest.TestCase):
    def setUp(self):
        self.completados = set()
        self.paradas = {}
        self.fallos = {}
        self.errores = {}

    def test_failed_stop_is_kept_pending_for_retry(self):
        resultado = {
            "poste": 812,
            "nombre": "Parada 812",
            "buses": [],
            "error": "Read timed out",
        }

        valida = app_module._registrar_resultado_parada_avanza(
            resultado,
            self.completados,
            self.paradas,
            self.fallos,
            self.errores,
        )

        self.assertFalse(valida)
        self.assertNotIn("812", self.completados)
        self.assertIn("812", self.fallos)
        self.assertIn("812", self.errores)
        self.assertNotIn("812", self.paradas)

    def test_valid_empty_response_counts_as_a_successful_stop(self):
        resultado = {
            "poste": 812,
            "nombre": "Parada 812",
            "buses": [],
            "error": None,
        }

        valida = app_module._registrar_resultado_parada_avanza(
            resultado,
            self.completados,
            self.paradas,
            self.fallos,
            self.errores,
        )

        self.assertTrue(valida)
        self.assertIn("812", self.completados)
        self.assertIn("812", self.paradas)
        self.assertNotIn("812", self.fallos)
        self.assertNotIn("812", self.errores)

    def test_successful_retry_clears_previous_failure(self):
        fallo = {
            "poste": 812,
            "nombre": "Parada 812",
            "buses": [],
            "error": "Read timed out",
        }
        app_module._registrar_resultado_parada_avanza(
            fallo,
            self.completados,
            self.paradas,
            self.fallos,
            self.errores,
        )

        recuperada = {
            "poste": 812,
            "nombre": "Parada 812",
            "buses": [],
            "error": None,
        }
        valida = app_module._registrar_resultado_parada_avanza(
            recuperada,
            self.completados,
            self.paradas,
            self.fallos,
            self.errores,
        )

        self.assertTrue(valida)
        self.assertIn("812", self.completados)
        self.assertNotIn("812", self.fallos)
        self.assertNotIn("812", self.errores)


if __name__ == "__main__":
    unittest.main()
