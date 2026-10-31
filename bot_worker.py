"""Worker de Telegram para avisar cuando aparece un servicio T05 en GTFS-RT.

Variables de entorno:
  TELEGRAM_BOT_TOKEN  Token creado con @BotFather (obligatorio).
  BOT_POLL_INTERVAL   Intervalo entre lecturas GTFS-RT (segundos, por defecto 15).
  BOT_STATE_DB        SQLite de estado; en Render, usar /var/data/... con disco persistente.
"""
from __future__ import annotations

import html
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

import app as mapa


LOG = logging.getLogger("mapa_tpa_telegram")
ZONA_HORARIA = ZoneInfo("Europe/Madrid")
INTERVALO_POR_DEFECTO = 15
GAP_NUEVO_SERVICIO = 15 * 60
TIMEOUT_TELEGRAM = 12


def ruta_base_datos() -> str:
    configurada = os.environ.get("BOT_STATE_DB")
    if configurada:
        return configurada
    if Path("/var/data").is_dir():
        return "/var/data/mapa-tpa-telegram.sqlite3"
    return str(Path(__file__).resolve().parent / "data" / "telegram_bot.sqlite3")


def abrir_base_datos(ruta: str | None = None) -> sqlite3.Connection:
    ubicacion = ruta or ruta_base_datos()
    Path(ubicacion).parent.mkdir(parents=True, exist_ok=True)
    conexion = sqlite3.connect(ubicacion, timeout=20)
    conexion.row_factory = sqlite3.Row
    conexion.execute("PRAGMA journal_mode=WAL")
    conexion.execute("PRAGMA busy_timeout=20000")
    conexion.executescript(
        """
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS servicios (
            clave TEXT PRIMARY KEY,
            linea TEXT NOT NULL,
            coche TEXT NOT NULL,
            matricula TEXT NOT NULL,
            primera_deteccion INTEGER NOT NULL,
            ultima_deteccion INTEGER NOT NULL,
            notificado INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conexion.commit()
    return conexion


def obtener_config(conexion: sqlite3.Connection, clave: str) -> str | None:
    fila = conexion.execute("SELECT valor FROM config WHERE clave = ?", (clave,)).fetchone()
    return fila["valor"] if fila else None


def guardar_config(conexion: sqlite3.Connection, clave: str, valor: str | None) -> None:
    if valor is None:
        conexion.execute("DELETE FROM config WHERE clave = ?", (clave,))
    else:
        conexion.execute(
            "INSERT INTO config(clave, valor) VALUES(?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, str(valor)),
        )
    conexion.commit()


def clave_servicio(servicio: dict) -> str:
    """Prioriza trip_id; si falta, usa vehículo + ruta."""
    trip_id = str(servicio.get("trip_id") or "").strip()
    if trip_id:
        return "trip:" + trip_id

    vehiculo = str(servicio.get("vehicle_id") or servicio.get("coche") or "sin-vehiculo")
    ruta = str(servicio.get("route_id") or servicio.get("linea") or "sin-ruta")
    return f"vehiculo:{ruta}:{vehiculo}"


def formatear_mensaje(servicio: dict, instante: int | None = None) -> str:
    detectado = datetime.fromtimestamp(instante or int(time.time()), ZONA_HORARIA)
    linea = html.escape(str(servicio.get("linea") or "T05"))
    coche = html.escape(str(servicio.get("coche") or "No identificado"))
    matricula = html.escape(str(servicio.get("matricula") or "No facilitada por el feed"))
    return (
        "🚌 <b>Nuevo servicio localizado</b>\n"
        f"<b>Línea:</b> {linea}\n"
        f"<b>Número de coche:</b> {coche}\n"
        f"<b>Matrícula:</b> {matricula}\n"
        f"<b>Detectado:</b> {detectado.strftime('%d/%m/%Y %H:%M:%S')}"
    )


def procesar_servicios_nuevos(
    conexion: sqlite3.Connection,
    servicios: list[dict],
    chat_id: str,
    enviar_mensaje,
    ahora: int | None = None,
) -> int:
    """Envía una sola notificación por aparición de servicio y deduplica reinicios."""
    instante = int(ahora or time.time())
    enviados = 0

    for servicio in servicios:
        clave = clave_servicio(servicio)
        fila = conexion.execute(
            "SELECT * FROM servicios WHERE clave = ?", (clave,)
        ).fetchone()

        if fila is None:
            primera_deteccion = instante
            notificado = 0
            conexion.execute(
                """
                INSERT INTO servicios
                    (clave, linea, coche, matricula, primera_deteccion, ultima_deteccion, notificado)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    clave,
                    str(servicio.get("linea") or "T05"),
                    str(servicio.get("coche") or "No identificado"),
                    str(servicio.get("matricula") or ""),
                    primera_deteccion,
                    instante,
                ),
            )
        else:
            separacion = instante - int(fila["ultima_deteccion"])
            if separacion >= GAP_NUEVO_SERVICIO:
                primera_deteccion = instante
                notificado = 0
            else:
                primera_deteccion = int(fila["primera_deteccion"])
                notificado = int(fila["notificado"])

            conexion.execute(
                """
                UPDATE servicios
                   SET linea = ?, coche = ?, matricula = ?,
                       primera_deteccion = ?, ultima_deteccion = ?, notificado = ?
                 WHERE clave = ?
                """,
                (
                    str(servicio.get("linea") or fila["linea"]),
                    str(servicio.get("coche") or fila["coche"]),
                    str(servicio.get("matricula") or fila["matricula"]),
                    primera_deteccion,
                    instante,
                    notificado,
                    clave,
                ),
            )

        conexion.commit()
        if not notificado:
            mensaje = formatear_mensaje(servicio, primera_deteccion)
            try:
                if enviar_mensaje(chat_id, mensaje):
                    conexion.execute(
                        "UPDATE servicios SET notificado = 1 WHERE clave = ?", (clave,)
                    )
                    conexion.commit()
                    enviados += 1
            except Exception:
                LOG.exception("No se pudo enviar el aviso del servicio %s", clave)

    # Mantiene el registro acotado sin borrar servicios recientes.
    conexion.execute("DELETE FROM servicios WHERE ultima_deteccion < ?", (instante - 30 * 86400,))
    conexion.commit()
    return enviados


