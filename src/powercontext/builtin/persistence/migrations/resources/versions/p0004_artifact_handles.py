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

"""Add scope-local handles that target exact Artifact revisions."""

import sqlalchemy as sa
from alembic import op

revision = "p0004"
down_revision = "p0003"


def upgrade() -> None:
    op.create_table(
        "pc_artifact_handles",
        sa.Column("scope_id", sa.String(length=256), nullable=False),
        sa.Column("family", sa.String(length=128), nullable=False),
        sa.Column("handle_key", sa.String(length=128), nullable=False),
        sa.Column("artifact_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_pc_artifact_handles_revision_positive"),
        sa.ForeignKeyConstraint(
            ("scope_id", "family", "artifact_id", "revision"),
            ("pc_artifacts.scope_id", "pc_artifacts.family", "pc_artifacts.artifact_id", "pc_artifacts.revision"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("scope_id", "family", "handle_key"),
    )
