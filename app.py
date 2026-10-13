import os
# Desactivar extensión C de Protobuf para compatibilidad con Python 3.11+
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

from flask import Flask, render_template, jsonify
import requests
import urllib3
import gzip
import time
import ssl
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

# IDs de rutas GTFS pertenecientes a la T05
ROUTES_T05 = [
    "d3efc296-1916-41c3-90c2-166ff0e8e4fb",  # T05-01 (Zaragoza - Fraga)
    "61b2122d-39cc-44e6-89f0-3057b6528c54"   # T05-20 (Castejón de Monegros - Zaragoza por Pueblos)
]

# --- LISTAS DE PARADAS ---

# T05-01: Zaragoza <-> Fraga
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

# T05-20: Castejón de Monegros <-> Zaragoza por Pueblos (ID: 61b2122d-39cc-44e6-89f0-3057b6528c54)
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

TRADUCCION_OCUPACION = {
    'EMPTY': ('Vacío', 'green'),
    'MANY_SEATS_AVAILABLE': ('Asientos libres', 'green'),
    'FEW_SEATS_AVAILABLE': ('Pocos asientos', 'yellow'),
    'STANDING_ROOM_ONLY': ('Solo de pie', 'orange'),
    'CRUSHED_STANDING_ROOM_ONLY': ('Espacio muy reducido', 'red'),
    'FULL': ('Lleno', 'red'),
    'NOT_ACCEPTING_PASSENGERS': ('No admite pasajeros', 'red')
}

def resolver_destino_y_parada(route_id, direction_id, stop_sequence):
    """
    Diferencia la línea de Castejón (61b2122d) de la de Fraga (d3efc296)
    y devuelve el destino exacto y la parada actual.
    """
    r_id = str(route_id).lower() if route_id else ""
    dir_str = str(direction_id) if direction_id is not None else None

    # LÍNEA DE CASTEJÓN DE MONEGROS (T05-20)
    if "61b2122d" in r_id:
        if dir_str == "1":
            destino = "Castejón de Monegros"
            paradas = PARADAS_ZARAGOZA_A_CASTEJON
        elif dir_str == "0":
            destino = "Zaragoza"
            paradas = PARADAS_CASTEJON_A_ZARAGOZA
        else:
            destino = "Castejón de Monegros"
            paradas = PARADAS_ZARAGOZA_A_CASTEJON

    # LÍNEA DE FRAGA (T05-01 O GENÉRICA)
    else:
        if dir_str == "1":
            destino = "Fraga"
            paradas = PARADAS_ZARAGOZA_A_FRAGA
        elif dir_str == "0":
            destino = "Zaragoza"
            paradas = PARADAS_FRAGA_A_ZARAGOZA
        else:
            destino = "Fraga"
            paradas = PARADAS_ZARAGOZA_A_FRAGA

    nombre_parada = "En trayecto"
    if stop_sequence is not None:
        try:
            seq = int(stop_sequence)
            if 0 <= seq < len(paradas):
                nombre_parada = paradas[seq]
            else:
                nombre_parada = f"Parada nº {seq}"
        except (ValueError, TypeError):
            nombre_parada = "Próxima parada"

    return destino, nombre_parada

def procesar_ocupacion(raw_status):
    """
    Parcheado: Se verifica que raw_status no sea None ni cadena vacía.
    De esta forma se evita que el valor 0 ('EMPTY') se evalúe sintácticamente como Falso.
    """
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

                    es_linea_bus = (linea_objetivo in r_id.upper()) or (linea_objetivo == "T05" and (any(rid in r_id for rid in ROUTES_T05) or coche_label in ["13443", "13444", "13445"]))

                    if es_linea_bus:
                        delay_sec = tu.delay if tu.HasField('delay') else 0
                        proxima_parada = "En trayecto"
                        
                        # Determinar el destino correcto según la línea y dirección
                        destino, _ = resolver_destino_y_parada(r_id, direction_id, None)

                        for stu in tu.stop_time_update:
                            arr_time = stu.arrival.time if stu.HasField('arrival') else (stu.departure.time if stu.HasField('departure') else 0)
                            if arr_time > ahora or arr_time == 0:
                                minutos = max(0, int((arr_time - ahora) / 60)) if arr_time > 0 else None
                                stop_seq = stu.stop_sequence if stu.HasField('stop_sequence') else None

                                if linea_objetivo == "T05":
                                    _, nombre_parada = resolver_destino_y_parada(r_id, direction_id, stop_seq)
                                else:
                                    nombre_parada = f"Parada {stop_seq}" if stop_seq is not None else "Próxima parada"

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
    print(f"-> Consultando API de autobuses para línea {linea_objetivo}...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': '*/*'}
    estimaciones = obtener_estimaciones_tripupdates(linea_objetivo)

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

                    es_linea_bus = (linea_objetivo in r_id.upper()) or (linea_objetivo == "T05" and (any(rid in r_id for rid in ROUTES_T05) or coche_num in ["13443", "13444", "13445"] or matricula in ["2560MSV", "2563MSV", "5992MSG"]))

                    if es_linea_bus:
                        lat = v.position.latitude if v.HasField('position') else None
                        lon = v.position.longitude if v.HasField('position') else None
                        
                        # Detección segura de occupancy_status (evita descartar el entero 0 / EMPTY)
                        if v.HasField('occupancy_status'):
                            raw_occ = v.occupancy_status
                        elif hasattr(v, 'occupancy_status'):
                            raw_occ = getattr(v, 'occupancy_status')
                        else:
                            raw_occ = None

                        occ_texto, occ_color = procesar_ocupacion(raw_occ)

                        dest_defecto, _ = resolver_destino_y_parada(r_id, direction_id, None)

                        info_est = (
                            estimaciones.get(v_id) or 
                            estimaciones.get(coche_num) or 
                            estimaciones.get(trip_id) or 
                            {'retraso': 'En hora', 'proxima_parada': 'En trayecto', 'destino': dest_defecto}
                        )

                        destino_final = info_est.get('destino')
                        if not destino_final or destino_final in ['Desconocido', 'En trayecto']:
                            destino_final = dest_defecto

                        if lat and lon:
                            buses.append({
                                'id': v_id or coche_num or "13445",
                                'coche': coche_num or "13445",
                                'matricula': matricula or "5992MSG",
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
    app.run(host='0.0.0.0', port=5000, debug=True)
