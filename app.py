import os
import fcntl
import html
try:
    import redis
except ImportError:
    redis = None
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
# Desactivar extensión C de Protobuf para compatibilidad con Python 3.11+
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

from flask import Flask, render_template, jsonify, request, session as flask_session
import hmac
from email.message import EmailMessage
import requests
import urllib3
import gzip
import time
import ssl
import math
import re
import smtplib
import unicodedata
import base64
import json
from requests.adapters import HTTPAdapter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import threading
import uuid
from types import SimpleNamespace
from urllib3.poolmanager import PoolManager

# Desactivar avisos de certificados SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
# Acceso básico al listado privado de Avanza. En Render se pueden sustituir
# estos valores mediante variables de entorno sin cambiar el código.
AVANZA_LOGIN_USER = os.environ.get("AVANZA_LOGIN_USER", "user").strip() or "user"
AVANZA_LOGIN_PASSWORD = os.environ.get("AVANZA_LOGIN_PASSWORD", "busero").strip() or "busero"
# Las credenciales publicadas como acceso inicial siguen siendo válidas aunque
# Render tenga configurada una pareja antigua en sus variables de entorno.
AVANZA_LOGIN_CREDENCIALES_INICIALES = ("user", "busero")
# Define FLASK_SECRET_KEY en producción para firmar las sesiones con un secreto
# propio y estable entre reinicios y trabajadores.
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "mapa-tpa-avanza-session-key-configura-un-secreto-propio",
)
app.config.update({
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_SECURE": (
        os.environ.get("SESSION_COOKIE_SECURE", "1" if os.environ.get("RENDER") else "0")
        .strip().lower() in {"1", "true", "yes", "si"}
    ),
})
app.permanent_session_lifetime = timedelta(hours=12)
ZONA_HORARIA = ZoneInfo('Europe/Madrid')

# Datos estáticos desacoplados por concesión. Los ficheros T05 actuales
# conservan sus nombres; las nuevas concesiones usan la misma convención.
from gtfs_datasets import (
    CATALOGO_CONCESIONES,
    CONCESIONES_CON_TIEMPO_REAL,
    cargar_datasets_gtfs,
    construir_catalogo_publico,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
GTFS_DATASETS = cargar_datasets_gtfs(DATA_DIR)
GTFS_T05_MAP = GTFS_DATASETS["T05"]["map"]       # Compatibilidad con herramientas existentes.
GTFS_T05_LOOKUP = GTFS_DATASETS["T05"]["lookup"]
GTFS_T05_SCHEDULE = GTFS_DATASETS["T05"]["schedule"]
T05_ROUTE_NAMES = {
    route_id: info.get("short_name", "T05")
    for route_id, info in GTFS_T05_MAP.get("routes", {}).items()
}


def obtener_dataset_gtfs(concesion):
    """Devuelve los datos GTFS de una concesión o estructuras vacías seguras."""
    codigo = str(concesion or "").upper()
    return GTFS_DATASETS.get(codigo, {
        "map": {"routes": {}, "shapes": {}, "stops": {}},
        "lookup": {"stops": {}, "trips": {}},
        "schedule": {"routes": {}, "services": {}, "exceptions": [], "stop_times": []},
        "has_map": False,
        "has_schedules": False,
        "has_realtime": False,
    })

# Adaptador SSL permisivo
class LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.options |= 0x4
        except AttributeError:
            pass
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize, block=block, ssl_context=ctx
        )

session = requests.Session()
adapter = LegacySSLAdapter()
session.mount('https://', adapter)
session.mount('http://', adapter)

VEHICLES_ENDPOINTS = [
    "https://tpaits.gmv.com/gtfsrtapi/api/vehiclepositions",
    "https://sintram.consorciozaragoza.es/api/gtfs-rt/vehicle-positions"
]

TRIPUPDATES_ENDPOINTS = [
    "https://tpaits.gmv.com/gtfsrtapi/api/tripupdates/",
    "https://tpaits.gmv.com/gtfsrtapi/api/tripupdates",
    "https://sintram.consorciozaragoza.es/api/gtfs-rt/trip-updates"
]

# Fuentes RT configuradas explícitamente por concesión.
# El piloto actual (ALSA/T05) es el único que utiliza estos feeds.
FUENTES_GTFSRT_POR_CONCESION = {
    "T05": {
        "vehiclepositions": VEHICLES_ENDPOINTS,
        "tripupdates": TRIPUPDATES_ENDPOINTS,
    }
}

# Caché compartida por las rutas Flask dentro de cada proceso Gunicorn.
# Evita descargar los mismos feeds por cada navegador y cada parada abierta.
CACHE_RESPUESTAS_GTFSRT = {}
CACHE_GTFSRT_LOCK = threading.Lock()
CACHE_GTFSRT_LOCKS = {}


def obtener_respuesta_cacheada(url, familia, ttl_segundos, headers, timeout):
    """
    Reutiliza el feed durante su TTL sin bloquear las descargas de otras URLs.

    La sección global protege solo el diccionario; cada feed tiene su propio
    lock para evitar descargas duplicadas concurrentes del mismo proveedor.
    """
    clave = (familia, url)

    with CACHE_GTFSRT_LOCK:
        ahora_monotonic = time.monotonic()
        cacheada = CACHE_RESPUESTAS_GTFSRT.get(clave)
        if cacheada and ahora_monotonic - cacheada["timestamp"] < cacheada.get("ttl", ttl_segundos):
            return SimpleNamespace(
                status_code=cacheada["status_code"],
                content=cacheada["content"]
            )
        lock_feed = CACHE_GTFSRT_LOCKS.setdefault(clave, threading.Lock())

    with lock_feed:
        with CACHE_GTFSRT_LOCK:
            ahora_monotonic = time.monotonic()
            cacheada = CACHE_RESPUESTAS_GTFSRT.get(clave)
            if cacheada and ahora_monotonic - cacheada["timestamp"] < cacheada.get("ttl", ttl_segundos):
                return SimpleNamespace(
                    status_code=cacheada["status_code"],
                    content=cacheada["content"]
                )

        try:
            respuesta = session.get(url, headers=headers, timeout=timeout, verify=False)
            resultado = SimpleNamespace(
                status_code=respuesta.status_code,
                content=respuesta.content
            )
        except requests.RequestException as exc:
            print(f"[GTFS-RT {familia.upper()} ERROR {url}]: {exc}")
            resultado = SimpleNamespace(status_code=0, content=b"")

        # Los errores se conservan unos segundos para que muchas consultas
        # simultáneas no repitan el mismo timeout una detrás de otra.
        ttl_cache = (
            ttl_segundos
            if resultado.status_code == 200 and resultado.content
            else min(max(int(ttl_segundos), 1), 3)
        )
        with CACHE_GTFSRT_LOCK:
            CACHE_RESPUESTAS_GTFSRT[clave] = {
                "timestamp": time.monotonic(),
                "ttl": ttl_cache,
                "status_code": resultado.status_code,
                "content": resultado.content
            }

        return resultado


# --- LISTAS DE PARADAS ---

# T05-01: Fraga
PARADAS_FRAGA_A_ZARAGOZA = [
    "Fraga. Estación", "Candasnos. C/Zaragoza Nº83", "Peñalba",
    "Bujaraloz. Rte español (original)", "Venta de Santa Lucía",
    "Pina de Ebro. Plaza de España", "Osera de Ebro. C/ San Martín S/N",
    "Villafranca de Ebro. Avenida Zaragoza Nº 1", "Condado (El)",
    "Nuez de Ebro. Avenida Zaragoza, 1", "Alfajarín. Avenida Europa 127",
    "La Puebla Alfindén", "Lugarico de Cerdán", "Zaragoza. Estación Intermodal"
]

PARADAS_ZARAGOZA_A_FRAGA = [
    "Zaragoza. Estación Intermodal", "Lugarico de Cerdán", "La Puebla Alfindén",
    "Alfajarín. Avenida Europa 127", "Nuez de Ebro. Avenida Zaragoza, 1",
    "Condado (El)", "Villafranca de Ebro. Avenida Zaragoza Nº 1",
    "Osera de Ebro. C/ San Martín S/N", "Pina de Ebro. Plaza de España",
    "Venta de Santa Lucía", "Bujaraloz. Rte español (original)", "Peñalba",
    "Candasnos. C/Zaragoza Nº83", "Fraga. Estación"
]

# T05-20: Castejón de Monegros
PARADAS_CASTEJON_A_ZARAGOZA = [
    "Castejón de Monegros.", "La Almolda. C/ San Antonio", "Bujaraloz. Rte español",
    "Venta de Santa Lucía", "Pina de Ebro. Plaza de España", "Osera de Ebro. C/ San Martín S/N",
    "Villafranca de Ebro. Avenida Zaragoza Nº 1", "Condado (El)", "Nuez de Ebro. Avenida Zaragoza, 1",
    "Alfajarín. Avenida Europa 127", "La Puebla Alfindén", "Lugarico de Cerdán",
    "Av. De Cataluña N.º 159", "Av. Salvador Allende N.º 107", "Plaza Europa",
    "Zaragoza. Estación Intermodal"
]

PARADAS_ZARAGOZA_A_CASTEJON = [
    "Zaragoza. Estación Intermodal", "Av. Pablo Gargallo / Plaza Europa",
    "Av. Salvador Allende / San Juan De La Peña", "Av. De Cataluña N.º 188",
    "Lugarico de Cerdán", "La Puebla Alfindén", "Alfajarín. Avenida Europa 127",
    "Nuez de Ebro. Avenida Zaragoza, 1", "Condado (El)", "Villafranca de Ebro. Avenida Zaragoza Nº 1",
    "Osera de Ebro. C/ San Martín S/N", "Pina de Ebro. Plaza de España",
    "Venta de Santa Lucía", "Bujaraloz. Rte español", "La Almolda. C/ San Antonio",
    "Castejón de Monegros."
]

# --- DICCIONARIO ESTRUCTURADO DE CONCESIONES ---
CONCESIONES = {
    "T05": {
        "d3efc296-1916-41c3-90c2-166ff0e8e4fb": {
            "nombre": "Zaragoza - Fraga",
            "dir_0": {"destino": "Zaragoza", "paradas": PARADAS_FRAGA_A_ZARAGOZA},
            "dir_1": {"destino": "Fraga", "paradas": PARADAS_ZARAGOZA_A_FRAGA}
        },
        "61b2122d-39cc-44e6-89f0-3057b6528c54": {
            "nombre": "Castejón de Monegros - Zaragoza",
            "dir_0": {"destino": "Zaragoza", "paradas": PARADAS_CASTEJON_A_ZARAGOZA},
            "dir_1": {"destino": "Castejón de Monegros", "paradas": PARADAS_ZARAGOZA_A_CASTEJON}
        }
    }
    # Para añadir T01, T02, etc., basta con agregar sus claves aquí
}

TRADUCCION_OCUPACION = {
    'EMPTY': ('Vacío', 'green'),
    'MANY_SEATS_AVAILABLE': ('Asientos libres', 'green'),
    'FEW_SEATS_AVAILABLE': ('Pocos asientos', 'yellow'),
    'STANDING_ROOM_ONLY': ('Solo de pie', 'orange'),
    'CRUSHED_STANDING_ROOM_ONLY': ('Espacio muy reducido', 'red'),
    'FULL': ('Lleno', 'red'),
    'NOT_ACCEPTING_PASSENGERS': ('No admite pasajeros', 'red'),
    'NO_DATA_AVAILABLE': ('Sin datos', 'gray'),
    'NOT_BOARDABLE': ('No admite viajeros', 'gray')
}

def validar_coordenadas(lat, lon):
    """Devuelve (lat, lon) si son coordenadas geográficas válidas; si no, None."""
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def resolver_destino_y_parada(concesion_key, route_id, direction_id, stop_sequence, stop_id=None, trip_id=None):
    """
    Busca de forma dinámica la línea dentro de la concesión activa.
    """
    r_id = str(route_id).lower() if route_id else ""
    dir_key = f"dir_{direction_id}" if direction_id in [0, 1, "0", "1"] else "dir_1"

    destino = "En trayecto"
    paradas = []

    concesion_data = CONCESIONES.get(concesion_key, {})
    
    # Buscar coincidencia exacta o por subcadena del ID de ruta
    for rid, info in concesion_data.items():
        if rid.lower() in r_id or r_id in rid.lower():
            if dir_key in info:
                destino = info[dir_key]["destino"]
                paradas = info[dir_key]["paradas"]
            break

    # Resolver con el índice estático de la concesión correspondiente.
    lookup = obtener_dataset_gtfs(concesion_key)["lookup"]
    nombre_parada = None
    if stop_id:
        nombre_parada = lookup.get("stops", {}).get(str(stop_id))
    if not nombre_parada and trip_id and stop_sequence is not None:
        stop_id_estatico = lookup.get("trips", {}).get(str(trip_id), {}).get(str(int(stop_sequence)))
        if stop_id_estatico:
            nombre_parada = lookup.get("stops", {}).get(str(stop_id_estatico))
    if not nombre_parada:
        nombre_parada = "Parada sin identificar"

    return destino, nombre_parada

def leer_campo_protobuf_opcional(mensaje, nombre):
    """Lee un campo opcional sin confundir su valor predeterminado con un dato publicado."""
    if mensaje is None or not hasattr(mensaje, nombre):
        return None

    comprobar_presencia = getattr(mensaje, "HasField", None)
    if callable(comprobar_presencia):
        try:
            if not comprobar_presencia(nombre):
                return None
        except (TypeError, ValueError):
            # Algunos mensajes/esquemas no permiten consultar presencia escalar.
            pass

    return getattr(mensaje, nombre, None)


def convertir_velocidad_kmh(velocidad_mps):
    """Convierte m/s de GTFS-RT a km/h; mantiene 0 como velocidad válida."""
    try:
        velocidad = float(velocidad_mps)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(velocidad) or velocidad < 0:
        return None
    return round(velocidad * 3.6, 1)


def procesar_ocupacion(raw_status, raw_percentage=None):
    """Traduce OccupancyStatus y, si existe, añade el porcentaje publicado."""
    status_str = str(raw_status).strip().upper() if raw_status is not None else ""
    enum_map = {
        '0': 'EMPTY',
        '1': 'MANY_SEATS_AVAILABLE',
        '2': 'FEW_SEATS_AVAILABLE',
        '3': 'STANDING_ROOM_ONLY',
        '4': 'CRUSHED_STANDING_ROOM_ONLY',
        '5': 'FULL',
        '6': 'NOT_ACCEPTING_PASSENGERS',
        '7': 'NO_DATA_AVAILABLE',
        '8': 'NOT_BOARDABLE'
    }
    clave = enum_map.get(status_str, status_str)
    texto, color = TRADUCCION_OCUPACION.get(clave, ('Sin datos', 'gray'))

    porcentaje = None
    if raw_percentage is not None:
        try:
            valor = float(raw_percentage)
            if math.isfinite(valor) and 0 <= valor <= 100:
                porcentaje = valor
        except (TypeError, ValueError, OverflowError):
            pass

    if porcentaje is not None:
        porcentaje_texto = f"{porcentaje:g}%"
        if clave == 'NO_DATA_AVAILABLE' or clave not in TRADUCCION_OCUPACION:
            texto = f"{porcentaje_texto} de ocupación"
        else:
            texto = f"{texto} ({porcentaje_texto})"

    return texto, color


