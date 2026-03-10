import os, sys, glob, json, argparse, re, random, yaml
from typing import List, Dict, Any
import torch
from datasets import Dataset
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from jinja2 import Template
from tqdm import tqdm
import torch, random, re
import wandb
from dataclasses import asdict

from unsloth import FastModel
from unsloth.chat_templates import get_chat_template, train_on_responses_only
from trl import SFTTrainer, SFTConfig
from transformers import TrainerCallback

import torch._dynamo
torch._dynamo.config.disable = True

#--------------------------------------------------------------------------------------------------

MODEL_NAME = "unsloth/Llama-3.2-1B-Instruct"

# MODEL_NAME = "unsloth/Llama-3.2-3B-Instruct"
# MODEL_NAME = "unsloth/gemma-3-4b-it"
# MODEL_NAME = "unsloth/gemma-3-270m-it"

EVAL_STEPS = 50
LOG_STEPS = 10

EVAL_HOLDOUT = 0.08

SEED = 1234

TRAIN_BATCH_SIZE = 8
EVAL_BATCH_SIZE = 8
NUM_EPOCHS = 10
GRAD_ACCUMULATION_STEPS = 1
LEARNING_RATE = 2e-5

LORA_R = 32
LORA_ALPHA = LORA_R * 2
LORA_DROPOUT = 0.05

MAX_SEQ_LENGTH = 2048  # max seq length for the model

JSON_DIR = "ft/data/train_data/json"
OUT_DIR = "ft/output"

TYPE_LABEL_FILE = "ft/data/train_data/type.tsv"
SIGNAL_LABEL_FILE = "ft/data/train_data/signal.tsv"

TYPE_PROMPT_FILE = "ft/prompts/type_prompt.md"
SIGNAL_PROMPT_FILE = "ft/prompts/signal_prompt.md"

#--------------------------------------------------------------------------------------------------

# function to load JSON files from a directory into a dictionary mapping each id to each JSON object
def load_json_files(json_dir: str) -> Dict[int, Dict[str, Any]]:
    json_files = glob.glob(os.path.join(json_dir, "*.json"))
    data = {}
    for file in json_files:
        with open(file, "r") as f:
            obj = json.load(f)
            data[obj["id"]] = obj
    return data

# function to load label file and return a dictionary mapping id to label (has header)
def load_label_file(label_file: str) -> Dict[int, Any]:
    label_map = {}
    with open(label_file, "r") as f:
        for line in f:
            if line.startswith("#"):  # skip header lines
                continue
            parts = line.strip().split("\t")
            if len(parts) != 2:
                continue
            uid, label = parts
            label_map[int(uid)] = int(label) if label.isdigit() else float(label)
    return label_map

def read_prompt(path: str) -> str:
    with open(path) as f: return f.read()

def render_prompt(md: str, obj: Dict[str, Any]) -> str:
    article_json = json.dumps(obj, ensure_ascii=False, indent=2)
    prompt = md.format(article=article_json)
    return prompt

def build_messages_gemma(prompt_text: str, target: str) -> List[Dict[str, Any]]:
    if prompt_text is None:
        return None
    return [
        {"role": "user",  "content": [{"type": "text", "text": prompt_text}]},
        {"role": "model", "content": [{"type": "text", "text": target}]},
    ]

def build_messages_llama(prompt_text: str, target: str) -> List[Dict[str, Any]]:
    if prompt_text is None:
        return None
    return [
        {"role": "user",  "content": prompt_text},
        {"role": "assistant", "content": target},
    ]

