# ============================================================
# run.py : serveur MCP « Roblox studios » (version builder, avec ngrok)
#
# Outils :
#   ping            test de connexion
#   run_luau        exécute du Luau dans le jeu et sauvegarde (builder principal)
#   inspect_game    affiche l'arborescence du jeu (Workspace, scripts, etc.)
#   write_script    crée / modifie un Script, LocalScript ou ModuleScript
#   read_script     lit le code d'un script
#   datastore_get / datastore_set / publish_place / send_message
#
# Variables d'environnement :
#   ROBLOX_API_KEY    (obligatoire) clé Open Cloud du Creator Hub
#   NGROK_AUTHTOKEN   (obligatoire pour lancer ngrok depuis ce script)
#   NGROK_DOMAIN      (optionnel)   domaine ngrok fixe
#   PANEL_PASSWORD    (obligatoire pour le panneau web /panel)
#   MCP_SECRET        (optionnel)   chemin secret ; sinon généré une fois dans mcp_secret.txt
# Paquets Python : mcp, ngrok
# Permissions de la clé API : Luau Execution (lecture + écriture), Places (écriture),
#   Data Stores (lecture + écriture), Messaging Service (publication)
# ============================================================
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP
from roblox_cloud_tools import register
_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")
if os.path.exists(_cfg):
    for _l in open(_cfg, encoding="utf-8"):
        if "=" in _l and not _l.lstrip().startswith("#"):
            _k, _v = _l.strip().split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
UNIVERSE_ID = "6520742564"
PLACE_ID = "119764430403494"
PORT = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or 8000)
API = "https://apis.roblox.com"


# ---------- chemin secret fixe ----------
def _load_secret():
    env = os.environ.get("MCP_SECRET", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "mcp_secret.txt")
    if os.path.exists(path):
        value = open(path, encoding="utf-8").read().strip()
        if value:
            return value
    value = secrets.token_hex(16)
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    return value


SECRET = _load_secret()

mcp = FastMCP(
    "Roblox studios",
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=f"/{SECRET}/mcp",
    stateless_http=True,
)


# ---------- HTTP Roblox ----------
def _api_key():
    key = os.environ.get("ROBLOX_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Variable d'environnement ROBLOX_API_KEY manquante.")
    return key


def _rbx(method, url, body=None, content_type="application/json", raw=False):
    headers = {"x-api-key": _api_key()}
    data = None
    if body is not None:
        headers["Content-Type"] = content_type
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} : {detail}")
    if raw:
        return text
    return json.loads(text) if text.strip() else {}


# ---------- Luau Execution ----------
def _lua_str(s):
    """Texte -> littéral Luau sûr (chaque octet en \\ddd, donc aucun souci de guillemets/accents)."""
    return '"' + "".join("\\%03d" % b for b in str(s).encode("utf-8")) + '"'


def _wrap_with_save(script):
    return (
        "local __ok, __res = pcall(function()\n"
        + script
        + "\nend)\n"
        "if not __ok then error(__res, 0) end\n"
        "local __s, __e = pcall(function() game:GetService('AssetService'):SavePlaceAsync() end)\n"
        "if not __s then return 'Script OK mais SAUVEGARDE ECHOUEE : ' .. tostring(__e) end\n"
        "if __res == nil then return 'OK et sauvegarde' end\n"
        "return __res\n"
    )


def _exec(script, wait_seconds=120):
    wait_seconds = max(10, min(int(wait_seconds), 280))
    base = f"{API}/cloud/v2"
    create_url = f"{base}/universes/{UNIVERSE_ID}/places/{PLACE_ID}/luau-execution-session-tasks"
    task = _rbx("POST", create_url, {"script": script, "timeout": f"{wait_seconds}s"})
    path = task["path"]

    deadline = time.time() + wait_seconds + 15
    while True:
        task = _rbx("GET", f"{base}/{path}")
        if task.get("state") not in ("QUEUED", "PROCESSING", "STATE_UNSPECIFIED"):
            break
        if time.time() > deadline:
            return {"state": "TIMEOUT_COTE_SERVEUR", "path": path}
        time.sleep(2)

    try:
        logs = _rbx("GET", f"{base}/{path}/logs")
        lines = []
        for chunk in logs.get("luauExecutionSessionTaskLogs", []):
            lines.extend(chunk.get("messages", []))
        logs_text = "\n".join(lines)
    except Exception as e:
        logs_text = f"(logs indisponibles : {e})"

    return {
        "state": task.get("state"),
        "output": (task.get("output") or {}).get("results"),
        "error": task.get("error"),
        "logs": logs_text,
    }


