#!/usr/bin/env python3
"""Monitor de cambios en documentación técnica del SIN (Bolivia).

Descarga cada página de urls.json, detecta los archivos enlazados
(XSD, XML, PDF, ZIP...), calcula su SHA-256 y compara con state.json.
Si algo cambia, avisa por Telegram (si hay credenciales) y lo imprime en el log.
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = ROOT / "urls.json"
STATE = ROOT / "state.json"

UA = "Mozilla/5.0 (compatible; MonitorDocsSIN/1.0; uso personal, 1 consulta diaria)"
FILE_EXT = (".xsd", ".xml", ".pdf", ".zip", ".rar", ".xlsx", ".xls",
            ".doc", ".docx", ".txt", ".json", ".wsdl")
MAX_BYTES = 50 * 1024 * 1024
DELAY = 2  # segundos entre peticiones
VERIFY = os.getenv("VERIFY_SSL", "1") != "0"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get(session, url, **kw):
    r = session.get(url, timeout=60, verify=VERIFY, **kw)
    r.raise_for_status()
    return r


def is_file_link(href: str) -> bool:
    path = unquote(urlparse(href).path).lower()
    return path.endswith(FILE_EXT) or "download" in href.lower()


def parse_page(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    main = (soup.select_one("[itemprop=articleBody]")
            or soup.select_one("#content")
            or soup.select_one("main")
            or soup.body)
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
    links = {}
    for a in main.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if is_file_link(href):
            label = a.get_text(" ", strip=True) or Path(urlparse(href).path).name
            links[href] = label
    return text, links


def hash_file(session, url):
    h = hashlib.sha256()
    size = 0
    with get(session, url, stream=True) as r:
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise RuntimeError(f"Archivo demasiado grande: {url}")
            h.update(chunk)
    return h.hexdigest(), size


def check_page(session, name, url, old):
    """Devuelve (nuevo_estado, lista_de_cambios, es_baseline)."""
    html = get(session, url).text
    text, links = parse_page(html, url)
    text_hash = sha256_text(text)

    files = {}
    for href, label in links.items():
        time.sleep(DELAY)
        digest, size = hash_file(session, href)
        files[href] = {"text": label, "sha256": digest, "size": size}

    new = {"page_text_hash": text_hash, "files": files}
    if old is None:
        return new, [], True

    changes = []
    old_files = old.get("files", {})
    for href, info in files.items():
        if href not in old_files:
            changes.append(f"🆕 Archivo nuevo: {info['text']}\n{href}")
        elif old_files[href]["sha256"] != info["sha256"]:
            changes.append(
                f"✏️ Archivo modificado: {info['text']}\n{href}\n"
                f"({old_files[href]['size']} → {info['size']} bytes)")
    for href, info in old_files.items():
        if href not in files:
            changes.append(f"🗑️ Archivo eliminado: {info['text']}\n{href}")
    if old.get("page_text_hash") != text_hash:
        changes.append("📝 El texto de la página cambió (revisar lineamientos)")
    return new, changes, False


def notify(message: str):
    print(message)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(Telegram no configurado; solo log)")
        return
    for i in range(0, len(message), 4000):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message[i:i + 4000],
                  "disable_web_page_preview": "true"},
            timeout=30)
        if not r.ok:
            print(f"Error Telegram: {r.status_code} {r.text}", file=sys.stderr)


def main():
    pages = json.loads(CONFIG.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}

    session = requests.Session()
    session.headers["User-Agent"] = UA

    reports, errors = [], []
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
            reports.append(f"✅ Línea base creada para «{name}» ({n} archivos).")
        elif changes:
            reports.append(f"🔔 Cambios en «{name}»\n{url}\n\n" + "\n\n".join(changes))
        else:
            print("  Sin cambios.")
        time.sleep(DELAY)

    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    if reports:
        notify("\n\n———\n\n".join(reports))
    if errors:
        print("ERRORES:\n" + "\n".join(errors), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
