# Instalação local segura

## 1. Visão geral

Este projeto automatiza um pipeline para transformar lives do YouTube em clips: sincroniza lives do canal de origem, baixa transcrição, usa IA para sugerir tópicos, corta vídeos com FFmpeg, gera thumbnails e publica os clips no canal de destino.

A arquitetura prevista é:

- `master-dashboard`: painel agregador das instâncias, normalmente na porta `8090`.
- `dashboard`: painel de uma instância/canal, normalmente na porta `8091` ou `809N`.
- `scheduler.py`: processo contínuo que executa cortes, publicações, imports, TikTok e enrich conforme configuração.
- `db.py`: banco SQLite local em `data/lives.db`.
- `scripts/`: comandos operacionais para autenticação, corte, publicação, thumbnail, setup e sincronização.

O repositório deve ser tratado como template/fork de código. Credenciais, tokens, banco e vídeos não devem ser versionados.

## 2. Pré-requisitos

Para rodar sem Docker:

- Python 3.10+.
- `pip`.
- `ffmpeg` e `ffprobe`.
- `yt-dlp`.
- `deno`, usado pelo `yt-dlp` com componentes remotos.
- `curl`.
- Dependências Python de `requirements.txt`: `cryptography` e `anthropic`.

Para rodar com Docker:

- Docker.
- Docker Compose.
- Arquivo `config/.env` real, criado localmente a partir de `.env.example`.

No ambiente Windows onde esta documentação foi criada, Docker e Docker Compose estavam disponíveis, mas `python`, `pip`, `ffmpeg`, `yt-dlp` e `deno` não estavam no `PATH`. O runtime Python embutido do Codex existe, mas não tinha `cryptography` instalado.

## 3. Clonar o repositório

```bash
git clone https://github.com/mic-rocha89-cloud/yt-pub-livesx.git
cd yt-pub-livesx
```

Se usar GitHub CLI:

```bash
gh repo clone mic-rocha89-cloud/yt-pub-livesx
cd yt-pub-livesx
```

## 4. Verificar remotes e branch

```bash
git status
git remote -v
git branch
```

O esperado para trabalhar com segurança:

- Branch estável: `main`.
- `origin`: `https://github.com/mic-rocha89-cloud/yt-pub-livesx.git`.
- `upstream`: `https://github.com/inematds/yt-pub-livesx.git`.

Se o `upstream` não existir, configure:

```bash
git remote add upstream https://github.com/inematds/yt-pub-livesx.git
```

## 5. Criar branch segura

Nunca trabalhe direto na `main`. Para documentação e diagnóstico:

```bash
git switch main
git pull origin main
git switch -c docs/install-local
```

## 6. Configurar `.env`

Não sobrescreva `.env` existente. Se ainda não houver `config/.env`, crie a partir do exemplo:

```bash
mkdir -p config
cp .env.example config/.env
chmod 600 config/.env
```

Preencha apenas com valores reais do seu ambiente. Nunca faça commit de `config/.env`.

## 7. Variáveis de ambiente

Obrigatórias para uma instância funcional:

- `CLIENT_ID`: OAuth Client ID do Google Cloud, tipo Desktop App.
- `CLIENT_SECRET`: segredo OAuth do Google Cloud.
- `API_KEY`: chave da YouTube Data API v3.
- `GCP_PROJECT`: ID do projeto Google Cloud da instância.
- `YOUTUBE_CHANNEL_ID`: canal de origem das lives.
- `DASHBOARD_PASSWORD`: senha do dashboard.

Obrigatórias para publicação OAuth:

- `config/.encryption_key`: chave local AES-GCM gerada pelo `scripts/yt-auth`.
- `config/credentials.enc`: tokens OAuth criptografados gerados pelo `scripts/yt-auth`.

Usada por migração legada:

- `SPREADSHEET_ID`: planilha Google usada por `scripts/setup-db --import`.

Opcionais ou dependentes de configuração no dashboard/banco:

