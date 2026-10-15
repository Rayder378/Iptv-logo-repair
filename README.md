# IPTV Logo Repair

Revisa el atributo `tvg-logo` de una playlist M3U y añade o reemplaza logos usando iptv-org y catálogos secundarios de logos.

## Entrada

- Si existe el secreto `M3U_URL`, el script descarga la playlist desde esa URL.
- Si no existe `M3U_URL`, busca archivos `.m3u`/`.m3u8` en la raíz.
- Si hay varios archivos locales, elige el primero por orden alfabético y lo informa en el log.
- Para elegir uno, crea la variable de repositorio `PLAYLIST_FILE` con el nombre exacto del archivo.

## Cómo identifica los canales

1. Prueba el nombre visible, `tvg-name`, nombres alternativos y feeds registrados en iptv-org.
2. Tolera errores de escritura cuando la coincidencia es suficientemente clara.
3. Si la entrada incluye `group-title`, utiliza grupos conocidos como `Colombia`, `Documentales`, `Infantiles`, `Deportes` o `Noticias` como contexto adicional. El grupo ayuda a desempatar; por sí solo nunca determina el canal.
4. Los nombres cortos pueden aceptarse si aparecen registrados de forma única en el catálogo. Si no se puede confirmar la identidad, no inventa un logo.

**Importante:** el grupo solo ayuda si está presente en la playlist original. El script no puede deducir el país o la categoría a partir de un grupo que no existe.

## Qué modifica

- Solo modifica el atributo `tvg-logo`.
- Conserva los nombres visibles, `tvg-id`, `group-title`, las URL de transmisión y las demás líneas de la playlist.
- Si el logo actual entrega una imagen reconocible, lo conserva.
- Si no puede identificar un canal de forma fiable, no borra el logo existente ni inventa otro.
- Comprueba que las URL de logo candidatas devuelvan datos con firma de imagen antes de asignarlas.

## Fuente de logos

El script consulta estas fuentes públicas:

- **iptv-org** (`channels.json`, `feeds.json`, `logos.json`): identidad de canales, alias, feeds y logos.
- **Jaruba Channel Logos** (`logo_paths.json`): catálogo de logos por nombre normalizado.
- **Yasserstudio TV Logos** (`index.json`): se intenta leer como fuente secundaria; el repositorio está archivado y el script solo aprovecha entradas cuyo índice exponga una URL utilizable.

Si una fuente secundaria no responde o cambia su formato, el script registra el estado y continúa con las demás. Los candidatos secundarios se verifican descargando la imagen y comprobando su firma. Las coincidencias aproximadas son deliberadamente estrictas para reducir asignaciones equivocadas; si no hay identidad fiable, el canal queda pendiente. Una respuesta válida de imagen tampoco garantiza por sí sola que el dibujo sea el logo correcto.

## Archivos

- `logo_repair.py`: script principal.
- `.github/workflows/logo-repair.yml`: workflow de GitHub Actions.
- `output/playlist_logos_reparados.m3u`: playlist generada.
- `output/informe_logos.csv` y `output/informe_logos.json`: informes.
- `output/report.md`: resumen Markdown con enlaces y vistas previas.

## Ejecución

1. Sube `logo_repair.py` y `.github/workflows/logo-repair.yml` al repositorio.
2. Configura `M3U_URL` en **Settings → Secrets and variables → Actions → New repository secret**, o sube la playlist a la raíz.
3. Ejecuta **Actions → Reparar logos IPTV → Run workflow**.
4. Revisa `output/report.md` y `output/informe_logos.csv` para ver qué se reparó y por qué otros canales quedaron pendientes.

## Limitaciones y privacidad

- El script no modifica ni comprueba las URL de transmisión.
- Algunos servidores pueden bloquear las comprobaciones desde GitHub Actions aunque el logo abra en un reproductor.
- Los nombres de grupos pueden variar; solo se reconocen los grupos configurados en el script.
- Si la playlist contiene enlaces privados o tokens, no la publiques en un repositorio público. Usa un repositorio privado o un secreto `M3U_URL`.
