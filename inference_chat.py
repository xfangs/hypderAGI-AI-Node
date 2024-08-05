from flask import Flask, request, jsonify
from unsloth import FastLanguageModel
import os
from eth_utils import is_address
import nacos
import logging
import time
import queue
import threading
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel
from transformers import TextStreamer

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG)

# Configuration and constants
max_seq_length = 2048
batch_size = 5
model_name = os.getenv("MODEL_NAME", "")
wallet_address = os.getenv("WALLET_ADDRESS", "")
nacos_server = os.getenv("NACOS_SERVER", "nacos.hyperagi.network:80")
public_ip = os.getenv("PUBLIC_IP", "")
port = int(os.getenv("PORT", 5000))
service_name = os.getenv("SERVICE_NAME", "hyperAGI-inference-chat")
dtype = None
load_in_4bit = True

# Prompt template
alpaca_prompt = """Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

### Instruction:
{}

### Input:
{}

### Response:
{}"""

# Validate environment variables
if not model_name:
    raise ValueError("MODEL_NAME environment variable is not set or is empty")
if not wallet_address or not is_address(wallet_address):
    raise ValueError("Invalid or empty WALLET_ADDRESS environment variable")
if not public_ip:
    raise ValueError("PUBLIC_IP environment variable is not set or is empty")

# Nacos client setup
nacos_client = nacos.NacosClient(nacos_server, namespace="", username=os.getenv("NACOS_USERNAME", ""), password=os.getenv("NACOS_PASSWORD", ""))


# Local cache for service registration
local_service_cache = None

# Service registration with retries
max_retries = 5

def register_service_with_cache():
    global local_service_cache
    for attempt in range(max_retries):
        try:
            response = nacos_client.add_naming_instance(service_name, public_ip, port, metadata={"walletAddress": wallet_address})
            logging.info(f"Successfully registered with Nacos: {response}")
            local_service_cache = {"service_name": service_name, "public_ip": public_ip, "port": port}
            break
        except Exception as e:
            logging.error(f"Failed to register with Nacos on attempt {attempt + 1}: {e}")
            time.sleep(5)
    else:
        if local_service_cache:
            logging.warning("Using cached service registration information due to Nacos unavailability.")
        else:
            raise RuntimeError("Failed to register with Nacos and no cache is available")

register_service_with_cache()


# Heartbeat function with improved error handling
def send_heartbeat():
    while True:
        try:
            nacos_client.send_heartbeat(service_name, public_ip, port, metadata={"walletAddress": wallet_address})
            logging.info("Heartbeat sent successfully.")
        except Exception as e:
            logging.error(f"Failed to send heartbeat: {e}")
            logging.warning("Heartbeat failed, but service will continue running.")
        time.sleep(5)

# Start heartbeat thread
heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
heartbeat_thread.start()

# # Load model and tokenizer
# model, tokenizer = FastLanguageModel.from_pretrained(model_name, dtype=dtype, load_in_4bit=load_in_4bit)
# FastLanguageModel.for_inference(model)


# 设置模型参数
adapter_name = model_name

logging.info(f'Model name: {adapter_name}')


max_seq_length = 2048
dtype = None  # 根据实际需要设置
load_in_4bit = True

# 加载预训练模型和分词器
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="unsloth/llama-2-7b-bnb-4bit",
    max_seq_length=max_seq_length,
    dtype=dtype,
    load_in_4bit=load_in_4bit,
)

# 应用PEFT适配器
model = PeftModel.from_pretrained(model, adapter_name)
FastLanguageModel.for_inference(model)  # 启用原生2倍速推理


# Request handling with queue and batching
request_queue = queue.Queue()

def batch_inference():
    while True:
        batch = []
        events = []
        for _ in range(batch_size):
            try:
                req_data = request_queue.get(timeout=1)
            except queue.Empty:
                break
            batch.append(req_data['data'])
            events.append(req_data['event'])

        if batch:
            # 在这里加入padding=True和truncation=True
            inputs = tokenizer([alpaca_prompt.format('.', text, "") for text in batch],
                               return_tensors="pt",
                               padding=True,
                               truncation=True,
                               max_length=max_seq_length).to("cuda")
            outputs = model.generate(**inputs, max_new_tokens=64)
            responses = [tokenizer.decode(out, skip_special_tokens=True) for out in outputs]

            for response, event, input_text in zip(responses, events, batch):
                event.response = response
                event.num_input_tokens = len(tokenizer(input_text, return_tensors="pt").input_ids[0])
                event.num_output_tokens = len(tokenizer(response, return_tensors="pt").input_ids[0])
                event.set()



# Start batch processing thread
threading.Thread(target=batch_inference, daemon=True).start()

@app.route('/inference', methods=['POST'])
def inference():
    data = request.json
    input_text = data.get("input_text")
    if not input_text:
        return jsonify({"error": "Please provide input_text"}), 400

    event = threading.Event()
    request_queue.put({'data': input_text, 'event': event})
    event.wait()


    # Extract the response part
    response_start = "### Response:\n"
    response = event.response.split(response_start)[-1].strip()


    return jsonify({
        "generated_text": response,
        "num_output_tokens": event.num_output_tokens,
        "num_input_tokens": event.num_input_tokens
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
