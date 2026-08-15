#!/usr/bin/env python3
"""
Notificacao Telegram de publicacoes.

Roda dentro do master-dashboard (processo unico) e faz duas coisas:

1. notify_loop()  — a cada 60s le o banco de cada instancia, acha publicacoes
   novas (clip / import / tiktok) e manda a mensagem para o chat_id configurado
   NAQUELA instancia (config `telegram_notify_chat_id`).
2. updates_loop() — long-poll do getUpdates do bot dedicado, tratando o clique
   no botao "Receber o video" (envia o MP4 do disco).

Por que na master e nao no scheduler de cada canal:
- um token de bot so pode ser pollado por UM processo (senao 409 Conflict);
- a master ja le o data/lives.db e o config/.env de cada instancia direto do
  disco, entao nao precisa de nenhuma mudanca no scheduler.

Token: TELEGRAM_NOTIFY_BOT_TOKEN (bot dedicado). Sem fallback proposital —
usar o mesmo token do monitor causaria conflito de polling.
"""

import html
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, 'notify_state.json')

# Janela de varredura: publicacoes mais antigas que isso nunca sao notificadas.
WINDOW_HOURS = 48
NOTIFY_INTERVAL = 60          # segundos entre varreduras
LONGPOLL_TIMEOUT = 30         # segundos de long-poll no getUpdates
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # teto de upload multipart da Bot API

API = 'https://api.telegram.org/bot{token}/{method}'

# Mesmo filtro de "publicado com sucesso" usado pelo get_db_stats do master.
_OK_FILTER = """
    clip_video_id IS NOT NULL
    AND clip_video_id != ''
    AND clip_video_id NOT LIKE 'erro%'
    AND clip_video_id NOT LIKE 'moved_%'
    AND clip_video_id != 'publicando'
"""

_state_lock = threading.Lock()
_state = None

# Injetados por init()
_instances = []
_log = print


def init(instances, log_fn):
    global _instances, _log
    _instances = instances
    _log = log_fn


def token():
    return os.environ.get('TELEGRAM_NOTIFY_BOT_TOKEN', '')


def enabled():
    return bool(token())


# --------------------------------------------------------------- estado

def _load_state():
    global _state
    if _state is not None:
        return _state
    try:
        with open(STATE_FILE) as f:
            _state = json.load(f)
    except Exception:
        _state = {}
    _state.setdefault('offset', 0)
    _state.setdefault('instances', {})
    return _state


def _save_state():
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        _log(f'[notifier] falha ao salvar estado: {e}')


def _inst_state(name):
    st = _load_state()
    return st['instances'].setdefault(name, {'notified': {}})


