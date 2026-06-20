#!/usr/bin/env python3
"""
Master Dashboard — monitora todas as 7 instâncias yt-pub-lives.
Porta 8090. Heartbeat a cada 5 min. Alerta Telegram a cada 30 min.
"""

import base64
import json
import os
import sys
import subprocess
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

PORT = 8090
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(DASHBOARD_DIR, '..')
CONFIG_DIR = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))
ENV_FILE = os.path.join(CONFIG_DIR, '.env')

# Load env
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ.setdefault(key, val)

# Estado de validação gmail extras
extra_validations_file = os.path.join(DASHBOARD_DIR, 'extra_validations.json')
def load_extra_validations():
    try:
        with open(extra_validations_file) as f:
            return json.load(f)
    except Exception:
        return {}

def save_extra_validation(email):
    data = load_extra_validations()
    data[email] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(extra_validations_file, 'w') as f:
        json.dump(data, f, ensure_ascii=False)

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

import secrets as _sec

_DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'Inema2026$$$')
_VALID_SESSIONS = set()

_LOGIN_HTML = '''<!doctype html>
<html lang="pt-br">
<head><meta charset="utf-8"><title>Login — YT Pub Master</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f1117;color:#e4e4e7;font-family:system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#18181b;border:1px solid #3f3f46;border-radius:12px;padding:2rem;width:320px}
h2{font-size:1.1rem;margin-bottom:1.5rem;color:#a1a1aa;font-weight:500}
label{display:block;margin-bottom:.4rem;font-size:.85rem;color:#71717a}
input{width:100%;padding:.6rem .8rem;border-radius:6px;border:1px solid #3f3f46;
      background:#09090b;color:#e4e4e7;font-size:1rem;outline:none;margin-bottom:1rem}
input:focus{border-color:#6366f1}
button{width:100%;padding:.65rem;border-radius:6px;background:#6366f1;color:#fff;
       border:none;font-size:1rem;cursor:pointer;font-weight:500}
button:hover{background:#4f46e5}
.err{color:#f87171;font-size:.85rem;margin-top:.8rem;text-align:center;min-height:1.2em}
</style></head>
<body>
<div class="card">
  <h2>YT Pub Master</h2>
  <label>Senha</label>
  <input type="password" id="pw" placeholder="••••••••" autofocus>
  <button onclick="login()">Entrar</button>
  <div class="err" id="err"></div>
</div>
<script>
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
async function login(){
  const r=await fetch('/api/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:document.getElementById('pw').value})});
  const d=await r.json();
  if(d.ok)location.href='/';
  else document.getElementById('err').textContent='Senha incorreta';
}
</script>
</body></html>'''

# Instâncias — carregadas de master-dashboard/instances.json
# (gerenciado pelo scripts/setup-canal; ver instances.json.example)
INSTANCES_FILE = os.path.join(DASHBOARD_DIR, 'instances.json')

def load_instances():
    if not os.path.exists(INSTANCES_FILE):
        return []
    try:
        with open(INSTANCES_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f'[WARN] falha ao ler {INSTANCES_FILE}: {e}', file=sys.stderr)
        return []

INSTANCES = load_instances()

# Estado global do heartbeat
heartbeat_data = {'instances': [], 'updated_at': '', 'telegram_last_alert': ''}
heartbeat_lock = threading.Lock()
last_telegram_alert = None


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', file=sys.stderr, flush=True)


def get_service_info(svc_name):
    """Retorna status de um serviço systemd user."""
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'show', svc_name,
             '--property=SubState,ActiveEnterTimestamp,MainPID,NRestarts'],
            capture_output=True, text=True, timeout=5
        )
        info = {}
        for line in result.stdout.strip().split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                info[k] = v
        return info
    except Exception:
        return {}


def get_scheduler_status(instance_path):
    """Lê scheduler_status.json da instância."""
    status_file = os.path.join(instance_path, 'dashboard', 'scheduler_status.json')
    try:
        with open(status_file) as f:
            return json.load(f)
    except Exception:
        return None