- `LIVES_DIR`: diretório dos vídeos processados. No Docker, o padrão é `/data/lives`.
- `PIRAMYD_API_KEY`: IA para análise/thumbnail quando usar Piramyd.
- `ANTHROPIC_API_KEY`: Anthropic API direta quando selecionada.
- `OPENROUTER_API_KEY`: OpenRouter quando selecionado.
- `KIE_API_KEY`: geração de imagem via Kie.ai.
- `MINIMAX_API_KEY`: geração de imagem via MiniMax.
- `GOOGLE_IMAGE_API_KEY` ou `GEMINI_API_KEY`: geração de imagem via Google.
- `GOOGLE_IMAGE_MODEL`: modelo de imagem Google.
- `INEMAIMG_URL`: endpoint local para geração de imagem.
- `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`: alertas do master-dashboard.
- `INSTANCE_NAME`: nome da instância, usado por setup/scheduler.
- `GWS_CONFIG_DIR`: diretório de configuração. Padrão local: `config/`.

Variáveis que precisam ser geradas com segurança:

- `DASHBOARD_PASSWORD`: use senha forte em produção.
- `CLIENT_SECRET`, `API_KEY`, `PIRAMYD_API_KEY` e demais chaves externas.
- `.encryption_key` e `credentials.enc`, criados pelo OAuth.

## 8. Instalar dependências

Sem Docker, em Linux/WSL/VPS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependências de sistema no Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip ffmpeg curl git unzip pipx
pipx install yt-dlp
curl -fsSL https://deno.land/install.sh | sh
```

Com Docker:

```bash
docker compose build
```

O `Dockerfile` instala Python 3.12, FFmpeg, Deno, `yt-dlp`, Pillow e os pacotes de `requirements.txt`.

## 9. Rodar localmente sem Docker

Para iniciar apenas o dashboard de uma instância:

```bash
GWS_CONFIG_DIR="$(pwd)/config" python3 dashboard/server.py 8091
```

Acesse:

```text
http://localhost:8091
```

Para iniciar o master-dashboard:

```bash
GWS_CONFIG_DIR="$(pwd)/config" python3 master-dashboard/server.py
```

Acesse:

```text
http://localhost:8090
```

Para iniciar o scheduler:

```bash
GWS_CONFIG_DIR="$(pwd)/config" LIVES_DIR="$(pwd)/lives" python3 scheduler.py
```

Só rode o scheduler quando `.env`, OAuth e banco local estiverem prontos. Ele pode chamar APIs externas, baixar vídeos, cortar arquivos e publicar conforme configuração.

### Windows: execução local validada

No Windows, use Git Bash para os scripts Bash e garanta que o processo enxergue a venv, o diretório `config` e UTF-8:

```powershell
$env:GWS_CONFIG_DIR = "$PWD\config"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
```

Para iniciar o dashboard:

```powershell
.\.venv\Scripts\python.exe -u dashboard\server.py 8091
```

Para iniciar o master-dashboard:

```powershell
.\.venv\Scripts\python.exe -u master-dashboard\server.py
```

Para testar um vídeo sem publicar:

```powershell
.\.venv\Scripts\python.exe -B -c "import os, subprocess, sys; sys.path.insert(0, os.getcwd()); import db; cfg=db.load_config(); env=os.environ.copy(); env['GWS_CONFIG_DIR']=os.path.join(os.getcwd(),'config'); env['PYTHONUTF8']='1'; env['PYTHONIOENCODING']='utf-8'; env['PATH']=os.path.join(os.getcwd(),'.venv','Scripts') + os.pathsep + env.get('PATH',''); env['OPENROUTER_API_KEY']=cfg.get('openrouter_api_key',''); env['AI_MODEL']=cfg.get('ai_model') or 'anthropic/claude-sonnet-4'; raise SystemExit(subprocess.run([r'C:\Program Files\Git\bin\bash.exe','./scripts/yt-clip','VIDEO_ID','--ai','openrouter-api','--dry-run','--work-dir',os.path.join(os.getcwd(),'lives')], env=env).returncode)"
```

Substitua `VIDEO_ID` pelo ID do vídeo do YouTube. O `--dry-run` baixa/processa a transcrição e salva tópicos, mas não corta nem publica.

Para gerar os cortes locais, remova `--dry-run`. Isso baixa o vídeo e cria arquivos em `lives/<VIDEO_ID>/clips/`, mas ainda não publica se `--publish` não for usado.

Notas importantes do teste real:

- Se a chave OpenRouter estiver salva no dashboard/banco e não no `.env`, carregue-a via `db.load_config()` ao rodar scripts pela linha de comando.
- Use `PYTHONUTF8=1` e `PYTHONIOENCODING=utf-8` para evitar erro de acentos no Windows.
- Sempre publique primeiro como `unlisted`, revise no YouTube Studio e só depois mude para `public`.
- Artefatos gerados em `lives/` e `data/` são locais e não devem ir para o Git.

## 10. Rodar localmente com Docker

Primeiro confirme que existe `config/.env` real. Depois:

```bash
docker compose up --build
```

Serviços criados:

- `dashboard`: porta `8091`, comando `python3 dashboard/server.py 8091`.
- `scheduler`: comando `python3 /app/scheduler.py`.

Volumes:

- `./config:/config`.
- `./lives:/data/lives`.

Dashboard:

```text
http://localhost:8091
```

## 11. Autenticação OAuth

Depois de preencher `config/.env`, rode:

```bash
GWS_CONFIG_DIR="$(pwd)/config" python3 scripts/yt-auth
```

O script abre o fluxo OAuth em `http://localhost:8888`, salva `config/.encryption_key` e `config/credentials.enc`, e testa o acesso ao canal autenticado.

