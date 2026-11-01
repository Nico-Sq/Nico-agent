"""Start the local Nico-agent Web application."""

import uvicorn


def main() -> None:
    uvicorn.run("career_agent.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
