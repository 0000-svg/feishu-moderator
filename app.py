"""
飞书群聊主持人智能体 - 主服务
Flask 应用，接收飞书事件回调
"""
import json
import logging
import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ============ 配置 ============
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFY_TOKEN = os.getenv("FEISHU_VERIFY_TOKEN", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
PORT = int(os.getenv("PORT", 8080))

# ============ 飞书 API ============
import requests
import time

class FeishuAPI:
    def __init__(self):
        self.tenant_access_token = None
        self.token_expire_time = 0
    
    def get_tenant_access_token(self):
        if self.tenant_access_token and time.time() < self.token_expire_time:
            return self.tenant_access_token
        
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        data = {
            "app_id": FEISHU_APP_ID,
            "app_secret": FEISHU_APP_SECRET
        }
        response = requests.post(url, json=data)
        result = response.json()
        
        if result.get("code") == 0:
            self.tenant_access_token = result["tenant_access_token"]
            self.token_expire_time = time.time() + 7000
            return self.tenant_access_token
        raise Exception(f"获取token失败: {result}")
    
    def send_text_message(self, chat_id, text):
        token = self.get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"receive_id_type": "chat_id"}
        data = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }
        response = requests.post(url, headers=headers, params=params, json=data)
        return response.json()
    
    def get_chat_members(self, chat_id):
        token = self.get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"member_id_type": "open_id", "page_size": 100}
        response = requests.get(url, headers=headers, params=params)
        result = response.json()
        if result.get("code") == 0:
            return result["data"].get("items", [])
        return []

feishu_api = FeishuAPI()

# ============ DeepSeek 服务 ============
from openai import OpenAI

def get_llm_response(question, context=""):
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1"
    )
    
    system_prompt = """你是一个引导式答疑助手，在群聊中帮助成员解答问题。
你的特点：
1. 不会直接给出答案，而是通过提问引导对方思考
2. 善于拆解复杂问题，帮助对方理清思路
3. 语言简洁友好，像朋友一样交流"""
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"问题：{question}\n上下文：{context}"}
        ],
        max_tokens=500
    )
    return response.choices[0].message.content

def summarize_discussion(messages, topic=""):
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1"
    )
    
    system_prompt = """你是一个会议记录助手，负责总结群聊讨论内容。

输出格式：
## 📋 讨论总结

### 讨论主题
[一句话概括]

### 主要观点
- [观点1]
- [观点2]

### 行动项
[如需要后续行动，列出]"""
    
    messages_text = "\n".join([f"- {m.get('speaker', '某人')}: {m.get('content', '')}" for m in messages])
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"讨论记录：\n{messages_text}"}
        ],
        max_tokens=800
    )
    return response.choices[0].message.content

# ============ 主持人状态管理 ============
import random

discussions = {}  # chat_id -> {speakers, order, current_index, messages}

def get_or_create_discussion(chat_id):
    if chat_id not in discussions:
        discussions[chat_id] = {
            "speakers": [],
            "order": [],
            "current_index": 0,
            "messages": []
        }
    return discussions[chat_id]

def generate_speaking_order(chat_id):
    discussion = get_or_create_discussion(chat_id)
    members = feishu_api.get_chat_members(chat_id)
    
    valid_members = [
        {"open_id": m["member_id"], "name": m.get("name", "成员")}
        for m in members
    ]
    random.shuffle(valid_members)
    
    discussion["speakers"] = valid_members
    discussion["order"] = [m["open_id"] for m in valid_members]
    discussion["current_index"] = 0
    
    return valid_members

def get_current_speaker(chat_id):
    discussion = get_or_create_discussion(chat_id)
    if not discussion["order"] or discussion["current_index"] >= len(discussion["order"]):
        return None
    current_open_id = discussion["order"][discussion["current_index"]]
    for speaker in discussion["speakers"]:
        if speaker["open_id"] == current_open_id:
            return speaker
    return {"open_id": current_open_id, "name": "成员"}

def next_speaker(chat_id):
    discussion = get_or_create_discussion(chat_id)
    if discussion["current_index"] < len(discussion["order"]) - 1:
        discussion["current_index"] += 1
        return get_current_speaker(chat_id)
    return None

def record_message(chat_id, open_id, name, content):
    discussion = get_or_create_discussion(chat_id)
    discussion["messages"].append({
        "open_id": open_id,
        "speaker": name,
        "content": content,
        "timestamp": time.time()
    })

# ============ 事件处理 ============

def handle_url_verification(data):
    challenge = data.get("challenge", "")
    return jsonify({"challenge": challenge})

def handle_message_event(event):
    try:
        message = event.get("message", {})
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type")
        content = message.get("content", "{}")
        sender = event.get("sender", {})
        
        if chat_type != "group":
            return
        
        content_data = json.loads(content)
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "unknown")
        text_content = content_data.get("text", "")
        
        record_message(chat_id, open_id, "成员", text_content)
        
        # 检测命令
        if "开始讨论" in text_content:
            members = generate_speaking_order(chat_id)
            if members:
                first = get_current_speaker(chat_id)
                feishu_api.send_text_message(
                    chat_id,
                    f"🎯 讨论开始！\n\n共 {len(members)} 位成员参与。\n\n请第一位发言者 **{first['name']}** 开始发言！"
                )
        
        elif "总结" in text_content:
            discussion = get_or_create_discussion(chat_id)
            if discussion["messages"]:
                summary = summarize_discussion(discussion["messages"])
                feishu_api.send_text_message(chat_id, summary)
        
        elif "下一位" in text_content:
            next_s = next_speaker(chat_id)
            if next_s:
                feishu_api.send_text_message(chat_id, f"📢 请 **{next_s['name']}** 发言！")
            else:
                feishu_api.send_text_message(chat_id, "🎉 所有人已发言完毕！")
        
        # 被@时回答问题
        mentions = message.get("mentions", [])
        if mentions:
            question = text_content.replace("@_user", "").strip()
            if question:
                response = get_llm_response(question)
                feishu_api.send_text_message(chat_id, response)
    
    except Exception as e:
        logger.error(f"处理消息失败: {e}", exc_info=True)

# ============ 路由 ============

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    event_type = data.get("type", "")
    
    logger.info(f"收到事件: {event_type}")
    
    if event_type == "url_verification":
        return handle_url_verification(data)
    
    if event_type == "event_callback":
        event = data.get("event", {})
        if event.get("type") == "message":
            handle_message_event(event)
    
    return jsonify({"code": 0})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    logger.info(f"🚀 服务启动，端口: {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
