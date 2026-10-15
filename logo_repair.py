#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Revisa logos de una playlist M3U y repara los que se confirman rotos."""

import csv
from datetime import datetime, timezone, timedelta
import concurrent.futures
from functools import lru_cache
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
JARUBA_URL = "https://jaruba.github.io/channel-logos/logo_paths.json"
YASSER_INDEX_URL = "https://raw.githubusercontent.com/yasserstudio/tv-logos/main/index.json"
JARUBA_BASE = "https://jaruba.github.io/channel-logos/export/transparent-color"
YASSER_BASE = "https://raw.githubusercontent.com/yasserstudio/tv-logos/main"
USER_AGENT = "iptv-logo-repair/1.1 (+GitHub Actions)"
TIMEOUT = 10
VERIFY_WORKERS = max(4, min(16, int(os.environ.get("LOGO_VERIFY_WORKERS", "12"))))
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


# Cache persistente: solo se confían resultados positivos recientes. Los errores
# se vuelven a comprobar para no confundir fallos temporales con logos rotos.
PERSISTENT_LOGO_DIR = Path(__file__).resolve().parent / "url logos"
PERSISTENT_CACHE_PATH = PERSISTENT_LOGO_DIR / "catalogo_urls.json"
PERSISTENT_CHANNELS_PATH = PERSISTENT_LOGO_DIR / "canales.json"
CACHE_MAX_AGE = timedelta(days=7)
_persistent_cache = {}

def load_persistent_cache():
    global _persistent_cache
    PERSISTENT_LOGO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(PERSISTENT_CACHE_PATH.read_text(encoding="utf-8"))
        _persistent_cache = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _persistent_cache = {}

def persistent_verify_logo(url):
    if not url:
        return verify_logo(url)
    now = datetime.now(timezone.utc)
    item = _persistent_cache.get(url)
    if isinstance(item, dict) and item.get("ok") is True:
        try:
            checked = datetime.fromisoformat(item.get("checked_at", "").replace("Z", "+00:00"))
            if now - checked < CACHE_MAX_AGE:
                return True, item.get("result", "válido (caché persistente)") + " [caché persistente]"
        except (TypeError, ValueError):
            pass
    ok, result = verify_logo(url)
    _persistent_cache[url] = {
        "ok": bool(ok), "result": result, "checked_at": now.isoformat(),
        "next_check_after_days": 7,
    }
    return ok, result

