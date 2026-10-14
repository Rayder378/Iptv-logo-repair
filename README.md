# IPTV Logo Repair

Script para comprobar los logos `tvg-logo` de una playlist M3U y reemplazar solo los que no se pueden verificar, usando los catálogos públicos de iptv-org.

## Archivos

- `playlist.m3u`: playlist de entrada. Coloca aquí tu M3U.
- `logo_repair.py`: script principal.
- `.github/workflows/logo-repair.yml`: ejecución manual, al cambiar archivos y diariamente.
- `output/playlist_logos_reparados.m3u`: playlist corregida.
- `output/informe_logos.csv` y `output/informe_logos.json`: resultados.

## Puesta en marcha

1. Crea un repositorio nuevo en GitHub.
2. Sube `logo_repair.py`, `playlist.m3u` y `.github/workflows/logo-repair.yml`, respetando las rutas.
3. En GitHub abre **Actions** y habilita los workflows si GitHub lo solicita.
4. Ejecuta **Reparar logos IPTV → Run workflow**.
5. Cuando termine, revisa `output/playlist_logos_reparados.m3u` y `output/informe_logos.csv`.

## Qué hace y qué no hace

- Mantiene un logo actual si el contenido descargado parece una imagen válida.
- Comprueba el cuerpo de la imagen, no solo el código HTTP.
- Si falla, busca logos marcados `in_use` para el canal identificado en la API.
- Usa coincidencias exactas por `tvg-id`, nombre o nombre de feed. No usa coincidencias parciales para evitar asignar el logo de un canal parecido.
- Si no puede identificar el canal o verificar un reemplazo, lo deja sin reparar y lo registra; no inventa un logo.
- No altera el archivo de entrada.
- No comprueba si el logo corresponde visualmente a la marca; verifica que la URL entregue una imagen y confía en la asociación del catálogo.
- Algunos servidores bloquean las solicitudes de GitHub Actions o limitan las descargas. Esos casos pueden aparecer como fallidos aunque el logo funcione en una app. Revisa el informe antes de confiar en todos los resultados.

## Privacidad

El archivo `playlist.m3u` se guarda en el repositorio. Si contiene URLs privadas, tokens o enlaces de acceso personal, configura el repositorio como **privado** y no publiques la playlist generada. No subas credenciales al código.
