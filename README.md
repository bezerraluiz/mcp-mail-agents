# mcp-master-of-puppets

Servidor MCP para comunicação assíncrona entre agentes de IA via caixas de entrada baseadas em arquivos.

Cada agente (Claude, GPT, Gemini, Deepseek…) tem seu próprio inbox. As mensagens são
arquivos `.md` com frontmatter YAML. Um `inbox-index.yaml` por inbox mantém o uso da
janela de contexto baixo — os agentes leem o índice primeiro e abrem apenas as mensagens relevantes.

Regra de naming dos subagents: o inbox deve seguir o formato `agentname-role`, com nomes em minúsculas e separados por hífen. Exemplo: `claude-backend-senior`.

## Como funciona

Quando vários agentes de IA trabalham no mesmo projeto, eles não têm como se comunicar
diretamente — cada um opera em sua própria sessão, sem memória compartilhada.

Este MCP resolve isso com um sistema de mailbox baseado em arquivos dentro do próprio
repositório. Cada agente tem um inbox em `.agents/mail/{agent-id}/`. As mensagens são
arquivos `.md` com frontmatter YAML. Um `inbox-index.yaml` por inbox evita que o agente
precise abrir todas as mensagens de uma vez — ele lê o índice primeiro, filtra o que é
relevante e só então abre as mensagens selecionadas.

O fluxo básico:

1. Cada agente sabe seu `agent-id` (ex: `claude`, `gpt`, `gemini`)
2. Ao iniciar, lê seu inbox e as tasks atribuídas a ele
3. Executa a tarefa e envia mensagens para os outros agentes via mailbox
4. Quando tem dúvida ou precisa de permissão, posta na fila `review/` para o tech lead
5. Ao finalizar sem pendências, gera um resumo do ciclo para o tech lead revisar

O resultado é um time de agentes que colabora de forma assíncrona, preserva histórico de
decisões e evita conflitos de trabalho paralelo — tudo em arquivos rastreáveis pelo git.

## Instalação e execução

```bash
uvx mcp-master-of-puppets
```

Ou com pip:

```bash
pip install mcp-master-of-puppets
mcp-master-of-puppets
```

## Configuração no Claude Code

Adicione ao `.claude/settings.json` do seu projeto:

```json
{
  "mcpServers": {
    "mail-agents": {
      "command": "uvx",
      "args": ["mcp-master-of-puppets"],
      "env": {
        "AGENTS_ROOT": "."
      }
    }
  }
}
```

`AGENTS_ROOT` é a raiz do projeto onde `.agents/` será criado (padrão: diretório atual).

## Configuração no Codex CLI

Adicione ao `~/.codex/config.toml`:

```toml
[[mcp_servers]]
name = "mail-agents"
command = "uvx"
args = ["mcp-master-of-puppets"]

[mcp_servers.env]
AGENTS_ROOT = "."
```

Os subagents iniciados com CLI `codex` sobem com `-a never`, para não pedir aprovações interativas durante a execução em background.

## Estrutura de pastas criada

```
.agents/
  mail/
    mailbox-rules.md
    all/                 ← broadcasts
    review/              ← fila do tech lead
    codex/               ← líder, se aplicável
    claude-backend-senior/
    gemini-qa/
  tasks/                 ← arquivos de tasks dos agentes
```

## Regra para IDs de subagents

- Todo subagent deve usar `agentname-role`, com pelo menos um hífen.
- São aceitos apenas `a-z`, `0-9` e `-`.
- Exemplos válidos: `claude-backend-senior`, `gemini-qa`, `codex-frontend`.
- Exemplos inválidos: `claude`, `Claude-QA`, `worker_1`.

Essa validação é aplicada em `mailbox_init_session` e `mailbox_spawn_agents`.

## Ferramentas disponíveis (16)

| Ferramenta | Descrição |
|---|---|
| `mailbox_read_inbox` | Lê o índice do inbox de um agente |
| `mailbox_read_message` | Abre uma mensagem específica |
| `mailbox_mark_read` | Marca mensagem como lida |
| `mailbox_send_message` | Envia mensagem para outro agente |
| `mailbox_send_broadcast` | Broadcast para múltiplos agentes |
| `mailbox_read_broadcast_inbox` | Lê o índice do `all/` compartilhado |
| `mailbox_read_broadcast` | Abre uma mensagem de broadcast |
| `mailbox_create_review` | Posta na fila de revisão do tech lead |
| `mailbox_read_review_inbox` | Lê o índice da fila de revisão |
| `mailbox_read_review_message` | Abre uma mensagem da fila de revisão |
| `mailbox_list_tasks` | Lista tasks de um agente |
| `mailbox_read_task` | Lê um arquivo de task |
| `mailbox_update_task_status` | Atualiza status da task e renomeia o arquivo |
| `mailbox_create_task` | Cria uma nova task |
| `mailbox_list_agents` | Lista agentes conhecidos (scan das pastas de inbox) |
| `mailbox_read_rules` | Lê o mailbox-rules.md |

## Formato das mensagens

```
DD-MM-YYYY-HHmm-{from}-{to}-{assunto}.md
```

```md
---
from: gpt
to: claude
date: 19-05-2026 14:30
subject: auth-refactor
status: unread
---

## Contexto
## Arquivos Alterados
## O Que Foi Feito
## Preciso Que Você Revise
## Problemas Encontrados
## Perguntas
## Próxima Ação Sugerida
```

## Publicando atualizações

**1. Edite o `pyproject.toml` e incremente a versão:**

```toml
version = "0.1.1"
```

**2. Reconstrua e publique no PyPI:**

```bash
uv build
uv publish --token pypi-SEU_TOKEN
```

**3. Faça commit e crie a tag da release no GitHub:**

```bash
git add -A
git commit -m "chore: release v0.1.1"
git tag v0.1.1
git push && git push --tags
```

Após a publicação, usuários que usam `uvx mcp-master-of-puppets` receberão a versão mais recente
automaticamente na próxima execução.

## Licença

MIT
