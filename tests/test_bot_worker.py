import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot_worker import (
    GAP_NUEVO_SERVICIO,
    abrir_base_datos,
    clave_servicio,
    formatear_mensaje,
    procesar_servicios_nuevos,
)


class TelegramBotServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = abrir_base_datos(":memory:")
        self.servicio = {
            "linea": "T05-01",
            "route_id": "ruta-t05-01",
            "trip_id": "viaje-123",
            "vehicle_id": "vehiculo-13444",
            "coche": "13444",
            "matricula": "2563MSV",
        }

    def tearDown(self):
        self.db.close()

    def test_prioriza_trip_id_para_identificar_un_servicio(self):
        self.assertEqual(clave_servicio(self.servicio), "trip:viaje-123")

    def test_fallback_usa_linea_y_vehiculo_si_no_hay_trip_id(self):
        servicio = {**self.servicio, "trip_id": ""}
        self.assertEqual(clave_servicio(servicio), "vehiculo:ruta-t05-01:vehiculo-13444")

    def test_formatea_linea_coche_matricula_y_hora_local(self):
        instante = int(datetime(2026, 10, 31, 14, 48, tzinfo=ZoneInfo("Europe/Madrid")).timestamp())
        mensaje = formatear_mensaje(self.servicio, instante)
        self.assertIn("T05-01", mensaje)
        self.assertIn("13444", mensaje)
        self.assertIn("2563MSV", mensaje)
        self.assertIn("31/10/2026 14:48:00", mensaje)

    def test_no_repite_avisos_en_las_actualizaciones_gps(self):
        mensajes = []
        enviar = lambda chat_id, texto: mensajes.append((chat_id, texto)) or True

        primero = procesar_servicios_nuevos(self.db, [self.servicio], "-100123", enviar, ahora=1000)
        segundo = procesar_servicios_nuevos(self.db, [self.servicio], "-100123", enviar, ahora=1020)

        self.assertEqual(primero, 1)
        self.assertEqual(segundo, 0)
        self.assertEqual(len(mensajes), 1)

    def test_una_reaparicion_tras_quince_minutos_es_otro_servicio(self):
        mensajes = []
        enviar = lambda chat_id, texto: mensajes.append(texto) or True

        procesar_servicios_nuevos(self.db, [self.servicio], "-100123", enviar, ahora=1000)
        enviados = procesar_servicios_nuevos(
            self.db, [self.servicio], "-100123", enviar,
            ahora=1000 + GAP_NUEVO_SERVICIO + 1
        )

        self.assertEqual(enviados, 1)
        self.assertEqual(len(mensajes), 2)

    def test_reintenta_si_telegram_no_acepto_el_primer_envio(self):
        resultados = iter([False, True])
        mensajes = []
        enviar = lambda chat_id, texto: mensajes.append(texto) or next(resultados)

        primero = procesar_servicios_nuevos(self.db, [self.servicio], "-100123", enviar, ahora=1000)
        segundo = procesar_servicios_nuevos(self.db, [self.servicio], "-100123", enviar, ahora=1020)

        self.assertEqual(primero, 0)
        self.assertEqual(segundo, 1)
        self.assertEqual(len(mensajes), 2)

    def test_vincular_guarda_grupo_y_responsable_sin_dar_admin_al_bot(self):
        from bot_worker import TelegramBot, obtener_config

        bot = TelegramBot("token-de-prueba", self.db)
        mensajes = []
        bot.enviar = lambda chat_id, texto: mensajes.append((chat_id, texto)) or True
        bot.procesar_comando({
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 456},
            "text": "/vincular@MapaTPAServiciosBot"
        })

        self.assertEqual(obtener_config(self.db, "chat_id"), "-100123")
        self.assertEqual(obtener_config(self.db, "owner_user_id"), "456")
        self.assertIn("vinculado", mensajes[-1][1].lower())

    def test_solo_el_responsable_puede_desvincular(self):
        from bot_worker import TelegramBot, obtener_config, guardar_config

        bot = TelegramBot("token-de-prueba", self.db)
        mensajes = []
        bot.enviar = lambda chat_id, texto: mensajes.append((chat_id, texto)) or True
        guardar_config(self.db, "chat_id", "-100123")
        guardar_config(self.db, "owner_user_id", "456")

        bot.procesar_comando({
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 999},
            "text": "/desvincular@MapaTPAServiciosBot"
        })
        self.assertEqual(obtener_config(self.db, "chat_id"), "-100123")
        self.assertIn("responsable", mensajes[-1][1].lower())

        bot.procesar_comando({
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 456},
            "text": "/desvincular@MapaTPAServiciosBot"
        })
        self.assertIsNone(obtener_config(self.db, "chat_id"))
        self.assertIsNone(obtener_config(self.db, "owner_user_id"))


if __name__ == "__main__":
    unittest.main()
