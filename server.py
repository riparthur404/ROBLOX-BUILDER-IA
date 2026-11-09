"""Serveur MCP Roblox (Open Cloud) : Data Stores, messages, scripts, publication.

Sécurité intégrée : modes lecture seule / autorisations par flags, limite d'écritures
par minute, sauvegarde automatique de chaque script avant modification, journal
d'audit, et publication limitée au dossier builds/.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP

try:
    from mcp.server.transport_security import TransportSecuritySettings

    EXTRA = {
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    }
except ImportError:
    EXTRA = {}

ROOT = os.path.dirname(os.path.abspath(__file__))


def flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "oui")


API_KEY = os.environ["ROBLOX_API_KEY"]
UNIVERSE_ID = os.environ["ROBLOX_UNIVERSE_ID"]
PLACE_ID = os.environ.get("ROBLOX_PLACE_ID", "")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")

READ_ONLY = flag("READ_ONLY")                    # 1 = aucune écriture possible
ALLOW_SCRIPT_EDIT = flag("ALLOW_SCRIPT_EDIT", "1")
ALLOW_PUBLISH = flag("ALLOW_PUBLISH")            # publish_place
ALLOW_DELETE = flag("ALLOW_DELETE")              # datastore_delete
ALLOW_LUAU = flag("ALLOW_LUAU")                  # run_luau (expérimental)
MAX_WRITES_PER_MIN = int(os.environ.get("MAX_WRITES_PER_MIN", "20"))

BUILDS_DIR = os.path.join(ROOT, "builds")
BACKUP_DIR = os.path.join(ROOT, "backups")
AUDIT_FILE = os.path.join(ROOT, "audit.log")
os.makedirs(BUILDS_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

mcp = FastMCP(
    "roblox",
    host="127.0.0.1",
    port=int(os.environ.get("PORT", "8000")),
    streamable_http_path=MCP_PATH,
    json_response=True,
    stateless_http=True,
    **EXTRA,
)

H = {"x-api-key": API_KEY}
BASE = "https://apis.roblox.com"
PLACE_URL = f"{BASE}/cloud/v2/universes/{UNIVERSE_ID}/places/{PLACE_ID}"
DS_URL = f"{BASE}/datastores/v1/universes/{UNIVERSE_ID}/standard-datastores"
SCRIPT_TYPES = ("Script", "LocalScript", "ModuleScript")
MAX_SOURCE_BYTES = 200_000  # limite de l'API Engine Instances

_writes: list = []


# ---------------------------------------------------------------- utilitaires
def audit(tool: str, target: str, result: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {tool} {target} -> {result[:80]}\n"
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def gate(allowed: bool, name: str):
    """Renvoie un message de refus, ou None si l'écriture est autorisée."""
    if READ_ONLY:
        return "Refusé : le serveur est en lecture seule (READ_ONLY=1)."
    if not allowed:
        return f"Refusé : {name} est désactivé dans config.env."
    now = time.time()
    _writes[:] = [t for t in _writes if now - t < 60]
    if len(_writes) >= MAX_WRITES_PER_MIN:
        return "Refusé : trop d'écritures par minute, réessaie plus tard."
    _writes.append(now)
    return None


def req(method: str, url: str, **kw):
    try:
        return httpx.request(method, url, headers={**H, **kw.pop("headers", {})}, timeout=kw.pop("timeout", 60), **kw)
    except httpx.HTTPError as e:
        return f"Erreur réseau : {e}"


def fmt(x, limit: int = 100_000) -> str:
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s[:limit]


def plain(method: str, url: str, **kw) -> str:
    r = req(method, url, **kw)
    return r if isinstance(r, str) else f"{r.status_code}: {r.text[:100_000]}"


def operation(method: str, url: str, **kw):
    """Appel Engine Instances : renvoie le contenu 'response' (dict) ou un texte d'erreur."""
    r = req(method, url, **kw)
    if isinstance(r, str):
        return r
    if r.status_code not in (200, 202):
        return f"{r.status_code}: {r.text[:2000]}"
    data = r.json()
    if data.get("done"):
        return data.get("response", data)
    path = data.get("path")
    if not path:
        return data
    for _ in range(15):
        time.sleep(2)
        p = req("GET", f"{BASE}/cloud/v2/{path}")
        if isinstance(p, str):
            return p
        if p.status_code != 200:
            return f"{p.status_code}: {p.text[:2000]}"
        d = p.json()
        if d.get("done"):
            if "error" in d:
                return f"Erreur : {json.dumps(d['error'], ensure_ascii=False)}"
            return d.get("response", d)
    return "Délai dépassé : l'opération n'est pas terminée."


def children_page(instance_id: str, size: int = 100, token: str = ""):
    params = {"maxPageSize": size}
    if token:
        params["pageToken"] = token
    return operation("GET", f"{PLACE_URL}/instances/{instance_id}:listChildren", params=params)


def script_type_of(details: dict) -> str:
    for t in SCRIPT_TYPES:
        if t in (details or {}):
            return t
    return ""


def get_instance_raw(instance_id: str):
    return operation("GET", f"{PLACE_URL}/instances/{instance_id}")


