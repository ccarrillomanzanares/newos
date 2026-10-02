#!/usr/bin/env python3
"""Fine-tuning LoRA/QLoRA de LLaMA 3.1 8B Instruct con transformers + PEFT.

Lee la configuración de ``lora_config.yaml`` (el mismo fichero sirve para
LLaMA-Factory) y entrena sobre el JSONL generado por ``format_dataset.py``.
La pérdida solo se calcula sobre los turnos del asistente.

Uso:
    python training/finetune/train.py --config training/finetune/lora_config.yaml
    python training/finetune/train.py --config ... --max-steps 10   # prueba rápida

Después: fusionar el adaptador y convertir a GGUF (ver training/README.md).

Dependencias: torch, transformers>=4.43, peft>=0.11, datasets, pyyaml, accelerate
(+ bitsandbytes para QLoRA 4-bit).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("agentos.train")
IGNORE_INDEX = -100


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def tokenize_conversation(messages: list[dict[str, str]], tokenizer: Any, max_len: int) -> dict[str, list[int]]:
    """Tokeniza con la plantilla de chat y enmascara todo lo que no sea respuesta del asistente.

    Se tokeniza incrementalmente: el prefijo hasta antes de cada turno del
    asistente se marca con IGNORE_INDEX; el contenido del asistente se aprende.
    """
    input_ids: list[int] = []
    labels: list[int] = []
    # Supone que la plantilla es "prefijo-estable" (cierto para llama3): tokenizar
    # messages[:i+1] produce los tokens de messages[:i] seguidos del turno i.
    for i, msg in enumerate(messages):
        full = tokenizer.apply_chat_template(messages[: i + 1], tokenize=True, add_generation_prompt=False)
        if msg["role"] == "assistant":
            with_prompt = tokenizer.apply_chat_template(messages[:i], tokenize=True, add_generation_prompt=True)
            # Cabecera del asistente -> ignorada; contenido (incluido <|eot_id|>) -> aprendido
            new_prompt = with_prompt[len(input_ids):]
            new_answer = full[len(with_prompt):]
            input_ids += new_prompt + new_answer
            labels += [IGNORE_INDEX] * len(new_prompt) + new_answer
        else:
            new = full[len(input_ids):]
            input_ids += new
            labels += [IGNORE_INDEX] * len(new)
    return {"input_ids": input_ids[:max_len], "labels": labels[:max_len],
            "attention_mask": [1] * min(len(input_ids), max_len)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="training/finetune/lora_config.yaml")
    parser.add_argument("--max-steps", type=int, default=None, help="Sobrescribe max_steps (pruebas)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (AutoModelForCausalLM, AutoTokenizer, DataCollatorForSeq2Seq, Trainer,
                              TrainingArguments)

    cfg = load_config(args.config)
    model_name = cfg["model_name_or_path"]
    max_len = int(cfg.get("cutoff_len", 4096))
    output_dir = Path(cfg.get("output_dir", "training/output/agentos-lora"))

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # --- Modelo (QLoRA 4-bit opcional)
    quant = cfg.get("quantization_bit")
    model_kwargs: dict[str, Any] = {"torch_dtype": torch.bfloat16 if cfg.get("bf16", True) else torch.float16}
    if quant == 4:
        from transformers import BitsAndBytesConfig

        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=model_kwargs["torch_dtype"])
        model_kwargs["device_map"] = "auto"
    model = AutoModelForCausalLM.from_pretrained(model_name, **model_kwargs)
    if quant == 4:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    elif cfg.get("gradient_checkpointing", True):
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()

    targets = cfg.get("lora_target", "q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj")
    lora = LoraConfig(
        r=int(cfg.get("lora_rank", 16)), lora_alpha=int(cfg.get("lora_alpha", 32)),
        lora_dropout=float(cfg.get("lora_dropout", 0.05)), bias="none", task_type="CAUSAL_LM",
        target_modules=[t.strip() for t in targets.split(",")] if isinstance(targets, str) else targets,
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    # --- Datos
    files = {"train": cfg["train_file"]}
    if cfg.get("eval_file") and Path(cfg["eval_file"]).is_file():
        files["validation"] = cfg["eval_file"]
    raw = load_dataset("json", data_files=files)
    tokenized = raw.map(lambda ex: tokenize_conversation(ex["messages"], tokenizer, max_len),
                        remove_columns=raw["train"].column_names, desc="Tokenizando")
    tokenized = tokenized.filter(lambda ex: any(label != IGNORE_INDEX for label in ex["labels"]))

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        per_device_train_batch_size=int(cfg.get("per_device_train_batch_size", 1)),
        gradient_accumulation_steps=int(cfg.get("gradient_accumulation_steps", 16)),
        learning_rate=float(cfg.get("learning_rate", 2e-4)),
        num_train_epochs=float(cfg.get("num_train_epochs", 3)),
        max_steps=args.max_steps if args.max_steps is not None else int(cfg.get("max_steps", -1)),
        lr_scheduler_type=cfg.get("lr_scheduler_type", "cosine"),
        warmup_ratio=float(cfg.get("warmup_ratio", 0.03)),
        logging_steps=int(cfg.get("logging_steps", 10)),
        save_steps=int(cfg.get("save_steps", 500)),
        eval_strategy="steps" if "validation" in tokenized else "no",
        eval_steps=int(cfg.get("eval_steps", 500)),
        bf16=bool(cfg.get("bf16", True)),
        fp16=bool(cfg.get("fp16", False)),
        gradient_checkpointing=bool(cfg.get("gradient_checkpointing", True)),
        report_to=cfg.get("report_to", "none"),
        remove_unused_columns=False,
    )
    trainer = Trainer(
        model=model, args=training_args, train_dataset=tokenized["train"],
        eval_dataset=tokenized.get("validation"),
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=IGNORE_INDEX),
    )
    trainer.train()
    model.save_pretrained(output_dir / "adapter")
    tokenizer.save_pretrained(output_dir / "adapter")
    logger.info("Adaptador LoRA guardado en %s", output_dir / "adapter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
