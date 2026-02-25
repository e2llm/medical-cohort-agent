# On-Prem LLM Deployment for Elastic Agent Builder

## How to run it on-prem

Agent Builder requires an LLM that supports **OpenAI-compatible tool calling** — the model must accept a `tools` array in the request and return structured `tool_calls` in the response. This is the standard OpenAI function calling protocol.

### Recommended models (Ollama)

| Model | VRAM (Q4) | Tool quality | Notes |
|-------|-----------|-------------|-------|
| **Qwen 3 30B** | ~20GB | Excellent | Best balance of size, quality, and tool reliability |
| **GPT-OSS 20B** | ~12GB | Good | [Validated with Agent Builder by Elastic](https://www.elastic.co/search-labs/blog/build-an-ai-agent-hr-elastic-agent-builder-gpt-oss) |
| **Qwen 2.5 32B** | ~20GB | Very good | Proven, mature |
| **Qwen 3 8B** | ~6GB | Good | Fits on consumer GPU |
| **Mistral Nemo 12B** | ~8GB | Decent | Lightest viable option |
| **Llama 3.1/3.3 70B** | ~40GB | Good | Needs 2x GPU |
| **Kimi K2** | ~500GB+ | Best agentic | Multi-GPU; strongest tool orchestration if hardware allows |

### Connector setup

1. Kibana → Stack Management → Connectors → Create → OpenAI
2. Provider: **"Other (OpenAI Compatible Service)"**
3. URL: `http://ollama:11434/v1/chat/completions` (or your host URL)
4. Default model: `qwen3:30b` (or your chosen model)
5. API key: any non-empty string (Ollama ignores it)
6. **"Enable native function calling": ON** (required)

### Alternative: vLLM

vLLM offers finer control over tool calling via explicit parser and template flags. This enables models that Ollama doesn't support for tools (including Llama 4), but requires more ops overhead.

| Model | `--tool-call-parser` |
|-------|---------------------|
| Qwen 2.5/3 | `qwen3_xml` |
| Llama 3.1/3.3 | `llama3_json` |
| Llama 4 | `llama4_pythonic` |
| Kimi K2 | `kimi_k2` |
| DeepSeek V3/R1 | `deepseek_v3` |
| Mistral | `mistral` |

Example (Llama 4 Maverick via vLLM — requires 8x H100 80GB):

```bash
vllm serve meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8 \
  --enable-auto-tool-choice \
  --tool-call-parser llama4_pythonic \
  --chat-template examples/tool_chat_template_llama4_pythonic.jinja \
  --tensor-parallel-size 8
```

## Model selection: what we learned

### The requirement

Agent Builder sends tool definitions using the OpenAI `tools` parameter and expects the model to respond with structured `tool_calls`. This is controlled by the **"Enable native function calling"** toggle on the connector — Agent Builder requires it to be ON.

There is a "simulated" fallback in Kibana (system-prompt injection with text markers), but it only works for the Observability AI Assistant, not Agent Builder.

### Not all "OpenAI-compatible" models support tools

We initially designed around **Ollama + Llama 4 Maverick**. The reasoning was straightforward: Llama 4 is Meta's latest, Ollama makes it easy to serve, and Elastic's OpenAI-compatible connector should handle the rest.

In practice, the model responded with generic chat answers and completely ignored all registered tools. The fix: serve Llama 4 via vLLM instead (see [Alternative: vLLM](#alternative-vllm) above), or choose a model with native Ollama tool support. The root cause:

**Ollama decides tool calling support per model via baked-in chat templates.** Models with a validated template get a "tools" tag in the [Ollama library](https://ollama.com/search?c=tools); models without one silently drop the `tools` parameter from API requests. The model never sees the tools.

Llama 4 (Scout and Maverick) has **no "tools" tag** in Ollama. Neither does DeepSeek R1. There is no user-configurable workaround — Ollama doesn't expose custom chat template options.

### How to verify a model before deploying

1. Check for the **"tools" tag** on the model's [Ollama library page](https://ollama.com/search?c=tools)
2. If using vLLM, confirm a matching `--tool-call-parser` exists in the [vLLM tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling/)
3. Test directly against the model endpoint before connecting to Agent Builder:

```bash
curl http://YOUR_LLM:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:30b",
    "messages": [{"role": "user", "content": "List all medical indices"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "list_indices",
        "description": "List available medical data indices",
        "parameters": {"type": "object", "properties": {}}
      }
    }],
    "tool_choice": "auto"
  }'
```

If the response contains `"tool_calls"` — the model is ready. If it returns plain text — it won't work with Agent Builder.

## References

- [Ollama: Models with tool support](https://ollama.com/search?c=tools)
- [Ollama: Tool Calling docs](https://docs.ollama.com/capabilities/tool-calling)
- [vLLM: Tool Calling](https://docs.vllm.ai/en/latest/features/tool_calling/)
- [Elastic: Model configuration in Agent Builder](https://www.elastic.co/docs/explore-analyze/ai-features/agent-builder/models)
- [Elastic: Connect to vLLM (air-gapped)](https://www.elastic.co/docs/explore-analyze/ai-features/llm-guides/connect-to-vLLM)
- [Elastic Blog: Agent Builder with GPT-OSS](https://www.elastic.co/search-labs/blog/build-an-ai-agent-hr-elastic-agent-builder-gpt-oss)
