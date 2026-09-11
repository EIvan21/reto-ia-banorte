"""Servidor MCP que expone el CV como herramientas para cualquier agente.

El mismo CV y las mismas funciones que usa el endpoint de Open Responses, servidas
por un segundo protocolo. Es exactamente el argumento de por que las herramientas
viven en `tools.py` y no dentro del loop del agente: la logica de negocio no sabe
nada del transporte, asi que agregar un protocolo es un archivo, no un rediseno.

Dos formas de correrlo:

  1. Montado en la misma app de FastAPI, en /mcp (transporte streamable HTTP).
     Un solo despliegue en Cloud Run atiende los dos protocolos.

  2. Como proceso stdio local, para conectarlo a Claude Desktop o Claude Code:

         python -m app.mcp_server

     En la configuracion del cliente MCP:
         {"command": "python", "args": ["-m", "app.mcp_server"], "cwd": "<ruta al repo>"}

Nota de version: el SDK de Python de MCP 2.x renombro FastMCP a MCPServer. Este
codigo usa la API 2.x; con `mcp<2` no corre.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import tools
from .config import AGENT_VERSION, PUBLIC_BASE_URL, load_cv

INSTRUCCIONES = """\
Estas herramientas consultan el CV estructurado de Edher Ivan Diaz Salazar,
ingeniero mexicano que construye agentes de IA sobre Google Cloud, con base en
Analytics Engineering (Looker, LookML, BigQuery).

Toda respuesta sobre su perfil debe apoyarse en lo que devuelvan estas herramientas.
Cada resultado incluye "_citas" con los ids de las entradas del CV que lo respaldan.
Si algo no aparece en los resultados, no esta en el CV: dilo en vez de inferirlo.
La herramienta buscar_cv ademas devuelve "terminos_ausentes_del_cv" cuando la
consulta menciona tecnologias que no existen en el perfil.
"""


def crear_servidor() -> MCPServer:
    cv = load_cv()

    servidor = MCPServer(
        name="cv-edher-diaz",
        title=f"CV de {cv['perfil']['nombre']}",
        description=(
            f"Consulta el CV de {cv['perfil']['nombre']}, {cv['perfil']['titular']} "
            f"especializado en {cv['perfil']['especialidad']}."
        ),
        instructions=INSTRUCCIONES,
        version=AGENT_VERSION,
        website_url=cv["contacto"]["sitio_web"],
    )

    # Las funciones se envuelven en vez de decorar las originales para que
    # tools.py siga siendo codigo puro, sin dependencia de MCP.

    @servidor.tool(
        name="buscar_cv",
        description=(
            "Busca en el CV por palabra clave. Herramienta de proposito general: usala "
            "cuando la pregunta no encaje en una mas especifica, o para confirmar si un "
            "tema existe en el CV antes de responder."
        ),
    )
    def buscar_cv(
        consulta: Annotated[
            str, Field(description='Terminos a buscar, por ejemplo "modelos de lenguaje" o "BigQuery".')
        ],
        seccion: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Acota la busqueda a una seccion. Cadena vacia para buscar en todo el CV. "
                    "Valores validos: perfil, experiencia, proyectos, habilidades, educacion, "
                    "certificaciones, open_source, preferencias_rol, intereses."
                ),
            ),
        ] = "",
    ) -> dict:
        """Busca informacion en el CV.

        Args:
            consulta: Terminos a buscar, por ejemplo "modelos de lenguaje" o "BigQuery".
            seccion: Acota la busqueda a una seccion. Vacio para buscar en todo el CV.
                Valores: perfil, experiencia, proyectos, habilidades, educacion,
                certificaciones, open_source, preferencias_rol, intereses.
        """
        return tools.buscar_cv(consulta, seccion)

    @servidor.tool(
        name="obtener_experiencia",
        description="Trayectoria laboral completa con logros medibles por puesto.",
    )
    def obtener_experiencia(
        empresa: Annotated[
            str,
            Field(default="", description="Filtra por empresa: GlobalLogic, GTEC o Infosys. Vacio para todas."),
        ] = "",
    ) -> dict:
        """Devuelve la experiencia laboral.

        Args:
            empresa: Filtra por empresa (GlobalLogic, GTEC, Infosys). Vacio para todas.
        """
        return tools.obtener_experiencia(empresa)

    @servidor.tool(
        name="obtener_proyectos",
        description="Proyectos y contribuciones open source, incluida la organizacion looker-open-source de Google.",
    )
    def obtener_proyectos(
        tecnologia: Annotated[
            str,
            Field(default="", description='Filtra por tecnologia, por ejemplo "LookML". Vacio para todos.'),
        ] = "",
    ) -> dict:
        """Devuelve los proyectos.

        Args:
            tecnologia: Filtra por tecnologia, por ejemplo "LookML". Vacio para todos.
        """
        return tools.obtener_proyectos(tecnologia)

    @servidor.tool(
        name="obtener_habilidades",
        description="Habilidades tecnicas con el contexto real donde se usaron, mas las certificaciones.",
    )
    def obtener_habilidades(
        categoria: Annotated[
            str, Field(default="", description="Filtra por categoria o tecnologia. Vacio para todas.")
        ] = "",
    ) -> dict:
        """Devuelve las habilidades.

        Args:
            categoria: Filtra por categoria o tecnologia. Vacio para todas.
        """
        return tools.obtener_habilidades(categoria)

    @servidor.tool(
        name="evaluar_vacante",
        description=(
            "Contrasta el perfil completo contra la descripcion de una vacante. Devuelve "
            "la evidencia relacionada y exige nombrar tanto lo que se cumple como lo que no."
        ),
    )
    def evaluar_vacante(
        descripcion_vacante: Annotated[
            str, Field(description="Texto de la vacante o descripcion del rol a evaluar.")
        ],
    ) -> dict:
        """Evalua el encaje contra un puesto.

        Args:
            descripcion_vacante: Texto de la vacante o descripcion del rol.
        """
        return tools.evaluar_vacante(descripcion_vacante)

    @servidor.tool(
        name="obtener_contacto",
        description="Canales de contacto publicos: email, LinkedIn, GitHub y sitio web.",
    )
    def obtener_contacto() -> dict:
        """Devuelve los canales de contacto publicos. No incluye telefono."""
        return tools.obtener_contacto()

    return servidor


def _hosts_permitidos() -> list[str]:
    """Hosts que el transporte HTTP acepta.

    La proteccion contra DNS rebinding viene activada por defecto en el SDK y
    rechaza cabeceras Host inesperadas. En Cloud Run el Host es el dominio
    *.run.app que asigna el servicio, asi que se deriva de PUBLIC_BASE_URL en vez
    de abrir el comodin.
    """
    hosts = ["localhost", "localhost:8080", "127.0.0.1", "127.0.0.1:8080"]
    host_publico = urlparse(PUBLIC_BASE_URL).netloc
    if host_publico:
        hosts.append(host_publico)
    return hosts


def app_asgi():
    """App Starlette para montar en FastAPI. Sirve MCP en /mcp del prefijo de montaje."""
    return crear_servidor().streamable_http_app(
        streamable_http_path="/",
        # Sin estado: Cloud Run puede mandar cada peticion a una instancia
        # distinta, asi que no puede haber sesion pegada a un proceso.
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_hosts_permitidos(),
            allowed_origins=["*"],
        ),
    )


if __name__ == "__main__":
    # Transporte stdio, para clientes MCP locales como Claude Desktop o Claude Code.
    crear_servidor().run(transport="stdio")