LUA_RESOLVE = """
local function resolve(path)
	local cur = game
	for part in string.gmatch(path, "[^%.]+") do
		local child = cur:FindFirstChild(part)
		if not child and cur == game then
			local ok, svc = pcall(function() return game:GetService(part) end)
			if ok then child = svc end
		end
		if not child then return nil end
		cur = child
	end
	return cur
end
"""


# ---------- outils builder ----------
@mcp.tool()
def ping() -> str:
    """Test de connexion : renvoie 'pong'."""
    return "pong"


@mcp.tool()
def run_luau(script: str, save: bool = True, wait_seconds: int = 120) -> str:
    """Exécute du code Luau dans le jeu Roblox (Open Cloud Luau Execution) : c'est l'outil
    pour CONSTRUIRE (terrain, parts, modèles, éclairage, scripts, UI...).
    save=True : sauvegarde automatiquement le jeu à la fin (si le script ne plante pas).
    Mets save=False si le script appelle déjà SavePlaceAsync lui-même.
    Le script peut finir par 'return "texte"' pour renvoyer un résultat.
    Renvoie l'état, la valeur retournée, l'erreur éventuelle et les logs (print).
    Limite Roblox : 5 tâches par minute."""
    code = _wrap_with_save(script) if save else script
    return json.dumps(_exec(code, wait_seconds), ensure_ascii=False)


@mcp.tool()
def inspect_game(path: str = "Workspace", depth: int = 2) -> str:
    """Affiche l'arborescence du jeu à partir de 'path' (ex. 'Workspace', 'ServerScriptService',
    'Workspace.ChillMap'), sur 'depth' niveaux (max 6, 400 lignes)."""
    depth = max(0, min(int(depth), 6))
    code = (
        LUA_RESOLVE
        + """
local root = resolve(__PATH__)
if not root then return "Introuvable : " .. __PATH__ end
local lines = {}
local function dump(inst, level, indent)
	if #lines >= 400 then return end
	local extra = ""
	if inst:IsA("LuaSourceContainer") then
		local ok, src = pcall(function() return inst.Source end)
		if ok then extra = " [" .. #src .. " car.]" end
	end
	local kids = inst:GetChildren()
	if level >= __DEPTH__ and #kids > 0 then extra = extra .. " (+" .. #kids .. " enfants)" end
	table.insert(lines, indent .. inst.Name .. " (" .. inst.ClassName .. ")" .. extra)
	if level < __DEPTH__ then
		for _, c in ipairs(kids) do dump(c, level + 1, indent .. "  ") end
	end
end
dump(root, 0, "")
return table.concat(lines, "\\n")
"""
    )
    code = code.replace("__PATH__", _lua_str(path)).replace("__DEPTH__", str(depth))
    return json.dumps(_exec(code, 60), ensure_ascii=False)


@mcp.tool()
def write_script(parent_path: str, name: str, source: str, script_type: str = "Script") -> str:
    """Crée ou remplace un script et sauvegarde le jeu.
    parent_path : ex. 'ServerScriptService', 'StarterPlayer.StarterPlayerScripts', 'Workspace.MaMap'.
    script_type : 'Script', 'LocalScript' ou 'ModuleScript'."""
    if script_type not in ("Script", "LocalScript", "ModuleScript"):
        raise ValueError("script_type doit être Script, LocalScript ou ModuleScript.")
    code = (
        LUA_RESOLVE
        + """
local parent = resolve(__PARENT__)
if not parent then error("Parent introuvable : " .. __PARENT__, 0) end
local s = parent:FindFirstChild(__NAME__)
if s and not s:IsA(__CLASS__) then s:Destroy() s = nil end
if not s then
	s = Instance.new(__CLASS__)
	s.Name = __NAME__
	s.Parent = parent
end
s.Source = __SOURCE__
return "Script écrit : " .. s:GetFullName()
"""
    )
    code = (
        code.replace("__PARENT__", _lua_str(parent_path))
        .replace("__NAME__", _lua_str(name))
        .replace("__CLASS__", _lua_str(script_type))
        .replace("__SOURCE__", _lua_str(source))
    )
    return json.dumps(_exec(_wrap_with_save(code), 120), ensure_ascii=False)


