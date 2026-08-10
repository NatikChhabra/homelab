"""Pre-normalization for fused typos."""
import re

def _pre_normalize(text: str) -> str:
    """Insert spaces around numbers and common fused word boundaries.

    Handles typos like "to60", "speed3", "ac22", "turnon", "onthe".
    Safe because it only touches obvious fusion points, never invents words.
    """
    # Number fused to preceding word: "to60" -> "to 60", "speed3" -> "speed 3"
    text = re.sub(r"([a-zA-Z])(\d+)", r"\1 \2", text)
    # Number fused to following word: "60percent" -> "60 percent"
    text = re.sub(r"(\d+)([a-zA-Z])", r"\1 \2", text)
    # Common fused pairs: "turnon" -> "turn on", "onthe" -> "on the"
    text = re.sub(r"\b(turn)(on)\b", r"\1 \2", text)
    text = re.sub(r"\b(on)(the)\b", r"\1 \2", text)
    text = re.sub(r"\b(turn)(off)\b", r"\1 \2", text)
    text = re.sub(r"\b(ac)(\d)\b", r"\1 \2", text)
    return text


def normalise(text: str) -> tuple[str, str]:
    """Return (rewritten_text, rule_reason). reason is "" when nothing matched."""
    if text.strip().endswith("?"):
        return text, ""
    cleaned = _pre_normalize(text.strip().rstrip(".!?"))
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return text, ""
    for pattern, repl, why in _RULES:
        m = pattern.match(cleaned)
        if m:
            out = repl(m) if callable(repl) else repl
            if out.lower() == cleaned.lower():
                return text, ""
            return out, why
    return text, ""