def get_db_stats(instance_path):
    """Lê estatísticas do banco SQLite da instância."""
    db_path = os.path.join(instance_path, 'data', 'lives.db')
    if not os.path.exists(db_path):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=2)
        cur = conn.cursor()

        # Total lives
        cur.execute("SELECT COUNT(*) FROM lives")
        total_lives = cur.fetchone()[0]

        # Total publicados
        cur.execute("SELECT COUNT(*) FROM publicados WHERE clip_video_id IS NOT NULL AND clip_video_id != '' AND clip_video_id NOT LIKE 'erro%' AND clip_video_id NOT LIKE 'moved_%' AND clip_video_id != 'publicando'")
        total_pub = cur.fetchone()[0]

        # Publicados ultimas 24h (total e por tipo)
        since_24h = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M')
        cur.execute("SELECT COUNT(*) FROM publicados WHERE data_publicacao >= ? AND clip_video_id IS NOT NULL AND clip_video_id != '' AND clip_video_id NOT LIKE 'erro%' AND clip_video_id NOT LIKE 'moved_%' AND clip_video_id != 'publicando'", (since_24h,))
        pub_hoje = cur.fetchone()[0]

        # Por tipo nas 24h
        cur.execute("""
            SELECT p.live_video_id, l.titulo
            FROM publicados p LEFT JOIN lives l ON p.live_video_id = l.video_id
            WHERE p.data_publicacao >= ? AND p.clip_video_id IS NOT NULL
            AND p.clip_video_id != '' AND p.clip_video_id NOT LIKE 'erro%'
            AND p.clip_video_id NOT LIKE 'moved_%' AND p.clip_video_id != 'publicando'
        """, (since_24h,))
        clips_24h = 0
        imports_24h = 0
        tiktok_24h = 0
        for r in cur.fetchall():
            live_vid = r[0] or ''
            live_titulo = r[1] or ''
            if live_vid.startswith('import_'):
                if live_titulo.startswith('TikTok @'):
                    tiktok_24h += 1
                else:
                    imports_24h += 1
            else:
                clips_24h += 1

        # Último publicado
        cur.execute("SELECT clip_titulo, data_publicacao, clip_video_id FROM publicados WHERE clip_video_id IS NOT NULL AND clip_video_id != '' AND clip_video_id NOT LIKE 'erro%' AND clip_video_id NOT LIKE 'moved_%' AND clip_video_id != 'publicando' ORDER BY data_publicacao DESC LIMIT 1")
        row = cur.fetchone()
        ultimo = {'title': row[0], 'at': row[1], 'video_id': row[2]} if row else None

        # Erros
        cur.execute("SELECT COUNT(*) FROM publicados WHERE clip_video_id LIKE 'erro%'")
        erros = cur.fetchone()[0]

        # Canal URL
        cur.execute("SELECT valor FROM config WHERE chave='canal_destino_url'")
        row = cur.fetchone()
        canal_url = row[0] if row else ''
        if not canal_url:
            cur.execute("SELECT valor FROM config WHERE chave='canal_destino_id'")
            row = cur.fetchone()
            if row and row[0]:
                canal_url = f'https://www.youtube.com/channel/{row[0]}'

        cur.execute("SELECT valor FROM config WHERE chave='canal_destino_nome'")
        row = cur.fetchone()
        canal_nome = row[0] if row else ''

        # Lives por status (mesma definicao do dashboard local: exclui imports/tiktok;
        # pendente = tudo que nao e concluido/erro, nao so status=='pendente')
        cur.execute("SELECT COUNT(*) FROM lives WHERE video_id NOT LIKE 'import_%' AND COALESCE(status_cortes,'') NOT IN ('concluido','erro')")
        lives_pendentes = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM lives WHERE video_id NOT LIKE 'import_%' AND status_cortes='concluido'")
        lives_cortadas = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM lives WHERE video_id NOT LIKE 'import_%' AND status_cortes='erro'")
        lives_erro = cur.fetchone()[0]

        # Clips stats (exclui imports — imports tem contagem propria)
        # total_clips conta rows reais em publicados, nao qtd_clips declarado
        # (qtd_clips e a intencao inicial; o cortador pode produzir menos clips)
        cur.execute("SELECT COUNT(*) FROM publicados WHERE live_video_id NOT LIKE 'import_%'")
        total_clips = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM publicados WHERE live_video_id NOT LIKE 'import_%' AND clip_video_id NOT IN ('erro_upload','publicando','') AND clip_video_id != '' AND clip_video_id NOT LIKE 'moved_%'")
        clips_publicados = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM publicados WHERE live_video_id NOT LIKE 'import_%' AND (clip_video_id IS NULL OR clip_video_id IN ('', 'publicando'))")
        clips_pendentes = cur.fetchone()[0]

        # TikTok stats (imports where titulo starts with 'TikTok @')
        cur.execute("SELECT video_id FROM lives WHERE video_id LIKE 'import_%' AND titulo LIKE 'TikTok @%'")
        tiktok_ids = {r[0] for r in cur.fetchall()}
        tiktok_total = len(tiktok_ids)
        tiktok_pub = 0
        tiktok_erro = 0
        if tiktok_ids:
            placeholders = ','.join(['?'] * len(tiktok_ids))
            cur.execute(f"SELECT clip_video_id FROM publicados WHERE live_video_id IN ({placeholders})", list(tiktok_ids))
            for r in cur.fetchall():
                if r[0] in ('erro_upload', 'publicando', ''):
                    tiktok_erro += 1
                else:
                    tiktok_pub += 1
        cur.execute("SELECT COALESCE(SUM(CAST(qtd_clips AS INTEGER)),0) FROM lives WHERE video_id LIKE 'import_%' AND titulo LIKE 'TikTok @%'")
        tiktok_clips_total = cur.fetchone()[0]
        tiktok_pend = max(0, tiktok_clips_total - tiktok_pub - tiktok_erro)

        # Imports stats (excluding TikTok)
        cur.execute("SELECT COUNT(*) FROM lives WHERE video_id LIKE 'import_%' AND titulo NOT LIKE 'TikTok @%'")
        imports_total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM publicados WHERE live_video_id LIKE 'import_%' AND clip_video_id NOT IN ('erro_upload','publicando','') AND clip_video_id != '' AND clip_video_id NOT LIKE 'moved_%'")
        imports_pub_all = cur.fetchone()[0]
        imports_pub = imports_pub_all - tiktok_pub

        cur.execute("SELECT COUNT(*) FROM publicados WHERE live_video_id LIKE 'import_%' AND clip_video_id='erro_upload'")
        imports_erro_all = cur.fetchone()[0]
        imports_erro = imports_erro_all - tiktok_erro

        cur.execute("SELECT COALESCE(SUM(CAST(qtd_clips AS INTEGER)),0) FROM lives WHERE video_id LIKE 'import_%' AND titulo NOT LIKE 'TikTok @%'")
        imports_clips_total = cur.fetchone()[0]

        # imports_pend: resto dos clips do manifest ainda nao publicados
        # (mesma definicao do dashboard local: soma len(manifest) - publicados_reais por live;
        # NAO somar a fila de publicados aqui — duplicava os clips em voo 'publicando')
        imports_pend = 0

        cur.execute("SELECT video_id FROM lives WHERE video_id LIKE 'import_%' AND titulo NOT LIKE 'TikTok @%'")
        import_only_ids = [r[0] for r in cur.fetchall()]
        if import_only_ids:
            placeholders = ','.join(['?'] * len(import_only_ids))
            cur.execute(
                f"SELECT live_video_id, COUNT(*) FROM publicados WHERE live_video_id IN ({placeholders}) "
                "AND clip_video_id IS NOT NULL AND clip_video_id NOT IN ('erro_upload','publicando','') "
                "AND clip_video_id NOT LIKE 'moved_%' GROUP BY live_video_id",
                import_only_ids,
            )
            pub_count_by_live = dict(cur.fetchall())
            lives_dir = os.path.join(instance_path, 'lives')
            for vid in import_only_ids:
                manifest_path = os.path.join(lives_dir, vid, 'clips_manifest.json')
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path) as mf:
                            clips = json.load(mf)
                        imports_pend += max(0, len(clips) - pub_count_by_live.get(vid, 0))
                    except Exception:
                        pass

        # Cortados ultimas 24h
        cur.execute("SELECT COUNT(*) FROM lives WHERE data_corte >= ?", (since_24h,))
        cortados_hoje = cur.fetchone()[0]

        # Total cortados e pendentes
        cur.execute("SELECT COUNT(*) FROM lives WHERE status_cortes='concluido'")
        total_cortados = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM lives WHERE status_cortes='pendente'")
        pendentes_corte = cur.fetchone()[0]

        # Historico publicacoes (varios periodos)
        pub_history = {}
        # 1d = ultimas 24 horas, por hora
        now = datetime.now()
        hist_1d = []
        for i in range(23, -1, -1):
            dt = now - timedelta(hours=i)
            d = dt.strftime('%Y-%m-%d')
            hh = dt.strftime('%H')
            cur.execute("SELECT COUNT(*) FROM publicados WHERE data_publicacao LIKE ? AND clip_video_id IS NOT NULL AND clip_video_id != '' AND clip_video_id NOT LIKE 'erro%' AND clip_video_id NOT LIKE 'moved_%' AND clip_video_id != 'publicando'", (f'{d} {hh}:%',))
            hist_1d.append({'date': f'{hh}:00', 'count': cur.fetchone()[0]})
        pub_history['1d'] = hist_1d

        for period, days in [('7d', 7), ('30d', 30), ('3m', 90), ('6m', 180), ('1a', 365)]:
            hist = []
            for i in range(days - 1, -1, -1):
                d = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                cur.execute("SELECT COUNT(*) FROM publicados WHERE data_publicacao LIKE ? AND clip_video_id IS NOT NULL AND clip_video_id != '' AND clip_video_id NOT LIKE 'erro%' AND clip_video_id NOT LIKE 'moved_%' AND clip_video_id != 'publicando'", (f'{d}%',))
                hist.append({'date': d, 'count': cur.fetchone()[0]})
            pub_history[period] = hist

        # Config de agendamento
        config = {}
        cur.execute("SELECT chave, valor FROM config WHERE chave IN ('pub_horarios','corte_horarios','pub_max_por_vez','corte_max_por_dia','pipeline_cortes_paused','pipeline_pub_paused','corte_auto')")
        for row in cur.fetchall():
            config[row[0]] = row[1]

        pub_horarios = config.get('pub_horarios', '')
        corte_horarios = config.get('corte_horarios', '')
        pub_max = config.get('pub_max_por_vez', '1')
        corte_max = config.get('corte_max_por_dia', '1')
        corte_paused = config.get('pipeline_cortes_paused', 'false') == 'true'
        pub_paused = config.get('pipeline_pub_paused', 'false') == 'true'
        corte_auto = config.get('corte_auto', 'false') == 'true'

        # Calcular próximo horário
        now = datetime.now()
        now_hm = now.strftime('%H:%M')

        def next_time(horarios_str):
            if not horarios_str:
                return None
            times = sorted([h.strip() for h in horarios_str.split(',') if h.strip()])
            for t in times:
                if t > now_hm:
                    return t
            return times[0] + ' (amanha)' if times else None

        # Previsão publicações por dia
        pub_list = [h.strip() for h in pub_horarios.split(',') if h.strip()]
        pub_previsao_dia = len(pub_list) * int(pub_max)
        corte_list = [h.strip() for h in corte_horarios.split(',') if h.strip()]

        # TikTok channels (handles ativos)
        tiktok_handles = []
        try:
            cur.execute("SELECT handle FROM tiktok_channels WHERE ativo=1")
            tiktok_handles = [r[0] for r in cur.fetchall()]
        except Exception:
            pass

        conn.close()
        return {
            'canal_url': canal_url,
            'canal_nome': canal_nome,
            'tiktok_handles': tiktok_handles,
            'total_lives': total_lives,
            'total_clips': total_clips,
            'clips_publicados': clips_publicados,
            'clips_pendentes': clips_pendentes,
            'pub_history': pub_history,
            'lives_pendentes': lives_pendentes,
            'lives_cortadas': lives_cortadas,
            'lives_erro': lives_erro,
            'total_publicados': total_pub,
            'total_cortados': total_cortados,
            'pendentes_corte': pendentes_corte,
            'publicados_hoje': pub_hoje,
            'clips_24h': clips_24h,
            'imports_24h': imports_24h,
            'tiktok_24h': tiktok_24h,
            'cortados_hoje': cortados_hoje,
            'ultimo_publicado': ultimo,
            'erros': erros,
            'imports_total': imports_total,
            'imports_pub': imports_pub,
            'imports_pend': imports_pend,
            'imports_erro': imports_erro,
            'tiktok_total': tiktok_total,
            'tiktok_pub': tiktok_pub,
            'tiktok_pend': tiktok_pend,
            'tiktok_erro': tiktok_erro,
            'proximo_pub': next_time(pub_horarios) if not pub_paused else 'pausado',
            'proximo_corte': next_time(corte_horarios) if corte_auto and not corte_paused else ('pausado' if corte_paused else 'desligado'),
            'pub_previsao_dia': pub_previsao_dia,
            'corte_previsao_dia': len(corte_list) * int(corte_max),
            'pub_max_por_vez': pub_max,
            'corte_max_por_dia': corte_max,
            'pub_horarios_count': len(pub_list),
            'corte_horarios_count': len(corte_list),
            'pub_paused': pub_paused,
            'corte_paused': corte_paused,
        }
    except Exception as e:
        return {'error': str(e)}


