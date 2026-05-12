# yt-pub-livesx

![YouTube Live Clips — Fabrica de Videos](assets/banner.jpg)

Pipeline automatizado para cortar lives do YouTube em clips por topico e publicar em outro canal.

**Canal de origem** (lives): [INEMA TDS](https://www.youtube.com/@inematdsx) (`UC2QbQDyPKuHk93dwo5iq3Sw`)
**Canal de destino** (clips): [INEMA TIA](https://www.youtube.com/@InemaTIA) (`UCavuQHkxBSAZbzRoOm6Gq4g`)

## Fluxo

```
YouTube (lives do canal origem) → Transcricao → Analise IA → Corte (FFmpeg) → Thumbnail (IA) → Publicacao (canal destino)
```

1. **Sincroniza** lives do canal de origem via YouTube Data API
2. **Baixa transcricao** automatica (legendas do YouTube)
3. **Analisa topicos** com IA (Piramyd/Claude/OpenRouter API)
4. **Corta clips** com FFmpeg baseado nos timestamps
5. **Gera thumbnails** com IA (LLM + gerador de imagem) ou local
6. **Publica clips** no canal de destino com titulo, descricao, tags e thumbnail

## Modos de entrada

Alem do fluxo principal (cortar lives → gerar clips → publicar), o pipeline aceita mais duas fontes de video:

1. **Lives do YouTube** → transcricao → analise IA → corte (FFmpeg) → thumbnail → publicacao
2. **Import de videos** → pasta `imports/` vira fila do pipeline → thumbnail → publicacao
3. **Sync TikTok → YouTube** → scan de canais TikTok → download → fila do pipeline → publicacao no canal YouTube de destino

## Estrutura

```
yt-pub-livesx/
├── config/                    # Configuracao isolada do projeto
│   ├── .env                   # Variaveis de ambiente (nao vai pro git)
│   ├── client_secret.json     # Credenciais OAuth (nao vai pro git)
│   ├── credentials.enc        # Tokens encriptados (nao vai pro git)
│   ├── .encryption_key        # Chave AES-GCM (nao vai pro git)
│   ├── prompt_cortes.txt      # Prompt IA para analise de topicos
│   ├── prompt_pub.txt         # Prompt IA para refinar titulo/descricao
│   └── prompt_thumb.txt       # Prompt IA para gerar thumbnails
├── data/
│   └── lives.db               # Banco SQLite local (nao vai pro git)
├── dashboard/
│   ├── server.py              # Backend API (Python HTTP server)
│   └── index.html             # Frontend SPA (vanilla JS)
├── scripts/
│   ├── yt-auth                # Autenticacao OAuth standalone
│   ├── yt-clip                # Pipeline: transcricao → analise → corte
│   ├── yt-publish             # Upload de video para YouTube
│   ├── yt-thumbnail           # Gera thumbnails com IA
│   ├── setup-db               # Cria banco SQLite (com --import migra do Sheets)
│   └── sync-instances         # Sync codigo para outras instancias
├── systemd/
│   ├── yt-dashboard.service   # Service systemd (porta 8091)
│   └── yt-scheduler.service   # Service systemd scheduler
├── db.py                      # Modulo SQLite (CONFIG, LIVES, PUBLICADOS)
├── scheduler.py               # Scheduler automatico
├── docker-compose.yml         # Docker (porta 8091)
├── Dockerfile
├── requirements.txt
├── setup.sh
└── docs/
    └── SETUP-CANAL-DESTINO.md # Documentacao completa do setup
```

## Requisitos

- Python 3.10+
- ffmpeg
- yt-dlp
- deno (runtime JS para yt-dlp)
- curl
- Pillow (thumbnails)

## Arquitetura: master + canais

O sistema e composto por **1 master-dashboard** + **N instancias** (1 por canal).

```
~/projetos/
├── yt-pub-livesx/              ← TEMPLATE (este repo, sem credenciais)
│   ├── master-dashboard/       ← agrega todas as instancias
│   ├── scripts/setup-system    ← Parte 1: sobe master
│   └── scripts/setup-canal     ← Parte 2: cria instancia nova
│
├── yt-pub-lives1/              ← Canal 1 (copia do template)
│   ├── config/.env             ← credenciais GCP do canal 1
│   ├── config/credentials.enc  ← OAuth tokens do canal 1
│   ├── data/lives.db           ← SQLite isolado
│   └── lives/                  ← videos baixados
│
├── yt-pub-livesx/              ← Canal 2 (idem)
└── yt-pub-lives7/              ← Canal N
```

### O que e compartilhado vs isolado

| Recurso | Master (porta 8090) | Cada canal (porta 809N) |
|---|---|---|
| Codigo (Python/HTML) | proprio (pasta template) | propria copia |
| Banco SQLite | nao usa | `data/lives.db` proprio |
| Credenciais GCP | nao usa | projeto GCP proprio |
| OAuth do canal | nao | `config/credentials.enc` proprio |
| Service systemd | `yt-master-dashboard` | `yt-dashboard<N>` + `yt-scheduler<N>` |
| Atualizacao de codigo | manual no template | via `sync-instances` (opt-in) |

### Servicos systemd (modelo final)

```
yt-master-dashboard           → porta 8090 (agrega todos)
yt-dashboard1 + yt-scheduler1 → porta 8091 (canal 1)
yt-dashboard2 + yt-scheduler2 → porta 8092 (canal 2)
...
yt-dashboardN + yt-schedulerN → porta 809N (canal N)
```

Cada par `dashboard<N>` + `scheduler<N>` e **independente**: se um canal cai, os outros continuam. O master so consome as APIs HTTP de cada dashboard.

### Fluxo de dados (1 canal)

```
YouTube (canal origem)
    ↓ YouTube Data API v3
scheduler<N>  ─→  baixa lives novas (yt-dlp)
    ↓
    transcricao (legendas YouTube)
    ↓
    analise IA (Piramyd/Claude) → topicos + timestamps
    ↓
    corte (FFmpeg)
    ↓
    geracao thumbnail (IA ou local)
    ↓
    upload via OAuth → YouTube (canal destino)
    ↓
data/lives.db  ←  log local + status
    ↑
dashboard<N>   ←  UI de controle (porta 809N)
    ↑
master-dashboard ← agrega todos (porta 8090)
```

### Por que 1 canal = 1 instancia?

- **OAuth do YouTube e por usuario/canal** — nao da pra autenticar 2 canais no mesmo OAuth
- **Cota da YouTube Data API e por projeto GCP** — separar projetos = cotas independentes
- **Isolamento de falhas** — bug ou rate-limit num canal nao afeta os outros
- **Sync de codigo opcional** — `scripts/sync-instances` propaga atualizacoes do template; cada instancia decide se entra

## Instalacao

A instalacao e dividida em **duas partes independentes**:

- **Parte 1 — `setup-system`** (1x por maquina): sobe o master-dashboard
  na porta 8090 e prepara dependencias.
- **Parte 2 — `setup-canal`** (1x por canal, inclusive o primeiro): cria
  uma nova instancia copiando este template.

> Esta pasta (`yt-pub-livesx`) e o **template oficial** — nunca deve
> conter `.env`, credenciais ou dados. Toda nova instancia e copia
> dela.

### Parte 1 — Setup do sistema

```bash
git clone <repo> yt-pub-livesx
cd yt-pub-livesx
./setup.sh                    # equivalente a: ./scripts/setup-system
```

O script:
1. Verifica `python3`, `ffmpeg`, `curl`, `yt-dlp`, `deno`
2. Instala pacotes Python (`cryptography`, `anthropic`)
3. Sobe o **master-dashboard** como systemd user service (`yt-master-dashboard`)
4. Para com erro se a porta 8090 ja estiver em uso

Apos terminar: `http://localhost:8090`

### Parte 2 — Adicionar canal

```bash
./scripts/setup-canal
```

Antes de rodar, tenha em maos:

| Pergunta | Origem | Default |
|---|---|---|
| Nome da instancia | livre (ex: `yt-pub-lives7`) | — |
| Numero da instancia (services) | extraido do nome se terminar em digito | proximo livre |
| Porta do dashboard | livre na maquina | proxima livre 8091+ |
| `YOUTUBE_CHANNEL_ID` (origem) | UC... do canal de onde vem as lives | INEMA TDS |
| Handle do canal de destino | so doc | opcional |
| `CLIENT_ID` / `CLIENT_SECRET` | GCP → OAuth Client ID (Desktop App) | — |
| `API_KEY` | GCP → API Key (YouTube Data API v3) | — |
| `GCP_PROJECT` | id do projeto GCP | — |
| `PIRAMYD_API_KEY` | painel Piramyd | — |
| Senha do dashboard | livre | `Inema2026$$$` |
| Adicionar ao `sync-instances`? | s/N | N |

> **ENTER em qualquer pergunta com `[default]` aceita o default mostrado.**

#### Pre-requisitos no Google Cloud (1 projeto por instancia)

Cada instancia precisa de um **projeto Google Cloud proprio**:

1. Acesse [Google Cloud Console](https://console.cloud.google.com) e crie um projeto (ex: `yt-pub-lives7`)
2. Ative a API: **YouTube Data API v3**
   - Menu: APIs & Services → Library → YouTube Data API v3 → Enable
3. Configure o **OAuth Consent Screen**:
   - Tipo: **External**, modo **Testing**
   - Scopes: `youtube`, `youtube.upload`
   - Test users: adicione o **email da conta dona do canal de destino**
4. Crie credenciais **OAuth 2.0 → Desktop App**:
   - Authorized redirect URIs: `http://localhost:8888`
   - Para re-auth pelo master-dashboard: tambem `http://localhost:8090/api/auth/callback`
   - Anote `CLIENT_ID` e `CLIENT_SECRET`
5. Crie uma **API Key** — anote o valor
6. (Opcional) Verifique o telefone do canal em `youtube.com/verify`
   - Necessario para upload de **thumbnails customizadas**

#### O que o `setup-canal` faz

1. Faz as perguntas acima (ENTER aceita default)
2. Mostra resumo, pede confirmacao (`[S/n]`)
3. `cp -r` deste template para `~/projetos/<nome>/`
4. Limpa `data/`, `lives/`, `.git/` e arquivos sensiveis (`.env`, `credentials.enc`, `.encryption_key`)
5. Gera `config/.env` (chmod 600) com as respostas
6. Patcha service files (porta, paths, dependencia entre dashboard/scheduler)
7. Cria symlinks em `~/.config/systemd/user/yt-dashboard<N>.service` e `yt-scheduler<N>.service`
8. Sobe o **dashboard** e **pausa** para voce rodar OAuth manualmente
9. Apos OAuth: sobe o **scheduler**
10. (Opcional) registra a instancia em `scripts/sync-instances`

URL final: `http://localhost:<porta>` — e ja aparece no master `http://localhost:8090`

### Autenticacao OAuth (passo manual dentro do `setup-canal`)

Quando o `setup-canal` pausa, abra **outro terminal** e rode:

```bash
GWS_CONFIG_DIR=~/projetos/<nome>/config python3 ~/projetos/<nome>/scripts/yt-auth
```

O `yt-auth`:
1. Gera um link de autenticacao do Google
2. Sobe um servidor local em `http://localhost:8888` aguardando callback
3. Voce abre o link no browser e autoriza com a conta do canal de destino
4. O callback salva os tokens encriptados em `config/credentials.enc`

**Troubleshooting OAuth:**
- *"Access blocked"*: clique em **Avancado → Ir para (app) (nao seguro)** (normal em modo Testing)
- *"app has not completed verification"*: a conta nao esta como **test user** — adicione em GCP → OAuth Consent Screen → Test users
- *"Unable to connect localhost:8888"*: o script `yt-auth` ja terminou — rode de novo e abra o link **enquanto ele estiver rodando**
- Varias contas no browser: use **aba anonima** ou adicione `&login_hint=email@gmail.com` ao link

**Re-autenticacao pelo Master Dashboard (porta 8090):**

O master usa `redirect_uri=http://localhost:8090/api/auth/callback`. Esse URI precisa estar **tambem** cadastrado em GCP → Credentials → OAuth Client ID da instancia → Authorized redirect URIs. Sem isso, a re-auth falha mesmo apos autorizar no Google.

### Banco de dados (SQLite local)

Criado automaticamente ao iniciar o scheduler ou dashboard. Para criar manualmente:

```bash
python3 scripts/setup-db                # cria DB vazio
python3 scripts/setup-db --import       # cria DB e importa do Google Sheets (legacy)
```

Banco em `data/lives.db` com tabelas **config**, **lives**, **publicados**.

### Deploy em VPS (Ubuntu/Debian)

Passo-a-passo do zero numa VPS limpa.

#### 1. Pacotes do sistema

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip ffmpeg curl git unzip pipx
pipx install yt-dlp

# Deno (runtime JS usado pelo yt-dlp)
curl -fsSL https://deno.land/install.sh | sh
echo 'export PATH="$HOME/.deno/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

#### 2. Habilitar lingering (services rodam sem login SSH)

```bash
sudo loginctl enable-linger $USER
```

Sem isso, todos os `--user` services param quando voce desconectar do SSH.

#### 3. Clonar e rodar Parte 1

```bash
mkdir -p ~/projetos && cd ~/projetos
git clone https://github.com/inematds/yt-pub-livesx.git
cd yt-pub-livesx
./setup.sh
```

#### 4. Firewall — opcional mas recomendado

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8090/tcp        # master-dashboard
sudo ufw allow 8091:8099/tcp   # range das instancias
sudo ufw enable
```

Para nao expor portas publicamente, mantenha o firewall fechado e use **SSH tunnel** da sua maquina local:

```bash
ssh -L 8090:localhost:8090 -L 8091:localhost:8091 user@vps
```

#### 5. Criar primeiro canal

```bash
./scripts/setup-canal
```

**OAuth numa VPS sem browser:** quando o `setup-canal` pausar, abra **outro terminal SSH com tunnel da porta 8888**:

```bash
ssh -L 8888:localhost:8888 user@vps
# dentro da VPS:
GWS_CONFIG_DIR=~/projetos/yt-pub-lives1/config python3 ~/projetos/yt-pub-lives1/scripts/yt-auth
```

Copie o link gerado, abra no **browser da sua maquina local**, autorize. O callback chega em `localhost:8888` local → via tunnel SSH → cai na VPS e salva os tokens encriptados.

#### 6. Verificar

```bash
systemctl --user list-units --type=service --state=active | grep yt-
journalctl --user -u yt-dashboard1 -f
journalctl --user -u yt-scheduler1 -f
```

#### 7. Backup (essencial)

Salve regularmente **fora da VPS**:

- `config/credentials.enc` — sem isso, precisa refazer OAuth
- `config/.encryption_key` — sem essa chave, `credentials.enc` e inutil
- `data/lives.db` — historico de lives processadas

```bash
tar -czf backup-$(date +%F).tar.gz \
  ~/projetos/yt-pub-lives*/config/.env \
  ~/projetos/yt-pub-lives*/config/credentials.enc \
  ~/projetos/yt-pub-lives*/config/.encryption_key \
  ~/projetos/yt-pub-lives*/data/lives.db
```

#### Recursos minimos da VPS

| Recurso | Minimo | Recomendado |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU (FFmpeg corta video) |
| RAM | 2 GB | 4 GB |
| Disco | 20 GB | 50+ GB (videos baixados ficam em `lives/`) |
| Banda | 1 TB/mes | depende de quantos canais |
| OS | Ubuntu 22.04+ / Debian 12+ | — |

> **Disco:** videos brutos ficam em `lives/` ate o pipeline cortar e publicar. Configure cleanup periodico ou o disco enche:
> ```bash
> find ~/projetos/yt-pub-lives*/lives -mtime +7 -delete
> ```

### Prompts de IA (opcional)

Copie os prompts personalizados para `config/`:
```bash
cp ~/caminho/prompt_cortes.txt config/
cp ~/caminho/prompt_pub.txt config/
cp ~/caminho/prompt_thumb.txt config/
```

Ou edite pelo dashboard na aba de configuracao.

## Uso

### Dashboard Web

```bash
python3 dashboard/server.py [porta]    # padrao: 8091
```

Acesse `http://localhost:8091` — ao abrir o browser pela primeira vez sera pedida a senha.

#### Autenticacao

Todos os dashboards (master `:8090` e cada canal `:809N`) exigem senha para acesso.

- **Senha default:** `Inema2026$$$`
- **Configurada em:** `config/.env` → variavel `DASHBOARD_PASSWORD`
- **Cookie de sessao** (`ds`) dura 30 dias; expira se o service reiniciar

**Trocar a senha** (via curl ou pelo devtools do browser):
```bash
curl -X POST http://localhost:8091/api/config/password \
  -b "ds=SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"current":"Inema2026$$$","new":"NovaSenha"}'
```

O token `ds` aparece nos cookies do browser apos o login. A troca invalida todas as sessoes ativas (force re-login).

**Instancias existentes** (criadas antes desta versao): adicionar manualmente ao `config/.env`:
```
DASHBOARD_PASSWORD=Inema2026$$$
```
E reiniciar o service: `systemctl --user restart yt-dashboard<N>`.

> **TODO (seguranca — revisar):**
> A implementacao atual e adequada para uso interno em VPS com SSH tunnel,
> mas tem limitacoes que devem ser avaliadas antes de expor em rede aberta:
> - Sem rate limiting em `/api/login` — suscetivel a brute force
> - Senha gravada em texto puro no `.env` (nao e hash)
> - Sem TLS: cookie e senha trafegam em claro (risco em redes nao confiaveis)
> - Sessions em memoria: restart do service = logout forcado de todos
> - Sem expiracao por inatividade (so pelo Max-Age de 30 dias)
> - Sem 2FA
> Enquanto o acesso for via SSH tunnel local, o risco e baixo. Se expor
> publicamente, o minimo e colocar um reverse proxy (nginx) com HTTPS.

Painel com:
- Stats clicaveis (total lives, cortadas, pendentes, clips aguardando, publicados)
- Configuracao de horarios (picker visual 24h)
- Tabela de lives com filtro por status
- Aba Clips unificada: publicados + pendentes
- Controle de clips: pausar/retomar publicacao individual
- Reprocessar lives com erro
- Controle de privacy
- Configuracao de thumbnails
- Status do scheduler em tempo real

### Docker

```bash
docker-compose up -d
```

Dashboard em `http://localhost:8091`.

### Systemd (user services)

```bash
# Criar symlinks (exemplo para lives5, porta 8095)
ln -sf /home/nmaldaner/projetos/yt-pub-lives5/systemd/yt-scheduler.service ~/.config/systemd/user/yt-scheduler5.service
ln -sf /home/nmaldaner/projetos/yt-pub-lives5/systemd/yt-dashboard.service ~/.config/systemd/user/yt-dashboard5.service
systemctl --user daemon-reload
systemctl --user enable --now yt-scheduler5 yt-dashboard5
```

### Multi-instancia

**Convencao recomendada:** nome do projeto GCP = nome do canal de destino
(facilita auditoria — voce vê na GCP Console qual canal cada projeto serve).

| Instancia | Porta | Scheduler | Dashboard | Canal Destino | GCP Project |
|-----------|-------|-----------|-----------|---------------|-------------|
| lives1 | 8091 | yt-scheduler1 | yt-dashboard1 | INEMA TDS | inema-tds |
| lives2 | 8092 | yt-scheduler2 | yt-dashboard2 | INEMA TIA | inema-tia |
| lives3 | 8093 | yt-scheduler3 | yt-dashboard3 | INEMA TDS | inema-tds-2 |
| lives4 | 8094 | yt-scheduler4 | yt-dashboard4 | INEMA Tec | inema-tec |
| lives5 | 8095 | yt-scheduler5 | yt-dashboard5 | INEMA PROMPTS | inema-prompts |
| lives6 | 8096 | yt-scheduler6 | yt-dashboard6 | INEMA Robot | inema-robot |

**Sync codigo** (`yt-pub-livesx` e o template fonte):
```bash
./scripts/sync-instances    # Propaga codigo do template para as instancias listadas
```

**Restart todos:**
```bash
systemctl --user restart yt-scheduler{1..6} yt-dashboard{1..6}
```

### Cortar uma Live

```bash
yt-clip <video_id>                    # Modo manual (gera prompt)
yt-clip <video_id> --ai piramyd-api   # Modo automatico (Piramyd API)
yt-clip <video_id> --dry-run          # So mostra topicos
yt-clip <video_id> --publish          # Corta e publica
```

### Gerar Thumbnail

```bash
yt-thumbnail --title "Titulo do clip" --output thumb.jpg
```

### Publicar um Video

```bash
yt-publish video.mp4 --title "Titulo" --description "Descricao"
yt-publish video.mp4 --title "Titulo" --description "Desc" --privacy unlisted --tags "ia,dev"
```

## Tecnologias

- **Backend**: Python 3 (stdlib HTTPServer, sem frameworks)
- **Frontend**: HTML/CSS/JS vanilla (single page, sem build)
- **Banco**: SQLite local (WAL mode, sem dependencia externa)
- **APIs**: YouTube Data API v3
- **IA**: Piramyd API / Anthropic Claude API / OpenRouter (analise de topicos + thumbnails)
- **Video**: FFmpeg (corte), yt-dlp (download)
- **Auth**: OAuth 2.0 com refresh token (AES-GCM encrypted)

## Licenca

Uso interno — INEMA TDS (@inematdsx)
