import configparser
import json
import logging
import os
import re
import signal
import subprocess
from typing import Any, Dict, List, Optional

import base64
import boto3
import docker
import wandb
from wandb.errors import CommError, LaunchError

from .abstract import AbstractRun, AbstractRunner, Status
from .._project_spec import (
    DEFAULT_LAUNCH_METADATA_PATH,
    get_entry_point_command,
    LaunchProject,
)
from ..docker import (
    build_docker_image_if_needed,
    docker_image_exists,
    docker_image_inspect,
    generate_docker_base_image,
    get_docker_command,
    pull_docker_image,
    validate_docker_installation,
)
from ..utils import (
    PROJECT_DOCKER_ARGS,
    PROJECT_SYNCHRONOUS,
)


_logger = logging.getLogger(__name__)


class AWSSubmittedRun(AbstractRun):
    """Instance of ``AbstractRun`` corresponding to a subprocess launched to run an entry point command locally."""

    def __init__(self, command_proc: "subprocess.Popen[bytes]") -> None:
        super().__init__()
        self.command_proc = command_proc

    @property
    def id(self) -> int:
        return self.command_proc.pid

    def wait(self) -> bool:
        return self.command_proc.wait() == 0

    def cancel(self) -> None:
        # Interrupt child process if it hasn't already exited
        if self.command_proc.poll() is None:
            # Kill the the process tree rooted at the child if it's the leader of its own process
            # group, otherwise just kill the child
            try:
                if self.command_proc.pid == os.getpgid(self.command_proc.pid):
                    os.killpg(self.command_proc.pid, signal.SIGTERM)
                else:
                    self.command_proc.terminate()
            except OSError:
                # The child process may have exited before we attempted to terminate it, so we
                # ignore OSErrors raised during child process termination
                _logger.info(
                    "Failed to terminate child process (PID %s). The process may have already exited.",
                    self.command_proc.pid,
                )
            self.command_proc.wait()

    def get_status(self) -> Status:
        exit_code = self.command_proc.poll()
        if exit_code is None:
            return Status("running")
        if exit_code == 0:
            return Status("finished")
        return Status("failed")


class AWSRunner(AbstractRunner):
    """Runner class, uses a project to create a LocallySubmittedRun."""

    def run(self, launch_project: LaunchProject) -> Optional[AbstractRun]:
        validate_docker_installation()
        region = None
        if launch_project.aws.get("region") is not None:
            region = launch_project.aws["region"]
        elif launch_project.aws.get("config_path") is not None:
            config = configparser.ConfigParser()
            config.read(launch_project.aws.get("config_path"))
            region = config.get("default", "region")
        elif os.path.exists(os.path.expanduser("~/.aws/config")):
            config = configparser.ConfigParser()
            config.read(os.path.expanduser("~/.aws/config"))
            region = config.get("default", "region")
        assert region is not None, "AWS region not specified."

        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if (
            access_key is None
            or secret_key is None
            and os.path.exists(os.path.expanduser("~/.aws/credentials"))
        ):
            config = configparser.ConfigParser()
            config.read(os.path.expanduser("~/.aws/credentials"))
            access_key = config.get("default", "aws_access_key_id")
            secret_key = config.get("default", "aws_secret_access_key")
        if access_key is None or secret_key is None:
            raise LaunchError("AWS credentials not found.")

        # client = boto3.client(
        #     "sts", aws_access_key_id=access_key, aws_secret_access_key=secret_key
        # )
        ecr_client = boto3.client(
            "ecr",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        token = ecr_client.get_authorization_token()
        username, password = (
            base64.b64decode(token["authorizationData"][0]["authorizationToken"])
            .decode()
            .split(":")
        )
        print(username, password)
        auth_config = {"username": username, "password": password}

        repository = launch_project.aws.get("repository") or "my-test-repository"
        aws_tag = (
            token["authorizationData"][0]["proxyEndpoint"].lstrip("https://")
            + f"/{repository}"
        )

        docker_args: Dict[str, Any] = self.backend_config[PROJECT_DOCKER_ARGS]

        entry_point = launch_project.get_single_entry_point()

        entry_cmd = entry_point.command
        copy_code = True
        if launch_project.docker_image:
            pull_docker_image(launch_project.docker_image)
            copy_code = False
        else:
            # TODO: potentially pull the base_image
            if not docker_image_exists(launch_project.base_image):
                if generate_docker_base_image(launch_project, entry_cmd) is None:
                    raise LaunchError("Unable to build base image")
            else:
                wandb.termlog(
                    "Using existing base image: {}".format(launch_project.base_image)
                )

        command_args = []
        command_separator = " "

        container_inspect = docker_image_inspect(launch_project.base_image)
        container_workdir = container_inspect["ContainerConfig"].get("WorkingDir", "/")
        container_env: List[str] = container_inspect["ContainerConfig"]["Env"]

        if launch_project.docker_image is None or launch_project.build_image:
            image = build_docker_image_if_needed(
                launch_project=launch_project,
                api=self._api,
                copy_code=copy_code,
                workdir=container_workdir,
                container_env=container_env,
                tag=aws_tag,
            )
        else:
            image = launch_project.docker_image
        docker_client = docker.from_env()
        print(aws_tag, auth_config)
        resp = docker_client.login(
            username, password, registry=token["authorizationData"][0]["proxyEndpoint"]
        )
        print(resp)
        resp = docker_client.images.push(
            aws_tag,
            auth_config=auth_config,
            tag="latest",
        )
        print(resp)

        command_args += get_docker_command(
            image=image,
            launch_project=launch_project,
            api=self._api,
            workdir=container_workdir,
            docker_args=docker_args,
        )
        if self.backend_config.get("runQueueItemId"):
            try:
                self._api.ack_run_queue_item(
                    self.backend_config["runQueueItemId"], launch_project.run_id
                )
            except CommError:
                wandb.termerror(
                    "Error acking run queue item. Item lease may have ended or another process may have acked it."
                )
                return None

        command_args += get_entry_point_command(
            entry_point, launch_project.override_args
        )

        command_str = command_separator.join(command_args)
        sanitized_command_str = re.sub(
            r"WANDB_API_KEY=\w+", "WANDB_API_KEY", command_str
        )
        with open(
            os.path.join(launch_project.aux_dir, DEFAULT_LAUNCH_METADATA_PATH), "w"
        ) as fp:
            json.dump(
                {
                    **launch_project.launch_spec,
                    "command": sanitized_command_str,
                    "dockerfile_contents": launch_project._dockerfile_contents,
                },
                fp,
            )
        wandb.termlog("Pushing container to ECR with tag: ")
        # wandb.termlog(
        #     "Launching run in docker with command: {}".format(sanitized_command_str)
        # )
        # run = _run_entry_point(command_str, launch_project.project_dir)
        # if synchronous:
        #     run.wait()
        # return run


def _run_entry_point(command: str, work_dir: str) -> AbstractRun:
    """Run an entry point command in a subprocess.

    Arguments:
        command: Entry point command to run
        work_dir: Working directory in which to run the command

    Returns:
        An instance of `LocalSubmittedRun`
    """
    env = os.environ.copy()
    if os.name == "nt":
        # we are running on windows
        process = subprocess.Popen(
            ["cmd", "/c", command], close_fds=True, cwd=work_dir, env=env
        )
    else:
        process = subprocess.Popen(
            ["bash", "-c", command],
            close_fds=True,
            cwd=work_dir,
            env=env,
        )

    return AWSSubmittedRun(process)
