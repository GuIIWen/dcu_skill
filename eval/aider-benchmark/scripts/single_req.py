#!/usr/bin/env python3
"""Single streaming request with a progress log: one line every 15 s with chunks/chars so decode speed can be read off."""
import json, sys, threading, time, urllib.request

REQ, URL, OUT, TIMEOUT = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
EFFORT = sys.argv[5] if len(sys.argv) > 5 else None

req = json.load(open(REQ))
req["chat_template_kwargs"] = {"thinking": True}
if EFFORT:
    req["reasoning_effort"] = EFFORT
req["stream"] = True
req["stream_options"] = {"include_usage": True}
req.pop("max_tokens", None)
json.dump(req, open(OUT + ".req.json", "w"), ensure_ascii=False)

st = {"chunks": 0, "rchars": 0, "cchars": 0, "first": None, "done": False}
t0 = time.time()
prog = open(OUT + ".progress.tsv", "w")
prog.write("t_s\tchunks\treasoning_chars\tcontent_chars\n")

def sampler():
    while not st["done"]:
        time.sleep(15)
        prog.write("%.0f\t%d\t%d\t%d\n" % (time.time() - t0, st["chunks"], st["rchars"], st["cchars"]))
        prog.flush()

threading.Thread(target=sampler, daemon=True).start()
finish = usage = None
err = None
reason = open(OUT + ".reasoning.txt", "w")
content = open(OUT + ".content.txt", "w")
try:
    r = urllib.request.urlopen(urllib.request.Request(URL, json.dumps(req).encode(), {"Content-Type": "application/json"}), timeout=TIMEOUT)
    for raw in r:
        if time.time() - t0 > TIMEOUT:
            err = "client timeout %ds" % TIMEOUT
            break
        line = raw.decode().strip()
        if not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if d == "[DONE]":
            break
        j = json.loads(d)
        usage = j.get("usage") or usage
        for ch in j.get("choices", []):
            dl = ch.get("delta", {})
            if dl.get("reasoning_content"):
                if st["first"] is None:
                    st["first"] = time.time() - t0
                st["rchars"] += len(dl["reasoning_content"]); reason.write(dl["reasoning_content"]); reason.flush()
            if dl.get("content"):
                if st["first"] is None:
                    st["first"] = time.time() - t0
                st["cchars"] += len(dl["content"]); content.write(dl["content"]); content.flush()
            finish = ch.get("finish_reason") or finish
        st["chunks"] += 1
except Exception as e:
    err = repr(e)
st["done"] = True
el = time.time() - t0
prog.write("%.0f\t%d\t%d\t%d\t(end)\n" % (el, st["chunks"], st["rchars"], st["cchars"]))
prog.close()
summary = dict(finish_reason=finish, error=err, elapsed_s=round(el, 1), first_chunk_s=st["first"], chunks=st["chunks"],
               reasoning_chars=st["rchars"], content_chars=st["cchars"], usage=usage)
json.dump(summary, open(OUT + ".summary.json", "w"), indent=1)
print(json.dumps(summary))
