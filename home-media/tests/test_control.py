import json

import pytest

from home_media.control import RunControl, StopRequested


def test_run_control_ignores_old_stop_request_and_honors_current_run(tmp_path):
    with RunControl(tmp_path) as control:
        stop_path = tmp_path / "worker-stop.json"
        stop_path.write_text(json.dumps({"run_id": "older-run"}), encoding="utf-8")

        control.check()

        stop_path.write_text(json.dumps({"run_id": control.run_id}), encoding="utf-8")
        with pytest.raises(StopRequested, match="current item will be retried"):
            control.check()