def make_dataset() -> List[Dict[str, Any]]:
    data = load_json_files(JSON_DIR)
    print(f"Loaded {len(data)} JSON files from {JSON_DIR}")
    
    # test formatting a prompt
    type_prompt_md = read_prompt(TYPE_PROMPT_FILE)
    signal_prompt_md = read_prompt(SIGNAL_PROMPT_FILE)
    sample_id = random.choice(list(data.keys()))
    sample_obj = data[sample_id]
    type_prompt = render_prompt(type_prompt_md, sample_obj)
    signal_prompt = render_prompt(signal_prompt_md, sample_obj)
    
    build_messages = build_messages_gemma if "gemma" in MODEL_NAME else build_messages_llama
    
    # load labels
    type_labels = load_label_file(TYPE_LABEL_FILE)
    signal_labels = load_label_file(SIGNAL_LABEL_FILE)
    print(f"Loaded {len(type_labels)} type labels from {TYPE_LABEL_FILE}")
    print(f"Loaded {len(signal_labels)} signal labels from {SIGNAL_LABEL_FILE}")
    out = []
    for uid, obj in data.items():
        if uid not in type_labels and uid not in signal_labels:
            continue
        type_label = type_labels.get(uid, -1)
        signal_label = signal_labels.get(uid, -1)
        type_prompt = render_prompt(type_prompt_md, obj) if type_label >-1 else None
        signal_prompt = render_prompt(signal_prompt_md, obj) if signal_label >-1 else None
        out.append({
            "uid": uid,
            "type_label": type_label,
            "signal_label": signal_label,
            "type_messages": build_messages(type_prompt, str(type_label)),
            "signal_messages": build_messages(signal_prompt, str(signal_label)),
        })
    return out

# expand train and eval data to have separate entries for type and signal if both are present
def expand_data(data):
    out = []
    for item in data:
        if item["type_label"] > -1:
            out.append({
                "uid": item["uid"],
                "task": "type",
                "label": item["type_label"],
                "messages": item["type_messages"],
            })
        if item["signal_label"] > -1:
            out.append({
                "uid": item["uid"],
                "task": "signal",
                "label": item["signal_label"],
                "messages": item["signal_messages"],
            })
    return out

class OnIntervalEval(TrainerCallback):
    def __init__(self, tokenizer, eval_ds, every_steps=50, max_new_tokens=3, batch_size=16):
        self.tok = tokenizer
        self.eval = eval_ds
        self.every_steps = every_steps
        self.max_new_tokens = max_new_tokens
        self.batch_size = batch_size

    def _run_eval(self, model, trainer=None):
        import torch, random, re
        from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix
        import numpy as np

        model.eval()
        idxs = list(range(len(self.eval)))
        
        # Calculate total number of batches for progress bar
        total_batches = (len(idxs) + self.batch_size - 1) // self.batch_size
        
        # Separate lists for signal and type tasks
        signal_preds, signal_golds = [], []
        type_preds, type_golds = [], []
        
        # Add progress bar to batch processing
        batch_range = range(0, len(idxs), self.batch_size)
        progress_bar = tqdm(batch_range, desc="Evaluating", unit="batch", 
                           total=total_batches, leave=False)
        
        for s in progress_bar:
            batch = [self.eval[i] for i in idxs[s:s+self.batch_size]]
            prompts = [self.tok.apply_chat_template(ex["messages"][:-1], tokenize=False, add_generation_prompt=True) for ex in batch]
            inputs = self.tok(prompts, 
                              return_tensors="pt", 
                              padding=True,
                            #   padding="max_length", 
                              padding_side='left',
                              truncation=True, 
                              ).to(model.device)
            with torch.no_grad():
                out = model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False, temperature=0.0, eos_token_id=self.tok.eos_token_id)
            lens = (inputs.input_ids != self.tok.pad_token_id).sum(dim=1)
            
            for i in range(out.size(0)):
                # gen = self.tok.decode(out[i][lens[i]:], skip_special_tokens=True)
                gen = self.tok.decode(out[i][-self.max_new_tokens:], skip_special_tokens=False)
                
                # Extract numeric prediction from generated text
                m = re.search(r'\b(\d+)\b', gen.strip())
                if m:
                    pred = int(m.group(1))
                else:
                    pred = -1  # Invalid prediction
                
                task = batch[i]["task"]
                label = batch[i]["label"]
                
                # Separate by task type
                if task == "signal":
                    signal_preds.append(pred)
                    signal_golds.append(label)
                elif task == "type":
                    type_preds.append(pred)
                    type_golds.append(label)
            
            # Update progress bar with current stats
            total_samples = len(signal_preds) + len(type_preds)
            progress_bar.set_postfix({"samples": total_samples, "signal": len(signal_preds), "type": len(type_preds)})
        
        progress_bar.close()
        
        # Compute metrics
        metrics = {}
        
        # Signal task metrics (RMSE, MAE)
        if signal_preds:
            # Filter out invalid predictions for RMSE calculation
            valid_signal = [(p, g) for p, g in zip(signal_preds, signal_golds) if p != -1]
            if valid_signal:
                s_preds, s_golds = zip(*valid_signal)
                s_preds = np.array(s_preds, dtype=float)
                s_golds = np.array(s_golds, dtype=float)
                
                # RMSE
                rmse = np.sqrt(np.mean((s_preds - s_golds) ** 2))
                mae = np.mean(np.abs(s_preds - s_golds))
                
                metrics["eval_signal_rmse"] = rmse
                metrics["eval_signal_mae"] = mae
                # metrics["eval_signal_samples"] = len(valid_signal)
                # metrics["eval_signal_invalid"] = len(signal_preds) - len(valid_signal)
        
        # Type task metrics (accuracy, precision, recall, F1)
        if type_preds:
            # Filter out invalid predictions
            valid_type = [(p, g) for p, g in zip(type_preds, type_golds) if p != -1]
            if valid_type:
                t_preds, t_golds = zip(*valid_type)
                t_preds = np.array(t_preds)
                t_golds = np.array(t_golds)
                
                # Classification metrics
                acc = accuracy_score(t_golds, t_preds)
                p, r, f1, _ = precision_recall_fscore_support(t_golds, t_preds, average="macro", zero_division=0)
                
                metrics["eval_type_accuracy"] = acc
                metrics["eval_type_precision"] = p
                metrics["eval_type_recall"] = r
                metrics["eval_type_f1"] = f1
                # metrics["eval_type_samples"] = len(valid_type)
                # metrics["eval_type_invalid"] = len(type_preds) - len(valid_type)
                
                # Confusion matrix
                cm = confusion_matrix(t_golds, t_preds, labels=list(range(8)))
                # Store confusion matrix as a string for logging (optional)
                # You could also log this as an image to wandb if needed
        
        print(f"\n[Eval @ step] {metrics}\n")
        if trainer: 
            trainer.log(metrics)
            
        try:
            if wandb.run is not None:
                step = trainer.state.global_step if trainer else None
                wandb.log({key.replace("eval_", "eval/"): value for key, value in metrics.items()}, step=step)
        except Exception as e:
            print(f"WARNING: Failed to explicitly log to wandb: {e}")

    def on_step_end(self, args, state, control, **kwargs):
        if state.global_step and state.global_step % self.every_steps == 0:
            self._run_eval(kwargs["model"], trainer=kwargs.get("trainer"))