@mcp.tool()
def read_script(path: str) -> str:
    """Lit le code d'un script (ex. 'ServerScriptService.Main')."""
    code = (
        LUA_RESOLVE
        + """
local s = resolve(__PATH__)
if not s then return "Introuvable : " .. __PATH__ end
if not s:IsA("LuaSourceContainer") then return "Ce n'est pas un script : " .. s.ClassName end
return s.Source
"""
    )
    code = code.replace("__PATH__", _lua_str(path))
    return json.dumps(_exec(code, 60), ensure_ascii=False)


# ---------- outils Open Cloud classiques ----------
@mcp.tool()
def datastore_get(datastore_name: str, entry_key: str) -> str:
    """Lit une entrée d'un Data Store (scope global) et renvoie sa valeur en JSON."""
    ds = urllib.parse.quote(datastore_name, safe="")
    key = urllib.parse.quote(entry_key, safe="")
    url = f"{API}/cloud/v2/universes/{UNIVERSE_ID}/data-stores/{ds}/entries/{key}"
    return json.dumps(_rbx("GET", url), ensure_ascii=False)


@mcp.tool()
def datastore_set(datastore_name: str, entry_key: str, value: str) -> str:
    """Écrit une entrée dans un Data Store (scope global). 'value' est du JSON (ou du texte simple)."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value
    ds = urllib.parse.quote(datastore_name, safe="")
    key = urllib.parse.quote(entry_key, safe="")
    url = f"{API}/cloud/v2/universes/{UNIVERSE_ID}/data-stores/{ds}/entries/{key}?allowMissing=true"
    return json.dumps(_rbx("PATCH", url, {"value": parsed}), ensure_ascii=False)


@mcp.tool()
def publish_place(file_path: str, version_type: str = "Published") -> str:
    """Publie un fichier .rbxl du serveur comme nouvelle version du place.
    version_type : 'Published' (visible par les joueurs) ou 'Saved' (brouillon)."""
    if version_type not in ("Published", "Saved"):
        raise ValueError("version_type doit être 'Published' ou 'Saved'.")
    with open(file_path, "rb") as f:
        content = f.read()
    url = f"{API}/universes/v1/{UNIVERSE_ID}/places/{PLACE_ID}/versions?versionType={version_type}"
    return _rbx("POST", url, content, content_type="application/octet-stream", raw=True)


@mcp.tool()
def send_message(topic: str, message: str) -> str:
    """Envoie un message à tous les serveurs en cours du jeu (Messaging Service)."""
    t = urllib.parse.quote(topic, safe="")
    _rbx("POST", f"{API}/messaging-service/v1/universes/{UNIVERSE_ID}/topics/{t}", {"message": message}, raw=True)
    return "Message envoyé."



# ---------- PANNEAU WEB (même serveur, même tunnel ngrok) ----------
# Adresse : <URL ngrok>/<chemin secret>/panel   (protégé par PANEL_PASSWORD)
import asyncio
import hmac

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

PANEL_HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panneau Roblox</title>
<style>
:root{color-scheme:dark}
body{margin:0;padding:16px;font:16px -apple-system,system-ui,sans-serif;background:#0f1320;color:#e8ecf5}
h1{font-size:20px;margin:0 0 12px}
section{background:#181e30;border-radius:12px;padding:12px;margin-bottom:12px}
h2{font-size:15px;margin:0 0 8px;color:#8fb4ff}
input,textarea{width:100%;box-sizing:border-box;font-size:16px;padding:10px;margin:4px 0;border-radius:8px;border:1px solid #2c3550;background:#0f1320;color:#e8ecf5}
textarea{min-height:140px;font-family:ui-monospace,monospace}
button{font-size:16px;padding:10px 14px;margin:4px 6px 4px 0;border:0;border-radius:8px;background:#3d6bff;color:#fff}
pre{white-space:pre-wrap;word-break:break-word;background:#0b0e18;padding:10px;border-radius:8px;min-height:60px;margin:0}
label{font-size:14px}
</style></head><body>
<h1>Panneau Roblox</h1>
<section><h2>Mot de passe</h2><input id="pwd" type="password" placeholder="PANEL_PASSWORD" autocomplete="current-password"></section>
<section><h2>Connexion</h2><button onclick="call('ping')">Tester</button></section>
<section><h2>Inspecter le jeu</h2>
<input id="ipath" value="Workspace"><button onclick="call('inspect',{path:ipath.value,depth:2})">Voir</button></section>
<section><h2>Message aux serveurs</h2>
<input id="topic" placeholder="Sujet (topic)"><input id="msg" placeholder="Message">
<button onclick="call('message',{topic:topic.value,message:msg.value})">Envoyer</button></section>
<section><h2>Exécuter du Luau</h2>
<textarea id="lua" placeholder="print('salut')"></textarea>
<label><input id="save" type="checkbox" checked style="width:auto"> Sauvegarder le jeu après</label><br>
<button onclick="call('luau',{script:lua.value,save:save.checked})">Exécuter</button></section>
<section><h2>Résultat</h2><pre id="out">—</pre></section>
<script>
const out=document.getElementById('out'),pwd=document.getElementById('pwd');
pwd.value=sessionStorage.getItem('pwd')||'';
async function call(action,payload){
  sessionStorage.setItem('pwd',pwd.value);
  out.textContent='En cours…';
  try{
    const r=await fetch(location.pathname.replace(/\/$/,'')+'/api/'+action,{
      method:'POST',
      headers:{'Content-Type':'application/json','x-panel-password':pwd.value},
      body:JSON.stringify(payload||{})});
    const j=await r.json();
    out.textContent=j.error?('Erreur : '+j.error):(typeof j.result==='string'?j.result:JSON.stringify(j.result,null,2));
  }catch(e){out.textContent='Erreur réseau : '+e;}
}
</script></body></html>"""


