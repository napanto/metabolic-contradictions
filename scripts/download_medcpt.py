"""Download pinned public encoder files without using host Hugging Face tokens."""

import json
from pathlib import Path

from huggingface_hub import model_info, snapshot_download

path = Path("configs/medcpt.json")
records = json.loads(path.read_text()) if path.exists() else []
for name in ["ncbi/MedCPT-Article-Encoder", "ncbi/MedCPT-Query-Encoder"]:
    existing = next((row for row in records if row["model"] == name), None)
    if existing is None:
        info = model_info(name, token=False)
        existing = {"model": name, "revision": info.sha}
        records.append(existing)
        path.write_text(json.dumps(records, indent=2) + "\n")
    info = model_info(name, revision=existing["revision"], token=False)
    weights = (
        "model.safetensors"
        if any(s.rfilename == "model.safetensors" for s in info.siblings)
        else "pytorch_model.bin"
    )
    local_path = snapshot_download(
        name,
        revision=existing["revision"],
        token=False,
        cache_dir="data/models/hf",
        allow_patterns=[
            "config.json",
            weights,
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "vocab.txt",
        ],
    )
    print(name, existing["revision"], local_path, flush=True)
