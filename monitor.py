#!/usr/bin/env python3
"""
Monitor de cambios en la documentación técnica de Impuestos Nacionales (Bolivia).

Detecta:
  - Cambios en el texto de la página.
  - Enlaces nuevos o eliminados a archivos (XSD, XML, ZIP, PDF, etc.).
  - Cambios en el contenido de los archivos enlazados (aunque conserven el nombre).

Variables de entorno opcionales:
  ARCHIVO_ESTADO   Ruta del JSON de estado (por defecto: estado.json)
  EXIT_ON_CHANGE   Si vale "1", el script termina con código 1 cuando hay cambios
                   (útil para que GitHub te envíe un correo por job fallido).
"""

import difflib
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
PAGINAS = [
    {
        "nombre": "Factura de compra y venta (XML/XSD)",
        "url": "https://siatanexo.impuestos.gob.bo/index.php/documentos-fiscales-digitales/archivos-xml-xsd-de-facturas-electronicas/factura-de-compra-y-venta",
    },
    # Agrega aquí más páginas:
    # {"nombre": "Otra página", "url": "https://siatanexo.impuestos.gob.bo/..."},
]

ARCHIVO_ESTADO = os.getenv("ARCHIVO_ESTADO", "estado.json")
EXIT_ON_CHANGE = os.getenv("EXIT_ON_CHANGE", "0") == "1"

EXTENSIONES = (".xsd", ".xml", ".zip", ".pdf", ".xlsx", ".xls", ".doc", ".docx")
MAX_BYTES_ARCHIVO = 20 * 1024 * 1024  # 20 MB
MAX_FALLOS_CONSECUTIVOS = 3  # recién entonces el job falla por errores de red
REINTENTOS = 3
TIMEOUT = 30

# Líneas de texto que cambian solas (contadores, fechas dinámicas) y generarían
# falsos positivos. Ajusta según lo que veas en los diffs.
IGNORAR_LINEAS = [
    re.compile(r"^(visitas|hits|accesos)\b", re.I),
    re.compile(r"^\d+\s+(visitas|hits)\b", re.I),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; monitor-sin/1.0)"}


# ----------------------------------------------------------------------------
# Red
# ----------------------------------------------------------------------------
def obtener(url, **kwargs):
    """GET con reintentos. Si el certificado SSL no se puede verificar (el
    servidor no envía el certificado intermedio), reintenta sin verificación."""
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            try:
                r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
            except requests.exceptions.SSLError:
                print(f"  ⚠ SSL no verificable, reintentando sin verificación: {url}")
                r = requests.get(
                    url, headers=HEADERS, timeout=TIMEOUT, verify=False, **kwargs
                )
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            ultimo_error = e
            if intento < REINTENTOS:
                time.sleep(2 * intento)
    raise ultimo_error


def hash_archivo(url):
    """SHA-256 del archivo enlazado (descarga en streaming)."""
    r = obtener(url, stream=True)
    h = hashlib.sha256()
    total = 0
    for chunk in r.iter_content(65536):
        total += len(chunk)
        if total > MAX_BYTES_ARCHIVO:
            return "demasiado-grande"
        h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# Extracción de contenido