def obtener_fecha_servicio_trip_descriptor(viaje):
    """Lee TripDescriptor.start_date, si el feed la proporciona."""
    if not viaje or not viaje.HasField("start_date") or not viaje.start_date:
        return None
    try:
        return datetime.strptime(str(viaje.start_date), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def obtener_instante_programado_trip_gtfs(
    horario,
    trip_id,
    stop_id,
    tipo_evento="arrival",
    timestamp_referencia=None,
    fecha_servicio=None,
    ahora=None,
    delay_referencia=0,
):
    """
    Busca la hora oficial de una parada del viaje y la devuelve como timestamp POSIX.

    Usa start_date cuando existe; si falta, contrasta los días de servicio próximos.
    También contempla horas GTFS superiores a 24:00 y excepciones de calendario.
    """
    trip_id = str(trip_id or "")
    stop_id = str(stop_id or "")
    if not trip_id or not stop_id:
        return None

    registros = [
        registro for registro in horario.get("stop_times", [])
        if str(registro.get("trip_id") or "") == trip_id
        and str(registro.get("stop_id") or "") == stop_id
    ]
    if not registros:
        return None

    if fecha_servicio is not None:
        fechas_candidatas = [fecha_servicio]
    else:
        referencia = timestamp_referencia
        if referencia is None:
            referencia = ahora if ahora is not None else time.time()
        fecha_referencia = datetime.fromtimestamp(
            float(referencia), ZONA_HORARIA
        ).date()
        fechas_candidatas = [
            fecha_referencia + timedelta(days=desplazamiento)
            for desplazamiento in (-2, -1, 0, 1)
        ]

    ahora_timestamp = float(ahora if ahora is not None else time.time())
    mejor_instante = None
    mejor_puntuacion = None

    for registro in registros:
        clave_hora = (
            "departure_time" if tipo_evento == "departure" else "arrival_time"
        )
        hora_gtfs = registro.get(clave_hora) or registro.get(
            "arrival_time" if clave_hora == "departure_time" else "departure_time"
        )
        segundos = segundos_horario_gtfs(hora_gtfs)
        if segundos is None:
            continue

        service_id = str(registro.get("service_id") or "")
        for fecha in fechas_candidatas:
            if service_id and not servicio_activo_gtfs(
                service_id, fecha, horario
            ):
                continue

            instante_programado = datetime.combine(
                fecha, datetime.min.time(), tzinfo=ZONA_HORARIA
            ) + timedelta(seconds=segundos)
            timestamp_programado = instante_programado.timestamp()

            if timestamp_referencia is not None:
                puntuacion = abs(timestamp_programado - float(timestamp_referencia))
            else:
                timestamp_estimado = timestamp_programado + float(
                    delay_referencia or 0
                )
                if ahora_timestamp - 120 <= timestamp_estimado <= ahora_timestamp + 12 * 3600:
                    puntuacion = max(0, timestamp_estimado - ahora_timestamp)
                else:
                    puntuacion = abs(timestamp_estimado - ahora_timestamp) + 1_000_000_000

            if mejor_puntuacion is None or puntuacion < mejor_puntuacion:
                mejor_puntuacion = puntuacion
                mejor_instante = timestamp_programado

    return mejor_instante


def resolver_evento_horario_gtfs(
    horario,
    trip_id,
    stop_id,
    tipo_evento="arrival",
    timestamp_evento=None,
    delay_evento=None,
    fecha_servicio=None,
    ahora=None,
):
    """
    Resuelve la predicción GTFS-RT como (timestamp estimado, desviación en segundos).

    Cuando el feed publica una hora absoluta, la compara con el GTFS estático.
    Cuando solo publica delay, reconstruye la hora estimada desde el horario oficial.
    """
    timestamp_evento = (
        int(timestamp_evento) if timestamp_evento is not None else None
    )
    delay_evento = int(delay_evento) if delay_evento is not None else None

    instante_programado = obtener_instante_programado_trip_gtfs(
        horario,
        trip_id,
        stop_id,
        tipo_evento=tipo_evento,
        timestamp_referencia=timestamp_evento,
        fecha_servicio=fecha_servicio,
        ahora=ahora,
        delay_referencia=delay_evento or 0,
    )

    if timestamp_evento is not None:
        if instante_programado is not None:
            retraso = int(round(timestamp_evento - instante_programado))
        else:
            # Si no se puede emparejar el horario, solo se conserva un delay
            # explícito del mismo StopTimeEvent; nunca se inventa puntualidad.
            retraso = delay_evento
        return timestamp_evento, retraso

    if delay_evento is not None and instante_programado is not None:
        return int(round(instante_programado + delay_evento)), delay_evento

    return None, delay_evento


def formatear_retraso_gtfs(delay_segundos):
    """Expresa una desviación GTFS-RT firmada; None significa que no hay datos."""
    if delay_segundos is None:
        return "Sin datos"

    try:
        delay = float(delay_segundos)
    except (TypeError, ValueError, OverflowError):
        return "Sin datos"
    if not math.isfinite(delay):
        return "Sin datos"
    if abs(delay) < 30:
        return "En hora"

    minutos = round(abs(delay) / 60.0, 1)
    minutos_texto = str(int(minutos)) if minutos.is_integer() else str(minutos)
    if delay > 0:
        return f"+{minutos_texto} min retraso"
    return f"-{minutos_texto} min adelanto"


def obtener_estimaciones_tripupdates(linea_objetivo):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': '*/*'
    }
    estimaciones = {}
    fuentes = FUENTES_GTFSRT_POR_CONCESION.get(linea_objetivo, {})
    if not fuentes:
        return estimaciones

    dataset = obtener_dataset_gtfs(linea_objetivo)
    horario = dataset.get("schedule", {})
    rutas_validas = list(dataset["map"].get("routes", {}).keys())

    for url in fuentes.get("tripupdates", []):
        try:
            res = obtener_respuesta_cacheada(url, "tripupdates", 5, headers, 8)
            if res.status_code != 200 or not res.content:
                continue

            content = res.content
            if content.startswith(b'\x1f\x8b'):
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass

            from google.transit import gtfs_realtime_pb2
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)

            ahora = time.time()
            for entity in feed.entity:
                if not entity.HasField('trip_update'):
                    continue

                tu = entity.trip_update
                if not tu.HasField('trip'):
                    continue
                viaje = tu.trip
                r_id = str(viaje.route_id or "")
                trip_id = str(viaje.trip_id or "")
                v_id = (
                    str(tu.vehicle.id)
                    if tu.HasField('vehicle') and tu.vehicle.id else ""
                )
                coche_label = (
                    str(tu.vehicle.label)
                    if tu.HasField('vehicle') and tu.vehicle.label else ""
                )
                direction_id = (
                    viaje.direction_id
                    if viaje.HasField('direction_id') else None
                )

                if r_id not in rutas_validas:
                    continue
                if (
                    viaje.HasField('schedule_relationship')
                    and viaje.schedule_relationship
                    == gtfs_realtime_pb2.TripDescriptor.CANCELED
                ):
                    continue

                fecha_servicio = obtener_fecha_servicio_trip_descriptor(viaje)
                delay_trip = int(tu.delay) if tu.HasField('delay') else None
                delay_sec = delay_trip
                proxima_parada = "En trayecto"

                destino, _ = resolver_destino_y_parada(
                    linea_objetivo, r_id, direction_id, None
                )

                # Se conserva el tipo de evento (llegada/salida) junto a su
                # hora y desviación: no se deben cruzar arrival.delay y departure.time.
                actualizaciones_parada = []
                for stu in tu.stop_time_update:
                    relacion = (
                        stu.schedule_relationship
                        if stu.HasField('schedule_relationship') else None
                    )
                    if relacion in (
                        gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED,
                        gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.NO_DATA,
                    ):
                        continue

                    evento_tipo = None
                    evento = None
                    for nombre_evento in ("arrival", "departure"):
                        if not stu.HasField(nombre_evento):
                            continue
                        candidato = getattr(stu, nombre_evento)
                        if candidato.HasField('time') or candidato.HasField('delay'):
                            evento_tipo = nombre_evento
                            evento = candidato
                            break

                    if evento is None:
                        continue

                    evento_time = (
                        int(evento.time) if evento.HasField('time') else None
                    )
                    evento_delay = (
                        int(evento.delay) if evento.HasField('delay') else None
                    )
                    if evento_time is not None and evento_time > ahora:
                        actualizaciones_parada.append(
                            (stu, evento_tipo, evento_time, evento_delay, True)
                        )
                    elif evento_time is None:
                        actualizaciones_parada.append(
                            (stu, evento_tipo, None, evento_delay, False)
                        )

                # El feed ordena las paradas por secuencia: se usa la primera
                # predicción futura; si solo hay delay, la primera con datos.
                elegida = next(
                    (item for item in actualizaciones_parada if item[4]),
                    None
                )
                if elegida is None:
                    elegida = next(
                        (item for item in actualizaciones_parada if not item[4]),
                        None
                    )

                if elegida:
                    stu, evento_tipo, evento_time, evento_delay, tiene_hora_futura = elegida
                    minutos = (
                        max(0, int((evento_time - ahora) / 60))
                        if tiene_hora_futura else None
                    )
                    stop_seq = (
                        stu.stop_sequence if stu.HasField('stop_sequence') else None
                    )
                    stop_id = (
                        str(stu.stop_id)
                        if stu.HasField('stop_id') and stu.stop_id else None
                    )
                    if not stop_id and trip_id and stop_seq is not None:
                        stop_id = str(
                            dataset.get("lookup", {})
                            .get("trips", {})
                            .get(trip_id, {})
                            .get(str(int(stop_seq)), "")
                        ) or None

                    _, nombre_parada = resolver_destino_y_parada(
                        linea_objetivo, r_id, direction_id,
                        stop_seq, stop_id, trip_id
                    )
                    proxima_parada = (
                        f"{nombre_parada}"
                        + (f" ({minutos} min)" if minutos is not None else "")
                    )

                    _, delay_calculado = resolver_evento_horario_gtfs(
                        horario,
                        trip_id,
                        stop_id,
                        tipo_evento=evento_tipo,
                        timestamp_evento=evento_time,
                        delay_evento=evento_delay,
                        fecha_servicio=fecha_servicio,
                        ahora=ahora,
                    )
                    # StopTimeEvent tiene prioridad sobre TripUpdate.delay.
                    # Si no se puede emparejar una hora absoluta con GTFS,
                    # se conserva el delay explícito del evento o el global.
                    if delay_calculado is not None:
                        delay_sec = delay_calculado
                    elif evento_delay is not None:
                        delay_sec = evento_delay

                info = {
                    'retraso': formatear_retraso_gtfs(delay_sec),
                    'retraso_segundos': delay_sec,
                    'proxima_parada': proxima_parada,
                    'destino': destino,
                }
                if v_id:
                    estimaciones.setdefault(v_id, info)
                if coche_label:
                    estimaciones.setdefault(coche_label, info)
                if trip_id:
                    estimaciones.setdefault(trip_id, info)
        except Exception as exc:
            print(f"[TRIPUPDATES ERROR {url}]: {exc}")

    return estimaciones

@app.route('/')
def index():
    return render_template('index.html')


# Buzón privado de sugerencias y quejas. Las credenciales SMTP viven solo
# en variables de entorno del servidor (nunca se envían al navegador).
BUZON_ULTIMO_ENVIO = {}
BUZON_ULTIMO_ENVIO_LOCK = threading.Lock()
# Cuotas antispam por navegador (cookie aleatoria HttpOnly) y por IP.
# Ventana móvil de 24 horas: 2 envíos por navegador y 5 por IP.
BUZON_CUOTAS = {"dispositivo": {}, "ip": {}}
BUZON_CUOTAS_LOCK = threading.Lock()
BUZON_VENTANA_SEGUNDOS = 24 * 60 * 60
BUZON_MAX_DISPOSITIVO = 2
BUZON_MAX_IP = 5
BUZON_COOKIE_DISPOSITIVO = "mapatpa_buzon_id"


def comprobar_cuota_buzon(dispositivo, ip, ahora=None):
    """Comprueba y reserva un envío dentro de las cuotas por navegador e IP."""
    ahora = time.time() if ahora is None else ahora
    claves = (("dispositivo", dispositivo, BUZON_MAX_DISPOSITIVO),
              ("ip", ip, BUZON_MAX_IP))
    with BUZON_CUOTAS_LOCK:
        for categoria, identificador, limite in claves:
            envios = BUZON_CUOTAS[categoria].get(identificador, [])
            recientes = [instante for instante in envios
                         if ahora - instante < BUZON_VENTANA_SEGUNDOS]
            BUZON_CUOTAS[categoria][identificador] = recientes
            if len(recientes) >= limite:
                restante = BUZON_VENTANA_SEGUNDOS - (ahora - recientes[0])
                return False, max(1, int((restante + 59) // 60))
        for categoria, identificador, _ in claves:
            BUZON_CUOTAS[categoria][identificador].append(ahora)
        # Limpieza periódica para que el diccionario no crezca indefinidamente.
        for categoria in ("dispositivo", "ip"):
            caducadas = [
                clave for clave, envios in BUZON_CUOTAS[categoria].items()
                if not envios or ahora - envios[-1] >= BUZON_VENTANA_SEGUNDOS
            ]
            for clave in caducadas:
                BUZON_CUOTAS[categoria].pop(clave, None)
        return True, 0


def retirar_reserva_cuota_buzon(dispositivo, ip):
    """Libera la reserva si el proveedor de correo no pudo aceptar el envío."""
    with BUZON_CUOTAS_LOCK:
        for categoria, identificador in (("dispositivo", dispositivo), ("ip", ip)):
            envios = BUZON_CUOTAS[categoria].get(identificador, [])
            if envios:
                envios.pop()
            if not envios:
                BUZON_CUOTAS[categoria].pop(identificador, None)


def obtener_configuracion_buzon():
    """Devuelve la configuración SMTP solo cuando están todos los secretos."""
    host = os.environ.get("BUZON_SMTP_HOST", "smtp.gmail.com").strip()
    usuario = os.environ.get("BUZON_SMTP_USER", "ribota2010@gmail.com").strip()
    password = re.sub(r"\s+", "", os.environ.get("BUZON_SMTP_PASSWORD", ""))
    destino = os.environ.get("BUZON_EMAIL_DESTINO", "ribota2010@gmail.com").strip()
    remitente = os.environ.get("BUZON_EMAIL_REMITENTE", "").strip() or usuario
    try:
        puerto = int(os.environ.get("BUZON_SMTP_PORT", "587"))
    except (TypeError, ValueError):
        return None
    if (
        not host or not usuario or not password or not destino or not remitente
        or not 1 <= puerto <= 65535
    ):
        return None
    return {
        "host": host,
        "puerto": puerto,
        "usuario": usuario,
        "password": password,
        "destino": destino,
        "remitente": remitente,
        "ssl": os.environ.get("BUZON_SMTP_SSL", "").strip().lower()
            in {"1", "true", "yes", "si"},
    }


def limitar_envios_buzon(ip):
    """Evita envíos repetidos desde una misma IP durante un minuto."""
    ahora = time.monotonic()
    with BUZON_ULTIMO_ENVIO_LOCK:
        caducadas = [
            direccion for direccion, instante in BUZON_ULTIMO_ENVIO.items()
            if ahora - instante > 3600
        ]
        for direccion in caducadas:
            BUZON_ULTIMO_ENVIO.pop(direccion, None)
        ultimo = BUZON_ULTIMO_ENVIO.get(ip)
        if ultimo is not None and ahora - ultimo < 60:
            return False
        BUZON_ULTIMO_ENVIO[ip] = ahora
        return True


# Moderación básica del buzón. Se aplica en el servidor antes de contactar
# con Resend o SMTP, por lo que no depende de la validación del navegador.
PALABRAS_OFENSIVAS_BUZON = frozenset({
    "gilipollas", "imbecil", "idiota", "estupido", "estupida",
    "subnormal", "retrasado", "retrasada", "cabrón", "cabron",
    "cabrona", "capullo", "capulla", "puto", "puta", "mierda",
    "joder", "zorra", "maricon", "maricona", "soplapollas",
    "mamón", "mamon", "pendejo", "pelotudo", "boludo", "hdp",
    "hijoputa", "fuck", "fucking", "shit", "bitch", "asshole",
    "bastard", "bastarda", "moron", "idiot", "dumbass", "cunt",
    "motherfucker",
})

FRASES_OFENSIVAS_BUZON = (
    "hijo de puta",
    "hija de puta",
    "vete a la mierda",
    "vete a tomar por culo",
    "que te jodan",
    "que os jodan",
    "me cago en ti",
    "te voy a matar",
    "os voy a matar",
    "voy a matarte",
    "voy a mataros",
    "te voy a partir la cara",
    "os voy a partir la cara",
    "te voy a reventar",
    "os voy a reventar",
    "ojala te mueras",
    "ojala os murais",
)


def normalizar_texto_buzon(texto):
    """Normaliza acentos, mayúsculas y algunas sustituciones habituales."""
    texto = unicodedata.normalize("NFKD", str(texto or "").casefold())
    texto = "".join(
        caracter for caracter in texto
        if not unicodedata.combining(caracter)
    )
    texto = texto.translate(str.maketrans({
        "0": "o", "1": "i", "3": "e", "4": "a",
        "5": "s", "7": "t", "@": "a", "$": "s",
    }))
    # Reduce repeticiones para detectar variantes como "putaaaa" o "idiiooota".
    texto = re.sub(r"(.)\1{2,}", r"\1", texto)
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def contenido_inadecuado_buzon(*partes):
    """Detecta insultos, tacos y amenazas claras en los campos de texto."""
    texto = " ".join(
        normalizar_texto_buzon(parte)
        for parte in partes if parte
    )
    if not texto:
        return False

    palabras = set(texto.split())
    if palabras.intersection(PALABRAS_OFENSIVAS_BUZON):
        return True

    texto_delimitado = f" {texto} "
    if any(f" {frase} " in texto_delimitado for frase in FRASES_OFENSIVAS_BUZON):
        return True

    # También detecta palabras separadas por puntos o espacios, por ejemplo
    # "g.i.l.i.p.o.l.l.a.s", sin buscar coincidencias dentro de otras palabras.
    for palabra in PALABRAS_OFENSIVAS_BUZON:
        patron = r"(?<![a-z0-9])" + r"\s*".join(
            re.escape(letra) for letra in palabra
        ) + r"(?![a-z0-9])"
        if re.search(patron, texto):
            return True

    return False


def obtener_configuracion_resend():
    """Devuelve la configuración de Resend cuando hay una API key disponible."""
    api_key = os.environ.get("BUZON_RESEND_API_KEY", "").strip()
    remitente = os.environ.get(
        "BUZON_RESEND_FROM", "Mapa TPA <onboarding@resend.dev>"
    ).strip()
    destino = os.environ.get("BUZON_EMAIL_DESTINO", "ribota2010@gmail.com").strip()
    if not api_key or not remitente or not destino:
        return None
    return {"api_key": api_key, "remitente": remitente, "destino": destino}


@app.route('/api/buzon/estado', methods=['GET'])
def get_estado_buzon():
    """Informa únicamente de si hay un proveedor de correo configurado."""
    disponible = (
        obtener_configuracion_resend() is not None
        or obtener_configuracion_buzon() is not None
    )
    return jsonify({"disponible": disponible})


@app.route('/api/buzon', methods=['POST'])
def post_buzon():
    """Envía de forma privada una sugerencia o queja al correo configurado."""
    if request.content_length and request.content_length > 12000:
        return jsonify({"error": "El mensaje es demasiado grande."}), 413

    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El formulario no contiene datos válidos."}), 400

    # Campo trampa para bots: se responde como aceptado, pero no se envía nada.
    if str(datos.get("website", "")).strip():
        return jsonify({"ok": True})

    configuracion_resend = obtener_configuracion_resend()
    configuracion = obtener_configuracion_buzon()
    if configuracion_resend is None and configuracion is None:
        return jsonify({
            "error": "El buzón no está disponible temporalmente. Inténtalo más tarde."
        }), 503

    tipo = str(datos.get("tipo", "")).strip().lower()
    asunto = re.sub(r"[\r\n]+", " ", str(datos.get("asunto", "")).strip())
    mensaje = str(datos.get("mensaje", "")).strip()
    nombre = re.sub(r"[\r\n]+", " ", str(datos.get("nombre", "")).strip())
    correo = str(datos.get("correo", "")).strip()

    if tipo not in {"sugerencia", "queja"}:
        return jsonify({"error": "Selecciona si quieres enviar una sugerencia o una queja."}), 400
    if len(asunto) < 4 or len(asunto) > 120:
        return jsonify({"error": "El asunto debe tener entre 4 y 120 caracteres."}), 400
    if len(mensaje) < 20 or len(mensaje) > 5000:
        return jsonify({"error": "El mensaje debe tener entre 20 y 5.000 caracteres."}), 400
    if len(nombre) > 80:
        return jsonify({"error": "El nombre no puede superar los 80 caracteres."}), 400
    if contenido_inadecuado_buzon(asunto, mensaje, nombre):
        return jsonify({
            "error": (
                "No se puede enviar el mensaje porque contiene insultos, "
                "lenguaje ofensivo o amenazas. Mantén un tono respetuoso; "
                "las quejas y críticas sobre el servicio sí están permitidas."
            )
        }), 400
    if correo and (
        len(correo) > 254
        or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", correo)
        or "\r" in correo or "\n" in correo
    ):
        return jsonify({"error": "Introduce un correo electrónico válido o deja el campo vacío."}), 400

    ip = request.remote_addr or "desconocida"
    dispositivo = request.cookies.get(BUZON_COOKIE_DISPOSITIVO, "")
    if not re.fullmatch(r"[a-f0-9]{32}", dispositivo):
        dispositivo = uuid.uuid4().hex

    # Conserva una pausa breve anti-reintentos y aplica además cuotas diarias.
    if not limitar_envios_buzon(ip):
        respuesta = jsonify({
            "error": "Has enviado un mensaje hace muy poco. Espera un minuto antes de volver a intentarlo."
        })
        return respuesta, 429

    permitido, minutos = comprobar_cuota_buzon(dispositivo, ip)
    if not permitido:
        respuesta = jsonify({
            "error": (
                f"Has alcanzado el límite de mensajes: máximo {BUZON_MAX_DISPOSITIVO} "
                f"por navegador y {BUZON_MAX_IP} por IP cada 24 horas. "
                f"Podrás volver a enviar mensajes dentro de aproximadamente {minutos} minutos."
            )
        })
        respuesta.status_code = 429
        respuesta.set_cookie(
            BUZON_COOKIE_DISPOSITIVO, dispositivo, max_age=30 * 24 * 60 * 60,
            httponly=True, secure=request.is_secure, samesite="Lax", path="/"
        )
        return respuesta

    tipo_texto = "Sugerencia" if tipo == "sugerencia" else "Queja"
    asunto_completo = f"[Mapa TPA] {tipo_texto}: {asunto}"
    cuerpo_texto = (
        f"Nuevo mensaje recibido desde el buzón de Mapa TPA.\n\n"
        f"Tipo: {tipo_texto}\n"
        f"Asunto: {asunto}\n"
        f"Nombre: {nombre or 'No indicado'}\n"
        f"Correo para responder: {correo or 'No indicado'}\n\n"
        f"Mensaje:\n{mensaje}\n"
    )

    try:
        # Resend usa HTTPS (443), por lo que también funciona en los servicios
        # gratuitos de Render, que bloquean los puertos SMTP salientes.
        # Tiene prioridad si se ha configurado una API key.
        if configuracion_resend is not None:
            payload = {
                "from": configuracion_resend["remitente"],
                # En modo de prueba de Resend se debe enviar a la dirección
                # propietaria de la cuenta; mantenerla como dirección simple.
                "to": [configuracion_resend["destino"]],
                "subject": asunto_completo,
                "text": cuerpo_texto,
            }
            if correo:
                payload["headers"] = {"Reply-To": correo}

            respuesta_api = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {configuracion_resend['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
            if not respuesta_api.ok:
                app.logger.error(
                    "Resend rechazó el envío (HTTP %s): %s",
                    respuesta_api.status_code,
                    respuesta_api.text[:1000],
                )
                respuesta_api.raise_for_status()
        else:
            correo_mensaje = EmailMessage()
            correo_mensaje["Subject"] = asunto_completo
            correo_mensaje["From"] = configuracion["remitente"]
            correo_mensaje["To"] = configuracion["destino"]
            if correo:
                correo_mensaje["Reply-To"] = correo
            correo_mensaje.set_content(cuerpo_texto)

            contexto_ssl = ssl.create_default_context()
            if configuracion["ssl"]:
                conexion = smtplib.SMTP_SSL(
                    configuracion["host"],
                    configuracion["puerto"],
                    timeout=12,
                    context=contexto_ssl,
                )
            else:
                conexion = smtplib.SMTP(
                    configuracion["host"],
                    configuracion["puerto"],
                    timeout=12,
                )
            with conexion as servidor:
                if not configuracion["ssl"]:
                    servidor.ehlo()
                    servidor.starttls(context=contexto_ssl)
                    servidor.ehlo()
                servidor.login(configuracion["usuario"], configuracion["password"])
                servidor.send_message(correo_mensaje)
    except (requests.RequestException, smtplib.SMTPException, OSError, ssl.SSLError):
        app.logger.exception("No se pudo enviar un mensaje del buzón de Mapa TPA.")
        with BUZON_ULTIMO_ENVIO_LOCK:
            BUZON_ULTIMO_ENVIO.pop(ip, None)
        retirar_reserva_cuota_buzon(dispositivo, ip)
        return jsonify({
            "error": "No se ha podido enviar el mensaje. Inténtalo más tarde."
        }), 502

    respuesta = jsonify({"ok": True, "mensaje": "Tu mensaje se ha enviado correctamente."})
    respuesta.set_cookie(
        BUZON_COOKIE_DISPOSITIVO, dispositivo, max_age=30 * 24 * 60 * 60,
        httponly=True, secure=request.is_secure, samesite="Lax", path="/"
    )
    return respuesta


@app.route('/api/mapa/<linea>')
def get_mapa_lineas(linea):
    """Devuelve las paradas GTFS y las líneas que pasan por cada una."""
    concesion = linea.upper()
    if concesion not in CATALOGO_CONCESIONES:
        return jsonify({"error": "Concesión no reconocida", "routes": [], "stops": []}), 404

    dataset = obtener_dataset_gtfs(concesion)
    mapa = dataset["map"]
    rutas = []
    paradas_por_id = {}
    for route_id, info in mapa.get("routes", {}).items():
        rutas.append({
            "route_id": route_id,
            "short_name": info.get("short_name", concesion),
            "long_name": info.get("long_name", ""),
            "color": "#" + str(info.get("color", "2563EB")).lstrip("#")
        })
        nombre_linea = info.get("short_name", concesion)
        for parada_original in mapa.get("stops", {}).get(nombre_linea, []):
            parada = dict(parada_original)
            parada["concesion"] = concesion
            stop_id = str(parada.get("id") or parada.get("stop_id") or "")
            if not stop_id:
                continue
            if stop_id not in paradas_por_id:
                parada["lineas"] = []
                paradas_por_id[stop_id] = parada
            if nombre_linea not in paradas_por_id[stop_id]["lineas"]:
                paradas_por_id[stop_id]["lineas"].append(nombre_linea)

    paradas = list(paradas_por_id.values())
    for parada in paradas:
        parada["lineas"].sort()

    return jsonify({
        "concesion": concesion,
        "routes": rutas,
        "stops": paradas,
        "funciones": {
            "paradas": dataset["has_map"],
            "horarios": dataset["has_schedules"],
            "tiempo_real": dataset["has_realtime"],
        }
    })


@app.route('/api/concesiones')
def get_concesiones():
    """Catálogo público: activa solo concesiones que tengan datos estáticos cargados."""
    return jsonify({"concesiones": construir_catalogo_publico(GTFS_DATASETS)})


def obtener_llegadas_en_tiempo_real(stop_id, concesion="T05", stop_ids=None):
    """Consulta GTFS-RT y devuelve predicciones para una o varias paradas fusionadas."""
    ids_parada_objetivo = {str(stop_id)}
    ids_parada_objetivo.update(
        str(valor) for valor in (stop_ids or []) if valor
    )
    codigo = str(concesion or "").upper()
    dataset = obtener_dataset_gtfs(codigo)
    fuentes = FUENTES_GTFSRT_POR_CONCESION.get(codigo, {})
    if not fuentes or not dataset["has_map"]:
        return {
            "llegadas": [],
            "tiempo_real_disponible": False,
            "actualizado": None,
        }

    mapa = dataset["map"]
    lookup = dataset["lookup"]
    ahora = time.time()

    # Nombres de las paradas seleccionadas, incluidos los marcadores fusionados.
    # Sirve como segunda protección cuando el proveedor RT no permite resolver
    # el stop_sequence terminal mediante el trip_id estático.
    nombres_parada_objetivo = set()
    for paradas_linea in mapa.get("stops", {}).values():
        for parada in paradas_linea:
            ids_registro = {
                str(valor) for valor in (parada.get("stop_ids") or [
                    parada.get("id") or parada.get("stop_id") or ""
                ]) if valor
            }
            if ids_registro.intersection(ids_parada_objetivo):
                nombre = normalizar_nombre_parada_gtfs(parada.get("name"))
                if nombre:
                    nombres_parada_objetivo.add(nombre)

    llegadas = []
    feed_disponible = False
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MapaTPA/1.0)",
        "Accept": "*/*"
    }

    from google.transit import gtfs_realtime_pb2

    for url in fuentes.get("tripupdates", []):
        try:
            respuesta = obtener_respuesta_cacheada(url, "tripupdates", 5, headers, 7)
            if respuesta.status_code != 200 or not respuesta.content:
                continue

            contenido = respuesta.content
            if contenido.startswith(b"\x1f\x8b"):
                try:
                    contenido = gzip.decompress(contenido)
                except (OSError, EOFError):
                    pass

            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(contenido)
            feed_disponible = True

            for entidad in feed.entity:
                if not entidad.HasField("trip_update"):
                    continue

                actualizacion = entidad.trip_update
                if not actualizacion.HasField("trip"):
                    continue

                viaje = actualizacion.trip
                route_id = str(viaje.route_id or "")
                trip_id = str(viaje.trip_id or "")
                entity_id = str(entidad.id or "")
                vehicle_id = (
                    str(actualizacion.vehicle.id)
                    if actualizacion.HasField("vehicle") and actualizacion.vehicle.id
                    else ""
                )
                info_ruta = mapa.get("routes", {}).get(route_id)
                if not info_ruta:
                    continue

                if viaje.HasField("schedule_relationship") and viaje.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.CANCELED:
                    continue

                direccion = viaje.direction_id if viaje.HasField("direction_id") else None
                destino, _ = resolver_destino_y_parada(codigo, route_id, direccion, None)
                if not destino or destino == "En trayecto":
                    destino = info_ruta.get("long_name", "Destino no disponible")

                # No anunciar como salida un servicio cuyo destino es la misma
                # parada terminal seleccionada (incluida puntuación variable).
                if normalizar_nombre_parada_gtfs(destino) in nombres_parada_objetivo:
                    continue

                for actualizacion_parada in actualizacion.stop_time_update:
                    if actualizacion_parada.HasField("schedule_relationship") and actualizacion_parada.schedule_relationship == gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED:
                        continue

                    stop_id_actualizacion = (
                        str(actualizacion_parada.stop_id)
                        if actualizacion_parada.HasField("stop_id") and actualizacion_parada.stop_id
                        else ""
                    )
                    if not stop_id_actualizacion and trip_id and actualizacion_parada.HasField("stop_sequence"):
                        stop_id_actualizacion = str(
                            lookup.get("trips", {})
                            .get(trip_id, {})
                            .get(str(int(actualizacion_parada.stop_sequence)), "")
                        )

                    if stop_id_actualizacion not in ids_parada_objetivo:
                        continue
                    if es_ultima_parada_viaje_gtfs(dataset, {
                        "trip_id": trip_id,
                        "stop_id": stop_id_actualizacion,
                    }):
                        continue

                    if (
                        actualizacion_parada.HasField("schedule_relationship")
                        and actualizacion_parada.schedule_relationship
                        in (
                            gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED,
                            gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.NO_DATA,
                        )
                    ):
                        continue

                    evento_tipo = None
                    evento = None
                    # El horario mostrado en la parada representa salidas; por
                    # tanto, departure es la referencia principal. Si el feed
                    # solo publica arrival, se utiliza como respaldo.
                    for nombre_evento in ("departure", "arrival"):
                        if not actualizacion_parada.HasField(nombre_evento):
                            continue
                        candidato = getattr(actualizacion_parada, nombre_evento)
                        if candidato.HasField("time") or candidato.HasField("delay"):
                            evento_tipo = nombre_evento
                            evento = candidato
                            break
                    if evento is None:
                        continue

                    timestamp_evento = (
                        int(evento.time) if evento.HasField("time") else None
                    )
                    delay_evento = (
                        int(evento.delay) if evento.HasField("delay") else None
                    )
                    fecha_servicio = obtener_fecha_servicio_trip_descriptor(viaje)
                    instante, retraso_segundos = resolver_evento_horario_gtfs(
                        dataset.get("schedule", {}),
                        trip_id,
                        stop_id_actualizacion,
                        tipo_evento=evento_tipo,
                        timestamp_evento=timestamp_evento,
                        delay_evento=delay_evento,
                        fecha_servicio=fecha_servicio,
                        ahora=ahora,
                    )

                    if instante is None or instante < ahora - 30 * 60 or instante > ahora + 12 * 3600:
                        continue

                    # Una expedición adelantada puede haber pasado ya aunque su
                    # hora oficial siga en el futuro. Se conserva durante 30 min
                    # como marca de paso, para retirar su salida teórica.
                    pasado = bool(
                        instante < ahora - 60
                        and retraso_segundos is not None
                        and retraso_segundos < 0
                    )
                    if instante < ahora - 60 and not pasado:
                        continue

                    fecha_hora = datetime.fromtimestamp(instante, ZONA_HORARIA)
                    minutos = max(0, math.ceil((instante - ahora) / 60))
                    llegadas.append({
                        "linea": info_ruta.get("short_name", codigo),
                        "destino": destino,
                        "hora": fecha_hora.strftime("%H:%M"),
                        "minutos": minutos,
                        "timestamp": instante,
                        "retraso_segundos": retraso_segundos,
                        "pasado": pasado,
                        "fuente": "Tiempo real",
                        "trip_id": trip_id,
                        "direccion": str(direccion) if direccion is not None else "",
                        "vehicle_id": vehicle_id,
                        "entity_id": entity_id,
                        "stop_id": stop_id_actualizacion
                    })
        except Exception as exc:
            print(f"[LLEGADAS GTFS-RT ERROR {url}]: {exc}")

    # Conserva la primera fuente para cada viaje/parada y añade los servicios
    # que solo estén presentes en los feeds secundarios.
    unicas_por_viaje = {}
    anonimas = []
    for llegada in llegadas:
        identificador = (
            llegada.get("trip_id") or
            llegada.get("vehicle_id") or
            llegada.get("entity_id")
        )
        if identificador:
            clave = (
                identificador,
                llegada["linea"],
                llegada.get("stop_id", ""),
            )
            unicas_por_viaje.setdefault(clave, llegada)
        else:
            anonimas.append(llegada)
    unicas = list(unicas_por_viaje.values()) + anonimas
    unicas.sort(key=lambda item: item["timestamp"])

    return {
        "llegadas": unicas,
        "tiempo_real_disponible": feed_disponible,
        "actualizado": datetime.now(ZONA_HORARIA).strftime("%H:%M:%S")
    }


