from fastapi import FastAPI

app = FastAPI(title="LaunchOps")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}
