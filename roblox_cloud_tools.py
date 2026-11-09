"""
Outils Open Cloud pour le serveur MCP Roblox (à ajouter à côté de run.py).

Variables d'environnement à définir sur le VPS :
  ROBLOX_API_KEY      clé API du Creator Hub
  ROBLOX_UNIVERSE_ID  ID de l'univers (l'expérience)
  ROBLOX_USER_ID      ton ID utilisateur (pour l'envoi de modèles 3D)

Permissions de la clé API :
  - Universe : universe:write  (visibilité, nom, description, redémarrage)
  - Assets : lecture + écriture (envoi de modèles)
  - Creator Store / Toolbox : à vérifier dans le Creator Hub pour la recherche
  - Game Passes : game-pass:read + game-pass:write

Installation : pip install requests

Branchement dans run.py (si tu utilises FastMCP) :
    from roblox_cloud_tools import register
    register(mcp)
"""

import json
import os
import time

import requests

BASE = "https://apis.roblox.com"


def _entetes(json_body=False):
    entetes = {"x-api-key": os.environ["ROBLOX_API_KEY"]}
    if json_body:
        entetes["Content-Type"] = "application/json"
    return entetes


def _universe():
    return os.environ["ROBLOX_UNIVERSE_ID"]


def _reponse(r):
    try:
        corps = r.json()
    except ValueError:
        corps = r.text
    return {"statut": r.status_code, "ok": r.ok, "reponse": corps}


def modifier_univers(champs):
    """PATCH sur l'univers : champs = {"visibility": "PUBLIC"} par exemple."""
    r = requests.patch(
        f"{BASE}/cloud/v2/universes/{_universe()}",
        params={"updateMask": ",".join(champs.keys())},
        headers=_entetes(json_body=True),
        json=champs,
        timeout=30,
    )
    return _reponse(r)


def set_visibility(public):
    return modifier_univers({"visibility": "PUBLIC" if public else "PRIVATE"})


def restart_servers():
    # Ne marche que si une nouvelle version du jeu a été publiée.
    r = requests.post(
        f"{BASE}/cloud/v2/universes/{_universe()}:restartServers",
        headers=_entetes(json_body=True),
        json={},
        timeout=30,
    )
    return _reponse(r)


def update_experience(nom=None, description=None):
    champs = {}
    if nom:
        champs["displayName"] = nom
    if description:
        champs["description"] = description
    if not champs:
        return {"ok": False, "reponse": "Donne un nom et/ou une description."}
    return modifier_univers(champs)


def search_creator_store(mot_cle, categorie="Model"):
    """Recherche dans le Creator Store (Model, Audio, Decal, Plugin, ...)."""
    r = requests.get(
        f"{BASE}/toolbox-service/v2/assets:search",
        params={"searchCategoryType": categorie, "query": mot_cle},
        headers=_entetes(),
        timeout=30,
    )
    return _reponse(r)


def upload_model(chemin_fichier, nom, description="", type_mime="model/gltf-binary"):
    """Envoie un .glb/.gltf/.fbx comme Model, attend le résultat, renvoie l'ID de l'asset."""
    requete = {
        "assetType": "Model",
        "displayName": nom,
        "description": description,
        "creationContext": {"creator": {"userId": os.environ["ROBLOX_USER_ID"]}},
    }
    with open(chemin_fichier, "rb") as fichier:
        r = requests.post(
            f"{BASE}/assets/v1/assets",
            headers=_entetes(),
            data={"request": json.dumps(requete)},
            files={"fileContent": (os.path.basename(chemin_fichier), fichier, type_mime)},
            timeout=120,
        )
    if not r.ok:
        return _reponse(r)

    operation = r.json().get("path")  # "operations/<id>"
    for _ in range(30):
        time.sleep(2)
        o = requests.get(f"{BASE}/assets/v1/{operation}", headers=_entetes(), timeout=30).json()
        if o.get("done"):
            return {"ok": "response" in o, "resultat": o}
    return {"ok": False, "resultat": "Délai dépassé", "operation": operation}


def _formulaire(champs):
    """Force l'envoi en multipart/form-data ; ignore les champs None."""
    formulaire = {}
    for cle, valeur in champs.items():
        if valeur is None:
            continue
        texte = str(valeur).lower() if isinstance(valeur, bool) else str(valeur)
        formulaire[cle] = (None, texte)
    return formulaire


def list_game_passes():
    r = requests.get(
        f"{BASE}/game-passes/v1/universes/{_universe()}/game-passes/creator",
        headers=_entetes(),
        timeout=30,
    )
    return _reponse(r)


def create_game_pass(nom, description="", prix=None, en_vente=False):
    """Crée un game pass. Par défaut : PAS en vente, sans prix."""
    champs = {
        "name": nom,
        "description": description or None,
        "isForSale": en_vente,
        "price": prix,
    }
    r = requests.post(
        f"{BASE}/game-passes/v1/universes/{_universe()}/game-passes",
        headers=_entetes(),
        files=_formulaire(champs),
        timeout=60,
    )
    # Roblox peut répondre OK sans rien faire si le scope manque : on relit la liste.
    return {"ecriture": _reponse(r), "verification": list_game_passes()}


def update_game_pass(game_pass_id, nom=None, description=None, prix=None, en_vente=None):
    """Modifie seulement les champs fournis."""
    champs = {
        "name": nom,
        "description": description,
        "price": prix,
        "isForSale": en_vente,
    }
    r = requests.patch(
        f"{BASE}/game-passes/v1/universes/{_universe()}/game-passes/{game_pass_id}",
        headers=_entetes(),
        files=_formulaire(champs),
        timeout=60,
    )
    return {"ecriture": _reponse(r), "verification": list_game_passes()}


def register(mcp):
    """Déclare les outils sur une instance FastMCP."""

    @mcp.tool()
    def roblox_set_visibility(public: bool) -> dict:
        """Met le jeu en public (True) ou privé (False). Privé expulse les joueurs."""
        return set_visibility(public)

    @mcp.tool()
    def roblox_restart_servers() -> dict:
        """Redémarre les serveurs si une nouvelle version a été publiée."""
        return restart_servers()

    @mcp.tool()
    def roblox_update_experience(nom: str = "", description: str = "") -> dict:
        """Change le nom et/ou la description du jeu."""
        return update_experience(nom or None, description or None)

    @mcp.tool()
    def roblox_search_store(mot_cle: str, categorie: str = "Model") -> dict:
        """Cherche dans le Creator Store (Model, Audio, Decal, Plugin...)."""
        return search_creator_store(mot_cle, categorie)

    @mcp.tool()
    def roblox_upload_model(chemin_fichier: str, nom: str, description: str = "") -> dict:
        """Envoie un modèle 3D (.glb, .gltf, .fbx) vers Roblox."""
        return upload_model(chemin_fichier, nom, description)

    @mcp.tool()
    def roblox_list_game_passes() -> dict:
        """Liste les game passes du jeu."""
        return list_game_passes()

    @mcp.tool()
    def roblox_create_game_pass(
        nom: str, description: str = "", prix: int = 0, en_vente: bool = False
    ) -> dict:
        """Crée un game pass (pas en vente par défaut). Ne fixer un prix que sur demande."""
        return create_game_pass(nom, description, prix or None, en_vente)

    @mcp.tool()
    def roblox_update_game_pass(
        game_pass_id: str,
        nom: str = "",
        description: str = "",
        prix: int = 0,
        en_vente: bool | None = None,
    ) -> dict:
        """Modifie un game pass (seuls les champs remplis changent)."""
        return update_game_pass(
            game_pass_id, nom or None, description or None, prix or None, en_vente
        )
