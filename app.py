import os
import fcntl
import html
try:
    import redis
except ImportError:
    redis = None
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
# Desactivar extensión C de Protobuf para compatibilidad con Python 3.11+
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

from flask import Flask, render_template, jsonify, request
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
AVANZA_LINEAS_URL = "https://zaragoza.avanzagrupo.com/lineas-y-horarios/"
AVANZA_AJAX_URL = "https://zaragoza.avanzagrupo.com/wp-admin/admin-ajax.php"

AVANZA_URBANOS_TTL = 360
AVANZA_CATALOGO_POSTES_TTL = 21600
AVANZA_PAUSA_RECORRIDOS = 0.0
AVANZA_PAUSA_POSTES = 0.0
AVANZA_CONCURRENCIA_GPS = 6
AVANZA_GPS_TIMEOUT_CONNECT = 3.0
AVANZA_GPS_TIMEOUT_READ = 4.0
AVANZA_GPS_PARADAS_POR_LOTE = 20
AVANZA_GPS_REFRESCOS_POR_LOTE = 3
AVANZA_GPS_SEGUIMIENTO_LOTE = 14
AVANZA_GPS_SEGUIMIENTO_NOVEDADES = 4
AVANZA_GPS_SEGUIMIENTO_INTERVALO = 6.0
AVANZA_GPS_BUS_ANTIGUEDAD_MAX = 300.0

try:
    AVANZA_URBANOS_TTL = max(120, int(os.environ.get("AVANZA_URBANOS_TTL", AVANZA_URBANOS_TTL)))
except (TypeError, ValueError):
    pass
try:
    AVANZA_PAUSA_RECORRIDOS = max(0.0, float(os.environ.get("AVANZA_PAUSA_RECORRIDOS", AVANZA_PAUSA_RECORRIDOS)))
    AVANZA_PAUSA_POSTES = max(0.0, float(os.environ.get("AVANZA_PAUSA_POSTES", AVANZA_PAUSA_POSTES)))
    AVANZA_CONCURRENCIA_GPS = min(6, max(1, int(os.environ.get("AVANZA_CONCURRENCIA_GPS", AVANZA_CONCURRENCIA_GPS))))
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
AVANZA_SHARED_WRITE_LOCK = threading.Lock()
AVANZA_REDIS_URL = (
    os.environ.get("AVANZA_REDIS_URL", "").strip()
    or os.environ.get("REDIS_URL", "").strip()
)
AVANZA_REDIS_STATE_KEY = os.environ.get(
    "AVANZA_REDIS_STATE_KEY", "mapa-tpa:avanza:shared-state"
)
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


def _leer_avanza_estado_compartido():
    """Lee el estado global desde Redis o, sin Redis, desde el fichero compartido."""
    if AVANZA_REDIS_CLIENT is not None:
        try:
            raw = AVANZA_REDIS_CLIENT.get(AVANZA_REDIS_STATE_KEY)
            contenido = json.loads(raw) if raw else {}
            return contenido if isinstance(contenido, dict) else {}
        except Exception as exc:
            print(f"[AVANZA URBANOS] No se pudo leer el estado Redis: {exc}")
            return {}
    try:
        with open(AVANZA_SHARED_STATE_PATH, "r", encoding="utf-8") as fichero:
            contenido = json.load(fichero)
        if not isinstance(contenido, dict):
            return {}
        return contenido
    except (OSError, ValueError, TypeError):
        return {}


