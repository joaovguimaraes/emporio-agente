"""Configuração central: caminhos, provedor de LLM e data de referência."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
RUNTIME_DIR = ROOT_DIR / "runtime"
SESSIONS_DIR = ROOT_DIR / ".sessions"

# A loja fica em Campo Grande/MS (UTC-4, sem horário de verão desde 2019).
STORE_TZ = ZoneInfo("America/Campo_Grande")

load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    model: str
    base_url: str | None
    temperature: float


def llm_settings() -> LLMSettings:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if not api_key or not model:
        raise RuntimeError(
            "Configure LLM_API_KEY e LLM_MODEL no arquivo .env (veja .env.example)."
        )
    return LLMSettings(
        api_key=api_key,
        model=model,
        base_url=os.getenv("LLM_BASE_URL", "").strip() or None,
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
    )


def now() -> datetime:
    """Data/hora "atual" no fuso da loja.

    Pode ser fixada via EMPORIO_AGORA (ISO 8601, ex.: 2026-03-20T10:30) para
    tornar testes e exemplos reprodutíveis — os dados de pedidos vão até
    março/2026, então prazos (devolução, garantia, atraso) dependem dessa data.
    """
    fixed = os.getenv("EMPORIO_AGORA", "").strip()
    if fixed:
        dt = datetime.fromisoformat(fixed)
        return dt if dt.tzinfo else dt.replace(tzinfo=STORE_TZ)
    return datetime.now(STORE_TZ)
