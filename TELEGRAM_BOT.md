# Bot de Telegram de Mapa TPA

El worker envía un aviso cuando detecta por primera vez un nuevo servicio de las líneas **T05-01, T05-20 o T05-50** en el feed GTFS-RT de posiciones de vehículos.

## Qué publica

Cada servicio genera un único mensaje similar a este:

- 🚌 Nuevo servicio localizado
- Línea: T05-01
- Número de coche: 13444
- Matrícula: 2563MSV
- Detectado: 31/10/2026 14:48:30

La matrícula se muestra cuando el proveedor la incluye en GTFS-RT. Si no llega ese dato, el mensaje indica que no está facilitada; el bot no la deduce.

El identificador de viaje GTFS-RT se utiliza para no repetir avisos con cada actualización GPS. Si falta, se usa como alternativa la combinación de línea y vehículo. Una ausencia de al menos 15 minutos se considera una nueva aparición del servicio.

## 1. Crear el bot (sin número de teléfono)

1. En Telegram, abre [@BotFather](https://t.me/BotFather).
2. Envía `/newbot`.
3. Elige un nombre visible, por ejemplo `Mapa TPA - Servicios`.
4. Elige un username terminado en `bot`, por ejemplo `MapaTPAServiciosBot` (si está disponible).
5. BotFather te entregará un **token**. Es una contraseña: no lo publiques, no lo subas a GitHub y no lo pegues en conversaciones.

La cuenta de bot no necesita un número de teléfono propio.

## 2. Preparar el grupo

1. Añade el bot al grupo de Telegram.
2. No necesita permisos de administrador para escribir, pero debe poder enviar mensajes en el grupo.
3. Un administrador del grupo debe enviar el comando `/vincular@NombreDeTuBot` (sustituye el username por el real).
4. El bot confirmará que el grupo está vinculado.

Se puede comprobar con `/estado@NombreDeTuBot`, mandar un mensaje de prueba con `/prueba@NombreDeTuBot` o detener los avisos con `/desvincular@NombreDeTuBot`. El comando `/ayuda@NombreDeTuBot` muestra la ayuda.

El bot usa el modo de privacidad normal de Telegram: solo necesita recibir los comandos dirigidos a él; no hace falta desactivar la privacidad del grupo.

## 3. Desplegar el worker en Render

El bot es un proceso continuo independiente de la web Mapa TPA. En Render Dashboard:

1. Selecciona **New → Background Worker** y conecta el repositorio `danigar2205/mapa-tpa`, rama `main`.
2. Build Command: `pip install -r requirements.txt`.
3. Start Command: `python bot_worker.py`.
4. Añade la variable de entorno `TELEGRAM_BOT_TOKEN` con el token de BotFather.
5. Opcional: `BOT_POLL_INTERVAL=15` (segundos; el valor mínimo es 5).
6. Para conservar el registro anti-duplicados tras reinicios y despliegues, añade un Persistent Disk montado en `/var/data` y configura `BOT_STATE_DB=/var/data/mapa-tpa-telegram.sqlite3`.

**Coste de Render:** los Background Workers no tienen instancia gratuita. El worker necesita un plan de cómputo activo. El disco persistente también requiere un servicio de pago. Consulta el precio del plan que vaya a seleccionarse antes de crearlo. La web existente no necesita cambiar su comando de inicio.

## 4. Funcionamiento y límites

- Lee el feed GTFS-RT T05 aproximadamente cada 15 segundos.
- No envía un mensaje por cada actualización de posición, sino una vez por aparición de cada servicio.
- Si se cae el feed, espera y vuelve a intentarlo; no interpreta una caída como finalización de todos los servicios.
- Guarda el estado en SQLite. Sin disco persistente, un redeploy puede hacer que se vuelvan a anunciar servicios que ya estaban en marcha.
- Si el feed no facilita matrícula o número de coche, el bot lo indica en lugar de inventarlo.
- El worker no añade otras concesiones: solo reconoce las tres rutas T05 incluidas en el GTFS-RT actual.
- Los avisos dependen de la disponibilidad y calidad del feed del operador.

## Variables de entorno

| Variable | Obligatoria | Valor |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Sí | Token de BotFather |
| `BOT_POLL_INTERVAL` | No | `15` segundos |
| `BOT_STATE_DB` | Recomendable en Render | `/var/data/mapa-tpa-telegram.sqlite3` |
| `LOG_LEVEL` | No | `INFO` |

No almacenes el token en el código ni en los ficheros de configuración versionados.
