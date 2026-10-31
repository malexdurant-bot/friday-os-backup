"""Conservative local-run accounting; these are not provider billing guarantees."""

# OpenRouter public catalog checked 2026-10-30. Restrict the model until another
# model's compatibility and price are deliberately verified.
MODEL_PRICES_PER_MILLION = {
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "google/gemma-4-31b-it:free": (0.0, 0.0),
    "liquid/lfm-2.5-2.6b:free": (0.0, 0.0),
}


def estimated_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in MODEL_PRICES_PER_MILLION:
        raise ValueError("Friday model has no verified pricing")
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("Invalid token usage")
    prompt, completion = MODEL_PRICES_PER_MILLION[model]
    return (input_tokens * prompt + output_tokens * completion) / 1_000_000
