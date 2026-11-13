"""Loop do agente: function calling sobre uma API compatível com OpenAI.

Por que um loop próprio e não LangChain/LangGraph? O fluxo é um único agente
com 8 ferramentas; ~100 linhas explícitas deixam o comportamento (e as
falhas) fáceis de auditar, sem camadas de abstração extras.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from openai import OpenAI

from . import config
from .history import Conversation
from .prompts import build_system_prompt
from .tools import TOOL_SCHEMAS, run_tool

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
FALLBACK_MESSAGE = (
    "Opa, tive um probleminha técnico aqui e não consegui concluir sua solicitação. "
    "Você pode tentar de novo em instantes ou falar com a equipe pelo (67) 3341-4444."
)


@dataclass
class ToolTrace:
    name: str
    arguments: dict
    result: dict


@dataclass
class Turn:
    reply: str
    tool_calls: list[ToolTrace] = field(default_factory=list)


class Agent:
    def __init__(self, conversation: Conversation, client: OpenAI | None = None, settings: config.LLMSettings | None = None):
        self.settings = settings or config.llm_settings()
        # Retries com backoff cobrem 429/503 ("high demand") que aparecem na camada gratuita.
        self.client = client or OpenAI(
            api_key=self.settings.api_key, base_url=self.settings.base_url, timeout=60, max_retries=4
        )
        self.conversation = conversation

    def _complete(self, messages: list[dict]):
        return self.client.chat.completions.create(
            model=self.settings.model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=self.settings.temperature,
        )

    def respond(self, user_message: str, on_tool: Callable[[ToolTrace], None] | None = None) -> Turn:
        self.conversation.append({"role": "user", "content": user_message})
        traces: list[ToolTrace] = []

        for _ in range(MAX_TOOL_ROUNDS):
            # O prompt de sistema é reconstruído a cada chamada (data/hora e status da loja mudam).
            messages = [{"role": "system", "content": build_system_prompt()}, *self.conversation.window()]
            try:
                response = self._complete(messages)
            except Exception as exc:  # noqa: BLE001
                log.exception("falha na chamada ao LLM")
                self.conversation.rollback_to_last_user()
                return Turn(FALLBACK_MESSAGE, traces)  # detalhe técnico vai para o log, não para o cliente

            msg = response.choices[0].message
            if not msg.tool_calls:
                reply = (msg.content or "").strip() or FALLBACK_MESSAGE
                self.conversation.append({"role": "assistant", "content": reply})
                self.conversation.save()
                return Turn(reply, traces)

            # Guarda a mensagem como veio do provedor: o Gemini 3 anexa uma `thought_signature`
            # em `extra_content` de cada tool_call e rejeita (HTTP 400) o turno seguinte se ela
            # não for devolvida. Reconstruir só com id/name/arguments perde esse campo.
            raw = msg.model_dump(exclude_none=True)
            assistant_msg = {"role": "assistant", "content": raw.get("content") or "", "tool_calls": raw["tool_calls"]}
            self.conversation.append(assistant_msg)
            for tc in msg.tool_calls:
                result = run_tool(tc.function.name, tc.function.arguments)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": tc.function.arguments}
                trace = ToolTrace(tc.function.name, args, result)
                traces.append(trace)
                if on_tool:
                    on_tool(trace)
                self.conversation.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False, default=str)}
                )

        # Estourou o limite de rodadas: evita loop infinito de ferramentas.
        reply = "Desculpa, me enrolei aqui para buscar essa informação. Vou passar seu caso para a equipe, tudo bem?"
        self.conversation.append({"role": "assistant", "content": reply})
        self.conversation.save()
        return Turn(reply, traces)
