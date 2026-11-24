import unittest
from unittest.mock import patch

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


class AvanzaResetTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_pending_reset_is_preserved_while_same_scan_writes_checkpoints(self):
        estado_nuevo = {
            "scan_id": "scan-actual",
            "nuevo_barrido_solicitado": False,
        }
        estado_compartido = {
            "scan_id": "scan-actual",
            "nuevo_barrido_solicitado": True,
            "nuevo_barrido_solicitado_epoch": 12345.0,
        }

        resultado = app_module._preservar_solicitud_nuevo_barrido(
            estado_nuevo,
            estado_compartido,
        )

        self.assertTrue(resultado["nuevo_barrido_solicitado"])
        self.assertEqual(resultado["nuevo_barrido_solicitado_epoch"], 12345.0)

    def test_new_scan_id_does_not_inherit_previous_reset_request(self):
        estado_nuevo = {
            "scan_id": "scan-nuevo",
            "nuevo_barrido_solicitado": False,
        }
        estado_compartido = {
            "scan_id": "scan-anterior",
            "nuevo_barrido_solicitado": True,
            "nuevo_barrido_solicitado_epoch": 12345.0,
        }

        resultado = app_module._preservar_solicitud_nuevo_barrido(
            estado_nuevo,
            estado_compartido,
        )

        self.assertFalse(resultado["nuevo_barrido_solicitado"])

    def test_forced_reload_queues_clean_scan_even_if_scan_is_active(self):
        estado_anterior = {
            "scan_id": "scan-activo",
            "escaneo_en_curso": True,
            "fase": "consultando_gps",
            "heartbeat_epoch": 9999999999.0,
            "buses_parciales": [{"coche": "257", "linea": "1"}],
        }
        estado_global_anterior = dict(app_module.AVANZA_URBANOS_STATE)
        try:
            with patch.object(
                app_module,
                "_leer_avanza_estado_compartido",
                return_value={"state": dict(estado_anterior)},
            ), patch.object(
                app_module,
                "_guardar_avanza_estado_compartido",
            ) as guardar:
                app_module._solicitar_nuevo_barrido_avanza_si_ocupado(forzar=True)

            estado_guardado = guardar.call_args.kwargs["estado"]
            self.assertTrue(estado_guardado["nuevo_barrido_solicitado"])
            self.assertEqual(estado_guardado["scan_id"], "scan-activo")
        finally:
            app_module.AVANZA_URBANOS_STATE.clear()
            app_module.AVANZA_URBANOS_STATE.update(estado_global_anterior)

    def test_pending_reset_response_hides_old_buses_and_zeros_counters(self):
        autobus_antiguo = {
            "coche": "257",
            "id": "257",
            "linea": "1",
            "destino": "Plaza Aragón",
        }
        estado = {
            "scan_id": "scan-antiguo",
            "nuevo_barrido_solicitado": True,
            "nuevo_barrido_solicitado_epoch": 1795530000.0,
            "escaneo_en_curso": True,
            "fase": "consultando_gps",
            "progreso": 150,
            "total_progreso": 900,
            "buses_parciales": [autobus_antiguo],
            "paradas_gps_consultadas": [{"poste": 14}],
        }
        datos_anteriores = {
            "buses": [autobus_antiguo],
            "coches_unicos_detectados": 1,
            "actualizado": "2026-11-24T14:00:00+01:00",
            "actualizado_epoch": 1795525200.0,
            "total_paradas": 900,
            "paradas_consultadas": [{"poste": 14}],
            "paradas_consultadas_count": 1,
        }

        acceso = self.client.post(
            "/api/avanza/login",
            json={"usuario": "user", "password": "busero"},
        )
        self.assertEqual(acceso.status_code, 200)

        with patch.object(app_module, "iniciar_barrido_avanza_si_necesario"), patch.object(
            app_module,
            "_sincronizar_avanza_estado_compartido",
            return_value=(datos_anteriores, estado, 1795525200.0),
        ):
            respuesta = self.client.get("/api/avanza/urbanos?forzar=1")

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertTrue(datos["reinicio_pendiente"])
        self.assertEqual(datos["fase"], "iniciando")
        self.assertEqual(datos["buses"], [])
        self.assertEqual(datos["coches_unicos_detectados"], 0)
        self.assertEqual(datos["total_paradas"], 0)
        self.assertEqual(datos["paradas_consultadas_count"], 0)
        self.assertIsNone(datos["actualizado"])





