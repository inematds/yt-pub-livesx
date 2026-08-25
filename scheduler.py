#!/usr/bin/env python3
"""
Scheduler para pipeline yt-pub-lives.
Roda em loop, checa a cada minuto se esta na hora de cortar ou publicar.
Le configuracao do banco SQLite local.
"""

import json
import os
import sys
import time
import subprocess
import base64
import tempfile
import urllib.request
import urllib.parse
import threading
from datetime import datetime

# Config
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get('GWS_CONFIG_DIR', os.path.join(PROJECT_ROOT, 'config'))
ENV_FILE = os.path.join(CONFIG_DIR, '.env')
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard', 'scheduler_status.json')

# Load env (before reading env-dependent vars)
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key] = val

LIVES_DIR = os.environ.get('LIVES_DIR', os.path.join(PROJECT_ROOT, 'lives'))

import db

AGNES_ENV = '/home/nmaldaner/projetos/agnes-nei/.env'


def _agnes_api_key():
    """AGNES_API_KEY lida em runtime do projeto agnes-nei (nao duplicamos a key)."""
    k = os.environ.get('AGNES_API_KEY', '')
    if k:
        return k
    try:
        with open(AGNES_ENV) as f:
            for line in f:
                if line.startswith('AGNES_API_KEY='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return ''


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', file=sys.stderr, flush=True)


def update_status(state, detail='', video_id='', step='', clip_id='', clip_title=''):
    """Escreve status atual do scheduler em JSON para o dashboard ler."""
    data = {
        'state': state,        # idle | cortando | publicando | erro
        'detail': detail,
        'video_id': video_id,
        'clip_id': clip_id,
        'clip_title': clip_title,
        'step': step,          # etapa atual: transcricao | analise | download | corte | thumbnail | upload
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def get_access_token():
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


def load_config():
    """Le CONFIG do banco local."""
    return db.load_config()


def get_pending_lives():
    """Retorna lives (mais antigas primeiro)."""
    return db.get_lives()


def get_matching_schedule(horarios_str):
    """Retorna o horario agendado que bate com agora, ou None.
    Suporta HH:00 (hora cheia) e HH:MM (minuto exato)."""
    if not horarios_str:
        return None
    now_hm = datetime.now().strftime('%H:%M')
    now_hour = datetime.now().strftime('%H:00')
    for h in horarios_str.split(','):
        h = h.strip()
        if h == now_hm:
            return h
        if h == now_hour:
            return h
    return None


def run_corte(video_id, config=None):
    """Executa yt-clip para uma live, atualizando status por etapa."""
    log(f'  Executando corte: {video_id}')
    update_status('cortando', f'Baixando transcricao...', video_id, step='transcricao')
    script = os.path.join(SCRIPTS_DIR, 'yt-clip')
    env = os.environ.copy()
    env['LIVES_DIR'] = LIVES_DIR
    env['PATH'] = f"{os.path.expanduser('~/.deno/bin')}:/usr/bin:{os.path.expanduser('~/.local/bin')}:{SCRIPTS_DIR}:{env.get('PATH', '')}"

    # Modo de analise: claude-api | anthropic-api | openrouter-api | agnes-api
    ai_mode = 'claude-api'
    if config:
        ai_mode = config.get('ai_mode', 'claude-api')
        ai_model = config.get('ai_model', '')
        if ai_model:
            env['AI_MODEL'] = ai_model
        if ai_mode == 'anthropic-api':
            key = config.get('anthropic_api_key', '')
            if key:
                env['ANTHROPIC_API_KEY'] = key
        elif ai_mode == 'openrouter-api':
            key = config.get('openrouter_api_key', '')
            if key:
                env['OPENROUTER_API_KEY'] = key
        elif ai_mode == 'agnes-api':
            # Chave do Agnes vem em runtime do projeto agnes-nei; o yt-clip
            # tambem sabe ler de la se nao vier no ambiente.
            key = _agnes_api_key()
            if key:
                env['AGNES_API_KEY'] = key

    proc = subprocess.Popen(
        [script, '--ai', ai_mode, '--', video_id],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env
    )

    output_lines = []
    last_logged_pct = -10
    step_map = {
        '[1/5]': ('transcricao', 'Baixando transcricao...'),
        '[2/5]': ('analise_transcript', 'Processando transcricao...'),
        '[3/5]': ('analise', 'Analisando topicos com IA...'),
        '[4/5]': ('corte', 'Baixando video e cortando clips...'),
        '[5/5]': ('publicacao', 'Finalizando...'),
    }

    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        output_lines.append(line)

        # Filtra linhas de download do yt-dlp (logar apenas a cada 10%)
        if '[download]' in line and '%' in line:
            try:
                pct = float(line.split('%')[0].split()[-1])
                if pct - last_logged_pct < 10:
                    continue
                last_logged_pct = pct
            except (ValueError, IndexError):
                pass

        log(f'    | {line}')
        # Detecta etapa pelo marcador [N/5]
        for marker, (step, label) in step_map.items():
            if marker in line:
                update_status('cortando', label, video_id, step=step)
                break

    proc.wait()

    if proc.returncode == 0:
        log(f'  Corte concluido: {video_id}')
        update_status('idle', f'Corte concluido: {video_id}', video_id)
        return True
    else:
        # Filtra linhas ruido (download% do yt-dlp) para que o tail capture o erro real
        meaningful = [l for l in output_lines if not ('[download]' in l and '%' in l)]
        last_output = '\n'.join(meaningful[-30:]) if meaningful else (output_lines[-5:] if output_lines else ['sem output'])
        if isinstance(last_output, list):
            last_output = '\n'.join(last_output)
        log(f'  Erro no corte:\n{last_output}')
        # Salva output completo em arquivo para debug
        try:
            err_dir = os.path.join(PROJECT_ROOT, 'data')
            os.makedirs(err_dir, exist_ok=True)
            err_file = os.path.join(err_dir, f'last_corte_error_{video_id}.log')
            with open(err_file, 'w') as f:
                f.write('\n'.join(output_lines))
        except Exception:
            pass
        update_status('erro', f'Erro no corte: {video_id}', video_id)
        return False


def refine_pub_with_ai(title, description, config, video_id=''):
    """Usa Claude CLI (OAuth) para refinar titulo e descricao antes de publicar."""
    prompt_file = os.path.join(CONFIG_DIR, 'prompt_pub.txt')
    if not os.path.exists(prompt_file):
        return title, description

    with open(prompt_file) as f:
        system_prompt = f.read().strip()
    if not system_prompt:
        return title, description

    user_msg = f'Titulo original: "{title}"\nDescricao original: "{description}"\nVideo ID da live original: {video_id}'
    full_prompt = f'{system_prompt}\n\n---\n\n{user_msg}'

    try:
        log(f'  Refinando titulo/descricao com Claude CLI...')
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)
        result = subprocess.run(
            ['claude', '-p', '--output-format', 'json', full_prompt],
            capture_output=True, text=True, timeout=120, env=env
        )
        if result.returncode != 0:
            log(f'  Claude CLI erro (code {result.returncode}): {result.stderr[:200]}, usando originais')
            return title, description

        data = json.loads(result.stdout)
        content = data.get('result', '')

        import re
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            refined = json.loads(json_match.group())
            new_title = refined.get('title', title)
            new_desc = refined.get('description', description)
            log(f'  Titulo refinado: {new_title[:60]}')
            return new_title, new_desc
        else:
            log(f'  IA nao retornou JSON valido, usando originais')
            return title, description
    except Exception as e:
        log(f'  Erro ao refinar com IA: {e}, usando originais')
        return title, description


def run_publicacao(video_id, clip_file, title, description, tags, privacy):
    """Executa yt-publish para um clip."""
    log(f'  Publicando: {title[:60]}')
    log(f'  Arquivo: {clip_file} ({os.path.getsize(clip_file) / 1024 / 1024:.1f} MB)')
    update_status('publicando', f'Publicando: {title[:50]}', video_id, step='upload')
    script = os.path.join(SCRIPTS_DIR, 'yt-publish')
    env = os.environ.copy()
    env['PATH'] = f"{os.path.expanduser('~/.deno/bin')}:/usr/bin:{os.path.expanduser('~/.local/bin')}:{SCRIPTS_DIR}:{env.get('PATH', '')}"

    cmd = [script, clip_file, '--title', title, '--description', description, '--privacy', privacy]
    if tags:
        cmd += ['--tags', tags]

    log(f'  CMD: {" ".join(cmd[:3])} ...')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)

    output_lines = []
    video_id_result = None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            output_lines.append(line)
            log(f'    | {line}')
            if 'Video ID:' in line:
                video_id_result = line.split('Video ID:')[1].strip()

        proc.wait(timeout=600)  # 10 min max per upload
    except subprocess.TimeoutExpired:
        log(f'  TIMEOUT: publicacao excedeu 10 min, matando processo')
        proc.kill()
        proc.wait()
        return None

    if proc.returncode == 0:
        if video_id_result:
            return video_id_result
        log(f'  Publicado mas sem video ID no output')
        return 'unknown'
    else:
        last_output = '\n'.join(output_lines[-5:]) if output_lines else 'sem output'
        log(f'  Erro na publicacao: {last_output}')
        return None


