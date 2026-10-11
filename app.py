from flask import Flask, render_template, jsonify
import requests
import urllib3
import json
import gzip
import re

# Desactivar advertencias de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# Servidor oficial de GMV y espejo del Consorcio
ENDPOINTS = [
    "https://tpaits.gmv.com/gtfsrtapi/api/vehiclepositions",
    "https://sintram.consorciozaragoza.es/api/gtfs-rt/vehicle-positions"
]

# IDs de todas las variantes conocidas de la línea T05 (T05-01, T05-20, etc.)
ROUTES_T05 = [
    "d3efc296-1916-41c3-90c2-166ff0e8e4fb",  # T05-01
    "61b2122d-39cc-44e6-89f0-3057b6528c54"   # T05-20
]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/buses/t05')
def get_t05_buses():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*'
    }

    for url in ENDPOINTS:
        try:
            res = requests.get(url, headers=headers, verify=False, timeout=6)
            if res.status_code != 200 or not res.content:
                continue

            content = res.content
            if content.startswith(b'\x1f\x8b'):
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass

            buses = []

            # 1. PARSEO PROTOBUF BINARIO (GMV GTFS-RT)
            try:
                from google.transit import gtfs_realtime_pb2
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(content)

                for entity in feed.entity:
                    if entity.HasField('vehicle'):
                        v = entity.vehicle
                        r_id = str(v.trip.route_id) if v.HasField('trip') else ""
                        coche_num = str(v.vehicle.label) if (v.HasField('vehicle') and v.vehicle.label) else ""
                        matricula = str(v.vehicle.license_plate) if (v.HasField('vehicle') and hasattr(v.vehicle, 'license_plate')) else ""

                        # Detectar por cualquiera de las IDs de ruta T05, por código genérico o coche/matrícula
                        es_t05 = any(rid in r_id for rid in ROUTES_T05) or "T05" in r_id or coche_num == "13444" or matricula == "2563MSV"

                        if es_t05:
                            lat = v.position.latitude if v.HasField('position') else None
                            lon = v.position.longitude if v.HasField('position') else None

                            if lat and lon:
                                buses.append({
                                    'id': str(v.vehicle.id if v.HasField('vehicle') else coche_num or "13444"),
                                    'coche': coche_num or "13444",
                                    'matricula': matricula or "2563MSV",
                                    'lat': float(lat),
                                    'lon': float(lon)
                                })

                if buses:
                    print(f"-> [ÉXITO GMV] Localizados {len(buses)} bus(es) T05 en {url}")
                    return jsonify(buses)
            except Exception:
                pass

            # 2. PARSEO JSON (RESPALDO TEXTO)
            try:
                text_data = content.decode('utf-8', errors='ignore')
                data = json.loads(text_data)
                entities = data.get('entity', [data]) if isinstance(data, dict) else data
                if not isinstance(entities, list):
                    entities = [entities]

                for item in entities:
                    if not isinstance(item, dict):
                        continue

                    v_data = item.get('vehicle', item) if isinstance(item.get('vehicle'), dict) else item
                    trip = item.get('trip') or (v_data.get('trip') if isinstance(v_data, dict) else {}) or {}
                    pos = item.get('position') or (v_data.get('position') if isinstance(v_data, dict) else {}) or {}
                    veh = item.get('vehicle') if isinstance(item.get('vehicle'), dict) and ('label' in item['vehicle'] or 'id' in item['vehicle']) else (v_data.get('vehicle') if isinstance(v_data, dict) else {})

                    r_id = str(trip.get('routeId') or trip.get('route_id') or '')
                    c_label = str(veh.get('label') or veh.get('id') or '')
                    c_plate = str(veh.get('licensePlate') or veh.get('license_plate') or '')

                    es_t05 = any(rid in r_id for rid in ROUTES_T05) or "T05" in r_id or c_label == "13444" or c_plate == "2563MSV" or any(rid in json.dumps(item) for rid in ROUTES_T05)

                    if es_t05:
                        lat = pos.get('latitude')
                        lon = pos.get('longitude')

                        if lat and lon:
                            buses.append({
                                'id': str(veh.get('id', c_label) if isinstance(veh, dict) else c_label),
                                'coche': c_label or "13444",
                                'matricula': c_plate or "2563MSV",
                                'lat': float(lat),
                                'lon': float(lon)
                            })

                if buses:
                    print(f"-> [ÉXITO JSON] Localizados {len(buses)} bus(es) T05 en {url}")
                    return jsonify(buses)
            except Exception:
                pass

            # 3. EXTRACTION REGEX DE EMERGENCIA
            try:
                text_data = content.decode('utf-8', errors='ignore')
                if any(rid in text_data for rid in ROUTES_T05) or "13444" in text_data or "2563MSV" in text_data:
                    lat_m = re.search(r'latitude["\s:]+([\d\.-]+)', text_data)
                    lon_m = re.search(r'longitude["\s:]+([\d\.-]+)', text_data)
                    label_m = re.search(r'label["\s:]+"?([\w]+)"?', text_data)
                    plate_m = re.search(r'licensePlate["\s:]+"?([\w]+)"?', text_data)

                    if lat_m and lon_m:
                        buses.append({
                            'id': '2befc504-928b-4664-ae2f-74406860dcc4',
                            'coche': label_m.group(1) if label_m else '13444',
                            'matricula': plate_m.group(1) if plate_m else '2563MSV',
                            'lat': float(lat_m.group(1)),
                            'lon': float(lon_m.group(1))
                        })
                        print(f"-> [ÉXITO REGEX] Bus localizado en {url}")
                        return jsonify(buses)
            except Exception:
                pass

        except Exception:
            continue

    print("-> Buscando señal del autobús...")
    return jsonify([])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)