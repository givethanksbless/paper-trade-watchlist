# Runpod private-chat harness

Open WebUI + Ollama on one 48 GB GPU (A6000 or A40 at ~$0.33-0.35/hr; L40, RTX 6000 Ada or
L40S at ~$0.69-0.79/hr if those are taken), with a watchdog that deletes the pod when idle
or past a time cap. Needs `RUNPOD_API_KEY`.

Model: `huihui_ai/qwen3.5-abliterated:27b-Claude-q8_0` (27B, 8-bit, 30 GB download). To
change it, edit `MODEL` in pod.py and make sure `GPU_TYPES` has enough memory for it.

```bash
python3 pod.py launch --idle-min 30 --max-hours 2     # COSTS MONEY, asks first
python3 pod.py list                                   # show pods, GPU and $/hr
python3 pod.py terminate-all                          # emergency stop: delete every pod
python3 pod.py collect                                # dry run of the village feed (free)
```

`launch` does everything in one window: refuses if a pod already exists, retries once a
minute while no GPU is free (failed tries cost nothing), prints the chat link and GPU,
then runs `guard` until the pod is gone. Closing the window is safe - the pod still
deletes itself.

Only launch through `pod.py`. Pods made in the Runpod website have no watchdog and
bill until deleted by hand (website: ⋮ → Terminate, not Stop).

## Safety layers
1. `watchdog.py` (inside the pod): deletes it `--idle-min` minutes after the model
   unloads (~5 min after the last message), or at `--max-hours`.
2. Backup timer (inside the pod): deletes it 10 min after the cap if the watchdog died.
3. `guard` (on your computer): deletes a pod whose container hasn't started after
   15 min (layers 1-2 never run then) or that is 15 min past the cap.

## Profile
`profile.md` is packed into every pod. The watchdog creates a `private-chat` model with
it as the system prompt and a 32K context; the chat selects it by default.
Edit the file any time; the next pod uses the new version. It does not keep chat history.

Both scripts send a custom User-Agent: rest.runpod.io returns 403 to Python's default one.

## Fixed login and the village feed
Set once on your computer (saved in ~/.zshrc like the API key): `CHAT_EMAIL` and
`CHAT_PASSWORD`. With them, every pod:
- gets that login as its only account (Open WebUI's `WEBUI_ADMIN_*` settings; sign-up is off).
  The values are visible in the pod's settings in your Runpod account, so use a password you
  don't use anywhere else.
- is fed the village at launch: `collector.py` reads `~/trading` and that folder's Claude Code
  chat history (read-only), strips secrets, and writes `memory/`; `feeder.py` then loads
  `memory/briefing.md` (trader docs + last 3 days) into `private-chat` and uploads
  `memory/archive/` as the "Village" knowledge base - type `#Village` in a chat to search it.

The collector never reads files whose names look like secrets (.env, *secret*, *.pem, *.b64,
...) and redacts key-like text (API keys, bot tokens, wallet keys, `password=...`) in what it
copies. `memory/` is git-ignored. Run `python3 pod.py collect` to see exactly what would be sent.
