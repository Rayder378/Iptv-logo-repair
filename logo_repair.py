#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Revisa logos de una playlist M3U y repara los que se confirman rotos."""

import csv
import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
FEEDS_URL = "https://iptv-org.github.io/api/feeds.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
USER_AGENT = "iptv-logo-repair/1.1 (+GitHub Actions)"
TIMEOUT = 20
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PLAYLIST_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 35 * 1024 * 1024


def fetch(url, limit=None):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Solo se permiten URLs HTTP/HTTPS.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        data = response.read() if limit is None else response.read(limit + 1)
        if limit is not None and len(data) > limit:
            raise ValueError("La respuesta supera el límite permitido.")
        return data, response.status, response.headers.get("Content-Type", "").lower()


def get_json(url):
    data, _, _ = fetch(url, MAX_JSON_BYTES)
    parsed = json.loads(data.decode("utf-8-sig"))
    if not isinstance(parsed, list):
        raise ValueError(f"La API no devolvió una lista: {url}")
    return parsed


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("&", " and ")
    value = re.sub(r"\b(hd|uhd|fhd|4k|sd|1080p|720p)\b", " ", value)
    # Sufijos de opciones se ignoran solo para buscar; el nombre visible no cambia.
    value = re.sub(r"\s*[\[(]\s*(?:opc(?:ion)?|op)\.?\s*\d+\s*[\])]\s*$", " ", value)
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
    value = str(value).replace('"', "&quot;")
    pattern = re.compile(r'(?<!\S)' + re.escape(key) + r'="[^\"]*"')
    replacement = f'{key}="{value}"'
    if pattern.search(header):
        return pattern.sub(lambda _: replacement, header, count=1)
    return header.rstrip() + " " + replacement


def remove_attr(header, key):
    """Elimina un atributo M3U, por ejemplo un logo no fiable."""
    pattern = re.compile(r'(?<!\S)' + re.escape(key) + r'="[^\"]*"')
    return re.sub(r'\s+', ' ', pattern.sub('', header)).strip()


