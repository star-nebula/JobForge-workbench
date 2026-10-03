# JobForge Workbench

**Language / 语言 / 言語 / Idioma / 언어 / Langues / Sprachen:** [English](../README.md) | [简体中文](README_zh-CN.md) | [日本語](README_ja.md) | [한국어](README_ko.md) | [Français](README_fr.md) | [Deutsch](README_de.md) | [Español](README_es.md)

Banco de trabajo local para la búsqueda de empleo: currículum → palabras clave → scraping de ofertas (BOSS Zhipin) → ranking por compatibilidad, todo en una sola herramienta de máquina única.

![Panel de control](../docs/screenshots/dashboard.png)

## Funcionalidades

- **Perfil**: carga y análisis del currículum, comparación y adopción campo por campo, puntuación local basada en reglas con sugerencias de mejora, vista previa del currículum, exportación a PDF
- **Scraping inteligente**: obtiene ofertas de BOSS a partir de las palabras clave del currículum (canal de navegador nativo), con historial de scraping y deduplicación por oferta
- **Mercado de ofertas**: las ofertas se guardan en la base de datos, obtención del JD con limpieza del texto, orden por compatibilidad, estadísticas de obtención del JD (obtenido / no obtenido / posiblemente incompleto; las píldoras filtran al hacer clic; los posiblemente incompletos pueden confirmarse manualmente o volver a obtenerse desde el modal de detalle). La compatibilidad tiene dos niveles: **preselección por etiquetas** (reglas locales de 4 dimensiones: habilidades / intención / salario / ciudad; el denominador de habilidades son las etiquetas de la oferta, coincidencia exacta tras normalizar palabras, salario normalizado a K, 0-100 real sin piso), calculada durante el scraping; y **ajuste fino del JD** (un LLM lee el JD completo más la información dura: etiquetas / salario / ciudad), producido en el modal de detalle o en el análisis por lotes; las tarjetas llevan una insignia azul «AI xx»
- **Capacidades de IA** (configuración multi-modelo, protocolo compatible con OpenAI, claves guardadas solo en SQLite local): generación de mensajes de saludo para BOSS, análisis de compatibilidad con IA (se ejecuta automáticamente en el modal de detalle + lote con un clic en el mercado de ofertas), pulido del currículum (comparación diff antes de adoptar)
- **Pipeline de postulaciones**: tablero kanban con arrastrar y soltar en 6 estados (discovered / reviewing / applied / interviewing / rejected / offered)
- **Agenda de entrevistas**: programa fecha, hora y notas desde la columna «entrevistando»
- **Centro de mensajes**: sincronización de solo lectura de las conversaciones de BOSS Zhipin (intercepción de respuestas CDP)
- **Ventana flotante de progreso**: permanece siempre visible durante el scraping, con botones de pausar / reanudar / detener. La ventana no roba el foco y deja pasar los clics del ratón, por lo que **no interrumpe una sesión de scraping con teclado y ratón en curso**; se puede mover arrastrando el asa ⠿

## Estructura del proyecto

```
JobForge-workbench/
├─ src/jobforge/                 # Código: paquete de Python
│  ├─ server.py                  # Punto de entrada FastAPI
│  ├─ paths.py                   # Fuente única de rutas del proyecto (código y datos desacoplados)
│  ├─ spider.py  fetch_jd.py  fetch_jd_native.py  fetch_gate.py
│  ├─ db.py  llm.py  profile_score.py
│  └─ tools/                     # Scripts de subproceso, lanzados por el server como `python -m jobforge.tools.*`
│     └─ hud.py  messages.py  grab_cookies.py
├─ web/job-workbench.html        # Página única del frontend (6 vistas)
├─ data/                         # Datos en tiempo de ejecución (no se versionan): jobs.db, cookies.json, messages.json, archivos de compuerta/limitación, chrome-profile/
├─ tests/                        # Regression con pytest (`venv\Scripts\python.exe -m pytest tests/`; la cantidad de tests no se documenta: se ejecutan para verla)
├─ run.bat  setup.bat  requirements.txt  README.md
```

Todas las rutas de archivos de datos provienen de `paths.py`; ningún módulo las deduce de su propio `__file__` — mover el código nunca se lleva los datos.
El user-data-dir del Chrome depurado por CDP (con la sesión de BOSS) también vive en el directorio de datos: `data/chrome-profile/`.

Hay un único vocabulario de estados de postulación: el `STATUS_META` del frontend (las columnas del kanban, el selector de estado del modal de detalle y el pipeline del dashboard se derivan de él)
y el `db.VALID_STATUSES` del backend. Ambos conjuntos son idénticos y cada estado tiene una vía de escritura alcanzable, fijado por
`tests/test_frontend_status_contract.py` — hubo un caso en que se declaraban 6 estados pero el kanban dibujaba solo 4 columnas,
`rejected`/`offered` no podían establecerse desde la UI y 114 ofertas en la base quedaron con solo dos valores.