def do_update(instance_id: str, stype: str, source: str) -> str:
    body = {"engineInstance": {"Details": {stype: {"Source": source}}}}
    res = operation("PATCH", f"{PLACE_URL}/instances/{instance_id}", json=body)
    return fmt(res, 2000)


# ------------------------------------------------------------ infos du jeu
@mcp.tool()
def get_universe_info() -> str:
    """Infos de l'univers (nom, visibilité...). Peut demander une permission de lecture de l'univers."""
    return plain("GET", f"{BASE}/cloud/v2/universes/{UNIVERSE_ID}")


@mcp.tool()
def get_place_info() -> str:
    """Infos de la place configurée (nom, description...)."""
    return plain("GET", PLACE_URL)


# ------------------------------------------------------------- Data Stores
@mcp.tool()
def datastore_list(prefix: str = "", limit: int = 50) -> str:
    """Liste les Data Stores de l'univers."""
    return plain("GET", DS_URL, params={"prefix": prefix, "limit": min(limit, 100)})


@mcp.tool()
def datastore_list_entries(datastore: str, prefix: str = "", limit: int = 50) -> str:
    """Liste les clés d'un Data Store."""
    return plain(
        "GET", f"{DS_URL}/datastore/entries",
        params={"datastoreName": datastore, "prefix": prefix, "limit": min(limit, 100)},
    )


@mcp.tool()
def datastore_get(datastore: str, key: str) -> str:
    """Lit une entrée d'un Data Store."""
    return plain(
        "GET", f"{DS_URL}/datastore/entries/entry",
        params={"datastoreName": datastore, "entryKey": key},
    )


@mcp.tool()
def datastore_set(datastore: str, key: str, value_json: str) -> str:
    """Écrit une entrée dans un Data Store (value_json = JSON en texte)."""
    refus = gate(True, "datastore_set")
    if refus:
        return refus
    try:
        json.loads(value_json)
    except ValueError:
        return "value_json n'est pas du JSON valide."
    out = plain(
        "POST", f"{DS_URL}/datastore/entries/entry",
        params={"datastoreName": datastore, "entryKey": key},
        headers={"content-type": "application/json"}, content=value_json,
    )
    audit("datastore_set", f"{datastore}/{key}", out)
    return out


@mcp.tool()
def datastore_delete(datastore: str, key: str) -> str:
    """Supprime une entrée d'un Data Store (désactivé par défaut : ALLOW_DELETE=1)."""
    refus = gate(ALLOW_DELETE, "ALLOW_DELETE")
    if refus:
        return refus
    out = plain(
        "DELETE", f"{DS_URL}/datastore/entries/entry",
        params={"datastoreName": datastore, "entryKey": key},
    )
    audit("datastore_delete", f"{datastore}/{key}", out)
    return out


# -------------------------------------------------------------- Messaging
@mcp.tool()
def send_message(topic: str, message: str) -> str:
    """Envoie un message à tous les serveurs du jeu (Messaging Service)."""
    refus = gate(True, "send_message")
    if refus:
        return refus
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", topic):
        return "Sujet invalide (lettres, chiffres, _ et - uniquement)."
    if len(message.encode("utf-8")) > 1024:
        return "Message trop long (1 Ko maximum)."
    out = plain(
        "POST", f"{BASE}/messaging-service/v1/universes/{UNIVERSE_ID}/topics/{topic}",
        json={"message": message},
    )
    audit("send_message", topic, out)
    return out


# ---------------------------------------------------------------- Scripts
@mcp.tool()
def list_children(instance_id: str = "root", page_token: str = "") -> str:
    """Liste les enfants d'un objet du jeu ('root' = racine) : nom, id, type de script.
    Demande une session Team Create active."""
    res = children_page(instance_id, 100, page_token)
    if isinstance(res, str):
        return res
    items = []
    for it in res.get("instances", []):
        e = it.get("engineInstance", {})
        items.append({
            "name": e.get("Name"),
            "id": e.get("Id"),
            "hasChildren": it.get("hasChildren"),
            "script": script_type_of(e.get("Details", {})) or None,
        })
    return fmt({"instances": items, "nextPageToken": res.get("nextPageToken", "")})


@mcp.tool()
def resolve_path(path: str) -> str:
    """Trouve l'id d'un objet à partir de son chemin, ex. 'ServerScriptService/Main'."""
    current = "root"
    found = None
    for name in [p for p in path.split("/") if p]:
        found, token = None, ""
        while True:
            res = children_page(current, 100, token)
            if isinstance(res, str):
                return res
            for it in res.get("instances", []):
                e = it.get("engineInstance", {})
                if e.get("Name") == name:
                    found = e
                    break
            token = res.get("nextPageToken", "")
            if found or not token:
                break
        if not found:
            return f"Introuvable : '{name}' dans '{path}'."
        current = found.get("Id")
    return fmt({"id": current, "name": found.get("Name") if found else None,
                "script": script_type_of(found.get("Details", {})) if found else None})


