# IPTV Logo Repair

Comprueba los logos `tvg-logo` de una playlist M3U y reemplaza los que no se pueden verificar con logos del catálogo público de iptv-org.

## Entrada flexible

- Si el secreto `M3U_URL` está configurado, el script usa esa URL.
- Si `M3U_URL` no está configurado, busca archivos `.m3u` y `.m3u8` en la raíz del repositorio.
- Si hay varios archivos locales, elige el primero por orden alfabético y lo informa en el log.
- Para elegir un archivo local concreto, crea la variable de repositorio `PLAYLIST_FILE` con el nombre del archivo, por ejemplo `mi_lista.m3u`.

## Archivos

- `logo_repair.py`: script principal.
- `playlist.m3u`: ejemplo de archivo local de entrada (sustitúyelo por tu playlist si quieres usar modo archivo).
- `.github/workflows/logo-repair.yml`: workflow de GitHub Actions.
- `output/playlist_logos_reparados.m3u`: playlist reparada.
- `output/informe_logos.csv` y `output/informe_logos.json`: informes.
- `output/report.md`: resumen Markdown con los nombres de los canales, estado y vistas previas de los logos.

## Uso

1. Crea un repositorio nuevo.
2. Sube `logo_repair.py` y `.github/workflows/logo-repair.yml`.
3. Para el modo local, sube uno o más archivos `.m3u`/`.m3u8` a la raíz.
4. Para modo URL, abre **Settings → Secrets and variables → Actions → New repository secret** y crea `M3U_URL` con la URL de la playlist.
5. Ve a **Actions → Reparar logos IPTV → Run workflow**.

## Seguridad y limitaciones

- El archivo de entrada no se modifica.
- Si el logo actual devuelve una imagen válida, se conserva.
- Si falla, se buscan logos `in_use` de la identidad exacta del canal o de su feed.
- No se usan coincidencias parciales para elegir logos.
- Si la identidad no es inequívoca, el canal queda registrado como no reparado.
- Comprobar que una URL devuelve una imagen no garantiza que esa imagen sea visualmente el logo correcto.
- Algunos servidores bloquean GitHub Actions, por lo que una imagen puede funcionar en un reproductor y fallar en la comprobación automática.
- Si la playlist contiene tokens o URLs privadas, usa un repositorio privado o el secreto `M3U_URL`. No publiques enlaces privados.
