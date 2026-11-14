#!/usr/bin/env bash
# Lance une commande ytclips à partir des saisies du workflow GitHub (variables IN_*).
# Les journaux d'un dépôt public sont publics : les liens de téléchargement signés y sont masqués.
set -euo pipefail
cd "$(dirname "$0")/.."

YTCLIPS="${YTCLIPS:-.venv/bin/ytclips}"
action="${IN_ACTION:-check}"
args=()

add() {  # add --option "valeur" : ajoute l'option seulement si la valeur n'est pas vide
  if [ -n "${2:-}" ]; then args+=("$1" "$2"); fi
}

case "$action" in
  check)
    args=(check)
    ;;
  transcribe)
    args=(transcribe)
    add --source "${IN_SOURCE:-}"
    add --language "${IN_LANGUAGE:-}"
    ;;
  montage-plan)
    args=(montage plan)
    add --source "${IN_SOURCE:-}"
    add --title "${IN_TITLE:-}"
    add --segments "${IN_SEGMENTS:-}"
    add --zoom "${IN_ZOOM:-}"
    if [ "${IN_DRY_RUN:-true}" = "true" ]; then args+=(--dry-run); fi
    ;;
  montage-list)
    args=(montage list)
    ;;
  montage-send)
    args=(montage send)
    if [ -n "${IN_MONTAGE_ID:-}" ]; then args+=("$IN_MONTAGE_ID"); fi
    # La confirmation des droits n'est transmise que si la case a été cochée par la personne qui lance.
    if [ "${IN_RIGHTS:-false}" = "true" ]; then args+=(--rights-confirmed); fi
    if [ "${IN_DRY_RUN:-true}" = "true" ]; then args+=(--dry-run); fi
    ;;
  tiktok-list)
    args=(tiktok list)
    ;;
  history)
    args=(history --remote)
    ;;
  *)
    echo "Commande inconnue : $action" >&2
    exit 2
    ;;
esac

echo "ytclips ${args[0]} ${args[1]:-} (simulation : ${IN_DRY_RUN:-sans objet})"
# Tout lien signé (accès direct à un fichier du bucket) est remplacé avant d'atteindre le journal.
set +e
"$YTCLIPS" "${args[@]}" 2>&1 | sed -E 's#https?://[^[:space:]]*X-Amz-[^[:space:]]*#[lien signé masqué]#g'
status=${PIPESTATUS[0]}
set -e
exit "$status"
