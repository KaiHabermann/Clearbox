# Notes for agents working on Clearbox

Tests are plain scripts in `tests/`; each file's docstring carries its exact run command. Most of
them need a mock server of their own:

    uv run clearbox --mock --port <free port> --db <temp path> --workdir <temp path>
    uv run python tests/<name>.py http://127.0.0.1:<free port>

`tests/test_openai_harness.py` and `tests/test_review_format.py` need no server. Pick a high
random port and temp paths; never use port 8765 or the project's own database (a live Clearbox
may be running there), and stop the server when you are done.
