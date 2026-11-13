"""Regras de negócio determinísticas.

Tudo que envolve conta (desconto, parcela, prazo em dias) é calculado aqui, em
Python, e entregue pronto ao modelo. LLMs erram aritmética e datas com
frequência; numa loja isso vira "propaganda enganosa" (manual, seção 7.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache

import holidays

from .text import brl

PIX_DISCOUNT = 0.05

# (parcelas_min, parcelas_max, parcela_minima) — seção 3.1 do manual.
INSTALLMENT_RULES = [(1, 3, 50.0), (4, 6, 80.0), (7, 12, 100.0)]

OPENING_HOURS: dict[int, tuple[time, time] | None] = {
    0: (time(9), time(18)),
    1: (time(9), time(18)),
    2: (time(9), time(18)),
    3: (time(9), time(18)),
    4: (time(9), time(18)),
    5: (time(9), time(13)),
    6: None,
}
WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


# --------------------------------------------------------------------------- preço

def max_installments(total: float) -> int:
    best = 1
    for lo, hi, min_parcel in INSTALLMENT_RULES:
        for n in range(lo, hi + 1):
            if n == 1 or total / n >= min_parcel:
                best = max(best, n)
    return best


def price_info(price: float, promo_percent: float | None = None, promo_name: str | None = None) -> dict:
    has_promo = promo_percent is not None and promo_percent == promo_percent and promo_percent > 0  # NaN-safe
    info: dict = {"preco_tabela": brl(price)}
    if has_promo:
        final = round(price * (1 - promo_percent / 100), 2)
        info.update(
            {
                "promocao_ativa": f"{promo_name} — {promo_percent:g}% de desconto",
                "preco_promocional": brl(final),
                "preco_pix": None,
                "obs_pix": "Desconto de PIX não é cumulativo com promoção.",
            }
        )
    else:
        final = price
        info.update({"promocao_ativa": None, "preco_pix": brl(round(price * (1 - PIX_DISCOUNT), 2))})
    n = max_installments(final)
    info["parcelamento"] = f"até {n}x de {brl(round(final / n, 2))} sem juros" if n > 1 else "à vista"
    info["_preco_efetivo"] = final
    info["_melhor_preco_a_vista"] = final if has_promo else round(price * (1 - PIX_DISCOUNT), 2)
    return info


# --------------------------------------------------------------------------- horário

@lru_cache(maxsize=8)
def _holidays(year: int) -> holidays.HolidayBase:
    h = holidays.Brazil(years=year, subdiv="MS")
    h[date(year, 8, 26)] = "Aniversário de Campo Grande"  # feriado municipal
    return h


def holiday_name(d: date) -> str | None:
    return _holidays(d.year).get(d)


def hours_for(d: date) -> tuple[time, time] | None:
    if holiday_name(d):
        return None
    return OPENING_HOURS[d.weekday()]


def store_status(now: datetime) -> dict:
    today = now.date()
    hours = hours_for(today)
    is_open = bool(hours and hours[0] <= now.time() < hours[1])
    nxt = None
    if not is_open:
        for delta in range(0, 15):
            d = today + timedelta(days=delta)
            h = hours_for(d)
            if h and (delta > 0 or now.time() < h[0]):
                label = WEEKDAYS_PT[d.weekday()]
                label = "hoje" if delta == 0 else (f"amanhã, {label}" if delta == 1 else label)
                nxt = f"{label} ({d.strftime('%d/%m')}) às {h[0].strftime('%H:%M')}"
                break
    return {
        "agora": now.strftime("%d/%m/%Y %H:%M") + f" ({WEEKDAYS_PT[now.weekday()]})",
        "aberta_agora": is_open,
        "feriado_hoje": holiday_name(today),
        "horario_hoje": f"{hours[0].strftime('%H:%M')} às {hours[1].strftime('%H:%M')}" if hours else "fechada",
        "proxima_abertura": nxt,
        "horario_padrao": "Seg a sex 09:00–18:00; sáb 09:00–13:00; domingos e feriados fechada.",
    }


# --------------------------------------------------------------------------- trocas

@dataclass
class ReturnAssessment:
    elegivel: bool | None  # None = depende de análise humana
    regra: str
    explicacao: str
    proximo_passo: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def assess_return(
    reason: str,
    status: str,
    order_date: date,
    received_date: date | None,
    today: date,
) -> ReturnAssessment:
    """Avalia troca/devolução segundo as seções 4 e 8 do manual.

    Premissa: os dados não trazem a data real de entrega; usamos
    `estimated_delivery` dos pedidos com status "delivered" como aproximação.
    """
    if status == "cancelled":
        return ReturnAssessment(False, "-", "O pedido está cancelado.", "Nenhuma ação necessária.")
    if status != "delivered":
        return ReturnAssessment(
            None,
            "4.1 (direito de arrependimento)",
            "O pedido ainda não foi entregue. O prazo de arrependimento só começa a contar no recebimento, "
            "então o cliente ainda está dentro do prazo; o cancelamento antes da entrega é feito pela equipe.",
            "Encaminhar para a equipe solicitar o cancelamento.",
        )

    received = received_date or order_date
    days_received = (today - received).days
    days_purchase = (today - order_date).days

    if reason == "arrependimento":
        if days_received <= 7:
            return ReturnAssessment(
                True,
                "4.1 Direito de Arrependimento",
                f"Recebido há {days_received} dia(s) (limite: 7 dias corridos). Produto deve estar na embalagem "
                "original, sem sinais de uso, com acessórios e manuais. Reembolso na mesma forma de pagamento em "
                "até 10 dias úteis; frete de devolução por conta da loja.",
                "Encaminhar para a equipe abrir a devolução.",
            )
        return ReturnAssessment(
            False,
            "4.1 Direito de Arrependimento",
            f"Recebido há {days_received} dias; o prazo de arrependimento é de 7 dias corridos após o recebimento. "
            "Se houver defeito de fabricação, a troca/garantia pode se aplicar.",
            "Perguntar se há algum defeito; se sim, avaliar como defeito.",
        )

    if reason == "preferencia":
        if days_received <= 7:
            return ReturnAssessment(
                True,
                "4.3 Trocas por Preferência",
                f"Recebido há {days_received} dia(s) (limite: 7 dias). Sujeito à disponibilidade do produto desejado; "
                "item em perfeito estado e na embalagem original; diferença de valor cobrada ou reembolsada.",
                "Verificar o estoque do produto desejado e encaminhar para a equipe.",
            )
        return ReturnAssessment(
            False,
            "4.3 Trocas por Preferência",
            f"Recebido há {days_received} dias; troca por preferência é aceita em até 7 dias.",
            "Informar com empatia; oferecer ajuda com outra compra.",
        )

    if reason == "defeito":
        if days_purchase <= 30:
            return ReturnAssessment(
                True,
                "4.2 Trocas por Defeito",
                f"Compra feita há {days_purchase} dia(s) (limite: 30 dias corridos). Troca direto na loja, "
                "desde que não seja dano por mau uso, queda, umidade ou modificação.",
                "Encaminhar para a equipe avaliar o produto e abrir a troca.",
            )
        if days_received <= 90:
            return ReturnAssessment(
                None,
                "8.1 Garantia Legal (90 dias)",
                f"Passou dos 30 dias de troca direta (compra há {days_purchase} dias), mas o produto está dentro "
                f"da garantia legal de 90 dias (recebido há {days_received} dias). A loja intermedia o atendimento.",
                "Encaminhar para a equipe abrir o atendimento de garantia.",
            )
        return ReturnAssessment(
            None,
            "8.2 Garantia do Fabricante",
            f"Fora da troca direta e da garantia legal (recebido há {days_received} dias). Pode haver garantia do "
            "fabricante (de 6 meses a 2 anos, conforme certificado do produto); a loja pode intermediar.",
            "Pedir que o cliente verifique o certificado de garantia e oferecer encaminhar para a equipe.",
        )

    raise ValueError(f"motivo inválido: {reason}")
