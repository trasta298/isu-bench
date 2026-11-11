# List prices in USD per million tokens, checked on 2026-11-03 (Anthropic's on 2026-11-10): Anthropic's model table, OpenAI's API pricing page
# (standard tier; no request in these trials passed 272K input tokens) and DeepSeek's pricing page (deepseek-flash is
# V4.1-Flash-1017, at the peak rate; off-peak is half). Google's is its introductory price of 2026-11-11, which doubles on 2026-12-01.
PRICES = {
    # Claude Code writes its prompt cache with the 1-hour TTL, billed at twice the input price.
    'claude-opus-5-5': {'input': 4.00, 'output': 20.00, 'cache_read': 0.20, 'cache_write': 8.00},
    'claude-sonnet-5-5': {'input': 2.00, 'output': 10.00, 'cache_read': 0.10, 'cache_write': 4.00},
    'claude-fable-5-2': {'input': 8.00, 'output': 40.00, 'cache_read': 0.20, 'cache_write': 16.00},
    'gpt-6-astra': {'input': 10.00, 'output': 50.00, 'cache_read': 1.00, 'cache_write': 12.50},
    'gpt-6.1-sol': {'input': 2.00, 'output': 10.00, 'cache_read': 0.10, 'cache_write': 2.50},
    'deepseek-v4.1-flash:1017': {'input': 0.30, 'output': 1.20, 'cache_read': 0.006, 'cache_write': 0.30},
    'gemini-4-carbon': {'input': 2.00, 'output': 10.00, 'cache_read': 0.10},
}


# Codex's output tokens already include its reasoning tokens, so reasoning is not billed again.
def cost(model, usage):
    p = PRICES.get(model)
    if not p or not usage:
        return None
    return sum(usage.get(f'{k}_tokens', 0) * p[k] for k in p) / 1e6
