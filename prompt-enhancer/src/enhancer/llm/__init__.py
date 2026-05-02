"""LLM provider abstraction.

The ``ChatProvider`` ABC in ``base`` defines the contract every backend
satisfies. v1 ships LM Studio fully and stubs Ollama / OpenAI / Anthropic
behind clear ``NotImplementedError`` (with install hints).
"""
