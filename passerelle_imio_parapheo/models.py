import base64
import json
import secrets
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models
from django.http import HttpResponse
from passerelle.base.models import BaseResource
from passerelle.utils.api import endpoint
from passerelle.utils.http_authenticators import TokenAuth
from passerelle.utils.jsonresponse import APIError
from requests import RequestException

STATUS_BY_CODE = {21: "waiting", 22: "partially_signed", 23: "done"}
# Code 20 : sceau apposé. Fin de session pour un sceau seul, simple étape avant la signature sinon.
SEALED_CODE = 20
# Seule plage d'erreurs documentée ; un autre code inconnu est journalisé sans changer le statut
ERROR_CODES = range(50, 60)

# Adresse fictive pour check_status : un utilisateur inconnu n'est pas une erreur pour apims-esign
# (has_account=false), l'appel teste donc l'URL, le token et le rôle sans dépendre d'un vrai compte
CHECK_STATUS_EMAIL = "passerelle-check-status@imio.be"

SIGN_CODES = [
    ("BULK_ORELY_PADES", "Signature qualifiée par lecteur eID (production)"),
    ("BULK_PADES_SEAL_EMAIL", "Signature par OTP e-mail (staging)"),
]


def new_token():
    return secrets.token_hex(16)


def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "oui")


def split_emails(value):
    # Ordre conservé : avec enforceUsersOrder, il fixe l'ordre de signature
    if not isinstance(value, list):
        value = str(value or "").split(" ")
    emails = []
    for email in value:
        email = str(email).strip().lower()
        if email and email not in emails:
            emails.append(email)
    return emails


def is_valid_email(email):
    # apims-esign type les adresses en format email et répond 422 sinon : on filtre avant l'appel
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


