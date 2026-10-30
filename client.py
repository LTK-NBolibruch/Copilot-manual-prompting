"""Example local client using the official OpenAI SDK (pip install openai).

Lists the models in Copilot's model picker, then holds a two-turn
conversation. The second request sends the first reply back in `messages`,
so the server continues the same Copilot chat instead of starting a new one.
"""
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:5000/v1", api_key="local")  # key is ignored

for m in client.models.list():
    print(m.id, "-", getattr(m, "name", ""))
model = "m365-copilot"   # current selection; or e.g. "m365-copilot-auto"

messages = [
    {"role": "system", "content": "Answer in at most three bullet points."},
    {"role": "user", "content": "What are three fun facts about the ocean?"},
]
resp = client.chat.completions.create(model=model, messages=messages)
reply = resp.choices[0].message.content
print(reply)
if resp.choices[0].finish_reason == "length":
    print("[reply was cut off by the server's time limit]")

# Follow-up in the same Copilot chat (streamed)
messages += [{"role": "assistant", "content": reply},
             {"role": "user", "content": "Expand on the second one."}]
for chunk in client.chat.completions.create(model=model, stream=True, messages=messages):
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
print()
