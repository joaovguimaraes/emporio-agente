"""Persistência do histórico de conversa em JSON (uma conversa por arquivo).

Num canal tipo WhatsApp o cliente volta horas depois ("e o meu pedido?"), então
manter o histórico por sessão melhora a experiência. JSON local basta para o
protótipo; em produção seria Redis/Postgres indexado pelo telefone do cliente.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import SESSIONS_DIR

# Janela de contexto: últimas N mensagens. O corte sempre começa numa mensagem
# de usuário para não separar uma chamada de ferramenta do seu resultado.
MAX_MESSAGES = 40


class Conversation:
    def __init__(self, session_id: str | None = None, directory: Path = SESSIONS_DIR):
        self.session_id = session_id
        self.messages: list[dict] = []
        self.path: Path | None = None
        if session_id:
            safe = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)[:64]
            self.path = directory / f"{safe}.json"
            if self.path.exists():
                self.messages = json.loads(self.path.read_text(encoding="utf-8"))

    def append(self, message: dict) -> None:
        self.messages.append(message)

    def window(self) -> list[dict]:
        if len(self.messages) <= MAX_MESSAGES:
            return list(self.messages)
        tail = self.messages[-MAX_MESSAGES:]
        for i, m in enumerate(tail):
            if m["role"] == "user":
                return tail[i:]
        return tail

    def rollback_to_last_user(self) -> None:
        """Em caso de erro no meio de um turno, descarta o turno incompleto."""
        while self.messages and self.messages[-1]["role"] != "user":
            self.messages.pop()
        if self.messages:
            self.messages.pop()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.messages, ensure_ascii=False, indent=1), encoding="utf-8")

    def reset(self) -> None:
        self.messages = []
        if self.path and self.path.exists():
            self.path.unlink()
