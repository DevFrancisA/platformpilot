"""python -m platformpilot [serve|eval] [--mode offline|llm]"""
import argparse
import json

import uvicorn

p = argparse.ArgumentParser(prog="platformpilot")
p.add_argument("cmd", nargs="?", default="serve", choices=["serve", "eval"])
p.add_argument("--mode", choices=["offline", "llm"], default=None)
p.add_argument("--port", type=int, default=8000)
a = p.parse_args()

if a.cmd == "eval":
    from .runner import run_eval
    print(json.dumps(run_eval(a.mode)["summary"], indent=2))
else:
    print(f"PlatformPilot UI -> http://127.0.0.1:{a.port}")
    uvicorn.run("platformpilot.server:app", host="127.0.0.1", port=a.port, log_level="warning")
