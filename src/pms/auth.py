"""Who is calling? Authentication for the web app (concept 7.3, ADR-005; BUILD_SPEC 4.3).

* Local mode (``APP_ENV=local`` and ``LOCAL_TEST_USERS=true``): one built-in test user per role -
  ``viewer``, ``analyst``, ``regulatory``, ``reviewer`` (medical reviewer) and ``operator`` - selected with
  the header ``X-Demo-User: <user>`` or with the cookie that the local user picker (``/login``) sets.
* Azure (``APP_ENV=azure``): Container Apps built-in authentication validates the Microsoft Entra
  sign-in (browser session or bearer token) and injects ``X-MS-CLIENT-PRINCIPAL``: base64-encoded JSON
  with the token claims. External requests cannot set this header. The app roles ``PMS.Viewer``,
  ``PMS.Analyst``, ``PMS.Regulatory``, ``PMS.MedicalReviewer`` and ``PMS.Operator`` are mapped to the
  domain roles. Test users never exist in Azure: the demo-user header and cookie are ignored there, and
  a missing or unreadable principal means "not signed in" (HTTP 401).

This module only establishes WHO the caller is. WHAT the caller may do is decided by the domain
(:meth:`pms.workflow.Workflow.authorize`, permission matrix of concept 7.3) - never here.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .config import Settings
from .models import Actor, Role

DEMO_USER_HEADER = "X-Demo-User"
DEMO_USER_COOKIE = "pms_demo_user"
PRINCIPAL_HEADER = "X-MS-CLIENT-PRINCIPAL"
PRINCIPAL_ID_HEADER = "X-MS-CLIENT-PRINCIPAL-ID"
PRINCIPAL_NAME_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"
MAX_PRINCIPAL_LENGTH = 64 * 1024

#: Claim types that carry app roles (short JWT name and the long name used by the auth middleware).
ROLE_CLAIM_TYPES: tuple[str, ...] = ("roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")
OBJECT_ID_CLAIM_TYPES: tuple[str, ...] = ("oid", "http://schemas.microsoft.com/identity/claims/objectidentifier")
USERNAME_CLAIM_TYPES: tuple[str, ...] = (
    "preferred_username", "upn", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn", "email",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
NAME_CLAIM_TYPES: tuple[str, ...] = ("name", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")

#: Entra app roles (infra/modules/entra-app.bicep) -> domain roles (pms.models.Role).
APP_ROLES: dict[str, str] = {
    "PMS.Viewer": Role.VIEWER.value,
    "PMS.Analyst": Role.ANALYST.value,
    "PMS.Regulatory": Role.REGULATORY.value,
    "PMS.MedicalReviewer": Role.MEDICAL_REVIEWER.value,
    "PMS.Operator": Role.OPERATOR.value,
}
ROLE_LABELS: dict[str, str] = {
    Role.VIEWER.value: "viewer",
    Role.ANALYST.value: "analyst",
    Role.REGULATORY.value: "regulatory",
    Role.MEDICAL_REVIEWER.value: "medical reviewer",
    Role.OPERATOR.value: "operator",
}

METHOD_LOCAL = "local-test-user"
METHOD_ENTRA = "entra"
METHOD_NONE = "none"


@dataclass(frozen=True)
class DemoUser:
    """A built-in local test user (offline mode only)."""

    name: str
    role: str

    def actor(self) -> Actor:
        return Actor(actor_id=self.name, roles=frozenset({self.role}),
                     display_name=f"{self.name} (local test user, {ROLE_LABELS[self.role]})")


LOCAL_TEST_USERS: dict[str, DemoUser] = {u.name: u for u in (
    DemoUser("viewer", Role.VIEWER.value),
    DemoUser("analyst", Role.ANALYST.value),
    DemoUser("regulatory", Role.REGULATORY.value),
    DemoUser("reviewer", Role.MEDICAL_REVIEWER.value),
    DemoUser("operator", Role.OPERATOR.value),
)}


class InvalidPrincipal(ValueError):
    """The X-MS-CLIENT-PRINCIPAL header cannot be read: treated as 'not signed in'."""


@dataclass(frozen=True)
class Identity:
    """Result of authentication. ``actor`` is :meth:`Actor.anonymous` when nobody is signed in."""

    actor: Actor
    method: str
    problem: str | None = None
    unmapped_roles: tuple[str, ...] = ()

    @property
    def authenticated(self) -> bool:
        return self.actor.authenticated

    @property
    def role_labels(self) -> list[str]:
        return [ROLE_LABELS.get(r, r) for r in sorted(self.actor.roles)]


def local_test_users_enabled(settings: Settings) -> bool:
    """Test users exist only offline (``APP_ENV=local``) and only with ``LOCAL_TEST_USERS=true``."""
    return (not settings.is_azure) and settings.local_test_users


def local_test_user(name: str | None) -> DemoUser | None:
    return LOCAL_TEST_USERS.get((name or "").strip().lower())


# --------------------------------------------------------------------------- Azure principal
def _b64decode(value: str) -> bytes:
    text = "".join(value.split())
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        try:
            return base64.urlsafe_b64decode(padded)
        except (binascii.Error, ValueError) as exc:
            raise InvalidPrincipal("the principal is not valid base64") from exc


def parse_client_principal(value: str) -> dict[str, Any]:
    """Decode ``X-MS-CLIENT-PRINCIPAL`` (base64 JSON: auth_typ, name_typ, role_typ, claims[typ, val])."""
    if not value or not value.strip():
        raise InvalidPrincipal("empty principal")
    if len(value) > MAX_PRINCIPAL_LENGTH:
        raise InvalidPrincipal("principal too large")
    raw = _b64decode(value)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidPrincipal("the principal is not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("claims", []), list):
        raise InvalidPrincipal("the principal has no claim list")
    return data


def _claims(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    out = []
    for claim in data.get("claims") or []:
        if isinstance(claim, dict) and claim.get("typ") is not None and claim.get("val") is not None:
            out.append((str(claim["typ"]), str(claim["val"])))
    return out


def _role_values(value: str) -> list[str]:
    """One role per claim; a JSON array in one claim value is accepted defensively."""
    text = value.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            items = None
        if isinstance(items, list):
            return [str(i).strip() for i in items if str(i).strip()]
    return [text] if text else []


def actor_from_principal(data: Mapping[str, Any], *, fallback_id: str | None = None,
                         fallback_name: str | None = None) -> tuple[Actor, tuple[str, ...]]:
    """Domain actor from a decoded principal: user id from oid, else preferred_username, else name."""
    claims = _claims(data)
    role_types = set(ROLE_CLAIM_TYPES)
    if data.get("role_typ"):
        role_types.add(str(data["role_typ"]))
    values: list[str] = []
    for typ, val in claims:
        if typ in role_types:
            values += _role_values(val)
    roles = frozenset(APP_ROLES[v] for v in values if v in APP_ROLES)
    unmapped = tuple(sorted({v for v in values if v not in APP_ROLES}))

    def first(types: tuple[str, ...]) -> str | None:
        for wanted in types:
            for typ, val in claims:
                if typ == wanted and val.strip():
                    return val.strip()
        return None

    name_types = NAME_CLAIM_TYPES + ((str(data["name_typ"]),) if data.get("name_typ") else ())
    object_id = first(OBJECT_ID_CLAIM_TYPES)
    username = first(USERNAME_CLAIM_TYPES)
    name = first(name_types)
    actor_id = object_id or username or name or (fallback_id or "").strip() or None
    if not actor_id:
        raise InvalidPrincipal("the principal carries no user id (oid, preferred_username or name)")
    display = name or username or (fallback_name or "").strip() or actor_id
    return Actor(actor_id=actor_id, roles=roles, display_name=display), unmapped


# --------------------------------------------------------------------------- entry point
def authenticate(header: Callable[[str], str | None], cookies: Mapping[str, str], settings: Settings) -> Identity:
    """Identify the caller of one request.

    ``header(name)`` returns a request header (case-insensitive) or None; ``cookies`` maps cookie names
    to values. Azure never falls back to a test user."""
    if settings.is_azure:
        value = header(PRINCIPAL_HEADER)
        if not value:
            return Identity(Actor.anonymous(), METHOD_NONE, "not signed in: no Microsoft Entra principal")
        try:
            actor, unmapped = actor_from_principal(parse_client_principal(value),
                                                   fallback_id=header(PRINCIPAL_ID_HEADER),
                                                   fallback_name=header(PRINCIPAL_NAME_HEADER))
        except InvalidPrincipal as exc:
            return Identity(Actor.anonymous(), METHOD_NONE, f"not signed in: {exc}")
        return Identity(actor, METHOD_ENTRA, None, unmapped)
    if not settings.local_test_users:
        return Identity(Actor.anonymous(), METHOD_NONE,
                        "not signed in: local test users are switched off (LOCAL_TEST_USERS=false)")
    name = header(DEMO_USER_HEADER) or cookies.get(DEMO_USER_COOKIE)
    if not name:
        return Identity(Actor.anonymous(), METHOD_NONE, "not signed in: pick a demo user")
    user = local_test_user(name)
    if user is None:
        return Identity(Actor.anonymous(), METHOD_NONE,
                        f"not signed in: unknown demo user (known: {', '.join(LOCAL_TEST_USERS)})")
    return Identity(user.actor(), METHOD_LOCAL)


def encode_client_principal(claims: list[tuple[str, str]], *, auth_typ: str = "aad",
                            name_typ: str = "name",
                            role_typ: str = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role") -> str:
    """Build an ``X-MS-CLIENT-PRINCIPAL`` value (tests and local experiments; Azure builds the real one)."""
    data = {"auth_typ": auth_typ, "name_typ": name_typ, "role_typ": role_typ,
            "claims": [{"typ": typ, "val": val} for typ, val in claims]}
    return base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
