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