def upload_thumbnail(video_id, thumb_path):
    """Upload thumbnail to YouTube using thumbnails.set API."""
    token = get_access_token()
    url = f'https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}&uploadType=media'

    with open(thumb_path, 'rb') as f:
        img_data = f.read()

    req = urllib.request.Request(url, data=img_data, method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'image/jpeg')
    req.add_header('Content-Length', str(len(img_data)))

    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read())
    log(f'  Thumbnail uploaded for {video_id}')
    return result


def _add_pending_thumb(video_id, title):
    """Add a thumbnail to the pending list for later upload."""
    pending_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lives', 'pending_thumbs.json')
    pending = []
    if os.path.exists(pending_file):
        try:
            with open(pending_file) as f:
                pending = json.load(f)
        except Exception:
            pending = []
    # Avoid duplicates
    if not any(p['id'] == video_id for p in pending):
        pending.append({'id': video_id, 'title': title})
        with open(pending_file, 'w') as f:
            json.dump(pending, f, indent=2, ensure_ascii=False)


def _apply_saved_preset(preset_name, config, yt_thumb):
    """Aplica preset salvo (customizado) ou hardcoded."""
    saved = config.get(f'preset_{preset_name}', '')
    if saved:
        try:
            preset_data = json.loads(saved)
            # Mapear campos JS para env vars
            field_map = {
                'font': 'DESIGN_FONT', 'fontSize': 'DESIGN_FONT_SIZE', 'lastLineScale': 'DESIGN_LAST_LINE_SCALE',
                'lineHeight': 'DESIGN_LINE_HEIGHT', 'tracking': 'DESIGN_TRACKING', 'case': 'DESIGN_CASE',
                'textColor': 'DESIGN_TEXT_COLOR', 'highlightColor': 'DESIGN_HIGHLIGHT_COLOR',
                'highlightEnabled': 'DESIGN_HIGHLIGHT_ENABLED', 'accentColor': 'DESIGN_ACCENT_COLOR',
                'accentWidth': 'DESIGN_ACCENT_WIDTH', 'accentHeight': 'DESIGN_ACCENT_HEIGHT',
                'accentGap': 'DESIGN_ACCENT_GAP', 'accentEnabled': 'DESIGN_ACCENT_ENABLED',
                'strokeEnabled': 'DESIGN_STROKE_ENABLED', 'strokeColor': 'DESIGN_STROKE_COLOR',
                'strokeSize': 'DESIGN_STROKE_SIZE', 'shadowType': 'DESIGN_SHADOW_TYPE',
                'shadowColor': 'DESIGN_SHADOW_COLOR', 'shadowSize': 'DESIGN_SHADOW_SIZE',
                'shadowOpacity': 'DESIGN_SHADOW_OPACITY', 'gradient': 'DESIGN_GRADIENT',
                'gradientOpacity': 'DESIGN_GRADIENT_OPACITY', 'gradientCoverage': 'DESIGN_GRADIENT_COVERAGE',
                'brand': 'DESIGN_BRAND', 'brandFont': 'DESIGN_BRAND_FONT', 'brandSize': 'DESIGN_BRAND_SIZE',
                'brandColor': 'DESIGN_BRAND_COLOR', 'brandPosition': 'DESIGN_BRAND_POSITION',
                'position': 'DESIGN_POSITION'
            }
            for js_key, env_key in field_map.items():
                if js_key in preset_data:
                    os.environ[env_key] = str(preset_data[js_key])
            log(f'  Preset customizado: {preset_name}')
            return
        except Exception:
            pass
    # Fallback para preset hardcoded
    if preset_name in yt_thumb.PRESETS:
        for k, v in yt_thumb.PRESETS[preset_name].items():
            os.environ[k] = v
    log(f'  [thumb] Preset ativo: {preset_name}')