def check_oauth(instance_path):
    """Testa se o OAuth do Google está funcional para a instância."""
    config_dir = os.path.join(instance_path, 'config')
    env_file = os.path.join(config_dir, '.env')
    enc_key_file = os.path.join(config_dir, '.encryption_key')
    creds_file = os.path.join(config_dir, 'credentials.enc')

    # Verificar arquivos necessários
    if not os.path.exists(env_file):
        return {'ok': False, 'status': 'no_env', 'msg': '.env ausente'}
    if not os.path.exists(enc_key_file):
        return {'ok': False, 'status': 'no_key', 'msg': '.encryption_key ausente'}
    if not os.path.exists(creds_file):
        return {'ok': False, 'status': 'no_creds', 'msg': 'credentials.enc ausente'}

    try:
        # Carregar env da instância
        env = {}
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k] = v

        if 'CLIENT_ID' not in env or 'CLIENT_SECRET' not in env:
            return {'ok': False, 'status': 'no_client', 'msg': 'CLIENT_ID/SECRET ausente no .env'}

        # Descriptografar credenciais
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64

        with open(enc_key_file, 'r') as f:
            key = base64.b64decode(f.read().strip())
        with open(creds_file, 'rb') as f:
            enc_data = f.read()

        aesgcm = AESGCM(key)
        creds = json.loads(aesgcm.decrypt(enc_data[:12], enc_data[12:], None))

        if 'refresh_token' not in creds:
            return {'ok': False, 'status': 'no_refresh', 'msg': 'refresh_token ausente'}

        # Tentar obter access token
        token_data = urllib.parse.urlencode({
            'client_id': env['CLIENT_ID'],
            'client_secret': env['CLIENT_SECRET'],
            'refresh_token': creds['refresh_token'],
            'grant_type': 'refresh_token'
        }).encode()

        req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())

        if 'access_token' in resp:
            # Testar YouTube API com o token
            api_key = env.get('API_KEY', '')
            channel_id = env.get('YOUTUBE_CHANNEL_ID', '')
            test_url = f'https://www.googleapis.com/youtube/v3/channels?part=snippet&id={channel_id}&key={api_key}'
            test_req = urllib.request.Request(test_url)
            test_req.add_header('Authorization', f'Bearer {resp["access_token"]}')
            test_resp = json.loads(urllib.request.urlopen(test_req, timeout=10).read())

            channel_name = ''
            if test_resp.get('items'):
                channel_name = test_resp['items'][0]['snippet']['title']

            return {'ok': True, 'status': 'ok', 'msg': f'OAuth OK | Canal: {channel_name}', 'channel': channel_name}
        else:
            return {'ok': False, 'status': 'token_fail', 'msg': 'Token refresh falhou'}

    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return {'ok': False, 'status': 'api_error', 'msg': f'HTTP {e.code}: {body}'}
    except Exception as e:
        return {'ok': False, 'status': 'error', 'msg': str(e)[:200]}


