#!/usr/bin/env python3
"""
Re-gera titulo/descricao (via transcricao + IA) para vídeos publicados que vieram
de imports/ e ficaram com titulo generico (ex: nome do arquivo, tipo "Mkivideo").

Uso: python3 scripts/reenrich_imports.py
Roda a partir da raiz do projeto (yt-pub-lives<N>).

Le a tabela `publicados` do banco local, filtra os que vieram de import
(live_video_id comeca com 'import_'), acha o clip mp4 correspondente via
lives/<lote>/clips_manifest.json (casando pelo indice salvo no filename:
"<lote>_<indice>"), transcreve o audio, gera titulo+descricao com IA
(mesma logica do import_worker.py) e atualiza no YouTube + no banco.
"""
import os, sys, json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import db
import scheduler
import import_worker


def find_clip_path(live_video_id, filename):
    """Resolve o mp4 do lote via clips_manifest.json, casando pelo indice
    no fim do filename salvo em publicados (ex: import_20260722_videos_3 -> indice 3)."""
    manifest_path = os.path.join('lives', live_video_id, 'clips_manifest.json')
    if not os.path.exists(manifest_path):
        return None
    idx = filename.rsplit('_', 1)[-1]
    if not idx.isdigit():
        return None
    with open(manifest_path) as f:
        manifest = json.load(f)
    for item in manifest:
        if str(item.get('index')) == idx:
            return item.get('file')
    return None


def main():
    rows = db.get_publicados()
    imports = [r for r in rows if str(r.get('live_video_id', '')).startswith('import_')]
    if not imports:
        print('Nenhum import publicado encontrado.')
        return

    for r in imports:
        video_id = r['clip_video_id']
        clip_path = find_clip_path(r['live_video_id'], r['filename'])
        print(f"\n=== {video_id} (titulo atual: {r['clip_titulo']}) ===")

        if not clip_path or not os.path.exists(clip_path):
            print(f'  clip nao encontrado em disco ({clip_path}), pulando')
            continue

        transcript = import_worker._transcrever_clip(clip_path)
        print(f'  transcript len: {len(transcript)}')
        fallback_title = import_worker._title_from_filename(os.path.basename(clip_path))

        title, desc = '', ''
        if transcript and len(transcript) >= import_worker.TRANSCRIPT_MIN_CHARS:
            title, desc = import_worker._gerar_titulo_descricao_ia(transcript, fallback_title)
        if not title:
            print('  fallback: sem transcricao util, gerando so por titulo')
            title = fallback_title
            desc = import_worker._gerar_descricao_ia(title)

        print(f'  TITLE: {title}')
        print(f'  DESC: {desc[:200]}')

        try:
            scheduler.update_video_metadata(video_id, title, desc)
            print('  YouTube: OK')
        except Exception as e:
            print(f'  YouTube ERRO: {e}')
            continue

        db.update_publicado(r['id'], clip_titulo=title)
        print('  DB: OK')


if __name__ == '__main__':
    main()
