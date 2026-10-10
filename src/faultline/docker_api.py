from dataclasses import dataclass

import docker

PROJECT = "ai-chaos"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
ONEOFF_LABEL = "com.docker.compose.oneoff"


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    service: str
    state: str
    health: str
    restarts: int
    oom_killed: bool


def list_project_containers(client=None, project: str = PROJECT) -> list[ContainerInfo]:
    client = client or docker.from_env()
    found = client.containers.list(all=True, filters={"label": f"{PROJECT_LABEL}={project}"})
    out = []
    for c in found:
        labels = c.labels or {}
        if labels.get(ONEOFF_LABEL) == "True":
            continue
        state = c.attrs.get("State", {})
        out.append(
            ContainerInfo(
                name=c.name,
                service=labels.get(SERVICE_LABEL, ""),
                state=state.get("Status", "unknown"),
                health=(state.get("Health") or {}).get("Status", "none"),
                restarts=int(c.attrs.get("RestartCount", 0)),
                oom_killed=bool(state.get("OOMKilled", False)),
            )
        )
    return sorted(out, key=lambda i: i.name)