@mcp.tool()
def get_instance(instance_id: str) -> str:
    """Lit un objet du jeu. Pour un script, renvoie aussi son code (Source)."""
    res = get_instance_raw(instance_id)
    return res if isinstance(res, str) else fmt(res)


@mcp.tool()
def update_script_source(instance_id: str, new_source: str, script_type: str = "") -> str:
    """Remplace le code d'un script EXISTANT. Le script actuel est sauvegardé dans backups/ avant.
    script_type (Script, LocalScript, ModuleScript) est détecté automatiquement si vide."""
    refus = gate(ALLOW_SCRIPT_EDIT, "ALLOW_SCRIPT_EDIT")
    if refus:
        return refus
    if len(new_source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return "Script trop long (200 000 octets maximum pour l'API)."
    cur = get_instance_raw(instance_id)
    if isinstance(cur, str):
        return f"Lecture impossible, modification annulée : {cur}"
    eng = cur.get("engineInstance", {})
    detected = script_type_of(eng.get("Details", {}))
    stype = script_type or detected
    if stype not in SCRIPT_TYPES:
        return "Cet objet n'est pas un script (ou son type est inconnu)."
    old = (eng.get("Details", {}).get(detected, {}) or {}).get("Source", "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    fname = f"{instance_id}__{stype}__{stamp}.lua"
    with open(os.path.join(BACKUP_DIR, fname), "w", encoding="utf-8") as f:
        f.write(old)
    out = do_update(instance_id, stype, new_source)
    audit("update_script_source", f"{eng.get('Name')} ({instance_id}) backup={fname}", out)
    return f"Sauvegarde : {fname}\nRésultat : {out}"


@mcp.tool()
def list_backups() -> str:
    """Liste les sauvegardes de scripts (backups/), les plus récentes d'abord."""
    files = sorted(os.listdir(BACKUP_DIR), reverse=True)
    return fmt(files[:50]) if files else "Aucune sauvegarde."


@mcp.tool()
def restore_backup(filename: str) -> str:
    """Remet un script à l'état d'une sauvegarde (nom exact donné par list_backups)."""
    m = re.fullmatch(r"([0-9a-fA-F-]{8,64})__(Script|LocalScript|ModuleScript)__\d{8}-\d{6,12}\.lua", filename)
    if not m:
        return "Nom de sauvegarde invalide."
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.isfile(path):
        return "Sauvegarde introuvable."
    with open(path, encoding="utf-8") as f:
        source = f.read()
    return update_script_source(m.group(1), source, m.group(2))


# ----------------------------------------------------------- Publication
@mcp.tool()
def publish_place(rbxl_filename: str) -> str:
    """Publie un .rbxl placé dans le dossier builds/ : REMPLACE la version publiée du jeu.
    Désactivé par défaut (ALLOW_PUBLISH=1)."""
    refus = gate(ALLOW_PUBLISH, "ALLOW_PUBLISH")
    if refus:
        return refus
    name = os.path.basename(rbxl_filename)
    path = os.path.join(BUILDS_DIR, name)
    if not name.lower().endswith(".rbxl") or not os.path.isfile(path):
        return "Fichier .rbxl introuvable dans builds/."
    if os.path.getsize(path) > 100 * 1024 * 1024:
        return "Fichier trop gros (100 Mo maximum)."
    with open(path, "rb") as f:
        data = f.read()
    out = plain(
        "POST", f"{BASE}/universes/v1/universes/{UNIVERSE_ID}/places/{PLACE_ID}/versions",
        params={"versionType": "Published"},
        headers={"content-type": "application/octet-stream"}, content=data, timeout=120,
    )
    audit("publish_place", name, out)
    return out


# --------------------------------------------- Luau Execution (expérimental)
@mcp.tool()
def run_luau(script: str) -> str:
    """EXPÉRIMENTAL : exécute du Luau dans une session serveur de la place (Luau Execution API).
    Désactivé par défaut (ALLOW_LUAU=1). Demande la permission luau-execution-session:write."""
    refus = gate(ALLOW_LUAU, "ALLOW_LUAU")
    if refus:
        return refus
    r = req("POST", f"{PLACE_URL}/luau-execution-session-tasks", json={"script": script})
    if isinstance(r, str):
        return r
    if r.status_code not in (200, 201, 202):
        return f"{r.status_code}: {r.text[:2000]}"
    path = r.json().get("path")
    audit("run_luau", path or "?", "lancé")
    for _ in range(30):
        time.sleep(2)
        p = req("GET", f"{BASE}/cloud/v2/{path}")
        if isinstance(p, str):
            return p
        d = p.json()
        if d.get("state") not in (None, "QUEUED", "PROCESSING", "STATE_UNSPECIFIED"):
            return fmt(d, 20_000)
    return "Délai dépassé : la tâche n'est pas terminée."


# ----------------------------------------------------------------- Audit
@mcp.tool()
def read_audit_log(lines: int = 30) -> str:
    """Affiche les dernières actions d'écriture effectuées par le serveur."""
    if not os.path.isfile(AUDIT_FILE):
        return "Journal vide."
    with open(AUDIT_FILE, encoding="utf-8") as f:
        return "".join(f.readlines()[-min(lines, 200):])


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