@app.route('/api/paradas/<stop_id>/llegadas')
def get_llegadas_parada(stop_id):
    """Próximas llegadas para una parada de una concesión."""
    concesion = request.args.get("concesion", "T05").upper()
    dataset = obtener_dataset_gtfs(concesion)
    ids_disponibles = set()
    for paradas in dataset["map"].get("stops", {}).values():
        for parada in paradas:
            ids_disponibles.update(
                str(valor) for valor in (parada.get("stop_ids") or [
                    parada.get("id") or parada.get("stop_id") or ""
                ]) if valor
            )

    if str(stop_id) not in ids_disponibles:
        return jsonify({"error": "Parada no encontrada", "llegadas": []}), 404

    ids_solicitados = {str(stop_id)}
    ids_parametro = request.args.get("stop_ids", "")
    ids_solicitados.update(
        valor.strip() for valor in ids_parametro.split(",") if valor.strip()
    )
    ids_consultados = ids_solicitados.intersection(ids_disponibles)
    return jsonify(obtener_llegadas_en_tiempo_real(
        stop_id, concesion, ids_consultados
    ))


def servicio_activo_gtfs(service_id, fecha, horario=None):
    """Aplica calendar.txt y calendar_dates.txt para el GTFS indicado."""
    horario = horario if horario is not None else GTFS_T05_SCHEDULE
    fecha_gtfs = fecha.strftime("%Y%m%d")
    for excepcion in horario.get("exceptions", []):
        if excepcion.get("service_id") == service_id and excepcion.get("date") == fecha_gtfs:
            return str(excepcion.get("exception_type")) == "1"

    servicio = horario.get("services", {}).get(service_id)
    if not servicio:
        return False

    try:
        inicio = datetime.strptime(
            str(servicio.get("start_date", "")), "%Y%m%d"
        ).date()
        fin = datetime.strptime(
            str(servicio.get("end_date", "")), "%Y%m%d"
        ).date()
    except (TypeError, ValueError):
        return False
    if not inicio <= fecha <= fin:
        return False

    campos_dias = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    return str(servicio.get(campos_dias[fecha.weekday()], "0")) == "1"





def obtener_vigencia_gtfs(horario):
    """Devuelve la última fecha cubierta por los calendarios y excepciones GTFS."""
    fechas = []
    for servicio in horario.get("services", {}).values():
        try:
            fechas.append(datetime.strptime(
                str(servicio.get("end_date", "")), "%Y%m%d"
            ).date())
        except (TypeError, ValueError):
            pass
    for excepcion in horario.get("exceptions", []):
        try:
            fechas.append(datetime.strptime(
                str(excepcion.get("date", "")), "%Y%m%d"
            ).date())
        except (TypeError, ValueError):
            pass
    return max(fechas) if fechas else None


def obtener_proxima_fecha_servicio_gtfs(horario, service_ids, fecha_inicio, fecha_fin):
    """
    Busca el primer día posterior con servicio para alguno de los calendarios
    asociados a esta parada. Aplica calendar_dates.txt antes del calendario semanal.
    """
    ids = {str(service_id) for service_id in service_ids if service_id}
    if not ids or fecha_inicio > fecha_fin:
        return None

    excepciones = {}
    for excepcion in horario.get("exceptions", []):
        clave = (
            str(excepcion.get("service_id", "")),
            str(excepcion.get("date", ""))
        )
        # Mantener la primera coincidencia, igual que servicio_activo_gtfs.
        if clave not in excepciones:
            excepciones[clave] = str(excepcion.get("exception_type", ""))

    campos_dias = (
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday"
    )
    servicios_preparados = {}
    for service_id in ids:
        servicio = horario.get("services", {}).get(service_id)
        if not servicio:
            servicios_preparados[service_id] = None
            continue
        try:
            inicio = datetime.strptime(
                str(servicio.get("start_date", "")), "%Y%m%d"
            ).date()
            fin = datetime.strptime(
                str(servicio.get("end_date", "")), "%Y%m%d"
            ).date()
            servicios_preparados[service_id] = (servicio, inicio, fin)
        except (TypeError, ValueError):
            servicios_preparados[service_id] = None

    fecha = fecha_inicio
    while fecha <= fecha_fin:
        fecha_gtfs = fecha.strftime("%Y%m%d")
        for service_id in ids:
            excepcion = excepciones.get((service_id, fecha_gtfs))
            if excepcion is not None:
                if excepcion == "1":
                    return fecha
                continue

            preparado = servicios_preparados.get(service_id)
            if not preparado:
                continue
            servicio, inicio, fin = preparado
            if inicio <= fecha <= fin and str(
                servicio.get(campos_dias[fecha.weekday()], "0")
            ) == "1":
                return fecha
        fecha += timedelta(days=1)

    return None

def normalizar_nombre_parada_gtfs(valor):
    """Normaliza nombres para comparar destinos aunque GTFS añada puntuación."""
    texto = unicodedata.normalize("NFKD", str(valor or "").casefold())
    sin_acentos = "".join(
        caracter for caracter in texto if not unicodedata.combining(caracter)
    )
    # En GTFS algunos destinos llevan un punto final (p. ej. «Castejón de
    # Monegros.») y el nombre de la parada no. Convertimos la puntuación en
    # espacios para que ambos nombres se comparen como la misma localidad.
    return " ".join(
        "".join(caracter if caracter.isalnum() or caracter.isspace() else " "
                for caracter in sin_acentos).split()
    )


def obtener_ultima_parada_id_viaje_gtfs(dataset, trip_id):
    """Devuelve el stop_id terminal de un viaje usando stop_sequence del lookup."""
    secuencias = dataset.get("lookup", {}).get("trips", {}).get(str(trip_id), {})
    if not isinstance(secuencias, dict) or not secuencias:
        return None
    try:
        ultima_secuencia = max(secuencias, key=lambda valor: int(valor))
    except (TypeError, ValueError):
        ultima_secuencia = max(secuencias)
    stop_id = secuencias.get(ultima_secuencia)
    return str(stop_id) if stop_id else None


def es_ultima_parada_viaje_gtfs(dataset, registro):
    """Indica si el registro corresponde al término real de esa expedición."""
    stop_id_final = obtener_ultima_parada_id_viaje_gtfs(
        dataset, registro.get("trip_id")
    )
    return bool(
        stop_id_final and
        str(registro.get("stop_id") or "") == stop_id_final
    )


def obtener_destino_final_viaje_gtfs(dataset, trip_id):
    """Obtiene el nombre de la última parada del viaje."""
    stop_id = obtener_ultima_parada_id_viaje_gtfs(dataset, trip_id)
    if not stop_id:
        return None
    return dataset.get("lookup", {}).get("stops", {}).get(stop_id)


def resolver_destino_horario_gtfs(concesion, registro, info_ruta, dataset):
    """
    Devuelve el destino concreto del viaje, no el nombre general de la línea.

    Algunos GTFS publican como trip_headsign el nombre completo del corredor
    (por ejemplo, «Teruel - Zaragoza»), que no distingue el sentido. En ese
    caso se usa la última parada del viaje. Los headsign específicos, como
    los que ya utiliza T05, se conservan.
    """
    headsign = str(registro.get("headsign") or "").strip()
    nombre_ruta = str(info_ruta.get("long_name") or "").strip()

    headsign_generico = (
        not headsign or
        (nombre_ruta and headsign.casefold() == nombre_ruta.casefold())
    )
    if headsign_generico and str(concesion).upper() != "T05":
        destino_final = obtener_destino_final_viaje_gtfs(
            dataset, registro.get("trip_id")
        )
        if destino_final:
            return destino_final

    return headsign or nombre_ruta or "Destino no disponible"


def segundos_horario_gtfs(valor):
    """Convierte una hora GTFS, admitiendo horas superiores a 24:00."""
    try:
        horas, minutos, segundos = (int(parte) for parte in str(valor).split(":"))
        if horas < 0 or minutos not in range(60) or segundos not in range(60):
            return None
        return horas * 3600 + minutos * 60 + segundos
    except (TypeError, ValueError):
        return None


@app.route('/api/paradas/<stop_id>/horarios')
def get_horarios_parada(stop_id):
    """Devuelve salidas programadas para la concesión solicitada."""
    concesion = request.args.get("concesion", "T05").upper()
    dataset = obtener_dataset_gtfs(concesion)
    horario = dataset["schedule"]
    ids_disponibles = set()
    for paradas in dataset["map"].get("stops", {}).values():
        for parada in paradas:
            ids_disponibles.update(
                str(valor) for valor in (parada.get("stop_ids") or [
                    parada.get("id") or parada.get("stop_id") or ""
                ]) if valor
            )
    ids_solicitados = {str(stop_id)}
    ids_parametro = request.args.get("stop_ids", "")
    ids_solicitados.update(valor.strip() for valor in ids_parametro.split(",") if valor.strip())
    ids_consultados = ids_solicitados.intersection(ids_disponibles)
    if str(stop_id) not in ids_disponibles:
        return jsonify({"error": "Parada no encontrada", "horarios": []}), 404

    # Nombres del marcador fusionado: se ocultan los servicios cuyo destino
    # vuelve a ser exactamente esta misma parada/estación.
    nombres_parada_marcada = set()
    for paradas in dataset["map"].get("stops", {}).values():
        for parada in paradas:
            ids_parada = {
                str(valor) for valor in (parada.get("stop_ids") or [
                    parada.get("id") or parada.get("stop_id") or ""
                ]) if valor
            }
            if ids_parada.intersection(ids_consultados):
                nombres_parada_marcada.add(
                    normalizar_nombre_parada_gtfs(parada.get("name"))
                )

    vigencia_fecha = obtener_vigencia_gtfs(horario)
    vigencia_hasta = vigencia_fecha.isoformat() if vigencia_fecha else None

    fecha_texto = request.args.get("fecha")
    try:
        fecha = (
            datetime.strptime(fecha_texto, "%Y-%m-%d").date()
            if fecha_texto else datetime.now(ZONA_HORARIA).date()
        )
    except ValueError:
        return jsonify({
            "error": "Fecha no válida",
            "horarios": [],
            "vigencia_gtfs_hasta": vigencia_hasta
        }), 400

    hoy = datetime.now(ZONA_HORARIA).date()
    fecha_fuera_de_vigencia = (
        fecha > vigencia_fecha if vigencia_fecha else fecha > hoy
    )
    if fecha < hoy or fecha_fuera_de_vigencia:
        limite = (
            " El horario GTFS está disponible hasta " + vigencia_hasta + "."
            if vigencia_hasta else " No hay una vigencia futura de horarios disponible."
        )
        return jsonify({
            "error": "La fecha debe estar entre hoy y el final de la vigencia GTFS." + limite,
            "horarios": [],
            "vigencia_gtfs_hasta": vigencia_hasta
        }), 400

    ahora = datetime.now(ZONA_HORARIA)
    horarios = []
    servicios_en_parada = set()
    for registro in horario.get("stop_times", []):
        if str(registro.get("stop_id")) not in ids_consultados:
            continue
        # La última parada de cada expedición no es una salida hacia sí misma.
        if es_ultima_parada_viaje_gtfs(dataset, registro):
            continue

        segundos = segundos_horario_gtfs(
            registro.get("departure_time") or registro.get("arrival_time")
        )
        if segundos is None:
            continue

        info_ruta = horario.get("routes", {}).get(registro.get("route_id"), {})
        destino = resolver_destino_horario_gtfs(
            concesion, registro, info_ruta, dataset
        )
        if normalizar_nombre_parada_gtfs(destino) in nombres_parada_marcada:
            continue

        service_id = registro.get("service_id")
        if service_id:
            servicios_en_parada.add(str(service_id))
        if not servicio_activo_gtfs(service_id, fecha, horario):
            continue

        instante = datetime.combine(
            fecha, datetime.min.time(), tzinfo=ZONA_HORARIA
        ) + timedelta(seconds=segundos)
        if fecha == hoy and instante < ahora - timedelta(minutes=1):
            continue

        horarios.append({
            "linea": info_ruta.get("short_name", concesion),
            "destino": destino,
            "hora": instante.strftime("%H:%M"),
            "timestamp": int(instante.timestamp()),
            "direccion": str(registro.get("direction_id", "")),
            "trip_id": registro.get("trip_id"),
            "fuente": "Horario oficial"
        })

    horarios.sort(key=lambda item: item["timestamp"])
    proxima_fecha = None
    if not horarios and vigencia_fecha and fecha < vigencia_fecha:
        proxima_fecha = obtener_proxima_fecha_servicio_gtfs(
            horario,
            servicios_en_parada,
            fecha + timedelta(days=1),
            vigencia_fecha
        )

    return jsonify({
        "fecha": fecha.isoformat(),
        "horarios": horarios,
        "vigencia_gtfs_hasta": vigencia_hasta,
        "proxima_fecha_disponible": (
            proxima_fecha.isoformat() if proxima_fecha else None
        ),
        "mensaje": None if horarios else "El GTFS no contiene salidas para esta parada en la fecha seleccionada."
    })


