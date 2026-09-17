# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from openviking.service.task_work_index import bind_task_context
from openviking.telemetry import OperationTelemetry, bind_telemetry
from openviking.utils.log_correlation import log_correlation


def test_log_correlation_uses_bound_task_and_telemetry() -> None:
    telemetry = OperationTelemetry(operation="add_resource_job", enabled=True)

    with (
        bind_task_context("task-123", "account", "user"),
        bind_telemetry(telemetry),
    ):
        assert log_correlation(message_id="semantic-456") == (
            f"task_id=task-123 telemetry_id={telemetry.telemetry_id} "
            "message_id=semantic-456"
        )


def test_log_correlation_accepts_explicit_queue_ids_without_context() -> None:
    assert log_correlation(
        task_id="task-1", telemetry_id="tm-1", message_id="embedding-2"
    ) == (
        "task_id=task-1 telemetry_id=tm-1 message_id=embedding-2"
    )