def main():
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)
    
    
    #--------------------------------------------------------------------------------------------------
    dataset = make_dataset()
    print(f"Built dataset with {len(dataset)} samples")
    
    # split into train and eval
    train_data, eval_data = train_test_split(dataset, test_size=EVAL_HOLDOUT, random_state=42)
    train_data = expand_data(train_data)
    eval_data = expand_data(eval_data)
    # shuffle data
    random.shuffle(train_data)
    random.shuffle(eval_data)
    print(f"Train samples: {len(train_data)}, Eval samples: {len(eval_data)}")
    train_ds = Dataset.from_list(train_data)
    eval_ds  = Dataset.from_list(eval_data)
    print()
    
    #----------------------------------------------------------------------------------------------

    # ---- Unsloth notebook style from here ----
    model, tokenizer = FastModel.from_pretrained(
        model_name = MODEL_NAME,
        max_seq_length = MAX_SEQ_LENGTH,
        load_in_4bit = False,
        load_in_8bit = False,
        full_finetuning = False,
    )
    
    model = FastModel.get_peft_model(
        model,
        finetune_vision_layers     = False, # Turn off for just text!
        finetune_language_layers   = True,  # Should leave on!
        finetune_attention_modules = True,  # Attention good for GRPO
        finetune_mlp_modules       = True,  # SHould leave on always!

        r = LORA_R,                # Larger = higher accuracy, but might overfit
        lora_alpha = LORA_ALPHA,   # Recommended alpha == r at least
        lora_dropout = LORA_DROPOUT,
        bias = "none",
        random_state = SEED,
    )
    
    if "gemma" in MODEL_NAME:
        tokenizer = get_chat_template(tokenizer, chat_template="gemma-3")
    
    def formatting_prompts_func(examples):
        convos = examples["messages"]
        texts = [
            tokenizer.apply_chat_template(
                convo, tokenize=False, add_generation_prompt=False
            )
            # ).removeprefix("<|eot_id|>")
            # ).removeprefix("<bos>")
            # ).lstrip("<bos>").strip()
            for convo in convos
        ]
        return {"text": texts}

    # Map to 'text' column for SFTTrainer
    train_ds = train_ds.map(formatting_prompts_func, batched=True, remove_columns=[c for c in train_ds.column_names if c != "text"])
    # eval_text_ds = eval_ds.map(formatting_prompts_func, batched=True, remove_columns=[c for c in eval_ds.column_names if c != "text"])
    # Keep a label-carrying copy for custom eval generation
    eval_gen_ds = eval_ds  # contains messages + label
    
    # print examples
    # for i in range(3):
    #     print(f"\n\nEXAMPLE {i+1}: {train_ds[i*10]['text']}\n")
    # sys.exit()
        
    #----------------------------------------------------------------------------------------------
    sft_config = SFTConfig(
        dataset_text_field = "text",
        per_device_train_batch_size = TRAIN_BATCH_SIZE,
        per_device_eval_batch_size = EVAL_BATCH_SIZE,
        gradient_accumulation_steps = GRAD_ACCUMULATION_STEPS,
        gradient_checkpointing = True,
        warmup_steps = 5,
        learning_rate = LEARNING_RATE,
        logging_steps = LOG_STEPS,
        save_steps = EVAL_STEPS,
        optim = "adamw_8bit",
        weight_decay = 0.01,
        lr_scheduler_type = "linear",
        seed = SEED,
        report_to = "wandb",
        output_dir = os.path.join(OUT_DIR, MODEL_NAME.replace("/", "_")),
        num_train_epochs = NUM_EPOCHS,
        # max_steps = args.max_steps if args.epochs is None else None,
    )

    #----------------------------------------------------------------------------------------------
    # Make data serializable for wandb
    def make_json_serializable(d):
        def is_json_serializable(value):
            try:
                json.dumps(value)
                return True
            except (TypeError, OverflowError):
                return False
        return {k: v if is_json_serializable(v) else str(v) for k, v in d.items()}
    
    wandb.init(project= f"FINTEXT-FT--{MODEL_NAME.replace('/','_')}")
    wandb.config.update(make_json_serializable(asdict(sft_config)), allow_val_change=True)
    wandb_run_name = wandb.run.name
    wandb_run_id = wandb.run.id
    print(f"Wandb run name: {wandb_run_name}, id: {wandb_run_id}")
    
    #----------------------------------------------------------------------------------------------
    
    trainer = SFTTrainer(
        model = model,
        tokenizer = tokenizer,
        train_dataset = train_ds,
        eval_dataset = None,  # custom eval below
        args = sft_config,
    )
    
    instruction_part = "<start_of_turn>user\n" if "gemma" in MODEL_NAME else "<|start_header_id|>user"
    response_part = "<start_of_turn>model\n" if "gemma" in MODEL_NAME else "<|start_header_id|>assistant"

    trainer = train_on_responses_only(
        trainer,
        instruction_part=instruction_part,
        response_part=response_part,
    )
    
    # On-epoch eval with custom generation and metrics
    trainer.add_callback(OnIntervalEval(tokenizer, eval_gen_ds, every_steps=EVAL_STEPS, batch_size=EVAL_BATCH_SIZE))

    trainer_stats = trainer.train()
    
    print(f"Training completed. Final stats: {trainer_stats}")
    

#----------------------------------------------------------------------------------------------

if __name__ == "__main__":
    main()
    
    # # test loading JSON files
    # data = load_json_files(JSON_DIR)
    # print(f"Loaded {len(data)} JSON files from {JSON_DIR}")
    
    # # test formatting a prompt
    # type_prompt_md = read_prompt(TYPE_PROMPT_FILE)
    # signal_prompt_md = read_prompt(SIGNAL_PROMPT_FILE)
    # sample_id = random.choice(list(data.keys()))
    # sample_obj = data[sample_id]
    # type_prompt = render_prompt(type_prompt_md, sample_obj)
    # signal_prompt = render_prompt(signal_prompt_md, sample_obj)
    # print("Sample Type Prompt:")
    # print(type_prompt)
    # print("Sample Signal Prompt:")
    # print(signal_prompt)
