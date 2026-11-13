"""Ferramentas expostas ao LLM via function calling.

Cada ferramenta devolve um dicionário JSON-serializável. Elas nunca devolvem
dados pessoais de clientes (e-mail, telefone) e exigem verificação de
identidade para qualquer dado de pedido (LGPD — manual, seção 9).
"""

from __future__ import annotations

import difflib
import json
import uuid
from datetime import date
from typing import Any, Callable

import pandas as pd

from . import business, config
from .data import STATUS_LABELS, store
from .policies import index as policy_index
from .text import brl, normalize, only_digits, tokens

ORDER_STATUS_PT = {
    "pending": "aguardando confirmação de pagamento",
    "confirmed": "pagamento confirmado, em separação",
    "shipped": "enviado",
    "delivered": "entregue",
    "cancelled": "cancelado",
}
PAYMENT_PT = {
    "pix": "PIX",
    "boleto": "boleto bancário",
    "debit": "cartão de débito",
}

# Palavras genéricas que o cliente usa e que mapeiam para filtros do catálogo.
CATEGORY_ALIASES = {
    "violao": "Violões", "violoes": "Violões",
    "guitarra": "Guitarras", "guitarras": "Guitarras",
    "baixo": "Baixos", "baixos": "Baixos", "contrabaixo": "Baixos",
    "bateria": "Baterias e Percussão", "baterias": "Baterias e Percussão", "percussao": "Baterias e Percussão",
    "teclado": "Teclados e Pianos", "teclados": "Teclados e Pianos", "piano": "Teclados e Pianos",
    "sintetizador": "Teclados e Pianos",
    "ukulele": "Ukuleles", "ukuleles": "Ukuleles",
    "flauta": "Instrumentos de Sopro (Madeiras)", "clarinete": "Instrumentos de Sopro (Madeiras)",
    "saxofone": "Instrumentos de Sopro (Madeiras)", "sax": "Instrumentos de Sopro (Madeiras)",
    "oboe": "Instrumentos de Sopro (Madeiras)", "sopro": "Instrumentos de Sopro (Madeiras)",
    "trompete": "Instrumentos de Sopro (Metais)", "trombone": "Instrumentos de Sopro (Metais)",
    "tuba": "Instrumentos de Sopro (Metais)", "trompa": "Instrumentos de Sopro (Metais)",
    "violino": "Cordas Orquestrais", "violoncelo": "Cordas Orquestrais", "cello": "Cordas Orquestrais",
}

# Acessórios: a loja NÃO vende (manual, seções 1 e 7.1).
ACCESSORY_WORDS = {
    "palheta", "palhetas", "cabo", "cabos", "pedal", "pedais", "pedaleira", "amplificador", "amplificadores",
    "amp", "case", "cases", "capa", "bag", "encordoamento", "baqueta", "baquetas", "correia", "estante",
    "suporte", "metronomo", "capotraste", "fone", "microfone", "pele", "peles", "banco",
}


def _is_accessory_query(text: str) -> bool:
    toks = normalize(text).split()
    if ACCESSORY_WORDS & set(toks):
        return True
    # "cordas"/"corda" isolado é acessório; "7 cordas" ou "cordas de nylon" descreve um instrumento.
    for i, t in enumerate(toks):
        if t in ("corda", "cordas", "jogo"):
            prev = toks[i - 1] if i else ""
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if not prev.isdigit() and nxt not in ("nylon", "aco") and not (nxt == "de" and i + 2 < len(toks) and toks[i + 2] in ("nylon", "aco")):
                return True
    return False


def _payment_pt(code: str) -> str:
    if code.startswith("credit_"):
        return f"cartão de crédito em {code.split('_')[1]}"
    return PAYMENT_PT.get(code, code)


def _product_card(row: pd.Series, detailed: bool = False) -> dict:
    price = business.price_info(row.price_brl, row.get("promo_percent"), row.get("promo_name"))
    card: dict[str, Any] = {
        "id": int(row.product_id),
        "nome": row["name"],
        "categoria": row.category,
        "situacao": STATUS_LABELS[row.availability],
    }
    if row.availability == "disponivel":
        card["estoque"] = "últimas unidades" if row.stock_quantity <= 2 else "em estoque"
    card.update({k: v for k, v in price.items() if not k.startswith("_") and v is not None})
    if detailed:
        card["especificacoes"] = row.specs
        card["descricao"] = (
            row.description
            if row.description_consistent
            else "Descrição em revisão no cadastro — use apenas o nome e as especificações."
        )
    return card


