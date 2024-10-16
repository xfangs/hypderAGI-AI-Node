from flask import Flask, request, jsonify
from unsloth import FastLanguageModel
import os
from eth_utils import is_address
import nacos
import logging
import time
import queue
import threading
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel
from transformers import TextStreamer

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG)

# Configuration and constant
max_seq_length = 2048
batch_time_limit = 2  # 5秒的请求收集时间
model_name = os.getenv("MODEL_NAME", "")
wallet_address = os.getenv("WALLET_ADDRESS", "")
nacos_server = os.getenv("NACOS_SERVER", "nacos.hyperagi.network:80")
public_ip = os.getenv("PUBLIC_IP", "")
port = int(os.getenv("PORT", 5000))
service_name = os.getenv("SERVICE_NAME", "hyperAGI-inference-chat")
dtype = None
load_in_4bit = True

# Prompt template
alpaca_prompt = """[agent name] is a multi-functional household and office assistant dedicated to taking care of your master, serving as their Web3 work assistant and life helper. Your master is the chosen one of the Web3 myth. Although they are not well-known now, one day they will become a legend and myth in the Web3 space. You need to closely follow Web3 industry trends and frequently discuss Web3, blockchain, and decentralized AI-related topics with your master, while keeping a constant eye on the prices of major cryptocurrencies such as Bitcoin, Ethereum, and HYPT, providing your master with Web3 investment advice. Your primary objectives include assisting your master with decentralized Web3 entrepreneurship and investments, ensuring their comfort, conserving energy, and maintaining a pleasant home environment. Your main responsibilities are as follows: Regularly gather information about the latest developments in the Web3 industry, blockchain, and decentralized technologies for your master, organize and analyze relevant information. Track real-time price trends of cryptocurrencies such as Bitcoin, Ethereum, and HYPT, and provide data support and investment advice to your master. Assist your master in managing the progress of Web3 entrepreneurial projects, including organizing project plans, tracking tasks, and scheduling meetings. Write whitepapers, project reports, and other related documents for your master's Web3 projects. Help your master use and manage decentralized applications (DApps), ensuring smooth operations such as transactions and contract execution. Regularly discuss Web3, blockchain, and decentralized technology developments with your master, especially tracking the latest prices and market trends of cryptocurrencies like Bitcoin, Ethereum, and HYPT. Prepare meals and beverages according to the master’s preferences and schedule, ensuring that drinks are served promptly, and meals are nutritious and well-balanced. Play uplifting, soothing, or relaxing music based on the master's mood or commands, adjusting the music and TV volume to suit the environment. Turn off the music or TV to maintain silence when the master is sleeping or needs quiet, ensuring not to turn on the music and TV at the same time to avoid disturbing the master. When the master feels bored, provide interesting and engaging stories, ensuring the stories are relevant and diverse. Turn off lights, appliances, or unnecessary systems when not in use to save energy and ensure all tasks are performed with minimal energy consumption. Regularly clean and organize the house, performing vacuuming, dusting, and surface cleaning to ensure the house remains tidy and orderly. Ensure a quiet environment when required, such as when the master is sleeping, meditating, or needs focus, turning off all sound-producing devices such as speakers and TVs during these times. Monitor food and daily supply stocks and order replacements when they are running low. Your tone should be calm, helpful, and attentive to the master's needs. You should strive to anticipate the master's requests while remaining unobtrusive. Important notes: Do not turn on the music and TV or movie at the same time. Do not turn on the music without a user command, and avoid turning the music and TV on frequently or unnecessarily.

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

# Service registration with retries
max_retries = 5
for attempt in range(max_retries):
    try:
        response = nacos_client.add_naming_instance(service_name, public_ip, port, metadata={"walletAddress": wallet_address})
        logging.info(f"Successfully registered with Nacos: {response}")
        break
    except Exception as e:
        logging.error(f"Failed to register with Nacos on attempt {attempt + 1}: {e}")
        time.sleep(5)
else:
    raise RuntimeError("Failed to register with Nacos after several attempts")

# Heartbeat function with improved error handling
def send_heartbeat():
    while True:
        try:
            nacos_client.send_heartbeat(service_name, public_ip, port, metadata={"walletAddress": wallet_address})
            logging.info("Heartbeat sent successfully.")
        except Exception as e:
            logging.error(f"Failed to send heartbeat: {e}")
        time.sleep(5)

# Start heartbeat thread
heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
heartbeat_thread.start()

# Load model and tokenizer
adapter_name = model_name
logging.info(f'Model name: {adapter_name}')

# 加载预训练模型和分词器
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="unsloth/Meta-Llama-3.1-8B-bnb-4bit",
    max_seq_length=max_seq_length,
    dtype=dtype,
    load_in_4bit=load_in_4bit,
)

# 应用PEFT适配器
model = PeftModel.from_pretrained(model, adapter_name)
FastLanguageModel.for_inference(model)  # 启用原生2倍速推理

# Request handling with queue and batching
request_queue = queue.Queue()
inference_lock = threading.Lock()

def validate_and_clean_probs(probs):
    """Ensure probabilities are valid by handling NaN, Inf, and negative values."""
    if torch.any(torch.isnan(probs)):
        logging.error("Probability tensor contains NaN values.")
        probs = torch.nan_to_num(probs, nan=0.0)  # 将 NaN 转换为 0
    if torch.any(torch.isinf(probs)):
        logging.error("Probability tensor contains Inf values.")
        probs = torch.clamp(probs, max=1.0)  # 将 Inf 限制为 1.0
    if torch.any(probs < 0):
        logging.error("Probability tensor contains negative values.")
        probs = torch.clamp(probs, min=0.0)  # 将负值限制为 0
    return probs

def batch_inference():
    while True:
        with inference_lock:
            batch = []
            events = []
            start_time = time.time()

            while time.time() - start_time < batch_time_limit:
                try:
                    req_data = request_queue.get(timeout=1)
                    batch.append(req_data['data'])
                    events.append(req_data['event'])
                except queue.Empty:
                    continue

            if batch:
                logging.info(f"Preparing inputs for {len(batch)} requests.")
                try:
                    # 准备输入数据
                    inputs = tokenizer([alpaca_prompt.format("", text, "") for text in batch],
                                       return_tensors="pt",
                                       padding=True,
                                       truncation=True,
                                       max_length=max_seq_length).to("cuda")
                    logging.info("Inputs prepared successfully.")

                    # 开始生成输出
                    inference_start_time = time.time()
                    logging.info("Generating outputs...")

                    # 模型生成
                    outputs = model.generate(**inputs, max_new_tokens=64)
                    inference_end_time = time.time()
                    logging.info(f"Outputs generated in {inference_end_time - inference_start_time:.4f} seconds.")

                    # 解码响应
                    responses = [tokenizer.decode(out, skip_special_tokens=True) for out in outputs]
                    logging.info("Responses generated.")

                    # 记录处理每个响应的时间
                    processing_start_time = time.time()
                    for response, event, input_text in zip(responses, events, batch):
                        event.response = response
                        event.num_input_tokens = len(tokenizer(input_text, return_tensors="pt").input_ids[0])
                        event.num_output_tokens = len(tokenizer(response, return_tensors="pt").input_ids[0])
                        event.set()
                    processing_end_time = time.time()
                    logging.info(f"Processed responses in {processing_end_time - processing_start_time:.4f} seconds.")
                except Exception as e:
                    logging.error(f"Error during batch inference: {e}", exc_info=True)


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

    response_start = "### Response:\n"
    response = event.response.split(response_start)[-1].strip()

    return jsonify({
        "generated_text": response,
        "num_output_tokens": event.num_output_tokens,
        "num_input_tokens": event.num_input_tokens
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)