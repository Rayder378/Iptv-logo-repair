#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Revisa logos de una playlist M3U y reemplaza solo los que se confirman rotos."""

import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
FEEDS_URL = "https://iptv-org.github.io/api/feeds.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
USER_AGENT = "iptv-logo-repair/1.0 (+GitHub Actions)"
TIMEOUT = 20
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"<svg", "image/svg+xml"),
    (b"<?xml", "image/svg+xml"),
)


def fetch(url, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        data = response.read() if limit is None else response.read(limit + 1)
        if limit is not None and len(data) > limit:
            raise ValueError("Respuesta supera el límite permitido")
        return data, response.status, response.headers.get("Content-Type", "").lower()


def get_json(url):
    data, status, _ = fetch(url, 30 * 1024 * 1024)
    parsed = json.loads(data.decode("utf-8-sig"))
    if not isinstance(parsed, list):
        raise ValueError(f"API no devolvió una lista: {url}")
    return parsed


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("&", " and ")
    value = re.sub(r"\b(hd|uhd|fhd|4k|sd|1080p|720p)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def split_extinf(line):
    content = line[len("#EXTINF:"):]
    quoted = False
    for i, ch in enumerate(content):
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            return content[:i], content[i + 1:].strip()
    return content, ""


def attrs(header):
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(header)}


def set_attr(header, key, value):
    # Attribute values are quoted; escape embedded quotes defensively.
    value = str(value).replace('"', "&quot;")
    pat = re.compile(r'(?<!\S)' + re.escape(key) + r'="[^"]*"')
    repl = f'{key}="{value}"'
    if pat.search(header):
        return pat.sub(lambda _: repl, header, count=1)
    return header.rstrip() + " " + repl


def image_signature(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    sample = data.lstrip()[:300].lower()
    if sample.startswith(b"<svg") or (sample.startswith(b"<?xml") and b"<svg" in sample):
        return "image/svg+xml"
    return ""


def verify_logo(url):
    """Descarga el cuerpo (con límite) y valida que parezca una imagen."""
    if not url or urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return False, "URL vacía o esquema no permitido"
    try:
        data, status, content_type = fetch(url, MAX_IMAGE_BYTES)
        if not 200 <= status < 300:
            return False, f"HTTP {status}"
        signature = image_signature(data)
        if not signature:
            return False, f"El contenido no parece una imagen ({content_type or 'tipo desconocido'})"
        # HTML de error nunca pasa la firma de imagen. El Content-Type puede variar,
        # así que la firma del contenido es la comprobación principal.
        return True, f"válido ({signature})"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def aliases(item):
    names = [item.get("name", "")]
    alt = item.get("alt_names") or []
    if isinstance(alt, str):
        alt = [alt]
    names.extend(alt)
    return [x.strip() for x in names if isinstance(x, str) and x.strip()]


def build_indexes(channels, feeds):
    channel_by_id = {str(c.get("id")): c for c in channels if c.get("id")}
    channel_name_index = defaultdict(list)
    feed_name_index = defaultdict(list)
    for c in channels:
        for name in aliases(c):
            channel_name_index[norm(name)].append(c)
    for f in feeds:
        for name in aliases(f):
            feed_name_index[norm(name)].append(f)
    return channel_by_id, channel_name_index, feed_name_index


def unique_channel(candidates):
    unique = {str(c.get("id")): c for c in candidates if c.get("id")}
    return next(iter(unique.values())) if len(unique) == 1 else None


def identify_channel(display_name, attributes, channel_by_id, channel_name_index, feed_name_index):
    # Strongest signal: a valid tvg-id that exactly exists in the API.
    tvg_id = (attributes.get("tvg-id") or "").strip()
    if tvg_id in channel_by_id:
        return channel_by_id[tvg_id], "", "tvg-id exacto"

    key = norm(display_name)
    channel = unique_channel(channel_name_index.get(key, []))
    if channel:
        return channel, "", "nombre exacto"

    feed_candidates = feed_name_index.get(key, [])
    feed_ids = {str(f.get("channel") or "") for f in feed_candidates if f.get("channel") in channel_by_id}
    if len(feed_ids) == 1:
        cid = next(iter(feed_ids))
        matching_feeds = [f for f in feed_candidates if str(f.get("channel") or "") == cid]
        feed_id = str(matching_feeds[0].get("id") or "") if len(matching_feeds) == 1 else ""
        return channel_by_id[cid], feed_id, "nombre exacto de feed"
    return None, "", "sin coincidencia inequívoca"


def candidate_logos(channel_id, feed_id, logos):
    candidates = [
        x for x in logos
        if str(x.get("channel") or "") == channel_id
        and x.get("in_use") is True
        and isinstance(x.get("url"), str)
        and x["url"].startswith(("https://", "http://"))
    ]
    # Prefer exact regional feed, then channel-wide logos with no feed.
    exact_feed = [x for x in candidates if feed_id and str(x.get("feed") or "") == feed_id]
    channel_wide = [x for x in candidates if not x.get("feed")]
    ordered = exact_feed + channel_wide
    # Preserve API order and remove duplicate URLs.
    seen, result = set(), []
    for item in ordered:
        url = item["url"]
        if url not in seen:
            seen.add(url)
            result.append(item)
    return result


def main():
    root = Path(__file__).resolve().parent
    input_path = Path(os.environ.get("PLAYLIST_FILE", "playlist.m3u"))
    if not input_path.is_absolute():
        input_path = root / input_path
    if not input_path.exists():
        print(f"ERROR: no existe el archivo M3U: {input_path}", file=sys.stderr)
        return 2

    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "playlist_logos_reparados.m3u"
    report_path = output_dir / "informe_logos.csv"
    summary_path = output_dir / "informe_logos.json"

    print(f"Leyendo playlist: {input_path.name}")
    lines = input_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    if not any(line.strip().startswith("#EXTM3U") for line in lines):
        print("ERROR: el archivo no parece una playlist M3U válida.", file=sys.stderr)
        return 2

    print("Descargando catálogos de iptv-org...")
    channels = get_json(CHANNELS_URL)
    feeds = get_json(FEEDS_URL)
    logos = get_json(LOGOS_URL)
    channel_by_id, channel_name_index, feed_name_index = build_indexes(channels, feeds)

    out_lines = []
    rows = []
    total = preserved = repaired = unresolved = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped.startswith("#EXTINF:"):
            out_lines.append(line)
            i += 1
            continue

        total += 1
        header, display_name = split_extinf(stripped)
        attributes = attrs(header)
        current_logo = attributes.get("tvg-logo", "").strip()
        channel, feed_id, reason = identify_channel(
            display_name, attributes, channel_by_id, channel_name_index, feed_name_index
        )
        status = "sin cambios"
        detail = ""
        chosen_logo = current_logo

        if current_logo:
            ok, check = verify_logo(current_logo)
        else:
            ok, check = False, "No tiene tvg-logo"

        if ok:
            preserved += 1
            status = "conservado"
            detail = f"Logo actual funcional: {check}"
        elif channel:
            cid = str(channel.get("id") or "")
            candidates = candidate_logos(cid, feed_id, logos)
            replacement = ""
            replacement_note = ""
            for candidate in candidates:
                candidate_url = str(candidate.get("url") or "")
                valid, result = verify_logo(candidate_url)
                if valid:
                    replacement = candidate_url
                    replacement_note = f"Reemplazo verificado ({result}); identificación: {reason}"
                    break
                replacement_note = f"Candidato rechazado: {result}"

            if replacement:
                chosen_logo = replacement
                header = set_attr(header, "tvg-logo", replacement)
                repaired += 1
                status = "reparado" if current_logo else "logo añadido"
                detail = replacement_note
            else:
                unresolved += 1
                status = "no reparado"
                detail = "No se encontró logo API verificable. " + replacement_note
        else:
            unresolved += 1
            status = "no reparado"
            detail = f"{check}; canal sin coincidencia inequívoca, no se adivinó el logo."

        out_lines.append("#EXTINF:" + header + "," + display_name)
        rows.append({
            "canal": display_name,
            "tvg_id_original": attributes.get("tvg-id", ""),
            "logo_original": current_logo,
            "logo_final": chosen_logo,
            "estado": status,
            "identificacion": reason,
            "detalle": detail,
        })
        i += 1

    output_path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    fields = ["canal", "tvg_id_original", "logo_original", "logo_final", "estado", "identificacion", "detalle"]
    with report_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "archivo_entrada": str(input_path.relative_to(root)) if input_path.is_relative_to(root) else input_path.name,
        "playlist_salida": "output/playlist_logos_reparados.m3u",
        "canales_analizados": total,
        "logos_conservados": preserved,
        "logos_reparados_o_agregados": repaired,
        "sin_reparar": unresolved,
        "catalogo_canales": len(channels),
        "catalogo_feeds": len(feeds),
        "catalogo_logos": len(logos),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
