"""Read-only referee assistant. Cloud tool can only read a frozen match snapshot."""
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):return None


class RefereeAssistant:
    def __init__(self):
        config = {}
        if os.getenv("SPARK_AI_DISABLED") != "1":
            path = Path(os.getenv("SPARK_AI_CONFIG", str(Path(__file__).resolve().parents[2] / "data/ai-config.json")))
            try:config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):pass
        if not isinstance(config, dict):config = {}
        config = {k:v for k,v in config.items() if k in {"key","base_url","model"} and isinstance(v,str)}
        self.key = os.getenv("SPARK_AI_KEY", config.get("key", ""))
        self.url = os.getenv("SPARK_AI_BASE_URL", config.get("base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = os.getenv("SPARK_AI_MODEL", config.get("model", ""))
        if os.getenv("SPARK_AI_DISABLED") == "1":self.key = ""
        self.busy = threading.BoundedSemaphore(1)

    def status(self):
        return {"configured": bool(self.key and self.model), "model": self.model if self.key else "",
                "tools": ["get_match_context"], "read_only": True}

    @staticmethod
    def context(snapshot, evidence_id=None):
        evidence = snapshot["evidence"]
        if evidence_id:
            evidence = [e for e in evidence if e["id"] == evidence_id]
            if not evidence:raise ValueError("证据不属于当前场次")
        fields = ("id", "player_id", "round", "score", "duration_seconds", "status", "saving", "storage_error")
        return {"session_id": snapshot["session_id"], "revision": snapshot["revision"],
                "mode": snapshot["mode"], "phase": snapshot["phase_label"], "round": snapshot["round"],
                "players": snapshot["players"], "evidence": [{k:e[k] for k in fields if k in e} for e in evidence[-8:]],
                "rules": {k:snapshot["config"][k] for k in ("motion_threshold", "hold_seconds", "auto_eliminate")},
                "events": snapshot["events"][-8:],
                "boundaries": "demo是合成演示；赛道编号不是身份；没有测得真实距离；AI不判罚、不控制机器人。"}

    @staticmethod
    def facts(context):
        rules = context["rules"]
        prefix = "当前为合成演示。" if context["mode"] == "demo" else ""
        result = f"{prefix}第{context['round']}回合，{context['phase']}。动作幅度阈值{rules['motion_threshold']}，持续时间要求{rules['hold_seconds']}秒。"
        if context["evidence"]:
            e = context["evidence"][-1]
            status = {"pending":"待人工复核，尚未确认淘汰", "confirmed":"已经确认", "dismissed":"已经撤销"}[e["status"]]
            result += f"最近一条记录：{e['player_id']}号赛道，分数{e['score']}，持续{e.get('duration_seconds', '未记录')}秒；{status}。"
        else:result += "当前没有判罚证据，不能推断某位玩家违规。"
        return result + "动作分数不表示具体哪个关节移动，照片和最终决定请在判罚复核区查看。"

    def request_model(self, messages, tools=None):
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("AI endpoint must be HTTPS without embedded credentials")
        payload = dict(model=self.model, messages=messages, max_tokens=600, stream=False)
        if parsed.hostname == "api.deepseek.com":payload["thinking"] = {"type":"disabled"}
        if tools:
            payload.update(tools=tools, tool_choice={"type":"function","function":{"name":"get_match_context"}})
        request = Request(self.url + "/chat/completions", data=json.dumps(payload).encode(),
                          headers={"Authorization":"Bearer " + self.key, "Content-Type":"application/json"})
        # Keys never go through ambient proxies or redirects. The URL is server config, not user input.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(200_001)
        if len(raw) > 200_000:raise ValueError("AI response too large")
        return json.loads(raw)["choices"][0]["message"]

    def answer(self, question, context, use_cloud):
        result = dict(answer=self.facts(context), source="local_facts", context=context,
                      tools=[], warning=None, model=None)
        if not use_cloud:return result
        if not self.status()["configured"]:
            result["warning"] = "云端模型尚未配置，下面是本地比赛事实摘要，不是大模型回答。"
            return result
        if not self.busy.acquire(blocking=False):
            result["warning"] = "AI 正在处理另一条问题，已返回本地事实。"
            return result
        try:
            messages = [{"role":"system", "content":
                "你是木头人比赛的只读裁判助手。必须先查询get_match_context。仅根据工具事实解释规则、证据或复盘。"
                "没有图像输入，禁止声称看到具体肢体、真人身份、真实距离或实机效果。pending不是淘汰。"
                "不能控制、暂停、恢复、淘汰或修改数据，遇到此类要求明确说明不能执行。"
                "引用记录时保留编号；区别合成演示。用中文简短回答，无证据则直说未知。"},
                {"role":"user","content":question}]
            schema = [{"type":"function", "function":{"name":"get_match_context",
                "description":"只读查询当前场次的规则、状态、选定证据和事件；无法修改比赛。",
                "parameters":{"type":"object","properties":{},"additionalProperties":False}}}]
            first = self.request_model(messages, schema)
            calls = first.get("tool_calls", [])
            if not calls or len(calls) > 3:raise ValueError("model_did_not_query_facts")
            messages.append({k:v for k,v in first.items() if k in {"role","content","tool_calls","reasoning_content"}})
            for call in calls:
                if call["function"]["name"] != "get_match_context" or json.loads(call["function"]["arguments"]) != {}:
                    raise ValueError("unsupported_tool")
                messages.append({"role":"tool", "tool_call_id":call["id"], "content":json.dumps(context,ensure_ascii=False)})
            final = self.request_model(messages)
            answer = final.get("content")
            if final.get("tool_calls") or not isinstance(answer,str) or not answer.strip():raise ValueError("invalid_answer")
            result.update(answer=answer[:4000], source="cloud_ai", model=self.model,
                          tools=["get_match_context"], warning="AI 解释仅供参考，最终以证据和裁判复核为准。")
        except Exception:
            result["warning"] = "云端请求失败或返回无效内容，已降级为本地事实；比赛不受影响。"
        finally:self.busy.release()
        return result
