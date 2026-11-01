import math
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, Mock

from app import (
    app,
    validar_coordenadas,
    resolver_destino_y_parada,
    T05_ROUTE_NAMES,
    GTFS_T05_SCHEDULE,
    GTFS_DATASETS,
    servicio_activo_gtfs,
    segundos_horario_gtfs,
    obtener_destino_final_viaje_gtfs,
    obtener_ultima_parada_id_viaje_gtfs,
    es_ultima_parada_viaje_gtfs,
    resolver_destino_horario_gtfs,
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


class DestinoSentidoGTFSTests(unittest.TestCase):
    def setUp(self):
        self.dataset = {
            "lookup": {
                "stops": {
                    "s1": "Teruel. Estación de Autobuses",
                    "s2": "Calamocha. Estación",
                    "s3": "Zaragoza. Estación Intermodal",
                },
                "trips": {
                    "viaje-teruel-zaragoza": {"1": "s1", "2": "s2", "3": "s3"},
                },
            }
        }

    def test_obtiene_destino_de_la_ultima_parada_del_viaje(self):
        self.assertEqual(
            obtener_destino_final_viaje_gtfs(
                self.dataset, "viaje-teruel-zaragoza"
            ),
            "Zaragoza. Estación Intermodal",
        )

    def test_sustituye_headsign_generico_por_el_sentido_del_viaje(self):
        registro = {
            "trip_id": "viaje-teruel-zaragoza",
            "headsign": "Teruel - Zaragoza",
        }
        info_ruta = {"long_name": "Teruel - Zaragoza"}
        self.assertEqual(
            resolver_destino_horario_gtfs("T11", registro, info_ruta, self.dataset),
            "Zaragoza. Estación Intermodal",
        )

    def test_conserva_headsign_especifico_de_t05(self):
        registro = {
            "trip_id": "viaje-teruel-zaragoza",
            "headsign": "Fraga",
        }
        info_ruta = {"long_name": "Zaragoza - Fraga"}
        self.assertEqual(
            resolver_destino_horario_gtfs("T05", registro, info_ruta, self.dataset),
            "Fraga",
        )

    def test_conserva_headsign_especifico_de_otra_concesion(self):
        registro = {
            "trip_id": "viaje-teruel-zaragoza",
            "headsign": "Alcañiz",
        }
        info_ruta = {"long_name": "Teruel - Zaragoza"}
        self.assertEqual(
            resolver_destino_horario_gtfs("T07", registro, info_ruta, self.dataset),
            "Alcañiz",
        )


    def test_t11_01_real_sustituye_el_nombre_generico_por_destino_del_viaje(self):
        dataset = GTFS_DATASETS["T11"]
        rutas = {
            route_id: info
            for route_id, info in dataset["schedule"].get("routes", {}).items()
            if info.get("short_name") == "T11-01"
        }
        self.assertTrue(rutas, "No se encontraron horarios de T11-01")

        registros = [
            registro
            for registro in dataset["schedule"].get("stop_times", [])
            if registro.get("route_id") in rutas
        ]
        self.assertTrue(registros, "T11-01 no contiene expediciones")

        # El GTFS incorpora el nombre de corredor como headsign genérico.
        genericos = [
            registro for registro in registros
            if str(registro.get("headsign") or "").strip().casefold()
            == str(rutas[registro["route_id"]].get("long_name") or "").strip().casefold()
        ]
        self.assertTrue(genericos, "No se encontró el headsign genérico que causa el problema")

        registro = genericos[0]
        info_ruta = rutas[registro["route_id"]]
        destino = resolver_destino_horario_gtfs("T11", registro, info_ruta, dataset)
        self.assertTrue(destino)
        self.assertNotEqual(destino.casefold(), info_ruta["long_name"].casefold())


class FusionParadasYTerminalesTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def _paradas_por_nombre(self, concesion, nombre):
        datos = self.client.get(f"/api/mapa/{concesion}").get_json()
        return [parada for parada in datos["stops"] if parada["name"] == nombre]

    def test_huesca_estacion_es_un_solo_marcador_en_t04(self):
        paradas = self._paradas_por_nombre("T04", "Huesca. Estación de Autobuses")
        self.assertEqual(len(paradas), 1)
        self.assertEqual(
            set(paradas[0]["stop_ids"]),
            {
                "daaf50c8-4c5c-4667-955d-9f0fe0e26ade",
                "2954b4ec-9b28-4e0b-8150-a8323af99a20",
            },
        )

    def test_delicias_es_un_solo_marcador_en_todas_las_concesiones(self):
        concesiones = ("T04", "T05", "T06", "T11", "T12", "T13", "T14", "T15")
        coordenadas = []
        for concesion in concesiones:
            paradas = self._paradas_por_nombre(concesion, "Zaragoza. Estación Intermodal")
            self.assertEqual(len(paradas), 1, concesion)
            coordenadas.append((paradas[0]["lat"], paradas[0]["lon"]))
        self.assertTrue(all(punto == coordenadas[0] for punto in coordenadas))

    def test_fusiona_los_dos_registros_de_delicias_en_t06(self):
        parada = self._paradas_por_nombre("T06", "Zaragoza. Estación Intermodal")[0]
        self.assertEqual(len(parada["stop_ids"]), 2)
        self.assertIn("a27f8e1d-8040-499c-bbdf-3d2d4d1ebdcf", parada["stop_ids"])
        self.assertIn("c419a8d4-6637-4deb-8f90-001d2a66f734", parada["stop_ids"])

    def test_ejea_estacion_es_un_solo_marcador_en_t15(self):
        paradas = self._paradas_por_nombre(
            "T15", "Ejea de los Caballeros. Estación de Autobuses"
        )
        self.assertEqual(len(paradas), 1)
        self.assertEqual(len(paradas[0]["stop_ids"]), 2)

    def test_horarios_fusionados_conservan_pasada_intermedia(self):
        parada = self._paradas_por_nombre("T04", "Huesca. Estación de Autobuses")[0]
        ids = ",".join(parada["stop_ids"])
        respuesta = self.client.get(
            "/api/paradas/" + parada["id"] + "/horarios"
            "?concesion=T04&fecha=2026-11-03&stop_ids=" + ids
        )
        self.assertEqual(respuesta.status_code, 200)
        horas = [item["hora"] for item in respuesta.get_json()["horarios"]]
        self.assertIn("08:13", horas)  # Inicio de un servicio hacia otro destino.
        self.assertNotIn("08:09", horas)  # El viaje vuelve a terminar en esta misma estación.
        self.assertNotIn("08:22", horas)  # La llamada terminal no se anuncia como llegada a sí misma.

    def test_identifica_la_terminal_y_no_una_pasada_intermedia(self):
        dataset = GTFS_DATASETS["T04"]
        trip_id = "8003bcd6-6a89-48e2-b535-05b4f45b2735"
        secuencias = dataset["lookup"]["trips"][trip_id]
        self.assertEqual(
            obtener_ultima_parada_id_viaje_gtfs(dataset, trip_id),
            secuencias["20"],
        )
        self.assertFalse(es_ultima_parada_viaje_gtfs(dataset, {
            "trip_id": trip_id,
            "stop_id": "2954b4ec-9b28-4e0b-8150-a8323af99a20",
        }))
        self.assertTrue(es_ultima_parada_viaje_gtfs(dataset, {
            "trip_id": trip_id,
            "stop_id": "daaf50c8-4c5c-4667-955d-9f0fe0e26ade",
        }))


class HorariosGTFSTests(unittest.TestCase):
    def test_se_carga_el_horario_de_las_tres_lineas(self):
        nombres = {
            info["short_name"] for info in GTFS_T05_SCHEDULE["routes"].values()
        }
        self.assertEqual(nombres, {"T05-01", "T05-20", "T05-50"})
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

    def test_api_de_horarios_t05_devuelve_salidas_sin_error_500(self):
        from datetime import timedelta

        fecha = date.today() + timedelta(days=1)
        dataset = {
            "map": {"stops": {"T05-01": [{"id": "parada-prueba"}]}},
            "schedule": {
                "routes": {"ruta-prueba": {"short_name": "T05-01", "long_name": "Destino"}},
                "services": {},
                "exceptions": [{
                    "service_id": "servicio-prueba",
                    "date": fecha.strftime("%Y%m%d"),
                    "exception_type": "1",
                }],
                "stop_times": [{
                    "stop_id": "parada-prueba",
                    "service_id": "servicio-prueba",
                    "departure_time": "10:00:00",
                    "route_id": "ruta-prueba",
                    "headsign": "Destino",
                    "trip_id": "viaje-prueba",
                    "direction_id": "0",
                }],
            },
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        with patch("app.obtener_dataset_gtfs", return_value=dataset):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/horarios?concesion=T05&fecha=" + fecha.isoformat()
            )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json()["horarios"][0]["linea"], "T05-01")


    def test_api_devuelve_todas_las_salidas_cuando_superan_veinte(self):
        from datetime import timedelta

        fecha = date.today() + timedelta(days=1)
        salidas = [{
            "stop_id": "parada-prueba",
            "service_id": "servicio-prueba",
            "departure_time": "10:{:02d}:00".format(minuto),
            "route_id": "ruta-prueba",
            "headsign": "Destino",
            "trip_id": "viaje-{}".format(minuto),
            "direction_id": "0",
        } for minuto in range(25)]
        dataset = {
            "map": {"stops": {"T05-01": [{
                "id": "parada-prueba",
                "name": "Parada de prueba",
            }]}},
            "lookup": {"stops": {}, "trips": {}},
            "schedule": {
                "routes": {"ruta-prueba": {
                    "short_name": "T05-01",
                    "long_name": "Destino",
                }},
                "services": {},
                "exceptions": [{
                    "service_id": "servicio-prueba",
                    "date": fecha.strftime("%Y%m%d"),
                    "exception_type": "1",
                }],
                "stop_times": salidas,
            },
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        with patch("app.obtener_dataset_gtfs", return_value=dataset):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/horarios?concesion=T05&fecha=" + fecha.isoformat()
            )

        self.assertEqual(respuesta.status_code, 200)
        horarios = respuesta.get_json()["horarios"]
        self.assertEqual(len(horarios), 25)
        self.assertEqual(horarios[0]["hora"], "10:00")
        self.assertEqual(horarios[-1]["hora"], "10:24")


    def test_api_admite_fechas_mas_alla_de_catorce_dias_dentro_de_vigencia(self):
        from datetime import timedelta

        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        fecha = hoy + timedelta(days=35)
        fin = fecha + timedelta(days=30)
        dataset = {
            "map": {"stops": {"T05-01": [{"id": "parada-prueba"}]}},
            "lookup": {"stops": {}, "trips": {}},
            "schedule": {
                "routes": {"ruta-prueba": {
                    "short_name": "T05-01",
                    "long_name": "Destino",
                }},
                "services": {
                    "servicio-prueba": {
                        "start_date": fecha.strftime("%Y%m%d"),
                        "end_date": fin.strftime("%Y%m%d"),
                        "monday": "1", "tuesday": "1", "wednesday": "1",
                        "thursday": "1", "friday": "1", "saturday": "1",
                        "sunday": "1",
                    }
                },
                "exceptions": [],
                "stop_times": [{
                    "stop_id": "parada-prueba",
                    "service_id": "servicio-prueba",
                    "departure_time": "10:00:00",
                    "route_id": "ruta-prueba",
                    "headsign": "Destino",
                    "trip_id": "viaje-prueba",
                    "direction_id": "0",
                }],
            },
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        with patch("app.obtener_dataset_gtfs", return_value=dataset):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/horarios?concesion=T05&fecha=" + fecha.isoformat()
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json()["horarios"][0]["hora"], "10:00")
        self.assertEqual(respuesta.get_json()["vigencia_gtfs_hasta"], fin.isoformat())

    def test_api_sugiere_el_siguiente_dia_con_servicio_en_la_parada(self):
        from datetime import timedelta

        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        fecha_consultada = hoy + timedelta(days=1)
        fecha_siguiente = hoy + timedelta(days=4)
        dataset = {
            "map": {"stops": {"T05-01": [{
                "id": "parada-prueba",
                "name": "Parada de prueba",
            }]}},
            "lookup": {"stops": {}, "trips": {}},
            "schedule": {
                "routes": {"ruta-prueba": {
                    "short_name": "T05-01",
                    "long_name": "Destino",
                }},
                "services": {},
                "exceptions": [{
                    "service_id": "servicio-prueba",
                    "date": fecha_siguiente.strftime("%Y%m%d"),
                    "exception_type": "1",
                }],
                "stop_times": [{
                    "stop_id": "parada-prueba",
                    "service_id": "servicio-prueba",
                    "departure_time": "10:00:00",
                    "route_id": "ruta-prueba",
                    "headsign": "Destino",
                    "trip_id": "viaje-prueba",
                    "direction_id": "0",
                }],
            },
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        with patch("app.obtener_dataset_gtfs", return_value=dataset):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/horarios?concesion=T05&fecha=" +
                fecha_consultada.isoformat()
            )

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertEqual(datos["horarios"], [])
        self.assertEqual(datos["proxima_fecha_disponible"], fecha_siguiente.isoformat())

    def test_api_rechaza_fechas_posteriores_a_la_vigencia_gtfs(self):
        from datetime import timedelta

        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        vigencia = hoy + timedelta(days=20)
        fecha_solicitada = vigencia + timedelta(days=1)
        dataset = {
            "map": {"stops": {"T05-01": [{"id": "parada-prueba"}]}},
            "lookup": {"stops": {}, "trips": {}},
            "schedule": {
                "routes": {},
                "services": {
                    "servicio-prueba": {
                        "start_date": hoy.strftime("%Y%m%d"),
                        "end_date": vigencia.strftime("%Y%m%d"),
                        "monday": "1", "tuesday": "1", "wednesday": "1",
                        "thursday": "1", "friday": "1", "saturday": "1",
                        "sunday": "1",
                    }
                },
                "exceptions": [],
                "stop_times": [],
            },
            "has_map": True,
            "has_schedules": True,
            "has_realtime": False,
        }
        with patch("app.obtener_dataset_gtfs", return_value=dataset):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/horarios?concesion=T05&fecha=" +
                fecha_solicitada.isoformat()
            )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(
            respuesta.get_json()["vigencia_gtfs_hasta"], vigencia.isoformat()
        )

    def test_api_de_llegadas_t05_responde_con_feed_gtfsrt_vacio(self):
        from types import SimpleNamespace

        dataset = {
            "map": {"routes": {"ruta-prueba": {"short_name": "T05-01"}} ,
                    "stops": {"T05-01": [{"id": "parada-prueba"}]}},
            "lookup": {"stops": {}, "trips": {}},
            "schedule": {"routes": {}, "services": {}, "exceptions": [], "stop_times": []},
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        feed_vacio = SimpleNamespace(status_code=200, content=bytes([0x12, 0x00]))
        with patch("app.obtener_dataset_gtfs", return_value=dataset), \
             patch("app.obtener_respuesta_cacheada", return_value=feed_vacio) as consulta:
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/llegadas?concesion=T05"
            )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.get_json()["tiempo_real_disponible"])
        self.assertEqual(respuesta.get_json()["llegadas"], [])
        consulta.assert_called()



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
        self.assertGreaterEqual(len(datos["stops"]), 20)
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

    def test_t13_muestra_sus_datos_sin_confundirse_con_t05(self):
        datos = self.client.get("/api/mapa/t13").get_json()
        nombres = {ruta["short_name"] for ruta in datos["routes"]}
        self.assertTrue(nombres)
        self.assertTrue(all(nombre.startswith("T13-") for nombre in nombres))
        self.assertTrue(datos["stops"])
        self.assertTrue(all(parada["concesion"] == "T13" for parada in datos["stops"]))
        self.assertFalse(datos["funciones"]["tiempo_real"])


    def test_selector_de_fecha_usa_vigencia_gtfs_y_ofrece_proximo_servicio(self):
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("selectorFecha.removeAttribute('max')", html)
        self.assertNotIn("selectorFecha.max = desplazarFechaISO(hoy, 14)", html)
        self.assertIn("datosHorario.vigencia_gtfs_hasta", html)
        self.assertIn("datosHorario.proxima_fecha_disponible", html)
        self.assertIn("stop-next-service-button", html)

    def test_solo_t05_se_habilita_en_el_menu_inicial(self):
        html = app.test_client().get("/").get_data(as_text=True)
        codigos = ["T01", "T02", "T03", "T04"] + [
            "T{:02d}".format(numero) for numero in range(6, 16)
        ]
        for codigo in codigos:
            boton = html.split('data-concesion="' + codigo + '"', 1)[0].rsplit("<button", 1)[-1]
            self.assertIn("disabled", boton, codigo)
            self.assertIn("btn-deshabilitado", boton, codigo)

        boton_t05 = html.split('data-concesion="T05"', 1)[0].rsplit("<button", 1)[-1]
        self.assertNotIn("disabled", boton_t05)
        self.assertNotIn("btn-deshabilitado", boton_t05)

        # La respuesta dinámica del catálogo tampoco puede volver a habilitar otras concesiones.
        self.assertIn("codigo === 'T05' && !!configuracion.disponible", html)
        self.assertIn(": 'Próximamente';", html)

    def test_catalogo_indica_capacidades_reales_por_concesion(self):
        catalogo = self.client.get("/api/concesiones").get_json()["concesiones"]
        por_codigo = {item["codigo"]: item for item in catalogo}
        self.assertEqual(len(por_codigo), 15)
        self.assertTrue(por_codigo["T05"]["disponible"])
        self.assertTrue(por_codigo["T05"]["funciones"]["paradas"])
        self.assertTrue(por_codigo["T05"]["funciones"]["horarios"])
        self.assertTrue(por_codigo["T05"]["funciones"]["tiempo_real"])
        self.assertFalse(por_codigo["T01"]["disponible"])
        self.assertFalse(por_codigo["T01"]["funciones"]["tiempo_real"])

    def test_endpoint_de_buses_no_consulta_concesiones_sin_feed_rt(self):
        with patch("app.obtener_respuesta_cacheada") as consulta:
            respuesta = self.client.get("/api/buses/t13")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json(), [])
        consulta.assert_not_called()

    def test_identifica_numero_de_linea_por_route_id(self):
        self.assertEqual(len(T05_ROUTE_NAMES), 3)
        self.assertEqual(set(T05_ROUTE_NAMES.values()), {"T05-01", "T05-20", "T05-50"})


class CorreccionesCoordenadasT11Tests(unittest.TestCase):
    def test_t11_usa_las_dos_coordenadas_corregidas(self):
        datos = app.test_client().get("/api/mapa/T11").get_json()
        por_nombre = {parada["name"]: parada for parada in datos["stops"]}

        violante = por_nombre["C/ Violante De Hungría (Escuela de Idiomas)"]
        self.assertEqual((violante["lat"], violante["lon"]), (41.642076, -0.905206))

        cutanda = por_nombre["Cutanda. Marquesina C/ Mayor Nº 2"]
        self.assertEqual((cutanda["lat"], cutanda["lon"]), (40.934940, -1.184232))

        self.assertTrue(violante["lineas"])
        self.assertTrue(cutanda["lineas"])


if __name__ == "__main__":
    unittest.main()
