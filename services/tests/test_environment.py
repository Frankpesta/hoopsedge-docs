"""Checks that the local Docker toolchain matches the project's rules."""

import os
import sys

import hoopsedge


def test_runs_on_pinned_python() -> None:
    assert sys.version_info[:2] == (3, 12)


def test_package_is_loaded_from_mounted_source() -> None:
    assert hoopsedge.__file__ == "/workspace/services/hoopsedge/__init__.py"


def test_dry_run_is_on_locally() -> None:
    assert os.environ["HOOPSEDGE_ENV"] == "local"
    assert os.environ["DRY_RUN"] == "true"


def test_no_aws_credentials_in_container() -> None:
    leaked = [name for name in os.environ if name.startswith("AWS_")]
    assert leaked == []
    assert not os.path.exists(os.path.expanduser("~/.aws"))