class Parapheo(BaseResource):
    """
    Connecteur iMio - parapheo
    """

    url = models.URLField(
        max_length=255,
        verbose_name="URL apims-esign",
    )
    token_url = models.URLField(
        max_length=255,
        verbose_name="URL du token Keycloak",
        help_text="…/realms/<realm>/protocol/openid-connect/token",
    )
    client_id = models.CharField(max_length=255, verbose_name="Client ID")
    client_secret = models.CharField(max_length=1024, verbose_name="Client secret")
    username = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Utilisateur",
        help_text="Vide pour s'authentifier avec le compte de service du client (client_credentials)",
    )
    password = models.CharField(max_length=1024, blank=True, verbose_name="Mot de passe")
    vat_number = models.CharField(
        max_length=15,
        verbose_name="Numéro de TVA",
        help_text="BE0123456789",
    )
    seal_user = models.EmailField(
        blank=True,
        verbose_name="Compte du sceau",
        help_text="Vide si l'organisme n'a pas de sceau",
    )
    seal_code = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Code du sceau",
        help_text="PADES_SEAL en staging ; en production, code fourni par LuxTrust à la commande du sceau",
    )
    sign_code = models.CharField(
        max_length=32,
        choices=SIGN_CODES,
        default="BULK_ORELY_PADES",
        verbose_name="Méthode de signature",
    )
    api_description = "Connecteur iMio - Parapheo"
    category = "Connecteurs iMio"
    requests_timeout = 60

    class Meta:
        verbose_name = "Connecteur Parapheo"

    def clean(self):
        super().clean()
        # Le sceau exige à la fois le compte qui porte son image et le code fourni par LuxTrust
        if bool(self.seal_user) != bool(self.seal_code):
            raise ValidationError("Le compte du sceau et le code du sceau vont ensemble : remplir les deux ou aucun.")
        if bool(self.username) != bool(self.password):
            raise ValidationError("L'utilisateur et le mot de passe vont ensemble : remplir les deux ou aucun.")

    def make_requests_auth(self, session):
        data = {"client_id": self.client_id, "client_secret": self.client_secret}
        if self.username:
            # ROPC : les groupes access_* sont portés par l'utilisateur sso-apps
            data.update({"grant_type": "password", "username": self.username, "password": self.password})
        else:
            # Compte de service du client, qui doit alors porter le rôle access_imio-apims-esign
            data["grant_type"] = "client_credentials"
        return TokenAuth(session=session, token_url=self.token_url, auth=None, data=data)

    def check_status(self):
        # Route authentifiée : vérifie l'URL, l'obtention du token et le rôle
        response = self.requests.get(f"{self.url}/v1/luxtrust/users/{CHECK_STATUS_EMAIL}")
        response.raise_for_status()

    def check_signers(self, signers):
        # Un signataire sans compte LuxTrust ou sans image de signature fait échouer la session
        problems = []
        for email in signers:
            if not is_valid_email(email):
                problems.append(f"adresse invalide : {email}")
                continue
            try:
                response = self.requests.get(f"{self.url}/v1/luxtrust/users/{email}")
                response.raise_for_status()
            except RequestException as e:
                raise APIError(f"Paraphéo Connector Error: {e}")
            user = response.json()
            if not user.get("has_account"):
                problems.append(f"{email} ne s'est jamais connecté à LuxTrust")
            elif not user.get("has_signature"):
                problems.append(f"{email} n'a pas enregistré son image de signature")
        if problems:
            raise APIError("Signataires pas prêts : " + ", ".join(problems), http_status=400)

    def get_signing_session(self, id):
        try:
            return self.signing_sessions.get(token=id)
        except SigningSession.DoesNotExist:
            raise APIError(f"Session inconnue : {id}", http_status=404)

    @endpoint(
        methods=["post"],
        name="create-session",
        perm="can_access",
        description="Envoyer un PDF en signature",
        long_description="Body : document (fichier wcs), signers, ordered, watchers, seal, acroform, session_name, external_ref",
        display_order=0,
        display_category="Signature",
    )
    def create_session(self, request):
        post_data = json.loads(request.body)
        document = post_data.get("document")
        if not isinstance(document, dict) or not document.get("content"):
            raise APIError("Le document est introuvable", http_status=400)

        signers = split_emails(post_data.get("signers"))
        seal = to_bool(post_data.get("seal"))
        if seal and not (self.seal_user and self.seal_code):
            raise APIError("Pas de compte de sceau configuré", http_status=400)
        if not signers and not seal:
            raise APIError("Il faut au moins un signataire ou le sceau", http_status=400)
        watchers = split_emails(post_data.get("watchers"))
        invalid_watchers = [email for email in watchers if not is_valid_email(email)]
        if invalid_watchers:
            raise APIError("Observateurs invalides : " + ", ".join(invalid_watchers), http_status=400)
        self.check_signers(signers)

        session = SigningSession(resource=self, external_ref=post_data.get("external_ref", ""))
        session.filename = document.get("filename") or "document.pdf"
        session.doc_uuid = str(uuid.uuid4())
        session.with_signers = bool(signers)

        data = {
            "commonData": {
                "sessionName": post_data.get("session_name") or f"Demande {session.external_ref}",
                "vatNumber": self.vat_number,
                "endpointUrl": request.build_absolute_uri(self.get_absolute_url()) + f"callback/{session.token}/",
                "imioAppSessionId": session.token,
                # Pas de GED iMio côté Téléservices : le PDF signé reste dans le stockage d'apims-esign.
                # docUuid reste obligatoire (un uuid4), mais n'a alors aucune utilité pour nous.
                "uploadToGed": False,
                "documentData": [
                    {"filename": session.filename, "uniqueCode": session.token, "docUuid": session.doc_uuid}
                ],
            }
        }
        if seal:
            data["sealData"] = {"sealCode": self.seal_code, "users": [self.seal_user]}
        if signers:
            data["signData"] = {
                "users": signers,
                # Toujours explicite : la valeur par défaut de l'API est la méthode OTP de staging
                "signCode": self.sign_code,
                "enforceUsersOrder": to_bool(post_data.get("ordered")),
                "acroform": to_bool(post_data.get("acroform")),
                "watchers": watchers,
            }

        files = [("files", (session.filename, base64.b64decode(document["content"]), "application/pdf"))]

        # Enregistrée avant l'appel : si la réponse se perd alors qu'apims-esign a créé la session,
        # ses notifications trouvent quand même la session au lieu de tomber en 404.
        session.status = "creating"
        session.save()
        # Les mises à jour ne touchent que les sessions encore en « creating » : une notification
        # arrivée entre-temps fait foi et ne doit pas être écrasée.
        still_creating = SigningSession.objects.filter(pk=session.pk, status="creating")
        try:
            response = self.requests.post(f"{self.url}/v1/luxtrust/sessions", data={"data": json.dumps(data)}, files=files)
            response.raise_for_status()
        except RequestException as e:
            still_creating.update(status="error")
            self.logger.warning(f"Paraphéo Connector Error: {e}")
            raise APIError(f"Paraphéo Connector Error: {e}")

        still_creating.update(status="pending")
        session.refresh_from_db()
        self.logger.info(f"Session {session.token} créée ({session.external_ref})")
        return {"data": session.to_json()}

    @endpoint(
        methods=["post"],
        name="callback",
        perm="OPEN",
        pattern=r"^(?P<id>[0-9a-f]{32})/$",
        example_pattern="{id}/",
        description="Notifications d'apims-esign",
        display_order=4,
        display_category="Signature",
    )
    def callback(self, request, id):
        session = self.get_signing_session(id)
        notification = json.loads(request.body)
        session.notifications.append(notification)
        session.code = notification.get("code")
        value = notification.get("value") or {}
        if value.get("sign_session_url"):
            session.sign_url = value["sign_session_url"]
        known = session.code == SEALED_CODE or session.code in STATUS_BY_CODE or session.code in ERROR_CODES
        if not known:
            self.logger.warning(f"Session {id} : code inconnu {session.code}, statut inchangé")
        elif session.status != "done":
            if session.code == SEALED_CODE:
                session.status = "sealed" if session.with_signers else "done"
            elif session.code in STATUS_BY_CODE:
                session.status = STATUS_BY_CODE[session.code]
            else:
                session.status = "error"
        session.save()
        self.logger.info(f"Session {id} : code {session.code}")
        return {"data": session.status}

    @endpoint(
        methods=["get"],
        name="session",
        perm="can_access",
        description="État d'une session",
        parameters={"id": {"description": "Identifiant de la session", "example_value": "0123456789abcdef0123456789abcdef"}},
        display_order=2,
        display_category="Signature",
    )
    def read_session(self, request, id):
        return {"data": self.get_signing_session(id).to_json()}

    @endpoint(
        methods=["get"],
        name="signed-document",
        perm="can_access",
        description="Récupérer le PDF signé",
        parameters={"id": {"description": "Identifiant de la session", "example_value": "0123456789abcdef0123456789abcdef"}},
        display_order=3,
        display_category="Signature",
    )
    def signed_document(self, request, id):
        session = self.get_signing_session(id)
        if session.status != "done":
            raise APIError(f"Session pas encore signée ({session.status})", http_status=409)
        try:
            # apims-esign range le PDF signé sous le docUuid qu'on lui a envoyé
            response = self.requests.get(f"{self.url}/v1/documents/{session.doc_uuid}")
            response.raise_for_status()
        except RequestException as e:
            self.logger.warning(f"Paraphéo Connector Error: {e}")
            raise APIError(f"Paraphéo Connector Error: {e}")
        return HttpResponse(
            response.content,
            content_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="signe-{session.filename}"'},
        )


class SigningSession(models.Model):
    resource = models.ForeignKey(Parapheo, on_delete=models.CASCADE, related_name="signing_sessions")
    token = models.CharField(max_length=32, unique=True, default=new_token)
    external_ref = models.CharField(max_length=128, blank=True)
    filename = models.CharField(max_length=255)
    doc_uuid = models.CharField(max_length=36)
    # Sans signataire, la session se termine dès que le sceau est apposé (code 20)
    with_signers = models.BooleanField(default=True)
    status = models.CharField(max_length=32, default="pending")
    code = models.IntegerField(null=True, blank=True)
    sign_url = models.URLField(max_length=1024, blank=True)
    notifications = models.JSONField(default=list, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def to_json(self):
        return {
            "id": self.token,
            "external_ref": self.external_ref,
            "status": self.status,
            "code": self.code,
            "sign_url": self.sign_url,
            "updated": self.updated.isoformat() if self.updated else None,
        }