def handle_thumbnail(video_id, title, description, config, thumb_override=None):
    """
    Generate and upload thumbnail based on config thumb_mode.
    thumb_override: caminho de uma capa ja pronta (manifest.json do import).
    Quando presente, pula a geracao e sobe esse arquivo — inclusive com
    thumb_mode=none.
    """
    thumb_mode = config.get('thumb_mode', 'none')
    use_override = bool(thumb_override) and os.path.exists(thumb_override)
    if thumb_override and not use_override:
        log(f'  [thumb] capa do manifesto sumiu ({thumb_override}), gerando normal')
    if thumb_mode == 'none' and not use_override:
        return

    thumb_path = f'/tmp/yt_thumb_{video_id}.jpg'

    try:
        if use_override:
            # Copia para /tmp: o bloco de upload remove thumb_path no fim
            import shutil
            shutil.copy2(thumb_override, thumb_path)
            log(f'  [thumb] Usando capa do manifesto: {os.path.basename(thumb_override)}')

        elif thumb_mode == 'api':
            # Set API key, model and visual config before importing
            api_key = config.get('thumb_api_key', '')
            model = config.get('thumb_model', 'dreamshaper')
            agnes_key = _agnes_api_key()
            if agnes_key:
                os.environ['AGNES_API_KEY'] = agnes_key
            os.environ['THUMB_MODEL'] = model
            # Image provider
            img_provider = config.get('thumb_image_provider', 'agnes')
            os.environ['THUMB_IMAGE_PROVIDER'] = img_provider
            kie_key = config.get('kie_api_key', '')
            if kie_key:
                os.environ['KIE_API_KEY'] = kie_key
            minimax_key = config.get('minimax_api_key', '')
            if minimax_key:
                os.environ['MINIMAX_API_KEY'] = minimax_key
            google_img_key = config.get('google_image_api_key', '')
            if google_img_key:
                os.environ['GOOGLE_IMAGE_API_KEY'] = google_img_key
            inemaimg_url = config.get('inemaimg_url', '')
            if inemaimg_url:
                os.environ['INEMAIMG_URL'] = inemaimg_url
            google_img_model = config.get('google_image_model', '')
            if google_img_model:
                os.environ['GOOGLE_IMAGE_MODEL'] = google_img_model
            # API keys dos providers
            or_key = config.get('openrouter_api_key', '')
            if or_key:
                os.environ['OPENROUTER_API_KEY'] = or_key
            ant_key = config.get('anthropic_api_key', '')
            if ant_key:
                os.environ['ANTHROPIC_API_KEY'] = ant_key
            # LLM chain (3 tentativas)
            for i in range(1, 4):
                p = config.get(f'thumb_llm_{i}_provider', '')
                m = config.get(f'thumb_llm_{i}_model', '')
                if p:
                    os.environ[f'THUMB_LLM_{i}_PROVIDER'] = p
                if m:
                    os.environ[f'THUMB_LLM_{i}_MODEL'] = m
            # Visual settings
            for key in ('thumb_font_size', 'thumb_text_color', 'thumb_accent_color',
                        'thumb_brand_color', 'thumb_text_position', 'thumb_brand'):
                val = config.get(key, '')
                if val:
                    os.environ[key.upper()] = val
            # Design config
            for key in ('design_font', 'design_font_size', 'design_last_line_scale',
                        'design_line_height', 'design_tracking', 'design_case',
                        'design_text_color', 'design_highlight_color', 'design_highlight_enabled',
                        'design_accent_color', 'design_accent_width',
                        'design_accent_height', 'design_accent_gap', 'design_accent_enabled',
                        'design_stroke_enabled', 'design_stroke_color', 'design_stroke_size',
                        'design_shadow_type', 'design_shadow_color', 'design_shadow_size',
                        'design_shadow_opacity', 'design_gradient', 'design_gradient_opacity',
                        'design_gradient_coverage', 'design_brand', 'design_brand_font',
                        'design_brand_size', 'design_brand_color', 'design_brand_position',
                        'design_position'):
                val = config.get(key, '')
                if val:
                    os.environ[key.upper()] = val

            # Random preset: se tem presets selecionados, sorteia um
            import random
            random_presets_str = config.get('design_random_presets', '')
            if random_presets_str:
                random_list = [p.strip() for p in random_presets_str.split(',') if p.strip()]
                if random_list:
                    chosen = random.choice(random_list)
                    os.environ['DESIGN_RANDOM_PRESET'] = chosen
                    # Carregar preset customizado se existir
                    saved = config.get(f'preset_{chosen}', '')
                    if saved:
                        os.environ['DESIGN_SAVED_PRESET'] = saved
                    log(f'  Random preset: {chosen} (de {len(random_list)} opcoes)')

            # Import generate_thumbnail from scripts/yt-thumbnail
            import types
            script_path = os.path.join(SCRIPTS_DIR, 'yt-thumbnail')
            yt_thumb = types.ModuleType('yt_thumbnail')
            yt_thumb.__file__ = script_path
            with open(script_path) as _f:
                exec(compile(_f.read(), script_path, 'exec'), yt_thumb.__dict__)

            log(f'  Generating API thumbnail for {video_id} (model: {model})')
            try:
                yt_thumb.generate_thumbnail(title, description, thumb_path)
            except Exception as api_err:
                log(f'  API thumbnail failed: {api_err}, using fallback')
                _apply_saved_preset('fallback', config, yt_thumb)
                bg = yt_thumb.create_gradient_bg()
                yt_thumb.compose_thumbnail(bg, title[:70], '', thumb_path)

        elif thumb_mode == 'local':
            # Local Pillow-based: extract frame from video + overlay text
            log(f'  Generating local thumbnail for {video_id}')
            from PIL import Image, ImageDraw, ImageFont

            # Create simple gradient background with text overlay
            img = Image.new('RGB', (1280, 720))
            for y in range(720):
                r = int(10 + 10 * y / 720)
                g = int(10 + 5 * y / 720)
                b = int(30 + 20 * y / 720)
                for x in range(1280):
                    img.putpixel((x, y), (r, g, b))

            draw = ImageDraw.Draw(img)
            font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
            try:
                font = ImageFont.truetype(font_path, 64)
            except Exception:
                font = ImageFont.load_default()

            # Wrap and draw title text
            words = title[:70].upper().split()
            lines, current = [], ''
            for word in words:
                test = (current + ' ' + word).strip()
                bbox = draw.textbbox((0, 0), test, font=font)
                if bbox[2] - bbox[0] > 1000 and current:
                    lines.append(current)
                    current = word
                else:
                    current = test
            if current:
                lines.append(current)

            y_pos = 80
            for line in lines[:3]:
                # Shadow
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        draw.text((130 + dx, y_pos + dy), line, font=font, fill=(0, 0, 0))
                draw.text((130, y_pos), line, font=font, fill=(255, 255, 255))
                y_pos += 80

            img.save(thumb_path, 'JPEG', quality=92)

        elif thumb_mode == 'fallback':
            # Fallback: sempre usa preset "fallback"
            log(f'  Generating fallback thumbnail for {video_id}')

            import types
            script_path = os.path.join(SCRIPTS_DIR, 'yt-thumbnail')
            yt_thumb = types.ModuleType('yt_thumbnail')
            yt_thumb.__file__ = script_path
            with open(script_path) as _f:
                exec(compile(_f.read(), script_path, 'exec'), yt_thumb.__dict__)

            _apply_saved_preset('fallback', config, yt_thumb)

            bg = yt_thumb.create_gradient_bg()
            yt_thumb.compose_thumbnail(bg, title[:70], '', thumb_path)

        else:
            log(f'  Unknown thumb_mode: {thumb_mode}, skipping')
            return

        # Save a copy to lives/thumbs/ for future reference
        if os.path.exists(thumb_path):
            thumbs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lives', 'thumbs')
            os.makedirs(thumbs_dir, exist_ok=True)
            import shutil
            saved_path = os.path.join(thumbs_dir, f'{video_id}.jpg')
            shutil.copy2(thumb_path, saved_path)

            # Upload to YouTube
            try:
                upload_thumbnail(video_id, thumb_path)
            except Exception as upload_err:
                log(f'  Thumbnail upload failed, saved as pending: {upload_err}')
                _add_pending_thumb(video_id, title)
            # Clean up temp file
            try:
                os.remove(thumb_path)
            except OSError:
                pass

    except Exception as e:
        log(f'  Thumbnail error (non-fatal): {e}')


