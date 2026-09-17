# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Runtime limit for file-level AddResource operations."""

from openviking_cli.utils.config import get_openviking_config


def get_file_operation_concurrency() -> int:
    """Return the configured per-job file operation concurrency."""
    return int(get_openviking_config().queue_workers.add_resource.file_operation_concurrency)