def calc_uptime(timestamp_str):
    """Calcula uptime a partir do ActiveEnterTimestamp do systemd."""
    if not timestamp_str:
        return None
    try:
        # formato: "Wed 2026-04-01 03:03:13 -03"
        # remover dia da semana e timezone
        parts = timestamp_str.strip().split()
        if len(parts) >= 4:
            dt_str = f"{parts[1]} {parts[2]}"
            dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
            delta = datetime.now() - dt
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            mins, _ = divmod(rem, 60)
            if days > 0:
                return f"{days}d {hours}h {mins}m"
            elif hours > 0:
                return f"{hours}h {mins}m"
            else:
                return f"{mins}m"
    except Exception:
        pass
    return None


def check_oauth_quick(instance_path):
    """Verifica OAuth de forma rapida (só testa refresh token, sem chamar YouTube API)."""
    config_dir = os.path.join(instance_path, 'config')
    env_file_path = os.path.join(config_dir, '.env')
    enc_key_file = os.path.join(config_dir, '.encryption_key')
    creds_file = os.path.join(config_dir, 'credentials.enc')

    if not os.path.exists(env_file_path) or not os.path.exists(enc_key_file) or not os.path.exists(creds_file):
        return {'ok': False, 'msg': 'config incompleto'}

    try:
        env = {}
        with open(env_file_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k] = v

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with open(enc_key_file, 'r') as f:
            key = base64.b64decode(f.read().strip())
        with open(creds_file, 'rb') as f:
            enc_data = f.read()

        aesgcm = AESGCM(key)
        creds = json.loads(aesgcm.decrypt(enc_data[:12], enc_data[12:], None))

        token_data = urllib.parse.urlencode({
            'client_id': env.get('CLIENT_ID', ''),
            'client_secret': env.get('CLIENT_SECRET', ''),
            'refresh_token': creds.get('refresh_token', ''),
            'grant_type': 'refresh_token'
        }).encode()

        req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if 'access_token' in resp:
            return {'ok': True, 'msg': 'OK'}
        return {'ok': False, 'msg': 'token refresh falhou'}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:100]
        return {'ok': False, 'msg': f'HTTP {e.code}: {body}'}
    except Exception as e:
        return {'ok': False, 'msg': str(e)[:100]}