def _guardar_avanza_estado_compartido(estado=None, cache=None, actualizar_cache=False, catalogo=None):
    """
    Escribe de forma atómica el progreso y la última flota en un fichero común.
    Cada worker web lee el mismo fichero, aunque las peticiones roten de proceso.
    """
    with AVANZA_SHARED_WRITE_LOCK:
        contenido = _leer_avanza_estado_compartido()
        if estado is not None:
            contenido["state"] = estado
        if actualizar_cache:
            contenido["cache"] = cache
        if catalogo is not None:
            contenido["catalogo"] = catalogo
        if AVANZA_REDIS_CLIENT is not None:
            try:
                AVANZA_REDIS_CLIENT.set(
                    AVANZA_REDIS_STATE_KEY,
                    json.dumps(contenido, ensure_ascii=False, separators=(",", ":")),
                    ex=172800,
                )
                _renovar_bloqueo_avanza_redis()
                return
            except Exception as exc:
                print(f"[AVANZA URBANOS] No se pudo guardar el estado Redis: {exc}")
                return
        directorio = os.path.dirname(AVANZA_SHARED_STATE_PATH) or "."
        os.makedirs(directorio, exist_ok=True)
        temporal = (
            AVANZA_SHARED_STATE_PATH +
            f".{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with open(temporal, "w", encoding="utf-8") as fichero:
                json.dump(contenido, fichero, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporal, AVANZA_SHARED_STATE_PATH)
        finally:
            try:
                if os.path.exists(temporal):
                    os.remove(temporal)
            except OSError:
                pass


def _sincronizar_avanza_estado_compartido():
    """Actualiza las copias locales desde el estado publicado por el worker activo."""
    contenido = _leer_avanza_estado_compartido()
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


def descubrir_postes_avanza(http):
    """
    Descubre los postes de los recorridos Avanza y reanuda desde el último
    sentido completado cuando un proceso anterior se haya interrumpido.
    """
    ahora = time.monotonic()
    with AVANZA_URBANOS_LOCK:
        cacheada = AVANZA_CATALOGO_POSTES_CACHE
        if cacheada["postes"] and ahora - cacheada["timestamp"] < AVANZA_CATALOGO_POSTES_TTL:
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
        and edad_catalogo < AVANZA_CATALOGO_POSTES_TTL
    ):
        lineas_cacheadas = int(catalogo_compartido.get("lineas") or 0)
        sentidos_cacheados = int(catalogo_compartido.get("sentidos") or 0)
        errores_cacheados = list(catalogo_compartido.get("errores") or [])
        with AVANZA_URBANOS_LOCK:
            AVANZA_CATALOGO_POSTES_CACHE.update({
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

    with ThreadPoolExecutor(
        max_workers=AVANZA_CONCURRENCIA_GPS,
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
            "timestamp": time.monotonic(),
            "postes": lista_postes,
            "lineas": len(lineas),
            "sentidos": sentidos_terminados,
            "errores": errores[:20],
        })
    _guardar_avanza_estado_compartido(catalogo={
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

    validas = validar_coordenadas(punto.get("LAT"), punto.get("LON"))
    if validas is None:
        return None
    lat, lon = validas

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
        "lat": float(lat),
        "lon": float(lon),
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


def _sesion_gps_avanza():
    """Una sesión HTTP persistente por trabajador, sin compartirla entre hilos."""
    sesion = getattr(AVANZA_GPS_THREAD_LOCAL, "sesion", None)
    if sesion is None:
        sesion = requests.Session()
        sesion.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; MapaTPA/1.0)",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
        AVANZA_GPS_THREAD_LOCAL.sesion = sesion
    return sesion


def _consultar_poste_gps_avanza(parada_info):
    """Consulta y analiza un poste; función independiente y segura para un hilo."""
    poste = parada_info["poste"]
    nombre_parada = parada_info.get("nombre") or f"Parada {poste}"
    try:
        sesion = _sesion_gps_avanza()
        respuesta = sesion.post(
            AVANZA_URBANOS_ENDPOINT,
            data={"poste": str(poste), "coche": "0"},
            headers={
                "Referer": f"https://gps.avanzabus.com/zaragoza/fParadas/{poste}",
                "Origin": "https://gps.avanzabus.com",
            },
            # Límite corto para que las últimas paradas lentas no bloqueen el final del barrido.
            timeout=(AVANZA_GPS_TIMEOUT_CONNECT, AVANZA_GPS_TIMEOUT_READ),
        )
        if respuesta.status_code != 200:
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

        if not isinstance(datos, dict) or "maquinas" not in datos:
            return {
                "poste": poste,
                "nombre": nombre_parada,
                "buses": [],
                "error": f"poste {poste}: respuesta sin máquinas GPS",
            }

        maquinas = datos.get("maquinas") or {}
        entradas = (
            list(maquinas.values()) if isinstance(maquinas, dict)
            else (maquinas if isinstance(maquinas, list) else [])
        )
        nombre_feed = None
        for maquina in entradas:
            if not isinstance(maquina, dict):
                continue
            icono = str(maquina.get("icon") or "").split("?")[0].rsplit("/", 1)[-1].lower()
            if icono == "bus_rojo.png":
                nombre_feed = limpiar_texto_html_avanza(
                    maquina.get("info") or maquina.get("title")
                ) or None
                break
        if nombre_feed:
            nombre_parada = nombre_feed

        buses = []
        for maquina in entradas:
            bus = extraer_bus_avanza(maquina, poste, nombre_parada)
            if bus:
                buses.append(bus)
        return {
            "poste": poste,
            "nombre": nombre_parada,
            "buses": buses,
            "error": None,
        }
    except (requests.RequestException, ValueError, TypeError) as exc:
        return {
            "poste": poste,
            "nombre": nombre_parada,
            "buses": [],
            "error": f"poste {poste}: {str(exc)[:160]}",
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


def consultar_flota_avanza_urbana():
    """
    Barre las paradas descubiertas. Si el proceso se interrumpe, retoma las
    paradas ya consultadas y los coches obtenidos desde el último estado Redis.
    """
    inicio = time.monotonic()
    http = requests.Session()
    http.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MapaTPA/1.0)",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })

    postes, lineas_descubiertas, sentidos_consultados, errores_catalogo = (
        obtener_postes_avanza_urbanos(http)
    )
    total_postes = len(postes)
    if total_postes == 0:
        raise ValueError("No se han encontrado postes operativos de Avanza.")

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
        errores_gps = list(estado_compartido.get("errores_gps_parciales") or [])
    else:
        postes_gps_completados = set()
        buses_gps_parciales = []
        paradas_consultadas = []
        errores_gps = []

    buses_por_coche = {
        str(bus["coche"]): bus for bus in buses_gps_parciales
    }
    with AVANZA_URBANOS_LOCK:
        buses_previos = [
            dict(bus) for bus in (AVANZA_URBANOS_STATE.get("buses_parciales") or [])
            if isinstance(bus, dict) and bus.get("coche")
        ]

    progreso_inicial = min(len(postes_gps_completados), total_postes)
    total_operaciones = sentidos_consultados + total_postes
    _actualizar_estado_avanza(
        fase="consultando_gps",
        progreso=progreso_inicial,
        total_progreso=total_postes,
        progreso_general=sentidos_consultados + progreso_inicial,
        total_progreso_general=total_operaciones,
        sentidos_consultados=sentidos_consultados,
        total_sentidos=sentidos_consultados,
        paradas_gps_completadas=progreso_inicial,
        coches_unicos_detectados=len(buses_previos),
        buses_parciales=buses_previos,
        buses_gps_parciales=list(buses_por_coche.values()),
        postes_gps_completados=sorted(postes_gps_completados),
        paradas_gps_consultadas=paradas_consultadas,
        errores_gps_parciales=errores_gps[-100:],
        lineas_descubiertas=lineas_descubiertas,
        paradas_descubiertas=total_postes,
    )

    errores = list(errores_catalogo) + errores_gps
    postes_pendientes = [
        parada_info for parada_info in postes
        if str(parada_info["poste"]) not in postes_gps_completados
    ]

    ultimo_checkpoint_gps = time.monotonic()
    resultados_desde_checkpoint = 0
    cursor_refresco = 0

    postes_por_numero = {
        str(item["poste"]): item for item in postes
    }

    def publicar_progreso_gps(forzar=False):
        nonlocal ultimo_checkpoint_gps, resultados_desde_checkpoint
        ahora_checkpoint = time.monotonic()
        if (
            not forzar
            and resultados_desde_checkpoint < 5
            and ahora_checkpoint - ultimo_checkpoint_gps < 2.0
        ):
            return

        buses_gps_ordenados = sorted(
            (dict(bus) for bus in buses_por_coche.values()),
            key=lambda bus: (
                str(bus.get("linea") or ""),
                int(bus["coche"]) if str(bus.get("coche", "")).isdigit() else 0,
            ),
        )
        buses_mostrados = {str(bus["coche"]): dict(bus) for bus in buses_previos}
        buses_mostrados.update({
            str(bus["coche"]): dict(bus) for bus in buses_gps_ordenados
        })
        buses_mostrados_ordenados = sorted(
            buses_mostrados.values(),
            key=lambda bus: (
                str(bus.get("linea") or ""),
                int(bus["coche"]) if str(bus.get("coche", "")).isdigit() else 0,
            ),
        )
        paradas_ordenadas = sorted(
            {str(item["poste"]): item for item in paradas_consultadas}.values(),
            key=lambda item: int(item["poste"]) if str(item["poste"]).isdigit() else str(item["poste"]),
        )
        indice = min(len(postes_gps_completados), total_postes)
        _actualizar_estado_avanza(
            fase="consultando_gps",
            progreso=indice,
            total_progreso=total_postes,
            progreso_general=sentidos_consultados + indice,
            total_progreso_general=total_operaciones,
            sentidos_consultados=sentidos_consultados,
            total_sentidos=sentidos_consultados,
            paradas_gps_completadas=indice,
            coches_unicos_detectados=len(buses_mostrados_ordenados),
            buses_parciales=buses_mostrados_ordenados,
            buses_gps_parciales=buses_gps_ordenados,
            postes_gps_completados=sorted(postes_gps_completados),
            paradas_gps_consultadas=paradas_ordenadas,
            errores_gps_parciales=errores_gps[-100:],
            lineas_descubiertas=lineas_descubiertas,
            paradas_descubiertas=total_postes,
        )
        ultimo_checkpoint_gps = ahora_checkpoint
        resultados_desde_checkpoint = 0

    # No se encolan todas las paradas de golpe: al cerrar cada lote se
    # reconsultan algunos postes que ya han localizado autobuses.
    for inicio_lote in range(0, len(postes_pendientes), AVANZA_GPS_PARADAS_POR_LOTE):
        lote = postes_pendientes[
            inicio_lote:inicio_lote + AVANZA_GPS_PARADAS_POR_LOTE
        ]
        with ThreadPoolExecutor(
            max_workers=AVANZA_CONCURRENCIA_GPS,
            thread_name_prefix="avanza-gps",
        ) as executor:
            futuros = {
                executor.submit(_consultar_poste_gps_avanza, parada_info): parada_info
                for parada_info in lote
            }

            for futuro in as_completed(futuros):
                try:
                    resultado_parada = futuro.result()
                except Exception as exc:
                    parada_info = futuros[futuro]
                    resultado_parada = {
                        "poste": parada_info["poste"],
                        "nombre": parada_info.get("nombre") or f"Parada {parada_info['poste']}",
                        "buses": [],
                        "error": f"poste {parada_info['poste']}: {str(exc)[:160]}",
                    }

                poste_key = str(resultado_parada["poste"])
                postes_gps_completados.add(poste_key)
                if resultado_parada.get("error"):
                    errores.append(resultado_parada["error"])
                    errores_gps.append(resultado_parada["error"])
                else:
                    paradas_consultadas.append({
                        "poste": resultado_parada["poste"],
                        "nombre": resultado_parada["nombre"],
                    })

                _fusionar_buses_avanza(
                    buses_por_coche, resultado_parada.get("buses", [])
                )
                resultados_desde_checkpoint += 1
                publicar_progreso_gps()

        # Primera publicación del lote: los coches ya aparecen en el mapa.
        publicar_progreso_gps(forzar=True)

        # Reconsulta unos pocos postes que ya han devuelto coches. Esto actualiza
        # coordenadas de vehículos conocidos sin esperar al final de toda la red.
        buses_conocidos = {
            str(bus["coche"]): dict(bus) for bus in buses_previos
            if bus.get("coche")
        }
        buses_conocidos.update({
            str(bus["coche"]): dict(bus) for bus in buses_por_coche.values()
            if bus.get("coche")
        })
        fuentes = sorted({
            str(bus.get("poste"))
            for bus in buses_conocidos.values()
            if bus.get("poste") is not None and str(bus.get("poste")).isdigit()
        }, key=int)

        if fuentes:
            cantidad_refrescos = min(AVANZA_GPS_REFRESCOS_POR_LOTE, len(fuentes))
            seleccion = [
                fuentes[(cursor_refresco + indice) % len(fuentes)]
                for indice in range(cantidad_refrescos)
            ]
            cursor_refresco = (cursor_refresco + cantidad_refrescos) % len(fuentes)
            paradas_refresco = [
                postes_por_numero.get(poste, {
                    "poste": int(poste), "nombre": f"Parada {poste}"
                })
                for poste in seleccion
            ]
            with ThreadPoolExecutor(
                max_workers=min(AVANZA_GPS_REFRESCOS_POR_LOTE, len(paradas_refresco)),
                thread_name_prefix="avanza-gps-refresh",
            ) as executor:
                futuros_refresco = [
                    executor.submit(_consultar_poste_gps_avanza, parada_info)
                    for parada_info in paradas_refresco
                ]
                for futuro in as_completed(futuros_refresco):
                    try:
                        resultado_refresco = futuro.result()
                    except Exception:
                        continue
                    if not resultado_refresco.get("error"):
                        _fusionar_buses_avanza(
                            buses_por_coche, resultado_refresco.get("buses", [])
                        )

            # Publica las coordenadas refrescadas sin incrementar el porcentaje:
            # los refrescos no son nuevas paradas del barrido.
            publicar_progreso_gps(forzar=True)

    if not paradas_consultadas and errores:
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
    elif errores_catalogo:
        aviso_cobertura = (
            "Cobertura parcial: algunos recorridos no se han podido leer. " +
            "Se han deduplicado los coches recibidos, pero pueden faltar vehículos."
        )
    else:
        aviso_cobertura = (
            "Se han consultado los postes encontrados en todos los recorridos " +
            "publicados por Avanza y se han agrupado los coches repetidos. " +
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
            {str(item["poste"]): item for item in paradas_consultadas}.values(),
            key=lambda item: int(item["poste"]) if str(item["poste"]).isdigit() else str(item["poste"]),
        ),
        "total_paradas": total_postes,
        "paradas_descubiertas": total_postes,
        "_postes_red": postes,
        "lineas_descubiertas": lineas_descubiertas,
        "sentidos_consultados": sentidos_consultados,
        "actualizado": datetime.now(ZONA_HORARIA).isoformat(),
        "actualizado_epoch": time.time(),
        "duracion_segundos": duracion,
        "concurrencia_gps": AVANZA_CONCURRENCIA_GPS,
        "fuente": "GPS de Avanza Zaragoza · barrido de red",
        "aviso_cobertura": aviso_cobertura,
        "errores": errores[:20],
    }
    return resultado


 
def _seguir_actualizando_flota_avanza(resultado, timestamp_cache):
    """
    Refresca prioritariamente los postes que están proporcionando autobuses y
    reserva algunas consultas para seguir descubriendo vehículos en el resto
    de la red. El escaneo completo se repetirá al vencer la caché.
    """
    postes_red = list(resultado.get("_postes_red") or [])
    if not postes_red or not resultado.get("buses"):
        return

    buses_por_coche = {
        str(bus["coche"]): dict(bus)
        for bus in resultado.get("buses", [])
        if isinstance(bus, dict) and bus.get("coche")
    }
    postes_por_numero = {str(item["poste"]): item for item in postes_red}
    cursor_activos = 0
    cursor_novedades = 0

    while time.time() - timestamp_cache < AVANZA_URBANOS_TTL:
        restante = AVANZA_URBANOS_TTL - (time.time() - timestamp_cache)
        if restante <= 0:
            break
        time.sleep(min(AVANZA_GPS_SEGUIMIENTO_INTERVALO, restante))
        if time.time() - timestamp_cache >= AVANZA_URBANOS_TTL:
            break

        # Prioridad: volver a preguntar en paradas que han localizado coches.
        postes_activos = sorted({
            str(bus.get("poste"))
            for bus in buses_por_coche.values()
            if bus.get("poste") is not None and str(bus.get("poste")).isdigit()
        }, key=int)
        cantidad_activos = min(AVANZA_GPS_SEGUIMIENTO_LOTE, len(postes_activos))
        lote_activo = [
            postes_por_numero[postes_activos[(cursor_activos + indice) % len(postes_activos)]]
            for indice in range(cantidad_activos)
        ] if cantidad_activos else []
        if postes_activos:
            cursor_activos = (cursor_activos + cantidad_activos) % len(postes_activos)

        # Un pequeño lote de exploración evita limitarse para siempre a las
        # paradas que ya habían anunciado vehículos en el primer barrido.
        ids_activos = {str(item["poste"]) for item in lote_activo}
        novedades = []
        intentos_novedades = 0
        while (
            len(novedades) < AVANZA_GPS_SEGUIMIENTO_NOVEDADES
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
        with ThreadPoolExecutor(
            max_workers=AVANZA_CONCURRENCIA_GPS,
            thread_name_prefix="avanza-gps-live",
        ) as executor:
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
                # Si un poste ya no anuncia un coche, no conservar eternamente
                # su antigua posición procedente de esa misma parada.
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
        # Evita conservar indefinidamente vehículos cuyas posiciones dejaron
        # de llegar desde la fuente GPS.
        for coche, bus in list(buses_por_coche.items()):
            try:
                edad_bus = ahora_epoch - float(bus.get("capturado_epoch") or 0)
            except (TypeError, ValueError):
                edad_bus = AVANZA_GPS_BUS_ANTIGUEDAD_MAX + 1
            if edad_bus > AVANZA_GPS_BUS_ANTIGUEDAD_MAX:
                buses_por_coche.pop(coche, None)

        buses_actualizados = sorted(
            buses_por_coche.values(),
            key=lambda bus: (
                str(bus.get("linea") or ""),
                int(bus["coche"]) if str(bus.get("coche", "")).isdigit() else 0,
            ),
        )
        heartbeat = ahora_epoch
        actualizado = datetime.fromtimestamp(
            ahora_epoch, ZONA_HORARIA
        ).isoformat()

        estado_actualizado = None
        if consultas_correctas:
            resultado_cache = dict(resultado)
            resultado_cache["buses"] = buses_actualizados
            resultado_cache["actualizado"] = actualizado
            resultado_cache["actualizado_epoch"] = ahora_epoch

            with AVANZA_URBANOS_LOCK:
                AVANZA_URBANOS_CACHE.update({
                    # El TTL del barrido completo no se renueva con cada refresco.
                    "timestamp": timestamp_cache,
                    "data": resultado_cache,
                })
                AVANZA_URBANOS_STATE.update({
                    "buses_parciales": buses_actualizados,
                    "coches_unicos_detectados": len(buses_actualizados),
                    "seguimiento_en_curso": True,
                    "actualizado": actualizado,
                    "actualizado_epoch": ahora_epoch,
                    "heartbeat_epoch": heartbeat,
                })
                estado_actualizado = dict(AVANZA_URBANOS_STATE)
            _guardar_avanza_estado_compartido(
                estado=estado_actualizado,
                cache={"timestamp": timestamp_cache, "data": resultado_cache},
                actualizar_cache=True,
            )
        else:
            # Aunque Avanza falle temporalmente, publica un latido para que
            # otra instancia no inicie un segundo seguimiento en paralelo.
            with AVANZA_URBANOS_LOCK:
                AVANZA_URBANOS_STATE.update({
                    "seguimiento_en_curso": True,
                    "heartbeat_epoch": heartbeat,
                })
                estado_actualizado = dict(AVANZA_URBANOS_STATE)
            _guardar_avanza_estado_compartido(estado=estado_actualizado)


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
                "progreso": resultado["total_paradas"],
                "total_progreso": resultado["total_paradas"],
                "progreso_general": resultado["sentidos_consultados"] + resultado["total_paradas"],
                "total_progreso_general": resultado["sentidos_consultados"] + resultado["total_paradas"],
                "sentidos_consultados": resultado["sentidos_consultados"],
                "total_sentidos": resultado["sentidos_consultados"],
                "paradas_gps_completadas": resultado["total_paradas"],
                "coches_unicos_detectados": len(resultado["buses"]),
                "buses_parciales": resultado["buses"],
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


def iniciar_barrido_avanza_si_necesario(forzar=False):
    """
    Inicia un único barrido global. Redis coordina todas las instancias cuando
    AVANZA_REDIS_URL/REDIS_URL está configurada; sin ella se usa flock local.
    """
    global AVANZA_REDIS_ACTIVE_LOCK_TOKEN
    # Primero evita dos inicios simultáneos dentro del mismo worker.
    if not AVANZA_URBAN_SCAN_LOCK.acquire(blocking=False):
        return
    descriptor = _adquirir_bloqueo_avanza_compartido()
    if descriptor is None:
        AVANZA_URBAN_SCAN_LOCK.release()
        return
    if isinstance(descriptor, dict) and descriptor.get("tipo") == "redis":
        AVANZA_REDIS_ACTIVE_LOCK_TOKEN = descriptor.get("token")

    try:
        datos_compartidos = _leer_avanza_estado_compartido()
        cache_compartida = datos_compartidos.get("cache") or {}
        cacheada = cache_compartida.get("data")
        estado_previo = datos_compartidos.get("state") or {}
        reanudar_escaneo = bool(
            not forzar
            and estado_previo.get("escaneo_en_curso")
            and estado_previo.get("scan_id")
        )
        buses_previos = list(
            estado_previo.get("buses_parciales")
            or (cacheada.get("buses", []) if isinstance(cacheada, dict) else [])
            or []
        )
        timestamp_cache = cache_compartida.get("timestamp") or 0.0
        edad = (
            time.time() - float(timestamp_cache)
            if isinstance(timestamp_cache, (int, float)) and timestamp_cache > 0
            else float("inf")
        )

        if (
            not forzar
            and not reanudar_escaneo
            and isinstance(cacheada, dict)
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

            if heartbeat_vivo or not cacheada.get("_postes_red") or not cacheada.get("buses"):
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
                    "buses_parciales": list(cacheada.get("buses") or []),
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
                "coches_unicos_detectados": len(buses_previos),
                "buses_parciales": buses_previos,
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
                "scan_id": estado_previo.get("scan_id") if reanudar_escaneo else str(uuid.uuid4()),
                "inicio_escaneo": estado_previo.get("inicio_escaneo") if reanudar_escaneo else datetime.now(ZONA_HORARIA).isoformat(),
            })
            estado_inicial = dict(AVANZA_URBANOS_STATE)
        _guardar_avanza_estado_compartido(estado=estado_inicial)

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


@app.route('/api/avanza/urbanos')
def get_avanza_urbanos():
    """Devuelve el último barrido único y su progreso, sin bloquear la petición."""
    iniciar_barrido_avanza_si_necesario(
        forzar=request.args.get("forzar") == "1"
    )
    datos, estado, _ = _sincronizar_avanza_estado_compartido()

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
    if not datos.get("actualizado"):
        datos["actualizado"] = actualizado_cache or datos.get("fin_escaneo") or datos.get("inicio_escaneo")
    if not datos.get("actualizado_epoch"):
        datos["actualizado_epoch"] = epoch_cache
    if estado.get("escaneo_en_curso") or estado.get("fase") == "error":
        datos["buses"] = list(estado.get("buses_parciales") or datos.get("buses") or [])
    if estado.get("ultimo_error") and not datos.get("buses"):
        datos["error"] = estado["ultimo_error"]
    datos.pop("_postes_red", None)
    respuesta = jsonify(datos)
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta

if __name__ == '__main__':
    # El modo debug solo se activa explícitamente en desarrollo local.
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1')