def update_video_metadata(video_id, title, description):
    """Update video title and description on YouTube via Data API v3."""
    token = get_access_token()
    body = {
        'id': video_id,
        'snippet': {
            'title': title,
            'description': description,
            'categoryId': '22'
        }
    }
    url = 'https://www.googleapis.com/youtube/v3/videos?part=snippet'
    req_data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=req_data, method='PUT')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')

    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read())
    log(f'  YouTube metadata updated for {video_id}: {title[:60]}')
    return result


def generate_enrich_thumbnail(title, config):
    """Generate thumbnail with default background + text overlay using Design Thumb settings."""
    import types
    import random
    from PIL import Image

    default_bg_path = os.path.join(CONFIG_DIR, 'thumb_default.jpg')

    # Load or generate default background
    if os.path.exists(default_bg_path):
        bg = Image.open(default_bg_path).resize((1280, 720), Image.LANCZOS).convert('RGB')
    else:
        bg = Image.new('RGB', (1280, 720))
        for y in range(720):
            r = int(26 - 11 * y / 720)
            g = int(26 - 11 * y / 720)
            b = int(46 - 11 * y / 720)
            for x in range(1280):
                bg.putpixel((x, y), (max(r, 0), max(g, 0), max(b, 0)))
        bg.save(default_bg_path, 'JPEG', quality=92)
        log(f'  Generated default thumbnail background: {default_bg_path}')

    # Apply Design Thumb config from database (same as handle_thumbnail)
    for key in ('design_font', 'design_font_size', 'design_last_line_scale',
                'design_line_height', 'design_tracking', 'design_case',
                'design_text_color', 'design_highlight_color', 'design_highlight_enabled',
                'design_accent_color', 'design_accent_width',
                'design_accent_height', 'design_accent_gap', 'design_accent_enabled',
                'design_stroke_enabled', 'design_stroke_color', 'design_stroke_size',
                'design_shadow_type', 'design_shadow_color', 'design_shadow_size',
                'design_shadow_opacity', 'design_gradient', 'design_gradient_opacity',
                'design_gradient_coverage', 'design_brand', 'design_brand_font',
                'design_brand_size', 'design_brand_color', 'design_brand_position',
                'design_position'):
        val = config.get(key, '')
        if val:
            os.environ[key.upper()] = val

    # Random preset: if configured, pick one
    random_presets_str = config.get('design_random_presets', '')
    if random_presets_str:
        random_list = [p.strip() for p in random_presets_str.split(',') if p.strip()]
        if random_list:
            chosen = random.choice(random_list)
            os.environ['DESIGN_RANDOM_PRESET'] = chosen
            saved = config.get(f'preset_{chosen}', '')
            if saved:
                os.environ['DESIGN_SAVED_PRESET'] = saved
            log(f'  Enrich thumb preset: {chosen}')

    # Load yt-thumbnail module
    script_path = os.path.join(SCRIPTS_DIR, 'yt-thumbnail')
    yt_thumb = types.ModuleType('yt_thumbnail')
    yt_thumb.__file__ = script_path
    with open(script_path) as _f:
        exec(compile(_f.read(), script_path, 'exec'), yt_thumb.__dict__)

    # If no random preset was set, apply fallback
    if not random_presets_str:
        _apply_saved_preset('fallback', config, yt_thumb)

    # Compose thumbnail with title text
    thumb_path = f'/tmp/yt_enrich_{int(time.time())}.jpg'
    yt_thumb.compose_thumbnail(bg, title[:70], '', thumb_path)
    return thumb_path


def enrich_live_with_ai(video_id, data_live, duracao_min, transcript_text, config):
    """Use AI to generate title and description based on the live's transcript."""
    prompt_file = os.path.join(CONFIG_DIR, 'prompt_enrich.txt')
    if not os.path.exists(prompt_file):
        log(f'  prompt_enrich.txt not found, skipping AI enrichment')
        return None, None

    with open(prompt_file) as f:
        system_prompt = f.read().strip()
    if not system_prompt:
        return None, None

    user_msg = (
        f'Video ID: {video_id}\n'
        f'Data da live: {data_live}\n'
        f'Duracao: {duracao_min} minutos\n\n'
        f'=== TRANSCRICAO DA LIVE ===\n{transcript_text}'
    )
    full_prompt = f'{system_prompt}\n\n---\n\n{user_msg}'

    try:
        log(f'  Gerando titulo/descricao com IA para {video_id}...')
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)
        result = subprocess.run(
            ['claude', '-p', '--output-format', 'json', full_prompt],
            capture_output=True, text=True, timeout=180, env=env
        )
        if result.returncode != 0:
            log(f'  Claude CLI erro (code {result.returncode}): {result.stderr[:200]}')
            return None, None

        data = json.loads(result.stdout)
        content = data.get('result', '')

        import re
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            refined = json.loads(json_match.group())
            new_title = refined.get('title', '')
            new_desc = refined.get('description', '')
            if new_title:
                log(f'  Titulo gerado: {new_title[:60]}')
                return new_title, new_desc
        log(f'  IA nao retornou JSON valido')
        return None, None
    except Exception as e:
        log(f'  Erro ao gerar com IA: {e}')
        return None, None


def _enrich_single_live(vid, live, config):
    """Enrich a single live: read transcript → AI title/desc → thumbnail → YouTube update.
    Returns True on success, False on error."""
    data_live = live.get('data_live', '')
    duracao = live.get('duracao_min', '0')

    # Read transcript
    condensed_file = os.path.join(LIVES_DIR, vid, 'condensed.txt')
    if not os.path.exists(condensed_file):
        log(f'  Enrich: transcricao nao encontrada para {vid}')
        return False

    with open(condensed_file) as f:
        transcript = f.read()
    if not transcript:
        log(f'  Enrich: transcricao vazia para {vid}')
        return False

    # Generate title + description with AI
    update_status('enriquecendo', f'Gerando titulo/descricao: {vid}', vid, step='analise')
    new_title, new_desc = enrich_live_with_ai(vid, data_live, duracao, transcript, config)
    if not new_title:
        log(f'  Enrich: IA nao gerou titulo para {vid}')
        return False

    # Update on YouTube (title + description)
    try:
        update_video_metadata(vid, new_title, new_desc or '')
    except Exception as yt_err:
        log(f'  Enrich: erro ao atualizar YouTube para {vid}: {yt_err}')
        # Still update local DB even if YouTube fails (e.g. wrong channel)

    # Generate and upload thumbnail
    try:
        update_status('enriquecendo', f'Thumbnail: {vid}', vid, step='thumbnail')
        thumb_path = generate_enrich_thumbnail(new_title, config)
        if thumb_path and os.path.exists(thumb_path):
            thumbs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lives', 'thumbs')
            os.makedirs(thumbs_dir, exist_ok=True)
            import shutil
            shutil.copy2(thumb_path, os.path.join(thumbs_dir, f'{vid}.jpg'))
            try:
                upload_thumbnail(vid, thumb_path)
            except Exception as upload_err:
                log(f'  Enrich: thumbnail upload failed (non-fatal): {upload_err}')
            try:
                os.remove(thumb_path)
            except OSError:
                pass
    except Exception as thumb_err:
        log(f'  Enrich: thumbnail error for {vid} (non-fatal): {thumb_err}')

    # Update local database
    db.update_live(vid, titulo=new_title, observacoes='enriquecida')
    log(f'  Enrich OK: {vid} -> {new_title[:60]}')
    return True


