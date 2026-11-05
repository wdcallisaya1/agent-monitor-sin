#!/usr/bin/env python3
"""Monitor de cambios en documentación técnica del SIN (Bolivia).

Descarga cada página de urls.json, detecta los archivos enlazados
(XSD, XML, PDF, ZIP...), calcula su SHA-256 y compara con state.json.
Guarda en snapshots/ una copia en texto de la última versión de cada archivo
(XML/XSD formateados, PDF como texto) y de la página, para poder mostrar
en Telegram qué líneas cambiaron respecto a la versión anterior.
"""
import difflib
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from xml.dom import minidom

import certifi
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = ROOT / "urls.json"
STATE = ROOT / "state.json"
SNAP = ROOT / "snapshots"

UA = "Mozilla/5.0 (compatible; MonitorDocsSIN/1.0; uso personal, 1 consulta diaria)"
FILE_EXT = (".xsd", ".xml", ".pdf", ".zip", ".rar", ".xlsx", ".xls",
            ".doc", ".docx", ".txt", ".json", ".wsdl")
MAX_BYTES = 50 * 1024 * 1024
DELAY = 2  # segundos entre peticiones

# Límites del diff que se manda por Telegram (por archivo)
MAX_DIFF_LINES = 80
MAX_DIFF_CHARS = 3000
MAX_LINE_CHARS = 300


def build_verify():
    """Decide cómo verificar SSL.

    - VERIFY_SSL=0 -> sin verificar (solución rápida).
    - Si existe certs/extra-ca.pem -> se agrega a los certificados de confianza.
    - Si no -> verificación normal.
    """
    if os.getenv("VERIFY_SSL", "1") == "0":
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    extra = ROOT / "certs" / "extra-ca.pem"
    if extra.exists():
        bundle = Path(tempfile.gettempdir()) / "ca-bundle.pem"
        bundle.write_bytes(
            Path(certifi.where()).read_bytes() + b"\n" + extra.read_bytes())
        return str(bundle)
    return True


VERIFY = build_verify()
# Por defecto la primera corrida de una URL (línea base) no manda mensaje.
NOTIFY_BASELINE = os.getenv("NOTIFY_BASELINE", "0") == "1"


