# Human-in-the-Loop

Plano para o usuário atuar como Leader diretamente, substituindo o `leader.md` agent, mantendo a comunicação entre os subagents via mailbox.

## Motivação

Em vez de um agent AI tomar decisões estratégicas como Leader, o usuário assume esse papel via Claude Code chat. Os workers e QA continuam operando normalmente via mailbox — só o orchestrador muda.

## Fluxo pretendido

```
Usuário (Claude Code chat)
    │
    ├── mailbox_spawn_agents → spawna workers/QA
    ├── mailbox_send → envia tasks para agents
    ├── mailbox_session_status → monitora heartbeats
    ├── mailbox_watch_inbox → recebe respostas dos agents
    └── review/ queue → agents pedem aprovação, usuário responde
```

## O que NÃO muda

- Arquitetura mailbox (`.agents/mail/`) permanece igual
- Workers e QA continuam sendo spawned via CLI
- Comunicação entre agents continua exclusivamente via inbox

## Melhorias a implementar no `server.py`

### 1. `mailbox_status_panel`

Versão melhorada do `mailbox_session_status` que exibe num único bloco:

- Heartbeat de cada agent (último ping + tempo decorrido)
- Última mensagem enviada/recebida por agent
- Items pendentes na fila `review/` aguardando aprovação do líder

### 2. Fila de review com notificação prioritária

Quando um worker posta em `review/`, o output de `mailbox_watch_inbox` destaca o item como notificação prioritária — o líder humano precisa responder antes de o agent continuar.

### 3. `mailbox_summary`

Nova ferramenta que agrega o estado de todos os agents num único bloco:

- O que cada agent está fazendo agora
- O que já concluiu
- O que está bloqueado ou aguardando revisão
