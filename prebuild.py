import requests
import os
import json
import argparse
import shutil
from semver import Version
from typing import Optional
from datetime import datetime

GITHUB_REPO = 'https://github.com/informatics-sa/WebsiteBuilder'

parser = argparse.ArgumentParser()
parser.add_argument("--path", required=False)
args = parser.parse_args()


def repo_to_api(github_repo: str) -> str:
    # Convert https://github.com/owner/repo → https://api.github.com/repos/owner/repo
    parts = github_repo.rstrip('/').split('github.com/')[-1]
    return f"https://api.github.com/repos/{parts}"

def list_files(*, ref: str, path: str = "") -> list[dict]:
    """Recursively list all files for a certain revision, returns list of {path, download_url}."""
    api_url = f"{repo_to_api(GITHUB_REPO)}/contents/{path}?ref={ref}"
    response = requests.get(api_url)
    response.raise_for_status()

    files = []
    for item in response.json():
        if item['type'] == 'file':
            files.append({'path': item['path'], 'download_url': item['download_url']})
        elif item['type'] == 'dir':
            files.extend(list_files(ref = ref, path = item['path']))
    return files

def download_file(file: dict, dest_dir: str) -> None:
    """Download a single file and save it relative to dest_dir."""
    response = requests.get(file['download_url'])
    response.raise_for_status()

    dest_path = os.path.join(dest_dir, file['path'])
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    with open(dest_path, 'wb') as f:
        f.write(response.content)
    print(f"[COPY] {file['path']}")


def version_requirement():
    def _assert(cond: bool, message: str):
        if not cond:
            print(f"[ERROR] malformed `settings.json`; {message}.")
            print("[NOTE] refer to https://template.sainformatics.org/data/")
            exit(1)

    with open('./root/data/settings.json', 'r') as file:
        settings = json.load(file)

    _assert(not ("version" in settings and "website_builder" in settings),
           "cannot specify both `version` and `website_builder` at the same time")

    if "version" in settings:
        version_string = settings['version'].lstrip("vV")
        _assert(Version.is_valid(version_string), "`version` is not a valid Semantic Version")
        version = Version.parse(version_string)
        return {
            "type": "release",
            "version": version,
            "nightly": version.prerelease is not None
        }

    if "website_builder" in settings:
        _assert(len(settings['website_builder'].keys() & {"version", "branch", "commit"}) == 1,
               "`website_builder` needs to contain exactly one of `version`, `branch` or `commit`")

        if "version" in settings["website_builder"]:
            version_string = settings['website_builder']['version'].lstrip("vV")
            _assert(Version.is_valid(version_string),
                   "`website_builder.version` is not a valid Semantic Version")
            version = Version.parse(version_string)
            if not settings['website_builder'].get('nightly', True) and version.prerelease:
                print("[WARNING] `website_builder.nightly` is turned off while specifying a prerelease `website_builder.version`; this will likely result in resolution failure.")
            return {
                "type": "release",
                "version": version,
                "nightly": settings["website_builder"].get("nightly", version.prerelease is not None)
            }

        if "branch" in settings["website_builder"]:
            _assert("nightly" not in settings["website_builder"],
                   "`website_builder` with 'branch' cannot specify `nightly`")
            return {
                "type": "branch",
                "branch": settings["website_builder"]["branch"]
            }

        if "commit" in settings["website_builder"]:
            _assert("nightly" not in settings["website_builder"],
                   "`website_builder` with 'commit' cannot specify `nightly`")
            return {
                "type": "commit",
                "revision": settings["website_builder"]["commit"]
            }
        # Unreachable; `website_builder` already asserted to contain at least one of the above cases.

    return { # by default
        "type": "branch",
        "branch": "main"
    }

def resolve_dependency() -> Optional[str]:
    requirement = version_requirement()
    api_base_url     = repo_to_api(GITHUB_REPO)

    match requirement['type']:
        case "release":
            res = requests.get(f"{api_base_url}/releases")
            if not res.ok: return None

            releases = res.json()
            if not requirement['nightly']:
                releases = filter(lambda release: not release['prerelease'], releases)
            releases = filter(lambda release: Version.is_valid(release['tag_name'].lstrip("vV"))
                              and requirement['version'].is_compatible(Version.parse(release['tag_name'].lstrip("vV"))), releases)
            # TODO: do we want to parse the datetime? ISO 8601 sorting works without parsing the date, but only if the timezone is consistent. is it consistent with Github APIs?
            releases = sorted(releases, key=lambda release: (Version.parse(release['tag_name'].lstrip("vV")), datetime.fromisoformat(release['published_at'])))

            releases = list(releases)
            if len(releases) == 0: return None
            selected_release = releases[-1]

            res = requests.get(f"{api_base_url}/tags")
            if not res.ok:
                print("[ERROR] unexpected: no available tags while there is at least one release; this is a github bug.")
                exit(1)

            tags = res.json()
            tags = dict(map(lambda tag: (tag['name'], tag), tags))

            return tags[selected_release['tag_name']]['commit']['sha']

        case "branch":
            res = requests.get(f"{api_base_url}/branches/{requirement['branch']}")
            if not res.ok: return None

            return res.json()['commit']['sha']

        case "commit":
            res = requests.get(f"{api_base_url}/commits/{requirement['revision']}")
            if not res.ok: return None

            return requirement["revision"]



def main():
    dest_dir = os.path.dirname(os.path.abspath(__file__))

    print(f"[INFO] Destination: {dest_dir}")

    if args.path:
        print("[INFO] skipping dependency resolution; using local WebsiteBuilder.")
        print(f"[INFO] Listing files from {args.path} [local] ...")

        shutil.copytree(args.path, dest_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "README.md")) # copy from args.path to dest_dir
    else:
        revision = resolve_dependency()
        if revision is None:
            requirement = version_requirement()
            match requirement['type']:
                case "release":
                    failure_message = f"couldn't find a release matching the constraints: {str(requirement['version'])} and {'nightly' if requirement['nightly'] else 'stable'}"
                case "branch":
                    failure_message = f"couldn't find branch: {requirement['branch']}"
                case "commit":
                    failure_message = f"couldn't find commit with hash: {requirement['revision']}"
            print(f"[ERROR] dependency resolution failed; {failure_message}.")
            exit(1)

        print(f"[INFO] Listing files from {GITHUB_REPO} [github] ...")

        files = list_files(ref = revision)
        print(f"[INFO] Found {len(files)} file(s)\n")

        for f in files:
            if os.path.basename(f["path"]).lower() == "readme.md": continue
            download_file(f, dest_dir)

    print(f"\n[DONE]")

if __name__ == '__main__':
    main()
