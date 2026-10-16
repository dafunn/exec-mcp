# exec-mcp

The MCP server that runs a build task's declared commands, and its git tools.

It is the only place in a build run that spawns a process. It holds no
credentials, has no route off the task network, and runs nothing the run's spec
did not declare: `run_task(repo, name)` executes argv from the manifest and
refuses anything else. A tool present in the image but named by no command is
unreachable.

## Tools

| tool | |
| --- | --- |
| `list_tasks` | the commands this run declared, per repo |
| `run_task` | run one of them and return its output |
| `task_output` | read or grep the full log of an earlier run |
| `git_status` `git_diff` `git_add` `git_commit` `git_log` | the write repos' history |

Command output is capped in the reply and spilled whole to `/exec-runs`;
`task_output` reads the rest.

## The image

This repo builds one image (`podman build -t exec-mcp .`). It is also the base
a project derives from when a task needs a toolchain this one does not carry:

```dockerfile
FROM exec-mcp:latest
RUN set -eux; apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg; \
    rm -rf /var/lib/apt/lists/*
```

Build the image locally.

The server reports its release in the MCP `initialize` result
(`serverInfo.version`). To stamp it, build from a checkout with tags:

```
docker build --build-arg EXEC_MCP_VERSION=$(git describe --tags --match 'exec-mcp-v*' | sed 's/^exec-mcp-v//') .
```

A build without it reports `dev`.

Three rules for deriving:

- **Do not override `CMD`, `ENTRYPOINT`, `WORKDIR` or `USER`.** The launcher
  expects this image's entrypoint to start the server.
- **Add tools only.** What belongs here is a toolchain with a closed
  dependency set - it works on any input. A tool whose requirements follow from
  one project's content belongs to that project.
- **Rebuild when `pyproject.toml`, `uv.lock` or the server source changes.** The image
  is self-contained; nothing is inherited from a caller's checkout.

## The wire contract

`run_task`'s first line is a header the calling build driver parses to
classify an outcome. Both sides assert against `wire_contract.toml`, which
ships at `/app/mcp/wire_contract.toml` in the image, so the driver checks
against the version it is about to run.

Change a prefix or a `short` form and the contract changes with it, or both
sides fail.

## Tests

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```
uv sync --frozen --extra dev
.venv/bin/python -m pytest tests
```

The test configuration makes the flat root modules importable whether pytest
is run through its console script or as `python -m pytest`.
