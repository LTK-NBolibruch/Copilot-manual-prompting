"""Example local client using the official OpenAI SDK (pip install openai)."""
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:5000/v1", api_key="local")  # key is ignored

resp = client.chat.completions.create(
    model="m365-copilot",
    messages=[
        {"role": "system", "content": "Answer in at most three bullet points."},
        {"role": "user", "content": "What are three fun facts about the ocean?"},
    ],
)
print(resp.choices[0].message.content)
if resp.choices[0].finish_reason == "length":
    print("[reply was cut off by the server's time limit]")

# Live streaming: markdown arrives one finished block at a time
for chunk in client.chat.completions.create(
        model="m365-copilot", stream=True,
        messages=[{"role": "user", "content": "Write a short paragraph about trains."}]):
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
print()