@app.route('/api/buses/<linea>')
def get_buses(linea):
    linea_objetivo = linea.upper()
    print(f"-> Consultando API de autobuses para la concesión {linea_objetivo}...")
    fuentes = FUENTES_GTFSRT_POR_CONCESION.get(linea_objetivo, {})
    if not fuentes:
        return jsonify([])

    dataset = obtener_dataset_gtfs(linea_objetivo)
    nombres_rutas = {
        route_id: info.get("short_name", linea_objetivo)
        for route_id, info in dataset["map"].get("routes", {}).items()
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': '*/*'
    }

    estimaciones = obtener_estimaciones_tripupdates(linea_objetivo)
    rutas_validas = set(dataset["map"].get("routes", {}))
    buses_por_identidad = {}
    feed_disponible = False

    for url in fuentes.get("vehiclepositions", []):
        try:
            res = obtener_respuesta_cacheada(
                url, "vehiclepositions", 5, headers, 8
            )
            if res.status_code != 200 or not res.content:
                continue

            content = res.content
            if content.startswith(b'\x1f\x8b'):
                try:
                    content = gzip.decompress(content)
                except (OSError, EOFError):
                    continue

            from google.transit import gtfs_realtime_pb2
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)
            feed_disponible = True

            for entity in feed.entity:
                if not entity.HasField('vehicle'):
                    continue

                v = entity.vehicle
                r_id = str(v.trip.route_id) if v.HasField('trip') else ""
                trip_id = str(v.trip.trip_id) if v.HasField('trip') else ""
                coche_num = (
                    str(v.vehicle.label)
                    if v.HasField('vehicle') and v.vehicle.label else ""
                )
                matricula = (
                    str(v.vehicle.license_plate)
                    if v.HasField('vehicle') and hasattr(v.vehicle, 'license_plate')
                    else ""
                )
                entity_id = str(entity.id or "")
                vehicle_id = (
                    str(v.vehicle.id)
                    if v.HasField('vehicle') and v.vehicle.id else ""
                )
                vehicle_key = vehicle_id or coche_num or entity_id
                direction_id = (
                    v.trip.direction_id
                    if v.HasField('trip') and v.trip.HasField('direction_id')
                    else None
                )

                if r_id not in rutas_validas:
                    continue

                lat = v.position.latitude if v.HasField('position') else None
                lon = v.position.longitude if v.HasField('position') else None
                raw_occ = leer_campo_protobuf_opcional(v, 'occupancy_status')
                raw_occ_percentage = leer_campo_protobuf_opcional(v, 'occupancy_percentage')
                occ_texto, occ_color = procesar_ocupacion(
                    raw_occ, raw_occ_percentage
                )
                velocidad_mps = (
                    leer_campo_protobuf_opcional(v.position, 'speed')
                    if v.HasField('position') else None
                )
                velocidad_kmh = convertir_velocidad_kmh(velocidad_mps)
                dest_defecto, _ = resolver_destino_y_parada(
                    linea_objetivo, r_id, direction_id, None
                )

                # El viaje identifica la expedición de forma más precisa que
                # el vehículo, cuyo identificador puede reutilizarse.
                info_est = (
                    estimaciones.get(trip_id) or
                    estimaciones.get(vehicle_id) or
                    estimaciones.get(coche_num) or
                    {
                        'retraso': 'Sin datos',
                        'retraso_segundos': None,
                        'proxima_parada': 'En trayecto',
                        'destino': dest_defecto,
                    }
                )

                destino_final = info_est.get('destino')
                if not destino_final or destino_final in ['Desconocido', 'En trayecto']:
                    destino_final = dest_defecto

                coordenadas = validar_coordenadas(lat, lon)
                if coordenadas is None:
                    continue
                lat, lon = coordenadas

                bus = {
                    'id': vehicle_key,
                    'trip_id': trip_id,
                    'linea': nombres_rutas.get(r_id, linea_objetivo),
                    'coche': coche_num,
                    'matricula': matricula,
                    'lat': float(lat),
                    'lon': float(lon),
                    'ocupacion': occ_texto,
                    'ocupacion_color': occ_color,
                    'velocidad_kmh': velocidad_kmh,
                    'retraso': info_est.get('retraso', 'Sin datos'),
                    'retraso_segundos': info_est.get('retraso_segundos'),
                    'proxima_parada': info_est.get('proxima_parada', 'En trayecto'),
                    'destino': destino_final,
                }

                if trip_id:
                    identidad_bus = ("trip", r_id, trip_id)
                elif vehicle_id or coche_num:
                    identidad_bus = ("vehicle", r_id, vehicle_id or coche_num)
                elif entity_id:
                    identidad_bus = (
                        "entity", r_id, entity_id,
                        round(float(lat), 6), round(float(lon), 6)
                    )
                else:
                    identidad_bus = (
                        "position", url, r_id, round(float(lat), 6), round(float(lon), 6)
                    )

                # Las fuentes se procesan por orden de prioridad: se conserva
                # el primer dato de cada autobús y se añaden los no repetidos.
                buses_por_identidad.setdefault(identidad_bus, bus)

        except Exception as exc:
            print(f"[API ERROR {url}]: {exc}")

    if buses_por_identidad:
        return jsonify(list(buses_por_identidad.values()))
    if feed_disponible:
        return jsonify([])
    return jsonify({
        "error": "No se pudo conectar con ningún feed de posiciones GTFS-RT."
    }), 503


# ─────────────────────────────────────────────────────────────────────────────
# AVANZA ZARAGOZA · BARRIDO DE TODA LA RED DE POSTES
#
# La API GPS no publica un volcado global documentado. La estrategia es:
#   1) Leer de la web de Avanza las líneas y los postes de cada sentido.
#   2) Consultar fRefrescaEmpresaExternos una vez por cada poste (coche=0).
#   3) Agrupar las observaciones por número de coche para pintar cada bus una vez.
#
# Un coche puede no aparecer en los dos próximos de un poste, pero sí en otro
# poste de la misma línea. El barrido de toda la red amplía mucho la cobertura.
# La fuente sigue limitando lo que anuncia por poste: no se puede prometer que
# publique todos los coches en cualquier instante.
# ─────────────────────────────────────────────────────────────────────────────

AVANZA_URBANOS_ENDPOINT = (
    "https://gps.avanzabus.com/index.php/zaragoza/fRefrescaEmpresaExternos"
)
AVANZA_HTTP_USER_AGENT = "MapaTPA/1.1 (+https://github.com/danigar2205/mapa-tpa)"
AVANZA_LINEAS_URL = "https://zaragoza.avanzagrupo.com/lineas-y-horarios/"
AVANZA_AJAX_URL = "https://zaragoza.avanzagrupo.com/wp-admin/admin-ajax.php"

AVANZA_URBANOS_TTL = 360
# Incrementar cuando cambie el significado de los datos de un barrido.
# La caché anterior se considera histórica y no se mezcla con el siguiente ciclo.
AVANZA_SCAN_VERSION = 10
# Incrementar cuando cambie la forma de descubrir recorridos/paradas de Avanza.
# La caché antigua puede omitir líneas aunque el GPS se haya actualizado.
AVANZA_CATALOGO_VERSION = 2
AVANZA_CATALOGO_POSTES_TTL = 21600
AVANZA_PAUSA_RECORRIDOS = 0.0
AVANZA_PAUSA_POSTES = 0.0
# La consulta de recorridos va separada para reservar capacidad al GPS de paradas.
AVANZA_CONCURRENCIA_RECORRIDOS = 12
# El GPS se consulta poste a poste. Evitamos acumular conexiones abiertas cuando la fuente tarda.
AVANZA_CONCURRENCIA_GPS = 8
AVANZA_GPS_TIMEOUT_CONNECT = 4.0
AVANZA_GPS_TIMEOUT_READ = 8.0
# Ritmo máximo moderado: permite terminar antes el barrido completo de la red
# sin lanzar ráfagas ilimitadas contra el GPS de Avanza.
AVANZA_GPS_PETICIONES_POR_SEGUNDO = 4.0
AVANZA_GPS_REINTENTOS_FINALES = 2
AVANZA_GPS_REFRESCOS_POR_LOTE = 1
AVANZA_GPS_SEGUIMIENTO_LOTE = 14
AVANZA_GPS_SEGUIMIENTO_NOVEDADES = 8
AVANZA_GPS_SEGUIMIENTO_INTERVALO = 6.0
AVANZA_GPS_BUS_ANTIGUEDAD_MAX = 300.0

try:
    AVANZA_URBANOS_TTL = max(120, int(os.environ.get("AVANZA_URBANOS_TTL", AVANZA_URBANOS_TTL)))
except (TypeError, ValueError):
    pass
try:
    AVANZA_PAUSA_RECORRIDOS = max(0.0, float(os.environ.get("AVANZA_PAUSA_RECORRIDOS", AVANZA_PAUSA_RECORRIDOS)))
    AVANZA_PAUSA_POSTES = max(0.0, float(os.environ.get("AVANZA_PAUSA_POSTES", AVANZA_PAUSA_POSTES)))
    AVANZA_CONCURRENCIA_RECORRIDOS = min(16, max(1, int(os.environ.get("AVANZA_CONCURRENCIA_RECORRIDOS", AVANZA_CONCURRENCIA_RECORRIDOS))))
    AVANZA_CONCURRENCIA_GPS = min(8, max(1, int(os.environ.get("AVANZA_CONCURRENCIA_GPS", AVANZA_CONCURRENCIA_GPS))))
    AVANZA_GPS_PETICIONES_POR_SEGUNDO = min(4.0, max(1.0, float(os.environ.get("AVANZA_GPS_PETICIONES_POR_SEGUNDO", AVANZA_GPS_PETICIONES_POR_SEGUNDO))))
    AVANZA_GPS_REINTENTOS_FINALES = min(3, max(1, int(os.environ.get("AVANZA_GPS_REINTENTOS_FINALES", AVANZA_GPS_REINTENTOS_FINALES))))
except (TypeError, ValueError):
    pass
try:
    AVANZA_GPS_TIMEOUT_CONNECT = max(1.0, min(10.0, float(os.environ.get("AVANZA_GPS_TIMEOUT_CONNECT", AVANZA_GPS_TIMEOUT_CONNECT))))
    AVANZA_GPS_TIMEOUT_READ = max(1.0, min(10.0, float(os.environ.get("AVANZA_GPS_TIMEOUT_READ", AVANZA_GPS_TIMEOUT_READ))))
except (TypeError, ValueError):
    pass

AVANZA_URBANOS_CACHE = {"timestamp": 0.0, "data": None}
AVANZA_URBANOS_LOCK = threading.Lock()
AVANZA_URBAN_SCAN_LOCK = threading.Lock()
AVANZA_CATALOGO_POSTES_CACHE = {
    "version": 0,
    "timestamp": 0.0,
    "postes": [],
    "lineas": 0,
    "sentidos": 0,
    "errores": [],
}
AVANZA_URBANOS_STATE = {
    "escaneo_en_curso": False,
    "fase": "pendiente",
    "progreso": 0,
    "total_progreso": 0,
    "progreso_general": 0,
    "total_progreso_general": 0,
    "sentidos_consultados": 0,
    "total_sentidos": 0,
    "paradas_gps_completadas": 0,
    "paradas_gps_intentadas": 0,
    "paradas_gps_con_error": 0,
    "ultimo_error_gps": None,
    "coches_unicos_detectados": 0,
    "buses_parciales": [],
    "lineas_descubiertas": 0,
    "paradas_descubiertas": 0,
    "seguimiento_en_curso": False,
    "actualizado": None,
    "actualizado_epoch": None,
    "heartbeat_epoch": None,
    "ultimo_error": None,
    "inicio_escaneo": None,
    "fin_escaneo": None,
}

AVANZA_SHARED_STATE_PATH = os.environ.get(
    "AVANZA_SHARED_STATE_PATH",
    "/tmp/mapa-tpa-avanza-state.json",
)
AVANZA_SHARED_LOCK_PATH = os.environ.get(
    "AVANZA_SHARED_LOCK_PATH",
    "/tmp/mapa-tpa-avanza-scan.lock",
)
AVANZA_SHARED_STATE_DATA_PATH = os.environ.get(
    "AVANZA_SHARED_STATE_DATA_PATH", AVANZA_SHARED_STATE_PATH + ".state"
)
AVANZA_SHARED_CACHE_DATA_PATH = os.environ.get(
    "AVANZA_SHARED_CACHE_DATA_PATH", AVANZA_SHARED_STATE_PATH + ".cache"
)
AVANZA_SHARED_CATALOG_DATA_PATH = os.environ.get(
    "AVANZA_SHARED_CATALOG_DATA_PATH", AVANZA_SHARED_STATE_PATH + ".catalogo"
)
AVANZA_SHARED_FILES_MIGRATED = False
AVANZA_SHARED_WRITE_LOCK = threading.Lock()
AVANZA_REDIS_URL = (
    os.environ.get("AVANZA_REDIS_URL", "").strip()
    or os.environ.get("REDIS_URL", "").strip()
)
AVANZA_REDIS_STATE_KEY = os.environ.get(
    "AVANZA_REDIS_STATE_KEY", "mapa-tpa:avanza:shared-state"
)
AVANZA_REDIS_STATE_DATA_KEY = os.environ.get(
    "AVANZA_REDIS_STATE_DATA_KEY", AVANZA_REDIS_STATE_KEY + ":state"
)
AVANZA_REDIS_CACHE_DATA_KEY = os.environ.get(
    "AVANZA_REDIS_CACHE_DATA_KEY", AVANZA_REDIS_STATE_KEY + ":cache"
)
AVANZA_REDIS_CATALOG_DATA_KEY = os.environ.get(
    "AVANZA_REDIS_CATALOG_DATA_KEY", AVANZA_REDIS_STATE_KEY + ":catalogo"
)
AVANZA_REDIS_MIGRATED = False
AVANZA_REDIS_LOCK_KEY = os.environ.get(
    "AVANZA_REDIS_LOCK_KEY", "mapa-tpa:avanza:scan-lock"
)
AVANZA_REDIS_CLIENT = None
AVANZA_REDIS_ACTIVE_LOCK_TOKEN = None
AVANZA_REDIS_LOCK_TTL = 60

if redis is not None and AVANZA_REDIS_URL:
    AVANZA_REDIS_CLIENT = redis.Redis.from_url(
        AVANZA_REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )


def _renovar_bloqueo_avanza_redis():
    """Renueva el alquiler del bloqueo solo si esta exploración sigue siendo dueña."""
    token = AVANZA_REDIS_ACTIVE_LOCK_TOKEN
    if AVANZA_REDIS_CLIENT is None or not token:
        return
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('expire', KEYS[1], ARGV[2]) "
        "else return 0 end"
    )
    try:
        AVANZA_REDIS_CLIENT.eval(
            script, 1, AVANZA_REDIS_LOCK_KEY, token, AVANZA_REDIS_LOCK_TTL
        )
    except Exception as exc:
        print(f"[AVANZA URBANOS] No se pudo renovar el bloqueo Redis: {exc}")


def _leer_json_avanza(path):
    try:
        with open(path, "r", encoding="utf-8") as fichero:
            valor = json.load(fichero)
        return valor
    except (OSError, ValueError, TypeError):
        return None


def _escribir_json_avanza_atomico(path, valor):
    directorio = os.path.dirname(path) or "."
    os.makedirs(directorio, exist_ok=True)
    temporal = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temporal, "w", encoding="utf-8") as fichero:
            json.dump(valor, fichero, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporal, path)
    finally:
        try:
            if os.path.exists(temporal):
                os.remove(temporal)
        except OSError:
            pass


def _leer_avanza_estado_compartido(incluir_catalogo=True):
    """Lee estado/caché pequeños; carga el catálogo solo cuando es necesario."""
    if AVANZA_REDIS_CLIENT is not None:
        try:
            pipeline = AVANZA_REDIS_CLIENT.pipeline(transaction=False)
            pipeline.get(AVANZA_REDIS_STATE_DATA_KEY)
            pipeline.get(AVANZA_REDIS_CACHE_DATA_KEY)
            if incluir_catalogo:
                pipeline.get(AVANZA_REDIS_CATALOG_DATA_KEY)
            if not AVANZA_REDIS_MIGRATED:
                pipeline.get(AVANZA_REDIS_STATE_KEY)
            raws = pipeline.execute()
            cursor = 0

            def decodificar(raw):
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except (TypeError, ValueError):
                    return None

            estado_nuevo = decodificar(raws[cursor]); cursor += 1
            cache_nueva = decodificar(raws[cursor]); cursor += 1
            catalogo_nuevo = None
            if incluir_catalogo:
                catalogo_nuevo = decodificar(raws[cursor]); cursor += 1
            legacy = {}
            if not AVANZA_REDIS_MIGRATED:
                legacy = decodificar(raws[cursor]) or {}
            legacy = legacy if isinstance(legacy, dict) else {}
            resultado = {
                "state": estado_nuevo if isinstance(estado_nuevo, dict) else legacy.get("state", {}),
                "cache": cache_nueva if isinstance(cache_nueva, dict) else legacy.get("cache", {}),
            }
            if incluir_catalogo:
                resultado["catalogo"] = (
                    catalogo_nuevo if isinstance(catalogo_nuevo, dict)
                    else legacy.get("catalogo", {})
                )
            return resultado
        except Exception as exc:
            print(f"[AVANZA URBANOS] No se pudo leer el estado Redis: {exc}")
            return {}

    legacy = _leer_json_avanza(AVANZA_SHARED_STATE_PATH)
    legacy = legacy if isinstance(legacy, dict) else {}
    hay_ficheros_separados = any(os.path.exists(path) for path in (
        AVANZA_SHARED_STATE_DATA_PATH,
        AVANZA_SHARED_CACHE_DATA_PATH,
        AVANZA_SHARED_CATALOG_DATA_PATH,
    ))
    if not hay_ficheros_separados:
        if not incluir_catalogo:
            legacy.pop("catalogo", None)
        return legacy

    estado = _leer_json_avanza(AVANZA_SHARED_STATE_DATA_PATH)
    cache = _leer_json_avanza(AVANZA_SHARED_CACHE_DATA_PATH)
    resultado = {
        "state": estado if isinstance(estado, dict) else legacy.get("state", {}),
        "cache": cache if isinstance(cache, dict) else legacy.get("cache", {}),
    }
    if incluir_catalogo:
        catalogo = _leer_json_avanza(AVANZA_SHARED_CATALOG_DATA_PATH)
        resultado["catalogo"] = (
            catalogo if isinstance(catalogo, dict) else legacy.get("catalogo", {})
        )
    return resultado




def _cache_avanza_sin_catalogo(cache):
    """Guarda solo flota y contadores; catálogos y listas largas viven aparte."""
    if not isinstance(cache, dict):
        return cache
    resultado = dict(cache)
    datos = resultado.get("data")
    if isinstance(datos, dict):
        datos_limpios = dict(datos)
        datos_limpios.pop("_postes_red", None)
        paradas_consultadas = datos_limpios.pop("paradas_consultadas", None)
        if isinstance(paradas_consultadas, list):
            datos_limpios["paradas_consultadas_count"] = len(paradas_consultadas)
        resultado["data"] = datos_limpios
    return resultado


def _preservar_solicitud_nuevo_barrido(estado, estado_compartido):
    """
    Evita que un worker antiguo borre una solicitud de reinicio publicada
    mientras ese mismo scan_id sigue escribiendo checkpoints.
    """
    resultado = dict(estado) if isinstance(estado, dict) else {}
    if not isinstance(estado_compartido, dict):
        return resultado
    if not estado_compartido.get("nuevo_barrido_solicitado"):
        return resultado

    scan_id_actual = str(resultado.get("scan_id") or "")
    scan_id_compartido = str(estado_compartido.get("scan_id") or "")
    if not scan_id_actual or scan_id_actual != scan_id_compartido:
        return resultado

    resultado["nuevo_barrido_solicitado"] = True
    try:
        resultado["nuevo_barrido_solicitado_epoch"] = max(
            float(resultado.get("nuevo_barrido_solicitado_epoch") or 0),
            float(estado_compartido.get("nuevo_barrido_solicitado_epoch") or 0),
        )
    except (TypeError, ValueError):
        resultado["nuevo_barrido_solicitado_epoch"] = (
            estado_compartido.get("nuevo_barrido_solicitado_epoch")
        )
    return resultado


def _guardar_avanza_estado_compartido(estado=None, cache=None, actualizar_cache=False, catalogo=None):
    """
    Actualiza solo el fichero o clave que ha cambiado. La flota y el catálogo
    ya no se vuelven a serializar en cada latido del seguimiento.
    """
    global AVANZA_REDIS_MIGRATED, AVANZA_SHARED_FILES_MIGRATED
    with AVANZA_SHARED_WRITE_LOCK:
        cache_guardada = (
            _cache_avanza_sin_catalogo(cache) if actualizar_cache else None
        )
        if estado is not None:
            estado = dict(estado)
            try:
                estado_compartido_actual = (
                    _leer_avanza_estado_compartido(incluir_catalogo=False).get("state") or {}
                )
                estado = _preservar_solicitud_nuevo_barrido(
                    estado, estado_compartido_actual
                )
            except Exception as exc:
                print(f"[AVANZA URBANOS] No se pudo comprobar el reinicio pendiente: {exc}")

        if AVANZA_REDIS_CLIENT is not None:
            try:
                pipeline = AVANZA_REDIS_CLIENT.pipeline(transaction=False)
                if not AVANZA_REDIS_MIGRATED:
                    contenido = _leer_avanza_estado_compartido()
                    if estado is not None:
                        contenido["state"] = estado
                    if actualizar_cache:
                        contenido["cache"] = cache_guardada
                    if catalogo is not None:
                        contenido["catalogo"] = catalogo
                    contenido["cache"] = _cache_avanza_sin_catalogo(
                        contenido.get("cache")
                    )

                    valores = (
                        (AVANZA_REDIS_STATE_DATA_KEY, contenido.get("state")),
                        (AVANZA_REDIS_CACHE_DATA_KEY, contenido.get("cache")),
                        (AVANZA_REDIS_CATALOG_DATA_KEY, contenido.get("catalogo")),
                    )
                    for clave, valor in valores:
                        if valor is None:
                            pipeline.delete(clave)
                        else:
                            pipeline.set(
                                clave,
                                json.dumps(valor, ensure_ascii=False, separators=(",", ":")),
                                ex=172800,
                            )
                    pipeline.delete(AVANZA_REDIS_STATE_KEY)
                    pipeline.execute()
                    AVANZA_REDIS_MIGRATED = True
                else:
                    if estado is not None:
                        pipeline.set(
                            AVANZA_REDIS_STATE_DATA_KEY,
                            json.dumps(estado, ensure_ascii=False, separators=(",", ":")),
                            ex=172800,
                        )
                    if actualizar_cache:
                        pipeline.set(
                            AVANZA_REDIS_CACHE_DATA_KEY,
                            json.dumps(cache_guardada, ensure_ascii=False, separators=(",", ":")),
                            ex=172800,
                        )
                    if catalogo is not None:
                        pipeline.set(
                            AVANZA_REDIS_CATALOG_DATA_KEY,
                            json.dumps(catalogo, ensure_ascii=False, separators=(",", ":")),
                            ex=172800,
                        )
                    pipeline.execute()
                _renovar_bloqueo_avanza_redis()
                return
            except Exception as exc:
                print(f"[AVANZA URBANOS] No se pudo guardar el estado Redis: {exc}")
                return

        if not AVANZA_SHARED_FILES_MIGRATED:
            contenido = _leer_avanza_estado_compartido()
            if estado is not None:
                contenido["state"] = estado
            if actualizar_cache:
                contenido["cache"] = cache_guardada
            if catalogo is not None:
                contenido["catalogo"] = catalogo
            contenido["cache"] = _cache_avanza_sin_catalogo(
                contenido.get("cache")
            )
            valores = (
                ("state", AVANZA_SHARED_STATE_DATA_PATH),
                ("cache", AVANZA_SHARED_CACHE_DATA_PATH),
                ("catalogo", AVANZA_SHARED_CATALOG_DATA_PATH),
            )
            for clave, ruta in valores:
                valor = contenido.get(clave)
                if valor is not None:
                    _escribir_json_avanza_atomico(ruta, valor)
            try:
                os.remove(AVANZA_SHARED_STATE_PATH)
            except OSError:
                pass
            AVANZA_SHARED_FILES_MIGRATED = True
        else:
            if estado is not None:
                _escribir_json_avanza_atomico(AVANZA_SHARED_STATE_DATA_PATH, estado)
            if actualizar_cache:
                _escribir_json_avanza_atomico(
                    AVANZA_SHARED_CACHE_DATA_PATH, cache_guardada
                )
            if catalogo is not None:
                _escribir_json_avanza_atomico(
                    AVANZA_SHARED_CATALOG_DATA_PATH, catalogo
                )



def _sincronizar_avanza_estado_compartido():
    """Actualiza las copias locales desde el estado publicado por el worker activo."""
    contenido = _leer_avanza_estado_compartido(incluir_catalogo=False)
    estado = contenido.get("state")
    cache = contenido.get("cache")
    with AVANZA_URBANOS_LOCK:
        if isinstance(estado, dict):
            AVANZA_URBANOS_STATE.update(estado)
        if isinstance(cache, dict):
            datos_cache = cache.get("data")
            timestamp_cache = cache.get("timestamp")
            if isinstance(datos_cache, dict) and isinstance(timestamp_cache, (int, float)):
                AVANZA_URBANOS_CACHE.update({
                    "data": datos_cache,
                    "timestamp": float(timestamp_cache),
                })
        return (
            dict(AVANZA_URBANOS_CACHE.get("data") or {}),
            dict(AVANZA_URBANOS_STATE),
            float(AVANZA_URBANOS_CACHE.get("timestamp") or 0.0),
        )


def _adquirir_bloqueo_avanza_compartido():
    """
    Bloqueo distribuido en Redis si está configurado; en caso contrario, usa
    flock para coordinar los procesos de una misma instancia Render.
    """
    if AVANZA_REDIS_CLIENT is not None:
        token = str(uuid.uuid4())
        try:
            adquirido = AVANZA_REDIS_CLIENT.set(
                AVANZA_REDIS_LOCK_KEY,
                token,
                nx=True,
                ex=AVANZA_REDIS_LOCK_TTL,
            )
            return {"tipo": "redis", "token": token} if adquirido else None
        except Exception as exc:
            print(f"[AVANZA URBANOS] No se pudo adquirir el bloqueo Redis: {exc}")
            return None

    directorio = os.path.dirname(AVANZA_SHARED_LOCK_PATH) or "."
    os.makedirs(directorio, exist_ok=True)
    descriptor = os.open(AVANZA_SHARED_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        return None
    except Exception:
        os.close(descriptor)
        raise


def _liberar_bloqueo_avanza_compartido(descriptor):
    global AVANZA_REDIS_ACTIVE_LOCK_TOKEN
    if descriptor is None:
        return
    if isinstance(descriptor, dict) and descriptor.get("tipo") == "redis":
        token = descriptor.get("token")
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('del', KEYS[1]) "
            "else return 0 end"
        )
        try:
            if AVANZA_REDIS_CLIENT is not None and token:
                AVANZA_REDIS_CLIENT.eval(script, 1, AVANZA_REDIS_LOCK_KEY, token)
        except Exception as exc:
            print(f"[AVANZA URBANOS] No se pudo liberar el bloqueo Redis: {exc}")
        if AVANZA_REDIS_ACTIVE_LOCK_TOKEN == token:
            AVANZA_REDIS_ACTIVE_LOCK_TOKEN = None
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass





class AvanzaHTMLParser(HTMLParser):
    """Extrae selects y el nonce sin depender de expresiones HTML frágiles."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.selects = []
        self.select_actual = None
        self.option_actual = None
        self.nonce = None

    def handle_starttag(self, tag, attrs):
        atributos = {clave.lower(): (valor or "") for clave, valor in attrs}
        if tag.lower() == "input" and atributos.get("id") == "avz_bus_ajax_nonce":
            self.nonce = atributos.get("value") or self.nonce
        elif tag.lower() == "select":
            self.select_actual = {
                "id": atributos.get("id", ""),
                "name": atributos.get("name", ""),
                "aria-label": atributos.get("aria-label", ""),
                "options": [],
            }
            self.selects.append(self.select_actual)
        elif tag.lower() == "option" and self.select_actual is not None:
            self.option_actual = {
                "value": atributos.get("value", "").strip(),
                "texto": [],
            }

    def handle_data(self, data):
        if self.option_actual is not None:
            self.option_actual["texto"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "option" and self.option_actual is not None:
            self.option_actual["texto"] = re.sub(
                r"\s+", " ", "".join(self.option_actual["texto"])
            ).strip()
            if self.select_actual is not None:
                self.select_actual["options"].append(self.option_actual)
            self.option_actual = None
        elif tag.lower() == "select":
            self.select_actual = None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def parsear_html_avanza(texto):
    parser = AvanzaHTMLParser()
    parser.feed(str(texto or ""))
    parser.close()
    return parser


def _opciones_de_lineas_avanza(html_pagina):
    parser = parsear_html_avanza(html_pagina)
    candidatos = [
        select for select in parser.selects
        if "linea" in (
            str(select.get("id", "")) + " " +
            str(select.get("name", "")) + " " +
            str(select.get("aria-label", ""))
        ).lower()
    ]
    if not candidatos:
        candidatos = [
            select for select in parser.selects
            if len(select.get("options", [])) >= 10
        ]
    if not candidatos:
        return []

    opciones = []
    vistos = set()
    for select in candidatos:
        for opcion in select.get("options", []):
            valor = str(opcion.get("value") or "").strip()
            clave = valor.lower()
            if (
                not valor
                or clave in vistos
                or valor in {"0", "-1"}
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", valor)
            ):
                continue
            vistos.add(clave)
            opciones.append({
                "valor": valor,
                "nombre": opcion.get("texto") or valor,
            })
    return opciones


def _extraer_postes_de_recorrido(fragmento_html):
    parser = parsear_html_avanza("<select>" + str(fragmento_html or "") + "</select>")
    postes = {}
    for select in parser.selects:
        for opcion in select.get("options", []):
            valor = str(opcion.get("value") or "").strip()
            if not valor.isdigit():
                continue
            poste = int(valor)
            if poste <= 0:
                continue
            nombre = re.sub(r"^\d+\s*[-–]\s*", "", opcion.get("texto") or "").strip()
            if poste not in postes or (nombre and not postes[poste]):
                postes[poste] = nombre
    return postes


AVANZA_DESCUBRIMIENTO_LOCAL = threading.local()


def _sesion_descubrimiento_avanza():
    """Sesión HTTP persistente e independiente para cada trabajador de recorridos."""
    sesion = getattr(AVANZA_DESCUBRIMIENTO_LOCAL, "sesion", None)
    if sesion is None:
        sesion = requests.Session()
        sesion.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; MapaTPA/1.0)",
            "Accept": "*/*",
        })
        AVANZA_DESCUBRIMIENTO_LOCAL.sesion = sesion
    return sesion


