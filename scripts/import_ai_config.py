"""Import only text-AI settings from an explicitly supplied Project7 .env file.

Never prints the key. Destination is private data/ and excluded from source packs.
"""
import argparse
import json
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    allowed = {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DASHSCOPE_API_KEY", "AI_TEXT_MODEL", "AI_TEXT_PROVIDER"}
    values = {}
    for line in args.source.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() in allowed:
            value = value.strip()
            if len(value)>1 and value[0] == value[-1] and value[0] in "\"'":value=value[1:-1]
            values[name.strip()] = value
    provider = values.get("AI_TEXT_PROVIDER", "auto")
    if provider in {"auto","deepseek"} and values.get("DEEPSEEK_API_KEY"):
        config = dict(key=values["DEEPSEEK_API_KEY"], base_url=values.get("DEEPSEEK_BASE_URL","https://api.deepseek.com"), model=values.get("DEEPSEEK_MODEL","deepseek-v4-flash"))
    elif provider in {"auto","dashscope"} and values.get("DASHSCOPE_API_KEY"):
        config = dict(key=values["DASHSCOPE_API_KEY"], base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model=values.get("AI_TEXT_MODEL",""))
    else:raise SystemExit("No configured text-AI provider was found; nothing imported")
    if not config["model"]:raise SystemExit("Model name is missing; nothing imported")
    destination = Path(__file__).resolve().parents[1] / "data/ai-config.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Imported private text-AI configuration. Key not displayed. Model:", config["model"])
