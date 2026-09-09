# LaunchOps

LaunchOps is a FastAPI service that exposes operational endpoints for launch readiness, starting with a health check so you can verify the API is up and responding.

## Development

### Create the virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Install dependencies

```bash
pip install fastapi uvicorn pytest httpx
```

### Start the API

```bash
uvicorn app.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

### Run tests

```bash
pytest
```
