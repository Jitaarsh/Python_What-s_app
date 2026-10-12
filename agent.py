import asyncio
import re
import time
import httpx
from dotenv import load_dotenv
import os

load_dotenv()
GROQ_API_KEY = (os.getenv("Groak_api") or "").strip()
GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_TURNS=10
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
HISTORY_TTL=60*60
SYSTEM_PROMPT = "You are a helpful assistant, Your Boss is 'Jitaarsh' and you are working for him. " \
"You are a WhatsApp bot that can answer questions about the company, " \
"its products, and services. You can also provide information about the company's policies," \
" procedures, and other relevant topics. " \
"You should always be polite and professional in your responses. " \
"If you don't know the answer to a question, you should say 'I'm sorry, I don't know the answer to that question.'"

_history={}
_locks={}

async def ask_llm(promt):
    payload={
        "model":GROQ_MODEL,
        "messages":[{"role":"system", "content":SYSTEM_PROMPT}]+promt,
        "temperature": 0.65,
        "max_tokens":700,
        }
    headers={"Authorization":f"Bearer {GROQ_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(2):
                r= await client.post(GROQ_URL,json=payload,headers=headers)
                if r.status_code == 429 and attempt ==0:
                    await asyncio.sleep(3)
                    continue
                break
            if r.status_code !=200:
                print("ERROR WITH TEH GROQ !", r.status_code, r.text)
                return None
        text = r.json()["choices"][0]["message"]["content"] or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        return text[:4000] or None
    except Exception as e:
        print(f'THE GROQ CONNECTION FAILED DUE TO : - {e}')
        return None
    
async def get_reply(user, text):
    lock = _locks.setdefault(user,asyncio.Lock())
    async with lock:
        if text.strip().lower() == "/reset":
            _history.pop(user,None)
            return "Momory Cleared"

        now= time.time()
        h=_history.get(user)
        if not h or now - h["ts"] > HISTORY_TTL:
            h = {"msgs": [], "ts": now}
            _history[user] = h
        h["msgs"].append({"role": "user", "content": text})
        h["msgs"] = h["msgs"][-MAX_TURNS * 2:]
        while h["msgs"] and h["msgs"][0]["role"] != "user":
            h["msgs"].pop(0)
        reply = await ask_llm(h["msgs"])
        if reply:
            h["msgs"].append({"role": "assistant", "content": reply})
            h["ts"] = time.time()
            return reply

        h["msgs"].pop()   # drop the unanswered message
        return "Trouble right now, try again in a bit."

