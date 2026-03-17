from __future__ import annotations

from dataclasses import dataclass
from typing import Any


APS_OAUTH_INTEGRATION = "aps-automation-webinar-alejandro"


@dataclass(frozen=True)
class ModelContext:
    token: str
    region: str
    version_urn: str


def get_token() -> str:
    import viktor as vkt

    integration = vkt.external.OAuth2Integration(APS_OAUTH_INTEGRATION)
    return integration.get_access_token()


def get_model_context(autodesk_file: Any) -> ModelContext:
    if autodesk_file is None:
        raise ValueError("No Autodesk model selected.")

    token = get_token()
    region = autodesk_file.get_region(token)
    version = autodesk_file.get_latest_version(token)

    return ModelContext(
        token=token,
        region=region,
        version_urn=version.urn,
    )