class AvanzaArrivalParsingTests(unittest.TestCase):
    HTML = (
        '<li><strong>039 <i class="fa fa-long-arrow-right"></i>VADORREY</strong>'
        '<ul><li><a href="/index.php/zaragoza/fParadas/812/4650">4650 [3 mins]</a></li></ul></li>'
        '<li><strong>CI1 <i class="fa fa-long-arrow-right"></i>PLAZA ESPAÑA</strong>'
        '<ul><li><a href="/index.php/zaragoza/fParadas/813/4701">4701 [5 mins]</a></li></ul></li>'
    )

    def test_extracts_line_destination_vehicle_and_eta_from_arrival_table(self):
        llegadas = app_module.extraer_llegadas_tablatiempos_avanza(self.HTML)

        self.assertEqual(
            llegadas,
            [
                {
                    "coche": "4650",
                    "linea": "039",
                    "destino": "VADORREY",
                    "eta_minutos": 3,
                },
                {
                    "coche": "4701",
                    "linea": "CI1",
                    "destino": "PLAZA ESPAÑA",
                    "eta_minutos": 5,
                },
            ],
        )

    def test_keeps_arrival_even_without_machine_coordinates(self):
        buses = app_module.buses_de_respuesta_gps_avanza(
            {"tablatiempos": self.HTML, "maquinas": {}},
            poste=812,
            nombre_parada="Paseo Independencia",
        )

        self.assertEqual([bus["coche"] for bus in buses], ["4650", "4701"])
        self.assertEqual([bus["linea"] for bus in buses], ["39", "CI1"])
        self.assertIsNone(buses[0]["lat"])
        self.assertIsNone(buses[0]["lon"])
        self.assertEqual(buses[0]["parada"], "Paseo Independencia")

    def test_merges_optional_position_from_machine_marker(self):
        maquinas = {
            "0": {
                "icon": "bus.png",
                "title": "039 4650",
                "coordenadas": {"0": {"LAT": 41.65, "LON": -0.88}},
                "info": (
                    "<table><tr><td>a</td><td>b</td><td>c</td><td>d</td>"
                    "<td>4650</td><td>039 -> VADORREY</td>"
                    "<td>3 min</td><td>0.5 km</td></tr></table>"
                ),
            }
        }
        buses = app_module.buses_de_respuesta_gps_avanza(
            {"tablatiempos": self.HTML, "maquinas": maquinas},
            poste=812,
            nombre_parada="Paseo Independencia",
        )
        autobus = next(bus for bus in buses if bus["coche"] == "4650")
        self.assertEqual(autobus["linea"], "39")
        self.assertAlmostEqual(autobus["lat"], 41.65)
        self.assertAlmostEqual(autobus["lon"], -0.88)




class AvanzaCatalogueVersionTests(unittest.TestCase):
    def test_old_route_catalogue_is_invalidated(self):
        self.assertFalse(
            app_module._catalogo_avanza_es_compatible({
                "version": app_module.AVANZA_CATALOGO_VERSION - 1,
                "postes": [{"poste": 14}],
            })
        )

    def test_current_route_catalogue_can_be_reused(self):
        self.assertTrue(
            app_module._catalogo_avanza_es_compatible({
                "version": app_module.AVANZA_CATALOGO_VERSION,
                "postes": [{"poste": 14}],
            })
        )



if __name__ == "__main__":
    unittest.main()
