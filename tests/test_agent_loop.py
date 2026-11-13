"""Testa o loop de function calling com um cliente LLM falso (sem rede)."""

import json
from types import SimpleNamespace as NS

from emporio.agent import Agent
from emporio.config import LLMSettings
from emporio.history import Conversation


def _tool_call(name, args, id_="call_1"):
    return NS(id=id_, type="function", function=NS(name=name, arguments=json.dumps(args)))


class FakeClient:
    """Primeira chamada pede uma ferramenta; a segunda devolve texto usando o resultado."""

    def __init__(self):
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            msg = NS(content=None, tool_calls=[_tool_call("buscar_produtos", {"consulta": "Takamine GD20"})])
        else:
            tool_msg = next(m for m in kwargs["messages"] if m["role"] == "tool")
            price = json.loads(tool_msg["content"])["resultados"][0]["preco_tabela"]
            msg = NS(content=f"Custa {price}.", tool_calls=None)
        return NS(choices=[NS(message=msg)])


def test_tool_loop_roundtrip(monkeypatch):
    monkeypatch.setenv("EMPORIO_AGORA", "2026-03-24T10:30")
    fake = FakeClient()
    agent = Agent(Conversation(), client=fake, settings=LLMSettings("x", "fake-model", None, 0))
    turn = agent.respond("Quanto custa o Takamine GD20?")
    assert turn.reply == "Custa R$ 2.199,00."
    assert [t.name for t in turn.tool_calls] == ["buscar_produtos"]
    assert fake.calls[0]["messages"][0]["role"] == "system"
    roles = [m["role"] for m in agent.conversation.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_llm_failure_rolls_back_turn():
    class Boom:
        chat = NS(completions=NS(create=lambda **_: (_ for _ in ()).throw(TimeoutError())))

    agent = Agent(Conversation(), client=Boom(), settings=LLMSettings("x", "m", None, 0))
    turn = agent.respond("oi")
    assert "problema" in turn.reply
    assert agent.conversation.messages == []
