"""
run.py — Start the chatbot server
==================================
Run this file to start the server:  python run.py

Why not just run uvicorn directly?
On Windows, uvicorn uses a ProactorEventLoop which has a bug with DNS resolution
when using the requests library inside async code. Setting WindowsSelectorEventLoopPolicy
fixes this.
"""

import asyncio
import sys

# Fix for Windows: use the selector event loop instead of the default proactor loop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    # reload=True means the server restarts automatically when you save a file
    uvicorn.run("main:app", reload=True, host="0.0.0.0", port=8000)
