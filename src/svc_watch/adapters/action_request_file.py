"""action_request_file — real service action: create a request file from a template
with {service} substituted in. The process supervisor eats the file (unlink) and recreates the service.
Not eaten within eat_within_ms → the observer raises P2_request_stuck."""

from __future__ import annotations

import os


class RequestFileAction:
    def __init__(self, file_template: str) -> None:
        if "{service}" not in file_template:
            raise ValueError("template must contain {service}: %r" % file_template)
        self._tmpl = file_template

    def path_for(self, target: str) -> str:
        return self._tmpl.format(service=target)

    def execute(self, target: str) -> None:
        path = self.path_for(target)
        # create 0644; empty flag file (tmpfs is fine, 0 bytes)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)


# ─── self-registration of the base type in the runtime registry (FR-41) ───
def _register() -> None:
    from ..runtime import register_action_builder
    register_action_builder("request_file", lambda a: RequestFileAction(a.params["file"]))


_register()