def _text_score(query_toks: list[str], row: pd.Series) -> float:
    haystack = normalize(" ".join([row["name"], row.category, json.dumps(row.specs, ensure_ascii=False)]))
    hay_toks = set(haystack.split())
    name_compact = normalize(row["name"]).replace(" ", "")
    hits = 0.0
    for t in query_toks:
        if t in hay_toks or any(h.startswith(t) for h in hay_toks if len(t) >= 3):
            hits += 1
        elif len(t) >= 3 and t in name_compact:  # "gd20" casa com "GD20", "fg800" etc.
            hits += 1
    return hits / max(len(query_toks), 1)


def buscar_produtos(
    consulta: str | None = None,
    categoria: str | None = None,
    preco_max: float | None = None,
    preco_min: float | None = None,
    cordas: str | None = None,
    eletroacustico: bool | None = None,
    incluir_indisponiveis: bool = False,
    limite: int = 8,
) -> dict:
    df = store().products.copy()
    notes: list[str] = []

    if consulta and _is_accessory_query(consulta):
        return {
            "resultados": [],
            "fora_do_escopo": "A loja trabalha exclusivamente com instrumentos musicais e não vende acessórios "
            "(cordas, palhetas, cabos, cases, pedais, amplificadores etc.). Redirecione com educação; não há lista "
            "de lojas parceiras no sistema — não invente nomes.",
        }

    q_toks = tokens(consulta or "")
    # Palavras de categoria na consulta viram filtro; o resto vira busca textual.
    if not categoria:
        for t in list(q_toks):
            if t in CATEGORY_ALIASES:
                categoria = CATEGORY_ALIASES[t]
                q_toks.remove(t)
    for noise in ("nylon", "aco", "eletrico", "eletroacustico", "eletroacusticos", "disponivel", "disponiveis"):
        if noise in q_toks:
            q_toks.remove(noise)
            if noise == "nylon" and not cordas:
                cordas = "nylon"
            elif noise == "aco" and not cordas:
                cordas = "aco"
            elif noise.startswith("eletr") and eletroacustico is None:
                eletroacustico = True

    if categoria:
        cat_norm = normalize(categoria)
        mask = df.category.map(normalize).str.contains(cat_norm, regex=False)
        if not mask.any():
            alias = CATEGORY_ALIASES.get(cat_norm.split()[0])
            mask = df.category == alias if alias else mask
        if not mask.any():
            existing = store().categories.name.tolist()
            return {
                "resultados": [],
                "observacao": f"Não temos produtos cadastrados para '{categoria}'. Categorias com itens: "
                + ", ".join(sorted(set(store().products.category))),
                "categorias_da_loja": existing,
            }
        df = df[mask]
    if cordas:
        want = "nylon" if normalize(cordas).startswith("nylon") else "steel"
        df = df[df.specs.map(lambda s: s.get("strings") == want)]
    if eletroacustico is not None:
        df = df[df.specs.map(lambda s: (s.get("electronics") == "yes") == eletroacustico)]

    pricing = df.apply(
        lambda r: business.price_info(r.price_brl, r.get("promo_percent"), r.get("promo_name")), axis=1
    )
    df = df.assign(_efetivo=pricing.map(lambda p: p["_preco_efetivo"]), _avista=pricing.map(lambda p: p["_melhor_preco_a_vista"]))

    pix_only: pd.DataFrame | None = None
    if preco_max is not None:
        pix_only = df[(df._efetivo > preco_max) & (df._avista <= preco_max) & (df.availability == "disponivel")]
        df = df[df._efetivo <= preco_max]
    if preco_min is not None:
        df = df[df._efetivo >= preco_min]

    if q_toks:
        df = df.assign(_score=df.apply(lambda r: _text_score(q_toks, r), axis=1))
        strong = df[df._score >= 0.99]
        df = strong if not strong.empty else df[df._score >= 0.5]
        df = df.sort_values(["_score", "_efetivo"], ascending=[False, True])
    else:
        df = df.sort_values("_efetivo")

    unavailable = df[df.availability != "disponivel"]
    out: dict[str, Any] = {}
    if not incluir_indisponiveis:
        if q_toks and not unavailable.empty:
            # Cliente perguntou por um modelo específico que existe mas não está à venda:
            # informamos a situação (e alternativas) em vez de dizer que "não existe".
            out["encontrados_mas_indisponiveis"] = [
                detalhar_produto(int(r.product_id)) for _, r in unavailable.head(2).iterrows()
            ]
        df = df[df.availability == "disponivel"]

    results = [_product_card(r, detailed=bool(q_toks) and len(df) <= 3) for _, r in df.head(limite).iterrows()]
    out = {"total_encontrado": int(len(df)), "resultados": results, **out}
    if len(df) > limite:
        out["observacao_limite"] = f"Mostrando {limite} de {len(df)}; refine a busca se necessário."
    if pix_only is not None and not pix_only.empty:
        out["cabem_no_orcamento_pagando_no_pix"] = [
            {"nome": r["name"], "preco_tabela": brl(r.price_brl), "preco_pix": brl(r._avista)}
            for _, r in pix_only.sort_values("_avista").iterrows()
        ]
    if not results and q_toks and "encontrados_mas_indisponiveis" not in out:
        names = store().products["name"].tolist()
        close = difflib.get_close_matches(consulta or "", names, n=3, cutoff=0.4)
        if close:
            notes.append("Nenhum resultado exato. Nomes parecidos no catálogo: " + "; ".join(close))
        else:
            notes.append("Nenhum produto correspondente no catálogo. Não invente produtos.")
    if notes:
        out["observacoes"] = notes
    return out


