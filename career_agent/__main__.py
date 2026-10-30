"""Start the local web app on the loopback interface."""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("career_agent.main:app", host="127.0.0.1", port=8000)
