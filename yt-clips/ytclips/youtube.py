"""Connexion OAuth et envoi via l'API YouTube Data v3.

Les bibliothèques Google sont importées à la demande : la création de clips
et le mode simulation fonctionnent sans elles.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .logs import register_secret

log = logging.getLogger("ytclips")


class YouTubeError(Exception):
    pass


class AuthError(YouTubeError):
    """Autorisation Google expirée, révoquée ou invalide : réessayer ne sert à rien."""


def _renew_steps() -> str:
    return (
        "Aucune nouvelle tentative ne sera faite tant que la connexion n'est pas rétablie.\n"
        "Pour la rétablir :\n"
        "  1. sur votre ordinateur, lancer « ytclips auth » et autoriser de nouveau la chaîne ;\n"
        f"  2. pour l'exécution cloud, recopier le champ « refresh_token » du fichier token.json dans la\n"
        f"     variable {config.ENV_REFRESH_TOKEN} (Hoplite : Settings → Project → Environment).\n"
        "Tant que l'écran de consentement OAuth est en mode « Test », Google fait expirer ce jeton tous les 7 jours."
    )


def _refusal_code(error: Exception) -> str:
    for argument in error.args:
        if isinstance(argument, dict) and argument.get("error"):
            return str(argument["error"])
    text = str(error)
    for code in ("invalid_grant", "invalid_client", "unauthorized_client", "deleted_client", "invalid_scope"):
        if code in text:
            return code
    status = getattr(getattr(error, "resp", None), "status", None)
    return f"HTTP {status}" if status else "refus"


def auth_error(error: Exception) -> AuthError:
    """Message unique et explicite, quelle que soit l'étape où Google a refusé."""
    if isinstance(error, AuthError):
        return error
    code = _refusal_code(error)
    if code == "invalid_grant":
        cause = "L'AUTORISATION GOOGLE A EXPIRÉ ou a été révoquée (invalid_grant)."
    elif code in ("invalid_client", "unauthorized_client", "deleted_client"):
        cause = (
            f"Google ne reconnaît pas le client OAuth ({code}) : vérifier "
            f"{config.ENV_CLIENT_ID} et {config.ENV_CLIENT_SECRET}."
        )
    else:
        cause = f"Google a refusé l'autorisation ({code})."
    return AuthError(cause + "\n" + _renew_steps())


def is_auth_error(error: Exception) -> bool:
    if isinstance(error, AuthError):
        return True
    try:
        from google.auth.exceptions import RefreshError
    except ImportError:
        return False
    if isinstance(error, RefreshError):
        return True
    status = getattr(getattr(error, "resp", None), "status", None)
    return status is not None and int(status) == 401


def _env_client() -> tuple[str, str] | None:
    client_id = os.environ.get(config.ENV_CLIENT_ID)
    client_secret = os.environ.get(config.ENV_CLIENT_SECRET)
    return (client_id, client_secret) if client_id and client_secret else None


def credential_source() -> str | None:
    """Indique d'où viendraient les identifiants, sans jamais les lire ni les afficher."""
    if _env_client() and os.environ.get(config.ENV_REFRESH_TOKEN):
        return "variables d'environnement"
    if config.token_path().is_file():
        return f"fichier {config.token_path()}"
    return None


def _refuse_repo(path: Path) -> None:
    if config.inside_git_repo(path):
        raise YouTubeError(
            f"{path} se trouve dans un dépôt git : les identifiants doivent rester hors du dépôt "
            f"(par défaut {config.config_dir()})."
        )


def _require_libs():
    try:
        import google.auth.transport.requests  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except ImportError as error:
        raise YouTubeError(
            f"Bibliothèques Google absentes ({error.name}). Installer avec : pip install -e ."
        ) from None


def load_credentials():
    """Charge les identifiants depuis l'environnement, sinon depuis token.json."""
    _require_libs()
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    client = _env_client()
    refresh_token = os.environ.get(config.ENV_REFRESH_TOKEN)
    if client and refresh_token:
        creds = Credentials(
            None, refresh_token=refresh_token, token_uri=config.TOKEN_URI,
            client_id=client[0], client_secret=client[1],
        )
    elif config.token_path().is_file():
        _refuse_repo(config.token_path())
        creds = Credentials.from_authorized_user_file(str(config.token_path()))
    else:
        raise YouTubeError(
            "Aucune connexion YouTube : lancer « ytclips auth », ou définir "
            f"{config.ENV_CLIENT_ID}, {config.ENV_CLIENT_SECRET} et {config.ENV_REFRESH_TOKEN}."
        )
    for secret in (creds.refresh_token, creds.client_secret):
        register_secret(secret)
    # Un seul essai : un refus de Google est définitif tant que l'utilisateur n'a pas renouvelé l'accès.
    try:
        creds.refresh(Request())
    except RefreshError as error:
        raise auth_error(error) from None
    except Exception as error:  # noqa: BLE001 - panne de transport, pas un refus
        raise YouTubeError(
            f"Google est injoignable ({type(error).__name__}) : problème de réseau, l'autorisation n'est pas en cause."
        ) from None
    register_secret(creds.token)
    return creds


