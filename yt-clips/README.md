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
jeton expire au bout de 7 jours (relancer `ytclips auth`). Depuis le 2 novembre 2026, les vidéos
envoyées par un projet API non vérifié ne sont plus verrouillées en privé : ce programme précise donc
toujours la visibilité `private` à l'envoi.

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

## Sélection automatique, évaluation et publication

Ces commandes travaillent sur le même stockage durable et le même historique que `batch`.

```bash
ytclips propose                          # analyse une source, enregistre jusqu'à 3 propositions
ytclips proposals                        # liste : identifiant, timecodes, titre, raison, état, lien
ytclips review --rights-confirmed        # crée les clips proposés et les envoie en PRIVÉ pour évaluation
ytclips approve <id> [--title "…"]       # valide : entre dans la file de publication
ytclips reject <id>                      # refuse : ce passage ne sera plus proposé
ytclips publish --rights-confirmed       # traite la file ; sans mode public, le clip reste privé
```

`propose`, `review` et `publish` acceptent `--dry-run`.

### Comment un passage est choisi

`propose` lit la vidéo une fois avec FFmpeg pour relever les changements de plan et les silences, puis
croise ces mesures avec les sous-titres `.srt` déposés à côté de la vidéo :

- un extrait dure de 20 à 60 secondes, commence au début d'une phrase et finit sur une fin de phrase ;
- ses bornes ne tombent jamais dans une réplique, et se calent sur un changement de plan s'il y en a un
  à moins de 1,5 s ;
- sont écartés : les paroles de chanson et indications (`♪`, `[Musique]`), les passages sans dialogue de
  plus de 4 s (génériques, plans muets), les silences de plus de 4 s, et tout passage déjà envoyé ou
  déjà proposé pour cette vidéo, même refusé ;
- au plus 3 extraits sans recouvrement sont retenus par vidéo ; `--skip-start` et `--skip-end` écartent
  un générique de durée connue.

Le score classe les passages selon la continuité du dialogue et la netteté des bornes. Il ne prédit
pas l'audience d'un clip. Le titre et la description sont tirés des répliques : ce sont des brouillons,
à corriger avec `approve --title`.

Chaque proposition est une fiche `state/proposals/<identifiant>.json` : timecodes, titre, description,
raison de la sélection, mesures, état, et lien de la vidéo une fois envoyée.

Une source analysée et sans fiche manuelle `.json` est laissée à la sélection automatique : `batch` ne
la traite plus. Les sources ne sont jamais modifiées ni supprimées.

### Sans sous-titres

Les bornes sont alors posées sur les pauses de la bande-son, sans garantie sur les fins de phrases ;
la proposition le signale dans sa raison. Deux options :

| Option | Coût | Besoins |
| --- | --- | --- |
| Transcription locale (`ytclips transcribe`, voir la section sur le montage) | gratuit | option à installer, modèle d'environ 0,5 Go à télécharger dans chaque environnement neuf, de l'ordre d'un quart de la durée de l'audio |
| API de transcription OpenAI | de 0,003 à 0,006 $ par minute d'audio selon le modèle (tarifs publiés le 12/11/2026), soit environ 0,14 à 0,27 $ pour 45 minutes | clé d'API en variable de projet, audio transmis à un tiers |

### Publication publique

Tout clip est d'abord envoyé en privé. Le rendre public consiste à modifier la visibilité de cette
vidéo ; il n'est jamais envoyé une seconde fois. Le mode public exige les deux verrous à la fois :

- la variable `YTCLIPS_PUBLIC_MODE=enabled` ;
- l'option `--public` de la commande `publish`.

`publish --public` rend alors public le plus ancien clip validé, **un seul par jour** (jour UTC, ou
celui du fuseau `YTCLIPS_TIMEZONE`). Le créneau du jour est une fiche `state/publications/<date>.json`
créée de façon exclusive : deux exécutions simultanées ne publient pas deux clips. Après une
interruption, l'exécution suivante relit l'état réel de la vidéo avant d'agir. Trois refus de YouTube
pour un même clip le mettent « à vérifier ».

