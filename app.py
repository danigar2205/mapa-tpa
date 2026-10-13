from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Base de datos completa GTFS para la Concesión T05
ROUTES_DB = {
    # ------------------------------------------------------------------
    # LÍNEA 1: T05-01 (Zaragoza - Fraga por el Eje del Ebro)
    # ------------------------------------------------------------------
    "T05-01": {
        "route_id": "T05-01",
        "route_short_name": "T05-01",
        "route_long_name": "Zaragoza - Fraga por el Eje del Ebro",
        "agency_name": "T05 - Fraga - Zaragoza por el eje del Ebro",
        "feed_id": "T05",
        "directions": {
            "0": {
                "headsign": "Fraga",
                "stops": [
                    "Zaragoza. Estación Intermodal",
                    "La Puebla Alfindén",
                    "Alfajarín",
                    "Nuez de Ebro",
                    "Villafranca de Ebro",
                    "Osera de Ebro",
                    "Pina de Ebro",
                    "Bujaraloz",
                    "Candasnos",
                    "Fraga"
                ]
            },
            "1": {
                "headsign": "Zaragoza. Estación Intermodal",
                "stops": [
                    "Fraga",
                    "Candasnos",
                    "Bujaraloz",
                    "Pina de Ebro",
                    "Osera de Ebro",
                    "Villafranca de Ebro",
                    "Nuez de Ebro",
                    "Alfajarín",
                    "La Puebla Alfindén",
                    "Zaragoza. Estación Intermodal"
                ]
            }
        }
    },

    # ------------------------------------------------------------------
    # LÍNEA 2: T05-20 (Castejón de Monegros - Zaragoza por Pueblos)
    # ID: 61b2122d-39cc-44e6-89f0-3057b6528c54
    # ------------------------------------------------------------------
    "61b2122d-39cc-44e6-89f0-3057b6528c54": {
        "route_id": "61b2122d-39cc-44e6-89f0-3057b6528c54",
        "route_short_name": "T05-20",
        "route_long_name": "Castejón de Monegros - Zaragoza por Pueblos",
        "agency_name": "T05 - Fraga - Zaragoza por el eje del Ebro",
        "feed_id": "T05",  # Corregido: antes desasociado en T02
        "directions": {
            # Sentido 1: Castejón de Monegros -> Zaragoza
            "0": {
                "headsign": "Zaragoza. Estación Intermodal",
                "stops": [
                    "Castejón de Monegros.",
                    "La Almolda. C/ San Antonio",
                    "Bujaraloz. Rte español",
                    "Venta de Santa Lucía",
                    "Pina de Ebro. Plaza de España",
                    "Osera de Ebro. C/ San Martín S/N",
                    "Villafranca de Ebro. Avenida Zaragoza Nº 1",
                    "Condado (El)",
                    "Nuez de Ebro. Avenida Zaragoza, 1",
                    "Alfajarín. Avenida Europa 127",
                    "La Puebla Alfindén",
                    "Lugarico de Cerdán",
                    "Av. De Cataluña N.º 159",
                    "Av. Salvador Allende N.º 107",
                    "Plaza Europa",
                    "Zaragoza. Estación Intermodal"
                ]
            },
            # Sentido 2: Zaragoza -> Castejón de Monegros
            "1": {
                "headsign": "Castejón de Monegros",
                "stops": [
                    "Zaragoza. Estación Intermodal",
                    "Av. Pablo Gargallo / Plaza Europa",
                    "Av. Salvador Allende / San Juan De La Peña",
                    "Av. De Cataluña N.º 188",
                    "Lugarico de Cerdán",
                    "La Puebla Alfindén",
                    "Alfajarín. Avenida Europa 127",
                    "Nuez de Ebro. Avenida Zaragoza, 1",
                    "Condado (El)",
                    "Villafranca de Ebro. Avenida Zaragoza Nº 1",
                    "Osera de Ebro. C/ San Martín S/N",
                    "Pina de Ebro. Plaza de España",
                    "Venta de Santa Lucía",
                    "Bujaraloz. Rte español",
                    "La Almolda. C/ San Antonio",
                    "Castejón de Monegros."
                ]
            }
        }
    }
}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/routes", methods=["GET"])
def get_routes():
    """Devuelve la lista de todas las líneas disponibles."""
    summary = []
    for r_id, r_data in ROUTES_DB.items():
        summary.append({
            "route_id": r_data["route_id"],
            "short_name": r_data["route_short_name"],
            "long_name": r_data["route_long_name"]
        })
    return jsonify(summary)

@app.route("/api/route/<route_id>", methods=["GET"])
def get_route_details(route_id):
    """Devuelve los datos de una línea específica."""
    route_data = ROUTES_DB.get(route_id)
    if not route_data:
        return jsonify({"error": "Línea no encontrada"}), 404
    return jsonify(route_data)

@app.route("/api/trip/active/<route_id>/<direction_id>", methods=["GET"])
def get_active_trip(route_id, direction_id):
    """
    Devuelve la información del trayecto activo asignando correctamente 
    el destino (trip_headsign) en lugar del nombre estático de la agencia.
    """
    route_data = ROUTES_DB.get(route_id)
    if not route_data or direction_id not in route_data["directions"]:
        return jsonify({"error": "Trayecto o dirección invalida"}), 404
    
    dir_info = route_data["directions"][direction_id]
    
    return jsonify({
        "route_id": route_id,
        "short_name": route_data["route_short_name"],
        "long_name": route_data["route_long_name"],
        "trip_headsign": dir_info["headsign"],  # Muestra Castejón o Zaragoza según corresponda
        "direction_id": direction_id,
        "stops": dir_info["stops"]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
