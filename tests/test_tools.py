"""Testes determinísticos das ferramentas e regras (sem chamar LLM)."""

from datetime import date, datetime

import pytest

from emporio import business
from emporio.config import STORE_TZ
from emporio.data import store
from emporio.policies import index
from emporio.tools import avaliar_troca_devolucao, buscar_produtos, consultar_pedidos, detalhar_produto


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setenv("EMPORIO_AGORA", "2026-03-20T10:30")


# ----------------------------------------------------------------- dados

def test_data_quality_issues_detected():
    issues = "\n".join(store().issues)
    assert "Produto 139" in issues  # descrição cita Pearl num produto Yamaha
    assert "Pedido 20" in issues  # total diverge da soma dos itens
    assert "[11, 43]" in issues  # e-mail compartilhado


# ----------------------------------------------------------------- preço

def test_pix_discount_not_cumulative_with_promo():
    promo = business.price_info(2399, 18, "Desconto Exclusivo")
    assert promo["preco_promocional"] == "R$ 1.967,18"
    assert promo["preco_pix"] is None
    regular = business.price_info(2199)
    assert regular["preco_pix"] == "R$ 2.089,05"


@pytest.mark.parametrize(
    "total,expected",
    [(159.9, 3), (140.0, 2), (429.9, 5), (749.0, 7), (2199.0, 12), (99.0, 1)],
)
def test_installment_rules(total, expected):
    assert business.max_installments(total) == expected


# ----------------------------------------------------------------- catálogo

def test_price_lookup_takamine():
    res = buscar_produtos(consulta="Takamine GD20")
    assert res["resultados"][0]["nome"].startswith("Takamine GD20")
    assert res["resultados"][0]["preco_tabela"] == "R$ 2.199,00"


def test_violoes_ate_1000_excludes_unavailable():
    res = buscar_produtos(categoria="Violões", preco_max=1000, limite=50)
    names = [r["nome"] for r in res["resultados"]]
    assert "Giannini GF-3D Dreadnought Sunburst" not in names  # estoque 0
    assert "Shelby SN-7C 7 Cordas Nylon Natural" not in names  # descontinuado
    assert len(names) == 12
    assert res["cabem_no_orcamento_pagando_no_pix"][0]["preco_pix"] == "R$ 996,55"


def test_out_of_stock_returns_alternatives():
    res = buscar_produtos(consulta="Giannini GF-3D")
    item = res["encontrados_mas_indisponiveis"][0]
    assert item["situacao"].startswith("esgotado")
    assert item["alternativas_disponiveis"]


def test_discontinued_with_stock_is_not_sold():
    assert detalhar_produto(136)["situacao"].startswith("descontinuado")


def test_inconsistent_description_is_hidden():
    assert "revisão" in detalhar_produto(139)["descricao"]


@pytest.mark.parametrize("q", ["cordas de violão", "palhetas", "amplificador para guitarra", "case para violão"])
def test_accessories_out_of_scope(q):
    assert "fora_do_escopo" in buscar_produtos(consulta=q)


def test_seven_string_guitar_is_not_accessory():
    assert buscar_produtos(consulta="violão 7 cordas")["resultados"]


def test_empty_category():
    res = buscar_produtos(consulta="flauta")
    assert res["resultados"] == [] and "Não temos" in res["observacao"]


# ----------------------------------------------------------------- pedidos / LGPD

def test_order_requires_matching_identity():
    ok = consultar_pedidos("anacarol.ferreira@coldmail.com", 8)
    assert ok["verificado"] and ok["pedidos"][0]["status"] == "enviado"
    other = consultar_pedidos("lucas.mendes@jmail.com", 8)  # cliente existe, mas o pedido não é dele
    assert not other["verificado"]
    unknown = consultar_pedidos("ninguem@x.com", 8)
    assert other["mensagem"] == unknown["mensagem"]  # não revela se o pedido existe


def test_shared_email_is_ambiguous_without_order_number():
    assert not consultar_pedidos("bruno.martins@jmail.com")["verificado"]
    assert consultar_pedidos("(67) 98543-2109")["verificado"]


def test_order_items_carry_category():
    # Regressão: sem a categoria, o modelo leu "Shelby SU-21S Soprano" como saxofone (é um ukulele).
    item = consultar_pedidos("leticia.rocha@jmail.com", 7)["pedidos"][0]["itens"][0]
    assert "Ukuleles" in item


def test_late_shipment_flagged():
    assert "alerta" in consultar_pedidos("anacarol.ferreira@coldmail.com", 8)["pedidos"][0]


# ----------------------------------------------------------------- trocas

def test_regret_window_expired():
    res = avaliar_troca_devolucao(7, "leticia.rocha@jmail.com", "arrependimento")
    assert res["avaliacao"]["elegivel"] is False


def test_regret_within_window():
    r = business.assess_return("arrependimento", "delivered", date(2026, 3, 10), date(2026, 3, 15), date(2026, 3, 20))
    assert r.elegivel is True


def test_defect_after_30_days_falls_in_legal_warranty():
    r = business.assess_return("defeito", "delivered", date(2026, 2, 3), date(2026, 2, 17), date(2026, 3, 20))
    assert r.regra.startswith("8.1")


# ----------------------------------------------------------------- horário

def test_store_closed_on_sunday_and_holiday():
    sunday = business.store_status(datetime(2026, 3, 22, 10, 0, tzinfo=STORE_TZ))
    assert not sunday["aberta_agora"] and "segunda" in sunday["proxima_abertura"]
    tiradentes = business.store_status(datetime(2026, 4, 21, 10, 0, tzinfo=STORE_TZ))
    assert not tiradentes["aberta_agora"] and tiradentes["feriado_hoje"]
    saturday_afternoon = business.store_status(datetime(2026, 3, 21, 14, 0, tzinfo=STORE_TZ))
    assert not saturday_afternoon["aberta_agora"]


# ----------------------------------------------------------------- políticas

@pytest.mark.parametrize(
    "question,section",
    [
        ("me arrependi da compra, posso devolver?", "4.1"),
        ("qual o horário de funcionamento no sábado?", "2"),
        ("em quantas vezes posso parcelar?", "3.1"),
        ("meu violão veio com defeito", "4.2"),
        ("qual o endereço da loja?", "1.2"),
        ("quanto tempo de garantia?", "8.1"),
    ],
)
def test_policy_retrieval(question, section):
    ids = [s.id for s, _ in index().search(question, k=3)]
    assert section in ids


def test_policy_retrieval_compound_question():
    # Regressão: os termos de horário dominavam e a seção de endereço (1.2) sumia do top-3.
    q = "Qual o endereço da loja e o horário de funcionamento, incluindo sábado à tarde?"
    ids = [s.id for s, _ in index().search(q)]
    assert "1.2" in ids and "2" in ids