def image_signature(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    sample = data.lstrip()[:500].lower()
    if sample.startswith(b"<svg") or (
        sample.startswith(b"<?xml") and b"<svg" in sample
    ):
        return "image/svg+xml"
    return ""


def verify_logo(url):
    """Devuelve 'ok', 'broken' o 'unknown' para evitar reemplazos por fallos temporales.

    'broken' solo se usa cuando hay evidencia clara: HTTP 404/410 o una
    respuesta exitosa cuyo contenido no es una imagen. Errores temporales,
    restricciones y respuestas ambiguas se consideran 'unknown'.
    """
    if not url or urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return "broken", "URL vacía o esquema no permitido"

    last_error = ""
    for attempt in range(2):
        try:
            data, status, content_type = fetch(url, MAX_IMAGE_BYTES)
            if status in (404, 410):
                return "broken", f"HTTP {status}"
            if status == 429 or status >= 500:
                last_error = f"HTTP {status} (posible fallo temporal)"
                continue
            if status in (401, 403):
                return "unknown", f"HTTP {status} (acceso restringido; no se reemplaza automáticamente)"
            if not 200 <= status < 300:
                return "unknown", f"HTTP {status} (respuesta no concluyente)"
            signature = image_signature(data)
            if not signature:
                return "broken", (
                    "El servidor respondió, pero el contenido no parece una imagen "
                    f"({content_type or 'tipo desconocido'})"
                )
            return "ok", f"válido ({signature}; HTTP {status})"
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return "broken", f"HTTP {exc.code}"
            if exc.code in (401, 403):
                return "unknown", f"HTTP {exc.code} (acceso restringido)"
            last_error = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                return "unknown", last_error
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    return "unknown", (
        f"No se pudo confirmar si el logo está roto tras 2 intentos: {last_error}"
    )


def aliases(item):
    names = [item.get("name", "")]
    alt = item.get("alt_names") or []
    if isinstance(alt, str):
        alt = [alt]
    names.extend(alt)
    return [x.strip() for x in names if isinstance(x, str) and x.strip()]


def build_indexes(channels, feeds):
    channel_by_id = {
        str(c.get("id")): c for c in channels if c.get("id")
    }
    channel_name_index = defaultdict(list)
    feed_name_index = defaultdict(list)

    for channel in channels:
        for name in aliases(channel):
            channel_name_index[norm(name)].append(channel)

    for feed in feeds:
        for name in aliases(feed):
            feed_name_index[norm(name)].append(feed)

    return channel_by_id, channel_name_index, feed_name_index


def unique_channel(candidates):
    unique = {
        str(channel.get("id")): channel
        for channel in candidates
        if channel.get("id")
    }
    return next(iter(unique.values())) if len(unique) == 1 else None


def identify_channel(display_name, attributes, channel_by_id,
                      channel_name_index, feed_name_index):
    """Identifica por nombre/alias exacto o por similitud conservadora.

    Los sufijos de opciones y calidad se eliminan únicamente para buscar.
    Nunca se cambia el nombre visible ni otros atributos de la playlist.
    """
    raw_candidates = [display_name, attributes.get("tvg-name", "")]
    checked = set()
    search_names = []
    for candidate_name in raw_candidates:
        key = norm(candidate_name)
        if not key or key in checked:
            continue
        checked.add(key)
        if len(key.replace(" ", "")) < 4 or len(key.split()) < 2:
            continue
        search_names.append((candidate_name.strip(), key))

    for candidate_name, key in search_names:
        channel = unique_channel(channel_name_index.get(key, []))
        if channel:
            return channel, "", f"nombre/alias exacto registrado: {candidate_name}"

        feed_candidates = feed_name_index.get(key, [])
        feed_ids = {
            str(feed.get("channel") or "")
            for feed in feed_candidates
            if str(feed.get("channel") or "") in channel_by_id
        }
        if len(feed_ids) == 1:
            channel_id = next(iter(feed_ids))
            matched_feeds = [
                feed for feed in feed_candidates
                if str(feed.get("channel") or "") == channel_id
            ]
            feed_id = (
                str(matched_feeds[0].get("id") or "")
                if len(matched_feeds) == 1 else ""
            )
            return channel_by_id[channel_id], feed_id, f"feed exacto registrado: {candidate_name}"

    # Tolera errores de escritura menores, pero solo acepta una identidad clara.
    catalog = []
    for key, candidates in channel_name_index.items():
        for channel in candidates:
            channel_id = str(channel.get("id") or "")
            if channel_id:
                catalog.append((key, channel_id, channel, ""))
    for key, feeds in feed_name_index.items():
        for feed in feeds:
            channel_id = str(feed.get("channel") or "")
            if channel_id in channel_by_id:
                catalog.append((key, channel_id, channel_by_id[channel_id],
                                str(feed.get("id") or "")))

    for candidate_name, key in search_names:
        scored = {}
        for catalog_key, channel_id, channel, feed_id in catalog:
            ratio = difflib.SequenceMatcher(None, key, catalog_key).ratio()
            words_ratio = difflib.SequenceMatcher(
                None, " ".join(sorted(key.split())),
                " ".join(sorted(catalog_key.split()))
            ).ratio()
            score = max(ratio, words_ratio)
            if score >= 0.86:
                previous = scored.get(channel_id)
                if previous is None or score > previous[0]:
                    scored[channel_id] = (score, channel, feed_id, catalog_key)

        ranked = sorted(scored.items(), key=lambda pair: pair[1][0], reverse=True)
        if ranked:
            _, best = ranked[0]
            second_score = ranked[1][1][0] if len(ranked) > 1 else 0
            if best[0] >= 0.86 and (best[0] - second_score >= 0.06 or len(ranked) == 1):
                return best[1], best[2], (
                    f"similitud alta ({best[0]:.0%}) con nombre registrado "
                    f"'{best[3]}' a partir de '{candidate_name}'"
                )

    if not display_name.strip():
        return None, "", "nombre vacío"
    normalized = norm(display_name)
    if len(normalized.replace(" ", "")) < 4 or len(normalized.split()) < 2:
        return None, "", "nombre demasiado corto o ambiguo"
    return None, "", "no se encontró una coincidencia exacta ni una similitud suficientemente fiable"


def candidate_logos(channel_id, feed_id, logos):
    candidates = [
        item for item in logos
        if str(item.get("channel") or "") == channel_id
        and item.get("in_use") is True
        and isinstance(item.get("url"), str)
        and item["url"].startswith(("https://", "http://"))
    ]

    exact_feed = [
        item for item in candidates
        if feed_id and str(item.get("feed") or "") == feed_id
    ]
    channel_wide = [item for item in candidates if not item.get("feed")]

    # Prioridad: logo del feed exacto; después, logo general del canal.
    ordered = exact_feed + channel_wide
    seen, result = set(), []
    for item in ordered:
        if item["url"] not in seen:
            seen.add(item["url"])
            result.append(item)
    return result


def choose_input(root):
    """URL tiene prioridad; si no, usa PLAYLIST_FILE o busca un M3U local."""
    m3u_url = os.environ.get("M3U_URL", "").strip()
    if m3u_url:
        print("Modo URL: descargando playlist configurada en M3U_URL...")
        data, _, _ = fetch(m3u_url, MAX_PLAYLIST_BYTES)
        text = data.decode("utf-8-sig", errors="replace")
        return text, "URL configurada en M3U_URL"

    override = os.environ.get("PLAYLIST_FILE", "").strip()
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidates = [candidate] if candidate.is_file() else []
    else:
        candidates = sorted(
            path for path in root.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".m3u", ".m3u8"}
            and path.name.lower() != "playlist_logos_reparados.m3u"
        )

    if not candidates:
        raise FileNotFoundError(
            "No hay archivo M3U/M3U8 en la raíz y M3U_URL no está configurada."
        )

    if len(candidates) > 1:
        print(
            "AVISO: hay varios archivos M3U/M3U8 en la raíz; "
            f"se usará {candidates[0].name} (orden alfabético)."
        )

    path = candidates[0]
    print(f"Modo archivo: leyendo {path.name}...")
    return path.read_text(encoding="utf-8-sig", errors="replace"), path.name