# ----------------------------------------------------------------------------
def extraer(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        t.decompose()

    contenedor = (
        soup.select_one("[itemprop=articleBody]")
        or soup.select_one("#content")
        or soup.select_one("main")
        or soup.body
        or soup
    )

    lineas = []
    for crudo in contenedor.get_text("\n").splitlines():
        linea = re.sub(r"\s+", " ", crudo).strip()
        if not linea:
            continue
        if any(p.search(linea) for p in IGNORAR_LINEAS):
            continue
        lineas.append(linea)
    texto = "\n".join(lineas)

    enlaces = {}
    for a in contenedor.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if urlparse(href).path.lower().endswith(EXTENSIONES):
            enlaces[href] = a.get_text(" ", strip=True)

    return texto, enlaces


def sha(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Estado
# ----------------------------------------------------------------------------
def cargar_estado():
    if os.path.exists(ARCHIVO_ESTADO):
        try:
            with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
                estado = json.load(f)
                estado.setdefault("paginas", {})
                return estado
        except (json.JSONDecodeError, OSError):
            print("⚠ No se pudo leer el estado previo; se empieza de cero.")
    return {"paginas": {}}


def guardar_estado(estado):
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2, sort_keys=True)


# ----------------------------------------------------------------------------
# Comparación
# ----------------------------------------------------------------------------
def comparar(prev, texto, enlaces_nuevos):
    """Devuelve una lista de descripciones de cambios (vacía si no hubo)."""
    cambios = []

    if prev.get("hash") != sha(texto):
        diff = list(
            difflib.unified_diff(
                prev.get("texto", "").splitlines(),
                texto.splitlines(),
                fromfile="antes",
                tofile="ahora",
                lineterm="",
                n=1,
            )
        )
        recorte = "\n".join(diff[:40])
        if len(diff) > 40:
            recorte += f"\n... ({len(diff) - 40} líneas más)"
        cambios.append("Cambió el texto de la página:\n" + recorte)

    previos = prev.get("enlaces", {})
    for url in sorted(set(enlaces_nuevos) - set(previos)):
        cambios.append(f"Archivo NUEVO: {enlaces_nuevos[url]['texto']} → {url}")
    for url in sorted(set(previos) - set(enlaces_nuevos)):
        cambios.append(f"Archivo ELIMINADO: {previos[url].get('texto', '')} → {url}")
    for url in sorted(set(previos) & set(enlaces_nuevos)):
        a = previos[url].get("sha256")
        b = enlaces_nuevos[url]["sha256"]
        if a and b and "error" not in (a, b) and a != b:
            cambios.append(
                f"Archivo MODIFICADO: {enlaces_nuevos[url]['texto'] or url} → {url}"
            )

    return cambios


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    estado = cargar_estado()
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")

    todos_cambios = []  # (nombre, url, [descripciones])
    errores = []  # (nombre, url, error, fallos_consecutivos)

    for pagina in PAGINAS:
        nombre, url = pagina["nombre"], pagina["url"]
        print(f"Revisando: {nombre}")
        prev = estado["paginas"].setdefault(nombre, {})

        try:
            r = obtener(url)
            texto, enlaces = extraer(r.text, url)

            enlaces_nuevos = {}
            previos = prev.get("enlaces", {})
            for u, t in enlaces.items():
                try:
                    digest = hash_archivo(u)
                except Exception as e:
                    print(f"  ⚠ No se pudo descargar {u}: {e}")
                    digest = previos.get(u, {}).get("sha256", "error")
                enlaces_nuevos[u] = {"texto": t, "sha256": digest}

            if "hash" not in prev:
                print(
                    f"  Primera ejecución: línea base guardada "
                    f"({len(enlaces_nuevos)} archivos enlazados)."
                )
            else:
                cambios = comparar(prev, texto, enlaces_nuevos)
                if cambios:
                    todos_cambios.append((nombre, url, cambios))
                    print(f"  ✔ {len(cambios)} cambio(s) detectado(s)")
                else:
                    print("  Sin cambios.")

            prev.update(
                {
                    "url": url,
                    "hash": sha(texto),
                    "texto": texto,
                    "enlaces": enlaces_nuevos,
                    "revisado": ahora,
                    "fallos": 0,
                }
            )

        except Exception as e:
            prev["fallos"] = prev.get("fallos", 0) + 1
            errores.append((nombre, url, e, prev["fallos"]))
            print(f"  ✖ Error ({prev['fallos']} seguido(s)): {e}")

    guardar_estado(estado)

    # --- Reporte -------------------------------------------------------------
    if todos_cambios:
        print("\nCAMBIOS DETECTADOS:")
        lineas_md = [f"# Cambios detectados ({ahora})", ""]
        for nombre, url, cambios in todos_cambios:
            print(f"\n■ {nombre}\n  {url}")
            lineas_md += [f"## {nombre}", url, ""]
            for c in cambios:
                print("  - " + c.replace("\n", "\n    "))
                lineas_md += ["```", c, "```", ""]
        with open("cambios.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lineas_md))

        resumen = os.getenv("GITHUB_STEP_SUMMARY")
        if resumen:
            with open(resumen, "a", encoding="utf-8") as f:
                f.write("\n".join(lineas_md) + "\n")

    salida = os.getenv("GITHUB_OUTPUT")
    if salida:
        with open(salida, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if todos_cambios else 'false'}\n")

    if errores:
        print("\nERRORES:")
        for nombre, url, e, fallos in errores:
            print(f"{nombre}: {e}\n→ {url} (fallos seguidos: {fallos})")

    # --- Código de salida ------------------------------------------------------
    if any(fallos >= MAX_FALLOS_CONSECUTIVOS for *_, fallos in errores):
        print(f"\nFalló {MAX_FALLOS_CONSECUTIVOS} veces seguidas: se marca el job como fallido.")
        sys.exit(1)
    if EXIT_ON_CHANGE and todos_cambios:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()