import os
import sys
import shutil
import subprocess
import difflib
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import gitlab
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} requis")
    return value


def truncate_text(text: str, max_len: int = 15000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n... contenu tronqué ..."


if len(sys.argv) < 2:
    raise SystemExit("Usage: python patch_routes.py [report|apply]")

mode = sys.argv[1].strip().lower()
if mode not in {"report", "apply"}:
    raise SystemExit("Mode invalide. Utilise 'report' ou 'apply'")

if shutil.which("git") is None:
    raise SystemExit("Le binaire 'git' est introuvable dans l'image. Ajoute git au Dockerfile.")

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)

# ENV VARIABLES
PROJECT_ID = require_env("PROJECT_ID")
ENV = require_env("ENV")
URL_PUBLIC = require_env("URL_PUBLIC")

URL_INTRA = os.getenv("URL_INTRA", "").strip()
NODE_PORT_RAW = os.getenv("NODE_PORT", "32253").strip()
TARGET_BRANCH = os.getenv("TARGET_BRANCH", "roks").strip()
BRANCH_PREFIX = os.getenv("BRANCH_PREFIX", "sec/patch").strip()

GITLAB_URL = require_env("CI_SERVER_URL")
TOKEN = require_env("GITLAB_TOKEN")

try:
    NODE_PORT = int(NODE_PORT_RAW)
except ValueError:
    raise SystemExit(f"NODE_PORT invalide: {NODE_PORT_RAW}")

values_path = f"{ENV}/values/values.yml"

print("Projet GitLab :", PROJECT_ID)
print("Branche cible :", TARGET_BRANCH)
print("Mode :", mode.upper())
print("Environnement :", ENV)
print("Fichier :", values_path)

# GitLab connexion
gl = gitlab.Gitlab(GITLAB_URL, private_token=TOKEN)
project = gl.projects.get(PROJECT_ID)

# Vérifie que la branche cible existe
branches = [b.name for b in project.branches.list(get_all=True)]
if TARGET_BRANCH not in branches:
    raise SystemExit(f"Branche cible introuvable: {TARGET_BRANCH}")

repo_dir = Path("repo")

if repo_dir.exists():
    shutil.rmtree(repo_dir)

# Construction URL git avec token
repo_url = project.http_url_to_repo
parts = urlsplit(repo_url)

auth_repo_url = urlunsplit(
    (
        parts.scheme,
        f"oauth2:{TOKEN}@{parts.netloc}",
        parts.path,
        parts.query,
        parts.fragment,
    )
)

# Clone repo
run(["git", "clone", "-b", TARGET_BRANCH, auth_repo_url, "repo"])

# Sécurise remote pour push
run(["git", "remote", "set-url", "origin", auth_repo_url], cwd="repo")

file_path = repo_dir / values_path

if not file_path.exists():
    raise SystemExit(f"values.yml introuvable: {values_path}")

# Lecture YAML
with open(file_path, "r", encoding="utf-8") as f:
    original = f.read()

data = yaml.load(original)
if data is None:
    data = CommentedMap()

# SERVICE
service = data.setdefault("service", CommentedMap())

old_service_enabled = service.get("enabled", "absent")
old_service_type = service.get("type", "absent")
old_service_nodeport = service.get("nodePort", "absent")
old_http_port = service.get("httpPort", "absent")
old_https_port = service.get("httpsPort", "absent")

service["enabled"] = True
service.setdefault("httpPort", 80)
service.setdefault("httpsPort", 443)
service["nodePort"] = NODE_PORT
service["type"] = "ClusterIP"

# ROUTES
old_routes_enabled = "absent"
old_routes_count = 0

if "routes" in data and isinstance(data["routes"], dict):
    old_routes_enabled = data["routes"].get("enabled", "absent")
    existing_list = data["routes"].get("routesList", [])
    if isinstance(existing_list, list):
        old_routes_count = len(existing_list)

routes = CommentedMap()
routes["enabled"] = True

routes_list = CommentedSeq()


def build_route(url: str) -> CommentedMap:
    r = CommentedMap()
    r["url"] = url
    r["path"] = ""
    r["portName"] = "https"
    r["tlsTermination"] = "passthrough"
    return r


routes_list.append(build_route(URL_PUBLIC))

if URL_INTRA:
    routes_list.append(build_route(URL_INTRA))

routes["routesList"] = routes_list
data["routes"] = routes

# DISABLE INGRESS
old_ingress_enabled = "absent"
if "ingress" in data and isinstance(data["ingress"], dict):
    old_ingress_enabled = data["ingress"].get("enabled", "absent")
    data["ingress"]["enabled"] = False

# YAML PATCH
patched = Path("patched.yml")

with open(patched, "w", encoding="utf-8") as f:
    yaml.dump(data, f)

with open(patched, "r", encoding="utf-8") as f:
    new_content = f.read()

# DIFF
diff = list(
    difflib.unified_diff(
        original.splitlines(),
        new_content.splitlines(),
        fromfile=values_path,
        tofile=values_path,
        lineterm="",
    )
)

