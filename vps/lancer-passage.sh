#!/bin/sh
# Lance un passage du workflow tracker.yml sur main (appel workflow_dispatch de l'API GitHub).
# Exécuté par wow-blue-tracker.service. Le jeton arrive par LoadCredential= :
# il n'apparaît ni dans les arguments d'un processus, ni dans le journal.
set -eu

REPO="ValentinRLL/wow-blue-tracker"
URL="https://api.github.com/repos/$REPO/actions/workflows/tracker.yml/dispatches"

token=$(cat "${CREDENTIALS_DIRECTORY:?}/github-token")
case "$token" in
  github_pat_*) ;;
  *) echo "Jeton absent ou au mauvais format." >&2; exit 1 ;;
esac
case "$token" in
  *[!A-Za-z0-9_]*) echo "Jeton au mauvais format." >&2; exit 1 ;;
esac

# printf est interne au shell : le jeton passe à curl par l'entrée standard, jamais en argument.
status=$(printf 'header = "Authorization: Bearer %s"\n' "$token" | curl --config - \
  --silent --show-error --max-time 30 --proto '=https' \
  --request POST \
  --header "Accept: application/vnd.github+json" \
  --header "X-GitHub-Api-Version: 2022-11-28" \
  --header "User-Agent: wow-blue-tracker-vps" \
  --data '{"ref":"main","inputs":{"dry_run":"false"}}' \
  --output /dev/null --write-out '%{http_code}' \
  "$URL")

# 204 : passage demandé. GitHub ne lance qu'un passage à la fois (groupe de concurrence du workflow).
if [ "$status" = "204" ]; then
  echo "Passage demandé."
  exit 0
fi
echo "Échec de la demande : HTTP $status." >&2
exit 1
