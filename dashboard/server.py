#!/usr/bin/env python3
"""
Dashboard server for GWS Lives & Clips pipeline.
Reads/writes to local SQLite database and syncs with YouTube channel.
"""

import json
import os
import sys
import base64
import shutil
import urllib.request
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

# Config
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
CONFIG_DIR = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))
ENV_FILE = os.path.join(CONFIG_DIR, '.env')
PORT = 8091

# Load env (before reading env-dependent vars)
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key] = val

sys.path.insert(0, PROJECT_ROOT)
import db
import secrets as _sec

_DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'Inema2026$$$')
_VALID_SESSIONS = set()


def yt_dlp_cmd():
    """Return a yt-dlp command that works when the executable is not on PATH."""
    exe = shutil.which('yt-dlp') or shutil.which('yt-dlp.exe')
    if exe:
        return [exe]
    return [sys.executable, '-m', 'yt_dlp']

_LOGIN_HTML = '''<!doctype html>
<html lang="pt-br">
<head><meta charset="utf-8"><title>Login — YT Pub Lives</title>
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
  <h2>YT Pub Lives</h2>
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


def get_access_token():
    """Get OAuth access token from encrypted credentials."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    with open(os.path.join(CONFIG_DIR, '.encryption_key'), 'r') as f:
        key = base64.b64decode(f.read().strip())

    with open(os.path.join(CONFIG_DIR, 'credentials.enc'), 'rb') as f:
        data = f.read()

    aesgcm = AESGCM(key)
    creds = json.loads(aesgcm.decrypt(data[:12], data[12:], None))

    token_data = urllib.parse.urlencode({
        'client_id': os.environ['CLIENT_ID'],
        'client_secret': os.environ['CLIENT_SECRET'],
        'refresh_token': creds['refresh_token'],
        'grant_type': 'refresh_token'
    }).encode()

    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp['access_token']


def youtube_api(endpoint, params=None):
    """Call YouTube Data API."""
    token = get_access_token()
    api_key = os.environ.get('API_KEY', '')

    base_url = f'https://www.googleapis.com/youtube/v3/{endpoint}'
    if params:
        params['key'] = api_key
        base_url += '?' + urllib.parse.urlencode(params)
    else:
        base_url += f'?key={api_key}'

    req = urllib.request.Request(base_url)
    req.add_header('Authorization', f'Bearer {token}')

    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        return {'error': error_body, 'status': e.code}


def get_channel_lives(channel_id, page_token=None, published_after=None, published_before=None):
    """Get live streams from channel using search API."""
    params = {
        'channelId': channel_id,
        'part': 'snippet',
        'type': 'video',
        'eventType': 'completed',
        'maxResults': 50,
        'order': 'date'
    }
    if page_token:
        params['pageToken'] = page_token
    if published_after:
        params['publishedAfter'] = published_after
    if published_before:
        params['publishedBefore'] = published_before
    return youtube_api('search', params)


def get_video_details(video_ids):
    """Get video details by IDs."""
    params = {
        'part': 'snippet,contentDetails,statistics',
        'id': ','.join(video_ids)
    }
    return youtube_api('videos', params)


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

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
            self.send_json(403, {'error': 'senha atual incorreta'})
            return
        if not new_pw:
            self.send_json(400, {'error': 'nova senha nao pode ser vazia'})
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
        self.send_json(200, {'ok': True})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/login':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(_LOGIN_HTML.encode())
            return

        if not self._require_auth():
            return

        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/api/lives':
            self.handle_api_lives()
        elif path == '/api/publicados':
            video_id = qs.get('live', [None])[0]
            self.handle_api_publicados(video_id)
        elif path == '/api/config':
            self.handle_api_config()
        elif path == '/api/prompts':
            self.handle_api_prompts_get()
        elif path == '/api/stats':
            self.handle_api_stats()
        elif path == '/api/scheduler/status':
            self.handle_scheduler_status()
        elif path == '/api/transcript':
            video_id = qs.get('id', [None])[0]
            self.handle_api_transcript(video_id)
        elif path == '/api/thumbs/pending':
            self.handle_thumbs_pending()
        elif path == '/api/sheet':
            sheet_name = qs.get('name', ['CONFIG'])[0]
            self.handle_sheet_read(sheet_name)
        elif path == '/api/enrich/bg':
            self.handle_enrich_bg_get()
        elif path == '/api/tiktok/channels':
            self.handle_tiktok_channels_get()
        elif path == '/api/tiktok/queue':
            self.handle_tiktok_queue(qs)
        elif path == '/api/health':
            self.handle_api_health()
        elif path.startswith('/clips/'):
            self.handle_serve_clip(path)
        elif path == '/':
            self.path = '/index.html'
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode() if content_length else '{}'
        data = json.loads(body) if body else {}

        parsed = urllib.parse.urlparse(self.path)
        post_path = parsed.path

        if post_path == '/api/login':
            self._handle_login(data)
            return
        if post_path == '/api/logout':
            self._handle_logout()
            return

        if not self._require_auth():
            return

        if post_path == '/api/config/password':
            self._handle_password_change(data)
        elif post_path == '/api/sync':
            self.handle_sync(data)
        elif post_path == '/api/sync/url':
            self.handle_sync_url(data)
        elif post_path == '/api/config':
            self.handle_update_config(data)
        elif post_path == '/api/clip/privacy':
            self.handle_clip_privacy(data)
        elif post_path == '/api/clip/delete':
            self.handle_clip_delete(data)
        elif post_path == '/api/pipeline/toggle':
            self.handle_pipeline_toggle(data)
        elif post_path == '/api/live/reprocess':
            self.handle_live_reprocess(data)
        elif post_path == '/api/clip/pause':
            self.handle_clip_pause(data)
        elif post_path == '/api/clip/delete-pending':
            self.handle_clip_delete_pending(data)
        elif post_path == '/api/prompts':
            self.handle_api_prompts_save(data)
        elif post_path == '/api/cleanup/clips':
            self.handle_cleanup_clips(data)
        elif post_path == '/api/cleanup/sources':
            self.handle_cleanup_sources(data)
        elif post_path == '/api/live/delete':
            self.handle_live_delete(data)
        elif post_path == '/api/thumbs/upload':
            self.handle_thumbs_upload(data)
        elif post_path == '/api/clip/retry':
            self.handle_clip_retry(data)
        elif post_path == '/api/clip/dismiss-erro':
            self.handle_clip_dismiss_erro(data)
        elif post_path == '/api/thumb/preview':
            self.handle_thumb_preview(data)
        elif post_path == '/api/sheet/update':
            self.handle_sheet_update(data)
        elif post_path == '/api/sheet/upload':
            self.handle_sheet_upload(data)
        elif post_path == '/api/lives/fix-dates':
            self.handle_fix_dates()
        elif post_path == '/api/publicados/cleanup':
            self.handle_publicados_cleanup()
        elif post_path == '/api/import/scan':
            self.handle_import_scan()
        elif post_path == '/api/import/clean':
            self.handle_import_clean(data)
        elif post_path == '/api/enrich':
            self.handle_enrich_run()
        elif post_path == '/api/enrich/upload-bg':
            self.handle_enrich_upload_bg(data)
        elif post_path == '/api/enrich/mark':
            self.handle_enrich_mark(data)
        elif post_path == '/api/tiktok/channels':
            self.handle_tiktok_channels_post(data)
        elif post_path == '/api/tiktok/channels/update':
            self.handle_tiktok_channels_update(data)
        elif post_path == '/api/tiktok/channels/delete':
            self.handle_tiktok_channels_delete(data)
        elif post_path == '/api/tiktok/scan':
            self.handle_tiktok_scan(data)
        elif post_path == '/api/tiktok/fetch-latest':
            self.handle_tiktok_fetch_latest(data)
        elif post_path == '/api/tiktok/download':
            self.handle_tiktok_download()
        elif post_path == '/api/tiktok/download-url':
            self.handle_tiktok_download_url(data)
        elif post_path == '/api/enrich/url':
            self.handle_enrich_url(data)
        else:
            self.send_json(404, {'error': 'not found'})

    def handle_thumb_preview(self, data):
        """Generate a thumbnail preview with current design settings."""
        import types, base64
        try:
            # Set design env vars from request
            for key, val in data.items():
                if key.startswith('design_') and val:
                    os.environ[key.upper()] = str(val)

            # Load yt-thumbnail module
            scripts_dir = os.path.join(PROJECT_ROOT, 'scripts')
            script_path = os.path.join(scripts_dir, 'yt-thumbnail')
            yt_thumb = types.ModuleType('yt_thumbnail')
            yt_thumb.__file__ = script_path
            with open(script_path) as f:
                exec(compile(f.read(), script_path, 'exec'), yt_thumb.__dict__)

            # Create background based on request
            preview_bg = data.get('preview_bg', 'dark')
            if preview_bg == 'light':
                from PIL import Image as _Img
                bg = _Img.new('RGB', (1280, 720), (220, 220, 225))
            else:
                bg = yt_thumb.create_gradient_bg()

            # Compose with sample text
            frase = data.get('preview_text', 'MULTIPLIQUE SEU LUCRO')
            import tempfile
            output_path = os.path.join(tempfile.gettempdir(), 'thumb_preview.jpg')
            yt_thumb.compose_thumbnail(bg, frase, '', output_path)

            # Return as base64
            with open(output_path, 'rb') as f:
                img_b64 = base64.b64encode(f.read()).decode()

            self.send_json(200, {'image': img_b64})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_sheet_read(self, sheet_name):
        """Read full sheet data."""
        allowed = {'CONFIG', 'LIVES', 'PUBLICADOS'}
        if sheet_name not in allowed:
            self.send_json(400, {'error': 'invalid sheet'})
            return
        values = db.get_table_as_rows(sheet_name)
        self.send_json(200, {'sheet': sheet_name, 'values': values})

    def handle_sheet_update(self, data):
        """Save edited sheet data (full replace)."""
        sheet_name = data.get('sheet', '')
        values = data.get('values', [])
        if sheet_name not in {'CONFIG', 'LIVES', 'PUBLICADOS'}:
            self.send_json(400, {'error': 'invalid sheet'})
            return
        if not values:
            self.send_json(400, {'error': 'no data'})
            return
        db.replace_table(sheet_name, values)
        self.send_json(200, {'ok': True, 'rows': len(values)})

    def handle_sheet_upload(self, data):
        """Upload CSV to replace sheet."""
        import csv as _csv, io as _io
        sheet_name = data.get('sheet', '')
        csv_text = data.get('csv', '')
        if sheet_name not in {'CONFIG', 'LIVES', 'PUBLICADOS'}:
            self.send_json(400, {'error': 'invalid sheet'})
            return
        if not csv_text:
            self.send_json(400, {'error': 'no csv data'})
            return
        reader = _csv.reader(_io.StringIO(csv_text))
        values = [row for row in reader]
        if not values:
            self.send_json(400, {'error': 'empty csv'})
            return
        db.replace_table(sheet_name, values)
        self.send_json(200, {'ok': True, 'rows': len(values)})

    def send_json(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def handle_api_health(self):
        """Check health of all system dependencies."""
        import subprocess
        checks = {}

        # yt-dlp
        try:
            r = subprocess.run(yt_dlp_cmd() + ['--version'], capture_output=True, text=True, timeout=5)
            checks['yt_dlp'] = {'ok': r.returncode == 0, 'detail': r.stdout.strip() if r.returncode == 0 else 'erro'}
        except Exception as e:
            checks['yt_dlp'] = {'ok': False, 'detail': str(e)}

        # Database
        try:
            db.load_config()
            checks['database'] = {'ok': True, 'detail': 'ok'}
        except Exception as e:
            checks['database'] = {'ok': False, 'detail': str(e)}

        cfg = db.load_config()

        # IA cortes/pub
        try:
            ai_mode = cfg.get('ai_mode', 'claude-cli')
            if ai_mode == 'openrouter-api':
                openrouter_key = cfg.get('openrouter_api_key', '') or os.environ.get('OPENROUTER_API_KEY', '')
                if openrouter_key:
                    checks['api_ia'] = {'ok': True, 'detail': 'openrouter key configurada'}
                else:
                    checks['api_ia'] = {'ok': False, 'detail': 'sem openrouter_api_key'}
            elif ai_mode == 'anthropic-api':
                anthropic_key = cfg.get('anthropic_api_key', '') or os.environ.get('ANTHROPIC_API_KEY', '')
                checks['api_ia'] = {'ok': bool(anthropic_key), 'detail': 'anthropic key configurada' if anthropic_key else 'sem anthropic_api_key'}
            else:
                r = subprocess.run(['claude', '-p', '--output-format', 'json', 'diga ok'], capture_output=True, text=True, timeout=30)
                checks['api_ia'] = {'ok': r.returncode == 0, 'detail': 'Claude CLI ok' if r.returncode == 0 else f'erro code {r.returncode}'}
        except Exception as e:
            checks['api_ia'] = {'ok': False, 'detail': str(e)[:80]}

        # Piramyd API key (para thumbnail)
        env_file = os.path.join(CONFIG_DIR, '.env')
        api_key = ''
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.startswith('PIRAMYD_API_KEY='):
                        api_key = line.split('=', 1)[1].strip()
        if not api_key:
            api_key = os.environ.get('PIRAMYD_API_KEY', '')

        # Thumbnail API - testa o provider configurado
        try:
            img_provider = cfg.get('thumb_image_provider', 'piramyd')

            if img_provider == 'kie':
                kie_key = cfg.get('kie_api_key', '')
                if kie_key:
                    # Testar Kie.ai listando jobs (leve, sem gerar imagem)
                    req = urllib.request.Request('https://api.kie.ai/api/v1/jobs/recordInfo?taskId=test')
                    req.add_header('Authorization', f'Bearer {kie_key}')
                    urllib.request.urlopen(req, timeout=10)
                    checks['api_thumb'] = {'ok': True, 'detail': f'kie ok'}
                else:
                    checks['api_thumb'] = {'ok': False, 'detail': 'sem kie_api_key'}
            elif img_provider == 'minimax':
                minimax_key = cfg.get('minimax_api_key', '') or os.environ.get('MINIMAX_API_KEY', '')
                if minimax_key:
                    checks['api_thumb'] = {'ok': True, 'detail': 'minimax key configurada'}
                else:
                    checks['api_thumb'] = {'ok': False, 'detail': 'sem minimax_api_key'}
            elif img_provider == 'piramyd':
                api_key = cfg.get('thumb_api_key', '') or api_key
                if api_key:
                    payload = json.dumps({'model': 'chatgpt-4.1', 'messages': [{'role': 'user', 'content': 'ok'}], 'max_tokens': 1}).encode()
                    req = urllib.request.Request('https://api.piramyd.cloud/v1/chat/completions', data=payload)
                    req.add_header('Content-Type', 'application/json')
                    req.add_header('Authorization', f'Bearer {api_key}')
                    urllib.request.urlopen(req, timeout=15)
                    checks['api_thumb'] = {'ok': True, 'detail': 'piramyd ok'}
                else:
                    checks['api_thumb'] = {'ok': False, 'detail': 'sem piramyd key'}
            elif img_provider == 'local':
                url = cfg.get('inemaimg_url', '') or 'http://localhost:8000'
                try:
                    urllib.request.urlopen(url, timeout=5)
                    checks['api_thumb'] = {'ok': True, 'detail': f'inemaimg ok ({url})'}
                except Exception as e:
                    checks['api_thumb'] = {'ok': False, 'detail': f'inemaimg down: {str(e)[:60]}'}
            else:
                checks['api_thumb'] = {'ok': True, 'detail': f'{img_provider} (sem teste)'}
        except Exception as e:
            checks['api_thumb'] = {'ok': False, 'detail': str(e)[:80]}

        # YouTube API
        try:
            result = youtube_api('channels', {'part': 'snippet', 'mine': 'true'})
            if 'items' in result:
                checks['youtube'] = {'ok': True, 'detail': result['items'][0]['snippet']['title']}
            else:
                checks['youtube'] = {'ok': False, 'detail': result.get('error', 'sem resposta')[:80]}
        except Exception as e:
            checks['youtube'] = {'ok': False, 'detail': str(e)[:80]}

        # Scheduler process
        try:
            status_file = os.path.join(os.path.dirname(__file__), 'scheduler_status.json')
            if os.path.exists(status_file):
                with open(status_file) as f:
                    st = json.load(f)
                checks['scheduler'] = {'ok': st.get('state') != 'offline', 'detail': st.get('state', 'offline')}
            else:
                checks['scheduler'] = {'ok': False, 'detail': 'offline'}
        except Exception as e:
            checks['scheduler'] = {'ok': False, 'detail': str(e)}

        self.send_json(200, checks)

    def handle_scheduler_status(self):
        status_file = os.path.join(os.path.dirname(__file__), 'scheduler_status.json')
        if os.path.exists(status_file):
            with open(status_file) as f:
                data = json.load(f)
            self.send_json(200, data)
        else:
            self.send_json(200, {'state': 'offline', 'detail': 'Scheduler nao iniciado', 'updated_at': ''})

    def handle_serve_clip(self, path):
        """Serve clip files from lives directory."""
        # /clips/<video_id>/<filename>
        parts = path.split('/', 3)  # ['', 'clips', 'video_id', 'filename']
        if len(parts) < 4:
            self.send_json(404, {'error': 'not found'})
            return
        video_id = parts[2]
        filename = parts[3]
        # Sanitize
        if '..' in video_id or '..' in filename or '/' in filename:
            self.send_json(400, {'error': 'invalid path'})
            return
        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        filepath = os.path.join(lives_dir, video_id, 'clips', filename)
        if not os.path.exists(filepath):
            self.send_json(404, {'error': 'file not found'})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(os.path.getsize(filepath)))
        self.send_header('Content-Disposition', f'inline; filename="{filename}"')
        self.end_headers()
        with open(filepath, 'rb') as f:
            self.wfile.write(f.read())

    def handle_api_lives(self):
        lives = db.get_lives()
        pub_records = db.get_publicados()

        # Build pub_dates map from pub_records
        pub_dates = {}
        for p in pub_records:
            lid = p.get('live_video_id', '')
            dt = p.get('data_publicacao', '')
            if lid and dt:
                if lid not in pub_dates or dt > pub_dates[lid]:
                    pub_dates[lid] = dt

        for live in lives:
            live['data_publicacao'] = pub_dates.get(live.get('video_id', ''), '')

        lives.sort(key=lambda l: l.get('data_live', ''))
        self.send_json(200, {'lives': lives, 'total': len(lives)})

    def handle_fix_dates(self):
        """Fill missing data_live from YouTube API for existing lives."""
        lives = db.get_lives()
        if not lives:
            self.send_json(200, {'ok': True, 'fixed': 0})
            return

        # Find lives missing data_live
        missing = [(l['video_id'],) for l in lives if l.get('video_id') and not l.get('data_live')]

        if not missing:
            self.send_json(200, {'ok': True, 'fixed': 0, 'message': 'Todas as lives ja tem data'})
            return

        # Fetch dates from YouTube in batches of 50
        fixed = 0
        updated = 0
        missing_vids = [m[0] for m in missing]
        for batch_start in range(0, len(missing_vids), 50):
            batch = missing_vids[batch_start:batch_start + 50]
            details = get_video_details(batch)
            for item in details.get('items', []):
                vid = item['id']
                published = item.get('snippet', {}).get('publishedAt', '')[:10]
                if published:
                    db.update_live(vid, data_live=published)
                    fixed += 1
                    updated += 1

        self.send_json(200, {'ok': True, 'fixed': fixed, 'updated': updated, 'total_missing': len(missing)})

    def handle_publicados_cleanup(self):
        """Remove empty rows, duplicate erro_upload, and duplicate published clips from PUBLICADOS."""
        result = db.cleanup_publicados()
        self.send_json(200, {'ok': True, **result})

    def handle_import_scan(self):
        """Dispara o import_worker manualmente: processa pastas em imports/."""
        try:
            sys.path.insert(0, PROJECT_ROOT)
            import import_worker
            config = db.load_config()
            results = import_worker.process_imports(config)
            ok = [r for r in results if r.get('ok')]
            self.send_json(200, {'ok': True, 'processados': len(ok), 'total': len(results), 'detalhes': results})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_import_clean(self, data):
        """
        Limpeza de pastas.
        action: 'imports'          — limpa imports/ (residuos nao processados)
                'clips'            — limpa clips/ das lives totalmente publicadas
                'clips_all'        — limpa clips/ de TODAS as lives (cuidado)
        """
        try:
            sys.path.insert(0, PROJECT_ROOT)
            import import_worker
            action = data.get('action', 'imports')
            if action == 'imports':
                n = import_worker.clean_imports()
                self.send_json(200, {'ok': True, 'removidos': n})
            elif action == 'clips':
                n = import_worker.clean_clips(only_fully_published=True)
                self.send_json(200, {'ok': True, 'lives_limpas': n})
            elif action == 'clips_all':
                n = import_worker.clean_clips(only_fully_published=False)
                self.send_json(200, {'ok': True, 'lives_limpas': n})
            else:
                self.send_json(400, {'error': f'action invalida: {action}'})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_enrich_run(self):
        """Run enrich process manually."""
        try:
            sys.path.insert(0, PROJECT_ROOT)
            from scheduler import process_enrich, load_config
            config = load_config()
            result = process_enrich(config)
            self.send_json(200, {'ok': True, **result})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_enrich_mark(self, data):
        """Mark a live for enrichment (redo title + description)."""
        video_id = data.get('video_id', '')
        if not video_id:
            self.send_json(400, {'error': 'video_id required'})
            return
        db.update_live(video_id, observacoes='refazer_enrich')
        self.send_json(200, {'ok': True, 'video_id': video_id})

    def handle_enrich_url(self, data):
        """Import a live from URL and enrich it (transcript → title/desc → thumb → cuts)."""
        import re
        url = data.get('url', '').strip()
        if not url:
            self.send_json(400, {'error': 'url required'})
            return

        # Extract video ID from various YouTube URL formats
        vid = None
        m = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
        if m:
            vid = m.group(1)
        if not vid:
            self.send_json(400, {'error': 'URL invalida — nao encontrei video ID'})
            return

        try:
            # Get video details from YouTube API
            details = get_video_details([vid])
            items = details.get('items', [])
            if not items:
                self.send_json(404, {'error': f'Video {vid} nao encontrado no YouTube'})
                return

            item = items[0]
            snippet = item.get('snippet', {})
            title = snippet.get('title', '')
            pub_date = snippet.get('publishedAt', '')[:10]

            # Parse duration
            dur_str = item.get('contentDetails', {}).get('duration', '')
            dur_min = str(parse_duration_minutes(dur_str))

            # Add to database if not exists
            existing = db.get_live(vid)
            if not existing:
                today = __import__('datetime').date.today().isoformat()
                db.add_lives([{
                    'video_id': vid,
                    'titulo': title,
                    'data_live': pub_date,
                    'duracao_min': dur_min,
                    'url': f'https://www.youtube.com/watch?v={vid}',
                    'status_transcricao': 'pendente',
                    'status_cortes': 'pendente',
                    'qtd_clips': '0',
                    'clips_publicados': '0',
                    'clips_pendentes': '0',
                    'data_sync': today,
                    'observacoes': 'refazer_enrich',
                }])
            else:
                db.update_live(vid, observacoes='refazer_enrich')

            # Run enrich (which runs cuts first if needed)
            sys.path.insert(0, PROJECT_ROOT)
            from scheduler import process_enrich, load_config
            config = load_config()
            result = process_enrich(config)

            # Get updated title
            live = db.get_live(vid)
            new_title = live.get('titulo', title) if live else title

            self.send_json(200, {
                'ok': True,
                'video_id': vid,
                'title': new_title,
                'enriched': result.get('enriched', 0),
                'errors': result.get('errors', 0),
            })
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tiktok_channels_get(self):
        channels = db.get_tiktok_channels()
        for c in channels:
            c['stats'] = db.get_tiktok_channel_stats(c.get('handle', ''))
        self.send_json(200, {'channels': channels})

    def handle_tiktok_channels_post(self, data):
        handle = data.get('handle', '').strip()
        if not handle:
            self.send_json(400, {'error': 'handle required'})
            return
        row_id = db.add_tiktok_channel(
            handle=handle,
            nome=data.get('nome', ''),
            ativo=int(data.get('ativo', 1)),
            data_desde=data.get('data_desde', ''),
            max_por_scan=int(data.get('max_por_scan', 2))
        )
        self.send_json(200, {'ok': True, 'id': row_id})

    def handle_tiktok_channels_update(self, data):
        channel_id = data.get('id')
        if not channel_id:
            self.send_json(400, {'error': 'id required'})
            return
        fields = {}
        for k in ('handle', 'nome', 'data_desde'):
            if k in data:
                fields[k] = data[k]
        for k in ('ativo', 'max_por_scan'):
            if k in data:
                fields[k] = int(data[k])
        if fields:
            db.update_tiktok_channel(channel_id, **fields)
        self.send_json(200, {'ok': True})

    def handle_tiktok_channels_delete(self, data):
        channel_id = data.get('id')
        if not channel_id:
            self.send_json(400, {'error': 'id required'})
            return
        db.delete_tiktok_channel(channel_id)
        self.send_json(200, {'ok': True})

    def handle_tiktok_scan(self, data=None):
        """Scan completo (popula fila). body opcional: {channel_id, playlist_end, download_after}."""
        try:
            sys.path.insert(0, PROJECT_ROOT)
            import tiktok_scanner
            data = data or {}
            playlist_end = int(data.get('playlist_end', 5000))
            download_after = bool(data.get('download_after', False))
            channel_id = data.get('channel_id')
            include_inactive = bool(data.get('include_inactive', False))

            channels = db.get_tiktok_channels()
            if channel_id:
                channels = [c for c in channels if c.get('id') == int(channel_id)]
            elif not include_inactive:
                channels = [c for c in channels if c.get('ativo', 0) == 1]

            scan_results = []
            for c in channels:
                r = tiktok_scanner.scan_channel_to_queue(c, playlist_end=playlist_end)
                r['handle'] = c.get('handle', '')
                scan_results.append(r)

            dl_results = []
            if download_after:
                dl_results = tiktok_scanner.download_pending_videos()

            self.send_json(200, {'ok': True, 'scan': scan_results, 'download': dl_results})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tiktok_fetch_latest(self, data):
        """Fetch incremental: scan com early-break no cutoff (MAX upload_date).

        Body: {channel_id?, include_inactive?, download_after?}.
        """
        try:
            sys.path.insert(0, PROJECT_ROOT)
            import tiktok_scanner
            channel_id = data.get('channel_id')
            include_inactive = bool(data.get('include_inactive', False))
            download_after = bool(data.get('download_after', True))

            channels = db.get_tiktok_channels()
            if channel_id:
                channels = [c for c in channels if c.get('id') == int(channel_id)]
            elif not include_inactive:
                channels = [c for c in channels if c.get('ativo', 0) == 1]

            scan_results = []
            for c in channels:
                r = tiktok_scanner.fetch_new_videos_for_channel(c)
                r['handle'] = c.get('handle', '')
                scan_results.append(r)

            dl_results = tiktok_scanner.download_pending_videos() if download_after else []
            self.send_json(200, {'ok': True, 'scan': scan_results, 'download': dl_results})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tiktok_download(self):
        """Dispara download imediato da fila (sem scan)."""
        try:
            sys.path.insert(0, PROJECT_ROOT)
            import tiktok_scanner
            results = tiktok_scanner.download_pending_videos()
            total = sum(r.get('downloaded', 0) for r in results)
            self.send_json(200, {'ok': True, 'total_downloaded': total, 'channels': results})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tiktok_queue(self, qs):
        """GET /api/tiktok/queue?channel=@x&status=pendente&limit=50."""
        channel = qs.get('channel', [''])[0]
        status = qs.get('status', ['pendente'])[0]
        limit = int(qs.get('limit', ['50'])[0])
        try:
            conn = db.get_db()
            rows = conn.execute(
                'SELECT * FROM tiktok_videos WHERE channel_handle=? AND status=? '
                'ORDER BY upload_date ASC LIMIT ?',
                (channel, status, limit)
            ).fetchall()
            self.send_json(200, {'videos': [dict(r) for r in rows]})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_tiktok_download_url(self, data):
        """Download avulso via URL. Se handle for de canal registrado, usa a fila; senao salva em imports/ manual."""
        url = data.get('url', '').strip()
        if not url:
            self.send_json(400, {'error': 'url required'})
            return
        try:
            sys.path.insert(0, PROJECT_ROOT)
            import tiktok_scanner
            import import_worker

            result = subprocess.run(
                yt_dlp_cmd() + ['-j', '--no-warnings', url],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                self.send_json(500, {'error': f'yt-dlp erro: {result.stderr[:200]}'})
                return

            info = json.loads(result.stdout)
            vid_id = info.get('id', '')
            title = info.get('title', '')
            uploader = info.get('uploader', '') or info.get('channel', '')
            handle = '@' + uploader.lstrip('@') if uploader else '@manual'

            db.upsert_tiktok_video(
                tiktok_id=vid_id, channel_handle=handle,
                title=title, url=url,
                upload_date=info.get('upload_date', ''),
                duration=int(info.get('duration') or 0),
                status='pendente'
            )

            channels = db.get_tiktok_channels()
            ch = next((c for c in channels if c.get('handle', '').lstrip('@') == handle.lstrip('@')), None)
            if ch is None:
                ch = {'id': 0, 'handle': handle, 'max_por_scan': 1}
            dl = tiktok_scanner.download_pending_for_channel(ch, max_por_scan=1)

            if dl.get('downloaded', 0) > 0:
                import_worker.process_imports()

            self.send_json(200, {
                'ok': True, 'video_id': vid_id, 'title': title,
                'downloaded': dl.get('downloaded', 0),
            })
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_enrich_bg_get(self):
        """Serve the enrich background image."""
        config_dir = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))
        bg_path = os.path.join(config_dir, 'thumb_default.jpg')
        if os.path.exists(bg_path):
            with open(bg_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_json(404, {'error': 'no background image'})

    def handle_enrich_upload_bg(self, data):
        """Upload a default background image for enrich thumbnails."""
        import base64
        try:
            img_b64 = data.get('image', '')
            if not img_b64:
                self.send_json(400, {'error': 'image (base64) required'})
                return
            img_data = base64.b64decode(img_b64)
            config_dir = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))
            bg_path = os.path.join(config_dir, 'thumb_default.jpg')
            with open(bg_path, 'wb') as f:
                f.write(img_data)
            self.send_json(200, {'ok': True, 'size': len(img_data), 'path': bg_path})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_api_publicados(self, filter_live_id=None):
        publicados = db.get_publicados(filter_live_id)

        # Enrich publicados with filename from manifest
        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        manifests_cache = {}
        for pub in publicados:
            lid = pub.get('live_video_id', '')
            if lid and lid not in manifests_cache:
                mp = os.path.join(lives_dir, lid, 'clips_manifest.json')
                if os.path.exists(mp):
                    try:
                        with open(mp, encoding='utf-8') as f:
                            manifests_cache[lid] = {c.get('title', ''): c.get('filename', '') for c in json.load(f)}
                    except Exception:
                        manifests_cache[lid] = {}
                else:
                    manifests_cache[lid] = {}
            pub['filename'] = manifests_cache.get(lid, {}).get(pub.get('clip_titulo', ''), '')

        # Incluir clips pendentes (cortados mas nao publicados)
        pendentes = []
        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        pub_titles = set(p.get('clip_titulo', '') for p in publicados)

        live_ids = [filter_live_id] if filter_live_id else []
        if not filter_live_id:
            # Scan all lives with topics.json OR imports with clips_manifest.json
            if os.path.isdir(lives_dir):
                for d in os.listdir(lives_dir):
                    job = os.path.join(lives_dir, d)
                    if os.path.exists(os.path.join(job, 'topics.json')) or \
                       (d.startswith('import_') and os.path.exists(os.path.join(job, 'clips_manifest.json'))):
                        live_ids.append(d)

        for lid in live_ids:
            topics_path = os.path.join(lives_dir, lid, 'topics.json')
            manifest_path = os.path.join(lives_dir, lid, 'clips_manifest.json')
            is_import = lid.startswith('import_')

            if is_import and not os.path.exists(topics_path):
                # Imports: usa clips_manifest.json como fonte de pendentes
                if not os.path.exists(manifest_path):
                    continue
                try:
                    with open(manifest_path, encoding='utf-8') as f:
                        clips = json.load(f)
                    for c in clips:
                        title = c.get('title', '')
                        _fn = c.get('filename', '')
                        if title not in pub_titles and _fn and os.path.exists(os.path.join(lives_dir, lid, 'clips', _fn)):
                            pendentes.append({
                                'title':         title,
                                'description':   c.get('description', ''),
                                'tags':          ', '.join(c.get('tags', [])) if isinstance(c.get('tags'), list) else c.get('tags', ''),
                                'start':         '',
                                'end':           '',
                                'live_video_id': lid,
                                'filename':      c.get('filename', ''),
                                'paused':        c.get('paused', False),
                            })
                except Exception:
                    pass
            elif os.path.exists(topics_path):
                try:
                    with open(topics_path, encoding='utf-8') as f:
                        topics_data = json.load(f)
                    # Load manifest for filenames and paused state
                    manifest = {}
                    if os.path.exists(manifest_path):
                        with open(manifest_path, encoding='utf-8') as f:
                            for c in json.load(f):
                                manifest[c.get('title', '')] = {
                                    'filename': c.get('filename', ''),
                                    'paused': c.get('paused', False)
                                }
                    for t in topics_data.get('topics', []):
                        title = t.get('title', '')
                        m = manifest.get(title, {})
                        _fn = m.get('filename', '')
                        if title not in pub_titles and _fn and os.path.exists(os.path.join(lives_dir, lid, 'clips', _fn)):
                            pendentes.append({
                                'title':         title,
                                'description':   t.get('description', ''),
                                'tags':          ', '.join(t.get('tags', [])),
                                'start':         t.get('start', ''),
                                'end':           t.get('end', ''),
                                'live_video_id': lid,
                                'filename':      m.get('filename', ''),
                                'paused':        m.get('paused', False),
                            })
                except Exception:
                    pass

        self.send_json(200, {'publicados': publicados, 'pendentes': pendentes, 'total': len(publicados), 'filter': filter_live_id})

    def handle_api_transcript(self, video_id):
        """Return transcript for a video if available locally."""
        if not video_id:
            self.send_json(400, {'error': 'id parameter required'})
            return

        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        job_dir = os.path.join(lives_dir, video_id)

        result = {'video_id': video_id, 'has_transcript': False, 'has_topics': False}

        # Check condensed transcript
        condensed_path = os.path.join(job_dir, 'condensed.txt')
        if os.path.exists(condensed_path):
            with open(condensed_path, 'r') as f:
                result['transcript'] = f.read()
            result['has_transcript'] = True

        # Check topics
        topics_path = os.path.join(job_dir, 'topics.json')
        if os.path.exists(topics_path):
            with open(topics_path, 'r') as f:
                result['topics'] = json.load(f)
            result['has_topics'] = True

        self.send_json(200, result)

    def handle_api_config(self):
        config = db.load_config()
        to_save = {}

        if 'minimax_api_key' not in config and os.environ.get('MINIMAX_API_KEY'):
            config['minimax_api_key'] = os.environ.get('MINIMAX_API_KEY', '')

        # Add canal info from env/YouTube API
        channel_id = os.environ.get('YOUTUBE_CHANNEL_ID', '')
        if channel_id and 'canal_origem_nome' not in config:
            config['canal_origem_id'] = channel_id
            config['canal_origem_url'] = f'https://www.youtube.com/channel/{channel_id}'
            to_save['canal_origem_id'] = channel_id
            to_save['canal_origem_url'] = config['canal_origem_url']
            try:
                result = youtube_api('channels', {'part': 'snippet', 'id': channel_id})
                items = result.get('items', [])
                if items:
                    config['canal_origem_nome'] = items[0]['snippet']['title']
                    to_save['canal_origem_nome'] = config['canal_origem_nome']
            except Exception:
                config['canal_origem_nome'] = channel_id
                to_save['canal_origem_nome'] = channel_id

        # Canal destino = authenticated channel
        if 'canal_destino_nome' not in config:
            try:
                token = get_access_token()
                req = urllib.request.Request('https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true')
                req.add_header('Authorization', f'Bearer {token}')
                resp = json.loads(urllib.request.urlopen(req).read())
                items = resp.get('items', [])
                if items:
                    config['canal_destino_id'] = items[0]['id']
                    config['canal_destino_nome'] = items[0]['snippet']['title']
                    config['canal_destino_url'] = f'https://www.youtube.com/channel/{items[0]["id"]}'
                    to_save['canal_destino_id'] = config['canal_destino_id']
                    to_save['canal_destino_nome'] = config['canal_destino_nome']
                    to_save['canal_destino_url'] = config['canal_destino_url']
            except Exception:
                pass

        # Persist in database so master dashboard can read it
        if to_save:
            db.update_config(to_save)

        self.send_json(200, {'config': config})

    def handle_api_prompts_get(self):
        config_dir = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))
        prompts = {}
        for name in ('prompt_cortes', 'prompt_pub', 'prompt_thumb', 'prompt_enrich'):
            path = os.path.join(config_dir, f'{name}.txt')
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    prompts[name] = f.read()
            else:
                prompts[name] = ''
        self.send_json(200, {'prompts': prompts})

    def handle_api_prompts_save(self, data):
        config_dir = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))
        saved = []
        for name in ('prompt_cortes', 'prompt_pub', 'prompt_thumb', 'prompt_enrich'):
            if name in data:
                path = os.path.join(config_dir, f'{name}.txt')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(data[name])
                saved.append(name)
        self.send_json(200, {'ok': True, 'saved': saved})

    def handle_api_stats(self):
        lives_list = db.get_lives()
        pub_list = db.get_publicados()

        total_lives = 0
        total_publicados = 0
        total_clips = 0
        pendentes = 0
        cortados = 0
        lives_erro = 0

        # Import stats
        imports_total = 0
        imports_clips_pub = 0
        imports_clips_pend = 0
        imports_clips_erro = 0

        import_ids = set()
        tiktok_ids = set()

        for live in lives_list:
            vid = live.get('video_id', '')
            is_import = vid.startswith('import_')
            is_tiktok = is_import and (live.get('titulo', '') or '').startswith('TikTok @')
            status = live.get('status_cortes', '')
            qtd = int(live.get('qtd_clips', '0') or '0')
            pub = int(live.get('clips_publicados', '0') or '0')

            if is_import:
                import_ids.add(vid)
                if is_tiktok:
                    tiktok_ids.add(vid)
                else:
                    imports_total += 1
            else:
                total_lives += 1
                total_clips += qtd
                if status == 'concluido':
                    cortados += 1
                elif status == 'erro':
                    lives_erro += 1
                else:
                    pendentes += 1

        # Clips stats (separate lives, imports, tiktok)
        clips_erro = 0
        tiktok_pub = 0
        tiktok_erro = 0
        for pub in pub_list:
            vid_status = pub.get('clip_video_id', '')
            live_vid = pub.get('live_video_id', '')
            is_err = vid_status in ('erro_upload', 'publicando', '')
            if live_vid in tiktok_ids:
                if is_err:
                    tiktok_erro += 1
                else:
                    tiktok_pub += 1
            elif live_vid in import_ids:
                if is_err:
                    imports_clips_erro += 1
                else:
                    imports_clips_pub += 1
            else:
                if is_err:
                    clips_erro += 1
                else:
                    total_publicados += 1

        # Clips pendentes de imports (manifest total - ja publicados, por quantidade)
        # Nao usar set de titulos: manifests com titulos duplicados ('Video') colapsam para 1
        pub_count_by_live = {}
        for pub in pub_list:
            lid = pub.get('live_video_id', '')
            cid = pub.get('clip_video_id', '')
            if lid in import_ids and cid and cid not in ('erro_upload', 'publicando') and not cid.startswith('moved_'):
                pub_count_by_live[lid] = pub_count_by_live.get(lid, 0) + 1

        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        import_only_ids = import_ids - tiktok_ids
        for vid in import_only_ids:
            manifest_path = os.path.join(lives_dir, vid, 'clips_manifest.json')
            if os.path.exists(manifest_path):
                try:
                    import json as _json
                    with open(manifest_path) as f:
                        clips = _json.load(f)
                    imports_clips_pend += max(0, len(clips) - pub_count_by_live.get(vid, 0))
                except Exception:
                    pass

        # Motivo de nao rodar imports
        config = db.load_config()
        import_motivo = ''
        if config.get('pipeline_imports_paused', 'false') == 'true':
            import_motivo = 'pausado'
        elif not config.get('import_pub_horarios', '').strip():
            import_motivo = 'sem horario'
        elif imports_total == 0 and imports_clips_pend > 0:
            import_motivo = 'fila vazia'

        # TikTok stats
        tiktok_total = len(tiktok_ids)
        tiktok_clips_total = sum(
            int(l.get('qtd_clips', '0') or '0')
            for l in lives_list if l.get('video_id', '') in tiktok_ids
        )
        tiktok_pend = max(0, tiktok_clips_total - tiktok_pub - tiktok_erro)

        # Publicados nas ultimas 24h por tipo
        from datetime import datetime as _dt, timedelta
        since_24h = (_dt.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M')
        clips_24h = 0
        imports_24h = 0
        tiktok_24h = 0
        cortados_24h = 0
        for pub in pub_list:
            if pub.get('data_publicacao', '') < since_24h:
                continue
            vid_status = pub.get('clip_video_id', '')
            if vid_status in ('erro_upload', 'publicando', ''):
                continue
            live_vid = pub.get('live_video_id', '')
            if live_vid in tiktok_ids:
                tiktok_24h += 1
            elif live_vid in import_ids:
                imports_24h += 1
            else:
                clips_24h += 1
        for live in lives_list:
            if live.get('data_corte', '') >= since_24h and live.get('status_cortes') == 'concluido':
                cortados_24h += 1

        self.send_json(200, {
            'instance_name': os.environ.get('INSTANCE_NAME', 'yt-pub-lives'),
            'total_lives': total_lives,
            'total_clips': total_clips,
            'total_publicados': total_publicados,
            'lives_cortadas': cortados,
            'lives_pendentes': pendentes,
            'lives_erro': lives_erro,
            'clips_erro': clips_erro,
            'imports_total': imports_total,
            'imports_clips_pub': imports_clips_pub,
            'imports_clips_pend': imports_clips_pend,
            'imports_clips_erro': imports_clips_erro,
            'import_motivo': import_motivo,
            'tiktok_total': tiktok_total,
            'tiktok_pub': tiktok_pub,
            'tiktok_pend': tiktok_pend,
            'tiktok_erro': tiktok_erro,
            'cortados_24h': cortados_24h,
            'clips_24h': clips_24h,
            'imports_24h': imports_24h,
            'tiktok_24h': tiktok_24h,
        })

    def handle_sync_url(self, data):
        """Import a single YouTube video by URL into the lives database."""
        import re
        url = data.get('url', '').strip()
        if not url:
            self.send_json(400, {'error': 'url required'})
            return

        m = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
        if not m:
            self.send_json(400, {'error': 'URL invalida — nao encontrei video ID'})
            return
        vid = m.group(1)

        existing = db.get_live(vid)
        if existing:
            self.send_json(200, {'ok': True, 'video_id': vid, 'titulo': existing.get('titulo', ''), 'message': 'ja existe no banco'})
            return

        try:
            details = get_video_details([vid])
            items = details.get('items', [])
            if not items:
                self.send_json(404, {'error': f'Video {vid} nao encontrado no YouTube'})
                return

            item = items[0]
            snippet = item.get('snippet', {})
            titulo = snippet.get('title', '')
            pub_date = snippet.get('publishedAt', '')[:10]
            dur_str = item.get('contentDetails', {}).get('duration', '')
            dur_min = str(parse_duration_minutes(dur_str))

            today = __import__('datetime').date.today().isoformat()
            db.add_lives([{
                'video_id': vid,
                'titulo': titulo,
                'data_live': pub_date,
                'duracao_min': dur_min,
                'url': f'https://www.youtube.com/watch?v={vid}',
                'status_transcricao': 'pendente',
                'status_cortes': 'pendente',
                'qtd_clips': '0',
                'clips_publicados': '0',
                'clips_pendentes': '0',
                'data_sync': today,
                'observacoes': '',
            }])

            self.send_json(200, {'ok': True, 'video_id': vid, 'titulo': titulo, 'duracao_min': dur_min})
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def handle_sync(self, data):
        """Sync lives from YouTube channel.
        Options:
          mode: 'novas' (only new, skip existing) | 'todas' (all in date range)
          date_from: 'YYYY-MM-DD' (optional)
          date_to: 'YYYY-MM-DD' (optional)
          max_pages: int (default 10)
        """
        channel_id = os.environ.get('YOUTUBE_CHANNEL_ID', '')
        mode = data.get('mode', 'novas')
        date_from = data.get('date_from', '').strip()
        date_to = data.get('date_to', '').strip()
        max_lives = data.get('max_lives', 50)
        max_pages = (max_lives // 50) + 1

        # Validar formato e validade das datas (YYYY-MM-DD)
        import re as _re
        from datetime import datetime as _dt
        date_pattern = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
        for label, val in [('inicio', date_from), ('fim', date_to)]:
            if val:
                if not date_pattern.match(val):
                    self.send_json(400, {'error': f'Data {label} invalida: "{val}". Use formato YYYY-MM-DD'})
                    return
                try:
                    _dt.strptime(val, '%Y-%m-%d')
                except ValueError:
                    self.send_json(400, {'error': f'Data {label} nao existe: "{val}" (ex: setembro tem 30 dias)'})
                    return

        # Build date filters for YouTube API (ISO 8601)
        published_after = f'{date_from}T00:00:00Z' if date_from else None
        published_before = f'{date_to}T23:59:59Z' if date_to else None

        # Get existing video IDs from database
        existing_ids = {l['video_id'] for l in db.get_lives()}

        # Fetch lives from YouTube
        all_lives = []
        page_token = None

        for _ in range(max_pages):
            result = get_channel_lives(channel_id, page_token, published_after, published_before)
            if 'error' in result:
                self.send_json(500, {'error': result['error']})
                return

            items = result.get('items', [])
            for item in items:
                vid = item['id'].get('videoId', '')
                if not vid:
                    continue
                if mode == 'novas' and vid in existing_ids:
                    continue
                snippet = item.get('snippet', {})
                pub_date = snippet.get('publishedAt', '')[:10]
                # Filtro server-side: YouTube API nem sempre respeita publishedBefore/After
                if date_from and pub_date < date_from:
                    continue
                if date_to and pub_date > date_to:
                    continue
                all_lives.append({
                    'video_id': vid,
                    'titulo': snippet.get('title', ''),
                    'data_live': pub_date,
                    'url': f'https://www.youtube.com/watch?v={vid}'
                })

            page_token = result.get('nextPageToken')
            if not page_token:
                break

        # Filter out duplicates and limit
        all_lives = [l for l in all_lives if l['video_id'] not in existing_ids][:max_lives]

        # Get durations for new videos
        if all_lives:
            video_ids = [l['video_id'] for l in all_lives]
            # Batch in groups of 50
            for i in range(0, len(video_ids), 50):
                batch = video_ids[i:i+50]
                details = get_video_details(batch)
                duration_map = {}
                for item in details.get('items', []):
                    vid = item['id']
                    # Parse ISO 8601 duration (PT1H30M15S)
                    dur = item.get('contentDetails', {}).get('duration', '')
                    minutes = parse_duration_minutes(dur)
                    duration_map[vid] = minutes

                for live in all_lives:
                    if live['video_id'] in duration_map:
                        live['duracao_min'] = str(duration_map[live['video_id']])

            # Add new lives to database
            today = __import__('datetime').date.today().isoformat()
            new_lives_list = []
            for live in all_lives:
                new_lives_list.append({
                    'video_id': live['video_id'],
                    'titulo': live['titulo'],
                    'data_live': live['data_live'],
                    'duracao_min': live.get('duracao_min', ''),
                    'url': live['url'],
                    'status_transcricao': 'pendente',
                    'status_cortes': 'pendente',
                    'qtd_clips': '0',
                    'clips_publicados': '0',
                    'clips_pendentes': '0',
                    'data_sync': today,
                    'observacoes': ''
                })

            if new_lives_list:
                db.add_lives(new_lives_list)

        self.send_json(200, {
            'novas_lives': len(all_lives),
            'ja_existentes': len(existing_ids),
            'mode': mode,
            'date_from': date_from,
            'date_to': date_to,
            'lives': all_lives
        })

    def handle_update_config(self, data):
        """Update config values."""
        db.update_config(data)
        self.send_json(200, {'ok': True, 'updated': list(data.keys())})

    def handle_clip_privacy(self, data):
        """Update privacy of a published clip on YouTube."""
        clip_id = data.get('clip_video_id')
        new_privacy = data.get('privacy')
        if not clip_id or not new_privacy:
            self.send_json(400, {'error': 'clip_video_id and privacy required'})
            return

        # Update on YouTube
        token = get_access_token()
        api_key = os.environ.get('API_KEY', '')
        body = {
            'id': clip_id,
            'status': {'privacyStatus': new_privacy}
        }
        url = f'https://www.googleapis.com/youtube/v3/videos?part=status&key={api_key}'
        req_data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=req_data, method='PUT')
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')

        try:
            resp = urllib.request.urlopen(req)
            json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            self.send_json(500, {'error': error_body})
            return

        # Update in database
        db.update_publicado_by_clip_id(clip_id, privacy=new_privacy)

        self.send_json(200, {'ok': True, 'clip_video_id': clip_id, 'privacy': new_privacy})

    def handle_clip_delete(self, data):
        """Delete a published clip from YouTube."""
        clip_id = data.get('clip_video_id')
        if not clip_id:
            self.send_json(400, {'error': 'clip_video_id required'})
            return

        # Delete from YouTube
        token = get_access_token()
        api_key = os.environ.get('API_KEY', '')
        url = f'https://www.googleapis.com/youtube/v3/videos?id={clip_id}&key={api_key}'
        req = urllib.request.Request(url, method='DELETE')
        req.add_header('Authorization', f'Bearer {token}')

        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            if e.code != 204:
                error_body = e.read().decode()
                self.send_json(500, {'error': error_body})
                return

        # Remove from database
        db.delete_publicado(clip_id)

        self.send_json(200, {'ok': True, 'deleted': clip_id})

    def handle_pipeline_toggle(self, data):
        """Toggle pipeline pause flags in CONFIG."""
        target = data.get('target', 'cortes')  # cortes | pub | imports
        key_map = {'cortes': 'pipeline_cortes_paused', 'pub': 'pipeline_pub_paused', 'imports': 'pipeline_imports_paused'}
        key = key_map.get(target, 'pipeline_pub_paused')

        current = db.get_config(key, 'false')
        new_val = 'false' if current == 'true' else 'true'
        db.set_config(key, new_val)

        self.send_json(200, {'ok': True, 'target': target, 'paused': new_val == 'true'})

    def handle_live_reprocess(self, data):
        """Reset status_cortes (and optionally status_transcricao) to allow reprocessing."""
        video_id = data.get('video_id', '')
        if not video_id:
            self.send_json(400, {'error': 'video_id required'})
            return

        live = db.get_live(video_id)
        if not live:
            self.send_json(404, {'error': f'video_id {video_id} not found'})
            return

        db.update_live(video_id,
                       status_transcricao='pendente',
                       status_cortes='pendente',
                       qtd_clips='0',
                       clips_publicados='0',
                       clips_pendentes='0',
                       observacoes='')

        # Clean local files to force re-download
        import shutil
        job_dir = os.path.join(os.path.dirname(__file__), '..', 'lives', video_id)
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)

        self.send_json(200, {'ok': True, 'video_id': video_id})

    def handle_clip_pause(self, data):
        """Toggle paused status of a clip in clips_manifest.json."""
        live_id = data.get('live_video_id', '')
        title = data.get('title', '')
        if not live_id or not title:
            self.send_json(400, {'error': 'live_video_id and title required'})
            return

        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        manifest_path = os.path.join(lives_dir, live_id, 'clips_manifest.json')
        if not os.path.exists(manifest_path):
            self.send_json(404, {'error': 'manifest not found'})
            return

        with open(manifest_path) as f:
            clips = json.load(f)

        found = False
        for clip in clips:
            if clip.get('title', '') == title:
                clip['paused'] = not clip.get('paused', False)
                found = True
                new_state = clip['paused']
                break

        if found:
            with open(manifest_path, 'w') as f:
                json.dump(clips, f, ensure_ascii=False, indent=2)
            self.send_json(200, {'ok': True, 'paused': new_state})
        else:
            self.send_json(404, {'error': 'clip not found in manifest'})

    def handle_clip_delete_pending(self, data):
        """Remove a pending clip from clips_manifest.json."""
        live_id = data.get('live_video_id', '')
        title = data.get('title', '')
        if not live_id or not title:
            self.send_json(400, {'error': 'live_video_id and title required'})
            return

        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        manifest_path = os.path.join(lives_dir, live_id, 'clips_manifest.json')
        if not os.path.exists(manifest_path):
            self.send_json(404, {'error': 'manifest not found'})
            return

        with open(manifest_path) as f:
            clips = json.load(f)

        original_len = len(clips)
        clips = [c for c in clips if c.get('title', '') != title]

        if len(clips) == original_len:
            self.send_json(404, {'error': 'clip not found in manifest'})
            return

        with open(manifest_path, 'w') as f:
            json.dump(clips, f, ensure_ascii=False, indent=2)

        self.send_json(200, {'ok': True, 'removed': original_len - len(clips)})

    def handle_cleanup_clips(self, data):
        """Deleta arquivos mp4 dos clips do disco. Mantem manifest e planilha."""
        video_id = data.get('video_id', '')  # opcional: limpar só uma live
        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        deleted = 0
        freed = 0

        if video_id:
            dirs = [os.path.join(lives_dir, video_id)]
        else:
            dirs = [os.path.join(lives_dir, d) for d in os.listdir(lives_dir)
                    if os.path.isdir(os.path.join(lives_dir, d))]

        for job_dir in dirs:
            clips_dir = os.path.join(job_dir, 'clips')
            if not os.path.isdir(clips_dir):
                continue
            for f in os.listdir(clips_dir):
                if f.endswith('.mp4'):
                    fpath = os.path.join(clips_dir, f)
                    freed += os.path.getsize(fpath)
                    os.remove(fpath)
                    deleted += 1

        freed_mb = freed / 1024 / 1024
        self.send_json(200, {'ok': True, 'deleted': deleted, 'freed_mb': round(freed_mb, 1)})

    def handle_cleanup_sources(self, data):
        """Deleta arquivos source.mp4 (videos originais) do disco. Mantem clips e manifest."""
        video_id = data.get('video_id', '')  # opcional: limpar só uma live
        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        deleted = 0
        freed = 0

        if video_id:
            dirs = [os.path.join(lives_dir, video_id)]
        else:
            dirs = [os.path.join(lives_dir, d) for d in os.listdir(lives_dir)
                    if os.path.isdir(os.path.join(lives_dir, d))]

        for job_dir in dirs:
            source = os.path.join(job_dir, 'source.mp4')
            if os.path.exists(source):
                freed += os.path.getsize(source)
                os.remove(source)
                deleted += 1

        freed_mb = freed / 1024 / 1024
        self.send_json(200, {'ok': True, 'deleted': deleted, 'freed_mb': round(freed_mb, 1)})

    def handle_live_delete(self, data):
        """Deleta live: remove arquivos do disco E remove do banco de dados."""
        import shutil
        video_id = data.get('video_id', '')
        if not video_id:
            self.send_json(400, {'error': 'video_id required'})
            return

        live = db.get_live(video_id)
        if not live:
            self.send_json(404, {'error': f'{video_id} not found'})
            return

        # Remove from database
        db.delete_live(video_id)

        # Remove arquivos do disco
        lives_dir = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))
        job_dir = os.path.join(lives_dir, video_id)
        freed = 0
        if os.path.exists(job_dir):
            for root, dirs, files in os.walk(job_dir):
                for f in files:
                    freed += os.path.getsize(os.path.join(root, f))
            shutil.rmtree(job_dir)

        freed_mb = freed / 1024 / 1024
        self.send_json(200, {'ok': True, 'video_id': video_id, 'freed_mb': round(freed_mb, 1)})


    def handle_thumbs_pending(self):
        """List pending thumbnails."""
        pending_file = os.path.join(os.path.dirname(__file__), '..', 'lives', 'pending_thumbs.json')
        thumb_dir = os.path.join(os.path.dirname(__file__), '..', 'lives', 'thumbs')
        if not os.path.exists(pending_file):
            self.send_json(200, {'pending': [], 'total': 0})
            return
        with open(pending_file) as f:
            clips = json.load(f)
        # Enrich with has_image flag
        for clip in clips:
            thumb_path = os.path.join(thumb_dir, f"{clip['id']}.jpg")
            clip['has_image'] = os.path.exists(thumb_path)
        self.send_json(200, {'pending': clips, 'total': len(clips)})

    def handle_thumbs_upload(self, data):
        """Upload pending thumbnails to YouTube."""
        pending_file = os.path.join(os.path.dirname(__file__), '..', 'lives', 'pending_thumbs.json')
        thumb_dir = os.path.join(os.path.dirname(__file__), '..', 'lives', 'thumbs')

        if not os.path.exists(pending_file):
            self.send_json(200, {'ok': True, 'uploaded': 0, 'errors': 0, 'remaining': 0})
            return

        with open(pending_file) as f:
            clips = json.load(f)

        if not clips:
            self.send_json(200, {'ok': True, 'uploaded': 0, 'errors': 0, 'remaining': 0})
            return

        # Import upload function from scheduler
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        from scheduler import upload_thumbnail

        uploaded = 0
        errors = 0
        remaining = []
        error_details = []

        for clip in clips:
            vid = clip['id']
            thumb_path = os.path.join(thumb_dir, f'{vid}.jpg')

            if not os.path.exists(thumb_path):
                remaining.append(clip)
                continue

            try:
                upload_thumbnail(vid, thumb_path)
                uploaded += 1
            except Exception as e:
                err_msg = str(e)
                if 'quota' in err_msg.lower():
                    remaining.append(clip)
                    # Add remaining clips that haven't been processed
                    idx = clips.index(clip)
                    remaining.extend(clips[idx + 1:])
                    error_details.append('Quota excedida - parou')
                    break
                errors += 1
                error_details.append(f'{clip.get("title", vid)[:40]}: {err_msg[:60]}')
                remaining.append(clip)

        # Update pending file
        with open(pending_file, 'w') as f:
            json.dump(remaining, f, indent=2, ensure_ascii=False)

        self.send_json(200, {
            'ok': True,
            'uploaded': uploaded,
            'errors': errors,
            'remaining': len(remaining),
            'error_details': error_details
        })


    def handle_clip_retry(self, data):
        """Remove erro_upload entry so the scheduler picks it up in the normal queue."""
        live_id = data.get('live_video_id', '')
        title = data.get('title', '')
        if not live_id or not title:
            self.send_json(400, {'error': 'live_video_id and title required'})
            return

        cleared = db.clear_erro_publicados(title)
        if not cleared:
            self.send_json(404, {'error': 'no erro_upload found for this clip'})
            return

        self.send_json(200, {'ok': True, 'message': f'Clip "{title[:50]}" devolvido para a fila', 'cleared': cleared})

    def handle_clip_dismiss_erro(self, data):
        """Remove erro_upload entries so clip becomes pendente again."""
        live_id = data.get('live_video_id', '')
        title = data.get('title', '')
        if not live_id or not title:
            self.send_json(400, {'error': 'live_video_id and title required'})
            return

        cleared = db.clear_erro_publicados(title)
        self.send_json(200, {'ok': True, 'cleared': cleared})


def parse_duration_minutes(iso_duration):
    """Parse ISO 8601 duration like PT1H30M15S to minutes."""
    import re
    hours = re.search(r'(\d+)H', iso_duration)
    minutes = re.search(r'(\d+)M', iso_duration)
    seconds = re.search(r'(\d+)S', iso_duration)

    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if minutes:
        total += int(minutes.group(1))
    if seconds:
        total += int(seconds.group(1)) / 60

    return round(total)


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f'Dashboard rodando em http://localhost:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServidor encerrado.')