def check_instance(inst):
    """Coleta todos os dados de uma instância."""
    sched_info = get_service_info(inst['scheduler_svc'])
    dash_info = get_service_info(inst['dashboard_svc'])
    sched_status = get_scheduler_status(inst['path'])
    db_stats = get_db_stats(inst['path'])
    oauth = check_oauth_quick(inst['path'])

    sched_running = sched_info.get('SubState') == 'running'
    dash_running = dash_info.get('SubState') == 'running'

    # Ler email do .env
    google_email = ''
    env_path = os.path.join(inst['path'], 'config', '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('GOOGLE_EMAIL='):
                    google_email = line.split('=', 1)[1]
                    break

    return {
        'id': inst['id'],
        'name': inst['name'],
        'port': inst['port'],
        'path': inst['path'],
        'scheduler': {
            'running': sched_running,
            'substate': sched_info.get('SubState', 'unknown'),
            'pid': sched_info.get('MainPID', ''),
            'uptime': calc_uptime(sched_info.get('ActiveEnterTimestamp', '')),
            'restarts': sched_info.get('NRestarts', '0'),
            'service': inst['scheduler_svc'],
        },
        'dashboard': {
            'running': dash_running,
            'substate': dash_info.get('SubState', 'unknown'),
            'pid': dash_info.get('MainPID', ''),
            'uptime': calc_uptime(dash_info.get('ActiveEnterTimestamp', '')),
            'service': inst['dashboard_svc'],
        },
        'google_email': google_email,
        'status': sched_status,
        'db': db_stats,
        'oauth': oauth,
    }


def check_all():
    """Verifica todas as instâncias."""
    results = []
    for inst in INSTANCES:
        results.append(check_instance(inst))
    return results


def send_telegram(message):
    """Envia mensagem via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log('Telegram nao configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)')
        return False
    try:
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML',
        }).encode()
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        log(f'Telegram alert sent')
        return True
    except Exception as e:
        log(f'Telegram error: {e}')
        return False


def heartbeat_loop():
    """Loop de heartbeat: checa a cada 5 min, alerta Telegram a cada 30 min."""
    global heartbeat_data, last_telegram_alert

    while True:
        try:
            instances = check_all()
            now = datetime.now()

            with heartbeat_lock:
                heartbeat_data['instances'] = instances
                heartbeat_data['updated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')

            # Verificar se algo está down
            down = []
            for inst in instances:
                if not inst['scheduler']['running']:
                    down.append(f"  - {inst['name']}: scheduler {inst['scheduler']['substate']}")
                if not inst['dashboard']['running']:
                    down.append(f"  - {inst['name']}: dashboard {inst['dashboard']['substate']}")
                if inst.get('oauth') and not inst['oauth'].get('ok'):
                    down.append(f"  - {inst['name']}: OAuth EXPIRADO")

            # Alerta Telegram a cada 30 minutos se algo estiver down
            if down:
                should_alert = (
                    last_telegram_alert is None or
                    (now - last_telegram_alert) >= timedelta(minutes=30)
                )
                if should_alert:
                    msg = f"⚠️ <b>YT-Pub Monitor</b>\n\n"
                    msg += f"Serviços com problema ({now.strftime('%H:%M')}):\n"
                    msg += "\n".join(down)
                    msg += f"\n\nTotal: {len(down)} serviço(s) down"
                    send_telegram(msg)
                    last_telegram_alert = now
                    with heartbeat_lock:
                        heartbeat_data['telegram_last_alert'] = now.strftime('%Y-%m-%d %H:%M:%S')
            else:
                # Tudo OK — se havia alerta anterior, notifica recuperação
                if last_telegram_alert is not None:
                    msg = f"✅ <b>YT-Pub Monitor</b>\n\nTodos os 7 serviços estão rodando ({now.strftime('%H:%M')})"
                    send_telegram(msg)
                    last_telegram_alert = None

        except Exception as e:
            log(f'Heartbeat error: {e}')

        time.sleep(300)  # 5 minutos


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def _require_auth(self):
        cookie = self.headers.get('Cookie', '')
        for part in cookie.split(';'):
            p = part.strip()
            if p.startswith('ds=') and p[3:] in _VALID_SESSIONS:
                return True
        if 'text/html' in self.headers.get('Accept', ''):
            self.send_response(302)
            self.send_header('Location', '/login')
            self.end_headers()
        else:
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
        return False

    def _handle_login(self, data):
        global _DASHBOARD_PASSWORD
        if data.get('password') == _DASHBOARD_PASSWORD:
            token = _sec.token_hex(32)
            _VALID_SESSIONS.add(token)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Set-Cookie', f'ds={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":false}')

    def _handle_logout(self):
        cookie = self.headers.get('Cookie', '')
        for part in cookie.split(';'):
            p = part.strip()
            if p.startswith('ds='):
                _VALID_SESSIONS.discard(p[3:])
        self.send_response(302)
        self.send_header('Set-Cookie', 'ds=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict')
        self.send_header('Location', '/login')
        self.end_headers()

    def _handle_password_change(self, data):
        global _DASHBOARD_PASSWORD
        current = data.get('current', '')
        new_pw = data.get('new', '').strip()
        if current != _DASHBOARD_PASSWORD:
            self._send_json(403, {'error': 'senha atual incorreta'})
            return
        if not new_pw:
            self._send_json(400, {'error': 'nova senha nao pode ser vazia'})
            return
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE) as f:
                lines = f.readlines()
            new_lines = []
            found = False
            for line in lines:
                if line.startswith('DASHBOARD_PASSWORD='):
                    new_lines.append(f'DASHBOARD_PASSWORD={new_pw}\n')
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f'\nDASHBOARD_PASSWORD={new_pw}\n')
            with open(ENV_FILE, 'w') as f:
                f.writelines(new_lines)
        _DASHBOARD_PASSWORD = new_pw
        _VALID_SESSIONS.clear()
        self._send_json(200, {'ok': True})

    def _send_json(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path == '/login':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(_LOGIN_HTML.encode())
            return

        if not self._require_auth():
            return

        if self.path == '/api/status':
            with heartbeat_lock:
                data = json.dumps(heartbeat_data, ensure_ascii=False)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
        elif self.path == '/api/refresh':
            instances = check_all()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with heartbeat_lock:
                heartbeat_data['instances'] = instances
                heartbeat_data['updated_at'] = now
            data = json.dumps(heartbeat_data, ensure_ascii=False)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
        elif self.path.startswith('/api/restart/'):
            # /api/restart/scheduler/3 ou /api/restart/dashboard/5
            parts = self.path.split('/')
            if len(parts) == 5:
                svc_type = parts[3]  # scheduler ou dashboard
                try:
                    inst_id = int(parts[4])
                except ValueError:
                    self.send_error(400)
                    return
                inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
                if inst:
                    svc_key = 'scheduler_svc' if svc_type == 'scheduler' else 'dashboard_svc'
                    svc_name = inst[svc_key]
                    # Tentar remover lock stale antes de reiniciar scheduler
                    if svc_type == 'scheduler':
                        lock_path = os.path.join(inst['path'], '.scheduler.lock')
                        try:
                            if os.path.exists(lock_path):
                                with open(lock_path) as f:
                                    old_pid = int(f.read().strip())
                                try:
                                    os.kill(old_pid, 0)
                                except (ProcessLookupError, OSError):
                                    os.remove(lock_path)
                                    log(f'Removed stale lock: {lock_path}')
                        except Exception:
                            pass
                    try:
                        subprocess.run(
                            ['systemctl', '--user', 'restart', svc_name],
                            capture_output=True, timeout=10
                        )
                        result = {'ok': True, 'msg': f'{svc_name} restarted'}
                    except Exception as e:
                        result = {'ok': False, 'msg': str(e)}
                else:
                    result = {'ok': False, 'msg': 'Instance not found'}
            else:
                result = {'ok': False, 'msg': 'Invalid path'}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        elif self.path == '/api/extra/status':
            result = load_extra_validations()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        elif self.path.startswith('/api/oauth/extra'):
            # /api/oauth/extra?email=xxx — testa OAuth para conta extra
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            email = qs.get('email', [''])[0]
            # Usa lives1 config
            result = check_oauth(INSTANCES[0]['path'])
            # Ajustar msg para indicar qual email
            if result.get('ok'):
                result['msg'] = f"OAuth OK ({email}) | {result.get('channel', '')}"
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        elif self.path.startswith('/api/auth/extra'):
            # /api/auth/extra?email=xxx — abre OAuth com login_hint para qualquer email
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            email = qs.get('email', [''])[0]
            # Usa CLIENT_ID do lives1 como padrão
            inst = INSTANCES[0]
            config_dir = os.path.join(inst['path'], 'config')
            env = {}
            env_file_path = os.path.join(config_dir, '.env')
            if os.path.exists(env_file_path):
                with open(env_file_path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            k, v = line.split('=', 1)
                            env[k] = v
            client_id = env.get('CLIENT_ID', '')
            if not client_id:
                result = {'ok': False, 'msg': 'CLIENT_ID ausente'}
            else:
                redirect_uri = 'http://localhost:8090/api/auth/callback'
                scopes = 'https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/youtube.upload'
                auth_params = {
                    'client_id': client_id,
                    'redirect_uri': redirect_uri,
                    'response_type': 'code',
                    'scope': scopes,
                    'access_type': 'offline',
                    'prompt': 'consent',
                    'state': 'extra',
                }
                if email:
                    auth_params['login_hint'] = email
                params = urllib.parse.urlencode(auth_params)
                auth_url = f'https://accounts.google.com/o/oauth2/auth?{params}'
                result = {'ok': True, 'url': auth_url}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        elif self.path.startswith('/api/auth/start/'):
            # /api/auth/start/3 — gera URL OAuth para a instância
            try:
                inst_id = int(self.path.split('/')[-1])
            except ValueError:
                self.send_error(400)
                return
            inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
            if not inst:
                result = {'ok': False, 'msg': 'Instance not found'}
            else:
                config_dir = os.path.join(inst['path'], 'config')
                env = {}
                env_file_path = os.path.join(config_dir, '.env')
                if os.path.exists(env_file_path):
                    with open(env_file_path) as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#') and '=' in line:
                                k, v = line.split('=', 1)
                                env[k] = v

                client_id = env.get('CLIENT_ID', '')
                if not client_id:
                    result = {'ok': False, 'msg': 'CLIENT_ID ausente no .env'}
                else:
                    redirect_uri = 'http://localhost:8090/api/auth/callback'
                    scopes = 'https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/youtube.upload'
                    # Ler email do .env para login_hint
                    login_hint = env.get('GOOGLE_EMAIL', '')
                    auth_params = {
                        'client_id': client_id,
                        'redirect_uri': redirect_uri,
                        'response_type': 'code',
                        'scope': scopes,
                        'access_type': 'offline',
                        'prompt': 'consent',
                        'state': str(inst_id),
                    }
                    if login_hint:
                        auth_params['login_hint'] = login_hint
                    params = urllib.parse.urlencode(auth_params)
                    auth_url = f'https://accounts.google.com/o/oauth2/auth?{params}'
                    result = {'ok': True, 'url': auth_url}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        elif self.path.startswith('/api/auth/callback'):
            # OAuth callback — troca code por tokens e salva
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = qs.get('code', [None])[0]
            state = qs.get('state', [None])[0]
            error = qs.get('error', [None])[0]

            if error or not code or not state:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(f'<h1>Erro: {error or "sem code"}</h1><p><a href="/">Voltar</a></p>'.encode())
                return

            # Auth extra — só confirma que deu certo, sem salvar em instancia
            if state == 'extra':
                # Usar lives1 para trocar o code (mesmo CLIENT_ID)
                inst = INSTANCES[0]
                config_dir = os.path.join(inst['path'], 'config')
                env = {}
                with open(os.path.join(config_dir, '.env')) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            k, v = line.split('=', 1)
                            env[k] = v
                try:
                    token_data = urllib.parse.urlencode({
                        'client_id': env['CLIENT_ID'],
                        'client_secret': env['CLIENT_SECRET'],
                        'code': code,
                        'grant_type': 'authorization_code',
                        'redirect_uri': 'http://localhost:8090/api/auth/callback',
                    }).encode()
                    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
                    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
                    if 'access_token' in resp:
                        # Pegar email do usuario
                        try:
                            ureq = urllib.request.Request('https://www.googleapis.com/oauth2/v1/userinfo')
                            ureq.add_header('Authorization', f'Bearer {resp["access_token"]}')
                            uinfo = json.loads(urllib.request.urlopen(ureq, timeout=10).read())
                            validated_email = uinfo.get('email', 'desconhecido')
                        except Exception:
                            validated_email = 'desconhecido'
                        save_extra_validation(validated_email)
                        msg = f'Gmail validado: {validated_email}'
                        color = '#22c55e'
                    else:
                        msg = 'Token obtido mas sem access_token'
                        color = '#eab308'
                except Exception as e:
                    msg = f'Erro: {str(e)[:200]}'
                    color = '#ef4444'

                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                html = f'''<html><body style="background:#0f1117;color:#e4e4e7;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh">
                <div style="text-align:center">
                <h1 style="color:{color}">{msg}</h1>
                <p><a href="/" style="color:#6366f1">Voltar ao dashboard</a></p>
                </div></body></html>'''
                self.wfile.write(html.encode())
                return

            inst_id = int(state)
            inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
            if not inst:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'<h1>Instancia nao encontrada</h1>')
                return

            config_dir = os.path.join(inst['path'], 'config')
            env = {}
            with open(os.path.join(config_dir, '.env')) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        env[k] = v

            redirect_uri = 'http://localhost:8090/api/auth/callback'

            try:
                # Exchange code for tokens
                token_data = urllib.parse.urlencode({
                    'client_id': env['CLIENT_ID'],
                    'client_secret': env['CLIENT_SECRET'],
                    'code': code,
                    'grant_type': 'authorization_code',
                    'redirect_uri': redirect_uri,
                }).encode()
                req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
                resp = json.loads(urllib.request.urlopen(req, timeout=10).read())

                if 'refresh_token' not in resp:
                    raise Exception('Resposta sem refresh_token')

                # Encrypt and save
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                import secrets as sec

                key = sec.token_bytes(32)
                key_b64 = base64.b64encode(key).decode()
                aesgcm = AESGCM(key)
                nonce = sec.token_bytes(12)
                creds_json = json.dumps({
                    'access_token': resp.get('access_token', ''),
                    'refresh_token': resp['refresh_token'],
                    'token_type': resp.get('token_type', 'Bearer'),
                }).encode()
                encrypted = nonce + aesgcm.encrypt(nonce, creds_json, None)

                key_path = os.path.join(config_dir, '.encryption_key')
                creds_path = os.path.join(config_dir, 'credentials.enc')
                with open(key_path, 'w') as f:
                    f.write(key_b64)
                os.chmod(key_path, 0o600)
                with open(creds_path, 'wb') as f:
                    f.write(encrypted)
                os.chmod(creds_path, 0o600)

                log(f'OAuth renovado para {inst["name"]}')

                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                html = f'''<html><body style="background:#0f1117;color:#e4e4e7;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh">
                <div style="text-align:center">
                <h1 style="color:#22c55e">Autenticacao concluida!</h1>
                <p>{inst["name"]} — OAuth renovado com sucesso</p>
                <p><a href="/" style="color:#6366f1">Voltar ao dashboard</a></p>
                </div></body></html>'''
                self.wfile.write(html.encode())

            except Exception as e:
                log(f'OAuth callback error: {e}')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                html = f'''<html><body style="background:#0f1117;color:#e4e4e7;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh">
                <div style="text-align:center">
                <h1 style="color:#ef4444">Erro na autenticacao</h1>
                <p>{str(e)[:300]}</p>
                <p><a href="/" style="color:#6366f1">Voltar ao dashboard</a></p>
                </div></body></html>'''
                self.wfile.write(html.encode())
        elif self.path.startswith('/api/resolve/'):
            # /api/resolve/3 — auto-fix: remove stale lock + restart scheduler + restart dashboard
            try:
                inst_id = int(self.path.split('/')[-1])
            except ValueError:
                self.send_error(400)
                return
            inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
            if not inst:
                result = {'ok': False, 'msg': 'Instance not found'}
            else:
                actions = []
                # 1. Remove stale lock
                lock_path = os.path.join(inst['path'], '.scheduler.lock')
                try:
                    if os.path.exists(lock_path):
                        with open(lock_path) as f:
                            old_pid = int(f.read().strip())
                        try:
                            os.kill(old_pid, 0)
                            actions.append(f'Lock ativo (PID {old_pid} vivo)')
                        except (ProcessLookupError, OSError):
                            os.remove(lock_path)
                            actions.append('Lock stale removido')
                except Exception:
                    pass

                # 2. Verificar porta ocupada por outro processo
                try:
                    port = inst['port']
                    ss_result = subprocess.run(
                        ['ss', '-tlnp'], capture_output=True, text=True, timeout=5
                    )
                    for line in ss_result.stdout.split('\n'):
                        if f':{port}' in line and 'pid=' in line:
                            pid_str = line.split('pid=')[1].split(',')[0]
                            pid = int(pid_str)
                            # Verificar se é de outra instância
                            try:
                                cmdline = open(f'/proc/{pid}/cmdline').read()
                                if inst['path'] not in cmdline:
                                    os.kill(pid, 9)
                                    actions.append(f'Porta {port} liberada (PID {pid} de outra instancia)')
                            except Exception:
                                pass
                except Exception:
                    pass

                # 3. Restart scheduler
                try:
                    subprocess.run(['systemctl', '--user', 'restart', inst['scheduler_svc']], capture_output=True, timeout=10)
                    actions.append(f"Scheduler {inst['scheduler_svc']} reiniciado")
                except Exception as e:
                    actions.append(f'Erro restart scheduler: {e}')

                # 3. Restart dashboard
                try:
                    subprocess.run(['systemctl', '--user', 'restart', inst['dashboard_svc']], capture_output=True, timeout=10)
                    actions.append(f"Dashboard {inst['dashboard_svc']} reiniciado")
                except Exception as e:
                    actions.append(f'Erro restart dashboard: {e}')

                # 4. Verificar status
                time.sleep(1)
                sched_info = get_service_info(inst['scheduler_svc'])
                dash_info = get_service_info(inst['dashboard_svc'])
                sched_ok = sched_info.get('SubState') == 'running'
                dash_ok = dash_info.get('SubState') == 'running'
                actions.append(f"Status: scheduler={'OK' if sched_ok else 'FALHOU'}, dashboard={'OK' if dash_ok else 'FALHOU'}")

                result = {'ok': sched_ok and dash_ok, 'msg': '\n'.join(actions)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        elif self.path.startswith('/api/oauth/'):
            # /api/oauth/3 — testa OAuth de uma instância
            try:
                inst_id = int(self.path.split('/')[-1])
            except ValueError:
                self.send_error(400)
                return
            inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
            if inst:
                result = check_oauth(inst['path'])
            else:
                result = {'ok': False, 'msg': 'Instance not found'}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        elif self.path.startswith('/api/logs/'):
            # /api/logs/scheduler/3?lines=50
            parts = self.path.split('?')[0].split('/')
            params = {}
            if '?' in self.path:
                params = dict(p.split('=') for p in self.path.split('?')[1].split('&') if '=' in p)
            if len(parts) == 5:
                svc_type = parts[3]
                try:
                    inst_id = int(parts[4])
                except ValueError:
                    self.send_error(400)
                    return
                inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
                if inst:
                    svc_key = 'scheduler_svc' if svc_type == 'scheduler' else 'dashboard_svc'
                    svc_name = inst[svc_key]
                    lines = min(int(params.get('lines', '80')), 500)
                    try:
                        result = subprocess.run(
                            ['journalctl', '--user', '-u', svc_name, '--no-pager', '-n', str(lines)],
                            capture_output=True, text=True, timeout=10
                        )
                        data = {'ok': True, 'logs': result.stdout}
                    except Exception as e:
                        data = {'ok': False, 'logs': str(e)}
                else:
                    data = {'ok': False, 'logs': 'Instance not found'}
            else:
                data = {'ok': False, 'logs': 'Invalid path'}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        elif self.path == '/' or self.path == '/index.html':
            self.path = '/index.html'
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/api/login':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            self._handle_login(body)
            return
        if self.path == '/api/logout':
            self._handle_logout()
            return

        if not self._require_auth():
            content_len = int(self.headers.get('Content-Length', 0))
            if content_len:
                self.rfile.read(content_len)
            return

        if self.path == '/api/config/password':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            self._handle_password_change(body)
            return

        if self.path == '/api/auth/code':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            inst_id = body.get('instance')
            code = body.get('code', '').strip()

            if not inst_id or not code:
                result = {'ok': False, 'msg': 'instance e code obrigatorios'}
            else:
                inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
                if not inst:
                    result = {'ok': False, 'msg': 'Instancia nao encontrada'}
                else:
                    config_dir = os.path.join(inst['path'], 'config')
                    env = {}
                    with open(os.path.join(config_dir, '.env')) as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#') and '=' in line:
                                k, v = line.split('=', 1)
                                env[k] = v
                    try:
                        token_data = urllib.parse.urlencode({
                            'client_id': env['CLIENT_ID'],
                            'client_secret': env['CLIENT_SECRET'],
                            'code': code,
                            'grant_type': 'authorization_code',
                            'redirect_uri': 'http://localhost',
                        }).encode()
                        req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
                        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())

                        if 'refresh_token' not in resp:
                            raise Exception('Resposta sem refresh_token')

                        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                        import secrets as sec

                        key = sec.token_bytes(32)
                        key_b64 = base64.b64encode(key).decode()
                        aesgcm = AESGCM(key)
                        nonce = sec.token_bytes(12)
                        creds_json = json.dumps({
                            'access_token': resp.get('access_token', ''),
                            'refresh_token': resp['refresh_token'],
                            'token_type': resp.get('token_type', 'Bearer'),
                        }).encode()
                        encrypted = nonce + aesgcm.encrypt(nonce, creds_json, None)

                        key_path = os.path.join(config_dir, '.encryption_key')
                        creds_path = os.path.join(config_dir, 'credentials.enc')
                        with open(key_path, 'w') as f:
                            f.write(key_b64)
                        os.chmod(key_path, 0o600)
                        with open(creds_path, 'wb') as f:
                            f.write(encrypted)
                        os.chmod(creds_path, 0o600)

                        log(f'OAuth renovado para {inst["name"]}')
                        result = {'ok': True, 'msg': f'OAuth renovado para {inst["name"]}!'}
                    except urllib.error.HTTPError as e:
                        err = e.read().decode()[:200]
                        result = {'ok': False, 'msg': f'Erro: {err}'}
                    except Exception as e:
                        result = {'ok': False, 'msg': str(e)[:200]}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        elif self.path == '/api/import/distribute':
            try:
                sys.path.insert(0, os.path.abspath(PROJECT_ROOT))
                import import_worker, importlib
                importlib.reload(import_worker)
                result = import_worker.distribute_imports()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': True, **result}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': str(e)}).encode())

        elif self.path == '/api/exec':
            content_len = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_len)) if content_len else {}
            inst_id = body.get('instance')  # None = geral
            cmd = body.get('cmd', '').strip()

            # Whitelist de comandos seguros
            ALLOWED = [
                'journalctl', 'systemctl', 'ps', 'ls', 'cat', 'head', 'tail',
                'grep', 'wc', 'df', 'du', 'free', 'uptime', 'date', 'sqlite3',
                'python3', 'pip', 'which', 'echo', 'find', 'stat',
            ]
            first_word = cmd.split()[0] if cmd else ''
            if not first_word or first_word not in ALLOWED:
                result = {'ok': False, 'output': f'Comando nao permitido: {first_word}\nPermitidos: {", ".join(ALLOWED)}'}
            else:
                cwd = None
                if inst_id is not None:
                    inst = next((i for i in INSTANCES if i['id'] == inst_id), None)
                    if inst:
                        cwd = inst['path']

                try:
                    proc = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True,
                        timeout=30, cwd=cwd
                    )
                    output = proc.stdout
                    if proc.stderr:
                        output += '\n' + proc.stderr if output else proc.stderr
                    result = {'ok': proc.returncode == 0, 'output': output or '(sem output)'}
                except subprocess.TimeoutExpired:
                    result = {'ok': False, 'output': 'Timeout (30s)'}
                except Exception as e:
                    result = {'ok': False, 'output': str(e)}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # silenciar logs HTTP


def main():
    # Primeira verificação imediata
    log('Master Dashboard starting...')
    instances = check_all()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with heartbeat_lock:
        heartbeat_data['instances'] = instances
        heartbeat_data['updated_at'] = now

    running = sum(1 for i in instances if i['scheduler']['running'])
    log(f'Initial check: {running}/7 schedulers running')

    # Thread de heartbeat
    t = threading.Thread(target=heartbeat_loop, daemon=True)
    t.start()

    # HTTP server
    server = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
    log(f'Master Dashboard on port {PORT}')
    server.serve_forever()


if __name__ == '__main__':
    main()
