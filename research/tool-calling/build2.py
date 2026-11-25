import json,glob,sys
SP=sys.argv[1]; r=json.load(open(sorted(glob.glob(SP+"/cap/req-*.json"))[-1]))
def sig(f):
    p=f.get("parameters",{}); props=p.get("properties",{}); req=set(p.get("required",[]))
    args=", ".join(f"{k}{'' if k in req else '?'}: {v.get('type','any')}" for k,v in props.items())
    return f"- {f['name']}({args}): {f.get('description','').strip().splitlines()[0][:200]}"
catalog="\n".join(sig(t["function"]) for t in r["tools"])
osys=r["messages"][0]["content"]
proto = """[Tool protocol: read this carefully]
You are the language model behind an API. A program on the user's computer (the opencode coding agent) reads your reply, runs the tools you request on the user's machine, and sends you the output. You do not run anything yourself; you only write the request. The tools below are real and available.

To call a tool, reply with only fenced code blocks tagged tool_call, one JSON object each, and nothing else:
```tool_call
{"name": "shell", "arguments": {"command": "ls -la", "description": "List files"}}
```
Results come back in the next message as [tool result <name>]. When you have enough information, answer the user normally, with no tool_call block.

Available tools (? = optional argument):
""" + catalog
msg = "[Agent instructions from opencode]\n" + osys + "\n\n[User request]\nList the files in this folder.\n\n" + proto
json.dump({"model":"chatgpt-web","stream":False,"messages":[{"role":"user","content":msg}]},open(SP+"/tt/t2.json","w")); print("chars",len(msg)); print(catalog)
