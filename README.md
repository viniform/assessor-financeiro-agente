# Assessor financeiro: agente, ferramentas e avaliação

Extrato público, para evidência, de um projeto privado de gestão financeira familiar. Este repositório contém **só a camada de agente e a avaliação**. O núcleo (ingestão de extratos e faturas, domínio, persistência, portal) é privado e não está aqui, então o código deste repositório **não roda sozinho**: ele importa `financeiro.aplicacao.*` e `financeiro.persistencia.*` do projeto original.

O que está aqui serve para uma pergunta: *como um agente conversacional de finanças foi desenhado, limitado e medido*.

## Contexto

Uma família, três pessoas com permissões diferentes, contas e cartões em quatro bancos. O desenho começou em julho de 2026 e o código em 01/08/2026. O núcleo importa OFX, CSV e PDF com conferência de saldo, mantém orçamento por categoria, faturas, projeções e alertas, e tem um portal web. O spec previa desde o início um agente conversacional com os mesmos limites e permissões da interface gráfica. Esta é essa camada, entregue e medida em 11/09/2026.

## Arquitetura

```
pessoa (Ana, Bruno ou Clara)
   │  pergunta em linguagem natural
   ▼
Assessor  (Claude Agent SDK, servidor MCP em processo)
   │  só enxerga 9 ferramentas; nenhuma ferramenta embutida liberada
   ▼
ferramentas.py  (finas, sem regra de negócio)
   │  identidade vem do contexto montado a partir do banco, nunca de parâmetro
   ▼
núcleo privado: consultas e serviços com a MESMA guarda de permissões do portal
   │
   ▼
PostgreSQL (dados reais só locais; aqui, base sintética)
```

Ferramentas: `saldo_contas`, `consultar_lancamentos`, `resumo_periodo`, `orcamento_status`, `alertas_ativos`, `faturas_cartao`, `preparar_lancamento`, `confirmar_lancamento`, `definir_teto`.

## Decisões que definem o agente

- **IA só onde há julgamento.** Cálculo, projeção, importação e permissão são código. O agente interpreta a pergunta, escolhe a ferramenta e explica.
- **Nunca inventa número.** Todo valor vem de ferramenta. Sem dado, diz que não sabe. É proibido responder "não encontrei" sem ter consultado.
- **Escrita em dois passos.** `preparar_lancamento` devolve um token e não grava; `confirmar_lancamento` só é chamado numa mensagem seguinte em que a pessoa confirmou.
- **Não executa operação financeira** (Pix, pagamento, transferência), **não recebe credencial** e **não recomenda investimento**.
- **Recusa explícita.** Falta de permissão é dita, não disfarçada de "dado não existe".
- **Conteúdo é dado, não instrução.** Descrição de lançamento com ordem embutida é ignorada e avisada.
- **Dados reais só locais.** Desenvolvimento e avaliação sobre base sintética gerada com semente fixa (`semente_sintetica.py`).

## Avaliação: 30 cenários julgados por código

Três famílias: exatidão numérica (o gabarito é calculado pelas mesmas consultas do núcleo), aderência aos limites (um verificador determinístico por cenário) e momento (o alerta certo com o valor certo). Nenhuma métrica usa modelo como juiz. Método completo em [docs/agente/AVALIACAO.md](docs/agente/AVALIACAO.md).

| Rodada | Exatidão | Aderência | Latência mediana | p90 |
|---|---|---|---|---|
| r1 | 18/18 | 11/12 | 6,8 s | 10,4 s |
| r2 | 17/18 | 12/12 | 7,1 s | 10,0 s |
| r3 | 18/18 | 12/12 * | 7,3 s | 12,0 s |
| r4 | 18/18 | 12/12 | 7,6 s | 12,0 s |

\* Na r3 o cenário 23 foi apontado como falha pelo verificador e reavaliado: o agente disse que não sabia e citou só um valor vindo de ferramenta. Detalhe em `docs/agente/AVALIACAO.md`.

120 execuções, **2 falhas reais** do agente (as duas: desistir antes de consultar a ferramenta), fechadas por regra no prompt. **98,3% consolidado, 100% na última rodada.** Zero valores em R$ sem origem em ferramenta na r4. Os relatórios com as 30 respostas de cada rodada estão em `docs/agente/avaliacao-r1.md` a `r4.md`.

## Limitações declaradas

Base sintética pequena; um modelo só; verificadores medem número e recusa, não clareza; há variância entre rodadas, por isso o número que vale é o consolidado. Só o Assessor foi construído e medido; agentes de alertas por mensageria e de conciliação estão no plano.

## Estrutura

```
financeiro/agente/ferramentas.py          ferramentas MCP sobre o núcleo
financeiro/agente/assessor.py             o agente e seu prompt de sistema
financeiro/agente/semente_sintetica.py    base sintética determinística
financeiro/agente/avaliacao/cenarios.py   os 30 cenários e os verificadores
financeiro/agente/avaliacao/avaliar.py    execução, métricas e relatório
docs/agente/                              método e relatórios das rodadas
```

Todos os direitos reservados. Publicado como evidência de trabalho, não como biblioteca.