def detalhar_produto(produto_id: int) -> dict:
    df = store().products
    row = df[df.product_id == int(produto_id)]
    if row.empty:
        return {"erro": f"Produto {produto_id} não encontrado."}
    r = row.iloc[0]
    card = _product_card(r, detailed=True)
    if r.availability != "disponivel":
        alt = df[(df.category_id == r.category_id) & (df.availability == "disponivel")].copy()
        alt["_dist"] = (alt.price_brl - r.price_brl).abs()
        card["alternativas_disponiveis"] = [_product_card(a) for _, a in alt.nsmallest(3, "_dist").iterrows()]
    return card


def listar_categorias() -> dict:
    s = store()
    counts = s.products[s.products.availability == "disponivel"].groupby("category_id").size()
    return {
        "categorias": [
            {"nome": c["name"], "descricao": c.description, "produtos_disponiveis": int(counts.get(c.category_id, 0))}
            for _, c in s.categories.iterrows()
        ],
        "observacao": "Categorias com 0 produtos disponíveis não têm itens no catálogo atual.",
    }


# --------------------------------------------------------------------------- pedidos

def _match_customers(identificacao: str) -> pd.DataFrame:
    customers = store().customers
    ident = identificacao.strip().lower()
    if "@" in ident:
        return customers[customers.email_norm == ident]
    digits = only_digits(ident)
    if len(digits) < 8:
        return customers.iloc[0:0]
    # Aceita com ou sem DDD/código do país: compara pelos últimos dígitos.
    tail = digits[-min(len(digits), 11):]
    return customers[customers.phone_digits.str.endswith(tail) | (customers.phone_digits == digits[-11:])]


def _order_card(o: pd.Series) -> dict:
    s = store()
    items = s.order_items[s.order_items.order_id == o.order_id].merge(
        s.products[["product_id", "name"]], on="product_id", how="left"
    )
    today = config.now().date()
    card: dict[str, Any] = {
        "numero_pedido": int(o.order_id),
        "data_pedido": o.order_date.strftime("%d/%m/%Y"),
        "status": ORDER_STATUS_PT.get(o.status, o.status),
        "itens": [f"{int(i.quantity)}x {i['name']}" for _, i in items.iterrows()],
        "total": brl(o.total_brl),
        "forma_pagamento": _payment_pt(o.payment_method),
    }
    if o.tracking_code:
        card["codigo_rastreio"] = o.tracking_code
        card["onde_rastrear"] = "site dos Correios ou da Jadlog"
    if isinstance(o.estimated_delivery, date):
        label = "data_entrega_registrada" if o.status == "delivered" else "previsao_entrega"
        card[label] = o.estimated_delivery.strftime("%d/%m/%Y")
        if o.status == "shipped" and o.estimated_delivery < today:
            card["alerta"] = (
                f"Previsão de entrega vencida há {(today - o.estimated_delivery).days} dias. "
                "Pedir desculpas e encaminhar para a equipe verificar com a transportadora."
            )
    if o.status in ("pending", "confirmed"):
        age = (today - o.order_date).days
        if age > 7:
            card["alerta"] = (
                f"Pedido está como '{ORDER_STATUS_PT[o.status]}' há {age} dias — fora do normal. "
                "Encaminhar para a equipe verificar."
            )
    if o.notes:
        card["observacao"] = o.notes
    return card


