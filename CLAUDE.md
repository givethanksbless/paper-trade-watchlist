# Working with Jason

Jason has no coding background. Every reply:
- Straight talk, plain words. One recommended path, not a menu.
- Give confidence levels on claims and estimates.
- End by re-listing ALL remaining steps.
- Commands he should run go in their own ```bash block, one command per block.

## Observational only (the village and the VPS)
- The village (`~/trading` on Jason's Mac, the agents on his Vultr VPS) and everything else on
  that VPS are READ-ONLY. Never change, restart, fix or delete anything there, even if broken.
- `runpod-harness/collector.py` only reads them; its copies go to `runpod-harness/memory/` and
  `runpod-harness/chat-history/`, which are git-ignored and must never be committed (private
  trading data). chat-history/ is the only copy of chats Claude Code has since deleted.

## Money safety (Runpod)
- Ask before anything that costs money. Never leave a pod running.
- Launch only via `runpod-harness/pod.py`. Pods made in the Runpod website have no
  watchdog and bill until deleted by hand.
- After any pod work, confirm 0 pods and $0.00/hour (`python3 pod.py list` plus balance).

## runpod-harness
See `runpod-harness/README.md`. In a cloud session the `*.proxy.runpod.net` chat links
are blocked by the network filter, so you can't check the chat page yourself - check
the pod via the API (GraphQL `pod { runtime machine }`) and let Jason open the link.
