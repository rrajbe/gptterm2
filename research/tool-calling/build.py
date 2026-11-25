import json,glob,sys
SP=sys.argv[1]; r=json.load(open(sorted(glob.glob(SP+"/cap/req-*.json"))[-1]))
tools=[t["function"] for t in r["tools"]]
fmt = """You are running inside a coding tool that can execute tools for you on the user's computer.
To use a tool, reply with ONLY one fenced code block whose language tag is tool_call, containing a JSON object:
```tool_call
{"name": "<tool name>", "arguments": { ... }}
```
You may put several such blocks in one reply to call several tools. Do not write anything after the blocks.
The tool results will come back in the next message, starting with [tool result]. When you have what you need, answer normally with no tool_call block.
Only use tools from this list (JSON Schema for the arguments):
""" + json.dumps(tools, separators=(",",":"))
system = fmt + "\n\n" + r["messages"][0]["content"]
body={"model":"chatgpt-web","stream":False,"messages":[{"role":"system","content":system},{"role":"user","content":"List the files in this folder."}]}
json.dump(body,open(SP+"/tt/t1.json","w")); print("system chars",len(system))
