import os
# Desactivar extensión C de Protobuf para compatibilidad con Python 3.11+
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

from flask import Flask, render_template, jsonify
import requests
import urllib3
import gzip
import time
import ssl
import math
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# Desactivar avisos de certificados SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

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
    'NOT_ACCEPTING_PASSENGERS': ('No admite pasajeros', 'red')
}

def resolver_destino_y_parada(concesion_key, route_id, direction_id, stop_sequence):
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

    # GTFS-RT stop_sequence es un identificador de orden, no un índice
    # garantizado para estas listas locales. Sin un GTFS estático que permita
    # relacionar stop_id/stop_sequence con cada parada, no debemos inventar el nombre.
    nombre_parada = "Próxima parada"
    if stop_sequence is not None:
        try:
            seq = int(stop_sequence)
            nombre_parada = f"Próxima parada (secuencia {seq})"
        except (ValueError, TypeError):
            pass

    return destino, nombre_parada

def procesar_ocupacion(raw_status):
    if raw_status is None or raw_status == "":
        return 'Sin datos', 'gray'
    
    status_str = str(raw_status).strip().upper()
    enum_map = {
        '0': 'EMPTY',
        '1': 'MANY_SEATS_AVAILABLE',
        '2': 'FEW_SEATS_AVAILABLE',
        '3': 'STANDING_ROOM_ONLY',
        '4': 'CRUSHED_STANDING_ROOM_ONLY',
        '5': 'FULL',
        '6': 'NOT_ACCEPTING_PASSENGERS'
    }
    clave = enum_map.get(status_str, status_str)
    return TRADUCCION_OCUPACION.get(clave, ('Sin datos', 'gray'))

def obtener_estimaciones_tripupdates(linea_objetivo):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': '*/*'}
    estimaciones = {}
    
    # IDs válidos de la concesión actual
    rutas_validas = list(CONCESIONES.get(linea_objetivo, {}).keys())

    for url in TRIPUPDATES_ENDPOINTS:
        try:
            res = session.get(url, headers=headers, timeout=8, verify=False)
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
                    es_linea_bus = any(rid in r_id for rid in rutas_validas) or (linea_objetivo in r_id.upper())

                    if es_linea_bus:
                        delay_sec = tu.delay if tu.HasField('delay') else 0
                        proxima_parada = "En trayecto"
                        
                        destino, _ = resolver_destino_y_parada(linea_objetivo, r_id, direction_id, None)

                        for stu in tu.stop_time_update:
                            arr_time = stu.arrival.time if stu.HasField('arrival') else (stu.departure.time if stu.HasField('departure') else 0)
                            if arr_time > ahora or arr_time == 0:
                                minutos = max(0, int((arr_time - ahora) / 60)) if arr_time > 0 else None
                                stop_seq = stu.stop_sequence if stu.HasField('stop_sequence') else None

                                _, nombre_parada = resolver_destino_y_parada(linea_objetivo, r_id, direction_id, stop_seq)
                                proxima_parada = f"{nombre_parada}" + (f" ({minutos} min)" if minutos is not None else "")

                                if stu.HasField('arrival') and stu.arrival.HasField('delay'):
                                    delay_sec = stu.arrival.delay
                                elif stu.HasField('departure') and stu.departure.HasField('delay'):
                                    delay_sec = stu.departure.delay
                                break

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

@app.route('/api/buses/<linea>')
def get_buses(linea):
    linea_objetivo = linea.upper()
    print(f"-> Consultando API de autobuses para la concesión {linea_objetivo}...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': '*/*'}
    
    estimaciones = obtener_estimaciones_tripupdates(linea_objetivo)
    rutas_validas = list(CONCESIONES.get(linea_objetivo, {}).keys())

    for url in VEHICLES_ENDPOINTS:
        try:
            res = session.get(url, headers=headers, timeout=8, verify=False)
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
                    v_id = str(v.vehicle.id) if (v.HasField('vehicle') and v.vehicle.id) else coche_num
                    direction_id = v.trip.direction_id if (v.HasField('trip') and v.trip.HasField('direction_id')) else None

                    # Garantiza que SOLO se carguen los buses de la concesión pulsada
                    es_linea_bus = any(rid in r_id for rid in rutas_validas) or (linea_objetivo in r_id.upper())

                    if es_linea_bus:
                        lat = v.position.latitude if v.HasField('position') else None
                        lon = v.position.longitude if v.HasField('position') else None
                        
                        if v.HasField('occupancy_status'):
                            raw_occ = v.occupancy_status
                        elif hasattr(v, 'occupancy_status'):
                            raw_occ = getattr(v, 'occupancy_status')
                        else:
                            raw_occ = None

                        occ_texto, occ_color = procesar_ocupacion(raw_occ)
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

                        # Las coordenadas 0.0 son válidas; comprobar presencia, finitud y rango.
                        if lat is not None and lon is not None:
                            lat = float(lat)
                            lon = float(lon)
                            if not (math.isfinite(lat) and math.isfinite(lon)):
                                continue
                            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                                continue
                            buses.append({
                                'id': v_id or coche_num,
                                'coche': coche_num,
                                'matricula': matricula,
                                'lat': float(lat),
                                'lon': float(lon),
                                'ocupacion': occ_texto,
                                'ocupacion_color': occ_color,
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
