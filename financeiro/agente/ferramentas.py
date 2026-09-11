"""As ferramentas que o agente enxerga (spec §9.1), finas sobre as consultas.

Regra do contrato: a identidade vem do contexto montado a partir do banco,
nunca de parâmetro. Cada ferramenta abre a própria sessão, chama a consulta
que o portal já usa, e devolve dicionário serializável. A guarda é a mesma
dos outros canais (§7.3): recusa vira erro com mensagem, e a trilha registra.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select

import financeiro.persistencia.modelos  # noqa: F401
from financeiro.aplicacao.auditoria import Auditoria
from financeiro.aplicacao.consultas.alertas import alertas as consultar_alertas
from financeiro.aplicacao.consultas.contas import saldo_por_conta
from financeiro.aplicacao.consultas.faturas import faturas as consultar_faturas
from financeiro.aplicacao.consultas.lancamentos import FiltroLancamentos, listar_lancamentos
from financeiro.aplicacao.consultas.orcamento import painel_de_orcamento
from financeiro.aplicacao.contexto import Canal, ContextoRequisicao
from financeiro.aplicacao.erros import PermissaoNegada
from financeiro.aplicacao.guarda import Guarda
from financeiro.dominio.lancamento import TipoLancamento
from financeiro.dominio.membro import Membro
from financeiro.dominio.permissao import ConjuntoPermissoes
from financeiro.persistencia.base import Sessao, criar_engine
from financeiro.persistencia.modelos import ContaORM, LancamentoORM, MembroORM


def url_do_agente() -> str:
    url = os.environ.get("FINANCEIRO_AGENTE_URL") or os.environ.get("BANCO_URL_TESTE", "")
    if not url:
        raise RuntimeError("FINANCEIRO_AGENTE_URL ou BANCO_URL_TESTE precisa estar definida")
    return url


_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = criar_engine(url_do_agente())
    return _engine


def contexto_por_nome(nome: str) -> ContextoRequisicao:
    with Sessao(bind=engine()) as s:
        orm = s.execute(select(MembroORM).where(MembroORM.nome == nome)).scalar_one()
        return ContextoRequisicao(
            membro=Membro(
                id=orm.id,
                nome=orm.nome,
                ativo=orm.ativo,
                permissoes=ConjuntoPermissoes(orm.chaves_vigentes),
            ),
            canal=Canal.MCP,
        )


@dataclass
class Sessao_do_Agente:
    """Quem está falando com o agente nesta conversa, e o que ele preparou."""

    ctx: ContextoRequisicao
    hoje: dt.date
    propostas: dict[str, dict] = field(default_factory=dict)
    chamadas: list[dict] = field(default_factory=list)


def _dinheiro(valor) -> str:
    return f"{Decimal(str(valor)).quantize(Decimal('0.01'))}"


# ------------------------------------------------------------------ leitura


def saldo_contas(sa: Sessao_do_Agente) -> dict:
    with Sessao(bind=engine()) as s:
        linhas = saldo_por_conta(s, sa.ctx)
        return {
            "contas": [
                {
                    "conta": c.nome,
                    "tipo": c.tipo.value,
                    "instituicao": c.instituicao,
                    "saldo": _dinheiro(c.saldo.valor) if c.saldo is not None else None,
                    "devido_no_cartao": _dinheiro(c.devido.valor) if c.devido is not None else None,
                    "saldo_conhecido": c.sabe_o_saldo,
                    "ultimo_movimento": c.ultimo_movimento.isoformat()
                    if c.ultimo_movimento
                    else None,
                }
                for c in linhas
            ]
        }


def consultar_lancamentos(
    sa: Sessao_do_Agente,
    de: str | None,
    ate: str | None,
    busca: str | None,
    tipo: str | None,
    limite: int,
) -> dict:
    filtro = FiltroLancamentos(
        de=dt.date.fromisoformat(de) if de else None,
        ate=dt.date.fromisoformat(ate) if ate else None,
        busca=busca or None,
        tipo=TipoLancamento(tipo) if tipo else None,
        limite=max(1, min(int(limite or 50), 200)),
    )
    with Sessao(bind=engine()) as s:
        pagina = listar_lancamentos(s, sa.ctx, filtro)
        return {
            "total_de_linhas_no_filtro": pagina.total_de_linhas,
            "soma_receitas_no_filtro": _dinheiro(pagina.receitas.valor),
            "soma_despesas_no_filtro": _dinheiro(pagina.despesas.valor),
            "linhas": [
                {
                    "data": ln.data.isoformat(),
                    "descricao": ln.descricao,
                    "valor": _dinheiro(ln.valor.valor),
                    "tipo": ln.tipo.value,
                    "conta": ln.conta_nome,
                    "categoria": ln.categoria_nome,
                }
                for ln in pagina.linhas
            ],
        }


def resumo_periodo(sa: Sessao_do_Agente, de: str, ate: str) -> dict:
    """Total por categoria no período. Só despesas ativas; transferência não é gasto."""
    from sqlalchemy import func

    from financeiro.persistencia.modelos import CategoriaORM

    d0, d1 = dt.date.fromisoformat(de), dt.date.fromisoformat(ate)
    with Sessao(bind=engine()) as s:
        # A autorização é a mesma da listagem de lançamentos.
        pagina = listar_lancamentos(s, sa.ctx, FiltroLancamentos(de=d0, ate=d1, limite=1))
        linhas = s.execute(
            select(CategoriaORM.nome, func.coalesce(func.sum(LancamentoORM.valor), 0), func.count())
            .join(CategoriaORM, CategoriaORM.id == LancamentoORM.categoria_id, isouter=True)
            .where(
                LancamentoORM.estado == "ativo",
                LancamentoORM.tipo == "despesa",
                LancamentoORM.data >= d0,
                LancamentoORM.data <= d1,
            )
            .group_by(CategoriaORM.nome)
            .order_by(func.sum(LancamentoORM.valor).desc())
        ).all()
        return {
            "periodo": {"de": de, "ate": ate},
            "total_receitas": _dinheiro(pagina.receitas.valor),
            "total_despesas": _dinheiro(pagina.despesas.valor),
            "despesas_por_categoria": [
                {"categoria": nome or "(sem categoria)", "total": _dinheiro(soma), "lancamentos": n}
                for nome, soma, n in linhas
            ],
        }


def orcamento_status(sa: Sessao_do_Agente, competencia: str | None) -> dict:
    mes = dt.date.fromisoformat(competencia) if competencia else sa.hoje.replace(day=1)
    with Sessao(bind=engine()) as s:
        p = painel_de_orcamento(s, sa.ctx, mes, hoje=sa.hoje)
        return {
            "competencia": p.competencia.isoformat(),
            "hoje": p.hoje.isoformat(),
            "categorias_com_teto": [
                {
                    "categoria": a.categoria_nome,
                    "teto": _dinheiro(a.teto.valor),
                    "consumido": _dinheiro(a.consumido.valor),
                    "restante": _dinheiro(a.restante.valor),
                    "situacao": a.situacao.value,
                    "projecao_fim_do_mes": _dinheiro(a.projecao.valor) if a.projecao else None,
                }
                for a in p.acompanhamentos
            ],
            "gastos_sem_teto": [
                {"categoria": g.categoria_nome, "gasto": _dinheiro(g.gasto.valor)}
                for g in p.fora_do_teto
            ],
            "sem_categoria": _dinheiro(p.sem_categoria.valor),
        }


def alertas_ativos(sa: Sessao_do_Agente) -> dict:
    with Sessao(bind=engine()) as s:
        achados = consultar_alertas(s, sa.ctx, hoje=sa.hoje)
        return {
            "hoje": sa.hoje.isoformat(),
            "alertas": [
                {
                    "tipo": a.tipo.value,
                    "titulo": a.titulo,
                    "valor_em_jogo": _dinheiro(a.valor.valor) if a.valor else None,
                    "detalhe": a.detalhe,
                }
                for a in achados
            ],
        }


def faturas_cartao(sa: Sessao_do_Agente) -> dict:
    with Sessao(bind=engine()) as s:
        v = consultar_faturas(s, sa.ctx)
        pagas = _pagamentos(s)
        return {
            "faturas": [
                {
                    "cartao": f.conta_nome,
                    "competencia": f.competencia.isoformat(),
                    "vencimento": f.vencimento.isoformat(),
                    "total": _dinheiro(f.total_declarado.valor),
                    "paga": pagas.get((f.conta_id, f.competencia), Decimal(0))
                    >= f.total_declarado.valor,
                }
                for f in v.faturas
            ]
        }


def _pagamentos(s) -> dict:
    from financeiro.aplicacao.consultas.alertas import _pagamentos_por_fatura

    return _pagamentos_por_fatura(s)


# ------------------------------------------------------------------ escrita


def preparar_lancamento(
    sa: Sessao_do_Agente, tipo: str, valor: str, data: str, conta: str, descricao: str
) -> dict:
    """Primeiro passo da escrita (§9.2): monta a proposta e devolve um token.
    Nada é gravado aqui. A pessoa precisa confirmar em linguagem natural e o
    agente então chama `confirmar_lancamento` com o token."""
    try:
        valor_d = (
            Decimal(valor.replace(".", "").replace(",", ".")) if "," in valor else Decimal(valor)
        )
    except InvalidOperation:
        return {"erro": f"valor inválido: {valor}"}
    if valor_d <= 0:
        return {"erro": "valor precisa ser positivo"}
    if tipo not in ("despesa", "receita"):
        return {"erro": "tipo precisa ser 'despesa' ou 'receita'"}
    with Sessao(bind=engine()) as s:
        conta_orm = (
            s.execute(select(ContaORM).where(ContaORM.nome.ilike(f"%{conta}%"))).scalars().first()
        )
        if conta_orm is None:
            return {"erro": f"conta '{conta}' não encontrada"}
        try:
            Guarda.exigir_alvo(sa.ctx, conta_orm.titular_id)
        except PermissaoNegada as e:
            Auditoria.registrar_recusa(s, sa.ctx, "preparar-lancamento", e.motivo, {"conta": conta})
            s.commit()
            return {"recusado": True, "motivo": e.motivo}
        token = secrets.token_hex(4)
        sa.propostas[token] = {
            "tipo": tipo,
            "valor": _dinheiro(valor_d),
            "data": data,
            "conta_id": str(conta_orm.id),
            "conta": conta_orm.nome,
            "descricao": descricao,
        }
        return {
            "token": token,
            "proposta": sa.propostas[token],
            "instrucao": "Mostre a proposta à pessoa e só chame confirmar_lancamento depois "
            "de ela confirmar.",
        }


def confirmar_lancamento(sa: Sessao_do_Agente, token: str) -> dict:
    prop = sa.propostas.pop(token, None)
    if prop is None:
        return {"erro": "token desconhecido ou já usado; prepare de novo"}
    with Sessao(bind=engine()) as s:
        conta_orm = s.get(ContaORM, UUID(prop["conta_id"]))
        try:
            Guarda.exigir_alvo(sa.ctx, conta_orm.titular_id)
        except PermissaoNegada as e:
            Auditoria.registrar_recusa(s, sa.ctx, "confirmar-lancamento", e.motivo, prop)
            s.commit()
            return {"recusado": True, "motivo": e.motivo}
        orm = LancamentoORM(
            tipo=prop["tipo"],
            valor=Decimal(prop["valor"]),
            data=dt.date.fromisoformat(prop["data"]),
            conta_id=conta_orm.id,
            descricao=prop["descricao"],
            membro_id=sa.ctx.membro.id,
            origem="agente",
            estado="ativo",
        )
        s.add(orm)
        s.flush()
        Auditoria.registrar(s, sa.ctx, "lancar", "lancamento", orm.id, {"via": "agente", **prop})
        s.commit()
        return {"gravado": True, "lancamento_id": str(orm.id), **prop}


def definir_teto(sa: Sessao_do_Agente, categoria: str, competencia: str, teto: str) -> dict:
    """Escrita que exige EDITAR_ORCAMENTO: membro pleno é recusado pela guarda."""
    from financeiro.aplicacao.servicos.orcamento import ServicoOrcamento
    from financeiro.dominio.dinheiro import Dinheiro
    from financeiro.persistencia.modelos import CategoriaORM

    with Sessao(bind=engine()) as s:
        cat = (
            s.execute(select(CategoriaORM).where(CategoriaORM.nome.ilike(categoria)))
            .scalars()
            .first()
        )
        if cat is None:
            return {"erro": f"categoria '{categoria}' não encontrada"}
        try:
            ServicoOrcamento.definir_teto(
                s, sa.ctx, cat.id, dt.date.fromisoformat(competencia), Dinheiro(teto)
            )
        except PermissaoNegada as e:
            s.commit()
            return {"recusado": True, "motivo": e.motivo}
        s.commit()
        return {
            "gravado": True,
            "categoria": cat.nome,
            "competencia": competencia,
            "teto": _dinheiro(teto),
        }
