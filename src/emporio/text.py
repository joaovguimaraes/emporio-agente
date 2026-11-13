"""Utilitários de texto: normalização, tokenização e formatação de moeda."""

from __future__ import annotations

import re
import unicodedata

_STOPWORDS = {
    "a", "o", "as", "os", "um", "uma", "de", "da", "do", "das", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "pra", "por", "com", "que", "qual", "quais",
    "quanto", "custa", "voces", "vcs", "tem", "ter", "me", "meu", "minha", "eu",
    "se", "ao", "aos", "ou", "mais", "muito", "algum", "alguma", "sobre", "como",
    "posso", "pode", "the",
}


def strip_accents(text: str) -> str:
    # NFKD também resolve nomes de arquivo em NFD (ex.: o "políticas" do zip).
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    text = strip_accents(str(text)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def tokens(text: str, drop_stopwords: bool = True) -> list[str]:
    toks = normalize(text).split()
    if drop_stopwords:
        toks = [t for t in toks if t not in _STOPWORDS]
    return toks


def stem(token: str, size: int = 5) -> str:
    """Stemming grosseiro por prefixo — suficiente para um corpus pequeno em PT-BR
    ("devolver"/"devolução" -> "devol", "arrependi"/"arrependimento" -> "arrep")."""
    return token[:size] if not token.isdigit() else token


def brl(value: float) -> str:
    """Formata no padrão brasileiro: 1234.5 -> 'R$ 1.234,50'."""
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def only_digits(text: str) -> str:
    return re.sub(r"\D", "", str(text))
