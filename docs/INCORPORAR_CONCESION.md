# Incorporar una nueva concesión a Mapa TPA

La aplicación carga los datos GTFS de cada concesión de forma independiente. Un ZIP puede incluir una o varias agencias; el importador separa las rutas por `agency_id`.

## 1. Importar GTFS estático

Descarga el ZIP GTFS oficial y ejecuta el importador desde la raíz del proyecto:

```bash
python scripts/importar_gtfs.py T02 descarga_gtfs.zip
```

Si el ZIP contiene varias agencias, ejecuta el comando una vez por cada código que quieras incorporar. Por ejemplo:

```bash
python scripts/importar_gtfs.py T02 descarga_gtfs.zip
python scripts/importar_gtfs.py T04 descarga_gtfs.zip
python scripts/importar_gtfs.py T06 descarga_gtfs.zip
```

El importador selecciona solo las rutas cuyo `agency_id` coincide con el código indicado. A partir de esas rutas conserva únicamente sus viajes, paradas utilizadas, horarios, calendarios y excepciones. Así no se mezclan concesiones al reutilizar un ZIP multiactor.

El importador admite ficheros GTFS en la raíz del ZIP o dentro de una carpeta. Genera tres ficheros en `data/`:

- `gtfs_t02_map.json.gz.b64`: rutas, paradas y coordenadas.
- `gtfs_t02_lookup.json.gz.b64`: relación entre identificadores de paradas, viajes y secuencias.
- `gtfs_t02_timetable.json.gz.b64`: servicios, calendarios, excepciones y horarios.

El ZIP debe contener `routes.txt`, `stops.txt`, `trips.txt` y `stop_times.txt`. Para habilitar horarios debe aportar calendarios y/o excepciones utilizables en `calendar.txt` y `calendar_dates.txt`.

## 2. Incorporar los ficheros

Comprueba el resumen del importador y añade los tres ficheros generados a `data/`. Al arrancar Flask, el catálogo detecta automáticamente qué concesiones tienen datos cargados.

La aplicación activa las paradas y rutas cuando encuentra un mapa válido. La consulta de horarios se habilita cuando también hay servicios, calendarios y horas de paso utilizables.

## 3. Tiempo real: configuración explícita

No se deduce la disponibilidad de GTFS-RT por tener GTFS estático. El piloto de posiciones y actualizaciones en tiempo real configurado actualmente es T05.

Cuando exista una fuente GTFS-RT real y probada para otra concesión:

1. Añade el código a `CONCESIONES_CON_TIEMPO_REAL` en `gtfs_datasets.py`.
2. Configura sus URLs de VehiclePositions y TripUpdates en `FUENTES_GTFSRT_POR_CONCESION` en `app.py`.
3. Verifica que los `route_id`, `trip_id` y `stop_id` del feed coinciden con el GTFS estático publicado.
4. Añade pruebas con muestras reales antes de activar la función.

No añadas una concesión a esa configuración solo porque comparta operador, plataforma o URL con otra.

## Consideraciones

- El importador no incorpora geometrías de `shapes.txt`: los trazados de líneas están desactivados en el mapa actual.
- Los calendarios GTFS deben actualizarse cuando el organismo publique un nuevo ZIP. Comprueba siempre la fecha final de vigencia antes de anunciar horarios.
- Si una concesión utiliza `frequencies.txt`, comprueba primero su modelo de servicio: el importador trabaja con viajes y horas explícitas en `stop_times.txt`.
- Los datos GTFS deben respetar las condiciones de uso y atribución de su fuente.