def save_persistent_cache(channel_rows=None):
    PERSISTENT_LOGO_DIR.mkdir(parents=True, exist_ok=True)
    PERSISTENT_CACHE_PATH.write_text(
        json.dumps(_persistent_cache, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if channel_rows is not None:
        PERSISTENT_CHANNELS_PATH.write_text(
            json.dumps(channel_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )

@lru_cache(maxsize=50000)
def verify_logo(url):
    """Comprueba el contenido real, no solo que el servidor responda."""
    if not url or urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return False, "URL vacía o esquema no permitido"
    try:
        data, status, content_type = fetch(url, MAX_IMAGE_BYTES)
        if not 200 <= status < 300:
            return False, f"HTTP {status}"
        signature = image_signature(data)
        if not signature:
            return False, (
                "El contenido no parece una imagen "
                f"({content_type or 'tipo desconocido'})"
            )
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


COUNTRY_GROUPS = {
    "colombia": "CO", "colombian": "CO", "col": "CO",
    "peru": "PE", "perú": "PE", "peruvian": "PE",
    "mexico": "MX", "méxico": "MX", "mexican": "MX",
    "argentina": "AR", "chile": "CL", "ecuador": "EC",
    "venezuela": "VE", "bolivia": "BO", "uruguay": "UY",
    "paraguay": "PY", "brasil": "BR", "brazil": "BR",
    "usa": "US", "united states": "US", "estados unidos": "US",
    "spain": "ES", "españa": "ES", "espana": "ES",
    "el salvador": "SV", "guatemala": "GT", "honduras": "HN",
    "nicaragua": "NI", "costa rica": "CR", "panama": "PA",
    "panamá": "PA", "dominicana": "DO", "republica dominicana": "DO",
    "puerto rico": "PR",
}
CATEGORY_GROUPS = {
    "documentales": "documentary", "documental": "documentary",
    "documentary": "documentary", "infantiles": "kids",
    "infantil": "kids", "kids": "kids", "niños": "kids",
    "ninos": "kids", "deportes": "sports", "deporte": "sports",
    "sports": "sports", "noticias": "news", "noticias": "news",
    "news": "news", "peliculas": "movies", "películas": "movies",
    "cine": "movies", "movies": "movies", "series": "series",
    "entretenimiento": "entertainment", "music": "music",
    "musica": "music", "música": "music", "religion": "religious",
    "religión": "religious", "religiosos": "religious",
}


def group_context(attributes):
    raw = attributes.get("group-title", "").strip()
    key = norm(raw)
    country = COUNTRY_GROUPS.get(key, "")
    category = CATEGORY_GROUPS.get(key, "")
    # Some playlists use compound group names such as "Colombia | Deportes".
    if not country:
        for name, code in COUNTRY_GROUPS.items():
            if name in key.split():
                country = code
                break
    if not category:
        for name, value in CATEGORY_GROUPS.items():
            if name in key:
                category = value
                break
    return raw, country, category


def context_score(channel, country, category):
    score = 0
    channel_countries = channel.get("country") or []
    if isinstance(channel_countries, str):
        channel_countries = [channel_countries]
    if country and country in channel_countries:
        score += 0.12
    channel_categories = channel.get("categories") or []
    if isinstance(channel_categories, str):
        channel_categories = [channel_categories]
    if category and category in channel_categories:
        score += 0.08
    return score


def build_fuzzy_catalog(channel_name_index, feed_name_index):
    """Construye una vez un catálogo deduplicado para búsqueda aproximada."""
    catalog = {}
    for key, candidates in channel_name_index.items():
        for channel in candidates:
            channel_id = str(channel.get("id") or "")
            if channel_id:
                catalog[(key, channel_id, "")] = (key, channel_id, channel, "")
    for key, feeds in feed_name_index.items():
        for feed in feeds:
            channel_id = str(feed.get("channel") or "")
            if channel_id:
                feed_id = str(feed.get("id") or "")
                catalog[(key, channel_id, feed_id)] = (
                    key, channel_id, {"id": channel_id}, feed_id
                )
    buckets = defaultdict(list)
    for item in catalog.values():
        key = item[0]
        # Primer carácter y longitud aproximada reducen comparaciones inútiles.
        buckets[(key[:1], len(key.split()))].append(item)
    return buckets


def identify_channel(display_name, attributes, channel_by_id,
                      channel_name_index, feed_name_index, fuzzy_catalog=None):
    """Identifica por nombre/alias exacto o por similitud conservadora.

    Los sufijos de opciones y calidad se eliminan únicamente para buscar.
    Nunca se cambia el nombre visible ni otros atributos de la playlist.
    """
    raw_candidates = [display_name, attributes.get("tvg-name", "")]
    checked = set()
    search_names = []
    group_name, group_country, group_category = group_context(attributes)
    for candidate_name in raw_candidates:
        key = norm(candidate_name)
        if not key or key in checked:
            continue
        checked.add(key)
        # Los nombres cortos se admiten si el grupo aporta contexto de país/categoría.
        if len(key.replace(" ", "")) < 3:
            continue
        if len(key.split()) < 2 and not (group_country or group_category):
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

    # Tolera errores menores usando un índice precalculado, no reconstruye
    # ni recorre todo el catálogo completo para cada canal de la playlist.
    if fuzzy_catalog is None:
        fuzzy_catalog = build_fuzzy_catalog(channel_name_index, feed_name_index)

    for candidate_name, key in search_names:
        scored = {}
        possible = []
        words = len(key.split())
        for length_bucket in (words - 1, words, words + 1):
            if length_bucket < 1:
                continue
            possible.extend(fuzzy_catalog.get((key[:1], length_bucket), []))
        # Si el primer carácter difiere por un error de escritura, ampliar solo
        # a nombres de longitud parecida; sigue siendo mucho menor que el catálogo.
        if not possible:
            for (first, count), bucket in fuzzy_catalog.items():
                if abs(count - words) <= 1:
                    possible.extend(bucket)
        seen_catalog = set()
        for catalog_key, channel_id, channel_stub, feed_id in possible:
            dedupe_key = (catalog_key, channel_id, feed_id)
            if dedupe_key in seen_catalog:
                continue
            seen_catalog.add(dedupe_key)
            channel = channel_by_id.get(channel_id, channel_stub)
            ratio = difflib.SequenceMatcher(None, key, catalog_key).ratio()
            words_ratio = difflib.SequenceMatcher(
                None, " ".join(sorted(key.split())),
                " ".join(sorted(catalog_key.split()))
            ).ratio()
            base_score = max(ratio, words_ratio)
            # El contexto nunca identifica por sí solo: solo desempata nombres parecidos.
            score = min(1.0, base_score + context_score(
                channel, group_country, group_category
            ))
            minimum = 0.76 if (group_country or group_category) else 0.82
            if base_score >= minimum:
                previous = scored.get(channel_id)
                if previous is None or score > previous[0]:
                    scored[channel_id] = (score, channel, feed_id, catalog_key, base_score)

        ranked = sorted(scored.items(), key=lambda pair: pair[1][0], reverse=True)
        if ranked:
            _, best = ranked[0]
            second_score = ranked[1][1][0] if len(ranked) > 1 else 0
            min_score = 0.76 if (group_country or group_category) else 0.82
            if best[4] >= min_score and (best[0] - second_score >= 0.045 or len(ranked) == 1):
                context_note = f"; grupo='{group_name}'" if group_name else ""
                return best[1], best[2], (
                    f"similitud {best[4]:.0%} con nombre registrado "
                    f"'{best[3]}' a partir de '{candidate_name}'{context_note}"
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



def get_json_object(url):
    """Descarga JSON de un catálogo secundario; admite objetos o listas."""
    data, _, _ = fetch(url, MAX_JSON_BYTES)
    return json.loads(data.decode("utf-8-sig"))


def secondary_logo_indexes():
    """Carga fuentes adicionales de forma tolerante: si una cae, sigue el proceso."""
    jaruba = {}
    yasser = []
    source_status = {}

    try:
        data = get_json_object(JARUBA_URL)
        if isinstance(data, dict):
            for name, path in data.items():
                if isinstance(name, str) and isinstance(path, str) and path.strip():
                    path = path.strip()
                    if path.startswith(("https://", "http://")):
                        url = path
                    else:
                        url = JARUBA_BASE.rstrip("/") + "/" + path.lstrip("/")
                    jaruba[norm(name)] = {"name": name, "url": url, "source": "jaruba"}
        source_status["jaruba"] = f"OK ({len(jaruba)} nombres)"
    except Exception as exc:
        source_status["jaruba"] = f"no disponible: {type(exc).__name__}: {str(exc)[:100]}"

    try:
        data = get_json_object(YASSER_INDEX_URL)
        # El índice puede ser una lista de canales o un objeto que contenga esa lista.
        entries = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ("channels", "logos", "items", "data"):
                if isinstance(data.get(key), list):
                    entries = data[key]
                    break
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("title") or item.get("channel") or item.get("id")
            raw_url = item.get("url") or item.get("logo") or item.get("image") or item.get("src")
            raw_path = item.get("path") or item.get("file") or item.get("filename")
            if not raw_url and isinstance(raw_path, str) and raw_path.strip():
                raw_url = YASSER_BASE.rstrip("/") + "/" + raw_path.lstrip("/")
            if isinstance(name, str) and name.strip() and isinstance(raw_url, str) and raw_url.startswith(("https://", "http://")):
                yasser.append({"name": name.strip(), "url": raw_url, "source": "yasserstudio"})
                for alt in item.get("alt_names", []) if isinstance(item.get("alt_names"), list) else []:
                    if isinstance(alt, str) and alt.strip():
                        yasser.append({"name": alt.strip(), "url": raw_url, "source": "yasserstudio"})
        source_status["yasserstudio"] = f"OK ({len(yasser)} nombres/alias indexados)"
    except Exception as exc:
        source_status["yasserstudio"] = f"no disponible: {type(exc).__name__}: {str(exc)[:100]}"

    return jaruba, yasser, source_status


def secondary_candidates(display_name, tvg_name, jaruba, yasser):
    """Solo devuelve candidatos exactos o con similitud muy alta y no ambigua."""
    names = []
    for value in (display_name, tvg_name):
        key = norm(value)
        if key and key not in names:
            names.append(key)
    result = []
    seen = set()
    for key in names:
        if key in jaruba:
            item = jaruba[key]
            if item["url"] not in seen:
                result.append((item["url"], f"Jaruba: coincidencia exacta '{item['name']}'"))
                seen.add(item["url"])
        exact = [item for item in yasser if norm(item.get("name")) == key]
        exact_urls = {item["url"] for item in exact}
        if len(exact_urls) == 1:
            item = exact[0]
            if item["url"] not in seen:
                result.append((item["url"], f"Yasserstudio: coincidencia exacta '{item['name']}'"))
                seen.add(item["url"])

        # Fuzzy solo con alta similitud, al menos dos palabras (o nombre largo),
        # y margen suficiente frente al segundo resultado. Evita TV ARAGUA -> Paraguay TV.
        pool = []
        for item in list(jaruba.values()) + yasser:
            candidate_key = norm(item.get("name"))
            if not candidate_key:
                continue
            ratio = difflib.SequenceMatcher(None, key, candidate_key).ratio()
            if ratio >= 0.94 and (len(key.replace(" ", "")) >= 8 or len(key.split()) >= 2):
                pool.append((ratio, item))
        pool.sort(key=lambda pair: pair[0], reverse=True)
        if pool:
            best_ratio, best_item = pool[0]
            second = pool[1][0] if len(pool) > 1 else 0
            best_urls = {item["url"] for ratio, item in pool if ratio == best_ratio}
            if best_ratio - second >= 0.04 and len(best_urls) == 1 and best_item["url"] not in seen:
                result.append((best_item["url"], f"{best_item['source']}: similitud estricta {best_ratio:.0%} con '{best_item['name']}'"))
                seen.add(best_item["url"])
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
    load_persistent_cache()
    print(f"Base persistente de logos: {PERSISTENT_LOGO_DIR} ({len(_persistent_cache):,} URLs guardadas)")
    playlist_text, input_label = choose_input(root)

    lines = playlist_text.splitlines()
    extinf_count = sum(1 for line in lines if line.strip().startswith("#EXTINF:"))
    print(f"Playlist descargada: {len(playlist_text):,} caracteres; {extinf_count:,} entradas EXTINF.")
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
    jaruba_logos, yasser_logos, extra_sources = secondary_logo_indexes()
    print("Fuentes secundarias:", json.dumps(extra_sources, ensure_ascii=False))
    channel_by_id, channel_name_index, feed_name_index = build_indexes(
        channels, feeds
    )
    fuzzy_catalog = build_fuzzy_catalog(channel_name_index, feed_name_index)
    print(f"Índice de búsqueda aproximada preparado: {sum(len(v) for v in fuzzy_catalog.values()):,} registros.")

    # Overrides manuales gestionados únicamente por el workflow de GitHub Actions.
    overrides_path = root / "logo_overrides.json"
    try:
        overrides = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
        if not isinstance(overrides, dict):
            overrides = {}
    except Exception as exc:
        print(f"AVISO: no se pudo leer logo_overrides.json: {exc}")
        overrides = {}

    # Verifica en paralelo solo logos actuales y URLs manuales.
    # No precalienta todos los candidatos de todos los catálogos: eso puede
    # disparar decenas de miles de peticiones aunque no se necesiten y agotar
    # el límite de GitHub Actions. Los candidatos se verifican bajo demanda y
    # verify_logo usa caché para no repetir URLs ya comprobadas.
    urls_to_verify = set()
    for source_line in lines:
        if not source_line.strip().startswith("#EXTINF:"):
            continue
        source_header, source_name = split_extinf(source_line.strip())
        source_attrs = attrs(source_header)
        source_current = source_attrs.get("tvg-logo", "").strip()
        manual = overrides.get(source_name, "__NO_OVERRIDE__")
        if source_current.startswith(("http://", "https://")):
            urls_to_verify.add(source_current)
        if isinstance(manual, str) and manual.startswith(("http://", "https://")):
            urls_to_verify.add(manual)

    unique_urls = sorted(urls_to_verify)
    print(f"Verificando {len(unique_urls)} URLs actuales/manuales únicas con hasta {VERIFY_WORKERS} solicitudes concurrentes...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
        for index, _ in enumerate(pool.map(verify_logo, unique_urls), start=1):
            if index % 100 == 0 or index == len(unique_urls):
                save_persistent_cache()
                print(f"Progreso logos actuales/manuales: {index}/{len(unique_urls)} URLs; caché guardada")

    # Reúne logos funcionales de variantes antes de procesar la lista, para que
    # una variante que aparece más tarde también pueda ayudar al canal original.
    reusable_logo_by_name = {}
    for source_line in lines:
        if not source_line.strip().startswith("#EXTINF:"):
            continue
        source_header, source_name = split_extinf(source_line.strip())
        source_attrs = attrs(source_header)
        source_logo = source_attrs.get("tvg-logo", "").strip()
        source_override = overrides.get(source_name, "__NO_OVERRIDE__")
        if source_override is None or source_override == "":
            continue
        candidate_logo = source_override if isinstance(source_override, str) and source_override.startswith(("http://", "https://")) else source_logo
        if candidate_logo and persistent_verify_logo(candidate_logo)[0]:
            for possible_name in (source_name, source_attrs.get("tvg-name", "")):
                key = norm(possible_name)
                if key:
                    reusable_logo_by_name.setdefault(key, candidate_logo)

    out_lines = []
    rows = []
    total = preserved = repaired = unresolved = 0

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#EXTINF:"):
            out_lines.append(line)
            continue

        total += 1
        if total % 100 == 0 or total == extinf_count:
            print(f"Progreso de canales: {total}/{extinf_count}")
        header, display_name = split_extinf(stripped)
        original_attrs = attrs(header)
        current_logo = original_attrs.get("tvg-logo", "").strip()
        channel, feed_id, reason = identify_channel(
            display_name,
            original_attrs,
            channel_by_id,
            channel_name_index,
            feed_name_index,
            fuzzy_catalog,
        )

        chosen_logo = current_logo
        status = "sin cambios"
        detail = ""
        current_ok = False
        current_result = "No se verificó el logo actual"

        # Primero se valida el logo existente, aunque el canal no se identifique.
        # Nunca se elimina un logo funcional solo porque el catálogo no reconozca el nombre.
        if current_logo:
            current_ok, current_result = persistent_verify_logo(current_logo)
        else:
            current_ok, current_result = False, "No tiene tvg-logo"

        if current_ok:
            preserved += 1
            status = "conservado"
            detail = f"Logo actual funcional: {current_result}"
        else:
            reuse_key = norm(display_name)
            reusable_logo = reusable_logo_by_name.get(reuse_key)
            if not reusable_logo:
                reusable_logo = reusable_logo_by_name.get(norm(original_attrs.get("tvg-name", "")))
            if reusable_logo and reusable_logo != current_logo:
                chosen_logo = reusable_logo
                header = set_attr(header, "tvg-logo", reusable_logo)
                repaired += 1
                status = "logo reutilizado de variante"
                detail = "Se reutilizó un logo previamente verificado de una entrada con el mismo nombre normalizado (misma identidad de canal)."
                current_ok = True
        if status == "conservado" or status == "logo reutilizado de variante":
            pass
        elif channel:

            channel_id = str(channel.get("id") or "")
            replacement = ""
            last_candidate_result = "La API no tiene candidatos in_use para este canal."

            for candidate in candidate_logos(channel_id, feed_id, logos):
                candidate_url = str(candidate.get("url") or "")
                valid, result = persistent_verify_logo(candidate_url)
                if valid:
                    replacement = candidate_url
                    last_candidate_result = f"iptv-org: reemplazo verificado: {result}"
                    break
                last_candidate_result = f"Candidato iptv-org rechazado: {result}"

            if not replacement:
                for candidate_url, candidate_reason in secondary_candidates(
                    display_name, original_attrs.get("tvg-name", ""),
                    jaruba_logos, yasser_logos
                ):
                    valid, result = persistent_verify_logo(candidate_url)
                    if valid:
                        replacement = candidate_url
                        last_candidate_result = f"{candidate_reason}; imagen verificada: {result}"
                        break
                    last_candidate_result = f"{candidate_reason}; candidato rechazado: {result}"

            if replacement:
                chosen_logo = replacement
                header = set_attr(header, "tvg-logo", replacement)
                repaired += 1
                status = "reparado" if current_logo else "logo añadido"
                detail = f"{last_candidate_result}; identificación: {reason}"
            else:
                unresolved += 1
                status = "no reparado"
                detail = (
                    "No se encontró logo API verificable. "
                    + last_candidate_result
                )
        else:
            unresolved += 1
            status = "no reparado"
            # Si la URL actual está rota, la conservamos como atributo hasta encontrar
            # una identidad fiable; no se inventa ni se borra información del usuario.
            tvg_name = original_attrs.get("tvg-name", "").strip()
            extra = f"; tvg-name='{tvg_name}' tampoco permitió identificarlo" if tvg_name else ""
            detail = (
                f"No se pudo identificar de forma fiable '{display_name}'"
                f"{extra}; no se cambió el nombre ni se inventó un logo."
            )

        # Correcciones manuales autorizadas tienen prioridad sobre la detección automática.
        # Clave exacta: el nombre visible no se normaliza ni se modifica.
        if display_name in overrides:
            previous_status = status
            manual_value = overrides[display_name]
            if manual_value is None or manual_value == "":
                header = remove_attr(header, "tvg-logo")
                chosen_logo = ""
                status = "logo quitado manualmente"
                detail = "Un administrador/colaborador quitó el logo mediante el workflow autorizado."
            elif isinstance(manual_value, str):
                valid_manual, manual_result = persistent_verify_logo(manual_value)
                if valid_manual:
                    header = set_attr(header, "tvg-logo", manual_value)
                    chosen_logo = manual_value
                    status = "logo manual"
                    detail = f"URL manual aprobada por workflow autorizado; {manual_result}"
                else:
                    print(f"AVISO: URL manual inválida para {display_name}: {manual_result}")
                    detail += f"; la URL manual no se aplicó: {manual_result}"
            if status in ("logo quitado manualmente", "logo manual"):
                if previous_status == "conservado":
                    preserved -= 1
                elif previous_status in ("reparado", "logo añadido"):
                    repaired -= 1
                elif previous_status == "no reparado":
                    unresolved -= 1
                if status == "logo manual":
                    repaired += 1
                else:
                    unresolved += 1

        out_lines.append("#EXTINF:" + header + "," + display_name)
        rows.append({
            "canal": display_name,
            "grupo": original_attrs.get("group-title", ""),
            "tvg_id_original": original_attrs.get("tvg-id", ""),
            "logo_original": current_logo,
            "logo_final": chosen_logo,
            "estado": status,
            "identificacion": reason,
            "detalle": detail,
        })

    # Persistir el inventario por entrada, manteniendo cada variante independiente.
    channel_database = []
    for source_line in lines:
        if not source_line.strip().startswith("#EXTINF:"):
            continue
        db_header, db_name = split_extinf(source_line.strip())
        db_attrs = attrs(db_header)
        logo_url = db_attrs.get("tvg-logo", "").strip()
        channel_database.append({
            "canal": db_name, "tvg_name": db_attrs.get("tvg-name", ""),
            "grupo": db_attrs.get("group-title", ""), "tvg_id": db_attrs.get("tvg-id", ""),
            "logo_url": logo_url, "url_status": _persistent_cache.get(logo_url, {}).get("result", "sin verificar"),
            "checked_at": _persistent_cache.get(logo_url, {}).get("checked_at", ""),
        })
    save_persistent_cache(channel_database)
    print(f"Base persistente guardada: {len(_persistent_cache):,} URLs; {len(channel_database):,} entradas de canal.")

    output_path.write_text(
        "\n".join(out_lines).rstrip() + "\n", encoding="utf-8"
    )

    fields = [
        "canal", "grupo", "tvg_id_original", "logo_original", "logo_final",
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
        "contexto_grupo_habilitado": True,
        "fuentes_logo": ["iptv-org logos.json", "Jaruba Channel Logos", "Yasserstudio TV Logos (si el índice permite extraer URLs)"],
        "estado_fuentes_secundarias": extra_sources,
        "catalogo_jaruba_nombres": len(jaruba_logos),
        "catalogo_yasserstudio_nombres": len(yasser_logos),
        "base_urls_persistente": str(PERSISTENT_CACHE_PATH.relative_to(root)),
        "urls_en_base_persistente": len(_persistent_cache),
        "entradas_en_base_canales": len(channel_database),
        "verificacion_periodica_dias": 7,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Markdown report: enlace a workflow manual. GitHub exige permiso de escritura
    # para ejecutar workflow_dispatch; no se ofrece una acción pública anónima.
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    workflow_url = f"https://github.com/{repository}/actions/workflows/manage-logo.yml" if repository else ""
    def management_link(channel):
        if not workflow_url:
            return "_Corrección manual disponible para administradores/colaboradores desde Actions → Gestionar logo manualmente._"
        title = urllib.parse.quote(f"Gestionar logo: {channel}", safe="")
        return f"[Gestionar logo (solo admin/colaboradores)]({workflow_url}?query={title})"

    # Markdown report with actual logo URLs as image previews.
    def md_escape(value):
        return str(value or "").replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    repaired_rows = [row for row in rows if row["estado"] in ("reparado", "logo añadido", "logo manual")]
    preserved_rows = [row for row in rows if row["estado"] == "conservado"]
    unresolved_rows = [row for row in rows if row["estado"] in ("no reparado", "logo quitado manualmente")]

    report_lines = [
        "# Informe de reparación de logos IPTV",
        "",
        f"- **Base persistente:** `{PERSISTENT_CACHE_PATH.relative_to(root)}` ({len(_persistent_cache)} URLs; revalidación positiva cada 7 días)",
        "",
        f"- **Entrada:** {md_escape(input_label)}",
        f"- **Canales analizados:** {total}",
        f"- **Logos reparados o añadidos (incluye URL manual):** {repaired}",
        f"- **Logos funcionales conservados:** {preserved}",
        f"- **Canales sin reparar o con logo retirado manualmente:** {unresolved}",
        "",
        "> La verificación confirma que la URL entrega un archivo con firma de imagen; no garantiza por sí sola que el logo represente visualmente el canal correcto.",
        "> Cuando existe `group-title`, se usa como contexto de país o categoría para desempatar coincidencias; nunca se usa el grupo por sí solo para inventar la identidad del canal.",
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
                f"- **Fuente/resultado:** {md_escape(row['detalle'])}",
                f"- **Logo:** {md_escape(row['logo_final'])}",
                f"- **Detalle:** {md_escape(row['detalle'])}",
                f"- **Corrección manual:** {management_link(row['canal'])}",
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
            report_lines.append(f"  - **Corrección manual:** {management_link(row['canal'])}")
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
                f"- **Corrección manual:** {management_link(row['canal'])}",
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
