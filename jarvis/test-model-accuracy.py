"""Test model accuracy on router test cases."""
import asyncio
import aiohttp
import json
import time

# Test cases that should be handled locally (not fall to LLM)
LOCAL_TESTS = [
    ("turn on the lights", "local"),
    ("lights off", "local"),
    ("turn on the fan", "local"),
    ("fan off", "local"),
    ("fan speed 3", "local"),
    ("set ac to 22", "local"),
    ("ac 24", "local"),
    ("good night", "local"),
    ("hey jarvis", "greeting"),
    ("hello jarvis", "greeting"),
    ("can you hear me", "greeting"),
]

# Test cases that need LLM
LLM_TESTS = [
    ("what's the weather", "llm"),
    ("search for news", "llm"),
    ("who is calling", "llm"),
]

async def test_model(host="localhost", port=11434, model="qwen2.5:3b"):
    """Test model responses."""
    url = f"http://{host}:{port}/api/chat"
    
    results = []
    
    async with aiohttp.ClientSession() as session:
        for text, expected in LOCAL_TESTS[:5]:  # Test first 5
            start = time.time()
            try:
                async with session.post(url, json={
                    "model": model,
                    "messages": [{"role": "user", "content": text}],
                    "stream": False
                }, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    result = await resp.json()
                    elapsed = time.time() - start
                    response = result.get("message", {}).get("content", "")
                    results.append((text, elapsed, len(response)))
                    print(f"{text:25s} -> {elapsed:.2f}s, {len(response)} chars")
            except Exception as e:
                print(f"{text:25s} -> ERROR: {e}")
    
    return results

if __name__ == "__main__":
    print("Testing qwen2.5:3b...")
    asyncio.run(test_model(model="qwen2.5:3b"))
