# FALHAS

| data | o que quebrou | menor correção | prompt \| infra |
|---|---|---|---|
| 2026-10-12 | Thumb viral das lives caiu no banner antigo de 08/10 a 11/10: o scheduler (systemd, PATH com /usr/bin antes do npm-global) chamava /usr/bin/codex 0.116, que morre com `service_tier = "default"` do config.toml | `_arte_codex` usa `~/.npm-global/bin/codex` quando existe (v3.34.33); testar sempre com o PATH da unit | infra |
