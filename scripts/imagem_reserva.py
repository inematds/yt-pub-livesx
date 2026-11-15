#!/usr/bin/env python3
"""Reserva local de imagem dos schedulers; chamada exclusivamente dentro da vaga GPU."""
import importlib.machinery, importlib.util, sys
from pathlib import Path
from urllib.parse import urlparse
p=Path(__file__).resolve().parent
loader=importlib.machinery.SourceFileLoader('yt_thumb_local',str(p/'yt-thumbnail'))
spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
if urlparse(m.INEMAIMG_URL).hostname not in {'127.0.0.1','localhost','::1'}:
    raise SystemExit('Reserva restrita ao inemaimg local')
m.IMAGE_MODEL=m.INEMAIMG_MODEL_DEFAULT
Path(sys.argv[1]).write_bytes(m._generate_image_local(sys.stdin.read()))
