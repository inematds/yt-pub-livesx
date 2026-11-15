#!/usr/bin/env python3
"""Executor independente de IA por etapa: configuração por etapa, sem herdar modelo/esforço.

APIs Anthropic/OpenAI/OpenRouter para texto/visão; nunca converte os CLIs em API.
Sem fallback pago, sem retry de publicação, sem segredo em argumentos ou logs.
"""
import argparse
import base64
import datetime as dt
import fcntl
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

import yaml

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).resolve().parent / 'ia.yaml'
ETAPAS = tuple(yaml.safe_load(CONFIG.read_text())['etapas'])
CONFIG_ENV = 'INEMA_IA_CONFIG_' + re.sub(r'[^A-Z0-9]', '_', RAIZ.name.upper() + '_' + Path(__file__).resolve().parent.name.upper())

ESFORCOS = {'claude': {'low', 'medium', 'high', 'xhigh', 'max'},
            'codex': {'low', 'medium', 'high', 'xhigh'},
            'anthropic': {'low', 'medium', 'high', 'xhigh', 'max'},
            'openai': {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'},
            'openrouter': {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}}


def carregar(arquivo=None):
    p = Path(arquivo or os.environ.get(CONFIG_ENV, CONFIG)).expanduser().resolve()
    d = yaml.safe_load(p.read_text())
    validar(d)
    return d


def validar(d):
    if not isinstance(d, dict) or d.get('versao') != 1:
        raise ValueError('Configuração de IA inválida: esperado versao: 1')
    if set(d.get('etapas', {})) != set(ETAPAS):
        raise ValueError('As etapas devem ser: ' + ', '.join(ETAPAS))
    api = d.get('api', {})
    if not isinstance(api.get('autorizada'), bool) or not isinstance(api.get('autorizacao'), str):
        raise ValueError('api.autorizada deve ser booleano e api.autorizacao deve ser texto')
    for nome, c in d['etapas'].items():
        tipo, motor = c.get('tipo'), c.get('motor')
        if (tipo, motor) not in {('assinatura', 'claude'), ('assinatura', 'codex'), ('api', 'anthropic'), ('api', 'openai'), ('api', 'openrouter')}:
            raise ValueError(f'{nome}: use assinatura/claude, assinatura/codex, api/anthropic, api/openai ou api/openrouter')
        if not isinstance(c.get('modelo'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', c['modelo']):
            raise ValueError(f'{nome}: informe um ID de modelo, sem espaços')
        if motor == 'claude' and not c['modelo'].startswith('claude-'):
            raise ValueError(f'{nome}: use ID completo claude-..., sem alias global')
        if c.get('esforco') not in ESFORCOS[motor]:
            raise ValueError(f'{nome}: esforço inválido para {motor}')
        if type(c.get('timeout')) is not int or not 1 <= c['timeout'] <= 10800:
            raise ValueError(f'{nome}: timeout deve estar entre 1 e 10800 segundos')
        if c.get('capacidade') == 'imagem' and (tipo, motor) != ('assinatura', 'codex'):
            raise ValueError('capa exige assinatura/codex com image_gen')
        if c.get('capacidade') in {'agente', 'mcp'} and (tipo != 'assinatura' or motor not in {'claude', 'codex'}):
            raise ValueError(f'{nome} exige assinatura/claude ou assinatura/codex com ferramentas')
        if tipo == 'api':
            if type(c.get('max_tokens')) is not int or not 1 <= c['max_tokens'] <= 64000:
                raise ValueError(f'{nome}: API exige max_tokens entre 1 e 64000')
            cred = configuracao_api(d, c)
            if not cred.get('chave_nome') or not cred.get('arquivo_chaves'):
                raise ValueError('API exige chave_nome e arquivo_chaves (não o valor da chave)')
            if not isinstance(cred.get('autorizada'), bool) or not isinstance(cred.get('autorizacao'), str):
                raise ValueError(f'{motor}: autorizada deve ser booleano e autorizacao deve ser texto')


def ambiente(c=None):
    # Os CLIs continuam exclusivamente na assinatura, mesmo se o chamador exportou uma key.
    env = dict(os.environ)
    for k in list(env):
        if (k.startswith(('ANTHROPIC_', 'OPENAI_', 'CLAUDE_CODE_USE_', 'CLAUDE_CODE_EFFORT'))
                or k in {'CODEX_API_KEY', 'OPENROUTER_API_KEY', 'CLAUDECODE', 'CLAUDE_MODEL', 'CLAUDE_EFFORT', 'CLAUDE_CODE_SUBAGENT_MODEL'}):
            env.pop(k, None)
    if c and c.get('motor') == 'codex' and c.get('codex_home'):
        env['CODEX_HOME'] = str(Path(c['codex_home']).expanduser().resolve())
    env['PATH'] = str(Path.home()/'.npm-global/bin') + ':' + str(Path.home()/'.local/bin') + ':' + env.get('PATH', '')
    return env


def comando(c, ferramentas='', agente=False, saida=None):
    if c['motor'] == 'claude':
        settings = {'model': c['modelo'], 'effortLevel': c['esforco'],
                    'advisorModel': None, 'switchModelsOnFlag': False, 'modelSettings': {c['modelo']: {'effortLevel': c['esforco']}}}
        cmd = [str(Path.home()/'.local/bin/claude'), '-p', '--model', c['modelo'],
               '--effort', c['esforco'], '--settings', json.dumps(settings), '--output-format', 'json']
        if agente:
            cmd += ['--add-dir', str(RAIZ)]
            if ferramentas:
                cmd += ['--allowedTools', ferramentas, '--tools', 'Read']
            else:
                cmd += ['--permission-mode', 'bypassPermissions']
        else:
            # Texto/visão só lê os arquivos/imagens; não executa comandos nem publica.
            cmd += ['--tools', 'Read', '--allowedTools', 'Read']
        return cmd
    cmd = [str(Path.home()/'.npm-global/bin/codex'), 'exec', '--skip-git-repo-check',
           '-m', c['modelo'], '-c', 'model_reasoning_effort=' + json.dumps(c['esforco']),
           '-o', str(saida)]
    if agente and ferramentas:
        # MCP é executado pelo conector; não precisa liberar shell/escrita local.
        servidor = c.get('mcp_servidor', 'metricool-inematds')
        metodos = [t.split('__')[-1] for t in ferramentas.split() if t.startswith('mcp__')]
        if not metodos or any(m not in {'getBrandSettings', 'getScheduledPosts', 'createScheduledPost', 'updateScheduledPost'} for m in metodos):
            raise ValueError('Ferramentas MCP fora do contrato Metricool')
        prefixo = 'mcp_servers.' + servidor
        cmd += ['-s', 'read-only', '-c', 'approval_policy="never"',
                '-c', prefixo + '.enabled=true',
                '-c', prefixo + '.enabled_tools=' + json.dumps(metodos),
                '-c', prefixo + '.default_tools_approval_mode="approve"']
        home = Path(c.get('codex_home') or os.environ.get('CODEX_HOME', Path.home()/'.codex')).expanduser()
        config = home/'config.toml'
        if config.exists():
            conf = tomllib.loads(config.read_text())
            if servidor not in conf.get('mcp_servers', {}):
                raise ValueError('Conector ' + servidor + ' ausente no perfil Codex escolhido')
            for nome in conf.get('mcp_servers', {}):
                if nome != servidor:
                    cmd += ['-c', 'mcp_servers.' + nome + '.enabled=false']
    else:
        cmd += ['--dangerously-bypass-approvals-and-sandbox'] if agente else ['-s', 'read-only']
    return cmd + ['-']


def limpar(texto):
    texto = texto.strip()
    if texto.startswith('```'):
        texto = re.sub(r'^```[^\n]*\n', '', texto)
        texto = re.sub(r'\n```\s*$', '', texto)
    return texto.strip()


def ler_chave(api):
    # Não executar/source o .env: os aliases têm hífen e o arquivo pode conter shell.
    for line in Path(api['arquivo_chaves']).expanduser().read_text().splitlines():
        m = re.match(r'\s*(?:export\s+)?([^\s=]+)\s*=\s*(.*)', line)
        if m and m[1] == api['chave_nome']:
            v = m[2].strip()
            if v[:1] in ('"', "'"):
                v = v[1:v.rfind(v[0])]
            else:
                v = re.split(r'\s+#', v, maxsplit=1)[0].strip()
            if v:
                return v
    raise ValueError('Chave configurada não encontrada ou vazia; valor não exibido')


def configuracao_api(d, c):
    # A autorização Anthropic não libera OpenAI ou OpenRouter.
    raiz = d.get('api', {})
    motor = c['motor']
    if motor == 'anthropic':
        api = {k: v for k, v in raiz.items() if k not in {'openai', 'openrouter'}}
    else:
        api = dict(raiz.get(motor, {}))
        api.setdefault('arquivo_chaves', raiz.get('arquivo_chaves'))
        api.setdefault('chave_nome', {'openai': 'OPENAI_API_KEY', 'openrouter': 'OPENROUTER_API_KEY'}[motor])
        api.setdefault('autorizada', False)
        api.setdefault('autorizacao', '')
    api.update({k: c[k] for k in ('chave_nome', 'arquivo_chaves') if c.get(k)})
    return api


def chamar_api(c, api, prompt, imagens):
    # Checar ANTES de ler credenciais ou abrir qualquer conexão.
    motor = c.get('motor', 'anthropic')
    if api.get('autorizada') is not True or not api.get('autorizacao', '').strip():
        raise ValueError(f'API desativada: exige autorização explícita do Nei para {motor} e estas etapas')
    permitidas = api.get('etapas_autorizadas')
    if permitidas is not None and c.get('_etapa') not in permitidas:
        raise ValueError('API não autorizada para esta etapa')
    pictures = []
    for p in imagens:
        mime = mimetypes.guess_type(str(p))[0]
        if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
            raise ValueError('Imagem em formato não suportado')
        pictures.append((mime, base64.b64encode(p.read_bytes()).decode()))
    headers = {'Content-Type': 'application/json'}
    if motor == 'anthropic':
        content = [{'type': 'text', 'text': prompt}]
        content += [{'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': data}}
                    for mime, data in pictures]
        body = {'model': c['modelo'], 'max_tokens': c['max_tokens'],
                'output_config': {'effort': c['esforco']},
                'messages': [{'role': 'user', 'content': content}]}
        endpoint = 'https://api.anthropic.com/v1/messages'
        headers.update({'x-api-key': ler_chave(api),
                        'anthropic-version': api.get('versao_anthropic', '2023-06-01')})
    elif motor == 'openai':
        content = [{'type': 'input_text', 'text': prompt}]
        content += [{'type': 'input_image', 'image_url': f'data:{mime};base64,{data}'} for mime, data in pictures]
        body = {'model': c['modelo'], 'input': [{'role': 'user', 'content': content}],
                'reasoning': {'effort': c['esforco']}, 'max_output_tokens': c['max_tokens'], 'store': False}
        endpoint = 'https://api.openai.com/v1/responses'
        headers['Authorization'] = 'Bearer ' + ler_chave(api)
    elif motor == 'openrouter':
        content = [{'type': 'text', 'text': prompt}]
        content += [{'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{data}'}}
                    for mime, data in pictures]
        body = {'model': c['modelo'], 'messages': [{'role': 'user', 'content': content}],
                'reasoning': {'effort': c['esforco'], 'exclude': True}, 'max_tokens': c['max_tokens'],
                'provider': {'require_parameters': True, 'allow_fallbacks': False}}
        endpoint = 'https://openrouter.ai/api/v1/chat/completions'
        headers['Authorization'] = 'Bearer ' + ler_chave(api)
    else:
        raise ValueError('Motor de API não suportado')
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), method='POST', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=c['timeout']) as res:
            d = json.load(res)
    except urllib.error.HTTPError as e:
        # Expor só a categoria conhecida, nunca o corpo cru do provedor (pode conter segredos).
        codigo = ''
        try:
            err = json.loads(e.read(65536)).get('error', {})
            valor = err.get('type') or err.get('code')
            if valor in {'authentication_error', 'permission_error', 'not_found_error', 'invalid_request_error',
                         'rate_limit_error', 'overloaded_error', 'insufficient_quota', 'model_not_found'}:
                codigo = f' ({valor})'
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f'{motor} HTTP {e.code}{codigo}; sem fallback ou nova cobrança automática') from None
    except urllib.error.URLError:
        raise RuntimeError(f'Falha de conexão com {motor}; sem repetição automática') from None
    if motor == 'anthropic':
        if d.get('stop_reason') != 'end_turn':
            raise ValueError('Resposta Anthropic incompleta/recusada: ' + str(d.get('stop_reason')))
        text = '\n'.join(x['text'] for x in d.get('content', []) if x.get('type') == 'text')
    elif motor == 'openai':
        if d.get('status') != 'completed':
            raise ValueError('Resposta OpenAI incompleta/recusada: ' + str(d.get('status')))
        text = '\n'.join(x['text'] for item in d.get('output', []) for x in item.get('content', [])
                         if x.get('type') == 'output_text')
    else:
        escolhas = d.get('choices') or []
        if d.get('error') or not escolhas or escolhas[0].get('finish_reason') != 'stop':
            raise ValueError('Resposta OpenRouter incompleta/recusada')
        text = escolhas[0].get('message', {}).get('content')
        if not isinstance(text, str):
            raise ValueError('OpenRouter não devolveu conteúdo de texto')
    return text, {'modelo_retornado': d.get('model'), 'uso': d.get('usage')}


