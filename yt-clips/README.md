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
- Codes de sortie : `0` succès, `1` erreur, `3` doublon refusé, `4` autorisation Google refusée.

États de l'historique : `uploaded` (envoyé), `failed` (refusé par YouTube, nouvel essai possible),
`pending` (envoi interrompu, issue inconnue). Une entrée `pending` bloque tout nouvel envoi du même
extrait : vérifiez YouTube Studio, puis `ytclips history --release N` si aucune vidéo n'a été créée.
`--force` passe outre un doublon, à utiliser en connaissance de cause.

## Automatisation cloud (ordinateur éteint)

La commande `batch` prend une source non traitée dans un stockage durable, crée le clip et l'envoie
en privé. Elle ne dépend d'aucun fichier local : chaque exécution peut partir d'une machine neuve.

```bash
bash yt-clips/scripts/setup.sh                 # FFmpeg, Python 3.12, dépendances verrouillées (uv.lock)
ytclips check                                  # tout doit être « prêt », y compris « Traitement par lot »
ytclips batch --rights-confirmed --dry-run     # simulation : rien n'est écrit ni envoyé
ytclips batch --rights-confirmed               # une source par exécution (--limit N pour davantage)
ytclips history --remote                       # historique partagé
```

Sur Hoplite, `.hoplite/settings.json` lance `scripts/setup.sh` à la création de chaque bac à sable.

### Variables d'environnement

À définir dans le gestionnaire de secrets de la plateforme (Hoplite : **Settings → Project → Environment**),
jamais dans le dépôt :

| Variable | Rôle |
| --- | --- |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | connexion à la chaîne (champs de même nom dans `token.json`) |
| `YTCLIPS_S3_BUCKET` | nom du bucket |
| `YTCLIPS_S3_ACCESS_KEY_ID`, `YTCLIPS_S3_SECRET_ACCESS_KEY` | clé d'accès au bucket |
| `YTCLIPS_S3_ENDPOINT_URL` | adresse du service (inutile pour AWS S3) ; R2 : `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `YTCLIPS_S3_REGION`, `YTCLIPS_S3_PREFIX` | facultatives (région `auto` par défaut ; sous-dossier du bucket) |

### Stockage durable

Tout bucket compatible S3 convient, à condition qu'il respecte l'écriture conditionnelle
`If-None-Match: *` : `ytclips check` le vérifie réellement et l'indique à la ligne « création exclusive ».

| Dossier du bucket | Contenu |
| --- | --- |
| `sources/` | vos vidéos (`.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v`, `.avi`), à déposer vous-même |
| `clips/` | clips produits et leur fiche |
| `state/` | historique des tentatives, index, comptes rendus d'exécution |

À côté d'une vidéo `sources/ma_video.mp4`, deux fichiers facultatifs de même nom :

- `ma_video.srt` : sous-titres à incruster ;
- `ma_video.json` : réglages de l'extrait. Sans cette fiche, le clip va du début de la vidéo jusqu'à
  60 secondes au plus, et le titre est le nom du fichier.

```json
{ "start": "1:05", "end": "1:40", "title": "Mon titre", "description": "Ma description", "tags": ["a", "b"] }
```

Champs acceptés : `start`, `end`, `title`, `description`, `tags`, `background`, `srt_timing`.

### Doublons, interruptions, exécutions simultanées

Un extrait est identifié par le contenu de la vidéo et ses bornes, pas par son nom de fichier. Avant
tout travail, l'exécution crée sa fiche de tentative par une écriture « seulement si absente » : de
deux exécutions simultanées, une seule l'obtient, l'autre passe à la source suivante.

| Ce qui s'est passé | Exécutions suivantes |
| --- | --- |
| Envoi réussi | source ignorée définitivement |
| Arrêt avant l'envoi (panne, coupure) | reprise automatique, après 6 h si l'arrêt a été brutal |
| Coupure pendant l'envoi | **jamais retentée** : source « à vérifier ». Contrôlez YouTube Studio, puis `ytclips history --remote --release <id>` si aucune vidéo n'existe |
| Refus de YouTube ou source invalide | 3 essais au plus (`--max-attempts`), puis source « à vérifier » |

Modifier la fiche `.json` d'une source (autres bornes) en fait un nouvel extrait.
L'historique local de la commande `upload` et l'historique partagé de `batch` sont distincts.

### Autorisation Google expirée

`batch` vérifie la connexion avant toute réservation. Si Google la refuse, la commande s'arrête
aussitôt avec le code de sortie `4` et un message « L'AUTORISATION GOOGLE A EXPIRÉ », sans nouvel
essai et sans rien modifier. Relancez `ytclips auth` sur votre ordinateur, puis remplacez la valeur de
`YOUTUBE_REFRESH_TOKEN`. Pour éviter l'expiration hebdomadaire du mode « Test », publiez l'application
OAuth en production dans la console Google Cloud.

## Emplacements

| Quoi | Où | Variable pour changer |
| --- | --- | --- |
| Historique et journaux | `yt-clips/data/` (ignoré par git) | `YTCLIPS_DATA_DIR` |
| Identifiants OAuth | `~/.config/ytclips/` | `YTCLIPS_CONFIG_DIR` |

L'historique n'évite les doublons que s'il est conservé : sauvegardez `data/history.sqlite3` et
utilisez toujours le même dossier de données.

## Tests

```bash
uv sync --frozen --extra dev && .venv/bin/pytest
```
