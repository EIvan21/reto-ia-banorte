"""Generacion de reportes descargables.

El protocolo Open Responses declara salida de texto, asi que un archivo no puede
viajar dentro de la respuesta. La salida es un ENLACE: se genera un HTML
autocontenido, se sube a Cloud Storage y se devuelve una URL firmada. Un enlace
es texto, asi que funciona sin importar que sepa renderizar el cliente.

HTML y no PDF a proposito. Un PDF exige una libreria pesada (y fuentes) dentro
del contenedor, para un formato que el navegador ya sabe producir: quien quiera
PDF le da Imprimir. Menos superficie que mantener por la misma utilidad.

Si no hay bucket configurado la funcion lo dice en vez de fallar. El agente
sigue sirviendo sin esta capacidad: es un extra, no el producto.
"""

from __future__ import annotations

import datetime
import html
import re
import uuid

from .config import REPORTES_BUCKET, REPORTES_DIAS_VALIDEZ
from .telemetry import registrar, registrar_error

_PLANTILLA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.65 ui-sans-serif, system-ui, -apple-system, sans-serif;
         max-width: 46rem; margin: 3rem auto; padding: 0 1.5rem; }}
  h1 {{ font-size: 1.6rem; line-height: 1.25; margin-bottom: .35rem; }}
  h2 {{ font-size: 1.15rem; margin-top: 2rem; }}
  .meta {{ opacity: .6; font-size: .875rem; margin-bottom: 2.5rem; }}
  .pie {{ margin-top: 3.5rem; padding-top: 1.25rem; font-size: .8125rem; opacity: .6;
          border-top: 1px solid currentColor; }}
  strong {{ font-weight: 650; }}
  li {{ margin: .3rem 0; }}
  @media print {{ body {{ margin: 0; max-width: none; }} }}
</style></head><body>
<h1>{titulo}</h1>
<p class="meta">{fecha} &middot; generado por el agente de CV de {nombre}</p>
{cuerpo}
<p class="pie">Este reporte lo produjo un agente conversacional a partir del CV estructurado de
{nombre}. Cada afirmacion proviene de una entrada del CV; el agente no infiere ni completa datos
faltantes. Contacto: <a href="mailto:{email}">{email}</a></p>
</body></html>
"""


def _markdown_minimo(texto: str) -> str:
    """Convierte el subconjunto de Markdown que el modelo realmente produce.

    Encabezados, negritas, vinetas y parrafos. No es un parser completo a
    proposito: traer una dependencia de Markdown para cuatro construcciones es
    peso muerto, y todo lo que entra se escapa antes de tocar el HTML.
    """
    lineas = texto.replace("\r\n", "\n").split("\n")
    salida: list[str] = []
    en_lista = False

    def _cerrar_lista() -> None:
        nonlocal en_lista
        if en_lista:
            salida.append("</ul>")
            en_lista = False

    for linea in lineas:
        limpia = html.escape(linea.strip())
        limpia = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", limpia)
        limpia = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", limpia)

        if not limpia:
            _cerrar_lista()
            continue
        if limpia.startswith("### "):
            _cerrar_lista()
            salida.append(f"<h3>{limpia[4:]}</h3>")
        elif limpia.startswith("## "):
            _cerrar_lista()
            salida.append(f"<h2>{limpia[3:]}</h2>")
        elif limpia.startswith("# "):
            _cerrar_lista()
            salida.append(f"<h2>{limpia[2:]}</h2>")
        elif limpia.startswith(("- ", "* ", "&middot; ")):
            if not en_lista:
                salida.append("<ul>")
                en_lista = True
            salida.append(f"<li>{limpia[2:].lstrip()}</li>")
        else:
            _cerrar_lista()
            salida.append(f"<p>{limpia}</p>")

    _cerrar_lista()
    return "\n".join(salida)


def _credenciales_de_firma() -> dict:
    """Argumentos extra para firmar una URL desde Cloud Run.

    Firmar una URL normalmente exige la llave privada de una cuenta de servicio.
    En Cloud Run no hay llave: las credenciales del entorno solo traen un token,
    y la libreria falla con "you need a private key to sign credentials".

    La salida es firmar a traves de la API de IAM, pasando el correo de la cuenta
    y un token de acceso. La cuenta necesita poder firmar en su propio nombre
    (roles/iam.serviceAccountTokenCreator sobre si misma), que es lo que otorga
    scripts/deploy.sh.

    En local, con credenciales de usuario que si pueden firmar, no hace falta
    nada de esto y se devuelve vacio.
    """
    try:
        import google.auth
        import google.auth.transport.requests

        credenciales, _ = google.auth.default()
        correo = getattr(credenciales, "service_account_email", None)
        if not correo or correo == "default":
            return {}

        credenciales.refresh(google.auth.transport.requests.Request())
        return {"service_account_email": correo, "access_token": credenciales.token}
    except Exception:  # noqa: BLE001 - sin esto se intenta la firma normal
        return {}


def generar(titulo: str, contenido: str, nombre: str, email: str) -> dict:
    """Genera el reporte y lo sube. Devuelve {'url': ...} o {'error': ...}."""
    if not REPORTES_BUCKET:
        return {
            "disponible": False,
            "mensaje": (
                "La generacion de reportes no esta configurada en este despliegue. "
                "Entrega el analisis directamente en el chat."
            ),
        }

    documento = _PLANTILLA.format(
        titulo=html.escape(titulo.strip() or "Reporte"),
        fecha=datetime.date.today().strftime("%d/%m/%Y"),
        nombre=html.escape(nombre),
        email=html.escape(email),
        cuerpo=_markdown_minimo(contenido),
    )

    # Nombre imposible de adivinar: el reporte puede contener el analisis de una
    # vacante concreta y no tiene por que ser enumerable.
    objeto = f"reportes/{datetime.date.today():%Y/%m}/{uuid.uuid4().hex}.html"

    try:
        from google.cloud import storage

        cliente = storage.Client()
        blob = cliente.bucket(REPORTES_BUCKET).blob(objeto)
        blob.upload_from_string(documento, content_type="text/html; charset=utf-8")

        url = blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(days=REPORTES_DIAS_VALIDEZ),
            method="GET",
            **_credenciales_de_firma(),
        )
        registrar("reporte_generado", objeto=objeto, bytes=len(documento))
        return {
            "disponible": True,
            "url": url,
            "vigencia_dias": REPORTES_DIAS_VALIDEZ,
            "mensaje": (
                f"Reporte listo. El enlace caduca en {REPORTES_DIAS_VALIDEZ} dias. "
                "Compartelo tal cual: se abre en el navegador y desde ahi se puede imprimir a PDF."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        registrar_error("fallo_generar_reporte", detalle=str(exc))
        return {
            "disponible": False,
            "mensaje": (
                "No se pudo generar el archivo en este momento. Entrega el analisis "
                "directamente en el chat, que es lo que importa."
            ),
        }
