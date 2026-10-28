"""Read a container's recipe revision from its matching OCI image tag."""

import base64
import re
import time
from urllib.parse import quote

import requests

from .doi_license import LicenseValidationError


REGISTRY = "https://ghcr.io"
DOCKER_HUB_REGISTRY = "https://registry-1.docker.io"
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
DOC_PATTERN = re.compile(
    r"^https://github\.com/neurodesk/neurocontainers/tree/"
    r"(?P<revision>[0-9a-f]{40})/recipes/(?P<recipe>[a-zA-Z0-9_.-]+)$",
    re.IGNORECASE,
)


class ProvenanceServiceError(RuntimeError):
    pass


class RegistryReader:
    def __init__(self, repository, github_token=None, registry=REGISTRY):
        self.repository = repository
        self.github_token = github_token
        self.registry = registry
        self.token = None

    def _request(self, path, accept):
        url = f"{self.registry}/v2/{self.repository}/{path}"
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = requests.get(url, headers=headers, timeout=(15, 60))
            if response.status_code == 401 and not self.token:
                challenge = response.headers.get("WWW-Authenticate", "")
                details = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
                realm = details.get("realm")
                if not realm or not realm.startswith("https://"):
                    raise ProvenanceServiceError("GHCR returned an unusable authentication challenge")
                params = {key: details[key] for key in ("service", "scope") if details.get(key)}
                token_response = requests.get(realm, params=params, timeout=(15, 60))
                if not token_response.ok and self.github_token:
                    token_response = requests.get(
                        realm, params=params,
                        auth=("x-access-token", self.github_token), timeout=(15, 60),
                    )
                if not token_response.ok:
                    raise ProvenanceServiceError(
                        f"GHCR token service returned HTTP {token_response.status_code}"
                    )
                self.token = token_response.json().get("token") or token_response.json().get("access_token")
                if not self.token:
                    raise ProvenanceServiceError("GHCR token service returned no token")
                headers["Authorization"] = f"Bearer {self.token}"
                response = requests.get(url, headers=headers, timeout=(15, 60))
        except requests.RequestException as exc:
            raise ProvenanceServiceError(f"Could not read GHCR image metadata: {exc}") from exc
        if response.status_code == 429:
            delay = response.headers.get("Retry-After", "")
            time.sleep(min(int(delay), 120) if delay.isdigit() else 60)
            response = requests.get(url, headers=headers, timeout=(15, 60))
        if response.status_code in (401, 403, 404):
            raise LicenseValidationError(
                f"Build image {self.repository} is unavailable (HTTP {response.status_code}); "
                "cannot verify its recipe revision"
            )
        if not response.ok:
            raise ProvenanceServiceError(
                f"GHCR image metadata returned HTTP {response.status_code}"
            )
        return response.json()

    def image_labels(self, tag, architecture):
        manifest = self._request(f"manifests/{quote(tag, safe=':@')}", MANIFEST_ACCEPT)
        if "manifests" in manifest:
            matches = [
                item for item in manifest["manifests"]
                if (item.get("platform") or {}).get("architecture") == architecture
                and (item.get("platform") or {}).get("os", "linux") == "linux"
            ]
            if len(matches) != 1:
                raise LicenseValidationError(
                    f"Build image has no unique linux/{architecture} manifest"
                )
            manifest = self._request(
                f"manifests/{quote(matches[0]['digest'], safe=':')}", MANIFEST_ACCEPT
            )
        digest = (manifest.get("config") or {}).get("digest")
        if not digest:
            raise LicenseValidationError("Build image manifest has no config digest")
        config = self._request(f"blobs/{quote(digest, safe=':')}", "*/*")
        return (config.get("config") or config.get("Config") or {}).get("Labels") or {}


def get_build_provenance(container_name, github_token=None):
    try:
        image_stem, build_date = container_name.rsplit("_", 1)
    except ValueError as exc:
        raise LicenseValidationError("Container name has no build date") from exc
    if not re.fullmatch(r"[0-9]{8}", build_date):
        raise LicenseValidationError("Container name needs an eight-digit build date")
    architecture = "arm64" if "_arm64_" in container_name or image_stem.endswith("_arm64") else "amd64"
    stems = [image_stem]
    if architecture == "arm64":
        if "_arm64_" in image_stem:
            base, version_part = image_stem.split("_arm64_", 1)
            stems.append(f"{base}_{version_part}_arm64")
        elif image_stem.endswith("_arm64"):
            base_and_version = image_stem[:-6]
            if "_" in base_and_version:
                base, version_part = base_and_version.rsplit("_", 1)
                stems.append(f"{base}_arm64_{version_part}")
    candidates = [
        (REGISTRY, f"neurodesk/{stem.lower()}", github_token)
        for stem in dict.fromkeys(stems)
    ] + [
        (DOCKER_HUB_REGISTRY, f"vnmd/{stem.lower()}", None)
        for stem in dict.fromkeys(stems)
    ]
    last_error = None
    for registry, repository, token in candidates:
        try:
            labels = RegistryReader(repository, token, registry).image_labels(build_date, architecture)
            prefix = "org.opencontainers.image."
            revision = labels.get(prefix + "revision") or ""
            title = labels.get(prefix + "title") or ""
            version = labels.get(prefix + "version") or ""
            documentation = labels.get(prefix + "documentation") or ""
            source = labels.get(prefix + "source") or ""
            match = DOC_PATTERN.fullmatch(documentation)
            if (
                not re.fullmatch(r"[0-9a-f]{40}", revision)
                or not match
                or match.group("revision") != revision
                or source.lower().rstrip("/") != "https://github.com/neurodesk/neurocontainers"
            ):
                raise LicenseValidationError("Build image lacks matching recipe revision labels")
            expected_names = {f"{title}_{version}"}
            if architecture == "arm64":
                if not title.endswith("_arm64"):
                    raise LicenseValidationError(
                        "Build image architecture label does not match the container"
                    )
                base_title = title[:-6]
                if version.endswith("_" + build_date):
                    expected_names.add(
                        f"{base_title}_{version[:-(len(build_date) + 1)]}_arm64_{build_date}"
                    )
            if container_name not in expected_names:
                raise LicenseValidationError(
                    f"Build image labels identify {sorted(expected_names)!r}, "
                    f"not {container_name!r}"
                )
            return {
                "revision": revision,
                "recipe": match.group("recipe"),
                "image": f"{registry.removeprefix('https://')}/{repository}:{build_date}",
            }
        except LicenseValidationError as exc:
            last_error = exc
    raise last_error or LicenseValidationError("No matching build image was found")