def _verify(identificacao: str, numero_pedido: int | None) -> tuple[pd.DataFrame | None, dict | None]:
    s = store()
    matched = _match_customers(identificacao or "")
    generic_fail = {
        "verificado": False,
        "mensagem": "Não localizamos pedidos com esses dados. Confira o número do pedido e o e-mail/telefone "
        "usados na compra.",
    }
    if matched.empty:
        return None, generic_fail
    orders = s.orders[s.orders.customer_id.isin(matched.customer_id)]
    if numero_pedido is not None:
        orders = orders[orders.order_id == int(numero_pedido)]
        if orders.empty:  # mesmo erro genérico: não revela se o pedido existe para outra pessoa
            return None, generic_fail
    elif matched.customer_id.nunique() > 1:
        return None, {
            "verificado": False,
            "mensagem": "Esse dado está associado a mais de um cadastro. Peça o número do pedido ou o telefone.",
        }
    return orders, None


def consultar_pedidos(identificacao: str, numero_pedido: int | None = None) -> dict:
    orders, fail = _verify(identificacao, numero_pedido)
    if fail:
        return fail
    s = store()
    cust_id = orders.customer_id.iloc[0] if not orders.empty else None
    first_name = (
        s.customers.set_index("customer_id").at[cust_id, "name"].split()[0] if cust_id is not None else None
    )
    return {
        "verificado": True,
        "primeiro_nome_cliente": first_name,
        "pedidos": [_order_card(o) for _, o in orders.sort_values("order_date", ascending=False).iterrows()],
    }


def avaliar_troca_devolucao(numero_pedido: int, identificacao: str, motivo: str) -> dict:
    orders, fail = _verify(identificacao, numero_pedido)
    if fail:
        return fail
    o = orders.iloc[0]
    received = o.estimated_delivery if o.status == "delivered" and isinstance(o.estimated_delivery, date) else None
    result = business.assess_return(motivo, o.status, o.order_date, received, config.now().date())
    out = {"verificado": True, "pedido": _order_card(o), "avaliacao": result.as_dict()}
    out["premissas"] = [
        "Data de recebimento aproximada pela data de entrega registrada no pedido.",
        "Pedidos do sistema são tratados como compras online (direito de arrependimento aplicável).",
        "Itens não elegíveis (seção 4.4): personalizados/regulados sob encomenda, 'venda final' em liquidação, "
        "boquilhas de sopro — confirmar com o cliente.",
    ]
    return out


# --------------------------------------------------------------------------- políticas e loja

def consultar_politicas(pergunta: str) -> dict:
    hits = policy_index().search(pergunta, k=3)
    if not hits:
        return {"trechos": [], "observacao": "Nada encontrado no manual. Não invente: ofereça encaminhar à equipe."}
    return {"trechos": [s.render() for s, _ in hits]}


def status_da_loja() -> dict:
    return business.store_status(config.now())