Nota: los `.bat` deben mantener finales de línea CRLF (`.gitattributes` declara `*.bat text eol=crlf`) — con «cambio de página de código vía `chcp` + comentarios en chino + LF puro», cmd.exe desalinea el análisis por offset de bytes y se come silenciosamente el inicio de la línea `set "PYTHONPATH=..."`, lo que se manifiesta como `ModuleNotFoundError: No module named 'jobforge'` al arrancar.

El arranque manual (IDE / línea de comandos) requiere que `src` esté en `PYTHONPATH`; de lo contrario `import jobforge` falla:

```
set PYTHONPATH=%CD%\src
venv\Scripts\python.exe -m jobforge.server
```

## Arquitectura

| Archivo | Función |
|---|---|
| `src/jobforge/server.py` | Punto de entrada FastAPI, `127.0.0.1:8080` (`--port` permite cambiarlo, útil para depurar una segunda instancia) |
| `src/jobforge/paths.py` | Fuente única de rutas: `PROJECT_ROOT` / `DATA_DIR` / `WEB_DIR` |
| `src/jobforge/spider.py` / `fetch_jd_native.py` | Scraping de la lista de ofertas y del JD (teclado/ratón nativo + canal UIA TextPattern) |
| `src/jobforge/fetch_jd.py` | Entrada general de obtención del JD (nativo primero, CDP de respaldo) + limpieza del texto del JD |
| `src/jobforge/tools/hud.py` | Ventana flotante de progreso (proceso independiente; las tres restricciones de ventana — siempre visible + sin robo de foco + click-through — están documentadas en el comentario de cabecera) |
| `src/jobforge/fetch_gate.py` | Compuerta de pausa/parada entre procesos: la señal se escribe en `data/fetch_gate.json`, compartida por el hilo del server y los subprocesos de scraping |
| `src/jobforge/tools/messages.py` | Sincronización de mensajes de BOSS (Playwright CDP se conecta al navegador del puerto 9222 e intercepta respuestas de página) |
| `src/jobforge/db.py` | SQLite (WAL): ofertas / mensajes / perfil / historial de scraping |
| `src/jobforge/profile_score.py` | Motor de puntuación local por reglas del perfil (13 comprobaciones) |
| `src/jobforge/llm.py` | Capa de capacidades LLM (cliente chat compatible con OpenAI + tres funciones: saludo / análisis de compatibilidad / pulido de currículum) |
| `web/job-workbench.html` | Página única del frontend (6 vistas) |
| `src/jobforge/tools/grab_cookies.py` | Extrae las cookies de sesión del navegador y escribe `data/cookies.json` |

## Uso

1. Doble clic en `setup.bat` para crear el venv e instalar las dependencias
2. Doble clic en `run.bat` para arrancar y abrir <http://127.0.0.1:8080> en el navegador
3. Abre Chrome de escritorio e inicia sesión en zhipin.com para habilitar el scraping (el scraping toma el control del teclado y ratón durante ~8-15 s; la actualización de mensajes requiere el navegador abierto con el puerto de depuración 9222)
4. Configura un modelo de IA con el ⚙ de la esquina superior derecha para habilitar saludos / análisis de compatibilidad / pulido del currículum (funciona con DeepSeek, Qwen, Zhipu, Ollama o cualquier servicio compatible con OpenAI)
5. Requisito previo del análisis por lotes con IA: Chrome de escritorio abierto con sesión en zhipin.com (ventana no minimizada); el análisis toma el control del teclado y ratón, y 3 fallos consecutivos activan un fusible automático que lo detiene. El server tiene un guardián de instancia única — un segundo arranque es rechazado
6. El progreso del scraping se ve en la ventana flotante (se abre manualmente con «🪟 Ventana de progreso» en la barra superior; también se abre automáticamente al iniciar un scraping):
   - **Pausar** solo se detiene en puntos seguros (límites de oferta, esperas de limitación) y nunca parte por la mitad una acción de teclado/ratón; el tiempo en pausa no cuenta para la limitación, así que no hay que volver a esperar tras reanudar
   - **Detener** surte efecto en segundos (además mata el subproceso de scraping en ejecución); las ofertas ya obtenidas y los análisis de IA ya completados se conservan
   - La ventana aparece por defecto en la esquina inferior derecha; arrastra ⠿ para moverla. Al terminar una tarea permanece unos segundos mostrando el resultado y se cierra sola; ✕ la cierra de inmediato

## Privacidad

`data/` (`jobs.db`, `messages.json`, `cookies.json`, `fetch_gate.json`, `hud_pos.json`, etc.) y el perfil del navegador (`chrome-profile/`, ubicación canónica `data/chrome-profile`) están excluidos por `.gitignore` y nunca se versionan.