def _consultar_sentido_recorrido_avanza(linea, sentido, nonce):
    """Consulta en paralelo un recorrido Avanza y devuelve sus postes o el error."""
    sesion = _sesion_descubrimiento_avanza()
    datos_peticion = {
        "action": "get_stops_list",
        "selectLinea": linea["valor"],
        "selectSentido": str(sentido),
        "nonce": nonce,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MapaTPA/1.0)",
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": AVANZA_LINEAS_URL,
        "Origin": "https://zaragoza.avanzagrupo.com",
    }
    try:
        respuesta = sesion.post(
            AVANZA_AJAX_URL,
            data=datos_peticion,
            headers=headers,
            timeout=(5, 12),
        )
        if respuesta.status_code == 403:
            nuevo_nonce, _ = obtener_nonce_y_lineas_avanza(sesion)
            datos_peticion["nonce"] = nuevo_nonce
            respuesta = sesion.post(
                AVANZA_AJAX_URL,
                data=datos_peticion,
                headers=headers,
                timeout=(5, 12),
            )
        if respuesta.status_code != 200:
            return {
                "linea": linea["valor"],
                "sentido": sentido,
                "postes": {},
                "error": f"línea {linea['valor']} sentido {sentido}: HTTP {respuesta.status_code}",
            }
        return {
            "linea": linea["valor"],
            "sentido": sentido,
            "postes": _extraer_postes_de_recorrido(respuesta.text),
            "error": None,
        }
    except (requests.RequestException, ValueError, TypeError) as exc:
        return {
            "linea": linea["valor"],
            "sentido": sentido,
            "postes": {},
            "error": f"línea {linea['valor']} sentido {sentido}: {str(exc)[:120]}",
        }


def obtener_nonce_y_lineas_avanza(http):
    """Carga el nonce WordPress y el selector de líneas actual de Avanza."""
    respuesta = http.get(
        AVANZA_LINEAS_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; MapaTPA/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=(5, 15),
    )
    respuesta.raise_for_status()
    parser = parsear_html_avanza(respuesta.text)
    nonce = parser.nonce
    if not nonce:
        raise ValueError("Avanza no ha publicado el nonce de su página de líneas.")
    lineas = _opciones_de_lineas_avanza(respuesta.text)
    if not lineas:
        raise ValueError("No se ha podido leer el selector de líneas de Avanza.")
    return nonce, lineas


class AvanzaBarridoInterrumpido(Exception):
    """El barrido actual se detuvo para ceder el turno a una recarga limpia."""


def _avanza_reinicio_pendiente():
    """Consulta la señal compartida de reinicio, también entre workers de Render."""
    try:
        contenido = _leer_avanza_estado_compartido(incluir_catalogo=False)
        estado = contenido.get("state") or {}
        if isinstance(estado, dict) and estado.get("nuevo_barrido_solicitado"):
            return True
    except Exception as exc:
        print(f"[AVANZA URBANOS] No se pudo comprobar el reinicio pendiente: {exc}")

    with AVANZA_URBANOS_LOCK:
        return bool(AVANZA_URBANOS_STATE.get("nuevo_barrido_solicitado"))


def descubrir_postes_avanza(http):
    """
    Descubre los postes de los recorridos Avanza y reanuda desde el último
    sentido completado cuando un proceso anterior se haya interrumpido.
    """
    ahora = time.monotonic()
    with AVANZA_URBANOS_LOCK:
        cacheada = AVANZA_CATALOGO_POSTES_CACHE
        if (
            cacheada["postes"]
            and _catalogo_avanza_es_compatible(cacheada)
            and ahora - cacheada["timestamp"] < AVANZA_CATALOGO_POSTES_TTL
        ):
            return (
                list(cacheada["postes"]),
                cacheada["lineas"],
                cacheada["sentidos"],
                list(cacheada["errores"]),
            )

    contenido_compartido = _leer_avanza_estado_compartido()
    estado_previo = contenido_compartido.get("state") or {}
    catalogo_compartido = contenido_compartido.get("catalogo") or {}
    actualizado_catalogo = catalogo_compartido.get("actualizado_epoch")
    try:
        edad_catalogo = max(0.0, time.time() - float(actualizado_catalogo))
    except (TypeError, ValueError):
        edad_catalogo = float("inf")

    postes_compartidos = catalogo_compartido.get("postes")
    if (
        isinstance(postes_compartidos, list)
        and postes_compartidos
        and _catalogo_avanza_es_compatible(catalogo_compartido)
        and edad_catalogo < AVANZA_CATALOGO_POSTES_TTL
    ):
        lineas_cacheadas = int(catalogo_compartido.get("lineas") or 0)
        sentidos_cacheados = int(catalogo_compartido.get("sentidos") or 0)
        errores_cacheados = list(catalogo_compartido.get("errores") or [])
        with AVANZA_URBANOS_LOCK:
            AVANZA_CATALOGO_POSTES_CACHE.update({
                "version": AVANZA_CATALOGO_VERSION,
                "timestamp": time.monotonic() - edad_catalogo,
                "postes": list(postes_compartidos),
                "lineas": lineas_cacheadas,
                "sentidos": sentidos_cacheados,
                "errores": errores_cacheados,
            })
        return (
            list(postes_compartidos),
            lineas_cacheadas,
            sentidos_cacheados,
            errores_cacheados,
        )

    nonce, lineas = obtener_nonce_y_lineas_avanza(http)
    tareas = [
        (linea, sentido)
        for linea in lineas
        for sentido in (-1, -2)
    ]
    total_sentidos = len(tareas)

    reanudar_descubrimiento = (
        bool(estado_previo.get("escaneo_en_curso"))
        and estado_previo.get("fase") == "descubriendo_paradas"
        and bool(estado_previo.get("scan_id"))
    )
    recorridos_completados = set(
        estado_previo.get("recorridos_completados") or []
    ) if reanudar_descubrimiento else set()
    postes_por_id = {
        str(poste): str(nombre or "")
        for poste, nombre in (
            (estado_previo.get("postes_catalogo_parcial") or {}).items()
            if reanudar_descubrimiento else []
        )
    }
    errores = list(
        estado_previo.get("errores_recorridos_parcial") or []
    ) if reanudar_descubrimiento else []

    lineas_terminadas = {
        clave.rsplit("|", 1)[0] for clave in recorridos_completados
    }
    sentidos_terminados = min(len(recorridos_completados), total_sentidos)
    tareas_pendientes = [
        (linea, sentido)
        for linea, sentido in tareas
        if f"{linea['valor']}|{sentido}" not in recorridos_completados
    ]

    _actualizar_estado_avanza(
        fase="descubriendo_paradas",
        progreso=sentidos_terminados,
        total_progreso=total_sentidos,
        progreso_general=sentidos_terminados,
        total_progreso_general=total_sentidos,
        sentidos_consultados=sentidos_terminados,
        total_sentidos=total_sentidos,
        paradas_gps_completadas=0,
        lineas_descubiertas=len(lineas_terminadas),
        paradas_descubiertas=len(postes_por_id),
        recorridos_completados=sorted(recorridos_completados),
        postes_catalogo_parcial=postes_por_id,
        errores_recorridos_parcial=errores,
    )

    ultimo_checkpoint_recorridos = time.monotonic()
    recorridos_desde_checkpoint = 0

    def publicar_checkpoint_recorridos(forzar=False):
        nonlocal ultimo_checkpoint_recorridos, recorridos_desde_checkpoint
        ahora = time.monotonic()
        if (
            not forzar
            and recorridos_desde_checkpoint < 5
            and ahora - ultimo_checkpoint_recorridos < 2.0
        ):
            return
        _actualizar_estado_avanza(
            fase="descubriendo_paradas",
            progreso=sentidos_terminados,
            total_progreso=total_sentidos,
            progreso_general=sentidos_terminados,
            total_progreso_general=total_sentidos,
            sentidos_consultados=sentidos_terminados,
            total_sentidos=total_sentidos,
            paradas_gps_completadas=0,
            lineas_descubiertas=len(lineas_terminadas),
            paradas_descubiertas=len(postes_por_id),
            recorridos_completados=sorted(recorridos_completados),
            postes_catalogo_parcial=postes_por_id,
            errores_recorridos_parcial=errores[-100:],
        )
        ultimo_checkpoint_recorridos = ahora
        recorridos_desde_checkpoint = 0

    with ThreadPoolExecutor(
        max_workers=AVANZA_CONCURRENCIA_RECORRIDOS,
        thread_name_prefix="avanza-recorridos",
    ) as executor:
        futuros = {
            executor.submit(
                _consultar_sentido_recorrido_avanza,
                linea,
                sentido,
                nonce,
            ): (linea, sentido)
            for linea, sentido in tareas_pendientes
        }

        for futuro in as_completed(futuros):
            if _avanza_reinicio_pendiente():
                for pendiente in futuros:
                    if not pendiente.done():
                        pendiente.cancel()
                raise AvanzaBarridoInterrumpido(
                    "Se ha interrumpido el descubrimiento para iniciar un barrido limpio."
                )
            try:
                resultado = futuro.result()
            except Exception as exc:
                linea, sentido = futuros[futuro]
                resultado = {
                    "linea": linea["valor"],
                    "sentido": sentido,
                    "postes": {},
                    "error": f"línea {linea['valor']} sentido {sentido}: {str(exc)[:120]}",
                }

            clave_recorrido = f"{resultado.get('linea')}|{resultado.get('sentido')}"
            recorridos_completados.add(clave_recorrido)
            sentidos_terminados = min(len(recorridos_completados), total_sentidos)
            lineas_terminadas.add(str(resultado.get("linea")))

            if resultado.get("error"):
                errores.append(resultado["error"])
            else:
                for poste, nombre in resultado.get("postes", {}).items():
                    poste = str(poste)
                    if poste not in postes_por_id or (nombre and not postes_por_id[poste]):
                        postes_por_id[poste] = nombre

            recorridos_desde_checkpoint += 1
            publicar_checkpoint_recorridos()

        publicar_checkpoint_recorridos(forzar=True)

    if not postes_por_id:
        raise ValueError(
            "Avanza no ha devuelto ningún poste en los recorridos. " +
            (" ".join(errores[:3]) if errores else "La estructura de respuesta ha cambiado.")
        )

    lista_postes = [
        {"poste": int(poste), "nombre": nombre or f"Parada {poste}"}
        for poste, nombre in sorted(
            postes_por_id.items(),
            key=lambda par: int(par[0]) if str(par[0]).isdigit() else str(par[0]),
        )
    ]
    with AVANZA_URBANOS_LOCK:
        AVANZA_CATALOGO_POSTES_CACHE.update({
            "version": AVANZA_CATALOGO_VERSION,
            "timestamp": time.monotonic(),
            "postes": lista_postes,
            "lineas": len(lineas),
            "sentidos": sentidos_terminados,
            "errores": errores[:20],
        })
    _guardar_avanza_estado_compartido(catalogo={
        "version": AVANZA_CATALOGO_VERSION,
        "actualizado_epoch": time.time(),
        "postes": lista_postes,
        "lineas": len(lineas),
        "sentidos": sentidos_terminados,
        "errores": errores[:20],
    })
    return lista_postes, len(lineas), sentidos_terminados, errores


