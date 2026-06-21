# TinyPyMCP - operator-only / authenticated admin MCP server.
# Runs NON-ROOT inside the container: this (not path_guard) is the real OS
# boundary for run_command. The agent's workspace root and the SQLite stores
# live on mounted volumes (see docker-compose.yml); the DBs sit OUTSIDE the
# workspace root, so file tools cannot reach them.
FROM python:3.12-slim

# Non-root runtime user with a fixed uid (named volumes inherit these dir perms
# on first init).
RUN useradd -r -u 10001 -m -d /home/app app

WORKDIR /app

# Dependencies (mirror pyproject [project].dependencies). Installed before src
# for layer caching; ovh is lazy-imported but present for the host-layer tools.
RUN pip install --no-cache-dir "mcp>=1.2.0" "httpx>=0.27" "ovh>=1.1" "uptime-kuma-api>=1.0"

COPY src ./src
COPY pyproject.toml README.md ./

# Data (oauth/memory DBs + audit) and the agent workspace live on volumes,
# owned by the non-root user.
RUN mkdir -p /data/logs /work/workspaces && chown -R app:app /data /work /app

USER app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765

# Defaults; docker-compose overrides command with auth/profile flags.
CMD ["python", "-m", "src.server", "--auth", "oauth", "--transport", "http", "--port", "8765"]
