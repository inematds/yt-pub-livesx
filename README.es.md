# yt-pub-livesx

**🇧🇷 [Português](README.md) · 🇺🇸 [English](README.en.md) · 🇪🇸 [Español](README.es.md)**

![YouTube Live Clips — Fabrica de Videos](assets/banner.jpg)

Pipeline automatizado para recortar transmisiones en vivo de YouTube en clips por tema y publicarlos en otro canal.

**Canal de origen** (transmisiones en vivo): [INEMA TDS](https://www.youtube.com/@inematdsx) (`UC2QbQDyPKuHk93dwo5iq3Sw`)
**Canal de destino** (clips): [INEMA TIA](https://www.youtube.com/@InemaTIA) (`UCavuQHkxBSAZbzRoOm6Gq4g`)

## Flujo

```
YouTube (transmisiones en vivo del canal de origen) → Transcripción → Análisis con IA → Corte (FFmpeg) → Miniatura (IA) → Publicación (canal de destino)
```

1. **Sincroniza** las transmisiones en vivo del canal de origen mediante YouTube Data API
2. **Descarga la transcripción** automática (subtítulos de YouTube)
3. **Analiza los temas** con IA (Piramyd/Claude/OpenRouter API)
4. **Corta clips** con FFmpeg según las marcas de tiempo
5. **Genera miniaturas** con IA (LLM + generador de imágenes) o localmente
6. **Publica clips** en el canal de destino con título, descripción, etiquetas y miniatura

## Modos de entrada

Además del flujo principal (recortar transmisiones en vivo → generar clips → publicar), el pipeline acepta otras dos fuentes de video:

1. **Transmisiones en vivo de YouTube** → transcripción → análisis con IA → corte (FFmpeg) → miniatura → publicación
2. **Importación de videos** → la carpeta `imports/` se convierte en la cola del pipeline → miniatura → publicación
3. **Sincronización TikTok → YouTube** → escaneo de canales de TikTok → descarga → cola del pipeline → publicación en el canal de YouTube de destino

### Importación con metadatos listos (`imports/<lote>/manifest.json`)

Cuando la pieza ya llega con título, descripción y portada definidos, el pipeline **usa lo que recibió
y no genera nada adicional**. Campo ausente = flujo normal (título derivado del nombre del archivo,
descripción generada con IA, portada según `thumb_mode` de la configuración).

```
imports/stay-kling/
├── stay-kling.mp4      # nombre del archivo = filename en el manifiesto
├── capa-yt.jpg
└── manifest.json
```

```json
{
  "titulo": "Stay",
  "clips": [
    {
      "filename":    "stay-kling.mp4",
      "title":       "Stay",
      "description": "Paragrafo 1...\n\nParagrafo 2...",
      "tags":        ["cinematic", "power ballad"],
      "thumbnail":   "capa-yt.jpg"
    }
  ]
}
```

| campo | efecto cuando está presente | cuando está ausente |
|---|---|---|
| `title` | se sube exactamente así | título derivado del nombre del archivo |
| `description` | se sube exactamente así; **no pasa por IA** | transcripción + IA (si `import_gerar_descricao=true`) |
| `tags` | se suben tal como están | sin etiquetas |
| `thumbnail` | la portada se envía a YouTube; el generador **no se ejecuta**, ni siquiera con `thumb_mode=api` o `none` | portada generada según `thumb_mode` |
| `privacy` (en el clip o en la raíz) | sobrescribe la visibilidad predeterminada | visibilidad de la configuración del canal |
| `publish_at` (raíz, `HH:MM`) | se respeta cuando `import_fila=false` | entra en la cola normal |

Reglas de la portada: misma carpeta que el MP4, `.jpg`/`.jpeg`/`.png` (PNG se convierte a JPEG,
porque `thumbnails.set` exige `image/jpeg`), máximo 2 MB. Si el archivo falta o no es válido,
se registra en el log y se continúa con el flujo normal — **el lote nunca se interrumpe**.

No es necesario aplicar faststart al MP4: `_ensure_faststart` se ejecuta en todos los videos importados.

`clips` acepta varios elementos — cada uno con su propio título, descripción, etiquetas y portada.

## Estructura

```
yt-pub-livesx/
├── config/                    # Configuración aislada del proyecto
│   ├── .env                   # Variables de entorno (no va al git)
│   ├── client_secret.json     # Credenciales OAuth (no va al git)
│   ├── credentials.enc        # Tokens cifrados (no va al git)
│   ├── .encryption_key        # Clave AES-GCM (no va al git)
│   ├── prompt_cortes.txt      # Prompt de IA para analizar temas
│   ├── prompt_pub.txt         # Prompt de IA para refinar título/descripción
│   └── prompt_thumb.txt       # Prompt de IA para generar miniaturas
├── data/
│   └── lives.db               # Base de datos SQLite local (no va al git)
├── dashboard/
│   ├── server.py              # API backend (servidor HTTP de Python)
│   └── index.html             # Frontend SPA (vanilla JS)
├── scripts/
│   ├── yt-auth                # Autenticación OAuth independiente
│   ├── yt-clip                # Pipeline: transcripción → análisis → corte
│   ├── yt-publish             # Carga de video a YouTube
│   ├── yt-thumbnail           # Genera miniaturas con IA
│   ├── setup-db               # Crea la base de datos SQLite (con --import migra desde Sheets)
│   └── sync-instances         # Sincroniza código con otras instancias
├── systemd/
│   ├── yt-dashboard.service   # Servicio systemd (puerto 8091)
│   └── yt-scheduler.service   # Servicio systemd scheduler
├── db.py                      # Módulo SQLite (CONFIG, LIVES, PUBLICADOS)
├── scheduler.py               # Scheduler automático
├── docker-compose.yml         # Docker (puerto 8091)
├── Dockerfile
├── requirements.txt
├── setup.sh
└── docs/
    └── SETUP-CANAL-DESTINO.md # Documentación completa de la configuración
```

## Requisitos

- Python 3.10+
- ffmpeg
- yt-dlp
- deno (runtime JS para yt-dlp)
- curl
- Pillow (miniaturas)

## Arquitectura: master + canales

El sistema se compone de **1 master-dashboard** + **N instancias** (1 por canal).

```
~/projetos/
├── yt-pub-livesx/              ← PLANTILLA (este repo, sin credenciales)
│   ├── master-dashboard/       ← agrega todas las instancias
│   ├── scripts/setup-system    ← Parte 1: inicia el master
│   └── scripts/setup-canal     ← Parte 2: crea una nueva instancia
│
├── yt-pub-lives1/              ← Canal 1 (copia de la plantilla)
│   ├── config/.env             ← credenciales GCP del canal 1
│   ├── config/credentials.enc  ← tokens OAuth del canal 1
│   ├── data/lives.db           ← SQLite aislado
│   └── lives/                  ← videos descargados
│
├── yt-pub-livesx/              ← Canal 2 (igual)
└── yt-pub-lives7/              ← Canal N
```

### Qué se comparte y qué está aislado

| Recurso | Master (puerto 8090) | Cada canal (puerto 809N) |
|---|---|---|
| Código (Python/HTML) | propio (carpeta de la plantilla) | copia propia |
| Base de datos SQLite | no usa | `data/lives.db` propia |
| Credenciales GCP | no usa | proyecto GCP propio |
| OAuth del canal | no | `config/credentials.enc` propio |
| Servicio systemd | `yt-master-dashboard` | `yt-dashboard<N>` + `yt-scheduler<N>` |
| Actualización de código | manual en la plantilla | mediante `sync-instances` (opt-in) |

### Servicios systemd (modelo final)

```
yt-master-dashboard           → puerto 8090 (agrega todos)
yt-dashboard1 + yt-scheduler1 → puerto 8091 (canal 1)
yt-dashboard2 + yt-scheduler2 → puerto 8092 (canal 2)
...
yt-dashboardN + yt-schedulerN → puerto 809N (canal N)
```

Cada par `dashboard<N>` + `scheduler<N>` es **independiente**: si un canal falla, los demás continúan. El master solo consume las API HTTP de cada dashboard.

### Flujo de datos (1 canal)

```
YouTube (canal de origen)
    ↓ YouTube Data API v3
scheduler<N>  ─→  descarga transmisiones en vivo nuevas (yt-dlp)
    ↓
    transcripción (subtítulos de YouTube)
    ↓
    análisis con IA (Piramyd/Claude) → temas + marcas de tiempo
    ↓
    corte (FFmpeg)
    ↓
    generación de miniatura (IA o local)
    ↓
    carga mediante OAuth → YouTube (canal de destino)
    ↓
data/lives.db  ←  registro local + estado
    ↑
dashboard<N>   ←  UI de control (puerto 809N)
    ↑
master-dashboard ← agrega todas (puerto 8090)
```

### ¿Por qué 1 canal = 1 instancia?

- **El OAuth de YouTube es por usuario/canal** — no se pueden autenticar 2 canales con el mismo OAuth
- **La cuota de YouTube Data API es por proyecto GCP** — separar proyectos = cuotas independientes
- **Aislamiento de fallas** — un error o límite de solicitudes en un canal no afecta a los demás
- **Sincronización de código opcional** — `scripts/sync-instances` propaga actualizaciones de la plantilla; cada instancia decide si se suma

## Instalación

La instalación se divide en **dos partes independientes**:

- **Parte 1 — `setup-system`** (1x por máquina): inicia el master-dashboard
  en el puerto 8090 y prepara las dependencias.
- **Parte 2 — `setup-canal`** (1x por canal, incluido el primero): crea
  una nueva instancia copiando esta plantilla.

> Esta carpeta (`yt-pub-livesx`) es la **plantilla oficial** — nunca debe
> contener `.env`, credenciales ni datos. Toda nueva instancia es una copia
> de ella.

### Parte 1 — Configuración del sistema

```bash
git clone <repo> yt-pub-livesx
cd yt-pub-livesx
./setup.sh                    # equivalente a: ./scripts/setup-system
```

El script:
1. Verifica `python3`, `ffmpeg`, `curl`, `yt-dlp`, `deno`
2. Instala paquetes de Python (`cryptography`, `anthropic`)
3. Inicia el **master-dashboard** como servicio de usuario de systemd (`yt-master-dashboard`)
4. Se detiene con un error si el puerto 8090 ya está en uso

Al terminar: `http://localhost:8090`

### Parte 2 — Agregar un canal

```bash
./scripts/setup-canal
```

Antes de ejecutarlo, ten a mano:

| Pregunta | Origen | Valor predeterminado |
|---|---|---|
| Nombre de la instancia | libre (ej.: `yt-pub-lives7`) | — |
| Número de la instancia (services) | se extrae del nombre si termina en un dígito | siguiente disponible |
| Puerto del dashboard | libre en la máquina | siguiente disponible 8091+ |
| `YOUTUBE_CHANNEL_ID` (origen) | UC... del canal del que provienen las transmisiones en vivo | INEMA TDS |
| Handle del canal de destino | solo documentación | opcional |
| `CLIENT_ID` / `CLIENT_SECRET` | GCP → OAuth Client ID (Desktop App) | — |
| `API_KEY` | GCP → API Key (YouTube Data API v3) | — |
| `GCP_PROJECT` | id del proyecto GCP | — |
| `PIRAMYD_API_KEY` | panel de Piramyd | — |
| Contraseña del dashboard | libre | `Inema2026$$$` |
| ¿Agregar a `sync-instances`? | s/N | N |

> **ENTER en cualquier pregunta con `[default]` acepta el valor predeterminado mostrado.**

#### Requisitos previos en Google Cloud (1 proyecto por instancia)

Cada instancia necesita su **propio proyecto de Google Cloud**:

1. Ve a [Google Cloud Console](https://console.cloud.google.com) y crea un proyecto (ej.: `yt-pub-lives7`)
2. Activa la API: **YouTube Data API v3**
   - Menú: APIs & Services → Library → YouTube Data API v3 → Enable
3. Configura la **pantalla de consentimiento de OAuth**:
   - Tipo: **External**, modo **Testing**
   - Scopes: `youtube`, `youtube.upload`
   - Test users: agrega el **correo electrónico de la cuenta propietaria del canal de destino**
4. Crea credenciales **OAuth 2.0 → Desktop App**:
   - Authorized redirect URIs: `http://localhost:8888`
   - Para volver a autenticar desde el master-dashboard: también `http://localhost:8090/api/auth/callback`
   - Anota `CLIENT_ID` y `CLIENT_SECRET`
5. Crea una **API Key** — anota el valor
6. (Opcional) Verifica el teléfono del canal en `youtube.com/verify`
   - Necesario para subir **miniaturas personalizadas**

#### Qué hace `setup-canal`

1. Hace las preguntas anteriores (ENTER acepta el valor predeterminado)
2. Muestra un resumen y pide confirmación (`[S/n]`)
3. `cp -r` esta plantilla en `~/projetos/<nome>/`
4. Limpia `data/`, `lives/`, `.git/` y los archivos sensibles (`.env`, `credentials.enc`, `.encryption_key`)
5. Genera `config/.env` (chmod 600) con las respuestas
6. Modifica los archivos de servicio (puerto, rutas, dependencia entre dashboard/scheduler)
7. Crea enlaces simbólicos en `~/.config/systemd/user/yt-dashboard<N>.service` y `yt-scheduler<N>.service`
8. Inicia el **dashboard** y **se detiene** para que ejecutes OAuth manualmente
9. Después de OAuth: inicia el **scheduler**
10. (Opcional) registra la instancia en `scripts/sync-instances`

URL final: `http://localhost:<porta>` — ya aparece en el master `http://localhost:8090`

### Autenticación OAuth (paso manual dentro de `setup-canal`)

Cuando `setup-canal` se detenga, abre **otra terminal** y ejecuta:

```bash
GWS_CONFIG_DIR=~/projetos/<nome>/config python3 ~/projetos/<nome>/scripts/yt-auth
```

`yt-auth`:
1. Genera un enlace de autenticación de Google
2. Inicia un servidor local en `http://localhost:8888` que espera el callback
3. Abres el enlace en el navegador y autorizas con la cuenta del canal de destino
4. El callback guarda los tokens cifrados en `config/credentials.enc`

**Solución de problemas de OAuth:**
- *"Access blocked"*: haz clic en **Avançado → Ir para (app) (nao seguro)** (normal en modo Testing)
- *"app has not completed verification"*: la cuenta no está registrada como **test user** — agrégala en GCP → OAuth Consent Screen → Test users
- *"Unable to connect localhost:8888"*: el script `yt-auth` ya terminó — ejecútalo de nuevo y abre el enlace **mientras se está ejecutando**
- Varias cuentas en el navegador: usa una **pestaña de incógnito** o agrega `&login_hint=email@gmail.com` al enlace

**Volver a autenticar desde el Master Dashboard (puerto 8090):**

El master usa `redirect_uri=http://localhost:8090/api/auth/callback`. Este URI también debe estar registrado en GCP → Credentials → OAuth Client ID de la instancia → Authorized redirect URIs. Sin esto, la reautenticación falla incluso después de autorizar en Google.

### Base de datos (SQLite local)

Se crea automáticamente al iniciar el scheduler o el dashboard. Para crearla manualmente:

```bash
python3 scripts/setup-db                # crea una base de datos vacía
python3 scripts/setup-db --import       # crea una base de datos e importa desde Google Sheets (legacy)
```

Base de datos en `data/lives.db` con las tablas **config**, **lives**, **publicados**.

### Despliegue en VPS (Ubuntu/Debian)

Guía paso a paso desde cero en una VPS limpia.

#### 1. Paquetes del sistema

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip ffmpeg curl git unzip pipx
pipx install yt-dlp

# Deno (runtime JS usado por yt-dlp)
curl -fsSL https://deno.land/install.sh | sh
echo 'export PATH="$HOME/.deno/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

#### 2. Habilitar lingering (los servicios se ejecutan sin inicio de sesión)

```bash
sudo loginctl enable-linger $USER
```

Sin esto, todos los servicios `--user` se detienen cuando te desconectas de SSH.

#### 3. Clonar y ejecutar la Parte 1

```bash
mkdir -p ~/projetos && cd ~/projetos
git clone https://github.com/inematds/yt-pub-livesx.git
cd yt-pub-livesx
./setup.sh
```

#### 4. Firewall — opcional, pero recomendado

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8090/tcp        # master-dashboard
sudo ufw allow 8091:8099/tcp   # rango de instancias
sudo ufw enable
```

Para no exponer puertos públicamente, mantén cerrado el firewall y usa un **túnel SSH** desde tu máquina local:

```bash
ssh -L 8090:localhost:8090 -L 8091:localhost:8091 user@vps
```

#### 5. Crear el primer canal

```bash
./scripts/setup-canal
```

**OAuth en una VPS sin navegador:** cuando `setup-canal` se detenga, abre **otra terminal SSH con un túnel del puerto 8888**:

```bash
ssh -L 8888:localhost:8888 user@vps
# dentro de la VPS:
GWS_CONFIG_DIR=~/projetos/yt-pub-lives1/config python3 ~/projetos/yt-pub-lives1/scripts/yt-auth
```

Copia el enlace generado, ábrelo en el **navegador de tu máquina local** y autoriza. El callback llega a `localhost:8888` local → pasa por el túnel SSH → llega a la VPS y guarda los tokens cifrados.

#### 6. Verificar

```bash
systemctl --user list-units --type=service --state=active | grep yt-
journalctl --user -u yt-dashboard1 -f
journalctl --user -u yt-scheduler1 -f
```

#### 7. Copia de seguridad (esencial)

Guarda regularmente **fuera de la VPS**:

- `config/credentials.enc` — sin esto, hay que repetir OAuth
- `config/.encryption_key` — sin esta clave, `credentials.enc` es inútil
- `data/lives.db` — historial de transmisiones en vivo procesadas

```bash
tar -czf backup-$(date +%F).tar.gz \
  ~/projetos/yt-pub-lives*/config/.env \
  ~/projetos/yt-pub-lives*/config/credentials.enc \
  ~/projetos/yt-pub-lives*/config/.encryption_key \
  ~/projetos/yt-pub-lives*/data/lives.db
```

#### Recursos mínimos de la VPS

| Recurso | Mínimo | Recomendado |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU (FFmpeg corta video) |
| RAM | 2 GB | 4 GB |
| Disco | 20 GB | 50+ GB (los videos descargados quedan en `lives/`) |
| Banda | 1 TB/mes | depende de cuántos canales |
| OS | Ubuntu 22.04+ / Debian 12+ | — |

> **Disco:** los videos sin procesar quedan en `lives/` hasta que el pipeline los recorte y publique. Configura una limpieza periódica o el disco se llena:
> ```bash
> find ~/projetos/yt-pub-lives*/lives -mtime +7 -delete
> ```

### Prompts de IA (opcional)

Copia los prompts personalizados a `config/`:
```bash
cp ~/caminho/prompt_cortes.txt config/
cp ~/caminho/prompt_pub.txt config/
cp ~/caminho/prompt_thumb.txt config/
```

O edítalos desde la pestaña de configuración del dashboard.

## Uso

### Dashboard web

```bash
python3 dashboard/server.py [porta]    # predeterminado: 8091
```

Accede a `http://localhost:8091` — la primera vez que abras el navegador, se te pedirá la contraseña.

#### Autenticación

Todos los dashboards (el master `:8090` y cada canal `:809N`) requieren contraseña para acceder.

- **Contraseña predeterminada:** `Inema2026$$$`
- **Configurada en:** `config/.env` → variable `DASHBOARD_PASSWORD`
- **La cookie de sesión** (`ds`) dura 30 días; vence si se reinicia el servicio

**Cambiar la contraseña** (con curl o desde las herramientas de desarrollo del navegador):
```bash
curl -X POST http://localhost:8091/api/config/password \
  -b "ds=SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"current":"Inema2026$$$","new":"NovaSenha"}'
```

El token `ds` aparece en las cookies del navegador después de iniciar sesión. El cambio invalida todas las sesiones activas (hay que volver a iniciar sesión).

**Instancias existentes** (creadas antes de esta versión): agrega manualmente a `config/.env`:
```
DASHBOARD_PASSWORD=Inema2026$$$
```
Y reinicia el servicio: `systemctl --user restart yt-dashboard<N>`.

> **TODO (seguridad — revisar):**
> La implementación actual es adecuada para uso interno en una VPS con túnel SSH,
> pero tiene limitaciones que deben evaluarse antes de exponerla en una red abierta:
> - Sin limitación de solicitudes en `/api/login` — vulnerable a ataques de fuerza bruta
> - La contraseña se guarda en texto plano en `.env` (no está cifrada con hash)
> - Sin TLS: la cookie y la contraseña se transmiten en texto claro (riesgo en redes no confiables)
> - Sesiones en memoria: al reiniciar el servicio, todos cierran sesión forzosamente
> - Sin vencimiento por inactividad (solo por el Max-Age de 30 días)
> - Sin 2FA
> Mientras el acceso sea mediante un túnel SSH local, el riesgo es bajo. Si se expone
> públicamente, lo mínimo es configurar un proxy inverso (nginx) con HTTPS.

Panel con:
- Estadísticas con enlaces (total de transmisiones en vivo, recortadas, pendientes, clips en espera, publicados)
- Configuración de horarios (selector visual de 24 h)
- Tabla de transmisiones en vivo con filtro por estado
- Pestaña Clips unificada: publicados + pendientes
- Control de clips: pausar/reanudar la publicación individual
- Volver a procesar transmisiones en vivo con errores
- Control de privacidad
- Configuración de miniaturas
- Estado del scheduler en tiempo real

### Docker

```bash
docker-compose up -d
```

Dashboard en `http://localhost:8091`.

### Systemd (servicios de usuario)

```bash
# Crear enlaces simbólicos (ejemplo para lives5, puerto 8095)
ln -sf /home/nmaldaner/projetos/yt-pub-lives5/systemd/yt-scheduler.service ~/.config/systemd/user/yt-scheduler5.service
ln -sf /home/nmaldaner/projetos/yt-pub-lives5/systemd/yt-dashboard.service ~/.config/systemd/user/yt-dashboard5.service
systemctl --user daemon-reload
systemctl --user enable --now yt-scheduler5 yt-dashboard5
```

### Varias instancias

**Convención recomendada:** el nombre del proyecto GCP debe ser el nombre del canal de destino
(facilita la auditoría: en GCP Console se puede ver qué canal sirve cada proyecto).

| Instancia | Puerto | Scheduler | Dashboard | Canal de destino | GCP Project |
|-----------|-------|-----------|-----------|---------------|-------------|
| lives1 | 8091 | yt-scheduler1 | yt-dashboard1 | INEMA TDS | inema-tds |
| lives2 | 8092 | yt-scheduler2 | yt-dashboard2 | INEMA TIA | inema-tia |
| lives3 | 8093 | yt-scheduler3 | yt-dashboard3 | INEMA TDS | inema-tds-2 |
| lives4 | 8094 | yt-scheduler4 | yt-dashboard4 | INEMA Tec | inema-tec |
| lives5 | 8095 | yt-scheduler5 | yt-dashboard5 | INEMA PROMPTS | inema-prompts |
| lives6 | 8096 | yt-scheduler6 | yt-dashboard6 | INEMA Robot | inema-robot |

**Sincronización de código** (`yt-pub-livesx` es la plantilla de origen):
```bash
./scripts/sync-instances    # Propaga el código de la plantilla a las instancias listadas
```

**Reiniciar todo:**
```bash
systemctl --user restart yt-scheduler{1..6} yt-dashboard{1..6}
```

### Recortar una transmisión en vivo

```bash
yt-clip <video_id>                    # Modo manual (genera el prompt)
yt-clip <video_id> --ai piramyd-api   # Modo automático (Piramyd API)
yt-clip <video_id> --dry-run          # Solo muestra los temas
yt-clip <video_id> --publish          # Recorta y publica
```

### Generar una miniatura

```bash
yt-thumbnail --title "Titulo do clip" --output thumb.jpg
```

### Publicar un video

```bash
yt-publish video.mp4 --title "Titulo" --description "Descricao"
yt-publish video.mp4 --title "Titulo" --description "Desc" --privacy unlisted --tags "ia,dev"
```

## Tecnologías

- **Backend**: Python 3 (stdlib HTTPServer, sin frameworks)
- **Frontend**: HTML/CSS/JS vanilla (single page, sin build)
- **Base de datos**: SQLite local (modo WAL, sin dependencia externa)
- **APIs**: YouTube Data API v3
- **IA**: Piramyd API / Anthropic Claude API / OpenRouter (análisis de temas + miniaturas)
- **Video**: FFmpeg (corte), yt-dlp (descarga)
- **Auth**: OAuth 2.0 con refresh token (AES-GCM encrypted)

## Licencia

Uso interno — INEMA TDS (@inematdsx)