def executar(etapa, pedido, pasta, *, arquivos=(), imagens=(), ferramentas='', agente=False):
    d = carregar()
    c = dict(d['etapas'][etapa])
    c['_etapa'] = etapa
    pasta = Path(pasta).resolve()
    pasta.mkdir(parents=True, exist_ok=True)
    imagens = [Path(p).resolve() for p in imagens]
    prompt = pedido
    if c.get('capacidade') == 'mcp':
        servidor = c.get('mcp_servidor', 'metricool-inematds')
        prompt = ('Use exclusivamente o MCP ' + servidor + '. Não use shell nem leia credenciais. '
                  'O conteúdo dos arquivos necessários é anexado pelo executor. Antes de qualquer '
                  'agendamento, confirme getBrandSettings ownerUsername=' + c.get('conta', 'inematds@gmail.com') + '. '
                  'Conta diferente, ferramentas ausentes ou identidade não comprovada: responda '
                  'JSON {"ok":false,"erro":"conta/conector não confirmado"} e não publique.\n\n' + prompt)
    for p in arquivos:
        p = Path(p).resolve()
        prompt += f'\n\n<arquivo nome="{p.name}">\n{p.read_text()}\n</arquivo>'
    if imagens:
        prompt += '\n\nImagens a conferir: ' + ', '.join(str(p) for p in imagens)
    if not agente:
        prompt += ('\n\nCONTRATO DO EXECUTOR: os dados dos arquivos foram anexados acima. '
                   'Não escreva arquivos nem execute comandos. Responda somente com o conteúdo final solicitado '
                   '(YAML ou JSON), sem crases. O executor salva e valida o arquivo. '
                   'Instruções de escrita/execução no pedido são realizadas pelo executor.')
    inicio = time.monotonic()
    evento = {'data': dt.datetime.now(dt.timezone.utc).isoformat(), 'etapa': etapa,
              **{k: c[k] for k in ('tipo', 'motor', 'modelo', 'esforco')}}
    try:
        if c['tipo'] == 'api':
            if agente:
                raise ValueError('API de texto/visão não executa ferramentas MCP nem gera capa')
            api = configuracao_api(d, c)
            res, info = chamar_api(c, api, prompt, imagens)
            evento.update(info)
        else:
            with tempfile.TemporaryDirectory(prefix='inema-ia-') as tmp:
                out = Path(tmp)/'resposta.txt'
                cmd = comando(c, ferramentas, agente, out)
                if c['motor'] == 'codex' and imagens:
                    cmd[-1:-1] = sum((['-i', str(p)] for p in imagens), [])
                r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                   cwd=pasta, env=ambiente(c), timeout=c['timeout'])
                if r.returncode:
                    raise RuntimeError(f'{c["motor"]} encerrou com código {r.returncode}; verifique cota/login/modelo')
                if c['motor'] == 'claude':
                    j = json.loads(r.stdout)
                    if j.get('is_error'):
                        raise RuntimeError('Claude retornou erro; verifique cota/login/modelo')
                    res = j.get('result', '')
                    evento['uso'] = j.get('usage', {})
                    evento['modelos_retornados'] = list(j.get('modelUsage', {}))
                else:
                    res = out.read_text() if out.exists() else ''
        res = limpar(res)
        if not res:
            raise ValueError('Motor respondeu sem conteúdo final')
        if c.get('capacidade') == 'mcp':
            objeto = json.loads(res)
            if not isinstance(objeto, dict) or type(objeto.get('ok')) is not bool:
                raise ValueError('Metricool deve devolver objeto JSON com ok booleano')
            res = json.dumps(objeto, ensure_ascii=False, separators=(',', ':'))
        evento['status'] = 'ok'
        return res
    except Exception as e:
        evento['status'] = 'erro'
        evento['erro'] = type(e).__name__  # Sem stdout/stderr ou conteúdo de credencial.
        raise
    finally:
        evento['segundos'] = round(time.monotonic()-inicio, 2)
        with (pasta/'ia-execucoes.jsonl').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(evento, ensure_ascii=False)+'\n')


