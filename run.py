"""
Launcher. Run: python run.py
(equivalent to: uvicorn backend.main:app --host 0.0.0.0 --port 8000)
"""
import os
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)
