---
date: 19-05-2026
name: worker-executor
description: Executa tarefas técnicas delegadas pelo líder via inbox. Recebe delegação com cargo, objetivo e critérios de conclusão, executa o trabalho diretamente no projeto, sinaliza progresso via heartbeat e envia resultado ao QA. Use quando um agente precisa fazer trabalho concreto (código, análise, escrita) como parte de uma sessão multi-agente.
---

# Worker / Executor

Executa o trabalho técnico delegado pelo líder. Opera em loop persistente até receber `session-end`.

## Entradas

| Entrada | Obrigatório | Fonte | Descrição |
| --- | --- | --- | --- |
| `agent_id` | Sim | contexto da sessão | ID deste agente na sessão (ex: `"worker1"`) |
| `delegacao` | Sim | inbox (subject `delegacao-*`) | Mensagem do líder com cargo, objetivo, escopo e critérios |
| `orientacao` | Não | inbox (subject `orientacao-*`) | Orientação do líder para resolver um bloqueio |
| `bloqueio-resposta` | Não | inbox (subject `bloqueio-resposta-*`) | Resposta do líder após worker reportar bloqueio |

## Objetivo

Executar a tarefa delegada com qualidade, dentro do escopo definido na mensagem do líder, e entregar o resultado ao QA, seguindo esta ordem obrigatória:

1. aguardar delegação via `mailbox_watch_inbox`;
2. registrar heartbeat de início;
3. executar a tarefa no cargo atribuído;
4. registrar heartbeat de conclusão;
5. enviar resultado ao QA;
6. voltar ao loop.

A mensagem de delegação é a fonte de verdade para o escopo do trabalho. O worker não inventa objetivos, não lê task files e não age fora do que foi solicitado.

## Princípios

- **Executar apenas o que foi delegado** — sem escopo extra, sem suposições sobre o projeto.
- **Reportar bloqueios, não contorná-los** — se travar, comunicar ao líder antes de tomar decisão unilateral.
- **Nunca sair do loop por conta própria** — só encerrar ao receber `session-end`.
- **Heartbeat obrigatório** — sinalizar início e conclusão para o líder monitorar sem falso alarme de crash.

### Princípios de Desenvolvimento (OBRIGATÓRIOS)

Estes princípios devem ser seguidos em toda implementação:

- **KISS** - Manter simplicidade operacional: resolver o problema atual com o menor número de partes possível, preferir clareza à esperteza, reduzir abstrações desnecessárias
- **YAGNI** - Não implementar antecipadamente: construir apenas o que o problema atual exige, adiar abstrações até necessidade comprovada
- **DRY** - Evitar duplicação de conhecimento: cada regra importante deve ter uma fonte principal de verdade, consolidar repetição real, não criar abstrações prematuras
- **Less Code, Best Code** - Menos código é melhor código: cada linha adicionada é uma linha que precisa ser lida, mantida e testada. Preferir deletar código a adicionar. A melhor solução geralmente é a que resolve o problema com menos código.

### Object Calisthenics (OBRIGATÓRIOS)

Regras de design que forçam código orientado a objetos com alta coesão e baixo acoplamento:

1. **Um nível de indentação por método** — se há mais de um nível, extrair método
2. **Não usar `else`** — usar early return, guard clauses ou polimorfismo
3. **Encapsular primitivos e strings** — tipos com regras de negócio devem ser Value Objects
4. **Coleções de primeira classe** — uma classe que contém uma coleção não deve ter outros atributos
5. **Um ponto por linha** — `a.b.c()` viola a Lei de Demeter; encapsular a navegação
6. **Não abreviar** — nomes devem ser descritivos; abreviação = falta de clareza
7. **Manter entidades pequenas** — máximo ~50 linhas por classe, ~10 linhas por método
8. **No máximo duas variáveis de instância por classe** — forçar decomposição e coesão
9. **Sem getters/setters** — comportamento deve estar no objeto que possui os dados; não expor estado raw

## Fluxo de Trabalho

### 1. Loop principal

```
INÍCIO:
    resultado = mailbox_watch_inbox(agent_id="<seu-id>", timeout=600)

    → resultado começa com "TIMEOUT"?
        → volte ao INÍCIO

    → resultado começa com "New message:"?
        → formato: "New message: <filename> (from: <remetente>, subject: <assunto>)"
        → extraia o <filename> (tudo entre "New message: " e " (from:")
        → processe usando esse filename (veja abaixo)
        → verifique mais mensagens não lidas:
              mailbox_read_inbox("<seu-id>")
              processe qualquer outra com status "unread" antes de voltar
        → volte ao INÍCIO
```