Modifier la visibilité demande une permission Google plus large que l'envoi (« voir, modifier et
supprimer vos vidéos YouTube »). `ytclips check` indique si elle est accordée. Sinon : `ytclips auth
--public` sur votre ordinateur, puis remplacer `YOUTUBE_REFRESH_TOKEN`.

### Import depuis une source autorisée

```bash
ytclips import https://media.exemple.org/videos/episode.mp4 --rights-confirmed
```

L'import est désactivé tant que `YTCLIPS_IMPORT_ALLOWED_HOSTS` ne liste pas les hôtes dont vous avez
le droit de reprendre les vidéos. Il ne fait que télécharger en HTTPS un fichier vidéo désigné
directement, sans jamais remplacer une source existante. Une page web, un lecteur, un accès refusé ou
un fichier illisible arrêtent l'import : aucune protection n'est contournée.

## Montage de 60 secondes à partir d'un film

Au lieu de plusieurs clips indépendants, `montage` assemble 4 à 6 passages d'un film en un seul MP4
vertical de 60 secondes, sous-titré, envoyé en privé sur YouTube et préparé pour TikTok.

```bash
ytclips montage plan --source sources/mon_film.mkv --title "Titre du film"   # analyse et plan
ytclips montage list                                                         # passages, timecodes, raisons
ytclips montage send --rights-confirmed                                      # montage, envoi PRIVÉ, fiche TikTok
ytclips tiktok link <id>                                                     # légende et lien de téléchargement
```

`plan` et `send` acceptent `--dry-run` ; pour `send`, le montage est alors créé puis jeté.

### Comment les passages sont choisis

Le programme ne comprend pas l'histoire : il applique des règles mesurables, et le plan reste à relire.

- **Ensemble** : la partie retenue du film est découpée en 4 à 6 zones égales et un passage est pris dans
  chacune, dans l'ordre du film. Le premier sert d'introduction, le dernier de conclusion.
- **Fin du film** : rien n'est pris après 75 % de la durée (`--cutoff`), pour ne pas révéler le dénouement.
- **Phrases** : un passage commence au début d'une phrase et finit sur une fin de phrase. Si la
  ponctuation des sous-titres ne le permet pas, les bornes sont posées sur des pauses et le plan le dit.
- **Génériques et répétitions** : les paroles de chanson et indications (`♪`, `[Musique]`) sont écartées,
  comme deux passages aux dialogues trop proches ; `--skip-start` et `--skip-end` écartent un générique parlé.
- **Durée** : la combinaison retenue totalise exactement 60 s. L'ajustement se fait sur les marges de
  silence avant et après le dialogue, jamais en coupant une réplique, et sans déborder sur un autre plan
  tant que c'est possible.
- **Préférence** : à règles égales, les passages au dialogue dense, avec questions ou exclamations et
  changements de plan, passent devant. Cela ne mesure pas ce qu'un spectateur trouvera marquant.

Pour corriger un plan, imposez les passages : `--segments "00:04:10-00:04:22.5, 00:17:03-00:17:15, …"`
(4 à 6 passages, 60 s au total, aucune réplique coupée ; sinon la commande refuse et dit pourquoi).

Le plan est une fiche `state/montages/<id>.json` : pour chaque passage, ses timecodes, son texte et la
raison du choix. Aucune musique ni filigrane n'est ajouté ; le son est celui du film, avec un fondu de
40 ms aux raccords.

### Cadrage

Par défaut l'image entière du film est conservée, sur fond flou, et les bandes noires du fichier sont
retirées. `--zoom 1.35` agrandit l'image en rognant ses bords latéraux, au centre : il n'y a pas de
suivi automatique de l'action. Changer de zoom donne un nouveau plan, donc un nouvel envoi.

### Sous-titres et transcription

Dans l'ordre : le fichier `.srt` de même nom que le film ; sinon une copie déjà enregistrée dans le
stockage ; sinon une piste de sous-titres texte du fichier (la française si elle existe). Les pistes
en image (Blu-ray, DVD) ne sont pas lisibles.

