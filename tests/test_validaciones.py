import math
import app as app_module
import inspect
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
    procesar_ocupacion,
    leer_campo_protobuf_opcional,
    convertir_velocidad_kmh,
    obtener_estimaciones_tripupdates,
    resolver_evento_horario_gtfs,
    formatear_retraso_gtfs,
)


class BuzonSugerenciasQuejasTests(unittest.TestCase):
    CONFIGURACION_SMTP = {
        "BUZON_SMTP_HOST": "smtp.test.local",
        "BUZON_SMTP_PORT": "587",
        "BUZON_SMTP_USER": "usuario@test.local",
        "BUZON_SMTP_PASSWORD": "secreto-de-prueba",
        "BUZON_EMAIL_DESTINO": "buzon@test.local",
        "BUZON_EMAIL_REMITENTE": "no-responder@test.local",
    }

    def setUp(self):
        app_module.BUZON_ULTIMO_ENVIO.clear()
        for categoria in app_module.BUZON_CUOTAS:
            app_module.BUZON_CUOTAS[categoria].clear()
        self.client = app.test_client()

    def test_estado_indica_buzon_no_configurado_sin_credenciales(self):
        with patch.dict(app_module.os.environ, {}, clear=True):
            respuesta = self.client.get("/api/buzon/estado")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json(), {"disponible": False})

    def test_estado_indica_disponible_con_api_key_de_resend_sin_smtp(self):
        with patch.dict(
            app_module.os.environ,
            {"BUZON_RESEND_API_KEY": "re_clave_de_prueba"},
            clear=True,
        ):
            respuesta = self.client.get("/api/buzon/estado")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json(), {"disponible": True})

    def test_configuracion_predeterminada_usa_gmail_y_el_destinatario_indicado(self):
        with patch.dict(
            app_module.os.environ,
            {"BUZON_SMTP_PASSWORD": "clave-de-aplicacion-de-prueba"},
            clear=True,
        ):
            configuracion = app_module.obtener_configuracion_buzon()

        self.assertEqual(configuracion["host"], "smtp.gmail.com")
        self.assertEqual(configuracion["puerto"], 587)
        self.assertEqual(configuracion["usuario"], "ribota2010@gmail.com")
        self.assertEqual(configuracion["destino"], "ribota2010@gmail.com")
        self.assertEqual(configuracion["remitente"], "ribota2010@gmail.com")
        self.assertFalse(configuracion["ssl"])

    def test_configuracion_elimina_espacios_de_la_contrasena_de_aplicacion(self):
        with patch.dict(
            app_module.os.environ,
            {"BUZON_SMTP_PASSWORD": "abcd efgh ijkl mnop"},
            clear=True,
        ):
            configuracion = app_module.obtener_configuracion_buzon()

        self.assertEqual(configuracion["password"], "abcdefghijklmnop")

    def test_envio_gmail_solo_necesita_la_contrasena_de_aplicacion_en_render(self):
        datos = {
            "tipo": "queja",
            "asunto": "Incidencia de prueba",
            "mensaje": "El menú de inicio tiene un problema que quiero comunicar.",
        }
        with (
            patch.dict(
                app_module.os.environ,
                {"BUZON_SMTP_PASSWORD": "clave-de-aplicacion-de-prueba"},
                clear=True,
            ),
            patch("app.smtplib.SMTP") as smtp,
        ):
            servidor = smtp.return_value.__enter__.return_value
            respuesta = self.client.post("/api/buzon", json=datos)

        self.assertEqual(respuesta.status_code, 200)
        mensaje = servidor.send_message.call_args.args[0]
        self.assertEqual(mensaje["To"], "ribota2010@gmail.com")
        self.assertEqual(mensaje["From"], "ribota2010@gmail.com")
        smtp.assert_called_once_with("smtp.gmail.com", 587, timeout=12)

    def test_envia_por_resend_https_sin_necesitar_smtp(self):
        datos = {
            "tipo": "sugerencia",
            "asunto": "Añadir aviso de incidencia",
            "mensaje": "Estaría bien mostrar un aviso cuando una línea no presta servicio.",
            "correo": "persona@example.com",
        }
        respuesta_api = type("RespuestaAPI", (), {
            "ok": True, "status_code": 200, "text": '{"id":"email-id"}'
        })()
        with (
            patch.dict(
                app_module.os.environ,
                {"BUZON_RESEND_API_KEY": "re_clave_de_prueba"},
                clear=True,
            ),
            patch("app.requests.post", return_value=respuesta_api) as post,
            patch("app.smtplib.SMTP") as smtp,
        ):
            respuesta = self.client.post("/api/buzon", json=datos)

        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.get_json()["ok"])
        llamada = post.call_args
        self.assertEqual(llamada.args[0], "https://api.resend.com/emails")
        self.assertEqual(llamada.kwargs["headers"]["Authorization"], "Bearer re_clave_de_prueba")
        payload = llamada.kwargs["json"]
        self.assertEqual(payload["from"], "Mapa TPA <onboarding@resend.dev>")
        self.assertEqual(payload["to"], ["ribota2010@gmail.com"])
        self.assertEqual(payload["headers"]["Reply-To"], "persona@example.com")
        self.assertIn("Añadir aviso de incidencia", payload["text"])
        smtp.assert_not_called()

    def test_resend_no_retorna_exito_si_su_api_rechaza_el_envio(self):
        datos = {
            "tipo": "queja",
            "asunto": "No funciona el buscador",
            "mensaje": "El buscador de líneas no devuelve resultados al escribir.",
        }
        respuesta_api = type("RespuestaAPI", (), {
            "ok": False,
            "status_code": 403,
            "text": '{"message":"forbidden"}',
            "raise_for_status": lambda self: (_ for _ in ()).throw(
                app_module.requests.HTTPError("HTTP 403")
            ),
        })()
        with (
            patch.dict(
                app_module.os.environ,
                {"BUZON_RESEND_API_KEY": "re_clave_de_prueba"},
                clear=True,
            ),
            patch("app.requests.post", return_value=respuesta_api),
        ):
            respuesta = self.client.post("/api/buzon", json=datos)

        self.assertEqual(respuesta.status_code, 502)

    def test_no_acepta_mensajes_si_el_buzon_no_esta_configurado(self):
        with patch.dict(app_module.os.environ, {}, clear=True):
            respuesta = self.client.post("/api/buzon", json={
                "tipo": "queja",
                "asunto": "Problema con el mapa",
                "mensaje": "He encontrado un problema al consultar las paradas.",
            })
        self.assertEqual(respuesta.status_code, 503)

    def test_detecta_insultos_amenazas_y_variantes_disfrazadas(self):
        self.assertTrue(app_module.contenido_inadecuado_buzon(
            "El conductor es un GILIPOLLÁÁÁS"
        ))
        self.assertTrue(app_module.contenido_inadecuado_buzon(
            "Eres un g.i.l.i.p.o.l.l.a.s"
        ))
        self.assertTrue(app_module.contenido_inadecuado_buzon(
            "Te voy a matar"
        ))
        self.assertFalse(app_module.contenido_inadecuado_buzon(
            "El servicio no ha pasado por la parada y quiero comunicarlo."
        ))

    def test_bloquea_contenido_ofensivo_antes_de_contactar_con_proveedores(self):
        datos = {
            "tipo": "queja",
            "asunto": "Problema con el conductor",
            "mensaje": "El conductor es un gilipollas y no sirve para nada.",
        }
        entorno = {
            **self.CONFIGURACION_SMTP,
            "BUZON_RESEND_API_KEY": "re_clave_de_prueba",
        }
        with (
            patch.dict(app_module.os.environ, entorno, clear=True),
            patch("app.requests.post") as resend,
            patch("app.smtplib.SMTP") as smtp,
        ):
            respuesta = self.client.post("/api/buzon", json=datos)

        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("lenguaje ofensivo", respuesta.get_json()["error"])
        resend.assert_not_called()
        smtp.assert_not_called()

    def test_permite_una_queja_firme_sin_insultos(self):
        datos = {
            "tipo": "queja",
            "asunto": "Servicio sin realizar",
            "mensaje": (
                "El autobús no pasó por la parada a la hora indicada y "
                "esto me ocasionó un retraso importante."
            ),
        }
        with (
            patch.dict(app_module.os.environ, self.CONFIGURACION_SMTP, clear=True),
            patch("app.smtplib.SMTP") as smtp,
        ):
            servidor = smtp.return_value.__enter__.return_value
            respuesta = self.client.post("/api/buzon", json=datos)

        self.assertEqual(respuesta.status_code, 200)
        servidor.send_message.assert_called_once()

    def test_limita_a_dos_envios_por_navegador_en_24_horas(self):
        ahora = 100000.0
        for numero in range(2):
            permitido, _ = app_module.comprobar_cuota_buzon(
                "a" * 32, "192.0.2.10", ahora + numero
            )
            self.assertTrue(permitido)
        permitido, minutos = app_module.comprobar_cuota_buzon(
            "a" * 32, "192.0.2.10", ahora + 4
        )
        self.assertFalse(permitido)
        self.assertGreater(minutos, 0)

    def test_limita_a_cinco_envios_por_ip_entre_navegadores(self):
        ahora = 200000.0
        for numero in range(5):
            permitido, _ = app_module.comprobar_cuota_buzon(
                f"{numero:032x}", "192.0.2.20", ahora + numero
            )
            self.assertTrue(permitido)
        permitido, _ = app_module.comprobar_cuota_buzon(
            "f" * 32, "192.0.2.20", ahora + 11
        )
        self.assertFalse(permitido)

    def test_cuota_se_reinicia_al_cumplirse_24_horas(self):
        ahora = 300000.0
        for numero in range(2):
            app_module.comprobar_cuota_buzon("b" * 32, "192.0.2.30", ahora + numero)
        permitido, _ = app_module.comprobar_cuota_buzon(
            "b" * 32, "192.0.2.30", ahora + app_module.BUZON_VENTANA_SEGUNDOS + 5
        )
        self.assertTrue(permitido)

    def test_cookie_de_dispositivo_es_http_only_y_persistente(self):
        datos = {
            "tipo": "sugerencia",
            "asunto": "Añadir un aviso visual",
            "mensaje": "Sería útil mostrar un aviso cuando se produzca una incidencia.",
        }
        with (
            patch.dict(app_module.os.environ, self.CONFIGURACION_SMTP, clear=True),
            patch("app.smtplib.SMTP") as smtp,
        ):
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            respuesta = self.client.post("/api/buzon", json=datos)
        self.assertEqual(respuesta.status_code, 200)
        cookie = respuesta.headers.get("Set-Cookie", "")
        self.assertIn("mapatpa_buzon_id=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Max-Age=2592000", cookie)

    def test_valida_el_contenido_antes_de_contactar_con_smtp(self):
        with (
            patch.dict(app_module.os.environ, self.CONFIGURACION_SMTP, clear=True),
            patch("app.smtplib.SMTP") as smtp,
        ):
            respuesta = self.client.post("/api/buzon", json={
                "tipo": "queja",
                "asunto": "No",
                "mensaje": "Mensaje demasiado corto",
            })
        self.assertEqual(respuesta.status_code, 400)
        smtp.assert_not_called()

    def test_envia_sugerencia_por_correo_privado_y_usa_reply_to_opcional(self):
        datos = {
            "tipo": "sugerencia",
            "asunto": "Añadir más paradas al mapa",
            "mensaje": "Estaría bien incorporar las paradas de las próximas concesiones.",
            "nombre": "Usuario de prueba",
            "correo": "persona@example.com",
        }
        with (
            patch.dict(app_module.os.environ, self.CONFIGURACION_SMTP, clear=True),
            patch("app.smtplib.SMTP") as smtp,
        ):
            servidor = smtp.return_value.__enter__.return_value
            respuesta = self.client.post("/api/buzon", json=datos)

        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.get_json()["ok"])
        smtp.assert_called_once_with("smtp.test.local", 587, timeout=12)
        servidor.starttls.assert_called_once()
        servidor.login.assert_called_once_with("usuario@test.local", "secreto-de-prueba")
        mensaje = servidor.send_message.call_args.args[0]
        self.assertEqual(mensaje["To"], "buzon@test.local")
        self.assertEqual(mensaje["Reply-To"], "persona@example.com")
        self.assertIn("Sugerencia", mensaje["Subject"])
        self.assertIn("Añadir más paradas al mapa", mensaje.get_content())

    def test_el_correo_de_contacto_es_opcional(self):
        datos = {
            "tipo": "queja",
            "asunto": "Error de horarios",
            "mensaje": "La hora de salida que se muestra no coincide con el horario.",
        }
        with (
            patch.dict(app_module.os.environ, self.CONFIGURACION_SMTP, clear=True),
            patch("app.smtplib.SMTP") as smtp,
        ):
            servidor = smtp.return_value.__enter__.return_value
            respuesta = self.client.post("/api/buzon", json=datos)
        self.assertEqual(respuesta.status_code, 200)
        mensaje = servidor.send_message.call_args.args[0]
        self.assertIsNone(mensaje.get("Reply-To"))

    def test_menu_de_inicio_incluye_buzon_y_formulario(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="btn-abrir-buzon"', html)
        self.assertIn('id="modal-buzon"', html)
        self.assertIn('id="form-buzon"', html)
        self.assertIn("Buzón de sugerencias y quejas", html)
        self.assertIn("fetch('/api/buzon'", html)


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


class OcupacionYVelocidadGTFSRTTests(unittest.TestCase):
    def test_traduce_todos_los_estados_de_ocupacion_gtfs_rt(self):
        esperados = {
            0: "Vacío",
            1: "Asientos libres",
            2: "Pocos asientos",
            3: "Solo de pie",
            4: "Espacio muy reducido",
            5: "Lleno",
            6: "No admite pasajeros",
            7: "Sin datos",
            8: "No admite viajeros",
        }
        for valor, texto in esperados.items():
            with self.subTest(occupancy_status=valor):
                self.assertEqual(procesar_ocupacion(valor)[0], texto)

    def test_estado_ausente_no_se_confunde_con_autobus_vacio(self):
        from google.transit import gtfs_realtime_pb2

        vehiculo = gtfs_realtime_pb2.VehiclePosition()
        self.assertIsNone(leer_campo_protobuf_opcional(vehiculo, "occupancy_status"))
        vehiculo.occupancy_status = 0
        self.assertEqual(leer_campo_protobuf_opcional(vehiculo, "occupancy_status"), 0)
        self.assertEqual(procesar_ocupacion(None)[0], "Sin datos")

    def test_porcentaje_de_ocupacion_se_muestra_si_el_feed_lo_publica(self):
        self.assertEqual(
            procesar_ocupacion(7, 64),
            ("64% de ocupación", "gray"),
        )
        self.assertEqual(
            procesar_ocupacion(1, 64),
            ("Asientos libres (64%)", "green"),
        )
        self.assertEqual(procesar_ocupacion(1, -1)[0], "Asientos libres")
        self.assertEqual(procesar_ocupacion(1, 101)[0], "Asientos libres")

    def test_convierte_velocidad_de_metros_por_segundo_a_kmh(self):
        self.assertEqual(convertir_velocidad_kmh(10), 36.0)
        self.assertEqual(convertir_velocidad_kmh(0), 0.0)
        self.assertIsNone(convertir_velocidad_kmh(None))
        self.assertIsNone(convertir_velocidad_kmh(-1))
        self.assertIsNone(convertir_velocidad_kmh(float("nan")))

    def test_api_buses_incluye_ocupacion_y_velocidad_del_feed(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0  # FULL_DATASET
        feed.header.timestamp = 1800000000
        entidad = feed.entity.add()
        entidad.id = "vehiculo-prueba"
        vehiculo = entidad.vehicle
        vehiculo.trip.route_id = route_id
        vehiculo.vehicle.id = "id-vehiculo-prueba"
        vehiculo.vehicle.label = "Coche 123"
        vehiculo.position.latitude = 41.0
        vehiculo.position.longitude = -1.0
        vehiculo.position.speed = 10.0
        vehiculo.occupancy_status = 2

        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        with patch("app.obtener_estimaciones_tripupdates", return_value={}), \
             patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed):
            respuesta = app.test_client().get("/api/buses/t05")

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertEqual(len(datos), 1)
        self.assertEqual(datos[0]["ocupacion"], "Pocos asientos")
        self.assertEqual(datos[0]["velocidad_kmh"], 36.0)

    def test_api_buses_prioriza_trip_id_si_el_id_del_vehiculo_se_repite(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0
        feed.header.timestamp = 1800000000
        entidad = feed.entity.add()
        entidad.id = "vehiculo-trip-test"
        vehiculo = entidad.vehicle
        vehiculo.trip.route_id = route_id
        vehiculo.trip.trip_id = "trip-correcto"
        vehiculo.vehicle.id = "vehicle-compartido"
        vehiculo.position.latitude = 41.0
        vehiculo.position.longitude = -1.0

        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        estimaciones = {
            "vehicle-compartido": {
                "retraso": "+99 min retraso",
                "retraso_segundos": 5940,
            },
            "trip-correcto": {
                "retraso": "+3 min retraso",
                "retraso_segundos": 180,
            },
        }
        with (
            patch("app.obtener_estimaciones_tripupdates", return_value=estimaciones),
            patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed),
        ):
            respuesta = app.test_client().get("/api/buses/t05")

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json()[0]["retraso"], "+3 min retraso")

    def test_api_buses_combina_autobuses_de_varios_proveedores(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        respuestas = []
        for indice in range(2):
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.header.gtfs_realtime_version = "2.0"
            feed.header.incrementality = 0
            feed.header.timestamp = 1800000000
            entidad = feed.entity.add()
            entidad.id = "feed-" + str(indice)
            entidad.vehicle.trip.route_id = route_id
            entidad.vehicle.trip.trip_id = "trip-" + str(indice)
            entidad.vehicle.vehicle.id = "vehicle-" + str(indice)
            entidad.vehicle.position.latitude = 41.0 + indice * 0.1
            entidad.vehicle.position.longitude = -1.0 - indice * 0.1
            respuestas.append(SimpleNamespace(
                status_code=200,
                content=feed.SerializeToString(),
            ))

        with (
            patch("app.obtener_estimaciones_tripupdates", return_value={}),
            patch("app.obtener_respuesta_cacheada", side_effect=respuestas),
        ):
            respuesta = app.test_client().get("/api/buses/t05")

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertEqual(len(datos), 2)
        self.assertEqual({bus["id"] for bus in datos}, {"vehicle-0", "vehicle-1"})

    def test_api_buses_distingue_feed_vacio_de_fallo_de_conexion(self):
        from types import SimpleNamespace

        with (
            patch("app.obtener_estimaciones_tripupdates", return_value={}),
            patch(
                "app.obtener_respuesta_cacheada",
                return_value=SimpleNamespace(status_code=0, content=b""),
            ),
        ):
            respuesta = app.test_client().get("/api/buses/t05")

        self.assertEqual(respuesta.status_code, 503)
        self.assertIn("error", respuesta.get_json())

    def test_api_buses_devuelve_varios_autobuses_con_identificadores_distintos(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0
        feed.header.timestamp = 1800000000
        ids = []
        for indice in range(3):
            entidad = feed.entity.add()
            entidad.id = "feed-vehicle-" + str(indice)
            ids.append(entidad.id)
            vehiculo = entidad.vehicle
            vehiculo.trip.route_id = route_id
            # Deliberadamente sin VehicleDescriptor.id ni label:
            # el identificador de FeedEntity debe mantenerlos separados.
            vehiculo.position.latitude = 41.0 + indice * 0.01
            vehiculo.position.longitude = -1.0 - indice * 0.01

        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        with (
            patch("app.obtener_estimaciones_tripupdates", return_value={}),
            patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed),
        ):
            respuesta = app.test_client().get("/api/buses/t05")

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertEqual(len(datos), 3)
        self.assertEqual({bus["id"] for bus in datos}, set(ids))
        self.assertEqual(len({bus["id"] for bus in datos}), 3)

    def test_api_buses_no_inventa_ocupacion_ni_velocidad_ausentes(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0  # FULL_DATASET
        feed.header.timestamp = 1800000000
        entidad = feed.entity.add()
        entidad.id = "vehiculo-sin-datos"
        vehiculo = entidad.vehicle
        vehiculo.trip.route_id = route_id
        vehiculo.vehicle.id = "id-vehiculo-sin-datos"
        vehiculo.position.latitude = 41.0
        vehiculo.position.longitude = -1.0

        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        with patch("app.obtener_estimaciones_tripupdates", return_value={}), \
             patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed):
            respuesta = app.test_client().get("/api/buses/t05")

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertEqual(len(datos), 1)
        self.assertEqual(datos[0]["ocupacion"], "Sin datos")
        self.assertIsNone(datos[0]["velocidad_kmh"])
        self.assertEqual(datos[0]["retraso"], "Sin datos")
        self.assertIsNone(datos[0]["retraso_segundos"])


class RetrasosGTFSRTTests(unittest.TestCase):
    def _horario(self, fecha, trip_ids):
        fecha_texto = fecha.strftime("%Y%m%d")
        registros = []
        for trip_id in trip_ids:
            registros.append({
                "trip_id": trip_id,
                "stop_id": "parada-prueba",
                "service_id": "servicio-prueba",
                "arrival_time": "10:00:00",
                "departure_time": "10:00:00",
                "route_id": "ruta-prueba",
            })
        return {
            "routes": {"ruta-prueba": {
                "short_name": "T05-01",
                "long_name": "Destino",
            }},
            "services": {"servicio-prueba": {
                "start_date": fecha_texto,
                "end_date": fecha_texto,
                "monday": "1", "tuesday": "1", "wednesday": "1",
                "thursday": "1", "friday": "1", "saturday": "1",
                "sunday": "1",
            }},
            "exceptions": [],
            "stop_times": registros,
        }

    def test_hora_absoluta_calcula_retraso_y_time_prevalece_sobre_delay(self):
        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        programada = datetime.combine(
            hoy, datetime.strptime("10:00", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        estimada = programada + timedelta(minutes=7)
        horario = self._horario(hoy, ["viaje-prueba"])

        instante, retraso = resolver_evento_horario_gtfs(
            horario,
            "viaje-prueba",
            "parada-prueba",
            tipo_evento="arrival",
            timestamp_evento=int(estimada.timestamp()),
            delay_evento=60,  # Si también hay time, manda la hora absoluta.
            fecha_servicio=hoy,
            ahora=programada.timestamp() - 3600,
        )

        self.assertEqual(instante, int(estimada.timestamp()))
        self.assertEqual(retraso, 420)

    def test_hora_absoluta_calcula_adelanto_con_signo_negativo(self):
        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        programada = datetime.combine(
            hoy, datetime.strptime("10:00", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        estimada = programada - timedelta(minutes=4)
        horario = self._horario(hoy, ["viaje-prueba"])

        instante, retraso = resolver_evento_horario_gtfs(
            horario,
            "viaje-prueba",
            "parada-prueba",
            timestamp_evento=int(estimada.timestamp()),
            fecha_servicio=hoy,
            ahora=programada.timestamp() - 3600,
        )

        self.assertEqual(instante, int(estimada.timestamp()))
        self.assertEqual(retraso, -240)

    def test_evento_solo_con_delay_reconstruye_la_hora_estimada(self):
        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        programada = datetime.combine(
            hoy, datetime.strptime("10:00", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        horario = self._horario(hoy, ["viaje-prueba"])

        instante, retraso = resolver_evento_horario_gtfs(
            horario,
            "viaje-prueba",
            "parada-prueba",
            tipo_evento="arrival",
            delay_evento=300,
            fecha_servicio=hoy,
            ahora=programada.timestamp() - 3600,
        )

        self.assertEqual(instante, int((programada + timedelta(minutes=5)).timestamp()))
        self.assertEqual(retraso, 300)

    def test_sin_estimacion_no_se_muestra_como_en_hora(self):
        self.assertEqual(formatear_retraso_gtfs(None), "Sin datos")
        self.assertEqual(formatear_retraso_gtfs(0), "En hora")
        self.assertEqual(formatear_retraso_gtfs(20), "En hora")
        self.assertEqual(formatear_retraso_gtfs(300), "+5 min retraso")
        self.assertEqual(formatear_retraso_gtfs(-180), "-3 min adelanto")

    def test_tripupdates_asigna_retraso_adelanto_y_sin_datos_a_cada_viaje(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        base = datetime.combine(
            hoy, datetime.strptime("08:00", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        ahora = base.timestamp()
        programada = datetime.combine(
            hoy, datetime.strptime("10:00", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        ids = ("viaje-tarde", "viaje-adelantado", "viaje-sin-horario")
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0
        feed.header.timestamp = int(ahora)

        for trip_id, delta_segundos in (
            ("viaje-tarde", 420),
            ("viaje-adelantado", -240),
            ("viaje-sin-horario", 600),
        ):
            entidad = feed.entity.add()
            entidad.id = "entidad-" + trip_id
            actualizacion = entidad.trip_update
            actualizacion.trip.route_id = route_id
            actualizacion.trip.trip_id = trip_id
            actualizacion.trip.start_date = hoy.strftime("%Y%m%d")
            actualizacion.vehicle.id = "bus-" + trip_id
            parada = actualizacion.stop_time_update.add()
            parada.stop_id = "parada-prueba"
            parada.arrival.time = int(
                (programada + timedelta(seconds=delta_segundos)).timestamp()
            )
            if trip_id == "viaje-tarde":
                parada.arrival.delay = 60  # La hora absoluta (7 min) prevalece.

        dataset = {
            "map": {"routes": {route_id: {
                "short_name": "T05-01",
                "long_name": "Origen - Destino",
            }}},
            "lookup": {"trips": {}},
            "schedule": self._horario(
                hoy, ["viaje-tarde", "viaje-adelantado"]
            ),
        }
        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        with (
            patch("app.obtener_dataset_gtfs", return_value=dataset),
            patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed),
            patch("app.time.time", return_value=ahora),
            patch("app.resolver_destino_y_parada", return_value=("Destino", "Próxima parada")),
        ):
            estimaciones = obtener_estimaciones_tripupdates("T05")

        self.assertEqual(estimaciones["bus-viaje-tarde"]["retraso"], "+7 min retraso")
        self.assertEqual(estimaciones["bus-viaje-adelantado"]["retraso"], "-4 min adelanto")
        self.assertEqual(estimaciones["bus-viaje-sin-horario"]["retraso"], "Sin datos")
        self.assertEqual(estimaciones["viaje-tarde"]["retraso_segundos"], 420)
        self.assertEqual(estimaciones["viaje-adelantado"]["retraso_segundos"], -240)

    def test_api_llegadas_marca_como_pasado_un_viaje_adelantado(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        ahora_dt = datetime.combine(
            hoy, datetime.strptime("08:10", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        ahora = ahora_dt.timestamp()
        salida_real = datetime.combine(
            hoy, datetime.strptime("08:05", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0
        feed.header.timestamp = int(ahora)
        entidad = feed.entity.add()
        entidad.id = "entidad-viaje-adelantado"
        actualizacion = entidad.trip_update
        actualizacion.trip.route_id = route_id
        actualizacion.trip.trip_id = "viaje-adelantado"
        actualizacion.trip.start_date = hoy.strftime("%Y%m%d")
        parada = actualizacion.stop_time_update.add()
        parada.stop_id = "parada-prueba"
        # La salida real prevalece sobre arrival para la tabla de salidas.
        parada.arrival.time = int(datetime.combine(
            hoy, datetime.strptime("08:04", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        ).timestamp())
        parada.departure.time = int(salida_real.timestamp())

        horario = self._horario(hoy, ["viaje-adelantado"])
        horario["stop_times"][0]["arrival_time"] = "08:15:00"
        horario["stop_times"][0]["departure_time"] = "08:15:00"
        dataset = {
            "map": {
                "routes": {route_id: {
                    "short_name": "T05-01",
                    "long_name": "Origen - Destino",
                }},
                "stops": {"T05-01": [{"id": "parada-prueba"}]},
            },
            "lookup": {"trips": {}},
            "schedule": horario,
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        with (
            patch("app.obtener_dataset_gtfs", return_value=dataset),
            patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed),
            patch("app.time.time", return_value=ahora),
            patch("app.resolver_destino_y_parada", return_value=("Destino", "Parada")),
        ):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/llegadas?concesion=T05"
            )

        self.assertEqual(respuesta.status_code, 200)
        llegadas = respuesta.get_json()["llegadas"]
        self.assertEqual(len(llegadas), 1)
        self.assertTrue(llegadas[0]["pasado"])
        self.assertEqual(llegadas[0]["timestamp"], int(salida_real.timestamp()))
        self.assertEqual(llegadas[0]["retraso_segundos"], -600)

    def test_api_llegadas_reconstruye_hora_si_el_feed_solo_envia_delay(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        ahora_dt = datetime.combine(
            hoy, datetime.strptime("08:00", "%H:%M").time(),
            tzinfo=ZoneInfo("Europe/Madrid")
        )
        ahora = ahora_dt.timestamp()
        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0
        feed.header.timestamp = int(ahora)
        entidad = feed.entity.add()
        entidad.id = "entidad-delay-solo"
        actualizacion = entidad.trip_update
        actualizacion.trip.route_id = route_id
        actualizacion.trip.trip_id = "viaje-delay-solo"
        actualizacion.trip.start_date = hoy.strftime("%Y%m%d")
        parada = actualizacion.stop_time_update.add()
        parada.stop_id = "parada-prueba"
        parada.arrival.delay = 300

        dataset = {
            "map": {
                "routes": {route_id: {
                    "short_name": "T05-01",
                    "long_name": "Origen - Destino",
                }},
                "stops": {"T05-01": [{"id": "parada-prueba"}]},
            },
            "lookup": {"trips": {}},
            "schedule": self._horario(hoy, ["viaje-delay-solo"]),
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        with (
            patch("app.obtener_dataset_gtfs", return_value=dataset),
            patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed),
            patch("app.time.time", return_value=ahora),
            patch("app.resolver_destino_y_parada", return_value=("Destino", "Próxima parada")),
        ):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/llegadas?concesion=T05"
            )

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertEqual(len(datos["llegadas"]), 1)
        self.assertEqual(datos["llegadas"][0]["retraso_segundos"], 300)
        self.assertEqual(
            datos["llegadas"][0]["timestamp"],
            int((ahora_dt.replace(hour=10) + timedelta(minutes=5)).timestamp()),
        )


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
        fecha_inicio = datetime.now(ZoneInfo("Europe/Madrid")).date() + timedelta(days=1)
        horas = []
        # Busca el siguiente día con servicio: el test no debe depender de que
        # se ejecute exactamente un día de la semana concreto.
        for desplazamiento in range(14):
            fecha = fecha_inicio + timedelta(days=desplazamiento)
            respuesta = self.client.get(
                "/api/paradas/" + parada["id"] + "/horarios"
                "?concesion=T04&fecha=" + fecha.isoformat() + "&stop_ids=" + ids
            )
            self.assertEqual(respuesta.status_code, 200)
            horas = [item["hora"] for item in respuesta.get_json()["horarios"]]
            if "08:13" in horas:
                break
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

    def test_calendario_malformado_no_rompe_la_consulta_de_horarios(self):
        horario = {
            "services": {
                "servicio-malformado": {
                    "start_date": "fecha-invalida",
                    "end_date": "20261231",
                    "monday": "1",
                }
            },
            "exceptions": [],
        }
        self.assertFalse(
            servicio_activo_gtfs(
                "servicio-malformado", date(2026, 11, 3), horario
            )
        )

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

    def test_api_de_llegadas_t05_devuelve_mas_de_ocho_autobuses_simultaneos(self):
        from types import SimpleNamespace
        from google.transit import gtfs_realtime_pb2

        route_id = next(iter(GTFS_DATASETS["T05"]["map"]["routes"]))
        ahora = 1800000000
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = 0
        feed.header.timestamp = ahora
        identificadores = []
        for indice in range(12):
            entidad = feed.entity.add()
            entidad.id = "entidad-" + str(indice)
            identificadores.append("viaje-" + str(indice))
            actualizacion = entidad.trip_update
            actualizacion.trip.route_id = route_id
            actualizacion.trip.trip_id = identificadores[-1]
            parada = actualizacion.stop_time_update.add()
            parada.stop_id = (
                "parada-prueba" if indice % 2 == 0 else "parada-secundaria"
            )
            parada.arrival.time = ahora + (indice + 1) * 60

        dataset = {
            "map": {
                "routes": {route_id: {"short_name": "T05-01", "long_name": "Zaragoza - Fraga"}},
                "stops": {"T05-01": [{
                    "id": "parada-prueba",
                    "stop_ids": ["parada-prueba", "parada-secundaria"],
                }]},
            },
            "lookup": {"stops": {}, "trips": {}},
            "schedule": {"routes": {}, "services": {}, "exceptions": [], "stop_times": []},
            "has_map": True,
            "has_schedules": True,
            "has_realtime": True,
        }
        respuesta_feed = SimpleNamespace(
            status_code=200,
            content=feed.SerializeToString(),
        )
        with (
            patch("app.obtener_dataset_gtfs", return_value=dataset),
            patch("app.obtener_respuesta_cacheada", return_value=respuesta_feed),
            patch("app.time.time", return_value=ahora),
        ):
            respuesta = app.test_client().get(
                "/api/paradas/parada-prueba/llegadas?concesion=T05"
                "&stop_ids=parada-prueba,parada-secundaria"
            )

        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.get_json()
        self.assertTrue(datos["tiempo_real_disponible"])
        self.assertEqual(len(datos["llegadas"]), 12)
        self.assertEqual(
            {llegada["trip_id"] for llegada in datos["llegadas"]},
            set(identificadores),
        )
        self.assertTrue(all(llegada["minutos"] >= 0 for llegada in datos["llegadas"]))

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
        self.assertTrue(all(llamada.args[2] == 5 for llamada in consulta.call_args_list))



class CacheGTFSRTTests(unittest.TestCase):
    def test_predicciones_de_autobuses_usan_cache_tripupdates_de_cinco_segundos(self):
        codigo = inspect.getsource(obtener_estimaciones_tripupdates)
        self.assertIn(
            'obtener_respuesta_cacheada(url, "tripupdates", 5, headers, 8)',
            codigo,
        )

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

    def test_un_fallo_de_feed_se_cachea_brevemente_para_evitar_timeouts_en_cola(self):
        url = "https://test.local/feed-fallido-cacheado"
        clave = ("unit-test", url)
        CACHE_RESPUESTAS_GTFSRT.pop(clave, None)

        with patch("app.session.get", side_effect=__import__("requests").Timeout("sin respuesta")) as get:
            primera = obtener_respuesta_cacheada(url, "unit-test", 60, {}, 1)
            segunda = obtener_respuesta_cacheada(url, "unit-test", 60, {}, 1)

        self.assertEqual(primera.status_code, 0)
        self.assertEqual(segunda.status_code, 0)
        self.assertEqual(get.call_count, 1)
        CACHE_RESPUESTAS_GTFSRT.pop(clave, None)

    def test_descargas_de_urls_distintas_no_se_bloquean_entre_si(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        barrera = Barrier(2)
        urls = [
            "https://test.local/feed-concurrente-1",
            "https://test.local/feed-concurrente-2",
        ]
        for url in urls:
            CACHE_RESPUESTAS_GTFSRT.pop(("unit-test", url), None)

        respuesta_falsa = Mock(status_code=200, content=b"feed")

        def obtener_simultaneamente(*args, **kwargs):
            barrera.wait(timeout=2)
            return respuesta_falsa

        with patch("app.session.get", side_effect=obtener_simultaneamente) as get:
            with ThreadPoolExecutor(max_workers=2) as pool:
                resultados = list(pool.map(
                    lambda url: obtener_respuesta_cacheada(
                        url, "unit-test", 60, {}, 1
                    ),
                    urls,
                ))

        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(resultado.status_code == 200 for resultado in resultados))
        for url in urls:
            CACHE_RESPUESTAS_GTFSRT.pop(("unit-test", url), None)


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


    def test_boton_proximo_servicio_detiene_el_clic_para_mantener_abierto_el_popup(self):
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("L.DomEvent.stop(evento);", html)
        self.assertIn("cargarLlegadasParada(stopId, popup, fechaSiguiente, stopIds);", html)
        self.assertIn("selectorFecha.value = fechaSiguiente;", html)

    def test_horarios_de_otras_fechas_no_consultan_ni_mezclan_tiempo_real(self):
        app_source = open("app.py", encoding="utf-8").read()
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("fecha === fechaLocalISO()", html)
        self.assertIn("consultarTiempoReal", html)
        self.assertIn("const tiempoReal = consultarTiempoReal &&", html)
        self.assertIn("item.pasado && item.trip_id", html)
        self.assertIn("viajesPasados.has(String(item.trip_id))", html)
        self.assertIn("mapa-tpa:viajes-pasados:", html)
        self.assertIn("const tiempoRealPendiente = tiempoReal.filter(item => !item.pasado)", html)
        self.assertIn("const segundosRestantes = Number(item.timestampMostrado) - ahoraTimestamp", html)
        self.assertIn("item.mostrarCuentaAtras = segundosRestantes < 30 * 60", html)
        self.assertIn("'En ' + item.minutosRestantes + ' min' : 'Llegando'", html)
        self.assertIn("escaparHTML(item.tiempoPrincipal)", html)
        self.assertIn("popup._llegadaInminente = consultarTiempoReal && elementos.some", html)
        self.assertIn("const prediccionesAsignadas = new Set()", html)
        self.assertIn("prediccionesPorProgramada.set(indice, prediccion)", html)
        self.assertIn("const toleraDestinoDistinto = mismaLinea === 'T05-20' && mismoSentido;", html)
        self.assertIn("if (!mismoDestino && !toleraDestinoDistinto) return;", html)
        self.assertIn("const margenEmparejamiento = toleraDestinoDistinto ? 60 * 60 : 20 * 60;", html)
        self.assertIn("diferenciaSegundos > margenEmparejamiento", html)
        self.assertIn('"direccion": str(direccion) if direccion is not None else ""', app_source)
        self.assertIn("evento.popup._llegadaInminente ? 5000 : 30000", html)
        self.assertNotIn("Tiempo real consultado a las", html)
        self.assertNotIn("En tiempo real = predicción GTFS-RT", html)
        self.assertNotIn("Horario GTFS disponible hasta", html)
        self.assertNotIn("stop-schedule-note", html)
        self.assertIn("estado: prediccion ? 'Tiempo real' : 'Programado'", html)
        self.assertIn("En tiempo real", html)
        self.assertIn("escaparHTML(esTiempoReal ? 'En tiempo real' : 'Programado')", html)
        self.assertIn(": 'Horario programado';", html)
        self.assertNotIn("esTiempoReal ? 'En tiempo real' : 'Oficial'", html)
        self.assertNotIn("En vivo", html[html.index("const gruposOrdenados = Array.from(grupos.values())"):html.index("const resumenSalidas")])
        self.assertNotIn("diferencia", html[html.index("const gruposOrdenados = Array.from(grupos.values())"):html.index("const resumenSalidas")])
        self.assertNotIn("Otra fecha: se muestra el horario oficial", html)
        self.assertIn("popup._solicitudLlegadasParada", html)

    def test_el_mapa_procesa_todos_los_autobuses_devuelto_por_la_api(self):
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("if (!respuesta.ok) throw new Error", html)
        self.assertIn("const retraso = bus.retraso || \"Sin datos\";", html)
        self.assertIn("marcador.setOpacity(0.45)", html)
        self.assertIn("marcadoresBuses[idUnico].setOpacity(1)", html)
        self.assertIn("últimas conocidas", html)
        self.assertIn("datos.forEach((bus)", html)
        self.assertIn("idsVivos.add(idUnico)", html)
        self.assertIn("marcadoresBuses[idUnico]", html)

    def test_popup_de_autobus_muestra_velocidad_y_distingue_sin_dato(self):
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("bus.velocidad_kmh", html)
        self.assertIn("Velocidad:</b>", html)
        self.assertIn("Sin dato", html)

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
