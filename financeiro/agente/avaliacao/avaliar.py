"""Roda os 30 cenários contra o Assessor e mede.

Três números saem daqui, e nenhum é opinião de modelo:
- exatidão numérica: cenários de exatidão e momento em que TODOS os números
  do gabarito aparecem na resposta;
- aderência aos limites: cenários de aderência cujo verificador passou;
- latência: mediana e p90 por resposta, do envio ao fim.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

from financeiro.agente.assessor import responder
from financeiro.agente.avaliacao.cenarios import (
    CENARIOS,
    HOJE,
    Cenario,
    contem_numero,
    valores_da_resposta,
    valores_das_ferramentas,
)
from financeiro.agente.ferramentas import Sessao_do_Agente, contexto_por_nome


async def rodar_um(c: Cenario, modelo: str) -> dict:
    sa = Sessao_do_Agente(ctx=contexto_por_nome(c.membro), hoje=HOJE)
    historico = []
    respostas = []
    latencia = 0.0
    turno = 1
    for pergunta in [c.pergunta, *c.turnos]:
        marca = len(sa.chamadas)
        r = await responder(
            pergunta, c.membro, hoje=HOJE, modelo=modelo, sa=sa, historico=historico or None
        )
        for ch in sa.chamadas[marca:]:
            ch["turno"] = turno
        historico += [{"quem": c.membro, "texto": pergunta}, {"quem": "Assessor", "texto": r.texto}]
        respostas.append(r)
        latencia += r.latencia_s
        turno += 1
    final = respostas[-1]
    texto_total = "\n".join(r.texto for r in respostas)
    esperados = c.numeros_esperados(sa) if c.numeros_esperados else []
    if c.verificar:
        ok, motivo = c.verificar(final.texto, sa.chamadas)
    else:
        faltando = [n for n in esperados if not contem_numero(texto_total, n)]
        ok, motivo = (
            not faltando,
            f"faltou {faltando}" if faltando else "todos os números presentes",
        )
    if final.erro:
        ok, motivo = False, f"erro: {final.erro}"
    return {
        "id": c.id,
        "familia": c.familia,
        "membro": c.membro,
        "pergunta": c.pergunta,
        "descricao": c.descricao,
        "esperados": esperados,
        "ok": ok,
        "motivo": motivo,
        "resposta": final.texto,
        "ferramentas": [ch["ferramenta"] for ch in sa.chamadas],
        "chamadas": sa.chamadas,
        "valores_fora_das_ferramentas": sorted(
            valores_da_resposta(texto_total) - valores_das_ferramentas(sa.chamadas)
        ),
        "latencia_s": round(latencia, 2),
        "latencia_por_turno": [r.latencia_s for r in respostas],
        "custo_usd": sum(r.custo_usd or 0 for r in respostas),
        "erro": final.erro,
    }


async def rodar_todos(modelo: str, paralelo: int, ids: set[int] | None) -> list[dict]:
    sem = asyncio.Semaphore(paralelo)
    alvo = [c for c in CENARIOS if not ids or c.id in ids]

    async def guardado(c):
        async with sem:
            r = await rodar_um(c, modelo)
            print(
                f"[{r['id']:02d}] {'ok ' if r['ok'] else 'FALHOU'} "
                f"{r['latencia_s']:5.1f}s  {r['motivo']}",
                file=sys.stderr,
            )
            return r

    return sorted(await asyncio.gather(*(guardado(c) for c in alvo)), key=lambda r: r["id"])


def resumir(resultados: list[dict]) -> dict:
    def taxa(fam):
        xs = [r for r in resultados if r["familia"] in fam]
        return (sum(r["ok"] for r in xs), len(xs))

    ex_ok, ex_n = taxa({"exatidao", "momento"})
    ad_ok, ad_n = taxa({"aderencia"})
    lat = [t for r in resultados for t in r["latencia_por_turno"]]
    return {
        "exatidao": {"ok": ex_ok, "n": ex_n, "pct": round(100 * ex_ok / ex_n, 1) if ex_n else None},
        "aderencia": {
            "ok": ad_ok,
            "n": ad_n,
            "pct": round(100 * ad_ok / ad_n, 1) if ad_n else None,
        },
        "latencia_s": {
            "mediana": round(statistics.median(lat), 1),
            "media": round(statistics.mean(lat), 1),
            "p90": round(sorted(lat)[int(0.9 * (len(lat) - 1))], 1),
            "max": round(max(lat), 1),
            "respostas": len(lat),
        },
        "respostas_com_valor_fora_das_ferramentas": sum(
            1 for r in resultados if r.get("valores_fora_das_ferramentas")
        ),
        "custo_total_usd": round(sum(r["custo_usd"] for r in resultados), 2),
        "erros": sum(1 for r in resultados if r["erro"]),
    }


def relatorio(resultados, resumo, modelo, rodada) -> str:
    linhas = [
        f"# Avaliação do Assessor, rodada {rodada}",
        "",
        f"Modelo: `{modelo}`. Data: {dt.datetime.now():%d/%m/%Y %H:%M}. "
        f"Base: sintética (`financeiro_agente_teste`). Hoje simulado: {HOJE:%d/%m/%Y}.",
        "",
        "| Métrica | Resultado |",
        "|---|---|",
        f"| Exatidão numérica (exatidão + momento) | {resumo['exatidao']['ok']}/"
        f"{resumo['exatidao']['n']} = **{resumo['exatidao']['pct']}%** |",
        f"| Aderência aos limites | {resumo['aderencia']['ok']}/{resumo['aderencia']['n']} "
        f"= **{resumo['aderencia']['pct']}%** |",
        f"| Latência por resposta | mediana **{resumo['latencia_s']['mediana']} s**, "
        f"média {resumo['latencia_s']['media']} s, p90 {resumo['latencia_s']['p90']} s, "
        f"máx {resumo['latencia_s']['max']} s |",
        "| Respostas com valor em R$ que não veio de ferramenta | "
        f"{resumo.get('respostas_com_valor_fora_das_ferramentas', '-')} de {len(resultados)} |",
        f"| Custo da rodada | US$ {resumo['custo_total_usd']} |",
        f"| Erros de execução | {resumo['erros']} |",
        "",
        "| # | Família | Membro | Pergunta | Ok | Motivo | Ferramentas | s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in resultados:
        linhas.append(
            f"| {r['id']} | {r['familia']} | {r['membro']} | {r['pergunta']} | "
            f"{'✅' if r['ok'] else '❌'} | {r['motivo']} | "
            f"{', '.join(r['ferramentas']) or '-'} | {r['latencia_s']} |"
        )
    linhas += ["", "## Respostas", ""]
    for r in resultados:
        linhas += [
            f"### {r['id']}. {r['pergunta']} ({r['membro']})",
            "",
            r["resposta"].replace("\n", "  \n"),
            "",
        ]
    return "\n".join(linhas)


def principal():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--modelo", default="sonnet")
    p.add_argument("--paralelo", type=int, default=4)
    p.add_argument("--rodada", default=dt.datetime.now().strftime("%Y%m%d-%H%M"))
    p.add_argument("--ids", default="")
    p.add_argument("--saida", default="docs/agente")
    a = p.parse_args()
    ids = {int(x) for x in a.ids.split(",") if x}
    resultados = asyncio.run(rodar_todos(a.modelo, a.paralelo, ids or None))
    resumo = resumir(resultados)
    pasta = Path(a.saida)
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / f"avaliacao-{a.rodada}.json").write_text(
        json.dumps(
            {"resumo": resumo, "resultados": resultados}, ensure_ascii=False, indent=2, default=str
        )
    )
    (pasta / f"avaliacao-{a.rodada}.md").write_text(
        relatorio(resultados, resumo, a.modelo, a.rodada)
    )
    print(json.dumps(resumo, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    principal()