# ---------------------------------------------------------------- utilidades

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", text).strip("_")[:80] or "x"


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def plain(text: str) -> str:
    """Quita etiquetas HTML y entidades (para medir largo y para el respaldo)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def get(session, url, **kw):
    r = session.get(url, timeout=60, verify=VERIFY, **kw)
    r.raise_for_status()
    return r


def is_file_link(href: str) -> bool:
    path = unquote(urlparse(href).path).lower()
    return path.endswith(FILE_EXT) or "download" in href.lower()


# ------------------------------------------------------------- página y datos

def parse_page(html_text: str, base_url: str):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    main = (soup.select_one("[itemprop=articleBody]")
            or soup.select_one("#content")
            or soup.select_one("main")
            or soup.body)
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))  # para el hash
    lines = main.get_text("\n", strip=True)                    # para el diff
    links = {}
    for a in main.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if is_file_link(href):
            label = a.get_text(" ", strip=True) or Path(urlparse(href).path).name
            links[href] = label
    return text, lines, links


def download(session, url):
    h = hashlib.sha256()
    chunks, size = [], 0
    with get(session, url, stream=True) as r:
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise RuntimeError(f"Archivo demasiado grande: {url}")
            h.update(chunk)
            chunks.append(chunk)
    return h.hexdigest(), size, b"".join(chunks)


# ------------------------------------------------- texto comparable y diff

def detect_kind(href: str, data: bytes):
    path = unquote(urlparse(href).path).lower()
    if path.endswith(".pdf") or data[:5] == b"%PDF-":
        return "pdf"
    if path.endswith((".xsd", ".xml", ".wsdl")) or data.lstrip()[:5] == b"<?xml":
        return "xml"
    if path.endswith((".txt", ".json")):
        return "text"
    return None


def extract_text(kind, data: bytes):
    """Devuelve el contenido como texto comparable, o None si no se puede."""
    try:
        if kind == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if kind == "xml":
            try:  # formatear para que el diff sea por líneas legibles
                pretty = minidom.parseString(data).toprettyxml(indent="  ")
                text = "\n".join(l for l in pretty.splitlines() if l.strip())
            except Exception:  # noqa: BLE001
                pass
        return text
    except Exception as e:  # noqa: BLE001
        print(f"  (no se pudo extraer texto: {e})")
        return None


def make_diff(old: str, new: str):
    """Diff unificado y recortado; None si no hay diferencias de texto."""
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), "anterior", "nueva", lineterm="", n=1))
    if not diff:
        return None
    out, total = [], 0
    for i, line in enumerate(diff):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "…"
        if i >= MAX_DIFF_LINES or total + len(line) > MAX_DIFF_CHARS:
            out.append(f"… ({len(diff) - i} líneas más; ver historial del repo)")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def snap_path(page_name: str, href: str) -> Path:
    fname = Path(unquote(urlparse(href).path)).name or "archivo"
    key = hashlib.sha1(href.encode()).hexdigest()[:8]
    return SNAP / slug(page_name) / f"{slug(fname)}_{key}.txt"


def read_snap(path: Path):
    return path.read_text(encoding="utf-8") if path.exists() else None


def write_snap(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------- revisión

def check_page(session, name, url, old):
    """Devuelve (nuevo_estado, partes_html_de_cambios, es_baseline)."""
    html_text = get(session, url).text
    text, page_lines, links = parse_page(html_text, url)
    text_hash = sha256_text(text)
    old_files = old.get("files", {}) if old else {}

    files, changes = {}, []
    for href, label in links.items():
        time.sleep(DELAY)
        digest, size, data = download(session, href)
        files[href] = {"text": label, "sha256": digest, "size": size}

        kind = detect_kind(href, data)
        new_text = extract_text(kind, data) if kind else None
        path = snap_path(name, href)
        prev = old_files.get(href)
        modified = prev is not None and prev["sha256"] != digest

        if old is not None and prev is None:
            changes.append(f"🆕 <b>Archivo nuevo:</b> {esc(label)}\n{esc(href)}")
        elif modified:
            head = (f"✏️ <b>Archivo modificado:</b> {esc(label)}\n{esc(href)}\n"
                    f"({prev['size']} → {size} bytes)")
            if new_text is None:
                head += "\n(formato sin vista de diferencias; revisa el archivo)"
                changes.append(head)
            else:
                old_text = read_snap(path)
                if old_text is None:
                    head += "\n(no había versión anterior guardada para comparar)"
                    changes.append(head)
                else:
                    diff = make_diff(old_text, new_text)
                    if diff is None:
                        head += "\n(el texto es igual; cambiaron bytes o formato)"
                        changes.append(head)
                    else:
                        changes.append(f"{head}\n<pre>{esc(diff)}</pre>")

        # guardar la versión actual como referencia para la próxima vez
        if new_text is not None and (prev is None or modified or not path.exists()):
            write_snap(path, new_text)

    for href, info in old_files.items():
        if href not in files:
            changes.append(
                f"🗑️ <b>Archivo eliminado:</b> {esc(info['text'])}\n{esc(href)}")

    page_path = SNAP / slug(name) / "_pagina.txt"
    if old is not None and old.get("page_text_hash") != text_hash:
        head = "📝 <b>El texto de la página cambió</b>"
        old_page = read_snap(page_path)
        diff = make_diff(old_page, page_lines) if old_page is not None else None
        if diff:
            changes.append(f"{head}\n<pre>{esc(diff)}</pre>")
        else:
            changes.append(head + " (revisar lineamientos)")
    if old is None or old.get("page_text_hash") != text_hash or not page_path.exists():
        write_snap(page_path, page_lines)

    return {"page_text_hash": text_hash, "files": files}, changes, old is None


# ------------------------------------------------------------- Telegram

def pack(parts, limit=3800):
    """Agrupa partes completas en mensajes de máximo `limit` caracteres."""
    messages, cur = [], ""
    for p in parts:
        cand = f"{cur}\n\n{p}" if cur else p
        if cur and len(plain(cand)) > limit:
            messages.append(cur)
            cur = p
        else:
            cur = cand
    if cur:
        messages.append(cur)
    return messages


def send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    base = {"chat_id": chat_id, "disable_web_page_preview": "true"}
    r = requests.post(url, data={**base, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    if not r.ok:  # respaldo: texto plano
        r = requests.post(url, data={**base, "text": plain(text)[:4000]},
                          timeout=30)
        if not r.ok:
            print(f"Error Telegram: {r.status_code} {r.text}", file=sys.stderr)


def notify(parts):
    print(plain("\n\n".join(parts)))
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(Telegram no configurado; solo log)")
        return
    for msg in pack(parts):
        send(token, chat_id, msg)


# ------------------------------------------------------------- principal

def main():
    pages = json.loads(CONFIG.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}

    session = requests.Session()
    session.headers["User-Agent"] = UA

    parts, errors = [], []
    for page in pages:
        name, url = page["name"], page["url"]
        print(f"→ {name}: {url}")
        try:
            new, changes, baseline = check_page(session, name, url, state.get(url))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            continue
        state[url] = new
        if baseline:
            n = len(new["files"])
            print(f"  Línea base creada con {n} archivos:")
            for href, info in new["files"].items():
                print(f"   - {info['text']} ({info['size']} bytes) {href}")
            if NOTIFY_BASELINE:
                parts.append(f"✅ Línea base creada para «{esc(name)}» ({n} archivos).")
        elif changes:
            parts.append(f"🔔 <b>Cambios en «{esc(name)}»</b>\n{esc(url)}")
            parts.extend(changes)
        else:
            print("  Sin cambios.")
        time.sleep(DELAY)

    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    if parts:
        repo = os.getenv("GITHUB_REPOSITORY")
        if repo:
            parts.append(
                f"📂 Versiones guardadas: https://github.com/{repo}/commits/main/snapshots")
        notify(parts)
    if errors:
        print("ERRORES:\n" + "\n".join(errors), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()