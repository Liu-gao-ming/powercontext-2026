# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Process identity shared by Server log and trace adapters."""

from importlib.metadata import PackageNotFoundError, version
from uuid import uuid4

from opentelemetry.sdk.resources import SERVICE_NAME, Resource

_INSTANCE_ID = uuid4().hex


def service_resource(role: str = "server") -> Resource:
    """Build the process identity shared by log and trace adapters.

    Source checkouts, vendored trees, and frozen bundles have no installed distribution
    metadata, so an unresolvable version omits the field instead of failing configuration.
    """

    resource = Resource.create()
    if str(resource.attributes.get(SERVICE_NAME, "")).startswith("unknown_service"):
        resource = resource.merge(Resource({SERVICE_NAME: "powercontext-server"}))
    identity = {
        "service.instance.id": _INSTANCE_ID,
        "powercontext.service.role": role,
    }
    installed_version = _installed_version()
    if installed_version is not None:
        identity["service.version"] = installed_version
    return resource.merge(Resource(identity))


def _installed_version() -> str | None:
    try:
        return version("powercontext")
    except PackageNotFoundError:
        return None