def _client_config() -> dict:
    client = _env_client()
    if client:
        register_secret(client[1])
        return {
            "installed": {
                "client_id": client[0], "client_secret": client[1],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": config.TOKEN_URI, "redirect_uris": ["http://localhost"],
            }
        }
    path = config.client_secret_path()
    if not path.is_file():
        raise YouTubeError(
            f"Client OAuth introuvable : placer le fichier téléchargé depuis Google Cloud dans {path} "
            f"(ou définir {config.ENV_CLIENT_ID} et {config.ENV_CLIENT_SECRET})."
        )
    _refuse_repo(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if "installed" not in data:
        raise YouTubeError("Le client OAuth doit être de type « Application de bureau ».")
    register_secret(data["installed"].get("client_secret"))
    return data


def run_auth(no_browser: bool = False, port: int = 8765) -> Path:
    """Flux OAuth « application de bureau » ; écrit token.json (0600) hors du dépôt."""
    _require_libs()
    from google_auth_oauthlib.flow import InstalledAppFlow

    target = config.token_path()
    _refuse_repo(target)
    flow = InstalledAppFlow.from_client_config(_client_config(), config.SCOPES)
    if no_browser:
        flow.redirect_uri = f"http://localhost:{port}/"
        url, state = flow.authorization_url(access_type="offline", prompt="consent")
        # Affichage direct sur le terminal, volontairement hors journal.
        print(
            "\n1. Ouvrez cette adresse dans un navigateur et autorisez l'accès à votre chaîne :\n\n"
            f"{url}\n\n"
            "2. Le navigateur est ensuite redirigé vers une page http://localhost… qui ne s'affiche pas : "
            "c'est normal.\n   Copiez l'adresse complète de cette page et collez-la ci-dessous "
            "(la saisie reste masquée).\n"
        )
        pasted = getpass.getpass("Adresse de redirection : ").strip()
        query = parse_qs(urlparse(pasted).query)
        if query.get("error"):
            raise YouTubeError(f"Autorisation refusée : {query['error'][0]}")
        if not query.get("code") or query.get("state", [""])[0] != state:
            raise YouTubeError("Adresse de redirection invalide (code absent ou état incohérent).")
        register_secret(query["code"][0])
        flow.fetch_token(code=query["code"][0])
        creds = flow.credentials
    else:
        creds = flow.run_local_server(
            port=port, open_browser=True, access_type="offline", prompt="consent",
            authorization_prompt_message="Ouvrez cette adresse si le navigateur ne s'ouvre pas : {url}",
            success_message="Connexion terminée, vous pouvez fermer cet onglet.",
        )
    if not creds.refresh_token:
        raise YouTubeError("Google n'a pas fourni de jeton de rafraîchissement ; réessayer.")
    for secret in (creds.token, creds.refresh_token, creds.client_secret):
        register_secret(secret)

    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
    os.chmod(target, 0o600)
    return target


def _service(creds):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel_info(creds) -> dict | None:
    """Chaîne associée à la connexion (id et nom), pour vérifier la cible."""
    response = _service(creds).channels().list(part="snippet", mine=True).execute()
    items = response.get("items") or []
    if not items:
        return None
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def upload_private(creds, clip: Path, body: dict) -> dict:
    """Envoi reprenable. Refuse toute visibilité autre que « private »."""
    from googleapiclient.http import MediaFileUpload

    if body.get("status", {}).get("privacyStatus") != config.PRIVACY:
        raise YouTubeError("Envoi refusé : la visibilité demandée n'est pas « private ».")
    media = MediaFileUpload(str(clip), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = _service(creds).videos().insert(
        part="snippet,status", body=body, media_body=media, notifySubscribers=False
    )
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=5)
        if status:
            log.info("Envoi : %d %%", int(status.progress() * 100))
    return response
