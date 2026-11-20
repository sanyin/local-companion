"""和 llama.cpp 说话。纯标准库，不引入任何依赖。

两个入口：
  complete()  —— 一次性拿完整回答（办正事、抽记忆、需要工具调用时用）
  stream()    —— 逐字吐出来（聊天用，让 11 秒的等待变成 1 秒的开始）
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8081"


class ModelUnavailable(RuntimeError):
    """模型服务没起来，或者连不上。"""


class LlamaClient:
    def __init__(self, base_url=DEFAULT_URL, timeout=180):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---------- 状态 ----------

    def health(self):
        try:
            with urlopen(f"{self.base_url}/health", timeout=2) as response:
                return json.loads(response.read() or b"{}")
        except Exception as exc:  # 连不上就是连不上，不抛
            return {"status": "unavailable", "error": str(exc)}

    def is_ready(self):
        return self.health().get("status") == "ok"

    # ---------- 请求体 ----------

    def _payload(self, messages, temperature, max_tokens, top_p, tools, stream, disable_thinking):
        body = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if disable_thinking:
            # Qwen3 混合推理模型：关掉思考链，否则闲聊时它会把预算全烧在脑内独白上。
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _open(self, payload, stream):
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"},
        )
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise ModelUnavailable(f"模型服务返回 {exc.code}：{detail}") from exc
        except URLError as exc:
            raise ModelUnavailable(f"连不上模型服务（{self.base_url}）：{exc.reason}") from exc

    # ---------- 一次性 ----------

    def complete(self, messages, temperature=0.4, max_tokens=800, top_p=0.9, tools=None, disable_thinking=True):
        payload = self._payload(messages, temperature, max_tokens, top_p, tools, False, disable_thinking)
        response = self._open(payload, stream=False)
        try:
            return json.loads(response.read() or b"{}")
        finally:
            response.close()

    # ---------- 流式 ----------

    def stream(self, messages, temperature=0.4, max_tokens=800, top_p=0.9, disable_thinking=True):
        """逐块产出 {'type': 'delta'|'done', ...}。

        思考内容（reasoning_content）不往外吐——那是她的内心，不是给主人看的东西。
        """
        payload = self._payload(messages, temperature, max_tokens, top_p, None, True, disable_thinking)
        response = self._open(payload, stream=True)
        finish_reason = None
        usage = {}
        try:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield {"type": "delta", "text": text}
        finally:
            response.close()
        yield {"type": "done", "finish_reason": finish_reason, "usage": usage}


def text_of(response):
    """从 complete() 的返回里取正文，取不到就返回空串。"""
    try:
        return (response["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def tool_calls_of(response):
    """从 complete() 的返回里取工具调用，取不到就返回空列表。"""
    try:
        calls = response["choices"][0]["message"].get("tool_calls") or []
    except (KeyError, IndexError, TypeError):
        return []
    parsed = []
    for call in calls:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        parsed.append({"id": call.get("id"), "name": function.get("name"), "arguments": arguments or {}})
    return parsed
