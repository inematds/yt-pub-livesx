# Import Worker — Especificação de Integração

Qualquer sistema externo (n8n, Make, scripts, outras instâncias) pode entregar clips
para publicação automática no YouTube simplesmente copiando arquivos para a pasta `imports/`.

---

## Estrutura de pastas

```
yt-pub-lives<N>/
  imports/
    nome-do-lote/          ← uma pasta = um lote de publicação
      clip_01_Titulo.mp4
      clip_02_Outro.mp4
      manifest.json        ← opcional, mas recomendado
```

- O nome da pasta vira o **título do lote** no dashboard (pode ser sobrescrito no manifest).
- Cada pasta é processada de forma independente.
- Após processado, a pasta é **removida automaticamente** de `imports/`.

---

## manifest.json — Formato completo

```json
{
  "titulo":     "Nome do lote no dashboard",
  "publish_at": "14:00",
  "privacy":    "public",
  "clips": [
    {
      "filename":    "clip_01_Titulo.mp4",
      "title":       "Título do vídeo no YouTube",
      "description": "Descrição completa do vídeo.",
      "tags":        ["tag1", "tag2", "tag3"]
    },
    {
      "filename":    "clip_02_Outro.mp4",
      "title":       "Segundo vídeo",
      "description": "",
      "tags":        []
    }
  ]
}
```

### Campos do manifest raiz

| Campo        | Tipo   | Obrigatório | Descrição |
|--------------|--------|-------------|-----------|
| `titulo`     | string | não         | Nome do lote no dashboard. Default: nome da pasta |
| `publish_at` | string | não         | Horário HH:MM para publicar (ex: `"14:00"`). Só respeitado se `import_fila_global=false` no config |
| `privacy`    | string | não         | `public` \| `unlisted` \| `private`. Sobrescreve o config global deste lote |
| `clips`      | array  | não         | Lista de metadados por arquivo. Se ausente, usa nomes dos arquivos |

### Campos por clip

| Campo         | Tipo         | Obrigatório | Descrição |
|---------------|--------------|-------------|-----------|
| `filename`    | string       | sim         | Nome exato do arquivo MP4 na mesma pasta |
| `title`       | string       | não         | Título no YouTube. Default: nome do arquivo sem extensão |
| `description` | string       | não         | Descrição. Se vazio e `import_gerar_descricao=true`, IA gera automaticamente |
| `tags`        | array string | não         | Tags do vídeo |

---

## Sem manifest — comportamento padrão

Se não houver `manifest.json`, o sistema:

1. Usa todos os `.mp4` da pasta em ordem alfabética
2. Título = nome do arquivo sem extensão e sem prefixo numérico
   - `clip_01_Como usar n8n.mp4` → `"Como usar n8n"`
   - `03_Tutorial basico.mp4` → `"Tutorial basico"`
3. Descrição = vazia (ou gerada por IA se `import_gerar_descricao=true`)
4. Tags = vazia
5. Privacy = valor global do config

---

## Config do sistema (painel de configuração)

| Chave                    | Valores         | Default   | Descrição |
|--------------------------|-----------------|-----------|-----------|
| `import_auto`            | `true`\|`false` | `false`   | Verificação horária automática da pasta imports/ |
| `import_gerar_descricao` | `true`\|`false` | `false`   | Gerar descrição via IA quando ausente no manifest |
| `import_fila_global`     | `true`\|`false` | `true`    | `true` = entra na fila normal (`pub_horarios`); `false` = respeita `publish_at` do manifest |

---

## Quando os clips são publicados

### Modo fila global (`import_fila_global=true`)

Os clips importados entram na **mesma fila** que os clips cortados das lives.
São publicados conforme o agendamento `pub_horarios` do config.

```
imports/lote/ → processado → fila global → pub_horarios → YouTube
```

### Modo horário próprio (`import_fila_global=false`)

O sistema respeita o `publish_at` definido no `manifest.json`.
Se o horário atual for menor que `publish_at`, o lote aguarda.

```
manifest.json: { "publish_at": "14:00" }
→ clips não publicados antes das 14:00
→ a partir das 14:00: entra na próxima rodada de publicação
```

Se `publish_at` não estiver definido no manifest, o lote também entra na fila global.

---

## Trigger manual via API

```http
POST /api/import/scan
Content-Type: application/json
{}
```

Resposta:
```json
{
  "ok": true,
  "processados": 2,
  "total": 2,
  "detalhes": [
    { "pasta": "lote-01", "video_id": "import_20260404_lote_01", "clips": 5, "ok": true },
    { "pasta": "lote-02", "video_id": "import_20260404_lote_02", "clips": 3, "ok": true }
  ]
}
```

---

## Limpeza via API

```http
POST /api/import/clean
Content-Type: application/json
{ "action": "imports" }
```

| `action`    | O que faz |
|-------------|-----------|
| `imports`   | Remove pastas residuais em `imports/` (não processadas) |
| `clips`     | Remove a pasta `clips/` das lives **totalmente publicadas** |
| `clips_all` | Remove a pasta `clips/` de **todas** as lives (cuidado!) |

---

## Exemplo de integração n8n/Make

1. Gerar MP4 dos clips via pipeline de corte externo
2. Criar `manifest.json` com títulos, descrições e `publish_at`
3. Copiar tudo para `imports/nome-do-lote/` via SSH/SCP ou volume compartilhado
4. Chamar `POST /api/import/scan` para processar imediatamente
   (ou aguardar a verificação horária automática se `import_auto=true`)

---

## CLI direto

```bash
# Processar imports/ manualmente
python3 import_worker.py scan

# Limpar imports/ (residuos)
python3 import_worker.py clean-imports

# Limpar clips/ das lives totalmente publicadas
python3 import_worker.py clean-clips

# Limpar clips/ de todas as lives
python3 import_worker.py clean-clips --all
```

---

## Re-enriquecer imports ja publicados (titulo/descricao genericos)

Se um import cair no fallback (`import_gerar_descricao=false` ou transcricao falhou) e
o video ja foi publicado no YouTube com titulo generico (ex: nome do arquivo, "Mkivideo"),
use `scripts/reenrich_imports.py` pra corrigir depois:

```bash
python3 scripts/reenrich_imports.py
```

O que ele faz:
1. Le a tabela `publicados`, filtra as linhas vindas de import (`live_video_id` comeca com `import_`).
2. Acha o clip mp4 original via `lives/<lote>/clips_manifest.json` (casa pelo indice no
   final do `filename` salvo em `publicados`, ex: `import_20260722_videos_3` -> indice 3).
   Se o mp4 nao existir mais em disco, pula esse video.
3. Transcreve o audio (Groq Whisper, fallback whisper local) e gera titulo+descricao
   via Claude CLI a partir da transcricao real (mesma logica de `_gerar_titulo_descricao_ia`
   do `import_worker.py`). Se a transcricao falhar/for curta demais, cai no fallback
   (titulo do nome do arquivo + descricao gerada so a partir do titulo).
4. Atualiza direto no YouTube (`scheduler.update_video_metadata`, PUT na Data API v3)
   e atualiza `clip_titulo` na tabela `publicados`.

Roda pra **todos** os imports publicados encontrados no banco, sem limite e sem filtro
de "ja enriquecido" — reprocessa mesmo os que ja tem titulo bom, entao rode sob demanda,
nao agendado.
