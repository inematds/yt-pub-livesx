#!/usr/bin/env python3
"""Análise configurável de cortes; legado claude-api significa configuração local/assinatura."""
import argparse, json, sys
from pathlib import Path
from ia_motor import carregar, executar
p=argparse.ArgumentParser(description=__doc__); p.add_argument('prompt',type=Path);p.add_argument('saida',type=Path);p.add_argument('--modo',default='config');a=p.parse_args()
c=carregar()['etapas']['analise_cortes']
exige={'anthropic-api':('api','anthropic'),'openrouter-api':('api','openrouter'),'openai-api':('api','openai'),'codex':('assinatura','codex'),'codex-assinatura':('assinatura','codex'),'claude-assinatura':('assinatura','claude')}
if a.modo in exige and (c['tipo'],c['motor'])!=exige[a.modo]:raise SystemExit('Modo diverge de ia.yaml; ajuste tipo/motor/modelo/esforco antes de executar')
res=executar('analise_cortes',a.prompt.read_text(),a.saida.parent)
d=json.loads(res)
if not isinstance(d,dict) or not isinstance(d.get('topics'),list):raise SystemExit('Resposta de análise sem lista topics')
for t in d['topics']:
 if not isinstance(t,dict):raise SystemExit('Tópico inválido')
a.saida.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
print('    Encontrados',len(d['topics']),'tópicos')