No Google Cloud, cadastre pelo menos:

- `http://localhost:8888`
- `http://localhost:8090/api/auth/callback`, se for usar reautenticação pelo master-dashboard.

## 12. Banco de dados

O banco é SQLite local e fica em:

```text
data/lives.db
```

Criar banco vazio:

```bash
GWS_CONFIG_DIR="$(pwd)/config" python3 scripts/setup-db
```

Importar Google Sheets legado:

```bash
GWS_CONFIG_DIR="$(pwd)/config" python3 scripts/setup-db --import
```

Use `--import` com cuidado: ele adiciona dados e usa OAuth/Google Sheets.

## 13. Rodar em VPS

Fluxo recomendado:

```bash
mkdir -p ~/projetos
cd ~/projetos
git clone https://github.com/mic-rocha89-cloud/yt-pub-livesx.git
cd yt-pub-livesx
./setup.sh
./scripts/setup-canal
```

O `setup.sh` chama `scripts/setup-system` e sobe o master-dashboard como serviço systemd user na porta `8090`.

O `setup-canal` cria uma instância em `~/projetos/<nome>`, gera `config/.env`, cria serviços `yt-dashboard<N>` e `yt-scheduler<N>`, pausa para OAuth e registra a instância no master.

Para serviços user continuarem após logout:

```bash
sudo loginctl enable-linger "$USER"
```

## 14. Portas

- `8090`: master-dashboard.
- `8091` a `8099`: dashboards das instâncias por convenção.
- `8888`: callback local do OAuth em `scripts/yt-auth`.
- Docker expõe `8091:8091`.

## 15. Testar funcionamento

Checks seguros:

```bash
git status
docker compose config
```

Com dependências e `.env` prontos:

```bash
GWS_CONFIG_DIR="$(pwd)/config" python3 dashboard/server.py 8091
```

Depois acesse `http://localhost:8091`, faça login com `DASHBOARD_PASSWORD` e confira `/api/health` pelo dashboard.

No systemd:

```bash
systemctl --user status yt-master-dashboard
systemctl --user status yt-dashboard<N>
systemctl --user status yt-scheduler<N>
journalctl --user -u yt-dashboard<N> -f
journalctl --user -u yt-scheduler<N> -f
```

## 16. Erros comuns

- `python/pip não encontrado`: instalar Python ou usar WSL/Docker.
- `ffmpeg não encontrado`: instalar FFmpeg antes de cortar vídeos.
- `yt-dlp não encontrado`: instalar via `pipx install yt-dlp`.
- `deno não encontrado`: instalar Deno e colocar no `PATH`.
- `config/.env ausente`: copiar `.env.example` para `config/.env` e preencher valores reais.
- `credentials.enc ausente`: rodar `scripts/yt-auth`.
- `porta em uso`: escolher outra porta ou parar o serviço conflitante.
- `OAuth bloqueado`: adicionar usuário de teste no OAuth Consent Screen.
- `redirect_uri_mismatch`: cadastrar exatamente o redirect usado.
- `Docker não lê config.json`: verificar permissões de `~/.docker/config.json`.
- `OPENROUTER_API_KEY não definida` ao rodar `yt-clip`: a chave pode estar salva no banco pelo dashboard, não no `.env`. Para execução manual, carregue a chave via `db.load_config()` ou adicione a variável no ambiente local sem commitar.
- `HTTP 400` no OpenRouter com `AI_MODEL` vazio: configure um modelo válido, por exemplo `anthropic/claude-sonnet-4`, ou deixe o launcher aplicar fallback.
- `UnicodeEncodeError` no Windows: rode com `PYTHONUTF8=1` e `PYTHONIOENCODING=utf-8`.
- Títulos com apóstrofo ou acentos quebrando upload: use a versão corrigida de `scripts/yt-publish`, que monta metadados via JSON seguro.

## 17. Checklist antes de produção

- `main` está limpa e protegida.
- Mudanças foram feitas em branch.
- `.env`, tokens, banco e vídeos não estão no Git.
- Senha do dashboard foi trocada.
- Acesso externo usa HTTPS/reverse proxy ou SSH tunnel.
- Portas públicas foram revisadas.
- OAuth testado no canal correto.
- Backup de `config/.env`, `config/.encryption_key`, `config/credentials.enc` e `data/lives.db`.
- Scheduler testado em instância não produtiva.

## 18. Checklist antes de alterar código

- Criar branch nova.
- Rodar `git status`.
- Listar arquivos que serão alterados.
- Fazer mudança pequena.
- Testar localmente.
- Revisar `git diff`.
- Abrir Pull Request.
- Nunca rodar migrations ou sync em produção sem plano de rollback.

## 19. Fluxo recomendado de branches

```bash
git switch main
git pull origin main
git switch -c feature/nome-pequeno-e-claro
```

Use nomes como:

- `docs/install-local`
- `feature/layout-dashboard`
- `feature/refino-agente-sdr`
- `feature/configuracoes-avancadas-agente`
- `feature/painel-admin-configuravel`
- `feature/seguranca-autenticacao`
- `feature/logs-e-monitoramento`
- `feature/deploy-vps`
- `feature/docker-compose-producao`

## 20. Fluxo recomendado de Pull Request

Antes de abrir PR:

```bash
git status
git diff
```

Commit:

```bash
git add INSTALL_LOCAL.md SAFE_DEVELOPMENT.md
git commit -m "docs: add safe local installation guide"
git push -u origin docs/install-local
```

Abra PR para `main` com:

- Resumo.
- Arquivos alterados.
- Como testar.
- Riscos.
- Próximos passos.

## 21. Sincronizar fork com upstream

```bash
git fetch upstream
git switch main
git pull origin main
git merge upstream/main
git push origin main
```

Se preferir fluxo por PR, crie uma branch:

```bash
git switch -c chore/sync-upstream
git merge upstream/main
git push -u origin chore/sync-upstream
```

Abra PR para revisar antes de atualizar `main`.