def _panel_auth_ok(request):
    expected = os.environ.get("PANEL_PASSWORD", "").strip()
    if not expected:
        return False, "Définis la variable PANEL_PASSWORD sur le serveur pour activer le panneau."
    given = request.headers.get("x-panel-password", "")
    if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        return False, "Mot de passe incorrect."
    return True, ""


@mcp.custom_route(f"/{SECRET}/panel", methods=["GET"])
async def panel_page(request: Request):
    return HTMLResponse(PANEL_HTML)


@mcp.custom_route(f"/{SECRET}/panel/api/{{action}}", methods=["POST"])
async def panel_api(request: Request):
    ok, why = _panel_auth_ok(request)
    if not ok:
        return JSONResponse({"error": why}, status_code=403)
    action = request.path_params.get("action", "")
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        if action == "ping":
            result = ping()
        elif action == "inspect":
            result = await asyncio.to_thread(inspect_game, str(data.get("path", "Workspace")), int(data.get("depth", 2)))
        elif action == "message":
            result = await asyncio.to_thread(send_message, str(data.get("topic", "")), str(data.get("message", "")))
        elif action == "luau":
            result = await asyncio.to_thread(run_luau, str(data.get("script", "")), bool(data.get("save", True)))
        else:
            return JSONResponse({"error": f"Action inconnue : {action}"}, status_code=404)
        return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------- démarrage + ngrok ----------
def _start_ngrok():
    token = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    if not token:
        print(f"[run.py] Pas de NGROK_AUTHTOKEN : lance ngrok toi-même vers le port {PORT}.")
        print(f"[run.py] Chemin à ajouter à ton URL ngrok : /{SECRET}/mcp  (panneau : /{SECRET}/panel)")
        return
    try:
        import ngrok
    except ImportError:
        print("[run.py] Paquet 'ngrok' absent : ajoute-le aux paquets Python du serveur.")
        return
    kwargs = {"authtoken": token}
    domain = os.environ.get("NGROK_DOMAIN", "").strip()
    if domain:
        kwargs["domain"] = domain
    listener = ngrok.forward(PORT, **kwargs)
    print("=" * 60)
    print("URL À COLLER DANS CLAUDE (copie-la d'un seul bloc, sans espace) :")
    print(f"{listener.url()}/{SECRET}/mcp")
    print("PANNEAU WEB (mot de passe = PANEL_PASSWORD) :")
    print(f"{listener.url()}/{SECRET}/panel")
    print("=" * 60)
register(mcp)


if __name__ == "__main__":
    _start_ngrok()
    mcp.run(transport="streamable-http")