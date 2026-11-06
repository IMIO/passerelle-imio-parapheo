# passerelle-imio-parapheo

Connecteur passerelle qui fait signer des PDF via Paraphéo, c'est-à-dire le microservice
**apims-esign** d'iMio, qui pilote la plateforme COSi / LuxTrust.

Le connecteur envoie le document et la liste des signataires, reçoit les notifications
d'avancement d'apims-esign, et met le PDF signé à disposition de wcs.

## Configuration du connecteur

| Champ | Description |
|---|---|
| URL apims-esign | Base de l'API, par ex. `https://api-dev.imio.be/imio/esign` (sans `/` final) |
| URL du token Keycloak | `…/realms/<realm>/protocol/openid-connect/token` |
| Client ID / Client secret | Client Keycloak. Le token doit porter le rôle `access_imio-apims-esign` |
| Utilisateur / Mot de passe | Remplis : authentification ROPC (`grant_type=password`). Vides : compte de service du client (`client_credentials`). Les deux ou aucun |
| Numéro de TVA | `BE##########`, sert aux statistiques de signature par organisme |
| Compte du sceau / Code du sceau | Les deux ou aucun. Vides si l'organisme n'a pas de sceau |
| Méthode de signature | `BULK_ORELY_PADES` (eID qualifiée, production, par défaut) ou `BULK_PADES_SEAL_EMAIL` (OTP, staging) |

Valeurs de staging : méthode `BULK_PADES_SEAL_EMAIL`, sceau `sceau@imio.be` avec le code `PADES_SEAL`.

Le test de disponibilité appelle une route authentifiée de l'API : il vérifie l'URL,
l'obtention du token et le rôle.

## Endpoints

| Endpoint | Méthode | Rôle |
|---|---|---|
| `create-session` | POST | Envoie un PDF en signature. Renvoie l'`id` de la session |
| `session?id=…` | GET | État de la session (`status`, `code`, `sign_url`…) |
| `signed-document?id=…` | GET | PDF signé, en binaire (`application/pdf`) |
| `callback/<id>/` | POST | Notifications d'apims-esign (ouvert, l'`id` aléatoire sert de secret) |

### Corps de `create-session`

```json
{
  "document": {"filename": "demande.pdf", "content_type": "application/pdf", "content": "<base64>"},
  "signers": "bourgmestre@commune.be directeur@commune.be",
  "ordered": true,
  "watchers": "secretariat@commune.be",
  "seal": false,
  "acroform": false,
  "session_name": "Permis 2026-042",
  "external_ref": "permis-42"
}
```

- `document` : le format d'un champ fichier wcs (`{{ form_var_fichier }}`).
- `signers`, `watchers` : adresses **séparées par des espaces**, ou liste. L'ordre est conservé
  (il fixe l'ordre de signature quand `ordered` est vrai), les doublons sont retirés.
- `seal`, `ordered`, `acroform` : booléens. Les chaînes `true` / `1` / `oui` sont acceptées.
- Il faut au moins un signataire ou le sceau.

Avant l'envoi, le connecteur vérifie chaque signataire : adresse valide, compte LuxTrust
existant, image de signature enregistrée. Tous les problèmes remontent dans une seule erreur 400.

## Statuts d'une session

| Statut | Signification |
|---|---|
| `creating` | Envoi à apims-esign en cours |
| `pending` | Session acceptée par apims-esign, création en arrière-plan |
| `sealed` | Sceau apposé, la signature suit (code 20 avec signataires) |
| `waiting` | Session de signature créée, `sign_url` disponible (code 21) |
| `partially_signed` | Au moins un signataire a signé (code 22) |
| `done` | Terminé : code 23, ou code 20 pour un sceau seul |
| `error` | Échec de l'envoi, ou code d'erreur 50 à 59 |

Un code inconnu est journalisé sans changer le statut. Toutes les notifications reçues sont
conservées sur la session.

## Utilisation dans un workflow wcs

Le workflow interroge le connecteur en boucle :

1. **Appel webservice** `POST create-session` (réponse JSON), et on garde `data.id`.
2. **Délai** puis **appel webservice** `GET session?id=…`, avec un saut selon `data.status` :
   `done` pour continuer, `error` pour traiter l'échec, sinon on reboucle. Prévoir un nombre
   maximal de tours.
3. **Appel webservice** `GET signed-document?id=…` avec la réponse de type **pièce jointe**,
   stockée dans un champ fichier back-office. Les erreurs remontent par l'en-tête
   `x-error-code` (et un statut 409 si la session n'est pas terminée).

## Développement

- Après toute modification de `models.py` : `passerelle-manage makemigrations passerelle_imio_parapheo`,
  puis `migrate_schemas`.
- En local, apims-esign ne peut pas joindre `passerelle.dev.publik.love` : les notifications
  n'arrivent pas. On peut les rejouer à la main par un `POST` sur `callback/<id>/`, avec un corps
  `{"code": 21, "value": {"sign_session_url": "…"}}`.
- En runserver, les délais wcs ne se déclenchent pas tout seuls : lancer le cron wcs à la main.
