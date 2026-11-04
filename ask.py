"""Command-line use without any server. Each prompt starts a new Copilot chat.

  python ask.py "What is a flaky test?"
  type log.txt | python ask.py --prefix "Summarize these failures:"
  python ask.py -i                 # interactive, browser stays open
  python ask.py --file prompts.txt # one prompt per line
  python ask.py --stream "..."     # print the reply as Copilot writes it
  python ask.py --models           # list the models in Copilot's model picker
  python ask.py --model "Think deeper" "..."

Stop server.py / manager.py first: they use the same browser profile.
"""
import argparse
import logging
import sys

import config
import procs
from copilot import CopilotSession
from models import model_id, resolve


def run(session, prompt, stream, model):
    if not stream:
        fut = session.submit(prompt, model=model)
        print(fut.result(timeout=session.result_timeout()))
    else:
        fut = session.submit(prompt, on_delta=lambda d: print(d, end="", flush=True), model=model)
        final = fut.result(timeout=session.result_timeout())
        if not fut.streamed_text:
            print(final, end="")
        print()
    if fut.truncated:
        print("\n[note: Copilot was still writing when COPILOT_REPLY_TIMEOUT ran out; the reply may be incomplete]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="*")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("-i", "--interactive", action="store_true")
    ap.add_argument("--file", help="text file with one prompt per line")
    ap.add_argument("--prefix", default="", help="text prepended to the prompt (useful with stdin)")
    ap.add_argument("--plain", action="store_true", help="plain text instead of markdown")
    ap.add_argument("--stream", action="store_true", help="print the reply as it is written")
    ap.add_argument("--models", action="store_true", help="list the models in Copilot's model picker")
    ap.add_argument("--model", help="model name or id to select first (default: keep the current selection)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if config.KILL_ON_EXIT:
        procs.kill_children_on_exit()

    with CopilotSession(show_window=args.show, markdown=not args.plain,
                        discover_models=bool(args.models or args.model)) as s:
        names = s.model_names(wait=60) if (args.models or args.model) else []
        if args.models:
            print(f"Selected: {s.current_model}")
            keys = s.model_keys()
            for n in names:
                print(f"  {model_id(n):48} {n}" + (f"   [{keys[n]}]" if n in keys else ""))
            return
        model = None
        if args.model:
            model, known = resolve(args.model, names, s.model_keys())
            if not known:
                ap.error(f"unknown model {args.model!r}; run --models to list them")
        if args.interactive:
            print("Interactive mode - empty line or Ctrl+C to quit.")
            try:
                while (p := input("\n> ").strip()):
                    print()
                    run(s, p, args.stream, model)
            except (KeyboardInterrupt, EOFError):
                pass
        elif args.file:
            with open(args.file, encoding="utf-8") as f:
                for line in filter(None, (l.strip() for l in f)):
                    print(f"### {line}\n")
                    run(s, line, args.stream, model)
                    print()
        else:
            prompt = " ".join(args.prompt) or (sys.stdin.read() if not sys.stdin.isatty() else "")
            if not prompt.strip():
                ap.error("no prompt given")
            run(s, f"{args.prefix}\n\n{prompt}".strip(), args.stream, model)


if __name__ == "__main__":
    main()
