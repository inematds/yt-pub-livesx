# Desenvolvimento seguro

## 1. Regra principal

A branch `main` é a versão estável. Não faça commit direto nela, não altere arquitetura sem discussão e não rode ações que afetem produção sem confirmação explícita.

## 2. Nunca fazer

- Não sobrescrever `.env`.
- Não commitar secrets, tokens, senhas, API keys, `credentials.enc`, `.encryption_key`, bancos SQLite ou vídeos.
- Não apagar arquivos sem autorização.
- Não rodar migrations ou importações em banco de produção.
- Não executar `scripts/sync-instances` sem confirmar origem, destinos e arquivos.
- Não expor dashboards publicamente sem HTTPS, senha forte e proteção adicional.
- Não atualizar dependências grandes junto com mudanças funcionais.
- Não refatorar frontend, backend, banco ou autenticação na mesma etapa de diagnóstico.

## 3. Criar branches

Sempre comece assim:

```bash
git status
git switch main
git pull origin main
git switch -c tipo/descricao-curta
```

Exemplos:

- `docs/install-local`
- `fix/oauth-callback-docs`
- `feature/layout-dashboard`
- `feature/seguranca-autenticacao`

## 4. Commits pequenos

Um commit deve representar uma mudança simples e reversível.

Bons exemplos:

- `docs: add local installation guide`
- `fix: validate missing dashboard password`
- `chore: document docker ports`

Evite commits que misturam documentação, layout, banco, autenticação e deploy ao mesmo tempo.

## 5. Antes de editar

Liste os arquivos que serão alterados. Para esta primeira etapa, o escopo documental é:

- `INSTALL_LOCAL.md`
- `SAFE_DEVELOPMENT.md`

Se outro arquivo parecer necessário, explique o motivo, o risco e a menor alteração possível antes de editar.

## 6. Testar antes de merge

Checks mínimos:

```bash
git status
git diff
docker compose config
```

Quando houver ambiente local completo:

```bash
GWS_CONFIG_DIR="$(pwd)/config" python3 scripts/setup-db
GWS_CONFIG_DIR="$(pwd)/config" python3 dashboard/server.py 8091
```

Para VPS/systemd:

```bash
systemctl --user status yt-master-dashboard
systemctl --user status yt-dashboard<N>
systemctl --user status yt-scheduler<N>
journalctl --user -u yt-dashboard<N> -n 100 --no-pager
journalctl --user -u yt-scheduler<N> -n 100 --no-pager
```

Só teste scheduler/publicação em instância controlada, porque ele pode baixar vídeos, cortar arquivos e publicar no YouTube.

Fluxo seguro para teste real de publicação:

1. Importe o vídeo e rode análise/corte local sem `--publish`.
2. Gere thumbnails e revise os arquivos em `lives/<VIDEO_ID>/`.
3. Publique apenas o primeiro clipe como `unlisted`.
4. Valide no YouTube Studio: título, descrição, thumbnail, áudio, vídeo, canal e privacidade.
5. Publique os demais como `unlisted`.
6. Só mude para `public` depois de aprovar o lote.
7. Valide pela API ou pelo dashboard se todos ficaram com a privacidade esperada.

Em Windows, cuidado extra com encoding e shell:

- Rode processos Python com `PYTHONUTF8=1` e `PYTHONIOENCODING=utf-8`.
- Prefira passar metadados via JSON/ambiente em vez de interpolar título/descrição em comandos shell.
- Verifique títulos com acentos no YouTube após o upload.

## 7. Usar Codex com segurança

Ao pedir mudanças ao Codex:

- Informe a branch alvo.
- Diga se ele pode ou não editar arquivos.
- Peça para listar arquivos antes de editar.
- Não cole secrets na conversa.
- Peça diagnóstico antes de mudança em auth, banco, Docker, systemd ou deploy.
- Peça PR pequeno por etapa.

Modelo de pedido seguro:

```text
Trabalhe na branch feature/x.
Não altere main.
Antes de editar, liste arquivos.
Faça apenas a menor correção.
Não mexa em .env, banco, auth ou deploy.
Depois rode testes seguros e mostre o diff.
```

## 8. `.env` e secrets

Arquivos sensíveis esperados fora do Git:

- `config/.env`
- `config/credentials.enc`
- `config/.encryption_key`
- `config/client_secret.json`
- `config/token_cache.json`
- `data/lives.db`
- `lives/`
- `imports/`

Boas práticas:

- Use `.env.example` apenas como modelo.
- Gere senhas e chaves com ferramenta segura.
- Faça backup criptografado de `.env`, `.encryption_key`, `credentials.enc` e `lives.db`.
- Nunca compartilhe prints ou logs com tokens.
- Troque credenciais se houver suspeita de vazamento.

## 9. Produção

Em produção, evite:

- Rodar scripts interativos sem ler o que fazem.
- Usar senha padrão `Inema2026$$$`.
- Expor `8090` ou `8091:8099` diretamente na internet.
- Rodar `setup-canal` apontando para canal/projeto errado.
- Rodar `setup-db --import` sem backup.
- Rodar limpeza de imports/clips sem confirmar impacto.

Preferência operacional:

- Acessar dashboards por SSH tunnel.
- Usar firewall restritivo.
- Usar systemd user services.
- Acompanhar logs com `journalctl`.
- Fazer backup antes de mudanças.

## 10. Revisar alterações

Antes de commit:

```bash
git status
git diff
git diff --check
```

Verifique:

- Só arquivos esperados foram alterados.
- Nenhum secret entrou no diff.
- Nenhum `.env` real entrou no Git.
- Documentação bate com os arquivos reais.
- Comandos perigosos estão marcados com aviso.

## 11. Voltar atrás

Se a mudança ainda não foi commitada:

```bash
git diff
```

Reverta apenas os arquivos da sua mudança, nunca alterações desconhecidas de outra pessoa.

Se já foi commitada em branch:

```bash
git revert <commit>
```

Se já entrou em produção:

1. Pare o serviço afetado.
2. Preserve logs.
3. Identifique a menor causa.
4. Reverter commit ou restaurar backup.
5. Subir novamente.
6. Registrar o incidente no PR ou issue.

## 12. Fluxo ideal para melhorias futuras

Roadmap recomendado, uma branch por tema:

- `feature/layout-dashboard`: melhorias visuais do dashboard.
- `feature/refino-agente-sdr`: ajustes de prompts/IA do agente.
- `feature/configuracoes-avancadas-agente`: painel de configurações avançadas.
- `feature/painel-admin-configuravel`: administração de instâncias e parâmetros.
- `feature/seguranca-autenticacao`: hardening de login, senha, sessão, rate limit e HTTPS.
- `feature/logs-e-monitoramento`: logs estruturados, health checks e alertas.
- `feature/deploy-vps`: documentação e scripts de deploy seguro em VPS.
- `feature/docker-compose-producao`: compose separado para produção.

Cada uma dessas melhorias deve ter PR próprio, teste próprio e rollback claro.
