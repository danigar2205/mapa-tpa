import os
# Desactivar extensión C de Protobuf para compatibilidad con Python 3.11+
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

from flask import Flask, render_template, jsonify, request
import requests
import urllib3
import gzip
import time
import ssl
import math
import unicodedata
import base64
import json
from requests.adapters import HTTPAdapter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import threading
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


def obtener_respuesta_cacheada(url, familia, ttl_segundos, headers, timeout):
    clave = (familia, url)
    ahora_monotonic = time.monotonic()

    with CACHE_GTFSRT_LOCK:
        cacheada = CACHE_RESPUESTAS_GTFSRT.get(clave)
        if cacheada and ahora_monotonic - cacheada["timestamp"] < ttl_segundos:
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
            return SimpleNamespace(status_code=0, content=b"")

        if resultado.status_code == 200 and resultado.content:
            CACHE_RESPUESTAS_GTFSRT[clave] = {
                "timestamp": ahora_monotonic,
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
            if math.isfinite(valor) and valor >= 0:
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


def obtener_estimaciones_tripupdates(linea_objetivo):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': '*/*'}
    estimaciones = {}
    fuentes = FUENTES_GTFSRT_POR_CONCESION.get(linea_objetivo, {})
    if not fuentes:
        return estimaciones

    dataset = obtener_dataset_gtfs(linea_objetivo)
    rutas_validas = list(dataset["map"].get("routes", {}).keys())

    for url in fuentes.get("tripupdates", []):
        try:
            res = obtener_respuesta_cacheada(url, "tripupdates", 15, headers, 8)
            if res.status_code != 200 or not res.content:
                continue

            content = res.content
            if content.startswith(b'\x1f\x8b'):
                try: content = gzip.decompress(content)
                except Exception: pass

            from google.transit import gtfs_realtime_pb2
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)

            ahora = time.time()
            for entity in feed.entity:
                if entity.HasField('trip_update'):
                    tu = entity.trip_update
                    r_id = str(tu.trip.route_id) if tu.HasField('trip') else ""
                    trip_id = str(tu.trip.trip_id) if tu.HasField('trip') else ""
                    v_id = str(tu.vehicle.id) if (tu.HasField('vehicle') and tu.vehicle.id) else ""
                    coche_label = str(tu.vehicle.label) if (tu.HasField('vehicle') and tu.vehicle.label) else ""
                    direction_id = tu.trip.direction_id if (tu.HasField('trip') and tu.trip.HasField('direction_id')) else None

                    # Filtro estricto por concesión activa
                    es_linea_bus = r_id in rutas_validas

                    if es_linea_bus:
                        delay_sec = tu.delay if tu.HasField('delay') else 0
                        proxima_parada = "En trayecto"
                        
                        destino, _ = resolver_destino_y_parada(linea_objetivo, r_id, direction_id, None)

                        # Priorizar la primera parada futura con hora prevista.
                        # Las paradas SKIPPED no deben mostrarse como próxima parada.
                        actualizaciones_parada = []
                        for stu in tu.stop_time_update:
                            if stu.HasField('schedule_relationship') and stu.schedule_relationship == gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED:
                                continue
                            tiene_hora = (
                                (stu.HasField('arrival') and stu.arrival.HasField('time')) or
                                (stu.HasField('departure') and stu.departure.HasField('time'))
                            )
                            arr_time = (
                                stu.arrival.time if stu.HasField('arrival') and stu.arrival.HasField('time')
                                else stu.departure.time if stu.HasField('departure') and stu.departure.HasField('time')
                                else 0
                            )
                            if arr_time > ahora:
                                actualizaciones_parada.append((stu, arr_time, True))
                            elif not tiene_hora:
                                actualizaciones_parada.append((stu, 0, False))

                        # Las actualizaciones suelen venir ordenadas por secuencia.
                        futura = next((item for item in actualizaciones_parada if item[2]), None)
                        elegida = futura or next((item for item in actualizaciones_parada if not item[2]), None)
                        if elegida:
                            stu, arr_time, tiene_hora_futura = elegida
                            minutos = max(0, int((arr_time - ahora) / 60)) if tiene_hora_futura else None
                            stop_seq = stu.stop_sequence if stu.HasField('stop_sequence') else None
                            stop_id = stu.stop_id if stu.HasField('stop_id') and stu.stop_id else None

                            _, nombre_parada = resolver_destino_y_parada(
                                linea_objetivo, r_id, direction_id, stop_seq, stop_id, trip_id
                            )
                            proxima_parada = f"{nombre_parada}" + (f" ({minutos} min)" if minutos is not None else "")

                            if stu.HasField('arrival') and stu.arrival.HasField('delay'):
                                delay_sec = stu.arrival.delay
                            elif stu.HasField('departure') and stu.departure.HasField('delay'):
                                delay_sec = stu.departure.delay

                        if abs(delay_sec) < 30:
                            texto_retraso = "En hora"
                        elif delay_sec > 0:
                            delay_min = round(delay_sec / 60.0, 1)
                            texto_retraso = f"+{int(delay_min) if delay_min.is_integer() else delay_min} min retraso"
                        else:
                            delay_min = abs(round(delay_sec / 60.0, 1))
                            texto_retraso = f"-{int(delay_min) if delay_min.is_integer() else delay_min} min adelanto"

                        info = {'retraso': texto_retraso, 'proxima_parada': proxima_parada, 'destino': destino}
                        if v_id: estimaciones[v_id] = info
                        if coche_label: estimaciones[coche_label] = info
                        if trip_id: estimaciones[trip_id] = info

            if estimaciones:
                return estimaciones
        except Exception as e:
            print(f"[TRIPUPDATES ERROR {url}]: {e}")

    return estimaciones

@app.route('/')
def index():
    return render_template('index.html')

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
    llegadas = []
    feed_disponible = False
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MapaTPA/1.0)",
        "Accept": "*/*"
    }

    from google.transit import gtfs_realtime_pb2

    for url in fuentes.get("tripupdates", []):
        try:
            respuesta = obtener_respuesta_cacheada(url, "tripupdates", 15, headers, 7)
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

                    instante = None
                    if actualizacion_parada.HasField("arrival") and actualizacion_parada.arrival.HasField("time"):
                        instante = int(actualizacion_parada.arrival.time)
                    elif actualizacion_parada.HasField("departure") and actualizacion_parada.departure.HasField("time"):
                        instante = int(actualizacion_parada.departure.time)

                    if instante is None or instante < ahora - 120 or instante > ahora + 12 * 3600:
                        continue

                    fecha_hora = datetime.fromtimestamp(instante, ZONA_HORARIA)
                    minutos = max(0, math.ceil((instante - ahora) / 60))
                    llegadas.append({
                        "linea": info_ruta.get("short_name", codigo),
                        "destino": destino,
                        "hora": fecha_hora.strftime("%H:%M"),
                        "minutos": minutos,
                        "timestamp": instante,
                        "fuente": "Tiempo real",
                        "trip_id": trip_id,
                        "vehicle_id": vehicle_id,
                        "entity_id": entity_id
                    })

            if llegadas:
                break
        except Exception as exc:
            print(f"[LLEGADAS GTFS-RT ERROR {url}]: {exc}")

    llegadas.sort(key=lambda item: item["timestamp"])
    unicas = []
    vistos = set()
    for llegada in llegadas:
        identificador = (
            llegada.get("trip_id") or
            llegada.get("vehicle_id") or
            llegada.get("entity_id")
        )
        clave = (identificador, llegada["linea"], llegada["timestamp"])
        if clave in vistos:
            continue
        vistos.add(clave)
        unicas.append(llegada)

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

    inicio = datetime.strptime(servicio["start_date"], "%Y%m%d").date()
    fin = datetime.strptime(servicio["end_date"], "%Y%m%d").date()
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
    """Normaliza nombres para comparar el destino con la parada seleccionada."""
    texto = unicodedata.normalize("NFKD", str(valor or "").casefold())
    return " ".join(
        "".join(caracter for caracter in texto if not unicodedata.combining(caracter)).split()
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
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': '*/*'}

    estimaciones = obtener_estimaciones_tripupdates(linea_objetivo)
    rutas_validas = list(dataset["map"].get("routes", {}).keys())

    for url in fuentes.get("vehiclepositions", []):
        try:
            res = obtener_respuesta_cacheada(url, "vehiclepositions", 5, headers, 8)
            if res.status_code != 200 or not res.content:
                continue

            content = res.content
            if content.startswith(b'\x1f\x8b'):
                try: content = gzip.decompress(content)
                except Exception: pass

            buses = []
            from google.transit import gtfs_realtime_pb2
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)

            for entity in feed.entity:
                if entity.HasField('vehicle'):
                    v = entity.vehicle
                    r_id = str(v.trip.route_id) if v.HasField('trip') else ""
                    trip_id = str(v.trip.trip_id) if v.HasField('trip') else ""
                    coche_num = str(v.vehicle.label) if (v.HasField('vehicle') and v.vehicle.label) else ""
                    matricula = str(v.vehicle.license_plate) if (v.HasField('vehicle') and hasattr(v.vehicle, 'license_plate')) else ""
                    entity_id = str(entity.id or "")
                    v_id = (
                        str(v.vehicle.id)
                        if (v.HasField('vehicle') and v.vehicle.id)
                        else coche_num or entity_id
                    )
                    direction_id = v.trip.direction_id if (v.HasField('trip') and v.trip.HasField('direction_id')) else None

                    # Garantiza que SOLO se carguen los buses de la concesión pulsada
                    es_linea_bus = r_id in rutas_validas

                    if es_linea_bus:
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
                        dest_defecto, _ = resolver_destino_y_parada(linea_objetivo, r_id, direction_id, None)

                        info_est = (
                            estimaciones.get(v_id) or 
                            estimaciones.get(coche_num) or 
                            estimaciones.get(trip_id) or 
                            {'retraso': 'En hora', 'proxima_parada': 'En trayecto', 'destino': dest_defecto}
                        )

                        destino_final = info_est.get('destino')
                        if not destino_final or destino_final in ['Desconocido', 'En trayecto']:
                            destino_final = dest_defecto

                        # Las coordenadas 0.0 son válidas; descartar datos GPS incorrectos.
                        coordenadas = validar_coordenadas(lat, lon)
                        if coordenadas is None:
                            continue
                        lat, lon = coordenadas
                        buses.append({
                                'id': v_id or coche_num or entity_id,
                                'linea': nombres_rutas.get(r_id, linea_objetivo),
                                'coche': coche_num,
                                'matricula': matricula,
                                'lat': float(lat),
                                'lon': float(lon),
                                'ocupacion': occ_texto,
                                'ocupacion_color': occ_color,
                                'velocidad_kmh': velocidad_kmh,
                                'retraso': info_est.get('retraso', 'En hora'),
                                'proxima_parada': info_est.get('proxima_parada', 'En trayecto'),
                                'destino': destino_final
                            })

            if buses:
                return jsonify(buses)

        except Exception as e:
            print(f"[API ERROR {url}]: {e}")

    return jsonify([])

if __name__ == '__main__':
    # El modo debug solo se activa explícitamente en desarrollo local.
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1')
