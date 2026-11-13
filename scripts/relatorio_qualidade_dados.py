"""Imprime as inconsistências encontradas nos dados e no manual.

Uso: python scripts/relatorio_qualidade_dados.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emporio import business  # noqa: E402
from emporio.data import store  # noqa: E402
from emporio.policies import ERRATA  # noqa: E402


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    s = store()
    print("== CSVs ==")
    for issue in s.issues:
        print(" -", issue)

    print("\n== Pedidos históricos fora da regra de parcelamento (seção 3.1) ==")
    for _, o in s.orders.iterrows():
        if o.payment_method.startswith("credit_"):
            n = int(o.payment_method.split("_")[1].rstrip("x"))
            if n > business.max_installments(o.total_brl):
                print(f" - Pedido {o.order_id}: R$ {o.total_brl:.2f} em {n}x = R$ {o.total_brl / n:.2f}/parcela")

    print("\n== Manual de políticas: decisões de curadoria ==")
    for sec, note in ERRATA.items():
        print(f" - Seção {sec}: {note}")


if __name__ == "__main__":
    main()