def process_enrich(config):
    """Manual enrich: if no cuts exist, run cuts first, then enrich."""
    max_por_vez = int(config.get('enrich_max_por_vez', '3'))
    lives = get_pending_lives()

    # Filter: lives with generic title OR marked for re-enrichment
    genericas = [
        l for l in lives
        if not (l.get('video_id', '') or '').startswith('import_')
        and (
            (l.get('titulo', '').strip().upper() == 'INEMA' and l.get('observacoes', '') != 'enriquecida')
            or l.get('observacoes', '') == 'refazer_enrich'
        )
    ]

    if not genericas:
        log('  Nenhuma live para enriquecer')
        return {'enriched': 0, 'errors': 0}

    log(f'  {len(genericas)} lives para enriquecer, processando ate {max_por_vez}')
    update_status('enriquecendo', f'Enriquecendo {min(len(genericas), max_por_vez)} lives...')

    enriched = 0
    errors = 0

    for live in genericas[:max_por_vez]:
        vid = live.get('video_id', '')

        try:
            # If no transcript yet, run full cut process first
            condensed_file = os.path.join(LIVES_DIR, vid, 'condensed.txt')
            if not os.path.exists(condensed_file):
                log(f'  Sem transcricao para {vid}, rodando corte primeiro...')
                update_status('enriquecendo', f'Cortando: {vid}', vid, step='corte')
                success = run_corte(vid, config)
                if success:
                    job_dir = os.path.join(LIVES_DIR, vid)
                    topics_file = os.path.join(job_dir, 'topics.json')
                    clips_dir = os.path.join(job_dir, 'clips')
                    qtd_clips = 0
                    if os.path.exists(topics_file):
                        with open(topics_file) as f:
                            topics = json.load(f)
                        qtd_clips = len(topics.get('topics', []))
                    mp4s = [f for f in os.listdir(clips_dir) if f.endswith('.mp4')] if os.path.isdir(clips_dir) else []
                    has_clips = len(mp4s) > 0
                    update_live_status(vid, 'status_transcricao', 'transcricao', {
                        'status_cortes': 'concluido' if has_clips else 'pendente',
                        'qtd_clips': qtd_clips,
                        'clips_disco': str(len(mp4s)),
                        'data_corte': datetime.now().strftime('%Y-%m-%d %H:%M')
                    })
                else:
                    update_live_status(vid, 'status_cortes', 'erro')
                    log(f'  Corte falhou para {vid}, pulando enrich')
                    errors += 1
                    continue

            # Now enrich using the transcript
            if _enrich_single_live(vid, live, config):
                enriched += 1
            else:
                errors += 1

        except Exception as e:
            log(f'  Erro ao enriquecer {vid}: {e}')
            errors += 1

    update_status('idle', f'Enrich concluido: {enriched} OK, {errors} erros')
    return {'enriched': enriched, 'errors': errors}


def update_live_status(video_id, status_field, new_status, extra=None):
    """Atualiza status de uma live no banco local."""
    fields = {status_field: new_status}
    if extra:
        fields.update(extra)
    db.update_live(video_id, **fields)


def process_cortes(config):
    """Processa cortes de lives pendentes."""
    max_per_run = int(config.get('corte_max_por_dia', '3'))
    lives = get_pending_lives()

    pendentes = [l for l in lives
                 if l.get('status_cortes') not in ('concluido', 'erro', 'sem_topicos')]
    if not pendentes:
        log('  Nenhuma live pendente para cortar')
        return

    log(f'  {len(pendentes)} lives pendentes, processando ate {max_per_run}')

    for live in pendentes[:max_per_run]:
        vid = live.get('video_id', '')
        if not vid:
            continue

        success = run_corte(vid, config)
        if success:
            job_dir = os.path.join(LIVES_DIR, vid)
            topics_file = os.path.join(job_dir, 'topics.json')
            clips_dir = os.path.join(job_dir, 'clips')

            qtd_clips = 0
            if os.path.exists(topics_file):
                with open(topics_file) as f:
                    topics = json.load(f)
                qtd_clips = len(topics.get('topics', []))

            mp4s = [f for f in os.listdir(clips_dir) if f.endswith('.mp4')] if os.path.isdir(clips_dir) else []
            has_clips = len(mp4s) > 0

            # Rodou, a analise concluiu e nao ha topico para cortar: nao adianta
            # tentar de novo — o topics.json ja existe e o yt-clip pula a analise.
            # Sem isso a live volta pra fila toda janela e bloqueia as outras
            # (aconteceu com uma live de abril, 68 tentativas seguidas).
            if has_clips:
                novo_status = 'concluido'
            elif qtd_clips == 0 and os.path.exists(topics_file):
                novo_status = 'sem_topicos'
                log(f'  {vid}: analise nao encontrou topicos — marcado como sem_topicos')
            else:
                novo_status = 'pendente'

            update_live_status(vid, 'status_transcricao', 'transcricao', {
                'status_cortes': novo_status,
                'qtd_clips': qtd_clips,
                'clips_disco': str(len(mp4s)),
                'data_corte': datetime.now().strftime('%Y-%m-%d %H:%M')
            })

            # If enrich is enabled, enrich after cutting
            enrich_auto = config.get('enrich_auto', 'false') == 'true'
            enrich_paused = config.get('pipeline_enrich_paused', 'false') == 'true'
            needs_enrich = (
                live.get('titulo', '').strip().upper() == 'INEMA'
                and live.get('observacoes', '') != 'enriquecida'
            )
            if enrich_auto and not enrich_paused and needs_enrich:
                log(f'  Enrich auto: enriquecendo {vid} apos corte...')
                _enrich_single_live(vid, live, config)
        else:
            update_live_status(vid, 'status_cortes', 'erro')


_pub_lock = threading.Lock()

_import_pub_lock = threading.Lock()

def process_publicacao(config):
    """Publica clips de lives (exclui imports)."""
    if not _pub_lock.acquire(blocking=False):
        log('  Publicacao ja em andamento, pulando')
        return
    try:
        _process_publicacao_inner(config)
    finally:
        _pub_lock.release()

_tiktok_pub_lock = threading.Lock()

