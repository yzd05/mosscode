"""Terminus 2 with a setup-only reliability shim for benchmark containers."""

from harbor.agents.terminus_2.terminus_2 import Terminus2
from harbor.agents.terminus_2.tmux_session import TmuxSession
from harbor.models.trial.paths import EnvironmentPaths


class ReliableTmuxSession(TmuxSession):
    """Retry package-manager downloads without changing the agent loop."""

    def _get_combined_install_command(self, system_info, tools):
        command = super()._get_combined_install_command(system_info, tools)
        if not command:
            return command
        if system_info.get("package_manager") == "apt-get":
            command = command.replace(
                "apt-get update",
                "apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 update",
            )
        return f"for attempt in 1 2 3; do ({command}) && exit 0; sleep $((attempt * 2)); done; exit 1"


class Terminus2Agent(Terminus2):
    """Use native Terminus 2 behavior with a more reliable tmux installation."""

    async def setup(self, environment):
        if self.options.record_terminal_session:
            local_recording_path = environment.trial_paths.agent_dir / "recording.cast"
            remote_recording_path = EnvironmentPaths.agent_dir / "recording.cast"
        else:
            local_recording_path = None
            remote_recording_path = None

        self._session = ReliableTmuxSession(
            session_name=self.name(),
            environment=environment,
            logging_path=EnvironmentPaths.agent_dir / "terminus_2.pane",
            local_asciinema_recording_path=local_recording_path,
            remote_asciinema_recording_path=remote_recording_path,
            pane_width=self.options.tmux_pane_width,
            pane_height=self.options.tmux_pane_height,
            extra_env=self._extra_env,
            user=environment.default_user,
        )
        await self._session.start()