def encaminhar_para_equipe(motivo: str, resumo: str, contato_cliente: str | None = None) -> dict:
    ticket = {
        "protocolo": "EM-" + uuid.uuid4().hex[:8].upper(),
        "criado_em": config.now().isoformat(timespec="minutes"),
        "motivo": motivo,
        "resumo": resumo,
        "contato_cliente": contato_cliente,
    }
    config.RUNTIME_DIR.mkdir(exist_ok=True)
    with open(config.RUNTIME_DIR / "tickets.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(ticket, ensure_ascii=False) + "\n")
    status = status_da_loja()
    return {
        "protocolo": ticket["protocolo"],
        "prazo_retorno": "até 24 horas úteis",
        "loja_aberta_agora": status["aberta_agora"],
        "proxima_abertura": status["proxima_abertura"],
        "precisa_contato": contato_cliente is None,
    }


# --------------------------------------------------------------------------- schemas

TOOLS: dict[str, Callable[..., dict]] = {
    "buscar_produtos": buscar_produtos,
    "detalhar_produto": detalhar_produto,
    "listar_categorias": listar_categorias,
    "consultar_pedidos": consultar_pedidos,
    "avaliar_troca_devolucao": avaliar_troca_devolucao,
    "consultar_politicas": consultar_politicas,
    "status_da_loja": status_da_loja,
    "encaminhar_para_equipe": encaminhar_para_equipe,
}

_IDENT = {
    "type": "string",
    "description": "E-mail ou telefone usado na compra, informado pelo PRÓPRIO cliente nesta conversa.",
}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "buscar_produtos",
            "description": "Busca no catálogo em tempo real: preço, promoção ativa, preço no PIX, parcelamento e "
            "disponibilidade. Use para QUALQUER pergunta sobre produto, preço, estoque ou recomendação.",
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {"type": "string", "description": "Texto livre: marca, modelo ou tipo (ex.: 'Takamine GD20', 'violão 7 cordas')."},
                    "categoria": {
                        "type": "string",
                        "description": "Categoria: Guitarras, Baixos, Baterias e Percussão, Teclados e Pianos, Violões, Ukuleles, "
                        "Instrumentos de Sopro (Madeiras), Instrumentos de Sopro (Metais), Cordas Orquestrais.",
                    },
                    "preco_max": {"type": "number", "description": "Preço máximo em reais."},
                    "preco_min": {"type": "number", "description": "Preço mínimo em reais."},
                    "cordas": {"type": "string", "enum": ["nylon", "aco"]},
                    "eletroacustico": {"type": "boolean", "description": "true = só com captação; false = só acústicos."},
                    "incluir_indisponiveis": {"type": "boolean", "description": "Inclui esgotados/descontinuados/em breve."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detalhar_produto",
            "description": "Detalhes de um produto pelo id (especificações, descrição) e alternativas se indisponível.",
            "parameters": {
                "type": "object",
                "properties": {"produto_id": {"type": "integer"}},
                "required": ["produto_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_categorias",
            "description": "Lista as categorias da loja e quantos produtos disponíveis cada uma tem.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_pedidos",
            "description": "Status, itens, rastreio e previsão de pedidos. Exige e-mail ou telefone do cliente; "
            "o número do pedido é opcional mas recomendado.",
            "parameters": {
                "type": "object",
                "properties": {"identificacao": _IDENT, "numero_pedido": {"type": "integer"}},
                "required": ["identificacao"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "avaliar_troca_devolucao",
            "description": "Aplica as regras de troca/devolução/garantia a um pedido real e calcula os prazos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "numero_pedido": {"type": "integer"},
                    "identificacao": _IDENT,
                    "motivo": {
                        "type": "string",
                        "enum": ["arrependimento", "defeito", "preferencia"],
                        "description": "arrependimento = desistiu da compra; defeito = problema de fabricação; "
                        "preferencia = trocar por outra cor/modelo.",
                    },
                },
                "required": ["numero_pedido", "identificacao", "motivo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_politicas",
            "description": "Busca no manual de políticas da loja: endereço, contato, horários, formas de pagamento, "
            "parcelamento, trocas, devoluções, garantia, frete, promoções (regras), privacidade.",
            "parameters": {
                "type": "object",
                "properties": {"pergunta": {"type": "string", "description": "A dúvida em linguagem natural."}},
                "required": ["pergunta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "status_da_loja",
            "description": "Data/hora atual em Campo Grande, se a loja está aberta agora, feriado e próxima abertura.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "encaminhar_para_equipe",
            "description": "Abre um chamado para atendente humano (reclamação, cancelamento, troca/devolução a "
            "efetivar, atraso de entrega, dúvida que o manual não resolve, pedido do cliente para falar com humano).",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "enum": ["reclamacao", "troca_devolucao", "cancelamento", "atraso_entrega", "duvida", "outro"]},
                    "resumo": {"type": "string", "description": "Resumo objetivo do caso, com número do pedido se houver."},
                    "contato_cliente": {"type": "string", "description": "Telefone ou e-mail para retorno, se o cliente informou."},
                },
                "required": ["motivo", "resumo"],
            },
        },
    },
]


def run_tool(name: str, arguments: str | dict) -> dict:
    """Executa a ferramenta com tratamento de erros — erro vira resposta para o modelo, não exceção."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"erro": f"Ferramenta desconhecida: {name}"}
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        args = {k: v for k, v in args.items() if v is not None}
        return fn(**args)
    except Exception as exc:  # noqa: BLE001 — o modelo precisa saber que falhou
        return {"erro": f"Falha ao executar {name}: {type(exc).__name__}: {exc}"}
