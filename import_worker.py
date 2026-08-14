#!/usr/bin/env python3
"""
import_worker.py — Importa clips de pastas externas para o pipeline de publicacao.

Monitora a pasta `imports/` na raiz do projeto.
Cada subfolder = um lote de clips. Quando detectado:
  1. Cria entrada "virtual" no banco (status_cortes=concluido)
  2. Move os MP4s para lives/<video_id>/clips/
  3. Gera clips_manifest.json
  4. Remove a subfolder de imports/

Configuracoes relevantes no banco (config):
  import_gerar_descricao  true|false  — usar IA para gerar descricao quando ausente
  import_auto             true|false  — ativar verificacao horaria pelo scheduler
"""

import os
import sys
import json
import shutil
import re
import subprocess
import tempfile
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
IMPORTS_DIR  = os.path.join(PROJECT_ROOT, 'imports')
LIVES_DIR    = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
CONFIG_DIR   = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))

import db


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] [import] {msg}', file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(name):
    """Converte nome de pasta para video_id seguro."""
    s = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    return s[:40].strip('_') or 'import'


def _title_from_filename(filename):
    """
    Extrai titulo legivel do nome do arquivo.
    Exemplos:
      clip_01_Como usar n8n.mp4        -> Como usar n8n
      03_Tutorial basico.mp4           -> Tutorial basico
      c0002-pascoa2026_quick_01.mp4    -> Pascoa2026 Quick 01
    """
    name = os.path.splitext(filename)[0]
    # Se tem __ (separador de prefixo), pega so a parte depois
    if '__' in name:
        name = name.split('__', 1)[1]
    # Remove prefixo tipo clip_01_, 03_, c0002-, etc.
    name = re.sub(r'^clip_\d+_', '', name)
    name = re.sub(r'^\d+[_\-\s]+', '', name)
    name = re.sub(r'^[a-z]\d+[_\-]', '', name)  # c0002- style
    # Remove sufixos de data/hora (ex: _20260404_095306)
    name = re.sub(r'_\d{8}_\d{6}$', '', name)
    name = re.sub(r'_\d{8}$', '', name)
    # Substitui separadores por espaco
    name = name.replace('_', ' ').replace('-', ' ')
    # Remove palavras puramente numericas soltas (ex: "01", "02")
    name = re.sub(r'\b\d+\b', '', name)
    # Normaliza espacos e capitaliza
    name = ' '.join(w for w in name.split() if w)
    return name.title() if name else filename