def _record_sent(resp, chat_id, kind, ref=''):
    """Guarda o message_id do que o bot mandou, pra permitir apagar depois."""
    try:
        mid = (resp or {}).get('result', {}).get('message_id')
    except Exception:
        mid = None
    if not mid:
        return
    with _state_lock:
        st = _load_state()
        sent = st.setdefault('sent', [])
        sent.append({'chat': str(chat_id), 'mid': mid, 'kind': kind,
                     'ref': ref, 'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
        del sent[:-500]
        _save_state()


def delete_message(chat_id, message_id):
    try:
        _api_call('deleteMessage', {'chat_id': str(chat_id), 'message_id': message_id})
        return True
    except Exception as e:
        _log(f'[notifier] deleteMessage {chat_id}/{message_id}: {e}')
        return False


# --------------------------------------------------------- telegram API

def _api_call(method, params):
    url = API.format(token=token(), method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=LONGPOLL_TIMEOUT + 15) as r:
        return json.loads(r.read())


def send_message(chat_id, text, reply_markup=None):
    params = {
        'chat_id': str(chat_id),
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'false',
    }
    if reply_markup:
        params['reply_markup'] = json.dumps(reply_markup)
    try:
        return _api_call('sendMessage', params)
    except Exception as e:
        _log(f'[notifier] sendMessage falhou (chat {chat_id}): {e}')
        return None


def answer_callback(callback_id, text=''):
    try:
        _api_call('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})
    except Exception as e:
        _log(f'[notifier] answerCallbackQuery falhou: {e}')


def send_video(chat_id, file_path, caption='', filename=None):
    """Upload multipart do MP4. `filename` sobrescreve o nome mostrado."""
    boundary = uuid.uuid4().hex
    fields = {'chat_id': str(chat_id), 'caption': caption,
              'parse_mode': 'HTML', 'supports_streaming': 'true'}
    body = bytearray()
    for k, v in fields.items():
        body += f'--{boundary}\r\n'.encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += f'{v}\r\n'.encode()

    fname = filename or os.path.basename(file_path)
    ctype = mimetypes.guess_type(fname)[0] or 'video/mp4'
    body += f'--{boundary}\r\n'.encode()
    body += (f'Content-Disposition: form-data; name="video"; filename="{fname}"\r\n'
             f'Content-Type: {ctype}\r\n\r\n').encode()
    with open(file_path, 'rb') as f:
        body += f.read()
    body += f'\r\n--{boundary}--\r\n'.encode()

    url = API.format(token=token(), method='sendVideo')
    req = urllib.request.Request(url, data=bytes(body))
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


# ------------------------------------------------------- leitura do canal

def _short_name(inst):
    """'yt-pub-lives10' -> 'lives10'."""
    return (inst.get('name') or '').replace('yt-pub-', '')


def _instance_by_id(inst_id):
    for i in _instances:
        if str(i.get('id')) == str(inst_id):
            return i
    return None


def _db_path(inst):
    return os.path.join(inst['path'], 'data', 'lives.db')


def _read_config(inst):
    """Le a tabela config da instancia."""
    path = _db_path(inst)
    if not os.path.exists(path):
        return {}
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        rows = conn.execute('SELECT chave, valor FROM config').fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        _log(f'[notifier] {inst["name"]}: falha lendo config: {e}')
        return {}


def _lives_dir(inst):
    """LIVES_DIR da instancia (mesmo padrao do GOOGLE_EMAIL em check_instance)."""
    env_path = os.path.join(inst['path'], 'config', '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('LIVES_DIR='):
                        val = line.split('=', 1)[1].strip()
                        if val:
                            return val
        except Exception:
            pass
    return os.path.join(inst['path'], 'lives')


def _tipo(live_video_id, live_titulo):
    if (live_video_id or '').startswith('import_'):
        if (live_titulo or '').startswith('TikTok @'):
            return 'tiktok'
        return 'import'
    return 'clip'


_TIPO_LABEL = {'clip': ('🎬', 'Clip'), 'import': ('📥', 'Import'), 'tiktok': ('🎵', 'TikTok')}


def _recent_publicados(inst, since):
    """Publicacoes OK dentro da janela."""
    path = _db_path(inst)
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT p.id, p.clip_video_id, p.clip_titulo, p.clip_url, p.filename,
                   p.live_video_id, p.data_publicacao, l.titulo AS live_titulo
            FROM publicados p
            LEFT JOIN lives l ON p.live_video_id = l.video_id
            WHERE p.data_publicacao >= ? AND {_OK_FILTER}
            ORDER BY p.id ASC
        """, (since,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _log(f'[notifier] {inst["name"]}: falha lendo publicados: {e}')
        return []


def _find_row(inst, row_id):
    path = _db_path(inst)
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT p.id, p.clip_video_id, p.clip_titulo, p.clip_url, p.filename,
                   p.live_video_id, l.titulo AS live_titulo
            FROM publicados p
            LEFT JOIN lives l ON p.live_video_id = l.video_id
            WHERE p.id = ?
        """, (row_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        _log(f'[notifier] {inst["name"]}: falha lendo row {row_id}: {e}')
        return None


def _resolve_clip_file(inst, live_video_id, filename):
    """Caminho do MP4 a partir de publicados.filename.

    Formato normal: '<live_video_id>_<index>' -> lookup no clips_manifest.json.
    Registros antigos guardam o nome do arquivo direto -> fallback por basename.
    """
    if not filename or not live_video_id:
        return None

    job_dir = os.path.join(_lives_dir(inst), live_video_id)
    clips = []
    manifest = os.path.join(job_dir, 'clips_manifest.json')
    if os.path.exists(manifest):
        try:
            with open(manifest) as f:
                clips = json.load(f)
        except Exception:
            clips = []

    def _abs(fpath):
        if not fpath:
            return None
        return fpath if os.path.isabs(fpath) else os.path.join(job_dir, fpath)

    # 1. por index
    if filename.startswith(live_video_id + '_'):
        try:
            index = int(filename[len(live_video_id) + 1:])
        except ValueError:
            index = None
        if index is not None:
            for clip in clips:
                if clip.get('index') == index:
                    return _abs(clip.get('file', ''))

    # 2. por nome de arquivo (registros antigos)
    base = os.path.basename(filename)
    for clip in clips:
        if os.path.basename(clip.get('file', '')) == base:
            return _abs(clip.get('file', ''))

    direct = os.path.join(job_dir, 'clips', base)
    if os.path.exists(direct):
        return direct
    return None


# --------------------------------------------------------- loop de aviso

def _chat_ids(cfg):
    """telegram_notify_chat_id aceita varios ids separados por virgula."""
    raw = cfg.get('telegram_notify_chat_id', '') or ''
    return [c.strip() for c in raw.replace(';', ',').split(',') if c.strip()]


def _notify_instance(inst):
    cfg = _read_config(inst)
    if (cfg.get('telegram_notify_enabled', 'false') or 'false').lower() != 'true':
        return
    chats = _chat_ids(cfg)
    if not chats:
        return

    since = (datetime.now() - timedelta(hours=WINDOW_HOURS)).strftime('%Y-%m-%d %H:%M')
    rows = _recent_publicados(inst, since)

    with _state_lock:
        st = _inst_state(inst['name'])
        notified = st['notified']
        # poda ids fora da janela
        for rid in [k for k, v in notified.items() if v < since]:
            notified.pop(rid, None)

        # Primeira ativacao: marca o historico da janela como visto em vez de
        # despejar 48h de publicacoes de uma vez.
        if not st.get('seeded'):
            for r in rows:
                notified[str(r['id'])] = r.get('data_publicacao') or since
            st['seeded'] = True
            _save_state()
            _log(f"[notifier] {inst['name']}: primeira ativacao, {len(rows)} "
                 f"publicacoes antigas marcadas como vistas")
            return

        pending = [r for r in rows if str(r['id']) not in notified]

    for r in pending:
        tipo = _tipo(r.get('live_video_id'), r.get('live_titulo'))
        emoji, label = _TIPO_LABEL[tipo]
        titulo = (r.get('clip_titulo') or '(sem titulo)')[:120]
        url = r.get('clip_url') or f"https://www.youtube.com/watch?v={r.get('clip_video_id')}"

        text = (f"{emoji} <b>{label} publicado</b> — {html.escape(_short_name(inst))}\n\n"
                f"{html.escape(titulo)}\n{url}")
        markup = {'inline_keyboard': [[{
            'text': '📥 Receber o vídeo',
            'callback_data': f"v:{inst['id']}:{r['id']}",
        }]]}
        ok = 0
        for chat_id in chats:
            resp = send_message(chat_id, text, markup)
            if resp and resp.get('ok'):
                ok += 1
                _record_sent(resp, chat_id, 'aviso', f"{inst['name']}:{r['id']}")

        if ok:
            with _state_lock:
                _inst_state(inst['name'])['notified'][str(r['id'])] = r.get('data_publicacao') or since
                _save_state()
            _log(f"[notifier] {inst['name']}: avisado {label} '{titulo[:40]}' "
                 f"({ok}/{len(chats)} destinos)")
        else:
            # nao marca como notificado — tenta de novo no proximo ciclo
            _log(f"[notifier] {inst['name']}: falha ao avisar row {r['id']}")


def notify_loop():
    if not enabled():
        _log('[notifier] TELEGRAM_NOTIFY_BOT_TOKEN nao definido — notificacoes desativadas')
        return
    _log('[notifier] loop de notificacao iniciado')
    while True:
        for inst in _instances:
            try:
                _notify_instance(inst)
            except Exception as e:
                _log(f"[notifier] erro em {inst.get('name')}: {e}")
        time.sleep(NOTIFY_INTERVAL)


# ------------------------------------------------------------- cadastro

def enroll_open():
    with _state_lock:
        return bool(_load_state().get('enroll_open'))


def set_enroll(open_):
    """Abre/fecha o cadastro por ping. Fechado, ninguem novo entra."""
    with _state_lock:
        _load_state()['enroll_open'] = bool(open_)
        _save_state()
    _log(f"[notifier] cadastro por ping {'ABERTO' if open_ else 'FECHADO'}")


def _write_config(inst, chave, valor):
    """Escreve uma chave na tabela config da instancia."""
    conn = sqlite3.connect(_db_path(inst), timeout=10)
    try:
        conn.execute('INSERT OR REPLACE INTO config (chave, valor) VALUES (?, ?)',
                     (chave, str(valor)))
        conn.commit()
    finally:
        conn.close()


def _match_instance(text):
    """'meu canal e o lives10' / 'livesN' / 'lives3x' -> instancia."""
    m = re.search(r'lives\s*0*(\d+)\s*(x?)', (text or '').lower())
    if not m:
        return None
    alvo = f"lives{int(m.group(1))}{m.group(2)}"
    for i in _instances:
        nome = (i.get('name') or '').lower()
        if nome == alvo or nome == f'yt-pub-{alvo}':
            return i
    return None


def _enroll(chat_id, inst):
    """Adiciona o chat na lista do canal e liga a notificacao."""
    cfg = _read_config(inst)
    if not cfg:
        return False, 'Canal sem banco de dados acessivel.'

    chats = _chat_ids(cfg)
    ja = str(chat_id) in chats
    if not ja:
        chats.append(str(chat_id))
        try:
            _write_config(inst, 'telegram_notify_chat_id', ','.join(chats))
        except Exception as e:
            _log(f"[notifier] falha ao cadastrar {chat_id} em {inst['name']}: {e}")
            return False, 'Nao consegui gravar o cadastro. Avisa o admin.'

    if (cfg.get('telegram_notify_enabled', 'false') or '').lower() != 'true':
        try:
            _write_config(inst, 'telegram_notify_enabled', 'true')
        except Exception as e:
            _log(f"[notifier] falha ao ligar notificacao em {inst['name']}: {e}")

    _log(f"[notifier] cadastrado chat {chat_id} em {inst['name']} "
         f"({'ja estava' if ja else 'novo'})")
    nome = html.escape(inst['name'])
    if ja:
        return True, f'Voce ja esta cadastrado no <b>{nome}</b>.'
    return True, (f'✅ Cadastrado no <b>{nome}</b>.\n\n'
                  f'A partir da proxima publicacao voce recebe o aviso com o link, '
                  f'e um botao pra eu te mandar o video.')


_AJUDA = ('Sou o bot de avisos de publicacao.\n\n'
          'Pra receber os avisos de um canal, manda:\n'
          '<code>meu canal e o lives10</code>')


def _handle_message(msg):
    chat = msg.get('chat') or {}
    chat_id = chat.get('id')
    text = (msg.get('text') or '').strip()
    if chat_id is None or not text:
        return

    inst = _match_instance(text)

    if inst is None:
        if text.lower().startswith('/start') or 'canal' in text.lower():
            send_message(chat_id, _AJUDA)
        return

    if not enroll_open():
        send_message(chat_id, 'Cadastro fechado no momento. Fala com o admin.')
        _log(f'[notifier] cadastro recusado (fechado): chat {chat_id} '
             f"pediu {inst['name']}")
        return

    ok, resposta = _enroll(chat_id, inst)
    send_message(chat_id, resposta)


# ------------------------------------------------------ loop dos cliques

def _handle_send_video(chat_id, inst_id, row_id):
    inst = _instance_by_id(inst_id)
    if not inst:
        send_message(chat_id, '❌ Instancia nao encontrada.')
        return

    row = _find_row(inst, row_id)
    if not row:
        send_message(chat_id, '❌ Publicacao nao encontrada no banco.')
        return

    canal = html.escape(_short_name(inst))
    titulo = html.escape((row.get('clip_titulo') or '')[:120])
    url = row.get('clip_url') or (f"https://www.youtube.com/watch?v={row['clip_video_id']}"
                                  if row.get('clip_video_id') else '')
    fpath = _resolve_clip_file(inst, row.get('live_video_id'), row.get('filename'))

    if not fpath or not os.path.exists(fpath):
        send_message(chat_id, f'⚠️ [{canal}] Arquivo nao esta mais no disco (limpeza).\n\n{titulo}\n{url}')
        return

    size = os.path.getsize(fpath)
    if size > MAX_UPLOAD_BYTES:
        mb = size / (1024 * 1024)
        send_message(chat_id, f'⚠️ [{canal}] Video de {mb:.0f} MB — acima do limite de 50 MB do Telegram.\n\n{titulo}\n{url}')
        return

    try:
        base = os.path.basename(fpath)
        nome = base if base.lower().startswith(canal.lower()) else f'{canal}_{base}'
        resp = send_video(chat_id, fpath, caption=f'<b>{canal}</b>\n{titulo}\n{url}',
                          filename=nome)
        _record_sent(resp, chat_id, 'video', f"{inst['name']}:{row_id}")
        _log(f"[notifier] video enviado: {inst['name']} row {row_id}")
    except Exception as e:
        _log(f'[notifier] sendVideo falhou: {e}')
        send_message(chat_id, f'❌ [{canal}] Falha ao enviar o video: '
                              f'{html.escape(str(e))}\n\n{titulo}\n{url}')


def _handle_callback(cb):
    data = cb.get('data') or ''
    chat_id = (cb.get('message') or {}).get('chat', {}).get('id')
    cb_id = cb.get('id')

    if not data.startswith('v:') or chat_id is None:
        answer_callback(cb_id)
        return

    parts = data.split(':')
    if len(parts) != 3:
        answer_callback(cb_id)
        return

    answer_callback(cb_id, 'Enviando o video...')
    threading.Thread(
        target=_handle_send_video,
        args=(chat_id, parts[1], parts[2]),
        daemon=True,
    ).start()


def updates_loop():
    if not enabled():
        return
    _log('[notifier] long-poll de callbacks iniciado')
    while True:
        try:
            with _state_lock:
                offset = _load_state()['offset']
            resp = _api_call('getUpdates', {
                'offset': offset,
                'timeout': LONGPOLL_TIMEOUT,
                'allowed_updates': json.dumps(['callback_query', 'message']),
            })
            if not resp.get('ok'):
                _log(f'[notifier] getUpdates nao-ok: {resp}')
                time.sleep(10)
                continue
            for upd in resp.get('result', []):
                with _state_lock:
                    _load_state()['offset'] = upd['update_id'] + 1
                    _save_state()
                try:
                    if 'callback_query' in upd:
                        _handle_callback(upd['callback_query'])
                    elif 'message' in upd:
                        _handle_message(upd['message'])
                except Exception as e:
                    _log(f'[notifier] erro tratando update: {e}')
        except urllib.error.HTTPError as e:
            if e.code == 409:
                _log('[notifier] 409 Conflict — este token ja esta sendo pollado '
                     'por outro processo. Use um bot dedicado.')
                time.sleep(60)
            else:
                _log(f'[notifier] getUpdates HTTP {e.code}: {e}')
                time.sleep(10)
        except Exception as e:
            _log(f'[notifier] getUpdates erro: {e}')
            time.sleep(10)


def start(instances, log_fn):
    """Sobe as duas threads. No-op se o token nao estiver configurado."""
    init(instances, log_fn)
    if not enabled():
        log_fn('[notifier] desativado (sem TELEGRAM_NOTIFY_BOT_TOKEN)')
        return
    threading.Thread(target=notify_loop, daemon=True).start()
    threading.Thread(target=updates_loop, daemon=True).start()
