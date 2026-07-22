#!/usr/bin/env bash
# Emits an emoji-annotated Markdown changelog for GitHub Releases, one bullet per
# commit since the given tag (all history, capped at 50, if no tag is given — e.g.
# the very first release). Classification is a simple keyword heuristic on the
# commit subject, not Conventional Commits — this repo's commit messages are plain
# descriptive sentences ("Add X", "Fix Y", "Split Z into..."), not prefixed types.
#   scripts/release_notes.sh [last_tag]
set -u
last_tag="${1:-}"

if [ -n "$last_tag" ]; then
  range="${last_tag}..HEAD"
else
  range="-50"
fi

classify() {
  local subject="$1"
  local first_word
  first_word="$(printf '%s' "$subject" | awk '{print tolower($1)}')"
  case "$first_word" in
    add|create|introduce) echo "✨" ;;
    fix|correct|resolve)  echo "🐛" ;;
    remove|delete|drop)   echo "🗑️" ;;
    split|refactor|extract|consolidate|merge|restructure|reorganize) echo "♻️" ;;
    document|docs)        echo "📝" ;;
    security|harden)      echo "🔒" ;;
    *)
      case "$(printf '%s' "$subject" | tr '[:upper:]' '[:lower:]')" in
        *style*|*redesign*|*design*|*polish*) echo "🎨" ;;
        *perf*|*speed*|*cache*|*optimi*)      echo "⚡" ;;
        *test*)                                echo "✅" ;;
        *ci*|*workflow*)                       echo "👷" ;;
        *) echo "🔧" ;;
      esac
      ;;
  esac
}

if [ -n "$last_tag" ]; then
  commits="$(git log --pretty=format:%s "$range" 2>/dev/null)"
else
  commits="$(git log --pretty=format:%s $range 2>/dev/null)"
fi

if [ -z "$commits" ]; then
  echo "_Нет изменений с предыдущего релиза._"
  exit 0
fi

while IFS= read -r subject; do
  [ -z "$subject" ] && continue
  emoji="$(classify "$subject")"
  echo "- ${emoji} ${subject}"
done <<< "$commits"
