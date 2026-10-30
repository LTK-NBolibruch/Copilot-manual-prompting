"""Command-line use without any server. Each prompt starts a new Copilot chat.

  python ask.py "What is a flaky test?"
  type log.txt | python ask.py --prefix "Summarize these failures:"
  python ask.py -i                 # interactive, browser stays open
  python ask.py --file prompts.txt # one prompt per line
  python ask.py --stream "..."     # print the reply as Copilot writes it
"""
import argparse
import logging
import sys

from copilot import CopilotSession


def run(session, prompt, stream):
    if not stream:
        fut = session.submit(prompt)
        print(fut.result(timeout=session.result_timeout()))
    else:
        fut = session.submit(prompt, on_delta=lambda d: print(d, end="", flush=True))
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
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    with CopilotSession(show_window=args.show, markdown=not args.plain) as s:
        if args.interactive:
            print("Interactive mode - empty line or Ctrl+C to quit.")
            try:
                while (p := input("\n> ").strip()):
                    print()
                    run(s, p, args.stream)
            except (KeyboardInterrupt, EOFError):
                pass
        elif args.file:
            with open(args.file, encoding="utf-8") as f:
                for line in filter(None, (l.strip() for l in f)):
                    print(f"### {line}\n")
                    run(s, line, args.stream)
                    print()
        else:
            prompt = " ".join(args.prompt) or (sys.stdin.read() if not sys.stdin.isatty() else "")
            if not prompt.strip():
                ap.error("no prompt given")
            run(s, f"{args.prefix}\n\n{prompt}".strip(), args.stream)


if __name__ == "__main__":
    main()
