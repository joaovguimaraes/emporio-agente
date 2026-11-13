"""Testa o loop de function calling com clientes LLM falsos (sem rede), nos dois backends."""

import json
from types import SimpleNamespace as NS

import pytest

from emporio.agent import Agent
from emporio.config import LLMSettings
from emporio.history import Conversation


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setenv("EMPORIO_AGORA", "2026-03-24T10:30")


def _settings(provider):
    return LLMSettings("x", "fake-model", None, 0, provider=provider)


# ----------------------------------------------------------------- OpenAI-compatível (Gemini etc.)

class FakeOpenAI:
    """1ª chamada pede uma ferramenta; a 2ª responde com o preço que a ferramenta devolveu."""

    def __init__(self):
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            args = json.dumps({"consulta": "Takamine GD20"})
            tc = NS(id="call_1", type="function", function=NS(name="buscar_produtos", arguments=args))
            raw_tc = {
                "id": "call_1", "type": "function",
                "function": {"name": "buscar_produtos", "arguments": args},
                "extra_content": {"google": {"thought_signature": "sig"}},
            }
            msg = NS(content=None, tool_calls=[tc], model_dump=lambda **_: {"role": "assistant", "tool_calls": [raw_tc]})
        else:
            # O campo extra do provedor (thought_signature do Gemini 3) precisa voltar intacto.
            assistant = next(m for m in kwargs["messages"] if m["role"] == "assistant")
            assert assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig"
            tool_msg = next(m for m in kwargs["messages"] if m["role"] == "tool")
            price = json.loads(tool_msg["content"])["resultados"][0]["preco_tabela"]
            msg = NS(content=f"Custa {price}.", tool_calls=None)
        return NS(choices=[NS(message=msg)])


def test_openai_compat_tool_loop():
    fake = FakeOpenAI()
    agent = Agent(Conversation(), client=fake, settings=_settings("openai"))
    turn = agent.respond("Quanto custa o Takamine GD20?")
    assert turn.reply == "Custa R$ 2.199,00."
    assert [t.name for t in turn.tool_calls] == ["buscar_produtos"]
    assert fake.calls[0]["messages"][0]["role"] == "system"
    assert [m["role"] for m in agent.conversation.messages] == ["user", "assistant", "tool", "assistant"]


# ----------------------------------------------------------------- Anthropic

class Block(NS):
    def model_dump(self, **_):
        return {k: v for k, v in vars(self).items()}


class FakeAnthropic:
    def __init__(self):
        self.calls = []
        self.messages = NS(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            content = [
                Block(type="thinking", thinking="", signature="sig"),
                Block(type="tool_use", id="toolu_1", name="buscar_produtos", input={"consulta": "Takamine GD20"}),
            ]
            return NS(content=content, stop_reason="tool_use", stop_details=None)
        msgs = kwargs["messages"]
        # Bloco de thinking devolvido sem alteração; resultados numa única mensagem de usuário.
        assert msgs[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
        tool_result = msgs[2]["content"][0]
        assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
        price = json.loads(tool_result["content"])["resultados"][0]["preco_tabela"]
        return NS(content=[Block(type="text", text=f"Custa {price}.")], stop_reason="end_turn", stop_details=None)


def test_anthropic_tool_loop():
    fake = FakeAnthropic()
    agent = Agent(Conversation(), client=fake, settings=_settings("anthropic"))
    turn = agent.respond("Quanto custa o Takamine GD20?")
    assert turn.reply == "Custa R$ 2.199,00."
    first = fake.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}  # parte estável em cache
    assert "Agora:" in first["system"][1]["text"]  # parte dinâmica fora do cache
    assert "temperature" not in first
    assert {t["name"] for t in first["tools"]} >= {"buscar_produtos", "consultar_politicas"}


def test_anthropic_refusal_is_handled():
    fake = NS(messages=NS(create=lambda **_: NS(content=[], stop_reason="refusal", stop_details=None)))
    agent = Agent(Conversation(), client=fake, settings=_settings("anthropic"))
    assert "não consigo ajudar" in agent.respond("...").reply


# ----------------------------------------------------------------- falhas e histórico

def test_llm_failure_rolls_back_turn():
    def boom(**_):
        raise TimeoutError()

    agent = Agent(Conversation(), client=NS(messages=NS(create=boom)), settings=_settings("anthropic"))
    turn = agent.respond("oi")
    assert "probleminha técnico" in turn.reply
    assert agent.conversation.messages == []


def test_window_never_starts_with_tool_result():
    conv = Conversation()
    for i in range(30):
        conv.append({"role": "user", "content": f"pergunta {i}"})
        conv.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}"}]})
        conv.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}"}]})
        conv.append({"role": "assistant", "content": "ok"})
    first = conv.window()[0]
    assert first["role"] == "user" and isinstance(first["content"], str)
