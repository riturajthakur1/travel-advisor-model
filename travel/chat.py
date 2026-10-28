"""Console chat with the travel model behind the guardrail harness, using the mock travel backend.

usage: uv run python -I -m travel.chat [--server http://127.0.0.1:8091] [--today 2026-10-10]
Start the model first: bash scripts/serve.sh <model.gguf> 8091 cpu
Type 'quit' to leave. When a booking needs confirmation you are asked y/N, which stands in for a Confirm button.
"""
import argparse
import datetime as dt
import pathlib
import sys

from travel.backend import Backend
from travel.harness import Harness, OpenAIClient
from travel.simulate import deployment_context

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8091")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--live", action="store_true", help="Enable live flight and hotel pricing (zero API cost)")
    args = ap.parse_args()

    prompt = (ROOT / "prompts" / "runtime_system_prompt.txt").read_text(encoding="utf-8").strip() + deployment_context(args.today)
    cfg = {"now": f"{args.today}T11:00+05:30"}
    if args.live:
        cfg["live_pricing"] = True
    backend = Backend(cfg, seed=1)
    mode_text = "LIVE Google Flights & Hotel pricing" if args.live else "mock backend"
    bot = Harness(llm=OpenAIClient(args.server), backend=backend, system_prompt=prompt, today=args.today)
    print(f"Travel advisor (today {args.today}, {mode_text}). Type 'quit' to exit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("quit", "exit"):
            break
        if not text:
            continue
        reply = bot.send(text)
        print(f"\nadvisor> {reply.text}\n")
        if reply.kind == "confirm_booking":
            answer = input("Confirm this booking? [y/N] ").strip().lower()
            reply = bot.confirm_booking() if answer == "y" else bot.cancel_booking()
            print(f"\nadvisor> {reply.text}\n")


if __name__ == "__main__":
    main()
