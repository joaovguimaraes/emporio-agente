"""Loop do agente com function calling.

Por que um loop próprio e não LangChain/LangGraph? O fluxo é um único agente
com 8 ferramentas; ~150 linhas explícitas deixam o comportamento (e as
falhas) fáceis de auditar, sem camadas de abstração extras.

Dois backends compartilham o mesmo loop:
- `AnthropicBackend`: SDK oficial da Anthropic (padrão, Claude Haiku 5.5);
- `OpenAICompatBackend`: qualquer API compatível com OpenAI (Gemini, OpenAI,
  Groq, Ollama) — alternativa gratuita para quem for rodar o projeto.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config
from .history import Conversation
from .prompts import build_system_parts
from .tools import TOOL_SCHEMAS, run_tool

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
FALLBACK_MESSAGE = (
    "Opa, tive um probleminha técnico aqui e não consegui concluir sua solicitação. "
    "Você pode tentar de novo em instantes ou falar com a equipe pelo (67) 3341-4444."
)
GIVE_UP_MESSAGE = "Desculpa, me enrolei aqui para buscar essa informação. Vou passar seu caso para a equipe, tudo bem?"


@dataclass
class ToolTrace:
    name: str
    arguments: dict
    result: dict


@dataclass
class Turn:
    reply: str
    tool_calls: list[ToolTrace] = field(default_factory=list)


@dataclass
class Step:
    """Resultado normalizado de uma chamada ao modelo, independente do provedor."""

    text: str
    tool_calls: list[tuple[str, str, dict]]  # (id, nome, argumentos)
    assistant_message: dict  # mensagem a guardar no histórico, no formato nativo do provedor


# --------------------------------------------------------------------------- backends

class AnthropicBackend:
    def __init__(self, settings: config.LLMSettings, client: Any = None):
        import anthropic

        self.settings = settings
        # max_retries cobre 429/5xx com backoff exponencial.
        # Chaves de organização sem workspace exigem o header anthropic-workspace-id.
        workspace = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        self.client = client or anthropic.Anthropic(
            api_key=settings.api_key, timeout=60, max_retries=4, default_headers=headers
        )
        self.tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in TOOL_SCHEMAS
        ]

    def step(self, system_static: str, system_dynamic: str, messages: list[dict]) -> Step:
        response = self.client.messages.create(
            model=self.settings.model,
            max_tokens=16000,
            # A parte estável do prompt (persona, regras, índice do manual) fica em cache;
            # data/hora vai num bloco separado depois do breakpoint para não invalidar o cache.
            system=[
                {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system_dynamic},
            ],
            tools=self.tools,
            messages=messages,
            output_config={"effort": self.settings.effort},
        )
        if response.stop_reason == "refusal":
            log.warning("recusa do modelo: %s", response.stop_details)
            text = "Desculpa, não consigo ajudar com isso. Posso te ajudar com algo sobre instrumentos ou seu pedido?"
            return Step(text, [], {"role": "assistant", "content": text})
        # Devolve o conteúdo inteiro (inclusive blocos de thinking) no próximo turno, sem alterar.
        blocks = [b.model_dump(exclude_none=True) for b in response.content]
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        calls = [(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "max_tokens" and not calls:
            text = text or FALLBACK_MESSAGE
        return Step(text, calls, {"role": "assistant", "content": blocks})

    @staticmethod
    def tool_result_messages(results: list[tuple[str, str]]) -> list[dict]:
        # Todos os resultados de um turno vão numa única mensagem de usuário.
        return [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tid, "content": content} for tid, content in results],
            }
        ]


class OpenAICompatBackend:
    def __init__(self, settings: config.LLMSettings, client: Any = None):
        from openai import OpenAI

        self.settings = settings
        self.client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=60, max_retries=4)

    def step(self, system_static: str, system_dynamic: str, messages: list[dict]) -> Step:
        response = self.client.chat.completions.create(
            model=self.settings.model,
            messages=[{"role": "system", "content": f"{system_static}\n{system_dynamic}"}, *messages],
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=self.settings.temperature,
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            text = (msg.content or "").strip()
            return Step(text, [], {"role": "assistant", "content": text})
        # Guarda a mensagem como veio do provedor: o Gemini 3 anexa uma `thought_signature`
        # em `extra_content` de cada tool_call e rejeita (HTTP 400) o turno seguinte se ela
        # não for devolvida. Reconstruir só com id/name/arguments perde esse campo.
        raw = msg.model_dump(exclude_none=True)
        calls = []
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            calls.append((tc.id, tc.function.name, args))
        return Step(
            (msg.content or "").strip(),
            calls,
            {"role": "assistant", "content": raw.get("content") or "", "tool_calls": raw["tool_calls"]},
        )

    @staticmethod
    def tool_result_messages(results: list[tuple[str, str]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": tid, "content": content} for tid, content in results]


# --------------------------------------------------------------------------- agente

class Agent:
    def __init__(self, conversation: Conversation, client: Any = None, settings: config.LLMSettings | None = None):
        self.settings = settings or config.llm_settings()
        backend_cls = AnthropicBackend if self.settings.provider == "anthropic" else OpenAICompatBackend
        self.backend = backend_cls(self.settings, client)
        self.conversation = conversation

    def respond(self, user_message: str, on_tool: Callable[[ToolTrace], None] | None = None) -> Turn:
        self.conversation.append({"role": "user", "content": user_message})
        traces: list[ToolTrace] = []

        for _ in range(MAX_TOOL_ROUNDS):
            # Prompt reconstruído a cada chamada: data/hora e status da loja mudam.
            static, dynamic = build_system_parts()
            try:
                step = self.backend.step(static, dynamic, self.conversation.window())
            except Exception:  # noqa: BLE001
                log.exception("falha na chamada ao LLM")
                self.conversation.rollback_to_last_user()
                return Turn(FALLBACK_MESSAGE, traces)  # detalhe técnico vai para o log, não para o cliente

            self.conversation.append(step.assistant_message)
            if not step.tool_calls:
                reply = step.text or FALLBACK_MESSAGE
                self.conversation.save()
                return Turn(reply, traces)

            results = []
            for call_id, name, args in step.tool_calls:
                result = run_tool(name, args)
                trace = ToolTrace(name, args, result)
                traces.append(trace)
                if on_tool:
                    on_tool(trace)
                results.append((call_id, json.dumps(result, ensure_ascii=False, default=str)))
            for m in self.backend.tool_result_messages(results):
                self.conversation.append(m)

        # Estourou o limite de rodadas: evita loop infinito de ferramentas.
        self.conversation.append({"role": "assistant", "content": GIVE_UP_MESSAGE})
        self.conversation.save()
        return Turn(GIVE_UP_MESSAGE, traces)
