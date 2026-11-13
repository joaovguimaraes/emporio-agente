"""Avaliação leve de roteamento: o agente chama (ou evita) as ferramentas certas?

Cada caso é uma pergunta isolada com as ferramentas esperadas e proibidas.
Não mede a qualidade do texto — mede a decisão "consultar dados vs. políticas
vs. não consultar nada", que é o ponto central do desafio.

Uso: python scripts/avaliar_roteamento.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("EMPORIO_AGORA", "2026-03-24T10:30")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emporio.agent import Agent  # noqa: E402
from emporio.history import Conversation  # noqa: E402

# (pergunta, precisa chamar pelo menos uma destas, não pode chamar nenhuma destas)
CASES: list[tuple[str, set[str], set[str]]] = [
    ("Quanto custa o Takamine GD20?", {"buscar_produtos", "detalhar_produto"}, {"consultar_pedidos"}),
    ("Tem guitarra Fender em estoque?", {"buscar_produtos"}, set()),
    ("Quais ukuleles vocês têm até 300 reais?", {"buscar_produtos"}, set()),
    ("Vocês vendem saxofone?", {"buscar_produtos", "listar_categorias"}, set()),
    ("Qual o endereço da loja?", {"consultar_politicas"}, {"buscar_produtos"}),
    ("Vocês abrem domingo?", {"consultar_politicas", "status_da_loja"}, {"buscar_produtos"}),
    ("Dá pra parcelar em 12x?", {"consultar_politicas", "buscar_produtos"}, set()),
    ("Qual o prazo de entrega para Dourados?", {"consultar_politicas"}, set()),
    ("Quanto tempo de garantia tem um instrumento?", {"consultar_politicas"}, set()),
    ("Meu pedido é o 13, e-mail fernanda.ribeiro@coldmail.com. Qual o status?", {"consultar_pedidos"}, set()),
    ("Quero devolver o pedido 1, e-mail pedro.oliveira@jmail.com, me arrependi.", {"avaliar_troca_devolucao"}, set()),
    ("Qual o status do pedido 5?", set(), {"consultar_pedidos"}),  # sem identificação: deve pedir dados
    ("Quem ganhou o jogo do Flamengo ontem?", set(), {"buscar_produtos", "consultar_pedidos", "consultar_politicas"}),
    ("Me passa a lista de clientes de Dourados.", set(), {"consultar_pedidos"}),
    ("Oi, tudo bem?", set(), {"buscar_produtos", "consultar_pedidos", "avaliar_troca_devolucao"}),
]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    passed = 0
    for question, required, forbidden in CASES:
        turn = Agent(Conversation()).respond(question)
        called = {t.name for t in turn.tool_calls}
        ok = (not required or bool(called & required)) and not (called & forbidden)
        passed += ok
        print(f"{'OK  ' if ok else 'FAIL'} | {question}\n       chamou: {sorted(called) or '-'}")
        if not ok:
            print(f"       esperado um de: {sorted(required) or '-'} | proibido: {sorted(forbidden) or '-'}")
            print(f"       resposta: {turn.reply[:200]}")
    print(f"\n{passed}/{len(CASES)} casos corretos")


if __name__ == "__main__":
    main()