def obtener_servicios_t05() -> list[dict] | None:
    """Lee el feed de vehículos; None significa que fallaron todos los proveedores."""
    cabeceras = {
        "User-Agent": "Mozilla/5.0 (compatible; MapaTPA-Telegram/1.0)",
        "Accept": "*/*",
    }

    for url in mapa.VEHICLES_ENDPOINTS:
        try:
            respuesta = mapa.obtener_respuesta_cacheada(
                url, "bot-vehiclepositions", 5, cabeceras, 8
            )
            if respuesta.status_code != 200 or not respuesta.content:
                continue

            contenido = respuesta.content
            if contenido.startswith(b"\x1f\x8b"):
                try:
                    import gzip
                    contenido = gzip.decompress(contenido)
                except (OSError, EOFError):
                    LOG.exception("Feed gzip no válido: %s", url)
                    continue

            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(contenido)
            servicios: dict[str, dict] = {}

            for entidad in feed.entity:
                if not entidad.HasField("vehicle"):
                    continue

                vehiculo = entidad.vehicle
                if not vehiculo.HasField("trip"):
                    continue

                viaje = vehiculo.trip
                route_id = str(viaje.route_id or "")
                linea = mapa.T05_ROUTE_NAMES.get(route_id)
                if not linea:
                    continue

                posicion = vehiculo.position if vehiculo.HasField("position") else None
                if posicion is None:
                    continue
                if mapa.validar_coordenadas(posicion.latitude, posicion.longitude) is None:
                    continue

                descriptor = vehiculo.vehicle if vehiculo.HasField("vehicle") else None
                coche = str(getattr(descriptor, "label", "") or "") if descriptor else ""
                vehicle_id = str(getattr(descriptor, "id", "") or "") if descriptor else ""
                matricula = str(getattr(descriptor, "license_plate", "") or "") if descriptor else ""
                trip_id = str(viaje.trip_id or "")

                servicio = {
                    "linea": linea,
                    "route_id": route_id,
                    "trip_id": trip_id,
                    "vehicle_id": vehicle_id,
                    "coche": coche or vehicle_id or "No identificado",
                    "matricula": matricula,
                    "lat": float(posicion.latitude),
                    "lon": float(posicion.longitude),
                }
                servicios[clave_servicio(servicio)] = servicio

            return list(servicios.values())

        except (requests.RequestException, ValueError, Exception) as exc:
            LOG.warning("No se pudo leer el feed de vehículos %s: %s", url, exc)

    return None


