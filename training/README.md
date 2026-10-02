# Pipeline de fine-tuning de AgentOS

Objetivo: especializar **LLaMA 3.1 8B Instruct** en administración de sistemas Linux
y en el formato exacto de tool calling de AgentD (`<tool>{...}</tool>` + `[OBSERVATION ...]`).

```
collect_manpages.py ─┐
collect_docs.py ─────┼─> format_dataset.py ─> agentos_sft.jsonl ─> train.py (LoRA/QLoRA)
plantillas ReAct ────┘                                              │
                                   merge + convert_hf_to_gguf + quantize Q4_K_M
                                                                    │
                                              models/agentos-8b-q4_k_m.gguf ─> AgentD
```

## 1. Dependencias

```bash
pip install torch transformers>=4.43 peft>=0.11 datasets accelerate bitsandbytes pyyaml
# opcional: pip install llamafactory
sudo apt install man-db manpages manpages-dev   # para collect_manpages.py
```

Requiere una GPU NVIDIA (≈ 12 GB de VRAM con QLoRA 4-bit, ≈ 24 GB con LoRA bf16) y aceptar
la licencia de `meta-llama/Meta-Llama-3.1-8B-Instruct` en Hugging Face (`huggingface-cli login`).

## 2. Recolección de datos

```bash
python training/data/collect_manpages.py --out training/data/raw/manpages.jsonl --sections 1 8
python training/data/collect_docs.py     --out training/data/raw/docs.jsonl
```

## 3. Formateo del dataset

```bash
python training/data/format_dataset.py --raw-dir training/data/raw \
    --out training/data/agentos_sft.jsonl --full-system-prompt
```

Genera `agentos_sft.jsonl` y `agentos_sft_eval.jsonl` en formato `{"messages": [...]}`.
Las trayectorias sintéticas de tool calling usan el system prompt real de AgentD
(`--full-system-prompt`), así el modelo aprende exactamente el formato de producción.

## 4. Entrenamiento

```bash
# Prueba rápida
python training/finetune/train.py --config training/finetune/lora_config.yaml --max-steps 10
# Completo
python training/finetune/train.py --config training/finetune/lora_config.yaml
# Alternativa con LLaMA-Factory (registrar el dataset, ver comentario en lora_config.yaml)
llamafactory-cli train training/finetune/lora_config.yaml
```

## 5. Fusión y conversión a GGUF

```bash
python - <<'EOF'
from peft import AutoPeftModelForCausalLM
from transformers import AutoTokenizer
m = AutoPeftModelForCausalLM.from_pretrained("training/output/agentos-lora/adapter", torch_dtype="auto")
m.merge_and_unload().save_pretrained("training/output/agentos-merged")
AutoTokenizer.from_pretrained("training/output/agentos-lora/adapter").save_pretrained("training/output/agentos-merged")
EOF
git clone https://github.com/ggerganov/llama.cpp && cd llama.cpp && cmake -B build && cmake --build build -j
python convert_hf_to_gguf.py ../training/output/agentos-merged --outfile agentos-8b-f16.gguf --outtype f16
./build/bin/llama-quantize agentos-8b-f16.gguf ../models/agentos-8b-q4_k_m.gguf Q4_K_M
```

Usar el modelo: `AGENTOS_MODEL_PATH=models/agentos-8b-q4_k_m.gguf make dev-agent`.

## 6. Siguientes pasos (fase 3 del roadmap)

- Recolectar trayectorias reales desde `~/.agentos/memory.db` (memoria episódica) y el audit log.
- DPO con pares (respuesta preferida / rechazada) para reforzar la confirmación ante acciones destructivas.
