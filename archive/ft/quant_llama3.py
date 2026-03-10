from llmcompressor.modifiers.quantization import QuantizationModifier
from transformers import AutoModelForCausalLM
from llmcompressor import oneshot

# MODEL_ID = "google/gemma-3-4b-it"
MODEL_ID = "/home/azureuser/alpaca-news/ft/output/unsloth_Llama-3.2-1B-Instruct/finscore"

scheme = "W4A16"  # W4A16, W8A8, NVFP4, FP8_DYNAMIC , --- NVFP4A16, W4A16_ASYM (==AWQ)

# OUTPUT_DIR = MODEL_ID + "_FP8DYNAMIC"
# OUTPUT_DIR = "/home/azureuser/mass-essays/output_wave/model-" + scheme
OUTPUT_DIR = f"{MODEL_ID}-{scheme}"

# load model
model = AutoModelForCausalLM.from_pretrained(
    MODEL_ID,
    device_map="auto",
    torch_dtype="auto",
)

print(model)
# for name, mod in model.named_modules():
#     if name.count(".") <= 3:  # trim output
#         print(name, type(mod).__name__)

#------------------------------------------------------------------------------

targets="Linear",
# targets=["Linear", r"re:^model\.language_model\..*"],


ignore = [
    "lm_head",
    # for gemma3...
    # r"re:^model\.vision_tower(\.|$)",
    # r"re:^model\.multi_modal_projector(\.|$)",
]


recipe = QuantizationModifier(
    scheme=scheme,
    targets=targets,
    ignore=ignore,
)

calib_kwargs = dict(
    model=model,
    recipe=recipe,
    output_dir=OUTPUT_DIR,
    #----------------------------
    dataset="open_platypus",  # can be replaced with a HF dataset or custom iterable
    max_seq_length=4096,
    num_calibration_samples=512,
)

oneshot(**calib_kwargs)

model = AutoModelForCausalLM.from_pretrained(
    OUTPUT_DIR,
    device_map="auto",
    torch_dtype="auto",
)
print(model)