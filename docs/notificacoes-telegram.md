# Notificacao Telegram de publicacoes

## Como funciona

Configuracao **por canal**, deteccao e envio **na master**.

- **Canal (instancia):** duas chaves na tabela `config`, editaveis na aba
  *Config > Horarios* do dashboard:
  - `telegram_notify_enabled` — `true` / `false`
  - `telegram_notify_chat_id` — um ou mais IDs separados por virgula
    (grupo comeca com `-`)
- **Master (`master-dashboard/notifier.py`):** duas threads, iniciadas pelo
  `main()` do `server.py`.
  - `notify_loop()` — a cada 60s le o `data/lives.db` de cada instancia,
    procura publicacoes novas e manda a mensagem.
  - `updates_loop()` — long-poll do `getUpdates`, trata o clique no botao
    "Enviar o video" e faz upload do MP4 do disco.

Nada muda no `scheduler.py`: a master ja le o banco e o `.env` de cada
instancia direto do disco.

## Bot dedicado (obrigatorio)

`TELEGRAM_NOTIFY_BOT_TOKEN` no `config/.env` da master. **Nao reutilizar o
`TELEGRAM_BOT_TOKEN` do monitor de saude**: dois processos fazendo
`getUpdates` no mesmo token dao `409 Conflict` e um derruba o outro.

Sem essa variavel o notifier simplesmente nao sobe (o resto da master segue
normal).

O destinatario precisa dar `/start` no bot uma vez, senao o Telegram recusa a
mensagem com 403.

## Cadastro pelo proprio bot

O usuario manda `meu canal e o livesN` para o bot. O `updates_loop` reconhece o
canal, adiciona o chat_id na lista daquele canal e liga
`telegram_notify_enabled`. Idempotente: repetir nao duplica.

**Interruptor:** so funciona com o cadastro aberto — flag `enroll_open` no
`notify_state.json`. Fechado, o bot responde "cadastro fechado" e nao grava
nada. Abrir / fechar:

```bash
cd <master>/master-dashboard
python3 -c "import notifier,json; notifier.init([], print); notifier.set_enroll(True)"
systemctl --user restart yt-master-dashboard   # recarrega o estado
```

Enquanto estiver aberto, **qualquer pessoa que descubra o bot consegue se
inscrever num canal e pedir o MP4**. Fechar assim que terminar o cadastro da
turma.

## Envio por fora do notifier — nao faca

Mandar link/video pro bot com script proprio (curl, sendMessage cru) produz
mensagem **sem o botao**, porque o botao e o `reply_markup` que so o notifier
monta. Ja aconteceu: em 14/08 um script do openpcbot mandou 33 links e todos
chegaram sem botao.

Qualquer disparo manual deve usar `notifier.send_message(chat, texto, markup)`
com o `callback_data` no formato `v:<instance_id>:<row_id>`, ou
`notifier._handle_send_video(chat, instance_id, row_id)` pra mandar o MP4
direto.

Publicacoes fora da janela de 48h nunca sao reenviadas pelo loop — para essas,
o disparo manual e o unico caminho.

## Deteccao

Janela deslizante de 48h sobre a tabela `publicados`, usando o mesmo filtro de
"publicado com sucesso" do `get_db_stats` (exclui `''`, `erro%`, `moved_%`,
`publicando`).

O estado fica em `master-dashboard/notify_state.json` (fora do git) como um
**conjunto de row ids ja avisados por instancia**, podado pela janela.

Nao e um "maior id visto": os tres caminhos de publicacao (clips, imports,
tiktok) rodam em locks separados e inserem a linha como `publicando` antes de
concluir, entao a row 99 pode virar OK depois da 100. Um watermark simples
pularia a 99 para sempre.

**Primeira ativacao de um canal:** o historico dentro da janela e marcado como
visto sem enviar nada. So publicacoes a partir dai geram aviso.

## Envio do video

`publicados.filename` (`<live_video_id>_<index>`) -> `clips_manifest.json` da
live -> `clip['file']`. Ha fallback por nome de arquivo para registros antigos.

Limites tratados com resposta explicativa + link do YouTube:
- arquivo ja apagado pela limpeza de clips;
- video acima de **50 MB** (teto de upload multipart da Bot API).

## Deploy

1. `commit` + `push` neste template (`yt-pub-livesx`).
2. `git pull` em `yt-pub-livesx-master` (onde o service da master roda).
3. `scripts/sync-instances` (leva o `dashboard/index.html` para os canais).
4. `systemctl --user restart yt-master-dashboard` + os `yt-dashboardN`.

`notifier.py` e so da master — nao vai para as instancias.

## Pendente (para outra hora)

- **Cadastro de admin para receber erros.** Hoje so sucesso gera aviso. A ideia
  e um chat_id de admin (global, na master) recebendo falhas: `erro_upload`,
  OAuth expirado por canal, corte que falhou. Ponto natural de entrada: uma
  varredura irma da `_notify_instance` filtrando `clip_video_id LIKE 'erro%'`,
  com o mesmo controle de estado por row id.
- Filtro por tipo (avisar so TikTok, so clip).
- Multiplos chat_ids por canal.
- Envio automatico do video sem clique.
