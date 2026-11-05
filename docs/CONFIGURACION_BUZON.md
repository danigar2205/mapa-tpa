# Configuración del buzón de sugerencias y quejas

El formulario del menú inicial envía los mensajes por correo electrónico desde el servidor. Los mensajes llegan a **ribota2010@gmail.com** por defecto. No se publican en GitHub ni se guardan en el disco efímero de Render.

## Configuración recomendada para Render gratuito: Resend

Los servicios gratuitos de Render bloquean las conexiones SMTP salientes en los puertos 25, 465 y 587. Por eso Gmail SMTP no puede conectarse desde el plan gratuito, aunque la contraseña de aplicación sea correcta. [Aviso oficial de Render](https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports).

El buzón admite Resend mediante HTTPS, que utiliza el puerto 443:

1. Crea una cuenta gratuita en [Resend](https://resend.com/signup) usando **ribota2010@gmail.com**, que es el destinatario configurado en Mapa TPA.
2. En Resend, crea una API key con permiso de envío y cópiala. No la compartas ni la guardes en GitHub.
3. En Render → servicio de Mapa TPA → Environment, añade:

   | Variable | Valor |
   |---|---|
   | `BUZON_RESEND_API_KEY` | Tu clave privada de Resend |

4. Guarda y despliega los cambios. El código usa por defecto `Mapa TPA <onboarding@resend.dev>` como remitente de prueba y envía a `ribota2010@gmail.com`.

**Importante:** el remitente compartido `onboarding@resend.dev` es para pruebas y Resend limita el envío a la dirección propietaria de la cuenta. Por eso la cuenta de Resend debe estar registrada con `ribota2010@gmail.com` y el destinatario debe seguir siendo esa dirección. Para enviar a otras direcciones o utilizar un remitente propio, hay que verificar un dominio en Resend y usar una dirección de ese dominio. [Referencia de envío de Resend](https://resend.com/docs/api-reference/emails/send-email).

El plan gratuito publicado por Resend incluye 3.000 correos al mes y un máximo de 100 al día. [Precios oficiales](https://resend.com/pricing).

## Alternativa: Gmail SMTP con Render de pago

Si el servicio de Render se actualiza a un plan de pago, se puede seguir utilizando Gmail SMTP. La configuración predeterminada es:

| Ajuste | Valor |
|---|---|
| Servidor SMTP | `smtp.gmail.com` |
| Puerto | `587` |
| Seguridad | STARTTLS |
| Usuario remitente | `ribota2010@gmail.com` |
| Destinatario | `ribota2010@gmail.com` |
| Variable secreta | `BUZON_SMTP_PASSWORD` (contraseña de aplicación de Google) |

Google requiere que la verificación en dos pasos esté activada para generar contraseñas de aplicación. [Ayuda oficial](https://support.google.com/accounts/answer/185833?hl=es).

## Variables opcionales

| Variable | Uso |
|---|---|
| `BUZON_RESEND_FROM` | Sobrescribe el remitente de Resend; para usar otro dominio debe estar verificado |
| `BUZON_EMAIL_DESTINO` | Sobrescribe el destinatario; con el remitente de prueba de Resend debe coincidir con el correo propietario de la cuenta |
| `BUZON_SMTP_HOST` | Servidor SMTP alternativo (Render de pago) |
| `BUZON_SMTP_PORT` | Puerto SMTP (por defecto `587`) |
| `BUZON_SMTP_USER` | Usuario SMTP |
| `BUZON_SMTP_PASSWORD` | Contraseña de aplicación SMTP |
| `BUZON_EMAIL_REMITENTE` | Remitente SMTP; por defecto se usa el usuario SMTP |
| `BUZON_SMTP_SSL` | `true` para SSL directo, normalmente en el puerto `465`; si se omite, usa STARTTLS |

## Comportamiento

- El formulario solo habilita el envío cuando `/api/buzon/estado` detecta un proveedor de correo configurado.
- Si existe `BUZON_RESEND_API_KEY`, se utiliza Resend por HTTPS. En caso contrario, se utiliza SMTP si está configurado.
- Los campos de nombre y correo de respuesta son opcionales.
- El correo de respuesta se incluye como `Reply-To`; no se exige identificar al remitente.
- Se limita el tamaño del mensaje y se aplica un intervalo mínimo de 60 segundos entre envíos desde una misma IP.
- Los mensajes no se publican en GitHub ni se guardan en el disco efímero de Render.