def obtener_postes_avanza_urbanos(http):
    """
    Permite una lista manual opcional en AVANZA_URBAN_POSTES; por defecto,
    descubre automáticamente todos los postes desde los recorridos operativos.
    """
    raw = os.environ.get("AVANZA_URBAN_POSTES", "").strip()
    if raw:
        postes = []
        for fragmento in raw.split(","):
            try:
                poste = int(fragmento.strip())
            except (TypeError, ValueError):
                continue
            if poste > 0 and poste not in {item["poste"] for item in postes}:
                postes.append({"poste": poste, "nombre": f"Parada {poste}"})
        if postes:
            return postes, 0, 0, []
    try:
        return descubrir_postes_avanza(http)
    except Exception as exc:
        # Conserva el piloto conocido si Avanza cambia el formulario o el nonce.
        # Se muestra como cobertura limitada, nunca como flota completa.
        print(f"[AVANZA URBANOS] No se pudo descubrir la red completa: {exc}")
        return (
            [{"poste": 14, "nombre": "Parada 14 · cobertura limitada"}],
            0,
            0,
            [f"No se pudo descubrir el catálogo completo; se usa solo el poste 14: {str(exc)[:160]}"],
        )


def limpiar_texto_html_avanza(valor):
    """Convierte celdas HTML del popup de Avanza a texto plano."""
    texto = re.sub(r"<[^>]*>", " ", str(valor or ""))
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


