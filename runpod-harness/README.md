# Runpod private-chat harness

Open WebUI + Ollama on one 24 GB GPU, with a watchdog that deletes the pod when idle
or past a time cap. Needs `RUNPOD_API_KEY` in the environment.

```bash
python3 pod.py list                                   # show pods and $/hr
python3 pod.py launch --idle-min 30 --max-hours 1     # COSTS MONEY, asks first
python3 pod.py terminate-all                          # delete every pod
```

Only launch through `pod.py`. Pods made in the Runpod website have no watchdog and
bill until deleted by hand (website: ⋮ → Terminate, not Stop).

Both scripts send a custom User-Agent: rest.runpod.io returns 403 to Python's default one.