À défaut, `ytclips transcribe --source … --language fr` transcrit la bande-son sur la machine, sans
service payant, puis enregistre le résultat. Cette option n'est pas installée par défaut :

```bash
uv sync --frozen --extra dev --extra transcribe
```

Elle ajoute plusieurs centaines de Mo de bibliothèques et télécharge le modèle à la première
utilisation dans chaque environnement neuf (environ 0,5 Go pour `small`). Mesuré sur 4 processeurs,
avec une voix de synthèse en anglais : environ un quart de la durée de l'audio pour `small`, soit de
l'ordre d'une demi-heure pour un film de deux heures. La transcription donne la langue parlée : elle
ne traduit pas vers le français.

### Réutilisation

L'analyse du film entier (`state/signals/`) et ses sous-titres extraits ou transcrits
(`state/transcripts/`) sont enregistrés une fois. Tant que le fichier du film ne change pas, un
nouveau plan ne refait ni l'une ni l'autre, et ne retélécharge pas le film.

`batch` ne découpe plus de lui-même une source de plus de 10 minutes sans fiche `.json` : il la laisse
à `montage` ou à `propose`.

## TikTok : préparation pour une publication manuelle

TikTok est une seconde destination, indépendante de YouTube. Le programme n'appelle pas l'API de
TikTok : il prépare, pour chaque clip déjà produit dans `clips/`, une légende modifiable et un lien de
téléchargement, et il tient un historique propre à TikTok.

```bash
ytclips tiktok prepare                    # une fiche par clip du stockage (jamais réécrite ensuite)
ytclips tiktok list                       # identifiant, état, clip, légende
ytclips tiktok caption <id> --text "…"    # modifier la légende (hashtags compris, 2200 caractères au plus)
ytclips tiktok link <id>                  # légende + lien de téléchargement du clip, valable 24 h
ytclips tiktok done <id> [--url https://…]  # noter la publication faite dans l'application TikTok
ytclips tiktok skip <id>                  # écarter ce clip pour TikTok ; « reopen » pour revenir en arrière
```

Parcours : `link` sur l'ordinateur, ouvrir le lien sur le téléphone, enregistrer la vidéo, la publier
dans l'application TikTok en collant la légende, puis `done`. Le lien donne accès au fichier pendant sa
durée de validité : ne le partagez pas. Le clip est aussi téléchargeable depuis le tableau de bord R2.

Les fiches sont dans `state/tiktok/`, l'historique YouTube dans `state/history/` : aucune commande
TikTok ne lit ni n'écrit l'historique YouTube, et inversement. Un clip refusé par YouTube reste
disponible pour TikTok ; une proposition refusée avec `reject` n'est pas préparée. Les clips sont
ceux produits par ce programme à partir de vos sources : ils ne portent aucun logo ni filigrane.

### Pourquoi pas de publication par l'API

D'après la documentation officielle de TikTok, consultée le 12 novembre 2026 :

- **Publication directe** (`video.publish`) : tant que l'application n'a pas passé l'audit de TikTok,
  les publications sont limitées à la visibilité « moi uniquement », le compte doit être privé au
  moment de l'envoi, et le choix de la visibilité doit être fait à la main par l'utilisateur à chaque
  publication, sans valeur par défaut. Ce n'est pas compatible avec une exécution automatique.
- **Audit** : les règles de TikTok citent comme non acceptable « un outil utilitaire qui aide à
  envoyer des contenus sur le ou les comptes que vous ou votre équipe gérez ». C'est le cas de ce
  programme : l'audit a peu de chances d'aboutir.
- **Envoi en brouillon** (`video.upload`) : la vidéo arrive dans la boîte de réception TikTok du compte,
  qui finalise et publie depuis l'application ; la légende ne peut pas être transmise par ce canal et
  il y a au plus 5 brouillons en attente par 24 heures. Cette permission demande elle aussi la
  validation de l'application, sauf dans le mode « Sandbox », prévu pour les essais (10 comptes au
  plus). Ce parcours n'est pas implémenté.

Aucun robot de navigateur n'est utilisé pour contourner ces limites.

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