def process_publicacao_imports(config):
    """Publica clips de imports normais (nao TikTok)."""
    if not _import_pub_lock.acquire(blocking=False):
        log('  Publicacao de imports ja em andamento, pulando')
        return
    try:
        privacy = config.get('import_privacy', '') or config.get('privacy_padrao', 'unlisted')
        max_por_vez = int(config.get('import_pub_max_por_vez', '1') or '1')
        all_imports = [l for l in get_pending_lives() if l.get('video_id', '').startswith('import_')]
        lives = [l for l in all_imports if not (l.get('titulo', '') or '').startswith('TikTok @')]
        _publish_import_list('import', lives, max_por_vez, privacy, config)
    finally:
        _import_pub_lock.release()

def process_publicacao_tiktok(config):
    """Publica clips de TikTok."""
    if not _tiktok_pub_lock.acquire(blocking=False):
        log('  Publicacao de TikTok ja em andamento, pulando')
        return
    try:
        privacy = config.get('tiktok_privacy', '') or config.get('privacy_padrao', 'unlisted')
        max_por_vez = int(config.get('tiktok_pub_max_por_vez', '1') or '1')
        all_imports = [l for l in get_pending_lives() if l.get('video_id', '').startswith('import_')]
        lives = [l for l in all_imports if (l.get('titulo', '') or '').startswith('TikTok @')]
        _publish_import_list('tiktok', lives, max_por_vez, privacy, config)
    finally:
        _tiktok_pub_lock.release()

def _publish_import_list(label, lives, max_por_vez, privacy, config):
    found_any = False
    global_count = 0
    for live in lives:
        if global_count >= max_por_vez:
            break
        vid = live.get('video_id', '')
        qtd_clips = int(live.get('qtd_clips', '0') or '0')
        publicados_count = int(live.get('clips_publicados', '0') or '0')

        if live.get('status_cortes') != 'concluido' or not vid:
            continue
        if publicados_count >= qtd_clips or qtd_clips == 0:
            continue

        obs = live.get('observacoes', '')
        import re as _re
        pa_match = _re.search(r'publish_at=(\d{2}:\d{2})', obs)
        if pa_match:
            publish_at = pa_match.group(1)
            now_hm = datetime.now().strftime('%H:%M')
            if now_hm < publish_at:
                log(f'  [{label}] {vid}: aguardando publish_at={publish_at} (agora={now_hm}), pulando')
                continue

        found_any = True
        log(f'  [{label}] {vid}: {qtd_clips} clips, {publicados_count} publicados')

        job_dir = os.path.join(LIVES_DIR, vid)
        manifest_file = os.path.join(job_dir, 'clips_manifest.json')
        if not os.path.exists(manifest_file):
            log(f'  Sem manifest para {vid}, pulando')
            continue

        with open(manifest_file) as f:
            clips = json.load(f)

        pub_records = db.get_publicados(live_video_id=vid)
        published_ids = set()
        pub_ok = 0
        pub_erro = 0
        for p in pub_records:
            vid_status = p.get('clip_video_id', '')
            cid = p.get('filename', '')
            if vid_status in ('erro_upload', 'publicando', ''):
                pub_erro += 1
            else:
                pub_ok += 1
            if cid:
                published_ids.add(cid)

        count = 0
        for clip in clips:
            if global_count >= max_por_vez:
                break

            clip_id = f'{vid}_{clip.get("index", 0)}'
            if clip_id in published_ids:
                continue
            if clip.get('paused', False):
                continue
            if not os.path.exists(clip['file']):
                log(f'  Arquivo nao encontrado: {clip["file"]}')
                continue

            clip_title = clip['title']
            clip_desc = clip.get('description', '') or clip.get('title', '')
            clip_privacy = clip.get('privacy', privacy)

            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            lock_row_id = db.add_publicado({
                'clip_video_id': 'publicando',
                'clip_titulo': clip_title,
                'clip_url': '',
                'live_video_id': vid,
                'live_titulo': live.get('titulo', ''),
                'data_publicacao': now,
                'privacy': clip_privacy,
                'duracao': str(clip.get('duration', '')),
                'tags': ','.join(clip.get('tags', [])) if isinstance(clip.get('tags'), list) else clip.get('tags', ''),
                'categoria': '27',
                'filename': clip_id
            })

            update_status('publicando', f'[{label}] Enviando: {clip_title[:50]}', vid, step='upload', clip_title=clip_title[:50])
            new_vid = run_publicacao(vid, clip['file'], clip_title, clip_desc,
                                     ','.join(clip.get('tags', [])) if isinstance(clip.get('tags'), list) else clip.get('tags', ''),
                                     clip_privacy)

            if new_vid:
                update_status('publicando', f'[{label}] Thumbnail...', vid, step='thumbnail', clip_id=new_vid)
                handle_thumbnail(new_vid, clip_title, clip_desc, config,
                                 thumb_override=clip.get('thumb_file'))
                db.update_publicado(lock_row_id,
                    clip_video_id=new_vid,
                    clip_titulo=clip_title,
                    clip_url=f'https://www.youtube.com/watch?v={new_vid}'
                )
                count += 1
                global_count += 1
                pub_ok += 1
                published_ids.add(clip_id)
                log(f'  [{label}] Publicado: {clip_title[:50]} -> {new_vid}')
            else:
                db.update_publicado(lock_row_id, clip_video_id='erro_upload')
                count += 1
                global_count += 1
                pub_erro += 1
                published_ids.add(clip_id)
                log(f'  [{label}] Falha: {clip_title[:50]}')

        if pub_ok != publicados_count or pub_erro:
            # pendente = ainda na fila (exclui publicados OK e os com erro)
            pend = max(0, qtd_clips - pub_ok - pub_erro)
            update_live_status(vid, 'clips_publicados', str(pub_ok), {'clips_pendentes': str(pend)})

    if not found_any:
        log(f'  [{label}] Nenhum pendente para publicar')

