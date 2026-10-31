# Incorporar una nueva concesión a Mapa TPA

La aplicación ya carga el GTFS estático de cada concesión de forma independiente. No hay que duplicar la web ni crear otro servidor para T01, T02, etc.

## 1. Importar el GTFS estático

Descarga el ZIP GTFS estático oficial de la concesión y ejecútalo desde la raíz del proyecto:

```bash
python scripts/importar_gtfs.py T01 descarga_t01.zip
```

El importador admite que los ficheros GTFS estén en la raíz del ZIP o dentro de una carpeta. Genera estos tres ficheros dentro de `data/`:

- `gtfs_t01_map.json.gz.b64`: rutas, paradas y coordenadas.
- `gtfs_t01_lookup.json.gz.b64`: relación entre identificadores de paradas, viajes y secuencias.
- `gtfs_t01_timetable.json.gz.b64`: servicios, calendarios, excepciones y horarios.

El código de la concesión determina el nombre de los ficheros. Para T13, por ejemplo, se usa `T13` y se generan ficheros `gtfs_t13_...`.

El ZIP debe contener `routes.txt`, `stops.txt`, `trips.txt` y `stop_times.txt`. Para habilitar horarios también debe aportar `calendar.txt`, `calendar_dates.txt`, o ambos, con al menos un calendario/servicio utilizable.

## 2. Añadir los ficheros al proyecto

Comprueba el resumen que muestra el importador y añade los tres ficheros generados a `data/`. Al arrancar Flask, el registro detecta automáticamente qué concesiones tienen datos cargados.

La pantalla inicial activa por sí sola una concesión cuando encuentra un mapa con rutas y paradas. Si se importan mapas pero no calendarios, se mostrarán las paradas, pero no se anunciarán horarios que no se puedan calcular.

## 3. Tiempo real: configuración explícita

No se deduce la disponibilidad de GTFS-RT por tener GTFS estático. El piloto de posiciones y actualizaciones en tiempo real configurado actualmente es T05.

Cuando exista una fuente GTFS-RT real y probada para otra concesión:

1. Añade el código a `CONCESIONES_CON_TIEMPO_REAL` en `gtfs_datasets.py`.
2. Configura sus URLs de VehiclePositions y TripUpdates en `FUENTES_GTFSRT_POR_CONCESION` en `app.py`.
3. Verifica que los `route_id`, `trip_id` y `stop_id` del feed coinciden con el GTFS estático publicado para esa concesión.
4. Añade pruebas con muestras reales antes de activar la función.

No añadas una concesión a esa configuración solo porque comparta operador, plataforma o URL con otra. Hay que verificar su cobertura real.

## 4. Qué se activa automáticamente

Cuando el GTFS estático está importado, la plataforma habilita el acceso al mapa y a sus paradas. Si también hay calendarios y horarios válidos, habilita la consulta de salidas programadas. Los filtros y colores de línea se construyen a partir de las rutas del propio GTFS.

El seguimiento de vehículos, la predicción de llegadas y los avisos de favoritos solo se activan para concesiones que tengan una fuente GTFS-RT configurada y validada.

## Consideraciones

- El importador no incorpora las geometrías de `shapes.txt`: la visualización de trazados de líneas está desactivada en el mapa actual.
- Los datos GTFS deben actualizarse cuando el organismo publique un nuevo ZIP, especialmente sus calendarios y fechas de vigencia.
- Si una concesión utiliza horarios basados en `frequencies.txt`, comprueba primero su modelo de servicio: el importador actual trabaja con viajes y horas explícitas en `stop_times.txt`.
- Los ficheros GTFS importados deben respetar las condiciones de uso y atribución de su fuente.
