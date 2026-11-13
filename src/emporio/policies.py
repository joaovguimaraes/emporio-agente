"""Base de conhecimento a partir do manual de políticas (PDF).

Pipeline:
1. Extração com pypdf em modo *layout* — o modo padrão devolve uma palavra
   por linha neste PDF, o que destruiria a separação por seções.
2. Remoção de cabeçalho/rodapé de página e divisão pelos títulos numerados
   ("4.1 Direito de Arrependimento ..."). Cada subseção vira um chunk; o
   documento tem ~30 seções, então chunking semântico seria exagero.
3. Busca lexical BM25 com normalização de acentos e stemming por prefixo.
   Para um corpus deste tamanho, embeddings adicionariam dependência,
   latência e custo sem ganho mensurável (ver README).
4. Notas de curadoria (errata) anexadas às seções que têm contradições
   ou ambiguidades no próprio PDF.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

from .config import DATA_DIR
from .text import stem, tokens

PDF_PATH = DATA_DIR / "politicas_da_loja.pdf"

_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2})?)\.?\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ].{2,80})$")
_PAGE_NOISE = re.compile(r"^(Empório da Música Manual de Políticas e Procedimentos|Página \d+)$")

# Decisões tomadas sobre contradições/ambiguidades do manual. Ficam anexadas ao
# trecho recuperado para que o modelo responda de forma consistente.
ERRATA: dict[str, str] = {
    "1": (
        "O número 'mais de 300 instrumentos' e a lista de categorias são institucionais. "
        "Para dizer o que existe para venda, consulte SEMPRE o catálogo (ferramenta de produtos): "
        "hoje não há itens cadastrados de sopro nem de cordas orquestrais."
    ),
    "7.1": (
        "Não há lista de lojas parceiras cadastrada no sistema: não invente nomes de lojas. "
        "Diga que a equipe pode indicar parceiros e ofereça encaminhar."
    ),
    "1.2": (
        "O e-mail correto é contato@emporiodamusica.com.br (sem acento; a versão "
        "'empório' com acento na tabela é erro de digitação — domínios não usam acento aqui)."
    ),
    "3": (
        "A tabela fala em parcela mínima de R$ 100,00, mas a seção 3.1 detalha faixas. "
        "Vale a regra mais específica (3.1): até 3x parcela mínima R$ 50,00; 4x–6x R$ 80,00; "
        "7x–12x R$ 100,00. Use a ferramenta de produtos, que já calcula o parcelamento."
    ),
    "3.1": (
        "Interpretação de 'sem valor mínimo (exceto abaixo de R$ 50,00)': parcelas de até 3x "
        "precisam ser de no mínimo R$ 50,00."
    ),
    "5.1": (
        "O manual não define o caso de pedido de exatamente R$ 500,00. Adotamos frete grátis "
        "(interpretação mais favorável ao consumidor, art. 47 do CDC)."
    ),
    "4.2": (
        "Atenção: dentro dos 90 dias de garantia legal (seção 8.1) a loja continua "
        "responsável junto com o fabricante (CDC art. 18). Não diga que o cliente 'precisa' "
        "ir ao fabricante nesse período — ofereça encaminhar para a equipe."
    ),
    "6.1": (
        "Esta lista descreve campanhas sazonais em geral. Só existe desconto se a promoção "
        "estiver ATIVA no sistema (ferramenta de produtos). Nunca prometa desconto de campanha "
        "só porque estamos no mês dela."
    ),
    "7": (
        "O manual traz dois números: (67) 3341-4444 (capa e tabela 'Telefone / WhatsApp') e "
        "(67) 3321-4500 (seção 7). Informe (67) 3341-4444, que aparece nas duas fontes oficiais "
        "de dados da empresa. A divergência foi reportada à loja."
    ),
}


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    parent_title: str
    text: str

    @property
    def heading(self) -> str:
        if self.parent_title and self.parent_title != self.title:
            return f"{self.id} {self.parent_title} > {self.title}"
        return f"{self.id} {self.title}"

    def render(self) -> str:
        body = f"## {self.heading}\n{self.text}"
        if self.id in ERRATA:
            body += f"\n[Nota de curadoria] {ERRATA[self.id]}"
        return body


def extract_text(pdf_path: Path = PDF_PATH) -> str:
    reader = PdfReader(str(pdf_path))
    lines: list[str] = []
    for page in reader.pages:
        for raw in page.extract_text(extraction_mode="layout").splitlines():
            line = raw.rstrip()
            if _PAGE_NOISE.match(line.strip()):
                continue
            lines.append(line)
    return "\n".join(lines)


def _clean_block(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^(•|\d+\.)\s+", r"\1 ", stripped)
        is_table_row = bool(re.search(r"\S\s{3,}\S", stripped))
        stripped = re.sub(r"\s{3,}", " | ", stripped)  # colunas de tabela
        is_item_start = stripped.startswith("•") or re.match(r"^\d+\.", stripped)
        prev_is_item = bool(out) and (out[-1].startswith("•") or re.match(r"^\d+\.", out[-1]))
        if line.startswith("    ") and prev_is_item and not is_item_start and not is_table_row:
            out[-1] += " " + stripped  # continuação de item de lista quebrado em 2 linhas
        else:
            out.append(stripped)
    return "\n".join(out)


def parse_sections(text: str) -> list[Section]:
    sections: list[Section] = []
    current_id, current_title, buf = "0", "Capa", []
    parents: dict[str, str] = {}

    def flush() -> None:
        body = _clean_block(buf)
        if body:
            parent = parents.get(current_id.split(".")[0], current_title)
            sections.append(Section(current_id, current_title, parent, body))

    for line in text.splitlines():
        m = _HEADING.match(line)  # títulos não têm indentação; itens de lista têm
        if m and not line.startswith(" "):
            flush()
            current_id, current_title, buf = m.group(1), m.group(2).strip(), []
            if "." not in current_id:
                parents[current_id] = current_title
            continue
        buf.append(line)
    flush()
    return sections


class PolicyIndex:
    """BM25 simples sobre as seções do manual."""

    def __init__(self, sections: list[Section], k1: float = 1.4, b: float = 0.6):
        self.sections = sections
        self.k1, self.b = k1, b
        self.docs = [self._terms(f"{s.parent_title} {s.title} {s.title} {s.text}") for s in sections]
        self.avgdl = sum(len(d) for d in self.docs) / len(self.docs)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    @staticmethod
    def _terms(text: str) -> list[str]:
        return [stem(t) for t in tokens(text)]

    def search(self, query: str, k: int = 3) -> list[tuple[Section, float]]:
        q = self._terms(query)
        scored = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            score = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                scored.append((self.sections[i], score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def get(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def table_of_contents(self) -> str:
        return "\n".join(f"- {s.id} {s.title}" for s in self.sections if s.id != "0")


@lru_cache(maxsize=1)
def index() -> PolicyIndex:
    return PolicyIndex(parse_sections(extract_text()))
