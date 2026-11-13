"""Interface de linha de comando (chat no terminal)."""

from __future__ import annotations

import argparse
import json
import sys

from .agent import Agent, ToolTrace
from .history import Conversation
from .prompts import PERSONA_NAME

DIM, BOLD, CYAN, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[0m"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Agente de atendimento da Empório da Música")
    parser.add_argument("--sessao", default="terminal", help="id da conversa (o histórico é retomado)")
    parser.add_argument("--nova", action="store_true", help="apaga o histórico da sessão antes de começar")
    parser.add_argument("--debug", action="store_true", help="mostra as ferramentas chamadas")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # Windows: console em cp1252 quebraria acentos

    conversation = Conversation(args.sessao)
    if args.nova:
        conversation.reset()
    agent = Agent(conversation)

    def show_tool(t: ToolTrace) -> None:
        if args.debug:
            preview = json.dumps(t.result, ensure_ascii=False)[:300]
            print(f"{DIM}  ↳ {t.name}({json.dumps(t.arguments, ensure_ascii=False)}) → {preview}{RESET}")

    print(f"{BOLD}Empório da Música{RESET} — atendimento virtual ({PERSONA_NAME}). Digite /sair para encerrar, /nova para recomeçar.")
    if conversation.messages:
        print(f"{DIM}(retomando conversa '{args.sessao}' com {len(conversation.messages)} mensagens){RESET}")
    while True:
        try:
            text = input(f"\n{BOLD}Você:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("/sair", "sair", "exit", "quit"):
            break
        if text.lower() == "/nova":
            conversation.reset()
            print(f"{DIM}(conversa reiniciada){RESET}")
            continue
        turn = agent.respond(text, on_tool=show_tool)
        print(f"\n{CYAN}{BOLD}{PERSONA_NAME}:{RESET} {turn.reply}")


if __name__ == "__main__":
    main()