def _process_publicacao_inner(config):
    privacy = config.get('privacy_padrao', 'unlisted')
    max_por_vez = int(config.get('pub_max_por_vez', '2') or '2')
    log(f'  Buscando lives para publicar (privacy={privacy}, max={max_por_vez})...')
    lives = get_pending_lives()
    log(f'  {len(lives)} lives encontradas')

    # Find lives with clips but not all published
    found_any = False
    for live in lives:
        vid = live.get('video_id', '')
        if live.get('status_cortes') != 'concluido' or not vid:
            continue

        qtd_clips = int(live.get('qtd_clips', '0') or '0')
        publicados_count = int(live.get('clips_publicados', '0') or '0')

        if publicados_count >= qtd_clips or qtd_clips == 0:
            continue

        # Imports tem fila propria — excluir daqui
        if vid.startswith('import_'):
            continue

        found_any = True
        log(f'  Live {vid}: {qtd_clips} clips, {publicados_count} publicados')

        job_dir = os.path.join(LIVES_DIR, vid)
        manifest_file = os.path.join(job_dir, 'clips_manifest.json')

        if not os.path.exists(manifest_file):
            log(f'  Sem manifest para {vid}, pulando')
            continue

        with open(manifest_file) as f:
            clips = json.load(f)

        # Build set of clip_ids already in the DB (by filename field)
        pub_records = db.get_publicados(live_video_id=vid)
        published_ids = set()
        pub_ok = 0
        pub_erro = 0
        for p in pub_records:
            vid_status = p.get('clip_video_id', '')
            cid = p.get('filename', '')
            if vid_status in ('erro_upload', 'publicando', ''):
                pub_erro += 1
            else:
                pub_ok += 1
            if cid:
                published_ids.add(cid)

        count = 0
        log(f'  {len(clips)} clips no manifest, {pub_ok} publicados OK, {pub_erro} com erro')
        for clip in clips:
            if count >= max_por_vez:
                log(f'  Limite de {max_por_vez} clips por vez atingido')
                break

            # Unique clip_id: live_video_id + index
            clip_id = f'{vid}_{clip.get("index", 0)}'

            # Skip if already published or in error
            if clip_id in published_ids:
                continue

            if clip.get('paused', False):
                log(f'  Pausado: {clip["title"][:50]}')
                continue

            if not os.path.exists(clip['file']):
                log(f'  Arquivo nao encontrado: {clip["file"]}')
                continue

            clip_title = clip['title']
            clip_desc = clip.get('description', '') or clip.get('title', '')

            # Lock no banco: marcar como "publicando" ANTES de iniciar
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            lock_row_id = db.add_publicado({
                'clip_video_id': 'publicando',
                'clip_titulo': clip_title,
                'clip_url': '',
                'live_video_id': vid,
                'live_titulo': live.get('titulo', ''),
                'data_publicacao': now,
                'privacy': privacy,
                'duracao': str(clip.get('duration', '')),
                'tags': ','.join(clip.get('tags', [])),
                'categoria': '27',
                'filename': clip_id
            })
            log(f'  Reservado no banco: {clip_title[:50]} (id: {clip_id})')

            # Refinar titulo e descricao com IA
            update_status('publicando', f'Refinando com IA: {clip_title[:50]}', vid, step='refine', clip_title=clip_title[:50])
            clip_title, clip_desc = refine_pub_with_ai(clip_title, clip_desc, config, video_id=vid)

            # Append link da live original (se ativado na config)
            if config.get('pub_link_live', 'true') == 'true':
                clip_desc += f'\n\nLive original: https://www.youtube.com/watch?v={vid}'

            update_status('publicando', f'Enviando: {clip_title[:50]}', vid, step='upload', clip_title=clip_title[:50])
            new_vid = run_publicacao(
                vid, clip['file'], clip_title,
                clip_desc, ','.join(clip.get('tags', [])),
                privacy
            )

            if new_vid:
                update_status('publicando', f'Gerando thumbnail...', vid, step='thumbnail', clip_id=new_vid, clip_title=clip_title[:50])
                handle_thumbnail(
                    new_vid, clip_title,
                    clip.get('description', ''), config
                )

                db.update_publicado(lock_row_id,
                    clip_video_id=new_vid,
                    clip_titulo=clip_title,
                    clip_url=f'https://www.youtube.com/watch?v={new_vid}'
                )

                count += 1
                pub_ok += 1
                published_ids.add(clip_id)
                log(f'  Publicado: {clip_title[:50]} -> {new_vid}')
            else:
                db.update_publicado(lock_row_id, clip_video_id='erro_upload')
                count += 1
                pub_erro += 1
                published_ids.add(clip_id)
                log(f'  Falha ao publicar: {clip_title[:50]}')

        # Update counter
        new_total = pub_ok
        if new_total != publicados_count or pub_erro:
            # pendente = ainda na fila (exclui publicados OK e os com erro)
            pend = max(0, qtd_clips - new_total - pub_erro)
            update_live_status(vid, 'clips_publicados', str(new_total), {'clips_pendentes': str(pend)})
            log(f'  Atualizado clips_publicados: {publicados_count} -> {new_total} para {vid}')

        if count > 0:
            log(f'  {count} tentativa(s) para {vid}')
            update_status('idle', f'Publicacao concluida para {vid}')
            break

    if not found_any:
        log('  Nenhum clip pendente para publicar')
        update_status('idle', 'Nenhum clip para publicar')