def main():
    root = Path(__file__).resolve().parent
    playlist_text, input_label = choose_input(root)

    lines = playlist_text.splitlines()
    if not any(line.strip().startswith("#EXTM3U") for line in lines):
        raise ValueError("La entrada no parece una playlist M3U válida.")

    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "playlist_logos_reparados.m3u"
    report_path = output_dir / "informe_logos.csv"
    summary_path = output_dir / "informe_logos.json"
    markdown_path = output_dir / "report.md"

    print("Descargando catálogos de iptv-org...")
    channels = get_json(CHANNELS_URL)
    feeds = get_json(FEEDS_URL)
    logos = get_json(LOGOS_URL)
    channel_by_id, channel_name_index, feed_name_index = build_indexes(
        channels, feeds
    )

    out_lines = []
    rows = []
    total = preserved = repaired = unresolved = 0

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#EXTINF:"):
            out_lines.append(line)
            continue

        total += 1
        header, display_name = split_extinf(stripped)
        original_attrs = attrs(header)
        current_logo = original_attrs.get("tvg-logo", "").strip()
        channel, feed_id, reason = identify_channel(
            display_name,
            original_attrs,
            channel_by_id,
            channel_name_index,
            feed_name_index,
        )

        chosen_logo = current_logo
        status = "sin cambios"
        detail = ""
        current_state = "unknown"
        current_result = "No se verificó el logo actual"

        if current_logo:
            current_state, current_result = verify_logo(current_logo)
        else:
            current_state, current_result = "broken", "No tiene tvg-logo"

        # Nunca se elimina un logo solo porque el nombre del canal sea ambiguo.
        # Si el nombre no se identifica, se conserva intacta la URL existente.
        if not channel:
            unresolved += 1
            status = "sin identificar"
            if current_logo and current_state == "ok":
                preserved += 1
                status = "conservado"
                detail = (
                    f"Logo actual funcional y conservado aunque el canal no se "
                    f"identificó: {current_result}"
                )
            elif current_logo and current_state == "unknown":
                detail = (
                    f"No se pudo confirmar el estado del logo; se conserva para "
                    f"evitar cambios por un fallo temporal: {current_result}"
                )
            elif current_logo and current_state == "broken":
                detail = (
                    f"El logo parece roto ({current_result}), pero el canal no se "
                    f"identificó con suficiente seguridad; se conserva la URL "
                    f"original para evitar asignar un logo de otro canal."
                )
            else:
                detail = (
                    "No se identificó el canal con suficiente seguridad. "
                    "No se añadió ningún logo."
                )

        elif current_state == "ok":
            preserved += 1
            status = "conservado"
            detail = f"Logo actual funcional: {current_result}"

        elif current_state == "unknown":
            unresolved += 1
            status = "no verificado"
            detail = (
                f"No se reemplazó el logo porque el resultado no confirma que "
                f"esté roto: {current_result}"
            )

        else:
            # Solo buscar reemplazo si falta el logo o si se confirmó que está roto.
            channel_id = str(channel.get("id") or "")
            replacement = ""
            last_candidate_result = "La API no tiene candidatos in_use para este canal."

            for candidate in candidate_logos(channel_id, feed_id, logos):
                candidate_url = str(candidate.get("url") or "")
                candidate_state, result = verify_logo(candidate_url)
                if candidate_state == "ok":
                    replacement = candidate_url
                    last_candidate_result = f"Reemplazo verificado: {result}"
                    break
                last_candidate_result = f"Candidato rechazado ({candidate_state}): {result}"

            if replacement:
                chosen_logo = replacement
                header = set_attr(header, "tvg-logo", replacement)
                repaired += 1
                status = "reparado" if current_logo else "logo añadido"
                detail = (
                    f"{last_candidate_result}; estado del logo anterior: "
                    f"{current_result}; identificación: {reason}"
                )
            else:
                unresolved += 1
                status = "logo roto sin reemplazo" if current_logo else "no reparado"
                detail = (
                    f"El logo actual está ausente o se confirmó roto ({current_result}), "
                    "pero no se encontró una alternativa verificable. "
                    "Se conservó la URL original si existía. "
                    + last_candidate_result
                )

        out_lines.append("#EXTINF:" + header + "," + display_name)
        rows.append({
            "canal": display_name,
            "tvg_id_original": original_attrs.get("tvg-id", ""),
            "logo_original": current_logo,
            "logo_final": chosen_logo,
            "estado": status,
            "identificacion": reason,
            "detalle": detail,
        })

    output_path.write_text(
        "\n".join(out_lines).rstrip() + "\n", encoding="utf-8"
    )

    fields = [
        "canal", "tvg_id_original", "logo_original", "logo_final",
        "estado", "identificacion", "detalle",
    ]
    with report_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "archivo_entrada": input_label,
        "playlist_salida": "output/playlist_logos_reparados.m3u",
        "canales_analizados": total,
        "logos_conservados": preserved,
        "logos_reparados_o_agregados": repaired,
        "sin_reparar": unresolved,
        "catalogo_canales": len(channels),
        "catalogo_feeds": len(feeds),
        "catalogo_logos": len(logos),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Markdown report with actual logo URLs as image previews.
    def md_escape(value):
        return str(value or "").replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    repaired_rows = [row for row in rows if row["estado"] in ("reparado", "logo añadido")]
    preserved_rows = [row for row in rows if row["estado"] == "conservado"]
    unresolved_rows = [row for row in rows if row["estado"] not in ("reparado", "logo añadido", "conservado")]

    report_lines = [
        "# Informe de reparación de logos IPTV",
        "",
        f"- **Entrada:** {md_escape(input_label)}",
        f"- **Canales analizados:** {total}",
        f"- **Logos reparados o añadidos:** {repaired}",
        f"- **Logos funcionales conservados:** {preserved}",
        f"- **Canales sin reparar:** {unresolved}",
        "",
        "> Solo se reemplaza una URL cuando se confirma que está rota (HTTP 404/410 o contenido que no es imagen). Los errores temporales, timeouts y accesos restringidos no provocan reemplazos automáticos. La verificación de imagen no garantiza por sí sola que el logo represente visualmente el canal correcto.",
        "",
        "[Logos reparados o añadidos](#logos-reparados-o-añadidos) · [Logos funcionales conservados](#logos-funcionales-conservados) · [Canales sin reparar](#canales-sin-reparar)",
        "",
        "## Logos reparados o añadidos",
        "",
    ]

    if repaired_rows:
        for row in repaired_rows:
            report_lines.extend([
                f"### {md_escape(row['canal']) if row['canal'].strip() else '(sin nombre)' }",
                "",
                f"![Logo de {md_escape(row['canal'])}]({row['logo_final']})" if row["logo_final"] else "_No hay URL de imagen para mostrar._",
                "",
                f"- **Estado:** {md_escape(row['estado'])}",
                f"- **Identificación:** {md_escape(row['identificacion'])}",
                f"- **Logo:** {md_escape(row['logo_final'])}",
                f"- **Detalle:** {md_escape(row['detalle'])}",
                "",
            ])
    else:
        report_lines.extend(["No se repararon ni añadieron logos en esta ejecución.", ""])

    report_lines.extend(["## Logos funcionales conservados", ""])
    if preserved_rows:
        for row in preserved_rows:
            report_lines.append(
                f"- **{md_escape(row['canal']) if row['canal'].strip() else '(sin nombre)'}** — "
                f"![Logo de {md_escape(row['canal'])}]({row['logo_final']})"
                if row["logo_final"] else f"- **{md_escape(row['canal'])}** — conservado"
            )
    else:
        report_lines.append("No se conservaron logos funcionales en esta ejecución.")
    report_lines.extend(["", "## Canales sin reparar", ""])
    if unresolved_rows:
        for row in unresolved_rows:
            report_lines.extend([
                f"### {md_escape(row['canal']) if row['canal'].strip() else '(sin nombre)' }",
                "",
                f"- **Motivo:** {md_escape(row['detalle'])}",
                f"- **Logo actual:** {md_escape(row['logo_original']) or 'No tenía logo'}",
                "",
            ])
    else:
        report_lines.extend(["Todos los canales analizados quedaron con un logo verificado o conservado.", ""])

    report_lines.extend([
        "## Archivos generados",
        "",
        "- `playlist_logos_reparados.m3u`: playlist corregida.",
        "- `informe_logos.csv`: detalle tabular por canal.",
        "- `informe_logos.json`: resumen estructurado.",
        "- `report.md`: este informe con vistas previas de los logos.",
        "",
    ])
    markdown_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Informe Markdown generado: {markdown_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
