import torch
from PIL import Image
from transformers import AutoProcessor, AutoModelForImageTextToText

torch.set_num_threads(4)
path = r"models\vision\PaddleOCR-VL-1.6"
processor = AutoProcessor.from_pretrained(path, local_files_only=True)
model = AutoModelForImageTextToText.from_pretrained(
    path, dtype=torch.float32, attn_implementation="eager",
    local_files_only=True,
).to("cpu").eval()
with Image.open(r"testing\records\vision-test.png") as source:
    picture = source.convert("RGB")
messages = [{"role": "user", "content": [
    {"type": "image", "image": picture},
    {"type": "text", "text": "OCR:"},
]}]
inputs = processor.apply_chat_template(
    messages, add_generation_prompt=True, tokenize=True,
    return_dict=True, return_tensors="pt",
)
with torch.inference_mode():
    generated = model.generate(**inputs, max_new_tokens=256, do_sample=False)
new_tokens = generated[0, inputs["input_ids"].shape[-1]:]
print(processor.decode(new_tokens, skip_special_tokens=True))