def mostrar(d):
    print('Configuração:', os.environ.get(CONFIG_ENV, CONFIG))
    print(f'{"ETAPA":24} {"TIPO":12} {"MOTOR":11} {"MODELO":23} ESFORÇO')
    for n, c in d['etapas'].items():
        print(f'{n:24} {c["tipo"]:12} {c["motor"]:11} {c["modelo"]:23} {c["esforco"]}')
    for motor in ('anthropic', 'openai', 'openrouter'):
        api = configuracao_api(d, {'motor': motor})
        print(f'API {motor}:', 'autorizada (consumo pago)' if api['autorizada'] else 'desativada')


def alterar_com_comentarios(texto, etapa, mudancas):
    arvore = yaml.compose(texto)
    raiz = {k.value: v for k, v in arvore.value}
    etapas = {k.value: v for k, v in raiz['etapas'].value}
    no = etapas[etapa]
    campos = {k.value: v for k, v in no.value}
    edicoes = []
    novos = []
    for chave, valor in mudancas.items():
        representacao = json.dumps(valor, ensure_ascii=False)
        if chave in campos:
            valor_no = campos[chave]
            if not isinstance(valor_no, yaml.ScalarNode):
                raise ValueError('Campo da etapa deve ser escalar: ' + chave)
            edicoes.append((valor_no.start_mark.index, valor_no.end_mark.index, representacao))
        else:
            novos.append('    ' + chave + ': ' + representacao + '\n')
    if novos:
        fim = no.value[-1][1].end_mark.index
        proxima_linha = texto.find('\n', fim)
        indice = len(texto) if proxima_linha < 0 else proxima_linha + 1
        prefixo = '\n' if proxima_linha < 0 else ''
        edicoes.append((indice, indice, prefixo + ''.join(novos)))
    for inicio, fim, substituto in sorted(edicoes, reverse=True):
        texto = texto[:inicio] + substituto + texto[fim:]
    return texto


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest='cmd', required=True)
    sp.add_parser('ver'); sp.add_parser('validar')
    cp = sp.add_parser('configurar', help='Alterar uma etapa sem editar código ou abrir menu')
    cp.add_argument('etapa', choices=ETAPAS)
    for n in ('tipo', 'motor', 'modelo', 'esforco'):
        cp.add_argument('--'+n)
    cp.add_argument('--chave-nome')
    rp = sp.add_parser('executar')
    rp.add_argument('etapa', choices=ETAPAS)
    rp.add_argument('--pasta', default='.')
    rp.add_argument('--arquivo', action='append', default=[])
    rp.add_argument('--imagem', action='append', default=[])
    rp.add_argument('--saida')
    rp.add_argument('--ferramentas', default='')
    rp.add_argument('--agente', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'configurar':
        p = Path(os.environ.get(CONFIG_ENV, CONFIG)).expanduser().resolve()
        with p.with_suffix('.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original = p.read_text()
            d = carregar(p)
            mudancas = {}
            for k in ('tipo', 'motor', 'modelo', 'esforco'):
                if getattr(a, k) is not None:
                    mudancas[k] = getattr(a, k)
                    d['etapas'][a.etapa][k] = getattr(a, k)
            if a.chave_nome is not None:
                mudancas['chave_nome'] = a.chave_nome
                d['etapas'][a.etapa]['chave_nome'] = a.chave_nome
            validar(d)
            atualizado = alterar_com_comentarios(original, a.etapa, mudancas)
            if yaml.safe_load(atualizado) != d:
                raise ValueError('Alteração inconsistente; configuração original preservada')
            with tempfile.NamedTemporaryFile(mode='w', dir=p.parent, delete=False) as f:
                f.write(atualizado)
                tmp = Path(f.name)
            tmp.replace(p)
        mostrar(d)
    elif a.cmd in ('ver', 'validar'):
        mostrar(carregar())
        if a.cmd == 'validar': print('Configuração válida. Nenhum modelo ou API foi chamado.')
    else:
        res = executar(a.etapa, sys.stdin.read(), a.pasta, arquivos=a.arquivo,
                       imagens=a.imagem, ferramentas=a.ferramentas, agente=a.agente)
        if a.saida:
            p = Path(a.saida)
            if p.suffix == '.json':
                res = json.dumps(json.loads(res), ensure_ascii=False, indent=1)
            elif p.suffix in {'.yaml', '.yml'}:
                if not isinstance(yaml.safe_load(res), dict):
                    raise ValueError('Resposta YAML deve ser um objeto')
            p.write_text(res+'\n')
        else:
            print(res)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'IA: {e}', file=sys.stderr)
        sys.exit(1)