class AvanzaLlegadasHTMLParser(HTMLParser):
    """Extrae línea, destino, coche y ETA de la tabla HTML incluida en tablatiempos."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.profundidad_li = 0
        self.bloque = None
        self.strong_activo = False
        self.strong_anidado = 0
        self.ancla = None
        self.llegadas = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        atributos = {clave.lower(): (valor or "") for clave, valor in attrs}

        if self.strong_activo:
            self.strong_anidado += 1
        elif tag == "strong" and self.profundidad_li == 1 and self.bloque is not None:
            self.strong_activo = True
            self.strong_anidado = 0
            self.bloque["cabecera"] = []

        if tag == "li":
            self.profundidad_li += 1
            if self.profundidad_li == 1:
                self.bloque = {"cabecera": [], "filas": []}
        elif tag == "a" and self.profundidad_li >= 2 and self.bloque is not None:
            self.ancla = {"href": atributos.get("href", ""), "texto": []}

    def handle_data(self, data):
        if self.ancla is not None:
            self.ancla["texto"].append(data)
        if (
            self.strong_activo
            and self.strong_anidado == 0
            and self.bloque is not None
        ):
            texto = str(data).strip()
            if texto:
                self.bloque["cabecera"].append(texto)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self.ancla is not None:
            if self.bloque is not None:
                self.bloque["filas"].append(self.ancla)
            self.ancla = None

        if self.strong_activo:
            if tag == "strong" and self.strong_anidado == 0:
                self.strong_activo = False
            else:
                self.strong_anidado = max(0, self.strong_anidado - 1)

        if tag == "li":
            if self.profundidad_li == 1 and self.bloque is not None:
                self._cerrar_bloque()
                self.bloque = None
            self.profundidad_li = max(0, self.profundidad_li - 1)

    def _cerrar_bloque(self):
        cabecera = " ".join(self.bloque.get("cabecera") or [])
        partes = re.sub(r"\s+", " ", cabecera).strip().split()
        if not partes:
            return
        linea = partes[0]
        destino = " ".join(partes[1:]).strip()
        if not linea:
            return

        for fila in self.bloque.get("filas") or []:
            texto = re.sub(r"\s+", " ", " ".join(fila.get("texto") or [])).strip()
            coincidencia_texto = re.match(r"^(\d+)", texto)
            coincidencia_eta = re.search(r"\[\s*(-?\d+)\s*min", texto, flags=re.I)
            coincidencia_href = re.search(
                r"/fParadas/\d+/(\d+)(?:[/?#].*)?$",
                str(fila.get("href") or "").strip(),
            )
            coche_texto = coincidencia_texto.group(1) if coincidencia_texto else None
            coche_href = coincidencia_href.group(1) if coincidencia_href else None
            if coche_texto and coche_href and coche_texto != coche_href:
                continue
            coche = coche_href or coche_texto
            if not coche:
                continue
            eta = int(coincidencia_eta.group(1)) if coincidencia_eta else None
            if eta is not None and not 0 <= eta <= 240:
                eta = None
            self.llegadas.append({
                "coche": coche,
                "linea": linea,
                "destino": destino or "Destino no indicado",
                "eta_minutos": eta,
            })


def extraer_llegadas_tablatiempos_avanza(fragmento_html):
    """Lee las llegadas declaradas por Avanza, independientemente del marcador GPS."""
    parser = AvanzaLlegadasHTMLParser()
    parser.feed(str(fragmento_html or ""))
    parser.close()
    # Un mismo coche puede repetirse en el HTML de un poste. Conservar la ETA
    # más cercana y la primera identidad de línea/destino coherente.
    unicas = {}
    for llegada in parser.llegadas:
        coche = str(llegada.get("coche") or "").strip()
        if not coche:
            continue
        anterior = unicas.get(coche)
        eta = llegada.get("eta_minutos")
        eta_anterior = anterior.get("eta_minutos") if anterior else None
        if anterior is None or (
            eta is not None and (eta_anterior is None or eta < eta_anterior)
        ):
            unicas[coche] = llegada
    return list(unicas.values())


def normalizar_linea_avanza(valor):
    """Normaliza códigos numéricos con ceros iniciales y conserva códigos alfanuméricos."""
    linea = str(valor or "").strip()
    if linea.isdigit():
        return str(int(linea))
    return linea.upper() or "?"


def buses_de_respuesta_gps_avanza(datos, poste, nombre_parada=None):
    """Combina llegadas oficiales del poste con coordenadas opcionales del mapa."""
    maquinas = datos.get("maquinas") or {}
    entradas = (
        list(maquinas.values()) if isinstance(maquinas, dict)
        else (maquinas if isinstance(maquinas, list) else [])
    )
    nombre_feed = None
    maquinas_por_coche = {}
    for maquina in entradas:
        if not isinstance(maquina, dict):
            continue
        icono = str(maquina.get("icon") or "").split("?")[0].rsplit("/", 1)[-1].lower()
        if icono == "bus_rojo.png":
            nombre_feed = limpiar_texto_html_avanza(
                maquina.get("info") or maquina.get("title")
            ) or nombre_feed
            continue
        bus = extraer_bus_avanza(maquina, poste, nombre_feed or nombre_parada)
        if bus:
            maquinas_por_coche[str(bus["coche"])] = bus

    nombre_parada = nombre_feed or nombre_parada or f"Parada {poste}"
    llegadas = extraer_llegadas_tablatiempos_avanza(datos.get("tablatiempos"))
    if not llegadas:
        # Compatibilidad defensiva si Avanza retira temporalmente tablatiempos:
        # conservamos lo que sí venga en máquinas, sin inventar datos.
        return [
            {**bus, "parada": nombre_parada}
            for bus in maquinas_por_coche.values()
        ]

    buses_por_coche = {}
    for llegada in llegadas:
        coche = str(llegada["coche"])
        bus = dict(maquinas_por_coche.get(coche) or {
            "id": coche,
            "coche": coche,
            "lat": None,
            "lon": None,
            "distancia_km": None,
            "poste": int(poste),
            "parada": nombre_parada,
            "capturado_epoch": time.time(),
        })
        bus.update({
            "id": coche,
            "coche": coche,
            "linea": normalizar_linea_avanza(llegada.get("linea")),
            "destino": str(llegada.get("destino") or "Destino no indicado"),
            "sentido": str(llegada.get("destino") or "Sentido no indicado"),
            "eta_minutos": llegada.get("eta_minutos"),
            "poste": int(poste),
            "parada": nombre_parada,
        })
        buses_por_coche[coche] = bus

    # Si la tabla omite una fila pero el mapa sigue publicando un bus con
    # identificador y línea válidos, lo conservamos como observación secundaria.
    for coche, bus in maquinas_por_coche.items():
        buses_por_coche.setdefault(coche, bus)
    return list(buses_por_coche.values())


def extraer_bus_avanza(maquina, poste, nombre_parada=None):
    """Interpreta una entrada bus.png de maquinas; ignora marcadores de parada."""
    if not isinstance(maquina, dict):
        return None

    icono = str(maquina.get("icon") or "").split("?")[0].rsplit("/", 1)[-1].lower()
    if icono != "bus.png":
        return None

    coordenadas = maquina.get("coordenadas") or {}
    punto = None
    if isinstance(coordenadas, dict):
        punto = coordenadas.get("0")
        if punto is None and coordenadas:
            punto = next(iter(coordenadas.values()))
    elif isinstance(coordenadas, list) and coordenadas:
        punto = coordenadas[0]
    if not isinstance(punto, dict):
        return None

    # La lista de llegadas es válida aunque Avanza no publique posición para
    # ese coche. En el panel de flota se debe conservar el vehículo y dejar sus
    # coordenadas vacías, nunca descartarlo por completo.
    validas = validar_coordenadas(punto.get("LAT"), punto.get("LON"))
    if validas is not None and validas[0] == 0 and validas[1] == 0:
        validas = None
    lat, lon = validas if validas is not None else (None, None)

    titulo = str(maquina.get("title") or "").strip()
    titulo_match = re.match(r"^([^\s]+)\s+(\d+)\s*$", titulo)
    linea = titulo_match.group(1) if titulo_match else ""
    coche = titulo_match.group(2) if titulo_match else ""

    celdas = [
        limpiar_texto_html_avanza(celda)
        for celda in re.findall(
            r"<td\b[^>]*>(.*?)</td>",
            str(maquina.get("info") or ""),
            flags=re.I | re.S,
        )
    ]
    destino = ""
    eta_minutos = None
    distancia_km = None

    if len(celdas) >= 8:
        coche = re.sub(r"^\D+", "", celdas[4]) or coche
        linea_destino = celdas[5]
        coincidencia = re.match(r"^\s*([^\s]+)\s*->\s*(.*?)\s*$", linea_destino)
        if coincidencia:
            linea = coincidencia.group(1) or linea
            destino = coincidencia.group(2).strip()
        eta_match = re.search(r"(-?\d+)\s*min", celdas[6], flags=re.I)
        if eta_match:
            eta_minutos = int(eta_match.group(1))
        distancia_match = re.search(r"([\d.,]+)\s*km", celdas[7], flags=re.I)
        if distancia_match:
            try:
                distancia_km = float(distancia_match.group(1).replace(",", "."))
            except ValueError:
                distancia_km = None

    if not coche:
        return None
    if not linea:
        linea = "?"
    return {
        "id": coche,
        "coche": coche,
        "linea": linea,
        "destino": destino or "Destino no indicado",
        "sentido": destino or "Sentido no indicado",
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "eta_minutos": eta_minutos,
        "distancia_km": distancia_km,
        "poste": int(poste),
        "parada": nombre_parada or f"Parada {poste}",
        "capturado_epoch": time.time(),
    }


def _actualizar_estado_avanza(**cambios):
    with AVANZA_URBANOS_LOCK:
        AVANZA_URBANOS_STATE.update(cambios)
        estado = dict(AVANZA_URBANOS_STATE)
    try:
        _guardar_avanza_estado_compartido(estado=estado)
    except OSError as exc:
        print(f"[AVANZA URBANOS] No se pudo guardar el progreso compartido: {exc}")


AVANZA_GPS_THREAD_LOCAL = threading.local()
AVANZA_GPS_RATE_LOCK = threading.Lock()
AVANZA_GPS_NEXT_REQUEST_AT = 0.0


def _esperar_turno_peticion_gps_avanza():
    """Espacia globalmente las peticiones GPS para evitar ráfagas al operador."""
    global AVANZA_GPS_NEXT_REQUEST_AT
    intervalo = 1.0 / max(1.0, AVANZA_GPS_PETICIONES_POR_SEGUNDO)
    with AVANZA_GPS_RATE_LOCK:
        ahora = time.monotonic()
        turno = max(ahora, AVANZA_GPS_NEXT_REQUEST_AT)
        AVANZA_GPS_NEXT_REQUEST_AT = turno + intervalo
    espera = turno - time.monotonic()
    if espera > 0:
        time.sleep(espera)


def _sesion_gps_avanza():
    """Una sesión HTTP persistente por trabajador, sin compartirla entre hilos."""
    sesion = getattr(AVANZA_GPS_THREAD_LOCAL, "sesion", None)
    if sesion is None:
        sesion = requests.Session()
        sesion.headers.update({
            "User-Agent": AVANZA_HTTP_USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        AVANZA_GPS_THREAD_LOCAL.sesion = sesion
    return sesion


def _consultar_poste_gps_avanza(parada_info, cancelar=None):
    """Consulta un poste GPS; permite cancelar tareas aún no enviadas al operador."""
    poste = parada_info["poste"]
    nombre_parada = parada_info.get("nombre") or f"Parada {poste}"
    sesion = _sesion_gps_avanza()
    headers = {}

    # Un único reintento para fallos transitorios. Si vence la primera lectura,
    # se reintenta una sola vez con un límite menor para no eternizar el barrido.
    for intento in range(2):
        if cancelar is not None and cancelar.is_set():
            return {
                "poste": poste, "nombre": nombre_parada, "buses": [],
                "error": None, "cancelada": True,
            }
        try:
            _esperar_turno_peticion_gps_avanza()
            # La espera del limitador puede durar varios segundos. Comprobar de
            # nuevo evita enviar una petición que ya no hace falta.
            if cancelar is not None and cancelar.is_set():
                return {
                    "poste": poste, "nombre": nombre_parada, "buses": [],
                    "error": None, "cancelada": True,
                }
            respuesta = sesion.post(
                AVANZA_URBANOS_ENDPOINT,
                data={"poste": str(poste), "coche": "0"},
                headers=headers,
                timeout=(
                    AVANZA_GPS_TIMEOUT_CONNECT,
                    AVANZA_GPS_TIMEOUT_READ if intento == 0
                    else min(4.0, AVANZA_GPS_TIMEOUT_READ),
                ),
            )
        except requests.exceptions.ReadTimeout as exc:
            if intento == 0:
                time.sleep(0.4)
                continue
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: {str(exc)[:160]}",
            }
        except requests.exceptions.Timeout as exc:
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: {str(exc)[:160]}",
            }
        except requests.exceptions.ConnectionError as exc:
            if intento == 0:
                time.sleep(0.35)
                continue
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: {str(exc)[:160]}",
            }
        except (requests.RequestException, ValueError, TypeError) as exc:
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: {str(exc)[:160]}",
            }

        if respuesta.status_code != 200:
            if intento == 0 and respuesta.status_code in {429, 500, 502, 503, 504}:
                try:
                    espera = min(1.5, max(0.25, float(respuesta.headers.get("Retry-After", 0.5))))
                except (TypeError, ValueError):
                    espera = 0.5
                time.sleep(espera)
                continue
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: HTTP {respuesta.status_code}",
            }

        try:
            datos = respuesta.json()
        except ValueError:
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: respuesta GPS no válida",
            }

        if not isinstance(datos, dict) or not (
            "maquinas" in datos or "tablatiempos" in datos
        ):
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: respuesta sin llegadas ni máquinas GPS",
            }

        buses = buses_de_respuesta_gps_avanza(datos, poste, nombre_parada)
        nombre_feed = next(
            (bus.get("parada") for bus in buses if bus.get("parada")),
            nombre_parada,
        )
        return {
            "poste": poste,
            "nombre": nombre_feed or nombre_parada,
            "buses": buses,
            "error": None,
        }

    return {
        "poste": poste,
        "nombre": nombre_parada,
        "buses": [],
        "error": f"poste {poste}: no se pudo completar la consulta GPS",
    }



def _fusionar_buses_avanza(buses_por_coche, buses_nuevos):
    """Conserva para cada coche la posición procedente de la consulta más reciente."""
    for bus in buses_nuevos or []:
        if not isinstance(bus, dict):
            continue
        coche = str(bus.get("coche") or bus.get("id") or "").strip()
        if not coche:
            continue
        actual = dict(bus)
        try:
            capturado = float(actual.get("capturado_epoch") or time.time())
        except (TypeError, ValueError):
            capturado = time.time()
        actual["capturado_epoch"] = capturado
        anterior = buses_por_coche.get(coche)
        try:
            captura_anterior = float(anterior.get("capturado_epoch") or 0) if anterior else 0
        except (TypeError, ValueError):
            captura_anterior = 0
        if anterior is None or capturado >= captura_anterior:
            buses_por_coche[coche] = actual


def _registrar_resultado_parada_avanza(
    resultado_parada,
    postes_gps_completados,
    paradas_por_id,
    fallos_por_poste,
    errores_por_poste,
):
    """
    Solo marca como completado un poste cuando recibe JSON GPS válido.
    Las respuestas fallidas permanecen pendientes para las rondas de reintento.
    """
    poste_key = str(resultado_parada.get("poste"))
    error = resultado_parada.get("error")
    if error:
        fallos_por_poste[poste_key] = dict(resultado_parada)
        errores_por_poste[poste_key] = str(error)
        return False

    fallos_por_poste.pop(poste_key, None)
    errores_por_poste.pop(poste_key, None)
    postes_gps_completados.add(poste_key)
    paradas_por_id[poste_key] = {
        "poste": resultado_parada["poste"],
        "nombre": resultado_parada["nombre"],
    }
    return True


def _avanza_resultado_permite_reintento(resultado):
    """Solo reintenta fallos transitorios; un 403/404 o un JSON incompatible no cambia por repetir."""
    if not isinstance(resultado, dict) or not resultado.get("error"):
        return False
    error = str(resultado.get("error") or "").lower()
    coincidencia = re.search(r"\bhttp\s+(\d{3})\b", error)
    if coincidencia:
        return int(coincidencia.group(1)) in {408, 425, 429, 500, 502, 503, 504}
    if "respuesta gps no válida" in error or "respuesta sin máquinas gps" in error:
        return False
    return True


def consultar_flota_avanza_urbana():
    """
    Escanea las paradas con un pool continuo. Cada resultado libera un hilo que
    inicia inmediatamente la siguiente consulta, sin barreras entre lotes.
    """
    inicio = time.monotonic()
    http = requests.Session()
    http.headers.update({
        "User-Agent": AVANZA_HTTP_USER_AGENT,
        "Accept": "*/*",
    })

    postes, lineas_descubiertas, sentidos_consultados, errores_catalogo = (
        obtener_postes_avanza_urbanos(http)
    )
    total_postes = len(postes)
    if total_postes == 0:
        raise ValueError("No se han encontrado postes operativos de Avanza.")

    # El catálogo se comparte en una clave separada y no se repite con cada flota.
    _guardar_avanza_estado_compartido(catalogo={
        "version": AVANZA_CATALOGO_VERSION,
        "actualizado_epoch": time.time(),
        "postes": postes,
        "lineas": lineas_descubiertas,
        "errores": errores_catalogo[:20],
    })

    estado_compartido = _leer_avanza_estado_compartido().get("state") or {}
    escaneo_mismo = bool(
        estado_compartido.get("escaneo_en_curso")
        and estado_compartido.get("scan_id")
    )
    if escaneo_mismo and estado_compartido.get("fase") == "consultando_gps":
        postes_gps_completados = set(
            str(poste) for poste in (estado_compartido.get("postes_gps_completados") or [])
        )
        buses_gps_parciales = [
            dict(bus) for bus in (estado_compartido.get("buses_gps_parciales") or [])
            if isinstance(bus, dict) and bus.get("coche")
        ]
        paradas_consultadas = list(
            estado_compartido.get("paradas_gps_consultadas") or []
        )
    else:
        postes_gps_completados = set()
        buses_gps_parciales = []
        paradas_consultadas = []

    buses_por_coche = {
        str(bus["coche"]): dict(bus) for bus in buses_gps_parciales
    }
    intentos_gps = len(postes_gps_completados)
    ultimo_error_gps = None
    consultas_primera_pasada = 0
    circuito_abierto = False
    reinicio_solicitado = False
    with AVANZA_URBANOS_LOCK:
        buses_previos = [
            dict(bus) for bus in (AVANZA_URBANOS_STATE.get("buses_parciales") or [])
            if isinstance(bus, dict) and bus.get("coche")
        ]

    paradas_por_id = {
        str(item["poste"]): dict(item)
        for item in paradas_consultadas
        if isinstance(item, dict) and item.get("poste") is not None
    }
    postes_por_numero = {str(item["poste"]): item for item in postes}
    total_operaciones = sentidos_consultados + total_postes
    _actualizar_estado_avanza(
        fase="consultando_gps",
        progreso=len(paradas_por_id),
        total_progreso=total_postes,
        progreso_general=sentidos_consultados + len(paradas_por_id),
        total_progreso_general=total_operaciones,
        sentidos_consultados=sentidos_consultados,
        total_sentidos=sentidos_consultados,
        paradas_gps_completadas=len(paradas_por_id),
        paradas_gps_intentadas=intentos_gps,
        paradas_gps_con_error=0,
        ultimo_error_gps=None,
        coches_unicos_detectados=len(buses_previos),
        buses_parciales=buses_previos,
        buses_gps_parciales=list(buses_por_coche.values()),
        postes_gps_completados=sorted(postes_gps_completados),
        paradas_gps_consultadas=list(paradas_por_id.values()),
        errores_gps_parciales=[],
        lineas_descubiertas=lineas_descubiertas,
        paradas_descubiertas=total_postes,
    )

    # Las paradas fallidas quedan pendientes y se vuelven a intentar al terminar
    # la primera pasada completa de la red.
    errores_por_poste = {}
    fallos_por_poste = {}
    errores = list(errores_catalogo)
    pendientes_iter = iter([
        item for item in postes
        if str(item["poste"]) not in postes_gps_completados
    ])
    ultimo_checkpoint_gps = time.monotonic()
    resultados_desde_checkpoint = 0

    def incorporar_respuesta_gps(resultado_parada, refresco=False):
        nonlocal intentos_gps, ultimo_error_gps
        poste_key = str(resultado_parada.get("poste"))
        if not refresco:
            intentos_gps += 1
            if resultado_parada.get("error"):
                ultimo_error_gps = str(resultado_parada.get("error"))[:220]
            respuesta_valida = _registrar_resultado_parada_avanza(
                resultado_parada,
                postes_gps_completados,
                paradas_por_id,
                fallos_por_poste,
                errores_por_poste,
            )
            if not respuesta_valida:
                return
        elif resultado_parada.get("error"):
            return

        coches_en_respuesta = {
            str(bus.get("coche") or bus.get("id") or "")
            for bus in resultado_parada.get("buses", [])
        }
        # Si una parada deja de anunciar un coche, elimina solo la posición
        # que provenía de esa parada; otra parada puede seguir localizándolo.
        for coche, anterior in list(buses_por_coche.items()):
            if (
                str(anterior.get("poste")) == poste_key
                and coche not in coches_en_respuesta
            ):
                buses_por_coche.pop(coche, None)
        _fusionar_buses_avanza(
            buses_por_coche, resultado_parada.get("buses", [])
        )

    def publicar_progreso_gps(forzar=False):
        nonlocal ultimo_checkpoint_gps, resultados_desde_checkpoint
        ahora = time.monotonic()
        if (
            not forzar
            and resultados_desde_checkpoint < 20
            and ahora - ultimo_checkpoint_gps < 2.0
        ):
            return

        buses_gps_ordenados = sorted(
            (dict(bus) for bus in buses_por_coche.values()),
            key=lambda bus: (
                str(bus.get("linea") or ""),
                int(bus["coche"]) if str(bus.get("coche", "")).isdigit() else 0,
            ),
        )
        buses_mostrados = {
            str(bus["coche"]): dict(bus)
            for bus in buses_previos if bus.get("coche")
        }
        buses_mostrados.update({
            str(bus["coche"]): dict(bus)
            for bus in buses_gps_ordenados if bus.get("coche")
        })
        buses_mostrados_ordenados = sorted(
            buses_mostrados.values(),
            key=lambda bus: (
                str(bus.get("linea") or ""),
                int(bus["coche"]) if str(bus.get("coche", "")).isdigit() else 0,
            ),
        )
        paradas_ordenadas = sorted(
            paradas_por_id.values(),
            key=lambda item: int(item["poste"]) if str(item["poste"]).isdigit() else str(item["poste"]),
        )
        progreso_valido = len(paradas_ordenadas)
        progreso_intentos = min(intentos_gps, total_postes)
        _actualizar_estado_avanza(
            fase="consultando_gps",
            progreso=progreso_intentos,
            total_progreso=total_postes,
            progreso_general=sentidos_consultados + progreso_intentos,
            total_progreso_general=total_operaciones,
            sentidos_consultados=sentidos_consultados,
            total_sentidos=sentidos_consultados,
            paradas_gps_completadas=progreso_valido,
            paradas_gps_intentadas=intentos_gps,
            paradas_gps_con_error=len(fallos_por_poste),
            ultimo_error_gps=ultimo_error_gps,
            heartbeat_epoch=time.time(),
            coches_unicos_detectados=len(buses_mostrados_ordenados),
            buses_parciales=buses_mostrados_ordenados,
            buses_gps_parciales=buses_gps_ordenados,
            postes_gps_completados=sorted(postes_gps_completados),
            paradas_gps_consultadas=paradas_ordenadas,
            errores_gps_parciales=list(errores_por_poste.values())[-100:],
            lineas_descubiertas=lineas_descubiertas,
            paradas_descubiertas=total_postes,
        )
        ultimo_checkpoint_gps = ahora
        resultados_desde_checkpoint = 0

    def siguiente_tarea():
        try:
            return next(pendientes_iter)
        except StopIteration:
            return None

    futuros = {}
    cancelar_peticiones_gps = threading.Event()
    with ThreadPoolExecutor(
        max_workers=AVANZA_CONCURRENCIA_GPS,
        thread_name_prefix="avanza-gps",
    ) as executor:
        def rellenar_hilos():
            while len(futuros) < AVANZA_CONCURRENCIA_GPS:
                parada_info = siguiente_tarea()
                if parada_info is None:
                    return
                futuro = executor.submit(
                    _consultar_poste_gps_avanza, parada_info, cancelar_peticiones_gps
                )
                futuros[futuro] = parada_info

        rellenar_hilos()
        while futuros:
            terminados, _ = wait(tuple(futuros), return_when=FIRST_COMPLETED)
            if _avanza_reinicio_pendiente():
                reinicio_solicitado = True
                cancelar_peticiones_gps.set()
                for futura_pendiente in futuros:
                    futura_pendiente.cancel()
                break
            for futuro in terminados:
                parada_info = futuros.pop(futuro)
                poste_key = str(parada_info["poste"])
                try:
                    resultado_parada = futuro.result()
                except Exception as exc:
                    resultado_parada = {
                        "poste": parada_info["poste"],
                        "nombre": parada_info.get("nombre") or f"Parada {parada_info['poste']}",
                        "buses": [],
                        "error": f"poste {parada_info['poste']}: {str(exc)[:160]}",
                    }

                incorporar_respuesta_gps(resultado_parada, refresco=False)
                consultas_primera_pasada += 1

                resultados_desde_checkpoint += 1
                publicar_progreso_gps()

                # Si las primeras 24 consultas no consiguen ni una respuesta JSON
                # válida, el problema es de acceso/formato de la fuente, no de cobertura.
                # No seguimos enviando cientos de peticiones y reintentos idénticos.
                if consultas_primera_pasada >= 24 and not paradas_por_id:
                    circuito_abierto = True
                    cancelar_peticiones_gps.set()
                    for futura_pendiente in futuros:
                        futura_pendiente.cancel()
                    publicar_progreso_gps(forzar=True)
                    break
            if circuito_abierto or reinicio_solicitado:
                break
            rellenar_hilos()

    if reinicio_solicitado:
        raise AvanzaBarridoInterrumpido(
            "El barrido anterior ha cedido el turno para iniciar uno nuevo."
        )

    if circuito_abierto:
        muestra_error = ultimo_error_gps or "sin detalle de error"
        raise ValueError(
            "Avanza GPS no ha dado ninguna respuesta válida en las primeras "
            f"{consultas_primera_pasada} consultas. Se detiene el barrido para evitar "
            f"una espera indefinida. Último error: {muestra_error}"
        )

    # Reintenta las paradas que fallaron, después de completar la primera pasada.
    # Así una parada lenta o una respuesta transitoria no se pierde definitivamente.
    for ronda_reintento in range(1, AVANZA_GPS_REINTENTOS_FINALES + 1):
        if _avanza_reinicio_pendiente():
            raise AvanzaBarridoInterrumpido(
                "El barrido anterior ha cedido el turno antes de reintentar paradas."
            )
        if not fallos_por_poste:
            break
        time.sleep(min(6.0, 1.5 * ronda_reintento))
        paradas_reintento = [
            postes_por_numero[poste]
            for poste in sorted(
                fallos_por_poste,
                key=lambda valor: int(valor) if str(valor).isdigit() else str(valor),
            )
            if (
                poste in postes_por_numero
                and _avanza_resultado_permite_reintento(fallos_por_poste.get(poste))
            )
        ]
        if not paradas_reintento:
            break

        futuros_reintento = {}
        with ThreadPoolExecutor(
            max_workers=AVANZA_CONCURRENCIA_GPS,
            thread_name_prefix=f"avanza-gps-reintento-{ronda_reintento}",
        ) as executor:
            for parada_info in paradas_reintento:
                futuro = executor.submit(_consultar_poste_gps_avanza, parada_info)
                futuros_reintento[futuro] = parada_info

            while futuros_reintento:
                terminados, _ = wait(
                    tuple(futuros_reintento),
                    return_when=FIRST_COMPLETED,
                )
                if _avanza_reinicio_pendiente():
                    for futura_pendiente in futuros_reintento:
                        futura_pendiente.cancel()
                    raise AvanzaBarridoInterrumpido(
                        "El barrido anterior ha cedido el turno durante los reintentos."
                    )
                for futuro in terminados:
                    parada_info = futuros_reintento.pop(futuro)
                    try:
                        resultado_parada = futuro.result()
                    except Exception as exc:
                        resultado_parada = {
                            "poste": parada_info["poste"],
                            "nombre": parada_info.get("nombre") or f"Parada {parada_info['poste']}",
                            "buses": [],
                            "error": f"poste {parada_info['poste']}: {str(exc)[:160]}",
                        }
                    incorporar_respuesta_gps(resultado_parada, refresco=False)
                    resultados_desde_checkpoint += 1
                    publicar_progreso_gps()

        publicar_progreso_gps(forzar=True)

    errores_gps_finales = [
        errores_por_poste[poste]
        for poste in sorted(
            errores_por_poste,
            key=lambda valor: int(valor) if str(valor).isdigit() else str(valor),
        )
    ]
    errores = list(errores_catalogo) + errores_gps_finales

    if not paradas_por_id and errores:
        raise ValueError(
            "No se ha podido consultar ninguna parada de Avanza. " +
            " | ".join(errores[:5])
        )

    duracion = round(time.monotonic() - inicio, 1)
    if lineas_descubiertas == 0:
        aviso_cobertura = (
            "Cobertura limitada: no se ha podido descubrir el catálogo completo " +
            "de recorridos y se ha recurrido a los postes configurados o al poste 14. " +
            "Este resultado no representa toda la flota."
        )
    elif errores_catalogo or errores:
        paradas_sin_respuesta = max(0, total_postes - len(paradas_por_id))
        aviso_cobertura = (
            f"Cobertura parcial: {len(paradas_por_id)} de {total_postes} paradas respondieron; "
            f"{paradas_sin_respuesta} no devolvieron respuesta válida. "
            "Puede faltar algún vehículo hasta el siguiente barrido."
        )
    else:
        aviso_cobertura = (
            "Se han consultado los postes encontrados en todos los recorridos "
            "publicados por Avanza y se han agrupado los coches repetidos. "
            "La fuente puede omitir vehículos que no anuncie en ninguno de esos postes."
        )

    resultado = {
        "buses": sorted(
            buses_por_coche.values(),
            key=lambda bus: (
                str(bus["linea"]),
                int(bus["coche"]) if str(bus["coche"]).isdigit() else 0,
            ),
        ),
        "paradas_consultadas": sorted(
            paradas_por_id.values(),
            key=lambda item: int(item["poste"]) if str(item["poste"]).isdigit() else str(item["poste"]),
        ),
        "total_paradas": total_postes,
        "paradas_descubiertas": total_postes,
        "paradas_sin_respuesta": max(0, total_postes - len(paradas_por_id)),
        "_postes_red": postes,
        "lineas_descubiertas": lineas_descubiertas,
        "sentidos_consultados": sentidos_consultados,
        "actualizado": datetime.now(ZONA_HORARIA).isoformat(),
        "actualizado_epoch": time.time(),
        "duracion_segundos": duracion,
        "concurrencia_gps": AVANZA_CONCURRENCIA_GPS,
        "peticiones_gps_por_segundo": AVANZA_GPS_PETICIONES_POR_SEGUNDO,
        "reintentos_finales_gps": AVANZA_GPS_REINTENTOS_FINALES,
        "scan_version": AVANZA_SCAN_VERSION,
        "fuente": "GPS de Avanza Zaragoza · barrido de red",
        "aviso_cobertura": aviso_cobertura,
        "errores": errores[:20],
    }
    return resultado


def _seguir_actualizando_flota_avanza(resultado, timestamp_cache):
    """
    Refresca paradas activas y explora algunas nuevas en rotación. Reutiliza
    el mismo pool durante todo el ciclo para reducir arranques de hilos.
    """
    postes_red = list(resultado.get("_postes_red") or [])
    if not postes_red:
        return

    buses_por_coche = {
        str(bus["coche"]): dict(bus)
        for bus in resultado.get("buses", [])
        if isinstance(bus, dict) and bus.get("coche")
    }
    postes_por_numero = {str(item["poste"]): item for item in postes_red}
    cursor_activos = 0
    cursor_novedades = 0

    with ThreadPoolExecutor(
        max_workers=AVANZA_CONCURRENCIA_GPS,
        thread_name_prefix="avanza-gps-live",
    ) as executor:
        while time.time() - timestamp_cache < AVANZA_URBANOS_TTL:
            restante = AVANZA_URBANOS_TTL - (time.time() - timestamp_cache)
            if restante <= 0:
                break
            time.sleep(min(AVANZA_GPS_SEGUIMIENTO_INTERVALO, restante))
            estado_compartido = _leer_avanza_estado_compartido(incluir_catalogo=False).get("state") or {}
            if estado_compartido.get("nuevo_barrido_solicitado"):
                break
            if time.time() - timestamp_cache >= AVANZA_URBANOS_TTL:
                break

            postes_activos = sorted({
                str(bus.get("poste"))
                for bus in buses_por_coche.values()
                if bus.get("poste") is not None and str(bus.get("poste")).isdigit()
            }, key=int)
            cantidad_activos = min(AVANZA_GPS_SEGUIMIENTO_LOTE, len(postes_activos))
            lote_activo = [
                postes_por_numero[postes_activos[
                    (cursor_activos + indice) % len(postes_activos)
                ]]
                for indice in range(cantidad_activos)
            ] if cantidad_activos else []
            if postes_activos:
                cursor_activos = (cursor_activos + cantidad_activos) % len(postes_activos)

            ids_activos = {str(item["poste"]) for item in lote_activo}
            novedades = []
            intentos_novedades = 0
            limite_novedades = (
                AVANZA_CONCURRENCIA_GPS
                if not postes_activos
                else AVANZA_GPS_SEGUIMIENTO_NOVEDADES
            )
            while (
                len(novedades) < limite_novedades
                and intentos_novedades < len(postes_red)
            ):
                item = postes_red[cursor_novedades % len(postes_red)]
                cursor_novedades = (cursor_novedades + 1) % len(postes_red)
                intentos_novedades += 1
                if str(item["poste"]) not in ids_activos:
                    novedades.append(item)
                    ids_activos.add(str(item["poste"]))

            lote = lote_activo + novedades
            if not lote:
                break

            consultas_correctas = 0
            futuros = {
                executor.submit(_consultar_poste_gps_avanza, parada_info): parada_info
                for parada_info in lote
            }
            for futuro in as_completed(futuros):
                try:
                    consulta = futuro.result()
                except Exception:
                    continue
                if consulta.get("error"):
                    continue

                consultas_correctas += 1
                poste_consultado = str(consulta.get("poste"))
                coches_en_respuesta = {
                    str(bus.get("coche") or bus.get("id") or "")
                    for bus in consulta.get("buses", [])
                }
                for coche, anterior in list(buses_por_coche.items()):
                    if (
                        str(anterior.get("poste")) == poste_consultado
                        and coche not in coches_en_respuesta
                    ):
                        buses_por_coche.pop(coche, None)
                _fusionar_buses_avanza(
                    buses_por_coche, consulta.get("buses", [])
                )

            ahora_epoch = time.time()
            for coche, bus in list(buses_por_coche.items()):
                try:
                    edad_bus = ahora_epoch - float(bus.get("capturado_epoch") or 0)
                except (TypeError, ValueError):
                    edad_bus = AVANZA_GPS_BUS_ANTIGUEDAD_MAX + 1
                if edad_bus > AVANZA_GPS_BUS_ANTIGUEDAD_MAX:
                    buses_por_coche.pop(coche, None)

            if not consultas_correctas:
                with AVANZA_URBANOS_LOCK:
                    AVANZA_URBANOS_STATE.update({
                        "seguimiento_en_curso": True,
                        "heartbeat_epoch": ahora_epoch,
                    })
                    estado_latido = dict(AVANZA_URBANOS_STATE)
                _guardar_avanza_estado_compartido(estado=estado_latido)
                continue

            buses_actualizados = sorted(
                buses_por_coche.values(),
                key=lambda bus: (
                    str(bus.get("linea") or ""),
                    int(bus["coche"]) if str(bus.get("coche", "")).isdigit() else 0,
                ),
            )
            actualizado = datetime.fromtimestamp(
                ahora_epoch, ZONA_HORARIA
            ).isoformat()

            resultado_cache = dict(resultado)
            resultado_cache["buses"] = buses_actualizados
            resultado_cache["actualizado"] = actualizado
            resultado_cache["actualizado_epoch"] = ahora_epoch

            with AVANZA_URBANOS_LOCK:
                AVANZA_URBANOS_CACHE.update({
                    "timestamp": timestamp_cache,
                    "data": resultado_cache,
                })
                AVANZA_URBANOS_STATE.update({
                    "coches_unicos_detectados": len(buses_actualizados),
                    "seguimiento_en_curso": True,
                    "actualizado": actualizado,
                    "actualizado_epoch": ahora_epoch,
                    "heartbeat_epoch": ahora_epoch,
                })
                estado_actualizado = dict(AVANZA_URBANOS_STATE)

            _guardar_avanza_estado_compartido(
                estado=estado_actualizado,
                cache={"timestamp": timestamp_cache, "data": resultado_cache},
                actualizar_cache=True,
            )


def _trabajador_seguimiento_avanza(resultado, timestamp_cache, descriptor_bloqueo):
    """Reanuda el refresco GPS si el proceso que lo llevaba se ha reiniciado."""
    try:
        ahora = time.time()
        with AVANZA_URBANOS_LOCK:
            AVANZA_URBANOS_STATE.update({
                "escaneo_en_curso": False,
                "fase": "completado",
                "seguimiento_en_curso": True,
                "heartbeat_epoch": ahora,
                "actualizado": resultado.get("actualizado"),
                "actualizado_epoch": resultado.get("actualizado_epoch"),
            })
            estado_inicial = dict(AVANZA_URBANOS_STATE)
        _guardar_avanza_estado_compartido(estado=estado_inicial)
        _seguir_actualizando_flota_avanza(resultado, timestamp_cache)
    except Exception as exc:
        print(f"[AVANZA URBANOS] Error al reanudar el seguimiento GPS: {exc}")
    finally:
        with AVANZA_URBANOS_LOCK:
            AVANZA_URBANOS_STATE.update({
                "seguimiento_en_curso": False,
                "heartbeat_epoch": time.time(),
            })
            estado_final = dict(AVANZA_URBANOS_STATE)
        _guardar_avanza_estado_compartido(estado=estado_final)
        _liberar_bloqueo_avanza_compartido(descriptor_bloqueo)
        try:
            AVANZA_URBAN_SCAN_LOCK.release()
        except RuntimeError:
            pass



def _trabajador_barrido_avanza(descriptor_bloqueo=None):
    try:
        resultado = consultar_flota_avanza_urbana()
        timestamp_cache = time.time()
        with AVANZA_URBANOS_LOCK:
            AVANZA_URBANOS_CACHE.update({
                "timestamp": timestamp_cache,
                "data": resultado,
            })
            AVANZA_URBANOS_STATE.update({
                "escaneo_en_curso": False,
                "fase": "completado",
                "seguimiento_en_curso": True,
                "actualizado": resultado.get("actualizado"),
                "actualizado_epoch": resultado.get("actualizado_epoch"),
                "heartbeat_epoch": time.time(),
                "progreso": len(resultado["paradas_consultadas"]),
                "total_progreso": resultado["total_paradas"],
                "progreso_general": resultado["sentidos_consultados"] + len(resultado["paradas_consultadas"]),
                "total_progreso_general": resultado["sentidos_consultados"] + resultado["total_paradas"],
                "sentidos_consultados": resultado["sentidos_consultados"],
                "total_sentidos": resultado["sentidos_consultados"],
                "paradas_gps_completadas": len(resultado["paradas_consultadas"]),
                "coches_unicos_detectados": len(resultado["buses"]),
                "buses_parciales": [],
                "buses_gps_parciales": [],
                "postes_gps_completados": [],
                "paradas_gps_consultadas": [],
                "errores_gps_parciales": [],
                "recorridos_completados": [],
                "postes_catalogo_parcial": {},
                "errores_recorridos_parcial": [],
                "ultimo_error": None,
                "fin_escaneo": datetime.now(ZONA_HORARIA).isoformat(),
            })
            estado_final = dict(AVANZA_URBANOS_STATE)
        _guardar_avanza_estado_compartido(
            estado=estado_final,
            cache={"timestamp": timestamp_cache, "data": resultado},
            actualizar_cache=True,
        )
        _seguir_actualizando_flota_avanza(resultado, timestamp_cache)
        with AVANZA_URBANOS_LOCK:
            AVANZA_URBANOS_STATE.update({
                "seguimiento_en_curso": False,
                "actualizado": AVANZA_URBANOS_CACHE.get("data", {}).get("actualizado", resultado.get("actualizado")),
                "actualizado_epoch": AVANZA_URBANOS_CACHE.get("data", {}).get("actualizado_epoch", resultado.get("actualizado_epoch")),
                "heartbeat_epoch": time.time(),
            })
            estado_seguimiento_final = dict(AVANZA_URBANOS_STATE)
        _guardar_avanza_estado_compartido(estado=estado_seguimiento_final)
    except AvanzaBarridoInterrumpido as exc:
        print(f"[AVANZA URBANOS] {exc}")
        estado_compartido = _leer_avanza_estado_compartido(incluir_catalogo=False).get("state") or {}
        with AVANZA_URBANOS_LOCK:
            AVANZA_URBANOS_STATE.update({
                "escaneo_en_curso": False,
                "seguimiento_en_curso": False,
                "fase": "iniciando",
                "nuevo_barrido_solicitado": True,
                "nuevo_barrido_solicitado_epoch": (
                    estado_compartido.get("nuevo_barrido_solicitado_epoch") or time.time()
                ),
                "heartbeat_epoch": time.time(),
                "buses_parciales": [],
                "coches_unicos_detectados": 0,
            })
            estado_interrumpido = dict(AVANZA_URBANOS_STATE)
        _guardar_avanza_estado_compartido(estado=estado_interrumpido)
    except Exception as exc:
        print(f"[AVANZA URBANOS] Error en el barrido de flota: {exc}")
        _actualizar_estado_avanza(
            escaneo_en_curso=False,
            fase="error",
            seguimiento_en_curso=False,
            ultimo_error=str(exc)[:300],
            fin_escaneo=datetime.now(ZONA_HORARIA).isoformat(),
        )
    finally:
        _liberar_bloqueo_avanza_compartido(descriptor_bloqueo)
        try:
            AVANZA_URBAN_SCAN_LOCK.release()
        except RuntimeError:
            pass


def _catalogo_avanza_es_compatible(catalogo):
    """Evita reutilizar listas de paradas descubiertas con un parser anterior."""
    return (
        isinstance(catalogo, dict)
        and catalogo.get("version") == AVANZA_CATALOGO_VERSION
    )


def _cache_avanza_es_compatible(cache):
    """Solo reutiliza flotas guardadas por la versión actual del barrido."""
    return (
        isinstance(cache, dict)
        and str(cache.get("scan_version") or "") == str(AVANZA_SCAN_VERSION)
    )


def _buses_parciales_del_mismo_escaneo(estado_previo, reanudar_escaneo):
    """
    Recupera autobuses parciales únicamente al reanudar el mismo scan_id.
    Un barrido nuevo empieza siempre con un recuento vacío.
    """
    if not reanudar_escaneo or not isinstance(estado_previo, dict):
        return []
    buses = estado_previo.get("buses_parciales") or []
    return [
        dict(bus) for bus in buses
        if isinstance(bus, dict) and (bus.get("coche") or bus.get("id"))
    ]


def _solicitar_nuevo_barrido_avanza_si_ocupado(forzar=False):
    """
    En una recarga, solicita un barrido limpio incluso si otro sigue activo.
    El listado anterior se oculta inmediatamente y el siguiente barrido arranca
    en cuanto el proceso que mantiene el bloqueo haya terminado.
    """
    if not forzar:
        return

    contenido = _leer_avanza_estado_compartido(incluir_catalogo=False)
    estado = contenido.get("state") or {}
    if not isinstance(estado, dict):
        return

    solicitud_epoch = time.time()
    estado["nuevo_barrido_solicitado"] = True
    estado["nuevo_barrido_solicitado_epoch"] = solicitud_epoch
    with AVANZA_URBANOS_LOCK:
        AVANZA_URBANOS_STATE.update({
            "nuevo_barrido_solicitado": True,
            "nuevo_barrido_solicitado_epoch": solicitud_epoch,
        })
    _guardar_avanza_estado_compartido(estado=estado)


def iniciar_barrido_avanza_si_necesario(forzar=False):
    """
    Inicia un único barrido global. Redis coordina todas las instancias cuando
    AVANZA_REDIS_URL/REDIS_URL está configurada; sin ella se usa flock local.
    """
    global AVANZA_REDIS_ACTIVE_LOCK_TOKEN
    # Primero evita dos inicios simultáneos dentro del mismo worker.
    if not AVANZA_URBAN_SCAN_LOCK.acquire(blocking=False):
        _solicitar_nuevo_barrido_avanza_si_ocupado(forzar)
        return
    descriptor = _adquirir_bloqueo_avanza_compartido()
    if descriptor is None:
        AVANZA_URBAN_SCAN_LOCK.release()
        _solicitar_nuevo_barrido_avanza_si_ocupado(forzar)
        return
    if isinstance(descriptor, dict) and descriptor.get("tipo") == "redis":
        AVANZA_REDIS_ACTIVE_LOCK_TOKEN = descriptor.get("token")

    try:
        datos_compartidos = _leer_avanza_estado_compartido()
        cache_compartida = datos_compartidos.get("cache") or {}
        cacheada = cache_compartida.get("data")
        estado_previo = datos_compartidos.get("state") or {}
        if isinstance(cacheada, dict) and not cacheada.get("_postes_red"):
            catalogo_red = datos_compartidos.get("catalogo") or {}
            postes_red = catalogo_red.get("postes") or []
            if postes_red:
                cacheada = dict(cacheada)
                cacheada["_postes_red"] = list(postes_red)
        solicitud_nuevo_barrido = bool(estado_previo.get("nuevo_barrido_solicitado"))
        reanudar_escaneo = bool(
            not forzar
            and not solicitud_nuevo_barrido
            and estado_previo.get("escaneo_en_curso")
            and estado_previo.get("scan_id")
            and str(estado_previo.get("scan_version") or "") == str(AVANZA_SCAN_VERSION)
        )
        # Nunca se usa la flota completada anterior como base de un barrido nuevo.
        # Solo se conservan parciales si se reanuda exactamente el mismo scan_id.
        buses_previos = _buses_parciales_del_mismo_escaneo(
            estado_previo, reanudar_escaneo
        )
        timestamp_cache = cache_compartida.get("timestamp") or 0.0
        edad = (
            time.time() - float(timestamp_cache)
            if isinstance(timestamp_cache, (int, float)) and timestamp_cache > 0
            else float("inf")
        )

        if (
            not forzar
            and not solicitud_nuevo_barrido
            and not reanudar_escaneo
            and _cache_avanza_es_compatible(cacheada)
            and edad < AVANZA_URBANOS_TTL
        ):
            heartbeat_previo = estado_previo.get("heartbeat_epoch")
            try:
                edad_heartbeat = time.time() - float(heartbeat_previo)
            except (TypeError, ValueError):
                edad_heartbeat = float("inf")
            heartbeat_vivo = (
                bool(estado_previo.get("seguimiento_en_curso"))
                and edad_heartbeat < max(30.0, AVANZA_GPS_SEGUIMIENTO_INTERVALO * 4)
            )

            with AVANZA_URBANOS_LOCK:
                AVANZA_URBANOS_CACHE.update({
                    "timestamp": float(timestamp_cache),
                    "data": cacheada,
                })
                if isinstance(datos_compartidos.get("state"), dict):
                    AVANZA_URBANOS_STATE.update(datos_compartidos["state"])

            if heartbeat_vivo or not cacheada.get("_postes_red"):
                _liberar_bloqueo_avanza_compartido(descriptor)
                AVANZA_URBAN_SCAN_LOCK.release()
                return

            # Caché todavía válida, pero no hay un proceso vivo refrescando.
            # Reinicia solo el seguimiento; no vuelve a descargar las 927 paradas.
            ahora_heartbeat = time.time()
            with AVANZA_URBANOS_LOCK:
                AVANZA_URBANOS_STATE.update({
                    "escaneo_en_curso": False,
                    "fase": "completado",
                    "seguimiento_en_curso": True,
                    "heartbeat_epoch": ahora_heartbeat,
                    "buses_parciales": [],
                    "coches_unicos_detectados": len(cacheada.get("buses") or []),
                })
                estado_reanudado = dict(AVANZA_URBANOS_STATE)
            _guardar_avanza_estado_compartido(estado=estado_reanudado)

            hilo_seguimiento = threading.Thread(
                target=_trabajador_seguimiento_avanza,
                args=(cacheada, float(timestamp_cache), descriptor),
                name="avanza-urbanos-seguimiento-recuperado",
                daemon=True,
            )
            hilo_seguimiento.start()
            return

        with AVANZA_URBANOS_LOCK:
            AVANZA_URBANOS_STATE.update({
                "escaneo_en_curso": True,
                "seguimiento_en_curso": False,
                "fase": estado_previo.get("fase", "iniciando") if reanudar_escaneo else "iniciando",
                "progreso": int(estado_previo.get("progreso") or 0) if reanudar_escaneo else 0,
                "total_progreso": int(estado_previo.get("total_progreso") or 0) if reanudar_escaneo else 0,
                "progreso_general": int(estado_previo.get("progreso_general") or 0) if reanudar_escaneo else 0,
                "total_progreso_general": int(estado_previo.get("total_progreso_general") or 0) if reanudar_escaneo else 0,
                "sentidos_consultados": int(estado_previo.get("sentidos_consultados") or 0) if reanudar_escaneo else 0,
                "total_sentidos": int(estado_previo.get("total_sentidos") or 0) if reanudar_escaneo else 0,
                "paradas_gps_completadas": int(estado_previo.get("paradas_gps_completadas") or 0) if reanudar_escaneo else 0,
                "paradas_gps_intentadas": int(estado_previo.get("paradas_gps_intentadas") or 0) if reanudar_escaneo else 0,
                "paradas_gps_con_error": int(estado_previo.get("paradas_gps_con_error") or 0) if reanudar_escaneo else 0,
                "ultimo_error_gps": estado_previo.get("ultimo_error_gps") if reanudar_escaneo else None,
                "coches_unicos_detectados": len(buses_previos),
                "buses_parciales": buses_previos,
                "scan_version": AVANZA_SCAN_VERSION,
                "lineas_descubiertas": int(estado_previo.get("lineas_descubiertas") or 0) if reanudar_escaneo else 0,
                "paradas_descubiertas": int(estado_previo.get("paradas_descubiertas") or 0) if reanudar_escaneo else 0,
                "recorridos_completados": list(estado_previo.get("recorridos_completados") or []) if reanudar_escaneo else [],
                "postes_catalogo_parcial": dict(estado_previo.get("postes_catalogo_parcial") or {}) if reanudar_escaneo else {},
                "errores_recorridos_parcial": list(estado_previo.get("errores_recorridos_parcial") or []) if reanudar_escaneo else [],
                "postes_gps_completados": list(estado_previo.get("postes_gps_completados") or []) if reanudar_escaneo else [],
                "buses_gps_parciales": list(estado_previo.get("buses_gps_parciales") or []) if reanudar_escaneo else [],
                "paradas_gps_consultadas": list(estado_previo.get("paradas_gps_consultadas") or []) if reanudar_escaneo else [],
                "errores_gps_parciales": list(estado_previo.get("errores_gps_parciales") or []) if reanudar_escaneo else [],
                "ultimo_error": None,
                "nuevo_barrido_solicitado": False,
                "nuevo_barrido_solicitado_epoch": None,
                # Una entrada nueva no debe heredar ninguna hora del barrido anterior.
                "actualizado": estado_previo.get("actualizado") if reanudar_escaneo else None,
                "actualizado_epoch": estado_previo.get("actualizado_epoch") if reanudar_escaneo else None,
                "fin_escaneo": estado_previo.get("fin_escaneo") if reanudar_escaneo else None,
                "heartbeat_epoch": time.time(),
                "scan_id": estado_previo.get("scan_id") if reanudar_escaneo else str(uuid.uuid4()),
                "inicio_escaneo": estado_previo.get("inicio_escaneo") if reanudar_escaneo else datetime.now(ZONA_HORARIA).isoformat(),
            })
            estado_inicial = dict(AVANZA_URBANOS_STATE)
        if reanudar_escaneo:
            _guardar_avanza_estado_compartido(estado=estado_inicial)
        else:
            # Borra también la flota compartida anterior, no solo el estado visual.
            # Así ninguna instancia puede devolver autobuses de la sesión previa.
            cache_vacia = {
                "buses": [],
                "total_paradas": 0,
                "paradas_descubiertas": 0,
                "lineas_descubiertas": 0,
                "actualizado": None,
                "actualizado_epoch": None,
                "scan_version": 0,
            }
            with AVANZA_URBANOS_LOCK:
                AVANZA_URBANOS_CACHE.update({
                    "timestamp": 0.0,
                    "data": cache_vacia,
                })
            _guardar_avanza_estado_compartido(
                estado=estado_inicial,
                cache={"timestamp": 0.0, "data": cache_vacia},
                actualizar_cache=True,
            )

        hilo = threading.Thread(
            target=_trabajador_barrido_avanza,
            args=(descriptor,),
            name="avanza-urbanos-barrido",
            daemon=True,
        )
        hilo.start()
    except Exception:
        _liberar_bloqueo_avanza_compartido(descriptor)
        try:
            AVANZA_URBAN_SCAN_LOCK.release()
        except RuntimeError:
            pass
        raise


@app.route('/api/avanza/login', methods=['POST'])
def login_avanza():
    """Valida las credenciales sin exponerlas al navegador ni a las respuestas."""
    datos = request.get_json(silent=True) or request.form
    usuario = str(datos.get("usuario") or "")
    password = str(datos.get("password") or "")
    usuario_configurado_correcto = hmac.compare_digest(usuario, AVANZA_LOGIN_USER)
    password_configurada_correcta = hmac.compare_digest(password, AVANZA_LOGIN_PASSWORD)
    usuario_inicial_correcto = hmac.compare_digest(usuario, AVANZA_LOGIN_CREDENCIALES_INICIALES[0])
    password_inicial_correcta = hmac.compare_digest(password, AVANZA_LOGIN_CREDENCIALES_INICIALES[1])
    acceso_configurado = usuario_configurado_correcto and password_configurada_correcta
    acceso_inicial = usuario_inicial_correcto and password_inicial_correcta
    if not (acceso_configurado or acceso_inicial):
        return jsonify({"ok": False, "error": "Usuario o contraseña incorrectos."}), 401

    flask_session.clear()
    flask_session["avanza_autorizado"] = True
    flask_session.permanent = True
    return jsonify({"ok": True})


@app.route('/api/avanza/logout', methods=['POST'])
def logout_avanza():
    """Cierra la sesión de acceso al panel urbano."""
    flask_session.pop("avanza_autorizado", None)
    return jsonify({"ok": True})


@app.route('/api/avanza/urbanos')
def get_avanza_urbanos():
    """Devuelve el último barrido único y su progreso, sin bloquear la petición."""
    if not flask_session.get("avanza_autorizado"):
        return jsonify({"error": "Inicia sesión para consultar los autobuses urbanos."}), 401

    iniciar_barrido_avanza_si_necesario(
        forzar=request.args.get("forzar") == "1"
    )
    datos, estado, timestamp_cache = _sincronizar_avanza_estado_compartido()

    if not datos:
        datos = {
            "buses": [],
            "paradas_consultadas": [],
            "total_paradas": 0,
            "paradas_descubiertas": estado.get("paradas_descubiertas", 0),
            "lineas_descubiertas": estado.get("lineas_descubiertas", 0),
            "actualizado": None,
            "errores": [],
        }
    actualizado_cache = datos.get("actualizado")
    epoch_cache = datos.get("actualizado_epoch")
    datos.update(estado)
    reinicio_pendiente = bool(estado.get("nuevo_barrido_solicitado"))
    if reinicio_pendiente:
        solicitud_epoch = float(estado.get("nuevo_barrido_solicitado_epoch") or time.time())
        datos.update({
            "reinicio_pendiente": True,
            "reinicio_espera_segundos": max(0, int(time.time() - solicitud_epoch)),
            "escaneo_en_curso": True,
            "seguimiento_en_curso": False,
            "fase": "iniciando",
            "inicio_escaneo": datetime.fromtimestamp(
                float(estado.get("nuevo_barrido_solicitado_epoch") or time.time()),
                ZONA_HORARIA,
            ).isoformat(),
            "fin_escaneo": None,
            "buses": [],
            "coches_unicos_detectados": 0,
            "actualizado": None,
            "actualizado_epoch": None,
            "progreso": 0,
            "total_progreso": 0,
            "progreso_general": 0,
            "total_progreso_general": 0,
            "sentidos_consultados": 0,
            "total_sentidos": 0,
            "paradas_gps_completadas": 0,
            "paradas_gps_intentadas": 0,
            "paradas_gps_con_error": 0,
            "lineas_descubiertas": 0,
            "paradas_descubiertas": 0,
            "total_paradas": 0,
            "paradas_consultadas": [],
            "paradas_gps_consultadas": [],
            "paradas_consultadas_count": 0,
            "paradas_sin_respuesta_count": 0,
            "ultimo_error": None,
            "ultimo_error_gps": None,
        })
    else:
        datos["reinicio_pendiente"] = False
    if not reinicio_pendiente and not datos.get("actualizado"):
        datos["actualizado"] = actualizado_cache or datos.get("fin_escaneo") or datos.get("inicio_escaneo")
    if not reinicio_pendiente and not datos.get("actualizado_epoch"):
        datos["actualizado_epoch"] = epoch_cache
    if not reinicio_pendiente and (estado.get("escaneo_en_curso") or estado.get("fase") == "error"):
        # Durante un barrido nuevo o fallido solo se exponen los coches de ese
        # mismo scan_id. No se recupera como fallback la última flota completada.
        datos["buses"] = list(estado.get("buses_parciales") or [])
        datos["coches_unicos_detectados"] = len(datos["buses"])
        if estado.get("escaneo_en_curso"):
            # No mostrar como última actualización la hora heredada de ayer.
            datos["actualizado"] = estado.get("actualizado") or estado.get("inicio_escaneo")
            datos["actualizado_epoch"] = estado.get("actualizado_epoch") or None

    # Una carga antigua nunca se presenta como flota actual, aunque el inicio
    # de un nuevo barrido no haya podido adquirir todavía el bloqueo compartido.
    try:
        edad_cache = time.time() - float(timestamp_cache or 0)
    except (TypeError, ValueError):
        edad_cache = float("inf")
    max_antiguedad_visible = min(90.0, float(AVANZA_URBANOS_TTL))
    if (
        not datos.get("escaneo_en_curso")
        and timestamp_cache
        and edad_cache > max_antiguedad_visible
    ):
        datos["buses"] = []
        datos["coches_unicos_detectados"] = 0
        datos["actualizado"] = None
        datos["actualizado_epoch"] = None
        datos["error"] = "La carga GPS anterior ha caducado. Esperando un barrido nuevo."

    if not reinicio_pendiente and estado.get("ultimo_error") and not datos.get("buses"):
        datos["error"] = estado["ultimo_error"]

    fase_actual = str(datos.get("fase") if reinicio_pendiente else (estado.get("fase") or datos.get("fase") or ""))
    if fase_actual == "consultando_gps":
        paradas_con_respuesta = len(estado.get("paradas_gps_consultadas") or [])
        datos["total_paradas"] = int(
            estado.get("paradas_descubiertas") or datos.get("total_paradas") or 0
        )
    elif fase_actual == "descubriendo_paradas":
        paradas_con_respuesta = 0
    else:
        paradas_con_respuesta = int(
            datos.get("paradas_consultadas_count")
            or len(datos.get("paradas_consultadas") or [])
        )
    datos["paradas_consultadas_count"] = paradas_con_respuesta
    datos["paradas_sin_respuesta_count"] = max(
        0, int(datos.get("total_paradas") or 0) - paradas_con_respuesta
    ) if fase_actual == "completado" else 0
    datos["lineas_detectadas"] = sorted({
        normalizar_linea_avanza(bus.get("linea"))
        for bus in (datos.get("buses") or [])
        if isinstance(bus, dict) and bus.get("linea")
    }, key=lambda linea: (not linea.isdigit(), int(linea) if linea.isdigit() else linea))

    # Son estructuras internas para reanudar el escaneo, no datos que necesite el mapa.
    for clave_interna in (
        "_postes_red",
        "paradas_consultadas",
        "paradas_gps_consultadas",
        "postes_gps_completados",
        "buses_parciales",
        "buses_gps_parciales",
        "recorridos_completados",
        "postes_catalogo_parcial",
        "errores_recorridos_parcial",
        "errores_gps_parciales",
    ):
        datos.pop(clave_interna, None)
    respuesta = jsonify(datos)
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta

if __name__ == '__main__':
    # El modo debug solo se activa explícitamente en desarrollo local.
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1')
