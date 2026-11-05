# Configuración del buzón de sugerencias y quejas

El formulario del menú inicial envía los mensajes por correo electrónico desde el servidor. No publica las quejas en GitHub ni guarda los mensajes en el disco efímero de Render.

## Variables privadas en Render

En el servicio web de Mapa TPA, abre **Environment** y añade estas variables:

| Variable | Valor |
|---|---|
| `BUZON_SMTP_HOST` | Servidor SMTP del proveedor de correo |
| `BUZON_SMTP_PORT` | `587` para STARTTLS o `465` para SSL |
| `BUZON_SMTP_USER` | Usuario de la cuenta SMTP |
| `BUZON_SMTP_PASSWORD` | Contraseña SMTP o contraseña de aplicación |
| `BUZON_EMAIL_DESTINO` | Dirección que recibirá las sugerencias y quejas |

Opcional:

| Variable | Valor |
|---|---|
| `BUZON_EMAIL_REMITENTE` | Dirección remitente; por defecto se usa `BUZON_SMTP_USER` |
| `BUZON_SMTP_SSL` | `true` para SSL directo (normalmente puerto 465); si se omite se usa STARTTLS |

No guardes contraseñas ni credenciales SMTP en el repositorio. Configúralas solo como variables privadas de entorno de Render.

## Comportamiento

- El formulario solo habilita el envío cuando `/api/buzon/estado` confirma que el servicio está configurado.
- Los campos de nombre y correo de respuesta son opcionales.
- El correo de respuesta se utiliza como `Reply-To`; no se exige identificar al remitente.
- Se limita el tamaño del mensaje y se aplica un intervalo mínimo de 60 segundos entre envíos desde una misma IP.
- Si falta alguna variable requerida, el buzón informa de que no está disponible y no simula que el mensaje se haya enviado.
