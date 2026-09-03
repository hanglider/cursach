#!/bin/bash
# .claude/hooks/guard-bash.sh
INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if echo "$CMD" | grep -qE 'kind delete|kubectl delete (ns|namespace|node)|docker system prune'; then
  echo "Заблокировано: разрушение стенда только вручную. Если нужно пересоздать — скажи мне." >&2
  exit 2
fi
exit 0
