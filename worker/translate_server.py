import os
from fastapi import FastAPI
from pydantic import BaseModel
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

MODEL_NAME = os.environ.get("TRANSLATE_MODEL", "facebook/mbart-large-50-many-to-many-mmt")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MAX_CHARS = int(os.environ.get("TRANSLATE_MAX_CHARS", "800"))

app = FastAPI()

tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME).to(DEVICE)


class TranslateRequest(BaseModel):
    text: str
    source_lang: str = "ja_XX"
    target_lang: str = "ko_KR"


def chunk_text(text: str, max_chars: int):
    parts = text.split("\n\n")
    chunks = []
    buf = ""
    for part in parts:
        candidate = part if not buf else buf + "\n\n" + part
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            buf = part
    if buf:
        chunks.append(buf)
    return chunks


def translate_chunk(text: str, source_lang: str, target_lang: str) -> str:
    tokenizer.src_lang = source_lang
    encoded = tokenizer(text, return_tensors="pt", truncation=True, max_length=1024)
    encoded = {k: v.to(DEVICE) for k, v in encoded.items()}
    generated = model.generate(
        **encoded,
        forced_bos_token_id=tokenizer.lang_code_to_id[target_lang],
        max_length=1024,
    )
    return tokenizer.batch_decode(generated, skip_special_tokens=True)[0]


@app.post("/translate")
def translate(req: TranslateRequest):
    text = req.text or ""
    if not text.strip():
        return {"translated": ""}

    chunks = chunk_text(text, MAX_CHARS)
    outputs = []
    for chunk in chunks:
        outputs.append(translate_chunk(chunk, req.source_lang, req.target_lang))
    return {"translated": "\n\n".join(outputs)}
