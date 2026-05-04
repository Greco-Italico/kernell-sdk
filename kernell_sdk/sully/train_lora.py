"""
Kernell OS SDK — Local LoRA Finetuning Pipeline
═══════════════════════════════════════════════
Closed-loop learning: Reads guardrail-validated datasets, applies LoRA,
and pushes versioned adapters locally for immediate deployment.
"""

import hashlib
import json
import logging
import os
import time

try:
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
except ImportError:
    logging.warning("[Trainer] transformers/peft not installed. Train pipeline will fail on execution.")

from kernell_sdk.sully.guardrail_pre_training import DataGuardrail, resample_for_training

logger = logging.getLogger("kernell.sully.trainer")


def compute_dataset_hash(filepath: str) -> str:
    """Generate deterministic hash of the dataset to version the adapter."""
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        hasher.update(f.read())
    return hasher.hexdigest()[:8]


def build_hf_dataset(samples: list, tokenizer) -> Dataset:
    """Format samples into HuggingFace dataset with prompt templates."""
    formatted = []
    for s in samples:
        # Simple instruct format (Alpaca style)
        text = (
            f"Below is an instruction that describes a task, paired with an input.\n"
            f"Write a response that appropriately completes the request.\n\n"
            f"### Instruction:\n{s['instruction']}\n\n"
            f"### Input:\n{s['input']}\n\n"
            f"### Response:\n{s['output']}"
        )
        formatted.append({"text": text})
        
    dataset = Dataset.from_list(formatted)
    
    # Tokenize
    def tokenize_function(examples):
        return tokenizer(
            examples["text"],
            padding="max_length",
            truncation=True,
            max_length=512
        )
        
    tokenized_dataset = dataset.map(tokenize_function, batched=True)
    return tokenized_dataset


def train(
    base_model_id: str = "meta-llama/Meta-Llama-3-8B-Instruct",
    dataset_path: str = "/home/anny/.gemini/antigravity/dataset/sully.jsonl",
    output_dir: str = "/home/anny/.gemini/antigravity/models/"
):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    # 1. Guardrail Validation (Fail hard if corrupted)
    logger.info("🛡️ Initiating Data Guardrail...")
    guardrail = DataGuardrail(dataset_path=dataset_path)
    samples = guardrail.validate_all()
    
    # 2. Pre-processing
    logger.info("⚖️ Resampling based on Reward signals...")
    samples = resample_for_training(samples)
    
    dataset_hash = compute_dataset_hash(dataset_path)
    adapter_name = f"sully-lora-v3.3-r{dataset_hash}"
    adapter_path = os.path.join(output_dir, adapter_name)
    
    logger.info(f"🚀 Initializing Training Pipeline for: {adapter_name}")
    
    # 3. Model & Tokenizer loading
    logger.info("📦 Loading Base Model & Tokenizer (4-bit quantization if supported)...")
    tokenizer = AutoTokenizer.from_pretrained(base_model_id)
    tokenizer.pad_token = tokenizer.eos_token
    
    # In a real environment, you'd use BitsAndBytesConfig for 4-bit loading.
    # For compatibility we use standard loading, assuming enough VRAM or CPU.
    try:
        model = AutoModelForCausalLM.from_pretrained(
            base_model_id,
            device_map="auto",
            torch_dtype=torch.float16,
        )
        model = prepare_model_for_kbit_training(model)
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        raise
        
    # 4. LoRA Configuration
    logger.info("🧠 Injecting LoRA adapters...")
    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM"
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    
    # 5. Dataset preparation
    logger.info("🗂️ Formatting HuggingFace Dataset...")
    train_dataset = build_hf_dataset(samples, tokenizer)
    
    # 6. Training Arguments
    training_args = TrainingArguments(
        output_dir=adapter_path,
        per_device_train_batch_size=4,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        logging_steps=10,
        max_steps=100,  # Quick iteration, adjust for full dataset
        save_strategy="steps",
        save_steps=50,
        optim="paged_adamw_8bit",
        fp16=True,
        report_to="none" # Disable wandb for local isolated runs
    )
    
    # 7. Train
    logger.info("🔥 Starting LoRA Fine-Tuning...")
    trainer = Trainer(
        model=model,
        train_dataset=train_dataset,
        args=training_args,
        data_collator=None # Use default DataCollatorForLanguageModeling
    )
    
    trainer.train()
    
    # 8. Save
    logger.info(f"💾 Saving specialized adapter to {adapter_path}...")
    model.save_pretrained(adapter_path)
    tokenizer.save_pretrained(adapter_path)
    logger.info("✅ Pipeline Complete. Adapter ready for Deployment.")


if __name__ == "__main__":
    train()
