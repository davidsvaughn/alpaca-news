git clone https://github.com/davidsvaughn/alpaca-news.git
cd alpaca-news
virtualenv -p python3.12 .venv && source .venv/bin/activate


cd /home/azureuser/alpaca-news/ft/data
cp /mnt/llm-train/tmp/fintext/*.zip .
unzip train_data.zip

-------------------------------------------

pip install -r requirements.txt

pip install huggingface_hub[cli]
hf auth login --token $HUG_READ_TOKEN
hf auth login --token $HUG_WRITE_TOKEN

wandb login --cloud

-------------------------------------------

python ft/train_model.py
python ft/merge_adapter.py --checkpoint_dir /home/azureuser/alpaca-news/ft/output/unsloth_Llama-3.2-1B-Instruct/checkpoint-5900
python ft/quant_llama3.py

hf upload davidsvaughn/finscore /home/azureuser/alpaca-news/ft/output/unsloth_Llama-3.2-1B-Instruct/finscore --private
hf upload davidsvaughn/finscore-W4A16 /home/azureuser/alpaca-news/ft/output/unsloth_Llama-3.2-1B-Instruct/finscore-W4A16 --private
hf download davidsvaughn/finscore-W4A16
hf download davidsvaughn/finscore-W4A16 --local-dir ~/models/finscore-W4A16

#--------------------------------------------------------------------------------------
# vllm

sudo docker run --gpus all \
    -e "HUGGING_FACE_HUB_TOKEN=$HUG_READ_TOKEN" \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    -p 8080:8000 \
    --ipc=host \
    vllm/vllm-openai:latest \
    --model davidsvaughn/finscore-W4A16 \
    --max-num-seqs 1 \
    --max-model-len 2048 \
    --max-num-batched-tokens 32 \
    --gpu-memory-utilization 0.03

# minimal curl test
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
    "max_tokens": 50
  }'


#--------------------------------------------------------------------------------------
# aphrodite

sudo docker run --gpus all \
  -e "HUGGING_FACE_HUB_TOKEN=$HUG_READ_TOKEN" \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -p 8080:7860 \
  --ipc=host \
  alpindale/aphrodite-engine:latest \
  serve \
  --model davidsvaughn/finscore-W4A16 \
  --port 8080 \
  --max-seq-len 2048 \
  --api openai \
  --dtype auto \
  --threads 8

sudo docker run --gpus all \
  -e HUGGING_FACE_HUB_TOKEN="$HUG_READ_TOKEN" \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -p 8080:2242 \
  --ipc=host \
  alpindale/aphrodite-openai:latest \
  --model davidsvaughn/finscore-W4A16 \
  --single-user-mode \
  --gpu-memory-utilization 0.1