with open("diff.txt", "w", encoding="utf-8") as f:
    for line in diff:
        f.write(line + "\n")

# REPORT
report_lines = [
    "# Rapport de patch routes",
    "",
    f"Projet GitLab       : {PROJECT_ID}",
    f"Branche cible       : {TARGET_BRANCH}",
    f"Mode                : {mode.upper()}",
    "",
    "Environnements demandés :",
    f"- {ENV}",
    "",
    "Résultat :",
    "",
    f"[{ENV}]",
    f"Fichier : {values_path}",
    f"- service.enabled : {old_service_enabled} -> true",
    f"- service.type : {old_service_type} -> ClusterIP",
    f"- service.httpPort : {old_http_port} -> {service.get('httpPort')}",
    f"- service.httpsPort : {old_https_port} -> {service.get('httpsPort')}",
    f"- service.nodePort : {old_service_nodeport} -> {NODE_PORT}",
    f"- routes.enabled : {old_routes_enabled} -> true",
    f"- routes.routesList : {old_routes_count} -> {len(routes_list)} routes",
    "- routes prévues :",
    f"  - {URL_PUBLIC}",
]

if URL_INTRA:
    report_lines.append(f"  - {URL_INTRA}")

if old_ingress_enabled != "absent":
    report_lines.append(f"- ingress.enabled : {old_ingress_enabled} -> false")

report_content = "\n".join(report_lines) + "\n"

with open("report.md", "w", encoding="utf-8") as r:
    r.write(report_content)

# Affichage détaillé dans les logs
print("\n===== RAPPORT DETAILLE =====\n")
print(report_content)

print("\n===== DIFF =====\n")
if diff:
    print("\n".join(diff))
else:
    print("Aucun changement détecté.")

# DRY RUN
if mode == "report":
    print("\nDRY RUN terminé")
    sys.exit(0)

# APPLY
with open(file_path, "w", encoding="utf-8") as f:
    f.write(new_content)

# Branche aléatoire pour éviter les conflits
branch_suffix = uuid.uuid4().hex[:8]
branch = f"{BRANCH_PREFIX}-{ENV}-{branch_suffix}"

run(["git", "checkout", "-b", branch], cwd="repo")
run(["git", "config", "user.email", "gitlab-bot@dsk.lab"], cwd="repo")
run(["git", "config", "user.name", "ROKS Patch Bot"], cwd="repo")
run(["git", "add", values_path], cwd="repo")

# Si aucun changement réel, on sort proprement
diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd="repo")
if diff_check.returncode == 0:
    print("Aucun changement à commit. Fin du traitement.")
    sys.exit(0)

run(
    ["git", "commit", "-m", f"chore: migrate ingress to routes ({ENV})"],
    cwd="repo",
)

run(["git", "push", "-u", "origin", branch], cwd="repo")

mr_description = f"""## Contexte
Migration de la configuration `ingress` vers `routes` pour ROKS.

## Paramètres
- Projet GitLab : `{PROJECT_ID}`
- Branche cible : `{TARGET_BRANCH}`
- Environnement : `{ENV}`
- Fichier : `{values_path}`

## Changements
- `service.enabled` : `{old_service_enabled}` -> `true`
- `service.type` : `{old_service_type}` -> `ClusterIP`
- `service.httpPort` : `{old_http_port}` -> `{service.get('httpPort')}`
- `service.httpsPort` : `{old_https_port}` -> `{service.get('httpsPort')}`
- `service.nodePort` : `{old_service_nodeport}` -> `{NODE_PORT}`
- `routes.enabled` : `{old_routes_enabled}` -> `true`
- `routes.routesList` : `{old_routes_count}` -> `{len(routes_list)} routes`
"""

if old_ingress_enabled != "absent":
    mr_description += f"- `ingress.enabled` : `{old_ingress_enabled}` -> `false`\n"

mr_description += "\n## Routes prévues\n"
mr_description += f"- `{URL_PUBLIC}`\n"
if URL_INTRA:
    mr_description += f"- `{URL_INTRA}`\n"

mr_description += """
## Validation
Merci de vérifier le diff et le rendu Helm avant merge.

## Nettoyage
La branche source sera supprimée automatiquement après merge.
"""

# MR
mr = project.mergerequests.create(
    {
        "source_branch": branch,
        "target_branch": TARGET_BRANCH,
        "title": f"[infra] routes migration ({ENV})",
        "description": mr_description,
        "remove_source_branch": True,
    }
)

# Commentaire avec le rapport
mr.notes.create({"body": f"### Rapport détaillé\n```text\n{truncate_text(report_content)}\n```"})

# Commentaire avec le diff
with open("diff.txt", "r", encoding="utf-8") as f:
    diff_content = f.read()

diff_content = truncate_text(diff_content, 12000)
mr.notes.create({"body": f"### Diff preview\n```diff\n{diff_content}\n```"})

print("Merge Request créée :", mr.web_url)
print("Branche source supprimée automatiquement après merge : oui")