def _gerar_descricao_ia(title):
    """Usa Claude CLI para gerar descricao curta a partir do titulo (fallback sem transcricao)."""
    prompt = (
        f'Gere uma descricao curta (2-3 frases) em portugues para um video de YouTube '
        f'com o titulo: "{title}". Retorne apenas a descricao, sem introducao.'
    )
    try:
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)
        result = subprocess.run(
            ['claude', '-p', '--output-format', 'text', prompt],
            capture_output=True, text=True, timeout=60, env=env
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        log(f'  IA descricao erro: {e}')
    return ''


# ---------------------------------------------------------------------------
# Transcricao (Groq Whisper -> fallback whisper local) + titulo/descricao IA
# ---------------------------------------------------------------------------

TRANSCRIPT_MIN_CHARS = 30  # abaixo disso, transcricao e' considerada inutil (silencio/musica)

_GROQ_KEY_PATHS = [
    '/home/nmaldaner/projetos/openpcbotv2/.env',
    '/home/nmaldaner/projetos/wifi/.env',
]


def _load_groq_key():
    """Le GROQ_API_KEY em runtime de openpcbot/.env ou wifi/.env, sem duplicar/salvar em outro lugar."""
    for path in _GROQ_KEY_PATHS:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('GROQ_API_KEY='):
                        return line.split('=', 1)[1].strip().strip('"').strip("'")
        except Exception:
            continue
    return None


def _ensure_faststart(mp4_path):
    """
    Garante que o moov atom esta no inicio do MP4 (faststart).
    TikTok rejeita videos sem faststart ('video nao esta codificado').
    Instagram aceita ambos, por isso passa la mas falha no TikTok.
    Usa ffmpeg -movflags +faststart com stream copy (sem re-encode).
    """
    import struct
    try:
        with open(mp4_path, 'rb') as f:
            # Le os primeiros atoms para checar se moov ja esta antes de mdat
            atoms = []
            for _ in range(10):
                pos = f.tell()
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size = struct.unpack('>I', hdr[:4])[0]
                name = hdr[4:8].decode('ascii', errors='replace')
                atoms.append(name)
                if name in ('moov', 'mdat'):
                    break
                if size < 8:
                    break
                f.seek(pos + size)
            # Se moov aparece antes de mdat, ja esta ok
            if 'moov' in atoms and ('mdat' not in atoms or atoms.index('moov') < atoms.index('mdat')):
                return
    except Exception:
        pass  # na duvida, re-processa

    tmp = os.path.join(tempfile.gettempdir(), f'faststart_{os.getpid()}_{os.path.basename(mp4_path)}')
    try:
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', mp4_path, '-c', 'copy',
             '-movflags', '+faststart', tmp],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, mp4_path)
            log(f'  faststart aplicado: {os.path.basename(mp4_path)}')
        else:
            log(f'  AVISO: faststart falhou para {os.path.basename(mp4_path)}')
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception as e:
        log(f'  AVISO: faststart erro: {e}')
        if os.path.exists(tmp):
            os.remove(tmp)


def _extract_audio(clip_path, out_wav):
    """Extrai audio mono 16kHz do clip via ffmpeg (formato ideal p/ whisper)."""
    result = subprocess.run(
        ['ffmpeg', '-y', '-i', clip_path, '-vn', '-ac', '1', '-ar', '16000', out_wav],
        capture_output=True, text=True, timeout=120
    )
    return result.returncode == 0 and os.path.exists(out_wav)


def _transcrever_groq(audio_path):
    """Transcreve via Groq Whisper API (rapido, sem custo de GPU local)."""
    import requests
    key = _load_groq_key()
    if not key:
        return None
    with open(audio_path, 'rb') as f:
        resp = requests.post(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {key}'},
            files={'file': (os.path.basename(audio_path), f, 'audio/wav')},
            data={'model': 'whisper-large-v3-turbo', 'language': 'pt', 'response_format': 'text'},
            timeout=90,
        )
    if resp.status_code == 200:
        return resp.text.strip()
    raise RuntimeError(f'Groq HTTP {resp.status_code}: {resp.text[:200]}')