def main():
    log('Scheduler iniciado')
    log(f'  Scripts: {SCRIPTS_DIR}')
    log(f'  Lives: {LIVES_DIR}')
    log(f'  Config: {CONFIG_DIR}')
    update_status('idle', 'Scheduler iniciado')

    config = None
    corte_running = threading.Event()
    try:
        config = load_config()
    except Exception as e:
        log(f'ERRO ao carregar config: {e}')

    # Rastreia qual horario agendado ja foi executado (evita repetir)
    startup_corte = get_matching_schedule(config.get('corte_horarios', '')) if config else None
    startup_pub = get_matching_schedule(config.get('pub_horarios', '')) if config else None
    startup_import_pub = get_matching_schedule(config.get('import_pub_horarios', '')) if config else None
    last_executed = {'cortes': startup_corte, 'pub': startup_pub, 'import_pub': startup_import_pub}
    log(f'  Agendamento: cortes={startup_corte or "nenhum agora"}, pub={startup_pub or "nenhum agora"}, import_pub={startup_import_pub or "nenhum agora"}')

    def run_cortes_thread(cfg):
        """Roda cortes em thread separada para nao bloquear publicacao."""
        try:
            corte_running.set()
            process_cortes(cfg)
        except Exception as e:
            log(f'ERRO no corte (thread): {e}')
        finally:
            corte_running.clear()

    while True:
        try:
            config = load_config()

            # --- Cortes (roda em thread separada) ---
            cortes_paused = config.get('pipeline_cortes_paused', 'false') == 'true'
            corte_auto = config.get('corte_auto', 'true') == 'true'
            corte_horarios = config.get('corte_horarios', '')
            corte_match = get_matching_schedule(corte_horarios)

            if not cortes_paused and corte_auto and corte_match:
                if last_executed['cortes'] != corte_match:
                    if not corte_running.is_set():
                        last_executed['cortes'] = corte_match
                        log(f'==> Hora de cortar! (agendado: {corte_match})')
                        threading.Thread(target=run_cortes_thread, args=(config,), daemon=True).start()
                    else:
                        log(f'==> Corte agendado ({corte_match}) mas outro corte ainda esta rodando, pulando')

            if not corte_match and last_executed['cortes']:
                last_executed['cortes'] = None

            # --- Publicacao (roda no loop principal, nao bloqueia) ---
            pub_paused = config.get('pipeline_pub_paused', 'false') == 'true'
            pub_horarios = config.get('pub_horarios', '')
            pub_match = get_matching_schedule(pub_horarios)

            if not pub_paused and pub_match:
                if last_executed['pub'] != pub_match:
                    last_executed['pub'] = pub_match
                    log(f'==> Hora de publicar! (agendado: {pub_match})')
                    process_publicacao(config)

            if not pub_match and last_executed['pub']:
                last_executed['pub'] = None

            # --- Publicacao de imports (fila propria via import_pub_horarios) ---
            import_pub_horarios = config.get('import_pub_horarios', '')
            import_pub_paused = config.get('pipeline_imports_paused', 'false') == 'true'
            import_pub_match = get_matching_schedule(import_pub_horarios) if import_pub_horarios else None

            if not import_pub_paused and import_pub_match:
                if last_executed['import_pub'] != import_pub_match:
                    last_executed['import_pub'] = import_pub_match
                    log(f'==> Hora de publicar imports! (agendado: {import_pub_match})')
                    process_publicacao_imports(config)

            if not import_pub_match and last_executed.get('import_pub'):
                last_executed['import_pub'] = None

            # --- Publicacao de TikTok (fila propria via tiktok_pub_horarios) ---
            tiktok_pub_horarios = config.get('tiktok_pub_horarios', '')
            tiktok_pub_paused = config.get('pipeline_tiktok_paused', 'false') == 'true'
            tiktok_pub_match = get_matching_schedule(tiktok_pub_horarios) if tiktok_pub_horarios else None

            if not tiktok_pub_paused and tiktok_pub_match:
                if last_executed.get('tiktok_pub') != tiktok_pub_match:
                    last_executed['tiktok_pub'] = tiktok_pub_match
                    log(f'==> Hora de publicar TikTok! (agendado: {tiktok_pub_match})')
                    process_publicacao_tiktok(config)

            if not tiktok_pub_match and last_executed.get('tiktok_pub'):
                last_executed['tiktok_pub'] = None

            # --- Enrich lives (titulo generico INEMA → titulo + desc + thumb) ---
            enrich_paused = config.get('pipeline_enrich_paused', 'false') == 'true'
            enrich_auto = config.get('enrich_auto', 'false') == 'true'
            enrich_horarios = config.get('enrich_horarios', '')
            enrich_match = get_matching_schedule(enrich_horarios)

            if not enrich_paused and enrich_auto and enrich_match:
                if last_executed.get('enrich') != enrich_match:
                    last_executed['enrich'] = enrich_match
                    log(f'==> Hora de enriquecer lives! (agendado: {enrich_match})')
                    process_enrich(config)

            if not enrich_match and last_executed.get('enrich'):
                last_executed['enrich'] = None

            # --- TikTok Scanner ---
            tiktok_paused = config.get('pipeline_tiktok_paused', 'false') == 'true'
            tiktok_auto = config.get('tiktok_auto', 'false') == 'true'
            tiktok_horarios = config.get('tiktok_horarios', '')
            tiktok_match = get_matching_schedule(tiktok_horarios)

            if not tiktok_paused and tiktok_auto and tiktok_match:
                if last_executed.get('tiktok') != tiktok_match:
                    last_executed['tiktok'] = tiktok_match
                    log(f'==> TikTok download da fila! (agendado: {tiktok_match})')
                    try:
                        import tiktok_scanner
                        results = tiktok_scanner.download_pending_videos(config)
                        total = sum(r.get('downloaded', 0) for r in results)
                        if total:
                            log(f'==> TikTok: {total} video(s) baixado(s)')
                    except Exception as e:
                        log(f'ERRO no tiktok_scanner: {e}')

            if not tiktok_match and last_executed.get('tiktok'):
                last_executed['tiktok'] = None

            # --- Import worker (verifica a cada hora ou se import_auto=true) ---
            now_hm = datetime.now().strftime('%H:%M')
            import_auto = config.get('import_auto', 'false') == 'true'
            if import_auto:
                now_h = datetime.now().strftime('%H')
                if last_executed.get('import') != now_h:
                    last_executed['import'] = now_h
                    try:
                        import import_worker
                        results = import_worker.process_imports(config)
                        novos = [r for r in results if r.get('ok')]
                        if novos:
                            log(f'==> Import: {len(novos)} lote(s) importado(s) para publicacao')
                    except Exception as e:
                        log(f'ERRO no import_worker: {e}')

            # --- Auto-sync (meia-noite) ---
            sync_auto = config.get('sync_auto', 'false') == 'true'
            if sync_auto and now_hm == '00:00':
                if last_executed.get('sync') != '00:00':
                    last_executed['sync'] = '00:00'
                    log('==> Auto-sync: sincronizando lives do canal de origem...')
                    update_status('sincronizando', 'Auto-sync em andamento...')
                    # Derive dashboard port from INSTANCE_NAME (yt-pub-livesN -> 8090+N)
                    instance_name = os.environ.get('INSTANCE_NAME', '')
                    _inst_num = ''.join(c for c in instance_name if c.isdigit())
                    dashboard_port = str(8090 + int(_inst_num)) if _inst_num else config.get('dashboard_port', '8091')
                    sync_payload = {'mode': 'novas', 'max_lives': 1000}
                    sync_date_from = config.get('sync_auto_date_from', '').strip()
                    if sync_date_from:
                        sync_payload['date_from'] = sync_date_from
                        log(f'  Auto-sync com filtro date_from={sync_date_from}')
                    payload = json.dumps(sync_payload).encode()
                    sync_ok = False
                    for attempt in range(1, 4):
                        try:
                            req = urllib.request.Request(
                                f'http://localhost:{dashboard_port}/api/sync',
                                data=payload
                            )
                            req.add_header('Content-Type', 'application/json')
                            resp = urllib.request.urlopen(req, timeout=120)
                            result = json.loads(resp.read())
                            novas = result.get('novas_lives', 0)
                            log(f'  Auto-sync concluido: {novas} novas lives')
                            update_status('idle', f'Auto-sync OK: {novas} novas lives')
                            sync_ok = True
                            break
                        except Exception as e:
                            log(f'  ERRO no auto-sync (tentativa {attempt}/3): {e}')
                            if attempt < 3:
                                time.sleep(30)
                    if not sync_ok:
                        log('  Auto-sync falhou apos 3 tentativas')
                        update_status('erro', 'Auto-sync falhou apos 3 tentativas (connection refused)')
            if now_hm != '00:00' and last_executed.get('sync'):
                last_executed['sync'] = None

        except Exception as e:
            log(f'ERRO: {e}')

        # Check every 60 seconds
        time.sleep(60)


def acquire_lock():
    """Garante que apenas 1 instancia do scheduler rode por projeto.

    Abertura em 'r+' (nao trunca) para nao apagar o PID antes de validar a flock.
    So trunca+escreve o PID DEPOIS de adquirir a flock. Remove o fluxo de "stale
    detectado" anterior, que tinha race condition causando multiplas instancias.
    """
    import fcntl
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.scheduler.lock')
    # Abre (ou cria) sem truncar
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    lock_file = os.fdopen(fd, 'r+')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Outro processo detem a flock. Le PID (sem modificar arquivo) e reporta.
        lock_file.seek(0)
        raw = lock_file.read().strip()
        try:
            old_pid = int(raw)
            os.kill(old_pid, 0)
            print(f'[ERRO] Outro scheduler ja esta rodando (PID {old_pid}, lock: {lock_path}). Saindo.', file=sys.stderr)
        except (ValueError, ProcessLookupError, OSError):
            print(f'[ERRO] flock travada mas PID vazio/morto (lock inconsistente). '
                  f'Remova manualmente {lock_path} e reinicie.', file=sys.stderr)
        lock_file.close()
        sys.exit(1)
    # flock OK: agora pode truncar e escrever o PID
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


if __name__ == '__main__':
    _lock = acquire_lock()
    main()
