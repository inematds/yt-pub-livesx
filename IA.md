# Motores independentes por etapa

O canal usa `scripts/ia.yaml`. Edite tipo, motor, modelo e esforco de analise_cortes ou prompt_capa. Exemplos com # incluem Claude/Codex por assinatura e APIs Anthropic/OpenAI/OpenRouter, desativadas até autorização específica. A etapa capa exige Codex assinatura/image_gen. As opções antigas do dashboard de IA não substituem este arquivo; claude-api é alias histórico para configuração local, não API. Não há fallback pago. Nenhuma credencial ou banco é copiado entre canais.

```bash
python3 scripts/ia_motor.py ver
python3 scripts/ia_motor.py configurar analise_cortes --tipo assinatura --motor claude --modelo claude-opus-5-5 --esforco medium
python3 scripts/ia_motor.py configurar analise_cortes --tipo assinatura --motor codex --modelo gpt-6.1-sol --esforco medium
```

Cada nova execução dos scripts lê a configuração; os schedulers não precisam reiniciar para adotar esta mudança nos subprocessos. Tradução continua central. O sincronizador deve copiar ia_motor.py e analise_cortes.py, preservando ia.yaml local de cada canal. Não copiar config/, data/, .env ou credentials.enc. Se Codex falhar, tenta inemaimg/FLUX local na fila GPU; se os dois falharem, preserva o gradiente com texto. Nenhuma API alternativa paga é chamada.
