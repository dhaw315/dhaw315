# ytclips — clips verticaux et envoi privé sur YouTube

Programme Python qui découpe un extrait de 20 à 60 secondes dans une vidéo que vous fournissez,
le met au format vertical 1080 × 1920 (image entière, sur fond flou ou uni), incruste au besoin
des sous-titres SRT, puis l'envoie sur votre chaîne YouTube **en mode privé uniquement**.

Garde-fous intégrés :

- seuls des fichiers locaux sont acceptés (aucun téléchargement), et `--rights-confirmed` est obligatoire ;
- la visibilité est fixée à `private` dans le code ; il n'existe aucune option pour la changer, et une
  fiche modifiée en `public` ou `unlisted` est refusée ;
- les identifiants restent hors du dépôt, et les jetons sont masqués dans les journaux ;
- un historique SQLite empêche d'envoyer deux fois le même extrait ;
- `--dry-run` simule l'envoi sans contacter YouTube.

## Installation

Prérequis : Python 3.10 ou plus, FFmpeg (avec libx264 et libass).

```bash
cd yt-clips
python3 -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -e .
ytclips check
```

`ytclips check` liste ce qui est présent ou manquant : FFmpeg, bibliothèques, dossiers, réseau,
identifiants, et — si la chaîne est connectée — le nom de la chaîne visée.

## 1. Fournir une vidéo

Copiez votre fichier dans `yt-clips/input/` (le contenu de ce dossier est ignoré par git), avec
éventuellement son fichier `.srt`. N'importe quel chemin local convient aussi.

Puis créez le clip en choisissant le début et la fin :

```bash
ytclips clip input/ma_video.mp4 --start 1:05 --end 1:40 \
    --title "Mon titre" --description "Ma description" \
    --srt input/ma_video.srt --rights-confirmed
```

- `--start` / `--end` : secondes (`65`), `MM:SS` ou `HH:MM:SS.mmm`. Durée hors 20–60 s : refus.
- `--srt` : les temps du SRT sont ceux de la vidéo source (recalage automatique). Si votre SRT est
  déjà calé sur l'extrait, ajoutez `--srt-timing clip`.
- `--background` : `blur` (défaut), `black` ou une couleur `#RRGGBB`.

Résultat dans `output/` : le clip `.mp4` et sa fiche `.json`. **Le titre, la description et les tags
se modifient dans la section `youtube` de cette fiche** avant l'envoi ; ne touchez pas à la section `clip`.

## 2. Connecter la chaîne YouTube

À faire une fois, dans la [console Google Cloud](https://console.cloud.google.com/) :

1. créer un projet, puis activer **YouTube Data API v3** ;
2. configurer l'écran de consentement OAuth (type « Externe ») et vous ajouter comme utilisateur de test ;
3. créer un identifiant **ID client OAuth** de type **Application de bureau** et télécharger son fichier JSON ;
4. enregistrer ce fichier sous `~/.config/ytclips/client_secret.json` — jamais dans le dépôt.

Ensuite, sur un ordinateur disposant d'un navigateur :

```bash
ytclips auth
```

Autorisez l'accès avec le compte de votre chaîne. Le jeton est écrit dans `~/.config/ytclips/token.json`
(lisible par vous seul) et la commande affiche le nom de la chaîne connectée. Sur une machine sans
navigateur : `ytclips auth --no-browser` (copier-coller manuel de l'adresse de redirection).

Pour un environnement sans fichiers persistants (serveur, bac à sable), définissez à la place trois
variables d'environnement dans son gestionnaire de secrets : `YOUTUBE_CLIENT_ID`,
`YOUTUBE_CLIENT_SECRET` et `YOUTUBE_REFRESH_TOKEN` (valeurs présentes dans `token.json`).
Ne les collez jamais dans une conversation, un ticket ou un fichier du dépôt.

À savoir, d'après la documentation Google : tant que l'écran de consentement est en mode « Test », le
jeton expire au bout de 7 jours (relancer `ytclips auth`) ; et les vidéos envoyées par un projet API
non vérifié restent verrouillées en privé, ce qui correspond à l'usage voulu ici.

## 3. Lancer un test

```bash
ytclips upload output/ma_video_65000-100000.json --dry-run   # simulation, aucun envoi
ytclips upload output/ma_video_65000-100000.json             # envoi réel, en privé
```

La simulation vérifie le fichier, la fiche et l'historique, puis affiche la requête qui serait
envoyée. L'envoi réel réserve d'abord l'extrait dans l'historique, envoie la vidéo, puis contrôle que
YouTube confirme bien la visibilité `private`.

## 4. Consulter le résultat et les erreurs

- Le terminal affiche le lien de la vidéo et son lien YouTube Studio.
- `ytclips history` (ou `--json`) : tous les envois, leur état, l'identifiant de la vidéo, l'erreur éventuelle.
- `data/logs/ytclips.log` : journal détaillé et horodaté (`-v` pour voir aussi la commande FFmpeg à l'écran).
- Codes de sortie : `0` succès, `1` erreur, `3` doublon refusé.

États de l'historique : `uploaded` (envoyé), `failed` (refusé par YouTube, nouvel essai possible),
`pending` (envoi interrompu, issue inconnue). Une entrée `pending` bloque tout nouvel envoi du même
extrait : vérifiez YouTube Studio, puis `ytclips history --release N` si aucune vidéo n'a été créée.
`--force` passe outre un doublon, à utiliser en connaissance de cause.

## Emplacements

| Quoi | Où | Variable pour changer |
| --- | --- | --- |
| Historique et journaux | `yt-clips/data/` (ignoré par git) | `YTCLIPS_DATA_DIR` |
| Identifiants OAuth | `~/.config/ytclips/` | `YTCLIPS_CONFIG_DIR` |

L'historique n'évite les doublons que s'il est conservé : sauvegardez `data/history.sqlite3` et
utilisez toujours le même dossier de données.

## Tests

```bash
pip install -e '.[dev]' && pytest
```
