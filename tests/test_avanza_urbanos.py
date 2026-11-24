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

    def test_http_403_is_not_retried(self):
        resultado = {
            "poste": 812,
            "nombre": "Parada 812",
            "buses": [],
            "error": "poste 812: HTTP 403",
        }
        self.assertFalse(app_module._avanza_resultado_permite_reintento(resultado))

    def test_read_timeout_is_retryable(self):
        resultado = {
            "poste": 812,
            "nombre": "Parada 812",
            "buses": [],
            "error": "poste 812: Read timed out",
        }
        self.assertTrue(app_module._avanza_resultado_permite_reintento(resultado))

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


class AvanzaAccessTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_urbanos_endpoint_rejects_requests_without_login(self):
        respuesta = self.client.get("/api/avanza/urbanos")
        self.assertEqual(respuesta.status_code, 401)

    def test_login_rejects_incorrect_credentials(self):
        respuesta = self.client.post(
            "/api/avanza/login",
            json={"usuario": app_module.AVANZA_LOGIN_USER, "password": "incorrecta"},
        )
        self.assertEqual(respuesta.status_code, 401)

    def test_login_accepts_configured_credentials(self):
        respuesta = self.client.post(
            "/api/avanza/login",
            json={
                "usuario": app_module.AVANZA_LOGIN_USER,
                "password": app_module.AVANZA_LOGIN_PASSWORD,
            },
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.get_json()["ok"])
        with self.client.session_transaction() as sesion:
            self.assertTrue(sesion.get("avanza_autorizado"))

    def test_login_accepts_initial_credentials_even_if_render_values_were_overridden(self):
        usuario_anterior = app_module.AVANZA_LOGIN_USER
        password_anterior = app_module.AVANZA_LOGIN_PASSWORD
        try:
            app_module.AVANZA_LOGIN_USER = "usuario-antiguo-de-render"
            app_module.AVANZA_LOGIN_PASSWORD = "clave-antigua-de-render"
            respuesta = self.client.post(
                "/api/avanza/login",
                json={"usuario": "user", "password": "busero"},
            )
            self.assertEqual(respuesta.status_code, 200)
            self.assertTrue(respuesta.get_json()["ok"])
            with self.client.session_transaction() as sesion:
                self.assertTrue(sesion.get("avanza_autorizado"))
        finally:
            app_module.AVANZA_LOGIN_USER = usuario_anterior
            app_module.AVANZA_LOGIN_PASSWORD = password_anterior

    def test_logout_clears_access_session(self):
        self.client.post(
            "/api/avanza/login",
            json={
                "usuario": app_module.AVANZA_LOGIN_USER,
                "password": app_module.AVANZA_LOGIN_PASSWORD,
            },
        )
        respuesta = self.client.post("/api/avanza/logout")
        self.assertEqual(respuesta.status_code, 200)
        with self.client.session_transaction() as sesion:
            self.assertFalse(sesion.get("avanza_autorizado"))


if __name__ == "__main__":
    unittest.main()