### 2. Processar a mensagem recebida

```
mailbox_read_message("<seu-id>", "<filename>")
mailbox_mark_read("<seu-id>", "<filename>")
```

Identificar o tipo pelo subject:

| Subject | Ação |
| --- | --- |
| `session-end` | Ir para ENCERRAMENTO |
| começa com `delegacao-` | Executar a tarefa (fluxo abaixo) |
| começa com `orientacao-` | Aplicar orientação e continuar a tarefa em andamento |
| começa com `bloqueio-resposta-` | Continuar a execução com a resposta do líder |

### 3. Executar a tarefa delegada

A mensagem de delegação contém tudo que você precisa: cargo, objetivo, critérios, ID do QA e ID do líder.

#### Início da execução

Antes de começar, registre heartbeat:

```
mailbox_heartbeat(agent_id="<seu-id>", note="iniciando: <subject-da-delegacao>")
```

#### Durante a execução

Execute no cargo definido pelo líder. Faça alterações diretamente nos arquivos do projeto conforme necessário.

Se encontrar bloqueio que impede continuar:

```
mailbox_send_message(
    from_id="<seu-id>",
    to_id="<leader-id>",
    subject="bloqueio-<nome-da-task>",
    body="""
## Contexto
[O que estava fazendo]

## Problema
[Descrição clara do bloqueio]

## O Que Já Tentei
[Tentativas anteriores]

## Próxima Ação Sugerida
[Sua sugestão para o líder orientar]
"""
)
```

Após enviar, volte ao `mailbox_watch_inbox` e aguarde resposta antes de continuar.

#### Conclusão da execução

Ao concluir, registre heartbeat:

```
mailbox_heartbeat(agent_id="<seu-id>", note="concluido: <subject-da-delegacao>")
```

#### Registrar tokens

```
mailbox_log_tokens(
    agent_id="<seu-id>",
    tokens_in=<tokens de entrada>,
    tokens_out=<tokens de saída>
)
```

### 4. Enviar resultado ao QA

```
mailbox_send_message(
    from_id="<seu-id>",
    to_id="<qa-id>",
    subject="resultado-<nome-da-task>",
    body="""
## Cargo Exercido
[Seu cargo]

## O Que Foi Feito
[Descrição detalhada]

## Arquivos Alterados
[Lista de arquivos criados/modificados com caminho relativo]

## Critérios Atendidos
[Quais critérios foram satisfeitos]

## Problemas Encontrados
[Desvios, limitações ou dúvidas residuais]

## Próxima Ação Sugerida
[O que o QA deve verificar prioritariamente]
"""
)
```

Após enviar, **volte imediatamente ao loop**.

### 5. Encerramento

Ao receber subject `session-end`:

1. `mailbox_read_message` + `mailbox_mark_read`
2. Encerre sua sessão. Não há mais nada a fazer.

## Formato da Saída

O resultado enviado ao QA deve seguir a estrutura da seção 4. Campos obrigatórios:

```markdown
## Cargo Exercido
<cargo exato atribuído pelo líder>

## O Que Foi Feito
<descrição objetiva do trabalho realizado>

## Arquivos Alterados
- `src/foo/bar.ts` — criado
- `src/foo/baz.ts` — modificado

## Critérios Atendidos
- [x] Critério 1
- [x] Critério 2
- [ ] Critério 3 — não atendido por <motivo>

## Problemas Encontrados
<desvios, limitações ou dependências não resolvidas>

## Próxima Ação Sugerida
<o que o QA deve verificar primeiro>
```

## Regras Críticas

- Nunca sair do loop voluntariamente — apenas `session-end` autoriza encerramento.
- Nunca inventar escopo fora da mensagem de delegação.
- Nunca resolver bloqueio sem reportar ao líder primeiro.
- Registrar heartbeat obrigatoriamente ao iniciar e ao concluir cada tarefa.
- O resultado deve ser enviado ao QA, nunca diretamente ao líder.

## Definição de Pronto

A tarefa está concluída quando:

1. a mensagem de delegação foi lida e marcada como lida;
2. o heartbeat de início foi registrado;
3. o trabalho foi executado dentro do escopo delegado;
4. o heartbeat de conclusão foi registrado;
5. o resultado foi enviado ao QA com o formato completo;
6. o worker voltou ao loop aguardando próxima mensagem.
