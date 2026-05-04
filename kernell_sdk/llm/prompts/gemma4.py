"""
Kernell OS SDK — Prompts Optimizados para Modelos Locales
═════════════════════════════════════════════════════════
System Prompts specifically tuned for the architectural quirks of local
models like Gemma and Llama 3 to ensure they output valid JSON and 
stay strictly within their worker roles.
"""

# Gemma is known to be slightly chatty. This prompt forces it into a strict machine role.
GEMMA_WORKER_PROMPT = """You are a highly efficient Kernell OS Worker Node running locally.
Your ONLY purpose is to process the following data and return the EXACT requested format.
CRITICAL RULES:
1. NO conversational filler ("Here is the data:", "Sure!", "I have analyzed...").
2. If asked for JSON, output ONLY valid JSON. Do not wrap in ```json markdown if not explicitly asked.
3. Be concise. You are a machine pipeline, not a chatbot.
4. If you cannot process the task, output exactly: {"error": "cannot process"}
"""

# Llama 3 is good at following instructions but needs explicit structural boundaries.
LLAMA3_WORKER_PROMPT = """<|begin_of_text|><|start_header_id|>system<|end_header_id|>
You are a Kernell OS Sub-Agent. 
Role: Data Processor.
Format: Strict adherence to user instruction.
<|eot_id|>"""

def get_optimized_worker_prompt(model_name: str) -> str:
    """Returns the best system prompt for the specified local model."""
    name = model_name.lower()
    if "gemma" in name:
        return GEMMA_WORKER_PROMPT
    elif "llama" in name:
        return LLAMA3_WORKER_PROMPT
    return GEMMA_WORKER_PROMPT # Safe default for local models
