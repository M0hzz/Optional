"""python -m optlab.api: serve the simulator on 127.0.0.1:8600 (OPTLAB_API_HOST / OPTLAB_API_PORT)."""
import os

import uvicorn

from . import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("OPTLAB_API_HOST", "127.0.0.1"),
                port=int(os.environ.get("OPTLAB_API_PORT", "8600")), log_level="info", proxy_headers=False)
