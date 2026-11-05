# Configuración del buzón de sugerencias y quejas

El formulario del menú inicial envía los mensajes por correo electrónico desde el servidor. Los mensajes llegan a **ribota2010@gmail.com** por defecto. No se publican en GitHub ni se guardan en el disco efímero de Render.

## Configuración de Gmail en Render

El código ya lleva estos valores predeterminados:

| Ajuste | Valor |
|---|---|
| Servidor SMTP | `smtp.gmail.com` |
| Puerto | `587` |
| Seguridad | STARTTLS |
| Usuario remitente | `ribota2010@gmail.com` |
| Destinatario | `ribota2010@gmail.com` |

Solo falta añadir una variable privada en el servicio web de Mapa TPA, dentro de **Render → Environment**:

| Variable | Valor |
|---|---|
| `BUZON_SMTP_PASSWORD` | Contraseña de aplicación de la cuenta Gmail |

No introduzcas tu contraseña normal de Google. Google indica que las contraseñas de aplicación requieren que la verificación en dos pasos esté activada. [Ayuda oficial sobre contraseñas de aplicación](https://support.google.com/accounts/answer/185833?hl=es).

Para crearla, inicia sesión en la cuenta `ribota2010@gmail.com`, activa la verificación en dos pasos si aún no está activada y abre la sección **Contraseñas de aplicaciones** de tu cuenta de Google. Genera una contraseña para Mapa TPA y copia ese código de 16 caracteres en Render como valor de `BUZON_SMTP_PASSWORD`. No la compartas en el chat ni la guardes en GitHub.

Al guardar la variable, Render reiniciará el servicio y el buzón podrá comprobar que está disponible.

## Variables opcionales

| Variable | Valor |
|---|---|
| `BUZON_SMTP_HOST` | Sobrescribe el servidor predeterminado |
| `BUZON_SMTP_PORT` | Sobrescribe el puerto (por defecto `587`) |
| `BUZON_SMTP_USER` | Sobrescribe el usuario remitente |
| `BUZON_EMAIL_DESTINO` | Sobrescribe el destinatario |
| `BUZON_EMAIL_REMITENTE` | Sobrescribe el remitente; por defecto se usa el usuario SMTP |
| `BUZON_SMTP_SSL` | `true` para SSL directo, normalmente en el puerto `465`; si se omite, usa STARTTLS |

## Comportamiento

- El formulario solo habilita el envío cuando `/api/buzon/estado` confirma que el servicio está configurado.
- Los campos de nombre y correo de respuesta son opcionales.
- El correo de respuesta se utiliza como `Reply-To`; no se exige identificar al remitente.
- Se limita el tamaño del mensaje y se aplica un intervalo mínimo de 60 segundos entre envíos desde una misma IP.
- Si falta la contraseña de aplicación, el buzón informa de que no está disponible y no simula que el mensaje se haya enviado.
