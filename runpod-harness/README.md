# Runpod private-chat harness

Open WebUI + Ollama on one 24 GB GPU (A5000, 3090 or 4090, whichever is free), with a
watchdog that deletes the pod when idle or past a time cap. Needs `RUNPOD_API_KEY`.

```bash
python3 pod.py launch --idle-min 30 --max-hours 2     # COSTS MONEY, asks first
python3 pod.py list                                   # show pods, GPU and $/hr
python3 pod.py terminate-all                          # emergency stop: delete every pod
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
it as the system prompt - pick that model in the chat to get answers that know you.
Edit the file any time; the next pod uses the new version. It does not keep chat history.

Both scripts send a custom User-Agent: rest.runpod.io returns 403 to Python's default one.
