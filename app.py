from flask import Flask, render_template, jsonify
import requests
import urllib3
import gzip
import time
import ssl
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# Adaptador SSL para permitir servidores GTFS antiguos en Linux/Render
class LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.options |= 0x4  # Permitir OP_LEGACY_SERVER_CONNECT
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

ROUTES_T05 = [
    "d3efc296-1916-41c3-90c2-166ff0e8e4fb",  # T05-01 (Fraga -> Zaragoza)
    "61b2122d-39cc-44e6-89f0-3057b6528c54"   # T05-20 (Zaragoza -> Fraga)
]

# PARADAS SENTIDO FRAGA (Zaragoza -> Fraga) - ÍNDICES BASE 0
PARADAS_A_FRAGA = [
    "Zaragoza. Estación Intermodal",               # Seq 0
    "Lugarico de Cerdán",                          # Seq 1
    "La Puebla Alfindén",                          # Seq 2
    "Alfajarín. Avenida Europa 127",               # Seq 3
    "Nuez de Ebro. Avenida Zaragoza, 1",           # Seq 4
    "Condado (El)",                                # Seq 5
    "Villafranca de Ebro. Avenida Zaragoza Nº 1",  # Seq 6
    "Osera de Ebro. C/ San Martín S/N",            # Seq 7
    "Pina de Ebro. Plaza de España",               # Seq 8
    "Venta de Santa Lucía",                        # Seq 9
    "Bujaraloz. Rte español (original)",           # Seq 10
    "Peñalba",                                     # Seq 11
    "Candasnos. C/Zaragoza Nº83",                  # Seq 12
    "Fraga. Estación"                              # Seq 13
]

# PARADAS SENTIDO ZARAGOZA (Fraga -> Zaragoza) - ÍNDICES BASE 0
PARADAS_A_ZARAGOZA = [
    "Fraga. Estación",                              # Seq 0
    "Candasnos. C/Zaragoza Nº83",                  # Seq 1
    "Peñalba",                                     # Seq 2
    "Bujaraloz. Rte español (original)",           # Seq 3
    "Venta de Santa Lucía",                        # Seq 4
    "Pina de Ebro. Plaza de España",               # Seq 5
    "Osera de Ebro. C/ San Martín S/N",            # Seq 6
    "Villafranca de Ebro. Avenida Zaragoza Nº 1",  # Seq 7
    "Condado (El)",                                # Seq 8
    "Nuez de Ebro. Avenida Zaragoza, 1",           # Seq 9
    "Alfajarín. Avenida Europa 127",               # Seq 10
    "La Puebla Alfindén",                          # Seq 11
    "Lugarico de Cerdán",                          # Seq 12
    "Zaragoza. Estación Intermodal"               # Seq 13
]

NOMBRES_PARADAS = {}

TRADUCCION_OCUPACION = {
    'EMPTY': ('Vacío', 'green'),
    'MANY_SEATS_AVAILABLE': ('Asientos libres', 'green'),
    'FEW_SEATS_AVAILABLE': ('Pocos asientos', 'yellow'),
    'STANDING_ROOM_ONLY': ('Solo de pie', 'orange'),
    'CRUSHED_STANDING_ROOM_ONLY': ('Espacio muy reducido', 'red'),
    'FULL': ('Lleno', 'red'),
    'NOT_ACCEPTING_PASSENGERS': ('No admite pasajeros', 'red')
}

def resolver_sentido(direction_id, route_id):
    if direction_id is not None:
        return str(direction_id) == "1"
    if "61b2122d" in str(route_id):
        return True
    if "d3efc296" in str(route_id):
        return False
    return True

def obtener_nombre_parada(stop_sequence, stop_id, route_id, direction_id=None):
    if stop_id and stop_id in NOMBRES_PARADAS:
        return NOMBRES_PARADAS[stop_id]

    es_sentido_fraga = resolver_sentido(direction_id, route_id)
    lista = PARADAS_A_FRAGA if es_sentido_fraga else PARADAS_A_ZARAGOZA

    if stop_sequence is not None and stop_sequence >= 0:
        idx = stop_sequence
        if 0 <= idx < len(lista):
            return lista[idx]

    return f"Parada nº {stop_sequence}" if stop_sequence is not None else "Próxima parada"