def _transcrever_local(audio_path):
    """Fallback: transcreve com whisper local (openai-whisper CLI), usado se a API Groq falhar."""
    with tempfile.TemporaryDirectory() as out_dir:
        result = subprocess.run(
            ['whisper', audio_path, '--model', 'small', '--language', 'Portuguese',
             '--output_format', 'txt', '--output_dir', out_dir],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            return None
        txt_path = os.path.join(out_dir, os.path.splitext(os.path.basename(audio_path))[0] + '.txt')
        if os.path.exists(txt_path):
            with open(txt_path) as f:
                return f.read().strip()
    return None


def _transcrever_clip(clip_path):
    """
    Transcreve o audio do clip: tenta Groq Whisper (API), com fallback pro whisper local
    se a API falhar (sem chave, erro de rede, rate limit, etc).
    Retorna string vazia se nada funcionar ou o audio nao render transcricao util.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        audio_path = os.path.join(tmp_dir, 'audio.wav')
        if not _extract_audio(clip_path, audio_path):
            log('  transcricao: falha ao extrair audio')
            return ''

        text = None
        try:
            text = _transcrever_groq(audio_path)
        except Exception as e:
            log(f'  transcricao Groq falhou ({e}), tentando whisper local...')

        if not text:
            try:
                text = _transcrever_local(audio_path)
            except Exception as e:
                log(f'  transcricao local falhou: {e}')

        return (text or '').strip()


def _gerar_titulo_descricao_ia(transcript, fallback_title):
    """
    Gera titulo + descricao a partir da transcricao do clip (via Claude CLI).
    Retorna (title, description). Cai pro fallback (titulo do arquivo + descricao
    so-por-titulo) se a IA nao responder um JSON valido.
    """
    prompt = (
        'Baseado na transcricao abaixo de um clip de video, gere um titulo chamativo '
        '(ate 70 caracteres) e uma descricao curta (2-3 frases), ambos em portugues, '
        'para publicacao no YouTube. Responda APENAS com um JSON no formato '
        '{"title": "...", "description": "..."}, sem markdown, sem introducao.\n\n'
        f'Transcricao:\n"""{transcript[:4000]}"""'
    )
    try:
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)
        result = subprocess.run(
            ['claude', '-p', '--output-format', 'text', prompt],
            capture_output=True, text=True, timeout=60, env=env
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout.strip()
            raw = re.sub(r'^```(json)?|```$', '', raw.strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)
            title = (data.get('title') or '').strip()
            description = (data.get('description') or '').strip()
            if title and description:
                return title, description
    except Exception as e:
        log(f'  IA titulo/descricao (transcricao) erro: {e}')
    return None, None


def _collect_mp4s(folder_path, exclude_dir=None):
    """
    Coleta todos os arquivos MP4 dentro de folder_path, recursivamente.
    Retorna lista de (caminho_absoluto, nome_arquivo) ordenada por nome.

    exclude_dir: caminho da pasta de destino, ignorada se estiver dentro de
    folder_path. Antes ignorava-se qualquer diretorio chamado 'clips', o que
    tornava invisivel todo lote cujo payload fica em <lote>/clips/ (layout
    gerado pelo distribute_imports e pela saida de outras instancias).
    """
    skip = os.path.realpath(exclude_dir) if exclude_dir else None
    found = []
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [
            d for d in sorted(dirs)
            if not (skip and os.path.realpath(os.path.join(root, d)) == skip)
        ]
        for f in sorted(files):
            if f.lower().endswith('.mp4'):
                found.append((os.path.join(root, f), f))
    return found


def _find_batches(root, rel='', depth=0):
    """
    Expande uma pasta de imports/ na lista de LOTES reais que ela contem.

    Uma pasta e um lote quando tem MP4 direto ou uma subpasta 'clips'.
    Caso contrario e so um container (ex.: imports/lives/ com uma subpasta por
    dia, ou imports/yt-pub-lives21/lives/<lote>/) e descemos um nivel.
    Sem isso, imports/lives/ viraria UM unico lote juntando dias diferentes.

    Retorna caminhos relativos a root.
    """
    path = os.path.join(root, rel) if rel else root
    if depth > 4:
        return []
    try:
        entries = os.listdir(path)
    except OSError:
        return []

    subdirs = [
        e for e in sorted(entries)
        if not e.startswith('.') and os.path.isdir(os.path.join(path, e))
    ]
    has_mp4 = any(e.lower().endswith('.mp4') for e in entries)

    if has_mp4 or 'clips' in subdirs:
        return [rel]

    batches = []
    for d in subdirs:
        batches.extend(_find_batches(root, os.path.join(rel, d), depth + 1))
    return batches


def _prune_empty_parents(rel):
    """
    Remove containers que ficaram sem nenhum arquivo apos o lote sair de
    imports/. So poda esqueleto de diretorios: se sobrou qualquer arquivo,
    para e deixa como esta.
    """
    parent = os.path.dirname(rel)
    while parent:
        p = os.path.join(IMPORTS_DIR, parent)
        if not os.path.isdir(p):
            return
        if any(files for _, _, files in os.walk(p)):
            return
        shutil.rmtree(p, ignore_errors=True)
        parent = os.path.dirname(parent)


def _build_manifest(clips_dir, folder_path, gerar_descricao):
    """
    Constroi a lista de clips para o manifest.
    Varre folder_path recursivamente em busca de MP4s.
    Usa manifest.json da pasta de import se existir para metadados.
    """
    manifest_src = os.path.join(folder_path, 'manifest.json')
    mp4_files = _collect_mp4s(folder_path, exclude_dir=clips_dir)

    if not mp4_files:
        return []

    # Mapa filename->dados do manifest manual (se existir)
    manual = {}
    if os.path.exists(manifest_src):
        try:
            with open(manifest_src) as f:
                raw = json.load(f)
            # suporta lista de clips ou dict com chave "clips"
            entries = raw if isinstance(raw, list) else raw.get('clips', [])
            for entry in entries:
                fname = os.path.basename(entry.get('file', entry.get('filename', '')))
                manual[fname] = entry
        except Exception as e:
            log(f'  manifest.json invalido: {e}, ignorando')

    clips = []
    for i, (src_path, fname) in enumerate(mp4_files, start=1):
        m = manual.get(fname, {})
        title_manual = m.get('title')
        title = title_manual or _title_from_filename(fname)
        description = m.get('description', '')
        tags = m.get('tags', [])

        if not description and gerar_descricao:
            log(f'  Transcrevendo audio para: {title[:50]}')
            transcript = _transcrever_clip(src_path)

            if transcript and len(transcript) >= TRANSCRIPT_MIN_CHARS:
                ia_title, ia_description = _gerar_titulo_descricao_ia(transcript, title)
                if ia_description:
                    description = ia_description
                    # so troca o titulo se ele nao veio explicito do manifest.json
                    if ia_title and not title_manual:
                        title = ia_title
            else:
                log('  transcricao vazia/curta, usando fallback por titulo')

            if not description:
                log(f'  Gerando descricao IA (fallback por titulo) para: {title[:50]}')
                description = _gerar_descricao_ia(title)

        dest_file = os.path.join(clips_dir, fname)
        clips.append({
            'index':       i,
            '_src_path':   src_path,   # origem real (pode estar em subdir)
            'file':        dest_file,
            'filename':    fname,
            'title':       title,
            'description': description,
            'tags':        tags,
            'duration':    0,
        })

    return clips


# ---------------------------------------------------------------------------
# Core: processar um subfolder
# ---------------------------------------------------------------------------

def _read_folder_meta(folder_path):
    """
    Le metadados opcionais do manifest.json raiz da pasta.
    Campos reconhecidos no nivel do lote:
      publish_at  — horario HH:MM para publicar (ex: "14:00")
                    se ausente, segue o agendamento global (pub_horarios)
      privacy     — public|unlisted|private (sobrescreve config global)
      titulo      — nome do lote para exibir no dashboard
    Retorna dict (pode ser vazio).
    """
    manifest_src = os.path.join(folder_path, 'manifest.json')
    if not os.path.exists(manifest_src):
        return {}
    try:
        with open(manifest_src) as f:
            data = json.load(f)
        # manifest pode ser lista (clips) ou dict (meta + clips)
        if isinstance(data, dict):
            return {
                'publish_at': data.get('publish_at', ''),
                'privacy':    data.get('privacy', ''),
                'titulo':     data.get('titulo', ''),
            }
    except Exception:
        pass
    return {}


def _process_folder(folder_name, gerar_descricao, import_fila=True):
    """
    Processa um subfolder de imports/.
    import_fila=True  -> entra na fila normal (pub_horarios do config global)
    import_fila=False -> usa publish_at do manifest.json se disponivel

    Retorna (video_id, qtd_clips) em caso de sucesso, ou None em caso de erro.
    """
    folder_path = os.path.join(IMPORTS_DIR, folder_name)
    if not os.path.isdir(folder_path):
        return None

    date_str  = datetime.now().strftime('%Y%m%d')
    base_id   = f'import_{date_str}_{_sanitize(os.path.basename(folder_name))}'

    # video_id do dia ja usado -> sufixa em vez de pular.
    # Pular deixava a pasta presa em imports/ ate virar o dia: todo arquivo que
    # chegasse depois do primeiro import do dia so entrava 24h mais tarde.
    video_id = base_id
    seq = 1
    while db.get_live(video_id):
        seq += 1
        video_id = f'{base_id}_{seq}'
        if seq > 50:
            log(f'  {base_id}: mais de 50 lotes hoje, pulando')
            return None
    if seq > 1:
        log(f'  {base_id} ja existe, usando {video_id}')

    # Le metadados do lote (publish_at, privacy, titulo)
    meta = _read_folder_meta(folder_path)
    titulo_lote = meta.get('titulo') or os.path.basename(folder_name)

    # publish_at: so usado se import_fila=False
    publish_at = '' if import_fila else meta.get('publish_at', '')

    # Prepara diretorio de destino
    job_dir   = os.path.join(LIVES_DIR, video_id)
    clips_dir = os.path.join(job_dir, 'clips')
    os.makedirs(clips_dir, exist_ok=True)

    # Constroi manifest (antes de mover, para ler lista de arquivos)
    clips = _build_manifest(clips_dir, folder_path, gerar_descricao)
    if not clips:
        log(f'  Nenhum MP4 em {folder_name}, pulando')
        shutil.rmtree(job_dir, ignore_errors=True)
        return None

    # Aplica privacy do lote a cada clip (se especificado)
    if meta.get('privacy'):
        for clip in clips:
            clip['privacy'] = meta['privacy']

    # Move MP4s (usa _src_path para suportar arquivos em subdiretorios)
    # Aplica faststart (moov atom no inicio) para compatibilidade com TikTok
    for clip in clips:
        src = clip.pop('_src_path', os.path.join(folder_path, clip['filename']))
        dst = clip['file']
        if os.path.exists(src):
            shutil.move(src, dst)
            # Garante faststart: TikTok rejeita MP4 com moov atom no final
            _ensure_faststart(dst)
            log(f'  Movido: {clip["filename"]}')

    # Escreve clips_manifest.json
    manifest_path = os.path.join(job_dir, 'clips_manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(clips, f, ensure_ascii=False, indent=2)

    # Observacao inclui publish_at se definido
    obs_parts = [f'importado de {folder_name}']
    if publish_at:
        obs_parts.append(f'publish_at={publish_at}')
    if meta.get('privacy'):
        obs_parts.append(f'privacy={meta["privacy"]}')

    # Insere no banco
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    db.add_lives([{
        'video_id':            video_id,
        'titulo':              titulo_lote,
        'data_live':           now[:10],
        'duracao_min':         '0',
        'url':                 '',
        'status_transcricao':  'concluido',
        'status_cortes':       'concluido',
        'qtd_clips':           str(len(clips)),
        'clips_disco':         str(len(clips)),
        'clips_publicados':    '0',
        'clips_pendentes':     str(len(clips)),
        'data_sync':           now,
        'observacoes':         ' | '.join(obs_parts),
        'data_corte':          now,
    }])

    log(f'  Criado {video_id}: {len(clips)} clips | publish_at={publish_at or "fila_global"} | privacy={meta.get("privacy") or "config_global"}')

    # Remove subfolder de imports/ (e os containers que ficarem vazios)
    shutil.rmtree(folder_path, ignore_errors=True)
    _prune_empty_parents(folder_name)

    return video_id, len(clips)


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

def process_imports(config=None):
    """
    Varre imports/ e processa todos os subfolders novos.
    Retorna lista de dicts com resultado por pasta.
    """
    if not os.path.isdir(IMPORTS_DIR):
        os.makedirs(IMPORTS_DIR, exist_ok=True)
        log('Pasta imports/ criada (vazia)')
        return []

    if config is None:
        config = db.load_config()

    gerar_descricao = config.get('import_gerar_descricao', 'false') == 'true'
    # import_fila_global=true  -> ignora publish_at do manifest, entra na fila normal
    # import_fila_global=false -> respeita publish_at do manifest se definido
    import_fila = config.get('import_fila_global', 'true') == 'true'

    tops = [
        f for f in sorted(os.listdir(IMPORTS_DIR))
        if os.path.isdir(os.path.join(IMPORTS_DIR, f))
        and not f.startswith('.')
    ]

    # Cada pasta de topo pode conter varios lotes aninhados
    folders = []
    for top in tops:
        found = _find_batches(IMPORTS_DIR, top)
        if not found:
            log(f'  {top}: sem MP4, ignorando')
        folders.extend(found)

    if not folders:
        log('imports/: nenhuma pasta nova encontrada')
        return []

    log(f'imports/: {len(folders)} pasta(s) encontrada(s)')
    results = []
    for folder_name in sorted(folders):
        log(f'  Processando: {folder_name}')
        try:
            res = _process_folder(folder_name, gerar_descricao, import_fila)
            if res:
                video_id, qtd = res
                results.append({'pasta': folder_name, 'video_id': video_id, 'clips': qtd, 'ok': True})
            else:
                results.append({'pasta': folder_name, 'ok': False, 'motivo': 'sem clips ou ja existente'})
        except Exception as e:
            log(f'  ERRO ao processar {folder_name}: {e}')
            results.append({'pasta': folder_name, 'ok': False, 'motivo': str(e)})

    return results


def clean_imports():
    """
    Remove todo o conteudo de imports/ (pastas nao processadas ou residuos).
    Retorna quantidade de itens removidos.
    """
    if not os.path.isdir(IMPORTS_DIR):
        return 0
    items = [f for f in os.listdir(IMPORTS_DIR) if not f.startswith('.')]
    for item in items:
        path = os.path.join(IMPORTS_DIR, item)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    log(f'clean_imports: {len(items)} item(s) removidos')
    return len(items)


def clean_clips(only_fully_published=True):
    """
    Remove a pasta clips/ das lives que ja tiveram todos os clips publicados.
    Se only_fully_published=False, remove clips/ de TODAS as lives (uso com cuidado).
    Retorna quantidade de lives limpas.
    """
    lives = db.get_lives()
    cleaned = 0
    for live in lives:
        vid = live.get('video_id', '')
        if not vid:
            continue

        qtd      = int(live.get('qtd_clips', '0') or '0')
        pub      = int(live.get('clips_publicados', '0') or '0')
        is_done  = qtd > 0 and pub >= qtd

        if not only_fully_published or is_done:
            clips_path = os.path.join(LIVES_DIR, vid, 'clips')
            if os.path.isdir(clips_path) and os.listdir(clips_path):
                shutil.rmtree(clips_path)
                os.makedirs(clips_path)  # recria vazia para nao quebrar checagens
                log(f'  Clips limpos: {vid}')
                cleaned += 1

    log(f'clean_clips: {cleaned} live(s) limpas')
    return cleaned


# ---------------------------------------------------------------------------
# Distribuicao entre instancias
# ---------------------------------------------------------------------------

# Raiz onde ficam todas as instancias (ex: /home/nmaldaner/projetos/)
_INSTANCES_BASE = os.path.dirname(PROJECT_ROOT)
_INSTANCE_NAMES = [f'yt-pub-lives{i}' for i in range(1, 10)]

# Pasta central de distribuicao (fora das instancias)
DIST_IMPORTS_DIR = '/home/nmaldaner/projetos/yt-pub-lives/imports'


def _collect_all_mp4s_flat(source_dir=None):
    """
    Coleta todos os MP4s dentro de source_dir recursivamente (exceto .hidden).
    Retorna lista de caminhos absolutos ordenados.
    """
    src = source_dir or IMPORTS_DIR
    found = []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted([d for d in dirs if not d.startswith('.')])
        for f in sorted(files):
            if f.lower().endswith('.mp4'):
                found.append(os.path.join(root, f))
    return found


def distribute_imports(config=None):
    """
    Le MP4s de DIST_IMPORTS_DIR (/home/nmaldaner/projetos/yt-pub-lives/imports/)
    e distribui round-robin para imports/dist_TIMESTAMP/ de cada uma das 7 instancias.
    Cada instancia processa na sua hora (scheduler import_auto ou scan manual).
    Retorna dict: { total, source, por_instancia: [{instancia, clips, ok}] }
    """
    if not os.path.isdir(DIST_IMPORTS_DIR):
        os.makedirs(DIST_IMPORTS_DIR, exist_ok=True)
        return {'total': 0, 'source': DIST_IMPORTS_DIR, 'por_instancia': []}

    all_mp4s = _collect_all_mp4s_flat(DIST_IMPORTS_DIR)
    if not all_mp4s:
        log(f'distribute_imports: nenhum MP4 encontrado em {DIST_IMPORTS_DIR}')
        return {'total': 0, 'source': DIST_IMPORTS_DIR, 'por_instancia': []}

    # Filtra instancias que existem no disco
    instances = [
        os.path.join(_INSTANCES_BASE, name)
        for name in _INSTANCE_NAMES
        if os.path.isdir(os.path.join(_INSTANCES_BASE, name))
    ]
    n = len(instances)
    log(f'distribute_imports: {len(all_mp4s)} MP4(s) -> {n} instancias')

    # Round-robin: video i vai para instances[i % n]
    buckets = [[] for _ in range(n)]
    for i, mp4 in enumerate(all_mp4s):
        buckets[i % n].append(mp4)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    results = []

    for inst_dir, bucket in zip(instances, buckets):
        inst_name = os.path.basename(inst_dir)
        if not bucket:
            results.append({'instancia': inst_name, 'clips': 0, 'ok': True})
            continue

        dist_folder = os.path.join(inst_dir, 'imports', f'dist_{timestamp}')
        os.makedirs(dist_folder, exist_ok=True)

        moved = []
        for src in bucket:
            fname = os.path.basename(src)
            dst = os.path.join(dist_folder, fname)
            counter = 1
            base, ext = os.path.splitext(fname)
            while os.path.exists(dst):
                dst = os.path.join(dist_folder, f'{base}_{counter}{ext}')
                counter += 1
            try:
                shutil.move(src, dst)
                moved.append(fname)
                log(f'  -> {inst_name}: {fname}')
            except Exception as e:
                log(f'  ERRO ao mover {fname} para {inst_name}: {e}')

        results.append({'instancia': inst_name, 'clips': len(moved), 'ok': True})

    # Remove diretorios vazios que sobraram em imports/
    for root, dirs, files in os.walk(IMPORTS_DIR, topdown=False):
        if root != IMPORTS_DIR and not os.listdir(root):
            try:
                os.rmdir(root)
            except OSError:
                pass

    return {'total': len(all_mp4s), 'source': DIST_IMPORTS_DIR, 'por_instancia': results}


# ---------------------------------------------------------------------------
# Execucao direta (teste / CLI)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'scan'
    if action == 'scan':
        results = process_imports()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif action == 'distribute':
        results = distribute_imports()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif action == 'clean-imports':
        n = clean_imports()
        print(f'{n} itens removidos de imports/')
    elif action == 'clean-clips':
        n = clean_clips(only_fully_published='--all' not in sys.argv)
        print(f'{n} lives com clips limpos')
    else:
        print(f'Uso: {sys.argv[0]} scan | distribute | clean-imports | clean-clips [--all]')
