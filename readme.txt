
jq -r '{id, signal_strength} | [.id, .signal_strength] | @tsv' ./labels/*.jsonl | sort -k1 -n > signal.tsv