def procesar_ocupacion(raw_status):
    if raw_status is None or raw_status == '':
        return 'Sin datos', 'gray'
    status_str = str(raw_status).strip().upper()
    enum_map = {
        '0': 'EMPTY', '1': 'MANY_SEATS_AVAILABLE', '2': 'FEW_SEATS_AVAILABLE',
        '3': 'STANDING_ROOM_ONLY', '4': 'CRUSHED_STANDING_ROOM_ONLY',
        '5': 'FULL', '6': 'NOT_ACCEPTING_PASSENGERS'
    }
    status_str = enum_map.get(status_str, status_str)
    return TRADUCCION_OCUPACION.get(status_str, ('Sin datos', 'gray'))

def obtener_estimaciones_tripupdates():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*'
    }
    estimaciones = {}

    for url in TRIPUPDATES_ENDPOINTS:
        try:
            res = session.get(url, headers=headers, timeout=8)
            if res.status_code != 200 or not res.content:
                continue

            content = res.content
            if content.startswith(b'\x1f\x8b'):
                try: content = gzip.decompress(content)
                except Exception: pass

            try:
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

                        es_t05 = any(rid in r_id for rid in ROUTES_T05) or "T05" in r_id or coche_label in ["13443", "13444"]

                        if es_t05:
                            delay_sec = tu.delay if tu.HasField('delay') else 0
                            proxima_parada = "En trayecto"
                            es_fraga = resolver_sentido(direction_id, r_id)
                            destino = "Fraga" if es_fraga else "Zaragoza"

                            for stu in tu.stop_time_update:
                                arr_time = stu.arrival.time if stu.HasField('arrival') else (stu.departure.time if stu.HasField('departure') else 0)
                                if arr_time > ahora or arr_time == 0:
                                    minutos = max(0, int((arr_time - ahora) / 60)) if arr_time > 0 else None
                                    stop_seq = stu.stop_sequence if stu.HasField('stop_sequence') else None
                                    stop_id = str(stu.stop_id) if stu.HasField('stop_id') else ""

                                    nombre_parada = obtener_nombre_parada(stop_seq, stop_id, r_id, direction_id)
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

                            info = {
                                'retraso': texto_retraso,
                                'proxima_parada': proxima_parada,
                                'destino': destino
                            }

                            if v_id: estimaciones[v_id] = info
                            if coche_label: estimaciones[coche_label] = info
                            if trip_id: estimaciones[trip_id] = info

                if estimaciones:
                    return estimaciones
            except Exception as e:
                print(f"[TRIPUPDATES Error Parseo]: {e}")
        except Exception as e:
            print(f"[TRIPUPDATES Error Conexión {url}]: {e}")

    return estimaciones

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/buses/t05')
def get_t05_buses():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*'
    }
    estimaciones = obtener_estimaciones_tripupdates()

    for url in VEHICLES_ENDPOINTS:
        try:
            res = session.get(url, headers=headers, timeout=8)
            if res.status_code != 200 or not res.content:
                print(f"[VEHICLES] HTTP Status: {res.status_code} desde {url}")
                continue

            content = res.content
            if content.startswith(b'\x1f\x8b'):
                try: content = gzip.decompress(content)
                except Exception: pass

            buses = []

            try:
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

                        es_t05 = any(rid in r_id for rid in ROUTES_T05) or "T05" in r_id or coche_num in ["13443", "13444"] or matricula in ["2560MSV", "2563MSV"]

                        if es_t05:
                            lat = v.position.latitude if v.HasField('position') else None
                            lon = v.position.longitude if v.HasField('position') else None
                            raw_occ = v.occupancy_status if hasattr(v, 'occupancy_status') else None
                            occ_texto, occ_color = procesar_ocupacion(raw_occ)

                            info_est = (
                                estimaciones.get(v_id) or 
                                estimaciones.get(coche_num) or 
                                estimaciones.get(trip_id) or 
                                {'retraso': 'En hora', 'proxima_parada': 'En trayecto', 'destino': 'Desconocido'}
                            )

                            if lat and lon:
                                buses.append({
                                    'id': v_id or coche_num or "13443",
                                    'coche': coche_num or "13443",
                                    'matricula': matricula or "2560MSV",
                                    'lat': float(lat),
                                    'lon': float(lon),
                                    'ocupacion': occ_texto,
                                    'ocupacion_color': occ_color,
                                    'retraso': info_est.get('retraso', 'En hora'),
                                    'proxima_parada': info_est.get('proxima_parada', 'En trayecto'),
                                    'destino': info_est.get('destino', 'En trayecto')
                                })

                if buses:
                    return jsonify(buses)
            except Exception as e:
                print(f"[VEHICLES Error Parseo]: {e}")

        except Exception as e:
            print(f"[VEHICLES Error Conexión {url}]: {e}")

    return jsonify([])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
