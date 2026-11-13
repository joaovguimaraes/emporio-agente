"""Carga e tratamento dos CSVs operacionais da loja.

Os dados têm várias inconsistências (ver `data_quality_issues()` e o README).
A regra geral adotada: campos estruturados (nome, preço, estoque, status,
specs) são a fonte da verdade; texto livre (descrição) só é exibido quando
não contradiz os campos estruturados.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .config import DATA_DIR
from .text import normalize

log = logging.getLogger(__name__)

# Marcas conhecidas — usadas para detectar descrições que citam outra marca
# (ex.: "Bateria Acústica Yamaha Kit 1" com descrição "Bateria Pearl Export").
KNOWN_BRANDS = [
    "yamaha", "tagima", "giannini", "rozini", "shelby", "crafter", "martin",
    "taylor", "takamine", "kala", "ohana", "kalani", "fender", "gibson",
    "ibanez", "prs", "music man", "pearl", "tama", "korg", "nord", "roland",
]

STATUS_LABELS = {
    "disponivel": "disponível",
    "esgotado": "esgotado no momento",
    "descontinuado": "descontinuado (não faz mais parte do catálogo)",
    "em_breve": "em breve (ainda não está à venda)",
}


def _brands_in(text: str) -> set[str]:
    norm = f" {normalize(text)} "
    return {b for b in KNOWN_BRANDS if f" {b} " in norm}


@dataclass
class StoreData:
    products: pd.DataFrame
    categories: pd.DataFrame
    customers: pd.DataFrame
    orders: pd.DataFrame
    order_items: pd.DataFrame
    promotions: pd.DataFrame
    issues: list[str] = field(default_factory=list)


def _availability(row: pd.Series) -> str:
    if row["status"] == "discontinued":
        return "descontinuado"
    if row["status"] == "coming_soon":
        return "em_breve"
    if row["status"] == "active" and row["stock_quantity"] > 0:
        return "disponivel"
    return "esgotado"


def load(data_dir: Path = DATA_DIR) -> StoreData:
    issues: list[str] = []
    read = lambda name: pd.read_csv(data_dir / f"{name}.csv", encoding="utf-8")  # noqa: E731

    categories = read("categories")
    products = read("products")
    customers = read("customers")
    orders = read("orders")
    order_items = read("order_items")
    promotions = read("promotions")

    # ---------- produtos ----------
    products["specs"] = products["specs"].apply(lambda s: json.loads(s) if isinstance(s, str) else {})
    products["status"] = products["status"].str.strip().str.lower()
    products = products.merge(
        categories[["category_id", "name"]].rename(columns={"name": "category"}),
        on="category_id",
        how="left",
    )
    products["availability"] = products.apply(_availability, axis=1)

    def description_ok(row: pd.Series) -> bool:
        cited = _brands_in(row["description"])
        own = _brands_in(row["name"])
        return not (cited - own)

    products["description_consistent"] = products.apply(description_ok, axis=1)
    for _, r in products[~products["description_consistent"]].iterrows():
        issues.append(
            f"Produto {r.product_id} ('{r['name']}'): descrição cita outra marca "
            f"({', '.join(sorted(_brands_in(r.description) - _brands_in(r['name'])))}) — descrição ocultada."
        )
    for _, r in products[(products["status"] == "discontinued") & (products["stock_quantity"] > 0)].iterrows():
        issues.append(f"Produto {r.product_id} descontinuado mas com estoque {r.stock_quantity} — tratado como indisponível.")
    for _, r in products[(products["status"] == "coming_soon") & (products["stock_quantity"] > 0)].iterrows():
        issues.append(f"Produto {r.product_id} 'coming_soon' com estoque {r.stock_quantity} — tratado como 'em breve'.")
    if not products["product_id"].is_monotonic_increasing:
        issues.append("products.csv fora de ordem por product_id (ex.: 145 antes de 131) — irrelevante após indexação.")
    empty_cats = set(categories["category_id"]) - set(products["category_id"])
    for cid in sorted(empty_cats):
        issues.append(
            f"Categoria {cid} ('{categories.set_index('category_id').at[cid, 'name']}') não tem nenhum produto cadastrado."
        )

    # ---------- promoções ----------
    promotions["is_active"] = promotions["is_active"].astype(int).astype(bool)
    active = promotions[promotions["is_active"]]
    dup = active[active.duplicated("product_id", keep=False)]
    if not dup.empty:
        issues.append(f"Produtos com mais de uma promoção ativa: {sorted(dup.product_id.unique())} — usa-se a de maior desconto.")
    best_promo = (
        active.sort_values("discount_percent", ascending=False)
        .drop_duplicates("product_id")
        .set_index("product_id")[["discount_percent", "description"]]
        .rename(columns={"description": "promo_name", "discount_percent": "promo_percent"})
    )
    products = products.merge(best_promo, left_on="product_id", right_index=True, how="left")
    for pid in sorted(set(active.product_id) - set(products.product_id)):
        issues.append(f"Promoção ativa para produto inexistente {pid}.")

    # ---------- pedidos ----------
    for col in ("order_date", "estimated_delivery"):
        orders[col] = pd.to_datetime(orders[col], errors="coerce").dt.date
    orders["notes"] = orders["notes"].fillna("")
    orders["tracking_code"] = orders["tracking_code"].fillna("")

    items = order_items.merge(products[["product_id", "name", "price_brl", "status"]], on="product_id", how="left")
    computed = items.assign(sub=items.quantity * items.price_brl).groupby("order_id")["sub"].sum()
    for _, o in orders.iterrows():
        calc = computed.get(o.order_id)
        if calc is not None and abs(calc - o.total_brl) > 0.01:
            issues.append(
                f"Pedido {o.order_id}: total registrado R$ {o.total_brl:.2f} difere da soma dos itens a preço atual "
                f"(R$ {calc:.2f}) — exibimos o total registrado no pedido."
            )
    for _, it in items[items["status"] == "coming_soon"].iterrows():
        issues.append(f"Pedido {it.order_id} contém produto {it.product_id} que está como 'coming_soon'.")

    # ---------- clientes ----------
    customers["email_norm"] = customers["email"].str.strip().str.lower()
    customers["phone_digits"] = customers["phone"].str.replace(r"\D", "", regex=True)
    for email, grp in customers.groupby("email_norm"):
        if len(grp) > 1:
            issues.append(
                f"E-mail compartilhado por clientes {list(grp.customer_id)} — e-mail sozinho não identifica o cliente."
            )

    return StoreData(products, categories, customers, orders, order_items, promotions, issues)


@lru_cache(maxsize=1)
def store() -> StoreData:
    data = load()
    for issue in data.issues:
        log.debug("qualidade de dados: %s", issue)
    return data
