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


if __name__ == "__main__":
    unittest.main()
