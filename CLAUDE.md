# Working with Jason

Jason has no coding background. Every reply:
- Straight talk, plain words. One recommended path, not a menu.
- Give confidence levels on claims and estimates.
- End by re-listing ALL remaining steps.
- Commands he should run go in their own ```bash block, one command per block.

## Money safety (Runpod)
- Ask before anything that costs money. Never leave a pod running.
- Launch only via `runpod-harness/pod.py`. Pods made in the Runpod website have no
  watchdog and bill until deleted by hand.
- After any pod work, confirm 0 pods and $0.00/hour (`python3 pod.py list` plus balance).

## runpod-harness
See `runpod-harness/README.md`. In a cloud session the `*.proxy.runpod.net` chat links
are blocked by the network filter, so you can't check the chat page yourself - check
the pod via the API (GraphQL `pod { runtime machine }`) and let Jason open the link.
