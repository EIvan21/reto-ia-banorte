# Conectar el CV como servidor MCP

Este agente expone el CV de Edher Iván Díaz Salazar por [MCP](https://modelcontextprotocol.io),
además de por Open Responses. Eso significa que **cualquier agente puede consultarlo como
herramienta**, sin pasar por el chat.

No hace falta clonar nada, ni instalar Python, ni pedir credenciales.

```
https://cv-agent-npnpuwxi2q-uc.a.run.app/mcp/
```

> La barra final importa. Sin ella el servidor redirige, y hay clientes MCP que no siguen
> redirecciones.

Transporte: **streamable HTTP**. Sin autenticación: las seis herramientas expuestas sólo leen
un CV que de todos modos es público, y ninguna llama a un modelo.

---

## Claude Code

```bash
claude mcp add --transport http cv-edher https://cv-agent-npnpuwxi2q-uc.a.run.app/mcp/
```

Para comprobar que quedó:

```bash
claude mcp list
```

Debe aparecer `cv-edher: ... (HTTP) - ✔ Connected`.

Para quitarlo:

```bash
claude mcp remove cv-edher
```

---

## Claude Desktop y Claude en la web

En la configuración de conectores, agregar un conector personalizado con esa URL. No requiere
token ni instalación local.

---

## Cursor, VS Code y otros clientes con `mcp.json`

```json
{
  "mcpServers": {
    "cv-edher": {
      "url": "https://cv-agent-npnpuwxi2q-uc.a.run.app/mcp/",
      "transport": "http"
    }
  }
}
```

Algunos clientes usan `"type"` en vez de `"transport"`, y otros aceptan sólo `url`. Si el tuyo
no conecta a la primera, revisa cuál de las tres formas espera.

---

## Desde código, sin cliente MCP

MCP es JSON-RPC sobre HTTP. Treinta líneas bastan:

```python
import json, urllib.request

URL = "https://cv-agent-npnpuwxi2q-uc.a.run.app/mcp/"

def llamar(metodo, params=None, id_=1):
    cuerpo = json.dumps({
        "jsonrpc": "2.0", "id": id_, "method": metodo, "params": params or {},
    }).encode()
    req = urllib.request.Request(URL, data=cuerpo, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        for linea in r.read().decode().splitlines():
            if linea.startswith("data: "):
                return json.loads(linea[6:])

llamar("initialize", {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "mi-cliente", "version": "1.0"},
}, 1)

print(llamar("tools/list", {}, 2))

print(llamar("tools/call", {
    "name": "buscar_cv",
    "arguments": {"consulta": "modelos de lenguaje", "seccion": ""},
}, 3))
```

---

## Las seis herramientas

| Herramienta | Qué devuelve |
|---|---|
| `buscar_cv` | Búsqueda general. Devuelve hasta 8 entradas relevantes de 30 |
| `obtener_experiencia` | Trayectoria laboral con logros medibles, filtrable por empresa |
| `obtener_proyectos` | Proyectos y contribuciones open source, filtrable por tecnología |
| `obtener_habilidades` | Habilidades con el contexto donde se usaron, más certificaciones |
| `evaluar_vacante` | Recibe una descripción de puesto y devuelve la evidencia relacionada |
| `obtener_contacto` | Canales públicos |

El endpoint de Open Responses expone una séptima, `generar_reporte`, que **no** se publica por
MCP: escribe un archivo en Cloud Storage, y una operación con efecto secundario no va en un
endpoint sin autenticar.

---

## Lo que hace distintas a estas herramientas

Toda respuesta trae el campo **`_citas`** con los ids de las entradas del CV que la respaldan.
No es adorno: es lo que permite verificar de dónde salió cada afirmación.

Y `buscar_cv` devuelve **`terminos_ausentes_del_cv`** cuando la consulta menciona tecnologías
que no existen en el perfil:

```json
{
  "encontrado": false,
  "terminos_ausentes_del_cv": ["kubernetes"],
  "mensaje": "No hay informacion en el CV que responda a esa consulta.
              Dilo explicitamente en lugar de inferir o inventar.",
  "_citas": []
}
```

La instrucción de no inventar **viaja en el dato, no en el prompt**. Un agente que consulte este
servidor recibe la señal de ausencia aunque no sepa nada de las reglas de este proyecto.

---

## Correrlo en local

El mismo servidor funciona por stdio, para clientes MCP locales:

```bash
git clone https://github.com/EIvan21/reto-ia-banorte
cd reto-ia-banorte
python -m venv .venv && pip install -r requirements.txt
python -m app.mcp_server
```

No necesita `ANTHROPIC_API_KEY`: ninguna de las seis herramientas llama a un modelo.

Configuración para un cliente stdio:

```json
{
  "mcpServers": {
    "cv-edher": {
      "command": "<ruta al python del venv>",
      "args": ["-m", "app.mcp_server"],
      "cwd": "<ruta al repositorio>"
    }
  }
}
```