class TelegramBot:
    def __init__(self, token: str, conexion: sqlite3.Connection):
        self.token = token
        self.conexion = conexion
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = int(self._meta("telegram_offset") or 0)
        self.username = ""

    def _meta(self, clave: str) -> str | None:
        fila = self.conexion.execute("SELECT valor FROM meta WHERE clave = ?", (clave,)).fetchone()
        return fila["valor"] if fila else None

    def _guardar_meta(self, clave: str, valor: str) -> None:
        self.conexion.execute(
            "INSERT INTO meta(clave, valor) VALUES(?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, str(valor)),
        )
        self.conexion.commit()

    def llamar(self, metodo: str, datos: dict | None = None) -> dict | None:
        try:
            respuesta = requests.post(
                f"{self.base_url}/{metodo}",
                json=datos or {},
                timeout=TIMEOUT_TELEGRAM,
            )
            respuesta.raise_for_status()
            payload = respuesta.json()
            if not payload.get("ok"):
                LOG.warning("Telegram %s respondió con error: %s", metodo, payload.get("description"))
                return None
            return payload.get("result")
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Error llamando a Telegram %s: %s", metodo, exc)
            return None

    def enviar(self, chat_id: str, texto: str) -> bool:
        resultado = self.llamar(
            "sendMessage",
            {"chat_id": chat_id, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        return resultado is not None

    def procesar_comando(self, mensaje: dict) -> None:
        chat = mensaje.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        tipo_chat = chat.get("type")
        usuario = mensaje.get("from") or {}
        texto = str(mensaje.get("text") or "").strip()
        if not texto.startswith("/"):
            return

        comando = texto.split()[0].split("@")[0].lower()
        if comando in {"/start", "/ayuda", "/help"}:
            self.enviar(
                chat_id,
                "🤖 <b>Bot de Mapa TPA</b>\\n"
                "Aviso automático de servicios T05 localizados.\\n\\n"
                "<code>/vincular</code> — vincular este grupo.\\n"
                "<code>/estado</code> — comprobar la vinculación.\\n"
                "<code>/prueba</code> — enviar un mensaje de prueba.\\n"
                "<code>/desvincular</code> — detener los avisos en este grupo.",
            )
            return

        if comando == "/estado":
            destino = obtener_config(self.conexion, "chat_id")
            estado = "Este grupo está vinculado." if destino == chat_id else "Este grupo no está vinculado."
            self.enviar(chat_id, "ℹ️ " + estado)
            return

        if comando not in {"/vincular", "/desvincular", "/prueba"}:
            return

        if tipo_chat not in {"group", "supergroup"}:
            self.enviar(chat_id, "Añádeme a un grupo y ejecuta este comando allí.")
            return

        user_id = usuario.get("id")
        if not user_id:
            self.enviar(chat_id, "No he podido identificar al usuario que ejecuta el comando.")
            return

        destino_actual = obtener_config(self.conexion, "chat_id")
        responsable = obtener_config(self.conexion, "owner_user_id")

        if comando == "/vincular":
            if destino_actual and destino_actual != chat_id:
                self.enviar(
                    chat_id,
                    "Ya hay otro grupo vinculado. El responsable actual debe ejecutar /desvincular allí antes de cambiarlo.",
                )
                return
            if destino_actual == chat_id:
                self.enviar(chat_id, "ℹ️ Este grupo ya está vinculado.")
                return

            guardar_config(self.conexion, "chat_id", chat_id)
            guardar_config(self.conexion, "owner_user_id", str(user_id))
            self.enviar(
                chat_id,
                "✅ <b>Grupo vinculado correctamente.</b>\\n"
                "A partir de ahora avisaré una vez cuando detecte un nuevo servicio T05.\\n"
                "Quien ha ejecutado /vincular queda como responsable para desvincularlo.",
            )
        elif comando == "/desvincular":
            if destino_actual != chat_id:
                self.enviar(chat_id, "Este grupo no está vinculado.")
                return
            if str(user_id) != responsable:
                self.enviar(chat_id, "Solo la persona responsable que vinculó este grupo puede desvincularlo.")
                return

            guardar_config(self.conexion, "chat_id", None)
            guardar_config(self.conexion, "owner_user_id", None)
            self.enviar(chat_id, "🔕 Grupo desvinculado. Ya no se enviarán avisos aquí.")
        elif comando == "/prueba":
            if destino_actual != chat_id:
                self.enviar(chat_id, "Primero vincula este grupo con /vincular.")
                return
            self.enviar(chat_id, "✅ <b>Mensaje de prueba de Mapa TPA.</b> La conexión con Telegram funciona.")

    def recibir_comandos(self) -> None:
        actualizaciones = self.llamar(
            "getUpdates",
            {
                "offset": self.offset,
                "timeout": 0,
                "allowed_updates": ["message"],
            },
        )
        if not isinstance(actualizaciones, list):
            return

        for actualizacion in actualizaciones:
            update_id = int(actualizacion.get("update_id", 0))
            self.offset = max(self.offset, update_id + 1)
            mensaje = actualizacion.get("message")
            if mensaje:
                try:
                    self.procesar_comando(mensaje)
                except Exception:
                    LOG.exception("Error procesando comando de Telegram")
        self._guardar_meta("telegram_offset", str(self.offset))

    def preparar(self) -> bool:
        identidad = self.llamar("getMe")
        if not identidad:
            return False
        self.username = str(identidad.get("username") or "")
        # Este worker utiliza long polling; elimina un webhook previo si existiera.
        self.llamar("deleteWebhook", {"drop_pending_updates": False})
        self.llamar(
            "setMyCommands",
            {
                "commands": [
                    {"command": "vincular", "description": "Vincular este grupo"},
                    {"command": "estado", "description": "Comprobar el estado del grupo"},
                    {"command": "prueba", "description": "Enviar un mensaje de prueba"},
                    {"command": "desvincular", "description": "Detener los avisos en este grupo"},
                    {"command": "ayuda", "description": "Mostrar ayuda"},
                ]
            },
        )
        LOG.info("Bot conectado como @%s", self.username or "sin-username")
        return True


def ejecutar() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        LOG.error("Falta TELEGRAM_BOT_TOKEN. Crea el bot con @BotFather y configura la variable.")
        raise SystemExit(2)

    intervalo = max(5, int(os.environ.get("BOT_POLL_INTERVAL", INTERVALO_POR_DEFECTO)))
    conexion = abrir_base_datos()
    bot = TelegramBot(token, conexion)
    if not bot.preparar():
        raise SystemExit("No se pudo autenticar el bot con Telegram.")

    proxima_lectura = 0.0
    LOG.info("Worker activo. Intervalo de lectura GTFS-RT: %s s", intervalo)

    try:
        while True:
            bot.recibir_comandos()
            ahora_monotonic = time.monotonic()

            if ahora_monotonic >= proxima_lectura:
                chat_id = obtener_config(conexion, "chat_id")
                if chat_id:
                    servicios = obtener_servicios_t05()
                    if servicios is None:
                        LOG.warning("Los feeds GTFS-RT no respondieron; se reintentará.")
                    else:
                        enviados = procesar_servicios_nuevos(
                            conexion, servicios, chat_id, bot.enviar
                        )
                        if enviados:
                            LOG.info("Avisos nuevos enviados: %s", enviados)
                proxima_lectura = ahora_monotonic + intervalo

            time.sleep(2)
    except KeyboardInterrupt:
        LOG.info("Worker detenido.")
    finally:
        conexion.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ejecutar()
